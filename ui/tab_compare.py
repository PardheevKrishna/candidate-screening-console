"""Compare Candidates tab - two to four candidates side by side.

Header cards, a metric-by-metric table with the best value of each numeric row highlighted,
the required-skills matrix (implied skills count as matched, exactly as in scoring), an
overlaid sub-score radar, resume highlights and a CSV export of the comparison. The selection
lives in ``state.K_COMPARE_IDS`` so the Individual Deep-Dive "Add to compare" button feeds it.
"""
from __future__ import annotations

import logging
import math
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from screening import schema as S
from screening import skills as skill_taxonomy
from screening.config import AppConfig
from screening.models import ParsedResume, PipelineResult
from ui import charts, state, theme
from ui.charts import SHORTLIST_STATUSES, SUB_LABELS, split_list, txt
from ui.tab_deepdive import COMPARE_MAX, MATCH_LABELS, candidate_options, clean_compare_ids
from ui.tab_shortlist import csv_bytes

log = logging.getLogger(__name__)

K_SELECT = "cmp_select"            # multiselect widget; mirrored into state.K_COMPARE_IDS
COMPARE_MIN = 2
DEFAULT_PICKS = 3                  # default selection: the top 3 shortlisted by rank
METRIC = "Metric"
SKILL = "Required skill"
TOP_PROJECTS = 3
MAX_INTERNSHIPS = 4
MAX_TECH_CHIPS = 8
MATCHED_MARK, MISSING_MARK = "✅", "❌"
BEST_STYLE = f"background-color: #FFF4C2; color: {theme.SLATE}; font-weight: 600;"
# One brand colour per selected candidate; dash style and marker keep the two golds distinguishable.
RADAR_COLORS = (theme.RED, theme.GOLD, theme.SLATE, theme.GOLD_DARK)
RADAR_DASHES = ("solid", "dash", "dot", "dashdot")
RADAR_SYMBOLS = ("circle", "square", "diamond", "triangle-up")
_FONT = "Inter, system-ui, -apple-system, Segoe UI, Roboto, sans-serif"

Cell = tuple[Any, str, float]  # (value for the CSV, text shown in the table, number compared for "best")


# ---------------------------------------------------------------------------
# Row access (columns may be missing; values may be NaN)
# ---------------------------------------------------------------------------
def _raw(row: pd.Series, name: str) -> Any:
    """Cell value, or None when the column is absent or the value is missing."""
    if name not in row.index:
        return None
    value = row[name]
    try:
        return None if pd.isna(value) else value
    except (TypeError, ValueError):  # list-like cell
        return value


def _number(row: pd.Series, *names: str) -> float:
    """First finite numeric value among ``names`` (NaN when there is none)."""
    for name in names:
        value = _raw(row, name)
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            return number
    return math.nan


def _text(row: pd.Series, *names: str) -> str:
    """First non-blank value among ``names`` as text ('' when there is none)."""
    for name in names:
        value = _raw(row, name)
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _compact(value: float, digits: int = 1) -> str:
    """12.0 -> '12', 6.5 -> '6.5' (at most ``digits`` decimals)."""
    text = f"{value:.{digits}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _plain(text: str) -> str:
    """Chart labels: keep data-derived text from being read as markup."""
    return str(text).replace("<", "‹").replace(">", "›")


def selected_rows(df: pd.DataFrame, ids: Sequence[str]) -> list[tuple[str, pd.Series]]:
    """(candidate_id, row) for every id present in ``df``, in the order given (duplicates dropped)."""
    if df is None or df.empty or S.CANDIDATE_ID not in df.columns:
        return []
    key = txt(df, S.CANDIDATE_ID)
    out: list[tuple[str, pd.Series]] = []
    for cid in dict.fromkeys(str(i) for i in ids):
        hit = df[key == cid]
        if not hit.empty:
            out.append((cid, hit.iloc[0]))
    return out


def column_label(cid: str, row: pd.Series) -> str:
    """'ID · Name' header for one candidate (just the ID when the name is unknown)."""
    name = _text(row, S.NAME)
    return f"{cid} · {name}" if name else cid


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------
def default_compare_ids(df: pd.DataFrame, count: int = DEFAULT_PICKS) -> list[str]:
    """Top ``count`` shortlisted candidates by rank; the overall rank order when fewer than two are shortlisted."""
    ranked, _ = candidate_options(df[txt(df, S.STATUS).isin(SHORTLIST_STATUSES)])
    if len(ranked) < COMPARE_MIN:
        ranked, _ = candidate_options(df)
    return ranked[:count]


