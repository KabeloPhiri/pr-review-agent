"""Loading and merging coding standards.

Standards come from two places: markdown bundled with the app (per analyzer)
and markdown committed to the reviewed repo. The repo's text wins where they
disagree, and that is stated in the prompt so the model resolves conflicts
the way a team would.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

STANDARDS_DIR = Path(__file__).resolve().parent.parent.parent / "defaults" / "standards"

#: Analyzer name -> bundled markdown file. Naming applies to every analyzer.
_BUNDLED = {
    "python": "python.md",
    "pyspark": "pyspark.md",
    "sql": "sql.md",
    "naming": "naming.md",
}


@dataclass
class StandardsBundle:
    bundled: dict[str, str] = field(default_factory=dict)
    repo_text: str = ""
    sources: list[str] = field(default_factory=list)

    def for_analyzers(self, analyzers: list[str]) -> str:
        """Standards text relevant to one file's analyzers."""
        parts: list[str] = []
        for analyzer in analyzers:
            text = self.bundled.get(analyzer)
            if text:
                parts.append(text.strip())
        naming = self.bundled.get("naming")
        if naming:
            parts.append(naming.strip())
        if self.repo_text:
            parts.append(
                "# Repository standards (authoritative — these override the sections above)\n\n"
                + self.repo_text.strip()
            )
        return "\n\n---\n\n".join(parts)


def load_bundled(analyzers: list[str]) -> dict[str, str]:
    wanted = set(analyzers) | {"naming"}
    out: dict[str, str] = {}
    for name in wanted:
        filename = _BUNDLED.get(name)
        if not filename:
            continue
        path = STANDARDS_DIR / filename
        if path.exists():
            out[name] = path.read_text(encoding="utf-8")
    return out


async def load(scm, pr, config) -> StandardsBundle:
    """Read bundled defaults plus any standards files the repo ships.

    A missing repo file is normal (most repos will not have one at first), so
    it is recorded and skipped rather than failing the review.
    """
    bundle = StandardsBundle(bundled=load_bundled(config.analyzers))
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
