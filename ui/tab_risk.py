"""Tab 2 - Integrity & Risk Intelligence (review evidence, never verdicts)."""
from __future__ import annotations

from functools import partial
from typing import Any

import pandas as pd
import streamlit as st

from screening import llm
from screening import schema as S
from screening.models import PipelineResult
from ui import charts, state, theme
from ui.charts import flag, num, split_list, txt
from ui.tab_shortlist import csv_bytes, excel_download

POLICY_TEXT = (
    "Integrity flags are review evidence, not verdicts. Policy: flagged candidates stay in the "
    "pipeline with a red flag and an evidence pack, and the recruiter decides after verification (for "
    "example a live coding re-test or a project walkthrough). Patterns are described as 'consistent with' "
    "coordination - never as proof of misconduct."
)
MEMBER_COLUMNS = [S.CANDIDATE_ID, S.NAME, S.COLLEGE, S.HR_PCT, S.FINAL_SCORE, S.STATUS, S.RISK_LEVEL,
                  S.RISK_SCORE, S.COMPLETION_MIN, S.SUBMISSION_TIME, S.IP_ADDRESS]
PAIR_COLUMNS = ["cand_a", "name_a", "cand_b", "name_b", "pair_score", "signals", "shared_hashes",
                "resume_similarity", "shared_bullets", "shared_github", "same_ip", "same_college",
                "start_gap_sec", "submit_gap_sec", "duration_gap_min", "sequential_gap_min"]
COMMONALITY_COLUMNS = ["attribute", "value", "in_focus", "focus_share", "population_share", "lift", "q_value",
                       "significant"]


def _member_config() -> dict[str, Any]:
    cc = st.column_config
    return {
        S.CANDIDATE_ID: cc.TextColumn("Candidate ID"), S.NAME: cc.TextColumn("Name"),
        S.COLLEGE: cc.TextColumn("College"), S.HR_PCT: cc.NumberColumn("HackerRank %", format="%.0f%%"),
        S.FINAL_SCORE: cc.NumberColumn("Final score", format="%.1f"), S.STATUS: cc.TextColumn("Status"),
        S.RISK_LEVEL: cc.TextColumn("Risk"), S.RISK_SCORE: cc.NumberColumn("Risk score", format="%.0f"),
        S.COMPLETION_MIN: cc.NumberColumn("Completion (min)", format="%.1f"),
        S.SUBMISSION_TIME: cc.DatetimeColumn("Submitted", format="D MMM YYYY, HH:mm:ss"),
        S.IP_ADDRESS: cc.TextColumn("IP address"),
    }


def _present(df: pd.DataFrame, cols: list[str]) -> list[str]:
    return [c for c in cols if c in df.columns]


def _frame(value: Any) -> pd.DataFrame:
    return value if isinstance(value, pd.DataFrame) else pd.DataFrame()


def pairs_with_names(pairs: pd.DataFrame, candidates: pd.DataFrame) -> pd.DataFrame:
    """Pairs table with both candidates' names, strongest first."""
    if pairs.empty:
        return pairs
    names = dict(zip(txt(candidates, S.CANDIDATE_ID), txt(candidates, S.NAME)))
    out = pairs.assign(name_a=txt(pairs, "cand_a").map(names), name_b=txt(pairs, "cand_b").map(names),
                       _s=num(pairs, "pair_score"))
    out = out.sort_values("_s", ascending=False, kind="stable")
    return out[_present(out, PAIR_COLUMNS)].reset_index(drop=True)


def _pair_config() -> dict[str, Any]:
    cc = st.column_config
    return {
        "cand_a": cc.TextColumn("Candidate A"), "name_a": cc.TextColumn("Name A"),
        "cand_b": cc.TextColumn("Candidate B"), "name_b": cc.TextColumn("Name B"),
        "pair_score": cc.ProgressColumn("Pair score", min_value=0, max_value=100, format="%.0f", color=theme.RED_DARK),
        "signals": cc.TextColumn("Signals", width="large"),
        "shared_hashes": cc.TextColumn("Identical answers"),
        "resume_similarity": cc.NumberColumn("Resume similarity", format="%.2f"),
        "shared_bullets": cc.NumberColumn("Shared bullets", format="%d"),
        "start_gap_sec": cc.NumberColumn("Start gap (s)", format="%.0f"),
        "submit_gap_sec": cc.NumberColumn("Submit gap (s)", format="%.0f"),
        "duration_gap_min": cc.NumberColumn("Duration gap (min)", format="%.1f"),
        "sequential_gap_min": cc.NumberColumn("Sequential gap (min)", format="%.1f"),
    }


