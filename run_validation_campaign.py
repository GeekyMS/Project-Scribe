"""
Phase 2 — Wider validation campaign.

Runs Tier 4 (evidence support) + synthetic-negative detection across
EVERY paper in research_test_with_phd/ that has a generated Related
Works section, not just one — the single-paper run in run_synthetic_
validation.py was a smoke test that the machinery works; this is
closer to the actual validation the plan calls for.

Each paper is independent: a fetch/judge failure on one paper is
logged and skipped, not fatal to the campaign. Per-paper artifacts
(evaluation_report, enrichment_cache, synthetic_negative_report) are
written as each paper completes, and campaign_summary.json is
rewritten after every paper — so an interrupted run has usable
partial results, and a re-run with --skip-existing reuses them
instead of re-hitting the retrieval/judge APIs.

Aggregation follows this project's own documented discipline
(METRICS_CONTRACT.md's macro- vs micro-average distinction for
Challenge Alignment F1): the corpus detection rate is computed BOTH
ways — pooled across every raw case (micro: total_detected/total_cases)
and as the mean of each paper's own rate (macro) — logged side by
side rather than picking one silently, since they can diverge when
papers contribute very different numbers of cases.

Usage:
    python run_validation_campaign.py --output-dir outputs/phase2_campaign
    python run_validation_campaign.py --output-dir outputs/phase2_campaign --skip-existing
    python run_validation_campaign.py --output-dir outputs/phase2_campaign --papers 2605.30051v1 2603.29890v1
"""

import argparse
import json
import random
import sys
import traceback
from pathlib import Path

from pipeline.evaluator import run_evaluation, _call_judge, JUDGE_MODEL
from pipeline.synthetic_negatives import evaluate_corruption_set
from pipeline import evidence_cache

CORPUS_DIR = Path("research_test_with_phd")


def discover_papers() -> list[dict]:
    """Find every paper dir with both a generated .tex and a references.bib."""
    papers = []
    for d in sorted(CORPUS_DIR.iterdir()):
        if not d.is_dir():
            continue
        tex_candidates = sorted(d.glob("**/generated_related_work_*.tex"))
        bib_candidates = sorted(d.glob("**/references.bib"))
        if not tex_candidates or not bib_candidates:
            continue
        # Prefer the canonical results/generation/ path if present.
        tex = next(
            (t for t in tex_candidates if "results/generation" in str(t).replace("\\", "/")),
            tex_candidates[0],
        )
        bib = bib_candidates[0]
        gt_candidates = sorted(d.glob("**/related_work.txt"))
        ground_truth = gt_candidates[0] if gt_candidates else None
        papers.append({"id": d.name, "tex": tex, "bib": bib, "ground_truth": ground_truth})
    return papers


def _run_one_paper(paper: dict, paper_out_dir: Path, judge_model: str, max_negations: int, rng: random.Random) -> dict:
    """Run Tier 4 + synthetic negatives for one paper. Never raises — errors are captured in the record."""
    paper_id = paper["id"]
    paper_out_dir.mkdir(parents=True, exist_ok=True)

    record: dict = {"paper_id": paper_id, "status": "pending"}

    generated_text = paper["tex"].read_text(encoding="utf-8")
    ground_truth = paper["ground_truth"].read_text(encoding="utf-8") if paper["ground_truth"] else ""

    try:
        report = run_evaluation(
            generated_text=generated_text,
            ground_truth=ground_truth,
            paper_text="",
            bib_path=paper["bib"],
            model_name=paper_id,
            run_judge=False,
            run_argumentation=False,
            run_similarity=False,
            run_evidence=True,
            enrichment_cache_path=paper_out_dir / "enrichment_cache.json",
            outputs_dir=paper_out_dir,
        )
    except Exception as e:
        record["status"] = "tier4_failed"
        record["error"] = f"{type(e).__name__}: {e}"
        record["traceback"] = traceback.format_exc()
        return record

    es = report.evidence_support
    record["tier4"] = {
        "total_spans": es.total_spans,
        "verifiable_count": es.verifiable_count,
        "evidence_coverage": es.evidence_coverage,
        "support_rate": es.support_rate,
        "verdict_counts": es.verdict_counts,
    }

    n_supported = es.verdict_counts.get("supported", 0)
    if n_supported == 0:
        record["status"] = "no_supported_rows"
        return record

    entry_by_key = evidence_cache.load_cache(paper_out_dir / "enrichment_cache.json")

    try:
        sn_result = evaluate_corruption_set(
            es.per_citation, entry_by_key,
            judge_model=judge_model, call_judge_fn=_call_judge,
            max_negations=max_negations, rng=rng,
        )
    except Exception as e:
        record["status"] = "synth_neg_failed"
        record["error"] = f"{type(e).__name__}: {e}"
        record["traceback"] = traceback.format_exc()
        return record

    sn_path = paper_out_dir / f"synthetic_negative_report_{paper_id}.json"
    sn_dict = sn_result.to_dict()
    sn_path.write_text(json.dumps(sn_dict, indent=2, ensure_ascii=False), encoding="utf-8")

    record["status"] = "ok"
    record["synthetic_negatives"] = {
        "n_cases": sn_result.n_cases,
        "n_detected": sn_result.n_detected,
        "detection_rate": sn_result.detection_rate,
        "detection_rate_by_type": sn_result.detection_rate_by_type,
    }
    record["synthetic_negative_cases"] = sn_dict["cases"]  # raw cases, for corpus-level pooling
    return record


