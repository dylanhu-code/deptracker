"""Adapter base classes and manifest-path registry."""

from __future__ import annotations

import fnmatch
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Change:
    package: str
    from_version: str | None
    to_version: str
    manifest_path: str
    is_lockfile: bool
    ecosystem: str


class Adapter(ABC):
    name: str
    manifest_filenames: frozenset[str]
    manifest_patterns: frozenset[str] = frozenset()
    lockfile_filenames: frozenset[str]
    lockfile_patterns: frozenset[str] = frozenset()

    def is_manifest_path(self, path: str) -> bool:
        """Return whether the repository path belongs to this adapter."""
        return _matches_path(path, self.manifest_filenames, self.manifest_patterns)

    def is_lockfile_path(self, path: str) -> bool:
        """Return whether the repository path is one of this adapter's lockfiles."""
        return _matches_path(path, self.lockfile_filenames, self.lockfile_patterns)

    @abstractmethod
    def parse_diff(self, file_path: str, diff_text: str) -> list[Change]:
        """Extract dependency changes from one manifest-file patch."""
        raise NotImplementedError

    @abstractmethod
    def version_key(self, version: str) -> tuple:
        """Return a sortable representation of an ecosystem version."""
        raise NotImplementedError


def _matches_path(path: str, filenames: frozenset[str], patterns: frozenset[str]) -> bool:
    """Match exact basenames and glob patterns against a repository path."""
    normalized = path.replace("\\", "/")
    name = Path(normalized).name
    if name in filenames:
        return True
    return any(fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(normalized, pattern) for pattern in patterns)


def is_manifest_path(path: str) -> bool:
    """Return whether any registered adapter recognizes a manifest path."""
    return adapter_for_file(path) is not None


def is_lockfile_path(path: str) -> bool:
    """Return whether any registered adapter recognizes a lockfile path."""
    return any(adapter().is_lockfile_path(path) for adapter in ADAPTERS.values())


def adapter_for_file(path: str) -> Adapter | None:
    """Return the registered adapter that owns a manifest path, if any."""
    for adapter_cls in ADAPTERS.values():
        adapter = adapter_cls()
        if adapter.is_manifest_path(path):
            return adapter
    return None


from deptracker.adapters.cargo import CargoAdapter  # noqa: E402
from deptracker.adapters.go import GoAdapter  # noqa: E402
from deptracker.adapters.maven import MavenAdapter  # noqa: E402
from deptracker.adapters.npm import NpmAdapter  # noqa: E402
from deptracker.adapters.pip import PipAdapter  # noqa: E402

ADAPTERS: dict[str, type[Adapter]] = {
    MavenAdapter.name: MavenAdapter,
    NpmAdapter.name: NpmAdapter,
    CargoAdapter.name: CargoAdapter,
    PipAdapter.name: PipAdapter,
    GoAdapter.name: GoAdapter,
}
