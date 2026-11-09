"""Integrity & anomaly engine.

``run_risk_analysis`` studies the *focus group* (hackerrank_pct >= cfg.risk.focus_threshold_pct,
i.e. perfect scorers by default) and returns review evidence - never verdicts:

1. commonality    - attribute values over-represented in the focus group (lift, one-sided
                    Fisher exact via the hypergeometric tail, Benjamini-Hochberg q-values);
2. pairs          - pair signals (identical answers, shared repo / rare bullets / resume text,
                    same IP / subnet, synchronised timing, back-to-back sessions on one IP,
                    shared phone / email, HackerRank plagiarism match), generated with groupby
                    self-joins, sorted time windows and sparse TF-IDF products - never a Python
                    double loop over candidates;
3. individual     - per-candidate flags (speed anomaly, HackerRank / Workday plagiarism and
                    integrity fields, hidden or adversarial resume text - prompt injection);
4. clusters       - connected components of pairs with pair_score >= edge_threshold;
5. candidate_risk - min(100, max_pair_score + individual_score) for every candidate, with
                    reasons that name the evidence and the linked candidate IDs, plus
                    ``injection_flags`` (summaries of hidden / adversarial resume text).

Precision guards for legitimate look-alikes (all reported in ``RiskResult.params``): answer
hashes held by a large share of the focus group are answer keys; bullets / repos held by many
resumes are boilerplate; a shared lab/NAT/hostel IP of one college (or of an On-campus venue)
is not evidence; timing matches inside scheduled or crowded sessions are down-weighted, and
timing alone links candidates only inside a synchronised group of >= 3. Teammates who list
the same 1-2 GitHub repos on otherwise different resumes are a team project
(``team_max_*`` settings), scored with ``team_repo_weight`` and worded as collaboration to
verify. Duplicate applications of one person (``duplicate_of`` / ``duplicate_ids`` in the
input) are never paired - eligibility handles them. Identity signals (phone/email) and
HackerRank's plagiarism match also link a perfect scorer to a non-focus candidate. Missing
columns skip a signal with a warning, and a failure inside one signal never aborts the analysis.
"""
from __future__ import annotations

import ipaddress
import logging
import math
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, TypeVar

import networkx as nx
import numpy as np
import pandas as pd
from scipy import sparse
from scipy.stats import false_discovery_control, hypergeom
from sklearn.feature_extraction.text import TfidfVectorizer

from . import injection
from . import schema as S
from .config import AppConfig, RiskConfig
from .models import ParsedResume, RiskResult

logger = logging.getLogger(__name__)
_T = TypeVar("_T")

# Derived at ingest (addendum); schema.py has no constant for it yet.
PLAGIARISM_MATCH_ID = getattr(S, "PLAGIARISM_MATCH_ID", "plagiarism_match_id")
SHARED_EMAIL_FLAG = "shared_email"

# ---------------------------------------------------------------------------
# Output columns (docs/ARCHITECTURE.md, "screening/risk.py")
# ---------------------------------------------------------------------------
CAND_A, CAND_B = "cand_a", "cand_b"
PAIR_COLUMNS = [
    CAND_A, CAND_B, "pair_score", "signals", "signal_keys", "shared_hashes", "resume_similarity",
    "shared_bullets", "shared_bullet_examples", "shared_github", "same_ip", "same_subnet",
    "same_college", "same_phone", "same_email", "start_gap_sec", "submit_gap_sec",
    "duration_gap_min", "sequential_gap_min",
]
CLUSTER_COLUMNS = [
    "cluster_id", "size", "members", "member_names", "risk_level", "max_pair_score",
    "mean_pair_score", "density", "top_signals", "shared_traits", "colleges", "n_colleges",
    "start_span_min", "submit_span_min", "summary",
]
COMMONALITY_COLUMNS = [
    "attribute", "value", "focus_count", "focus_total", "focus_share", "population_count",
    "population_total", "population_share", "lift", "p_value", "q_value", "significant",
    "attribute_key",
]
INDIVIDUAL_COLUMNS = [S.CANDIDATE_ID, "flag_key", "flag", "detail", "weight"]
CANDIDATE_RISK_COLUMNS = [
    S.CANDIDATE_ID, S.IS_FOCUS, S.RISK_SCORE, S.RISK_LEVEL, S.RISK_REASONS, S.CLUSTER_ID,
    S.N_LINKS, S.MAX_PAIR_SCORE, S.INDIVIDUAL_SCORE, S.INJECTION_FLAGS,
]

# ---------------------------------------------------------------------------
# Small-sample / precision guards (reported in RiskResult.params)
# ---------------------------------------------------------------------------
COMMON_HASH_MIN_HOLDERS = 10   # hash is an answer key if held by > max(share*n, min(10, n/2))
RARE_MIN_HOLDERS = 5           # bullets / repos stay "rare" up to max(share*N, 5) resumes
TIMING_CROWD_MAX = 5           # > 5 other focus members in a sync window that do not also move
                               # with the candidate (start AND submit) = scheduled/crowded session
TIMING_EXPAND_CAP = 250        # neighbour expansion cap per candidate (performance guard)
MIN_SYNC_GROUP = 3             # timing-only links need a synchronised group of >= 3 candidates
IDENTITY_MAX_GROUP = 10        # phone/email on more candidates = placeholder / institutional
IP_INFRA_MAX_FOCUS = 50        # more perfect scorers than this on one IP = venue / VPN / proxy server
MIN_SPEED_SAMPLE = 5           # completion times needed for a focus-group median
SCHEDULED_TIMING_FACTOR = 0.25  # timing weight for scheduled On-campus / crowded sessions
MIN_DOC_WORDS = 25             # projects+experience shorter than this -> full resume text
MAX_IDS_IN_REASON = 6
MAX_BULLET_EXAMPLES = 3


@dataclass(frozen=True)
class _SignalDef:
    label: str
    group: str          # reasons aggregate per (candidate, group)
    timing: bool = False


SIGNALS: dict[str, _SignalDef] = {
    "shared_answer_hash": _SignalDef("Identical answers", "hash"),
    "plagiarism_match": _SignalDef("HackerRank plagiarism match", "plag"),
    "same_phone": _SignalDef("Same phone", "phone"),
    "same_email": _SignalDef("Same email", "email"),
    "shared_github_repo": _SignalDef("Shared GitHub repo", "repo"),
    "team_github_repo": _SignalDef("Shared team project GitHub repo", "team_repo"),
    "resume_text_high": _SignalDef("Near-identical resume text", "text"),
    "resume_text_medium": _SignalDef("Similar resume text", "text"),
    "shared_bullets": _SignalDef("Shared rare resume bullets", "bullets"),
    "sequential_same_ip": _SignalDef("Back-to-back sessions on one IP", "ip"),
    "same_ip": _SignalDef("Same IP", "ip"),
    "same_subnet": _SignalDef("Same /24 subnet", "subnet"),
    "sync_submission": _SignalDef("Synchronised submission", "timing", True),
    "sync_start": _SignalDef("Synchronised start", "timing", True),
    "duration_match": _SignalDef("Matching duration", "timing", True),
}
SIGNAL_LABELS: dict[str, str] = {k: s.label for k, s in SIGNALS.items()}
TIMING_SIGNALS = frozenset(k for k, s in SIGNALS.items() if s.timing)
_SIGNAL_RANK = {k: i for i, k in enumerate(SIGNALS)}

FLAG_LABELS: dict[str, str] = {
    "prompt_injection": "Prompt injection",
    "hidden_text": "Hidden text in resume",
    "invisible_chars": "Invisible characters in resume",
    "plagiarism_flag": "HackerRank plagiarism flag",
    "speed_anomaly": "Speed anomaly",
    "high_code_similarity": "High code similarity",
    "integrity_status": "Proctoring integrity status",
    "proctoring_flags": "Proctoring flags",
    "out_of_window_time": "Time outside test window",
    "secure_mode_exits": "Secure-mode exits",
    "tab_switches": "Tab switches",
    "copy_paste": "Copy-paste events",
}

_DEFAULT_RC = RiskConfig()
_EPOCH = pd.Timestamp("1970-01-01")
_MISSING_TOKENS = frozenset({"", "nan", "none", "null", "na", "n/a", "-", "--", "nil", "<na>", "nat"})
_EMPTY_HASHES = frozenset({
    "0",
    "d41d8cd98f00b204e9800998ecf8427e",                                   # md5("")
    "da39a3ee5e6b4b0d3255bfef95601890afd80709",                           # sha1("")
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",   # sha256("")
})
_NO_FLAG_TOKENS = frozenset({
    "no", "false", "0", "clear", "ok", "nofl", "noflags", "noflag", "noissues", "noissue",
    "nonedetected", "noviolations", "noviolation", "nothingdetected", "clean",
})
_TRUE_TOKENS = frozenset({"true", "t", "yes", "y", "1", "flagged", "suspicious", "plagiarised",
                          "plagiarized", "detected"})
_ON_CAMPUS = frozenset({"oncampus", "campus", "incampus", "offline", "onsite", "inperson",
                        "venue", "testcentre", "testcenter", "centre", "center", "poolcampus",
                        "campusdrive", "poolcampusdrive"})
_FAKE_PHONES = frozenset({"1234567890", "0123456789", "9876543210", "0987654321"})
_WORD_RE = re.compile(r"[a-z]+")
_ID_SPLIT_RE = re.compile(r"[,;|\s]+")      # ", "-joined candidate IDs (duplicate_ids)
_NON_ALNUM_RE = re.compile(r"[^0-9a-z]+")
_KEY_RE = re.compile(r"[^\w]+")
_IP_SPLIT_RE = re.compile(r"[,;|\s]+")
_LIST_SEP_RE = re.compile(r"[,|;/•]")
_REPO_RE = re.compile(r"(?:https?://)?(?:www\.)?github\.com/([^/\s?#]+)/([^/\s?#]+)", re.I)
_GH_RESERVED = frozenset({
    "orgs", "topics", "features", "sponsors", "settings", "marketplace", "apps", "about",
    "pricing", "login", "join", "explore", "collections", "trending", "events", "site",
    "enterprise", "customer-stories", "security", "team", "nonprofit", "search", "notifications",
})


# ===========================================================================
# Public entry point
# ===========================================================================
def run_risk_analysis(
    candidates: pd.DataFrame,
    parsed_by_candidate: dict[str, ParsedResume],
    cfg: AppConfig,
    parsed_all: dict[str, ParsedResume] | None = None,
) -> RiskResult:
    """Run the integrity engine over ``candidates`` (one row per candidate_id).

    ``parsed_by_candidate`` maps candidate_id -> matched resume; ``parsed_all`` (all parsed
    resumes, keyed by file hash) is the population for bullet / repo rarity. Never raises on
    bad data: skipped signals are listed in ``RiskResult.warnings``.
    """
    cfg = cfg if isinstance(cfg, AppConfig) else AppConfig()
    rc = cfg.risk
    warnings: list[str] = []
    params: dict[str, Any] = _base_params(rc)

    ctx = _guard("Risk input preparation",
                 lambda: _build_context(candidates, parsed_by_candidate, parsed_all, rc, warnings, params),
                 None, warnings)
    if ctx is None:
        return RiskResult(focus_ids=[], candidate_risk=_all_clear(candidates),
                          pairs=_empty(PAIR_COLUMNS), clusters=_empty(CLUSTER_COLUMNS),
                          commonality=_empty(COMMONALITY_COLUMNS), individual=_empty(INDIVIDUAL_COLUMNS),
                          params=params, warnings=warnings)

    commonality = _guard("Commonality analysis", lambda: _commonality(ctx),
                         _empty(COMMONALITY_COLUMNS), warnings)
    individual = _guard("Individual flags", lambda: _individual_flags(ctx),
                        _empty(INDIVIDUAL_COLUMNS), warnings)
    pairs = _guard("Pair analysis", lambda: _pair_analysis(ctx), None, warnings) or _Pairs.empty()
    candidate_risk, clusters = _guard(
        "Risk aggregation", lambda: _aggregate(ctx, pairs, individual),
        (_all_clear(ctx.df, ctx.focus), _empty(CLUSTER_COLUMNS)), warnings)

    params.update(ctx.params)
    params.update({
        "n_pairs": int(len(pairs.frame)),
        "n_edges": int((pairs.score >= rc.edge_threshold).sum()),
        "n_clusters": int(len(clusters)),
    })
    return RiskResult(
        focus_ids=[str(x) for x in ctx.ids[ctx.focus]],
        candidate_risk=candidate_risk,
        pairs=pairs.frame,
        clusters=clusters,
        commonality=commonality,
        individual=individual,
        params=params,
        warnings=warnings,
    )


