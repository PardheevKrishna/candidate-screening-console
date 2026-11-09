"""Duplicate applications: one person behind several candidate IDs.

Workday exports one row per job *application*. A student who applies twice - typically once
with a personal webmail address and once with the university address - shows up as two
candidates with different IDs but the same name, the same college and the same resume.

``detect_duplicates`` finds such rows. Same person =

* the same normalised name (case-folded, accents removed, tokens sorted; the name inside
  "Job Application" - "First Last (first last)" - is used when there is no Name value), AND
* the same canonical college (case / whitespace / punctuation-insensitive; a college that is
  not recorded never matches), AND
* an identical resume file, OR resume texts with TF-IDF cosine >= 0.95, OR - when neither
  application has a resume - the same CGPA and the same branch,

with different candidate IDs. Groups of two or more applications are supported. Every group is
marked on the returned frame:

* ``duplicate_of``        on each duplicate: candidate_id of the group's primary application
* ``duplicate_ids``       on the primary: ", "-joined candidate_ids of its duplicates
* ``duplicate_evidence``  on every member: why the rows were judged to be the same person

Primary = an application with an assessment score, then one with an institutional (non-webmail)
email, then the lowest numeric candidate ID. Whether duplicates are disqualified is decided later
by ``scoring.apply_shortlist`` (``ShortlistConfig.auto_disqualify_duplicates``).

Safety: placeholder names ("N/A", "unknown") never match, and a group of more than
MAX_GROUP_SIZE applications is reported as a warning for review instead of being marked
(placeholder data must not mass-disqualify candidates).

Rows are grouped by (name, college) first and each group is compared with vectorised
operations, so 3,000 rows take well under a second. Nothing here raises on odd values; issues
name candidate IDs only (no names or emails).
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import defaultdict
from collections.abc import Mapping
from typing import Any

import numpy as np
import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer

from . import schema as S
from .models import ParsedResume
from .scoring import college_key

STAGE = "duplicates"
TEXT_SIMILARITY_MIN = 0.95         # resume text cosine at or above this = the same resume
# More "applications from one person" than this points at placeholder data (e.g. one constant
# name for everyone), not at a real person: such groups are reported for review, never marked.
MAX_GROUP_SIZE = 10

# Personal mailbox providers; any other domain counts as institutional (university / employer).
WEBMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "icloud.com",
    "rediffmail.com", "protonmail.com", "proton.me", "aol.com", "zoho.com", "mail.com",
    "mail.example.com",   # reserved domain used for personal addresses in the synthetic sample
})
WEBMAIL_PREFIXES = ("yahoo.",)     # yahoo.com, yahoo.co.in, ...
DUPLICATE_COLUMNS = (S.DUPLICATE_OF, S.DUPLICATE_IDS, S.DUPLICATE_EVIDENCE)

EVIDENCE_NAME_COLLEGE = "same name and college"
EVIDENCE_FILE = "identical resume file"
EVIDENCE_PROFILE = "no resumes; same CGPA and branch"

_LINK_FILE, _LINK_TEXT, _LINK_PROFILE = "file", "text", "profile"
_PARENS_RE = re.compile(r"\([^()]*\)")
_NON_ALNUM_RE = re.compile(r"[^0-9a-z]+")
_DIGITS_RE = re.compile(r"\d+")


# ---------------------------------------------------------------------------
# Normalisation helpers (public: also useful for the UI and tests)
# ---------------------------------------------------------------------------
def _clean(value: Any) -> str:
    if value is None or isinstance(value, float) and math.isnan(value):
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def _fold(text: str) -> str:
    """Accents removed and case-folded: 'José ÁLVAREZ' -> 'jose alvarez'."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def name_key(name: Any, job_application: Any = None) -> str:
    """Normalised person name: 'MENON, Asha' -> 'asha menon'.

    Falls back to the name inside a Workday "Job Application" value when ``name`` is blank:
    'Asha Menon (asha menon)' -> 'asha menon'. Returns '' when no name is available.
    """
    text = _clean(name)
    if not college_key(text):   # blank or a placeholder ("N/A", "unknown", "-")
        text = _PARENS_RE.sub(" ", _clean(job_application))
    if not college_key(text):
        return ""
    tokens = [t for t in _NON_ALNUM_RE.split(_fold(text)) if t]
    return " ".join(sorted(tokens))


