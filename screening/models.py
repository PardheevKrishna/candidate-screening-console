"""Shared data containers passed between pipeline stages and the UI.

All containers are plain dataclasses (picklable -> safe for st.cache_data).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

import pandas as pd

from .config import AppConfig


@dataclass
class ResumeDoc:
    """A raw resume file (uploaded, read from a folder, or extracted from a ZIP)."""

    file_name: str                      # basename only, e.g. "CAND-00012_Asha_Menon.pdf"
    content: bytes = field(repr=False)
    source: str = "upload"              # "upload" | "folder" | "zip:<archive name>"

    @property
    def extension(self) -> str:
        return Path(self.file_name).suffix.lower()

    @cached_property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


@dataclass
class ParsedResume:
    """Structured view of one resume. Produced by resume_parser.parse_resume (never raises)."""

    file_name: str
    file_hash: str
    text: str = ""                                   # full text, whitespace-normalised, line breaks kept
    sections: dict[str, str] = field(default_factory=dict)   # schema.SECTION_KEYS -> text
    emails: list[str] = field(default_factory=list)          # lowercase
    phones: list[str] = field(default_factory=list)          # digits only, last 10 digits
    urls: list[str] = field(default_factory=list)
    github_urls: list[str] = field(default_factory=list)     # normalised "github.com/user/repo" (lowercase)
    linkedin_urls: list[str] = field(default_factory=list)
    name_guess: str | None = None
    candidate_id_guess: str | None = None
    skills: list[str] = field(default_factory=list)          # canonical skill names (skills.py taxonomy)
    skill_evidence: dict[str, list[str]] = field(default_factory=dict)  # skill -> section keys where seen
    projects: list[dict[str, Any]] = field(default_factory=list)
    # each: {"title": str, "text": str, "bullets": list[str], "tech": list[str], "github": str | None}
    experiences: list[dict[str, Any]] = field(default_factory=list)
    # each: {"title": str, "org": str, "text": str, "months": float | None, "is_internship": bool}
    bullets: list[str] = field(default_factory=list)         # every bullet line, cleaned (no bullet glyph)
    education: dict[str, Any] = field(default_factory=dict)
    # keys: degree, branch, college, cgpa (10-pt scale float), grad_year (int) - values may be None
    certifications: list[str] = field(default_factory=list)
    total_experience_months: float = 0.0
    internship_count: int = 0
    project_count: int = 0
    word_count: int = 0
    parse_status: str = "ok"                                 # "ok" | "empty" | "error"
    parse_error: str | None = None
    warnings: list[str] = field(default_factory=list)
    # Hidden / adversarial text found in the file (excluded from scoring text):
    # each {"kind": "instruction"|"invisible_chars"|"hidden_text", "snippet": str, "detail": str}
    injection_findings: list[dict[str, Any]] = field(default_factory=list)
    # When split from a multi-candidate packet: "<packet file name> p.<first>-<last>"
    bundle_source: str | None = None


@dataclass
class DataQualityReport:
    """Everything the user should know about the inputs (mapping, drops, mismatches)."""

    column_mapping: dict[str, str] = field(default_factory=dict)   # source header -> canonical
    unmapped_columns: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)        # optional canonical fields absent
    rows_raw: int = 0
    rows_clean: int = 0
    duplicates_dropped: int = 0
    issues: list[dict[str, Any]] = field(default_factory=list)
    # each: {"severity": "error"|"warning"|"info", "stage": str, "issue": str,
    #        "candidate_id": str|None, "file_name": str|None}

    def add(self, severity: str, stage: str, issue: str, candidate_id: str | None = None,
            file_name: str | None = None) -> None:
        self.issues.append({"severity": severity, "stage": stage, "issue": issue,
                            "candidate_id": candidate_id, "file_name": file_name})

    def extend(self, issues: list[dict[str, Any]]) -> None:
        for it in issues:
            self.add(it.get("severity", "info"), it.get("stage", ""), it.get("issue", ""),
                     it.get("candidate_id"), it.get("file_name"))

    def to_frame(self) -> pd.DataFrame:
        cols = ["severity", "stage", "issue", "candidate_id", "file_name"]
        return pd.DataFrame(self.issues, columns=cols)

    def counts(self) -> dict[str, int]:
        out = {"error": 0, "warning": 0, "info": 0}
        for it in self.issues:
            out[it.get("severity", "info")] = out.get(it.get("severity", "info"), 0) + 1
        return out


@dataclass
class RiskResult:
    """Output of risk.run_risk_analysis. DataFrame columns are documented in docs/ARCHITECTURE.md."""

    focus_ids: list[str] = field(default_factory=list)
    candidate_risk: pd.DataFrame = field(default_factory=pd.DataFrame)
    pairs: pd.DataFrame = field(default_factory=pd.DataFrame)
    clusters: pd.DataFrame = field(default_factory=pd.DataFrame)
    commonality: pd.DataFrame = field(default_factory=pd.DataFrame)
    individual: pd.DataFrame = field(default_factory=pd.DataFrame)
    params: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass
class PipelineResult:
    candidates: pd.DataFrame                       # one row per candidate (see ARCHITECTURE.md)
    parsed_resumes: dict[str, ParsedResume]        # file_hash -> ParsedResume (all parsed files)
    parsed_by_candidate: dict[str, ParsedResume]   # candidate_id -> matched ParsedResume
    matches: pd.DataFrame                          # one row per parsed resume
    risk: RiskResult
    quality: DataQualityReport
    config: AppConfig
    # Always {} for new runs (the LLM never changes scores); kept so runs saved by older
    # versions still load.
    llm_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)
    mode: str = "ta+resumes"                       # "ta+resumes" | "ta_only" | "resumes_only"
