# Findings — Overleaf Risk Spike

**Date tested:** 2026-09-04
**Overleaf project used:** `long_test.tex` fixture, 998 lines / 152,366 chars (`extension/test-fixtures/long_test.tex`)

## 1. Reachability

- Path A (`UNSTABLE_editor:extensions`) resolved: [x] yes [ ] no
- Path B (`.cmView.view` walk) resolved: [x] yes [ ] no — winning candidate path: `.cm-content cmView.view`
- Did A and B resolve to the same object? [x] yes [ ] no

Both paths reach the live `EditorView` on first probe, and land on the identical object (`sameObject: true`). No ambiguity here.

## 2. Completeness

- `doc.length` at top of file: `152366`, `viewport`: `{from: 0, to: 4158}`
- `doc.length` at bottom of file: `152366`, `viewport`: `{from: 147247, to: 152366}`
- Hash identical between the two reads? [x] yes (`3b750706` both times) [ ] no
- `head`/`tail` markers (`SCRIBE-MARKER-TOP-0001` / `SCRIBE-MARKER-BOTTOM-9999`) present in both reads regardless of scroll: [x] yes
- Conclusion: [x] full-document access confirmed [ ] viewport-truncated

CodeMirror only rendered ~4-5KB into the DOM at any given scroll position (viewport range), but `view.state.doc` returned the complete 152KB document both times, byte-identical. This directly refutes the plan doc's premise that CM6 viewporting limits programmatic content access — that limit applies to what's painted into the DOM, not to the document model the EditorView API exposes.

## 3. Writability

- Test text appeared in the editor immediately: [x] yes
- Survived a full page reload: [x] yes (confirmed across multiple reloads and leaving/re-entering the project)
- If not, present in project history/track-changes: [ ] n/a — not needed, it persisted
- Conclusion: [x] edits persist to Overleaf's sync layer [ ] edits are local-only

`view.dispatch({changes: {from, insert}})` produces an edit that Overleaf's collaborative sync layer picks up and saves server-side, not just a local CodeMirror repaint. This was the one genuinely uncertain measurement, and it passed — nothing in this design blocks an eventual "Accept → insert" flow.

## 4. Durability across remount

- Path A `hookFireCount` before file switch: `1`, after: `2` (exactly one new fire, matching the switch)
- Path A still resolved after switching files and back: [x] yes
- Path B still resolved after switching files and back: [x] yes — **with a caveat, see below**

**Caveat on the Path B result:** `probePathB()` re-walks the DOM fresh on every single call rather than caching a resolved reference. So this result shows that a *live re-query* finds the new editor instance after a remount — not that a *previously cached* Path B handle survives a remount. Those are different claims. A real call site that resolved Path B once and reused that object later (the realistic implementation pattern, and the actual risk this measurement was meant to probe) was never exercised here. Path A's durability is a stronger result: it's push-based — Overleaf's own event notifies us on every rebuild — so no polling or re-query logic is needed to stay current.

## Path decision

- [x] Build MVP on Path A (`UNSTABLE_editor:extensions`)
- [ ] Build MVP on Path B + `MutationObserver` re-acquire
- [ ] Blocked — writes don't persist

**Rationale:** Path A passed every measurement cleanly and needs no extra machinery to stay durable across remounts — it's notified, not polled. Path B works today but only because our probe re-walks the DOM every time; making a real Path B-based implementation equally durable would require bolting on a `MutationObserver` to detect remounts and re-acquire, which is strictly more code for a path that isn't the officially exposed integration point. Keep Path B in the codebase only as a comparison/fallback measurement, not as the primary access mechanism.

## Notes / anything surprising

- No surprises on completeness or writability — both came back cleanly on the first attempt.
- The Path B "durability" result needed a second look to interpret correctly; the instrumentation's own implementation choice (live re-query vs. cache) shaped the result more than an inherent property of the DOM-walk approach. Worth remembering when reading this file later: a clean pass here does not mean "cache a Path B handle and reuse it safely" — that specific claim remains unverified.

---

## 5. Bib file access (Part 1 of the MVP plan — separate spike, pending)

The measurements above only proved read/write on the *currently open*
document. The MVP needs to read a `.bib` file elsewhere in the project's file
tree, which is untested. Run **Probe file access** (see README §"Bib-access
probe") against a project containing at least one `.bib` file and record the
raw JSON result here.

**Date tested:**

**Raw `SCRIBE_PROBE_FILES` result:**
```
(paste JSON here)
```

- `ideObject` — any of `_ide`/`ide`/`__OL_IDE__` present? [ ] yes [ ] no — which: `___`
- `fileTreeDom` — `.bib` entries found in the DOM? [ ] yes [ ] no — count: `___`
- `socketOrRest` — `socket`/`io` present? [ ] yes [ ] no — project id parsed from URL: `___`

**Chosen approach:** [ ] ideObject [ ] file-tree DOM open-and-read [ ] socket/REST [ ] none worked — fall back to `%%%SCRIBE-BIB-START` comment block for the MVP

**Notes:**
