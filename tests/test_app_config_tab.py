"""UI tests (AppTest) for the Config & Rubric Tuning tab: presets, missing-resume policy, tie-break
order, duplicates & exclusions, college tier scoring and the tier list, team-project risk settings,
the dataset cache, the LLM section (GitHub Copilot via the VS Code proxy) and tuning profiles.

The engine is faked with tests/ui_fixtures.py (scores are re-shortlisted with the real
scoring.apply_shortlist). No network is used: the proxy status and the LLM client are fakes, the
connection file is never read, and the dataset cache and saved runs live under a pytest tmp folder.
All names, IDs and colleges are fictional.
"""
from __future__ import annotations

import io
import json
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from screening import llm, pipeline, runs, scoring, snapshots
from screening import llm_backends as backends
from screening import schema as S
from screening.config import PRESETS, TIE_BREAK_KEYS, AppConfig
from screening.models import DataQualityReport, PipelineResult, RiskResult
from ui import state, tab_config

try:
    from tests.ui_fixtures import make_fake_prepared_and_result
except ImportError:  # pragma: no cover - tests/ imported as rootdir-relative modules
    from ui_fixtures import make_fake_prepared_and_result

APP = str(Path(__file__).resolve().parent.parent / "app.py")
FP = "fake-fingerprint"
KEY_ENV = AppConfig().llm.api_key_env           # env var / secret name of the proxy token, from the config
TOKEN = "5e" * 32                               # fake proxy token: must never reach the page
BASE_URL = AppConfig().llm.base_url
TIER_CSV = (b"college,tier,aliases\n"
            b"Northfield Institute of Technology,1,NIT Northfield;Northfield Tech\n"
            b"Riverside College of Engineering,2,\n"
            b"Lakeside University,Premier,\n"
            b",3,\n")


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
def _patch(monkeypatch: pytest.MonkeyPatch, module: Any, name: str, value: Any) -> None:
    monkeypatch.setitem(vars(module), name, value)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(KEY_ENV, raising=False)
    monkeypatch.setenv(runs.ENV_RUNS_DIR, str(tmp_path / "runs"))
    _patch(monkeypatch, pipeline, "DEFAULT_SAMPLE_DIR", tmp_path / "no-sample")
    _patch(monkeypatch, backends, "discover_proxy", lambda *args, **kwargs: None)  # no real connection file
    _patch(monkeypatch, snapshots, "PROJECT_ROOT", tmp_path)                        # cache under tmp_path


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake run shortlisted by the real apply_shortlist; evaluate() re-shortlists with the given config."""
    prepared, fake = make_fake_prepared_and_result()
    result = replace(fake, candidates=scoring.apply_shortlist(fake.candidates, fake.config))
    calls: list[AppConfig] = []

    def evaluate(prep: Any, cfg: AppConfig, *, risk_result: Any = None) -> PipelineResult:
        calls.append(cfg)
        return replace(result, candidates=scoring.apply_shortlist(result.candidates, cfg), config=cfg)

    _patch(monkeypatch, pipeline, "evaluate", evaluate)
    return {"prepared": prepared, "result": result, "calls": calls}


@pytest.fixture
def proxy(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A token in the environment, a fake proxy status and a fake LLM client (no network)."""
    monkeypatch.setenv(KEY_ENV, TOKEN)
    outcome: dict[str, Any] = {"ok": True, "message": "Connected to GitHub Copilot (VS Code proxy): model chat-model-a "
                                                      "(0.4 s).", "raise": None, "tests": 0}

    def proxy_status(llm_cfg: Any, token: Any = None, **kwargs: Any) -> dict[str, Any]:
        return {"reachable": True, "models": ["chat-model-a", "chat-model-b"], "error": "", "base_url": BASE_URL,
                "token_source": "configured"}

    class FakeClient:
        def __init__(self, cfg: Any, api_key: str | None, client: Any = None) -> None:
            self.cfg = cfg

        def test_connection(self) -> tuple[bool, str]:
            outcome["tests"] += 1
            if outcome["raise"] is not None:
                raise outcome["raise"]
            return outcome["ok"], outcome["message"]

    _patch(monkeypatch, backends, "proxy_status", proxy_status)
    _patch(monkeypatch, llm, "LLMClient", FakeClient)
    return outcome


def _app() -> AppTest:
    at = AppTest.from_file(APP, default_timeout=60)
    at.secrets[KEY_ENV] = ""  # never read a real secrets.toml during tests
    return at


