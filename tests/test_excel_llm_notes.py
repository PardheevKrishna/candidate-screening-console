"""LLM rationales and summaries must reach the Excel report."""
import io

import openpyxl

from screening import export
from tests.ui_fixtures import make_fake_prepared_and_result


def test_llm_outputs_reach_excel_report():
    _, result = make_fake_prepared_and_result()
    cid = str(result.candidates.iloc[0]["candidate_id"])
    cluster = str(result.risk.clusters.iloc[0]["cluster_id"])
    notes = {
        "candidates": {cid: {"ok": True, "summary": "UNIQUE-RATIONALE-TEXT", "strengths": ["s"], "gaps": ["g"]}},
        "clusters": {cluster: {"ok": True, "headline": "UNIQUE-CLUSTER-HEADLINE", "summary": "x",
                               "recommended_actions": ["verify"]}},
        "executive_summary": {"ok": True, "markdown": "## Overview\n- UNIQUE-EXEC-SUMMARY"},
    }
    wb = openpyxl.load_workbook(io.BytesIO(export.build_excel_report(result, llm_notes=notes)), read_only=True)
    text = " ".join(str(c) for ws in wb.worksheets for row in ws.iter_rows(values_only=True) for c in row if c)
    for marker in ("UNIQUE-RATIONALE-TEXT", "UNIQUE-CLUSTER-HEADLINE", "UNIQUE-EXEC-SUMMARY"):
        assert marker in text, marker
