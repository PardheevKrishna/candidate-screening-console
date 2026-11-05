"""Tests for screening.ingest: reading, header auto-mapping and normalisation."""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import random
import time

import numpy as np
import pandas as pd
import pytest

import screening.schema as S
from screening import ingest
from screening.config import AppConfig

CFG = AppConfig()

# HackerRank for Work CSV export + TA-appended candidate details, in export order.
HR_HEADERS = [
    "Candidate ID", "Test Candidate ID", "Full Name", "Email", "Phone Number", "College", "College Tier",
    "Degree", "Branch", "Year of Passing", "CGPA", "City", "Test Name", "Status", "Invited On", "Invited By",
    "Attempt Start Time", "Attempt End Time", "Time Taken (mins)", "Score", "Max Score", "Percentage Score",
    "Q1 Score (MCQ - CS Fundamentals)", "Q2 Score (Coding - Arrays)", "Q3 Score (Coding - Dynamic Programming)",
    "Q4 Score (Coding - Graphs)", "Q2 Code Similarity %", "Q3 Code Similarity %", "Q4 Code Similarity %",
    "Max Code Similarity %", "Most Similar Candidate", "Plagiarism Status", "Out of Window Events",
    "Out of Window Duration (sec)", "Editor Paste Count", "Secure Mode Exit Count", "IP Address",
    "Integrity Status", "Proctoring Flags", "Test Mode", "Source", "Q1 Answer Hash", "Q2 Code Hash",
    "Q3 Code Hash", "Q4 Code Hash", "Report URL",
]
HR_EXPECTED = {
    "Candidate ID": S.CANDIDATE_ID, "Full Name": S.NAME, "Email": S.EMAIL, "Phone Number": S.PHONE,
    "College": S.COLLEGE, "College Tier": S.COLLEGE_TIER, "Degree": S.DEGREE, "Branch": S.BRANCH,
    "Year of Passing": S.GRAD_YEAR, "CGPA": S.CGPA, "City": S.CITY, "Test Name": S.TEST_NAME,
    "Status": S.TEST_STATUS, "Attempt Start Time": S.TEST_START, "Attempt End Time": S.SUBMISSION_TIME,
    "Time Taken (mins)": S.COMPLETION_MIN, "Score": S.HR_SCORE, "Max Score": S.MAX_SCORE,
    "Percentage Score": S.HR_PCT, "Q1 Score (MCQ - CS Fundamentals)": "q1_score",
    "Q2 Score (Coding - Arrays)": "q2_score", "Q3 Score (Coding - Dynamic Programming)": "q3_score",
    "Q4 Score (Coding - Graphs)": "q4_score", "Q2 Code Similarity %": "q2_similarity",
    "Q3 Code Similarity %": "q3_similarity", "Q4 Code Similarity %": "q4_similarity",
    "Max Code Similarity %": S.MAX_CODE_SIMILARITY, "Most Similar Candidate": S.PLAGIARISM_MATCH,
    "Plagiarism Status": S.PLAGIARISM_FLAG, "Out of Window Events": S.TAB_SWITCHES,
    "Out of Window Duration (sec)": S.OUT_OF_WINDOW_SEC, "Editor Paste Count": S.COPY_PASTE,
    "Secure Mode Exit Count": S.SECURE_MODE_EXITS, "IP Address": S.IP_ADDRESS,
    "Integrity Status": S.INTEGRITY_STATUS, "Proctoring Flags": S.PROCTORING_FLAGS, "Test Mode": S.TEST_MODE,
    "Source": S.SOURCE, "Q1 Answer Hash": "q1_hash", "Q2 Code Hash": "q2_hash", "Q3 Code Hash": "q3_hash",
    "Q4 Code Hash": "q4_hash", "Report URL": S.REPORT_URL,
}

FIRST = ["Asha", "Ravi", "Neha", "Vikram", "Priya", "Arjun", "Meera", "Kiran", "Rohan", "Divya", "Sanjay", "Anita"]
LAST = ["Kapoor", "Kumar", "Singh", "Shah", "Nair", "Iyer", "Das", "Reddy", "Gupta", "Menon", "Joshi", "Patel"]
COLLEGES = [("IIT Bombay", 1, "Mumbai"), ("NIT Trichy", 1, "Tiruchirappalli"), ("VIT Vellore", 2, "Vellore"),
            ("PES University", 2, "Bengaluru"), ("SRM Institute", 3, "Chennai"), ("Anna University", 2, "Chennai")]
SET_A = ("Campus SWE Online Assessment 2026 - Set A", (20, 20, 30, 30))
SET_B = ("Campus SWE Online Assessment 2026 - Set B", (30, 30, 45, 45))
MCQ_KEY_HASH = hashlib.sha1(b"mcq-answer-key").hexdigest()[:12]


def _public_ip(rng: random.Random) -> str:
    return f"{rng.choice([49, 103, 106, 117, 122, 157, 182, 183])}.{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}"


