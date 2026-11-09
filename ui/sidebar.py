"""Sidebar: data source, recent datasets, column-mapping review, run button, LLM and data-quality status.

``render()`` draws the data-source controls and returns a RunRequest when the user starts a run
(synthetic sample, uploaded files or a cached dataset). ``render_status()`` draws the last-run
summary below an empty slot that ``render_llm_status()`` fills at the end of the script run, so
the GitHub Copilot proxy check (cached 30 s) never holds up the main page.

The pure helpers (mapping rows, mapping profiles, recent-dataset rows, LLM status note, sheet
detection) are public so they can be tested without Streamlit.
"""
from __future__ import annotations

import hashlib
import html
import io
import itertools
import json
import math
import re
import warnings
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any

import pandas as pd
import streamlit as st

from screening import ingest, snapshots
from screening import llm_backends as backends
from screening import schema as S
from screening.models import PipelineResult, ResumeDoc
from ui import state
from ui.theme import md_escape, md_text

SOURCE_SAMPLE = "Synthetic sample dataset"
SOURCE_UPLOAD = "Upload TA feed + resumes"
TA_TYPES = ["csv", "tsv", "txt", "xlsx", "xlsm", "xls"]
RESUME_TYPES = ["pdf", "docx", "txt", "zip"]
SAMPLE_CAPTION = ("Synthetic Workday export (2,133 applications) + Resume.zip with 33 PDF packets - "
                  "create it with `python generate_mock_data.py`")
TA_LABEL = "TA export: Workday or HackerRank (CSV or Excel)"
TA_HELP = ("Workday assessment export (.xlsx): the data sheet (Sheet2, after the recruiter summary on "
           "Sheet1) is detected automatically, and names come from the Job Application column. "
           "HackerRank CSV/XLSX exports work too. Headers are mapped automatically - review or change "
           "them under Review column mapping.")
RESUME_LABEL = "Resumes: Resume.zip, PDF, DOCX or TXT"
RESUME_HELP = ("Resume.zip with multi-candidate PDF packets (table of contents + bookmarks) is split per "
               "candidate automatically. Individual PDF/DOCX/TXT resumes and ZIPs of them work too.")
_Q_COL_RE = re.compile(r"^q(\d+)_(hash|score|similarity)$")
_WORKBOOK_MAGIC = b"PK\x03\x04"

# ---------------------------------------------------------------------------
# Column-mapping editor and saved mapping profiles
# ---------------------------------------------------------------------------
COL_SOURCE = "Source column"
COL_SAMPLES = "Sample values"
COL_TARGET = "Maps to"
COL_ACTION = "Action"
MAPPING_COLUMNS = [COL_SOURCE, COL_SAMPLES, COL_TARGET, COL_ACTION]
NOT_MAPPED = ""                      # "Maps to" value of a column that fills no field
ACTION_INCLUDE = "Include"
ACTION_IGNORE = "Ignore"             # leaves the column out of the mapping
ACTIONS = [ACTION_INCLUDE, ACTION_IGNORE]
SAMPLE_COUNT = 3                     # distinct sample values shown per column
SAMPLE_WIDTH = 30                    # characters per sample value
SAMPLE_SCAN_LIMIT = 5000             # distinct values inspected per column at most
_PLACEHOLDERS = frozenset({"na", "n/a", "n.a.", "nan", "null", "none", "nil", "-", "--", "---", "#n/a",
                           "not available", "not applicable"})
SHEET_SCAN_ROWS = 50                 # rows per sheet searched for the header (as ingest does)
PROFILE_VERSION = 1
PROFILE_IGNORE = "ignore"
PROFILE_MAX_BYTES = 64 * 1024
PROFILE_FILE_NAME = "column_mapping.json"
K_PROFILE_UPLOAD = "sb_mapping_upload"
K_PROFILE_DOWNLOAD = "sb_mapping_dl"
K_PROFILE = "_sb_mapping_profile"    # profile applied to the editor: digest, file id, assignments, version

# ---------------------------------------------------------------------------
# Recent datasets (snapshot cache) and LLM status
# ---------------------------------------------------------------------------
K_RECENT = "sb_recent_datasets"      # expander state: the cache folder is read only while it is open
RECENT_LABEL = "Recent datasets (fast reload)"
MAX_RECENT = 8
K_LLM_RECHECK = "_sb_llm_recheck_requested"
LLM_POLICY = ("The LLM only writes text (explanations and summaries); it never changes scores, ranks, "
              "cutoffs or the shortlist.")