# ===========================================================================
# Context: one cleaned, ID-sorted view of the inputs shared by all signals
# ===========================================================================
@dataclass
class _Ctx:
    rc: RiskConfig
    df: pd.DataFrame                 # sorted by candidate_id, RangeIndex (position == pair index)
    ids: np.ndarray                  # object[str], unique, ascending
    focus: np.ndarray                # bool
    names: np.ndarray                # object[str]
    college: np.ndarray              # object[str | None] display value
    college_key: np.ndarray          # object[str | None] normalised
    test_mode: np.ndarray            # object[str | None]
    on_campus: np.ndarray            # bool
    ip: np.ndarray | None            # object[str | None] first IP
    subnet: np.ndarray | None        # object[str | None] IPv4 /24
    phone_key: np.ndarray | None     # object[str | None], placeholders removed
    email_key: np.ndarray | None
    start: np.ndarray | None         # float epoch seconds
    submit: np.ndarray | None
    duration: np.ndarray | None      # float minutes
    sim_best: np.ndarray             # float 0..100 (HackerRank code similarity), NaN
    sim_q: np.ndarray                # object[str] question of sim_best ("" = overall)
    match_pos: np.ndarray | None     # int position of HackerRank's most-similar candidate, -1
    resumes: dict[int, ParsedResume]
    population: list[ParsedResume]
    pw: dict[str, float]
    iw: dict[str, float]
    warnings: list[str]
    params: dict[str, Any]
    text_rows: dict[int, int] = field(default_factory=dict)   # position -> TF-IDF row
    text_matrix: Any = None                                   # csr, L2-normalised rows
    bullet_counts: dict[str, int] = field(default_factory=dict)  # bullet -> resumes holding it
    timing: _Timing | None = None
    dup_group: np.ndarray | None = None   # same number = one person's applications (never paired)

    @property
    def n(self) -> int:
        return len(self.ids)

    @property
    def n_focus(self) -> int:
        return int(self.focus.sum())


def _build_context(candidates: Any, parsed_by_candidate: Any, parsed_all: Any, rc: RiskConfig,
                   warnings: list[str], params: dict[str, Any]) -> _Ctx | None:
    if not isinstance(candidates, pd.DataFrame) or candidates.empty:
        warnings.append("No candidates supplied; risk analysis skipped.")
        return None
    if S.CANDIDATE_ID not in candidates.columns:
        warnings.append("No candidate_id column; risk analysis skipped.")
        return None

    raw_ids = _str_values(candidates[S.CANDIDATE_ID])
    keep = np.zeros(len(raw_ids), dtype=bool)
    seen: set[str] = set()
    for i, v in enumerate(raw_ids):
        if v is not None and v not in seen:
            seen.add(v)
            keep[i] = True
    n_blank = int(sum(v is None for v in raw_ids))
    n_dup = len(raw_ids) - n_blank - int(keep.sum())
    if n_blank:
        warnings.append(f"{n_blank} row(s) without a candidate_id were ignored by the risk analysis.")
    if n_dup:
        warnings.append(f"{n_dup} duplicate candidate_id row(s) were ignored by the risk analysis (first kept).")
    if not keep.any():
        warnings.append("No usable candidate IDs; risk analysis skipped.")
        return None
    kept = np.flatnonzero(keep)
    order = kept[np.argsort(raw_ids[kept].astype(str), kind="stable")]
    df = candidates.iloc[order].reset_index(drop=True)
    ids = raw_ids[order]
    n = len(ids)

    pct = _num_col(df, S.HR_PCT)
    if pct is None:
        warnings.append("No hackerrank_pct column: the focus group is empty, so commonality and pair "
                        "signals were skipped.")
        focus = np.zeros(n, dtype=bool)
    else:
        focus = np.nan_to_num(pct, nan=-np.inf) >= float(rc.focus_threshold_pct) - 1e-9
        if not focus.any():
            warnings.append(f"No candidate reached the focus threshold ({rc.focus_threshold_pct:g}%): "
                            "commonality and pair signals were skipped.")

    names = _str_col(df, S.NAME)
    college = _str_col(df, S.COLLEGE)
    test_mode = _str_col(df, S.TEST_MODE)
    college = college if college is not None else _nones(n)
    test_mode = test_mode if test_mode is not None else _nones(n)

    ip_raw = _str_col(df, S.IP_ADDRESS)
    ip = None if ip_raw is None else np.array([_first_ip(v) for v in ip_raw], dtype=object)
    subnet_col = _str_col(df, S.IP_SUBNET)
    subnet: np.ndarray | None = None
    if ip is not None or subnet_col is not None:
        subnet = np.array([
            _subnet_of(ip[i] if ip is not None else None) or (subnet_col[i] if subnet_col is not None else None)
            for i in range(n)], dtype=object)

    phone_key, email_key = _identity_keys(df, warnings)
    start, submit, duration = _timing_arrays(df)
    sim_best, sim_q = _code_similarity(df, n)
    match_pos = _resolve_matches(df, ids)

    resumes, population = _resume_inputs(ids, parsed_by_candidate, parsed_all)
    n_focus = int(focus.sum())
    derived: dict[str, Any] = {
        "n_candidates": n,
        "n_focus": n_focus,
        "n_resumes_linked": len(resumes),
        "n_resumes_population": len(population),
        "timing_crowd_max": TIMING_CROWD_MAX,
        "min_sync_group": MIN_SYNC_GROUP,
        "rare_max_holders": int(max(RARE_MIN_HOLDERS,
                                    math.ceil(rc.max_bullet_population_share * len(population)))),
        "identity_max_group": IDENTITY_MAX_GROUP,
        "ip_infra_max_focus": IP_INFRA_MAX_FOCUS,
        "scheduled_timing_factor": SCHEDULED_TIMING_FACTOR,
        "text_scope": "focus x all" if rc.compare_against_all else "focus",
    }
    params.update(derived)
    return _Ctx(
        rc=rc, df=df, ids=ids, focus=focus,
        names=names if names is not None else _blanks(n),
        college=college,
        college_key=np.array([_norm_key(v) for v in college], dtype=object),
        test_mode=test_mode,
        on_campus=np.array([_is_on_campus(v) for v in test_mode], dtype=bool),
        ip=ip, subnet=subnet, phone_key=phone_key, email_key=email_key,
        start=start, submit=submit, duration=duration,
        sim_best=sim_best, sim_q=sim_q, match_pos=match_pos,
        resumes=resumes, population=population,
        pw=_weights(rc.pair_weights, _DEFAULT_RC.pair_weights),
        iw=_weights(rc.individual_weights, _DEFAULT_RC.individual_weights),
        warnings=warnings, params=dict(derived),
        dup_group=_duplicate_groups(df, ids),
    )


def _identity_keys(df: pd.DataFrame, warnings: list[str]) -> tuple[np.ndarray | None, np.ndarray | None]:
    """Canonical phone / email keys; values shared by too many candidates are placeholders."""
    phones = _str_col(df, S.PHONE)
    phone_key = None if phones is None else np.array([_phone_key(v) for v in phones], dtype=object)
    emails = _str_col(df, S.EMAIL)
    email_key = None if emails is None else np.array([_email_key(v) for v in emails], dtype=object)
    for label, keys in (("phone number", phone_key), ("email address", email_key)):
        if keys is None:
            continue
        counts = _group_counts(keys)
        crowded = counts > IDENTITY_MAX_GROUP
        if crowded.any():
            n_values = len({keys[i] for i in np.flatnonzero(crowded)})
            warnings.append(f"{n_values} {label}(s) shared by more than {IDENTITY_MAX_GROUP} candidates "
                            "ignored as placeholders.")
            keys[crowded] = None
    flags = _str_col(df, S.DATA_FLAGS)
    if email_key is None and flags is not None:
        n_flagged = sum(1 for f in flags if f and SHARED_EMAIL_FLAG in f)
        if n_flagged:
            warnings.append(f"{n_flagged} candidate(s) carry the '{SHARED_EMAIL_FLAG}' data flag but no email "
                            "column is available to pair them.")
    return phone_key, email_key


def _timing_arrays(df: pd.DataFrame) -> tuple[np.ndarray | None, np.ndarray | None, np.ndarray | None]:
    """Start / submit (epoch seconds) and duration (minutes), each filled from the other two."""
    start = _seconds_col(df, S.TEST_START)
    submit = _seconds_col(df, S.SUBMISSION_TIME)
    duration = _num_col(df, S.COMPLETION_MIN)
    if start is None and submit is None and duration is None:
        return None, None, None
    n = len(df)
    start = start if start is not None else np.full(n, np.nan)
    submit = submit if submit is not None else np.full(n, np.nan)
    duration = duration if duration is not None else np.full(n, np.nan)
    calc = (submit - start) / 60.0
    duration = np.where(np.isnan(duration) & (calc > 0), calc, duration)
    duration = np.where(duration > 0, duration, np.nan)
    submit = np.where(np.isnan(submit), start + duration * 60.0, submit)
    start = np.where(np.isnan(start), submit - duration * 60.0, start)
    return (start if (~np.isnan(start)).any() else None,
            submit if (~np.isnan(submit)).any() else None,
            duration if (~np.isnan(duration)).any() else None)


def _code_similarity(df: pd.DataFrame, n: int) -> tuple[np.ndarray, np.ndarray]:
    """Best HackerRank code similarity (0..100) per row and the question it came from."""
    cols = ([S.MAX_CODE_SIMILARITY] if S.MAX_CODE_SIMILARITY in df.columns else []) + \
        S.question_similarity_columns(df.columns)
    if not cols:
        return np.full(n, np.nan), _blanks(n)
    mat = np.full((n, len(cols)), np.nan)
    for j, col in enumerate(cols):
        v = _num_col(df, col)
        if v is None or np.isnan(v).all():
            continue
        if 0 < np.nanmax(v) <= 1.0:          # fractions -> percent
            v = v * 100.0
        mat[:, j] = v
    best = np.fmax.reduce(mat, axis=1)
    arg = np.where(np.isnan(mat), -np.inf, mat).argmax(axis=1)
    labels = np.array(["" if c == S.MAX_CODE_SIMILARITY else c.split("_", 1)[0] for c in cols], dtype=object)
    return best, np.where(np.isnan(best), "", labels[arg]).astype(object)


def _resolve_matches(df: pd.DataFrame, ids: np.ndarray) -> np.ndarray | None:
    """Position of HackerRank's most-similar candidate (plagiarism_match[_id]) or -1."""
    cols = [c for c in (PLAGIARISM_MATCH_ID, S.PLAGIARISM_MATCH) if c in df.columns]
    if not cols:
        return None
    id_pos = {str(cid).upper(): i for i, cid in enumerate(ids)}
    email_pos: dict[str, int] = {}
    emails = _str_col(df, S.EMAIL)
    if emails is not None:
        counts = Counter(e.casefold() for e in emails if e)
        email_pos = {e.casefold(): i for i, e in enumerate(emails) if e and counts[e.casefold()] == 1}
    out = np.full(len(ids), -1, dtype=np.int64)
    columns = [_str_col(df, c) for c in cols]
    for i in range(len(ids)):
        for vals in columns:
            t = vals[i] if vals is not None else None
            if not t:
                continue
            p = id_pos.get(t.upper())
            if p is None and "@" in t:
                p = email_pos.get(t.casefold())
            if p is not None and p != i:
                out[i] = p
                break
    return out


