"""Config & Rubric Tuning tab: edit the AppConfig, preview its impact, re-score, manage the college
tier list and the dataset cache, and import/export tuning profiles.

The LLM settings only shape the rationales the LLM writes (candidate explanations and summaries); the
LLM never changes scores, ranks, cutoffs or the shortlist.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import re
import zipfile
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from screening import analytics, export, ingest, pipeline, scoring, snapshots
from screening import llm_backends as backends
from screening import risk as risk_engine
from screening import schema as S
from screening import skills as skills_mod
from screening.config import DEFAULT_REQUIRED_SKILLS, PRESETS, TIE_BREAK_KEYS, AppConfig, RiskConfig
from screening.models import PipelineResult
from ui import state, theme
from ui.charts import num, split_list, txt

log = logging.getLogger(__name__)

K_PREVIEW = "_cfg_preview"            # {"config", "diff", "changes", "basis", "ineligible"}
K_TIER_UPLOAD = "_cfg_tier_upload"    # (digest of the uploaded tier list, TierList | error text)
K_TIER_PREVIEW = "_cfg_tier_preview"  # (cache key, tier_preview() result) for the dataset's colleges
PREVIEW_ROWS = 25                     # rows shown in "Biggest rank moves"
SKILL_MODES = {"per_skill": "Per skill: fixed points per matched skill (full marks at N skills)",
               "coverage": "Coverage (matched ÷ required)"}
METHODS = {"top_n": "Top N", "top_pct": "Top % of eligible", "threshold": "Score threshold"}
WEIGHT_FIELDS = (("hackerrank", "HackerRank"), ("skills", "Skills"), ("experience", "Experience"),
                 ("education", "Education"))
RESUME_POLICIES = {"auto": "Auto (recommended)", "zero": "Score Skills & Experience as 0",
                   "renormalize": "Rescale HackerRank + Education to 100"}
RESUME_POLICY_HELP = ("auto: renormalize to HackerRank + Education when the dataset has no resumes, else a missing "
                      "resume scores 0 for Skills/Experience")
POLICY_EFFECTS = {"zero": "a missing resume scores 0 for Skills and Experience",
                  "renormalize": "candidates without a resume are scored on HackerRank + Education, rescaled to 100"}
POLICIES = {False: "Keep & flag (default): flagged candidates stay shortlisted with evidence",
            True: "Hold & backfill: High-risk candidates move to Integrity Hold; slots are backfilled"}
CAP_NOTE = ("Limits the share of the shortlist a single college can take (0 = no cap). Candidates beyond the cap "
            "are not shortlisted and the next-best candidates fill the slots (Top N / Top %); with a score "
            "threshold they are simply excluded. Candidates with no college on record are never capped.")
TIE_BREAK_NOTE = "Candidate ID is always the final tie-break"
DEDUPE_LABEL = ("Auto-disqualify duplicate applications (keeps one per person: the scored application, then the "
                "institutional e-mail, then the lowest candidate ID)")
EXCLUDE_LABEL = "Exclude / disqualify candidate IDs"
MAX_LISTED_IDS = 20                   # IDs named in the "not in this dataset" warning
_ID_SPLIT_RE = re.compile(r"[\s,;]+")

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
TEAM_HELP = ("A GitHub repo shared by two candidates whose resumes are otherwise different (few shared repos, "
             "low text similarity, few shared rare bullets) is treated as a legitimate team project and "
             "weighted low.")
TEAM_PARAMS = [("team_max_shared_repos", "Team project: max shared repos", 0, 20, 1),
               ("team_max_text_similarity", "Team project: resume similarity below", 0.0, 1.0, 0.01),
               ("team_max_shared_bullets", "Team project: max shared rare bullets", 0, 20, 1),
               ("team_repo_weight", "Team project: repo signal weight", 0.0, 100.0, 1.0)]
TEAM_FIELD_HELP = {
    "team_max_shared_repos": f"{TEAM_HELP} A pair may share at most this many repos.",
    "team_max_text_similarity": f"{TEAM_HELP} Their resume text similarity must stay below this value.",
    "team_max_shared_bullets": f"{TEAM_HELP} They may share at most this many rare resume bullets.",
    "team_repo_weight": f"{TEAM_HELP} Such a repo adds this weight to the pair score instead of the full "
                        "shared-repo weight (the reason asks the recruiter to verify each candidate's role).",
}

LLM_TITLE = "LLM (GitHub Copilot via VS Code proxy)"
LLM_NOTE = ("The LLM writes rationales only - shortlist and borderline explanations, candidate explanations, "
            "risk-group and batch summaries. It never changes scores, ranks, cutoffs or the shortlist.")
PROXY_DEFAULT = "(proxy default)"
TOKEN_SOURCE_LABELS = {
    getattr(state, "TOKEN_ENV", "environment"): "environment variable",
    getattr(state, "TOKEN_SECRETS", "secrets"): "Streamlit secrets",
    getattr(state, "TOKEN_FILE", "connection file"): "VS Code connection file",
    "configured": "environment or secrets",
    "discovered": "VS Code connection file",
}
_REDACTED = "[redacted]"

TIER_NOTE = ("Tier scoring can disadvantage strong candidates from less-known colleges. Keep it off unless the "
             "hiring policy requires it; when off, the education points come from CGPA and degree only.")
UNMATCHED_NOTE = "unmatched colleges get no tier points; they are not penalised"
TIER_TYPES = ["csv", "xlsx"]
TIER_MAX_MB = 2
TIER_MAX_BYTES = TIER_MAX_MB * 1024 * 1024
TIER_MAX_ROWS = 5000
TIER_MAX_UNPACKED_BYTES = 64 * 1024 * 1024   # an .xlsx is a zip archive: refuse "zip bombs" before parsing
TIER_MAX_ZIP_MEMBERS = 1000
_ZIP_BOUNDED_METHODS = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)   # inflate in bounded steps
_READ_STEP = 1024 * 1024
TIER_PREVIEW_TOP = 50
TIER_COLUMNS = ["college", "candidates", "tier", "source"]
TIER_SOURCE_LABELS = {getattr(scoring, "TIER_FROM_FEED", "feed"): "Assessment feed",
                      getattr(scoring, "TIER_FROM_LIST", "tier list"): "Tier list", "": "No match"}
_COLLEGE_HEADERS = frozenset({"college", "collegename", "institution", "institutionname", "institute",
                              "university", "school"})
_TIER_HEADERS = frozenset({"tier", "collegetier", "tierlabel", "institutiontier"})
_ALIAS_HEADERS = frozenset({"aliases", "alias", "alternatenames", "othernames", "aka"})
_ZIP_MAGIC = b"PK\x03\x04"
PROFILE_MAX_MB = 5


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def clean_tie_breaks(keys: Any) -> list[str]:
    """Known tie-break keys in priority order (empty, unknown and repeated keys dropped)."""
    if isinstance(keys, str) or not isinstance(keys, (list, tuple)):
        keys = [keys] if isinstance(keys, str) else []
    out: list[str] = []
    for key in keys:
        name = str(key or "").strip()
        if name in TIE_BREAK_KEYS and name not in out:
            out.append(name)
    return out


def tie_break_label(key: str) -> str:
    return TIE_BREAK_KEYS.get(key, (str(key), True))[0]


def parse_candidate_ids(value: Any) -> list[str]:
    """Candidate IDs from text (comma, semicolon, space or newline separated) or a list:
    upper-case, de-duplicated, in the order given."""
    if isinstance(value, (list, tuple, set)):
        parts = [str(v) for v in value if v is not None]
    else:
        parts = _ID_SPLIT_RE.split(str(value or ""))
    cleaned = (p.strip().strip("'\"").strip().upper() for p in parts)
    return list(dict.fromkeys(p for p in cleaned if p))


def _cell(value: Any) -> str:
    if value is None or value is pd.NA or (isinstance(value, float) and math.isnan(value)):
        return ""
    return " ".join(str(value).split())


def _plain_key(value: Any) -> str:
    """Fallback for scoring.tier_list_key: case-folded words of letters and digits."""
    return " ".join(t for t in re.split(r"[^0-9a-z]+", _cell(value).casefold().replace("&", " and ")) if t)


def _natural(label: Any) -> tuple[float, str]:
    """Sort key for tier labels: 'Tier 2' after 'Tier 1', labels without a number last."""
    text = _cell(label)
    digits = re.search(r"\d+", text)
    return (float(digits.group()) if digits else math.inf, text.casefold())


@dataclass
class TierList:
    """A parsed tier-list upload: normalised college name / alias -> tier label."""

    mapping: dict[str, str]
    colleges: int = 0                                     # rows whose college name was added
    aliases: int = 0                                      # alias names added
    skipped: int = 0                                      # rows without a college or a tier
    conflicts: list[str] = field(default_factory=list)    # names listed again with another tier
    unscored: list[str] = field(default_factory=list)     # tier labels that give no points

    @property
    def tiers(self) -> list[str]:
        return sorted(set(self.mapping.values()), key=_natural)


def _safe_workbook(data: bytes) -> bytes:
    """The workbook repacked without compression, after inflating every part in bounded steps.

    openpyxl reads some parts with a single ZipFile.read call that trusts the sizes in the zip
    directory, so a crafted file could inflate to gigabytes ("zip bomb"). Here the directory is
    checked first (part count, total size, compression method); the parts are then inflated in
    1 MB steps - zipfile never returns more than a part's declared size when reading in steps,
    and a part whose real content differs fails its CRC check - and repacked uncompressed, so
    the reader only copies bytes. Raises ValueError with a message for the user.
    """
    too_big = ("The Excel workbook is too large once unpacked. Save the tier list as CSV and upload it "
               "again.")
    corrupt = "Could not open the Excel workbook (corrupt or unsupported format)."
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > TIER_MAX_ZIP_MEMBERS or sum(i.file_size for i in infos) > TIER_MAX_UNPACKED_BYTES:
                raise ValueError(too_big)
            if any(i.compress_type not in _ZIP_BOUNDED_METHODS for i in infos):
                raise ValueError(corrupt)
            out = io.BytesIO()
            unpacked = 0
            with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as copy:
                for info in infos:
                    if info.is_dir() or archive.getinfo(info.filename) is not info:
                        continue  # folders, and entries shadowed by a later duplicate name, are never read
                    chunks: list[bytes] = []
                    with archive.open(info) as part:
                        while chunk := part.read(_READ_STEP):
                            unpacked += len(chunk)
                            if unpacked > TIER_MAX_UNPACKED_BYTES:
                                raise ValueError(too_big)
                            chunks.append(chunk)
                    copy.writestr(info.filename, b"".join(chunks))
    except ValueError:
        raise
    except Exception:  # noqa: BLE001 - bad zip, CRC mismatch, encrypted or truncated parts
        raise ValueError(corrupt) from None
    return out.getvalue()


def _read_tier_table(name: str, data: bytes) -> pd.DataFrame:
    reader = state.engine_fn(ingest, "read_table")
    try:
        if reader is not None:
            frame = reader(name, data)
        elif data.startswith(_ZIP_MAGIC):
            frame = pd.read_excel(io.BytesIO(data), dtype=str)
        else:
            frame = pd.read_csv(io.BytesIO(data), dtype=str, encoding="utf-8-sig")
    except ValueError as exc:
        message = str(exc).replace("TA file", "tier list").strip()
        raise ValueError(message or "The tier list could not be read.") from None
    except Exception as exc:  # noqa: BLE001 - any parser failure is a bad upload
        raise ValueError(f"The tier list could not be read ({type(exc).__name__}).") from None
    if not isinstance(frame, pd.DataFrame):
        raise ValueError("The tier list could not be read.")
    return frame


def _tier_columns(frame: pd.DataFrame) -> tuple[Any, Any, Any]:
    """(college column, tier column, aliases column or None) of an uploaded tier list."""
    normalised = [(c, re.sub(r"[^a-z]", "", str(c).casefold())) for c in frame.columns]

    def find(names: frozenset[str]) -> Any:
        return next((c for c, n in normalised if n in names), None)

    college, tier, aliases = find(_COLLEGE_HEADERS), find(_TIER_HEADERS), find(_ALIAS_HEADERS)
    if college is None or tier is None:
        found = ", ".join(str(c) for c in list(frame.columns)[:8]) or "none"
        raise ValueError("The tier list needs a 'college' column and a 'tier' column (optional: 'aliases', "
                         f"separated by ';'). Columns found: {found}.")
    return college, tier, aliases


def parse_tier_list(file_name: str, data: bytes, tier_scores: dict[str, Any] | None = None) -> TierList:
    """Read an approved tier list (CSV or XLSX, <= 2 MB, <= 5,000 rows).

    Columns: college, tier and optional aliases separated by ';'. Every name and alias is
    normalised with scoring.tier_list_key and maps to the row's tier. A name listed again with
    another tier keeps the first tier (reported in ``conflicts``). Raises ValueError with a
    message for the user when the file cannot be used.
    """
    name = Path(str(file_name or "")).name or "tier_list.csv"
    if Path(name).suffix.lower() not in {f".{t}" for t in TIER_TYPES}:
        raise ValueError("Upload the tier list as a CSV (.csv) or Excel (.xlsx) file.")
    if not data:
        raise ValueError("The tier list is empty.")
    if len(data) > TIER_MAX_BYTES:
        raise ValueError(f"The tier list is {len(data) / 1048576:.1f} MB; the limit is {TIER_MAX_MB} MB.")
    if data.startswith(_ZIP_MAGIC):
        data = _safe_workbook(data)
    frame = _read_tier_table(name, data)
    college_col, tier_col, alias_col = _tier_columns(frame)
    if len(frame) > TIER_MAX_ROWS:
        raise ValueError(f"The tier list has {len(frame):,} rows; the limit is {TIER_MAX_ROWS:,}.")
    key_of = state.engine_fn(scoring, "tier_list_key") or _plain_key
    aliases = frame[alias_col].tolist() if alias_col is not None else [None] * len(frame)
    out = TierList(mapping={})
    for college_value, tier_value, alias_value in zip(frame[college_col].tolist(), frame[tier_col].tolist(), aliases):
        college, tier, alias_text = _cell(college_value), _cell(tier_value), _cell(alias_value)
        if not college or not tier:
            if college or tier or alias_text:
                out.skipped += 1
            continue
        names = [college, *(a for a in (_cell(x) for x in alias_text.split(";")) if a)]
        for position, raw in enumerate(names):
            key = str(key_of(raw) or "")
            if not key:
                continue
            known = out.mapping.get(key)
            if known is None:
                out.mapping[key] = tier
                if position == 0:
                    out.colleges += 1
                else:
                    out.aliases += 1
            elif known != tier and raw not in out.conflicts:
                out.conflicts.append(raw)
    if not out.mapping:
        raise ValueError("The tier list has no rows with both a college and a tier.")
    scored = {str(k).strip().casefold() for k in (tier_scores or {})}
    out.unscored = sorted({t for t in out.mapping.values() if t.casefold() not in scored and not re.search(r"\d", t)},
                          key=str.casefold)
    return out


def tier_list_frame(mapping: Any) -> pd.DataFrame:
    """The tier list in use as a two-column table (normalised names; aliases are rows of their own)."""
    items = mapping.items() if isinstance(mapping, dict) else []
    rows = sorted(((str(k), str(v)) for k, v in items if str(k).strip()), key=lambda kv: (_natural(kv[1]), kv[0]))
    return pd.DataFrame(rows, columns=["college", "tier"])


def tier_preview(frame: Any, cfg: AppConfig, top: int = TIER_PREVIEW_TOP) -> dict[str, Any]:
    """Colleges of a dataset, most candidates first, with the tier scoring would use.

    -> {"table": DataFrame(TIER_COLUMNS) of the ``top`` largest colleges, "colleges": distinct
    colleges, "unmatched": colleges without a tier}. Uses scoring.resolve_tier (the feed's own tier
    wins when it can be scored). Never raises for a single bad college.
    """
    empty = {"table": pd.DataFrame(columns=TIER_COLUMNS), "colleges": 0, "unmatched": 0}
    resolve = state.engine_fn(scoring, "resolve_tier")
    if resolve is None or not isinstance(frame, pd.DataFrame) or S.COLLEGE not in frame.columns:
        return empty
    data = pd.DataFrame({"college": txt(frame, S.COLLEGE).map(_cell), "feed": txt(frame, S.COLLEGE_TIER).map(_cell)})
    data = data[data["college"] != ""]
    if data.empty:
        return empty
    counts = data.groupby("college", sort=False).size()
    feed = data[data["feed"] != ""].groupby("college", sort=False)["feed"].first()
    rows = []
    for college, n in counts.items():
        try:
            label, source = resolve(college, cfg, feed.get(college) or None)
        except Exception:  # noqa: BLE001 - one odd college name must not hide the others
            label, source = None, ""
        rows.append({"college": str(college), "candidates": int(n), "tier": _cell(label), "source": str(source or "")})
    table = pd.DataFrame(rows, columns=TIER_COLUMNS).sort_values(["candidates", "college"], ascending=[False, True],
                                                                 kind="mergesort")
    return {"table": table.head(max(0, int(top))).reset_index(drop=True), "colleges": len(table),
            "unmatched": int((table["tier"] == "").sum())}


def ineligible_summary(before: Any, after: Any) -> dict[str, Any] | None:
    """Ineligible candidates now vs after a change, plus the rules whose counts move (None when
    either result has no status column)."""
    frames = (before, after)
    if not all(isinstance(f, pd.DataFrame) and S.STATUS in f.columns for f in frames):
        return None
    out: dict[str, Any] = {"before": int((txt(before, S.STATUS) == S.STATUS_INELIGIBLE).sum()),
                           "after": int((txt(after, S.STATUS) == S.STATUS_INELIGIBLE).sum()), "rules": []}
    counts = state.engine_fn(export, "ineligibility_counts")
    if counts is not None:
        try:
            was, now = dict(counts(before)), dict(counts(after))
            out["rules"] = [(rule, int(was.get(rule, 0)), int(now.get(rule, 0)))
                            for rule in dict.fromkeys([*was, *now]) if was.get(rule, 0) != now.get(rule, 0)]
        except Exception:  # noqa: BLE001 - the per-rule breakdown is optional
            out["rules"] = []
    return out


def model_options(cfg: AppConfig, status: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    """Model choices: '' (proxy default), the proxy's models, and the configured model even when
    the proxy does not list it. -> (options, display labels)."""
    models = list(dict.fromkeys(str(m).strip() for m in (status.get("models") or []) if str(m).strip()))
    current = str(cfg.llm.model or "").strip()
    options = ["", *[m for m in models if m]]
    labels = {"": PROXY_DEFAULT}
    if current and current not in options:
        options.append(current)
        labels[current] = f"{current} (not offered by the proxy)" if models else current
    return options, labels


def llm_status_line(status: dict[str, Any], cfg: AppConfig) -> str:
    """'Proxy `http://127.0.0.1:5757/v1` · reachable, 3 models · token: VS Code connection file'
    (the token itself is never part of it)."""
    base = str(status.get("base_url") or cfg.llm.base_url or "").replace("`", "") or "(not set)"
    models = [m for m in (status.get("models") or []) if str(m).strip()]
    if status.get("checked") is False:
        reach = "not checked while LLM features are off"
    elif status.get("reachable"):
        reach = f"reachable, {len(models)} model{'s' if len(models) != 1 else ''}" if models else "reachable, no models"
    else:
        reach = "not reachable"
    source = str(status.get("token_source") or "")
    label = TOKEN_SOURCE_LABELS.get(source, "") if source else "not found"
    if source == getattr(state, "TOKEN_ENV", "environment"):
        label = f"{label} {cfg.llm.api_key_env}".strip()
    return f"Proxy `{base}` · {reach} · token: {label or 'found'}"


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


def _short_value(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        text = ", ".join(str(v) for v in value) or "none"
    elif isinstance(value, float):
        text = f"{value:g}"
    else:
        text = str(value)
    return text if len(text) <= 60 else text[:59] + "…"


# ---------------------------------------------------------------------------
# Small UI helpers
# ---------------------------------------------------------------------------
def _skill_options(cfg: AppConfig, result: PipelineResult | None) -> list[str]:
    options = list(dict.fromkeys([*_required_skills(cfg), *DEFAULT_REQUIRED_SKILLS]))
    taxonomy = getattr(skills_mod, "__dict__", {}).get("SKILL_TAXONOMY")  # tolerant of a missing taxonomy
    if isinstance(taxonomy, dict):
        options += [str(k) for k in taxonomy]
    if result is not None and S.SKILLS_FOUND in result.candidates.columns:
        for cell in txt(result.candidates, S.SKILLS_FOUND).unique():
            options += split_list(cell)
    return list(dict.fromkeys(options))


def _required_skills(cfg: AppConfig) -> list[str]:
    return list(dict.fromkeys(str(s).strip() for s in _list(cfg.skills.required_skills) if str(s).strip()))


def _clamp(value: Any, lo: float, hi: float, cast: type = float) -> Any:
    """Coerce a (possibly user-uploaded) config value into a widget's valid range."""
    try:
        v = cast(value)
        if isinstance(v, float) and not math.isfinite(v):
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        v = cast(lo)
    return cast(min(hi, max(lo, v)))