def render(result: PipelineResult) -> None:
    risk, df = result.risk, result.candidates
    theme.callout(POLICY_TEXT)
    st.space("small")
    _metrics(result)
    _llm_briefing(result)
    _commonality(_frame(risk.commonality))
    _network(result)
    _clusters(result)
    st.subheader("Submission timeline")
    st.caption("Each dot is a candidate; perfect scorers are gold, linked cluster members are coloured diamonds. "
               "Tight vertical/horizontal bands of the same colour suggest synchronised sessions.")
    st.plotly_chart(charts.timeline_scatter(df, risk.focus_ids), width="stretch", theme=None, key="rk_timeline")
    _anomalies(result)
    pairs = pairs_with_names(_frame(risk.pairs), df)
    with st.expander(f"All risk pairs ({len(pairs):,})", icon=":material/link:"):
        if pairs.empty:
            st.caption("No candidate pairs share risk signals.")
        else:
            st.dataframe(pairs, hide_index=True, width="stretch", column_config=_pair_config(), height=420)
    _downloads(result, pairs)
    if risk.warnings:
        with st.expander("Signals skipped (missing data)", icon=":material/warning:"):
            for w in risk.warnings:
                st.markdown(f"- {theme.md_escape(w)}")


def _metrics(result: PipelineResult) -> None:
    risk, df = result.risk, result.candidates
    levels = txt(df, S.RISK_LEVEL)
    focus = len(risk.focus_ids) if risk.focus_ids else int(flag(df, S.IS_FOCUS).sum())
    comm = _frame(risk.commonality)
    sig = int(flag(comm, "significant").sum()) if not comm.empty else 0
    cols = st.columns(5)
    cols[0].metric("Perfect scorers", f"{focus:,}", border=True,
                   help=f"Focus group: HackerRank % >= {result.config.risk.focus_threshold_pct:g}")
    cols[1].metric("High risk", f"{int((levels == S.RISK_HIGH).sum()):,}", border=True)
    cols[2].metric("Medium risk", f"{int((levels == S.RISK_MEDIUM).sum()):,}", border=True)
    cols[3].metric("Linked clusters", f"{len(_frame(risk.clusters)):,}", border=True)
    cols[4].metric("Significant traits", f"{sig:,}", border=True,
                   help="Attribute values significantly over-represented among perfect scorers")


def _commonality(comm: pd.DataFrame) -> None:
    st.subheader("What 100% scorers have in common")
    if comm.empty:
        st.info("Commonality analysis is not available (no perfect scorers, or no shared attributes in the feed).")
        return
    st.caption("Lift = share among perfect scorers ÷ share among all candidates. Significant rows pass the "
               "minimum count, lift and false-discovery-rate (BH q-value) thresholds.")
    left, right = st.columns([1, 1.15], gap="large")
    left.plotly_chart(charts.lift_bar(comm), width="stretch", theme=None, key="rk_lift")
    with right:
        only_sig = st.toggle("Only significant rows", value=True, key="rk_only_sig")
        view = comm.assign(
            attribute=txt(comm, "attribute").map(lambda a: S.COMMONALITY_ATTRIBUTES.get(a, a)),
            in_focus=[f"{c:.0f} / {t:.0f}" if pd.notna(c) and pd.notna(t) else "" for c, t in
                      zip(num(comm, "focus_count"), num(comm, "focus_total"))])
        if only_sig and "significant" in view.columns:
            view = view[flag(view, "significant")]
        cc = st.column_config
        st.dataframe(view, hide_index=True, width="stretch", column_order=COMMONALITY_COLUMNS, column_config={
            "attribute": cc.TextColumn("Attribute"), "value": cc.TextColumn("Value", width="medium"),
            "in_focus": cc.TextColumn("Perfect scorers"),
            "focus_share": cc.ProgressColumn("Share (100%)", min_value=0, max_value=1, format="percent"),
            "population_share": cc.ProgressColumn("Share (all)", min_value=0, max_value=1, format="percent",
                                                  color=theme.GREY),
            "lift": cc.NumberColumn("Lift", format="%.2fx"),
            "q_value": cc.NumberColumn("q (BH)", format="%.4f"),
            "significant": cc.CheckboxColumn("Significant"),
        })