def resolve_selection(stored: Any, valid_ids: Collection[str],
                      defaults: Sequence[str] | Callable[[], Sequence[str]]) -> list[str]:
    """The stored compare list (unknown ids dropped, capped at COMPARE_MAX), else the defaults.

    Defaults apply when nothing was stored yet or every stored id belongs to an earlier dataset.
    An emptied list stays empty, so a cleared selection is not refilled on the next rerun.
    """
    ids = clean_compare_ids(stored, valid_ids)
    if not isinstance(stored, (list, tuple)) or (stored and not ids):
        picks = defaults() if callable(defaults) else defaults
        return clean_compare_ids(list(picks), valid_ids)
    return ids


def _remember_selection() -> None:
    """Multiselect callback: keep the picks in the shared compare list (runs before the next render)."""
    st.session_state[state.K_COMPARE_IDS] = clean_compare_ids(st.session_state.get(K_SELECT))


# ---------------------------------------------------------------------------
# Comparison table
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Metric:
    """One row of the comparison table."""

    label: str
    sources: tuple[str, ...]              # the row is shown when any of these columns exists
    cell: Callable[[pd.Series], Cell]
    better: str | None = None             # "high" / "low": highlight the best value; None: not ranked


@dataclass(frozen=True)
class Comparison:
    display: pd.DataFrame                 # METRIC + one text column per candidate
    raw: pd.DataFrame                     # METRIC + the underlying values (CSV export)
    best: pd.DataFrame                    # bool, same shape as ``display``: best value of each numeric row


def _csv_value(value: float) -> float | int:
    return int(value) if value.is_integer() else value


def _numeric(label: str, sources: tuple[str, ...], digits: int, better: str | None = None, *,
             prefix: str = "", suffix: str = "", compact: bool = False) -> Metric:
    def cell(row: pd.Series) -> Cell:
        value = _number(row, *sources)
        if math.isnan(value):
            return math.nan, "-", math.nan
        shown = _compact(value, digits) if compact else f"{value:.{digits}f}"
        return _csv_value(value), f"{prefix}{shown}{suffix}", round(value, digits)
    return Metric(label, sources, cell, better)


def _textual(label: str, sources: tuple[str, ...], show: Callable[[str], str] | None = None) -> Metric:
    def cell(row: pd.Series) -> Cell:
        value = _text(row, *sources)
        return value, (show(value) if show is not None and value else value) or "-", math.nan
    return Metric(label, sources, cell)


def _skills_cell(required_total: int) -> Callable[[pd.Series], Cell]:
    def cell(row: pd.Series) -> Cell:
        matched = split_list(_raw(row, S.MATCHED_SKILLS))
        missing = split_list(_raw(row, S.MISSING_SKILLS))
        n = _number(row, S.N_MATCHED_SKILLS)
        n = float(len(matched)) if math.isnan(n) else n
        total = len(matched) + len(missing) or required_total
        shown = f"{n:.0f} / {total}" if total else f"{n:.0f}"
        return shown, shown, n
    return cell


def _cert_cell(row: pd.Series) -> Cell:
    count = _number(row, S.CERT_COUNT)
    if math.isnan(count) and S.CERTIFICATIONS in row.index:
        count = float(len(split_list(_raw(row, S.CERTIFICATIONS), S.SEP_NOTES)))
    if math.isnan(count):
        return math.nan, "-", math.nan
    return _csv_value(count), f"{count:.0f}", count


def _risk_level_cell(row: pd.Series) -> Cell:
    level = _text(row, S.RISK_LEVEL) or S.RISK_CLEAR
    return level, theme.risk_label(level), math.nan


def _match_cell(row: pd.Series) -> Cell:
    method = _text(row, S.MATCH_METHOD)
    label = MATCH_LABELS.get(method, method) if method else "no resume"
    confidence = _number(row, S.MATCH_CONFIDENCE)
    shown = f"{label} ({confidence:.0%})" if method and not math.isnan(confidence) else label
    return label, shown, math.nan


