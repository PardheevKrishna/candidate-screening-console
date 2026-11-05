"""Saved screening runs (screening/runs.py): round trip, listing, editing, path safety and comparison.

All data is synthetic (tests/ui_fixtures.py); every run is written to a pytest tmp folder.
"""
from __future__ import annotations

import gzip
import json
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

import screening
from screening import pipeline, runs
from screening import schema as S
from screening.config import AppConfig

try:
    from tests.ui_fixtures import make_fake_prepared_and_result
except ImportError:  # pragma: no cover - tests/ imported as rootdir-relative modules
    from ui_fixtures import make_fake_prepared_and_result

LLM_STATE: dict[str, Any] = {
    "rescore": {"CAND-00012": {"ok": True, "score": 0.72, "score_20": 14.4, "rationale": "Solid internship work.",
                               "highlights": ["Built data pipelines"], "source": "llm", "error": ""}},
    "explain": {"CAND-00003": {"ok": True, "summary": "Strong problem solver.", "strengths": ["DSA"], "gaps": [],
                               "interview_questions": ["Walk through q2"], "integrity_questions": ["Re-test q2"],
                               "source": "cache", "error": ""}},
    "clusters": {"C01": {"ok": False, "headline": "", "summary": "", "evidence_highlights": [],
                         "recommended_actions": [], "source": "", "error": "timeout"}},
    "executive": {"ok": True, "markdown": "## Brief\n- 15 shortlisted", "source": "llm", "error": ""},
}


@pytest.fixture
def fake() -> tuple[pipeline.PreparedData, Any]:
    return make_fake_prepared_and_result()


@pytest.fixture
def store(tmp_path: Path) -> runs.RunStore:
    return runs.RunStore(tmp_path / "runs")


def _evaluated(prepared: pipeline.PreparedData, base_cfg: AppConfig, **shortlist: Any) -> Any:
    cfg = AppConfig.from_dict(base_cfg.to_dict())
    for key, value in shortlist.items():
        setattr(cfg.shortlist, key, value)
    return pipeline.evaluate(prepared, cfg)


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------
def test_save_load_round_trip(store: runs.RunStore, fake: tuple[Any, Any]) -> None:
    prepared, result = fake
    run_id = store.save(prepared, result, "Campus drive - North", llm_state=LLM_STATE, dataset_label="Fake dataset")
    assert runs.RUN_ID_RE.match(run_id)
    p2, r2, llm, meta = store.load(run_id)

    pairs = [(r2.candidates, result.candidates), (p2.base, prepared.base), (p2.ta_df, prepared.ta_df),
             (p2.matches, prepared.matches), (r2.matches, prepared.matches)]
    pairs += [(getattr(r2.risk, name), getattr(result.risk, name)) for name in runs.RISK_FRAMES]
    for loaded, original in pairs:
        pd.testing.assert_frame_equal(loaded, original, check_dtype=False)
    assert list(r2.candidates.columns) == list(result.candidates.columns)

    assert p2.parsed == prepared.parsed and p2.parsed_by_candidate == prepared.parsed_by_candidate
    assert all(p2.parsed[p.file_hash] is p for p in p2.parsed_by_candidate.values())  # shared, like the pipeline
    assert r2.parsed_resumes is p2.parsed and r2.parsed_by_candidate is p2.parsed_by_candidate
    assert p2.quality == prepared.quality and r2.quality is p2.quality
    assert (r2.risk.focus_ids, r2.risk.params, r2.risk.warnings) == \
        (result.risk.focus_ids, result.risk.params, result.risk.warnings)
    assert isinstance(r2.config, AppConfig) and r2.config == result.config
    assert llm == LLM_STATE and r2.llm_results == result.llm_results
    assert (p2.mode, r2.mode, p2.timings, r2.timings) == (prepared.mode, result.mode, prepared.timings, result.timings)

    assert meta["label"] == "Campus drive - North" and meta["dataset"] == "Fake dataset"
    assert meta["app_version"] == screening.__version__ and meta["mode"] == "ta+resumes"
    assert meta["config"] == result.config.to_dict()
    assert {k: meta["counts"][k] for k in ("candidates", "shortlisted", "in_review", "clusters")} == \
        {"candidates": 60, "shortlisted": 15, "in_review": 2, "clusters": 2}


