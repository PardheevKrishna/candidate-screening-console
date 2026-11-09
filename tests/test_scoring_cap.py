"""College cap in scoring.apply_shortlist (ShortlistConfig.college_cap_pct)."""
from __future__ import annotations

import types

import numpy as np
import pandas as pd
import pytest

from screening import schema as S
from screening import scoring
from screening.config import AppConfig

SHORT, REVIEW, HOLD, NOT = S.STATUS_SHORTLISTED, S.STATUS_REVIEW, S.STATUS_HOLD, S.STATUS_NOT


def make_cfg(**shortlist) -> AppConfig:
    cfg = AppConfig()
    for key, value in shortlist.items():
        setattr(cfg.shortlist, key, value)
    return cfg


def make_scored(colleges: list, **extra_cols) -> pd.DataFrame:
    """Candidates C00, C01, ... ranked in list order (scores 100, 99, ...)."""
    n = len(colleges)
    df = pd.DataFrame({
        S.CANDIDATE_ID: [f"C{i:02d}" for i in range(n)],
        S.FINAL_SCORE: [100.0 - i for i in range(n)],
        S.HR_PCT: 90.0, S.SUB_SKILLS: 1.0, S.SUB_EXPERIENCE: 0.5, S.HAS_RESUME: True,
        S.COLLEGE: colleges,
    })
    for col, values in extra_cols.items():
        df[col] = values
    return df


def by_id(out: pd.DataFrame) -> pd.DataFrame:
    return out.set_index(S.CANDIDATE_ID)


def shortlisted(out: pd.DataFrame) -> list[str]:
    return out.loc[out[S.STATUS].isin(scoring.IN_SHORTLIST), S.CANDIDATE_ID].tolist()


def capped(out: pd.DataFrame) -> list[str]:
    return out.loc[out[S.COLLEGE_CAPPED], S.CANDIDATE_ID].tolist()


def test_top_n_cap_backfills_with_the_next_best_candidates():
    colleges = ["A", "A", "A", "B", "A", "C", "B", "A", "C", "D"]
    out = scoring.apply_shortlist(make_scored(colleges), make_cfg(top_n=4, college_cap_pct=50.0))
    # cap = max(1, floor(50% x 4)) = 2 places per college
    assert shortlisted(out) == ["C00", "C01", "C03", "C05"]
    assert capped(out) == ["C02", "C04"]
    assert out[S.COLLEGE_CAPPED].dtype == bool and len(out[S.COLLEGE_CAPPED]) == 10
    r = by_id(out)
    assert (r.loc["C02", S.STATUS], r.loc["C04", S.STATUS]) == (NOT, NOT)
    assert r.loc["C02", S.STATUS_REASON] == "College cap reached: A already has 2 shortlisted (cap 50%)"
    assert r.loc["C03", S.STATUS_REASON] == "Rank 4 of 10 (top 4)"
    assert r.loc["C05", S.STATUS_REASON] == "Rank 6 of 10 (top 4; backfill for 2 college-capped candidates)"
    assert r.loc["C06", S.STATUS_REASON] == "Rank 7 of 10 (cutoff: top 4)"
    assert not r.loc["C07", S.COLLEGE_CAPPED]                 # beyond the filled shortlist: not capped
    assert r[S.RANK].tolist() == list(range(1, 11))           # ranks are unaffected by the cap


def test_top_pct_cap_keeps_the_shortlist_size():
    colleges = ["X"] * 4 + ["Y"] * 6 + [f"Z{i}" for i in range(10)]
    out = scoring.apply_shortlist(make_scored(colleges), make_cfg(method="top_pct", top_pct=25.0, college_cap_pct=40.0))
    # 20 eligible -> 5 slots; cap = floor(40% x 5) = 2 places
    assert shortlisted(out) == ["C00", "C01", "C04", "C05", "C10"]
    assert capped(out) == ["C02", "C03", "C06", "C07", "C08", "C09"]
    assert by_id(out).loc["C10", S.STATUS_REASON] == (
        "Rank 11 of 20 (top 25% = 5; backfill for 6 college-capped candidates)")


def test_threshold_cap_excludes_without_backfill():
    colleges = ["A", "A", "A", "B", "A", "B"]
    df = make_scored(colleges).assign(**{S.FINAL_SCORE: [95.0, 90.0, 85.0, 80.0, 75.0, 65.0]})
    out = scoring.apply_shortlist(df, make_cfg(method="threshold", threshold=70.0, college_cap_pct=40.0))
    # 5 candidates at or above 70 -> cap = floor(40% x 5) = 2 places
    assert shortlisted(out) == ["C00", "C01", "C03"]
    assert capped(out) == ["C02", "C04"]
    r = by_id(out)
    assert r.loc["C05", S.STATUS] == NOT and not r.loc["C05", S.COLLEGE_CAPPED]
    assert r.loc["C05", S.STATUS_REASON] == "Rank 6 of 6; score 65.00 below threshold 70"