def _tier(value: str) -> str:
    return f"Tier {value}" if value.isdigit() else value


def metric_rows(required_total: int = 0) -> list[Metric]:
    """Every row the comparison table can show, in display order."""
    points = [(S.PTS_HR, "HackerRank pts"), (S.PTS_SKILLS, "Skills pts"), (S.PTS_EXPERIENCE, "Experience pts"),
              (S.PTS_EDUCATION, "Education pts")]
    return [
        _numeric("Rank", (S.RANK,), 0, "low", prefix="#"),
        _textual("Status", (S.STATUS,)),
        _numeric("Final score", (S.FINAL_SCORE,), 1, "high"),
        _numeric("HackerRank %", (S.HR_PCT,), 1, "high", suffix="%", compact=True),
        *[_numeric(label, (column,), 1, "high") for column, label in points],
        Metric("Required skills matched", (S.N_MATCHED_SKILLS, S.MATCHED_SKILLS, S.MISSING_SKILLS),
               _skills_cell(required_total), "high"),
        _numeric("Internships", (S.INTERNSHIP_COUNT,), 0, "high"),
        _numeric("Experience months", (S.EXPERIENCE_MONTHS,), 1, "high", compact=True),
        _numeric("Projects", (S.PROJECT_COUNT,), 0, "high"),
        Metric("Certifications", (S.CERT_COUNT, S.CERTIFICATIONS), _cert_cell, "high"),
        _textual("Degree", (S.DEGREE, S.RESUME_DEGREE)),
        _textual("Branch", (S.BRANCH,)),
        _numeric("CGPA", (S.CGPA, S.RESUME_CGPA), 2, "high"),
        _textual("College tier", (S.COLLEGE_TIER,), _tier),
        Metric("Risk level", (S.RISK_LEVEL,), _risk_level_cell),
        _numeric("Risk score", (S.RISK_SCORE,), 0, "low"),
        _textual("Cluster", (S.CLUSTER_ID,)),
        Metric("Resume match method", (S.MATCH_METHOD, S.HAS_RESUME), _match_cell),
    ]


def best_flags(scores: Sequence[float], better: str | None) -> list[bool]:
    """True for the best value(s) of one row; nothing is marked when every candidate has the same value."""
    flags = [False] * len(scores)
    if better not in ("high", "low"):
        return flags
    valid = [s for s in scores if not math.isnan(s)]
    if not valid:
        return flags
    target = max(valid) if better == "high" else min(valid)
    if len(valid) == len(scores) and all(math.isclose(s, target, abs_tol=1e-9) for s in valid):
        return flags
    return [not math.isnan(s) and math.isclose(s, target, abs_tol=1e-9) for s in scores]


def build_comparison(df: pd.DataFrame, ids: Sequence[str], required_total: int = 0) -> Comparison:
    """Rows = metrics present in ``df``, columns = the selected candidates (in selection order)."""
    rows = selected_rows(df, ids)
    labels = [column_label(cid, row) for cid, row in rows]
    metrics = [m for m in metric_rows(required_total) if any(c in df.columns for c in m.sources)]
    display: dict[str, list[Any]] = {METRIC: [m.label for m in metrics]}
    raw: dict[str, list[Any]] = {METRIC: [m.label for m in metrics]}
    best: dict[str, list[bool]] = {METRIC: [False] * len(metrics)}
    for label in labels:
        display[label], raw[label], best[label] = [], [], []
    for metric in metrics:
        cells = [metric.cell(row) for _, row in rows]
        for label, (value, shown, _), hit in zip(labels, cells, best_flags([c[2] for c in cells], metric.better)):
            raw[label].append(value)
            display[label].append(shown)
            best[label].append(hit)
    return Comparison(display=pd.DataFrame(display), raw=pd.DataFrame(raw, dtype=object),
                      best=pd.DataFrame(best, dtype=bool))


