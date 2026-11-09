"""Compare Candidates tab and the Individual Deep-Dive "Add to compare" button.

The Compare tab is rendered through a small page that mirrors how app.py renders a tab, so these
tests do not depend on which optional tabs app.py has switched on; the Deep-Dive button is tested
through app.py itself. The engine is faked (tests/ui_fixtures.py); no network, proxy or token is used.
"""
from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import pytest
from streamlit.testing.v1 import AppTest

from screening import export, llm_backends, pipeline
from screening import schema as S
from screening.config import AppConfig
from screening.models import DataQualityReport, PipelineResult, RiskResult
from ui import state, tab_compare, tab_deepdive, theme
from ui.charts import SHORTLIST_STATUSES
from ui.tab_shortlist import csv_bytes

try:
    from tests.ui_fixtures import make_fake_prepared_and_result
except ImportError:  # pragma: no cover - tests/ imported as rootdir-relative modules
    from ui_fixtures import make_fake_prepared_and_result

APP = str(Path(__file__).resolve().parent.parent / "app.py")
FP = "fake-fingerprint"
KEY_ENV = AppConfig().llm.api_key_env  # env var / secret name of the proxy token, from the config
PAGE = "_compare_test_page"
# Outcome rows first, then one group per data source with the source as a label prefix.
SPEC_ROWS = ["Rank", "Status", "Final score", "Ineligibility", "Tie-break note",
             "HackerRank: Percentage", "HackerRank: Rubric pts",
             "Resume: Skills pts", "Resume: Experience pts", "Resume: Required skills matched", "Resume: Internships",
             "Resume: Experience months", "Resume: Projects", "Resume: Certifications", "Resume: Match method",
             "Academic: Education pts", "Academic: Degree", "Academic: Branch", "Academic: CGPA",
             "Academic: College tier",
             "Integrity: Risk level", "Integrity: Risk score", "Integrity: Cluster", "Integrity: Duplicate of",
             "Integrity: Merged duplicates", "Integrity: Hidden-text flags"]
# Rows that only exist for results carrying the screening-rule columns (runs saved earlier lack them).
RULE_ROW_COLUMNS = {"Ineligibility": S.INELIGIBILITY, "Tie-break note": S.TIE_BREAK_NOTE,
                    "Integrity: Duplicate of": S.DUPLICATE_OF, "Integrity: Merged duplicates": S.DUPLICATE_IDS,
                    "Integrity: Hidden-text flags": S.INJECTION_FLAGS}


def _spec_rows(df: pd.DataFrame) -> list[str]:
    return [r for r in SPEC_ROWS if RULE_ROW_COLUMNS.get(r, S.CANDIDATE_ID) in df.columns]


def _page() -> None:
    """The Compare tab (or the Deep-Dive) rendered the way app.py renders the active tab."""
    import streamlit as st

    from ui import state, tab_compare, tab_deepdive, theme

    theme.inject_css()
    state.init_state()
    result = state.current_result()
    state.flush_messages()
    if st.session_state.get("_compare_test_page") == "deepdive":
        tab_deepdive.render(result)
    else:
        tab_compare.render(result)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(KEY_ENV, raising=False)
    monkeypatch.setitem(vars(pipeline), "DEFAULT_SAMPLE_DIR", tmp_path / "no-sample")
    monkeypatch.setitem(vars(llm_backends), "discover_proxy", lambda path=None: None)  # ignore a running proxy


@pytest.fixture(scope="module")
def fake() -> tuple[pipeline.PreparedData, PipelineResult]:
    return make_fake_prepared_and_result()


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch, fake: tuple[pipeline.PreparedData, PipelineResult]) -> dict[str, Any]:
    """Fake evaluate (records calls) and evidence pack; the real CSV export is used."""
    prepared, result = fake
    calls: list[Any] = []

    def evaluate(prep, cfg, *, risk_result=None):
        calls.append(cfg)
        return replace(result, config=cfg)

    monkeypatch.setitem(vars(pipeline), "evaluate", evaluate)
    monkeypatch.setitem(vars(export), "build_evidence_pack", lambda res, cid: f"# Evidence pack {cid}\n")
    return {"prepared": prepared, "result": result, "calls": calls}