MAX_MODEL_IDS = 3


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
def render() -> state.RunRequest | None:
    """Data-source controls. Returns a RunRequest when the user starts a run."""
    with st.sidebar:
        st.markdown("#### Data source")
        source = st.radio("Data source", [SOURCE_SAMPLE, SOURCE_UPLOAD], key="sb_source",
                          label_visibility="collapsed")
        request = _sample_controls() if source == SOURCE_SAMPLE else _upload_controls()
        cached = _recent_datasets()
        request = request or cached
        if st.session_state.get(state.K_RESULT) is not None:
            st.button("Clear results", on_click=state.clear_results, icon=":material/delete_sweep:",
                      width="stretch", key="sb_clear")
    return request


def render_status(result: PipelineResult | None) -> Any:
    """Summary of the last run (data quality, timings) below an empty slot for the LLM status.

    Returns the slot; app.py fills it with ``render_llm_status(slot)`` once the main page is drawn.
    """
    with st.sidebar:
        st.divider()
        slot = st.container()
        if result is not None:
            try:
                _run_summary(result)
            except Exception as exc:  # noqa: BLE001 - a summary problem must not break the page
                st.caption(f"Run summary unavailable ({state.safe_error(exc)}).")
    return slot


def render_llm_status(slot: Any = None) -> None:
    """GitHub Copilot (VS Code proxy) status in ``slot`` (from render_status) or the sidebar."""
    with slot if slot is not None else st.sidebar:
        try:
            _llm_status()
        except Exception as exc:  # noqa: BLE001 - a status display must never break the page
            st.caption(f"LLM status unavailable ({state.safe_error(exc)}).")


# ---------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------
def _sample_controls() -> state.RunRequest | None:
    available = state.sample_available()
    st.caption(SAMPLE_CAPTION)
    if not available:
        st.warning('Sample data not found. Run `python generate_mock_data.py` in the project root, then '
                   "click Run again.", icon=":material/info:")
    if st.button("Run screening", type="primary", icon=":material/play_arrow:", width="stretch",
                 disabled=not available, key="sb_run_sample"):
        return state.RunRequest(label="Synthetic sample", use_sample=True)
    return None


def _upload_controls() -> state.RunRequest | None:
    ta = st.file_uploader(TA_LABEL, type=TA_TYPES, key="sb_ta_file", help=TA_HELP)
    resumes = st.file_uploader(RESUME_LABEL, type=RESUME_TYPES, accept_multiple_files=True, key="sb_resumes",
                               help=RESUME_HELP)
    mapping = _mapping_review(ta) if ta is not None else None
    if ta is None and not resumes:
        st.caption("Upload a TA export, resumes, or both (resume-only screening is supported).")
    if not st.button("Run screening", type="primary", icon=":material/play_arrow:", width="stretch",
                     disabled=ta is None and not resumes, key="sb_run_upload"):
        return None
    ta_file = (Path(ta.name).name, ta.getvalue()) if ta is not None else None
    docs = [ResumeDoc(file_name=Path(f.name).name, content=f.getvalue(), source="upload") for f in resumes or []]
    label = ta_file[0] if ta_file else f"{len(docs):,} resume file(s)"
    return state.RunRequest(label=f"Upload: {label}", ta_file=ta_file, resume_docs=docs, mapping_override=mapping)


# ---------------------------------------------------------------------------
# Recent datasets (fast reload from the snapshot cache)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RecentDataset:
    """One cached dataset as listed in the sidebar."""

    fingerprint: str
    label: str
    details: str

    @property
    def short(self) -> str:
        """First 12 hex digits of the fingerprint (widget keys)."""
        return self.fingerprint[:12]


def _is_missing(value: Any) -> bool:
    try:
        return value is None or bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _count(value: Any) -> int | None:
    if _is_missing(value) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if math.isfinite(number) else None


def _when(value: Any) -> str:
    """'09 Nov 2026 14:32' for a timestamp, '' when missing or unreadable."""
    if _is_missing(value):
        return ""
    try:
        return pd.Timestamp(value).strftime("%d %b %Y %H:%M")
    except (TypeError, ValueError, OverflowError):
        return ""