# ---------------------------------------------------------------------------
# Required skills, radar, export
# ---------------------------------------------------------------------------
def required_skills(cfg: AppConfig | None, df: pd.DataFrame, ids: Sequence[str] = ()) -> list[str]:
    """Required skills in configured order, spelled the way scoring records them."""
    out: list[str] = []
    seen: set[str] = set()

    def add(name: Any) -> None:
        text = " ".join(str(name or "").split())
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            out.append(text)

    canonical = state.engine_fn(skill_taxonomy, "canonicalize_skill")
    for name in (cfg.skills.required_skills if cfg is not None else None) or []:
        if canonical is not None:
            try:
                name = canonical(str(name)) or name
            except Exception:  # noqa: BLE001 - an odd configured name must not break the tab
                pass
        add(name)
    picked = df[txt(df, S.CANDIDATE_ID).isin([str(i) for i in ids])]
    for column in (S.MATCHED_SKILLS, S.MISSING_SKILLS):  # what scoring actually used for these rows
        for value in txt(picked, column):
            for skill in split_list(value):
                add(skill)
    return out


def skills_matrix(df: pd.DataFrame, ids: Sequence[str], required: Sequence[str]) -> pd.DataFrame:
    """Required skill x candidate -> True when matched; read from the matched_skills column, so a skill
    implied by another (AWS -> Cloud, PostgreSQL -> SQL) counts exactly as it does in scoring."""
    data: dict[str, list[Any]] = {SKILL: list(required)}
    for cid, row in selected_rows(df, ids):
        have = {s.casefold() for s in split_list(_raw(row, S.MATCHED_SKILLS))}
        data[column_label(cid, row)] = [s.casefold() in have for s in required]
    return pd.DataFrame(data)


def _rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"


def radar_figure(df: pd.DataFrame, ids: Sequence[str]) -> go.Figure:
    """Overlaid radar of the four 0-1 sub-scores, one trace per selected candidate."""
    rows = selected_rows(df, ids)
    if not rows or not any(k in df.columns for k in SUB_LABELS):
        return charts.empty_figure("Sub-scores are not available for this selection.", height=380)
    cats = list(SUB_LABELS.values())
    fig = go.Figure()
    for i, (cid, row) in enumerate(rows):
        values = []
        for key in SUB_LABELS:
            v = _number(row, key)
            values.append(0.0 if math.isnan(v) else min(1.0, max(0.0, v)))
        color = RADAR_COLORS[i % len(RADAR_COLORS)]
        fig.add_trace(go.Scatterpolar(
            r=values + values[:1], theta=cats + cats[:1], name=_plain(column_label(cid, row)),
            mode="lines+markers", fill="toself", fillcolor=_rgba(color, 0.08),
            line=dict(color=color, width=2.5, dash=RADAR_DASHES[i % len(RADAR_DASHES)]),
            marker=dict(size=8, color=color, symbol=RADAR_SYMBOLS[i % len(RADAR_SYMBOLS)],
                        line=dict(color=theme.SLATE_2, width=1)),
            hovertemplate="<b>%{fullData.name}</b><br>%{theta}: %{r:.0%}<extra></extra>"))
    fig.update_layout(
        template="plotly_white", height=420, showlegend=True, margin=dict(l=40, r=40, t=30, b=40),
        font=dict(family=_FONT, size=12, color=theme.SLATE_2), paper_bgcolor="rgba(0,0,0,0)",
        legend=dict(orientation="h", yanchor="top", y=-0.08, xanchor="center", x=0.5, font=dict(size=11)),
        hoverlabel=dict(bgcolor="#FFFFFF", font=dict(family=_FONT, size=12, color=theme.SLATE)),
        polar=dict(bgcolor="#FFFFFF",
                   radialaxis=dict(range=[0, 1], tickformat=".0%", angle=45, tickangle=45, gridcolor="#E5E5E5",
                                   tickfont=dict(size=9, color=theme.GREY)),
                   angularaxis=dict(gridcolor="#E5E5E5", rotation=90, direction="clockwise")))
    return fig


