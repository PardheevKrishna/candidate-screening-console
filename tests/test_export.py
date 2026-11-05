"""Tests for screening.export: CSV, Excel workbook and markdown evidence packs."""
from __future__ import annotations

import csv
import io

import numpy as np
import openpyxl
import pandas as pd
import pytest

from screening import export
from screening import schema as S
from screening.config import AppConfig
from screening.models import DataQualityReport, PipelineResult, RiskResult

INJECTED = "=cmd|' /C calc'!A0"


def _candidate(cid, name, status, rank, final, level="Clear", reasons="", cluster="", **extra):
    row = {
        S.RANK: rank, S.STATUS: status, S.STATUS_REASON: f"{status} reason", S.CANDIDATE_ID: cid,
        S.NAME: name, S.EMAIL: f"{cid.lower()}@example.com", S.PHONE: "9876543210",
        S.COLLEGE: "Alpha Institute", S.COLLEGE_TIER: "2", S.DEGREE: "B.Tech", S.BRANCH: "CSE",
        S.CGPA: 8.6, S.FINAL_SCORE: final, S.PTS_HR: 40.0, S.PTS_SKILLS: 30.0, S.PTS_EXPERIENCE: 15.4,
        S.PTS_EDUCATION: 7.9, S.SUB_HR: 1.0, S.SUB_SKILLS: 1.0, S.SUB_EXPERIENCE: 0.77,
        S.SUB_EDUCATION: 0.79, S.HR_SCORE: 100.0, S.MAX_SCORE: 100.0, S.HR_PCT: 100.0,
        S.RISK_LEVEL: level, S.RISK_SCORE: 85.0 if level == "High" else 0.0, S.RISK_REASONS: reasons,
        S.CLUSTER_ID: cluster, S.IS_FOCUS: True, S.MATCHED_SKILLS: "Python, SQL, Cloud",
        S.MISSING_SKILLS: "Java, PyTorch", S.INTERNSHIP_COUNT: 2, S.EXPERIENCE_MONTHS: 5.0,
        S.PROJECT_COUNT: 3, S.CERT_COUNT: 1, S.RELEVANCE: 0.72, S.EXPERIENCE_SOURCE: S.EXP_HEURISTIC,
        S.LLM_RATIONALE: "", S.SCORE_NOTES: "", S.TEST_START: pd.Timestamp("2026-10-12 10:00:05"),
        S.SUBMISSION_TIME: pd.Timestamp("2026-10-12 10:58:11"), S.COMPLETION_MIN: 58.1,
        S.TEST_MODE: "Remote", S.HAS_RESUME: True, S.ELIGIBLE: rank is not None,
    }
    row.update(extra)
    return row


def make_result(risk: RiskResult | None = None) -> PipelineResult:
    candidates = pd.DataFrame([
        _candidate("CAND-001", "Asha Menon", S.STATUS_REVIEW, 1, 95.5, "High",
                   "Identical answers to q2, q3 with CAND-002 | Same IP 49.36.12.7 with CAND-002", "C01"),
        _candidate("CAND-002", "Ravi Kumar", S.STATUS_REVIEW, 2, 95.0, "High",
                   "Identical answers to q2, q3 with CAND-001", "C01"),
        _candidate("CAND-003", INJECTED, S.STATUS_SHORTLISTED, 3, 90.0, extra_note="+SUM(A1:A9)"),
        _candidate("CAND-004", "Meera Iyer", S.STATUS_NOT, 4, 60.0, "Medium", "Same /24 subnet as CAND-009"),
        _candidate("CAND-005", "Kiran Das", S.STATUS_INELIGIBLE, None, 30.0,
                   **{S.HR_PCT: np.nan, S.HR_SCORE: np.nan}),
    ])
    candidates[S.RANK] = pd.array(candidates[S.RANK], dtype="Int64")
    if risk is None:
        risk = RiskResult(
            focus_ids=["CAND-001", "CAND-002", "CAND-003", "CAND-004"],
            pairs=pd.DataFrame([{
                "cand_a": "CAND-001", "cand_b": "CAND-002", "pair_score": 85.0,
                "signals": "Identical answers to q2, q3 | Same IP 49.36.12.7 | Submissions 40 s apart",
                "signal_keys": "shared_answer_hash, same_ip, sync_submission", "shared_hashes": "q2, q3",
                "resume_similarity": 0.86, "shared_bullets": 2,
                "shared_bullet_examples": "Built a fraud detection model with 94% recall | "
                                          "Reduced API latency by 40% using Redis caching",
                "shared_github": "github.com/asha/fraud-detector", "same_ip": True, "same_subnet": True,
                "same_college": True, "same_phone": False, "same_email": False, "start_gap_sec": 30.0,
                "submit_gap_sec": 40.0, "duration_gap_min": 0.2, "sequential_gap_min": np.nan,
            }]),
            clusters=pd.DataFrame([{
                "cluster_id": "C01", "size": 2, "members": "CAND-001, CAND-002",
                "member_names": "Asha Menon, Ravi Kumar", "risk_level": "High", "max_pair_score": 85.0,
                "mean_pair_score": 85.0, "density": 1.0, "top_signals": "Identical answers, Same IP",
                "shared_traits": "College: Alpha Institute (2/2) | IP 49.36.12.7 (2/2)",
                "colleges": "Alpha Institute", "n_colleges": 1, "start_span_min": 0.5,
                "submit_span_min": 0.7, "summary": "2 perfect scorers share identical answers to q2, q3.",
            }]),
            commonality=pd.DataFrame([{
                "attribute": "College", "value": "Alpha Institute", "focus_count": 4, "focus_total": 4,
                "focus_share": 1.0, "population_count": 5, "population_total": 5,
                "population_share": 1.0, "lift": 1.0, "p_value": 1.0, "q_value": 1.0, "significant": False,
            }]),
            individual=pd.DataFrame([{
                S.CANDIDATE_ID: "CAND-001", "flag_key": "speed_anomaly", "flag": "Speed anomaly",
                "detail": "Completed in 12 min vs focus median 48 min", "weight": 25.0,
            }]),
        )
    quality = DataQualityReport(column_mapping={"Candidate ID": S.CANDIDATE_ID, "Score": S.HR_SCORE},
                                rows_raw=6, rows_clean=5, duplicates_dropped=1)
    quality.add("warning", "ingest", "Duplicate candidate_id dropped", "CAND-003")
    quality.add("error", "parse", "Unreadable file", file_name="=bad.pdf")
    return PipelineResult(candidates=candidates, parsed_resumes={}, parsed_by_candidate={},
                          matches=pd.DataFrame(), risk=risk, quality=quality, config=AppConfig(),
                          timings={"score": 0.21, "risk": 0.5}, mode="ta+resumes")


