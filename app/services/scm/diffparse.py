"""Unified-diff parsing and construction.

Every connector funnels into these helpers so hunk handling has exactly one
implementation: providers that hand back a unified diff parse it, and
providers that only expose file contents (Azure DevOps) build one with
`difflib` and then parse it the same way.
"""

from __future__ import annotations

import difflib
import re

from app.core.models import ChangeType, Diff, DiffFile, Hunk

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
_DIFF_GIT_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")

#: Extension -> language tag used to pick analyzers and prompt fragments.
LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".sql": "sql",
    ".scala": "scala",
    ".r": "r",
    ".sh": "shell",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".json": "json",
    ".tf": "terraform",
    ".md": "markdown",
}


def language_for(path: str) -> str | None:
    lowered = path.lower()
    for suffix, language in LANGUAGE_BY_SUFFIX.items():
        if lowered.endswith(suffix):
            return language
    return None


def build_unified_diff(
    old_text: str,
    new_text: str,
    path: str,
    *,
    previous_path: str | None = None,
    context: int = 3,
) -> str:
    """Produce a git-style unified diff for a single file."""
    old_lines = old_text.splitlines(keepends=True) if old_text else []
    new_lines = new_text.splitlines(keepends=True) if new_text else []
    body = "".join(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{previous_path or path}",
            tofile=f"b/{path}",
            n=context,
        )
    )
    if not body:
        return ""
    if not body.endswith("\n"):
        body += "\n"
    return f"diff --git a/{previous_path or path} b/{path}\n{body}"


def parse_unified_diff(text: str) -> Diff:
    """Parse one or more file diffs into the domain model."""
    files: list[DiffFile] = []
    current: DiffFile | None = None
    hunk: Hunk | None = None

    for raw_line in text.splitlines():
        git_header = _DIFF_GIT_RE.match(raw_line)
        if git_header:
            old_path, new_path = git_header.group(1), git_header.group(2)
            current = _new_file(new_path, old_path)
            files.append(current)
            hunk = None
            continue

        if current is None:
            # Tolerate a bare diff with no `diff --git` header.
            if raw_line.startswith("--- "):
                current = _new_file(_strip_prefix(raw_line[4:]), None)
                files.append(current)
            continue

        if raw_line.startswith("new file mode"):
            current.change_type = ChangeType.ADD
            current.previous_path = None
            continue
        if raw_line.startswith("deleted file mode"):
            current.change_type = ChangeType.DELETE
            continue
        if raw_line.startswith("Binary files") or raw_line.startswith("GIT binary patch"):
            current.is_binary = True
            continue
        if raw_line.startswith("--- "):
            if raw_line[4:].strip() == "/dev/null":
                current.change_type = ChangeType.ADD
                current.previous_path = None
            continue
        if raw_line.startswith("+++ "):
            if raw_line[4:].strip() == "/dev/null":
                current.change_type = ChangeType.DELETE
            else:
                path = _strip_prefix(raw_line[4:])
                if path:
                    current.path = path
                    current.language = language_for(path)
            continue

        hunk_header = _HUNK_RE.match(raw_line)
        if hunk_header:
            hunk = Hunk(
                old_start=int(hunk_header.group(1)),
                old_lines=int(hunk_header.group(2) or 1),
                new_start=int(hunk_header.group(3)),
                new_lines=int(hunk_header.group(4) or 1),
                header=hunk_header.group(5).strip(),
            )
            current.hunks.append(hunk)
            continue

        if hunk is not None and raw_line[:1] in {"+", "-", " ", ""}:
            if raw_line.startswith("\\"):  # "\ No newline at end of file"
                continue
            hunk.lines.append(raw_line if raw_line else " ")

    for file in files:
        if file.previous_path == file.path:
            file.previous_path = None
        elif file.previous_path and file.change_type is ChangeType.EDIT:
            file.change_type = ChangeType.RENAME
    return Diff(files=files)


def _new_file(path: str, previous_path: str | None) -> DiffFile:
    return DiffFile(
        path=path,
        previous_path=previous_path,
        change_type=ChangeType.EDIT,
        language=language_for(path),
    )


def _strip_prefix(path: str) -> str:
    path = path.strip().split("\t")[0]
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path
