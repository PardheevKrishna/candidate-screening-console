"""CSV / Excel / evidence-pack exports for recruiters and the TA team.

All exports neutralise spreadsheet formula injection: a text cell that begins with
= + - @ (or a tab / carriage return) is prefixed with an apostrophe, so a value such as
'=HYPERLINK(...)' smuggled into a TA feed or resume is shown as text and never evaluated
by Excel / Google Sheets. Plain signed numbers ("-5", "+919876543210") are left as is.

Columns are grouped by where their values come from (schema.COLUMN_SOURCE): the assessment
export (HackerRank), the resume, the academic profile and the integrity engine. The Excel
"Shortlist" and "All Candidates" sheets show that as a coloured super-header row; CSV files,
which have a single header row, use prefixed labels instead ("HackerRank: Score",
"Resume: Skills Pts", "Academic: CGPA", "Integrity: Risk Level") - see labelled_columns().

Integrity flags are written as review evidence ("pattern consistent with ..."), never as
verdicts. LLM output appears only as clearly labelled notes; the LLM never changes a score,
rank or cutoff.
"""
from __future__ import annotations

import io
import json
import math
import re
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd
import xlsxwriter

from . import schema as S
from .config import LLM_PROVIDERS, TIE_BREAK_KEYS, AppConfig
from .models import PipelineResult
from .scoring import IN_SHORTLIST, explain_score, order_columns

SHEET_NAMES = [
    "Summary", "Shortlist", "All Candidates", "Ineligible Candidates", "Integrity Evidence", "Risk Clusters",
    "Risk Pairs", "Commonality", "Data Quality", "Config",
]
BRAND_RED = "#D71E28"
HIGH_FILL, HIGH_FONT = "#F8D7DA", "#842029"
MEDIUM_FILL, MEDIUM_FONT = "#FFF3CD", "#664D03"
CLEAR_FILL, CLEAR_FONT = "#D1E7DD", "#0F5132"
DISCLAIMER = (
    "Integrity risk flags are evidence for human review, not findings of misconduct. A flag means "
    "the data shows a pattern consistent with shared work, collaboration or a shared test "
    "environment - patterns that can also have legitimate explanations (shared campus network, "
    "scheduled lab session, common resume templates, group projects). Verify with the candidate "
    "(live coding re-test, project walkthrough, proctored re-assessment) before any decision, and "
    "never present a flag to a candidate as a conclusion."
)

# ---------------------------------------------------------------------------
# Column groups (super-header row) and source prefixes (CSV labels)
# ---------------------------------------------------------------------------
GROUP_CORE = "Candidate & Outcome"
GROUP_LLM = "AI Notes (LLM)"
GROUP_OTHER = "Other"
GROUP_ORDER = (GROUP_CORE, S.SRC_ASSESSMENT, S.SRC_RESUME, S.SRC_ACADEMIC, S.SRC_INTEGRITY, GROUP_LLM, GROUP_OTHER)
SOURCE_PREFIX = {S.SRC_ASSESSMENT: "HackerRank", S.SRC_RESUME: "Resume", S.SRC_ACADEMIC: "Academic",
                 S.SRC_INTEGRITY: "Integrity"}
GROUP_STYLE: dict[str, tuple[str, str]] = {  # group -> (fill, font colour)
    GROUP_CORE: ("#333333", "#FFFFFF"),
    S.SRC_ASSESSMENT: ("#1F4E79", "#FFFFFF"),
    S.SRC_RESUME: ("#3B6E2B", "#FFFFFF"),
    S.SRC_ACADEMIC: ("#F1B521", "#1A1A1A"),
    S.SRC_INTEGRITY: ("#BA0C2F", "#FFFFFF"),
    GROUP_LLM: ("#5B3A8C", "#FFFFFF"),
    GROUP_OTHER: ("#7F7F7F", "#FFFFFF"),
}
# Identity and screening outcome: no single data source, shown first.
_CORE_COLUMNS = frozenset({
    S.RANK, S.STATUS, S.STATUS_REASON, S.CANDIDATE_ID, S.NAME, S.JOB_APPLICATION, S.EMAIL, S.PHONE, S.CITY,
    S.SOURCE, S.EMAIL_DOMAIN, S.FINAL_SCORE, S.ELIGIBLE, S.INELIGIBILITY, S.TIE_BREAK_NOTE, S.COLLEGE_CAPPED,
    S.SCORE_NOTES,
})
# Derived columns schema.COLUMN_SOURCE does not list, with the source they are derived from.
_EXTRA_SOURCE: dict[str, str] = {
    **{c: S.SRC_ASSESSMENT for c in (S.IP_SUBNET, S.IP_ADDRESSES, S.TEST_SLOT, S.PLAGIARISM_MATCH,
                                     S.PLAGIARISM_MATCH_ID, S.REPORT_URL)},
    **{c: S.SRC_RESUME for c in (S.RESUME_HASH, S.MATCH_METHOD, S.MATCH_CONFIDENCE, S.EXPERIENCE_SOURCE)},
    S.DUPLICATE_EVIDENCE: S.SRC_INTEGRITY,
}
# Labels used after a source prefix when stripping the prefix word would leave too little.
_PREFIXED_LABELS = {S.HR_PCT: "Percentage", S.PTS_HR: "Rubric Pts", S.SUB_HR: "Sub-score"}

# Ineligibility rules in display order (Ineligible Candidates sheet, Summary counts).
RULE_ORDER = (S.RULE_NO_SCORE, S.RULE_MIN_SCORE, S.RULE_MIN_CGPA, S.RULE_GRAD_YEAR, S.RULE_DUPLICATE,
              S.RULE_EXCLUDED)
OTHER_RULE = "Other"
INELIGIBLE_COLUMNS = [S.CANDIDATE_ID, S.NAME, "rule", "candidate_value", "threshold_value", "explanation"]
# Fallback for runs without the ineligibility column: status-reason text -> rule.
_REASON_RULES = [
    (re.compile(r"^no (?:hackerrank|assessment) score", re.I), S.RULE_NO_SCORE),
    (re.compile(r"^(?:hackerrank|assessment|score)\b.*\bbelow\b", re.I), S.RULE_MIN_SCORE),
    (re.compile(r"^cgpa\b", re.I), S.RULE_MIN_CGPA),
    (re.compile(r"^graduation year\b", re.I), S.RULE_GRAD_YEAR),
    (re.compile(r"duplicate", re.I), S.RULE_DUPLICATE),
    (re.compile(r"exclu", re.I), S.RULE_EXCLUDED),
]
_INJECTION_KINDS = {"instruction": "Instruction aimed at screeners or AI tools",
                    "invisible_chars": "Invisible characters", "hidden_text": "Hidden text"}

