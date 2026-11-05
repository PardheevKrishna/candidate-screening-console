"""UI tests for the Ask the Data tab (AppTest). No network and no real API key: the LLM
client is faked and the screening result comes from tests/ui_fixtures.py."""
from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import pytest
from streamlit.testing.v1 import AppTest

from screening import llm, pipeline
from screening import schema as S
from screening.models import PipelineResult
from ui import state, tab_ask

try:
    from tests.ui_fixtures import make_fake_prepared_and_result
except ImportError:  # pragma: no cover - tests/ imported as rootdir-relative modules
    from ui_fixtures import make_fake_prepared_and_result

APP = str(Path(__file__).resolve().parent.parent / "app.py")
FP = "fake-fingerprint"
# Renders only this tab, so the tests do not depend on which feature tabs app.py enables.
HARNESS = """
import streamlit as st
from ui import state, tab_ask
state.init_state()
tab_ask.render(st.session_state.get(state.K_RESULT))
"""
SHORTLISTED = [S.STATUS_SHORTLISTED, S.STATUS_REVIEW]
PERFECT_PER_COLLEGE = {
    "filters": [{"column": "hackerrank_pct", "op": ">=", "value": 100},
                {"column": "status", "op": "in", "value": SHORTLISTED}],
    "group_by": ["college"], "metrics": [{"column": "candidate_id", "agg": "count", "as": "candidates"}],
    "columns": [], "sort": [{"column": "candidates", "desc": True}], "limit": 50, "chart": "bar",
    "explanation": "Counts shortlisted perfect scorers per college.",
}


class FakeLLM:
    """Replaces llm.LLMClient: records every prompt and answers from REPLIES (by question)."""

    calls: list[dict[str, Any]] = []
    replies: dict[str, Any] = {}

    def __init__(self, cfg: Any, api_key: str | None, client: Any = None) -> None:
        self.cfg = cfg

    def chat_json(self, system: str, user: str, *, max_tokens: int | None = None,
                  required_keys: tuple[str, ...] = ()) -> Any:
        FakeLLM.calls.append({"system": system, "user": user, "required_keys": required_keys})
        question = user.split("\n", 1)[0].removeprefix("Question: ")
        reply = FakeLLM.replies.get(question, FakeLLM.replies.get("*"))
        if isinstance(reply, BaseException):
            raise reply
        return reply


@pytest.fixture(scope="module")
def fake() -> tuple[pipeline.PreparedData, PipelineResult]:
    return make_fake_prepared_and_result()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.setitem(vars(pipeline), "DEFAULT_SAMPLE_DIR", tmp_path / "no-sample")
    FakeLLM.calls = []
    FakeLLM.replies = {}


@pytest.fixture
def llm_on(monkeypatch: pytest.MonkeyPatch) -> type[FakeLLM]:
    monkeypatch.setenv("NVIDIA_API_KEY", "dummy-test-value")
    monkeypatch.setitem(vars(llm), "LLMClient", FakeLLM)
    return FakeLLM


def _harness(result: PipelineResult, prepared: Any = None) -> AppTest:
    at = AppTest.from_string(HARNESS, default_timeout=60)
    at.secrets["NVIDIA_API_KEY"] = ""  # never read a real secrets.toml during tests
    at.session_state[state.K_CONFIG] = result.config
    at.session_state[state.K_RESULT] = result
    at.session_state[state.K_PREPARED_FP] = FP
    at.session_state[state.K_RESULT_KEY] = state.eval_key(FP, result.config, None)
    if prepared is not None:
        at.session_state[state.K_PREPARED] = prepared
    return at


def _assert_clean(at: AppTest) -> None:
    assert not at.exception, [e.value for e in at.exception]


def _history(at: AppTest) -> list[dict[str, Any]]:
    return at.session_state[state.K_ASK_HISTORY]


def _personal_values(result: PipelineResult) -> list[str]:
    df = result.candidates
    return [str(v) for col in (S.NAME, S.EMAIL, S.PHONE, S.IP_ADDRESS, S.RESUME_FILE, "extra_recruiter_notes")
            for v in df[col].dropna().unique() if str(v).strip()]


# ---------------------------------------------------------------------------
# Tab behaviour
# ---------------------------------------------------------------------------
def test_tab_without_llm_is_disabled_with_a_friendly_message(fake: tuple[Any, PipelineResult]) -> None:
    at = _harness(fake[1]).run()
    _assert_clean(at)
    assert at.chat_input(key=tab_ask.K_INPUT).disabled
    assert at.button_group(key=tab_ask.K_EXAMPLES).disabled
    assert at.button(key=tab_ask.K_CLEAR).disabled                       # nothing to clear yet
    assert any("No LLM API key configured" in i.value for i in at.info)
    assert any("never candidate rows" in c.value for c in at.caption)
    assert at.button_group(key=tab_ask.K_EXAMPLES).options == tab_ask.EXAMPLE_QUESTIONS
    assert _history(at) == [] and FakeLLM.calls == []


