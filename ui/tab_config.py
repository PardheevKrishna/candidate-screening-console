"""Tab 4 - Config & Rubric Tuning: edit the AppConfig, preview its impact, re-score, import/export profiles."""
from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

import pandas as pd
import streamlit as st

from screening import analytics, pipeline
from screening import schema as S
from screening import skills as skills_mod
from screening.config import DEFAULT_REQUIRED_SKILLS, LLM_PROVIDERS, AppConfig
from screening.models import PipelineResult
from ui import state, theme
from ui.charts import num, split_list, txt

log = logging.getLogger(__name__)

K_PREVIEW = "_cfg_preview"  # {"config": AppConfig, "diff": dict, "changes": list[str], "basis": str}
PREVIEW_ROWS = 25           # rows shown in "Biggest rank moves"
SKILL_MODES = {"per_skill": "Per skill: fixed points per matched skill (full marks at N skills)",
               "coverage": "Coverage (matched ÷ required)"}
METHODS = {"top_n": "Top N", "top_pct": "Top % of eligible", "threshold": "Score threshold"}
RESUME_POLICIES = {"zero": "Score resume components as 0", "renormalize": "Rescale available components to 100"}
POLICIES = {False: "Keep & flag (default): flagged candidates stay shortlisted with evidence",
            True: "Hold & backfill: High-risk candidates move to Integrity Hold; slots are backfilled"}
CAP_NOTE = ("Limits the share of the shortlist a single college can take (0 = no cap). Candidates beyond the cap "
            "are not shortlisted and the next-best candidates fill the slots (Top N / Top %); with a score "
            "threshold they are simply excluded. Candidates with no college on record are never capped.")
RISK_NUMBERS = [  # (field, label, min, max, step)
    ("focus_threshold_pct", "Focus group: HackerRank % at least", 0.0, 100.0, 1.0),
    ("high_threshold", "High-risk threshold", 0.0, 100.0, 1.0),
    ("medium_threshold", "Medium-risk threshold", 0.0, 100.0, 1.0),
    ("edge_threshold", "Pair score to link candidates", 0.0, 100.0, 1.0),
    ("text_similarity_high", "Resume similarity: high", 0.0, 1.0, 0.01),
    ("text_similarity_medium", "Resume similarity: medium", 0.0, 1.0, 0.01),
    ("sync_window_sec", "Sync window (seconds)", 0.0, 3600.0, 10.0),
    ("duration_tolerance_min", "Duration match tolerance (min)", 0.0, 60.0, 0.5),
    ("proxy_gap_max_min", "Proxy gap on same IP (min)", 0.0, 240.0, 1.0),
    ("speed_anomaly_ratio", "Speed anomaly: share of focus median", 0.0, 1.0, 0.05),
    ("common_hash_max_share", "Answer hash 'common' above share", 0.0, 1.0, 0.01),
    ("lift_min", "Commonality: minimum lift", 1.0, 20.0, 0.5),
    ("lift_alpha", "Commonality: FDR alpha", 0.001, 0.5, 0.005),
]
RISK_INTS = [("shared_ip_max_group", "IP shared by more than N = lab/NAT", 1, 500),
             ("tab_switch_threshold", "Tab switches threshold", 1, 100),
             ("copy_paste_threshold", "Copy-paste events threshold", 1, 100),
             ("lift_min_count", "Commonality: minimum count", 1, 100),
             ("min_bullet_words", "Shared bullet: minimum words", 3, 50)]


def _skill_options(cfg: AppConfig, result: PipelineResult | None) -> list[str]:
    options = list(dict.fromkeys([*cfg.skills.required_skills, *DEFAULT_REQUIRED_SKILLS]))
    taxonomy = getattr(skills_mod, "__dict__", {}).get("SKILL_TAXONOMY")  # tolerant of a missing taxonomy
    if isinstance(taxonomy, dict):
        options += [str(k) for k in taxonomy]
    if result is not None and S.SKILLS_FOUND in result.candidates.columns:
        for cell in txt(result.candidates, S.SKILLS_FOUND).unique():
            options += split_list(cell)
    return list(dict.fromkeys(options))


