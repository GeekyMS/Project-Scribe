"""
Consolidate evaluation results from multiple paper runs into a single report.

Usage:
    python consolidate_results.py

Scans test_outputs/paper*_run/generation/ for evaluation reports and
produces a consolidated markdown report + CSV.
"""

import json
import csv
from pathlib import Path


def load_evaluation(paper_id: int, base_dir: str = "test_outputs") -> dict | None:
    """Load evaluation report for a given paper."""
    report_dir = Path(base_dir) / f"paper{paper_id}_run" / "generation"
    
    # Find evaluation report files
    reports = list(report_dir.glob("evaluation_report_*.json"))
    if not reports:
        return None
    
    report_path = reports[0]
    with open(report_path, "r") as f:
        data = json.load(f)
    
    # Also load generation log for token usage
    gen_logs = list(report_dir.glob("generation_log_*.json"))
    gen_log = {}
    if gen_logs:
        with open(gen_logs[0], "r") as f:
            gen_log = json.load(f)
    
    # Load ground truth word count from structural stats
    return {
        "paper_id": paper_id,
        "model": data.get("model", "unknown"),
        "tier_1": data.get("tier_1_citation_metrics", {}),
        "tier_2": data.get("tier_2_judge_scores", {}),
        "tier_3": data.get("tier_3_text_similarity", {}),
        "structural": data.get("structural_stats", {}),
        "gen_log": gen_log,
    }