def _inject(at: AppTest, prepared: Any, result: PipelineResult, **values: Any) -> AppTest:
    at.secrets[KEY_ENV] = ""  # never read a real secrets.toml during tests
    at.session_state[state.K_CONFIG] = result.config
    at.session_state[state.K_RESULT] = result
    if prepared is not None:
        at.session_state[state.K_PREPARED] = prepared
        at.session_state[state.K_PREPARED_FP] = FP
        at.session_state[state.K_RESULT_KEY] = state.eval_key(FP, result.config)
    at.session_state[state.K_DATASET] = "Fake dataset"
    for key, value in values.items():
        at.session_state[key] = value
    return at


def _compare_app(engine: dict[str, Any], **values: Any) -> AppTest:
    return _inject(AppTest.from_function(_page, default_timeout=60), engine["prepared"], engine["result"], **values)


def _assert_clean(at: AppTest) -> None:
    assert not at.exception, [e.value for e in at.exception]
    assert not at.error, [e.value for e in at.error]


def _frame(at: AppTest, first_column: str) -> pd.DataFrame:
    return _element(at, first_column).value


def _element(at: AppTest, first_column: str) -> Any:
    for element in at.dataframe:
        if list(element.value.columns)[:1] == [first_column]:
            return element
    raise AssertionError(f"no dataframe starting with {first_column!r}")


def _ids(frame: pd.DataFrame) -> list[str]:
    return [str(c).split(" · ")[0] for c in frame.columns[1:]]


def _top_shortlisted(df: pd.DataFrame, n: int = 3) -> list[str]:
    short = df[df[S.STATUS].isin(SHORTLIST_STATUSES)].sort_values(S.RANK)
    return short[S.CANDIDATE_ID].head(n).tolist()


def _row(frame: pd.DataFrame, metric: str) -> list[Any]:
    return frame.loc[frame[tab_compare.METRIC] == metric].iloc[0, 1:].tolist()


# ---------------------------------------------------------------------------
# Compare tab (AppTest)
# ---------------------------------------------------------------------------
def test_compare_defaults_to_top_three_shortlisted(engine: dict[str, Any]) -> None:
    at = _compare_app(engine).run()
    _assert_clean(at)
    result = engine["result"]
    expected = _top_shortlisted(result.candidates)
    picker = at.multiselect(key=tab_compare.K_SELECT)
    assert picker.label == "Candidates to compare (2-4)"
    assert picker.value == expected
    assert at.session_state[state.K_COMPARE_IDS] == expected

    table = _frame(at, tab_compare.METRIC)
    assert table[tab_compare.METRIC].tolist() == _spec_rows(result.candidates)
    assert _ids(table) == expected
    assert _row(table, "Rank") == ["#1", "#2", "#3"]
    assert "FFF4C2" in str(_element(at, tab_compare.METRIC).proto)  # best-value highlight reaches the grid

    matrix = _frame(at, tab_compare.SKILL)
    assert matrix[tab_compare.SKILL].tolist() == result.config.skills.required_skills
    assert set(matrix.iloc[:, 1:].stack()) <= {tab_compare.MATCHED_MARK, tab_compare.MISSING_MARK}

    cards = "\n".join(h.value for h in at.html)
    assert all(cid in cards for cid in expected)
    assert [b.key for b in at.button] == [f"cmp_open_{cid}" for cid in expected]
    assert [m.value for m in at.metric if m.label == "Rank"] == ["#1", "#2", "#3"]

    charts = at.get("plotly_chart")
    assert len(charts) == 1
    traces = json.loads(charts[0].proto.spec)["data"]
    assert [t["line"]["color"] for t in traces] == list(tab_compare.RADAR_COLORS[:3])
    assert [e.label for e in at.expander] == [theme.md_escape(c) for c in table.columns[1:]]
    assert at.download_button(key="cmp_dl_csv").label == "Download comparison (CSV)"
    assert engine["calls"] == []  # rendering never re-scores


