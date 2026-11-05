"""Saved screening runs: save, list, reopen, rename, delete and compare.

Each run is one folder ``<base_dir>/<run_id>/`` (run_id ``YYYYMMDD-HHMMSS-<6 hex>``):

    meta.json              label, created_at (ISO 8601), dataset label, mode, counts, app version,
                           config, timings and a manifest of the stored tables
    candidates.parquet     PipelineResult.candidates
    base.parquet           PreparedData.base
    ta.parquet             PreparedData.ta_df (absent for resume-only runs)
    matches.parquet        PreparedData.matches
    risk_<name>.parquet    RiskResult tables (candidate_risk, pairs, clusters, commonality, individual)
    risk_meta.json         RiskResult focus_ids, params and warnings
    quality.json           DataQualityReport fields
    parsed.json.gz         every ParsedResume keyed by file_hash, plus the candidate -> resume links
    llm.json               the session's LLM outputs (re-scores, rationales, summaries) and the
                           re-scores applied to the result

Only JSON, Parquet and gzipped JSON are written - never pickle - so opening a saved run cannot
execute code. A run is written into a temporary sibling folder and moved into place with a single
rename, so other readers never see a half-written run. Run ids are validated against a strict
pattern and resolved inside ``base_dir`` only.

Saved runs hold candidate personal data (names, contact details, resume text and integrity
evidence), so the organisation's data-retention policy applies to the runs folder.
"""
from __future__ import annotations

import base64
import gzip
import importlib
import json
import logging
import math
import os
import re
import secrets
import shutil
import stat
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, fields, is_dataclass
from datetime import date, datetime, time as dtime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from . import __version__
from . import schema as S
from .config import AppConfig
from .models import DataQualityReport, ParsedResume, PipelineResult, RiskResult
from .pipeline import PreparedData

log = logging.getLogger(__name__)

ENV_RUNS_DIR = "SCREENING_RUNS_DIR"
DEFAULT_RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"
FORMAT_VERSION = 1
RUN_ID_RE = re.compile(r"\A[0-9]{8}-[0-9]{6}-[0-9a-f]{6}\Z")
MAX_LABEL_LEN = 120
DEFAULT_LABEL = "Untitled run"
IN_SHORTLIST = (S.STATUS_SHORTLISTED, S.STATUS_REVIEW)
COUNT_KEYS = ["candidates", "shortlisted", "in_review", "high", "medium", "perfect_scorers", "clusters"]
LIST_COLUMNS = ["run_id", "label", "created_at", "dataset", "mode", *COUNT_KEYS, "size_mb"]
RISK_FRAMES = ("candidate_risk", "pairs", "clusters", "commonality", "individual")
DIFF_COLUMNS = [S.CANDIDATE_ID, S.NAME, S.COLLEGE, "rank_before", "rank_after", "rank_change", "status_before",
                "status_after", "score_before", "score_after", "reason_after", "shortlisted_before",
                "shortlisted_after"]

_REQUIRED_FILES = ("meta.json", "candidates.parquet", "base.parquet", "risk_meta.json", "quality.json",
                   "parsed.json.gz")
_TMP_PREFIX = ".tmp-"
_TRASH_PREFIX = ".trash-"
_STALE_TMP_SECONDS = 6 * 3600
_PARQUET_KEY = b"screening.runs"
_TAG = "$t"
_ARROW_ERRORS = (pa.ArrowException, TypeError, ValueError)
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")
_PARSED_FIELDS = frozenset(f.name for f in fields(ParsedResume))
_QUALITY_FIELDS = frozenset(f.name for f in fields(DataQualityReport))


class RunStoreError(Exception):
    """A saved-run operation failed. Messages are safe to show to users (no file paths)."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def clean_label(label: object, default: str = DEFAULT_LABEL) -> str:
    """Single-line label without control characters, at most MAX_LABEL_LEN characters."""
    text = _CONTROL_CHARS.sub(" ", "" if label is None else str(label))
    text = re.sub(r"\s+", " ", text).strip()[:MAX_LABEL_LEN].rstrip()
    return text or default


def _os_reason(exc: OSError) -> str:
    """OSError description without the file name (paths never reach the UI)."""
    return exc.strerror or type(exc).__name__


def _retry(fn: Callable[..., Any], *args: Any, attempts: int = 5, delay: float = 0.1) -> Any:
    """Retry briefly on PermissionError (Windows sync/antivirus tools hold files for a moment)."""
    for attempt in range(attempts):
        try:
            return fn(*args)
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay * (2 ** attempt))
    return None  # pragma: no cover - loop always returns or raises


def _make_writable(func: Callable[..., Any], path: str, *_exc: Any) -> None:
    """rmtree error handler: clear the read-only bit and retry once."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def _remove_tree(path: Path) -> None:
    """Best-effort recursive delete (never raises)."""
    try:
        if not path.exists():
            return
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=_make_writable)
        else:  # pragma: no cover - exercised on Python 3.11 only
            shutil.rmtree(path, onerror=_make_writable)
    except OSError as exc:
        log.warning("Could not remove a runs work folder (%s)", _os_reason(exc))


def _frame_or_empty(value: Any) -> pd.DataFrame:
    return value if isinstance(value, pd.DataFrame) else pd.DataFrame()


def _text(df: pd.DataFrame, name: str) -> pd.Series:
    if name in df.columns:
        return df[name].fillna("").astype(str)
    return pd.Series([""] * len(df), index=df.index, dtype=object)


