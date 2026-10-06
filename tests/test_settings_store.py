"""Repository keys and the default (empty) settings store."""

import pytest

from app.core.models import PullRequestRef
from app.core.settings_store import NullStore, RepoKey, get_settings_store, set_settings_store


@pytest.mark.parametrize(
    "ref, path",
    [
        (
            PullRequestRef(scm="github", repository="KabeloPhiri/pr-review-agent", pull_request_id="1"),
            "github/KabeloPhiri__pr-review-agent",
        ),
        (
            PullRequestRef(
                scm="azure_devops",
                organization="contoso",
                project="data",
                repository="platform",
                pull_request_id="42",
            ),
            "azure_devops/contoso__data__platform",
        ),
    ],
)
def test_repo_key_round_trip(ref, path):
    key = RepoKey.from_ref(ref)
    assert key.path == path
    assert RepoKey.parse(path) == key


def test_repo_slug_drops_only_the_pull_request_number():
    ref = PullRequestRef(scm="github", repository="o/r", pull_request_id="5")
    assert ref.slug() == "o/r#5"
    assert ref.repo_slug() == "o/r"


@pytest.mark.parametrize("bad", ["", "github", "github/"])
def test_repo_key_rejects_malformed_values(bad):
    with pytest.raises(ValueError):
        RepoKey.parse(bad)


def test_default_store_changes_nothing(monkeypatch):
    monkeypatch.delenv("PRREVIEW_SETTINGS_STORE", raising=False)
    set_settings_store(None)
    try:
        store = get_settings_store()
        assert isinstance(store, NullStore)
        key = RepoKey("github", "o/r")
        assert store.config_overrides(key) == {}
        assert store.standards_dir() is None
        assert store.repo_standards(key) == ""
        assert store.prompt("review_guidance") is None
        assert store.console_settings() == {}
        assert store.repos() == []
    finally:
        set_settings_store(None)
