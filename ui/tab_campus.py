"""Campus Distribution: how applicants and the shortlist spread across colleges and campus attributes."""
from __future__ import annotations

import logging
from functools import partial
from typing import Any

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from screening import analytics
from screening import schema as S
from screening.models import PipelineResult
from ui import charts, state, theme
from ui.charts import flag, num, txt
from ui.tab_shortlist import csv_bytes

log = logging.getLogger(__name__)

K_GROUP_BY = "cp_group_by"
GROUPINGS: dict[str, str] = {
    S.COLLEGE: "College", S.COLLEGE_TIER: "College tier", S.CITY: "City", S.DEGREE: "Degree", S.BRANCH: "Branch",
}
PLURALS: dict[str, str] = {
    S.COLLEGE: "Colleges", S.COLLEGE_TIER: "College tiers", S.CITY: "Cities", S.DEGREE: "Degrees",
    S.BRANCH: "Branches",
}
TOP_GROUPS = 20
OVER_CAP_BG = "#FDECEC"  # light brand red
REPRESENTATION_NOTE = (
    "Representation = share of shortlist ÷ share of applicants. 1.0 means a group is shortlisted in "
    "proportion to its applicants; above 1.0 it is over-represented, below 1.0 under-represented. "
    "The shortlist counts Shortlisted and Integrity Review candidates."
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def available_groupings(df: pd.DataFrame) -> list[str]:
    """Group-by columns present in the data with at least one recorded value."""
    return [c for c in GROUPINGS
            if c in df.columns and (analytics.group_labels(df, c) != analytics.UNKNOWN_GROUP).any()]


def display_distribution(dist: pd.DataFrame, by: str) -> pd.DataFrame:
    """Distribution with readable group labels (college tier '1' -> 'Tier 1')."""
    if by != S.COLLEGE_TIER or dist.empty:
        return dist
    labels = [f"Tier {g}" if str(g).isdigit() else str(g) for g in dist["group"]]
    return dist.assign(group=labels)


def _shorten(text: str, limit: int = 42) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def share_chart(dist: pd.DataFrame | None, label: str = "Group", cap_pct: float = 0.0,
                top: int = TOP_GROUPS) -> go.Figure:
    """Share of the shortlist vs share of applicants per group (horizontal bars, top groups first).

    Over-cap groups get hatched, darker bars and a warning sign; a dashed line marks the cap.
    """
    needed = {"group", "share_of_shortlist", "share_of_applicants"}
    if dist is None or dist.empty or not needed <= set(dist.columns):
        return charts.empty_figure(f"No {label.lower()} data to compare yet.")
    df = dist.head(top).iloc[::-1]  # plotly draws the first category at the bottom
    groups = txt(df, "group").tolist()
    short = num(df, "share_of_shortlist").fillna(0.0)
    apps = num(df, "share_of_applicants").fillna(0.0)
    over = flag(df, "over_cap").tolist()
    custom = np.column_stack([num(df, "shortlisted").fillna(0.0), num(df, "applicants").fillna(0.0),
                              num(df, "representation"), short.to_numpy(), apps.to_numpy()])
    hover = ("<b>%{y}</b><br>Share of shortlist %{customdata[3]:.1%} (%{customdata[0]:,.0f} shortlisted)"
             "<br>Share of applicants %{customdata[4]:.1%} (%{customdata[1]:,.0f} applicants)"
             "<br>Representation %{customdata[2]:.2f}x<extra></extra>")
    fig = go.Figure([
        go.Bar(y=groups, x=apps, orientation="h", name="Share of applicants", customdata=custom,
               marker=dict(color=theme.GOLD_DARK), hovertemplate=hover),
        go.Bar(y=groups, x=short, orientation="h", name="Share of shortlist", customdata=custom,
               marker=dict(color=[theme.RED_DARK if o else theme.RED for o in over],
                           pattern=dict(shape=["/" if o else "" for o in over], fgcolor="#FFFFFF", size=6)),
               text=[f"{v:.0%}" for v in short], textposition="outside", cliponaxis=False, hovertemplate=hover),
    ])
    fig.update_layout(barmode="group", bargap=0.28, bargroupgap=0.06)
    high = max(float(short.max()), float(apps.max()), cap_pct / 100.0 if cap_pct > 0 else 0.0, 0.05)
    if cap_pct > 0:
        fig.add_vline(x=cap_pct / 100.0, line=dict(color=theme.SLATE_2, width=2, dash="dash"),
                      annotation_text=f"College cap {cap_pct:g}%", annotation_position="top right",
                      annotation_font=dict(color=theme.SLATE, size=11))
    fig.update_xaxes(title="Share", tickformat=".0%", range=[0, min(1.0, high * 1.2) + 0.02])
    fig.update_yaxes(tickmode="array", tickvals=groups,
                     ticktext=[("⚠ " if o else "") + _shorten(g) for g, o in zip(groups, over)], automargin=True)
    fig = charts._layout(fig, height=max(260, 30 * len(df) + 120))  # shared chart styling
    fig.update_layout(legend=dict(traceorder="reversed"))
    return fig


def _cap(result: PipelineResult) -> float:
    try:
        value = float(getattr(result.config.shortlist, "college_cap_pct", 0.0) or 0.0)
    except (TypeError, ValueError, AttributeError):
        return 0.0
    return value if value > 0 else 0.0


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def render(result: PipelineResult | None) -> None:
    df = None if result is None else result.candidates
    if df is None or df.empty:
        st.info("Run a screening to see how applicants and the shortlist are spread across campuses.",
                icon=":material/school:")
        return
    try:
        options = available_groupings(df)
        if not options:
            st.info("This dataset has no campus attributes (college, tier, city, degree or branch) to group by.",
                    icon=":material/school:")
            return
        if st.session_state.get(K_GROUP_BY) not in options:
            st.session_state[K_GROUP_BY] = options[0]
        by = st.segmented_control("Group by", options, format_func=GROUPINGS.get, key=K_GROUP_BY, required=True,
                                  persist_state="session") or options[0]
        _render_distribution(result, by)
    except Exception as exc:  # noqa: BLE001 - never break the tab on unexpected data
        log.warning("Campus distribution failed (%s)", type(exc).__name__)
        st.error(f"Could not build the campus distribution: {state.safe_error(exc)}")


def _render_distribution(result: PipelineResult, by: str) -> None:
    df, cap = result.candidates, _cap(result)
    dist = analytics.campus_distribution(df, by, result.config)
    colleges = None
    if cap > 0 and S.COLLEGE in df.columns:
        colleges = dist if by == S.COLLEGE else analytics.campus_distribution(df, S.COLLEGE, result.config)
    view = display_distribution(dist, by)
    label = GROUPINGS[by]
    _kpis(view, df, by, cap, colleges)

    show_cap = cap if by == S.COLLEGE else 0.0
    st.plotly_chart(share_chart(view, label, show_cap), width="stretch", theme=None, key="cp_chart")
    if len(view) > TOP_GROUPS:
        st.caption(f"The chart shows the {TOP_GROUPS} {PLURALS[by].lower()} with the most shortlisted candidates; "
                   f"the table lists all {len(view):,}.")
    st.caption(REPRESENTATION_NOTE)
    _table(view, by, cap)
    st.download_button(f"Distribution by {label.lower()} (CSV)",
                       data=partial(csv_bytes, view.rename(columns={"group": by})),
                       file_name=f"campus_distribution_by_{by}.csv", mime="text/csv", on_click="ignore",
                       icon=":material/download:", key="cp_dl_csv", disabled=view.empty)


def _kpis(view: pd.DataFrame, df: pd.DataFrame, by: str, cap: float, colleges: pd.DataFrame | None) -> None:
    label = GROUPINGS[by].lower()
    known = view[view["group"] != analytics.UNKNOWN_GROUP]
    unknown_apps = int(view.loc[view["group"] == analytics.UNKNOWN_GROUP, "applicants"].sum())
    represented = int((known["shortlisted"] > 0).sum())
    total_short = int(view["shortlisted"].sum())
    groups_help = f"Distinct {label} values among applicants."
    if unknown_apps:
        groups_help += f" {unknown_apps:,} applicant(s) have no {label} on record and are grouped as (unknown)."
    cols = st.columns(3)
    cols[0].metric(PLURALS[by], f"{len(known):,}", delta=f"{represented:,} with a shortlisted candidate",
                   delta_color="off", delta_arrow="off", border=True, help=groups_help)
    if total_short:
        top = view.sort_values(["share_of_shortlist", "shortlisted"], ascending=False, kind="mergesort").iloc[0]
        rep = top["representation"]
        cols[1].metric(f"Top {label} share of shortlist", f"{float(top['share_of_shortlist']):.1%}",
                       delta=theme.md_escape(top["group"]), delta_color="off", delta_arrow="off", border=True,
                       help=f"{int(top['shortlisted']):,} of {total_short:,} shortlisted"
                            + (f"; representation {float(rep):.2f}x" if pd.notna(rep) else ""))
    else:
        cols[1].metric(f"Top {label} share of shortlist", "-", delta="Nobody is shortlisted", delta_color="off",
                       delta_arrow="off", border=True)
    if cap > 0:
        over = int(colleges["over_cap"].sum()) if colleges is not None and not colleges.empty else 0
        capped = int(flag(df, S.COLLEGE_CAPPED).sum())
        cols[2].metric("Colleges over the cap", f"{over:,}",
                       delta=f"{capped:,} candidate{'' if capped == 1 else 's'} capped out (cap {cap:g}%)",
                       delta_color="off", delta_arrow="off", border=True,
                       help="Colleges holding a larger share of the shortlist than the cap. This can happen when "
                            "too few candidates from other colleges are available to fill the slots, when the cap "
                            "rounds up to one place, or with a score threshold (no backfill).")
    else:
        cols[2].metric("College cap", "Off", delta="Set it in Config & Rubric Tuning", delta_color="off",
                       delta_arrow="off", border=True,
                       help="Optional limit on the share of the shortlist a single college can take.")


def _column_config(label: str) -> dict[str, Any]:
    cc = st.column_config
    return {
        "group": cc.TextColumn(label, width="large", pinned=True),
        "applicants": cc.NumberColumn("Applicants", format="%d"),
        "eligible": cc.NumberColumn("Eligible", format="%d"),
        "perfect_scorers": cc.NumberColumn("Perfect scorers", format="%d", help="HackerRank 100%"),
        "shortlisted": cc.NumberColumn("Shortlisted", format="%d", help="Shortlisted + Integrity Review"),
        "in_review": cc.NumberColumn("In review", format="%d", help="Shortlisted (Integrity Review)"),
        "capped_out": cc.NumberColumn("Capped out", format="%d",
                                      help="Would have been shortlisted but the college cap was reached"),
        "shortlist_rate": cc.ProgressColumn("Shortlist rate", min_value=0, max_value=1, format="percent",
                                            color=theme.SLATE_2, help="Shortlisted ÷ eligible"),
        "share_of_shortlist": cc.ProgressColumn("Share of shortlist", min_value=0, max_value=1, format="percent",
                                                color=theme.RED),
        "share_of_applicants": cc.ProgressColumn("Share of applicants", min_value=0, max_value=1, format="percent",
                                                 color=theme.GOLD_DARK),
        "representation": cc.NumberColumn("Representation", format="%.2fx",
                                          help="Share of shortlist ÷ share of applicants (1.0 = proportional)"),
        "avg_final_score": cc.NumberColumn("Avg final score", format="%.1f"),
        "avg_hackerrank_pct": cc.NumberColumn("Avg HackerRank %", format="%.0f%%"),
        "flagged": cc.NumberColumn("Risk flagged", format="%d", help="High or Medium integrity risk"),
        "over_cap": cc.CheckboxColumn("Over cap"),
    }


def _highlight_over_cap(view: pd.DataFrame) -> Any:
    """Styler shading over-cap rows (falls back to the plain frame)."""
    try:
        return view.style.apply(
            lambda row: [f"background-color: {OVER_CAP_BG}" if bool(row["over_cap"]) else ""] * len(row), axis=1)
    except Exception:  # noqa: BLE001 - styling is cosmetic
        return view


def _table(view: pd.DataFrame, by: str, cap: float) -> None:
    columns = list(analytics.DISTRIBUTION_COLUMNS)
    show_cap = cap > 0 and by == S.COLLEGE
    if not show_cap:
        columns.remove("over_cap")
    if cap <= 0 and not int(view["capped_out"].sum()):
        columns.remove("capped_out")
    over = show_cap and bool(view["over_cap"].any())
    st.dataframe(_highlight_over_cap(view) if over else view, hide_index=True, width="stretch",
                 column_order=columns, column_config=_column_config(GROUPINGS[by]), key="cp_table",
                 height=min(520, 42 + 35 * max(len(view), 1)))
    if over:
        st.caption(f"Shaded rows hold more than the {cap:g}% college cap of the shortlist.")
