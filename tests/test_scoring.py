"""Tests for screening.scoring: rubric math, policies, tiers, shortlist statuses, borderline band,
explanations, and the pipeline's evaluate / sample loader.

The two skills.py functions scoring uses are replaced with simple fakes (exact, case-insensitive
matching of the parsed skills) so these tests exercise the rubric logic in isolation.
"""
from __future__ import annotations

import inspect
import io
import time
import types
import zipfile

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
    assert r[S.CGPA_SOURCE] == "feed" and r[S.TIER_SOURCE] == ""
    pd.testing.assert_frame_equal(base, before)
    assert S.FINAL_SCORE not in base.columns


def test_partial_candidate_known_points():
    cfg = make_cfg()
    cfg.experience.w_relevance = 0.0  # isolate the deterministic sub-components
    cfg.education.tier_enabled = True
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
    assert r[S.TIER_SOURCE] == "feed"


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
    r = score_one({S.HR_SCORE: 90.0, S.CGPA: 9.5, S.HAS_RESUME: False}, None, make_cfg(missing_resume_policy="zero"))
    assert (r[S.PTS_HR], r[S.PTS_SKILLS], r[S.PTS_EXPERIENCE]) == (36.0, 0.0, 0.0)
    assert r[S.PTS_EDUCATION] == 10.0          # TA degree and CGPA; certifications excluded
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


@pytest.mark.parametrize("mode, expected_final, note", [
    ("ta_only", 92.0, "final rescaled to 100 from 50 available pts"),       # no resumes at all
    ("ta+resumes", 46.0, "no resume: skills & experience scored 0"),        # a missing CV gets no boost
    ("resumes_only", 46.0, "no resume: skills & experience scored 0"),
])
def test_auto_missing_resume_policy_follows_the_run_mode(mode, expected_final, note):
    cfg = make_cfg()
    assert cfg.shortlist.missing_resume_policy == "auto"
    r = score_one({S.HR_SCORE: 90.0, S.CGPA: 9.5, S.HAS_RESUME: False}, None, cfg, mode=mode)
    assert r[S.FINAL_SCORE] == pytest.approx(expected_final)
    assert note in r[S.SCORE_NOTES]


def test_auto_policy_default_mode_is_zero_and_explicit_policies_ignore_mode():
    row = {S.HR_SCORE: 90.0, S.CGPA: 9.5, S.HAS_RESUME: False}
    assert score_one(row, None, make_cfg())[S.FINAL_SCORE] == 46.0                       # default mode
    assert score_one(row, None, make_cfg(missing_resume_policy="zero"), mode="ta_only")[S.FINAL_SCORE] == 46.0
    assert score_one(row, None, make_cfg(missing_resume_policy="renormalize"),
                     mode="ta+resumes")[S.FINAL_SCORE] == pytest.approx(92.0)
    cfg = make_cfg()
    assert scoring.resolve_missing_resume_policy(cfg, "ta_only") == "renormalize"
    assert scoring.resolve_missing_resume_policy(cfg, "ta+resumes") == "zero"
    assert scoring.resolve_missing_resume_policy(make_cfg(missing_resume_policy="bogus"), "ta_only") == "zero"


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


def test_experience_is_rules_only_and_llm_blend_is_gone():
    params = inspect.signature(scoring.score_candidates).parameters
    assert "llm_experience" not in params and params["mode"].kind is inspect.Parameter.KEYWORD_ONLY
    assert not hasattr(scoring, "_blend_llm")
    cfg = make_cfg()
    cfg.experience.w_relevance = 0.0
    r = score_one({}, make_resume("C1", internships=1, months=3.0, n_projects=0), cfg)
    assert r[S.SUB_EXPERIENCE] == pytest.approx((0.30 * 0.5 + 0.15 * 0.5) / 0.70, abs=1e-4)
    assert r[S.EXPERIENCE_SOURCE] == S.EXP_HEURISTIC and r[S.LLM_RATIONALE] == ""
    with pytest.raises(TypeError):
        scoring.score_candidates(make_base([{S.CANDIDATE_ID: "C1"}]), {}, cfg, {"C1": 1.0})


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


