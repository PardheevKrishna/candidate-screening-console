"""screening.duplicates: the same person applying more than once (different candidate IDs)."""
from __future__ import annotations

import time
import types

import numpy as np
import pandas as pd
import pytest

from screening import duplicates
from screening import schema as S
from screening.config import AppConfig
from screening.models import DataQualityReport, ParsedResume

RESUME_TEXT = ("Asha Menon. B.Tech Computer Science. Projects: fraud detection pipeline in Python and SQL, "
               "reduced false positives by 18%. Internship: backend services with Spring Boot and Docker.")
OTHER_TEXT = ("Rahul Iyer. Mechanical engineering. Projects: drone chassis design in SolidWorks, thermal "
              "analysis, CNC machining. Internship: plant maintenance and lean manufacturing.")


@pytest.fixture(autouse=True)
def fictional_webmail(monkeypatch):
    """Test addresses use reserved .example domains; register two of them as webmail providers."""
    monkeypatch.setattr(duplicates, "WEBMAIL_DOMAINS", duplicates.WEBMAIL_DOMAINS | {"freemail.example", "webmail.example"})


def resume(cid: str, text: str = RESUME_TEXT, file_hash: str | None = None) -> ParsedResume:
    return ParsedResume(file_name=f"{cid}.pdf", file_hash=file_hash or f"hash-{cid}", text=text)


def frame(rows: list[dict]) -> pd.DataFrame:
    defaults = {S.NAME: "Asha Menon", S.COLLEGE: "Northfield Institute of Technology", S.BRANCH: "CSE",
                S.CGPA: 9.1, S.HR_PCT: 90.0, S.EMAIL: "", S.RESUME_HASH: "", S.HAS_RESUME: False}
    return pd.DataFrame([{**defaults, **r} for r in rows])


def marks(out: pd.DataFrame) -> dict[str, tuple[str, str]]:
    return {cid: (of, ids) for cid, of, ids in zip(out[S.CANDIDATE_ID], out[S.DUPLICATE_OF], out[S.DUPLICATE_IDS])}


def test_identical_resume_pair_prefers_the_institutional_email():
    base = frame([
        {S.CANDIDATE_ID: "C-1000042", S.EMAIL: "asha.m@freemail.example", S.RESUME_HASH: "abc", S.HAS_RESUME: True},
        {S.CANDIDATE_ID: "C-1000077", S.EMAIL: "asha.menon@northfield-tech.example", S.RESUME_HASH: "abc",
         S.HAS_RESUME: True, S.NAME: "MENON,  Asha", S.COLLEGE: "northfield institute of technology."},
        {S.CANDIDATE_ID: "C-1000099", S.NAME: "Rahul Iyer", S.EMAIL: "rahul@example.com"},
    ])
    before = base.copy()
    out, issues = duplicates.detect_duplicates(base, {})
    assert marks(out) == {"C-1000042": ("C-1000077", ""), "C-1000077": ("", "C-1000042"),
                          "C-1000099": ("", "")}
    evidence = "same name and college; identical resume file; emails differ (webmail vs institutional)"
    assert out[S.DUPLICATE_EVIDENCE].tolist() == [evidence, evidence, ""]
    assert len(issues) == 1 and issues[0]["severity"] == "info" and issues[0]["stage"] == "duplicates"
    assert issues[0]["candidate_id"] == "C-1000077"
    assert "C-1000042" in issues[0]["issue"] and "@" not in issues[0]["issue"] and "Asha" not in issues[0]["issue"]
    pd.testing.assert_frame_equal(base, before)                 # input untouched


def test_job_application_name_and_parsed_resume_hash_are_used():
    base = frame([
        {S.CANDIDATE_ID: "C-1000043", S.NAME: None, S.JOB_APPLICATION: "Asha Menon (asha menon)"},
        {S.CANDIDATE_ID: "C-1000042", S.NAME: "", S.JOB_APPLICATION: "ASHA MENON (asha menon)"},
    ]).drop(columns=[S.RESUME_HASH, S.HAS_RESUME])
    same_file = {"C-1000042": resume("C-1000042", file_hash="h1"), "C-1000043": resume("C-1000043", file_hash="h1")}
    out, _ = duplicates.detect_duplicates(base, same_file)
    assert marks(out) == {"C-1000043": ("C-1000042", ""), "C-1000042": ("", "C-1000043")}   # lowest numeric ID
    assert out.loc[0, S.DUPLICATE_EVIDENCE] == "same name and college; identical resume file"


