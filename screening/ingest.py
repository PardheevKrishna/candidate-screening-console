"""TA / HackerRank feed ingest: read CSV/Excel, auto-map headers, normalise rows.

Public API (docs/ARCHITECTURE.md):

* ``read_table(file_name, content)``           -> raw DataFrame, every column str (missing = NaN)
* ``suggest_column_mapping(columns)``          -> {source header: canonical name}
* ``normalize_ta_feed(raw, mapping, cfg)``     -> (ta_df, DataQualityReport)
* ``load_ta_feed(file_name, content, cfg, mapping_override=None)`` -> (ta_df, DataQualityReport)

Row-level problems never raise: they become ``data_flags`` entries plus report issues.
Only a file that cannot be read as a table at all (empty, corrupt, legacy binary .xls)
raises ``ValueError``; its message is safe to show to users (no cell values / PII).

Data flags written to ``S.DATA_FLAGS`` ("; "-joined): generated_id, invalid_email,
invalid_phone, score_missing, score_from_pct, score_exceeds_max, score_negative,
max_score_assumed, cgpa_out_of_range, invalid_graduation_year, invalid_test_start,
invalid_submission_time, submission_before_start, invalid_completion_time, invalid_ip,
duplicate_id, shared_email.
"""
from __future__ import annotations

import csv
import datetime as _dt
import io
import ipaddress
import math
import re
import warnings
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
from rapidfuzz import fuzz

from . import schema as S
from .config import AppConfig
from .models import DataQualityReport

STAGE = "ingest"
EXTRA_PREFIX = "extra_"
# Derived columns not (yet) in schema.py
PLAGIARISM_MATCH_ID = "plagiarism_match_id"   # candidate_id resolved from plagiarism_match
IP_ADDRESSES = "ip_addresses"                 # ", "-joined list when a cell lists several IPs

FUZZY_MIN_RATIO = 90.0        # rapidfuzz ratio needed for a fuzzy header match
HEADER_MIN_CELLS = 3          # a header row has at least this many non-empty cells
HEADER_SCAN_ROWS = 50         # rows inspected when looking for the header
DEFAULT_MAX_SCORE = 100.0
PCT_TOLERANCE_PTS = 1.0       # percentage column vs score/max disagreement worth noting
MAX_TABLE_ROWS = 500_000

FLAG_GENERATED_ID = "generated_id"
FLAG_INVALID_EMAIL = "invalid_email"
FLAG_INVALID_PHONE = "invalid_phone"
FLAG_SCORE_MISSING = "score_missing"
FLAG_SCORE_FROM_PCT = "score_from_pct"
FLAG_SCORE_EXCEEDS_MAX = "score_exceeds_max"
FLAG_SCORE_NEGATIVE = "score_negative"
FLAG_MAX_ASSUMED = "max_score_assumed"
FLAG_CGPA_RANGE = "cgpa_out_of_range"
FLAG_GRAD_YEAR = "invalid_graduation_year"
FLAG_BAD_START = "invalid_test_start"
FLAG_BAD_SUBMIT = "invalid_submission_time"
FLAG_SUBMIT_BEFORE_START = "submission_before_start"
FLAG_BAD_DURATION = "invalid_completion_time"
FLAG_INVALID_IP = "invalid_ip"
FLAG_DUPLICATE_ID = "duplicate_id"
FLAG_SHARED_EMAIL = "shared_email"

# Canonical column order of the normalised frame (only present columns are emitted).
_OUTPUT_ORDER = [
    S.CANDIDATE_ID, S.NAME, S.EMAIL, S.PHONE, S.COLLEGE, S.COLLEGE_TIER, S.DEGREE, S.BRANCH,
    S.GRAD_YEAR, S.CGPA, S.CITY, S.HR_SCORE, S.MAX_SCORE, S.HR_PCT, S.TEST_NAME, S.TEST_STATUS,
    S.TEST_START, S.SUBMISSION_TIME, S.COMPLETION_MIN, S.IP_ADDRESS, IP_ADDRESSES, S.IP_SUBNET,
    S.TEST_MODE, S.TAB_SWITCHES, S.COPY_PASTE, S.OUT_OF_WINDOW_SEC, S.SECURE_MODE_EXITS,
    S.PLAGIARISM_FLAG, S.MAX_CODE_SIMILARITY, S.PLAGIARISM_MATCH, PLAGIARISM_MATCH_ID,
    S.INTEGRITY_STATUS, S.PROCTORING_FLAGS, S.SOURCE, S.EMAIL_DOMAIN, S.TEST_SLOT,
    S.RESUME_FILE, S.REPORT_URL,
]
# Free-text categories whose spellings are unified (case / whitespace / punctuation only).
_CANONICAL_SPELLING_COLUMNS = [S.COLLEGE, S.CITY, S.DEGREE, S.BRANCH, S.SOURCE, S.TEST_NAME, S.TEST_MODE]

_NULL_TOKENS = frozenset({
    "", "na", "n/a", "n.a.", "nan", "null", "none", "nil", "-", "--", "---", "#n/a",
    "not available", "not applicable",
})


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
_EXCEL_EXTENSIONS = {".xlsx", ".xlsm", ".xltx", ".xltm", ".xls"}
_ZIP_MAGIC = b"PK\x03\x04"
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")
_DELIMITERS = (",", ";", "\t", "|")


def read_table(file_name: str, content: bytes) -> pd.DataFrame:
    """Read a CSV/TSV/TXT or Excel export into a DataFrame of strings (missing = NaN).

    The header row is the first row with >= 3 non-empty cells (and at least half as many
    as the widest row), so title rows above the header are skipped. Raises ValueError when
    the file cannot be read as a table.
    """
    if not content:
        raise ValueError("The TA file is empty.")
    ext = Path(str(file_name)).suffix.lower()
    is_workbook = content.startswith((_ZIP_MAGIC, _OLE2_MAGIC))
    if is_workbook or (ext in _EXCEL_EXTENSIONS and b"\x00" in content[:4096]
                       and not content.startswith(_UTF16_BOMS)):
        rows = _excel_rows(content)
    else:  # includes web "Excel" exports that are really delimited text
        rows = _text_rows(content)
    return _rows_to_frame(rows)


def _decode(content: bytes) -> str:
    if content.startswith(_UTF16_BOMS):
        return content.decode("utf-16")
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("latin-1")  # never fails


def _sniff_delimiter(text: str) -> str:
    """Delimiter giving the most consistent multi-column rows in a sample (title rows tolerated)."""
    sample = text[:65536]
    best, best_key = ",", (-1.0, 0)
    for delim in _DELIMITERS:
        try:
            widths = [len(r) for r in csv.reader(io.StringIO(sample), delimiter=delim) if any(c.strip() for c in r)]
        except csv.Error:
            continue
        if not widths:
            continue
        mode, freq = Counter(widths).most_common(1)[0]
        key = (freq / len(widths) if mode >= 2 else 0.0, mode)
        if key > best_key:
            best, best_key = delim, key
    return best


