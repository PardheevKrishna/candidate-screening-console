"""Candidate Screening & Integrity Console - Streamlit entry point.

Run with:  streamlit run app.py
"""
from __future__ import annotations

import importlib

import streamlit as st

from screening.env import load_env

load_env()  # optional .env -> environment, before ui modules read settings at import time

from ui import sidebar, state, tab_config, tab_deepdive, tab_risk, tab_shortlist, theme  # noqa: E402

# Feature tabs are switched on here once they are verified; a tab whose module is missing or
# fails to import is hidden instead of breaking the app.
ENABLED_FEATURE_TABS: dict[str, str] = {
    state.TAB_COMPARE: "ui.tab_compare",
    state.TAB_CAMPUS: "ui.tab_campus",
    state.TAB_HISTORY: "ui.tab_history",
}


def _feature_render(label: str):
    module = ENABLED_FEATURE_TABS.get(label)
    if not module:
        return None
    try:
        return importlib.import_module(module).render
    except Exception:  # noqa: BLE001 - a broken optional tab must not take the app down
        return None


def _main(request: state.RunRequest | None, result) -> None:
    """Main area: getting started before the first run, a screening run when one was requested,
    otherwise the KPI row and the tabs (only the open tab does any work)."""
    if request is None and result is None:
        # Before the first run: let TA set the job description, skills, weights and shortlist size
        # up front (they can still be changed after a run - re-scoring takes under a second).
        history_render = _feature_render(state.TAB_HISTORY)
        labels = ["Getting started", state.TAB_CONFIG] + ([state.TAB_HISTORY] if history_render else [])
        pre_tabs = st.tabs(labels, key="pre_run_tabs")
        with pre_tabs[0]:
            if theme.render_empty_state(state.sample_available()):
                request = state.RunRequest(label="Synthetic sample", use_sample=True)
        with pre_tabs[1]:
            tab_config.render(None)
        if history_render:
            with pre_tabs[2]:
                history_render(None)
    if request is not None:
        if state.run_screening(request):
            st.rerun()
        return
    if result is None:
        return

    theme.render_kpis(state.summary_kpis(result))
    core = {state.TAB_SHORTLIST: tab_shortlist.render, state.TAB_RISK: tab_risk.render,
            state.TAB_DEEPDIVE: tab_deepdive.render, state.TAB_CONFIG: tab_config.render}
    tabs = [(label, core.get(label) or _feature_render(label)) for label in state.TAB_LABELS]
    tabs = [(label, render) for label, render in tabs if render is not None]
    for tab, (_, render) in zip(st.tabs([label for label, _ in tabs], key=state.K_TABS, on_change="rerun"), tabs):
        if tab.open is not False:  # only the active tab does work
            with tab:
                render(result)


st.set_page_config(page_title=theme.APP_TITLE, page_icon=theme.PAGE_ICON, layout="wide",
                   initial_sidebar_state="expanded")
theme.inject_css()
state.init_state()

run_request = sidebar.render()
current = state.current_result()
llm_slot = sidebar.render_status(current)
theme.render_header(state.dataset_summary(current))
state.flush_messages()
_main(run_request, current)
# Last, into its sidebar slot: the Copilot proxy check (cached 30 s) never holds up the page.
sidebar.render_llm_status(llm_slot)
