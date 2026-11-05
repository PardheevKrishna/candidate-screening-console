"""Rubric scoring (weighted components), shortlist statuses, LLM cutoff band and score explanations.

Points out of 100, weights from ``cfg.weights.normalized()`` (defaults shown)::

    HackerRank   40   hackerrank_pct / 100 x 40
    Skills       30   per_skill: 10 pts per required skill found, full marks at 3
    Experience   20   internships, duration, projects, JD relevance (+ optional LLM blend)
    Education    10   degree relevance, CGPA, certifications, college tier

Each component has a 0..1 sub-score (``sub_*``) and points (``pts_*`` = sub x weight).
``apply_shortlist`` turns scores into eligibility, a deterministic rank and a status with
a human-readable reason; ``explain_score`` renders the breakdown of one candidate.
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import numpy as np
import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer

from . import schema as S
from . import skills
from .config import AppConfig, EducationConfig, ShortlistConfig
from .models import ParsedResume

TEST_SLOT = "test_slot"  # derived at ingest (start time floored to the hour); not in schema yet

# Final column order (docs/ARCHITECTURE.md). q{n}_hash / q{n}_score follow TEST_SLOT;
# unknown columns and then extra_* go last.
_HEAD_COLUMNS: list[str] = [
    S.RANK, S.STATUS, S.STATUS_REASON, S.CANDIDATE_ID, S.NAME, S.EMAIL, S.PHONE, S.COLLEGE,
    S.COLLEGE_TIER, S.DEGREE, S.BRANCH, S.GRAD_YEAR, S.CGPA, S.CITY, S.FINAL_SCORE,
    S.PTS_HR, S.PTS_SKILLS, S.PTS_EXPERIENCE, S.PTS_EDUCATION,
    S.SUB_HR, S.SUB_SKILLS, S.SUB_EXPERIENCE, S.SUB_EDUCATION,
    S.HR_SCORE, S.MAX_SCORE, S.HR_PCT,
    S.RISK_LEVEL, S.RISK_SCORE, S.RISK_REASONS, S.CLUSTER_ID, S.IS_FOCUS, S.N_LINKS,
    S.MAX_PAIR_SCORE, S.INDIVIDUAL_SCORE,
    S.MATCHED_SKILLS, S.MISSING_SKILLS, S.N_MATCHED_SKILLS, S.SKILLS_FOUND,
    S.INTERNSHIP_COUNT, S.EXPERIENCE_MONTHS, S.PROJECT_COUNT, S.CERTIFICATIONS, S.CERT_COUNT,
    S.RESUME_CGPA, S.RESUME_DEGREE, S.RELEVANCE, S.EXPERIENCE_SOURCE, S.LLM_RATIONALE,
    S.SCORE_NOTES,
    S.TEST_START, S.SUBMISSION_TIME, S.COMPLETION_MIN, S.IP_ADDRESS, S.IP_SUBNET, S.TEST_MODE,
    S.TAB_SWITCHES, S.COPY_PASTE, S.PLAGIARISM_FLAG, S.SOURCE, S.EMAIL_DOMAIN, TEST_SLOT,
]
_TAIL_COLUMNS: list[str] = [
    S.RESUME_FILE, S.RESUME_HASH, S.HAS_RESUME, S.MATCH_METHOD, S.MATCH_CONFIDENCE,
    S.ELIGIBLE, S.DATA_FLAGS,
]
_Q_SCORE_RE = re.compile(r"^q(\d+)_score$")

SHORTLIST_METHODS = ("top_n", "top_pct", "threshold")
IN_SHORTLIST = (S.STATUS_SHORTLISTED, S.STATUS_REVIEW)
# College values that mean "not recorded"; such candidates are never college-capped.
_UNKNOWN_COLLEGE_TOKENS = frozenset({
    "", "na", "n/a", "n.a.", "nan", "null", "none", "nil", "-", "--", "---", "#n/a",
    "not available", "not applicable", "unknown", "(unknown)",
})

# Degree/branch relevance: the first matching tier wins; other non-empty text -> 0.2.
_DEGREE_TIERS: tuple[tuple[float, re.Pattern[str]], ...] = (
    (1.0, re.compile(
        r"comput|software|informat|\bcse\b|\bcs\b|\bit\b|\bise\b|\bmca\b|\bbca\b|\bdata\b|"
        r"artificial intelligence|machine learning|\bai\b|\bml\b|\baiml\b|\baids\b|cyber",
        re.I)),
    (0.7, re.compile(
        r"electr|\bece\b|\beee\b|\bee\b|\beie\b|\bextc\b|\bentc\b|telecom|instrumentation|"
        r"\bmath|statistic|\bstats?\b",
        re.I)),
    (0.4, re.compile(
        r"engineer|technolog|\bb\.?\s?tech\b|\bm\.?\s?tech\b|\bb\.?\s?e\b|\bm\.?\s?e\b|"
        r"mechanical|civil|chemical|aero|automobile|production|industrial|metallurg|mining|"
        r"marine|biotech|bio-?medical|petroleum|textile|physics|chemistry|\b[bm]\.?\s?sc\b|"
        r"(?:bachelor|master)s? of science",
        re.I)),
)
_DEGREE_OTHER = 0.2

# A certification counts as relevant when it names a technical topic or a required skill.
_TECH_CERT_PATTERN = (
    r"\b(?:aws|amazon web services|azure|gcp|google cloud|cloud|python|java|javascript|sql|"
    r"oracle|mysql|postgres\w*|mongodb|data|analytics?|machine learning|deep learning|ai|ml|"
    r"nlp|artificial intelligence|tensorflow|pytorch|docker|kubernetes|devops|linux|git|"
    r"cyber\s?security|security|network\w*|ccna|comptia|salesforce|sap|tableau|power ?bi|"
    r"microsoft|developer|programming|software|web|full ?stack|front ?end|back ?end|react|"
    r"angular|node|spring|golang|kotlin|android|ios|blockchain|iot|databases?|dbms|big data|"
    r"hadoop|spark|scrum|agile|hackerrank|leetcode|codechef|algorithms?|data structures)\b"
)
# Quantified outcome: a percentage or a number that is not a calendar year ("2024").
_QUANTIFIED_RE = re.compile(r"\d\s*%|\b(?!(?:19|20)\d\d\b)\d+(?:[.,]\d+)?")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _isna(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (str, list, tuple, set, dict, np.ndarray)):
        return False
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _clean(value: Any) -> str:
    return "" if _isna(value) else str(value).strip()


def _float(value: Any) -> float | None:
    """Finite float or None (accepts numbers and numeric strings)."""
    if _isna(value) or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _fmt_num(value: float, dp: int = 2) -> str:
    """Compact number: 40.0 -> '40', 8.60 -> '8.6', 87.25 -> '87.25'."""
    text = f"{value:.{dp}f}".rstrip("0").rstrip(".") if dp > 0 else f"{value:.0f}"
    return "0" if text in ("", "-0") else text


def _plural(count: float, noun: str) -> str:
    return f"{_fmt_num(count, 1)} {noun}{'' if count == 1 else 's'}"


def _num(df: pd.DataFrame, col: str) -> pd.Series:
    """Float view of a column (NaN when the column is missing or unparseable)."""
    if col not in df.columns:
        return pd.Series(np.nan, index=df.index, dtype="float64")
    return pd.to_numeric(df[col], errors="coerce").astype("float64")


def _str_list(df: pd.DataFrame, col: str) -> list[str]:
    if col not in df.columns:
        return [""] * len(df)
    return [_clean(v) for v in df[col].tolist()]


def _as_bool(value: Any) -> bool:
    if _isna(value):
        return False
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, float, np.integer, np.floating)):
        return value != 0
    return str(value).strip().lower() in {"true", "t", "yes", "y", "1"}


def _bool_col(df: pd.DataFrame, col: str, default: bool = False) -> np.ndarray:
    if col not in df.columns:
        return np.full(len(df), default, dtype=bool)
    return np.array([_as_bool(v) for v in df[col].tolist()], dtype=bool)


def _ids(df: pd.DataFrame) -> list[str]:
    if S.CANDIDATE_ID not in df.columns:
        return [f"ROW-{i:05d}" for i in range(1, len(df) + 1)]
    return _str_list(df, S.CANDIDATE_ID)


def _hr_pct(df: pd.DataFrame) -> pd.Series:
    """hackerrank_pct (0..100); falls back to score / max x 100 where the pct is missing."""
    pct = _num(df, S.HR_PCT)
    if S.HR_SCORE in df.columns:
        mx = _num(df, S.MAX_SCORE).fillna(100.0)
        derived = _num(df, S.HR_SCORE) / mx.where(mx > 0) * 100.0
        pct = pct.fillna(derived)
    return pct.clip(0.0, 100.0)


def _ratio(values: np.ndarray, full: Any) -> np.ndarray:
    """min(1, values / full); full <= 0 means 'any positive value earns full marks'."""
    v = np.asarray(values, dtype=float)
    f = _float(full)
    if f is None or f <= 0:
        return (v > 0).astype(float)
    return np.clip(v / f, 0.0, 1.0)


def _clip01(value: Any) -> float:
    v = _float(value)
    return 0.0 if v is None else min(1.0, max(0.0, v))


def _effective_cgpa(df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """(cgpa, from_resume): TA cgpa (valid 0 < v <= 10) preferred, else resume_cgpa."""
    ta = _num(df, S.CGPA).to_numpy()
    ta = np.where((ta > 0) & (ta <= 10), ta, np.nan)
    res = _num(df, S.RESUME_CGPA).to_numpy()
    res = np.where((res > 0) & (res <= 10), res, np.nan)
    return np.where(np.isnan(ta), res, ta), np.isnan(ta) & ~np.isnan(res)


def order_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Reorder columns to the documented final order (every column is kept)."""
    cols = list(df.columns)
    present = set(cols)
    head = [c for c in _HEAD_COLUMNS if c in present]
    hashes = S.answer_hash_columns(cols)
    q_scores = sorted((c for c in cols if _Q_SCORE_RE.match(str(c))),
                      key=lambda c: int(_Q_SCORE_RE.match(str(c)).group(1)))
    tail = [c for c in _TAIL_COLUMNS if c in present]
    used = set(head) | set(hashes) | set(q_scores) | set(tail)
    extras = [c for c in cols if c not in used and str(c).startswith("extra_")]
    others = [c for c in cols if c not in used and c not in extras]
    return df[head + hashes + q_scores + tail + others + extras]


