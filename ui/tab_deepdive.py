"""Tab 3 - Individual Deep-Dive: profile, score breakdown, resume preview, integrity evidence."""
from __future__ import annotations

import math
import re
from collections.abc import Collection
from functools import partial
from typing import Any

import numpy as np
import pandas as pd
import streamlit as st

from screening import export, llm, scoring
from screening import schema as S
from screening.config import AppConfig
from screening.models import ParsedResume, PipelineResult
from ui import charts, state, theme
from ui.charts import SHORTLIST_STATUSES, SUB_LABELS, num, split_list, txt

PTS_COMPONENTS = [(S.PTS_HR, "HackerRank", "hackerrank"), (S.PTS_SKILLS, "Skills", "skills"),
                  (S.PTS_EXPERIENCE, "Experience", "experience"), (S.PTS_EDUCATION, "Education", "education")]
LINK_COLUMNS = ["linked_candidate", "linked_name", "linked_status", "pair_score", "signals", "shared_hashes",
                "resume_similarity", "shared_bullets", "shared_bullet_examples", "shared_github", "same_ip",
                "start_gap_sec", "submit_gap_sec", "duration_gap_min"]
MATCH_LABELS = {S.MATCH_TA_FILE: "TA resume file", S.MATCH_ID_FILENAME: "ID in file name", S.MATCH_EMAIL: "email",
                S.MATCH_ID_TEXT: "ID in resume text", S.MATCH_PHONE: "phone", S.MATCH_NAME: "fuzzy name",
                S.MATCH_RESUME_ONLY: "resume only", S.MATCH_NONE: "unmatched", S.MATCH_DUPLICATE: "duplicate"}
TIMING_FIELDS = [S.TEST_START, S.SUBMISSION_TIME, S.COMPLETION_MIN, S.IP_ADDRESS, S.TEST_MODE, S.TAB_SWITCHES,
                 S.COPY_PASTE, S.PLAGIARISM_FLAG]
COMPARE_MAX = 4  # candidates the Compare Candidates tab shows side by side


def clean_compare_ids(value: Any, valid_ids: Collection[str] | None = None, limit: int = COMPARE_MAX) -> list[str]:
    """Compare-list ids as a de-duplicated list of known candidate ids (at most ``limit``)."""
    out: list[str] = []
    if isinstance(value, (list, tuple)):
        for item in value:
            cid = "" if item is None else str(item).strip()
            if cid and cid not in out and (valid_ids is None or cid in valid_ids):
                out.append(cid)
    return out[:limit]


def add_to_compare(value: Any, candidate_id: str, valid_ids: Collection[str] | None = None,
                   limit: int = COMPARE_MAX) -> tuple[list[str], str]:
    """Append a candidate to the compare list -> (new list, "added" | "present" | "full").

    Unknown ids from an earlier dataset and duplicates are dropped first, so they never use up a slot.
    """
    ids = clean_compare_ids(value, valid_ids, limit)
    if candidate_id in ids:
        return ids, "present"
    if len(ids) >= limit:
        return ids, "full"
    return [*ids, candidate_id], "added"


def _v(row: pd.Series, name: str, default: Any = "") -> Any:
    """Row value with missing/NaN mapped to ``default``."""
    value = row.get(name, default)
    try:
        return default if value is None or pd.isna(value) else value
    except (TypeError, ValueError):  # list-like values
        return value


def _f(row: pd.Series, name: str) -> float:
    try:
        value = float(_v(row, name, math.nan))
    except (TypeError, ValueError):
        return math.nan
    return value


def candidate_options(df: pd.DataFrame) -> tuple[list[str], dict[str, str]]:
    """Candidate ids in rank order plus 'ID - Name (status)' labels."""
    order = df.assign(_r=num(df, S.RANK), _s=num(df, S.FINAL_SCORE), _id=txt(df, S.CANDIDATE_ID))
    order = order.sort_values(["_r", "_s", "_id"], ascending=[True, False, True], na_position="last", kind="stable")
    labels = {cid: f"{cid} - {name or 'Unknown'} ({status or 'n/a'})"
              for cid, name, status in zip(order["_id"], txt(order, S.NAME), txt(order, S.STATUS)) if cid}
    return list(labels), labels


