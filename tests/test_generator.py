"""Tests for generate_mock_data.py (synthetic Workday-style export + resume packets).

One --quick dataset (300 applications) is generated per module and validated directly with
openpyxl, pypdf and pdfplumber - never with the app's own ingest or packet parser - for structure,
distributions, the planted evaluation cases, privacy (reserved e-mail domains) and determinism.
"""
from __future__ import annotations

import copy
import io
import itertools
import json
import re
import zipfile
from collections import Counter
from pathlib import Path
from statistics import mean

import numpy as np
import openpyxl
import pandas as pd
import pdfplumber
import pypdf
import pytest
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

import generate_mock_data as gen
from screening.config import RiskConfig

N = gen.QUICK_N
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
REPO_RE = re.compile(r"github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9._-]+)")
PACKET_RE = re.compile(r"^Resume/R-100200 - 2027 Technology Program Intern_set (\d+)\.pdf$")
WHITE = {(1, 1, 1), (1.0, 1.0, 1.0)}
ZERO_WIDTH = set(gen.ZERO_WIDTH)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def out(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("quick") / "sample"
    assert gen.main(["--quick", "--out", str(path)]) == 0
    return path


@pytest.fixture(scope="module")
def workbook(out: Path) -> openpyxl.Workbook:
    return openpyxl.load_workbook(out / gen.EXPORT_FILE)


@pytest.fixture(scope="module")
def rows(workbook: openpyxl.Workbook) -> list[dict]:
    values = list(workbook[gen.DATA_SHEET].iter_rows(values_only=True))
    return [dict(zip(values[0], r)) for r in values[1:]]


@pytest.fixture(scope="module")
def by_id(rows: list[dict]) -> dict[str, dict]:
    return {r["Candidate ID"]: r for r in rows}


@pytest.fixture(scope="module")
def truth(out: Path) -> dict:
    folder = out / gen.TRUTH_DIR
    return {
        "labels": pd.read_csv(folder / "labels.csv", dtype=str, keep_default_na=False),
        "groups": json.loads((folder / "clusters.json").read_text(encoding="utf-8"))["clusters"],
        "issues": json.loads((folder / "injected_issues.json").read_text(encoding="utf-8")),
        "index": pd.read_csv(folder / "resume_index.csv", dtype=str, keep_default_na=False),
    }


@pytest.fixture(scope="module")
def groups(truth: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for g in truth["groups"]:
        out.setdefault(g["pattern"], []).append(g)
    return out


@pytest.fixture(scope="module")
def packets(out: Path) -> dict[str, dict]:
    """Packet file name -> raw bytes, pypdf reader and per-page text."""
    result = {}
    with zipfile.ZipFile(out / gen.ARCHIVE_FILE) as zf:
        for name in zf.namelist():
            if name.endswith(".pdf"):
                data = zf.read(name)
                reader = pypdf.PdfReader(io.BytesIO(data))
                result[name.split("/", 1)[1]] = {"data": data, "reader": reader,
                                                 "texts": [p.extract_text() or "" for p in reader.pages]}
    return result


@pytest.fixture(scope="module")
def index(truth: dict) -> dict[str, dict]:
    return {r["candidate_id"]: r for r in truth["index"].to_dict("records")}


@pytest.fixture(scope="module")
def resume_text(packets: dict[str, dict], index: dict[str, dict]):
    def text(cid: str) -> str:
        r = index[cid]
        pages = packets[r["packet"]]["texts"][int(r["first_page"]) - 1:int(r["last_page"])]
        return "\n".join(pages)
    return text


@pytest.fixture(scope="module")
def dataset() -> gen.Dataset:
    return gen.build_dataset(N, gen.DEFAULT_SEED)


def _flat(text: str) -> str:
    return " ".join(text.split())


def _number(value: object) -> float:
    assert isinstance(value, (int, float)) and not isinstance(value, bool), value
    return float(value)


def _parse_toc(lines: list[str]) -> list[dict]:
    """Table-of-contents rows plus the attachment lines printed under each row."""
    rows: list[dict] = []
    for line in lines:
        m = gen.TOC_ROW_RE.match(line)
        if m:
            rows.append({"number": int(m.group(1)), "name": m.group(2), "cid": m.group(3), "attachments": []})
        elif rows and line.strip() and not line.startswith("Candidate Name"):
            rows[-1]["attachments"].append(line.strip())
    return rows


# ---------------------------------------------------------------------------
# Output files and workbook
# ---------------------------------------------------------------------------


def test_output_files(out: Path) -> None:
    for name in (gen.EXPORT_FILE, gen.ARCHIVE_FILE, "job_description.txt", "README.md", gen.MARKER_FILE,
                 "_ground_truth/labels.csv", "_ground_truth/clusters.json", "_ground_truth/injected_issues.json",
                 "_ground_truth/resume_index.csv"):
        assert (out / name).exists(), name
    for legacy in gen.LEGACY_ENTRIES:
        assert not (out / legacy).exists()
    readme = (out / "README.md").read_text(encoding="utf-8").lower()
    assert "synthetic" in readme and "fictional" in readme
    assert "synthetic" in (gen.__doc__ or "").lower()
    jd = (out / "job_description.txt").read_text(encoding="utf-8")
    assert "bank" in jd.lower() and gen.JOB_TITLE in jd


def test_workbook_sheets_and_summary(workbook: openpyxl.Workbook) -> None:
    assert workbook.sheetnames == ["Sheet1", "Sheet2"]
    summary = list(workbook["Sheet1"].iter_rows(values_only=True))
    assert 12 <= len(summary) <= 18
    assert all(sum(v not in (None, "") for v in row) < 3 for row in summary)
    assert str(summary[0][0]).startswith("Review Candidates: R-100200")
    cells = {row[0]: row[1] for row in summary if row[0]}
    assert re.fullmatch(r"\d+ of \d+", cells["Active Candidates"])
    active, total = map(int, cells["Active Candidates"].split(" of "))
    assert active < total and active >= N
    assert cells["Numeric Return Attribute"] == "Recruiting Stage (Workday Owner)"
    stages = {row[1]: row[0] for row in summary if row[1] in gen.SUMMARY_STAGES}
    assert set(stages) == set(gen.SUMMARY_STAGES) and all(re.fullmatch(r"\d+", v) for v in stages.values())
    assert int(stages["Review"]) + int(stages["Assessment"]) == active and stages["Screen"] == "0"


def test_export_columns_and_constants(workbook: openpyxl.Workbook, rows: list[dict]) -> None:
    sheet = workbook["Sheet2"]
    header = [c.value for c in sheet[1]]
    assert header == gen.EXPORT_COLUMNS and len(header) == 14
    assert "Name" not in header and "Phone" not in header
    assert len(rows) == N
    ids = [r["Candidate ID"] for r in rows]
    assert len(set(ids)) == N and all(re.fullmatch(r"C-\d{7}", i) for i in ids)
    for r in rows:
        m = re.fullmatch(r"(.+) \((.+)\)", r["Job Application"])
        assert m and m.group(2) == m.group(1).lower(), r["Job Application"]
        assert r["Cutoff Score"] == gen.CUTOFF_SCORE == 96
    scored = [r for r in rows if r["Score"] != gen.NA]
    unscored = [r for r in rows if r["Score"] == gen.NA]
    assert len(unscored) == 1
    for r in scored:
        assert r["Max Score"] == 175 and r["Cutoff Score Percentage"] == 55
        score = _number(r["Score"])
        assert 100 <= score <= 175 and round(score, 2) == score
        assert _number(r["Percentage Score"]) == round(score / 175 * 100, 2)
        assert r["Candidate Option"] in {"Django (Python)", "Node.JS (JavaScript)", "Spring Boot (Java)"}
    row = unscored[0]
    assert row["Max Score"] in (100, None)
    for col in ("Score", "Percentage Score", "Cutoff Score Percentage", "Candidate Option"):
        assert row[col] == "#N/A"
    position = ids.index(row["Candidate ID"]) + 2
    assert sheet.cell(row=position, column=header.index("Score") + 1).data_type == "e"   # Excel error value


def test_distributions(rows: list[dict]) -> None:
    scored = [_number(r["Score"]) for r in rows if r["Score"] != gen.NA]
    assert 0.55 <= sum(s == 175 for s in scored) / len(scored) <= 0.73
    assert any(s != int(s) for s in scored)                     # partial credit -> decimals
    cgpa = [_number(r["CGPA"]) for r in rows]
    assert min(cgpa) >= 8.5 and max(cgpa) <= 10.0 and abs(mean(cgpa) - 9.06) < 0.08
    branches = Counter(r["Branch"] for r in rows)
    assert 0.60 <= branches["Computer Science & Engineering"] / N <= 0.78
    assert 0.05 <= branches["Information Technology"] / N <= 0.18
    assert len(branches) >= 5
    assert {r["Candidate Option"] for r in rows} >= {"Django (Python)", "Node.JS (JavaScript)", "Spring Boot (Java)"}
    schools = Counter(r["School"] for r in rows)
    assert any(re.fullmatch(r"[A-Z]{2}-IICT", s) for s in schools) and len(schools) >= 20


def test_assessment_comment_format(rows: list[dict], groups: dict) -> None:
    head = "Hackerrank Completed \u2013 Qualified&#xa;Integrity Issues: "
    flagged = []
    for r in rows:
        comment = r["Assessment Comment"]
        assert comment.startswith(head), repr(comment)
        assert comment.count("\n") == 1 and "\nIntegrity Summary: " in comment
        if comment != gen.COMMENT_CLEAN:
            assert comment == gen.COMMENT_CODE_SIMILARITY
            flagged.append(r["Candidate ID"])
    assert gen.COMMENT_CLEAN.endswith("No Issues Detected\nIntegrity Summary: N/A")
    assert "Integrity Issues: Medium\nIntegrity Summary: Detected Code Similarity With Other Submissions" \
        in gen.COMMENT_CODE_SIMILARITY
    assert sorted(flagged) == groups[gen.CODE_SIMILARITY][0]["members"] and len(flagged) == 2


def test_emails_webmail_and_institutional(rows: list[dict]) -> None:
    by_school = {}
    for c in gen.COLLEGES:
        by_school[c.display] = c
        if c.alt_school:
            by_school[c.alt_school] = c
    kinds = Counter()
    for r in rows:
        domain = r["Email"].rsplit("@", 1)[1]
        if domain == gen.WEBMAIL_DOMAIN:
            kinds["webmail"] += 1
        else:
            assert domain == f"{by_school[r['School']].slug}.example", (r["Email"], r["School"])
            kinds["institutional"] += 1
    assert 0.15 <= kinds["institutional"] / N <= 0.45
    assert len({r["Email"] for r in rows}) == N


def test_reserved_domains_everywhere(out: Path, workbook: openpyxl.Workbook, packets: dict) -> None:
    texts = [str(v) for ws in workbook for row in ws.iter_rows(values_only=True) for v in row if v]
    texts += [p["texts"][i] for p in packets.values() for i in range(len(p["texts"]))]
    texts += [(out / name).read_text(encoding="utf-8") for name in ("README.md", "job_description.txt")]
    texts += [p.read_text(encoding="utf-8") for p in (out / gen.TRUTH_DIR).iterdir()]
    domains = Counter(m.group(1).lower() for t in texts for m in EMAIL_RE.finditer(t))
    assert domains
    bad = {d for d in domains if d != gen.WEBMAIL_DOMAIN and not d.endswith(".example")}
    assert not bad, bad


# ---------------------------------------------------------------------------
# Resume archive and packets
# ---------------------------------------------------------------------------


def test_archive_layout(out: Path, truth: dict) -> None:
    with zipfile.ZipFile(out / gen.ARCHIVE_FILE) as zf:
        names = zf.namelist()
    assert names[0] == "Resume/"
    sets = sorted(int(PACKET_RE.match(n).group(1)) for n in names[1:])
    assert sets == list(range(1, len(sets) + 1)) and len(sets) == gen.default_packets(N)
    assert [p["set_number"] for p in truth["issues"]["archive"]["packets"]] == sets


def test_packet_structure(packets: dict, index: dict, by_id: dict) -> None:
    seen = []
    for name, packet in packets.items():
        reader, texts = packet["reader"], packet["texts"]
        outline = reader.outline
        assert outline and all(not isinstance(o, list) for o in outline)     # flat: one bookmark per attachment
        starts = [reader.get_destination_page_number(o) for o in outline]
        assert starts == sorted(starts) and len(set(starts)) == len(starts)
        toc_pages = starts[0]
        assert 1 <= toc_pages <= gen.MAX_TOC_PAGES
        lines = [line for i in range(toc_pages) for line in texts[i].splitlines()]
        set_number = int(re.search(r"_set (\d+)\.pdf$", name).group(1))
        assert lines[0] == "Table of Contents"
        assert re.fullmatch(rf"R-100200 - 2027 Technology Program Intern \({set_number} of {len(packets)}\)", lines[1])
        assert lines[2] == "Candidate Name Candidate ID Attachments"
        toc = _parse_toc(lines[3:])
        assert [r["number"] for r in toc] == list(range(1, len(toc) + 1))
        assert gen.MIN_PER_PACKET <= len(toc) <= gen.MAX_PER_PACKET
        assert sum(len(r["attachments"]) for r in toc) == len(outline)
        k = 0
        for i, row in enumerate(toc):
            app = by_id[row["cid"]]
            assert app["Job Application"].startswith(row["name"] + " (")
            first = starts[k]
            for attachment in row["attachments"]:
                assert outline[k].title == f"{row['name']} - {attachment}"
                k += 1
            last = starts[k] if k < len(starts) else len(reader.pages)       # [start_i, start_{i+1})
            rec = index[row["cid"]]
            assert (rec["packet"], int(rec["toc_number"])) == (name, row["number"])
            assert (int(rec["first_page"]), int(rec["last_page"])) == (first + 1, last)
            assert rec["bundle_source"] == f"{name} p.{first + 1}-{last}"
            seen.append(row["cid"])
    assert len(seen) == len(set(seen)) == len(index)


def test_resume_coverage_and_page_counts(truth: dict, index: dict) -> None:
    labels = truth["labels"]
    with_resume = set(labels.loc[labels["has_resume"] == "yes", "candidate_id"])
    assert set(index) == with_resume
    assert sorted(set(labels["candidate_id"]) - with_resume) == truth["issues"]["missing_resume"]
    assert 0.09 <= 1 - len(with_resume) / N <= 0.15
    resume_pages = Counter(int(r["resume_pages"]) for r in index.values())
    total = sum(resume_pages.values())
    assert 0.75 <= resume_pages[1] / total <= 0.90 and 0.08 <= resume_pages[2] / total <= 0.25
    long = {cid for cid, r in index.items() if int(r["resume_pages"]) >= 3}
    assert long and all(int(index[c]["resume_pages"]) <= 6 for c in long)
    assert long == {r["candidate_id"] for r in truth["issues"]["long_resumes"]}
    multi = {cid for cid, r in index.items() if int(r["attachments"]) == 2}
    assert multi and multi == {r["candidate_id"] for r in truth["issues"]["second_attachments"]}
    for cid, r in index.items():
        assert int(r["pages"]) == int(r["last_page"]) - int(r["first_page"]) + 1 >= int(r["resume_pages"])
        assert r["split_file_name"] == f"{cid}_{gen._clean_name(r['name'])}.pdf"
        assert re.fullmatch(r"C-\d{7}_[A-Za-z0-9_]+\.pdf", r["split_file_name"])


def test_resumes_are_realistic(index: dict, resume_text, by_id: dict) -> None:
    texts = {cid: _flat(resume_text(cid)) for cid in index}
    for cid in sorted(index)[:40]:
        text = texts[cid]
        assert "CGPA" in text and re.search(r"projects", text, re.IGNORECASE), cid
        assert index[cid]["name"].split()[0].lower() in text.lower()
    assert sum("github.com/" in t for t in texts.values()) / len(texts) > 0.6
    emails = sum(by_id[c]["Email"] in texts[c] for c in index)
    assert emails / len(index) > 0.7          # most resumes list the application e-mail


# ---------------------------------------------------------------------------
# Planted evaluation cases
# ---------------------------------------------------------------------------


def test_planted_cases_present(groups: dict, truth: dict) -> None:
    counts = gen.planted_counts(N)
    assert len(groups[gen.DUPLICATE]) == counts["duplicate_pairs"]
    assert len(groups[gen.TEAM]) == counts["team_pairs"]
    assert [g["size"] for g in groups[gen.MILL]] == list(counts["mill_sizes"])
    assert len(groups[gen.CODE_SIMILARITY]) == 1 and groups[gen.CODE_SIMILARITY][0]["size"] == 2
    assert len(groups[gen.INJECTION]) == counts["injections"]
    assert len(groups[gen.NAMESAKE]) == counts["namesake_pairs"]
    labels = truth["labels"].set_index("candidate_id")
    for pattern, items in groups.items():
        for g in items:
            assert g["description"] and g["members"] == sorted(g["members"])
            for cid in g["members"]:
                assert labels.loc[cid, "label"] == pattern and labels.loc[cid, "group_key"] == g["key"]
                if pattern not in (gen.BOILERPLATE,):
                    assert labels.loc[cid, "has_resume"] == "yes"
    for g in groups[gen.MILL] + groups[gen.CODE_SIMILARITY]:
        assert g["expected_min_level"] == "Medium"
    for g in groups[gen.INJECTION]:   # invisible characters alone are a Low signal; hidden text / instructions Medium
        only_invisible = g["details"]["kinds"] == ["invisible_chars"]
        assert g["expected_min_level"] == ("Low" if only_invisible else "Medium")
    for g in groups[gen.TEAM]:
        assert g["must_not_exceed"] == "Low"


def test_duplicate_applications(groups: dict, by_id: dict, resume_text, truth: dict) -> None:
    labels = truth["labels"].set_index("candidate_id")
    for g in groups[gen.DUPLICATE]:
        a, b = (by_id[c] for c in g["members"])
        assert a["Candidate ID"] != b["Candidate ID"]
        assert a["Job Application"] == b["Job Application"] and a["School"] == b["School"]
        domains = {r["Email"].rsplit("@", 1)[1] == gen.WEBMAIL_DOMAIN for r in (a, b)}
        assert domains == {True, False}                                  # webmail vs institutional
        kept = g["details"]["kept"]
        assert by_id[kept]["Email"].rsplit("@", 1)[1] != gen.WEBMAIL_DOMAIN
        assert labels.loc[g["details"]["duplicate"], "duplicate_of"] == kept and labels.loc[kept, "duplicate_of"] == ""
        assert _flat(resume_text(a["Candidate ID"])) == _flat(resume_text(b["Candidate ID"]))   # identical resume


def test_namesakes_are_different_people(groups: dict, by_id: dict, resume_text) -> None:
    schools_differ = 0
    for g in groups[gen.NAMESAKE]:
        a, b = (by_id[c] for c in g["members"])
        assert a["Job Application"] == b["Job Application"] and a["Email"] != b["Email"]
        assert _flat(resume_text(a["Candidate ID"])) != _flat(resume_text(b["Candidate ID"]))
        schools_differ += a["School"] != b["School"]
    assert schools_differ >= 1


def _repos(text: str) -> set[str]:
    return {f"github.com/{u.lower()}/{r.lower().rstrip('.')}" for u, r in REPO_RE.findall(text)}


def _long_bullet_keys(rc: gen.ResumeContent, min_words: int) -> set[str]:
    return {k for k in map(gen._bullet_key, rc.all_bullets()) if len(k.split()) >= min_words}


def _page_text(chars: list[dict], white: bool) -> str:
    """Text of the white (or non-white) characters of a page, with line breaks kept."""
    out, last_top = [], None
    for c in chars:
        if (tuple(c.get("non_stroking_color") or ()) in WHITE) != white:
            continue
        if last_top is not None and abs(c["top"] - last_top) > 1:
            out.append("\n")
        out.append(c["text"])
        last_top = c["top"]
    return "".join(out)


def test_team_projects_share_one_repo_only(groups: dict, by_id: dict, resume_text, dataset: gen.Dataset) -> None:
    rc_cfg = RiskConfig()
    contents = {a.cid: dataset.resumes[a.person.pid] for a in dataset.applications if a.person.has_resume}
    for g in groups[gen.TEAM]:
        a, b = g["members"]
        assert by_id[a]["School"] == by_id[b]["School"]
        shared = _repos(_flat(resume_text(a))) & _repos(_flat(resume_text(b)))
        assert shared == {g["details"]["shared_repo"].lower()}
        assert len(shared) <= rc_cfg.team_max_shared_repos
        shared_bullets = (_long_bullet_keys(contents[a], rc_cfg.min_bullet_words)
                          & _long_bullet_keys(contents[b], rc_cfg.min_bullet_words))
        assert len(shared_bullets) <= rc_cfg.team_max_shared_bullets and not shared_bullets
    # Text similarity (risk-engine settings) stays below the team-project ceiling.
    pids = sorted(dataset.resumes)
    docs = [dataset.resumes[p].section_text("projects") + "\n" + dataset.resumes[p].section_text("experience")
            for p in pids]
    sims = cosine_similarity(TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, stop_words="english",
                                             max_df=0.85).fit_transform(docs))
    pos = {p: i for i, p in enumerate(pids)}
    pairs: dict[str, list[int]] = {}
    for p in dataset.people:
        if p.role == gen.TEAM:
            pairs.setdefault(p.group_key, []).append(pos[p.pid])
    assert len(pairs) == len(groups[gen.TEAM])
    for i, j in pairs.values():
        assert sims[i, j] < rc_cfg.team_max_text_similarity


def test_resume_mills(groups: dict, by_id: dict, resume_text, index: dict, dataset: gen.Dataset) -> None:
    rc_cfg = RiskConfig()
    rare_max = max(rc_cfg.max_bullet_population_share * len(index), 5)
    contents = {a.cid: dataset.resumes[a.person.pid] for a in dataset.applications if a.person.has_resume}
    for g in groups[gen.MILL]:
        members = g["members"]
        assert 4 <= len(members) <= 7 and len(members) <= rare_max          # still "rare" evidence
        assert len({by_id[c]["School"] for c in members}) == len(members)  # different colleges
        details = g["details"]
        assert len(details["github_repos"]) == 2 and len(details["shared_bullets"]) >= 3
        assert all(len(gen._bullet_key(b).split()) >= rc_cfg.min_bullet_words for b in details["shared_bullets"])
        for cid in members:
            text = _flat(resume_text(cid))
            assert all(repo in text for repo in details["github_repos"])
            assert all(bullet in text for bullet in details["shared_bullets"])
        for a, b in itertools.combinations(members, 2):
            shared = (_long_bullet_keys(contents[a], rc_cfg.min_bullet_words)
                      & _long_bullet_keys(contents[b], rc_cfg.min_bullet_words))
            assert len(shared) == len(details["shared_bullets"])            # only the kit's bullets


def test_planted_members_score_full_marks(groups: dict, by_id: dict) -> None:
    for pattern in (gen.TEAM, gen.MILL, gen.CODE_SIMILARITY, gen.INJECTION):
        for g in groups[pattern]:
            for cid in g["members"]:
                assert by_id[cid]["Score"] == 175, (pattern, cid)


def test_prompt_injection_resumes(groups: dict, packets: dict, index: dict, resume_text) -> None:
    kinds_seen = set()
    for g in groups[gen.INJECTION]:
        cid = g["members"][0]
        d = g["details"]
        kinds_seen.update(d["kinds"])
        rec = index[cid]
        with pdfplumber.open(io.BytesIO(packets[rec["packet"]]["data"])) as pdf:
            chars = pdf.pages[int(rec["first_page"]) - 1].chars
        white, visible = _page_text(chars, white=True), _page_text(chars, white=False)
        squash = lambda s: re.sub(r"\s+", "", s).lower()   # noqa: E731
        text = resume_text(cid)
        if "hidden_text" in d["kinds"]:
            assert d["hidden_instruction"] and len(d["hidden_keywords"]) >= 4
            assert "ignore" in d["hidden_instruction"].lower()
            assert squash(d["hidden_instruction"]) in squash(white)
            assert squash(d["hidden_instruction"]) in squash(text)            # plain extraction reads it
            for keyword in d["hidden_keywords"]:
                assert squash(keyword) in squash(white)
                assert not gen._has_word(visible, keyword), keyword           # only the hidden text has it
        else:
            assert not white.strip()
        if "invisible_chars" in d["kinds"]:
            assert d["zero_width_chars"] >= 20
            assert sum(ch in ZERO_WIDTH for ch in text) == d["zero_width_chars"]
        else:
            assert not any(ch in ZERO_WIDTH for ch in text)
        if "instruction" in d["kinds"]:
            assert squash(d["visible_instruction"]) in squash(visible)
    assert kinds_seen == set(gen.INJECTION_KINDS)


# ---------------------------------------------------------------------------
# Precision traps: honest resumes must not look related
# ---------------------------------------------------------------------------


def test_honest_resumes_share_no_long_bullets(dataset: gen.Dataset) -> None:
    allowed = {gen._bullet_key(b) for b in gen.BOILERPLATE_BULLETS}
    allowed |= {gen._bullet_key(b) for _, kit in gen.MILL_KITS for *_, shared in kit for b in shared}
    min_words = RiskConfig().min_bullet_words
    holders: dict[str, set[int]] = {}
    for pid, rc in dataset.resumes.items():
        for b in rc.all_bullets() + rc.positions + rc.certifications:
            holders.setdefault(gen._bullet_key(b), set()).add(pid)
        assert all(len(gen._bullet_key(x).split()) < min_words for x in rc.certifications + rc.positions)
    shared = [k for k, who in holders.items() if len(who) > 1 and k not in allowed and len(k.split()) >= min_words]
    assert not shared, shared[:3]


def test_boilerplate_bullets_are_common(dataset: gen.Dataset) -> None:
    rc = RiskConfig()
    rare_max = max(rc.max_bullet_population_share * len(dataset.resumes), 5)
    counts = Counter(b for r in dataset.resumes.values() for b in r.all_bullets() if b in gen.BOILERPLATE_BULLETS)
    assert set(counts) == set(gen.BOILERPLATE_BULLETS)
    assert min(counts.values()) > rare_max


def test_honest_text_similarity_below_review_threshold(dataset: gen.Dataset) -> None:
    rc = RiskConfig()
    role = {p.pid: p.role for p in dataset.people}
    honest = [pid for pid in sorted(dataset.resumes) if role[pid] != gen.MILL]
    docs = [dataset.resumes[p].section_text("projects") + "\n" + dataset.resumes[p].section_text("experience")
            for p in honest]
    sims = cosine_similarity(TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, stop_words="english",
                                             max_df=0.85).fit_transform(docs))
    assert sims[np.triu_indices_from(sims, k=1)].max() < rc.text_similarity_medium