def _text_rows(content: bytes) -> list[list[str]]:
    text = _decode(content)
    if not text.strip():
        raise ValueError("The TA file has no data.")
    delim = _sniff_delimiter(text)
    csv.field_size_limit(max(csv.field_size_limit(), 2**24))
    try:
        reader = csv.reader(io.StringIO(text), delimiter=delim)
        return [row for _, row in zip(range(MAX_TABLE_ROWS), reader)]
    except csv.Error:
        raise ValueError("Could not parse the TA file as delimited text.") from None


def _excel_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        if not math.isfinite(value):
            return ""
        return str(int(value)) if value.is_integer() and abs(value) < 1e15 else format(value, ".15g")
    if isinstance(value, _dt.datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (_dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, _dt.timedelta):
        secs = int(round(value.total_seconds()))
        return f"{secs // 3600}:{secs % 3600 // 60:02d}:{secs % 60:02d}"
    return str(value)


def _excel_rows(content: bytes) -> list[list[str]]:
    """Rows of the first sheet that has a header-like row (else the first non-empty sheet)."""
    if content.startswith(_OLE2_MAGIC):
        raise ValueError("Legacy binary .xls files are not supported - re-save the file as .xlsx or CSV.")
    from openpyxl import load_workbook  # local import keeps CSV-only use light

    sheets: list[list[list[str]]] = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        except Exception:  # noqa: BLE001 - any openpyxl failure means "not a readable workbook"
            raise ValueError("Could not open the Excel workbook (corrupt or unsupported format).") from None
        try:
            for ws in wb.worksheets:
                rows = [[_excel_cell(v) for v in row]
                        for _, row in zip(range(MAX_TABLE_ROWS), ws.iter_rows(values_only=True))]
                widths = [sum(1 for c in r if c.strip()) for r in rows]
                if widths and max(widths) >= HEADER_MIN_CELLS:
                    return rows
                if any(widths):
                    sheets.append(rows)
        finally:
            wb.close()
    if not sheets:
        raise ValueError("The Excel workbook has no data.")
    return sheets[0]


def _label_share(row: list[str]) -> float:
    """Share of non-empty cells that look like column labels (letters; not emails/URLs)."""
    cells = [c for c in row if c]
    labels = [c for c in cells if re.search(r"[A-Za-z]", c) and "@" not in c and "://" not in c and len(c) <= 80]
    return len(labels) / len(cells) if cells else 0.0


def _find_header_row(rows: list[list[str]]) -> int:
    """First row with >= 3 cells and >= half the widest row; among the first few such rows
    prefer one that reads like labels (skips "Generated | 2026-11-04 | v2" title rows)."""
    counts = [sum(1 for c in r if c) for r in rows[:HEADER_SCAN_ROWS]]
    widest = max(counts, default=0)
    need = max(min(HEADER_MIN_CELLS, widest), math.ceil(widest / 2), 1)
    candidates = [i for i, cnt in enumerate(counts) if cnt >= need]
    if not candidates:
        return 0
    return next((i for i in candidates[:5] if _label_share(rows[i]) >= 0.75), candidates[0])


def _dedupe_headers(headers: list[str]) -> list[str]:
    taken: set[str] = set()
    out = []
    for h in headers:
        name, k = h, 1
        while name in taken:
            name, k = f"{h}.{k}", k + 1
        taken.add(name)
        out.append(name)
    return out


def _rows_to_frame(rows: list[list[str]]) -> pd.DataFrame:
    rows = [[" ".join(str(c).split()) for c in r] for r in rows]
    rows = [r for r in rows if any(r)]
    if not rows:
        raise ValueError("The TA file has no data.")
    h = _find_header_row(rows)
    header, data = rows[h], rows[h + 1:]
    width = max([len(header)] + [len(r) for r in data])
    header = header + [""] * (width - len(header))
    data = [r + [""] * (width - len(r)) for r in data]
    header_key = [c.lower() for c in header]
    data = [r for r in data if [c.lower() for c in r] != header_key]  # repeated header lines
    keep = [j for j in range(width) if header[j] or any(r[j] for r in data)]
    names = _dedupe_headers([header[j] or f"Column {j + 1}" for j in keep])
    frame = pd.DataFrame([[r[j] for j in keep] for r in data], columns=names, dtype="str")
    return frame.replace("", np.nan)


# ---------------------------------------------------------------------------
# Header auto-mapping
# ---------------------------------------------------------------------------
def _build_alias_pairs() -> list[tuple[str, str]]:
    """(normalised alias, canonical); canonical names themselves first so they always win."""
    canonicals = list(S.TA_FIELD_ALIASES)
    candidates = [(S.normalize_header(c), c) for c in canonicals]
    candidates += [(S.normalize_header(a), c) for c in canonicals for a in S.TA_FIELD_ALIASES[c]]
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for alias, canon in candidates:
        if alias and alias not in seen:
            seen.add(alias)
            pairs.append((alias, canon))
    return pairs


_ALIAS_PAIRS = _build_alias_pairs()
_ALIAS_INDEX = dict(_ALIAS_PAIRS)
_CANON_RANK = {c: i for i, c in enumerate(S.TA_FIELD_ALIASES)}
_QUESTION_PREFIX_RE = re.compile(r"^(?:q|ques|question)\d+")
_QUESTION_RULES = (
    (S.ANSWER_HASH_HEADER_RE, S.ANSWER_HASH_TEMPLATE),
    (S.QUESTION_SCORE_HEADER_RE, S.QUESTION_SCORE_TEMPLATE),
    (S.QUESTION_SIMILARITY_HEADER_RE, S.QUESTION_SIMILARITY_TEMPLATE),
)
_QUESTION_CANONICAL_RES = (S.ANSWER_HASH_RE, S.QUESTION_SCORE_RE, S.QUESTION_SIMILARITY_RE)


def _question_canonical(norm_header: str) -> str | None:
    for regex, template in _QUESTION_RULES:
        m = regex.match(norm_header)
        if m:
            return template.format(n=int(m.group(1)))
    return None


def is_canonical_field(name: str) -> bool:
    """True if ``name`` is a TA field the mapper may target (incl. q{n}_hash/score/similarity)."""
    return name in S.TA_FIELD_ALIASES or any(rx.match(name) for rx in _QUESTION_CANONICAL_RES)


def suggest_column_mapping(columns: list[str]) -> dict[str, str]:
    """Map source headers to canonical names, one-to-one.

    Tiers: exact alias (incl. the canonical name itself) -> per-question regexes
    (q{n}_hash / q{n}_score / q{n}_similarity) -> rapidfuzz ratio >= 90 for leftovers.
    An earlier tier always beats a later one (so "Candidate ID" keeps candidate_id even if
    "Test Candidate ID" were a close fuzzy match); within a tier the earlier column wins.
    Unmapped headers are absent from the result.
    """
    headers = [str(c) for c in columns]
    norm = [S.normalize_header(h) for h in headers]
    first_pos: dict[str, int] = {}
    for i, h in enumerate(headers):
        first_pos.setdefault(h, i)
    eligible = [i for i, h in enumerate(headers) if first_pos[h] == i and norm[i]]
    assigned: dict[int, str] = {}
    used: set[str] = set()

    def take(i: int, canon: str | None) -> None:
        if canon and i not in assigned and canon not in used:
            assigned[i] = canon
            used.add(canon)

    for i in eligible:
        take(i, _ALIAS_INDEX.get(norm[i]))
    for i in eligible:
        take(i, _question_canonical(norm[i]))

    scored: list[tuple[float, int, int, str]] = []
    for i in eligible:
        # per-question headers never fall through to whole-test fields (q2 similarity != max similarity)
        if i in assigned or len(norm[i]) < 3 or _QUESTION_PREFIX_RE.match(norm[i]):
            continue
        for alias, canon in _ALIAS_PAIRS:
            if canon not in used:
                score = fuzz.ratio(norm[i], alias, score_cutoff=FUZZY_MIN_RATIO)
                if score:
                    scored.append((-score, i, _CANON_RANK[canon], canon))
    for _, i, _, canon in sorted(scored):
        take(i, canon)
    return {headers[i]: assigned[i] for i in sorted(assigned)}


# ---------------------------------------------------------------------------
# Value parsing helpers (all tolerant: bad input -> NaN / None)
# ---------------------------------------------------------------------------
def _cell_text(value: Any) -> str | None:
    """Raw cell -> stripped, whitespace-collapsed str, or None for blanks / null tokens."""
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, (float, np.floating)):
        v = float(value)
        if not math.isfinite(v):
            return None
        text = str(int(v)) if v.is_integer() and abs(v) < 1e15 else format(v, ".15g")
    elif isinstance(value, (bool, np.bool_)):
        text = "true" if value else "false"
    elif isinstance(value, (int, np.integer)):
        text = str(int(value))
    elif isinstance(value, _dt.datetime):  # includes pd.Timestamp
        text = value.isoformat(sep=" ")
    else:
        text = str(value)
    text = " ".join(text.split())
    return None if text.lower() in _NULL_TOKENS else text


