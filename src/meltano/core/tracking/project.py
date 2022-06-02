"""Project context for the Snowplow tracker."""

from __future__ import annotations

import json
import uuid
from enum import Enum, auto
from functools import cached_property
from typing import Any

from snowplow_tracker import SelfDescribingJson
from structlog.stdlib import get_logger

from meltano.core.project import Project
from meltano.core.settings_service import SettingsService
from meltano.core.utils import hash_sha256

logger = get_logger(__name__)


class ProjectUUIDSource(Enum):
    """The source of the `project_uuid` used for telemetry."""

    explicit = auto()
    """The UUID was explicitly provided in the config as the `project_id`."""

    derived = auto()
    """The UUID was derived by hashing the `project_id` in the config."""

    random = auto()
    """The UUID was randomly generated (UUID v4) since no `project_id` was configured."""


class ProjectContext(SelfDescribingJson):
    def __init__(self, project: Project):
        logger.debug(
            f"Initializing '{type(self).__module__}.{type(self).__qualname__}' for {project!r}"
        )
        self.project = project
        self.settings_service = SettingsService(project)
        super().__init__(
            "iglu:com.meltano/project_context/jsonschema/1-0-0",
            {
                "context_uuid": uuid.uuid4(),
                "project_uuid": self.project_uuid,
                "project_uuid_source": self.project_uuid_source.name,
                "client_uuid": self.client_uuid,
                "environment_name": (
                    self.project.active_environment.name
                    if self.project.active_environment
                    else None
                ),
            },
        )

    @property
    def project_uuid_source(self) -> ProjectUUIDSource:
        self.project_uuid  # Ensure the `project_uuid` has been generated
        return self._project_uuid_source

    @cached_property
    def project_uuid(self) -> uuid.UUID:
        """The `project_id` from the project config file.

        If it is not found (e.g. first time run), generate a valid v4 UUID, and and store it in the
        project config file.
        """
        project_id_str = self.settings_service.get("project_id")

        if project_id_str:
            try:
                # Project ID might already be a UUID
                project_id = uuid.UUID(project_id_str)
            except ValueError:
                # If the project ID is not a UUID, then we hash it, and use the hash to make a UUID
                project_id = uuid.UUID(hash_sha256(project_id_str)[::2])
                self._project_uuid_source = ProjectUUIDSource.derived
            else:
                self._project_uuid_source = ProjectUUIDSource.explicit
        else:
            project_id = uuid.uuid4()
            self._project_uuid_source = ProjectUUIDSource.random

            if self.send_anonymous_usage_stats:
                # If we are set to track anonymous usage stats, also store the generated project_id
                # back to the project config file so that it persists between meltano runs.
                self.settings_service.set("project_id", str(project_id))

        return project_id

    @cached_property
    def client_uuid(self) -> uuid.UUID:
        """The `client_id` from the non-versioned `analytics.json`.

        If it is not found (e.g. first time run), generate a valid v4 UUID, and store it in
        `analytics.json`.
        """
        analytics_json_path = self.project.meltano_dir() / "analytics.json"
        try:
            with open(analytics_json_path) as analytics_json_file:
                analytics_json = json.load(analytics_json_file)
        except FileNotFoundError:
            client_id = uuid.uuid4()

            if self.send_anonymous_usage_stats:
                # If we are set to track Anonymous Usage stats, also store the generated
                # `client_id` in a non-versioned `analytics.json` file so that it persists between
                # meltano runs.
                with open(analytics_json_path, "w") as analytics_json_file:
                    json.dump({"client_id": str(client_id)}, analytics_json_file)
        else:
            client_id = uuid.UUID(analytics_json["client_id"], version=4)

        return client_id