"""Tab 3 - Individual Deep-Dive: eligibility, score breakdown and the evidence grouped by data source
(A. Assessment, B. Resume, C. Academic, D. Integrity), plus the LLM assessment."""
from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable, Collection
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
from ui.tab_shortlist import FOCUS_GAPS, LLM_POLICY, ineligible_table

log = logging.getLogger(__name__)

PTS_COMPONENTS = [(S.PTS_HR, "HackerRank", "hackerrank"), (S.PTS_SKILLS, "Skills", "skills"),
                  (S.PTS_EXPERIENCE, "Experience", "experience"), (S.PTS_EDUCATION, "Education", "education")]
LINK_COLUMNS = ["linked_candidate", "linked_name", "linked_status", "pair_type", "pair_score", "signals",
                "shared_hashes", "resume_similarity", "shared_bullets", "shared_bullet_examples", "shared_github",
                "same_ip", "start_gap_sec", "submit_gap_sec", "duration_gap_min"]
MATCH_LABELS = {S.MATCH_TA_FILE: "TA resume file", S.MATCH_ID_FILENAME: "ID in file name", S.MATCH_EMAIL: "email",
                S.MATCH_ID_TEXT: "ID in resume text", S.MATCH_PHONE: "phone", S.MATCH_NAME: "fuzzy name",
                S.MATCH_RESUME_ONLY: "resume only", S.MATCH_NONE: "unmatched", S.MATCH_DUPLICATE: "duplicate"}
TIMING_FIELDS = [S.TEST_START, S.SUBMISSION_TIME, S.COMPLETION_MIN, S.IP_ADDRESS, S.TEST_MODE, S.TAB_SWITCHES,
                 S.COPY_PASTE, S.PLAGIARISM_FLAG]
# Assessment telemetry shown in section A (the timing fields plus the HackerRank proctoring counters).
TELEMETRY_FIELDS = [S.TEST_NAME, *TIMING_FIELDS[:6], S.OUT_OF_WINDOW_SEC, S.SECURE_MODE_EXITS, S.MAX_CODE_SIMILARITY,
                    S.PLAGIARISM_FLAG]
COMPARE_MAX = 4  # candidates the Compare Candidates tab shows side by side
SECTION_ASSESSMENT = "A. Assessment (HackerRank)"
SECTION_RESUME = "B. Resume (self-reported)"
SECTION_ACADEMIC = "C. Academic"
SECTION_INTEGRITY = "D. Integrity"
SECTIONS = (SECTION_ASSESSMENT, SECTION_RESUME, SECTION_ACADEMIC, SECTION_INTEGRITY)
BORDERLINE_TITLE = "Borderline gap analysis"
TIER_NOTES = {"feed": "from the TA feed, used for scoring",
              "tier list": "matched from the approved tier list, used for scoring"}


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


def _s(row: pd.Series, name: str) -> str:
    """Row value as stripped text ('' when missing)."""
    value = _v(row, name, "")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def _num_text(value: float, digits: int = 2) -> str:
    """150.0 -> '150', 87.5 -> '87.5', 8.425 -> '8.43' ('-' when missing)."""
    if value is None or math.isnan(value):
        return "-"
    text = f"{value:.{digits}f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def candidate_options(df: pd.DataFrame) -> tuple[list[str], dict[str, str]]:
    """Candidate ids in rank order plus 'ID - Name (status)' labels."""
    order = df.assign(_r=num(df, S.RANK), _s=num(df, S.FINAL_SCORE), _id=txt(df, S.CANDIDATE_ID))
    order = order.sort_values(["_r", "_s", "_id"], ascending=[True, False, True], na_position="last", kind="stable")
    labels = {cid: f"{cid} - {name or 'Unknown'} ({status or 'n/a'})"
              for cid, name, status in zip(order["_id"], txt(order, S.NAME), txt(order, S.STATUS)) if cid}
    return list(labels), labels


