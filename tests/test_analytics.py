"""Tests for screening.analytics: campus distribution and before/after shortlist comparison."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from screening import analytics
from screening import schema as S
from screening.config import AppConfig

SHORT, REVIEW, HOLD, NOT, INELIGIBLE = (S.STATUS_SHORTLISTED, S.STATUS_REVIEW, S.STATUS_HOLD, S.STATUS_NOT,
                                        S.STATUS_INELIGIBLE)


def cap_cfg(pct: float) -> AppConfig:
    cfg = AppConfig()
    cfg.shortlist.college_cap_pct = pct
    return cfg


def campus_frame() -> pd.DataFrame:
    """8 applicants: College A (4), College B (2) and two without a recorded college."""
    rows = [
        # id, college, city, status, eligible, hr_pct, final, risk, capped
        ("A1", "College A", "Pune", SHORT, True, 100.0, 90.0, "High", False),
        ("A2", "College A", "Pune", REVIEW, True, 100.0, 85.0, "Medium", False),
        ("A3", "College A", "Pune", NOT, True, 80.0, 60.0, "Clear", True),
        ("A4", "College A", "Chennai", INELIGIBLE, False, np.nan, 40.0, "Low", False),
        ("B1", "College B", "Chennai", SHORT, True, 90.0, 80.0, "Clear", False),
        ("B2", "College B", "Chennai", NOT, True, 70.0, 55.0, None, False),
        ("U1", None, "Pune", SHORT, True, 95.0, 75.0, "Clear", False),
        ("U2", "N/A", None, NOT, True, 60.0, 50.0, "Clear", False),
    ]
    cols = [S.CANDIDATE_ID, S.COLLEGE, S.CITY, S.STATUS, S.ELIGIBLE, S.HR_PCT, S.FINAL_SCORE, S.RISK_LEVEL,
            S.COLLEGE_CAPPED]
    return pd.DataFrame(rows, columns=cols)


# ---------------------------------------------------------------------------
# campus_distribution
# ---------------------------------------------------------------------------
def test_campus_distribution_counts_shares_and_order():
    df = campus_frame()
    before = df.copy()
    dist = analytics.campus_distribution(df, S.COLLEGE)
    assert list(dist.columns) == analytics.DISTRIBUTION_COLUMNS
    # shortlisted desc, applicants desc, then group name ("(unknown)" sorts before letters)
    assert dist["group"].tolist() == ["College A", analytics.UNKNOWN_GROUP, "College B"]
    a = dist.set_index("group").loc["College A"]
    assert (a["applicants"], a["eligible"], a["perfect_scorers"]) == (4, 3, 2)
    assert (a["shortlisted"], a["in_review"], a["capped_out"], a["flagged"]) == (2, 1, 1, 2)
    assert a["shortlist_rate"] == pytest.approx(2 / 3)
    assert a["share_of_shortlist"] == pytest.approx(0.5)      # 2 of 4 shortlisted overall
    assert a["share_of_applicants"] == pytest.approx(0.5)     # 4 of 8 applicants
    assert a["representation"] == pytest.approx(1.0)
    assert a["avg_final_score"] == pytest.approx((90 + 85 + 60 + 40) / 4)
    assert a["avg_hackerrank_pct"] == pytest.approx((100 + 100 + 80) / 3)   # NaN ignored
    b = dist.set_index("group").loc["College B"]
    assert b["representation"] == pytest.approx(0.25 / 0.25)
    assert dist["share_of_shortlist"].sum() == pytest.approx(1.0)
    assert dist["share_of_applicants"].sum() == pytest.approx(1.0)
    assert not dist["over_cap"].any()                       # no config -> no cap
    pd.testing.assert_frame_equal(df, before)               # input untouched


def test_unknown_values_and_spelling_variants_are_grouped():
    df = pd.DataFrame({
        S.CANDIDATE_ID: [f"C{i}" for i in range(8)],
        S.COLLEGE: ["Alpha College", "  alpha   college ", "Alpha College", "ALPHA COLLEGE", None, "", "-", "unknown"],
        S.STATUS: [SHORT] * 8,
    })
    dist = analytics.campus_distribution(df, S.COLLEGE)
    assert dist.set_index("group")["applicants"].to_dict() == {"Alpha College": 4, analytics.UNKNOWN_GROUP: 4}
    labels = analytics.group_labels(df, S.COLLEGE)
    assert labels.tolist() == ["Alpha College"] * 4 + [analytics.UNKNOWN_GROUP] * 4


def test_over_cap_only_for_college_groups_strictly_above_the_cap():
    df = campus_frame()
    dist = analytics.campus_distribution(df, S.COLLEGE, cap_cfg(30.0)).set_index("group")
    assert dist["over_cap"].to_dict() == {"College A": True, analytics.UNKNOWN_GROUP: False, "College B": False}
    at_cap = analytics.campus_distribution(df, S.COLLEGE, cap_cfg(50.0)).set_index("group")
    assert not at_cap["over_cap"].any()                     # exactly 50% is not above a 50% cap
    assert not analytics.campus_distribution(df, S.COLLEGE, cap_cfg(0.0))["over_cap"].any()
    assert not analytics.campus_distribution(df, S.CITY, cap_cfg(10.0))["over_cap"].any()   # cap is per college
    # the unknown group is never over the cap, however large its share
    unknown_heavy = df.assign(**{S.COLLEGE: [None] * 6 + ["College B", "College B"]})
    heavy = analytics.campus_distribution(unknown_heavy, S.COLLEGE, cap_cfg(10.0)).set_index("group")
    assert heavy.loc[analytics.UNKNOWN_GROUP, "share_of_shortlist"] == pytest.approx(0.75)
    assert not heavy.loc[analytics.UNKNOWN_GROUP, "over_cap"]
    assert heavy.loc["College B", "over_cap"]                # 25% of the shortlist > 10% cap


def test_group_by_other_attributes():
    dist = analytics.campus_distribution(campus_frame(), S.CITY).set_index("group")
    assert dist["applicants"].to_dict() == {"Pune": 4, "Chennai": 3, analytics.UNKNOWN_GROUP: 1}
    assert dist.loc["Pune", "shortlisted"] == 3
    tiers = pd.DataFrame({S.CANDIDATE_ID: ["T1", "T2", "T3"], S.COLLEGE_TIER: ["1", "2", np.nan],
                          S.STATUS: [SHORT, NOT, SHORT]})
    assert analytics.campus_distribution(tiers, S.COLLEGE_TIER)["group"].tolist() == [
        analytics.UNKNOWN_GROUP, "1", "2"]                  # ties on counts -> group name order


def test_nobody_shortlisted_gives_zero_shares_and_no_representation():
    df = campus_frame().assign(**{S.STATUS: NOT})
    dist = analytics.campus_distribution(df, S.COLLEGE, cap_cfg(10.0))
    assert (dist["share_of_shortlist"] == 0).all()
    assert dist["representation"].isna().all()
    assert not dist["over_cap"].any()


def test_distribution_tolerates_missing_columns_and_empty_input():
    minimal = pd.DataFrame({S.CANDIDATE_ID: ["X", "Y", "Z"], S.STATUS: [SHORT, INELIGIBLE, NOT]})
    dist = analytics.campus_distribution(minimal, S.COLLEGE)
    assert dist["group"].tolist() == [analytics.UNKNOWN_GROUP]
    row = dist.iloc[0]
    assert (row["applicants"], row["eligible"], row["shortlisted"], row["perfect_scorers"]) == (3, 2, 1, 0)
    assert np.isnan(row["avg_final_score"])
    for empty in (pd.DataFrame(), None):
        out = analytics.campus_distribution(empty, S.COLLEGE)
        assert out.empty and list(out.columns) == analytics.DISTRIBUTION_COLUMNS


# ---------------------------------------------------------------------------
# shortlist_diff
# ---------------------------------------------------------------------------
def outcome(rows: list[tuple]) -> pd.DataFrame:
    cols = [S.CANDIDATE_ID, S.RANK, S.STATUS, S.FINAL_SCORE, S.STATUS_REASON, S.COLLEGE_CAPPED]
    df = pd.DataFrame(rows, columns=cols)
    return df.assign(**{S.NAME: "Candidate " + df[S.CANDIDATE_ID], S.COLLEGE: "College A",
                        S.RANK: df[S.RANK].astype("Int64")})


BEFORE = outcome([
    ("P", 1, SHORT, 90.0, "Rank 1 of 6 (top 3)", False),
    ("Q", 2, REVIEW, 85.0, "Integrity review: Medium risk", False),
    ("R", 3, SHORT, 80.0, "Rank 3 of 6 (top 3)", False),
    ("S", 4, NOT, 75.0, "Rank 4 of 6 (cutoff: top 3)", False),
    ("T", 5, NOT, 70.0, "Rank 5 of 6 (cutoff: top 3)", False),
    ("U", None, INELIGIBLE, 20.0, "No HackerRank score", False),
])
AFTER = outcome([
    ("T", 1, SHORT, 95.0, "Rank 1 of 6 (top 3)", False),
    ("P", 2, SHORT, 88.0, "Rank 2 of 6 (top 3)", False),
    ("Q", 3, SHORT, 84.0, "Rank 3 of 6 (top 3)", False),
    ("R", 4, NOT, 79.0, "College cap reached: College A already has 3 shortlisted (cap 50%)", True),
    ("S", 5, NOT, 74.0, "Rank 5 of 6 (cutoff: top 3)", False),
    ("U", None, INELIGIBLE, 20.0, "No HackerRank score", False),
])


def test_shortlist_diff_entering_leaving_changes_and_moves():
    before, after = BEFORE.copy(), AFTER.copy()
    diff = analytics.shortlist_diff(before, after)
    pd.testing.assert_frame_equal(before, BEFORE)            # inputs untouched
    for key in ("entering", "leaving", "status_changes", "rank_moves"):
        assert list(diff[key].columns) == analytics.DIFF_COLUMNS
    entering, leaving = diff["entering"], diff["leaving"]
    assert entering[S.CANDIDATE_ID].tolist() == ["T"]
    assert entering.iloc[0][[S.NAME, "rank_before", "rank_after", "rank_change"]].tolist() == ["Candidate T", 5, 1, 4]
    assert leaving[S.CANDIDATE_ID].tolist() == ["R"]
    assert leaving.iloc[0]["reason_after"].startswith("College cap reached")
    changes = diff["status_changes"].set_index(S.CANDIDATE_ID)
    assert sorted(changes.index) == ["Q", "R", "T"]          # Q: Integrity Review -> Shortlisted
    assert (changes.loc["Q", "status_before"], changes.loc["Q", "status_after"]) == (REVIEW, SHORT)
    assert diff["rank_moves"][S.CANDIDATE_ID].tolist() == ["T", "P", "Q", "R", "S"]   # |4|, then |1| by new rank
    assert diff["rank_moves"]["rank_change"].tolist() == [4, -1, -1, -1, -1]
    assert diff["summary"] == {
        "shortlisted_before": 3, "shortlisted_after": 3, "entering": 1, "leaving": 1,
        "cutoff_before": 80.0, "cutoff_after": 84.0, "review_before": 1, "review_after": 0,
        "status_changes": 3, "capped_before": 0, "capped_after": 1,
    }


def test_shortlist_diff_of_identical_outcomes_is_empty():
    diff = analytics.shortlist_diff(BEFORE, BEFORE.copy())
    assert all(diff[k].empty for k in ("entering", "leaving", "status_changes", "rank_moves"))
    s = diff["summary"]
    assert (s["shortlisted_before"], s["shortlisted_after"], s["cutoff_before"], s["cutoff_after"]) == (3, 3, 80.0, 80.0)


def test_shortlist_diff_handles_new_missing_and_partial_data():
    after = pd.concat([AFTER, outcome([("V", 6, SHORT, 60.0, "Rank 6 of 7", False)])], ignore_index=True)
    before = BEFORE[BEFORE[S.CANDIDATE_ID] != "P"]           # P only exists after
    diff = analytics.shortlist_diff(before, after)
    assert diff["entering"][S.CANDIDATE_ID].tolist() == ["T", "P", "V"]
    assert diff["entering"].set_index(S.CANDIDATE_ID).loc["P", "status_before"] == ""
    # no candidate ids -> no row-level diff, but the summary still counts statuses
    no_ids = analytics.shortlist_diff(BEFORE.drop(columns=[S.CANDIDATE_ID]), AFTER.drop(columns=[S.CANDIDATE_ID]))
    assert no_ids["entering"].empty and no_ids["summary"]["shortlisted_after"] == 3
    empty = analytics.shortlist_diff(None, pd.DataFrame())
    assert empty["summary"]["cutoff_before"] is None and empty["summary"]["shortlisted_after"] == 0
    assert all(empty[k].empty for k in ("entering", "leaving", "status_changes", "rank_moves"))
