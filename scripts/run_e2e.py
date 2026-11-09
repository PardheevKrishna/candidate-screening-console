"""End-to-end run on the synthetic Workday-style sample + ground-truth evaluation.

Usage:
    python scripts/run_e2e.py                     # data/sample, rules only (no LLM)
    python scripts/run_e2e.py --sample DIR        # another dataset written by generate_mock_data.py
    python scripts/run_e2e.py --excel out.xlsx    # also write the Excel report
    python scripts/run_e2e.py --strict            # exit 1 when a ground-truth check fails

Loads test_scores_export.xlsx + Resume.zip (through pipeline.load_sample_dataset, or straight from
the folder when the loader returns nothing), runs prepare_data + evaluate and prints shortlist / risk
summaries and stage timings. When <sample>/_ground_truth/ exists it also reports: duplicate
applications detected vs planted (precision / recall), legitimate team projects not flagged
High/Medium, resume mills flagged, code-similarity rows at Medium, prompt-injection resumes flagged
(hidden keywords not counted as skills), the missing-resume count and the shortlist outcome.
Output contains candidate IDs and counts only - never names, e-mails or resume text.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from screening import pipeline  # noqa: E402
from screening import schema as S  # noqa: E402
from screening.config import AppConfig  # noqa: E402
from screening.models import ResumeDoc  # noqa: E402

EXPORT_FILE = "test_scores_export.xlsx"
ARCHIVE_FILE = "Resume.zip"
TRUTH_DIR = "_ground_truth"
FLAGGED = {S.RISK_HIGH, S.RISK_MEDIUM}


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def _progress(stage: str, done: int, total: int, detail: str = "") -> None:
    if total and (done == total or done % 750 == 0):
        print(f"  [{stage}] {done}/{total}")


def load_inputs(sample: Path) -> tuple[tuple[str, bytes] | None, list[ResumeDoc], str | None]:
    """The sample through the app's loader; falls back to the export and archive files themselves."""
    try:
        ta_file, docs, jd = pipeline.load_sample_dataset(sample)
    except Exception as exc:   # loader being reworked: read the files directly below
        print(f"  load_sample_dataset failed ({type(exc).__name__}); reading the files directly")
        ta_file, docs, jd = None, [], None
    export, archive, jd_path = sample / EXPORT_FILE, sample / ARCHIVE_FILE, sample / "job_description.txt"
    if ta_file is None and export.exists():
        ta_file = (export.name, export.read_bytes())
    if not docs and archive.exists():
        docs = [ResumeDoc(archive.name, archive.read_bytes(), source="upload")]
    if jd is None and jd_path.exists():
        jd = jd_path.read_text(encoding="utf-8")
    return ta_file, list(docs or []), jd


# ---------------------------------------------------------------------------
# Ground-truth checks
# ---------------------------------------------------------------------------


def _series(df: pd.DataFrame, col: str, default: Any = "") -> pd.Series:
    """Column indexed by candidate ID (a constant series when the column is missing)."""
    ids = df[S.CANDIDATE_ID].astype(str)
    if col not in df.columns:
        return pd.Series(default, index=ids)
    return pd.Series(df[col].to_numpy(), index=ids)


def _levels(df: pd.DataFrame, ids: list[str]) -> dict[str, str]:
    level = _series(df, S.RISK_LEVEL, "missing")
    return {cid: str(level.get(cid, "missing")) for cid in ids}


def _fmt_levels(levels: dict[str, str]) -> str:
    return ", ".join(f"{k}:{v}" for k, v in sorted(Counter(levels.values()).items())) or "-"


def _of(groups: list[dict], pattern: str) -> list[dict]:
    return [g for g in groups if g.get("pattern") == pattern]