def comparison_export(df: pd.DataFrame, ids: Sequence[str], comparison: Comparison,
                      matrix: pd.DataFrame | None = None) -> pd.DataFrame:
    """CSV frame: identity rows, every metric (underlying values) and the required-skill matrix."""
    rows = selected_rows(df, ids)
    labels = [column_label(cid, row) for cid, row in rows]
    parts = [pd.DataFrame({METRIC: ["Candidate ID", "Name", "College"],
                           **{label: [cid, _text(row, S.NAME), _text(row, S.COLLEGE)]
                              for label, (cid, row) in zip(labels, rows)}}, dtype=object)]
    if not comparison.raw.empty:
        parts.append(comparison.raw)
    if matrix is not None and not matrix.empty:
        parts.append(pd.DataFrame({METRIC: [f"Required skill: {s}" for s in matrix[SKILL]],
                                   **{label: ["Matched" if hit else "Missing" for hit in matrix[label]]
                                      for label in labels if label in matrix.columns}}, dtype=object))
    return pd.concat(parts, ignore_index=True)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def render(result: PipelineResult | None) -> None:
    df = result.candidates if result is not None else None
    if df is None or df.empty or S.CANDIDATE_ID not in df.columns:
        st.info("No candidates to compare yet. Run a screening, then pick two to four candidates here.")
        return
    try:
        _render(result, df)
    except Exception as exc:  # noqa: BLE001 - keep the rest of the app usable
        log.warning("Compare tab failed (%s)", type(exc).__name__)
        st.error(f"The comparison could not be shown ({state.safe_error(exc)}). Try a different selection.")


def _render(result: PipelineResult, df: pd.DataFrame) -> None:
    ids, labels = candidate_options(df)
    if len(ids) < COMPARE_MIN:
        st.info("This run has fewer than two candidates, so there is nothing to compare.")
        return
    selected = _selector(df, ids, labels)
    if len(selected) < COMPARE_MIN:
        st.info(f"Pick at least {COMPARE_MIN} candidates (up to {COMPARE_MAX}) to compare them side by side. "
                f"You can also use **Add to compare** in the {state.TAB_DEEPDIVE} tab.", icon=":material/info:")
        return
    rows = selected_rows(df, selected)
    _cards(rows)
    required = required_skills(result.config, df, selected)
    comparison = build_comparison(df, selected, len(required))
    matrix = skills_matrix(df, selected, required) if S.MATCHED_SKILLS in df.columns else None
    _table(comparison, comparison_export(df, selected, comparison, matrix))
    _skills_section(matrix, required)
    st.markdown("##### Sub-scores (0-1)")
    _, middle, _ = st.columns([1, 3, 1])
    middle.plotly_chart(radar_figure(df, selected), width="stretch", theme=None, key="cmp_radar")
    st.markdown("##### Resume highlights & integrity")
    parsed = result.parsed_by_candidate or {}
    for col, (cid, row) in zip(st.columns(len(rows)), rows):
        with col, st.expander(theme.md_escape(column_label(cid, row)), icon=":material/person:"):
            _candidate_details(row, parsed.get(cid))


def _selector(df: pd.DataFrame, ids: list[str], labels: dict[str, str]) -> list[str]:
    picked = resolve_selection(st.session_state.get(state.K_COMPARE_IDS), labels, lambda: default_compare_ids(df))
    if st.session_state.get(K_SELECT) != picked:
        st.session_state[K_SELECT] = picked  # seeded before the widget exists in this run
    st.multiselect(f"Candidates to compare ({COMPARE_MIN}-{COMPARE_MAX})", ids, key=K_SELECT,
                   format_func=lambda c: labels.get(c, c), max_selections=COMPARE_MAX, select_all=False,
                   on_change=_remember_selection, placeholder="Search by ID or name",
                   help=f"Defaults to the top {DEFAULT_PICKS} shortlisted candidates. **Add to compare** in the "
                        f"{state.TAB_DEEPDIVE} tab adds candidates here too.")
    selected = clean_compare_ids(st.session_state.get(K_SELECT), labels)
    st.session_state[state.K_COMPARE_IDS] = selected
    return selected


def _cards(rows: list[tuple[str, pd.Series]]) -> None:
    for col, (cid, row) in zip(st.columns(len(rows)), rows):
        with col, st.container(border=True):
            meta = " · ".join(x for x in (cid, _text(row, S.COLLEGE)) if x)
            st.html(f'<div class="wf-profile-name" style="font-size:1.2rem">{theme.esc(_text(row, S.NAME) or cid)}</div>'
                    f'<div class="wf-profile-meta">{theme.esc(meta)}</div>')
            with st.container(horizontal=True, gap="small"):
                theme.status_badge(_text(row, S.STATUS))
                theme.risk_badge(_text(row, S.RISK_LEVEL) or S.RISK_CLEAR)
            score, rank = _number(row, S.FINAL_SCORE), _number(row, S.RANK)
            with st.container(horizontal=True, gap="small"):
                st.metric("Final score", "-" if math.isnan(score) else f"{score:.1f}")
                st.metric("Rank", "-" if math.isnan(rank) else f"#{rank:.0f}")
            st.button("Open profile", key=f"cmp_open_{cid}", icon=":material/person_search:", width="stretch",
                      on_click=state.select_candidate, args=(cid, True),
                      help=f"Open this candidate in the {state.TAB_DEEPDIVE} tab.")


