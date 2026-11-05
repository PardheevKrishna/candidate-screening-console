"""Ask the Data: plain-English questions answered by a validated query plan run locally.

An LLM translates a question into one small JSON plan::

    {"filters": [{"column", "op", "value"}], "group_by": [...],
     "metrics": [{"column", "agg", "as"}], "columns": [...],
     "sort": [{"column", "desc"}], "limit": int, "chart": "bar" | "none",
     "explanation": str}

or ``{"unsupported": true, "explanation": ...}``. ``validate_plan`` checks every column,
operator and aggregation against whitelists and coerces values to the column types;
``execute_plan`` runs the plan with plain pandas operations only (boolean masks, groupby
aggregations, sort_values, head). Nothing taken from a plan is ever evaluated as code.

Privacy: the model receives the question and a description of the columns (name, type,
label, description, numeric ranges, and example values of a fixed set of low-cardinality,
non-personal columns). It never receives row data. Candidate names, emails, phone numbers,
IP addresses and links typed into the question are swapped for placeholders before the
request and restored locally, so a question about one candidate still works without the
value leaving the application.
"""
from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd

from . import llm
from . import schema as S
from .config import AppConfig

log = logging.getLogger(__name__)

__all__ = [
    "ALLOWED_AGGS", "ALLOWED_OPS", "DEFAULT_LIMIT", "DEFAULT_LIST_COLUMNS", "MAX_LIMIT", "MAX_QUESTION_CHARS",
    "PII_COLUMNS", "AskError", "AskResult", "answer", "build_prompts", "column_info", "column_kind",
    "column_label", "describe_columns", "execute_plan", "is_hidden_column", "is_pii_column", "mask_question",
    "plan_query", "restore_placeholders", "summarize", "validate_plan",
]

# ---------------------------------------------------------------------------
# Whitelists and limits
# ---------------------------------------------------------------------------
ALLOWED_OPS = frozenset({"==", "!=", ">", ">=", "<", "<=", "in", "not_in", "contains", "is_null", "not_null"})
ALLOWED_AGGS = frozenset({"count", "sum", "mean", "median", "min", "max", "nunique"})

EXTRA_PREFIX = "extra_"   # unmapped source columns kept by ingest (may hold anything)


def is_hidden_column(name: object) -> bool:
    """Answer and file hashes: never queryable and never shown in answers."""
    return isinstance(name, str) and (name == S.RESUME_HASH or S.ANSWER_HASH_RE.match(name) is not None)


class _ColumnFamily(frozenset):
    """Fixed column names plus the dynamic families extra_* and q{n}_hash (``in`` covers both)."""

    def __contains__(self, item: object) -> bool:
        return isinstance(item, str) and (frozenset.__contains__(self, item) or item.startswith(EXTRA_PREFIX)
                                          or is_hidden_column(item))


# Personal data. Answers may list these columns (hashes excepted) when asked to,
# but their values are never sent to the LLM.
PII_COLUMNS = _ColumnFamily({
    S.NAME, S.EMAIL, S.PHONE, S.IP_ADDRESS, S.IP_ADDRESSES, S.RESUME_FILE, S.RESUME_HASH, S.REPORT_URL,
    S.PLAGIARISM_MATCH, S.PLAGIARISM_MATCH_ID,
})


def is_pii_column(name: object) -> bool:
    """True for personal-data columns (including every extra_* and q{n}_hash column)."""
    return name in PII_COLUMNS


# The only columns whose values may be shown to the LLM as examples (categorical,
# non-personal, application vocabulary). Everything else is described without values.
_EXAMPLE_COLUMNS = frozenset({
    S.STATUS, S.RISK_LEVEL, S.COLLEGE, S.COLLEGE_TIER, S.DEGREE, S.BRANCH, S.CITY, S.TEST_MODE, S.SOURCE,
    S.EXPERIENCE_SOURCE, S.MATCH_METHOD, S.CLUSTER_ID, S.TEST_SLOT, S.TEST_NAME, S.TEST_STATUS,
    S.INTEGRITY_STATUS, S.DATA_FLAGS, S.MATCHED_SKILLS, S.MISSING_SKILLS,
})
# Comma-separated skill lists: "contains" tests membership of one skill ("Java" != "JavaScript").
_SKILL_LIST_COLUMNS = frozenset({S.MATCHED_SKILLS, S.MISSING_SKILLS, S.SKILLS_FOUND})

DEFAULT_LIST_COLUMNS = [S.CANDIDATE_ID, S.NAME, S.COLLEGE, S.FINAL_SCORE, S.HR_PCT, S.STATUS, S.RISK_LEVEL]
DEFAULT_LIMIT = 50
MAX_LIMIT = 1000
MAX_EXAMPLES = 30
MAX_QUESTION_CHARS = 500
_MAX_FILTERS = 20
_MAX_GROUP_BY = 3
_MAX_METRICS = 8
_MAX_COLUMNS = 40
_MAX_SORT = 5
_MAX_LIST_VALUES = 200
_MAX_VALUE_CHARS = 200
_MAX_EXAMPLE_CHARS = 60
_MAX_PHRASE_WORDS = 8
_REQUIRED_KEYS = ("explanation",)
_NONE_LABEL = "(none)"

_OP_ALIASES = {
    "=": "==", "eq": "==", "equals": "==", "is": "==", "ne": "!=", "neq": "!=", "<>": "!=", "not": "!=",
    "gt": ">", "gte": ">=", "ge": ">=", "=>": ">=", "lt": "<", "lte": "<=", "le": "<=", "=<": "<=",
    "not in": "not_in", "notin": "not_in", "nin": "not_in", "not-in": "not_in", "includes": "contains",
    "like": "contains", "is null": "is_null", "isnull": "is_null", "null": "is_null", "is_none": "is_null",
    "is not null": "not_null", "notnull": "not_null", "not null": "not_null", "is_not_null": "not_null",
}
_AGG_ALIASES = {
    "avg": "mean", "average": "mean", "total": "sum", "cnt": "count", "size": "count", "n": "count",
    "distinct": "nunique", "count_distinct": "nunique", "unique": "nunique", "minimum": "min", "maximum": "max",
}
_TRUE_TEXT = frozenset({"true", "yes", "y", "t", "1"})
_FALSE_TEXT = frozenset({"false", "no", "n", "f", "0"})


class AskError(Exception):
    """User-presentable problem with a question or a query plan (never contains row data)."""


