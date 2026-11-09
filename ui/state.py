"""Session state, configuration access, LLM client and cached pipeline calls.

Heavy work (``pipeline.prepare_data``) runs once per input fingerprint and is cached on disk
as a snapshot for fast reloads; the fast ``pipeline.evaluate`` is memoised per session on
(inputs, config), reusing the previous risk analysis whenever the risk settings did not change.
The LLM (GitHub Copilot through the local VS Code proxy) only writes text - it never changes
scores, ranks or the shortlist.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from typing import Any, TypeVar

import streamlit as st

from screening import llm_backends as backends
from screening import llm, pipeline, snapshots
from screening import schema as S
from screening.config import DEFAULT_JOB_DESCRIPTION, AppConfig
from screening.models import PipelineResult, ResumeDoc
from ui.charts import SHORTLIST_STATUSES, flag, num, txt
from ui.theme import Kpi

log = logging.getLogger(__name__)
T = TypeVar("T")

# ---------------------------------------------------------------------------
# Session-state keys
# ---------------------------------------------------------------------------
K_CONFIG = "app_config"
K_CONFIG_VERSION = "app_config_version"   # bumped on every config change (form widget keys)
K_PREPARED = "prepared_data"
K_PREPARED_FP = "prepared_fingerprint"
K_RESULT = "pipeline_result"
K_RESULT_KEY = "pipeline_result_key"
K_EVAL_MEMO = "_evaluate_memo"
K_RISK_MEMO = "_risk_memo"
K_LLM_EXPLAIN = "llm_explain"             # candidate_id -> explanation dict (shortlisted, focus "qualified")
K_LLM_BORDERLINE = "llm_borderline"       # candidate_id -> explanation dict (just below the cutoff, focus "gaps")
K_LLM_CLUSTERS = "llm_clusters"           # cluster_id -> summary dict
K_LLM_EXEC = "llm_executive"              # {"ok", "markdown", "error"}
K_LLM_CLIENT = "_llm_client"
K_LLM_STATUS = "_llm_status"              # (cache key, checked_at, proxy_status dict)
LLM_STORES = (K_LLM_EXPLAIN, K_LLM_BORDERLINE, K_LLM_CLUSTERS)
K_DATASET = "dataset_label"
K_MESSAGES = "_pending_messages"
K_TABS = "main_tabs"
K_DD_CANDIDATE = "deepdive_candidate"
K_COMPARE_IDS = "compare_candidate_ids"     # list[str] picked for side-by-side comparison
K_LOADED_RUN = "loaded_run_id"              # run id when viewing a saved run ("" otherwise)

TAB_SHORTLIST = "Candidate Shortlist & Leaderboard"
TAB_RISK = "Integrity & Risk Intelligence"
TAB_DEEPDIVE = "Individual Deep-Dive"
TAB_COMPARE = "Compare Candidates"
TAB_CAMPUS = "Campus Distribution"
TAB_HISTORY = "Run History"
TAB_CONFIG = "Config & Rubric Tuning"
TAB_LABELS = [TAB_SHORTLIST, TAB_RISK, TAB_DEEPDIVE, TAB_COMPARE, TAB_CAMPUS, TAB_HISTORY, TAB_CONFIG]

MODE_LABELS = {"ta+resumes": "TA feed + resumes", "ta_only": "TA feed only", "resumes_only": "Resumes only"}
_EVAL_MEMO_SIZE = 4
_SECRET_RE = re.compile("|".join([p.pattern for p in backends.KEY_PATTERNS] + [r"Bearer\s+\S+"]))

ProgressCB = Callable[[str, int, int, str], None]
LLMProgressCB = Callable[[int, int, str], None]


@dataclass
class RunRequest:
    """What the user asked to screen (built by the sidebar / empty state)."""

    label: str
    ta_file: tuple[str, bytes] | None = None
    resume_docs: list[ResumeDoc] = field(default_factory=list)
    mapping_override: dict[str, str] | None = None
    use_sample: bool = False
    snapshot_fp: str | None = None      # reload a cached dataset (sidebar "Recent datasets")


# ---------------------------------------------------------------------------
# Basics
# ---------------------------------------------------------------------------
def init_state() -> None:
    """Create default session entries (idempotent)."""
    ss = st.session_state
    if not isinstance(ss.get(K_CONFIG), AppConfig):
        ss[K_CONFIG] = AppConfig()
    ss.setdefault(K_CONFIG_VERSION, 0)
    for key in LLM_STORES:
        ss.setdefault(key, {})
    ss.setdefault(K_MESSAGES, [])


def get_config() -> AppConfig:
    cfg = st.session_state.get(K_CONFIG)
    return cfg if isinstance(cfg, AppConfig) else AppConfig()


def set_config(cfg: AppConfig) -> None:
    """Store a new config; dependent results re-evaluate lazily on the next access."""
    st.session_state[K_CONFIG] = cfg
    st.session_state[K_CONFIG_VERSION] = int(st.session_state.get(K_CONFIG_VERSION, 0)) + 1


def config_version() -> int:
    return int(st.session_state.get(K_CONFIG_VERSION, 0))


def engine_fn(module: object, name: str) -> Callable[..., Any] | None:
    """``module.name`` if implemented, else None (None when the engine lacks it)."""
    try:
        fn = getattr(module, name)
    except Exception:  # noqa: BLE001 - NotImplementedError from stubs, AttributeError, ...
        return None
    return fn if callable(fn) else None


def safe_error(exc: BaseException) -> str:
    """Short user-facing error text with anything secret-looking redacted."""
    msg = _SECRET_RE.sub("[redacted]", str(exc)).strip()
    return f"{type(exc).__name__}: {msg[:300]}" if msg else type(exc).__name__


def queue_message(text: str, icon: str = ":material/check_circle:") -> None:
    """Toast shown at the start of the next script run (survives st.rerun)."""
    st.session_state.setdefault(K_MESSAGES, []).append((text, icon))


def flush_messages() -> None:
    for text, icon in st.session_state.get(K_MESSAGES, []):
        st.toast(text, icon=icon)
    st.session_state[K_MESSAGES] = []


def get_prepared() -> pipeline.PreparedData | None:
    return st.session_state.get(K_PREPARED)


def llm_store(key: str) -> dict[str, Any]:
    """Mutable per-session store for LLM outputs (K_LLM_EXPLAIN, K_LLM_CLUSTERS, ...)."""
    store = st.session_state.get(key)
    if not isinstance(store, dict):
        store = {}
        st.session_state[key] = store
    return store


def select_candidate(candidate_id: str, open_tab: bool = False) -> None:
    """Point the Deep-Dive at a candidate. Call from callbacks (before widgets render)."""
    st.session_state[K_DD_CANDIDATE] = candidate_id
    if open_tab:
        st.session_state[K_TABS] = TAB_DEEPDIVE


# ---------------------------------------------------------------------------
# LLM (GitHub Copilot through the local VS Code proxy)
# ---------------------------------------------------------------------------
TOKEN_ENV = "environment"
TOKEN_SECRETS = "secrets"
TOKEN_FILE = "connection file"      # published by the running VS Code extension
_STATUS_TTL_SEC = 30.0


def _secret(name: str) -> str:
    """st.secrets[name] or ''; a missing or malformed secrets file never raises."""
    try:
        if name and st.secrets.load_if_toml_exists():
            value = st.secrets.get(name)
            if value and str(value).strip():
                return str(value).strip()
    except Exception:  # noqa: BLE001 - malformed secrets must not crash the app
        pass
    return ""


def llm_token(cfg: AppConfig | None = None) -> tuple[str | None, str]:
    """(proxy token, source). Environment variable, then .streamlit/secrets.toml, then the
    connection file the VS Code extension writes. Never logged or shown."""
    name = (cfg or get_config()).llm.api_key_env
    if name:
        value = os.environ.get(name, "").strip()
        if value:
            return value, TOKEN_ENV
        value = _secret(name)
        if value:
            return value, TOKEN_SECRETS
    token, source = llm.resolve_token(None, "", discover=True)
    return (token, TOKEN_FILE) if token and source else (None, "")


def llm_key_source() -> str:
    """'environment' | 'secrets' | 'connection file' | '' (never the token itself)."""
    return llm_token()[1]


def llm_ready(cfg: AppConfig) -> tuple[bool, str]:
    """Whether LLM actions can run, else a friendly reason (no network call)."""
    if not cfg.llm.enabled:
        return False, "LLM features are switched off in Config & Rubric Tuning."
    if engine_fn(llm, "LLMClient") is None:
        return False, "The LLM module is not available in this build."
    if llm_token(cfg)[0] is None:
        return False, ("GitHub Copilot proxy not found. Open VS Code with the Screening Copilot Proxy "
                       "extension running (see docs/COPILOT_PROXY.md); the app connects automatically.")
    return True, ""


def llm_status(cfg: AppConfig, refresh: bool = False) -> dict[str, Any]:
    """Proxy reachability and model ids, re-checked at most every 30 s (or on ``refresh``).

    -> {"reachable", "models", "error", "base_url", "token_source", "checked_at"}; never the token.
    """
    token, source = llm_token(cfg)
    key = hashlib.sha256(f"{cfg.llm.base_url}|{source}|{token or ''}".encode()).hexdigest()
    cached = st.session_state.get(K_LLM_STATUS)
    now = time.time()
    if (not refresh and isinstance(cached, tuple) and len(cached) == 3 and cached[0] == key
            and now - float(cached[1]) < _STATUS_TTL_SEC):
        return dict(cached[2])
    if token is None:
        status: dict[str, Any] = {"reachable": False, "models": [], "base_url": cfg.llm.base_url,
                                  "token_source": "", "error": llm_ready(cfg)[1] if cfg.llm.enabled else ""}
    else:
        try:
            status = dict(backends.proxy_status(cfg.llm, token))
        except Exception as exc:  # noqa: BLE001 - status checks must never break a page
            status = {"reachable": False, "models": [], "base_url": cfg.llm.base_url, "token_source": source,
                      "error": safe_error(exc)}
        status["token_source"] = source
    status["checked_at"] = now
    st.session_state[K_LLM_STATUS] = (key, now, status)
    return dict(status)


def get_llm_client(cfg: AppConfig) -> Any | None:
    """Lazily build (and reuse) the LLM client for the current LLM settings."""
    if not llm_ready(cfg)[0]:
        return None
    token, source = llm_token(cfg)
    # A token from the connection file also brings the proxy's address (port changes are followed).
    key = "" if source == TOKEN_FILE else (token or "")
    try:   # the proxy address is part of the key, so a changed extension port is followed without a reload
        address = backends.resolve_connection(cfg.llm, key or None)[0]
    except Exception:  # noqa: BLE001
        address = cfg.llm.base_url
    cache_key = hashlib.sha256(
        f"{json.dumps(asdict(cfg.llm), sort_keys=True)}|{source}|{token}|{address}".encode()).hexdigest()
    cached = st.session_state.get(K_LLM_CLIENT)
    if isinstance(cached, tuple) and cached[0] == cache_key:
        return cached[1]
    client = llm.LLMClient(cfg.llm, key or None)
    st.session_state[K_LLM_CLIENT] = (cache_key, client)
    return client


def run_llm_task(title: str, task: Callable[[LLMProgressCB], T]) -> T | None:
    """Run an LLM batch inside st.status with a progress bar; None on failure (shown to the user)."""
    with st.status(title, expanded=True) as status:
        bar = st.progress(0.0, text="Starting...")

        def cb(done: int, total: int, detail: str = "") -> None:
            bar.progress(min(1.0, done / total) if total else 0.0, text=f"{done:,} / {total:,}")

        try:
            out = task(cb)
        except Exception as exc:  # noqa: BLE001 - surface any client/network error politely
            log.warning("LLM task failed (%s)", type(exc).__name__)
            status.update(label=f"{title} - failed", state="error", expanded=True)
            st.error(f"The LLM request failed: {safe_error(exc)}. Check that VS Code is open with the "
                     "Screening Copilot Proxy extension running and GitHub Copilot signed in.")
            return None
        status.update(label=f"{title} - done", state="complete", expanded=False)
    return out


# ---------------------------------------------------------------------------
# Screening runs
# ---------------------------------------------------------------------------
def sample_available() -> bool:
    try:
        return bool(pipeline.sample_dataset_available())
    except Exception:  # noqa: BLE001
        return False


def inputs_fingerprint(ta_file: tuple[str, bytes] | None, docs: list[ResumeDoc],
                       mapping: dict[str, str] | None, cfg: AppConfig) -> str:
    """sha256 over file hashes + column mapping + processing config."""
    payload = {
        "ta": [ta_file[0], hashlib.sha256(ta_file[1]).hexdigest()] if ta_file else None,
        "resumes": sorted([d.file_name, d.sha256] for d in docs),
        "mapping": sorted((mapping or {}).items()),
        "processing": asdict(cfg.processing),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def eval_key(prepared_fp: str, cfg: AppConfig) -> str:
    """Cache key of an evaluate() call (scores depend on the inputs and the config only)."""
    return hashlib.sha256(f"{prepared_fp}|{cfg.fingerprint()}".encode()).hexdigest()


def _progress_writer(bar: Any) -> ProgressCB:
    last = {"t": 0.0, "stage": ""}

    def cb(stage: str, done: int, total: int, detail: str = "") -> None:
        now = time.perf_counter()
        if stage == last["stage"] and 0 < done < total and now - float(last["t"]) < 0.15:
            return  # throttle UI updates
        last.update(t=now, stage=stage)
        frac = min(1.0, max(0.0, done / total)) if total > 0 else 0.0
        text = f"{stage} - {done:,}/{total:,}" if total > 1 else stage
        if detail and total <= 1:
            text = f"{stage} - {str(detail)[:60]}"
        bar.progress(frac, text=text)

    return cb


def _evaluate(prepared: pipeline.PreparedData, prepared_fp: str, cfg: AppConfig) -> PipelineResult:
    """pipeline.evaluate with a small per-session memo and risk-analysis reuse."""
    key = eval_key(prepared_fp, cfg)
    memo = st.session_state.get(K_EVAL_MEMO)
    if not isinstance(memo, OrderedDict):
        memo = OrderedDict()
        st.session_state[K_EVAL_MEMO] = memo
    if key in memo:
        memo.move_to_end(key)
        return memo[key]
    risk_key = hashlib.sha256(f"{prepared_fp}|{json.dumps(asdict(cfg.risk), sort_keys=True)}".encode()).hexdigest()
    cached_risk = st.session_state.get(K_RISK_MEMO)
    reuse = cached_risk[1] if isinstance(cached_risk, tuple) and cached_risk[0] == risk_key else None
    result = pipeline.evaluate(prepared, cfg, risk_result=reuse)
    st.session_state[K_RISK_MEMO] = (risk_key, result.risk)
    memo[key] = result
    while len(memo) > _EVAL_MEMO_SIZE:
        memo.popitem(last=False)
    return result


def _load_snapshot(fp: str, cfg: AppConfig) -> pipeline.PreparedData | None:
    """Cached prepared data for ``fp`` (None when absent, expired, stale or disabled). Never raises."""
    if not cfg.snapshots.enabled:
        return None
    try:
        return snapshots.load_snapshot(fp, cfg)
    except Exception as exc:  # noqa: BLE001 - a broken cache only costs a re-parse
        log.warning("Snapshot load failed (%s)", type(exc).__name__)
        return None


def _save_snapshot(prepared: pipeline.PreparedData, fp: str, label: str, cfg: AppConfig) -> None:
    if not cfg.snapshots.enabled:
        return
    try:
        snapshots.save_snapshot(prepared, fp, label, cfg)
    except Exception as exc:  # noqa: BLE001 - caching is best effort
        log.warning("Snapshot save failed (%s)", type(exc).__name__)


def recent_datasets(cfg: AppConfig | None = None) -> Any:
    """Cached datasets (snapshots.LIST_COLUMNS), newest first; expired ones are purged first."""
    cfg = cfg or get_config()
    try:
        snapshots.purge_expired(cfg)
        return snapshots.list_snapshots(cfg)
    except Exception as exc:  # noqa: BLE001
        log.warning("Listing snapshots failed (%s)", type(exc).__name__)
        return None


def _with_sample_job_description(cfg: AppConfig, jd: str | None = None) -> AppConfig:
    """Use the sample's job_description.txt while the config still holds the default JD."""
    if jd is None:
        try:
            path = pipeline.DEFAULT_SAMPLE_DIR / pipeline.SAMPLE_JD_FILE
            jd = path.read_text(encoding="utf-8") if path.is_file() else ""
        except OSError:
            jd = ""
    if jd and jd.strip() and cfg.experience.job_description.strip() == DEFAULT_JOB_DESCRIPTION.strip():
        cfg = replace(cfg, experience=replace(cfg.experience, job_description=jd))
        set_config(cfg)
    return cfg


