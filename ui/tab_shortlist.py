"""Tab 1 - Candidate Shortlist & Leaderboard."""
from __future__ import annotations

import logging
from collections.abc import Iterable
from functools import partial
from typing import Any

import pandas as pd
import streamlit as st

from screening import export, llm, pipeline
from screening import schema as S
from screening.config import AppConfig
from screening.models import PipelineResult
from ui import charts, state, theme
from ui.charts import SHORTLIST_STATUSES, num, split_list, txt

log = logging.getLogger(__name__)

K_TABLE = "sl_table"
K_VIEW_IDS = "_sl_view_ids"
K_CHANGES = "_sl_llm_status_changes"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
TABLE_COLUMNS = [
    S.RANK, S.STATUS, S.CANDIDATE_ID, S.NAME, S.COLLEGE, S.FINAL_SCORE, S.PTS_HR, S.PTS_SKILLS,
    S.PTS_EXPERIENCE, S.PTS_EDUCATION, S.HR_PCT, "risk", S.RISK_SCORE, S.MATCHED_SKILLS, S.CLUSTER_ID,
    S.CGPA, S.GRAD_YEAR, S.EXPERIENCE_SOURCE, S.STATUS_REASON, S.EMAIL,
]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def filter_candidates(df: pd.DataFrame, *, statuses: Iterable[str] = (), risks: Iterable[str] = (),
                      colleges: Iterable[str] = (), min_score: float = 0.0, skills: Iterable[str] = (),
                      query: str = "") -> pd.DataFrame:
    """Apply the leaderboard filters; empty selections mean 'all'."""
    mask = pd.Series(True, index=df.index)
    if statuses := list(statuses):
        mask &= txt(df, S.STATUS).isin(statuses)
    if risks := list(risks):
        mask &= txt(df, S.RISK_LEVEL).replace("", S.RISK_CLEAR).isin(risks)
    if colleges := list(colleges):
        mask &= txt(df, S.COLLEGE).isin(colleges)
    if min_score > 0:
        mask &= num(df, S.FINAL_SCORE).fillna(0.0) >= min_score
    if wanted := {s.strip().lower() for s in skills if s.strip()}:
        have = txt(df, S.MATCHED_SKILLS).map(lambda v: {x.lower() for x in split_list(v)})
        mask &= have.map(lambda h: wanted <= h).astype(bool)
    if q := query.strip().lower():
        hay = txt(df, S.CANDIDATE_ID) + " " + txt(df, S.NAME) + " " + txt(df, S.EMAIL)
        mask &= hay.str.lower().str.contains(q, regex=False)
    return df[mask]


def build_view(df: pd.DataFrame) -> pd.DataFrame:
    """Leaderboard frame: rank order, emoji risk label, skills as lists, known columns only."""
    view = df.assign(risk=txt(df, S.RISK_LEVEL).map(theme.risk_label),
                     _rank=num(df, S.RANK), _score=num(df, S.FINAL_SCORE))
    if S.MATCHED_SKILLS in view.columns:
        view[S.MATCHED_SKILLS] = view[S.MATCHED_SKILLS].map(split_list)
    view = view.sort_values(["_rank", "_score"], ascending=[True, False], na_position="last", kind="stable")
    return view[[c for c in TABLE_COLUMNS if c in view.columns]].reset_index(drop=True)


def csv_bytes(df: pd.DataFrame) -> bytes:
    """export.to_csv_bytes with a plain-pandas fallback."""
    fn = state.engine_fn(export, "to_csv_bytes")
    if fn is not None:
        try:
            return fn(df)
        except Exception as exc:  # noqa: BLE001
            log.warning("to_csv_bytes failed (%s); using pandas fallback", type(exc).__name__)
    return df.to_csv(index=False).encode("utf-8-sig")


def excel_bytes(result: PipelineResult, notes: dict[str, Any] | None) -> bytes:
    """Full Excel report (called lazily when the download button is clicked)."""
    return export.build_excel_report(result, llm_notes=notes or None)


def export_llm_notes() -> dict[str, Any]:
    """LLM outputs in the shape export.build_excel_report expects."""
    executive = st.session_state.get(state.K_LLM_EXEC)
    return {
        "candidates": dict(state.llm_store(state.K_LLM_EXPLAIN)),
        "clusters": dict(state.llm_store(state.K_LLM_CLUSTERS)),
        "executive_summary": executive if isinstance(executive, dict) and executive.get("ok") else None,
    }


