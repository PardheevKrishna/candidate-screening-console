"""Tests for screening.resume_parser. All fixtures are generated in tmp_path at test time."""
from __future__ import annotations

import io
import threading
import time
import zipfile
from pathlib import Path

import docx
import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from screening import resume_parser as RP
from screening import schema as S
from screening.config import ProcessingConfig
from screening.models import ParsedResume, ResumeDoc

TXT_RESUME = (
    "ASHA MENON\r\n"
    "asha.menon@Example.com | +91 98765 43210 | linkedin.com/in/Asha-Menon | https://github.com/AshaMenon\r\n"
    "Hyderabad, Telangana\r\n"
    "Motivated computer science undergraduate passionate about reliable backend systems and applied "
    "machine learning for fintech products.\r\n"
    "\r\n"
    "Education: B.Tech in Computer Science and Engineering, XYZ Institute of Technology   2021 - 2025\r\n"
    "CGPA: 8.7/10\r\n"
    "Class XII, ABC Junior College   2021   94%\r\n"
    "Skills: Python, Java, C, SQL, Spring Boot, AWS\r\n"
    "Tech Stack: React | Node.js\r\n"
    "Experience\r\n"
    "Software Engineering Intern\r\n"
    "ABC Technologies Pvt Ltd, Bangalore | Jun 2025 - Aug 2025\r\n"
    "➢ Built REST APIs in Java Spring Boot serving 10k requests per day.\r\n"
    "▪ Reduced query latency by 40% by adding PostgreSQL indexes and caching with\r\n"
    "  Redis for hot paths.\r\n"
    "Teaching Assistant | Department of CSE | May 2024 - Present\r\n"
    "\uf0b7 Mentored 60 juniors in data structures and algorithms.\r\n"
    "PROJECTS\r\n"
    "Smart Attendance System | Python, OpenCV, Flask | github.com/AshaMenon/Smart-Attendance.git\r\n"
    "• Developed face recognition based attendance with 95% accuracy for 500 students.\r\n"
    "• Deployed on AWS EC2 using Docker containers.\r\n"
    "Loan Default Predictor\r\n"
    "o Trained gradient boosting and PyTorch models on 100k loan records; AUC 0.91.\r\n"
    "Certifications & Courses\r\n"
    "– AWS Certified Cloud Practitioner (2024)\r\n"
    "– Oracle Certified Java Programmer\r\n"
    "ACHIEVEMENTS\r\n"
    "* Winner, Smart India Hackathon 2024\r\n"
)


def _pdf_bytes(lines: list[str], *, link: str | None = None, columns: list[list[str]] | None = None) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = 800
    for line in lines:
        c.drawString(50, y, line)
        y -= 16
    if columns:
        for i, col in enumerate(columns):
            yy = y - 20
            for line in col:
                c.drawString(50 + 280 * i, yy, line)
                yy -= 16
    if link:
        c.drawString(50, 60, "GitHub")
        c.linkURL(link, (45, 55, 120, 75), relative=0)
    c.save()
    return buf.getvalue()


def _image_only_pdf() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.rect(100, 400, 300, 200, fill=1)
    c.circle(300, 300, 50, fill=1)
    c.save()
    return buf.getvalue()


PDF_LINES = [
    "Ravi Kumar",
    "ravi.kumar@example.com | 9123456789",
    "EDUCATION",
    "B.E. Electronics and Communication Engineering, PQR College of Engineering, 2020 - 2024",
    "GPA: 3.6/4",
    "TECHNICAL SKILLS",
    "Languages: C, C++, Java",
    "Tools: Git, Docker, Kubernetes",
    "INTERNSHIPS",
    "Embedded Software Trainee, LMN Systems | 06/2023 - 08/2023",
    "• Wrote device drivers in C for ARM microcontrollers.",
    "PROJECTS",
    "IoT Weather Station | Arduino, Python",
    "• Collected sensor data from 20 nodes and visualised it with Grafana.",
]


