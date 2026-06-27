"""Go module manifest adapter."""

from __future__ import annotations

from pathlib import Path

from deptracker.adapters.base import Adapter, Change
from deptracker.diffutils import safe_version_key


class GoAdapter(Adapter):
    name = "go"
    manifest_filenames = frozenset({"go.mod", "go.sum"})
    lockfile_filenames = frozenset({"go.sum"})

    def parse_diff(self, file_path: str, diff_text: str) -> list[Change]:
        """Extract module bumps from a go.mod patch."""
        if Path(file_path).name == "go.sum":
            # TODO: go.sum parsing
            return []

        removed, added = _collect_changed_requires(diff_text)
        changes: list[Change] = []
        for module, old_version in removed.items():
            new_version = added.get(module)
            if new_version and new_version != old_version:
                changes.append(
                    Change(
                        package=module,
                        from_version=old_version,
                        to_version=new_version,
                        manifest_path=file_path,
                        is_lockfile=False,
                        ecosystem=self.name,
                    )
                )
        return changes

    def version_key(self, version: str) -> tuple:
        """Return a sortable Go module version key."""
        return safe_version_key(version)


def _collect_changed_requires(diff_text: str) -> tuple[dict[str, str], dict[str, str]]:
    """Collect removed and added require directives from a go.mod patch."""
    removed: dict[str, str] = {}
    added: dict[str, str] = {}
    in_require_block = False

    for raw_line in diff_text.splitlines():
        if raw_line.startswith(("---", "+++", "@@", "diff --git", "index ")):
            continue

        marker = raw_line[0] if raw_line[:1] in {" ", "-", "+"} else " "
        text = raw_line[1:] if raw_line[:1] in {" ", "-", "+"} else raw_line
        stripped = text.strip()

        if stripped == "require (":
            in_require_block = True
            continue
        if in_require_block and stripped == ")":
            in_require_block = False
            continue

        requirement_text: str | None = None
        if stripped.startswith("require "):
            rest = stripped[len("require ") :].strip()
            if rest == "(":
                in_require_block = True
                continue
            requirement_text = rest
        elif in_require_block:
            requirement_text = stripped

        if requirement_text and marker in {"-", "+"}:
            parsed = _parse_module_version(requirement_text)
            if parsed:
                module, version = parsed
                if marker == "-":
                    removed[module] = version
                else:
                    added[module] = version

    return removed, added


def _parse_module_version(text: str) -> tuple[str, str] | None:
    """Parse a module path and version from one require directive."""
    parts = text.split()
    if len(parts) < 2:
        return None
    return parts[0], parts[1]
