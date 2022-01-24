"""Extract_load is a basic EL style BlockSet implementation."""
import asyncio
import logging
from asyncio import Task
from typing import AsyncIterator, Dict, List, Optional, Set, Tuple

import structlog
from async_generator import asynccontextmanager
from meltano.core.elt_context import PluginContext
from meltano.core.job import Job
from meltano.core.logging import JobLoggingService, OutputLogger
from meltano.core.plugin import PluginType
from meltano.core.plugin.project_plugin import ProjectPlugin
from meltano.core.plugin.settings_service import PluginSettingsService
from meltano.core.plugin_invoker import PluginInvoker, invoker_factory
from meltano.core.project import Project
from meltano.core.project_plugins_service import ProjectPluginsService
from meltano.core.project_settings_service import ProjectSettingsService
from meltano.core.runner import RunnerError
from sqlalchemy.orm import Session

from .blockset import BlockSet, BlockSetValidationError
from .future_utils import first_failed_future, handle_producer_line_length_limit_error
from .ioblock import IOBlock
from .singer import SingerBlock

logger = structlog.getLogger(__name__)


class ELBContext:
    def __init__(
        self,
        project: Project,
        plugins_service: ProjectPluginsService = None,
        session: Session = None,
        job: Optional[Job] = None,
        base_output_logger: Optional[OutputLogger] = None,
    ):
        """Use an ELBContext to pass information on to ExtractLoadBlocks.

        Args:
            project: The project to use.
            plugins_service: The plugins service to use.
            session: The session to use.
            job: The job within this context should run.
            base_output_logger: The base logger to use.
        """
        self.project = project
        self.plugins_service = plugins_service or ProjectPluginsService(project)

        self.session = session
        self.job = job

        self.base_output_logger = base_output_logger

    @property
    def elt_run_dir(self) -> str:
        """Obtain the run directory for the current job."""
        if self.job:
            return self.project.job_dir(self.job.job_id, str(self.job.run_id))


class ELBContextBuilder:
    def __init__(
        self,
        project: Project,
        plugins_service: ProjectPluginsService = None,
        session: Session = None,
        job: Optional[Job] = None,
    ):
        """Initialize a new `ELBContextBuilder` that can be used to upgrade plugins to blocks for use in a ExtractLoadBlock.

        Args:
            project: the meltano project for the context.
            plugins_service: the plugins service for the context.
            session: the database session for the context.
            job:
        """
        self.project = project
        self.plugins_service = plugins_service or ProjectPluginsService(project)
        self.session = session
        self.job = job

        self._env = {}
        self._blocks = []

        self._base_output_logger = None

    def make_block(
        self,
        plugin: ProjectPlugin,
        plugin_args: Optional[List[str]] = None,
        plugin_config_overrides: Optional[dict] = None,
    ) -> SingerBlock:
        """Create a new `SingerBlock` object, from a plugin.

        Args:
            plugin: The plugin to be executed.
            plugin_args: The arguments to be passed to the plugin.
            plugin_config_overrides: The env overrides, primarily used by mappers to inject name of the requested mapping.
        Returns:
            The new `SingerBlock` object.
        """
        ctx = self.plugin_context(plugin, env=self._env.copy())

        block = SingerBlock(
            block_ctx=ctx,
            project=self.project,
            plugins_service=self.plugins_service,
            plugin_invoker=self.invoker_for(ctx, plugin_config_overrides),
            plugin_args=plugin_args,
        )
        self._blocks.append(block)
        self._env.update(ctx.env)
        return block

    def plugin_context(
        self,
        plugin: ProjectPlugin,
        env: dict = None,
    ) -> PluginContext:
        """Create context object for a plugin.

        Args:
            plugin: The plugin to create the context for.
            env: Environment override dictionary. Defaults to None.
        Returns:
            A new `PluginContext` object.
        """
        return PluginContext(
            plugin=plugin,
            settings_service=PluginSettingsService(
                self.project,
                plugin,
                plugins_service=self.plugins_service,
                env_override=env,
            ),
            session=self.session,
        )

    def invoker_for(
        self,
        plugin_context: PluginContext,
        plugin_config_override: Optional[Dict] = None,
    ) -> PluginInvoker:
        """Create an invoker for a plugin from a PluginContext."""
        return invoker_factory(
            self.project,
            plugin_context.plugin,
            context=self.context(),
            run_dir=self.elt_run_dir,
            plugins_service=self.plugins_service,
            plugin_settings_service=plugin_context.settings_service,
            plugin_config_override=plugin_config_override,
        )

    @property
    def elt_run_dir(self) -> str:
        """Get the run directory for the current job."""
        if self.job:
            return self.project.job_dir(self.job.job_id, str(self.job.run_id))

    def context(self) -> ELBContext:
        """Create an ELBContext object from the current builder state."""
        return ELBContext(
            project=self.project,
            plugins_service=self.plugins_service,
            session=self.session,
            job=self.job,
            base_output_logger=self._base_output_logger,
        )