def run_screening(req: RunRequest) -> bool:
    """prepare_data (skipped when inputs are unchanged or cached) + evaluate, with live progress."""
    ss = st.session_state
    cfg = get_config()
    t0 = time.perf_counter()
    source = "parsed"
    with st.status(f"Running screening - {req.label}", expanded=True) as status:
        cb = _progress_writer(st.progress(0.0, text="Starting..."))
        try:
            if req.snapshot_fp:
                fp = req.snapshot_fp
                prepared = ss.get(K_PREPARED) if ss.get(K_PREPARED_FP) == fp else None
                if prepared is None:
                    cb("Loading cached dataset", 0, 1, "")
                    prepared = _load_snapshot(fp, cfg)
                    source = "cache"
                if prepared is not None and "synthetic sample" in req.label.lower():
                    cfg = _with_sample_job_description(cfg)
                if prepared is None:
                    status.update(label="Cached dataset not available", state="error")
                    st.warning("This dataset is no longer in the cache (expired or removed). "
                               "Upload the files again to screen it.")
                    return False
            else:
                ta_file, docs = req.ta_file, list(req.resume_docs)
                if req.use_sample:
                    cb("Loading sample dataset", 0, 1, "")
                    ta_file, docs, jd = pipeline.load_sample_dataset()
                    cfg = _with_sample_job_description(cfg, jd)
                if ta_file is None and not docs:
                    status.update(label="Nothing to screen", state="error")
                    st.warning("Upload a TA feed and/or resumes first.")
                    return False
                fp = inputs_fingerprint(ta_file, docs, req.mapping_override, cfg)
                prepared = ss.get(K_PREPARED) if ss.get(K_PREPARED_FP) == fp else None
                if prepared is None:
                    prepared = _load_snapshot(fp, cfg)
                    source = "cache" if prepared is not None else source
                if prepared is None:
                    prepared = pipeline.prepare_data(ta_file, docs, cfg, mapping_override=req.mapping_override,
                                                     progress_cb=cb)
                    cb("Caching parsed data for fast reload", 0, 1, "")
                    _save_snapshot(prepared, fp, req.label, cfg)
            if ss.get(K_PREPARED_FP) != fp:  # new dataset: previous LLM outputs no longer apply
                for key in LLM_STORES:
                    ss[key] = {}
                ss.pop(K_LLM_EXEC, None)
                ss.pop(K_DD_CANDIDATE, None)
                ss[K_EVAL_MEMO] = OrderedDict()
                ss.pop(K_RISK_MEMO, None)
            status.update(label="Scoring, integrity analysis & shortlisting...")
            cb("Scoring & integrity analysis", 0, 1, "")
            result = _evaluate(prepared, fp, cfg)
        except Exception as exc:  # noqa: BLE001 - show the failure, keep the app alive
            log.error("Screening run failed (%s)", type(exc).__name__)
            status.update(label="Screening failed", state="error", expanded=True)
            st.error(f"Screening failed: {safe_error(exc)}")
            return False
        status.update(label=f"Screening complete - {len(result.candidates):,} candidates", state="complete",
                      expanded=False)
    ss[K_PREPARED], ss[K_PREPARED_FP] = prepared, fp
    ss[K_RESULT], ss[K_RESULT_KEY] = result, eval_key(fp, cfg)
    ss[K_DATASET] = req.label
    tm = result.timings
    parts = " · ".join(f"{k} {tm[k]:.1f}s" for k in ("ingest", "parse", "match", "score", "risk") if k in tm)
    how = " from the cache" if source == "cache" else ""
    queue_message(f"Screened {len(result.candidates):,} candidates{how} in {time.perf_counter() - t0:.1f}s"
                  + (f" ({parts})" if parts else ""))
    return True


