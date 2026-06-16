"""
Project Scribe Phase 2 — Related Works Generation & Evaluation.

Usage:
    # Full pipeline: generate + evaluate
    python generate_related_work.py --paper outputs/paper_without_related.md \\
                                     --bib outputs/references.bib \\
                                     --ground-truth outputs/related_work.txt

    # Multiple models for comparison
    python generate_related_work.py --paper outputs/paper_without_related.md \\
                                     --bib outputs/references.bib \\
                                     --ground-truth outputs/related_work.txt \\
                                     --model anthropic/claude-sonnet-4-20250514 \\
                                     --model google/gemini-2.5-flash-preview-05-20

    # Export prompt for manual web LLM testing (no API call)
    python generate_related_work.py --paper outputs/paper_without_related.md \\
                                     --bib outputs/references.bib \\
                                     --export-prompt

    # Evaluate an existing generation (from web LLM or previous run)
    python generate_related_work.py --eval-only outputs/generation/generated_related_work.md \\
                                     --ground-truth outputs/related_work.txt \\
                                     --paper outputs/paper_without_related.md \\
                                     --bib outputs/references.bib

    # Generate human evaluation questionnaires
    python generate_related_work.py --eval-only outputs/generation/generated_related_work.md \\
                                     --ground-truth outputs/related_work.txt \\
                                     --human-eval

Outputs (in ./outputs/generation/):
    - generated_related_work_<model>.tex   — LaTeX with \\cite{} commands
    - generated_related_work_<model>.md    — Markdown with author-date citations
    - evaluation_report_<model>.json       — Full three-tier evaluation
    - side_by_side_<model>.md              — Human-readable comparison
    - prompt_<model>.txt                   — Exact prompt sent to the model
    - prompt_manual_web_llm.txt            — Prompt for copy-paste into web LLMs
    - generation_log_<model>.json          — Token usage, timing, metadata
    - enrichment_cache.json                — Cached Semantic Scholar abstracts
    - human_eval_blind_ab.md               — Blind A/B questionnaire
    - human_eval_likert.md                 — Likert scoring form
    - human_eval_schema.json               — JSON schema for responses
    - ground_truth.txt                     — Copy of original for comparison
"""

import sys
import argparse
from pathlib import Path

from pipeline.generator import generate_related_work, DEFAULT_MODEL
from pipeline.evaluator import run_evaluation
from pipeline.human_eval import generate_human_eval_materials


