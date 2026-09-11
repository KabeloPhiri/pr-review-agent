"""Standards discovery: the contract is "a markdown file is enough".

Adding a standard — or teaching the reviewer a new language — must not
require a code change, so these drive `discover()` against a temp directory
rather than the bundled files.
"""

import pytest

from app.core.config import resolve
from app.core.models import Diff, DiffFile, Hunk
from app.services.policy.standards import StandardsBundle, discover
from app.services.review.chunking import build_chunks, detect_analyzers


def _write(directory, name, text):
    (directory / name).write_text(text, encoding="utf-8")


def _file(path, language, lines=("+x = 1",)):
    return DiffFile(
        path=path,
        language=language,
        hunks=[Hunk(new_start=1, new_lines=len(lines), lines=list(lines))],
    )


@pytest.fixture
def catalog_dir(tmp_path):
    _write(
        tmp_path, "python.md", "---\napplies_to:\n  languages: [python]\n---\n# Python\n- rule\n"
    )
    _write(
        tmp_path,
        "pyspark.md",
        "---\napplies_to:\n  languages: [python]\n  content_match: 'pyspark|SparkSession'\n"
        "---\n# PySpark\n- spark rule\n",
    )
    _write(tmp_path, "naming.md", "# Naming\n- names matter\n")
    return tmp_path


def test_every_markdown_file_becomes_an_analyzer(catalog_dir):
    catalog = discover(catalog_dir)
    assert catalog.names() == ["naming", "pyspark", "python"]


def test_a_new_file_is_all_it_takes_to_review_a_new_language(catalog_dir):
    """The headline claim: drop in a markdown file, get a new language."""
    before = discover(catalog_dir)
    assert "terraform" not in before.reviewable_languages()

    _write(catalog_dir, "terraform.md", "---\napplies_to:\n  languages: [terraform]\n---\n# TF\n")
    after = discover(catalog_dir)

    assert "terraform" in after.reviewable_languages()
    tf = _file("infra/main.tf", "terraform")
    assert after.analyzers_for(tf) == ["terraform"]

    # And it survives the whole chunking path, not just the catalog.
    chunks, skipped = build_chunks(Diff(files=[tf]), resolve(), after)
    assert [c.analyzers for c in chunks] == [["terraform"]]
    assert skipped == []


def test_a_language_no_standard_claims_is_skipped(catalog_dir):
    catalog = discover(catalog_dir)
    chunks, skipped = build_chunks(Diff(files=[_file("main.tf", "terraform")]), resolve(), catalog)
    assert chunks == []
    assert "unsupported type: terraform" in skipped[0]


def test_content_match_narrows_within_a_language(catalog_dir):
    catalog = discover(catalog_dir)
    plain = _file("jobs/plain.py", "python", ["+import os"])
    spark = _file("jobs/etl.py", "python", ["+from pyspark.sql import functions"])

    assert catalog.analyzers_for(plain) == ["python"]
    # General first, narrowed last, so the model reads the specific rules last.
    assert catalog.analyzers_for(spark) == ["python", "pyspark"]


def test_path_globs_are_matched_case_insensitively(tmp_path):
    _write(
        tmp_path,
        "spark.md",
        "---\napplies_to:\n  languages: [python]\n  path_globs: ['*spark*']\n---\n# S\n",
    )
    catalog = discover(tmp_path)
    assert catalog.analyzers_for(_file("jobs/Spark_Job.py", "python")) == ["spark"]


def test_a_standard_without_front_matter_applies_everywhere(catalog_dir):
    """Unscoped standards are injected into every prompt, like naming.md."""
    catalog = discover(catalog_dir)
    assert catalog.docs["naming"].unscoped is True
    # It is not listed per file...
    assert "naming" not in catalog.analyzers_for(_file("a.py", "python"))
    # ...but its text is always present.
    assert "names matter" in StandardsBundle(catalog=catalog).for_analyzers(["python"])


def test_malformed_front_matter_is_ignored_not_raised(tmp_path):
    """A typo in a standard must not take every review down with it."""
    _write(tmp_path, "broken.md", "---\napplies_to: [this is: not a mapping\n---\n# Broken\n")
    catalog = discover(tmp_path)
    assert "broken" in catalog.names()
    assert catalog.docs["broken"].unscoped is True


def test_invalid_content_match_regex_is_ignored(tmp_path):
    _write(
        tmp_path,
        "bad.md",
        "---\napplies_to:\n  languages: [python]\n  content_match: '([unclosed'\n---\n# Bad\n",
    )
    doc = discover(tmp_path).docs["bad"]
    assert doc.content_pattern is None
    # With the regex dropped the language scope still stands.
    assert doc.applies_to(_file("a.py", "python")) is True


def test_analyzers_config_still_restricts_what_is_used(catalog_dir):
    catalog = discover(catalog_dir)
    spark = _file("jobs/etl.py", "python", ["+from pyspark.sql import functions"])

    assert catalog.analyzers_for(spark, ["python"]) == ["python"]
    assert detect_analyzers(spark, resolve(repo_config={"analyzers": ["python"]}), catalog) == [
        "python"
    ]


def test_repo_standards_stay_authoritative_and_last(catalog_dir):
    bundle = StandardsBundle(
        catalog=discover(catalog_dir), repo_text="# Ours\n- our rule", sources=[".prreview/s.md"]
    )
    text = bundle.for_analyzers(["python"])
    assert "authoritative" in text
    assert text.index("our rule") > text.index("names matter")


def test_front_matter_is_stripped_from_the_prompt_text(catalog_dir):
    """The YAML is routing metadata; the model should never see it."""
    text = StandardsBundle(catalog=discover(catalog_dir)).for_analyzers(["python"])
    assert "applies_to" not in text
    assert "# Python" in text