def _num_grid(prefix: str, items: list[tuple[str, str, Any, Any, Any]], values: dict[str, Any],
              ncols: int = 3, helps: dict[str, str] | None = None) -> dict[str, Any]:
    """Grid of number inputs; integer fields are recognised by int bounds and step."""
    out: dict[str, Any] = {}
    cols = st.columns(ncols)
    for i, (name, label, lo, hi, step) in enumerate(items):
        cast = int if all(isinstance(x, int) for x in (lo, hi, step)) else float
        lo, hi, step = cast(lo), cast(hi), cast(step)
        value = _clamp(values.get(name, lo), lo, hi, cast)
        out[name] = cast(cols[i % ncols].number_input(label, lo, hi, value, step, key=f"{prefix}_{name}",
                                                       help=(helps or {}).get(name)))
    return out


def _named(label: Any, name: str) -> str:
    """Readable label for a weight key (keys from uploaded profiles are escaped)."""
    return str(label) if label else theme.md_escape(name.replace("_", " ").capitalize())


def _dataset_frame(result: PipelineResult | None, prepared: Any) -> pd.DataFrame | None:
    """The loaded dataset's candidate table (prepared data first), or None before a run."""
    for frame in (getattr(prepared, "base", None), getattr(result, "candidates", None)):
        if isinstance(frame, pd.DataFrame) and not frame.empty:
            return frame
    return None