def _resume_inputs(ids: np.ndarray, parsed_by_candidate: Any, parsed_all: Any
                   ) -> tuple[dict[int, ParsedResume], list[ParsedResume]]:
    lookup: dict[str, ParsedResume] = {}
    if isinstance(parsed_by_candidate, dict):
        for k, p in parsed_by_candidate.items():
            if isinstance(p, ParsedResume):
                lookup[str(k).strip().upper()] = p
    resumes = {i: lookup[str(cid).upper()] for i, cid in enumerate(ids) if str(cid).upper() in lookup}
    pool: dict[str, ParsedResume] = {}
    sources = list(parsed_all.values()) if isinstance(parsed_all, dict) else []
    sources += [lookup[k] for k in sorted(lookup)]
    for j, p in enumerate(sources):
        if isinstance(p, ParsedResume):
            pool.setdefault(p.file_hash or p.file_name or f"#{j}", p)
    return resumes, [pool[k] for k in sorted(pool)]


# ===========================================================================
# 1. Commonality (lift)
# ===========================================================================
def _commonality(ctx: _Ctx) -> pd.DataFrame:
    if ctx.n_focus == 0:
        return _empty(COMMONALITY_COLUMNS)
    rc = ctx.rc
    frames: list[pd.DataFrame] = []
    for col, label in S.COMMONALITY_ATTRIBUTES.items():
        vals = _attribute_values(ctx, col)
        if vals is None:
            continue
        keys = np.array([_norm_key(v) for v in vals], dtype=object)
        known = np.array([k is not None for k in keys], dtype=bool)
        focus_total = int((known & ctx.focus).sum())
        if focus_total == 0:
            continue
        f = pd.DataFrame({"key": keys[known], "value": vals[known], "focus": ctx.focus[known]})
        stats = f.groupby("key", sort=True).agg(population_count=("focus", "size"), focus_count=("focus", "sum"))
        stats = stats[stats["focus_count"] > 0]
        display = (f.groupby(["key", "value"]).size().rename("c").reset_index()
                   .sort_values(["key", "c", "value"], ascending=[True, False, True])
                   .drop_duplicates("key").set_index("key")["value"])
        frames.append(pd.DataFrame({
            "attribute": label,
            "value": display.reindex(stats.index).to_numpy(dtype=object),
            "focus_count": stats["focus_count"].to_numpy(dtype=np.int64),
            "focus_total": focus_total,
            "population_count": stats["population_count"].to_numpy(dtype=np.int64),
            "population_total": int(known.sum()),
            "attribute_key": col,
        }))
    if not frames:
        return _empty(COMMONALITY_COLUMNS)
    out = pd.concat(frames, ignore_index=True)
    fc, ft = out["focus_count"].to_numpy(float), out["focus_total"].to_numpy(float)
    pc, pt = out["population_count"].to_numpy(float), out["population_total"].to_numpy(float)
    focus_share, pop_share = fc / ft, pc / pt
    lift = focus_share / pop_share
    # One-sided Fisher exact test (alternative="greater") == upper hypergeometric tail.
    p = np.clip(hypergeom.sf(fc - 1, pt, pc, ft), 0.0, 1.0)
    q = false_discovery_control(p, method="bh")
    out = out.assign(
        focus_share=np.round(focus_share, 4), population_share=np.round(pop_share, 4),
        lift=np.round(lift, 3), p_value=p, q_value=q,
        significant=(fc >= rc.lift_min_count) & (lift >= rc.lift_min) & (q <= rc.lift_alpha),
    )
    out = out.sort_values(["significant", "lift", "q_value", "focus_count", "attribute", "value"],
                          ascending=[False, False, True, False, True, True], kind="mergesort")
    return out[COMMONALITY_COLUMNS].reset_index(drop=True)


def _attribute_values(ctx: _Ctx, col: str) -> np.ndarray | None:
    df = ctx.df
    if col == S.COLLEGE:
        return ctx.college if S.COLLEGE in df.columns else None
    if col == S.IP_ADDRESS:
        return ctx.ip
    if col == S.IP_SUBNET:
        return ctx.subnet
    if col == S.EMAIL_DOMAIN and col not in df.columns:
        emails = _str_col(df, S.EMAIL)
        if emails is None:
            return None
        return np.array([e.rsplit("@", 1)[1].casefold() if e and "@" in e else None for e in emails], dtype=object)
    if col == S.TEST_SLOT and col not in df.columns:
        if ctx.start is None:
            return None
        slots = pd.to_datetime(pd.Series(ctx.start), unit="s", errors="coerce").dt.floor("h")
        return _str_values(slots.dt.strftime("%Y-%m-%d %H:00"))
    return _str_col(df, col)


# ===========================================================================
# 3. Individual flags
# ===========================================================================
def _individual_flags(ctx: _Ctx) -> pd.DataFrame:
    rc, df, n = ctx.rc, ctx.df, ctx.n
    rows: list[tuple[int, str, str]] = []
    absent: list[str] = []

    # Speed anomaly: focus members far faster than the focus-group median.
    if ctx.n_focus:
        fd = ctx.duration[ctx.focus] if ctx.duration is not None else np.empty(0)
        fd = fd[~np.isnan(fd)]
        if len(fd) < MIN_SPEED_SAMPLE:
            ctx.warnings.append(f"Speed anomaly skipped: fewer than {MIN_SPEED_SAMPLE} focus-group "
                                "completion times.")
        else:
            med = float(np.median(fd))
            ctx.params["focus_median_minutes"] = round(med, 2)
            for i in np.flatnonzero(ctx.focus & (ctx.duration < rc.speed_anomaly_ratio * med)):
                rows.append((i, "speed_anomaly",
                             f"Completed in {ctx.duration[i]:.1f} min vs focus-group median {med:.1f} min"))

    plag = _bool_col(df, S.PLAGIARISM_FLAG)
    if plag is None:
        absent.append(S.PLAGIARISM_FLAG)
    else:
        rows += [(i, "plagiarism_flag", "HackerRank plagiarism status: flagged") for i in np.flatnonzero(plag)]

    if np.isnan(ctx.sim_best).all():
        absent.append(S.MAX_CODE_SIMILARITY)
    else:
        hit = np.nan_to_num(ctx.sim_best, nan=-1.0) >= rc.code_similarity_threshold
        for i in np.flatnonzero(hit):
            where = f" on {ctx.sim_q[i]}" if ctx.sim_q[i] else ""
            rows.append((i, "high_code_similarity",
                         f"HackerRank max code similarity {ctx.sim_best[i]:.0f}%{where}"))

    status = _str_col(df, S.INTEGRITY_STATUS)
    if status is None:
        absent.append(S.INTEGRITY_STATUS)
    else:
        for i, t in enumerate(status):
            if t and _is_integrity_concern(t):
                severity = _WORD_RE.findall(t.casefold())[:1] in (["medium"], ["high"])
                label = "Assessment integrity issues" if severity else "HackerRank AI-proctoring status"
                rows.append((i, "integrity_status", f"{label}: {_clip(t, 60)}"))

    # Hidden or adversarial resume text found while the resume was parsed: one flag per resume, for
    # its most serious kind (prompt injection > hidden text > invisible characters).
    for i, p in sorted(ctx.resumes.items()):
        findings = _injection_findings(p)
        flag = injection.flag_kind(findings)
        if flag:
            rows.append((i, flag, f"{injection.FLAG_REASONS[flag]}: '{injection.reason_snippet(findings)}'"))

    notes = _str_col(df, S.PROCTORING_FLAGS)
    if notes is None:
        absent.append(S.PROCTORING_FLAGS)
    else:
        for i, t in enumerate(notes):
            if t and re.sub(r"[^a-z0-9]", "", t.casefold()) not in _NO_FLAG_TOKENS:
                rows.append((i, "proctoring_flags", f"Proctoring notes: {_clip(t, 120)}"))

    numeric_flags = (
        (S.OUT_OF_WINDOW_SEC, "out_of_window_time", rc.out_of_window_sec_threshold,
         lambda v: f"{v / 60:.1f} min outside the test window ({v:.0f} s)"),
        (S.SECURE_MODE_EXITS, "secure_mode_exits", rc.secure_mode_exit_threshold,
         lambda v: f"{v:.0f} secure-mode exit(s)"),
        (S.TAB_SWITCHES, "tab_switches", rc.tab_switch_threshold,
         lambda v: f"{v:.0f} tab switches / out-of-window events"),
        (S.COPY_PASTE, "copy_paste", rc.copy_paste_threshold,
         lambda v: f"{v:.0f} editor paste events"),
    )
    for col, key, threshold, describe in numeric_flags:
        vals = _num_col(df, col)
        if vals is None:
            absent.append(col)
            continue
        v = np.nan_to_num(vals, nan=0.0)
        rows += [(i, key, describe(v[i])) for i in np.flatnonzero((v >= threshold) & (v > 0))]

    if absent:
        ctx.warnings.append("Individual flags not evaluated (columns absent): " + ", ".join(absent) + ".")
    rows = [r for r in rows if ctx.iw.get(r[1], 0.0) > 0]
    if not rows:
        return _empty(INDIVIDUAL_COLUMNS)
    out = pd.DataFrame({
        S.CANDIDATE_ID: [str(ctx.ids[i]) for i, _, _ in rows],
        "flag_key": [k for _, k, _ in rows],
        "flag": [FLAG_LABELS[k] for _, k, _ in rows],
        "detail": [d for _, _, d in rows],
        "weight": [float(ctx.iw[k]) for _, k, _ in rows],
    })
    return out.sort_values([S.CANDIDATE_ID, "weight", "flag_key"], ascending=[True, False, True],
                           kind="mergesort").reset_index(drop=True)


# ===========================================================================
# 2. Pair signals
# ===========================================================================
@dataclass
class _Gen:
    """Candidate pairs from one generator: positions a < b plus aligned evidence arrays."""

    a: np.ndarray
    b: np.ndarray
    ev: dict[str, np.ndarray] = field(default_factory=dict)

    @staticmethod
    def empty() -> "_Gen":
        return _Gen(np.empty(0, np.int64), np.empty(0, np.int64))


@dataclass
class _Pairs:
    """Kept pairs: positions, scores, per-signal weights, evidence and the public frame."""

    a: np.ndarray
    b: np.ndarray
    score: np.ndarray
    weights: dict[str, np.ndarray]
    ev: dict[str, np.ndarray]
    frame: pd.DataFrame

    @staticmethod
    def empty() -> "_Pairs":
        z = np.empty(0, np.int64)
        return _Pairs(z, z, np.empty(0), {}, {}, _empty(PAIR_COLUMNS))


@dataclass
class _ResumeFeatures:
    repos: frozenset[str]
    bullets: dict[str, str]     # normalised bullet -> original text


@dataclass
class _Timing:
    """Session-density context for the timing signals."""

    crowd_start: np.ndarray     # other focus members in the start window NOT co-moving with the row
    crowd_submit: np.ndarray    # same for the submission window
    comove: Any                 # csr (n x n): focus members starting AND submitting within the window


