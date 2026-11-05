"""Tests for screening.scoring: rubric math, policies, shortlist statuses, cutoff band, explanations.

The two skills.py functions scoring uses are replaced with simple fakes (exact, case-insensitive
matching of the parsed skills) so these tests exercise the rubric logic in isolation.
"""
from __future__ import annotations

import time
import types

import numpy as np
import pandas as pd
import pytest

from screening import schema as S
from screening import scoring
from screening.config import AppConfig, RubricWeights
from screening.models import ParsedResume

REQUIRED = ["Python", "Java", "SQL", "Cloud", "PyTorch"]
JD = "python sql cloud backend services"
TWO_CERTS = ("AWS Certified Cloud Practitioner", "Oracle Certified SQL Associate")


def _fake_match_required(required, found, text=""):
    have = {f.casefold() for f in found}
    return ([r for r in required if r.casefold() in have],
            [r for r in required if r.casefold() not in have])


@pytest.fixture(autouse=True)
def fake_skills(monkeypatch):
    monkeypatch.setattr(scoring, "skills", types.SimpleNamespace(
        match_required=_fake_match_required, canonicalize_skill=lambda s: s.strip()))


def make_cfg(**shortlist) -> AppConfig:
    cfg = AppConfig()
    cfg.experience.job_description = JD
    for key, value in shortlist.items():
        setattr(cfg.shortlist, key, value)
    return cfg


def make_resume(cid: str, skills=("Python", "SQL", "Cloud"), internships: int = 2, months: float = 6.0,
                n_projects: int = 3, certs=TWO_CERTS, text: str = JD, cgpa: float | None = 9.6,
                tech=("Python",)) -> ParsedResume:
    projects = [{"title": f"Project {i}", "text": text, "bullets": [], "tech": list(tech), "github": None}
                for i in range(n_projects)]
    experiences = [{"title": "Software Intern", "org": "Acme", "text": text,
                    "months": months / internships, "is_internship": True} for _ in range(internships)]
    return ParsedResume(
        file_name=f"{cid}.pdf", file_hash=f"hash-{cid}", text=text,
        sections={"projects": text, "experience": text, "skills": ", ".join(skills)},
        skills=sorted(skills), projects=projects, experiences=experiences, certifications=list(certs),
        total_experience_months=months, internship_count=internships, project_count=n_projects,
        education={"degree": "B.Tech", "branch": "Computer Science", "cgpa": cgpa},
    )


def make_base(rows: list[dict]) -> pd.DataFrame:
    defaults = {S.HR_SCORE: 100.0, S.MAX_SCORE: 100.0, S.DEGREE: "B.Tech", S.BRANCH: "CSE",
                S.CGPA: 9.6, S.COLLEGE_TIER: "1", S.HAS_RESUME: True}
    df = pd.DataFrame([{**defaults, **r} for r in rows])
    df[S.HR_PCT] = df[S.HR_SCORE] / df[S.MAX_SCORE] * 100.0
    return df


def score_one(row: dict, resume: ParsedResume | None, cfg: AppConfig | None = None, **kw) -> pd.Series:
    base = make_base([{S.CANDIDATE_ID: "C1", **row}])
    parsed = {"C1": resume} if resume is not None else {}
    return scoring.score_candidates(base, parsed, cfg or make_cfg(), **kw).iloc[0]


# ---------------------------------------------------------------------------
# score_candidates
# ---------------------------------------------------------------------------
def test_perfect_candidate_gets_full_marks_and_base_is_untouched():
    base = make_base([{S.CANDIDATE_ID: "C1"}])
    before = base.copy()
    out = scoring.score_candidates(base, {"C1": make_resume("C1")}, make_cfg())
    r = out.iloc[0]
    assert (r[S.PTS_HR], r[S.PTS_SKILLS], r[S.PTS_EXPERIENCE], r[S.PTS_EDUCATION]) == (40.0, 30.0, 20.0, 10.0)
    assert r[S.FINAL_SCORE] == 100.0
    assert r[S.MATCHED_SKILLS] == "Python, SQL, Cloud"
    assert r[S.MISSING_SKILLS] == "Java, PyTorch"
    assert r[S.N_MATCHED_SKILLS] == 3
    assert r[S.SKILLS_FOUND] == "Cloud, Python, SQL"
    assert (r[S.INTERNSHIP_COUNT], r[S.EXPERIENCE_MONTHS], r[S.PROJECT_COUNT]) == (2, 6.0, 3)
    assert r[S.CERT_COUNT] == 2 and r[S.CERTIFICATIONS] == "; ".join(TWO_CERTS)
    assert r[S.RESUME_CGPA] == 9.6 and r[S.RESUME_DEGREE] == "B.Tech Computer Science"
    assert r[S.RELEVANCE] == 1.0
    assert r[S.EXPERIENCE_SOURCE] == S.EXP_HEURISTIC
    assert r[S.LLM_RATIONALE] == "" and r[S.SCORE_NOTES] == ""
    pd.testing.assert_frame_equal(base, before)
    assert S.FINAL_SCORE not in base.columns


