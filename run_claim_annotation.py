"""
Phase 2 — Generate a human claim-annotation form from a real Tier 4
evaluation report, or score a filled-in one against its answer key.

Usage:
    # Generate the form
    python run_claim_annotation.py generate \\
        --evaluation-report outputs/generation/evaluation_report_claude-sonnet-4.6.json \\
        --enrichment-cache outputs/generation/enrichment_cache.json \\
        --output-dir outputs/generation \\
        --sample-size 20

    # After a human fills in the form and exports responses:
    python run_claim_annotation.py score \\
        --responses outputs/generation/claim_annotation_responses.json \\
        --answer-key outputs/generation/claim_annotation_answer_key_claude-sonnet-4.6.json
"""

import argparse
import json
import sys
from pathlib import Path

from pipeline import evidence_cache
from pipeline.claim_annotation import generate_claim_annotation_form, compute_agreement


def cmd_generate(args):
    report_path = Path(args.evaluation_report)
    cache_path = Path(args.enrichment_cache)
    output_dir = Path(args.output_dir)

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

    entry_by_key = evidence_cache.load_cache(cache_path)

    try:
        generate_claim_annotation_form(
            tier4.get("per_citation", []),
            entry_by_key,
            model_name=report_data.get("model", "unknown"),
            outputs_dir=output_dir,
            sample_size=args.sample_size,
            seed=args.seed,
        )
    except ValueError as e:
        print(f"❌ {e}")
        sys.exit(1)


def cmd_score(args):
    responses_path = Path(args.responses)
    answer_key_path = Path(args.answer_key)

    if not responses_path.exists():
        print(f"❌ Responses file not found: {responses_path}")
        sys.exit(1)
    if not answer_key_path.exists():
        print(f"❌ Answer key not found: {answer_key_path}")
        sys.exit(1)

    result = compute_agreement(responses_path, answer_key_path)

    print(f"\n{'='*60}")
    print(f"📝 Checker vs. Human Agreement")
    print(f"{'='*60}")
    print(f"   Items scored: {result.n_items}")
    print(f"   Agreement rate: {result.agreement_rate:.1%} ({result.n_agree}/{result.n_items})")
    print(f"\n   Note: this is checker-vs-one-human agreement, not standard")
    print(f"   multi-annotator IAA (which needs 2+ independent humans).")

    if result.confusion:
        print(f"\n   Confusion (row=human, col=checker):")
        verdicts = sorted({v for row in result.confusion.values() for v in row} | set(result.confusion.keys()))
        header = "".join(f"{v[:12]:>14s}" for v in verdicts)
        print(f"   {'':>20s}{header}")
        for human_v in verdicts:
            row = result.confusion.get(human_v, {})
            cells = "".join(f"{row.get(v, 0):>14d}" for v in verdicts)
            print(f"   {human_v:>20s}{cells}")

    if result.disagreements:
        print(f"\n   Disagreements ({len(result.disagreements)}):")
        for d in result.disagreements[:10]:
            print(f"      {d['citation_id']}: human={d['human_verdict']} "
                  f"vs checker={d['checker_verdict']}")

    out_path = answer_key_path.parent / f"agreement_report_{answer_key_path.stem}.json"
    out_path.write_text(json.dumps(result.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n📋 Full agreement report → {out_path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="Generate an annotation form from a Tier 4 evaluation report")
    gen.add_argument("--evaluation-report", required=True)
    gen.add_argument("--enrichment-cache", required=True)
    gen.add_argument("--output-dir", default="outputs/generation")
    gen.add_argument("--sample-size", type=int, default=20)
    gen.add_argument("--seed", type=int, default=None)
    gen.set_defaults(func=cmd_generate)

    score = sub.add_parser("score", help="Score filled-in responses against the withheld answer key")
    score.add_argument("--responses", required=True)
    score.add_argument("--answer-key", required=True)
    score.set_defaults(func=cmd_score)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
