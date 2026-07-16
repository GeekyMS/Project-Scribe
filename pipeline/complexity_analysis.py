"""
Phase 2 — Context Complexity Correlation Analysis (Metric 5).

Performs cross-paper analysis to test the hypothesis:
  "Argumentative quality degrades as input complexity increases,
   even though citation recall stays high."

This module:
  1. Collects independent variables (input complexity) and dependent
     variables (argumentative quality scores) across all evaluated papers.
  2. Computes Pearson and Spearman correlations for each pair.
  3. Generates scatter plots and a summary report.

Usage:
    python -m pipeline.complexity_analysis --base-dir test_outputs

Dependencies:
    - matplotlib (optional, for plots)
    - scipy (optional, for statistical tests)
"""

import csv
import json
from pathlib import Path


def collect_paper_data(paper_id: int, base_dir: str = "test_outputs") -> dict | None:
    """
    Collect all complexity and quality variables for a single paper.

    Reads from the standard output directory structure:
      {base_dir}/paper{id}_run/generation/

    Returns:
        Dict of all variables, or None if data is missing.
    """
    report_dir = Path(base_dir) / f"paper{paper_id}_run" / "generation"

    # ── Load evaluation report ──
    reports = list(report_dir.glob("evaluation_report_*.json"))
    if not reports:
        return None

    with open(reports[0], "r") as f:
        eval_data = json.load(f)

    # ── Load generation log ──
    gen_logs = list(report_dir.glob("generation_log_*.json"))
    gen_log = {}
    if gen_logs:
        with open(gen_logs[0], "r") as f:
            gen_log = json.load(f)

    # ── Extract independent variables (input complexity) ──
    tier1 = eval_data.get("tier_1_citation_metrics", {})
    structural = eval_data.get("structural_stats", {})
    gt = structural.get("ground_truth", {})

    independent = {
        # Number of references provided
        "bib_entry_count": len(tier1.get("bib_keys", [])),
        # Prompt size
        "prompt_tokens": gen_log.get("prompt_tokens", 0),
        "paper_text_length": gen_log.get("paper_text_length", 0),
        # Ground truth complexity
        "gt_word_count": gt.get("word_count", 0),
        "gt_paragraph_count": gt.get("paragraph_count", 0),
        # Total tokens used
        "total_tokens": gen_log.get("total_tokens", 0),
    }

    # ── Extract dependent variables (quality scores) ──
    tier2 = eval_data.get("tier_2_judge_scores", {})
    tier2b = eval_data.get("tier_2b_argumentative_roles", {})
    tier2c = eval_data.get("tier_2c_coherence", {})
    tier3 = eval_data.get("tier_3_text_similarity", {})

    scores = tier2.get("scores", {})
    judge_avg = sum(scores.values()) / len(scores) if scores else 0

    gen_alignment = tier2c.get("alignment_scores", {}).get("generated", {})
    gen_arc = tier2c.get("narrative_arc_scores", {}).get("generated", {})

    dependent = {
        # Existing metrics
        "citation_recall": tier1.get("recall", 0),
        "citation_precision": tier1.get("precision", 0),
        "judge_avg": round(judge_avg, 2),
        "positioning_score": scores.get("positioning", 0),
        "depth_score": scores.get("depth", 0),
        # New argumentative metrics (Metrics 1-2)
        "role_match_score": tier2b.get("role_match_score", 0),
        "emphasis_match_score": tier2b.get("emphasis_match_score", 0),
        "role_distribution_jsd": tier2b.get("role_distribution_divergence", 0),
        # New coherence metrics (Metrics 3-4)
        "challenge_coverage": gen_alignment.get("challenge_coverage", 0),
        "citation_relevance": gen_alignment.get("citation_relevance", 0),
        "alignment_f1": gen_alignment.get("alignment_f1", 0),
        "narrative_arc_composite": gen_arc.get("composite_arc_score", 0),
        # Text similarity
        "length_ratio": tier3.get("length_ratio", 0),
    }

    return {
        "paper_id": paper_id,
        "model": eval_data.get("model", "unknown"),
        **independent,
        **dependent,
    }