def _clamp(value: Any, lo: float, hi: float, cast: type = float) -> Any:
    """Coerce a (possibly user-uploaded) config value into a widget's valid range."""
    try:
        v = cast(value)
    except (TypeError, ValueError):
        v = cast(lo)
    return cast(min(hi, max(lo, v)))


def _num_grid(prefix: str, items: list[tuple[str, str, Any, Any, Any]], values: dict[str, Any],
              ncols: int = 3) -> dict[str, Any]:
    """Grid of number inputs; integer fields are recognised by int bounds and step."""
    out: dict[str, Any] = {}
    cols = st.columns(ncols)
    for i, (field, label, lo, hi, step) in enumerate(items):
        cast = int if all(isinstance(x, int) for x in (lo, hi, step)) else float
        lo, hi, step = cast(lo), cast(hi), cast(step)
        value = _clamp(values.get(field, lo), lo, hi, cast)
        out[field] = cast(cols[i % ncols].number_input(label, lo, hi, value, step, key=f"{prefix}_{field}"))
    return out


def _form_config(cfg: AppConfig, v: dict[str, Any]) -> AppConfig:
    """The AppConfig described by the submitted form values (shared by Apply and Preview)."""
    lc = cfg.llm
    data = cfg.to_dict()
    data["weights"].update(v["weights"])
    data["skills"].update(required_skills=[str(s).strip() for s in v["required"] if str(s).strip()], mode=v["mode"],
                          skills_for_full_marks=int(v["full"]))
    data["experience"].update(v["exp"], job_description=v["jd"] or "",
                              internships_for_full=int(v["exp"]["internships_for_full"]),
                              projects_for_full=int(v["exp"]["projects_for_full"]))
    data["education"].update(v["edu"], tier_scores=v["tier_scores"], certs_for_full=int(v["edu"]["certs_for_full"]))
    data["shortlist"].update(method=v["method"], top_n=int(v["top_n"]), top_pct=float(v["top_pct"]),
                             threshold=float(v["threshold"]), min_hackerrank_pct=float(v["min_pct"]),
                             min_cgpa=float(v["min_cgpa"]), allowed_grad_years=sorted(int(y) for y in v["grad_years"]),
                             missing_resume_policy=v["no_resume"], exclude_high_risk=bool(v["exclude"]),
                             review_risk_levels=list(v["review_levels"]), college_cap_pct=float(v["college_cap"]))
    data["risk"].update(v["risk_nums"], **v["risk_ints"], compare_against_all=bool(v["compare_all"]),
                        pair_weights=v["pair_w"], individual_weights=v["ind_w"])
    data["llm"].update(enabled=bool(v["llm_on"]), model=(v["model"] or lc.model).strip(),
                       max_concurrency=int(v["concurrency"]), temperature=float(v["temperature"]),
                       cutoff_band=int(v["band"]), blend_weight=float(v["blend"]))
    return AppConfig.from_dict(data)


