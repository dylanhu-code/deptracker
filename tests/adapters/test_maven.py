from pathlib import Path

from deptracker.adapters.maven import MavenAdapter


def test_maven_simple_bump() -> None:
    """Extract a literal POM dependency bump."""
    diff_text = _fixture("maven_simple_bump.diff")
    changes = MavenAdapter().parse_diff("pom.xml", diff_text)

    assert len(changes) == 1
    assert changes[0].package == "org.apache.commons:commons-lang3"
    assert changes[0].from_version == "3.13.0"
    assert changes[0].to_version == "3.14.0"


def test_maven_property_version_bump() -> None:
    """Resolve and extract a property-backed POM dependency bump."""
    diff_text = _fixture("maven_property_bump.diff")
    changes = MavenAdapter().parse_diff("pom.xml", diff_text)

    assert len(changes) == 1
    assert changes[0].package == "org.apache.commons:commons-lang3"
    assert changes[0].from_version == "3.13.0"
    assert changes[0].to_version == "3.14.0"


def test_gradle_simple_bump() -> None:
    """Extract a literal Groovy Gradle dependency bump."""
    diff_text = _fixture("gradle_simple_bump.diff")
    changes = MavenAdapter().parse_diff("build.gradle", diff_text)

    assert len(changes) == 1
    assert changes[0].package == "com.google.guava:guava"
    assert changes[0].from_version == "32.1.0-jre"
    assert changes[0].to_version == "32.1.1-jre"


def test_gradle_kts_simple_bump() -> None:
    """Extract a literal Kotlin Gradle dependency bump."""
    diff_text = _fixture("gradle_kts_simple_bump.diff")
    changes = MavenAdapter().parse_diff("build.gradle.kts", diff_text)

    assert len(changes) == 1
    assert changes[0].package == "junit:junit"
    assert changes[0].from_version == "4.13.1"
    assert changes[0].to_version == "4.13.2"


def _fixture(name: str) -> str:
    """Load an adapter diff fixture."""
    return (Path(__file__).parent / "fixtures" / name).read_text()
