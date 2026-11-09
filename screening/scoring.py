"""Rubric scoring, eligibility, shortlist statuses, tie-breaks and score explanations.

Points out of 100, weights from ``cfg.weights.normalized()`` (defaults shown)::

    HackerRank   40   hackerrank_pct / 100 x 40
    Skills       30   per_skill: 10 pts per required skill found, full marks at 3
    Experience   20   internships, duration, projects, JD relevance (rules only)
    Education    10   degree relevance, CGPA, certifications (+ college tier when enabled)

Each component has a 0..1 sub-score (``sub_*``) and points (``pts_*`` = sub x weight).
Scoring is fully rule-based: an LLM may explain results but never changes scores, ranks or
cutoffs. ``apply_shortlist`` turns scores into structured eligibility, a deterministic rank
(configurable tie-breaks) and a status with a human-readable reason; ``explain_score`` renders
the breakdown of one candidate.
"""
from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import Any

import numpy as np
import pandas as pd
from rapidfuzz import fuzz
from rapidfuzz import process as rf_process
from sklearn.feature_extraction.text import TfidfVectorizer

from . import schema as S
from . import skills
from .config import TIE_BREAK_KEYS, AppConfig, EducationConfig, ShortlistConfig
from .models import ParsedResume

TEST_SLOT = S.TEST_SLOT

# Final column order (docs/ARCHITECTURE.md). q{n}_hash / q{n}_score follow TEST_SLOT;
# unknown columns and then extra_* go last.
_HEAD_COLUMNS: list[str] = [
    S.RANK, S.STATUS, S.STATUS_REASON, S.CANDIDATE_ID, S.NAME, S.JOB_APPLICATION, S.EMAIL, S.PHONE,
    S.COLLEGE, S.COLLEGE_TIER, S.TIER_SOURCE, S.DEGREE, S.BRANCH, S.GRAD_YEAR, S.CGPA, S.CGPA_SOURCE,
    S.CITY, S.FINAL_SCORE,
    S.PTS_HR, S.PTS_SKILLS, S.PTS_EXPERIENCE, S.PTS_EDUCATION,
    S.SUB_HR, S.SUB_SKILLS, S.SUB_EXPERIENCE, S.SUB_EDUCATION,
    S.HR_SCORE, S.MAX_SCORE, S.HR_PCT, S.CUTOFF_SCORE, S.CUTOFF_PCT, S.CANDIDATE_OPTION,
    S.ASSESSMENT_COMMENT,
    S.RISK_LEVEL, S.RISK_SCORE, S.RISK_REASONS, S.CLUSTER_ID, S.IS_FOCUS, S.N_LINKS,
    S.MAX_PAIR_SCORE, S.INDIVIDUAL_SCORE, S.INJECTION_FLAGS,
    S.DUPLICATE_OF, S.DUPLICATE_IDS, S.DUPLICATE_EVIDENCE,
    S.MATCHED_SKILLS, S.MISSING_SKILLS, S.N_MATCHED_SKILLS, S.SKILLS_FOUND,
    S.INTERNSHIP_COUNT, S.EXPERIENCE_MONTHS, S.PROJECT_COUNT, S.CERTIFICATIONS, S.CERT_COUNT,
    S.RESUME_CGPA, S.RESUME_DEGREE, S.RELEVANCE, S.EXPERIENCE_SOURCE, S.LLM_RATIONALE,
    S.SCORE_NOTES, S.TIE_BREAK_NOTE,
    S.TEST_START, S.SUBMISSION_TIME, S.COMPLETION_MIN, S.IP_ADDRESS, S.IP_SUBNET, S.TEST_MODE,
    S.TAB_SWITCHES, S.COPY_PASTE, S.PLAGIARISM_FLAG, S.SOURCE, S.EMAIL_DOMAIN, S.TEST_SLOT,
]
_TAIL_COLUMNS: list[str] = [
    S.RESUME_FILE, S.RESUME_HASH, S.HAS_RESUME, S.MATCH_METHOD, S.MATCH_CONFIDENCE,
    S.ELIGIBLE, S.INELIGIBILITY, S.COLLEGE_CAPPED, S.DATA_FLAGS,
]
_Q_SCORE_RE = re.compile(r"^q(\d+)_score$")

SHORTLIST_METHODS = ("top_n", "top_pct", "threshold")
IN_SHORTLIST = (S.STATUS_SHORTLISTED, S.STATUS_REVIEW)
RUN_MODES = ("ta+resumes", "ta_only", "resumes_only")
CGPA_FROM_FEED = "feed"
CGPA_FROM_RESUME = "resume"
TIER_FROM_FEED = "feed"
TIER_FROM_LIST = "tier list"
CANDIDATE_ID_LABEL = "Candidate ID"
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


def _fmt_pair(a: float, b: float, dp: int = 2, max_dp: int = 6) -> tuple[str, str]:
    """Two different numbers formatted with just enough decimals to tell them apart."""
    sa, sb = _fmt_num(a, dp), _fmt_num(b, dp)
    while sa == sb and a != b and dp < max_dp:
        dp += 1
        sa, sb = _fmt_num(a, dp), _fmt_num(b, dp)
    return sa, sb


def _fmt_fixed_pair(a: float, b: float, dp: int, max_dp: int = 4) -> tuple[str, str]:
    """Like _fmt_pair but keeps trailing zeros: (52.0, 60.0, 1) -> ('52.0', '60.0')."""
    sa, sb = f"{a:.{dp}f}", f"{b:.{dp}f}"
    while sa == sb and a != b and dp < max_dp:
        dp += 1
        sa, sb = f"{a:.{dp}f}", f"{b:.{dp}f}"
    return sa, sb


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


def _valid_cgpa(values: np.ndarray) -> np.ndarray:
    return np.where((values > 0) & (values <= 10), values, np.nan)


