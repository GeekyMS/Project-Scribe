"""
Phase 2 — Evaluation Engine.

Multi-tier evaluation of generated Related Works sections:
  Tier 1:  Deterministic citation metrics (recall, precision, key validation)
  Tier 2:  LLM-as-judge scoring on 7 quality dimensions + knowledge leakage
  Tier 2B: LLM-as-judge argumentative role analysis (per-citation)
  Tier 2C: LLM-as-judge argumentative coherence (section-level)
  Tier 3:  Text similarity metrics (ROUGE, BERTScore) — supplementary

Also handles comparison across multiple models.
"""

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .abstract_fetcher import parse_bib_file
from .gen_prompts import (
    build_judge_prompt,
    build_argumentative_role_prompt,
    build_coherence_prompt,
)
from .extractor import client, MAX_RETRIES, RETRY_DELAY


# Judge model — should differ from generation model to avoid self-evaluation bias
JUDGE_MODEL = "google/gemini-2.5-pro-preview-06-05"

# Valid argumentative role codes for validation
VALID_ROLES = {"FND", "CTR", "GAP", "SUP", "MTD", "CTX", "ABSENT"}

# Adjacent role pairs (roles that are rhetorically close)
ADJACENT_ROLES = {
    frozenset({"FND", "SUP"}),  # foundation and support both affirm
    frozenset({"CTR", "GAP"}),  # contrast and gap both critique
    frozenset({"SUP", "CTX"}),  # support and context both background
    frozenset({"FND", "MTD"}),  # foundation and method both build-upon
}


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
    prompt_hash: str = ""
    decoding_params: dict = field(default_factory=dict)
    timestamp: str = ""
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
            "prompt_hash": self.prompt_hash,
            "decoding_params": self.decoding_params,
            "timestamp": self.timestamp,
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
class ArgumentativeRoleScores:
    """
    Tier 2B: Per-citation argumentative role analysis.

    Captures two metrics:
      Metric 1 — Role Classification: Does the generated text use each
        citation for the same rhetorical purpose as the original?
      Metric 2 — Selective Emphasis: Does the generated text highlight
        the same aspect of each cited paper as the original?
    """
    # Per-citation role data from the judge
    citation_roles: dict = field(default_factory=dict)
    emphasis_comparisons: dict = field(default_factory=dict)
    role_distribution: dict = field(default_factory=dict)

    # Computed aggregate scores
    role_match_score: float = 0.0       # Metric 1: avg match across citations
    emphasis_match_score: float = 0.0   # Metric 2: avg emphasis match
    role_distribution_divergence: float = 0.0  # JSD between role distributions
    alignment_coverage: float = 0.0     # Fraction of generated citations aligned to original

    # Counts for transparency
    total_citations_analyzed: int = 0
    exact_role_matches: int = 0
    adjacent_role_matches: int = 0
    role_mismatches: int = 0
    one_sided_citations: int = 0
    precise_emphasis: int = 0
    partial_emphasis: int = 0
    generic_emphasis: int = 0

    summary: str = ""
    judge_model: str = ""
    prompt_hash: str = ""
    decoding_params: dict = field(default_factory=dict)
    timestamp: str = ""
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "citation_roles": self.citation_roles,
            "emphasis_comparisons": self.emphasis_comparisons,
            "role_distribution": self.role_distribution,
            "role_match_score": round(self.role_match_score, 4),
            "emphasis_match_score": round(self.emphasis_match_score, 4),
            "role_distribution_divergence": round(self.role_distribution_divergence, 4),
            "alignment_coverage": round(self.alignment_coverage, 4),
            "total_citations_analyzed": self.total_citations_analyzed,
            "exact_role_matches": self.exact_role_matches,
            "adjacent_role_matches": self.adjacent_role_matches,
            "role_mismatches": self.role_mismatches,
            "one_sided_citations": self.one_sided_citations,
            "precise_emphasis": self.precise_emphasis,
            "partial_emphasis": self.partial_emphasis,
            "generic_emphasis": self.generic_emphasis,
            "summary": self.summary,
            "judge_model": self.judge_model,
            "prompt_hash": self.prompt_hash,
            "decoding_params": self.decoding_params,
            "timestamp": self.timestamp,
            "error": self.error,
        }


