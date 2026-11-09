"""Workday formats: the assessment export workbook and the multi-candidate resume packets.

Everything is generated at test time with fictional people, IDs ("C-1000042"), requisitions
("R-100200") and domains (example.com / "<slug>.example").
"""
from __future__ import annotations

import io
import time
import zipfile

import numpy as np
import pandas as pd
import pypdf
import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

import screening.schema as S
from screening import ingest, matching, risk
from screening import resume_parser as RP
from screening.config import AppConfig, ProcessingConfig
from screening.models import ResumeDoc

CFG = AppConfig()
MB = 1024 * 1024
HEADERS = ["Job Application", "Candidate ID", "Assessment Comment", "Email", "CGPA", "Branch", "School", "Location",
           "Max Score", "Score", "Cutoff Score", "Cutoff Score Percentage", "Percentage Score", "Candidate Option"]
EXPECTED_MAPPING = {
    "Job Application": S.JOB_APPLICATION, "Candidate ID": S.CANDIDATE_ID, "Assessment Comment": S.ASSESSMENT_COMMENT,
    "Email": S.EMAIL, "CGPA": S.CGPA, "Branch": S.BRANCH, "School": S.COLLEGE, "Location": S.CITY,
    "Max Score": S.MAX_SCORE, "Score": S.HR_SCORE, "Cutoff Score": S.CUTOFF_SCORE,
    "Cutoff Score Percentage": S.CUTOFF_PCT, "Percentage Score": S.HR_PCT, "Candidate Option": S.CANDIDATE_OPTION,
}
CLEAN = "Hackerrank Completed – Qualified&#xa;Integrity Issues: No Issues Detected\nIntegrity Summary: N/A"
MEDIUM = ("Hackerrank Completed – Qualified&#xa;Integrity Issues: Medium\n"
          "Integrity Summary: Detected Code Similarity With Other Submissions")
HIGH = ("Hackerrank Completed – Qualified&#xa;Integrity Issues: High\n"
        "Integrity Summary: Detected Code Similarity With Other Submissions")
FIRST = ["Asha", "Ravi", "Neha", "Vikram", "Priya", "Arjun", "Meera", "Kiran", "Rohan", "Divya", "Sanjay", "Anita"]
LAST = ["Menon", "Kumar", "Singh", "Shah", "Nair", "Iyer", "Das", "Reddy", "Gupta", "Joshi", "Patel", "Kapoor"]
SCHOOLS = [("XY-IICT", "xyiict.example"), ("Example Institute of Technology", "eit.example"),
           ("Sample University of Engineering", "sue.example")]
BRANCHES = ["Computer Science & Engineering"] * 7 + ["Information Technology", "Electronics & Communication",
                                                     "AI & ML"]
OPTIONS = ["Django (Python)", "Node.JS (JavaScript)", "Spring Boot (Java)"]
SUMMARY_SHEET = [["Review Candidates: R-100200 20", None], ["Active Candidates", "14673 of 32326"],
                 ["Applied Prefilters", None], ["Numeric Return Attribute", "Recruiting Stage (Workday Owner)"],
                 ["10696", "Review"], ["0", "Screen"], ["3977", "Assessment"]]


def person(i: int) -> tuple[str, str]:
    return f"{FIRST[i % 12]} {LAST[(i * 5 + i // 12) % 12]}", f"C-{1000042 + 7 * i}"


def export_rows(n: int = 40, seed: int = 3) -> list[list]:
    """Rows of the Workday "Sheet2" (14 columns, no Name / Phone), last row unscored."""
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n):
        name, cid = person(i)
        school, domain = SCHOOLS[i % 3]
        email = (f"{name.split()[0].lower()}.{name.split()[1].lower()}{i}@example.com" if i % 2
                 else f"{name.split()[0].lower()}{i}@{domain}")
        score = 175.0 if rng.random() < 0.64 else round(float(rng.uniform(100, 174.9)), 2)
        comment = MEDIUM if i == 3 else HIGH if i == 7 else CLEAN
        rows.append([f"{name} ({name.lower()})", cid, comment, email, round(float(rng.uniform(8.5, 10.0)), 2),
                     BRANCHES[i % len(BRANCHES)], school, ["Pune", "Chennai", "Hyderabad"][i % 3], 175, score, 96, 55,
                     round(score / 175 * 100, 2), OPTIONS[i % 3]])
    rows[-1][8:] = [100, "#N/A", 96, "#N/A", "#N/A", "#N/A"]
    return rows