def test_partial_candidate_known_points():
    cfg = make_cfg()
    cfg.experience.w_relevance = 0.0  # isolate the deterministic sub-components
    resume = make_resume("C1", skills=("Python", "Java"), internships=1, months=3.0, n_projects=0,
                         certs=("AWS Certified Developer", "Yoga for beginners"))
    r = score_one({S.HR_SCORE: 80.0, S.DEGREE: "B.E.", S.BRANCH: "ECE", S.CGPA: 7.75,
                   S.COLLEGE_TIER: "Tier 2"}, resume, cfg)
    exp_sub = (0.30 * 0.5 + 0.15 * 0.5 + 0.25 * 0.0) / 0.70           # internships, months, projects
    edu_sub = 0.35 * 0.7 + 0.35 * 0.5 + 0.20 * 0.5 + 0.10 * 0.7       # ECE, CGPA, 1/2 certs, tier 2
    assert r[S.PTS_HR] == 32.0                                        # 80% of 40
    assert r[S.PTS_SKILLS] == 20.0                                    # 2 skills x 10
    assert r[S.SUB_EXPERIENCE] == pytest.approx(exp_sub, abs=1e-4)
    assert r[S.SUB_EDUCATION] == pytest.approx(edu_sub, abs=1e-4)
    assert r[S.PTS_EDUCATION] == pytest.approx(5.9, abs=0.005)
    assert r[S.FINAL_SCORE] == pytest.approx(32 + 20 + 20 * exp_sub + 10 * edu_sub, abs=0.01)


def test_weights_are_normalised():
    resume = make_resume("C1")
    a = score_one({S.HR_SCORE: 50.0}, resume, make_cfg())
    cfg = make_cfg()
    cfg.weights = RubricWeights(4, 3, 2, 1)
    b = score_one({S.HR_SCORE: 50.0}, resume, cfg)
    assert b[S.PTS_HR] == 20.0 and b[S.FINAL_SCORE] == a[S.FINAL_SCORE] == 80.0
    cfg.weights = RubricWeights(1, 0, 0, 0)
    c = score_one({S.HR_SCORE: 50.0}, resume, cfg)
    assert c[S.FINAL_SCORE] == 50.0 and c[S.PTS_SKILLS] == 0.0


@pytest.mark.parametrize("mode, found, points", [
    ("per_skill", ("Python",), 10.0),
    ("per_skill", ("Python", "SQL"), 20.0),
    ("per_skill", ("Python", "SQL", "Cloud", "Java"), 30.0),   # capped at 30
    ("per_skill", ("Docker",), 0.0),
    ("coverage", ("Python", "SQL"), 12.0),
    ("coverage", ("Python", "SQL", "Cloud", "Java"), 24.0),
    ("coverage", tuple(REQUIRED), 30.0),
])
def test_skills_per_skill_vs_coverage(mode, found, points):
    cfg = make_cfg()
    cfg.skills.mode = mode
    r = score_one({}, make_resume("C1", skills=found), cfg)
    assert r[S.PTS_SKILLS] == pytest.approx(points)


def test_required_skills_are_canonicalised_and_deduplicated(monkeypatch):
    calls = []

    def match(required, found, text=""):
        calls.append(list(required))
        return _fake_match_required(required, found, text)

    monkeypatch.setattr(scoring, "skills", types.SimpleNamespace(
        match_required=match, canonicalize_skill=lambda s: s.strip().title()))
    cfg = make_cfg()
    cfg.skills.required_skills = [" python", "PYTHON", "sql", ""]
    r = score_one({}, make_resume("C1", skills=("Python",)), cfg)
    assert calls == [["Python", "Sql"]]
    assert r[S.MISSING_SKILLS] == "Sql" and r[S.N_MATCHED_SKILLS] == 1