def _text(series: pd.Series) -> pd.Series:
    return pd.Series([_cell_text(v) for v in series.tolist()], dtype="str")


_DECIMAL_COMMA_RE = re.compile(r"[-+]?\d+,\d{1,2}")
_NUMBER_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")


def _to_float(value: str) -> float:
    t = value.replace("−", "-").strip()
    t = t.replace(",", ".") if _DECIMAL_COMMA_RE.fullmatch(t) else t.replace(",", "")
    m = _NUMBER_RE.search(t)
    if not m:
        return math.nan
    v = float(m.group(0))
    return v if math.isfinite(v) else math.nan


def _floats(text: pd.Series) -> pd.Series:
    """Lenient numeric parse: "85", "85.5", "1,234", "85 %", "Score: 85" -> float; else NaN."""
    out = pd.to_numeric(text, errors="coerce").astype("float64")
    out = out.where(np.isfinite(out))
    todo = out.isna() & text.notna()
    if todo.any():
        out.loc[todo] = [_to_float(v) for v in text[todo]]
    return out


def _fraction_aware(text: pd.Series) -> pd.Series:
    """Percent-like values; a column whose values are all <= 1 (and has no "%") is a fraction -> x100."""
    values = _floats(text)
    finite = values.dropna()
    if len(finite) and 0 < finite.max() <= 1.0 and not text.str.contains("%", regex=False).any():
        values = values * 100.0
    return values


_YES = frozenset({"yes", "y", "true", "t", "detected"})
_NO = frozenset({"no", "n", "false", "f", "not detected"})


def _counts(text: pd.Series) -> pd.Series:
    """Event counts -> nullable Int64 (negative / absurd / unparseable -> NA; yes/no -> 1/0)."""
    lower = text.str.lower()
    values = _floats(text).mask(lower.isin(_YES), 1.0).mask(lower.isin(_NO), 0.0)
    return values.where((values >= 0) & (values < 1e9)).round().astype("Int64")


_DURATION_PART_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)(?![a-z])", re.I
)
_UNIT_SECONDS = {"h": 3600.0, "m": 60.0, "s": 1.0}


def _duration_seconds(value: str, default_unit_sec: float, two_part_hm: bool = False) -> float:
    """Duration text -> seconds; plain numbers are in ``default_unit_sec``.

    Accepts "72", "72.5", "01:12:30" (H:M:S), "72:30" (M:S, or H:M when two_part_hm),
    "1h 12m", "72 min", "95 s", "1m 35s".
    """
    t = value.strip().lower()
    try:
        v = float(t)
        return v * default_unit_sec if math.isfinite(v) else math.nan
    except ValueError:
        pass
    if ":" in t:
        try:
            nums = [float(p) for p in t.split(":")]
        except ValueError:
            nums = []
        if len(nums) == 3:
            return nums[0] * 3600 + nums[1] * 60 + nums[2]
        if len(nums) == 2:
            return nums[0] * 3600 + nums[1] * 60 if two_part_hm else nums[0] * 60 + nums[1]
    parts = _DURATION_PART_RE.findall(t)
    if parts:
        return sum(float(num) * _UNIT_SECONDS[unit[0].lower()] for num, unit in parts)
    m = _NUMBER_RE.search(t)  # e.g. "approx. 72"
    return float(m.group(0)) * default_unit_sec if m else math.nan


_TWO_PART_RE = re.compile(r"^\s*(\d+):(\d+(?:\.\d+)?)\s*$")


def _completion_minutes(text: pd.Series, report: DataQualityReport) -> pd.Series:
    """Completion time in minutes; an all-numeric column with median > 600 is read as seconds."""
    present = text.dropna()
    two = present.str.extract(_TWO_PART_RE).dropna().astype(float)
    # "1:12" for a ~70 min test means H:MM; "72:30" means MM:SS.
    two_part_hm = bool(len(two) and two[0].max() <= 23 and (two[0] + two[1] / 60).median() < 5)
    minutes = pd.Series([_duration_seconds(v, 60.0, two_part_hm) / 60.0 if isinstance(v, str) else math.nan
                         for v in text.tolist()], dtype="float64")
    numeric = pd.to_numeric(present, errors="coerce")
    if len(present) and numeric.notna().all() and float(numeric.median()) > 600:
        report.add("info", STAGE, "Completion times look like seconds (median > 600); converted to minutes.")
        minutes = minutes / 60.0
    return minutes


_TIME_TZ_RE = re.compile(r"(?i)^(.*\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?)\s*(?:z|utc|gmt|ist|[+-]\d{2}:?\d{2})$")
_YMD_RE = r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}"
_DMY_RE = r"^\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}(?!\d)"
_PLAIN_NUMBER_RE = r"^\d+(?:\.\d+)?$"


def _to_datetime(values: pd.Series, **kwargs: Any) -> pd.Series:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return pd.to_datetime(values, errors="coerce", **kwargs)


def _span_seconds(*parts: pd.Series) -> float:
    joined = pd.concat([p.dropna() for p in parts])
    return float((joined.max() - joined.min()).total_seconds()) if len(joined) else 0.0