def test_review_counts_toward_the_cap_and_holds_do_not():
    colleges = ["A", "A", "A", "A", "B", "C", "D"]
    levels = ["High", "Medium", "Clear", "Clear", "Clear", "Clear", "Clear"]
    reasons = ["Identical answers to q2 with C09", "Resume text similarity 0.82 with C08", "", "", "", "", ""]
    df = make_scored(colleges, **{S.RISK_LEVEL: levels, S.RISK_REASONS: reasons, S.RISK_SCORE: 70.0})
    out = scoring.apply_shortlist(df, make_cfg(top_n=4, college_cap_pct=50.0, exclude_high_risk=True))
    status = by_id(out)[S.STATUS].to_dict()
    assert status == {"C00": HOLD, "C01": REVIEW, "C02": SHORT, "C03": NOT, "C04": SHORT, "C05": SHORT,
                      "C06": NOT}
    assert capped(out) == ["C03"]     # A's 2 places: the review + one shortlisted; the hold took none
    r = by_id(out)
    assert r.loc["C00", S.STATUS_REASON].endswith("slot backfilled")
    assert r.loc["C05", S.STATUS_REASON] == (
        "Rank 6 of 7 (top 4; backfill for 1 integrity hold and 1 college-capped candidate)")
    assert len(shortlisted(out)) == 4


def test_unknown_college_is_never_capped():
    colleges = ["A", "A", None, "", "N/A", "unknown", "B"]
    out = scoring.apply_shortlist(make_scored(colleges), make_cfg(top_n=5, college_cap_pct=20.0))
    # cap = 1 place; the four candidates without a college fill the rest of the shortlist
    assert shortlisted(out) == ["C00", "C02", "C03", "C04", "C05"]
    assert capped(out) == ["C01"]


def test_spelling_variants_share_one_cap():
    colleges = ["Alpha College", " alpha   college", "ALPHA COLLEGE", "Beta", "Gamma"]
    out = scoring.apply_shortlist(make_scored(colleges), make_cfg(top_n=3, college_cap_pct=34.0))
    assert shortlisted(out) == ["C00", "C03", "C04"]
    assert capped(out) == ["C01", "C02"]
    assert by_id(out).loc["C01", S.STATUS_REASON].startswith("College cap reached: alpha college already has 1")


def test_cap_is_at_least_one_place_and_handles_float_rounding():
    tiny = scoring.apply_shortlist(make_scored(["A", "A", "B", "B", "C"]), make_cfg(top_n=3, college_cap_pct=10.0))
    assert shortlisted(tiny) == ["C00", "C02", "C04"]         # floor(0.3) -> still one place each
    # 29% of 100 slots must be 29 places (29 * 100 / 100), not 28 from float error
    big = make_scored(["A"] * 60 + [f"U{i}" for i in range(80)])
    out = scoring.apply_shortlist(big, make_cfg(top_n=100, college_cap_pct=29.0))
    assert int((out[S.STATUS].isin(scoring.IN_SHORTLIST) & (out[S.COLLEGE] == "A")).sum()) == 29
    assert len(shortlisted(out)) == 100


def test_too_few_candidates_to_backfill_leaves_a_shorter_shortlist():
    out = scoring.apply_shortlist(make_scored(["A", "A", "A", "B"]), make_cfg(top_n=4, college_cap_pct=25.0))
    assert shortlisted(out) == ["C00", "C03"] and capped(out) == ["C01", "C02"]
    # the backfill note counts only capped candidates ranked above the last shortlisted one
    out = scoring.apply_shortlist(make_scored(["A", "A", "B", "A", "C", "A", "B"]),
                                  make_cfg(top_n=4, college_cap_pct=25.0))
    assert shortlisted(out) == ["C00", "C02", "C04"]
    assert capped(out) == ["C01", "C03", "C05", "C06"]
    assert by_id(out).loc["C04", S.STATUS_REASON] == "Rank 5 of 7 (top 4; backfill for 2 college-capped candidates)"


