"""Maven and first-cut Gradle adapter."""

from __future__ import annotations

import re

from lxml import etree

from deptracker.adapters.base import Adapter, Change
from deptracker.diffutils import reconstruct_before_after_from_unified_diff, safe_version_key


class MavenAdapter(Adapter):
    name = "maven"
    manifest_filenames = frozenset({"pom.xml", "build.gradle", "build.gradle.kts"})
    lockfile_filenames = frozenset()

    def parse_diff(self, file_path: str, diff_text: str) -> list[Change]:
        """Extract Maven or simple Gradle dependency bumps from a patch."""
        if file_path.endswith(("build.gradle", "build.gradle.kts")):
            return _parse_gradle_diff(file_path, diff_text)

        old_text, new_text = reconstruct_before_after_from_unified_diff(diff_text)
        try:
            old_deps = _pom_dependencies(old_text)
            new_deps = _pom_dependencies(new_text)
        except etree.XMLSyntaxError:
            return _fallback_version_line_parse(file_path, diff_text)

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
                        is_lockfile=False,
                        ecosystem=self.name,
                    )
                )
        return changes

    def version_key(self, version: str) -> tuple:
        """Return a sortable Maven version key."""
        return safe_version_key(version)


def _pom_dependencies(text: str) -> dict[str, str]:
    """Map Maven dependency coordinates to resolved versions in a POM."""
    root = etree.fromstring(text.encode())
    properties = _pom_properties(root)
    deps: dict[str, str] = {}
    for dep in root.xpath("//*[local-name()='dependency']"):
        group_id = _first_child_text(dep, "groupId")
        artifact_id = _first_child_text(dep, "artifactId")
        version = _first_child_text(dep, "version")
        if group_id and artifact_id and version:
            deps[f"{group_id}:{artifact_id}"] = _resolve_property_version(version, properties)
    return deps


def _pom_properties(root: etree._Element) -> dict[str, str]:
    """Collect version properties declared in a parsed POM."""
    properties: dict[str, str] = {}
    for properties_element in root.xpath("//*[local-name()='properties']"):
        for child in properties_element:
            if child.text:
                properties[etree.QName(child).localname] = child.text.strip()
    return properties


def _resolve_property_version(version: str, properties: dict[str, str]) -> str:
    """Resolve a simple Maven property reference when its value is known."""
    match = re.fullmatch(r"\$\{([^}]+)}", version.strip())
    if not match:
        return version
    return properties.get(match.group(1), version)


def _first_child_text(element: etree._Element, local_name: str) -> str | None:
    """Return the trimmed text of the first matching XML child."""
    matches = element.xpath(f"./*[local-name()='{local_name}']/text()")
    if not matches:
        return None
    return str(matches[0]).strip()


def _fallback_version_line_parse(file_path: str, diff_text: str) -> list[Change]:
    """Recover simple POM bumps directly from partial XML patch lines."""
    lines = diff_text.splitlines()
    changes: list[Change] = []

    for index, line in enumerate(lines):
        if not line.startswith("-") or line.startswith("---"):
            continue
        old_version = _tag_value(line[1:], "version")
        if not old_version:
            continue

        added_version = _nearest_added_version(lines, index)
        group_id, artifact_id = _nearest_dependency_identity(lines, index)
        if group_id and artifact_id and added_version and added_version != old_version:
            changes.append(
                Change(
                    package=f"{group_id}:{artifact_id}",
                    from_version=old_version,
                    to_version=added_version,
                    manifest_path=file_path,
                    is_lockfile=False,
                    ecosystem=MavenAdapter.name,
                )
            )

    return changes


def _nearest_added_version(lines: list[str], index: int) -> str | None:
    """Find the nearest added Maven version around a removed line."""
    start = max(0, index - 10)
    end = min(len(lines), index + 11)
    for line in lines[index + 1 : end]:
        if line.startswith("+") and not line.startswith("+++"):
            version = _tag_value(line[1:], "version")
            if version:
                return version
    for line in lines[start:index]:
        if line.startswith("+") and not line.startswith("+++"):
            version = _tag_value(line[1:], "version")
            if version:
                return version
    return None


def _nearest_dependency_identity(lines: list[str], index: int) -> tuple[str | None, str | None]:
    """Find nearby Maven group and artifact identifiers around a patch line."""
    start = max(0, index - 10)
    end = min(len(lines), index + 11)
    group_id: str | None = None
    artifact_id: str | None = None
    for line in lines[start:end]:
        content = line[1:] if line[:1] in {" ", "+", "-"} else line
        group_id = group_id or _tag_value(content, "groupId")
        artifact_id = artifact_id or _tag_value(content, "artifactId")
    return group_id, artifact_id


def _tag_value(text: str, tag: str) -> str | None:
    """Extract one inline XML tag value from text."""
    match = re.search(rf"<{tag}>\s*([^<]+?)\s*</{tag}>", text)
    if not match:
        return None
    return match.group(1).strip()


GRADLE_DEP_RE = re.compile(
    r"""^\s*
    [A-Za-z_][\w]*(?:\s+|\s*\(\s*)
    ["'](?P<group>[^:"']+):(?P<artifact>[^:"']+):(?P<version>[^"']+)["']
    \s*\)?""",
    re.VERBOSE,
)


def _parse_gradle_diff(file_path: str, diff_text: str) -> list[Change]:
    """Extract literal group-artifact-version bumps from a Gradle patch."""
    removed: dict[str, str] = {}
    added: dict[str, str] = {}

    for raw_line in diff_text.splitlines():
        if raw_line.startswith(("---", "+++", "@@", "diff --git", "index ")):
            continue
        marker = raw_line[0] if raw_line[:1] in {"-", "+"} else ""
        if marker not in {"-", "+"}:
            continue
        match = GRADLE_DEP_RE.match(raw_line[1:])
        if not match:
            continue
        package = f"{match.group('group')}:{match.group('artifact')}"
        version = match.group("version").strip()
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
                    ecosystem=MavenAdapter.name,
                )
            )
    return changes