def main():
    parser = argparse.ArgumentParser(
        description="Project Scribe Phase 2 — Related Works Generation & Evaluation.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    
    # Input files
    parser.add_argument(
        "--paper",
        help="Path to paper_without_related.md (paper text without Related Works)",
    )
    parser.add_argument(
        "--bib",
        help="Path to references.bib (bibliography of cited papers)",
    )
    parser.add_argument(
        "--ground-truth",
        help="Path to related_work.txt (original Related Works for evaluation)",
    )
    
    # Generation options
    parser.add_argument(
        "--model",
        action="append",
        dest="models",
        help="Model to use for generation (can specify multiple). "
             f"Default: {DEFAULT_MODEL}",
    )
    parser.add_argument(
        "--no-enrich",
        action="store_true",
        help="Skip Semantic Scholar abstract enrichment (default: enrich is ON)",
    )
    parser.add_argument(
        "--format",
        choices=["latex", "markdown", "both"],
        default="both",
        help="Output format (default: both)",
    )
    
    # Output
    parser.add_argument(
        "--output-dir",
        default="outputs/generation",
        help="Directory for output files (default: outputs/generation)",
    )
    
    # Mode flags
    parser.add_argument(
        "--export-prompt",
        action="store_true",
        help="Only export the prompt (for manual use in web LLMs). No API call.",
    )
    parser.add_argument(
        "--eval-only",
        metavar="GENERATED_FILE",
        help="Skip generation — evaluate an existing file against ground truth.",
    )
    parser.add_argument(
        "--human-eval",
        action="store_true",
        help="Generate human evaluation questionnaires (blind A/B + Likert).",
    )
    parser.add_argument(
        "--no-judge",
        action="store_true",
        help="Skip Tier 2 LLM-as-judge evaluation (saves API tokens).",
    )
    parser.add_argument(
        "--no-similarity",
        action="store_true",
        help="Skip Tier 3 text similarity metrics (ROUGE/BERTScore).",
    )
    
    args = parser.parse_args()
    
    outputs_dir = Path(args.output_dir)
    
    # ── Mode: Export Prompt Only ──
    if args.export_prompt:
        if not args.paper or not args.bib:
            print("❌ --export-prompt requires --paper and --bib")
            sys.exit(1)
        
        _export_prompt_only(args.paper, args.bib, args.no_enrich, outputs_dir)
        return
    
    # ── Mode: Eval Only ──
    if args.eval_only:
        if not args.ground_truth:
            print("❌ --eval-only requires --ground-truth")
            sys.exit(1)
        
        generated_text = Path(args.eval_only).read_text(encoding="utf-8")
        ground_truth = Path(args.ground_truth).read_text(encoding="utf-8")
        paper_text = ""
        if args.paper:
            paper_text = Path(args.paper).read_text(encoding="utf-8")
        
        if args.bib:
            report = run_evaluation(
                generated_text=generated_text,
                ground_truth=ground_truth,
                paper_text=paper_text,
                bib_path=args.bib,
                model_name="manual_eval",
                run_judge=not args.no_judge,
                run_similarity=not args.no_similarity,
                outputs_dir=outputs_dir,
            )
        
        if args.human_eval:
            generate_human_eval_materials(
                generated_text=generated_text,
                ground_truth=ground_truth,
                model_name="manual_eval",
                outputs_dir=outputs_dir,
            )
        
        return
    
    # ── Mode: Full Pipeline (Generate + Evaluate) ──
    if not args.paper or not args.bib:
        print("❌ Requires --paper and --bib")
        print("   Run with --help for usage information")
        sys.exit(1)
    
    if not Path(args.paper).exists():
        print(f"❌ Paper file not found: {args.paper}")
        sys.exit(1)
    if not Path(args.bib).exists():
        print(f"❌ Bib file not found: {args.bib}")
        sys.exit(1)
    
    models = args.models or [DEFAULT_MODEL]
    ground_truth = None
    if args.ground_truth:
        ground_truth = Path(args.ground_truth).read_text(encoding="utf-8")
    
    # Run for each model
    for model in models:
        print(f"\n{'═'*60}")
        print(f"  Running model: {model}")
        print(f"{'═'*60}")
        
        gen_result = generate_related_work(
            paper_md_path=args.paper,
            bib_path=args.bib,
            model=model,
            output_format=args.format,
            enrich=not args.no_enrich,
            outputs_dir=outputs_dir,
        )
        
        # Use whichever output is available for evaluation
        generated_text = gen_result.markdown_output or gen_result.latex_output or gen_result.raw_output
        
        if not generated_text:
            print(f"   ❌ No output generated for {model}")
            continue
        
        # Evaluate if ground truth is available
        if ground_truth:
            paper_text = Path(args.paper).read_text(encoding="utf-8")
            run_evaluation(
                generated_text=generated_text,
                ground_truth=ground_truth,
                paper_text=paper_text,
                bib_path=args.bib,
                model_name=model,
                run_judge=not args.no_judge,
                run_similarity=not args.no_similarity,
                outputs_dir=outputs_dir,
            )
            
            if args.human_eval:
                generate_human_eval_materials(
                    generated_text=generated_text,
                    ground_truth=ground_truth,
                    model_name=model,
                    outputs_dir=outputs_dir,
                )
        else:
            print(f"\n   ℹ️ No --ground-truth provided — skipping evaluation")
    
    print(f"\n{'='*60}")
    print(f"🏁 Done! Outputs in: {outputs_dir}")
    print(f"{'='*60}\n")


def _export_prompt_only(paper_path, bib_path, no_enrich, outputs_dir):
    """Export prompt files without making any API calls."""
    from pipeline.abstract_fetcher import enrich_bib_entries
    from pipeline.gen_prompts import build_generation_prompt, build_manual_prompt
    
    outputs_dir.mkdir(parents=True, exist_ok=True)
    
    paper_text = Path(paper_path).read_text(encoding="utf-8")
    bib_content = Path(bib_path).read_text(encoding="utf-8")
    
    enriched_entries = None
    if not no_enrich:
        cache_path = outputs_dir / "enrichment_cache.json"
        enriched_entries = enrich_bib_entries(bib_path, cache_path=cache_path)
    
    # Save API prompt
    api_prompt = build_generation_prompt(
        paper_text=paper_text,
        bib_content=bib_content,
        enriched_entries=enriched_entries,
        output_format="both",
    )
    api_path = outputs_dir / "prompt_api.txt"
    api_path.write_text(api_prompt, encoding="utf-8")
    print(f"✅ API prompt saved → {api_path} ({len(api_prompt)} chars)")
    
    # Save manual prompt (for web LLMs)
    manual_prompt = build_manual_prompt(
        paper_text=paper_text,
        bib_content=bib_content,
        enriched_entries=enriched_entries,
    )
    manual_path = outputs_dir / "prompt_manual_web_llm.txt"
    manual_path.write_text(manual_prompt, encoding="utf-8")
    print(f"✅ Manual prompt (for web LLMs) saved → {manual_path} ({len(manual_prompt)} chars)")
    print(f"\n📋 To use with a web LLM:")
    print(f"   1. Open ChatGPT, Claude, or Gemini in your browser")
    print(f"   2. Copy the contents of {manual_path}")
    print(f"   3. Paste and send")
    print(f"   4. Save the response as a .md file")
    print(f"   5. Run evaluation: python generate_related_work.py --eval-only <response.md> "
          f"--ground-truth outputs/related_work.txt --bib {bib_path} --paper {paper_path}")


if __name__ == "__main__":
    main()
