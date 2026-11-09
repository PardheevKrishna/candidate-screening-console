"""LLM explanations and summaries must reach the Excel report - as notes only, never as scores."""
import io

import openpyxl

from screening import export
from screening import schema as S
from tests.ui_fixtures import make_fake_prepared_and_result


def test_llm_outputs_reach_excel_report():
    _, result = make_fake_prepared_and_result()
    candidates = result.candidates
    cid = str(candidates.iloc[0][S.CANDIDATE_ID])
    borderline = str(candidates.loc[candidates[S.STATUS] == S.STATUS_NOT, S.CANDIDATE_ID].iloc[0])
    cluster = str(result.risk.clusters.iloc[0]["cluster_id"])
    notes = {
        "candidates": {cid: {"ok": True, "summary": "UNIQUE-RATIONALE-TEXT", "strengths": ["s"], "gaps": ["g"]}},
        "borderline": {borderline: {"ok": True, "summary": "UNIQUE-BORDERLINE-TEXT", "gaps": ["depth"]}},
        "clusters": {cluster: {"ok": True, "headline": "UNIQUE-CLUSTER-HEADLINE", "summary": "x",
                               "recommended_actions": ["verify"]}},
        "executive_summary": {"ok": True, "markdown": "## Overview\n- UNIQUE-EXEC-SUMMARY"},
    }
    wb = openpyxl.load_workbook(io.BytesIO(export.build_excel_report(result, llm_notes=notes)), read_only=True)
    text = " ".join(str(c) for ws in wb.worksheets for row in ws.iter_rows(values_only=True) for c in row if c)
    for marker in ("UNIQUE-RATIONALE-TEXT", "UNIQUE-BORDERLINE-TEXT", "UNIQUE-CLUSTER-HEADLINE",
                   "UNIQUE-EXEC-SUMMARY"):
        assert marker in text, marker

    rows = list(wb["All Candidates"].iter_rows(min_row=2, values_only=True))
    header = list(rows[0])
    by_id = {r[header.index("Candidate ID")]: r for r in rows[1:]}
    assert by_id[borderline][header.index("AI Notes (LLM)")].startswith("Near the cutoff: UNIQUE-BORDERLINE-TEXT")
    expected = dict(zip(candidates[S.CANDIDATE_ID], candidates[S.FINAL_SCORE]))
    for candidate_id, row in by_id.items():  # notes never change a score
        assert row[header.index("Final Score")] == expected[candidate_id]