def _pair_analysis(ctx: _Ctx) -> _Pairs:
    if ctx.n_focus == 0:
        return _Pairs.empty()
    w = ctx.warnings
    gens: list[_Gen] = []

    def run(label: str, fn: Callable[[], _Gen | None]) -> None:
        g = _guard(label, fn, None, w)
        if g is not None and len(g.a):
            gens.append(g)

    run("Answer-hash signal", lambda: _gen_hashes(ctx))
    if ctx.ip is None:
        w.append("IP signals skipped: no ip_address column.")
    else:
        run("Same-IP signal", lambda: _gen_same_ip(ctx))
    if ctx.subnet is not None:
        run("Same-subnet signal", lambda: _gen_subnet(ctx))
    if ctx.phone_key is None:
        w.append("Same-phone signal skipped: no phone column.")
    else:
        run("Same-phone signal", lambda: _gen_groups(ctx, ctx.phone_key, both_focus=False))
    if ctx.email_key is None:
        w.append("Same-email signal skipped: no email column.")
    else:
        run("Same-email signal", lambda: _gen_groups(ctx, ctx.email_key, both_focus=False))
    if ctx.match_pos is None:
        w.append("HackerRank plagiarism-match signal skipped: no plagiarism_match column.")
    else:
        run("Plagiarism-match signal", lambda: _gen_plagiarism(ctx))
    if ctx.start is None and ctx.submit is None:
        w.append("Timing signals skipped: no test_start_time / submission_time.")
    else:
        ctx.timing = _guard("Timing preparation", lambda: _timing_info(ctx), None, w)
        if ctx.timing is not None:
            run("Timing signals", lambda: _gen_timing(ctx))

    scope = sorted(i for i in ctx.resumes if ctx.rc.compare_against_all or ctx.focus[i])
    if not any(ctx.focus[i] for i in scope):
        w.append("Resume signals skipped: no parsed resumes linked to focus-group candidates.")
    else:
        feats = _guard("Resume feature extraction", lambda: _resume_features(ctx), None, w)
        if feats is not None:
            run("Shared GitHub repo signal", lambda: _gen_github(ctx, scope, feats))
            run("Shared bullet signal", lambda: _gen_bullets(ctx, scope, feats))
        run("Resume text similarity", lambda: _gen_text(ctx, scope))

    if not gens:
        return _Pairs.empty()
    return _score_pairs(ctx, gens)


def _gen_hashes(ctx: _Ctx) -> _Gen | None:
    cols = S.answer_hash_columns(ctx.df.columns)
    if not cols:
        ctx.warnings.append("Answer-hash signal skipped: no q{n}_hash columns.")
        return None
    fpos = np.flatnonzero(ctx.focus)
    nf = len(fpos)
    common_max = max(ctx.rc.common_hash_max_share * nf, min(COMMON_HASH_MIN_HOLDERS, nf / 2))
    ctx.params["common_hash_max_holders"] = round(float(common_max), 2)
    pos_parts: list[np.ndarray] = []
    key_parts: list[np.ndarray] = []
    n_common = 0
    for col in cols:
        q = col.split("_", 1)[0]
        vals = np.array([_hash_key(v) for v in ctx.df[col].to_numpy(dtype=object)[fpos]], dtype=object)
        counts = Counter(v for v in vals if v is not None)
        common = {h for h, c in counts.items() if c > common_max}
        n_common += len(common)
        sel = np.array([v is not None and v not in common for v in vals], dtype=bool)
        if sel.any():
            pos_parts.append(fpos[sel])
            key_parts.append(np.array([f"{q}:{v}" for v in vals[sel]], dtype=object))
    ctx.params["common_hashes_ignored"] = n_common
    if not pos_parts:
        return _Gen.empty()
    m = _self_join(np.concatenate(pos_parts), np.concatenate(key_parts))
    if m.empty:
        return _Gen.empty()
    q = m["key"].str.split(":", n=1).str[0]
    m = m.assign(q=q, qn=q.str[1:].astype(int)).sort_values(["a", "b", "qn"], kind="mergesort")
    agg = m.groupby(["a", "b"], sort=True).agg(hash_qs=("q", ", ".join), n_hash=("q", "size")).reset_index()
    return _Gen(agg["a"].to_numpy(np.int64), agg["b"].to_numpy(np.int64),
                {"hash_qs": agg["hash_qs"].to_numpy(dtype=object), "n_hash": agg["n_hash"].to_numpy(np.int64)})


def _gen_groups(ctx: _Ctx, keys: np.ndarray, *, both_focus: bool) -> _Gen:
    """Pairs sharing a key value: both in focus, or at least one in focus (phone/email)."""
    known = _known(keys)
    pos = np.flatnonzero(known & ctx.focus) if both_focus else np.flatnonzero(known)
    m = _self_join(pos, keys[pos], anchor=None if both_focus else ctx.focus)
    return _unique_gen(m["a"].to_numpy(np.int64), m["b"].to_numpy(np.int64))


def _gen_same_ip(ctx: _Ctx) -> _Gen:
    """Focus pairs on one IP; IPs shared by very many perfect scorers are infrastructure."""
    infra = _ip_focus_counts(ctx) > IP_INFRA_MAX_FOCUS
    if infra.any():
        n_ips = len({ctx.ip[i] for i in np.flatnonzero(infra)})
        ctx.warnings.append(f"{n_ips} IP address(es) shared by more than {IP_INFRA_MAX_FOCUS} perfect scorers "
                            "treated as shared infrastructure (venue / VPN / proxy server), not pair evidence.")
    keys = np.where(infra, None, ctx.ip)
    return _gen_groups(ctx, keys, both_focus=True)


def _ip_focus_counts(ctx: _Ctx) -> np.ndarray:
    """Number of focus members sharing each row's IP (0 where the IP is missing)."""
    if ctx.ip is None:
        return np.zeros(ctx.n)
    counts = Counter(ctx.ip[i] for i in np.flatnonzero(ctx.focus) if ctx.ip[i] is not None)
    return np.array([counts.get(v, 0) if v is not None else 0 for v in ctx.ip], dtype=float)


def _gen_subnet(ctx: _Ctx) -> _Gen:
    """Focus pairs on a small /24 (<= shared_ip_max_group candidates overall)."""
    small = _group_counts(ctx.subnet) <= ctx.rc.shared_ip_max_group
    pos = np.flatnonzero(ctx.focus & small)
    m = _self_join(pos, ctx.subnet[pos])
    return _unique_gen(m["a"].to_numpy(np.int64), m["b"].to_numpy(np.int64))


def _gen_plagiarism(ctx: _Ctx) -> _Gen:
    mp = ctx.match_pos
    src = np.flatnonzero(mp >= 0)
    dst = mp[src]
    keep = ctx.focus[src] | ctx.focus[dst]
    src, dst = src[keep], dst[keep]
    if not len(src):
        return _Gen.empty()
    f = pd.DataFrame({"a": np.minimum(src, dst), "b": np.maximum(src, dst), "sim": ctx.sim_best[src]})
    agg = f.groupby(["a", "b"], sort=True)["sim"].max().reset_index()
    return _Gen(agg["a"].to_numpy(np.int64), agg["b"].to_numpy(np.int64),
                {"plag": np.ones(len(agg), dtype=bool), "plag_sim": agg["sim"].to_numpy(float)})


def _timing_info(ctx: _Ctx) -> _Timing:
    """Per-candidate crowding and the co-moving graph (sorted windows, no O(n^2) loop).

    Two focus members *co-move* when both their starts and their submissions fall within
    sync_window_sec. Crowding counts the other focus members sharing a start (or submission)
    window *without* co-moving: many of them means a scheduled session or deadline rush, where
    synchronised timestamps are expected, while a genuine synchronised group co-moves.
    """
    n, w = ctx.n, float(ctx.rc.sync_window_sec)
    s = ctx.start if ctx.start is not None else np.full(n, np.nan)
    u = ctx.submit if ctx.submit is not None else np.full(n, np.nan)
    rows = cols = np.empty(0, np.int64)
    ref = np.flatnonzero(ctx.focus & ~np.isnan(s) & ~np.isnan(u))
    cand = np.flatnonzero(~np.isnan(s) & ~np.isnan(u))
    if len(ref) and len(cand):
        order = ref[np.argsort(s[ref], kind="stable")]
        rs = s[order]
        lo = np.searchsorted(rs, s[cand] - w, side="left")
        cnt = np.searchsorted(rs, s[cand] + w, side="right") - lo
        cnt = np.where(cnt <= TIMING_EXPAND_CAP, cnt, 0)   # beyond the cap: crowded anyway
        total = int(cnt.sum())
        i = np.repeat(cand, cnt)
        j = order[np.repeat(lo, cnt) + np.arange(total) - np.repeat(np.cumsum(cnt) - cnt, cnt)]
        hit = (i != j) & (np.abs(u[j] - u[i]) <= w)
        rows, cols = i[hit], j[hit]
    both = np.bincount(rows, minlength=n)
    comove = sparse.csr_matrix((np.ones(len(rows), np.float32), (rows, cols)), shape=(n, n))
    return _Timing(_window_counts(s, ctx.focus, w) - both, _window_counts(u, ctx.focus, w) - both, comove)


def _window_counts(values: np.ndarray, ref_mask: np.ndarray, window: float) -> np.ndarray:
    """Number of *other* reference rows whose value lies within +/- window of each row."""
    ok = ~np.isnan(values)
    is_ref = ref_mask & ok
    ref = np.sort(values[is_ref])
    out = np.zeros(len(values), dtype=np.int64)
    v = values[ok]
    out[ok] = (np.searchsorted(ref, v + window, side="right") - np.searchsorted(ref, v - window, side="left")
               - is_ref[ok])
    return out


