"""UI tests (AppTest): Campus Distribution tab, college cap setting and the Config what-if preview.

The engine is faked with tests/ui_fixtures.py; previews re-run the real scoring.apply_shortlist on
the fake candidates so the shortlist differences are genuine. No network or API key is used.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import pytest
from streamlit.testing.v1 import AppTest

from screening import analytics, export, pipeline, scoring
from screening import schema as S
from screening.config import AppConfig
from screening.models import DataQualityReport, PipelineResult, RiskResult
from ui import state, tab_campus, tab_config

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
    monkeypatch.setitem(vars(module), name, value)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    _patch(monkeypatch, pipeline, "DEFAULT_SAMPLE_DIR", tmp_path / "no-sample")
    _patch(monkeypatch, export, "to_csv_bytes", lambda df: df.to_csv(index=False).encode("utf-8-sig"))


def _scored(cap_pct: float = 0.0) -> tuple[Any, PipelineResult]:
    """Fake run (top 15) shortlisted by the real apply_shortlist, optionally with a college cap."""
    prepared, fake = make_fake_prepared_and_result()
    cfg = fake.config
    cfg.shortlist.college_cap_pct = cap_pct
    return prepared, replace(fake, candidates=scoring.apply_shortlist(fake.candidates, cfg), config=cfg)


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake pipeline.evaluate that re-shortlists the fake candidates with the given config."""
    prepared, result = _scored()
    calls: list[dict[str, Any]] = []

    def evaluate(prep, cfg, llm_results=None, *, risk_result=None):
        calls.append({"cfg": cfg, "llm": llm_results, "risk_reused": risk_result is not None})
        return replace(result, candidates=scoring.apply_shortlist(result.candidates, cfg), config=cfg)

    _patch(monkeypatch, pipeline, "evaluate", evaluate)
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


def _metrics(at: AppTest) -> dict[str, str]:
    return {m.label: m.value for m in at.metric}


def _campus_page() -> None:
    import streamlit as st

    from ui import state, tab_campus

    state.init_state()
    tab_campus.render(st.session_state.get(state.K_RESULT))


def _campus(result: PipelineResult | None) -> AppTest:
    """Render the Campus Distribution tab on its own (app.py decides when the tab is shown)."""
    at = AppTest.from_function(_campus_page, default_timeout=60)
    if result is not None:
        at.session_state[state.K_RESULT] = result
    return at.run()


def _preview(at: AppTest, **form: Any) -> AppTest:
    ver = at.session_state[state.K_CONFIG_VERSION]
    for name, value in form.items():
        at.number_input(key=f"cfg{ver}_{name}").set_value(value)
    at.button(key="cfg_preview").click().run()
    _assert_clean(at)
    return at


# ---------------------------------------------------------------------------
# Campus Distribution tab
# ---------------------------------------------------------------------------
def test_campus_tab_renders_kpis_chart_table_and_download() -> None:
    _, result = _scored()
    at = _campus(result)
    _assert_clean(at)
    assert _metrics(at) == {"Colleges": "5", "Top college share of shortlist": "26.7%", "College cap": "Off"}
    table = at.dataframe[0].value
    assert len(table) == 5 and int(table["shortlisted"].sum()) == 15
    assert table["group"].iloc[0] == "Northfield Institute of Technology"
    assert len(at.get("plotly_chart")) == 1
    assert any("1.0 means a group is shortlisted in proportion" in c.value for c in at.caption)
    assert at.download_button[0].label == "Distribution by college (CSV)"


def test_campus_tab_group_by_switch() -> None:
    _, result = _scored()
    at = _campus(result)
    control = at.button_group[0]
    assert control.options == ["College", "College tier", "City", "Degree", "Branch"]
    control.set_value(S.COLLEGE_TIER).run()
    _assert_clean(at)
    assert "College tiers" in _metrics(at)
    assert set(at.dataframe[0].value["group"]) == {"Tier 1", "Tier 2", "Tier 3"}
    at.button_group[0].set_value(S.CITY).run()
    _assert_clean(at)
    assert _metrics(at)["Cities"] == "5"
    assert at.session_state[tab_campus.K_GROUP_BY] == S.CITY


