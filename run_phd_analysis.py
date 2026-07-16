"""
Generate consolidated report and complexity analysis for PhD testing papers.
"""
import sys
import json
from pathlib import Path

# Add current dir to path to import local modules
sys.path.insert(0, str(Path(".").resolve()))

from consolidate_results import generate_report
from pipeline.complexity_analysis import (
    compute_correlations,
    generate_plots,
    generate_complexity_report,
)

BASE_DIR = Path("research_test_with_phd")

def main():
    if not BASE_DIR.exists():
        print(f"❌ Base directory not found: {BASE_DIR}")
        return

    # 1. Collect data for both consolidation and complexity analysis
    consolidated_data = []
    complexity_data = []

    for d in sorted(BASE_DIR.iterdir()):
        if not d.is_dir():
            continue
            
        paper_id = d.name
        report_dir = d / "results" / "generation"
        if not report_dir.exists():
            print(f"  ⏭️  Paper {paper_id}: no generation dir")
            continue

        reports = list(report_dir.glob("evaluation_report_*.json"))
        if not reports:
            print(f"  ⏭️  Paper {paper_id}: no evaluation_report_*.json")
            continue

        report_path = reports[0]
        with open(report_path, "r") as f:
            data = json.load(f)

        gen_logs = list(report_dir.glob("generation_log_*.json"))
        gen_log = {}
        if gen_logs:
            with open(gen_logs[0], "r") as f:
                gen_log = json.load(f)

        # Build data for consolidation
        c_data = {
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
        consolidated_data.append(c_data)

        # Build data for complexity analysis
        comp_data = {
            "paper_id": paper_id,
            "model": data.get("model", "unknown"),
            "bib_entry_count": c_data["tier_1"].get("citations_found", 0),
            "prompt_tokens": gen_log.get("prompt_tokens", 0),
            "paper_length_words": c_data["structural"].get("ground_truth", {}).get("word_count", 0),
            "citation_recall": c_data["tier_1"].get("recall", 0),
            "citation_precision": c_data["tier_1"].get("precision", 0),
            "role_match_score": c_data["tier_2b"].get("role_match_score", 0),
            "emphasis_match_score": c_data["tier_2b"].get("emphasis_match_score", 0),
            "role_jsd": c_data["tier_2b"].get("role_distribution_divergence", 0),
            "alignment_f1": c_data["tier_2c"].get("alignment_scores", {}).get("generated", {}).get("alignment_f1", 0),
            "narrative_arc_composite": c_data["tier_2c"].get("narrative_arc_scores", {}).get("generated", {}).get("composite_arc_score", 0),
        }
        complexity_data.append(comp_data)
        print(f"  ✅ Paper {paper_id}: loaded")

    if not consolidated_data:
        print("❌ No evaluation reports found!")
        return

    # Output directory for reports
    out_dir = Path("phd_analysis_outputs")
    out_dir.mkdir(exist_ok=True)

    # 2. Generate Consolidated Report
    print(f"\nConsolidating {len(consolidated_data)} papers...")
    report_text = generate_report(consolidated_data, out_dir)
    report_path = out_dir / "consolidated_report.md"
    report_path.write_text(report_text, encoding="utf-8")
    print(f"✅ Saved consolidated report → {report_path}")

    # 3. Generate Complexity Analysis
    print(f"\n📈 Metric 5: Context Complexity Correlation Analysis")
    correlations = compute_correlations(complexity_data)
    
    class NpEncoder(json.JSONEncoder):
        def default(self, obj):
            import numpy as np
            if isinstance(obj, np.bool_):
                return bool(obj)
            if isinstance(obj, np.floating):
                return float(obj)
            if isinstance(obj, np.integer):
                return int(obj)
            return super().default(obj)
            
    with open(out_dir / "complexity_correlations.json", "w") as f:
        json.dump(correlations, f, indent=2, cls=NpEncoder)

    with open(out_dir / "complexity_analysis_data.csv", "w") as f:
        import csv
        if complexity_data:
            writer = csv.DictWriter(f, fieldnames=complexity_data[0].keys())
            writer.writeheader()
            writer.writerows(complexity_data)

    generate_plots(complexity_data, str(out_dir))
    comp_report = generate_complexity_report(complexity_data, correlations)
    comp_path = out_dir / "complexity_analysis_report.md"
    comp_path.write_text(comp_report, encoding="utf-8")
    print(f"✅ Saved complexity analysis report → {comp_path}")
    print(f"✅ Outputs generated in {out_dir}/")

if __name__ == "__main__":
    main()