def xlsx(sheets: list[tuple[str, list[list]]]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    for title, rows in sheets:
        ws = wb.create_sheet(title)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def workday_export(rows: list[list] | None = None) -> bytes:
    return xlsx([("Sheet1", SUMMARY_SHEET), ("Sheet2", [HEADERS] + (rows or export_rows()))])


def frame(data: dict[str, list]) -> pd.DataFrame:
    return pd.DataFrame({k: pd.Series(v, dtype="str") for k, v in data.items()})


# ---------------------------------------------------------------------------
# Assessment export
# ---------------------------------------------------------------------------
def test_export_reads_the_data_sheet_and_maps_every_column():
    rows = export_rows()
    raw = ingest.read_table("R-100200 assessment.xlsx", workday_export(rows))
    assert list(raw.columns) == HEADERS and len(raw) == len(rows)
    assert ingest.suggest_column_mapping(HEADERS) == EXPECTED_MAPPING

    df, report = ingest.load_ta_feed("R-100200 assessment.xlsx", workday_export(rows), CFG)
    assert report.rows_raw == report.rows_clean == len(rows)
    assert df[S.CANDIDATE_ID].tolist() == [r[1] for r in rows]                  # "C-1234567" kept exactly
    assert df[S.NAME].tolist()[:2] == [person(0)[0], person(1)[0]]               # from "Job Application"
    assert df[S.JOB_APPLICATION][0] == f"{person(0)[0]} ({person(0)[0].lower()})"
    assert S.NAME not in report.missing_fields
    assert df[S.MAX_SCORE].iloc[:-1].eq(175).all() and df[S.CUTOFF_SCORE].eq(96).all()
    assert df[S.CUTOFF_PCT].iloc[:-1].eq(55).all()
    np.testing.assert_allclose(df[S.HR_PCT].iloc[:-1], [r[12] for r in rows[:-1]], atol=0.01)
    assert set(df[S.CANDIDATE_OPTION].dropna()) == set(OPTIONS)
    assert df[S.TEST_STATUS].iloc[:-1].eq("Completed").all()
    assert df[S.INTEGRITY_STATUS].tolist()[:8] == ["Clear"] * 3 + ["Medium"] + ["Clear"] * 3 + ["High"]
    assert df[S.PROCTORING_FLAGS][3] == "Detected Code Similarity With Other Submissions"
    assert df[S.PROCTORING_FLAGS].notna().sum() == 2
    assert df[S.ASSESSMENT_COMMENT][0] == ("Hackerrank Completed – Qualified\nIntegrity Issues: No Issues "
                                           "Detected\nIntegrity Summary: N/A")
    assert any("read from the 'Assessment Comment' column" in i["issue"] for i in report.issues)
    assert not set(report.missing_fields) & {S.TEST_STATUS, S.INTEGRITY_STATUS, S.PROCTORING_FLAGS}


def test_unscored_row_is_flagged_not_dropped():
    df, report = ingest.load_ta_feed("export.xlsx", workday_export(), CFG)
    last = df.iloc[-1]
    assert pd.isna(last[S.HR_SCORE]) and pd.isna(last[S.HR_PCT]) and pd.isna(last[S.CUTOFF_PCT])
    assert pd.isna(last[S.CANDIDATE_OPTION]) and last[S.MAX_SCORE] == 100
    assert "score_missing" in last[S.DATA_FLAGS]
    assert df[S.DATA_FLAGS].iloc[:-1].eq("").all()
    assert any("no assessment score" in i["issue"] for i in report.issues)


@pytest.mark.parametrize("summary_first", [True, False])
def test_sheet_choice_follows_the_header_mapping(summary_first):
    pivot = [["Stage", "Owner", "Count"], ["Review", "Recruiter", 10696], ["Assessment", "Recruiter", 3977]]
    data = [HEADERS] + export_rows(5)
    sheets = [("Pivot", pivot), ("Data", data)] if summary_first else [("Data", data), ("Pivot", pivot)]
    raw = ingest.read_table("export.xlsx", xlsx(sheets))
    assert list(raw.columns) == HEADERS and len(raw) == 5
    only_summary = ingest.read_table("summary.xlsx", xlsx([("Sheet1", SUMMARY_SHEET)]))
    assert only_summary.shape[0] >= 1                                     # still readable, nothing mapped


@pytest.mark.parametrize("comment, status, integrity, summary", [
    (CLEAN, "Completed", "Clear", None),
    ("Hackerrank Completed - Qualified&#10;Integrity Issues: None&#10;Integrity Summary: -",
     "Completed", "Clear", None),
    ("Hackerrank Completed — Qualified&amp;#xa;Integrity Issues: N/A&amp;#xa;Integrity Summary: None",
     "Completed", "Clear", None),
    ("Hackerrank Completed–Qualified\nIntegrity Issues: Medium\nIntegrity Summary: Webcam turned off",
     "Completed", "Medium", "Webcam turned off"),
    ("Hackerrank Completed-Qualified&#xa;Integrity Issues: HIGH&#xa;Integrity Summary: "
     "Detected Code Similarity With Other Submissions", "Completed", "High",
     "Detected Code Similarity With Other Submissions"),
    ("Hackerrank Completed &#8211; Qualified&#xa;Integrity Issues: low&#xa;Integrity Summary: N/A",
     "Completed", "Low", None),
    ("Hackerrank Expired&#xa;Integrity Issues: N/A", "Expired", "Clear", None),
    ("Integrity Issues: Review\nIntegrity Summary: Tab switching observed", None, "Review", "Tab switching observed"),
], ids=["en-dash", "hyphen-&#10;", "em-dash-double-escaped", "no-space-dash", "ascii-dash-upper", "dash-entity",
        "expired", "labels-only"])
def test_composite_comment_variants(comment, status, integrity, summary):
    df, _ = ingest.normalize_ta_feed(frame({"Candidate ID": ["C-1000042", "C-1000049"], "Score": ["150", "160"],
                                            "Assessment Comment": [comment, CLEAN]}),
                                     {"Candidate ID": S.CANDIDATE_ID, "Score": S.HR_SCORE,
                                      "Assessment Comment": S.ASSESSMENT_COMMENT}, CFG)
    got = df.iloc[0]
    assert (None if pd.isna(got[S.TEST_STATUS]) else got[S.TEST_STATUS]) == status
    assert got[S.INTEGRITY_STATUS] == integrity
    assert (None if pd.isna(got[S.PROCTORING_FLAGS]) else got[S.PROCTORING_FLAGS]) == summary
    assert "&#" not in got[S.ASSESSMENT_COMMENT] and "&amp;" not in got[S.ASSESSMENT_COMMENT]


def test_separately_mapped_columns_win_over_the_comment():
    raw = frame({"Candidate ID": ["C-1000042", "C-1000049"], "Score": ["150", "160"],
                 "Assessment Comment": [MEDIUM, CLEAN], "Integrity Status": ["Clear", "Review"],
                 "Status": ["In Progress", "Completed"]})
    df, _ = ingest.normalize_ta_feed(raw, ingest.suggest_column_mapping(list(raw.columns)), CFG)
    assert df[S.INTEGRITY_STATUS].tolist() == ["Clear", "Review"]
    assert df[S.TEST_STATUS].tolist() == ["In Progress", "Completed"]
    assert df[S.PROCTORING_FLAGS][0] == "Detected Code Similarity With Other Submissions"   # not mapped -> filled


def test_free_text_comment_is_kept_but_not_parsed():
    df, report = ingest.normalize_ta_feed(
        frame({"Candidate ID": ["C-1000042", "C-1000049"], "Comments": ["Strong communication &amp; clear code",
                                                                        "Completed the take-home task"]}),
        {"Candidate ID": S.CANDIDATE_ID, "Comments": S.ASSESSMENT_COMMENT}, CFG)
    assert df[S.ASSESSMENT_COMMENT][0] == "Strong communication & clear code"
    assert S.TEST_STATUS not in df.columns and S.INTEGRITY_STATUS not in df.columns
    assert not any("read from the" in i["issue"] for i in report.issues)


def test_integrity_columns_named_like_comment_parts():
    raw = frame({"Candidate ID": ["C-1000042", "C-1000049", "C-1000056"],
                 "Integrity Issues": ["No Issues Detected", "Medium", "high"]})
    df, _ = ingest.normalize_ta_feed(raw, ingest.suggest_column_mapping(list(raw.columns)), CFG)
    assert df[S.INTEGRITY_STATUS].tolist() == ["Clear", "Medium", "High"]


def test_job_application_fills_only_missing_names():
    raw = frame({"Candidate ID": ["C-1000042", "C-1000049", "C-1000056"],
                 "Name": ["Asha K. Menon", "", ""],
                 "Job Application": ["Asha Menon (asha menon)", "ravi kumar (ravi kumar)", "(neha singh)"]})
    df, report = ingest.normalize_ta_feed(raw, ingest.suggest_column_mapping(list(raw.columns)), CFG)
    assert df[S.NAME].tolist() == ["Asha K. Menon", "Ravi Kumar", "Neha Singh"]
    assert any("2 missing candidate name(s) taken from the 'Job Application'" in i["issue"] for i in report.issues)


def test_flagged_comments_raise_integrity_flags():
    rows = export_rows(12)
    rows[5][2] = rows[5][2].replace("No Issues Detected", "Low")
    df, _ = ingest.load_ta_feed("export.xlsx", workday_export(rows), CFG)
    result = risk.run_risk_analysis(df, {}, CFG)
    flags = result.individual.groupby(S.CANDIDATE_ID)["flag_key"].apply(set).to_dict()
    assert flags == {rows[3][1]: {"integrity_status", "proctoring_flags"},
                     rows[7][1]: {"integrity_status", "proctoring_flags"}}
    detail = result.individual.set_index([S.CANDIDATE_ID, "flag_key"])["detail"]
    assert detail[(rows[3][1], "integrity_status")] == "Assessment integrity issues: Medium"
    cr = result.candidate_risk.set_index(S.CANDIDATE_ID)
    assert cr.loc[rows[7][1], S.RISK_LEVEL] == S.RISK_MEDIUM and cr.loc[rows[0][1], S.RISK_LEVEL] == S.RISK_CLEAR


# ---------------------------------------------------------------------------
# Resume packets
# ---------------------------------------------------------------------------
PACKET = "R-100200 - 2027 Technology Program Intern_set {}.pdf"


def _resume_page(c: canvas.Canvas, name: str, page: int, *, cover: bool = False, show_name: bool = True,
                 hidden: str | None = None) -> None:
    c.setFont("Helvetica", 11)
    if cover:
        lines = ["Cover Letter", f"{name}", "Dear Hiring Team,",
                 "I would like to apply for the 2027 Technology Program internship.",
                 "I enjoy building software that people use every day and learning from experienced engineers."]
    else:
        email = f"{name.split()[0].lower()}.{name.split()[-1].lower()}@example.com"
        lines = ([name.upper()] if show_name else ["CURRICULUM VITAE"]) + [
            email if show_name else "applicant@example.com",
            "EDUCATION", "B.Tech Computer Science and Engineering, Example Institute of Technology, CGPA 9.1",
            "TECHNICAL SKILLS", "Python, SQL, Git, Django",
            "PROJECTS", "Campus Events Portal | Django, SQL",
            "- Built an events portal used by 1,200 students with role-based access.",
            f"Page {page}"]
    for k, line in enumerate(lines):
        c.drawString(60, 800 - 16 * k, line)
    if hidden:
        c.setFillColorRGB(1, 1, 1)
        c.drawString(60, 120, hidden)
        c.setFillColorRGB(0, 0, 0)


def build_packet(cands: list[tuple[str, str]], *, pages=lambda i: 1, extra_attachment: frozenset | set = frozenset(),
                 rows_per_toc_page: int = 20, outline: bool = True, plain_titles: bool = False,
                 names_on_pages: bool = True, hidden: dict[int, str] | None = None) -> bytes:
    """A Workday-style packet: TOC pages, then each candidate's attachment pages (one bookmark each)."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4, invariant=1)
    for start in range(0, len(cands), rows_per_toc_page):
        c.setFont("Helvetica", 10)
        head = (["Table of Contents", "R-100200 - 2027 Technology Program Intern (3 of 5)"] if start == 0 else [])
        head.append("Candidate Name Candidate ID Attachments")
        y = 800
        for line in head:
            c.drawString(60, y, line)
            y -= 16
        for i, (name, cid) in enumerate(cands[start:start + rows_per_toc_page], start=start + 1):
            c.drawString(60, y, f"{i}. {name} {cid}")
            y -= 16
        c.showPage()
    key = 0
    for i, (name, cid) in enumerate(cands):
        for attachment in range(2 if i in extra_attachment else 1):
            for page in range(pages(i) if attachment == 0 else 1):
                if page == 0 and outline:
                    key += 1
                    c.bookmarkPage(f"b{key}")
                    c.addOutlineEntry(name if plain_titles else f"{name} ({name.lower()})", f"b{key}", level=0)
                _resume_page(c, name, page + 1, cover=attachment == 1, show_name=names_on_pages,
                             hidden=(hidden or {}).get(i) if attachment == 0 and page == 0 else None)
                c.showPage()
    c.save()
    return buf.getvalue()


def zip_of(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Resume/", b"")
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


CANDS = [person(i) for i in range(30)]
PAGES = (lambda i: 3 if i == 4 else 2 if i % 5 == 0 else 1)


@pytest.fixture(scope="module")
def packet() -> bytes:
    return build_packet(CANDS, pages=PAGES, extra_attachment={6})


def _range(doc: RP.PacketResumeDoc) -> tuple[int, int]:
    first, last = doc.bundle_source.rsplit(" p.", 1)[1].split("-")
    return int(first), int(last)


def test_packet_split_names_pages_and_sources(packet):
    issues: list[dict] = []
    docs = RP.split_bundle(ResumeDoc(PACKET.format(3), packet), ProcessingConfig(), issues)
    names = [d.file_name for d in docs]
    assert len(docs) == len(CANDS) + 1
    assert names[0] == f"{CANDS[0][1]}_{CANDS[0][0].replace(' ', '_')}.pdf"
    assert f"{CANDS[6][1]}_{CANDS[6][0].replace(' ', '_')}_att2.pdf" in names
    assert all(d.source == f"bundle:{PACKET.format(3)}" for d in docs)
    assert all(d.bundle_source.startswith(PACKET.format(3) + " p.") for d in docs)
    toc_pages = 2                                                        # 30 rows, 20 per TOC page
    assert _range(docs[0]) == (toc_pages + 1, toc_pages + 2)              # candidate 0: 2 pages
    assert _range(docs[4])[1] - _range(docs[4])[0] == 2                  # candidate 4: 3 pages
    ends = [_range(d) for d in docs]
    assert all(a <= b for a, b in ends) and all(ends[k][1] + 1 == ends[k + 1][0] for k in range(len(ends) - 1))
    assert ends[-1][1] == len(pypdf.PdfReader(io.BytesIO(packet)).pages)
    assert len({d.sha256 for d in docs}) == len(docs)
    (info,) = issues
    assert info["severity"] == "info" and info["file_name"] == PACKET.format(3)
    assert info["issue"].startswith(f"Split multi-candidate packet into {len(docs)} file(s) for {len(CANDS)}")


def test_packet_zip_expands_member_by_member_with_progress(packet):
    small = build_packet(CANDS[:6], plain_titles=True)
    archive = zip_of({"Resume/" + PACKET.format(1): packet, "Resume/" + PACKET.format(2): small,
                      "Resume/notes.txt": b"Loose Person\nSkills: Python, SQL and Git"})
    calls: list[tuple[int, int, str]] = []
    cfg = ProcessingConfig(max_file_mb=(len(packet) - 1) / MB)          # the packet alone breaks the per-file cap
    out, issues = RP.expand_uploads([ResumeDoc("Resume.zip", archive)], cfg,
                                    progress_cb=lambda d, t, detail: calls.append((d, t, detail)))
    assert len(out) == len(CANDS) + 1 + 6 + 1
    assert out[-1].file_name == "notes.txt" and out[-1].source == "zip:Resume.zip"
    assert {d.source for d in out[:-1]} == {f"bundle:{PACKET.format(1)}", f"bundle:{PACKET.format(2)}"}
    assert [c[0] for c in calls] == [1, 2, 3] and {c[1] for c in calls} == {3}
    assert [i["severity"] for i in issues] == ["info", "info"]


def test_split_files_still_meet_the_per_file_cap(packet):
    docs = RP.split_bundle(ResumeDoc(PACKET.format(3), packet), ProcessingConfig())
    biggest = max(len(d.content) for d in docs)
    out, issues = RP.expand_uploads([ResumeDoc(PACKET.format(3), packet)],
                                    ProcessingConfig(max_file_mb=(biggest - 1) / MB))
    assert len(out) < len(docs) and all(len(d.content) < biggest for d in out)
    assert any(i["issue"].startswith("File is larger than") for i in issues)


def test_packet_over_the_bundle_limit_is_skipped(packet):
    cfg = ProcessingConfig(max_file_mb=0.001, max_bundle_mb=(len(packet) - 1) / MB)
    out, issues = RP.expand_uploads([ResumeDoc("Resume.zip", zip_of({"Resume/" + PACKET.format(1): packet}))], cfg)
    assert out == [] and "multi-candidate packet limit" in issues[0]["issue"]
    out, issues = RP.expand_uploads([ResumeDoc(PACKET.format(1), packet)], cfg)
    assert out == [] and "larger than" in issues[0]["issue"]


def test_packet_without_outline_falls_back_to_names_on_pages():
    issues: list[dict] = []
    docs = RP.split_bundle(ResumeDoc(PACKET.format(4), build_packet(CANDS[:8], pages=PAGES, outline=False)),
                           ProcessingConfig(), issues)
    assert [d.file_name.split("_")[0] for d in docs] == [cid for _, cid in CANDS[:8]]
    assert _range(docs[0]) == (2, 3) and _range(docs[4]) == (7, 9) and _range(docs[-1]) == (13, 13)
    assert issues[0]["severity"] == "warning" and "no bookmarks" in issues[0]["issue"]


def test_unsplittable_packet_is_kept_whole_with_a_warning():
    data = build_packet(CANDS[:8], outline=False, names_on_pages=False)
    issues: list[dict] = []
    docs = RP.split_bundle(ResumeDoc(PACKET.format(5), data), ProcessingConfig(), issues)
    assert len(docs) == 1 and docs[0].content == data and docs[0].file_name == PACKET.format(5)
    assert issues[0]["severity"] == "warning" and "kept as one file" in issues[0]["issue"]


def test_non_packets_are_left_alone(packet):
    def resume_with_bookmarks() -> bytes:
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=A4, invariant=1)
        for k, section in enumerate(["Education", "Projects", "Experience"]):
            c.bookmarkPage(f"s{k}")
            c.addOutlineEntry(section, f"s{k}", level=0)
            _resume_page(c, "Asha Menon", k + 1)
            c.showPage()
        c.save()
        return buf.getvalue()

    for data in (resume_with_bookmarks(), b"%PDF-1.4 broken", b"not a pdf"):
        assert RP.split_bundle(ResumeDoc("resume.pdf", data), ProcessingConfig()) is None
    assert RP.split_bundle(ResumeDoc("x.docx", packet), ProcessingConfig()) is None
    assert RP.split_bundle(ResumeDoc(PACKET.format(1), packet), ProcessingConfig(split_bundles=False)) is None
    out, _ = RP.expand_uploads([ResumeDoc(PACKET.format(1), packet)], ProcessingConfig(split_bundles=False))
    assert [d.file_name for d in out] == [PACKET.format(1)]


def test_split_resumes_parse_and_match_by_id(packet):
    docs = RP.split_bundle(ResumeDoc(PACKET.format(3), packet), ProcessingConfig())
    parsed = RP.parse_resumes(docs, max_workers=4, id_pattern=CFG.processing.candidate_id_pattern)
    first = parsed[docs[0].sha256]
    assert first.bundle_source == docs[0].bundle_source and first.parse_status == "ok"
    assert first.candidate_id_guess == CANDS[0][1] and first.name_guess == CANDS[0][0]
    assert "Table of Contents" not in first.text
    ta = pd.DataFrame({S.CANDIDATE_ID: pd.Series([cid for _, cid in CANDS] + ["C-1999999"], dtype="str"),
                       S.NAME: pd.Series([n for n, _ in CANDS] + ["No Resume"], dtype="str")})
    matches = matching.match_resumes(ta, parsed, CFG).set_index(matching.FILE_NAME)
    att2 = f"{CANDS[6][1]}_{CANDS[6][0].replace(' ', '_')}_att2.pdf"
    primaries = matches.drop(index=att2)
    assert primaries[S.MATCH_METHOD].eq(S.MATCH_ID_FILENAME).all()
    assert matches.loc[att2, S.MATCH_METHOD] == S.MATCH_DUPLICATE
    assert "additional attachment of " + CANDS[6][1] in matches.loc[att2, matching.NOTE]
    base, by_cand, issues = matching.link_candidates(ta, parsed, matches.reset_index(), CFG)
    assert base[S.HAS_RESUME].sum() == len(CANDS) and not base.set_index(S.CANDIDATE_ID).loc["C-1999999", S.HAS_RESUME]
    assert by_cand[CANDS[6][1]].file_name.endswith(f"{CANDS[6][0].replace(' ', '_')}.pdf")
    assert any(i["issue"].startswith("Additional attachment") for i in issues)


def test_duplicate_application_keeps_both_copies():
    twins = [("Asha Menon", "C-1000042"), ("Asha Menon", "C-1000999"), ("Ravi Kumar", "C-1000056")]
    docs = RP.split_bundle(ResumeDoc(PACKET.format(6), build_packet(twins)), ProcessingConfig())
    assert [d.file_name for d in docs] == ["C-1000042_Asha_Menon.pdf", "C-1000999_Asha_Menon.pdf",
                                           "C-1000056_Ravi_Kumar.pdf"]
    assert docs[0].sha256 != docs[1].sha256                              # same pages, own file per application
    parsed = RP.parse_resumes(docs)
    assert parsed[docs[0].sha256].text == parsed[docs[1].sha256].text
    ta = pd.DataFrame({S.CANDIDATE_ID: pd.Series([c for _, c in twins], dtype="str"),
                       S.NAME: pd.Series([n for n, _ in twins], dtype="str"),
                       S.EMAIL: pd.Series(["asha.menon@example.com", "asha42@xyiict.example", "ravi@example.com"],
                                          dtype="str")})
    matches = matching.match_resumes(ta, parsed, CFG)
    assert sorted(matches[S.CANDIDATE_ID]) == sorted(c for _, c in twins)
    note = matches.set_index(S.CANDIDATE_ID).loc["C-1000999", matching.NOTE]
    assert "possible duplicate application" in note and matching.IDENTITY_CONFLICT not in note


def test_hidden_text_in_a_packet_resume_is_reported_on_that_candidate():
    data = build_packet(CANDS[:5], hidden={2: "Kubernetes Docker Terraform PyTorch"})
    parsed = RP.parse_resumes(RP.split_bundle(ResumeDoc(PACKET.format(7), data), ProcessingConfig()))
    flagged = {p.file_name: p.injection_findings for p in parsed.values() if p.injection_findings}
    assert list(flagged) == [f"{CANDS[2][1]}_{CANDS[2][0].replace(' ', '_')}.pdf"]
    assert all("Kubernetes" not in p.text and "Kubernetes" not in p.skills for p in parsed.values())


def test_large_packet_splits_quickly():
    cands = [person(i) for i in range(100)]
    data = build_packet(cands, pages=lambda i: 2 if i % 6 == 0 else 1, extra_attachment={10, 50})
    start = time.perf_counter()
    docs = RP.split_bundle(ResumeDoc(PACKET.format(8), data), ProcessingConfig())
    elapsed = time.perf_counter() - start
    assert len(docs) == 102 and elapsed < 10.0, elapsed
