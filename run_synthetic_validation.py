"""
Phase 2 — Synthetic-negative validation for Tier 4 (evidence support).

Takes a real evaluation_report_<model>.json (which has to have been
run with Tier 4 enabled — i.e. NOT --no-evidence) plus the enrichment
cache from that same generation run, corrupts the claims Tier 4
verified as "supported" into known-false variants (swap_key,
inflate_number, negate — see pipeline/synthetic_negatives.py), checks
them again through the same Tier 4 machinery, and reports the
detection rate: how often the checker correctly stops saying
"supported" once the claim has been falsified.

This is a floor, not a substitute for real human-annotated ground
truth — see also pipeline/claim_annotation.py for the human-review
half of Phase 2.

Usage:
    python run_synthetic_validation.py \\
        --evaluation-report outputs/generation/evaluation_report_claude-sonnet-4.6.json \\
        --enrichment-cache outputs/generation/enrichment_cache.json \\
        --output-dir outputs/generation
"""

import argparse
import json
import sys
from pathlib import Path

from pipeline import evidence_cache
from pipeline.evaluator import _call_judge, JUDGE_MODEL
from pipeline.synthetic_negatives import evaluate_corruption_set


def main():
    parser = argparse.ArgumentParser(
        description="Run synthetic-negative validation against a real Tier 4 evaluation report.",
    )
    parser.add_argument(
        "--evaluation-report", required=True,
        help="Path to evaluation_report_<model>.json from a run with Tier 4 enabled.",
    )
    parser.add_argument(
        "--enrichment-cache", required=True,
        help="Path to the enrichment_cache.json or full_text_cache.json from the SAME "
             "generation run (so corrupted claims are checked against the same evidence).",
    )
    parser.add_argument(
        "--output-dir", default="outputs/generation",
        help="Directory to write synthetic_negative_report_<model>.json (default: outputs/generation)",
    )
    parser.add_argument(
        "--judge-model", default=JUDGE_MODEL,
        help=f"Judge model for both negation generation and re-checking (default: {JUDGE_MODEL})",
    )
    parser.add_argument(
        "--max-negations", type=int, default=8,
        help="Cap on LLM-generated 'negate' cases (each costs one extra call). Default: 8.",
    )
    parser.add_argument(
        "--seed", type=int, default=None,
        help="Random seed for reproducible case selection (swap_key target, negation sampling).",
    )
    args = parser.parse_args()

    report_path = Path(args.evaluation_report)
    cache_path = Path(args.enrichment_cache)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not report_path.exists():
        print(f"❌ Evaluation report not found: {report_path}")
        sys.exit(1)
    if not cache_path.exists():
        print(f"❌ Enrichment cache not found: {cache_path}")
        sys.exit(1)

    report_data = json.loads(report_path.read_text(encoding="utf-8"))
    tier4 = report_data.get("tier_4_evidence_support")
    if not tier4:
        print("❌ This evaluation report has no tier_4_evidence_support section "
              "— was it run with --no-evidence?")
        sys.exit(1)

    per_citation_rows = tier4.get("per_citation", [])
    n_supported = sum(1 for r in per_citation_rows if r.get("verdict") == "supported")
    print(f"📄 Loaded {len(per_citation_rows)} Tier 4 rows from {report_path.name} "
          f"({n_supported} verdict='supported' — the corruption seed pool)")

    entry_by_key = evidence_cache.load_cache(cache_path)
    print(f"📦 Loaded {len(entry_by_key)} enriched entries from {cache_path.name}")

    if n_supported == 0:
        print("❌ No 'supported' rows to corrupt — nothing to validate. "
              "Run the paper through Tier 4 first and confirm it finds at least "
              "some supported citations.")
        sys.exit(1)

    import random
    rng = random.Random(args.seed) if args.seed is not None else random.Random()

    print(f"\n🧪 Building and running synthetic-negative test set "
          f"(judge: {args.judge_model})...")
    result = evaluate_corruption_set(
        per_citation_rows, entry_by_key,
        judge_model=args.judge_model,
        call_judge_fn=_call_judge,
        max_negations=args.max_negations,
        rng=rng,
    )

    model_name = report_data.get("model", "unknown")
    out_path = output_dir / f"synthetic_negative_report_{model_name}.json"
    out_path.write_text(
        json.dumps(result.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8",
    )

    print(f"\n{'='*60}")
    print(f"🧪 Synthetic-Negative Detection Results")
    print(f"{'='*60}")
    if result.error and result.n_cases == 0:
        print(f"❌ {result.error}")
        sys.exit(1)
    print(f"   Cases evaluated: {result.n_cases}")
    print(f"   Overall detection rate: {result.detection_rate:.1%} "
          f"({result.n_detected}/{result.n_cases})")
    print(f"   By corruption type:")
    for ctype, rate in sorted(result.detection_rate_by_type.items()):
        n = sum(1 for c in result.cases if c.corruption_type == ctype and c.detected is not None)
        print(f"      {ctype:20s}: {rate:.1%} (n={n})")
    if result.error:
        print(f"   ⚠️ Partial failure during batching: {result.error}")

    missed = [c for c in result.cases if c.detected is False]
    if missed:
        print(f"\n   ⚠️ Missed corruptions (checker said 'supported' anyway):")
        for c in missed[:10]:
            print(f"      [{c.corruption_type}] {c.original_key}: \"{c.corrupted_claim[:100]}\"")

    print(f"\n📋 Full report → {out_path}")


if __name__ == "__main__":
    main()