def check_duplicates(df: pd.DataFrame, groups: list[dict], cfg: AppConfig) -> list[Check]:
    planted = {frozenset(g["members"]): g for g in _of(groups, "duplicate_application")}
    namesakes = {frozenset(g["members"]) for g in _of(groups, "namesake")}
    if S.DUPLICATE_OF not in df.columns:
        return [Check("duplicate applications", False, f"column '{S.DUPLICATE_OF}' missing")]
    dup_of = _series(df, S.DUPLICATE_OF).fillna("").astype(str)
    detected = {frozenset((cid, d)): (cid, d) for cid, d in dup_of.items() if d and d != cid}
    hits = set(detected) & set(planted)
    precision = len(hits) / len(detected) if detected else float("nan")
    recall = len(hits) / len(planted) if planted else float("nan")
    kept_ok = sum(detected[p][1] == planted[p]["details"]["kept"] for p in hits)
    merged_namesakes = len(set(detected) & namesakes)
    status = _series(df, S.STATUS)
    dropped = [cid for cid, _ in detected.values()]                  # rows marked as someone's duplicate
    ineligible = sum(str(status.get(c, "")) == S.STATUS_INELIGIBLE for c in dropped)
    print(f"  Duplicate applications: planted {len(planted)} pairs, detected {len(detected)} pairs, "
          f"correct {len(hits)} -> precision {precision:.0%}, recall {recall:.0%}")
    print(f"    kept the institutional-e-mail application: {kept_ok}/{len(hits)}; marked duplicates Ineligible: "
          f"{ineligible}/{len(dropped)}; namesake pairs merged by mistake: {merged_namesakes}/{len(namesakes)}")
    checks = [
        Check("duplicates: recall", recall == 1.0, f"{len(hits)}/{len(planted)} planted pairs found"),
        Check("duplicates: precision", bool(detected) and precision == 1.0,
              f"{len(hits)}/{len(detected)} detected pairs are planted"),
        Check("duplicates: institutional application kept", bool(hits) and kept_ok == len(hits),
              f"{kept_ok}/{len(hits)} pairs keep the institutional-e-mail application"),
        Check("duplicates: namesakes kept apart", merged_namesakes == 0, f"{merged_namesakes} namesake pairs merged"),
    ]
    if cfg.shortlist.auto_disqualify_duplicates:
        checks.append(Check("duplicates: marked duplicates Ineligible", bool(dropped) and ineligible == len(dropped),
                            f"{ineligible}/{len(dropped)} Ineligible"))
    return checks


def check_team_projects(df: pd.DataFrame, groups: list[dict], pairs: pd.DataFrame | None, edge: float) -> Check:
    teams = _of(groups, "team_project")
    levels = _levels(df, [c for g in teams for c in g["members"]])
    flagged = sorted(c for c, lv in levels.items() if lv in FLAGGED)
    linked = 0
    if pairs is not None and not pairs.empty and {"cand_a", "cand_b", "pair_score"} <= set(pairs.columns):
        edges = {frozenset((str(a), str(b))) for a, b, s in zip(pairs["cand_a"], pairs["cand_b"], pairs["pair_score"])
                 if s >= edge}
        linked = sum(frozenset(g["members"]) in edges for g in teams)
    print(f"  Team projects: {len(teams)} pairs; members flagged High/Medium {len(flagged)}; pairs linked "
          f"(score >= {edge:g}) {linked}; levels {_fmt_levels(levels)}")
    return Check("team projects not flagged", not flagged and linked == 0,
                 f"{len(flagged)} members High/Medium, {linked} pairs linked")


def check_flagged(df: pd.DataFrame, groups: list[dict], pattern: str, label: str) -> Check:
    found = _of(groups, pattern)
    members = [c for g in found for c in g["members"]]
    levels = _levels(df, members)
    hit = [c for c in members if levels[c] in FLAGGED]
    clusters = sorted({str(v) for v in _series(df, S.CLUSTER_ID).reindex(members).fillna("") if v})
    print(f"  {label}: {len(found)} groups, {len(members)} candidates; flagged High/Medium {len(hit)}/{len(members)}; "
          f"levels {_fmt_levels(levels)}; clusters {', '.join(clusters) or '-'}")
    return Check(f"{label} flagged", bool(members) and len(hit) == len(members), f"{len(hit)}/{len(members)} flagged")


def _canonical(skill: str) -> str:
    try:
        from screening import skills as SK
        return SK.canonicalize_skill(skill) or skill
    except Exception:   # taxonomy unavailable: compare the raw keyword
        return skill