def _parse_datetimes(text: pd.Series) -> pd.Series:
    """Mixed-format timestamps -> naive datetime64 (wall-clock as exported; tz suffix dropped).

    ISO / year-first strings parse directly. D/M/Y vs M/D/Y is decided per column by the
    reading with fewer failures, then the tighter overall date range (a test window is
    short), defaulting to day-first. Excel serial numbers and Unix epochs are accepted.
    """
    s = text.str.replace(_TIME_TZ_RE, r"\1", regex=True)
    out = pd.Series(pd.NaT, index=s.index, dtype="datetime64[us]")
    present = s.notna()
    numeric = present & s.str.match(_PLAIN_NUMBER_RE)
    ymd = present & ~numeric & s.str.match(_YMD_RE)
    dmy = present & ~numeric & s.str.match(_DMY_RE)
    other = present & ~numeric & ~ymd & ~dmy

    if numeric.any():
        num = s[numeric].astype(float)
        for mask, kw in ((num.between(20000, 80000), {"unit": "D", "origin": "1899-12-30"}),
                         (num.between(1e9, 2.2e9), {"unit": "s"}),
                         (num.between(1e12, 2.2e12), {"unit": "ms"})):
            if mask.any():
                out.loc[mask[mask].index] = _to_datetime(num[mask], **kw).dt.round("s").to_numpy()
    if ymd.any():
        parsed = _to_datetime(s[ymd], format="ISO8601")
        retry = parsed.isna()
        if retry.any():
            parsed.loc[retry] = _to_datetime(s[ymd][retry], format="mixed").to_numpy()
        out.loc[ymd] = parsed.to_numpy()
    if other.any():
        out.loc[other] = _to_datetime(s[other], format="mixed", dayfirst=False).to_numpy()
    if dmy.any():
        day_first = _to_datetime(s[dmy], format="mixed", dayfirst=True)
        month_first = _to_datetime(s[dmy], format="mixed", dayfirst=False)
        reference = out[present & ~dmy]
        key_day = (int(day_first.isna().sum()), _span_seconds(reference, day_first))
        key_month = (int(month_first.isna().sum()), _span_seconds(reference, month_first))
        out.loc[dmy] = (day_first if key_day <= key_month else month_first).to_numpy()
    return out


_EMAIL_RE = re.compile(r"[\w.+'%-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}")
_SCI_RE = re.compile(r"^\d(?:\.\d+)?[eE]\+?\d+$")


def clean_email(value: Any) -> str | None:
    """First email address in the cell, lowercased; None if there is none."""
    if not isinstance(value, str):
        return None
    m = _EMAIL_RE.search(value.replace("mailto:", " "))
    return m.group(0).lower().strip(".") if m else None


def clean_phone(value: Any) -> str | None:
    """Digits only, last 10 (Indian mobile); None when no usable number (incl. "9.87E+09")."""
    if not isinstance(value, str) or _SCI_RE.match(value.strip()):
        return None
    for part in re.split(r"[/,;|]", value):
        digits = re.sub(r"\D", "", part)
        if len(digits) >= 10:
            return digits[-10:]
    return None


def _smart_title(value: str) -> str:
    return value.title() if value.islower() or value.isupper() else value


def clean_name(value: Any) -> str | None:
    """Collapse whitespace; Title Case names typed all-lower / ALL-CAPS."""
    text = _cell_text(value)
    text = text.strip(" ,;.") if text else None
    return _smart_title(text) if text else None


_ROMAN_TIERS = {"i": "1", "ii": "2", "iii": "3", "iv": "4"}


def _college_tier(value: str) -> str | None:
    m = re.search(r"\d", value)
    if m:
        return m.group(0)
    return next((_ROMAN_TIERS[t] for t in re.findall(r"[a-z]+", value.lower()) if t in _ROMAN_TIERS), None)


_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?:\s*[-/–]\s*(\d{4}|\d{2}))?(?!\d)")


def _grad_year(value: str) -> float:
    """Latest year mentioned: "2026", "Batch 2026", "2022-2026", "2025-26" -> 2026."""
    years = []
    for start, end in _YEAR_RE.findall(value):
        years.append(int(start))
        if end:
            years.append(int(start[:2] + end) if len(end) == 2 else int(end))
    year = max(years, default=0)
    return float(year) if 1980 <= year <= 2100 else math.nan


_STATUS_MAP = {
    "7": "Completed", "completed": "Completed", "complete": "Completed", "submitted": "Completed",
    "done": "Completed", "finished": "Completed", "attempted": "Completed",
    "invited": "Invited", "pending": "Invited", "notstarted": "Invited", "yettostart": "Invited",
    "inprogress": "In Progress", "started": "In Progress", "ongoing": "In Progress",
    "expired": "Expired", "linkexpired": "Expired",
    "didnotattempt": "Did Not Attempt", "notattempted": "Did Not Attempt", "absent": "Did Not Attempt",
    "noshow": "Did Not Attempt", "dna": "Did Not Attempt",
}


def _test_status(value: str) -> str:
    return _STATUS_MAP.get(re.sub(r"[^a-z0-9]", "", value.lower())) or _smart_title(value)


_CAMPUS_WORDS = frozenset({"campus", "oncampus", "incampus", "onsite", "offline", "lab", "labs", "centre",
                           "center", "venue", "college", "physical", "inperson"})
_REMOTE_WORDS = frozenset({"remote", "remotely", "online", "home", "wfh", "virtual"})


def _test_mode(value: str) -> str:
    words = re.findall(r"[a-z]+", value.lower())
    joined = "".join(words)
    campus = bool(_CAMPUS_WORDS.intersection(words)) or any(w in joined for w in ("oncampus", "onsite", "inperson"))
    remote = bool(_REMOTE_WORDS.intersection(words))
    if campus != remote:
        return "On-campus" if campus else "Remote"
    return value


_FLAG_FALSE = frozenset({"no", "n", "false", "f", "0", "notsuspicious", "clear", "clean", "none", "pass",
                         "passed", "ok", "notflagged", "notplagiarized", "notplagiarised", "negative",
                         "notdetected", "nomatch"})
_FLAG_TRUE = frozenset({"yes", "y", "true", "t", "1", "suspicious", "flagged", "flag", "plagiarized",
                        "plagiarised", "plagiarism", "detected", "fail", "failed", "positive", "copied",
                        "match", "matched", "suspected"})


def _plagiarism_flag(value: Any) -> bool | None:
    """True/False from HackerRank-style status text; None if unrecognised. Blank -> False."""
    if not isinstance(value, str):
        return False
    key = re.sub(r"[^a-z0-9]", "", value.lower())
    if key in _FLAG_FALSE:
        return False
    if key in _FLAG_TRUE:
        return True
    try:
        return float(value) != 0.0
    except ValueError:
        pass
    if key.startswith(("not", "no")):
        return False
    if any(w in key for w in ("suspicious", "flag", "plagiar", "detect", "copied")):
        return True
    return None


