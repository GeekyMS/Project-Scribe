"""
Phase 2 — Related Works Generation.

Takes the paper text (markdown, with Related Works removed) and the
bibliography, enriches references with abstracts, and uses a top-tier
LLM to generate a new Related Works section.

Supports multiple models and logs everything for cheat-detection analysis.
"""

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from openai import OpenAI
from dotenv import load_dotenv

from .abstract_fetcher import enrich_bib_entries, parse_bib_file
from .full_text_fetcher import fetch_and_extract_full_texts
from .gen_prompts import build_generation_prompt, build_manual_prompt

load_dotenv()
API_KEY = os.getenv("OPENROUTER_API_KEY")
if not API_KEY:
    raise ValueError("Missing OPENROUTER_API_KEY in .env")

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=API_KEY,
    timeout=httpx.Timeout(300.0, connect=10.0),  # longer timeout for generation
)

# Default generation model
DEFAULT_MODEL = "anthropic/claude-sonnet-4.6"

# Maximum retries for generation
MAX_RETRIES = 2
RETRY_DELAY = 10  # seconds


@dataclass
class GenerationResult:
    """Result of a single generation run."""
    model: str = ""
    latex_output: str = ""
    markdown_output: str = ""
    raw_output: str = ""
    prompt: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    generation_time_s: float = 0.0
    enriched_entries: list[dict] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "generation_time_s": round(self.generation_time_s, 2),
            "enriched_entry_count": len(self.enriched_entries),
            "abstracts_found": sum(
                1 for e in self.enriched_entries if e.get("abstract")
            ),
            "latex_output_length": len(self.latex_output),
            "markdown_output_length": len(self.markdown_output),
            "error": self.error,
        }


def _parse_dual_output(raw: str) -> tuple[str, str]:
    """
    Parse the dual-format output (LaTeX + Markdown) separated by
    ---FORMAT_SEPARATOR---.
    
    Returns (latex_text, markdown_text). Falls back gracefully if
    the separator is missing.
    """
    separator = "---FORMAT_SEPARATOR---"
    
    if separator in raw:
        parts = raw.split(separator, 1)
        latex_part = parts[0].strip()
        md_part = parts[1].strip()
        
        # Clean up any leading format labels
        latex_part = re.sub(
            r'^(?:FORMAT\s*1\s*\(?LaTeX\)?\s*:?\s*)',
            '', latex_part, flags=re.IGNORECASE,
        ).strip()
        md_part = re.sub(
            r'^(?:FORMAT\s*2\s*\(?Markdown\)?\s*:?\s*)',
            '', md_part, flags=re.IGNORECASE,
        ).strip()
        
        return latex_part, md_part
    
    # No separator — try to detect format
    if "\\cite{" in raw:
        # Looks like LaTeX — return as latex, leave markdown empty
        return raw.strip(), ""
    else:
        # Assume markdown
        return "", raw.strip()


