"""Pure Plotly figure builders (no Streamlit calls) plus tolerant DataFrame helpers.

Every builder accepts frames that may be empty or miss optional columns and
returns a ``go.Figure`` (an annotated empty figure when there is nothing to draw).
"""
from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from typing import Any

import networkx as nx
import numpy as np
import pandas as pd
import plotly.graph_objects as go

from screening import injection as injection_text
from screening import schema as S
from ui.theme import (CLEAR_GREEN, GOLD, GOLD_DARK, GREY, GREY_LIGHT, RED, RED_DARK, RISK_COLORS,
                      SLATE, SLATE_2)

SHORTLIST_STATUSES = (S.STATUS_SHORTLISTED, S.STATUS_REVIEW)
SUB_LABELS: dict[str, str] = {
    S.SUB_HR: "HackerRank", S.SUB_SKILLS: "Skills", S.SUB_EXPERIENCE: "Experience", S.SUB_EDUCATION: "Education",
}
CLUSTER_COLORS = [RED, GOLD_DARK, SLATE_2, "#5B6770", RED_DARK, "#E8730C", "#00796B", "#3F51B5",
                  "#7B1FA2", "#6D4C41", GOLD, CLEAR_GREEN]
_FONT = "Inter, system-ui, -apple-system, Segoe UI, Roboto, sans-serif"


# ---------------------------------------------------------------------------
# Tolerant frame helpers (the engine may omit optional columns)
# ---------------------------------------------------------------------------
def col(df: pd.DataFrame, name: str, default: Any = np.nan) -> pd.Series:
    """``df[name]`` when present, else a constant Series aligned to ``df``."""
    if name in df.columns:
        return df[name]
    return pd.Series([default] * len(df), index=df.index)


def num(df: pd.DataFrame, name: str) -> pd.Series:
    """Numeric view of a column (NaN where missing / unparseable)."""
    return pd.to_numeric(col(df, name), errors="coerce")


def txt(df: pd.DataFrame, name: str) -> pd.Series:
    """String view of a column with missing values as ''."""
    return col(df, name, "").fillna("").astype(str)


def flag(df: pd.DataFrame, name: str) -> pd.Series:
    """Boolean view of a column (missing -> False)."""
    s = col(df, name, False)
    if s.dtype == bool:
        return s
    return s.map(lambda v: str(v).strip().lower() in {"true", "1", "yes", "y"} if pd.notna(v) else False).astype(bool)


def split_list(value: Any, sep: str = S.SEP_LIST) -> list[str]:
    """Split a joined list cell ('a, b' / 'a | b' / 'a; b') into clean items."""
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    if value is None or (isinstance(value, float) and math.isnan(value)) or value is pd.NA:
        return []
    return [p.strip() for p in str(value).split(sep.strip() or sep) if p.strip()]


def shortlist_cutoff(df: pd.DataFrame, method: str, threshold: float) -> float | None:
    """Final score at the shortlist boundary (lowest shortlisted score, or the threshold)."""
    if method == "threshold":
        return float(threshold)
    scores = num(df, S.FINAL_SCORE)[txt(df, S.STATUS).isin(SHORTLIST_STATUSES)].dropna()
    return float(scores.min()) if len(scores) else None


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------
def _layout(fig: go.Figure, *, height: int = 360, title: str | None = None, legend: bool = True) -> go.Figure:
    fig.update_layout(
        template="plotly_white", height=height, showlegend=legend,
        margin=dict(l=10, r=10, t=44 if title else 16, b=10),
        font=dict(family=_FONT, size=12, color=SLATE_2),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="#FFFFFF",
        legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="left", x=0, font=dict(size=11)),
        hoverlabel=dict(bgcolor="#FFFFFF", font=dict(family=_FONT, size=12, color=SLATE)),
    )
    if title:
        fig.update_layout(title=dict(text=title, x=0, xanchor="left", font=dict(size=14, color=SLATE)))
    return fig