def _dataset_mode(result: PipelineResult | None, prepared: Any) -> str | None:
    mode = getattr(prepared, "mode", None) or getattr(result, "mode", None)
    return str(mode) if mode else None


def _redact(text: Any, cfg: AppConfig) -> str:
    """Text for the UI with the proxy token (and anything token-like) removed."""
    out = str(text or "")
    try:
        token = state.llm_token(cfg)[0]
    except Exception:  # noqa: BLE001 - redaction must never fail
        token = None
    if token:
        out = out.replace(str(token), _REDACTED)
    for pattern in getattr(backends, "KEY_PATTERNS", ()):
        out = pattern.sub(_REDACTED, out)
    return out


def _llm_status(cfg: AppConfig) -> dict[str, Any]:
    """state.llm_status (cached 30 s); a failure becomes an 'unreachable' status. With LLM features
    switched off the proxy is not contacted at all."""
    if not cfg.llm.enabled:
        try:
            source = state.llm_token(cfg)[1]
        except Exception:  # noqa: BLE001
            source = ""
        return {"reachable": False, "models": [], "error": "", "base_url": cfg.llm.base_url, "token_source": source,
                "checked": False}
    try:
        status = state.llm_status(cfg)
    except Exception as exc:  # noqa: BLE001 - a status check must never break the page
        log.warning("LLM status check failed (%s)", type(exc).__name__)
        status = {"reachable": False, "models": [], "error": state.safe_error(exc), "base_url": cfg.llm.base_url,
                  "token_source": ""}
    return status if isinstance(status, dict) else {}


# ---------------------------------------------------------------------------
# Tab
# ---------------------------------------------------------------------------
def _problems(cfg: AppConfig) -> list[str]:
    try:
        return [str(p) for p in cfg.validate()]
    except Exception as exc:  # noqa: BLE001 - e.g. a hand-edited profile with text where numbers belong
        return [f"Some settings have values of the wrong type ({type(exc).__name__}). Apply the form below to "
                "correct them, or reset to defaults."]


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def render(result: PipelineResult | None) -> None:
    cfg, ver = state.get_config(), state.config_version()
    k = f"cfg{ver}"
    for problem in _problems(cfg):
        st.warning(theme.md_escape(problem), icon=":material/rule:")
    prepared = state.get_prepared()
    frame = _dataset_frame(result, prepared)
    status = _llm_status(cfg)
    can_preview = result is not None and prepared is not None
    _presets(k)
    with st.form(f"{k}_form", border=True):
        values = _rubric_section(cfg, k, _dataset_mode(result, prepared))
        values.update(_skills_section(cfg, k, result))
        values.update(_experience_section(cfg, k))
        values.update(_education_section(cfg, k))
        values.update(_shortlist_section(cfg, k, result, frame))
        values.update(_integrity_section(cfg, k))
        values.update(_llm_section(cfg, k, status))
        values.update(_snapshot_section(cfg, k))
        b1, b2, b3 = st.columns([1, 1, 2], vertical_alignment="center")
        submitted = b1.form_submit_button("Apply & re-score", type="primary", icon=":material/published_with_changes:",
                                          key="cfg_apply", width="stretch")
        previewed = b2.form_submit_button("Preview impact", icon=":material/difference:", key="cfg_preview",
                                          disabled=not can_preview, width="stretch",
                                          help="See who would enter or leave the shortlist before applying.")
        b3.caption("Preview shows how the shortlist would change; nothing is applied until you confirm."
                   if can_preview else "Preview impact is available after the first screening run.")

    if submitted or previewed:
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
    _tier_section(cfg, frame)
    _tools(cfg, status)