def check_injection(df: pd.DataFrame, groups: list[dict]) -> list[Check]:
    found = _of(groups, "prompt_injection")
    levels = _levels(df, [g["members"][0] for g in found])
    flags = _series(df, S.INJECTION_FLAGS).fillna("").astype(str)
    skills = _series(df, S.SKILLS_FOUND).fillna("").astype(str)
    flagged = with_flags = 0
    leaks: list[str] = []
    for g in found:
        cid = g["members"][0]
        flagged += levels[cid] in FLAGGED
        with_flags += bool(flags.get(cid, "").strip())
        have = {s.strip().casefold() for s in str(skills.get(cid, "")).split(S.SEP_LIST) if s.strip()}
        leaked = [k for k in g["details"].get("hidden_keywords", []) if _canonical(k).casefold() in have]
        if leaked:
            leaks.append(f"{cid}: {', '.join(leaked)}")
        print(f"    {g['key']} ({'+'.join(g['details'].get('kinds', []))}): level {levels[cid]}, "
              f"injection flags {'yes' if flags.get(cid, '').strip() else 'no'}, hidden keywords counted as skills "
              f"{len(leaked)}")
    print(f"  Prompt injection: {len(found)} resumes; flagged High/Medium {flagged}; with injection flags {with_flags}")
    return [
        Check("prompt injection flagged", bool(found) and flagged == with_flags == len(found),
              f"{flagged}/{len(found)} High/Medium, {with_flags}/{len(found)} with injection flags"),
        Check("hidden keywords not counted as skills", not leaks, "; ".join(leaks) or "none leaked"),
    ]


def check_missing_resumes(df: pd.DataFrame, issues: dict, groups: list[dict]) -> Check:
    planted = set(issues.get("missing_resume", []))
    if S.HAS_RESUME not in df.columns:
        return Check("missing resumes", False, f"column '{S.HAS_RESUME}' missing")
    has = _series(df, S.HAS_RESUME, False).fillna(False).astype(bool)
    observed = {c for c, v in has.items() if not v}
    dup_members = {c for g in _of(groups, "duplicate_application") for c in g["members"]}
    extra = observed - planted
    print(f"  Missing resumes: planted {len(planted)} ({len(planted) / max(1, len(has)):.1%} of applications), "
          f"observed {len(observed)}; unexpectedly without a resume {len(extra)} "
          f"({len(extra & dup_members)} of them duplicate applications); resume not linked although planted "
          f"missing {len(planted - observed)}")
    return Check("missing resumes", observed == planted, f"observed {len(observed)} vs planted {len(planted)}")


def check_quiet_groups(df: pd.DataFrame, groups: list[dict], pattern: str, label: str) -> Check:
    members = [c for g in _of(groups, pattern) for c in g["members"]]
    levels = _levels(df, members)
    flagged = [c for c in members if levels[c] in FLAGGED]
    print(f"  {label}: {len(members)} candidates; flagged High/Medium {len(flagged)}; levels {_fmt_levels(levels)}")
    return Check(f"{label} not flagged", not flagged, f"{len(flagged)}/{len(members)} High/Medium")


def check_unscored(df: pd.DataFrame, issues: dict) -> Check:
    ids = [r["candidate_id"] for r in issues.get("unscored_rows", [])]
    status = _series(df, S.STATUS)
    states = {c: str(status.get(c, "missing")) for c in ids}
    print(f"  Unscored rows (#N/A): {len(ids)}; status {', '.join(states.values()) or '-'}")
    return Check("unscored row not shortlisted", bool(ids) and all(s == S.STATUS_INELIGIBLE for s in states.values()),
                 ", ".join(f"{c}: {s}" for c, s in states.items()))


def shortlist_summary(df: pd.DataFrame) -> None:
    print("\nShortlist")
    if S.STATUS in df.columns:
        counts = df[S.STATUS].value_counts()
        for status in S.STATUS_ORDER:
            if status in counts:
                print(f"  {status:<32}{int(counts[status]):>6,}")
    if S.INELIGIBILITY in df.columns:
        rules: Counter[str] = Counter()
        for value in df[S.INELIGIBILITY].dropna():
            try:
                items = json.loads(value) if isinstance(value, str) else value
            except ValueError:
                continue
            for item in items or []:
                if isinstance(item, dict):
                    rules[str(item.get("rule_violated", "?"))] += 1
        if rules:
            print("  Ineligibility rules: " + ", ".join(f"{k} {v}" for k, v in rules.most_common()))
    cols = [c for c in (S.RANK, S.CANDIDATE_ID, S.FINAL_SCORE, S.STATUS, S.RISK_LEVEL, S.TIE_BREAK_NOTE)
            if c in df.columns]
    if S.RANK in df.columns:
        print("  Top 5:\n" + df.sort_values(S.RANK).head(5)[cols].to_string(index=False, max_colwidth=60))