@dataclass
class CoherenceScores:
    """
    Tier 2C: Section-level argumentative coherence.

    Captures two metrics:
      Metric 3 — Challenge-Citation Alignment: Are citations connected
        to the paper's stated research challenges?
      Metric 4 — Narrative Arc: Does the section follow a logical
        argumentative progression?
    """
    # Challenge data
    challenges: list[dict] = field(default_factory=list)
    challenge_citation_mapping: dict = field(default_factory=dict)

    # Alignment scores (Metric 3)
    generated_alignment: dict = field(default_factory=dict)
    original_alignment: dict = field(default_factory=dict)

    # Narrative arc data (Metric 4)
    generated_arc: dict = field(default_factory=dict)
    original_arc: dict = field(default_factory=dict)
    generated_arc_scores: dict = field(default_factory=dict)
    original_arc_scores: dict = field(default_factory=dict)

    summary: str = ""
    judge_model: str = ""
    prompt_hash: str = ""
    decoding_params: dict = field(default_factory=dict)
    timestamp: str = ""
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "challenges": self.challenges,
            "challenge_citation_mapping": self.challenge_citation_mapping,
            "alignment_scores": {
                "generated": self.generated_alignment,
                "original": self.original_alignment,
            },
            "narrative_arc": {
                "generated": self.generated_arc,
                "original": self.original_arc,
            },
            "narrative_arc_scores": {
                "generated": self.generated_arc_scores,
                "original": self.original_arc_scores,
            },
            "summary": self.summary,
            "judge_model": self.judge_model,
            "prompt_hash": self.prompt_hash,
            "decoding_params": self.decoding_params,
            "timestamp": self.timestamp,
            "error": self.error,
        }


@dataclass
class EvaluationReport:
    """Full evaluation report combining all tiers."""
    model: str = ""
    citation_metrics: CitationMetrics = field(default_factory=CitationMetrics)
    judge_scores: JudgeScores = field(default_factory=JudgeScores)
    argumentative_roles: ArgumentativeRoleScores = field(
        default_factory=ArgumentativeRoleScores,
    )
    coherence_scores: CoherenceScores = field(default_factory=CoherenceScores)
    text_similarity: TextSimilarity = field(default_factory=TextSimilarity)
    structural_stats: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "tier_1_citation_metrics": self.citation_metrics.to_dict(),
            "tier_2_judge_scores": self.judge_scores.to_dict(),
            "tier_2b_argumentative_roles": self.argumentative_roles.to_dict(),
            "tier_2c_coherence": self.coherence_scores.to_dict(),
            "tier_3_text_similarity": self.text_similarity.to_dict(),
            "structural_stats": self.structural_stats,
        }


# ═════════════════════════════════════════════════════════════
# Shared judge-call helper
# ═════════════════════════════════════════════════════════════

def _call_judge(
    prompt: str,
    *,
    judge_model: str = JUDGE_MODEL,
    timeout: float = 180.0,
    max_retries: int = MAX_RETRIES,
    label: str = "judge",
    decoding_params: dict | None = None,
) -> tuple[dict | None, dict]:
    """
    Shared call/retry/JSON-parse for LLM-as-judge tiers.

    Factored out of the three copy-pasted blocks that used to live in
    evaluate_with_judge, evaluate_argumentative_roles, and
    evaluate_coherence. Behaviour is unchanged; this additionally
    returns run provenance (prompt_hash, timestamp, attempts, latency)
    per METRICS_CONTRACT.md §8.

    Returns:
        (parsed_json, meta) — parsed_json is None if every attempt
        failed to produce valid JSON. meta always has a value for
        every key below.
    """
    decoding_params = decoding_params or {"default": True}
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
    meta = {
        "judge_model": judge_model,
        "prompt_hash": prompt_hash,
        "decoding_params": decoding_params,
        "attempts": 0,
        "latency_s": 0.0,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "error": None,
    }

    call_start = time.time()
    for attempt in range(1, max_retries + 1):
        meta["attempts"] = attempt
        try:
            response = client.chat.completions.create(
                model=judge_model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                timeout=timeout,
            )

            raw = response.choices[0].message.content
            if raw is None:
                print(f"   ⚠️ {label}: empty content on attempt {attempt}")
                if attempt < max_retries:
                    time.sleep(RETRY_DELAY)
                continue

            # Strip thinking tags (reasoning models)
            raw = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL)

            match = re.search(r'\{.*\}', raw, re.DOTALL)
            if match:
                data = json.loads(match.group(0))
                meta["latency_s"] = time.time() - call_start
                return data, meta
            else:
                print(f"   ⚠️ {label}: no JSON found on attempt {attempt}")

        except Exception as e:
            print(f"   ⚠️ {label} call failed on attempt {attempt}: {e}")
            meta["error"] = str(e)

        if attempt < max_retries:
            time.sleep(RETRY_DELAY)

    meta["latency_s"] = time.time() - call_start
    return None, meta


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

    data, meta = _call_judge(
        prompt, judge_model=judge_model, timeout=120.0, label="Tier 2 judge",
    )
    result.prompt_hash = meta["prompt_hash"]
    result.decoding_params = meta["decoding_params"]
    result.timestamp = meta["timestamp"]

    if data:
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
    else:
        result.error = meta["error"] or "no valid JSON returned"

    return result