def generate_report(results: list[dict], output_dir: str = "test_outputs") -> str:
    """Generate a consolidated markdown report."""
    output_dir = Path(output_dir)
    
    lines = []
    lines.append("# Project Scribe — Consolidated Evaluation Report")
    lines.append("")
    lines.append(f"**Papers evaluated**: {len(results)}")
    lines.append(f"**Model**: {results[0]['model'] if results else 'N/A'}")
    lines.append(f"**Enrichment**: Disabled (--no-enrich)")
    lines.append("")
    
    # ── Summary Table ──
    lines.append("## Summary Table")
    lines.append("")
    lines.append("| Paper | Recall | Precision | Halluc. | Missing | Judge Avg | Leakage | Length Ratio | Words (Gen/GT) |")
    lines.append("|-------|--------|-----------|---------|---------|-----------|---------|--------------|----------------|")
    
    total_recall = 0
    total_precision = 0
    total_judge = 0
    total_length_ratio = 0
    total_word_ratio = 0
    leakage_counts = {"none": 0, "minor": 0, "major": 0}
    all_scores = {k: 0 for k in ["citation_accuracy", "coverage", "positioning", "depth", "flow", "organization", "overall_quality"]}
    
    for r in results:
        t1 = r["tier_1"]
        t2 = r["tier_2"]
        t3 = r["tier_3"]
        ss = r["structural"]
        
        recall = t1.get("recall", 0)
        precision = t1.get("precision", 0)
        halluc = len(t1.get("hallucinated_keys", []))
        missing = len(t1.get("missing_keys", []))
        
        scores = t2.get("scores", {})
        avg = sum(scores.values()) / len(scores) if scores else 0
        leakage = t2.get("knowledge_leakage", {}).get("severity", "?")
        
        length_ratio = t3.get("length_ratio", 0)
        gen_words = ss.get("generated", {}).get("word_count", 0)
        gt_words = ss.get("ground_truth", {}).get("word_count", 0)
        word_ratio = ss.get("ratios", {}).get("word_count_ratio", 0)
        
        total_recall += recall
        total_precision += precision
        total_judge += avg
        total_length_ratio += length_ratio
        total_word_ratio += word_ratio
        if leakage in leakage_counts:
            leakage_counts[leakage] += 1
        for k, v in scores.items():
            if k in all_scores:
                all_scores[k] += v
        
        lines.append(
            f"| {r['paper_id']} | {recall:.0%} | {precision:.0%} | {halluc} | {missing} | "
            f"{avg:.1f}/5.0 | {leakage} | {length_ratio:.2f}x | {gen_words}/{gt_words} |"
        )
    
    n = len(results)
    lines.append(
        f"| **Avg** | **{total_recall/n:.0%}** | **{total_precision/n:.0%}** | — | — | "
        f"**{total_judge/n:.2f}/5.0** | — | **{total_length_ratio/n:.2f}x** | — |"
    )
    lines.append("")
    
    # ── Detailed Judge Scores ──
    lines.append("## Detailed Judge Scores (per dimension)")
    lines.append("")
    dims = ["citation_accuracy", "coverage", "positioning", "depth", "flow", "organization", "overall_quality"]
    header = "| Paper | " + " | ".join(d.replace("_", " ").title() for d in dims) + " | Avg |"
    sep = "|-------|" + "|".join(["------"] * len(dims)) + "|-----|"
    lines.append(header)
    lines.append(sep)
    
    for r in results:
        scores = r["tier_2"].get("scores", {})
        vals = [str(scores.get(d, "—")) for d in dims]
        avg = sum(scores.values()) / len(scores) if scores else 0
        lines.append(f"| {r['paper_id']} | " + " | ".join(vals) + f" | {avg:.1f} |")
    
    # Averages
    avg_vals = [f"{all_scores[d]/n:.1f}" for d in dims]
    lines.append(f"| **Avg** | " + " | ".join(avg_vals) + f" | **{total_judge/n:.2f}** |")
    lines.append("")
    
    # ── Knowledge Leakage ──
    lines.append("## Knowledge Leakage Analysis")
    lines.append("")
    lines.append(f"- **None**: {leakage_counts['none']}/{n} papers")
    lines.append(f"- **Minor**: {leakage_counts['minor']}/{n} papers")
    lines.append(f"- **Major**: {leakage_counts['major']}/{n} papers")
    lines.append("")
    
    for r in results:
        leakage_info = r["tier_2"].get("knowledge_leakage", {})
        if leakage_info.get("detected"):
            evidence = leakage_info.get("evidence", [])
            if evidence:
                lines.append(f"**Paper {r['paper_id']}** ({leakage_info.get('severity', '?')}):")
                for e in evidence[:2]:
                    lines.append(f"  - {e[:200]}...")
                lines.append("")
    
    # ── Structural Analysis ──
    lines.append("## Structural Analysis")
    lines.append("")
    lines.append("| Paper | Gen Words | GT Words | Word Ratio | Gen Paragraphs | GT Paragraphs | Gen Subsections |")
    lines.append("|-------|-----------|----------|------------|----------------|---------------|-----------------|")
    
    for r in results:
        gen = r["structural"].get("generated", {})
        gt = r["structural"].get("ground_truth", {})
        ratio = r["structural"].get("ratios", {}).get("word_count_ratio", 0)
        lines.append(
            f"| {r['paper_id']} | {gen.get('word_count', 0)} | {gt.get('word_count', 0)} | "
            f"{ratio:.2f}x | {gen.get('paragraph_count', 0)} | {gt.get('paragraph_count', 0)} | "
            f"{gen.get('subsection_count', 0)} |"
        )
    lines.append("")
    
    # ── Token Usage ──
    lines.append("## Token Usage & Generation Time")
    lines.append("")
    lines.append("| Paper | Prompt Tokens | Completion Tokens | Total Tokens | Generation Time (s) |")
    lines.append("|-------|---------------|-------------------|--------------|---------------------|")
    
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_total_tokens = 0
    
    for r in results:
        gl = r["gen_log"]
        pt = gl.get("prompt_tokens", 0)
        ct = gl.get("completion_tokens", 0)
        tt = gl.get("total_tokens", 0)
        gt = gl.get("generation_time_s", 0)
        total_prompt_tokens += pt
        total_completion_tokens += ct
        total_total_tokens += tt
        lines.append(f"| {r['paper_id']} | {pt:,} | {ct:,} | {tt:,} | {gt:.1f} |")
    
    lines.append(f"| **Total** | **{total_prompt_tokens:,}** | **{total_completion_tokens:,}** | **{total_total_tokens:,}** | — |")
    lines.append("")
    
    # ── Key Findings ──
    lines.append("## Key Findings")
    lines.append("")
    
    avg_recall = total_recall / n
    avg_precision = total_precision / n
    avg_judge = total_judge / n
    avg_word_ratio = total_word_ratio / n
    
    lines.append(f"1. **Citation Accuracy**: Average recall {avg_recall:.0%}, precision {avg_precision:.0%}")
    if avg_recall == 1.0 and avg_precision == 1.0:
        lines.append("   - Near-perfect citation coverage across all papers")
    lines.append(f"2. **Judge Quality**: Average score {avg_judge:.2f}/5.0 across all dimensions")
    lines.append(f"3. **Length**: Generated sections are ~{avg_word_ratio:.1f}x longer than ground truth on average")
    lines.append(f"4. **Knowledge Leakage**: {leakage_counts['minor']} minor, {leakage_counts['major']} major out of {n} papers")
    lines.append(f"5. **Structure**: Model consistently adds subsections and better organization vs. original")
    lines.append("")
    
    # ── Strengths & Weaknesses (aggregated from judge) ──
    lines.append("## Aggregated Strengths & Weaknesses")
    lines.append("")
    lines.append("### Strengths (from LLM Judge)")
    for r in results:
        strengths = r["tier_2"].get("strengths", [])
        if strengths:
            lines.append(f"- **Paper {r['paper_id']}**: {strengths[0]}")
    lines.append("")
    lines.append("### Weaknesses (from LLM Judge)")
    for r in results:
        weaknesses = r["tier_2"].get("weaknesses", [])
        if weaknesses:
            lines.append(f"- **Paper {r['paper_id']}**: {weaknesses[0]}")
    lines.append("")
    
    report_text = "\n".join(lines)
    
    # Save markdown report
    report_path = output_dir / "consolidated_report.md"
    report_path.write_text(report_text, encoding="utf-8")
    print(f"✅ Saved consolidated report → {report_path}")
    
    # Save CSV
    csv_path = output_dir / "consolidated_results.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "paper_id", "model", "recall", "precision", "hallucinated", "missing",
            "judge_avg", "citation_accuracy", "coverage", "positioning", "depth",
            "flow", "organization", "overall_quality", "leakage_severity",
            "length_ratio", "word_count_ratio", "gen_words", "gt_words",
            "gen_paragraphs", "gt_paragraphs", "gen_subsections",
            "prompt_tokens", "completion_tokens", "total_tokens", "generation_time_s",
        ])
        for r in results:
            t1 = r["tier_1"]
            t2 = r["tier_2"]
            t3 = r["tier_3"]
            ss = r["structural"]
            gl = r["gen_log"]
            scores = t2.get("scores", {})
            avg = sum(scores.values()) / len(scores) if scores else 0
            writer.writerow([
                r["paper_id"], r["model"],
                t1.get("recall", 0), t1.get("precision", 0),
                len(t1.get("hallucinated_keys", [])), len(t1.get("missing_keys", [])),
                round(avg, 2),
                scores.get("citation_accuracy", ""), scores.get("coverage", ""),
                scores.get("positioning", ""), scores.get("depth", ""),
                scores.get("flow", ""), scores.get("organization", ""),
                scores.get("overall_quality", ""),
                t2.get("knowledge_leakage", {}).get("severity", ""),
                t3.get("length_ratio", 0),
                ss.get("ratios", {}).get("word_count_ratio", 0),
                ss.get("generated", {}).get("word_count", 0),
                ss.get("ground_truth", {}).get("word_count", 0),
                ss.get("generated", {}).get("paragraph_count", 0),
                ss.get("ground_truth", {}).get("paragraph_count", 0),
                ss.get("generated", {}).get("subsection_count", 0),
                gl.get("prompt_tokens", 0), gl.get("completion_tokens", 0),
                gl.get("total_tokens", 0), gl.get("generation_time_s", 0),
            ])
    print(f"✅ Saved CSV → {csv_path}")
    
    return report_text


def main():
    # Scan for all paper runs
    base_dir = Path("test_outputs")
    paper_ids = []
    for d in sorted(base_dir.iterdir()):
        if d.is_dir() and d.name.startswith("paper") and d.name.endswith("_run"):
            try:
                pid = int(d.name.replace("paper", "").replace("_run", ""))
                paper_ids.append(pid)
            except ValueError:
                continue
    
    print(f"Found paper runs: {paper_ids}")
    
    results = []
    for pid in sorted(paper_ids):
        r = load_evaluation(pid)
        if r:
            results.append(r)
            print(f"  ✅ Paper {pid}: loaded")
        else:
            print(f"  ⏭️  Paper {pid}: no evaluation report found")
    
    if not results:
        print("❌ No evaluation results found!")
        return
    
    print(f"\nConsolidating {len(results)} papers...")
    generate_report(results)


if __name__ == "__main__":
    main()
