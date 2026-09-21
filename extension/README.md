# Scribe Overleaf Risk Spike

## Status (2026-09-17): JS-in-extension pipeline port

The Draft/Check pipeline (bib parsing, Semantic Scholar/Crossref/OpenAlex
resolution, citation-span/Tier-1 checking, Tier-4 evidence-support judging,
generation) has been ported from the Python service into the extension
itself — `extension/lib/citationSpans.js`, `resolvers.js`, `bibEnrichment.js`,
`enrichmentCache.js` (IndexedDB), `genPrompts.js`, `evaluator.js`,
`providers.js`/`llmProviders.js` (provider-agnostic LLM adapter —
OpenRouter/OpenAI/Anthropic presets + a "Custom" OpenAI-shaped base URL),
and `generate.js`, all loaded into `background.js` via `importScripts`.
No local Python server is required for the default flow any more; the user
configures their own provider + API key + model via the gear-icon settings
panel in the extension UI.

**Two deterministic pieces were verified against the live Python pipeline
this session** (see `~/.claude/plans/zazzy-jumping-swan.md`'s verification
section): `parseBibContent` + `findCitationSpans` + `evaluateCitations`
produced an exact field-by-field match against `pipeline.evaluator
.evaluate_citations()` on `test-fixtures/long_test.tex` + `refs.bib`
(recall/precision 0.9167, `hallucinated_keys: ["wong2024fake"]`,
`missing_keys: ["fournier2015spans"]`); and the from-scratch port of
`difflib.SequenceMatcher.ratio()` in `resolvers.js` matched Python's output
exactly across 5 test string pairs.

**What has NOT been live-tested yet** (no browser/Overleaf session was
available to this change): the network-dependent pieces — `resolvers.js`'s
actual HTTP calls to Semantic Scholar/Crossref/OpenAlex, the LLM provider
adapter's real calls to OpenRouter/OpenAI/Anthropic, the full
Draft/Check/Accept cycle through `background.js`'s in-process job store, the
settings UI's save/test-key/provider-switch flows, `chrome.storage.session`
surviving a forced service-worker restart, and the bridge nonce actually
rejecting a forged `scribe:request`. Needs a fresh `chrome://extensions`
reload and a real run before any of this is treated as confirmed — same
discipline as every other finding in this file.

`service/` (the Python service) still exists and still works — kept
deliberately as an optional dev/debug and cross-validation tool (e.g.
comparing this JS pipeline's output against the Python service's on the
same input), not because the extension depends on it any more.

---