# ═════════════════════════════════════════════════════════════
# Tier 2B: Argumentative Role Analysis
# ═════════════════════════════════════════════════════════════

def _compute_jsd(dist_a: dict, dist_b: dict) -> float:
    """
    Compute Jensen-Shannon divergence between two role distributions.

    Both inputs should be dicts mapping role codes to counts.
    Returns a value in [0, 1] where 0 = identical distributions.
    Uses base-2 log so the result is bounded by 1.
    """
    import math

    roles = sorted(set(dist_a.keys()) | set(dist_b.keys()))
    total_a = sum(dist_a.values()) or 1
    total_b = sum(dist_b.values()) or 1

    p = [dist_a.get(r, 0) / total_a for r in roles]
    q = [dist_b.get(r, 0) / total_b for r in roles]
    m = [(pi + qi) / 2 for pi, qi in zip(p, q)]

    def _kl(dist_p, dist_m):
        return sum(
            pi * math.log2(pi / mi) if pi > 0 and mi > 0 else 0
            for pi, mi in zip(dist_p, dist_m)
        )

    return (_kl(p, m) + _kl(q, m)) / 2


def evaluate_argumentative_roles(
    generated_text: str,
    ground_truth: str,
    paper_text: str,
    bib_content: str,
    judge_model: str = JUDGE_MODEL,
) -> ArgumentativeRoleScores:
    """
    Tier 2B: Per-citation argumentative role and emphasis analysis.

    Uses a separate LLM judge call (different prompt from Tier 2) to:
      1. Classify the rhetorical role of each citation in both texts
      2. Compare which aspect of each cited paper is emphasized
      3. Compute aggregate match scores

    Returns:
        ArgumentativeRoleScores with per-citation data and aggregates.
    """
    result = ArgumentativeRoleScores(judge_model=judge_model)

    prompt = build_argumentative_role_prompt(
        generated_text=generated_text,
        ground_truth=ground_truth,
        paper_text=paper_text,
        bib_content=bib_content,
    )

    print(f"\n   🎭 Running Argumentative Role Judge ({judge_model})...")

    data, meta = _call_judge(
        prompt, judge_model=judge_model, timeout=180.0, label="Tier 2B judge",
    )
    result.prompt_hash = meta["prompt_hash"]
    result.decoding_params = meta["decoding_params"]
    result.timestamp = meta["timestamp"]

    if data:
        # Store raw judge data
        result.citation_roles = data.get("citation_roles", {})
        result.emphasis_comparisons = data.get("emphasis_comparisons", {})
        result.role_distribution = data.get("role_distribution", {})
        result.summary = data.get("summary", "")

        # ── Compute Metric 1: Role Match Score ──
        role_scores = []
        generated_citations_count = 0

        for key, info in result.citation_roles.items():
            gen_role = info.get("generated_role", "ABSENT")
            if gen_role != "ABSENT":
                generated_citations_count += 1

            match_type = info.get("role_match", "mismatch")
            if match_type == "exact":
                role_scores.append(1.0)
                result.exact_role_matches += 1
            elif match_type == "adjacent":
                role_scores.append(0.5)
                result.adjacent_role_matches += 1
            elif match_type == "one_sided":
                # Citation only in one version — not a mismatch per se
                result.one_sided_citations += 1
            else:  # mismatch
                role_scores.append(0.0)
                result.role_mismatches += 1

        result.total_citations_analyzed = len(result.citation_roles)
        if role_scores:
            result.role_match_score = sum(role_scores) / len(role_scores)

        if generated_citations_count > 0:
            result.alignment_coverage = len(role_scores) / generated_citations_count

        # ── Compute Metric 2: Emphasis Match Score ──
        emphasis_scores = []
        for key, info in result.emphasis_comparisons.items():
            level = info.get("match_level", "generic_or_different")
            if level == "precise":
                emphasis_scores.append(1.0)
                result.precise_emphasis += 1
            elif level == "partial":
                emphasis_scores.append(0.5)
                result.partial_emphasis += 1
            else:  # generic_or_different
                emphasis_scores.append(0.0)
                result.generic_emphasis += 1

        if emphasis_scores:
            result.emphasis_match_score = (
                sum(emphasis_scores) / len(emphasis_scores)
            )

        # ── Compute Role Distribution Divergence ──
        gen_dist = result.role_distribution.get("generated", {})
        orig_dist = result.role_distribution.get("original", {})
        if gen_dist and orig_dist:
            result.role_distribution_divergence = _compute_jsd(
                gen_dist, orig_dist,
            )

        # Print results
        print(f"   ✅ Argumentative Role Analysis complete")
        print(f"      Alignment Coverage: {result.alignment_coverage:.2f} "
              f"(aligned={len(role_scores)}, total_generated={generated_citations_count})")
        print(f"      Role Match Score: {result.role_match_score:.2f} "
              f"(exact={result.exact_role_matches}, "
              f"adjacent={result.adjacent_role_matches}, "
              f"mismatch={result.role_mismatches})")
        print(f"      Emphasis Match Score: {result.emphasis_match_score:.2f} "
              f"(precise={result.precise_emphasis}, "
              f"partial={result.partial_emphasis}, "
              f"generic={result.generic_emphasis})")
        print(f"      Role Distribution JSD: "
              f"{result.role_distribution_divergence:.3f}")
    else:
        result.error = meta["error"] or "no valid JSON returned"

    return result


