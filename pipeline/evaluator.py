"""
Phase 2 — Evaluation Engine.

Three-tier evaluation of generated Related Works sections:
  Tier 1: Deterministic citation metrics (recall, precision, key validation)
  Tier 2: LLM-as-judge scoring on 7 quality dimensions + knowledge leakage
  Tier 3: Text similarity metrics (ROUGE, BERTScore) — supplementary

Also handles comparison across multiple models.
"""

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .abstract_fetcher import parse_bib_file
from .gen_prompts import build_judge_prompt
from .extractor import client, MAX_RETRIES, RETRY_DELAY


# Judge model — should differ from generation model to avoid self-evaluation bias
JUDGE_MODEL = "google/gemini-2.5-pro-preview-06-05"


@dataclass
class CitationMetrics:
    """Tier 1: Deterministic citation metrics."""
    bib_keys: list[str] = field(default_factory=list)
    cited_keys: list[str] = field(default_factory=list)
    hallucinated_keys: list[str] = field(default_factory=list)
    missing_keys: list[str] = field(default_factory=list)
    recall: float = 0.0     # fraction of bib entries actually cited
    precision: float = 1.0  # fraction of cited keys that exist in bib
    total_citations: int = 0  # total citation instances (not unique)

    def to_dict(self) -> dict:
        return {
            "bib_keys": self.bib_keys,
            "cited_keys": self.cited_keys,
            "hallucinated_keys": self.hallucinated_keys,
            "missing_keys": self.missing_keys,
            "recall": round(self.recall, 4),
            "precision": round(self.precision, 4),
            "total_citation_instances": self.total_citations,
            "unique_citations": len(self.cited_keys),
            "bib_entry_count": len(self.bib_keys),
        }


@dataclass
class JudgeScores:
    """Tier 2: LLM-as-judge evaluation."""
    scores: dict = field(default_factory=dict)
    justifications: dict = field(default_factory=dict)
    knowledge_leakage: dict = field(default_factory=dict)
    strengths: list[str] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)
    comparison_summary: str = ""
    judge_model: str = ""
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "scores": self.scores,
            "justifications": self.justifications,
            "knowledge_leakage": self.knowledge_leakage,
            "strengths": self.strengths,
            "weaknesses": self.weaknesses,
            "comparison_summary": self.comparison_summary,
            "judge_model": self.judge_model,
            "error": self.error,
        }


@dataclass
class TextSimilarity:
    """Tier 3: Text similarity metrics."""
    rouge_1: float = 0.0
    rouge_2: float = 0.0
    rouge_l: float = 0.0
    bert_score_precision: float = 0.0
    bert_score_recall: float = 0.0
    bert_score_f1: float = 0.0
    length_ratio: float = 0.0  # generated / ground_truth
    available: bool = False    # False if dependencies not installed

    def to_dict(self) -> dict:
        return {
            "rouge_1": round(self.rouge_1, 4),
            "rouge_2": round(self.rouge_2, 4),
            "rouge_l": round(self.rouge_l, 4),
            "bert_score_precision": round(self.bert_score_precision, 4),
            "bert_score_recall": round(self.bert_score_recall, 4),
            "bert_score_f1": round(self.bert_score_f1, 4),
            "length_ratio": round(self.length_ratio, 4),
            "metrics_available": self.available,
        }


@dataclass
class EvaluationReport:
    """Full evaluation report combining all tiers."""
    model: str = ""
    citation_metrics: CitationMetrics = field(default_factory=CitationMetrics)
    judge_scores: JudgeScores = field(default_factory=JudgeScores)
    text_similarity: TextSimilarity = field(default_factory=TextSimilarity)
    structural_stats: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "tier_1_citation_metrics": self.citation_metrics.to_dict(),
            "tier_2_judge_scores": self.judge_scores.to_dict(),
            "tier_3_text_similarity": self.text_similarity.to_dict(),
            "structural_stats": self.structural_stats,
        }


# ═════════════════════════════════════════════════════════════
# Tier 1: Citation Metrics
# ═════════════════════════════════════════════════════════════