def excel_download(result: PipelineResult, key: str) -> None:
    """Shared 'full Excel report' button (also used by the risk tab)."""
    available = state.engine_fn(export, "build_excel_report") is not None
    st.download_button(
        "Full Excel report", data=partial(excel_bytes, result, export_llm_notes()),
        file_name="screening_report.xlsx", mime=XLSX_MIME, on_click="ignore", icon=":material/table_view:",
        disabled=not available, key=key, width="stretch",
        help=None if available else "Excel export is not available in this build.")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _keep_valid(key: str, options: list[Any]) -> None:
    """Drop persisted selections that are no longer valid options (e.g. after a new run)."""
    value = st.session_state.get(key)
    if isinstance(value, list):
        st.session_state[key] = [v for v in value if v in options]


def _on_pick() -> None:
    """Row selected -> point the Deep-Dive at that candidate."""
    sel = st.session_state.get(K_TABLE)
    try:
        rows = list(sel["selection"]["rows"])  # type: ignore[index]
    except Exception:  # noqa: BLE001 - no / malformed selection state
        return
    ids = st.session_state.get(K_VIEW_IDS) or []
    if rows and 0 <= int(rows[0]) < len(ids):
        state.select_candidate(ids[int(rows[0])])


def _filters(df: pd.DataFrame, cfg: AppConfig) -> pd.DataFrame:
    status_counts = txt(df, S.STATUS).value_counts()
    statuses = [s for s in S.STATUS_ORDER if s in status_counts] + sorted(set(status_counts.index) - set(S.STATUS_ORDER) - {""})
    risks = [r for r in S.RISK_ORDER if r in set(txt(df, S.RISK_LEVEL).replace("", S.RISK_CLEAR))]
    colleges = sorted(c for c in txt(df, S.COLLEGE).unique() if c)
    skills = list(dict.fromkeys(cfg.skills.required_skills))
    for key, options in (("sl_status", statuses), ("sl_risk", risks), ("sl_college", colleges), ("sl_skills", skills)):
        _keep_valid(key, options)
    picked_status = st.pills("Status", statuses, selection_mode="multi", key="sl_status", persist_state="session",
                             format_func=lambda s: f"{s} ({int(status_counts.get(s, 0)):,})",
                             help="No selection = all statuses")
    c1, c2, c3 = st.columns([1, 1.4, 1])
    picked_risk = c1.multiselect("Risk level", risks, key="sl_risk", persist_state="session",
                                 format_func=theme.risk_label, placeholder="All levels")
    picked_college = c2.multiselect("College", colleges, key="sl_college", persist_state="session",
                                    placeholder="All colleges")
    min_score = c3.slider("Min final score", 0, 100, key="sl_min_score", persist_state="session")
    c4, c5 = st.columns([1.4, 1])
    picked_skills = c4.multiselect("Must have required skills", skills, key="sl_skills", persist_state="session",
                                   placeholder="Any skills")
    query = c5.text_input("Search", key="sl_query", persist_state="session", placeholder="Name, candidate ID or email",
                          icon=":material/search:")
    return filter_candidates(df, statuses=picked_status or [], risks=picked_risk, colleges=picked_college,
                             min_score=float(min_score or 0), skills=picked_skills, query=query or "")


def _column_config() -> dict[str, Any]:
    cc = st.column_config
    pts = {k: cc.NumberColumn(label, format="%.1f", help="Rubric points") for k, label in (
        (S.PTS_HR, "HackerRank pts"), (S.PTS_SKILLS, "Skills pts"), (S.PTS_EXPERIENCE, "Experience pts"),
        (S.PTS_EDUCATION, "Education pts"))}
    return {
        S.RANK: cc.NumberColumn("Rank", format="%d", width="small", pinned=True),
        S.STATUS: cc.TextColumn("Status", width="medium"),
        S.CANDIDATE_ID: cc.TextColumn("Candidate ID", pinned=True),
        S.NAME: cc.TextColumn("Name"),
        S.COLLEGE: cc.TextColumn("College"),
        S.FINAL_SCORE: cc.ProgressColumn("Final score", min_value=0, max_value=100, format="%.1f", color=theme.RED),
        **pts,
        S.HR_PCT: cc.NumberColumn("HackerRank %", format="%.0f%%"),
        "risk": cc.TextColumn("Integrity risk"),
        S.RISK_SCORE: cc.NumberColumn("Risk score", format="%.0f"),
        S.MATCHED_SKILLS: cc.ListColumn("Matched skills"),
        S.CLUSTER_ID: cc.TextColumn("Cluster", width="small"),
        S.CGPA: cc.NumberColumn("CGPA", format="%.2f"),
        S.GRAD_YEAR: cc.NumberColumn("Grad year", format="%d"),
        S.EXPERIENCE_SOURCE: cc.TextColumn("Experience source"),
        S.STATUS_REASON: cc.TextColumn("Status reason", width="large"),
        S.EMAIL: cc.TextColumn("Email"),
    }