def _inject(at: AppTest, prepared: Any, result: PipelineResult, cfg: AppConfig | None = None) -> AppTest:
    cfg = cfg or result.config
    at.session_state[state.K_CONFIG] = cfg
    at.session_state[state.K_PREPARED] = prepared
    at.session_state[state.K_PREPARED_FP] = FP
    at.session_state[state.K_RESULT] = replace(result, config=cfg)
    at.session_state[state.K_RESULT_KEY] = state.eval_key(FP, cfg)
    at.session_state[state.K_DATASET] = "Fake dataset"
    at.session_state[state.K_TABS] = state.TAB_CONFIG
    return at


def _open(engine: dict[str, Any], cfg: AppConfig | None = None, prepared: Any = None) -> AppTest:
    at = _inject(_app(), prepared or engine["prepared"], engine["result"], cfg).run()
    _clean(at)
    return at


def _clean(at: AppTest) -> None:
    assert not at.exception, [e.value for e in at.exception]


def _k(at: AppTest) -> str:
    return f"cfg{at.session_state[state.K_CONFIG_VERSION]}"


def _cfg(at: AppTest) -> AppConfig:
    return at.session_state[state.K_CONFIG]


def _apply(at: AppTest) -> AppTest:
    at.button(key="cfg_apply").click().run()
    _clean(at)
    return at


def _texts(at: AppTest) -> list[str]:
    out: list[str] = []
    for elements in (at.markdown, at.caption, at.info, at.warning, at.error, at.success, at.toast):
        out += [str(e.value) for e in elements]
    return out


def _widget_values(at: AppTest) -> list[str]:
    out: list[str] = []
    for widgets in (at.text_input, at.text_area, at.number_input, at.selectbox, at.multiselect, at.slider):
        for w in widgets:
            out += [str(w.value), str(getattr(w, "label", "")), str(getattr(w, "help", ""))]
            out += [str(o) for o in getattr(w, "options", [])]
    return out


def _table(at: AppTest, column: str) -> pd.DataFrame:
    return next(d.value for d in at.dataframe if column in d.value.columns)


def _clean_cfg(engine: dict[str, Any]) -> AppConfig:
    return AppConfig.from_dict(engine["result"].config.to_dict())


# ---------------------------------------------------------------------------
# Presets & missing-resume policy
# ---------------------------------------------------------------------------
def test_presets_fill_the_weights_and_apply_on_confirm(engine: dict[str, Any]) -> None:
    at = _open(engine)
    k = _k(at)
    assert {at.button(key=f"cfg_preset_{name}").label for name in PRESETS} == \
        {"With Resumes (40/30/20/10)", "No Resumes (80/0/0/20)"}
    at.button(key="cfg_preset_no_resumes").click().run()
    _clean(at)
    assert [at.number_input(key=f"{k}_w_{n}").value for n in ("hackerrank", "skills", "experience", "education")] \
        == [80.0, 0.0, 0.0, 20.0]
    assert _cfg(at).weights.hackerrank == 40.0                      # nothing applied yet
    assert any("No Resumes (80/0/0/20)" in t.value for t in at.toast)
    _apply(at)
    w = _cfg(at).weights
    assert (w.hackerrank, w.skills, w.experience, w.education) == (80.0, 0.0, 0.0, 20.0)
    assert engine["calls"][-1].weights.hackerrank == 80.0
    k = _k(at)
    at.button(key="cfg_preset_with_resumes").click().run()
    _clean(at)
    assert at.number_input(key=f"{k}_w_skills").value == 30.0 and _cfg(at).weights.skills == 0.0


def test_missing_resume_policy_select_and_what_auto_means(engine: dict[str, Any]) -> None:
    at = _open(engine)
    k = _k(at)
    box = at.selectbox(key=f"{k}_no_resume")
    assert box.value == "auto" and len(box.options) == 3
    assert box.help and "renormalize to HackerRank + Education when the dataset has no resumes" in box.help
    assert any("auto means **zero**" in c.value for c in at.caption)          # dataset has resumes
    box.set_value("renormalize")
    _apply(at)
    assert _cfg(at).shortlist.missing_resume_policy == "renormalize"
    assert any("The applied policy is **renormalize**" in c.value for c in at.caption)

    ta_only = replace(engine["prepared"], mode="ta_only")
    at = _open(engine, prepared=ta_only)
    assert any("TA feed only" in c.value and "auto means **renormalize**" in c.value for c in at.caption)


