"""Realistic fake PreparedData / PipelineResult for UI tests.

Built directly from the contract in docs/ARCHITECTURE.md - no engine code runs, so
UI tests stay fast and independent of ingest/matching/scoring/risk.
All people, colleges and addresses are synthetic.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from screening import schema as S
from screening.config import AppConfig
from screening.models import DataQualityReport, ParsedResume, PipelineResult, RiskResult
from screening.pipeline import PreparedData

N_CANDIDATES = 60
N_QUESTIONS = 4
TOP_N = 15
FIRST = ["Asha", "Rohan", "Meera", "Vikram", "Priya", "Arjun", "Kavya", "Tarun", "Ananya", "Rahul", "Ishita",
         "Karthik", "Nisha"]
LAST = ["Kapoor", "Sharma", "Iyer", "Patel", "Reddy", "Nair", "Gupta", "Menon", "Das", "Kulkarni", "Bose"]
COLLEGES = [("Northfield Institute of Technology", "1", "Pune"), ("Riverside College of Engineering", "2", "Hyderabad"),
            ("Lakeside University", "2", "Chennai"), ("Hillcrest Engineering College", "3", "Bengaluru"),
            ("Eastgate Institute of Science", "3", "Kolkata")]
EXTRA_SKILLS = ["Docker", "React", "AWS", "Git", "Pandas", "Spring Boot"]
CLUSTER_1 = ["CAND-00003", "CAND-00007", "CAND-00011", "CAND-00015"]  # same college + IP, identical answers
CLUSTER_2 = ["CAND-00020", "CAND-00024", "CAND-00028"]                # shared GitHub repo + similar resumes
WEAK_PAIR = ["CAND-00032", "CAND-00036"]                              # timing-only link
FOCUS = CLUSTER_1 + CLUSTER_2 + WEAK_PAIR + ["CAND-00001", "CAND-00040", "CAND-00045"]
NO_RESUME = {"CAND-00058", "CAND-00059", "CAND-00060"}
NO_SCORE = {"CAND-00057", "CAND-00060"}
SHARED_REPO = "github.com/devhub/loan-default-predictor"
INDIVIDUAL = [("CAND-00040", "speed_anomaly", "Speed anomaly", "Completed in 14.0 min vs focus median 62.0 min", 25.0),
              ("CAND-00045", "plagiarism_flag", "Plagiarism flag", "HackerRank plagiarism flag set", 40.0),
              ("CAND-00050", "tab_switches", "Tab switches", "9 tab switches (threshold 5)", 10.0)]


def _cid(i: int) -> str:
    return f"CAND-{i:05d}"


def _risk_profile(cid: str) -> dict[str, Any]:
    ind = sum(w for c, _, _, _, w in INDIVIDUAL if c == cid)
    if cid in CLUSTER_1:
        others = ", ".join(o for o in CLUSTER_1 if o != cid)
        return dict(risk_score=min(100.0, 82.0 + ind), risk_level=S.RISK_HIGH, cluster_id="C01", n_links=3,
                    max_pair_score=82.0, individual_score=ind,
                    risk_reasons=f"Identical answers to q2, q3 with {others} | Same IP 49.36.12.7 as {others} | "
                                 "Submissions within 60 s of each other")
    if cid in CLUSTER_2:
        return dict(risk_score=48.0 + ind, risk_level=S.RISK_MEDIUM, cluster_id="C02", n_links=2, max_pair_score=48.0,
                    individual_score=ind, risk_reasons=f"Shared GitHub repo {SHARED_REPO} | Resume project text "
                                                       "similarity 0.86 | 2 shared rare resume bullets")
    if cid in WEAK_PAIR:
        return dict(risk_score=15.0, risk_level=S.RISK_LOW, cluster_id="", n_links=1, max_pair_score=15.0,
                    individual_score=0.0, risk_reasons="Test started within 90 s of another perfect scorer")
    if ind:
        level = S.RISK_MEDIUM if ind >= 30 else S.RISK_LOW
        reasons = " | ".join(f"Pattern consistent with {f.lower()}: {d}" for c, _, f, d, _ in INDIVIDUAL if c == cid)
        return dict(risk_score=ind, risk_level=level, cluster_id="", n_links=0, max_pair_score=0.0,
                    individual_score=ind, risk_reasons=reasons)
    return dict(risk_score=0.0, risk_level=S.RISK_CLEAR, cluster_id="", n_links=0, max_pair_score=0.0,
                individual_score=0.0, risk_reasons="")


def _candidate_rows(cfg: AppConfig, rng: np.random.Generator) -> list[dict[str, Any]]:
    w = cfg.weights.normalized()
    t0 = datetime(2026, 10, 20, 10, 0, 0)
    rows = []
    for i in range(1, N_CANDIDATES + 1):
        cid = _cid(i)
        first, last = FIRST[i % len(FIRST)], LAST[(i * 7) % len(LAST)]
        college, tier, city = COLLEGES[0] if cid in CLUSTER_1 else COLLEGES[i % len(COLLEGES)]
        pct = np.nan if cid in NO_SCORE else (100.0 if cid in FOCUS else float(rng.integers(35, 99)))
        has_resume = cid not in NO_RESUME
        n_req = int(rng.integers(1, 6)) if has_resume else 0
        matched = cfg.skills.required_skills[:n_req]
        missing = cfg.skills.required_skills[n_req:]
        found = sorted(set(matched) | set(rng.choice(EXTRA_SKILLS, size=2, replace=False).tolist())) if has_resume else []
        sub = {S.SUB_HR: 0.0 if np.isnan(pct) else pct / 100,
               S.SUB_SKILLS: min(1.0, len(matched) / cfg.skills.skills_for_full_marks),
               S.SUB_EXPERIENCE: round(float(rng.uniform(0.2, 0.95)), 3) if has_resume else 0.0,
               S.SUB_EDUCATION: round(float(rng.uniform(0.3, 0.95)), 3)}
        pts = {S.PTS_HR: sub[S.SUB_HR] * w.hackerrank, S.PTS_SKILLS: sub[S.SUB_SKILLS] * w.skills,
               S.PTS_EXPERIENCE: sub[S.SUB_EXPERIENCE] * w.experience, S.PTS_EDUCATION: sub[S.SUB_EDUCATION] * w.education}
        k = CLUSTER_1.index(cid) if cid in CLUSTER_1 else -1
        start = t0 + (timedelta(minutes=30, seconds=20 * k) if k >= 0 else timedelta(minutes=int(rng.integers(0, 240))))
        minutes = 41.0 + 0.3 * k if k >= 0 else (14.0 if cid == "CAND-00040" else round(float(rng.uniform(45, 88)), 1))
        ip = "49.36.12.7" if k >= 0 else f"49.36.{12 + i % 7}.{100 + i}"
        hashes = {f"q{q}_hash": (f"cluster1-q{q}" if k >= 0 and q in (2, 3) else
                                 hashlib.md5(f"{cid}-{q}".encode()).hexdigest()[:12]) for q in range(1, N_QUESTIONS + 1)}
        q_scores = {f"q{q}_score": (25.0 if pct == 100 else float(rng.integers(0, 26))) for q in range(1, N_QUESTIONS + 1)}
        email = f"{first.lower()}.{last.lower()}{i}@mail.example.com"
        rows.append({
            S.CANDIDATE_ID: cid, S.NAME: f"{first} {last}", S.EMAIL: email, S.PHONE: f"98{i:08d}",
            S.COLLEGE: college, S.COLLEGE_TIER: tier, S.DEGREE: "B.Tech", S.BRANCH: "Computer Science",
            S.GRAD_YEAR: 2026 if i % 4 else 2025, S.CGPA: round(float(rng.uniform(6.5, 9.6)), 2), S.CITY: city,
            S.FINAL_SCORE: round(sum(pts.values()), 2), **pts, **sub,
            S.HR_SCORE: np.nan if np.isnan(pct) else pct, S.MAX_SCORE: 100.0, S.HR_PCT: pct,
            **_risk_profile(cid), S.IS_FOCUS: cid in FOCUS,
            S.MATCHED_SKILLS: S.SEP_LIST.join(matched), S.MISSING_SKILLS: S.SEP_LIST.join(missing),
            S.N_MATCHED_SKILLS: len(matched), S.SKILLS_FOUND: S.SEP_LIST.join(found),
            S.INTERNSHIP_COUNT: int(rng.integers(0, 3)) if has_resume else 0,
            S.EXPERIENCE_MONTHS: float(rng.integers(0, 9)) if has_resume else 0.0,
            S.PROJECT_COUNT: int(rng.integers(1, 4)) if has_resume else 0,
            S.CERTIFICATIONS: "AWS Certified Cloud Practitioner" if has_resume and i % 3 == 0 else "",
            S.CERT_COUNT: 1 if has_resume and i % 3 == 0 else 0,
            S.RESUME_CGPA: round(float(rng.uniform(6.5, 9.6)), 2) if has_resume else np.nan,
            S.RESUME_DEGREE: "B.Tech" if has_resume else "", S.RELEVANCE: round(float(rng.uniform(0.1, 0.9)), 3),
            S.EXPERIENCE_SOURCE: S.EXP_HEURISTIC if has_resume else S.EXP_NO_RESUME, S.LLM_RATIONALE: "",
            S.SCORE_NOTES: "" if has_resume else "no resume", S.TEST_START: start,
            S.SUBMISSION_TIME: start + timedelta(minutes=minutes), S.COMPLETION_MIN: minutes, S.IP_ADDRESS: ip,
            S.IP_SUBNET: ".".join(ip.split(".")[:3]) + ".0/24", S.TEST_MODE: "Remote" if i % 5 else "On-campus",
            S.TAB_SWITCHES: 9 if cid == "CAND-00050" else int(rng.integers(0, 4)),
            S.COPY_PASTE: int(rng.integers(0, 3)), S.PLAGIARISM_FLAG: cid == "CAND-00045",
            S.SOURCE: "Campus drive" if i % 2 else "Referral", S.EMAIL_DOMAIN: "mail.example.com",
            "test_slot": start.strftime("%Y-%m-%d %H:00"), **hashes, **q_scores,
            S.RESUME_FILE: f"{cid}_{first}_{last}.pdf" if has_resume else "",
            S.RESUME_HASH: hashlib.sha256(cid.encode()).hexdigest() if has_resume else "",
            S.HAS_RESUME: has_resume, S.MATCH_METHOD: S.MATCH_ID_FILENAME if has_resume else "",
            S.MATCH_CONFIDENCE: 1.0 if has_resume else np.nan, S.ELIGIBLE: cid not in NO_SCORE,
            S.DATA_FLAGS: "score_missing" if cid in NO_SCORE else "", "extra_recruiter_notes": "",
        })
    return rows


def _apply_shortlist(df: pd.DataFrame) -> pd.DataFrame:
    """Contract-like ranking/status so the fake is internally consistent."""
    elig = df[df[S.ELIGIBLE]].sort_values([S.FINAL_SCORE, S.HR_PCT, S.CANDIDATE_ID], ascending=[False, False, True])
    rank = pd.Series(np.arange(1, len(elig) + 1, dtype=float), index=elig.index)
    df = df.assign(**{S.RANK: rank.reindex(df.index)})
    in_band = df[S.RANK] <= TOP_N
    review = in_band & df[S.RISK_LEVEL].isin([S.RISK_HIGH, S.RISK_MEDIUM])
    status = np.select([~df[S.ELIGIBLE], review, in_band], [S.STATUS_INELIGIBLE, S.STATUS_REVIEW, S.STATUS_SHORTLISTED],
                       default=S.STATUS_NOT)
    reason = np.select([~df[S.ELIGIBLE], review, in_band],
                       ["No HackerRank score", "Integrity review: " + df[S.RISK_REASONS], f"Top {TOP_N}"],
                       default=f"Below top {TOP_N}")
    return df.assign(**{S.STATUS: status, S.STATUS_REASON: reason})


def _pairs() -> pd.DataFrame:
    rows = []
    for members, base in ((CLUSTER_1, 82.0), (CLUSTER_2, 48.0)):
        for x in range(len(members)):
            for y in range(x + 1, len(members)):
                c1 = members is CLUSTER_1
                rows.append({
                    "cand_a": members[x], "cand_b": members[y], "pair_score": base - 2 * (y - x),
                    "signals": ("Identical answers to q2, q3 | Same IP 49.36.12.7 | Submissions within 60 s" if c1 else
                                f"Shared GitHub repo {SHARED_REPO} | Resume text similarity 0.86 | 2 shared rare bullets"),
                    "signal_keys": ("shared_answer_hash, same_ip, sync_submission" if c1 else
                                    "shared_github_repo, resume_text_high, shared_bullets"),
                    "shared_hashes": "q2, q3" if c1 else "", "resume_similarity": 0.31 if c1 else 0.86,
                    "shared_bullets": 0 if c1 else 2,
                    "shared_bullet_examples": "" if c1 else ("Built a gradient-boosted loan default model with 0.91 AUC "
                                                             "on 50k applications | Deployed a FastAPI scoring service"),
                    "shared_github": "" if c1 else SHARED_REPO, "same_ip": c1, "same_subnet": c1, "same_college": c1,
                    "same_phone": False, "same_email": False, "start_gap_sec": 20.0 * (y - x) if c1 else np.nan,
                    "submit_gap_sec": 38.0 * (y - x) if c1 else np.nan, "duration_gap_min": 0.3 * (y - x) if c1 else np.nan,
                    "sequential_gap_min": np.nan,
                })
    rows.append({"cand_a": WEAK_PAIR[0], "cand_b": WEAK_PAIR[1], "pair_score": 15.0,
                 "signals": "Test started within 90 s", "signal_keys": "sync_start", "shared_hashes": "",
                 "resume_similarity": np.nan, "shared_bullets": 0, "shared_bullet_examples": "", "shared_github": "",
                 "same_ip": False, "same_subnet": False, "same_college": False, "same_phone": False,
                 "same_email": False, "start_gap_sec": 90.0, "submit_gap_sec": 640.0, "duration_gap_min": 9.2,
                 "sequential_gap_min": np.nan})
    return pd.DataFrame(rows)


def _clusters(cands: pd.DataFrame) -> pd.DataFrame:
    names = dict(zip(cands[S.CANDIDATE_ID], cands[S.NAME]))
    common = dict(n_colleges=1, start_span_min=1.0, submit_span_min=1.2)
    return pd.DataFrame([
        {"cluster_id": "C01", "size": 4, "members": ", ".join(CLUSTER_1),
         "member_names": ", ".join(names[c] for c in CLUSTER_1), "risk_level": S.RISK_HIGH, "max_pair_score": 80.0,
         "mean_pair_score": 77.3, "density": 1.0, "top_signals": "Identical answers, Same IP, Synchronised submission",
         "shared_traits": f"College: {COLLEGES[0][0]} (4/4) | IP 49.36.12.7 (4/4) | Submissions span 1.2 min | "
                          "Identical answers: q2, q3", "colleges": COLLEGES[0][0], **common,
         "summary": "4 perfect scorers from one college share an IP, identical answers to q2 and q3 and submitted "
                    "within 1.2 minutes - pattern consistent with coordinated attempts."},
        {"cluster_id": "C02", "size": 3, "members": ", ".join(CLUSTER_2),
         "member_names": ", ".join(names[c] for c in CLUSTER_2), "risk_level": S.RISK_MEDIUM, "max_pair_score": 46.0,
         "mean_pair_score": 44.7, "density": 1.0, "top_signals": "Shared GitHub repo, Resume text similarity",
         "shared_traits": f"GitHub repo {SHARED_REPO} (3/3) | Resume similarity >= 0.86",
         "colleges": ", ".join(sorted({cands.set_index(S.CANDIDATE_ID).at[c, S.COLLEGE] for c in CLUSTER_2})),
         "n_colleges": 2, "start_span_min": 95.0, "submit_span_min": 110.0,
         "summary": "3 perfect scorers list the same GitHub project with near-identical descriptions - pattern "
                    "consistent with shared or purchased project work."},
    ])


def _commonality() -> pd.DataFrame:
    rows = [(S.COLLEGE, COLLEGES[0][0], 6, 14, 0.004, 0.02, True), (S.IP_SUBNET, "49.36.12.0/24", 5, 9, 0.003, 0.02, True),
            (S.CITY, "Pune", 6, 14, 0.03, 0.08, False), (S.TEST_MODE, "Remote", 10, 48, 0.4, 0.6, False)]
    out = []
    for attr, value, fc, pc, p, q, sig in rows:
        fs, ps = fc / len(FOCUS), pc / N_CANDIDATES
        out.append({"attribute": attr, "value": value, "focus_count": fc, "focus_total": len(FOCUS), "focus_share": fs,
                    "population_count": pc, "population_total": N_CANDIDATES, "population_share": ps,
                    "lift": fs / ps, "p_value": p, "q_value": q, "significant": sig})
    return pd.DataFrame(out).sort_values(["significant", "lift"], ascending=[False, False]).reset_index(drop=True)


def _resume(row: pd.Series) -> ParsedResume:
    cid, name = row[S.CANDIDATE_ID], row[S.NAME]
    in_c2 = cid in CLUSTER_2
    project_bullets = ["Built a gradient-boosted loan default model with 0.91 AUC on 50k applications",
                       "Deployed a FastAPI scoring service"] if in_c2 else [
        f"Implemented a REST API in Python serving {100 + len(cid)} requests per second",
        "Wrote unit tests raising coverage to 85%"]
    projects = [{"title": "Loan Default Predictor" if in_c2 else "Campus Placement Portal",
                 "text": " ".join(project_bullets), "bullets": project_bullets, "tech": ["Python", "SQL", "Docker"],
                 "github": SHARED_REPO if in_c2 else f"github.com/{cid.lower()}/placement-portal"}]
    experiences = [{"title": "Software Engineering Intern", "org": "Acme Fintech", "text": "Built data pipelines.",
                    "months": 3.0, "is_internship": True}]
    sections = {"summary": f"{name} - final-year computer science student.",
                "education": f"B.Tech Computer Science, {row[S.COLLEGE]}, CGPA {row[S.CGPA]}/10, 2026",
                "skills": row[S.SKILLS_FOUND], "projects": "\n".join(project_bullets),
                "experience": "Software Engineering Intern, Acme Fintech (Jun 2025 - Aug 2025)",
                "certifications": row[S.CERTIFICATIONS]}
    text = "\n".join([name, row[S.EMAIL]] + [f"{k.upper()}\n{v}" for k, v in sections.items() if v])
    return ParsedResume(
        file_name=row[S.RESUME_FILE], file_hash=row[S.RESUME_HASH], text=text, sections=sections,
        emails=[row[S.EMAIL]], phones=[row[S.PHONE]], urls=[], github_urls=[projects[0]["github"]], linkedin_urls=[],
        name_guess=name, candidate_id_guess=cid, skills=[s for s in row[S.SKILLS_FOUND].split(", ") if s],
        skill_evidence={}, projects=projects, experiences=experiences, bullets=project_bullets,
        education={"degree": "B.Tech", "branch": "Computer Science", "college": row[S.COLLEGE],
                   "cgpa": row[S.RESUME_CGPA], "grad_year": 2026},
        certifications=[c for c in [row[S.CERTIFICATIONS]] if c], total_experience_months=3.0, internship_count=1,
        project_count=1, word_count=len(text.split()))


def make_fake_prepared_and_result(cfg: AppConfig | None = None) -> tuple[PreparedData, PipelineResult]:
    """60 candidates with every contract column, 2 risk clusters, pairs, commonality, flags and resumes."""
    cfg = cfg or AppConfig()
    cfg.shortlist.top_n = TOP_N
    rng = np.random.default_rng(7)
    df = _apply_shortlist(pd.DataFrame(_candidate_rows(cfg, rng)))
    df[S.GRAD_YEAR] = df[S.GRAD_YEAR].astype("Int64")
    for c in (S.TAB_SWITCHES, S.COPY_PASTE):
        df[c] = df[c].astype("Int64")
    q_hash = [f"q{q}_hash" for q in range(1, N_QUESTIONS + 1)]
    q_score = [f"q{q}_score" for q in range(1, N_QUESTIONS + 1)]
    order = [S.RANK, S.STATUS, S.STATUS_REASON, S.CANDIDATE_ID, S.NAME, S.EMAIL, S.PHONE, S.COLLEGE, S.COLLEGE_TIER,
             S.DEGREE, S.BRANCH, S.GRAD_YEAR, S.CGPA, S.CITY, S.FINAL_SCORE, S.PTS_HR, S.PTS_SKILLS, S.PTS_EXPERIENCE,
             S.PTS_EDUCATION, S.SUB_HR, S.SUB_SKILLS, S.SUB_EXPERIENCE, S.SUB_EDUCATION, S.HR_SCORE, S.MAX_SCORE,
             S.HR_PCT, S.RISK_LEVEL, S.RISK_SCORE, S.RISK_REASONS, S.CLUSTER_ID, S.IS_FOCUS, S.N_LINKS,
             S.MAX_PAIR_SCORE, S.INDIVIDUAL_SCORE, S.MATCHED_SKILLS, S.MISSING_SKILLS, S.N_MATCHED_SKILLS,
             S.SKILLS_FOUND, S.INTERNSHIP_COUNT, S.EXPERIENCE_MONTHS, S.PROJECT_COUNT, S.CERTIFICATIONS, S.CERT_COUNT,
             S.RESUME_CGPA, S.RESUME_DEGREE, S.RELEVANCE, S.EXPERIENCE_SOURCE, S.LLM_RATIONALE, S.SCORE_NOTES,
             S.TEST_START, S.SUBMISSION_TIME, S.COMPLETION_MIN, S.IP_ADDRESS, S.IP_SUBNET, S.TEST_MODE,
             S.TAB_SWITCHES, S.COPY_PASTE, S.PLAGIARISM_FLAG, S.SOURCE, S.EMAIL_DOMAIN, "test_slot", *q_hash,
             *q_score, S.RESUME_FILE, S.RESUME_HASH, S.HAS_RESUME, S.MATCH_METHOD, S.MATCH_CONFIDENCE, S.ELIGIBLE,
             S.DATA_FLAGS, "extra_recruiter_notes"]
    candidates = df[order].reset_index(drop=True)

    with_resume = candidates[candidates[S.HAS_RESUME]]
    parsed_by_candidate = {r[S.CANDIDATE_ID]: _resume(r) for _, r in with_resume.iterrows()}
    parsed = {p.file_hash: p for p in parsed_by_candidate.values()}
    matches = pd.DataFrame([{"file_hash": p.file_hash, "file_name": p.file_name, S.CANDIDATE_ID: cid,
                             "match_method": S.MATCH_ID_FILENAME, "match_confidence": 1.0, "note": ""}
                            for cid, p in parsed_by_candidate.items()])
    risk_cols = [S.CANDIDATE_ID, S.IS_FOCUS, S.RISK_SCORE, S.RISK_LEVEL, S.RISK_REASONS, S.CLUSTER_ID, S.N_LINKS,
                 S.MAX_PAIR_SCORE, S.INDIVIDUAL_SCORE]
    risk = RiskResult(
        focus_ids=list(FOCUS), candidate_risk=candidates[risk_cols].copy(), pairs=_pairs(),
        clusters=_clusters(candidates), commonality=_commonality(),
        individual=pd.DataFrame([{S.CANDIDATE_ID: c, "flag_key": k, "flag": f, "detail": d, "weight": wt}
                                 for c, k, f, d, wt in INDIVIDUAL]),
        params={"focus_threshold_pct": 100.0, "edge_threshold": 30.0},
        warnings=["Resume text similarity limited to the focus group (compare_against_all is off)."])
    quality = DataQualityReport(
        column_mapping={"Candidate ID": S.CANDIDATE_ID, "Full Name": S.NAME, "Score": S.HR_SCORE},
        unmapped_columns=["Recruiter Notes"], missing_fields=[], rows_raw=N_CANDIDATES + 1, rows_clean=N_CANDIDATES,
        duplicates_dropped=1)
    quality.add("warning", "ingest", "Duplicate candidate_id dropped (kept latest submission)", "CAND-00012")
    quality.add("info", "match", "3 candidates have no resume")
    quality.add("warning", "ingest", "Missing HackerRank score", "CAND-00057")
    ta_cols = [c for c in order if c in df.columns and not c.startswith(("pts_", "sub_", "risk_"))]
    timings = {"ingest": 0.4, "parse": 12.5, "match": 0.8, "score": 0.9, "risk": 1.1, "shortlist": 0.1}
    base = candidates[ta_cols].copy()
    prepared = PreparedData(ta_df=base.copy(), quality=quality, parsed=parsed, matches=matches, base=base,
                            parsed_by_candidate=parsed_by_candidate, mode="ta+resumes",
                            timings={k: timings[k] for k in ("ingest", "parse", "match")})
    result = PipelineResult(candidates=candidates, parsed_resumes=parsed, parsed_by_candidate=parsed_by_candidate,
                            matches=matches, risk=risk, quality=quality, config=cfg, llm_results={}, timings=timings,
                            mode="ta+resumes")
    return prepared, result