def _docx_bytes(*, header: str | None = None) -> bytes:
    d = docx.Document()
    if header:
        d.sections[0].header.paragraphs[0].text = header
    d.add_paragraph("Priya Sharma")
    d.add_paragraph("priya.sharma@example.com | 9988776655")
    d.add_heading("Technical Skills", level=1)
    for s in ("Python", "C", "Machine Learning", "SQL"):
        d.add_paragraph(s, style="List Bullet")
    d.add_heading("Education", level=1)
    table = d.add_table(rows=2, cols=3)
    for i, v in enumerate(("Degree", "Institute", "Score")):
        table.cell(0, i).text = v
    for i, v in enumerate(("MCA", "National Institute of Technology, Trichy", "82%")):
        table.cell(1, i).text = v
    d.add_heading("Projects", level=1)
    d.add_paragraph("Fraud Detection Engine")
    d.add_paragraph("Built an XGBoost and scikit-learn model that flags risky card transactions.", style="List Bullet")
    d.add_paragraph("Served predictions through a FastAPI service on Azure.", style="List Bullet")
    d.add_heading("Work Experience", level=1)
    d.add_paragraph("Data Science Intern at QRS Analytics (Jan 2026 - Present)")
    d.add_paragraph("Automated feature pipelines in Pandas.", style="List Bullet")
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _empty_docx_bytes() -> bytes:
    buf = io.BytesIO()
    docx.Document().save(buf)
    return buf.getvalue()


