"""Sidebar tests (AppTest + pure helpers): GitHub Copilot proxy status, recent datasets (snapshot
cache), the column-mapping editor and saved mapping profiles.

The engine is faked where a run would start (tests/ui_fixtures.py); the proxy status check is a fake,
the snapshot cache lives in a pytest tmp folder, and no real token, connection file or .env file of
this machine is ever read. All people and addresses are fictional.
"""
from __future__ import annotations

import hashlib
import io
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import streamlit
from streamlit.testing.v1 import AppTest
from streamlit.testing.v1.element_tree import ElementTree

from screening import env, ingest, pipeline, snapshots
from screening import llm_backends as backends
from screening import schema as S
from screening.config import AppConfig, LLMConfig
from ui import sidebar, state, tab_shortlist

try:
    from tests.ui_fixtures import make_fake_prepared_and_result
except ImportError:  # pragma: no cover - tests/ imported as rootdir-relative modules
    from ui_fixtures import make_fake_prepared_and_result

APP = str(Path(__file__).resolve().parent.parent / "app.py")
KEY_ENV = LLMConfig().api_key_env
TOKEN = "sidebar-test-token-value"
FILE_TOKEN = "0f" * 32                      # shaped like the token the VS Code extension publishes
CONNECTED = {"reachable": True, "models": ["gpt-4o", "gpt-4.1", "gemini-2.5-pro", "o4-mini"],
             "error": "", "base_url": "http://127.0.0.1:5757/v1", "token_source": "configured"}
NOT_RUNNING = {"reachable": False, "models": [], "base_url": "http://127.0.0.1:5757/v1", "token_source": "configured",
               "error": f"Could not reach the Copilot proxy at http://127.0.0.1:5757/v1. {backends.PROXY_GUIDANCE}"}
HEADERS = ["Job Application", "Candidate ID", "Email", "School", "Location", "Score", "Max Score", "Recruiter Notes"]
CSV = ("\n".join([
    ",".join(HEADERS),
    "Asha Menon (asha menon),C-1000042,asha.menon@mail.example.com,Northfield Institute of Technology,Pune,175,175,"
    "Strong systems design answers in the panel round",
    "Rohan Iyer (rohan iyer),C-1000043,rohan.iyer@mail.example.com,Lakeside University,Chennai,140,175,",
    "Meera Das (meera das),C-1000044,meera.das@mail.example.com,Northfield Institute of Technology,Pune,96,175,Follow up",
    "Kavya Nair (kavya nair),C-1000045,kavya.nair@mail.example.com,Riverside College of Engineering,Hyderabad,175,175,",
]) + "\n").encode("utf-8")
SUGGESTED = {"Job Application": S.JOB_APPLICATION, "Candidate ID": S.CANDIDATE_ID, "Email": S.EMAIL,
             "School": S.COLLEGE, "Location": S.CITY, "Score": S.HR_SCORE, "Max Score": S.MAX_SCORE}
