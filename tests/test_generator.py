"""Tests for generate_mock_data.py (synthetic dataset generator).

One --quick dataset (300 candidates) is generated per module and checked for structure,
determinism, the planted integrity patterns / decoys and the injected data-quality problems.
The tests only use the generator, pandas and the document libraries - never other engine modules.
"""
from __future__ import annotations

import itertools
import json
import re
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path

import docx
import numpy as np
import pandas as pd
import pypdf
import pytest
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

import generate_mock_data as gen
from screening.config import RiskConfig

N = gen.QUICK_N
RESERVED = set(gen.RESERVED_EMAIL_DOMAINS)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def out(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("quick") / "sample"
    assert gen.main(["--quick", "--out", str(path)]) == 0
    return path


@pytest.fixture(scope="module")
def ta(out: Path) -> pd.DataFrame:
    return pd.read_csv(out / "ta_feed.csv", dtype=str, keep_default_na=False)


@pytest.fixture(scope="module")
def clusters(out: Path) -> dict[str, dict]:
    payload = json.loads((out / "_ground_truth" / "clusters.json").read_text(encoding="utf-8"))
    return {c["key"]: c for c in payload["clusters"]}


@pytest.fixture(scope="module")
def issues(out: Path) -> dict:
    return json.loads((out / "_ground_truth" / "injected_issues.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def labels(out: Path) -> pd.DataFrame:
    return pd.read_csv(out / "_ground_truth" / "labels.csv", dtype=str, keep_default_na=False)


@pytest.fixture(scope="module")
def resume_map(out: Path) -> pd.DataFrame:
    return pd.read_csv(out / "_ground_truth" / "resume_files.csv", dtype=str, keep_default_na=False)


@pytest.fixture(scope="module")
def dataset() -> gen.Dataset:
    return gen.build_dataset(N, gen.DEFAULT_SEED)


def _members(ta: pd.DataFrame, cluster: dict) -> pd.DataFrame:
    rows = ta[ta["Candidate ID"].isin(cluster["members"])]
    assert len(rows) == cluster["size"] == len(cluster["members"])
    return rows


def _times(rows: pd.DataFrame, col: str) -> list[datetime]:
    return [datetime.fromisoformat(v) for v in rows[col]]


def _span_sec(values: list[datetime]) -> float:
    return (max(values) - min(values)).total_seconds()


def _durations_sec(rows: pd.DataFrame) -> list[float]:
    return [(e - s).total_seconds() for s, e in zip(_times(rows, "Attempt Start Time"),
                                                     _times(rows, "Attempt End Time"))]


def _resume_text(path: Path) -> str:
    if path.suffix == ".pdf":
        return "\n".join(page.extract_text() or "" for page in pypdf.PdfReader(path).pages)
    if path.suffix == ".docx":
        return "\n".join(p.text for p in docx.Document(str(path)).paragraphs)
    return path.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


# ---------------------------------------------------------------------------
# Structure & determinism
# ---------------------------------------------------------------------------


def test_output_structure(out: Path, ta: pd.DataFrame, labels: pd.DataFrame, resume_map: pd.DataFrame) -> None:
    for name in ("ta_feed.csv", "resumes", "resumes.zip", "job_description.txt", "README.md",
                 "_ground_truth/labels.csv", "_ground_truth/clusters.json",
                 "_ground_truth/injected_issues.json", "_ground_truth/resume_files.csv"):
        assert (out / name).exists(), name
    assert list(ta.columns) == gen.TA_COLUMNS
    assert len(ta) == N + gen.N_DUPLICATES
    assert ta["Candidate ID"].nunique() == N
    assert ta["Candidate ID"].str.fullmatch(r"CAND-\d{5}").all()
    assert set(ta["Max Score"]) == {"100", "150"}
    set_b = ta["Test Name"].str.endswith("Set B")
    assert set(ta.loc[set_b, "Max Score"]) == {"150"} and 0.03 < set_b.mean() < 0.2
    done = ta["Status"] == "Completed"
    assert set(ta.loc[~done, "Status"]) <= {"Expired", "Did Not Attempt"}
    assert (ta.loc[~done, ["Score", "Attempt Start Time", "Q1 Answer Hash"]] == "").all().all()

    assert list(labels.columns) == gen.LABEL_COLUMNS and len(labels) == N
    assert set(labels["label"]) == {"colluder", "decoy", "normal"}
    assert set(labels["expected_min_level"]) == {"High", "Medium", "None"}

    files = sorted(p.name for p in (out / "resumes").iterdir())
    assert files == sorted(resume_map["file_name"])
    primaries = resume_map[resume_map["kind"] == "primary"]
    assert 0.9 * N <= len(primaries) <= 0.99 * N
    with zipfile.ZipFile(out / "resumes.zip") as zf:
        assert sorted(n.removeprefix("resumes/") for n in zf.namelist()) == files

    readme = (out / "README.md").read_text(encoding="utf-8")
    assert "synthetic" in readme.lower() and "fictional" in readme.lower()
    assert "synthetic" in (gen.__doc__ or "").lower()
    assert "bank" in (out / "job_description.txt").read_text(encoding="utf-8").lower()


def test_determinism_same_seed_identical_bytes(out: Path, tmp_path: Path) -> None:
    again = tmp_path / "again"
    assert gen.main(["--quick", "--out", str(again), "--formats", "txt", "--no-zip"]) == 0
    for rel in ("ta_feed.csv", "_ground_truth/labels.csv", "_ground_truth/clusters.json"):
        assert (again / rel).read_bytes() == (out / rel).read_bytes(), rel
    assert not (again / "resumes.zip").exists()
    assert {p.suffix for p in (again / "resumes").iterdir() if p.stat().st_size} <= {".txt", ".pdf"}
    other = gen.build_dataset(N, seed=7)
    assert other.ta_csv_bytes() != (out / "ta_feed.csv").read_bytes()


def test_rendering_is_byte_reproducible(dataset: gen.Dataset) -> None:
    jobs = gen._render_jobs(dataset.resumes)
    for kind in ("pdf", "docx", "image_only_pdf", "corrupt_pdf"):
        job = next(j for j in jobs if j.kind == kind)
        assert gen._render(job) == gen._render(job), kind


def test_refuses_to_overwrite_foreign_folder(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("user data", encoding="utf-8")
    assert gen.main(["--quick", "--out", str(tmp_path), "--formats", "txt", "--no-zip"]) == 2
    assert (tmp_path / "keep.txt").read_text(encoding="utf-8") == "user data"
    assert not (tmp_path / "ta_feed.csv").exists()


def test_cli_validation() -> None:
    assert gen._parse_formats("txt, PDF") == ("pdf", "txt")
    with pytest.raises(SystemExit):
        gen.main(["--formats", "rtf"])
    with pytest.raises(SystemExit):
        gen.main(["--n", "10"])


# ---------------------------------------------------------------------------
# Planted integrity patterns
# ---------------------------------------------------------------------------


def test_planted_members_are_perfect_and_labelled(ta: pd.DataFrame, labels: pd.DataFrame,
                                                  clusters: dict[str, dict]) -> None:
    lab = labels.set_index("candidate_id")
    planted = {k: c for k, c in clusters.items() if c["label"] == "colluder"}
    sizes = gen.pattern_sizes(N)
    assert {c["pattern"] for c in planted.values()} == set(sizes)
    for key, cluster in planted.items():
        assert cluster["size"] == sizes[cluster["pattern"]]
        assert cluster["description"] and cluster["expected_signals"]
        rows = _members(ta, cluster)
        assert (rows["Status"] == "Completed").all()
        assert (rows["Score"] == rows["Max Score"]).all() and (rows["Percentage Score"] == "100").all()
        assert (rows["Test Mode"] == "Remote").all()
        for cid in cluster["members"]:
            assert lab.loc[cid, "label"] == "colluder" and lab.loc[cid, "cluster_key"] == key
            assert lab.loc[cid, "expected_min_level"] == cluster["expected_min_level"]
    assert (labels["label"] == "colluder").sum() == sum(sizes.values())


def test_ring_shared_solution(ta: pd.DataFrame, clusters: dict[str, dict], out: Path,
                              resume_map: pd.DataFrame) -> None:
    ring = clusters["ring_01"]
    rows = _members(ta, ring)
    emails = set(rows["Email"])
    assert rows["College"].nunique() == 1
    for col in ("Q2 Code Hash", "Q3 Code Hash", "Q4 Code Hash"):
        assert rows[col].nunique() == 1 and rows[col].iloc[0]
        assert (ta[col] == rows[col].iloc[0]).sum() == len(rows)          # nobody else has it
    for col in ("Q2 Code Similarity %", "Q3 Code Similarity %", "Q4 Code Similarity %"):
        assert (rows[col].astype(float) >= 95).all()
    assert all(m in emails and m != e for m, e in zip(rows["Most Similar Candidate"], rows["Email"]))
    assert (rows["Plagiarism Status"] == "Suspicious").sum() == 6
    assert _span_sec(_times(rows, "Attempt Start Time")) <= 180
    assert _span_sec(_times(rows, "Attempt End Time")) <= 90
    durations = _durations_sec(rows)
    assert max(durations) - min(durations) <= 120
    assert rows["Out of Window Events"].astype(int).between(6, 12).all()
    assert rows["Editor Paste Count"].astype(int).between(4, 9).all()
    repo = ring["details"]["github_repo"]
    files = resume_map[resume_map["candidate_id"].isin(ring["members"]) & (resume_map["kind"] == "primary")]
    assert len(files) == len(rows)
    for name in files["file_name"]:
        text = _flat(_resume_text(out / "resumes" / name))
        assert repo in text and "Smart Campus Event Management Platform" in text


def test_proxy_test_taker(ta: pd.DataFrame, clusters: dict[str, dict]) -> None:
    rows = _members(ta, clusters["proxy_01"]).sort_values("Attempt Start Time")
    assert rows["College"].nunique() == 6 and rows["IP Address"].nunique() == 1
    starts, ends = _times(rows, "Attempt Start Time"), _times(rows, "Attempt End Time")
    gaps = [(s - e).total_seconds() / 60 for e, s in zip(ends, starts[1:])]
    assert all(5 <= g <= 15 for g in gaps)
    assert all(18 * 60 <= d <= 26 * 60 for d in _durations_sec(rows))
    assert rows["Q3 Code Hash"].nunique() == 1 and rows["Q4 Code Hash"].nunique() == 1
    assert rows["Q2 Code Hash"].nunique() == len(rows)
    assert (rows[["Q3 Code Similarity %", "Q4 Code Similarity %"]].astype(float) >= 90).all().all()
    assert (rows["Integrity Status"] == "Review").all()
    assert (rows["Proctoring Flags"] == "Face mismatch with profile photo").sum() == 4


def test_resume_mill(ta: pd.DataFrame, clusters: dict[str, dict], out: Path, resume_map: pd.DataFrame) -> None:
    mill = clusters["mill_01"]
    rows = _members(ta, mill)
    assert rows["College"].nunique() == mill["size"] >= 3
    assert {gen.COLLEGE_BY_NAME[c].city for c in rows["College"]} == {gen.MILL_CITY}
    assert set(rows["City"]) == {gen.MILL_CITY} and set(rows["Source"]) == {"Referral"}
    for col in ("Q2 Code Hash", "Q3 Code Hash", "Q4 Code Hash"):
        assert all((ta[col] == h).sum() == 1 for h in rows[col])           # unique, normal-looking
    assert (rows["Plagiarism Status"] == "Not Suspicious").all() and (rows["Most Similar Candidate"] == "").all()
    assert (rows["Max Code Similarity %"].astype(float) < 70).all()
    files = resume_map[resume_map["candidate_id"].isin(mill["members"]) & (resume_map["kind"] == "primary")]
    for name in files["file_name"]:
        text = _flat(_resume_text(out / "resumes" / name))
        assert all(repo in text for repo in mill["details"]["github_repos"])
        assert all(bullet in text for bullet in mill["details"]["shared_bullets"])


def test_mill_shares_exactly_three_bullets(dataset: gen.Dataset) -> None:
    mill = [f.content for f in dataset.resumes
            if f.kind == "primary" and f.content and f.candidate_id in
            next(c for c in dataset.clusters if c["key"] == "mill_01")["members"]]
    for a, b in itertools.combinations(mill, 2):
        assert len(set(a.all_bullets()) & set(b.all_bullets())) == 3


def test_synchronized_hostel(ta: pd.DataFrame, clusters: dict[str, dict]) -> None:
    hostel = clusters["hostel_01"]
    rows = _members(ta, hostel)
    assert rows["College"].nunique() == 1 and rows["IP Address"].nunique() == 10
    assert rows["IP Address"].str.rsplit(".", n=1).str[0].nunique() == 1
    assert _span_sec(_times(rows, "Attempt Start Time")) <= 120
    assert _span_sec(_times(rows, "Attempt End Time")) <= 120
    q4 = rows.set_index("Candidate ID")["Q4 Code Hash"]
    for group in hostel["details"]["subgroups"]:
        assert len(group) == 5 and q4[group].nunique() == 1
    assert q4.nunique() == 2
    assert rows["Editor Paste Count"].astype(int).between(5, 10).all()
    assert rows["Out of Window Events"].astype(int).between(5, 9).all()


def test_leaked_paper_speedrun(ta: pd.DataFrame, clusters: dict[str, dict]) -> None:
    rows = _members(ta, clusters["speedrun_01"])
    assert rows["College"].nunique() == 8 and rows["City"].nunique() == 8
    assert all(9 * 60 <= d <= 15 * 60 for d in _durations_sec(rows))
    starts, ends = _times(rows, "Attempt Start Time"), _times(rows, "Attempt End Time")
    assert all(s >= datetime(2026, 11, 3, 21, 0) for s in starts) and _span_sec(starts) <= 2 * 3600
    assert all(e <= datetime(2026, 11, 3, 23, 0) for e in ends)
    for col in ("Q2 Code Hash", "Q3 Code Hash", "Q4 Code Hash"):
        assert rows[col].nunique() == 1
    assert (rows["Max Code Similarity %"] == "100").all()
    assert (rows["Plagiarism Status"] == "Suspicious").sum() == 5


def test_identity_overlap(ta: pd.DataFrame, clusters: dict[str, dict]) -> None:
    rows = _members(ta, clusters["identity_01"])
    assert rows["Phone Number"].str.replace(r"\D", "", regex=True).str[-10:].nunique() == 1
    assert rows["Full Name"].nunique() == 3 and rows["Email"].nunique() == 3
    assert sorted(Counter(rows["IP Address"]).values()) == [1, 2]


# ---------------------------------------------------------------------------
# Decoys and traps
# ---------------------------------------------------------------------------


def test_oncampus_lab_decoys(ta: pd.DataFrame, clusters: dict[str, dict], labels: pd.DataFrame) -> None:
    lab = clusters["decoy_lab_01"]
    rows = _members(ta, lab)
    assert lab["size"] == gen.DECOY_SIZE and lab["must_not_exceed"] == "Medium"
    assert set(rows["College"]) == {gen.DECOY_COLLEGE} and set(rows["Test Mode"]) == {"On-campus"}
    assert (rows["Score"] == rows["Max Score"]).all()
    ip = rows["IP Address"].iloc[0]
    assert rows["IP Address"].nunique() == 1
    sharing = ta[ta["IP Address"] == ip]
    assert len(sharing) == lab["details"]["lab_size"] >= 20 and set(sharing["College"]) == {gen.DECOY_COLLEGE}
    session = datetime(2026, 11, 2, 9, 30)
    assert all(0 <= (s - session).total_seconds() <= 150 for s in _times(rows, "Attempt Start Time"))
    assert all(45 * 60 <= d <= 85 * 60 for d in _durations_sec(rows))
    for col in ("Q2 Code Hash", "Q3 Code Hash", "Q4 Code Hash"):
        assert all((ta[col] == h).sum() == 1 for h in rows[col])
    sims = rows[["Q2 Code Similarity %", "Q3 Code Similarity %", "Q4 Code Similarity %"]]
    assert (sims.astype(float) < 50).all().all()
    assert set(labels.set_index("candidate_id").loc[lab["members"], "label"]) == {"decoy"}


def test_mcq_answer_key_is_shared(ta: pd.DataFrame, issues: dict, clusters: dict[str, dict]) -> None:
    done = ta[ta["Status"] == "Completed"]
    q1_max = done["Max Score"].map({"100": "20", "150": "30"})
    full = done["Q1 Score (MCQ - CS Fundamentals)"] == q1_max
    assert set(done.loc[full, "Q1 Answer Hash"]) == {issues["mcq_answer_key_hash"]}
    assert issues["mcq_answer_key_hash"] not in set(done.loc[~full, "Q1 Answer Hash"])
    assert clusters["decoy_mcq_key_01"]["size"] == done.loc[full, "Candidate ID"].nunique()


def test_boilerplate_decoys(dataset: gen.Dataset, labels: pd.DataFrame) -> None:
    boiler = labels[labels["cluster_key"] == "decoy_boilerplate_01"]
    resumes = [f for f in dataset.resumes if f.content is not None]
    holders = [f for f in resumes if set(f.content.all_bullets()) & set(gen.BOILERPLATE_BULLETS)]
    assert set(boiler["label"]) == {"decoy"}
    assert {f.candidate_id for f in holders if f.kind == "primary"} == set(boiler["candidate_id"])
    assert 0.04 <= len(holders) / len(resumes) <= 0.13
    for bullet in gen.BOILERPLATE_BULLETS:     # common enough to be filtered as boilerplate (> 2%)
        share = sum(bullet in f.content.all_bullets() for f in resumes) / len(resumes)
        assert share > RiskConfig().max_bullet_population_share


def test_only_planted_or_boilerplate_bullets_are_shared(dataset: gen.Dataset) -> None:
    """Honest resumes never share a bullet by chance (that would be false 'rare bullet' evidence)."""
    pattern = {c.cid: c.pattern for c in dataset.candidates}
    allowed = {gen._bullet_key(b) for b in gen.BOILERPLATE_BULLETS + gen.MILL_SHARED_BULLETS}
    holders: dict[str, set[str]] = {}
    min_words = RiskConfig().min_bullet_words
    for f in dataset.resumes:
        if f.content is not None and f.kind != "v2":
            for b in f.content.all_bullets() + f.content.achievements:
                holders.setdefault(gen._bullet_key(b), set()).add(f.candidate_id or f.file_name)
            # certifications are legitimately shared, so they stay below the bullet-evidence length
            assert all(len(gen._bullet_key(x).split()) < min_words for x in f.content.certifications)
    for key, who in holders.items():
        if len(who) > 1 and key not in allowed:
            assert {pattern.get(w) for w in who} == {gen.RING}, (key, sorted(who))


def test_honest_perfect_scorers_stay_quiet(dataset: gen.Dataset) -> None:
    """Honest 100% scorers must not carry evidence that a sane engine would turn into High."""
    rc = RiskConfig()
    focus = [c for c in dataset.candidates if c.in_focus]
    honest = [c for c in focus if c.label != "colluder"]
    assert len(honest) >= gen.DECOY_SIZE + 5
    for c in honest:
        assert (c.max_sim or 0) < 70 and c.most_similar == ""
        assert gen._individual_points(c, rc) < gen.LEGIT_INDIVIDUAL_CAP
    for a, b in itertools.combinations(focus, 2):
        if a.label == b.label == "colluder" and a.pattern == b.pattern:
            continue
        if a.mode == b.mode == "On-campus" and a.college == b.college:
            continue
        double_sync = (abs((a.start - b.start).total_seconds()) <= rc.sync_window_sec
                       and abs((a.end - b.end).total_seconds()) <= rc.sync_window_sec)
        assert not double_sync, (a.cid, b.cid)
        assert a.ip != b.ip, (a.cid, b.cid)
    texts = {f.candidate_id: f.content.section_text("projects") + "\n" + f.content.section_text("experience")
             for f in dataset.resumes if f.kind == "primary" and f.content}
    honest_docs = [texts[c.cid] for c in honest if c.cid in texts]
    sims = cosine_similarity(TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True).fit_transform(honest_docs))
    assert sims[np.triu_indices_from(sims, k=1)].max() < rc.text_similarity_medium


# ---------------------------------------------------------------------------
# Privacy: reserved e-mail domains only
# ---------------------------------------------------------------------------


def test_emails_use_reserved_domains_only(out: Path) -> None:
    texts = [(out / "ta_feed.csv").read_text(encoding="utf-8")]
    texts += [p.read_text(encoding="utf-8") for p in out.glob("*.txt")] + [(out / "README.md").read_text(encoding="utf-8")]
    texts += [p.read_text(encoding="utf-8") for p in (out / "_ground_truth").iterdir()]
    for path in (out / "resumes").iterdir():
        if path.stat().st_size and not path.name.endswith("_scan.pdf"):
            try:
                texts.append(_resume_text(path))
            except Exception:       # the deliberately corrupt PDF
                assert path.suffix == ".pdf"
    domains = Counter(m.group(1).lower() for t in texts for m in EMAIL_RE.finditer(t))
    assert domains and set(domains) <= RESERVED, set(domains) - RESERVED


def test_phones_are_synthetic(ta: pd.DataFrame) -> None:
    digits = ta["Phone Number"].str.replace(r"\D", "", regex=True).str[-10:]
    assert digits.str.fullmatch(r"9\d{9}").all()


def test_most_similar_candidate_references(ta: pd.DataFrame) -> None:
    named = ta[ta["Most Similar Candidate"] != ""]
    assert len(named) > 0
    assert (named["Max Code Similarity %"].astype(float) >= 70).all()
    assert named["Most Similar Candidate"].isin(set(ta["Email"])).all()
    assert (named["Most Similar Candidate"] != named["Email"]).all()
    unnamed = ta[(ta["Most Similar Candidate"] == "") & (ta["Max Code Similarity %"] != "")]
    assert (unnamed["Max Code Similarity %"].astype(float) < 70).all()


# ---------------------------------------------------------------------------
# Resume files
# ---------------------------------------------------------------------------


def test_resume_formats_and_special_files(out: Path, resume_map: pd.DataFrame, ta: pd.DataFrame, issues: dict) -> None:
    folder = out / "resumes"
    by_kind = resume_map.groupby("kind")["file_name"].apply(list).to_dict()
    assert len(by_kind["v2"]) == gen.N_V2 and all("_v2." in n for n in by_kind["v2"])
    assert (folder / by_kind["empty_docx"][0]).stat().st_size == 0
    corrupt = folder / by_kind["corrupt_pdf"][0]
    try:
        text = "".join(p.extract_text() or "" for p in pypdf.PdfReader(corrupt).pages)
    except Exception:
        text = ""
    assert text.strip() == ""
    image = pypdf.PdfReader(folder / by_kind["image_only_pdf"][0])
    assert len(image.pages) == 1 and not (image.pages[0].extract_text() or "").strip()
    walk_ins = set(by_kind["walk_in"])
    assert walk_ins == set(issues["walk_in_resumes"])
    ta_emails = set(ta["Email"])
    for name in walk_ins:
        found = EMAIL_RE.search(_resume_text(folder / name))
        assert found and found.group(0) not in ta_emails

    prim = resume_map[resume_map["kind"] == "primary"]
    styles = prim["file_name"].map(lambda n: "id" if n.startswith("CAND-") else "generic" if n.startswith("Resume (")
                                   else "name" if "_Resume." in n else "cv" if "_cv." in n else "?")
    assert set(styles) == {"id", "generic", "name", "cv"}
    assert styles.value_counts(normalize=True)["id"] > 0.35
    assert set(prim["format"]) == {"pdf", "docx", "txt"} and set(prim["layout"]) == set(gen.LAYOUTS)

    emails = dict(zip(ta["Candidate ID"], ta["Email"]))
    pdf_row = prim[(prim["format"] == "pdf") & (prim["layout"] == "classic")].iloc[0]
    pdf_text = _resume_text(folder / pdf_row["file_name"])
    assert emails[pdf_row["candidate_id"]] in pdf_text and "•" in pdf_text and "CGPA" in pdf_text
    docx_row = prim[(prim["format"] == "docx") & (prim["layout"] == "modern")].iloc[0]
    document = docx.Document(str(folder / docx_row["file_name"]))
    style_names = {p.style.name for p in document.paragraphs}
    assert {"Title", "List Bullet"} <= style_names and any(s.startswith("Heading") for s in style_names)
    assert document.core_properties.comments.startswith("Synthetic")


# ---------------------------------------------------------------------------
# Data-quality injections
# ---------------------------------------------------------------------------


def test_injected_data_quality_issues(ta: pd.DataFrame, issues: dict) -> None:
    dup_ids = ta["Candidate ID"][ta["Candidate ID"].duplicated()]
    assert sorted(dup_ids) == [d["candidate_id"] for d in issues["duplicate_rows"]]
    for d in issues["duplicate_rows"]:
        rows = ta[ta["Candidate ID"] == d["candidate_id"]]
        ends = sorted(pd.to_datetime(rows["Attempt End Time"], format="mixed"))
        assert ends[0] < ends[1] and ends[1] == pd.Timestamp(d["kept_end_time"])

    assert sorted(ta.loc[ta["Email"] == "", "Candidate ID"]) == issues["missing_emails"]
    over = ta[pd.to_numeric(ta["Score"], errors="coerce") > ta["Max Score"].astype(float)]
    assert sorted(over["Candidate ID"]) == sorted(r["candidate_id"] for r in issues["scores_above_max"])
    blank = ta.loc[ta["Score"] == "", "Candidate ID"]
    assert sorted(blank) == sorted(r["candidate_id"] for r in issues["blank_scores"])

    first = ta.drop_duplicates("Candidate ID").set_index("Candidate ID")
    patterns = {"%d-%b-%Y %H:%M:%S": r"\d{2}-[A-Z][a-z]{2}-2026 \d{2}:\d{2}:\d{2}",
                "%Y-%m-%dT%H:%M:%S": r"2026-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"}
    assert 0.03 * N <= len(issues["alt_datetime_formats"]) <= 0.07 * N
    for rec in issues["alt_datetime_formats"]:
        assert re.fullmatch(patterns[rec["format"]], first.loc[rec["candidate_id"], "Attempt Start Time"])
    for rec in issues["time_taken_hhmmss"]:
        assert re.fullmatch(r"\d{2}:\d{2}:\d{2}", first.loc[rec["candidate_id"], "Time Taken (mins)"])
    for rec in issues["cgpa_as_percentage"]:
        assert float(first.loc[rec["candidate_id"], "CGPA"]) > 10
    assert issues["college_name_variants"]
    for rec in issues["college_name_variants"]:
        shown = first.loc[rec["candidate_id"], "College"]
        assert shown == rec["value"] != rec["canonical"]
        assert " ".join(shown.split()).casefold() == rec["canonical"].casefold()
    std = ta["Attempt Start Time"][ta["Attempt Start Time"] != ""]
    assert pd.to_datetime(std, format="mixed").notna().all()