def test_cgpa_source_feed_resume_or_blank():
    base = make_base([{S.CANDIDATE_ID: "F", S.CGPA: 8.4}, {S.CANDIDATE_ID: "R", S.CGPA: None},
                      {S.CANDIDATE_ID: "X", S.CGPA: 84.0}, {S.CANDIDATE_ID: "N", S.CGPA: None}])
    parsed = {"F": make_resume("F", cgpa=7.0), "R": make_resume("R", cgpa=8.1), "X": make_resume("X", cgpa=None)}
    out = scoring.score_candidates(base, parsed, make_cfg()).set_index(S.CANDIDATE_ID)
    assert out[S.CGPA_SOURCE].to_dict() == {"F": "feed", "R": "resume", "X": "", "N": ""}
    # resume-only runs build the candidate rows (CGPA included) from the resumes
    resume_only = scoring.score_candidates(base, parsed, make_cfg(), mode="resumes_only").set_index(S.CANDIDATE_ID)
    assert resume_only[S.CGPA_SOURCE].to_dict() == {"F": "resume", "R": "resume", "X": "", "N": ""}
    cfg = make_cfg(min_cgpa=9.0)
    ranked = scoring.apply_shortlist(resume_only.reset_index().assign(**{S.HR_PCT: np.nan}), cfg).set_index(S.CANDIDATE_ID)
    assert ranked.loc["F", S.STATUS_REASON] == "CGPA 8.40 (from resume) is below the minimum threshold of 9.00."
    assert "CGPA 8.4 (from resume)" in scoring.explain_score(resume_only.loc["F"], cfg)[3]


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
# College tier: off by default, feed first, then the approved tier list
# ---------------------------------------------------------------------------
TIER_LIST = {
    "Quartz Zenith Institute of Information and Communication Technology": "Tier 1",
    "Alpha Valley Institute of Technology": "Tier 2",
    "Coastal State University": "Tier 3",
}


def tier_cfg(enabled: bool = True, mapping: dict | None = None) -> AppConfig:
    cfg = make_cfg()
    cfg.education.tier_enabled = enabled
    cfg.education.tier_mapping = dict(TIER_LIST if mapping is None else mapping)
    cfg.education.w_degree, cfg.education.w_cgpa, cfg.education.w_certifications = 0.5, 0.5, 0.0
    cfg.education.w_tier = 1.0
    return cfg


def test_tier_disabled_excludes_tier_and_renormalises():
    cfg = make_cfg()
    assert cfg.education.tier_enabled is False
    cfg.experience.w_relevance = 0.0
    resume = make_resume("C1", certs=("AWS Certified Developer", "Yoga for beginners"))
    r = score_one({S.DEGREE: "B.E.", S.BRANCH: "ECE", S.CGPA: 7.75, S.COLLEGE_TIER: "Tier 3"}, resume, cfg)
    expected = (0.35 * 0.7 + 0.35 * 0.5 + 0.20 * 0.5) / 0.90            # tier weight dropped
    assert r[S.SUB_EDUCATION] == pytest.approx(expected, abs=1e-4)
    assert r[S.TIER_SOURCE] == "" and r[S.COLLEGE_TIER] == "Tier 3"     # feed value kept for display


def test_tier_enabled_feed_first_then_tier_list():
    base = make_base([
        {S.CANDIDATE_ID: "FEED", S.COLLEGE_TIER: "2", S.COLLEGE: "Coastal State University"},
        {S.CANDIDATE_ID: "LIST", S.COLLEGE_TIER: None, S.COLLEGE: "coastal  state university"},
        {S.CANDIDATE_ID: "NONE", S.COLLEGE_TIER: None, S.COLLEGE: "Unlisted Hill College"},
    ])
    out = scoring.score_candidates(base, {}, tier_cfg()).set_index(S.CANDIDATE_ID)
    assert out[S.TIER_SOURCE].to_dict() == {"FEED": "feed", "LIST": "tier list", "NONE": ""}
    assert out.loc["FEED", S.COLLEGE_TIER] == "2"          # feed wins over the list's Tier 3
    assert out.loc["LIST", S.COLLEGE_TIER] == "3"          # filled from the tier list
    # degree 1.0 and CGPA 1.0 (weights 0.5 / 0.5) plus the tier (weight 1.0)
    assert out.loc["FEED", S.SUB_EDUCATION] == pytest.approx((0.5 + 0.5 + 0.7) / 2.0)
    assert out.loc["LIST", S.SUB_EDUCATION] == pytest.approx((0.5 + 0.5 + 0.4) / 2.0)
    # unmatched college: no tier sub-score and no penalty (renormalised over degree + CGPA)
    assert out.loc["NONE", S.SUB_EDUCATION] == pytest.approx(1.0)