WORKDAY_HEADER = ["Job Application", "Candidate ID", "Assessment Comment", "Email", "CGPA", "Branch", "School",
                  "Location", "Max Score", "Score", "Cutoff Score", "Cutoff Score Percentage", "Percentage Score",
                  "Candidate Option"]


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
class FakeProxy:
    """Stands in for llm_backends.proxy_status: records the tokens it gets, returns ``status``."""

    def __init__(self) -> None:
        self.calls: list[str | None] = []
        self.status: dict[str, Any] = dict(CONNECTED)

    def __call__(self, cfg: Any, token: str | None = None, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(token)
        return dict(self.status)


def _patch(monkeypatch: pytest.MonkeyPatch, module: Any, name: str, value: Any) -> None:
    monkeypatch.setitem(vars(module), name, value)


@pytest.fixture(autouse=True)
def proxy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeProxy:
    """Isolation: no token, connection file, .env file or sample folder of this machine; snapshots in tmp."""
    monkeypatch.delenv(KEY_ENV, raising=False)
    _patch(monkeypatch, backends, "discovery_path", lambda: tmp_path / "no-proxy-connection.json")
    _patch(monkeypatch, env, "DEFAULT_ENV_FILE", tmp_path / "no.env")
    _patch(monkeypatch, snapshots, "PROJECT_ROOT", tmp_path / "project")
    _patch(monkeypatch, pipeline, "DEFAULT_SAMPLE_DIR", tmp_path / "no-sample")
    fake = FakeProxy()
    _patch(monkeypatch, backends, "proxy_status", fake)
    return fake


@pytest.fixture
def runs(monkeypatch: pytest.MonkeyPatch) -> list[state.RunRequest]:
    """state.run_screening replaced by a recorder (the sidebar's RunRequest is what is tested)."""
    requests: list[state.RunRequest] = []

    def record(req: state.RunRequest) -> bool:
        requests.append(req)
        return False

    _patch(monkeypatch, state, "run_screening", record)
    return requests


def _app(cfg: AppConfig | None = None, **values: Any) -> AppTest:
    at = AppTest.from_file(APP, default_timeout=60)
    at.secrets[KEY_ENV] = ""  # never read a real secrets.toml during tests
    if cfg is not None:
        at.session_state[state.K_CONFIG] = cfg
    for key, value in values.items():
        at.session_state[key] = value
    return at


def _clean(at: AppTest) -> None:
    assert not at.exception, [e.value for e in at.exception]


def _text(at: AppTest) -> str:
    bar = at.sidebar
    return "\n".join(e.value for e in [*bar.markdown, *bar.caption, *bar.warning, *bar.info, *bar.error,
                                       *bar.success])


def _keys(at: AppTest) -> list[str]:
    return [b.key for b in at.sidebar.button]


def _upload(at: AppTest, name: str = "drive.csv", content: bytes = CSV) -> AppTest:
    at.run()
    at.radio(key="sb_source").set_value(sidebar.SOURCE_UPLOAD).run()
    at.file_uploader(key="sb_ta_file").set_value((name, content, "text/csv")).run()
    _clean(at)
    return at


def _editor(at: AppTest) -> Any:
    return next(d for d in at.dataframe if (d.key or "").startswith("sb_mapping_"))


def _edit(monkeypatch: pytest.MonkeyPatch, at: AppTest, edits: dict[int, dict[str, Any]]) -> None:
    """Send data-editor edits with the next runs, as the browser would."""
    editor_id = _editor(at).proto.id
    payload = json.dumps({"edited_rows": {str(k): v for k, v in edits.items()}, "added_rows": [], "deleted_rows": []})
    real = ElementTree.get_widget_states

    def with_edits(tree: ElementTree) -> Any:
        states = real(tree)
        widget = states.widgets.add()
        widget.id = editor_id
        widget.string_value = payload
        return states

    monkeypatch.setattr(ElementTree, "get_widget_states", with_edits)


def _row(frame: pd.DataFrame, source: str) -> dict[str, Any]:
    return frame.set_index(sidebar.COL_SOURCE).loc[source].to_dict()


def _workday_xlsx() -> bytes:
    from openpyxl import Workbook

    book = Workbook()
    summary = book.active
    summary.title = "Sheet1"
    summary.append(["Review Candidates: R-100200 Technology Program Intern"])
    summary.append(["Active Candidates", "2 of 2"])
    data = book.create_sheet("Sheet2")
    data.append(WORKDAY_HEADER)
    data.append(["Asha Menon (asha menon)", "C-1000042", "Hackerrank Completed – Qualified", "asha.menon@mail.example.com",
                 8.9, "Computer Science", "Northfield Institute of Technology", "Pune", 175, 175, 96, 55, 100,
                 "Django (Python)"])
    data.append(["Rohan Iyer (rohan iyer)", "C-1000043", "Hackerrank Completed – Not Qualified",
                 "rohan.iyer@mail.example.com", 7.4, "Information Technology", "Lakeside University", "Chennai",
                 175, 70, 96, 55, 40, "Spring Boot (Java)"])
    buf = io.BytesIO()
    book.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# LLM status (GitHub Copilot through the VS Code proxy)
# ---------------------------------------------------------------------------
def test_llm_status_connected_lists_models_and_only_the_token_source(monkeypatch: pytest.MonkeyPatch,
                                                                      proxy: FakeProxy) -> None:
    monkeypatch.setenv(KEY_ENV, TOKEN)
    at = _app().run()
    _clean(at)
    text = _text(at)
    assert any(m.value.startswith(":green-badge[") and "GitHub Copilot (VS Code proxy)" in m.value
               for m in at.sidebar.markdown)
    assert ("**Connected** · 4 models: `gpt-4o`, `gpt-4.1`, `gemini-2.5-pro` +1 more · "
            "token from environment") in text
    assert "o4-mini" not in text
    assert sidebar.LLM_POLICY in text
    assert TOKEN not in text and proxy.calls == [TOKEN]
    stored = json.dumps({k: at.session_state[k] for k in at.session_state if k.startswith("_llm")}, default=str)
    assert TOKEN not in stored
    assert "sb_llm_recheck" in _keys(at)


def test_llm_status_not_running_shows_the_error_in_amber(monkeypatch: pytest.MonkeyPatch, proxy: FakeProxy) -> None:
    monkeypatch.setenv(KEY_ENV, TOKEN)
    proxy.status = dict(NOT_RUNNING)
    at = _app().run()
    _clean(at)
    assert any(m.value.startswith(":orange-badge[") for m in at.sidebar.markdown)
    text = _text(at)
    assert "**Not connected** - Could not reach the Copilot proxy at `http://127.0.0.1:5757/v1`." in text
    assert "Screening Copilot proxy extension" in text and "token from environment" in text


def test_llm_status_without_any_token_needs_no_network(proxy: FakeProxy) -> None:
    at = _app().run()
    _clean(at)
    text = _text(at)
    assert "**Not connected**" in text
    assert "Open VS Code with the Screening Copilot Proxy extension running" in text
    assert "token from" not in text and proxy.calls == []


def test_llm_status_with_the_connection_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                             proxy: FakeProxy) -> None:
    connection = tmp_path / "proxy-connection.json"
    connection.write_text(json.dumps({"token": FILE_TOKEN, "base_url": "http://127.0.0.1:5757/v1", "pid": 4242,
                                      "started_at": "2026-11-09T09:00:00Z", "version": "1.0.0"}), encoding="utf-8")
    _patch(monkeypatch, backends, "discovery_path", lambda: connection)
    at = _app().run()
    _clean(at)
    text = _text(at)
    assert "**Connected**" in text and "token from connection file" in text
    assert FILE_TOKEN not in text and proxy.calls == [FILE_TOKEN]