def hackerrank_rows(n: int, seed: int = 7) -> list[list[str]]:
    """Synthetic HackerRank-for-Work export rows (realistic distributions, see HR_HEADERS)."""
    rng = random.Random(seed)
    nprng = np.random.default_rng(seed)
    emails = [f"{FIRST[i % 12].lower()}.{LAST[(i * 7) % 12].lower()}{i}@example.com" for i in range(1, n + 1)]
    rows = []
    for i in range(1, n + 1):
        college, tier, city = rng.choice(COLLEGES)
        test_name, weights = SET_B if rng.random() < 0.10 else SET_A
        max_score = sum(weights)
        status = rng.choices(["Completed", "Expired", "Did Not Attempt"], [0.99, 0.005, 0.005])[0]
        u = rng.random()
        pct = (100.0 if u < 0.04 else rng.uniform(0, 40) if u < 0.39 else rng.uniform(40, 75) if u < 0.74
               else rng.uniform(75, 99.4))
        score = round(pct / 100 * max_score)
        q_scores, left = [], score
        for k, w in enumerate(weights):  # fill questions greedily -> consistent with the total
            take = min(w, left) if k == len(weights) - 1 else min(w, round(score * w / max_score), left)
            q_scores.append(take)
            left -= take
        q_scores[-1] += min(left, weights[-1] - q_scores[-1])
        score = sum(q_scores)
        perfect = score == max_score
        minutes = rng.uniform(50, 85) if perfect else float(min(90.0, max(20.0, nprng.normal(70, 12))))
        day = rng.randint(1, 3)
        start = dt.datetime(2026, 11, day, rng.randint(9, 21), rng.randint(0, 59), rng.randint(0, 59))
        end = start + dt.timedelta(minutes=minutes)
        sims = [rng.uniform(60, 75) if k == 0 and rng.random() < 0.1 else rng.uniform(5, 55) for k in range(3)]
        max_sim = max(sims)
        similar = emails[(i - 1 + rng.randint(1, n - 1)) % n] if max_sim >= 70 else ""  # never self
        ow_events = int(nprng.poisson(0.8))
        attempted = status == "Completed"
        values = [
            f"CAND-{i:05d}", str(113244911 + i), f"{FIRST[i % 12]} {LAST[(i * 7) % 12]}", emails[i - 1],
            f"+91 9{rng.randint(100000000, 999999999)}", college if rng.random() > 0.05 else college.lower(),
            str(tier), rng.choice(["B.Tech", "B.E.", "M.Tech", "MCA"]), rng.choice(["CSE", "IT", "ECE", "EEE"]),
            "2026", f"{min(10.0, max(5.0, nprng.normal(7.8, 0.8))):.2f}", city, test_name, status,
            "2026-10-25", "ta.campus@example.com",
            start.strftime("%Y-%m-%d %H:%M:%S") if attempted else "",
            end.strftime("%Y-%m-%d %H:%M:%S") if attempted else "",
            f"{minutes:.1f}" if attempted else "", str(score) if attempted else "",
            str(max_score), f"{score / max_score * 100:.2f}" if attempted else "",
            *[str(q) if attempted else "" for q in q_scores],
            *[f"{s:.1f}" if attempted else "" for s in sims], f"{max_sim:.1f}" if attempted else "",
            similar if attempted else "",
            "Suspicious" if rng.random() < 0.01 else "Not Suspicious",
            str(ow_events), str(ow_events * rng.randint(5, 40)), str(int(nprng.poisson(0.6))),
            "1" if rng.random() < 0.03 else "0", _public_ip(rng),
            rng.choices(["Clear", "Review", "Suspicious"], [0.95, 0.04, 0.01])[0],
            "Multiple faces detected; Phone detected" if rng.random() < 0.02 else "",
            "Remote" if rng.random() < 0.7 else "On-campus",
            rng.choice(["Campus Drive", "Referral", "Job Portal", "Hackathon"]),
            MCQ_KEY_HASH if q_scores[0] == weights[0] else hashlib.sha1(f"q1-{i}".encode()).hexdigest()[:12],
            *[hashlib.sha1(f"q{k}-{i}".encode()).hexdigest()[:12] for k in (2, 3, 4)],
            f"https://reports.example.com/hackerrank/tests/4242/candidates/{113244911 + i}",
        ]
        rows.append(values)
    return rows


def to_csv_bytes(header: list[str], rows: list[list[str]], *, title_rows: list[list[str]] | None = None,
                 delimiter: str = ",", encoding: str = "utf-8") -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=delimiter, lineterminator="\n")
    writer.writerows(title_rows or [])
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue().encode(encoding)


def frame(data: dict[str, list]) -> pd.DataFrame:
    return pd.DataFrame({k: pd.Series(v, dtype="str") for k, v in data.items()})


def normalize(data: dict[str, list]) -> tuple[pd.DataFrame, object]:
    raw = frame(data)
    return ingest.normalize_ta_feed(raw, ingest.suggest_column_mapping(list(raw.columns)), CFG)


def flags_of(df: pd.DataFrame, cid: str) -> set[str]:
    value = df.loc[df[S.CANDIDATE_ID] == cid, S.DATA_FLAGS].iloc[0]
    return {f for f in value.split(S.SEP_NOTES) if f}


# ---------------------------------------------------------------------------
# Header auto-mapping
# ---------------------------------------------------------------------------
def test_hackerrank_headers_map_exactly():
    mapping = ingest.suggest_column_mapping(HR_HEADERS)
    assert mapping == HR_EXPECTED
    assert [h for h in HR_HEADERS if h not in mapping] == ["Test Candidate ID", "Invited On", "Invited By"]