Not a product. This measures whether a durable handle on Overleaf's live
CodeMirror 6 `EditorView` is reachable, complete (full document regardless of
scroll position), and writable (edits persist through Overleaf's sync), and
which of two access paths is durable across editor remounts. See the plan at
`~/.claude/plans/ill-get-the-keys-wild-lightning.md` for the full rationale.

No LLM calls, no bibliography parsing, no network access. Three plain,
unbundled files — no build step.

## Load it

1. `chrome://extensions`
2. Enable **Developer mode** (top right)
3. **Load unpacked** → select this `extension/` directory
4. Open a real Overleaf project at `https://www.overleaf.com/project/...`.
   Use (or create) a `.tex` file that's **at least ~800 lines long** — a
   short file would make the "full doc vs. viewport" measurement pass
   vacuously, since a short doc has no un-rendered region to miss.

A small dark panel should appear pinned to the bottom-right of the page with
five buttons: **Probe**, **Read doc**, **Insert test text**, **Ping
background**, **Probe file access**.

## Manual test procedure

1. Click **Probe**. Confirms both access paths resolve (or don't) and reports
   whether they point at the same object.
   - Path A: `UNSTABLE_editor:extensions`, Overleaf's own documented (if
     unsupported) hook for registering CodeMirror extensions.
   - Path B: walking the DOM for a node carrying `.cmView.view`.
2. Scroll to the very top of the file. Click **Read doc**. Note `length` and
   `viewport.from/to`.
3. Scroll to the very bottom. Click **Read doc** again.
   **Expected: `length` and `hash` identical to step 2, `viewport` different.**
   This is the direct check of whether `view.state.doc` holds the whole
   document independent of what CM has rendered into the DOM.
4. Place the cursor mid-document (click into the editor). Click **Insert test
   text**. Confirm `%SCRIBE-SPIKE-TEST-INSERT%` appears in the editor, then
   **reload the page** and confirm it's still there. If it vanished, check
   the project's history/track-changes panel — that distinguishes "the edit
   never reached Overleaf's sync" from "sync saw it and rejected it."
5. Switch to a different file in the project's file tree, then switch back.
   Click **Probe** again. Expected: Path A's `hookFireCount` incremented and
   it still resolves; Path B may now be stale (pointing at a
   detached/replaced CM instance) or may fail to resolve at all.
6. Click **Ping background** — confirms the full inject → content →
   background round trip works, independent of the CodeMirror measurements.
7. Copy the four results into `FINDINGS.md` in this directory (template
   already there) and commit.

## Bib-access probe (Part 1 of the MVP plan)

The measurements above only prove we can read/write the *currently open*
document. The MVP needs to find and read a `.bib` file elsewhere in the
project's file tree — that's a separate, untested capability.

Make sure the Overleaf project has at least one `.bib` file in its file tree
(create one if needed — content doesn't matter for this probe), then:

1. Click **Probe file access**. It checks three things and reports all of
   them, without calling anything that could have a side effect:
   - **`ideObject`** — whether `window._ide` / `window.ide` / a couple of
     other plausible global names exist and, if so, what top-level keys they
     expose. This is the cleanest route if it's there: Overleaf's own app
     object, no DOM scraping.
   - **`fileTreeDom`** — file-tree DOM elements whose text ends in `.bib`,
     with tag/class info. Tells us whether a "click it open, then read it
     with the existing SCRIBE_READ path" fallback is even findable.
   - **`socketOrRest`** — whether `window.socket`/`window.io` (Overleaf's
     real-time collab transport) are present, plus the project id parsed out
     of the URL, which any REST-based approach would need.
2. Record the full JSON result in `FINDINGS.md` under a new "Bib access"
   section (template pending — copy the shape of the sections above).
3. If `ideObject` looks promising, the next step (not yet built) is to
   inspect its keys by hand in the DevTools console (`window._ide`, etc.) to
   find the doc-loading method — this probe only detects presence, it
   doesn't call in.

**Result on the tested project:** `ideObject` was empty, `fileTreeDom` found
clean leaf matches for `refs.bib`. Chosen approach: file-tree DOM
open-and-read. See `FINDINGS.md` §5.

## Fetch another file by name

With that chosen, the panel now has an input (defaults to `refs.bib`) and a
**Fetch file by name** button that:

1. Records whichever file-tree entry looks currently "selected" (best-effort).
2. Finds the file-tree leaf entry with that exact filename and clicks it.
3. Polls `SCRIBE_PROBE`'s `pathA.hookFireCount` until it increments — i.e.
   waits for confirmation that Overleaf actually rebuilt the editor for the
   new file, rather than guessing a fixed delay.
4. Reads the new document via the existing `SCRIBE_READ` path.
5. Attempts to click back to whatever was open before.

To test: with `refs.bib` present in the project (see
`extension/test-fixtures/refs.bib`), have some *other* `.tex` file open,
click **Fetch file by name**, and check:
- `read.head`/`read.tail` in the result show BibTeX content, not your `.tex`.
- The editor visibly switches back to the original file afterward
  (`restored: true` isn't proof by itself — confirm visually, since the
  "selected" class used to find the original file is an unconfirmed guess).

## Why `world: "MAIN"` instead of an injected `<script src=...>` tag

Overleaf serves a strict Content-Security-Policy. A CSP applies to `<script>`
elements a content script appends to the page just as it would to the page's
own scripts — so the classic "inject a script tag pointing at
`chrome.runtime.getURL(...)`" trick can be blocked. Manifest V3's
`"world": "MAIN"` key runs the content script directly in the page's JS
context without going through a script tag at all, sidestepping that CSP
surface. If it ever misbehaves on a real Overleaf CSP, the fallback is the
script-tag injection from `content.js`, gated behind `web_accessible_resources`.

## Why `run_at: "document_start"`

Path A only works if we're listening for `UNSTABLE_editor:extensions` before
Overleaf constructs its editor and fires it. Registering the listener any
later (e.g. the default `document_idle`) risks missing the event entirely and
silently falling back to Path B without anyone noticing.
