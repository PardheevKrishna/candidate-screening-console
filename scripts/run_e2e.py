"""End-to-end run on the synthetic dataset + ground-truth evaluation of the risk engine.

Usage:
    python scripts/run_e2e.py                 # data/sample, rules only
    python scripts/run_e2e.py --excel out.xlsx  # also write the Excel report

Prints shortlist/risk summaries, stage timings, and - when
data/sample/_ground_truth/labels.csv exists - recall per planted pattern and
false-positive rates on the decoys.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from screening import pipeline  # noqa: E402
from screening import schema as S  # noqa: E402
from screening.config import AppConfig  # noqa: E402

FLAGGED = {S.RISK_HIGH, S.RISK_MEDIUM}


def _progress(stage: str, done: int, total: int, detail: str = "") -> None:
    if total and (done == total or done % 750 == 0):
        print(f"  [{stage}] {done}/{total}")


def evaluate_ground_truth(df: pd.DataFrame, gt_path: Path) -> dict:
    gt = pd.read_csv(gt_path, dtype=str).fillna("")
    cols = [S.CANDIDATE_ID, S.RISK_LEVEL, S.RISK_SCORE, S.CLUSTER_ID, S.STATUS, S.IS_FOCUS]
    merged = gt.merge(df[[c for c in cols if c in df.columns]], on=S.CANDIDATE_ID, how="left")
    out: dict = {"patterns": {}}

    print("\nGround truth - planted patterns")
    print(f"  {'pattern':<26}{'n':>3}  {'expected':<8} {'hit':>5}  {'levels':<34} clusters")
    colluders = merged[merged["label"] == "colluder"]
    for pattern, g in colluders.groupby("pattern"):
        expected = g["expected_min_level"].iloc[0] or S.RISK_MEDIUM
        ok_levels = {S.RISK_HIGH} if expected == S.RISK_HIGH else FLAGGED
        hit = g[S.RISK_LEVEL].isin(ok_levels).mean()
        levels = ", ".join(f"{k}:{v}" for k, v in g[S.RISK_LEVEL].value_counts().items())
        clusters = sorted({c for c in g[S.CLUSTER_ID].fillna("") if c})
        print(f"  {pattern:<26}{len(g):>3}  {expected:<8} {hit:>5.0%}  {levels:<34} {', '.join(clusters) or '-'}")
        out["patterns"][pattern] = {"n": len(g), "hit_rate": float(hit), "clusters": clusters}

    flagged_any = colluders[S.RISK_LEVEL].isin(FLAGGED).mean() if len(colluders) else float("nan")
    decoys = merged[merged["label"] == "decoy"]
    decoy_high = decoys[S.RISK_LEVEL].eq(S.RISK_HIGH).mean() if len(decoys) else float("nan")
    decoy_flag = decoys[S.RISK_LEVEL].isin(FLAGGED).mean() if len(decoys) else float("nan")

    normal_focus = df[(df[S.IS_FOCUS]) & (~df[S.CANDIDATE_ID].isin(colluders[S.CANDIDATE_ID]))]
    normal_focus = normal_focus[~normal_focus[S.CANDIDATE_ID].isin(decoys[S.CANDIDATE_ID])]
    fp_high = normal_focus[S.RISK_LEVEL].eq(S.RISK_HIGH).mean() if len(normal_focus) else float("nan")

    print(f"\n  Colluders flagged (High/Medium): {flagged_any:.1%} of {len(colluders)}")
    print(f"  Decoys flagged High: {decoy_high:.1%} | High/Medium: {decoy_flag:.1%} of {len(decoys)}")
    print(f"  Other perfect scorers flagged High: {fp_high:.1%} of {len(normal_focus)}")
    out.update(recall=float(flagged_any), decoy_high=float(decoy_high), decoy_flagged=float(decoy_flag),
               other_focus_high=float(fp_high))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample", default=str(ROOT / "data" / "sample"))
    ap.add_argument("--excel", default=None, help="write the Excel report to this path")
    args = ap.parse_args()

    cfg = AppConfig()
    ta_file, docs, jd = pipeline.load_sample_dataset(args.sample)
    if ta_file is None:
        print("No data/sample/ta_feed.csv - run: python generate_mock_data.py")
        return 1
    if jd:
        cfg.experience.job_description = jd

    t0 = time.perf_counter()
    prepared = pipeline.prepare_data(ta_file, docs, cfg, progress_cb=_progress)
    t1 = time.perf_counter()
    result = pipeline.evaluate(prepared, cfg)
    t2 = time.perf_counter()
    df = result.candidates

    print(f"\nMode: {result.mode} | candidates: {len(df):,} | resumes parsed: {len(result.parsed_resumes):,}")
    print(f"prepare_data: {t1 - t0:.1f}s  evaluate: {t2 - t1:.2f}s  stage timings: "
          + ", ".join(f"{k}={v:.2f}s" for k, v in result.timings.items()))
    print("\nStatus counts:\n" + df[S.STATUS].value_counts().to_string())
    print("\nRisk levels:\n" + df[S.RISK_LEVEL].value_counts().to_string())
    print(f"\nPerfect scorers (focus group): {int(df[S.IS_FOCUS].sum())}")
    print(f"Clusters: {len(result.risk.clusters)} | pairs: {len(result.risk.pairs)}")
    if not result.risk.clusters.empty:
        show = [c for c in ["cluster_id", "size", "risk_level", "max_pair_score", "top_signals"]
                if c in result.risk.clusters.columns]
        print(result.risk.clusters[show].to_string(index=False, max_colwidth=70))
    if not result.risk.commonality.empty and "significant" in result.risk.commonality.columns:
        sig = result.risk.commonality[result.risk.commonality["significant"]]
        print(f"\nSignificant commonalities among perfect scorers: {len(sig)}")
        show = [c for c in ["attribute", "value", "focus_count", "focus_share", "population_share", "lift", "q_value"]
                if c in sig.columns]
        print(sig[show].head(12).to_string(index=False))
    q = result.quality.counts()
    print(f"\nData quality issues: {q}")
    print(f"Matches: {result.matches['match_method'].value_counts().to_dict() if not result.matches.empty else {}}")
    top = df.sort_values(S.RANK).head(5)[[S.RANK, S.CANDIDATE_ID, S.FINAL_SCORE, S.STATUS, S.RISK_LEVEL]]
    print("\nTop 5:\n" + top.to_string(index=False))

    gt_path = Path(args.sample) / "_ground_truth" / "labels.csv"
    if gt_path.exists():
        evaluate_ground_truth(df, gt_path)

    if args.excel:
        from screening import export
        Path(args.excel).write_bytes(export.build_excel_report(result))
        print(f"\nExcel report written: {args.excel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