def render(result: PipelineResult) -> None:
    df = result.candidates
    if df is None or df.empty or S.CANDIDATE_ID not in df.columns:
        st.info("No candidates to inspect.")
        return
    ids, labels = candidate_options(df)
    if st.session_state.get(state.K_DD_CANDIDATE) not in labels:
        st.session_state[state.K_DD_CANDIDATE] = ids[0]
    cid = st.selectbox("Candidate", ids, key=state.K_DD_CANDIDATE, format_func=lambda c: labels.get(c, c),
                       persist_state="session", placeholder="Search by ID or name",
                       help="Type to search. Selecting a row in the Shortlist tab also lands here.")
    row = df[txt(df, S.CANDIDATE_ID) == cid].iloc[0]
    parsed = result.parsed_by_candidate.get(cid) if result.parsed_by_candidate else None
    _header(result, row)
    _scores(result, row)
    _skills(row, parsed)
    _resume(row, parsed)
    _risk_evidence(result, row)
    _llm_explain(result, row)


def _header(result: PipelineResult, row: pd.Series) -> None:
    cid = str(_v(row, S.CANDIDATE_ID))
    degree = " ".join(str(x) for x in (_v(row, S.DEGREE), _v(row, S.BRANCH)) if x)
    grad = _v(row, S.GRAD_YEAR)
    meta = " · ".join(str(x) for x in (cid, _v(row, S.COLLEGE), degree, f"Class of {grad}" if grad else "",
                                       _v(row, S.CITY)) if x)
    left, right = st.columns([3, 1], vertical_alignment="center")
    with left:
        st.html(f'<div class="wf-profile-name">{theme.esc(_v(row, S.NAME) or cid)}</div>'
                f'<div class="wf-profile-meta">{theme.esc(meta)}</div>')
    with right:
        available = state.engine_fn(export, "build_evidence_pack") is not None
        st.download_button("Evidence pack (.md)", data=partial(export_pack, result, cid), on_click="ignore",
                           file_name=f"evidence_{re.sub(r'[^A-Za-z0-9_-]', '_', cid)}.md", mime="text/markdown",
                           icon=":material/description:", disabled=not available, key="dd_evidence", width="stretch",
                           help=None if available else "Evidence-pack export is not available in this build.")
        _compare_button(result, cid)
    with st.container(horizontal=True, gap="small"):
        theme.status_badge(str(_v(row, S.STATUS)))
        theme.risk_badge(str(_v(row, S.RISK_LEVEL, S.RISK_CLEAR)))
        if _v(row, S.CLUSTER_ID):
            st.badge(f"Cluster {_v(row, S.CLUSTER_ID)}", color="violet", icon=":material/hub:")
        if _f(row, S.HR_PCT) >= 99.995:
            st.badge("Perfect HackerRank score", color="yellow", icon=":material/star:")
        if not bool(_v(row, S.HAS_RESUME, False)):
            st.badge("No resume linked", color="gray", icon=":material/description:")
    if _v(row, S.STATUS_REASON):
        st.caption(theme.md_escape(_v(row, S.STATUS_REASON)))
    rank, score, pct, risk = _f(row, S.RANK), _f(row, S.FINAL_SCORE), _f(row, S.HR_PCT), _f(row, S.RISK_SCORE)
    conf = _f(row, S.MATCH_CONFIDENCE)
    cols = st.columns(5)
    cols[0].metric("Final score", "-" if math.isnan(score) else f"{score:.1f}", border=True)
    cols[1].metric("Rank", "-" if math.isnan(rank) else f"#{rank:.0f}", border=True)
    cols[2].metric("HackerRank", "-" if math.isnan(pct) else f"{pct:.0f}%", border=True)
    cols[3].metric("Risk score", "-" if math.isnan(risk) else f"{risk:.0f}", border=True)
    method = str(_v(row, S.MATCH_METHOD))
    cols[4].metric("Resume match", "-" if math.isnan(conf) else f"{conf:.0%}", border=True,
                   delta=MATCH_LABELS.get(method, method) or "no resume", delta_color="off", delta_arrow="off")