def _number(df: pd.DataFrame, name: str) -> pd.Series:
    if name in df.columns:
        return pd.to_numeric(df[name], errors="coerce")
    return pd.Series([np.nan] * len(df), index=df.index, dtype=float)


# ---------------------------------------------------------------------------
# JSON documents (meta, quality, risk, LLM outputs, parsed resumes)
# ---------------------------------------------------------------------------
def _json_default(obj: Any) -> Any:
    """Fallback for values the json module cannot encode natively."""
    if obj is pd.NA or obj is pd.NaT:
        return None
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (datetime, date, dtime)):
        return obj.isoformat()
    if isinstance(obj, timedelta):
        return obj.total_seconds()
    if isinstance(obj, (set, frozenset)):
        return sorted(obj, key=str)
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, Path):
        return obj.as_posix()
    if isinstance(obj, (bytes, bytearray)):
        return base64.b64encode(bytes(obj)).decode("ascii")
    if isinstance(obj, pd.DataFrame):
        return obj.to_dict("records")
    if isinstance(obj, pd.Series):
        return obj.tolist()
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    return str(obj)


def _json_key(key: Any) -> Any:
    if isinstance(key, np.generic):
        key = key.item()
    if key is None or isinstance(key, (str, int, float, bool)):
        return key
    return str(_json_default(key))