def evaluate_ground_truth(df: pd.DataFrame, result: Any, truth: Path, cfg: AppConfig) -> list[Check]:
    groups = json.loads((truth / "clusters.json").read_text(encoding="utf-8"))["clusters"]
    issues = json.loads((truth / "injected_issues.json").read_text(encoding="utf-8"))
    pairs = getattr(getattr(result, "risk", None), "pairs", None)
    print("\nGround truth")
    checks = check_duplicates(df, groups, cfg)
    checks.append(check_team_projects(df, groups, pairs, cfg.risk.edge_threshold))
    checks.append(check_flagged(df, groups, "resume_mill", "resume mills"))
    checks.append(check_flagged(df, groups, "code_similarity", "code-similarity rows"))
    checks += check_injection(df, groups)
    checks.append(check_quiet_groups(df, groups, "boilerplate_bullets", "boilerplate-bullet resumes"))
    checks.append(check_missing_resumes(df, issues, groups))
    checks.append(check_unscored(df, issues))
    print("\nChecks")
    for c in checks:
        print(f"  [{'PASS' if c.ok else 'FAIL'}] {c.name}: {c.detail}")
    return checks


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sample", default=str(ROOT / "data" / "sample"))
    ap.add_argument("--excel", default=None, help="write the Excel report to this path")
    ap.add_argument("--strict", action="store_true", help="exit 1 when a ground-truth check fails")
    args = ap.parse_args()
    sample = Path(args.sample)

    cfg = AppConfig()
    ta_file, docs, jd = load_inputs(sample)
    if ta_file is None:
        print(f"No {EXPORT_FILE} in {sample} - run: python generate_mock_data.py")
        return 1
    if jd:
        cfg.experience.job_description = jd

    t0 = time.perf_counter()
    prepared = pipeline.prepare_data(ta_file, docs, cfg, progress_cb=_progress)
    t1 = time.perf_counter()
    result = pipeline.evaluate(prepared, cfg)
    t2 = time.perf_counter()
    df = result.candidates

    print(f"\nMode: {result.mode} | candidates: {len(df):,} | resumes parsed: {len(result.parsed_resumes):,} "
          f"| linked: {len(result.parsed_by_candidate):,}")
    print(f"prepare_data: {t1 - t0:.1f}s  evaluate: {t2 - t1:.2f}s  stage timings: "
          + ", ".join(f"{k}={v:.2f}s" for k, v in result.timings.items()))
    if S.RISK_LEVEL in df.columns:
        print("\nRisk levels:\n" + df[S.RISK_LEVEL].value_counts().to_string())
    if S.IS_FOCUS in df.columns:
        print(f"\nPerfect scorers (focus group): {int(df[S.IS_FOCUS].sum()):,}")
    clusters = result.risk.clusters
    print(f"Clusters: {len(clusters)} | pairs: {len(result.risk.pairs)}")
    if not clusters.empty:
        show = [c for c in ("cluster_id", "size", "risk_level", "max_pair_score", "top_signals") if c in clusters.columns]
        print(clusters[show].to_string(index=False, max_colwidth=70))
    print(f"\nData quality issues: {result.quality.counts()}")
    if not result.matches.empty and "match_method" in result.matches.columns:
        print(f"Matches: {result.matches['match_method'].value_counts().to_dict()}")
    shortlist_summary(df)

    failed = 0
    truth = sample / TRUTH_DIR
    if (truth / "clusters.json").exists() and (truth / "injected_issues.json").exists():
        checks = evaluate_ground_truth(df, result, truth, cfg)
        failed = sum(not c.ok for c in checks)
        print(f"\n{len(checks) - failed}/{len(checks)} ground-truth checks passed")

    if args.excel:
        from screening import export
        Path(args.excel).write_bytes(export.build_excel_report(result))
        print(f"\nExcel report written: {args.excel}")
    return 1 if (args.strict and failed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
