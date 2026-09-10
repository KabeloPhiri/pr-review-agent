"""Turn a diff into reviewable chunks.

Two jobs: decide what is worth sending to the model, and render it so the
model can cite real file line numbers. Inline comments are only as good as
the line numbers in the findings, so every line the model sees is prefixed
with its number in the new file.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field

from app.core.config import EffectiveConfig
from app.core.models import ChangeType, Diff, DiffFile, Hunk

#: Languages this reviewer knows how to critique. Anything else is skipped
#: rather than reviewed badly.
REVIEWABLE_LANGUAGES = {"python", "sql"}

_PYSPARK_HINTS = re.compile(
    r"\b(pyspark|SparkSession|spark\.(read|sql|table|createDataFrame)|DataFrame|"
    r"withColumn|groupBy|dbutils|delta)\b"
)


@dataclass
class ReviewChunk:
    """One model request: part of one file, with its analyzers resolved."""

    path: str
    language: str
    analyzers: list[str]
    hunks: list[Hunk]
    changed_lines: set[int] = field(default_factory=set)
    part: int = 1
    total_parts: int = 1

    def render(self) -> str:
        """Annotated diff text: `<line> | <+|-| > <code>`."""
        out: list[str] = []
        for hunk in self.hunks:
            header = f" {hunk.header}" if hunk.header else ""
            out.append(f"@@ new lines {hunk.new_start}..{hunk.new_start + hunk.new_lines - 1} @@{header}")
            line_no = hunk.new_start
            for raw in hunk.lines:
                marker, text = raw[:1] or " ", raw[1:]
                if marker == "-":
                    out.append(f"     - | {text}")
                elif marker == "+":
                    out.append(f"{line_no:>6} + | {text}")
                    line_no += 1
                else:
                    out.append(f"{line_no:>6}   | {text}")
                    line_no += 1
        return "\n".join(out)

    def size(self) -> int:
        return sum(h.size() for h in self.hunks)


def detect_analyzers(file: DiffFile, config: EffectiveConfig) -> list[str]:
    """Which standards fragments apply to this file."""
    enabled = set(config.analyzers)
    analyzers: list[str] = []
    if file.language == "python":
        if "python" in enabled:
            analyzers.append("python")
        if "pyspark" in enabled and _looks_like_spark(file):
            analyzers.append("pyspark")
    elif file.language == "sql":
        if "sql" in enabled:
            analyzers.append("sql")
    return analyzers


def _looks_like_spark(file: DiffFile) -> bool:
    if "spark" in file.path.lower():
        return True
    for hunk in file.hunks:
        for line in hunk.lines:
            if _PYSPARK_HINTS.search(line):
                return True
    return False


def is_excluded(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def build_chunks(diff: Diff, config: EffectiveConfig) -> tuple[list[ReviewChunk], list[str]]:
    """Return (chunks, skipped file descriptions)."""
    chunks: list[ReviewChunk] = []
    skipped: list[str] = []

    considered = 0
    budget = config.max_diff_bytes
    for file in diff.files:
        reason = _skip_reason(file, config)
        if reason:
            skipped.append(f"{file.path} ({reason})")
            continue
        considered += 1
        if considered > config.max_files:
            skipped.append(f"{file.path} (over max_files={config.max_files})")
            continue
        analyzers = detect_analyzers(file, config)
        if not analyzers:
            skipped.append(f"{file.path} (no analyzer enabled for {file.language})")
            continue
        size = file.size()
        if size > budget:
            skipped.append(f"{file.path} (over the max_diff_bytes budget)")
            continue
        budget -= size
        chunks.extend(_split_file(file, analyzers, config.max_chunk_chars))

    return chunks, skipped


def _skip_reason(file: DiffFile, config: EffectiveConfig) -> str | None:
    if file.is_binary:
        return "binary"
    if file.change_type is ChangeType.DELETE:
        return "deleted"
    if is_excluded(file.path, config.exclude_paths):
        return "excluded by config"
    if file.language not in REVIEWABLE_LANGUAGES:
        return f"unsupported type: {file.language or 'unknown'}"
    if not file.hunks:
        return "no textual changes"
    return None


def _split_file(file: DiffFile, analyzers: list[str], max_chars: int) -> list[ReviewChunk]:
    """Group hunks into chunks under `max_chars`, splitting giant hunks."""
    groups: list[list[Hunk]] = []
    current: list[Hunk] = []
    current_size = 0

    for hunk in file.hunks:
        pieces = [hunk] if hunk.size() <= max_chars else _split_hunk(hunk, max_chars)
        for piece in pieces:
            size = piece.size()
            if current and current_size + size > max_chars:
                groups.append(current)
                current, current_size = [], 0
            current.append(piece)
            current_size += size
    if current:
        groups.append(current)

    total = len(groups) or 1
    return [
        ReviewChunk(
            path=file.path,
            language=file.language or "text",
            analyzers=analyzers,
            hunks=group,
            changed_lines={n for h in group for n in h.changed_line_numbers()},
            part=index + 1,
            total_parts=total,
        )
        for index, group in enumerate(groups)
    ]


def _split_hunk(hunk: Hunk, max_chars: int) -> list[Hunk]:
    """Break one oversized hunk on line boundaries, keeping line numbering."""
    pieces: list[Hunk] = []
    lines: list[str] = []
    size = 0
    new_start = hunk.new_start
    old_start = hunk.old_start
    consumed_new = consumed_old = 0

    def flush() -> None:
        nonlocal lines, size, new_start, old_start, consumed_new, consumed_old
        if not lines:
            return
        pieces.append(
            Hunk(
                old_start=old_start,
                old_lines=consumed_old,
                new_start=new_start,
                new_lines=consumed_new,
                header=hunk.header,
                lines=lines,
            )
        )
        new_start += consumed_new
        old_start += consumed_old
        lines, size, consumed_new, consumed_old = [], 0, 0, 0

    for raw in hunk.lines:
        if size + len(raw) > max_chars and lines:
            flush()
        lines.append(raw)
        size += len(raw)
        marker = raw[:1]
        if marker == "+":
            consumed_new += 1
        elif marker == "-":
            consumed_old += 1
        else:
            consumed_new += 1
            consumed_old += 1
    flush()
    return pieces or [hunk]