class ExtractLoadBlocks(BlockSet):  # noqa: WPS214
    """A basic BlockSet interface implementation that supports running basic EL (extract, load) patterns."""

    def __init__(
        self,
        context: ELBContextBuilder,
        blocks: Tuple[IOBlock],
    ):
        """Initialize a basic BlockSet suitable for executing ELT tasks.

        Args:
            context: the elt context to use for this elt run.
            blocks: the IOBlocks that should be used for this elt run.
        """
        self.context = context
        self.blocks = blocks
        self.project_settings_service = ProjectSettingsService(
            self.context.project,
            config_service=self.context.plugins_service.config_service,
        )

        log_file = "run.log"
        if self.context.job:
            job_logging_service = JobLoggingService(self.context.project)
            log_file = job_logging_service.generate_log_name(
                self.context.job.job_id, self.context.job.run_id
            )

        self.output_logger = OutputLogger(log_file)

        self._process_futures = None
        self._stdout_futures = None
        self._stderr_futures = None
        self._errors = []

    def index_last_input_done(self) -> int:
        """Return index of the block furthest from the start that has exited and required input."""
        for idx, block in reversed(list(enumerate(self.blocks))):
            if block.requires_input and block.proxy_stderr.done():
                return idx

    def upstream_complete(self, index: int) -> bool:
        """Return whether blocks upstream from a given block index are already done."""
        for idx, block in enumerate(self.blocks):
            if idx >= index:
                return True
            if block.process_future.done():
                continue
            return False

    async def upstream_stop(self, index) -> None:
        """Stop all blocks upstream of a given index."""
        for block in reversed(self.blocks[:index]):
            await block.stop()

    async def process_wait(
        self, output_exception_future: Task, subset: int = None
    ) -> Set[Task]:
        """Wait on all process futures in the block set."""
        done, _ = await asyncio.wait(
            [*self.process_futures[subset:], output_exception_future],
            return_when=asyncio.FIRST_COMPLETED,
        )
        return done

    def validate_set(self) -> None:  # noqa: WPS231
        """Validate a ExtractLoad block set to ensure its valid and runnable.

        Raises: BlockSetValidationError on validation failure
        """
        if not self.blocks:
            raise BlockSetValidationError("No blocks in set.")

        if self.head.consumer:
            raise BlockSetValidationError("first block in set should not be consumer")

        if self.tail.producer:
            raise BlockSetValidationError("last block in set should not be a producer")

        for block in self.intermediate:
            if not block.consumer or not block.producer:
                raise BlockSetValidationError(
                    "intermediate blocks must be producers AND consumers"
                )

    async def run(self, session) -> bool:
        """Build the IO chain and execute the actual ELT task.

        Raises:
            RunnerError if failures are encountered during execution.
        """
        async with self._start_blocks(session):
            await self._link_io()
            manager = ELBExecutionManager(self)
            await manager.run()
            return True

    @staticmethod
    async def terminate(self, graceful: bool = False) -> bool:
        """Terminate an in flight ExtractLoad execution, potentially disruptive.

        Not actually implemented yet.

        Args:
            graceful: Whether or not the BlockSet should try to gracefully quit.
        Returns:
            Whether or not the BlockSet terminated successfully.
        """
        if graceful:
            raise NotImplementedError

        for block in self.blocks:
            block.stop(kill=True)

    @property
    def process_futures(self) -> List[Task]:
        """Return the futures of the blocks subprocess calls."""
        if self._process_futures is None:
            self._process_futures = [block.process_future for block in self.blocks]
        return self._process_futures

    @property
    def stdout_futures(self) -> List[Task]:
        """Access the futures of the blocks stdout proxy tasks."""
        if self._stdout_futures is None:
            self._stdout_futures = [block.proxy_stdout() for block in self.blocks]
        return self._stdout_futures

    @property
    def stderr_futures(self) -> List[Task]:
        """Access the futures of the blocks stderr proxy tasks."""
        if self._stderr_futures is None:
            self._stderr_futures = [block.proxy_stderr() for block in self.blocks]
        return self._stderr_futures

    @property
    def head(self) -> IOBlock:
        """Obtain the first block in the block set."""
        return self.blocks[0]

    @property
    def tail(self) -> IOBlock:
        """Obtain the last block in the block set."""
        return self.blocks[-1]

    @property
    def intermediate(self) -> Tuple[IOBlock]:
        """Obtain the intermediate blocks in the set - excluding the first and last block."""
        return self.blocks[1:-1]

    @asynccontextmanager
    async def _start_blocks(self, session) -> AsyncIterator[None]:
        """Start the blocks in the block set.

        Args:
            session: The session to use for the blocks.
        """
        try:  # noqa:  WPS229
            for block in self.blocks:
                await block.pre({"session": session})
                await block.start()
            yield
        finally:
            await self._cleanup()

    async def _cleanup(self) -> None:
        for block in self.blocks:
            await block.post()

    async def _link_io(self) -> None:  # noqa: WPS231
        """Link the blocks in the set together.

        This method does one last validation check to ensure that a consumer has a producer upstream.

        Raises:
            BlockSetValidationError: if consumer does not have an upstream producer.
        """
        for idx, block in enumerate(self.blocks):
            logger_base = logger.bind(
                consumer=block.consumer,
                producer=block.producer,
                string_id=block.string_id,
                cmd_type="elb",
            )
            if logger.isEnabledFor(logging.DEBUG):
                block.stdout_link(
                    self.output_logger.out(
                        block.string_id,
                        logger_base.bind(stdio="stdout"),
                    )
                )
            block.stderr_link(
                self.output_logger.out(
                    block.string_id,
                    logger_base.bind(stdio="stderr"),
                )
            )
            if block.consumer:
                if idx != 0 and self.blocks[idx - 1].producer:
                    self.blocks[idx - 1].stdout_link(
                        block.stdin
                    )  # link previous blocks stdout with current blocks stdin
                else:
                    raise BlockSetValidationError(
                        "run step requires input but has no upstream"
                    )