# ---------------------------------------------------------------------------
# Determinism, safety and CLI
# ---------------------------------------------------------------------------


def test_determinism_and_legacy_cleanup(out: Path, tmp_path: Path) -> None:
    again = tmp_path / "again"
    again.mkdir()
    (again / gen.MARKER_FILE).write_text("Synthetic dataset written by generate_mock_data.py\n", encoding="utf-8")
    (again / "ta_feed.csv").write_text("old format\n", encoding="utf-8")
    (again / "resumes").mkdir()
    (again / "resumes" / "old.txt").write_text("old format\n", encoding="utf-8")
    assert gen.main(["--quick", "--out", str(again), "--workers", "1"]) == 0
    assert not (again / "ta_feed.csv").exists() and not (again / "resumes").exists()
    for rel in (gen.EXPORT_FILE, gen.ARCHIVE_FILE, "README.md", "job_description.txt", "_ground_truth/labels.csv",
                "_ground_truth/clusters.json", "_ground_truth/injected_issues.json", "_ground_truth/resume_index.csv"):
        assert (again / rel).read_bytes() == (out / rel).read_bytes(), rel
    other = gen.build_dataset(N, seed=7)
    assert other.export_rows() != gen.build_dataset(N, seed=gen.DEFAULT_SEED).export_rows()