def _parse_ips(value: str) -> list[str]:
    ips = []
    for token in re.split(r"[\s,;|]+", value):
        token = token.strip("[]()\"'")
        if token.count(":") == 1 and "." in token:  # IPv4 with port
            token = token.split(":")[0]
        try:
            ips.append(str(ipaddress.ip_address(token)))
        except ValueError:
            continue
    return ips


def ip_subnet(ip: Any) -> str | None:
    """IPv4 -> "a.b.c.0/24", IPv6 -> /64 network; None if not an IP."""
    try:
        addr = ipaddress.ip_address(str(ip))
    except ValueError:
        return None
    return str(ipaddress.ip_network(f"{addr}/{24 if addr.version == 4 else 64}", strict=False))


def normalize_candidate_id(value: Any) -> str | None:
    """Stripped, upper-cased ID ("12345.0" spreadsheet artefacts -> "12345"); None if blank."""
    text = _cell_text(value)
    if not text:
        return None
    return re.sub(r"^(\d+)\.0+$", r"\1", text.upper())


def _spelling_key(value: str) -> str:
    return re.sub(r"[\W_]+", "", value.casefold()) or value


def canonicalize_spellings(series: pd.Series) -> pd.Series:
    """Unify spellings differing only by case/whitespace/punctuation to the most frequent one.

    Deterministic tie-break: mixed-case spelling over all-lower / ALL-CAPS, then lexicographic.
    """
    counts = Counter(v for v in series.tolist() if isinstance(v, str))
    groups: dict[str, list[str]] = {}
    for spelling in counts:
        groups.setdefault(_spelling_key(spelling), []).append(spelling)
    replace: dict[str, str] = {}
    for spellings in groups.values():
        if len(spellings) > 1:
            winner = min(spellings, key=lambda s: (-counts[s], s.islower() or s.isupper(), s))
            replace.update({s: winner for s in spellings if s != winner})
    return series.replace(replace) if replace else series


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------
@dataclass
class _Ctx:
    report: DataQualityReport
    flags: list[list[str]]                       # per original row
    ids: list[str] = field(default_factory=list)  # candidate_id per original row

    def flag(self, mask: Iterable[bool] | pd.Series | np.ndarray, name: str,
             severity: str | None = None, message: str | None = None) -> int:
        """Append ``name`` to every flagged row (+ one issue per row if a message is given)."""
        rows = np.flatnonzero(np.asarray(mask, dtype=bool))
        for i in rows:
            self.flags[i].append(name)
            if severity and message:
                self.report.add(severity, STAGE, message, candidate_id=self.ids[i] if self.ids else None)
        return len(rows)


def _effective_mapping(columns: list[Any], mapping: dict[str, str], report: DataQualityReport) -> dict[int, str]:
    """Validate a (possibly user-edited) mapping -> {column position: canonical}.

    Blank targets are ignored, unknown targets warned about, and a canonical mapped from
    several columns keeps the earliest column. Only the first of duplicate headers maps.
    """
    by_name = {str(k): v for k, v in mapping.items()}
    names = [str(c) for c in columns]
    for src in by_name:
        if src not in names:
            report.add("info", STAGE, f"Mapped column '{src}' is not in the file; ignored.")
    effective: dict[int, str] = {}
    used: dict[str, str] = {}
    seen: set[str] = set()
    for j, name in enumerate(names):
        target = by_name.get(name) if name not in seen else None
        seen.add(name)
        if target is None or (isinstance(target, float) and math.isnan(target)) or not str(target).strip():
            continue
        target = str(target).strip()
        if not is_canonical_field(target):
            report.add("warning", STAGE, f"Column '{name}' is mapped to unknown field '{target}'; "
                                         f"kept as an extra column.")
        elif target in used:
            report.add("warning", STAGE, f"Columns '{used[target]}' and '{name}' both map to '{target}'; "
                                         f"using '{used[target]}'.")
        else:
            effective[j] = target
            used[target] = name
    return effective


def _extra_names(columns: list[Any]) -> list[str]:
    names: list[str] = []
    taken: set[str] = set()
    for i, col in enumerate(columns):
        base = EXTRA_PREFIX + (S.normalize_header(col) or f"col{i + 1}")
        name, k = base, 2
        while name in taken:
            name, k = f"{base}_{k}", k + 1
        taken.add(name)
        names.append(name)
    return names


_FIRST_NAME_KEYS = ("firstname", "fname", "givenname", "first")
_MIDDLE_NAME_KEYS = ("middlename", "mname")
_LAST_NAME_KEYS = ("lastname", "lname", "surname", "familyname", "last")


def _names_from_parts(raw: pd.DataFrame) -> pd.Series | None:
    """Join first [+ middle] + last name columns when no full-name column was mapped."""
    pos: dict[str, int] = {}
    for j, c in enumerate(raw.columns):
        pos.setdefault(S.normalize_header(c), j)
    first = next((pos[k] for k in _FIRST_NAME_KEYS if k in pos), None)
    last = next((pos[k] for k in _LAST_NAME_KEYS if k in pos), None)
    if first is None or last is None:
        return None
    middle = next((pos[k] for k in _MIDDLE_NAME_KEYS if k in pos), None)
    cols = [first] + ([middle] if middle is not None else []) + [last]
    parts = [_text(raw.iloc[:, j]).tolist() for j in cols]
    joined = [" ".join(p for p in row if isinstance(p, str)) or None for row in zip(*parts)]
    return pd.Series(joined, dtype="str")


def _candidate_ids(text: pd.Series | None, n: int, ctx: _Ctx) -> pd.Series:
    if text is None:
        ids: list[str | None] = [None] * n
        ctx.report.add("warning", STAGE, "No candidate ID column found; generated IDs ROW-00001, ROW-00002, ...")
    else:
        ids = [normalize_candidate_id(v) for v in text.tolist()]
    existing = {v for v in ids if v}
    blank = [i for i, v in enumerate(ids) if not v]
    for i in blank:
        gen = f"ROW-{i + 1:05d}"
        while gen in existing:
            gen += "X"
        existing.add(gen)
        ids[i] = gen
    ctx.ids = [str(v) for v in ids]
    if text is not None:
        mask = np.zeros(n, dtype=bool)
        mask[blank] = True
        ctx.flag(mask, FLAG_GENERATED_ID, "warning", "Blank candidate ID; generated an ID for this row.")
    return pd.Series(ctx.ids, dtype="str")


def _typical_max_by_test(tests: pd.Series, max_scores: np.ndarray) -> dict[str, float]:
    """Most common known max score per test name (ties -> smaller max)."""
    seen: dict[str, Counter[float]] = {}
    for name, value in zip(tests.tolist(), max_scores.tolist()):
        if isinstance(name, str) and not math.isnan(value):
            seen.setdefault(name, Counter())[value] += 1
    return {name: min(c.items(), key=lambda kv: (-kv[1], kv[0]))[0] for name, c in seen.items()}


