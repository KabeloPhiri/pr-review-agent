"""Loading and merging coding standards.

Standards come from two places: markdown bundled with the app and markdown
committed to the reviewed repo. The repo's text wins where they disagree, and
that is stated in the prompt so the model resolves conflicts the way a team
would.

The bundled set is **discovered from disk**, not listed in code: every `*.md`
in `app/defaults/standards/` becomes an analyzer named after the file. Each
file declares which code it applies to in optional YAML front matter, so
adding a standard — or teaching the reviewer a new language — is a markdown
file and nothing else:

    ---
    applies_to:
      languages: [python]              # file languages this covers
      path_globs: ["**/spark/**"]      # optional extra narrowing
      content_match: "pyspark|Spark"   # optional regex over changed lines
    ---
    # PySpark standards
    ...

Omit `applies_to` entirely and the standard applies to every reviewed file —
that is how `naming.md` works. `languages` is the field that matters most:
it is what makes a file type reviewable at all, so a `terraform.md` declaring
`languages: [terraform]` is enough to start reviewing `.tf` files.
"""

from __future__ import annotations

import fnmatch
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.core.models import DiffFile

logger = logging.getLogger(__name__)

STANDARDS_DIR = Path(__file__).resolve().parent.parent.parent / "defaults" / "standards"

_FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass(frozen=True)
class StandardsDoc:
    """One standards fragment plus the rule for when it applies."""

    name: str
    text: str
    #: Empty means "not scoped to a language".
    languages: frozenset[str] = frozenset()
    path_globs: tuple[str, ...] = ()
    content_pattern: re.Pattern[str] | None = None
    #: True when the file declared no `applies_to` at all.
    unscoped: bool = False

    @property
    def narrowed(self) -> bool:
        """Whether this standard targets a subset of its languages.

        Used to order the prompt: general rules first, specific ones last, so
        `pyspark` is read after `python` and wins where the two disagree.
        """
        return bool(self.path_globs) or self.content_pattern is not None

    def applies_to(self, file: DiffFile) -> bool:
        if self.unscoped:
            return True
        if self.languages and (file.language or "") not in self.languages:
            return False
        # `path_globs` and `content_match` narrow further, and are OR-ed with
        # each other: a Spark job is recognised by living under a spark/
        # directory *or* by importing pyspark.
        narrowing = bool(self.path_globs) or self.content_pattern is not None
        if not narrowing:
            return True
        # `fnmatch` folds case via os.path.normcase, which is a no-op on Linux
        # and lowercases on Windows — so matching would differ between a
        # developer's laptop and the deployed app. Fold explicitly instead.
        path = file.path.lower()
        if any(fnmatch.fnmatchcase(path, pattern.lower()) for pattern in self.path_globs):
            return True
        return self.content_pattern is not None and self._matches_content(file)

    def _matches_content(self, file: DiffFile) -> bool:
        assert self.content_pattern is not None
        for hunk in file.hunks:
            for line in hunk.lines:
                if self.content_pattern.search(line):
                    return True
        return False


@dataclass(frozen=True)
class StandardsCatalog:
    """Every bundled standard, keyed by analyzer name."""

    docs: dict[str, StandardsDoc] = field(default_factory=dict)

    def names(self) -> list[str]:
        return sorted(self.docs)

    def reviewable_languages(self) -> set[str]:
        """Languages some standard knows how to critique.

        A file whose language nothing covers is skipped rather than reviewed
        badly, so this is what makes a new language reviewable.
        """
        languages: set[str] = set()
        for doc in self.docs.values():
            languages |= doc.languages
        return languages

    def analyzers_for(self, file: DiffFile, enabled: list[str] | None = None) -> list[str]:
        """Scoped standards that apply to this file, in a stable order.

        Unscoped standards are deliberately excluded: they are appended to
        every prompt by `text_for`, so listing them per file would only make
        the prompt's analyzer line noisier.
        """
        allowed = set(enabled) if enabled else None
        matching = [
            doc
            for name, doc in self.docs.items()
            if not doc.unscoped and (allowed is None or name in allowed) and doc.applies_to(file)
        ]
        matching.sort(key=lambda doc: (doc.narrowed, doc.name))
        return [doc.name for doc in matching]

    def text_for(self, analyzers: list[str], enabled: list[str] | None = None) -> list[str]:
        """The scoped texts for `analyzers`, then every unscoped standard."""
        allowed = set(enabled) if enabled else None
        parts = [self.docs[name].text.strip() for name in analyzers if name in self.docs]
        parts += [
            doc.text.strip()
            for name, doc in sorted(self.docs.items())
            if doc.unscoped and (allowed is None or name in allowed)
        ]
        return [part for part in parts if part]