def test_missing_resume_zero_policy():
    r = score_one({S.HR_SCORE: 90.0, S.CGPA: 9.5, S.HAS_RESUME: False}, None)
    assert (r[S.PTS_HR], r[S.PTS_SKILLS], r[S.PTS_EXPERIENCE]) == (36.0, 0.0, 0.0)
    assert r[S.PTS_EDUCATION] == 10.0          # TA degree, CGPA, tier; certifications excluded
    assert r[S.FINAL_SCORE] == 46.0
    assert r[S.EXPERIENCE_SOURCE] == S.EXP_NO_RESUME
    assert r[S.MISSING_SKILLS] == ", ".join(REQUIRED)
    assert "no resume: skills & experience scored 0" in r[S.SCORE_NOTES]


def test_missing_resume_renormalize_policy():
    cfg = make_cfg(missing_resume_policy="renormalize")
    no_resume = score_one({S.HR_SCORE: 90.0, S.CGPA: 9.5}, None, cfg)
    assert no_resume[S.FINAL_SCORE] == pytest.approx((36.0 + 10.0) / 50.0 * 100.0)   # 92.0
    assert "final rescaled to 100 from 50 available pts" in no_resume[S.SCORE_NOTES]
    hr_only = score_one({S.HR_SCORE: 70.0, S.DEGREE: None, S.BRANCH: None, S.CGPA: None,
                         S.COLLEGE_TIER: None}, None, cfg)
    assert hr_only[S.FINAL_SCORE] == 70.0                      # only HackerRank is available
    with_resume = score_one({}, make_resume("C1"), cfg)
    assert with_resume[S.FINAL_SCORE] == 100.0


def test_unreadable_resume_is_treated_as_missing():
    empty = ParsedResume(file_name="scan.pdf", file_hash="x", text="", parse_status="empty")
    r = score_one({}, empty)
    assert r[S.EXPERIENCE_SOURCE] == S.EXP_NO_RESUME
    assert r[S.PTS_SKILLS] == 0.0
    assert "resume has no extractable text" in r[S.SCORE_NOTES]


def test_missing_hackerrank_score_scores_zero_with_note():
    r = score_one({S.HR_SCORE: np.nan}, make_resume("C1"))
    assert r[S.SUB_HR] == 0.0 and r[S.PTS_HR] == 0.0
    assert "no assessment score" in r[S.SCORE_NOTES]
    assert r[S.FINAL_SCORE] == 60.0


def test_llm_blend_hybrid_rationale_and_failure():
    cfg = make_cfg()
    cfg.experience.w_relevance = 0.0
    resume = make_resume("C1", internships=1, months=3.0, n_projects=0)
    rules = (0.30 * 0.5 + 0.15 * 0.5) / 0.70

    r = score_one({}, resume, cfg, llm_experience={"C1": 1.0})
    assert r[S.SUB_EXPERIENCE] == pytest.approx(0.5 * rules + 0.5, abs=1e-4)
    assert r[S.EXPERIENCE_SOURCE] == S.EXP_HYBRID
    assert "experience blended" in r[S.SCORE_NOTES]

    r = score_one({}, resume, cfg, llm_experience={"C1": {"ok": True, "score": 0.0, "rationale": "Shallow"}})
    assert r[S.SUB_EXPERIENCE] == pytest.approx(0.5 * rules, abs=1e-4)
    assert r[S.LLM_RATIONALE] == "Shallow"

    r = score_one({}, resume, cfg, llm_experience={"C1": None})
    assert r[S.SUB_EXPERIENCE] == pytest.approx(rules, abs=1e-4)
    assert r[S.EXPERIENCE_SOURCE] == S.EXP_LLM_FALLBACK

    r = score_one({}, resume, cfg, llm_experience={"OTHER": 1.0})
    assert r[S.EXPERIENCE_SOURCE] == S.EXP_HEURISTIC