def _scores(col: dict[str, pd.Series], n: int, ctx: _Ctx) -> dict[str, pd.Series]:
    """hackerrank_score / max_score / hackerrank_pct with the ingest contract rules."""
    report = ctx.report
    score_txt, max_txt, pct_txt = col.get(S.HR_SCORE), col.get(S.MAX_SCORE), col.get(S.HR_PCT)
    blank = np.full(n, np.nan)
    score = _floats(score_txt).to_numpy(copy=True) if score_txt is not None else blank.copy()
    max_in_score = blank.copy()
    if score_txt is not None:  # "85/100" style cells
        frac = score_txt.str.extract(r"^([-+]?\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)$").astype(float)
        has = frac[0].notna().to_numpy()
        score[has], max_in_score[has] = frac[0].to_numpy()[has], frac[1].to_numpy()[has]
    mx = _floats(max_txt).to_numpy(copy=True) if max_txt is not None else blank.copy()
    mx[mx <= 0] = np.nan
    max_in_score[max_in_score <= 0] = np.nan
    pct = _fraction_aware(pct_txt).clip(0, 100).to_numpy(copy=True) if pct_txt is not None else blank.copy()
    score_given = ~np.isnan(score)
    has_result = score_given | ~np.isnan(pct)

    # Missing max: "85/100" cell -> same test's usual max -> score/percentage -> default 100.
    fill = np.isnan(mx) & ~np.isnan(max_in_score)
    mx[fill] = max_in_score[fill]
    tests = col.get(S.TEST_NAME)
    if tests is not None and np.isnan(mx).any():
        typical = _typical_max_by_test(tests, mx)
        by_test = tests.map(typical).astype("float64").to_numpy()
        fill = np.isnan(mx) & ~np.isnan(by_test)
        mx[fill] = by_test[fill]
    infer = np.isnan(mx) & score_given & (score > 0) & ~np.isnan(pct) & (pct > 0)
    if infer.any():
        guess = score[infer] * 100.0 / pct[infer]
        mx[infer] = np.where(np.abs(guess - np.round(guess)) <= 0.05, np.round(guess), np.round(guess, 2))
        report.add("info", STAGE, f"Max score inferred from the percentage column for {int(infer.sum())} row(s).")
    defaulted = np.isnan(mx)
    mx[defaulted] = DEFAULT_MAX_SCORE
    if (defaulted & has_result).any():
        if max_txt is not None:
            ctx.flag(defaulted & has_result, FLAG_MAX_ASSUMED)
        report.add("info", STAGE, f"Max score missing for {int((defaulted & has_result).sum())} scored row(s); "
                                  f"assumed {DEFAULT_MAX_SCORE:g}.")

    from_pct = ~score_given & ~np.isnan(pct)
    score[from_pct] = pct[from_pct] * mx[from_pct] / 100.0
    ctx.flag(from_pct, FLAG_SCORE_FROM_PCT)
    missing = np.isnan(score)
    if ctx.flag(missing, FLAG_SCORE_MISSING):
        report.add("warning", STAGE, f"{int(missing.sum())} candidate(s) have no assessment score "
                                     f"(data flag 'score_missing').")
    exceeds = ~missing & (score > mx + 1e-9)
    score[exceeds] = mx[exceeds]
    ctx.flag(exceeds, FLAG_SCORE_EXCEEDS_MAX, "warning", "Score above max score; clipped to max.")
    ctx.flag(~missing & (score < 0), FLAG_SCORE_NEGATIVE, "warning", "Negative score; percentage clipped to 0.")

    with np.errstate(divide="ignore", invalid="ignore"):
        computed = np.round(np.clip(score / mx * 100.0, 0.0, 100.0), 4)
    computed[missing] = np.nan
    disagree = score_given & ~np.isnan(pct) & (np.abs(computed - pct) > PCT_TOLERANCE_PTS)
    if disagree.any():
        sample = ", ".join(ctx.ids[i] for i in np.flatnonzero(disagree)[:5])
        report.add("info", STAGE, f"Percentage column differs from score/max by more than {PCT_TOLERANCE_PTS:g} "
                                  f"pt for {int(disagree.sum())} row(s); score/max kept (e.g. {sample}).")
    return {S.HR_SCORE: pd.Series(score), S.MAX_SCORE: pd.Series(mx), S.HR_PCT: pd.Series(computed)}


_RATIO_RE = re.compile(r"^\s*([-+]?\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)")


def _cgpa(text: pd.Series, ctx: _Ctx) -> pd.Series:
    """CGPA on a 10-point scale: "8.5", "8.5/10", "3.6/4", "78.5%" / 78.5 (-> 7.85); 4-pt columns x2.5."""
    values = np.full(len(text), np.nan)
    plain = np.zeros(len(text), dtype=bool)
    for i, v in enumerate(text.tolist()):
        if not isinstance(v, str):
            continue
        m = _RATIO_RE.match(v)
        if m and float(m.group(2)) > 0:
            values[i] = float(m.group(1)) / float(m.group(2)) * 10.0
        elif "%" in v:
            values[i] = _to_float(v) / 10.0
        else:
            values[i], plain[i] = _to_float(v), True
    plain_vals = values[plain & ~np.isnan(values)]
    if len(plain_vals) and 0 < plain_vals.max() <= 4.0:
        values[plain] *= 2.5
        ctx.report.add("info", STAGE, "CGPA column looks like a 4-point scale; converted to 10-point (x2.5).")
    pct_like = plain & (values > 10) & (values <= 100)
    values[pct_like] /= 10.0
    bad = text.notna().to_numpy() & (np.isnan(values) | (values <= 0) | (values > 10))
    values[bad] = np.nan
    ctx.flag(bad, FLAG_CGPA_RANGE, "warning", "CGPA out of range or unparseable; left blank.")
    return pd.Series(values)


def _times(col: dict[str, pd.Series], ctx: _Ctx) -> dict[str, pd.Series]:
    """test_start_time / submission_time / completion_minutes / test_slot."""
    out: dict[str, pd.Series] = {}
    for canon, flag, label in ((S.TEST_START, FLAG_BAD_START, "test start time"),
                               (S.SUBMISSION_TIME, FLAG_BAD_SUBMIT, "submission time")):
        if canon in col:
            out[canon] = _parse_datetimes(col[canon])
            ctx.flag(col[canon].notna() & out[canon].isna(), flag, "warning", f"Unparseable {label}; left blank.")
    start, submit = out.get(S.TEST_START), out.get(S.SUBMISSION_TIME)
    span = None
    if start is not None and submit is not None:
        span = (submit - start).dt.total_seconds() / 60.0
        ctx.flag((span < 0).to_numpy(), FLAG_SUBMIT_BEFORE_START, "warning",
                 "Submission time is before the start time.")
        span = span.mask(span < 0)
    if S.COMPLETION_MIN in col:
        minutes = _completion_minutes(col[S.COMPLETION_MIN], ctx.report)
        bad = col[S.COMPLETION_MIN].notna().to_numpy() & (minutes.isna() | (minutes < 0)).to_numpy()
        minutes = minutes.mask(minutes < 0)
        if span is not None:
            minutes = minutes.fillna(span)
        ctx.flag(bad & minutes.isna().to_numpy(), FLAG_BAD_DURATION, "warning",
                 "Unparseable completion time; left blank.")
        out[S.COMPLETION_MIN] = minutes.round(4)
    elif span is not None:
        out[S.COMPLETION_MIN] = span.round(4)
        ctx.report.add("info", STAGE, "Completion minutes computed from start and submission times.")
    if start is not None:
        out[S.TEST_SLOT] = start.dt.floor("h").dt.strftime("%Y-%m-%d %H:00").astype("str")
    return out