def evaluate_citations(
    generated_text: str,
    bib_path: str | Path,
) -> CitationMetrics:
    """
    Tier 1: Deterministic citation analysis.
    
    Checks:
    - Which bib keys were cited (recall)
    - Whether any hallucinated citations appear (precision)
    - Total citation instances
    """
    bib_path = Path(bib_path)
    metrics = CitationMetrics()
    
    # Get all valid bib keys
    entries = parse_bib_file(bib_path)
    metrics.bib_keys = sorted([e["key"] for e in entries])
    bib_key_set = set(metrics.bib_keys)
    
    # Extract cited keys from the generated text
    cited_keys = set()
    
    # Pattern 1: LaTeX \cite{key} or \cite{key1, key2}
    for match in re.finditer(r'\\cite\{([^}]+)\}', generated_text):
        keys = [k.strip() for k in match.group(1).split(',')]
        cited_keys.update(keys)
    
    # Pattern 2: Markdown <!-- CITATION_KEYS: key1, key2, ... -->
    ck_match = re.search(r'<!--\s*CITATION_KEYS:\s*(.*?)\s*-->', generated_text)
    if ck_match:
        keys = [k.strip() for k in ck_match.group(1).split(',')]
        cited_keys.update(k for k in keys if k)
    
    # Clean up empty strings
    cited_keys.discard("")
    
    metrics.cited_keys = sorted(cited_keys)
    metrics.hallucinated_keys = sorted(cited_keys - bib_key_set)
    metrics.missing_keys = sorted(bib_key_set - cited_keys)
    
    # Count total citation instances (not unique)
    total = len(re.findall(r'\\cite\{[^}]+\}', generated_text))
    if not total:
        # Count author-date style citations
        for key in cited_keys:
            total += generated_text.count(key)
    metrics.total_citations = total
    
    # Compute precision and recall
    if cited_keys:
        valid_cited = cited_keys & bib_key_set
        metrics.precision = len(valid_cited) / len(cited_keys)
    else:
        metrics.precision = 1.0  # No citations = vacuously precise
    
    if bib_key_set:
        cited_from_bib = cited_keys & bib_key_set
        metrics.recall = len(cited_from_bib) / len(bib_key_set)
    else:
        metrics.recall = 1.0
    
    return metrics


# ═════════════════════════════════════════════════════════════
# Tier 2: LLM-as-Judge
# ═════════════════════════════════════════════════════════════

def evaluate_with_judge(
    generated_text: str,
    ground_truth: str,
    paper_text: str,
    bib_content: str,
    judge_model: str = JUDGE_MODEL,
) -> JudgeScores:
    """
    Tier 2: Use a different LLM to evaluate the generated section.
    
    Scores on 7 dimensions + knowledge leakage detection.
    """
    result = JudgeScores(judge_model=judge_model)
    
    prompt = build_judge_prompt(
        generated_text=generated_text,
        ground_truth=ground_truth,
        paper_text=paper_text,
        bib_content=bib_content,
    )
    
    print(f"\n   🧑‍⚖️ Running LLM-as-Judge ({judge_model})...")
    
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=judge_model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                timeout=120.0,
            )
            
            raw = response.choices[0].message.content
            if raw is None:
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY)
                continue
            
            # Strip thinking tags
            raw = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL)
            
            # Parse JSON
            match = re.search(r'\{.*\}', raw, re.DOTALL)
            if match:
                data = json.loads(match.group(0))
                result.scores = data.get("scores", {})
                result.justifications = data.get("score_justifications", {})
                result.knowledge_leakage = data.get("knowledge_leakage", {})
                result.strengths = data.get("strengths", [])
                result.weaknesses = data.get("weaknesses", [])
                result.comparison_summary = data.get("comparison_summary", "")
                
                avg_score = (
                    sum(result.scores.values()) / len(result.scores)
                    if result.scores else 0
                )
                print(f"   ✅ Judge scores (avg: {avg_score:.1f}/5.0)")
                for dim, score in result.scores.items():
                    print(f"      {dim}: {score}/5")
                
                leakage = result.knowledge_leakage.get("severity", "none")
                if leakage != "none":
                    print(f"   ⚠️ Knowledge leakage detected: {leakage}")
                
                break
            else:
                print(f"   ⚠️ No JSON found on attempt {attempt}")
                
        except Exception as e:
            print(f"   ⚠️ Judge call failed on attempt {attempt}: {e}")
            result.error = str(e)
        
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY)
    
    return result


# ═════════════════════════════════════════════════════════════
# Tier 3: Text Similarity Metrics
# ═════════════════════════════════════════════════════════════

