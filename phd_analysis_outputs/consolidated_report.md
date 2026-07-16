# Project Scribe — Consolidated Evaluation Report

**Papers evaluated**: 6
**Model**: anthropic/claude-sonnet-4.6
**Enrichment**: Enabled for 2603.29890v1 (3/35 abstracts found); disabled for others (--no-enrich)

## Summary Table

| Paper | Recall | Precision | Halluc. | Missing | Judge Avg | Leakage | Length Ratio | Words (Gen/GT) |
|-------|--------|-----------|---------|---------|-----------|---------|--------------|----------------|
| 2503.07928v3 | 100% | 100% | 0 | 0 | 3.1/5.0 | none | 1.35x | 999/824 |
| 2601.23096v2 | 100% | 100% | 0 | 0 | 4.9/5.0 | minor | 2.16x | 1178/568 |
| 2603.29890v1 🆕 | 100% | 100% | 0 | 0 | **5.0/5.0** | minor | 2.23x | 1476/705 |
| 2604.05160v1 | 100% | 100% | 0 | 0 | 4.9/5.0 | minor | 1.56x | 1078/703 |
| 2605.30051v1 | 100% | 100% | 0 | 0 | 4.6/5.0 | minor | 2.92x | 1064/385 |
| Cross_Rubric_Generalization__CEUR_2026_ (2) | 100% | 100% | 0 | 0 | 4.4/5.0 | major | 2.07x | 858/416 |
| **Avg** | **100%** | **100%** | — | — | **4.48/5.0** | — | **2.05x** | — |

## Detailed Judge Scores (per dimension)

| Paper | Citation Accuracy | Coverage | Positioning | Depth | Flow | Organization | Overall Quality | Avg |
|-------|------|------|------|------|------|------|------|-----|
| 2503.07928v3 | 5 | 2 | 3 | 4 | 3 | 2 | 3 | 3.1 |
| 2601.23096v2 | 5 | 4 | 5 | 5 | 5 | 5 | 5 | 4.9 |
| 2603.29890v1 🆕 | **5** | **5** | **5** | **5** | **5** | **5** | **5** | **5.0** |
| 2604.05160v1 | 4 | 5 | 5 | 5 | 5 | 5 | 5 | 4.9 |
| 2605.30051v1 | 4 | 5 | 5 | 4 | 5 | 5 | 4 | 4.6 |
| Cross_Rubric_Generalization__CEUR_2026_ (2) | 3 | 5 | 5 | 4 | 5 | 5 | 4 | 4.4 |
| **Avg** | 4.3 | 4.3 | 4.7 | 4.5 | 4.7 | 4.5 | 4.3 | **4.48** |

## Knowledge Leakage Analysis

- **None**: 1/6 papers
- **Minor**: 4/6 papers
- **Major**: 1/6 papers

**Paper 2603.29890v1** (minor) 🆕:
  - The generated text is described by the judge as exhibiting minor leakage — it provides marginally more detailed descriptions of some cited methods than the source material provides, though all citations themselves are accurate and present in the bibliography.

**Paper 2601.23096v2** (minor):
  - The generated text provides more detailed descriptions of methods like Focal Loss ('down-weights well-classified examples'), Mixup ('trains on convex combinations of example pairs'), and Dirichlet cal...
  - The citation to 'DeepSeek-R1 (DeepSeek-AI et al., 2025)' is not present in the original text and seems to be an addition from the model's knowledge base, possibly referencing a very recent or pre-prin...

**Paper 2604.05160v1** (minor):
  - Includes a new citation, 'Walkington et al., 2025b', which is not in the original related works section....
  - Provides a more detailed description of EduAgentQG, listing its 'five specialized agents—a planner, a writer, a solver, an educator, and a checker', whereas the original was less specific....

**Paper 2605.30051v1** (minor):
  - The claim that the LearnLM AI model made students '5.5 percentage points more likely to solve novel problems' is a specific quantitative result not present in the provided paper context....
  - The description of the findings from Cao et al. (2026) that 'fine-tuned models produced realistic but brief responses, whereas multi-agent and DPO approaches generated more explicit reasoning behind s...