def test_relevance_is_scaled_by_pool_and_zero_without_text():
    base = make_base([{S.CANDIDATE_ID: c} for c in ("A", "B", "C")])
    parsed = {
        "A": make_resume("A"),
        "B": make_resume("B", skills=(), text="gardening flowers soil watering plants"),
    }
    out = scoring.score_candidates(base, parsed, make_cfg()).set_index(S.CANDIDATE_ID)
    assert out.loc["A", S.RELEVANCE] == 1.0
    assert out.loc["B", S.RELEVANCE] == 0.0
    assert out.loc["C", S.RELEVANCE] == 0.0
    assert out[S.RELEVANCE].between(0, 1).all()


def test_education_renormalises_over_available_components():
    only_cgpa = score_one({S.DEGREE: None, S.BRANCH: None, S.COLLEGE_TIER: None, S.CGPA: 7.75}, None)
    assert only_cgpa[S.SUB_EDUCATION] == pytest.approx(0.5)
    nothing = score_one({S.DEGREE: None, S.BRANCH: None, S.COLLEGE_TIER: None, S.CGPA: None}, None)
    assert nothing[S.SUB_EDUCATION] == 0.0
    assert "no education data" in nothing[S.SCORE_NOTES]
    from_resume = score_one({S.CGPA: None}, make_resume("C1", cgpa=8.0))
    assert "CGPA from resume" in from_resume[S.SCORE_NOTES]


@pytest.mark.parametrize("degree, branch, expected", [
    ("B.Tech", "CSE", 1.0), ("MCA", None, 1.0), ("B.E.", "Electronics & Communication", 0.7),
    ("M.Sc", "Statistics", 0.7), ("B.Tech", "Mechanical", 0.4), ("B.Tech", None, 0.4),
    ("B.Com", None, 0.2),
])
def test_degree_relevance_tiers(degree, branch, expected):
    cfg = make_cfg()
    cfg.education.w_cgpa = cfg.education.w_tier = cfg.education.w_certifications = 0.0
    r = score_one({S.DEGREE: degree, S.BRANCH: branch}, None, cfg)
    assert r[S.SUB_EDUCATION] == pytest.approx(expected)


def test_scores_3000_candidates_quickly():
    rng = np.random.default_rng(7)
    words = np.array([f"term{i}" for i in range(5000)] + ("python java sql cloud aws docker api backend "
                                                          "fraud model pipeline latency users").split())
    rows, parsed = [], {}
    for i in range(3000):
        cid = f"CAND-{i:05d}"
        rows.append({S.CANDIDATE_ID: cid, S.HR_SCORE: float(rng.integers(0, 101)),
                     S.CGPA: float(rng.uniform(5, 10)), S.HAS_RESUME: i % 10 != 0})
        if i % 10:
            text = " ".join(rng.choice(words, 400))  # realistic vocabulary size and resume length
            parsed[cid] = make_resume(cid, skills=tuple(rng.choice(REQUIRED + ["Docker"], 3, replace=False)),
                                      internships=int(rng.integers(1, 3)), n_projects=int(rng.integers(0, 4)),
                                      text=text)
    base = make_base(rows)
    start = time.perf_counter()
    out = scoring.score_candidates(base, parsed, make_cfg())
    elapsed = time.perf_counter() - start
    assert elapsed < 2.0
    assert len(out) == 3000 and out[S.FINAL_SCORE].between(0, 100).all()


# ---------------------------------------------------------------------------
# apply_shortlist
# ---------------------------------------------------------------------------
def make_scored(rows: list[dict]) -> pd.DataFrame:
    defaults = {S.HR_PCT: 90.0, S.SUB_SKILLS: 1.0, S.SUB_EXPERIENCE: 0.5, S.HAS_RESUME: True}
    return pd.DataFrame([{**defaults, **r} for r in rows])


def ranks(df: pd.DataFrame) -> dict[str, int]:
    elig = df[df[S.ELIGIBLE]]
    return dict(zip(elig[S.CANDIDATE_ID], elig[S.RANK].astype(int)))


