import math

from deptracker.sq1_analysis import cramers_v


def test_cramers_v_is_one_for_separated_2x2_table() -> None:
    """Return perfect association for a separated contingency table."""
    value = cramers_v([[50, 0], [0, 50]])

    assert math.isclose(value, 1.0)


def test_cramers_v_is_zero_for_independent_2x2_table() -> None:
    """Return zero association for an independent contingency table."""
    value = cramers_v([[25, 25], [25, 25]])

    assert math.isclose(value, 0.0)