def test_compare_with_explicit_ids(engine: dict[str, Any]) -> None:
    # CAND-00060: no HackerRank score and no resume (ineligible); unknown ids and duplicates are dropped.
    at = _compare_app(engine, **{state.K_COMPARE_IDS: ["CAND-00020", "NOT-A-CANDIDATE", "CAND-00060",
                                                       "CAND-00020"]}).run()
    _assert_clean(at)
    assert at.multiselect(key=tab_compare.K_SELECT).value == ["CAND-00020", "CAND-00060"]
    assert at.session_state[state.K_COMPARE_IDS] == ["CAND-00020", "CAND-00060"]
    table = _frame(at, tab_compare.METRIC)
    assert _ids(table) == ["CAND-00020", "CAND-00060"]
    assert _row(table, "Rank")[1] == "-" and _row(table, "HackerRank: Percentage")[1] == "-"
    assert _row(table, "Resume: Match method")[1] == "no resume"
    assert _row(table, "Integrity: Cluster")[0] == "C02"
    if S.INELIGIBILITY in engine["result"].candidates.columns:   # no assessment score -> Ineligible
        assert _row(table, "Ineligibility") == ["-", S.RULE_NO_SCORE]
    assert any("No resume is linked" in c.value for c in at.caption)
    assert len(json.loads(at.get("plotly_chart")[0].proto.spec)["data"]) == 2


def test_compare_needs_two_candidates_and_keeps_a_cleared_selection(engine: dict[str, Any]) -> None:
    at = _compare_app(engine, **{state.K_COMPARE_IDS: ["CAND-00001"]}).run()
    _assert_clean(at)
    assert any("at least 2 candidates" in i.value for i in at.info)
    assert not at.dataframe and not at.get("plotly_chart")
    at.multiselect(key=tab_compare.K_SELECT).set_value([]).run()
    at.run()  # a cleared selection is not refilled with the defaults
    _assert_clean(at)
    assert at.session_state[state.K_COMPARE_IDS] == []
    assert at.multiselect(key=tab_compare.K_SELECT).value == []


def test_multiselect_changes_are_kept_in_session(engine: dict[str, Any]) -> None:
    at = _compare_app(engine).run()
    at.multiselect(key=tab_compare.K_SELECT).set_value(["CAND-00045", "CAND-00003"]).run()
    _assert_clean(at)
    assert at.session_state[state.K_COMPARE_IDS] == ["CAND-00045", "CAND-00003"]
    assert _ids(_frame(at, tab_compare.METRIC)) == ["CAND-00045", "CAND-00003"]
    at.run()
    assert at.multiselect(key=tab_compare.K_SELECT).value == ["CAND-00045", "CAND-00003"]


def test_stored_ids_are_capped_and_stale_lists_fall_back_to_defaults(engine: dict[str, Any]) -> None:
    five = ["CAND-00001", "CAND-00002", "CAND-00003", "CAND-00004", "CAND-00005"]
    at = _compare_app(engine, **{state.K_COMPARE_IDS: five}).run()
    _assert_clean(at)
    assert at.multiselect(key=tab_compare.K_SELECT).value == five[:tab_deepdive.COMPARE_MAX]
    at.session_state[state.K_COMPARE_IDS] = ["OLD-00001", "OLD-00002"]  # ids from an earlier dataset
    at.run()
    _assert_clean(at)
    assert at.multiselect(key=tab_compare.K_SELECT).value == _top_shortlisted(engine["result"].candidates)


def test_open_profile_points_the_deep_dive_at_the_candidate(engine: dict[str, Any]) -> None:
    at = _compare_app(engine, **{state.K_COMPARE_IDS: ["CAND-00020", "CAND-00003"]}).run()
    at.button(key="cmp_open_CAND-00003").click().run()
    _assert_clean(at)
    assert at.session_state[state.K_DD_CANDIDATE] == "CAND-00003"
    assert at.session_state[state.K_TABS] == state.TAB_DEEPDIVE