def test_ranking_tie_breaks_are_deterministic():
    rows = [
        {S.CANDIDATE_ID: "D", S.FINAL_SCORE: 80.0},
        {S.CANDIDATE_ID: "C", S.FINAL_SCORE: 80.0},                              # id asc vs D
        {S.CANDIDATE_ID: "B", S.FINAL_SCORE: 80.0, S.SUB_EXPERIENCE: 0.9},       # experience
        {S.CANDIDATE_ID: "A", S.FINAL_SCORE: 80.0, S.HR_PCT: 85.0, S.SUB_EXPERIENCE: 1.0},  # lower HR
        {S.CANDIDATE_ID: "E", S.FINAL_SCORE: 80.0, S.SUB_SKILLS: 0.67, S.SUB_EXPERIENCE: 1.0},  # skills
        {S.CANDIDATE_ID: "F", S.FINAL_SCORE: 95.0, S.HR_PCT: 50.0},
    ]
    expected = {"F": 1, "B": 2, "C": 3, "D": 4, "E": 5, "A": 6}
    df = make_scored(rows)
    cfg = make_cfg(top_n=3)
    for seed in range(5):
        shuffled = df.sample(frac=1.0, random_state=seed).reset_index(drop=True)
        out = scoring.apply_shortlist(shuffled, cfg)
        assert ranks(out) == expected
        assert out[S.CANDIDATE_ID].tolist() == ["F", "B", "C", "D", "E", "A"]
    assert list(out.columns[:4]) == [S.RANK, S.STATUS, S.STATUS_REASON, S.CANDIDATE_ID]
    assert out[S.RANK].dtype == "Int64"


def test_top_n_statuses_and_reasons_without_risk_columns():
    df = make_scored([{S.CANDIDATE_ID: f"C{i}", S.FINAL_SCORE: 100.0 - i} for i in range(5)])
    out = scoring.apply_shortlist(df, make_cfg(top_n=3)).set_index(S.CANDIDATE_ID)
    assert out[S.STATUS].tolist() == [S.STATUS_SHORTLISTED] * 3 + [S.STATUS_NOT] * 2
    assert out.loc["C1", S.STATUS_REASON] == "Rank 2 of 5 (top 3)"
    assert out.loc["C3", S.STATUS_REASON] == "Rank 4 of 5 (cutoff: top 3)"


def test_reason_uses_thousands_separators():
    df = make_scored([{S.CANDIDATE_ID: f"C{i:05d}", S.FINAL_SCORE: 100.0 - i * 0.01} for i in range(2812)])
    out = scoring.apply_shortlist(df, make_cfg(top_n=300)).set_index(S.CANDIDATE_ID)
    assert out.loc["C00056", S.STATUS_REASON] == "Rank 57 of 2,812 (top 300)"


@pytest.mark.parametrize("n, pct, expected", [
    (30, 10.0, 3), (31, 10.0, 4), (2812, 10.0, 282), (8, 12.5, 1), (5, 0.0, 0), (5, 100.0, 5), (3, 150.0, 3),
])
def test_top_pct_rounds_up(n, pct, expected):
    df = make_scored([{S.CANDIDATE_ID: f"C{i:05d}", S.FINAL_SCORE: 100.0 - i * 0.01} for i in range(n)])
    out = scoring.apply_shortlist(df, make_cfg(method="top_pct", top_pct=pct))
    assert int((out[S.STATUS] == S.STATUS_SHORTLISTED).sum()) == expected
    if expected:
        assert out.iloc[0][S.STATUS_REASON] == f"Rank 1 of {n:,} (top {pct:g}% = {expected:,})"


def test_threshold_method():
    df = make_scored([{S.CANDIDATE_ID: c, S.FINAL_SCORE: s}
                      for c, s in (("A", 90.0), ("B", 75.0), ("C", 70.0), ("D", 69.99), ("E", 50.0))])
    out = scoring.apply_shortlist(df, make_cfg(method="threshold", threshold=70.0)).set_index(S.CANDIDATE_ID)
    assert out[S.STATUS].tolist() == [S.STATUS_SHORTLISTED] * 3 + [S.STATUS_NOT] * 2
    assert out.loc["D", S.STATUS_REASON] == "Rank 4 of 5; score 69.99 below threshold 70"


