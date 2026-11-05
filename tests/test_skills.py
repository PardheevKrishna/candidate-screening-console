"""Tests for screening.skills: taxonomy integrity, extraction, canonicalisation, matching."""
from __future__ import annotations

import re
import time

import pytest

from screening import skills as K


# ---------------------------------------------------------------------------
# Taxonomy integrity
# ---------------------------------------------------------------------------
def test_taxonomy_size_and_required_canonical_names():
    assert 140 <= len(K.SKILL_TAXONOMY) <= 220
    for name in ["Python", "Java", "SQL", "Cloud", "PyTorch", "Machine Learning", "AWS", "Azure", "GCP",
                 "Spring Boot", "Docker", "Kubernetes", "React", "Node.js", "C++", "C#", ".NET", "Power BI",
                 "Git", "REST APIs", "Data Structures & Algorithms", "C", "R", "Go", "scikit-learn", "Next.js"]:
        assert name in K.SKILL_TAXONOMY, name
    assert K.CANONICAL_SKILLS == sorted(K.SKILL_TAXONOMY, key=str.lower)
    assert set(K.SKILL_CATEGORY) == set(K.SKILL_TAXONOMY)


def test_synonyms_compile_without_capturing_groups():
    for skill, patterns in K.SKILL_TAXONOMY.items():
        assert patterns, skill
        for pat in patterns:
            assert re.compile(pat).groups == 0, (skill, pat)


def test_case_insensitive_synonyms_stay_on_the_fast_path():
    # Lower-case literals that start with a plain character keep the single-pass scanner fast.
    for skill, patterns in K.SKILL_TAXONOMY.items():
        for pat in patterns:
            if pat.startswith("(?-i:"):
                continue
            assert re.sub(r"\\.", "", pat) == re.sub(r"\\.", "", pat).lower(), (skill, pat)
            assert K._split_head(pat) is not None, (skill, pat)


def test_hierarchy_references_known_skills():
    known = set(K.SKILL_TAXONOMY)
    assert set(K.SKILL_IMPLIES) <= known
    assert {p for parents in K.SKILL_IMPLIES.values() for p in parents} <= known
    assert set(K.LIST_ONLY_TOKENS) <= known
    for parent in ("Cloud",):
        assert all(parent in K.SKILL_IMPLIES[c] for c in ("AWS", "Azure", "GCP"))


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text, expected, absent", [
    ("Worked with JavaScript and TypeScript", {"JavaScript", "TypeScript"}, {"Java"}),
    ("Backend in Java, Spring Boot and MySQL", {"Java", "Spring Boot", "MySQL"}, {"JavaScript", "SQL"}),
    ("Java Script for the UI", {"JavaScript"}, {"Java"}),
    ("Languages: C, C++, C#, Python", {"C", "C++", "C#", "Python"}, set()),
    ("Strong in C++ and C# only", {"C++", "C#"}, {"C"}),
    ("Developed drivers in C/C++", {"C", "C++"}, set()),
    ("ASP.NET Core and .NET 6 services; site at example.net", {".NET"}, set()),
    ("Node.js, Next.js, Express.js, Vue.js", {"Node.js", "Next.js", "Express.js", "Vue.js"}, {"JavaScript"}),
    ("Models with scikit-learn (sklearn) and PyTorch", {"scikit-learn", "PyTorch"}, set()),
    ("Dashboards in Power BI and PowerBI", {"Power BI"}, set()),
    ("Python3, HTML5, CSS3, Java8", {"Python", "HTML", "CSS", "Java"}, set()),
    ("SQL Server, T-SQL, NoSQL, PostgreSQL", {"SQL Server", "NoSQL", "PostgreSQL"}, {"SQL"}),
    ("Deployed on AWS EC2 with Docker and k8s", {"AWS", "Docker", "Kubernetes"}, set()),
    ("Google Cloud Platform and Microsoft Azure", {"GCP", "Azure"}, {"Cloud"}),
    ("Exposure to cloud computing", {"Cloud"}, set()),
    ("Built a word cloud from point cloud data", set(), {"Cloud"}),
    ("Built REST APIs and RESTful services", {"REST APIs"}, set()),
    ("Data Structures and Algorithms, OOPs, DBMS", {"Data Structures & Algorithms", "OOP", "DBMS"}, set()),
    ("AI/ML enthusiast; bought a 500 ml bottle", {"Artificial Intelligence", "Machine Learning"}, set()),
    ("Oracle 11g database administration", {"Oracle"}, set()),
    ("React Native mobile app", {"React Native"}, {"React"}),
])
def test_extract_skills(text, expected, absent):
    found = set(K.extract_skills(text))
    assert expected <= found, found
    assert not (absent & found), found


