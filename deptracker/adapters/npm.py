"""npm package manifest adapter."""

from __future__ import annotations

import json
import re
from pathlib import Path

from deptracker.adapters.base import Adapter, Change
from deptracker.diffutils import (
    normalise_version,
    reconstruct_before_after_from_unified_diff,
    safe_version_key,
)


class NpmAdapter(Adapter):
    name = "npm"
    manifest_filenames = frozenset({"package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml"})
    lockfile_filenames = frozenset({"package-lock.json", "yarn.lock", "pnpm-lock.yaml"})

    def parse_diff(self, file_path: str, diff_text: str) -> list[Change]:
        """Extract npm dependency bumps from a manifest or supported lockfile patch."""
        name = Path(file_path).name
        if name == "package-lock.json":
            return _parse_package_lock(file_path, diff_text)
        if name in {"yarn.lock", "pnpm-lock.yaml"}:
            return []

        old_text, new_text = reconstruct_before_after_from_unified_diff(diff_text)
        try:
            old_data = json.loads(old_text)
            new_data = json.loads(new_text)
        except json.JSONDecodeError:
            return _parse_package_json_line_fallback(file_path, diff_text)

        changes: list[Change] = []
        for section in (
            "dependencies",
            "devDependencies",
            "peerDependencies",
            "optionalDependencies",
        ):
            old_deps = old_data.get(section, {})
            new_deps = new_data.get(section, {})
            if not isinstance(old_deps, dict) or not isinstance(new_deps, dict):
                continue

            for package, old_version in old_deps.items():
                new_version = new_deps.get(package)
                if (
                    isinstance(old_version, str)
                    and isinstance(new_version, str)
                    and new_version != old_version
                ):
                    changes.append(
                        Change(
                            package=package,
                            from_version=normalise_version(old_version),
                            to_version=normalise_version(new_version),
                            manifest_path=file_path,
                            is_lockfile=False,
                            ecosystem=self.name,
                        )
                    )

        return changes

    def version_key(self, version: str) -> tuple:
        """Return a sortable npm version key."""
        return safe_version_key(version)


DEPENDENCY_SECTIONS = frozenset(
    {
        "dependencies",
        "devDependencies",
        "peerDependencies",
        "optionalDependencies",
    }
)

PACKAGE_METADATA_KEYS = frozenset(
    {
        "name",
        "version",
        "description",
        "main",
        "types",
        "typings",
        "private",
        "license",
        "author",
        "repository",
        "homepage",
        "bugs",
        "engines",
        "packageManager",
        "scripts",
        "workspaces",
        "files",
        "exports",
        "imports",
        "bin",
    }
)

ENTRY_RE = re.compile(r'^\s*"(?P<key>[^"]+)":\s*"(?P<value>[^"]+)"\s*,?\s*$')
SECTION_RE = re.compile(r'^\s*"(?P<section>[^"]+)":\s*\{\s*$')


def _parse_package_json_line_fallback(file_path: str, diff_text: str) -> list[Change]:
    """Recover package.json bumps directly from partial JSON patch lines."""
    removed: dict[str, str] = {}
    added: dict[str, str] = {}
    active_section: str | None = None

    for raw_line in diff_text.splitlines():
        if raw_line.startswith(("---", "+++", "@@", "diff --git", "index ")):
            continue

        marker = raw_line[0] if raw_line[:1] in {" ", "-", "+"} else " "
        text = raw_line[1:] if raw_line[:1] in {" ", "-", "+"} else raw_line
        stripped = text.strip()

        section_match = SECTION_RE.match(text)
        if section_match:
            section = section_match.group("section")
            active_section = section if section in DEPENDENCY_SECTIONS else None
            continue
        if stripped == "}":
            active_section = None
            continue
        if marker not in {"-", "+"}:
            continue

        entry = ENTRY_RE.match(text)
        if not entry:
            continue
        package = entry.group("key")
        version = entry.group("value")
        if not _looks_like_dependency_entry(package, version, active_section):
            continue
        if marker == "-":
            removed[package] = version
        else:
            added[package] = version

    changes: list[Change] = []
    for package, old_version in removed.items():
        new_version = added.get(package)
        if new_version and new_version != old_version:
            changes.append(
                Change(
                    package=package,
                    from_version=normalise_version(old_version),
                    to_version=normalise_version(new_version),
                    manifest_path=file_path,
                    is_lockfile=False,
                    ecosystem=NpmAdapter.name,
                )
            )
    return changes