def _load_existing_record(paper_out_dir: Path, paper_id: str) -> dict | None:
    """Reconstruct a campaign record from a previous run's artifacts, for --skip-existing."""
    report_path = paper_out_dir / f"evaluation_report_{paper_id}.json"
    sn_path = paper_out_dir / f"synthetic_negative_report_{paper_id}.json"
    if not report_path.exists():
        return None

    report_data = json.loads(report_path.read_text(encoding="utf-8"))
    es = report_data.get("tier_4_evidence_support", {})
    record = {
        "paper_id": paper_id,
        "status": "ok" if sn_path.exists() else "no_supported_rows",
        "tier4": {
            "total_spans": es.get("total_spans", 0),
            "verifiable_count": es.get("verifiable_count", 0),
            "evidence_coverage": es.get("evidence_coverage", 0),
            "support_rate": es.get("support_rate", 0),
            "verdict_counts": es.get("verdict_counts", {}),
        },
    }
    if sn_path.exists():
        sn_data = json.loads(sn_path.read_text(encoding="utf-8"))
        record["synthetic_negatives"] = {
            "n_cases": sn_data.get("n_cases", 0),
            "n_detected": sn_data.get("n_detected", 0),
            "detection_rate": sn_data.get("detection_rate", 0),
            "detection_rate_by_type": sn_data.get("detection_rate_by_type", {}),
        }
        record["synthetic_negative_cases"] = sn_data.get("cases", [])
    return record


