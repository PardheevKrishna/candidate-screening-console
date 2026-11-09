"""Fast reload: parsed datasets cached on disk, so a browser refresh does not re-parse.

pipeline.prepare_data (TA ingest, resume parsing and packet splitting, matching, duplicate
detection) is the slow stage - minutes for a few thousand resumes. Its output is cached per input
fingerprint (a lowercase hex digest that must cover everything prepare_data depends on - the UI
hashes the uploaded files, the column mapping and the processing settings), so a refresh, a new
session or a colleague opening the same files reloads it in well under a second:

    <cache_dir>/<fingerprint>/
        manifest.json     format, fingerprint, label, created_at, app version, engine signature,
                          mode, counts, original timings and the stored tables
        base.parquet      PreparedData.base
        matches.parquet   PreparedData.matches
        ta.parquet        PreparedData.ta_df (absent for resume-only datasets)
        quality.json      DataQualityReport
        parsed.json.gz    every ParsedResume plus the candidate -> resume links

Only the prepared data is cached (no scores, ranks or LLM output: evaluate() recomputes those
from the current configuration). The files are written by runs.write_prepared / read by
runs.read_prepared - JSON, Parquet and gzipped JSON only, never pickle, so reading a snapshot
cannot execute code. A snapshot is written into a temporary sibling folder and moved into place
with one rename. Fingerprints are validated (lowercase hex only), so they can never point
outside the cache folder.

Snapshots hold candidate personal data: the cache lives under data/ (gitignored) and every
snapshot is deleted ``ttl_days`` after it was written (purge_expired runs on every list and
load). A snapshot written by different preparation code (app version or engine source changed)
is a cache miss and is deleted, so a parser fix is never hidden behind a stale cache.
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import secrets
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from . import __version__, runs
from .config import AppConfig, SnapshotConfig
from .pipeline import PreparedData

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_FORMAT = 1
MANIFEST = "manifest.json"
FINGERPRINT_RE = re.compile(r"\A[0-9a-f]{16,128}\Z")
LIST_COLUMNS = ["fingerprint", "label", "created_at", "candidates", "resumes", "size_mb", "expires_at"]
DEFAULT_LABEL = "Cached dataset"
COMPRESS_LEVEL = 1          # snapshots are written while the user waits: favour speed over size
MAX_TTL_DAYS = 36500.0

_KIND = "screening-snapshot"
_TMP_PREFIX = runs._TMP_PREFIX
_TRASH_PREFIX = runs._TRASH_PREFIX
_STALE_TMP_SECONDS = 3600
_PACKAGE_DIR = Path(__file__).resolve().parent
# Modules that never shape PreparedData (scoring, risk, LLM text, exports, analytics). Every other module
# of the package - including modules added later - is part of the engine signature, so changing any of
# them invalidates existing snapshots.
_NON_ENGINE_MODULES = frozenset({"analytics", "export", "llm", "llm_backends", "risk", "scoring"})
_signature_cache: dict[tuple[Any, ...], str] = {}


class SnapshotError(runs.RunStoreError):
    """A snapshot operation was refused or failed. Messages are safe to show (no file paths)."""


# ---------------------------------------------------------------------------
# Settings, paths and validation
# ---------------------------------------------------------------------------
def _settings(cfg: Any) -> SnapshotConfig:
    """SnapshotConfig from an AppConfig, a SnapshotConfig or None (defaults)."""
    if isinstance(cfg, SnapshotConfig):
        return cfg
    nested = getattr(cfg, "snapshots", None)
    return nested if isinstance(nested, SnapshotConfig) else SnapshotConfig()


def cache_root(cfg: AppConfig | SnapshotConfig | None = None) -> Path:
    """Absolute snapshot folder: ``cache_dir`` (relative paths are taken from the project root)."""
    raw = str(_settings(cfg).cache_dir or "").strip() or SnapshotConfig().cache_dir
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def _ttl(settings: SnapshotConfig) -> timedelta:
    try:
        days = float(settings.ttl_days)
    except (TypeError, ValueError):
        days = float(SnapshotConfig().ttl_days)
    if not math.isfinite(days):
        days = float(SnapshotConfig().ttl_days)
    return timedelta(days=min(max(days, 0.0), MAX_TTL_DAYS))


def _check(fingerprint: object) -> str:
    if not isinstance(fingerprint, str) or not FINGERPRINT_RE.match(fingerprint):
        raise SnapshotError("Invalid dataset fingerprint.")
    return fingerprint


def _folder(root: Path, fingerprint: str) -> Path:
    """Snapshot folder of a validated fingerprint, confined to ``root``."""
    path = (root / fingerprint).resolve()
    if path.parent != root or path.name != fingerprint:
        raise SnapshotError("Invalid dataset fingerprint.")
    return path


def engine_signature() -> str:
    """Short hash of the app version and the source of every module that can shape PreparedData.

    Re-computed whenever one of those files changes on disk (a code update is noticed without a
    restart); unreadable sources fall back to the app version alone.
    """
    try:
        sources = sorted(p for p in _PACKAGE_DIR.glob("*.py") if p.stem not in _NON_ENGINE_MODULES)
    except OSError:
        sources = []
    stats: list[tuple[str, int, int]] = []
    for path in sources:
        try:
            info = path.stat()
            stats.append((path.name, info.st_mtime_ns, info.st_size))
        except OSError:
            stats.append((path.name, 0, -1))
    key = (__version__, SNAPSHOT_FORMAT, tuple(stats))
    cached = _signature_cache.get(key)
    if cached is not None:
        return cached
    digest = hashlib.sha256(f"{__version__}|{SNAPSHOT_FORMAT}".encode())
    for path in sources:
        digest.update(f"|{path.name}|".encode())
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<unavailable>")
    signature = digest.hexdigest()[:20]
    _signature_cache.clear()
    _signature_cache[key] = signature
    return signature


def _reason(exc: BaseException) -> str:
    return runs._os_reason(exc) if isinstance(exc, OSError) else type(exc).__name__


# ---------------------------------------------------------------------------
# Manifest helpers
# ---------------------------------------------------------------------------
def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.astimezone()


def _local_naive(moment: datetime | None) -> datetime | None:
    return None if moment is None else moment.astimezone().replace(tzinfo=None)


def _created(manifest: dict[str, Any]) -> datetime | None:
    try:
        return _aware(datetime.fromisoformat(str(manifest.get("created_at"))))
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _read_manifest(folder: Path) -> dict[str, Any]:
    data = runs.read_json(folder / MANIFEST)
    if not isinstance(data, dict) or data.get("kind") != _KIND:
        raise ValueError("not a snapshot manifest")
    return data


def _problem(manifest: dict[str, Any], fingerprint: str) -> str:
    """Why a snapshot cannot be used ('' when it can)."""
    if manifest.get("format") != SNAPSHOT_FORMAT:
        return "format"
    if manifest.get("fingerprint") != fingerprint:
        return "fingerprint"
    if manifest.get("engine") != engine_signature():
        return "engine"
    if _created(manifest) is None:
        return "created_at"
    return ""


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return int(value)


def _row(fingerprint: str, manifest: dict[str, Any], folder: Path, ttl: timedelta) -> dict[str, Any]:
    created = _created(manifest)
    counts = manifest.get("counts") if isinstance(manifest.get("counts"), dict) else {}
    return {
        "fingerprint": fingerprint,
        "label": runs.clean_label(manifest.get("label"), DEFAULT_LABEL),
        "created_at": _local_naive(created),
        "candidates": _count(counts.get("candidates")),
        "resumes": _count(counts.get("resumes")),
        "size_mb": runs.folder_size_mb(folder),
        "expires_at": _local_naive(created + ttl) if created is not None else None,
    }


def _discard(root: Path, folder: Path) -> bool:
    """Move a snapshot aside, then delete it (best effort). True when it is gone."""
    trash = root / f"{_TRASH_PREFIX}{folder.name}-{secrets.token_hex(4)}"
    try:
        runs.move_path(folder, trash)
    except FileNotFoundError:
        return True
    except OSError as exc:
        log.warning("A cached dataset could not be removed (%s)", _reason(exc))
        return False
    runs.remove_tree(trash)
    return True


def _install(work: Path, final: Path, root: Path) -> None:
    """Move a finished work folder into place, replacing an older snapshot of the same data."""
    if not final.exists():
        runs.move_path(work, final)
        return
    trash = root / f"{_TRASH_PREFIX}{final.name}-{secrets.token_hex(4)}"
    runs.move_path(final, trash)
    try:
        runs.move_path(work, final)
    except OSError:
        try:
            runs.move_path(trash, final)  # put the previous snapshot back
        except OSError:
            pass
        raise
    runs.remove_tree(trash)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def save_snapshot(prepared: PreparedData, fingerprint: str, label: str = "",
                  cfg: AppConfig | SnapshotConfig | None = None) -> dict[str, Any] | None:
    """Cache ``prepared`` under ``<cache_dir>/<fingerprint>/`` (atomic; replaces an older copy).

    Returns the snapshot's list row (LIST_COLUMNS keys), or None when snapshots are disabled,
    ``ttl_days`` is 0 or the write failed (a failed write is logged, leaves nothing behind and
    never breaks the screening). Raises SnapshotError for an invalid fingerprint or missing data.
    """
    fp = _check(fingerprint)
    if prepared is None or not isinstance(getattr(prepared, "base", None), pd.DataFrame):
        raise SnapshotError("Nothing to cache: there is no prepared data.")
    settings = _settings(cfg)
    ttl = _ttl(settings)
    if not settings.enabled or ttl <= timedelta(0):
        return None
    root = cache_root(settings)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("The snapshot folder could not be created (%s)", _reason(exc))
        return None
    final = _folder(root, fp)
    runs.sweep_work_folders(root, _STALE_TMP_SECONDS)
    now = datetime.now().astimezone()
    manifest: dict[str, Any] = {
        "kind": _KIND, "format": SNAPSHOT_FORMAT, "fingerprint": fp,
        "label": runs.clean_label(label, DEFAULT_LABEL), "created_at": now.isoformat(timespec="microseconds"),
        "app_version": __version__, "engine": engine_signature(), "mode": str(getattr(prepared, "mode", "")),
        "counts": {"candidates": int(len(prepared.base)), "resumes": int(len(prepared.parsed or {}))},
        "timings": runs._mapping(getattr(prepared, "timings", None)),
    }
    work = root / f"{_TMP_PREFIX}{fp}-{secrets.token_hex(4)}"
    try:
        work.mkdir()
        manifest["frames"] = runs.write_prepared(work, prepared, compresslevel=COMPRESS_LEVEL)
        runs.write_json(work / MANIFEST, manifest)
        _install(work, final, root)
    except Exception as exc:  # noqa: BLE001 - a cache write must never break the screening
        runs.remove_tree(work)
        log.warning("Caching the parsed dataset failed (%s)", _reason(exc))
        return None
    return _row(fp, manifest, final, ttl)


def load_snapshot(fingerprint: str, cfg: AppConfig | SnapshotConfig | None = None) -> PreparedData | None:
    """PreparedData exactly as cached, or None (disabled, not cached, expired, stale or damaged).

    Expired snapshots are purged first. Stale (other app/engine version) and damaged snapshots are
    deleted so the next prepare_data run caches a fresh copy. ``timings`` of the returned data is
    {"snapshot_load": seconds}. Raises SnapshotError only for an invalid fingerprint.
    """
    fp = _check(fingerprint)
    settings = _settings(cfg)
    if not settings.enabled:
        return None
    started = time.perf_counter()
    purge_expired(settings)
    root = cache_root(settings)
    folder = _folder(root, fp)
    if not folder.is_dir():
        return None
    manifest: dict[str, Any] | None = None
    try:
        manifest = _read_manifest(folder)
        problem = _problem(manifest, fp)
        if problem:
            log.info("A cached dataset is out of date (%s); it will be rebuilt", problem)
            _discard(root, folder)
            return None
        prepared = runs.read_prepared(folder, manifest.get("frames"), mode=str(manifest.get("mode") or ""))
    except Exception as exc:  # noqa: BLE001 - a broken cache entry is a miss, never an error
        if not isinstance(exc, _TRANSIENT_ERRORS) and _unchanged(folder, manifest):
            log.warning("A cached dataset could not be read (%s); it will be rebuilt", _reason(exc))
            _discard(root, folder)
        else:
            log.info("A cached dataset is unavailable right now (%s)", _reason(exc))
        return None
    prepared.timings = {"snapshot_load": round(time.perf_counter() - started, 4)}
    return prepared


# Errors that say "try again later" rather than "this snapshot is damaged".
_TRANSIENT_ERRORS = (PermissionError, TimeoutError, BlockingIOError, InterruptedError)


def _unchanged(folder: Path, manifest: dict[str, Any] | None) -> bool:
    """True when ``folder`` still holds the snapshot a failed read started on (not replaced meanwhile)."""
    if not folder.is_dir():
        return False
    if manifest is None:
        return True
    try:
        current = _read_manifest(folder)
    except (OSError, ValueError):
        return True
    return current.get("created_at") == manifest.get("created_at")


def list_snapshots(cfg: AppConfig | SnapshotConfig | None = None) -> pd.DataFrame:
    """Cached datasets, newest first (columns LIST_COLUMNS; times are naive local time).

    Expired, stale and damaged snapshots are purged first. Never raises.
    """
    settings = _settings(cfg)
    purge_expired(settings)
    root = cache_root(settings)
    ttl = _ttl(settings)
    rows: list[dict[str, Any]] = []
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name) if root.is_dir() else []
    except OSError:
        entries = []
    for entry in entries:
        try:
            if not FINGERPRINT_RE.match(entry.name) or not entry.is_dir(follow_symlinks=False):
                continue
            folder = Path(entry.path)
            manifest = _read_manifest(folder)
        except (OSError, ValueError):
            continue
        if not _problem(manifest, entry.name):
            rows.append(_row(entry.name, manifest, folder, ttl))
    frame = pd.DataFrame(rows, columns=LIST_COLUMNS)
    for col in ("created_at", "expires_at"):
        frame[col] = pd.to_datetime(frame[col], errors="coerce")
    for col in ("candidates", "resumes"):
        frame[col] = frame[col].astype("Int64")
    frame["size_mb"] = pd.to_numeric(frame["size_mb"], errors="coerce").astype(float)
    frame = frame.sort_values(["created_at", "fingerprint"], ascending=[False, True], na_position="last",
                              kind="stable")
    return frame.reset_index(drop=True)


def delete_snapshot(fingerprint: str, cfg: AppConfig | SnapshotConfig | None = None) -> bool:
    """Delete one cached dataset. True when deleted, False when there was none.

    Raises SnapshotError for an invalid fingerprint or when the files are in use.
    """
    fp = _check(fingerprint)
    root = cache_root(cfg)
    folder = _folder(root, fp)
    if not folder.is_dir():
        return False
    trash = root / f"{_TRASH_PREFIX}{fp}-{secrets.token_hex(4)}"
    try:
        runs.move_path(folder, trash)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise SnapshotError(f"The cached dataset could not be deleted ({_reason(exc)}).") from exc
    runs.remove_tree(trash)
    return True


def purge_expired(cfg: AppConfig | SnapshotConfig | None = None, *, now: datetime | None = None) -> int:
    """Delete snapshots older than ``ttl_days`` (plus stale or damaged ones and leftovers of
    interrupted writes). Returns the number of snapshots deleted. Never raises."""
    settings = _settings(cfg)
    root = cache_root(settings)
    try:
        entries = list(os.scandir(root))
    except OSError:
        return 0
    moment = _aware(now) if isinstance(now, datetime) else datetime.now().astimezone()
    ttl = _ttl(settings)
    removed = 0
    for entry in entries:
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
            if entry.name.startswith(_TRASH_PREFIX):
                runs.remove_tree(Path(entry.path))
                continue
            if entry.name.startswith(_TMP_PREFIX):
                if time.time() - entry.stat(follow_symlinks=False).st_mtime > _STALE_TMP_SECONDS:
                    runs.remove_tree(Path(entry.path))
                continue
            if not FINGERPRINT_RE.match(entry.name):
                continue
            folder = Path(entry.path)
            try:
                manifest = _read_manifest(folder)
            except FileNotFoundError:
                if not folder.is_dir():
                    continue  # replaced or deleted meanwhile
                expired = True  # a snapshot folder without a manifest is unusable
            except ValueError:
                expired = True  # damaged manifest
            else:
                created = _created(manifest)
                expired = (bool(_problem(manifest, entry.name)) or created is None
                           or moment >= created + ttl)
            if expired and _discard(root, folder):
                removed += 1
        except OSError:
            continue  # e.g. a file held open by another program; try again next time
    if removed:
        log.info("Removed %d cached dataset(s) past retention or out of date", removed)
    return removed