def _dataset_details(rec: Mapping[str, Any]) -> str:
    parts = [f"Cached {_when(rec.get('created_at')) or 'at an unknown time'}"]
    for key, noun in (("candidates", "candidate"), ("resumes", "resume")):
        n = _count(rec.get(key))
        if n is not None:
            parts.append(f"{n:,} {noun}{'' if n == 1 else 's'}")
    size = rec.get("size_mb")
    if not _is_missing(size):
        try:
            parts.append(f"{float(size):,.1f} MB")
        except (TypeError, ValueError):
            pass
    expires = _when(rec.get("expires_at"))
    if expires:
        parts.append(f"expires {expires}")
    return " · ".join(parts)


def recent_rows(frame: Any) -> list[RecentDataset]:
    """Listable rows of ``state.recent_datasets()`` (None, empty frames and odd values tolerated).

    Rows without a valid fingerprint, or whose first 12 digits repeat an earlier row's, are skipped.
    """
    if not isinstance(frame, pd.DataFrame) or frame.empty or "fingerprint" not in frame.columns:
        return []
    rows: list[RecentDataset] = []
    seen: set[str] = set()
    for rec in frame.to_dict("records"):
        fp = rec.get("fingerprint")
        if not isinstance(fp, str) or not snapshots.FINGERPRINT_RE.match(fp) or fp[:12] in seen:
            continue
        seen.add(fp[:12])
        raw_label = rec.get("label")
        label = " ".join(str(raw_label).split()) if not _is_missing(raw_label) else ""
        rows.append(RecentDataset(fp, label or snapshots.DEFAULT_LABEL, _dataset_details(rec)))
    return rows


def cache_note(settings: Any) -> str:
    """Caption explaining where parsed data is cached and for how long (never an absolute path)."""
    try:
        days = float(getattr(settings, "ttl_days", 7))
    except (TypeError, ValueError):
        days = 7.0
    days = days if math.isfinite(days) and days >= 0 else 7.0
    span = f"{days:g} day{'' if days == 1 else 's'}"
    raw = str(getattr(settings, "cache_dir", "") or "").strip() or "data/cache"
    path = PurePath(raw)
    relative = not (path.is_absolute() or path.anchor or raw.startswith(("~", "/", "\\")) or ".." in path.parts)
    where = f"under `{path.as_posix()}`" if relative else "in the app's cache folder"
    return (f"Parsed data is cached locally {where} for {span} (contains candidate data; never committed) "
            "and loads in about a second.")


def _delete_cached(fingerprint: str, label: str) -> None:
    """Delete one cached dataset (button callback, so the list below is read after the deletion)."""
    try:
        removed = snapshots.delete_snapshot(fingerprint, state.get_config())
    except Exception as exc:  # noqa: BLE001 - e.g. files in use: tell the user, keep the app alive
        state.queue_message(f"Could not remove the cached dataset ({state.safe_error(exc)}).",
                            icon=":material/error:")
        return
    state.queue_message(f"Removed cached dataset {md_escape(label)}." if removed
                        else "That cached dataset was already removed.", icon=":material/delete:")


def _recent_datasets() -> state.RunRequest | None:
    """Expander listing cached datasets with Load / delete buttons (hidden when snapshots are off)."""
    cfg = state.get_config()
    settings = getattr(cfg, "snapshots", None)
    if settings is None or not getattr(settings, "enabled", False):
        return None
    request: state.RunRequest | None = None
    box = st.expander(RECENT_LABEL, icon=":material/history:", key=K_RECENT, on_change="rerun")
    with box:
        if box.open is False:  # collapsed: the cache folder is not read at all
            return None
        st.caption(cache_note(settings))
        rows = recent_rows(state.recent_datasets(cfg))
        if not rows:
            st.caption("No cached datasets yet - every screening run caches its parsed data here.")
            return None
        current = st.session_state.get(state.K_PREPARED_FP)
        for row in rows[:MAX_RECENT]:
            loaded = row.fingerprint == current
            with st.container(border=True):
                st.markdown(f"**{md_escape(row.label)}**")
                st.caption(row.details)
                load_col, delete_col = st.columns([3, 1], vertical_alignment="center")
                if load_col.button("Loaded" if loaded else "Load", key=f"sb_snap_load_{row.short}",
                                   icon=":material/check:" if loaded else ":material/bolt:", width="stretch",
                                   disabled=loaded, help="Screen this dataset again without re-parsing the files"):
                    request = state.RunRequest(label=f"{row.label} (cached)", snapshot_fp=row.fingerprint)
                delete_col.button(":material/delete:", key=f"sb_snap_del_{row.short}", type="tertiary",
                                  help="Remove this dataset from the cache", on_click=_delete_cached,
                                  args=(row.fingerprint, row.label))
        if len(rows) > MAX_RECENT:
            st.caption(f"{len(rows) - MAX_RECENT:,} older cached dataset(s) not shown.")
    return request