def _gen_timing(ctx: _Ctx) -> _Gen:
    """Focus pairs with synchronised start or submission, enumerated only outside crowds."""
    n, tm = ctx.n, ctx.timing
    keys: list[np.ndarray] = []
    for arr, crowd in ((ctx.start, tm.crowd_start), (ctx.submit, tm.crowd_submit)):
        if arr is None:
            continue
        pos = np.flatnonzero(ctx.focus & ~np.isnan(arr))
        if len(pos) < 2:
            continue
        a, b = _window_pairs(arr[pos], pos, float(ctx.rc.sync_window_sec), crowd[pos] <= TIMING_CROWD_MAX)
        keys.append(np.minimum(a, b) * n + np.maximum(a, b))
    if not keys:
        return _Gen.empty()
    k = np.unique(np.concatenate(keys))
    return _Gen(k // n, k % n)


def _window_pairs(t: np.ndarray, pos: np.ndarray, window: float, eligible: np.ndarray
                  ) -> tuple[np.ndarray, np.ndarray]:
    """All pairs of eligible members whose times differ by <= window (sorted-window expansion)."""
    order = np.argsort(t, kind="stable")
    ts, ps, el = t[order], pos[order], eligible[order]
    idx = np.arange(len(ts))
    hi = np.searchsorted(ts, ts + window, side="right")
    cnt = np.where(el, hi - idx - 1, 0)
    total = int(cnt.sum())
    if total == 0:
        return np.empty(0, np.int64), np.empty(0, np.int64)
    i = np.repeat(idx, cnt)
    j = i + 1 + (np.arange(total) - np.repeat(np.cumsum(cnt) - cnt, cnt))
    ok = el[j]
    return ps[i[ok]].astype(np.int64), ps[j[ok]].astype(np.int64)


def _resume_features(ctx: _Ctx) -> dict[int, _ResumeFeatures]:
    """Repo / rare-bullet features per linked resume; rarity counted over the population."""
    min_words = int(ctx.rc.min_bullet_words)
    cache: dict[int, _ResumeFeatures] = {}

    def features(p: ParsedResume) -> _ResumeFeatures:
        key = id(p)
        if key not in cache:
            cache[key] = _ResumeFeatures(_resume_repos(p), _resume_bullets(p, min_words))
        return cache[key]

    bullet_counts: Counter[str] = Counter()
    repo_counts: Counter[str] = Counter()
    display: dict[str, str] = {}
    for p in ctx.population:
        f = features(p)
        bullet_counts.update(f.bullets.keys())
        repo_counts.update(f.repos)
        for k, t in f.bullets.items():
            display[k] = min(display.get(k, t), t)
    rare_max = ctx.params["rare_max_holders"]
    ctx.params["boilerplate_bullets_ignored"] = sum(1 for c in bullet_counts.values() if c > rare_max)
    ctx.params["popular_repos_ignored"] = sum(1 for c in repo_counts.values() if c > rare_max)
    out: dict[int, _ResumeFeatures] = {}
    for i, p in ctx.resumes.items():
        f = features(p)
        out[i] = _ResumeFeatures(
            frozenset(r for r in f.repos if repo_counts.get(r, 1) <= rare_max),
            {k: display.get(k, t) for k, t in f.bullets.items() if bullet_counts.get(k, 1) <= rare_max},
        )
    ctx.bullet_counts = dict(bullet_counts)
    return out


def _gen_github(ctx: _Ctx, scope: list[int], feats: dict[int, _ResumeFeatures]) -> _Gen:
    pos = [i for i in scope for _ in feats[i].repos]
    keys = [r for i in scope for r in sorted(feats[i].repos)]
    m = _self_join(np.array(pos, np.int64), np.array(keys, dtype=object), anchor=ctx.focus)
    if m.empty:
        return _Gen.empty()
    agg = (m.sort_values(["a", "b", "key"], kind="mergesort")
           .groupby(["a", "b"], sort=True).agg(repos=("key", ", ".join)).reset_index())
    return _Gen(agg["a"].to_numpy(np.int64), agg["b"].to_numpy(np.int64),
                {"repos": agg["repos"].to_numpy(dtype=object)})


def _gen_bullets(ctx: _Ctx, scope: list[int], feats: dict[int, _ResumeFeatures]) -> _Gen:
    pos = [i for i in scope for _ in feats[i].bullets]
    keys = [k for i in scope for k in feats[i].bullets]
    m = _self_join(np.array(pos, np.int64), np.array(keys, dtype=object), anchor=ctx.focus)
    if m.empty:
        return _Gen.empty()
    counts = ctx.bullet_counts
    m = m.assign(pop=m["key"].map(lambda k: counts.get(k, 1)))
    m = m.sort_values(["a", "b", "pop", "key"], kind="mergesort")
    agg = m.groupby(["a", "b"], sort=True).agg(n=("key", "size"), keys=("key", list)).reset_index()
    text_of: dict[str, str] = {}
    for i in scope:
        text_of.update(feats[i].bullets)
    examples = [S.SEP_REASONS.join(_clip(text_of[k], 100) for k in ks[:MAX_BULLET_EXAMPLES])
                for ks in agg["keys"]]
    return _Gen(agg["a"].to_numpy(np.int64), agg["b"].to_numpy(np.int64),
                {"n_bullets": agg["n"].to_numpy(np.int64), "bullet_examples": np.array(examples, dtype=object)})


def _gen_text(ctx: _Ctx, scope: list[int]) -> _Gen:
    """TF-IDF (1-2 grams, sublinear tf) on projects+experience; focus rows x scope columns."""
    docs = {i: _resume_doc(ctx.resumes[i]) for i in scope}
    rows = [i for i in scope if docs[i].strip()]
    if len(rows) < 2 or not any(ctx.focus[i] for i in rows):
        return _Gen.empty()
    vec = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, stop_words="english",
                          max_df=0.85 if len(rows) >= 20 else 1.0, dtype=np.float32)
    try:
        X = vec.fit_transform([docs[i] for i in rows]).tocsr()
    except ValueError:                      # empty vocabulary after stop words / pruning
        ctx.warnings.append("Resume text similarity skipped: no usable vocabulary in resume text.")
        return _Gen.empty()
    ctx.text_matrix = X
    ctx.text_rows = {p: r for r, p in enumerate(rows)}
    rows_arr = np.array(rows, dtype=np.int64)
    frows = np.flatnonzero(ctx.focus[rows_arr])
    sims = (X[frows] @ X.T).tocoo()
    hit = np.round(sims.data, 4) >= ctx.rc.text_similarity_medium
    pa, pb = rows_arr[frows[sims.row[hit]]], rows_arr[sims.col[hit]]
    keep = pa != pb
    return _unique_gen(np.minimum(pa, pb)[keep], np.maximum(pa, pb)[keep], ctx.n)


def _without_duplicate_applications(ctx: _Ctx, gens: list[_Gen]) -> list[_Gen]:
    """Drop pairs between applications of the same person (eligibility handles duplicates)."""
    if ctx.dup_group is None:
        return gens
    out: list[_Gen] = []
    dropped: set[tuple[int, int]] = set()
    for g in gens:
        keep = ctx.dup_group[g.a] != ctx.dup_group[g.b]
        dropped.update(zip(g.a[~keep].tolist(), g.b[~keep].tolist()))
        if keep.any():
            out.append(_Gen(g.a[keep], g.b[keep], {k: v[keep] for k, v in g.ev.items()}))
    ctx.params["duplicate_application_pairs_ignored"] = len(dropped)
    return out


def _score_pairs(ctx: _Ctx, gens: list[_Gen]) -> _Pairs:
    """Union all candidate pairs, evaluate every signal vectorised, keep the meaningful ones."""
    rc, pw, n = ctx.rc, ctx.pw, ctx.n
    gens = _without_duplicate_applications(ctx, gens)
    if not gens:
        return _Pairs.empty()
    gen_keys = [g.a * n + g.b for g in gens]
    union = np.unique(np.concatenate(gen_keys))
    A, B = union // n, union % n
    m = len(union)
    E: dict[str, np.ndarray] = {}
    for g, k in zip(gens, gen_keys):
        idx = np.searchsorted(union, k)
        for name, arr in g.ev.items():
            E.setdefault(name, _blank(arr, m))[idx] = arr
    for name, kind in (("hash_qs", ""), ("repos", ""), ("bullet_examples", ""), ("n_hash", 0),
                       ("n_bullets", 0), ("plag", False), ("plag_sim", np.nan)):
        if name not in E:
            E[name] = _blank(np.array([kind]), m)

    # --- facts -------------------------------------------------------------
    same_ip = _same(ctx.ip, A, B)
    same_subnet = _same(ctx.subnet, A, B)
    same_college = _same(ctx.college_key, A, B)
    same_phone = _same(ctx.phone_key, A, B)
    same_email = _same(ctx.email_key, A, B)
    both_on = ctx.on_campus[A] & ctx.on_campus[B]
    co_sched = both_on & (same_college | same_ip)       # scheduled On-campus session
    nan = np.full(m, np.nan)
    start_gap = np.abs(ctx.start[A] - ctx.start[B]) if ctx.start is not None else nan
    submit_gap = np.abs(ctx.submit[A] - ctx.submit[B]) if ctx.submit is not None else nan
    dur_gap = np.abs(ctx.duration[A] - ctx.duration[B]) if ctx.duration is not None else nan
    seq_gap = nan
    if ctx.start is not None and ctx.submit is not None:
        g1 = (ctx.start[B] - ctx.submit[A]) / 60.0
        g2 = (ctx.start[A] - ctx.submit[B]) / 60.0
        seq_gap = np.where(g1 >= 0, g1, np.where(g2 >= 0, g2, np.nan))
    sim = _exact_similarity(ctx, A, B)

    # --- weights -----------------------------------------------------------
    win, tol = float(rc.sync_window_sec), float(rc.duration_tolerance_min)
    tm = ctx.timing
    no = np.zeros(m, dtype=bool)
    crowd_start = np.maximum(tm.crowd_start[A], tm.crowd_start[B]) > TIMING_CROWD_MAX if tm else no
    crowd_submit = np.maximum(tm.crowd_submit[A], tm.crowd_submit[B]) > TIMING_CROWD_MAX if tm else no
    # Common co-movers: a third candidate synchronised with both (synchronised group >= 3).
    common = np.asarray(tm.comove[A].multiply(tm.comove[B]).sum(axis=1)).ravel() if tm else np.zeros(m)
    f_start = _timing_factor(co_sched, crowd_start)
    f_submit = _timing_factor(co_sched, crowd_submit)
    f_dur = _timing_factor(co_sched, no)
    ip_count = _group_counts(ctx.ip)[A] if ctx.ip is not None else np.zeros(m)
    subnet_count = _group_counts(ctx.subnet)[A] if ctx.subnet is not None else np.zeros(m)
    known_colleges = _known(ctx.college_key)
    diff_college = known_colleges[A] & known_colleges[B] & ~same_college
    small_ip = ip_count <= rc.shared_ip_max_group
    infra_ip = _ip_focus_counts(ctx)[A] > IP_INFRA_MAX_FOCUS
    # Lab / NAT / hostel IPs: evidence only across colleges and outside scheduled venues.
    ip_factor = np.where(same_ip & ~infra_ip & (small_ip | (diff_college & ~both_on)), 1.0, 0.0)
    sequential = same_ip & _within(seq_gap, rc.proxy_gap_max_min)
    seq_factor = ip_factor * np.where(co_sched, SCHEDULED_TIMING_FACTOR, 1.0)
    sim0 = np.nan_to_num(sim, nan=0.0)
    # Team project: 1-2 shared repos on otherwise different resumes (low text similarity, at most
    # one shared rare bullet) - teammates list the same repo; weighted down and worded as such.
    has_repo = E["repos"] != ""
    n_repos = np.array([len([r for r in str(v).split(S.SEP_LIST) if r]) for v in E["repos"]], dtype=np.int64)
    team = (has_repo & (n_repos <= int(rc.team_max_shared_repos))
            & (sim0 < float(rc.team_max_text_similarity)) & (E["n_bullets"] <= int(rc.team_max_shared_bullets)))
    W: dict[str, np.ndarray] = {
        "shared_answer_hash": pw["shared_answer_hash"] * np.minimum(E["n_hash"], 2),
        "plagiarism_match": np.where(E["plag"], pw["plagiarism_match"], 0.0),
        "same_phone": np.where(same_phone, pw["same_phone"], 0.0),
        "same_email": np.where(same_email, pw["same_email"], 0.0),
        "shared_github_repo": np.where(has_repo & ~team, pw["shared_github_repo"], 0.0),
        "team_github_repo": np.where(team, max(0.0, float(rc.team_repo_weight)), 0.0),
        "resume_text_high": np.where(sim0 >= rc.text_similarity_high, pw["resume_text_high"], 0.0),
        "resume_text_medium": np.where((sim0 >= rc.text_similarity_medium) & (sim0 < rc.text_similarity_high),
                                       pw["resume_text_medium"], 0.0),
        "shared_bullets": pw["shared_bullets"] * np.minimum(E["n_bullets"], 3),
        "sequential_same_ip": np.where(sequential, pw["sequential_same_ip"] * seq_factor, 0.0),
        "same_ip": pw["same_ip"] * ip_factor,
        "same_subnet": np.where(same_subnet & ~same_ip & (subnet_count <= rc.shared_ip_max_group),
                                pw["same_subnet"], 0.0),
        "sync_submission": np.where(_within(submit_gap, win), pw["sync_submission"] * f_submit, 0.0),
        "sync_start": np.where(_within(start_gap, win), pw["sync_start"] * f_start, 0.0),
        "duration_match": np.where(_within(dur_gap, tol), pw["duration_match"] * f_dur, 0.0),
    }
    W = {k: np.asarray(v, dtype=float) for k, v in W.items()}
    score = np.round(np.minimum(100.0, np.sum(list(W.values()), axis=0)), 2)
    non_timing = np.any([W[k] > 0 for k in W if k not in TIMING_SIGNALS], axis=0)
    sync_group = common >= MIN_SYNC_GROUP - 2
    keep = (score > 0) & (non_timing | ((score >= rc.edge_threshold) & sync_group))

    E.update({
        "sim": sim, "same_ip": same_ip, "same_subnet": same_subnet, "same_college": same_college,
        "same_phone": same_phone, "same_email": same_email, "start_gap": start_gap,
        "submit_gap": submit_gap, "dur_gap": dur_gap, "ip_count": ip_count,
        # Only where the back-to-back same-IP pattern counted (consumers read it as that pattern).
        "seq_gap": np.where(W["sequential_same_ip"] > 0, seq_gap, np.nan),
        "f_start": f_start, "f_submit": f_submit, "f_dur": f_dur, "seq_factor": seq_factor,
    })
    E = {k: v[keep] for k, v in E.items()}
    W = {k: v[keep] for k, v in W.items()}
    A, B, score = A[keep], B[keep], score[keep]
    ip_val = ctx.ip[A] if ctx.ip is not None else _nones(len(A))
    subnet_val = ctx.subnet[A] if ctx.subnet is not None else _nones(len(A))
    E["ip"], E["subnet"] = ip_val, subnet_val

    signals, signal_keys = [], []
    for r in range(len(A)):
        active = sorted((k for k in W if W[k][r] > 0), key=lambda k: (-W[k][r], _SIGNAL_RANK[k]))
        signals.append(S.SEP_REASONS.join(_signal_text(k, r, E) for k in active))
        signal_keys.append(S.SEP_LIST.join(active))
    frame = pd.DataFrame({
        CAND_A: ctx.ids[A].astype(str), CAND_B: ctx.ids[B].astype(str), "pair_score": score,
        "signals": signals, "signal_keys": signal_keys,
        "shared_hashes": E["hash_qs"].astype(str),
        "resume_similarity": np.round(E["sim"], 4),
        "shared_bullets": E["n_bullets"].astype(np.int64),
        "shared_bullet_examples": E["bullet_examples"].astype(str),
        "shared_github": E["repos"].astype(str),
        "same_ip": E["same_ip"], "same_subnet": E["same_subnet"], "same_college": E["same_college"],
        "same_phone": E["same_phone"], "same_email": E["same_email"],
        "start_gap_sec": np.round(E["start_gap"], 1), "submit_gap_sec": np.round(E["submit_gap"], 1),
        "duration_gap_min": np.round(E["dur_gap"], 2), "sequential_gap_min": np.round(E["seq_gap"], 1),
    })
    order = np.lexsort((B, A, -score))          # positions follow candidate_id order
    frame = frame.iloc[order].reset_index(drop=True)
    return _Pairs(A[order], B[order], score[order], {k: v[order] for k, v in W.items()},
                  {k: v[order] for k, v in E.items()}, frame[PAIR_COLUMNS])