def ineligibility_lines(row: pd.Series) -> list[str]:
    """'<Rule>: <explanation>' for every eligibility rule the candidate failed ([] when eligible)."""
    rows = ineligible_table(pd.DataFrame([row.to_dict()]))
    lines = []
    for rule, cand, thr, expl in zip(txt(rows, "rule"), txt(rows, "candidate_value"), txt(rows, "threshold_value"),
                                     txt(rows, "explanation")):
        detail = expl or (f"{cand} (threshold {thr})" if cand and thr else cand or (f"threshold {thr}" if thr else ""))
        line = f"{rule}: {detail}" if rule and detail else rule or detail
        if line:
            lines.append(line)
    if not lines and _s(row, S.STATUS) == S.STATUS_INELIGIBLE:
        lines = split_list(_v(row, S.STATUS_REASON), S.SEP_NOTES)
    return lines


def explain_lines(row: pd.Series, cfg: AppConfig, mode: str | None = None) -> list[str]:
    """scoring.explain_score when available (with the run mode when it accepts one), else the score notes."""
    fn = state.engine_fn(scoring, "explain_score")
    if fn is not None:
        for kwargs in ([{"mode": mode}] if mode else []) + [{}]:
            try:
                return [str(line) for line in fn(row, cfg, **kwargs)]
            except Exception:  # noqa: BLE001 - explanation is optional
                continue
    return split_list(_v(row, S.SCORE_NOTES), S.SEP_NOTES)


def _guarded(title: str, fn: Callable[..., Any], *args: Any) -> Any:
    """Render one section; a problem with this candidate's data is reported, never raised."""
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001 - one odd row must not take the tab down
        log.warning("Deep-Dive section '%s' failed (%s)", title, type(exc).__name__)
        st.warning(f"{title}: this part could not be shown for this candidate ({state.safe_error(exc)}).",
                   icon=":material/warning:")
        return None


def render(result: PipelineResult) -> None:
    df = result.candidates
    if df is None or df.empty or S.CANDIDATE_ID not in df.columns:
        st.info("No candidates to inspect.")
        return
    ids, labels = candidate_options(df)
    if not ids:
        st.info("No candidates to inspect.")
        return
    if st.session_state.get(state.K_DD_CANDIDATE) not in labels:
        st.session_state[state.K_DD_CANDIDATE] = ids[0]
    cid = st.selectbox("Candidate", ids, key=state.K_DD_CANDIDATE, format_func=lambda c: labels.get(c, c),
                       persist_state="session", placeholder="Search by ID or name",
                       help="Type to search. Selecting a row in the Shortlist tab also lands here.")
    row = df[txt(df, S.CANDIDATE_ID) == cid].iloc[0]
    parsed = result.parsed_by_candidate.get(cid) if result.parsed_by_candidate else None
    banner = _guarded("Eligibility", _ineligible_banner, row)
    _header(result, row, show_reason=not banner)
    _guarded("Duplicate applications", _duplicates, result, row)
    _guarded("Score breakdown", _scores, result, row)
    _guarded(SECTION_ASSESSMENT, _assessment, row)
    _guarded(SECTION_RESUME, _resume_section, row, parsed)
    _guarded(SECTION_ACADEMIC, _academic, row)
    _guarded(SECTION_INTEGRITY, _integrity, result, row, parsed)
    _llm_explain(result, row)


# ---------------------------------------------------------------------------
# Top: eligibility banner, header, duplicate applications
# ---------------------------------------------------------------------------
def _ineligible_banner(row: pd.Series) -> bool:
    """Red banner listing every eligibility rule the candidate failed; True when shown."""
    lines = ineligibility_lines(row)
    if not lines and _s(row, S.STATUS) != S.STATUS_INELIGIBLE:
        return False
    body = "\n".join(f"- {theme.md_escape(line)}" for line in lines) or "- No rule was recorded."
    st.error(f"**Ineligible - not ranked or shortlisted.** Eligibility rules not met:\n{body}", icon=":material/block:")
    return True