@pytest.mark.parametrize("text", [
    "I can go the extra mile and C the big picture",
    "Worked with the R&D team on a go-to-market plan",
    "Interned at Oracle in the finance team",
    "We react quickly to customer feedback in the spring",
    "Grade: C",
])
def test_ambiguous_short_names_not_matched_in_prose(text):
    found = set(K.extract_skills(text))
    assert not found & {"C", "R", "Go", "Oracle", "React", "Spring"}, found


@pytest.mark.parametrize("text, expected", [
    ("Languages: C, Python, R", {"C", "Python", "R"}),
    ("Tech Stack - Go | Docker | Kubernetes", {"Go", "Docker", "Kubernetes"}),
    ("Python, C, Java", {"C", "Python", "Java"}),
    ("Core subjects: OS, DBMS, CN, OOPs", {"Operating Systems", "DBMS", "Computer Networks", "OOP"}),
    ("Tools: React, Node, Express", {"React", "Node.js", "Express.js"}),
    ("Golang developer", {"Go"}),
    ("R programming and RStudio", {"R"}),
])
def test_list_only_tokens_in_lists(text, expected):
    assert expected <= set(K.extract_skills(text))


def test_urls_and_emails_are_not_skill_evidence():
    assert K.extract_skills("github.com/asha/python-notes | java.dev@gmail.com") == []
    assert K.extract_skills("(https://example.dev/java) and www.python.org; SQL") == ["SQL"]


def test_long_tokens_scan_in_linear_time():
    start = time.perf_counter()
    for blob in ("x" * 200_000 + "@" + "y" * 100_000, "a-" * 150_000, "a." * 150_000):
        K.extract_skills(blob)
    assert time.perf_counter() - start < 3.0


def test_extract_skills_sorted_and_handles_empty():
    assert K.extract_skills("") == []
    assert K.extract_skills(None) == []  # type: ignore[arg-type]
    out = K.extract_skills("SQL, AWS, Python, C++")
    assert out == sorted(out, key=str.lower)


def test_skill_evidence_treats_skills_section_as_list():
    ev = K.skill_evidence({
        "skills": "C\nPython\nProficient in Go and Java",
        "projects": "Wrote it in C quickly using Python and Docker",
        "summary": "",
    })
    assert ev["C"] == ["skills"]            # bare "C" in prose does not count
    assert ev["Go"] == ["skills"]
    assert ev["Python"] == ["skills", "projects"]
    assert ev["Docker"] == ["projects"]
    assert list(ev) == sorted(ev, key=str.lower)
    assert K.skill_evidence({}) == {}


