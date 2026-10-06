"""Settings managed from the admin console.

The console lets an admin change configuration, standards and prompt
guidance without a redeploy. This module is the only thing the review path
knows about that: a small `SettingsStore` interface, selected by the
`PRREVIEW_SETTINGS_STORE` env var:

    none    (default) nothing is stored; the app behaves exactly as before
    local   files under PRREVIEW_SETTINGS_DIR (local development, tests)
    volume  files in a Unity Catalog volume, PRREVIEW_SETTINGS_VOLUME

Settings and statistics are both keyed by `RepoKey`, so what the console
shows for a repository and what it configures for that repository line up.

Layout, relative to the store root (the only paths `write` accepts):

    config.yaml                              global config overrides
    console.yaml                             console-only settings (model prices)
    prompts/review_guidance.md               replaces the review prompt guidance
    prompts/apply_guidance.md                replaces the apply prompt guidance
    standards/<name>.md                      replaces the bundled standards set
    repos/<scm>/<repo-key>/config.yaml       one repository's overrides
    repos/<scm>/<repo-key>/standards/<n>.md  extra standards for one repository
    history/<version>.content / .json        every save, for audit and rollback

Reads never fail a review: an unreachable store falls back to the last value
read, then to "no console settings", and `status()` explains why so the
pipeline can add a warning to the result.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import tempfile
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import yaml

from app.core.models import PullRequestRef

logger = logging.getLogger(__name__)

STORE_ENV = "PRREVIEW_SETTINGS_STORE"
DIR_ENV = "PRREVIEW_SETTINGS_DIR"
VOLUME_ENV = "PRREVIEW_SETTINGS_VOLUME"

PROMPT_NAMES = ("review_guidance", "apply_guidance")
CONFIG_FILE = "config.yaml"
CONSOLE_FILE = "console.yaml"
HISTORY_DIR = "history"

_NAME = r"[a-z0-9][a-z0-9_-]*"
_ALLOWED_PATH = re.compile(
    rf"^(?:{CONFIG_FILE}|{CONSOLE_FILE}"
    rf"|prompts/(?:{'|'.join(PROMPT_NAMES)})\.md"
    rf"|standards/{_NAME}\.md"
    rf"|repos/[a-z_]+/[A-Za-z0-9._-]+/(?:{CONFIG_FILE}|standards/{_NAME}\.md))$"
)
_FRONT_MATTER_RE = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.DOTALL)


#: `/` separators in each SCM's repository slug: `owner/repo`, `org/project/repo`.
_SLUG_SPLITS = {"github": 1, "azure_devops": 2}


@dataclass(frozen=True, order=True)
class RepoKey:
    """One repository on one SCM, e.g. `github` + `KabeloPhiri/pr-review-agent`."""

    scm: str
    slug: str

    @classmethod
    def from_ref(cls, ref: PullRequestRef) -> "RepoKey":
        return cls(scm=ref.scm, slug=ref.repo_slug())

    @classmethod
    def parse(cls, value: str) -> "RepoKey":
        """Inverse of `str()`: `github/KabeloPhiri__pr-review-agent`."""
        scm, _, rest = value.partition("/")
        if not scm or not rest or "/" in rest:
            raise ValueError(f"Not a repository key: {value!r}")
        # `path` turns `/` into `__`. Split only as many times as the SCM has
        # path segments, so a repository *name* containing `__` survives:
        # GitHub owners and Azure DevOps organisations cannot contain `_`.
        splits = _SLUG_SPLITS.get(scm, -1)
        return cls(scm=scm, slug="/".join(rest.split("__", splits)))

    @property
    def path(self) -> str:
        """Filesystem-safe form, used for settings paths and as the trace tag."""
        return f"{self.scm}/{self.slug.replace('/', '__')}"

    @property
    def settings_dir(self) -> str:
        return f"repos/{self.path}"

    def __str__(self) -> str:
        return self.path


@dataclass(frozen=True)
class HistoryEntry:
    """One save, deletion or rollback, as recorded in `history/`."""

    version: str
    path: str
    actor: str
    action: str
    at: str


class SettingsStore(Protocol):
    """What the review path reads from the console's settings."""

    #: False for `NullStore`: the console shows settings read-only.
    editable: bool

    def config_overrides(self, repo: RepoKey | None = None) -> dict[str, Any]:
        """Global console overrides, with `repo`'s own overrides merged on top."""
        ...

    def standards_dir(self) -> Path | None:
        """A local directory of standards replacing the bundled set, or None."""
        ...

    def repo_standards(self, repo: RepoKey) -> str:
        """Extra standards text for one repository ('' when none)."""
        ...

    def prompt(self, name: str) -> str | None:
        """Replacement guidance for a prompt (`review_guidance`, `apply_guidance`)."""
        ...

    def console_settings(self) -> dict[str, Any]:
        """Console-only settings such as model prices; not review config."""
        ...

    def repos(self) -> list[RepoKey]:
        """Repositories that have their own settings."""
        ...

    def status(self) -> str | None:
        """Why the last read fell back to cached or default settings, if it did."""
        ...