# ---------------------------------------------------------------------------
# Column catalogue
# ---------------------------------------------------------------------------
_STATUS_TEXT = ", ".join(S.STATUS_ORDER)
_RISK_TEXT = ", ".join(S.RISK_ORDER)
_INFO: dict[str, tuple[str, str]] = {
    S.RANK: ("Rank", "Shortlist rank among eligible candidates (1 = best); empty when ineligible."),
    S.STATUS: ("Status", f"Screening outcome, one of: {_STATUS_TEXT}."),
    S.STATUS_REASON: ("Status reason", "Why the candidate received this status."),
    S.CANDIDATE_ID: ("Candidate ID", "Unique candidate identifier."),
    S.NAME: ("Name", "Candidate full name."),
    S.EMAIL: ("Email", "Candidate email address."),
    S.PHONE: ("Phone", "Candidate phone number (digits)."),
    S.COLLEGE: ("College", "College or university."),
    S.COLLEGE_TIER: ("College tier", "Institution tier stored as text: '1', '2' or '3'."),
    S.DEGREE: ("Degree", "Degree, e.g. B.Tech."),
    S.BRANCH: ("Branch", "Branch or stream of study."),
    S.GRAD_YEAR: ("Graduation year", "Year of graduation."),
    S.CGPA: ("CGPA", "CGPA on a 10-point scale."),
    S.CITY: ("City", "Candidate city or location."),
    S.FINAL_SCORE: ("Final score", "Weighted rubric score from 0 to 100 (HackerRank, skills, experience, "
                                   "education)."),
    S.PTS_HR: ("HackerRank points", "Rubric points earned for the HackerRank component."),
    S.PTS_SKILLS: ("Skills points", "Rubric points earned for the skills component."),
    S.PTS_EXPERIENCE: ("Experience points", "Rubric points earned for the experience & projects component."),
    S.PTS_EDUCATION: ("Education points", "Rubric points earned for the education component."),
    S.SUB_HR: ("HackerRank sub-score", "HackerRank component score from 0 to 1 before weighting."),
    S.SUB_SKILLS: ("Skills sub-score", "Skills component score from 0 to 1 before weighting."),
    S.SUB_EXPERIENCE: ("Experience sub-score", "Experience & projects component score from 0 to 1 before "
                                               "weighting."),
    S.SUB_EDUCATION: ("Education sub-score", "Education component score from 0 to 1 before weighting."),
    S.HR_SCORE: ("HackerRank score", "Raw assessment score."),
    S.MAX_SCORE: ("Max score", "Maximum possible assessment score."),
    S.HR_PCT: ("HackerRank %", "Assessment percentage from 0 to 100; 100 means a perfect score."),
    S.RISK_LEVEL: ("Risk level", f"Integrity risk level, one of: {_RISK_TEXT}."),
    S.RISK_SCORE: ("Risk score", "Integrity risk score from 0 to 100."),
    S.RISK_REASONS: ("Risk reasons", "Integrity evidence, ' | '-separated."),
    S.CLUSTER_ID: ("Cluster", "Integrity risk cluster ID such as C01; empty when the candidate is not in a "
                              "cluster."),
    S.IS_FOCUS: ("Perfect scorer", "True for candidates in the integrity focus group (perfect assessment "
                                   "scores)."),
    S.N_LINKS: ("Integrity links", "Number of integrity links to other candidates."),
    S.MAX_PAIR_SCORE: ("Max pair score", "Strongest integrity link to another candidate (0-100)."),
    S.INDIVIDUAL_SCORE: ("Individual flag score", "Risk points from individual integrity flags."),
    S.MATCHED_SKILLS: ("Matched skills", "Required skills found in the resume, comma-separated; use 'contains' "
                                         "with one skill."),
    S.MISSING_SKILLS: ("Missing skills", "Required skills not found in the resume, comma-separated; use "
                                         "'contains' with one skill."),
    S.N_MATCHED_SKILLS: ("Matched skill count", "Number of required skills found in the resume."),
    S.SKILLS_FOUND: ("Skills found", "All skills found in the resume, comma-separated; use 'contains' with one "
                                     "skill."),
    S.INTERNSHIP_COUNT: ("Internships", "Number of internships on the resume."),
    S.EXPERIENCE_MONTHS: ("Experience (months)", "Total months of experience on the resume."),
    S.PROJECT_COUNT: ("Projects", "Number of projects on the resume."),
    S.CERTIFICATIONS: ("Certifications", "Certifications listed on the resume, '; '-separated."),
    S.CERT_COUNT: ("Certification count", "Number of relevant certifications."),
    S.RESUME_CGPA: ("Resume CGPA", "CGPA parsed from the resume (10-point scale)."),
    S.RESUME_DEGREE: ("Resume degree", "Degree parsed from the resume."),
    S.RELEVANCE: ("Job relevance", "Similarity of resume projects and experience to the job description "
                                   "(0-1)."),
    S.EXPERIENCE_SOURCE: ("Experience source", "How the experience component was scored."),
    S.LLM_RATIONALE: ("LLM rationale", "LLM note on the experience score; empty when not used."),
    S.SCORE_NOTES: ("Score notes", "Scoring notes, '; '-separated."),
    S.TEST_START: ("Test start time", "When the assessment attempt started."),
    S.SUBMISSION_TIME: ("Submission time", "When the assessment was submitted."),
    S.COMPLETION_MIN: ("Completion (min)", "Minutes taken to complete the assessment."),
    S.IP_ADDRESS: ("IP address", "IP address used for the assessment."),
    S.IP_ADDRESSES: ("IP addresses", "All IP addresses listed for the attempt."),
    S.IP_SUBNET: ("IP subnet (/24)", "IPv4 /24 subnet of the assessment IP address."),
    S.TEST_MODE: ("Test mode", "Remote or On-campus."),
    S.TAB_SWITCHES: ("Tab switches", "Times the candidate left the test window."),
    S.COPY_PASTE: ("Copy-paste events", "Paste events in the code editor."),
    S.PLAGIARISM_FLAG: ("Plagiarism flag", "True when HackerRank flagged possible plagiarism."),
    S.SOURCE: ("Source", "Sourcing channel."),
    S.EMAIL_DOMAIN: ("Email domain", "Part of the email address after '@'."),
    S.TEST_SLOT: ("Test slot", "Test start time floored to the hour, as text 'YYYY-MM-DD HH:00'."),
    S.RESUME_FILE: ("Resume file", "File name of the linked resume."),
    S.HAS_RESUME: ("Has resume", "True when a resume is linked to the candidate."),
    S.MATCH_METHOD: ("Resume match method", "How the resume was linked to the candidate."),
    S.MATCH_CONFIDENCE: ("Resume match confidence", "Confidence of the resume link (0-1)."),
    S.ELIGIBLE: ("Eligible", "True when the candidate passed the eligibility filters."),
    S.DATA_FLAGS: ("Data flags", "Data-quality flags, '; '-separated."),
    S.COLLEGE_CAPPED: ("College capped", "True when the college cap kept the candidate out of the shortlist."),
    S.TEST_NAME: ("Test name", "Assessment name."),
    S.TEST_STATUS: ("Test status", "Assessment attempt status."),
    S.OUT_OF_WINDOW_SEC: ("Out-of-window (sec)", "Seconds spent outside the test window."),
    S.SECURE_MODE_EXITS: ("Secure-mode exits", "Times the candidate left secure / full-screen mode."),
    S.MAX_CODE_SIMILARITY: ("Max code similarity %", "Highest code similarity to another submission (0-100)."),
    S.PLAGIARISM_MATCH: ("Most similar candidate", "ID or email of the most similar other candidate, as "
                                                   "exported."),
    S.PLAGIARISM_MATCH_ID: ("Most similar candidate ID", "Candidate ID of the most similar other candidate."),
    S.INTEGRITY_STATUS: ("Integrity status", "AI-proctoring verdict."),
    S.PROCTORING_FLAGS: ("Proctoring flags", "Proctoring notes."),
    S.REPORT_URL: ("Report URL", "Link to the detailed assessment report."),
}
_LABELS: dict[str, str] = {**{k: v[0] for k, v in _INFO.items()}, **S.TA_FIELD_LABELS}


def column_info(name: str) -> tuple[str, str]:
    """(label, description) for a column; dynamic families get generated text."""
    label = _LABELS.get(name)
    desc = _INFO.get(name, ("", ""))[1]
    if label is not None:
        return label, desc
    if m := S.QUESTION_SCORE_RE.match(name):
        return f"Q{m.group(1)} score", f"Score on assessment question {m.group(1)}."
    if m := S.QUESTION_SIMILARITY_RE.match(name):
        return f"Q{m.group(1)} code similarity %", f"Code similarity on question {m.group(1)} (0-100)."
    if name.startswith(EXTRA_PREFIX):
        header = name[len(EXTRA_PREFIX):].replace("_", " ").strip() or name
        return f"{header} (source column)", "Unmapped column from the source file."
    return name.replace("_", " ").strip().capitalize() or name, ""


def column_label(name: str) -> str:
    return column_info(name)[0] if isinstance(name, str) else str(name)


def column_kind(series: pd.Series) -> str:
    """'number' | 'text' | 'bool' | 'datetime' from the dtype (object columns are inspected)."""
    dtype = series.dtype
    if pd.api.types.is_bool_dtype(dtype):
        return "bool"
    if pd.api.types.is_datetime64_any_dtype(dtype):
        return "datetime"
    if pd.api.types.is_numeric_dtype(dtype):
        return "number"
    if dtype == object:
        values = series.dropna()
        if len(values):
            if values.map(lambda v: isinstance(v, (bool, np.bool_))).all():
                return "bool"
            if values.map(lambda v: isinstance(v, (int, float, np.integer, np.floating))
                          and not isinstance(v, (bool, np.bool_))).all():
                return "number"
            if values.map(lambda v: isinstance(v, (datetime, pd.Timestamp))).all():
                return "datetime"
    return "text"


def _unique_columns(df: pd.DataFrame) -> pd.DataFrame:
    """The frame with repeated column labels dropped (first occurrence kept)."""
    dup = df.columns.duplicated()
    return df.loc[:, ~dup] if dup.any() else df