def _on_add_compare(candidate_id: str, valid_ids: frozenset[str]) -> None:
    """Button callback: add the candidate to the compare list (runs before the next render)."""
    ids, outcome = add_to_compare(st.session_state.get(state.K_COMPARE_IDS), candidate_id, valid_ids)
    st.session_state[state.K_COMPARE_IDS] = ids
    who = theme.md_escape(candidate_id)
    if outcome == "added":
        state.queue_message(f"Added {who} to {state.TAB_COMPARE} ({len(ids)}/{COMPARE_MAX}).",
                            icon=":material/compare_arrows:")
    elif outcome == "full":
        state.queue_message(f"The compare list already holds {COMPARE_MAX} candidates. Remove one in "
                            f"{state.TAB_COMPARE} before adding {who}.", icon=":material/info:")


def _compare_button(result: PipelineResult, cid: str) -> None:
    """'Add to compare' (max COMPARE_MAX); shows an 'In compare list' state once the candidate is in."""
    valid = frozenset(txt(result.candidates, S.CANDIDATE_ID))
    ids = clean_compare_ids(st.session_state.get(state.K_COMPARE_IDS), valid)
    if cid in ids:
        st.button("In compare list", icon=":material/check_circle:", disabled=True, key="dd_add_compare",
                  width="stretch", help=f"{len(ids)}/{COMPARE_MAX} candidates selected. Open the {state.TAB_COMPARE} "
                                        "tab to see them side by side.")
        return
    full = len(ids) >= COMPARE_MAX
    st.button("Add to compare", icon=":material/compare_arrows:", key="dd_add_compare", width="stretch",
              on_click=_on_add_compare, args=(cid, valid),
              help=(f"The compare list is full ({COMPARE_MAX}/{COMPARE_MAX}). Remove a candidate in the "
                    f"{state.TAB_COMPARE} tab first." if full else
                    f"Add this candidate to the {state.TAB_COMPARE} tab ({len(ids)}/{COMPARE_MAX} selected)."))


def export_pack(result: PipelineResult, candidate_id: str) -> str:
    """Markdown evidence pack (called lazily when the download button is clicked)."""
    return export.build_evidence_pack(result, candidate_id)


def explain_lines(row: pd.Series, cfg: AppConfig) -> list[str]:
    """scoring.explain_score when available, else the row's score notes."""
    fn = state.engine_fn(scoring, "explain_score")
    if fn is not None:
        try:
            return [str(line) for line in fn(row, cfg)]
        except Exception:  # noqa: BLE001 - explanation is optional
            pass
    return split_list(_v(row, S.SCORE_NOTES), S.SEP_NOTES)


def _scores(result: PipelineResult, row: pd.Series) -> None:
    df, cfg = result.candidates, result.config
    w = cfg.weights.normalized()
    available = {"HackerRank": w.hackerrank, "Skills": w.skills, "Experience": w.experience, "Education": w.education}
    earned = {label: _f(row, col) for col, label, _ in PTS_COMPONENTS}
    short = df[txt(df, S.STATUS).isin(SHORTLIST_STATUSES)]
    baseline = {k: float(num(short, k).mean()) for k in SUB_LABELS} if len(short) else None
    left, right = st.columns(2, gap="large")
    with left:
        st.markdown("##### Sub-scores vs shortlist average")
        st.plotly_chart(charts.radar_subscores({k: _f(row, k) for k in SUB_LABELS}, baseline), width="stretch",
                        theme=None, key="dd_radar")
    with right:
        st.markdown("##### Rubric points")
        st.plotly_chart(charts.points_breakdown(earned, available), width="stretch", theme=None, key="dd_points")
        lines = explain_lines(row, cfg)
        if lines:
            st.markdown("\n".join(f"- {theme.md_escape(line)}" for line in lines))
        if _v(row, S.LLM_RATIONALE):
            st.caption(f"LLM rationale: {theme.md_escape(_v(row, S.LLM_RATIONALE))}")