@pytest.mark.parametrize("college, expected", [
    ("Quartz Zenith Institute of Information and Communication Technology", "Tier 1"),      # exact
    ("QUARTZ ZENITH INSTITUTE OF INFORMATION & COMMUNICATION TECHNOLOGY", "Tier 1"),        # & / case
    ("QZ-IICT", "Tier 1"),                                    # acronym of the long name
    ("QZIICT", "Tier 1"),
    ("Alpha Valley Institute of Technology, Northfield", "Tier 2"),   # fuzzy: extra campus word
    ("AVIT", "Tier 2"),                                       # acronym (of skipped)
    ("Coastal State Univ", None),                             # too far from any listed name
    ("Institute of Technology", None),                        # generic words only: never fuzzy-matched
    ("XY-IICT", None),                                        # unknown acronym
    ("N/A", None), (None, None),
])
def test_resolve_tier_exact_fuzzy_and_acronym(college, expected):
    label, source = scoring.resolve_tier(college, tier_cfg())
    assert label == expected
    assert source == ("tier list" if expected else "")


def test_resolve_tier_acronym_in_list_matches_long_name_and_feed_wins():
    cfg = tier_cfg(mapping={"XY-IICT": "1", "Coastal State University": "3"})
    assert scoring.resolve_tier("Xavier Yale Institute of Information and Communication Technology", cfg) == (
        "1", "tier list")
    assert scoring.resolve_tier("XY IICT", cfg) == ("1", "tier list")
    assert scoring.resolve_tier("Coastal State University", cfg, feed_tier="Tier 2") == ("Tier 2", "feed")
    assert scoring.resolve_tier("Coastal State University", cfg, feed_tier="n/a") == ("3", "tier list")


@pytest.mark.parametrize("college, expected", [
    ("NIT Rivertown", "1"),            # acronym + campus word
    ("NIT-R", "1"),                    # 'nitr': the campus initial is part of the acronym
    ("NIT-L", None),                   # no listed name has initials n-i-t-l
    ("NIIT Lakeside", "2"),
    ("NIT Lakeside", None),            # NIT != NIIT: a significant word cannot be skipped
    ("NIT Hillview", None),            # campus word must follow the acronym
    ("Northfield Institute of Technology, Rivertown", "1"),
    ("SMU", "3"),                      # acronym key in the list vs long college below
    ("Summit Mountain University Westgate", "3"),
])
def test_acronym_with_campus_word(college, expected):
    cfg = tier_cfg(mapping={"Northfield Institute of Technology Rivertown": "1",
                            "Northfield Institute of Information Technology Lakeside": "2",
                            "Summit Mountain University": "3"})
    assert scoring.resolve_tier(college, cfg)[0] == expected


def test_resolve_tier_ambiguous_match_gives_no_tier():
    cfg = tier_cfg(mapping={"Riverside Engineering College East": "1", "Riverside Engineering College West": "3"})
    assert scoring.resolve_tier("Riverside Engineering College", cfg) == (None, "")
    same_tier = tier_cfg(mapping={"Riverside Engineering College East": "2", "Riverside Engineering College West": "2"})
    assert scoring.resolve_tier("Riverside Engineering College", same_tier) == ("2", "tier list")


def test_tier_threshold_and_disabled_scoring_ignore_the_list():
    cfg = tier_cfg()
    cfg.education.tier_match_threshold = 100
    assert scoring.resolve_tier("Alpha Valley Institute of Technology, Northfield", cfg)[0] == "Tier 2"  # subset = 100
    assert scoring.resolve_tier("Alpha Valley Institut of Technology", cfg)[0] is None
    off = tier_cfg(enabled=False)
    base = make_base([{S.CANDIDATE_ID: "A", S.COLLEGE_TIER: None, S.COLLEGE: "QZ-IICT"}])
    r = scoring.score_candidates(base, {}, off).iloc[0]
    assert r[S.TIER_SOURCE] == "" and pd.isna(r[S.COLLEGE_TIER])