def test_group_of_three_and_primary_rules():
    base = frame([
        {S.CANDIDATE_ID: "C-1000050", S.EMAIL: "asha@webmail.example", S.RESUME_HASH: "x", S.HAS_RESUME: True},
        {S.CANDIDATE_ID: "C-999999", S.EMAIL: "asha.menon@freemail.example", S.RESUME_HASH: "x", S.HAS_RESUME: True},
        {S.CANDIDATE_ID: "C-1000044", S.EMAIL: "asha.m@webmail.example", S.RESUME_HASH: "x", S.HAS_RESUME: True},
    ])
    out, issues = duplicates.detect_duplicates(base, {})
    # no institutional email -> the lowest numeric candidate ID is the primary
    assert marks(out)["C-999999"] == ("", "C-1000044, C-1000050")
    assert marks(out)["C-1000050"] == ("C-999999", "") and marks(out)["C-1000044"] == ("C-999999", "")
    assert out[S.DUPLICATE_EVIDENCE].nunique() == 1
    assert out.loc[0, S.DUPLICATE_EVIDENCE].endswith("; emails differ")
    assert len(issues) == 1 and "3 applications" in issues[0]["issue"]
    # an application with an assessment score is preferred over an unscored one
    unscored = base.assign(**{S.HR_PCT: [90.0, np.nan, 85.0]})
    out, _ = duplicates.detect_duplicates(unscored, {})
    assert marks(out)["C-1000044"] == ("", "C-999999, C-1000050")


def test_near_identical_resume_text_links_different_files():
    base = frame([{S.CANDIDATE_ID: "C-1000042", S.HAS_RESUME: True, S.EMAIL: "asha@northfield-tech.example"},
                  {S.CANDIDATE_ID: "C-1000043", S.HAS_RESUME: True, S.EMAIL: "asha@northfield-tech.example"}])
    parsed = {"C-1000042": resume("C-1000042"), "C-1000043": resume("C-1000043", text=RESUME_TEXT + " Chess.")}
    out, _ = duplicates.detect_duplicates(base, parsed)
    assert marks(out)["C-1000043"] == ("C-1000042", "")
    evidence = out.loc[0, S.DUPLICATE_EVIDENCE]
    assert evidence.startswith("same name and college; near-identical resume text (similarity 0.9")
    assert evidence.endswith("; same email")


def test_different_resumes_or_one_missing_resume_are_not_merged():
    base = frame([{S.CANDIDATE_ID: "C-1000042", S.HAS_RESUME: True}, {S.CANDIDATE_ID: "C-1000043", S.HAS_RESUME: True},
                  {S.CANDIDATE_ID: "C-1000044"}])
    parsed = {"C-1000042": resume("C-1000042"), "C-1000043": resume("C-1000043", text=OTHER_TEXT)}
    out, issues = duplicates.detect_duplicates(base, parsed)
    assert (out[S.DUPLICATE_OF] == "").all() and (out[S.DUPLICATE_IDS] == "").all() and not issues


def test_without_resumes_cgpa_and_branch_must_match():
    base = frame([
        {S.CANDIDATE_ID: "C-1000042", S.CGPA: 9.06, S.BRANCH: "Computer Science & Engineering"},
        {S.CANDIDATE_ID: "C-1000043", S.CGPA: 9.060000001, S.BRANCH: "computer science and engineering"},
        {S.CANDIDATE_ID: "C-1000044", S.CGPA: 8.70, S.BRANCH: "Computer Science & Engineering"},
        {S.CANDIDATE_ID: "C-1000045", S.CGPA: None, S.BRANCH: None},
        {S.CANDIDATE_ID: "C-1000046", S.CGPA: None, S.BRANCH: None},
        {S.CANDIDATE_ID: "C-1000047", S.CGPA: 8.1, S.BRANCH: "N/A"},
        {S.CANDIDATE_ID: "C-1000048", S.CGPA: 8.1, S.BRANCH: "n/a"},
    ])
    out, _ = duplicates.detect_duplicates(base, {})
    assert marks(out)["C-1000043"] == ("C-1000042", "")
    assert marks(out)["C-1000042"] == ("", "C-1000043")
    assert all(marks(out)[c] == ("", "") for c in ("C-1000044", "C-1000045", "C-1000046", "C-1000047", "C-1000048"))
    assert out.loc[0, S.DUPLICATE_EVIDENCE] == "same name and college; no resumes; same CGPA and branch"