def generate_related_work(
    paper_md_path: str | Path,
    bib_path: str | Path,
    model: str = DEFAULT_MODEL,
    output_format: str = "both",
    enrich: bool = True,
    full_context: bool = False,
    outputs_dir: str | Path = "outputs/generation",
) -> GenerationResult:
    """
    Generate a Related Works section using an LLM.
    
    Args:
        paper_md_path: Path to the paper text (markdown) without Related Works.
        bib_path: Path to the .bib file with cited references.
        model: OpenRouter model identifier.
        output_format: "latex", "markdown", or "both".
        enrich: Whether to fetch abstracts from Semantic Scholar (default: True).
        outputs_dir: Directory for output files.
    
    Returns:
        GenerationResult with the generated text, token usage, and timing.
    """
    paper_md_path = Path(paper_md_path)
    bib_path = Path(bib_path)
    outputs_dir = Path(outputs_dir)
    outputs_dir.mkdir(parents=True, exist_ok=True)
    
    result = GenerationResult(model=model)
    
    print(f"\n{'='*60}")
    print(f"🖊️  Phase 2: Related Works Generation")
    print(f"{'='*60}")
    print(f"   Model: {model}")
    print(f"   Format: {output_format}")
    print(f"   Enrich: {enrich}")
    
    # ── Load inputs ──
    paper_text = paper_md_path.read_text(encoding="utf-8")
    bib_content = bib_path.read_text(encoding="utf-8")
    print(f"   Paper text: {len(paper_text)} chars")
    print(f"   Bib content: {len(bib_content)} chars")
    
    # ── Enrich references ──
    enriched_entries = None
    if full_context:
        cache_path = outputs_dir / "full_text_cache.json"
        enriched_entries = fetch_and_extract_full_texts(bib_path, cache_path=cache_path)
        result.enriched_entries = enriched_entries
    elif enrich:
        cache_path = outputs_dir / "enrichment_cache.json"
        enriched_entries = enrich_bib_entries(bib_path, cache_path=cache_path)
        result.enriched_entries = enriched_entries
    
    # ── Build prompt ──
    prompt = build_generation_prompt(
        paper_text=paper_text,
        bib_content=bib_content,
        enriched_entries=enriched_entries,
        output_format=output_format,
    )
    result.prompt = prompt
    print(f"   Prompt length: {len(prompt)} chars (~{len(prompt)//4} tokens)")
    
    # ── Save prompt for inspection ──
    prompt_path = outputs_dir / f"prompt_{_model_shortname(model)}.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    print(f"   📋 Prompt saved → {prompt_path}")
    
    # ── Also save manual prompt for web LLM testing ──
    manual_prompt = build_manual_prompt(
        paper_text=paper_text,
        bib_content=bib_content,
        enriched_entries=enriched_entries,
    )
    manual_path = outputs_dir / "prompt_manual_web_llm.txt"
    manual_path.write_text(manual_prompt, encoding="utf-8")
    print(f"   📋 Manual prompt (for web LLMs) saved → {manual_path}")
    
    # ── Generate ──
    print(f"\n   🤖 Generating Related Works...")
    start_time = time.time()
    
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                timeout=300.0,
            )
            
            raw = response.choices[0].message.content
            if raw is None:
                print(f"   ⚠️ Empty response on attempt {attempt}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY)
                continue
            
            # Strip thinking tags
            raw = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL)
            
            elapsed = time.time() - start_time
            result.raw_output = raw
            result.generation_time_s = elapsed
            
            # Parse token usage
            if response.usage:
                result.prompt_tokens = response.usage.prompt_tokens or 0
                result.completion_tokens = response.usage.completion_tokens or 0
                result.total_tokens = response.usage.total_tokens or 0
            
            # Parse output format
            if output_format == "both":
                result.latex_output, result.markdown_output = _parse_dual_output(raw)
            elif output_format == "latex":
                result.latex_output = raw.strip()
            else:
                result.markdown_output = raw.strip()
            
            print(f"   ✅ Generated in {elapsed:.1f}s")
            print(f"      Tokens: {result.prompt_tokens} prompt + "
                  f"{result.completion_tokens} completion = {result.total_tokens} total")
            
            break
            
        except Exception as e:
            print(f"   ⚠️ Generation failed on attempt {attempt}: {e}")
            result.error = str(e)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
    
    # ── Save outputs ──
    model_short = _model_shortname(model)
    
    if result.latex_output:
        latex_path = outputs_dir / f"generated_related_work_{model_short}.tex"
        latex_path.write_text(result.latex_output, encoding="utf-8")
        print(f"   ✅ Saved {latex_path.name}")
    
    if result.markdown_output:
        md_path = outputs_dir / f"generated_related_work_{model_short}.md"
        md_path.write_text(result.markdown_output, encoding="utf-8")
        print(f"   ✅ Saved {md_path.name}")
    
    if result.raw_output:
        raw_path = outputs_dir / f"generated_raw_{model_short}.txt"
        raw_path.write_text(result.raw_output, encoding="utf-8")
        print(f"   ✅ Saved {raw_path.name}")
    
    # ── Save generation log ──
    log_data = result.to_dict()
    log_data["prompt_length_chars"] = len(prompt)
    log_data["paper_text_length"] = len(paper_text)
    log_data["bib_entry_count"] = len(enriched_entries) if enriched_entries else len(parse_bib_file(bib_path))
    
    log_path = outputs_dir / f"generation_log_{model_short}.json"
    log_path.write_text(
        json.dumps(log_data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"   📋 Generation log → {log_path.name}")
    
    return result


def _model_shortname(model: str) -> str:
    """Convert model ID to a filesystem-safe short name."""
    # "anthropic/claude-sonnet-4-20250514" → "claude-sonnet-4"
    name = model.split("/")[-1]
    # Remove date suffixes like -20250514
    name = re.sub(r'-\d{8}$', '', name)
    # Remove other long suffixes
    name = re.sub(r'-\d{4}-\d{2}-\d{2}$', '', name)
    return name
