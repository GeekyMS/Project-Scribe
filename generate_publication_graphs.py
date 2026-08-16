import os
import json
import glob
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns
from pathlib import Path
import argparse

# --- Configuration for Publication-Quality Graphs ---
plt.style.use('seaborn-v0_8-whitegrid')
plt.rcParams.update({
    'font.size': 12,
    'axes.labelsize': 14,
    'axes.titlesize': 16,
    'xtick.labelsize': 12,
    'ytick.labelsize': 12,
    'legend.fontsize': 12,
    'figure.dpi': 300,
    'axes.edgecolor': '#333333',
    'font.family': 'sans-serif'
})

def get_latest_eval_report(search_dir):
    """Helper to find the evaluation report JSON in a directory."""
    files = glob.glob(str(Path(search_dir) / "evaluation_report_*.json"))
    if not files:
        return None
    return sorted(files)[-1]  # Get the most recent one if multiple

def plot_ablation_study(ablation_dir: str, out_dir: str):
    """
    Graph 1: Ablation Bar Chart (Length constraint vs Role Match & Hallucinations)
    Expects subdirectories starting with 'run_1_', 'run_2_', 'run_3_'
    """
    print(f"\n📊 Generating Ablation Study Graph from {ablation_dir}...")
    ablation_path = Path(ablation_dir)
    
    runs = []
    for r in [1, 2, 3]:
        dirs = glob.glob(str(ablation_path / f"run_{r}_*"))
        if not dirs:
            print(f"   ⚠️ Could not find run_{r}_* directory. Skipping ablation plot.")
            return
        
        report_file = get_latest_eval_report(dirs[0])
        if not report_file:
            print(f"   ⚠️ Could not find evaluation report in {dirs[0]}. Skipping.")
            return
            
        with open(report_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
            t2b = data.get("tier_2b_argumentative_roles", {})
            runs.append({
                "name": f"Run {r}",
                "role_match": t2b.get("role_match_score", 0.0),
                "one_sided": t2b.get("one_sided_citations", 0),
                "label": ["Unconstrained\n(Full Bib)", "Length Constrained", "Constrained +\nEnriched"][r-1]
            })

    labels = [r["label"] for r in runs]
    role_matches = [r["role_match"] for r in runs]
    one_sided = [r["one_sided"] for r in runs]

    x = np.arange(len(labels))
    width = 0.35

    fig, ax1 = plt.subplots(figsize=(8, 6))

    # Primary axis for Role Match Score (0 to 1)
    color1 = '#2c7bb6'
    ax1.set_xlabel('Pipeline Configuration')
    ax1.set_ylabel('Role Match Score (Higher is Better)', color=color1)
    bars1 = ax1.bar(x - width/2, role_matches, width, label='Role Match Score', color=color1, alpha=0.8)
    ax1.tick_params(axis='y', labelcolor=color1)
    ax1.set_ylim(0, 1.1)

    # Secondary axis for One-sided (hallucinated/omitted) citations
    ax2 = ax1.twinx()
    color2 = '#d7191c'
    ax2.set_ylabel('One-Sided Citations (Lower is Better)', color=color2)
    bars2 = ax2.bar(x + width/2, one_sided, width, label='One-Sided Citations', color=color2, alpha=0.8)
    ax2.tick_params(axis='y', labelcolor=color2)
    ax2.set_ylim(0, max(one_sided) * 1.2 if max(one_sided) > 0 else 10)

    # Add text labels on bars
    for bar in bars1:
        yval = bar.get_height()
        ax1.text(bar.get_x() + bar.get_width()/2, yval + 0.02, f'{yval:.2f}', ha='center', va='bottom', color=color1, fontweight='bold')
    
    for bar in bars2:
        yval = bar.get_height()
        ax2.text(bar.get_x() + bar.get_width()/2, yval + (max(one_sided)*0.02), f'{int(yval)}', ha='center', va='bottom', color=color2, fontweight='bold')

    plt.title('Impact of Length Constraints on Argumentative Fidelity', pad=20)
    plt.xticks(x, labels)
    
    # Combined legend
    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc='upper center', bbox_to_anchor=(0.5, -0.15), ncol=2)

    fig.tight_layout()
    out_path = Path(out_dir) / "ablation_study_chart.pdf"
    plt.savefig(out_path, format='pdf', bbox_inches='tight')
    plt.savefig(out_path.with_suffix('.png'), format='png', bbox_inches='tight')
    plt.close()
    print(f"   ✅ Saved: {out_path}")