# ---------------------------------------------------------------------------
# Tie-breaks, duplicates & exclusions
# ---------------------------------------------------------------------------
def test_tie_break_order_drops_unknown_keys_and_keeps_the_picked_order(engine: dict[str, Any]) -> None:
    cfg = _clean_cfg(engine)
    cfg.shortlist.tie_break_order = ["bogus", "cgpa", "", "cgpa", "hackerrank_pct"]
    at = _open(engine, cfg)
    k = _k(at)
    assert any("bogus" in w.value for w in at.warning)                        # validate() names it
    picker = at.multiselect(key=f"{k}_tie_breaks")
    assert picker.value == ["cgpa", "hackerrank_pct"]
    assert picker.options == [label for label, _ in TIE_BREAK_KEYS.values()]
    assert any(tab_config.TIE_BREAK_NOTE in c.value for c in at.caption)
    picker.set_value(["submission_time", "cgpa", "coding_marks"])
    _apply(at)
    assert _cfg(at).shortlist.tie_break_order == ["submission_time", "cgpa", "coding_marks"]
    assert not any("tie-break rule" in w.value for w in at.warning)


def test_exclusions_are_normalised_and_unknown_ids_are_warned(engine: dict[str, Any]) -> None:
    at = _open(engine)
    k = _k(at)
    assert at.text_area(key=f"{k}_exclude").label == tab_config.EXCLUDE_LABEL
    at.text_area(key=f"{k}_exclude").set_value("cand-00003, CAND-00005\nCAND-00005; C-1000042  x-77")
    _apply(at)
    expected = ["CAND-00003", "CAND-00005", "C-1000042", "X-77"]
    assert _cfg(at).shortlist.exclude_candidates == expected
    assert engine["calls"][-1].shortlist.exclude_candidates == expected
    warning = next(w.value for w in at.warning if "not in the current dataset" in w.value)
    assert "C-1000042, X-77" in warning and "CAND-00003" not in warning
    names = engine["result"].candidates.set_index(S.CANDIDATE_ID)[S.NAME]
    assert not any(names[c] in warning for c in ("CAND-00003", "CAND-00005"))   # IDs only, never names
    assert any("2 candidate(s) in the current dataset are disqualified" in c.value for c in at.caption)
    status = at.session_state[state.K_RESULT].candidates.set_index(S.CANDIDATE_ID)[S.STATUS]
    assert status["CAND-00003"] == S.STATUS_INELIGIBLE and status["CAND-00005"] == S.STATUS_INELIGIBLE
    assert at.text_area(key=f"{_k(at)}_exclude").value == "\n".join(expected)


def test_duplicate_toggle_and_dataset_note(engine: dict[str, Any]) -> None:
    prepared = engine["prepared"]
    duplicate_of = [""] * len(prepared.base)
    duplicate_of[9] = "CAND-00009"                                            # CAND-00010 repeats CAND-00009
    marked = replace(prepared, base=prepared.base.assign(**{S.DUPLICATE_OF: duplicate_of}))
    at = _open(engine, prepared=marked)
    k = _k(at)
    box = at.checkbox(key=f"{k}_dedupe")
    assert box.value is True and box.label == tab_config.DEDUPE_LABEL
    assert any("1 duplicate application(s) of 1 candidate(s)" in c.value and "Ineligible" in c.value
               for c in at.caption)
    box.uncheck()
    _apply(at)
    assert _cfg(at).shortlist.auto_disqualify_duplicates is False
    assert any("scored like any other application" in c.value for c in at.caption)


# ---------------------------------------------------------------------------
# College tier scoring & tier list
# ---------------------------------------------------------------------------
def _without_feed_tiers(engine: dict[str, Any]) -> Any:
    prepared = engine["prepared"]
    return replace(prepared, base=prepared.base.drop(columns=[S.COLLEGE_TIER]))