def _header(result: PipelineResult, row: pd.Series, show_reason: bool = True) -> None:
    cid = str(_v(row, S.CANDIDATE_ID))
    degree = " ".join(str(x) for x in (_v(row, S.DEGREE), _v(row, S.BRANCH)) if x)
    grad = _s(row, S.GRAD_YEAR)
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
        if _s(row, S.INJECTION_FLAGS):
            st.badge("Hidden text in resume", color="red", icon=":material/gpp_bad:")
        if _s(row, S.DUPLICATE_OF):
            st.badge("Duplicate application", color="gray", icon=":material/content_copy:")
        elif _s(row, S.DUPLICATE_IDS):
            st.badge("Merged duplicate applications", color="blue", icon=":material/merge:")
    if show_reason and _v(row, S.STATUS_REASON):
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


def _duplicates(result: PipelineResult, row: pd.Series) -> None:
    """Duplicate-application context: which application was kept, which were merged, and why."""
    dup_of = _s(row, S.DUPLICATE_OF)
    merged = [m for m in split_list(_v(row, S.DUPLICATE_IDS)) if m]
    evidence = _s(row, S.DUPLICATE_EVIDENCE)
    if not dup_of and not merged:
        return
    known = set(txt(result.candidates, S.CANDIDATE_ID))
    if dup_of:
        st.warning(f"Duplicate application of {theme.md_escape(dup_of)} (the kept application). This application "
                   "is not ranked; the kept one carries the candidate's score.", icon=":material/content_copy:")
    if merged:
        st.info(f"Merged duplicate applications: {theme.md_escape(', '.join(merged))}", icon=":material/merge:")
    if evidence:
        st.caption(f"Duplicate evidence: {theme.md_escape(evidence)}")
    targets = [c for c in dict.fromkeys(([dup_of] if dup_of else []) + merged[:3]) if c in known]
    if targets:
        with st.container(horizontal=True, gap="small"):
            for target in targets:
                st.button(f"Open {target}", key=f"dd_open_dup_{target}", icon=":material/person_search:",
                          on_click=state.select_candidate, args=(target,),
                          help="Show this application in the Deep-Dive.")


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


# ---------------------------------------------------------------------------
# Score breakdown
# ---------------------------------------------------------------------------
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
        tie = _s(row, S.TIE_BREAK_NOTE)
        lines = [line for line in explain_lines(row, cfg, getattr(result, "mode", None))
                 if not (tie and line.startswith("Tie-break:"))]
        if lines:
            st.markdown("\n".join(f"- {theme.md_escape(line)}" for line in lines))
        if tie:
            st.info(f"**Tie-break:** {theme.md_escape(tie)}", icon=":material/balance:")
        if _v(row, S.LLM_RATIONALE):
            st.caption(f"LLM rationale: {theme.md_escape(_v(row, S.LLM_RATIONALE))}")


def _section(title: str, caption: str) -> None:
    st.subheader(title, anchor=False, divider="gray")
    st.caption(caption)


def _facts(items: list[tuple[str, str]]) -> None:
    lines = [f"- **{label}:** {theme.md_escape(value)}" for label, value in items if value]
    if lines:
        st.markdown("\n".join(lines))