def _form_config(cfg: AppConfig, v: dict[str, Any]) -> AppConfig:
    """The AppConfig described by the submitted form values (shared by Apply and Preview)."""
    data = cfg.to_dict()
    data["weights"].update({name: float(value) for name, value in v["weights"].items()})
    data["skills"].update(required_skills=[str(s).strip() for s in v["required"] if str(s).strip()], mode=v["mode"],
                          skills_for_full_marks=int(v["full"]))
    data["experience"].update(v["exp"], job_description=v["jd"] or "",
                              internships_for_full=int(v["exp"]["internships_for_full"]),
                              projects_for_full=int(v["exp"]["projects_for_full"]))
    data["education"].update(v["edu"], tier_scores=v["tier_scores"], certs_for_full=int(v["edu"]["certs_for_full"]),
                             tier_enabled=bool(v["tier_on"]), tier_match_threshold=int(v["tier_threshold"]))
    data["shortlist"].update(method=v["method"], top_n=int(v["top_n"]), top_pct=float(v["top_pct"]),
                             threshold=float(v["threshold"]), min_hackerrank_pct=float(v["min_pct"]),
                             min_cgpa=float(v["min_cgpa"]), allowed_grad_years=sorted(int(y) for y in v["grad_years"]),
                             missing_resume_policy=str(v["no_resume"] or "auto"), exclude_high_risk=bool(v["hold"]),
                             review_risk_levels=list(v["review_levels"]), college_cap_pct=float(v["college_cap"]),
                             tie_break_order=clean_tie_breaks(v["tie_breaks"]),
                             exclude_candidates=parse_candidate_ids(v["exclude_ids"]),
                             auto_disqualify_duplicates=bool(v["dedupe"]))
    data["risk"].update(v["risk_nums"], **v["risk_ints"], **v["team"], compare_against_all=bool(v["compare_all"]),
                        pair_weights=v["pair_w"], individual_weights=v["ind_w"])
    data["llm"].update(enabled=bool(v["llm_on"]), model=str(v["model"] or "").strip(),
                       temperature=float(v["temperature"]), max_tokens=int(v["max_tokens"]),
                       timeout_sec=float(v["timeout"]), max_concurrency=int(v["concurrency"]),
                       max_input_chars=int(v["max_chars"]), borderline_band=int(v["borderline"]))
    data["snapshots"].update(enabled=bool(v["snap_on"]), ttl_days=int(v["snap_ttl"]))
    return AppConfig.from_dict(data)


# ---------------------------------------------------------------------------
# Form sections (each returns its submitted values)
# ---------------------------------------------------------------------------
def _use_preset(prefix: str, name: str) -> None:
    """Preset button callback: fill the four weight fields of the form (nothing is applied yet)."""
    spec = PRESETS.get(name) or {}
    weights = spec.get("weights") or {}
    for field_name, _ in WEIGHT_FIELDS:
        if field_name in weights:
            st.session_state[f"{prefix}_w_{field_name}"] = _clamp(weights[field_name], 0.0, 100.0)
    state.queue_message(f"Preset '{spec.get('label', name)}' loaded into the form - click Apply & re-score to use it.",
                        icon=":material/tune:")


def _presets(prefix: str) -> None:
    """One-click rubric presets above the weights (outside the form, so Enter never triggers them)."""
    if not PRESETS:
        return
    cols = st.columns([1.0] + [1.4] * len(PRESETS) + [2.4], vertical_alignment="center")
    cols[0].markdown("**Rubric presets**")
    for col, (name, spec) in zip(cols[1:], PRESETS.items()):
        weights = spec.get("weights") or {}
        split = " / ".join(f"{label} {float(weights.get(f, 0)):g}" for f, label in WEIGHT_FIELDS)
        col.button(str(spec.get("label") or name), key=f"cfg_preset_{name}", on_click=_use_preset, args=(prefix, name),
                   icon=":material/tune:", width="stretch", help=f"Fills in the rubric weights: {split}.")
    cols[-1].caption("A preset fills in the four rubric weights below; nothing changes until you click "
                     "**Apply & re-score**.")


def _rubric_section(cfg: AppConfig, k: str, mode: str | None) -> dict[str, Any]:
    st.markdown("##### Rubric weights (points)")
    wc = st.columns(4)
    weights: dict[str, float] = {}
    for i, (name, label) in enumerate(WEIGHT_FIELDS):
        key = f"{k}_w_{name}"
        if key not in st.session_state:  # seeded once; preset buttons overwrite it before the next run
            st.session_state[key] = _clamp(getattr(cfg.weights, name, 0.0), 0.0, 100.0)
        weights[name] = float(wc[i].number_input(label, min_value=0.0, max_value=100.0, step=1.0, key=key) or 0.0)
    try:
        applied = f"Applied weights sum to {float(cfg.weights.total()):.0f}. "
    except Exception:  # noqa: BLE001 - wrongly typed weights from a profile
        applied = ""
    st.caption(f"{applied}Weights are always rescaled to 100 when scoring, so only their ratios matter.")
    options = list(RESUME_POLICIES)
    current = str(cfg.shortlist.missing_resume_policy or "auto")
    p1, p2 = st.columns([1, 2], vertical_alignment="center")
    no_resume = p1.selectbox("Candidates without a resume", options,
                             index=options.index(current) if current in options else 0,
                             format_func=lambda p: RESUME_POLICIES.get(p, p), key=f"{k}_no_resume",
                             help=RESUME_POLICY_HELP)
    p2.caption(_policy_caption(cfg, mode))
    return {"weights": weights, "no_resume": no_resume}


def _policy_caption(cfg: AppConfig, mode: str | None) -> str:
    text = ("**With Resumes** suits a drive that collected CVs, **No Resumes** an assessment-only drive. The "
            "missing-resume policy decides how candidates without a resume are scored.")
    resolve = state.engine_fn(scoring, "resolve_missing_resume_policy")
    if not mode or resolve is None:
        return (f"{text} Auto rescales to HackerRank + Education when a dataset has no resumes at all; otherwise "
                "a missing resume scores 0 for Skills and Experience.")
    try:
        auto = replace(cfg, shortlist=replace(cfg.shortlist, missing_resume_policy="auto"))
        effective = str(resolve(auto, mode))
    except Exception:  # noqa: BLE001 - the hint is optional
        return text
    mode_label = state.MODE_LABELS.get(mode, mode)
    note = (f"{text} With the current dataset ({theme.md_escape(mode_label)}), auto means **{effective}**: "
            f"{POLICY_EFFECTS.get(effective, effective)}.")
    current = str(cfg.shortlist.missing_resume_policy or "auto")
    return note if current == "auto" else f"{note} The applied policy is **{theme.md_escape(current)}**."


def _skills_section(cfg: AppConfig, k: str, result: PipelineResult | None) -> dict[str, Any]:
    st.markdown("##### Skills")
    required = st.multiselect("Required skills", _skill_options(cfg, result), default=_required_skills(cfg),
                              accept_new_options=True, key=f"{k}_req_skills",
                              help="Type to add a skill outside the taxonomy (matched as a whole word).")
    sc1, sc2 = st.columns([2, 1])
    skill_mode = str(cfg.skills.mode)
    mode = sc1.radio("Skills scoring", list(SKILL_MODES), format_func=SKILL_MODES.get, key=f"{k}_skill_mode",
                     index=list(SKILL_MODES).index(skill_mode) if skill_mode in SKILL_MODES else 0)
    full = sc2.number_input("Skills for full marks", 1, 20, _clamp(cfg.skills.skills_for_full_marks, 1, 20, int), 1,
                            key=f"{k}_skills_full")
    return {"required": required, "mode": mode, "full": full}


