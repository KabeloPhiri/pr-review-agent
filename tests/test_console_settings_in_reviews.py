"""Console settings reach reviews: config, standards and prompt guidance.

Runs the real pipeline over the sample-pr fixture with a stub reviewer that
records what it was given, against a `LocalDirStore` in a temp directory.
"""

import pytest

from app.core.pipeline import ReviewPipeline
from app.core.settings_store import LocalDirStore, RepoKey, set_settings_store
from app.services.review import prompts as review_prompts
from app.services.review.base import Reviewer, reviewer_registry

SEEN: dict = {}


@reviewer_registry.register("recording")
class RecordingReviewer(Reviewer):
    name = "recording"

    async def review(self, chunks, context):
        SEEN["config"] = context.config
        SEEN["sources"] = list(context.standards.sources)
        SEEN["repo_text"] = context.standards.repo_text
        SEEN["catalog"] = context.standards.catalog.names()
        return []


@pytest.fixture
def store(tmp_path):
    SEEN.clear()
    s = LocalDirStore(tmp_path)
    set_settings_store(s)
    yield s
    set_settings_store(None)


def _key(ref):
    return RepoKey.from_ref(ref)


async def _review(ref, **kwargs):
    return await ReviewPipeline().run(
        ref, overrides={"reviewer": "recording"}, publish=False, **kwargs
    )


async def test_console_global_setting_beats_the_repos_own_config(ref, store):
    # The fixture repo's .prreview/config.yaml sets severity_gate: warning.
    store.write("config.yaml", "severity_gate: none\n", actor="a@x")
    await _review(ref)
    assert SEEN["config"].severity_gate.value == "none"


async def test_repo_setting_applies_to_that_repository_only(ref, store):
    store.write(f"{_key(ref).settings_dir}/config.yaml", "max_findings_per_file: 3\n", actor="a@x")
    await _review(ref)
    assert SEEN["config"].max_findings_per_file == 3

    other = ref.model_copy(update={"repository": "another-repo"})
    await _review(other)
    assert SEEN["config"].max_findings_per_file != 3


async def test_an_unknown_console_key_is_ignored_not_fatal(ref, store):
    store.write("config.yaml", "no_such_setting: 1\nmax_files: 9\n", actor="a@x")
    await _review(ref)  # a 400 here would take every review down
    assert SEEN["config"].max_files == 9


async def test_pinned_beats_the_console(ref, store):
    store.write("config.yaml", "model_endpoint: console-model\n", actor="a@x")
    await _review(ref, pinned={"model_endpoint": "candidate"})
    assert SEEN["config"].model_endpoint == "candidate"


async def test_console_standards_replace_the_bundled_set(ref, store):
    store.write(
        "standards/house.md",
        "---\napplies_to:\n  languages: [python, sql]\n---\n# House rules\n",
        actor="a@x",
    )
    await _review(ref)
    assert SEEN["catalog"] == ["house"]


async def test_repo_standards_are_added_for_that_repository(ref, store):
    store.write(
        f"{_key(ref).settings_dir}/standards/payments.md", "Never log card numbers.", actor="a@x"
    )
    await _review(ref)
    assert "Never log card numbers." in SEEN["repo_text"]
    assert "admin console (this repository)" in SEEN["sources"]


async def test_store_outage_is_a_warning_on_the_result(ref, store, monkeypatch):
    def broken(path):
        raise OSError("volume unavailable")

    monkeypatch.setattr(store, "_read", broken)
    result = await _review(ref)
    assert any("admin console settings unavailable" in w for w in result.warnings)


def test_prompt_guidance_from_the_console_keeps_the_fixed_contract(store):
    store.write("prompts/review_guidance.md", "Review like a payments auditor.", actor="a@x")
    rules = review_prompts.system_rules()
    assert rules.startswith("Review like a payments auditor.")
    assert rules.endswith(review_prompts.OUTPUT_CONTRACT.strip())