class ELBExecutionManager:
    """Execution manager for ExtractLoadBlock sets."""

    def __init__(self, elb: ExtractLoadBlocks) -> None:
        """Initialize the ELBExecutionManager which will handle the actual runtime management of the block set.

        Args:
            elb: The ExtractLoadBlocks to manage.
        """
        self.elb = elb
        self.stream_buffer_size = self.elb.project_settings_service.get(
            "elt.buffer_size"
        )
        self.line_length_limit = self.stream_buffer_size // 2

        self._producer_code = None
        self._consumer_code = None

    async def run(self) -> None:
        """Run is used to actually perform the execution of the ExtractLoadBlock set.

        That entails starting the blocks, waiting for them to complete, ensuring that exceptions are handled, and
        stopping blocks or waiting for IO to complete as appropriate.

        Raises:
            RunnerError: if any blocks in the set finished with a non 0 exit code
        """
        await self._wait_for_process_completion(self.elb.head)
        _check_exit_codes(self._producer_code, self._consumer_code)

    async def _complete_upstream(self) -> None:
        """Wait for the upstream blocks to complete."""
        producer = self.elb.head
        consumer = self.elb.tail

        if self.elb.upstream_complete(len(self.elb.blocks) - 1):
            self._producer_code = producer.process_future.result()
        else:
            # If the last consumer (target) completes before the upstream producers, it failed before processing all
            # output. So we should kill the upstream producers and cancel output processing since there's no final
            # destination to forward output to.
            await self.elb.upstream_stop(len(self.elb.blocks) - 1)
            # Pretend the producer (tap) finished successfully since it didn't itself fail
            self._producer_code = 0
        # Wait for all buffered consumer (target) output to be processed
        await asyncio.wait([consumer.proxy_stdout(), consumer.proxy_stderr()])

    async def _wait_for_process_completion(  # noqa: WPS213 WPS217
        self, current_head: IOBlock
    ) -> Tuple[int, int]:
        """Wait for the current head block to complete or for an error to occur.

        Args:
            current_head: The current head block
        Raises:
            RunnerError: if any intermediate blocks failed.
        """
        start_idx = self.elb.blocks.index(current_head)
        remaining_blocks = self.elb.blocks[start_idx:]

        if remaining_blocks is None or current_head == self.elb.tail:
            return

        stdout_futures = [block.proxy_stdout() for block in remaining_blocks]
        stderr_futures = [block.proxy_stderr() for block in remaining_blocks]

        output_exception_future = asyncio.ensure_future(
            asyncio.wait(
                [*stdout_futures, *stderr_futures],
                return_when=asyncio.FIRST_EXCEPTION,
            )
        )

        logger.debug("waiting for process completion or exception")
        done = await self.elb.process_wait(output_exception_future, start_idx)

        output_futures_failed = first_failed_future(output_exception_future, done)
        if output_futures_failed:
            # Special behavior for a producer stdout handler raising a line length limit error.
            if self.elb.head.proxy_stdout() == output_futures_failed:
                handle_producer_line_length_limit_error(
                    output_futures_failed.exception(),
                    line_length_limit=self.line_length_limit,
                    stream_buffer_size=self.stream_buffer_size,
                )
            raise output_futures_failed.exception()
        else:
            # If all the output handlers completed without raising an exception,
            # we still need to wait for all the underlying block processes to complete.
            done, _ = await asyncio.wait(
                self.elb.process_futures,
                return_when=asyncio.FIRST_COMPLETED,
            )
        if self.elb.tail.process_future.done():
            logger.debug("tail consumer completed first")
            self._consumer_code = self.elb.tail.process_future.result()
            await self._complete_upstream()
        elif current_head.process_future.done():
            logger.debug("head producer completed first as expected")
            await self._handle_head_completed(current_head, start_idx)
        else:
            logger.warning("Intermediate block in sequence failed.")
            await self._stop_all_blocks(start_idx)
            raise RunnerError(
                "Unexpected completion sequence in ExtractLoadBlock set. Intermediate block (likely a mapper) failed.",
                {
                    PluginType.EXTRACTORS: 1,
                    PluginType.LOADERS: 1,
                },
            )

    async def _handle_head_completed(
        self, current_head: IOBlock, start_idx: int
    ) -> None:
        next_head = self.elb.blocks[start_idx + 1]

        if current_head is self.elb.head:
            self._producer_code = current_head.process_future.result()

        await asyncio.wait([current_head.proxy_stdout(), current_head.proxy_stderr()])
        # Close next inline stdin so downstream can cascade and complete naturally
        next_head.stdin.close()
        await next_head.stdin.wait_closed()

        if next_head is self.elb.tail:
            logger.debug("tail consumer is next block, wrapping up")
            # Wait for all buffered target output to be processed
            await asyncio.wait([next_head.proxy_stdout(), next_head.proxy_stderr()])

            # Wait for target process future to complete
            await next_head.process_future
            self._consumer_code = next_head.process_future.result()
            return  # break our recursion

        await self._wait_for_process_completion(next_head)

    async def _stop_all_blocks(self, idx: int = 0) -> None:
        """Close stdin and stop all blocks inclusive of index.

        Args:
            idx: starting index of the block's to stop.
        """
        for block in self.elb.blocks[idx:]:
            await block.close_stdin()
            await block.stop()


def _check_exit_codes(producer_code: int, consumer_code: int) -> None:
    """Check exit codes for failures, and raise the appropriate RunnerError if needed.

    Args:
        producer_code: exit code of the producer (tap)
        consumer_code: exit code of the consumer (target)
    Raises:
        RunnerError: if the producer or consumer exit codes are non-zero
    """
    if producer_code and consumer_code:
        raise RunnerError(
            "Extractor and loader failed",
            {PluginType.EXTRACTORS: producer_code, PluginType.LOADERS: consumer_code},
        )

    if producer_code:
        raise RunnerError("Extractor failed", {PluginType.EXTRACTORS: producer_code})

    if consumer_code:
        raise RunnerError("Loader failed", {PluginType.LOADERS: consumer_code})