# ---------------------------------------------------------------------------
# A. Assessment (HackerRank)
# ---------------------------------------------------------------------------
def _assessment(row: pd.Series) -> None:
    _section(SECTION_ASSESSMENT, "Verified assessment results and test telemetry from the assessment export.")
    score, top, pct = _f(row, S.HR_SCORE), _f(row, S.MAX_SCORE), _f(row, S.HR_PCT)
    cut, cut_pct = _f(row, S.CUTOFF_SCORE), _f(row, S.CUTOFF_PCT)
    status = _s(row, S.INTEGRITY_STATUS)
    shown = "-" if math.isnan(score) else _num_text(score) + ("" if math.isnan(top) else f" / {_num_text(top)}")
    gap = score - cut if not math.isnan(score) and not math.isnan(cut) else math.nan
    parts = [] if math.isnan(cut) else [_num_text(cut)]
    if not math.isnan(cut_pct):
        parts.append(f"({_num_text(cut_pct)}%)" if parts else f"{_num_text(cut_pct)}%")
    cutoff = " ".join(parts) or "-"
    cols = st.columns(4)
    cols[0].metric("Score", shown, border=True, delta=None if math.isnan(gap) else f"{gap:+g} vs cutoff")
    cols[1].metric("Percentage", "-" if math.isnan(pct) else f"{_num_text(pct, 1)}%", border=True)
    cols[2].metric("Cutoff score", cutoff, border=True, help="Pass mark from the assessment export (points / %).")
    cols[3].metric("Integrity status", status or "-", border=True)
    if charts.integrity_concern(status):
        st.warning(f"Assessment integrity issues reported by the assessment platform: {theme.md_escape(status)}. "
                   "Review the assessment comment and proctoring notes below.", icon=":material/report:")
    _facts([("Candidate option", _s(row, S.CANDIDATE_OPTION)), ("Test status", _s(row, S.TEST_STATUS)),
            ("Proctoring flags", _s(row, S.PROCTORING_FLAGS))])
    comment = _s(row, S.ASSESSMENT_COMMENT)
    if comment:
        st.markdown("**Assessment comment**")
        st.text(comment)
    facts = [(S.TA_FIELD_LABELS.get(f, f), _s(row, f)) for f in TELEMETRY_FIELDS if f in row.index and _s(row, f)]
    if facts:
        with st.expander("Session & proctoring data"):
            st.dataframe(pd.DataFrame(facts, columns=["field", "value"]), hide_index=True, width="stretch")


# ---------------------------------------------------------------------------
# B. Resume (self-reported)
# ---------------------------------------------------------------------------
def _resume_section(row: pd.Series, parsed: ParsedResume | None) -> None:
    _section(SECTION_RESUME, "Self-reported by the candidate and parsed from the resume: verify in the interview.")
    if parsed is not None:
        bundle = charts.bundle_text(getattr(parsed, "bundle_source", None))
        st.caption(" · ".join(p for p in (
            theme.md_escape(parsed.file_name), f"{parsed.word_count:,} words", f"parse status {parsed.parse_status}",
            theme.md_escape(bundle)) if p))
        if parsed.parse_status != "ok":
            st.warning(theme.md_text(parsed.parse_error) if parsed.parse_error
                       else "The resume text could not be extracted (scanned PDF?).")
    elif _s(row, S.RESUME_FILE):
        st.caption(theme.md_escape(_s(row, S.RESUME_FILE)))
    _skills(row, parsed)
    has_resume = parsed is not None or bool(_v(row, S.HAS_RESUME, False))
    counts = [(label, _f(row, col)) for label, col in (
        ("Internships", S.INTERNSHIP_COUNT), ("Experience (months)", S.EXPERIENCE_MONTHS),
        ("Projects", S.PROJECT_COUNT), ("Certifications", S.CERT_COUNT)) if col in row.index]
    if has_resume and counts:
        for col, (label, value) in zip(st.columns(len(counts)), counts):
            col.metric(label, _num_text(value, 1), border=True)
    _resume(row, parsed)


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
            st.markdown("**Education (as written on the resume)**")
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


# ---------------------------------------------------------------------------
# C. Academic
# ---------------------------------------------------------------------------
def _valid_cgpa(value: float) -> bool:
    return not math.isnan(value) and 0 < value <= 10


def cgpa_value(row: pd.Series) -> tuple[float, bool]:
    """(CGPA used for scoring, taken from the resume?): the TA feed's CGPA when valid (0-10), else
    the resume's, following CGPA_SOURCE when the run recorded it."""
    feed, resume = _f(row, S.CGPA), _f(row, S.RESUME_CGPA)
    source = _s(row, S.CGPA_SOURCE).lower()
    if source == "resume":
        return (resume if _valid_cgpa(resume) else feed), True
    if _valid_cgpa(feed) or source == "feed":
        return feed, False
    if _valid_cgpa(resume):
        return resume, True
    return feed, False


def cgpa_text(row: pd.Series) -> str:
    """CGPA used for scoring with its source: '8.42' (TA feed) or '8.42 (from resume)'."""
    value, from_resume = cgpa_value(row)
    if math.isnan(value):
        return ""
    return f"{value:.2f}" + (" (from resume)" if from_resume else "")


