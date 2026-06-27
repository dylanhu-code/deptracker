"""Python packaging manifest adapter."""

from __future__ import annotations

import re
import json
import tomllib
from pathlib import Path
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement

from deptracker.adapters.base import Adapter, Change
from deptracker.diffutils import (
    normalise_version,
    reconstruct_before_after_from_unified_diff,
    safe_version_key,
)


class PipAdapter(Adapter):
    name = "pip"
    manifest_filenames = frozenset({"pyproject.toml", "Pipfile", "Pipfile.lock", "poetry.lock"})
    manifest_patterns = frozenset({"requirements*.txt"})
    lockfile_filenames = frozenset({"Pipfile.lock", "poetry.lock"})
    lockfile_patterns = frozenset()

    def parse_diff(self, file_path: str, diff_text: str) -> list[Change]:
        """Extract Python dependency bumps from supported manifest and lockfile patches."""
        name = Path(file_path).name
        if name == "Pipfile.lock":
            return _parse_pipfile_lock(file_path, diff_text)
        if name == "poetry.lock":
            return _parse_poetry_lock(file_path, diff_text)
        if name == "pyproject.toml":
            return _parse_pyproject(file_path, diff_text)
        if name == "Pipfile":
            return _parse_pipfile(file_path, diff_text)
        if self.is_manifest_path(file_path):
            return _parse_requirements(file_path, diff_text)
        return []

    def version_key(self, version: str) -> tuple:
        """Return a sortable Python package version key."""
        return safe_version_key(version)


def _parse_requirements(file_path: str, diff_text: str) -> list[Change]:
    """Extract pinned dependency bumps from a requirements patch."""
    removed: dict[str, str] = {}
    added: dict[str, str] = {}

    for raw_line in diff_text.splitlines():
        if raw_line.startswith(("---", "+++", "@@")) or not raw_line:
            continue
        marker = raw_line[0]
        if marker not in {"-", "+"}:
            continue
        parsed = _parse_requirement_pin(raw_line[1:])
        if not parsed:
            continue
        package, version = parsed
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
                    from_version=old_version,
                    to_version=new_version,
                    manifest_path=file_path,
                    is_lockfile=False,
                    ecosystem=PipAdapter.name,
                )
            )
    return changes


def _parse_pyproject(file_path: str, diff_text: str) -> list[Change]:
    """Extract dependency bumps from a pyproject.toml patch."""
    old_text, new_text = reconstruct_before_after_from_unified_diff(diff_text)
    try:
        old_data = tomllib.loads(old_text)
        new_data = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError:
        return _parse_toml_manifest_line_fallback(file_path, diff_text)

    old_deps = _pep621_dependencies(old_data) | _poetry_dependencies(old_data)
    new_deps = _pep621_dependencies(new_data) | _poetry_dependencies(new_data)
    return _changes_from_maps(file_path, old_deps, new_deps)


def _parse_pipfile(file_path: str, diff_text: str) -> list[Change]:
    """Extract dependency bumps from a Pipfile patch."""
    old_text, new_text = reconstruct_before_after_from_unified_diff(diff_text)
    try:
        old_data = tomllib.loads(old_text)
        new_data = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError:
        return _parse_toml_manifest_line_fallback(file_path, diff_text)

    old_deps = _pipfile_dependencies(old_data)
    new_deps = _pipfile_dependencies(new_data)
    return _changes_from_maps(file_path, old_deps, new_deps)


def _parse_pipfile_lock(file_path: str, diff_text: str) -> list[Change]:
    """Extract resolved dependency bumps from a Pipfile.lock patch."""
    old_text, new_text = reconstruct_before_after_from_unified_diff(diff_text)
    try:
        old_data = json.loads(old_text)
        new_data = json.loads(new_text)
    except json.JSONDecodeError:
        return _parse_pipfile_lock_line_fallback(file_path, diff_text)
    old_deps = _pipfile_lock_dependencies(old_data)
    new_deps = _pipfile_lock_dependencies(new_data)
    return _changes_from_maps(file_path, old_deps, new_deps, is_lockfile=True)