def test_evaluate_works_on_loaded_run(store: runs.RunStore, fake: tuple[Any, Any]) -> None:
    prepared, result = fake
    p2, r2, _, _ = store.load(store.save(prepared, result, "Re-tune me"))
    expected = pipeline.evaluate(prepared, result.config).candidates
    pd.testing.assert_frame_equal(pipeline.evaluate(p2, r2.config).candidates, expected, check_dtype=False)
    retuned = _evaluated(p2, r2.config, top_n=10)
    assert int(retuned.candidates[S.STATUS].isin(runs.IN_SHORTLIST).sum()) == 10


def test_only_json_and_parquet_files_are_written(store: runs.RunStore, fake: tuple[Any, Any]) -> None:
    prepared, result = fake
    run_id = store.save(prepared, result, "Format check", llm_state=LLM_STATE)
    files = [p for p in store.base_dir.rglob("*") if p.is_file()]
    assert {p.name for p in files} >= {"meta.json", "candidates.parquet", "base.parquet", "ta.parquet",
                                       "matches.parquet", "risk_meta.json", "quality.json", "parsed.json.gz",
                                       "llm.json", *(f"risk_{n}.parquet" for n in runs.RISK_FRAMES)}
    for path in files:
        assert path.suffix in {".json", ".parquet", ".gz"}, path.name
        assert not path.name.endswith((".pkl", ".pickle")), path.name
        assert path.read_bytes()[:4].startswith((b"PAR1", b"\x1f\x8b", b"{")), path.name
    payload = json.loads(gzip.decompress((store.base_dir / run_id / "parsed.json.gz").read_bytes()))
    assert set(payload["parsed"]) == set(prepared.parsed)
    assert "import pickle" not in Path(runs.__file__).read_text(encoding="utf-8")


def test_resume_only_run_and_missing_tables(store: runs.RunStore, fake: tuple[Any, Any]) -> None:
    prepared, result = fake
    prepared = replace(prepared, ta_df=None, mode="resumes_only")
    result = replace(result, mode="resumes_only", risk=replace(result.risk, clusters=None))
    p2, r2, llm, _ = store.load(store.save(prepared, result, "Resumes only"))
    assert p2.ta_df is None and r2.risk.clusters is None and llm == {}
    assert (p2.mode, r2.mode) == ("resumes_only", "resumes_only")
    pd.testing.assert_frame_equal(r2.risk.pairs, result.risk.pairs, check_dtype=False)


def test_parsed_by_candidate_rebuilt_from_resume_hash(store: runs.RunStore, fake: tuple[Any, Any]) -> None:
    prepared, result = fake
    run_id = store.save(prepared, result, "Links")
    path = store.base_dir / run_id / "parsed.json.gz"
    payload = json.loads(gzip.decompress(path.read_bytes()))
    payload.pop("by_candidate")
    path.write_bytes(gzip.compress(json.dumps(payload).encode("utf-8")))
    p2, r2, _, _ = store.load(run_id)
    assert p2.parsed_by_candidate == prepared.parsed_by_candidate
    assert r2.parsed_by_candidate is p2.parsed_by_candidate