def _experience_section(cfg: AppConfig, k: str) -> dict[str, Any]:
    st.markdown("##### Experience")
    jd = st.text_area("Job description (drives project/experience relevance)", cfg.experience.job_description,
                      height=180, key=f"{k}_jd")
    exp = _num_grid(f"{k}_exp", [("w_internships", "Weight: internships", 0.0, 1.0, 0.05),
                                 ("w_duration", "Weight: duration", 0.0, 1.0, 0.05),
                                 ("w_projects", "Weight: projects", 0.0, 1.0, 0.05),
                                 ("w_relevance", "Weight: JD relevance", 0.0, 1.0, 0.05),
                                 ("internships_for_full", "Internships for full marks", 1, 10, 1),
                                 ("months_for_full", "Months for full marks", 1.0, 48.0, 1.0),
                                 ("projects_for_full", "Projects for full marks", 1, 10, 1)], vars(cfg.experience),
                    ncols=4)
    return {"jd": jd, "exp": exp}


def _education_section(cfg: AppConfig, k: str) -> dict[str, Any]:
    st.markdown("##### Education")
    ed = cfg.education
    edu = _num_grid(f"{k}_edu", [("w_degree", "Weight: degree relevance", 0.0, 1.0, 0.05),
                                 ("w_cgpa", "Weight: CGPA", 0.0, 1.0, 0.05),
                                 ("w_certifications", "Weight: certifications", 0.0, 1.0, 0.05),
                                 ("w_tier", "Weight: institution tier", 0.0, 1.0, 0.05),
                                 ("cgpa_floor", "CGPA floor (scores 0)", 0.0, 10.0, 0.1),
                                 ("cgpa_ceiling", "CGPA ceiling (scores 1)", 0.0, 10.0, 0.1),
                                 ("certs_for_full", "Certifications for full marks", 1, 10, 1)], vars(ed), ncols=4)
    # Keys deliberately avoid the f"{k}_tier_<label>" pattern of the tier score inputs below.
    t1, t2 = st.columns([1, 2], vertical_alignment="center")
    tier_on = t1.toggle("Score college tier", value=bool(ed.tier_enabled), key=f"{k}_college_tier_on",
                        help="Off by default. Uses the tier in the assessment feed when present, else the approved "
                             "tier list (College tier list, below the form).")
    tier_threshold = t2.slider("Fuzzy match threshold", 70, 100, _clamp(ed.tier_match_threshold, 70, 100, int), 1,
                               key=f"{k}_college_tier_threshold",
                               help="How closely a college name must match a name in the tier list (100 = same "
                                    "words). Case, accents and punctuation are ignored; acronyms are matched too.")
    st.info(TIER_NOTE, icon=":material/balance:")
    scores = ed.tier_scores if isinstance(ed.tier_scores, dict) else {}
    tiers = sorted(scores, key=str)
    tc = st.columns(max(3, len(tiers)))
    tier_scores = {t: float(tc[i].number_input(f"Tier {theme.md_escape(t)} score", 0.0, 1.0,
                                               _clamp(scores[t], 0.0, 1.0), 0.05, key=f"{k}_tier_{t}"))
                   for i, t in enumerate(tiers)}
    listed = len(ed.tier_mapping) if isinstance(ed.tier_mapping, dict) else 0
    st.caption((f"Tier list in use: {listed:,} college names and aliases." if listed else
                "No tier list uploaded: only tiers given in the assessment feed are used.")
               + " Upload, download or clear it under **College tier list** below the form.")
    return {"edu": edu, "tier_scores": tier_scores, "tier_on": tier_on, "tier_threshold": tier_threshold}


def _shortlist_section(cfg: AppConfig, k: str, result: PipelineResult | None,
                       frame: pd.DataFrame | None) -> dict[str, Any]:
    st.markdown("##### Shortlist & eligibility")
    sl = cfg.shortlist
    s1, s2, s3, s4 = st.columns(4)
    current_method = str(sl.method)
    method = s1.radio("Method", list(METHODS), format_func=METHODS.get, key=f"{k}_method",
                      index=list(METHODS).index(current_method) if current_method in METHODS else 0)
    top_n = s2.number_input("Top N", 1, 100000, _clamp(sl.top_n, 1, 100000, int), 10, key=f"{k}_top_n")
    top_pct = s3.number_input("Top %", 0.1, 100.0, _clamp(sl.top_pct, 0.1, 100.0), 0.5, key=f"{k}_top_pct")
    threshold = s4.number_input("Score threshold", 0.0, 100.0, _clamp(sl.threshold, 0.0, 100.0), 1.0,
                                key=f"{k}_threshold")
    g1, g2, g3 = st.columns([1, 1, 2])
    min_pct = g1.number_input("Min HackerRank %", 0.0, 100.0, _clamp(sl.min_hackerrank_pct, 0.0, 100.0), 5.0,
                              key=f"{k}_min_pct")
    min_cgpa = g2.number_input("Min CGPA", 0.0, 10.0, _clamp(sl.min_cgpa, 0.0, 10.0), 0.1, key=f"{k}_min_cgpa")
    years = sorted({*range(2020, 2031), *_safe_years(sl.allowed_grad_years), *_data_years(result)})
    grad_years = g3.multiselect("Allowed graduation years", years, default=_safe_years(sl.allowed_grad_years),
                                key=f"{k}_years", placeholder="Any year")
    cap1, cap2 = st.columns([1, 3], vertical_alignment="center")
    college_cap = cap1.number_input("Max share of shortlist per college (%)", 0.0, 100.0,
                                    _clamp(getattr(sl, "college_cap_pct", 0.0), 0.0, 100.0), 1.0,
                                    key=f"{k}_college_cap", help="0 = no cap.")
    cap2.caption(CAP_NOTE)
    tie_breaks = st.multiselect("Tie-break order (after the final score)", list(TIE_BREAK_KEYS),
                                default=clean_tie_breaks(sl.tie_break_order), format_func=tie_break_label,
                                key=f"{k}_tie_breaks", placeholder="Candidate ID only",
                                help="Candidates with the same final score are ordered by these rules, in the order "
                                     "you pick them (the first rule decides first).")
    st.caption(TIE_BREAK_NOTE)

    st.markdown("###### Duplicates & exclusions")
    dedupe = st.checkbox(DEDUPE_LABEL, value=bool(sl.auto_disqualify_duplicates), key=f"{k}_dedupe",
                         help="Duplicate applications (same person under another e-mail or candidate ID) become "
                              "Ineligible with the rule 'Duplicate Application'.")
    _duplicate_note(cfg, frame)
    exclude_ids = st.text_area(EXCLUDE_LABEL, "\n".join(parse_candidate_ids(sl.exclude_candidates)), height=110,
                               key=f"{k}_exclude", placeholder="e.g. C-1000042, C-1000107",
                               help="Comma, space or newline separated; matched ignoring case. Listed candidates "
                                    "become Ineligible with the rule 'Administrative Exclusion'.")
    _exclusion_notes(cfg, frame)
    return {"method": method, "top_n": top_n, "top_pct": top_pct, "threshold": threshold, "min_pct": min_pct,
            "min_cgpa": min_cgpa, "grad_years": grad_years, "college_cap": college_cap, "tie_breaks": tie_breaks,
            "dedupe": dedupe, "exclude_ids": exclude_ids}


def _safe_years(values: Any) -> list[int]:
    out: list[int] = []
    for value in values if isinstance(values, (list, tuple)) else []:
        try:
            year = int(float(value))
        except (TypeError, ValueError, OverflowError):
            continue
        if 1900 <= year <= 2200 and year not in out:
            out.append(year)
    return out


def _duplicate_note(cfg: AppConfig, frame: pd.DataFrame | None) -> None:
    if frame is None or S.DUPLICATE_OF not in frame.columns:
        return
    kept = txt(frame, S.DUPLICATE_OF).map(_cell)
    n = int((kept != "").sum())
    if not n:
        return
    people = int(kept[kept != ""].nunique())
    effect = ("they are Ineligible (Duplicate Application)." if cfg.shortlist.auto_disqualify_duplicates else
              "they are scored like any other application.")
    st.caption(f"This dataset has {n:,} duplicate application(s) of {people:,} candidate(s); with the applied "
               f"setting {effect}")