def test_tier_list_key_normalisation():
    assert scoring.tier_list_key("  Q.Z.-IICT, Nörthfield ") == "q z iict northfield"
    assert scoring.tier_list_key("Science & Arts") == "science and arts"


# ---------------------------------------------------------------------------
# apply_shortlist (statuses; eligibility rules and tie-breaks: tests/test_shortlist_rules.py)
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
    assert (out[S.TIE_BREAK_NOTE] == "").all() and (out[S.INELIGIBILITY] == "[]").all()


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


def test_resume_only_mode_everyone_is_eligible():
    df = make_scored([{S.CANDIDATE_ID: c, S.FINAL_SCORE: s, S.HR_PCT: np.nan}
                      for c, s in (("R1", 40.0), ("R2", 55.0), ("R3", 50.0))])
    out = scoring.apply_shortlist(df, make_cfg(top_n=2, min_hackerrank_pct=60.0))
    assert out[S.ELIGIBLE].all()
    assert ranks(out) == {"R2": 1, "R3": 2, "R1": 3}


def test_new_columns_follow_the_documented_order():
    df = make_scored([{S.CANDIDATE_ID: "A", S.FINAL_SCORE: 90.0, S.CGPA_SOURCE: "feed", S.CGPA: 9.0,
                       S.TIER_SOURCE: "", S.COLLEGE_TIER: "1", S.DUPLICATE_OF: "", S.DUPLICATE_IDS: "",
                       S.INJECTION_FLAGS: "", S.SCORE_NOTES: "", S.INDIVIDUAL_SCORE: 0.0}])
    cols = list(scoring.apply_shortlist(df, make_cfg()).columns)
    assert cols.index(S.CGPA_SOURCE) == cols.index(S.CGPA) + 1
    assert cols.index(S.TIER_SOURCE) == cols.index(S.COLLEGE_TIER) + 1
    assert cols.index(S.INJECTION_FLAGS) == cols.index(S.INDIVIDUAL_SCORE) + 1
    assert cols.index(S.TIE_BREAK_NOTE) == cols.index(S.SCORE_NOTES) + 1
    assert cols[-3:] == [S.ELIGIBLE, S.INELIGIBILITY, S.COLLEGE_CAPPED]


# ---------------------------------------------------------------------------
# cutoff_band_ids: borderline candidates just below the cutoff (LLM explanations only)
# ---------------------------------------------------------------------------
def band_frame(n: int = 20, no_resume: tuple[str, ...] = (), high: tuple[str, ...] = ()) -> pd.DataFrame:
    return make_scored([{S.CANDIDATE_ID: f"C{i:02d}", S.FINAL_SCORE: 100.0 - i,
                         S.HAS_RESUME: f"C{i:02d}" not in no_resume,
                         S.RISK_LEVEL: "High" if f"C{i:02d}" in high else "Clear"} for i in range(n)])


def test_borderline_band_is_the_ranks_just_below_the_cutoff():
    cfg = make_cfg(top_n=10)
    cfg.llm.borderline_band = 3
    ranked = scoring.apply_shortlist(band_frame(no_resume=("C10",)), cfg)
    assert scoring.cutoff_band_ids(ranked, cfg) == ["C10", "C11", "C12"]   # ranks 11..13, resume or not
    assert scoring.cutoff_band_ids(band_frame(), cfg) == ["C10", "C11", "C12"]   # unranked input
    cfg.llm.borderline_band = 0
    assert scoring.cutoff_band_ids(ranked, cfg) == []
    cfg.llm.borderline_band = 50
    assert scoring.cutoff_band_ids(ranked, cfg) == [f"C{i:02d}" for i in range(10, 20)]   # only 10 below