def _network(result: PipelineResult) -> None:
    st.subheader("Risk network")
    c1, c2 = st.columns([1, 1])
    st.session_state.setdefault("rk_color", "Risk level")
    color_by = c1.segmented_control("Colour nodes by", ["Risk level", "Cluster"], key="rk_color",
                                    persist_state="session") or "Risk level"
    min_score = c2.slider("Minimum pair score", 0, 100, step=5, key="rk_min_pair", persist_state="session")
    df = result.candidates
    nodes = df[_present(df, [S.CANDIDATE_ID, S.NAME, S.COLLEGE, S.RISK_LEVEL, S.RISK_SCORE, S.CLUSTER_ID,
                             S.RISK_REASONS])]
    fig = charts.risk_network(_frame(result.risk.pairs), nodes, min_score=float(min_score or 0),
                              color_by="cluster" if color_by == "Cluster" else "risk")
    st.plotly_chart(fig, width="stretch", theme=None, key="rk_network")
    st.caption("Nodes are candidates linked by at least one pair signal; node size grows with the number of links "
               "and edge width with the pair score. Hover nodes or edge midpoints for the evidence.")


def _clusters(result: PipelineResult) -> None:
    clusters, df = _frame(result.risk.clusters), result.candidates
    st.subheader(f"Linked clusters ({len(clusters):,})")
    if clusters.empty:
        st.success("No linked clusters at the current edge threshold.", icon=":material/verified_user:")
        return
    pairs = _frame(result.risk.pairs)
    summaries = state.llm_store(state.K_LLM_CLUSTERS)
    ids = txt(df, S.CANDIDATE_ID)
    for i, row in enumerate(clusters.to_dict("records")):
        cid = str(row.get("cluster_id", f"C{i + 1:02d}"))
        members = split_list(row.get("members"))
        level = str(row.get("risk_level") or S.RISK_CLEAR)
        max_pair = pd.to_numeric(pd.Series([row.get("max_pair_score")]), errors="coerce").iloc[0]
        title = (f"{theme.RISK_EMOJI.get(level, '⚪')} {cid} · {len(members) or row.get('size', 0)} candidates · "
                 f"{level} risk" + (f" · max pair score {max_pair:.0f}" if pd.notna(max_pair) else ""))
        with st.expander(title, expanded=i == 0):
            if row.get("summary"):
                st.markdown(theme.md_escape(row["summary"]))
            theme.chips(split_list(row.get("shared_traits"), S.SEP_REASONS), "gold", empty="No shared traits recorded")
            st.markdown("**Members**")
            st.dataframe(df[ids.isin(members)][_present(df, MEMBER_COLUMNS)], hide_index=True, width="stretch",
                         column_config=_member_config())
            if not pairs.empty:
                inside = pairs[txt(pairs, "cand_a").isin(members) & txt(pairs, "cand_b").isin(members)]
                if not inside.empty:
                    st.markdown("**Pair evidence**")
                    st.dataframe(pairs_with_names(inside, df), hide_index=True, width="stretch",
                                 column_config=_pair_config())
            _cluster_summary(summaries.get(cid))


def _cluster_summary(summary: Any) -> None:
    if not isinstance(summary, dict):
        return
    if not summary.get("ok"):
        st.caption(f"LLM summary failed: {theme.md_escape(summary.get('error', 'unknown error'))}")
        return
    with st.container(border=True):
        st.markdown(f"**:material/smart_toy: {theme.md_escape(summary.get('headline', 'LLM summary'))}**")
        st.markdown(theme.md_escape(summary.get("summary", "")))
        for label, key in (("Evidence", "evidence_highlights"), ("Recommended actions", "recommended_actions")):
            items = summary.get(key) or []
            if items:
                st.markdown(f"*{label}*\n" + "\n".join(f"- {theme.md_escape(x)}" for x in items))