# ---------------------------------------------------------------------------
# Resume features
# ---------------------------------------------------------------------------
@dataclass
class _ResumeFeatures:
    """Per-candidate resume features, aligned with the rows of ``base``."""

    linked: np.ndarray            # a resume is linked to the candidate
    usable: np.ndarray            # ... and it has extractable content
    skill_error: np.ndarray       # skills.match_required failed for the row
    skills_found: list[str]
    matched: list[str]
    missing: list[str]
    n_matched: np.ndarray
    internships: np.ndarray
    months: np.ndarray
    projects: np.ndarray
    project_quality: np.ndarray   # share of projects with quantified outcomes / tech evidence
    certifications: list[str]
    cert_count: np.ndarray
    relevant_certs: np.ndarray
    resume_cgpa: np.ndarray
    resume_degree: list[str]      # "degree branch" as parsed from the resume
    degree_only: list[str]
    branch_only: list[str]
    texts: list[str]              # projects + experience + skills text (JD relevance)


def _required_skills(cfg: AppConfig) -> list[str]:
    """Configured required skills -> canonical names, de-duplicated, order kept."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in cfg.skills.required_skills or []:
        name = _clean(raw)
        if not name:
            continue
        try:
            canon = _clean(skills.canonicalize_skill(name)) or name
        except NotImplementedError:
            raise
        except Exception:  # never let one odd skill name break scoring
            canon = name
        if canon.casefold() not in seen:
            seen.add(canon.casefold())
            out.append(canon)
    return out


def _cert_pattern(required: list[str]) -> re.Pattern[str]:
    alts = "|".join(re.escape(s) for s in required if s)
    extra = rf"|(?<![A-Za-z0-9_])(?:{alts})(?![A-Za-z0-9_])" if alts else ""
    return re.compile(_TECH_CERT_PATTERN + extra, re.I)


def _is_usable(p: ParsedResume) -> bool:
    return bool(_clean(getattr(p, "text", ""))) or bool(getattr(p, "skills", None))


def _unique_clean(values: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for v in values or []:
        text = " ".join(_clean(v).split())
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            out.append(text)
    return out


def _internships(p: ParsedResume) -> int:
    flagged = sum(1 for e in p.experiences or [] if isinstance(e, Mapping) and e.get("is_internship"))
    return max(int(_float(p.internship_count) or 0), flagged)


def _months(p: ParsedResume) -> float:
    total = _float(p.total_experience_months) or 0.0
    if total <= 0:
        total = sum(_float(e.get("months")) or 0.0 for e in p.experiences or [] if isinstance(e, Mapping))
    return max(0.0, total)


def _projects(p: ParsedResume) -> tuple[int, float]:
    """(project count, share of projects with quantified outcomes or tech evidence)."""
    entries = [e for e in p.projects or [] if isinstance(e, Mapping)]
    count = max(int(_float(p.project_count) or 0), len(entries))
    if not entries:
        return count, 0.0
    strong = 0
    for e in entries:
        text = " ".join([_clean(e.get("title")), _clean(e.get("text"))]
                        + [_clean(b) for b in e.get("bullets") or []])
        if e.get("tech") or _QUANTIFIED_RE.search(text):
            strong += 1
    return count, strong / len(entries)


def _relevance_text(p: ParsedResume) -> str:
    sections = p.sections or {}
    text = "\n".join(t for t in (_clean(sections.get(k)) for k in ("projects", "experience", "skills")) if t)
    if not text:
        parts = [_clean(e.get("text")) for e in (p.projects or []) + (p.experiences or [])
                 if isinstance(e, Mapping)]
        text = "\n".join(t for t in parts if t)
    if not text:
        text = _clean(p.text)
    if p.skills:
        text = f"{text}\n{' '.join(map(str, p.skills))}"
    return text.strip()


def _extract_features(ids: list[str], parsed_by_candidate: Mapping[str, ParsedResume],
                      required: list[str]) -> _ResumeFeatures:
    n = len(ids)
    cert_re = _cert_pattern(required)
    all_missing = S.SEP_LIST.join(required)
    linked = np.zeros(n, dtype=bool)
    usable = np.zeros(n, dtype=bool)
    skill_error = np.zeros(n, dtype=bool)
    skills_found, matched, missing = [""] * n, [""] * n, [all_missing] * n
    n_matched = np.zeros(n, dtype=np.int64)
    internships = np.zeros(n, dtype=np.int64)
    months = np.zeros(n, dtype=float)
    projects = np.zeros(n, dtype=np.int64)
    quality = np.zeros(n, dtype=float)
    certs, cert_count, relevant = [""] * n, np.zeros(n, dtype=np.int64), np.zeros(n, dtype=np.int64)
    resume_cgpa = np.full(n, np.nan)
    degree, degree_only, branch_only, texts = [""] * n, [""] * n, [""] * n, [""] * n

    for i, cid in enumerate(ids):
        p = parsed_by_candidate.get(cid)
        if p is None:
            continue
        linked[i] = True
        if not _is_usable(p):
            continue
        usable[i] = True
        found = [str(s) for s in p.skills or [] if _clean(s)]
        skills_found[i] = S.SEP_LIST.join(found)
        if required:
            try:
                hit, miss = skills.match_required(required, found, p.text or "")
            except NotImplementedError:
                raise
            except Exception:  # one malformed resume must not abort the batch
                hit, miss = [], list(required)
                skill_error[i] = True
            matched[i] = S.SEP_LIST.join(map(str, hit))
            missing[i] = S.SEP_LIST.join(map(str, miss))
            n_matched[i] = len(hit)
        internships[i] = _internships(p)
        months[i] = _months(p)
        projects[i], quality[i] = _projects(p)
        cert_list = _unique_clean(p.certifications)
        certs[i] = S.SEP_NOTES.join(cert_list)
        cert_count[i] = len(cert_list)
        relevant[i] = sum(1 for c in cert_list if cert_re.search(c))
        edu = p.education or {}
        cg = _float(edu.get("cgpa"))
        resume_cgpa[i] = cg if cg is not None and 0 < cg <= 10 else np.nan
        degree_only[i], branch_only[i] = _clean(edu.get("degree")), _clean(edu.get("branch"))
        degree[i] = " ".join(x for x in (degree_only[i], branch_only[i]) if x)
        texts[i] = _relevance_text(p)

    return _ResumeFeatures(linked, usable, skill_error, skills_found, matched, missing, n_matched,
                           internships, months, projects, quality, certs, cert_count, relevant,
                           resume_cgpa, degree, degree_only, branch_only, texts)


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------
def _skills_sub(n_matched: np.ndarray, n_required: int, cfg: AppConfig) -> np.ndarray:
    if n_required == 0:
        return np.zeros(len(n_matched))
    if cfg.skills.mode == "coverage":
        return np.clip(n_matched / n_required, 0.0, 1.0)
    return _ratio(n_matched, cfg.skills.skills_for_full_marks)  # "per_skill" (default)


def _relevance(texts: list[str], usable: np.ndarray, cfg: AppConfig,
               required: list[str]) -> tuple[np.ndarray, bool]:
    """TF-IDF cosine vs the JD, scaled by the pool's 95th percentile (floor 0.1), clipped 0..1.

    Returns (relevance, available); available is False when there is no JD / skills text.
    """
    rel = np.zeros(len(texts))
    jd = "\n".join(t for t in (_clean(cfg.experience.job_description), " ".join(required)) if t)
    if not jd:
        return rel, False
    idx = [i for i, t in enumerate(texts) if usable[i] and t]
    if not idx:
        return rel, True
    try:
        # Unigrams: bigrams add ~1M+ features at 3,000 resumes (5x slower) for no ranking gain.
        vec = TfidfVectorizer(stop_words="english", sublinear_tf=True)
        mat = vec.fit_transform([texts[i] for i in idx] + [jd])
    except ValueError:  # empty vocabulary (only stop words / symbols)
        return rel, True
    cos = (mat[:-1] @ mat[-1].T).toarray().ravel()
    scale = max(float(np.percentile(cos, 95)), 0.1)
    rel[idx] = np.clip(cos / scale, 0.0, 1.0)
    return np.round(rel, 4), True


def _experience_heuristic(f: _ResumeFeatures, relevance: np.ndarray, rel_available: bool,
                          cfg: AppConfig) -> np.ndarray:
    e = cfg.experience
    w = np.array([_float(e.w_internships) or 0.0, _float(e.w_duration) or 0.0,
                  _float(e.w_projects) or 0.0, (_float(e.w_relevance) or 0.0) if rel_available else 0.0])
    w = np.clip(w, 0.0, None)
    if w.sum() <= 0:
        w = np.array([1.0, 1.0, 1.0, 1.0 if rel_available else 0.0])
    w = w / w.sum()
    parts = np.vstack([
        _ratio(f.internships, e.internships_for_full),
        _ratio(f.months, e.months_for_full),
        0.7 * _ratio(f.projects, e.projects_for_full) + 0.3 * f.project_quality,
        relevance,
    ])
    return np.where(f.usable, w @ parts, 0.0)


def _llm_entry(value: Any) -> tuple[float | None, str]:
    """(score 0..1 or None if unusable, rationale) from a float or {"score", "rationale"}."""
    rationale = ""
    if isinstance(value, Mapping):
        rationale = _clean(value.get("rationale"))
        if value.get("ok") is False:
            return None, rationale
        value = value.get("score")
    score = _float(value)
    return (None if score is None else min(1.0, max(0.0, score))), rationale


def _blend_llm(heuristic: np.ndarray, usable: np.ndarray, ids: list[str],
               llm_experience: Mapping[str, Any] | None, cfg: AppConfig
               ) -> tuple[np.ndarray, list[str], list[str], list[str]]:
    """-> (sub_experience, experience_source, llm_rationale, llm note) per row."""
    n = len(ids)
    sub = heuristic.copy()
    source = [S.EXP_HEURISTIC if u else S.EXP_NO_RESUME for u in usable]
    rationale, notes = [""] * n, [""] * n
    if not llm_experience:
        return sub, source, rationale, notes
    w = _clip01(cfg.llm.blend_weight)
    for i, cid in enumerate(ids):
        if not usable[i] or cid not in llm_experience:
            continue
        score, why = _llm_entry(llm_experience[cid])
        if score is None:
            source[i] = S.EXP_LLM_FALLBACK
            notes[i] = "LLM re-score unavailable - rules-based experience kept"
            continue
        sub[i] = (1.0 - w) * heuristic[i] + w * score
        source[i] = S.EXP_LLM if w >= 1.0 else S.EXP_HYBRID
        rationale[i] = why
        notes[i] = f"experience blended: rules {heuristic[i]:.2f}, LLM {score:.2f} (LLM weight {w:.0%})"
    return sub, source, rationale, notes


@lru_cache(maxsize=4096)
def _degree_relevance(text: str) -> float:
    if not text:
        return np.nan
    for score, pattern in _DEGREE_TIERS:
        if pattern.search(text):
            return score
    return _DEGREE_OTHER


def _tier_scores(ed: EducationConfig) -> tuple[dict[str, float], float]:
    """Tier digit -> score, plus the fallback (lowest configured) for unlisted tiers."""
    scores: dict[str, float] = {}
    for key, value in (ed.tier_scores or {}).items():
        digit, v = re.search(r"\d", str(key)), _float(value)
        if digit and v is not None:
            scores[digit.group()] = min(1.0, max(0.0, v))
    return scores, (min(scores.values()) if scores else np.nan)


def _tier_digit(value: Any) -> str:
    m = re.search(r"\d", _clean(value))
    return m.group() if m else ""


def _education(df: pd.DataFrame, f: _ResumeFeatures, cfg: AppConfig
               ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """-> (sub_education, has_any_data, cgpa_from_resume). Weights re-normalised over
    the sub-components that have data (missing -> excluded, not zero)."""
    ed = cfg.education
    degree_text = [
        " ".join(x for x in (d or rd, b or rb) if x)
        for d, b, rd, rb in zip(_str_list(df, S.DEGREE), _str_list(df, S.BRANCH),
                                f.degree_only, f.branch_only)
    ]
    c_degree = np.array([_degree_relevance(t) for t in degree_text], dtype=float)

    ta = _num(df, S.CGPA).to_numpy()
    ta = np.where((ta > 0) & (ta <= 10), ta, np.nan)
    cgpa = np.where(np.isnan(ta), f.resume_cgpa, ta)
    from_resume = np.isnan(ta) & ~np.isnan(f.resume_cgpa)
    lo, hi = _float(ed.cgpa_floor) or 0.0, _float(ed.cgpa_ceiling) or 10.0
    if hi > lo:
        c_cgpa = np.clip((cgpa - lo) / (hi - lo), 0.0, 1.0)
    else:
        c_cgpa = np.where(np.isnan(cgpa), np.nan, (cgpa >= hi).astype(float))

    c_certs = np.where(f.usable, _ratio(f.relevant_certs, ed.certs_for_full), np.nan)

    scores, fallback = _tier_scores(ed)
    c_tier = np.array([scores.get(d, fallback) if d else np.nan
                       for d in map(_tier_digit, _str_list(df, S.COLLEGE_TIER))], dtype=float)

    comps = np.vstack([c_degree, c_cgpa, c_certs, c_tier])
    w = np.array([_float(ed.w_degree) or 0.0, _float(ed.w_cgpa) or 0.0,
                  _float(ed.w_certifications) or 0.0, _float(ed.w_tier) or 0.0])
    w = np.clip(w, 0.0, None)[:, None]
    have = ~np.isnan(comps) & (w > 0)
    num = (np.where(have, comps, 0.0) * w).sum(axis=0)
    den = (have * w).sum(axis=0)
    sub = np.divide(num, den, out=np.zeros(len(df)), where=den > 0)
    return sub, den > 0, from_resume


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def score_candidates(base: pd.DataFrame, parsed_by_candidate: Mapping[str, ParsedResume] | None,
                     cfg: AppConfig, llm_experience: Mapping[str, Any] | None = None) -> pd.DataFrame:
    """Return a NEW frame: ``base`` + resume-derived features + rubric sub-scores/points.

    llm_experience: candidate_id -> LLM experience score 0..1 (a float, or a mapping with
    "score" and optional "rationale"). Blended with the heuristic for candidates with a
    usable resume; a None/NaN score marks a failed call (heuristic kept, noted).
    A linked resume without extractable text is treated as missing (missing-resume policy).
    """
    out = base.copy()
    n = len(out)
    weights = cfg.weights.normalized()
    ids = _ids(out)
    required = _required_skills(cfg)
    f = _extract_features(ids, parsed_by_candidate or {}, required)

    pct = _hr_pct(out).to_numpy()
    sub_hr = np.nan_to_num(pct / 100.0, nan=0.0)
    sub_skills = np.where(f.usable, _skills_sub(f.n_matched, len(required), cfg), 0.0)
    relevance, rel_available = _relevance(f.texts, f.usable, cfg, required)
    heuristic = _experience_heuristic(f, relevance, rel_available, cfg)
    sub_exp, source, rationale, llm_notes = _blend_llm(heuristic, f.usable, ids, llm_experience, cfg)
    sub_edu, edu_available, cgpa_from_resume = _education(out, f, cfg)

    pts_hr = sub_hr * weights.hackerrank
    pts_skills = sub_skills * weights.skills
    pts_exp = sub_exp * weights.experience
    pts_edu = sub_edu * weights.education
    final = pts_hr + pts_skills + pts_exp + pts_edu

    # "renormalize" only drops the resume components; a missing HackerRank score still counts 0.
    renormalize = cfg.shortlist.missing_resume_policy == "renormalize"
    available = weights.hackerrank + weights.education * edu_available
    if renormalize:
        rescaled = np.divide((pts_hr + pts_edu) * 100.0, available, out=np.zeros(n), where=available > 0)
        final = np.where(f.usable, final, rescaled)

    notes: list[str] = []
    for i in range(n):
        row: list[str] = []
        if np.isnan(pct[i]):
            row.append("no assessment score")
        if not f.usable[i]:
            label = "resume has no extractable text" if f.linked[i] else "no resume"
            row.append(f"{label}: final rescaled to 100 from {_fmt_num(available[i])} available pts"
                       if renormalize else f"{label}: skills & experience scored 0")
        if f.skill_error[i]:
            row.append("skills matching failed")
        if cgpa_from_resume[i]:
            row.append("CGPA from resume")
        if not edu_available[i]:
            row.append("no education data")
        if llm_notes[i]:
            row.append(llm_notes[i])
        notes.append(S.SEP_NOTES.join(row))

    out[S.SKILLS_FOUND] = f.skills_found
    out[S.MATCHED_SKILLS] = f.matched
    out[S.MISSING_SKILLS] = f.missing
    out[S.N_MATCHED_SKILLS] = f.n_matched
    out[S.INTERNSHIP_COUNT] = f.internships
    out[S.EXPERIENCE_MONTHS] = np.round(f.months, 1)
    out[S.PROJECT_COUNT] = f.projects
    out[S.CERTIFICATIONS] = f.certifications
    out[S.CERT_COUNT] = f.cert_count
    out[S.RESUME_CGPA] = f.resume_cgpa
    out[S.RESUME_DEGREE] = f.resume_degree
    out[S.SUB_HR] = np.round(sub_hr, 4)
    out[S.SUB_SKILLS] = np.round(sub_skills, 4)
    out[S.SUB_EXPERIENCE] = np.round(sub_exp, 4)
    out[S.SUB_EDUCATION] = np.round(sub_edu, 4)
    out[S.PTS_HR] = np.round(pts_hr, 2)
    out[S.PTS_SKILLS] = np.round(pts_skills, 2)
    out[S.PTS_EXPERIENCE] = np.round(pts_exp, 2)
    out[S.PTS_EDUCATION] = np.round(pts_edu, 2)
    out[S.FINAL_SCORE] = np.round(final, 2)
    out[S.RELEVANCE] = relevance
    out[S.EXPERIENCE_SOURCE] = source
    out[S.LLM_RATIONALE] = rationale
    out[S.SCORE_NOTES] = notes
    return out


def _slots(method: str, sl: ShortlistConfig, n_eligible: int, final_sorted: np.ndarray) -> tuple[int, str]:
    """-> (number of shortlist slots, cutoff description used in status reasons)."""
    if method == "threshold":
        thr = _float(sl.threshold) or 0.0
        return int((final_sorted >= thr).sum()), f"score >= {_fmt_num(thr)}"
    if method == "top_pct":
        pct = _float(sl.top_pct) or 0.0
        slots = math.ceil(round(n_eligible * pct / 100.0, 9)) if pct > 0 else 0
        slots = min(n_eligible, slots)
        return slots, f"top {_fmt_num(pct)}% = {slots:,}"
    top_n = max(0, int(_float(sl.top_n) or 0))
    return min(n_eligible, top_n), f"top {top_n:,}"


def _lower_first(text: str) -> str:
    """'Identical answers ...' -> 'identical answers ...'; acronyms ('IP', 'GitHub') kept."""
    word = text.split(" ", 1)[0]
    if len(word) > 1 and word[0].isupper() and word[1:].islower():
        return text[0].lower() + text[1:]
    return text


def _risk_summary(reasons: str, score: float | None, max_items: int = 3) -> str:
    parts = [p.strip() for p in reasons.split(S.SEP_REASONS.strip()) if p.strip()]
    if not parts:
        return f"risk score {_fmt_num(score, 0)}" if score else "no reasons recorded"
    text = S.SEP_REASONS.join(parts[:max_items])
    if len(parts) > max_items:
        text += f" (+{len(parts) - max_items} more)"
    return _lower_first(text)


def _eligibility(df: pd.DataFrame, pct: np.ndarray, sl: ShortlistConfig) -> list[list[str]]:
    """Ineligibility reasons per row (empty list = eligible)."""
    n = len(df)
    reasons: list[list[str]] = [[] for _ in range(n)]
    resume_only = n > 0 and bool(np.isnan(pct).all())
    if not resume_only:
        min_pct = _float(sl.min_hackerrank_pct) or 0.0
        for i in range(n):
            if np.isnan(pct[i]):
                reasons[i].append("No HackerRank score")
            elif min_pct > 0 and pct[i] < min_pct:
                reasons[i].append(f"HackerRank {_fmt_num(pct[i], 1)}% below minimum {_fmt_num(min_pct, 1)}%")
    min_cgpa = _float(sl.min_cgpa) or 0.0
    if min_cgpa > 0:
        cgpa, from_resume = _effective_cgpa(df)
        for i in np.flatnonzero(cgpa < min_cgpa):
            src = " (from resume)" if from_resume[i] else ""
            reasons[i].append(f"CGPA {cgpa[i]:.2f}{src} below minimum {min_cgpa:.2f}")
    allowed = sorted({int(y) for y in map(_float, sl.allowed_grad_years or []) if y is not None})
    if allowed:
        years = _num(df, S.GRAD_YEAR).to_numpy()
        listed = ", ".join(map(str, allowed))
        for i in np.flatnonzero(~np.isnan(years) & ~np.isin(years, allowed)):
            reasons[i].append(f"Graduation year {int(years[i])} not in allowed years ({listed})")
    return reasons


def college_key(value: Any) -> str:
    """College as compared by the college cap: whitespace-collapsed and case-folded.

    Returns '' when the college is not recorded (blank or a placeholder such as 'N/A');
    such candidates are never capped.
    """
    key = " ".join(_clean(value).split()).casefold()
    return "" if key in _UNKNOWN_COLLEGE_TOKENS else key


def _college_cap_band(keys: list[str], hold: np.ndarray, slots: int, cap_n: int,
                      headcount: bool) -> tuple[np.ndarray, np.ndarray]:
    """(in_band, capped) for eligible candidates in rank order when a college cap is set.

    Walks the ranking: a candidate whose college already holds ``cap_n`` shortlist places is
    capped. Holds and capped candidates take neither a slot nor a college place. top_n /
    top_pct keep walking until ``slots`` places are filled (backfill); threshold only considers
    the ``slots`` candidates at or above the threshold (no backfill).
    """
    m = len(keys)
    in_band = np.zeros(m, dtype=bool)
    capped = np.zeros(m, dtype=bool)
    places: dict[str, int] = {}
    filled = 0
    for k in range(m):
        if (filled if headcount else k) >= slots:
            break
        in_band[k] = True
        if hold[k]:
            continue
        key = keys[k]
        if key and places.get(key, 0) >= cap_n:
            capped[k] = True
            continue
        if key:
            places[key] = places.get(key, 0) + 1
        filled += 1
    return in_band, capped


def _backfill_note(n_holds: int, n_capped: int) -> str:
    parts = []
    if n_holds:
        parts.append(f"{n_holds:,} integrity hold{'' if n_holds == 1 else 's'}")
    if n_capped:
        parts.append(f"{n_capped:,} college-capped candidate{'' if n_capped == 1 else 's'}")
    return " and ".join(parts)


def apply_shortlist(df: pd.DataFrame, cfg: AppConfig) -> pd.DataFrame:
    """Eligibility, deterministic rank, cutoff and status (+ reason) for every candidate.

    Rank (1..n, eligible only): final_score desc, hackerrank_pct desc, sub_skills desc,
    sub_experience desc, candidate_id asc. Resume-only data (no HackerRank scores at all)
    -> everyone is eligible. Missing CGPA / graduation year never knocks a candidate out.
    Risk columns are optional (missing -> Clear). With exclude_high_risk, High-risk
    candidates in the band go to Integrity Hold and top_n / top_pct slots are backfilled
    (a threshold shortlist is never extended below the threshold).
    College cap (``shortlist.college_cap_pct`` > 0): one college may hold at most
    max(1, floor(pct% x shortlist slots)) places (Shortlisted + Integrity Review; holds do not
    count). Candidates beyond that are Not Shortlisted with COLLEGE_CAPPED True; top_n /
    top_pct backfill with the next-best candidates, threshold does not. Candidates without a
    recorded college are never capped. COLLEGE_CAPPED is False everywhere when the cap is off.
    Returns a new frame sorted by rank (ineligible last), index reset, columns in final order.
    """
    out = df.copy()
    sl = cfg.shortlist
    n = len(out)
    ids = _ids(out)
    pct = _hr_pct(out).to_numpy()
    final = _num(out, S.FINAL_SCORE).fillna(0.0).to_numpy()
    reasons = _eligibility(out, pct, sl)
    eligible = np.array([not r for r in reasons], dtype=bool)

    keys = pd.DataFrame({
        "final": final, "pct": pct,
        "skills": _num(out, S.SUB_SKILLS).to_numpy(), "exp": _num(out, S.SUB_EXPERIENCE).to_numpy(),
        "cid": ids,
    })
    order = keys.iloc[np.flatnonzero(eligible)].sort_values(
        ["final", "pct", "skills", "exp", "cid"], ascending=[False, False, False, False, True],
        na_position="last", kind="mergesort").index.to_numpy()
    m = len(order)

    method = sl.method if sl.method in SHORTLIST_METHODS else "top_n"
    headcount = method != "threshold"
    slots, cut_desc = _slots(method, sl, m, final[order])

    levels = [lvl.title() if lvl else S.RISK_CLEAR for lvl in _str_list(out, S.RISK_LEVEL)]
    risk_reasons = _str_list(out, S.RISK_REASONS)
    risk_scores = _num(out, S.RISK_SCORE).to_numpy()
    review_levels = {_clean(x).title() for x in sl.review_risk_levels or []}

    hold = np.array([bool(sl.exclude_high_risk) and levels[i] == S.RISK_HIGH for i in order], dtype=bool)
    colleges = [" ".join(c.split()) for c in _str_list(out, S.COLLEGE)]
    cap_pct = max(0.0, _float(getattr(sl, "college_cap_pct", 0.0)) or 0.0)
    cap_n = max(1, math.floor(round(cap_pct * slots / 100.0, 9))) if cap_pct > 0 and slots > 0 else 0
    if cap_n:
        in_band, capped = _college_cap_band([college_key(colleges[i]) for i in order], hold, slots, cap_n,
                                            headcount)
    elif headcount:  # holds do not consume a slot -> the band extends (backfill)
        takes_slot = ~hold
        used = np.cumsum(takes_slot)
        in_band = np.where(takes_slot, used <= slots, used - takes_slot < slots)
        capped = np.zeros(m, dtype=bool)
    else:
        in_band = np.arange(1, m + 1) <= slots
        capped = np.zeros(m, dtype=bool)
    n_holds = int((hold & in_band).sum())
    # capped candidates whose places were backfilled: those ranked above the last slot taken
    took_slot = np.flatnonzero(in_band & ~hold & ~capped)
    n_capped = int(capped[: took_slot[-1] + 1].sum()) if len(took_slot) else 0

    rank = np.full(n, -1, dtype=np.int64)
    status = [S.STATUS_INELIGIBLE] * n
    status_reason = [S.SEP_NOTES.join(r) for r in reasons]
    college_capped = np.zeros(n, dtype=bool)
    for k, i in enumerate(order):
        r = k + 1
        rank[i] = r
        level = levels[i]
        if not in_band[k]:
            status[i] = S.STATUS_NOT
            status_reason[i] = (f"Rank {r:,} of {m:,} (cutoff: {cut_desc})" if headcount else
                                f"Rank {r:,} of {m:,}; score {final[i]:.2f} below threshold "
                                f"{_fmt_num(_float(sl.threshold) or 0.0)}")
        elif hold[k]:
            status[i] = S.STATUS_HOLD
            status_reason[i] = (f"Integrity hold: {level} risk - "
                                f"{_risk_summary(risk_reasons[i], _float(risk_scores[i]))}; "
                                + ("slot backfilled" if headcount else "excluded from shortlist"))
        elif capped[k]:
            status[i] = S.STATUS_NOT
            college_capped[i] = True
            status_reason[i] = (f"College cap reached: {colleges[i]} already has {cap_n:,} shortlisted "
                                f"(cap {_fmt_num(cap_pct)}%)")
        elif level in review_levels:
            status[i] = S.STATUS_REVIEW
            status_reason[i] = (f"Integrity review: {level} risk - "
                                f"{_risk_summary(risk_reasons[i], _float(risk_scores[i]))}")
        else:
            status[i] = S.STATUS_SHORTLISTED
            status_reason[i] = f"Rank {r:,} of {m:,} ({cut_desc})"
            if headcount and r > slots and (n_holds or n_capped):
                status_reason[i] = (f"Rank {r:,} of {m:,} ({cut_desc}; backfill for "
                                    f"{_backfill_note(n_holds, n_capped)})")

    out[S.ELIGIBLE] = eligible
    out[S.RANK] = pd.array([r if r > 0 else None for r in rank], dtype="Int64")
    out[S.STATUS] = status
    out[S.STATUS_REASON] = status_reason
    out[S.COLLEGE_CAPPED] = college_capped

    ineligible = keys.iloc[np.flatnonzero(~eligible)].sort_values(
        ["final", "cid"], ascending=[False, True], kind="mergesort").index.to_numpy()
    out = out.iloc[np.concatenate([order, ineligible]).astype(np.int64)].reset_index(drop=True)
    return order_columns(out)


def _cutoff_rank(df: pd.DataFrame, cfg: AppConfig, rank: pd.Series, eligible: np.ndarray) -> int:
    """Rank of the last shortlisted candidate (0 when nobody is shortlisted)."""
    if S.STATUS in df.columns:
        listed = eligible & np.isin(np.array(_str_list(df, S.STATUS), dtype=object), IN_SHORTLIST)
        return int(rank[listed].max()) if listed.any() else 0
    method = cfg.shortlist.method if cfg.shortlist.method in SHORTLIST_METHODS else "top_n"
    final_sorted = np.sort(_num(df, S.FINAL_SCORE).fillna(0.0).to_numpy()[eligible])[::-1]
    return _slots(method, cfg.shortlist, int(eligible.sum()), final_sorted)[0]


def cutoff_band_ids(ranked: pd.DataFrame, cfg: AppConfig) -> list[str]:
    """Candidates near the shortlist boundary (LLM re-scoring targets), ordered by rank.

    top_n / top_pct: eligible candidates ranked within the last ``cfg.llm.cutoff_band``
    places inside the cutoff and the first ``cutoff_band`` places outside it (ranks
    cutoff - band + 1 .. cutoff + band), restricted to candidates with a resume.
    threshold: the 2 x band eligible candidates with a resume whose final score is
    nearest the threshold.
    """
    band = int(_float(cfg.llm.cutoff_band) or 0)
    if ranked is None or ranked.empty or band <= 0:
        return []
    df = ranked if {S.RANK, S.ELIGIBLE} <= set(ranked.columns) else apply_shortlist(ranked, cfg)
    rank = _num(df, S.RANK)
    eligible = _bool_col(df, S.ELIGIBLE) & rank.notna().to_numpy()
    pool = eligible & _bool_col(df, S.HAS_RESUME, default=True)
    ids = pd.Series(_ids(df), index=df.index)

    if cfg.shortlist.method == "threshold":
        dist = (_num(df, S.FINAL_SCORE) - (_float(cfg.shortlist.threshold) or 0.0)).abs()
        near = pd.DataFrame({"dist": dist, "rank": rank})[pool]
        near = near.sort_values(["dist", "rank"], kind="mergesort").head(2 * band)
        return ids.loc[near.sort_values("rank", kind="mergesort").index].tolist()

    cutoff = _cutoff_rank(df, cfg, rank, eligible)
    window = pool & rank.between(cutoff - band + 1, cutoff + band).to_numpy()
    return ids[window].loc[rank[window].sort_values(kind="mergesort").index].tolist()


# ---------------------------------------------------------------------------
# Explanations
# ---------------------------------------------------------------------------
def _get(r: Mapping[str, Any], key: str) -> Any:
    v = r.get(key)
    return None if _isna(v) else v


def _pts(r: Mapping[str, Any], pts_col: str, sub_col: str, weight: float) -> float:
    pts = _float(_get(r, pts_col))
    if pts is None:
        pts = (_float(_get(r, sub_col)) or 0.0) * weight
    return pts


def _split(value: Any, sep: str) -> list[str]:
    return [p.strip() for p in _clean(value).split(sep.strip()) if p.strip()]


def explain_score(row: pd.Series | Mapping[str, Any], cfg: AppConfig) -> list[str]:
    """Human-readable score breakdown, e.g.::

        HackerRank: 100/100 (100%) -> 40.0 / 40 pts
        Skills: Python, SQL, Cloud matched (3/5) -> 30.0 / 30 pts
        Experience: 2 internships, 5 months, 3 projects, relevance 0.72 -> 15.4 / 20 pts
        Education: B.Tech CSE, CGPA 8.6, 1 cert, Tier 2 -> 7.9 / 10 pts
        Final score: 93.30 / 100
    """
    r: Mapping[str, Any] = row if isinstance(row, Mapping) else row.to_dict()
    w = cfg.weights.normalized()
    source = _clean(_get(r, S.EXPERIENCE_SOURCE))
    no_resume = source == S.EXP_NO_RESUME or (not source and not _as_bool(_get(r, S.HAS_RESUME)))
    rescaled = no_resume and cfg.shortlist.missing_resume_policy == "renormalize"
    absent = "resume unreadable" if _as_bool(_get(r, S.HAS_RESUME)) else "no resume"

    def tail(pts: float, weight: float) -> str:
        return f"-> {pts:.1f} / {_fmt_num(weight, 1)} pts"

    def without_resume(component: str, pts: float, weight: float) -> str:
        return (f"{component}: {absent} - excluded (score rescaled)" if rescaled
                else f"{component}: {absent} {tail(pts, weight)}")

    # HackerRank
    pts = _pts(r, S.PTS_HR, S.SUB_HR, w.hackerrank)
    score, max_score, pct = (_float(_get(r, c)) for c in (S.HR_SCORE, S.MAX_SCORE, S.HR_PCT))
    if pct is None and score is not None:
        pct = min(100.0, max(0.0, score / (max_score or 100.0) * 100.0))
    if pct is None:
        hr = f"HackerRank: no assessment score {tail(pts, w.hackerrank)}"
    elif score is None:
        hr = f"HackerRank: {_fmt_num(pct, 1)}% {tail(pts, w.hackerrank)}"
    else:
        hr = (f"HackerRank: {_fmt_num(score)}/{_fmt_num(max_score or 100.0)} ({_fmt_num(pct, 1)}%) "
              f"{tail(pts, w.hackerrank)}")
    lines = [hr]

    # Skills
    pts = _pts(r, S.PTS_SKILLS, S.SUB_SKILLS, w.skills)
    matched, missing = _split(_get(r, S.MATCHED_SKILLS), S.SEP_LIST), _split(_get(r, S.MISSING_SKILLS), S.SEP_LIST)
    total = len(matched) + len(missing)
    if no_resume:
        lines.append(without_resume("Skills", pts, w.skills))
    elif total == 0:
        lines.append(f"Skills: no required skills configured {tail(pts, w.skills)}")
    elif matched:
        lines.append(f"Skills: {', '.join(matched)} matched ({len(matched)}/{total}) {tail(pts, w.skills)}")
    else:
        lines.append(f"Skills: none of the {total} required skills matched {tail(pts, w.skills)}")

    # Experience
    pts = _pts(r, S.PTS_EXPERIENCE, S.SUB_EXPERIENCE, w.experience)
    if no_resume:
        lines.append(without_resume("Experience", pts, w.experience))
    else:
        parts = [
            _plural(_float(_get(r, S.INTERNSHIP_COUNT)) or 0, "internship"),
            _plural(_float(_get(r, S.EXPERIENCE_MONTHS)) or 0, "month"),
            _plural(_float(_get(r, S.PROJECT_COUNT)) or 0, "project"),
            f"relevance {(_float(_get(r, S.RELEVANCE)) or 0.0):.2f}",
        ]
        suffix = {S.EXP_HYBRID: " (blended with LLM review)", S.EXP_LLM: " (LLM review)",
                  S.EXP_LLM_FALLBACK: " (LLM unavailable - rules only)"}.get(source, "")
        lines.append(f"Experience: {', '.join(parts)} {tail(pts, w.experience)}{suffix}")

    # Education
    pts = _pts(r, S.PTS_EDUCATION, S.SUB_EDUCATION, w.education)
    degree = " ".join(x for x in (_clean(_get(r, S.DEGREE)), _clean(_get(r, S.BRANCH))) if x)
    parts = [degree or _clean(_get(r, S.RESUME_DEGREE))]
    cgpa = _float(_get(r, S.CGPA))
    cgpa = cgpa if cgpa is not None and 0 < cgpa <= 10 else _float(_get(r, S.RESUME_CGPA))
    if cgpa is not None:
        parts.append(f"CGPA {_fmt_num(cgpa)}")
    if not no_resume:
        parts.append(_plural(_float(_get(r, S.CERT_COUNT)) or 0, "cert"))
    tier = _tier_digit(_get(r, S.COLLEGE_TIER))
    if tier:
        parts.append(f"Tier {tier}")
    parts = [p for p in parts if p]
    lines.append(f"Education: {', '.join(parts) if parts else 'no data'} {tail(pts, w.education)}")

    final = _float(_get(r, S.FINAL_SCORE))
    if final is not None:
        lines.append(f"Final score: {final:.2f} / 100"
                     + (" (rescaled from the available components)" if rescaled else ""))
    notes = _clean(_get(r, S.SCORE_NOTES))
    if notes:
        lines.append(f"Notes: {notes}")
    rationale = _clean(_get(r, S.LLM_RATIONALE))
    if rationale:
        lines.append(f"LLM rationale: {rationale}")
    return lines