def test_borderline_band_follows_backfilled_cutoff_and_skips_ineligible():
    cfg = make_cfg(top_n=10, exclude_high_risk=True)
    cfg.llm.borderline_band = 2
    frame = band_frame(high=("C02",))
    frame.loc[frame[S.CANDIDATE_ID] == "C12", S.HR_PCT] = np.nan    # ineligible: no score
    ranked = scoring.apply_shortlist(frame, cfg)
    assert scoring.cutoff_band_ids(ranked, cfg) == ["C11", "C13"]   # cutoff rank 11 (C10 backfilled)


def test_borderline_band_threshold_and_nobody_shortlisted():
    cfg = make_cfg(method="threshold", threshold=70.0)
    cfg.llm.borderline_band = 1
    df = make_scored([{S.CANDIDATE_ID: c, S.FINAL_SCORE: s}
                      for c, s in (("A", 90.0), ("B", 80.0), ("C", 72.0), ("D", 69.0), ("E", 60.0))])
    assert scoring.cutoff_band_ids(scoring.apply_shortlist(df, cfg), cfg) == ["D"]
    cfg.shortlist.threshold = 95.0
    cfg.llm.borderline_band = 2
    assert scoring.cutoff_band_ids(scoring.apply_shortlist(df, cfg), cfg) == ["A", "B"]


# ---------------------------------------------------------------------------
# pipeline.evaluate (no LLM) and the sample loader
# ---------------------------------------------------------------------------
def _prepared(base, parsed, mode="ta+resumes"):
    from screening import pipeline
    from screening.models import DataQualityReport

    return pipeline.PreparedData(ta_df=base, quality=DataQualityReport(), parsed={}, matches=pd.DataFrame(),
                                 base=base, parsed_by_candidate=parsed, mode=mode)


def test_pipeline_evaluate_is_rules_only(monkeypatch):
    from screening import pipeline
    from screening.models import RiskResult

    assert not hasattr(pipeline, "run_llm_rescore")
    params = inspect.signature(pipeline.evaluate).parameters
    assert list(params) == ["prepared", "cfg", "risk_result"]
    assert params["risk_result"].kind is inspect.Parameter.KEYWORD_ONLY
    assert "llm_client" not in inspect.signature(pipeline.run_pipeline).parameters

    ids = [f"C{i:02d}" for i in range(30)]
    base = make_base([{S.CANDIDATE_ID: cid, S.HR_SCORE: 100.0 - i} for i, cid in enumerate(ids)])
    parsed = {cid: make_resume(cid) for cid in ids}
    risk_frame = pd.DataFrame({S.CANDIDATE_ID: ["C00", "C03"], S.RISK_LEVEL: ["High", "Medium"],
                               S.RISK_SCORE: [80.0, 50.0], S.IS_FOCUS: [True, False],
                               S.RISK_REASONS: ["Identical answers to q1 with C05", "Hidden text in resume"],
                               S.INJECTION_FLAGS: ["", "hidden_text: white text aimed at screeners"]})
    calls = []

    def fake_risk(scored, pbc, cfg, parsed_all=None):
        calls.append(scored)
        return RiskResult(candidate_risk=risk_frame)

    monkeypatch.setattr(pipeline, "risk", types.SimpleNamespace(run_risk_analysis=fake_risk))
    cfg = make_cfg(top_n=10)
    result = pipeline.evaluate(_prepared(base, parsed), cfg)
    c = result.candidates.set_index(S.CANDIDATE_ID)
    assert result.llm_results == {} and result.mode == "ta+resumes"
    assert (c[S.EXPERIENCE_SOURCE] == S.EXP_HEURISTIC).all()
    assert c.loc["C00", S.STATUS] == S.STATUS_REVIEW
    assert c.loc["C03", S.INJECTION_FLAGS].startswith("hidden_text") and c.loc["C04", S.INJECTION_FLAGS] == ""
    assert int(c[S.STATUS].isin(scoring.IN_SHORTLIST).sum()) == 10
    assert c[S.RANK].tolist()[:10] == list(range(1, 11))
    again = pipeline.evaluate(_prepared(base, parsed), cfg, risk_result=RiskResult(candidate_risk=risk_frame))
    pd.testing.assert_frame_equal(again.candidates, result.candidates)
    assert len(calls) == 1                                    # the risk result was reused