def render(result: PipelineResult | None) -> None:
    cfg, ver = state.get_config(), state.config_version()
    k = f"cfg{ver}"
    for problem in cfg.validate():
        st.warning(problem, icon=":material/rule:")
    prepared = state.get_prepared()
    can_preview = result is not None and prepared is not None
    with st.form(f"{k}_form", border=True):
        st.markdown("##### Rubric weights (points)")
        wc = st.columns(4)
        weights = {name: float(wc[i].number_input(label, 0.0, 100.0, _clamp(getattr(cfg.weights, name), 0.0, 100.0), 1.0,
                                                  key=f"{k}_w_{name}"))
                   for i, (name, label) in enumerate((("hackerrank", "HackerRank"), ("skills", "Skills"),
                                                      ("experience", "Experience"), ("education", "Education")))}
        st.caption(f"Applied weights sum to {cfg.weights.total():.0f}. Weights are always rescaled to 100 when "
                   "scoring, so only their ratios matter.")

        st.markdown("##### Skills")
        required = st.multiselect("Required skills", _skill_options(cfg, result), default=cfg.skills.required_skills,
                                  accept_new_options=True, key=f"{k}_req_skills",
                                  help="Type to add a skill outside the taxonomy (matched as a whole word).")
        sc1, sc2 = st.columns([2, 1])
        mode = sc1.radio("Skills scoring", list(SKILL_MODES), format_func=SKILL_MODES.get, key=f"{k}_skill_mode",
                         index=list(SKILL_MODES).index(cfg.skills.mode) if cfg.skills.mode in SKILL_MODES else 0)
        full = sc2.number_input("Skills for full marks", 1, 20, _clamp(cfg.skills.skills_for_full_marks, 1, 20, int), 1,
                                key=f"{k}_skills_full")

        st.markdown("##### Experience")
        jd = st.text_area("Job description (drives project/experience relevance)", cfg.experience.job_description,
                          height=180, key=f"{k}_jd")
        e = cfg.experience
        exp = _num_grid(f"{k}_exp", [("w_internships", "Weight: internships", 0.0, 1.0, 0.05),
                                     ("w_duration", "Weight: duration", 0.0, 1.0, 0.05),
                                     ("w_projects", "Weight: projects", 0.0, 1.0, 0.05),
                                     ("w_relevance", "Weight: JD relevance", 0.0, 1.0, 0.05),
                                     ("internships_for_full", "Internships for full marks", 1, 10, 1),
                                     ("months_for_full", "Months for full marks", 1.0, 48.0, 1.0),
                                     ("projects_for_full", "Projects for full marks", 1, 10, 1)], vars(e), ncols=4)

        st.markdown("##### Education")
        ed = cfg.education
        edu = _num_grid(f"{k}_edu", [("w_degree", "Weight: degree relevance", 0.0, 1.0, 0.05),
                                     ("w_cgpa", "Weight: CGPA", 0.0, 1.0, 0.05),
                                     ("w_certifications", "Weight: certifications", 0.0, 1.0, 0.05),
                                     ("w_tier", "Weight: institution tier", 0.0, 1.0, 0.05),
                                     ("cgpa_floor", "CGPA floor (scores 0)", 0.0, 10.0, 0.1),
                                     ("cgpa_ceiling", "CGPA ceiling (scores 1)", 0.0, 10.0, 0.1),
                                     ("certs_for_full", "Certifications for full marks", 1, 10, 1)], vars(ed), ncols=4)
        st.caption("Fairness: institution tier can act as a proxy for socio-economic background. Keep its weight low "
                   "(or 0) and review shortlist composition by college before finalising.")
        tiers = sorted(ed.tier_scores, key=str)
        tc = st.columns(max(3, len(tiers)))
        tier_scores = {t: float(tc[i].number_input(f"Tier {t} score", 0.0, 1.0, _clamp(ed.tier_scores[t], 0.0, 1.0), 0.05,
                                                   key=f"{k}_tier_{t}")) for i, t in enumerate(tiers)}

        st.markdown("##### Shortlist & eligibility")
        sl = cfg.shortlist
        s1, s2, s3, s4 = st.columns(4)
        method = s1.radio("Method", list(METHODS), format_func=METHODS.get, key=f"{k}_method",
                          index=list(METHODS).index(sl.method) if sl.method in METHODS else 0)
        top_n = s2.number_input("Top N", 1, 100000, _clamp(sl.top_n, 1, 100000, int), 10, key=f"{k}_top_n")
        top_pct = s3.number_input("Top %", 0.1, 100.0, _clamp(sl.top_pct, 0.1, 100.0), 0.5, key=f"{k}_top_pct")
        threshold = s4.number_input("Score threshold", 0.0, 100.0, _clamp(sl.threshold, 0.0, 100.0), 1.0, key=f"{k}_threshold")
        g1, g2, g3, g4 = st.columns(4)
        min_pct = g1.number_input("Min HackerRank %", 0.0, 100.0, _clamp(sl.min_hackerrank_pct, 0.0, 100.0), 5.0, key=f"{k}_min_pct")
        min_cgpa = g2.number_input("Min CGPA", 0.0, 10.0, _clamp(sl.min_cgpa, 0.0, 10.0), 0.1, key=f"{k}_min_cgpa")
        years = sorted({*range(2020, 2031), *sl.allowed_grad_years, *_data_years(result)})
        grad_years = g3.multiselect("Allowed graduation years", years, default=list(sl.allowed_grad_years),
                                    key=f"{k}_years", placeholder="Any year")
        no_resume = g4.radio("Candidates without a resume", list(RESUME_POLICIES), format_func=RESUME_POLICIES.get,
                             key=f"{k}_no_resume", index=list(RESUME_POLICIES).index(sl.missing_resume_policy)
                             if sl.missing_resume_policy in RESUME_POLICIES else 0)
        cap1, cap2 = st.columns([1, 3], vertical_alignment="center")
        college_cap = cap1.number_input("Max share of shortlist per college (%)", 0.0, 100.0,
                                        _clamp(getattr(sl, "college_cap_pct", 0.0), 0.0, 100.0), 1.0,
                                        key=f"{k}_college_cap", help="0 = no cap.")
        cap2.caption(CAP_NOTE)

        st.markdown("##### Integrity policy")
        exclude = st.radio("When a shortlisted candidate is High risk", [False, True], format_func=POLICIES.get,
                           index=1 if sl.exclude_high_risk else 0, key=f"{k}_policy")
        review_levels = st.multiselect("Risk levels that mark a shortlisted candidate for integrity review",
                                       [S.RISK_HIGH, S.RISK_MEDIUM, S.RISK_LOW], default=[
                                           x for x in sl.review_risk_levels if x in (S.RISK_HIGH, S.RISK_MEDIUM, S.RISK_LOW)],
                                       key=f"{k}_review_levels")
        with st.expander("Risk thresholds & signal weights"):
            r = cfg.risk
            risk_nums = _num_grid(f"{k}_risk", RISK_NUMBERS, vars(r))
            ic = st.columns(len(RISK_INTS))
            risk_ints = {f: int(ic[i].number_input(label, lo, hi, _clamp(getattr(r, f), lo, hi, int), 1, key=f"{k}_ri_{f}"))
                         for i, (f, label, lo, hi) in enumerate(RISK_INTS)}
            compare_all = st.toggle("Also compare perfect scorers' resumes against the whole pool",
                                    value=r.compare_against_all, key=f"{k}_compare_all")
            st.caption("Pair signal weights (summed into a 0-100 pair score)")
            pair_w = _num_grid(f"{k}_pw", [(n, n.replace("_", " "), 0.0, 100.0, 1.0) for n in r.pair_weights],
                               r.pair_weights, ncols=4)
            st.caption("Individual flag weights")
            ind_w = _num_grid(f"{k}_iw", [(n, n.replace("_", " "), 0.0, 100.0, 1.0) for n in r.individual_weights],
                              r.individual_weights, ncols=4)

        provider = LLM_PROVIDERS.get(cfg.llm.provider, {}).get("label", cfg.llm.provider)
        st.markdown(f"##### LLM ({provider})")
        lc = cfg.llm
        l1, l2, l3 = st.columns([1, 2, 1])
        llm_on = l1.toggle("LLM features enabled", value=lc.enabled, key=f"{k}_llm_on")
        model = l2.text_input("Model", lc.model, key=f"{k}_llm_model")
        concurrency = l3.number_input("Max concurrency", 1, 32, _clamp(lc.max_concurrency, 1, 32, int), 1, key=f"{k}_llm_conc")
        l4, l5, l6 = st.columns(3)
        temperature = l4.slider("Temperature", 0.0, 1.0, _clamp(lc.temperature, 0.0, 1.0), 0.05, key=f"{k}_llm_temp")
        band = l5.number_input("Re-score band (± ranks around cutoff)", 0, 5000, _clamp(lc.cutoff_band, 0, 5000, int), 5,
                               key=f"{k}_llm_band")
        blend = l6.slider("LLM blend weight (experience)", 0.0, 1.0, _clamp(lc.blend_weight, 0.0, 1.0), 0.05,
                          key=f"{k}_llm_blend")
        b1, b2, b3 = st.columns([1, 1, 2], vertical_alignment="center")
        submitted = b1.form_submit_button("Apply & re-score", type="primary", icon=":material/published_with_changes:",
                                          key="cfg_apply", width="stretch")
        previewed = b2.form_submit_button("Preview impact", icon=":material/difference:", key="cfg_preview",
                                          disabled=not can_preview, width="stretch",
                                          help="See who would enter or leave the shortlist before applying.")
        b3.caption("Preview shows how the shortlist would change; nothing is applied until you confirm."
                   if can_preview else "Preview impact is available after the first screening run.")

    if submitted or previewed:
        values = dict(weights=weights, required=required, mode=mode, full=full, jd=jd, exp=exp, edu=edu,
                      tier_scores=tier_scores, method=method, top_n=top_n, top_pct=top_pct, threshold=threshold,
                      min_pct=min_pct, min_cgpa=min_cgpa, grad_years=grad_years, no_resume=no_resume,
                      college_cap=college_cap, exclude=exclude, review_levels=review_levels, risk_nums=risk_nums,
                      risk_ints=risk_ints, compare_all=compare_all, pair_w=pair_w, ind_w=ind_w, llm_on=llm_on,
                      model=model, concurrency=concurrency, temperature=temperature, band=band, blend=blend)
        try:
            candidate = _form_config(cfg, values)
        except Exception as exc:  # noqa: BLE001 - never raise into the UI
            log.warning("Could not build the configuration (%s)", type(exc).__name__)
            st.error(f"Could not read the form: {state.safe_error(exc)}")
        else:
            if submitted:
                _apply(candidate, "Configuration applied - candidates re-scored.")
            else:
                _preview(result, prepared, cfg, candidate)
    _preview_panel(result)
    _tools(cfg)