def _exclusion_notes(cfg: AppConfig, frame: pd.DataFrame | None) -> None:
    """Warn about excluded IDs that are not in the loaded dataset (IDs only, never names)."""
    ids = parse_candidate_ids(cfg.shortlist.exclude_candidates)
    if not ids or frame is None or S.CANDIDATE_ID not in frame.columns:
        return
    known = set(txt(frame, S.CANDIDATE_ID).map(lambda v: _cell(v).upper()))
    missing = [i for i in ids if i not in known]
    found = len(ids) - len(missing)
    if missing:
        shown = ", ".join(missing[:MAX_LISTED_IDS])
        more = f" and {len(missing) - MAX_LISTED_IDS:,} more" if len(missing) > MAX_LISTED_IDS else ""
        st.warning(f"{len(missing):,} excluded ID(s) are not in the current dataset: {theme.md_escape(shown)}{more}.",
                   icon=":material/person_off:")
    if found:
        st.caption(f"{found:,} candidate(s) in the current dataset are disqualified by this list "
                   "(Administrative Exclusion).")


def _integrity_section(cfg: AppConfig, k: str) -> dict[str, Any]:
    st.markdown("##### Integrity policy")
    sl = cfg.shortlist
    hold = st.radio("When a shortlisted candidate is High risk", [False, True], format_func=POLICIES.get,
                    index=1 if sl.exclude_high_risk else 0, key=f"{k}_policy")
    levels = (S.RISK_HIGH, S.RISK_MEDIUM, S.RISK_LOW)
    review_levels = st.multiselect("Risk levels that mark a shortlisted candidate for integrity review", list(levels),
                                   default=list(dict.fromkeys(x for x in _list(sl.review_risk_levels) if x in levels)),
                                   key=f"{k}_review_levels")
    with st.expander("Risk thresholds & signal weights"):
        r = cfg.risk
        risk_nums = _num_grid(f"{k}_risk", RISK_NUMBERS, vars(r))
        ic = st.columns(len(RISK_INTS))
        risk_ints = {f: int(ic[i].number_input(label, lo, hi, _clamp(getattr(r, f), lo, hi, int), 1, key=f"{k}_ri_{f}"))
                     for i, (f, label, lo, hi) in enumerate(RISK_INTS)}
        st.caption(f"Team projects: {TEAM_HELP}")
        team = _num_grid(f"{k}_risk", TEAM_PARAMS, vars(r), ncols=4, helps=TEAM_FIELD_HELP)
        compare_all = st.toggle("Also compare perfect scorers' resumes against the whole pool",
                                value=bool(r.compare_against_all), key=f"{k}_compare_all")
        defaults = RiskConfig()
        st.caption("Pair signal weights (summed into a 0-100 pair score)")
        signal_labels = getattr(risk_engine, "SIGNAL_LABELS", None) or {}
        pair_values = {**defaults.pair_weights, **_weight_dict(r.pair_weights)}
        pair_w = _num_grid(f"{k}_pw", [(n, _named(signal_labels.get(n), n), 0.0, 100.0, 1.0) for n in pair_values],
                           pair_values, ncols=4)
        st.caption("Individual flag weights")
        flag_labels = getattr(risk_engine, "FLAG_LABELS", None) or {}
        ind_values = {**defaults.individual_weights, **_weight_dict(r.individual_weights)}
        ind_w = _num_grid(f"{k}_iw", [(n, _named(flag_labels.get(n), n), 0.0, 100.0, 1.0) for n in ind_values],
                          ind_values, ncols=4)
    return {"hold": hold, "review_levels": review_levels, "risk_nums": risk_nums, "risk_ints": risk_ints,
            "team": team, "compare_all": compare_all, "pair_w": pair_w, "ind_w": ind_w}


def _weight_dict(value: Any) -> dict[str, Any]:
    return {str(k): v for k, v in value.items() if str(k).strip()} if isinstance(value, dict) else {}


def _llm_section(cfg: AppConfig, k: str, status: dict[str, Any]) -> dict[str, Any]:
    lc = cfg.llm
    st.markdown(f"##### {LLM_TITLE}")
    st.caption(f":material/info: {LLM_NOTE}")
    st.caption(llm_status_line(status, cfg))
    error = str(status.get("error") or "")
    if error and lc.enabled:  # e.g. proxy not running, or running without Copilot models
        st.caption(theme.md_escape(_redact(error, cfg)))
    l1, l2, l3 = st.columns([1, 2, 1])
    llm_on = l1.toggle("LLM features enabled", value=bool(lc.enabled), key=f"{k}_llm_on")
    options, labels = model_options(cfg, status)
    current = str(lc.model or "").strip()
    model = l2.selectbox("Model", options, index=options.index(current) if current in options else 0,
                         format_func=lambda m: labels.get(m, m), key=f"{k}_llm_model", accept_new_options=True,
                         help=f"Models offered by the running proxy; {PROXY_DEFAULT} lets the proxy pick the best "
                              "available Copilot chat model. You can also type a model id.")
    concurrency = l3.number_input("Max concurrency", 1, 32, _clamp(lc.max_concurrency, 1, 32, int), 1,
                                  key=f"{k}_llm_conc")
    l4, l5, l6 = st.columns(3)
    temperature = l4.slider("Temperature", 0.0, 1.0, _clamp(lc.temperature, 0.0, 1.0), 0.05, key=f"{k}_llm_temp")
    max_tokens = l5.number_input("Max tokens", 128, 8192, _clamp(lc.max_tokens, 128, 8192, int), 128,
                                 key=f"{k}_llm_tokens", help="Upper limit on the length of each LLM answer.")
    timeout = l6.number_input("Timeout (s)", 5.0, 600.0, _clamp(lc.timeout_sec, 5.0, 600.0), 5.0,
                              key=f"{k}_llm_timeout", help="How long to wait for one LLM answer.")
    l7, l8 = st.columns([1, 2])
    max_chars = l7.number_input("Max input chars", 1000, 50000, _clamp(lc.max_input_chars, 1000, 50000, int), 500,
                                key=f"{k}_llm_chars",
                                help="Resume text sent to the LLM per candidate is cut to this many characters.")
    borderline = l8.number_input("Borderline band (candidates just below the cutoff to explain)", 0, 500,
                                 _clamp(lc.borderline_band, 0, 500, int), 5, key=f"{k}_llm_borderline",
                                 help="How many eligible candidates ranked just below the shortlist cutoff get a gap "
                                      "explanation. Explanations never move anyone onto the shortlist.")
    return {"llm_on": llm_on, "model": model, "concurrency": concurrency, "temperature": temperature,
            "max_tokens": max_tokens, "timeout": timeout, "max_chars": max_chars, "borderline": borderline}


def _snapshot_section(cfg: AppConfig, k: str) -> dict[str, Any]:
    sc = cfg.snapshots
    st.markdown("##### Fast reload")
    f1, f2, f3 = st.columns([1, 1, 2], vertical_alignment="center")
    snap_on = f1.toggle("Cache parsed datasets for fast reload", value=bool(sc.enabled), key=f"{k}_snap_on",
                        help="Reopening the same files, or refreshing the page, then skips re-parsing.")
    snap_ttl = f2.number_input("Keep cached datasets for (days)", 1, 30, _clamp(sc.ttl_days, 1, 30, int), 1,
                               key=f"{k}_snap_ttl")
    f3.caption("Cached datasets hold candidate data on the server and are deleted automatically once they are "
               "older than this. **Clear cached datasets** (below) deletes them at once.")
    return {"snap_on": snap_on, "snap_ttl": snap_ttl}


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