def tier_text(row: pd.Series) -> str:
    """College tier with where it came from and whether it counted towards the score."""
    tier = _s(row, S.COLLEGE_TIER)
    if not tier:
        return ""
    label = f"Tier {tier}" if tier.isdigit() else tier
    if S.TIER_SOURCE not in row.index:
        return label
    return f"{label} ({TIER_NOTES.get(_s(row, S.TIER_SOURCE).lower(), 'not used for scoring')})"


def _academic(row: pd.Series) -> None:
    _section(SECTION_ACADEMIC, "Academic profile from the TA feed, reconciled with the resume where the feed is blank.")
    degree, resume_degree = _s(row, S.DEGREE), _s(row, S.RESUME_DEGREE)
    items = [("College", _s(row, S.COLLEGE)), ("College tier", tier_text(row)),
             ("Degree", degree or (f"{resume_degree} (from resume)" if resume_degree else "")),
             ("Branch", _s(row, S.BRANCH)), ("Graduation year", _s(row, S.GRAD_YEAR)), ("CGPA", cgpa_text(row))]
    if any(value for _, value in items):
        _facts(items)
    else:
        st.caption("No academic data in this run.")


# ---------------------------------------------------------------------------
# D. Integrity
# ---------------------------------------------------------------------------
def injection_table(row: pd.Series, parsed: ParsedResume | None) -> pd.DataFrame:
    """Hidden / adversarial resume text: kind, snippet, detail (the INJECTION_FLAGS text when the
    parsed resume is not available)."""
    findings = charts.injection_findings(parsed)
    if findings:
        rows = [{"kind": charts.INJECTION_KIND_LABELS.get(f["kind"], f["kind"] or "Hidden text"),
                 "snippet": f["snippet"], "detail": f["detail"]} for f in findings]
    else:
        rows = [{"kind": "", "snippet": text, "detail": ""} for text in split_list(_v(row, S.INJECTION_FLAGS),
                                                                                     S.SEP_REASONS)]
    return pd.DataFrame(rows, columns=["kind", "snippet", "detail"])


def _integrity(result: PipelineResult, row: pd.Series, parsed: ParsedResume | None) -> None:
    _section(SECTION_INTEGRITY, "Integrity flags are review evidence, not verdicts: verify before any decision.")
    cid, df = str(_v(row, S.CANDIDATE_ID)), result.candidates
    level, risk = _s(row, S.RISK_LEVEL) or S.RISK_CLEAR, _f(row, S.RISK_SCORE)
    _facts([("Risk level", theme.risk_label(level)), ("Risk score", "" if math.isnan(risk) else f"{risk:.0f} / 100"),
            ("Cluster", _s(row, S.CLUSTER_ID))])
    reasons = split_list(_v(row, S.RISK_REASONS), S.SEP_REASONS)
    if reasons:
        st.markdown("\n".join(f"- {theme.md_escape(r)}" for r in reasons))
    else:
        st.success("No integrity signals recorded for this candidate.", icon=":material/verified_user:")
    status = _s(row, S.INTEGRITY_STATUS)
    if charts.integrity_concern(status):
        st.caption(f":material/report: Assessment integrity status: **{theme.md_escape(status)}** (see section A).")
    injected = injection_table(row, parsed)
    if not injected.empty:
        st.warning(charts.INJECTION_LABEL, icon=":material/gpp_bad:")
        cc = st.column_config
        st.dataframe(injected, hide_index=True, width="stretch",
                     column_config={"kind": cc.TextColumn("Kind"), "snippet": cc.TextColumn("Snippet", width="large"),
                                    "detail": cc.TextColumn("Detail", width="medium")})
        st.caption("Hidden or adversarial text is excluded from scoring and from LLM prompts; it is shown here for "
                   "review only.")
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
                                 pair_type=charts.pair_types(linked).to_numpy(),
                                 _s=num(linked, "pair_score")).sort_values("_s", ascending=False)
            st.markdown(f"**Linked candidates ({len(view)})**")
            if charts.team_project_mask(linked).any():
                st.caption(f"'{charts.TEAM_PAIR_LABEL}': the same GitHub repo on otherwise different resumes - "
                           "consistent with a group project; verify the candidate's own role.")
            st.dataframe(view[[c for c in LINK_COLUMNS if c in view.columns]], hide_index=True, width="stretch",
                         column_config={"pair_type": st.column_config.TextColumn("Pair type"),
                                        "pair_score": st.column_config.ProgressColumn(
                                            "Pair score", min_value=0, max_value=100, format="%.0f",
                                            color=theme.RED_DARK),
                                        "signals": st.column_config.TextColumn("Signals", width="large")})
    ind = result.risk.individual if isinstance(result.risk.individual, pd.DataFrame) else pd.DataFrame()
    if not ind.empty and S.CANDIDATE_ID in ind.columns:
        mine = ind[txt(ind, S.CANDIDATE_ID) == cid]
        if not mine.empty:
            st.markdown("**Individual flags**")
            st.dataframe(mine[[c for c in ("flag", "detail", "weight") if c in mine.columns]], hide_index=True,
                         width="stretch")