@dataclass
class StandardsBundle:
    catalog: StandardsCatalog = field(default_factory=StandardsCatalog)
    repo_text: str = ""
    sources: list[str] = field(default_factory=list)
    #: The effective `analyzers` allow-list; empty means "everything found".
    enabled: list[str] = field(default_factory=list)

    def for_analyzers(self, analyzers: list[str]) -> str:
        """Standards text relevant to one file's analyzers."""
        parts = self.catalog.text_for(analyzers, self.enabled)
        if self.repo_text:
            parts.append(
                "# Repository standards (authoritative — these override the sections above)\n\n"
                + self.repo_text.strip()
            )
        return "\n\n---\n\n".join(parts)


def _parse_front_matter(raw: str) -> tuple[dict[str, Any], str]:
    """Split leading YAML front matter from the markdown body.

    A malformed block is logged and ignored rather than raised: a typo in a
    standards file must not take every review down with it.
    """
    match = _FRONT_MATTER_RE.match(raw)
    if not match:
        return {}, raw
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        logger.warning("Ignoring malformed front matter: %s", exc)
        return {}, raw[match.end() :]
    if not isinstance(meta, dict):
        logger.warning("Ignoring front matter that is not a mapping")
        meta = {}
    return meta, raw[match.end() :]


def _build_doc(name: str, raw: str) -> StandardsDoc:
    meta, body = _parse_front_matter(raw)
    applies = meta.get("applies_to")
    if not isinstance(applies, dict):
        # No `applies_to` (or an unusable one): the standard is general, so
        # inject it everywhere rather than silently never using it.
        return StandardsDoc(name=name, text=body, unscoped=True)

    languages = applies.get("languages") or []
    if isinstance(languages, str):
        languages = [languages]
    globs = applies.get("path_globs") or []
    if isinstance(globs, str):
        globs = [globs]

    pattern: re.Pattern[str] | None = None
    expression = applies.get("content_match")
    if expression:
        try:
            pattern = re.compile(str(expression))
        except re.error as exc:
            logger.warning("Ignoring invalid content_match in %s.md: %s", name, exc)

    return StandardsDoc(
        name=name,
        text=body,
        languages=frozenset(str(language).lower() for language in languages),
        path_globs=tuple(str(glob) for glob in globs),
        content_pattern=pattern,
    )


def discover(directory: Path | None = None) -> StandardsCatalog:
    """Read every `*.md` in the standards directory into a catalog.

    Read fresh each time rather than cached, so editing a standard takes
    effect on the next review instead of the next restart. The directory
    holds a handful of small files.
    """
    root = directory or STANDARDS_DIR
    docs: dict[str, StandardsDoc] = {}
    if not root.is_dir():
        logger.warning("No standards directory at %s", root)
        return StandardsCatalog(docs)

    for path in sorted(root.glob("*.md")):
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            logger.warning("Could not read standards file %s", path, exc_info=True)
            continue
        docs[path.stem] = _build_doc(path.stem, raw)
    return StandardsCatalog(docs)


async def load(scm, pr, config) -> StandardsBundle:
    """Read bundled defaults plus any standards files the repo ships.

    A missing repo file is normal (most repos will not have one at first), so
    it is recorded and skipped rather than failing the review.
    """
    bundle = StandardsBundle(catalog=discover(), enabled=list(config.analyzers))
    texts: list[str] = []
    for path in config.standards:
        try:
            content = await scm.get_file_text(pr, path)
        except Exception:
            logger.warning("Could not read standards file %s", path, exc_info=True)
            continue
        if content and content.strip():
            texts.append(f"<!-- from {path} -->\n{content.strip()}")
            bundle.sources.append(path)
    bundle.repo_text = "\n\n".join(texts)
    return bundle