# ---------------------------------------------------------------------------
# Column-mapping helpers (pure)
# ---------------------------------------------------------------------------
def _clean(value: Any) -> str:
    return "" if _is_missing(value) else str(value).strip()


def _is_canonical(name: str) -> bool:
    check = state.engine_fn(ingest, "is_canonical_field")
    if check is not None:
        try:
            return bool(check(name))
        except Exception:  # noqa: BLE001 - fall back to the schema lists
            pass
    return name in S.TA_FIELD_ALIASES or name in S.TA_FIELD_LABELS or bool(_Q_COL_RE.match(name))


def field_label(value: Any) -> str:
    """'Candidate ID (candidate_id)' for a canonical field; '— not mapped —' for ''."""
    text = _clean(value)
    if not text:
        return "— not mapped —"
    if m := _Q_COL_RE.match(text):
        kind = {"hash": "answer hash", "score": "score", "similarity": "code similarity %"}[m.group(2)]
        return f"Q{m.group(1)} {kind} ({text})"
    return f"{S.TA_FIELD_LABELS.get(text, text)} ({text})"


def _question_order(name: str) -> tuple[int, str]:
    m = _Q_COL_RE.match(name)
    return (int(m.group(1)) if m else 10**6, name)


def field_options(targets: Iterable[Any] = ()) -> list[str]:
    """"Maps to" choices: '' (not mapped), every TA field, then other canonical ``targets`` (q{n}_...)."""
    fields = list(dict.fromkeys([*S.TA_FIELD_LABELS, *S.TA_FIELD_ALIASES]))
    extra = {t for t in (_clean(v) for v in targets) if t and t not in fields and _is_canonical(t)}
    return [NOT_MAPPED, *fields, *sorted(extra, key=_question_order)]


def sample_values(series: pd.Series, count: int = SAMPLE_COUNT, width: int = SAMPLE_WIDTH) -> str:
    """Up to ``count`` distinct non-empty values, each clipped to ``width`` characters, ' | '-joined.

    HTML entities are decoded (Workday writes line breaks as "&#xa;"), placeholders such as "#N/A"
    are skipped, and values that look the same once clipped are shown once.
    """
    out: list[str] = []
    try:
        values: Iterable[Any] = pd.unique(series.dropna())
    except TypeError:  # unhashable cells
        values = series.dropna().tolist()
    for value in itertools.islice(values, SAMPLE_SCAN_LIMIT):
        text = " ".join(html.unescape(str(value)).split())
        if not text or text.casefold() in _PLACEHOLDERS:
            continue
        shown = text if len(text) <= width else text[: width - 1].rstrip() + "…"
        if shown not in out:
            out.append(shown)
        if len(out) >= count:
            break
    return " | ".join(out)


def mapping_rows(sources: list[str], samples: Mapping[str, str], suggested: Mapping[str, str],
                 assignments: Mapping[str, str] | None = None) -> pd.DataFrame:
    """Editor rows: the auto-detected mapping, with a loaded profile's ``assignments`` applied
    (source header -> canonical field or "ignore")."""
    rows = []
    for src in sources:
        target, action = _clean(suggested.get(src)), ACTION_INCLUDE
        assigned = _clean((assignments or {}).get(src))
        if assigned == PROFILE_IGNORE:
            action = ACTION_IGNORE
        elif assigned:
            target = assigned
        rows.append({COL_SOURCE: src, COL_SAMPLES: samples.get(src, ""), COL_TARGET: target, COL_ACTION: action})
    return pd.DataFrame(rows, columns=MAPPING_COLUMNS)