def empty_figure(message: str, height: int = 260) -> go.Figure:
    """Blank figure carrying a centred explanatory message."""
    fig = go.Figure()
    fig.add_annotation(text=message, x=0.5, y=0.5, xref="paper", yref="paper", showarrow=False,
                       font=dict(size=13, color=GREY))
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    return _layout(fig, height=height, legend=False)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
def score_histogram(df: pd.DataFrame, cutoff: float | None = None, bin_size: float = 2.5) -> go.Figure:
    """Final-score distribution split shortlisted / not, with the cutoff line."""
    scores = num(df, S.FINAL_SCORE)
    if scores.notna().sum() == 0:
        return empty_figure("No scores to plot yet.")
    short = txt(df, S.STATUS).isin(SHORTLIST_STATUSES)
    fig = go.Figure()
    bins = dict(start=0, end=100 + bin_size, size=bin_size)
    for name, mask, color in (("Shortlisted", short, RED), ("Not shortlisted", ~short, GREY_LIGHT)):
        vals = scores[mask].dropna()
        if len(vals):
            fig.add_trace(go.Histogram(
                x=vals, name=f"{name} ({len(vals):,})", xbins=bins,
                marker=dict(color=color, line=dict(color="#FFFFFF", width=0.6)),
                hovertemplate="Score %{x}<br>%{y} candidates<extra></extra>"))
    fig.update_layout(barmode="stack", bargap=0.04)
    if cutoff is not None and math.isfinite(cutoff):
        fig.add_vline(x=cutoff, line=dict(color=GOLD_DARK, width=3, dash="dash"),
                      annotation_text=f"Cutoff {cutoff:.1f}", annotation_position="top left",
                      annotation_font=dict(color=SLATE, size=12))
    fig.update_xaxes(title="Final score (0-100)", range=[0, 101])
    fig.update_yaxes(title="Candidates")
    return _layout(fig, height=320)


def lift_bar(commonality: pd.DataFrame | None, top: int = 15) -> go.Figure:
    """Horizontal bars of lift for significant commonality rows."""
    if commonality is None or commonality.empty or "lift" not in commonality.columns:
        return empty_figure("No commonality analysis available.")
    df = commonality
    if "significant" in df.columns:
        df = df[flag(df, "significant")]
    df = df.assign(_lift=num(df, "lift")).dropna(subset=["_lift"])
    if df.empty:
        return empty_figure("No statistically significant commonalities among perfect scorers.")
    df = df.sort_values("_lift", ascending=False).head(top).iloc[::-1]
    labels = [f"{S.COMMONALITY_ATTRIBUTES.get(a, a)}: {v}" for a, v in zip(txt(df, "attribute"), txt(df, "value"))]
    custom = np.column_stack([num(df, "focus_count"), num(df, "focus_total"), num(df, "focus_share") * 100,
                              num(df, "population_share") * 100, num(df, "q_value")])
    fig = go.Figure(go.Bar(
        x=df["_lift"], y=labels, orientation="h", marker=dict(color=RED),
        text=[f"{v:.1f}x" for v in df["_lift"]], textposition="outside", cliponaxis=False, customdata=custom,
        hovertemplate=("<b>%{y}</b><br>Lift %{x:.2f}x<br>Perfect scorers: %{customdata[0]:.0f}/"
                       "%{customdata[1]:.0f} (%{customdata[2]:.1f}%)<br>All candidates: %{customdata[3]:.1f}%"
                       "<br>q-value %{customdata[4]:.3g}<extra></extra>")))
    fig.add_vline(x=1, line=dict(color=GREY, dash="dot"))
    fig.update_xaxes(title="Lift vs all candidates (x)", range=[0, float(df["_lift"].max()) * 1.3])
    return _layout(fig, height=max(220, 34 * len(df) + 90), legend=False)