def college_identity(college: Any) -> str:
    """Canonical college for duplicate checks: 'XY-IICT' == 'xy iict' == 'XYIICT'.

    Case, whitespace and punctuation are ignored ('&' reads as 'and'); '' when the college is
    not recorded (blank or a placeholder such as 'N/A').
    """
    if not college_key(college):
        return ""
    return _NON_ALNUM_RE.sub("", _fold(_clean(college)).replace("&", " and "))


def email_domain(email: Any) -> str:
    text = _clean(email).lower()
    return text.rsplit("@", 1)[1].strip() if "@" in text else ""


def is_webmail_domain(domain: Any) -> bool:
    """True for a personal mailbox provider's domain (WEBMAIL_DOMAINS, or yahoo.*)."""
    text = _clean(domain).lower()
    return bool(text) and (text in WEBMAIL_DOMAINS or text.startswith(WEBMAIL_PREFIXES))


def is_webmail(email: Any) -> bool:
    """True for an address at a personal mailbox provider (see is_webmail_domain)."""
    return is_webmail_domain(email_domain(email))


def is_institutional(email: Any) -> bool:
    """True for an email whose domain is not a webmail provider (e.g. a university address)."""
    return bool(email_domain(email)) and not is_webmail(email)


def _numeric_id(cid: str) -> float:
    digits = "".join(_DIGITS_RE.findall(cid))
    return float(int(digits)) if digits else math.inf


def _id_order(cid: str) -> tuple[float, str]:
    return _numeric_id(cid), cid


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
def _column(df: pd.DataFrame, col: str) -> list[Any]:
    return df[col].tolist() if col in df.columns else [None] * len(df)


def _as_bool(value: Any) -> bool:
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    return _clean(value).lower() in {"true", "t", "yes", "y", "1"}


