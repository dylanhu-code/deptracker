"""Helpers for working with unified diffs and dependency versions."""

from __future__ import annotations

import re

from packaging.version import InvalidVersion, Version
from unidiff import PatchSet


def strip_diff_path(path: str) -> str:
    """Remove unified-diff source/target prefixes from a path."""
    if path == "/dev/null":
        return path
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def reconstruct_before_after_from_unified_diff(diff_text: str) -> tuple[str, str]:
    """Reconstruct old and new file contents from a one-file unified diff."""
    patch = PatchSet(diff_text.splitlines(keepends=True))
    if not patch:
        return "", ""

    old_lines: list[str] = []
    new_lines: list[str] = []

    for hunk in patch[0]:
        for line in hunk:
            if line.line_type == " ":
                old_lines.append(line.value)
                new_lines.append(line.value)
            elif line.line_type == "-":
                old_lines.append(line.value)
            elif line.line_type == "+":
                new_lines.append(line.value)

    return "".join(old_lines), "".join(new_lines)


def normalise_version(version: str) -> str:
    """Strip common range prefixes and quotes from a dependency version."""
    cleaned = version.strip().strip("\"'")
    prefixes = (">=", "<=", "==", "!=", "~=", "^", "~", ">", "<", "=")
    changed = True
    while changed:
        changed = False
        for prefix in prefixes:
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix) :].strip()
                changed = True
                break
    return cleaned.strip().strip("\"'")


def safe_version_key(version: str) -> tuple:
    """Return a comparable key without assuming the version is PEP 440."""
    cleaned = normalise_version(version)
    try:
        return (0, Version(cleaned))
    except InvalidVersion:
        parts = tuple(
            (0, int(part)) if part.isdigit() else (1, part.lower())
            for part in re.split(r"([0-9]+)", cleaned)
            if part
        )
        return (1, parts, cleaned.lower())
