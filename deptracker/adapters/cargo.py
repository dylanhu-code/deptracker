"""Cargo manifest adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import re
import tomllib

from deptracker.adapters.base import Adapter, Change
from deptracker.diffutils import (
    normalise_version,
    reconstruct_before_after_from_unified_diff,
    safe_version_key,
)


class CargoAdapter(Adapter):
    name = "cargo"
    manifest_filenames = frozenset({"Cargo.toml", "Cargo.lock"})
    lockfile_filenames = frozenset({"Cargo.lock"})

    def parse_diff(self, file_path: str, diff_text: str) -> list[Change]:
        """Extract Cargo dependency bumps from a manifest or lockfile patch."""
        if Path(file_path).name == "Cargo.lock":
            return _parse_cargo_lock(file_path, diff_text)

        old_text, new_text = reconstruct_before_after_from_unified_diff(diff_text)
        old_data = tomllib.loads(old_text)
        new_data = tomllib.loads(new_text)
        changes: list[Change] = []

        for section in ("dependencies", "dev-dependencies", "build-dependencies"):
            old_deps = old_data.get(section, {})
            new_deps = new_data.get(section, {})
            if not isinstance(old_deps, dict) or not isinstance(new_deps, dict):
                continue

            for package, old_value in old_deps.items():
                old_version = _dependency_version(old_value)
                new_version = _dependency_version(new_deps.get(package))
                if old_version and new_version and new_version != old_version:
                    changes.append(
                        Change(
                            package=package,
                            from_version=old_version,
                            to_version=new_version,
                            manifest_path=file_path,
                            is_lockfile=False,
                            ecosystem=self.name,
                        )
                    )

        return changes

    def version_key(self, version: str) -> tuple:
        """Return a sortable Cargo version key."""
        return safe_version_key(version)


def _dependency_version(value: Any) -> str | None:
    """Read a Cargo dependency version from string or inline-table syntax."""
    if isinstance(value, str):
        return normalise_version(value)
    if isinstance(value, dict) and isinstance(value.get("version"), str):
        return normalise_version(value["version"])
    return None


def _parse_cargo_lock(file_path: str, diff_text: str) -> list[Change]:
    """Extract changed package versions from a Cargo.lock patch."""
    old_text, new_text = reconstruct_before_after_from_unified_diff(diff_text)
    try:
        old_versions = _cargo_lock_versions(tomllib.loads(old_text))
        new_versions = _cargo_lock_versions(tomllib.loads(new_text))
    except tomllib.TOMLDecodeError:
        return _parse_cargo_lock_line_fallback(file_path, diff_text)
    if not old_versions and not new_versions:
        return _parse_cargo_lock_line_fallback(file_path, diff_text)
    return [
        Change(
            package=package,
            from_version=old_version,
            to_version=new_version,
            manifest_path=file_path,
            is_lockfile=True,
            ecosystem=CargoAdapter.name,
        )
        for package, old_version in old_versions.items()
        if (new_version := new_versions.get(package)) and new_version != old_version
    ]


def _cargo_lock_versions(data: dict[str, Any]) -> dict[str, str]:
    """Map Cargo.lock package names to their parsed versions."""
    packages = data.get("package", [])
    if not isinstance(packages, list):
        return {}
    return {
        package["name"]: package["version"]
        for package in packages
        if isinstance(package, dict)
        and isinstance(package.get("name"), str)
        and isinstance(package.get("version"), str)
    }


LOCK_NAME_RE = re.compile(r'^\s*name\s*=\s*"(?P<name>[^"]+)"\s*$')
LOCK_VERSION_RE = re.compile(r'^\s*version\s*=\s*"(?P<version>[^"]+)"\s*$')


def _parse_cargo_lock_line_fallback(file_path: str, diff_text: str) -> list[Change]:
    """Extract Cargo.lock bumps directly from partial patch lines."""
    removed: dict[str, str] = {}
    added: dict[str, str] = {}
    current_package: str | None = None

    for raw_line in diff_text.splitlines():
        if raw_line.startswith(("---", "+++", "@@", "diff --git", "index ")):
            continue
        marker = raw_line[0] if raw_line[:1] in {" ", "-", "+"} else " "
        text = raw_line[1:] if raw_line[:1] in {" ", "-", "+"} else raw_line
        name_match = LOCK_NAME_RE.match(text)
        if name_match:
            current_package = name_match.group("name")
            continue
        version_match = LOCK_VERSION_RE.match(text)
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
            ecosystem=CargoAdapter.name,
        )
        for package, old_version in removed.items()
        if (new_version := added.get(package)) and new_version != old_version
    ]
