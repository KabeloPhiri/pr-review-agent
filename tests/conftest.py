from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "sample-pr"


@pytest.fixture
def fixture_dir() -> Path:
    return FIXTURE_DIR


@pytest.fixture
def ref():
    from app.core.models import PullRequestRef

    return PullRequestRef(
        scm="fake",
        organization="contoso",
        project="data",
        repository="platform",
        pull_request_id="42",
        extra={"fixture_dir": str(FIXTURE_DIR)},
    )