def _preview(result: PipelineResult | None, prepared: Any, cfg: AppConfig, candidate: AppConfig) -> None:
    """Evaluate the proposed config on the current data and store the shortlist diff."""
    if result is None or prepared is None:
        st.info("Run a screening first - the preview compares against the current results.")
        return
    try:
        # Reuse the current integrity analysis when the risk settings are unchanged (as re-scoring does).
        same_risk = isinstance(result.config, AppConfig) and asdict(candidate.risk) == asdict(result.config.risk)
        with st.spinner("Scoring the proposed configuration..."):
            preview = pipeline.evaluate(prepared, candidate, risk_result=result.risk if same_risk else None)
            diff = analytics.shortlist_diff(result.candidates, preview.candidates)
    except Exception as exc:  # noqa: BLE001 - show the failure, keep the app alive
        log.warning("Config preview failed (%s)", type(exc).__name__)
        st.session_state.pop(K_PREVIEW, None)
        st.error(f"Could not preview these settings: {state.safe_error(exc)}")
        return
    st.session_state[K_PREVIEW] = {"config": candidate, "diff": diff, "changes": config_changes(cfg, candidate),
                                   "basis": _basis(),
                                   "ineligible": ineligible_summary(result.candidates, preview.candidates)}


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
    inel = preview.get("ineligible") if isinstance(preview.get("ineligible"), dict) else None
    with st.container(border=True, key="cfg_preview_panel"):
        st.markdown("##### :material/difference: Preview: impact of the proposed changes")
        changes = preview.get("changes") or []
        if changes:
            shown = "; ".join(theme.md_escape(c) for c in changes[:12])
            st.caption(f"Proposed changes: {shown}" + (f" (+{len(changes) - 12} more)" if len(changes) > 12 else ""))
        else:
            st.caption("The form matches the active configuration.")
        m = st.columns(6 if inel else 5)
        _delta_metric(m[0], "Shortlisted", s.get("shortlisted_before"), s.get("shortlisted_after"))
        m[1].metric("Entering", f"{int(s.get('entering', 0)):,}", border=True,
                    help="Not shortlisted now, shortlisted after the change")
        m[2].metric("Leaving", f"{int(s.get('leaving', 0)):,}", border=True,
                    help="Shortlisted now, not shortlisted after the change")
        _delta_metric(m[3], "Cutoff score", s.get("cutoff_before"), s.get("cutoff_after"), fmt="{:.2f}")
        _delta_metric(m[4], "Integrity reviews", s.get("review_before"), s.get("review_after"))
        if inel:
            _delta_metric(m[5], "Ineligible", inel.get("before"), inel.get("after"))
            moved = [f"{theme.md_escape(rule or 'Other')} {a:,} → {b:,}" for rule, a, b in inel.get("rules") or []]
            if moved:
                st.caption("Ineligible by rule (now → after): " + "; ".join(moved))
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
# College tier list
# ---------------------------------------------------------------------------
def _set_tier_mapping(mapping: dict[str, str], message: str) -> None:
    """Make ``mapping`` the active tier list.

    No form field shows the tier list, so the config is replaced without re-keying the form:
    unsaved edits in the form survive (they are applied later on top of the new list).
    """
    new = AppConfig.from_dict(state.get_config().to_dict())
    new.education.tier_mapping = {str(k): str(v) for k, v in mapping.items()}
    st.session_state.pop(K_PREVIEW, None)
    st.session_state[state.K_CONFIG] = new
    state.queue_message(message, icon=":material/school:")
    st.rerun()


def _parsed_upload(uploaded: Any, cfg: AppConfig) -> TierList | str:
    """The uploaded tier list (parsed once per file), or the reason it cannot be used."""
    try:
        data = uploaded.getvalue()
        name = str(getattr(uploaded, "name", "") or "")
    except Exception as exc:  # noqa: BLE001 - unreadable upload
        return f"The uploaded file could not be read ({type(exc).__name__})."
    scores = cfg.education.tier_scores if isinstance(cfg.education.tier_scores, dict) else {}
    digest = hashlib.sha256(name.encode("utf-8", "replace") + b"\0" + data + json.dumps(
        sorted(map(str, scores))).encode()).hexdigest()
    cached = st.session_state.get(K_TIER_UPLOAD)
    if isinstance(cached, tuple) and len(cached) == 2 and cached[0] == digest:
        return cached[1]
    try:
        parsed: TierList | str = parse_tier_list(name, data, scores)
    except ValueError as exc:
        parsed = str(exc)
    except Exception as exc:  # noqa: BLE001 - never raise into the UI
        log.warning("Reading a tier list failed (%s)", type(exc).__name__)
        parsed = f"The tier list could not be read ({type(exc).__name__})."
    st.session_state[K_TIER_UPLOAD] = (digest, parsed)
    return parsed


def _tier_csv(mapping: dict[str, str]) -> bytes:
    frame = tier_list_frame(mapping)
    writer = state.engine_fn(export, "to_csv_bytes")
    if writer is not None:
        try:
            return writer(frame)
        except Exception:  # noqa: BLE001 - fall back to a plain CSV
            pass
    return frame.to_csv(index=False).encode("utf-8-sig")


def _tier_section(cfg: AppConfig, frame: pd.DataFrame | None) -> None:
    ed = cfg.education
    mapping = {str(k): str(v) for k, v in ed.tier_mapping.items()} if isinstance(ed.tier_mapping, dict) else {}
    with st.expander("College tier list", icon=":material/school:", key="cfg_tier_expander"):
        st.caption(f"Approved tier list (CSV or Excel, up to {TIER_MAX_MB} MB and {TIER_MAX_ROWS:,} rows) with the "
                   "columns **college** and **tier**, plus optional **aliases** separated by ';'. Names are matched "
                   "ignoring case, accents and punctuation; acronyms and close spellings are matched at the fuzzy "
                   "match threshold (Education).")
        in_use = (f"In use: {len(mapping):,} college names and aliases, tiers "
                  f"{', '.join(theme.md_escape(t) for t in sorted(set(mapping.values()), key=_natural))}."
                  if mapping else "No tier list in use.")
        scoring_note = ("" if ed.tier_enabled else " Tier scoring is off, so the list gives no points until "
                        "**Score college tier** is switched on and applied.")
        st.caption(in_use + scoring_note)
        u1, u2 = st.columns([3, 1])
        uploaded = u1.file_uploader("Upload a tier list (CSV or Excel)", type=TIER_TYPES, key="cfg_tier_upload",
                                    max_upload_size=TIER_MAX_MB)
        with u2:
            st.download_button("Download tier list (CSV)", data=_tier_csv(mapping), file_name="college_tier_list.csv",
                               mime="text/csv", on_click="ignore", icon=":material/download:", key="cfg_tier_dl",
                               width="stretch", disabled=not mapping)
            clear = st.button("Clear tier list", icon=":material/delete:", key="cfg_tier_clear", width="stretch",
                              disabled=not mapping,
                              help="Stop using the tier list; tier scoring then uses only tiers given in the "
                                   "assessment feed.")
        if clear:
            _set_tier_mapping({}, "Tier list cleared.")
        pending: dict[str, str] | None = None
        if uploaded is not None:
            parsed = _parsed_upload(uploaded, cfg)
            if isinstance(parsed, str):
                st.error(f"This tier list cannot be used: {theme.md_escape(parsed)}", icon=":material/error:")
            else:
                pending = _upload_summary(parsed, mapping)
        _tier_preview_block(cfg, mapping, pending, frame)


def _upload_summary(parsed: TierList, mapping: dict[str, str]) -> dict[str, str] | None:
    """Show what an upload contains; -> its mapping while it is not the list in use."""
    same = parsed.mapping == mapping
    tiers = ", ".join(theme.md_escape(t) for t in parsed.tiers)
    st.success(f"Read {parsed.colleges:,} college(s) and {parsed.aliases:,} alias(es); tiers {tiers}.",
               icon=":material/task_alt:")
    if parsed.skipped:
        st.warning(f"{parsed.skipped:,} row(s) without a college or a tier were skipped.", icon=":material/warning:")
    if parsed.conflicts:
        names = ", ".join(theme.md_escape(n) for n in parsed.conflicts[:10])
        more = f" and {len(parsed.conflicts) - 10:,} more" if len(parsed.conflicts) > 10 else ""
        st.warning(f"{len(parsed.conflicts):,} name(s) are listed again with a different tier; the first tier was "
                   f"kept: {names}{more}.", icon=":material/warning:")
    if parsed.unscored:
        labels = ", ".join(theme.md_escape(t) for t in parsed.unscored[:10])
        st.warning(f"Tier label(s) without a score give no tier points: {labels}. Use tier numbers (1, 2, 3) or "
                   "labels that match the Tier scores in Education.", icon=":material/warning:")
    if same:
        st.caption(":material/check_circle: This list is the tier list in use.")
    if st.button("Use this tier list", type="primary", icon=":material/check:", key="cfg_tier_use", disabled=same,
                 help="Makes this the tier list in use. Unsaved edits in the form above are kept."):
        _set_tier_mapping(parsed.mapping, f"Tier list in use: {len(parsed.mapping):,} college names and aliases.")
    return None if same else parsed.mapping


def _tier_preview_cached(frame: pd.DataFrame, cfg: AppConfig) -> dict[str, Any]:
    ed = cfg.education
    blob = json.dumps([st.session_state.get(state.K_PREPARED_FP, ""), id(frame), len(frame), ed.tier_mapping,
                       ed.tier_match_threshold, ed.tier_scores], sort_keys=True, default=str)
    key = hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()
    cached = st.session_state.get(K_TIER_PREVIEW)
    if isinstance(cached, tuple) and len(cached) == 2 and cached[0] == key:
        return cached[1]
    with st.spinner("Matching the dataset's colleges to the tier list..."):
        data = tier_preview(frame, cfg)
    st.session_state[K_TIER_PREVIEW] = (key, data)
    return data