def mapping_from_rows(rows: pd.DataFrame) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """(source -> canonical for included, mapped columns; (source, field) pairs dropped because an
    earlier column already maps to that field)."""
    mapping: dict[str, str] = {}
    duplicates: list[tuple[str, str]] = []
    if not isinstance(rows, pd.DataFrame) or not set(MAPPING_COLUMNS) <= set(rows.columns):
        return mapping, duplicates
    for src, target, action in zip(rows[COL_SOURCE], rows[COL_TARGET], rows[COL_ACTION]):
        dst = _clean(target)
        if _clean(action) == ACTION_IGNORE or not dst:
            continue
        if dst in mapping.values():
            duplicates.append((str(src), dst))
            continue
        mapping[str(src)] = dst
    return mapping, duplicates


def mapping_profile(rows: pd.DataFrame) -> dict[str, Any]:
    """{"version": 1, "mapping": {source header: canonical field | "ignore"}} for every column."""
    mapping: dict[str, str] = {}
    if isinstance(rows, pd.DataFrame) and set(MAPPING_COLUMNS) <= set(rows.columns):
        for src, target, action in zip(rows[COL_SOURCE], rows[COL_TARGET], rows[COL_ACTION]):
            dst = _clean(target)
            mapping[str(src)] = dst if dst and _clean(action) != ACTION_IGNORE else PROFILE_IGNORE
    return {"version": PROFILE_VERSION, "mapping": mapping}


def profile_bytes(rows: pd.DataFrame) -> bytes:
    return json.dumps(mapping_profile(rows), indent=2, ensure_ascii=False).encode("utf-8")


@dataclass
class ProfileResult:
    """Outcome of reading an uploaded mapping profile."""

    assignments: dict[str, str] = field(default_factory=dict)  # source header -> canonical | "ignore"
    skipped: list[str] = field(default_factory=list)           # profile headers not in this file
    error: str = ""                                            # Markdown-safe; "" when valid

    @property
    def ok(self) -> bool:
        return not self.error


def _shown(value: Any, limit: int = 40) -> str:
    text = " ".join(str(value).split())
    return md_escape(text if len(text) <= limit else text[: limit - 1] + "…")


def _header_key(value: Any) -> str:
    return " ".join(str(value).split())


def parse_mapping_profile(content: bytes | None, sources: Iterable[Any]) -> ProfileResult:
    """Validate a mapping profile against the uploaded file's headers. Never raises.

    Rules: at most 64 KB of UTF-8 JSON; ``{"version": 1, "mapping": {...}}``; text keys and
    values; every target a canonical field (``ingest.is_canonical_field``) or "ignore". Headers
    that are not in this file are reported in ``skipped`` (whitespace differences are ignored).
    """
    if content is None:
        return ProfileResult(error="No file was uploaded.")
    if len(content) > PROFILE_MAX_BYTES:
        return ProfileResult(error=f"The file is larger than {PROFILE_MAX_BYTES // 1024} KB - a mapping "
                                   "profile is a small JSON file.")
    try:
        data = json.loads(bytes(content).decode("utf-8-sig"))
    except UnicodeDecodeError:
        return ProfileResult(error="The file is not UTF-8 text.")
    except (ValueError, RecursionError):
        return ProfileResult(error="The file is not valid JSON.")
    if not isinstance(data, dict):
        return ProfileResult(error='Expected a JSON object with "version" and "mapping".')
    version = data.get("version")
    if isinstance(version, bool) or version != PROFILE_VERSION:
        shown = _shown(version) if isinstance(version, (int, float, str)) else "missing"
        return ProfileResult(error=f"Unsupported profile version ({shown}); expected version {PROFILE_VERSION}.")
    mapping = data.get("mapping")
    if not isinstance(mapping, dict):
        return ProfileResult(error='The profile has no "mapping" object.')
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in mapping.items()):
        return ProfileResult(error="Every mapping entry must map a column header (text) to a field name (text).")
    targets: dict[str, str] = {}
    unknown: list[str] = []
    for header, value in mapping.items():
        text = value.strip()
        if text.lower() == PROFILE_IGNORE:
            targets[header] = PROFILE_IGNORE
        elif _is_canonical(text):
            targets[header] = text
        elif _is_canonical(text.lower()):
            targets[header] = text.lower()
        else:
            unknown.append(value)
    if unknown:
        listed = ", ".join(_shown(u) or '""' for u in sorted(set(unknown))[:5])
        more = f" (+{len(set(unknown)) - 5} more)" if len(set(unknown)) > 5 else ""
        return ProfileResult(error=f"Unknown target field(s): {listed}{more}. Use canonical field names such as "
                                   f"candidate\\_id or hackerrank\\_score, or \"{PROFILE_IGNORE}\".")
    present = {_header_key(s): str(s) for s in sources}
    result = ProfileResult()
    for header, target in targets.items():
        src = present.get(_header_key(header))
        if src is None:
            result.skipped.append(header)
        else:
            result.assignments[src] = target
    return result


