"""Repository keys, the settings stores, and how console settings layer in."""

import json

import pytest

from app.core.config import resolve, resolve_with_sources
from app.core.models import PullRequestRef
from app.core.settings_store import (
    LocalDirStore,
    NullStore,
    RepoKey,
    get_settings_store,
    set_settings_store,
)


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


# --- editable stores -----------------------------------------------------------

A = RepoKey("github", "acme/payments")
B = RepoKey("github", "acme/ledger")


@pytest.fixture
def store(tmp_path):
    return LocalDirStore(tmp_path)


def test_repo_overrides_merge_over_global_for_that_repo_only(store):
    store.write("config.yaml", "severity_gate: warning\nmax_files: 10\n", actor="a@x")
    store.write(f"{A.settings_dir}/config.yaml", "severity_gate: none\n", actor="a@x")

    assert store.config_overrides(None) == {"severity_gate": "warning", "max_files": 10}
    assert store.config_overrides(A) == {"severity_gate": "none", "max_files": 10}
    assert store.config_overrides(B) == {"severity_gate": "warning", "max_files": 10}
    assert store.repos() == [A]


def test_every_write_and_delete_is_recorded_with_its_author(store):
    store.write("prompts/review_guidance.md", "v1", actor="a@x")
    store.write("prompts/review_guidance.md", "v2", actor="b@x")
    store.delete("prompts/review_guidance.md", actor="a@x")

    history = store.history("prompts/review_guidance.md")
    assert [(e.action, e.actor) for e in history] == [
        ("delete", "a@x"),
        ("save", "b@x"),
        ("save", "a@x"),
    ]
    assert store.read_version(history[2].version) == "v1"
    assert store.prompt("review_guidance") is None
    assert len(store.history()) == 3


@pytest.mark.parametrize(
    "path",
    ["../etc/passwd", "standards/../config.yaml", "notes.txt", "standards/Bad Name.md",
     "repos/github/../config.yaml", "history/x.content", "prompts/other.md"],
)
def test_only_known_settings_paths_can_be_written(store, path):
    with pytest.raises(ValueError):
        store.write(path, "x", actor="a@x")


def test_standards_dir_is_a_new_directory_per_content(store):
    assert store.standards_dir() is None  # nothing in the console: bundled set
    store.write("standards/python.md", "# Python\nv1", actor="a@x")
    first = store.standards_dir()
    assert (first / "python.md").read_text() == "# Python\nv1"
    store.write("standards/python.md", "# Python\nv2", actor="a@x")
    second = store.standards_dir()
    assert second != first and (second / "python.md").read_text().endswith("v2")


def test_repo_standards_drop_front_matter(store):
    store.write(
        f"{A.settings_dir}/standards/payments.md",
        "---\napplies_to:\n  languages: [python]\n---\nNever log card numbers.\n",
        actor="a@x",
    )
    text = store.repo_standards(A)
    assert "Never log card numbers." in text and "applies_to" not in text
    assert store.repo_standards(B) == ""


def test_unreadable_store_keeps_the_last_value_and_says_why(store, monkeypatch):
    store.write("config.yaml", "max_files: 7\n", actor="a@x")
    assert store.config_overrides() == {"max_files": 7}

    def broken(path):
        raise OSError("volume unavailable")

    monkeypatch.setattr(store, "_read", broken)
    assert store.config_overrides() == {"max_files": 7}  # last value read
    assert "volume unavailable" in store.status()
    store._cache.clear()
    assert store.config_overrides() == {}  # nothing cached: no console settings


def test_invalid_yaml_is_ignored_and_reported(store):
    store.write("config.yaml", "max_files: [unclosed\n", actor="a@x")
    assert store.config_overrides() == {}
    assert "not valid YAML" in store.status()


def test_history_metadata_is_json(store):
    entry = store.write("console.yaml", "prices: {}\n", actor="a@x")
    raw = (store.root / "history" / f"{entry.version}.json").read_text()
    assert json.loads(raw)["path"] == "console.yaml"


# --- precedence ------------------------------------------------------------------


def test_console_beats_repo_and_request_and_pinned_beats_console():
    config, sources = resolve_with_sources(
        repo_config={"severity_gate": "warning", "max_files": 5},
        request_overrides={"severity_gate": "none"},
        console_overrides={"severity_gate": "error"},
    )
    assert config.severity_gate.value == "error" and sources["severity_gate"] == "console"
    assert config.max_files == 5 and sources["max_files"] == "repository"

    pinned = resolve(console_overrides={"model_endpoint": "a"}, pinned={"model_endpoint": "b"})
    assert pinned.model_endpoint == "b"
