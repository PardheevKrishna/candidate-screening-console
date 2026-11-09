"""Run History tab: save the current screening, reopen, rename, delete and compare saved runs.

Saved runs live on the server (screening/runs.py, JSON + Parquet only). Opening one restores the
prepared data, results, configuration and LLM outputs (executive brief, candidate explanations,
borderline explanations, cluster summaries), so it can be reviewed or re-tuned without the
original files. LLM outputs are text only: they never changed scores, ranks or the shortlist. The
tab also renders before the first screening, so a saved run can be opened directly.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from screening import runs
from screening import schema as S
from screening.config import AppConfig
from screening.models import PipelineResult
from screening.pipeline import PreparedData
from ui import state, theme

log = logging.getLogger(__name__)

K_LABEL = "hist_label"               # label of the run to save (text input)
K_LABEL_FOR = "_hist_label_for"      # dataset/run the default label was made for
K_PICK = "hist_pick"                 # saved run selected for load / rename / delete
K_TABLE = "hist_table"               # saved-runs table (row selection)
K_TABLE_IDS = "_hist_table_ids"      # run ids in table order (for the selection callback)
K_CMP_A = "hist_cmp_a"
K_CMP_B = "hist_cmp_b"
K_CMP_CACHE = "_hist_cmp_cache"      # (key, comparison) of the last comparison shown
K_ACTIVE = "_hist_active"            # saved run on screen: {"run_id", "label", "fp", "sig", "dataset"}
K_LLM_INFO = "_hist_llm_info"        # run_id -> ((mtime_ns, size) of llm.json, summary text)
SAVED_PREFIX = "Saved run: "
LLM_FILE = "llm.json"                # written by RunStore.save next to meta.json
LLM_COLUMN = "llm"                   # saved-runs table: what LLM text was saved with the run
NO_LLM = "None"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
RETENTION_NOTE = ("Saved runs contain candidate personal data (names, contact details, resume text and integrity "
                  "evidence); your data-retention policy applies. Delete runs you no longer need.")
SECTION_LABELS = {"weights": "Rubric weights", "skills": "Skills", "experience": "Experience",
                  "education": "Education", "shortlist": "Shortlist", "risk": "Integrity risk", "llm": "LLM",
                  "processing": "Processing", "snapshots": "Fast reload"}
TIER_LIST_SETTING = "education.tier_mapping"   # one setting per college name once flattened
DIFF_ORDER = {
    "entering": [S.CANDIDATE_ID, S.NAME, S.COLLEGE, "score_after", "rank_after", "status_after", "status_before",
                 "rank_before"],
    "leaving": [S.CANDIDATE_ID, S.NAME, S.COLLEGE, "score_before", "rank_before", "status_before", "status_after",
                "reason_after"],
    "status_changes": [S.CANDIDATE_ID, S.NAME, S.COLLEGE, "status_before", "status_after", "rank_before",
                       "rank_after", "score_before", "score_after"],
    "rank_moves": [S.CANDIDATE_ID, S.NAME, "rank_before", "rank_after", "rank_change", "score_before", "score_after",
                   "status_after"],
}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def storage_location(base_dir: Path) -> str:
    """Where saved runs live, without exposing server paths (which can contain account names).

    Inside the app folder: the relative folder (e.g. 'runs'); elsewhere: only the folder's name.
    """
    try:
        return base_dir.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix() or "."
    except (ValueError, OSError):
        return base_dir.name or "runs"


def storage_note(base_dir: Path) -> str:
    """One sentence for the UI saying where runs are stored."""
    name = storage_location(base_dir).replace("`", "")
    try:
        base_dir.resolve().relative_to(PROJECT_ROOT.resolve())
    except (ValueError, OSError):
        return f"Runs are stored on the server in the `{name}` folder set by `{runs.ENV_RUNS_DIR}`."
    return f"Runs are stored on the server in the app's `{name}` folder."


def run_labels(saved: pd.DataFrame) -> dict[str, str]:
    """run_id -> 'label · 5 Nov 2026 14:32' for pickers."""
    out: dict[str, str] = {}
    for run_id, label, created in zip(saved["run_id"], saved["label"], saved["created_at"]):
        dated = isinstance(created, datetime) and pd.notna(created)
        out[str(run_id)] = f"{label} · {created:%d %b %Y %H:%M}" if dated else str(label)
    return out


def runs_view(saved: pd.DataFrame, active_id: str = "", llm_info: dict[str, str] | None = None) -> pd.DataFrame:
    """Saved-runs table for display (friendly mode names, 'open' marker and, with ``llm_info``
    (run_id -> llm_summary text), which LLM outputs each run kept)."""
    view = saved.assign(mode=saved["mode"].map(lambda m: state.MODE_LABELS.get(m, m)),
                        open=saved["run_id"] == active_id)
    if llm_info is not None:
        view[LLM_COLUMN] = [llm_info.get(str(r)) or NO_LLM for r in view["run_id"]]
    order = ["open", "label", "created_at", "dataset", "mode", *runs.COUNT_KEYS, LLM_COLUMN, "size_mb", "run_id"]
    return view[[c for c in order if c in view.columns]].reset_index(drop=True)


def _plural(n: int, one: str, many: str) -> str:
    return f"{n:,} {one if n == 1 else many}"


def _useful(value: Any) -> bool:
    """An LLM output worth counting: a dict that is not a recorded failure."""
    return isinstance(value, dict) and bool(value) and value.get("ok", True) is not False


def llm_summary(llm: Any) -> str:
    """What a saved LLM state holds, e.g. 'brief · 12 explanations · 5 borderline · 2 cluster summaries'
    ('' when nothing was saved). Failed items are not counted; keys from older app versions are ignored."""
    data = _normalized_llm(llm)
    parts = []
    if _useful(data.get("executive")):
        parts.append("brief")
    explain = sum(_useful(v) for v in _as_dict(data.get("explain")).values())
    borderline = sum(_useful(v) for v in _as_dict(data.get("borderline")).values())
    clusters = sum(_useful(v) for v in _as_dict(data.get("clusters")).values())
    if explain:
        parts.append(_plural(explain, "explanation", "explanations"))
    if borderline:
        parts.append(f"{borderline:,} borderline")
    if clusters:
        parts.append(_plural(clusters, "cluster summary", "cluster summaries"))
    return " · ".join(parts)


def _normalized_llm(llm: Any) -> dict[str, Any]:
    """runs.normalize_llm_state: executive, explain, borderline and clusters (legacy keys dropped)."""
    normalize = state.engine_fn(runs, "normalize_llm_state")
    if normalize is not None:
        try:
            return dict(normalize(llm))
        except Exception:  # noqa: BLE001 - fall back to the plain shape below
            pass
    source = llm if isinstance(llm, dict) else {}
    out = {key: _as_dict(source.get(key)) for key in ("explain", "borderline", "clusters")}
    executive = source.get("executive")
    out["executive"] = dict(executive) if isinstance(executive, dict) and executive else None
    return out


def format_value(value: Any, limit: int = 140) -> str:
    """Config value as short display text."""
    if value is None:
        return "(not set)"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, (list, tuple)):
        text = ", ".join(str(v) for v in value) or "(none)"
    elif isinstance(value, dict):
        text = json.dumps(value, sort_keys=True, default=str) if value else "(none)"
    else:
        text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def setting_label(key: str) -> str:
    """'shortlist.top_n' -> 'Shortlist › top n'."""
    parts = key.split(".")
    head = SECTION_LABELS.get(parts[0], parts[0].replace("_", " ").capitalize())
    return " › ".join([head, *(p.replace("_", " ") for p in parts[1:])])


def _tier_list_size(meta: Any) -> int:
    education = _as_dict(_as_dict(_as_dict(meta).get("config")).get("education"))
    return len(_as_dict(education.get("tier_mapping")))


def _names(count: int) -> str:
    return "(none)" if not count else f"{count:,} college name{'s' if count != 1 else ''}"


def fold_tier_list_changes(changes: list[tuple[str, Any, Any]], meta_a: Any = None,
                           meta_b: Any = None) -> list[tuple[str, Any, Any]]:
    """One summary row instead of a row per college when the runs' tier lists differ
    (runs.config_changes flattens education.tier_mapping into one setting per name)."""
    kept: list[tuple[str, Any, Any]] = []
    differ, found = 0, False
    for key, before, after in changes:
        name = str(key)
        if name == TIER_LIST_SETTING or name.startswith(f"{TIER_LIST_SETTING}."):
            found = True
            differ += name != TIER_LIST_SETTING
            continue
        kept.append((key, before, after))
    if found:
        size_a, size_b = _tier_list_size(meta_a), _tier_list_size(meta_b)
        detail = f" ({differ:,} differ)" if differ and size_a and size_b else ""
        kept.append((TIER_LIST_SETTING, _names(size_a), f"{_names(size_b)}{detail}"))
    return kept


def config_changes_frame(changes: list[tuple[str, Any, Any]]) -> pd.DataFrame:
    rows = [{"setting": setting_label(str(k)), "run_a": format_value(a), "run_b": format_value(b)}
            for k, a, b in changes]
    return pd.DataFrame(rows, columns=["setting", "run_a", "run_b"])


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _frame(value: Any) -> pd.DataFrame:
    return value if isinstance(value, pd.DataFrame) else pd.DataFrame()


def _int(value: Any) -> int | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if math.isfinite(number) else None


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _reason(exc: BaseException) -> str:
    """User-facing error text (RunStoreError messages are written to be shown as-is)."""
    return str(exc) if isinstance(exc, runs.RunStoreError) else state.safe_error(exc)


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------
def llm_state() -> dict[str, Any]:
    """The session's LLM outputs, in the shape RunStore.save expects and RunStore.load returns:
    {"executive": dict | None, "explain": {...}, "borderline": {...}, "clusters": {...}}."""
    ss = st.session_state
    return _normalized_llm({"executive": ss.get(state.K_LLM_EXEC), "explain": ss.get(state.K_LLM_EXPLAIN),
                            "borderline": ss.get(state.K_LLM_BORDERLINE), "clusters": ss.get(state.K_LLM_CLUSTERS)})


def _signature(llm: dict[str, Any]) -> str:
    """Fingerprint of what a save would capture: the evaluated result (data + config) and the four
    LLM output stores (executive brief, explanations, borderline explanations, cluster summaries)."""
    try:
        payload = json.dumps([st.session_state.get(state.K_RESULT_KEY), llm], sort_keys=True, default=str)
    except TypeError:  # non-string keys cannot be sorted
        payload = json.dumps([st.session_state.get(state.K_RESULT_KEY), llm], default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _active_run(saved: pd.DataFrame) -> dict[str, Any] | None:
    """The saved run currently on screen (opened or just saved), or None. Clears stale markers."""
    ss = st.session_state
    info = ss.get(K_ACTIVE)
    fp = ss.get(state.K_PREPARED_FP)
    valid = (isinstance(info, dict) and isinstance(fp, str) and fp and info.get("fp") == fp
             and ss.get(state.K_RESULT) is not None and info.get("run_id") in set(saved["run_id"]))
    if valid:
        return info
    if info is not None or ss.get(state.K_LOADED_RUN):
        ss.pop(K_ACTIVE, None)
        ss[state.K_LOADED_RUN] = ""
    return None


def _dataset_label(active: dict[str, Any] | None) -> str:
    current = str(st.session_state.get(state.K_DATASET) or "")
    if current.startswith(SAVED_PREFIX):
        return str((active or {}).get("dataset") or current[len(SAVED_PREFIX):])
    return current


def apply_loaded_run(run_id: str, prepared: PreparedData, result: PipelineResult, llm: dict[str, Any],
                     meta: dict[str, Any]) -> None:
    """Make a loaded run the session's current screening (no re-evaluation needed) and restore its
    LLM outputs: executive brief, explanations, borderline explanations and cluster summaries."""
    ss = st.session_state
    cfg = result.config if isinstance(result.config, AppConfig) else AppConfig()
    fp = f"run:{run_id}"
    restored = _normalized_llm(llm)
    state.set_config(cfg)
    ss[state.K_PREPARED] = prepared
    ss[state.K_PREPARED_FP] = fp
    ss[state.K_RESULT] = result
    ss[state.K_RESULT_KEY] = state.eval_key(fp, cfg)
    ss[state.K_LLM_EXPLAIN] = _as_dict(restored.get("explain"))
    ss[state.K_LLM_BORDERLINE] = _as_dict(restored.get("borderline"))
    ss[state.K_LLM_CLUSTERS] = _as_dict(restored.get("clusters"))
    executive = restored.get("executive")
    if isinstance(executive, dict) and executive:
        ss[state.K_LLM_EXEC] = dict(executive)
    else:
        ss.pop(state.K_LLM_EXEC, None)
    label = runs.clean_label(meta.get("label"), run_id)
    ss[state.K_DATASET] = f"{SAVED_PREFIX}{label}"
    ss[state.K_LOADED_RUN] = run_id
    ss[state.K_EVAL_MEMO] = OrderedDict()
    for key in (state.K_RISK_MEMO, state.K_DD_CANDIDATE, state.K_COMPARE_IDS):
        ss.pop(key, None)
    ss[K_ACTIVE] = {"run_id": run_id, "label": label, "fp": fp, "sig": _signature(llm_state()),
                    "dataset": str(meta.get("dataset") or "")}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def render(result: PipelineResult | None) -> None:
    try:
        store = runs.RunStore()
    except Exception as exc:  # noqa: BLE001 - e.g. an unusable SCREENING_RUNS_DIR
        log.warning("Run store unavailable (%s)", type(exc).__name__)
        st.error(f"Saved runs are unavailable: {_reason(exc)}")
        return
    try:
        saved: pd.DataFrame | None = store.list_runs()
    except Exception as exc:  # noqa: BLE001
        log.warning("Listing saved runs failed (%s)", type(exc).__name__)
        st.error(f"The saved runs could not be listed: {_reason(exc)}")
        saved = None
    active = _active_run(saved if saved is not None else pd.DataFrame(columns=runs.LIST_COLUMNS))
    _save_section(store, result, active)
    if saved is None:
        return
    st.divider()
    _saved_runs_section(store, saved, active)
    st.divider()
    _compare_section(store, saved)


def _save_section(store: runs.RunStore, result: PipelineResult | None, active: dict[str, Any] | None) -> None:
    ss = st.session_state
    st.subheader("Save current run")
    prepared = state.get_prepared()
    llm = llm_state()
    sig = _signature(llm)
    unchanged = active is not None and active.get("sig") == sig
    if active is not None:
        name = theme.md_escape(active.get("label") or active.get("run_id"))
        if unchanged:
            st.info(f"You are viewing saved run **{name}**. Change the configuration or the LLM outputs to save a "
                    "new version.", icon=":material/history:")
        else:
            st.caption(f"Based on saved run **{name}**, with changes made since it was saved or opened.")

    marker = f"{ss.get(state.K_PREPARED_FP, '')}|{(active or {}).get('run_id', '')}"
    if K_LABEL not in ss or ss.get(K_LABEL_FOR) != marker:
        dataset = _dataset_label(active) or "Screening run"
        ss[K_LABEL] = runs.clean_label(f"{dataset} · {datetime.now():%Y-%m-%d %H:%M}")
        ss[K_LABEL_FOR] = marker

    if result is None:
        blocker = "Run a screening first, or open a saved run below."
    elif prepared is None:
        blocker = ("This result cannot be saved because its prepared data is not in this session; run the "
                   "screening again.")
    elif unchanged:
        blocker = "already saved"
    else:
        blocker = None
    c1, c2 = st.columns([3, 1], vertical_alignment="bottom")
    label = c1.text_input("Run label", key=K_LABEL, max_chars=runs.MAX_LABEL_LEN, disabled=result is None,
                          help="A name to recognise this run later, e.g. the drive and what you changed.")
    clicked = c2.button("Save run", type="primary", icon=":material/save:", key="hist_save", width="stretch",
                        disabled=blocker is not None)
    if blocker and not unchanged:
        st.caption(blocker)
    if result is not None and prepared is not None:
        kept = llm_summary(llm)
        st.caption(f"Saving keeps this session's LLM outputs: {kept}." if kept else
                   "No LLM outputs in this session yet; the run is saved without them.")
    st.caption(f"{storage_note(store.base_dir)} {RETENTION_NOTE}")
    if clicked and blocker is None and prepared is not None and result is not None:
        _save(store, prepared, result, label, llm, active)


def _save(store: runs.RunStore, prepared: PreparedData, result: PipelineResult, label: str, llm: dict[str, Any],
          active: dict[str, Any] | None) -> None:
    ss = st.session_state
    name = runs.clean_label(label)
    dataset = _dataset_label(active)
    try:
        with st.spinner("Saving the run..."):
            run_id = store.save(prepared, result, name, llm_state=llm, dataset_label=dataset)
    except Exception as exc:  # noqa: BLE001 - keep the app alive, explain the failure
        log.warning("Saving a run failed (%s)", type(exc).__name__)
        st.error(f"The run could not be saved: {_reason(exc)}")
        return
    ss[K_ACTIVE] = {"run_id": run_id, "label": name, "fp": ss.get(state.K_PREPARED_FP), "sig": _signature(llm),
                    "dataset": dataset}
    ss[state.K_LOADED_RUN] = run_id
    ss[K_PICK] = run_id
    ss.pop(K_CMP_CACHE, None)
    state.queue_message(f"Saved run '{theme.md_escape(name)}'.", icon=":material/save:")
    st.rerun()


def _on_table_pick() -> None:
    """Row selected in the saved-runs table -> select that run in the picker."""
    ss = st.session_state
    try:
        rows = list(ss[K_TABLE]["selection"]["rows"])  # type: ignore[index]
    except Exception:  # noqa: BLE001 - no / malformed selection state
        return
    ids = ss.get(K_TABLE_IDS) or []
    if rows and 0 <= int(rows[0]) < len(ids):
        ss[K_PICK] = ids[int(rows[0])]


def _runs_config() -> dict[str, Any]:
    cc = st.column_config
    counts = {"candidates": "Candidates", "shortlisted": "Shortlisted", "in_review": "In review", "high": "High risk",
              "medium": "Medium risk", "perfect_scorers": "Perfect scorers", "clusters": "Clusters"}
    return {
        "open": cc.CheckboxColumn("Open", width="small", help="The run currently on screen"),
        "label": cc.TextColumn("Label", width="medium"),
        "created_at": cc.DatetimeColumn("Saved", format="D MMM YYYY, HH:mm"),
        "dataset": cc.TextColumn("Dataset"),
        "mode": cc.TextColumn("Inputs"),
        **{k: cc.NumberColumn(v, format="%d") for k, v in counts.items()},
        LLM_COLUMN: cc.TextColumn("LLM outputs", width="medium",
                                  help="LLM text saved with the run (executive brief, candidate and borderline "
                                       "explanations, cluster summaries); restored when the run is loaded. The LLM "
                                       "never changed scores, ranks or the shortlist."),
        "size_mb": cc.NumberColumn("Size (MB)", format="%.2f"),
        "run_id": cc.TextColumn("Run ID"),
    }


def saved_llm_summaries(store: runs.RunStore, run_ids: list[str]) -> dict[str, str]:
    """run_id -> llm_summary of the run's saved LLM outputs ('' = none, 'unreadable' = damaged file).

    Reads each run's llm.json once per change of the file (cached in the session)."""
    ss = st.session_state
    cache = ss.get(K_LLM_INFO) if isinstance(ss.get(K_LLM_INFO), dict) else {}
    out: dict[str, str] = {}
    for run_id in run_ids:
        if not runs.RUN_ID_RE.match(str(run_id)):
            continue
        path = Path(store.base_dir) / str(run_id) / LLM_FILE
        try:
            info = path.stat()
        except OSError:
            out[run_id] = ""  # no llm.json: nothing was saved
            continue
        stamp = (info.st_mtime_ns, info.st_size)
        hit = cache.get(run_id)
        if isinstance(hit, tuple) and len(hit) == 2 and hit[0] == stamp:
            out[run_id] = str(hit[1])
            continue
        try:
            doc = runs.read_json(path)
            summary = llm_summary(doc.get("state") if isinstance(doc, dict) else None)
        except Exception:  # noqa: BLE001 - a damaged file only hides this summary
            summary = "unreadable"
        cache[run_id] = (stamp, summary)
        out[run_id] = summary
    ss[K_LLM_INFO] = {k: v for k, v in cache.items() if k in out}
    return out