def _tier_preview_block(cfg: AppConfig, mapping: dict[str, str], pending: dict[str, str] | None,
                        frame: pd.DataFrame | None) -> None:
    st.markdown("###### Preview: the current dataset's colleges")
    if frame is None:
        st.caption("Load a dataset to see how its colleges match the tier list.")
        return
    shown = cfg
    if pending is not None:
        shown = AppConfig.from_dict(cfg.to_dict())
        shown.education.tier_mapping = dict(pending)
    try:
        data = _tier_preview_cached(frame, shown)
    except Exception as exc:  # noqa: BLE001 - the preview is optional
        log.warning("Tier preview failed (%s)", type(exc).__name__)
        st.warning(f"The tier preview is unavailable: {state.safe_error(exc)}")
        return
    total, unmatched, table = int(data.get("colleges", 0)), int(data.get("unmatched", 0)), data.get("table")
    if not total or not isinstance(table, pd.DataFrame):
        st.caption("This dataset has no college names to match.")
        return
    which = ("the uploaded list (not in use yet)" if pending is not None else
             "the tier list in use" if mapping else "tiers from the assessment feed only (no tier list in use)")
    st.caption(f"{min(len(table), total):,} largest of {total:,} colleges, resolved with {which}. "
               f"{unmatched:,} of {total:,} colleges have no tier - {UNMATCHED_NOTE}.")
    view = table.assign(source=table["source"].map(lambda s: TIER_SOURCE_LABELS.get(s, s)))
    cc = st.column_config
    st.dataframe(view, hide_index=True, width="stretch", key="cfg_tier_preview", height=min(420, 42 + 35 * len(view)),
                 column_config={"college": cc.TextColumn("College", width="large"),
                                "candidates": cc.NumberColumn("Candidates", format="%d"),
                                "tier": cc.TextColumn("Tier"),
                                "source": cc.TextColumn("Source", help="Assessment feed: the feed's own tier (it wins "
                                                                       "when it can be scored). Tier list: matched in "
                                                                       "the approved list. No match: no tier points.")})


# ---------------------------------------------------------------------------
# Profiles, cache & connectivity
# ---------------------------------------------------------------------------
def _tools(cfg: AppConfig, status: dict[str, Any]) -> None:
    st.markdown("##### Profiles, cache & connectivity")
    c1, c2, c3, c4 = st.columns(4)
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
        uploaded = st.file_uploader("Upload a config JSON", type=["json"], key="cfg_upload",
                                    max_upload_size=PROFILE_MAX_MB)
        if uploaded is not None and st.button("Apply uploaded config", key="cfg_apply_upload", width="stretch"):
            _apply_profile(uploaded)
    with c3:
        _cache_tools(cfg)
    with c4:
        _connection_tools(cfg, status)


def _apply_profile(uploaded: Any) -> None:
    """Apply an uploaded tuning profile (connection settings in it are ignored by AppConfig.from_json)."""
    try:
        data = uploaded.getvalue()
        if len(data) > PROFILE_MAX_MB * 1024 * 1024:
            raise ValueError(f"the file is larger than {PROFILE_MAX_MB} MB")
        new = AppConfig.from_json(data.decode("utf-8-sig"))
        new.shortlist.tie_break_order = clean_tie_breaks(new.shortlist.tie_break_order)
        new.shortlist.exclude_candidates = parse_candidate_ids(new.shortlist.exclude_candidates)
        new.validate()                      # raises for values of the wrong type (e.g. text weights)
        float(new.weights.total())
        state.eval_key("profile-check", new)  # the profile must serialise like any applied config
    except Exception as exc:  # noqa: BLE001 - any malformed file
        st.error(f"Could not read that config: {state.safe_error(exc)}")
        return
    st.session_state.pop(K_PREVIEW, None)
    state.set_config(new)
    state.queue_message("Uploaded configuration applied.", icon=":material/upload:")
    st.rerun()


def _cache_tools(cfg: AppConfig) -> None:
    cached = state.recent_datasets(cfg)
    n = len(cached) if isinstance(cached, pd.DataFrame) else 0
    if st.button("Clear cached datasets", icon=":material/delete_sweep:", key="cfg_snap_clear", width="stretch",
                 disabled=cached is not None and n == 0,
                 help="Deletes every cached dataset on the server. Saved runs are not affected; uploading the files "
                      "again rebuilds the cache."):
        _clear_snapshots(cfg)
    days = _clamp(cfg.snapshots.ttl_days, 1, 36500, int)
    if cached is None:
        st.caption("The dataset cache could not be read.")
    elif n:
        sizes = pd.to_numeric(cached["size_mb"], errors="coerce") if "size_mb" in cached.columns else pd.Series([0.0])
        st.caption(f"{n:,} cached dataset(s), {float(sizes.fillna(0).sum()):,.1f} MB on the server; each is kept "
                   f"for {days:,} day(s).")
    else:
        st.caption("No cached datasets." + ("" if cfg.snapshots.enabled else " Caching is switched off."))


def _clear_snapshots(cfg: AppConfig) -> None:
    lister = state.engine_fn(snapshots, "list_snapshots")
    deleter = state.engine_fn(snapshots, "delete_snapshot")
    if lister is None or deleter is None:
        st.error("The dataset cache is not available in this build.")
        return
    try:
        listed = lister(cfg)
        fingerprints = [str(fp) for fp in listed["fingerprint"]] if "fingerprint" in listed else []
    except Exception as exc:  # noqa: BLE001
        log.warning("Listing cached datasets failed (%s)", type(exc).__name__)
        st.error(f"The cached datasets could not be listed: {state.safe_error(exc)}")
        return
    deleted = failed = 0
    for fp in fingerprints:
        try:
            deleted += 1 if deleter(fp, cfg) else 0
        except Exception as exc:  # noqa: BLE001 - e.g. files in use; keep going
            log.warning("Deleting a cached dataset failed (%s)", type(exc).__name__)
            failed += 1
    if failed:
        st.error(f"Deleted {deleted:,} cached dataset(s); {failed:,} could not be deleted (files in use?). "
                 "Try again in a moment.")
        return
    state.queue_message(f"Cleared {deleted:,} cached dataset(s).", icon=":material/delete_sweep:")
    st.rerun()


def _connection_tools(cfg: AppConfig, status: dict[str, Any]) -> None:
    clicked = st.button("Test connection", icon=":material/network_check:", key="cfg_llm_test", width="stretch",
                        disabled=not cfg.llm.enabled,
                        help="Sends one tiny request through the VS Code proxy. The LLM only writes text; it never "
                             "changes scores, ranks or the shortlist.")
    if clicked:
        _test_connection(cfg)
    if not cfg.llm.enabled:
        st.caption("LLM features are switched off (LLM section above).")
    elif state.llm_ready(cfg)[0]:
        base = str(status.get("base_url") or cfg.llm.base_url or "").replace("`", "")
        st.caption(f"Tests the applied settings: model {theme.md_escape(cfg.llm.model or PROXY_DEFAULT)} at `{base}`.")
    else:
        st.caption("No proxy token found yet: see the LLM section above, then test again.")


def _test_connection(cfg: AppConfig) -> None:
    """One tiny request with the applied settings; the outcome never contains the token."""
    try:
        state.llm_status(cfg, refresh=True)
    except Exception:  # noqa: BLE001 - the status line is refreshed on a best-effort basis
        pass
    try:
        client = state.get_llm_client(cfg)
    except Exception as exc:  # noqa: BLE001 - e.g. no usable token
        st.error(f"LLM connection failed: {theme.md_escape(_redact(state.safe_error(exc), cfg))}")
        return
    if client is None:
        reason = state.llm_ready(cfg)[1] or "the LLM client is not available."
        st.error(f"LLM connection failed: {theme.md_escape(_redact(reason, cfg))}")
        return
    try:
        ok, message = client.test_connection()
    except Exception as exc:  # noqa: BLE001
        ok, message = False, state.safe_error(exc)
    text = theme.md_escape(_redact(message, cfg))
    if ok:
        st.success(f"LLM connection OK: {text}", icon=":material/check_circle:")
    else:
        st.error(f"LLM connection failed: {text}", icon=":material/error:")
