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

**Date tested:** 2026-09-04

**Raw `SCRIBE_PROBE_FILES` result:**
```json
{
  "ideObject": { "checked": ["_ide", "ide", "__OL_IDE__"], "found": {} },
  "fileTreeDom": {
    "matchCount": 6,
    "candidates": [
      { "text": "book_5refs.bib", "tag": "LI", "className": "" },
      { "text": "book_5refs.bib", "tag": "DIV", "className": "entity" },
      { "text": "book_5refs.bib", "tag": "DIV", "className": "entity-name entity-name-react" },
      { "text": "book_5refs.bib", "tag": "DIV", "className": "file-tree-entity-details" },
      { "text": "refs.bib", "tag": "DIV", "className": "item-name" },
      { "text": "refs.bib", "tag": "SPAN", "className": "" }
    ]
  },
  "socketOrRest": { "found": { "io": "object" }, "projectId": "6a9b426c13ee868708058ffa" }
}
```

- `ideObject` — any of `_ide`/`ide`/`__OL_IDE__` present? [ ] yes [x] no
- `fileTreeDom` — `.bib` entries found in the DOM? [x] yes [ ] no — count: `6` (2 clean leaf matches — `DIV.item-name` and a bare `SPAN`, both exact text `"refs.bib"`; the other 4 are folder-container elements whose `textContent` concatenates descendant filenames, e.g. `"book_5refs.bib"` — not real targets, an artifact of the query selector)
- `socketOrRest` — `socket`/`io` present? [x] yes (`window.io`) [ ] no — project id parsed from URL: `6a9b426c13ee868708058ffa`

**Chosen approach:** [ ] ideObject [x] file-tree DOM open-and-read [ ] socket/REST [ ] none worked

**Notes:**
`window._ide` and friends don't exist on this Overleaf version — that route is closed. `window.io` (Socket.IO) is present, which could support a REST/socket-based read later, but calling into an undocumented internal transport is more invasive than clicking a file open; kept as a fallback idea, not pursued. The file-tree DOM route is concretely viable: exact-text leaf matches exist and are trivially selectable. Implemented as `fetchFileByName()` in `content.js` — finds the leaf entry, records whatever's currently marked "selected" (best-effort; the actual "selected" class name is unconfirmed), clicks the target, polls `SCRIBE_PROBE`'s `pathA.hookFireCount` until it increments (proving Path A re-fired for the newly opened file, per the durability result in §4) rather than guessing a fixed delay, then reads via the existing `SCRIBE_READ`. Attempts to click back to the previously open file afterward.

**Live test result (2026-09-04, `refs.bib` fixture, `main.tex` open beforehand):**
```json
{
  "target": "refs.bib", "hookFired": true, "hookCountBefore": 1,
  "previouslySelected": "main.tex", "restored": true,
  "read": { "source": "A", "length": 3504, "lines": 102, "hash": "-508c73c8",
    "head": "@article{chen2019graphattn,...", "tail": "...pages = {33--41}\n}\n",
    "viewport": { "from": 1292, "to": 3504 } }
}
```
Full pass, including the previously-unverified restore step: `previouslySelected` correctly captured `"main.tex"` (the "selected"-class guess was right), the hook re-fired for the newly opened `refs.bib`, the read returned genuine BibTeX content matching the fixture's real size (3504 chars), and the editor visibly returned to `main.tex` afterward. **Bib discovery from the real file tree is confirmed working end to end — no fallback to a comment-block bib is needed for the MVP.**

---

## 6. Real data loss found via file-switch-and-restore (2026-09-12)

**This overturns part of §5's "confirmed working end to end."** §5 verified that `fetchFileByName()`'s restore step visually returns to the right file and reads back plausible content. It did **not** verify that the restored document is provably byte-identical to what it was before switching away — and in real end-to-end use (via the Draft/Check UI, not the raw debug button), it was not.

**Symptom:** after one bib-fetch (switch to `refs.bib`, read, switch back to `main.tex`) followed by two Accept insertions, `SCRIBE_READ` on the live document reported `length: 141153` — **11,213 characters smaller than the original fixture (152,366) alone**, despite two insertions that each *added* several KB. Net: roughly 19,000–20,000 characters of real filler content (~33 paragraphs) were missing from the document. Confirmed via `SCRIBE_READ` (bypasses DOM virtualization) after the user reloaded the page and re-checked — not a copy-paste artifact from pulling a long document into a chat box (which was the first, wrong hypothesis, since Overleaf's CM6 DOM is virtualized and a naive "select all" could plausibly under-copy; `SCRIBE_READ`'s `length`/`hash` ruled that out).

**Leading theory, not yet independently confirmed against Overleaf's own history/diff:** `fetchFileByName()` tears down and rebuilds the CodeMirror editor twice (switch to bib, switch back). The only things verified after restore were "does a working view resolve" and "does it read back *something* plausible" — never "is the restored buffer provably caught up with the server's true state before we act on it." If there's a window where a freshly remounted editor briefly holds a stale/partial local buffer, reading or writing during that window and letting Overleaf's real-time sync reconcile afterward could silently drop content that existed server-side but wasn't in our stale snapshot.

**Fix:** `fetchFileByName()` now snapshots the open document's `length`/`hash` *before* switching away, waits for Path A's hook to re-fire on restore (previously only waited for the hook on the *target* switch, not the restore switch — also fixed), then re-reads and compares. `fetchBibContent()` throws if they don't match, refusing to hand back bib content for Draft/Check to act on rather than silently proceeding. This detects the failure mode instead of preventing whatever causes it — the underlying mechanism is still a theory, not confirmed.

**Status: fix implemented, not yet live-tested.** Needs a fresh run: reload the extension and page, run Draft/Check (which will exercise the bib fetch once), and confirm either (a) the integrity check passes and no content goes missing, or (b) it correctly refuses and reports a mismatch instead of silently corrupting the document again. If it still corrupts silently, the snapshot-compare approach itself needs revisiting, not just this instance of it.

**Open, unresolved:** whether the theorized "stale buffer after remount" mechanism is actually correct — never confirmed against Overleaf's own version history/diff for the affected edit. If this recurs, pulling up that diff before writing more code would give a definitive mechanism instead of a best-guess one.

**Resolved (2026-09-16):** re-ran the fix against a freshly-pasted-in clean copy of `long_test.tex` (new baseline after fixture edits: `length: 154113`, `hash: "4cd6ca4e"`, 1002 lines as CodeMirror counts it). Full Draft → Check → Accept cycle with the service running:

- Pre-Accept `SCRIBE_READ` (after the bib-fetch-and-restore) matched the baseline exactly: `154113`/`4cd6ca4e`. No corruption from the fetch this time.
- Post-Accept `SCRIBE_READ`: `164112` chars, `+46` lines — consistent with a real ~10KB Related Work section added, no data lost.
- No `document changed while fetching` error fired, consistent with the clean pre-Accept read.
- Visual/source inspection confirmed the inserted `\section{Related Work}` landed cleanly on its own paragraph boundary (not mid-word/mid-sentence like the first broken run), with real `\citep{}`/`\citet{}` keys pulled from `refs.bib`, no duplication, both top/bottom markers intact.

The integrity-check fix works as intended on this run. The underlying "stale buffer after remount" mechanism is still an unconfirmed theory — this result doesn't prove *why* the original corruption happened, only that the detection/refusal path added in response to it now passes cleanly when nothing goes wrong, and (per the fix's own design) would throw rather than silently corrupt if it happened again. Continued live testing is the only way to build confidence this doesn't recur under different timing.
