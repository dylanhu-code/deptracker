from pathlib import Path

from deptracker.adapters.pip import PipAdapter


def test_pip_simple_bump() -> None:
    """Extract a pinned requirements dependency bump."""
    diff_text = _fixture("pip_simple_bump.diff")
    changes = PipAdapter().parse_diff("requirements.txt", diff_text)

    assert len(changes) == 1
    assert changes[0].package == "requests"
    assert changes[0].from_version == "2.31.0"
    assert changes[0].to_version == "2.32.0"


def test_pipfile_lock_simple_bump() -> None:
    """Extract a complete Pipfile.lock dependency bump."""
    diff_text = _fixture("pipfile_lock_bump.diff")
    changes = PipAdapter().parse_diff("Pipfile.lock", diff_text)

    assert len(changes) == 1
    assert changes[0].package == "requests"
    assert changes[0].from_version == "2.31.0"
    assert changes[0].to_version == "2.32.0"
    assert changes[0].is_lockfile is True


def test_poetry_lock_simple_bump() -> None:
    """Extract a complete poetry.lock dependency bump."""
    diff_text = _fixture("poetry_lock_bump.diff")
    changes = PipAdapter().parse_diff("poetry.lock", diff_text)

    assert len(changes) == 1
    assert changes[0].package == "requests"
    assert changes[0].from_version == "2.31.0"
    assert changes[0].to_version == "2.32.0"
    assert changes[0].is_lockfile is True


def test_pyproject_partial_toml_fallback_bump() -> None:
    """Recover a pyproject dependency bump from a partial patch."""
    diff_text = """diff --git a/pyproject.toml b/pyproject.toml
--- a/pyproject.toml
+++ b/pyproject.toml
@@ -10,2 +10,2 @@
 dependencies = [
-  "requests>=2.31.0",
+  "requests>=2.32.0",
"""
    changes = PipAdapter().parse_diff("pyproject.toml", diff_text)

    assert len(changes) == 1
    assert changes[0].package == "requests"
    assert changes[0].from_version == "2.31.0"
    assert changes[0].to_version == "2.32.0"
    assert changes[0].is_lockfile is False


def test_pipfile_lock_partial_json_fallback_bump() -> None:
    """Recover a Pipfile.lock bump from a partial patch."""
    diff_text = """diff --git a/Pipfile.lock b/Pipfile.lock
--- a/Pipfile.lock
+++ b/Pipfile.lock
@@ -20,2 +20,2 @@
     "requests": {
-      "version": "==2.31.0",
+      "version": "==2.32.0",
"""
    changes = PipAdapter().parse_diff("Pipfile.lock", diff_text)

    assert len(changes) == 1
    assert changes[0].package == "requests"
    assert changes[0].from_version == "2.31.0"
    assert changes[0].to_version == "2.32.0"
    assert changes[0].is_lockfile is True


def test_poetry_lock_partial_toml_fallback_bump() -> None:
    """Recover a poetry.lock bump from a partial patch."""
    diff_text = """diff --git a/poetry.lock b/poetry.lock
--- a/poetry.lock
+++ b/poetry.lock
@@ -50,4 +50,4 @@
 [[package]]
 name = "requests"
-version = "2.31.0"
+version = "2.32.0"
 files = [
"""
    changes = PipAdapter().parse_diff("poetry.lock", diff_text)

    assert len(changes) == 1
    assert changes[0].package == "requests"
    assert changes[0].from_version == "2.31.0"
    assert changes[0].to_version == "2.32.0"
    assert changes[0].is_lockfile is True


def _fixture(name: str) -> str:
    """Load an adapter diff fixture."""
    return (Path(__file__).parent / "fixtures" / name).read_text()