RISKY = [
    {S.CANDIDATE_ID: "A", S.FINAL_SCORE: 95.0, S.RISK_LEVEL: "High", S.RISK_SCORE: 85.0,
     S.RISK_REASONS: "Identical answers to q2, q3 with CAND-00123 | Same IP 49.36.12.7 with CAND-00123"},
    {S.CANDIDATE_ID: "B", S.FINAL_SCORE: 90.0, S.RISK_LEVEL: "Clear"},
    {S.CANDIDATE_ID: "C", S.FINAL_SCORE: 85.0, S.RISK_LEVEL: "Medium", S.RISK_SCORE: 35.0,
     S.RISK_REASONS: "Resume text similarity 0.82 with CAND-00999"},
    {S.CANDIDATE_ID: "D", S.FINAL_SCORE: 80.0, S.RISK_LEVEL: None},
    {S.CANDIDATE_ID: "E", S.FINAL_SCORE: 75.0, S.RISK_LEVEL: "High", S.RISK_REASONS: "Same phone as CAND-1"},
    {S.CANDIDATE_ID: "F", S.FINAL_SCORE: 70.0, S.RISK_LEVEL: "Low"},
]


def test_flagged_candidates_stay_shortlisted_for_review_by_default():
    out = scoring.apply_shortlist(make_scored(RISKY), make_cfg(top_n=3)).set_index(S.CANDIDATE_ID)
    assert out[S.STATUS].to_dict() == {
        "A": S.STATUS_REVIEW, "B": S.STATUS_SHORTLISTED, "C": S.STATUS_REVIEW,
        "D": S.STATUS_NOT, "E": S.STATUS_NOT, "F": S.STATUS_NOT}
    assert out.loc["A", S.STATUS_REASON] == (
        "Integrity review: High risk - identical answers to q2, q3 with CAND-00123 | "
        "Same IP 49.36.12.7 with CAND-00123")
    assert out.loc["C", S.STATUS_REASON].startswith("Integrity review: Medium risk - resume text similarity")
    only_high = scoring.apply_shortlist(make_scored(RISKY), make_cfg(top_n=3, review_risk_levels=["high"]))
    assert only_high.set_index(S.CANDIDATE_ID).loc["C", S.STATUS] == S.STATUS_SHORTLISTED


def test_exclude_high_risk_holds_and_backfills():
    out = scoring.apply_shortlist(make_scored(RISKY), make_cfg(top_n=3, exclude_high_risk=True))
    status = out.set_index(S.CANDIDATE_ID)[S.STATUS].to_dict()
    assert status == {"A": S.STATUS_HOLD, "B": S.STATUS_SHORTLISTED, "C": S.STATUS_REVIEW,
                      "D": S.STATUS_SHORTLISTED, "E": S.STATUS_NOT, "F": S.STATUS_NOT}
    assert int(out[S.STATUS].isin(scoring.IN_SHORTLIST).sum()) == 3
    reasons = out.set_index(S.CANDIDATE_ID)[S.STATUS_REASON]
    assert reasons["A"].startswith("Integrity hold: High risk - identical answers") and "backfilled" in reasons["A"]
    assert reasons["D"] == "Rank 4 of 6 (top 3; backfill for 1 integrity hold)"


def test_threshold_with_exclusion_does_not_backfill_below_threshold():
    cfg = make_cfg(method="threshold", threshold=80.0, exclude_high_risk=True)
    out = scoring.apply_shortlist(make_scored(RISKY), cfg).set_index(S.CANDIDATE_ID)
    assert out[S.STATUS].to_dict() == {
        "A": S.STATUS_HOLD, "B": S.STATUS_SHORTLISTED, "C": S.STATUS_REVIEW,
        "D": S.STATUS_SHORTLISTED, "E": S.STATUS_NOT, "F": S.STATUS_NOT}
    assert out.loc["A", S.STATUS_REASON].endswith("excluded from shortlist")


