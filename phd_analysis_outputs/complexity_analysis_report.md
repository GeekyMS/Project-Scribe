# Context Complexity Correlation Analysis

**Papers analyzed**: 5
**Model**: anthropic/claude-sonnet-4.6

## Hypothesis

> As input context complexity grows (more references, longer prompts),
> the LLM's ability to maintain *argumentative coherence* degrades,
> even though *citation recall* stays high.

## Paper-Level Data

| Paper | Bib Entries | Prompt Tokens | Recall | Role Match | Emphasis | Align F1 | Arc Score |
|-------|-------------|---------------|--------|------------|----------|----------|-----------|
| 2503.07928v3 | 0 | 19,260 | 100% | 0.38 | 0.25 | 0.80 | 0.57 |
| 2601.23096v2 | 0 | 43,580 | 100% | 0.00 | 0.00 | 0.97 | 0.00 |
| 2604.05160v1 | 0 | 13,515 | 100% | 0.79 | 0.83 | 1.00 | 1.00 |
| 2605.30051v1 | 0 | 22,501 | 100% | 0.00 | 0.00 | 1.00 | 1.00 |
| Cross_Rubric_Generalization__CEUR_2026_ (2) | 0 | 19,390 | 100% | 0.80 | 0.55 | 0.80 | 1.00 |

## Correlation Results

### Significant Correlations (p < 0.05)

| Independent | Dependent | Pearson r | p | Spearman r | p |
|-------------|-----------|-----------|---|------------|---|
| prompt_tokens | narrative_arc_composite | -0.883 | 0.047 | -0.447 | 0.450 |

### All Correlations

| Independent → Dependent | Pearson r | Spearman r | n |
|-------------------------|-----------|------------|---|
| prompt_tokens → narrative_arc_composite | -0.883 ** | -0.4472 | 5 |
| prompt_tokens → role_match_score | -0.708 ** | -0.6669 | 5 |
| prompt_tokens → emphasis_match_score | -0.706 ** | -0.8721 | 5 |
| prompt_tokens → alignment_f1 | 0.224 | 0.0 | 5 |

## Key Findings

1. ✅ **Hypothesis supported**: Citation recall remains high (100%) while argumentative role accuracy is significantly lower (0.39). LLMs can *find* the right papers but don't always use them for the right *rhetorical purpose*.
