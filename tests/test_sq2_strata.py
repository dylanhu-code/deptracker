import math

import numpy as np

from deptracker.sq2_strata import summarize_strata


def test_sq2_strata_means_by_ecosystem_and_semver() -> None:
    """Compute expected SQ2 means across ecosystem and semver strata."""
    rows = []
    for index in range(50):
        ecosystem = ["npm", "cargo", "pip"][index % 3]
        semver = "major" if index < 20 else "patch"
        alignment = 1.0 if ecosystem == "npm" else 0.5
        if semver == "patch":
            alignment -= 0.1
        rows.append(
            {
                "ecosystem": ecosystem,
                "semver_tier": semver,
                "alignment": alignment,
            }
        )

    rng = np.random.default_rng(42)
    by_ecosystem = summarize_strata(rows, ["ecosystem"], bootstrap_resamples=10, rng=rng)
    by_semver = summarize_strata(rows, ["semver_tier"], bootstrap_resamples=10, rng=rng)

    ecosystem_means = {row["values"]["ecosystem"]: row["mean"] for row in by_ecosystem}
    semver_means = {row["values"]["semver_tier"]: row["mean"] for row in by_semver}
    assert math.isclose(
        ecosystem_means["npm"],
        sum(r["alignment"] for r in rows if r["ecosystem"] == "npm") / 17,
    )
    assert math.isclose(
        ecosystem_means["cargo"],
        sum(r["alignment"] for r in rows if r["ecosystem"] == "cargo") / 17,
    )
    assert math.isclose(
        ecosystem_means["pip"],
        sum(r["alignment"] for r in rows if r["ecosystem"] == "pip") / 16,
    )
    assert math.isclose(
        semver_means["major"],
        sum(r["alignment"] for r in rows if r["semver_tier"] == "major") / 20,
    )
    assert math.isclose(
        semver_means["patch"],
        sum(r["alignment"] for r in rows if r["semver_tier"] == "patch") / 30,
    )