def _data_years(result: PipelineResult | None) -> set[int]:
    if result is None:
        return set()
    return {int(y) for y in num(result.candidates, S.GRAD_YEAR).dropna().unique() if 1990 <= y <= 2100}


def _apply(candidate: AppConfig, message: str) -> None:
    st.session_state.pop(K_PREVIEW, None)
    state.set_config(candidate)
    state.queue_message(message, icon=":material/tune:")
    st.rerun()


# ---------------------------------------------------------------------------
# What-if preview
# ---------------------------------------------------------------------------
def _basis() -> str:
    """What a preview was computed against: config version + the current result's evaluation key."""
    return f"{state.config_version()}|{st.session_state.get(state.K_RESULT_KEY, '')}"


def _short_value(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        text = ", ".join(str(v) for v in value) or "none"
    elif isinstance(value, float):
        text = f"{value:g}"
    else:
        text = str(value)
    return text if len(text) <= 60 else text[:59] + "…"


def config_changes(old: AppConfig, new: AppConfig) -> list[str]:
    """Readable list of settings that differ, e.g. 'shortlist › top n: 300 → 250'."""
    out: list[str] = []

    def walk(a: Any, b: Any, path: list[str]) -> None:
        if isinstance(a, dict) and isinstance(b, dict):
            for key in sorted(set(a) | set(b), key=str):
                walk(a.get(key), b.get(key), [*path, str(key)])
            return
        if a == b:
            return
        label = " › ".join(p.replace("_", " ") for p in path)
        if any(isinstance(x, str) and len(x) > 60 for x in (a, b)):
            out.append(f"{label} edited")
        else:
            out.append(f"{label}: {_short_value(a)} → {_short_value(b)}")

    walk(old.to_dict(), new.to_dict(), [])
    return out


def _preview(result: PipelineResult | None, prepared: Any, cfg: AppConfig, candidate: AppConfig) -> None:
    """Evaluate the proposed config on the current data and store the shortlist diff."""
    if result is None or prepared is None:
        st.info("Run a screening first - the preview compares against the current results.")
        return
    llm_results = dict(state.llm_store(state.K_LLM_RESCORE)) or None
    try:
        # Reuse the current integrity analysis when the risk settings are unchanged (as re-scoring does).
        same_risk = isinstance(result.config, AppConfig) and asdict(candidate.risk) == asdict(result.config.risk)
        with st.spinner("Scoring the proposed configuration..."):
            preview = pipeline.evaluate(prepared, candidate, llm_results,
                                        risk_result=result.risk if same_risk else None)
            diff = analytics.shortlist_diff(result.candidates, preview.candidates)
    except Exception as exc:  # noqa: BLE001 - show the failure, keep the app alive
        log.warning("Config preview failed (%s)", type(exc).__name__)
        st.session_state.pop(K_PREVIEW, None)
        st.error(f"Could not preview these settings: {state.safe_error(exc)}")
        return
    st.session_state[K_PREVIEW] = {"config": candidate, "diff": diff, "changes": config_changes(cfg, candidate),
                                   "basis": _basis()}


def _delta_metric(col: Any, label: str, before: float | None, after: float | None, fmt: str = "{:,.0f}") -> None:
    now = "-" if before is None else fmt.format(before)
    if after is None:
        col.metric(label, "-", border=True, help=f"Current: {now}")
        return
    delta: Any = None
    if before is not None:
        change = round(after - before, 6)
        delta = 0 if change == 0 else ("+" if change > 0 else "") + fmt.format(change)
    col.metric(label, fmt.format(after), delta=delta, delta_color="off", border=True,
               delta_description=None if before is None else f"vs {now} now")


def _diff_config() -> dict[str, Any]:
    cc = st.column_config
    return {
        S.CANDIDATE_ID: cc.TextColumn("Candidate ID", pinned=True), S.NAME: cc.TextColumn("Name"),
        S.COLLEGE: cc.TextColumn("College"),
        "rank_before": cc.NumberColumn("Rank now", format="%d"),
        "rank_after": cc.NumberColumn("Rank after", format="%d"),
        "rank_change": cc.NumberColumn("Rank change", format="%+d", help="Positive = moves up"),
        "status_before": cc.TextColumn("Status now"), "status_after": cc.TextColumn("Status after"),
        "score_before": cc.NumberColumn("Score now", format="%.2f"),
        "score_after": cc.NumberColumn("Score after", format="%.2f"),
        "reason_after": cc.TextColumn("Reason after", width="large"),
    }


def _rows(frame: Any) -> int:
    return len(frame) if isinstance(frame, pd.DataFrame) else 0


def _near_shortlist_moves(moves: Any) -> Any:
    """Biggest rank moves among candidates shortlisted before or after the change."""
    if not isinstance(moves, pd.DataFrame) or moves.empty:
        return moves
    if {"shortlisted_before", "shortlisted_after"} <= set(moves.columns):
        moves = moves[moves["shortlisted_before"].astype(bool) | moves["shortlisted_after"].astype(bool)]
    return moves.head(PREVIEW_ROWS)


def _diff_table(frame: Any, columns: list[str], empty: str, key: str) -> None:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        st.caption(empty)
        return
    st.dataframe(frame, hide_index=True, width="stretch", column_order=[c for c in columns if c in frame.columns],
                 column_config=_diff_config(), height=min(420, 42 + 35 * len(frame)), key=key)


def _preview_panel(result: PipelineResult | None) -> None:
    preview = st.session_state.get(K_PREVIEW)
    if not isinstance(preview, dict):
        return
    if result is None or preview.get("basis") != _basis() or not isinstance(preview.get("diff"), dict):
        st.session_state.pop(K_PREVIEW, None)  # data or configuration changed since the preview
        return
    diff = preview["diff"]
    s = diff.get("summary", {})
    with st.container(border=True, key="cfg_preview_panel"):
        st.markdown("##### :material/difference: Preview: impact of the proposed changes")
        changes = preview.get("changes") or []
        if changes:
            shown = "; ".join(theme.md_escape(c) for c in changes[:12])
            st.caption(f"Proposed changes: {shown}" + (f" (+{len(changes) - 12} more)" if len(changes) > 12 else ""))
        else:
            st.caption("The form matches the active configuration.")
        m = st.columns(5)
        _delta_metric(m[0], "Shortlisted", s.get("shortlisted_before"), s.get("shortlisted_after"))
        m[1].metric("Entering", f"{int(s.get('entering', 0)):,}", border=True,
                    help="Not shortlisted now, shortlisted after the change")
        m[2].metric("Leaving", f"{int(s.get('leaving', 0)):,}", border=True,
                    help="Shortlisted now, not shortlisted after the change")
        _delta_metric(m[3], "Cutoff score", s.get("cutoff_before"), s.get("cutoff_after"), fmt="{:.2f}")
        _delta_metric(m[4], "Integrity reviews", s.get("review_before"), s.get("review_after"))
        if not (s.get("entering") or s.get("leaving") or s.get("status_changes")):
            st.success("No candidate would change status with these settings.", icon=":material/check_circle:")
        entering, leaving = diff.get("entering"), diff.get("leaving")
        changed, moves = diff.get("status_changes"), _near_shortlist_moves(diff.get("rank_moves"))
        t1, t2, t3, t4 = st.tabs([f"Entering ({_rows(entering):,})", f"Leaving ({_rows(leaving):,})",
                                  f"Status changes ({_rows(changed):,})", "Biggest rank moves"])
        with t1:
            _diff_table(entering, [S.CANDIDATE_ID, S.NAME, S.COLLEGE, "rank_before", "rank_after", "status_before",
                                   "status_after", "score_after", "reason_after"],
                        "Nobody new enters the shortlist.", "cfg_pv_entering")
        with t2:
            _diff_table(leaving, [S.CANDIDATE_ID, S.NAME, S.COLLEGE, "rank_before", "rank_after", "status_before",
                                  "status_after", "score_after", "reason_after"],
                        "Nobody leaves the shortlist.", "cfg_pv_leaving")
        with t3:
            _diff_table(changed, [S.CANDIDATE_ID, S.NAME, S.COLLEGE, "status_before", "status_after", "rank_before",
                                  "rank_after", "reason_after"], "No status changes.", "cfg_pv_status")
        with t4:
            _diff_table(moves, [S.CANDIDATE_ID, S.NAME, S.COLLEGE, "rank_before", "rank_after", "rank_change",
                                "score_before", "score_after", "status_after"],
                        "No rank changes among shortlisted candidates.", "cfg_pv_moves")
            st.caption(f"Largest rank changes among candidates shortlisted now or after the change "
                       f"(top {PREVIEW_ROWS}).")
        c1, c2, _ = st.columns([1, 1, 2])
        apply = c1.button("Apply these changes", type="primary", icon=":material/published_with_changes:",
                          key="cfg_preview_apply", width="stretch")
        discard = c2.button("Discard preview", icon=":material/close:", key="cfg_preview_discard", width="stretch",
                            help="Close the preview and reset the form to the active configuration.")
    if apply and isinstance(preview.get("config"), AppConfig):
        _apply(preview["config"], "Previewed changes applied - candidates re-scored.")
    elif discard:
        st.session_state.pop(K_PREVIEW, None)
        state.set_config(state.get_config())  # new widget keys -> the form shows the active configuration again
        st.rerun()


# ---------------------------------------------------------------------------
# Profiles & connectivity
# ---------------------------------------------------------------------------
def _tools(cfg: AppConfig) -> None:
    st.markdown("##### Profiles & connectivity")
    c1, c2, c3 = st.columns(3)
    with c1:
        st.download_button("Download current config (JSON)", data=cfg.to_json(), file_name="screening_config.json",
                           mime="application/json", on_click="ignore", icon=":material/download:", key="cfg_dl",
                           width="stretch")
        if st.button("Reset to defaults", icon=":material/restart_alt:", key="cfg_reset", width="stretch"):
            st.session_state.pop(K_PREVIEW, None)
            state.set_config(AppConfig())
            state.queue_message("Configuration reset to defaults.", icon=":material/restart_alt:")
            st.rerun()
    with c2:
        uploaded = st.file_uploader("Upload a config JSON", type=["json"], key="cfg_upload")
        if uploaded is not None and st.button("Apply uploaded config", key="cfg_apply_upload", width="stretch"):
            try:
                new = AppConfig.from_json(uploaded.getvalue().decode("utf-8-sig"))
            except Exception as exc:  # noqa: BLE001 - any malformed file
                st.error(f"Could not read that config: {state.safe_error(exc)}")
            else:
                st.session_state.pop(K_PREVIEW, None)
                state.set_config(new)
                state.queue_message("Uploaded configuration applied.", icon=":material/upload:")
                st.rerun()
    with c3:
        ready, reason = state.llm_ready(cfg)
        if st.button("Test LLM connection", icon=":material/network_check:", key="cfg_llm_test", disabled=not ready,
                     width="stretch"):
            client = state.get_llm_client(cfg)
            try:
                ok, message = client.test_connection() if client is not None else (False, "No client")
            except Exception as exc:  # noqa: BLE001
                ok, message = False, state.safe_error(exc)
            (st.success if ok else st.error)(f"LLM connection {'OK' if ok else 'failed'}: {message}")
        st.caption(reason or f"Model `{cfg.llm.model}` at {cfg.llm.base_url}")