def _plain(obj: Any) -> Any:
    """Make dict keys JSON-safe (json rejects e.g. numpy integer or tuple keys)."""
    if isinstance(obj, dict):
        return {_json_key(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    return obj


def _json_bytes(obj: Any, *, compact: bool = False) -> bytes:
    if compact:
        text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=_json_default)
    else:
        text = json.dumps(_plain(obj), ensure_ascii=False, indent=2, default=_json_default)
    return text.encode("utf-8")


def _write_json(path: Path, obj: Any) -> None:
    path.write_bytes(_json_bytes(obj))


def _read_json(path: Path) -> Any:
    return json.loads(path.read_bytes().decode("utf-8"))


# ---------------------------------------------------------------------------
# Parquet tables: native columns where Arrow round-trips them, JSON text otherwise
# ---------------------------------------------------------------------------
def _sort_key(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _enc(value: Any) -> Any:
    """Encode one cell as JSON data, tagging the types JSON cannot represent."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if value is pd.NA:
        return {_TAG: "na"}
    if value is pd.NaT:
        return {_TAG: "nat"}
    if isinstance(value, np.datetime64):
        return {_TAG: "nat"} if np.isnat(value) else _enc(pd.Timestamp(value))
    if isinstance(value, np.timedelta64):
        return {_TAG: "nat"} if np.isnat(value) else _enc(pd.Timedelta(value))
    if isinstance(value, np.generic):
        return _enc(value.item())
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, pd.Timestamp):
        return {_TAG: "ts", "v": value.isoformat()}
    if isinstance(value, datetime):
        return {_TAG: "dt", "v": value.isoformat()}
    if isinstance(value, date):
        return {_TAG: "date", "v": value.isoformat()}
    if isinstance(value, dtime):
        return {_TAG: "time", "v": value.isoformat()}
    if isinstance(value, timedelta):
        return {_TAG: "td", "v": int(pd.Timedelta(value).value)}
    if isinstance(value, Decimal):
        return {_TAG: "dec", "v": str(value)}
    if isinstance(value, complex):
        return {_TAG: "complex", "v": [value.real, value.imag]}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {_TAG: "bytes", "v": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, list):
        return [_enc(v) for v in value]
    if isinstance(value, tuple):
        return {_TAG: "tuple", "v": [_enc(v) for v in value]}
    if isinstance(value, (set, frozenset)):
        kind = "frozenset" if isinstance(value, frozenset) else "set"
        return {_TAG: kind, "v": sorted((_enc(v) for v in value), key=_sort_key)}
    if isinstance(value, np.ndarray):
        return {_TAG: "array", "dtype": value.dtype.str, "shape": list(value.shape),
                "v": [_enc(v) for v in value.ravel()]}
    if isinstance(value, dict):
        if all(isinstance(k, str) for k in value) and _TAG not in value:
            return {k: _enc(v) for k, v in value.items()}
        return {_TAG: "dict", "v": [[_enc(k), _enc(v)] for k, v in value.items()]}
    return {_TAG: "text", "v": str(value)}


def _dec(value: Any) -> Any:
    """Inverse of _enc."""
    if isinstance(value, list):
        return [_dec(v) for v in value]
    if not isinstance(value, dict):
        return value
    tag = value.get(_TAG)
    if not isinstance(tag, str):
        return {k: _dec(v) for k, v in value.items()}
    data = value.get("v")
    if tag == "na":
        return pd.NA
    if tag == "nat":
        return pd.NaT
    if tag == "ts":
        return pd.Timestamp(data)
    if tag == "dt":
        return datetime.fromisoformat(data)
    if tag == "date":
        return date.fromisoformat(data)
    if tag == "time":
        return dtime.fromisoformat(data)
    if tag == "td":
        return pd.Timedelta(int(data), unit="ns")
    if tag == "dec":
        return Decimal(data)
    if tag == "complex":
        return complex(data[0], data[1])
    if tag == "bytes":
        return base64.b64decode(data)
    if tag == "tuple":
        return tuple(_dec(v) for v in data)
    if tag == "set":
        return {_dec(v) for v in data}
    if tag == "frozenset":
        return frozenset(_dec(v) for v in data)
    if tag == "array":
        arr = np.array([_dec(v) for v in data], dtype=np.dtype(value.get("dtype", "O")))
        return arr.reshape(value.get("shape") or [-1])
    if tag == "dict":
        return {_dec(k): _dec(v) for k, v in data}
    if tag == "text":
        return str(data)
    return {k: _dec(v) for k, v in value.items()}


def _dumps_cell(value: Any) -> str:
    return json.dumps(_enc(value), ensure_ascii=False, separators=(",", ":"))


def _loads_cell(text: Any) -> Any:
    return _dec(json.loads(text)) if isinstance(text, str) else text


def _is_missing(value: Any) -> bool:
    if value is None or value is pd.NA or value is pd.NaT:
        return True
    return isinstance(value, (float, np.floating)) and math.isnan(value)


def _needs_json(series: pd.Series) -> bool:
    """True for object columns Parquet would reject or bring back with different values."""
    if series.dtype != object:
        return False
    values = [v for v in series.tolist() if not _is_missing(v)]
    if not values:
        return len(series) > 0  # all-missing: keep None vs NaN exactly
    if all(isinstance(v, str) for v in values):
        return False
    return not all(isinstance(v, datetime) for v in values)  # keep datetimes native


def _json_column(series: pd.Series) -> pd.Series:
    return pd.Series([_dumps_cell(v) for v in series.tolist()], index=series.index, dtype=object)


def _encode_frame(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    info: dict[str, Any] = {"format": FORMAT_VERSION, "json_columns": []}
    out = df.copy(deep=False)
    names = list(out.columns)
    if not all(isinstance(c, str) for c in names) or len(set(names)) != len(names):
        info["names"] = [_enc(c) for c in names]
        info["multiindex"] = isinstance(out.columns, pd.MultiIndex)
        info["level_names"] = [_enc(n) for n in out.columns.names]
        out.columns = [f"__col{i}" for i in range(len(names))]
    for name in out.columns:
        if _needs_json(out[name]):
            out[name] = _json_column(out[name])
            info["json_columns"].append(name)
    return out, info


def _to_table(frame: pd.DataFrame, info: dict[str, Any]) -> pa.Table:
    try:
        return pa.Table.from_pandas(frame, preserve_index=None)
    except _ARROW_ERRORS:
        pass
    for name in frame.columns:  # store whatever Arrow still rejects as JSON text
        if name in info["json_columns"]:
            continue
        try:
            pa.Table.from_pandas(frame[[name]], preserve_index=False)
        except _ARROW_ERRORS:
            frame[name] = _json_column(frame[name].astype(object))
            info["json_columns"].append(name)
    try:
        return pa.Table.from_pandas(frame, preserve_index=None)
    except _ARROW_ERRORS:
        info["index_dropped"] = True  # an exotic index Arrow cannot store
        return pa.Table.from_pandas(frame, preserve_index=False)


def write_frame(df: pd.DataFrame, path: str | Path) -> None:
    """Write ``df`` to Parquet so that read_frame() returns an equal frame (no pickle involved)."""
    frame, info = _encode_frame(df)
    table = _to_table(frame, info)
    metadata = dict(table.schema.metadata or {})
    metadata[_PARQUET_KEY] = json.dumps(info, ensure_ascii=False).encode("utf-8")
    pq.write_table(table.replace_schema_metadata(metadata), str(path), compression="zstd")


def read_frame(path: str | Path) -> pd.DataFrame:
    """Read a table written by write_frame (also plain Parquet files)."""
    table = pq.read_table(str(path))
    raw = (table.schema.metadata or {}).get(_PARQUET_KEY)
    info = json.loads(raw.decode("utf-8")) if raw else {}
    df = table.to_pandas()
    for name in info.get("json_columns") or []:
        if name in df.columns:
            df[name] = pd.Series([_loads_cell(v) for v in df[name].tolist()], index=df.index, dtype=object)
    names = info.get("names")
    if isinstance(names, list) and len(names) == len(df.columns):
        columns = pd.Index([_dec(n) for n in names], tupleize_cols=bool(info.get("multiindex")))
        level_names = info.get("level_names")
        if isinstance(level_names, list) and len(level_names) == columns.nlevels:
            columns = columns.set_names([_dec(n) for n in level_names])
        df.columns = columns
    return df


# ---------------------------------------------------------------------------
# Parsed resumes
# ---------------------------------------------------------------------------
def _resume_dict(resume: Any) -> dict[str, Any]:
    if is_dataclass(resume) and not isinstance(resume, type):
        return {f.name: getattr(resume, f.name) for f in fields(resume)}
    if isinstance(resume, dict):
        return dict(resume)
    raise RunStoreError("A parsed resume has an unexpected type and cannot be saved.")


def _resume_from(key: str, data: Any) -> ParsedResume:
    if not isinstance(data, dict):
        raise ValueError("parsed resume record is not an object")
    kwargs = {k: v for k, v in data.items() if k in _PARSED_FIELDS}
    kwargs.setdefault("file_name", "")
    kwargs.setdefault("file_hash", key)
    return ParsedResume(**kwargs)


def _parsed_payload(parsed: dict[str, Any], by_candidate: dict[str, Any]) -> dict[str, Any]:
    """All parsed resumes plus candidate -> resume links (shared objects are stored once)."""
    records = {str(k): _resume_dict(p) for k, p in (parsed or {}).items()}
    key_of = {id(p): str(k) for k, p in (parsed or {}).items()}
    links: dict[str, str] = {}
    extra: dict[str, dict[str, Any]] = {}
    for cid, resume in (by_candidate or {}).items():
        key = key_of.get(id(resume))
        if key is None:
            file_hash = str(getattr(resume, "file_hash", ""))
            if file_hash in records and parsed.get(file_hash) == resume:
                key = file_hash
            else:
                key = f"candidate:{cid}"
                extra[key] = _resume_dict(resume)
        links[str(cid)] = key
    return {"format": FORMAT_VERSION, "parsed": records, "extra": extra, "by_candidate": links}


def _links_from_base(base: pd.DataFrame, parsed: dict[str, ParsedResume]) -> dict[str, ParsedResume]:
    """candidate_id -> ParsedResume via base.resume_hash (fallback when no links were stored)."""
    if base is None or S.CANDIDATE_ID not in base.columns or S.RESUME_HASH not in base.columns:
        return {}
    out: dict[str, ParsedResume] = {}
    for cid, file_hash in zip(_text(base, S.CANDIDATE_ID), _text(base, S.RESUME_HASH)):
        if cid and file_hash in parsed:
            out[cid] = parsed[file_hash]
    return out


def _read_parsed(path: Path, base: pd.DataFrame) -> tuple[dict[str, ParsedResume], dict[str, ParsedResume]]:
    payload = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
    parsed = {str(k): _resume_from(str(k), d) for k, d in (payload.get("parsed") or {}).items()}
    extra = {str(k): _resume_from(str(k), d) for k, d in (payload.get("extra") or {}).items()}
    links = payload.get("by_candidate")
    if not isinstance(links, dict):
        return parsed, _links_from_base(base, parsed)
    by_candidate: dict[str, ParsedResume] = {}
    for cid, key in links.items():
        resume = parsed.get(key) or extra.get(key)
        if resume is not None:
            by_candidate[str(cid)] = resume
    return parsed, by_candidate


def config_from_saved(data: Any) -> AppConfig:
    """AppConfig from a saved run, read like an uploaded profile (AppConfig.from_json).

    Saved runs are data, not trusted settings: LLM connection fields (endpoint, key name, backend,
    cache folder) always come from the running code, so an edited run file cannot redirect the API
    key or candidate data.
    """
    return AppConfig.from_json(json.dumps(data if isinstance(data, dict) else {}, default=str))


def _quality_from(data: Any) -> DataQualityReport:
    if not isinstance(data, dict):
        return DataQualityReport()
    return DataQualityReport(**{k: v for k, v in data.items() if k in _QUALITY_FIELDS})


# ---------------------------------------------------------------------------
# Counts, diffs and config comparison
# ---------------------------------------------------------------------------
def run_counts(result: PipelineResult) -> dict[str, int]:
    """Headline numbers stored in meta.json and shown in the run list."""
    df = _frame_or_empty(getattr(result, "candidates", None))
    status, risk = _text(df, S.STATUS), _text(df, S.RISK_LEVEL)
    clusters = getattr(getattr(result, "risk", None), "clusters", None)
    return {
        "candidates": int(len(df)),
        "shortlisted": int(status.isin(IN_SHORTLIST).sum()),
        "in_review": int((status == S.STATUS_REVIEW).sum()),
        "high": int((risk == S.RISK_HIGH).sum()),
        "medium": int((risk == S.RISK_MEDIUM).sum()),
        "perfect_scorers": int((_number(df, S.HR_PCT) >= 99.995).sum()),
        "clusters": int(len(clusters)) if isinstance(clusters, pd.DataFrame) else 0,
        "resumes": int(len(getattr(result, "parsed_resumes", None) or {})),
    }


def _diff_view(df: Any) -> pd.DataFrame:
    """One row per candidate_id with the fields a shortlist comparison needs."""
    df = _frame_or_empty(df)
    capped = df[S.COLLEGE_CAPPED] if S.COLLEGE_CAPPED in df.columns else pd.Series(False, index=df.index)
    view = pd.DataFrame({
        S.CANDIDATE_ID: _text(df, S.CANDIDATE_ID), S.NAME: _text(df, S.NAME), S.COLLEGE: _text(df, S.COLLEGE),
        S.RANK: _number(df, S.RANK), S.STATUS: _text(df, S.STATUS), S.FINAL_SCORE: _number(df, S.FINAL_SCORE),
        S.STATUS_REASON: _text(df, S.STATUS_REASON),
        S.COLLEGE_CAPPED: capped.map(lambda v: str(v).strip().lower() in {"true", "1", "yes"}).astype(bool),
    }, index=df.index)
    return view[view[S.CANDIDATE_ID] != ""].drop_duplicates(S.CANDIDATE_ID).set_index(S.CANDIDATE_ID)


def _cutoff(view: pd.DataFrame) -> float | None:
    scores = view.loc[view[S.STATUS].isin(IN_SHORTLIST), S.FINAL_SCORE].dropna()
    return float(scores.min()) if len(scores) else None


def basic_shortlist_diff(before: pd.DataFrame, after: pd.DataFrame) -> dict[str, Any]:
    """Shortlist changes between two candidate tables, matched on candidate_id.

    Fallback with the same structure as analytics.shortlist_diff: {"entering", "leaving",
    "status_changes", "rank_moves": DataFrames with DIFF_COLUMNS, "summary": dict}.
    """
    b0, a0 = _diff_view(before), _diff_view(after)
    ids = b0.index.union(a0.index)
    b, a = b0.reindex(ids), a0.reindex(ids)
    status_b, status_a = b[S.STATUS].fillna("").astype(object), a[S.STATUS].fillna("").astype(object)
    short_b, short_a = status_b.isin(IN_SHORTLIST), status_a.isin(IN_SHORTLIST)
    name_a, college_a = a[S.NAME].fillna(""), a[S.COLLEGE].fillna("")
    table = pd.DataFrame({
        S.CANDIDATE_ID: ids.to_numpy(dtype=object),
        S.NAME: name_a.where(name_a != "", b[S.NAME]).fillna("").to_numpy(dtype=object),
        S.COLLEGE: college_a.where(college_a != "", b[S.COLLEGE]).fillna("").to_numpy(dtype=object),
        "rank_before": b[S.RANK].to_numpy(dtype="float64"), "rank_after": a[S.RANK].to_numpy(dtype="float64"),
        "rank_change": (b[S.RANK] - a[S.RANK]).to_numpy(dtype="float64"),
        "status_before": status_b.to_numpy(), "status_after": status_a.to_numpy(),
        "score_before": b[S.FINAL_SCORE].to_numpy(dtype="float64"),
        "score_after": a[S.FINAL_SCORE].to_numpy(dtype="float64"),
        "reason_after": a[S.STATUS_REASON].fillna("").to_numpy(dtype=object),
        "shortlisted_before": short_b.to_numpy(dtype=bool), "shortlisted_after": short_a.to_numpy(dtype=bool),
    })
    table = table.assign(_abs_move=table["rank_change"].abs())
    moved = table["rank_before"].notna() & table["rank_after"].notna() & (table["rank_change"] != 0)

    def part(mask: Any, by: list[str], ascending: list[bool] | bool = True) -> pd.DataFrame:
        rows = table[np.asarray(mask, dtype=bool)].sort_values(by, ascending=ascending, na_position="last",
                                                                kind="mergesort")
        return rows[DIFF_COLUMNS].reset_index(drop=True)

    status_changes = part(status_b.to_numpy() != status_a.to_numpy(), ["rank_after", "rank_before", S.CANDIDATE_ID])
    summary = {
        "shortlisted_before": int(short_b.sum()), "shortlisted_after": int(short_a.sum()),
        "entering": int((short_a & ~short_b).sum()), "leaving": int((short_b & ~short_a).sum()),
        "cutoff_before": _cutoff(b0), "cutoff_after": _cutoff(a0),
        "review_before": int((status_b == S.STATUS_REVIEW).sum()),
        "review_after": int((status_a == S.STATUS_REVIEW).sum()),
        "status_changes": len(status_changes),
        "capped_before": int(b0[S.COLLEGE_CAPPED].sum()), "capped_after": int(a0[S.COLLEGE_CAPPED].sum()),
    }
    return {
        "entering": part(short_a & ~short_b, ["rank_after", S.CANDIDATE_ID]),
        "leaving": part(short_b & ~short_a, ["rank_before", S.CANDIDATE_ID]),
        "status_changes": status_changes,
        "rank_moves": part(moved, ["_abs_move", "rank_after", S.CANDIDATE_ID], [False, True, True]),
        "summary": summary,
    }


def _analytics_diff() -> Callable[..., Any] | None:
    """analytics.shortlist_diff when that module is importable and implements it."""
    try:
        module = importlib.import_module(f"{__package__}.analytics")
        fn = getattr(module, "shortlist_diff")
    except Exception:  # noqa: BLE001 - module absent, incomplete or a stub
        return None
    return fn if callable(fn) else None


def _valid_diff(diff: Any) -> bool:
    return (isinstance(diff, dict) and isinstance(diff.get("summary"), dict)
            and all(isinstance(diff.get(k), pd.DataFrame) for k in ("entering", "leaving")))


def shortlist_diff(before: pd.DataFrame, after: pd.DataFrame) -> dict[str, Any]:
    """analytics.shortlist_diff when available, else basic_shortlist_diff (same structure)."""
    fn = _analytics_diff()
    if fn is not None:
        try:
            diff = fn(before, after)
        except Exception as exc:  # noqa: BLE001 - never let a comparison helper break the history view
            log.warning("analytics.shortlist_diff failed (%s); using the basic diff", type(exc).__name__)
        else:
            if _valid_diff(diff):
                return diff
    return basic_shortlist_diff(before, after)


def flatten_config(data: Any, prefix: str = "") -> dict[str, Any]:
    """{"shortlist": {"top_n": 300}} -> {"shortlist.top_n": 300} (lists stay as values)."""
    out: dict[str, Any] = {}
    if not isinstance(data, dict):
        return out
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict) and value:
            out.update(flatten_config(value, path))
        else:
            out[path] = value
    return out


def config_changes(before: dict[str, Any], after: dict[str, Any]) -> list[tuple[str, Any, Any]]:
    """(setting, before, after) for every flattened setting that differs (None = not set)."""
    a, b = flatten_config(before), flatten_config(after)
    keys = list(a) + [k for k in b if k not in a]
    return [(k, a.get(k), b.get(k)) for k in keys if k not in a or k not in b or a[k] != b[k]]


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------
def _created_at(meta: dict[str, Any] | None, run_id: str) -> datetime | None:
    """created_at as naive local time (falls back to the timestamp inside the run id)."""
    try:
        moment = datetime.fromisoformat(str((meta or {}).get("created_at")))
        return moment.astimezone().replace(tzinfo=None) if moment.tzinfo else moment
    except (TypeError, ValueError, OverflowError, OSError):
        try:
            return datetime.strptime(run_id[:15], "%Y%m%d-%H%M%S")
        except ValueError:
            return None


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return int(value)


def _folder_mb(folder: Path) -> float:
    total = 0
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                if entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
    except OSError:
        return float("nan")
    return round(total / (1024 * 1024), 3)


class RunStore:
    """Saved screening runs under ``base_dir`` (env SCREENING_RUNS_DIR, else <project>/runs).

    The folder is created lazily on the first save; listing an absent folder returns no runs.
    """

    def __init__(self, base_dir: str | Path | None = None) -> None:
        configured = base_dir if base_dir not in (None, "") else (os.environ.get(ENV_RUNS_DIR) or "").strip()
        self.base_dir = Path(configured or DEFAULT_RUNS_DIR).expanduser().resolve()

    # ---- paths ----------------------------------------------------------
    def _run_dir(self, run_id: object) -> Path:
        """Folder of a run id; rejects anything but YYYYMMDD-HHMMSS-<6 hex> inside base_dir."""
        if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
            raise RunStoreError("Invalid run id.")
        path = (self.base_dir / run_id).resolve()
        if path.parent != self.base_dir or path.name != run_id:
            raise RunStoreError("Invalid run id.")
        return path

    def _existing(self, run_id: object) -> Path:
        path = self._run_dir(run_id)
        if not path.is_dir():
            raise RunStoreError(f"Saved run {run_id} was not found.")
        return path

    def _read_meta(self, folder: Path) -> dict[str, Any]:
        try:
            meta = _read_json(folder / "meta.json")
        except FileNotFoundError as exc:
            raise RunStoreError(f"Saved run {folder.name} is incomplete (meta.json is missing).") from exc
        except (OSError, ValueError) as exc:
            raise RunStoreError(f"Saved run {folder.name} could not be read (meta.json is damaged).") from exc
        if not isinstance(meta, dict):
            raise RunStoreError(f"Saved run {folder.name} could not be read (meta.json is damaged).")
        return meta

    def _new_run_id(self, now: datetime) -> str:
        for _ in range(20):
            run_id = f"{now:%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
            if not (self.base_dir / run_id).exists():
                return run_id
        raise RunStoreError("Could not allocate a new run id; try again.")

    def _housekeeping(self) -> None:
        """Remove leftovers of interrupted saves/deletes (best effort)."""
        try:
            entries = list(os.scandir(self.base_dir))
        except OSError:
            return
        now = time.time()
        for entry in entries:
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                stale_tmp = (entry.name.startswith(_TMP_PREFIX)
                             and now - entry.stat(follow_symlinks=False).st_mtime > _STALE_TMP_SECONDS)
                if stale_tmp or entry.name.startswith(_TRASH_PREFIX):
                    _remove_tree(Path(entry.path))
            except OSError:
                continue

    # ---- save -----------------------------------------------------------
    def save(self, prepared: PreparedData, result: PipelineResult, label: str,
             llm_state: dict[str, Any] | None = None, dataset_label: str = "") -> str:
        """Persist a screening run; returns its run id ("YYYYMMDD-HHMMSS-<6 hex>").

        ``result`` must come from ``pipeline.evaluate(prepared, ...)``: parsed resumes, matches and
        the data-quality report are stored once and shared by both objects on load. ``llm_state``
        holds the session's LLM outputs, e.g. {"rescore": {...}, "explain": {...}, "clusters": {...},
        "executive": {...} | None}; it is returned unchanged by load().
        """
        if prepared is None or not isinstance(getattr(prepared, "base", None), pd.DataFrame):
            raise RunStoreError("Nothing to save: this run has no prepared data.")
        if result is None or not isinstance(getattr(result, "candidates", None), pd.DataFrame):
            raise RunStoreError("Nothing to save: this run has no results.")
        name = clean_label(label)
        now = datetime.now().astimezone()
        try:
            self.base_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RunStoreError(f"The runs folder could not be created ({_os_reason(exc)}).") from exc
        self._housekeeping()
        run_id = self._new_run_id(now)
        work = self.base_dir / f"{_TMP_PREFIX}{run_id}-{secrets.token_hex(4)}"
        try:
            work.mkdir()
            manifest = self._write_tables(work, prepared, result)
            _write_json(work / "risk_meta.json", self._risk_meta(result.risk, manifest))
            _write_json(work / "quality.json", self._quality_dict(prepared.quality))
            parsed = _parsed_payload(prepared.parsed, prepared.parsed_by_candidate)
            (work / "parsed.json.gz").write_bytes(gzip.compress(_json_bytes(parsed, compact=True), compresslevel=6))
            _write_json(work / "llm.json", {"state": dict(llm_state or {}), "applied": dict(result.llm_results or {})})
            cfg = result.config if isinstance(result.config, AppConfig) else AppConfig()
            _write_json(work / "meta.json", {
                "format_version": FORMAT_VERSION, "run_id": run_id, "label": name,
                "created_at": now.isoformat(timespec="microseconds"), "dataset": str(dataset_label or ""),
                "mode": str(result.mode), "prepared_mode": str(getattr(prepared, "mode", result.mode)),
                "app_version": __version__, "counts": run_counts(result), "config": cfg.to_dict(),
                "timings": {"prepared": dict(getattr(prepared, "timings", None) or {}),
                            "result": dict(result.timings or {})},
                "frames": manifest,
            })
            final = self.base_dir / run_id
            if final.exists():
                raise RunStoreError("A run with the same id already exists; try again.")
            _retry(os.replace, work, final)
        except RunStoreError:
            _remove_tree(work)
            raise
        except OSError as exc:
            _remove_tree(work)
            raise RunStoreError(f"The run could not be saved ({_os_reason(exc)}).") from exc
        except Exception as exc:  # noqa: BLE001 - report without data values (they may hold PII)
            _remove_tree(work)
            log.warning("Saving a run failed (%s)", type(exc).__name__)
            raise RunStoreError(f"The run could not be saved ({type(exc).__name__}).") from exc
        return run_id

    @staticmethod
    def _write_tables(folder: Path, prepared: PreparedData, result: PipelineResult) -> dict[str, bool]:
        tables = {"candidates": result.candidates, "base": prepared.base, "matches": prepared.matches,
                  "ta": prepared.ta_df}
        risk = result.risk if result.risk is not None else RiskResult()
        tables.update({f"risk_{name}": getattr(risk, name, None) for name in RISK_FRAMES})
        manifest: dict[str, bool] = {}
        for name, frame in tables.items():
            manifest[name] = isinstance(frame, pd.DataFrame)
            if manifest[name]:
                write_frame(frame, folder / f"{name}.parquet")
        return manifest

    @staticmethod
    def _risk_meta(risk: RiskResult | None, manifest: dict[str, bool]) -> dict[str, Any]:
        risk = risk if risk is not None else RiskResult()
        return {"focus_ids": list(risk.focus_ids or []), "params": dict(risk.params or {}),
                "warnings": list(risk.warnings or []),
                "frames": {name: manifest.get(f"risk_{name}", False) for name in RISK_FRAMES}}

    @staticmethod
    def _quality_dict(quality: DataQualityReport | None) -> dict[str, Any]:
        if quality is None or not is_dataclass(quality):
            return {}
        return {f.name: getattr(quality, f.name) for f in fields(quality)}

    # ---- read -----------------------------------------------------------
    def list_runs(self) -> pd.DataFrame:
        """One row per saved run, newest first (columns: LIST_COLUMNS)."""
        rows: list[dict[str, Any]] = []
        try:
            entries = sorted(os.scandir(self.base_dir), key=lambda e: e.name) if self.base_dir.is_dir() else []
        except OSError as exc:
            raise RunStoreError(f"The runs folder could not be read ({_os_reason(exc)}).") from exc
        for entry in entries:
            if not RUN_ID_RE.match(entry.name) or not entry.is_dir(follow_symlinks=False):
                continue
            folder = Path(entry.path)
            try:
                meta: dict[str, Any] | None = self._read_meta(folder)
            except RunStoreError:
                meta = None
            counts = (meta or {}).get("counts")
            counts = counts if isinstance(counts, dict) else {}
            row: dict[str, Any] = {
                "run_id": entry.name,
                "label": clean_label((meta or {}).get("label"), entry.name) if meta else "(unreadable run)",
                "created_at": _created_at(meta, entry.name),
                "dataset": str((meta or {}).get("dataset") or ""),
                "mode": str((meta or {}).get("mode") or ""),
                "size_mb": _folder_mb(folder),
            }
            row.update({key: _count(counts.get(key)) for key in COUNT_KEYS})
            rows.append(row)
        frame = pd.DataFrame(rows, columns=LIST_COLUMNS)
        frame["created_at"] = pd.to_datetime(frame["created_at"], errors="coerce")
        for key in COUNT_KEYS:
            frame[key] = frame[key].astype("Int64")
        frame["size_mb"] = pd.to_numeric(frame["size_mb"], errors="coerce").astype(float)
        frame = frame.sort_values(["created_at", "run_id"], ascending=[False, False], na_position="last",
                                  kind="stable")
        return frame.reset_index(drop=True)

    def read_meta(self, run_id: str) -> dict[str, Any]:
        """meta.json of a saved run."""
        return self._read_meta(self._existing(run_id))

    @staticmethod
    def _optional_frame(path: Path, stored: Any) -> pd.DataFrame | None:
        if stored is False:
            return None
        if stored is None and not path.exists():  # manifest silent and no file: treat as absent
            return None
        return read_frame(path)

    def load(self, run_id: str) -> tuple[PreparedData, PipelineResult, dict[str, Any], dict[str, Any]]:
        """(PreparedData, PipelineResult, llm_state, meta) exactly as saved.

        pipeline.evaluate(prepared, result.config) works on the loaded data, so a saved run can be
        re-tuned without the original files.
        """
        folder = self._existing(run_id)
        meta = self._read_meta(folder)
        try:
            version = int(meta.get("format_version", 1))
        except (TypeError, ValueError):
            version = FORMAT_VERSION + 1
        if version > FORMAT_VERSION:
            raise RunStoreError(f"Saved run {run_id} was written by a newer version of the app and cannot be "
                                "opened here.")
        try:
            missing = [name for name in _REQUIRED_FILES if not (folder / name).is_file()]
            if missing:
                raise RunStoreError(f"Saved run {run_id} is incomplete ({', '.join(missing)} missing).")
            frames = meta.get("frames") if isinstance(meta.get("frames"), dict) else {}
            cfg = config_from_saved(meta.get("config"))
            candidates = read_frame(folder / "candidates.parquet")
            base = read_frame(folder / "base.parquet")
            matches = self._optional_frame(folder / "matches.parquet", frames.get("matches"))
            ta_df = self._optional_frame(folder / "ta.parquet", frames.get("ta"))
            risk_meta = _read_json(folder / "risk_meta.json")
            risk_meta = risk_meta if isinstance(risk_meta, dict) else {}
            stored = risk_meta.get("frames") if isinstance(risk_meta.get("frames"), dict) else {}
            risk = RiskResult(
                focus_ids=list(risk_meta.get("focus_ids") or []),
                params=dict(risk_meta.get("params") or {}),
                warnings=list(risk_meta.get("warnings") or []),
                **{name: self._optional_frame(folder / f"risk_{name}.parquet", stored.get(name))
                   for name in RISK_FRAMES},
            )
            quality = _quality_from(_read_json(folder / "quality.json"))
            parsed, by_candidate = _read_parsed(folder / "parsed.json.gz", base)
            llm_path = folder / "llm.json"
            llm_doc = _read_json(llm_path) if llm_path.exists() else {}
            llm_doc = llm_doc if isinstance(llm_doc, dict) else {}
            timings = meta.get("timings") if isinstance(meta.get("timings"), dict) else {}
        except RunStoreError:
            raise
        except FileNotFoundError as exc:
            raise RunStoreError(f"Saved run {run_id} is incomplete (a file is missing).") from exc
        except Exception as exc:  # noqa: BLE001 - damaged files: report without data values
            log.warning("Reading a saved run failed (%s)", type(exc).__name__)
            raise RunStoreError(f"Saved run {run_id} could not be read ({type(exc).__name__}).") from exc
        mode = str(meta.get("mode") or "ta+resumes")
        prepared = PreparedData(ta_df=ta_df, quality=quality, parsed=parsed, matches=matches, base=base,
                                parsed_by_candidate=by_candidate, mode=str(meta.get("prepared_mode") or mode),
                                timings=dict(timings.get("prepared") or {}))
        result = PipelineResult(candidates=candidates, parsed_resumes=parsed, parsed_by_candidate=by_candidate,
                                matches=matches, risk=risk, quality=quality, config=cfg,
                                llm_results=dict(llm_doc.get("applied") or {}),
                                timings=dict(timings.get("result") or {}), mode=mode)
        llm_state = llm_doc.get("state") if isinstance(llm_doc.get("state"), dict) else {}
        return prepared, result, dict(llm_state), meta

    # ---- edit -----------------------------------------------------------
    def rename(self, run_id: str, label: str) -> str:
        """Change a run's label; returns the cleaned label."""
        folder = self._existing(run_id)
        meta = self._read_meta(folder)
        meta["label"] = clean_label(label)
        meta["renamed_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        work = folder / f"meta.json.{secrets.token_hex(4)}.tmp"
        try:
            _write_json(work, meta)
            _retry(os.replace, work, folder / "meta.json")
        except OSError as exc:
            work.unlink(missing_ok=True)
            raise RunStoreError(f"Saved run {run_id} could not be renamed ({_os_reason(exc)}).") from exc
        return str(meta["label"])

    def delete(self, run_id: str) -> None:
        """Permanently delete a saved run (moved aside first, so it vanishes from the list at once)."""
        folder = self._existing(run_id)
        trash = self.base_dir / f"{_TRASH_PREFIX}{run_id}-{secrets.token_hex(4)}"
        try:
            _retry(os.replace, folder, trash)
        except OSError as exc:
            raise RunStoreError(f"Saved run {run_id} could not be deleted ({_os_reason(exc)}).") from exc
        _remove_tree(trash)
        self._housekeeping()

    # ---- compare --------------------------------------------------------
    def compare_runs(self, run_a: str, run_b: str) -> dict[str, Any]:
        """{"diff": shortlist diff A -> B, "config_changes": [(setting, A, B)], "meta": (meta_a, meta_b)}."""
        folder_a, folder_b = self._existing(run_a), self._existing(run_b)
        meta_a, meta_b = self._read_meta(folder_a), self._read_meta(folder_b)
        try:
            before = read_frame(folder_a / "candidates.parquet")
            after = read_frame(folder_b / "candidates.parquet")
        except FileNotFoundError as exc:
            raise RunStoreError("A saved run is incomplete (its candidate table is missing).") from exc
        except Exception as exc:  # noqa: BLE001
            log.warning("Reading saved runs for comparison failed (%s)", type(exc).__name__)
            raise RunStoreError(f"The saved runs could not be read ({type(exc).__name__}).") from exc
        cfg_a = meta_a.get("config") if isinstance(meta_a.get("config"), dict) else {}
        cfg_b = meta_b.get("config") if isinstance(meta_b.get("config"), dict) else {}
        return {"diff": shortlist_diff(before, after), "config_changes": config_changes(cfg_a, cfg_b),
                "meta": (meta_a, meta_b)}