def evaluate_text_similarity(
    generated_text: str,
    ground_truth: str,
) -> TextSimilarity:
    """
    Tier 3: ROUGE and BERTScore metrics.
    
    These are supplementary — included for reviewer expectations but
    not the primary evaluation signal (valid paraphrasing is penalized).
    
    Dependencies (rouge-score, bert-score) are optional. If not installed,
    returns empty metrics with available=False.
    """
    result = TextSimilarity()
    
    # Length ratio
    if ground_truth:
        result.length_ratio = len(generated_text) / len(ground_truth)
    
    # Try ROUGE
    try:
        from rouge_score import rouge_scorer
        scorer = rouge_scorer.RougeScorer(
            ['rouge1', 'rouge2', 'rougeL'], use_stemmer=True,
        )
        scores = scorer.score(ground_truth, generated_text)
        result.rouge_1 = scores['rouge1'].fmeasure
        result.rouge_2 = scores['rouge2'].fmeasure
        result.rouge_l = scores['rougeL'].fmeasure
        result.available = True
        print(f"   📊 ROUGE-1: {result.rouge_1:.3f}, ROUGE-2: {result.rouge_2:.3f}, "
              f"ROUGE-L: {result.rouge_l:.3f}")
    except ImportError:
        print(f"   ℹ️ rouge-score not installed — skipping ROUGE metrics")
        print(f"      Install with: pip install rouge-score")
    
    # Try BERTScore
    try:
        import bert_score
        P, R, F1 = bert_score.score(
            [generated_text], [ground_truth],
            lang="en", verbose=False,
        )
        result.bert_score_precision = P[0].item()
        result.bert_score_recall = R[0].item()
        result.bert_score_f1 = F1[0].item()
        result.available = True
        print(f"   📊 BERTScore F1: {result.bert_score_f1:.3f}")
    except ImportError:
        print(f"   ℹ️ bert-score not installed — skipping BERTScore")
        print(f"      Install with: pip install bert-score")
    
    return result


# ═════════════════════════════════════════════════════════════
# Structural Analysis
# ═════════════════════════════════════════════════════════════

def compute_structural_stats(
    generated_text: str,
    ground_truth: str,
) -> dict:
    """Compute structural statistics for comparison."""
    def _stats(text: str) -> dict:
        lines = text.strip().split("\n")
        paragraphs = [p for p in text.split("\n\n") if p.strip()]
        words = text.split()
        
        # Count subsection headings (markdown ## or LaTeX \subsection)
        subsections = len(re.findall(
            r'(?:^#{2,3}\s|\\(?:sub)?section\{)', text, re.MULTILINE,
        ))
        
        return {
            "char_count": len(text),
            "word_count": len(words),
            "line_count": len(lines),
            "paragraph_count": len(paragraphs),
            "subsection_count": subsections,
        }
    
    gen_stats = _stats(generated_text)
    gt_stats = _stats(ground_truth)
    
    return {
        "generated": gen_stats,
        "ground_truth": gt_stats,
        "ratios": {
            "word_count_ratio": (
                gen_stats["word_count"] / gt_stats["word_count"]
                if gt_stats["word_count"] else 0
            ),
            "paragraph_count_ratio": (
                gen_stats["paragraph_count"] / gt_stats["paragraph_count"]
                if gt_stats["paragraph_count"] else 0
            ),
        },
    }


# ═════════════════════════════════════════════════════════════
# Full Evaluation Pipeline
# ═════════════════════════════════════════════════════════════

