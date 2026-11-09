"""scoring.apply_shortlist rules: structured ineligibility, administrative exclusions, duplicate
applications, configurable tie-breaks and tie notes at the shortlist cutoff."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from screening import schema as S
from screening import scoring
from screening.config import TIE_BREAK_KEYS, AppConfig


def make_cfg(**shortlist) -> AppConfig:
    cfg = AppConfig()
    for key, value in shortlist.items():
        setattr(cfg.shortlist, key, value)
    return cfg


def make_scored(rows: list[dict]) -> pd.DataFrame:
    defaults = {S.HR_PCT: 90.0, S.SUB_SKILLS: 1.0, S.SUB_EXPERIENCE: 0.5, S.HAS_RESUME: True}
    return pd.DataFrame([{**defaults, **r} for r in rows])


def by_id(out: pd.DataFrame) -> pd.DataFrame:
    return out.set_index(S.CANDIDATE_ID)


def rules_of(out: pd.DataFrame, cid: str) -> list[dict]:
    return json.loads(by_id(out).loc[cid, S.INELIGIBILITY])


# ---------------------------------------------------------------------------
# Structured ineligibility
# ---------------------------------------------------------------------------
ELIGIBILITY_ROWS = [
    {S.CANDIDATE_ID: "C-1000001", S.FINAL_SCORE: 99.0, S.HR_PCT: np.nan},
    {S.CANDIDATE_ID: "C-1000002", S.FINAL_SCORE: 98.0, S.HR_PCT: 52.0},
    {S.CANDIDATE_ID: "C-1000003", S.FINAL_SCORE: 97.0, S.CGPA: None, S.RESUME_CGPA: 6.4},
    {S.CANDIDATE_ID: "C-1000004", S.FINAL_SCORE: 96.0, S.CGPA: 6.4},
    {S.CANDIDATE_ID: "C-1000005", S.FINAL_SCORE: 95.0, S.GRAD_YEAR: 2024},
    {S.CANDIDATE_ID: "C-1000006", S.FINAL_SCORE: 94.0},
    {S.CANDIDATE_ID: "C-1000007", S.FINAL_SCORE: 93.0, S.HR_PCT: 40.0, S.CGPA: 5.0, S.GRAD_YEAR: 2025},
    {S.CANDIDATE_ID: "C-1000008", S.FINAL_SCORE: 50.0},                    # missing CGPA / year pass
    {S.CANDIDATE_ID: "C-1000009", S.FINAL_SCORE: 60.0, S.CGPA: 8.0, S.GRAD_YEAR: 2026},
]
ELIGIBILITY_CFG = dict(min_hackerrank_pct=60.0, min_cgpa=7.0, allowed_grad_years=["2027", 2026.0],
                       exclude_candidates=[" c-1000006 "])


def test_every_ineligibility_rule_is_structured_with_the_exact_explanation():
    out = scoring.apply_shortlist(make_scored(ELIGIBILITY_ROWS), make_cfg(**ELIGIBILITY_CFG))
    assert rules_of(out, "C-1000001") == [{"rule_violated": S.RULE_NO_SCORE, "candidate_value": None,
                                           "threshold_value": None,
                                           "explanation": "No assessment score recorded on HackerRank."}]
    assert rules_of(out, "C-1000002") == [{"rule_violated": S.RULE_MIN_SCORE, "candidate_value": 52.0,
                                           "threshold_value": 60.0,
                                           "explanation": "HackerRank score 52.0% is below the required minimum "
                                                          "of 60.0%."}]
    assert rules_of(out, "C-1000003") == [{"rule_violated": S.RULE_MIN_CGPA, "candidate_value": 6.4,
                                           "threshold_value": 7.0,
                                           "explanation": "CGPA 6.40 (from resume) is below the minimum "
                                                          "threshold of 7.00."}]
    assert rules_of(out, "C-1000004")[0]["explanation"] == "CGPA 6.40 is below the minimum threshold of 7.00."
    assert rules_of(out, "C-1000005") == [{"rule_violated": S.RULE_GRAD_YEAR, "candidate_value": 2024,
                                           "threshold_value": [2026, 2027],
                                           "explanation": "Graduation year 2024 is not within allowed batches "
                                                          "(2026, 2027)."}]
    assert rules_of(out, "C-1000006") == [{"rule_violated": S.RULE_EXCLUDED, "candidate_value": "C-1000006",
                                           "threshold_value": None,
                                           "explanation": "Disqualified: administrative exclusion by recruiter."}]
    several = rules_of(out, "C-1000007")
    assert [r["rule_violated"] for r in several] == [S.RULE_MIN_SCORE, S.RULE_MIN_CGPA, S.RULE_GRAD_YEAR]
    r = by_id(out)
    assert r.loc["C-1000007", S.STATUS_REASON] == " ".join(x["explanation"] for x in several)
    assert r.loc["C-1000002", S.STATUS_REASON] == "HackerRank score 52.0% is below the required minimum of 60.0%."
    for cid in ("C-1000008", "C-1000009"):
        assert r.loc[cid, S.INELIGIBILITY] == "[]" and r.loc[cid, S.ELIGIBLE]
    ineligible = [f"C-100000{i}" for i in range(1, 8)]
    assert (r.loc[ineligible, S.STATUS] == S.STATUS_INELIGIBLE).all()
    assert r.loc[ineligible, S.RANK].isna().all() and (r.loc[ineligible, S.TIE_BREAK_NOTE] == "").all()
    assert out[S.CANDIDATE_ID].tolist()[:2] == ["C-1000009", "C-1000008"]   # eligible first, by rank
    assert scoring.ineligibility_rules(r.loc["C-1000007", S.INELIGIBILITY]) == several
    assert scoring.ineligibility_rules("") == [] and scoring.ineligibility_rules("not json") == []


def test_explanations_never_print_a_value_equal_to_the_threshold():
    df = make_scored([{S.CANDIDATE_ID: "A", S.FINAL_SCORE: 90.0, S.HR_PCT: 59.96, S.CGPA: 6.999}])
    out = scoring.apply_shortlist(df, make_cfg(min_hackerrank_pct=60.0, min_cgpa=7.0))
    texts = [r["explanation"] for r in rules_of(out, "A")]
    assert texts == ["HackerRank score 59.96% is below the required minimum of 60.00%.",
                     "CGPA 6.999 is below the minimum threshold of 7.000."]


def test_resume_only_data_skips_the_assessment_rules_but_not_the_others():
    df = make_scored([{S.CANDIDATE_ID: "R1", S.FINAL_SCORE: 40.0, S.HR_PCT: np.nan},
                      {S.CANDIDATE_ID: "R2", S.FINAL_SCORE: 55.0, S.HR_PCT: np.nan, S.GRAD_YEAR: 2020}])
    out = scoring.apply_shortlist(df, make_cfg(min_hackerrank_pct=60.0, allowed_grad_years=[2026]))
    assert by_id(out)[S.ELIGIBLE].to_dict() == {"R1": True, "R2": False}
    assert rules_of(out, "R2")[0]["rule_violated"] == S.RULE_GRAD_YEAR


def test_exclusion_list_is_case_insensitive_and_ignores_unknown_ids():
    df = make_scored([{S.CANDIDATE_ID: c, S.FINAL_SCORE: s} for c, s in (("C-1000042", 90.0), ("C-1000043", 80.0))])
    out = scoring.apply_shortlist(df, make_cfg(exclude_candidates=["c-1000042", "C-9999999", "", None]))
    r = by_id(out)
    assert r.loc["C-1000042", S.STATUS] == S.STATUS_INELIGIBLE
    assert r.loc["C-1000042", S.STATUS_REASON] == "Disqualified: administrative exclusion by recruiter."
    assert r.loc["C-1000043", S.RANK] == 1 and r.loc["C-1000043", S.STATUS_REASON] == "Rank 1 of 1 (top 300)"


# ---------------------------------------------------------------------------
# Duplicate applications
# ---------------------------------------------------------------------------
def duplicate_frame() -> pd.DataFrame:
    return make_scored([
        {S.CANDIDATE_ID: "C-1000042", S.FINAL_SCORE: 90.0, S.EMAIL: "asha.menon@northfield-tech.example",
         S.DUPLICATE_IDS: "C-1000043, C-1000044", S.DUPLICATE_OF: ""},
        {S.CANDIDATE_ID: "C-1000043", S.FINAL_SCORE: 92.0, S.EMAIL: "asha.m@freemail.example",
         S.DUPLICATE_OF: "C-1000042", S.DUPLICATE_IDS: ""},
        {S.CANDIDATE_ID: "C-1000044", S.FINAL_SCORE: 91.0, S.EMAIL: "asha.m@webmail.example",
         S.DUPLICATE_OF: "C-1000042", S.DUPLICATE_IDS: ""},
        {S.CANDIDATE_ID: "C-2000001", S.FINAL_SCORE: 80.0, S.EMAIL: "", S.DUPLICATE_IDS: "C-2000002",
         S.DUPLICATE_OF: ""},
        {S.CANDIDATE_ID: "C-2000002", S.FINAL_SCORE: 70.0, S.EMAIL: "rohan@example.net",
         S.DUPLICATE_OF: "C-2000001", S.DUPLICATE_IDS: ""},
        {S.CANDIDATE_ID: "C-3000001", S.FINAL_SCORE: 60.0, S.DUPLICATE_OF: "", S.DUPLICATE_IDS: ""},
    ])


def test_duplicates_are_disqualified_and_the_primary_notes_the_merge():
    out = scoring.apply_shortlist(duplicate_frame(), make_cfg(top_n=2))
    r = by_id(out)
    assert rules_of(out, "C-1000043") == [{
        "rule_violated": S.RULE_DUPLICATE, "candidate_value": "C-1000042", "threshold_value": None,
        "explanation": "Disqualified: Duplicate application of C-1000042 (merged into official profile "
                       "asha.menon@northfield-tech.example)."}]
    assert r.loc["C-1000044", S.STATUS_REASON] == (
        "Disqualified: Duplicate application of C-1000042 (merged into official profile "
        "asha.menon@northfield-tech.example).")
    assert r.loc["C-2000002", S.STATUS_REASON] == "Disqualified: Duplicate application of C-2000001."  # no email
    assert r.loc["C-1000042", S.STATUS_REASON] == (
        "Rank 1 of 3 (top 2) | merged duplicate applications C-1000043, C-1000044")
    assert r.loc["C-2000001", S.STATUS_REASON] == "Rank 2 of 3 (top 2) | merged duplicate application C-2000002"
    assert r.loc["C-3000001", S.STATUS_REASON] == "Rank 3 of 3 (cutoff: top 2)"
    assert r[S.ELIGIBLE].to_dict() == {"C-1000042": True, "C-1000043": False, "C-1000044": False,
                                       "C-2000001": True, "C-2000002": False, "C-3000001": True}


def test_duplicates_stay_eligible_when_auto_disqualify_is_off():
    out = scoring.apply_shortlist(duplicate_frame(), make_cfg(top_n=2, auto_disqualify_duplicates=False))
    r = by_id(out)
    assert r[S.ELIGIBLE].all() and (r[S.INELIGIBILITY] == "[]").all()
    assert r.loc["C-1000043", S.RANK] == 1
    assert "merged" not in " ".join(r[S.STATUS_REASON])


def test_ineligible_primary_still_notes_the_merge_and_self_reference_is_ignored():
    df = make_scored([
        {S.CANDIDATE_ID: "C-1000042", S.FINAL_SCORE: 90.0, S.HR_PCT: np.nan, S.DUPLICATE_IDS: "C-1000043"},
        {S.CANDIDATE_ID: "C-1000043", S.FINAL_SCORE: 92.0, S.DUPLICATE_OF: "C-1000042"},
        {S.CANDIDATE_ID: "C-1000044", S.FINAL_SCORE: 80.0, S.DUPLICATE_OF: "C-1000044"},
    ])
    r = by_id(scoring.apply_shortlist(df, make_cfg()))
    assert r.loc["C-1000042", S.STATUS_REASON] == (
        "No assessment score recorded on HackerRank. | merged duplicate application C-1000043")
    assert r.loc["C-1000044", S.ELIGIBLE]


# ---------------------------------------------------------------------------
# Tie-breaks
# ---------------------------------------------------------------------------
def tied_pair(a: dict, b: dict, score: float = 91.2) -> pd.DataFrame:
    return make_scored([{S.CANDIDATE_ID: "B", S.FINAL_SCORE: score, **b},
                        {S.CANDIDATE_ID: "A", S.FINAL_SCORE: score, **a}])


@pytest.mark.parametrize("key, winner, loser, shown", [
    ("hackerrank_pct", {S.HR_PCT: 100.0}, {S.HR_PCT: 95.0}, ("100%", "95%")),
    ("sub_skills", {S.SUB_SKILLS: 1.0}, {S.SUB_SKILLS: 0.6667}, ("1", "0.67")),
    ("sub_experience", {S.SUB_EXPERIENCE: 0.71}, {S.SUB_EXPERIENCE: 0.7}, ("0.71", "0.7")),
    ("cgpa", {S.CGPA: None, S.RESUME_CGPA: 9.2}, {S.CGPA: 9.0}, ("9.2", "9")),
    ("submission_time", {S.SUBMISSION_TIME: "2026-10-12 10:31:05"}, {S.SUBMISSION_TIME: "2026-10-12 10:45:00"},
     ("2026-10-12 10:31:05", "2026-10-12 10:45:00")),
])
def test_each_tie_break_key_decides_and_is_explained(key, winner, loser, shown):
    # The loser is better on every other key and has the smaller ID: only the configured key can
    # put the winner ("Z-WIN") first.
    others_w = {S.HR_PCT: 90.0, S.SUB_SKILLS: 0.5, S.SUB_EXPERIENCE: 0.5, S.CGPA: 8.0}
    others_l = {S.HR_PCT: 99.0, S.SUB_SKILLS: 1.0, S.SUB_EXPERIENCE: 1.0, S.CGPA: 9.9}
    df = make_scored([{S.CANDIDATE_ID: "A-LOSE", S.FINAL_SCORE: 91.2, **others_l, **loser},
                      {S.CANDIDATE_ID: "Z-WIN", S.FINAL_SCORE: 91.2, **others_w, **winner}])
    out = scoring.apply_shortlist(df, make_cfg(tie_break_order=[key]))
    assert out[S.CANDIDATE_ID].tolist() == ["Z-WIN", "A-LOSE"]
    label = TIE_BREAK_KEYS[key][0]
    r = by_id(out)
    assert r.loc["Z-WIN", S.TIE_BREAK_NOTE] == f"tied at 91.20 pts; won tie-break on {label} {shown[0]} vs {shown[1]}"
    assert r.loc["A-LOSE", S.TIE_BREAK_NOTE] == f"tied at 91.20 pts; lost tie-break on {label} {shown[1]} vs {shown[0]}"


def test_coding_marks_sum_only_coding_questions():
    # q1 is multiple choice (no similarity column); q2 / q3 are coding questions
    a = {"q1_score": 0.0, "q2_score": 60.0, "q3_score": 60.0, "q2_similarity": 10.0, "q3_similarity": 5.0}
    b = {"q1_score": 40.0, "q2_score": 50.0, "q3_score": 60.0, "q2_similarity": 10.0, "q3_similarity": 5.0}
    out = scoring.apply_shortlist(tied_pair(a, b), make_cfg(tie_break_order=["coding_marks", "hackerrank_pct"]))
    assert out[S.CANDIDATE_ID].tolist() == ["A", "B"]
    assert by_id(out).loc["A", S.TIE_BREAK_NOTE] == (
        "tied at 91.20 pts; won tie-break on HackerRank coding-question marks 120 vs 110")
    # without similarity columns every q{n}_score counts: B's MCQ marks now decide
    plain = tied_pair({k: v for k, v in a.items() if "similarity" not in k},
                      {k: v for k, v in b.items() if "similarity" not in k})
    out = scoring.apply_shortlist(plain, make_cfg(tie_break_order=["coding_marks"]))
    assert out[S.CANDIDATE_ID].tolist() == ["B", "A"]
    # no per-question columns at all: the key is skipped (candidate ID decides)
    out = scoring.apply_shortlist(tied_pair({}, {}), make_cfg(tie_break_order=["coding_marks"]))
    assert by_id(out).loc["A", S.TIE_BREAK_NOTE] == "tied at 91.20 pts; won tie-break on Candidate ID A vs B"


def test_tie_break_order_is_followed_and_candidate_id_is_the_last_resort():
    a = {S.HR_PCT: 95.0, S.SUB_SKILLS: 1.0}
    b = {S.HR_PCT: 100.0, S.SUB_SKILLS: 0.5}
    default = scoring.apply_shortlist(tied_pair(a, b), make_cfg())
    assert default[S.CANDIDATE_ID].tolist() == ["B", "A"]                       # HackerRank % first
    skills_first = scoring.apply_shortlist(tied_pair(a, b), make_cfg(tie_break_order=["sub_skills", "hackerrank_pct"]))
    assert skills_first[S.CANDIDATE_ID].tolist() == ["A", "B"]
    only_id = scoring.apply_shortlist(tied_pair(a, b), make_cfg(tie_break_order=[]))
    assert only_id[S.CANDIDATE_ID].tolist() == ["A", "B"]
    assert by_id(only_id).loc["A", S.TIE_BREAK_NOTE] == "tied at 91.20 pts; won tie-break on Candidate ID A vs B"
    unknown = scoring.apply_shortlist(tied_pair(a, b), make_cfg(tie_break_order=["bogus", "sub_skills", "sub_skills"]))
    assert unknown[S.CANDIDATE_ID].tolist() == ["A", "B"]


def test_missing_values_lose_and_submission_time_formats():
    a = {S.SUBMISSION_TIME: pd.Timestamp("2026-10-12 10:31:05")}
    b = {S.SUBMISSION_TIME: None}
    out = scoring.apply_shortlist(tied_pair(a, b), make_cfg(tie_break_order=["submission_time"]))
    assert out[S.CANDIDATE_ID].tolist() == ["A", "B"]
    assert by_id(out).loc["A", S.TIE_BREAK_NOTE] == (
        "tied at 91.20 pts; won tie-break on Earliest assessment submission 2026-10-12 10:31:05 vs n/a")
    pct = scoring.apply_shortlist(tied_pair({S.HR_PCT: 50.0}, {S.HR_PCT: np.nan}), make_cfg(min_hackerrank_pct=0.0))
    # a missing HackerRank score makes B ineligible, so A is not tied with anyone
    assert by_id(pct).loc["A", S.TIE_BREAK_NOTE] == ""


def test_three_way_tie_notes_and_untied_candidates():
    df = make_scored([
        {S.CANDIDATE_ID: "TOP", S.FINAL_SCORE: 99.0},
        {S.CANDIDATE_ID: "X", S.FINAL_SCORE: 91.2, S.HR_PCT: 100.0},
        {S.CANDIDATE_ID: "Y", S.FINAL_SCORE: 91.2, S.HR_PCT: 95.0},
        {S.CANDIDATE_ID: "Z", S.FINAL_SCORE: 91.2, S.HR_PCT: 95.0, S.SUB_SKILLS: 0.5},
    ])
    r = by_id(scoring.apply_shortlist(df, make_cfg(top_n=10)))
    assert r.loc["TOP", S.TIE_BREAK_NOTE] == ""
    assert r.loc["X", S.TIE_BREAK_NOTE] == "tied at 91.20 pts; won tie-break on HackerRank percentage 100% vs 95%"
    assert r.loc["Y", S.TIE_BREAK_NOTE] == ("tied at 91.20 pts; lost tie-break on HackerRank percentage 95% vs 100%; "
                                            "won tie-break on Skills sub-score 1 vs 0.5")
    assert r.loc["Z", S.TIE_BREAK_NOTE] == "tied at 91.20 pts; lost tie-break on Skills sub-score 0.5 vs 1"
    assert r.loc["Y", S.STATUS_REASON] == "Rank 3 of 4 (top 10)"          # not at the cutoff: reason unchanged


def test_tie_at_the_cutoff_is_in_both_status_reasons():
    tops = [{S.CANDIDATE_ID: f"C-{1000000 + i}", S.FINAL_SCORE: round(99.0 - i * 0.02, 2)} for i in range(299)]
    tied = [{S.CANDIDATE_ID: "C-1900000", S.FINAL_SCORE: 91.2, S.HR_PCT: 100.0},
            {S.CANDIDATE_ID: "C-1800000", S.FINAL_SCORE: 91.2, S.HR_PCT: 95.0}]
    rest = [{S.CANDIDATE_ID: f"C-{3000000 + i}", S.FINAL_SCORE: round(50.0 - i * 0.02, 2)} for i in range(1830)]
    out = scoring.apply_shortlist(make_scored(tops + tied + rest), make_cfg(top_n=300))
    r = by_id(out)
    assert r.loc["C-1900000", S.STATUS_REASON] == (
        "Rank 300 of 2,131 (tied at 91.20 pts; won tie-break on HackerRank percentage 100% vs 95%)")
    assert r.loc["C-1800000", S.STATUS_REASON] == (
        "Rank 301 of 2,131 (cutoff: top 300; tied at 91.20 pts; lost tie-break on HackerRank percentage 95% vs 100%)")
    assert r.loc["C-1900000", S.STATUS] == S.STATUS_SHORTLISTED and r.loc["C-1800000", S.STATUS] == S.STATUS_NOT
    assert r.loc["C-1000298", S.STATUS_REASON] == "Rank 299 of 2,131 (top 300)"


def test_cutoff_tie_with_backfill_and_with_integrity_review():
    rows = [
        {S.CANDIDATE_ID: "H", S.FINAL_SCORE: 99.0, S.RISK_LEVEL: "High", S.RISK_REASONS: "Same IP as C-1000077"},
        {S.CANDIDATE_ID: "P", S.FINAL_SCORE: 95.0},
        {S.CANDIDATE_ID: "Q", S.FINAL_SCORE: 91.2, S.HR_PCT: 100.0},
        {S.CANDIDATE_ID: "R", S.FINAL_SCORE: 91.2, S.HR_PCT: 95.0},
    ]
    r = by_id(scoring.apply_shortlist(make_scored(rows), make_cfg(top_n=2, exclude_high_risk=True)))
    assert r.loc["Q", S.STATUS_REASON] == ("Rank 3 of 4 (top 2; backfill for 1 integrity hold; tied at 91.20 pts; "
                                           "won tie-break on HackerRank percentage 100% vs 95%)")
    assert r.loc["R", S.STATUS_REASON].startswith("Rank 4 of 4 (cutoff: top 2; tied at 91.20 pts; lost tie-break")
    rows[2] = {**rows[2], S.RISK_LEVEL: "Medium", S.RISK_REASONS: "Resume text similarity 0.82 with C-1000099"}
    r = by_id(scoring.apply_shortlist(make_scored(rows), make_cfg(top_n=2, exclude_high_risk=True)))
    assert r.loc["Q", S.STATUS] == S.STATUS_REVIEW
    assert r.loc["Q", S.STATUS_REASON] == (
        "Integrity review: Medium risk - resume text similarity 0.82 with C-1000099 "
        "(tied at 91.20 pts; won tie-break on HackerRank percentage 100% vs 95%)")


def test_threshold_ties_never_straddle_the_cutoff():
    df = make_scored([{S.CANDIDATE_ID: c, S.FINAL_SCORE: s, S.HR_PCT: p}
                      for c, s, p in (("A", 80.0, 100.0), ("B", 70.0, 99.0), ("C", 70.0, 98.0), ("D", 60.0, 90.0))])
    r = by_id(scoring.apply_shortlist(df, make_cfg(method="threshold", threshold=70.0)))
    assert r.loc["C", S.STATUS] == S.STATUS_SHORTLISTED
    assert r.loc["C", S.STATUS_REASON] == "Rank 3 of 4 (score >= 70)"
    assert r.loc["C", S.TIE_BREAK_NOTE].startswith("tied at 70.00 pts; lost tie-break on HackerRank percentage")


def test_final_scores_are_compared_at_two_decimals():
    df = make_scored([{S.CANDIDATE_ID: "A", S.FINAL_SCORE: 91.2000000001, S.HR_PCT: 80.0},
                      {S.CANDIDATE_ID: "B", S.FINAL_SCORE: 91.2, S.HR_PCT: 90.0}])
    out = scoring.apply_shortlist(df, make_cfg())
    assert out[S.CANDIDATE_ID].tolist() == ["B", "A"]           # float noise is a tie, HackerRank % decides