def _ips(text: pd.Series, ctx: _Ctx) -> dict[str, pd.Series]:
    """First valid IP per cell (+ full list when any cell has several) and its /24 subnet."""
    cells = text.tolist()
    lists = [_parse_ips(v) if isinstance(v, str) else [] for v in cells]
    first = [ips[0] if ips else None for ips in lists]
    ctx.flag([isinstance(v, str) and not ips for v, ips in zip(cells, lists)], FLAG_INVALID_IP, "warning",
             "Unrecognised IP address; left blank.")
    subnets = {ip: ip_subnet(ip) for ip in set(first) if ip}
    out = {S.IP_ADDRESS: pd.Series(first, dtype="str"),
           S.IP_SUBNET: pd.Series([subnets.get(ip) if ip else None for ip in first], dtype="str")}
    if any(len(ips) > 1 for ips in lists):
        out[IP_ADDRESSES] = pd.Series([S.SEP_LIST.join(ips) or None for ips in lists], dtype="str")
    return out


def _resolve_plagiarism_matches(df: pd.DataFrame) -> pd.Series:
    """plagiarism_match (ID / email / unique name as exported) -> candidate_id in this feed, else NaN."""
    ids = df[S.CANDIDATE_ID].tolist()
    id_set = set(ids)
    by_email: dict[str, list[str]] = {}
    by_name: dict[str, list[str]] = {}
    for cid, email, name in zip(ids, df[S.EMAIL].tolist(), df[S.NAME].tolist()):
        if isinstance(email, str):
            by_email.setdefault(email, []).append(cid)
        if isinstance(name, str):
            by_name.setdefault(" ".join(name.lower().split()), []).append(cid)

    def resolve(own: str, value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        for token in [value, *re.split(r"[\s,;|]+", value)]:
            token = token.strip(" <>()[]\"'")
            if not token:
                continue
            cid = normalize_candidate_id(token)
            if cid in id_set and cid != own:
                return cid
            email = clean_email(token) if "@" in token else None
            others = sorted(c for c in by_email.get(email or "", []) if c != own)
            if others:
                return others[0]
        others = [c for c in by_name.get(" ".join(value.lower().split()), []) if c != own]
        return others[0] if len(others) == 1 else None

    return pd.Series([resolve(o, v) for o, v in zip(ids, df[S.PLAGIARISM_MATCH].tolist())],
                     index=df.index, dtype="str")


def _drop_duplicates(df: pd.DataFrame, ctx: _Ctx) -> pd.DataFrame:
    """One row per candidate_id: keep the latest submission_time (no time = oldest), else the last row."""
    if not df[S.CANDIDATE_ID].duplicated().any():
        return df
    order = [S.CANDIDATE_ID] + ([S.SUBMISSION_TIME] if S.SUBMISSION_TIME in df.columns else []) + ["_row"]
    ordered = df.sort_values(order, na_position="first", kind="mergesort")
    keep = ~ordered.duplicated(S.CANDIDATE_ID, keep="last")
    dropped = Counter(ordered.loc[~keep, S.CANDIDATE_ID].tolist())
    for cid, count in sorted(dropped.items()):
        ctx.report.add("warning", STAGE, f"Duplicate candidate ID: kept the latest submission and dropped "
                                         f"{count} other row(s).", candidate_id=cid)
    ctx.report.duplicates_dropped = sum(dropped.values())
    kept = ordered.loc[keep]
    first_seen = kept[S.CANDIDATE_ID].map(df.groupby(S.CANDIDATE_ID)["_row"].min()).to_numpy()
    kept = kept.iloc[np.argsort(first_seen, kind="stable")]  # keep the ID's first-appearance position
    for row, cid in zip(kept["_row"].tolist(), kept[S.CANDIDATE_ID].tolist()):
        if cid in dropped:
            ctx.flags[row].append(FLAG_DUPLICATE_ID)
    return kept


def normalize_ta_feed(raw: pd.DataFrame, mapping: dict[str, str], cfg: AppConfig
                      ) -> tuple[pd.DataFrame, DataQualityReport]:
    """Apply ``mapping`` (source header -> canonical) and the normalisation rules.

    Output: exactly one row per candidate_id; canonical + derived columns (identity and
    score columns always present), q{n}_* columns, data_flags, then unmapped source columns
    as ``extra_<normalised header>``. ``cfg`` is accepted for interface stability.
    """
    report = DataQualityReport()
    raw = raw.reset_index(drop=True)
    n = len(raw)
    report.rows_raw = n
    columns = list(raw.columns)
    eff = _effective_mapping(columns, mapping or {}, report)
    # Protected attributes (e.g. HackerRank's optional "Gender" field) are dropped outright:
    # never scored, displayed, exported or sent to an LLM - even if a mapping names them.
    protected = [j for j in range(len(columns))
                 if S.normalize_header(columns[j]) in S.PROTECTED_ATTRIBUTE_HEADERS]
    if protected:
        eff = {j: canon for j, canon in eff.items() if j not in protected}
        report.add("info", STAGE, "Protected attribute column(s) dropped and not used: "
                   + ", ".join(str(columns[j]) for j in protected) + ".")
    report.column_mapping = {str(columns[j]): canon for j, canon in eff.items()}
    unmapped = [j for j in range(len(columns)) if j not in eff and j not in protected]
    report.unmapped_columns = [str(columns[j]) for j in unmapped]
    mapped = set(eff.values())
    report.missing_fields = [c for c in S.TA_FIELD_ALIASES if c not in mapped]
    col: dict[str, pd.Series] = {canon: _text(raw.iloc[:, j]) for j, canon in eff.items()}
    names = col.get(S.NAME)
    if names is None:
        names = _names_from_parts(raw)
        if names is not None:
            report.add("info", STAGE, "Candidate names built from first/last name columns.")
    if S.HR_SCORE not in mapped and S.HR_PCT not in mapped:
        report.add("error", STAGE, "No HackerRank score column found; assessment scores will be blank.")
    if not mapped & set(S.IDENTITY_FIELDS) and names is None:
        report.add("error", STAGE, "No candidate ID, email or name column found; resumes cannot be linked.")

    ctx = _Ctx(report=report, flags=[[] for _ in range(n)])
    empty = pd.Series([None] * n, dtype="str")
    out: dict[str, pd.Series] = {S.CANDIDATE_ID: _candidate_ids(col.get(S.CANDIDATE_ID), n, ctx)}

    # identity --------------------------------------------------------------------
    out[S.NAME] = (empty if names is None else names).map(clean_name, na_action="ignore").astype("str")
    emails = col.get(S.EMAIL, empty)
    out[S.EMAIL] = emails.map(clean_email, na_action="ignore").astype("str")
    ctx.flag(emails.notna() & out[S.EMAIL].isna(), FLAG_INVALID_EMAIL, "warning", "Invalid email address; left blank.")
    out[S.EMAIL_DOMAIN] = out[S.EMAIL].str.extract(r"@([^@]+)$", expand=False).astype("str")
    phones = col.get(S.PHONE, empty)
    out[S.PHONE] = phones.map(clean_phone, na_action="ignore").astype("str")
    ctx.flag(phones.notna() & out[S.PHONE].isna(), FLAG_INVALID_PHONE, "warning",
             "Phone number has fewer than 10 usable digits; left blank.")

    # education / profile / free text ------------------------------------------------
    for canon in (S.COLLEGE, S.DEGREE, S.BRANCH, S.CITY, S.SOURCE, S.TEST_NAME, S.REPORT_URL,
                  S.PROCTORING_FLAGS, S.PLAGIARISM_MATCH):
        if canon in col:
            out[canon] = col[canon]
    if S.COLLEGE_TIER in col:
        out[S.COLLEGE_TIER] = col[S.COLLEGE_TIER].map(_college_tier, na_action="ignore").astype("str")
    if S.GRAD_YEAR in col:
        years = col[S.GRAD_YEAR].map(_grad_year, na_action="ignore").astype("float64")
        ctx.flag(col[S.GRAD_YEAR].notna() & years.isna(), FLAG_GRAD_YEAR, "warning",
                 "Unrecognised graduation year; left blank.")
        out[S.GRAD_YEAR] = years.round().astype("Int64")
    if S.CGPA in col:
        out[S.CGPA] = _cgpa(col[S.CGPA], ctx)
    if S.RESUME_FILE in col:
        out[S.RESUME_FILE] = col[S.RESUME_FILE].map(lambda v: re.split(r"[\\/]", v)[-1].strip() or None,
                                                    na_action="ignore").astype("str")

    # assessment --------------------------------------------------------------------
    out.update(_scores(col, n, ctx))
    for canon, func in ((S.TEST_STATUS, _test_status), (S.INTEGRITY_STATUS, _smart_title), (S.TEST_MODE, _test_mode)):
        if canon in col:
            out[canon] = col[canon].map(func, na_action="ignore").astype("str")
    out.update(_times(col, ctx))
    if S.IP_ADDRESS in col:
        out.update(_ips(col[S.IP_ADDRESS], ctx))
    for canon in (S.TAB_SWITCHES, S.COPY_PASTE, S.SECURE_MODE_EXITS):
        if canon in col:
            out[canon] = _counts(col[canon])
    if S.OUT_OF_WINDOW_SEC in col:
        secs = pd.Series([_duration_seconds(v, 1.0) if isinstance(v, str) else math.nan
                          for v in col[S.OUT_OF_WINDOW_SEC].tolist()], dtype="float64")
        out[S.OUT_OF_WINDOW_SEC] = secs.mask(secs < 0)
    if S.PLAGIARISM_FLAG in col:
        parsed = [_plagiarism_flag(v) for v in col[S.PLAGIARISM_FLAG].tolist()]
        unknown = sum(v is None for v in parsed)
        if unknown:
            report.add("warning", STAGE, f"{unknown} unrecognised plagiarism status value(s) treated as not flagged.")
        out[S.PLAGIARISM_FLAG] = pd.Series([bool(v) for v in parsed], dtype="bool")
    for canon in col:
        if canon == S.MAX_CODE_SIMILARITY or S.QUESTION_SIMILARITY_RE.match(canon):
            values = _fraction_aware(col[canon])
            bad = values.notna() & ((values < 0) | (values > 100))
            if bad.any():
                report.add("warning", STAGE, f"{int(bad.sum())} value(s) of '{canon}' outside 0-100; left blank.")
            out[canon] = values.mask(bad)
        elif S.ANSWER_HASH_RE.match(canon):
            out[canon] = col[canon].str.lower()
        elif S.QUESTION_SCORE_RE.match(canon):
            out[canon] = _floats(col[canon])

    # assemble, de-duplicate, cross-row rules ----------------------------------------------
    df = pd.DataFrame(out)
    for name, j in zip(_extra_names([columns[j] for j in unmapped]), unmapped):
        df[name] = raw.iloc[:, j].to_numpy()
    df["_row"] = np.arange(n)
    df = _drop_duplicates(df, ctx)

    shared = df[S.EMAIL].map(df[S.EMAIL].value_counts()).gt(1)
    for row in df.loc[shared, "_row"].tolist():
        ctx.flags[row].append(FLAG_SHARED_EMAIL)
    if shared.any():
        report.add("warning", STAGE, f"{int(shared.sum())} candidate(s) share an email address with another "
                                     f"candidate ID (data flag 'shared_email').")
    for canon in _CANONICAL_SPELLING_COLUMNS:
        if canon in df.columns:
            df[canon] = canonicalize_spellings(df[canon])
    if S.PLAGIARISM_MATCH in df.columns:
        df[PLAGIARISM_MATCH_ID] = _resolve_plagiarism_matches(df)
        unresolved = int((df[S.PLAGIARISM_MATCH].notna() & df[PLAGIARISM_MATCH_ID].isna()).sum())
        if unresolved:
            report.add("info", STAGE, f"{unresolved} 'most similar candidate' value(s) do not match any "
                                      f"candidate in this feed.")
    df[S.DATA_FLAGS] = pd.Series([S.SEP_NOTES.join(dict.fromkeys(ctx.flags[r])) for r in df["_row"].tolist()],
                                 index=df.index, dtype="str")

    q_cols = (S.answer_hash_columns(df.columns) + S.question_score_columns(df.columns)
              + S.question_similarity_columns(df.columns))
    extras = [c for c in df.columns if c.startswith(EXTRA_PREFIX)]
    ordered = [c for c in _OUTPUT_ORDER if c in df.columns] + q_cols + [S.DATA_FLAGS] + extras
    df = df[ordered].reset_index(drop=True)
    report.rows_clean = len(df)
    return df, report


def load_ta_feed(file_name: str, content: bytes, cfg: AppConfig,
                 mapping_override: dict[str, str] | None = None
                 ) -> tuple[pd.DataFrame, DataQualityReport]:
    """read_table -> (mapping_override or suggest_column_mapping) -> normalize_ta_feed.

    Raises ValueError (message safe to display) only when the file cannot be read.
    """
    raw = read_table(file_name, content)
    mapping = mapping_override or suggest_column_mapping(list(raw.columns))
    df, report = normalize_ta_feed(raw, mapping, cfg)
    source = "user-supplied" if mapping_override else "auto-detected"
    report.issues.insert(0, {
        "severity": "info", "stage": STAGE,
        "issue": f"Read {report.rows_raw:,} row(s); {len(report.column_mapping)} column(s) mapped ({source}), "
                 f"{len(report.unmapped_columns)} kept as extra columns.",
        "candidate_id": None, "file_name": Path(str(file_name)).name,
    })
    return df, report