def test_compare_tolerates_minimal_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(vars(export), "to_csv_bytes", raising=False)  # behave like an unimplemented engine
    minimal = pd.DataFrame({S.CANDIDATE_ID: ["A-1", "A-2", "A-3"], S.NAME: ["Candidate X", "Candidate Y", None],
                            S.FINAL_SCORE: [81.0, 64.5, None], S.STATUS: [S.STATUS_SHORTLISTED, S.STATUS_NOT, ""]})
    result = PipelineResult(candidates=minimal, parsed_resumes={}, parsed_by_candidate={}, matches=pd.DataFrame(),
                            risk=RiskResult(), quality=DataQualityReport(), config=AppConfig())
    at = _inject(AppTest.from_function(_page, default_timeout=60), None, result).run()
    _assert_clean(at)
    assert at.multiselect(key=tab_compare.K_SELECT).value == ["A-1", "A-2", "A-3"]  # fewer than 2 shortlisted
    table = _frame(at, tab_compare.METRIC)
    assert table[tab_compare.METRIC].tolist() == ["Status", "Final score"]
    assert _row(table, "Final score") == ["81.0", "64.5", "-"]
    assert any("not available" in c.value for c in at.caption)
    assert any("No resume is linked" in c.value for c in at.caption)


def test_compare_without_result_shows_info() -> None:
    at = AppTest.from_function(_page, default_timeout=60)
    at.secrets[KEY_ENV] = ""
    at.run()
    _assert_clean(at)
    assert any("No candidates to compare" in i.value for i in at.info)


# ---------------------------------------------------------------------------
# Deep-Dive "Add to compare"
# ---------------------------------------------------------------------------
def test_deepdive_add_to_compare_appends_and_caps_at_four(engine: dict[str, Any]) -> None:
    at = _inject(AppTest.from_file(APP, default_timeout=60), engine["prepared"], engine["result"],
                 **{state.K_TABS: state.TAB_DEEPDIVE, state.K_DD_CANDIDATE: "CAND-00011",
                    state.K_COMPARE_IDS: ["OLD-00001"]}).run()
    _assert_clean(at)
    button = at.button(key="dd_add_compare")
    assert button.label == "Add to compare" and not button.disabled
    button.click().run()
    _assert_clean(at)
    assert at.session_state[state.K_COMPARE_IDS] == ["CAND-00011"]  # the stale id did not use up a slot
    assert at.button(key="dd_add_compare").label == "In compare list"
    assert at.button(key="dd_add_compare").disabled
    assert any("Added CAND-00011" in t.value for t in at.toast)

    added = ["CAND-00011"]
    for cid in ("CAND-00003", "CAND-00020", "CAND-00045"):
        at.selectbox(key=state.K_DD_CANDIDATE).set_value(cid).run()
        at.button(key="dd_add_compare").click().run()
        added.append(cid)
        assert at.session_state[state.K_COMPARE_IDS] == added
    assert any("(4/4)" in t.value for t in at.toast)

    at.selectbox(key=state.K_DD_CANDIDATE).set_value("CAND-00024").run()
    at.button(key="dd_add_compare").click().run()
    _assert_clean(at)
    assert at.session_state[state.K_COMPARE_IDS] == added  # capped at 4
    assert any("already holds 4" in t.value for t in at.toast)
    assert at.button(key="dd_add_compare").label == "Add to compare"


def test_added_candidates_appear_in_compare(engine: dict[str, Any]) -> None:
    at = _compare_app(engine, **{PAGE: "deepdive", state.K_DD_CANDIDATE: "CAND-00045"}).run()
    at.button(key="dd_add_compare").click().run()
    at.selectbox(key=state.K_DD_CANDIDATE).set_value("CAND-00007").run()
    at.button(key="dd_add_compare").click().run()
    at.session_state[PAGE] = "compare"
    at.run()
    _assert_clean(at)
    assert at.multiselect(key=tab_compare.K_SELECT).value == ["CAND-00045", "CAND-00007"]
    assert _ids(_frame(at, tab_compare.METRIC)) == ["CAND-00045", "CAND-00007"]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def test_add_to_compare_helper() -> None:
    valid = {"A", "B", "C", "D", "E"}
    assert tab_deepdive.add_to_compare(None, "A", valid) == (["A"], "added")
    assert tab_deepdive.add_to_compare(["A", "A", "X", " B "], "C", valid) == (["A", "B", "C"], "added")
    assert tab_deepdive.add_to_compare(["A", "B"], "B", valid) == (["A", "B"], "present")
    assert tab_deepdive.add_to_compare(["A", "B", "C", "D"], "E", valid) == (["A", "B", "C", "D"], "full")
    assert tab_deepdive.add_to_compare(["A", "B", "C", "D", "E"], "E", valid)[1] == "full"  # over-long list capped
    assert tab_deepdive.clean_compare_ids("A, B") == []  # only lists are accepted


