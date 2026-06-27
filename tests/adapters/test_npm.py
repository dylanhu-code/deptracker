from pathlib import Path

from deptracker.adapters.base import adapter_for_file
from deptracker.adapters.go import GoAdapter
from deptracker.adapters.npm import NpmAdapter
from deptracker.adapters.pip import PipAdapter


def test_npm_simple_bump() -> None:
    """Extract a simple package.json dependency bump."""
    diff_text = _fixture("npm_simple_bump.diff")
    changes = NpmAdapter().parse_diff("package.json", diff_text)

    assert len(changes) == 1
    assert changes[0].package == "react"
    assert changes[0].from_version == "18.2.0"
    assert changes[0].to_version == "18.3.0"


def test_npm_partial_package_json_delete_only_fallback() -> None:
    """Ignore delete-only package.json fallback candidates."""
    diff_text = _fixture("npm_partial_delete_only.diff")
    changes = NpmAdapter().parse_diff("frontend/package.json", diff_text)

    assert changes == []


def test_npm_partial_package_json_dependency_bump_fallback() -> None:
    """Recover a package.json bump from a partial patch."""
    diff_text = _fixture("npm_partial_dev_bump.diff")
    changes = NpmAdapter().parse_diff("package.json", diff_text)

    assert len(changes) == 1
    assert changes[0].package == "typescript"
    assert changes[0].from_version == "5.9.3"
    assert changes[0].to_version == "6.0.0"


def test_npm_package_lock_packages_bump() -> None:
    """Extract a modern package-lock packages-section bump."""
    diff_text = _fixture("npm_package_lock_bump.diff")
    changes = NpmAdapter().parse_diff("package-lock.json", diff_text)

    assert len(changes) == 1
    assert changes[0].package == "react"
    assert changes[0].from_version == "18.2.0"
    assert changes[0].to_version == "18.3.0"
    assert changes[0].is_lockfile is True


def test_npm_package_lock_legacy_dependencies_bump() -> None:
    """Extract a legacy package-lock dependencies-section bump."""
    diff_text = _fixture("npm_package_lock_legacy_bump.diff")
    changes = NpmAdapter().parse_diff("package-lock.json", diff_text)

    assert len(changes) == 1
    assert changes[0].package == "react"
    assert changes[0].from_version == "18.2.0"
    assert changes[0].to_version == "18.3.0"
    assert changes[0].is_lockfile is True


def test_npm_package_lock_partial_bump_fallback() -> None:
    """Recover a package-lock bump from a partial patch."""
    diff_text = _fixture("npm_package_lock_partial_bump.diff")
    changes = NpmAdapter().parse_diff("package-lock.json", diff_text)

    assert len(changes) == 1
    assert changes[0].package == "react"
    assert changes[0].from_version == "18.2.0"
    assert changes[0].to_version == "18.3.0"
    assert changes[0].is_lockfile is True


def test_nested_manifest_path_matching() -> None:
    """Recognize adapter manifests nested within repository directories."""
    assert isinstance(adapter_for_file("frontend/package.json"), NpmAdapter)
    assert isinstance(adapter_for_file("services/api/requirements-dev.txt"), PipAdapter)
    assert isinstance(adapter_for_file("backend/go.mod"), GoAdapter)


def _fixture(name: str) -> str:
    """Load an adapter diff fixture."""
    return (Path(__file__).parent / "fixtures" / name).read_text()