def _timing_factor(co_sched: np.ndarray, crowded: np.ndarray) -> np.ndarray:
    return np.where(co_sched | crowded, SCHEDULED_TIMING_FACTOR, 1.0)


def _within(gap: np.ndarray, limit: float) -> np.ndarray:
    """gap <= limit, False where the gap is unknown."""
    return np.nan_to_num(gap, nan=np.inf) <= limit


def _exact_similarity(ctx: _Ctx, A: np.ndarray, B: np.ndarray) -> np.ndarray:
    sim = np.full(len(A), np.nan)
    if ctx.text_matrix is None or not len(A):
        return sim
    ra = np.array([ctx.text_rows.get(int(i), -1) for i in A], dtype=np.int64)
    rb = np.array([ctx.text_rows.get(int(i), -1) for i in B], dtype=np.int64)
    ok = (ra >= 0) & (rb >= 0)
    if ok.any():
        X = ctx.text_matrix
        dots = np.asarray(X[ra[ok]].multiply(X[rb[ok]]).sum(axis=1)).ravel()
        sim[ok] = np.round(np.clip(dots, 0.0, 1.0), 4)
    return sim


def _signal_text(key: str, r: int, E: dict[str, np.ndarray]) -> str:
    """One pair-level signal, in words (no partner IDs: the pair row names both)."""
    down = " (scheduled/crowded session, down-weighted)"
    if key == "shared_answer_hash":
        return f"Identical answers to {E['hash_qs'][r]}"
    if key == "plagiarism_match":
        s = E["plag_sim"][r]
        if s == s:
            return f"HackerRank plagiarism engine: {s:.0f}% similar"
        return "HackerRank plagiarism engine: most similar submission"
    if key == "same_phone":
        return "Same phone number"
    if key == "same_email":
        return "Same email address"
    if key == "shared_github_repo":
        return f"Same GitHub repo {E['repos'][r]}"
    if key == "team_github_repo":
        return f"Same GitHub repo {E['repos'][r]} on otherwise different resumes (team project)"
    if key in ("resume_text_high", "resume_text_medium"):
        return f"Resume project/experience text {E['sim'][r] * 100:.0f}% similar (TF-IDF cosine)"
    if key == "shared_bullets":
        k = int(E["n_bullets"][r])
        return f"{k} shared rare resume bullet{'s' if k != 1 else ''}"
    if key == "sequential_same_ip":
        tail = down if E["seq_factor"][r] < 1 else ""
        return f"Back-to-back sessions on IP {E['ip'][r]} (gap {E['seq_gap'][r]:.0f} min){tail}"
    if key == "same_ip":
        cnt = int(np.nan_to_num(E["ip_count"][r]))
        return f"Same IP {E['ip'][r]}" + (f" (used by {cnt} candidates)" if cnt > 2 else "")
    if key == "same_subnet":
        return f"Same /24 subnet {E['subnet'][r]}"
    if key == "sync_start":
        return f"Starts {E['start_gap'][r]:.0f} s apart" + (down if E["f_start"][r] < 1 else "")
    if key == "sync_submission":
        return f"Submissions {E['submit_gap'][r]:.0f} s apart" + (down if E["f_submit"][r] < 1 else "")
    if key == "duration_match":
        return f"Durations {E['dur_gap'][r]:.1f} min apart" + (down if E["f_dur"][r] < 1 else "")
    return SIGNAL_LABELS.get(key, key)


