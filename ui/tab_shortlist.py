"""Tab 1 - Candidate Shortlist & Leaderboard."""
from __future__ import annotations

import logging
from collections.abc import Iterable
from functools import partial
from typing import Any

import pandas as pd
import streamlit as st

from screening import export, llm
from screening import schema as S
from screening.config import AppConfig
from screening.models import PipelineResult
from ui import charts, state, theme
from ui.charts import SHORTLIST_STATUSES, num, split_list, txt

log = logging.getLogger(__name__)

K_TABLE = "sl_table"
K_VIEW_IDS = "_sl_view_ids"
K_INELIGIBLE_TABLE = "sl_inelig_table"
K_INELIGIBLE_IDS = "_sl_inelig_ids"
K_INELIGIBLE_PICK = "sl_inelig_pick"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
FOCUS_QUALIFIED = getattr(llm, "FOCUS_QUALIFIED", "qualified")
FOCUS_GAPS = getattr(llm, "FOCUS_GAPS", "gaps")
LLM_POLICY = ("The LLM (GitHub Copilot through the local VS Code proxy) only writes text: it never changes "
              "scores, ranks, cutoffs or the shortlist.")
RATIONALES_HELP = ("Plain-language rationale for the top shortlisted candidates: why each one ranks where "
                   "they do. Scores and ranks do not change.")
BORDERLINE_HELP = ("Plain-language gap analysis for the candidates ranked just below the cutoff (borderline "
                   "band in Config). Scores and ranks do not change.")
TABLE_COLUMNS = [
    S.RANK, S.STATUS, S.CANDIDATE_ID, S.NAME, S.COLLEGE, S.FINAL_SCORE, S.PTS_HR, S.PTS_SKILLS,
    S.PTS_EXPERIENCE, S.PTS_EDUCATION, S.HR_SCORE, S.HR_PCT, "risk", S.RISK_SCORE, S.MATCHED_SKILLS, S.CLUSTER_ID,
    S.CGPA, S.GRAD_YEAR, S.EXPERIENCE_SOURCE, S.STATUS_REASON, S.TIE_BREAK_NOTE, S.EMAIL,
]
INELIGIBLE_COLUMNS = list(getattr(export, "INELIGIBLE_COLUMNS", None) or [
    S.CANDIDATE_ID, S.NAME, "rule", "candidate_value", "threshold_value", "explanation"])
INELIGIBLE_LABELS = {S.CANDIDATE_ID: "Candidate ID", S.NAME: "Name", "rule": "Rule",
                     "candidate_value": "Candidate value", "threshold_value": "Threshold",
                     "explanation": "Explanation"}


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


def _plain_label(column: Any) -> str:
    return S.TA_FIELD_LABELS.get(str(column)) or str(column).replace("_", " ").strip().title()


def header_labels(columns: Iterable[Any]) -> dict[Any, str]:
    """Source-prefixed display labels for the leaderboard ("HackerRank: Score", "Resume: Skills Pts",
    "Academic: CGPA", "Integrity: Risk Level"); the emoji risk column is the integrity risk level."""
    cols = list(columns)
    labels: dict[Any, str] = {}
    fn = state.engine_fn(export, "column_labels")
    if fn is not None:
        try:
            labels = dict(fn(cols, prefixed=True))
        except Exception as exc:  # noqa: BLE001 - labels are cosmetic
            log.warning("column_labels failed (%s)", type(exc).__name__)
    single = state.engine_fn(export, "column_label")
    if "risk" in cols:
        try:
            labels["risk"] = single(S.RISK_LEVEL, prefixed=True) if single is not None else "Risk Level"
        except Exception:  # noqa: BLE001
            labels["risk"] = "Risk Level"
    return {c: labels.get(c) or _plain_label(c) for c in cols}