def compute_correlations(data: list[dict]) -> dict:
    """
    Compute correlation matrix between independent and dependent variables.

    Returns a dict of correlation results. Uses scipy if available,
    otherwise falls back to a simple Pearson implementation.
    """
    if len(data) < 3:
        print(f"   ⚠️ Only {len(data)} papers — too few for meaningful correlations")
        return {"error": f"Only {len(data)} papers, need at least 3"}

    independent_vars = [
        "bib_entry_count", "prompt_tokens", "paper_text_length",
        "gt_word_count", "total_tokens",
    ]
    dependent_vars = [
        "citation_recall", "judge_avg", "positioning_score",
        "role_match_score", "emphasis_match_score", "role_distribution_jsd",
        "alignment_f1", "narrative_arc_composite",
    ]

    results = {}

    # Try scipy for proper statistical tests
    try:
        from scipy import stats

        for iv in independent_vars:
            iv_values = [d[iv] for d in data if d.get(iv) is not None]
            for dv in dependent_vars:
                dv_values = [d[dv] for d in data if d.get(dv) is not None]

                if len(iv_values) != len(dv_values) or len(iv_values) < 3:
                    continue

                # Skip if all values are the same (no variance)
                if len(set(iv_values)) < 2 or len(set(dv_values)) < 2:
                    continue

                pearson_r, pearson_p = stats.pearsonr(iv_values, dv_values)
                spearman_r, spearman_p = stats.spearmanr(iv_values, dv_values)

                key = f"{iv}_vs_{dv}"
                results[key] = {
                    "independent": iv,
                    "dependent": dv,
                    "n": len(iv_values),
                    "pearson_r": round(pearson_r, 4),
                    "pearson_p": round(pearson_p, 4),
                    "spearman_r": round(spearman_r, 4),
                    "spearman_p": round(spearman_p, 4),
                    "significant_pearson": pearson_p < 0.05,
                    "significant_spearman": spearman_p < 0.05,
                }

        print(f"   ✅ Computed {len(results)} correlation pairs (scipy)")

    except ImportError:
        print("   ℹ️ scipy not installed — using simple Pearson correlation")
        print("      Install with: pip install scipy")

        # Fallback: simple Pearson
        def _pearson(x, y):
            n = len(x)
            if n < 3:
                return 0.0
            mean_x = sum(x) / n
            mean_y = sum(y) / n
            cov = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
            std_x = (sum((xi - mean_x) ** 2 for xi in x)) ** 0.5
            std_y = (sum((yi - mean_y) ** 2 for yi in y)) ** 0.5
            if std_x == 0 or std_y == 0:
                return 0.0
            return cov / (std_x * std_y)

        for iv in independent_vars:
            iv_values = [d[iv] for d in data]
            for dv in dependent_vars:
                dv_values = [d[dv] for d in data]
                if len(set(iv_values)) < 2 or len(set(dv_values)) < 2:
                    continue
                r = _pearson(iv_values, dv_values)
                key = f"{iv}_vs_{dv}"
                results[key] = {
                    "independent": iv,
                    "dependent": dv,
                    "n": len(iv_values),
                    "pearson_r": round(r, 4),
                    "pearson_p": None,
                    "note": "p-values require scipy",
                }

        print(f"   ✅ Computed {len(results)} correlation pairs (simple)")

    return results


def generate_plots(
    data: list[dict],
    output_dir: str | Path = "test_outputs",
) -> list[str]:
    """
    Generate scatter plots for key complexity-quality relationships.

    Returns list of paths to generated plot files.
    """
    output_dir = Path(output_dir)
    plot_paths = []

    try:
        import matplotlib
        matplotlib.use("Agg")  # Non-interactive backend
        import matplotlib.pyplot as plt
    except ImportError:
        print("   ℹ️ matplotlib not installed — skipping plots")
        print("      Install with: pip install matplotlib")
        return plot_paths

    # Key relationships to plot
    plot_configs = [
        {
            "x": "prompt_tokens", "y": "role_match_score",
            "title": "Context Size vs Argumentative Role Accuracy",
            "xlabel": "Prompt Tokens", "ylabel": "Role Match Score",
        },
        {
            "x": "bib_entry_count", "y": "emphasis_match_score",
            "title": "Number of References vs Selective Emphasis",
            "xlabel": "Bib Entry Count", "ylabel": "Emphasis Match Score",
        },
        {
            "x": "prompt_tokens", "y": "alignment_f1",
            "title": "Context Size vs Challenge-Citation Alignment",
            "xlabel": "Prompt Tokens", "ylabel": "Alignment F1",
        },
        {
            "x": "prompt_tokens", "y": "narrative_arc_composite",
            "title": "Context Size vs Narrative Arc Quality",
            "xlabel": "Prompt Tokens", "ylabel": "Narrative Arc Score",
        },
        {
            "x": "prompt_tokens", "y": "citation_recall",
            "title": "Context Size vs Citation Recall (Control)",
            "xlabel": "Prompt Tokens", "ylabel": "Citation Recall",
        },
        {
            "x": "bib_entry_count", "y": "role_match_score",
            "title": "Number of References vs Role Match Score",
            "xlabel": "Bib Entry Count", "ylabel": "Role Match Score",
        },
    ]

    for config in plot_configs:
        x_vals = [d.get(config["x"], 0) for d in data]
        y_vals = [d.get(config["y"], 0) for d in data]
        labels = [f"P{d['paper_id']}" for d in data]

        if not any(y_vals):
            continue  # Skip if no data for this metric

        fig, ax = plt.subplots(figsize=(8, 5))
        ax.scatter(x_vals, y_vals, s=60, alpha=0.7, edgecolors="black", linewidth=0.5)

        # Label each point
        for i, label in enumerate(labels):
            ax.annotate(
                label, (x_vals[i], y_vals[i]),
                textcoords="offset points", xytext=(5, 5),
                fontsize=8, alpha=0.7,
            )

        ax.set_xlabel(config["xlabel"])
        ax.set_ylabel(config["ylabel"])
        ax.set_title(config["title"])
        ax.grid(True, alpha=0.3)

        # Add trend line
        try:
            import numpy as np
            if len(set(x_vals)) > 1:
                z = np.polyfit(x_vals, y_vals, 1)
                p = np.poly1d(z)
                x_line = np.linspace(min(x_vals), max(x_vals), 100)
                ax.plot(x_line, p(x_line), "--", color="red", alpha=0.5, label="Trend")
                ax.legend()
        except ImportError:
            pass  # No numpy, no trend line

        filename = f"plot_{config['x']}_vs_{config['y']}.png"
        path = output_dir / filename
        fig.tight_layout()
        fig.savefig(path, dpi=150)
        plt.close(fig)
        plot_paths.append(str(path))
        print(f"   📊 Saved plot → {filename}")

    return plot_paths