# ===========================================================================
# 4 + 5. Clusters and candidate risk
# ===========================================================================
def _aggregate(ctx: _Ctx, pairs: _Pairs, individual: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    rc, n = ctx.rc, ctx.n
    edge = pairs.score >= rc.edge_threshold
    comp_of, comps = _components(pairs.a[edge], pairs.b[edge], pairs.score[edge], n)

    max_pair = np.zeros(n)
    n_links = np.zeros(n, dtype=np.int64)
    if len(pairs.a):
        np.maximum.at(max_pair, pairs.a, pairs.score)
        np.maximum.at(max_pair, pairs.b, pairs.score)
        np.add.at(n_links, pairs.a[edge], 1)
        np.add.at(n_links, pairs.b[edge], 1)

    pos_of = {str(c): i for i, c in enumerate(ctx.ids)}
    indiv = np.zeros(n)
    reasons: dict[int, list[tuple[float, int, str]]] = _pair_reasons(ctx, pairs)
    if not individual.empty:
        cols = [S.CANDIDATE_ID, "flag_key", "detail", "weight"]
        for cid, key, detail, weight in individual[cols].itertuples(index=False):
            i = pos_of.get(str(cid))
            if i is None:
                continue
            indiv[i] += float(weight)
            reasons.setdefault(i, []).append((float(weight), 100 + list(FLAG_LABELS).index(key), detail))
    indiv = np.round(np.minimum(indiv, 100.0), 2)
    risk = np.round(np.minimum(100.0, max_pair + indiv), 2)
    level = np.select([risk >= rc.high_threshold, risk >= rc.medium_threshold, risk > 0],
                      [S.RISK_HIGH, S.RISK_MEDIUM, S.RISK_LOW], default=S.RISK_CLEAR).astype(object)
    cluster_ids = np.array([comps[c]["id"] if c >= 0 else "" for c in comp_of], dtype=object)
    reason_text = [
        S.SEP_REASONS.join(t for _, _, t in sorted(reasons.get(i, []), key=lambda x: (-x[0], x[1], x[2])))
        for i in range(n)
    ]
    injected = [injection.summarize(_injection_findings(ctx.resumes[i])) if i in ctx.resumes else ""
                for i in range(n)]
    cr = pd.DataFrame({
        S.CANDIDATE_ID: ctx.ids.astype(str), S.IS_FOCUS: ctx.focus.astype(bool), S.RISK_SCORE: risk,
        S.RISK_LEVEL: level.astype(str), S.RISK_REASONS: reason_text, S.CLUSTER_ID: cluster_ids.astype(str),
        S.N_LINKS: n_links, S.MAX_PAIR_SCORE: np.round(max_pair, 2), S.INDIVIDUAL_SCORE: indiv,
        S.INJECTION_FLAGS: injected,
    })
    order = np.lexsort((np.arange(n), -risk))   # risk desc, then candidate_id asc
    cr = cr.iloc[order].reset_index(drop=True)[CANDIDATE_RISK_COLUMNS]
    clusters = _cluster_frame(ctx, pairs, edge, comp_of, comps, level, individual)
    return cr, clusters


def _components(a: np.ndarray, b: np.ndarray, score: np.ndarray, n: int
                ) -> tuple[np.ndarray, list[dict[str, Any]]]:
    """Connected components ordered by max pair score desc, size desc, first member; ids C01..."""
    comp_of = np.full(n, -1, dtype=np.int64)
    if not len(a):
        return comp_of, []
    g = nx.Graph()
    g.add_edges_from(zip(a.tolist(), b.tolist()))
    comps = [sorted(c) for c in nx.connected_components(g)]
    tmp = np.full(n, -1, dtype=np.int64)
    for k, members in enumerate(comps):
        tmp[members] = k
    best = np.zeros(len(comps))
    np.maximum.at(best, tmp[a], score)
    ranked = sorted(range(len(comps)), key=lambda k: (-best[k], -len(comps[k]), comps[k][0]))
    width = max(2, len(str(len(comps))))
    out: list[dict[str, Any]] = []
    for new, k in enumerate(ranked):
        comp_of[comps[k]] = new
        out.append({"id": f"C{new + 1:0{width}d}", "members": comps[k]})
    return comp_of, out


def _pair_reasons(ctx: _Ctx, pairs: _Pairs) -> dict[int, list[tuple[float, int, str]]]:
    """Aggregate pair signals per candidate and reason group, naming partner IDs."""
    E, W = pairs.ev, pairs.weights
    groups: dict[tuple[int, str, str], dict[int, dict[str, Any]]] = defaultdict(dict)
    sub_of = {"hash": "hash_qs", "repo": "repos", "team_repo": "repos", "ip": "ip", "subnet": "subnet"}
    for r in range(len(pairs.a)):
        a, b = int(pairs.a[r]), int(pairs.b[r])
        for key, wv in W.items():
            if wv[r] <= 0:
                continue
            group = SIGNALS[key].group
            sub = str(E[sub_of[group]][r]) if group in sub_of else ""
            for me, other in ((a, b), (b, a)):
                info = groups[(me, group, sub)].setdefault(other, {"w": 0.0, "keys": set(), "r": r})
                info["w"] += float(wv[r])
                info["keys"].add(key)
    out: dict[int, list[tuple[float, int, str]]] = defaultdict(list)
    for (me, group, sub), partners in groups.items():
        ranked = sorted(partners.items(), key=lambda kv: (-kv[1]["w"], str(ctx.ids[kv[0]])))
        weight = ranked[0][1]["w"]
        rank = min(_SIGNAL_RANK[k] for _, info in ranked for k in info["keys"])
        out[me].append((weight, rank, _format_reason(ctx, group, sub, ranked, E)))
    return out


def _format_reason(ctx: _Ctx, group: str, sub: str, ranked: list[tuple[int, dict[str, Any]]],
                   E: dict[str, np.ndarray]) -> str:
    ids = [str(ctx.ids[p]) for p, _ in ranked]
    plist = _id_list(ids)
    if group == "hash":
        return f"Identical answers to {sub} with {plist} - pattern consistent with shared solutions"
    if group == "repo":
        return f"Same GitHub repo {sub} as {plist} - pattern consistent with a shared or purchased project"
    if group == "team_repo":
        return (f"Shared team project GitHub repo {sub} with {plist} - pattern consistent with legitimate team "
                f"collaboration (verify candidate's individual role)")
    if group == "text":
        parts = [f"{ids[k]} ({E['sim'][info['r']] * 100:.0f}%)"
                 for k, (_, info) in enumerate(ranked[:MAX_IDS_IN_REASON])]
        more = f" (+{len(ids) - MAX_IDS_IN_REASON} more)" if len(ids) > MAX_IDS_IN_REASON else ""
        return ("Resume project/experience text similar to " + ", ".join(parts) + more
                + " - pattern consistent with a copied or templated resume")
    if group == "bullets":
        k = max(int(E["n_bullets"][info["r"]]) for _, info in ranked)
        example = str(E["bullet_examples"][ranked[0][1]["r"]]).split(S.SEP_REASONS)[0]
        return (f"{k} rare resume bullet{'s' if k != 1 else ''} shared with {plist} "
                f"(e.g. \"{_clip(example, 80)}\") - pattern consistent with a templated resume")
    if group == "ip":
        cnt = int(np.nan_to_num(E["ip_count"][ranked[0][1]["r"]]))
        text = f"Same IP {sub} as {plist}"
        if cnt > len(ids) + 1:
            text += f" ({cnt} candidates on this IP)"
        seq = [(str(ctx.ids[p]), float(E["seq_gap"][info["r"]])) for p, info in ranked
               if "sequential_same_ip" in info["keys"]]
        if seq:
            text += (f"; sessions back-to-back with {_id_list([s for s, _ in seq])} "
                     f"(gap {min(g for _, g in seq):.0f} min) - pattern consistent with a proxy test-taker")
        return text
    if group == "subnet":
        return f"Same /24 subnet {sub} as {plist}"
    if group == "timing":
        return _format_timing(ctx, ranked, E)
    if group == "phone":
        return f"Same phone number as {plist} - pattern consistent with identity overlap"
    if group == "email":
        return f"Same email address as {plist} - pattern consistent with identity overlap"
    if group == "plag":
        parts = []
        for pid, (_, info) in zip(ids, ranked):
            s = E["plag_sim"][info["r"]]
            parts.append(f"{s:.0f}% similar to {pid}" if s == s else f"most similar submission is {pid}")
        return "HackerRank plagiarism engine: " + "; ".join(parts[:MAX_IDS_IN_REASON])
    return f"{group} with {plist}"


def _format_timing(ctx: _Ctx, ranked: list[tuple[int, dict[str, Any]]], E: dict[str, np.ndarray]
                   ) -> str:
    """Synchronised sessions (start/submission) first; duration-only matches as a weak aside."""
    sync_keys = {"sync_start", "sync_submission"}
    synced = [(p, info) for p, info in ranked if info["keys"] & sync_keys]
    dur_only = [(p, info) for p, info in ranked if not info["keys"] & sync_keys]
    out: list[str] = []
    if synced:
        spec = {"sync_start": ("start_gap", "f_start", "starts", "s", ".0f"),
                "sync_submission": ("submit_gap", "f_submit", "submissions", "s", ".0f"),
                "duration_match": ("dur_gap", "f_dur", "durations", "min", ".1f")}
        parts, scheduled = [], True
        for key, (col, fcol, noun, unit, fmt) in spec.items():
            fired = [info["r"] for _, info in synced if key in info["keys"]]
            if not fired:
                continue
            gap = max(float(E[col][r]) for r in fired)
            scheduled &= key == "duration_match" or all(E[fcol][r] < 1 for r in fired)
            parts.append(f"{noun} {gap:{fmt}} {unit} apart" if len(synced) == 1
                         else f"{noun} within {gap:{fmt}} {unit}")
        tail = (" - expected in a scheduled or crowded session (down-weighted)" if scheduled
                else " - pattern consistent with coordinated test-taking")
        out.append(f"Synchronised test sessions with {_id_list([str(ctx.ids[p]) for p, _ in synced])} "
                   f"({', '.join(parts)}){tail}")
    if dur_only:
        gap = max(float(E["dur_gap"][info["r"]]) for _, info in dur_only)
        others = _id_list([str(ctx.ids[p]) for p, _ in dur_only])
        out.append(f"Completion time within {gap:.1f} min of {others}")
    return "; ".join(out)


def _cluster_frame(ctx: _Ctx, pairs: _Pairs, edge: np.ndarray, comp_of: np.ndarray,
                   comps: list[dict[str, Any]], level: np.ndarray, individual: pd.DataFrame
                   ) -> pd.DataFrame:
    if not comps:
        return _empty(CLUSTER_COLUMNS)
    rank_of = {lvl: i for i, lvl in enumerate(S.RISK_ORDER)}
    speed_ids: set[str] = set()
    if not individual.empty:
        speed_ids = set(individual.loc[individual["flag_key"] == "speed_anomaly", S.CANDIDATE_ID].astype(str))
    edge_comp, es = comp_of[pairs.a[edge]], pairs.score[edge]
    ekeys = pairs.frame["signal_keys"].to_numpy()[edge]
    ehash = pairs.ev["hash_qs"][edge]
    erepo = pairs.ev["repos"][edge]
    ebul = pairs.ev["n_bullets"][edge]
    rows = []
    for c, comp in enumerate(comps):
        mem = comp["members"]
        k = len(mem)
        sel = edge_comp == c
        scores = es[sel]
        key_counts: Counter[str] = Counter(s for ks in ekeys[sel] for s in ks.split(S.SEP_LIST) if s)
        top = sorted(key_counts, key=lambda s: (-key_counts[s], _SIGNAL_RANK.get(s, 99)))
        colleges = sorted({ctx.college_key[i]: str(ctx.college[i]) for i in mem if ctx.college_key[i]}.values())
        n_speed = sum(1 for i in mem if str(ctx.ids[i]) in speed_ids)
        start_span = _span_min(ctx.start, mem)
        submit_span = _span_min(ctx.submit, mem)
        traits = _cluster_traits(ctx, mem, ehash[sel], erepo[sel], ebul[sel], key_counts, n_speed,
                                 start_span, submit_span)
        sync_keys = {"sync_start", "sync_submission"}       # duration alone is not synchronisation
        sync_frac = float(np.mean([bool(sync_keys & set(ks.split(S.SEP_LIST))) for ks in ekeys[sel]]))
        pattern = _cluster_pattern(key_counts, n_speed, k, sync_frac)
        labels = [_lower_first(SIGNAL_LABELS[s]) for s in top[:3]]
        where = (f"{len(colleges)} college{'s' if len(colleges) != 1 else ''}" if colleges
                 else "unrecorded colleges")
        summary = (f"Pattern consistent with {pattern}: {k} candidates from {where} linked by "
                   f"{_and_join(labels)}; max pair score {float(scores.max()):.0f}.")
        best_level = min((str(level[i]) for i in mem), key=lambda lv: rank_of.get(lv, 99))
        rows.append({
            "cluster_id": comp["id"], "size": k,
            "members": S.SEP_LIST.join(str(ctx.ids[i]) for i in mem),
            "member_names": S.SEP_LIST.join(str(ctx.names[i] or "") for i in mem),
            "risk_level": best_level,
            "max_pair_score": round(float(scores.max()), 2),
            "mean_pair_score": round(float(scores.mean()), 2),
            "density": round(len(scores) / (k * (k - 1) / 2), 3),
            "top_signals": S.SEP_LIST.join(SIGNAL_LABELS[s] for s in top),
            "shared_traits": S.SEP_REASONS.join(traits),
            "colleges": S.SEP_LIST.join(colleges), "n_colleges": len(colleges),
            "start_span_min": start_span, "submit_span_min": submit_span,
            "summary": summary,
        })
    return pd.DataFrame(rows, columns=CLUSTER_COLUMNS)


def _cluster_traits(ctx: _Ctx, mem: list[int], hashes: np.ndarray, repos: np.ndarray, bullets: np.ndarray,
                    key_counts: Counter[str], n_speed: int, start_span: float, submit_span: float
                    ) -> list[str]:
    """Human-readable traits shared by a cluster, e.g. "College: X (8/8)", "IP 1.2.3.4 (6/6)"."""
    k = len(mem)
    traits: list[str] = []
    for label, values in (("College", ctx.college), ("IP", ctx.ip), ("Subnet", ctx.subnet),
                          ("Test mode", ctx.test_mode)):
        if values is None or (label == "Subnet" and any(t.startswith("IP ") for t in traits)):
            continue
        top = _most_common([values[i] for i in mem])
        if top and top[1] >= 2 and (label != "Test mode" or top[1] == k):
            sep = ": " if label in ("College", "Test mode") else " "
            traits.append(f"{label}{sep}{top[0]} ({top[1]}/{k})")
    for label, keys in (("Shared phone number", ctx.phone_key), ("Shared email address", ctx.email_key)):
        top = _most_common([keys[i] for i in mem]) if keys is not None else None
        if top and top[1] >= 2:
            traits.append(f"{label} ({top[1]}/{k})")
    if start_span == start_span and start_span <= 15:
        traits.append(f"Starts span {start_span:.1f} min")
    if submit_span == submit_span and submit_span <= 15:
        traits.append(f"Submissions span {submit_span:.1f} min")
    qs = sorted({q for h in hashes if h for q in str(h).split(S.SEP_LIST)}, key=lambda q: int(q[1:]))
    if qs:
        traits.append("Identical answers: " + S.SEP_LIST.join(qs))
    rp = sorted({x for r in repos if r for x in str(r).split(S.SEP_LIST)})
    if rp:
        traits.append("Shared GitHub repo: " + S.SEP_LIST.join(rp))
    if len(bullets) and int(bullets.max()) > 0:
        traits.append(f"Shared rare resume bullets: up to {int(bullets.max())}")
    if n_speed:
        traits.append(f"Speed anomaly ({n_speed}/{k})")
    if key_counts.get("plagiarism_match"):
        traits.append(f"HackerRank plagiarism matches: {key_counts['plagiarism_match']}")
    return traits


def _cluster_pattern(keys: Counter[str], n_speed: int, size: int, sync_frac: float) -> str:
    """Name the dominant pattern of a cluster for its one-sentence summary."""
    if keys.get("sequential_same_ip"):
        return "proxy test-taking (shared IP with back-to-back sessions)"
    if keys.get("same_phone") or keys.get("same_email"):
        return "identity overlap (shared contact details)"
    if keys.get("shared_answer_hash") and n_speed * 2 >= size:
        return "prior access to questions (fast completion with identical answers)"
    if keys.get("shared_answer_hash") or keys.get("plagiarism_match"):
        if sync_frac >= 0.5:
            return "coordinated test-taking (synchronised sessions with matching solutions)"
        return "shared solutions (identical or highly similar answers)"
    if any(keys.get(k) for k in ("shared_github_repo", "shared_bullets", "resume_text_high",
                                 "resume_text_medium")):
        return "shared or templated resumes"
    if keys.get("team_github_repo") and sync_frac == 0:
        return "a shared team project (verify individual roles)"
    if sync_frac > 0:
        return "coordinated test-taking (synchronised sessions)"
    if keys.get("same_ip") or keys.get("same_subnet"):
        return "a shared test location"
    return "linked activity"


# ===========================================================================
# Helpers
# ===========================================================================
def _guard(label: str, fn: Callable[[], _T], default: _T, warnings: list[str]) -> _T:
    """Run one analysis step; on failure record a warning (exception type only - no data)."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - a failing signal must never abort the analysis
        logger.warning("Risk step '%s' failed (%s)", label, type(exc).__name__)
        warnings.append(f"{label} skipped: internal error ({type(exc).__name__}).")
        return default


def _base_params(rc: RiskConfig) -> dict[str, Any]:
    params = asdict(rc)
    params.update({
        "common_hash_min_holders": COMMON_HASH_MIN_HOLDERS,
        "rare_min_holders": RARE_MIN_HOLDERS,
        "min_speed_sample": MIN_SPEED_SAMPLE,
        "min_doc_words": MIN_DOC_WORDS,
    })
    return params


def _empty(columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype=object) for c in columns})


def _all_clear(candidates: Any, focus: np.ndarray | None = None) -> pd.DataFrame:
    """Clear risk rows for whatever candidate IDs exist (fallback when analysis cannot run)."""
    if not isinstance(candidates, pd.DataFrame) or S.CANDIDATE_ID not in candidates.columns:
        return _empty(CANDIDATE_RISK_COLUMNS)
    ids = [v for v in dict.fromkeys(_str_values(candidates[S.CANDIDATE_ID])) if v is not None]
    is_focus = np.zeros(len(ids), dtype=bool)
    if focus is not None and len(focus) == len(ids):
        is_focus = focus.astype(bool)
    return pd.DataFrame({
        S.CANDIDATE_ID: pd.Series(ids, dtype=object).astype(str), S.IS_FOCUS: is_focus,
        S.RISK_SCORE: 0.0, S.RISK_LEVEL: S.RISK_CLEAR, S.RISK_REASONS: "", S.CLUSTER_ID: "",
        S.N_LINKS: 0, S.MAX_PAIR_SCORE: 0.0, S.INDIVIDUAL_SCORE: 0.0, S.INJECTION_FLAGS: "",
    })[CANDIDATE_RISK_COLUMNS]


def _weights(user: Any, defaults: dict[str, float]) -> dict[str, float]:
    out = {k: float(v) for k, v in defaults.items()}
    if isinstance(user, dict):
        for k, v in user.items():
            try:
                out[str(k)] = max(0.0, float(v))
            except (TypeError, ValueError):
                continue
    return out


def _text(v: Any) -> str | None:
    """Stripped string or None for missing / placeholder values (integral floats lose '.0')."""
    if v is None or v is pd.NA or v is pd.NaT:
        return None
    if isinstance(v, (float, np.floating)):
        if np.isnan(v):
            return None
        if float(v).is_integer():
            v = int(v)
    t = str(v).strip()
    return None if t.casefold() in _MISSING_TOKENS else t


def _str_values(s: pd.Series) -> np.ndarray:
    return np.array([_text(v) for v in s.to_numpy(dtype=object)], dtype=object)


def _str_col(df: pd.DataFrame, col: str) -> np.ndarray | None:
    return _str_values(df[col]) if col in df.columns else None


def _num_col(df: pd.DataFrame, col: str) -> np.ndarray | None:
    if col not in df.columns:
        return None
    s = df[col]
    if pd.api.types.is_bool_dtype(s):
        s = s.astype(float)
    return pd.to_numeric(s, errors="coerce").to_numpy(dtype=float, na_value=np.nan)


def _bool_col(df: pd.DataFrame, col: str) -> np.ndarray | None:
    vals = _str_col(df, col)
    return None if vals is None else np.array([bool(t) and t.casefold() in _TRUE_TOKENS for t in vals], dtype=bool)


def _seconds_col(df: pd.DataFrame, col: str) -> np.ndarray | None:
    if col not in df.columns:
        return None
    s = df[col]
    try:
        if not pd.api.types.is_datetime64_any_dtype(s):
            try:
                s = pd.to_datetime(s, errors="coerce", format="mixed")
            except (TypeError, ValueError):
                s = pd.to_datetime(s, errors="coerce", format="mixed", utc=True)
            if not pd.api.types.is_datetime64_any_dtype(s):
                s = pd.to_datetime(s, errors="coerce", format="mixed", utc=True)
        if getattr(s.dt, "tz", None) is not None:
            s = s.dt.tz_convert("UTC").dt.tz_localize(None)
        secs = (s - _EPOCH) / pd.Timedelta(seconds=1)
        return secs.to_numpy(dtype=float, na_value=np.nan)
    except (TypeError, ValueError, AttributeError, OverflowError):
        return np.full(len(df), np.nan)


def _nones(n: int) -> np.ndarray:
    return np.full(n, None, dtype=object)


def _blanks(n: int) -> np.ndarray:
    return np.full(n, "", dtype=object)


def _blank(proto: np.ndarray, m: int) -> np.ndarray:
    kind = np.asarray(proto).dtype.kind
    if kind == "f":
        return np.full(m, np.nan)
    if kind in "iu":
        return np.zeros(m, dtype=np.int64)
    if kind == "b":
        return np.zeros(m, dtype=bool)
    return _blanks(m)


def _known(values: np.ndarray) -> np.ndarray:
    return np.array([v is not None for v in values], dtype=bool)


def _same(values: np.ndarray | None, A: np.ndarray, B: np.ndarray) -> np.ndarray:
    if values is None or not len(A):
        return np.zeros(len(A), dtype=bool)
    known = _known(values)
    return known[A] & known[B] & (values[A] == values[B])


def _group_counts(values: np.ndarray | None) -> np.ndarray:
    """Number of candidates sharing each row's value (NaN where the value is missing)."""
    if values is None:
        return np.empty(0)
    counts = Counter(v for v in values if v is not None)
    return np.array([counts[v] if v is not None else np.nan for v in values], dtype=float)


def _self_join(pos: np.ndarray, keys: np.ndarray, anchor: np.ndarray | None = None) -> pd.DataFrame:
    """Rows (a, b, key) for distinct positions a < b sharing a key; anchor: keep a|b in anchor."""
    if not len(pos):
        return pd.DataFrame({"a": np.empty(0, np.int64), "b": np.empty(0, np.int64),
                             "key": np.empty(0, dtype=object)})
    long = pd.DataFrame({"pos": np.asarray(pos, dtype=np.int64), "key": keys}).drop_duplicates()
    m = long.merge(long, on="key", suffixes=("_a", "_b"))
    pa, pb = m["pos_a"].to_numpy(), m["pos_b"].to_numpy()
    sel = pa < pb
    if anchor is not None:
        sel &= anchor[pa] | anchor[pb]
    return pd.DataFrame({"a": pa[sel], "b": pb[sel], "key": m["key"].to_numpy(dtype=object)[sel]})


def _unique_gen(a: np.ndarray, b: np.ndarray, n: int | None = None) -> _Gen:
    if not len(a):
        return _Gen.empty()
    n = n or int(max(a.max(), b.max())) + 1
    k = np.unique(a.astype(np.int64) * n + b)
    return _Gen(k // n, k % n)


def _norm_key(t: str | None) -> str | None:
    if not t:
        return None
    key = " ".join(_KEY_RE.sub(" ", t.casefold()).split())
    return key or None


def _hash_key(v: Any) -> str | None:
    t = _text(v)
    if t is None:
        return None
    t = t.casefold()
    return None if t in _EMPTY_HASHES else t


def _first_ip(t: str | None) -> str | None:
    if not t:
        return None
    tok = _IP_SPLIT_RE.split(t.strip())[0].strip("[]").lower()
    if tok.count(":") == 1 and "." in tok:          # "1.2.3.4:5678" -> drop port
        tok = tok.split(":", 1)[0]
    try:
        addr = ipaddress.ip_address(tok)
    except ValueError:
        return tok or None                           # opaque but present: still comparable
    if addr.is_unspecified or addr.is_loopback or addr.is_link_local or addr.is_multicast:
        return None
    return str(addr)


def _subnet_of(ip: str | None) -> str | None:
    if not ip:
        return None
    parts = ip.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        return f"{parts[0]}.{parts[1]}.{parts[2]}.0/24"
    return None


def _phone_key(t: str | None) -> str | None:
    if not t:
        return None
    d = re.sub(r"\D", "", t)[-10:]
    if len(d) < 10 or len(set(d)) == 1 or d in _FAKE_PHONES:
        return None
    return d


def _email_key(t: str | None) -> str | None:
    """Canonical mailbox: lowercase, '+tag' dropped, Gmail dots ignored."""
    if not t or "@" not in t:
        return None
    local, _, domain = t.strip().casefold().rpartition("@")
    local = local.split("+", 1)[0]
    if domain in ("gmail.com", "googlemail.com"):
        local, domain = local.replace(".", ""), "gmail.com"
    if not local or "." not in domain:
        return None
    return f"{local}@{domain}"


def _is_on_campus(t: str | None) -> bool:
    if not t:
        return False
    k = re.sub(r"[^a-z]", "", t.casefold())
    return k in _ON_CAMPUS or k.startswith("oncampus")


def _is_integrity_concern(t: str) -> bool:
    """Review / Suspicious / Flagged (HackerRank AI proctoring) or Medium / High (Workday integrity
    issues) - schema.INTEGRITY_CONCERN_VALUES; negated values ("Not Suspicious") are not."""
    low = t.casefold().strip()
    if low.startswith(("not ", "no ", "non-", "non ")):
        return False
    return any(word in S.INTEGRITY_CONCERN_VALUES for word in _WORD_RE.findall(low))


def _injection_findings(p: Any) -> list[dict[str, Any]]:
    """Hidden-text / prompt-injection findings of a parsed resume (absent on older cached objects)."""
    found = getattr(p, "injection_findings", None) or []
    return [f for f in found if isinstance(f, dict)]


def _duplicate_groups(df: pd.DataFrame, ids: np.ndarray) -> np.ndarray | None:
    """Group number per row joining duplicate applications with their primary
    (``duplicate_of`` / ``duplicate_ids``); None when the input marks no duplicates."""
    of = _str_col(df, S.DUPLICATE_OF)
    listed = _str_col(df, S.DUPLICATE_IDS)
    if of is None and listed is None:
        return None
    n = len(ids)
    pos = {str(c).upper(): i for i, c in enumerate(ids)}
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    linked = False
    for i in range(n):
        others = [of[i]] if of is not None and of[i] else []
        if listed is not None and listed[i]:
            others += [t for t in _ID_SPLIT_RE.split(listed[i]) if t]
        for other in others:
            j = pos.get(other.strip().upper())
            if j is not None and j != i:
                parent[find(i)] = find(j)
                linked = True
    return np.array([find(i) for i in range(n)], dtype=np.int64) if linked else None


def _resume_repos(p: ParsedResume) -> frozenset[str]:
    urls = list(p.github_urls or [])
    urls += [str(x.get("github")) for x in (p.projects or []) if isinstance(x, dict) and x.get("github")]
    out = set()
    for u in urls:
        m = _REPO_RE.search(str(u))
        if not m:
            continue
        user, repo = m.group(1).lower(), re.sub(r"\.git$", "", m.group(2).lower())
        if repo and user not in _GH_RESERVED:
            out.add(f"github.com/{user}/{repo}")
    return frozenset(out)


def _resume_bullets(p: ParsedResume, min_words: int) -> dict[str, str]:
    raw = [b for b in (p.bullets or []) if isinstance(b, str)]
    if not raw:
        for proj in p.projects or []:
            if isinstance(proj, dict):
                raw += [b for b in (proj.get("bullets") or []) if isinstance(b, str)]
    out: dict[str, str] = {}
    for b in raw:
        text = " ".join(b.split()).replace("|", "/")
        words = text.split()
        if not words or len(_LIST_SEP_RE.findall(text)) >= 0.4 * len(words):   # skill lists, not sentences
            continue
        key = " ".join(_NON_ALNUM_RE.sub(" ", text.casefold()).split())
        if len(key.split()) >= min_words:
            out[key] = min(out.get(key, text), text)
    return out


def _resume_doc(p: ParsedResume) -> str:
    """Projects + experience text; falls back to entry texts, then the full resume text."""
    secs = p.sections or {}
    body = "\n".join(str(secs.get(k) or "") for k in ("projects", "experience")).strip()
    if len(body.split()) < MIN_DOC_WORDS:
        entries = [str(x.get("text") or "") for x in list(p.projects or []) + list(p.experiences or [])
                   if isinstance(x, dict)]
        alt = "\n".join(entries).strip()
        if len(alt.split()) > len(body.split()):
            body = alt
    if len(body.split()) < MIN_DOC_WORDS and p.text:
        body = p.text
    return body


def _span_min(values: np.ndarray | None, mem: list[int]) -> float:
    if values is None:
        return float("nan")
    v = values[mem]
    v = v[~np.isnan(v)]
    return round(float((v.max() - v.min()) / 60.0), 2) if len(v) >= 2 else float("nan")


def _most_common(values: list[Any]) -> tuple[str, int] | None:
    counts = Counter(str(v) for v in values if v)
    if not counts:
        return None
    value, count = min(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return value, count


def _id_list(ids: list[str]) -> str:
    head = S.SEP_LIST.join(ids[:MAX_IDS_IN_REASON])
    return head + (f" (+{len(ids) - MAX_IDS_IN_REASON} more)" if len(ids) > MAX_IDS_IN_REASON else "")


def _lower_first(label: str) -> str:
    """'Same IP' -> 'same IP'; brand names ('HackerRank ...') stay as they are."""
    return label if label.startswith(("HackerRank", "GitHub")) else label[:1].lower() + label[1:]


def _and_join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _clip(text: str, n: int) -> str:
    t = " ".join(str(text).split()).replace("|", "/")
    return t if len(t) <= n else t[: n - 3].rstrip() + "..."