def _parse_poetry_lock(file_path: str, diff_text: str) -> list[Change]:
    """Extract resolved dependency bumps from a poetry.lock patch."""
    old_text, new_text = reconstruct_before_after_from_unified_diff(diff_text)
    try:
        old_data = tomllib.loads(old_text)
        new_data = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError:
        return _parse_poetry_lock_line_fallback(file_path, diff_text)
    old_deps = _poetry_lock_dependencies(old_data)
    new_deps = _poetry_lock_dependencies(new_data)
    return _changes_from_maps(file_path, old_deps, new_deps, is_lockfile=True)


def _pep621_dependencies(data: dict[str, Any]) -> dict[str, str]:
    """Map PEP 621 project dependencies to normalized versions."""
    dependencies = data.get("project", {}).get("dependencies", [])
    result: dict[str, str] = {}
    if not isinstance(dependencies, list):
        return result

    for dependency in dependencies:
        if not isinstance(dependency, str):
            continue
        parsed = _parse_requirement_version(dependency)
        if parsed:
            package, version = parsed
            result[package] = version
    return result


def _poetry_dependencies(data: dict[str, Any]) -> dict[str, str]:
    """Map Poetry manifest dependencies to normalized versions."""
    dependencies = data.get("tool", {}).get("poetry", {}).get("dependencies", {})
    if not isinstance(dependencies, dict):
        return {}
    return _dependency_table_versions(dependencies)


def _pipfile_dependencies(data: dict[str, Any]) -> dict[str, str]:
    """Map Pipfile runtime and development dependencies to versions."""
    result: dict[str, str] = {}
    for section in ("packages", "dev-packages"):
        dependencies = data.get(section, {})
        if isinstance(dependencies, dict):
            result.update(_dependency_table_versions(dependencies))
    return result


def _pipfile_lock_dependencies(data: dict[str, Any]) -> dict[str, str]:
    """Map Pipfile.lock package entries to normalized versions."""
    result: dict[str, str] = {}
    for section in ("default", "develop"):
        dependencies = data.get(section, {})
        if not isinstance(dependencies, dict):
            continue
        for package, value in dependencies.items():
            if isinstance(value, dict) and isinstance(value.get("version"), str):
                result[package] = normalise_version(value["version"])
    return result


def _poetry_lock_dependencies(data: dict[str, Any]) -> dict[str, str]:
    """Map poetry.lock package entries to normalized versions."""
    packages = data.get("package", [])
    if not isinstance(packages, list):
        return {}
    return {
        package["name"]: normalise_version(package["version"])
        for package in packages
        if isinstance(package, dict)
        and isinstance(package.get("name"), str)
        and isinstance(package.get("version"), str)
    }


def _dependency_table_versions(dependencies: dict[str, Any]) -> dict[str, str]:
    """Normalize dependency versions stored in a TOML table."""
    result: dict[str, str] = {}
    for package, value in dependencies.items():
        version = _table_value_version(value)
        if version:
            result[package] = version
    return result


def _table_value_version(value: Any) -> str | None:
    """Read a version from a TOML string or inline table value."""
    if isinstance(value, str):
        return normalise_version(value)
    if isinstance(value, dict) and isinstance(value.get("version"), str):
        return normalise_version(value["version"])
    return None


def _changes_from_maps(
    file_path: str,
    old_deps: dict[str, str],
    new_deps: dict[str, str],
    is_lockfile: bool = False,
) -> list[Change]:
    """Create change rows for packages whose mapped versions differ."""
    changes: list[Change] = []
    for package, old_version in old_deps.items():
        new_version = new_deps.get(package)
        if new_version and new_version != old_version:
            changes.append(
                Change(
                    package=package,
                    from_version=old_version,
                    to_version=new_version,
                    manifest_path=file_path,
                    is_lockfile=is_lockfile,
                    ecosystem=PipAdapter.name,
                )
            )
    return changes


def _parse_toml_manifest_line_fallback(file_path: str, diff_text: str) -> list[Change]:
    """Extract simple pyproject/Pipfile version bumps directly from patch lines."""
    removed: dict[str, str] = {}
    added: dict[str, str] = {}
    for raw_line in diff_text.splitlines():
        if raw_line.startswith(("---", "+++", "@@", "diff --git", "index ")):
            continue
        marker = raw_line[0] if raw_line[:1] in {"-", "+"} else ""
        if marker not in {"-", "+"}:
            continue
        parsed = _parse_toml_dependency_line(raw_line[1:])
        if not parsed:
            continue
        package, version = parsed
        if marker == "-":
            removed[package] = version
        else:
            added[package] = version
    return _changes_from_maps(file_path, removed, added)


