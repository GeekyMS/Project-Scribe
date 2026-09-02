"""
Phase 2 — Human annotation of Tier 4 claim-level verdicts.

The synthetic-negative test (pipeline/synthetic_negatives.py) measures
whether Tier 4 catches claims that were deliberately falsified. It
says nothing about whether Tier 4's judgment matches a human's on
claims nobody corrupted — i.e. whether the checker's "supported" /
"unsupported" / "partially_supported" line actually falls where a
person would draw it. That's what this module is for.

Produces a self-contained HTML form presenting a sample of
(claim, evidence, sentence) triples — WITHOUT showing the checker's
own verdict, so the human's judgment isn't anchored — plus a blinded
answer key (the checker's verdicts, held back the same way
human_eval.py holds back the A/B mapping) and a JSON response schema.
A companion function scores the returned responses against the
answer key once filled in.
"""

import json
import random
from dataclasses import dataclass, field
from pathlib import Path

VERDICT_OPTIONS = ["supported", "partially_supported", "unsupported", "contradicted", "unverifiable"]

VERDICT_DESCRIPTIONS = {
    "supported": "The evidence text clearly backs the claim.",
    "partially_supported": "The evidence is related but doesn't fully confirm the specific claim.",
    "unsupported": "The evidence is on-topic but doesn't address this specific claim.",
    "contradicted": "The evidence says something that conflicts with the claim.",
    "unverifiable": "The evidence doesn't engage with the claim's subject at all.",
}