def _status_counts(df: pd.DataFrame, filtered: pd.DataFrame) -> None:
    total, shown = txt(df, S.STATUS).value_counts(), txt(filtered, S.STATUS).value_counts()
    with st.container(horizontal=True, gap="small"):
        st.caption(f"Showing **{len(filtered):,}** of {len(df):,} candidates")
        for status in [s for s in S.STATUS_ORDER if s in total]:
            color, icon = theme.STATUS_BADGE.get(status, ("gray", None))
            st.badge(f"{status}: {int(shown.get(status, 0)):,} / {int(total[status]):,}", color=color, icon=icon)  # type: ignore[arg-type]


def _selected_hint(df: pd.DataFrame) -> None:
    picked = st.session_state.get(state.K_DD_CANDIDATE)
    names = dict(zip(txt(df, S.CANDIDATE_ID), txt(df, S.NAME)))
    if picked and picked in names:
        c1, c2 = st.columns([3, 1], vertical_alignment="center")
        c1.caption(f"Selected for the Individual Deep-Dive: **{theme.md_escape(picked)}** - {theme.md_escape(names[picked])}")
        c2.button("Open profile", icon=":material/person_search:", on_click=state.select_candidate,
                  args=(picked, True), key="sl_open_profile", width="stretch")
    else:
        st.caption("Tip: select a row to load that candidate into the Individual Deep-Dive tab.")


def render(result: PipelineResult) -> None:
    df = result.candidates
    if df is None or df.empty:
        st.info("No candidates in this run.")
        return
    cfg = state.get_config()
    filtered = _filters(df, cfg)
    _status_counts(df, filtered)
    view = build_view(filtered)
    st.session_state[K_VIEW_IDS] = txt(view, S.CANDIDATE_ID).tolist()
    st.dataframe(view, key=K_TABLE, on_select=_on_pick, selection_mode="single-row", hide_index=True,
                 column_config=_column_config(), height=min(560, 42 + 35 * max(len(view), 1)), width="stretch",
                 placeholder="-")
    _selected_hint(df)

    left, right = st.columns([1.6, 1], gap="large")
    with left:
        st.markdown("##### Score distribution")
        cutoff = charts.shortlist_cutoff(df, cfg.shortlist.method, cfg.shortlist.threshold)
        st.plotly_chart(charts.score_histogram(df, cutoff), width="stretch", theme=None, key="sl_hist")
    with right:
        st.markdown("##### Downloads")
        st.download_button(f"Filtered view as CSV ({len(filtered):,} rows)", data=partial(csv_bytes, filtered),
                           file_name="candidates_filtered.csv", mime="text/csv", on_click="ignore",
                           icon=":material/download:", key="sl_dl_csv", width="stretch")
        excel_download(result, key="sl_dl_xlsx")
        st.markdown("##### LLM assist")
        _llm_actions(result, cfg)
    _llm_outputs(df)


# ---------------------------------------------------------------------------
# LLM actions
# ---------------------------------------------------------------------------
def _shortlisted_ids(df: pd.DataFrame) -> list[str]:
    short = df[txt(df, S.STATUS).isin(SHORTLIST_STATUSES)]
    short = short.assign(_r=num(short, S.RANK)).sort_values("_r", na_position="last", kind="stable")
    return txt(short, S.CANDIDATE_ID).tolist()


