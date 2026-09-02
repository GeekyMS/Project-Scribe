"""
Synthetic-negative validation for Tier 4 (evidence support).

There is no claim-level or evidence-level ground truth anywhere in
this project — the only ground truth Phase 2 previously had access to
was the section-level original Related Works text. This module
builds a cheap proxy: take claims Tier 4 already verified as
"supported" against real evidence, corrupt them in a way that makes
them false, and check whether Tier 4 (run again, against the SAME
real evidence) correctly stops calling them "supported".

Three corruption types, matching the plan:
  - swap_key: reassign a real, verified claim to a DIFFERENT cited
    paper's evidence. Deterministic, no LLM call. Simulates a
    plausible real-world hallucination: attributing a real finding to
    the wrong (but genuinely cited) paper.
  - inflate_number: find a number/percentage in the claim and change
    it substantially. Deterministic (regex), no LLM call. Only
    applies to claims that contain a number.
  - negate: ask an LLM to produce a plausible-sounding false variant
    of the claim (build_claim_negation_prompt). Necessary because
    reliable semantic negation isn't a regex problem.

A checker that cannot catch these is not doing its job — this is a
floor, not a substitute for real human-annotated ground truth (which
Phase 2's other half — human review of a claim sample — is for).
"""

import random
import re
from dataclasses import dataclass, field

from .gen_prompts import build_claim_negation_prompt, build_evidence_support_prompt

# Verdicts that count as "corruption detected" — the failure mode under
# test is the checker FALSELY ENDORSING a corrupted claim as "supported".
# Any judge-reasoned verdict other than "supported" means it didn't do
# that, including "unverifiable" (the judge looked at the evidence and
# declined to back the claim, which is the correct call — it must NOT
# be scored as a miss just because it isn't "unsupported").
_DETECTED_VERDICTS = {"unsupported", "contradicted", "partially_supported", "unverifiable"}

# Regex for a number Tier 4 can inflate: integers, decimals, percentages.
_NUMBER_RE = re.compile(r'\b(\d+(?:\.\d+)?)\s*(%|percent|x|times)?\b', re.IGNORECASE)


@dataclass
class CorruptedClaim:
    """One synthetic-negative test case."""
    corruption_id: str
    corruption_type: str          # "swap_key" | "inflate_number" | "negate"
    original_citation_id: str
    original_key: str
    original_claim: str
    corrupted_claim: str
    corrupted_key: str            # key whose evidence the corrupted claim is checked against
    corruption_note: str = ""     # human-readable description of what changed
    # Filled in after checking:
    verdict: str | None = None
    detected: bool | None = None  # True if Tier 4 correctly flagged it as not-supported
    justification: str = ""


@dataclass
class SyntheticNegativeReport:
    """Aggregate result of running the synthetic-negative test set."""
    cases: list[CorruptedClaim] = field(default_factory=list)
    detection_rate: float = 0.0
    detection_rate_by_type: dict = field(default_factory=dict)
    n_cases: int = 0
    n_detected: int = 0
    judge_model: str = ""
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "n_cases": self.n_cases,
            "n_detected": self.n_detected,
            "detection_rate": round(self.detection_rate, 4),
            "detection_rate_by_type": {
                k: round(v, 4) for k, v in self.detection_rate_by_type.items()
            },
            "judge_model": self.judge_model,
            "error": self.error,
            "cases": [
                {
                    "corruption_id": c.corruption_id,
                    "corruption_type": c.corruption_type,
                    "original_citation_id": c.original_citation_id,
                    "original_key": c.original_key,
                    "original_claim": c.original_claim,
                    "corrupted_claim": c.corrupted_claim,
                    "corrupted_key": c.corrupted_key,
                    "corruption_note": c.corruption_note,
                    "verdict": c.verdict,
                    "detected": c.detected,
                    "justification": c.justification,
                }
                for c in self.cases
            ],
        }