def generate_complexity_report(
    data: list[dict],
    correlations: dict,
    output_dir: str | Path = "test_outputs",
) -> str:
    """
    Generate a markdown report summarizing the complexity analysis.

    Returns the report text.
    """
    output_dir = Path(output_dir)

    lines = []
    lines.append("# Context Complexity Correlation Analysis")
    lines.append("")
    lines.append(f"**Papers analyzed**: {len(data)}")
    lines.append(f"**Model**: {data[0]['model'] if data else 'N/A'}")
    lines.append("")
    lines.append("## Hypothesis")
    lines.append("")
    lines.append("> As input context complexity grows (more references, longer prompts),")
    lines.append("> the LLM's ability to maintain *argumentative coherence* degrades,")
    lines.append("> even though *citation recall* stays high.")
    lines.append("")

    # ── Summary table ──
    lines.append("## Paper-Level Data")
    lines.append("")
    lines.append("| Paper | Bib Entries | Prompt Tokens | Recall | "
                 "Role Match | Emphasis | Align F1 | Arc Score |")
    lines.append("|-------|-------------|---------------|--------|"
                 "------------|----------|----------|-----------|")
    for d in data:
        lines.append(
            f"| {d['paper_id']} | {d.get('bib_entry_count', 0)} | "
            f"{d.get('prompt_tokens', 0):,} | "
            f"{d.get('citation_recall', 0):.0%} | "
            f"{d.get('role_match_score', 0):.2f} | "
            f"{d.get('emphasis_match_score', 0):.2f} | "
            f"{d.get('alignment_f1', 0):.2f} | "
            f"{d.get('narrative_arc_composite', 0):.2f} |"
        )
    lines.append("")

    # ── Correlation results ──
    if "error" not in correlations:
        lines.append("## Correlation Results")
        lines.append("")

        # Find significant correlations
        significant = {
            k: v for k, v in correlations.items()
            if v.get("significant_pearson") or v.get("significant_spearman")
        }

        if significant:
            lines.append("### Significant Correlations (p < 0.05)")
            lines.append("")
            lines.append("| Independent | Dependent | Pearson r | p | "
                         "Spearman r | p |")
            lines.append("|-------------|-----------|-----------|---|"
                         "------------|---|")
            for key, v in sorted(
                significant.items(), key=lambda x: abs(x[1].get("pearson_r", 0)),
                reverse=True,
            ):
                lines.append(
                    f"| {v['independent']} | {v['dependent']} | "
                    f"{v['pearson_r']:.3f} | {v.get('pearson_p', '?'):.3f} | "
                    f"{v.get('spearman_r', '?'):.3f} | "
                    f"{v.get('spearman_p', '?'):.3f} |"
                )
            lines.append("")
        else:
            lines.append("### No statistically significant correlations found.")
            lines.append("")
            lines.append(f"> With only {len(data)} papers, this is expected. "
                         f"Consider expanding to 20-30 papers.")
            lines.append("")

        # All correlations table
        lines.append("### All Correlations")
        lines.append("")
        lines.append("| Independent → Dependent | Pearson r | "
                     "Spearman r | n |")
        lines.append("|-------------------------|-----------|"
                     "------------|---|")
        for key, v in sorted(
            correlations.items(),
            key=lambda x: abs(x[1].get("pearson_r", 0)),
            reverse=True,
        ):
            r = v.get("pearson_r", 0)
            marker = " **" if abs(r) > 0.5 else ""
            lines.append(
                f"| {v['independent']} → {v['dependent']} | "
                f"{r:.3f}{marker} | "
                f"{v.get('spearman_r', '?')} | {v.get('n', '?')} |"
            )
        lines.append("")
    else:
        lines.append(f"## ⚠️ {correlations['error']}")
        lines.append("")

    # ── Key findings ──
    lines.append("## Key Findings")
    lines.append("")

    # Check if recall stays high while argumentative metrics vary
    recalls = [d.get("citation_recall", 0) for d in data]
    role_scores = [d.get("role_match_score", 0) for d in data]

    avg_recall = sum(recalls) / len(recalls) if recalls else 0
    avg_role = sum(role_scores) / len(role_scores) if role_scores else 0

    if avg_recall > 0.9 and avg_role < 0.7:
        lines.append("1. ✅ **Hypothesis supported**: Citation recall remains high "
                      f"({avg_recall:.0%}) while argumentative role accuracy is "
                      f"significantly lower ({avg_role:.2f}). LLMs can *find* "
                      f"the right papers but don't always use them for the right "
                      f"*rhetorical purpose*.")
    elif avg_recall > 0.9 and avg_role >= 0.7:
        lines.append(f"1. ⚠️ **Partial support**: Both citation recall ({avg_recall:.0%}) "
                      f"and role accuracy ({avg_role:.2f}) are relatively high. "
                      f"The gap may be narrower than hypothesized.")
    else:
        lines.append(f"1. ℹ️ **Inconclusive**: Recall={avg_recall:.0%}, "
                      f"Role Match={avg_role:.2f}. More data needed.")

    lines.append("")

    report_text = "\n".join(lines)

    # Save report
    report_path = output_dir / "complexity_analysis_report.md"
    report_path.write_text(report_text, encoding="utf-8")
    print(f"✅ Saved complexity analysis report → {report_path}")

    return report_text