def test_campus_tab_flags_colleges_over_the_cap() -> None:
    _, result = _scored(cap_pct=5.0)        # 1 place per college -> 5 shortlisted, 20% each
    at = _campus(result)
    _assert_clean(at)
    metrics = _metrics(at)
    assert metrics["Colleges over the cap"] == "5"
    over = next(m for m in at.metric if m.label == "Colleges over the cap")
    assert "capped out (cap 5%)" in over.delta
    table = at.dataframe[0].value
    assert table["over_cap"].all() and int(table["capped_out"].sum()) == int(result.candidates[S.COLLEGE_CAPPED].sum())
    assert any("Shaded rows hold more than the 5% college cap" in c.value for c in at.caption)


def test_campus_tab_handles_missing_data() -> None:
    at = _campus(None)
    _assert_clean(at)
    assert "Run a screening" in at.info[0].value
    minimal = PipelineResult(candidates=pd.DataFrame({S.CANDIDATE_ID: ["A-1", "A-2"], S.STATUS: [S.STATUS_SHORTLISTED, ""]}),
                             parsed_resumes={}, parsed_by_candidate={}, matches=pd.DataFrame(), risk=RiskResult(),
                             quality=DataQualityReport(), config=AppConfig())
    at = _campus(minimal)
    _assert_clean(at)
    assert "no campus attributes" in at.info[0].value


def test_campus_tab_reports_errors_instead_of_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: Any, **kwargs: Any) -> pd.DataFrame:
        raise ValueError("unexpected column type")

    _patch(monkeypatch, analytics, "campus_distribution", broken)
    _, result = _scored()
    at = _campus(result)
    _assert_clean(at)
    assert any("Could not build the campus distribution" in e.value for e in at.error)


def test_share_chart_builder() -> None:
    _, result = _scored()
    dist = analytics.campus_distribution(result.candidates, S.COLLEGE, result.config)
    fig = tab_campus.share_chart(dist, "College", cap_pct=20.0, top=3)
    assert isinstance(fig, go.Figure) and [t.name for t in fig.data] == ["Share of applicants", "Share of shortlist"]
    assert all(len(t.y) == 3 for t in fig.data)
    assert list(fig.data[1].y)[-1] == dist["group"].iloc[0]          # largest group drawn at the top
    assert fig.layout.shapes                                          # dashed cap line
    flagged = tab_campus.share_chart(dist.assign(over_cap=[True] + [False] * (len(dist) - 1)), "College")
    assert sum(str(t).startswith("⚠") for t in flagged.layout.yaxis.ticktext) == 1
    for empty in (pd.DataFrame(), None):
        assert tab_campus.share_chart(empty, "College").layout.annotations


# ---------------------------------------------------------------------------
# Config: college cap + what-if preview
# ---------------------------------------------------------------------------
def test_college_cap_setting_is_applied(engine: dict[str, Any]) -> None:
    at = _inject(_app(), engine["prepared"], engine["result"], state.TAB_CONFIG).run()
    _assert_clean(at)
    ver = at.session_state[state.K_CONFIG_VERSION]
    cap = at.number_input(key=f"cfg{ver}_college_cap")
    assert cap.label == "Max share of shortlist per college (%)" and cap.value == 0.0
    cap.set_value(25.0)
    at.button(key="cfg_apply").click().run()
    _assert_clean(at)
    assert at.session_state[state.K_CONFIG].shortlist.college_cap_pct == 25.0
    assert engine["calls"][-1]["cfg"].shortlist.college_cap_pct == 25.0


def test_preview_is_disabled_before_the_first_run() -> None:
    at = _app().run()
    _assert_clean(at)
    assert at.button(key="cfg_preview").disabled
    assert any("available after the first screening run" in c.value for c in at.caption)