def _visible_columns(df: pd.DataFrame) -> list[str]:
    """String-named, non-hash, non-duplicated columns in frame order."""
    dup = df.columns.duplicated()
    return [c for c, d in zip(df.columns, dup) if isinstance(c, str) and not d and not is_hidden_column(c)]


def describe_columns(df: pd.DataFrame) -> list[dict[str, Any]]:
    """Column catalogue sent to the LLM: names, kinds, labels, descriptions, numeric and date
    ranges, and examples only for allow-listed categorical columns. No row data, no personal values."""
    if not isinstance(df, pd.DataFrame):
        return []
    df = _unique_columns(df)
    full_names = _name_index(df)[0]
    out: list[dict[str, Any]] = []
    for col in _visible_columns(df):
        series = df[col]
        kind = column_kind(series)
        label, desc = column_info(col)
        item: dict[str, Any] = {"name": col, "kind": kind, "label": label}
        if desc:
            item["description"] = desc
        if is_pii_column(col):
            item["personal"] = True          # values withheld
        elif kind == "number":
            values = _num(series).dropna()
            if len(values):
                item["min"], item["max"] = _json_number(values.min()), _json_number(values.max())
        elif kind == "datetime":
            values = _dt(series).dropna()
            if len(values):
                item["min"], item["max"] = _iso(values.min()), _iso(values.max())
        elif kind == "text" and col in _EXAMPLE_COLUMNS:
            examples = _examples(series, full_names)
            if examples:
                item["examples"] = examples
        out.append(item)
    return out


def _examples(series: pd.Series, full_names: set[str]) -> list[str]:
    """Distinct values (most frequent first) when there are at most MAX_EXAMPLES and none of
    them looks like contact data, a candidate's name or free text; otherwise []."""
    text = _text(series)
    counts = text[text != ""].value_counts()
    if counts.empty or len(counts) > MAX_EXAMPLES:
        return []
    ordered = sorted(((str(v), int(n)) for v, n in counts.items()), key=lambda vn: (-vn[1], vn[0]))
    values = [v for v, _ in ordered]
    for v in values:
        if len(v) > _MAX_EXAMPLE_CHARS or llm.redact_pii(v) != v or _contains_full_name(v, full_names):
            return []
    return values