def test_ineligible_reasons():
    df = make_scored([
        {S.CANDIDATE_ID: "A", S.FINAL_SCORE: 99.0, S.HR_PCT: np.nan},
        {S.CANDIDATE_ID: "B", S.FINAL_SCORE: 98.0, S.HR_PCT: 40.0, S.CGPA: 6.0},
        {S.CANDIDATE_ID: "C", S.FINAL_SCORE: 97.0, S.CGPA: 6.0},
        {S.CANDIDATE_ID: "D", S.FINAL_SCORE: 96.0, S.GRAD_YEAR: 2024},
        {S.CANDIDATE_ID: "E", S.FINAL_SCORE: 50.0},                      # missing CGPA/year pass
        {S.CANDIDATE_ID: "F", S.FINAL_SCORE: 60.0, S.CGPA: 8.0, S.GRAD_YEAR: 2026},
    ])
    cfg = make_cfg(min_hackerrank_pct=50.0, min_cgpa=7.0, allowed_grad_years=["2025", 2026.0])
    out = scoring.apply_shortlist(df, cfg)
    assert out[S.CANDIDATE_ID].tolist() == ["F", "E", "A", "B", "C", "D"]   # eligible first, by rank
    by_id = out.set_index(S.CANDIDATE_ID)
    assert by_id.loc["A", S.STATUS_REASON] == "No HackerRank score"
    assert by_id.loc["B", S.STATUS_REASON] == ("HackerRank 40% below minimum 50%; "
                                               "CGPA 6.00 below minimum 7.00")
    assert by_id.loc["C", S.STATUS_REASON] == "CGPA 6.00 below minimum 7.00"
    assert by_id.loc["D", S.STATUS_REASON] == "Graduation year 2024 not in allowed years (2025, 2026)"
    assert (by_id.loc[["A", "B", "C", "D"], S.STATUS] == S.STATUS_INELIGIBLE).all()
    assert by_id.loc[["A", "B", "C", "D"], S.RANK].isna().all()
    assert ranks(out) == {"F": 1, "E": 2}


def test_resume_only_mode_everyone_is_eligible():
    df = make_scored([{S.CANDIDATE_ID: c, S.FINAL_SCORE: s, S.HR_PCT: np.nan}
                      for c, s in (("R1", 40.0), ("R2", 55.0), ("R3", 50.0))])
    out = scoring.apply_shortlist(df, make_cfg(top_n=2, min_hackerrank_pct=60.0))
    assert out[S.ELIGIBLE].all()
    assert ranks(out) == {"R2": 1, "R3": 2, "R1": 3}


# ---------------------------------------------------------------------------
# cutoff_band_ids
# ---------------------------------------------------------------------------
def band_frame(n: int = 20, no_resume: tuple[str, ...] = (), high: tuple[str, ...] = ()) -> pd.DataFrame:
    return make_scored([{S.CANDIDATE_ID: f"C{i:02d}", S.FINAL_SCORE: 100.0 - i,
                         S.HAS_RESUME: f"C{i:02d}" not in no_resume,
                         S.RISK_LEVEL: "High" if f"C{i:02d}" in high else "Clear"} for i in range(n)])


def test_cutoff_band_top_n_window_skips_candidates_without_resume():
    cfg = make_cfg(top_n=10)
    cfg.llm.cutoff_band = 2
    ranked = scoring.apply_shortlist(band_frame(no_resume=("C10",)), cfg)
    assert scoring.cutoff_band_ids(ranked, cfg) == ["C08", "C09", "C11"]   # ranks 9..12 minus rank 11
    cfg.llm.cutoff_band = 0
    assert scoring.cutoff_band_ids(ranked, cfg) == []


def test_cutoff_band_follows_backfilled_cutoff():
    cfg = make_cfg(top_n=10, exclude_high_risk=True)
    cfg.llm.cutoff_band = 2
    ranked = scoring.apply_shortlist(band_frame(high=("C02",)), cfg)
    assert scoring.cutoff_band_ids(ranked, cfg) == ["C09", "C10", "C11", "C12"]   # cutoff rank 11


def test_cutoff_band_threshold_uses_score_distance():
    cfg = make_cfg(method="threshold", threshold=70.0)
    cfg.llm.cutoff_band = 1
    df = make_scored([{S.CANDIDATE_ID: c, S.FINAL_SCORE: s}
                      for c, s in (("A", 90.0), ("B", 80.0), ("C", 72.0), ("D", 69.0), ("E", 60.0))])
    assert scoring.cutoff_band_ids(scoring.apply_shortlist(df, cfg), cfg) == ["C", "D"]


