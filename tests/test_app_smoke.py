"""UI tests: AppTest smoke runs of app.py plus unit tests of the pure UI helpers.

The engine is faked (tests/ui_fixtures.py) so these tests exercise the UI in isolation. No network,
no real proxy token, no connection file and no .env file of this machine is ever used, and the
parsed-data cache is written to a pytest tmp folder.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import pytest
from streamlit.testing.v1 import AppTest

from screening import env, export, llm, pipeline, scoring, snapshots
from screening import llm_backends as backends
from screening import schema as S
from screening.config import AppConfig, LLMConfig
from screening.models import DataQualityReport, PipelineResult, ResumeDoc, RiskResult
from ui import charts, state, tab_deepdive, tab_shortlist, theme

try:
    from tests import ui_fixtures as fx
    from tests.ui_fixtures import make_fake_prepared_and_result
except ImportError:  # pragma: no cover - tests/ imported as rootdir-relative modules
    import ui_fixtures as fx
    from ui_fixtures import make_fake_prepared_and_result

APP = str(Path(__file__).resolve().parent.parent / "app.py")
FP = "fake-fingerprint"
KEY_ENV = LLMConfig().api_key_env  # env var / secret name of the proxy token, from the config
FAKE_TOKEN = "dummy-test-value"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
def _patch(monkeypatch: pytest.MonkeyPatch, module: Any, name: str, value: Any) -> None:
    """setattr that also works on stub modules (their __getattr__ raises NotImplementedError)."""
    monkeypatch.setitem(vars(module), name, value)


@pytest.fixture(autouse=True)
def proxy_calls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[Any]:
    """Isolation: no token in the environment, no connection file, no .env, no sample folder, a tmp
    snapshot cache and a fake proxy status check (records the tokens it was given)."""
    monkeypatch.delenv(KEY_ENV, raising=False)
    _patch(monkeypatch, backends, "discovery_path", lambda: tmp_path / "no-proxy-connection.json")
    _patch(monkeypatch, env, "DEFAULT_ENV_FILE", tmp_path / "no.env")
    _patch(monkeypatch, snapshots, "PROJECT_ROOT", tmp_path / "project")
    _patch(monkeypatch, pipeline, "DEFAULT_SAMPLE_DIR", tmp_path / "no-sample")
    calls: list[Any] = []

    def proxy_status(cfg: Any, token: str | None = None, **kwargs: Any) -> dict[str, Any]:
        calls.append(token)
        return {"reachable": True, "models": ["gpt-4o", "gpt-4.1"], "error": "",
                "base_url": cfg.base_url, "token_source": "configured"}

    _patch(monkeypatch, backends, "proxy_status", proxy_status)
    return calls


@pytest.fixture(scope="module")
def fake() -> tuple[pipeline.PreparedData, PipelineResult]:
    return make_fake_prepared_and_result()


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch, fake: tuple[pipeline.PreparedData, PipelineResult]) -> dict[str, Any]:
    """Fake evaluate/export/scoring; records evaluate calls."""
    prepared, result = fake
    calls: list[Any] = []

    def evaluate(prep, cfg, *, risk_result=None):
        calls.append((cfg, None))
        return replace(result, config=cfg)

    _patch(monkeypatch, pipeline, "evaluate", evaluate)
    _patch(monkeypatch, export, "to_csv_bytes",
           lambda df, labelled=False: df.to_csv(index=False).encode("utf-8-sig"))
    _patch(monkeypatch, export, "build_excel_report", lambda res, llm_notes=None: b"PK-fake-xlsx")
    _patch(monkeypatch, export, "build_evidence_pack", lambda res, cid: f"# Evidence pack {cid}\n")
    _patch(monkeypatch, scoring, "explain_score",
           lambda row, cfg, **kwargs: ["HackerRank: 40.0 / 40 pts", "Skills: 2 of 3"])
    return {"prepared": prepared, "result": result, "calls": calls}


def _app() -> AppTest:
    at = AppTest.from_file(APP, default_timeout=60)
    at.secrets[KEY_ENV] = ""  # never read a real secrets.toml during tests
    return at


def _inject(at: AppTest, prepared: Any, result: PipelineResult, tab: str | None = None) -> AppTest:
    at.session_state[state.K_CONFIG] = result.config
    at.session_state[state.K_PREPARED] = prepared
    at.session_state[state.K_PREPARED_FP] = FP
    at.session_state[state.K_RESULT] = result
    at.session_state[state.K_RESULT_KEY] = state.eval_key(FP, result.config)
    at.session_state[state.K_DATASET] = "Fake dataset"
    if tab:
        at.session_state[state.K_TABS] = tab
    return at


def _assert_clean(at: AppTest) -> None:
    assert not at.exception, [e.value for e in at.exception]


def _html(at: AppTest) -> str:
    return "\n".join(h.value for h in at.html)


def _sidebar_text(at: AppTest) -> str:
    bar = at.sidebar
    return "\n".join(e.value for e in [*bar.markdown, *bar.caption, *bar.warning, *bar.info, *bar.error])


# ---------------------------------------------------------------------------
# AppTest smoke tests
# ---------------------------------------------------------------------------
def test_empty_state_renders(proxy_calls: list[Any]) -> None:
    at = _app().run()
    _assert_clean(at)
    assert "wf-banner" in _html(at) and "TALENT ACQUISITION" in _html(at)
    assert "Workday" in _html(at) and "Resume.zip" in _html(at)          # current data format in the guide
    assert "never changes scores, ranks, cutoffs or the shortlist" in _html(at)
    button = at.button(key="empty_state_run")
    assert button.label == "Load synthetic sample & run"
    assert button.disabled  # sample folder missing -> instructions instead
    assert any("generate_mock_data.py" in c.value for c in at.caption)
    text = _sidebar_text(at)
    assert "Synthetic Workday export (2,133 applications) + Resume.zip with 33 PDF packets" in text
    assert "GitHub Copilot (VS Code proxy)" in text and "Not connected" in text
    assert proxy_calls == []  # no token anywhere -> no network check


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


def test_sample_button_runs_pipeline_with_progress_and_caches_it(engine: dict[str, Any],
                                                                 monkeypatch: pytest.MonkeyPatch,
                                                                 tmp_path: Path) -> None:
    sample = tmp_path / "sample"
    sample.mkdir()
    (sample / "Resume.zip").write_bytes(b"PK")          # the sample button needs a sample to exist
    _patch(monkeypatch, pipeline, "DEFAULT_SAMPLE_DIR", sample)
    docs = [ResumeDoc("CAND-00001_Asha_Menon.txt", b"Asha Menon\nPython")]
    _patch(monkeypatch, pipeline, "load_sample_dataset",
           lambda sample_dir=None: (("test_scores_export.xlsx", b"PK-workbook"), docs, None))
    prepare_calls: list[Any] = []

    def prepare_data(ta_file, resume_docs, cfg, *, mapping_override=None, progress_cb=None):
        for done in range(3):
            progress_cb("Reading resume files", done, 2, "file")
        prepare_calls.append((ta_file[0], len(resume_docs), mapping_override))
        return engine["prepared"]

    _patch(monkeypatch, pipeline, "prepare_data", prepare_data)
    at = _app().run()
    assert not at.button(key="empty_state_run").disabled
    at.button(key="empty_state_run").click().run()
    _assert_clean(at)
    assert prepare_calls == [("test_scores_export.xlsx", 1, None)]
    assert at.session_state[state.K_RESULT] is not None
    assert at.session_state[state.K_PREPARED_FP]
    assert "Total Processed" in _html(at)

    cached = snapshots.list_snapshots(AppConfig())          # written under the tmp project folder
    assert cached["label"].tolist() == ["Synthetic sample"] and cached["candidates"].tolist() == [fx.N_CANDIDATES]
    fresh = _app().run()                                    # a new session: reloaded, not re-parsed
    fresh.button(key="empty_state_run").click().run()
    _assert_clean(fresh)
    assert len(prepare_calls) == 1
    assert any("from the cache" in t.value for t in fresh.toast)


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


def test_llm_actions_with_fake_client(engine: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
                                      proxy_calls: list[Any]) -> None:
    monkeypatch.setenv(KEY_ENV, FAKE_TOKEN)
    _patch(monkeypatch, state, "_STATUS_TTL_SEC", 3600.0)  # a slow machine must not expire the cached status

    class FakeClient:
        def __init__(self, cfg: Any, api_key: str | None, client: Any = None) -> None:
            self.cfg = cfg

        def test_connection(self) -> tuple[bool, str]:
            return True, "ok"

    def explain(ids, candidates, parsed_by_candidate, risk_result, cfg, client, progress_cb=None, **kwargs):
        progress_cb(1, 1, "")
        return {cid: {"ok": True, "focus": kwargs.get("focus", llm.FOCUS_QUALIFIED), "summary": f"Summary for {cid}",
                      "strengths": ["Strong DSA"], "gaps": ["Cloud"],
                      "interview_questions": ["Walk through your project"], "integrity_questions": ["Re-test q2"],
                      "source": "llm", "error": ""}
                for cid in ids}

    _patch(monkeypatch, llm, "LLMClient", FakeClient)
    _patch(monkeypatch, llm, "explain_candidates", explain)
    _patch(monkeypatch, llm, "executive_summary", lambda result, client: {"ok": True, "markdown": "## Exec brief"})
    at = _inject(_app(), engine["prepared"], engine["result"], state.TAB_DEEPDIVE)
    at.session_state[state.K_DD_CANDIDATE] = "CAND-00011"
    at.run()
    _assert_clean(at)
    text = _sidebar_text(at)
    assert "GitHub Copilot (VS Code proxy)" in text and "Connected" in text and "token from environment" in text
    assert FAKE_TOKEN not in text and proxy_calls == [FAKE_TOKEN]
    at.button(key="dd_llm_explain").click().run()
    _assert_clean(at)
    assert at.session_state[state.K_LLM_EXPLAIN]["CAND-00011"]["ok"]
    assert any("Summary for CAND" in m.value for m in at.markdown)
    at.session_state[state.K_TABS] = state.TAB_RISK
    at.run()
    at.button(key="rk_llm_exec").click().run()
    _assert_clean(at)
    assert any("Exec brief" in m.value for m in at.markdown)
    assert len(proxy_calls) == 1  # the proxy status is cached for 30 s: one check across these runs


def test_llm_buttons_disabled_without_key(engine: dict[str, Any], proxy_calls: list[Any]) -> None:
    at = _inject(_app(), engine["prepared"], engine["result"], state.TAB_SHORTLIST).run()
    _assert_clean(at)
    keys = {b.key for b in at.button}
    assert "sl_llm_rescore" not in keys  # the LLM never re-scores
    assert at.button(key="sl_llm_rationales").disabled and at.button(key="sl_llm_borderline").disabled
    text = _sidebar_text(at)
    assert "Not connected" in text and "Open VS Code with the Screening Copilot Proxy extension running" in text
    assert "never changes scores, ranks, cutoffs or the shortlist" in text
    assert proxy_calls == []


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def test_fake_fixture_agrees_with_the_real_shortlist_rules(fake: tuple[Any, PipelineResult]) -> None:
    """The fake's statuses, ranks, ineligibility rules and tie-break notes are what the engine computes."""
    result = fake[1]
    real = scoring.apply_shortlist(result.candidates, result.config).set_index(S.CANDIDATE_ID)
    mine = result.candidates.set_index(S.CANDIDATE_ID)
    for col in (S.STATUS, S.ELIGIBLE, S.TIE_BREAK_NOTE, S.INELIGIBILITY):
        assert mine[col].astype(str).to_dict() == real.loc[mine.index, col].astype(str).to_dict(), col
    assert mine[S.RANK].fillna(-1).astype(int).to_dict() == real.loc[mine.index, S.RANK].fillna(-1).astype(int).to_dict()
    rules = {cid: [r["rule_violated"] for r in scoring.ineligibility_rules(v)] for cid, v in mine[S.INELIGIBILITY].items()}
    assert {cid: r for cid, r in rules.items() if r} == {"CAND-00056": [S.RULE_DUPLICATE], "CAND-00057": [S.RULE_NO_SCORE],
                                                        "CAND-00060": [S.RULE_NO_SCORE]}
    assert (mine[S.STATUS] == S.STATUS_INELIGIBLE).sum() == 3 and (mine[S.STATUS] == S.STATUS_REVIEW).sum() == 2
    assert int(mine[S.STATUS].isin(charts.SHORTLIST_STATUSES).sum()) == fx.TOP_N
    tied = mine[mine[S.TIE_BREAK_NOTE] != ""]
    assert len(tied) == 2 and set(tied[S.RANK]) == {fx.TOP_N, fx.TOP_N + 1}
    assert mine.at[fx.DUPLICATE_ID, S.DUPLICATE_OF] == fx.DUPLICATE_PRIMARY
    assert mine.at[fx.DUPLICATE_PRIMARY, S.DUPLICATE_IDS] == fx.DUPLICATE_ID
    assert (mine.at[fx.INJECTION_ID, S.STATUS], mine.at[fx.INJECTION_ID, S.RISK_LEVEL]) == (S.STATUS_NOT, S.RISK_MEDIUM)
    assert mine.at[fx.INJECTION_ID, S.INJECTION_FLAGS] and (mine[S.INJECTION_FLAGS] != "").sum() == 1
    resumes = fake[1].parsed_by_candidate
    assert next(iter(resumes)) == fx.INJECTION_ID and resumes[fx.INJECTION_ID].injection_findings
    assert [c for c, p in resumes.items() if p.injection_findings or p.bundle_source] == [fx.INJECTION_ID]
    flags = fake[1].risk.individual
    assert flags.loc[flags["flag_key"] == "prompt_injection", S.CANDIDATE_ID].tolist() == [fx.INJECTION_ID]
    assert set(mine[S.CUTOFF_SCORE]) == {96.0} and set(mine[S.CUTOFF_PCT]) == {55.0}
    assert mine.at["CAND-00001", S.JOB_APPLICATION] == "Rohan Menon (rohan menon)"


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
    assert state.safe_error(RuntimeError("bad key sk-abc123XYZ")) == "RuntimeError: bad key [redacted]"
    assert "[redacted]" in state.safe_error(RuntimeError("token " + "ab12" * 16))  # proxy tokens are hex


