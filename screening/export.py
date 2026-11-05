"""CSV / Excel / evidence-pack exports for recruiters and the TA team.

All exports neutralise spreadsheet formula injection: a text cell that begins with
= + - @ (or a tab / carriage return) is prefixed with an apostrophe, so a value such as
'=HYPERLINK(...)' smuggled into a TA feed or resume is shown as text and never evaluated
by Excel / Google Sheets. Plain signed numbers ("-5", "+919876543210") are left as is.

Integrity flags are written as review evidence ("pattern consistent with ..."), never
as verdicts.
"""
from __future__ import annotations

import io
import json
import math
import re
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd
import xlsxwriter

from . import schema as S
from .config import AppConfig
from .models import PipelineResult
from .scoring import IN_SHORTLIST, explain_score, order_columns

SHEET_NAMES = [
    "Summary", "Shortlist", "All Candidates", "Integrity Evidence", "Risk Clusters",
    "Risk Pairs", "Commonality", "Data Quality", "Config",
]
BRAND_RED = "#D71E28"
HIGH_FILL, HIGH_FONT = "#F8D7DA", "#842029"
MEDIUM_FILL, MEDIUM_FONT = "#FFF3CD", "#664D03"
CLEAR_FILL, CLEAR_FONT = "#D1E7DD", "#0F5132"
DISCLAIMER = (
    "Integrity risk flags are evidence for human review, not findings of misconduct. A flag means "
    "the data shows a pattern consistent with shared work, collaboration or a shared test "
    "environment - patterns that can also have legitimate explanations (shared campus network, "
    "scheduled lab session, common resume templates, group projects). Verify with the candidate "
    "(live coding re-test, project walkthrough, proctored re-assessment) before any decision, and "
    "never present a flag to a candidate as a conclusion."
)