def test_parquet_round_trip_of_awkward_frames(tmp_path: Path) -> None:
    odd = pd.DataFrame({
        "lists": [[1, 2], None, ["a", {"b": 1}]],
        "dicts": [{"k": 1.5}, {"$t": "looks like a tag"}, {}],
        "mixed": ["x", 1, None],
        "missing": pd.Series([None, np.nan, None], dtype=object),
        "tuples": [(1, 2), (3,), ()],
        "sets": [{1, 2}, set(), frozenset({"z"})],
        "stamps": pd.Series([pd.Timestamp("2026-01-01 10:00"), None, pd.Timestamp("2026-01-02")], dtype=object),
        "when": pd.to_datetime(["2026-01-01 10:00:00", None, "2026-01-03 00:00:00"]),
        "tz": pd.to_datetime(["2026-01-01 10:00+05:30"] * 3),
        "count": pd.array([1, None, 3], dtype="Int64"),
        "cat": pd.Categorical(["a", "b", "a"]),
        "flags": pd.Series([True, None, False], dtype=object),
        "money": [Decimal("1.10"), None, 2],
        "raw": [b"\x00\x01", b"", None],
    }, index=[10, 11, 12])
    runs.write_frame(odd, tmp_path / "odd.parquet")
    back = runs.read_frame(tmp_path / "odd.parquet")
    pd.testing.assert_frame_equal(back, odd)
    assert back.at[11, "dicts"] == {"$t": "looks like a tag"} and back.at[12, "sets"] == frozenset({"z"})
    assert back["mixed"].tolist() == ["x", 1, None] and back.at[10, "tuples"] == (1, 2)

    for frame in (pd.DataFrame([[1, "a", 2.0]], columns=["x", "x", 3]),
                  pd.DataFrame([[1, 2]], columns=pd.MultiIndex.from_tuples([("a", "b"), ("a", "c")], names=["l1", "l2"])),
                  pd.DataFrame(), pd.DataFrame(columns=["a", "b"]), pd.DataFrame({"s": pd.Series([], dtype="str")})):
        runs.write_frame(frame, tmp_path / "frame.parquet")
        pd.testing.assert_frame_equal(runs.read_frame(tmp_path / "frame.parquet"), frame, check_dtype=False)


# ---------------------------------------------------------------------------
# Listing and editing
# ---------------------------------------------------------------------------
def test_list_rename_delete(store: runs.RunStore, fake: tuple[Any, Any]) -> None:
    empty = store.list_runs()
    assert empty.empty and list(empty.columns) == runs.LIST_COLUMNS
    assert not store.base_dir.exists()  # listing never creates the folder

    prepared, result = fake
    first = store.save(prepared, result, "First")
    second = store.save(prepared, _evaluated(prepared, result.config, top_n=10), "Second")
    (store.base_dir / "notes").mkdir()                                   # ignored: not a run id
    (store.base_dir / "20260101-000000-abcdef.txt").write_text("x")     # ignored: not a folder
    listed = store.list_runs()
    assert list(listed.columns) == runs.LIST_COLUMNS
    assert listed["run_id"].tolist() == [second, first]                  # newest first
    assert listed["label"].tolist() == ["Second", "First"]
    assert listed["shortlisted"].tolist() == [10, 15] and listed["candidates"].tolist() == [60, 60]
    assert (listed["size_mb"] > 0).all() and listed["created_at"].notna().all()

    assert store.rename(first, "  Renamed\n\tdrive  ") == "Renamed drive"
    assert store.list_runs().set_index("run_id").at[first, "label"] == "Renamed drive"
    assert store.rename(first, "   ") == runs.DEFAULT_LABEL
    assert len(store.rename(first, "x" * 500)) == runs.MAX_LABEL_LEN

    store.delete(second)
    assert store.list_runs()["run_id"].tolist() == [first]
    assert not (store.base_dir / second).exists()
    assert not [p for p in store.base_dir.iterdir() if p.name.startswith((".tmp-", ".trash-"))]
    for call in (store.delete, store.load, lambda r: store.rename(r, "x")):
        with pytest.raises(runs.RunStoreError, match="not found"):
            call(second)


def test_invalid_and_traversal_run_ids_are_rejected(store: runs.RunStore, fake: tuple[Any, Any],
                                                   tmp_path: Path) -> None:
    prepared, result = fake
    run_id = store.save(prepared, result, "Real run")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "meta.json").write_text("{}")
    bad: list[Any] = ["", ".", "..", "../outside", "..\\outside", f"{run_id}/../../outside", f"../runs/{run_id}",
                      f"{run_id}/", f"{run_id}\n", f" {run_id}", f"{run_id}:stream", "/etc/passwd", "C:\\Windows",
                      str(outside), "20261105-101010-ABCDEF", "20261105-101010-abcde", "20261105-101010-abcdef0",
                      "２０２６１１０５-101010-abcdef", None, 20261105, ["x"]]
    calls = (store.load, store.delete, store.read_meta, lambda r: store.rename(r, "hijacked"),
             lambda r: store.compare_runs(run_id, r), lambda r: store.compare_runs(r, run_id))
    for value in bad:
        for call in calls:
            with pytest.raises(runs.RunStoreError):
                call(value)
    assert store.list_runs()["label"].tolist() == ["Real run"]
    assert (outside / "meta.json").read_text() == "{}"