def _skills(row: pd.Series, parsed: ParsedResume | None) -> None:
    st.markdown("##### Required skills")
    matched, missing = split_list(_v(row, S.MATCHED_SKILLS)), split_list(_v(row, S.MISSING_SKILLS))
    c1, c2 = st.columns(2)
    with c1:
        st.caption(f"Matched ({len(matched)})")
        theme.chips(matched, "match", empty="None matched")
    with c2:
        st.caption(f"Missing ({len(missing)})")
        theme.chips(missing, "miss", empty="Nothing missing")
    found = split_list(_v(row, S.SKILLS_FOUND)) or (list(parsed.skills) if parsed else [])
    if found:
        with st.expander(f"All skills found in the resume ({len(found)})"):
            theme.chips(found, "neutral")


def _resume(row: pd.Series, parsed: ParsedResume | None) -> None:
    st.markdown("##### Resume")
    if parsed is None:
        st.info(f"No resume is linked to this candidate (match method: {_v(row, S.MATCH_METHOD) or 'none'}).")
        return
    st.caption(f"{theme.md_escape(parsed.file_name)} · {parsed.word_count:,} words · parse status {parsed.parse_status}")
    if parsed.parse_status != "ok":
        st.warning(parsed.parse_error or "The resume text could not be extracted (scanned PDF?).")
    left, right = st.columns(2, gap="large")
    with left:
        st.markdown(f"**Projects ({len(parsed.projects)})**")
        for p in parsed.projects[:8]:
            with st.container(border=True):
                st.markdown(f"**{theme.md_escape(p.get('title') or 'Untitled project')}**")
                if p.get("tech"):
                    theme.chips([str(t) for t in p["tech"]], "neutral")
                bullets = [f"- {theme.md_escape(b)}" for b in (p.get("bullets") or [])[:6]]
                if bullets:
                    st.markdown("\n".join(bullets))
                if p.get("github"):
                    st.markdown(f":material/link: `{p['github']}`")
        if not parsed.projects:
            st.caption("No projects detected.")
    with right:
        st.markdown(f"**Experience ({len(parsed.experiences)})**")
        for e in parsed.experiences[:8]:
            months = e.get("months")
            extra = f" · {months:.0f} months" if isinstance(months, (int, float)) and not math.isnan(months) else ""
            extra += " · internship" if e.get("is_internship") else ""
            st.markdown(f"**{theme.md_escape(e.get('title') or 'Role')}** - {theme.md_escape(e.get('org') or '')}{extra}")
        if not parsed.experiences:
            st.caption("No experience entries detected.")
        edu = {k: v for k, v in (parsed.education or {}).items() if v not in (None, "")}
        if edu:
            st.markdown("**Education**")
            labels = {"cgpa": "CGPA", "grad_year": "Graduation year"}
            st.markdown("\n".join(f"- {labels.get(k, k.replace('_', ' ').title())}: {theme.md_escape(v)}"
                                  for k, v in edu.items()))
        if parsed.certifications:
            st.markdown("**Certifications**")
            st.markdown("\n".join(f"- {theme.md_escape(c)}" for c in parsed.certifications[:10]))
        links = list(parsed.github_urls) + list(parsed.linkedin_urls)
        if links:
            st.markdown("**Links**  \n" + "  \n".join(f"`{u}`" for u in links[:6]))
    for key in S.SECTION_KEYS:
        text = (parsed.sections or {}).get(key)
        if text and str(text).strip():
            with st.expander(f"Section: {key.title()}"):
                st.text(str(text))
    with st.expander("Raw extracted text"):
        st.text(parsed.text[:30000] if parsed.text else "(empty)")