def plot_radar_chart(cohort_dir: str, out_dir: str):
    """
    Graph 2: Radar Chart of LLM Judge Scores (averaging across the cohort).
    """
    print(f"\n📊 Generating Radar Chart from {cohort_dir}...")
    cohort_path = Path(cohort_dir)
    
    # Find all paper result directories
    paper_dirs = [d for d in cohort_path.iterdir() if d.is_dir() and (d / "results" / "generation").exists()]
    
    if not paper_dirs:
        print(f"   ⚠️ No valid paper output directories found in {cohort_dir}")
        return

    all_scores = {}
    valid_papers = 0
    
    for pdir in paper_dirs:
        report_file = get_latest_eval_report(pdir / "results" / "generation")
        if not report_file:
            continue
            
        with open(report_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
            scores = data.get("tier_2_judge_scores", {}).get("scores", {})
            if not scores:
                continue
            
            valid_papers += 1
            for dim, score in scores.items():
                all_scores[dim] = all_scores.get(dim, 0) + score
                
    if valid_papers == 0:
         print(f"   ⚠️ No judge scores found.")
         return

    # Average the scores
    dimensions = []
    avg_values = []
    for dim, total in all_scores.items():
        dimensions.append(dim.replace('_', ' ').title())
        avg_values.append(total / valid_papers)
        
    # Radar chart setup
    N = len(dimensions)
    angles = [n / float(N) * 2 * np.pi for n in range(N)]
    angles += angles[:1]
    avg_values += avg_values[:1]
    
    fig, ax = plt.subplots(figsize=(8, 8), subplot_kw=dict(polar=True))
    
    # Draw one axe per variable and add labels
    plt.xticks(angles[:-1], dimensions, size=11)
    
    # Draw ylabels
    ax.set_rlabel_position(0)
    plt.yticks([1, 2, 3, 4, 5], ["1", "2", "3", "4", "5"], color="grey", size=10)
    plt.ylim(0, 5)
    
    # Plot data
    ax.plot(angles, avg_values, linewidth=2, linestyle='solid', color='#1b9e77')
    ax.fill(angles, avg_values, '#1b9e77', alpha=0.25)
    
    plt.title(f'Average AI Generation Quality across {valid_papers} Papers', size=16, y=1.1)
    
    out_path = Path(out_dir) / "judge_scores_radar.pdf"
    plt.savefig(out_path, format='pdf', bbox_inches='tight')
    plt.savefig(out_path.with_suffix('.png'), format='png', bbox_inches='tight')
    plt.close()
    print(f"   ✅ Saved: {out_path}")

def plot_role_distribution(cohort_dir: str, out_dir: str):
    """
    Graph 3: Role Distribution Histogram (Comparing Original vs Generated averages).
    """
    print(f"\n📊 Generating Role Distribution Graph from {cohort_dir}...")
    cohort_path = Path(cohort_dir)
    
    paper_dirs = [d for d in cohort_path.iterdir() if d.is_dir() and (d / "results" / "generation").exists()]
    
    orig_dist = {"FND": 0, "CTR": 0, "GAP": 0, "SUP": 0, "MTD": 0, "CTX": 0}
    gen_dist = {"FND": 0, "CTR": 0, "GAP": 0, "SUP": 0, "MTD": 0, "CTX": 0}
    valid_papers = 0
    
    for pdir in paper_dirs:
        report_file = get_latest_eval_report(pdir / "results" / "generation")
        if not report_file:
            continue
            
        with open(report_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
            t2b = data.get("tier_2b_argumentative_roles", {})
            dist = t2b.get("role_distribution", {})
            
            orig = dist.get("original", {})
            gen = dist.get("generated", {})
            
            if not orig or not gen:
                continue
                
            valid_papers += 1
            for k in orig_dist.keys():
                orig_dist[k] += orig.get(k, 0)
                gen_dist[k] += gen.get(k, 0)

    if valid_papers == 0:
        print(f"   ⚠️ No role distribution data found.")
        return

    # Normalize to percentages across the whole cohort
    total_orig = sum(orig_dist.values()) or 1
    total_gen = sum(gen_dist.values()) or 1
    
    roles = list(orig_dist.keys())
    orig_pct = [orig_dist[r] / total_orig * 100 for r in roles]
    gen_pct = [gen_dist[r] / total_gen * 100 for r in roles]

    x = np.arange(len(roles))
    width = 0.35

    fig, ax = plt.subplots(figsize=(10, 6))
    bars1 = ax.bar(x - width/2, orig_pct, width, label='Human Authors (Original)', color='#999999', alpha=0.8)
    bars2 = ax.bar(x + width/2, gen_pct, width, label='AI Model (Generated)', color='#7570b3', alpha=0.9)

    ax.set_ylabel('Percentage of Citations (%)')
    ax.set_title('Aggregate Argumentative Role Distribution', pad=20)
    ax.set_xticks(x)
    ax.set_xticklabels(roles)
    ax.legend()

    # Formatting percentages on bars
    for bar in bars1 + bars2:
        yval = bar.get_height()
        if yval > 0:
            ax.text(bar.get_x() + bar.get_width()/2, yval + 1, f'{yval:.1f}%', ha='center', va='bottom', fontsize=10)

    fig.tight_layout()
    out_path = Path(out_dir) / "role_distribution_histogram.pdf"
    plt.savefig(out_path, format='pdf', bbox_inches='tight')
    plt.savefig(out_path.with_suffix('.png'), format='png', bbox_inches='tight')
    plt.close()
    print(f"   ✅ Saved: {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate Publication-Quality Graphs")
    parser.add_argument("--ablation-dir", type=str, default="outputs/acl_2026_full_bib", 
                        help="Path to folder containing ablation runs (run_1_*, etc)")
    parser.add_argument("--cohort-dir", type=str, default="research_test_with_phd", 
                        help="Path to cohort folder containing paper subdirectories")
    parser.add_argument("--out-dir", type=str, default="phd_analysis_outputs/graphs", 
                        help="Where to save the generated PDFs/PNGs")
    
    args = parser.parse_args()
    
    os.makedirs(args.out_dir, exist_ok=True)
    
    # Try importing optional graphics libraries
    try:
        import matplotlib
        import seaborn
    except ImportError:
        print("❌ Missing required libraries. Please install them first:")
        print("   pip install matplotlib seaborn numpy pandas")
        exit(1)
        
    print(f"\n🎨 Starting Graph Generation Pipeline")
    print(f"Output directory: {args.out_dir}")
    
    plot_ablation_study(args.ablation_dir, args.out_dir)
    plot_radar_chart(args.cohort_dir, args.out_dir)
    plot_role_distribution(args.cohort_dir, args.out_dir)
    
    print("\n🎉 All graphs generated successfully!")
