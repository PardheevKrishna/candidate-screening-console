"""Pipeline orchestration: ingest -> parse -> match -> duplicates -> score -> risk -> shortlist.

Split in two so the UI stays fast:

* prepare_data()  - heavy, config-light (file parsing, matching, duplicate applications).
                    Run once per upload.
* evaluate()      - fast, config-heavy (scoring, risk, shortlist). Re-run on every
                    rubric / threshold change, reusing the prepared data.

Scores, ranks and cutoffs are rule-based only. LLM features (rationales, questions and
summaries) write text next to the results and never feed back into them.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from . import duplicates, ingest, matching, resume_parser, risk, scoring
from . import schema as S
from .config import AppConfig
from .models import DataQualityReport, ParsedResume, PipelineResult, ResumeDoc, RiskResult

# progress_cb(stage, done, total, detail) - always invoked from the calling thread
ProgressCB = Callable[[str, int, int, str], None]

RISK_MERGE_COLUMNS = [
    S.IS_FOCUS, S.RISK_SCORE, S.RISK_LEVEL, S.RISK_REASONS, S.CLUSTER_ID,
    S.N_LINKS, S.MAX_PAIR_SCORE, S.INDIVIDUAL_SCORE, S.INJECTION_FLAGS,
]
_RISK_DEFAULTS: dict[str, Any] = {
    S.IS_FOCUS: False, S.RISK_SCORE: 0.0, S.RISK_LEVEL: S.RISK_CLEAR, S.RISK_REASONS: "",
    S.CLUSTER_ID: "", S.N_LINKS: 0, S.MAX_PAIR_SCORE: 0.0, S.INDIVIDUAL_SCORE: 0.0,
    S.INJECTION_FLAGS: "",
}


@dataclass
class PreparedData:
    """Output of the heavy stages; independent of rubric/risk settings."""

    ta_df: pd.DataFrame | None
    quality: DataQualityReport
    parsed: dict[str, ParsedResume]                 # file_hash -> ParsedResume
    matches: pd.DataFrame
    base: pd.DataFrame                              # one row per candidate (TA + linkage + duplicate columns)
    parsed_by_candidate: dict[str, ParsedResume]    # candidate_id -> ParsedResume
    mode: str = "ta+resumes"                        # "ta+resumes" | "ta_only" | "resumes_only"
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
    def expand_cb(done: int, total: int, detail: str) -> None:
        cb("Reading resume files", done, total, detail)

    docs, issues = resume_parser.expand_uploads(resume_docs, cfg.processing, progress_cb=expand_cb)
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

    # 4. Duplicate applications (same person, several candidate IDs) --------
    t = time.perf_counter()
    cb("Checking duplicate applications", 0, 1, "")
    try:
        base, dup_issues = duplicates.detect_duplicates(base, parsed_by_candidate)
        quality.extend(dup_issues)
    except Exception as exc:  # noqa: BLE001 - a detection failure must not stop the run
        quality.add("warning", duplicates.STAGE,
                    f"Duplicate-application check failed ({type(exc).__name__}); duplicates were not marked.")
    cb("Checking duplicate applications", 1, 1, "")
    timings["duplicates"] = time.perf_counter() - t

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
    if cr is None or cr.empty or S.CANDIDATE_ID not in cr.columns or S.CANDIDATE_ID not in out.columns:
        for col, default in _RISK_DEFAULTS.items():
            out[col] = default
        return out
    keep = [S.CANDIDATE_ID] + [c for c in RISK_MERGE_COLUMNS if c in cr.columns]
    out = out.merge(cr[keep], on=S.CANDIDATE_ID, how="left")
    for col, default in _RISK_DEFAULTS.items():
        if col not in out.columns:
            out[col] = default
        else:
            out[col] = out[col].fillna(default)
    out[S.IS_FOCUS] = out[S.IS_FOCUS].astype(bool)
    return out


def evaluate(
    prepared: PreparedData,
    cfg: AppConfig,
    *,
    risk_result: RiskResult | None = None,
) -> PipelineResult:
    """Score, run risk analysis, shortlist. Fast enough to re-run on every config change.

    Deterministic for given prepared data + config (no LLM input). The run mode
    (``prepared.mode``) decides the "auto" missing-resume policy. Pass ``risk_result`` to reuse
    an existing analysis when only rubric weights changed.
    """
    timings = dict(prepared.timings)
    mode = getattr(prepared, "mode", "ta+resumes")

    t = time.perf_counter()
    scored = scoring.score_candidates(prepared.base, prepared.parsed_by_candidate, cfg, mode=mode)
    timings["score"] = time.perf_counter() - t

    t = time.perf_counter()
    risk_res = risk_result if risk_result is not None else risk.run_risk_analysis(
        scored, prepared.parsed_by_candidate, cfg, prepared.parsed)
    timings["risk"] = time.perf_counter() - t

    t = time.perf_counter()
    ranked = scoring.apply_shortlist(_merge_risk(scored, risk_res), cfg)
    timings["shortlist"] = time.perf_counter() - t

    return PipelineResult(
        candidates=ranked,
        parsed_resumes=prepared.parsed,
        parsed_by_candidate=prepared.parsed_by_candidate,
        matches=prepared.matches,
        risk=risk_res,
        quality=prepared.quality,
        config=cfg,
        timings=timings,
        mode=mode,
    )


def run_pipeline(
    ta_file: tuple[str, bytes] | None,
    resume_docs: list[ResumeDoc],
    cfg: AppConfig | None = None,
    *,
    mapping_override: dict[str, str] | None = None,
    progress_cb: ProgressCB | None = None,
) -> PipelineResult:
    """One-shot convenience used by tests and scripts (prepare_data + evaluate, no LLM)."""
    cfg = cfg or AppConfig()
    prepared = prepare_data(ta_file, resume_docs, cfg, mapping_override=mapping_override,
                            progress_cb=progress_cb)
    return evaluate(prepared, cfg)


# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------
DEFAULT_SAMPLE_DIR = Path(__file__).resolve().parent.parent / "data" / "sample"
SAMPLE_TA_FILE = "test_scores_export.xlsx"   # Workday assessment export (2 sheets)
SAMPLE_RESUME_ZIP = "Resume.zip"             # "Resume/" folder of multi-candidate PDF packets
SAMPLE_JD_FILE = "job_description.txt"


def _sample_ta_path(base: Path) -> Path | None:
    return base / SAMPLE_TA_FILE if (base / SAMPLE_TA_FILE).is_file() else None


def sample_dataset_available(sample_dir: str | Path | None = None) -> bool:
    """True when the sample folder holds a TA export and/or resumes that load_sample_dataset reads."""
    base = Path(sample_dir) if sample_dir else DEFAULT_SAMPLE_DIR
    return _sample_ta_path(base) is not None or (base / SAMPLE_RESUME_ZIP).is_file()


def load_sample_dataset(
    sample_dir: str | Path | None = None,
) -> tuple[tuple[str, bytes] | None, list[ResumeDoc], str | None]:
    """Load the synthetic Workday-style dataset written by generate_mock_data.py.

    * TA file: ``test_scores_export.xlsx``
    * resumes: ``Resume.zip`` as one upload (expanded and split by resume_parser)
    * job description: ``job_description.txt``

    Returns (ta_file, resume_docs, job_description_text). Missing pieces are None/[].
    """
    base = Path(sample_dir) if sample_dir else DEFAULT_SAMPLE_DIR
    ta_path = _sample_ta_path(base)
    ta_file = (ta_path.name, ta_path.read_bytes()) if ta_path is not None else None
    zip_path = base / SAMPLE_RESUME_ZIP
    docs = [ResumeDoc(SAMPLE_RESUME_ZIP, zip_path.read_bytes(), source="folder")] if zip_path.is_file() else []
    jd_path = base / SAMPLE_JD_FILE
    jd = jd_path.read_text(encoding="utf-8") if jd_path.is_file() else None
    return ta_file, docs, jd