def test_llm_status_when_switched_off(monkeypatch: pytest.MonkeyPatch, proxy: FakeProxy) -> None:
    monkeypatch.setenv(KEY_ENV, TOKEN)
    cfg = AppConfig()
    cfg.llm.enabled = False
    at = _app(cfg).run()
    _clean(at)
    assert any(m.value.startswith(":gray-badge[") for m in at.sidebar.markdown)
    assert "LLM features off" in _text(at)
    assert "sb_llm_recheck" not in _keys(at) and proxy.calls == []


def test_check_again_refreshes_the_cached_status(monkeypatch: pytest.MonkeyPatch, proxy: FakeProxy) -> None:
    monkeypatch.setenv(KEY_ENV, TOKEN)
    _patch(monkeypatch, state, "_STATUS_TTL_SEC", 3600.0)  # a slow machine must not expire the cached status
    proxy.status = dict(NOT_RUNNING)
    at = _app().run()
    assert proxy.calls == [TOKEN]
    at.run()
    assert proxy.calls == [TOKEN]                  # cached for 30 s: page loads do not wait for the proxy
    proxy.status = dict(CONNECTED)
    at.button(key="sb_llm_recheck").click().run()
    _clean(at)
    assert proxy.calls == [TOKEN, TOKEN]           # one check for the click, not one per call site
    assert "**Connected**" in _text(at)
    at.run()
    assert len(proxy.calls) == 2


def test_llm_status_note_formats() -> None:
    color, note = sidebar.llm_status_note(True, {"reachable": True, "models": ["gpt-4o"], "token_source": "secrets"})
    assert color == "green" and note == "**Connected** · 1 model: `gpt-4o` · token from secrets"
    color, note = sidebar.llm_status_note(True, {"reachable": True, "models": "gpt-4o", "error": ""})
    assert color == "orange" and note.startswith("**Connected, but no models**")
    _, note = sidebar.llm_status_note(True, {"reachable": False, "error": "Proxy said [click](https://x.example) *now*"})
    assert r"\[click\]\(`https://x.example`\)" in note and r"\*now\*" in note
    _, note = sidebar.llm_status_note(True, None)
    assert note.startswith("**Not connected**") and "Screening Copilot proxy extension" in note
    assert sidebar.llm_status_note(False, {"reachable": True, "models": ["gpt-4o"]})[0] == "gray"
    _, note = sidebar.llm_status_note(True, {"reachable": True, "models": ["a`b", "x" * 80]})
    assert "`ab`" in note and "x" * 40 not in note


