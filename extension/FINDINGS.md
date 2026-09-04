# Findings — Overleaf Risk Spike

Fill this in after running the manual procedure in `README.md` against a
real Overleaf project. Do not fill in expected/assumed values — this file's
only job is to record what actually happened.

**Date tested:**
**Overleaf project used (length of test file):**

## 1. Reachability

- Path A (`UNSTABLE_editor:extensions`) resolved: [ ] yes [ ] no
- Path B (`.cmView.view` walk) resolved: [ ] yes [ ] no — winning candidate path: `___`
- Did A and B resolve to the same object? [ ] yes [ ] no

## 2. Completeness

- `doc.length` at top of file: `___`, `viewport`: `___`
- `doc.length` at bottom of file: `___`, `viewport`: `___`
- Hash identical between the two reads? [ ] yes [ ] no
- Conclusion: [ ] full-document access confirmed [ ] viewport-truncated (unexpected — investigate)

## 3. Writability

- Test text appeared in the editor immediately: [ ] yes [ ] no
- Survived a full page reload: [ ] yes [ ] no
- If not, present in project history/track-changes: [ ] yes [ ] no [ ] n/a
- Conclusion: [ ] edits persist to Overleaf's sync layer [ ] edits are local-only (blocker — see plan's decision gate)

## 4. Durability across remount

- Path A `hookFireCount` before file switch: `___`, after: `___`
- Path A still resolved after switching files and back: [ ] yes [ ] no
- Path B still resolved after switching files and back: [ ] yes [ ] no

## Path decision

- [ ] Build MVP on Path A (`UNSTABLE_editor:extensions`)
- [ ] Build MVP on Path B + `MutationObserver` re-acquire
- [ ] Blocked — writes don't persist; escalate per the plan's decision gate

## Notes / anything surprising