def test_same_name_in_different_or_unknown_colleges_is_never_merged():
    base = frame([
        {S.CANDIDATE_ID: "C-1000042", S.COLLEGE: "Northfield Institute of Technology", S.RESUME_HASH: "x",
         S.HAS_RESUME: True},
        {S.CANDIDATE_ID: "C-1000043", S.COLLEGE: "Southgate University", S.RESUME_HASH: "x", S.HAS_RESUME: True},
        {S.CANDIDATE_ID: "C-1000044", S.COLLEGE: "N/A", S.RESUME_HASH: "x", S.HAS_RESUME: True},
        {S.CANDIDATE_ID: "C-1000045", S.COLLEGE: None, S.RESUME_HASH: "x", S.HAS_RESUME: True},
        {S.CANDIDATE_ID: "C-1000046", S.COLLEGE: "", S.RESUME_HASH: "x", S.HAS_RESUME: True},
        {S.CANDIDATE_ID: "C-1000047", S.NAME: "Asha Menon Rao", S.RESUME_HASH: "x", S.HAS_RESUME: True},
        {S.CANDIDATE_ID: "C-1000042", S.NAME: "Asha Menon", S.RESUME_HASH: "x", S.HAS_RESUME: True},  # same ID
    ])
    out, issues = duplicates.detect_duplicates(base, {})
    assert (out[S.DUPLICATE_OF] == "").all() and not issues


def test_placeholder_names_and_implausibly_large_groups_are_not_marked():
    placeholder = frame([{S.CANDIDATE_ID: f"C-{1000040 + i}", S.NAME: "N/A", S.JOB_APPLICATION: None,
                          S.RESUME_HASH: "x", S.HAS_RESUME: True} for i in range(3)])
    out, issues = duplicates.detect_duplicates(placeholder, {})
    assert (out[S.DUPLICATE_OF] == "").all() and not issues
    # "N/A" in Name falls back to the Job Application name
    fallback = placeholder.assign(**{S.JOB_APPLICATION: "Asha Menon (asha menon)"})
    out, _ = duplicates.detect_duplicates(fallback.head(2), {})
    assert (out[S.DUPLICATE_OF] != "").sum() == 1
    crowd = frame([{S.CANDIDATE_ID: f"C-{1000100 + i}", S.NAME: "Test Candidate"} for i in range(duplicates.MAX_GROUP_SIZE + 1)])
    out, issues = duplicates.detect_duplicates(crowd, {})
    assert (out[S.DUPLICATE_OF] == "").all() and (out[S.DUPLICATE_IDS] == "").all()
    assert len(issues) == 1 and issues[0]["severity"] == "warning" and "placeholder" in issues[0]["issue"]
    assert "Test Candidate" not in issues[0]["issue"]
    out, issues = duplicates.detect_duplicates(crowd.head(duplicates.MAX_GROUP_SIZE), {})
    assert (out[S.DUPLICATE_OF] != "").sum() == duplicates.MAX_GROUP_SIZE - 1 and issues[0]["severity"] == "info"


def test_missing_columns_and_tiny_frames_never_raise():
    for base in (pd.DataFrame(), pd.DataFrame({S.CANDIDATE_ID: ["C-1"]}),
                 pd.DataFrame({S.NAME: ["Asha Menon", "Asha Menon"]}),
                 pd.DataFrame({S.CANDIDATE_ID: ["C-1", "C-2"], S.NAME: [1.5, None], S.COLLEGE: [object(), "X"]})):
        out, issues = duplicates.detect_duplicates(base, None)
        assert set(duplicates.DUPLICATE_COLUMNS) <= set(out.columns) and not issues
        assert len(out) == len(base)