def _risk_evidence(result: PipelineResult, row: pd.Series) -> None:
    st.markdown("##### Integrity evidence")
    cid, df = str(_v(row, S.CANDIDATE_ID)), result.candidates
    reasons = split_list(_v(row, S.RISK_REASONS), S.SEP_REASONS)
    if reasons:
        st.markdown("\n".join(f"- {theme.md_escape(r)}" for r in reasons))
    else:
        st.success("No integrity signals recorded for this candidate.", icon=":material/verified_user:")
    pairs = result.risk.pairs if isinstance(result.risk.pairs, pd.DataFrame) else pd.DataFrame()
    if not pairs.empty and {"cand_a", "cand_b"} <= set(pairs.columns):
        a, b = txt(pairs, "cand_a"), txt(pairs, "cand_b")
        linked = pairs[(a == cid) | (b == cid)]
        if not linked.empty:
            other = np.where(txt(linked, "cand_a") == cid, txt(linked, "cand_b"), txt(linked, "cand_a"))
            names = dict(zip(txt(df, S.CANDIDATE_ID), txt(df, S.NAME)))
            statuses = dict(zip(txt(df, S.CANDIDATE_ID), txt(df, S.STATUS)))
            view = linked.assign(linked_candidate=other, linked_name=[names.get(o, "") for o in other],
                                 linked_status=[statuses.get(o, "") for o in other],
                                 _s=num(linked, "pair_score")).sort_values("_s", ascending=False)
            st.markdown(f"**Linked candidates ({len(view)})**")
            st.dataframe(view[[c for c in LINK_COLUMNS if c in view.columns]], hide_index=True, width="stretch",
                         column_config={"pair_score": st.column_config.ProgressColumn(
                             "Pair score", min_value=0, max_value=100, format="%.0f", color=theme.RED_DARK),
                             "signals": st.column_config.TextColumn("Signals", width="large")})
    ind = result.risk.individual if isinstance(result.risk.individual, pd.DataFrame) else pd.DataFrame()
    if not ind.empty and S.CANDIDATE_ID in ind.columns:
        mine = ind[txt(ind, S.CANDIDATE_ID) == cid]
        if not mine.empty:
            st.markdown("**Individual flags**")
            st.dataframe(mine[[c for c in ("flag", "detail", "weight") if c in mine.columns]], hide_index=True,
                         width="stretch")
    facts = [(S.TA_FIELD_LABELS.get(f, f), str(_v(row, f))) for f in TIMING_FIELDS if f in row.index and _v(row, f) != ""]
    if facts:
        with st.expander("Session & proctoring data"):
            st.dataframe(pd.DataFrame(facts, columns=["field", "value"]), hide_index=True, width="stretch")


def _llm_explain(result: PipelineResult, row: pd.Series) -> None:
    st.markdown("##### LLM assessment")
    cfg, cid = state.get_config(), str(_v(row, S.CANDIDATE_ID))
    ready, reason = state.llm_ready(cfg)
    store = state.llm_store(state.K_LLM_EXPLAIN)
    if st.button("LLM: explain this candidate", icon=":material/smart_toy:", disabled=not ready, key="dd_llm_explain"):
        client = state.get_llm_client(cfg)
        out = state.run_llm_task("Explaining this candidate", lambda cb: llm.explain_candidates(
            [cid], result.candidates, result.parsed_by_candidate, result.risk, cfg, client, progress_cb=cb))
        if out:
            store.update(out)
    if not ready:
        st.caption(reason)
    info = store.get(cid)
    if not isinstance(info, dict):
        return
    if not info.get("ok"):
        st.warning(f"LLM explanation failed: {info.get('error', 'unknown error')}")
        return
    with st.container(border=True):
        st.markdown(theme.md_escape(info.get("summary", "")))
        c1, c2 = st.columns(2)
        for col, label, key in ((c1, "Strengths", "strengths"), (c2, "Gaps", "gaps")):
            items = info.get(key) or []
            col.markdown(f"**{label}**\n" + "\n".join(f"- {theme.md_escape(x)}" for x in items) if items else f"**{label}**: -")
        questions = info.get("interview_questions") or []
        if questions:
            st.markdown("**Interview questions**\n" + "\n".join(f"1. {theme.md_escape(q)}" for q in questions))
        integrity = info.get("integrity_questions") or []
        if integrity and str(_v(row, S.RISK_LEVEL)) in (S.RISK_HIGH, S.RISK_MEDIUM):
            st.markdown("**Integrity verification questions**\n" + "\n".join(f"1. {theme.md_escape(q)}" for q in integrity))