def _effective_cgpa(df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """(cgpa, from_resume): TA cgpa (valid 0 < v <= 10) preferred, else resume_cgpa."""
    ta = _valid_cgpa(_num(df, S.CGPA).to_numpy())
    res = _valid_cgpa(_num(df, S.RESUME_CGPA).to_numpy())
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


def resolve_missing_resume_policy(cfg: AppConfig, mode: str | None = None) -> str:
    """Effective missing-resume policy: "zero" or "renormalize".

    "auto" renormalizes only when the run has no resumes at all (mode "ta_only"); otherwise a
    missing resume scores 0 for skills and experience (no boost for a missing CV).
    """
    policy = _clean(cfg.shortlist.missing_resume_policy).lower()
    if policy == "auto":
        return "renormalize" if mode == "ta_only" else "zero"
    return policy if policy in {"zero", "renormalize"} else "zero"


# ---------------------------------------------------------------------------
# College tier (optional: cfg.education.tier_enabled)
# ---------------------------------------------------------------------------
_TIER_NORM_RE = re.compile(r"[^0-9a-z]+")
_ACRONYM_SKIP = frozenset({"of", "and", "the"})
_ACRONYM_MAX_TRAILING = 2   # words allowed after the acronym's last letter (e.g. a campus city)
# Words that say nothing about *which* institution a name is; a college made only of these is
# never fuzzy-matched ("Institute of Technology" would otherwise match every such name).
_GENERIC_COLLEGE_WORDS = frozenset({
    "of", "and", "the", "for", "in", "at", "institute", "institution", "university", "college",
    "school", "technology", "technological", "engineering", "science", "sciences", "management",
    "national", "indian", "india", "academy", "polytechnic", "studies", "research", "education",
    "deemed", "campus", "faculty", "department", "centre", "center", "information",
    "communication", "applied", "advanced", "international", "government", "govt", "state",
    "central", "private", "autonomous", "women", "womens", "tech", "technical", "engg", "univ",
    "inst", "coll", "arts", "commerce",
})


def _fold_text(value: Any) -> str:
    decomposed = unicodedata.normalize("NFKD", _clean(value))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def tier_list_key(value: Any) -> str:
    """College name as compared with the tier list: 'Q.Z.-IICT, Northfield' -> 'q z iict northfield'.

    Case-folded, accents removed, '&' read as 'and', punctuation -> spaces. Use it to
    normalise the keys of ``EducationConfig.tier_mapping``.
    """
    text = _fold_text(value).replace("&", " and ")
    return " ".join(t for t in _TIER_NORM_RE.split(text) if t)


def _acronym_forms(key: str) -> list[tuple[str, str | None]]:
    """Ways to read a short name as an acronym: (letters, word that must follow or None).

    'qz iict' / 'qziict' -> ('qziict', None); 'nit rivertown' -> also ('nit', 'rivertown') - an
    acronym plus a campus word. Ordinary (3+ word) names have none.
    """
    tokens = key.split()
    forms: list[tuple[str, str | None]] = []
    letters = "".join(tokens)
    if 1 <= len(tokens) <= 2 and letters.isalpha() and 3 <= len(letters) <= 12:
        forms.append((letters, None))
    if len(tokens) == 2 and tokens[0].isalpha() and 2 <= len(tokens[0]) <= 8 and len(tokens[1]) >= 3:
        forms.append((tokens[0], tokens[1]))
    return forms


def _acronym_matches(letters: str, words: list[str], then: str | None = None) -> bool:
    """True when ``letters`` are the word initials of ``words`` in order.

    "of" / "and" / "the" may be skipped (or used); up to _ACRONYM_MAX_TRAILING other words may
    follow the last matched word (a campus city) and must include ``then`` when given.
    'qziict' matches 'quartz zenith institute of information and communication technology';
    ('nit', 'rivertown') matches 'northfield institute of technology rivertown'.
    """
    k, m = len(letters), len(words)

    def match(i: int, j: int) -> bool:
        if i == k:
            trailing = [w for w in words[j:] if w not in _ACRONYM_SKIP]
            return len(trailing) <= _ACRONYM_MAX_TRAILING and (then is None or then in trailing)
        if j == m:
            return False
        word = words[j]
        if word[0] == letters[i] and match(i + 1, j + 1):
            return True
        return word in _ACRONYM_SKIP and match(i, j + 1)

    return k > 0 and match(0, 0)


def _initials(words: list[str]) -> set[str]:
    """First letters an acronym of ``words`` may start with (the first word, or the first word
    after leading "of/and/the")."""
    out = {words[0][0]} if words else set()
    first = next((w for w in words if w not in _ACRONYM_SKIP), "")
    if first:
        out.add(first[0])
    return out


class _TierIndex:
    """Lookup of colleges in an approved tier list (exact, acronym, then fuzzy)."""

    def __init__(self, items: tuple[tuple[str, str], ...], threshold: float) -> None:
        self.threshold = threshold
        self.exact: dict[str, str] = {}
        self.compact: dict[str, str] = {}
        self.keys: list[str] = []
        self.labels: list[str] = []
        # initial -> (letters, required following word, label) for acronym-like names in the list
        self.short_keys: dict[str, list[tuple[str, str | None, str]]] = defaultdict(list)
        self.long_keys: dict[str, list[tuple[list[str], str]]] = defaultdict(list)  # initial -> (words, label)
        self.by_token: dict[str, list[int]] = defaultdict(list)   # distinctive word -> key positions
        self._cache: dict[str, str | None] = {}
        for raw_key, raw_label in items:
            key, label = tier_list_key(raw_key), _clean(raw_label)
            if not key or not label or key in self.exact:
                continue
            self.exact[key] = label
            self.compact.setdefault(key.replace(" ", ""), label)
            for token in set(key.split()) - _GENERIC_COLLEGE_WORDS:
                self.by_token[token].append(len(self.keys))
            self.keys.append(key)
            self.labels.append(label)
            for letters, then in _acronym_forms(key):
                self.short_keys[letters[0]].append((letters, then, label))
            words = key.split()
            if len(words) >= 3:
                for initial in _initials(words):
                    self.long_keys[initial].append((words, label))

    def lookup(self, college: Any) -> str | None:
        key = tier_list_key(college)
        if not key or not college_key(college):
            return None
        if key not in self._cache:
            self._cache[key] = self._lookup(key)
        return self._cache[key]

    def _lookup(self, key: str) -> str | None:
        label = self.exact.get(key) or self.compact.get(key.replace(" ", ""))
        return label or self._acronym(key) or self._fuzzy(key)

    def _acronym(self, key: str) -> str | None:
        found: set[str] = set()
        for letters, then in _acronym_forms(key):   # acronym college vs long names in the list
            for words, label in self.long_keys.get(letters[0], ()):
                if _acronym_matches(letters, words, then):
                    found.add(label)
        words = key.split()
        if len(words) >= 3:   # long college vs acronyms in the list
            for initial in _initials(words):
                for letters, then, label in self.short_keys.get(initial, ()):
                    if _acronym_matches(letters, words, then):
                        found.add(label)
        return found.pop() if len(found) == 1 else None

    def _fuzzy(self, key: str) -> str | None:
        # Only names sharing a distinctive word can reach the threshold in practice; scoring just
        # those keeps a 2,000-name list fast. A college of generic words only is never matched.
        positions = sorted({p for t in set(key.split()) - _GENERIC_COLLEGE_WORDS for p in self.by_token.get(t, ())})
        if not positions:
            return None
        choices = [self.keys[p] for p in positions]
        hits = rf_process.extract(key, choices, scorer=fuzz.token_set_ratio,
                                  score_cutoff=self.threshold, limit=None)
        if not hits:
            return None
        best = max(h[1] for h in hits)
        top = [h for h in hits if h[1] == best]
        labels = {self.labels[positions[h[2]]] for h in top}
        if len(labels) == 1:
            return labels.pop()
        # several names tie with different tiers: prefer the closest overall spelling
        scored = [(fuzz.token_sort_ratio(key, h[0]), self.labels[positions[h[2]]]) for h in top]
        best_sort = max(s for s, _ in scored)
        labels = {label for s, label in scored if s == best_sort}
        return labels.pop() if len(labels) == 1 else None


@lru_cache(maxsize=8)
def _tier_index_cached(items: tuple[tuple[str, str], ...], threshold: float) -> _TierIndex:
    return _TierIndex(items, threshold)


def _tier_index(ed: EducationConfig) -> _TierIndex | None:
    mapping = ed.tier_mapping
    if not isinstance(mapping, Mapping) or not mapping:
        return None
    items = tuple((str(k), _clean(v)) for k, v in mapping.items())
    threshold = _float(ed.tier_match_threshold)
    threshold = 90.0 if threshold is None else min(100.0, max(0.0, threshold))
    return _tier_index_cached(items, threshold)


def _tier_maps(ed: EducationConfig) -> tuple[dict[str, float], dict[str, float], float]:
    """(label -> score, digit -> score, fallback score for an unlisted tier digit)."""
    labels: dict[str, float] = {}
    digits: dict[str, float] = {}
    for key, value in (ed.tier_scores or {}).items():
        v = _float(value)
        if v is None:
            continue
        v = min(1.0, max(0.0, v))
        labels.setdefault(_clean(key).casefold(), v)
        digit = re.search(r"\d", str(key))
        if digit:
            digits.setdefault(digit.group(), v)
    return labels, digits, (min(digits.values()) if digits else np.nan)


def _tier_digit(value: Any) -> str:
    m = re.search(r"\d", _clean(value))
    return m.group() if m else ""


def _tier_value(label: Any, maps: tuple[dict[str, float], dict[str, float], float]) -> float:
    """Tier sub-score 0..1 for a tier label ('1', 'Tier 2', ...); NaN when it cannot be scored."""
    text = _clean(label)
    if not text or text.casefold() in _UNKNOWN_COLLEGE_TOKENS:
        return np.nan
    by_label, by_digit, fallback = maps
    if text.casefold() in by_label:
        return by_label[text.casefold()]
    digit = _tier_digit(text)
    return by_digit.get(digit, fallback) if digit else np.nan


def _tier_label(label: Any) -> str:
    """Tier as stored in college_tier: the first digit ('Tier 1' -> '1'), else the label."""
    return _tier_digit(label) or _clean(label)


def resolve_tier(college: Any, cfg: AppConfig, feed_tier: Any = None) -> tuple[str | None, str]:
    """(tier label or None, source) for one college; source is "feed", "tier list" or "".

    The feed's tier wins when it can be scored; otherwise ``college`` is looked up in
    ``cfg.education.tier_mapping``: exact name/alias (case, spacing and punctuation ignored),
    acronym rule ('XY-IICT' matches a long name whose word initials give those letters in
    order, ignoring of/and/the), then rapidfuzz token_set_ratio >= tier_match_threshold
    (ambiguous matches with different tiers -> no tier). The tier-list label is returned as
    written in the list. Pure lookup: ``tier_enabled`` is applied by score_candidates.
    """
    ed = cfg.education
    if not np.isnan(_tier_value(feed_tier, _tier_maps(ed))):
        return _clean(feed_tier), TIER_FROM_FEED
    index = _tier_index(ed)
    label = index.lookup(college) if index is not None else None
    return (label, TIER_FROM_LIST) if label else (None, "")


def _tiers(df: pd.DataFrame, cfg: AppConfig) -> tuple[np.ndarray, list[str], list[str]]:
    """-> (tier sub-score or NaN, tier source, tier label) per row."""
    n = len(df)
    score = np.full(n, np.nan)
    source, label = [""] * n, [""] * n
    ed = cfg.education
    if not ed.tier_enabled:
        return score, source, label
    maps = _tier_maps(ed)
    index = _tier_index(ed)
    colleges = df[S.COLLEGE].tolist() if S.COLLEGE in df.columns else [None] * n
    for i, feed in enumerate(_str_list(df, S.COLLEGE_TIER)):
        value = _tier_value(feed, maps)
        if not np.isnan(value):
            score[i], source[i], label[i] = value, TIER_FROM_FEED, _tier_label(feed)
            continue
        listed = index.lookup(colleges[i]) if index is not None else None
        value = _tier_value(listed, maps) if listed else np.nan
        if not np.isnan(value):
            score[i], source[i], label[i] = value, TIER_FROM_LIST, _tier_label(listed)
    return score, source, label


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


@lru_cache(maxsize=4096)
def _degree_relevance(text: str) -> float:
    if not text:
        return np.nan
    for score, pattern in _DEGREE_TIERS:
        if pattern.search(text):
            return score
    return _DEGREE_OTHER


@dataclass
class _Education:
    sub: np.ndarray                # 0..1
    available: np.ndarray          # any education sub-component had data
    cgpa_source: list[str]         # CGPA_FROM_FEED | CGPA_FROM_RESUME | ""
    tier_source: list[str]         # TIER_FROM_FEED | TIER_FROM_LIST | ""
    tier_label: list[str]          # tier used for scoring ("" when none)


def _education(df: pd.DataFrame, f: _ResumeFeatures, cfg: AppConfig) -> _Education:
    """Weights re-normalised over the sub-components that have data (missing -> excluded, not
    zero). The tier sub-component counts only when ``tier_enabled``; an unmatched college has
    no tier sub-score (it is not penalised)."""
    ed = cfg.education
    degree_text = [
        " ".join(x for x in (d or rd, b or rb) if x)
        for d, b, rd, rb in zip(_str_list(df, S.DEGREE), _str_list(df, S.BRANCH),
                                f.degree_only, f.branch_only)
    ]
    c_degree = np.array([_degree_relevance(t) for t in degree_text], dtype=float)

    ta = _valid_cgpa(_num(df, S.CGPA).to_numpy())
    cgpa = np.where(np.isnan(ta), f.resume_cgpa, ta)
    from_resume = np.isnan(ta) & ~np.isnan(f.resume_cgpa)
    cgpa_source = [CGPA_FROM_FEED if not np.isnan(t) else (CGPA_FROM_RESUME if r else "")
                   for t, r in zip(ta, from_resume)]
    lo, hi = _float(ed.cgpa_floor) or 0.0, _float(ed.cgpa_ceiling) or 10.0
    if hi > lo:
        c_cgpa = np.clip((cgpa - lo) / (hi - lo), 0.0, 1.0)
    else:
        c_cgpa = np.where(np.isnan(cgpa), np.nan, (cgpa >= hi).astype(float))

    c_certs = np.where(f.usable, _ratio(f.relevant_certs, ed.certs_for_full), np.nan)
    c_tier, tier_source, tier_label = _tiers(df, cfg)

    comps = np.vstack([c_degree, c_cgpa, c_certs, c_tier])
    w = np.array([_float(ed.w_degree) or 0.0, _float(ed.w_cgpa) or 0.0,
                  _float(ed.w_certifications) or 0.0, (_float(ed.w_tier) or 0.0) if ed.tier_enabled else 0.0])
    w = np.clip(w, 0.0, None)[:, None]
    have = ~np.isnan(comps) & (w > 0)
    num = (np.where(have, comps, 0.0) * w).sum(axis=0)
    den = (have * w).sum(axis=0)
    sub = np.divide(num, den, out=np.zeros(len(df)), where=den > 0)
    return _Education(sub, den > 0, cgpa_source, tier_source, tier_label)


# ---------------------------------------------------------------------------
# Public API: scoring
# ---------------------------------------------------------------------------
def score_candidates(base: pd.DataFrame, parsed_by_candidate: Mapping[str, ParsedResume] | None,
                     cfg: AppConfig, *, mode: str = "ta+resumes") -> pd.DataFrame:
    """Return a NEW frame: ``base`` + resume-derived features + rubric sub-scores/points.

    mode: the run mode ("ta+resumes" | "ta_only" | "resumes_only"); it decides the "auto"
    missing-resume policy (renormalize only for "ta_only"). A linked resume without extractable
    text is treated as missing. Experience is rules-based (EXPERIENCE_SOURCE "heuristic" or
    "no resume"). Adds CGPA_SOURCE ("feed" | "resume" | "") and TIER_SOURCE ("feed" |
    "tier list" | ""); a tier found in the tier list is written to COLLEGE_TIER.
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
    sub_exp = _experience_heuristic(f, relevance, rel_available, cfg)
    edu = _education(out, f, cfg)

    pts_hr = sub_hr * weights.hackerrank
    pts_skills = sub_skills * weights.skills
    pts_exp = sub_exp * weights.experience
    pts_edu = edu.sub * weights.education
    final = pts_hr + pts_skills + pts_exp + pts_edu

    # "renormalize" only drops the resume components; a missing HackerRank score still counts 0.
    renormalize = resolve_missing_resume_policy(cfg, mode) == "renormalize"
    available = weights.hackerrank + weights.education * edu.available
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
        if edu.cgpa_source[i] == CGPA_FROM_RESUME:
            row.append("CGPA from resume")
        if not edu.available[i]:
            row.append("no education data")
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
    # resume-only runs: the candidate rows (and their CGPA) were built from the resumes
    out[S.CGPA_SOURCE] = ([CGPA_FROM_RESUME if s else "" for s in edu.cgpa_source] if mode == "resumes_only"
                          else edu.cgpa_source)
    out[S.TIER_SOURCE] = edu.tier_source
    if TIER_FROM_LIST in edu.tier_source:
        current = out[S.COLLEGE_TIER].tolist() if S.COLLEGE_TIER in out.columns else [None] * n
        out[S.COLLEGE_TIER] = [edu.tier_label[i] if edu.tier_source[i] == TIER_FROM_LIST else current[i]
                               for i in range(n)]
    out[S.SUB_HR] = np.round(sub_hr, 4)
    out[S.SUB_SKILLS] = np.round(sub_skills, 4)
    out[S.SUB_EXPERIENCE] = np.round(sub_exp, 4)
    out[S.SUB_EDUCATION] = np.round(edu.sub, 4)
    out[S.PTS_HR] = np.round(pts_hr, 2)
    out[S.PTS_SKILLS] = np.round(pts_skills, 2)
    out[S.PTS_EXPERIENCE] = np.round(pts_exp, 2)
    out[S.PTS_EDUCATION] = np.round(pts_edu, 2)
    out[S.FINAL_SCORE] = np.round(final, 2)
    out[S.RELEVANCE] = relevance
    out[S.EXPERIENCE_SOURCE] = [S.EXP_HEURISTIC if u else S.EXP_NO_RESUME for u in f.usable]
    out[S.LLM_RATIONALE] = [""] * n
    out[S.SCORE_NOTES] = notes
    return out


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------
def _rule(rule: str, candidate_value: Any, threshold_value: Any, explanation: str) -> dict[str, Any]:
    return {"rule_violated": rule, "candidate_value": candidate_value, "threshold_value": threshold_value,
            "explanation": explanation}


def _duplicate_flags(df: pd.DataFrame, ids: list[str]) -> list[str]:
    """DUPLICATE_OF per row ('' when not a duplicate or when it points at the row itself)."""
    return [d if d and d != cid else "" for d, cid in zip(_str_list(df, S.DUPLICATE_OF), ids)]


def _ineligibility(df: pd.DataFrame, ids: list[str], pct: np.ndarray, sl: ShortlistConfig) -> list[list[dict[str, Any]]]:
    """Structured ineligibility rules per row (empty list = eligible).

    Resume-only data (no HackerRank scores at all) skips the assessment rules. A missing
    CGPA / graduation year never knocks a candidate out.
    """
    n = len(df)
    rules: list[list[dict[str, Any]]] = [[] for _ in range(n)]
    resume_only = n > 0 and bool(np.isnan(pct).all())
    if not resume_only:
        min_pct = _float(sl.min_hackerrank_pct) or 0.0
        for i in range(n):
            if np.isnan(pct[i]):
                rules[i].append(_rule(S.RULE_NO_SCORE, None, None, "No assessment score recorded on HackerRank."))
            elif min_pct > 0 and pct[i] < min_pct:
                have, need = _fmt_fixed_pair(float(pct[i]), min_pct, 1)
                rules[i].append(_rule(S.RULE_MIN_SCORE, round(float(pct[i]), 2), round(min_pct, 2),
                                      f"HackerRank score {have}% is below the required minimum of {need}%."))
    min_cgpa = _float(sl.min_cgpa) or 0.0
    if min_cgpa > 0:
        cgpa, from_resume = _effective_cgpa(df)
        if S.CGPA_SOURCE in df.columns:   # recorded by score_candidates (also right for resume-only runs)
            from_resume = np.array([s == CGPA_FROM_RESUME for s in _str_list(df, S.CGPA_SOURCE)], dtype=bool)
        for i in np.flatnonzero(cgpa < min_cgpa):
            have, need = _fmt_fixed_pair(float(cgpa[i]), min_cgpa, 2)
            src = " (from resume)" if from_resume[i] else ""
            rules[i].append(_rule(S.RULE_MIN_CGPA, round(float(cgpa[i]), 2), round(min_cgpa, 2),
                                  f"CGPA {have}{src} is below the minimum threshold of {need}."))
    allowed = sorted({int(y) for y in map(_float, sl.allowed_grad_years or []) if y is not None})
    if allowed:
        years = _num(df, S.GRAD_YEAR).to_numpy()
        listed = ", ".join(map(str, allowed))
        for i in np.flatnonzero(~np.isnan(years) & ~np.isin(years, allowed)):
            year = int(years[i])
            rules[i].append(_rule(S.RULE_GRAD_YEAR, year, list(allowed),
                                  f"Graduation year {year} is not within allowed batches ({listed})."))
    excluded = {_clean(x).casefold() for x in sl.exclude_candidates or [] if _clean(x)}
    if excluded:
        for i, cid in enumerate(ids):
            if cid.casefold() in excluded:
                rules[i].append(_rule(S.RULE_EXCLUDED, cid, None, "Disqualified: administrative exclusion by recruiter."))
    if sl.auto_disqualify_duplicates:
        dup_of = _duplicate_flags(df, ids)
        if any(dup_of):
            emails: dict[str, str] = {}
            for cid, email in zip(ids, _str_list(df, S.EMAIL)):
                if email and cid not in emails:
                    emails[cid] = email
            for i, primary in enumerate(dup_of):
                if not primary:
                    continue
                email = emails.get(primary)
                merged = f" (merged into official profile {email})" if email else ""
                rules[i].append(_rule(S.RULE_DUPLICATE, primary, None,
                                      f"Disqualified: Duplicate application of {primary}{merged}."))
    return rules


# ---------------------------------------------------------------------------
# Tie-breaks
# ---------------------------------------------------------------------------
def _tie_break_keys(sl: ShortlistConfig) -> list[str]:
    """Configured tie-break keys (unknown / repeated keys dropped); candidate_id is implicit."""
    out: list[str] = []
    for key in sl.tie_break_order or []:
        k = _clean(key)
        if k in TIE_BREAK_KEYS and k not in out:
            out.append(k)
    return out


def _coding_marks(df: pd.DataFrame) -> np.ndarray:
    """Sum of q{n}_score over coding questions (those with a q{n}_similarity column - code
    similarity exists only for coding answers); all q{n}_score when no similarity columns."""
    scores = S.question_score_columns(df.columns)
    if not scores:
        return np.full(len(df), np.nan)
    coding = {int(S.QUESTION_SIMILARITY_RE.match(c).group(1)) for c in S.question_similarity_columns(df.columns)}
    cols = [c for c in scores if int(S.QUESTION_SCORE_RE.match(c).group(1)) in coding] or scores
    mat = np.column_stack([_num(df, c).to_numpy() for c in cols])
    total = np.nansum(mat, axis=1)
    return np.where(np.isnan(mat).all(axis=1), np.nan, total)


def _submission_seconds(df: pd.DataFrame) -> np.ndarray:
    """Submission time as seconds since the epoch (UTC; NaN when missing / unparseable)."""
    if S.SUBMISSION_TIME not in df.columns:
        return np.full(len(df), np.nan)
    col = df[S.SUBMISSION_TIME]
    try:
        ts = pd.to_datetime(col, errors="coerce", utc=True, format="mixed")
    except (TypeError, ValueError):
        try:
            ts = pd.to_datetime(col.astype("str"), errors="coerce", utc=True, format="mixed")
        except (TypeError, ValueError):
            return np.full(len(df), np.nan)
    secs = (ts - pd.Timestamp("1970-01-01", tz="UTC")) / pd.Timedelta(seconds=1)
    return pd.Series(secs, index=df.index).to_numpy(dtype=float, na_value=np.nan)


def _tie_values(df: pd.DataFrame, key: str, pct: np.ndarray) -> np.ndarray:
    if key == "hackerrank_pct":
        values = pct
    elif key == "coding_marks":
        values = _coding_marks(df)
    elif key == "sub_skills":
        values = _num(df, S.SUB_SKILLS).to_numpy()
    elif key == "sub_experience":
        values = _num(df, S.SUB_EXPERIENCE).to_numpy()
    elif key == "cgpa":
        values = _effective_cgpa(df)[0]
    elif key == "submission_time":
        return _submission_seconds(df)
    else:  # pragma: no cover - keys are validated by _tie_break_keys
        return np.full(len(df), np.nan)
    return np.round(np.asarray(values, dtype=float), 4)   # float noise must not break a tie


def _display_time(value: Any) -> str:
    if isinstance(value, (pd.Timestamp, datetime)) and not _isna(value):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return _clean(value) or "n/a"


@dataclass
class _TieDecision:
    label: str     # deciding rule ("" = identical on every rule)
    winner: str    # deciding value of the higher-ranked candidate
    loser: str     # ... of the lower-ranked candidate


def _decide_tie(a: int, b: int, keys: list[str], values: dict[str, np.ndarray], ids: list[str],
                raw_times: list[Any]) -> _TieDecision:
    """Which tie-break rule ranks row ``a`` above row ``b`` (rows tied on final score)."""
    for key in keys:
        va, vb = float(values[key][a]), float(values[key][b])
        na, nb = math.isnan(va), math.isnan(vb)
        if (na and nb) or (not na and not nb and va == vb):
            continue
        label = TIE_BREAK_KEYS[key][0]
        if key == "submission_time":
            return _TieDecision(label, "n/a" if na else _display_time(raw_times[a]),
                                "n/a" if nb else _display_time(raw_times[b]))
        if na or nb:
            sa, sb = ("n/a" if na else _fmt_num(va, 4)), ("n/a" if nb else _fmt_num(vb, 4))
        else:
            sa, sb = _fmt_pair(va, vb)
        if key == "hackerrank_pct":
            sa, sb = (s if s == "n/a" else f"{s}%" for s in (sa, sb))
        return _TieDecision(label, sa, sb)
    if ids[a] != ids[b]:
        return _TieDecision(CANDIDATE_ID_LABEL, ids[a], ids[b])
    return _TieDecision("", "", "")


def _won(d: _TieDecision) -> str:
    return (f"won tie-break on {d.label} {d.winner} vs {d.loser}" if d.label
            else "tie unresolved (identical on every tie-break rule)")


def _lost(d: _TieDecision) -> str:
    return (f"lost tie-break on {d.label} {d.loser} vs {d.winner}" if d.label
            else "tie unresolved (identical on every tie-break rule)")


def _tied_at(score: float) -> str:
    return f"tied at {score:.2f} pts"


# ---------------------------------------------------------------------------
# Shortlist
# ---------------------------------------------------------------------------
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

    Eligibility: INELIGIBILITY is a JSON list of {"rule_violated", "candidate_value",
    "threshold_value", "explanation"} (rules S.RULE_*: no assessment score, score below the
    minimum, CGPA below the minimum, graduation year not allowed, administrative exclusion
    (``exclude_candidates``, case-insensitive IDs), duplicate application (rows with
    DUPLICATE_OF when ``auto_disqualify_duplicates``)); STATUS_REASON of an ineligible
    candidate = the explanations joined. Resume-only data (no HackerRank scores at all) skips
    the assessment rules; a missing CGPA / graduation year never knocks a candidate out.

    Rank (1..n, eligible only): final_score desc, then ``shortlist.tie_break_order`` (see
    config.TIE_BREAK_KEYS), then candidate_id asc. Candidates whose final score ties with
    another's get TIE_BREAK_NOTE ("tied at 91.20 pts; won tie-break on HackerRank percentage
    100% vs 95%"); at the shortlist cutoff the note is also in both status reasons.
    Risk columns are optional (missing -> Clear). With exclude_high_risk, High-risk
    candidates in the band go to Integrity Hold and top_n / top_pct slots are backfilled
    (a threshold shortlist is never extended below the threshold).
    College cap (``shortlist.college_cap_pct`` > 0): one college may hold at most
    max(1, floor(pct% x shortlist slots)) places (Shortlisted + Integrity Review; holds do not
    count). Candidates beyond that are Not Shortlisted with COLLEGE_CAPPED True; top_n /
    top_pct backfill with the next-best candidates, threshold does not. Candidates without a
    recorded college are never capped. COLLEGE_CAPPED is False everywhere when the cap is off.
    The primary application of a disqualified duplicate group gets " | merged duplicate
    application <ids>" appended to its status reason.
    Returns a new frame sorted by rank (ineligible last), index reset, columns in final order.
    """
    out = df.copy()
    sl = cfg.shortlist
    n = len(out)
    ids = _ids(out)
    pct = _hr_pct(out).to_numpy()
    final = np.round(_num(out, S.FINAL_SCORE).fillna(0.0).to_numpy(), 2)
    rules = _ineligibility(out, ids, pct, sl)
    eligible = np.array([not r for r in rules], dtype=bool)

    tb_keys = _tie_break_keys(sl)
    tb_values = {k: _tie_values(out, k, pct) for k in tb_keys}
    keys = pd.DataFrame({"final": final, **{f"tb_{k}": v for k, v in tb_values.items()}, "cid": ids})
    sort_cols = ["final"] + [f"tb_{k}" for k in tb_keys] + ["cid"]
    ascending = [False] + [not TIE_BREAK_KEYS[k][1] for k in tb_keys] + [True]
    order = keys.iloc[np.flatnonzero(eligible)].sort_values(
        sort_cols, ascending=ascending, na_position="last", kind="mergesort").index.to_numpy()
    m = len(order)

    # tie-breaks between neighbours with the same final score
    raw_times = out[S.SUBMISSION_TIME].tolist() if S.SUBMISSION_TIME in out.columns else [None] * n
    ties: list[_TieDecision | None] = [
        _decide_tie(order[k], order[k + 1], tb_keys, tb_values, ids, raw_times)
        if final[order[k]] == final[order[k + 1]] else None
        for k in range(m - 1)
    ]
    tie_note = [""] * n
    for k, i in enumerate(order):
        parts = []
        if k > 0 and ties[k - 1] is not None:
            parts.append(_lost(ties[k - 1]))
        if k < m - 1 and ties[k] is not None:
            parts.append(_won(ties[k]))
        if parts:
            tie_note[i] = S.SEP_NOTES.join([_tied_at(final[i])] + parts)

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

    # a tie across the cutoff: the last candidate in the band vs the first one outside it
    cut_last, cut_note = -1, ("", "")
    band_pos = np.flatnonzero(in_band)
    if headcount and len(band_pos) and band_pos[-1] + 1 < m and ties[band_pos[-1]] is not None:
        cut_last = int(band_pos[-1])
        decision, score = ties[cut_last], final[order[cut_last]]
        cut_note = (f"{_tied_at(score)}; {_won(decision)}", f"{_tied_at(score)}; {_lost(decision)}")

    rank = np.full(n, -1, dtype=np.int64)
    status = [S.STATUS_INELIGIBLE] * n
    status_reason = [" ".join(rule["explanation"] for rule in r) for r in rules]
    college_capped = np.zeros(n, dtype=bool)
    for k, i in enumerate(order):
        r = k + 1
        rank[i] = r
        level = levels[i]
        if not in_band[k]:
            status[i] = S.STATUS_NOT
            if headcount:
                extra = f"; {cut_note[1]}" if k == cut_last + 1 and cut_last >= 0 else ""
                status_reason[i] = f"Rank {r:,} of {m:,} (cutoff: {cut_desc}{extra})"
            else:
                status_reason[i] = (f"Rank {r:,} of {m:,}; score {final[i]:.2f} below threshold "
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
            if k == cut_last:
                status_reason[i] += f" ({cut_note[0]})"
        else:
            status[i] = S.STATUS_SHORTLISTED
            parts = [cut_desc]
            if headcount and r > slots and (n_holds or n_capped):
                parts.append(f"backfill for {_backfill_note(n_holds, n_capped)}")
            if k == cut_last:
                parts = (parts if len(parts) > 1 else []) + [cut_note[0]]
            status_reason[i] = f"Rank {r:,} of {m:,} ({S.SEP_NOTES.join(parts)})"

    if sl.auto_disqualify_duplicates:
        dup_of = _duplicate_flags(out, ids)
        for i, listed in enumerate(_str_list(out, S.DUPLICATE_IDS)):
            if listed and not dup_of[i]:
                plural = "s" if S.SEP_LIST.strip() in listed else ""
                status_reason[i] = f"{status_reason[i]} | merged duplicate application{plural} {listed}".lstrip(" |")

    out[S.ELIGIBLE] = eligible
    out[S.RANK] = pd.array([r if r > 0 else None for r in rank], dtype="Int64")
    out[S.STATUS] = status
    out[S.STATUS_REASON] = status_reason
    out[S.COLLEGE_CAPPED] = college_capped
    out[S.INELIGIBILITY] = [json.dumps(r, ensure_ascii=False) for r in rules]
    out[S.TIE_BREAK_NOTE] = tie_note

    ineligible = keys.iloc[np.flatnonzero(~eligible)].sort_values(
        ["final", "cid"], ascending=[False, True], kind="mergesort").index.to_numpy()
    out = out.iloc[np.concatenate([order, ineligible]).astype(np.int64)].reset_index(drop=True)
    return order_columns(out)


def ineligibility_rules(value: Any) -> list[dict[str, Any]]:
    """Parse an INELIGIBILITY cell back into its list of rule dicts ([] when blank/invalid)."""
    text = _clean(value)
    if not text:
        return []
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def _cutoff_rank(df: pd.DataFrame, cfg: AppConfig, rank: pd.Series, eligible: np.ndarray) -> int:
    """Rank of the last shortlisted candidate (0 when nobody is shortlisted)."""
    if S.STATUS in df.columns:
        listed = eligible & np.isin(np.array(_str_list(df, S.STATUS), dtype=object), IN_SHORTLIST)
        return int(rank[listed].max()) if listed.any() else 0
    method = cfg.shortlist.method if cfg.shortlist.method in SHORTLIST_METHODS else "top_n"
    final_sorted = np.sort(_num(df, S.FINAL_SCORE).fillna(0.0).to_numpy()[eligible])[::-1]
    return _slots(method, cfg.shortlist, int(eligible.sum()), final_sorted)[0]


def cutoff_band_ids(ranked: pd.DataFrame, cfg: AppConfig) -> list[str]:
    """Borderline candidates: the first ``cfg.llm.borderline_band`` eligible candidates ranked
    just below the shortlist cutoff (Not Shortlisted, rank > the last shortlisted rank), by rank.

    Used to pick candidates for LLM explanations ("why not shortlisted") - the LLM only writes
    text; it never changes scores, ranks or the cutoff. Works for every shortlist method
    (threshold: the best-ranked candidates below the threshold); ``ranked`` may be unranked
    (apply_shortlist is run first).
    """
    band = int(_float(getattr(cfg.llm, "borderline_band", 0)) or 0)
    if ranked is None or ranked.empty or band <= 0:
        return []
    df = ranked if {S.RANK, S.ELIGIBLE} <= set(ranked.columns) else apply_shortlist(ranked, cfg)
    rank = _num(df, S.RANK)
    eligible = _bool_col(df, S.ELIGIBLE) & rank.notna().to_numpy()
    cutoff = _cutoff_rank(df, cfg, rank, eligible)
    below = eligible & (rank > cutoff).to_numpy()
    if S.STATUS in df.columns:
        below &= np.array(_str_list(df, S.STATUS), dtype=object) == S.STATUS_NOT
    ids = pd.Series(_ids(df), index=df.index)
    picked = rank[below].sort_values(kind="mergesort").head(band)
    return ids.loc[picked.index].tolist()


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


def _was_rescaled(r: Mapping[str, Any], cfg: AppConfig, mode: str | None) -> bool:
    policy = _clean(cfg.shortlist.missing_resume_policy).lower()
    if policy == "auto" and mode is None:   # infer from the row's own score notes
        return "final rescaled to 100" in _clean(_get(r, S.SCORE_NOTES))
    return resolve_missing_resume_policy(cfg, mode) == "renormalize"


def explain_score(row: pd.Series | Mapping[str, Any], cfg: AppConfig, *, mode: str | None = None) -> list[str]:
    """Human-readable score breakdown, e.g.::

        HackerRank: 100/100 (100%) -> 40.0 / 40 pts
        Skills: Python, SQL, Cloud matched (3/5) -> 30.0 / 30 pts
        Experience: 2 internships, 5 months, 3 projects, relevance 0.72 -> 15.4 / 20 pts
        Education: B.Tech CSE, CGPA 8.6, 1 cert -> 7.9 / 10 pts
        Final score: 93.30 / 100

    mode: the run mode (PipelineResult.mode) for the "auto" missing-resume policy; when it is
    not given, the row's score notes tell whether its score was rescaled. The college tier is
    listed only when it was used for scoring (TIER_SOURCE), with "(tier list)" when matched
    from the approved tier list. A TIE_BREAK_NOTE adds a "Tie-break:" line.
    """
    r: Mapping[str, Any] = row if isinstance(row, Mapping) else row.to_dict()
    w = cfg.weights.normalized()
    source = _clean(_get(r, S.EXPERIENCE_SOURCE))
    no_resume = source == S.EXP_NO_RESUME or (not source and not _as_bool(_get(r, S.HAS_RESUME)))
    rescaled = no_resume and _was_rescaled(r, cfg, mode)
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
        lines.append(f"Experience: {', '.join(parts)} {tail(pts, w.experience)}")

    # Education
    pts = _pts(r, S.PTS_EDUCATION, S.SUB_EDUCATION, w.education)
    degree = " ".join(x for x in (_clean(_get(r, S.DEGREE)), _clean(_get(r, S.BRANCH))) if x)
    parts = [degree or _clean(_get(r, S.RESUME_DEGREE))]
    cgpa = _float(_get(r, S.CGPA))
    missing_in_feed = not (cgpa is not None and 0 < cgpa <= 10)
    cgpa = _float(_get(r, S.RESUME_CGPA)) if missing_in_feed else cgpa
    cgpa_source = _clean(_get(r, S.CGPA_SOURCE)) if S.CGPA_SOURCE in r else ""
    from_resume = cgpa_source == CGPA_FROM_RESUME if cgpa_source else missing_in_feed
    if cgpa is not None:
        parts.append(f"CGPA {_fmt_num(cgpa)}" + (" (from resume)" if from_resume else ""))
    if not no_resume:
        parts.append(_plural(_float(_get(r, S.CERT_COUNT)) or 0, "cert"))
    if S.TIER_SOURCE in r:
        tier_source = _clean(_get(r, S.TIER_SOURCE))
    else:  # rows scored before TIER_SOURCE existed
        tier_source = TIER_FROM_FEED if cfg.education.tier_enabled else ""
    tier = _tier_label(_get(r, S.COLLEGE_TIER)) if tier_source else ""
    if tier:
        parts.append(f"Tier {tier}" + (" (tier list)" if tier_source == TIER_FROM_LIST else ""))
    parts = [p for p in parts if p]
    lines.append(f"Education: {', '.join(parts) if parts else 'no data'} {tail(pts, w.education)}")

    final = _float(_get(r, S.FINAL_SCORE))
    if final is not None:
        lines.append(f"Final score: {final:.2f} / 100"
                     + (" (rescaled from the available components)" if rescaled else ""))
    notes = _clean(_get(r, S.SCORE_NOTES))
    if notes:
        lines.append(f"Notes: {notes}")
    tie = _clean(_get(r, S.TIE_BREAK_NOTE))
    if tie:
        lines.append(f"Tie-break: {tie}")
    rationale = _clean(_get(r, S.LLM_RATIONALE))
    if rationale:
        lines.append(f"LLM rationale: {rationale}")
    return lines