def csv_bytes(df: pd.DataFrame, labelled: bool = False) -> bytes:
    """export.to_csv_bytes with a plain-pandas fallback; ``labelled`` writes source-prefixed headers."""
    fn = state.engine_fn(export, "to_csv_bytes")
    if fn is not None:
        try:
            return fn(df, labelled=True) if labelled else fn(df)
        except Exception as exc:  # noqa: BLE001
            log.warning("to_csv_bytes failed (%s); using pandas fallback", type(exc).__name__)
    frame = df
    relabel = state.engine_fn(export, "labelled_columns") if labelled else None
    if relabel is not None:
        try:
            frame = relabel(df)
        except Exception:  # noqa: BLE001 - plain headers are fine
            frame = df
    return frame.to_csv(index=False).encode("utf-8-sig")


def excel_bytes(result: PipelineResult, notes: dict[str, Any] | None) -> bytes:
    """Full Excel report (called lazily when the download button is clicked)."""
    return export.build_excel_report(result, llm_notes=notes or None)


def export_llm_notes() -> dict[str, Any]:
    """LLM outputs in the shape export.build_excel_report expects."""
    executive = st.session_state.get(state.K_LLM_EXEC)
    return {
        "candidates": dict(state.llm_store(state.K_LLM_EXPLAIN)),
        "borderline": dict(state.llm_store(state.K_LLM_BORDERLINE)),
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


def value_text(value: Any) -> str:
    """An ineligibility candidate / threshold value as display text ('' when missing)."""
    if isinstance(value, (list, tuple, set, frozenset)):
        return ", ".join(t for t in (value_text(v) for v in value) if t)
    try:
        if value is None or pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    if isinstance(value, float):
        text = f"{value:.2f}".rstrip("0").rstrip(".")
        return text if text not in ("", "-0") else "0"
    return str(value).strip()


def ineligible_table(df: pd.DataFrame | None) -> pd.DataFrame:
    """One row per violated eligibility rule (export.ineligible_rows) with display-ready values."""
    empty = pd.DataFrame(columns=INELIGIBLE_COLUMNS)
    fn = state.engine_fn(export, "ineligible_rows")
    if fn is None or not isinstance(df, pd.DataFrame) or df.empty:
        return empty
    try:
        rows = fn(df)
    except Exception as exc:  # noqa: BLE001 - one odd cell must not hide the leaderboard
        log.warning("ineligible_rows failed (%s)", type(exc).__name__)
        return empty
    if not isinstance(rows, pd.DataFrame) or rows.empty:
        return empty
    rows = rows.reindex(columns=INELIGIBLE_COLUMNS)
    for column in INELIGIBLE_COLUMNS:
        rows[column] = rows[column].map(value_text)
    return rows.reset_index(drop=True)


def ineligible_counts(df: pd.DataFrame) -> list[tuple[str, int]]:
    """[(rule, candidates failing it)] in the export's rule order ([] when unavailable)."""
    fn = state.engine_fn(export, "ineligibility_counts")
    if fn is None:
        return []
    try:
        return [(str(rule), int(n)) for rule, n in fn(df)]
    except Exception as exc:  # noqa: BLE001
        log.warning("ineligibility_counts failed (%s)", type(exc).__name__)
        return []


def duplicate_count(df: pd.DataFrame, rows: pd.DataFrame | None = None) -> int:
    """Duplicate applications made Ineligible (rows pointing at the application that was kept)."""
    if S.DUPLICATE_OF in df.columns:
        return int((txt(df, S.DUPLICATE_OF).str.strip() != "").sum())
    if rows is not None and not rows.empty and {"rule", S.CANDIDATE_ID} <= set(rows.columns):
        return int(rows.loc[rows["rule"] == S.RULE_DUPLICATE, S.CANDIDATE_ID].nunique())
    return 0


def borderline_ids(df: pd.DataFrame, cfg: AppConfig) -> list[str]:
    """llm.borderline_ids: eligible candidates ranked just below the cutoff (read-only selection)."""
    fn = state.engine_fn(llm, "borderline_ids")
    if fn is None:
        return []
    try:
        return [str(cid) for cid in fn(df, cfg)]
    except Exception as exc:  # noqa: BLE001
        log.warning("borderline_ids failed (%s)", type(exc).__name__)
        return []


def _join(items: Any) -> str:
    return "; ".join(str(i) for i in items) if isinstance(items, (list, tuple)) else str(items or "")


def llm_notes_frame(df: pd.DataFrame, store: dict[str, Any], gaps_first: bool = False) -> pd.DataFrame:
    """LLM explanations of candidates in ``df`` as one row each, in rank order."""
    ids = [cid for cid in txt(df, S.CANDIDATE_ID) if cid in store]
    lookup = df.assign(_id=txt(df, S.CANDIDATE_ID)).drop_duplicates("_id").set_index("_id")
    rows = []
    for cid in dict.fromkeys(ids):
        r = store[cid] if isinstance(store[cid], dict) else {}
        rank = pd.to_numeric(pd.Series([lookup.at[cid, S.RANK] if S.RANK in lookup.columns else None]),
                             errors="coerce").iloc[0]
        rows.append({"candidate_id": cid, "name": lookup.at[cid, S.NAME] if S.NAME in lookup.columns else "",
                     "rank": rank, "status": lookup.at[cid, S.STATUS] if S.STATUS in lookup.columns else "",
                     "summary": r.get("summary", "") if r.get("ok") else f"(failed: {r.get('error', 'unknown error')})",
                     "strengths": _join(r.get("strengths")), "gaps": _join(r.get("gaps"))})
    order = ["candidate_id", "name", "rank", "status", "summary"] + (
        ["gaps", "strengths"] if gaps_first else ["strengths", "gaps"])
    frame = pd.DataFrame(rows, columns=order)
    return frame.sort_values("rank", na_position="last", kind="stable").reset_index(drop=True)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _keep_valid(key: str, options: list[Any]) -> None:
    """Drop persisted selections that are no longer valid options (e.g. after a new run)."""
    value = st.session_state.get(key)
    if isinstance(value, list):
        st.session_state[key] = [v for v in value if v in options]


def _selected_row(table_key: str, ids_key: str) -> str | None:
    sel = st.session_state.get(table_key)
    try:
        rows = list(sel["selection"]["rows"])  # type: ignore[index]
    except Exception:  # noqa: BLE001 - no / malformed selection state
        return None
    ids = st.session_state.get(ids_key) or []
    if rows and 0 <= int(rows[0]) < len(ids):
        return str(ids[int(rows[0])])
    return None


def _on_pick() -> None:
    """Row selected -> point the Deep-Dive at that candidate."""
    cid = _selected_row(K_TABLE, K_VIEW_IDS)
    if cid:
        state.select_candidate(cid)


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


def _column_config(columns: Iterable[Any] = ()) -> dict[str, Any]:
    cc = st.column_config
    label = header_labels(dict.fromkeys([*TABLE_COLUMNS, *columns]))

    def text(column: str, **kwargs: Any) -> Any:
        return cc.TextColumn(label.get(column, _plain_label(column)), **kwargs)

    def number(column: str, fmt: str, **kwargs: Any) -> Any:
        return cc.NumberColumn(label.get(column, _plain_label(column)), format=fmt, **kwargs)

    return {
        S.RANK: number(S.RANK, "%d", width="small", pinned=True),
        S.STATUS: text(S.STATUS, width="medium"),
        S.CANDIDATE_ID: text(S.CANDIDATE_ID, pinned=True),
        S.NAME: text(S.NAME),
        S.COLLEGE: text(S.COLLEGE),
        S.FINAL_SCORE: cc.ProgressColumn(label.get(S.FINAL_SCORE, "Final Score"), min_value=0, max_value=100,
                                         format="%.1f", color=theme.RED),
        **{c: number(c, "%.1f", help="Rubric points") for c in (S.PTS_HR, S.PTS_SKILLS, S.PTS_EXPERIENCE,
                                                                 S.PTS_EDUCATION)},
        S.HR_SCORE: number(S.HR_SCORE, "%.1f", help="Assessment score in points"),
        S.HR_PCT: number(S.HR_PCT, "%.0f%%"),
        "risk": text("risk"),
        S.RISK_SCORE: number(S.RISK_SCORE, "%.0f"),
        S.MATCHED_SKILLS: cc.ListColumn(label.get(S.MATCHED_SKILLS, "Matched Skills")),
        S.CLUSTER_ID: text(S.CLUSTER_ID, width="small"),
        S.CGPA: number(S.CGPA, "%.2f"),
        S.GRAD_YEAR: number(S.GRAD_YEAR, "%d"),
        S.EXPERIENCE_SOURCE: text(S.EXPERIENCE_SOURCE),
        S.STATUS_REASON: text(S.STATUS_REASON, width="large"),
        S.TIE_BREAK_NOTE: text(S.TIE_BREAK_NOTE, width="large",
                               help="How a tie on the final score was broken (tie-break order in Config)."),
        S.EMAIL: text(S.EMAIL),
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


def _on_pick_ineligible() -> None:
    """Row selected in the ineligible table -> preselect it for 'Open in Deep-Dive'."""
    cid = _selected_row(K_INELIGIBLE_TABLE, K_INELIGIBLE_IDS)
    if cid:
        st.session_state[K_INELIGIBLE_PICK] = cid


def _open_ineligible() -> None:
    cid = st.session_state.get(K_INELIGIBLE_PICK)
    if cid:
        state.select_candidate(str(cid), open_tab=True)


def _ineligible_section(df: pd.DataFrame) -> None:
    """'Ineligible candidates (N)': counts by rule and one row per violated rule."""
    rows = ineligible_table(df)
    if rows.empty:
        return
    ids = [cid for cid in dict.fromkeys(txt(rows, S.CANDIDATE_ID)) if cid]
    if not ids:
        return
    dups = duplicate_count(df, rows)
    label = f"Ineligible candidates ({len(ids):,})" + (f" · Duplicate applications: {dups:,}" if dups else "")
    with st.expander(label, icon=":material/block:"):
        st.caption("These candidates failed an eligibility rule, so they are not ranked or shortlisted. One row per "
                   "rule; a candidate can fail several. Duplicate applications are merged into the application "
                   "that was kept (its status reason names the merged IDs).")
        counts = ineligible_counts(df)
        if counts:
            with st.container(horizontal=True, gap="small"):
                for rule, n in counts:
                    st.badge(f"{rule}: {n:,}", color="gray", icon=":material/rule:")
        st.session_state[K_INELIGIBLE_IDS] = txt(rows, S.CANDIDATE_ID).tolist()
        cc = st.column_config
        st.dataframe(rows, key=K_INELIGIBLE_TABLE, on_select=_on_pick_ineligible, selection_mode="single-row",
                     hide_index=True, width="stretch", height=min(420, 42 + 35 * len(rows)),
                     column_config={c: cc.TextColumn(INELIGIBLE_LABELS.get(c, _plain_label(c)),
                                                     width="large" if c == "explanation" else None)
                                    for c in INELIGIBLE_COLUMNS})
        names = dict(zip(txt(rows, S.CANDIDATE_ID), txt(rows, S.NAME)))
        if st.session_state.get(K_INELIGIBLE_PICK) not in ids:
            st.session_state[K_INELIGIBLE_PICK] = ids[0]
        c1, c2 = st.columns([3, 1], vertical_alignment="bottom")
        c1.selectbox("Ineligible candidate", ids, key=K_INELIGIBLE_PICK,
                     format_func=lambda c: f"{c} - {names.get(c) or 'Unknown'}",
                     help="Select a row above or pick a candidate here, then open the profile.")
        c2.button("Open in Deep-Dive", icon=":material/person_search:", key="sl_inelig_open",
                  on_click=_open_ineligible, width="stretch")


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
                 column_config=_column_config(view.columns), height=min(560, 42 + 35 * max(len(view), 1)),
                 width="stretch", placeholder="-")
    _selected_hint(df)
    _ineligible_section(df)

    left, right = st.columns([1.6, 1], gap="large")
    with left:
        st.markdown("##### Score distribution")
        cutoff = charts.shortlist_cutoff(df, cfg.shortlist.method, cfg.shortlist.threshold)
        st.plotly_chart(charts.score_histogram(df, cutoff), width="stretch", theme=None, key="sl_hist")
    with right:
        st.markdown("##### Downloads")
        st.download_button(f"Filtered view as CSV ({len(filtered):,} rows)",
                           data=partial(csv_bytes, filtered, labelled=True), file_name="candidates_filtered.csv",
                           mime="text/csv", on_click="ignore", icon=":material/download:", key="sl_dl_csv",
                           width="stretch", help="Headers name the data source, e.g. 'HackerRank: Score', "
                                                 "'Resume: Skills Pts', 'Academic: CGPA'.")
        excel_download(result, key="sl_dl_xlsx")
        st.markdown("##### LLM assist")
        _llm_actions(result, cfg)
    _llm_outputs(df)


# ---------------------------------------------------------------------------
# LLM actions (text only: explanations never change scores, ranks or the shortlist)
# ---------------------------------------------------------------------------
def _shortlisted_ids(df: pd.DataFrame) -> list[str]:
    short = df[txt(df, S.STATUS).isin(SHORTLIST_STATUSES)]
    short = short.assign(_r=num(short, S.RANK)).sort_values("_r", na_position="last", kind="stable")
    return txt(short, S.CANDIDATE_ID).tolist()


def _llm_actions(result: PipelineResult, cfg: AppConfig) -> None:
    ready, reason = state.llm_ready(cfg)
    st.caption(LLM_POLICY)
    if not ready:
        st.caption(f":material/info: {reason}")
    ids = _shortlisted_ids(result.candidates)
    n = st.number_input("Rationales for the top N shortlisted", min_value=1, max_value=max(1, len(ids)),
                        value=min(25, max(1, len(ids))), key="sl_rationale_n")
    if st.button("LLM: write rationales for shortlist", icon=":material/edit_note:", disabled=not ready or not ids,
                 key="sl_llm_rationales", width="stretch", help=RATIONALES_HELP):
        _explain(result, cfg, ids[: int(n)], FOCUS_QUALIFIED)
    border = borderline_ids(result.candidates, cfg)
    if st.button("LLM: explain borderline", icon=":material/troubleshoot:", disabled=not ready or not border,
                 key="sl_llm_borderline", width="stretch", help=BORDERLINE_HELP):
        _explain(result, cfg, border, FOCUS_GAPS)
    st.caption(f"Borderline band: {len(border):,} eligible candidate(s) ranked just below the cutoff."
               if border else "No eligible candidates are ranked just below the cutoff.")


def _explain(result: PipelineResult, cfg: AppConfig, ids: list[str], focus: str) -> None:
    """Shortlist rationales (focus 'qualified') or borderline gap analysis (focus 'gaps')."""
    gaps = focus == FOCUS_GAPS
    store_key = state.K_LLM_BORDERLINE if gaps else state.K_LLM_EXPLAIN
    title = (f"LLM gap analysis for {len(ids):,} borderline candidates" if gaps
             else f"LLM rationales for {len(ids):,} shortlisted candidates")
    client = state.get_llm_client(cfg)
    out = state.run_llm_task(title, lambda cb: llm.explain_candidates(
        ids, result.candidates, result.parsed_by_candidate, result.risk, cfg, client, progress_cb=cb, focus=focus))
    if out:
        state.llm_store(store_key).update(out)
        ok = sum(1 for r in out.values() if isinstance(r, dict) and r.get("ok"))
        what = "borderline gap analyses" if gaps else "rationales"
        state.queue_message(f"Generated {ok:,} of {len(out):,} {what}.", icon=":material/smart_toy:")
        st.rerun()


def _llm_outputs(df: pd.DataFrame) -> None:
    cc = st.column_config
    config = {"rank": cc.NumberColumn("Rank", format="%d", width="small"),
              "summary": cc.TextColumn("Summary", width="large")}
    for key, title, gaps_first in ((state.K_LLM_EXPLAIN, "LLM rationales", False),
                                   (state.K_LLM_BORDERLINE, "LLM borderline gap analysis", True)):
        frame = llm_notes_frame(df, state.llm_store(key), gaps_first=gaps_first)
        if frame.empty:
            continue
        with st.expander(f"{title} ({len(frame):,})", icon=":material/smart_toy:", expanded=True):
            st.caption("Drafting aid written by the LLM. Scores, ranks and the shortlist do not change.")
            st.dataframe(frame, hide_index=True, width="stretch", column_config=config)