def _parse_pipfile_lock_line_fallback(file_path: str, diff_text: str) -> list[Change]:
    """Extract Pipfile.lock version bumps when full JSON reconstruction is partial."""
    return _parse_named_version_line_fallback(
        file_path=file_path,
        diff_text=diff_text,
        package_regex=re.compile(r'^\s*"(?P<package>[^"]+)":\s*\{'),
        version_regex=re.compile(r'^\s*"version":\s*"(?P<version>[^"]+)"'),
        is_lockfile=True,
    )


def _parse_poetry_lock_line_fallback(file_path: str, diff_text: str) -> list[Change]:
    """Extract poetry.lock version bumps when full TOML reconstruction is partial."""
    return _parse_named_version_line_fallback(
        file_path=file_path,
        diff_text=diff_text,
        package_regex=re.compile(r'^\s*name\s*=\s*["\'](?P<package>[^"\']+)["\']'),
        version_regex=re.compile(r'^\s*version\s*=\s*["\'](?P<version>[^"\']+)["\']'),
        is_lockfile=True,
    )


def _parse_named_version_line_fallback(
    file_path: str,
    diff_text: str,
    package_regex: re.Pattern[str],
    version_regex: re.Pattern[str],
    is_lockfile: bool,
) -> list[Change]:
    """Extract named package version changes from lockfile patch hunks."""
    removed: dict[str, str] = {}
    added: dict[str, str] = {}
    current_package: str | None = None
    for raw_line in diff_text.splitlines():
        if raw_line.startswith(("---", "+++", "@@", "diff --git", "index ")):
            continue
        marker = raw_line[0] if raw_line[:1] in {"-", "+", " "} else ""
        content = raw_line[1:] if marker else raw_line
        package_match = package_regex.match(content)
        if package_match:
            current_package = package_match.group("package")
            continue
        version_match = version_regex.match(content)
        if marker in {"-", "+"} and current_package and version_match:
            version = normalise_version(version_match.group("version"))
            if marker == "-":
                removed[current_package] = version
            else:
                added[current_package] = version
    return _changes_from_maps(file_path, removed, added, is_lockfile=is_lockfile)


def _parse_toml_dependency_line(line: str) -> tuple[str, str] | None:
    """Parse one simple TOML or quoted requirement dependency line."""
    cleaned = line.strip().rstrip(",")
    if not cleaned:
        return None
    if cleaned[:1] in {"'", '"'} and cleaned[-1:] == cleaned[:1]:
        return _parse_requirement_version(cleaned[1:-1])

    table_match = re.match(
        r"""^\s*["']?(?P<package>[A-Za-z0-9_.-]+)["']?\s*=\s*
        (?:
          ["'](?P<string_version>[^"']+)["']
          |\{[^}]*version\s*=\s*["'](?P<table_version>[^"']+)["'][^}]*\}
        )""",
        cleaned,
        re.VERBOSE,
    )
    if not table_match:
        return None
    version = table_match.group("string_version") or table_match.group("table_version")
    if not version:
        return None
    return table_match.group("package"), normalise_version(version)


def _parse_requirement_pin(line: str) -> tuple[str, str] | None:
    """Parse an exact Python requirement pin from one line."""
    cleaned = _clean_requirement_line(line)
    if not cleaned:
        return None
    try:
        requirement = Requirement(cleaned)
    except InvalidRequirement:
        match = re.match(r"^\s*([A-Za-z0-9_.-]+)\s*==\s*([^\s;#]+)", cleaned)
        if not match:
            return None
        return match.group(1), normalise_version(match.group(2))

    for specifier in requirement.specifier:
        if specifier.operator == "==":
            return requirement.name, normalise_version(specifier.version)
    return None


def _parse_requirement_version(line: str) -> tuple[str, str] | None:
    """Parse a Python requirement and normalize its version specifier."""
    try:
        requirement = Requirement(line)
    except InvalidRequirement:
        return None
    specifier = str(requirement.specifier)
    if not specifier:
        return None
    return requirement.name, normalise_version(specifier)


def _clean_requirement_line(line: str) -> str:
    """Remove unsupported and trailing-comment content from a requirement line."""
    cleaned = line.strip()
    if not cleaned or cleaned.startswith(("#", "-", "--")):
        return ""
    return cleaned.split(" #", 1)[0].strip()