def test_test_candidate_id_never_steals_candidate_id():
    cols = ["Test Candidate ID", "Score", "Candidate ID", "Percentage Score", "Status", "Out of Window Events",
            "Editor Paste Count", "CGPA"]
    mapping = ingest.suggest_column_mapping(cols)
    assert mapping["Candidate ID"] == S.CANDIDATE_ID
    assert "Test Candidate ID" not in mapping
    assert mapping["Percentage Score"] == S.HR_PCT and mapping["CGPA"] == S.CGPA
    assert mapping["Status"] == S.TEST_STATUS
    assert mapping["Out of Window Events"] == S.TAB_SWITCHES
    assert mapping["Editor Paste Count"] == S.COPY_PASTE


def test_alternative_and_fuzzy_headers():
    cols = ["Sr No", "Candidate Name", "Email ID", "Mobile No", "College Name", "Tier", "Qualification",
            "Specialization", "Batch", "CGPA", "Location", "HackerRank Score", "Total Marks", "Test Start Time",
            "Submitted At", "Time Taken", "IP", "Mode", "Tab Switches", "Copy Paste Events", "Plagiarism Flag",
            "Resume File", "Candidate Emial", "Phone Numbr"]
    mapping = ingest.suggest_column_mapping(cols)
    assert mapping["Candidate Name"] == S.NAME
    assert mapping["Email ID"] == S.EMAIL and mapping["Mobile No"] == S.PHONE
    assert mapping["Batch"] == S.GRAD_YEAR and mapping["Total Marks"] == S.MAX_SCORE
    assert mapping["Time Taken"] == S.COMPLETION_MIN and mapping["Submitted At"] == S.SUBMISSION_TIME
    assert mapping["Resume File"] == S.RESUME_FILE
    # typos only reach canonicals that are still free: email/phone are taken by exact aliases
    assert "Candidate Emial" not in mapping and "Phone Numbr" not in mapping
    assert "Sr No" not in mapping
    typo = ingest.suggest_column_mapping(["Cand ID", "Candidate Emial", "Phone Numbr", "Scoree"])
    assert typo == {"Cand ID": S.CANDIDATE_ID, "Candidate Emial": S.EMAIL, "Phone Numbr": S.PHONE,
                    "Scoree": S.HR_SCORE}


def test_question_headers_never_fuzzy_into_whole_test_fields():
    mapping = ingest.suggest_column_mapping(["Q2 Code Similarity %", "Q2 Code Similarity (copy)", "Question 3 Marks",
                                             "Q10 Submission Fingerprint", "Q01 Score"])
    assert mapping == {"Q2 Code Similarity %": "q2_similarity", "Question 3 Marks": "q3_score",
                       "Q10 Submission Fingerprint": "q10_hash", "Q01 Score": "q1_score"}


def test_mapping_is_one_to_one_and_exact_beats_fuzzy():
    rng = random.Random(11)
    aliases = [a for v in S.TA_FIELD_ALIASES.values() for a in v]
    for _ in range(200):
        cols = [rng.choice(aliases).upper() if rng.random() < 0.3 else rng.choice(aliases) for _ in range(rng.randint(1, 25))]
        cols += [c + "x" for c in rng.sample(cols, k=min(3, len(cols)))]  # near-duplicates -> fuzzy candidates
        cols += [f"Q{rng.randint(1, 5)} Score" for _ in range(3)]
        mapping = ingest.suggest_column_mapping(cols)
        assert len(set(mapping.values())) == len(mapping)
        assert set(mapping) <= set(cols)
        for src, canon in mapping.items():  # exact alias beats fuzzy; the earliest exact column wins
            alias_cols = [c for c in cols if ingest._ALIAS_INDEX.get(S.normalize_header(c)) == canon]
            if alias_cols:
                assert src == alias_cols[0]
    assert ingest.suggest_column_mapping(["Email", "E-mail", "Score"]) == {"Email": S.EMAIL, "Score": S.HR_SCORE}


def test_canonical_names_round_trip():
    cols = [S.CANDIDATE_ID, S.HR_PCT, S.TEST_START, "q3_hash", "q2_similarity", S.MAX_CODE_SIMILARITY]
    assert ingest.suggest_column_mapping(cols) == {c: c for c in cols}


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def test_read_csv_with_title_rows_and_repeated_header():
    header = ["Candidate ID", "Email", "Score", "Max Score"]
    rows = [["CAND-1", "a@example.com", "85", "100"], header, ["CAND-2", "b@example.com", "", "100"], ["", "", "", ""]]
    content = to_csv_bytes(header, rows, title_rows=[["HackerRank Report"], ["Generated", "2026-11-04", "v2"], []])
    raw = ingest.read_table("export.csv", content)
    assert list(raw.columns) == header
    assert raw["Candidate ID"].tolist() == ["CAND-1", "CAND-2"]
    assert pd.isna(raw.loc[1, "Score"])
    assert all(str(t) == "str" for t in raw.dtypes)


@pytest.mark.parametrize("delimiter,encoding", [(";", "utf-8-sig"), ("\t", "utf-16"), ("|", "cp1252"), (",", "latin-1")])
def test_read_delimiters_and_encodings(delimiter, encoding):
    header = ["Candidate ID", "Full Name", "Score", "Proctoring Flags"]
    rows = [["CAND-1", "José Müller", "85", "Phone detected; Multiple faces detected"], ["CAND-2", "Asha Menon", "90", ""]]
    raw = ingest.read_table("feed.txt", to_csv_bytes(header, rows, delimiter=delimiter, encoding=encoding))
    assert list(raw.columns) == header
    assert raw.loc[0, "Full Name"] == "José Müller"
    assert raw.loc[0, "Proctoring Flags"] == "Phone detected; Multiple faces detected"


