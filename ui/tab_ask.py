"""Ask the Data tab: plain-English questions about the current screening run.

The LLM only turns a question into a query plan (screening.ask). The plan is validated
against the allowed columns, operators and aggregations and executed locally on the
current results; the model never receives candidate rows.
"""
from __future__ import annotations

import logging
import math
from functools import partial
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from screening import ask, export
from screening.config import AppConfig
from screening.models import PipelineResult
from ui import charts, state, theme

log = logging.getLogger(__name__)

EXAMPLE_QUESTIONS = [
    "How many perfect scorers are shortlisted per college?",
    "Which colleges have the most High-risk candidates?",
    "Average final score by college tier",
    "List shortlisted candidates from Pune with CGPA above 9",
    "How many candidates are under integrity review by cluster?",
]
INTRO = (
    "Ask about the current screening results in plain English: counts and averages by college, tier, status, "
    "risk level or cluster, or a filtered list of candidates. Answers are computed locally on this run's data. "
    "The LLM only turns your question into a query plan: it sees the question and descriptions of the columns, "
    "never candidate rows, and names, emails, phone numbers and IP addresses you type are replaced with "
    "placeholders before the question is sent."
)
HOW_ANSWERED = (
    "The LLM produced this plan from your question (personal details replaced by placeholders) and the column "
    "descriptions. It was checked against the allowed columns, operators and aggregations, then run locally "
    "on the current results."
)
K_EXAMPLES = "ask_examples"
K_INPUT = "ask_input"
K_CLEAR = "ask_clear"
K_PENDING = "_ask_pending_question"
K_SEQ = "_ask_seq"
K_DATA_FP = "_ask_data_fingerprint"
MAX_HISTORY = 20
CHART_MAX_BARS = 30
_FONT = "Inter, system-ui, -apple-system, Segoe UI, Roboto, sans-serif"


# ---------------------------------------------------------------------------
# Session helpers / callbacks
# ---------------------------------------------------------------------------
def history() -> list[dict[str, Any]]:
    """Conversation entries: {id, question, plan, result (table), answer (summary), error, ...}."""
    entries = st.session_state.get(state.K_ASK_HISTORY)
    if not isinstance(entries, list):
        entries = []
        st.session_state[state.K_ASK_HISTORY] = entries
    return entries


def _clear_history() -> None:
    st.session_state[state.K_ASK_HISTORY] = []


def _pick_example() -> None:
    """Example pill clicked -> ask it on this run; reset the pill so it can be picked again."""
    picked = st.session_state.get(K_EXAMPLES)
    if picked:
        st.session_state[K_PENDING] = str(picked)
    st.session_state[K_EXAMPLES] = None


def _forget_answers_for_other_data() -> None:
    """A different dataset makes earlier answers meaningless: start a fresh conversation."""
    fingerprint = st.session_state.get(state.K_PREPARED_FP)
    if st.session_state.get(K_DATA_FP) != fingerprint:
        st.session_state[K_DATA_FP] = fingerprint
        _clear_history()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def csv_bytes(table: pd.DataFrame) -> bytes:
    """export.to_csv_bytes (formula-injection safe) with a plain-pandas fallback."""
    fn = state.engine_fn(export, "to_csv_bytes")
    if fn is not None:
        try:
            return fn(table)
        except Exception as exc:  # noqa: BLE001
            log.warning("to_csv_bytes failed (%s); using pandas fallback", type(exc).__name__)
    return table.to_csv(index=False).encode("utf-8-sig")


def _display(value: Any) -> str:
    if value is None or value is pd.NA or (isinstance(value, float) and math.isnan(value)):
        return "(none)"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float) and value.is_integer():
        return f"{value:.0f}"
    return str(value)


def _number(value: float) -> str:
    if value is None or math.isnan(value):
        return "-"
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"