_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")
_SIGNED_NUMBER_RE = re.compile(r"[+-](?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_SURROGATE_RE = re.compile("[\ud800-\udfff]")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE_RE = re.compile(r"(?<!\w)\+?\d(?:[\s-]?\d){9,}(?!\w)")
_EXCEL_MAX_CHARS = 32767
_MAX_LINKS = 10
_MAX_SNIPPET = 160
_LEVEL_ORDER = {S.RISK_HIGH: 0, S.RISK_MEDIUM: 1, S.RISK_LOW: 2, S.RISK_CLEAR: 3}

_LABELS: dict[str, str] = {
    **S.TA_FIELD_LABELS,
    S.RANK: "Rank", S.STATUS: "Status", S.STATUS_REASON: "Status Reason", S.ELIGIBLE: "Eligible",
    S.FINAL_SCORE: "Final Score", S.PTS_HR: "HackerRank Pts", S.PTS_SKILLS: "Skills Pts",
    S.PTS_EXPERIENCE: "Experience Pts", S.PTS_EDUCATION: "Education Pts",
    S.SUB_HR: "HackerRank Sub", S.SUB_SKILLS: "Skills Sub", S.SUB_EXPERIENCE: "Experience Sub",
    S.SUB_EDUCATION: "Education Sub", S.HR_PCT: "HackerRank %", S.RISK_LEVEL: "Risk Level",
    S.RISK_SCORE: "Risk Score", S.RISK_REASONS: "Risk Reasons", S.CLUSTER_ID: "Cluster",
    S.IS_FOCUS: "Perfect Scorer", S.N_LINKS: "Risk Links", S.MAX_PAIR_SCORE: "Max Pair Score",
    S.INDIVIDUAL_SCORE: "Individual Risk Score", S.MATCHED_SKILLS: "Matched Skills",
    S.MISSING_SKILLS: "Missing Skills", S.N_MATCHED_SKILLS: "# Matched Skills",
    S.SKILLS_FOUND: "Skills Found", S.INTERNSHIP_COUNT: "Internships",
    S.EXPERIENCE_MONTHS: "Experience (months)", S.PROJECT_COUNT: "Projects",
    S.CERTIFICATIONS: "Certifications", S.CERT_COUNT: "# Certifications",
    S.RESUME_CGPA: "Resume CGPA", S.RESUME_DEGREE: "Resume Degree", S.RELEVANCE: "JD Relevance",
    S.EXPERIENCE_SOURCE: "Experience Source", S.LLM_RATIONALE: "LLM Rationale",
    S.SCORE_NOTES: "Score Notes", S.IP_SUBNET: "IP Subnet", S.EMAIL_DOMAIN: "Email Domain",
    S.TEST_SLOT: "Test Slot", S.RESUME_HASH: "Resume Hash", S.HAS_RESUME: "Has Resume",
    S.MATCH_METHOD: "Match Method", S.MATCH_CONFIDENCE: "Match Confidence",
    S.DATA_FLAGS: "Data Flags", S.COLLEGE_CAPPED: "College Capped", S.IP_ADDRESSES: "IP Addresses",
    S.PLAGIARISM_MATCH_ID: "Most Similar Candidate ID",
    S.INELIGIBILITY: "Ineligibility", S.TIE_BREAK_NOTE: "Tie-break Note", S.CGPA_SOURCE: "CGPA Source",
    S.TIER_SOURCE: "Tier Source", S.DUPLICATE_OF: "Duplicate Of", S.DUPLICATE_IDS: "Duplicate IDs",
    S.DUPLICATE_EVIDENCE: "Duplicate Evidence", S.INJECTION_FLAGS: "Hidden Text Flags",
    # risk frames
    "cand_a": "Candidate A", "cand_b": "Candidate B", "name_a": "Name A", "name_b": "Name B",
    "pair_score": "Pair Score", "signals": "Signals", "signal_keys": "Signal Keys",
    "shared_hashes": "Identical Answers", "resume_similarity": "Resume Similarity",
    "shared_bullets": "Shared Bullets", "shared_bullet_examples": "Shared Bullet Examples",
    "shared_github": "Shared GitHub", "same_ip": "Same IP", "same_subnet": "Same Subnet",
    "same_college": "Same College", "same_phone": "Same Phone", "same_email": "Same Email",
    "start_gap_sec": "Start Gap (s)", "submit_gap_sec": "Submit Gap (s)",
    "duration_gap_min": "Duration Gap (min)", "sequential_gap_min": "Sequential Gap (min)",
    "size": "Size", "members": "Members", "member_names": "Member Names",
    "mean_pair_score": "Mean Pair Score", "density": "Density", "top_signals": "Top Signals",
    "shared_traits": "Shared Traits", "colleges": "Colleges", "n_colleges": "# Colleges",
    "start_span_min": "Start Span (min)", "submit_span_min": "Submit Span (min)",
    "summary": "Summary", "attribute": "Attribute", "value": "Value",
    "focus_count": "Focus Count", "focus_total": "Focus Total", "focus_share": "Focus Share",
    "population_count": "Population Count", "population_total": "Population Total",
    "population_share": "Population Share", "lift": "Lift", "p_value": "p-value",
    "q_value": "q-value (BH)", "significant": "Significant",
    # integrity evidence / ineligible candidates / data quality / llm
    "individual_flags": "Individual Flags", "linked_candidates": "Linked Candidates",
    "link_evidence": "Link Evidence (pair score: signals)", "ai_notes": "AI Notes (LLM)",
    "ai_headline": "AI Headline (LLM)", "ai_summary": "AI Summary (LLM)",
    "ai_actions": "AI Recommended Actions (LLM)", "severity": "Severity", "stage": "Stage",
    "issue": "Issue", "file_name": "File Name", "key": "Setting",
    "rule": "Rule", "candidate_value": "Candidate value", "threshold_value": "Threshold",
    "explanation": "Explanation",
}
_INT_COLS = {
    S.RANK, S.N_LINKS, S.N_MATCHED_SKILLS, S.INTERNSHIP_COUNT, S.PROJECT_COUNT, S.CERT_COUNT,
    S.TAB_SWITCHES, S.COPY_PASTE, S.GRAD_YEAR, "size", "n_colleges", "shared_bullets",
    "focus_count", "focus_total", "population_count", "population_total",
}
_DEC1_COLS = {
    S.HR_PCT, S.CGPA, S.RESUME_CGPA, S.COMPLETION_MIN, S.EXPERIENCE_MONTHS, S.RISK_SCORE,
    S.MAX_PAIR_SCORE, S.INDIVIDUAL_SCORE, "pair_score", "mean_pair_score", "start_gap_sec",
    "submit_gap_sec", "duration_gap_min", "sequential_gap_min", "start_span_min", "submit_span_min",
}
_DEC3_COLS = {
    S.SUB_HR, S.SUB_SKILLS, S.SUB_EXPERIENCE, S.SUB_EDUCATION, S.RELEVANCE, S.MATCH_CONFIDENCE,
    "resume_similarity", "density",
}
_PCT_COLS = {"focus_share", "population_share"}
_SCI_COLS = {"p_value", "q_value"}
_WIDE_COLS = {
    S.STATUS_REASON, S.RISK_REASONS, S.SCORE_NOTES, S.LLM_RATIONALE, S.SKILLS_FOUND,
    S.CERTIFICATIONS, S.MATCHED_SKILLS, S.MISSING_SKILLS, S.INELIGIBILITY, S.TIE_BREAK_NOTE,
    S.DUPLICATE_EVIDENCE, S.INJECTION_FLAGS, S.ASSESSMENT_COMMENT, "signals", "shared_bullet_examples",
    "shared_traits", "summary", "members", "member_names", "issue", "link_evidence",
    "individual_flags", "ai_notes", "ai_summary", "ai_actions", "value", "explanation",
}
_SHORTLIST_COLUMNS = [
    S.RANK, S.CANDIDATE_ID, S.NAME, S.STATUS, S.FINAL_SCORE, S.EMAIL, S.PHONE, S.STATUS_REASON,
    S.TIE_BREAK_NOTE, S.SCORE_NOTES,
    S.PTS_HR, S.HR_SCORE, S.HR_PCT, S.CANDIDATE_OPTION,
    S.PTS_SKILLS, S.PTS_EXPERIENCE, S.MATCHED_SKILLS, S.MISSING_SKILLS,
    S.PTS_EDUCATION, S.COLLEGE, S.COLLEGE_TIER, S.DEGREE, S.BRANCH, S.CGPA, S.CGPA_SOURCE, S.GRAD_YEAR,
    S.RISK_LEVEL, S.RISK_SCORE, S.CLUSTER_ID, S.RISK_REASONS, S.DUPLICATE_IDS,
]
_NOTE_TEXT_KEYS = ("summary", "explanation", "rationale", "headline")


# ---------------------------------------------------------------------------
# Sanitising + small helpers
# ---------------------------------------------------------------------------
def sanitize_cell(value: Any) -> Any:
    """Prefix text that a spreadsheet would treat as a formula with an apostrophe."""
    if (isinstance(value, str) and value and value[0] in _FORMULA_TRIGGERS
            and not _SIGNED_NUMBER_RE.fullmatch(value)):
        return "'" + value
    return value


def sanitize_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Copy of ``df`` with every text cell (and header) passed through sanitize_cell.

    Text UTF-8 cannot encode (lone surrogates from broken PDF text) is repaired on the way.
    """
    out = df.copy()
    for col in out.columns:
        s = out[col]
        if (pd.api.types.is_object_dtype(s.dtype) or pd.api.types.is_string_dtype(s.dtype)
                or isinstance(s.dtype, pd.CategoricalDtype)):
            out[col] = s.astype(object).map(_sanitize_value)
    out.columns = [sanitize_cell(_safe_text(str(c))) for c in out.columns]
    return out


def _sanitize_value(value: Any) -> Any:
    return sanitize_cell(_safe_text(value) if isinstance(value, str) else value)


def _safe_text(text: str) -> str:
    """Text UTF-8 can encode: split surrogate pairs re-joined, lone surrogates -> U+FFFD."""
    if _SURROGATE_RE.search(text):
        return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    return text


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
    if _isna(value) or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _truthy(value: Any) -> bool:
    if _isna(value):
        return False
    if isinstance(value, (bool, np.bool_, int, float, np.integer, np.floating)):
        return bool(value)
    return str(value).strip().lower() in {"true", "t", "yes", "y", "1"}


def _fmt(value: float | None, dp: int = 1) -> str:
    if value is None:
        return ""
    text = f"{value:.{dp}f}".rstrip("0").rstrip(".") if dp > 0 else f"{value:.0f}"
    return "0" if text in ("", "-0") else text


def _label(col: Any) -> str:
    name = str(col)
    return _LABELS.get(name) or name.replace("_", " ").strip().title()


def _text_of(value: Any) -> str:
    if isinstance(value, (list, tuple, set, frozenset)):
        items = sorted(value, key=str) if isinstance(value, (set, frozenset)) else list(value)
        if any(isinstance(v, (dict, list, tuple)) for v in items):
            return json.dumps(items, default=str, ensure_ascii=False)
        return ", ".join(map(str, items))
    if isinstance(value, dict):
        return json.dumps(value, default=str, sort_keys=True, ensure_ascii=False)
    return str(value)


def _split_reasons(value: Any) -> list[str]:
    return [p.strip() for p in _clean(value).split(S.SEP_REASONS.strip()) if p.strip()]


def _split_notes(value: Any) -> list[str]:
    return [p.strip() for p in _clean(value).split(S.SEP_NOTES.strip()) if p.strip()]


def _split_ids(value: Any) -> list[str]:
    return [p.strip() for p in _clean(value).split(",") if p.strip()]


def _level(value: Any) -> str:
    text = _clean(value).title()
    return text or S.RISK_CLEAR


def _col(df: pd.DataFrame | None, col: str) -> pd.Series:
    if df is None or col not in df.columns:
        return pd.Series([None] * (0 if df is None else len(df)), index=None if df is None else df.index,
                         dtype=object)
    return df[col]


def _frame(df: Any) -> pd.DataFrame:
    return df if isinstance(df, pd.DataFrame) else pd.DataFrame()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _display_name(name: Any, job_application: Any = None) -> str:
    """Candidate name; falls back to the Workday "First Last (first last)" application label."""
    text = _clean(name)
    if text:
        return text
    return re.sub(r"\s*\([^()]*\)\s*$", "", _clean(job_application)).strip()


def _name_map(candidates: pd.DataFrame, col: str = S.NAME) -> dict[str, str]:
    """candidate_id -> ``col`` value (names fall back to the Job Application label)."""
    if S.CANDIDATE_ID not in candidates.columns:
        return {}
    ids = candidates[S.CANDIDATE_ID].tolist()
    if col == S.NAME:
        names, apps = _col(candidates, S.NAME).tolist(), _col(candidates, S.JOB_APPLICATION).tolist()
        return {_clean(k): _display_name(n, a) for k, n, a in zip(ids, names, apps)}
    if col not in candidates.columns:
        return {}
    return {_clean(k): _clean(v) for k, v in zip(ids, candidates[col].tolist())}


def _redact(text: str) -> str:
    """Remove email addresses and phone numbers from free text (evidence packs omit contact data)."""
    return _PHONE_RE.sub("[phone]", _EMAIL_RE.sub("[email]", text))


def _links_for(pairs: pd.DataFrame, candidate_id: str) -> pd.DataFrame:
    """Pairs touching ``candidate_id`` with an ``other`` column, strongest first."""
    if pairs.empty or not {"cand_a", "cand_b"} <= set(pairs.columns):
        return pd.DataFrame(columns=["other", "pair_score", "signals"])
    a = pairs["cand_a"].map(_clean)
    b = pairs["cand_b"].map(_clean)
    mask = (a == candidate_id) | (b == candidate_id)
    sel = pairs.loc[mask].copy()
    sel["other"] = np.where(a[mask] == candidate_id, b[mask], a[mask])
    sel["_score"] = pd.to_numeric(_col(sel, "pair_score"), errors="coerce").fillna(0.0)
    return sel.sort_values(["_score", "other"], ascending=[False, True], kind="mergesort")


def _individual_flags(individual: pd.DataFrame, candidate_id: str) -> list[str]:
    if individual.empty or S.CANDIDATE_ID not in individual.columns:
        return []
    rows = individual.loc[individual[S.CANDIDATE_ID].map(_clean) == candidate_id]
    out = []
    for flag, detail in zip(_col(rows, "flag"), _col(rows, "detail")):
        text = ": ".join(t for t in (_clean(flag), _clean(detail)) if t)
        if text:
            out.append(text)
    return out


def _injection_lines(row: Mapping[str, Any], parsed_by_candidate: Mapping[str, Any] | None) -> list[str]:
    """Hidden / adversarial resume text for one candidate: the INJECTION_FLAGS column, else the
    linked ParsedResume.injection_findings."""
    lines = _split_reasons(row.get(S.INJECTION_FLAGS))
    if lines:
        return lines
    resume = (parsed_by_candidate or {}).get(_clean(row.get(S.CANDIDATE_ID)))
    for finding in getattr(resume, "injection_findings", None) or []:
        if not isinstance(finding, Mapping):
            continue
        kind_key = _clean(finding.get("kind"))
        kind = _INJECTION_KINDS.get(kind_key) or kind_key.replace("_", " ").capitalize() or "Hidden text"
        detail = _clean(finding.get("detail"))
        snippet = " ".join(_clean(finding.get("snippet")).split())
        if len(snippet) > _MAX_SNIPPET:
            snippet = snippet[:_MAX_SNIPPET - 3].rstrip() + "..."
        text = f"{kind}: {detail}" if detail else kind
        lines.append(f'{text} - "{snippet}"' if snippet else text)
    return lines


def _llm_candidate_text(note: Any) -> str:
    if not isinstance(note, Mapping):
        return _clean(note)
    if note.get("ok") is False:
        return ""
    parts = [next((_clean(note.get(k)) for k in _NOTE_TEXT_KEYS if _clean(note.get(k))), "")]
    for key, title in (("strengths", "Strengths"), ("gaps", "Gaps")):
        values = note.get(key)
        items = [_clean(x) for x in (values if isinstance(values, (list, tuple)) else []) if _clean(x)]
        if items:
            parts.append(f"{title}: {'; '.join(items)}")
    return " ".join(p for p in parts if p)


def _candidate_notes(notes: Mapping[str, Any]) -> dict[str, str]:
    """candidate_id -> AI note text from llm_notes "candidates" and "borderline"."""
    out: dict[str, list[str]] = {}
    for key, prefix in (("candidates", ""), ("borderline", "Near the cutoff: ")):
        store = notes.get(key)
        if not isinstance(store, Mapping):
            continue
        for cid, note in store.items():
            text = _llm_candidate_text(note)
            if text:
                out.setdefault(_clean(cid), []).append(prefix + text)
    return {cid: "\n".join(dict.fromkeys(texts)) for cid, texts in out.items()}


# ---------------------------------------------------------------------------
# Column sources, groups and labels (shared with the UI)
# ---------------------------------------------------------------------------
def column_source(col: Any) -> str:
    """Data source of a canonical column: schema.SRC_* ("Assessment", "Resume", "Academic",
    "Integrity") or "" for identity / outcome / unknown columns. Per-question columns
    (q1_score, q2_hash, ...) and a few derived columns follow the data they come from."""
    name = str(col)
    source = S.COLUMN_SOURCE.get(name) or _EXTRA_SOURCE.get(name)
    if source:
        return source
    if S.ANSWER_HASH_RE.match(name) or S.QUESTION_SCORE_RE.match(name) or S.QUESTION_SIMILARITY_RE.match(name):
        return S.SRC_ASSESSMENT
    return ""


def column_group(col: Any) -> str:
    """Super-header group of a column (one of GROUP_ORDER)."""
    source = column_source(col)
    if source:
        return source
    name = str(col)
    if name in _CORE_COLUMNS:
        return GROUP_CORE
    if name == S.LLM_RATIONALE or name.startswith("ai_"):
        return GROUP_LLM
    return GROUP_OTHER


def column_label(col: Any, *, prefixed: bool = False) -> str:
    """Display label of a column; ``prefixed`` adds the data source for single-header formats:
    "HackerRank: Score", "Resume: Skills Pts", "Academic: CGPA", "Integrity: Risk Level"."""
    label = _label(col)
    source = column_source(col) if prefixed else ""
    prefix = SOURCE_PREFIX.get(source)
    if not prefix:
        return label
    short = _PREFIXED_LABELS.get(str(col))
    if short is None:
        short = label
        for word in (prefix, source):  # "HackerRank Score" -> "Score", "Resume CGPA" -> "CGPA"
            if short.lower().startswith(word.lower() + " ") and len(short) > len(word) + 1:
                short = short[len(word) + 1:]
                break
    return f"{prefix}: {short}"


def _unique_labels(columns: list[Any], prefixed: bool) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for col in columns:
        label = column_label(col, prefixed=prefixed)
        n = seen.get(label.casefold(), 0) + 1
        seen[label.casefold()] = n
        out.append(label if n == 1 else f"{label} ({n})")
    return out


def column_labels(columns: Iterable[Any], *, prefixed: bool = True) -> dict[Any, str]:
    """{column: unique display label} - e.g. for st.column_config or renaming a view."""
    cols = list(columns)
    return dict(zip(cols, _unique_labels(cols, prefixed)))


def _group_positions(columns: list[Any]) -> list[int]:
    rank = {group: i for i, group in enumerate(GROUP_ORDER)}
    return sorted(range(len(columns)), key=lambda j: (rank[column_group(columns[j])], j))


def group_columns(columns: Iterable[Any]) -> list[Any]:
    """Columns reordered by group (GROUP_ORDER); the order inside a group is kept."""
    cols = list(columns)
    return [cols[j] for j in _group_positions(cols)]


def labelled_columns(df: pd.DataFrame, *, group: bool = False) -> pd.DataFrame:
    """Copy of ``df`` with source-prefixed display labels as headers (for CSV and the UI).

    Values are unchanged. ``group=True`` also orders the columns as the Excel sheets do:
    candidate & outcome, Assessment, Resume, Academic, Integrity, AI notes, other.
    """
    columns = list(df.columns)
    positions = _group_positions(columns) if group else list(range(len(columns)))
    out = df.iloc[:, positions].copy()
    out.columns = _unique_labels([columns[j] for j in positions], prefixed=True)
    return out


# ---------------------------------------------------------------------------
# Ineligibility (schema.INELIGIBILITY: JSON list of rule violations)
# ---------------------------------------------------------------------------
def _rule_item(rule: str, candidate_value: Any, threshold_value: Any, explanation: str) -> dict[str, Any]:
    return {"rule_violated": rule, "candidate_value": None if _isna(candidate_value) else candidate_value,
            "threshold_value": None if _isna(threshold_value) else threshold_value, "explanation": explanation}


def ineligibility_items(value: Any) -> list[dict[str, Any]]:
    """One INELIGIBILITY cell (JSON text or a list of dicts) ->
    [{"rule_violated", "candidate_value", "threshold_value", "explanation"}]. Never raises."""
    data = value
    if isinstance(data, (bytes, bytearray)):
        data = bytes(data).decode("utf-8", "replace")
    if isinstance(data, str):
        text = data.strip()
        if not text:
            return []
        try:
            data = json.loads(text)
        except ValueError:
            return [_rule_item("", None, None, text)]
        if isinstance(data, str):
            return [_rule_item("", None, None, data.strip())] if data.strip() else []
    if isinstance(data, np.ndarray):
        data = data.tolist()
    if isinstance(data, Mapping):
        data = [data]
    if not isinstance(data, (list, tuple)):
        return []
    out = []
    for item in data:
        if isinstance(item, Mapping):
            rule = _clean(item.get("rule_violated") or item.get("rule"))
            out.append(_rule_item(rule, item.get("candidate_value"), item.get("threshold_value"),
                                  _clean(item.get("explanation"))))
        elif _clean(item):
            out.append(_rule_item("", None, None, _clean(item)))
    return out


def _value_text(value: Any) -> str:
    if value is None or _isna(value):
        return ""
    if isinstance(value, float):
        return _fmt(value, 2)
    return _text_of(value)


def ineligibility_text(value: Any, sep: str = S.SEP_NOTES) -> str:
    """Readable one-line form of an INELIGIBILITY cell: "Academic CGPA: CGPA 6.80 is below 7.00"."""
    parts = []
    for item in ineligibility_items(value):
        detail = item["explanation"]
        if not detail:
            cand, thr = _value_text(item["candidate_value"]), _value_text(item["threshold_value"])
            detail = f"{cand} (threshold {thr})" if cand and thr else cand or (f"threshold {thr}" if thr else "")
        rule = item["rule_violated"]
        parts.append(f"{rule}: {detail}" if rule and detail else rule or detail)
    return sep.join(p for p in parts if p)


def _rule_from_reason(text: str) -> str:
    for pattern, rule in _REASON_RULES:
        if pattern.search(text):
            return rule
    return ""


def _rule_rank(rule: str) -> int:
    return RULE_ORDER.index(rule) if rule in RULE_ORDER else len(RULE_ORDER)


def ineligible_rows(candidates: pd.DataFrame) -> pd.DataFrame:
    """One row per violated rule (columns INELIGIBLE_COLUMNS), ordered by rule then candidate ID.

    Uses schema.INELIGIBILITY; an Ineligible candidate without it (e.g. a run saved by an older
    version) gets one row per status-reason part, with the rule inferred from the text.
    """
    c = _frame(candidates)
    if c.empty:
        return pd.DataFrame(columns=INELIGIBLE_COLUMNS)
    ids = _col(c, S.CANDIDATE_ID).map(_clean).tolist()
    names = [_display_name(n, j) for n, j in zip(_col(c, S.NAME).tolist(), _col(c, S.JOB_APPLICATION).tolist())]
    status = _col(c, S.STATUS).map(_clean).tolist()
    reasons = _col(c, S.STATUS_REASON).tolist()
    values = _col(c, S.INELIGIBILITY).tolist()
    rows = []
    for i in range(len(c)):
        items = ineligibility_items(values[i])
        if not items and status[i] == S.STATUS_INELIGIBLE:
            items = [_rule_item(_rule_from_reason(p), None, None, p) for p in _split_notes(reasons[i])]
            items = items or [_rule_item("", None, None, "Ineligible (no reason recorded)")]
        for item in items:
            rows.append({S.CANDIDATE_ID: ids[i], S.NAME: names[i], "rule": item["rule_violated"] or OTHER_RULE,
                         "candidate_value": item["candidate_value"], "threshold_value": item["threshold_value"],
                         "explanation": item["explanation"]})
    out = pd.DataFrame(rows, columns=INELIGIBLE_COLUMNS)
    if out.empty:
        return out
    out = (out.assign(_r=out["rule"].map(_rule_rank))
           .sort_values(["_r", "rule", S.CANDIDATE_ID], kind="mergesort").drop(columns="_r"))
    return out.reset_index(drop=True)


def ineligibility_counts(candidates: pd.DataFrame) -> list[tuple[str, int]]:
    """[(rule, candidates failing it)] in RULE_ORDER; a candidate can fail several rules."""
    rows = ineligible_rows(candidates)
    if rows.empty:
        return []
    per_rule = rows.drop_duplicates([S.CANDIDATE_ID, "rule"]).groupby("rule", sort=False).size()
    return sorted(((str(r), int(n)) for r, n in per_rule.items()), key=lambda t: (_rule_rank(t[0]), t[0]))


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------
def _csv_cell(value: Any) -> Any:
    if isinstance(value, (list, tuple, set, frozenset, dict, np.ndarray)):
        data = value.tolist() if isinstance(value, np.ndarray) else value
        if isinstance(data, (set, frozenset)):
            data = sorted(data, key=str)
        return _safe_text(json.dumps(data, default=str, ensure_ascii=False))
    return _safe_text(value) if isinstance(value, str) else value


def _flat_cells(df: pd.DataFrame) -> pd.DataFrame:
    """Lists / dicts in object columns as JSON text (pandas would write their Python repr)."""
    out = df
    for j in range(df.shape[1]):
        series = df.iloc[:, j]
        if series.dtype == object and any(isinstance(v, (list, tuple, set, frozenset, dict, np.ndarray))
                                          for v in series.tolist()):
            if out is df:
                out = df.copy()
            out.isetitem(j, series.map(_csv_cell))
    return out


def to_csv_bytes(df: pd.DataFrame, *, labelled: bool = False) -> bytes:
    """CSV encoded as UTF-8 with BOM (opens cleanly in Excel), formula-injection safe.

    ``labelled=True`` writes source-prefixed display labels as headers (labelled_columns).
    """
    frame = labelled_columns(df) if labelled else df
    text = sanitize_frame(_flat_cells(frame)).to_csv(index=False, lineterminator="\r\n",
                                                     date_format="%Y-%m-%d %H:%M:%S")
    return _safe_text(text).encode("utf-8-sig")


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------
class _Book:
    """xlsxwriter workbook with shared brand formats and a typed table writer."""

    def __init__(self, buffer: io.BytesIO) -> None:
        self.wb = xlsxwriter.Workbook(buffer, {
            "in_memory": True, "strings_to_formulas": False, "strings_to_urls": False,
            "strings_to_numbers": False,
        })
        add = self.wb.add_format
        self.header = add({"bold": True, "font_color": "#FFFFFF", "bg_color": BRAND_RED,
                           "border": 1, "border_color": "#BA0C2F", "text_wrap": True,
                           "valign": "top"})
        self.groups = {group: add({"bold": True, "font_color": font, "bg_color": fill, "align": "center",
                                   "valign": "vcenter", "border": 1, "border_color": "#FFFFFF"})
                       for group, (fill, font) in GROUP_STYLE.items()}
        self.title = add({"bold": True, "font_size": 16, "font_color": BRAND_RED})
        self.subtitle = add({"italic": True, "font_color": "#555555"})
        self.label = add({"bold": True, "valign": "top"})
        self.wrap = add({"text_wrap": True, "valign": "top"})
        self.disclaimer = add({"text_wrap": True, "italic": True, "valign": "top", "border": 1,
                               "bg_color": MEDIUM_FILL, "font_color": MEDIUM_FONT})
        self.int = add({"num_format": "0"})
        self.thousands = add({"num_format": "#,##0", "align": "left"})
        self.dec1 = add({"num_format": "0.0"})
        self.dec2 = add({"num_format": "0.00"})
        self.dec3 = add({"num_format": "0.000"})
        self.pct = add({"num_format": "0.0%"})
        self.sci = add({"num_format": "0.00E+00"})
        self.datetime = add({"num_format": "yyyy-mm-dd hh:mm:ss"})
        self.date = add({"num_format": "yyyy-mm-dd"})
        self.high = add({"bg_color": HIGH_FILL, "font_color": HIGH_FONT})
        self.medium = add({"bg_color": MEDIUM_FILL, "font_color": MEDIUM_FONT})
        self.clear = add({"bg_color": CLEAR_FILL, "font_color": CLEAR_FONT})
        self.review = add({"bg_color": MEDIUM_FILL, "font_color": MEDIUM_FONT, "bold": True})
        self.hold = add({"bg_color": HIGH_FILL, "font_color": HIGH_FONT, "bold": True})

    # -- cells -----------------------------------------------------------
    def cell(self, ws: Any, r: int, c: int, value: Any, fmt: Any = None) -> None:
        if _isna(value):
            return
        if isinstance(value, (bool, np.bool_)):
            ws.write_boolean(r, c, bool(value), fmt)
        elif isinstance(value, (int, np.integer)):
            ws.write_number(r, c, int(value), fmt)
        elif isinstance(value, (float, np.floating)):
            if math.isfinite(value):
                ws.write_number(r, c, float(value), fmt)
        elif isinstance(value, datetime):
            dt = value.to_pydatetime() if isinstance(value, pd.Timestamp) else value
            ws.write_datetime(r, c, dt.replace(tzinfo=None), self.datetime)
        elif isinstance(value, date):
            ws.write_datetime(r, c, datetime(value.year, value.month, value.day), self.date)
        else:
            text = sanitize_cell(_safe_text(_text_of(value)))[:_EXCEL_MAX_CHARS]
            if text:  # empty text stays a blank cell
                ws.write_string(r, c, text, fmt)

    def _column_format(self, col: str, series: pd.Series, wrap: bool) -> Any:
        dtype = series.dtype
        if wrap:
            return self.wrap
        if pd.api.types.is_bool_dtype(dtype):
            return None
        if pd.api.types.is_datetime64_any_dtype(dtype):
            return self.datetime
        if col in _INT_COLS or pd.api.types.is_integer_dtype(dtype):
            return self.int
        for cols, fmt in ((_DEC3_COLS, self.dec3), (_DEC1_COLS, self.dec1), (_PCT_COLS, self.pct),
                          (_SCI_COLS, self.sci)):
            if col in cols:
                return fmt
        return self.dec2 if pd.api.types.is_float_dtype(dtype) else None

    @staticmethod
    def _width(col: str, series: pd.Series, wrap: bool) -> float:
        cap = 60.0 if (col in _WIDE_COLS or wrap) else 40.0
        if pd.api.types.is_numeric_dtype(series.dtype) and not pd.api.types.is_bool_dtype(series.dtype):
            cap = 14.0
        lens = [len(_label(col))]
        for v in series.head(300):
            if not _isna(v):
                text = _text_of(v)
                lens.append(max(len(line) for line in text.split("\n")) if text else 0)
        return float(min(cap, max(8.0, max(lens) + 2.0)))

    # -- tables ------------------------------------------------------------
    def table(self, ws: Any, df: pd.DataFrame, *, row: int = 0, col: int = 0,
              freeze_cols: int | None = 0, wrap: Iterable[str] = (),
              widths: Mapping[str, float] | None = None) -> int:
        """Write ``df`` as a filtered table under a brand header; returns the next free row."""
        columns = [str(c) for c in df.columns]
        if not columns:
            ws.write_string(row, col, "No data available for this run.", self.subtitle)
            return row + 2
        wrap_set, widths = set(wrap), dict(widths or {})
        for j, name in enumerate(columns):
            ws.write_string(row, col + j, sanitize_cell(_safe_text(_label(name))), self.header)
            series = df.iloc[:, j]
            ws.set_column(col + j, col + j, widths.get(name) or self._width(name, series, name in wrap_set))
        formats = [self._column_format(name, df.iloc[:, j], name in wrap_set)
                   for j, name in enumerate(columns)]
        for i, values in enumerate(df.itertuples(index=False, name=None), start=row + 1):
            for j, value in enumerate(values):
                self.cell(ws, i, col + j, value, formats[j])
        last_row, last_col = row + len(df), col + len(columns) - 1
        ws.autofilter(row, col, last_row, last_col)
        if freeze_cols is not None:
            ws.freeze_panes(row + 1, freeze_cols)
        if len(df):
            self._highlight(ws, columns, row + 1, last_row, col)
        return last_row + 2

    def grouped_table(self, ws: Any, df: pd.DataFrame, *, freeze_cols: int | None = 0,
                      wrap: Iterable[str] = (), widths: Mapping[str, float] | None = None) -> int:
        """Table whose columns are grouped by data source under a coloured super-header row
        (row 1: "Candidate & Outcome", "Assessment", "Resume", "Academic", "Integrity", ...)."""
        columns = list(df.columns)
        if not columns:
            return self.table(ws, df, freeze_cols=freeze_cols, wrap=wrap, widths=widths)
        frame = df.iloc[:, _group_positions(columns)]
        groups = [column_group(c) for c in frame.columns]
        start = 0
        for j in range(1, len(groups) + 1):
            if j < len(groups) and groups[j] == groups[start]:
                continue
            fmt = self.groups[groups[start]]
            if j - 1 > start:
                ws.merge_range(0, start, 0, j - 1, groups[start], fmt)
            else:
                ws.write_string(0, start, groups[start], fmt)
            start = j
        ws.set_row(0, 20)
        return self.table(ws, frame, row=1, freeze_cols=freeze_cols, wrap=wrap, widths=widths)

    def _highlight(self, ws: Any, columns: list[str], first: int, last: int, col0: int) -> None:
        rules = {
            S.RISK_LEVEL: [(S.RISK_HIGH, self.high), (S.RISK_MEDIUM, self.medium)],
            S.STATUS: [(S.STATUS_REVIEW, self.review), (S.STATUS_HOLD, self.hold),
                       (S.STATUS_SHORTLISTED, self.clear)],
            "severity": [("error", self.high), ("warning", self.medium)],
        }
        for j, name in enumerate(columns):
            for value, fmt in rules.get(name, []):
                ws.conditional_format(first, col0 + j, last, col0 + j, {
                    "type": "cell", "criteria": "==", "value": json.dumps(value), "format": fmt})
            if name == "significant":
                ws.conditional_format(first, col0 + j, last, col0 + j, {
                    "type": "cell", "criteria": "==", "value": "TRUE", "format": self.medium})

    def key_values(self, ws: Any, row: int, title: str, items: list[tuple[str, Any]],
                   col: int = 0) -> int:
        """Section header + label/value rows; returns the next free row."""
        ws.write_string(row, col, title, self.header)
        ws.write_string(row, col + 1, "", self.header)
        for k, (key, value) in enumerate(items, start=row + 1):
            ws.write_string(k, col, sanitize_cell(_safe_text(str(key))), self.label)
            if isinstance(value, (int, np.integer)) and not isinstance(value, (bool, np.bool_)):
                self.cell(ws, k, col + 1, value, self.thousands)
            elif isinstance(value, (float, np.floating)):
                self.cell(ws, k, col + 1, value, self.dec2)
            else:
                self.cell(ws, k, col + 1, value, self.wrap)
        return row + len(items) + 2


def _hidden_text_count(result: PipelineResult, c: pd.DataFrame) -> int | None:
    if S.INJECTION_FLAGS in c.columns:
        return int(_col(c, S.INJECTION_FLAGS).map(_clean).astype(bool).sum())
    parsed = result.parsed_by_candidate or {}
    if not parsed:
        return None
    return sum(1 for p in parsed.values() if getattr(p, "injection_findings", None))


def _kpis(result: PipelineResult, cfg: AppConfig) -> list[tuple[str, Any]]:
    c = _frame(result.candidates)
    status = _col(c, S.STATUS).map(_clean)
    level = _col(c, S.RISK_LEVEL).map(_level)
    final = pd.to_numeric(_col(c, S.FINAL_SCORE), errors="coerce")
    listed = status.isin(IN_SHORTLIST)
    pct = pd.to_numeric(_col(c, S.HR_PCT), errors="coerce")
    focus = len(result.risk.focus_ids) if result.risk and result.risk.focus_ids else int(
        (pct >= cfg.risk.focus_threshold_pct).sum())
    no_resume = int((~_col(c, S.HAS_RESUME).map(_truthy).astype(bool)).sum())
    risk = result.risk
    items: list[tuple[str, Any]] = [
        ("Run mode", result.mode),
        ("Candidates processed", len(c)),
        ("Eligible", int(_col(c, S.ELIGIBLE).map(_truthy).sum())),
        ("Shortlisted (total)", int(listed.sum())),
        ("  - Shortlisted", int((status == S.STATUS_SHORTLISTED).sum())),
        ("  - Shortlisted (integrity review)", int((status == S.STATUS_REVIEW).sum())),
        ("Integrity hold", int((status == S.STATUS_HOLD).sum())),
        ("Not shortlisted", int((status == S.STATUS_NOT).sum())),
        ("Ineligible", int((status == S.STATUS_INELIGIBLE).sum())),
        ("Average final score (all)", round(float(final.mean()), 2) if final.notna().any() else "n/a"),
        ("Average final score (shortlist)",
         round(float(final[listed].mean()), 2) if final[listed].notna().any() else "n/a"),
        ("Lowest shortlisted score", round(float(final[listed].min()), 2) if final[listed].notna().any() else "n/a"),
        ("Perfect scorers (risk focus group)", focus),
        ("Risk flags - High", int((level == S.RISK_HIGH).sum())),
        ("Risk flags - Medium", int((level == S.RISK_MEDIUM).sum())),
        ("Risk clusters", len(_frame(risk.clusters)) if risk else 0),
        ("Linked candidate pairs", len(_frame(risk.pairs)) if risk else 0),
        ("Resumes parsed", len(result.parsed_resumes or {})),
        ("Resumes linked to candidates", len(result.parsed_by_candidate or {})),
        ("Candidates without a resume", no_resume if S.HAS_RESUME in c.columns else "n/a"),
    ]
    if S.DUPLICATE_OF in c.columns:
        items.append(("Duplicate applications (same person applied again)",
                      int(_col(c, S.DUPLICATE_OF).map(_clean).astype(bool).sum())))
    hidden = _hidden_text_count(result, c)
    if hidden is not None:
        items.append(("Resumes with hidden text (excluded from scoring)", hidden))
    return items


def _config_highlights(cfg: AppConfig) -> list[tuple[str, Any]]:
    w, sk, sl, rk, llm, ed = cfg.weights.normalized(), cfg.skills, cfg.shortlist, cfg.risk, cfg.llm, cfg.education
    if sl.method == "top_pct":
        method = f"Top {_fmt(sl.top_pct)}% of eligible candidates"
    elif sl.method == "threshold":
        method = f"Final score >= {_fmt(sl.threshold)}"
    else:
        method = f"Top {int(sl.top_n):,} eligible candidates"
    excluded = list(getattr(sl, "exclude_candidates", None) or [])
    knockouts = [f"HackerRank >= {_fmt(sl.min_hackerrank_pct)}%" if sl.min_hackerrank_pct else "",
                 f"CGPA >= {_fmt(sl.min_cgpa, 2)}" if sl.min_cgpa else "",
                 f"Graduation year in {', '.join(map(str, sl.allowed_grad_years))}" if sl.allowed_grad_years else "",
                 "duplicate applications disqualified (one kept per person)"
                 if getattr(sl, "auto_disqualify_duplicates", False) else "",
                 f"{len(excluded):,} candidate(s) excluded by recruiters" if excluded else ""]
    skills_rule = (f"per skill - full marks at {sk.skills_for_full_marks} matched skills"
                   if sk.mode != "coverage" else "coverage - matched / required")
    policy = ("High risk -> Integrity Hold, slots backfilled" if sl.exclude_high_risk
              else "Flagged candidates stay on the shortlist for recruiter review")
    missing_resume = {
        "auto": "auto - re-normalised when no resumes were loaded, otherwise resume components score 0",
        "zero": "resume components score 0",
        "renormalize": "remaining components re-scaled to 100",
    }.get(sl.missing_resume_policy, str(sl.missing_resume_policy))
    ties = [TIE_BREAK_KEYS.get(k, (str(k), True))[0] for k in getattr(sl, "tie_break_order", None) or []]
    tier = (f"on ({len(getattr(ed, 'tier_mapping', None) or {}):,} colleges in the approved tier list)"
            if getattr(ed, "tier_enabled", False) else "off (tier ignored; other education weights re-normalised)")
    cap = _float(getattr(sl, "college_cap_pct", 0.0)) or 0.0
    provider = LLM_PROVIDERS.get(llm.provider, {}).get("label", llm.provider)
    llm_text = (f"enabled - {provider}, model {llm.model or 'proxy default'}; writes explanations and summaries "
                "only and never changes scores, ranks or cutoffs" if llm.enabled else "disabled")
    return [
        ("Rubric (points out of 100)",
         f"HackerRank {_fmt(w.hackerrank)} | Skills {_fmt(w.skills)} | Experience {_fmt(w.experience)} "
         f"| Education {_fmt(w.education)}"),
        ("Skills scoring", skills_rule),
        ("Required skills", ", ".join(sk.required_skills) or "none"),
        ("Shortlist rule", method),
        ("Tie-break order", " > ".join(["Final score", *ties, "Candidate ID"])),
        ("Eligibility knock-outs", "; ".join(k for k in knockouts if k) or "none"),
        ("Integrity policy", f"{policy} (review levels: {', '.join(sl.review_risk_levels) or 'none'})"),
        ("Missing-resume policy", missing_resume),
        ("College tier scoring", tier),
        ("College cap", f"at most {_fmt(cap)}% of the shortlist per college" if cap > 0 else "off"),
        ("Risk focus group", f"HackerRank >= {_fmt(rk.focus_threshold_pct)}%"),
        ("Risk thresholds", f"High >= {_fmt(rk.high_threshold)}, Medium >= {_fmt(rk.medium_threshold)}, "
                            f"link >= {_fmt(rk.edge_threshold)}"),
        ("LLM assist", llm_text),
    ]


def _quality_items(result: PipelineResult) -> list[tuple[str, Any]]:
    q = result.quality
    if q is None:
        return []
    counts = q.counts()
    return [
        ("TA rows (raw / clean)", f"{q.rows_raw:,} / {q.rows_clean:,}"),
        ("Duplicate IDs dropped", q.duplicates_dropped),
        ("Issues (errors / warnings / info)",
         f"{counts.get('error', 0):,} / {counts.get('warning', 0):,} / {counts.get('info', 0):,}"),
        ("Missing optional fields", ", ".join(q.missing_fields) or "none"),
        ("Unmapped TA columns (kept as extra_*)", ", ".join(q.unmapped_columns) or "none"),
    ]


def _executive_summary(llm_notes: Mapping[str, Any]) -> list[str]:
    """LLM executive summary as plain-text lines (markdown headings/bullets/emphasis removed)."""
    note = llm_notes.get("executive_summary")
    if isinstance(note, Mapping):
        note = note.get("markdown") if note.get("ok", True) else ""
    lines = []
    for raw in _clean(note).splitlines():
        line = re.sub(r"^\s*#+\s*", "", raw.strip())
        line = re.sub(r"^[-*+]\s+", "• ", line).replace("**", "")
        if line:
            lines.append(line)
    return lines


def _write_summary(book: _Book, result: PipelineResult, cfg: AppConfig, llm_notes: Mapping[str, Any],
                   candidates: pd.DataFrame) -> None:
    ws = book.wb.add_worksheet("Summary")
    ws.set_column(0, 0, 40)
    ws.set_column(1, 1, 95)
    ws.write_string(0, 0, "Candidate Screening & Integrity Report", book.title)
    ws.write_string(1, 0, f"Generated {_now()} (local time) - internal use only; contains candidate data.",
                    book.subtitle)
    ws.write_string(3, 0, "Important", book.label)
    ws.write_string(3, 1, DISCLAIMER, book.disclaimer)
    ws.set_row(3, 96)
    row = book.key_values(ws, 5, "Key metrics", _kpis(result, cfg))
    counts = ineligibility_counts(candidates)
    if counts:
        total = int((_col(candidates, S.STATUS).map(_clean) == S.STATUS_INELIGIBLE).sum())
        row = book.key_values(ws, row, "Ineligible candidates by rule (a candidate can fail several rules)",
                              [("Ineligible candidates (total)", total), *counts])
    row = book.key_values(ws, row, "Configuration highlights", _config_highlights(cfg))
    quality = _quality_items(result)
    if quality:
        row = book.key_values(ws, row, "Data quality", quality)
    if result.timings:
        row = book.key_values(ws, row, "Processing time (s)",
                              [(k, round(float(v), 2)) for k, v in result.timings.items()
                               if _float(v) is not None])
    summary = _executive_summary(llm_notes)
    if summary:
        book.key_values(ws, row, "AI executive summary (LLM-generated - verify before acting)",
                        [("", line) for line in summary])


def _all_candidates_frame(candidates: pd.DataFrame, ai_notes: Mapping[str, str]) -> pd.DataFrame:
    view = candidates.copy()
    if S.INELIGIBILITY in view.columns:
        view[S.INELIGIBILITY] = [ineligibility_text(v) for v in view[S.INELIGIBILITY].tolist()]
    if S.LLM_RATIONALE in view.columns and not view[S.LLM_RATIONALE].map(_clean).astype(bool).any():
        view = view.drop(columns=[S.LLM_RATIONALE])  # legacy re-score rationale, empty since re-scoring was removed
    if ai_notes and S.CANDIDATE_ID in view.columns:
        view["ai_notes"] = [ai_notes.get(_clean(cid), "") for cid in view[S.CANDIDATE_ID].tolist()]
    return view


def _integrity_frame(result: PipelineResult, cfg: AppConfig, ai_notes: Mapping[str, str]) -> pd.DataFrame:
    c = _frame(result.candidates)
    columns = [S.CANDIDATE_ID, S.NAME, S.COLLEGE, S.STATUS, S.RANK, S.FINAL_SCORE, S.HR_PCT,
               S.RISK_LEVEL, S.RISK_SCORE, S.CLUSTER_ID, S.RISK_REASONS, "individual_flags",
               S.INJECTION_FLAGS, S.N_LINKS, "linked_candidates", "link_evidence"]
    if ai_notes:
        columns.append("ai_notes")
    if c.empty or S.CANDIDATE_ID not in c.columns:
        return pd.DataFrame(columns=columns)
    parsed = result.parsed_by_candidate or {}
    levels = _col(c, S.RISK_LEVEL).map(_level)
    flag_levels = {S.RISK_HIGH, S.RISK_MEDIUM} | {_level(x) for x in cfg.shortlist.review_risk_levels or []}
    flag_levels.discard(S.RISK_CLEAR)
    hidden_ids = {str(k) for k, p in parsed.items() if getattr(p, "injection_findings", None)}
    hidden = _col(c, S.INJECTION_FLAGS).map(_clean).astype(bool) | c[S.CANDIDATE_ID].map(_clean).isin(hidden_ids)
    mask = (levels.isin(flag_levels) | _col(c, S.STATUS).map(_clean).isin([S.STATUS_REVIEW, S.STATUS_HOLD])
            | hidden)
    risk = result.risk
    pairs = _frame(risk.pairs) if risk else pd.DataFrame()
    individual = _frame(risk.individual) if risk else pd.DataFrame()
    names = _name_map(c)
    rows = []
    for rec in c.loc[mask].to_dict("records"):
        cid = _clean(rec.get(S.CANDIDATE_ID))
        links = _links_for(pairs, cid)
        evidence = [f"{o} ({names.get(o) or 'unknown'}; pair score {_fmt(_float(s), 0)}): {_clean(sig)}"
                    for o, s, sig in zip(links["other"].head(_MAX_LINKS),
                                         _col(links, "pair_score").head(_MAX_LINKS),
                                         _col(links, "signals").head(_MAX_LINKS))]
        if len(links) > _MAX_LINKS:
            evidence.append(f"... and {len(links) - _MAX_LINKS} more linked candidates (see Risk Pairs)")
        item = {
            S.CANDIDATE_ID: cid, S.NAME: names.get(cid) or rec.get(S.NAME), S.COLLEGE: rec.get(S.COLLEGE),
            S.STATUS: rec.get(S.STATUS), S.RANK: rec.get(S.RANK), S.FINAL_SCORE: rec.get(S.FINAL_SCORE),
            S.HR_PCT: rec.get(S.HR_PCT), S.RISK_LEVEL: _level(rec.get(S.RISK_LEVEL)),
            S.RISK_SCORE: rec.get(S.RISK_SCORE), S.CLUSTER_ID: rec.get(S.CLUSTER_ID),
            S.RISK_REASONS: "\n".join(_split_reasons(rec.get(S.RISK_REASONS))),
            "individual_flags": "\n".join(_individual_flags(individual, cid)),
            S.INJECTION_FLAGS: "\n".join(_injection_lines(rec, parsed)),
            S.N_LINKS: len(links), "linked_candidates": S.SEP_LIST.join(links["other"].tolist()),
            "link_evidence": "\n".join(evidence),
        }
        if ai_notes:
            item["ai_notes"] = ai_notes.get(cid, "")
        rows.append(item)
    out = pd.DataFrame(rows, columns=columns)
    if out.empty:
        return out
    out["_lvl"] = out[S.RISK_LEVEL].map(lambda v: _LEVEL_ORDER.get(v, 9))
    out["_score"] = pd.to_numeric(out[S.RISK_SCORE], errors="coerce").fillna(0.0)
    out = out.sort_values(["_lvl", "_score", S.CANDIDATE_ID], ascending=[True, False, True], kind="mergesort")
    out[S.RANK] = pd.array(pd.to_numeric(out[S.RANK], errors="coerce"), dtype="Int64")
    return out.drop(columns=["_lvl", "_score"]).reset_index(drop=True)


def _clusters_frame(result: PipelineResult, llm_clusters: Mapping[str, Any]) -> pd.DataFrame:
    clusters = _frame(result.risk.clusters if result.risk else None).copy()
    if clusters.empty or not llm_clusters or "cluster_id" not in clusters.columns:
        return clusters
    notes = [llm_clusters.get(_clean(cid)) for cid in clusters["cluster_id"]]
    notes = [n if isinstance(n, Mapping) and n.get("ok") is not False else None for n in notes]
    get = lambda n, k: n.get(k) if isinstance(n, Mapping) else None  # noqa: E731
    clusters["ai_headline"] = [_clean(get(n, "headline")) for n in notes]
    clusters["ai_summary"] = [_clean(get(n, "summary")) for n in notes]
    clusters["ai_actions"] = ["; ".join(_clean(a) for a in get(n, "recommended_actions") or [] if _clean(a))
                              for n in notes]
    return clusters


def _pairs_frame(result: PipelineResult) -> pd.DataFrame:
    pairs = _frame(result.risk.pairs if result.risk else None).copy()
    if pairs.empty or not {"cand_a", "cand_b"} <= set(pairs.columns):
        return pairs
    names = _name_map(_frame(result.candidates))
    for side in ("a", "b"):
        if f"name_{side}" not in pairs.columns:
            pairs.insert(pairs.columns.get_loc(f"cand_{side}") + 1, f"name_{side}",
                         pairs[f"cand_{side}"].map(lambda x: names.get(_clean(x), "")))
    if "pair_score" in pairs.columns:
        pairs["_s"] = pd.to_numeric(pairs["pair_score"], errors="coerce")
        pairs = pairs.sort_values(["_s", "cand_a", "cand_b"], ascending=[False, True, True],
                                  na_position="last", kind="mergesort").drop(columns="_s")
    return pairs.reset_index(drop=True)


def _flatten(prefix: str, value: Any) -> list[tuple[str, Any]]:
    if isinstance(value, Mapping):
        out: list[tuple[str, Any]] = []
        for key in sorted(value, key=str):
            out += _flatten(f"{prefix}.{key}" if prefix else str(key), value[key])
        return out
    if isinstance(value, (list, tuple, set)):
        return [(prefix, json.dumps(list(value), default=str))]
    return [(prefix, value)]


def _write_data_quality(book: _Book, result: PipelineResult) -> None:
    ws = book.wb.add_worksheet("Data Quality")
    q = result.quality
    issues = q.to_frame() if q is not None else pd.DataFrame(
        columns=["severity", "stage", "issue", S.CANDIDATE_ID, "file_name"])
    if not issues.empty:
        order = {"error": 0, "warning": 1, "info": 2}
        issues = (issues.assign(_o=issues["severity"].map(lambda s: order.get(_clean(s), 3)))
                  .sort_values(["_o", "stage"], kind="mergesort").drop(columns="_o"))
    book.table(ws, issues, freeze_cols=0, widths={"issue": 70.0, "severity": 10.0, "stage": 12.0})
    side = len(issues.columns) + 1
    ws.set_column(side, side, 36)
    ws.set_column(side + 1, side + 1, 40)
    row = book.key_values(ws, 0, "Input summary", _quality_items(result), col=side)
    mapping = list((q.column_mapping or {}).items()) if q is not None else []
    if mapping:
        book.key_values(ws, row, "Column mapping (source -> field)",
                        [(str(src), str(dst)) for src, dst in mapping], col=side)


def build_excel_report(result: PipelineResult, llm_notes: Mapping[str, Any] | None = None) -> bytes:
    """Recruiter workbook (xlsxwriter) with the sheets in ``SHEET_NAMES``.

    "Shortlist" and "All Candidates" group their columns by data source under a coloured
    super-header row (row 1; column labels in row 2). "Ineligible Candidates" lists one row per
    violated eligibility rule; the Summary sheet counts them by rule.

    llm_notes (optional; every key optional; notes with "ok": False are skipped):
      "executive_summary": str or {"ok", "markdown"}           -> Summary sheet
      "clusters":   {cluster_id: {"headline", "summary", "recommended_actions"}} -> Risk Clusters
      "candidates": {candidate_id: {"summary", "strengths", "gaps"}}   -> "AI Notes (LLM)" on
                    Shortlist, All Candidates and Integrity Evidence
      "borderline": {candidate_id: {...same shape...}} - explanations for candidates just below the
                    cutoff, shown in the same column prefixed "Near the cutoff:"
    """
    cfg = result.config or AppConfig()
    notes = llm_notes if isinstance(llm_notes, Mapping) else {}
    ai_notes = _candidate_notes(notes)
    llm_clusters = notes.get("clusters") if isinstance(notes.get("clusters"), Mapping) else {}
    candidates = order_columns(_frame(result.candidates))

    buffer = io.BytesIO()
    book = _Book(buffer)
    _write_summary(book, result, cfg, notes, candidates)

    # Shortlist: shortlisted + integrity review, by rank
    listed = candidates.loc[_col(candidates, S.STATUS).map(_clean).isin(IN_SHORTLIST)]
    if S.RANK in listed.columns:
        listed = listed.sort_values(S.RANK, kind="mergesort", na_position="last")
    shortlist = listed[[c for c in _SHORTLIST_COLUMNS if c in listed.columns]].copy()
    if ai_notes and S.CANDIDATE_ID in shortlist.columns:
        shortlist["ai_notes"] = [ai_notes.get(_clean(cid), "") for cid in shortlist[S.CANDIDATE_ID].tolist()]
    book.grouped_table(book.wb.add_worksheet("Shortlist"), shortlist, freeze_cols=3,
                       widths={S.STATUS_REASON: 55.0, S.RISK_REASONS: 60.0, S.TIE_BREAK_NOTE: 50.0})

    book.grouped_table(book.wb.add_worksheet("All Candidates"), _all_candidates_frame(candidates, ai_notes),
                       freeze_cols=0)
    book.table(book.wb.add_worksheet("Ineligible Candidates"), ineligible_rows(candidates), freeze_cols=2,
               wrap=("explanation",), widths={"explanation": 80.0, "rule": 24.0})
    book.table(book.wb.add_worksheet("Integrity Evidence"), _integrity_frame(result, cfg, ai_notes),
               freeze_cols=2, wrap=(S.RISK_REASONS, "individual_flags", S.INJECTION_FLAGS, "link_evidence",
                                    "ai_notes"),
               widths={S.RISK_REASONS: 60.0, "link_evidence": 90.0, "individual_flags": 40.0,
                       S.INJECTION_FLAGS: 60.0})
    book.table(book.wb.add_worksheet("Risk Clusters"), _clusters_frame(result, llm_clusters), freeze_cols=1)
    book.table(book.wb.add_worksheet("Risk Pairs"), _pairs_frame(result), freeze_cols=2)
    book.table(book.wb.add_worksheet("Commonality"),
               _frame(result.risk.commonality if result.risk else None), freeze_cols=2)
    _write_data_quality(book, result)
    config = pd.DataFrame(_flatten("", cfg.to_dict()), columns=["key", "value"])
    book.table(book.wb.add_worksheet("Config"), config, freeze_cols=1, wrap=("value",),
               widths={"key": 42.0, "value": 100.0})

    book.wb.close()
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Evidence pack (markdown)
# ---------------------------------------------------------------------------
def _md(value: Any) -> str:
    """Single-line markdown-safe text (escapes table pipes and emphasis characters)."""
    text = _safe_text(" ".join(_clean(value).split()))
    return re.sub(r"([\\`*_\[\]<>|#])", r"\\\1", text)


def _find_row(candidates: pd.DataFrame, candidate_id: str) -> dict[str, Any] | None:
    if candidates.empty or S.CANDIDATE_ID not in candidates.columns:
        return None
    ids = candidates[S.CANDIDATE_ID].map(_clean)
    hit = candidates.loc[ids == candidate_id]
    if hit.empty:
        hit = candidates.loc[ids.str.upper() == candidate_id.upper()]
    return None if hit.empty else hit.iloc[0].to_dict()


def _duplicate_lines(row: Mapping[str, Any], names: Mapping[str, str], cfg: AppConfig) -> list[str]:
    """Markdown bullet texts about duplicate applications (contact details removed)."""
    primary = _clean(row.get(S.DUPLICATE_OF))
    others = _split_ids(row.get(S.DUPLICATE_IDS))
    lines = []
    if primary:
        text = (f"This application appears to come from the same person as {_md(primary)} "
                f"({_md(names.get(primary)) or 'name unknown'}).")
        if getattr(cfg.shortlist, "auto_disqualify_duplicates", False):
            text += " One application per person is kept, so this one is not ranked."
        lines.append(text)
    if others:
        lines.append("The same person also applied as: "
                     + ", ".join(f"{_md(o)} ({_md(names.get(o)) or 'name unknown'})" for o in others) + ".")
    evidence = _redact(_clean(row.get(S.DUPLICATE_EVIDENCE)))
    if lines and evidence:
        lines.append(f"Evidence: {_md(evidence)}")
    return lines


def _link_details(link: Mapping[str, Any], cfg: AppConfig) -> tuple[list[str], list[str]]:
    """(evidence bullet lines, 'pattern consistent with' themes) for one linked pair."""
    lines: list[str] = []
    themes: list[str] = []
    hashes = _clean(link.get("shared_hashes"))
    if hashes:
        lines.append(f"Identical answer submissions: {_md(hashes)}")
        themes.append("shared solutions")
    sim = _float(link.get("resume_similarity"))
    if sim is not None and sim >= cfg.risk.text_similarity_medium:
        lines.append(f"Resume project/experience text similarity: {sim:.2f} (cosine)")
        themes.append("shared or templated resume content")
    n_bullets = int(_float(link.get("shared_bullets")) or 0)
    if n_bullets:
        examples = [f'"{_md(e)}"' for e in _split_reasons(link.get("shared_bullet_examples"))[:3]]
        lines.append(f"Rare resume bullets shared verbatim: {n_bullets}"
                     + (f" - e.g. {'; '.join(examples)}" if examples else ""))
        themes.append("shared project write-ups")
    github = _clean(link.get("shared_github"))
    if github:
        lines.append(f"Same GitHub repository listed: {_md(github)}")
        themes.append("a shared project")
    network = [t for t, k in (("same IP address", "same_ip"), ("same /24 subnet", "same_subnet"))
               if _truthy(link.get(k))]
    if network:
        lines.append(f"Network: {', '.join(network)}")
        themes.append("a shared test location or network")
    timing = []
    start, submit = _float(link.get("start_gap_sec")), _float(link.get("submit_gap_sec"))
    duration, seq = _float(link.get("duration_gap_min")), _float(link.get("sequential_gap_min"))
    if start is not None and start <= cfg.risk.sync_window_sec:
        timing.append(f"tests started {_fmt(start)} s apart")
    if submit is not None and submit <= cfg.risk.sync_window_sec:
        timing.append(f"submitted {_fmt(submit)} s apart")
    if duration is not None and duration <= cfg.risk.duration_tolerance_min:
        timing.append(f"completion times within {_fmt(duration)} min")
    if seq is not None:
        timing.append(f"one session started {_fmt(seq)} min after the other ended on the same IP")
        themes.append("one person taking both sessions")
    if timing:
        lines.append(f"Timing: {'; '.join(timing)}")
        themes.append("synchronised test sessions")
    identity = [t for t, k in (("same phone number", "same_phone"), ("same email address", "same_email"))
                if _truthy(link.get(k))]
    if identity:
        lines.append(f"Identity: {', '.join(identity)} under different candidate IDs")
        themes.append("duplicate registration")
    if _truthy(link.get("same_college")):
        lines.append("Same college")
    return lines, list(dict.fromkeys(themes))


def build_evidence_pack(result: PipelineResult, candidate_id: str) -> str:
    """Markdown evidence pack for recruiters: profile, score breakdown, duplicate applications,
    risk reasons, hidden resume text, linked candidates with the shared evidence, and neutral
    verification steps. Contact details (email, phone) are never included."""
    cid = _clean(candidate_id)
    candidates = _frame(result.candidates)
    row = _find_row(candidates, cid)
    if row is None:
        return (f"# Integrity evidence pack\n\nCandidate `{_md(cid) or '(blank)'}` was not found in "
                f"this screening run.\n")
    cid = _clean(row.get(S.CANDIDATE_ID))
    cfg = result.config or AppConfig()
    risk = result.risk
    names, colleges = _name_map(candidates), _name_map(candidates, S.COLLEGE)
    level = _level(row.get(S.RISK_LEVEL))
    eligible_n = int(_col(candidates, S.ELIGIBLE).map(_truthy).sum())
    rank = _float(row.get(S.RANK))
    name = names.get(cid) or _display_name(row.get(S.NAME), row.get(S.JOB_APPLICATION))

    out = [f"# Integrity evidence pack: {_md(cid)}", "",
           f"_Generated {_now()} for internal recruiter review. Integrity flags are evidence for "
           f"human review, not findings of misconduct._", "",
           "## Candidate", "", "| Field | Value |", "|---|---|",
           f"| Candidate ID | {_md(cid)} |",
           f"| Name | {_md(name) or '-'} |",
           f"| College | {_md(row.get(S.COLLEGE)) or '-'} |",
           f"| Status | {_md(row.get(S.STATUS)) or '-'} |",
           f"| Rank | {f'{int(rank):,} of {eligible_n:,} eligible' if rank else '-'} |",
           f"| Status reason | {_md(row.get(S.STATUS_REASON)) or '-'} |", "",
           "## Score breakdown", ""]
    out += [f"- {_md(line)}" for line in explain_score(row, cfg)]
    duplicates = _duplicate_lines(row, names, cfg)
    if duplicates:
        out += ["", "## Duplicate applications", ""] + [f"- {line}" for line in duplicates]

    out += ["", f"## Integrity assessment: {level} risk", ""]
    risk_score = _float(row.get(S.RISK_SCORE))
    facts = [f"risk score {_fmt(risk_score, 0)}" if risk_score is not None else "",
             "perfect-scorer focus group" if _truthy(row.get(S.IS_FOCUS)) else "",
             f"cluster {_md(row.get(S.CLUSTER_ID))}" if _clean(row.get(S.CLUSTER_ID)) else ""]
    if any(facts):
        out += [f"**{level}** ({', '.join(f for f in facts if f)})", ""]
    reasons = _split_reasons(row.get(S.RISK_REASONS))
    flags = _individual_flags(_frame(risk.individual if risk else None), cid)
    hidden = _injection_lines(row, result.parsed_by_candidate)
    if level == S.RISK_CLEAR and not reasons and not flags and not hidden:
        out += ["No integrity flags were raised for this candidate. No verification beyond the "
                "standard process is suggested.", ""]
        return "\n".join(out)
    if reasons:
        out += ["Reasons (strongest first):", ""] + [f"- {_md(r)}" for r in reasons] + [""]
    if flags:
        out += ["Individual signals:", ""] + [f"- {_md(f)}" for f in flags] + [""]
    if hidden:
        out += ["Hidden text found in the resume file (not visible to a reader; excluded from scoring):", ""]
        out += [f"- {_md(h)}" for h in hidden] + [""]
    session = [f"started {_md(row.get(S.TEST_START))}" if _clean(row.get(S.TEST_START)) else "",
               f"submitted {_md(row.get(S.SUBMISSION_TIME))}" if _clean(row.get(S.SUBMISSION_TIME)) else "",
               f"{_fmt(_float(row.get(S.COMPLETION_MIN)))} min" if _float(row.get(S.COMPLETION_MIN)) is not None else "",
               _md(row.get(S.TEST_MODE))]
    if any(session):
        out += [f"Assessment session: {', '.join(s for s in session if s)}.", ""]

    links = _links_for(_frame(risk.pairs if risk else None), cid)
    themes: list[str] = ["text hidden from human readers in the resume"] if hidden else []
    if not links.empty:
        shown = links.head(_MAX_LINKS)
        out += ["## Linked candidates", "", "| Candidate | Name | College | Pair score | Signals |",
                "|---|---|---|---|---|"]
        for rec in shown.to_dict("records"):
            o = _clean(rec["other"])
            out.append(f"| {_md(o)} | {_md(names.get(o)) or '-'} | {_md(colleges.get(o)) or '-'} | "
                       f"{_fmt(_float(rec.get('pair_score')), 0)} | {_md(rec.get('signals'))} |")
        if len(links) > _MAX_LINKS:
            out.append(f"\n_{len(links) - _MAX_LINKS} more linked candidates are listed in the Excel report._")
        out += ["", "## Shared evidence", ""]
        for rec in shown.to_dict("records"):
            o = _clean(rec["other"])
            lines, link_themes = _link_details(rec, cfg)
            themes += link_themes
            out.append(f"### With {_md(o)} ({_md(names.get(o)) or 'name unknown'}) - pair score "
                       f"{_fmt(_float(rec.get('pair_score')), 0)}")
            out.append("")
            out += [f"- {line}" for line in lines] or [f"- {_md(rec.get('signals')) or 'See signals above'}"]
            if link_themes:
                out.append(f"- Pattern consistent with: {', '.join(link_themes)}")
            out.append("")

    cluster_id = _clean(row.get(S.CLUSTER_ID))
    clusters = _frame(risk.clusters if risk else None)
    if cluster_id and "cluster_id" in clusters.columns:
        hit = clusters.loc[clusters["cluster_id"].map(_clean) == cluster_id]
        if not hit.empty:
            c = hit.iloc[0].to_dict()
            out += [f"## Cluster {_md(cluster_id)}", "",
                    f"- Members ({_fmt(_float(c.get('size')), 0) or '?'}): {_md(c.get('members'))}",
                    f"- Shared traits: {_md(c.get('shared_traits')) or '-'}",
                    f"- Summary: {_md(c.get('summary')) or '-'}", ""]

    unique = list(dict.fromkeys(themes))
    hashes = sorted({h.strip() for v in _col(links, "shared_hashes") for h in _clean(v).split(",") if h.strip()})
    repos = sorted({g.strip() for v in _col(links, "shared_github") for g in _clean(v).split(",") if g.strip()})
    steps = [
        "Live coding re-test (30-45 min, proctored): ask the candidate to solve and talk through problems "
        + (f"equivalent to {_md(', '.join(hashes))}." if hashes else "of similar difficulty to the assessment."),
        "Project walkthrough: ask the candidate to explain the design, trade-offs and their individual "
        "contribution for the projects on their resume"
        + (f" (including {', '.join(_md(r) for r in repos)})." if repos else "."),
        "Proctored re-assessment with identity verification (photo ID, camera on) if the pattern is "
        "not explained by the steps above.",
    ]
    if hidden:
        steps.insert(0, "Open the original resume file and review the hidden text listed above, then ask the "
                        "candidate to confirm the resume's content and how it was prepared (templates and "
                        "online resume tools can add hidden text).")
    if any(t in unique for t in ("a shared test location or network", "synchronised test sessions",
                                 "one person taking both sessions")):
        steps.append("Check with the placement cell whether the linked candidates were in the same lab or "
                     "scheduled session - a shared network or slot is a legitimate explanation.")
    if "duplicate registration" in unique:
        steps.append("Confirm contact details and identity documents for the linked registrations.")
    steps.append("Apply the same steps to every linked candidate, and record the outcome and the "
                 "candidate's explanation before any decision.")
    out += ["## Recommended verification steps", ""] + [f"{i}. {s}" for i, s in enumerate(steps, 1)]
    out += ["", "> These observations describe a pattern consistent with "
            + (", ".join(unique) if unique else "the risk signals listed above")
            + ". They are not proof of misconduct and may have legitimate explanations; decide only "
              "after verification and in line with the hiring policy.", ""]
    return "\n".join(out)
