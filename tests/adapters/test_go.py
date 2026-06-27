from pathlib import Path

from deptracker.adapters.go import GoAdapter


def test_go_simple_bump() -> None:
    """Extract a simple go.mod dependency bump."""
    diff_text = _fixture("go_simple_bump.diff")
    changes = GoAdapter().parse_diff("go.mod", diff_text)

    assert len(changes) == 1
    assert changes[0].package == "github.com/stretchr/testify"
    assert changes[0].from_version == "v1.8.4"
    assert changes[0].to_version == "v1.9.0"


def _fixture(name: str) -> str:
    """Load an adapter diff fixture."""
    return (Path(__file__).parent / "fixtures" / name).read_text()