def test_llm_markdown_is_sanitised() -> None:
    text = "## Brief\n![x](https://evil.example/p?d=1) see [site](https://evil.example) <img src=x> cost $5"
    out = theme.safe_markdown(text)
    assert "evil.example" not in out and "<img" not in out and "](" not in out
    assert out.startswith("## Brief") and "see site" in out and r"\$5" in out
    assert theme.md_escape("a*b_[c]$") == r"a\*b\_\[c\]\$"


def test_md_text_shows_urls_as_code_and_escapes_markdown() -> None:
    out = theme.md_text("Could not reach the proxy at http://127.0.0.1:5757/v1. See [docs](x) *now* $5")
    assert "`http://127.0.0.1:5757/v1`." in out
    assert r"\[docs\]\(x\)" in out and r"\*now\*" in out and r"\$5" in out
    assert theme.md_text(None) == "" and theme.md_text("plain") == "plain"


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


def test_sidebar_never_stores_the_token(engine: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(KEY_ENV, FAKE_TOKEN)
    at = _inject(_app(), engine["prepared"], engine["result"], state.TAB_SHORTLIST).run()
    _assert_clean(at)
    for key in at.session_state:
        value = at.session_state[key]
        if isinstance(value, (str, dict, list, tuple)):
            assert FAKE_TOKEN not in json.dumps(value, default=str), key
    assert all(FAKE_TOKEN not in str(getattr(w, "value", "")) for w in at.sidebar.button)