def _table(comparison: Comparison, export_frame: pd.DataFrame) -> None:
    st.markdown("##### Side-by-side metrics")
    if comparison.display.empty:
        st.caption("No comparable metrics are available for this run.")
    else:
        css = comparison.best.map(lambda hit: BEST_STYLE if hit else "")
        styled = comparison.display.style.apply(lambda _frame: css, axis=None)
        st.dataframe(styled, hide_index=True, width="stretch", height=42 + 35 * len(comparison.display),
                     column_config={METRIC: st.column_config.TextColumn(METRIC, pinned=True)})
    note, download = st.columns([2, 1], vertical_alignment="center")
    note.caption("Highlighted cells mark the best value in each numeric row, compared as shown "
                 "(lower is better for rank and risk score).")
    download.download_button("Download comparison (CSV)", data=partial(csv_bytes, export_frame),
                             file_name="candidate_comparison.csv", mime="text/csv", on_click="ignore",
                             icon=":material/download:", key="cmp_dl_csv", width="stretch")


def _skills_section(matrix: pd.DataFrame | None, required: Sequence[str]) -> None:
    st.markdown("##### Required skills")
    if matrix is None:
        st.caption("Required-skill matching is not available in this run.")
        return
    if not required:
        st.caption("No required skills are configured.")
        return
    columns = [c for c in matrix.columns if c != SKILL]
    shown = matrix.assign(**{c: matrix[c].map({True: MATCHED_MARK, False: MISSING_MARK}) for c in columns})
    st.dataframe(shown, hide_index=True, width="stretch", height=42 + 35 * len(shown),
                 column_config={SKILL: st.column_config.TextColumn(SKILL, pinned=True)})
    st.caption(f"{MATCHED_MARK} matched, directly or implied by a related skill (AWS counts as Cloud), exactly as "
               f"in scoring · {MISSING_MARK} missing.")


def _experience_line(entry: Mapping[str, Any]) -> str:
    title = theme.md_escape(entry.get("title") or "Internship")
    org = theme.md_escape(entry.get("org") or "")
    try:
        months = float(entry.get("months"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        months = math.nan
    extra = f" · {months:.0f} months" if math.isfinite(months) and months > 0 else ""
    return f"**{title}**" + (f" - {org}" if org else "") + extra


def _candidate_details(row: pd.Series, parsed: ParsedResume | None) -> None:
    if parsed is None:
        st.caption("No resume is linked to this candidate.")
    else:
        projects = [p for p in (getattr(parsed, "projects", None) or []) if isinstance(p, Mapping)]
        st.markdown(f"**Top projects** ({len(projects)})")
        for project in projects[:TOP_PROJECTS]:
            st.markdown(f"**{theme.md_escape(project.get('title') or 'Untitled project')}**")
            tech = project.get("tech") or []
            tech = split_list(tech) if isinstance(tech, str) else [str(t) for t in tech if str(t).strip()]
            theme.chips(tech[:MAX_TECH_CHIPS], "neutral", empty="No tech stack detected")
        if not projects:
            st.caption("No projects detected.")
        internships = [e for e in (getattr(parsed, "experiences", None) or [])
                       if isinstance(e, Mapping) and e.get("is_internship")]
        st.markdown(f"**Internships** ({len(internships)})")
        if internships:
            st.markdown("\n".join(f"- {_experience_line(e)}" for e in internships[:MAX_INTERNSHIPS]))
        else:
            st.caption("No internships detected.")
    st.markdown("**Integrity signals**")
    reasons = split_list(_raw(row, S.RISK_REASONS), S.SEP_REASONS)
    if reasons:
        st.markdown("\n".join(f"- {theme.md_escape(r)}" for r in reasons))
    else:
        st.caption("No integrity signals recorded.")