def test_resume_fitting_trims_and_breaks(dataset: gen.Dataset) -> None:
    """Overfull content is trimmed to one page (planted / protected parts never); thin content is
    split over two pages when a two-page resume is required."""
    base = next(r for r in dataset.resumes.values() if r.style["layout"] == "modern" and len(r.projects) >= 2)
    full = copy.deepcopy(base)
    full.projects = [copy.deepcopy(p) for p in base.projects * 4]
    for p in full.projects:
        p.bullets = p.bullets * 3
    full.projects[0].protected = True
    full.achievements, full.certifications = base.achievements * 4, base.certifications * 4
    data = gen._render_resume(gen.AttachmentJob("cv.pdf", "resume", base.name, 1, resume=full, resume_pages=1))
    reader = pypdf.PdfReader(io.BytesIO(data))
    assert len(reader.pages) == 1
    text = _flat(reader.pages[0].extract_text())
    assert all(_flat(b)[:40] in text for b in full.projects[0].bullets)      # protected bullets survive
    thin = copy.deepcopy(base)
    thin.projects, thin.experiences = thin.projects[:1], []
    data = gen._render_resume(gen.AttachmentJob("cv.pdf", "resume", base.name, 2, resume=thin, resume_pages=2))
    assert len(pypdf.PdfReader(io.BytesIO(data)).pages) == 2
    lim, steps = gen.Limits(), 0
    while (nxt := gen._trimmed(lim, protect_summary=True)) is not None:
        assert nxt != lim and nxt.summary                                      # summary of a planted case stays
        lim, steps = nxt, steps + 1
    assert steps >= 10 and lim.scale < 1 and lim.projects == 2 and not lim.extras