def test_pipeline_evaluate_passes_the_run_mode_and_defaults_risk_columns(monkeypatch):
    from screening import pipeline
    from screening.models import RiskResult

    monkeypatch.setattr(pipeline, "risk", types.SimpleNamespace(
        run_risk_analysis=lambda scored, pbc, cfg, parsed_all=None: RiskResult()))
    base = make_base([{S.CANDIDATE_ID: "A", S.HR_SCORE: 90.0, S.CGPA: 9.5, S.HAS_RESUME: False}])
    ta_only = pipeline.evaluate(_prepared(base, {}, mode="ta_only"), make_cfg())
    mixed = pipeline.evaluate(_prepared(base, {}, mode="ta+resumes"), make_cfg())
    assert ta_only.candidates.loc[0, S.FINAL_SCORE] == pytest.approx(92.0)    # auto -> renormalize
    assert mixed.candidates.loc[0, S.FINAL_SCORE] == pytest.approx(46.0)      # auto -> zero
    row = ta_only.candidates.iloc[0]
    assert row[S.RISK_LEVEL] == S.RISK_CLEAR and row[S.INJECTION_FLAGS] == "" and not row[S.IS_FOCUS]
    assert ta_only.mode == "ta_only"


def _write_fake_workday_sample(folder) -> None:
    from openpyxl import Workbook

    wb = Workbook()
    summary = wb.active
    summary.title = "Sheet1"
    for row in (["Review Candidates: R-100200 2", None], ["Active Candidates", "2 of 3"],
                ["Applied Prefilters", None], ["2", "Review"]):
        summary.append(row)
    data = wb.create_sheet("Sheet2")
    data.append(["Job Application", "Candidate ID", "Assessment Comment", "Email", "CGPA", "Branch", "School",
                 "Location", "Max Score", "Score", "Cutoff Score", "Cutoff Score Percentage", "Percentage Score",
                 "Candidate Option"])
    data.append(["Asha Menon (asha menon)", "C-1000042",
                 "Hackerrank Completed – Qualified&#xa;Integrity Issues: No Issues Detected\n"
                 "Integrity Summary: N/A", "asha.menon@example.org", 9.1, "Computer Science & Engineering",
                 "Example Institute of Technology", "Northfield", 175, 175, 96, 55, 100, "Django (Python)"])
    buf = io.BytesIO()
    wb.save(buf)
    (folder / "test_scores_export.xlsx").write_bytes(buf.getvalue())
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as zf:
        zf.writestr("Resume/R-100200 - 2027 Technology Program Intern_set 1.pdf", b"%PDF-1.4 fake packet")
    (folder / "Resume.zip").write_bytes(zbuf.getvalue())
    (folder / "job_description.txt").write_text("Backend engineering with Python and SQL.", encoding="utf-8")


def test_load_sample_dataset_reads_the_workday_sample(tmp_path):
    from screening import pipeline

    _write_fake_workday_sample(tmp_path)
    (tmp_path / "resumes").mkdir()                      # an older layout next to it is ignored
    (tmp_path / "resumes" / "old.txt").write_text("old resume", encoding="utf-8")
    ta_file, docs, jd = pipeline.load_sample_dataset(tmp_path)
    assert ta_file[0] == "test_scores_export.xlsx"
    assert ta_file[1] == (tmp_path / "test_scores_export.xlsx").read_bytes()
    assert [(d.file_name, d.source) for d in docs] == [("Resume.zip", "folder")]
    assert docs[0].content == (tmp_path / "Resume.zip").read_bytes()
    assert jd == "Backend engineering with Python and SQL."
    assert pipeline.sample_dataset_available(tmp_path)
    with zipfile.ZipFile(io.BytesIO(docs[0].content)) as zf:
        assert zf.namelist()[0].startswith("Resume/")


def test_load_sample_dataset_falls_back_to_the_older_layout(tmp_path):
    from screening import pipeline

    (tmp_path / "ta_feed.csv").write_text("candidate_id,score\nC-1000042,90\n", encoding="utf-8")
    (tmp_path / "resumes").mkdir()
    (tmp_path / "resumes" / "C-1000042_Asha_Menon.txt").write_text("Python SQL", encoding="utf-8")
    ta_file, docs, jd = pipeline.load_sample_dataset(str(tmp_path))
    assert ta_file[0] == "ta_feed.csv" and [d.file_name for d in docs] == ["C-1000042_Asha_Menon.txt"]
    assert jd is None
    empty = tmp_path / "empty"
    empty.mkdir()
    assert pipeline.load_sample_dataset(empty) == (None, [], None)
    assert not pipeline.sample_dataset_available(empty)


