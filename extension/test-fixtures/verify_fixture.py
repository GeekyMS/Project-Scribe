#!/usr/bin/env python3
"""
Static checks on a generated LaTeX fixture.

No LaTeX compiler is installed on this machine, so this cannot prove the
file compiles — it catches the classes of breakage the throwaway
converter actually shipped (unescaped specials, raw non-ASCII, flattened
section hierarchy, malformed tabulars, invented citation keys). The real
compile check is uploading to Overleaf.

Usage: verify_fixture.py FIXTURE.tex REFERENCES.bib
"""

import re
import sys
from collections import Counter
from pathlib import Path


def _brace_group(s: str, open_idx: int) -> str:
    """Return the contents of the brace group starting at s[open_idx]=='{',
    honouring nesting — a naive [^}]* stops inside p{5.75cm} and
    miscounts every column."""
    assert s[open_idx] == "{"
    depth, i = 0, open_idx
    while i < len(s):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                return s[open_idx + 1:i]
        i += 1
    raise ValueError("unbalanced brace group")


def _tabulars(tex: str):
    """Yield (colspec, body) for each tabular, brace-aware."""
    for m in re.finditer(r"\\begin\{tabular\}", tex):
        brace = tex.index("{", m.end())
        spec = _brace_group(tex, brace)
        end = tex.index(r"\end{tabular}", brace)
        yield spec, tex[brace + len(spec) + 2:end]


def _spec_cols(spec: str) -> int:
    return len(re.findall(r"[plrc]\{[^{}]*\}|[lrc]|\|", spec.replace("|", "")))


def check(tex_path: str, bib_path: str) -> bool:
    tex = Path(tex_path).read_text(encoding="utf-8")
    bib_keys = set(re.findall(r"^@\w+\{([^,]+),", Path(bib_path).read_text(encoding="utf-8"), re.M))
    fails = []

    # 1 — non-ASCII
    bad = sorted({c for c in tex if ord(c) > 127})
    if bad:
        fails.append(f"non-ASCII present: {[f'{c} U+{ord(c):04X}' for c in bad[:8]]}")

    # 2 — unescaped specials. Strip tabulars FIRST (& is the column
    # separator there and legitimately unescaped), then math, then
    # commands; doing it in the other order leaves \begin as a bare
    # {tabular} that the tabular-strip can no longer match.
    body = tex
    body = re.sub(r"\\begin\{tabular\}.*?\\end\{tabular\}", "", body, flags=re.S)
    body = re.sub(r"\$[^$]*\$", "", body)
    # Filename arguments are passed through to BibTeX/graphics, never
    # typeset, so an underscore in them is legal and must not be flagged.
    body = re.sub(r"\\(?:bibliography|bibliographystyle|include|input|includegraphics)"
                  r"(?:\[[^\]]*\])?\{[^}]*\}", "", body)
    body = re.sub(r"\\[a-zA-Z]+\*?", "", body)
    body = re.sub(r"(?m)(?<!\\)%.*$", "", body)
    for ch in ["&", "#", "_", "%", "$"]:
        hits = re.findall(r"(?<!\\)" + re.escape(ch), body)
        if hits:
            ctx = re.search(r".{0,40}(?<!\\)" + re.escape(ch) + r".{0,40}", body)
            fails.append(f"unescaped {ch!r} x{len(hits)} — e.g. ...{ctx.group(0).strip()!r}...")

    # 3 — balanced environments
    begins = Counter(re.findall(r"\\begin\{(\w+\*?)\}", tex))
    ends = Counter(re.findall(r"\\end\{(\w+\*?)\}", tex))
    if begins != ends:
        fails.append(f"unbalanced environments: {begins - ends or ends - begins}")

    # 4 — tabular column counts
    ntab = 0
    for ti, (spec, tbody) in enumerate(_tabulars(tex), 1):
        ntab += 1
        ncol = _spec_cols(spec)
        for line in tbody.split(r"\\"):
            line = re.sub(r"\\(top|mid|bottom)rule", "", line).strip()
            if not line or r"\multicolumn" in line:
                continue
            # Only unescaped & separates columns; "M\&A" is one cell.
            cells = len(re.findall(r"(?<!\\)&", line)) + 1
            if cells != ncol:
                fails.append(f"table {ti}: row has {cells} cells, colspec {spec!r} declares {ncol}")
                break

    # 5 — citation keys must exist in the bib (a fabricated key is the
    # exact failure mode the tool under test detects)
    cited = set()
    for m in re.finditer(r"\\cite[pt]?\{([^}]*)\}", tex):
        cited |= {k.strip() for k in m.group(1).split(",") if k.strip()}
    bogus = cited - bib_keys
    if bogus:
        fails.append(f"citation keys not in bib: {sorted(bogus)}")

    # 6 — structure
    if re.search(r"\\(sub)?section\*?\{[^}]*Related\s+[Ww]ork", tex):
        fails.append("a Related Work section survived the strip")
    nsec = len(re.findall(r"\\section\{", tex))
    nsub = len(re.findall(r"\\subsection\{", tex))
    if nsec == 0 or nsub == 0:
        fails.append(f"hierarchy flat: {nsec} \\section, {nsub} \\subsection")
    for req in [r"\maketitle", r"\begin{abstract}", r"\bibliography{"]:
        if req not in tex:
            fails.append(f"missing {req}")

    print(f"--- {tex_path}")
    print(f"    {nsec} sections | {nsub} subsections | {ntab} tabulars | "
          f"{len(cited)} distinct cites (all resolve: {not bogus})")
    for f in fails:
        print(f"    FAIL: {f}")
    if not fails:
        print("    ALL STATIC CHECKS PASS (compile still unverified — no local LaTeX)")
    return not fails


if __name__ == "__main__":
    sys.exit(0 if check(sys.argv[1], sys.argv[2]) else 1)
