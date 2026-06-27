from pathlib import Path

from deptracker.adapters.cargo import CargoAdapter


def test_cargo_simple_bump() -> None:
    """Extract a simple Cargo.toml dependency bump."""
    diff_text = _fixture("cargo_simple_bump.diff")
    changes = CargoAdapter().parse_diff("Cargo.toml", diff_text)

    assert len(changes) == 1
    assert changes[0].package == "serde"
    assert changes[0].from_version == "1.0.150"
    assert changes[0].to_version == "1.0.180"


def test_cargo_lock_simple_bump() -> None:
    """Extract a complete Cargo.lock dependency bump."""
    diff_text = _fixture("cargo_lock_bump.diff")
    changes = CargoAdapter().parse_diff("Cargo.lock", diff_text)

    assert len(changes) == 1
    assert changes[0].package == "serde"
    assert changes[0].from_version == "1.0.150"
    assert changes[0].to_version == "1.0.180"
    assert changes[0].is_lockfile is True


def test_cargo_lock_partial_bump_fallback() -> None:
    """Recover a Cargo.lock bump from a partial patch."""
    diff_text = _fixture("cargo_lock_partial_bump.diff")
    changes = CargoAdapter().parse_diff("Cargo.lock", diff_text)

    assert len(changes) == 1
    assert changes[0].package == "serde"
    assert changes[0].from_version == "1.0.150"
    assert changes[0].to_version == "1.0.180"
    assert changes[0].is_lockfile is True


def _fixture(name: str) -> str:
    """Load an adapter diff fixture."""
    return (Path(__file__).parent / "fixtures" / name).read_text()