def run_evaluation(
    generated_text: str,
    ground_truth: str,
    paper_text: str,
    bib_path: str | Path,
    model_name: str = "",
    run_judge: bool = True,
    run_similarity: bool = True,
    outputs_dir: str | Path = "outputs/generation",
) -> EvaluationReport:
    """
    Run the full three-tier evaluation.
    
    Args:
        generated_text: The LLM-generated Related Works section.
        ground_truth: The original Related Works text.
        paper_text: The paper text (for judge context).
        bib_path: Path to the .bib file.
        model_name: Name of the model that generated the text.
        run_judge: Whether to run Tier 2 (LLM-as-judge). Costs API tokens.
        run_similarity: Whether to run Tier 3 (ROUGE/BERTScore).
        outputs_dir: Directory for output files.
    
    Returns:
        EvaluationReport with all tier results.
    """
    bib_path = Path(bib_path)
    outputs_dir = Path(outputs_dir)
    outputs_dir.mkdir(parents=True, exist_ok=True)
    
    report = EvaluationReport(model=model_name)
    
    print(f"\n{'='*60}")
    print(f"📊 Evaluation: {model_name or 'Unknown Model'}")
    print(f"{'='*60}")
    
    # ── Tier 1: Citation Metrics ──
    print(f"\n   📏 Tier 1: Citation Metrics")
    report.citation_metrics = evaluate_citations(generated_text, bib_path)
    cm = report.citation_metrics
    print(f"      Recall: {cm.recall:.1%} ({len(cm.cited_keys)}/{len(cm.bib_keys)} bib entries cited)")
    print(f"      Precision: {cm.precision:.1%} "
          f"({'✅ No hallucinated citations' if not cm.hallucinated_keys else f'⚠️ Hallucinated: {cm.hallucinated_keys}'})")
    if cm.missing_keys:
        print(f"      Missing: {cm.missing_keys[:10]}{'...' if len(cm.missing_keys) > 10 else ''}")
    
    # ── Tier 2: LLM-as-Judge ──
    if run_judge:
        bib_content = bib_path.read_text(encoding="utf-8")
        report.judge_scores = evaluate_with_judge(
            generated_text=generated_text,
            ground_truth=ground_truth,
            paper_text=paper_text,
            bib_content=bib_content,
        )
    else:
        print(f"\n   ⏩ Tier 2: Skipped (--no-judge)")
    
    # ── Tier 3: Text Similarity ──
    if run_similarity:
        print(f"\n   📊 Tier 3: Text Similarity Metrics")
        report.text_similarity = evaluate_text_similarity(
            generated_text, ground_truth,
        )
        print(f"      Length ratio: {report.text_similarity.length_ratio:.2f}x "
              f"(generated/original)")
    else:
        print(f"\n   ⏩ Tier 3: Skipped (--no-similarity)")
    
    # ── Structural Stats ──
    print(f"\n   📐 Structural Analysis")
    report.structural_stats = compute_structural_stats(
        generated_text, ground_truth,
    )
    gs = report.structural_stats["generated"]
    gts = report.structural_stats["ground_truth"]
    print(f"      Generated:    {gs['word_count']} words, {gs['paragraph_count']} paragraphs, "
          f"{gs['subsection_count']} subsections")
    print(f"      Ground Truth: {gts['word_count']} words, {gts['paragraph_count']} paragraphs, "
          f"{gts['subsection_count']} subsections")
    
    # ── Save report ──
    from .generator import _model_shortname
    model_short = _model_shortname(model_name) if model_name else "unknown"
    
    report_path = outputs_dir / f"evaluation_report_{model_short}.json"
    report_path.write_text(
        json.dumps(report.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"\n   📋 Evaluation report → {report_path.name}")
    
    # ── Save side-by-side comparison ──
    _save_side_by_side(generated_text, ground_truth, report, outputs_dir, model_short)
    
    return report


def _save_side_by_side(
    generated: str,
    ground_truth: str,
    report: EvaluationReport,
    outputs_dir: Path,
    model_short: str,
):
    """Generate a human-readable side-by-side comparison document."""
    cm = report.citation_metrics
    js = report.judge_scores
    
    doc = f"""# Side-by-Side Comparison: {report.model}

## Quick Stats

| Metric | Value |
|---|---|
| Citation Recall | {cm.recall:.1%} ({len(cm.cited_keys)}/{len(cm.bib_keys)}) |
| Citation Precision | {cm.precision:.1%} |
| Hallucinated Citations | {len(cm.hallucinated_keys)} |
| Missing Citations | {len(cm.missing_keys)} |
"""

    if js.scores:
        avg = sum(js.scores.values()) / len(js.scores)
        doc += f"| Judge Avg Score | {avg:.1f}/5.0 |\n"
        for dim, score in js.scores.items():
            doc += f"| Judge: {dim} | {score}/5 |\n"
    
    if js.knowledge_leakage:
        severity = js.knowledge_leakage.get("severity", "none")
        doc += f"| Knowledge Leakage | {severity} |\n"
    
    ts = report.text_similarity
    if ts.available:
        doc += f"| ROUGE-L | {ts.rouge_l:.3f} |\n"
        doc += f"| BERTScore F1 | {ts.bert_score_f1:.3f} |\n"
    
    doc += f"| Length Ratio | {ts.length_ratio:.2f}x |\n"
    
    if js.comparison_summary:
        doc += f"\n## Judge Summary\n\n{js.comparison_summary}\n"
    
    if js.strengths:
        doc += f"\n## Strengths\n\n"
        for s in js.strengths:
            doc += f"- {s}\n"
    
    if js.weaknesses:
        doc += f"\n## Weaknesses\n\n"
        for w in js.weaknesses:
            doc += f"- {w}\n"
    
    if cm.hallucinated_keys:
        doc += f"\n## ⚠️ Hallucinated Citations\n\n"
        for k in cm.hallucinated_keys:
            doc += f"- `{k}`\n"
    
    doc += f"\n---\n\n## Generated Related Works\n\n{generated}\n"
    doc += f"\n---\n\n## Original Related Works (Ground Truth)\n\n{ground_truth}\n"
    
    path = outputs_dir / f"side_by_side_{model_short}.md"
    path.write_text(doc, encoding="utf-8")
    print(f"   📋 Side-by-side comparison → {path.name}")