def _group_colors(groups: list[str], color_by: str) -> dict[str, str]:
    if color_by == "risk":
        return {g: RISK_COLORS.get(g, GREY) for g in groups}
    named = [g for g in groups if g != "Unclustered"]
    out = {g: CLUSTER_COLORS[i % len(CLUSTER_COLORS)] for i, g in enumerate(sorted(named))}
    out["Unclustered"] = GREY
    return out


def risk_network(pairs: pd.DataFrame | None, nodes: pd.DataFrame | None, *, color_by: str = "risk",
                 min_score: float = 0.0) -> go.Figure:
    """Spring-layout graph of risk pairs. color_by: 'risk' (level) or 'cluster'."""
    if pairs is None or pairs.empty or not {"cand_a", "cand_b"} <= set(pairs.columns):
        return empty_figure("No linked candidate pairs to draw.", height=420)
    p = pairs.assign(_score=num(pairs, "pair_score").fillna(0.0))
    p = p[p["_score"] >= min_score]
    graph = nx.Graph()
    for a, b, score, sig in zip(txt(p, "cand_a"), txt(p, "cand_b"), p["_score"], txt(p, "signals")):
        if a and b and a != b and (not graph.has_edge(a, b) or graph[a][b]["score"] < score):
            graph.add_edge(a, b, score=float(score), weight=max(float(score), 1.0) / 100.0, signals=sig)
    if graph.number_of_nodes() == 0:
        return empty_figure("No pairs at or above the selected pair score.", height=420)
    # Unweighted with a wider optimal distance: dense clusters stay readable instead of collapsing.
    pos = nx.spring_layout(graph, seed=7, k=2.0 / math.sqrt(graph.number_of_nodes()), weight=None)
    fig = go.Figure()
    for lo, hi, width in ((0, 30, 0.8), (30, 50, 1.8), (50, 75, 3.2), (75, math.inf, 5.0)):
        xs: list[float | None] = []
        ys: list[float | None] = []
        for a, b, d in graph.edges(data=True):
            if lo <= d["score"] < hi:
                xs += [pos[a][0], pos[b][0], None]
                ys += [pos[a][1], pos[b][1], None]
        if xs:
            fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", hoverinfo="skip", showlegend=False,
                                     line=dict(width=width, color="rgba(51,51,51,0.35)")))
    mids = [((pos[a][0] + pos[b][0]) / 2, (pos[a][1] + pos[b][1]) / 2,
             f"<b>{a} ↔ {b}</b><br>Pair score {d['score']:.0f}<br>" + "<br>".join(split_list(d["signals"], S.SEP_REASONS)[:6]))
            for a, b, d in graph.edges(data=True)]
    fig.add_trace(go.Scatter(x=[m[0] for m in mids], y=[m[1] for m in mids], mode="markers", showlegend=False,
                             marker=dict(size=10, color="rgba(0,0,0,0)"), hovertext=[m[2] for m in mids],
                             hoverinfo="text"))
    info = (nodes.drop_duplicates(S.CANDIDATE_ID).set_index(S.CANDIDATE_ID)
            if nodes is not None and S.CANDIDATE_ID in nodes.columns else pd.DataFrame())

    def attr(cid: str, name: str, default: Any = "") -> Any:
        if cid in info.index and name in info.columns:
            v = info.at[cid, name]
            return default if (v is None or (isinstance(v, float) and math.isnan(v))) else v
        return default

    ids = list(graph.nodes())
    if color_by == "risk":
        keys = {c: str(attr(c, S.RISK_LEVEL, S.RISK_CLEAR) or S.RISK_CLEAR) for c in ids}
        order = [g for g in S.RISK_ORDER if g in keys.values()] + sorted(set(keys.values()) - set(S.RISK_ORDER))
    else:
        keys = {c: str(attr(c, S.CLUSTER_ID, "") or "Unclustered") for c in ids}
        order = sorted(set(keys.values()) - {"Unclustered"}) + (["Unclustered"] if "Unclustered" in keys.values() else [])
    colors = _group_colors(order, color_by)
    degree = dict(graph.degree())
    show_text = len(ids) <= 40
    for group in order:
        members = [c for c in ids if keys[c] == group]
        hover = [
            f"<b>{c}</b> · {attr(c, S.NAME)}<br>{attr(c, S.COLLEGE)}<br>Risk: {attr(c, S.RISK_LEVEL, S.RISK_CLEAR)}"
            f" ({float(attr(c, S.RISK_SCORE, 0) or 0):.0f}) · Cluster: {attr(c, S.CLUSTER_ID) or '-'}"
            f"<br>{degree[c]} link(s)<br>" + "<br>".join(split_list(attr(c, S.RISK_REASONS), S.SEP_REASONS)[:3])
            for c in members
        ]
        fig.add_trace(go.Scatter(
            x=[pos[c][0] for c in members], y=[pos[c][1] for c in members], name=f"{group} ({len(members)})",
            mode="markers+text" if show_text else "markers", text=members if show_text else None,
            textposition="top center", textfont=dict(size=9, color=SLATE_2), hovertext=hover, hoverinfo="text",
            marker=dict(size=[12 + 4 * min(degree[c], 8) for c in members], color=colors.get(group, GREY),
                        line=dict(color="#FFFFFF", width=1.5), opacity=0.95)))
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    return _layout(fig, height=540)