def _llm_actions(result: PipelineResult, cfg: AppConfig) -> None:
    ready, reason = state.llm_ready(cfg)
    if not ready:
        st.caption(f":material/info: {reason}")
    prepared = state.get_prepared()
    ids = _shortlisted_ids(result.candidates)
    if st.button("LLM: re-score near cutoff", icon=":material/tune:", disabled=not ready or prepared is None,
                 key="sl_llm_rescore", width="stretch",
                 help=f"Blend an LLM experience score ({cfg.llm.blend_weight:.0%}) for candidates within "
                      f"±{cfg.llm.cutoff_band} ranks of the cutoff."):
        _rescore(result, prepared, cfg)
    n = st.number_input("Rationales for the top N shortlisted", min_value=1, max_value=max(1, len(ids)),
                        value=min(25, max(1, len(ids))), key="sl_rationale_n")
    if st.button("LLM: write rationales for shortlist", icon=":material/edit_note:", disabled=not ready or not ids,
                 key="sl_llm_rationales", width="stretch"):
        _rationales(result, cfg, ids[: int(n)])


def _rescore(result: PipelineResult, prepared: Any, cfg: AppConfig) -> None:
    client = state.get_llm_client(cfg)
    before = dict(zip(txt(result.candidates, S.CANDIDATE_ID), txt(result.candidates, S.STATUS)))
    out = state.run_llm_task(
        "LLM re-scoring candidates near the cutoff",
        lambda cb: pipeline.run_llm_rescore(prepared, cfg, client, progress_cb=lambda _s, d, t, det="": cb(d, t, det)))
    if out is None:
        return
    st.session_state[state.K_LLM_RESCORE] = {**(st.session_state.get(state.K_LLM_RESCORE) or {}), **out}
    new = state.current_result()
    if new is None:
        return
    after = dict(zip(txt(new.candidates, S.CANDIDATE_ID), txt(new.candidates, S.STATUS)))
    changes = [{"candidate_id": cid, "before": before.get(cid, ""), "after": status}
               for cid, status in after.items() if before.get(cid, status) != status]
    st.session_state[K_CHANGES] = changes
    ok = sum(1 for r in out.values() if isinstance(r, dict) and r.get("ok"))
    state.queue_message(f"LLM re-scored {ok:,}/{len(out):,} candidates near the cutoff; "
                        f"{len(changes):,} changed status.", icon=":material/smart_toy:")
    st.rerun()


def _rationales(result: PipelineResult, cfg: AppConfig, ids: list[str]) -> None:
    client = state.get_llm_client(cfg)
    out = state.run_llm_task(
        f"LLM rationales for {len(ids):,} shortlisted candidates",
        lambda cb: llm.explain_candidates(ids, result.candidates, result.parsed_by_candidate, result.risk, cfg,
                                          client, progress_cb=cb))
    if out:
        state.llm_store(state.K_LLM_EXPLAIN).update(out)
        ok = sum(1 for r in out.values() if isinstance(r, dict) and r.get("ok"))
        state.queue_message(f"Generated {ok:,} of {len(out):,} rationales.", icon=":material/smart_toy:")
        st.rerun()


def _join(items: Any) -> str:
    return "; ".join(str(i) for i in items) if isinstance(items, (list, tuple)) else str(items or "")


def _llm_outputs(df: pd.DataFrame) -> None:
    changes = st.session_state.get(K_CHANGES) or []
    if changes:
        with st.expander(f"Status changes from LLM re-scoring ({len(changes):,})", icon=":material/swap_vert:"):
            st.dataframe(pd.DataFrame(changes), hide_index=True, width="stretch")
    store = state.llm_store(state.K_LLM_EXPLAIN)
    ids = [cid for cid in txt(df, S.CANDIDATE_ID) if cid in store]
    if not ids:
        return
    lookup = df.assign(_id=txt(df, S.CANDIDATE_ID)).drop_duplicates("_id").set_index("_id")
    rows = []
    for cid in ids:
        r = store[cid] if isinstance(store[cid], dict) else {}
        rows.append({"candidate_id": cid, "name": lookup.at[cid, S.NAME] if S.NAME in lookup.columns else "",
                     "status": lookup.at[cid, S.STATUS] if S.STATUS in lookup.columns else "",
                     "summary": r.get("summary", "") if r.get("ok") else f"(failed: {r.get('error', 'unknown error')})",
                     "strengths": _join(r.get("strengths")), "gaps": _join(r.get("gaps"))})
    with st.expander(f"LLM rationales ({len(rows):,})", icon=":material/smart_toy:", expanded=True):
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                     column_config={"summary": st.column_config.TextColumn("Summary", width="large")})
