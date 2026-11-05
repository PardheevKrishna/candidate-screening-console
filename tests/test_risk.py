"""Tests for screening.risk (integrity & anomaly engine).

A hand-built population of 140 candidates (45 perfect scorers) plants one example of every
collusion pattern (solution ring, proxy test-taker, leaked-paper speedrun, identity overlap,
synchronised group, resume mill) and every legitimate look-alike (strong-college lab on one
NAT IP with a scheduled start, hostel NAT with back-to-back sessions, MCQ answer key shared by
all perfect scorers, boilerplate bullets, popular / profile-only GitHub URLs, a coincidental
single timing match). No other screening module is needed: resumes are built by hand.
"""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Any

import numpy as np
import pandas as pd
import pytest
from scipy.stats import fisher_exact

import screening.schema as S
from screening import risk
from screening.config import AppConfig
from screening.models import ParsedResume, RiskResult

BASE = pd.Timestamp("2026-10-05 08:00:00")
IITM = "Indian Institute of Technology Madras"
NIT = "National Institute of Technology Trichy"
VIT = "Vellore Institute of Technology"
COEP = "College of Engineering Pune"
PSG = "PSG College of Technology"
ANNA = "Anna University"
MIT = "Manipal Institute of Technology"
BITS = "BITS Pilani Hyderabad"
OTHER_COLLEGES = [NIT, VIT, COEP, PSG, ANNA, MIT, BITS]

LAB_IP, PROXY_IP, HOSTEL_IP, IDENTITY_IP = "14.139.160.10", "49.36.12.7", "103.88.1.1", "103.21.5.9"
BOILERPLATE = [
    "Worked collaboratively in a team environment to deliver the project on time and within scope",
    "Participated actively in coding competitions and hackathons organised by the college technical club",
    "Demonstrated strong problem solving and communication skills while working with cross functional teams",
]
MILL_BULLETS = [
    "Engineered a real time transaction scoring service that flags anomalous card payments within milliseconds",
    "Designed a feature store with rolling merchant velocity aggregates to improve the model recall",
    "Deployed the PyTorch ensemble on AWS SageMaker behind an autoscaling REST endpoint for the bank",
]
MILL_REPO = "github.com/projhub/bank-fraud-detection"
POPULAR_REPO = "github.com/tensorflow/tensorflow"
PROFILE_ONLY = "github.com/codingclub"
HASH_COLS = [S.ANSWER_HASH_TEMPLATE.format(n=q) for q in range(1, 6)]


def at(day: int, hh: int, mm: int = 0, ss: int = 0) -> pd.Timestamp:
    return pd.Timestamp(year=2026, month=10, day=day, hour=hh, minute=mm) + pd.Timedelta(seconds=ss)


def make_resume(cid: str, projects: str, experience: str, bullets: list[str],
                github: list[str] | None = None) -> ParsedResume:
    text = f"{cid}\nPROJECTS\n{projects}\nEXPERIENCE\n{experience}"
    return ParsedResume(file_name=f"{cid}.pdf", file_hash=f"hash-{cid}", text=text,
                        sections={"projects": projects, "experience": experience},
                        github_urls=list(github or []), bullets=list(bullets),
                        word_count=len(text.split()))


class Population:
    """Accumulates canonical TA rows + hand-built ParsedResume objects."""

    def __init__(self, seed: int = 11) -> None:
        self.rng = np.random.default_rng(seed)
        self.vocab = [f"tok{i:04d}" for i in range(4000)]
        self.rows: list[dict[str, Any]] = []
        self.resumes: dict[str, ParsedResume] = {}
        self.groups: dict[str, list[str]] = defaultdict(list)

    def words(self, k: int) -> str:
        return " ".join(self.rng.choice(self.vocab, k))

    def random_resume(self, cid: str, extra_bullets: list[str] = (), github: list[str] | None = None) -> ParsedResume:
        bullets = [self.words(10) for _ in range(3)] + list(extra_bullets)
        projects = self.words(45) + "\n" + "\n".join(bullets)
        return make_resume(cid, projects, self.words(25), bullets, github)

    def add(self, group: str, college: str, *, pct: float = 100.0, mode: str = "Remote",
            ip: str | None = None, start: pd.Timestamp | None = None, minutes: float | None = None,
            hashes: dict[int, str] | None = None, resume: ParsedResume | None = None,
            github: list[str] | None = None, **extra: Any) -> str:
        k = len(self.rows) + 1
        cid = f"CAND-{k:05d}"
        if start is None:
            start = BASE + pd.Timedelta(minutes=int(self.rng.integers(0, 6 * 24 * 60)))
        minutes = float(self.rng.uniform(45, 85)) if minutes is None else float(minutes)
        ip = ip or f"117.{200 + k // 256}.{k % 256}.{10 + k % 200}"
        row: dict[str, Any] = {
            S.CANDIDATE_ID: cid, S.NAME: f"Candidate {k}", S.EMAIL: f"user{k}@example.org",
            S.PHONE: f"98{k:08d}", S.COLLEGE: college, S.CITY: "Chennai" if college == IITM else "Pune",
            S.HR_SCORE: pct, S.MAX_SCORE: 100.0, S.HR_PCT: pct, S.TEST_MODE: mode, S.IP_ADDRESS: ip,
            S.TEST_START: start, S.SUBMISSION_TIME: start + pd.Timedelta(minutes=minutes),
            S.COMPLETION_MIN: minutes, S.TAB_SWITCHES: 0, S.COPY_PASTE: 0, S.PLAGIARISM_FLAG: False,
            S.OUT_OF_WINDOW_SEC: 0.0, S.SECURE_MODE_EXITS: 0, S.MAX_CODE_SIMILARITY: float(self.rng.uniform(5, 40)),
            S.INTEGRITY_STATUS: "Clear", S.PROCTORING_FLAGS: "", S.DATA_FLAGS: "",
        }
        mcq = "mcqkey1" if pct >= 100 else str(self.rng.choice(["mcqkey1", "mcqwrong2", "mcqwrong3"]))
        row[HASH_COLS[0]] = mcq
        for q in range(2, 6):
            row[HASH_COLS[q - 1]] = f"{cid.lower()}q{q}h"
        for q, h in (hashes or {}).items():
            row[HASH_COLS[q - 1]] = h
        row.update(extra)
        self.rows.append(row)
        self.resumes[cid] = resume or self.random_resume(cid, github=github)
        self.groups[group].append(cid)
        return cid

    def frame(self) -> pd.DataFrame:
        df = pd.DataFrame(self.rows)
        parts = df[S.IP_ADDRESS].str.split(".")
        df[S.IP_SUBNET] = parts.str[0] + "." + parts.str[1] + "." + parts.str[2] + ".0/24"
        df[S.EMAIL_DOMAIN] = df[S.EMAIL].str.split("@").str[1]
        df[S.TEST_SLOT] = df[S.TEST_START].dt.floor("h").dt.strftime("%Y-%m-%d %H:00")
        df[S.TAB_SWITCHES] = df[S.TAB_SWITCHES].astype("Int64")
        df[S.COPY_PASTE] = df[S.COPY_PASTE].astype("Int64")
        df[S.SECURE_MODE_EXITS] = df[S.SECURE_MODE_EXITS].astype("Int64")
        return df