def _saved_runs_section(store: runs.RunStore, saved: pd.DataFrame, active: dict[str, Any] | None) -> None:
    ss = st.session_state
    st.subheader("Saved runs")
    if saved.empty:
        st.info("No saved runs yet. Save a screening run above to reopen it later without re-processing the files.",
                icon=":material/inventory_2:")
        return
    ids = [str(r) for r in saved["run_id"]]
    labels = run_labels(saved)
    active_id = str((active or {}).get("run_id", ""))
    ss[K_TABLE_IDS] = ids
    try:
        llm_info: dict[str, str] | None = saved_llm_summaries(store, ids)
    except Exception as exc:  # noqa: BLE001 - the column is informative only
        log.warning("Reading saved LLM outputs failed (%s)", type(exc).__name__)
        llm_info = None
    st.dataframe(runs_view(saved, active_id, llm_info), key=K_TABLE, on_select=_on_table_pick,
                 selection_mode="single-row", hide_index=True, width="stretch", column_config=_runs_config(),
                 height=min(420, 42 + 35 * len(saved)))
    if ss.get(K_PICK) not in ids:
        ss[K_PICK] = active_id if active_id in ids else ids[0]
    c1, c2 = st.columns([3, 1], vertical_alignment="bottom")
    pick = c1.selectbox("Saved run", ids, key=K_PICK, format_func=lambda r: labels.get(r, r),
                        help="Or select a row in the table above.")
    if c2.button("Load", type="primary", icon=":material/open_in_browser:", key="hist_load", width="stretch",
                 help="Open this run: results, configuration and LLM outputs are restored, ready to review or "
                      "re-tune."):
        _load(store, pick)
    _rename_delete(store, saved, pick, active)