def _inflate_number(claim: str) -> str | None:
    """Return a copy of claim with the first number substantially changed, or None if no number found."""
    match = _NUMBER_RE.search(claim)
    if not match:
        return None
    num_str, unit = match.group(1), match.group(2) or ""
    try:
        num = float(num_str)
    except ValueError:
        return None

    # Substantial, direction-varying change — not just +1.
    if num == 0:
        new_num = 50
    elif num < 10:
        new_num = num * 5
    else:
        new_num = num * 2.5

    new_num_str = str(int(new_num)) if new_num == int(new_num) else f"{new_num:.1f}"
    start, end = match.span(1)
    return claim[:start] + new_num_str + claim[end:]


def build_corruption_set(
    per_citation_rows: list[dict],
    entry_by_key: dict[str, dict],
    *,
    max_negations: int = 8,
    rng: random.Random | None = None,
) -> list[CorruptedClaim]:
    """
    Build synthetic-negative test cases from real Tier 4 "supported" rows.

    Args:
        per_citation_rows: EvidenceSupportScores.per_citation from a
            real run — only rows with verdict == "supported" are used
            as corruption seeds (corrupting an already-wrong claim
            tells you nothing).
        entry_by_key: bib key -> enriched entry (for swap_key, to pick
            a real alternate paper whose evidence the corrupted claim
            will be checked against).
        max_negations: cap on how many "negate" cases to build (each
            costs one LLM call) — swap_key and inflate_number are free.
        rng: optional seeded Random for reproducible case selection.

    Returns:
        (deterministic_cases, negation_seeds) — deterministic_cases is
        a list of fully-built CorruptedClaim (swap_key/inflate_number,
        no LLM call needed). negation_seeds is the list of source rows
        to build "negate" cases from — building those needs an LLM
        call per case, done separately by _apply_negations() so this
        function stays synchronous and free.
    """
    rng = rng or random.Random()
    supported = [r for r in per_citation_rows if r.get("verdict") == "supported" and r.get("claim")]

    cases: list[CorruptedClaim] = []
    all_keys = list(entry_by_key.keys())

    for i, row in enumerate(supported):
        key = row["citation_key"]
        claim = row["claim"]

        # swap_key: check this real claim against a DIFFERENT real
        # paper's evidence. Skip if there's no other key to swap to.
        other_keys = [
            k for k in all_keys
            if k != key and (entry_by_key.get(k, {}).get("abstract") or entry_by_key.get(k, {}).get("full_text"))
        ]
        if other_keys:
            swap_target = rng.choice(other_keys)
            cases.append(CorruptedClaim(
                corruption_id=f"swap_{i}",
                corruption_type="swap_key",
                original_citation_id=row["citation_id"],
                original_key=key,
                original_claim=claim,
                corrupted_claim=claim,  # claim text unchanged — only the evidence target changes
                corrupted_key=swap_target,
                corruption_note=f"Same claim, checked against [{swap_target}]'s evidence instead of [{key}]'s.",
            ))

        # inflate_number: only when the claim has a number to inflate
        inflated = _inflate_number(claim)
        if inflated and inflated != claim:
            cases.append(CorruptedClaim(
                corruption_id=f"inflate_{i}",
                corruption_type="inflate_number",
                original_citation_id=row["citation_id"],
                original_key=key,
                original_claim=claim,
                corrupted_claim=inflated,
                corrupted_key=key,  # same evidence, different (wrong) number in the claim
                corruption_note="Numeric value in the claim substantially changed.",
            ))

    # negate: capped, since each needs an LLM call
    negation_candidates = [r for r in supported if r.get("claim")]
    rng.shuffle(negation_candidates)
    negation_seeds = negation_candidates[:max_negations]

    return cases, negation_seeds


def _apply_negations(negation_seeds: list[dict], call_judge_fn) -> list[CorruptedClaim]:
    """Run the negation LLM call for each seed row. call_judge_fn has the _call_judge(prompt, ...) signature."""
    cases = []
    for i, row in enumerate(negation_seeds):
        prompt = build_claim_negation_prompt(row["claim"])
        data, meta = call_judge_fn(prompt, label=f"negation corruption {i}", timeout=60.0)
        if not data or not data.get("corrupted_claim"):
            continue
        cases.append(CorruptedClaim(
            corruption_id=f"negate_{i}",
            corruption_type="negate",
            original_citation_id=row["citation_id"],
            original_key=row["citation_key"],
            original_claim=row["claim"],
            corrupted_claim=data["corrupted_claim"],
            corrupted_key=row["citation_key"],  # same evidence, falsified claim
            corruption_note=data.get("corruption_description", ""),
        ))
    return cases