def detect_sheet(content: bytes, columns: Iterable[Any]) -> tuple[str, int] | None:
    """(sheet name, number of sheets) of the workbook sheet holding the header that was read.

    ingest.read_table picks the sheet with the best header match (a Workday export: Sheet2 after
    the Sheet1 recruiter summary); this finds the sheet with a row containing most of the columns
    it returned. None for non-workbooks, unreadable files or no clear match. Never raises.
    """
    if not isinstance(content, (bytes, bytearray)) or bytes(content[:4]) != _WORKBOOK_MAGIC:
        return None
    wanted = {_header_key(c) for c in columns if _header_key(c)}
    if not wanted:
        return None
    try:
        from openpyxl import load_workbook

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            book = load_workbook(io.BytesIO(bytes(content)), read_only=True, data_only=True)
    except Exception:  # noqa: BLE001 - not a readable workbook: no caption
        return None
    try:
        best, best_hits = None, 0
        for sheet in book.worksheets:
            for _, row in zip(range(SHEET_SCAN_ROWS), sheet.iter_rows(values_only=True)):
                hits = len(wanted & {_header_key(v) for v in row if v is not None})
                if hits > best_hits:
                    best, best_hits = str(sheet.title), hits
        count = len(book.worksheets)
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            book.close()
        except Exception:  # noqa: BLE001
            pass
    if best is None or best_hits * 2 < len(wanted):
        return None
    return best, count


# ---------------------------------------------------------------------------
# Column-mapping editor (Streamlit)
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner="Reading the TA feed...", max_entries=4, ttl=3600)
def _read_table_cached(file_name: str, digest: str, _content: bytes) -> pd.DataFrame:
    """ingest.read_table keyed by file name + content hash (bytes not re-hashed)."""
    return ingest.read_table(file_name, _content)


@st.cache_data(show_spinner=False, max_entries=4, ttl=3600)
def _samples_cached(digest: str, _raw: pd.DataFrame) -> dict[str, str]:
    return {str(c): sample_values(_raw[c]) for c in _raw.columns}


@st.cache_data(show_spinner=False, max_entries=8, ttl=3600)
def _sheet_used_cached(digest: str, columns: tuple[str, ...], _content: bytes) -> tuple[str, int] | None:
    return detect_sheet(_content, columns)


def _profile_record(digest: str, sources: list[str], uploaded: Any) -> dict[str, Any]:
    """Mapping profile applied to this file's editor (applied once per uploaded file)."""
    record = st.session_state.get(K_PROFILE)
    if not isinstance(record, dict) or record.get("digest") != digest:
        record = {"digest": digest, "file_id": None, "assignments": {}, "version": 0, "messages": []}
    if uploaded is None:
        record["file_id"], record["messages"] = None, []
    else:
        file_id = str(getattr(uploaded, "file_id", "") or f"{uploaded.name}:{uploaded.size}")
        if file_id != record.get("file_id"):
            size = int(getattr(uploaded, "size", 0) or 0)
            content = None if size > PROFILE_MAX_BYTES else uploaded.getvalue()
            result = (ProfileResult(error=f"The file is larger than {PROFILE_MAX_BYTES // 1024} KB - a mapping "
                                          "profile is a small JSON file.")
                      if content is None else parse_mapping_profile(content, sources))
            record["file_id"] = file_id
            if result.ok:
                record["assignments"] = dict(result.assignments)
                record["version"] = int(record.get("version", 0)) + 1
                record["messages"] = _profile_messages(Path(uploaded.name).name, result, sources)
            else:
                record["messages"] = [("error", f"Could not load the mapping profile: {result.error}")]
    st.session_state[K_PROFILE] = record
    return record


def _profile_messages(name: str, result: ProfileResult, sources: list[str]) -> list[tuple[str, str]]:
    n = len(result.assignments)
    messages = [("success", f"Applied mapping profile {md_escape(name)} to {n:,} column{'' if n == 1 else 's'}.")]
    if result.skipped:
        listed = ", ".join(_shown(h) for h in result.skipped[:6])
        more = f" (+{len(result.skipped) - 6:,} more)" if len(result.skipped) > 6 else ""
        messages.append(("info", f"Not in this file, skipped: {listed}{more}."))
    uncovered = len([s for s in sources if s not in result.assignments])
    if uncovered:
        messages.append(("info", f"{uncovered:,} column{'' if uncovered == 1 else 's'} of this file "
                                 f"{'is' if uncovered == 1 else 'are'} not in the profile and keep the "
                                 "auto-detected mapping."))
    return messages