def test_rendering_is_byte_reproducible(dataset: gen.Dataset) -> None:
    job = min(dataset.packet_jobs, key=lambda j: len(j.entries))
    first, second = gen._build_packet(job), gen._build_packet(job)
    assert first.data == second.data and first.entries == second.entries


def test_refuses_to_overwrite_foreign_folder(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("user data", encoding="utf-8")
    assert gen.main(["--quick", "--out", str(tmp_path)]) == 2
    assert (tmp_path / "keep.txt").read_text(encoding="utf-8") == "user data"
    assert not (tmp_path / gen.EXPORT_FILE).exists()


def test_cli_validation() -> None:
    for argv in (["--n", "10"], ["--packets", "0"], ["--workers", "-1"]):
        with pytest.raises(SystemExit):
            gen.main(argv)


@pytest.mark.parametrize("total", [5, 9, 132, 264, 1877, 10560])
def test_packet_sizes_are_feasible(total: int) -> None:
    for requested in (1, 3, gen.default_packets(total), 500):
        k = gen.feasible_packets(total, requested)
        sizes = gen._packet_sizes(total, k, gen._rng(1, "sizes", total, requested))
        assert sum(sizes) == total and len(sizes) == k
        if k > 1:
            assert all(gen.MIN_PER_PACKET <= s <= gen.MAX_PER_PACKET for s in sizes)