# ---------------------------------------------------------------------------
# explain_score
# ---------------------------------------------------------------------------
EXPLAIN_ROW = {
    S.HR_SCORE: 100.0, S.MAX_SCORE: 100.0, S.HR_PCT: 100.0, S.PTS_HR: 40.0,
    S.MATCHED_SKILLS: "Python, SQL, Cloud", S.MISSING_SKILLS: "Java, PyTorch", S.PTS_SKILLS: 30.0,
    S.INTERNSHIP_COUNT: 2, S.EXPERIENCE_MONTHS: 5.0, S.PROJECT_COUNT: 3, S.RELEVANCE: 0.72,
    S.PTS_EXPERIENCE: 15.4, S.EXPERIENCE_SOURCE: S.EXP_HEURISTIC, S.DEGREE: "B.Tech",
    S.BRANCH: "CSE", S.CGPA: 8.6, S.CERT_COUNT: 1, S.COLLEGE_TIER: "2", S.PTS_EDUCATION: 7.9,
    S.FINAL_SCORE: 93.3, S.SCORE_NOTES: "", S.LLM_RATIONALE: "",
}


def test_explain_score_lines():
    lines = scoring.explain_score(pd.Series(EXPLAIN_ROW), AppConfig())
    assert lines == [
        "HackerRank: 100/100 (100%) -> 40.0 / 40 pts",
        "Skills: Python, SQL, Cloud matched (3/5) -> 30.0 / 30 pts",
        "Experience: 2 internships, 5 months, 3 projects, relevance 0.72 -> 15.4 / 20 pts",
        "Education: B.Tech CSE, CGPA 8.6, 1 cert -> 7.9 / 10 pts",       # tier scoring is off
        "Final score: 93.30 / 100",
    ]


def test_explain_score_tier_cgpa_source_and_tie_note():
    cfg = AppConfig()
    cfg.education.tier_enabled = True
    assert scoring.explain_score(EXPLAIN_ROW, cfg)[3] == "Education: B.Tech CSE, CGPA 8.6, 1 cert, Tier 2 -> 7.9 / 10 pts"
    row = {**EXPLAIN_ROW, S.TIER_SOURCE: "tier list", S.CGPA: None, S.RESUME_CGPA: 8.4,
           S.TIE_BREAK_NOTE: "tied at 93.30 pts; won tie-break on HackerRank percentage 100% vs 95%"}
    lines = scoring.explain_score(row, cfg)
    assert lines[3] == "Education: B.Tech CSE, CGPA 8.4 (from resume), 1 cert, Tier 2 (tier list) -> 7.9 / 10 pts"
    assert lines[-1] == "Tie-break: tied at 93.30 pts; won tie-break on HackerRank percentage 100% vs 95%"
    assert scoring.explain_score({**row, S.TIER_SOURCE: ""}, cfg)[3].endswith("1 cert -> 7.9 / 10 pts")


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


def test_explain_score_auto_policy_uses_mode_or_the_rows_notes():
    cfg = make_cfg()   # "auto"
    base = make_base([{S.CANDIDATE_ID: "C2", S.HR_SCORE: 90.0, S.HAS_RESUME: False}])
    ta_only = scoring.score_candidates(base, {}, cfg, mode="ta_only").iloc[0]
    mixed = scoring.score_candidates(base, {}, cfg, mode="ta+resumes").iloc[0]
    assert scoring.explain_score(ta_only, cfg)[1] == "Skills: no resume - excluded (score rescaled)"
    assert scoring.explain_score(mixed, cfg)[1] == "Skills: no resume -> 0.0 / 30 pts"
    assert scoring.explain_score(mixed, cfg, mode="ta+resumes")[1] == "Skills: no resume -> 0.0 / 30 pts"
    assert scoring.explain_score(ta_only, cfg, mode="ta_only")[-2].endswith("(rescaled from the available components)")