def load(data: bytes) -> openpyxl.Workbook:
    return openpyxl.load_workbook(io.BytesIO(data))


def sheet_rows(ws) -> list[dict]:
    rows = list(ws.iter_rows(values_only=True))
    return [dict(zip(rows[0], r)) for r in rows[1:]]


# ---------------------------------------------------------------------------
# CSV + sanitising
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value, expected", [
    ("=1+2", "'=1+2"), ("+SUM(A1)", "'+SUM(A1)"), ("-2+3", "'-2+3"), ("@cmd", "'@cmd"),
    ("\t=x", "'\t=x"), ("\r=x", "'\r=x"), ("-5", "-5"), ("+919876543210", "+919876543210"),
    ("plain", "plain"), ("", ""), (3.5, 3.5), (None, None),
])
def test_sanitize_cell(value, expected):
    assert export.sanitize_cell(value) == expected


def test_to_csv_bytes_has_bom_and_sanitises_cells_and_headers():
    df = pd.DataFrame({"=evil": ["=1+2", "-5", None], "name": ["@x", "Asha", "-2+3"], "score": [1.5, 2.0, np.nan]})
    data = export.to_csv_bytes(df)
    assert data.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
    assert rows[0] == ["'=evil", "name", "score"]
    assert rows[1] == ["'=1+2", "'@x", "1.5"]
    assert rows[2] == ["-5", "Asha", "2.0"]
    assert rows[3] == ["", "'-2+3", ""]
    assert df.loc[0, "=evil"] == "=1+2"          # input untouched


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------
def test_excel_opens_with_expected_sheets_and_brand_header():
    wb = load(export.build_excel_report(make_result()))
    assert wb.sheetnames == export.SHEET_NAMES
    ws = wb["Shortlist"]
    header = ws["A1"]
    assert header.value == "Rank"
    assert header.fill.fgColor.rgb.endswith("D71E28")
    assert header.font.bold and header.font.color.rgb.endswith("FFFFFF")
    assert ws.freeze_panes == "D2"
    assert ws.auto_filter.ref.startswith("A1:")
    assert wb["All Candidates"].freeze_panes == "A2"


def test_excel_shortlist_rows_and_number_formats():
    wb = load(export.build_excel_report(make_result()))
    rows = sheet_rows(wb["Shortlist"])
    assert [r["Candidate ID"] for r in rows] == ["CAND-001", "CAND-002", "CAND-003"]
    assert rows[0]["Status"] == S.STATUS_REVIEW and rows[0]["Final Score"] == 95.5
    ws = wb["Shortlist"]
    headers = [c.value for c in ws[1]]
    assert ws.cell(2, headers.index("Final Score") + 1).number_format == "0.00"
    all_rows = sheet_rows(wb["All Candidates"])
    assert len(all_rows) == 5
    assert all_rows[0]["Test Start Time"].year == 2026          # written as a real datetime
    assert all_rows[4]["Rank"] is None                          # ineligible: blank rank


def test_excel_conditional_formats_for_risk_and_status():
    ws = load(export.build_excel_report(make_result()))["All Candidates"]
    rules = {}
    for rng in ws.conditional_formatting:
        for rule in rng.rules:
            rules[rule.formula[0]] = rule.dxf.fill.bgColor.rgb
    assert rules['"High"'].endswith("F8D7DA")
    assert rules['"Medium"'].endswith("FFF3CD")
    assert f'"{S.STATUS_REVIEW}"' in rules and f'"{S.STATUS_HOLD}"' in rules