# ---------------------------------------------------------------------------
# Canonicalisation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name, expected", [
    ("python", "Python"), ("  PYTHON  ", "Python"), ("postgres", "PostgreSQL"), ("Postgre SQL", "PostgreSQL"),
    ("ml", "Machine Learning"), ("gcp", "GCP"), ("google cloud", "GCP"), ("Google Cloud Platform", "GCP"),
    ("k8s", "Kubernetes"), ("js", "JavaScript"), ("node", "Node.js"), ("nodejs", "Node.js"), ("react.js", "React"),
    ("c", "C"), ("c++", "C++"), ("cpp", "C++"), ("c#", "C#"), (".net", ".NET"), ("dotnet", ".NET"),
    ("sklearn", "scikit-learn"), ("power bi", "Power BI"), ("dsa", "Data Structures & Algorithms"),
    ("cloud computing", "Cloud"), ("AWS Cloud", "AWS"), ("Java 8", "Java"), ("Python programming", "Python"),
    ("Spring Boot (Java)", "Spring Boot"), ("rest api", "REST APIs"), ("golang", "Go"), ("ms sql server", "SQL Server"),
])
def test_canonicalize_known(name, expected):
    assert K.canonicalize_skill(name) == expected


@pytest.mark.parametrize("name, expected", [
    ("kafka streams", "Kafka Streams"),        # unknown + lowercase -> title-cased
    ("GraphQL Federation", "GraphQL Federation"),  # unknown mixed case -> kept as typed
    ("  Quantum   Annealing ", "Quantum Annealing"),
    ("", ""),
])
def test_canonicalize_unknown(name, expected):
    assert K.canonicalize_skill(name) == expected


# ---------------------------------------------------------------------------
# Required-skill matching
# ---------------------------------------------------------------------------
def test_match_required_direct_and_implied_in_order():
    required = ["Python", "Java", "SQL", "Cloud", "PyTorch"]
    matched, missing = K.match_required(required, ["Spring Boot", "MySQL", "AWS", "Pandas"])
    assert matched == ["Python", "Java", "SQL", "Cloud"]
    assert missing == ["PyTorch"]


def test_match_required_transitive_and_umbrella_direction():
    assert K.match_required(["Machine Learning"], ["Keras"]) == (["Machine Learning"], [])
    assert K.match_required(["JavaScript"], ["Next.js"]) == (["JavaScript"], [])   # Next.js -> React -> JS
    assert K.match_required(["Cloud"], ["BigQuery"]) == (["Cloud"], [])            # BigQuery -> GCP -> Cloud
    assert K.match_required(["AWS"], ["Cloud"]) == ([], ["AWS"])                  # umbrella does not imply
    assert K.match_required(["Java"], ["JavaScript"]) == ([], ["Java"])


def test_match_required_canonicalises_and_dedupes():
    matched, missing = K.match_required(["postgres", "PostgreSQL", "k8s", "ml"], ["PostgreSQL", "Kubernetes"])
    assert matched == ["PostgreSQL", "Kubernetes"]
    assert missing == ["Machine Learning"]


def test_match_required_literal_fallback_only_for_unknown_skills():
    text = "Built pipelines with Kafka   Streams and Apache Beam; some C here."
    matched, missing = K.match_required(["Kafka Streams", "Apache Beam", "Flink", "C"], [], text)
    assert matched == ["Kafka Streams", "Apache Beam"]
    assert missing == ["Flink", "C"]   # C is in the taxonomy -> no literal fallback
    assert K.match_required(["Beam"], [], "Apache Beams") == ([], ["Beam"])   # whole word only
    assert K.match_required([], ["Python"]) == ([], [])


def test_implied_skills():
    assert K.implied_skills(["MERN Stack"]) >= {"MongoDB", "Express.js", "React", "Node.js", "JavaScript", "NoSQL"}
    assert K.implied_skills([]) == set()


def test_extraction_is_fast():
    para = ("Developed a scalable REST API using Flask and PostgreSQL deployed on AWS with Docker; "
            "built ML models with scikit-learn, Pandas and PyTorch; CI/CD with GitHub Actions and Jenkins.\n")
    texts = [f"Candidate {i}\nSkills: C, C++, Java, Python, Go\n" + para * 25 for i in range(300)]
    start = time.perf_counter()
    for t in texts:
        K.extract_skills(t)
    assert time.perf_counter() - start < 3.0
