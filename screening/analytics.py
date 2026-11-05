"""Shortlist analytics: campus distribution and before/after shortlist comparison.

Pure pandas (no Streamlit). Both functions accept frames shaped like
``PipelineResult.candidates``, tolerate missing optional columns and never modify
their inputs.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from . import schema as S
from .config import AppConfig
from .scoring import college_key

IN_SHORTLIST = (S.STATUS_SHORTLISTED, S.STATUS_REVIEW)
UNKNOWN_GROUP = "(unknown)"
FLAGGED_LEVELS = (S.RISK_HIGH, S.RISK_MEDIUM)
PERFECT_PCT = 99.995  # HackerRank % treated as a perfect score (float tolerance)

DISTRIBUTION_COLUMNS = [
    "group", "applicants", "eligible", "perfect_scorers", "shortlisted", "in_review", "capped_out",
    "shortlist_rate", "share_of_shortlist", "share_of_applicants", "representation",
    "avg_final_score", "avg_hackerrank_pct", "flagged", "over_cap",
]
_COUNT_COLUMNS = ["applicants", "eligible", "perfect_scorers", "shortlisted", "in_review", "capped_out", "flagged"]
DIFF_COLUMNS = [
    S.CANDIDATE_ID, S.NAME, S.COLLEGE, "rank_before", "rank_after", "rank_change",
    "status_before", "status_after", "score_before", "score_after", "reason_after",
    "shortlisted_before", "shortlisted_after",
]
_TRUE_TEXT = frozenset({"true", "t", "yes", "y", "1"})


# ---------------------------------------------------------------------------
# Tolerant column access
# ---------------------------------------------------------------------------
def _isna(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (str, list, tuple, set, dict)):
        return False
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _cell_text(value: Any) -> str:
    if _isna(value):
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return " ".join(str(value).split())


def _text(df: pd.DataFrame, col: str) -> pd.Series:
    """Whitespace-collapsed text ('' when missing), aligned with ``df``."""
    if col not in df.columns:
        return pd.Series([""] * len(df), index=df.index, dtype=object)
    return pd.Series([_cell_text(v) for v in df[col].tolist()], index=df.index, dtype=object)


def _num(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series(np.nan, index=df.index, dtype="float64")
    return pd.to_numeric(df[col], errors="coerce").astype("float64")


def _truthy(value: Any) -> bool:
    if _isna(value):
        return False
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, float, np.integer, np.floating)):
        return value != 0
    return str(value).strip().lower() in _TRUE_TEXT


def _flag(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series(False, index=df.index, dtype=bool)
    return pd.Series([_truthy(v) for v in df[col].tolist()], index=df.index, dtype=bool)


def _eligible(df: pd.DataFrame) -> pd.Series:
    if S.ELIGIBLE in df.columns:
        return _flag(df, S.ELIGIBLE)
    if S.RANK in df.columns:
        return _num(df, S.RANK).notna()
    if S.STATUS in df.columns:
        status = _text(df, S.STATUS)
        return (status != "") & (status != S.STATUS_INELIGIBLE)
    return pd.Series(True, index=df.index, dtype=bool)


def group_labels(candidates: pd.DataFrame, by: str) -> pd.Series:
    """Group label per row for ``by``: blanks and placeholders ('N/A', ...) -> '(unknown)'.

    Values are compared like the college cap compares colleges (case and spacing ignored);
    each group is labelled with its most frequent spelling.
    """
    raw = _text(candidates, by)
    keys = raw.map(college_key)
    labels = pd.Series(UNKNOWN_GROUP, index=candidates.index, dtype=object)
    known = keys != ""
    if known.any():
        counts = (pd.DataFrame({"key": keys[known], "raw": raw[known]})
                  .groupby(["key", "raw"], sort=True).size().rename("n").reset_index())
        best = (counts.sort_values(["key", "n", "raw"], ascending=[True, False, True], kind="mergesort")
                .drop_duplicates("key").set_index("key")["raw"])
        labels[known] = keys[known].map(best)
    return labels


def _cap_pct(cfg: AppConfig | None) -> float:
    if cfg is None:
        return 0.0
    try:
        value = float(getattr(cfg.shortlist, "college_cap_pct", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


# ---------------------------------------------------------------------------
# Campus distribution
# ---------------------------------------------------------------------------
def _empty_distribution() -> pd.DataFrame:
    dtypes = {c: "int64" for c in _COUNT_COLUMNS}
    dtypes.update(group=object, over_cap=bool)
    return pd.DataFrame({c: pd.Series(dtype=dtypes.get(c, "float64")) for c in DISTRIBUTION_COLUMNS})


def campus_distribution(candidates: pd.DataFrame | None, by: str = S.COLLEGE,
                        cfg: AppConfig | None = None) -> pd.DataFrame:
    """One row per value of ``by`` (blank -> '(unknown)') describing who applied and who was shortlisted.

    Columns: group, applicants, eligible, perfect_scorers (HackerRank 100%), shortlisted
    (Shortlisted + Integrity Review), in_review, capped_out (COLLEGE_CAPPED), shortlist_rate
    (shortlisted / eligible; NaN when nobody is eligible), share_of_shortlist,
    share_of_applicants, representation (share_of_shortlist / share_of_applicants; 1.0 =
    proportional, NaN when nobody is shortlisted), avg_final_score, avg_hackerrank_pct,
    flagged (High or Medium risk), over_cap (``by`` is the college, a cap is set in ``cfg`` and
    the group's share of the shortlist exceeds it; the unknown group is never over the cap).
    Sorted by shortlisted desc, applicants desc, group.
    """
    if candidates is None or len(candidates) == 0:
        return _empty_distribution()
    df = candidates
    status = _text(df, S.STATUS)
    work = pd.DataFrame({
        "group": group_labels(df, by).to_numpy(),
        "applicants": np.ones(len(df), dtype=np.int64),
        "eligible": _eligible(df).to_numpy(dtype=np.int64),
        "perfect_scorers": (_num(df, S.HR_PCT) >= PERFECT_PCT).to_numpy(dtype=np.int64),
        "shortlisted": status.isin(IN_SHORTLIST).to_numpy(dtype=np.int64),
        "in_review": (status == S.STATUS_REVIEW).to_numpy(dtype=np.int64),
        "capped_out": _flag(df, S.COLLEGE_CAPPED).to_numpy(dtype=np.int64),
        "flagged": _text(df, S.RISK_LEVEL).str.title().isin(FLAGGED_LEVELS).to_numpy(dtype=np.int64),
        "_score": _num(df, S.FINAL_SCORE).to_numpy(),
        "_pct": _num(df, S.HR_PCT).to_numpy(),
    })
    grouped = work.groupby("group", sort=False)
    out = grouped[_COUNT_COLUMNS].sum()
    out["avg_final_score"] = grouped["_score"].mean()
    out["avg_hackerrank_pct"] = grouped["_pct"].mean()
    out = out.reset_index()

    total_short = int(out["shortlisted"].sum())
    total_apps = int(out["applicants"].sum())
    out["shortlist_rate"] = out["shortlisted"] / out["eligible"].where(out["eligible"] > 0)
    out["share_of_shortlist"] = out["shortlisted"] / total_short if total_short else 0.0
    out["share_of_applicants"] = out["applicants"] / total_apps
    out["representation"] = (out["share_of_shortlist"] / out["share_of_applicants"]) if total_short else np.nan
    cap = _cap_pct(cfg) if by == S.COLLEGE else 0.0
    # shortlisted / total > cap% compared in integers-times-100 to avoid float noise at the boundary
    out["over_cap"] = ((out["group"] != UNKNOWN_GROUP) & (out["shortlisted"] * 100.0 > cap * total_short + 1e-9)
                       if cap > 0 else False)
    out = out.astype({c: "int64" for c in _COUNT_COLUMNS}).astype({"over_cap": bool})
    out = out.sort_values(["shortlisted", "applicants", "group"], ascending=[False, False, True], kind="mergesort")
    return out[DISTRIBUTION_COLUMNS].reset_index(drop=True)


# ---------------------------------------------------------------------------
# Shortlist diff
# ---------------------------------------------------------------------------
def _diff_view(df: pd.DataFrame | None) -> pd.DataFrame:
    """candidate_id-indexed view (first row wins for duplicated ids)."""
    cols = ["name", "college", "rank", "status", "score", "reason"]
    if df is None or len(df) == 0 or S.CANDIDATE_ID not in df.columns:
        return pd.DataFrame({c: pd.Series(dtype="float64" if c in ("rank", "score") else object) for c in cols},
                            index=pd.Index([], dtype=object, name=S.CANDIDATE_ID))
    view = pd.DataFrame({
        "name": _text(df, S.NAME).to_numpy(), "college": _text(df, S.COLLEGE).to_numpy(),
        "rank": _num(df, S.RANK).to_numpy(), "status": _text(df, S.STATUS).to_numpy(),
        "score": _num(df, S.FINAL_SCORE).to_numpy(), "reason": _text(df, S.STATUS_REASON).to_numpy(),
    }, index=pd.Index(_text(df, S.CANDIDATE_ID).to_numpy(), dtype=object, name=S.CANDIDATE_ID))
    view = view[view.index != ""]
    return view[~view.index.duplicated(keep="first")]


def _cutoff(df: pd.DataFrame | None) -> float | None:
    """Final score of the lowest-scoring shortlisted candidate (None when nobody is shortlisted)."""
    if df is None or len(df) == 0:
        return None
    scores = _num(df, S.FINAL_SCORE)[_text(df, S.STATUS).isin(IN_SHORTLIST)].dropna()
    return round(float(scores.min()), 2) if len(scores) else None


def _count(df: pd.DataFrame | None, statuses: tuple[str, ...]) -> int:
    if df is None or len(df) == 0:
        return 0
    return int(_text(df, S.STATUS).isin(statuses).sum())


def _prefer(after: pd.Series, before: pd.Series) -> pd.Series:
    """Value from ``after`` unless it is blank/missing there."""
    a = after.fillna("").astype(object)
    return a.where(a != "", before.fillna("").astype(object))


def shortlist_diff(before: pd.DataFrame | None, after: pd.DataFrame | None) -> dict[str, Any]:
    """Compare two shortlist outcomes for the same candidates (matched by candidate_id).

    Returns {"entering", "leaving", "status_changes", "rank_moves": DataFrames with
    DIFF_COLUMNS, "summary": dict}. entering = shortlisted (Shortlisted or Integrity Review)
    only after, sorted by new rank; leaving = shortlisted only before, sorted by old rank;
    status_changes = every candidate whose status differs; rank_moves = candidates ranked in
    both with a different rank, biggest move first (rank_change > 0 = moved up). summary:
    shortlisted_before/after, entering, leaving, cutoff_before/after (lowest shortlisted final
    score or None), review_before/after, status_changes, capped_before/after.
    """
    b, a = _diff_view(before), _diff_view(after)
    ids = b.index.union(a.index)
    b, a = b.reindex(ids), a.reindex(ids)
    status_before = b["status"].fillna("").astype(object)
    status_after = a["status"].fillna("").astype(object)
    in_before = status_before.isin(IN_SHORTLIST)
    in_after = status_after.isin(IN_SHORTLIST)
    table = pd.DataFrame({
        S.CANDIDATE_ID: ids.to_numpy(dtype=object),
        S.NAME: _prefer(a["name"], b["name"]).to_numpy(),
        S.COLLEGE: _prefer(a["college"], b["college"]).to_numpy(),
        "rank_before": b["rank"].to_numpy(dtype="float64"),
        "rank_after": a["rank"].to_numpy(dtype="float64"),
        "rank_change": (b["rank"] - a["rank"]).to_numpy(dtype="float64"),
        "status_before": status_before.to_numpy(), "status_after": status_after.to_numpy(),
        "score_before": b["score"].to_numpy(dtype="float64"), "score_after": a["score"].to_numpy(dtype="float64"),
        "reason_after": a["reason"].fillna("").astype(object).to_numpy(),
        "shortlisted_before": in_before.to_numpy(dtype=bool), "shortlisted_after": in_after.to_numpy(dtype=bool),
    })

    def ordered(mask: Any, by: list[str], ascending: list[bool] | bool = True) -> pd.DataFrame:
        part = table[np.asarray(mask, dtype=bool)]
        part = part.sort_values(by, ascending=ascending, na_position="last", kind="mergesort")
        return part[DIFF_COLUMNS].reset_index(drop=True)

    entering = ordered(in_after.to_numpy() & ~in_before.to_numpy(), ["rank_after", S.CANDIDATE_ID])
    leaving = ordered(in_before.to_numpy() & ~in_after.to_numpy(), ["rank_before", S.CANDIDATE_ID])
    status_changes = ordered((status_before != status_after).to_numpy(), ["rank_after", "rank_before", S.CANDIDATE_ID])
    moved = table["rank_before"].notna() & table["rank_after"].notna() & (table["rank_change"] != 0)
    table["_abs_move"] = table["rank_change"].abs()
    rank_moves = (table[moved.to_numpy()]
                  .sort_values(["_abs_move", "rank_after", S.CANDIDATE_ID], ascending=[False, True, True],
                               kind="mergesort")[DIFF_COLUMNS].reset_index(drop=True))

    summary = {
        "shortlisted_before": _count(before, IN_SHORTLIST),
        "shortlisted_after": _count(after, IN_SHORTLIST),
        "entering": len(entering),
        "leaving": len(leaving),
        "cutoff_before": _cutoff(before),
        "cutoff_after": _cutoff(after),
        "review_before": _count(before, (S.STATUS_REVIEW,)),
        "review_after": _count(after, (S.STATUS_REVIEW,)),
        "status_changes": len(status_changes),
        "capped_before": 0 if before is None else int(_flag(before, S.COLLEGE_CAPPED).sum()),
        "capped_after": 0 if after is None else int(_flag(after, S.COLLEGE_CAPPED).sum()),
    }
    return {"entering": entering, "leaving": leaving, "status_changes": status_changes,
            "rank_moves": rank_moves, "summary": summary}
