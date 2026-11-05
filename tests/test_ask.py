"""Tests for screening.ask: plan validation, local execution, prompts and privacy.

No network: a fake client returns canned plans. All people and addresses are synthetic.
"""
from __future__ import annotations

import builtins
import inspect
import json
import re
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pytest

from screening import ask, llm
from screening import schema as S
from screening.config import AppConfig

try:
    from tests.ui_fixtures import make_fake_prepared_and_result
except ImportError:  # pragma: no cover - tests/ imported as rootdir-relative modules
    from ui_fixtures import make_fake_prepared_and_result

NAMES = ["Asha Menon", "Rohan Das", "Meera Iyer", "Vikram Shah", "Priya Nair", "Arjun Rao", "Kavya Bose",
         "Tarun Gupta"]
EXAMPLE_QUESTIONS = [
    "How many perfect scorers are shortlisted per college?",
    "Which colleges have the most High-risk candidates?",
    "Average final score by college tier",
    "List shortlisted candidates from Pune with CGPA above 9",
    "How many candidates are under integrity review by cluster?",
]
SHORTLISTED = [S.STATUS_SHORTLISTED, S.STATUS_REVIEW]


def make_frame() -> pd.DataFrame:
    """Eight candidates covering every column kind, blanks, hashes and personal data."""
    first_last = [n.lower().split() for n in NAMES]
    return pd.DataFrame({
        S.CANDIDATE_ID: [f"C-{i}" for i in range(1, 9)],
        S.NAME: NAMES,
        S.EMAIL: [f"{f}.{l}@example.com" for f, l in first_last],
        S.PHONE: [f"98765432{i:02d}" for i in range(1, 9)],
        S.IP_ADDRESS: [f"49.36.12.{i}" for i in range(1, 9)],
        S.COLLEGE: ["Northfield Institute", "Northfield Institute", "Riverside College", "Riverside College",
                    "Lakeside University", "Lakeside University", "Northfield Institute", None],
        S.COLLEGE_TIER: ["1", "1", "2", "2", "3", "3", "1", None],
        S.CITY: ["Pune", "Pune", "Chennai", "Pune", "Kolkata", "Chennai", "pune", ""],
        S.CGPA: [9.2, 8.1, 9.5, np.nan, 7.0, 9.1, 8.8, 6.5],
        S.FINAL_SCORE: [91.0, 85.5, 78.25, 66.0, 59.5, 88.0, 72.0, 40.0],
        S.HR_PCT: [100.0, 100.0, 90.0, 100.0, 55.0, 100.0, 80.0, np.nan],
        S.RANK: pd.array([1, 3, 4, 6, 7, 2, 5, None], dtype="Int64"),
        S.GRAD_YEAR: pd.array([2026, 2026, 2025, 2026, 2025, 2026, 2026, None], dtype="Int64"),
        S.STATUS: [S.STATUS_SHORTLISTED, S.STATUS_REVIEW, S.STATUS_SHORTLISTED, S.STATUS_REVIEW, S.STATUS_NOT,
                   S.STATUS_SHORTLISTED, S.STATUS_NOT, S.STATUS_INELIGIBLE],
        S.RISK_LEVEL: [S.RISK_CLEAR, S.RISK_HIGH, S.RISK_LOW, S.RISK_HIGH, S.RISK_CLEAR, S.RISK_MEDIUM, S.RISK_CLEAR,
                       S.RISK_CLEAR],
        S.CLUSTER_ID: ["", "C01", "", "C01", "", "C02", "", ""],
        S.IS_FOCUS: [True, True, False, True, False, True, False, False],
        S.PLAGIARISM_FLAG: pd.Series([False, True, None, True, False, False, None, False], dtype=object),
        S.MATCHED_SKILLS: ["Python, Java, SQL", "JavaScript", "Java", "Python", "", "Python, SQL", "SQL, Cloud", ""],
        S.TEST_START: pd.to_datetime(["2026-10-20 10:00", "2026-10-20 10:30", "2026-10-20 11:00",
                                      "2026-10-20 11:30", "2026-10-20 12:00", None, "2026-10-20 09:15",
                                      "2026-10-20 13:45"]),
        "q1_hash": [f"h{i}" for i in range(8)],
        "q2_score": [25.0, 25.0, 20.0, 25.0, 5.0, 25.0, 15.0, 0.0],
        S.RESUME_HASH: [f"{i:064x}" for i in range(8)],
        "extra_recruiter_notes": ["call back Tuesday", "", "strong communicator", "", "", "", "",
                                  "relocation pending"],
    })


@pytest.fixture
def df() -> pd.DataFrame:
    return make_frame()


def ids(table: pd.DataFrame) -> list[str]:
    return sorted(table[S.CANDIDATE_ID].tolist())


class FakeClient:
    """Stand-in for screening.llm.LLMClient: records prompts, replies with a canned plan."""

    def __init__(self, reply: Any) -> None:
        self.reply = reply
        self.calls: list[dict[str, Any]] = []

    def chat_json(self, system: str, user: str, *, max_tokens: int | None = None,
                  required_keys: tuple[str, ...] = ()) -> Any:
        self.calls.append({"system": system, "user": user, "required_keys": required_keys})
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply(system, user) if callable(self.reply) else self.reply