# ---------------------------------------------------------------------------
# Recent datasets (fast reload)
# ---------------------------------------------------------------------------
@pytest.fixture
def cached() -> tuple[str, Any, Any]:
    prepared, result = make_fake_prepared_and_result()
    fp = hashlib.sha256(b"north-campus-drive").hexdigest()
    assert snapshots.save_snapshot(prepared, fp, "Upload: north_campus.xlsx", AppConfig()) is not None
    return fp, prepared, result


def test_recent_datasets_lists_cached_datasets(cached: tuple[str, Any, Any]) -> None:
    fp = cached[0]
    closed = _app().run()
    _clean(closed)
    assert sidebar.RECENT_LABEL in [e.label for e in closed.sidebar.expander]
    assert f"sb_snap_load_{fp[:12]}" not in _keys(closed)       # collapsed: the cache is not read
    at = _app(**{sidebar.K_RECENT: True}).run()
    _clean(at)
    text = _text(at)
    assert "Upload: north\\_campus.xlsx" in text
    assert "60 candidates · 57 resumes" in text and " MB · expires " in text
    assert "cached locally under `data/cache` for 7 days (contains candidate data; never committed)" in text
    assert {f"sb_snap_load_{fp[:12]}", f"sb_snap_del_{fp[:12]}"} <= set(_keys(at))