def test_damaged_runs_are_reported_not_raised(store: runs.RunStore, fake: tuple[Any, Any]) -> None:
    prepared, result = fake
    broken = "20260101-000000-abcdef"
    (store.base_dir / broken).mkdir(parents=True)
    (store.base_dir / broken / "meta.json").write_text("{not json")
    row = store.list_runs().set_index("run_id").loc[broken]
    assert row["label"] == "(unreadable run)" and pd.isna(row["candidates"])
    with pytest.raises(runs.RunStoreError, match="could not be read"):
        store.load(broken)
    store.delete(broken)  # still removable from the UI

    run_id = store.save(prepared, result, "Will lose a file")
    (store.base_dir / run_id / "base.parquet").unlink()
    with pytest.raises(runs.RunStoreError, match="incomplete"):
        store.load(run_id)

    newer = store.save(prepared, result, "From the future")
    meta_path = store.base_dir / newer / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta_path.write_text(json.dumps({**meta, "format_version": runs.FORMAT_VERSION + 1}), encoding="utf-8")
    with pytest.raises(runs.RunStoreError, match="newer version"):
        store.load(newer)


def test_saved_config_cannot_redirect_the_llm(store: runs.RunStore, fake: tuple[Any, Any]) -> None:
    prepared, result = fake
    run_id = store.save(prepared, result, "Tampered")
    meta_path = store.base_dir / run_id / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["config"]["llm"].update(base_url="https://elsewhere.example.net/v1", backend_factory="os:system",
                                 api_key_env="HOME", cache_dir="/tmp/x")
    meta["config"]["shortlist"]["top_n"] = 12
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    cfg = store.load(run_id)[1].config
    default = AppConfig().llm
    assert (cfg.llm.base_url, cfg.llm.backend_factory, cfg.llm.api_key_env, cfg.llm.cache_dir) == \
        (default.base_url, default.backend_factory, default.api_key_env, default.cache_dir)
    assert cfg.shortlist.top_n == 12  # tuning values still restored