def _load(store: runs.RunStore, run_id: str) -> None:
    try:
        with st.spinner("Opening the saved run..."):
            prepared, result, llm, meta = store.load(run_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("Opening a saved run failed (%s)", type(exc).__name__)
        st.error(f"This saved run could not be opened: {_reason(exc)}")
        return
    apply_loaded_run(run_id, prepared, result, llm, meta)
    st.session_state.pop(K_CMP_CACHE, None)
    name = theme.md_escape(runs.clean_label(meta.get("label"), run_id))
    kept = llm_summary(llm)
    state.queue_message(f"Opened saved run '{name}' ({len(result.candidates):,} candidates"
                        + (f"; LLM outputs restored: {kept})." if kept else ")."), icon=":material/history:")
    st.rerun()


def _rename_delete(store: runs.RunStore, saved: pd.DataFrame, run_id: str, active: dict[str, Any] | None) -> None:
    ss = st.session_state
    match = saved.loc[saved["run_id"] == run_id, "label"]
    current = str(match.iloc[0]) if len(match) else run_id
    with st.expander("Rename or delete this run", icon=":material/edit:"):
        rename_key = f"hist_rename_{run_id}"
        if rename_key not in ss:
            ss[rename_key] = current
        c1, c2 = st.columns([3, 1], vertical_alignment="bottom")
        new_label = c1.text_input("New label", key=rename_key, max_chars=runs.MAX_LABEL_LEN)
        if c2.button("Rename", icon=":material/edit:", key="hist_rename", width="stretch",
                     disabled=runs.clean_label(new_label) == current):
            _rename(store, run_id, new_label, active)
        confirm = st.checkbox("I understand that deleting permanently removes this saved run and the candidate "
                              "data in it.", key=f"hist_delete_confirm_{run_id}")
        if st.button("Delete", icon=":material/delete:", key="hist_delete", disabled=not confirm):
            _delete(store, run_id, active)


def _rename(store: runs.RunStore, run_id: str, label: str, active: dict[str, Any] | None) -> None:
    ss = st.session_state
    try:
        name = store.rename(run_id, label)
    except Exception as exc:  # noqa: BLE001
        log.warning("Renaming a saved run failed (%s)", type(exc).__name__)
        st.error(f"The run could not be renamed: {_reason(exc)}")
        return
    if active is not None and active.get("run_id") == run_id:
        if ss.get(state.K_DATASET) == f"{SAVED_PREFIX}{active.get('label')}":
            ss[state.K_DATASET] = f"{SAVED_PREFIX}{name}"
        ss[K_ACTIVE] = {**active, "label": name}
    ss.pop(K_CMP_CACHE, None)
    state.queue_message(f"Renamed the run to '{theme.md_escape(name)}'.", icon=":material/edit:")
    st.rerun()


def _delete(store: runs.RunStore, run_id: str, active: dict[str, Any] | None) -> None:
    ss = st.session_state
    try:
        store.delete(run_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("Deleting a saved run failed (%s)", type(exc).__name__)
        st.error(f"The run could not be deleted: {_reason(exc)}")
        return
    if active is not None and active.get("run_id") == run_id:
        ss.pop(K_ACTIVE, None)
        ss[state.K_LOADED_RUN] = ""
    ss.pop(K_CMP_CACHE, None)
    state.queue_message("Deleted the saved run.", icon=":material/delete:")
    st.rerun()


# ---------------------------------------------------------------------------
# Compare
# ---------------------------------------------------------------------------
def _compare_section(store: runs.RunStore, saved: pd.DataFrame) -> None:
    ss = st.session_state
    st.subheader("Compare two runs")
    if len(saved) < 2:
        st.caption("Save at least two runs to compare their shortlists and configuration.")
        return
    ids = [str(r) for r in saved["run_id"]]
    labels = run_labels(saved)
    if ss.get(K_CMP_B) not in ids:
        ss[K_CMP_B] = ids[0]
    if ss.get(K_CMP_A) not in ids:
        ss[K_CMP_A] = next(r for r in ids if r != ss[K_CMP_B])
    c1, c2 = st.columns(2)
    run_a = c1.selectbox("Run A (before)", ids, key=K_CMP_A, format_func=lambda r: labels.get(r, r))
    run_b = c2.selectbox("Run B (after)", ids, key=K_CMP_B, format_func=lambda r: labels.get(r, r))
    if run_a == run_b:
        st.info("Pick two different runs to compare.")
        return
    comparison = _comparison(store, saved, run_a, run_b)
    if comparison is not None:
        render_comparison(comparison)


def _comparison(store: runs.RunStore, saved: pd.DataFrame, run_a: str, run_b: str) -> dict[str, Any] | None:
    label_of = dict(zip(saved["run_id"], saved["label"]))
    key = (str(store.base_dir), run_a, run_b, label_of.get(run_a), label_of.get(run_b))
    cached = st.session_state.get(K_CMP_CACHE)
    if isinstance(cached, tuple) and len(cached) == 2 and cached[0] == key:
        return cached[1]
    try:
        with st.spinner("Comparing the runs..."):
            comparison = store.compare_runs(run_a, run_b)
    except Exception as exc:  # noqa: BLE001
        log.warning("Comparing saved runs failed (%s)", type(exc).__name__)
        st.error(f"These runs could not be compared: {_reason(exc)}")
        return None
    st.session_state[K_CMP_CACHE] = (key, comparison)
    return comparison


def _delta(before: float | int | None, after: float | int | None, fmt: str) -> tuple[str | None, str]:
    """(delta text, arrow) for st.metric: '+3 vs A' with an arrow, 'same as A' without."""
    if before is None or after is None:
        return None, "off"
    change = after - before
    if not change:
        return "same as A", "off"
    return f"{change:{fmt}} vs A", "auto"


def _metric(column: Any, label: str, value: float | int | None, before: float | int | None, fmt: str,
            help_text: str | None = None) -> None:
    shown = "-" if value is None else f"{value:{fmt.lstrip('+')}}"
    delta, arrow = _delta(before, value, fmt) if before is not None else (None, "off")
    column.metric(label, shown, delta=delta, delta_color="off", delta_arrow=arrow, border=True, help=help_text)


def _diff_config() -> dict[str, Any]:
    cc = st.column_config
    return {
        S.CANDIDATE_ID: cc.TextColumn("Candidate ID"), S.NAME: cc.TextColumn("Name"),
        S.COLLEGE: cc.TextColumn("College"),
        "rank_before": cc.NumberColumn("Rank (A)", format="%d"), "rank_after": cc.NumberColumn("Rank (B)", format="%d"),
        "rank_change": cc.NumberColumn("Rank change", format="%+d", help="Positive = moved up in run B"),
        "status_before": cc.TextColumn("Status (A)"), "status_after": cc.TextColumn("Status (B)"),
        "score_before": cc.NumberColumn("Score (A)", format="%.1f"),
        "score_after": cc.NumberColumn("Score (B)", format="%.1f"),
        "reason_after": cc.TextColumn("Reason (B)", width="large"),
        "shortlisted_before": cc.CheckboxColumn("Shortlisted (A)"),
        "shortlisted_after": cc.CheckboxColumn("Shortlisted (B)"),
    }


def _diff_table(frame: pd.DataFrame, kind: str, key: str) -> None:
    order = [c for c in DIFF_ORDER.get(kind, []) if c in frame.columns] or list(frame.columns)
    st.dataframe(frame, hide_index=True, width="stretch", column_order=order, column_config=_diff_config(),
                 height=min(380, 42 + 35 * max(len(frame), 1)), key=key)


def render_comparison(comparison: dict[str, Any]) -> None:
    """Summary metrics, entering / leaving tables and configuration changes of compare_runs()."""
    diff = comparison.get("diff") if isinstance(comparison.get("diff"), dict) else {}
    summary = _as_dict(diff.get("summary"))
    metas = comparison.get("meta") or ({}, {})
    meta_a, meta_b = (_as_dict(metas[0]), _as_dict(metas[1])) if len(metas) == 2 else ({}, {})
    cols = st.columns(5)
    _metric(cols[0], "Shortlisted (B)", _int(summary.get("shortlisted_after")), _int(summary.get("shortlisted_before")),
            "+,")
    _metric(cols[1], "Entering shortlist", _int(summary.get("entering")), None, ",",
            "Shortlisted in run B but not in run A")
    _metric(cols[2], "Leaving shortlist", _int(summary.get("leaving")), None, ",",
            "Shortlisted in run A but not in run B")
    _metric(cols[3], "Cutoff score (B)", _float(summary.get("cutoff_after")), _float(summary.get("cutoff_before")),
            "+.1f", "Lowest final score on the shortlist")
    _metric(cols[4], "Integrity review (B)", _int(summary.get("review_after")), _int(summary.get("review_before")),
            "+,")
    counts_a, counts_b = _as_dict(meta_a.get("counts")), _as_dict(meta_b.get("counts"))
    if (meta_a.get("dataset") != meta_b.get("dataset")) or (counts_a.get("candidates") != counts_b.get("candidates")):
        st.caption(":material/info: These runs screened different datasets, so the differences reflect both the "
                   "data and the configuration.")

    left, right = st.columns(2, gap="large")
    for column, kind, title, empty in ((left, "entering", "Entering the shortlist", "Nobody new entered the shortlist."),
                                       (right, "leaving", "Leaving the shortlist", "Nobody left the shortlist.")):
        with column:
            frame = _frame(diff.get(kind))
            st.markdown(f"##### {title} ({len(frame):,})")
            if frame.empty:
                st.caption(empty)
            else:
                _diff_table(frame, kind, key=f"hist_cmp_{kind}")
    for kind, title in (("status_changes", "Status changes"), ("rank_moves", "Rank moves")):
        frame = _frame(diff.get(kind))
        if not frame.empty:
            with st.expander(f"{title} ({len(frame):,})", icon=":material/swap_vert:"):
                _diff_table(frame, kind, key=f"hist_cmp_{kind}")

    st.markdown("##### Configuration changes")
    changes = fold_tier_list_changes(list(comparison.get("config_changes") or []), meta_a, meta_b)
    if not changes:
        st.caption("Both runs used the same configuration.")
        return
    cc = st.column_config
    st.dataframe(config_changes_frame(changes), hide_index=True, width="stretch", key="hist_cmp_config",
                 column_config={"setting": cc.TextColumn("Setting", width="medium"),
                                "run_a": cc.TextColumn("Run A", width="medium"),
                                "run_b": cc.TextColumn("Run B", width="medium")})
