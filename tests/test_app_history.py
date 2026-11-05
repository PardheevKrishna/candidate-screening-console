"""AppTest flows for the Run History tab: save, reopen, re-tune, rename/delete and compare.

Runs are written to a pytest tmp folder (SCREENING_RUNS_DIR); data is synthetic
(tests/ui_fixtures.py); no network or real API key is used. app.py shows feature tabs only once
they are listed in ENABLED_FEATURE_TABS, so these tests run app.py with the Run History tab on.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from screening import pipeline, runs
from screening import schema as S
from screening.config import AppConfig
from screening.models import PipelineResult
from ui import state, tab_history

try:
    from tests.ui_fixtures import make_fake_prepared_and_result
except ImportError:  # pragma: no cover - tests/ imported as rootdir-relative modules
    from ui_fixtures import make_fake_prepared_and_result

APP = Path(__file__).resolve().parent.parent / "app.py"
FP = "fake-fingerprint"
ENABLE_HISTORY = ('try:\n    ENABLED_FEATURE_TABS[state.TAB_HISTORY] = "ui.tab_history"\n'
                  "except (NameError, TypeError):  # app.py no longer gates feature tabs\n    pass\n")


def _app_script() -> str:
    """app.py with the Run History tab switched on (inserted before the first Streamlit call)."""
    source = APP.read_text(encoding="utf-8")
    return re.sub(r"(?m)^st\.set_page_config\(", lambda m: ENABLE_HISTORY + m.group(0), source, count=1)


@pytest.fixture(autouse=True)
def runs_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    folder = tmp_path / "runs"
    monkeypatch.setenv(runs.ENV_RUNS_DIR, str(folder))
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.setitem(vars(pipeline), "DEFAULT_SAMPLE_DIR", tmp_path / "no-sample")
    return folder


@pytest.fixture
def evaluate_calls(monkeypatch: pytest.MonkeyPatch) -> list[AppConfig]:
    """Real pipeline.evaluate, recording each call."""
    real, calls = pipeline.evaluate, []

    def recording(prepared: Any, cfg: AppConfig, llm_results: Any = None, *, risk_result: Any = None) -> Any:
        calls.append(cfg)
        return real(prepared, cfg, llm_results, risk_result=risk_result)

    monkeypatch.setitem(vars(pipeline), "evaluate", recording)
    return calls


def _app() -> AppTest:
    at = AppTest.from_string(_app_script(), default_timeout=60)
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


def _clean(at: AppTest) -> None:
    assert not at.exception, [e.value for e in at.exception]


def _evaluated(prepared: Any, cfg: AppConfig, top_n: int) -> PipelineResult:
    tuned = AppConfig.from_dict(cfg.to_dict())
    tuned.shortlist.top_n = top_n
    return pipeline.evaluate(prepared, tuned)


def test_history_tab_before_first_run_with_no_saved_runs(runs_dir: Path) -> None:
    at = _app().run()
    _clean(at)
    assert state.TAB_HISTORY in [t.label for t in at.tabs]
    assert any("No saved runs yet" in i.value for i in at.info)
    assert at.button(key="hist_save").disabled
    assert any("candidate personal data" in c.value for c in at.caption)
    assert not runs_dir.exists()  # rendering never creates the runs folder


def test_save_then_load_in_a_new_session(runs_dir: Path, evaluate_calls: list[AppConfig]) -> None:
    prepared, result = make_fake_prepared_and_result()
    explanation = {"ok": True, "summary": "Strong problem solver.", "strengths": ["DSA"], "gaps": [],
                   "interview_questions": [], "integrity_questions": [], "error": ""}
    at = _inject(_app(), prepared, result, state.TAB_HISTORY)
    at.session_state[state.K_LLM_EXPLAIN] = {"CAND-00003": explanation}
    at.run()
    _clean(at)
    assert not at.button(key="hist_save").disabled
    at.text_input(key=tab_history.K_LABEL).set_value("North campus drive")
    at.button(key="hist_save").click().run()
    _clean(at)
    listed = runs.RunStore(runs_dir).list_runs()
    assert listed["label"].tolist() == ["North campus drive"] and listed.loc[0, "dataset"] == "Fake dataset"
    run_id = listed.loc[0, "run_id"]
    assert at.session_state[state.K_LOADED_RUN] == run_id
    assert at.button(key="hist_save").disabled  # nothing changed since saving
    assert any("Saved run" in t.value for t in at.toast)

    fresh = _app().run()  # a new session, before any screening
    _clean(fresh)
    assert fresh.selectbox(key=tab_history.K_PICK).value == run_id
    fresh.button(key="hist_load").click().run()
    _clean(fresh)
    ss = fresh.session_state
    loaded = ss[state.K_RESULT]
    pd.testing.assert_frame_equal(loaded.candidates, result.candidates, check_dtype=False)
    assert ss[state.K_LOADED_RUN] == run_id and ss[state.K_PREPARED_FP] == f"run:{run_id}"
    assert ss[state.K_DATASET] == "Saved run: North campus drive"
    assert ss[state.K_RESULT_KEY] == state.eval_key(f"run:{run_id}", loaded.config, None)
    assert ss[state.K_CONFIG] == result.config
    assert ss[state.K_LLM_EXPLAIN] == {"CAND-00003": explanation}
    assert isinstance(ss[state.K_PREPARED], pipeline.PreparedData)
    assert evaluate_calls == []  # restored exactly as saved: nothing re-evaluated
    assert "Total Processed" in "\n".join(h.value for h in fresh.html)
    assert any("Opened saved run" in t.value for t in fresh.toast)


def test_loaded_run_can_be_retuned_and_saved_as_new_run(runs_dir: Path, evaluate_calls: list[AppConfig]) -> None:
    store = runs.RunStore(runs_dir)
    prepared, result = make_fake_prepared_and_result()
    store.save(prepared, result, "Baseline", dataset_label="Fake dataset")
    at = _app().run()
    at.button(key="hist_load").click().run()
    _clean(at)
    at.session_state[state.K_TABS] = state.TAB_HISTORY
    at.run()
    _clean(at)
    assert at.button(key="hist_save").disabled
    assert any("viewing saved run" in i.value for i in at.info)

    tuned = AppConfig.from_dict(at.session_state[state.K_CONFIG].to_dict())
    tuned.shortlist.top_n = 10
    at.session_state[state.K_CONFIG] = tuned
    at.run()  # re-scored from the loaded prepared data
    _clean(at)
    assert len(evaluate_calls) == 1 and evaluate_calls[0].shortlist.top_n == 10
    assert not at.button(key="hist_save").disabled
    assert at.text_input(key=tab_history.K_LABEL).value.startswith("Fake dataset · ")
    at.button(key="hist_save").click().run()
    _clean(at)
    listed = store.list_runs()
    assert listed["shortlisted"].tolist() == [10, 15]
    assert listed["dataset"].tolist() == ["Fake dataset", "Fake dataset"]


def test_rename_and_two_step_delete(runs_dir: Path) -> None:
    store = runs.RunStore(runs_dir)
    prepared, result = make_fake_prepared_and_result()
    keep = store.save(prepared, result, "Keep me")
    drop = store.save(prepared, result, "Drop me")
    at = _app().run()
    _clean(at)
    at.selectbox(key=tab_history.K_PICK).set_value(drop).run()
    assert at.button(key="hist_delete").disabled  # needs the confirmation tick first
    at.checkbox(key=f"hist_delete_confirm_{drop}").check().run()
    assert not at.button(key="hist_delete").disabled
    at.button(key="hist_delete").click().run()
    _clean(at)
    assert store.list_runs()["run_id"].tolist() == [keep]
    assert at.selectbox(key=tab_history.K_PICK).value == keep

    assert at.button(key="hist_rename").disabled  # label unchanged
    at.text_input(key=f"hist_rename_{keep}").set_value("Kept run").run()
    at.button(key="hist_rename").click().run()
    _clean(at)
    assert store.list_runs()["label"].tolist() == ["Kept run"]


def test_compare_two_saved_runs(runs_dir: Path) -> None:
    store = runs.RunStore(runs_dir)
    prepared, result = make_fake_prepared_and_result()
    run_a = store.save(prepared, _evaluated(prepared, result.config, 15), "Top 15")
    run_b = store.save(prepared, _evaluated(prepared, result.config, 10), "Top 10")
    at = _app().run()
    _clean(at)
    assert (at.selectbox(key=tab_history.K_CMP_A).value, at.selectbox(key=tab_history.K_CMP_B).value) == (run_a, run_b)
    metrics = {m.label: m.value for m in at.metric}
    assert (metrics["Shortlisted (B)"], metrics["Entering shortlist"], metrics["Leaving shortlist"]) == ("10", "0", "5")
    tables = [d.value for d in at.dataframe]
    leaving = next(t for t in tables if "shortlisted_before" in t.columns and len(t) == 5)
    assert leaving["shortlisted_before"].all()
    changes = next(t for t in tables if "setting" in t.columns)
    assert changes.to_dict("records") == [{"setting": "Shortlist › top n", "run_a": "15", "run_b": "10"}]


def test_storage_problems_show_errors_not_exceptions(runs_dir: Path, tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    prepared, result = make_fake_prepared_and_result()
    broken = "20260101-000000-abcdef"
    (runs_dir / broken).mkdir(parents=True)
    (runs_dir / broken / "meta.json").write_text("{damaged")
    at = _app().run()
    _clean(at)
    at.button(key="hist_load").click().run()
    _clean(at)
    assert any("could not be opened" in e.value for e in at.error)
    assert [t.label for t in at.tabs][0] == "Getting started"  # nothing was half-loaded

    not_a_folder = tmp_path / "not-a-folder"
    not_a_folder.write_text("x")
    monkeypatch.setenv(runs.ENV_RUNS_DIR, str(not_a_folder))
    at = _inject(_app(), prepared, result, state.TAB_HISTORY).run()
    _clean(at)
    at.button(key="hist_save").click().run()
    _clean(at)
    assert any("could not be saved" in e.value for e in at.error)
    assert str(not_a_folder) not in "\n".join(e.value for e in at.error)


def test_result_without_prepared_data_cannot_be_saved(runs_dir: Path) -> None:
    minimal = pd.DataFrame({S.CANDIDATE_ID: ["A-1", "A-2"], S.NAME: ["X", None], S.FINAL_SCORE: [81.0, None],
                            S.STATUS: [S.STATUS_SHORTLISTED, ""]})
    _, fake_result = make_fake_prepared_and_result()
    result = PipelineResult(candidates=minimal, parsed_resumes={}, parsed_by_candidate={}, matches=pd.DataFrame(),
                            risk=fake_result.risk, quality=fake_result.quality, config=fake_result.config)
    at = _app()
    at.session_state[state.K_RESULT] = result
    at.session_state[state.K_TABS] = state.TAB_HISTORY
    at.run()
    _clean(at)
    assert at.button(key="hist_save").disabled
    assert any("prepared data is not in this session" in c.value for c in at.caption)
    assert not runs_dir.exists()


def test_pure_helpers(tmp_path: Path) -> None:
    assert tab_history.storage_location(tab_history.PROJECT_ROOT / "runs") == "runs"
    assert tab_history.storage_note(tab_history.PROJECT_ROOT / "runs") == \
        "Runs are stored on the server in the app's `runs` folder."
    outside = tmp_path / "screening-runs"
    assert tab_history.storage_location(outside) == "screening-runs"
    assert str(tmp_path) not in tab_history.storage_note(outside)  # never the absolute server path
    assert tab_history.setting_label("risk.pair_weights.same_ip") == "Integrity risk › pair weights › same ip"
    assert tab_history.format_value(["Python", "SQL"]) == "Python, SQL" and tab_history.format_value(None) == "(not set)"
    assert tab_history.format_value(True) == "Yes" and tab_history.format_value(0.5) == "0.5"
    assert tab_history.format_value("x" * 500).endswith("…") and len(tab_history.format_value("x" * 500)) == 140
    saved = pd.DataFrame({"run_id": ["20261105-101010-abcdef"], "label": ["Drive"], "mode": ["ta+resumes"],
                          "created_at": [pd.Timestamp("2026-11-05 10:10")]})
    assert tab_history.run_labels(saved) == {"20261105-101010-abcdef": "Drive · 05 Nov 2026 10:10"}
    view = tab_history.runs_view(saved, "20261105-101010-abcdef")
    assert view.loc[0, "open"] and view.loc[0, "mode"] == state.MODE_LABELS["ta+resumes"]
    assert S.CANDIDATE_ID in tab_history.DIFF_ORDER["entering"]