def _anomalies(result: PipelineResult) -> None:
    st.subheader("Individual anomalies")
    ind, df = _frame(result.risk.individual), result.candidates
    if ind.empty:
        st.caption("No individual anomalies flagged (speed, plagiarism flag, tab switches, copy-paste).")
        return
    lookup = df[_present(df, [S.CANDIDATE_ID, S.NAME, S.COLLEGE, S.STATUS, S.HR_PCT, S.COMPLETION_MIN])]
    view = ind.merge(lookup, on=S.CANDIDATE_ID, how="left") if S.CANDIDATE_ID in ind.columns else ind
    view = view.drop(columns=[c for c in ("flag_key",) if c in view.columns])
    st.dataframe(view, hide_index=True, width="stretch", height=min(400, 42 + 35 * len(view)),
                 column_config={**_member_config(), "flag": st.column_config.TextColumn("Flag"),
                                "detail": st.column_config.TextColumn("Detail", width="large"),
                                "weight": st.column_config.NumberColumn("Weight", format="%.0f")})


def _downloads(result: PipelineResult, pairs: pd.DataFrame) -> None:
    st.subheader("Downloads")
    df, risk = result.candidates, result.risk
    flagged = df[txt(df, S.RISK_LEVEL).isin([S.RISK_HIGH, S.RISK_MEDIUM, S.RISK_LOW])]
    files = [("Flagged candidates", flagged, "flagged_candidates.csv"), ("Risk pairs", pairs, "risk_pairs.csv"),
             ("Clusters", _frame(risk.clusters), "risk_clusters.csv"),
             ("Commonality", _frame(risk.commonality), "commonality.csv")]
    cols = st.columns(len(files) + 1)
    for c, (label, frame, name) in zip(cols, files):
        c.download_button(f"{label} ({len(frame):,})", data=partial(csv_bytes, frame), file_name=name,
                          mime="text/csv", on_click="ignore", icon=":material/download:", key=f"rk_dl_{name}",
                          disabled=frame.empty, width="stretch")
    with cols[-1]:
        excel_download(result, key="rk_dl_xlsx")


def _llm_briefing(result: PipelineResult) -> None:
    cfg = state.get_config()
    ready, reason = state.llm_ready(cfg)
    n_clusters = len(_frame(result.risk.clusters))
    with st.container(border=True):
        c1, c2, c3 = st.columns([1.25, 1.1, 1.65], vertical_alignment="center")
        summarize = c1.button("LLM: summarize risk groups", icon=":material/hub:", key="rk_llm_clusters",
                              disabled=not ready or n_clusters == 0, width="stretch")
        brief = c2.button("LLM: executive summary", icon=":material/summarize:", key="rk_llm_exec",
                          disabled=not ready, width="stretch")
        c3.caption(reason if not ready else "LLM output is a drafting aid: verify it against the evidence below.")
        if summarize:
            client = state.get_llm_client(cfg)
            out = state.run_llm_task(f"Summarising {n_clusters:,} risk group(s)", lambda cb: llm.summarize_clusters(
                result.risk, result.candidates, cfg, client, progress_cb=cb))
            if out:
                state.llm_store(state.K_LLM_CLUSTERS).update(out)
                state.queue_message(f"Summarised {len(out):,} risk group(s).", icon=":material/smart_toy:")
                st.rerun()
        if brief:
            client = state.get_llm_client(cfg)
            out = state.run_llm_task("Drafting the executive summary", lambda _cb: llm.executive_summary(result, client))
            if out is not None:
                st.session_state[state.K_LLM_EXEC] = out
        summary = st.session_state.get(state.K_LLM_EXEC)
        if isinstance(summary, dict):
            if summary.get("ok") and summary.get("markdown"):
                st.markdown(theme.safe_markdown(summary["markdown"]))
            else:
                st.warning(f"Executive summary unavailable: {summary.get('error', 'unknown error')}")