def _looks_like_dependency_entry(package: str, version: str, active_section: str | None) -> bool:
    """Conservatively identify dependency-like package.json entries."""
    if active_section in DEPENDENCY_SECTIONS:
        return True
    if package in PACKAGE_METADATA_KEYS:
        return False
    return bool(re.match(r"^\s*(?:[\^~<>=]|v?\d)", version))


def _parse_package_lock(file_path: str, diff_text: str) -> list[Change]:
    """Extract package version bumps from a package-lock.json patch."""
    try:
        old_text, new_text = reconstruct_before_after_from_unified_diff(diff_text)
        old_data = json.loads(old_text)
        new_data = json.loads(new_text)
    except Exception:
        return _parse_package_lock_line_fallback(file_path, diff_text)
    old_versions = _package_lock_versions(old_data)
    new_versions = _package_lock_versions(new_data)
    return [
        Change(
            package=package,
            from_version=old_version,
            to_version=new_version,
            manifest_path=file_path,
            is_lockfile=True,
            ecosystem=NpmAdapter.name,
        )
        for package, old_version in old_versions.items()
        if (new_version := new_versions.get(package)) and new_version != old_version
    ]


def _package_lock_versions(data: dict) -> dict[str, str]:
    """Map package-lock entries to resolved package versions."""
    versions: dict[str, str] = {}
    packages = data.get("packages")
    if isinstance(packages, dict):
        for path, info in packages.items():
            if not path.startswith("node_modules/") or not isinstance(info, dict):
                continue
            version = info.get("version")
            if isinstance(version, str):
                versions[path.removeprefix("node_modules/")] = version

    dependencies = data.get("dependencies")
    if isinstance(dependencies, dict):
        for package, info in dependencies.items():
            if isinstance(info, dict) and isinstance(info.get("version"), str):
                versions.setdefault(package, info["version"])
    return versions


PACKAGE_LOCK_PACKAGE_RE = re.compile(r'^\s*"(?:(?:node_modules/)?(?P<name>[^"]+))":\s*\{\s*$')
PACKAGE_LOCK_VERSION_RE = re.compile(r'^\s*"version":\s*"(?P<version>[^"]+)"\s*,?\s*$')


def _parse_package_lock_line_fallback(file_path: str, diff_text: str) -> list[Change]:
    """Recover package-lock bumps directly from partial JSON patch lines."""
    removed: dict[str, str] = {}
    added: dict[str, str] = {}
    current_package: str | None = None

    for raw_line in diff_text.splitlines():
        if raw_line.startswith(("---", "+++", "@@", "diff --git", "index ")):
            continue
        marker = raw_line[0] if raw_line[:1] in {" ", "-", "+"} else " "
        text = raw_line[1:] if raw_line[:1] in {" ", "-", "+"} else raw_line

        package_match = PACKAGE_LOCK_PACKAGE_RE.match(text)
        if package_match:
            package = package_match.group("name")
            if package and package not in {"", "packages", "dependencies"}:
                current_package = package
            continue

        version_match = PACKAGE_LOCK_VERSION_RE.match(text)
        if not current_package or not version_match or marker not in {"-", "+"}:
            continue
        version = version_match.group("version")
        if marker == "-":
            removed[current_package] = version
        else:
            added[current_package] = version

    return [
        Change(
            package=package,
            from_version=old_version,
            to_version=new_version,
            manifest_path=file_path,
            is_lockfile=True,
            ecosystem=NpmAdapter.name,
        )
        for package, old_version in removed.items()
        if (new_version := added.get(package)) and new_version != old_version
    ]
