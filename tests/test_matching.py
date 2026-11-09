"""Tests for screening.matching: resume <-> TA candidate linkage (ParsedResume stubs built by hand)."""
from __future__ import annotations

import hashlib
import random
import time

import numpy as np
import pandas as pd
import pytest

import screening.schema as S
from screening import matching
from screening.config import AppConfig
from screening.models import ParsedResume

CFG = AppConfig()


def ta_frame(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    for col in (S.CANDIDATE_ID, S.NAME, S.EMAIL, S.PHONE, S.RESUME_FILE, S.DATA_FLAGS, "extra_notes"):
        if col in df.columns:
            df[col] = df[col].astype("str")
    return df


def resume(file_name: str, **kw) -> ParsedResume:
    kw.setdefault("file_hash", hashlib.sha256(file_name.encode()).hexdigest())
    return ParsedResume(file_name=file_name, **kw)


def as_dict(*resumes: ParsedResume) -> dict[str, ParsedResume]:
    return {p.file_hash: p for p in resumes}


def by_file(matches: pd.DataFrame) -> pd.DataFrame:
    return matches.set_index(matching.FILE_NAME)


PEOPLE = [
    ("CAND-00012", "Asha Menon", "asha.menon@example.com", "9876543210"),
    ("CAND-00013", "Vikram Shah", "vikram.shah@example.com", "9876543211"),
    ("CAND-00014", "Neha Singh", "neha.singh@example.com", "9876543212"),
    ("CAND-00015", "Rohan Das", "rohan.das@example.com", "9876543213"),
    ("CAND-00016", "Priya Nair", "priya.nair@example.com", "9876543214"),
    ("CAND-00017", "Kiran Kumar", "kiran.kumar@example.com", "9876543215"),
    ("CAND-00018", "Meera Iyer", "meera.iyer@example.com", "9876543216"),
    ("CAND-00019", "Arjun Menon", "arjun.menon@example.com", "9876543217"),
]


@pytest.fixture()
def ta() -> pd.DataFrame:
    return ta_frame([{S.CANDIDATE_ID: c, S.NAME: n, S.EMAIL: e, S.PHONE: p, S.HR_PCT: 100.0 - i,
                      S.DATA_FLAGS: "", "extra_notes": f"n{i}"} for i, (c, n, e, p) in enumerate(PEOPLE)])


# ---------------------------------------------------------------------------
# Matching paths
# ---------------------------------------------------------------------------
def test_every_matching_path(ta):
    ta = ta.assign(**{S.RESUME_FILE: pd.Series(["Asha Menon CV.PDF", "Vikram_Resume", None, None, None, None, None, None],
                                               dtype="str")})
    parsed = as_dict(
        resume("asha menon cv.pdf"),                                              # TA resume_file (case-insensitive)
        resume("Vikram_Resume.docx"),                                           # TA resume_file stem
        resume("cand_00014 resume.pdf"),                                        # ID token, other separator
        resume("Resume (12).pdf", emails=["ROHAN.DAS@example.com"]),            # email
        resume("document.pdf", text="Profile\nCandidate ID: CAND-00016\nSkills: Python, SQL"),  # ID in text
        resume("cv_final.pdf", phones=["+91 98765 43215"]),                     # phone last-10
        resume("Meera_Iyer_Resume.docx"),                                       # name from file name
        resume("arjunmenon_cv.pdf"),                                            # compact file-name name
        resume("nobody.pdf", name_guess="Totally Unknown", emails=["x@example.org"]),
    )
    m = by_file(matching.match_resumes(ta, parsed, CFG))
    expected = {
        "asha menon cv.pdf": ("CAND-00012", S.MATCH_TA_FILE, 1.0),
        "Vikram_Resume.docx": ("CAND-00013", S.MATCH_TA_FILE, 1.0),
        "cand_00014 resume.pdf": ("CAND-00014", S.MATCH_ID_FILENAME, 1.0),
        "Resume (12).pdf": ("CAND-00015", S.MATCH_EMAIL, 0.99),
        "document.pdf": ("CAND-00016", S.MATCH_ID_TEXT, 0.97),
        "cv_final.pdf": ("CAND-00017", S.MATCH_PHONE, 0.95),
        "Meera_Iyer_Resume.docx": ("CAND-00018", S.MATCH_NAME, 0.9),
        "arjunmenon_cv.pdf": ("CAND-00019", S.MATCH_NAME, 0.9),
    }
    for file_name, (cid, method, conf) in expected.items():
        row = m.loc[file_name]
        assert (row[S.CANDIDATE_ID], row[S.MATCH_METHOD], row[S.MATCH_CONFIDENCE]) == (cid, method, conf), file_name
    assert pd.isna(m.loc["nobody.pdf", S.CANDIDATE_ID]) and m.loc["nobody.pdf", S.MATCH_METHOD] == S.MATCH_NONE
    assert m.loc["nobody.pdf", S.MATCH_CONFIDENCE] == 0.0
    assert list(matching.match_resumes(ta, parsed, CFG).columns) == matching.MATCH_COLUMNS


def test_id_in_file_name_variants(ta):
    names = ["CAND-00012_Asha_Menon.pdf", "cand_00013.docx", "CAND00014.pdf", "Resume-CAND-00015-final.docx",
             "CAND 00016 Priya Nair.pdf", "CAND-000170.pdf"]
    m = by_file(matching.match_resumes(ta, as_dict(*[resume(n) for n in names]), CFG))
    assert m.loc[names[:5], S.CANDIDATE_ID].tolist() == ["CAND-00012", "CAND-00013", "CAND-00014", "CAND-00015",
                                                        "CAND-00016"]
    assert (m.loc[names[:5], S.MATCH_METHOD] == S.MATCH_ID_FILENAME).all()
    assert m.loc["CAND-000170.pdf", S.MATCH_METHOD] == S.MATCH_NONE  # a longer ID is not a token match


def test_candidate_id_guess_and_fuzzy_name_guess(ta):
    parsed = as_dict(resume("a.pdf", candidate_id_guess="cand-00013"),
                     resume("b.pdf", name_guess="Neha  Singhh"),         # typo -> fuzzy >= 92
                     resume("c.pdf", name_guess="Menon Asha"))             # reversed order -> exact token key
    m = by_file(matching.match_resumes(ta, parsed, CFG))
    assert m.loc["a.pdf", [S.CANDIDATE_ID, S.MATCH_METHOD]].tolist() == ["CAND-00013", S.MATCH_ID_TEXT]
    assert m.loc["b.pdf", S.CANDIDATE_ID] == "CAND-00014" and m.loc["b.pdf", S.MATCH_METHOD] == S.MATCH_NAME
    assert 0.83 < m.loc["b.pdf", S.MATCH_CONFIDENCE] < 0.9
    assert m.loc["c.pdf", S.CANDIDATE_ID] == "CAND-00012" and m.loc["c.pdf", S.MATCH_CONFIDENCE] == 0.9


def test_ambiguous_or_weak_names_are_not_matched():
    ta = ta_frame([
        {S.CANDIDATE_ID: "C1", S.NAME: "Rahul Sharma"}, {S.CANDIDATE_ID: "C2", S.NAME: "Rahul Sharma"},
        {S.CANDIDATE_ID: "C3", S.NAME: "Ananya Krishnan"}, {S.CANDIDATE_ID: "C4", S.NAME: "Ananya Krishnen"},
        {S.CANDIDATE_ID: "C5", S.NAME: "Asha Menon"},
    ])
    parsed = as_dict(resume("Rahul_Sharma_CV.pdf"),                     # duplicate TA name
                     resume("x.pdf", name_guess="Ananya Krishnon"),     # best - second < 5
                     resume("y.pdf", name_guess="Asha"),                # first name only
                     resume("z.pdf", name_guess="Asha K Menon"))          # initials ignored -> exact
    m = by_file(matching.match_resumes(ta, parsed, CFG))
    assert (m.loc[["Rahul_Sharma_CV.pdf", "x.pdf", "y.pdf"], S.MATCH_METHOD] == S.MATCH_NONE).all()
    assert m.loc["z.pdf", S.CANDIDATE_ID] == "C5"


def test_short_numeric_ids_and_years_are_not_id_tokens():
    ta = ta_frame([{S.CANDIDATE_ID: "12", S.NAME: "Neha Singh"}, {S.CANDIDATE_ID: "2026", S.NAME: "Ravi Kumar"},
                   {S.CANDIDATE_ID: "113244912", S.NAME: "Asha Menon"}, {S.CANDIDATE_ID: "113244913", S.NAME: "Divya Menon"}])
    parsed = as_dict(resume("Resume (12).pdf"), resume("Asha_Menon_2026.pdf"), resume("113244913_report.pdf"))
    m = by_file(matching.match_resumes(ta, parsed, CFG))
    assert m.loc["Resume (12).pdf", S.MATCH_METHOD] == S.MATCH_NONE
    assert m.loc["Asha_Menon_2026.pdf", [S.CANDIDATE_ID, S.MATCH_METHOD]].tolist() == ["113244912", S.MATCH_NAME]
    assert m.loc["113244913_report.pdf", [S.CANDIDATE_ID, S.MATCH_METHOD]].tolist() == ["113244913", S.MATCH_ID_FILENAME]


def test_priority_and_identity_conflicts(ta):
    ta = ta.assign(**{S.RESUME_FILE: pd.Series(["shared.pdf"] + [None] * 7, dtype="str")})
    parsed = as_dict(
        resume("CAND-00018_Meera.pdf", emails=["vikram.shah@example.com"], phones=["9876543217"]),
        resume("shared.pdf", text="ID CAND-00014"),                     # TA file wins over ID in text
        resume("CAND-00015-x.pdf", emails=["rohan.das@example.com"]),   # consistent -> no conflict
        resume("misnamed_CAND-00016.pdf", emails=["asha.menon@example.com"], file_hash="h-conflict2"),
    )
    m = by_file(matching.match_resumes(ta, parsed, CFG))
    row = m.loc["CAND-00018_Meera.pdf"]
    assert (row[S.CANDIDATE_ID], row[S.MATCH_METHOD]) == ("CAND-00018", S.MATCH_ID_FILENAME)
    assert "identity conflict: resume email belongs to CAND-00013" in row[matching.NOTE]
    assert "resume phone matches CAND-00019" in row[matching.NOTE]
    assert m.loc["shared.pdf", S.CANDIDATE_ID] == "CAND-00012" and m.loc["shared.pdf", S.MATCH_METHOD] == S.MATCH_TA_FILE
    assert m.loc["CAND-00015-x.pdf", matching.NOTE] == ""
    for note in m[matching.NOTE]:
        assert "@" not in note and "98765" not in note  # notes carry IDs, never contact details
    _, _, issues = matching.link_candidates(ta, parsed, matching.match_resumes(ta, parsed, CFG), CFG)
    conflicts = [i for i in issues if "identity conflict" in i["issue"]]
    assert {(i["candidate_id"], i["severity"]) for i in conflicts} == {("CAND-00018", "warning"), ("CAND-00016", "warning")}


def test_ta_resume_file_listed_for_other_candidate_is_a_conflict(ta):
    ta = ta.assign(**{S.RESUME_FILE: pd.Series(["CAND-00013_cv.pdf"] + [None] * 7, dtype="str")})
    m = by_file(matching.match_resumes(ta, as_dict(resume("CAND-00013_cv.pdf")), CFG))
    assert m.loc["CAND-00013_cv.pdf", S.CANDIDATE_ID] == "CAND-00012"
    assert "identity conflict: file name carries candidate ID CAND-00013" in m.loc["CAND-00013_cv.pdf", matching.NOTE]


def test_duplicates_keep_best_resume(ta):
    parsed = as_dict(
        resume("CAND-00012_Asha_Menon.pdf", word_count=420),
        resume("CAND-00012_Asha_Menon_v2.pdf", word_count=420),          # tie -> newer version kept
        resume("Vikram_Shah_Resume.docx", word_count=900),             # name 0.9
        resume("Resume (3).pdf", emails=["vikram.shah@example.com"], word_count=100),  # email 0.99 wins
        resume("CAND-00014_short.pdf", word_count=150),
        resume("CAND-00014_long.pdf", word_count=600),                 # same confidence -> more words
    )
    matches = matching.match_resumes(ta, parsed, CFG)
    m = by_file(matches)
    assert m.loc["CAND-00012_Asha_Menon_v2.pdf", S.MATCH_METHOD] == S.MATCH_ID_FILENAME
    assert m.loc["CAND-00012_Asha_Menon.pdf", S.MATCH_METHOD] == S.MATCH_DUPLICATE
    assert m.loc["CAND-00012_Asha_Menon.pdf", S.CANDIDATE_ID] == "CAND-00012"
    assert "duplicate of CAND-00012_Asha_Menon_v2.pdf" in m.loc["CAND-00012_Asha_Menon.pdf", matching.NOTE]
    assert m.loc["Resume (3).pdf", S.MATCH_METHOD] == S.MATCH_EMAIL
    assert m.loc["Vikram_Shah_Resume.docx", S.MATCH_METHOD] == S.MATCH_DUPLICATE
    assert m.loc["CAND-00014_long.pdf", S.MATCH_METHOD] == S.MATCH_ID_FILENAME
    assert m.loc["CAND-00014_short.pdf", S.MATCH_METHOD] == S.MATCH_DUPLICATE
    linked = matches[~matches[S.MATCH_METHOD].isin([S.MATCH_DUPLICATE, S.MATCH_NONE])]
    assert linked[S.CANDIDATE_ID].is_unique
    base, by_cand, issues = matching.link_candidates(ta, parsed, matches, CFG)
    assert by_cand["CAND-00012"].file_name == "CAND-00012_Asha_Menon_v2.pdf"
    assert sum(i["issue"].startswith("Duplicate resume") for i in issues) == 3


def test_shared_email_resolved_by_name():
    ta = ta_frame([{S.CANDIDATE_ID: "C1", S.NAME: "Kiran Kumar", S.EMAIL: "family@example.com"},
                   {S.CANDIDATE_ID: "C2", S.NAME: "Kavya Kumar", S.EMAIL: "family@example.com"}])
    m = by_file(matching.match_resumes(ta, as_dict(resume("cv.pdf", emails=["family@example.com"],
                                                           name_guess="Kavya Kumar")), CFG))
    row = m.loc["cv.pdf"]
    assert (row[S.CANDIDATE_ID], row[S.MATCH_METHOD], row[S.MATCH_CONFIDENCE]) == ("C2", S.MATCH_EMAIL, 0.9)
    assert "several candidates (C1, C2)" in row[matching.NOTE]


# ---------------------------------------------------------------------------
# Linking
# ---------------------------------------------------------------------------
def test_link_candidates_base_and_issues(ta):
    parsed = as_dict(resume("CAND-00012_Asha.pdf", word_count=300), resume("Resume (4).pdf", emails=["rohan.das@example.com"]),
                     resume("stranger.pdf", name_guess="Totally Unknown"))
    matches = matching.match_resumes(ta, parsed, CFG)
    base, by_cand, issues = matching.link_candidates(ta, parsed, matches, CFG)
    assert len(base) == len(ta) and base[S.CANDIDATE_ID].tolist() == ta[S.CANDIDATE_ID].tolist()
    link_cols = [S.RESUME_FILE, S.RESUME_HASH, S.HAS_RESUME, S.MATCH_METHOD, S.MATCH_CONFIDENCE]
    assert list(base.columns) == [S.CANDIDATE_ID, S.NAME, S.EMAIL, S.PHONE, S.HR_PCT, *link_cols, S.DATA_FLAGS,
                                  "extra_notes"]
    row = base.set_index(S.CANDIDATE_ID)
    assert row.loc["CAND-00012", [S.RESUME_FILE, S.HAS_RESUME, S.MATCH_METHOD]].tolist() == [
        "CAND-00012_Asha.pdf", True, S.MATCH_ID_FILENAME]
    assert row.loc["CAND-00012", S.RESUME_HASH] == hashlib.sha256(b"CAND-00012_Asha.pdf").hexdigest()
    assert row.loc["CAND-00015", S.MATCH_CONFIDENCE] == 0.99
    assert row.loc["CAND-00013", [S.RESUME_FILE, S.RESUME_HASH, S.HAS_RESUME, S.MATCH_METHOD, S.MATCH_CONFIDENCE]
                   ].tolist() == ["", "", False, "", 0.0]
    assert str(base[S.HAS_RESUME].dtype) == "bool" and str(base[S.MATCH_CONFIDENCE].dtype) == "float64"
    assert set(by_cand) == {"CAND-00012", "CAND-00015"} and by_cand["CAND-00015"].file_name == "Resume (4).pdf"
    assert [i for i in issues if i["file_name"] == "stranger.pdf"][0]["severity"] == "warning"
    assert any(i["issue"].startswith("6 of 8 TA candidates have no matching resume") for i in issues)
    per_candidate = {i["candidate_id"] for i in issues if i["issue"].startswith("No resume matched")}
    assert per_candidate == set(ta[S.CANDIDATE_ID]) - {"CAND-00012", "CAND-00015"}
    assert all(set(i) == {"severity", "stage", "issue", "candidate_id", "file_name"} for i in issues)
    assert all(i["stage"] == matching.STAGE for i in issues)


def test_ta_only_mode(ta):
    matches = matching.match_resumes(ta, {}, CFG)
    assert matches.empty and list(matches.columns) == matching.MATCH_COLUMNS
    base, by_cand, issues = matching.link_candidates(ta, {}, matches, CFG)
    assert not base[S.HAS_RESUME].any() and (base[S.MATCH_METHOD] == "").all() and by_cand == {}
    assert len(issues) == 1 and issues[0]["severity"] == "info" and "No resumes provided" in issues[0]["issue"]


def test_link_revalidates_edited_matches(ta):
    a, b, c = resume("one.pdf", word_count=10), resume("two.pdf", word_count=99), resume("three.pdf")
    parsed = as_dict(a, b, c)
    matches = pd.DataFrame([
        {matching.FILE_HASH: a.file_hash, matching.FILE_NAME: "one.pdf", S.CANDIDATE_ID: "CAND-00012",
         S.MATCH_METHOD: S.MATCH_NAME, S.MATCH_CONFIDENCE: 0.95, matching.NOTE: ""},
        {matching.FILE_HASH: b.file_hash, matching.FILE_NAME: "two.pdf", S.CANDIDATE_ID: "CAND-00012",
         S.MATCH_METHOD: S.MATCH_NAME, S.MATCH_CONFIDENCE: 0.95, matching.NOTE: ""},
        {matching.FILE_HASH: c.file_hash, matching.FILE_NAME: "three.pdf", S.CANDIDATE_ID: "CAND-99999",
         S.MATCH_METHOD: S.MATCH_EMAIL, S.MATCH_CONFIDENCE: 0.99, matching.NOTE: ""},
        {matching.FILE_HASH: "missing", matching.FILE_NAME: "ghost.pdf", S.CANDIDATE_ID: "CAND-00013",
         S.MATCH_METHOD: S.MATCH_EMAIL, S.MATCH_CONFIDENCE: np.nan, matching.NOTE: None},
    ])
    base, by_cand, issues = matching.link_candidates(ta, parsed, matches, CFG)
    assert by_cand["CAND-00012"].file_name == "two.pdf"  # equal confidence -> more words
    assert set(by_cand) == {"CAND-00012"}
    texts = {(i["file_name"], i["severity"], i["issue"]) for i in issues}
    assert ("one.pdf", "info", "Duplicate resume for this candidate; another file was linked.") in texts
    assert ("three.pdf", "warning", "Resume matched an ID that is not in the TA feed; ignored.") in texts
    assert any(f == "ghost.pdf" and s == "warning" for f, s, _ in texts)


# ---------------------------------------------------------------------------
# Resume-only mode
# ---------------------------------------------------------------------------
def test_resume_only_mode():
    parsed = as_dict(
        resume("CAND-00012_Asha_Menon.pdf", candidate_id_guess="CAND-00012", name_guess="ASHA MENON",
               emails=["Asha.Menon@example.com"], phones=["+91 98765 43210"], word_count=500,
               education={"college": "IIT Bombay", "degree": "B.Tech", "branch": "CSE", "cgpa": 8.7, "grad_year": 2026}),
        resume("Asha_Menon_v2.pdf", emails=["asha.menon@example.com"], word_count=300),  # same email -> duplicate
        resume("Ravi_Kumar_CV.pdf", file_hash="ab12cd34ef567890"),                    # generated RES- id
        resume("APP-4471 resume.pdf"),                                               # cfg pattern on file name
    )
    matches = matching.match_resumes(None, parsed, CFG)
    m = by_file(matches)
    assert m.loc["CAND-00012_Asha_Menon.pdf", [S.CANDIDATE_ID, S.MATCH_METHOD]].tolist() == ["CAND-00012", S.MATCH_RESUME_ONLY]
    assert m.loc["Asha_Menon_v2.pdf", [S.CANDIDATE_ID, S.MATCH_METHOD]].tolist() == ["CAND-00012", S.MATCH_DUPLICATE]
    assert m.loc["Ravi_Kumar_CV.pdf", S.CANDIDATE_ID] == "RES-AB12CD34"
    assert m.loc["APP-4471 resume.pdf", S.CANDIDATE_ID] == "APP-4471"
    base, by_cand, issues = matching.link_candidates(None, parsed, matches, CFG)
    assert len(base) == 3 and base[S.CANDIDATE_ID].is_unique
    row = base.set_index(S.CANDIDATE_ID).loc["CAND-00012"]
    assert row[S.NAME] == "Asha Menon" and row[S.EMAIL] == "asha.menon@example.com" and row[S.PHONE] == "9876543210"
    assert row[S.COLLEGE] == "IIT Bombay" and row[S.CGPA] == 8.7 and row[S.GRAD_YEAR] == 2026
    assert row[S.EMAIL_DOMAIN] == "example.com"
    assert base.set_index(S.CANDIDATE_ID).loc["RES-AB12CD34", S.NAME] == "Ravi Kumar"  # from the file name
    assert base[[S.HR_SCORE, S.MAX_SCORE, S.HR_PCT]].isna().all().all()
    assert (base[S.MATCH_METHOD] == S.MATCH_RESUME_ONLY).all() and base[S.HAS_RESUME].all()
    assert (base[S.DATA_FLAGS] == "no_assessment").all() and (base[S.MATCH_CONFIDENCE] == 1.0).all()
    assert str(base[S.GRAD_YEAR].dtype) == "Int64"
    assert set(by_cand) == set(base[S.CANDIDATE_ID]) and by_cand["CAND-00012"].file_name == "CAND-00012_Asha_Menon.pdf"
    assert any("Resume-only mode" in i["issue"] for i in issues)


def test_resume_only_never_merges_different_people():
    parsed = as_dict(
        resume("a/CAND-00012.pdf", file_hash="aaaa1111", candidate_id_guess="CAND-00012", emails=["asha@example.com"]),
        resume("b/CAND-00012.pdf", file_hash="bbbb2222", candidate_id_guess="CAND-00012", emails=["ravi@example.com"]),
        resume("CV2026.pdf", file_hash="cccc3333"),
        resume("RESUME-2025.pdf", file_hash="dddd4444"),
    )
    matches = matching.match_resumes(None, parsed, CFG)
    ids = dict(zip(matches[matching.FILE_HASH], matches[S.CANDIDATE_ID]))
    assert ids == {"aaaa1111": "CAND-00012", "bbbb2222": "RES-BBBB2222", "cccc3333": "RES-CCCC3333",
                   "dddd4444": "RES-DDDD4444"}
    assert (matches[S.MATCH_METHOD] == S.MATCH_RESUME_ONLY).all()
    note = matches.set_index(matching.FILE_HASH).loc["bbbb2222", matching.NOTE]
    assert "kept as a separate candidate" in note and "@" not in note


# ---------------------------------------------------------------------------
# Robustness, determinism, performance
# ---------------------------------------------------------------------------
def test_degenerate_inputs_never_raise():
    empty_ta = ta_frame([{S.CANDIDATE_ID: "C1"}]).iloc[0:0]
    parsed = as_dict(resume("C1.pdf", text=None, emails=None, phones=None, name_guess=None))  # type: ignore[arg-type]
    m = matching.match_resumes(empty_ta, parsed, CFG)
    assert m[S.MATCH_METHOD].tolist() == [S.MATCH_NONE]
    bare = pd.DataFrame({S.CANDIDATE_ID: pd.Series(["C1", None], dtype="str")})  # no name/email/phone columns
    m2 = matching.match_resumes(bare, as_dict(resume("x_C1_.pdf"), resume("y.pdf", emails=["a@b.co"])), CFG)
    assert m2.set_index(matching.FILE_NAME).loc["y.pdf", S.MATCH_METHOD] == S.MATCH_NONE
    base, _, _ = matching.link_candidates(bare, {}, matching.match_resumes(bare, {}, CFG), CFG)
    assert len(base) == 2
    assert matching.match_resumes(None, {}, CFG).empty


def test_junk_resumes_never_raise(ta):
    rng = random.Random(3)
    junk = ["", "@", "a@b", "CAND-", "CAND-00012-CAND-00013", "+91", "١٢٣", "(1)", "..", "v2", "x" * 300, "9" * 25]
    parsed = {}
    for i in range(60):
        p = ParsedResume(file_name=rng.choice(junk) + rng.choice([".pdf", "", ".docx"]), file_hash=f"h{i}",
                         text=" ".join(rng.choices(junk, k=5)), emails=rng.choices(junk, k=2),
                         phones=rng.choices(junk, k=2), name_guess=rng.choice([None, *junk]),
                         candidate_id_guess=rng.choice([None, *junk]), word_count=rng.randint(0, 3),
                         education=rng.choice([{}, {"cgpa": "x", "grad_year": "20x6", "college": 5}]))
        parsed[p.file_hash] = p
    for frame_ in (ta, None):
        matches = matching.match_resumes(frame_, parsed, CFG)
        assert len(matches) == len(parsed)
        base, by_cand, issues = matching.link_candidates(frame_, parsed, matches, CFG)
        assert base[S.CANDIDATE_ID].is_unique


def test_matching_is_deterministic(ta):
    items = [resume(f"CAND-000{12 + i % 8}_{i}.pdf", word_count=i % 3) for i in range(24)]
    items += [resume("Resume (1).pdf", emails=["asha.menon@example.com"]), resume("Neha_Singh.pdf")]
    first = matching.match_resumes(ta, as_dict(*items), CFG)
    random.Random(5).shuffle(items)
    second = matching.match_resumes(ta, as_dict(*items), CFG)
    pd.testing.assert_frame_equal(first, second)


def _perf_dataset(n: int = 3000) -> tuple[pd.DataFrame, dict[str, ParsedResume], dict[str, str | None]]:
    """3,000 TA rows x 3,000 stubs over every path; returns (ta, parsed, file name -> expected id)."""
    rng = random.Random(42)
    firsts = [f"First{chr(65 + i // 26)}{chr(97 + i % 26)}" for i in range(60)]
    lasts = [f"Last{chr(65 + i // 26)}{chr(97 + i % 26)}" for i in range(60)]
    names = rng.sample([f"{f} {l}" for f in firsts for l in lasts], n)
    ta = ta_frame([{S.CANDIDATE_ID: f"CAND-{i:05d}", S.NAME: names[i], S.EMAIL: f"user{i}@example.com",
                    S.PHONE: f"9{i:09d}", S.DATA_FLAGS: ""} for i in range(n)])
    resumes, expected = [], {}
    for i in range(n):
        u = rng.random()
        first, last = names[i].split()
        cid: str | None = f"CAND-{i:05d}"
        if u < 0.40:
            p = resume(f"CAND-{i:05d}_{first}_{last}.pdf", emails=[f"user{i}@example.com"])
        elif u < 0.65:
            p = resume(f"Resume ({i}).pdf", emails=[f"USER{i}@example.com"])
        elif u < 0.75:
            p = resume(f"doc{i}.pdf", text=f"{names[i]}\nCandidate ID CAND-{i:05d}\n" + "Built APIs in Python. " * 30)
        elif u < 0.85:
            p = resume(f"cv{i}.pdf", phones=[f"+91 9{i:09d}"], text="Projects 2024 2025 v2.1 " * 20)
        elif u < 0.90:
            p = resume(f"{first}_{last}_Resume.docx")                   # name from the file name only
        elif u < 0.95:
            p = resume(f"cv_{i}.pdf", name_guess=f"{first}  {last.upper()}")
        else:
            p, cid = resume(f"unknown_{i}.pdf", name_guess=f"Someone Else{i}", text="No identifiers here 2026"), None
        resumes.append(p)
        expected[p.file_name] = cid
    return ta, as_dict(*resumes), expected


def test_performance_3000_by_3000():
    ta, parsed, expected = _perf_dataset()
    t0 = time.perf_counter()
    matches = matching.match_resumes(ta, parsed, CFG)
    base, by_cand, issues = matching.link_candidates(ta, parsed, matches, CFG)
    elapsed = time.perf_counter() - t0
    assert elapsed < 3.0, f"matching 3,000 x 3,000 took {elapsed:.2f}s"
    counts = matches[S.MATCH_METHOD].value_counts().to_dict()
    assert {S.MATCH_ID_FILENAME, S.MATCH_EMAIL, S.MATCH_ID_TEXT, S.MATCH_PHONE, S.MATCH_NAME, S.MATCH_NONE} <= set(counts)
    got = {f: c if isinstance(c, str) else None for f, c in zip(matches[matching.FILE_NAME], matches[S.CANDIDATE_ID])}
    assert got == expected  # every resume linked to the right candidate (or to nobody)
    assert base[S.HAS_RESUME].sum() == len(by_cand) == sum(v is not None for v in expected.values())


def test_name_only_match_is_not_linked_when_contact_details_disagree():
    """A walk-in 'Arjun Dhillon' (own email/phone) must not be linked to TA's Arjun Dhillon."""
    import pandas as pd
    from screening import matching
    from screening import schema as S
    from screening.config import AppConfig
    from screening.models import ParsedResume

    ta = pd.DataFrame({
        S.CANDIDATE_ID: ["CAND-00001", "CAND-00002"],
        S.NAME: ["Arjun Dhillon", "Meera Iyer"],
        S.EMAIL: ["arjun.d@example.com", "meera@example.com"],
        S.PHONE: ["9000000001", "9000000002"],
    })
    walk_in = ParsedResume(file_name="arjundhillon_cv.docx", file_hash="h1", text="Arjun Dhillon",
                           name_guess="Arjun Dhillon", emails=["arjun.other@example.org"],
                           phones=["9111111111"], word_count=300)
    no_contact = ParsedResume(file_name="Meera_Iyer_Resume.pdf", file_hash="h2", text="Meera Iyer",
                              name_guess="Meera Iyer", word_count=300)
    m = matching.match_resumes(ta, {"h1": walk_in, "h2": no_contact}, AppConfig()).set_index("file_hash")
    assert m.loc["h1", S.MATCH_METHOD] == S.MATCH_NONE
    assert "possible name match CAND-00001" in m.loc["h1", "note"]
    assert m.loc["h2", S.MATCH_METHOD] == S.MATCH_NAME            # no contradicting contact -> linked
    assert m.loc["h2", S.CANDIDATE_ID] == "CAND-00002"


# ---------------------------------------------------------------------------
# Workday packet splits: "{candidate_id}_{Cleaned_Name}.pdf" and "..._att2.pdf"
# ---------------------------------------------------------------------------
RESUME_SECTIONS = {"education": "B.Tech", "skills": "Python", "projects": "Portal", "experience": "Intern"}
WORKDAY_TA = [("C-1000042", "Asha Menon", "asha.menon@example.com"), ("C-1000049", "Ravi Kumar", "ravi@example.com"),
              ("C-1000999", "Asha Menon", "asha42@xyiict.example"), ("C-1000056", "Neha Singh", "neha@example.org")]


@pytest.fixture()
def workday_ta() -> pd.DataFrame:
    return ta_frame([{S.CANDIDATE_ID: c, S.NAME: n, S.EMAIL: e} for c, n, e in WORKDAY_TA])


def test_split_file_names_match_by_candidate_id(workday_ta):
    parsed = as_dict(resume("C-1000042_Asha_Menon.pdf", word_count=300, sections=RESUME_SECTIONS),
                     resume("C-1000049_Ravi_Kumar.pdf", word_count=280, name_guess="Somebody Else",
                            emails=["ravi@example.com"]),
                     resume("C-1000056_Neha_Singh.pdf", word_count=10))     # near-empty scan still links by ID
    m = by_file(matching.match_resumes(workday_ta, parsed, CFG))
    assert m[S.MATCH_METHOD].eq(S.MATCH_ID_FILENAME).all() and m[S.MATCH_CONFIDENCE].eq(1.0).all()
    assert m[S.CANDIDATE_ID].to_dict() == {"C-1000042_Asha_Menon.pdf": "C-1000042",
                                           "C-1000049_Ravi_Kumar.pdf": "C-1000049",
                                           "C-1000056_Neha_Singh.pdf": "C-1000056"}
    assert matching.attachment_index("C-1000042_Asha_Menon_att2.pdf") == 2
    assert matching.attachment_index("C-1000042_Asha_Menon.pdf") == 1
    assert matching.name_from_filename("C-1000042_Asha_Menon_att2.pdf") == "asha menon"


@pytest.mark.parametrize("primary, second, winner", [
    # a longer cover letter does not replace the resume
    ({"word_count": 300, "sections": RESUME_SECTIONS}, {"word_count": 900}, "primary"),
    # two resume-like attachments: the larger one is kept
    ({"word_count": 300, "sections": RESUME_SECTIONS}, {"word_count": 500, "sections": RESUME_SECTIONS}, "second"),
    # identical size and content: the primary attachment wins the tie
    ({"word_count": 300, "sections": RESUME_SECTIONS}, {"word_count": 300, "sections": RESUME_SECTIONS}, "primary"),
], ids=["cover-letter", "larger-resume", "tie"])
def test_second_attachment_of_the_same_candidate(workday_ta, primary, second, winner):
    parsed = as_dict(resume("C-1000042_Asha_Menon.pdf", **primary), resume("C-1000042_Asha_Menon_att2.pdf", **second))
    matches = matching.match_resumes(workday_ta, parsed, CFG)
    m = by_file(matches)
    keep, other = (("C-1000042_Asha_Menon.pdf", "C-1000042_Asha_Menon_att2.pdf") if winner == "primary"
                   else ("C-1000042_Asha_Menon_att2.pdf", "C-1000042_Asha_Menon.pdf"))
    assert m.loc[keep, S.MATCH_METHOD] == S.MATCH_ID_FILENAME
    assert m.loc[other, S.MATCH_METHOD] == S.MATCH_DUPLICATE and m.loc[other, S.CANDIDATE_ID] == "C-1000042"
    assert m.loc[other, matching.NOTE] == f"additional attachment of C-1000042; {keep} linked as the resume"
    _base, by_cand, issues = matching.link_candidates(workday_ta, parsed, matches, CFG)
    assert by_cand["C-1000042"].file_name == keep
    assert [i["issue"] for i in issues if i["file_name"] == other] == [
        "Additional attachment for this candidate (not scored); the primary resume was linked."]


def test_duplicate_application_note_vs_identity_conflict(workday_ta):
    parsed = as_dict(resume("C-1000999_Asha_Menon.pdf", emails=["asha.menon@example.com"], word_count=300),
                     resume("C-1000056_Neha_Singh.pdf", emails=["ravi@example.com"], word_count=300))
    m = by_file(matching.match_resumes(workday_ta, parsed, CFG))
    assert m.loc["C-1000999_Asha_Menon.pdf", S.CANDIDATE_ID] == "C-1000999"
    assert m.loc["C-1000999_Asha_Menon.pdf", matching.NOTE] == (
        "possible duplicate application: resume email belongs to C-1000042 (same name)")
    assert m.loc["C-1000056_Neha_Singh.pdf", matching.NOTE] == (
        f"{matching.IDENTITY_CONFLICT}: resume email belongs to C-1000049")