# ---------------------------------------------------------------------------
# Small conversions (tolerant of NaN / numpy / nullable dtypes)
# ---------------------------------------------------------------------------
def _num(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").astype("float64")


def _dt(series: pd.Series) -> pd.Series:
    if pd.api.types.is_datetime64_any_dtype(series.dtype):
        return series
    return pd.to_datetime(series, errors="coerce", format="mixed")


def _text(series: pd.Series) -> pd.Series:
    """Stripped text with missing values as ''."""
    return series.astype("string").fillna("").str.strip()


def _is_missing_scalar(value: Any) -> bool:
    if value is None or value is pd.NA or value is pd.NaT:
        return True
    return isinstance(value, (float, np.floating)) and math.isnan(value)


def _bool_series(series: pd.Series) -> pd.Series:
    if pd.api.types.is_bool_dtype(series.dtype):
        return series.astype("boolean")

    def conv(value: Any) -> Any:
        if isinstance(value, (bool, np.bool_)):
            return bool(value)
        if _is_missing_scalar(value):
            return pd.NA
        token = str(value).strip().lower()
        return True if token in _TRUE_TEXT else False if token in _FALSE_TEXT else pd.NA

    return series.map(conv).astype("boolean")


def _as_mask(values: pd.Series) -> pd.Series:
    return values.fillna(False).astype(bool)


def _json_number(value: Any) -> int | float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return int(v) if v.is_integer() and abs(v) < 1e15 else round(v, 4)


def _iso(value: Any) -> str:
    ts = pd.Timestamp(value)
    return ts.isoformat(sep=" ", timespec="minutes") if not pd.isna(ts) else ""


def _clip(text: Any, limit: int) -> str:
    s = " ".join(str(text).split())
    s = "".join(ch for ch in s if ch.isprintable())
    return s if len(s) <= limit else s[: max(0, limit - 1)].rstrip() + "…"


def _truthy(value: Any) -> bool:
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in _TRUE_TEXT
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 1
    return False


# ---------------------------------------------------------------------------
# Question masking (personal values never leave the application)
# ---------------------------------------------------------------------------
_WORD_RE = re.compile(r"[^\W\d_]+")
_EMAIL_RE = re.compile(r"(?<![\w.%+-])[\w.%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_URL_RE = re.compile(r"(?:\b(?:https?|ftp)://|\bwww\.)[^\s<>\"'`]+", re.IGNORECASE)
# Bare domains with a lowercase TLD; the first label needs >= 2 chars so "B.Tech" survives.
_DOMAIN_RE = re.compile(
    r"(?<![\w@./-])(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"(?:com|org|net|edu|gov|io|dev|me|ai|app|co|in|uk|us|tech|xyz|site|info|page|cloud|blog|live|online|so)"
    r"(?![\w-])(?:/[^\s<>\"'`]*)?")
_NOT_DOMAINS = frozenset({"asp.net", "vb.net", "ado.net", "socket.io"})
_IPV4_RE = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?(?!\w|\.\d)")
# Four or more groups, or a "::" form - so clock times such as 10:30:45 are left alone.
_IPV6_RE = re.compile(
    r"(?<![\w:])(?:(?:[0-9A-Fa-f]{1,4}:){3,7}[0-9A-Fa-f]{1,4}"
    r"|(?:[0-9A-Fa-f]{1,4}:)+:(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4})*)?)(?![\w:])")
_PHONE_RE = re.compile(r"(?<![\w+])\+?\d(?:[ ().-]{0,3}\d){9,12}(?!\w)")
_DATE_LIKE_RE = re.compile(r"^\(?(?:(?:19|20)\d\d[-./]\d{1,2}[-./]\d{1,2}|\d{1,2}[-./]\d{1,2}[-./](?:19|20)\d\d)")
_DIGITS_RE = re.compile(r"\d+")
_TRAILING_PUNCT = ".,;:!?)]}'\""
_PLACEHOLDER_RE = re.compile(r"\[?\b(NAME|EMAIL|PHONE|IP|URL)_(\d{1,3})\b\]?", re.IGNORECASE)
_CONTACT_PATTERNS = (("EMAIL", _EMAIL_RE), ("URL", _URL_RE), ("URL", _DOMAIN_RE), ("IP", _IPV4_RE),
                     ("IP", _IPV6_RE), ("PHONE", _PHONE_RE))

# Words that are never treated as a candidate's name when typed on their own.
_COMMON_WORDS = frozenset("""
a an the and or nor but if then else than as at by for from in into of on onto per to with without within via
over under above below between among across after before since until during about around near up down out off
i me my we us our you your he him his she her they them their it its this that these those who whom whose which
what where when why how is are was were be been being am do does did done have has had having get gets got give
gives show shows list lists find tell display return count counts compute calculate rank ranked ranking sort
sorted order ordered group grouped split break filter filtered include includes including exclude excluding
compare compared versus vs make please can could would should will shall may might must
all any each every some many much more most less least few fewer number numbers total totals sum average avg mean
median min minimum max maximum top bottom best worst highest lowest high higher low lower greater smaller larger
largest smallest equal equals only just also same different distinct unique overall other others both either
neither not no yes true false none null empty missing blank unknown new old first last next previous
day days week weeks month months year years today yesterday tomorrow time times date dates hour hours minute
minutes mins second seconds sec morning afternoon evening night
january february march april june july august september october november december jan feb mar apr jun jul aug sep
sept oct nov dec monday tuesday wednesday thursday friday saturday sunday
candidate candidates applicant applicants student students people person persons profile profiles perfect scorer
scorers score scores scored scoring mark marks point points pct percent percentage percentages ratio rate rates
share shares test tests assessment assessments exam exams question questions answer answers college colleges
university universities institute institutes institution campus campuses tier tiers city cities location
locations branch branches degree degrees stream streams graduation graduating batch cgpa gpa skill skills resume
resumes cv internship internships intern interns project projects experience certification certifications
certified risk risks risky integrity review reviewed hold held shortlist shortlists shortlisted shortlisting
ineligible eligible eligibility status statuses cluster clusters clustered pair pairs link links linked flag
flags flagged clear medium ip ips address addresses email emails mail phone phones mobile name names remote
onsite oncampus source sources referral referrals mode plagiarism plagiarised plagiarized similarity similar copy
paste tab tabs switch switches window proctoring proctored suspicious speed fast faster fastest slow slower
slowest quick quickest submission submissions submitted start started end ended duration completion hackerrank
rubric weight weights component components education final notes reason reasons evidence focus group groups
breakdown distribution summary details detail info information data table row rows column columns value values
chart graph plot percentile
""".split())


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text.casefold())


def _name_index(df: pd.DataFrame | None) -> tuple[set[str], set[str]]:
    """(full names as casefolded word strings, name tokens of >= 3 letters) from the name column."""
    if not isinstance(df, pd.DataFrame) or S.NAME not in df.columns:
        return set(), set()
    full: set[str] = set()
    tokens: set[str] = set()
    for value in _text(_unique_columns(df)[S.NAME]).unique():
        words = _words(str(value))
        if len(words) >= 2:
            full.add(" ".join(words))
        tokens.update(w for w in words if len(w) >= 3)
    return full, tokens


def _contains_full_name(text: str, full_names: set[str]) -> bool:
    if not full_names:
        return False
    words = _words(text)
    return any(" ".join(words[i:i + n]) in full_names
               for n in range(2, min(5, len(words)) + 1) for i in range(len(words) - n + 1))


def _safe_phrases(df: pd.DataFrame | None) -> set[str]:
    """Whole values of non-personal categorical columns (a college named after a person, a city);
    a question quoting one of them is about that value, not about a candidate."""
    phrases: set[str] = set()
    if not isinstance(df, pd.DataFrame):
        return phrases
    df = _unique_columns(df)
    for col in _EXAMPLE_COLUMNS:
        if col not in df.columns or column_kind(df[col]) != "text":
            continue
        uniq = _text(df[col]).unique()
        if len(uniq) > 5000:
            continue
        for value in uniq:
            items = str(value).split(",") if col in _SKILL_LIST_COLUMNS else [str(value)]
            for item in items:
                words = _words(item)
                if 0 < len(words) <= _MAX_PHRASE_WORDS:
                    phrases.add(" ".join(words))
    return phrases


def _vocabulary(df: pd.DataFrame | None) -> set[str]:
    words = set(_COMMON_WORDS)
    for value in [*S.STATUS_ORDER, *S.RISK_ORDER, *_LABELS.values(), *_LABELS.keys()]:
        words.update(_words(value))
    if isinstance(df, pd.DataFrame):
        for col in df.columns:
            if isinstance(col, str):
                words.update(_words(col.replace("_", " ")))
                words.update(_words(column_label(col)))
    return words


def _accept_contact(kind: str, value: str) -> bool:
    if kind == "URL" and "://" not in value and not value.lower().startswith("www."):
        host = value.split("/", 1)[0].lower()
        return host not in _NOT_DOMAINS and len(host.split(".", 1)[0]) >= 2
    if kind == "IP" and "." in value:
        return all(int(part) <= 255 for part in value.split("/", 1)[0].split("."))
    if kind == "PHONE":
        groups = _DIGITS_RE.findall(value)
        if all(len(g) == 4 and g[:2] in ("19", "20") for g in groups):   # "2024 2025 2026"
            return False
        if sum(len(g) == 1 for g in groups) >= 3:                        # "8.5 9.0 8.7 9.1"
            return False
        return not _DATE_LIKE_RE.match(value)                            # "2026-10-20 10"
    return True


def _restored_value(kind: str, original: str) -> str:
    if kind == "PHONE":   # ingest keeps digits only, last 10
        return "".join(_DIGITS_RE.findall(original))[-10:]
    if kind == "EMAIL":
        return original.lower()
    return original


def mask_question(question: str, df: pd.DataFrame | None = None) -> tuple[str, dict[str, str]]:
    """Replace emails, links, IPs, phone numbers and candidate names with [KIND_n] placeholders.

    Returns (masked question, {"NAME_1": original, ...}). Names are matched against the frame's
    name column (full names, then single name tokens that are not ordinary words); whole values
    of categorical columns (e.g. a college named after a person) are left alone. The result is
    passed through ``llm.redact_pii`` as a final safety net.
    """
    text = question if isinstance(question, str) else ""
    spans: list[tuple[int, int, str, str]] = []

    def free(a: int, b: int) -> bool:
        return all(b <= s or a >= e for s, e, _, _ in spans)

    for kind, pattern in _CONTACT_PATTERNS:
        for m in pattern.finditer(text):
            core = m.group(0).rstrip(_TRAILING_PUNCT)
            a, b = m.start(), m.start() + len(core)
            if core and free(a, b) and _accept_contact(kind, core):
                spans.append((a, b, kind, core))

    full_names, tokens = _name_index(df)
    if full_names or tokens:
        words = [(m.group(0).casefold(), m.start(), m.end()) for m in _WORD_RE.finditer(text)]
        n_words = len(words)
        safe = [False] * n_words
        phrases = _safe_phrases(df)
        for n in range(min(_MAX_PHRASE_WORDS, n_words), 0, -1):
            for i in range(n_words - n + 1):
                if not any(safe[i:i + n]) and " ".join(w for w, _, _ in words[i:i + n]) in phrases:
                    safe[i:i + n] = [True] * n
        taken = list(safe)
        for n in range(min(5, n_words), 1, -1):
            for i in range(n_words - n + 1):
                if any(taken[i:i + n]):
                    continue
                if " ".join(w for w, _, _ in words[i:i + n]) in full_names:
                    a, b = words[i][1], words[i + n - 1][2]
                    if free(a, b):
                        spans.append((a, b, "NAME", text[a:b]))
                        taken[i:i + n] = [True] * n
        common = _vocabulary(df)
        for idx, (w, a, b) in enumerate(words):
            if not taken[idx] and len(w) >= 3 and w in tokens and w not in common and free(a, b):
                spans.append((a, b, "NAME", text[a:b]))

    mapping: dict[str, str] = {}
    seen: dict[tuple[str, str], str] = {}
    counters: dict[str, int] = {}
    pieces: list[str] = []
    last = 0
    for a, b, kind, original in sorted(spans):
        key = (kind, original.casefold())
        placeholder = seen.get(key)
        if placeholder is None:
            counters[kind] = counters.get(kind, 0) + 1
            placeholder = f"{kind}_{counters[kind]}"
            seen[key] = placeholder
            mapping[placeholder] = _restored_value(kind, original)
        pieces += [text[last:a], f"[{placeholder}]"]
        last = b
    pieces.append(text[last:])
    return llm.redact_pii("".join(pieces)), mapping


def restore_placeholders(obj: Any, mapping: dict[str, str]) -> Any:
    """Put the original values back into every string of a plan (keys are left alone)."""
    if not mapping:
        return obj
    if isinstance(obj, str):
        return _PLACEHOLDER_RE.sub(lambda m: mapping.get(f"{m.group(1).upper()}_{m.group(2)}", m.group(0)), obj)
    if isinstance(obj, list):
        return [restore_placeholders(v, mapping) for v in obj]
    if isinstance(obj, dict):
        return {k: restore_placeholders(v, mapping) for k, v in obj.items()}
    return obj


# ---------------------------------------------------------------------------
# Prompts + planning
# ---------------------------------------------------------------------------
def _system_prompt(cfg: AppConfig | None) -> str:
    cfg = cfg if isinstance(cfg, AppConfig) else AppConfig()
    skills = ", ".join(str(s) for s in cfg.skills.required_skills) or "none configured"
    shortlisted = json.dumps([S.STATUS_SHORTLISTED, S.STATUS_REVIEW])
    flagged = json.dumps([S.RISK_HIGH, S.RISK_MEDIUM])
    ops = " ".join(sorted(ALLOWED_OPS))
    aggs = " ".join(sorted(ALLOWED_AGGS))
    return f"""You convert a recruiter's question about a campus-hiring screening table into ONE JSON query plan.
The application validates the plan and runs it locally; you never see the rows.

Return ONLY one JSON object (no prose, no markdown, no code fences) with these keys:
{{"filters": [{{"column": "<name>", "op": "<op>", "value": <value>}}],
 "group_by": ["<name>"],
 "metrics": [{{"column": "<name>", "agg": "<agg>", "as": "<result name>"}}],
 "columns": ["<name>"],
 "sort": [{{"column": "<name or result name>", "desc": true}}],
 "limit": 50,
 "chart": "bar",
 "explanation": "<one plain-English sentence describing the plan>"}}

Rules:
- Use only column names from the "name" fields of the column list, spelled exactly.
- op is one of: {ops}. "in" and "not_in" take a JSON list; "is_null" and "not_null" take no value; "contains" is a case-insensitive substring test on text columns (on comma-separated skill lists it checks for one skill). Text comparisons ignore case.
- agg is one of: {aggs}. Count candidates with {{"column": "candidate_id", "agg": "count", "as": "candidates"}}. sum, mean and median need a number or true/false column.
- Use the values listed under "examples" when a column has them. Numbers are JSON numbers; dates use "YYYY-MM-DD HH:MM".
- To list candidates, leave group_by and metrics empty and name the columns to show in "columns" (candidate_id first, at most 12).
- For "per X", "by X" or "which X has the most" questions, group_by [X] with metrics and sort by the metric (desc for most/highest).
- sort may use group_by columns and metric "as" names in grouped plans, or any listed column otherwise.
- limit is the number of rows to return (default 50, maximum 1000). chart is "bar" for one group_by column with one metric, otherwise "none".
- Never invent columns, values or numbers, and do not answer the question yourself: only produce the plan.
- If the question cannot be answered from these columns (it needs other data, asks for opinions or predictions, concerns protected attributes such as gender, age, religion, caste, ethnicity, nationality, marital status or disability, or asks to change data), return {{"unsupported": true, "explanation": "<why, in one sentence>"}}.
- Placeholders such as [NAME_1], [EMAIL_1], [PHONE_1], [IP_1] or [URL_1] stand for values the user typed that are hidden from you. Copy them verbatim as filter values, e.g. {{"column": "name", "op": "contains", "value": "[NAME_1]"}}.

Vocabulary:
- status is one of: {_STATUS_TEXT}. "Shortlisted" candidates means status in {shortlisted}; "under integrity review" means status == "{S.STATUS_REVIEW}"; "on hold" means status == "{S.STATUS_HOLD}".
- risk_level is one of: {_RISK_TEXT}. "Flagged" or "high/medium risk" means risk_level in {flagged}.
- "Perfect scorers" means hackerrank_pct >= 100.
- cluster_id groups linked candidates (C01, C02, ...); it is empty for candidates outside a cluster (use not_null to keep clustered candidates).
- Required skills for this role: {skills}."""


def _user_prompt(question: str, df: pd.DataFrame) -> str:
    catalogue = json.dumps(describe_columns(df), ensure_ascii=False, separators=(",", ":"), default=str)
    return (f"Question: {question}\n\n"
            f"The table has one row per candidate ({len(df):,} rows). Columns:\n{catalogue}\n\n"
            "Return the JSON plan.")


def _clean_question(question: Any) -> str:
    if not isinstance(question, str):
        return ""
    q = "".join(ch for ch in " ".join(question.split()) if ch.isprintable())
    return q[:MAX_QUESTION_CHARS].strip()


def build_prompts(question: str, df: pd.DataFrame, cfg: AppConfig | None = None
                  ) -> tuple[str, str, dict[str, str]]:
    """(system prompt, user prompt, placeholder mapping) exactly as plan_query sends them."""
    masked, mapping = mask_question(_clean_question(question), df)
    return _system_prompt(cfg), _user_prompt(masked, df), mapping


_FENCE_RE = re.compile(r"^```[A-Za-z]*\s*(.*?)\s*```$", re.DOTALL)


def _as_plan_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        fenced = _FENCE_RE.match(text)
        try:
            parsed = json.loads(fenced.group(1) if fenced else text)
        except (ValueError, RecursionError):
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    raise AskError("The LLM reply was not a valid JSON query plan. Try rephrasing the question.")


def plan_query(question: str, df: pd.DataFrame, client: Any, cfg: AppConfig | None = None) -> dict[str, Any]:
    """Ask the LLM for a query plan (raw, not yet validated; placeholders restored).

    Raises AskError with a friendly message when there is no data or client, or the LLM fails
    or replies with something that is not a JSON object.
    """
    if not isinstance(df, pd.DataFrame) or df.empty:
        raise AskError("There are no screening results to query yet. Run a screening first.")
    q = _clean_question(question)
    if not q:
        raise AskError("Type a question about the screening results first.")
    if client is None or not hasattr(client, "chat_json"):
        raise AskError("The LLM is not configured, so the question cannot be turned into a query plan.")
    system, user, mapping = build_prompts(q, df, cfg)
    try:
        raw = client.chat_json(system, user, required_keys=_REQUIRED_KEYS)
    except llm.LLMError as exc:
        raise AskError(f"The LLM could not plan this question: {_clip(exc, 300)}") from None
    except Exception as exc:  # noqa: BLE001 - any client failure becomes a friendly message
        log.warning("Ask the Data: LLM call failed (%s)", type(exc).__name__)
        raise AskError(f"The LLM request failed ({type(exc).__name__}). Check the LLM settings and try "
                       "again.") from None
    return restore_placeholders(_as_plan_dict(raw), mapping)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def _norm_ref(value: str) -> str:
    return re.sub(r"[\s_\-]+", "", value.casefold())


class _Columns:
    """Column whitelist for one frame: name/label resolution and cached kinds."""

    def __init__(self, df: pd.DataFrame) -> None:
        self.df = df = _unique_columns(df)
        self.visible = _visible_columns(df)
        self._exact = set(self.visible)
        self._lookup: dict[str, str] = {}
        for col in self.visible:
            self._lookup.setdefault(_norm_ref(column_label(col)), col)
        for col in self.visible:
            self._lookup[_norm_ref(col)] = col
        self._kinds: dict[str, str] = {}

    def resolve(self, ref: Any, where: str) -> str:
        if not isinstance(ref, str) or not ref.strip():
            raise AskError(f"Every entry in {where} needs a column name.")
        name = ref.strip()
        if name in self._exact:
            return name
        if is_hidden_column(name):
            raise AskError(f"'{_clip(name, 60)}' cannot be used: answer and resume hashes are not available "
                           "for questions.")
        hit = self._lookup.get(_norm_ref(name))
        if hit is None:
            raise AskError(f"Unknown column '{_clip(name, 60)}' in {where}.")
        return hit

    def kind(self, col: str) -> str:
        if col not in self._kinds:
            self._kinds[col] = column_kind(self.df[col])
        return self._kinds[col]


def _as_list(value: Any, where: str, max_items: int) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, (str, dict)):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise AskError(f"'{where}' must be a list.")
    if len(value) > max_items:
        raise AskError(f"Too many entries in '{where}' (at most {max_items}).")
    return list(value)


def _to_number(value: Any, label: str) -> int | float:
    if isinstance(value, (bool, np.bool_)) or value is None:
        raise AskError(f"{label} needs a number.")
    if isinstance(value, (int, float, np.integer, np.floating)):
        v = float(value)
    elif isinstance(value, str):
        cleaned = value.strip().replace(",", "").rstrip("%").strip()
        try:
            v = float(cleaned)
        except ValueError:
            raise AskError(f"'{_clip(value, 40)}' is not a number ({label}).") from None
    else:
        raise AskError(f"{label} needs a number.")
    if not math.isfinite(v):
        raise AskError(f"{label} needs a finite number.")
    return int(v) if v.is_integer() and abs(v) < 1e15 else v


def _to_bool(value: Any, label: str) -> bool:
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _TRUE_TEXT:
            return True
        if token in _FALSE_TEXT:
            return False
    raise AskError(f"{label} is true/false; '{_clip(value, 40)}' is not.")


def _to_datetime_text(value: Any, label: str) -> str:
    if not isinstance(value, (str, datetime, date, pd.Timestamp)):
        raise AskError(f"{label} needs a date/time such as \"2026-10-20 14:00\".")
    try:
        ts = pd.Timestamp(value)
    except (ValueError, TypeError, OverflowError):
        raise AskError(f"'{_clip(value, 40)}' is not a date/time ({label}).") from None
    if pd.isna(ts):
        raise AskError(f"{label} needs a date/time.")
    return ts.isoformat(sep=" ")


def _to_text(value: Any, label: str) -> str:
    if isinstance(value, (bool, np.bool_)):
        text = "True" if value else "False"
    elif isinstance(value, (float, np.floating)) and math.isfinite(float(value)) and float(value).is_integer():
        text = str(int(value))
    elif isinstance(value, (str, int, float, np.integer, np.floating)):
        text = str(value)
    else:
        raise AskError(f"Values for {label} must be text, numbers or true/false.")
    text = text.strip()
    if len(text) > _MAX_VALUE_CHARS:
        raise AskError(f"A value for {label} is too long (at most {_MAX_VALUE_CHARS} characters).")
    return text


_COERCE = {"number": _to_number, "bool": _to_bool, "datetime": _to_datetime_text, "text": _to_text}
_OPS_BY_KIND = {
    "number": ALLOWED_OPS - {"contains"},
    "datetime": ALLOWED_OPS - {"contains"},
    "bool": frozenset({"==", "!=", "in", "not_in", "is_null", "not_null"}),
    "text": ALLOWED_OPS,
}


def _validate_filter(item: Any, cols: _Columns) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise AskError("Each filter must be an object with column, op and value.")
    col = cols.resolve(item.get("column"), "filters")
    raw_op = item.get("op", "==")
    op = str(raw_op).strip().lower() if isinstance(raw_op, str) else ""
    op = _OP_ALIASES.get(op, op)
    value = item.get("value")
    if op in ("==", "!=") and value is None:
        op = "is_null" if op == "==" else "not_null"
    if op in ("==", "!=") and isinstance(value, (list, tuple)):
        op = "in" if op == "==" else "not_in"
    if op not in ALLOWED_OPS:
        raise AskError(f"Operator '{_clip(raw_op, 20)}' is not supported. Use one of: "
                       f"{', '.join(sorted(ALLOWED_OPS))}.")
    kind, label = cols.kind(col), column_label(col)
    if op not in _OPS_BY_KIND[kind]:
        raise AskError(f"'{op}' cannot be used on {label} ({kind} column).")
    if op in ("is_null", "not_null"):
        return {"column": col, "op": op, "value": None}
    coerce = _COERCE[kind]
    if op in ("in", "not_in") or (op == "contains" and isinstance(value, (list, tuple))):
        items = list(value) if isinstance(value, (list, tuple)) else [value]
        if not items:
            raise AskError(f"'{op}' on {label} needs at least one value.")
        if len(items) > _MAX_LIST_VALUES:
            raise AskError(f"Too many values for '{op}' on {label} (at most {_MAX_LIST_VALUES}).")
        coerced = list(dict.fromkeys(coerce(v, label) for v in items))
        if op == "contains" and any(v == "" for v in coerced):
            raise AskError(f"'contains' on {label} needs some text.")
        return {"column": col, "op": op, "value": coerced}
    if isinstance(value, (list, tuple)):
        raise AskError(f"'{op}' on {label} takes a single value.")
    coerced_value = coerce(value, label)
    if op == "contains" and coerced_value == "":
        raise AskError(f"'contains' on {label} needs some text.")
    return {"column": col, "op": op, "value": coerced_value}


def _validate_columns(raw: Any, cols: _Columns, where: str, max_items: int) -> list[str]:
    out: list[str] = []
    for ref in _as_list(raw, where, max_items):
        if isinstance(ref, dict):
            ref = ref.get("column")
        if where == "columns" and isinstance(ref, str) and ref.strip() == "*":
            return list(cols.visible)
        col = cols.resolve(ref, where)
        if col not in out:
            out.append(col)
    return out


def _alias(raw: Any, agg: str, col: str | None, used: set[str]) -> str:
    base = re.sub(r"[^\w %().,/+-]", "", raw).strip()[:60] if isinstance(raw, str) else ""
    if not base:
        base = "count" if col is None else f"{agg}_{col}"
    alias, n = base, 2
    while alias in used:
        alias, n = f"{base}_{n}", n + 1
    return alias


def _validate_metrics(raw: Any, cols: _Columns, group_by: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    used = set(group_by)
    for item in _as_list(raw, "metrics", _MAX_METRICS):
        if isinstance(item, str):
            item = {"agg": item}
        if not isinstance(item, dict):
            raise AskError("Each metric must be an object with column, agg and as.")
        raw_agg = item.get("agg") or item.get("aggregation") or item.get("function") or "count"
        agg = str(raw_agg).strip().lower() if isinstance(raw_agg, str) else ""
        agg = _AGG_ALIASES.get(agg, agg)
        if agg not in ALLOWED_AGGS:
            raise AskError(f"Aggregation '{_clip(raw_agg, 20)}' is not supported. Use one of: "
                           f"{', '.join(sorted(ALLOWED_AGGS))}.")
        ref = item.get("column")
        if ref is None or (isinstance(ref, str) and ref.strip() in ("", "*")):
            if agg != "count":
                raise AskError(f"'{agg}' needs a column.")
            col = None
        else:
            col = cols.resolve(ref, "metrics")
            kind = cols.kind(col)
            if agg in ("sum", "mean", "median") and kind not in ("number", "bool"):
                raise AskError(f"Cannot compute {agg} of {column_label(col)} ({kind} column).")
        alias = _alias(item.get("as") or item.get("alias") or item.get("name"), agg, col, used)
        used.add(alias)
        out.append({"column": col, "agg": agg, "as": alias})
    return out


def _sort_desc(item: dict[str, Any]) -> bool:
    if "desc" in item:
        value = item.get("desc")
        return _truthy(value) or (isinstance(value, str) and value.strip().lower() in ("desc", "descending"))
    if "ascending" in item:
        return not _truthy(item.get("ascending"))
    direction = str(item.get("direction") or item.get("order") or "").strip().lower()
    return direction in ("desc", "descending")


def _resolve_output(ref: Any, cols: _Columns, group_by: list[str], metrics: list[dict[str, Any]]) -> str:
    aliases = [m["as"] for m in metrics]
    if isinstance(ref, str):
        name = ref.strip()
        if name in aliases or name in group_by:
            return name
        by_norm = {_norm_ref(a): a for a in aliases}
        if _norm_ref(name) in by_norm:
            return by_norm[_norm_ref(name)]
        if _norm_ref(name) in ("count", "n", "total", "number", "size", "candidates", "rows"):
            counts = [m["as"] for m in metrics if m["agg"] == "count"]
            if counts:
                return counts[0]
    try:
        col = cols.resolve(ref, "sort")
    except AskError:
        col = None
    if col in group_by:
        return str(col)
    using = [m["as"] for m in metrics if m["column"] == col] if col else []
    if len(using) == 1:
        return using[0]
    shown = ", ".join([*group_by, *aliases])
    raise AskError(f"Cannot sort by '{_clip(ref, 40)}': a grouped answer can only be sorted by its own columns "
                   f"({shown}).")


def _validate_sort(raw: Any, cols: _Columns, group_by: list[str], metrics: list[dict[str, Any]]
                   ) -> list[dict[str, Any]]:
    aggregate = bool(group_by or metrics)
    out: list[dict[str, Any]] = []
    for item in _as_list(raw, "sort", _MAX_SORT):
        if isinstance(item, str):
            item = {"column": item}
        if not isinstance(item, dict):
            raise AskError("Each sort entry must be an object with column and desc.")
        ref = item.get("column", item.get("by", item.get("field")))
        col = _resolve_output(ref, cols, group_by, metrics) if aggregate else cols.resolve(ref, "sort")
        if col not in [s["column"] for s in out]:
            out.append({"column": col, "desc": _sort_desc(item)})
    if out:
        return out
    if group_by and metrics:
        return [{"column": metrics[0]["as"], "desc": True}]
    if not aggregate:
        if S.RANK in cols.visible:
            return [{"column": S.RANK, "desc": False}]
        if S.FINAL_SCORE in cols.visible:
            return [{"column": S.FINAL_SCORE, "desc": True}]
    return []


def _validate_limit(raw: Any) -> int:
    if isinstance(raw, (bool, np.bool_)) or raw is None:
        return DEFAULT_LIMIT
    try:
        n = int(float(raw))
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_LIMIT
    return DEFAULT_LIMIT if n <= 0 else min(n, MAX_LIMIT)


def _unsupported_message(plan: dict[str, Any]) -> str:
    why = _clip(plan.get("explanation") or "", 300)
    return "This question can't be answered from the screening results." + (f" {why}" if why else "")


def validate_plan(plan: Any, df: pd.DataFrame) -> dict[str, Any]:
    """Check a plan against the frame's columns and the whitelists; return a normalised, explicit plan.

    Unknown columns, hashes, operators and aggregations are rejected; values are coerced to the
    column kinds; defaults are filled in (list columns, sort, limit 50, max 1000). Raises AskError.
    """
    if not isinstance(df, pd.DataFrame) or df.empty:
        raise AskError("There are no screening results to query yet. Run a screening first.")
    if isinstance(plan, str):
        plan = _as_plan_dict(plan)
    if not isinstance(plan, dict):
        raise AskError("The query plan must be a JSON object.")
    if _truthy(plan.get("unsupported")):
        raise AskError(_unsupported_message(plan))
    cols = _Columns(df)
    filters = [_validate_filter(f, cols) for f in _as_list(plan.get("filters"), "filters", _MAX_FILTERS)]
    group_by = _validate_columns(plan.get("group_by"), cols, "group_by", _MAX_GROUP_BY)
    metrics = _validate_metrics(plan.get("metrics"), cols, group_by)
    if group_by and not metrics:
        metrics = [{"column": None, "agg": "count", "as": _alias("count", "count", None, set(group_by))}]
    columns: list[str] = []
    if not group_by and not metrics:
        columns = _validate_columns(plan.get("columns"), cols, "columns", _MAX_COLUMNS)
        if not columns:
            columns = [c for c in DEFAULT_LIST_COLUMNS if c in cols.visible] or cols.visible[:8]
    sort = _validate_sort(plan.get("sort"), cols, group_by, metrics)
    if columns and plan.get("sort"):
        columns += [s["column"] for s in sort if s["column"] not in columns]   # show what was sorted on
    requested_chart = str(plan.get("chart") or "").strip().lower()
    chart = "bar" if group_by and len(metrics) == 1 and requested_chart != "none" else "none"
    return {
        "filters": filters,
        "group_by": group_by,
        "metrics": metrics,
        "columns": columns,
        "sort": sort,
        "limit": _validate_limit(plan.get("limit")),
        "chart": chart,
        "explanation": _clip(plan.get("explanation") or "", 500) if isinstance(plan.get("explanation"), str) else "",
    }


# ---------------------------------------------------------------------------
# Execution (boolean masks, groupby aggregations, sort_values, head - nothing else)
# ---------------------------------------------------------------------------
def _tol(v: float) -> float:
    return 1e-9 * max(1.0, abs(v))


def _close(x: pd.Series, v: float) -> pd.Series:
    return (x - v).abs() <= _tol(v)


def _missing(series: pd.Series, kind: str) -> pd.Series:
    if kind == "number":
        return _num(series).isna()
    if kind == "bool":
        return _bool_series(series).isna()
    if kind == "datetime":
        return _dt(series).isna()
    return _text(series) == ""


def _number_mask(x: pd.Series, op: str, value: Any) -> pd.Series:
    if op in ("in", "not_in"):
        hit = pd.Series(False, index=x.index)
        for v in value:
            hit = hit | _close(x, float(v))
        return hit if op == "in" else ~hit
    v = float(value)
    if op == "==":
        return _close(x, v)
    if op == "!=":
        return ~_close(x, v)
    if op == ">":
        return x > v + _tol(v)
    if op == ">=":
        return x >= v - _tol(v)
    if op == "<":
        return x < v - _tol(v)
    return x <= v + _tol(v)   # "<="


def _bool_mask(b: pd.Series, op: str, value: Any) -> pd.Series:
    if op in ("in", "not_in"):
        hit = _as_mask(b.isin([bool(v) for v in value]))
        return hit if op == "in" else ~hit
    equal = _as_mask(b == bool(value))
    return equal if op == "==" else ~equal


def _timestamp_for(d: pd.Series, value: str) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    tz = d.dt.tz
    if tz is not None and ts.tzinfo is None:
        return ts.tz_localize(tz)
    if tz is None and ts.tzinfo is not None:
        return ts.tz_convert(None)
    return ts


def _datetime_mask(d: pd.Series, op: str, value: Any) -> pd.Series:
    if op in ("in", "not_in"):
        hit = _as_mask(d.isin([_timestamp_for(d, v) for v in value]))
        return hit if op == "in" else ~hit
    ts = _timestamp_for(d, value)
    if op == "==":
        return _as_mask(d == ts)
    if op == "!=":
        return ~_as_mask(d == ts)
    if op == ">":
        return _as_mask(d > ts)
    if op == ">=":
        return _as_mask(d >= ts)
    if op == "<":
        return _as_mask(d < ts)
    return _as_mask(d <= ts)   # "<="


def _skill_items(cell: str) -> set[str]:
    return {part.strip() for part in cell.split(",") if part.strip()}


def _text_mask(t: pd.Series, op: str, value: Any, skill_list: bool) -> pd.Series:
    """t: casefolded, stripped text with missing as ''."""
    if op in ("in", "not_in"):
        hit = _as_mask(t.isin([str(v).casefold() for v in value]))
        return hit if op == "in" else ~hit
    if op == "contains":
        hit = pd.Series(False, index=t.index)
        for needle in [str(v).casefold() for v in (value if isinstance(value, list) else [value])]:
            if skill_list:
                found = t.map(lambda cell, n=needle: n in _skill_items(cell))
            else:
                found = t.str.contains(needle, regex=False)
            hit = hit | _as_mask(found)
        return hit
    v = str(value).casefold()
    if op == "==":
        return _as_mask(t == v)
    if op == "!=":
        return ~_as_mask(t == v)
    present = t != ""
    if op == ">":
        return _as_mask(present & (t > v))
    if op == ">=":
        return _as_mask(present & (t >= v))
    if op == "<":
        return _as_mask(present & (t < v))
    return _as_mask(present & (t <= v))   # "<="


def _filter_mask(df: pd.DataFrame, flt: dict[str, Any], cols: _Columns) -> pd.Series:
    col, op, value = flt["column"], flt["op"], flt["value"]
    series, kind = df[col], cols.kind(col)
    if op in ("is_null", "not_null"):
        missing = _as_mask(_missing(series, kind))
        return missing if op == "is_null" else ~missing
    if kind == "number":
        return _number_mask(_num(series), op, value)
    if kind == "bool":
        return _bool_mask(_bool_series(series), op, value)
    if kind == "datetime":
        return _datetime_mask(_dt(series), op, value)
    return _text_mask(_text(series).str.casefold(), op, value, col in _SKILL_LIST_COLUMNS)


def _metric_values(series: pd.Series, kind: str) -> pd.Series:
    if kind == "number":
        return _num(series)
    if kind == "bool":
        return _bool_series(series).astype("Float64")
    if kind == "datetime":
        return _dt(series)
    text = _text(series)
    return text.mask(text == "")


def _group_key(series: pd.Series, kind: str, name: str) -> pd.Series:
    if kind == "number":
        key = series if pd.api.types.is_numeric_dtype(series.dtype) else _num(series)
    elif kind == "bool":
        key = _bool_series(series)
    elif kind == "datetime":
        key = _dt(series)
    else:
        text = _text(series)
        key = text.mask(text == "", _NONE_LABEL).astype(str)
    return key.rename(name)


def _aggregate(values: Any, agg: str) -> Any:
    """values: Series or SeriesGroupBy. Explicit dispatch over the whitelisted aggregations."""
    if agg == "count":
        return values.count()
    if agg == "sum":
        return values.sum()
    if agg == "mean":
        return values.mean()
    if agg == "median":
        return values.median()
    if agg == "min":
        return values.min()
    if agg == "max":
        return values.max()
    if agg == "nunique":
        return values.nunique()
    raise AskError(f"Aggregation '{agg}' is not supported.")


def _scalar(value: Any) -> Any:
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, np.generic):
        return value.item()
    return value


def _tidy_metrics(table: pd.DataFrame, metrics: list[dict[str, Any]], cols: _Columns) -> pd.DataFrame:
    """Nullable floats -> float64; means, medians and float sums rounded to 2 dp; true/false
    sums, minimums and maximums as whole numbers."""
    updates: dict[str, pd.Series] = {}
    for m in metrics:
        col = table[m["as"]]
        if not pd.api.types.is_numeric_dtype(col.dtype) or pd.api.types.is_bool_dtype(col.dtype):
            continue
        if pd.api.types.is_float_dtype(col.dtype):
            if m["column"] is not None and cols.kind(m["column"]) == "bool" and m["agg"] in ("sum", "min", "max"):
                col = col.round().astype("Int64")
            else:
                col = col.astype("float64")
                if m["agg"] in ("mean", "median", "sum"):
                    col = col.round(2)
        updates[m["as"]] = col
    return table.assign(**updates) if updates else table


def _aggregate_frame(sub: pd.DataFrame, plan: dict[str, Any], cols: _Columns) -> pd.DataFrame:
    group_by, metrics = plan["group_by"], plan["metrics"]
    aliases = [m["as"] for m in metrics]
    if not group_by:
        row: dict[str, Any] = {}
        for m in metrics:
            if m["column"] is None:
                row[m["as"]] = len(sub)
            else:
                values = _metric_values(sub[m["column"]], cols.kind(m["column"]))
                row[m["as"]] = _scalar(_aggregate(values, m["agg"]))
        return _tidy_metrics(pd.DataFrame([row], columns=aliases), metrics, cols)
    if sub.empty:
        return pd.DataFrame(columns=[*group_by, *aliases])
    keys = [_group_key(sub[c], cols.kind(c), c) for c in group_by]
    # One grouper for every metric, so all results share the same group index.
    work = pd.DataFrame({f"_m{i}": (_metric_values(sub[m["column"]], cols.kind(m["column"]))
                                    if m["column"] is not None else pd.Series(1, index=sub.index))
                         for i, m in enumerate(metrics)}, index=sub.index)
    grouped = work.groupby(keys, dropna=False, sort=False)
    parts = []
    for i, m in enumerate(metrics):
        res = grouped.size() if m["column"] is None else _aggregate(grouped[f"_m{i}"], m["agg"])
        parts.append(res.rename(m["as"]))
    table = pd.concat(parts, axis=1).reset_index()
    return _tidy_metrics(table, metrics, cols)


def _sort_key(kinds: dict[str, str]):
    def key(col: pd.Series) -> pd.Series:
        kind = kinds.get(str(col.name), "text")
        if kind == "number":
            return _num(col)
        if kind == "bool":
            return _bool_series(col)
        if kind == "datetime":
            return _dt(col)
        text = _text(col).str.casefold()
        return text.mask((text == "") | (text == _NONE_LABEL))   # blanks sort last
    return key


def _sorted(frame: pd.DataFrame, sort: list[dict[str, Any]], kinds: dict[str, str],
            tie_breakers: list[str]) -> pd.DataFrame:
    by = [s["column"] for s in sort if s["column"] in frame.columns]
    ascending = [not s["desc"] for s in sort if s["column"] in frame.columns]
    for col in tie_breakers:
        if col in frame.columns and col not in by:
            by.append(col)
            ascending.append(True)
    if not by or frame.empty:
        return frame
    return frame.sort_values(by=by, ascending=ascending, key=_sort_key(kinds), na_position="last", kind="stable")


def _execute(plan: dict[str, Any], df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Run a validated plan -> (answer table, rows/groups before the limit)."""
    df = _unique_columns(df)
    cols = _Columns(df)
    mask = pd.Series(True, index=df.index)
    for flt in plan["filters"]:
        mask = mask & _filter_mask(df, flt, cols)
    sub = df.loc[mask]
    if plan["group_by"] or plan["metrics"]:
        table = _aggregate_frame(sub, plan, cols)
        total = len(table)
        if plan["group_by"]:
            kinds = {c: (cols.kind(c) if c in plan["group_by"] else column_kind(table[c])) for c in table.columns}
            table = _sorted(table, plan["sort"], kinds, list(plan["group_by"])).head(plan["limit"])
    else:
        total = len(sub)
        used = [s["column"] for s in plan["sort"]] + ([S.CANDIDATE_ID] if S.CANDIDATE_ID in cols.visible else [])
        kinds = {c: cols.kind(c) for c in used if c in sub.columns}
        ordered = _sorted(sub, plan["sort"], kinds, [S.CANDIDATE_ID])
        table = ordered.loc[:, plan["columns"]].head(plan["limit"])
    table = table.reset_index(drop=True)
    table.attrs["total_rows"] = int(total)
    return table, int(total)


def execute_plan(plan: dict[str, Any], df: pd.DataFrame) -> pd.DataFrame:
    """Validate (idempotent) and run a plan; ``table.attrs["total_rows"]`` holds the count before the limit."""
    return _execute(validate_plan(plan, df), df)[0]


# ---------------------------------------------------------------------------
# Answers
# ---------------------------------------------------------------------------
@dataclass
class AskResult:
    """One answered (or refused) question. ``plan`` is the validated plan when the answer
    succeeded, otherwise the plan as received (for display), or {} when there was none."""

    question: str
    plan: dict[str, Any] = field(default_factory=dict)
    table: pd.DataFrame | None = None
    summary: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and self.table is not None


def _fmt_value(value: Any) -> str:
    value = _scalar(value)
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        if math.isnan(value):
            return "-"
        return f"{value:,.0f}" if value.is_integer() else f"{value:,.2f}"
    if isinstance(value, (pd.Timestamp, datetime)):
        return _iso(value)
    return _clip(value, 80)


def summarize(plan: dict[str, Any], table: pd.DataFrame | None, total_rows: int | None = None) -> str:
    """Deterministic one-line summary, e.g. '12 groups · grouped by College · 2 filters'."""
    n = 0 if table is None else len(table)
    total = n if total_rows is None else max(int(total_rows), n)
    group_by, metrics = plan.get("group_by") or [], plan.get("metrics") or []
    parts: list[str] = []
    if metrics and not group_by and table is not None and n:
        row = table.iloc[0]
        parts.append(", ".join(f"{m['as']}: {_fmt_value(row.get(m['as']))}" for m in metrics))
    elif total == 0:
        parts.append("No matching rows")
    elif group_by:
        word = "group" if total == 1 else "groups"
        parts.append(f"{total:,} {word}" if n >= total else f"Top {n:,} of {total:,} groups")
        parts.append("grouped by " + ", ".join(column_label(c) for c in group_by))
    else:
        word = "row" if total == 1 else "rows"
        parts.append(f"{total:,} matching {word}" + ("" if n >= total else f", showing the first {n:,}"))
    if plan.get("filters"):
        k = len(plan["filters"])
        parts.append(f"{k} filter{'s' if k != 1 else ''}")
    sort = plan.get("sort") or []
    if sort and total > 1 and (group_by or not metrics):
        first = sort[0]
        name = first["column"] if first["column"] in [m["as"] for m in metrics] else column_label(first["column"])
        parts.append(f"sorted by {name} ({'highest' if first['desc'] else 'lowest'} first)")
    return " · ".join(parts)


def _display_plan(raw: Any) -> dict[str, Any]:
    """JSON-safe, size-capped copy of a raw plan for the 'how this was answered' panel."""
    try:
        text = json.dumps(raw, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return {}
    if len(text) > 6000:
        return {"note": "The plan was too large to display."}
    data = json.loads(text)
    return data if isinstance(data, dict) else {}


def answer(question: str, df: pd.DataFrame, client: Any, cfg: AppConfig | None = None) -> AskResult:
    """Plan (LLM) -> validate -> execute locally. Never raises: problems land in ``error``."""
    q = _clean_question(question)
    if not q:
        return AskResult(question="", error="Type a question about the screening results first.")
    try:
        raw = plan_query(q, df, client, cfg)
    except AskError as exc:
        return AskResult(question=q, error=str(exc))
    except Exception as exc:  # noqa: BLE001 - defensive: the UI must never see a traceback
        log.warning("Ask the Data: planning failed (%s)", type(exc).__name__)
        return AskResult(question=q, error="Something went wrong while planning this question. Please try again.")
    shown = _display_plan(raw)
    if _truthy(raw.get("unsupported")):
        return AskResult(question=q, plan={**shown, "unsupported": True}, error=_unsupported_message(raw))
    try:
        plan = validate_plan(raw, df)
        table, total = _execute(plan, df)
    except AskError as exc:
        return AskResult(question=q, plan=shown, error=f"The query plan could not be used: {exc}")
    except Exception as exc:  # noqa: BLE001
        log.warning("Ask the Data: execution failed (%s)", type(exc).__name__)
        return AskResult(question=q, plan=shown,
                         error="The query could not be run on this data. Try rephrasing the question.")
    return AskResult(question=q, plan=plan, table=table, summary=summarize(plan, table, total))