def sample_rows_for_annotation(
    per_citation_rows: list[dict],
    *,
    sample_size: int = 20,
    seed: int | None = None,
) -> list[dict]:
    """
    Sample a stratified mix of rows for human review: some of every
    verdict Tier 4 produced (not just "supported"), so the annotation
    set can reveal disagreement in either direction — the checker
    being too lenient AND too strict.
    """
    rng = random.Random(seed)
    by_verdict: dict[str, list[dict]] = {}
    for row in per_citation_rows:
        if row.get("verdict_source") != "judge":
            continue  # only rows the judge actually reasoned about, not short-circuited ones
        by_verdict.setdefault(row["verdict"], []).append(row)

    for rows in by_verdict.values():
        rng.shuffle(rows)

    verdicts_present = list(by_verdict.keys())
    if not verdicts_present:
        return []

    per_bucket = max(1, sample_size // len(verdicts_present))
    sample = []
    for verdict in verdicts_present:
        sample.extend(by_verdict[verdict][:per_bucket])

    # Top up to sample_size from whatever's left, if buckets were uneven.
    if len(sample) < sample_size:
        remaining = [r for rows in by_verdict.values() for r in rows if r not in sample]
        rng.shuffle(remaining)
        sample.extend(remaining[:sample_size - len(sample)])

    rng.shuffle(sample)
    return sample[:sample_size]


def _escape(text: str) -> str:
    return (
        (text or "")
        .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def generate_claim_annotation_form(
    per_citation_rows: list[dict],
    entry_by_key: dict[str, dict],
    *,
    model_name: str = "",
    outputs_dir: str | Path = "outputs/generation",
    sample_size: int = 20,
    max_evidence_chars: int = 1500,
    seed: int | None = None,
) -> dict:
    """
    Generate the annotation HTML form, the blinded checker answer
    key, and the JSON response schema.

    Returns a dict of {name: path}. The answer key file is explicitly
    marked DO NOT SHARE, matching human_eval.py's blind_ab_mapping
    pattern — a human annotator must not see it before annotating.
    """
    outputs_dir = Path(outputs_dir)
    outputs_dir.mkdir(parents=True, exist_ok=True)

    sample = sample_rows_for_annotation(per_citation_rows, sample_size=sample_size, seed=seed)

    if not sample:
        raise ValueError(
            "No judge-evaluated rows to sample from (all rows were short-circuited "
            "as unverifiable, or per_citation_rows is empty) — nothing to annotate."
        )

    cards_html = []
    answer_key = {}

    for i, row in enumerate(sample):
        item_id = f"item_{i}"
        answer_key[item_id] = {
            "citation_id": row["citation_id"],
            "checker_verdict": row["verdict"],
            "checker_justification": row.get("justification", ""),
        }

        entry = entry_by_key.get(row["citation_key"], {})
        evidence_text = entry.get("full_text") or entry.get("abstract") or ""
        evidence_text = evidence_text[:max_evidence_chars]
        evidence_kind = "full text (truncated)" if entry.get("full_text") else "abstract"

        radio_options = "\n".join(
            f'''<label class="option">
                <input type="radio" name="{item_id}_verdict" value="{v}">
                <span><strong>{v}</strong> — {_escape(VERDICT_DESCRIPTIONS[v])}</span>
            </label>'''
            for v in VERDICT_OPTIONS
        )

        cards_html.append(f"""
        <div class="card">
            <h3>Citation {i + 1} of {len(sample)}</h3>
            <p class="sentence"><strong>Sentence citing the paper:</strong> "{_escape(row['sentence'])}"</p>
            <p class="claim"><strong>Claim Tier 4 extracted:</strong> "{_escape(row.get('claim', ''))}"</p>
            <p class="paper-title"><strong>Cited paper:</strong> {_escape(entry.get('title', 'Unknown'))}</p>
            <details open>
                <summary>Evidence ({evidence_kind}, {len(evidence_text)} chars)</summary>
                <pre class="evidence">{_escape(evidence_text) or '(no evidence text retrieved)'}</pre>
            </details>
            <div class="options">
                <p><strong>Your verdict — does the evidence support the claim?</strong></p>
                {radio_options}
            </div>
            <label class="notes-label">Notes (optional):
                <textarea name="{item_id}_notes" rows="2"></textarea>
            </label>
        </div>
        """)

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Tier 4 Claim Annotation — {_escape(model_name)}</title>
<style>
  body {{ font-family: -apple-system, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; color: #222; }}
  h1 {{ font-size: 1.4rem; }}
  .intro {{ background: #f5f5f5; padding: 1rem; border-radius: 8px; margin-bottom: 2rem; }}
  .card {{ border: 1px solid #ddd; border-radius: 8px; padding: 1.2rem; margin-bottom: 1.5rem; }}
  .sentence {{ font-style: italic; }}
  .claim {{ background: #fffbe6; padding: 0.5rem; border-radius: 4px; }}
  .evidence {{ white-space: pre-wrap; background: #f0f0f0; padding: 0.8rem; border-radius: 4px; max-height: 300px; overflow-y: auto; font-size: 0.85rem; }}
  .option {{ display: block; margin: 0.4rem 0; cursor: pointer; }}
  .option input {{ margin-right: 0.5rem; }}
  .notes-label {{ display: block; margin-top: 0.8rem; }}
  textarea {{ width: 100%; box-sizing: border-box; }}
  #export-btn {{ position: sticky; bottom: 1rem; background: #2563eb; color: white; border: none;
                 padding: 0.8rem 1.5rem; border-radius: 6px; font-size: 1rem; cursor: pointer; }}
</style>
</head>
<body>
<h1>Tier 4 Claim Annotation — {_escape(model_name)}</h1>
<div class="intro">
  <p>For each citation below, you're shown the sentence that cites a paper, the claim our checker
  extracted from it, and the actual evidence text (abstract or full text) retrieved for that paper.
  Judge ONLY from the evidence shown — not from what you may know about the paper otherwise.</p>
  <p>You are <strong>not</strong> shown the checker's own verdict. This is intentional — it lets us
  measure where the checker's judgment and a human's line up, and where they don't.</p>
</div>
<form id="annotation-form">
{''.join(cards_html)}
<button type="button" id="export-btn" onclick="exportResponses()">Export Responses (JSON)</button>
</form>
<script>
function exportResponses() {{
  const form = document.getElementById('annotation-form');
  const data = new FormData(form);
  const responses = {{}};
  for (const [k, v] of data.entries()) {{
    const m = k.match(/^(item_\\d+)_(verdict|notes)$/);
    if (!m) continue;
    const [, itemId, field] = m;
    responses[itemId] = responses[itemId] || {{}};
    responses[itemId][field] = v;
  }}
  const blob = new Blob([JSON.stringify({{model: "{_escape(model_name)}", responses}}, null, 2)],
                         {{type: 'application/json'}});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'claim_annotation_responses.json';
  a.click();
}}
</script>
</body>
</html>
"""

    model_short = model_name.replace("/", "_") or "unknown"
    form_path = outputs_dir / f"claim_annotation_form_{model_short}.html"
    form_path.write_text(html, encoding="utf-8")

    answer_key_path = outputs_dir / f"claim_annotation_answer_key_{model_short}.json"
    answer_key_path.write_text(
        json.dumps({
            "model": model_name,
            "note": "DO NOT share this file with the annotator before they submit responses — "
                    "it contains the checker's own verdicts and would bias the annotation.",
            "answer_key": answer_key,
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    schema_path = outputs_dir / "claim_annotation_response_schema.json"
    schema_path.write_text(
        json.dumps({
            "model": {"type": "string"},
            "responses": {
                "item_id": {
                    "verdict": {"type": "string", "enum": VERDICT_OPTIONS},
                    "notes": {"type": "string"},
                },
            },
        }, indent=2),
        encoding="utf-8",
    )

    print(f"\n   📝 Claim annotation materials generated ({len(sample)} items):")
    print(f"      form: {form_path.name}")
    print(f"      answer key (withhold until after): {answer_key_path.name}")
    print(f"      schema: {schema_path.name}")

    return {
        "form": str(form_path),
        "answer_key": str(answer_key_path),
        "schema": str(schema_path),
    }


@dataclass
class AgreementResult:
    """Checker-vs-human agreement on the annotated sample."""
    n_items: int = 0
    n_agree: int = 0
    agreement_rate: float = 0.0
    confusion: dict = field(default_factory=dict)  # {human_verdict: {checker_verdict: count}}
    disagreements: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "n_items": self.n_items,
            "n_agree": self.n_agree,
            "agreement_rate": round(self.agreement_rate, 4),
            "confusion": self.confusion,
            "disagreements": self.disagreements,
        }


def compute_agreement(responses_path: str | Path, answer_key_path: str | Path) -> AgreementResult:
    """
    Score a filled-in response export against the withheld answer key.

    Note: this measures agreement between the checker and ONE human
    annotator, not standard multi-annotator inter-annotator agreement
    (which needs 2+ independent humans) — call it what it is when
    reporting it.
    """
    responses = json.loads(Path(responses_path).read_text(encoding="utf-8"))["responses"]
    answer_key = json.loads(Path(answer_key_path).read_text(encoding="utf-8"))["answer_key"]

    result = AgreementResult()
    for item_id, key_entry in answer_key.items():
        human = responses.get(item_id, {})
        human_verdict = human.get("verdict")
        checker_verdict = key_entry["checker_verdict"]
        if not human_verdict:
            continue  # annotator skipped this item

        result.n_items += 1
        result.confusion.setdefault(human_verdict, {}).setdefault(checker_verdict, 0)
        result.confusion[human_verdict][checker_verdict] += 1

        if human_verdict == checker_verdict:
            result.n_agree += 1
        else:
            result.disagreements.append({
                "item_id": item_id,
                "citation_id": key_entry["citation_id"],
                "human_verdict": human_verdict,
                "checker_verdict": checker_verdict,
                "checker_justification": key_entry.get("checker_justification", ""),
                "human_notes": human.get("notes", ""),
            })

    result.agreement_rate = result.n_agree / result.n_items if result.n_items else 0.0
    return result