def _editor_columns(options: list[str]) -> dict[str, Any]:
    return {
        COL_SOURCE: st.column_config.TextColumn(COL_SOURCE, width="medium", pinned=True,
                                                help="Column header in the uploaded file"),
        COL_SAMPLES: st.column_config.TextColumn(COL_SAMPLES, width="medium",
                                                 help="Up to 3 distinct values from the file"),
        COL_TARGET: st.column_config.SelectboxColumn(
            COL_TARGET, options=options, format_func=field_label, width="medium",
            help="Field this column fills; '— not mapped —' keeps it only as an extra column"),
        COL_ACTION: st.column_config.SelectboxColumn(
            COL_ACTION, options=ACTIONS, required=True, width="small",
            help="Ignore leaves the column out of the mapping"),
    }


def _mapping_review(ta: Any) -> dict[str, str] | None:
    """Editable header -> canonical mapping. Returns an override only when it differs from the
    auto-detected one."""
    with st.expander("Review column mapping", icon=":material/table_edit:"):
        read_table = state.engine_fn(ingest, "read_table")
        suggest = state.engine_fn(ingest, "suggest_column_mapping")
        if read_table is None or suggest is None:
            st.info("Mapping preview is unavailable in this build; columns will be auto-mapped.")
            return None
        content = ta.getvalue()
        digest = hashlib.sha256(content).hexdigest()
        try:
            raw = _read_table_cached(Path(ta.name).name, digest, content)
            sources = [str(c) for c in raw.columns]
            suggested = {str(k): str(v) for k, v in suggest(sources).items() if v}
            samples = _samples_cached(digest, raw)
        except Exception as exc:  # noqa: BLE001 - malformed upload: explain, fall back to auto-mapping
            st.warning(f"Could not preview this file: {state.safe_error(exc)}")
            return None
        sheet = _sheet_used_cached(digest, tuple(sources), content)
        if sheet is not None and sheet[1] > 1:
            st.caption(f"Sheet used: **{md_escape(sheet[0])}** - detected automatically as the best header "
                       f"match among the workbook's {sheet[1]} sheets.")
        uploaded = st.file_uploader("Load mapping profile", type=["json"], key=K_PROFILE_UPLOAD,
                                    help=f"A {PROFILE_FILE_NAME} saved with Download mapping below "
                                         "(at most 64 KB). It replaces the table's mapping.")
        record = _profile_record(digest, sources, uploaded)
        for kind, text in record["messages"]:
            {"success": st.success, "error": st.error}.get(kind, st.info)(text)
        assignments = record["assignments"]
        rows = mapping_rows(sources, samples, suggested, assignments)
        key = f"sb_mapping_{digest[:16]}" + (f"_v{record['version']}" if record["version"] else "")
        edited = st.data_editor(
            rows, key=key, hide_index=True, width="stretch", num_rows="fixed",
            disabled=[COL_SOURCE, COL_SAMPLES],
            column_config=_editor_columns(field_options([*suggested.values(), *assignments.values()])),
        )
        mapping, duplicates = mapping_from_rows(edited)
        if duplicates:
            st.warning("Each field can be mapped once; ignored: " + "; ".join(
                f"{md_escape(src)} → {md_escape(field_label(dst))}" for src, dst in duplicates))
        missing = [S.TA_FIELD_LABELS.get(f, f) for f in S.REQUIRED_FIELDS if f not in mapping.values()]
        if missing:
            st.warning(f"Not mapped: {', '.join(missing)} - candidates will be scored without it.")
        ignored = int((edited[COL_ACTION] == ACTION_IGNORE).sum()) if COL_ACTION in edited.columns else 0
        st.caption(f"{len(mapping)} of {len(sources)} columns mapped" + (f", {ignored} ignored" if ignored else "")
                   + ". Ignored and unmapped columns are not scored (kept as `extra_*` fields).")
        st.download_button("Download mapping (JSON)", data=profile_bytes(edited), file_name=PROFILE_FILE_NAME,
                           mime="application/json", key=K_PROFILE_DOWNLOAD, on_click="ignore",
                           icon=":material/download:", width="stretch",
                           help="Save this mapping as a profile to reuse with the next export")
        if not mapping:
            if suggested:
                st.warning("No column is mapped - the auto-detected mapping will be used instead.")
            return None
        return mapping if mapping != suggested else None


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
def _model_id(value: Any) -> str:
    text = " ".join(str(value).replace("`", "").split())
    return text if len(text) <= 40 else text[:39] + "…"