def test_helpers():
    assert duplicates.name_key("  MENON, Asha ") == duplicates.name_key(None, "Asha Menon (asha menon)") == "asha menon"
    assert duplicates.name_key("José Álvarez") == "alvarez jose"
    assert duplicates.name_key(None, None) == ""
    assert duplicates.college_identity("XY-IICT") == duplicates.college_identity("xy iict") == "xyiict"
    assert duplicates.college_identity("A & M College") == duplicates.college_identity("A and M college")
    assert duplicates.college_identity("n/a") == "" and duplicates.college_identity(None) == ""
    for domain in ("gmail.com", "GoogleMail.com", "yahoo.co.in", "yahoo.com", "proton.me", "mail.com", "zoho.com"):
        assert duplicates.is_webmail_domain(domain)
    assert not duplicates.is_webmail_domain("northfield-tech.example") and not duplicates.is_webmail_domain("")
    for email in ("asha@freemail.example", "ASHA@WebMail.Example"):
        assert duplicates.is_webmail(email) and not duplicates.is_institutional(email)
    for email in ("a@northfield-tech.example", "a@students.northfield.example"):
        assert duplicates.is_institutional(email) and not duplicates.is_webmail(email)
    assert not duplicates.is_webmail("") and not duplicates.is_institutional("no-at-sign")


def test_3000_rows_run_quickly():
    rng = np.random.default_rng(3)
    rows, parsed = [], {}
    for i in range(3000):
        cid = f"C-{1000000 + i}"
        person = i if i % 20 else i - 1             # every 20th row re-applies as the previous person
        college = f"College {person % 150}"
        rows.append({S.CANDIDATE_ID: cid, S.NAME: f"Person{person} Family{person % 97}", S.COLLEGE: college,
                     S.EMAIL: f"p{i}@freemail.example" if i % 2 else f"p{i}@campus{person % 150}.example",
                     S.BRANCH: "CSE", S.CGPA: float(rng.uniform(8.5, 10.0)), S.HR_PCT: 90.0,
                     S.RESUME_HASH: "", S.HAS_RESUME: i % 8 != 0})
        if i % 8:
            words = " ".join(rng.choice([f"w{k}" for k in range(3000)], 300))
            parsed[cid] = resume(cid, text=f"person{person} {words}")
    # duplicate rows reuse the previous person's resume text
    for i in range(20, 3000, 20):
        cid, prev = f"C-{1000000 + i}", f"C-{1000000 + i - 1}"
        if cid in parsed and prev in parsed:
            parsed[cid] = resume(cid, text=parsed[prev].text)
    base = pd.DataFrame(rows)
    start = time.perf_counter()
    out, issues = duplicates.detect_duplicates(base, parsed)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0
    # re-applications where both rows have a resume; a resume on only one side is not evidence
    expected = sum(1 for i in range(20, 3000, 20) if f"C-{1000000 + i}" in parsed and f"C-{1000000 + i - 1}" in parsed)
    assert len(issues) == int((out[S.DUPLICATE_OF] != "").sum()) == expected == 75
    # the re-application (even row) has the institutional email, so the earlier webmail row is the duplicate
    assert set(out.loc[out[S.DUPLICATE_OF] != "", S.CANDIDATE_ID]) == {
        f"C-{1000000 + i - 1}" for i in range(20, 3000, 20) if f"C-{1000000 + i}" in parsed}


# ---------------------------------------------------------------------------
# pipeline.prepare_data runs the check after linking
# ---------------------------------------------------------------------------
def _fake_stages(monkeypatch, base: pd.DataFrame, parsed_by_candidate: dict, detect=None):
    from screening import pipeline

    monkeypatch.setattr(pipeline, "ingest", types.SimpleNamespace(
        load_ta_feed=lambda name, data, cfg, mapping=None: (base, DataQualityReport(rows_raw=len(base)))))
    monkeypatch.setattr(pipeline, "resume_parser", types.SimpleNamespace(
        expand_uploads=lambda docs, cfg, progress_cb=None: (list(docs), []),
        parse_resumes=lambda docs, **kw: {p.file_hash: p for p in parsed_by_candidate.values()}))
    monkeypatch.setattr(pipeline, "matching", types.SimpleNamespace(
        match_resumes=lambda ta, parsed, cfg: pd.DataFrame(),
        link_candidates=lambda ta, parsed, matches, cfg: (ta, dict(parsed_by_candidate), [])))
    if detect is not None:
        monkeypatch.setattr(pipeline, "duplicates", types.SimpleNamespace(detect_duplicates=detect,
                                                                          STAGE=duplicates.STAGE))
    return pipeline