def test_failed_save_leaves_nothing_behind(store: runs.RunStore, fake: tuple[Any, Any],
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    prepared, result = fake
    real_write = runs.write_frame
    calls = {"n": 0}

    def flaky(df: pd.DataFrame, path: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError(28, "No space left on device", str(path))
        real_write(df, path)

    monkeypatch.setattr(runs, "write_frame", flaky)
    with pytest.raises(runs.RunStoreError, match="No space left on device") as err:
        store.save(prepared, result, "Disk full")
    assert str(store.base_dir) not in str(err.value) and "parquet" not in str(err.value)
    assert store.list_runs().empty
    assert list(store.base_dir.iterdir()) == []  # the work folder was removed


def test_save_requires_prepared_data_and_result(store: runs.RunStore, fake: tuple[Any, Any]) -> None:
    prepared, result = fake
    with pytest.raises(runs.RunStoreError, match="prepared data"):
        store.save(None, result, "x")  # type: ignore[arg-type]
    with pytest.raises(runs.RunStoreError, match="no results"):
        store.save(prepared, None, "x")  # type: ignore[arg-type]
    assert not store.base_dir.exists()


def test_base_dir_from_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(runs.ENV_RUNS_DIR, str(tmp_path / "custom"))
    assert runs.RunStore().base_dir == (tmp_path / "custom").resolve()
    assert runs.RunStore(tmp_path / "explicit").base_dir == (tmp_path / "explicit").resolve()
    monkeypatch.setenv(runs.ENV_RUNS_DIR, "   ")
    assert runs.RunStore().base_dir == runs.DEFAULT_RUNS_DIR.resolve()
    monkeypatch.delenv(runs.ENV_RUNS_DIR)
    assert runs.RunStore().base_dir == runs.DEFAULT_RUNS_DIR.resolve()


def test_clean_label() -> None:
    assert runs.clean_label("  Drive\x00 A\r\nretest ") == "Drive A retest"
    assert runs.clean_label(None) == runs.DEFAULT_LABEL and runs.clean_label("", "fallback") == "fallback"
    assert len(runs.clean_label("y" * 300)) == runs.MAX_LABEL_LEN


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------
def test_compare_runs(store: runs.RunStore, fake: tuple[Any, Any]) -> None:
    prepared, result = fake
    before = _evaluated(prepared, result.config)            # top 15 (fixture config)
    after = _evaluated(prepared, result.config, top_n=10)
    run_a, run_b = store.save(prepared, before, "Top 15"), store.save(prepared, after, "Top 10")
    comparison = store.compare_runs(run_a, run_b)
    diff, (meta_a, meta_b) = comparison["diff"], comparison["meta"]
    summary = diff["summary"]
    assert (summary["shortlisted_before"], summary["shortlisted_after"], summary["entering"], summary["leaving"]) == \
        (15, 10, 0, 5)
    ranks = before.candidates.set_index(S.CANDIDATE_ID)[S.RANK]
    assert set(diff["leaving"][S.CANDIDATE_ID]) == set(ranks[(ranks > 10) & (ranks <= 15)].index)
    assert diff["entering"].empty and summary["cutoff_after"] >= summary["cutoff_before"]
    assert comparison["config_changes"] == [("shortlist.top_n", 15, 10)]
    assert (meta_a["label"], meta_b["label"]) == ("Top 15", "Top 10")
    assert store.compare_runs(run_a, run_a)["diff"]["summary"]["leaving"] == 0


def test_basic_diff_fallback(monkeypatch: pytest.MonkeyPatch, fake: tuple[Any, Any]) -> None:
    prepared, result = fake
    before = _evaluated(prepared, result.config).candidates
    after = _evaluated(prepared, result.config, top_n=10, min_cgpa=8.0).candidates
    basic = runs.basic_shortlist_diff(before, after)
    assert set(basic) == {"entering", "leaving", "status_changes", "rank_moves", "summary"}
    for key in ("entering", "leaving", "status_changes", "rank_moves"):
        assert list(basic[key].columns) == runs.DIFF_COLUMNS
    s = basic["summary"]
    assert s["shortlisted_before"] - s["leaving"] + s["entering"] == s["shortlisted_after"] == 10
    assert s["leaving"] == len(basic["leaving"]) and s["status_changes"] == len(basic["status_changes"])
    assert basic["leaving"]["shortlisted_before"].all() and not basic["leaving"]["shortlisted_after"].any()

    monkeypatch.setitem(sys.modules, "screening.analytics", None)  # analytics unavailable -> fallback
    fallback = runs.shortlist_diff(before, after)
    assert fallback["summary"] == s
    monkeypatch.setattr(runs, "_analytics_diff", lambda: lambda b, a: 1 / 0)  # broken helper -> fallback
    assert runs.shortlist_diff(before, after)["summary"] == s
    monkeypatch.undo()

    analytics_fn = runs._analytics_diff()
    if analytics_fn is not None:  # same answers as the analytics module, whenever it is present
        other = analytics_fn(before, after)
        core = ("shortlisted_before", "shortlisted_after", "entering", "leaving", "cutoff_before", "cutoff_after",
                "review_before", "review_after")
        assert {k: other["summary"][k] for k in core} == {k: s[k] for k in core}
        for key in ("entering", "leaving"):
            assert other[key][S.CANDIDATE_ID].tolist() == basic[key][S.CANDIDATE_ID].tolist()


def test_config_changes_flattening() -> None:
    a, b = AppConfig().to_dict(), AppConfig().to_dict()
    b["shortlist"]["top_n"] = 100
    b["risk"]["pair_weights"]["same_ip"] = 5.0
    b["skills"]["required_skills"] = ["Python"]
    changes = runs.config_changes(a, b)
    assert ("shortlist.top_n", 300, 100) in changes and ("risk.pair_weights.same_ip", 20.0, 5.0) in changes
    assert ("skills.required_skills", a["skills"]["required_skills"], ["Python"]) in changes and len(changes) == 3
    assert runs.config_changes(a, a) == []
    assert runs.config_changes({"x": 1, "n": {"y": 2}}, {"n": {"y": 3}, "z": 0}) == \
        [("x", 1, None), ("n.y", 2, 3), ("z", None, 0)]
    assert runs.flatten_config({"a": {"b": {"c": 1}}, "d": {}}) == {"a.b.c": 1, "d": {}}