def timeline_scatter(df: pd.DataFrame, focus_ids: Iterable[str] | None = None) -> go.Figure:
    """Submission time vs completion minutes; focus group highlighted, clusters coloured."""
    try:
        x = pd.to_datetime(col(df, S.SUBMISSION_TIME), errors="coerce", format="mixed")
        x_title = "Submission time"
        if x.notna().sum() == 0:
            x = pd.to_datetime(col(df, S.TEST_START), errors="coerce", format="mixed")
            x_title = "Test start time"
    except (TypeError, ValueError):
        return empty_figure("Timing columns could not be read.")
    y = num(df, S.COMPLETION_MIN)
    ok = x.notna() & y.notna()
    if not ok.any():
        return empty_figure("No timing data (submission time and completion minutes) in this feed.")
    ids = txt(df, S.CANDIDATE_ID)
    focus = set(focus_ids) if focus_ids else set(ids[num(df, S.HR_PCT) >= 100])
    cluster = txt(df, S.CLUSTER_ID)
    custom = pd.DataFrame({"id": ids, "name": txt(df, S.NAME), "college": txt(df, S.COLLEGE),
                           "pct": num(df, S.HR_PCT).fillna(-1)}).to_numpy(dtype=object)
    groups: list[tuple[str, pd.Series, dict[str, Any]]] = [
        ("Other candidates", ok & ~ids.isin(focus) & (cluster == ""), dict(size=6, color=GREY_LIGHT, opacity=0.6)),
        ("Perfect scorers", ok & ids.isin(focus) & (cluster == ""),
         dict(size=9, color=GOLD, opacity=0.95, line=dict(color=SLATE_2, width=0.8))),
    ]
    for i, cid in enumerate(sorted(set(cluster[ok & (cluster != "")]))):
        groups.append((f"Cluster {cid}", ok & (cluster == cid),
                       dict(size=12, color=CLUSTER_COLORS[i % len(CLUSTER_COLORS)], opacity=0.95,
                            symbol="diamond", line=dict(color="#FFFFFF", width=1))))
    fig = go.Figure()
    for name, mask, marker in groups:
        if mask.any():
            fig.add_trace(go.Scatter(
                x=x[mask], y=y[mask], mode="markers", name=f"{name} ({int(mask.sum())})", marker=marker,
                customdata=custom[mask.to_numpy()],
                hovertemplate=("<b>%{customdata[0]}</b> · %{customdata[1]}<br>%{customdata[2]}<br>"
                               "HackerRank %{customdata[3]:.0f}%<br>%{x}<br>%{y:.1f} min<extra></extra>")))
    fig.update_xaxes(title=x_title)
    fig.update_yaxes(title="Completion (minutes)")
    return _layout(fig, height=420)