def test_prepare_data_marks_duplicates_after_linking(monkeypatch):
    base = frame([{S.CANDIDATE_ID: "C-1000042", S.EMAIL: "asha@freemail.example", S.HAS_RESUME: True},
                  {S.CANDIDATE_ID: "C-1000043", S.EMAIL: "asha@northfield-tech.example", S.HAS_RESUME: True}])
    shared = {"C-1000042": resume("C-1000042", file_hash="same"), "C-1000043": resume("C-1000043", file_hash="same")}
    pipeline = _fake_stages(monkeypatch, base, shared)
    from screening.models import ResumeDoc

    prepared = pipeline.prepare_data(("export.xlsx", b"x"), [ResumeDoc("Resume.zip", b"zip")], AppConfig())
    assert prepared.mode == "ta+resumes" and "duplicates" in prepared.timings
    assert marks(prepared.base) == {"C-1000042": ("C-1000043", ""), "C-1000043": ("", "C-1000042")}
    assert [i["stage"] for i in prepared.quality.issues] == ["duplicates"]


def test_prepare_data_survives_a_failing_duplicate_check(monkeypatch):
    def broken(base, parsed):
        raise RuntimeError("boom")

    base = frame([{S.CANDIDATE_ID: "C-1000042"}])
    pipeline = _fake_stages(monkeypatch, base, {}, detect=broken)
    prepared = pipeline.prepare_data(("export.xlsx", b"x"), [], AppConfig())
    assert prepared.mode == "ta_only" and len(prepared.base) == 1
    issue = prepared.quality.issues[-1]
    assert issue["severity"] == "warning" and "RuntimeError" in issue["issue"] and "boom" not in issue["issue"]


@pytest.mark.parametrize("auto", [True, False])
def test_end_to_end_with_scoring(monkeypatch, auto):
    """Duplicates found at prepare time are disqualified (or not) by apply_shortlist."""
    from screening import pipeline, scoring
    from screening.models import RiskResult

    base = frame([{S.CANDIDATE_ID: "C-1000042", S.EMAIL: "asha@freemail.example", S.RESUME_HASH: "r", S.HAS_RESUME: True},
                  {S.CANDIDATE_ID: "C-1000043", S.EMAIL: "asha@northfield-tech.example", S.RESUME_HASH: "r",
                   S.HAS_RESUME: True},
                  {S.CANDIDATE_ID: "C-1000099", S.NAME: "Rahul Iyer", S.EMAIL: "rahul@example.com"}])
    marked, _ = duplicates.detect_duplicates(base, {})
    monkeypatch.setattr(pipeline, "risk", types.SimpleNamespace(
        run_risk_analysis=lambda scored, pbc, cfg, parsed_all=None: RiskResult()))
    prepared = pipeline.PreparedData(ta_df=base, quality=DataQualityReport(), parsed={}, matches=pd.DataFrame(),
                                     base=marked, parsed_by_candidate={}, mode="ta_only")
    cfg = AppConfig()
    cfg.shortlist.auto_disqualify_duplicates = auto
    out = pipeline.evaluate(prepared, cfg).candidates.set_index(S.CANDIDATE_ID)
    assert bool(out.loc["C-1000042", S.ELIGIBLE]) is (not auto)
    if auto:
        assert out.loc["C-1000042", S.STATUS_REASON] == (
            "Disqualified: Duplicate application of C-1000043 (merged into official profile "
            "asha@northfield-tech.example).")
        assert out.loc["C-1000043", S.STATUS_REASON].endswith(" | merged duplicate application C-1000042")
    assert set(scoring.IN_SHORTLIST) >= {out.loc["C-1000043", S.STATUS]}