def test_tier_toggle_upload_use_preview_and_clear(engine: dict[str, Any]) -> None:
    at = _open(engine, prepared=_without_feed_tiers(engine))
    k = _k(at)
    version = at.session_state[state.K_CONFIG_VERSION]
    toggle = at.toggle(key=f"{k}_college_tier_on")
    assert toggle.label == "Score college tier" and toggle.value is False
    assert any(tab_config.TIER_NOTE in i.value for i in at.info)              # visible fairness note
    assert at.slider(key=f"{k}_college_tier_threshold").value == 90
    assert at.button(key="cfg_tier_clear").disabled and at.download_button(key="cfg_tier_dl").disabled
    preview = _table(at, "source")
    assert set(preview["source"]) == {"No match"} and len(preview) == 5      # no list yet, no feed tiers

    at.file_uploader(key="cfg_tier_upload").set_value(("tiers.csv", TIER_CSV, "text/csv")).run()
    _clean(at)
    assert any("Read 3 college(s) and 2 alias(es)" in s.value for s in at.success)
    assert any("1 row(s) without a college or a tier were skipped" in w.value for w in at.warning)
    assert any("Premier" in w.value and "give no tier points" in w.value for w in at.warning)
    preview = _table(at, "source").set_index("college")
    assert preview.loc["Northfield Institute of Technology", ["tier", "source"]].tolist() == ["1", "Tier list"]
    assert preview.loc["Lakeside University", "tier"] == "Premier"
    assert preview.loc["Hillcrest Engineering College", "source"] == "No match"
    assert any("uploaded list (not in use yet)" in c.value and "2 of 5 colleges have no tier" in c.value
               and tab_config.UNMATCHED_NOTE in c.value for c in at.caption)
    assert _cfg(at).education.tier_mapping == {}

    at.button(key="cfg_tier_use").click().run()
    _clean(at)
    mapping = _cfg(at).education.tier_mapping
    assert mapping == {"northfield institute of technology": "1", "nit northfield": "1", "northfield tech": "1",
                       "riverside college of engineering": "2", "lakeside university": "Premier"}
    # Same form keys: a browser keeps unsaved form edits (AppTest drops unsubmitted values itself).
    assert at.session_state[state.K_CONFIG_VERSION] == version
    assert at.button(key="cfg_tier_use").disabled                           # this list is now in use
    assert not at.button(key="cfg_tier_clear").disabled and not at.download_button(key="cfg_tier_dl").disabled
    assert any("Tier list in use: 5 college names and aliases" in t.value for t in at.toast)

    at.toggle(key=f"{k}_college_tier_on").set_value(True)
    at.slider(key=f"{k}_college_tier_threshold").set_value(85)
    at.text_area(key=f"{k}_exclude").set_value("C-1000042")
    _apply(at)
    ed = _cfg(at).education
    assert (ed.tier_enabled, ed.tier_match_threshold, ed.tier_mapping) == (True, 85, mapping)
    assert _cfg(at).shortlist.exclude_candidates == ["C-1000042"]
    assert engine["calls"][-1].education.tier_enabled
    assert any("the tier list in use" in c.value for c in at.caption)

    at.button(key="cfg_tier_clear").click().run()
    _clean(at)
    assert _cfg(at).education.tier_mapping == {} and _cfg(at).education.tier_enabled
    assert any("Tier list cleared" in t.value for t in at.toast)


@pytest.mark.parametrize("name, data, message", [
    ("tiers.csv", b"college,grade\nLakeside University,1\n", "needs a 'college' column and a 'tier' column"),
    ("tiers.csv", b"college,tier\n" + b"x" * (2 * 1024 * 1024), "the limit is 2 MB"),
    ("tiers.csv", b"college,tier\n" + b"".join(b"College %d,1\n" % i for i in range(5001)), "the limit is 5,000"),
    ("tiers.xlsx", b"PK\x03\x04not really a workbook", "Could not open the Excel workbook"),
])
def test_invalid_tier_lists_are_rejected(engine: dict[str, Any], name: str, data: bytes, message: str) -> None:
    at = _open(engine)
    at.file_uploader(key="cfg_tier_upload").set_value((name, data, "text/csv")).run()
    _clean(at)
    assert any(message in e.value for e in at.error), [e.value for e in at.error]
    assert not any(b.key == "cfg_tier_use" for b in at.button)
    assert _cfg(at).education.tier_mapping == {}


def test_tier_preview_without_a_dataset_and_with_feed_tiers(engine: dict[str, Any]) -> None:
    at = _app().run()                                                       # before the first run
    _clean(at)
    assert any("Load a dataset to see how its colleges match" in c.value for c in at.caption)
    at = _open(engine)                                                      # the fake feed has tiers
    preview = _table(at, "source")
    assert set(preview["source"]) == {"Assessment feed"} and set(preview["tier"]) == {"1", "2", "3"}
    assert any("0 of 5 colleges have no tier" in c.value for c in at.caption)