# ═════════════════════════════════════════════════════════════
# Tier 2C: Argumentative Coherence
# ═════════════════════════════════════════════════════════════

def evaluate_coherence(
    generated_text: str,
    ground_truth: str,
    paper_text: str,
    judge_model: str = JUDGE_MODEL,
) -> CoherenceScores:
    """
    Tier 2C: Section-level argumentative coherence analysis.

    Uses a separate LLM judge call to evaluate:
      Metric 3 — Challenge-Citation Alignment: Are citations connected
        to the paper's stated research challenges?
      Metric 4 — Narrative Arc: Does the section follow a logical
        argumentative progression (landscape → gaps → positioning)?

    Returns:
        CoherenceScores with challenge mappings and arc analysis.
    """
    result = CoherenceScores(judge_model=judge_model)

    prompt = build_coherence_prompt(
        generated_text=generated_text,
        ground_truth=ground_truth,
        paper_text=paper_text,
    )

    print(f"\n   🧭 Running Coherence Judge ({judge_model})...")

    data, meta = _call_judge(
        prompt, judge_model=judge_model, timeout=180.0, label="Tier 2C judge",
    )
    result.prompt_hash = meta["prompt_hash"]
    result.decoding_params = meta["decoding_params"]
    result.timestamp = meta["timestamp"]

    if data:
        # Store challenge and mapping data
        result.challenges = data.get("challenges", [])
        result.challenge_citation_mapping = data.get(
            "challenge_citation_mapping", {},
        )

        # Alignment scores (Metric 3)
        alignment = data.get("alignment_scores", {})
        result.generated_alignment = alignment.get("generated", {})
        result.original_alignment = alignment.get("original", {})

        # Narrative arc data (Metric 4)
        arc = data.get("narrative_arc", {})
        result.generated_arc = arc.get("generated", {})
        result.original_arc = arc.get("original", {})

        arc_scores = data.get("narrative_arc_scores", {})
        result.generated_arc_scores = arc_scores.get("generated", {})
        result.original_arc_scores = arc_scores.get("original", {})

        result.summary = data.get("coherence_summary", "")

        # Print results
        gen_align = result.generated_alignment
        orig_align = result.original_alignment
        gen_arc = result.generated_arc_scores

        print(f"   ✅ Coherence Analysis complete")
        print(f"      Challenges identified: {len(result.challenges)}")
        print(f"      Challenge-Citation Alignment (generated): "
              f"F1={gen_align.get('alignment_f1', 0):.2f} "
              f"(coverage={gen_align.get('challenge_coverage', 0):.2f}, "
              f"relevance={gen_align.get('citation_relevance', 0):.2f})")
        print(f"      Challenge-Citation Alignment (original):  "
              f"F1={orig_align.get('alignment_f1', 0):.2f} "
              f"(coverage={orig_align.get('challenge_coverage', 0):.2f}, "
              f"relevance={orig_align.get('citation_relevance', 0):.2f})")
        print(f"      Narrative Arc (generated): "
              f"composite={gen_arc.get('composite_arc_score', 0):.2f}")

        unconnected = gen_align.get("unconnected_citations", [])
        if unconnected:
            print(f"      ⚠️ Unconnected citations: {unconnected}")
    else:
        result.error = meta["error"] or "no valid JSON returned"

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
    run_argumentation: bool = True,
    run_similarity: bool = True,
    outputs_dir: str | Path = "outputs/generation",
) -> EvaluationReport:
    """
    Run the full multi-tier evaluation.
    
    Args:
        generated_text: The LLM-generated Related Works section.
        ground_truth: The original Related Works text.
        paper_text: The paper text (for judge context).
        bib_path: Path to the .bib file.
        model_name: Name of the model that generated the text.
        run_judge: Whether to run Tier 2 (LLM-as-judge). Costs API tokens.
        run_argumentation: Whether to run Tier 2B/2C (argumentative analysis).
            Requires run_judge=True. Costs additional API tokens (2 extra
            judge calls per paper).
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
    
    # ── Tier 2: LLM-as-Judge (Quality Dimensions) ──
    bib_content = bib_path.read_text(encoding="utf-8")
    if run_judge:
        report.judge_scores = evaluate_with_judge(
            generated_text=generated_text,
            ground_truth=ground_truth,
            paper_text=paper_text,
            bib_content=bib_content,
        )
    else:
        print(f"\n   ⏩ Tier 2: Skipped (--no-judge)")
    
    # ── Tier 2B: Argumentative Role Analysis ──
    if run_argumentation:
        report.argumentative_roles = evaluate_argumentative_roles(
            generated_text=generated_text,
            ground_truth=ground_truth,
            paper_text=paper_text,
            bib_content=bib_content,
        )
    else:
        print(f"\n   ⏩ Tier 2B: Skipped (--no-argumentation)")

    # ── Tier 2C: Argumentative Coherence ──
    if run_argumentation:
        report.coherence_scores = evaluate_coherence(
            generated_text=generated_text,
            ground_truth=ground_truth,
            paper_text=paper_text,
        )
    else:
        print(f"\n   ⏩ Tier 2C: Skipped (--no-argumentation)")
    
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
    ar = report.argumentative_roles
    cs = report.coherence_scores
    
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
    
    # Argumentative Role metrics
    if ar.total_citations_analyzed > 0:
        doc += f"| **Alignment Coverage** | **{ar.alignment_coverage:.2f}** |\n"
        doc += f"| **Role Match Score** | **{ar.role_match_score:.2f}** |\n"
        doc += f"| **Emphasis Match Score** | **{ar.emphasis_match_score:.2f}** |\n"
        doc += f"| Role Distribution JSD | {ar.role_distribution_divergence:.3f} |\n"

    # Coherence metrics
    gen_align = cs.generated_alignment
    if gen_align:
        doc += f"| **Challenge-Citation F1** | **{gen_align.get('alignment_f1', 0):.2f}** |\n"
    gen_arc = cs.generated_arc_scores
    if gen_arc:
        doc += f"| **Narrative Arc Score** | **{gen_arc.get('composite_arc_score', 0):.2f}** |\n"
    
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
    
    # ── Argumentative Role Breakdown ──
    if ar.total_citations_analyzed > 0:
        doc += f"\n## 🎭 Argumentative Role Analysis\n\n"
        doc += f"**Role Match Score**: {ar.role_match_score:.2f} "
        doc += f"(exact={ar.exact_role_matches}, "
        doc += f"adjacent={ar.adjacent_role_matches}, "
        doc += f"mismatch={ar.role_mismatches})\n\n"
        doc += f"**Emphasis Match Score**: {ar.emphasis_match_score:.2f} "
        doc += f"(precise={ar.precise_emphasis}, "
        doc += f"partial={ar.partial_emphasis}, "
        doc += f"generic={ar.generic_emphasis})\n\n"

        if ar.role_distribution:
            doc += "### Role Distribution Comparison\n\n"
            doc += "| Role | Generated | Original |\n"
            doc += "|------|-----------|----------|\n"
            gen_dist = ar.role_distribution.get("generated", {})
            orig_dist = ar.role_distribution.get("original", {})
            for role in ["FND", "CTR", "GAP", "SUP", "MTD", "CTX"]:
                doc += f"| {role} | {gen_dist.get(role, 0)} | {orig_dist.get(role, 0)} |\n"
            doc += f"\n**Distribution Divergence (JSD)**: {ar.role_distribution_divergence:.3f}\n"

        if ar.summary:
            doc += f"\n{ar.summary}\n"

    # ── Coherence Breakdown ──
    if cs.challenges:
        doc += f"\n## 🧭 Argumentative Coherence Analysis\n\n"
        doc += f"### Paper Challenges\n\n"
        for c in cs.challenges:
            doc += f"- **{c.get('id', '?')}**: {c.get('description', '')}\n"

        if gen_align:
            doc += f"\n### Challenge-Citation Alignment\n\n"
            doc += f"| Metric | Generated | Original |\n"
            doc += f"|--------|-----------|----------|\n"
            doc += f"| Coverage | {gen_align.get('challenge_coverage', 0):.2f} | {cs.original_alignment.get('challenge_coverage', 0):.2f} |\n"
            doc += f"| Relevance | {gen_align.get('citation_relevance', 0):.2f} | {cs.original_alignment.get('citation_relevance', 0):.2f} |\n"
            doc += f"| **F1** | **{gen_align.get('alignment_f1', 0):.2f}** | **{cs.original_alignment.get('alignment_f1', 0):.2f}** |\n"

            unconnected = gen_align.get("unconnected_citations", [])
            if unconnected:
                doc += f"\n**Unconnected citations**: {', '.join(unconnected)}\n"

        if gen_arc:
            doc += f"\n### Narrative Arc\n\n"
            doc += f"| Metric | Generated | Original |\n"
            doc += f"|--------|-----------|----------|\n"
            orig_arc = cs.original_arc_scores
            for metric in ["presence_score", "ordering_score", "balance_score", "composite_arc_score"]:
                label = metric.replace("_", " ").title()
                doc += f"| {label} | {gen_arc.get(metric, 0):.2f} | {orig_arc.get(metric, 0):.2f} |\n"

        if cs.summary:
            doc += f"\n{cs.summary}\n"
    
    if cm.hallucinated_keys:
        doc += f"\n## ⚠️ Hallucinated Citations\n\n"
        for k in cm.hallucinated_keys:
            doc += f"- `{k}`\n"
    
    doc += f"\n---\n\n## Generated Related Works\n\n{generated}\n"
    doc += f"\n---\n\n## Original Related Works (Ground Truth)\n\n{ground_truth}\n"
    
    path = outputs_dir / f"side_by_side_{model_short}.md"
    path.write_text(doc, encoding="utf-8")
    print(f"   📋 Side-by-side comparison → {path.name}")
