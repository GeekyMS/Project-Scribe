"""
Human Evaluation Questionnaire Generator — Author Edition
==========================================================
Generates two print-ready HTML questionnaires (designed for browser → Save as PDF):

  Version 1 (Revealed):  AI text shown first, original shown as reference.
                          Full Likert + open-ended + author-specific questions.

  Version 2 (Blind):     Both texts shown without labels, randomised order.
                          A/B preference questions + authorship detection + open-ended.

Usage:
    python -m pipeline.questionnaire_generator \\
        --generated  outputs/generation/generated_related_work_<model>.md \\
        --ground-truth outputs/related_work.txt \\
        --output-dir outputs/generation \\
        --paper-title "My Paper Title" \\
        --model claude-sonnet-4.6
"""

import argparse
import json
import random
from pathlib import Path
from datetime import date


# ─────────────────────────────────────────────────────────────────────────────
# REVEALED VERSION (Version 1) — author knows which is AI
# ─────────────────────────────────────────────────────────────────────────────

_REVEALED_QUESTIONS = [
    # ── SECTION A: Factual Accuracy ─────────────────────────────────────────
    {
        "section": "A. Factual Accuracy",
        "section_desc": "These questions focus on whether the AI correctly describes the cited papers.",
        "id": "A1",
        "type": "likert",
        "label": "Citation Accuracy",
        "question": "Are the cited papers described correctly? Does the AI text accurately represent what each paper contributes, or does it misstate methods, results, or claims?",
        "scale": ["1 — Major factual errors", "2 — Several inaccuracies", "3 — Mostly correct", "4 — Minor issues only", "5 — Fully accurate"],
    },
    {
        "section": "A. Factual Accuracy",
        "id": "A2",
        "type": "likert",
        "label": "Citation Relevance Precision",
        "question": "When the AI describes a cited paper, does it highlight the aspect <em>most relevant to this paper's argument</em>, or does it give a generic description of what the cited paper does?",
        "scale": ["1 — All generic descriptions", "2 — Mostly generic", "3 — Mixed", "4 — Mostly precise", "5 — Every description is precisely relevant"],
    },
    {
        "section": "A. Factual Accuracy",
        "id": "A3",
        "type": "open",
        "label": "Specific Factual Errors",
        "question": "Are there any specific citations where the AI has misrepresented what the paper says, exaggerated claims, or attributed findings incorrectly? Please quote the relevant passage and describe the error.",
    },
    {
        "section": "A. Factual Accuracy",
        "id": "A4",
        "type": "open",
        "label": "Your Paper's Contribution",
        "question": "As one of the authors of this paper: does the AI-generated Related Works misrepresent <em>your paper's</em> contribution or research gap? If so, please describe.",
    },

    # ── SECTION B: Coverage ──────────────────────────────────────────────────
    {
        "section": "B. Coverage",
        "section_desc": "These questions assess whether the AI covers the right topics and citations.",
        "id": "B1",
        "type": "likert",
        "label": "Thematic Coverage",
        "question": "Does the AI section cover the same research themes and topics as your original? Are any important sub-areas missed or over-emphasised?",
        "scale": ["1 — Misses most themes", "2 — Covers some themes", "3 — Covers main themes", "4 — Minor gaps only", "5 — Comprehensive"],
    },
    {
        "section": "B. Coverage",
        "id": "B2",
        "type": "open",
        "label": "Missing Citations",
        "question": "Are any citations that appear in your original Related Works section missing from the AI version? List them and explain why each is important.",
    },
    {
        "section": "B. Coverage",
        "id": "B3",
        "type": "open",
        "label": "Hallucinated or Spurious Citations",
        "question": "Does the AI include any citations that do not appear in your reference list, or references that seem to be invented? Please list any suspicious entries.",
    },

    # ── SECTION C: Argumentation & Positioning ───────────────────────────────
    {
        "section": "C. Argumentation & Positioning",
        "section_desc": "These questions focus on how well the AI constructs the scholarly argument.",
        "id": "C1",
        "type": "likert",
        "label": "Framing & Paper Positioning",
        "question": "Does the AI version properly frame your paper's contribution relative to prior work? After reading it, would a reader understand <em>why your paper needs to exist</em>?",
        "scale": ["1 — No positioning", "2 — Weak positioning", "3 — Some positioning", "4 — Good positioning", "5 — Excellent positioning"],
    },
    {
        "section": "C. Argumentation & Positioning",
        "id": "C2",
        "type": "likert",
        "label": "Argumentative Role Fidelity",
        "question": "For the major citations, does the AI use each reference for the <em>same rhetorical purpose</em> as your original? (e.g., if you cite a paper to contrast against it, does the AI also contrast — or does it just describe it generically?)",
        "scale": ["1 — Completely different roles", "2 — Most roles differ", "3 — Some match, some differ", "4 — Minor differences only", "5 — Same argumentative roles throughout"],
    },
    {
        "section": "C. Argumentation & Positioning",
        "id": "C3",
        "type": "likert",
        "label": "Argumentative Progression",
        "question": "Does the AI section build a logical argument — progressing from broad context → specific sub-problems → gaps in existing work → why your approach is necessary?",
        "scale": ["1 — Flat list, no progression", "2 — Weak structure", "3 — Some structure", "4 — Good progression", "5 — Clear, compelling arc"],
    },
    {
        "section": "C. Argumentation & Positioning",
        "id": "C4",
        "type": "open",
        "label": "Rhetorical Role Changes",
        "question": "Please identify 1–3 specific citations where the AI uses the reference for a <em>different rhetorical purpose</em> than your original (e.g., you use it as a contrast, the AI uses it as support). Quote both versions.",
    },

    # ── SECTION D: Quality & Usability ──────────────────────────────────────
    {
        "section": "D. Quality & Usability",
        "section_desc": "These questions assess the overall writing quality and practical usefulness of the AI output.",
        "id": "D1",
        "type": "likert",
        "label": "Depth of Engagement",
        "question": "Does the AI engage substantively with the cited work, or does it mostly just name-drop? Does it explain <em>what each paper does</em> and <em>why it matters here</em>?",
        "scale": ["1 — Pure listing", "2 — Mostly superficial", "3 — Some depth", "4 — Good depth", "5 — Substantive engagement throughout"],
    },
    {
        "section": "D. Quality & Usability",
        "id": "D2",
        "type": "likert",
        "label": "Flow & Coherence",
        "question": "Does the AI section read as a coherent narrative, or does it feel like a disconnected list of summaries?",
        "scale": ["1 — Disconnected", "2 — Some transitions", "3 — Reasonable flow", "4 — Good narrative", "5 — Seamless narrative"],
    },
    {
        "section": "D. Quality & Usability",
        "id": "D3",
        "type": "likert",
        "label": "Overall Quality",
        "question": "Overall, would this AI-generated Related Works section pass peer review as-is?",
        "scale": ["1 — Unacceptable", "2 — Needs major revision", "3 — Adequate (minor revision)", "4 — Good quality", "5 — Publication-ready"],
    },
    {
        "section": "D. Quality & Usability",
        "id": "D4",
        "type": "open",
        "label": "Usability as a Draft",
        "question": "Would you use this AI output as a starting draft for your Related Works section? What would you keep, and what would you need to rewrite?",
    },
    {
        "section": "D. Quality & Usability",
        "id": "D5",
        "type": "open",
        "label": "Missing Context or Nuance",
        "question": "Is there important context, nuance, or community knowledge about any of the cited papers that the AI misses — knowledge that only an expert in this field would know?",
    },

    # ── SECTION E: Author Perspective ────────────────────────────────────────
    {
        "section": "E. Author Perspective",
        "section_desc": "As the paper's author, you have unique insight that external evaluators cannot provide. These questions are for you specifically.",
        "id": "E1",
        "type": "open",
        "label": "Most Important Difference",
        "question": "What is the single most important difference between your original Related Works and the AI-generated version — either something the AI does much better, or something it gets fundamentally wrong?",
    },
    {
        "section": "E. Author Perspective",
        "id": "E2",
        "type": "open",
        "label": "Authorial Intent",
        "question": "As the author, were there deliberate choices in how you organised and framed your Related Works (e.g., choosing to contrast specific papers, ordering themes to build a specific argument)? Does the AI version preserve or ignore those choices?",
    },
    {
        "section": "E. Author Perspective",
        "id": "E3",
        "type": "open",
        "label": "Surprising Observations",
        "question": "Was there anything in the AI-generated version that surprised you — either pleasantly (e.g., a connection you hadn't considered) or negatively (e.g., a confident-sounding but wrong claim)?",
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# BLIND VERSION (Version 2) — author does not know which is AI
# ─────────────────────────────────────────────────────────────────────────────

_BLIND_QUESTIONS = [
    {
        "section": "Part 1: Preference Judgements",
        "section_desc": 'For each question, choose <strong>Version A</strong> or <strong>Version B</strong>, or <strong>Tie</strong> if they are equal. <em>You do not know which version was written by the authors and which was generated by AI.</em>',
        "id": "P1", "type": "ab", "label": "Citation Accuracy",
        "question": "Which version describes the cited papers more accurately and faithfully — without overstating, understating, or misrepresenting their contributions?",
    },
    {
        "section": "Part 1: Preference Judgements",
        "id": "P2", "type": "ab", "label": "Citation Relevance",
        "question": "Which version does a better job of highlighting the specific aspect of each cited paper that is <em>most relevant to this paper's argument</em>, rather than giving a generic description?",
    },
    {
        "section": "Part 1: Preference Judgements",
        "id": "P3", "type": "ab", "label": "Narrative Flow",
        "question": "Which version reads as a more coherent, connected narrative rather than a disconnected list of summaries?",
    },
    {
        "section": "Part 1: Preference Judgements",
        "id": "P4", "type": "ab", "label": "Paper Positioning",
        "question": "Which version better positions this paper's contribution relative to prior work — making clear <em>why this paper needs to exist</em>?",
    },
    {
        "section": "Part 1: Preference Judgements",
        "id": "P5", "type": "ab", "label": "Thematic Organisation",
        "question": "Which version organises the related work into more logical, coherent thematic groups?",
    },
    {
        "section": "Part 1: Preference Judgements",
        "id": "P6", "type": "ab", "label": "Depth of Engagement",
        "question": "Which version engages more deeply with the cited works — explaining <em>what</em> each paper does and <em>why it matters here</em>?",
    },
    {
        "section": "Part 1: Preference Judgements",
        "id": "P7", "type": "ab", "label": "Argumentative Progression",
        "question": "Which version better builds a logical argument — moving from broad context → specific sub-problems → gaps in existing work → why this approach is necessary?",
    },
    {
        "section": "Part 1: Preference Judgements",
        "id": "P8", "type": "ab", "label": "Citation Argumentation",
        "question": "In which version do the citations feel like they are <em>advancing an argument</em> (e.g., contrasting approaches, establishing gaps, building a foundation), rather than just listing related work?",
    },
    {
        "section": "Part 1: Preference Judgements",
        "id": "P9", "type": "ab", "label": "Overall Preference",
        "question": "Setting aside everything else: which version would you prefer to appear in the final published paper?",
    },

    # ── Part 2: Detection ─────────────────────────────────────────────────────
    {
        "section": "Part 2: Authorship Detection",
        "section_desc": "One of the two versions above was written by the paper's original authors. The other was generated by an AI. Please try to identify which is which.",
        "id": "D1", "type": "detect",
        "label": "Which version is AI-generated?",
        "question": "Based on your reading, which version do you believe was generated by AI?",
    },

    # ── Part 3: Open-ended ────────────────────────────────────────────────────
    {
        "section": "Part 3: Open-Ended Observations",
        "section_desc": "Please answer these in your own words.",
        "id": "O1", "type": "open",
        "label": "Specific Differences",
        "question": "Are there specific citations or passages where one version is notably better or worse than the other? Please quote the relevant text and explain.",
    },
    {
        "section": "Part 3: Open-Ended Observations",
        "id": "O2", "type": "open",
        "label": "Authorial Intent",
        "question": "As the paper's author: were there deliberate choices in how you organised or framed your Related Works (e.g., ordering themes, contrasting specific papers)? Does either version preserve those choices?",
    },
    {
        "section": "Part 3: Open-Ended Observations",
        "id": "O3", "type": "open",
        "label": "Surprising Observations",
        "question": "Was there anything in either version that surprised you — a connection you hadn't considered, a confident-sounding but wrong claim, or an unusually good description of a paper?",
    },
    {
        "section": "Part 3: Open-Ended Observations",
        "id": "O4", "type": "open",
        "label": "Additional Comments",
        "question": "Any other observations about the two versions?",
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# HTML TEMPLATE
# ─────────────────────────────────────────────────────────────────────────────

_CSS = """
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Lora:ital,wght@0,400;0,600;1,400&display=swap');

  :root {
    --primary: #1a1a2e;
    --accent:  #4f46e5;
    --accent2: #7c3aed;
    --surface: #f8f7ff;
    --border:  #e0deff;
    --text:    #1e1e2e;
    --muted:   #6b6b8a;
    --good:    #059669;
    --warn:    #d97706;
    --page-w:  740px;
    --radius:  10px;
  }

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    font-family: 'Inter', sans-serif;
    font-size: 13.5px;
    line-height: 1.65;
    color: var(--text);
    background: #fff;
    max-width: var(--page-w);
    margin: 0 auto;
    padding: 36px 48px 60px;
  }

  /* ── Cover ── */
  .cover {
    border: 2px solid var(--accent);
    border-radius: var(--radius);
    padding: 32px 36px 28px;
    margin-bottom: 36px;
    background: linear-gradient(135deg, #f5f3ff 0%, #ede9fe 100%);
  }
  .cover .badge {
    display: inline-block;
    background: var(--accent);
    color: #fff;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 1.2px;
    text-transform: uppercase;
    padding: 3px 10px;
    border-radius: 20px;
    margin-bottom: 12px;
  }
  .cover h1 {
    font-family: 'Lora', serif;
    font-size: 22px;
    font-weight: 600;
    color: var(--primary);
    margin-bottom: 6px;
  }
  .cover .subtitle {
    font-size: 13px;
    color: var(--muted);
    margin-bottom: 18px;
  }
  .cover-meta {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px 24px;
    font-size: 12px;
    color: var(--muted);
    border-top: 1px solid var(--border);
    padding-top: 14px;
    margin-top: 4px;
  }
  .cover-meta strong { color: var(--text); }

  /* ── Instructions block ── */
  .instructions {
    background: #f0fdf4;
    border-left: 4px solid var(--good);
    border-radius: 0 var(--radius) var(--radius) 0;
    padding: 14px 18px;
    margin-bottom: 32px;
    font-size: 13px;
  }
  .instructions h2 { font-size: 13px; font-weight: 700; margin-bottom: 6px; color: var(--good); }

  /* ── Text blocks ── */
  .text-block {
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 18px 22px;
    margin-bottom: 28px;
    font-size: 13px;
    line-height: 1.7;
    background: var(--surface);
  }
  .text-block h2 {
    font-family: 'Lora', serif;
    font-size: 15px;
    font-weight: 600;
    color: var(--accent);
    margin-bottom: 12px;
    padding-bottom: 8px;
    border-bottom: 1px solid var(--border);
  }
  .text-block .text-label {
    display: inline-block;
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.9px;
    padding: 2px 8px;
    border-radius: 12px;
    margin-right: 8px;
    vertical-align: middle;
  }
  .label-ai   { background: #ede9fe; color: var(--accent2); }
  .label-orig { background: #ecfdf5; color: var(--good); }
  .label-ver  { background: #f3f4f6; color: #374151; }

  /* ── Section headers ── */
  .section-header {
    margin: 36px 0 4px;
    padding: 10px 16px;
    background: linear-gradient(90deg, var(--accent) 0%, var(--accent2) 100%);
    border-radius: var(--radius);
    color: #fff;
  }
  .section-header h2 {
    font-size: 14px;
    font-weight: 700;
    letter-spacing: 0.3px;
  }
  .section-desc {
    font-size: 12px;
    color: var(--muted);
    margin: 6px 0 18px;
    padding: 0 4px;
    font-style: italic;
  }

  /* ── Questions ── */
  .question-card {
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 16px 20px;
    margin-bottom: 16px;
    page-break-inside: avoid;
  }
  .q-header {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 8px;
  }
  .q-id {
    font-size: 10px;
    font-weight: 700;
    background: var(--accent);
    color: #fff;
    padding: 2px 8px;
    border-radius: 20px;
    letter-spacing: 0.5px;
    flex-shrink: 0;
  }
  .q-label {
    font-size: 13px;
    font-weight: 600;
    color: var(--primary);
  }
  .q-text {
    font-size: 13px;
    color: var(--text);
    margin-bottom: 12px;
  }
  .q-text em { color: var(--accent); font-style: italic; }

  /* Likert scale */
  .likert-row {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
  }
  .likert-option {
    border: 1.5px solid var(--border);
    border-radius: 8px;
    padding: 6px 10px;
    font-size: 11px;
    text-align: center;
    min-width: 60px;
    flex: 1;
    cursor: pointer;
    transition: border-color 0.15s, background 0.15s;
  }
  .likert-option:hover { border-color: var(--accent); background: #f5f3ff; }
  .likert-num { font-size: 15px; font-weight: 700; color: var(--accent); display: block; }
  .likert-desc { font-size: 10px; color: var(--muted); line-height: 1.3; }

  /* A/B radio */
  .ab-row {
    display: flex;
    gap: 10px;
  }
  .ab-option {
    flex: 1;
    border: 1.5px solid var(--border);
    border-radius: 8px;
    padding: 8px 12px;
    font-size: 13px;
    font-weight: 600;
    text-align: center;
    cursor: pointer;
    transition: border-color 0.15s, background 0.15s;
  }
  .ab-option:hover { border-color: var(--accent); background: #f5f3ff; }

  /* Detect question */
  .detect-block {
    display: flex;
    gap: 10px;
    margin-bottom: 10px;
  }
  .detect-option {
    flex: 1;
    border: 1.5px solid var(--border);
    border-radius: 8px;
    padding: 8px 12px;
    font-size: 13px;
    font-weight: 600;
    text-align: center;
    cursor: pointer;
    transition: border-color 0.15s, background 0.15s;
  }
  .detect-option:hover { border-color: var(--accent); background: #f5f3ff; }
  .confidence-row {
    display: flex;
    gap: 6px;
    margin-bottom: 10px;
  }
  .conf-option {
    width: 40px; height: 36px;
    border: 1.5px solid var(--border);
    border-radius: 8px;
    font-size: 15px;
    font-weight: 700;
    color: var(--accent);
    text-align: center;
    line-height: 36px;
    cursor: pointer;
    transition: border-color 0.15s, background 0.15s;
  }
  .conf-option:hover { border-color: var(--accent); background: #f5f3ff; }
  .conf-label { font-size: 11px; color: var(--muted); margin-bottom: 4px; }

  /* Open-ended */
  .open-box {
    width: 100%;
    min-height: 80px;
    border: 1.5px solid var(--border);
    border-radius: 8px;
    padding: 10px 12px;
    font-family: 'Inter', sans-serif;
    font-size: 13px;
    color: var(--text);
    resize: vertical;
    background: #fff;
    transition: border-color 0.15s;
  }
  .open-box:focus { outline: none; border-color: var(--accent); }

  /* ── Divider & footer ── */
  .divider {
    border: none;
    border-top: 1px solid var(--border);
    margin: 32px 0;
  }
  .footer {
    text-align: center;
    font-size: 11px;
    color: var(--muted);
    margin-top: 40px;
    padding-top: 16px;
    border-top: 1px solid var(--border);
  }

  /* ── Print ── */
  @media print {
    body { padding: 20px 30px 40px; font-size: 12px; }
    .cover { page-break-after: always; }
    .section-header { break-before: auto; }
    .question-card { page-break-inside: avoid; }
    .text-block { page-break-inside: auto; }
    .open-box { min-height: 60px; }
  }
"""


def _md_to_html(text: str) -> str:
    """Very lightweight Markdown → HTML (no deps)."""
    import re
    lines = text.split("\n")
    out = []
    for line in lines:
        line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
        line = re.sub(r"\*(.+?)\*", r"<em>\1</em>", line)
        line = re.sub(r"`(.+?)`", r"<code>\1</code>", line)
        if line.startswith("### "):
            out.append(f"<h4>{line[4:]}</h4>")
        elif line.startswith("## "):
            out.append(f"<h3>{line[3:]}</h3>")
        elif line.startswith("# "):
            out.append(f"<h3>{line[2:]}</h3>")
        elif line.strip() == "":
            out.append("<br>")
        else:
            out.append(f"<p>{line}</p>")
    return "\n".join(out)


def _render_revealed(
    generated: str,
    ground_truth: str,
    paper_title: str,
    model_name: str,
    today: str,
) -> str:
    """Render Version 1 (Revealed) HTML."""

    sections_seen = set()
    questions_html = []

    for q in _REVEALED_QUESTIONS:
        sec = q["section"]
        if sec not in sections_seen:
            sections_seen.add(sec)
            desc_html = (
                f'<p class="section-desc">{q.get("section_desc","")}</p>'
                if q.get("section_desc") else ""
            )
            questions_html.append(
                f'<div class="section-header"><h2>{sec}</h2></div>{desc_html}'
            )

        qid = q["id"]
        label = q["label"]
        qtext = q["question"]
        qtype = q["type"]

        inner = f"""
        <div class="question-card">
          <div class="q-header">
            <span class="q-id">{qid}</span>
            <span class="q-label">{label}</span>
          </div>
          <div class="q-text">{qtext}</div>
        """

        if qtype == "likert":
            scale = q["scale"]
            opts = "".join(
                f'<div class="likert-option">'
                f'<span class="likert-num">{i+1}</span>'
                f'<span class="likert-desc">{s.split("—",1)[1].strip() if "—" in s else s}</span>'
                f'</div>'
                for i, s in enumerate(scale)
            )
            inner += f'<div class="likert-row">{opts}</div>'
            inner += '<p class="conf-label" style="margin-top:10px">Comments (optional):</p>'
            inner += '<textarea class="open-box" style="min-height:44px" placeholder="Any specific observations…"></textarea>'

        elif qtype == "open":
            inner += '<textarea class="open-box" placeholder="Your response…"></textarea>'

        inner += "</div>"
        questions_html.append(inner)

    gen_html = _md_to_html(generated)
    gt_html = _md_to_html(ground_truth)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Related Works Evaluation — Revealed Version</title>
<style>{_CSS}</style>
</head>
<body>

<div class="cover">
  <div class="badge">Version 1 — Revealed</div>
  <h1>Related Works Human Evaluation</h1>
  <p class="subtitle">You are evaluating an AI-generated Related Works section for your paper.</p>
  <div class="cover-meta">
    <div><strong>Paper:</strong> {paper_title or "—"}</div>
    <div><strong>Model:</strong> {model_name or "—"}</div>
    <div><strong>Protocol:</strong> Revealed (you know which is AI)</div>
    <div><strong>Date:</strong> {today}</div>
  </div>
</div>

<div class="instructions">
  <h2>📋 Instructions</h2>
  <ul style="padding-left:16px;margin-top:4px">
    <li>The <strong>AI-generated</strong> Related Works section is shown below, followed by your <strong>original</strong> for reference.</li>
    <li>Please read both before answering the questions.</li>
    <li>For Likert questions, circle or tick the most appropriate number (1–5).</li>
    <li>For open-ended questions, write your answer in the space provided.</li>
    <li>There are no right or wrong answers — your expert judgement as the paper's author is what matters.</li>
  </ul>
</div>

<div class="text-block">
  <h2><span class="text-label label-ai">AI Generated</span>Related Works Section</h2>
  {gen_html}
</div>

<div class="text-block">
  <h2><span class="text-label label-orig">Original</span>Your Related Works Section (Reference)</h2>
  {gt_html}
</div>

<hr class="divider">

{''.join(questions_html)}

<div class="footer">
  Project Scribe · Human Evaluation · {today}<br>
  Please return this completed form to the researcher. Thank you for your time.
</div>

</body>
</html>"""


def _render_blind(
    generated: str,
    ground_truth: str,
    paper_title: str,
    model_name: str,
    today: str,
    mapping_path: Path,
) -> str:
    """Render Version 2 (Blind) HTML. Randomises A/B assignment."""

    if random.random() < 0.5:
        version_a, version_b = generated, ground_truth
        mapping = {"A": "generated", "B": "original"}
    else:
        version_a, version_b = ground_truth, generated
        mapping = {"A": "original", "B": "generated"}

    mapping_path.write_text(
        json.dumps({"mapping": mapping, "model": model_name,
                    "note": "DO NOT share with evaluator until after they submit."}, indent=2),
        encoding="utf-8",
    )

    sections_seen = set()
    questions_html = []

    for q in _BLIND_QUESTIONS:
        sec = q["section"]
        if sec not in sections_seen:
            sections_seen.add(sec)
            desc_html = (
                f'<p class="section-desc">{q.get("section_desc","")}</p>'
                if q.get("section_desc") else ""
            )
            questions_html.append(
                f'<div class="section-header"><h2>{sec}</h2></div>{desc_html}'
            )

        qid = q["id"]
        label = q["label"]
        qtext = q["question"]
        qtype = q["type"]

        inner = f"""
        <div class="question-card">
          <div class="q-header">
            <span class="q-id">{qid}</span>
            <span class="q-label">{label}</span>
          </div>
          <div class="q-text">{qtext}</div>
        """

        if qtype == "ab":
            inner += """
            <div class="ab-row">
              <div class="ab-option">Version A</div>
              <div class="ab-option">Version B</div>
              <div class="ab-option">Tie</div>
            </div>"""
            inner += '<p class="conf-label" style="margin-top:10px">Brief reason (optional):</p>'
            inner += '<textarea class="open-box" style="min-height:40px" placeholder="…"></textarea>'

        elif qtype == "detect":
            inner += """
            <div class="detect-block">
              <div class="detect-option">Version A is AI-generated</div>
              <div class="detect-option">Version B is AI-generated</div>
            </div>"""
            inner += '<p class="conf-label">Confidence (1 = guessing, 5 = certain):</p>'
            inner += """
            <div class="confidence-row">
              <div class="conf-option">1</div>
              <div class="conf-option">2</div>
              <div class="conf-option">3</div>
              <div class="conf-option">4</div>
              <div class="conf-option">5</div>
            </div>"""
            inner += '<p class="conf-label">Reasoning:</p>'
            inner += '<textarea class="open-box" style="min-height:44px" placeholder="What cues led you to this conclusion?"></textarea>'

        elif qtype == "open":
            inner += '<textarea class="open-box" placeholder="Your response…"></textarea>'

        inner += "</div>"
        questions_html.append(inner)

    va_html = _md_to_html(version_a)
    vb_html = _md_to_html(version_b)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Related Works Evaluation — Blind Version</title>
<style>{_CSS}</style>
</head>
<body>

<div class="cover">
  <div class="badge">Version 2 — Blind</div>
  <h1>Related Works Human Evaluation</h1>
  <p class="subtitle">You are comparing two versions of a Related Works section. You do <strong>not</strong> know which was written by the authors and which was generated by AI.</p>
  <div class="cover-meta">
    <div><strong>Paper:</strong> {paper_title or "—"}</div>
    <div><strong>Protocol:</strong> Blind A/B Comparison</div>
    <div><strong>Date:</strong> {today}</div>
    <div><strong>⚠️ Note:</strong> Do not share the mapping file until after submission.</div>
  </div>
</div>

<div class="instructions">
  <h2>📋 Instructions</h2>
  <ul style="padding-left:16px;margin-top:4px">
    <li>Two versions of the Related Works section are shown below, labelled <strong>Version A</strong> and <strong>Version B</strong>.</li>
    <li><strong>You do not know</strong> which was written by the paper's authors and which was AI-generated.</li>
    <li>Read both versions carefully, then answer the questions.</li>
    <li>For preference questions, choose A, B, or Tie.</li>
    <li>At the end, try to identify which version is AI-generated.</li>
  </ul>
</div>

<div class="text-block">
  <h2><span class="text-label label-ver">Version A</span>Related Works</h2>
  {va_html}
</div>

<div class="text-block">
  <h2><span class="text-label label-ver">Version B</span>Related Works</h2>
  {vb_html}
</div>

<hr class="divider">

{''.join(questions_html)}

<div class="footer">
  Project Scribe · Human Evaluation · {today}<br>
  Please return this completed form to the researcher. Thank you for your time.
</div>

</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────────────────────────

def generate_questionnaires(
    generated_text: str,
    ground_truth: str,
    model_name: str = "",
    paper_title: str = "",
    outputs_dir: str | Path = "outputs/generation",
) -> dict:
    """
    Generate both questionnaire versions (revealed + blind) as HTML files.

    Returns dict with paths:
      - revealed_html
      - blind_html
      - blind_mapping_json
    """
    outputs_dir = Path(outputs_dir)
    outputs_dir.mkdir(parents=True, exist_ok=True)

    today = date.today().strftime("%B %d, %Y")

    # Version 1: Revealed
    revealed_html = _render_revealed(
        generated_text, ground_truth, paper_title, model_name, today
    )
    revealed_path = outputs_dir / "questionnaire_revealed.html"
    revealed_path.write_text(revealed_html, encoding="utf-8")

    # Version 2: Blind
    mapping_path = outputs_dir / "questionnaire_blind_mapping.json"
    blind_html = _render_blind(
        generated_text, ground_truth, paper_title, model_name, today, mapping_path
    )
    blind_path = outputs_dir / "questionnaire_blind.html"
    blind_path.write_text(blind_html, encoding="utf-8")

    print(f"\n   📋 Questionnaires generated:")
    print(f"      Revealed:     {revealed_path}")
    print(f"      Blind:        {blind_path}")
    print(f"      Blind mapping (DO NOT share): {mapping_path}")

    return {
        "revealed_html": str(revealed_path),
        "blind_html": str(blind_path),
        "blind_mapping_json": str(mapping_path),
    }


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Generate human evaluation questionnaires for Project Scribe."
    )
    parser.add_argument("--generated", required=True, help="Path to AI-generated .md or .txt file")
    parser.add_argument("--ground-truth", required=True, help="Path to original related_work.txt")
    parser.add_argument("--output-dir", default="outputs/generation", help="Output directory")
    parser.add_argument("--paper-title", default="", help="Paper title for cover page")
    parser.add_argument("--model", default="", help="Model name for cover page")
    args = parser.parse_args()

    generated = Path(args.generated).read_text(encoding="utf-8")
    ground_truth = Path(args.ground_truth).read_text(encoding="utf-8")

    paths = generate_questionnaires(
        generated_text=generated,
        ground_truth=ground_truth,
        model_name=args.model,
        paper_title=args.paper_title,
        outputs_dir=args.output_dir,
    )
    print("\n   ✅ Done! Open the HTML files in a browser and use File → Print → Save as PDF.")
    return paths


if __name__ == "__main__":
    main()