def _xlsx(sheets: list[tuple[str, list[list]]]) -> bytes:
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


def test_read_excel_with_two_title_rows_and_typed_cells():
    rows = [
        ["Campus Hiring 2026 - HackerRank for Work export"],
        ["Test: Campus SWE Online Assessment 2026", None, None, None, "Exported 2026-11-04"],
        ["Candidate ID", "Full Name", "Phone Number", "CGPA", "Score", "Attempt Start Time", "Time Taken"],
        ["CAND-00001", "ASHA MENON", 9876543210, 8.7, 85.0, dt.datetime(2026, 11, 1, 9, 15), dt.time(1, 12, 30)],
        ["CAND-00002", "ravi kumar", 9876543211, 78.5, 100, dt.datetime(2026, 11, 2, 14, 0), dt.time(0, 55, 0)],
    ]
    content = _xlsx([("Instructions", [["Read me first"], ["Do not edit"]]), ("Candidates", rows)])
    raw = ingest.read_table("ta_feed.xlsx", content)
    assert list(raw.columns) == rows[2]
    assert raw.loc[0, "Phone Number"] == "9876543210" and raw.loc[0, "Score"] == "85"
    df, report = ingest.load_ta_feed("ta_feed.xlsx", content, CFG)
    assert df[S.NAME].tolist() == ["Asha Menon", "Ravi Kumar"]
    assert df[S.CGPA].tolist() == [8.7, 7.85]
    assert df[S.COMPLETION_MIN].tolist() == [72.5, 55.0]
    assert df[S.TEST_START].tolist() == [pd.Timestamp("2026-11-01 09:15"), pd.Timestamp("2026-11-02 14:00")]
    assert report.rows_raw == 2 and report.rows_clean == 2