_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")
_SIGNED_NUMBER_RE = re.compile(r"[+-](?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_EXCEL_MAX_CHARS = 32767
_MAX_LINKS = 10
_LEVEL_ORDER = {S.RISK_HIGH: 0, S.RISK_MEDIUM: 1, S.RISK_LOW: 2, S.RISK_CLEAR: 3}

_LABELS: dict[str, str] = {
    **S.TA_FIELD_LABELS,
    S.RANK: "Rank", S.STATUS: "Status", S.STATUS_REASON: "Status Reason", S.ELIGIBLE: "Eligible",
    S.FINAL_SCORE: "Final Score", S.PTS_HR: "HackerRank Pts", S.PTS_SKILLS: "Skills Pts",
    S.PTS_EXPERIENCE: "Experience Pts", S.PTS_EDUCATION: "Education Pts",
    S.SUB_HR: "HackerRank Sub", S.SUB_SKILLS: "Skills Sub", S.SUB_EXPERIENCE: "Experience Sub",
    S.SUB_EDUCATION: "Education Sub", S.HR_PCT: "HackerRank %", S.RISK_LEVEL: "Risk Level",
    S.RISK_SCORE: "Risk Score", S.RISK_REASONS: "Risk Reasons", S.CLUSTER_ID: "Cluster",
    S.IS_FOCUS: "Perfect Scorer", S.N_LINKS: "Risk Links", S.MAX_PAIR_SCORE: "Max Pair Score",
    S.INDIVIDUAL_SCORE: "Individual Risk Score", S.MATCHED_SKILLS: "Matched Skills",
    S.MISSING_SKILLS: "Missing Skills", S.N_MATCHED_SKILLS: "# Matched Skills",
    S.SKILLS_FOUND: "Skills Found", S.INTERNSHIP_COUNT: "Internships",
    S.EXPERIENCE_MONTHS: "Experience (months)", S.PROJECT_COUNT: "Projects",
    S.CERTIFICATIONS: "Certifications", S.CERT_COUNT: "# Certifications",
    S.RESUME_CGPA: "Resume CGPA", S.RESUME_DEGREE: "Resume Degree", S.RELEVANCE: "JD Relevance",
    S.EXPERIENCE_SOURCE: "Experience Source", S.LLM_RATIONALE: "LLM Rationale",
    S.SCORE_NOTES: "Score Notes", S.IP_SUBNET: "IP Subnet", S.EMAIL_DOMAIN: "Email Domain",
    "test_slot": "Test Slot", S.RESUME_HASH: "Resume Hash", S.HAS_RESUME: "Has Resume",
    S.MATCH_METHOD: "Match Method", S.MATCH_CONFIDENCE: "Match Confidence",
    S.DATA_FLAGS: "Data Flags",
    # risk frames
    "cand_a": "Candidate A", "cand_b": "Candidate B", "name_a": "Name A", "name_b": "Name B",
    "pair_score": "Pair Score", "signals": "Signals", "signal_keys": "Signal Keys",
    "shared_hashes": "Identical Answers", "resume_similarity": "Resume Similarity",
    "shared_bullets": "Shared Bullets", "shared_bullet_examples": "Shared Bullet Examples",
    "shared_github": "Shared GitHub", "same_ip": "Same IP", "same_subnet": "Same Subnet",
    "same_college": "Same College", "same_phone": "Same Phone", "same_email": "Same Email",
    "start_gap_sec": "Start Gap (s)", "submit_gap_sec": "Submit Gap (s)",
    "duration_gap_min": "Duration Gap (min)", "sequential_gap_min": "Sequential Gap (min)",
    "size": "Size", "members": "Members", "member_names": "Member Names",
    "mean_pair_score": "Mean Pair Score", "density": "Density", "top_signals": "Top Signals",
    "shared_traits": "Shared Traits", "colleges": "Colleges", "n_colleges": "# Colleges",
    "start_span_min": "Start Span (min)", "submit_span_min": "Submit Span (min)",
    "summary": "Summary", "attribute": "Attribute", "value": "Value",
    "focus_count": "Focus Count", "focus_total": "Focus Total", "focus_share": "Focus Share",
    "population_count": "Population Count", "population_total": "Population Total",
    "population_share": "Population Share", "lift": "Lift", "p_value": "p-value",
    "q_value": "q-value (BH)", "significant": "Significant",
    # integrity evidence / data quality / llm
    "individual_flags": "Individual Flags", "linked_candidates": "Linked Candidates",
    "link_evidence": "Link Evidence (pair score: signals)", "ai_notes": "AI Notes (LLM)",
    "ai_headline": "AI Headline (LLM)", "ai_summary": "AI Summary (LLM)",
    "ai_actions": "AI Recommended Actions (LLM)", "severity": "Severity", "stage": "Stage",
    "issue": "Issue", "file_name": "File Name", "key": "Setting",
}
_INT_COLS = {
    S.RANK, S.N_LINKS, S.N_MATCHED_SKILLS, S.INTERNSHIP_COUNT, S.PROJECT_COUNT, S.CERT_COUNT,
    S.TAB_SWITCHES, S.COPY_PASTE, S.GRAD_YEAR, "size", "n_colleges", "shared_bullets",
    "focus_count", "focus_total", "population_count", "population_total",
}
_DEC1_COLS = {
    S.HR_PCT, S.CGPA, S.RESUME_CGPA, S.COMPLETION_MIN, S.EXPERIENCE_MONTHS, S.RISK_SCORE,
    S.MAX_PAIR_SCORE, S.INDIVIDUAL_SCORE, "pair_score", "mean_pair_score", "start_gap_sec",
    "submit_gap_sec", "duration_gap_min", "sequential_gap_min", "start_span_min", "submit_span_min",
}
_DEC3_COLS = {
    S.SUB_HR, S.SUB_SKILLS, S.SUB_EXPERIENCE, S.SUB_EDUCATION, S.RELEVANCE, S.MATCH_CONFIDENCE,
    "resume_similarity", "density",
}
_PCT_COLS = {"focus_share", "population_share"}
_SCI_COLS = {"p_value", "q_value"}
_WIDE_COLS = {
    S.STATUS_REASON, S.RISK_REASONS, S.SCORE_NOTES, S.LLM_RATIONALE, S.SKILLS_FOUND,
    S.CERTIFICATIONS, S.MATCHED_SKILLS, S.MISSING_SKILLS, "signals", "shared_bullet_examples",
    "shared_traits", "summary", "members", "member_names", "issue", "link_evidence",
    "individual_flags", "ai_notes", "ai_summary", "ai_actions", "value",
}
_SHORTLIST_COLUMNS = [
    S.RANK, S.CANDIDATE_ID, S.NAME, S.STATUS, S.FINAL_SCORE, S.PTS_HR, S.PTS_SKILLS,
    S.PTS_EXPERIENCE, S.PTS_EDUCATION, S.HR_PCT, S.MATCHED_SKILLS, S.MISSING_SKILLS, S.COLLEGE,
    S.COLLEGE_TIER, S.DEGREE, S.BRANCH, S.CGPA, S.GRAD_YEAR, S.EMAIL, S.PHONE, S.RISK_LEVEL,
    S.RISK_SCORE, S.CLUSTER_ID, S.RISK_REASONS, S.STATUS_REASON, S.EXPERIENCE_SOURCE,
    S.LLM_RATIONALE, S.SCORE_NOTES,
]


# ---------------------------------------------------------------------------
# Sanitising + small helpers
# ---------------------------------------------------------------------------
def sanitize_cell(value: Any) -> Any:
    """Prefix text that a spreadsheet would treat as a formula with an apostrophe."""
    if (isinstance(value, str) and value and value[0] in _FORMULA_TRIGGERS
            and not _SIGNED_NUMBER_RE.fullmatch(value)):
        return "'" + value
    return value


def sanitize_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Copy of ``df`` with every text cell (and header) passed through sanitize_cell."""
    out = df.copy()
    for col in out.columns:
        s = out[col]
        if (pd.api.types.is_object_dtype(s.dtype) or pd.api.types.is_string_dtype(s.dtype)
                or isinstance(s.dtype, pd.CategoricalDtype)):
            out[col] = s.astype(object).map(sanitize_cell)
    out.columns = [sanitize_cell(str(c)) for c in out.columns]
    return out


def _isna(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (str, list, tuple, set, dict, np.ndarray)):
        return False
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _clean(value: Any) -> str:
    return "" if _isna(value) else str(value).strip()


def _float(value: Any) -> float | None:
    if _isna(value) or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _truthy(value: Any) -> bool:
    if _isna(value):
        return False
    if isinstance(value, (bool, np.bool_, int, float, np.integer, np.floating)):
        return bool(value)
    return str(value).strip().lower() in {"true", "t", "yes", "y", "1"}


def _fmt(value: float | None, dp: int = 1) -> str:
    if value is None:
        return ""
    text = f"{value:.{dp}f}".rstrip("0").rstrip(".") if dp > 0 else f"{value:.0f}"
    return "0" if text in ("", "-0") else text


def _label(col: Any) -> str:
    name = str(col)
    return _LABELS.get(name) or name.replace("_", " ").strip().title()


def _text_of(value: Any) -> str:
    if isinstance(value, (list, tuple, set)):
        return ", ".join(map(str, value))
    if isinstance(value, dict):
        return json.dumps(value, default=str, sort_keys=True)
    return str(value)


def _split_reasons(value: Any) -> list[str]:
    return [p.strip() for p in _clean(value).split(S.SEP_REASONS.strip()) if p.strip()]


def _level(value: Any) -> str:
    text = _clean(value).title()
    return text or S.RISK_CLEAR


def _col(df: pd.DataFrame | None, col: str) -> pd.Series:
    if df is None or col not in df.columns:
        return pd.Series([None] * (0 if df is None else len(df)), index=None if df is None else df.index,
                         dtype=object)
    return df[col]


def _frame(df: Any) -> pd.DataFrame:
    return df if isinstance(df, pd.DataFrame) else pd.DataFrame()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _name_map(candidates: pd.DataFrame, col: str = S.NAME) -> dict[str, str]:
    if S.CANDIDATE_ID not in candidates.columns or col not in candidates.columns:
        return {}
    return {_clean(k): _clean(v) for k, v in zip(candidates[S.CANDIDATE_ID], candidates[col])}


def _links_for(pairs: pd.DataFrame, candidate_id: str) -> pd.DataFrame:
    """Pairs touching ``candidate_id`` with an ``other`` column, strongest first."""
    if pairs.empty or not {"cand_a", "cand_b"} <= set(pairs.columns):
        return pd.DataFrame(columns=["other", "pair_score", "signals"])
    a = pairs["cand_a"].map(_clean)
    b = pairs["cand_b"].map(_clean)
    mask = (a == candidate_id) | (b == candidate_id)
    sel = pairs.loc[mask].copy()
    sel["other"] = np.where(a[mask] == candidate_id, b[mask], a[mask])
    sel["_score"] = pd.to_numeric(_col(sel, "pair_score"), errors="coerce").fillna(0.0)
    return sel.sort_values(["_score", "other"], ascending=[False, True], kind="mergesort")


def _individual_flags(individual: pd.DataFrame, candidate_id: str) -> list[str]:
    if individual.empty or S.CANDIDATE_ID not in individual.columns:
        return []
    rows = individual.loc[individual[S.CANDIDATE_ID].map(_clean) == candidate_id]
    out = []
    for flag, detail in zip(_col(rows, "flag"), _col(rows, "detail")):
        text = ": ".join(t for t in (_clean(flag), _clean(detail)) if t)
        if text:
            out.append(text)
    return out


def _llm_candidate_text(note: Any) -> str:
    if not isinstance(note, Mapping):
        return _clean(note)
    parts = [_clean(note.get("summary"))]
    for key, title in (("strengths", "Strengths"), ("gaps", "Gaps")):
        items = [_clean(x) for x in note.get(key) or [] if _clean(x)]
        if items:
            parts.append(f"{title}: {'; '.join(items)}")
    return " ".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------
def to_csv_bytes(df: pd.DataFrame) -> bytes:
    """CSV encoded as UTF-8 with BOM (opens cleanly in Excel), formula-injection safe."""
    text = sanitize_frame(df).to_csv(index=False, lineterminator="\r\n",
                                     date_format="%Y-%m-%d %H:%M:%S")
    return text.encode("utf-8-sig")


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------
class _Book:
    """xlsxwriter workbook with shared brand formats and a typed table writer."""

    def __init__(self, buffer: io.BytesIO) -> None:
        self.wb = xlsxwriter.Workbook(buffer, {
            "in_memory": True, "strings_to_formulas": False, "strings_to_urls": False,
            "strings_to_numbers": False,
        })
        add = self.wb.add_format
        self.header = add({"bold": True, "font_color": "#FFFFFF", "bg_color": BRAND_RED,
                           "border": 1, "border_color": "#BA0C2F", "text_wrap": True,
                           "valign": "top"})
        self.title = add({"bold": True, "font_size": 16, "font_color": BRAND_RED})
        self.subtitle = add({"italic": True, "font_color": "#555555"})
        self.label = add({"bold": True, "valign": "top"})
        self.wrap = add({"text_wrap": True, "valign": "top"})
        self.disclaimer = add({"text_wrap": True, "italic": True, "valign": "top", "border": 1,
                               "bg_color": MEDIUM_FILL, "font_color": MEDIUM_FONT})
        self.int = add({"num_format": "0"})
        self.thousands = add({"num_format": "#,##0", "align": "left"})
        self.dec1 = add({"num_format": "0.0"})
        self.dec2 = add({"num_format": "0.00"})
        self.dec3 = add({"num_format": "0.000"})
        self.pct = add({"num_format": "0.0%"})
        self.sci = add({"num_format": "0.00E+00"})
        self.datetime = add({"num_format": "yyyy-mm-dd hh:mm:ss"})
        self.date = add({"num_format": "yyyy-mm-dd"})
        self.high = add({"bg_color": HIGH_FILL, "font_color": HIGH_FONT})
        self.medium = add({"bg_color": MEDIUM_FILL, "font_color": MEDIUM_FONT})
        self.clear = add({"bg_color": CLEAR_FILL, "font_color": CLEAR_FONT})
        self.review = add({"bg_color": MEDIUM_FILL, "font_color": MEDIUM_FONT, "bold": True})
        self.hold = add({"bg_color": HIGH_FILL, "font_color": HIGH_FONT, "bold": True})

    # -- cells -----------------------------------------------------------
    def cell(self, ws: Any, r: int, c: int, value: Any, fmt: Any = None) -> None:
        if _isna(value):
            return
        if isinstance(value, (bool, np.bool_)):
            ws.write_boolean(r, c, bool(value), fmt)
        elif isinstance(value, (int, np.integer)):
            ws.write_number(r, c, int(value), fmt)
        elif isinstance(value, (float, np.floating)):
            if math.isfinite(value):
                ws.write_number(r, c, float(value), fmt)
        elif isinstance(value, datetime):
            dt = value.to_pydatetime() if isinstance(value, pd.Timestamp) else value
            ws.write_datetime(r, c, dt.replace(tzinfo=None), self.datetime)
        elif isinstance(value, date):
            ws.write_datetime(r, c, datetime(value.year, value.month, value.day), self.date)
        else:
            ws.write_string(r, c, sanitize_cell(_text_of(value))[:_EXCEL_MAX_CHARS], fmt)

    def _column_format(self, col: str, series: pd.Series, wrap: bool) -> Any:
        dtype = series.dtype
        if wrap:
            return self.wrap
        if pd.api.types.is_bool_dtype(dtype):
            return None
        if pd.api.types.is_datetime64_any_dtype(dtype):
            return self.datetime
        if col in _INT_COLS or pd.api.types.is_integer_dtype(dtype):
            return self.int
        for cols, fmt in ((_DEC3_COLS, self.dec3), (_DEC1_COLS, self.dec1), (_PCT_COLS, self.pct),
                          (_SCI_COLS, self.sci)):
            if col in cols:
                return fmt
        return self.dec2 if pd.api.types.is_float_dtype(dtype) else None

    @staticmethod
    def _width(col: str, series: pd.Series, wrap: bool) -> float:
        cap = 60.0 if (col in _WIDE_COLS or wrap) else 40.0
        if pd.api.types.is_numeric_dtype(series.dtype) and not pd.api.types.is_bool_dtype(series.dtype):
            cap = 14.0
        lens = [len(_label(col))]
        for v in series.head(300):
            if not _isna(v):
                text = _text_of(v)
                lens.append(max(len(line) for line in text.split("\n")) if text else 0)
        return float(min(cap, max(8.0, max(lens) + 2.0)))

    # -- tables ------------------------------------------------------------
    def table(self, ws: Any, df: pd.DataFrame, *, row: int = 0, col: int = 0,
              freeze_cols: int | None = 0, wrap: Iterable[str] = (),
              widths: Mapping[str, float] | None = None) -> int:
        """Write ``df`` as a filtered table under a brand header; returns the next free row."""
        columns = [str(c) for c in df.columns]
        if not columns:
            ws.write_string(row, col, "No data available for this run.", self.subtitle)
            return row + 2
        wrap_set, widths = set(wrap), dict(widths or {})
        for j, name in enumerate(columns):
            ws.write_string(row, col + j, sanitize_cell(_label(name)), self.header)
            series = df.iloc[:, j]
            ws.set_column(col + j, col + j, widths.get(name) or self._width(name, series, name in wrap_set))
        formats = [self._column_format(name, df.iloc[:, j], name in wrap_set)
                   for j, name in enumerate(columns)]
        for i, values in enumerate(df.itertuples(index=False, name=None), start=row + 1):
            for j, value in enumerate(values):
                self.cell(ws, i, col + j, value, formats[j])
        last_row, last_col = row + len(df), col + len(columns) - 1
        ws.autofilter(row, col, last_row, last_col)
        if freeze_cols is not None:
            ws.freeze_panes(row + 1, freeze_cols)
        if len(df):
            self._highlight(ws, columns, row + 1, last_row, col)
        return last_row + 2

    def _highlight(self, ws: Any, columns: list[str], first: int, last: int, col0: int) -> None:
        rules = {
            S.RISK_LEVEL: [(S.RISK_HIGH, self.high), (S.RISK_MEDIUM, self.medium)],
            S.STATUS: [(S.STATUS_REVIEW, self.review), (S.STATUS_HOLD, self.hold),
                       (S.STATUS_SHORTLISTED, self.clear)],
            "severity": [("error", self.high), ("warning", self.medium)],
        }
        for j, name in enumerate(columns):
            for value, fmt in rules.get(name, []):
                ws.conditional_format(first, col0 + j, last, col0 + j, {
                    "type": "cell", "criteria": "==", "value": json.dumps(value), "format": fmt})
            if name == "significant":
                ws.conditional_format(first, col0 + j, last, col0 + j, {
                    "type": "cell", "criteria": "==", "value": "TRUE", "format": self.medium})

    def key_values(self, ws: Any, row: int, title: str, items: list[tuple[str, Any]],
                   col: int = 0) -> int:
        """Section header + label/value rows; returns the next free row."""
        ws.write_string(row, col, title, self.header)
        ws.write_string(row, col + 1, "", self.header)
        for k, (key, value) in enumerate(items, start=row + 1):
            ws.write_string(k, col, key, self.label)
            if isinstance(value, (int, np.integer)) and not isinstance(value, (bool, np.bool_)):
                self.cell(ws, k, col + 1, value, self.thousands)
            elif isinstance(value, (float, np.floating)):
                self.cell(ws, k, col + 1, value, self.dec2)
            else:
                self.cell(ws, k, col + 1, value, self.wrap)
        return row + len(items) + 2


def _kpis(result: PipelineResult, cfg: AppConfig) -> list[tuple[str, Any]]:
    c = _frame(result.candidates)
    status = _col(c, S.STATUS).map(_clean)
    level = _col(c, S.RISK_LEVEL).map(_level)
    final = pd.to_numeric(_col(c, S.FINAL_SCORE), errors="coerce")
    listed = status.isin(IN_SHORTLIST)
    pct = pd.to_numeric(_col(c, S.HR_PCT), errors="coerce")
    focus = len(result.risk.focus_ids) if result.risk and result.risk.focus_ids else int(
        (pct >= cfg.risk.focus_threshold_pct).sum())
    no_resume = int((~_col(c, S.HAS_RESUME).map(_truthy).astype(bool)).sum())
    risk = result.risk
    items: list[tuple[str, Any]] = [
        ("Run mode", result.mode),
        ("Candidates processed", len(c)),
        ("Eligible", int(_col(c, S.ELIGIBLE).map(_truthy).sum())),
        ("Shortlisted (total)", int(listed.sum())),
        ("  - Shortlisted", int((status == S.STATUS_SHORTLISTED).sum())),
        ("  - Shortlisted (integrity review)", int((status == S.STATUS_REVIEW).sum())),
        ("Integrity hold", int((status == S.STATUS_HOLD).sum())),
        ("Not shortlisted", int((status == S.STATUS_NOT).sum())),
        ("Ineligible", int((status == S.STATUS_INELIGIBLE).sum())),
        ("Average final score (all)", round(float(final.mean()), 2) if final.notna().any() else "n/a"),
        ("Average final score (shortlist)",
         round(float(final[listed].mean()), 2) if final[listed].notna().any() else "n/a"),
        ("Lowest shortlisted score", round(float(final[listed].min()), 2) if final[listed].notna().any() else "n/a"),
        ("Perfect scorers (risk focus group)", focus),
        ("Risk flags - High", int((level == S.RISK_HIGH).sum())),
        ("Risk flags - Medium", int((level == S.RISK_MEDIUM).sum())),
        ("Risk clusters", len(_frame(risk.clusters)) if risk else 0),
        ("Linked candidate pairs", len(_frame(risk.pairs)) if risk else 0),
        ("Resumes parsed", len(result.parsed_resumes or {})),
        ("Resumes linked to candidates", len(result.parsed_by_candidate or {})),
        ("Candidates without a resume", no_resume if S.HAS_RESUME in c.columns else "n/a"),
        ("LLM re-scored (near cutoff)", len(result.llm_results or {})),
    ]
    return items


def _config_highlights(cfg: AppConfig) -> list[tuple[str, Any]]:
    w, sk, sl, rk, llm = cfg.weights.normalized(), cfg.skills, cfg.shortlist, cfg.risk, cfg.llm
    if sl.method == "top_pct":
        method = f"Top {_fmt(sl.top_pct)}% of eligible candidates"
    elif sl.method == "threshold":
        method = f"Final score >= {_fmt(sl.threshold)}"
    else:
        method = f"Top {int(sl.top_n):,} eligible candidates"
    knockouts = [f"HackerRank >= {_fmt(sl.min_hackerrank_pct)}%" if sl.min_hackerrank_pct else "",
                 f"CGPA >= {_fmt(sl.min_cgpa, 2)}" if sl.min_cgpa else "",
                 f"Graduation year in {', '.join(map(str, sl.allowed_grad_years))}" if sl.allowed_grad_years else ""]
    skills_rule = (f"per skill - full marks at {sk.skills_for_full_marks} matched skills"
                   if sk.mode != "coverage" else "coverage - matched / required")
    policy = ("High risk -> Integrity Hold, slots backfilled" if sl.exclude_high_risk
              else "Flagged candidates stay on the shortlist for recruiter review")
    return [
        ("Rubric (points out of 100)",
         f"HackerRank {_fmt(w.hackerrank)} | Skills {_fmt(w.skills)} | Experience {_fmt(w.experience)} "
         f"| Education {_fmt(w.education)}"),
        ("Skills scoring", skills_rule),
        ("Required skills", ", ".join(sk.required_skills) or "none"),
        ("Shortlist rule", method),
        ("Eligibility knock-outs", "; ".join(k for k in knockouts if k) or "none"),
        ("Integrity policy", f"{policy} (review levels: {', '.join(sl.review_risk_levels) or 'none'})"),
        ("Missing-resume policy", sl.missing_resume_policy),
        ("Risk focus group", f"HackerRank >= {_fmt(rk.focus_threshold_pct)}%"),
        ("Risk thresholds", f"High >= {_fmt(rk.high_threshold)}, Medium >= {_fmt(rk.medium_threshold)}, "
                            f"link >= {_fmt(rk.edge_threshold)}"),
        ("LLM assist", f"{'enabled' if llm.enabled else 'disabled'} - {llm.model}; blend weight "
                       f"{_fmt(llm.blend_weight * 100)}% for +/-{llm.cutoff_band} ranks around the cutoff"),
    ]


def _quality_items(result: PipelineResult) -> list[tuple[str, Any]]:
    q = result.quality
    if q is None:
        return []
    counts = q.counts()
    return [
        ("TA rows (raw / clean)", f"{q.rows_raw:,} / {q.rows_clean:,}"),
        ("Duplicate IDs dropped", q.duplicates_dropped),
        ("Issues (errors / warnings / info)",
         f"{counts.get('error', 0):,} / {counts.get('warning', 0):,} / {counts.get('info', 0):,}"),
        ("Missing optional fields", ", ".join(q.missing_fields) or "none"),
        ("Unmapped TA columns (kept as extra_*)", ", ".join(q.unmapped_columns) or "none"),
    ]


def _executive_summary(llm_notes: Mapping[str, Any]) -> list[str]:
    """LLM executive summary as plain-text lines (markdown headings/bullets/emphasis removed)."""
    note = llm_notes.get("executive_summary")
    if isinstance(note, Mapping):
        note = note.get("markdown") if note.get("ok", True) else ""
    lines = []
    for raw in _clean(note).splitlines():
        line = re.sub(r"^\s*#+\s*", "", raw.strip())
        line = re.sub(r"^[-*+]\s+", "• ", line).replace("**", "")
        if line:
            lines.append(line)
    return lines


def _write_summary(book: _Book, result: PipelineResult, cfg: AppConfig,
                   llm_notes: Mapping[str, Any]) -> None:
    ws = book.wb.add_worksheet("Summary")
    ws.set_column(0, 0, 40)
    ws.set_column(1, 1, 95)
    ws.write_string(0, 0, "Candidate Screening & Integrity Report", book.title)
    ws.write_string(1, 0, f"Generated {_now()} (local time) - internal use only; contains candidate data.",
                    book.subtitle)
    ws.write_string(3, 0, "Important", book.label)
    ws.write_string(3, 1, DISCLAIMER, book.disclaimer)
    ws.set_row(3, 96)
    row = book.key_values(ws, 5, "Key metrics", _kpis(result, cfg))
    row = book.key_values(ws, row, "Configuration highlights", _config_highlights(cfg))
    quality = _quality_items(result)
    if quality:
        row = book.key_values(ws, row, "Data quality", quality)
    if result.timings:
        row = book.key_values(ws, row, "Processing time (s)",
                              [(k, round(float(v), 2)) for k, v in result.timings.items()
                               if _float(v) is not None])
    summary = _executive_summary(llm_notes)
    if summary:
        book.key_values(ws, row, "AI executive summary (LLM-generated - verify before acting)",
                        [("", line) for line in summary])


def _integrity_frame(result: PipelineResult, cfg: AppConfig, llm_candidates: Mapping[str, Any]) -> pd.DataFrame:
    c = _frame(result.candidates)
    columns = [S.CANDIDATE_ID, S.NAME, S.COLLEGE, S.STATUS, S.RANK, S.FINAL_SCORE, S.HR_PCT,
               S.RISK_LEVEL, S.RISK_SCORE, S.CLUSTER_ID, S.RISK_REASONS, "individual_flags",
               S.N_LINKS, "linked_candidates", "link_evidence"]
    if llm_candidates:
        columns.append("ai_notes")
    if c.empty or S.CANDIDATE_ID not in c.columns:
        return pd.DataFrame(columns=columns)
    levels = _col(c, S.RISK_LEVEL).map(_level)
    flag_levels = {S.RISK_HIGH, S.RISK_MEDIUM} | {_level(x) for x in cfg.shortlist.review_risk_levels or []}
    flag_levels.discard(S.RISK_CLEAR)
    mask = levels.isin(flag_levels) | _col(c, S.STATUS).map(_clean).isin([S.STATUS_REVIEW, S.STATUS_HOLD])
    risk = result.risk
    pairs = _frame(risk.pairs) if risk else pd.DataFrame()
    individual = _frame(risk.individual) if risk else pd.DataFrame()
    names = _name_map(c)
    rows = []
    for rec in c.loc[mask].to_dict("records"):
        cid = _clean(rec.get(S.CANDIDATE_ID))
        links = _links_for(pairs, cid)
        evidence = [f"{o} ({names.get(o) or 'unknown'}; pair score {_fmt(_float(s), 0)}): {_clean(sig)}"
                    for o, s, sig in zip(links["other"].head(_MAX_LINKS),
                                         _col(links, "pair_score").head(_MAX_LINKS),
                                         _col(links, "signals").head(_MAX_LINKS))]
        if len(links) > _MAX_LINKS:
            evidence.append(f"... and {len(links) - _MAX_LINKS} more linked candidates (see Risk Pairs)")
        item = {
            S.CANDIDATE_ID: cid, S.NAME: rec.get(S.NAME), S.COLLEGE: rec.get(S.COLLEGE),
            S.STATUS: rec.get(S.STATUS), S.RANK: rec.get(S.RANK), S.FINAL_SCORE: rec.get(S.FINAL_SCORE),
            S.HR_PCT: rec.get(S.HR_PCT), S.RISK_LEVEL: _level(rec.get(S.RISK_LEVEL)),
            S.RISK_SCORE: rec.get(S.RISK_SCORE), S.CLUSTER_ID: rec.get(S.CLUSTER_ID),
            S.RISK_REASONS: "\n".join(_split_reasons(rec.get(S.RISK_REASONS))),
            "individual_flags": "\n".join(_individual_flags(individual, cid)),
            S.N_LINKS: len(links), "linked_candidates": S.SEP_LIST.join(links["other"].tolist()),
            "link_evidence": "\n".join(evidence),
        }
        if llm_candidates:
            item["ai_notes"] = _llm_candidate_text(llm_candidates.get(cid))
        rows.append(item)
    out = pd.DataFrame(rows, columns=columns)
    if out.empty:
        return out
    out["_lvl"] = out[S.RISK_LEVEL].map(lambda v: _LEVEL_ORDER.get(v, 9))
    out["_score"] = pd.to_numeric(out[S.RISK_SCORE], errors="coerce").fillna(0.0)
    out = out.sort_values(["_lvl", "_score", S.CANDIDATE_ID], ascending=[True, False, True], kind="mergesort")
    out[S.RANK] = pd.array(pd.to_numeric(out[S.RANK], errors="coerce"), dtype="Int64")
    return out.drop(columns=["_lvl", "_score"]).reset_index(drop=True)


def _clusters_frame(result: PipelineResult, llm_clusters: Mapping[str, Any]) -> pd.DataFrame:
    clusters = _frame(result.risk.clusters if result.risk else None).copy()
    if clusters.empty or not llm_clusters or "cluster_id" not in clusters.columns:
        return clusters
    notes = [llm_clusters.get(_clean(cid)) for cid in clusters["cluster_id"]]
    get = lambda n, k: n.get(k) if isinstance(n, Mapping) else None  # noqa: E731
    clusters["ai_headline"] = [_clean(get(n, "headline")) for n in notes]
    clusters["ai_summary"] = [_clean(get(n, "summary")) for n in notes]
    clusters["ai_actions"] = ["; ".join(_clean(a) for a in get(n, "recommended_actions") or [] if _clean(a))
                              for n in notes]
    return clusters


def _pairs_frame(result: PipelineResult) -> pd.DataFrame:
    pairs = _frame(result.risk.pairs if result.risk else None).copy()
    if pairs.empty or not {"cand_a", "cand_b"} <= set(pairs.columns):
        return pairs
    names = _name_map(_frame(result.candidates))
    for side in ("a", "b"):
        if f"name_{side}" not in pairs.columns:
            pairs.insert(pairs.columns.get_loc(f"cand_{side}") + 1, f"name_{side}",
                         pairs[f"cand_{side}"].map(lambda x: names.get(_clean(x), "")))
    if "pair_score" in pairs.columns:
        pairs["_s"] = pd.to_numeric(pairs["pair_score"], errors="coerce")
        pairs = pairs.sort_values(["_s", "cand_a", "cand_b"], ascending=[False, True, True],
                                  na_position="last", kind="mergesort").drop(columns="_s")
    return pairs.reset_index(drop=True)


def _flatten(prefix: str, value: Any) -> list[tuple[str, Any]]:
    if isinstance(value, Mapping):
        out: list[tuple[str, Any]] = []
        for key in sorted(value, key=str):
            out += _flatten(f"{prefix}.{key}" if prefix else str(key), value[key])
        return out
    if isinstance(value, (list, tuple, set)):
        return [(prefix, json.dumps(list(value), default=str))]
    return [(prefix, value)]


def _write_data_quality(book: _Book, result: PipelineResult) -> None:
    ws = book.wb.add_worksheet("Data Quality")
    q = result.quality
    issues = q.to_frame() if q is not None else pd.DataFrame(
        columns=["severity", "stage", "issue", S.CANDIDATE_ID, "file_name"])
    if not issues.empty:
        order = {"error": 0, "warning": 1, "info": 2}
        issues = (issues.assign(_o=issues["severity"].map(lambda s: order.get(_clean(s), 3)))
                  .sort_values(["_o", "stage"], kind="mergesort").drop(columns="_o"))
    book.table(ws, issues, freeze_cols=0, widths={"issue": 70.0, "severity": 10.0, "stage": 12.0})
    side = len(issues.columns) + 1
    ws.set_column(side, side, 36)
    ws.set_column(side + 1, side + 1, 40)
    row = book.key_values(ws, 0, "Input summary", _quality_items(result), col=side)
    mapping = list((q.column_mapping or {}).items()) if q is not None else []
    if mapping:
        book.key_values(ws, row, "Column mapping (source -> field)",
                        [(str(src), str(dst)) for src, dst in mapping], col=side)


def build_excel_report(result: PipelineResult, llm_notes: Mapping[str, Any] | None = None) -> bytes:
    """Recruiter workbook (xlsxwriter) with the sheets in ``SHEET_NAMES``.

    llm_notes (optional; every key optional):
      "executive_summary": str or {"ok", "markdown"}           -> Summary sheet
      "clusters":   {cluster_id: {"headline", "summary", "recommended_actions"}} -> Risk Clusters
      "candidates": {candidate_id: {"summary", "strengths", "gaps"}}             -> Shortlist,
                                                                                    Integrity Evidence
    """
    cfg = result.config or AppConfig()
    notes = llm_notes if isinstance(llm_notes, Mapping) else {}
    llm_candidates = notes.get("candidates") if isinstance(notes.get("candidates"), Mapping) else {}
    llm_clusters = notes.get("clusters") if isinstance(notes.get("clusters"), Mapping) else {}
    candidates = order_columns(_frame(result.candidates))

    buffer = io.BytesIO()
    book = _Book(buffer)
    _write_summary(book, result, cfg, notes)

    # Shortlist: shortlisted + integrity review, by rank
    listed = candidates.loc[_col(candidates, S.STATUS).map(_clean).isin(IN_SHORTLIST)]
    if S.RANK in listed.columns:
        listed = listed.sort_values(S.RANK, kind="mergesort", na_position="last")
    shortlist = listed[[c for c in _SHORTLIST_COLUMNS if c in listed.columns]].copy()
    if llm_candidates and S.CANDIDATE_ID in shortlist.columns:
        shortlist["ai_notes"] = [_llm_candidate_text(llm_candidates.get(_clean(cid)))
                                 for cid in shortlist[S.CANDIDATE_ID]]
    book.table(book.wb.add_worksheet("Shortlist"), shortlist, freeze_cols=3,
               widths={S.STATUS_REASON: 55.0, S.RISK_REASONS: 60.0})

    book.table(book.wb.add_worksheet("All Candidates"), candidates, freeze_cols=0)
    book.table(book.wb.add_worksheet("Integrity Evidence"), _integrity_frame(result, cfg, llm_candidates),
               freeze_cols=2, wrap=(S.RISK_REASONS, "individual_flags", "link_evidence", "ai_notes"),
               widths={S.RISK_REASONS: 60.0, "link_evidence": 90.0, "individual_flags": 40.0})
    book.table(book.wb.add_worksheet("Risk Clusters"), _clusters_frame(result, llm_clusters), freeze_cols=1)
    book.table(book.wb.add_worksheet("Risk Pairs"), _pairs_frame(result), freeze_cols=2)
    book.table(book.wb.add_worksheet("Commonality"),
               _frame(result.risk.commonality if result.risk else None), freeze_cols=2)
    _write_data_quality(book, result)
    config = pd.DataFrame(_flatten("", cfg.to_dict()), columns=["key", "value"])
    book.table(book.wb.add_worksheet("Config"), config, freeze_cols=1, wrap=("value",),
               widths={"key": 42.0, "value": 100.0})

    book.wb.close()
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Evidence pack (markdown)
# ---------------------------------------------------------------------------
def _md(value: Any) -> str:
    """Single-line markdown-safe text (escapes table pipes and emphasis characters)."""
    text = " ".join(_clean(value).split())
    return re.sub(r"([\\`*_\[\]<>|#])", r"\\\1", text)


def _find_row(candidates: pd.DataFrame, candidate_id: str) -> dict[str, Any] | None:
    if candidates.empty or S.CANDIDATE_ID not in candidates.columns:
        return None
    ids = candidates[S.CANDIDATE_ID].map(_clean)
    hit = candidates.loc[ids == candidate_id]
    if hit.empty:
        hit = candidates.loc[ids.str.upper() == candidate_id.upper()]
    return None if hit.empty else hit.iloc[0].to_dict()


def _link_details(link: Mapping[str, Any], cfg: AppConfig) -> tuple[list[str], list[str]]:
    """(evidence bullet lines, 'pattern consistent with' themes) for one linked pair."""
    lines: list[str] = []
    themes: list[str] = []
    hashes = _clean(link.get("shared_hashes"))
    if hashes:
        lines.append(f"Identical answer submissions: {_md(hashes)}")
        themes.append("shared solutions")
    sim = _float(link.get("resume_similarity"))
    if sim is not None and sim >= cfg.risk.text_similarity_medium:
        lines.append(f"Resume project/experience text similarity: {sim:.2f} (cosine)")
        themes.append("shared or templated resume content")
    n_bullets = int(_float(link.get("shared_bullets")) or 0)
    if n_bullets:
        examples = [f'"{_md(e)}"' for e in _split_reasons(link.get("shared_bullet_examples"))[:3]]
        lines.append(f"Rare resume bullets shared verbatim: {n_bullets}"
                     + (f" - e.g. {'; '.join(examples)}" if examples else ""))
        themes.append("shared project write-ups")
    github = _clean(link.get("shared_github"))
    if github:
        lines.append(f"Same GitHub repository listed: {_md(github)}")
        themes.append("a shared project")
    network = [t for t, k in (("same IP address", "same_ip"), ("same /24 subnet", "same_subnet"))
               if _truthy(link.get(k))]
    if network:
        lines.append(f"Network: {', '.join(network)}")
        themes.append("a shared test location or network")
    timing = []
    start, submit = _float(link.get("start_gap_sec")), _float(link.get("submit_gap_sec"))
    duration, seq = _float(link.get("duration_gap_min")), _float(link.get("sequential_gap_min"))
    if start is not None and start <= cfg.risk.sync_window_sec:
        timing.append(f"tests started {_fmt(start)} s apart")
    if submit is not None and submit <= cfg.risk.sync_window_sec:
        timing.append(f"submitted {_fmt(submit)} s apart")
    if duration is not None and duration <= cfg.risk.duration_tolerance_min:
        timing.append(f"completion times within {_fmt(duration)} min")
    if seq is not None:
        timing.append(f"one session started {_fmt(seq)} min after the other ended on the same IP")
        themes.append("one person taking both sessions")
    if timing:
        lines.append(f"Timing: {'; '.join(timing)}")
        themes.append("synchronised test sessions")
    identity = [t for t, k in (("same phone number", "same_phone"), ("same email address", "same_email"))
                if _truthy(link.get(k))]
    if identity:
        lines.append(f"Identity: {', '.join(identity)} under different candidate IDs")
        themes.append("duplicate registration")
    if _truthy(link.get("same_college")):
        lines.append("Same college")
    return lines, list(dict.fromkeys(themes))


def build_evidence_pack(result: PipelineResult, candidate_id: str) -> str:
    """Markdown evidence pack for recruiters: profile, score breakdown, risk reasons,
    linked candidates with the shared evidence, and neutral verification steps."""
    cid = _clean(candidate_id)
    candidates = _frame(result.candidates)
    row = _find_row(candidates, cid)
    if row is None:
        return (f"# Integrity evidence pack\n\nCandidate `{_md(cid) or '(blank)'}` was not found in "
                f"this screening run.\n")
    cid = _clean(row.get(S.CANDIDATE_ID))
    cfg = result.config or AppConfig()
    risk = result.risk
    names, colleges = _name_map(candidates), _name_map(candidates, S.COLLEGE)
    level = _level(row.get(S.RISK_LEVEL))
    eligible_n = int(_col(candidates, S.ELIGIBLE).map(_truthy).sum())
    rank = _float(row.get(S.RANK))

    out = [f"# Integrity evidence pack: {_md(cid)}", "",
           f"_Generated {_now()} for internal recruiter review. Integrity flags are evidence for "
           f"human review, not findings of misconduct._", "",
           "## Candidate", "", "| Field | Value |", "|---|---|",
           f"| Candidate ID | {_md(cid)} |",
           f"| Name | {_md(row.get(S.NAME)) or '-'} |",
           f"| College | {_md(row.get(S.COLLEGE)) or '-'} |",
           f"| Status | {_md(row.get(S.STATUS)) or '-'} |",
           f"| Rank | {f'{int(rank):,} of {eligible_n:,} eligible' if rank else '-'} |",
           f"| Status reason | {_md(row.get(S.STATUS_REASON)) or '-'} |", "",
           "## Score breakdown", ""]
    out += [f"- {_md(line)}" for line in explain_score(row, cfg)]

    out += ["", f"## Integrity assessment: {level} risk", ""]
    risk_score = _float(row.get(S.RISK_SCORE))
    facts = [f"risk score {_fmt(risk_score, 0)}" if risk_score is not None else "",
             "perfect-scorer focus group" if _truthy(row.get(S.IS_FOCUS)) else "",
             f"cluster {_md(row.get(S.CLUSTER_ID))}" if _clean(row.get(S.CLUSTER_ID)) else ""]
    if any(facts):
        out += [f"**{level}** ({', '.join(f for f in facts if f)})", ""]
    reasons = _split_reasons(row.get(S.RISK_REASONS))
    flags = _individual_flags(_frame(risk.individual if risk else None), cid)
    if level == S.RISK_CLEAR and not reasons and not flags:
        out += ["No integrity flags were raised for this candidate. No verification beyond the "
                "standard process is suggested.", ""]
        return "\n".join(out)
    if reasons:
        out += ["Reasons (strongest first):", ""] + [f"- {_md(r)}" for r in reasons] + [""]
    if flags:
        out += ["Individual signals:", ""] + [f"- {_md(f)}" for f in flags] + [""]
    session = [f"started {_md(row.get(S.TEST_START))}" if _clean(row.get(S.TEST_START)) else "",
               f"submitted {_md(row.get(S.SUBMISSION_TIME))}" if _clean(row.get(S.SUBMISSION_TIME)) else "",
               f"{_fmt(_float(row.get(S.COMPLETION_MIN)))} min" if _float(row.get(S.COMPLETION_MIN)) is not None else "",
               _md(row.get(S.TEST_MODE))]
    if any(session):
        out += [f"Assessment session: {', '.join(s for s in session if s)}.", ""]

    links = _links_for(_frame(risk.pairs if risk else None), cid)
    themes: list[str] = []
    if not links.empty:
        shown = links.head(_MAX_LINKS)
        out += ["## Linked candidates", "", "| Candidate | Name | College | Pair score | Signals |",
                "|---|---|---|---|---|"]
        for rec in shown.to_dict("records"):
            o = _clean(rec["other"])
            out.append(f"| {_md(o)} | {_md(names.get(o)) or '-'} | {_md(colleges.get(o)) or '-'} | "
                       f"{_fmt(_float(rec.get('pair_score')), 0)} | {_md(rec.get('signals'))} |")
        if len(links) > _MAX_LINKS:
            out.append(f"\n_{len(links) - _MAX_LINKS} more linked candidates are listed in the Excel report._")
        out += ["", "## Shared evidence", ""]
        for rec in shown.to_dict("records"):
            o = _clean(rec["other"])
            lines, link_themes = _link_details(rec, cfg)
            themes += link_themes
            out.append(f"### With {_md(o)} ({_md(names.get(o)) or 'name unknown'}) - pair score "
                       f"{_fmt(_float(rec.get('pair_score')), 0)}")
            out.append("")
            out += [f"- {line}" for line in lines] or [f"- {_md(rec.get('signals')) or 'See signals above'}"]
            if link_themes:
                out.append(f"- Pattern consistent with: {', '.join(link_themes)}")
            out.append("")

    cluster_id = _clean(row.get(S.CLUSTER_ID))
    clusters = _frame(risk.clusters if risk else None)
    if cluster_id and "cluster_id" in clusters.columns:
        hit = clusters.loc[clusters["cluster_id"].map(_clean) == cluster_id]
        if not hit.empty:
            c = hit.iloc[0].to_dict()
            out += [f"## Cluster {_md(cluster_id)}", "",
                    f"- Members ({_fmt(_float(c.get('size')), 0) or '?'}): {_md(c.get('members'))}",
                    f"- Shared traits: {_md(c.get('shared_traits')) or '-'}",
                    f"- Summary: {_md(c.get('summary')) or '-'}", ""]

    unique = list(dict.fromkeys(themes))
    hashes = sorted({h.strip() for v in _col(links, "shared_hashes") for h in _clean(v).split(",") if h.strip()})
    repos = sorted({g.strip() for v in _col(links, "shared_github") for g in _clean(v).split(",") if g.strip()})
    steps = [
        "Live coding re-test (30-45 min, proctored): ask the candidate to solve and talk through problems "
        + (f"equivalent to {_md(', '.join(hashes))}." if hashes else "of similar difficulty to the assessment."),
        "Project walkthrough: ask the candidate to explain the design, trade-offs and their individual "
        "contribution for the projects on their resume"
        + (f" (including {', '.join(_md(r) for r in repos)})." if repos else "."),
        "Proctored re-assessment with identity verification (photo ID, camera on) if the pattern is "
        "not explained by the steps above.",
    ]
    if any(t in unique for t in ("a shared test location or network", "synchronised test sessions",
                                 "one person taking both sessions")):
        steps.append("Check with the placement cell whether the linked candidates were in the same lab or "
                     "scheduled session - a shared network or slot is a legitimate explanation.")
    if "duplicate registration" in unique:
        steps.append("Confirm contact details and identity documents for the linked registrations.")
    steps.append("Apply the same steps to every linked candidate, and record the outcome and the "
                 "candidate's explanation before any decision.")
    out += ["## Recommended verification steps", ""] + [f"{i}. {s}" for i, s in enumerate(steps, 1)]
    out += ["", "> These observations describe a pattern consistent with "
            + (", ".join(unique) if unique else "the risk signals listed above")
            + ". They are not proof of misconduct and may have legitimate explanations; decide only "
              "after verification and in line with the hiring policy.", ""]
    return "\n".join(out)