def current_result() -> PipelineResult | None:
    """Result for the current config; re-evaluates when the config changed."""
    ss = st.session_state
    prepared, result = ss.get(K_PREPARED), ss.get(K_RESULT)
    if prepared is None:
        return result
    cfg = get_config()
    fp = str(ss.get(K_PREPARED_FP, ""))
    key = eval_key(fp, cfg)
    if result is not None and ss.get(K_RESULT_KEY) == key:
        return result
    try:
        with st.spinner("Re-scoring with the updated configuration..."):
            new = _evaluate(prepared, fp, cfg)
    except Exception as exc:  # noqa: BLE001
        log.error("Re-evaluation failed (%s)", type(exc).__name__)
        st.error(f"Re-scoring failed - showing the previous results. ({safe_error(exc)})")
        return result
    ss[K_RESULT], ss[K_RESULT_KEY] = new, key
    return new


def clear_results() -> None:
    """Forget the current dataset and every derived output (config is kept)."""
    for key in (K_PREPARED, K_PREPARED_FP, K_RESULT, K_RESULT_KEY, K_EVAL_MEMO, K_RISK_MEMO, K_LLM_EXEC,
                K_DATASET, K_DD_CANDIDATE):
        st.session_state.pop(key, None)
    for key in LLM_STORES:
        st.session_state[key] = {}