def bar_figure(table: pd.DataFrame, group_by: list[str], metric: str, max_bars: int = CHART_MAX_BARS) -> go.Figure:
    """Horizontal bars of one metric per group, in the answer's order (first ``max_bars`` groups)."""
    if (not isinstance(table, pd.DataFrame) or table.empty or metric not in table.columns or not group_by
            or any(g not in table.columns for g in group_by)):
        return charts.empty_figure("Nothing to chart for this answer.")
    data = table.head(max_bars)
    values = pd.to_numeric(data[metric], errors="coerce").astype("float64")
    if values.notna().sum() == 0:
        return charts.empty_figure("This answer has no numeric values to chart.")
    labels: list[str] = []
    for row in data[list(group_by)].itertuples(index=False, name=None):
        label = " · ".join(_display(v) for v in row)
        while label in labels:          # a categorical axis would merge duplicate labels
            label += " "
        labels.append(label)
    text = [_number(v) for v in values]
    fig = go.Figure(go.Bar(
        x=values, y=labels, orientation="h", marker=dict(color=theme.RED), text=text, textposition="outside",
        cliponaxis=False, hovertemplate="%{y}: %{text}<extra></extra>"))
    fig.update_yaxes(type="category", autorange="reversed", automargin=True)
    top = float(values.max()) if values.notna().any() else 0.0
    if float(values.min()) >= 0 and top > 0:
        fig.update_xaxes(range=[0, top * 1.2])
    if bool((values.dropna() % 1 == 0).all()) and 0 < top <= 10:
        fig.update_xaxes(dtick=1)       # whole counts: no 0.5 ticks
    fig.update_xaxes(title=metric)
    fig.update_layout(
        template="plotly_white", height=max(220, 28 * len(data) + 90), showlegend=False, bargap=0.25,
        margin=dict(l=10, r=10, t=16, b=10), font=dict(family=_FONT, size=12, color=theme.SLATE_2),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="#FFFFFF",
        hoverlabel=dict(bgcolor="#FFFFFF", font=dict(family=_FONT, size=12, color=theme.SLATE)))
    return fig


def chart_metric(plan: dict[str, Any], table: pd.DataFrame | None) -> str | None:
    """The metric to chart when the plan asks for a bar chart of one metric per group."""
    metrics = [m for m in plan.get("metrics") or [] if isinstance(m, dict)]
    if (plan.get("chart") != "bar" or not plan.get("group_by") or len(metrics) != 1
            or not isinstance(table, pd.DataFrame) or table.empty):
        return None
    alias = metrics[0].get("as")
    if alias not in table.columns or not pd.api.types.is_numeric_dtype(table[alias].dtype):
        return None
    return str(alias)


def _column_config(table: pd.DataFrame, plan: dict[str, Any]) -> dict[str, Any]:
    aliases = {m.get("as") for m in plan.get("metrics") or [] if isinstance(m, dict)}
    return {c: st.column_config.Column(ask.column_label(c)) for c in table.columns
            if isinstance(c, str) and c not in aliases}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _how_answered(plan: dict[str, Any]) -> None:
    with st.expander("How this was answered", icon=":material/account_tree:"):
        st.caption(HOW_ANSWERED)
        st.json(plan, expanded=True)


def _render_answer(entry: dict[str, Any], stale: bool) -> None:
    eid = entry.get("id", 0)
    plan = entry.get("plan") if isinstance(entry.get("plan"), dict) else {}
    error = str(entry.get("error") or "")
    if error:
        if entry.get("unsupported"):
            st.info(theme.md_escape(error), icon=":material/help:")
        else:
            st.error(theme.md_escape(error), icon=":material/error:")
        if plan:
            _how_answered(plan)
        return
    table = entry.get("result")
    if not isinstance(table, pd.DataFrame):
        st.warning("This answer is no longer available. Ask the question again.")
        return
    st.markdown(f"**{theme.md_escape(entry.get('answer') or '')}**")
    if plan.get("explanation"):
        st.caption(theme.md_escape(plan["explanation"]))
    if stale:
        st.caption(":material/history: Answered before the latest re-scoring; ask again for current numbers.")
    if table.empty:
        st.info("No rows match this question.", icon=":material/search_off:")
    else:
        st.dataframe(table, hide_index=True, width="stretch", height=min(420, 38 + 35 * len(table)),
                     column_config=_column_config(table, plan))
        metric = chart_metric(plan, table)
        if metric:
            st.plotly_chart(bar_figure(table, list(plan.get("group_by") or []), metric), width="stretch",
                            theme=None, key=f"ask_chart_{eid}")
    st.download_button("Download CSV", data=partial(csv_bytes, table), file_name=f"ask_answer_{eid}.csv",
                       mime="text/csv", on_click="ignore", icon=":material/download:", key=f"ask_dl_{eid}",
                       disabled=table.empty)
    _how_answered(plan)