def test_pipeline_evaluate_blends_llm_for_cutoff_band(monkeypatch):
    from screening import pipeline
    from screening.models import DataQualityReport, RiskResult

    ids = [f"C{i:02d}" for i in range(30)]
    base = make_base([{S.CANDIDATE_ID: cid, S.HR_SCORE: 100.0 - i} for i, cid in enumerate(ids)])
    parsed = {cid: make_resume(cid) for cid in ids}
    risk_frame = pd.DataFrame({S.CANDIDATE_ID: ["C00"], S.RISK_LEVEL: ["High"], S.RISK_SCORE: [80.0],
                               S.RISK_REASONS: ["Identical answers to q1 with C05"], S.IS_FOCUS: [True]})
    monkeypatch.setattr(pipeline, "risk", types.SimpleNamespace(
        run_risk_analysis=lambda scored, pbc, cfg, parsed_all=None: RiskResult(candidate_risk=risk_frame)))
    prepared = pipeline.PreparedData(ta_df=base, quality=DataQualityReport(), parsed={},
                                     matches=pd.DataFrame(), base=base, parsed_by_candidate=parsed)
    cfg = make_cfg(top_n=10)
    cfg.llm.cutoff_band = 2
    result = pipeline.evaluate(prepared, cfg, {cid: {"ok": True, "score": 0.0} for cid in ids})
    c = result.candidates.set_index(S.CANDIDATE_ID)
    assert sorted(result.llm_results) == ["C08", "C09", "C10", "C11"]          # ranks 9..12
    assert (c.loc[sorted(result.llm_results), S.EXPERIENCE_SOURCE] == S.EXP_HYBRID).all()
    assert c.loc["C00", S.STATUS] == S.STATUS_REVIEW
    assert int(c[S.STATUS].isin(scoring.IN_SHORTLIST).sum()) == 10
    assert c.loc["C12", S.STATUS] == S.STATUS_SHORTLISTED                      # moved up after re-score


# ---------------------------------------------------------------------------
# explain_score
# ---------------------------------------------------------------------------
def test_explain_score_lines():
    row = {
        S.HR_SCORE: 100.0, S.MAX_SCORE: 100.0, S.HR_PCT: 100.0, S.PTS_HR: 40.0,
        S.MATCHED_SKILLS: "Python, SQL, Cloud", S.MISSING_SKILLS: "Java, PyTorch", S.PTS_SKILLS: 30.0,
        S.INTERNSHIP_COUNT: 2, S.EXPERIENCE_MONTHS: 5.0, S.PROJECT_COUNT: 3, S.RELEVANCE: 0.72,
        S.PTS_EXPERIENCE: 15.4, S.EXPERIENCE_SOURCE: S.EXP_HEURISTIC, S.DEGREE: "B.Tech",
        S.BRANCH: "CSE", S.CGPA: 8.6, S.CERT_COUNT: 1, S.COLLEGE_TIER: "2", S.PTS_EDUCATION: 7.9,
        S.FINAL_SCORE: 93.3, S.SCORE_NOTES: "", S.LLM_RATIONALE: "",
    }
    lines = scoring.explain_score(pd.Series(row), AppConfig())
    assert lines == [
        "HackerRank: 100/100 (100%) -> 40.0 / 40 pts",
        "Skills: Python, SQL, Cloud matched (3/5) -> 30.0 / 30 pts",
        "Experience: 2 internships, 5 months, 3 projects, relevance 0.72 -> 15.4 / 20 pts",
        "Education: B.Tech CSE, CGPA 8.6, 1 cert, Tier 2 -> 7.9 / 10 pts",
        "Final score: 93.30 / 100",
    ]


def test_explain_score_matches_scored_rows():
    cfg = make_cfg(missing_resume_policy="renormalize")
    base = make_base([{S.CANDIDATE_ID: "C1"}, {S.CANDIDATE_ID: "C2", S.HR_SCORE: np.nan, S.HAS_RESUME: False}])
    scored = scoring.score_candidates(base, {"C1": make_resume("C1")}, cfg).set_index(S.CANDIDATE_ID)
    full = scoring.explain_score(scored.loc["C1"], cfg)
    assert full[0] == "HackerRank: 100/100 (100%) -> 40.0 / 40 pts"
    assert full[1] == "Skills: Python, SQL, Cloud matched (3/5) -> 30.0 / 30 pts"
    missing = scoring.explain_score(scored.loc["C2"].to_dict(), cfg)
    assert missing[0] == "HackerRank: no assessment score -> 0.0 / 40 pts"
    assert missing[1] == "Skills: no resume - excluded (score rescaled)"
    assert missing[4].startswith("Final score: 20.00 / 100 (rescaled")   # (0 + 10) / 50 pts
    assert any(line.startswith("Notes: no assessment score") for line in missing)
