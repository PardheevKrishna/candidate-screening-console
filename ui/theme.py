"""Brand styling: CSS, header banner, KPI cards, badges and chips.

Everything here is presentational. Any text that may come from candidate data
(names, skills, resume text) is HTML-escaped before it reaches ``st.html``.
"""
from __future__ import annotations

import html
import os
import re
from dataclasses import dataclass

import streamlit as st

from screening import schema as S

# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------
RED = "#D71E28"
RED_DARK = "#BA0C2F"
GOLD = "#FFCD00"
GOLD_DARK = "#F1B521"
SLATE = "#1A1A1A"
SLATE_2 = "#333333"
BACKGROUND = "#F9F9F9"
GREY = "#8A8A8A"
GREY_LIGHT = "#D9D9D9"
CLEAR_GREEN = "#4C8C4A"
ORANGE = "#E8730C"

BRAND_EYEBROW = os.environ.get("APP_BRAND_EYEBROW", "WELLS FARGO · TALENT ACQUISITION")
APP_TITLE = "Candidate Screening & Integrity Console"
PAGE_ICON = ":material/verified_user:"

RISK_COLORS: dict[str, str] = {
    S.RISK_HIGH: RED_DARK, S.RISK_MEDIUM: ORANGE, S.RISK_LOW: GOLD_DARK, S.RISK_CLEAR: CLEAR_GREEN,
}
RISK_EMOJI: dict[str, str] = {S.RISK_HIGH: "🔴", S.RISK_MEDIUM: "🟠", S.RISK_LOW: "🟡", S.RISK_CLEAR: "🟢"}
# st.badge colour + material icon per status / risk level
STATUS_BADGE: dict[str, tuple[str, str]] = {
    S.STATUS_SHORTLISTED: ("green", ":material/check_circle:"),
    S.STATUS_REVIEW: ("orange", ":material/flag:"),
    S.STATUS_HOLD: ("red", ":material/pause_circle:"),
    S.STATUS_NOT: ("gray", ":material/remove_circle_outline:"),
    S.STATUS_INELIGIBLE: ("gray", ":material/block:"),
}
RISK_BADGE: dict[str, tuple[str, str]] = {
    S.RISK_HIGH: ("red", ":material/gpp_bad:"),
    S.RISK_MEDIUM: ("orange", ":material/gpp_maybe:"),
    S.RISK_LOW: ("yellow", ":material/shield:"),
    S.RISK_CLEAR: ("green", ":material/verified_user:"),
}

_FONT_STACK = "'Inter', system-ui, -apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif"

_CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
.stApp, .stApp p, .stApp li, .stApp label, .stApp input, .stApp textarea, .stApp button,
.stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp h5 {{ font-family: {_FONT_STACK}; }}
.stApp {{ background: {BACKGROUND}; }}
.block-container {{ padding-top: 4.2rem; padding-bottom: 3rem; max-width: 1500px; }}
[data-testid="stHeader"] {{ background: rgba(249,249,249,0.85); backdrop-filter: blur(4px); }}
.stApp h3 {{ font-weight: 650; letter-spacing: -0.01em; }}
[data-testid="stSidebar"] {{ border-right: 1px solid #ECECEC; }}
.wf-banner {{
  background: linear-gradient(115deg, {RED_DARK} 0%, {RED} 58%, #E4404A 100%);
  color: #FFFFFF; padding: 20px 28px 16px; border-radius: 10px;
  border-bottom: 5px solid {GOLD}; box-shadow: 0 3px 12px rgba(0,0,0,.10); margin-bottom: 14px;
}}
.wf-eyebrow {{ color: {GOLD}; font-size: .72rem; font-weight: 700; letter-spacing: .16em; }}
.wf-title {{ font-size: 1.7rem; font-weight: 700; line-height: 1.2; margin-top: 4px; }}
.wf-subtitle {{ font-size: .92rem; opacity: .93; margin-top: 6px; }}
.wf-kpis {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 12px; margin: 4px 0 10px; }}
@media (max-width: 1100px) {{ .wf-kpis {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} }}
.wf-kpi {{
  background: #FFFFFF; border: 1px solid #E7E7E7; border-left: 4px solid {RED};
  border-radius: 8px; padding: 12px 16px; box-shadow: 0 1px 3px rgba(0,0,0,.04);
}}
.wf-kpi.gold {{ border-left-color: {GOLD_DARK}; }}
.wf-kpi.slate {{ border-left-color: {SLATE_2}; }}
.wf-kpi-label {{ color: #5F5F5F; font-size: .74rem; font-weight: 600; letter-spacing: .05em; text-transform: uppercase; }}
.wf-kpi-value {{ color: {SLATE}; font-size: 1.65rem; font-weight: 700; line-height: 1.25; margin-top: 2px; }}
.wf-kpi-sub {{ color: #6B6B6B; font-size: .78rem; margin-top: 2px; }}
.wf-card {{ background: #FFFFFF; border: 1px solid #E7E7E7; border-radius: 10px; padding: 18px 22px; }}
.wf-callout {{
  background: #FFF8DB; border: 1px solid {GOLD_DARK}; border-left: 5px solid {GOLD_DARK};
  border-radius: 8px; padding: 12px 16px; color: {SLATE_2}; font-size: .92rem;
}}
.wf-chip {{
  display: inline-block; padding: 2px 10px; margin: 2px 4px 2px 0; border-radius: 999px;
  font-size: .8rem; font-weight: 500; border: 1px solid transparent;
}}
.wf-chip.match {{ background: #FDECEC; color: {RED_DARK}; border-color: #F5C2C5; }}
.wf-chip.miss {{ background: #FFFFFF; color: #7A7A7A; border-color: #D5D5D5; text-decoration: line-through; }}
.wf-chip.neutral {{ background: #F1F1F1; color: {SLATE_2}; border-color: #E2E2E2; }}
.wf-chip.gold {{ background: #FFF4C2; color: #6B5200; border-color: {GOLD_DARK}; }}
.wf-muted {{ color: #6B6B6B; font-size: .85rem; }}
.wf-profile-name {{ font-size: 1.45rem; font-weight: 700; color: {SLATE}; line-height: 1.2; }}
.wf-profile-meta {{ color: #5F5F5F; font-size: .9rem; margin-top: 2px; }}
</style>
"""


@dataclass(frozen=True)
class Kpi:
    """One KPI card: label, headline value, optional sub-caption and accent."""

    label: str
    value: str
    sub: str = ""
    accent: str = ""  # "" (red) | "gold" | "slate"


def esc(value: object) -> str:
    """HTML-escape any value for safe inclusion in ``st.html``."""
    return html.escape("" if value is None else str(value), quote=True)


_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]<>()#+!|~$])")


def md_escape(value: object) -> str:
    """Escape Markdown/LaTeX control characters in data-derived text for ``st.markdown``."""
    return _MD_SPECIAL.sub(r"\\\1", "" if value is None else str(value))


_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\((?:https?|ftp|data|javascript):[^)]*\)", re.IGNORECASE)
_HTML_TAG = re.compile(r"<[^>]{1,200}>")


def safe_markdown(text: object) -> str:
    """LLM-written Markdown with images, external links and HTML tags neutralised.

    Model output can be steered by resume content, so nothing in it may make the
    browser fetch a URL (image beacons / link exfiltration).
    """
    out = _MD_IMAGE.sub(lambda m: m.group(1), "" if text is None else str(text))
    out = _MD_LINK.sub(lambda m: m.group(1), out)
    return _HTML_TAG.sub("", out).replace("$", r"\$")


def inject_css() -> None:
    """Inject the brand stylesheet (one call per script run)."""
    st.html(_CSS)


def render_header(subtitle: str = "") -> None:
    """Red gradient banner with gold bottom rule."""
    sub = f'<div class="wf-subtitle">{esc(subtitle)}</div>' if subtitle else ""
    st.html(
        f'<div class="wf-banner"><div class="wf-eyebrow">{esc(BRAND_EYEBROW)}</div>'
        f'<div class="wf-title">{esc(APP_TITLE)}</div>{sub}</div>'
    )


def render_kpis(kpis: list[Kpi]) -> None:
    """Row of KPI cards."""
    cards = "".join(
        f'<div class="wf-kpi {esc(k.accent)}"><div class="wf-kpi-label">{esc(k.label)}</div>'
        f'<div class="wf-kpi-value">{esc(k.value)}</div><div class="wf-kpi-sub">{esc(k.sub)}</div></div>'
        for k in kpis
    )
    st.html(f'<div class="wf-kpis">{cards}</div>')


def callout(text: str) -> None:
    """Gold-accented explanatory banner (plain text, escaped)."""
    st.html(f'<div class="wf-callout">{esc(text)}</div>')


def chips(items: list[str], variant: str = "neutral", empty: str = "None") -> None:
    """Render a list of short strings as pill chips."""
    if not items:
        st.html(f'<span class="wf-muted">{esc(empty)}</span>')
        return
    st.html("".join(f'<span class="wf-chip {esc(variant)}">{esc(i)}</span>' for i in items))


def status_badge(status: str) -> None:
    color, icon = STATUS_BADGE.get(status, ("gray", ":material/help:"))
    st.badge(status or "Unknown", color=color, icon=icon)  # type: ignore[arg-type]


def risk_badge(level: str) -> None:
    level = level or S.RISK_CLEAR
    color, icon = RISK_BADGE.get(level, ("gray", ":material/help:"))
    st.badge(f"{level} risk" if level != S.RISK_CLEAR else "No integrity flags",
             color=color, icon=icon)  # type: ignore[arg-type]


def risk_label(level: object) -> str:
    """'🔴 High' style label for tables; blanks are treated as Clear."""
    lvl = str(level) if isinstance(level, str) and level else S.RISK_CLEAR
    return f"{RISK_EMOJI.get(lvl, '⚪')} {lvl}"


def render_empty_state(sample_available: bool) -> bool:
    """Getting-started panel. Returns True when the sample run button is clicked."""
    st.html(
        '<div class="wf-card"><h3 style="margin:0 0 6px">Welcome — let\'s screen a hiring drive</h3>'
        '<div class="wf-muted">Fuse the TA / HackerRank export with bulk resumes, score every '
        "candidate on the weighted rubric, shortlist the top N and surface integrity patterns "
        "among perfect scorers.</div><ol style='margin:12px 0 0 18px;color:#333'>"
        "<li><b>Set up the rubric</b> in the <i>Config &amp; Rubric Tuning</i> tab next to this one: "
        "job description, required skills, weights, shortlist size and integrity policy. Defaults: "
        "HackerRank 40 / Skills 30 / Experience 20 / Education 10, top 300). You can also "
        "change them after the run; scores update in under a second.</li>"
        "<li><b>Choose a data source</b> in the sidebar: the synthetic sample, or upload a TA feed "
        "(CSV/XLSX) plus resumes (PDF/DOCX/TXT or a ZIP).</li>"
        "<li><b>Run screening</b> to parse, match, score and analyse.</li>"
        "<li><b>Review</b> the shortlist, integrity evidence and individual profiles, then export.</li>"
        "</ol></div>"
    )
    st.space("small")
    clicked = st.button("Load synthetic sample & run", type="primary", icon=":material/rocket_launch:",
                        disabled=not sample_available, key="empty_state_run")
    if not sample_available:
        st.caption('Sample data not found. Run `python generate_mock_data.py` from the project root, '
                   "or upload your own files from the sidebar.")
    return bool(clicked)