def test_excel_formula_injection_is_neutralised():
    wb = load(export.build_excel_report(make_result()))
    rows = sheet_rows(wb["All Candidates"])
    injected = next(r for r in rows if r["Candidate ID"] == "CAND-003")
    assert injected["Name"] == "'" + INJECTED
    assert injected["Extra Note"] == "'+SUM(A1:A9)"
    ws = wb["All Candidates"]
    assert all(cell.data_type != "f" for row in ws.iter_rows() for cell in row)
    issues = sheet_rows(wb["Data Quality"])
    assert issues[0]["Severity"] == "error" and issues[0]["File Name"] == "'=bad.pdf"


def test_excel_integrity_evidence_and_risk_sheets():
    wb = load(export.build_excel_report(make_result()))
    evidence = sheet_rows(wb["Integrity Evidence"])
    assert [r["Candidate ID"] for r in evidence] == ["CAND-001", "CAND-002", "CAND-004"]
    first = evidence[0]
    assert first["Linked Candidates"] == "CAND-002"
    assert first["Link Evidence (pair score: signals)"].startswith("CAND-002 (Ravi Kumar; pair score 85)")
    assert "Speed anomaly" in first["Individual Flags"]
    assert first["Risk Reasons"].splitlines()[0] == "Identical answers to q2, q3 with CAND-002"
    pairs = sheet_rows(wb["Risk Pairs"])
    assert pairs[0]["Name A"] == "Asha Menon" and pairs[0]["Name B"] == "Ravi Kumar"
    assert sheet_rows(wb["Risk Clusters"])[0]["Cluster"] == "C01"
    assert sheet_rows(wb["Commonality"])[0]["Attribute"] == "College"


def test_excel_summary_and_config():
    wb = load(export.build_excel_report(make_result()))
    summary = {r[0]: r[1] for r in wb["Summary"].iter_rows(values_only=True) if r[0]}
    texts = " ".join(str(v) for row in wb["Summary"].iter_rows(values_only=True) for v in row if v)
    assert "evidence for human review" in texts and "Generated" in texts
    assert summary["Candidates processed"] == 5
    assert summary["Shortlisted (total)"] == 3
    assert summary["Risk flags - High"] == 2
    assert summary["Perfect scorers (risk focus group)"] == 4
    config = {r[0]: r[1] for r in wb["Config"].iter_rows(min_row=2, values_only=True)}
    assert config["weights.hackerrank"] == 40
    assert config["shortlist.top_n"] == 300
    assert config["skills.required_skills"] == '["Python", "Java", "SQL", "Cloud", "PyTorch"]'


def test_excel_handles_empty_risk_and_llm_notes():
    notes = {
        "executive_summary": {"ok": True, "markdown": "# Summary\n- 3 candidates shortlisted"},
        "clusters": {"C01": {"headline": "Shared answers", "summary": "s", "recommended_actions": ["a", "b"]}},
        "candidates": {"CAND-001": {"summary": "Strong backend projects", "strengths": ["APIs"], "gaps": []}},
    }
    wb = load(export.build_excel_report(make_result(RiskResult()), llm_notes=notes))
    assert wb.sheetnames == export.SHEET_NAMES
    texts = [v for row in wb["Summary"].iter_rows(values_only=True) for v in row if v]
    assert "• 3 candidates shortlisted" in texts and "Summary" in texts
    shortlist = sheet_rows(wb["Shortlist"])
    assert shortlist[0]["AI Notes (LLM)"] == "Strong backend projects Strengths: APIs"
    assert wb["Risk Pairs"]["A1"].value == "No data available for this run."


# ---------------------------------------------------------------------------
# Evidence pack
# ---------------------------------------------------------------------------
def test_evidence_pack_for_flagged_candidate():
    md = export.build_evidence_pack(make_result(), "CAND-001")
    for text in ("CAND-001", "Asha Menon", "Alpha Institute", "High risk", "CAND-002", "Ravi Kumar",
                 "q2, q3", "github.com/asha/fraud-detector", "Speed anomaly", "Live coding re-test",
                 "Project walkthrough", "Proctored re-assessment", "HackerRank: 100/100 (100%)",
                 "Cluster C01", "Reduced API latency by 40% using Redis caching"):
        assert text in md, text
    assert "pattern consistent with" in md.lower()
    assert "Identical answers to q2, q3 \\| Same IP" in md          # pipes escaped inside the table
    assert "cand-001@example.com" not in md and "9876543210" not in md
    assert not any(word in md.lower() for word in ("cheat", "guilty", "dishonest"))


def test_evidence_pack_clear_and_unknown_candidates():
    clear = export.build_evidence_pack(make_result(), "cand-003")    # case-insensitive lookup
    assert "No integrity flags were raised" in clear
    assert "Recommended verification steps" not in clear
    missing = export.build_evidence_pack(make_result(), "NOPE-1")
    assert "was not found" in missing