def _zip_bytes(entries: dict[str, bytes], compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


@pytest.fixture(scope="module")
def parsed_txt() -> ParsedResume:
    return RP.parse_resume_text(TXT_RESUME, "CAND-00012_Asha_Menon.txt", "h1")


# ---------------------------------------------------------------------------
# parse_resume_text: sections, bullets, entities
# ---------------------------------------------------------------------------
def test_sections_with_inline_and_mixed_case_headings(parsed_txt):
    sec = parsed_txt.sections
    assert set(sec) <= set(S.SECTION_KEYS)
    assert list(sec) == [k for k in S.SECTION_KEYS if k in sec]
    assert sec["education"].startswith("B.Tech in Computer Science")
    assert "Python, Java, C, SQL" in sec["skills"]
    assert "Tech Stack: React | Node.js" in sec["skills"]
    assert sec["experience"].startswith("Software Engineering Intern")
    assert sec["projects"].startswith("Smart Attendance System")
    assert "AWS Certified Cloud Practitioner (2024)" in sec["certifications"]
    assert "Smart India Hackathon" in sec["achievements"]
    assert sec["summary"].startswith("Motivated computer science")
    assert "asha.menon@Example.com" in sec["other"]   # contact block before the first heading
    assert "\r" not in parsed_txt.text
    assert parsed_txt.parse_status == "ok" and parsed_txt.parse_error is None
    assert parsed_txt.word_count == len(parsed_txt.text.split())


def test_bullets_strip_unicode_glyphs_and_merge_wrapped_lines(parsed_txt):
    b = parsed_txt.bullets
    assert "Built REST APIs in Java Spring Boot serving 10k requests per day." in b
    assert ("Reduced query latency by 40% by adding PostgreSQL indexes and caching with Redis for hot paths."
            in b)
    assert "Mentored 60 juniors in data structures and algorithms." in b   # PUA (Symbol font) bullet
    assert "Trained gradient boosting and PyTorch models on 100k loan records; AUC 0.91." in b  # "o " bullet
    assert "Winner, Smart India Hackathon 2024" in b
    assert not any(x[:1] in "•➢▪–*o\uf0b7" and x[1:2] == " " for x in b)


def test_entities(parsed_txt):
    assert parsed_txt.emails == ["asha.menon@example.com"]
    assert parsed_txt.phones == ["9876543210"]
    assert parsed_txt.github_urls == ["github.com/ashamenon", "github.com/ashamenon/smart-attendance"]
    assert parsed_txt.linkedin_urls == ["linkedin.com/in/asha-menon"]
    assert any("github.com/AshaMenon" in u for u in parsed_txt.urls)
    assert parsed_txt.name_guess == "Asha Menon"
    assert parsed_txt.candidate_id_guess == "CAND-00012"


@pytest.mark.parametrize("raw, expected", [
    ("Phone: 098765 43210", ["9876543210"]),
    ("Mob (+91) 98765-43210 / 91234 56789", ["9876543210", "9123456789"]),
    ("+919876543210", ["9876543210"]),
    ("2019 - 2023, Roll 1601202533, pin 500032", []),
    ("+1 (555) 123-4567", ["5551234567"]),
])
def test_phone_normalisation(raw, expected):
    assert RP.parse_resume_text(f"Somebody Name\n{raw}\nfiller text to reach the minimum length", "x.txt").phones == expected


@pytest.mark.parametrize("raw, expected", [
    ("https://www.GitHub.com/User-1/My.Repo/tree/main", ["github.com/user-1/my.repo"]),
    ("github.com/someone.", ["github.com/someone"]),
    ("see github.com/settings/profile and gist.github.com/abc/123", []),
    ("http://github.com/A/B.git, github.com/a/b", ["github.com/a/b"]),
])
def test_github_normalisation(raw, expected):
    pr = RP.parse_resume_text(f"Somebody Name\n{raw}\nfiller text to reach the minimum length", "x.txt")
    assert pr.github_urls == expected


def test_skills_and_evidence(parsed_txt):
    sk = set(parsed_txt.skills)
    assert {"Python", "Java", "C", "SQL", "Spring Boot", "AWS", "React", "Node.js", "OpenCV", "PyTorch",
            "Docker", "PostgreSQL", "Redis"} <= sk
    assert "JavaScript" not in sk
    assert parsed_txt.skills == sorted(parsed_txt.skills, key=str.lower)
    assert parsed_txt.skill_evidence["C"] == ["skills"]
    assert "projects" in parsed_txt.skill_evidence["OpenCV"]
    assert set(parsed_txt.skill_evidence) == sk


def test_projects(parsed_txt):
    projects = parsed_txt.projects
    assert parsed_txt.project_count == len(projects) == 2
    p1, p2 = projects
    assert p1["title"] == "Smart Attendance System"
    assert p1["github"] == "github.com/ashamenon/smart-attendance"
    assert {"Python", "OpenCV", "Flask", "AWS", "Docker"} <= set(p1["tech"])
    assert len(p1["bullets"]) == 2 and p1["bullets"][0].startswith("Developed face recognition")
    assert p2["title"] == "Loan Default Predictor" and p2["github"] is None
    assert p2["tech"] == ["PyTorch"]
    assert set(p1) == {"title", "text", "bullets", "tech", "github"}


def test_experience_entries_and_month_math(parsed_txt):
    exps = parsed_txt.experiences
    assert [e["title"] for e in exps] == ["Software Engineering Intern", "Teaching Assistant"]
    assert exps[0]["org"] == "ABC Technologies Pvt Ltd"
    assert exps[0]["months"] == 3.0 and exps[0]["is_internship"] is True
    assert exps[1]["months"] == 30.0 and exps[1]["is_internship"] is False   # May 2024 -> 2026-11-01
    assert parsed_txt.internship_count == 1
    assert parsed_txt.total_experience_months == 30.0   # overlapping ranges counted once


@pytest.mark.parametrize("dates, months", [
    ("Jun 2025 - Aug 2025", 3.0),
    ("06/2025 - 08/2025", 3.0),
    ("June 2025 to August 2025", 3.0),
    ("May 2024 – Present", 30.0),
    ("Jan 2026 - Current", 10.0),
    ("Dec 2024 - Feb 2025", 3.0),
    ("Jun'25 - Aug'25", 3.0),
    ("Jun - Aug 2025", 3.0),
    ("2023 - 2024", 12.0),
    ("2025-01 to 2025-06", 6.0),
])
def test_date_range_months(dates, months):
    text = f"Somebody Name\nEXPERIENCE\nBackend Intern, Foo Labs | {dates}\n• Built internal tools for the ops team."
    pr = RP.parse_resume_text(text, "x.txt")
    assert pr.experiences[0]["months"] == months
    assert pr.experiences[0]["is_internship"] is True


def test_duration_fallback_and_internship_heading():
    text = ("Somebody Name\nINTERNSHIPS\nData Analyst, Foo Corp (2 months)\n• Cleaned data for reports.\n"
            "Web Developer, Bar Inc | 8 weeks\n• Built landing pages for clients.")
    pr = RP.parse_resume_text(text, "x.txt")
    assert [e["months"] for e in pr.experiences] == [2.0, 1.8]
    assert all(e["is_internship"] for e in pr.experiences)   # section heading says internships
    assert pr.internship_count == 2
    assert pr.total_experience_months == pytest.approx(3.8)


def test_education(parsed_txt):
    edu = parsed_txt.education
    assert edu == {"degree": "B.Tech", "branch": "Computer Science and Engineering",
                   "college": "XYZ Institute of Technology", "cgpa": 8.7, "grad_year": 2025}


@pytest.mark.parametrize("line, cgpa", [
    ("CGPA: 8.7/10", 8.7),
    ("GPA 3.6/4", 9.0),
    ("GPA: 3.6 / 4.0", 9.0),
    ("Aggregate 82%", 8.2),
    ("Percentage: 78.5", 7.85),
    ("8.25 CGPA", 8.25),
    ("CPI - 9.1", 9.1),
])
def test_cgpa_conversions(line, cgpa):
    text = f"Somebody Name\nEDUCATION\nB.Tech, Information Technology, ABC University, 2022 - 2026\n{line}"
    pr = RP.parse_resume_text(text, "x.txt")
    assert pr.education["cgpa"] == cgpa
    assert pr.education["branch"] == "Information Technology"
    assert pr.education["grad_year"] == 2026


def test_cgpa_ignores_school_percentages():
    text = ("Somebody Name\nEDUCATION\nB.Sc Statistics, LMN College 2021-2024\n"
            "Class XII (CBSE) 2021 - 95%\nClass X 2019 - 97%")
    edu = RP.parse_resume_text(text, "x.txt").education
    assert edu["degree"] == "B.Sc" and edu["branch"] == "Statistics" and edu["cgpa"] is None


def test_certifications_and_fallback(parsed_txt):
    assert parsed_txt.certifications == ["AWS Certified Cloud Practitioner (2024)", "Oracle Certified Java Programmer"]
    pr = RP.parse_resume_text("Somebody Name\nSKILLS\nPython\nAWS Certified Developer - Associate\n"
                              "Completed Google certification in data analytics", "x.txt")
    assert pr.certifications == ["AWS Certified Developer - Associate", "Completed Google certification in data analytics"]


def test_name_and_id_guess_fallbacks():
    text = "RESUME\nPhone 9876543210\nSKILLS\nPython, SQL and some other things here"
    pr = RP.parse_resume_text(text, "cand_00077-meera_iyer_resume.pdf")
    assert pr.name_guess == "Meera Iyer"
    assert pr.candidate_id_guess == "CAND_00077"
    pr = RP.parse_resume_text("John Doe\nCandidate ID: EMP-12345\nSkills: Python and Java", "resume_2024_final.pdf")
    assert pr.candidate_id_guess == "EMP-12345"
    pr = RP.parse_resume_text("John Doe\nWinner of SIH-2024 hackathon, plus Python and Java", "resume_2024_final.pdf")
    assert pr.candidate_id_guess is None
    assert RP.parse_resume_text("John Doe\nCandidate ID: EMP-12345\nmore text here ok", "x.txt",
                                id_pattern="").candidate_id_guess is None
    assert RP.parse_resume_text("John Doe\nPython, Java and SQL work here", "CV_2024.pdf").candidate_id_guess is None
    pr = RP.parse_resume_text("John Doe\nCandidate ID: EMP-12345\nmore text", "x.txt", id_pattern=r"\d*")
    assert pr.candidate_id_guess == "12345"          # empty matches are never returned as an ID
    pr = RP.parse_resume_text("John Doe\nCandidate ID: EMP-12345\nmore text", "x.txt", id_pattern="([unclosed")
    assert pr.candidate_id_guess is None and pr.parse_status == "ok"   # invalid user regex is ignored


def test_cgpa_prefers_education_section_over_other_mentions():
    text = ("Somebody Name\nSUMMARY\nTutored students to raise their GPA 3.0/4 on average.\n"
            "EDUCATION\nB.Tech CSE, ABC University, 2025 - 76%\n")
    assert RP.parse_resume_text(text, "x.txt").education["cgpa"] == 7.6


def test_no_headings_goes_to_other():
    pr = RP.parse_resume_text("Just a paragraph about my Python and SQL work without any headings at all.", "x.txt")
    assert list(pr.sections) == ["other"]
    assert {"Python", "SQL"} <= set(pr.skills)
    assert any("No section headings" in w for w in pr.warnings)


def test_spaced_and_numbered_headings_and_glued_heading():
    text = ("Somebody Name\nE D U C A T I O N\nB.Tech CSE, ABC University 2025\n2. PROJECTS:\nChatbot | Python\n"
            "• Built an intent classifier for support tickets.\nSKILLS Python, Java and SQL")
    pr = RP.parse_resume_text(text, "x.txt")
    assert "B.Tech CSE" in pr.sections["education"]
    assert pr.projects[0]["title"] == "Chatbot"
    assert pr.sections["skills"] == "Python, Java and SQL"


def test_tech_stack_line_inside_project_does_not_switch_section():
    text = ("Somebody Name\nPROJECTS\nChatbot\nTech Stack:\nReact, Node.js\n• Built a chat UI with websockets.\n"
            "Skills: React, Redux\nE-Commerce Site - MERN\n• Implemented cart and checkout flows.")
    pr = RP.parse_resume_text(text, "x.txt")
    assert "skills" not in pr.sections
    assert [p["title"] for p in pr.projects] == ["Chatbot", "E-Commerce Site"]


def test_one_line_projects_and_bullet_only_projects():
    pr = RP.parse_resume_text("Somebody Name\nPROJECTS\nChatbot | Python, NLP\nAttendance System | OpenCV\n", "x.txt")
    assert [p["title"] for p in pr.projects] == ["Chatbot", "Attendance System"]
    pr = RP.parse_resume_text(
        "Somebody Name\nPROJECTS\n• Chatbot (Python, NLP): Built a support chatbot with intent detection.\n"
        "• Attendance System - face recognition attendance using OpenCV and Flask.\n", "x.txt")
    assert [p["title"] for p in pr.projects] == ["Chatbot", "Attendance System"]


def test_pathological_long_tokens_parse_quickly():
    start = time.perf_counter()
    for blob in ("x" * 200_000 + "@" + "y" * 100_000, "a-" * 150_000, "9" * 300_000):
        assert RP.parse_resume_text("Somebody Name\n" + blob, "x.txt").parse_status == "ok"
    assert time.perf_counter() - start < 5.0


@pytest.mark.parametrize("text", ["", "   \n\t  ", "Too short to parse"])
def test_empty_text(text):
    pr = RP.parse_resume_text(text, "CAND-00009_Some_One.txt", "h")
    assert pr.parse_status == "empty"
    assert pr.parse_error
    assert pr.sections == {} and pr.skills == []
    assert pr.name_guess == "Some One" and pr.candidate_id_guess == "CAND-00009"


def test_parse_resume_text_never_raises(monkeypatch):
    monkeypatch.setattr(RP, "_split_blocks", lambda lines: 1 / 0)
    pr = RP.parse_resume_text(TXT_RESUME, "CAND-00012_Asha_Menon.txt", "h")
    assert pr.parse_status == "error" and "ZeroDivisionError" in pr.parse_error
    assert pr.candidate_id_guess == "CAND-00012"


# ---------------------------------------------------------------------------
# extract_text / parse_resume on real file formats
# ---------------------------------------------------------------------------
def test_pdf_with_sections_bullets_columns_and_link(tmp_path):
    path = tmp_path / "Ravi_Kumar.pdf"
    path.write_bytes(_pdf_bytes(PDF_LINES, link="https://github.com/RaviK/Weather-Station",
                                columns=[["ACHIEVEMENTS", "• Hackathon finalist 2023"],
                                         ["CERTIFICATIONS", "• Azure AZ-900 certified"]]))
    doc = ResumeDoc(path.name, path.read_bytes(), "folder")
    text, warnings = RP.extract_text(doc)
    assert "TECHNICAL SKILLS" in text and warnings == []
    pr = RP.parse_resume(doc)
    assert pr.parse_status == "ok"
    assert pr.file_hash == doc.sha256
    assert {"education", "skills", "experience", "projects"} <= set(pr.sections)
    assert {"C", "C++", "Java", "Git", "Docker", "Kubernetes", "Arduino", "Python", "Grafana"} <= set(pr.skills)
    assert "JavaScript" not in pr.skills
    assert pr.education["degree"] == "B.E."
    assert pr.education["branch"] == "Electronics and Communication Engineering"
    assert pr.education["college"] == "PQR College of Engineering"
    assert pr.education["cgpa"] == 9.0 and pr.education["grad_year"] == 2024
    assert pr.experiences[0]["months"] == 3.0 and pr.experiences[0]["is_internship"]
    assert pr.projects[0]["title"] == "IoT Weather Station"
    assert "Wrote device drivers in C for ARM microcontrollers." in pr.bullets
    assert "github.com/ravik/weather-station" in pr.github_urls   # from the link annotation
    assert pr.name_guess == "Ravi Kumar" and pr.phones == ["9123456789"]
    assert "Hackathon finalist 2023" in pr.bullets


def test_image_only_pdf_is_empty_with_ocr_hint():
    pr = RP.parse_resume(ResumeDoc("scan.pdf", _image_only_pdf()))
    assert pr.parse_status == "empty"
    assert "OCR" in pr.parse_error and any("OCR" in w for w in pr.warnings)


@pytest.mark.parametrize("name, content", [
    ("garbage.pdf", b"%PDF-1.4\n\x00\x01 this is not really a pdf \xff\xfe"),
    ("random.pdf", bytes(range(256)) * 4),
    ("broken.docx", b"PK\x03\x04 definitely not a docx"),
    ("photo.png", b"\x89PNG\r\n\x1a\n"),
])
def test_corrupted_or_unsupported_files_report_error(name, content):
    pr = RP.parse_resume(ResumeDoc(name, content))
    assert pr.parse_status == "error" and pr.parse_error
    assert pr.sections == {}
    text, warnings = RP.extract_text(ResumeDoc(name, content))
    assert text == "" and warnings


@pytest.mark.parametrize("content", [b"", _empty_docx_bytes()])
def test_empty_docx(content):
    pr = RP.parse_resume(ResumeDoc("CAND-00001_Empty.docx", content))
    assert pr.parse_status == "empty"
    assert pr.candidate_id_guess == "CAND-00001"


def test_docx_with_headings_list_bullets_table_and_header():
    doc = ResumeDoc("priya.docx", _docx_bytes(header="Candidate ID: APP-77001"))
    text, warnings = RP.extract_text(doc)
    assert warnings == []
    assert "• Python" in text                       # List Bullet paragraphs keep a glyph
    assert "MCA | National Institute of Technology, Trichy | 82%" in text   # table row
    assert text.index("APP-77001") < text.index("Priya Sharma")             # header first
    pr = RP.parse_resume(doc)
    assert pr.parse_status == "ok"
    assert pr.candidate_id_guess == "APP-77001"
    assert pr.name_guess == "Priya Sharma"
    assert {"Python", "C", "Machine Learning", "SQL", "XGBoost", "scikit-learn", "FastAPI", "Azure",
            "Pandas"} <= set(pr.skills)
    assert pr.skill_evidence["C"] == ["skills"]
    assert pr.education["degree"] == "MCA" and pr.education["branch"] == "Computer Applications"
    assert pr.education["college"] == "National Institute of Technology"
    assert pr.education["cgpa"] == 8.2
    assert pr.projects[0]["title"] == "Fraud Detection Engine" and len(pr.projects[0]["bullets"]) == 2
    assert pr.experiences[0]["months"] == 10.0 and pr.experiences[0]["is_internship"]
    assert pr.experiences[0]["title"] == "Data Science Intern" and pr.experiences[0]["org"] == "QRS Analytics"


def test_txt_encodings():
    body = "José Álvarez\nSKILLS\nPython, SQL, Docker and Kubernetes for deployment work"
    for enc in ("utf-8", "utf-8-sig", "cp1252", "utf-16"):
        pr = RP.parse_resume(ResumeDoc("cv.txt", body.encode(enc)))
        assert pr.parse_status == "ok", enc
        assert pr.name_guess == "José Álvarez", enc
        assert {"Python", "SQL", "Docker", "Kubernetes"} <= set(pr.skills), enc


def test_renamed_docx_with_pdf_extension_is_sniffed():
    pr = RP.parse_resume(ResumeDoc("priya.pdf", _docx_bytes()))
    assert pr.parse_status == "ok" and pr.name_guess == "Priya Sharma"


# ---------------------------------------------------------------------------
# Uploads, ZIPs, folders
# ---------------------------------------------------------------------------
def test_expand_uploads_zip_and_top_level_filtering():
    txt = TXT_RESUME.encode()
    inner = _zip_bytes({"inner.txt": b"Inner Person\nSkills: Python"})
    archive = _zip_bytes({
        "batch1/": b"",
        "batch1/CAND-00012_Asha_Menon.txt": txt,
        "batch1/sub/deeper/Ravi_Kumar.pdf": _pdf_bytes(PDF_LINES),
        "batch1/sub/copy_of_asha.txt": txt,                 # duplicate content, different name
        "__MACOSX/batch1/._CAND-00012_Asha_Menon.txt": b"junk",
        "batch1/.hidden.txt": b"hidden",
        "batch1/~$draft.docx": b"lock",
        "batch1/photo.png": b"\x89PNG\r\n\x1a\n",
        "../../escape/evil.txt": b"Path Traversal\nSkills: Python, SQL and more text here",
        "nested.zip": inner,
    })
    docs = [
        ResumeDoc("resumes.zip", archive),
        ResumeDoc("C:\\Users\\hr\\Desktop\\Priya.docx", _docx_bytes()),
        ResumeDoc("notes.png", b"\x89PNG"),
        ResumeDoc("old.doc", b"\xd0\xcf\x11\xe0"),
        ResumeDoc("broken.zip", b"PK\x03\x04 not a zip"),
    ]
    out, issues = RP.expand_uploads(docs, ProcessingConfig())
    names = [d.file_name for d in out]
    assert names == ["CAND-00012_Asha_Menon.txt", "Ravi_Kumar.pdf", "copy_of_asha.txt", "evil.txt", "Priya.docx"]
    assert all("/" not in n and "\\" not in n for n in names)
    assert out[0].source == "zip:resumes.zip" and out[-1].source == "upload"
    by_file = {i["file_name"]: i for i in issues}
    assert by_file["photo.png"]["severity"] == "info"
    assert "Nested ZIP" in by_file["nested.zip"]["issue"]
    assert by_file["notes.png"]["severity"] == "warning"
    assert ".docx or PDF" in by_file["old.doc"]["issue"]
    assert by_file["broken.zip"]["severity"] == "error"
    assert not any("MACOSX" in str(i["file_name"]) or "hidden" in str(i["file_name"]) for i in issues)
    for i in issues:
        assert set(i) == {"severity", "stage", "issue", "candidate_id", "file_name"} and i["stage"] == "upload"


def test_zip_limits_and_bomb_guard():
    big = b"A" * 5000
    archive = _zip_bytes({"a.txt": b"Aaa Person\nSkills: Python", "bomb.txt": b"\0" * 3_000_000,
                          "big.txt": bytes(range(256)) * 20, "c.txt": b"Ccc Person\nSkills: SQL"})
    out, issues = RP.expand_uploads([ResumeDoc("x.zip", archive)], ProcessingConfig(max_file_mb=4.0))
    assert [d.file_name for d in out] == ["a.txt", "big.txt", "c.txt"]
    assert any("zip bomb" in i["issue"] and i["file_name"] == "bomb.txt" for i in issues)

    out, issues = RP.expand_uploads([ResumeDoc("x.zip", archive)], ProcessingConfig(max_file_mb=0.004))
    assert [d.file_name for d in out] == ["a.txt", "c.txt"]
    assert any(i["file_name"] == "big.txt" and "larger than" in i["issue"] for i in issues)

    out, issues = RP.expand_uploads([ResumeDoc("x.zip", archive)], ProcessingConfig(max_zip_members=1))
    assert [d.file_name for d in out] == ["a.txt"]
    assert any("more than 1" in i["issue"] for i in issues)

    out, issues = RP.expand_uploads([ResumeDoc("x.zip", archive)],
                                    ProcessingConfig(max_zip_total_mb=0.004))
    assert [d.file_name for d in out] == ["a.txt"]
    assert any("exceeds" in i["issue"] for i in issues)

    out, issues = RP.expand_uploads([ResumeDoc("big.txt", big)], ProcessingConfig(max_file_mb=0.001))
    assert out == [] and "larger than" in issues[0]["issue"]


def test_load_resume_folder_recursive_sorted(tmp_path):
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "deep").mkdir(parents=True)
    (tmp_path / ".git").mkdir()
    (tmp_path / "b" / "z.txt").write_text("Zed Person\nSkills: Python")
    (tmp_path / "a" / "deep" / "y.pdf").write_bytes(_pdf_bytes(PDF_LINES))
    (tmp_path / "a" / "x.docx").write_bytes(_docx_bytes())
    (tmp_path / "archive.zip").write_bytes(_zip_bytes({"q.txt": b"Q Person\nSkills: SQL"}))
    (tmp_path / "notes.md").write_text("ignore me")
    (tmp_path / ".git" / "config.txt").write_text("ignore me")
    (tmp_path / "~$lock.docx").write_bytes(b"lock")
    docs = RP.load_resume_folder(tmp_path)
    assert [d.file_name for d in docs] == ["y.pdf", "x.docx", "archive.zip", "z.txt"]
    assert all(d.source == "folder" for d in docs)
    assert RP.load_resume_folder(tmp_path / "missing") == []
    assert [d.file_name for d in RP.load_resume_folder(tmp_path / "b" / "z.txt")] == ["z.txt"]
    expanded, _ = RP.expand_uploads(docs, ProcessingConfig())
    assert [d.file_name for d in expanded] == ["y.pdf", "x.docx", "q.txt", "z.txt"]