**Paper Cross_Rubric_Generalization__CEUR_2026_ (2)** (major):
  - The generated text invents specific methodological details for papers that are only listed by number and a brief topic in the original. For example, for citation [11] ('multi-agent extraction of rubri...
  - The model assigns future publication dates (2024, 2025, 2026) to several citations, which are clear fabrications (e.g., 'Do et al. (2025)', 'Wang et al. (2026)')....

## Structural Analysis

| Paper | Gen Words | GT Words | Word Ratio | Gen Paragraphs | GT Paragraphs | Gen Subsections |
|-------|-----------|----------|------------|----------------|---------------|-----------------|
| 2503.07928v3 | 999 | 824 | 1.21x | 13 | 2 | 5 |
| 2601.23096v2 | 1178 | 568 | 2.07x | 13 | 2 | 3 |
| 2603.29890v1 🆕 | **1476** | 705 | 2.09x | 17 | 2 | 3 |
| 2604.05160v1 | 1078 | 703 | 1.53x | 16 | 2 | 4 |
| 2605.30051v1 | 1064 | 385 | 2.76x | 16 | 1 | 7 |
| Cross_Rubric_Generalization__CEUR_2026_ (2) | 858 | 416 | 2.06x | 11 | 2 | 5 |

## Token Usage & Generation Time

| Paper | Prompt Tokens | Completion Tokens | Total Tokens | Generation Time (s) |
|-------|---------------|-------------------|--------------|---------------------|
| 2503.07928v3 | 19,260 | 2,962 | 22,222 | 47.0 |
| 2601.23096v2 | 43,580 | 3,544 | 47,124 | 60.4 |
| 2603.29890v1 🆕 | **35,878** | **4,503** | **40,381** | **76.4** |
| 2604.05160v1 | 13,515 | 3,320 | 16,835 | 56.0 |
| 2605.30051v1 | 22,501 | 3,103 | 25,604 | 48.7 |
| Cross_Rubric_Generalization__CEUR_2026_ (2) | 19,390 | 2,650 | 22,040 | 41.8 |
| **Total** | **154,124** | **20,082** | **174,206** | — |

## Key Findings

1. **Citation Accuracy**: Average recall 100%, precision 100% across all 6 papers
   - Zero hallucinated citations in any run
2. **Judge Quality**: Average score 4.48/5.0 (up from 4.37 with 5 papers)
   - 2603.29890v1 is the **first paper to achieve a perfect 5.0/5.0** across all 7 dimensions
3. **Length**: Generated sections are ~2.05x longer than ground truth on average
4. **Knowledge Leakage**: 4 minor, 1 major out of 6 papers (only 1 clean)
5. **Structure**: Model consistently adds subsections and better organisation vs. original
6. **Narrative Arc**: 2603.29890v1 scored 1.0 composite arc (vs 0.87 for the original) — the AI version was judged to have *more explicit* argumentative structure than the human-written one

## Argumentative Metrics

### Per-Citation Analysis (Tier 2B)

| Paper | Role Match | Emphasis Match | Role JSD | Exact/Adj/Mis | Precise/Partial/Generic |
|-------|------------|----------------|----------|---------------|-------------------------|
| 2503.07928v3 | 0.38 | 0.25 | 0.397 | 3/0/5 | 1/2/5 |
| 2603.29890v1 🆕 | — | — | — | — | — |
| 2604.05160v1 | 0.79 | 0.83 | 0.064 | 9/1/2 | 9/2/1 |
| Cross_Rubric_Generalization__CEUR_2026_ (2) | 0.80 | 0.55 | 0.037 | 8/0/2 | 1/9/0 |
| **Avg** | **0.66** | **0.54** | **0.166** | — | — |

> ℹ️ Tier 2B role analysis returned empty for 2603.29890v1 (uses numerical citation style — the judge's per-citation matching may need key alignment fixes for numeric refs).

### Section-Level Coherence (Tier 2C)

| Paper | Challenges | Align Coverage | Align Relevance | Align F1 | Arc Score |
|-------|------------|----------------|-----------------|----------|-----------|
| 2503.07928v3 | 3 | 0.67 | 1.00 | 0.80 | 0.57 |
| 2601.23096v2 | 3 | 1.00 | 0.94 | 0.97 | 0.00 |
| 2603.29890v1 🆕 | **4** | 0.75 | 1.00 | **0.86** | **1.00** |
| 2604.05160v1 | 4 | 1.00 | 1.00 | 1.00 | 1.00 |
| 2605.30051v1 | 5 | 1.00 | 1.00 | 1.00 | 1.00 |
| Cross_Rubric_Generalization__CEUR_2026_ (2) | 3 | 0.67 | 1.00 | 0.80 | 1.00 |
| **Avg** | — | — | — | **0.907** | **0.76** |

> 🆕 2603.29890v1 coherence summary: *"The generated text exhibits a slightly higher degree of argumentative coherence due to its exceptionally clear and explicit narrative structure. It methodically establishes the landscape, details sub-problems in dedicated paragraphs, consolidates limitations, and uses a powerful concluding paragraph for positioning. The AI arc score (1.0) exceeds the original (0.87)."*

## Aggregated Strengths & Weaknesses

### Strengths (from LLM Judge)
- **Paper 2503.07928v3**: Provides accurate and faithful descriptions of the cited papers.
- **Paper 2601.23096v2**: Excellent organization that perfectly matches the logical structure of the original.
- **Paper 2603.29890v1** 🆕: Perfect citation accuracy, coverage, depth, flow, and organisation across all 7 dimensions. The AI's narrative arc was judged more explicit and well-structured than the human-written original.
- **Paper 2604.05160v1**: Excellent organization with clear, logical subheadings that improve on the original structure.
- **Paper 2605.30051v1**: Excellent organization with clear, logical subsections.
- **Paper Cross_Rubric_Generalization__CEUR_2026_ (2)**: Excellent organization into logical thematic subsections with clear headings.

### Weaknesses (from LLM Judge)
- **Paper 2503.07928v3**: Major coverage gaps: It completely fails to discuss the literature on 'Education Discourse Analysis' and 'Education Dialogue Datasets', which are two of the three core pillars of the original related work section.
- **Paper 2601.23096v2**: Misses a few specific citations from the original text (e.g., citations by Leng et al. and Xiao et al.) related to the confidence inflation phenomenon, although it does describe the phenomenon itself.
- **Paper 2603.29890v1** 🆕: Minor knowledge leakage — slightly more detailed descriptions of cited methods than source material provides; otherwise no identified weaknesses at judge level.
- **Paper 2604.05160v1**: Shows evidence of minor knowledge leakage, introducing details and citations not present in the original text. This makes it less a pure replication and more of an enhancement.
- **Paper 2605.30051v1**: Exhibits knowledge leakage by including specific quantitative and qualitative details about cited works that are not in the provided source texts.
- **Paper Cross_Rubric_Generalization__CEUR_2026_ (2)**: Contains significant factual inaccuracies, inventing specific methods and findings for cited papers.