def evaluate_corruption_set(
    per_citation_rows: list[dict],
    entry_by_key: dict[str, dict],
    *,
    judge_model: str,
    call_judge_fn,
    max_full_text_chars: int = 6000,
    batch_size: int = 8,
    max_negations: int = 8,
    rng: random.Random | None = None,
) -> SyntheticNegativeReport:
    """
    Build and run the full synthetic-negative test set against real
    Tier 4 machinery, and compute the detection rate.

    Args:
        per_citation_rows: EvidenceSupportScores.per_citation from a
            real Tier 4 run (the seed pool — only "supported" rows
            are corrupted).
        entry_by_key: bib key -> enriched entry, same as passed to
            evaluate_evidence_support().
        judge_model: OpenRouter model id.
        call_judge_fn: the evaluator's _call_judge (passed in rather
            than imported, to avoid a circular import between
            evaluator.py and this module).
        max_full_text_chars, batch_size: same semantics as
            evaluate_evidence_support().
        max_negations: cap on LLM-based "negate" cases.
        rng: optional seeded Random.

    Returns:
        SyntheticNegativeReport with per-case verdicts and the
        aggregate/by-type detection rate.
    """
    from .evaluator import _entry_evidence_kind  # local import: avoid a module-level cycle

    result = SyntheticNegativeReport(judge_model=judge_model)

    deterministic_cases, negation_seeds = build_corruption_set(
        per_citation_rows, entry_by_key, max_negations=max_negations, rng=rng,
    )
    negation_cases = _apply_negations(negation_seeds, call_judge_fn) if negation_seeds else []
    cases = deterministic_cases + negation_cases

    if not cases:
        result.error = "no eligible 'supported' rows to corrupt (need a real Tier 4 run with supported claims first)"
        return result

    # Build judge batches, one item per case, evidence pulled from
    # corrupted_key's entry (usually == original_key, except swap_key).
    items = []
    case_by_id = {}
    for case in cases:
        entry = entry_by_key.get(case.corrupted_key)
        if entry is None:
            continue
        evidence_kind, evidence_text = _entry_evidence_kind(entry, max_full_text_chars)
        if evidence_kind == "none":
            continue
        case_by_id[case.corruption_id] = case
        items.append({
            "citation_id": case.corruption_id,
            "key": case.corrupted_key,
            "sentence": case.corrupted_claim,
            "context": case.corrupted_claim,
            "co_cited_keys": [],
            "paper_title": entry.get("title", "Unknown"),
            "evidence_text": evidence_text,
            "evidence_kind": evidence_kind,
        })

    for batch_start in range(0, len(items), batch_size):
        batch = items[batch_start:batch_start + batch_size]
        if not batch:
            continue
        prompt = build_evidence_support_prompt(batch)
        data, meta = call_judge_fn(
            prompt, judge_model=judge_model, timeout=180.0,
            label=f"synthetic-negative batch {batch_start // batch_size + 1}",
        )
        if not data:
            result.error = meta.get("error") or "no valid JSON returned"
            continue
        for verdict in data.get("verdicts", []):
            case = case_by_id.get(verdict.get("citation_id", ""))
            if case is None:
                continue
            case.verdict = verdict.get("verdict", "unverifiable")
            case.justification = verdict.get("justification", "")
            case.detected = case.verdict in _DETECTED_VERDICTS

    evaluated = [c for c in cases if c.detected is not None]
    result.cases = cases
    result.n_cases = len(evaluated)
    result.n_detected = sum(1 for c in evaluated if c.detected)
    result.detection_rate = result.n_detected / len(evaluated) if evaluated else 0.0

    by_type: dict[str, list[bool]] = {}
    for c in evaluated:
        by_type.setdefault(c.corruption_type, []).append(c.detected)
    result.detection_rate_by_type = {
        t: sum(v) / len(v) for t, v in by_type.items()
    }

    return result