def test_resolve_selection_and_defaults(fake: tuple[Any, PipelineResult]) -> None:
    df = fake[1].candidates
    valid = set(df[S.CANDIDATE_ID])
    top = _top_shortlisted(df)
    assert tab_compare.default_compare_ids(df) == top
    assert tab_compare.resolve_selection(None, valid, top) == top
    assert tab_compare.resolve_selection([], valid, top) == []
    assert tab_compare.resolve_selection(["OLD-1"], valid, lambda: top) == top
    assert tab_compare.resolve_selection(["OLD-1", "CAND-00009"], valid, top) == ["CAND-00009"]
    nobody = df.assign(**{S.STATUS: S.STATUS_NOT})
    assert tab_compare.default_compare_ids(nobody) == df.sort_values(S.RANK)[S.CANDIDATE_ID].head(3).tolist()


def _small_frame() -> pd.DataFrame:
    return pd.DataFrame({
        S.CANDIDATE_ID: ["C-1", "C-2", "C-3"], S.NAME: ["Candidate A", "Candidate B", "Candidate C"],
        S.RANK: [2.0, 1.0, math.nan], S.STATUS: [S.STATUS_SHORTLISTED, S.STATUS_REVIEW, S.STATUS_INELIGIBLE],
        S.FINAL_SCORE: [80.04, 80.01, 55.0], S.HR_PCT: [99.6, 100.0, math.nan],
        S.PTS_SKILLS: [30.0, 30.0, 30.0], S.RISK_SCORE: [0.0, 62.0, 0.0],
        S.RISK_LEVEL: ["", S.RISK_HIGH, S.RISK_CLEAR], S.COLLEGE_TIER: ["1", "3", None],
        S.MATCHED_SKILLS: ["Python, Cloud", "SQL", ""], S.MISSING_SKILLS: ["SQL", "Python, Cloud", "Python, Cloud, SQL"],
        S.N_MATCHED_SKILLS: [2, 1, 0], S.CGPA: [8.1, math.nan, 9.2],
    })


def test_build_comparison_highlights_best_values() -> None:
    comparison = tab_compare.build_comparison(_small_frame(), ["C-1", "C-2", "C-3"], required_total=3)
    shown = comparison.display.set_index(tab_compare.METRIC)
    best = comparison.best.drop(columns=[tab_compare.METRIC]).set_index(comparison.display[tab_compare.METRIC])
    assert list(shown.index) == ["Rank", "Status", "Final score", "HackerRank: Percentage", "Resume: Skills pts",
                                 "Resume: Required skills matched", "Academic: CGPA", "Academic: College tier",
                                 "Integrity: Risk level", "Integrity: Risk score"]
    assert shown.loc["Rank"].tolist() == ["#2", "#1", "-"]
    assert best.loc["Rank"].tolist() == [False, True, False]                 # lower is better
    assert best.loc["Final score"].tolist() == [True, True, False]           # compared as shown: 80.0 == 80.0
    assert shown.loc["HackerRank: Percentage"].tolist() == ["99.6%", "100%", "-"]
    assert best.loc["HackerRank: Percentage"].tolist() == [False, True, False]
    assert not best.loc["Resume: Skills pts"].any()                          # all equal -> nothing marked
    assert shown.loc["Resume: Required skills matched"].tolist() == ["2 / 3", "1 / 3", "0 / 3"]
    assert best.loc["Resume: Required skills matched"].tolist() == [True, False, False]
    assert best.loc["Academic: CGPA"].tolist() == [False, False, True]       # missing values are skipped
    assert best.loc["Integrity: Risk score"].tolist() == [True, False, True]  # lower is better, ties all marked
    assert shown.loc["Academic: College tier"].tolist() == ["Tier 1", "Tier 3", "-"]
    text_rows = ["Status", "Academic: College tier", "Integrity: Risk level"]
    assert not best.loc[text_rows].to_numpy().any()                          # text rows are not ranked
    assert shown.loc["Integrity: Risk level"].tolist() == [theme.risk_label(S.RISK_CLEAR),
                                                           theme.risk_label(S.RISK_HIGH),
                                                           theme.risk_label(S.RISK_CLEAR)]
    raw = comparison.raw.set_index(tab_compare.METRIC)
    assert raw.loc["Rank"].tolist()[:2] == [2, 1] and raw.loc["Integrity: Risk level"].tolist()[0] == S.RISK_CLEAR


