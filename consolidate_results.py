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
        "tier_2b": data.get("tier_2b_argumentative_roles", {}),
        "tier_2c": data.get("tier_2c_coherence", {}),
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
    
    # ── Argumentative Metrics (Tier 2B + 2C) ──
    has_argumentation = any(r.get("tier_2b", {}).get("total_citations_analyzed", 0) > 0 for r in results)
    
    if has_argumentation:
        lines.append("## Argumentative Metrics")
        lines.append("")
        lines.append("### Per-Citation Analysis (Tier 2B)")
        lines.append("")
        lines.append("| Paper | Role Match | Emphasis Match | Role JSD | "
                     "Exact/Adj/Mis | Precise/Partial/Generic |")
        lines.append("|-------|------------|----------------|----------|" 
                     "---------------|-------------------------|")
        
        total_role = 0
        total_emphasis = 0
        total_jsd = 0
        count_2b = 0
        
        for r in results:
            t2b = r.get("tier_2b", {})
            if t2b.get("total_citations_analyzed", 0) > 0:
                role = t2b.get("role_match_score", 0)
                emphasis = t2b.get("emphasis_match_score", 0)
                jsd = t2b.get("role_distribution_divergence", 0)
                exact = t2b.get("exact_role_matches", 0)
                adj = t2b.get("adjacent_role_matches", 0)
                mis = t2b.get("role_mismatches", 0)
                precise = t2b.get("precise_emphasis", 0)
                partial = t2b.get("partial_emphasis", 0)
                generic = t2b.get("generic_emphasis", 0)
                
                total_role += role
                total_emphasis += emphasis
                total_jsd += jsd
                count_2b += 1
                
                lines.append(
                    f"| {r['paper_id']} | {role:.2f} | {emphasis:.2f} | "
                    f"{jsd:.3f} | {exact}/{adj}/{mis} | "
                    f"{precise}/{partial}/{generic} |"
                )
        
        if count_2b > 0:
            lines.append(
                f"| **Avg** | **{total_role/count_2b:.2f}** | "
                f"**{total_emphasis/count_2b:.2f}** | "
                f"**{total_jsd/count_2b:.3f}** | — | — |"
            )
        lines.append("")
        
        lines.append("### Section-Level Coherence (Tier 2C)")
        lines.append("")
        lines.append("| Paper | Challenges | Align Coverage | Align Relevance | "
                     "Align F1 | Arc Score |")
        lines.append("|-------|------------|----------------|-----------------|" 
                     "----------|-----------|")
        
        total_f1 = 0
        total_arc = 0
        count_2c = 0
        
        for r in results:
            t2c = r.get("tier_2c", {})
            challenges = t2c.get("challenges", [])
            gen_align = t2c.get("alignment_scores", {}).get("generated", {})
            gen_arc = t2c.get("narrative_arc_scores", {}).get("generated", {})
            
            if challenges:
                coverage = gen_align.get("challenge_coverage", 0)
                relevance = gen_align.get("citation_relevance", 0)
                f1 = gen_align.get("alignment_f1", 0)
                arc = gen_arc.get("composite_arc_score", 0)
                
                total_f1 += f1
                total_arc += arc
                count_2c += 1
                
                lines.append(
                    f"| {r['paper_id']} | {len(challenges)} | {coverage:.2f} | "
                    f"{relevance:.2f} | {f1:.2f} | {arc:.2f} |"
                )
        
        if count_2c > 0:
            lines.append(
                f"| **Avg** | — | — | — | "
                f"**{total_f1/count_2c:.2f}** | **{total_arc/count_2c:.2f}** |"
            )
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
            # New argumentative metrics
            "role_match_score", "emphasis_match_score", "role_jsd",
            "exact_roles", "adjacent_roles", "role_mismatches",
            "precise_emphasis", "partial_emphasis", "generic_emphasis",
            "challenge_count", "alignment_coverage", "alignment_relevance",
            "alignment_f1", "arc_presence", "arc_ordering",
            "arc_balance", "arc_composite",
            # Existing structural + token metrics
            "length_ratio", "word_count_ratio", "gen_words", "gt_words",
            "gen_paragraphs", "gt_paragraphs", "gen_subsections",
            "prompt_tokens", "completion_tokens", "total_tokens", "generation_time_s",
        ])
        for r in results:
            t1 = r["tier_1"]
            t2 = r["tier_2"]
            t2b = r.get("tier_2b", {})
            t2c = r.get("tier_2c", {})
            t3 = r["tier_3"]
            ss = r["structural"]
            gl = r["gen_log"]
            scores = t2.get("scores", {})
            avg = sum(scores.values()) / len(scores) if scores else 0
            
            gen_align = t2c.get("alignment_scores", {}).get("generated", {})
            gen_arc = t2c.get("narrative_arc_scores", {}).get("generated", {})
            
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
                # Argumentative role metrics
                t2b.get("role_match_score", ""),
                t2b.get("emphasis_match_score", ""),
                t2b.get("role_distribution_divergence", ""),
                t2b.get("exact_role_matches", ""),
                t2b.get("adjacent_role_matches", ""),
                t2b.get("role_mismatches", ""),
                t2b.get("precise_emphasis", ""),
                t2b.get("partial_emphasis", ""),
                t2b.get("generic_emphasis", ""),
                # Coherence metrics
                len(t2c.get("challenges", [])),
                gen_align.get("challenge_coverage", ""),
                gen_align.get("citation_relevance", ""),
                gen_align.get("alignment_f1", ""),
                gen_arc.get("presence_score", ""),
                gen_arc.get("ordering_score", ""),
                gen_arc.get("balance_score", ""),
                gen_arc.get("composite_arc_score", ""),
                # Structural + tokens
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