# ---------------------------------------------------------------------------
# Integrity: team projects & prompt-injection weight
# ---------------------------------------------------------------------------
def test_team_project_settings_and_prompt_injection_weight(engine: dict[str, Any]) -> None:
    cfg = _clean_cfg(engine)
    cfg.risk.individual_weights = {"speed_anomaly": 25.0}                    # e.g. a profile from an older version
    at = _open(engine, cfg)
    k = _k(at)
    weight = at.number_input(key=f"{k}_iw_prompt_injection")
    assert weight.label == "Prompt injection" and weight.value == 50.0
    repos = at.number_input(key=f"{k}_risk_team_max_shared_repos")
    assert repos.value == 2 and tab_config.TEAM_HELP in repos.help
    assert any(tab_config.TEAM_HELP in c.value for c in at.caption)
    repos.set_value(3)
    at.number_input(key=f"{k}_risk_team_max_text_similarity").set_value(0.35)
    at.number_input(key=f"{k}_risk_team_max_shared_bullets").set_value(2)
    at.number_input(key=f"{k}_risk_team_repo_weight").set_value(12.0)
    weight.set_value(60.0)
    _apply(at)
    r = _cfg(at).risk
    assert (r.team_max_shared_repos, r.team_max_text_similarity, r.team_max_shared_bullets, r.team_repo_weight) == \
        (3, 0.35, 2, 12.0)
    assert r.individual_weights["prompt_injection"] == 60.0 and r.individual_weights["speed_anomaly"] == 25.0
    assert "plagiarism_flag" in r.individual_weights                        # defaults filled back in


# ---------------------------------------------------------------------------
# Dataset cache (snapshots)
# ---------------------------------------------------------------------------
def test_snapshot_settings_and_clear_cached_datasets(engine: dict[str, Any]) -> None:
    cfg = _clean_cfg(engine)
    snapshots.save_snapshot(engine["prepared"], "ab" * 32, "Fake dataset", cfg)
    assert len(snapshots.list_snapshots(cfg)) == 1
    at = _open(engine, cfg)
    k = _k(at)
    assert at.toggle(key=f"{k}_snap_on").value is True and at.number_input(key=f"{k}_snap_ttl").value == 7
    assert any("1 cached dataset(s)" in c.value for c in at.caption)
    at.toggle(key=f"{k}_snap_on").set_value(False)
    at.number_input(key=f"{k}_snap_ttl").set_value(3)
    _apply(at)
    assert (_cfg(at).snapshots.enabled, _cfg(at).snapshots.ttl_days) == (False, 3)
    at.button(key="cfg_snap_clear").click().run()
    _clean(at)
    assert snapshots.list_snapshots(cfg).empty
    assert any("Cleared 1 cached dataset(s)" in t.value for t in at.toast)
    assert at.button(key="cfg_snap_clear").disabled
    assert any("No cached datasets. Caching is switched off." in c.value for c in at.caption)


# ---------------------------------------------------------------------------
# LLM (GitHub Copilot via VS Code proxy)
# ---------------------------------------------------------------------------
def test_llm_section_models_status_and_settings(engine: dict[str, Any], proxy: dict[str, Any]) -> None:
    cfg = _clean_cfg(engine)
    cfg.llm.model = "chat-model-old"                                        # not offered by the proxy
    at = _open(engine, cfg)
    k = _k(at)
    assert any(m.value.startswith("##### LLM (GitHub Copilot via VS Code proxy)") for m in at.markdown)
    assert any(tab_config.LLM_NOTE in c.value for c in at.caption)
    status = next(c.value for c in at.caption if c.value.startswith("Proxy `"))
    assert status == (f"Proxy `{BASE_URL}` · reachable, 2 models · token: environment variable {KEY_ENV}")
    model = at.selectbox(key=f"{k}_llm_model")
    assert model.options == ["(proxy default)", "chat-model-a", "chat-model-b",
                             "chat-model-old (not offered by the proxy)"]
    assert model.value == "chat-model-old"
    labels = [w.label for w in (*at.number_input, *at.slider)]
    assert not any("re-score" in x.lower() or "blend" in x.lower() for x in labels)
    model.set_value("")
    for suffix, value in (("llm_borderline", 120), ("llm_tokens", 1024), ("llm_timeout", 60.0),
                          ("llm_chars", 8000), ("llm_conc", 2)):
        at.number_input(key=f"{k}_{suffix}").set_value(value)
    at.slider(key=f"{k}_llm_temp").set_value(0.4)
    assert "cutoff" in at.number_input(key=f"{k}_llm_borderline").label
    _apply(at)
    lc = _cfg(at).llm
    assert (lc.model, lc.borderline_band, lc.max_tokens, lc.timeout_sec, lc.max_input_chars, lc.max_concurrency,
            lc.temperature) == ("", 120, 1024, 60.0, 8000, 2, 0.4)
    assert lc.base_url == BASE_URL and lc.api_key_env == KEY_ENV
    assert at.selectbox(key=f"{_k(at)}_llm_model").value == ""
    page = "\n".join(_texts(at) + _widget_values(at))
    assert TOKEN not in page


