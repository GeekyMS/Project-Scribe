# paper19 (BERT) — Bibliography Bug Report

**File:** `pipeline/bib_extractor.py`  
**Function:** `_split_bibliography_entries()` → `match_author_date_citations()`  
**Symptom:** `entries_resolved = 56` when only 28 citations were found  

---

## Background: What the pipeline is trying to do

The pipeline has two jobs for bibliography handling:

1. **Parse citation keys** from the Related Work text — e.g. pull out strings like
   `"Mikolov et al., 2013"` or `"Brown et al., 1992"` from in-text citations.

2. **Match each key to its full bibliography entry** — find the actual reference line in
   the References section that corresponds to that key, e.g.:
   ```
   Tomas Mikolov, Ilya Sutskever, Kai Chen... 2013. Distributed representations...
   ```
   That raw text entry is then passed to the LLM to generate proper BibTeX.

The matching happens in `match_author_date_citations()`. It needs the references
section split into individual entries first, so it can check each one. That splitting
is the job of `_split_bibliography_entries()`.

---

## What Docling's output looks like for paper19

Docling outputs the references section as **one entry per line, no blank lines between entries**:

```
## References
Alan Akbik, Duncan Blythe, and Roland Vollgraf. 2018. Contextual string embeddings...
Rami Al-Rfou, Dokook Choe, Noah Constant... 2018. Character-level language modeling...
Rie Kubota Ando and Tong Zhang. 2005. A framework for learning predictive structures...
John Blitzer, Ryan McDonald, and Fernando Pereira. 2006. Domain adaptation...
Samuel R. Bowman, Gabor Angeli... 2015. A large annotated corpus...
Peter F Brown, Peter V Desouza... 1992. Class-based n-gram models...
... (one line per entry, ~56 entries total across pages 10–11)
```

This is clean, correct output. The problem is entirely in the code that tries to split it.

---

## The bug: `_split_bibliography_entries()` failing in two stages

### Stage 1 — Double-newline split (Strategy 1)

```python
entries = re.split(r'\n\s*\n', bibliography_text)
```

This splits on **blank lines** between entries. PyMuPDF output typically preserves blank
lines between bibliography entries, so this works there. Docling does **not** insert
blank lines — it outputs one entry per line. Result: the regex finds zero blank lines,
and returns the **entire bibliography as a single string**. That's 1 chunk, which fails
the `>= 5 entries` check, so it falls through to Strategy 2.

### Stage 2 — Capital-letter line detection (Strategy 2)

```python
entry_start = re.compile(r'^[A-Z][a-z]')
```

This tries to detect the start of a new entry by looking for lines that begin with a
capital letter. The logic:

```python
if entry_start.match(stripped) and not skip_pattern.match(stripped) and current_entry:
    # flush current_entry, start a new one
```

The `and current_entry` guard means: *only flush when we're already mid-entry.*

**Why it fails for paper19:**

Every single bibliography line in an author-date bibliography starts with a capital letter
(the author's surname). That means **every line triggers a flush of the previous one**,
so each "entry" is just a single line — not a full citation, just one author + partial title.

Strategy 2 does technically return many "entries" but they are all wrong/too short.

---

## How the bad split poisons the matching step

`match_author_date_citations()` receives the list of "entries" and tries to find which
one contains the citation's author surname and year:

```python
for entry in all_entries:
    if author_name.lower() in entry.lower() and year in entry:
        matched[key] = entry
        break
```

Because Strategy 1 returned **one giant blob** (the entire references section as a single
string), and Strategy 2's output was discarded in favor of Strategy 1's because the
fallback logic compares lengths:

```python
return entries if len(entries) >= len(fallback) else fallback
```

Strategy 2 returned many short fragments (> 5 items), so it was used... but wait — let's
look at what actually happened from the `references.json` output.

**What `references.json` actually shows:**

Every citation key maps to one of two values:
- `"References\nAlan Akbik..."` — the entire page-10 references block (starting from the
  heading)
- `"--- PAGE 11 ---\nMandar Joshi..."` — the entire page-11 references block

So in practice, `extract_bibliography_section()` found the references section spanning
two pages, and `_split_bibliography_entries()` returned **two chunks** (one per page,
split at the `--- PAGE 11 ---` marker which contains a blank line above it). Two chunks
fails the `>= 5` check, so both strategies were tried, but the fallback returned these
two big page-chunks as "entries."

Each of the 28 citation keys then matched against one of those two page-wide blobs,
because the author name + year are definitely somewhere in ~13,000 chars of text.

---

## The cascade to the LLM

`generate_bibtex()` receives:

```python
list(matched_entries.values())
```

Since all 28 keys map to one of only **2 unique blobs** (page 10 blob and page 11 blob),
the LLM effectively receives those two giant reference blocks and is asked to generate
BibTeX. It correctly generates BibTeX for every entry it can see in both blobs — which
happens to be approximately 56 total entries. Those 56 entries are real and correct,
but they include references the Related Work section never actually cited.

---

## Why this is worse than it sounds

The point of the matching step is **precision** — only include references that were
cited in the Related Work section, not the entire bibliography of the paper. Here:

- **28 citations were found** in the Related Work text  
- **56 BibTeX entries were generated** — the entire bibliography of the paper

The pipeline is supposed to produce a curated `.bib` file for the Related Work section
only. Instead it produced the full bibliography. If you were using this output to write
a new paper, you'd have twice as many unnecessary references in your `.bib`.

---

## Context: PyMuPDF was even worse on this paper

Looking at `test_data/paper19_truth.json`, the PyMuPDF citation keys for this paper were
completely garbled due to two-column interleaving:

```json
"Col-\nwords has been an active area of research for    lobert and Weston, 2008",
"Conneau et  al.,\n                                        of Transformers has...2017",
"Dai\nare an integral part of modern NLP systems, of-   and Le, 2015",
```

The truth file records `"expected_match_count": 0` — PyMuPDF couldn't match a single
citation because the citation keys themselves were garbage. So Docling is a large step
forward even with this bug: it correctly finds all 28 clean citation keys, correctly
identifies the bibliography section, and the only problem is that the matched "entries"
are too coarse-grained.

---

## The fix

`_split_bibliography_entries()` needs a third strategy: **split on individual lines when
the bibliography has no blank lines between entries** (the Docling case).

The heuristic to detect this case is simple: if Strategy 1 returns fewer than 5 entries
AND the bibliography text has many lines each containing a 4-digit year, treat each
non-empty, non-heading line as its own entry:

```python
# Strategy 3: Single-line entries (Docling-style clean output)
# Detect when bibliography entries are one-per-line with no blank separators.
# Heuristic: if > 80% of non-empty lines contain a 4-digit year, it's a
# one-entry-per-line bibliography.
lines = [l.strip() for l in bibliography_text.split('\n') if l.strip()]
year_lines = [l for l in lines if re.search(r'\b\d{4}\b', l)]
if len(lines) > 0 and len(year_lines) / len(lines) > 0.6:
    entries = [l for l in lines
               if len(l) > 20
               and not re.match(r'^(---|##|References|Bibliography)', l, re.IGNORECASE)]
    if len(entries) >= 5:
        return entries
```

This would correctly return ~56 individual lines for paper19, and the matching step
would then bind each of the 28 citation keys to its **specific single-line entry**
rather than to an entire page of references.