def test_tab_without_a_result_asks_for_a_run() -> None:
    at = AppTest.from_string(HARNESS, default_timeout=60)
    at.secrets["NVIDIA_API_KEY"] = ""
    at.run()
    _assert_clean(at)
    assert any("Run a screening first" in i.value for i in at.info)


def test_example_question_is_answered_with_a_fake_client(fake: tuple[Any, PipelineResult],
                                                         llm_on: type[FakeLLM]) -> None:
    llm_on.replies = {tab_ask.EXAMPLE_QUESTIONS[0]: PERFECT_PER_COLLEGE}
    at = _harness(fake[1]).run()
    _assert_clean(at)
    assert not at.chat_input(key=tab_ask.K_INPUT).disabled
    at.button_group(key=tab_ask.K_EXAMPLES).set_value(tab_ask.EXAMPLE_QUESTIONS[0]).run()
    _assert_clean(at)

    (entry,) = _history(at)
    assert entry["error"] == "" and entry["question"] == tab_ask.EXAMPLE_QUESTIONS[0]
    df = fake[1].candidates
    expected = df[df[S.STATUS].isin(SHORTLISTED) & (df[S.HR_PCT] >= 100)].groupby(S.COLLEGE).size()
    table = entry["result"]
    assert dict(zip(table[S.COLLEGE], table["candidates"])) == expected.to_dict()
    assert entry["answer"].startswith(f"{len(expected)} groups · grouped by College · 2 filters")
    assert at.session_state[tab_ask.K_EXAMPLES] is None                  # pill reset for the next pick

    shown = at.dataframe[0].value
    assert shown[S.COLLEGE].tolist() == table[S.COLLEGE].tolist()
    assert [m.name for m in at.chat_message] == ["user", "assistant"]
    assert any(entry["answer"].split(" · ")[0] in m.value for m in at.markdown)
    assert any("Counts shortlisted perfect scorers per college." in c.value for c in at.caption)
    assert len(at.get("plotly_chart")) == 1                              # group_by + one metric -> bar chart
    assert at.download_button(key="ask_dl_1").label == "Download CSV"
    assert json.loads(at.json[0].value)["group_by"] == ["college"]      # "How this was answered"
    assert not at.button(key=tab_ask.K_CLEAR).disabled

    (call,) = llm_on.calls
    prompt = call["system"] + call["user"]
    for value in _personal_values(fake[1]):
        assert value not in prompt


def test_typed_question_masks_names_and_lists_the_candidate(fake: tuple[Any, PipelineResult],
                                                             llm_on: type[FakeLLM]) -> None:
    df = fake[1].candidates
    row = df[df[S.CANDIDATE_ID] == "CAND-00011"].iloc[0]
    llm_on.replies = {"*": {"filters": [{"column": "name", "op": "contains", "value": "[NAME_1]"}],
                            "columns": ["candidate_id", "name", "status", "final_score"],
                            "explanation": "Looks up [NAME_1]."}}
    at = _harness(fake[1]).run()
    at.chat_input(key=tab_ask.K_INPUT).set_value(f"What is the status of {row[S.NAME]}?").run()
    _assert_clean(at)
    (entry,) = _history(at)
    assert entry["error"] == "", entry["error"]
    assert row[S.CANDIDATE_ID] in entry["result"][S.CANDIDATE_ID].tolist()
    assert entry["plan"]["filters"][0]["value"] == row[S.NAME]
    (call,) = llm_on.calls
    assert "[NAME_1]" in call["user"]
    first, last = row[S.NAME].split()
    for token in (first, last):
        assert not re.search(rf"\b{token}\b", call["system"] + call["user"], re.IGNORECASE)


def test_failures_and_unsupported_questions_are_shown_politely(fake: tuple[Any, PipelineResult],
                                                               llm_on: type[FakeLLM]) -> None:
    llm_on.replies = {
        "Predict who will accept the offer": {"unsupported": True,
                                              "explanation": "Offer acceptance is not in the data."},
        "Break it": llm.LLMError("Rate limited by the LLM provider. Gave up after 5 attempts."),
        "Use a bad column": {"columns": ["salary"], "explanation": "Lists salaries."},
    }
    at = _harness(fake[1]).run()
    for question in llm_on.replies:
        at.chat_input(key=tab_ask.K_INPUT).set_value(question).run()
        _assert_clean(at)
    unsupported, failed, invalid = _history(at)
    assert unsupported["unsupported"] and "Offer acceptance is not in the data." in unsupported["error"]
    assert "Rate limited" in failed["error"] and not failed["unsupported"]
    assert "Unknown column 'salary'" in invalid["error"]
    assert any("Offer acceptance" in i.value for i in at.info)
    errors = " ".join(e.value for e in at.error)
    assert "Rate limited" in errors and "salary" in errors
    assert len(at.json) == 2          # the unsupported reply and the rejected plan stay inspectable