def test_llm_status_explains_a_proxy_without_models(engine: dict[str, Any], proxy: dict[str, Any],
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, backends, "proxy_status",
           lambda *args, **kwargs: {"reachable": True, "models": [], "error": backends.NO_MODELS_TEXT,
                                    "base_url": BASE_URL, "token_source": "configured"})
    at = _open(engine)
    assert any("reachable, no models" in c.value for c in at.caption)
    assert any("no GitHub Copilot chat models are available" in c.value for c in at.caption)
    assert at.selectbox(key=f"{_k(at)}_llm_model").options == ["(proxy default)"]


def test_llm_off_does_not_contact_the_proxy(engine: dict[str, Any], proxy: dict[str, Any],
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    def no_network(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("the proxy must not be contacted while LLM features are off")

    _patch(monkeypatch, backends, "proxy_status", no_network)
    cfg = _clean_cfg(engine)
    cfg.llm.enabled = False
    at = _open(engine, cfg)
    assert any("not checked while LLM features are off" in c.value for c in at.caption)
    assert at.button(key="cfg_llm_test").disabled
    assert at.selectbox(key=f"{_k(at)}_llm_model").options == ["(proxy default)"]


def test_test_connection_reports_success_and_failure_without_the_token(engine: dict[str, Any],
                                                                       proxy: dict[str, Any]) -> None:
    at = _open(engine)
    button = at.button(key="cfg_llm_test")
    assert button.label == "Test connection" and not button.disabled
    assert "never changes scores" in button.help
    button.click().run()
    _clean(at)
    assert any("LLM connection OK" in s.value and "chat-model-a" in s.value for s in at.success)
    assert proxy["tests"] == 1

    proxy.update(ok=False, message=f"The proxy rejected the token {TOKEN} (401).")
    at.button(key="cfg_llm_test").click().run()
    _clean(at)
    error = next(e.value for e in at.error if "LLM connection failed" in e.value)
    assert TOKEN not in error and "redacted" in error

    proxy["raise"] = RuntimeError(f"connection reset while sending Bearer {TOKEN}")
    at.button(key="cfg_llm_test").click().run()
    _clean(at)
    assert any("LLM connection failed" in e.value and "RuntimeError" in e.value for e in at.error)
    assert TOKEN not in "\n".join(_texts(at) + _widget_values(at))


def test_test_connection_explains_a_missing_proxy(engine: dict[str, Any]) -> None:
    at = _open(engine)                                                      # no token anywhere
    at.button(key="cfg_llm_test").click().run()
    _clean(at)
    assert any("LLM connection failed" in e.value and "proxy not found" in e.value for e in at.error)


# ---------------------------------------------------------------------------
# What-if preview & profiles
# ---------------------------------------------------------------------------
def test_preview_shows_ineligible_counts(engine: dict[str, Any]) -> None:
    at = _open(engine)
    at.number_input(key=f"{_k(at)}_min_pct").set_value(60.0)
    at.button(key="cfg_preview").click().run()
    _clean(at)
    metric = next(m for m in at.metric if m.label == "Ineligible")
    preview = at.session_state[tab_config.K_PREVIEW]["ineligible"]
    assert preview["after"] > preview["before"] and metric.value == str(preview["after"])
    assert any("Ineligible by rule (now → after): Score Threshold 0 → " in c.value for c in at.caption)
    assert _cfg(at).shortlist.min_hackerrank_pct == 0.0                      # nothing applied yet


def test_profile_round_trip_keeps_new_fields_and_ignores_connection_settings(engine: dict[str, Any]) -> None:
    profile = AppConfig()
    sl, ed, rk, lc = profile.shortlist, profile.education, profile.risk, profile.llm
    sl.tie_break_order, sl.exclude_candidates = ["cgpa", "submission_time"], ["C-1000042"]
    sl.auto_disqualify_duplicates, sl.missing_resume_policy = False, "renormalize"
    ed.tier_enabled, ed.tier_mapping, ed.tier_match_threshold = True, {"lakeside university": "2"}, 80
    rk.team_max_shared_repos, rk.team_repo_weight = 4, 9.0
    rk.individual_weights = {**rk.individual_weights, "prompt_injection": 45.0}
    lc.model, lc.borderline_band, lc.max_tokens = "chat-model-a", 75, 1536
    lc.timeout_sec, lc.max_input_chars = 90.0, 7000
    profile.snapshots.enabled, profile.snapshots.ttl_days = False, 5
    assert AppConfig.from_json(profile.to_json()) == profile                # pure round trip

    data = json.loads(profile.to_json())
    data["llm"].update(base_url="http://203.0.113.9:8080/v1", api_key_env="OTHER_TOKEN")
    data["snapshots"]["cache_dir"] = "elsewhere"
    data["shortlist"]["tie_break_order"] = ["cgpa", "nope", "", "submission_time"]
    data["shortlist"]["exclude_candidates"] = ["c-1000042", "C-1000042"]
    at = _open(engine)
    at.file_uploader(key="cfg_upload").set_value(("profile.json", json.dumps(data).encode(), "application/json")).run()
    at.button(key="cfg_apply_upload").click().run()
    _clean(at)
    applied = _cfg(at)
    assert applied == profile                                               # connection fields ignored, lists cleaned
    assert applied.llm.base_url == BASE_URL and applied.snapshots.cache_dir == AppConfig().snapshots.cache_dir
    k = _k(at)
    assert at.multiselect(key=f"{k}_tie_breaks").value == ["cgpa", "submission_time"]
    assert at.text_area(key=f"{k}_exclude").value == "C-1000042"
    assert at.selectbox(key=f"{k}_no_resume").value == "renormalize"
    assert at.toggle(key=f"{k}_college_tier_on").value is True
    assert at.slider(key=f"{k}_college_tier_threshold").value == 80
    assert at.number_input(key=f"{k}_llm_borderline").value == 75
    assert at.number_input(key=f"{k}_iw_prompt_injection").value == 45.0
    assert at.toggle(key=f"{k}_snap_on").value is False


def test_wrongly_typed_profiles_are_rejected_and_never_crash_the_tab(engine: dict[str, Any]) -> None:
    data = json.loads(AppConfig().to_json())
    data["weights"]["hackerrank"] = "forty"
    at = _open(engine)
    before = _cfg(at)
    at.file_uploader(key="cfg_upload").set_value(("bad.json", json.dumps(data).encode(), "application/json")).run()
    at.button(key="cfg_apply_upload").click().run()
    _clean(at)
    assert any("Could not read that config" in e.value for e in at.error)
    assert _cfg(at) == before

    broken = _clean_cfg(engine)                       # e.g. a configuration restored from an older file
    broken.weights.hackerrank = "40"
    broken.education.tier_scores = ["1", "2"]
    broken.shortlist.review_risk_levels = None
    broken.skills.required_skills = "Python"
    at = _app()
    at.session_state[state.K_CONFIG] = broken
    at.session_state[state.K_PREPARED] = engine["prepared"]
    at.session_state[state.K_PREPARED_FP] = FP
    at.session_state[state.K_RESULT] = engine["result"]
    at.session_state[state.K_RESULT_KEY] = state.eval_key(FP, broken)
    at.session_state[state.K_TABS] = state.TAB_CONFIG
    at.run()
    _clean(at)
    assert any("values of the wrong type" in w.value for w in at.warning)
    assert at.number_input(key=f"{_k(at)}_w_hackerrank").value == 40.0


def test_config_tab_tolerates_a_minimal_result() -> None:
    minimal = pd.DataFrame({S.CANDIDATE_ID: ["A-1", "A-2"], S.STATUS: [S.STATUS_SHORTLISTED, ""]})
    cfg = AppConfig()
    cfg.shortlist.exclude_candidates = ["A-2", "Z-9"]
    result = PipelineResult(candidates=minimal, parsed_resumes={}, parsed_by_candidate={}, matches=pd.DataFrame(),
                            risk=RiskResult(), quality=DataQualityReport(), config=cfg)
    at = _app()
    at.session_state[state.K_CONFIG] = cfg
    at.session_state[state.K_RESULT] = result
    at.session_state[state.K_TABS] = state.TAB_CONFIG
    at.run()
    _clean(at)
    assert any("Z-9" in w.value for w in at.warning)
    assert any("no college names to match" in c.value for c in at.caption)
    assert at.button(key="cfg_preview").disabled                            # no prepared data to preview


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def test_parse_candidate_ids_and_clean_tie_breaks() -> None:
    assert tab_config.parse_candidate_ids(" c-1000042,\nC-1000042 ; 'c-1000107'\tX1 ") == \
        ["C-1000042", "C-1000107", "X1"]
    assert tab_config.parse_candidate_ids(["a-1", None, "A-1", " "]) == ["A-1"]
    assert tab_config.parse_candidate_ids(None) == []
    assert tab_config.clean_tie_breaks(["cgpa", "nope", "", None, "cgpa", "coding_marks"]) == ["cgpa", "coding_marks"]
    assert tab_config.clean_tie_breaks("cgpa") == ["cgpa"] and tab_config.clean_tie_breaks(None) == []


def test_parse_tier_list_csv_and_xlsx() -> None:
    parsed = tab_config.parse_tier_list("tiers.csv", TIER_CSV, {"1": 1.0, "2": 0.7, "3": 0.4})
    assert (parsed.colleges, parsed.aliases, parsed.skipped, parsed.unscored) == (3, 2, 1, ["Premier"])
    assert parsed.tiers == ["1", "2", "Premier"]
    conflict = tab_config.parse_tier_list("t.csv", b"College Name,Tier\nLakeside University,1\nLAKESIDE UNIVERSITY,2\n")
    assert conflict.mapping == {"lakeside university": "1"} and conflict.conflicts == ["LAKESIDE UNIVERSITY"]

    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    for row in (("College", "Tier", "Aliases"), ("Riverside College of Engineering", 2, "RCE; Riverside Engg"),
                ("Eastgate Institute of Science", 3, None)):
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    parsed = tab_config.parse_tier_list("tiers.xlsx", buffer.getvalue())
    assert parsed.mapping == {"riverside college of engineering": "2", "rce": "2", "riverside engg": "2",
                              "eastgate institute of science": "3"}


def test_parse_tier_list_rejects_bad_files(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, data, message in (("t.csv", b"", "empty"), ("t.csv", b"college,tier\n,\n", "no rows with both"),
                                ("t.txt", b"college,tier\nLakeside University,1\n", "Upload the tier list as a CSV"),
                                ("t.xlsx", b"PK\x03\x04not really a zip", "Could not open the Excel workbook")):
        with pytest.raises(ValueError, match=message):
            tab_config.parse_tier_list(name, data)
    monkeypatch.setattr(tab_config, "TIER_MAX_UNPACKED_BYTES", 1024)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/sharedStrings.xml", "a" * 4096)                # unpacks to 4x the allowed size
    bomb = buffer.getvalue()
    with pytest.raises(ValueError, match="too large once unpacked"):
        tab_config.parse_tier_list("t.xlsx", bomb)
    # A directory that understates the size passes the directory check, but the part is inflated
    # in bounded steps (never past the declared size) and its CRC no longer matches.
    entry = bomb.rindex(b"PK\x01\x02")                                      # central directory record
    liar = bomb[:entry + 24] + (100).to_bytes(4, "little") + bomb[entry + 28:]
    assert zipfile.ZipFile(io.BytesIO(liar)).infolist()[0].file_size == 100
    with pytest.raises(ValueError, match="Could not open the Excel workbook"):
        tab_config.parse_tier_list("t.xlsx", liar)


def test_tier_list_frame_and_preview_helpers(engine: dict[str, Any]) -> None:
    frame = tab_config.tier_list_frame({"b college": "Tier 2", "a college": "Tier 10", "c college": "Tier 2",
                                        "d college": "Elite"})
    assert frame.values.tolist() == [["b college", "Tier 2"], ["c college", "Tier 2"], ["a college", "Tier 10"],
                                     ["d college", "Elite"]]
    cfg = AppConfig()
    cfg.education.tier_mapping = {"northfield institute of technology": "1"}
    base = engine["prepared"].base.drop(columns=[S.COLLEGE_TIER])
    out = tab_config.tier_preview(base, cfg, top=2)
    assert out["colleges"] == 5 and out["unmatched"] == 4 and len(out["table"]) == 2
    assert out["table"].iloc[0]["college"] == "Northfield Institute of Technology"   # most candidates first
    assert tab_config.tier_preview(pd.DataFrame({S.CANDIDATE_ID: ["A-1"]}), cfg)["colleges"] == 0


def test_model_options_status_line_and_ineligible_summary(engine: dict[str, Any]) -> None:
    cfg = AppConfig()
    assert tab_config.model_options(cfg, {"models": ["m1", "m1", " "]}) == (["", "m1"], {"": "(proxy default)"})
    cfg.llm.model = "m9"
    options, labels = tab_config.model_options(cfg, {"models": []})
    assert options == ["", "m9"] and labels["m9"] == "m9"
    line = tab_config.llm_status_line({"reachable": False, "token_source": "connection file"}, cfg)
    assert line == f"Proxy `{BASE_URL}` · not reachable · token: VS Code connection file"
    assert tab_config.llm_status_line({}, cfg).endswith("token: not found")
    before = engine["result"].candidates
    after = before.assign(**{S.STATUS: S.STATUS_INELIGIBLE})
    summary = tab_config.ineligible_summary(before, after)
    assert summary["before"] == int((before[S.STATUS] == S.STATUS_INELIGIBLE).sum()) and summary["after"] == len(before)
    assert tab_config.ineligible_summary(before.drop(columns=[S.STATUS]), after) is None