def build_population() -> tuple[pd.DataFrame, dict[str, ParsedResume], dict[str, ParsedResume], dict[str, list[str]]]:
    p = Population()
    boiler = lambda i: [BOILERPLATE[i % len(BOILERPLATE)]]  # noqa: E731

    # Decoy: strong college, 12 perfect scorers + 2 others in one On-campus lab (NAT IP, 10:00 start).
    # Members 0/1 also submit 23 s apart with near-equal durations: coincidental, scheduled timing.
    lab_minutes = [60.0, 60.3, 52, 64, 67, 70, 73, 76, 79, 55, 81, 66.6]
    for j in range(14):
        focus = j < 12
        cid = p.add("lab" if focus else "lab_other", IITM, pct=100.0 if focus else 88.0, mode="On-campus",
                    ip=LAB_IP, start=at(5, 10, 0, 5 * j), minutes=lab_minutes[j] if focus else 70.0 + j)
        if focus:
            p.resumes[cid] = p.random_resume(cid, extra_bullets=boiler(0), github=[POPULAR_REPO])

    # Ring sharing solutions to q2, q3 (different colleges, IPs and times).
    ring_times = [(9, 10), (11, 45), (14, 20), (17, 5)]
    for college, (hh, mm), minutes in zip([NIT, VIT, COEP, PSG], ring_times, [55, 62, 58, 66]):
        p.add("ring", college, start=at(6, hh, mm), minutes=minutes, hashes={2: "ringq2solution", 3: "ringq3solution"})

    # Proxy test-taker: one IP, back-to-back sessions (gaps 7 and 5 min), identical q4.
    for college, (hh, mm), minutes in zip([ANNA, MIT, BITS], [(9, 0), (9, 47), (10, 30)], [40, 38, 40]):
        p.add("proxy", college, ip=PROXY_IP, start=at(7, hh, mm), minutes=minutes, hashes={4: "proxyq4answer"})

    # Leaked-paper speedrun: ~11 min vs ~60 min median, identical q2 and q5.
    for college, (hh, mm), minutes in zip([VIT, ANNA, PSG], [(8, 15), (12, 40), (19, 5)], [10.5, 11.2, 12.0]):
        p.add("speedrun", college, start=at(8, hh, mm), minutes=minutes, hashes={2: "leakq2", 5: "leakq5"})

    # Identity overlap: two IDs, same phone and IP, copied resume, different days.
    i1 = p.add("identity", COEP, ip=IDENTITY_IP, start=at(9, 9, 0), minutes=58, phone="9876501234")
    i2 = p.add("identity", COEP, ip=IDENTITY_IP, start=at(10, 15, 30), minutes=61, phone="9876501234")
    src = p.resumes[i1]
    p.resumes[i2] = make_resume(i2, src.sections["projects"], src.sections["experience"], src.bullets)

    # Synchronised group: starts within 40 s, submissions within ~50 s; HackerRank names each other.
    sync_ids = [f"CAND-{len(p.rows) + j + 1:05d}" for j in range(4)]
    for j, (college, secs, minutes, sim) in enumerate(zip([NIT, MIT, BITS, PSG], [0, 15, 30, 40],
                                                          [52.0, 52.3, 51.8, 52.1], [91.0, 88.0, 93.0, 90.0])):
        p.add("sync", college, start=at(8, 15, 7, secs), minutes=minutes,
              **{S.MAX_CODE_SIMILARITY: sim, risk.PLAGIARISM_MATCH_ID: sync_ids[(j + 1) % 4],
                 S.PLAGIARISM_MATCH: sync_ids[(j + 1) % 4]})

    # Resume mill: shared repo + three rare bullets + templated project text.
    template = p.words(60)
    for college, hh in zip([NIT, VIT, ANNA, MIT], [8, 11, 14, 17]):
        cid = p.add("mill", college, start=at(9, hh, 30))
        projects = template + " " + p.words(4) + "\n" + "\n".join(MILL_BULLETS)
        p.resumes[cid] = make_resume(cid, projects, p.words(6), MILL_BULLETS + [p.words(10)], [MILL_REPO])

    # Decoy: coincidental single timing match (starts 30 s apart, nothing else in common).
    p.add("timing_decoy", PSG, start=at(10, 13, 0, 0), minutes=40)
    p.add("timing_decoy", BITS, start=at(10, 13, 0, 30), minutes=75)
    # Decoy: an isolated pair that happens to start AND submit together (no group, no other evidence).
    p.add("sync_pair_decoy", ANNA, start=at(10, 18, 20, 0), minutes=47.0)
    p.add("sync_pair_decoy", NIT, start=at(10, 18, 20, 20), minutes=47.2)
    # Decoy: Remote outsider who starts inside the lab's scheduled window and submits 4 s after a
    # lab member with a near-equal duration (crowded session).
    p.add("lab_outsider", PSG, start=at(5, 10, 0, 20), minutes=51.9)

    # Decoy: hostel NAT shared by 7 students of one college, back-to-back Remote sessions.
    for j in range(7):
        focus = j % 2 == 0
        cid = p.add("hostel" if focus else "hostel_other", VIT, pct=100.0 if focus else 85.0, ip=HOSTEL_IP,
                    start=at(6, 8) + pd.Timedelta(minutes=60 * j), minutes=50)
        if focus:
            p.resumes[cid] = p.random_resume(cid, github=[PROFILE_ONLY])

    # Normal perfect scorers; the first has a non-focus resume twin (compare_against_all test).
    for college in [COEP, ANNA, MIT, NIT]:
        p.add("normal", college, github=[PROFILE_ONLY] if college == COEP else None)
    n1 = p.groups["normal"][0]
    x1 = p.add("twin", PSG, pct=72.0)
    src = p.resumes[n1]
    p.resumes[x1] = make_resume(x1, src.sections["projects"], src.sections["experience"], src.bullets)

    # Individual-only flags among non-focus candidates.
    p.add("flag_tabs", NIT, pct=81.0, **{S.TAB_SWITCHES: 9, S.COPY_PASTE: 4})
    p.add("flag_proctor", ANNA, pct=77.0,
          **{S.INTEGRITY_STATUS: "Suspicious", S.PROCTORING_FLAGS: "Multiple faces detected"})
    p.add("flag_plag", MIT, pct=69.0, **{S.PLAGIARISM_FLAG: True, S.MAX_CODE_SIMILARITY: 96.0})
    p.add("flag_clear", BITS, pct=73.0, **{S.INTEGRITY_STATUS: "Not Suspicious", S.PROCTORING_FLAGS: "None"})

    # Filler population (non-focus), some with boilerplate bullets / the popular repo.
    while len(p.rows) < 140:
        k = len(p.rows)
        cid = p.add("filler", OTHER_COLLEGES[k % len(OTHER_COLLEGES)], pct=float(p.rng.uniform(35, 99)),
                    mode="On-campus" if k % 4 == 0 else "Remote")
        p.resumes[cid] = p.random_resume(cid, extra_bullets=boiler(k // 3) if k % 3 == 0 else [],
                                         github=[POPULAR_REPO] if k % 11 == 0 else None)

    df = p.frame()
    parsed_by_candidate = dict(p.resumes)
    parsed_all = {r.file_hash: r for r in parsed_by_candidate.values()}
    for j in range(2):                                   # unmatched resumes still count for rarity
        extra = p.random_resume(f"UNMATCHED-{j}", extra_bullets=[BOILERPLATE[1]])
        parsed_all[extra.file_hash] = extra
    return df, parsed_by_candidate, parsed_all, dict(p.groups)


@pytest.fixture(scope="module")
def population() -> tuple[pd.DataFrame, dict[str, ParsedResume], dict[str, ParsedResume], dict[str, list[str]]]:
    return build_population()


@pytest.fixture(scope="module")
def result(population) -> RiskResult:
    df, by_cand, parsed_all, _ = population
    return risk.run_risk_analysis(df, by_cand, AppConfig(), parsed_all)


def levels(res: RiskResult, ids: list[str]) -> dict[str, str]:
    cr = res.candidate_risk.set_index(S.CANDIDATE_ID)
    return {i: cr.at[i, S.RISK_LEVEL] for i in ids}


def row(res: RiskResult, cid: str) -> pd.Series:
    cr = res.candidate_risk.set_index(S.CANDIDATE_ID)
    return cr.loc[cid]


def no_internal_errors(res: RiskResult) -> None:
    assert not [w for w in res.warnings if "internal error" in w], res.warnings


# ---------------------------------------------------------------------------
# Recall: planted patterns
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("group", ["ring", "proxy", "speedrun", "identity", "sync"])
def test_planted_patterns_are_high_and_form_one_cluster(result, population, group):
    ids = population[3][group]
    assert set(levels(result, ids).values()) == {S.RISK_HIGH}, levels(result, ids)
    clusters = {row(result, i)[S.CLUSTER_ID] for i in ids}
    assert len(clusters) == 1 and "" not in clusters
    cl = result.clusters.set_index("cluster_id").loc[clusters.pop()]
    assert set(cl["members"].split(S.SEP_LIST)) == set(ids)
    assert cl["summary"].startswith("Pattern consistent with")
    no_internal_errors(result)


def test_resume_mill_is_at_least_medium_and_clustered(result, population):
    ids = population[3]["mill"]
    assert set(levels(result, ids).values()) <= {S.RISK_HIGH, S.RISK_MEDIUM}
    clusters = {row(result, i)[S.CLUSTER_ID] for i in ids}
    assert len(clusters) == 1 and "" not in clusters
    cl = result.clusters.set_index("cluster_id").loc[clusters.pop()]
    assert set(cl["members"].split(S.SEP_LIST)) == set(ids)
    assert MILL_REPO in cl["shared_traits"]
    pair = result.pairs[result.pairs[risk.CAND_A].isin(ids) & result.pairs[risk.CAND_B].isin(ids)].iloc[0]
    assert pair["shared_bullets"] == 3 and pair["shared_github"] == MILL_REPO


def test_cluster_patterns_are_named(result, population):
    groups = population[3]
    expected = {"proxy": "proxy test-taking", "identity": "identity overlap", "speedrun": "prior access",
                "ring": "shared solutions", "sync": "coordinated test-taking", "mill": "templated resumes"}
    for group, phrase in expected.items():
        cid = row(result, groups[group][0])[S.CLUSTER_ID]
        summary = result.clusters.set_index("cluster_id").at[cid, "summary"]
        assert phrase in summary, (group, summary)


# ---------------------------------------------------------------------------
# Precision: legitimate look-alikes
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("group", ["lab", "lab_other", "lab_outsider", "hostel", "hostel_other",
                                   "timing_decoy", "sync_pair_decoy", "normal"])
def test_decoys_stay_clear_or_low(result, population, group):
    lv = levels(result, population[3][group])
    assert set(lv.values()) <= {S.RISK_CLEAR, S.RISK_LOW}, lv
    cr = result.candidate_risk.set_index(S.CANDIDATE_ID).loc[population[3][group]]
    assert (cr[S.MAX_PAIR_SCORE] < AppConfig().risk.edge_threshold).all()


def test_lab_nat_ip_and_scheduled_start_create_no_pairs(result, population):
    lab = set(population[3]["lab"])
    pairs = result.pairs
    assert not (pairs[risk.CAND_A].isin(lab) & pairs[risk.CAND_B].isin(lab)).any()
    hostel = set(population[3]["hostel"])
    assert not (pairs[risk.CAND_A].isin(hostel) & pairs[risk.CAND_B].isin(hostel)).any()


def test_common_mcq_hash_boilerplate_and_popular_repos_are_ignored(result):
    pairs = result.pairs
    assert not pairs["shared_hashes"].str.contains(r"\bq1\b").any()
    assert result.params["common_hashes_ignored"] >= 1
    assert not pairs["shared_bullet_examples"].str.contains("collaboratively|hackathons", regex=True).any()
    assert not pairs["shared_github"].str.contains("tensorflow|codingclub", regex=True).any()
    assert result.params["boilerplate_bullets_ignored"] >= 2
    assert result.params["popular_repos_ignored"] >= 1


def test_single_timing_match_never_makes_a_pair(result, population):
    for group in ("timing_decoy", "sync_pair_decoy"):
        a, b = population[3][group]
        assert not ((result.pairs[risk.CAND_A] == a) & (result.pairs[risk.CAND_B] == b)).any()
    timing_only = result.pairs["signal_keys"].map(
        lambda ks: set(ks.split(S.SEP_LIST)) <= risk.TIMING_SIGNALS)
    assert (result.pairs.loc[timing_only, "pair_score"] >= AppConfig().risk.edge_threshold).all()
    sync = set(population[3]["sync"])
    assert set(result.pairs.loc[timing_only, risk.CAND_A]) | set(result.pairs.loc[timing_only, risk.CAND_B]) <= sync


def _timing_frame(starts_min: list[float], durations: list[float], mode: str = "Remote") -> pd.DataFrame:
    n = len(starts_min)
    start = [at(12, 14) + pd.Timedelta(minutes=s) for s in starts_min]
    return pd.DataFrame({
        S.CANDIDATE_ID: [f"T-{i:03d}" for i in range(n)], S.HR_PCT: 100.0,
        S.COLLEGE: [f"College {i}" for i in range(n)], S.TEST_MODE: mode,
        S.TEST_START: start, S.COMPLETION_MIN: durations,
    })


def test_timing_needs_a_synchronised_group_outside_crowds():
    group3 = _timing_frame([0, 1 / 3, 2 / 3], [50.0, 50.1, 49.9])
    res = risk.run_risk_analysis(group3, {}, AppConfig())
    assert len(res.pairs) == 3 and (res.pairs["pair_score"] == 30.0).all()
    assert set(res.candidate_risk[S.RISK_LEVEL]) == {S.RISK_MEDIUM} and len(res.clusters) == 1
    assert "Synchronised test sessions with" in res.candidate_risk[S.RISK_REASONS].iloc[0]

    pair_only = group3.iloc[:2]                              # isolated co-moving pair: coincidence
    res = risk.run_risk_analysis(pair_only, {}, AppConfig())
    assert res.pairs.empty and set(res.candidate_risk[S.RISK_LEVEL]) == {S.RISK_CLEAR}

    crowd = _timing_frame([0.5 + 0.1 * k for k in range(10)], [20, 25, 30, 35, 40, 60, 65, 70, 75, 80])
    crowded = pd.concat([group3, crowd.assign(**{S.CANDIDATE_ID: [f"U-{i:03d}" for i in range(10)]})],
                        ignore_index=True)
    res = risk.run_risk_analysis(crowded, {}, AppConfig())   # scheduled-start crowd: down-weighted
    assert res.pairs.empty and set(res.candidate_risk[S.RISK_LEVEL]) == {S.RISK_CLEAR}
    no_internal_errors(res)

    on_campus = _timing_frame([0, 1 / 3, 2 / 3], [50.0, 50.1, 49.9], mode="On-campus")
    on_campus = on_campus.assign(**{S.COLLEGE: "One College"})
    res = risk.run_risk_analysis(on_campus, {}, AppConfig())  # scheduled On-campus session, same college
    assert res.pairs.empty


# ---------------------------------------------------------------------------
# Explainability
# ---------------------------------------------------------------------------
def test_reasons_name_evidence_and_linked_ids(result, population):
    g = population[3]
    ring_reason = row(result, g["ring"][0])[S.RISK_REASONS]
    assert "Identical answers to q2, q3 with " in ring_reason
    assert all(other in ring_reason for other in g["ring"][1:])

    p1, p2, _ = g["proxy"]
    assert f"Same IP {PROXY_IP} as " in row(result, p1)[S.RISK_REASONS]
    assert f"sessions back-to-back with {p2} (gap 7 min)" in row(result, p1)[S.RISK_REASONS]
    assert "proxy test-taker" in row(result, p2)[S.RISK_REASONS]

    speed = row(result, g["speedrun"][0])[S.RISK_REASONS]
    assert "Completed in 10.5 min vs focus-group median" in speed
    assert "Identical answers to q2, q5 with" in speed

    sync = row(result, g["sync"][0])[S.RISK_REASONS]
    assert f"HackerRank plagiarism engine: 91% similar to {g['sync'][1]}" in sync
    assert "Synchronised test sessions with" in sync

    ident = row(result, g["identity"][0])[S.RISK_REASONS]
    assert f"Same phone number as {g['identity'][1]}" in ident
    assert "9876501234" not in ident                       # no raw phone numbers in reasons

    reasons = result.candidate_risk[S.RISK_REASONS]
    assert not reasons.str.contains(r"\bcheat|\bguilty|\bfraudster", case=False, regex=True).any()
    assert (result.clusters["summary"].str.startswith("Pattern consistent with")).all()


def test_individual_flags(result, population):
    g = population[3]
    ind = result.individual
    assert list(ind.columns) == risk.INDIVIDUAL_COLUMNS
    keys = lambda cid: set(ind.loc[ind[S.CANDIDATE_ID] == cid, "flag_key"])  # noqa: E731
    assert keys(g["flag_tabs"][0]) == {"tab_switches", "copy_paste"}
    assert keys(g["flag_proctor"][0]) == {"integrity_status", "proctoring_flags"}
    assert keys(g["flag_plag"][0]) == {"plagiarism_flag", "high_code_similarity"}
    assert keys(g["flag_clear"][0]) == set()
    assert keys(g["speedrun"][0]) == {"speed_anomaly"}
    assert row(result, g["flag_tabs"][0])[S.RISK_LEVEL] == S.RISK_LOW
    assert row(result, g["flag_plag"][0])[S.INDIVIDUAL_SCORE] == 50.0
    assert row(result, g["flag_plag"][0])[S.RISK_LEVEL] == S.RISK_MEDIUM
    assert result.params["focus_median_minutes"] > 40


# ---------------------------------------------------------------------------
# Commonality (lift)
# ---------------------------------------------------------------------------
def test_lift_flags_overrepresented_college(result):
    com = result.commonality
    assert list(com.columns) == risk.COMMONALITY_COLUMNS
    iitm = com[(com["attribute"] == "College") & (com["value"] == IITM)].iloc[0]
    assert bool(iitm["significant"]) and iitm["q_value"] <= 0.05 and iitm["lift"] >= 2
    counts = ("focus_count", "focus_total", "population_count", "population_total")
    assert tuple(int(iitm[c]) for c in counts) == (12, 45, 14, 140)
    a, b = int(iitm["focus_count"]), int(iitm["focus_total"] - iitm["focus_count"])
    c = int(iitm["population_count"] - iitm["focus_count"])
    d = int(iitm["population_total"] - iitm["focus_total"]) - c
    assert iitm["p_value"] == pytest.approx(fisher_exact([[a, b], [c, d]], alternative="greater").pvalue, rel=1e-9)
    assert bool(com["significant"].iloc[0])                     # significant rows first
    assert not com.loc[com["attribute"] == "College"].query("value != @IITM")["significant"].any()


# ---------------------------------------------------------------------------
# Output contract
# ---------------------------------------------------------------------------
def test_output_schema_and_invariants(result, population):
    df = population[0]
    cr, pairs, clusters = result.candidate_risk, result.pairs, result.clusters
    assert list(cr.columns) == risk.CANDIDATE_RISK_COLUMNS
    assert len(cr) == len(df) and set(cr[S.CANDIDATE_ID]) == set(df[S.CANDIDATE_ID])
    assert int(cr[S.IS_FOCUS].sum()) == 45 == len(result.focus_ids)
    assert set(cr[S.RISK_LEVEL]) <= set(S.RISK_ORDER)
    assert (cr[S.RISK_SCORE] == np.minimum(100, cr[S.MAX_PAIR_SCORE] + cr[S.INDIVIDUAL_SCORE]).round(2)).all()
    assert list(pairs.columns) == risk.PAIR_COLUMNS
    assert (pairs[risk.CAND_A] < pairs[risk.CAND_B]).all()
    assert pairs["pair_score"].between(0, 100, inclusive="right").all()
    assert pairs["pair_score"].is_monotonic_decreasing
    assert list(clusters.columns) == risk.CLUSTER_COLUMNS
    assert list(clusters["cluster_id"]) == [f"C{i:02d}" for i in range(1, len(clusters) + 1)]
    assert clusters["max_pair_score"].is_monotonic_decreasing
    linked = cr[cr[S.CLUSTER_ID] != ""]
    assert (linked[S.N_LINKS] >= 1).all() and (linked[S.MAX_PAIR_SCORE] >= AppConfig().risk.edge_threshold).all()
    # sequential_gap_min is only filled where the back-to-back same-IP (proxy) pattern counted
    seq = pairs[pairs["sequential_gap_min"].notna()]
    assert len(seq) == 2 and seq["signal_keys"].str.contains("sequential_same_ip").all()
    assert sorted(seq["sequential_gap_min"]) == [5.0, 7.0] and seq["same_ip"].all()
    for key in ("edge_threshold", "high_threshold", "common_hash_max_holders", "rare_max_holders", "timing_crowd_max"):
        assert key in result.params


# ---------------------------------------------------------------------------
# Signals reaching outside the focus group / options
# ---------------------------------------------------------------------------
def test_compare_against_all_links_focus_resume_to_non_focus_twin(population):
    df, by_cand, parsed_all, g = population
    n1, x1 = g["normal"][0], g["twin"][0]
    cfg = AppConfig()
    cfg.risk.compare_against_all = True
    res = risk.run_risk_analysis(df, by_cand, cfg, parsed_all)
    pair = res.pairs[(res.pairs[risk.CAND_A] == n1) & (res.pairs[risk.CAND_B] == x1)]
    assert len(pair) == 1 and pair["resume_similarity"].iloc[0] >= 0.99
    assert levels(res, [n1, x1]) == {n1: S.RISK_MEDIUM, x1: S.RISK_MEDIUM}
    assert set(levels(res, g["lab"]).values()) <= {S.RISK_CLEAR, S.RISK_LOW}
    no_internal_errors(res)


def _small_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def test_identity_and_plagiarism_link_focus_to_non_focus():
    df = _small_frame([
        {S.CANDIDATE_ID: "A-001", S.HR_PCT: 100.0, S.PHONE: "9123456780", S.EMAIL: "a.b+test@gmail.com",
         S.MAX_CODE_SIMILARITY: 97.0, S.PLAGIARISM_MATCH: "c@x.org", S.PLAGIARISM_FLAG: "Suspicious"},
        {S.CANDIDATE_ID: "B-002", S.HR_PCT: 64.0, S.PHONE: "+91 91234 56780", S.EMAIL: "ab@gmail.com",
         S.PLAGIARISM_FLAG: "Not Suspicious"},
        {S.CANDIDATE_ID: "C-003", S.HR_PCT: 80.0, S.PHONE: "9000000001", S.EMAIL: "c@x.org"},
        {S.CANDIDATE_ID: "D-004", S.HR_PCT: 70.0, S.PHONE: "9999999999", S.EMAIL: "d@x.org"},
        {S.CANDIDATE_ID: "E-005", S.HR_PCT: 90.0, S.PHONE: "9999999999", S.EMAIL: "e@x.org"},
    ])
    res = risk.run_risk_analysis(df, {}, AppConfig())
    pairs = res.pairs.set_index([risk.CAND_A, risk.CAND_B])
    ab = pairs.loc[("A-001", "B-002")]
    assert ab["same_phone"] and ab["same_email"] and "same_phone" in ab["signal_keys"]
    ac = pairs.loc[("A-001", "C-003")]
    assert "plagiarism_match" in ac["signal_keys"] and "97% similar" in ac["signals"]
    assert ("D-004", "E-005") not in pairs.index            # placeholder phone, neither in focus
    assert row(res, "A-001")[S.RISK_LEVEL] == S.RISK_HIGH
    assert "HackerRank plagiarism engine: 97% similar to C-003" in row(res, "A-001")[S.RISK_REASONS]
    assert row(res, "C-003")[S.RISK_LEVEL] == S.RISK_MEDIUM
    assert set(res.individual.loc[res.individual[S.CANDIDATE_ID] == "B-002", "flag_key"]) == set()
    no_internal_errors(res)


def test_same_email_on_different_ids_is_a_pair():
    df = _small_frame([
        {S.CANDIDATE_ID: "X1", S.HR_PCT: 100.0, S.EMAIL: "dup@x.org", S.DATA_FLAGS: "shared_email"},
        {S.CANDIDATE_ID: "X2", S.HR_PCT: 100.0, S.EMAIL: "DUP@x.org ", S.DATA_FLAGS: "shared_email"},
        {S.CANDIDATE_ID: "X3", S.HR_PCT: 100.0, S.EMAIL: "other@x.org"},
    ])
    res = risk.run_risk_analysis(df, {}, AppConfig())
    assert list(zip(res.pairs[risk.CAND_A], res.pairs[risk.CAND_B])) == [("X1", "X2")]
    assert res.pairs["same_email"].iloc[0] and res.pairs["pair_score"].iloc[0] == 40.0
    assert levels(res, ["X1", "X2", "X3"]) == {"X1": S.RISK_MEDIUM, "X2": S.RISK_MEDIUM, "X3": S.RISK_CLEAR}


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------
def test_missing_timing_columns(population):
    df, by_cand, parsed_all, g = population
    slim = df.drop(columns=[S.TEST_START, S.SUBMISSION_TIME, S.COMPLETION_MIN, S.TEST_SLOT])
    res = risk.run_risk_analysis(slim, by_cand, AppConfig(), parsed_all)
    no_internal_errors(res)
    assert any("Timing signals skipped" in w for w in res.warnings)
    assert any("Speed anomaly skipped" in w for w in res.warnings)
    assert set(levels(res, g["ring"] + g["identity"]).values()) == {S.RISK_HIGH}
    # Without timestamps the back-to-back proxy signal is unavailable: IP + identical q4 remain.
    assert set(levels(res, g["proxy"]).values()) == {S.RISK_MEDIUM}
    assert res.pairs["start_gap_sec"].isna().all()


def test_missing_ip_and_hash_columns(population):
    df, by_cand, parsed_all, g = population
    slim = df.drop(columns=[S.IP_ADDRESS, S.IP_SUBNET] + HASH_COLS)
    res = risk.run_risk_analysis(slim, by_cand, AppConfig(), parsed_all)
    no_internal_errors(res)
    assert any("IP signals skipped" in w for w in res.warnings)
    assert any("Answer-hash signal skipped" in w for w in res.warnings)
    assert set(levels(res, g["mill"] + g["identity"]).values()) <= {S.RISK_HIGH, S.RISK_MEDIUM}
    assert set(levels(res, g["lab"] + g["hostel"]).values()) <= {S.RISK_CLEAR, S.RISK_LOW}


def test_minimal_and_degenerate_inputs():
    only = pd.DataFrame({S.CANDIDATE_ID: ["A", "B", None, "A"], S.HR_PCT: [100.0, 50.0, 100.0, 10.0]})
    res = risk.run_risk_analysis(only, None, AppConfig())
    no_internal_errors(res)
    assert list(res.candidate_risk[S.CANDIDATE_ID]) == ["A", "B"]
    assert (res.candidate_risk[S.RISK_LEVEL] == S.RISK_CLEAR).all()
    assert res.focus_ids == ["A"] and res.pairs.empty and res.clusters.empty
    assert any("without a candidate_id" in w for w in res.warnings)
    assert any("duplicate candidate_id" in w for w in res.warnings)

    for empty_input in (pd.DataFrame(), None, pd.DataFrame({S.NAME: ["x"]})):
        res = risk.run_risk_analysis(empty_input, {}, AppConfig())
        assert res.candidate_risk.empty and list(res.pairs.columns) == risk.PAIR_COLUMNS
        assert res.warnings

    no_pct = pd.DataFrame({S.CANDIDATE_ID: ["A", "B"], S.TAB_SWITCHES: [12, 0]})
    res = risk.run_risk_analysis(no_pct, {}, AppConfig())
    assert any("No hackerrank_pct column" in w for w in res.warnings)
    assert levels(res, ["A", "B"]) == {"A": S.RISK_LOW, "B": S.RISK_CLEAR}


def test_infrastructure_ip_is_not_pair_evidence():
    n = 60                                      # 60 Remote perfect scorers, 60 colleges, one IP
    df = pd.DataFrame({
        S.CANDIDATE_ID: [f"V-{i:03d}" for i in range(n)], S.HR_PCT: 100.0, S.IP_ADDRESS: "52.66.1.20",
        S.COLLEGE: [f"College {i}" for i in range(n)], S.TEST_MODE: "Remote",
        S.TEST_START: [at(6, 9) + pd.Timedelta(minutes=37 * i) for i in range(n)], S.COMPLETION_MIN: 30.0,
    })
    res = risk.run_risk_analysis(df, {}, AppConfig())
    no_internal_errors(res)
    assert res.pairs.empty and set(res.candidate_risk[S.RISK_LEVEL]) == {S.RISK_CLEAR}
    assert any("shared infrastructure" in w for w in res.warnings)
    ip_row = res.commonality[res.commonality["attribute_key"] == S.IP_ADDRESS]
    assert int(ip_row["focus_count"].iloc[0]) == n          # still visible in the lift table


def test_garbage_values_do_not_raise():
    df = pd.DataFrame({
        S.CANDIDATE_ID: ["A", "B", "C"], S.HR_PCT: ["100", "abc", None],
        S.TEST_START: ["2026-10-05 10:00", "not a date", None], S.SUBMISSION_TIME: [None, None, None],
        S.COMPLETION_MIN: ["x", None, "60"], S.IP_ADDRESS: ["::1", "10.0.0.1, 1.2.3.4", "garbage"],
        S.PHONE: ["123", None, "abc"], S.EMAIL: ["no-at-sign", None, "@x"], S.MAX_CODE_SIMILARITY: ["0.97", "", None],
        "q1_hash": ["", "d41d8cd98f00b204e9800998ecf8427e", None], S.TEST_MODE: [None, 3, "On campus"],
    })
    bad = ParsedResume(file_name="a.pdf", file_hash="h", text="", sections={"projects": None}, bullets=[None, 5])
    res = risk.run_risk_analysis(df, {"A": bad, "B": "not a resume"}, AppConfig(), {"x": None})
    no_internal_errors(res)
    assert len(res.candidate_risk) == 3


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------
def test_deterministic_and_row_order_invariant(population, result):
    df, by_cand, parsed_all, _ = population
    again = risk.run_risk_analysis(df, by_cand, AppConfig(), parsed_all)
    shuffled = risk.run_risk_analysis(df.sample(frac=1.0, random_state=3), dict(reversed(list(by_cand.items()))),
                                      AppConfig(), dict(reversed(list(parsed_all.items()))))
    for other in (again, shuffled):
        for name in ("candidate_risk", "pairs", "clusters", "commonality", "individual"):
            pd.testing.assert_frame_equal(getattr(result, name), getattr(other, name), check_dtype=False)
        assert other.focus_ids == result.focus_ids and other.warnings == result.warnings


# ---------------------------------------------------------------------------
# Performance: 3,000 candidates, 300 perfect scorers
# ---------------------------------------------------------------------------
def build_large(n: int = 3000, n_focus: int = 300, seed: int = 5
                ) -> tuple[pd.DataFrame, dict[str, ParsedResume], list[str]]:
    rng = np.random.default_rng(seed)
    ids = [f"CAND-{i + 1:05d}" for i in range(n)]
    perm = rng.permutation(n)
    focus = np.zeros(n, dtype=bool)
    focus[perm[:n_focus]] = True
    college = np.array([f"College {c:02d}" for c in rng.integers(0, 60, n)], dtype=object)
    mode = np.where(rng.random(n) < 0.3, "On-campus", "Remote").astype(object)
    ip = np.array([f"{100 + i // 60000}.{(i // 250) % 256}.{i % 250}.{(i * 7) % 250 + 1}" for i in range(n)],
                  dtype=object)
    start = BASE + pd.to_timedelta(rng.integers(0, 3 * 24 * 3600, n), unit="s")
    start = np.array(start, dtype="datetime64[s]")
    minutes = rng.normal(60, 12, n).clip(20, 120)
    for lab in range(3):                     # 3 labs: 30 perfect scorers + 40 others, one NAT IP, 10:00 start
        members = np.concatenate([perm[lab * 30:(lab + 1) * 30], perm[n_focus + lab * 40: n_focus + (lab + 1) * 40]])
        college[members] = f"Lab College {lab}"
        mode[members] = "On-campus"
        ip[members] = f"14.139.{160 + lab}.10"
        start[members] = np.datetime64(at(5 + lab, 10)) + rng.integers(0, 90, len(members)).astype("timedelta64[s]")
    hashes = {f"q{q}_hash": np.array([f"{i}-{q}" for i in range(n)], dtype=object) for q in range(1, 7)}
    for q in (1, 2):                                       # MCQ answer keys: every perfect scorer agrees
        hashes[f"q{q}_hash"][focus] = f"mcq-key-{q}"
    ring = list(perm[100:105])                             # 5 focus members (not in a lab) share q3, q4
    for q in (3, 4):
        hashes[f"q{q}_hash"][ring] = f"ring-{q}"
    df = pd.DataFrame({
        S.CANDIDATE_ID: ids, S.NAME: [f"Name {i}" for i in range(n)], S.EMAIL: [f"u{i}@mail.org" for i in range(n)],
        S.PHONE: [f"97{i:08d}" for i in range(n)], S.COLLEGE: college, S.TEST_MODE: mode, S.IP_ADDRESS: ip,
        S.HR_PCT: np.where(focus, 100.0, rng.uniform(20, 99, n).round(1)),
        S.TEST_START: start, S.COMPLETION_MIN: minutes,
        S.SUBMISSION_TIME: start + (minutes * 60).astype("timedelta64[s]"),
        S.TAB_SWITCHES: rng.poisson(1, n), S.COPY_PASTE: rng.poisson(0.2, n),
        S.MAX_CODE_SIMILARITY: rng.uniform(0, 60, n).round(0), **hashes,
    })
    vocab = np.array([f"w{i:05d}" for i in range(8000)], dtype=object)
    boiler = [f"boilerplate bullet number {k} about teamwork delivery and communication skills" for k in range(20)]
    by_cand: dict[str, ParsedResume] = {}
    for i, cid in enumerate(ids):
        bullets = [" ".join(rng.choice(vocab, 10)) for _ in range(4)] + [boiler[i % 20]]
        projects = " ".join(rng.choice(vocab, 60)) + "\n" + "\n".join(bullets)
        by_cand[cid] = make_resume(cid, projects, " ".join(rng.choice(vocab, 30)), bullets,
                                   [f"github.com/user{i}/project{i % 7}"])
    return df, by_cand, [ids[i] for i in ring]


def test_performance_3000_candidates_300_focus():
    df, by_cand, ring = build_large()
    risk.run_risk_analysis(df.head(50), {}, AppConfig())    # warm-up (lazy imports, caches)
    t0 = time.perf_counter()
    res = risk.run_risk_analysis(df, by_cand, AppConfig(), {r.file_hash: r for r in by_cand.values()})
    elapsed = time.perf_counter() - t0
    no_internal_errors(res)
    assert len(res.focus_ids) == 300 and len(res.candidate_risk) == 3000
    assert elapsed < 3.0, f"risk analysis took {elapsed:.2f}s"
    assert set(levels(res, ring).values()) == {S.RISK_HIGH}
    lab_ids = list(df.loc[df[S.COLLEGE].str.startswith("Lab College"), S.CANDIDATE_ID])
    assert set(levels(res, lab_ids).values()) <= {S.RISK_CLEAR, S.RISK_LOW}
    cr = res.candidate_risk.set_index(S.CANDIDATE_ID)
    assert (cr.loc[lab_ids, S.MAX_PAIR_SCORE] < AppConfig().risk.edge_threshold).all()
    assert (cr[S.RISK_LEVEL].isin([S.RISK_HIGH, S.RISK_MEDIUM])).sum() <= 15     # few flags overall