# ---------------------------------------------------------------------------
# Summaries for the header / KPI row
# ---------------------------------------------------------------------------
def dataset_summary(result: PipelineResult | None) -> str:
    if result is None:
        return "No dataset loaded - choose a data source in the sidebar to begin."
    w = result.config.weights.normalized()
    label = st.session_state.get(K_DATASET) or "Dataset"
    return (f"{label} · {len(result.candidates):,} candidates · {len(result.parsed_resumes):,} resumes parsed · "
            f"{MODE_LABELS.get(result.mode, result.mode)} · rubric {w.hackerrank:.0f}/{w.skills:.0f}/"
            f"{w.experience:.0f}/{w.education:.0f}")


def summary_kpis(result: PipelineResult) -> list[Kpi]:
    df = result.candidates
    n = len(df)
    status = txt(df, S.STATUS)
    short = status.isin(SHORTLIST_STATUSES)
    n_review, n_hold = int((status == S.STATUS_REVIEW).sum()), int((status == S.STATUS_HOLD).sum())
    risk = txt(df, S.RISK_LEVEL)
    high, med = int((risk == S.RISK_HIGH).sum()), int((risk == S.RISK_MEDIUM).sum())
    scores = num(df, S.FINAL_SCORE)
    perfect = int((num(df, S.HR_PCT) >= 99.995).sum())
    clusters = 0 if result.risk.clusters is None else len(result.risk.clusters)
    avg_short = scores[short].mean()
    review_sub = f"{n_review:,} under integrity review" + (f" · {n_hold:,} on hold" if n_hold else "")
    return [
        Kpi("Total Processed", f"{n:,}", f"{int(flag(df, S.HAS_RESUME).sum()):,} with a linked resume"),
        Kpi("Shortlisted", f"{int(short.sum()):,}", review_sub),
        Kpi("Risk Flags (High / Medium)", f"{high:,} / {med:,}", f"{clusters:,} linked cluster(s)", "gold"),
        Kpi("Average Score", f"{scores.mean():.1f}" if scores.notna().any() else "-",
            f"shortlist average {avg_short:.1f}" if short.any() else "out of 100", "slate"),
        Kpi("Perfect Scorers (100%)", f"{perfect:,}", f"{perfect / n:.1%} of candidates" if n else "", "gold"),
    ]