def test_preview_shows_the_diff_and_apply_uses_it(engine: dict[str, Any]) -> None:
    at = _inject(_app(), engine["prepared"], engine["result"], state.TAB_CONFIG).run()
    assert not at.button(key="cfg_preview").disabled
    _preview(at, top_n=10)
    assert len(engine["calls"]) == 1 and engine["calls"][0]["cfg"].shortlist.top_n == 10
    assert engine["calls"][0]["risk_reused"]                          # risk settings unchanged
    assert at.session_state[state.K_CONFIG].shortlist.top_n == 15     # nothing applied yet
    metrics = _metrics(at)
    assert (metrics["Shortlisted"], metrics["Entering"], metrics["Leaving"]) == ("10", "0", "5")
    assert next(m for m in at.metric if m.label == "Shortlisted").delta == "-5"
    assert {"Entering (0)", "Leaving (5)", "Status changes (5)", "Biggest rank moves"} <= {t.label for t in at.tabs}
    expected = analytics.shortlist_diff(engine["result"].candidates, scoring.apply_shortlist(
        engine["result"].candidates, engine["calls"][0]["cfg"]))["leaving"][S.CANDIDATE_ID].tolist()
    assert any(d.value[S.CANDIDATE_ID].tolist() == expected for d in at.dataframe)
    assert any("top n: 15 → 10" in c.value for c in at.caption)

    at.button(key="cfg_preview_apply").click().run()
    _assert_clean(at)
    assert at.session_state[state.K_CONFIG].shortlist.top_n == 10
    assert tab_config.K_PREVIEW not in at.session_state
    assert not any(m.label == "Leaving" for m in at.metric)            # panel closed


def test_preview_of_a_college_cap_lists_capped_candidates(engine: dict[str, Any]) -> None:
    at = _inject(_app(), engine["prepared"], engine["result"], state.TAB_CONFIG).run()
    _preview(at, college_cap=20.0)
    diff = at.session_state[tab_config.K_PREVIEW]["diff"]
    assert diff["summary"]["capped_after"] > 0 and diff["summary"]["shortlisted_after"] == 15
    assert diff["leaving"]["reason_after"].str.startswith("College cap reached").all()
    assert _metrics(at)["Leaving"] == str(len(diff["leaving"]))


def test_discard_preview_keeps_the_active_config(engine: dict[str, Any]) -> None:
    at = _inject(_app(), engine["prepared"], engine["result"], state.TAB_CONFIG).run()
    ver = at.session_state[state.K_CONFIG_VERSION]
    _preview(at, top_n=10)
    at.button(key="cfg_preview_discard").click().run()
    _assert_clean(at)
    assert tab_config.K_PREVIEW not in at.session_state
    assert at.session_state[state.K_CONFIG].shortlist.top_n == 15
    new_ver = at.session_state[state.K_CONFIG_VERSION]
    assert new_ver == ver + 1 and at.number_input(key=f"cfg{new_ver}_top_n").value == 15
    assert len(engine["calls"]) == 1                                  # discarding does not re-score


def test_preview_recomputes_risk_only_when_risk_settings_change(engine: dict[str, Any]) -> None:
    at = _inject(_app(), engine["prepared"], engine["result"], state.TAB_CONFIG).run()
    _preview(at, risk_high_threshold=75.0)
    assert engine["calls"][-1]["cfg"].risk.high_threshold == 75.0
    assert not engine["calls"][-1]["risk_reused"]


def test_preview_is_dropped_when_the_config_changes_elsewhere(engine: dict[str, Any]) -> None:
    at = _inject(_app(), engine["prepared"], engine["result"], state.TAB_CONFIG).run()
    _preview(at, top_n=10)
    assert tab_config.K_PREVIEW in at.session_state
    at.button(key="cfg_reset").click().run()
    _assert_clean(at)
    assert tab_config.K_PREVIEW not in at.session_state
    assert not any(m.label == "Leaving" for m in at.metric)


def test_preview_failure_is_reported_not_raised(engine: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(prep, cfg, llm_results=None, *, risk_result=None):
        raise RuntimeError("scoring backend unavailable")

    _patch(monkeypatch, pipeline, "evaluate", broken)
    at = _inject(_app(), engine["prepared"], engine["result"], state.TAB_CONFIG).run()
    _preview(at, top_n=10)
    assert any("Could not preview these settings" in e.value for e in at.error)
    assert tab_config.K_PREVIEW not in at.session_state
    assert at.session_state[state.K_CONFIG].shortlist.top_n == 15


def test_config_changes_lists_differences() -> None:
    old, new = AppConfig(), AppConfig()
    new.shortlist.top_n = 250
    new.shortlist.college_cap_pct = 10.0
    new.skills.required_skills = ["Python", "Go"]
    new.experience.job_description = "Backend role. " * 10
    changes = tab_config.config_changes(old, new)
    assert "shortlist › top n: 300 → 250" in changes
    assert "shortlist › college cap pct: 0 → 10" in changes
    assert "skills › required skills: Python, Java, SQL, Cloud, PyTorch → Python, Go" in changes
    assert "experience › job description edited" in changes
    assert tab_config.config_changes(old, AppConfig()) == []