def test_recent_dataset_loads_without_reparsing(cached: tuple[str, Any, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    fp, _, result = cached
    _patch(monkeypatch, pipeline, "evaluate", lambda prep, cfg, *, risk_result=None: replace(result, config=cfg))

    def no_parsing(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a cached dataset must not be parsed again")

    _patch(monkeypatch, pipeline, "prepare_data", no_parsing)
    _patch(monkeypatch, tab_shortlist, "render", lambda res: None)   # this test is about the sidebar
    at = _app(**{sidebar.K_RECENT: True}).run()
    at.button(key=f"sb_snap_load_{fp[:12]}").click().run()
    _clean(at)
    assert at.session_state[state.K_PREPARED_FP] == fp
    assert at.session_state[state.K_DATASET] == "Upload: north_campus.xlsx (cached)"
    assert any("from the cache" in t.value for t in at.toast)
    assert "Total Processed" in "\n".join(h.value for h in at.html)
    button = at.button(key=f"sb_snap_load_{fp[:12]}")
    assert (button.label, button.disabled) == ("Loaded", True)       # the dataset in use is marked


def test_recent_dataset_load_returns_a_snapshot_request(cached: tuple[str, Any, Any],
                                                        runs: list[state.RunRequest]) -> None:
    fp = cached[0]
    at = _app(**{sidebar.K_RECENT: True}).run()
    at.button(key=f"sb_snap_load_{fp[:12]}").click().run()
    _clean(at)
    (req,) = runs
    assert (req.label, req.snapshot_fp, req.ta_file, req.resume_docs) == ("Upload: north_campus.xlsx (cached)", fp,
                                                                         None, [])


def test_recent_dataset_delete(cached: tuple[str, Any, Any]) -> None:
    fp = cached[0]
    at = _app(**{sidebar.K_RECENT: True}).run()
    at.button(key=f"sb_snap_del_{fp[:12]}").click().run()
    _clean(at)
    assert snapshots.list_snapshots(AppConfig()).empty
    assert f"sb_snap_load_{fp[:12]}" not in _keys(at)
    assert any("Removed cached dataset" in t.value for t in at.toast)
    assert "No cached datasets yet" in _text(at)


def test_recent_datasets_hidden_when_snapshots_are_off(cached: tuple[str, Any, Any]) -> None:
    cfg = AppConfig()
    cfg.snapshots.enabled = False
    at = _app(cfg, **{sidebar.K_RECENT: True}).run()
    _clean(at)
    assert sidebar.RECENT_LABEL not in [e.label for e in at.sidebar.expander]
    assert not any(k.startswith("sb_snap_") for k in _keys(at))


@pytest.mark.parametrize("listing", [None, pd.DataFrame(), pd.DataFrame({"label": ["x"]})])
def test_recent_datasets_tolerate_missing_listings(monkeypatch: pytest.MonkeyPatch, listing: Any) -> None:
    _patch(monkeypatch, state, "recent_datasets", lambda cfg=None: listing)
    at = _app(**{sidebar.K_RECENT: True}).run()
    _clean(at)
    assert "No cached datasets yet" in _text(at)


def test_recent_rows_and_cache_note() -> None:
    good = "ab" * 32
    frame = pd.DataFrame({
        "fingerprint": [good, "not-hex", good[:12] + "0" * 20, None, "cd" * 32],
        "label": ["Drive A", "x", "dup", "y", None],
        "created_at": [pd.Timestamp("2026-11-09 10:15"), None, None, None, pd.NaT],
        "candidates": pd.array([2133, 1, 1, 1, None], dtype="Int64"),
        "resumes": pd.array([1870, 1, 1, 1, 1], dtype="Int64"),
        "size_mb": [12.34, 1.0, 1.0, 1.0, float("nan")],
        "expires_at": [pd.Timestamp("2026-11-16 10:15"), None, None, None, None],
    })
    rows = sidebar.recent_rows(frame)
    assert [r.fingerprint for r in rows] == [good, "cd" * 32]   # invalid and colliding fingerprints skipped
    assert rows[0].short == good[:12]
    assert rows[0].details == ("Cached 09 Nov 2026 10:15 · 2,133 candidates · 1,870 resumes · 12.3 MB · "
                               "expires 16 Nov 2026 10:15")
    assert rows[1].label == snapshots.DEFAULT_LABEL and rows[1].details == "Cached at an unknown time · 1 resume"
    assert sidebar.recent_rows(None) == [] and sidebar.recent_rows(pd.DataFrame()) == []
    cfg = AppConfig()
    assert "under `data/cache` for 7 days" in sidebar.cache_note(cfg.snapshots)
    cfg.snapshots.cache_dir, cfg.snapshots.ttl_days = str(Path.cwd().resolve() / "cache"), 1
    note = sidebar.cache_note(cfg.snapshots)
    assert "in the app's cache folder for 1 day" in note and str(Path.cwd().resolve()) not in note


# ---------------------------------------------------------------------------
# Column-mapping editor
# ---------------------------------------------------------------------------
def test_mapping_editor_shows_samples_and_the_detected_mapping(runs: list[state.RunRequest]) -> None:
    at = _upload(_app())
    ta = at.file_uploader(key="sb_ta_file")
    assert "Workday" in ta.help and "Sheet2" in ta.help and "HackerRank CSV/XLSX" in ta.help
    resumes = at.file_uploader(key="sb_resumes")
    assert "Resume.zip" in resumes.help and "split per candidate" in resumes.help and "PDF/DOCX/TXT" in resumes.help
    frame = _editor(at).value
    assert list(frame.columns) == sidebar.MAPPING_COLUMNS
    assert frame[sidebar.COL_SOURCE].tolist() == HEADERS
    assert dict(zip(frame[sidebar.COL_SOURCE], frame[sidebar.COL_TARGET])) == {**SUGGESTED, "Recruiter Notes": ""}
    assert set(frame[sidebar.COL_ACTION]) == {sidebar.ACTION_INCLUDE}
    assert _row(frame, "School")[sidebar.COL_SAMPLES] == ("Northfield Institute of Techn… | Lakeside University | "
                                                          "Riverside College of Engineer…")
    assert _row(frame, "Recruiter Notes")[sidebar.COL_SAMPLES] == "Strong systems design answers… | Follow up"
    assert all(len(v) <= sidebar.SAMPLE_WIDTH for s in frame[sidebar.COL_SAMPLES] for v in s.split(" | "))
    assert _row(frame, "Max Score")[sidebar.COL_SAMPLES] == "175"
    assert any("7 of 8 columns mapped" in c.value for c in at.sidebar.caption)
    assert not any("Sheet used" in c.value for c in at.sidebar.caption)   # CSV: no sheet caption
    at.button(key="sb_run_upload").click().run()
    _clean(at)
    (req,) = runs
    assert req.mapping_override is None and req.ta_file == ("drive.csv", CSV) and req.label == "Upload: drive.csv"


def test_mapping_editor_ignore_and_retarget_build_the_override(monkeypatch: pytest.MonkeyPatch,
                                                                runs: list[state.RunRequest]) -> None:
    at = _upload(_app())
    _edit(monkeypatch, at, {HEADERS.index("Location"): {sidebar.COL_ACTION: sidebar.ACTION_IGNORE},
                            HEADERS.index("Recruiter Notes"): {sidebar.COL_TARGET: S.ASSESSMENT_COMMENT}})
    at.run()
    _clean(at)
    assert any("7 of 8 columns mapped, 1 ignored" in c.value for c in at.sidebar.caption)
    at.button(key="sb_run_upload").click().run()
    _clean(at)
    (req,) = runs
    expected = {k: v for k, v in SUGGESTED.items() if k != "Location"} | {"Recruiter Notes": S.ASSESSMENT_COMMENT}
    assert req.mapping_override == expected


def test_mapping_editor_warns_about_duplicate_and_missing_fields(monkeypatch: pytest.MonkeyPatch,
                                                                 runs: list[state.RunRequest]) -> None:
    at = _upload(_app())
    _edit(monkeypatch, at, {HEADERS.index("Score"): {sidebar.COL_ACTION: sidebar.ACTION_IGNORE},
                            HEADERS.index("Recruiter Notes"): {sidebar.COL_TARGET: S.EMAIL}})
    at.run()
    _clean(at)
    warnings = " ".join(w.value for w in at.sidebar.warning)
    assert "Each field can be mapped once; ignored: Recruiter Notes → Email \\(email\\)" in warnings
    assert "Not mapped: HackerRank Score" in warnings
    at.button(key="sb_run_upload").click().run()
    (req,) = runs
    assert S.HR_SCORE not in req.mapping_override.values() and "Recruiter Notes" not in req.mapping_override


def test_ignoring_every_column_falls_back_to_auto_mapping(monkeypatch: pytest.MonkeyPatch,
                                                          runs: list[state.RunRequest]) -> None:
    at = _upload(_app())
    _edit(monkeypatch, at, {i: {sidebar.COL_ACTION: sidebar.ACTION_IGNORE} for i in range(len(HEADERS))})
    at.run()
    _clean(at)
    assert any("auto-detected mapping will be used instead" in w.value for w in at.sidebar.warning)
    at.button(key="sb_run_upload").click().run()
    assert runs[0].mapping_override is None


def test_workday_workbook_names_the_sheet_used() -> None:
    at = _upload(_app(), "test_scores_export.xlsx", _workday_xlsx())
    assert any("Sheet used: **Sheet2**" in c.value and "2 sheets" in c.value for c in at.sidebar.caption)
    frame = _editor(at).value
    mapped = dict(zip(frame[sidebar.COL_SOURCE], frame[sidebar.COL_TARGET]))
    assert mapped["Job Application"] == S.JOB_APPLICATION and mapped["Assessment Comment"] == S.ASSESSMENT_COMMENT
    assert mapped["Cutoff Score Percentage"] == S.CUTOFF_PCT and mapped["Candidate Option"] == S.CANDIDATE_OPTION
    assert _row(frame, "Cutoff Score")[sidebar.COL_SAMPLES] == "96"


def test_detect_sheet() -> None:
    xlsx = _workday_xlsx()
    assert sidebar.detect_sheet(xlsx, WORKDAY_HEADER) == ("Sheet2", 2)
    assert sidebar.detect_sheet(xlsx, ["Totally", "Different", "Headers"]) is None
    assert sidebar.detect_sheet(CSV, HEADERS) is None
    assert sidebar.detect_sheet(b"PK\x03\x04 not really a workbook", HEADERS) is None
    assert sidebar.detect_sheet(xlsx, []) is None


def test_mapping_helpers() -> None:
    options = sidebar.field_options(["q2_score", "q10_hash", "not_a_field", S.EMAIL])
    assert options[0] == sidebar.NOT_MAPPED and options[-2:] == ["q2_score", "q10_hash"]
    assert {S.JOB_APPLICATION, S.ASSESSMENT_COMMENT, S.CANDIDATE_OPTION, S.CUTOFF_SCORE, S.CUTOFF_PCT} <= set(options)
    assert "not_a_field" not in options
    assert sidebar.field_label("") == "— not mapped —"
    assert sidebar.field_label(S.CUTOFF_PCT) == "Cutoff Score % (cutoff_pct)"
    assert sidebar.field_label("q3_similarity") == "Q3 code similarity % (q3_similarity)"
    assert sidebar.sample_values(pd.Series(["a", None, "a", " ", "b\n c", "d", "e"])) == "a | b c | d"
    assert sidebar.sample_values(pd.Series([None, None])) == ""
    comments = pd.Series(["#N/A", "Hackerrank Completed – Qualified&#xa;Integrity Issues: No Issues Detected",
                          "Hackerrank Completed – Qualified&#xa;Integrity Issues: High", "N/A",
                          "Hackerrank Completed – Not Qualified&#xa;Integrity Issues: No Issues Detected",
                          "Research &amp; Development"])
    assert sidebar.sample_values(comments) == ("Hackerrank Completed – Qualif… | Hackerrank Completed – Not Qu… | "
                                               "Research & Development")
    rows = sidebar.mapping_rows(["A", "B", "C"], {"A": "1"}, {"A": S.EMAIL, "B": S.NAME},
                                {"B": sidebar.PROFILE_IGNORE, "C": S.CITY})
    assert rows.to_dict("records") == [
        {"Source column": "A", "Sample values": "1", "Maps to": S.EMAIL, "Action": "Include"},
        {"Source column": "B", "Sample values": "", "Maps to": S.NAME, "Action": "Ignore"},
        {"Source column": "C", "Sample values": "", "Maps to": S.CITY, "Action": "Include"}]
    assert sidebar.mapping_from_rows(rows) == ({"A": S.EMAIL, "C": S.CITY}, [])
    assert sidebar.mapping_profile(rows) == {"version": 1, "mapping": {"A": S.EMAIL, "B": "ignore", "C": S.CITY}}
    assert sidebar.mapping_from_rows(pd.DataFrame()) == ({}, [])


# ---------------------------------------------------------------------------
# Saved mapping profiles (JSON download / upload)
# ---------------------------------------------------------------------------
def test_mapping_profile_download(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict[str, dict[str, Any]] = {}
    real = streamlit.download_button

    def recording(label: str, data: Any = None, file_name: str | None = None, mime: str | None = None,
                  key: str | None = None, **kwargs: Any) -> bool:
        recorded[str(key)] = {"label": label, "data": data, "file_name": file_name, "mime": mime}
        return real(label, data=data, file_name=file_name, mime=mime, key=key, **kwargs)

    monkeypatch.setattr(streamlit, "download_button", recording)
    at = _upload(_app())
    _edit(monkeypatch, at, {HEADERS.index("Location"): {sidebar.COL_ACTION: sidebar.ACTION_IGNORE}})
    at.run()
    _clean(at)
    saved = recorded[sidebar.K_PROFILE_DOWNLOAD]
    assert (saved["label"], saved["file_name"], saved["mime"]) == ("Download mapping (JSON)", "column_mapping.json",
                                                                   "application/json")
    assert json.loads(saved["data"]) == {"version": 1, "mapping": {**SUGGESTED, "Location": "ignore",
                                                                   "Recruiter Notes": "ignore"}}


def test_mapping_profile_upload_is_applied(runs: list[state.RunRequest]) -> None:
    at = _upload(_app())
    profile = {"version": 1, "mapping": {"Score": "ignore", "Recruiter Notes": "assessment_comment",
                                         "Location": "City", "Phone Number": "phone"}}
    at.file_uploader(key=sidebar.K_PROFILE_UPLOAD).set_value(
        ("column_mapping.json", json.dumps(profile).encode(), "application/json")).run()
    _clean(at)
    assert any("Applied mapping profile column\\_mapping.json to 3 columns" in s.value for s in at.sidebar.success)
    infos = " ".join(i.value for i in at.sidebar.info)
    assert "Not in this file, skipped: Phone Number" in infos and "5 columns of this file are not in the profile" in infos
    frame = _editor(at).value
    assert _editor(at).key.endswith("_v1")
    assert _row(frame, "Score")[sidebar.COL_ACTION] == sidebar.ACTION_IGNORE
    assert _row(frame, "Recruiter Notes")[sidebar.COL_TARGET] == S.ASSESSMENT_COMMENT
    at.run()  # applied once per uploaded file: later runs keep the table (and any edits)
    assert _editor(at).key.endswith("_v1")
    at.button(key="sb_run_upload").click().run()
    _clean(at)
    expected = {k: v for k, v in SUGGESTED.items() if k != "Score"} | {"Recruiter Notes": S.ASSESSMENT_COMMENT}
    assert runs[0].mapping_override == expected


@pytest.mark.parametrize("content, message", [
    (b"{not json", "not valid JSON"),
    (json.dumps({"version": 2, "mapping": {}}).encode(), "Unsupported profile version (2)"),
    (json.dumps({"mapping": {}}).encode(), "Unsupported profile version (missing)"),
    (json.dumps({"version": 1, "mapping": {"Score": "points"}}).encode(), "Unknown target field(s): points"),
    (json.dumps({"version": 1, "mapping": {"Score": " "}}).encode(), 'Unknown target field(s): ""'),
    (json.dumps({"version": 1, "mapping": {"Score": 5}}).encode(), "must map a column header (text)"),
    (json.dumps({"version": 1, "mapping": ["Score"]}).encode(), 'no "mapping" object'),
    (json.dumps([1, 2]).encode(), 'Expected a JSON object'),
    (b"\xff\xfe\x00bad", "not UTF-8 text"),
    (b"[" * 70000, "larger than 64 KB"),
    # Too deep: older Pythons stop parsing (RecursionError), newer ones parse it and the nested
    # value is rejected as not text. Either way the profile is refused.
    (b'{"version": 1, "mapping": {"a": ' + b"[" * 5000 + b"]" * 5000 + b"}}",
     ("not valid JSON", "must map a column header (text)")),
])
def test_parse_mapping_profile_rejects_bad_files(content: bytes, message: str | tuple[str, ...]) -> None:
    result = sidebar.parse_mapping_profile(content, HEADERS)
    expected = (message,) if isinstance(message, str) else message
    assert not result.ok and any(m in result.error for m in expected) and not result.assignments


def test_parse_mapping_profile_accepts_canonical_fields_and_reports_unknown_headers() -> None:
    profile = {"version": 1, "mapping": {" Candidate  ID ": "candidate_id", "Score": "IGNORE", "Email": "Email",
                                         "Q2 Marks": "q2_score", "Nope": "city"}}
    result = sidebar.parse_mapping_profile(json.dumps(profile).encode("utf-8-sig"), [*HEADERS, "Q2 Marks"])
    assert result.ok and result.skipped == ["Nope"]
    assert result.assignments == {"Candidate ID": S.CANDIDATE_ID, "Score": "ignore", "Email": S.EMAIL,
                                  "Q2 Marks": "q2_score"}
    assert sidebar.parse_mapping_profile(None, HEADERS).error


@pytest.mark.parametrize("content, message", [
    (b"{not json", "not valid JSON"),
    (json.dumps({"version": 2, "mapping": {}}).encode(), "Unsupported profile version"),
    (json.dumps({"version": 1, "mapping": {"Score": "points"}}).encode(), "Unknown target field"),
    (b" " * (64 * 1024 + 1), "larger than 64 KB"),
])
def test_bad_mapping_profile_upload_is_reported_and_not_applied(content: bytes, message: str,
                                                                runs: list[state.RunRequest]) -> None:
    at = _upload(_app())
    at.file_uploader(key=sidebar.K_PROFILE_UPLOAD).set_value(("column_mapping.json", content,
                                                              "application/json")).run()
    _clean(at)
    errors = " ".join(e.value for e in at.sidebar.error)
    assert "Could not load the mapping profile" in errors and message in errors
    assert not _editor(at).key.endswith("_v1")                       # the table is unchanged
    at.button(key="sb_run_upload").click().run()
    assert runs[0].mapping_override is None


def test_sample_source_caption(runs: list[state.RunRequest]) -> None:
    at = _app().run()
    _clean(at)
    assert any(sidebar.SAMPLE_CAPTION in c.value for c in at.sidebar.caption)
    assert at.button(key="sb_run_sample").disabled                   # no sample folder in tests
    assert any("generate_mock_data.py" in w.value for w in at.sidebar.warning)


def test_suggest_mapping_contract_used_by_the_editor() -> None:
    """The editor relies on ingest's public helpers; Workday headers map to the new fields."""
    mapping = ingest.suggest_column_mapping(WORKDAY_HEADER)
    assert {mapping[h] for h in ("Job Application", "Assessment Comment", "Candidate Option", "Cutoff Score",
                                 "Cutoff Score Percentage")} == {S.JOB_APPLICATION, S.ASSESSMENT_COMMENT,
                                                                 S.CANDIDATE_OPTION, S.CUTOFF_SCORE, S.CUTOFF_PCT}
    assert all(ingest.is_canonical_field(v) for v in mapping.values())
