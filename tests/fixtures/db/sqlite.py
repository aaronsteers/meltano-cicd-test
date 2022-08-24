import contextlib
import os

import pytest
import sqlalchemy


@pytest.fixture(scope="session")
def engine_uri(test_dir):
    database_path = test_dir.joinpath("pytest_meltano.db")

    with contextlib.suppress(FileNotFoundError):
        database_path.unlink()
    return f"sqlite:///{database_path}"