def _finite(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


class _TextSimilarity:
    """Cosine similarity between the resume texts of selected rows (one TF-IDF fit)."""

    _CHUNK = 256   # rows per block of the similarity product (bounded memory for large buckets)

    def __init__(self, texts: dict[int, str]) -> None:
        self._row: dict[int, int] = {}
        self._mat: Any = None
        docs = [(row, text) for row, text in texts.items() if text]
        if len(docs) < 2:
            return
        try:
            self._mat = TfidfVectorizer(sublinear_tf=True).fit_transform([t for _, t in docs])
        except ValueError:  # empty vocabulary (symbols / numbers only)
            return
        self._row = {row: k for k, (row, _) in enumerate(docs)}

    def pairs(self, rows: list[int], threshold: float) -> list[tuple[int, int, float]]:
        """(row a, row b, cosine) for every pair of ``rows`` with cosine >= threshold."""
        present = [r for r in rows if r in self._row]
        if self._mat is None or len(present) < 2:
            return []
        sub = self._mat[[self._row[r] for r in present]]
        out: list[tuple[int, int, float]] = []
        for start in range(0, len(present), self._CHUNK):
            block = (sub[start:start + self._CHUNK] @ sub.T).toarray()
            for i, j in zip(*np.nonzero(block >= threshold)):
                if start + i < j:
                    out.append((present[start + i], present[j], float(block[i, j])))
        return out


def _components(rows: list[int], edges: list[tuple[int, int, str, float | None]]) -> list[list[int]]:
    parent = {r: r for r in rows}

    def find(r: int) -> int:
        while parent[r] != r:
            parent[r] = parent[parent[r]]
            r = parent[r]
        return r

    for a, b, _, _ in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    groups: dict[int, list[int]] = defaultdict(list)
    for r in rows:
        groups[find(r)].append(r)
    return [g for g in groups.values() if len(g) > 1]


def _email_evidence(emails: list[str]) -> str:
    known = [e for e in emails if "@" in e]
    if len(known) < 2:
        return ""
    if len(set(known)) == 1:
        return "same email"
    if any(is_webmail(e) for e in known) and any(is_institutional(e) for e in known):
        return "emails differ (webmail vs institutional)"
    return "emails differ"


def _evidence(members: list[int], edges: list[tuple[int, int, str, float | None]],
              emails: Mapping[int, str]) -> str:
    kinds = {kind for _, _, kind, _ in edges}
    resume: list[str] = []
    if _LINK_FILE in kinds:
        resume.append(EVIDENCE_FILE)
    if _LINK_TEXT in kinds:
        low = min(s for _, _, kind, s in edges if kind == _LINK_TEXT and s is not None)
        resume.append(f"near-identical resume text (similarity {math.floor(low * 100) / 100:.2f})")
    if _LINK_PROFILE in kinds:
        resume.append(EVIDENCE_PROFILE)
    parts = [EVIDENCE_NAME_COLLEGE, " and ".join(resume), _email_evidence([emails[r] for r in members])]
    return "; ".join(p for p in parts if p)


def detect_duplicates(base: pd.DataFrame, parsed_by_candidate: Mapping[str, ParsedResume] | None = None
                      ) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """Mark duplicate applications (see module docstring).

    Returns (a NEW frame = ``base`` + duplicate_of / duplicate_ids / duplicate_evidence, issues).
    Rows that are not part of a duplicate group get "" in all three columns. One info issue
    per group (candidate IDs only). Never raises on odd values.
    """
    out = base.copy()
    n = len(out)
    dup_of, dup_ids, evidence = [""] * n, [""] * n, [""] * n
    issues: list[dict[str, Any]] = []
    if n >= 2 and S.CANDIDATE_ID in out.columns:
        for primary, others, text in _find_groups(out, parsed_by_candidate or {}):
            if len(others) + 1 > MAX_GROUP_SIZE:
                issues.append({
                    "severity": "warning", "stage": STAGE, "candidate_id": primary[1], "file_name": None,
                    "issue": (f"{len(others) + 1} applications share one name and college with matching "
                              f"evidence ({text}) - too many to be one person (placeholder data?); "
                              f"not marked as duplicates, please review."),
                })
                continue
            dup_ids[primary[0]] = S.SEP_LIST.join(cid for _, cid in others)
            for row, _ in others:
                dup_of[row] = primary[1]
            for row in [primary[0]] + [r for r, _ in others]:
                evidence[row] = text
            issues.append({
                "severity": "info", "stage": STAGE, "candidate_id": primary[1], "file_name": None,
                "issue": (f"Duplicate application: {len(others) + 1} applications from the same person - "
                          f"{primary[1]} kept as the primary application, "
                          f"{S.SEP_LIST.join(cid for _, cid in others)} marked as duplicate ({text})."),
            })
    out[S.DUPLICATE_OF] = dup_of
    out[S.DUPLICATE_IDS] = dup_ids
    out[S.DUPLICATE_EVIDENCE] = evidence
    return out, issues


def _find_groups(df: pd.DataFrame, parsed: Mapping[str, ParsedResume]
                 ) -> list[tuple[tuple[int, str], list[tuple[int, str]], str]]:
    """-> [((primary row, id), [(duplicate row, id), ...], evidence)] in row order of the primary."""
    ids = [_clean(v) for v in _column(df, S.CANDIDATE_ID)]
    names = [name_key(nm, app) for nm, app in zip(_column(df, S.NAME), _column(df, S.JOB_APPLICATION))]
    colleges = [college_identity(c) for c in _column(df, S.COLLEGE)]
    buckets: dict[tuple[str, str], list[int]] = defaultdict(list)
    for row in range(len(df)):
        if ids[row] and names[row] and colleges[row]:
            buckets[(names[row], colleges[row])].append(row)
    candidates = [rows for rows in buckets.values() if len({ids[r] for r in rows}) > 1]
    if not candidates:
        return []

    needed = sorted({r for rows in candidates for r in rows})
    hash_col, has_col = _column(df, S.RESUME_HASH), _column(df, S.HAS_RESUME)
    branch_col, cgpa_col, email_col = _column(df, S.BRANCH), _column(df, S.CGPA), _column(df, S.EMAIL)
    pct_col, score_col = _column(df, S.HR_PCT), _column(df, S.HR_SCORE)
    resume_hash: dict[int, str] = {}
    has_resume: dict[int, bool] = {}
    texts: dict[int, str] = {}
    cgpa: dict[int, float | None] = {}
    branch: dict[int, str] = {}
    emails: dict[int, str] = {}
    has_score: dict[int, bool] = {}
    for r in needed:
        p = parsed.get(ids[r])
        resume_hash[r] = _clean(hash_col[r]) or _clean(getattr(p, "file_hash", None))
        has_resume[r] = bool(resume_hash[r]) or p is not None or _as_bool(has_col[r])
        text = getattr(p, "text", None)
        texts[r] = text.strip() if isinstance(text, str) else ""
        value = _finite(cgpa_col[r])
        cgpa[r] = round(value, 2) if value is not None else None
        raw_branch = _clean(branch_col[r])
        branch[r] = _NON_ALNUM_RE.sub("", _fold(raw_branch).replace("&", " and ")) if college_key(raw_branch) else ""
        emails[r] = _clean(email_col[r]).lower()
        has_score[r] = _finite(pct_col[r]) is not None or _finite(score_col[r]) is not None
    similarity = _TextSimilarity({r: texts[r] for r in needed if has_resume[r]})

    def star(groups_by_key: dict[Any, list[int]], kind: str) -> list[tuple[int, int, str, float | None]]:
        """Link every member of a group to its first member (same evidence, linear work)."""
        return [(members[0], r, kind, None) for members in groups_by_key.values() for r in members[1:]
                if ids[r] != ids[members[0]]]

    def edges_of(rows: list[int]) -> list[tuple[int, int, str, float | None]]:
        same_file: dict[str, list[int]] = defaultdict(list)
        same_profile: dict[tuple[float, str], list[int]] = defaultdict(list)
        for r in rows:
            if resume_hash[r]:
                same_file[resume_hash[r]].append(r)
            if not has_resume[r] and cgpa[r] is not None and branch[r]:
                same_profile[(cgpa[r], branch[r])].append(r)
        edges = star(same_file, _LINK_FILE) + star(same_profile, _LINK_PROFILE)
        for a, b, sim in similarity.pairs([r for r in rows if has_resume[r]], TEXT_SIMILARITY_MIN):
            if ids[a] != ids[b] and not (resume_hash[a] and resume_hash[a] == resume_hash[b]):
                edges.append((a, b, _LINK_TEXT, sim))
        return edges

    def primary_rank(r: int) -> tuple[int, int, float, str]:
        return (0 if has_score[r] else 1, 0 if is_institutional(emails[r]) else 1, _numeric_id(ids[r]), ids[r])

    groups = []
    for rows in candidates:
        edges = edges_of(rows)
        for members in _components(rows, edges):
            if len({ids[r] for r in members}) < 2:
                continue
            inside = set(members)
            primary = min(members, key=primary_rank)
            others = sorted((r for r in members if r != primary and ids[r] != ids[primary]),
                            key=lambda r: _id_order(ids[r]))
            text = _evidence(members, [e for e in edges if e[0] in inside and e[1] in inside], emails)
            groups.append(((primary, ids[primary]), [(r, ids[r]) for r in others], text))
    groups.sort(key=lambda g: g[0][0])
    return groups