def test_clear_conversation_and_new_dataset_reset_history(fake: tuple[Any, PipelineResult],
                                                          llm_on: type[FakeLLM]) -> None:
    llm_on.replies = {"*": PERFECT_PER_COLLEGE}
    at = _harness(fake[1]).run()
    at.chat_input(key=tab_ask.K_INPUT).set_value("first question").run()
    at.chat_input(key=tab_ask.K_INPUT).set_value("second question").run()
    _assert_clean(at)
    assert [e["question"] for e in _history(at)] == ["first question", "second question"]
    assert [e["id"] for e in _history(at)] == [1, 2]
    at.button(key=tab_ask.K_CLEAR).click().run()
    _assert_clean(at)
    assert _history(at) == []

    at.chat_input(key=tab_ask.K_INPUT).set_value("third question").run()
    assert len(_history(at)) == 1
    at.session_state[state.K_PREPARED_FP] = "another-dataset"
    at.run()
    _assert_clean(at)
    assert _history(at) == []


def test_rescoring_marks_older_answers(fake: tuple[Any, PipelineResult], llm_on: type[FakeLLM]) -> None:
    llm_on.replies = {"*": PERFECT_PER_COLLEGE}
    at = _harness(fake[1]).run()
    at.chat_input(key=tab_ask.K_INPUT).set_value("per college").run()
    assert not any("Answered before the latest re-scoring" in c.value for c in at.caption)
    at.session_state[state.K_RESULT_KEY] = "re-scored"
    at.run()
    _assert_clean(at)
    assert any("Answered before the latest re-scoring" in c.value for c in at.caption)


# ---------------------------------------------------------------------------
# Full app (runs once app.py enables the tab)
# ---------------------------------------------------------------------------
def test_full_app_ask_tab(fake: tuple[Any, PipelineResult], monkeypatch: pytest.MonkeyPatch) -> None:
    prepared, result = fake
    monkeypatch.setitem(vars(pipeline), "evaluate",
                        lambda prep, cfg, llm_results=None, *, risk_result=None: replace(result, config=cfg))
    at = AppTest.from_file(APP, default_timeout=60)
    at.secrets["NVIDIA_API_KEY"] = ""
    at.session_state[state.K_CONFIG] = result.config
    at.session_state[state.K_PREPARED] = prepared
    at.session_state[state.K_PREPARED_FP] = FP
    at.session_state[state.K_RESULT] = result
    at.session_state[state.K_RESULT_KEY] = state.eval_key(FP, result.config, None)
    at.session_state[state.K_TABS] = state.TAB_ASK
    at.run()
    _assert_clean(at)
    tabs = [t for t in at.tabs if t.label == state.TAB_ASK]
    if not tabs:
        pytest.skip("The Ask the Data tab is not enabled in app.py yet.")
    assert tabs[0].children
    assert at.chat_input(key=tab_ask.K_INPUT).disabled                    # no API key in tests


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def test_bar_figure_and_chart_metric() -> None:
    table = pd.DataFrame({"college": ["A", "B", None], "candidates": [5, 3, 1]})
    plan = {"group_by": ["college"], "metrics": [{"column": None, "agg": "count", "as": "candidates"}],
            "chart": "bar"}
    assert tab_ask.chart_metric(plan, table) == "candidates"
    fig = tab_ask.bar_figure(table, ["college"], "candidates")
    assert isinstance(fig, go.Figure) and list(fig.data[0].y) == ["A", "B", "(none)"]
    assert list(fig.data[0].x) == [5, 3, 1] and list(fig.data[0].text) == ["5", "3", "1"]
    two_keys = pd.DataFrame({"a": ["x", "x"], "b": [1.0, 2.0], "n": [2.5, 1.25]})
    fig2 = tab_ask.bar_figure(two_keys, ["a", "b"], "n")
    assert list(fig2.data[0].y) == ["x · 1", "x · 2"] and list(fig2.data[0].text) == ["2.50", "1.25"]
    assert tab_ask.chart_metric({**plan, "chart": "none"}, table) is None
    assert tab_ask.chart_metric({**plan, "metrics": plan["metrics"] * 2}, table) is None
    assert tab_ask.chart_metric(plan, table.iloc[0:0]) is None
    for empty in (tab_ask.bar_figure(table.iloc[0:0], ["college"], "candidates"),
                  tab_ask.bar_figure(table, ["college"], "missing"),
                  tab_ask.bar_figure(pd.DataFrame({"college": ["A"], "n": [None]}), ["college"], "n")):
        assert isinstance(empty, go.Figure) and empty.layout.annotations


def test_csv_bytes_neutralises_formulas() -> None:
    data = tab_ask.csv_bytes(pd.DataFrame({"college": ["=HYPERLINK(\"x\")", "Lakeside"], "n": [1, 2]}))
    text = data.decode("utf-8-sig")
    assert text.splitlines()[0] == "college,n" and "'=HYPERLINK" in text
