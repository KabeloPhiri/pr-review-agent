from app.core.config import resolve
from app.core.models import ChangeType
from app.services.review.chunking import build_chunks, detect_analyzers
from app.services.scm.diffparse import build_unified_diff, parse_unified_diff


def _diff(fixture_dir):
    return parse_unified_diff((fixture_dir / "diff.patch").read_text(encoding="utf-8"))


def test_parses_every_file_in_the_fixture(fixture_dir):
    diff = _diff(fixture_dir)
    paths = [f.path for f in diff.files]
    assert paths == [
        "jobs/sales_etl.py",
        "sql/daily_totals.sql",
        "notebooks/explore.ipynb",
        "docs/architecture.png",
    ]
    assert diff.files[1].change_type is ChangeType.ADD
    assert diff.files[3].is_binary


def test_line_numbers_follow_the_new_file(fixture_dir):
    python_file = _diff(fixture_dir).files[0]
    # The hunk starts at new line 8; two context lines and a blank precede the
    # first added line, so the added block starts at 11.
    assert min(python_file.changed_line_numbers()) == 11
    assert python_file.language == "python"


def test_pyspark_is_detected_from_content(fixture_dir):
    config = resolve()
    files = {f.path: f for f in _diff(fixture_dir).files}
    assert detect_analyzers(files["jobs/sales_etl.py"], config) == ["python", "pyspark"]
    assert detect_analyzers(files["sql/daily_totals.sql"], config) == ["sql"]


def test_build_chunks_skips_what_it_cannot_review(fixture_dir):
    chunks, skipped = build_chunks(_diff(fixture_dir), resolve())
    assert sorted({c.path for c in chunks}) == ["jobs/sales_etl.py", "sql/daily_totals.sql"]

    reasons = " ".join(skipped)
    assert "notebooks/explore.ipynb (excluded by config)" in skipped
    assert "binary" in reasons


def test_rendered_chunk_carries_line_numbers(fixture_dir):
    chunks, _ = build_chunks(_diff(fixture_dir), resolve())
    rendered = next(c for c in chunks if c.path == "jobs/sales_etl.py").render()
    assert "    11 + | def Summarise(spark):" in rendered
    assert "@@ new lines" in rendered


def test_oversized_files_split_into_parts(fixture_dir):
    config = resolve(request_overrides={"max_chunk_chars": 60})
    chunks, _ = build_chunks(_diff(fixture_dir), config)
    python_chunks = [c for c in chunks if c.path == "jobs/sales_etl.py"]
    assert len(python_chunks) > 1
    assert python_chunks[0].total_parts == len(python_chunks)
    # Splitting must not lose or duplicate any changed line.
    combined = set().union(*(c.changed_lines for c in python_chunks))
    whole, _ = build_chunks(_diff(fixture_dir), resolve())
    expected = next(c for c in whole if c.path == "jobs/sales_etl.py").changed_lines
    assert combined == expected


def test_diff_budget_skips_large_files(fixture_dir):
    config = resolve(request_overrides={"max_diff_bytes": 10})
    chunks, skipped = build_chunks(_diff(fixture_dir), config)
    assert chunks == []
    assert any("max_diff_bytes" in item for item in skipped)


def test_build_unified_diff_round_trips():
    patch = build_unified_diff("a\nb\nc\n", "a\nB\nc\n", "x.py")
    parsed = parse_unified_diff(patch)
    assert parsed.files[0].path == "x.py"
    assert parsed.files[0].changed_line_numbers() == {2}