def _aggregate(campaign_results: list[dict]) -> dict:
    ok_results = [r for r in campaign_results if r["status"] == "ok"]
    tier4_results = [r for r in campaign_results if "tier4" in r]

    total_spans = sum(r["tier4"]["total_spans"] for r in tier4_results)
    total_verifiable = sum(r["tier4"]["verifiable_count"] for r in tier4_results)

    # Pooled (micro) detection: every raw case across every paper, counted once.
    all_cases = [c for r in ok_results for c in r.get("synthetic_negative_cases", [])]
    evaluated_cases = [c for c in all_cases if c.get("detected") is not None]
    micro_detected = sum(1 for c in evaluated_cases if c["detected"])
    micro_rate = micro_detected / len(evaluated_cases) if evaluated_cases else 0.0

    by_type_cases: dict[str, list[bool]] = {}
    for c in evaluated_cases:
        by_type_cases.setdefault(c["corruption_type"], []).append(c["detected"])
    micro_by_type = {t: sum(v) / len(v) for t, v in by_type_cases.items()}

    # Macro: mean of each paper's own detection_rate (unweighted by case count).
    per_paper_rates = [r["synthetic_negatives"]["detection_rate"] for r in ok_results]
    macro_rate = sum(per_paper_rates) / len(per_paper_rates) if per_paper_rates else 0.0

    return {
        "n_papers_discovered": len(campaign_results),
        "n_papers_ok": len(ok_results),
        "n_papers_no_supported_rows": len([r for r in campaign_results if r["status"] == "no_supported_rows"]),
        "n_papers_failed": len([r for r in campaign_results if r["status"] not in ("ok", "no_supported_rows")]),
        "corpus_total_citation_spans": total_spans,
        "corpus_verifiable_spans": total_verifiable,
        "corpus_evidence_coverage": total_verifiable / total_spans if total_spans else 0.0,
        "corpus_synthetic_negative_cases_micro": len(evaluated_cases),
        "corpus_detection_rate_micro": round(micro_rate, 4),
        "corpus_detection_rate_micro_by_type": {k: round(v, 4) for k, v in micro_by_type.items()},
        "corpus_detection_rate_macro": round(macro_rate, 4),
        "note": (
            "micro = pooled across every raw case (papers with more citations "
            "contribute more cases); macro = unweighted mean of each paper's own "
            "rate. They can diverge — report both, per METRICS_CONTRACT.md's "
            "macro/micro discipline for Challenge Alignment F1."
        ),
        "per_paper": [
            {k: v for k, v in r.items() if k != "synthetic_negative_cases"}  # raw cases live in per-paper files
            for r in campaign_results
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output-dir", default="outputs/phase2_campaign")
    parser.add_argument("--judge-model", default=JUDGE_MODEL)
    parser.add_argument("--max-negations", type=int, default=8,
                         help="Cap on LLM-generated 'negate' cases PER PAPER.")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--papers", nargs="*", default=None,
                         help="Only run these paper ids (default: all discovered).")
    parser.add_argument("--skip-existing", action="store_true",
                         help="Reuse a paper's existing evaluation_report/synthetic_negative_report "
                              "instead of re-running it (no API calls for that paper).")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    papers = discover_papers()
    if args.papers:
        papers = [p for p in papers if p["id"] in args.papers]
    if not papers:
        print("❌ No papers found with both a generated .tex and a references.bib")
        sys.exit(1)

    print(f"📚 Campaign: {len(papers)} papers discovered")
    for p in papers:
        print(f"   - {p['id']}")

    rng = random.Random(args.seed)
    campaign_results = []

    for i, paper in enumerate(papers):
        paper_id = paper["id"]
        paper_out_dir = output_dir / paper_id
        print(f"\n{'='*70}")
        print(f"📄 [{i+1}/{len(papers)}] {paper_id}")
        print(f"{'='*70}")

        record = None
        if args.skip_existing:
            record = _load_existing_record(paper_out_dir, paper_id)
            if record:
                print(f"   ⏩ Reusing existing results (status={record['status']})")

        if record is None:
            record = _run_one_paper(paper, paper_out_dir, args.judge_model, args.max_negations, rng)

        campaign_results.append(record)

        if record["status"] == "ok":
            t4 = record["tier4"]
            sn = record["synthetic_negatives"]
            print(f"   ✅ Tier 4: {t4['evidence_coverage']:.1%} coverage, "
                  f"{t4['support_rate']:.1%} support rate ({t4['total_spans']} spans)")
            print(f"   ✅ Synthetic negatives: {sn['detection_rate']:.1%} detection "
                  f"({sn['n_detected']}/{sn['n_cases']})")
        elif record["status"] == "no_supported_rows":
            print(f"   ⏩ No 'supported' rows — nothing to corrupt for this paper")
        else:
            print(f"   ❌ {record['status']}: {record.get('error', 'unknown error')}")

        # Rewrite the summary after every paper — partial results survive an interruption.
        aggregate = _aggregate(campaign_results)
        (output_dir / "campaign_summary.json").write_text(
            json.dumps(aggregate, indent=2, ensure_ascii=False), encoding="utf-8",
        )

    aggregate = _aggregate(campaign_results)
    print(f"\n{'='*70}")
    print(f"📊 CAMPAIGN SUMMARY")
    print(f"{'='*70}")
    print(f"   Papers: {aggregate['n_papers_ok']} ok, "
          f"{aggregate['n_papers_no_supported_rows']} had no supported rows, "
          f"{aggregate['n_papers_failed']} failed")
    print(f"   Corpus evidence coverage: {aggregate['corpus_evidence_coverage']:.1%} "
          f"({aggregate['corpus_verifiable_spans']}/{aggregate['corpus_total_citation_spans']} spans)")
    print(f"   Corpus detection rate (micro, pooled): {aggregate['corpus_detection_rate_micro']:.1%} "
          f"({aggregate['corpus_synthetic_negative_cases_micro']} cases)")
    print(f"   Corpus detection rate (macro, per-paper mean): {aggregate['corpus_detection_rate_macro']:.1%}")
    print(f"   By corruption type (micro): {aggregate['corpus_detection_rate_micro_by_type']}")
    print(f"\n📋 Full campaign summary → {output_dir / 'campaign_summary.json'}")


if __name__ == "__main__":
    main()