def run_complexity_analysis(base_dir: str = "test_outputs") -> dict:
    """
    Run the full Metric 5 cross-paper complexity correlation analysis.

    This scans all paper runs in the base directory, collects data,
    computes correlations, generates plots, and produces a report.

    Returns:
        Dict with collected data, correlations, and file paths.
    """
    base_path = Path(base_dir)

    # ── Discover paper runs ──
    paper_ids = []
    for d in sorted(base_path.iterdir()):
        if d.is_dir() and d.name.startswith("paper") and d.name.endswith("_run"):
            try:
                pid = int(d.name.replace("paper", "").replace("_run", ""))
                paper_ids.append(pid)
            except ValueError:
                continue

    print(f"\n{'='*60}")
    print(f"📈 Metric 5: Context Complexity Correlation Analysis")
    print(f"{'='*60}")
    print(f"   Found paper runs: {paper_ids}")

    # ── Collect data ──
    data = []
    for pid in sorted(paper_ids):
        d = collect_paper_data(pid, base_dir)
        if d:
            data.append(d)
            print(f"   ✅ Paper {pid}: collected")
        else:
            print(f"   ⏭️  Paper {pid}: no evaluation data found")

    if not data:
        print("   ❌ No data found!")
        return {"error": "No data found"}

    # ── Save raw data as CSV ──
    csv_path = base_path / "complexity_analysis_data.csv"
    if data:
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=data[0].keys())
            writer.writeheader()
            writer.writerows(data)
        print(f"   📋 Raw data → {csv_path.name}")

    # ── Compute correlations ──
    print(f"\n   Computing correlations across {len(data)} papers...")
    correlations = compute_correlations(data)

    # ── Save correlations ──
    corr_path = base_path / "complexity_correlations.json"
    corr_path.write_text(
        json.dumps(correlations, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"   📋 Correlations → {corr_path.name}")

    # ── Generate plots ──
    plot_paths = generate_plots(data, base_path)

    # ── Generate report ──
    generate_complexity_report(data, correlations, base_path)

    return {
        "paper_count": len(data),
        "data": data,
        "correlations": correlations,
        "plot_paths": plot_paths,
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="Run context complexity correlation analysis (Metric 5)",
    )
    parser.add_argument(
        "--base-dir", default="test_outputs",
        help="Base directory containing paper*_run/ directories",
    )
    args = parser.parse_args()
    run_complexity_analysis(args.base_dir)