class NullStore:
    """No console settings: every lookup falls through to today's behaviour."""

    editable = False

    def config_overrides(self, repo: RepoKey | None = None) -> dict[str, Any]:
        return {}

    def standards_dir(self) -> Path | None:
        return None

    def repo_standards(self, repo: RepoKey) -> str:
        return ""

    def prompt(self, name: str) -> str | None:
        return None

    def console_settings(self) -> dict[str, Any]:
        return {}

    def repos(self) -> list[RepoKey]:
        return []

    def status(self) -> str | None:
        return None


class FileStore(ABC):
    """Settings as files under a root, with history. Subclasses supply I/O."""

    editable = True

    def __init__(self, ttl_seconds: float = 30.0) -> None:
        self._ttl = ttl_seconds
        self._cache: dict[str, tuple[float, str | None]] = {}
        self._problem: str | None = None

    # -- primitives (relative paths) ---------------------------------------

    @abstractmethod
    def _read(self, path: str) -> str | None:
        """File content, or None when it does not exist."""

    @abstractmethod
    def _write(self, path: str, text: str) -> None: ...

    @abstractmethod
    def _delete(self, path: str) -> None: ...

    @abstractmethod
    def _list(self, directory: str, *, directories: bool = False) -> list[str]:
        """Names of files (or sub-directories) in `directory`; [] if missing."""

    @property
    @abstractmethod
    def location(self) -> str:
        """Where the settings live, for the console to show."""

    # -- reads (never raise) -------------------------------------------------

    def read_text(self, path: str) -> str | None:
        now = time.monotonic()
        hit = self._cache.get(path)
        if hit and now - hit[0] < self._ttl:
            return hit[1]
        try:
            text = self._read(path)
        except Exception as exc:
            logger.warning("Could not read console setting %s", path, exc_info=True)
            self._problem = f"could not read {path}: {exc}"
            return hit[1] if hit else None
        self._cache[path] = (now, text)
        self._problem = None
        return text

    def _list_quietly(self, directory: str, *, directories: bool = False) -> list[str]:
        try:
            return self._list(directory, directories=directories)
        except Exception as exc:
            logger.warning("Could not list console settings in %s", directory, exc_info=True)
            self._problem = f"could not list {directory}: {exc}"
            return []

    def _yaml(self, path: str) -> dict[str, Any]:
        raw = self.read_text(path)
        if not raw or not raw.strip():
            return {}
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            self._problem = f"{path} is not valid YAML: {exc}"
            return {}
        if not isinstance(data, dict):
            self._problem = f"{path} must contain a mapping"
            return {}
        return data

    def config_overrides(self, repo: RepoKey | None = None) -> dict[str, Any]:
        merged = dict(self._yaml(CONFIG_FILE))
        if repo is not None:
            merged.update(self._yaml(f"{repo.settings_dir}/{CONFIG_FILE}"))
        return merged

    def console_settings(self) -> dict[str, Any]:
        return self._yaml(CONSOLE_FILE)

    def prompt(self, name: str) -> str | None:
        if name not in PROMPT_NAMES:
            return None
        text = self.read_text(f"prompts/{name}.md")
        return text.strip() if text and text.strip() else None

    def standards_names(self, repo: RepoKey | None = None) -> list[str]:
        directory = f"{repo.settings_dir}/standards" if repo else "standards"
        return sorted(n[:-3] for n in self._list_quietly(directory) if n.endswith(".md"))

    def standards_dir(self) -> Path | None:
        """Copy the console's standards to a local directory `discover()` can read.

        The directory is named after a hash of the content, so a change is a
        new directory and a review never sees a half-written set.
        """
        names = self.standards_names()
        if not names:
            return None
        files = {name: self.read_text(f"standards/{name}.md") or "" for name in names}
        digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:16]
        target = Path(tempfile.gettempdir()) / "prreview-standards" / digest
        if not target.is_dir():
            staging = target.with_name(f"{digest}.tmp-{os.getpid()}")
            staging.mkdir(parents=True, exist_ok=True)
            for name, text in files.items():
                (staging / f"{name}.md").write_text(text, encoding="utf-8")
            try:
                staging.rename(target)
            except OSError:  # another worker got there first
                pass
        return target

    def repo_standards(self, repo: RepoKey) -> str:
        parts = []
        for name in self.standards_names(repo):
            text = self.read_text(f"{repo.settings_dir}/standards/{name}.md") or ""
            body = _FRONT_MATTER_RE.sub("", text, count=1).strip()
            if body:
                parts.append(f"<!-- from the admin console: {name} -->\n{body}")
        return "\n\n".join(parts)

    def repos(self) -> list[RepoKey]:
        keys = []
        for scm in self._list_quietly("repos", directories=True):
            for slug in self._list_quietly(f"repos/{scm}", directories=True):
                try:
                    keys.append(RepoKey.parse(f"{scm}/{slug}"))
                except ValueError:
                    continue
        return sorted(keys)

    def status(self) -> str | None:
        return self._problem

    # -- writes (raise on failure: the admin must know) ------------------------

    def write(self, path: str, text: str, *, actor: str, action: str = "save") -> HistoryEntry:
        _check_path(path)
        self._write(path, text)
        return self._record(path, text, actor=actor, action=action)

    def delete(self, path: str, *, actor: str) -> HistoryEntry:
        _check_path(path)
        self._delete(path)
        return self._record(path, "", actor=actor, action="delete")

    def _record(self, path: str, text: str, *, actor: str, action: str) -> HistoryEntry:
        self._cache.pop(path, None)
        stamp = datetime.now(timezone.utc)
        # `~` separates; unlike `+` it survives a URL query unencoded.
        version = f"{stamp.strftime('%Y%m%dT%H%M%S%fZ')}~{path.replace('/', '~')}"
        entry = HistoryEntry(
            version=version,
            path=path,
            actor=actor,
            action=action,
            at=stamp.isoformat(timespec="seconds"),
        )
        self._write(f"{HISTORY_DIR}/{version}.content", text)
        self._write(f"{HISTORY_DIR}/{version}.json", json.dumps(entry.__dict__))
        return entry

    def history(self, path: str | None = None, *, limit: int = 200) -> list[HistoryEntry]:
        """Newest first. Versions sort by name because they start with a timestamp."""
        names = sorted(
            (n for n in self._list(HISTORY_DIR) if n.endswith(".json")), reverse=True
        )
        if path is not None:
            names = [n for n in names if n[:-5].split("~", 1)[-1] == path.replace("/", "~")]
        entries = []
        for name in names[:limit]:
            raw = self._read(f"{HISTORY_DIR}/{name}")
            if raw:
                entries.append(HistoryEntry(**json.loads(raw)))
        return entries

    def read_version(self, version: str) -> str | None:
        if not re.fullmatch(r"[0-9TZ]+~[A-Za-z0-9._~-]+", version):
            raise ValueError(f"Not a history version: {version!r}")
        return self._read(f"{HISTORY_DIR}/{version}.content")

    def raw(self, path: str) -> str | None:
        """Current content of an editable file, read fresh (for the editor)."""
        _check_path(path)
        self._cache.pop(path, None)
        return self._read(path)


