# Scribe Overleaf Risk Spike

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
four buttons: **Probe**, **Read doc**, **Insert test text**, **Ping
background**.

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
