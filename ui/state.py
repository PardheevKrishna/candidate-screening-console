"""Session state, configuration access, LLM client and cached pipeline calls.

Heavy work (``pipeline.prepare_data``) runs once per input fingerprint; the fast
``pipeline.evaluate`` is memoised per session on (inputs, config, LLM results),
reusing the previous risk analysis whenever the risk settings did not change.
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
from screening import llm, pipeline
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
K_LLM_RESCORE = "llm_rescore"             # candidate_id -> rescore dict (fed to evaluate)
K_LLM_EXPLAIN = "llm_explain"             # candidate_id -> explanation dict
K_LLM_CLUSTERS = "llm_clusters"           # cluster_id -> summary dict
K_LLM_EXEC = "llm_executive"              # {"ok", "markdown", "error"}
K_LLM_CLIENT = "_llm_client"
K_DATASET = "dataset_label"
K_MESSAGES = "_pending_messages"
K_TABS = "main_tabs"
K_DD_CANDIDATE = "deepdive_candidate"
K_COMPARE_IDS = "compare_candidate_ids"     # list[str] picked for side-by-side comparison
K_ASK_HISTORY = "ask_history"               # list of {question, plan, result, answer, error}
K_LOADED_RUN = "loaded_run_id"              # run id when viewing a saved run ("" otherwise)

TAB_SHORTLIST = "Candidate Shortlist & Leaderboard"
TAB_RISK = "Integrity & Risk Intelligence"
TAB_DEEPDIVE = "Individual Deep-Dive"
TAB_COMPARE = "Compare Candidates"
TAB_CAMPUS = "Campus Distribution"
TAB_ASK = "Ask the Data"
TAB_HISTORY = "Run History"
TAB_CONFIG = "Config & Rubric Tuning"
TAB_LABELS = [TAB_SHORTLIST, TAB_RISK, TAB_DEEPDIVE, TAB_COMPARE, TAB_CAMPUS, TAB_ASK, TAB_HISTORY,
              TAB_CONFIG]

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


# ---------------------------------------------------------------------------
# Basics
# ---------------------------------------------------------------------------
def init_state() -> None:
    """Create default session entries (idempotent)."""
    ss = st.session_state
    if not isinstance(ss.get(K_CONFIG), AppConfig):
        ss[K_CONFIG] = AppConfig()
    ss.setdefault(K_CONFIG_VERSION, 0)
    for key in (K_LLM_RESCORE, K_LLM_EXPLAIN, K_LLM_CLUSTERS):
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
# LLM
# ---------------------------------------------------------------------------
NO_KEY_SOURCE = "no key needed"   # backend authenticates itself (cfg.llm.api_key_env == "")


def _api_key(env_name: str | None = None) -> tuple[str | None, str]:
    """(key, source) for cfg.llm.api_key_env. Env var wins; a missing secrets file never raises.

    An empty api_key_env means the backend handles its own authentication.
    """
    name = get_config().llm.api_key_env if env_name is None else env_name
    if not name:
        return "", NO_KEY_SOURCE
    key = os.environ.get(name, "").strip()
    if key:
        return key, "environment"
    try:
        if st.secrets.load_if_toml_exists():
            value = st.secrets.get(name)
            if value and str(value).strip():
                return str(value).strip(), "secrets"
    except Exception:  # noqa: BLE001 - malformed secrets must not crash the app
        pass
    return None, ""


def llm_key_source() -> str:
    """'environment' | 'secrets' | '' (never the key itself)."""
    return _api_key()[1]


def llm_ready(cfg: AppConfig) -> tuple[bool, str]:
    """Whether LLM actions can run, else a friendly reason."""
    if not cfg.llm.enabled:
        return False, "LLM features are switched off in Config & Rubric Tuning."
    if cfg.llm.api_key_env and _api_key(cfg.llm.api_key_env)[0] is None:
        return False, (f"No LLM API key configured. Set {cfg.llm.api_key_env} in the environment or in "
                       ".streamlit/secrets.toml to enable LLM features.")
    if engine_fn(llm, "LLMClient") is None:
        return False, "The LLM module is not available in this build."
    return True, ""


def get_llm_client(cfg: AppConfig) -> Any | None:
    """Lazily build (and reuse) the LLM client for the current LLM settings."""
    if not llm_ready(cfg)[0]:
        return None
    key = _api_key(cfg.llm.api_key_env)[0] or ""
    cache_key = hashlib.sha256(f"{json.dumps(asdict(cfg.llm), sort_keys=True)}|{key}".encode()).hexdigest()
    cached = st.session_state.get(K_LLM_CLIENT)
    if isinstance(cached, tuple) and cached[0] == cache_key:
        return cached[1]
    client = llm.LLMClient(cfg.llm, key)
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
            st.error(f"The LLM request failed: {safe_error(exc)}. Check the API key, model and network.")
            return None
        status.update(label=f"{title} - done", state="complete", expanded=False)
    return out


# ---------------------------------------------------------------------------
# Screening runs
# ---------------------------------------------------------------------------
def sample_available() -> bool:
    base = pipeline.DEFAULT_SAMPLE_DIR
    return (base / "ta_feed.csv").exists() or (base / "resumes").is_dir()


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


def eval_key(prepared_fp: str, cfg: AppConfig, llm_results: dict[str, Any] | None) -> str:
    """Cache key of an evaluate() call."""
    llm_part = json.dumps(llm_results or {}, sort_keys=True, default=str)
    return hashlib.sha256(f"{prepared_fp}|{cfg.fingerprint()}|{llm_part}".encode()).hexdigest()


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


def _evaluate(prepared: pipeline.PreparedData, prepared_fp: str, cfg: AppConfig,
              llm_results: dict[str, Any] | None) -> PipelineResult:
    """pipeline.evaluate with a small per-session memo and risk-analysis reuse."""
    key = eval_key(prepared_fp, cfg, llm_results)
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
    result = pipeline.evaluate(prepared, cfg, llm_results or None, risk_result=reuse)
    st.session_state[K_RISK_MEMO] = (risk_key, result.risk)
    memo[key] = result
    while len(memo) > _EVAL_MEMO_SIZE:
        memo.popitem(last=False)
    return result


def run_screening(req: RunRequest) -> bool:
    """prepare_data (skipped when inputs are unchanged) + evaluate, with live progress."""
    ss = st.session_state
    cfg = get_config()
    t0 = time.perf_counter()
    with st.status(f"Running screening - {req.label}", expanded=True) as status:
        cb = _progress_writer(st.progress(0.0, text="Starting..."))
        try:
            ta_file, docs = req.ta_file, list(req.resume_docs)
            if req.use_sample:
                cb("Loading sample dataset", 0, 1, "")
                ta_file, docs, jd = pipeline.load_sample_dataset()
                if jd and jd.strip() and cfg.experience.job_description.strip() == DEFAULT_JOB_DESCRIPTION.strip():
                    cfg = replace(cfg, experience=replace(cfg.experience, job_description=jd))
                    set_config(cfg)
            if ta_file is None and not docs:
                status.update(label="Nothing to screen", state="error")
                st.warning("Upload a TA feed and/or resumes first.")
                return False
            fp = inputs_fingerprint(ta_file, docs, req.mapping_override, cfg)
            prepared = ss.get(K_PREPARED) if ss.get(K_PREPARED_FP) == fp else None
            if prepared is None:
                prepared = pipeline.prepare_data(ta_file, docs, cfg, mapping_override=req.mapping_override,
                                                 progress_cb=cb)
            if ss.get(K_PREPARED_FP) != fp:  # new dataset: previous LLM outputs no longer apply
                for key in (K_LLM_RESCORE, K_LLM_EXPLAIN, K_LLM_CLUSTERS):
                    ss[key] = {}
                ss.pop(K_LLM_EXEC, None)
                ss.pop(K_DD_CANDIDATE, None)
                ss[K_EVAL_MEMO] = OrderedDict()
                ss.pop(K_RISK_MEMO, None)
            status.update(label="Scoring, integrity analysis & shortlisting...")
            cb("Scoring & integrity analysis", 0, 1, "")
            llm_results = ss.get(K_LLM_RESCORE) or None
            result = _evaluate(prepared, fp, cfg, llm_results)
        except Exception as exc:  # noqa: BLE001 - show the failure, keep the app alive
            log.error("Screening run failed (%s)", type(exc).__name__)
            status.update(label="Screening failed", state="error", expanded=True)
            st.error(f"Screening failed: {safe_error(exc)}")
            return False
        status.update(label=f"Screening complete - {len(result.candidates):,} candidates", state="complete",
                      expanded=False)
    ss[K_PREPARED], ss[K_PREPARED_FP] = prepared, fp
    ss[K_RESULT], ss[K_RESULT_KEY] = result, eval_key(fp, cfg, llm_results)
    ss[K_DATASET] = req.label
    tm = result.timings
    parts = " · ".join(f"{k} {tm[k]:.1f}s" for k in ("ingest", "parse", "match", "score", "risk") if k in tm)
    queue_message(f"Screened {len(result.candidates):,} candidates in {time.perf_counter() - t0:.1f}s ({parts})")
    return True


def current_result() -> PipelineResult | None:
    """Result for the current config; re-evaluates when config or LLM results changed."""
    ss = st.session_state
    prepared, result = ss.get(K_PREPARED), ss.get(K_RESULT)
    if prepared is None:
        return result
    cfg = get_config()
    fp = str(ss.get(K_PREPARED_FP, ""))
    llm_results = ss.get(K_LLM_RESCORE) or None
    key = eval_key(fp, cfg, llm_results)
    if result is not None and ss.get(K_RESULT_KEY) == key:
        return result
    try:
        with st.spinner("Re-scoring with the updated configuration..."):
            new = _evaluate(prepared, fp, cfg, llm_results)
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
    for key in (K_LLM_RESCORE, K_LLM_EXPLAIN, K_LLM_CLUSTERS):
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