class LocalDirStore(FileStore):
    """Settings on the local filesystem — development and tests."""

    def __init__(self, root: Path | str, ttl_seconds: float = 0.0) -> None:
        super().__init__(ttl_seconds)
        self.root = Path(root)

    @property
    def location(self) -> str:
        return str(self.root)

    def _read(self, path: str) -> str | None:
        target = self.root / path
        return target.read_text(encoding="utf-8") if target.is_file() else None

    def _write(self, path: str, text: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def _delete(self, path: str) -> None:
        (self.root / path).unlink(missing_ok=True)

    def _list(self, directory: str, *, directories: bool = False) -> list[str]:
        target = self.root / directory
        if not target.is_dir():
            return []
        return sorted(p.name for p in target.iterdir() if p.is_dir() == directories)


class VolumeStore(FileStore):
    """Settings in a Unity Catalog volume, through the Databricks Files API."""

    def __init__(self, volume_path: str, *, client: Any | None = None, ttl_seconds: float = 30.0):
        super().__init__(ttl_seconds)
        self.volume_path = volume_path.rstrip("/")
        self._client = client

    @property
    def location(self) -> str:
        return self.volume_path

    @property
    def files(self) -> Any:
        if self._client is None:
            from databricks.sdk import WorkspaceClient

            self._client = WorkspaceClient()
        return self._client.files

    def _full(self, path: str) -> str:
        return f"{self.volume_path}/{path}" if path else self.volume_path

    def _read(self, path: str) -> str | None:
        from databricks.sdk.errors import NotFound

        try:
            response = self.files.download(self._full(path))
        except NotFound:
            return None
        return response.contents.read().decode("utf-8")

    def _write(self, path: str, text: str) -> None:
        self.files.upload(self._full(path), io.BytesIO(text.encode("utf-8")), overwrite=True)

    def _delete(self, path: str) -> None:
        from databricks.sdk.errors import NotFound

        try:
            self.files.delete(self._full(path))
        except NotFound:
            pass

    def _list(self, directory: str, *, directories: bool = False) -> list[str]:
        from databricks.sdk.errors import NotFound

        try:
            entries = list(self.files.list_directory_contents(self._full(directory)))
        except NotFound:
            return []
        return sorted(e.name for e in entries if bool(e.is_directory) == directories)


def _check_path(path: str) -> None:
    if ".." in path.split("/") or not _ALLOWED_PATH.fullmatch(path):
        raise ValueError(f"Not an editable settings path: {path!r}")


_store: SettingsStore | None = None


def get_settings_store() -> SettingsStore:
    """The process-wide store, chosen once from `PRREVIEW_SETTINGS_STORE`."""
    global _store
    if _store is None:
        kind = os.getenv(STORE_ENV, "none").strip().lower()
        if kind == "local":
            _store = LocalDirStore(os.getenv(DIR_ENV, ".prreview-console"))
        elif kind == "volume" and os.getenv(VOLUME_ENV):
            _store = VolumeStore(os.environ[VOLUME_ENV])
        else:
            if kind != "none":
                logger.warning("%s=%r needs its location set; using none", STORE_ENV, kind)
            _store = NullStore()
    return _store


def set_settings_store(store: SettingsStore | None) -> None:
    """Swap the store (tests)."""
    global _store
    _store = store