def test_metric_rows_are_grouped_by_source_with_prefixed_labels() -> None:
    rows = tab_compare.metric_rows(5)
    assert [m.label for m in rows] == SPEC_ROWS
    groups = [m.group for m in rows]
    assert groups == sorted(groups, key=tab_compare.SOURCE_ORDER.index)
    assert set(groups) == set(tab_compare.SOURCE_ORDER)
    for metric in rows:
        prefix = tab_compare.SOURCE_PREFIX.get(metric.group, "")
        assert metric.label.startswith(f"{prefix}: ") if prefix else ": " not in metric.label
    assert tab_compare.SOURCE_PREFIX[S.SRC_ASSESSMENT] == "HackerRank"


def test_rule_rows_show_ineligibility_tie_breaks_duplicates_and_hidden_text() -> None:
    rules = [{"rule_violated": S.RULE_NO_SCORE, "candidate_value": None, "threshold_value": None,
              "explanation": "No assessment score recorded."},
             {"rule_violated": S.RULE_MIN_CGPA, "candidate_value": 6.4, "threshold_value": 7.0,
              "explanation": "CGPA 6.40 is below the minimum 7.00."}]
    df = _small_frame().assign(**{
        S.INELIGIBILITY: ["[]", "[]", json.dumps(rules)],
        S.TIE_BREAK_NOTE: ["tied at 80.00 pts; lost tie-break on HackerRank percentage 99.6% vs 100%",
                           "tied at 80.00 pts; won tie-break on HackerRank percentage 100% vs 99.6%", ""],
        S.DUPLICATE_OF: ["", "", "C-1"], S.DUPLICATE_IDS: ["C-3", "", ""],
        S.INJECTION_FLAGS: ["", "Hidden text (page 1: white text): 'x' | Invisible characters (6) | +2 more", None],
        S.RESUME_CGPA: [7.9, 8.3, 9.0], S.CGPA_SOURCE: ["feed", "resume", "feed"],
    })
    comparison = tab_compare.build_comparison(df, ["C-1", "C-2", "C-3"], required_total=3)
    shown = comparison.display.set_index(tab_compare.METRIC)
    assert shown.loc["Ineligibility"].tolist() == ["-", "-", f"{S.RULE_NO_SCORE}, {S.RULE_MIN_CGPA}"]
    assert shown.loc["Tie-break note"].tolist()[1].startswith("tied at 80.00 pts; won")
    assert shown.loc["Integrity: Duplicate of"].tolist() == ["-", "-", "C-1"]
    assert shown.loc["Integrity: Merged duplicates"].tolist() == ["C-3", "-", "-"]
    assert shown.loc["Integrity: Hidden-text flags"].tolist() == ["None", "4 findings", "None"]
    assert shown.loc["Academic: CGPA"].tolist() == ["8.10", "8.30 (from resume)", "9.20"]
    raw = comparison.raw.set_index(tab_compare.METRIC)
    assert raw.loc["Ineligibility"].tolist()[2] == ("Assessment: No assessment score recorded.; "
                                                    "Academic CGPA: CGPA 6.40 is below the minimum 7.00.")
    assert not comparison.best.set_index(comparison.display[tab_compare.METRIC]).loc[
        ["Ineligibility", "Integrity: Hidden-text flags"]].drop(columns=[tab_compare.METRIC]).to_numpy().any()


