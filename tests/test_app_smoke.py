"""UI tests: AppTest smoke runs of app.py plus unit tests of the pure UI helpers.

The engine is faked (tests/ui_fixtures.py) so these tests exercise the UI in isolation;
no network or real API key is ever used.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import pytest
from streamlit.testing.v1 import AppTest

from screening import export, llm, pipeline, scoring
from screening import schema as S
from screening.models import PipelineResult, ResumeDoc, RiskResult, DataQualityReport
from ui import charts, state, tab_deepdive, tab_shortlist

try:
    from tests.ui_fixtures import make_fake_prepared_and_result
except ImportError:  # pragma: no cover - tests/ imported as rootdir-relative modules
    from ui_fixtures import make_fake_prepared_and_result

APP = str(Path(__file__).resolve().parent.parent / "app.py")
FP = "fake-fingerprint"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
def _patch(monkeypatch: pytest.MonkeyPatch, module: Any, name: str, value: Any) -> None:
    """setattr that also works on stub modules (their __getattr__ raises NotImplementedError)."""
    monkeypatch.setitem(vars(module), name, value)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    _patch(monkeypatch, pipeline, "DEFAULT_SAMPLE_DIR", tmp_path / "no-sample")


@pytest.fixture(scope="module")
def fake() -> tuple[pipeline.PreparedData, PipelineResult]:
    return make_fake_prepared_and_result()


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch, fake: tuple[pipeline.PreparedData, PipelineResult]) -> dict[str, Any]:
    """Fake evaluate/export/scoring; records evaluate calls."""
    prepared, result = fake
    calls: list[Any] = []

    def evaluate(prep, cfg, llm_results=None, *, risk_result=None):
        calls.append((cfg, llm_results))
        return replace(result, config=cfg)

    _patch(monkeypatch, pipeline, "evaluate", evaluate)
    _patch(monkeypatch, export, "to_csv_bytes", lambda df: df.to_csv(index=False).encode("utf-8-sig"))
    _patch(monkeypatch, export, "build_excel_report", lambda res, llm_notes=None: b"PK-fake-xlsx")
    _patch(monkeypatch, export, "build_evidence_pack", lambda res, cid: f"# Evidence pack {cid}\n")
    _patch(monkeypatch, scoring, "explain_score", lambda row, cfg: ["HackerRank: 40.0 / 40 pts", "Skills: 2 of 3"])
    return {"prepared": prepared, "result": result, "calls": calls}


def _app() -> AppTest:
    at = AppTest.from_file(APP, default_timeout=60)
    at.secrets["NVIDIA_API_KEY"] = ""  # never read a real secrets.toml during tests
    return at


def _inject(at: AppTest, prepared: Any, result: PipelineResult, tab: str | None = None) -> AppTest:
    at.session_state[state.K_CONFIG] = result.config
    at.session_state[state.K_PREPARED] = prepared
    at.session_state[state.K_PREPARED_FP] = FP
    at.session_state[state.K_RESULT] = result
    at.session_state[state.K_RESULT_KEY] = state.eval_key(FP, result.config, None)
    at.session_state[state.K_DATASET] = "Fake dataset"
    if tab:
        at.session_state[state.K_TABS] = tab
    return at


def _assert_clean(at: AppTest) -> None:
    assert not at.exception, [e.value for e in at.exception]


def _html(at: AppTest) -> str:
    return "\n".join(h.value for h in at.html)


# ---------------------------------------------------------------------------
# AppTest smoke tests
# ---------------------------------------------------------------------------
def test_empty_state_renders() -> None:
    at = _app().run()
    _assert_clean(at)
    assert "wf-banner" in _html(at) and "TALENT ACQUISITION" in _html(at)
    button = at.button(key="empty_state_run")
    assert button.label == "Load synthetic sample & run"
    assert button.disabled  # sample folder missing -> instructions instead
    assert any("generate_mock_data.py" in c.value for c in at.caption)


@pytest.mark.parametrize("tab", state.TAB_LABELS)
def test_each_tab_renders_with_fake_result(engine: dict[str, Any], tab: str) -> None:
    at = _inject(_app(), engine["prepared"], engine["result"], tab).run()
    _assert_clean(at)
    opened = [t for t in at.tabs if t.label == tab]
    assert opened and opened[0].children, f"{tab} rendered nothing"
    assert "Total Processed" in _html(at)
    assert engine["calls"] == []  # cached result reused, no re-evaluation


def test_tabs_tolerate_minimal_result_and_missing_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    for mod, name in ((export, "to_csv_bytes"), (export, "build_excel_report"), (export, "build_evidence_pack"),
                      (scoring, "explain_score")):
        monkeypatch.delitem(vars(mod), name, raising=False)  # behave like an unimplemented stub
    minimal = pd.DataFrame({S.CANDIDATE_ID: ["A-1", "A-2", "A-3"], S.NAME: ["X", "Y", None],
                            S.FINAL_SCORE: [81.0, 64.5, None], S.STATUS: [S.STATUS_SHORTLISTED, S.STATUS_NOT, ""]})
    result = PipelineResult(candidates=minimal, parsed_resumes={}, parsed_by_candidate={}, matches=pd.DataFrame(),
                            risk=RiskResult(), quality=DataQualityReport(), config=make_fake_prepared_and_result()[1].config)
    at = _app()
    at.session_state[state.K_RESULT] = result
    for tab in state.TAB_LABELS:
        at.session_state[state.K_TABS] = tab
        at.run()
        _assert_clean(at)


def test_sample_button_runs_pipeline_with_progress(engine: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
                                                   tmp_path: Path) -> None:
    sample = tmp_path / "sample"
    (sample / "resumes").mkdir(parents=True)
    _patch(monkeypatch, pipeline, "DEFAULT_SAMPLE_DIR", sample)
    docs = [ResumeDoc("CAND-00001_Asha_Menon.txt", b"Asha Menon\nPython")]
    _patch(monkeypatch, pipeline, "load_sample_dataset", lambda sample_dir=None: (("ta_feed.csv", b"id\n1\n"), docs, None))
    prepare_calls: list[Any] = []

    def prepare_data(ta_file, resume_docs, cfg, *, mapping_override=None, progress_cb=None):
        for done in range(3):
            progress_cb("Parsing resumes", done, 2, "file")
        prepare_calls.append((ta_file[0], len(resume_docs), mapping_override))
        return engine["prepared"]

    _patch(monkeypatch, pipeline, "prepare_data", prepare_data)
    at = _app().run()
    assert not at.button(key="empty_state_run").disabled
    at.button(key="empty_state_run").click().run()
    _assert_clean(at)
    assert prepare_calls == [("ta_feed.csv", 1, None)]
    assert at.session_state[state.K_RESULT] is not None
    assert at.session_state[state.K_PREPARED_FP]
    assert "Total Processed" in _html(at)


def test_open_profile_switches_to_deep_dive(engine: dict[str, Any]) -> None:
    at = _inject(_app(), engine["prepared"], engine["result"], state.TAB_SHORTLIST)
    at.session_state[state.K_DD_CANDIDATE] = "CAND-00011"
    at.run()
    at.button(key="sl_open_profile").click().run()
    _assert_clean(at)
    assert at.session_state[state.K_TABS] == state.TAB_DEEPDIVE
    assert at.selectbox(key=state.K_DD_CANDIDATE).value == "CAND-00011"


def test_config_form_applies_and_rescored(engine: dict[str, Any]) -> None:
    at = _inject(_app(), engine["prepared"], engine["result"], state.TAB_CONFIG).run()
    ver = at.session_state[state.K_CONFIG_VERSION]
    at.number_input(key=f"cfg{ver}_top_n").set_value(10)
    at.button(key="cfg_apply").click().run()
    _assert_clean(at)
    assert at.session_state[state.K_CONFIG].shortlist.top_n == 10
    assert engine["calls"] and engine["calls"][-1][0].shortlist.top_n == 10


def test_llm_actions_with_fake_client(engine: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NVIDIA_API_KEY", "dummy-test-value")

    class FakeClient:
        def __init__(self, cfg: Any, api_key: str, client: Any = None) -> None:
            self.cfg = cfg

        def test_connection(self) -> tuple[bool, str]:
            return True, "ok"

    def explain(ids, candidates, parsed_by_candidate, risk_result, cfg, client, progress_cb=None):
        progress_cb(1, 1, "")
        return {cid: {"ok": True, "summary": f"Summary for {cid}", "strengths": ["Strong DSA"], "gaps": ["Cloud"],
                      "interview_questions": ["Walk through your project"], "integrity_questions": ["Re-test q2"]}
                for cid in ids}

    _patch(monkeypatch, llm, "LLMClient", FakeClient)
    _patch(monkeypatch, llm, "explain_candidates", explain)
    _patch(monkeypatch, llm, "executive_summary", lambda result, client: {"ok": True, "markdown": "## Exec brief"})
    at = _inject(_app(), engine["prepared"], engine["result"], state.TAB_DEEPDIVE)
    at.session_state[state.K_DD_CANDIDATE] = "CAND-00011"
    at.run()
    assert any("LLM: configured" in m.value for m in at.sidebar.markdown)
    at.button(key="dd_llm_explain").click().run()
    _assert_clean(at)
    assert at.session_state[state.K_LLM_EXPLAIN]["CAND-00011"]["ok"]
    assert any("Summary for CAND" in m.value for m in at.markdown)
    at.session_state[state.K_TABS] = state.TAB_RISK
    at.run()
    at.button(key="rk_llm_exec").click().run()
    _assert_clean(at)
    assert any("Exec brief" in m.value for m in at.markdown)


def test_llm_buttons_disabled_without_key(engine: dict[str, Any]) -> None:
    at = _inject(_app(), engine["prepared"], engine["result"], state.TAB_SHORTLIST).run()
    assert at.button(key="sl_llm_rescore").disabled and at.button(key="sl_llm_rationales").disabled
    assert any("LLM: not configured" in m.value for m in at.sidebar.markdown)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def test_filter_candidates(fake: tuple[Any, PipelineResult]) -> None:
    df = fake[1].candidates
    assert len(tab_shortlist.filter_candidates(df)) == len(df)
    review = tab_shortlist.filter_candidates(df, statuses=[S.STATUS_REVIEW])
    assert set(review[S.STATUS]) == {S.STATUS_REVIEW}
    high = tab_shortlist.filter_candidates(df, risks=[S.RISK_HIGH])
    assert set(high[S.CANDIDATE_ID]) == {"CAND-00003", "CAND-00007", "CAND-00011", "CAND-00015"}
    assert (tab_shortlist.filter_candidates(df, min_score=80)[S.FINAL_SCORE] >= 80).all()
    with_skills = tab_shortlist.filter_candidates(df, skills=["python", "Java"])
    assert all({"Python", "Java"} <= set(charts.split_list(v)) for v in with_skills[S.MATCHED_SKILLS])
    assert list(tab_shortlist.filter_candidates(df, query="cand-00011")[S.CANDIDATE_ID]) == ["CAND-00011"]
    assert tab_shortlist.filter_candidates(df, colleges=["Nowhere"]).empty


def test_build_view_orders_by_rank_and_labels_risk(fake: tuple[Any, PipelineResult]) -> None:
    view = tab_shortlist.build_view(fake[1].candidates)
    ranks = view[S.RANK].dropna().tolist()
    assert ranks == sorted(ranks) and view[S.RANK].iloc[-1] != view[S.RANK].iloc[-1]  # NaN (ineligible) last
    assert set(view["risk"]) <= {"🔴 High", "🟠 Medium", "🟡 Low", "🟢 Clear"}
    assert isinstance(view[S.MATCHED_SKILLS].iloc[0], list)


def test_chart_builders_return_figures(fake: tuple[Any, PipelineResult]) -> None:
    result = fake[1]
    df, risk = result.candidates, result.risk
    cutoff = charts.shortlist_cutoff(df, "top_n", 70.0)
    assert cutoff == df.loc[df[S.STATUS].isin(charts.SHORTLIST_STATUSES), S.FINAL_SCORE].min()
    assert charts.shortlist_cutoff(df, "threshold", 70.0) == 70.0
    figs = [charts.score_histogram(df, cutoff), charts.lift_bar(risk.commonality),
            charts.risk_network(risk.pairs, df, color_by="risk"), charts.risk_network(risk.pairs, df, color_by="cluster"),
            charts.timeline_scatter(df, risk.focus_ids),
            charts.radar_subscores({S.SUB_HR: 1.0, S.SUB_SKILLS: 0.5}, {S.SUB_HR: 0.8}),
            charts.points_breakdown({"HackerRank": 40.0, "Skills": float("nan")}, {"HackerRank": 40.0, "Skills": 30.0})]
    assert all(isinstance(f, go.Figure) and f.data for f in figs)
    network = charts.risk_network(risk.pairs, df)
    assert sum(len(t.x) for t in network.data if t.mode and "markers" in t.mode and t.showlegend is not False) == 9
    empty = pd.DataFrame()
    for fig in (charts.score_histogram(empty), charts.lift_bar(None), charts.risk_network(None, None),
                charts.timeline_scatter(empty), charts.points_breakdown({}, {})):
        assert isinstance(fig, go.Figure) and fig.layout.annotations


def test_split_list_and_candidate_options(fake: tuple[Any, PipelineResult]) -> None:
    assert charts.split_list("Python, SQL ,") == ["Python", "SQL"]
    assert charts.split_list("a | b", S.SEP_REASONS) == ["a", "b"]
    assert charts.split_list(float("nan")) == [] and charts.split_list(None) == []
    ids, labels = tab_deepdive.candidate_options(fake[1].candidates)
    assert len(ids) == 60 and labels[ids[0]].startswith(ids[0] + " - ")
    assert fake[1].candidates.set_index(S.CANDIDATE_ID).at[ids[0], S.RANK] == 1


def test_inputs_fingerprint_is_stable_and_sensitive() -> None:
    cfg = make_fake_prepared_and_result()[1].config
    docs = [ResumeDoc("a.pdf", b"one"), ResumeDoc("b.pdf", b"two")]
    fp = state.inputs_fingerprint(("ta.csv", b"x"), docs, None, cfg)
    assert fp == state.inputs_fingerprint(("ta.csv", b"x"), list(reversed(docs)), None, cfg)
    assert fp != state.inputs_fingerprint(("ta.csv", b"x"), docs, {"Score": S.HR_SCORE}, cfg)
    assert fp != state.inputs_fingerprint(("ta.csv", b"y"), docs, None, cfg)
    assert state.safe_error(RuntimeError("bad key nvapi-abc123XYZ")) == "RuntimeError: bad key [redacted]"


def test_llm_markdown_is_sanitised() -> None:
    from ui import theme

    text = "## Brief\n![x](https://evil.example/p?d=1) see [site](https://evil.example) <img src=x> cost $5"
    out = theme.safe_markdown(text)
    assert "evil.example" not in out and "<img" not in out and "](" not in out
    assert out.startswith("## Brief") and "see site" in out and r"\$5" in out
    assert theme.md_escape("a*b_[c]$") == r"a\*b\_\[c\]\$"


def test_config_tab_available_before_first_run() -> None:
    """TA can set the JD / skills / weights before any data is loaded."""
    at = _app()
    at.run()
    _assert_clean(at)
    assert [t.label for t in at.tabs][:2] == ["Getting started", state.TAB_CONFIG]
    jd_box = next(a for a in at.text_area if "Job description" in a.label)
    jd_box.set_value("Backend Java developer role: Spring Boot, SQL, AWS.")
    next(b for b in at.button if b.label == "Apply & re-score").click()
    at.run()
    _assert_clean(at)
    assert at.session_state[state.K_CONFIG].experience.job_description.startswith("Backend Java developer")