def test_ineligible_candidates_are_not_capped_or_counted():
    df = make_scored(["A", "A", "A", "B"]).assign(**{S.HR_PCT: [np.nan, 90.0, 90.0, 90.0]})
    out = scoring.apply_shortlist(df, make_cfg(top_n=2, college_cap_pct=50.0))
    r = by_id(out)
    assert r.loc["C00", S.STATUS] == S.STATUS_INELIGIBLE and not r.loc["C00", S.COLLEGE_CAPPED]
    assert shortlisted(out) == ["C01", "C03"] and capped(out) == ["C02"]


RISKY_COLLEGES = [
    {S.CANDIDATE_ID: "A", S.FINAL_SCORE: 95.0, S.RISK_LEVEL: "High", S.RISK_SCORE: 85.0, S.COLLEGE: "X",
     S.RISK_REASONS: "Identical answers to q2, q3 with CAND-00123 | Same IP 49.36.12.7 with CAND-00123"},
    {S.CANDIDATE_ID: "B", S.FINAL_SCORE: 90.0, S.RISK_LEVEL: "Clear", S.COLLEGE: "X"},
    {S.CANDIDATE_ID: "C", S.FINAL_SCORE: 85.0, S.RISK_LEVEL: "Medium", S.RISK_SCORE: 35.0, S.COLLEGE: "X",
     S.RISK_REASONS: "Resume text similarity 0.82 with CAND-00999"},
    {S.CANDIDATE_ID: "D", S.FINAL_SCORE: 80.0, S.RISK_LEVEL: None, S.COLLEGE: "X"},
    {S.CANDIDATE_ID: "E", S.FINAL_SCORE: 75.0, S.RISK_LEVEL: "High", S.RISK_REASONS: "Same phone as CAND-1",
     S.COLLEGE: "Y"},
    {S.CANDIDATE_ID: "F", S.FINAL_SCORE: 70.0, S.RISK_LEVEL: "Low", S.COLLEGE: None},
]


@pytest.mark.parametrize("settings, expected", [
    (dict(top_n=3), {"A": REVIEW, "B": SHORT, "C": REVIEW, "D": NOT, "E": NOT, "F": NOT}),
    (dict(top_n=3, exclude_high_risk=True), {"A": HOLD, "B": SHORT, "C": REVIEW, "D": SHORT, "E": NOT, "F": NOT}),
    (dict(method="threshold", threshold=80.0, exclude_high_risk=True),
     {"A": HOLD, "B": SHORT, "C": REVIEW, "D": SHORT, "E": NOT, "F": NOT}),
])
def test_cap_zero_is_identical_to_no_cap(settings, expected):
    df = pd.DataFrame([{S.HR_PCT: 90.0, S.SUB_SKILLS: 1.0, S.SUB_EXPERIENCE: 0.5, **r} for r in RISKY_COLLEGES])
    off = scoring.apply_shortlist(df, make_cfg(college_cap_pct=0.0, **settings))
    assert by_id(off)[S.STATUS].to_dict() == expected          # the documented pre-cap outcomes
    assert not off[S.COLLEGE_CAPPED].any() and off[S.COLLEGE_CAPPED].dtype == bool
    # a cap that can never bind (100%) and data without colleges both reproduce cap 0 exactly
    never = scoring.apply_shortlist(df, make_cfg(college_cap_pct=100.0, **settings))
    pd.testing.assert_frame_equal(never, off)
    no_college = scoring.apply_shortlist(df.drop(columns=[S.COLLEGE]), make_cfg(college_cap_pct=30.0, **settings))
    pd.testing.assert_frame_equal(no_college, off.drop(columns=[S.COLLEGE]))


def test_cap_zero_matches_non_binding_cap_on_random_frames():
    rng = np.random.default_rng(11)
    for _ in range(150):
        n = int(rng.integers(1, 40))
        df = make_scored(list(rng.choice(np.array(["A", "B", "C", None, "N/A"], dtype=object), n)),
                         **{S.RISK_LEVEL: rng.choice(np.array(["High", "Medium", "Low", "Clear", None], dtype=object), n),
                            S.HR_PCT: rng.choice([np.nan, 50.0, 90.0, 100.0], n)})
        df[S.FINAL_SCORE] = rng.choice([60.0, 70.0, 80.0, 90.0], n)
        settings = dict(method=str(rng.choice(["top_n", "top_pct", "threshold"])), top_n=int(rng.integers(0, 30)),
                        top_pct=float(rng.choice([10.0, 50.0])), threshold=75.0,
                        exclude_high_risk=bool(rng.integers(0, 2)), min_hackerrank_pct=float(rng.choice([0.0, 60.0])))
        off = scoring.apply_shortlist(df, make_cfg(college_cap_pct=0.0, **settings))
        never = scoring.apply_shortlist(df, make_cfg(college_cap_pct=100.0, **settings))
        pd.testing.assert_frame_equal(never, off)
        assert not off[S.COLLEGE_CAPPED].any()