class FakeTransport:
    """chat.completions stand-in for a real llm.LLMClient (no network)."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        content = self.replies.pop(0)
        message = SimpleNamespace(content=content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")])


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------
def test_whitelists_and_personal_columns() -> None:
    assert ask.ALLOWED_OPS == {"==", "!=", ">", ">=", "<", "<=", "in", "not_in", "contains", "is_null", "not_null"}
    assert ask.ALLOWED_AGGS == {"count", "sum", "mean", "median", "min", "max", "nunique"}
    for col in (S.NAME, S.EMAIL, S.PHONE, S.IP_ADDRESS, S.IP_ADDRESSES, S.RESUME_FILE, S.RESUME_HASH, S.REPORT_URL,
                S.PLAGIARISM_MATCH, S.PLAGIARISM_MATCH_ID, "extra_invitedby", "q7_hash"):
        assert col in ask.PII_COLUMNS and ask.is_pii_column(col)
    for col in (S.COLLEGE, S.FINAL_SCORE, S.STATUS, "q2_score", S.CANDIDATE_ID):
        assert col not in ask.PII_COLUMNS
    assert ask.is_hidden_column("q3_hash") and ask.is_hidden_column(S.RESUME_HASH)
    assert not ask.is_hidden_column(S.NAME)


def test_describe_columns_kinds_ranges_and_no_personal_values(df: pd.DataFrame) -> None:
    catalogue = ask.describe_columns(df)
    cols = {c["name"]: c for c in catalogue}
    assert "q1_hash" not in cols and S.RESUME_HASH not in cols          # hashes are never offered
    assert (cols[S.FINAL_SCORE]["kind"], cols[S.FINAL_SCORE]["min"], cols[S.FINAL_SCORE]["max"]) == ("number", 40, 91)
    assert cols[S.CGPA]["min"] == 6.5 and cols[S.CGPA]["max"] == 9.5
    assert cols[S.IS_FOCUS]["kind"] == "bool" and cols[S.PLAGIARISM_FLAG]["kind"] == "bool"
    assert cols[S.TEST_START]["kind"] == "datetime" and cols[S.TEST_START]["min"] == "2026-10-20 09:15"
    assert cols[S.RANK]["kind"] == "number" and cols[S.GRAD_YEAR]["max"] == 2026
    assert cols[S.STATUS]["kind"] == "text" and set(cols[S.STATUS]["examples"]) == {
        S.STATUS_SHORTLISTED, S.STATUS_REVIEW, S.STATUS_NOT, S.STATUS_INELIGIBLE}
    assert cols[S.COLLEGE]["examples"][0] == "Northfield Institute"        # most frequent first
    assert cols[S.HR_PCT]["label"] == S.TA_FIELD_LABELS[S.HR_PCT]
    assert cols[S.FINAL_SCORE]["label"] == "Final score" and cols[S.FINAL_SCORE]["description"]
    assert cols["q2_score"]["label"] == "Q2 score"
    for col in (S.NAME, S.EMAIL, S.PHONE, S.IP_ADDRESS, "extra_recruiter_notes"):
        assert cols[col]["personal"] is True
        assert not {"examples", "min", "max"} & set(cols[col])
    blob = json.dumps(catalogue)
    for col in (S.NAME, S.EMAIL, S.PHONE, S.IP_ADDRESS, "extra_recruiter_notes"):
        for value in df[col]:
            if value:
                assert str(value) not in blob


def test_examples_only_for_low_cardinality_allow_listed_columns() -> None:
    wide = pd.DataFrame({S.COLLEGE: [f"College {i}" for i in range(31)], S.STATUS: [S.STATUS_SHORTLISTED] * 31,
                         S.SOURCE: ["Referral from asha@example.com"] + ["Campus Drive"] * 30,
                         S.RISK_REASONS: ["Same IP"] * 31, "custom_text": ["x"] * 31})
    cols = {c["name"]: c for c in ask.describe_columns(wide)}
    assert "examples" not in cols[S.COLLEGE]                # 31 distinct values > 30
    assert cols[S.STATUS]["examples"] == [S.STATUS_SHORTLISTED]
    assert "examples" not in cols[S.SOURCE]                 # a value looks like contact data
    assert "examples" not in cols[S.RISK_REASONS]           # free text is never sampled
    assert "examples" not in cols["custom_text"]            # unknown columns are not sampled


# ---------------------------------------------------------------------------
# Filters: every operator on every kind
# ---------------------------------------------------------------------------
ALL = [f"C-{i}" for i in range(1, 9)]


@pytest.mark.parametrize("flt, expected", [
    ({"column": S.CGPA, "op": ">", "value": 9}, ["C-1", "C-3", "C-6"]),
    ({"column": S.CGPA, "op": ">=", "value": 9.2}, ["C-1", "C-3"]),
    ({"column": S.CGPA, "op": "<", "value": 8}, ["C-5", "C-8"]),
    ({"column": S.CGPA, "op": "<=", "value": "8.1"}, ["C-2", "C-5", "C-8"]),
    ({"column": S.CGPA, "op": "==", "value": 9.5}, ["C-3"]),
    ({"column": S.CGPA, "op": "!=", "value": 9.5}, [c for c in ALL if c != "C-3"]),
    ({"column": S.HR_PCT, "op": ">=", "value": 100}, ["C-1", "C-2", "C-4", "C-6"]),
    ({"column": S.GRAD_YEAR, "op": "in", "value": [2025]}, ["C-3", "C-5"]),
    ({"column": S.GRAD_YEAR, "op": "not_in", "value": [2026]}, ["C-3", "C-5", "C-8"]),
    ({"column": S.CGPA, "op": "is_null"}, ["C-4"]),
    ({"column": S.CGPA, "op": "not_null"}, [c for c in ALL if c != "C-4"]),
    ({"column": S.CITY, "op": "==", "value": "PUNE"}, ["C-1", "C-2", "C-4", "C-7"]),
    ({"column": S.CITY, "op": "!=", "value": "pune"}, ["C-3", "C-5", "C-6", "C-8"]),
    ({"column": S.CITY, "op": "in", "value": ["chennai", "Kolkata"]}, ["C-3", "C-5", "C-6"]),
    ({"column": S.CITY, "op": "not_in", "value": ["Pune"]}, ["C-3", "C-5", "C-6", "C-8"]),
    ({"column": S.CITY, "op": "is_null"}, ["C-8"]),                         # "" counts as missing
    ({"column": S.COLLEGE, "op": "not_null"}, ALL[:7]),
    ({"column": S.COLLEGE, "op": "contains", "value": "FIELD"}, ["C-1", "C-2", "C-7"]),
    ({"column": S.COLLEGE_TIER, "op": "<=", "value": 2}, ["C-1", "C-2", "C-3", "C-4", "C-7"]),
    ({"column": S.COLLEGE_TIER, "op": ">", "value": "1"}, ["C-3", "C-4", "C-5", "C-6"]),
    ({"column": S.COLLEGE_TIER, "op": ">=", "value": "2"}, ["C-3", "C-4", "C-5", "C-6"]),
    ({"column": S.COLLEGE_TIER, "op": "<", "value": "2"}, ["C-1", "C-2", "C-7"]),
    ({"column": S.IS_FOCUS, "op": "==", "value": True}, ["C-1", "C-2", "C-4", "C-6"]),
    ({"column": S.IS_FOCUS, "op": "!=", "value": "yes"}, ["C-3", "C-5", "C-7", "C-8"]),
    ({"column": S.IS_FOCUS, "op": "not_in", "value": [True]}, ["C-3", "C-5", "C-7", "C-8"]),
    ({"column": S.PLAGIARISM_FLAG, "op": "in", "value": [True]}, ["C-2", "C-4"]),
    ({"column": S.PLAGIARISM_FLAG, "op": "is_null"}, ["C-3", "C-7"]),
    ({"column": S.PLAGIARISM_FLAG, "op": "not_null"}, ["C-1", "C-2", "C-4", "C-5", "C-6", "C-8"]),
    ({"column": S.TEST_START, "op": ">=", "value": "2026-10-20 11:00"}, ["C-3", "C-4", "C-5", "C-8"]),
    ({"column": S.TEST_START, "op": ">", "value": "2026-10-20 11:00"}, ["C-4", "C-5", "C-8"]),
    ({"column": S.TEST_START, "op": "<", "value": "2026-10-20 11:00"}, ["C-1", "C-2", "C-7"]),
    ({"column": S.TEST_START, "op": "<=", "value": "2026-10-20 10:00"}, ["C-1", "C-7"]),
    ({"column": S.TEST_START, "op": "==", "value": "2026-10-20 10:30"}, ["C-2"]),
    ({"column": S.TEST_START, "op": "!=", "value": "2026-10-20 10:30"}, [c for c in ALL if c != "C-2"]),
    ({"column": S.TEST_START, "op": "in", "value": ["2026-10-20 10:30", "2026-10-20 13:45"]}, ["C-2", "C-8"]),
    ({"column": S.TEST_START, "op": "is_null"}, ["C-6"]),
    ({"column": S.MATCHED_SKILLS, "op": "contains", "value": "java"}, ["C-1", "C-3"]),   # not JavaScript
    ({"column": S.MATCHED_SKILLS, "op": "contains", "value": ["Cloud", "Java"]}, ["C-1", "C-3", "C-7"]),
    ({"column": S.MATCHED_SKILLS, "op": "is_null"}, ["C-5", "C-8"]),
    ({"column": S.STATUS, "op": "in", "value": SHORTLISTED}, ["C-1", "C-2", "C-3", "C-4", "C-6"]),
    ({"column": S.CLUSTER_ID, "op": "not_null"}, ["C-2", "C-4", "C-6"]),
    ({"column": S.NAME, "op": "contains", "value": "menon"}, ["C-1"]),
])
def test_every_operator(df: pd.DataFrame, flt: dict[str, Any], expected: list[str]) -> None:
    table = ask.execute_plan({"filters": [flt], "columns": [S.CANDIDATE_ID]}, df)
    assert ids(table) == sorted(expected)


def test_filters_combine_with_and(df: pd.DataFrame) -> None:
    plan = {"filters": [{"column": S.CITY, "op": "==", "value": "Pune"},
                        {"column": S.STATUS, "op": "in", "value": SHORTLISTED},
                        {"column": S.CGPA, "op": ">", "value": 9}]}
    assert ids(ask.execute_plan(plan, df)) == ["C-1"]


def test_operator_aliases_and_fixups(df: pd.DataFrame) -> None:
    plan = ask.validate_plan({
        "filters": [{"column": "College", "op": "=", "value": "Riverside College"},
                    {"column": "cgpa", "op": "gte", "value": "8"},
                    {"column": "status", "op": "==", "value": [S.STATUS_SHORTLISTED]},
                    {"column": "Cluster", "op": "==", "value": None},
                    {"column": "final score", "op": "not in", "value": 1}],
        "metrics": [{"column": "final_score", "agg": "avg"}]}, df)
    assert [(f["column"], f["op"]) for f in plan["filters"]] == [
        (S.COLLEGE, "=="), (S.CGPA, ">="), (S.STATUS, "in"), (S.CLUSTER_ID, "is_null"), (S.FINAL_SCORE, "not_in")]
    assert plan["filters"][1]["value"] == 8 and plan["filters"][4]["value"] == [1]
    assert plan["metrics"] == [{"column": S.FINAL_SCORE, "agg": "mean", "as": "mean_final_score"}]


# ---------------------------------------------------------------------------
# Aggregations, grouping, sorting, limits
# ---------------------------------------------------------------------------
def test_every_aggregation_without_group_by(df: pd.DataFrame) -> None:
    plan = {"metrics": [{"column": S.FINAL_SCORE, "agg": agg, "as": agg} for agg in sorted(ask.ALLOWED_AGGS)]}
    table = ask.execute_plan(plan, df)
    assert len(table) == 1
    row, scores = table.iloc[0], df[S.FINAL_SCORE]
    assert row["count"] == 8 and row["nunique"] == 8
    assert row["sum"] == pytest.approx(scores.sum()) and row["mean"] == pytest.approx(scores.mean(), abs=0.01)
    assert row["median"] == pytest.approx(scores.median(), abs=0.01)
    assert row["min"] == 40.0 and row["max"] == 91.0


def test_aggregations_on_other_kinds(df: pd.DataFrame) -> None:
    counts = {"metrics": [{"agg": "count", "as": "rows"},
                          {"column": S.CGPA, "agg": "count", "as": "with_cgpa"},
                          {"column": S.COLLEGE, "agg": "nunique", "as": "colleges"},
                          {"column": S.CITY, "agg": "count", "as": "with_city"},
                          {"column": S.IS_FOCUS, "agg": "sum", "as": "perfect"},
                          {"column": S.IS_FOCUS, "agg": "mean", "as": "perfect_share"}]}
    row = ask.execute_plan(counts, df).iloc[0]
    assert row["rows"] == 8 and row["with_cgpa"] == 7 and row["colleges"] == 3 and row["with_city"] == 7
    assert row["perfect"] == 4 and row["perfect_share"] == 0.5
    extremes = {"metrics": [{"column": S.TEST_START, "agg": "min", "as": "first_start"},
                            {"column": S.TEST_START, "agg": "max", "as": "last_start"},
                            {"column": S.COLLEGE, "agg": "max", "as": "last_college"}]}
    row = ask.execute_plan(extremes, df).iloc[0]
    assert row["first_start"] == pd.Timestamp("2026-10-20 09:15")
    assert row["last_start"] == pd.Timestamp("2026-10-20 13:45")
    assert row["last_college"] == "Riverside College"
    with pytest.raises(ask.AskError, match="Too many entries in 'metrics'"):
        ask.validate_plan({"metrics": [{"agg": "count"}] * 9}, df)


def test_group_by_with_several_metrics(df: pd.DataFrame) -> None:
    plan = {"group_by": [S.COLLEGE],
            "metrics": [{"column": S.CANDIDATE_ID, "agg": "count", "as": "candidates"},
                        {"column": S.FINAL_SCORE, "agg": "mean", "as": "avg_score"}],
            "sort": [{"column": "avg_score", "desc": True}], "chart": "bar"}
    validated = ask.validate_plan(plan, df)
    assert validated["chart"] == "none"          # a bar chart needs exactly one metric
    table = ask.execute_plan(plan, df)
    assert list(table.columns) == [S.COLLEGE, "candidates", "avg_score"]
    assert table[S.COLLEGE].tolist() == ["Northfield Institute", "Lakeside University", "Riverside College", "(none)"]
    assert table["candidates"].tolist() == [3, 2, 2, 1]
    assert table["avg_score"].tolist() == pytest.approx([82.83, 73.75, 72.12, 40.0], abs=0.01)


def test_group_by_defaults_count_sort_and_chart(df: pd.DataFrame) -> None:
    plan = ask.validate_plan({"group_by": "risk_level"}, df)
    assert plan["metrics"] == [{"column": None, "agg": "count", "as": "count"}]
    assert plan["sort"] == [{"column": "count", "desc": True}] and plan["chart"] == "bar"
    table = ask.execute_plan(plan, df)
    assert table.to_dict("list") == {S.RISK_LEVEL: ["Clear", "High", "Low", "Medium"], "count": [4, 2, 1, 1]}


def test_group_by_two_columns_and_bool_keys(df: pd.DataFrame) -> None:
    plan = {"group_by": [S.IS_FOCUS, S.COLLEGE_TIER], "metrics": [{"agg": "count", "as": "n"}],
            "sort": [{"column": "is_focus", "desc": True}, {"column": "college_tier"}]}
    table = ask.execute_plan(plan, df)
    assert [tuple(r) for r in table.itertuples(index=False)] == [
        (True, "1", 2), (True, "2", 1), (True, "3", 1), (False, "1", 1), (False, "2", 1), (False, "3", 1),
        (False, "(none)", 1)]


def test_grouped_sort_resolves_metric_by_its_column(df: pd.DataFrame) -> None:
    plan = ask.validate_plan({"group_by": ["college"],
                              "metrics": [{"column": "final_score", "agg": "max", "as": "best"},
                                          {"agg": "count", "as": "n"}],
                              "sort": [{"column": "final_score", "desc": True}, {"column": "count"},
                                       {"column": "College", "direction": "desc"}]}, df)
    assert plan["sort"] == [{"column": "best", "desc": True}, {"column": "n", "desc": False},
                            {"column": S.COLLEGE, "desc": True}]


def test_list_sort_limit_total_and_sorted_column_shown(df: pd.DataFrame) -> None:
    plan = {"columns": [S.CANDIDATE_ID, S.NAME], "sort": [{"column": S.CGPA, "desc": True}], "limit": 3}
    table = ask.execute_plan(plan, df)
    assert table[S.CANDIDATE_ID].tolist() == ["C-3", "C-1", "C-6"]
    assert list(table.columns) == [S.CANDIDATE_ID, S.NAME, S.CGPA]
    assert table.attrs["total_rows"] == 8


def test_text_sort_ignores_case_and_puts_blanks_last(df: pd.DataFrame) -> None:
    table = ask.execute_plan({"columns": [S.CANDIDATE_ID, S.CITY], "sort": [{"column": S.CITY}]}, df)
    assert table[S.CITY].tolist() == ["Chennai", "Chennai", "Kolkata", "Pune", "Pune", "Pune", "pune", ""]


def test_default_list_columns_sort_and_limit(df: pd.DataFrame) -> None:
    plan = ask.validate_plan({"explanation": "Everyone"}, df)
    assert plan["columns"] == ask.DEFAULT_LIST_COLUMNS
    assert plan["columns"] == [S.CANDIDATE_ID, S.NAME, S.COLLEGE, S.FINAL_SCORE, S.HR_PCT, S.STATUS, S.RISK_LEVEL]
    assert plan["sort"] == [{"column": S.RANK, "desc": False}] and plan["limit"] == 50
    table = ask.execute_plan(plan, df)
    assert table[S.CANDIDATE_ID].tolist() == ["C-1", "C-6", "C-2", "C-3", "C-7", "C-4", "C-5", "C-8"]


@pytest.mark.parametrize("raw, expected", [(None, 50), (0, 50), (-5, 50), ("abc", 50), (True, 50), (10, 10),
                                           ("25", 25), (999.9, 999), (5000, 1000)])
def test_limit_default_and_cap(df: pd.DataFrame, raw: Any, expected: int) -> None:
    assert ask.validate_plan({"limit": raw}, df)["limit"] == expected


def test_limit_is_capped_at_execution() -> None:
    big = pd.DataFrame({S.CANDIDATE_ID: [f"X-{i:05d}" for i in range(1500)], S.FINAL_SCORE: np.linspace(0, 100, 1500)})
    table = ask.execute_plan({"columns": [S.CANDIDATE_ID], "limit": 100000}, big)
    assert len(table) == ask.MAX_LIMIT and table.attrs["total_rows"] == 1500
    assert table[S.CANDIDATE_ID].iloc[0] == "X-01499"             # default: best final score first


def test_validate_plan_is_idempotent(df: pd.DataFrame) -> None:
    plans = [
        {"filters": [{"column": "Test start time", "op": ">=", "value": "2026-10-20 10:15"},
                     {"column": "status", "op": "==", "value": [S.STATUS_SHORTLISTED, S.STATUS_REVIEW]}],
         "columns": ["candidate_id"], "sort": [{"column": "cgpa", "ascending": False}], "limit": "7",
         "explanation": "x"},
        {"group_by": ["college_tier"], "metrics": [{"column": "final_score", "agg": "mean"}, "count"]},
    ]
    for raw in plans:
        once = ask.validate_plan(raw, df)
        assert ask.validate_plan(once, df) == once
        json.dumps(once)      # JSON-safe for display


# ---------------------------------------------------------------------------
# Rejections and injection attempts
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("plan, message", [
    ({"columns": ["salary"]}, "Unknown column 'salary'"),
    ({"filters": [{"column": "salary", "op": ">", "value": 1}]}, "Unknown column"),
    ({"filters": [{"column": S.CGPA, "op": "between", "value": [1, 2]}]}, "Operator 'between' is not supported"),
    ({"filters": [{"column": S.CGPA, "op": "~=", "value": 1}]}, "not supported"),
    ({"metrics": [{"column": S.CGPA, "agg": "stddev"}]}, "Aggregation 'stddev' is not supported"),
    ({"metrics": [{"column": S.COLLEGE, "agg": "mean"}]}, "Cannot compute mean"),
    ({"metrics": [{"agg": "sum"}]}, "needs a column"),
    ({"filters": [{"column": S.CGPA, "op": "contains", "value": "9"}]}, "cannot be used"),
    ({"filters": [{"column": S.IS_FOCUS, "op": ">", "value": True}]}, "cannot be used"),
    ({"filters": [{"column": S.CGPA, "op": ">", "value": "high"}]}, "not a number"),
    ({"filters": [{"column": S.CGPA, "op": ">", "value": True}]}, "needs a number"),
    ({"filters": [{"column": S.CGPA, "op": ">", "value": [1, 2]}]}, "single value"),
    ({"filters": [{"column": S.TEST_START, "op": ">", "value": "not a date"}]}, "not a date"),
    ({"filters": [{"column": S.TEST_START, "op": ">", "value": 1700000000}]}, "needs a date"),
    ({"filters": [{"column": S.IS_FOCUS, "op": "==", "value": "maybe"}]}, "true/false"),
    ({"filters": [{"column": S.COLLEGE, "op": "==", "value": {"$ne": 1}}]}, "must be text"),
    ({"filters": [{"column": S.COLLEGE, "op": "==", "value": "x" * 201}]}, "too long"),
    ({"filters": [{"column": S.CGPA, "op": "in", "value": []}]}, "at least one value"),
    ({"filters": [{"column": S.COLLEGE, "op": "contains", "value": " "}]}, "needs some text"),
    ({"filters": "cgpa > 9"}, "must be an object"),
    ({"filters": {"column": S.CGPA}, "group_by": 5}, "must be a list"),
    ({"filters": [{"column": S.CGPA, "op": "not_null"}] * 21}, "Too many entries in 'filters'"),
    ({"group_by": ["q1_hash"]}, "cannot be used"),
    ({"columns": [S.RESUME_HASH]}, "cannot be used"),
    ({"filters": [{"column": "q1_hash", "op": "is_null"}]}, "cannot be used"),
    ({"metrics": [{"column": "q1_hash", "agg": "nunique"}]}, "cannot be used"),
    ({"group_by": [S.COLLEGE], "sort": [{"column": S.CGPA}]}, "Cannot sort by"),
    ({"unsupported": True, "explanation": "Needs salary data."}, "can't be answered.*Needs salary data"),
    ("not json", "valid JSON"),
    (["a list"], "JSON object"),
])
def test_invalid_plans_are_rejected(df: pd.DataFrame, plan: Any, message: str) -> None:
    with pytest.raises(ask.AskError, match=message):
        ask.validate_plan(plan, df)


def test_empty_frame_is_rejected() -> None:
    with pytest.raises(ask.AskError, match="no screening results"):
        ask.validate_plan({}, pd.DataFrame())


INJECTIONS = [
    "__import__('os').system('echo hi')",
    "final_score; import os",
    "final_score) | (df.eval('1')",
    "df['name']",
    "final_score.__class__",
    "@final_score",
    "`name`",
    "lambda: 1",
    "name or 1==1",
    "{final_score}",
    "../../etc/passwd",
    "final_score\nimport os",
    "name; DROP TABLE candidates",
]


def _plan_with_column(where: str, ref: str) -> dict[str, Any]:
    return {
        "columns": {"columns": [ref]},
        "group_by": {"group_by": [ref]},
        "filters": {"filters": [{"column": ref, "op": "==", "value": "1"}]},
        "metrics": {"metrics": [{"column": ref, "agg": "max"}]},
        "sort": {"sort": [{"column": ref, "desc": True}]},
    }[where]


@pytest.mark.parametrize("where", ["columns", "group_by", "filters", "metrics", "sort"])
@pytest.mark.parametrize("ref", INJECTIONS)
def test_code_in_column_names_is_rejected(df: pd.DataFrame, where: str, ref: str) -> None:
    with pytest.raises(ask.AskError, match="Unknown column"):
        ask.validate_plan(_plan_with_column(where, ref), df)


def test_values_are_data_and_nothing_is_evaluated(df: pd.DataFrame, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("dynamic evaluation must never be used")

    monkeypatch.setattr(pd.DataFrame, "query", boom)
    monkeypatch.setattr(pd.DataFrame, "eval", boom)
    monkeypatch.setattr(pd, "eval", boom)
    monkeypatch.setattr(builtins, "eval", boom)
    monkeypatch.setattr(builtins, "exec", boom)
    hostile = {"filters": [{"column": S.NAME, "op": "contains", "value": "__import__('os').system('echo hi')"},
                           {"column": S.COLLEGE, "op": "==", "value": "x' or '1'=='1"}]}
    assert ask.execute_plan(hostile, df).empty
    grouped = {"filters": [{"column": S.STATUS, "op": "in", "value": SHORTLISTED}], "group_by": [S.COLLEGE],
               "metrics": [{"column": S.FINAL_SCORE, "agg": agg} for agg in ("count", "mean", "max")]}
    assert len(ask.execute_plan(grouped, df)) == 3
    assert len(ask.execute_plan({"columns": [S.NAME], "sort": [{"column": S.CITY}]}, df)) == 8


def test_engine_source_has_no_dynamic_evaluation() -> None:
    source = inspect.getsource(ask)
    for pattern in (r"\beval\s*\(", r"\bexec\s*\(", r"\.query\s*\(", r"\.eval\s*\(", r"\bgetattr\s*\(",
                    r"__import__", r"\bimportlib\b"):
        assert not re.search(pattern, source), pattern


# ---------------------------------------------------------------------------
# plan_query + prompts
# ---------------------------------------------------------------------------
CANNED = {
    "filters": [{"column": "hackerrank_pct", "op": ">=", "value": 100},
                {"column": "status", "op": "in", "value": SHORTLISTED}],
    "group_by": ["college"], "metrics": [{"column": "candidate_id", "agg": "count", "as": "candidates"}],
    "columns": [], "sort": [{"column": "candidates", "desc": True}], "limit": 50, "chart": "bar",
    "explanation": "Counts shortlisted perfect scorers per college.",
}


def _catalogue_in(user_prompt: str) -> list[dict[str, Any]]:
    return json.loads(user_prompt.split("Columns:\n", 1)[1].rsplit("\n\nReturn", 1)[0])


def test_plan_query_returns_canned_plan_and_sends_only_the_catalogue(df: pd.DataFrame) -> None:
    client = FakeClient(CANNED)
    plan = ask.plan_query(EXAMPLE_QUESTIONS[0], df, client, AppConfig())
    assert plan == CANNED
    (call,) = client.calls
    assert call["required_keys"] == ("explanation",)
    assert f"Question: {EXAMPLE_QUESTIONS[0]}" in call["user"]
    assert _catalogue_in(call["user"]) == ask.describe_columns(df)
    assert "(8 rows)" in call["user"]
    for op in ask.ALLOWED_OPS | ask.ALLOWED_AGGS:
        assert op in call["system"]
    assert '"unsupported": true' in call["system"] and "Never invent" in call["system"]
    assert "Python, Java, SQL, Cloud, PyTorch" in call["system"]     # configured required skills


def test_plan_query_accepts_a_fenced_json_string(df: pd.DataFrame) -> None:
    client = FakeClient('```json\n{"columns": ["candidate_id"], "explanation": "IDs"}\n```')
    assert ask.plan_query("List candidate IDs", df, client) == {"columns": ["candidate_id"], "explanation": "IDs"}


@pytest.mark.parametrize("reply", [
    "{not json", "", "[1, 2]", ["a", "list"], 42, None,
    llm.LLMError("The model did not return valid JSON, even after a corrective re-ask."),
    RuntimeError("socket closed"),
])
def test_plan_query_bad_replies_raise_ask_error(df: pd.DataFrame, reply: Any) -> None:
    with pytest.raises(ask.AskError):
        ask.plan_query("Average final score by college tier", df, FakeClient(reply))


def test_plan_query_needs_data_question_and_client(df: pd.DataFrame) -> None:
    with pytest.raises(ask.AskError, match="not configured"):
        ask.plan_query("Average final score by college tier", df, None)
    with pytest.raises(ask.AskError, match="Type a question"):
        ask.plan_query("   \n ", df, FakeClient(CANNED))
    with pytest.raises(ask.AskError, match="no screening results"):
        ask.plan_query("Average final score by college tier", pd.DataFrame(), FakeClient(CANNED))


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", text.lower()))


def test_prompt_contains_no_personal_values(df: pd.DataFrame) -> None:
    client = FakeClient({"columns": ["candidate_id"], "explanation": "x"})
    typed = ["Show Asha Menon and rohan's scores",
             "Is meera.iyer@example.com shortlisted? Call +91 98765 43203 or check 49.36.12.4",
             "What did VIKRAM SHAH score? See https://reports.example.org/vikram"]
    for question in EXAMPLE_QUESTIONS + typed:
        ask.plan_query(question, df, client)
    personal = [str(v) for col in (S.NAME, S.EMAIL, S.PHONE, S.IP_ADDRESS, "extra_recruiter_notes")
                for v in df[col] if v]
    name_tokens = {w for name in NAMES for w in _words(name)}
    for call in client.calls:
        prompt = call["system"] + "\n" + call["user"]
        for value in personal:
            assert value not in prompt
        assert not name_tokens & _words(prompt), name_tokens & _words(prompt)
        assert "98765" not in prompt and "49.36" not in prompt and "reports.example.org" not in prompt


def test_typed_personal_values_are_masked_then_restored_locally(df: pd.DataFrame) -> None:
    def reply(system: str, user: str) -> dict[str, Any]:
        assert "[NAME_1]" in user and "[EMAIL_1]" in user and "[PHONE_1]" in user
        return {"filters": [{"column": "name", "op": "contains", "value": "[NAME_1]"},
                            {"column": "email", "op": "in", "value": ["[EMAIL_1]", "nobody@example.com"]},
                            {"column": "phone", "op": "==", "value": "PHONE_1"}],
                "columns": ["candidate_id", "name", "status"], "explanation": "Find [NAME_1]."}

    res = ask.answer("What is Asha Menon's status? Email asha.menon@example.com, phone +91 98765-43201", df,
                     FakeClient(reply))
    assert res.ok, res.error
    assert res.table[S.CANDIDATE_ID].tolist() == ["C-1"]
    assert res.plan["filters"][0]["value"] == "Asha Menon"
    assert res.plan["filters"][2]["value"] == "9876543201"
    assert res.plan["explanation"] == "Find Asha Menon."


@pytest.mark.parametrize("question, kept", [
    ("List shortlisted candidates from Pune with CGPA above 9", "Pune"),
    ("How many candidates from Northfield Institute are shortlisted?", "Northfield Institute"),
    ("Candidates who started after 2026-10-20 10:30:45", "2026-10-20 10:30:45"),
    ("Compare B.Tech and ASP.NET mentions for years 2024 2025 2026", "B.Tech and ASP.NET"),
])
def test_masking_leaves_ordinary_questions_alone(df: pd.DataFrame, question: str, kept: str) -> None:
    masked, mapping = ask.mask_question(question, df)
    assert masked == question and mapping == {} and kept in masked


def test_duplicated_column_labels_keep_masking_and_execution_working(df: pd.DataFrame) -> None:
    dup = pd.concat([df, df[[S.NAME]]], axis=1)              # a second "name" column
    masked, mapping = ask.mask_question("What is Asha Menon's score?", dup)
    assert masked == "What is [NAME_1]'s score?" and mapping == {"NAME_1": "Asha Menon"}
    assert [c["name"] for c in ask.describe_columns(dup)].count(S.NAME) == 1
    plan = {"filters": [{"column": S.NAME, "op": "contains", "value": "asha"}], "columns": [S.CANDIDATE_ID, S.NAME],
            "sort": [{"column": S.CGPA, "desc": "desc"}]}
    table = ask.execute_plan(plan, dup)
    assert table[S.CANDIDATE_ID].tolist() == ["C-1"] and list(table.columns) == [S.CANDIDATE_ID, S.NAME, S.CGPA]
    assert ask.validate_plan(plan, dup)["sort"] == [{"column": S.CGPA, "desc": True}]


def test_mask_question_numbers_placeholders_per_kind(df: pd.DataFrame) -> None:
    masked, mapping = ask.mask_question(
        "Compare asha menon, Rohan and ASHA MENON from 49.36.12.7 and github.com/someone/repo", df)
    assert masked == "Compare [NAME_1], [NAME_2] and [NAME_1] from [IP_1] and [URL_1]"
    assert mapping == {"NAME_1": "asha menon", "NAME_2": "Rohan", "IP_1": "49.36.12.7",
                       "URL_1": "github.com/someone/repo"}
    assert ask.restore_placeholders({"value": ["[name_2]", "IP_1", "[NAME_9]"]}, mapping) == {
        "value": ["Rohan", "49.36.12.7", "[NAME_9]"]}


# ---------------------------------------------------------------------------
# answer()
# ---------------------------------------------------------------------------
def test_answer_end_to_end_with_summary(df: pd.DataFrame) -> None:
    res = ask.answer(EXAMPLE_QUESTIONS[0], df, FakeClient(CANNED), AppConfig())
    assert res.ok and res.error == ""
    assert res.table.to_dict("list") == {S.COLLEGE: ["Northfield Institute", "Lakeside University",
                                                     "Riverside College"], "candidates": [2, 1, 1]}
    assert res.summary == "3 groups · grouped by College · 2 filters · sorted by candidates (highest first)"
    assert res.plan == ask.validate_plan(CANNED, df)


def test_answer_through_the_real_llm_client(df: pd.DataFrame, tmp_path: Any) -> None:
    cfg = AppConfig()
    cfg.llm.cache_dir = str(tmp_path / "llm-cache")
    reply = ('Here is the plan:\n```json\n{"group_by": ["risk_level"], "metrics": [{"column": "candidate_id", '
             '"agg": "count", "as": "candidates"}], "chart": "bar", "explanation": "Candidates per risk level."}\n```')
    transport = FakeTransport([reply])
    client = llm.LLMClient(cfg.llm, "test-key", client=transport)
    res = ask.answer("How many candidates per risk level?", df, client, cfg)
    assert res.ok, res.error
    assert dict(zip(res.table[S.RISK_LEVEL], res.table["candidates"])) == {"Clear": 4, "High": 2, "Low": 1,
                                                                           "Medium": 1}
    assert res.summary.startswith("4 groups · grouped by Risk level")
    sent = json.dumps(transport.calls[0]["messages"])
    for value in df[S.NAME].tolist() + df[S.EMAIL].tolist() + df[S.PHONE].tolist():
        assert value not in sent


def test_answer_reports_llm_failure(df: pd.DataFrame, tmp_path: Any) -> None:
    cfg = AppConfig()
    cfg.llm.cache_dir = ""
    client = llm.LLMClient(cfg.llm, "test-key", client=FakeTransport(["no json here", "still none"]))
    res = ask.answer("Average final score by college tier", df, client, cfg)
    assert not res.ok and res.table is None and "could not plan this question" in res.error


def test_answer_reports_unsupported_questions(df: pd.DataFrame) -> None:
    client = FakeClient({"unsupported": True, "explanation": "Salary data is not available."})
    res = ask.answer("What salary should we offer?", df, client)
    assert not res.ok and res.table is None
    assert res.error.startswith("This question can't be answered") and "Salary data is not available." in res.error
    assert res.plan["unsupported"] is True


def test_answer_reports_invalid_plans_and_keeps_them_for_display(df: pd.DataFrame) -> None:
    raw = {"columns": ["salary"], "explanation": "x"}
    res = ask.answer("salary?", df, FakeClient(raw))
    assert "Unknown column 'salary'" in res.error and res.plan == raw and res.table is None


def test_answer_never_raises(df: pd.DataFrame) -> None:
    class Weird:
        def chat_json(self, *args: Any, **kwargs: Any) -> Any:
            raise ValueError("boom")

    assert "not configured" in ask.answer(EXAMPLE_QUESTIONS[2], df, None).error
    assert ask.answer("  ", df, FakeClient(CANNED)).error
    assert "no screening results" in ask.answer(EXAMPLE_QUESTIONS[2], pd.DataFrame(), FakeClient(CANNED)).error
    assert "request failed (ValueError)" in ask.answer(EXAMPLE_QUESTIONS[2], df, Weird()).error


def test_summary_templates(df: pd.DataFrame) -> None:
    def summary(plan: dict[str, Any]) -> str:
        v = ask.validate_plan(plan, df)
        table = ask.execute_plan(v, df)
        return ask.summarize(v, table, table.attrs["total_rows"])

    assert summary({"metrics": [{"agg": "count", "as": "candidates"}]}) == "candidates: 8"
    assert summary({"metrics": [{"column": S.FINAL_SCORE, "agg": "mean", "as": "avg"}],
                    "filters": [{"column": S.CITY, "op": "==", "value": "Pune"}]}) == "avg: 78.62 · 1 filter"
    assert summary({"group_by": [S.RISK_LEVEL], "limit": 2}) == (
        "Top 2 of 4 groups · grouped by Risk level · sorted by count (highest first)")
    assert summary({"columns": [S.CANDIDATE_ID], "sort": [{"column": S.CGPA, "desc": True}], "limit": 3}) == (
        "8 matching rows, showing the first 3 · sorted by CGPA (highest first)")
    assert summary({"filters": [{"column": S.CGPA, "op": ">", "value": 10}]}) == "No matching rows · 1 filter"
    assert summary({"filters": [{"column": S.CANDIDATE_ID, "op": "==", "value": "C-1"}]}) == (
        "1 matching row · 1 filter")


# ---------------------------------------------------------------------------
# Realistic frame (60 candidates with every contract column)
# ---------------------------------------------------------------------------
def test_example_question_plans_on_a_realistic_frame() -> None:
    cands = make_fake_prepared_and_result()[1].candidates
    shortlisted = cands[S.STATUS].isin(SHORTLISTED)

    table = ask.execute_plan(CANNED, cands)
    expected = cands[shortlisted & (cands[S.HR_PCT] >= 100)].groupby(S.COLLEGE).size()
    assert dict(zip(table[S.COLLEGE], table["candidates"])) == expected.to_dict()
    assert table["candidates"].is_monotonic_decreasing

    high = ask.execute_plan({"filters": [{"column": "risk_level", "op": "==", "value": "High"}],
                             "group_by": ["college"], "metrics": [{"agg": "count", "as": "high_risk"}]}, cands)
    assert dict(zip(high[S.COLLEGE], high["high_risk"])) == (
        cands[cands[S.RISK_LEVEL] == S.RISK_HIGH].groupby(S.COLLEGE).size().to_dict())

    tiers = ask.execute_plan({"group_by": ["college_tier"],
                              "metrics": [{"column": "final_score", "agg": "mean", "as": "avg"}],
                              "sort": [{"column": "college_tier"}]}, cands)
    assert tiers["avg"].tolist() == pytest.approx(cands.groupby(S.COLLEGE_TIER)[S.FINAL_SCORE].mean().round(2).tolist())

    pune = ask.execute_plan({"filters": [{"column": "city", "op": "==", "value": "pune"},
                                         {"column": "status", "op": "in", "value": SHORTLISTED},
                                         {"column": "cgpa", "op": ">", "value": 9}],
                             "columns": ["candidate_id", "name", "cgpa"]}, cands)
    direct = cands[(cands[S.CITY] == "Pune") & shortlisted & (cands[S.CGPA] > 9)]
    assert sorted(pune[S.CANDIDATE_ID]) == sorted(direct[S.CANDIDATE_ID])

    review = ask.execute_plan({"filters": [{"column": "status", "op": "==", "value": S.STATUS_REVIEW}],
                               "group_by": ["cluster_id"], "metrics": [{"agg": "count", "as": "n"}]}, cands)
    assert review["n"].sum() == int((cands[S.STATUS] == S.STATUS_REVIEW).sum())

    cols = {c["name"]: c for c in ask.describe_columns(cands)}
    assert not any(ask.is_hidden_column(name) for name in cols)
    for col in cols.values():
        if ask.is_pii_column(col["name"]):
            assert "examples" not in col and "min" not in col