def test_unreadable_files_raise_clean_value_error():
    with pytest.raises(ValueError):
        ingest.read_table("feed.csv", b"")
    with pytest.raises(ValueError, match="xls"):
        ingest.read_table("feed.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100)
    with pytest.raises(ValueError):
        ingest.read_table("feed.xlsx", b"PK\x03\x04garbage")
    # a ".xls" that is really tab-separated text (common web export) still works
    raw = ingest.read_table("feed.xls", to_csv_bytes(["Candidate ID", "Email", "Score"], [["C1", "a@x.com", "5"]],
                                                     delimiter="\t"))
    assert raw.shape == (1, 3)


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------
def test_identity_normalisation():
    df, report = normalize({
        "Candidate ID": [" cand-001 ", "CAND-002", "", "12345.0"],
        "Full Name": ["ASHA MENON", "ravi  kumar", "McDonald O'Brien", "neha singh"],
        "Email ID": [" Asha.Menon@Example.COM ", "not-an-email", "mailto:x.y@example.co.in", ""],
        "Mobile No": ["+91 98765 43210", "98765", "9.87654E+09", "098765-43213 / 9123456789"],
    })
    assert df[S.CANDIDATE_ID].tolist() == ["CAND-001", "CAND-002", "ROW-00003", "12345"]
    assert df[S.NAME].tolist() == ["Asha Menon", "Ravi Kumar", "McDonald O'Brien", "Neha Singh"]
    assert df[S.EMAIL].tolist()[0] == "asha.menon@example.com" and pd.isna(df[S.EMAIL][1])
    assert df[S.EMAIL][2] == "x.y@example.co.in" and df[S.EMAIL_DOMAIN][2] == "example.co.in"
    assert df[S.PHONE].tolist()[0] == "9876543210" and df[S.PHONE][3] == "9876543213"
    assert pd.isna(df[S.PHONE][1]) and pd.isna(df[S.PHONE][2])
    assert flags_of(df, "CAND-002") >= {"invalid_email", "invalid_phone"}
    assert "generated_id" in flags_of(df, "ROW-00003")
    warned = {(i["candidate_id"], i["issue"]) for i in report.issues if i["severity"] == "warning"}
    assert ("ROW-00003", "Blank candidate ID; generated an ID for this row.") in warned
    assert all("example" not in i["issue"].lower() for i in report.issues)  # no PII in issue text


def test_missing_id_column_generates_ids_and_first_last_names():
    df, report = normalize({"First Name": ["asha", "Ravi"], "Last Name": ["menon", "Kumar"], "Score": ["10", "20"]})
    assert df[S.CANDIDATE_ID].tolist() == ["ROW-00001", "ROW-00002"]
    assert df[S.NAME].tolist() == ["Asha Menon", "Ravi Kumar"]
    assert "extra_firstname" in df.columns and "extra_lastname" in df.columns
    assert sum("No candidate ID column" in i["issue"] for i in report.issues) == 1


def test_scores_percentages_and_flags():
    df, report = normalize({
        "Candidate ID": ["A", "B", "C", "D", "E", "F", "G"],
        "Test Name": ["Set A", "Set A", "Set B", "Set B", "Set A", "Set A", "Set A"],
        "Status": ["Completed", "Completed", "Completed", "completed", "Expired", "7", "Did not attempt"],
        "Score": ["85", "120", "", "135", "", "50/80", "AB"],
        "Max Score": ["100", "100", "150", "", "", "", ""],
        "Percentage Score": ["85", "", "90", "90", "", "", ""],
    })
    get = df.set_index(S.CANDIDATE_ID)
    assert get.loc["A", S.HR_PCT] == 85.0
    assert get.loc["B", S.HR_SCORE] == 100.0 and get.loc["B", S.HR_PCT] == 100.0
    assert "score_exceeds_max" in flags_of(df, "B")
    assert get.loc["C", S.HR_SCORE] == 135.0 and get.loc["C", S.HR_PCT] == 90.0  # derived from percentage
    assert "score_from_pct" in flags_of(df, "C")
    assert get.loc["D", S.MAX_SCORE] == 150.0  # same test's usual max
    assert pd.isna(get.loc["E", S.HR_SCORE]) and pd.isna(get.loc["E", S.HR_PCT])
    assert get.loc["E", S.TEST_STATUS] == "Expired" and "score_missing" in flags_of(df, "E")
    assert "max_score_assumed" not in flags_of(df, "E")  # no score -> the default max is irrelevant
    assert get.loc["F", S.HR_SCORE] == 50.0 and get.loc["F", S.MAX_SCORE] == 80.0 and get.loc["F", S.HR_PCT] == 62.5
    assert get.loc["F", S.TEST_STATUS] == "Completed" and get.loc["G", S.TEST_STATUS] == "Did Not Attempt"
    assert df[S.HR_PCT].dropna().between(0, 100).all()
    assert any(i["severity"] == "warning" and i["candidate_id"] == "B" for i in report.issues)


def test_percentage_disagreement_is_info_and_score_kept():
    df, report = normalize({"Candidate ID": ["A", "B"], "Score": ["50", "70"], "Max Score": ["100", "100"],
                            "Percentage Score": ["50", "75"]})
    assert df[S.HR_PCT].tolist() == [50.0, 70.0]
    infos = [i for i in report.issues if "Percentage column differs" in i["issue"]]
    assert len(infos) == 1 and infos[0]["severity"] == "info" and "B" in infos[0]["issue"]


def test_max_score_inferred_from_percentage_without_max_column():
    df, report = normalize({"Candidate ID": ["A", "B"], "Score": ["135", "40"], "Percentage Score": ["90", "40"]})
    assert df[S.MAX_SCORE].tolist() == [150.0, 100.0]
    assert df[S.HR_PCT].tolist() == [90.0, 40.0]
    assert "score_exceeds_max" not in flags_of(df, "A")


def test_cgpa_scales():
    df, _ = normalize({"Candidate ID": list("ABCDEFG"), "CGPA": ["8.7", "78.5", "3.6/4", "8.5/10", "82%", "150", "x"]})
    assert df[S.CGPA].tolist()[:5] == pytest.approx([8.7, 7.85, 9.0, 8.5, 8.2])
    assert pd.isna(df[S.CGPA][5]) and pd.isna(df[S.CGPA][6])
    assert "cgpa_out_of_range" in flags_of(df, "F")
    four_point, report = normalize({"Candidate ID": ["A", "B"], "GPA": ["3.6", "3.2"]})
    assert four_point[S.CGPA].tolist() == pytest.approx([9.0, 8.0])
    assert any("4-point" in i["issue"] for i in report.issues)


def test_education_fields():
    df, _ = normalize({
        "Candidate ID": list("ABCDE"),
        "Tier": ["Tier 1", "II", "3.0", "Premier", ""],
        "Year of Passing": ["2026", "2022-2026", "2025-26", "Batch of 2026.0", "soon"],
    })
    assert df[S.COLLEGE_TIER].tolist()[:3] == ["1", "2", "3"] and df[S.COLLEGE_TIER][3:].isna().all()
    assert df[S.GRAD_YEAR].tolist()[:4] == [2026] * 4 and pd.isna(df[S.GRAD_YEAR][4])
    assert str(df[S.GRAD_YEAR].dtype) == "Int64"
    assert "invalid_graduation_year" in flags_of(df, "E")


def test_college_and_category_canonicalisation():
    df, _ = normalize({
        "Candidate ID": [f"C{i}" for i in range(7)],
        "College": ["IIT Bombay", "IIT Bombay", "iit bombay", "IIT  BOMBAY", "I.I.T. Bombay", "NIT Trichy", "nit-trichy"],
        "City": ["Bengaluru", "bengaluru", "BENGALURU", "Bengaluru ", "Pune", "Pune", "pune"],
    })
    assert df[S.COLLEGE].tolist()[:5] == ["IIT Bombay"] * 5
    assert df[S.COLLEGE].tolist()[5:] == ["NIT Trichy"] * 2  # 1-1 tie: mixed case wins
    assert set(df[S.CITY]) == {"Bengaluru", "Pune"}
    tie = ingest.canonicalize_spellings(pd.Series(["ABC College", "abc college", "Abc College"], dtype="str"))
    assert tie.tolist() == ["ABC College"] * 3  # deterministic: mixed case, then lexicographic


def test_mixed_datetime_formats_and_derived_time_fields():
    df, _ = normalize({
        "Candidate ID": list("ABCDEFG"),
        "Attempt Start Time": ["2026-11-01 09:15:00", "01/11/2026 09:20", "Nov 1, 2026 9:30 AM",
                               "2026-11-01T09:45:00+05:30", "2026-11-01T04:15:00.000Z", "garbage", ""],
        "Attempt End Time": ["2026-11-01 10:27:30", "01/11/2026 10:20", "2026-11-01 10:00", "2026-11-01 10:00",
                             "2026-11-01 05:00", "2026-11-01 10:00", ""],
        "Time Taken": ["01:12:30", "", "30 min", "1h 12m", "72:30", "", ""],
    })
    start = df[S.TEST_START]
    assert start.tolist()[:5] == [pd.Timestamp(x) for x in ("2026-11-01 09:15", "2026-11-01 09:20", "2026-11-01 09:30",
                                                             "2026-11-01 09:45", "2026-11-01 04:15")]
    assert pd.isna(start[5]) and "invalid_test_start" in flags_of(df, "F")
    assert df[S.COMPLETION_MIN].tolist()[:5] == [72.5, 60.0, 30.0, 72.0, 72.5]  # B computed from start/end
    assert df[S.TEST_SLOT].tolist()[:3] == ["2026-11-01 09:00"] * 3 and pd.isna(df[S.TEST_SLOT][6])


def test_month_first_dates_detected_by_tight_window():
    df, _ = normalize({"Candidate ID": list("ABC"), "Start Time": ["11/01/2026 09:00", "11/02/2026 10:00", "11/13/2026 11:00"]})
    assert [t.day for t in df[S.TEST_START]] == [1, 2, 13] and {t.month for t in df[S.TEST_START]} == {11}
    day_first, _ = normalize({"Candidate ID": list("AB"), "Start Time": ["01/11/2026 09:00", "02/11/2026 09:00"]})
    assert [t.month for t in day_first[S.TEST_START]] == [11, 11]


def test_completion_in_seconds_and_hh_mm():
    secs, report = normalize({"Candidate ID": list("AB"), "Time Taken": ["4350", "3600"]})
    assert secs[S.COMPLETION_MIN].tolist() == [72.5, 60.0]
    assert any("seconds" in i["issue"] for i in report.issues)
    hm, _ = normalize({"Candidate ID": list("AB"), "Time Taken": ["1:12", "0:58"]})
    assert hm[S.COMPLETION_MIN].tolist() == [72.0, 58.0]


def test_hackerrank_integrity_fields():
    df, report = normalize({
        "Candidate ID": list("ABCDE"),
        "Plagiarism Status": ["Suspicious", "Not Suspicious", "", "yes", "under review"],
        "Out of Window Duration (sec)": ["95", "95 s", "1m 35s", "00:01:35", "-3"],
        "Out of Window Events": ["3", "0", "", "2.0", "-1"],
        "Editor Paste Count": ["1", "yes", "no", "", "4"],
        "Secure Mode Exit Count": ["0", "1", "", "0", "2"],
        "Max Code Similarity %": ["92%", "15%", "", "100%", "150%"],
        "Q2 Code Similarity %": ["0.92", "0.15", "", "1", "0.5"],
        "Integrity Status": ["CLEAR", "review", "Suspicious", "", "Clear"],
        "Proctoring Flags": ["", "Multiple faces detected; Phone detected", "None", "", ""],
        "Test Mode": ["remote", "On Campus", "Online", "Hybrid", "campus lab"],
    })
    assert df[S.PLAGIARISM_FLAG].tolist() == [True, False, False, True, False]
    assert str(df[S.PLAGIARISM_FLAG].dtype) == "bool"
    assert df[S.OUT_OF_WINDOW_SEC].tolist()[:4] == [95.0] * 4 and pd.isna(df[S.OUT_OF_WINDOW_SEC][4])
    assert df[S.TAB_SWITCHES].tolist()[:2] == [3, 0] and str(df[S.TAB_SWITCHES].dtype) == "Int64"
    assert pd.isna(df[S.TAB_SWITCHES][4])
    assert df[S.COPY_PASTE].tolist()[:3] == [1, 1, 0]
    assert df[S.MAX_CODE_SIMILARITY].tolist()[:2] == [92.0, 15.0] and pd.isna(df[S.MAX_CODE_SIMILARITY][4])
    assert df["q2_similarity"].tolist()[:2] == pytest.approx([92.0, 15.0])  # fraction column -> x100
    assert df[S.INTEGRITY_STATUS].tolist()[:3] == ["Clear", "Review", "Suspicious"]
    assert df[S.PROCTORING_FLAGS][1] == "Multiple faces detected; Phone detected" and pd.isna(df[S.PROCTORING_FLAGS][2])
    assert df[S.TEST_MODE].tolist() == ["Remote", "On-campus", "Remote", "Hybrid", "On-campus"]
    assert any("unrecognised plagiarism status" in i["issue"] for i in report.issues)


def test_ip_addresses_and_subnets():
    df, _ = normalize({"Candidate ID": list("ABCD"),
                       "IP Address": ["49.36.12.7", "49.36.12.99:5050, 103.1.2.3", "2401:4900:1c2a::1", "not-an-ip"]})
    assert df[S.IP_ADDRESS].tolist()[:3] == ["49.36.12.7", "49.36.12.99", "2401:4900:1c2a::1"]
    assert df[S.IP_SUBNET].tolist()[:3] == ["49.36.12.0/24", "49.36.12.0/24", "2401:4900:1c2a::/64"]
    assert df[ingest.IP_ADDRESSES][1] == "49.36.12.99, 103.1.2.3"
    assert pd.isna(df[S.IP_ADDRESS][3]) and "invalid_ip" in flags_of(df, "D")


def test_duplicates_keep_latest_submission_and_shared_email():
    df, report = normalize({
        "Candidate ID": ["A", "B", "a", "C", "B", "D"],
        "Email": ["a@example.com", "b@example.com", "a@example.com", "c@example.com", "b@example.com", "c@example.com"],
        "Score": ["10", "20", "30", "40", "50", "60"],
        "Submitted At": ["2026-11-01 10:00", "2026-11-02 10:00", "2026-11-03 10:00", "2026-11-01 10:00", "", "2026-11-01 11:00"],
    })
    assert df[S.CANDIDATE_ID].tolist() == ["A", "B", "C", "D"]  # first-appearance order, one row per ID
    assert df.set_index(S.CANDIDATE_ID)[S.HR_SCORE].to_dict() == {"A": 30.0, "B": 20.0, "C": 40.0, "D": 60.0}
    assert report.duplicates_dropped == 2 and report.rows_raw == 6 and report.rows_clean == 4
    assert {i["candidate_id"] for i in report.issues if i["issue"].startswith("Duplicate candidate ID")} == {"A", "B"}
    assert "duplicate_id" in flags_of(df, "A") and "duplicate_id" not in flags_of(df, "C")
    assert "shared_email" in flags_of(df, "C") and "shared_email" in flags_of(df, "D")
    assert "shared_email" not in flags_of(df, "A")  # same email only on its own duplicate rows
    no_time, report2 = normalize({"Candidate ID": ["X", "X"], "Score": ["1", "2"]})
    assert no_time[S.HR_SCORE].tolist() == [2.0] and report2.duplicates_dropped == 1


def test_plagiarism_match_id_resolution():
    df, report = normalize({
        "Candidate ID": ["CAND-1", "CAND-2", "CAND-3", "CAND-4", "CAND-5"],
        "Full Name": ["Asha Menon", "Ravi Kumar", "Neha Singh", "Vikram Shah", "Priya Nair"],
        "Email": ["asha@example.com", "ravi@example.com", "neha@example.com", "vikram@example.com", "priya@example.com"],
        "Most Similar Candidate": ["RAVI@example.com", "cand-1", "outsider@example.com", "Asha Menon", "priya@example.com"],
    })
    resolved = df[ingest.PLAGIARISM_MATCH_ID]
    assert resolved[0] == "CAND-2" and resolved[1] == "CAND-1" and resolved[3] == "CAND-1"
    assert pd.isna(resolved[2]) and pd.isna(resolved[4])  # unknown email; self-reference
    assert df[S.PLAGIARISM_MATCH][0] == "RAVI@example.com"  # kept as given
    assert any("do not match any candidate" in i["issue"] for i in report.issues)


def test_extras_report_and_override():
    raw = frame({"Candidate ID": ["A"], "Notes": ["x"], "notes": ["y"], "Score": ["5"], "Invited By": ["ta@example.com"]})
    df, report = ingest.normalize_ta_feed(raw, ingest.suggest_column_mapping(list(raw.columns)), CFG)
    assert ["extra_notes", "extra_notes_2", "extra_invitedby"] == [c for c in df.columns if c.startswith("extra_")]
    assert df["extra_invitedby"][0] == "ta@example.com"
    assert report.column_mapping == {"Candidate ID": S.CANDIDATE_ID, "Score": S.HR_SCORE}
    assert report.unmapped_columns == ["Notes", "notes", "Invited By"]
    assert S.EMAIL in report.missing_fields and S.HR_SCORE not in report.missing_fields
    assert df.columns[-4] == S.DATA_FLAGS

    override = {"Candidate ID": S.CANDIDATE_ID, "Notes": "not_a_field", "notes": "", "Score": S.HR_SCORE,
                "Invited By": S.HR_SCORE, "Ghost": S.EMAIL}
    df2, report2 = ingest.normalize_ta_feed(raw, override, CFG)
    assert report2.column_mapping == {"Candidate ID": S.CANDIDATE_ID, "Score": S.HR_SCORE}
    assert {"extra_notes", "extra_notes_2", "extra_invitedby"} <= set(df2.columns)
    texts = " ".join(i["issue"] for i in report2.issues)
    assert "unknown field 'not_a_field'" in texts and "both map to 'hackerrank_score'" in texts and "'Ghost'" in texts


def test_no_score_or_identity_columns_is_reported_not_raised():
    df, report = normalize({"Remarks": ["good", "ok"]})
    assert len(df) == 2 and df[S.HR_PCT].isna().all()
    assert sum(i["severity"] == "error" for i in report.issues) == 2


JUNK = ["", " ", "NA", "-", "0", "-1", "1e309", "nan", "inf", "100%", "1:2:3", "12:", "2026-13-45", "31/31/2026",
        "99999999999999999999", "− 5", "a,b;c|d", "x@y", "1.2.3.4.5", "::1", "999.1.1.1", "Tier 9", "1h 99m",
        "2026-11-01T25:00:00Z", "None", "CAND-1", "cand-1 ", "12,5", "1,234,567", "0x1F", "٣", "½"]


def test_junk_values_never_raise():
    rng = random.Random(1)
    for _ in range(25):
        cols = rng.sample(HR_HEADERS, rng.randint(1, len(HR_HEADERS)))
        n = rng.randint(0, 30)
        raw = frame({c: [rng.choice(JUNK) if rng.random() < 0.7 else str(rng.uniform(-1e6, 1e6)) for _ in range(n)]
                     for c in cols})
        df, report = ingest.normalize_ta_feed(raw, ingest.suggest_column_mapping(cols), CFG)
        assert df[S.CANDIDATE_ID].is_unique and len(df) <= n and report.rows_raw == n
        if S.TAB_SWITCHES in df.columns:
            assert str(df[S.TAB_SWITCHES].dtype) == "Int64"


def test_empty_feed():
    raw = pd.DataFrame({"Candidate ID": pd.Series([], dtype="str"), "Score": pd.Series([], dtype="str")})
    df, report = ingest.normalize_ta_feed(raw, ingest.suggest_column_mapping(list(raw.columns)), CFG)
    assert len(df) == 0 and S.HR_PCT in df.columns and report.rows_clean == 0


# ---------------------------------------------------------------------------
# End to end on a synthetic HackerRank for Work export
# ---------------------------------------------------------------------------
def test_load_synthetic_hackerrank_feed_end_to_end():
    rows = hackerrank_rows(400)
    content = to_csv_bytes(HR_HEADERS, rows, title_rows=[["HackerRank for Work - Candidate Report"],
                                                         ["Exported 2026-11-04 by TA"]])
    df, report = ingest.load_ta_feed("hackerrank_export.csv", content, CFG)
    assert report.column_mapping == HR_EXPECTED
    assert report.unmapped_columns == ["Test Candidate ID", "Invited On", "Invited By"]
    assert len(df) == 400 and df[S.CANDIDATE_ID].is_unique
    completed = df[S.TEST_STATUS] == "Completed"
    assert set(df[S.TEST_STATUS]) <= {"Completed", "Expired", "Did Not Attempt"}
    assert df.loc[completed, S.HR_PCT].between(0, 100).all() and df.loc[~completed, S.HR_PCT].isna().all()
    set_b = df[S.TEST_NAME].str.endswith("Set B")
    assert (df.loc[set_b, S.MAX_SCORE] == 150).all() and (df.loc[~set_b, S.MAX_SCORE] == 100).all()
    q_total = df[S.question_score_columns(df.columns)].sum(axis=1, min_count=1)
    assert np.allclose(q_total[completed], df.loc[completed, S.HR_SCORE])
    perfect = df[S.HR_PCT] == 100
    assert perfect.sum() >= 5 and df.loc[perfect, S.COMPLETION_MIN].between(50, 85).all()
    assert df.loc[completed, S.COMPLETION_MIN].between(0, 90.01).all()
    assert df.loc[perfect, "q1_hash"].nunique() == 1  # MCQ answer key shared by every perfect scorer
    assert df[S.IP_SUBNET].dropna().str.endswith(".0/24").all()
    assert df[S.TEST_SLOT].dropna().str.match(r"^2026-11-0[1-3] \d{2}:00$").all()
    matched = df[S.PLAGIARISM_MATCH].notna()
    assert matched.any() and df.loc[matched, ingest.PLAGIARISM_MATCH_ID].notna().all()
    assert set(df[S.COLLEGE]) == {c for c, _, _ in COLLEGES}  # lower-cased variants canonicalised
    assert df[[*S.answer_hash_columns(df.columns)]].map(lambda v: pd.isna(v) or len(v) == 12).all().all()
    assert df[S.INTEGRITY_STATUS].isin(["Clear", "Review", "Suspicious"]).all()
    assert df["extra_testcandidateid"].str.match(r"^\d{9}$").all()


def test_ingest_is_deterministic_and_fast_at_scale():
    content = to_csv_bytes(HR_HEADERS, hackerrank_rows(3000, seed=3))
    t0 = time.perf_counter()
    df1, report1 = ingest.load_ta_feed("feed.csv", content, CFG)
    elapsed = time.perf_counter() - t0
    df2, report2 = ingest.load_ta_feed("feed.csv", content, CFG)
    assert len(df1) == 3000
    assert df1.equals(df2) and report1.issues == report2.issues
    assert elapsed < 10.0, f"ingest of 3,000 rows took {elapsed:.1f}s"


def test_protected_attribute_columns_are_dropped():
    """HackerRank can collect Gender in candidate details; it must never flow downstream."""
    import pandas as pd
    from screening import ingest
    from screening.config import AppConfig

    raw = pd.DataFrame({
        "Candidate ID": ["CAND-00001", "CAND-00002"],
        "Full Name": ["Asha Menon", "Ravi Kumar"],
        "Email": ["asha@example.com", "ravi@example.com"],
        "Score": ["100", "80"],
        "Max Score": ["100", "100"],
        "Gender": ["Female", "Male"],
        "Date of Birth": ["2003-01-01", "2002-05-05"],
    })
    mapping = ingest.suggest_column_mapping(list(raw.columns))
    mapping["Gender"] = "source"  # even an explicit mapping must not resurrect it
    df, report = ingest.normalize_ta_feed(raw, mapping, AppConfig())
    flat = " ".join(map(str, df.columns)).lower()
    assert "gender" not in flat and "birth" not in flat and "dob" not in flat
    assert "Female" not in df.astype(str).to_numpy().ravel().tolist()
    assert any("Protected attribute" in i["issue"] for i in report.issues)
