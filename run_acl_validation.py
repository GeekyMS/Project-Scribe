"""
One-off: run the 3 ACL claude-opus-5 generation variants (no-limit,
length-limited, length-limited+enriched) through the same Tier 4 +
synthetic-negative pipeline used for the main 7-paper campaign, for
direct comparison against the length-limit findings discussed earlier.
Not part of the general campaign script since these come from
outputs/acl_2026_full_bib/, not research_test_with_phd/.
"""
import random
from pathlib import Path

from run_validation_campaign import _run_one_paper
from pipeline.evaluator import JUDGE_MODEL

papers = [
    {
        "id": "acl_run1_no_enrich_no_limit",
        "tex": Path("outputs/acl_2026_full_bib/run_1_no_enrich_no_limit/generated_related_work_claude-opus-5.tex"),
        "bib": Path("outputs/acl_2026_full_bib/references_full.bib"),
        "ground_truth": Path("outputs/acl_2026_full_bib/related_work.txt"),
    },
    {
        "id": "acl_run2_no_enrich_limit",
        "tex": Path("outputs/acl_2026_full_bib/run_2_no_enrich_limit/generated_related_work_claude-opus-5.tex"),
        "bib": Path("outputs/acl_2026_full_bib/references_full.bib"),
        "ground_truth": Path("outputs/acl_2026_full_bib/related_work.txt"),
    },
    {
        "id": "acl_run3_enrich_limit",
        "tex": Path("outputs/acl_2026_full_bib/run_3_enrich_limit/generated_related_work_claude-opus-5.tex"),
        "bib": Path("outputs/acl_2026_full_bib/references_full.bib"),
        "ground_truth": Path("outputs/acl_2026_full_bib/related_work.txt"),
    },
]

output_dir = Path("outputs/phase2_campaign")
rng = random.Random(42)

for paper in papers:
    print(f"\n{'='*70}\n{paper['id']}\n{'='*70}")
    paper_out_dir = output_dir / paper["id"]
    record = _run_one_paper(paper, paper_out_dir, JUDGE_MODEL, 8, rng)
    print(f"status: {record['status']}")
    if "tier4" in record:
        print(f"tier4: {record['tier4']}")
    if "synthetic_negatives" in record:
        print(f"synthetic_negatives: {record['synthetic_negatives']}")

print("\nDONE")