def radar_subscores(candidate: Mapping[str, Any], baseline: Mapping[str, Any] | None = None,
                    baseline_name: str = "Shortlist average") -> go.Figure:
    """Radar of the four 0-1 sub-scores, optionally against a baseline."""
    cats = list(SUB_LABELS.values())

    def values(d: Mapping[str, Any]) -> list[float]:
        out = []
        for k in SUB_LABELS:
            try:
                v = float(d.get(k))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                v = 0.0
            out.append(0.0 if math.isnan(v) else min(1.0, max(0.0, v)))
        return out + out[:1]

    fig = go.Figure()
    if baseline:
        fig.add_trace(go.Scatterpolar(r=values(baseline), theta=cats + cats[:1], name=baseline_name,
                                      line=dict(color=GOLD_DARK, width=2, dash="dash")))
    fig.add_trace(go.Scatterpolar(r=values(candidate), theta=cats + cats[:1], name="Candidate", fill="toself",
                                  fillcolor="rgba(215,30,40,0.22)", line=dict(color=RED, width=2.5)))
    fig.update_layout(polar=dict(bgcolor="#FFFFFF",
                                 radialaxis=dict(range=[0, 1], tickformat=".0%", angle=45, tickangle=45,
                                                 gridcolor="#E5E5E5", tickfont=dict(size=9, color=GREY)),
                                 angularaxis=dict(gridcolor="#E5E5E5", rotation=90, direction="clockwise")))
    fig = _layout(fig, height=360)
    fig.update_layout(legend=dict(yanchor="top", y=-0.06), margin=dict(t=30, b=40))
    return fig


def points_breakdown(earned: Mapping[str, float], available: Mapping[str, float]) -> go.Figure:
    """Earned vs available rubric points per component (horizontal overlay bars)."""
    labels = list(earned)
    if not labels:
        return empty_figure("No score breakdown available.")
    got = [0.0 if pd.isna(earned[k]) else float(earned[k]) for k in labels]
    cap = [float(available.get(k, 0.0)) for k in labels]
    fig = go.Figure([
        go.Bar(y=labels, x=cap, orientation="h", name="Available", marker=dict(color="#EFEFEF"), hoverinfo="skip"),
        go.Bar(y=labels, x=got, orientation="h", name="Earned", marker=dict(color=RED),
               text=[f"{g:.1f} / {c:.0f}" for g, c in zip(got, cap)], textposition="outside", cliponaxis=False,
               hovertemplate="%{y}: %{x:.1f} pts<extra></extra>"),
    ])
    fig.update_layout(barmode="overlay", bargap=0.35)
    fig.update_yaxes(autorange="reversed")
    fig.update_xaxes(title="Rubric points", range=[0, max(cap + got + [1.0]) * 1.18])
    return _layout(fig, height=260)


# ---------------------------------------------------------------------------
# Integrity helpers shared by the Risk, Deep-Dive and Compare tabs (tolerant, no Streamlit)
# ---------------------------------------------------------------------------
TEAM_SIGNAL = "team_github_repo"
TEAM_PAIR_LABEL = "Team project (likely legitimate)"
SIGNAL_PAIR_LABEL = "Integrity signal"
INJECTION_LABEL = "Hidden or adversarial text in resume"
INJECTION_KIND_LABELS: dict[str, str] = {
    "instruction": "Instruction aimed at screeners or AI tools",
    "hidden_text": "Hidden text",
    "invisible_chars": "Invisible characters",
}
_INJECTION_PRIORITY = {"instruction": 0, "hidden_text": 1, "invisible_chars": 2}
_WORD_RE = re.compile(r"[a-z]+")
_BUNDLE_RE = re.compile(r"^(?P<file>.+?)\s+p\.\s*(?P<first>\d+)(?:\s*-\s*(?P<last>\d+))?\s*$")