def test_borderline_band_follows_the_cap_backfill():
    cfg = make_cfg(top_n=10, college_cap_pct=20.0)       # 2 places per college
    cfg.llm.borderline_band = 2
    ranked = scoring.apply_shortlist(make_scored(["A"] * 4 + [f"U{i}" for i in range(16)]), cfg)
    assert capped(ranked) == ["C02", "C03"] and len(shortlisted(ranked)) == 10
    # cutoff moved to rank 12; capped candidates above it are not "just below the cutoff"
    assert scoring.cutoff_band_ids(ranked, cfg) == ["C12", "C13"]


def test_cap_and_tie_notes_coexist():
    df = make_scored(["A", "A", "B", "C"]).assign(**{S.FINAL_SCORE: [99.0, 98.0, 91.2, 91.2],
                                                    S.HR_PCT: [90.0, 90.0, 100.0, 95.0]})
    out = by_id(scoring.apply_shortlist(df, make_cfg(top_n=2, college_cap_pct=50.0)))
    # A holds 1 place; C01 is capped and C02 backfills; C02 / C03 tie across the cutoff
    assert out.loc["C01", S.COLLEGE_CAPPED]
    assert out.loc["C02", S.STATUS_REASON] == (
        "Rank 3 of 4 (top 2; backfill for 1 college-capped candidate; tied at 91.20 pts; "
        "won tie-break on HackerRank percentage 100% vs 95%)")
    assert out.loc["C03", S.STATUS_REASON] == (
        "Rank 4 of 4 (cutoff: top 2; tied at 91.20 pts; lost tie-break on HackerRank percentage 95% vs 100%)")


def test_college_key():
    assert scoring.college_key("  Alpha   College ") == scoring.college_key("ALPHA COLLEGE") == "alpha college"
    for blank in (None, np.nan, "", "  ", "N/A", "null", "-", "Unknown", "(unknown)"):
        assert scoring.college_key(blank) == ""


def test_pipeline_evaluate_applies_the_cap(monkeypatch):
    from screening import pipeline
    from screening.models import DataQualityReport, ParsedResume, RiskResult

    monkeypatch.setattr(scoring, "skills", types.SimpleNamespace(
        match_required=lambda required, found, text="": ([r for r in required if r in found],
                                                          [r for r in required if r not in found]),
        canonicalize_skill=lambda s: s.strip()))
    monkeypatch.setattr(pipeline, "risk", types.SimpleNamespace(
        run_risk_analysis=lambda scored, pbc, cfg, parsed_all=None: RiskResult()))
    ids = [f"C{i:02d}" for i in range(12)]
    base = pd.DataFrame({S.CANDIDATE_ID: ids, S.HR_SCORE: [100.0 - i for i in range(12)], S.MAX_SCORE: 100.0,
                         S.COLLEGE: ["A"] * 5 + [f"U{i}" for i in range(7)], S.HAS_RESUME: True})
    base[S.HR_PCT] = base[S.HR_SCORE]
    parsed = {cid: ParsedResume(file_name=f"{cid}.txt", file_hash=cid, text="python sql", skills=["Python", "SQL"])
              for cid in ids}
    prepared = pipeline.PreparedData(ta_df=base, quality=DataQualityReport(), parsed={}, matches=pd.DataFrame(),
                                     base=base, parsed_by_candidate=parsed)
    cfg = make_cfg(top_n=5, college_cap_pct=40.0)        # 2 places per college
    first = pipeline.evaluate(prepared, cfg)
    c = first.candidates
    assert capped(c) == ["C02", "C03", "C04"]
    assert first.llm_results == {}
    assert int(c[S.STATUS].isin(scoring.IN_SHORTLIST).sum()) == 5
    assert int((c[S.STATUS].isin(scoring.IN_SHORTLIST) & (c[S.COLLEGE] == "A")).sum()) == 2
    assert c[S.COLLEGE_CAPPED].any() and set(c.loc[c[S.COLLEGE_CAPPED], S.COLLEGE]) == {"A"}
    # re-evaluating with the same config (e.g. reusing the risk result) is deterministic
    again = pipeline.evaluate(prepared, cfg, risk_result=first.risk).candidates
    pd.testing.assert_frame_equal(again, c)
