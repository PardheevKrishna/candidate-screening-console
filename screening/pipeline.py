"""Pipeline orchestration: ingest -> parse -> match -> score -> risk -> shortlist.

Split in two so the UI stays fast:

* prepare_data()  - heavy, config-light (file parsing, matching). Run once per upload.
* evaluate()      - fast, config-heavy (scoring, risk, shortlist). Re-run on every
                    rubric / threshold change, reusing the prepared data.

LLM re-scoring only touches candidates near the shortlist cutoff and is blended
50/50 (configurable) with the rule-based experience score.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from . import ingest, matching, resume_parser, risk, scoring
from . import schema as S
from .config import AppConfig
from .models import DataQualityReport, ParsedResume, PipelineResult, ResumeDoc, RiskResult

# progress_cb(stage, done, total, detail) - always invoked from the calling thread
ProgressCB = Callable[[str, int, int, str], None]

RISK_MERGE_COLUMNS = [
    S.IS_FOCUS, S.RISK_SCORE, S.RISK_LEVEL, S.RISK_REASONS, S.CLUSTER_ID,
    S.N_LINKS, S.MAX_PAIR_SCORE, S.INDIVIDUAL_SCORE,
]


@dataclass
class PreparedData:
    """Output of the heavy stages; independent of rubric/risk settings."""

    ta_df: pd.DataFrame | None
    quality: DataQualityReport
    parsed: dict[str, ParsedResume]                 # file_hash -> ParsedResume
    matches: pd.DataFrame
    base: pd.DataFrame                              # one row per candidate (TA + linkage columns)
    parsed_by_candidate: dict[str, ParsedResume]    # candidate_id -> ParsedResume
    mode: str = "ta+resumes"
    timings: dict[str, float] = field(default_factory=dict)


def _noop(stage: str, done: int, total: int, detail: str = "") -> None:  # pragma: no cover
    return None


def prepare_data(
    ta_file: tuple[str, bytes] | None,
    resume_docs: list[ResumeDoc],
    cfg: AppConfig,
    *,
    mapping_override: dict[str, str] | None = None,
    progress_cb: ProgressCB | None = None,
) -> PreparedData:
    cb = progress_cb or _noop
    timings: dict[str, float] = {}

    # 1. TA feed ------------------------------------------------------------
    t = time.perf_counter()
    if ta_file is not None:
        cb("Loading TA feed", 0, 1, ta_file[0])
        ta_df, quality = ingest.load_ta_feed(ta_file[0], ta_file[1], cfg, mapping_override)
        cb("Loading TA feed", 1, 1, f"{len(ta_df):,} candidates")
    else:
        ta_df, quality = None, DataQualityReport()
    timings["ingest"] = time.perf_counter() - t

    # 2. Resumes ------------------------------------------------------------
    t = time.perf_counter()
    docs, issues = resume_parser.expand_uploads(resume_docs, cfg.processing)
    quality.extend(issues)

    def parse_cb(done: int, total: int, name: str) -> None:
        cb("Parsing resumes", done, total, name)

    parsed = resume_parser.parse_resumes(
        docs,
        max_workers=cfg.processing.max_workers,
        progress_cb=parse_cb,
        id_pattern=cfg.processing.candidate_id_pattern,
    ) if docs else {}
    for p in parsed.values():
        if p.parse_status != "ok":
            quality.add("warning" if p.parse_status == "empty" else "error", "parse",
                        p.parse_error or "No extractable text (scanned image PDF? OCR required)",
                        file_name=p.file_name)
    timings["parse"] = time.perf_counter() - t

    # 3. Match resumes to candidates ---------------------------------------
    t = time.perf_counter()
    cb("Matching resumes", 0, 1, "")
    matches = matching.match_resumes(ta_df, parsed, cfg)
    base, parsed_by_candidate, link_issues = matching.link_candidates(ta_df, parsed, matches, cfg)
    quality.extend(link_issues)
    cb("Matching resumes", 1, 1, f"{len(parsed_by_candidate):,} linked")
    timings["match"] = time.perf_counter() - t

    if ta_df is None:
        mode = "resumes_only"
    elif not parsed:
        mode = "ta_only"
    else:
        mode = "ta+resumes"
    return PreparedData(ta_df, quality, parsed, matches, base, parsed_by_candidate, mode, timings)


def _merge_risk(scored: pd.DataFrame, risk_res: RiskResult) -> pd.DataFrame:
    out = scored.drop(columns=[c for c in RISK_MERGE_COLUMNS if c in scored.columns])
    cr = risk_res.candidate_risk
    if cr is None or cr.empty:
        out[S.IS_FOCUS] = False
        out[S.RISK_SCORE] = 0.0
        out[S.RISK_LEVEL] = S.RISK_CLEAR
        out[S.RISK_REASONS] = ""
        out[S.CLUSTER_ID] = ""
        out[S.N_LINKS] = 0
        out[S.MAX_PAIR_SCORE] = 0.0
        out[S.INDIVIDUAL_SCORE] = 0.0
        return out
    keep = [S.CANDIDATE_ID] + [c for c in RISK_MERGE_COLUMNS if c in cr.columns]
    out = out.merge(cr[keep], on=S.CANDIDATE_ID, how="left")
    defaults: dict[str, Any] = {
        S.IS_FOCUS: False, S.RISK_SCORE: 0.0, S.RISK_LEVEL: S.RISK_CLEAR, S.RISK_REASONS: "",
        S.CLUSTER_ID: "", S.N_LINKS: 0, S.MAX_PAIR_SCORE: 0.0, S.INDIVIDUAL_SCORE: 0.0,
    }
    for col, default in defaults.items():
        if col not in out.columns:
            out[col] = default
        else:
            out[col] = out[col].fillna(default)
    out[S.IS_FOCUS] = out[S.IS_FOCUS].astype(bool)
    return out


def evaluate(
    prepared: PreparedData,
    cfg: AppConfig,
    llm_results: dict[str, dict[str, Any]] | None = None,
    *,
    risk_result: RiskResult | None = None,
) -> PipelineResult:
    """Score, run risk analysis, shortlist. Fast enough to re-run on every config change.

    llm_results: candidate_id -> {"ok": bool, "score": 0..1, ...} from llm re-scoring.
    Only candidates inside the *rule-based* cutoff band get the LLM blend, so the
    result is deterministic for a given config + LLM cache.
    Pass risk_result to reuse an existing analysis when only rubric weights changed.
    """
    timings = dict(prepared.timings)

    t = time.perf_counter()
    scored = scoring.score_candidates(prepared.base, prepared.parsed_by_candidate, cfg, None)
    timings["score"] = time.perf_counter() - t

    t = time.perf_counter()
    risk_res = risk_result if risk_result is not None else risk.run_risk_analysis(
        scored, prepared.parsed_by_candidate, cfg, prepared.parsed)
    timings["risk"] = time.perf_counter() - t

    t = time.perf_counter()
    ranked = scoring.apply_shortlist(_merge_risk(scored, risk_res), cfg)

    applied: dict[str, dict[str, Any]] = {}
    if llm_results and cfg.llm.enabled:
        band = set(scoring.cutoff_band_ids(ranked, cfg))
        # Pass the full result dicts (score + rationale) so LLM_RATIONALE reaches the table.
        llm_exp = {
            cid: r
            for cid, r in llm_results.items()
            if cid in band and r.get("ok") and r.get("score") is not None
        }
        if llm_exp:
            rescored = scoring.score_candidates(prepared.base, prepared.parsed_by_candidate, cfg, llm_exp)
            ranked = scoring.apply_shortlist(_merge_risk(rescored, risk_res), cfg)
            applied = {cid: llm_results[cid] for cid in llm_exp}
    timings["shortlist"] = time.perf_counter() - t

    return PipelineResult(
        candidates=ranked,
        parsed_resumes=prepared.parsed,
        parsed_by_candidate=prepared.parsed_by_candidate,
        matches=prepared.matches,
        risk=risk_res,
        quality=prepared.quality,
        config=cfg,
        llm_results=applied,
        timings=timings,
        mode=prepared.mode,
    )


def run_llm_rescore(
    prepared: PreparedData,
    cfg: AppConfig,
    client: Any,
    *,
    progress_cb: ProgressCB | None = None,
) -> dict[str, dict[str, Any]]:
    """LLM double-check of the experience score for candidates near the cutoff.

    Returns candidate_id -> result dict; feed it back into evaluate().
    """
    from . import llm  # local import: optional dependency path

    rules_only = evaluate(prepared, cfg, None)
    band_ids = scoring.cutoff_band_ids(rules_only.candidates, cfg)
    cb = progress_cb or _noop

    def llm_cb(done: int, total: int, detail: str) -> None:
        cb("LLM re-scoring (near cutoff)", done, total, detail)

    return llm.rescore_candidates(band_ids, rules_only.candidates, prepared.parsed_by_candidate,
                                  cfg, client, progress_cb=llm_cb)


def run_pipeline(
    ta_file: tuple[str, bytes] | None,
    resume_docs: list[ResumeDoc],
    cfg: AppConfig | None = None,
    *,
    mapping_override: dict[str, str] | None = None,
    llm_client: Any = None,
    progress_cb: ProgressCB | None = None,
) -> PipelineResult:
    """One-shot convenience used by tests and scripts."""
    cfg = cfg or AppConfig()
    prepared = prepare_data(ta_file, resume_docs, cfg, mapping_override=mapping_override,
                            progress_cb=progress_cb)
    llm_results = None
    if llm_client is not None and cfg.llm.enabled and cfg.llm.rescore_near_cutoff:
        llm_results = run_llm_rescore(prepared, cfg, llm_client, progress_cb=progress_cb)
    return evaluate(prepared, cfg, llm_results)


# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------
DEFAULT_SAMPLE_DIR = Path(__file__).resolve().parent.parent / "data" / "sample"


def load_sample_dataset(
    sample_dir: str | Path | None = None,
) -> tuple[tuple[str, bytes] | None, list[ResumeDoc], str | None]:
    """Load the synthetic dataset written by generate_mock_data.py.

    Returns (ta_file, resume_docs, job_description_text). Missing pieces are None/[].
    """
    base = Path(sample_dir) if sample_dir else DEFAULT_SAMPLE_DIR
    ta_path = base / "ta_feed.csv"
    ta_file = (ta_path.name, ta_path.read_bytes()) if ta_path.exists() else None
    resume_dir = base / "resumes"
    docs = resume_parser.load_resume_folder(resume_dir) if resume_dir.exists() else []
    jd_path = base / "job_description.txt"
    jd = jd_path.read_text(encoding="utf-8") if jd_path.exists() else None
    return ta_file, docs, jd