def test_best_flags_edge_cases() -> None:
    nan = math.nan
    assert tab_compare.best_flags([1.0, 2.0], None) == [False, False]
    assert tab_compare.best_flags([nan, nan], "high") == [False, False]
    assert tab_compare.best_flags([3.0, nan], "high") == [True, False]
    assert tab_compare.best_flags([5.0, 5.0, 5.0], "low") == [False, False, False]
    assert tab_compare.best_flags([5.0, 5.0, nan], "low") == [True, True, False]


def test_skills_matrix_uses_the_matched_skills_column() -> None:
    df = _small_frame()
    cfg = AppConfig()
    cfg.skills.required_skills = ["python", "CLOUD", "sql"]
    required = tab_compare.required_skills(cfg, df, ["C-1", "C-2"])
    assert required == ["Python", "Cloud", "SQL"]  # canonical spelling, configured order
    matrix = tab_compare.skills_matrix(df, ["C-1", "C-2"], required)
    assert matrix[tab_compare.SKILL].tolist() == required
    # C-1 lists AWS but not "Cloud" on the resume; scoring recorded Cloud as matched (implied) - the matrix agrees.
    assert matrix["C-1 · Candidate A"].tolist() == [True, True, False]
    assert matrix["C-2 · Candidate B"].tolist() == [False, False, True]


def test_radar_overlays_selected_candidates_in_brand_colours() -> None:
    df = _small_frame().assign(**{S.SUB_HR: [1.0, 0.5, math.nan], S.SUB_SKILLS: [1.4, 0.2, 0.0],
                                  S.SUB_EXPERIENCE: [0.3, 0.9, 0.1], S.SUB_EDUCATION: [0.6, -0.2, 0.4]})
    fig = tab_compare.radar_figure(df, ["C-2", "C-1", "C-3"])
    assert isinstance(fig, go.Figure) and len(fig.data) == 3
    assert [t.line.color for t in fig.data] == ["#D71E28", "#FFCD00", "#1A1A1A"]
    assert [t.name for t in fig.data] == ["C-2 · Candidate B", "C-1 · Candidate A", "C-3 · Candidate C"]
    assert list(fig.data[1].r) == [1.0, 1.0, 0.3, 0.6, 1.0]           # clipped to 0..1, polygon closed
    assert list(fig.data[0].r)[3] == 0.0 and list(fig.data[2].r)[0] == 0.0  # below 0 / missing -> 0
    four = pd.concat([df, df.iloc[[0]].assign(**{S.CANDIDATE_ID: "C-4"})], ignore_index=True)
    assert tab_compare.radar_figure(four, ["C-1", "C-2", "C-3", "C-4"]).data[3].line.color == "#F1B521"
    empty = tab_compare.radar_figure(df.drop(columns=list(tab_compare.SUB_LABELS)), ["C-1", "C-2"])
    assert not empty.data and empty.layout.annotations


def test_comparison_export_is_complete_and_formula_safe() -> None:
    df = _small_frame().assign(**{S.NAME: ["=HYPERLINK(\"http://x\")", "Candidate B", "Candidate C"],
                                  S.COLLEGE: ["College One", "College Two", None]})
    ids = ["C-1", "C-2"]
    required = ["Python", "Cloud", "SQL"]
    comparison = tab_compare.build_comparison(df, ids, len(required))
    matrix = tab_compare.skills_matrix(df, ids, required)
    frame = tab_compare.comparison_export(df, ids, comparison, matrix)
    metrics = frame[tab_compare.METRIC].tolist()
    assert metrics[:3] == ["Candidate ID", "Name", "College"]
    assert "Final score" in metrics and metrics[-3:] == [f"Required skill: {s}" for s in required]
    assert frame.iloc[-3:, 2].tolist() == ["Missing", "Missing", "Matched"]
    text = csv_bytes(frame).decode("utf-8-sig")
    assert "Required skill: Python" in text and "College Two" in text
    assert "'=HYPERLINK" in text  # spreadsheet formula injection neutralised by the CSV export