def llm_status_note(enabled: bool, status: Mapping[str, Any] | None) -> tuple[str, str]:
    """(badge colour, Markdown note) for the LLM status. Shows where the token came from, never the token."""
    if not enabled:
        return "gray", "LLM features off - switch them on in Config & Rubric Tuning."
    status = status if isinstance(status, Mapping) else {}
    source = _clean(status.get("token_source"))
    via = f" · token from {md_escape(source)}" if source else ""
    raw_models = status.get("models")
    models = [m for m in (raw_models if isinstance(raw_models, (list, tuple)) else []) if _model_id(m)]
    error = _clean(status.get("error"))
    if status.get("reachable") and models:
        shown = ", ".join(f"`{_model_id(m)}`" for m in models[:MAX_MODEL_IDS])
        more = f" +{len(models) - MAX_MODEL_IDS} more" if len(models) > MAX_MODEL_IDS else ""
        return "green", f"**Connected** · {len(models)} model{'' if len(models) == 1 else 's'}: {shown}{more}{via}"
    if status.get("reachable"):
        return "orange", f"**Connected, but no models** - {md_text(error or backends.NO_MODELS_TEXT)}{via}"
    return "orange", f"**Not connected** - {md_text(error or backends.PROXY_GUIDANCE)}{via}"


def _request_llm_recheck() -> None:
    st.session_state[K_LLM_RECHECK] = True


def _llm_status() -> None:
    """Copilot proxy status: one (cached) state.llm_status call per run, none when LLM features are off."""
    cfg = state.get_config()
    refresh = bool(st.session_state.pop(K_LLM_RECHECK, False))
    status = state.llm_status(cfg, refresh=refresh) if cfg.llm.enabled else None
    color, note = llm_status_note(bool(cfg.llm.enabled), status)
    st.badge(backends.provider_label(cfg.llm), icon=":material/smart_toy:", color=color)  # type: ignore[arg-type]
    st.caption(note)
    if status is not None:
        st.button("Check again", key="sb_llm_recheck", icon=":material/refresh:", type="tertiary",
                  on_click=_request_llm_recheck,
                  help="Check the Copilot proxy now (the status is kept for 30 s and refreshed on your next action).")
    st.caption(LLM_POLICY)


def _run_summary(result: PipelineResult) -> None:
    q = result.quality
    counts = q.counts()
    st.markdown("#### Last run")
    st.caption(f"{state.MODE_LABELS.get(result.mode, result.mode)} · {q.rows_raw:,} rows in feed → "
               f"{q.rows_clean:,} candidates · {q.duplicates_dropped:,} duplicate row(s) dropped")
    st.markdown(f"🔴 {counts.get('error', 0):,} errors · 🟠 {counts.get('warning', 0):,} warnings · "
                f"🔵 {counts.get('info', 0):,} info")
    with st.expander("Data quality details", icon=":material/fact_check:"):
        issues = q.to_frame()
        if issues.empty:
            st.caption("No issues recorded.")
        else:
            order = {"error": 0, "warning": 1, "info": 2}
            issues = issues.sort_values("severity", key=lambda s: s.map(order).fillna(3), kind="stable")
            st.dataframe(issues, hide_index=True, height=260, width="stretch")
        if q.missing_fields:
            st.caption("Optional fields absent: " + ", ".join(
                md_escape(S.TA_FIELD_LABELS.get(f, f)) for f in q.missing_fields))
        if q.unmapped_columns:
            st.caption("Kept as `extra_*` columns: " + ", ".join(md_escape(c) for c in map(str, q.unmapped_columns)))
    if result.timings:
        with st.expander("Timings", icon=":material/timer:"):
            st.dataframe(pd.DataFrame({"stage": list(result.timings),
                                       "seconds": [round(float(v), 2) for v in result.timings.values()]}),
                         hide_index=True, width="stretch")