def _render_entry(entry: dict[str, Any], current_key: Any) -> None:
    with st.chat_message("user"):
        st.markdown(theme.md_escape(entry.get("question") or ""))
    with st.chat_message("assistant"):
        data_key = entry.get("data_key")
        _render_answer(entry, stale=current_key is not None and data_key is not None and data_key != current_key)


def _answer(question: str, df: pd.DataFrame, cfg: AppConfig) -> ask.AskResult:
    try:
        client = state.get_llm_client(cfg)
    except Exception as exc:  # noqa: BLE001 - bad LLM settings must not break the tab
        log.warning("Ask the Data: LLM client unavailable (%s)", type(exc).__name__)
        return ask.AskResult(question=question, error=f"The LLM client could not be created: {state.safe_error(exc)}")
    try:
        return ask.answer(question, df, client, cfg)
    except Exception as exc:  # noqa: BLE001 - answer() reports problems itself; this is a last resort
        log.warning("Ask the Data: answering failed (%s)", type(exc).__name__)
        return ask.AskResult(question=question, error="Something went wrong while answering. Please try again.")


def _ask(question: str, df: pd.DataFrame, cfg: AppConfig, data_key: Any) -> dict[str, Any]:
    """Answer a new question, render it in place and return its history entry."""
    with st.chat_message("user"):
        st.markdown(theme.md_escape(question))
    with st.chat_message("assistant"):
        with st.spinner("Planning the query and running it on the current results..."):
            res = _answer(question, df, cfg)
        seq = int(st.session_state.get(K_SEQ, 0)) + 1
        st.session_state[K_SEQ] = seq
        entry = {"id": seq, "question": res.question or question, "plan": res.plan, "result": res.table,
                 "answer": res.summary, "error": res.error, "unsupported": res.plan.get("unsupported") is True,
                 "data_key": data_key}
        _render_answer(entry, stale=False)
    return entry


def render(result: PipelineResult | None) -> None:
    _forget_answers_for_other_data()
    intro, clear = st.columns([5, 1], vertical_alignment="center")
    intro.caption(INTRO)
    clear_slot = clear.empty()          # filled last, so it reflects a question answered on this run
    _render_conversation(result)
    clear_slot.button("Clear conversation", icon=":material/delete_sweep:", key=K_CLEAR, on_click=_clear_history,
                      disabled=not history(), width="stretch")


def _render_conversation(result: PipelineResult | None) -> None:
    entries = history()
    df = None if result is None else result.candidates
    if not isinstance(df, pd.DataFrame) or df.empty:
        st.info("Run a screening first: questions are answered from the current results.", icon=":material/info:")
        return
    cfg = state.get_config()
    ready, reason = state.llm_ready(cfg)
    if not ready:
        st.info(f"{reason} Ask the Data uses the LLM only to turn a question into a query plan; the answer itself "
                "is computed locally.", icon=":material/smart_toy:")
    st.pills("Try an example", EXAMPLE_QUESTIONS, key=K_EXAMPLES, on_change=_pick_example, disabled=not ready)
    conversation = st.container()
    typed = st.chat_input("Ask about this screening run, e.g. average final score by college tier", key=K_INPUT,
                          max_chars=ask.MAX_QUESTION_CHARS, disabled=not ready)
    pending = st.session_state.pop(K_PENDING, None)
    question = typed if isinstance(typed, str) and typed.strip() else pending
    current_key = st.session_state.get(state.K_RESULT_KEY)
    with conversation:
        if not entries and not (question and ready):
            st.caption("No questions yet: pick an example above or type your own below.")
        for entry in entries:
            _render_entry(entry, current_key)
        if question and ready:
            entries.append(_ask(str(question), df, cfg, current_key))
            del entries[:-MAX_HISTORY]
            st.session_state[state.K_ASK_HISTORY] = entries