# ---------------------------------------------------------------------------
# LLM assessment (text only)
# ---------------------------------------------------------------------------
def _llm_explain(result: PipelineResult, row: pd.Series) -> None:
    st.markdown("##### LLM assessment")
    st.caption(LLM_POLICY)
    cfg, cid = state.get_config(), str(_v(row, S.CANDIDATE_ID))
    ready, reason = state.llm_ready(cfg)
    store = state.llm_store(state.K_LLM_EXPLAIN)
    if st.button("LLM: explain this candidate", icon=":material/smart_toy:", disabled=not ready, key="dd_llm_explain",
                 help="Plain-language rationale and interview questions for this candidate. Scores and ranks do "
                      "not change."):
        client = state.get_llm_client(cfg)
        out = state.run_llm_task("Explaining this candidate", lambda cb: llm.explain_candidates(
            [cid], result.candidates, result.parsed_by_candidate, result.risk, cfg, client, progress_cb=cb))
        if out:
            store.update(out)
    if not ready:
        st.caption(reason)
    explained = store.get(cid)
    if isinstance(explained, dict):
        _explanation(explained, row, BORDERLINE_TITLE if explained.get("focus") == FOCUS_GAPS else "")
    borderline = state.llm_store(state.K_LLM_BORDERLINE).get(cid)
    if isinstance(borderline, dict):
        _explanation(borderline, row, BORDERLINE_TITLE)


def _items(value: Any) -> list[str]:
    """LLM list output as clean strings (a lone string counts as one item)."""
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if v is not None and str(v).strip()]
    return [str(value).strip()] if isinstance(value, str) and value.strip() else []


def _explanation(info: dict[str, Any], row: pd.Series, title: str) -> None:
    if not info.get("ok"):
        what = title or "LLM explanation"
        st.warning(f"{what} failed: {theme.md_text(info.get('error') or 'unknown error')}")
        return
    gaps_first = title == BORDERLINE_TITLE
    with st.container(border=True):
        if title:
            st.markdown(f"**:material/troubleshoot: {title}**")
        st.markdown(theme.md_escape(info.get("summary") or ""))
        order = (("Gaps", "gaps"), ("Strengths", "strengths")) if gaps_first else (("Strengths", "strengths"),
                                                                                   ("Gaps", "gaps"))
        for col, (label, key) in zip(st.columns(2), order):
            items = _items(info.get(key))
            col.markdown(f"**{label}**\n" + "\n".join(f"- {theme.md_escape(x)}" for x in items) if items else f"**{label}**: -")
        questions = _items(info.get("interview_questions"))
        if questions:
            st.markdown("**Interview questions**\n" + "\n".join(f"1. {theme.md_escape(q)}" for q in questions))
        integrity = _items(info.get("integrity_questions"))
        if integrity and str(_v(row, S.RISK_LEVEL)) in (S.RISK_HIGH, S.RISK_MEDIUM):
            st.markdown("**Integrity verification questions**\n" + "\n".join(f"1. {theme.md_escape(q)}" for q in integrity))