# ---------------------------------------------------------------------------
# parse_resumes: dedup, progress, ordering, performance
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("workers", [1, 4])
def test_parse_resumes_dedup_and_progress(workers):
    txt = TXT_RESUME.encode()
    docs = [
        ResumeDoc("CAND-00012_Asha_Menon.txt", txt),
        ResumeDoc("Ravi_Kumar.pdf", _pdf_bytes(PDF_LINES)),
        ResumeDoc("copy_of_asha.txt", txt),                 # identical bytes
        ResumeDoc("garbage.pdf", b"not a pdf at all"),
        ResumeDoc("empty.docx", b""),
    ]
    calls: list[tuple[int, int, str, int]] = []
    out = RP.parse_resumes(docs, max_workers=workers,
                           progress_cb=lambda d, t, n: calls.append((d, t, n, threading.get_ident())))
    assert list(out) == [docs[0].sha256, docs[1].sha256, docs[3].sha256, docs[4].sha256]
    assert [c[0] for c in calls] == [1, 2, 3, 4] and {c[1] for c in calls} == {4}
    assert {c[3] for c in calls} == {threading.get_ident()}       # callback on the calling thread
    assert sorted(c[2] for c in calls) == sorted(["CAND-00012_Asha_Menon.txt", "Ravi_Kumar.pdf", "garbage.pdf",
                                                  "empty.docx"])
    asha = out[docs[0].sha256]
    assert asha.file_name == "CAND-00012_Asha_Menon.txt" and asha.file_hash == docs[0].sha256
    assert any("copy_of_asha.txt" in w for w in asha.warnings)
    assert [p.parse_status for p in out.values()] == ["ok", "ok", "error", "empty"]
    assert RP.parse_resumes([]) == {}


def test_parse_resumes_survives_worker_failure(monkeypatch):
    def boom(doc, id_pattern=None):
        raise RuntimeError("boom")
    monkeypatch.setattr(RP, "parse_resume", boom)
    out = RP.parse_resumes([ResumeDoc("a.txt", b"x" * 50), ResumeDoc("b.txt", b"y" * 50)], max_workers=2)
    assert [p.parse_status for p in out.values()] == ["error", "error"]


def test_parse_300_txt_resumes_quickly():
    docs = [ResumeDoc(f"CAND-{i:05d}_Person_{i}.txt", (TXT_RESUME + f"\nRef {i}\n").encode()) for i in range(300)]
    start = time.perf_counter()
    out = RP.parse_resumes(docs, max_workers=8)
    elapsed = time.perf_counter() - start
    assert len(out) == 300 and all(p.parse_status == "ok" for p in out.values())
    assert elapsed < 5.0, elapsed