def team_project_mask(pairs: pd.DataFrame | None) -> pd.Series:
    """True for pairs linked only by a shared team-project GitHub repo (signal_keys ==
    "team_github_repo"): teammates listing the same repo on otherwise different resumes."""
    if pairs is None or pairs.empty:
        return pd.Series([], dtype=bool)
    keys = txt(pairs, "signal_keys").map(lambda v: set(split_list(v)))
    return keys.map(lambda k: bool(k) and k <= {TEAM_SIGNAL}).astype(bool)


def pair_types(pairs: pd.DataFrame | None) -> pd.Series:
    """'Team project (likely legitimate)' or 'Integrity signal' per pair row."""
    mask = team_project_mask(pairs)
    return mask.map({True: TEAM_PAIR_LABEL, False: SIGNAL_PAIR_LABEL}).astype(object)


def integrity_concern(value: Any) -> bool:
    """Assessment integrity status that needs review: Workday Medium / High or HackerRank
    Review / Suspicious / Flagged (schema.INTEGRITY_CONCERN_VALUES); "Not suspicious" is not."""
    if value is None or (isinstance(value, float) and math.isnan(value)) or value is pd.NA:
        return False
    low = str(value).casefold().strip()
    if not low or low.startswith(("not ", "no ", "non-", "non ")):
        return False
    return any(word in S.INTEGRITY_CONCERN_VALUES for word in _WORD_RE.findall(low))


def injection_flagged(df: pd.DataFrame) -> pd.Series:
    """True where the row carries hidden / adversarial resume text (INJECTION_FLAGS)."""
    return txt(df, S.INJECTION_FLAGS).str.strip() != ""


def injection_findings(parsed: Any) -> list[dict[str, str]]:
    """ParsedResume.injection_findings as clean {kind, snippet, detail} dicts, most serious first
    ([] for resumes parsed before the field existed)."""
    out = []
    for f in getattr(parsed, "injection_findings", None) or []:
        if isinstance(f, Mapping):
            out.append({"kind": str(f.get("kind") or "").strip(),
                        "snippet": " ".join(str(f.get("snippet") or "").split()),
                        "detail": " ".join(str(f.get("detail") or "").split())})
    return sorted(out, key=lambda f: _INJECTION_PRIORITY.get(f["kind"], len(_INJECTION_PRIORITY)))


def injection_summaries(flags: Any, parsed: Any = None, width: int = 80) -> list[str]:
    """One line per finding: the parsed resume's findings (injection.finding_summary) when
    available, else the " | "-joined INJECTION_FLAGS cell."""
    findings = injection_findings(parsed)
    if findings:
        try:
            return [injection_text.finding_summary(f, width=width) for f in findings]
        except Exception:  # noqa: BLE001 - wording helper must never break a page
            return [f"{INJECTION_KIND_LABELS.get(f['kind'], f['kind'])}: {f['snippet']}" for f in findings]
    return split_list(flags, S.SEP_REASONS)


def injection_snippet(flags: Any, parsed: Any = None, width: int = 60) -> str:
    """Short snippet of the most serious finding (injection.reason_snippet), else the first flag."""
    findings = injection_findings(parsed)
    if findings:
        try:
            return injection_text.reason_snippet(findings, width=width)
        except Exception:  # noqa: BLE001
            return findings[0]["snippet"][:width]
    parts = split_list(flags, S.SEP_REASONS)
    return parts[0] if parts else ""


def bundle_text(bundle_source: Any) -> str:
    """'<packet> p.3-4' -> 'From packet <packet>, pages 3-4' ('' when not split from a packet)."""
    if bundle_source is None or (isinstance(bundle_source, float) and math.isnan(bundle_source)):
        return ""
    text = " ".join(str(bundle_source).split())
    if not text:
        return ""
    match = _BUNDLE_RE.match(text)
    if not match:
        return f"From packet {text}"
    first, last = match.group("first"), match.group("last")
    pages = f"pages {first}-{last}" if last and last != first else f"page {first}"
    return f"From packet {match.group('file')}, {pages}"
