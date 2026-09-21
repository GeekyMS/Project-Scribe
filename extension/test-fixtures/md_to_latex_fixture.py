#!/usr/bin/env python3
"""
Convert a Docling-extracted paper markdown into a compilable LaTeX test
fixture with its Related Work section removed.

These fixtures exist to test the Overleaf extension end to end: the
extension drafts a *new* Related Work section from the project's .bib, so
the fixture must be a real paper that is missing exactly that section.

Why a script and not a one-off heredoc: this conversion has been needed
twice (StudyChat, AnalystBench/ACL) and the throwaway version got it
wrong both times in identical ways (unescaped `$`, raw non-ASCII, every
heading flattened to \\section, markdown tables left as literal pipe
text, citations left as prose). One tested code path means fixing it
once fixes every fixture.

Usage:
    python3 extension/test-fixtures/md_to_latex_fixture.py \\
        --md   outputs/acl_2026_full_bib/paper_without_related.md \\
        --bib-file outputs/acl_2026_full_bib/references_full.bib \\
        --bib  references_full \\
        --strip-section "6 Related work" \\
        --out  outputs/acl_2026_full_bib/paper_without_related_test.tex
"""

import argparse
import re
import sys
from pathlib import Path

# ── Escaping ────────────────────────────────────────────────────────────
# Single pass: escaping in sequential passes would re-escape the braces
# and backslashes introduced by earlier replacements.

_SPECIALS = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}
_SPECIAL_RE = re.compile(r"[\\&%$#_{}~^]")

# Non-ASCII seen in these extractions. Anything NOT listed here raises,
# rather than silently passing through to become a compile error the user
# only discovers in Overleaf.
_UNICODE = {
    "§": r"\S{}",
    "→": r"$\rightarrow$",
    "←": r"$\leftarrow$",
    "↑": r"$\uparrow$",
    "↓": r"$\downarrow$",
    "∼": r"$\sim$",
    "~": r"$\sim$",
    "∗": "*",
    "✓": r"\checkmark{}",
    "✗": r"$\times$",
    "×": r"$\times$",
    "≈": r"$\approx$",
    "≤": r"$\leq$",
    "≥": r"$\geq$",
    "±": r"$\pm$",
    "–": "--",
    "—": "---",
    "‘": "`",
    "’": "'",
    "“": "``",
    "”": "''",
    "…": r"\ldots{}",
    "·": r"$\cdot$",
    "•": r"$\bullet$",
    "′": "'",
    "″": "''",
    "†": r"\dag{}",
    "‡": r"\ddag{}",
    "©": r"\copyright{}",
    "→": r"$\rightarrow$",
    "°": r"$^\circ$",
    "µ": r"$\mu$",
    "α": r"$\alpha$",
    "β": r"$\beta$",
    "γ": r"$\gamma$",
    "Δ": r"$\Delta$",
    "κ": r"$\kappa$",
    " ": " ",
    " ": " ",
    "​": "",
    "ﬁ": "fi",
    "ﬂ": "fl",
}

# OCR'd FontAwesome icon macros from the source PDF — stray literal text.
_ICON_RE = re.compile(r"/(?:cogs|image|tasks|envelope|lock|check|cross|star)\s*")


def escape(text: str) -> str:
    """Escape LaTeX specials, then map known non-ASCII. Order matters."""
    out = _SPECIAL_RE.sub(lambda m: _SPECIALS[m.group()], text)
    for ch, repl in _UNICODE.items():
        out = out.replace(ch, repl)
    leftover = sorted({c for c in out if ord(c) > 127})
    if leftover:
        raise ValueError(
            "unmapped non-ASCII character(s): "
            + ", ".join(f"{c!r} (U+{ord(c):04X})" for c in leftover)
            + " — add them to _UNICODE"
        )
    return out


def clean_icons(text: str) -> str:
    return _ICON_RE.sub("", text)


def normalize_spacing(text: str) -> str:
    """PDF extraction leaves a space before punctuation ("AnalystBench ,
    a benchmark" / "AnalystBench : Benchmarking"). Harmless to fix
    globally — no English construction wants a space there."""
    text = re.sub(r"\s+([:;,.!?])", r"\1", text)
    text = re.sub(r"\(\s+", "(", text)
    text = re.sub(r"\s+\)", ")", text)
    return text


# A name followed by an optional affiliation marker, as the extractor
# flattens it: "Chau Minh Pham 1 Zichao Wang 2 Puneet Mathur 2 ..."
_AUTHOR_RE = re.compile(r"([A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+)*)\s*(\d+)?")


def build_author_block(authors_raw: str, affiliations: dict, emails=(),
                       per_line: int = 4) -> str:
    """Rebuild a real LaTeX author block from the flattened extraction.

    Without this the superscript affiliation markers render as literal
    digits between names ("Chau Minh Pham 1 Zichao Wang 2"), which is the
    single most obviously-wrong thing on the title page.
    """
    authors = []
    for m in _AUTHOR_RE.finditer(authors_raw):
        name = m.group(1).strip()
        if not name or len(name) < 3:
            continue
        marker = m.group(2)
        authors.append((name, marker))
    if not authors:
        return escape(authors_raw)

    rendered = []
    for name, marker in authors:
        sup = f"$^{{{marker}}}$" if marker else ""
        rendered.append(escape(name) + sup)

    lines = [r" \quad ".join(rendered[i:i + per_line])
             for i in range(0, len(rendered), per_line)]
    block = " \\\\\n".join(lines)

    if affiliations:
        aff = r" \quad ".join(
            f"$^{{{k}}}$" + escape(v) for k, v in sorted(affiliations.items())
        )
        block += " \\\\[0.4em]\n" + aff
    if emails:
        block += " \\\\\n" + r" \quad ".join(r"\texttt{" + escape(e) + "}" for e in emails)
    return block


# ── Citations: author-year prose → \citep{} ─────────────────────────────

def load_bib_keys(bib_path: Path) -> set:
    return set(re.findall(r"^@\w+\{([^,]+),", bib_path.read_text(encoding="utf-8"), re.M))


def _surname_key(surname: str, year: str) -> str:
    s = surname.lower()
    s = re.sub(r"[^a-z]", "", s)  # Dell'Acqua -> dellacqua, de Chezelles -> dechezelles
    return f"{s}{year}"


# Nobiliary particles that are part of the surname and therefore part of
# the BibTeX key ("de Chezelles" -> dechezelles2025). Deliberately does NOT
# include "and": treating "Reimers and Gurevych" as one surname produced
# "reimersandgurevych2019" and silently failed to resolve.
_PARTICLE = (r"(?:de|van|von|der|den|del|della|di|da|do|dos|la|le|du|el|"
             r"bin|ibn|mac|mc|st)")

# One author-year mention inside a citation group:
#   "Brynjolfsson et al., 2023" | "Autor, 2015" | "Huang et al., 2025a,b"
#   "de Chezelles et al., 2025"  -> particle kept
#   "Reimers and Gurevych, 2019" -> first surname wins (matches BibTeX keys)
_MENTION_RE = re.compile(
    r"\b((?:" + _PARTICLE + r"\s+)?[A-Z][A-Za-z'’\-]+)"
    r"(?:\s+et\s+al\.?)?"
    r"(?:\s+and\s+[A-Z][A-Za-z'’\-]+)?"
    r",?\s+(\d{4})([a-z](?:\s*,\s*[a-z])*)?"
)


def _resolve_group(group_text: str, bib_keys: set, aliases: dict):
    """Return (keys, ok). ok=False if any mention fails to resolve."""
    keys, ok = [], True
    for part in group_text.split(";"):
        m = _MENTION_RE.search(part)
        if not m:
            ok = False
            continue
        surname, year = m.group(1), m.group(2)
        suffixes = m.group(3)

        # Explicit alias first — corporate authors ("TerminalBench (Team,
        # 2025)" -> terminalbenchteam2025) can't be derived from the
        # surname alone, so they're declared rather than guessed.
        alias = aliases.get((re.sub(r"[^a-z]", "", surname.lower()), year))
        if alias:
            keys.append(alias)
            continue

        candidates = []
        if suffixes:
            for suf in re.findall(r"[a-z]", suffixes):
                candidates.append(_surname_key(surname, year) + suf)
        else:
            candidates.append(_surname_key(surname, year))
        for cand in candidates:
            if cand in bib_keys:
                keys.append(cand)
            elif cand.rstrip("abcdefgh") in bib_keys:
                keys.append(cand.rstrip("abcdefgh"))
            else:
                ok = False
    return keys, ok


# Citation groups appear as (Author, YYYY) or [Author, YYYY] depending on
# the source paper's bibstyle — StudyChat uses square brackets, the ACL
# paper uses parens. Both become \citep{}.
_GROUP_RE = re.compile(
    r"\(([^()\[\]]*?\b\d{4}[a-z]?\b[^()\[\]]*?)\)"
    r"|\[([^()\[\]]*?\b\d{4}[a-z]?\b[^()\[\]]*?)\]"
)
_NARRATIVE_RE = re.compile(
    r"\b([A-Z][A-Za-z'’\-]+)(\s+et\s+al\.?)\s+[\(\[](\d{4}[a-z]?)[\)\]]"
)


def convert_citations(text: str, bib_keys: set, stats: dict, aliases: dict = None) -> str:
    """Rewrite author-year prose as \\citep/\\citet. Never invents a key:
    anything that doesn't resolve against the real .bib is left as prose
    and counted, since a fabricated key is exactly the failure mode the
    tool under test exists to detect."""
    aliases = aliases or {}

    def repl_group(m):
        inner = m.group(1) if m.group(1) is not None else m.group(2)
        # A bare "(2024)" or "[2018]" is a year reference, a table cell, or
        # an enumeration — not a citation. Require a capitalised surname
        # before treating the group as one, otherwise these get counted as
        # failed citations and pollute the unresolved report.
        if not re.search(r"[A-Z][A-Za-z'’\-]", inner):
            return m.group(0)
        keys, ok = _resolve_group(inner, bib_keys, aliases)
        if ok and keys:
            stats["converted"] += len(keys)
            return r"\citep{" + ",".join(keys) + "}"
        stats["left_as_prose"] += 1
        stats["unresolved"].add(inner.strip()[:60])
        return m.group(0)

    def repl_narrative(m):
        key = _surname_key(m.group(1), m.group(3))
        if key in bib_keys:
            stats["converted"] += 1
            return r"\citet{" + key + "}"
        if key.rstrip("abcdefgh") in bib_keys:
            stats["converted"] += 1
            return r"\citet{" + key.rstrip("abcdefgh") + "}"
        stats["left_as_prose"] += 1
        stats["unresolved"].add(m.group(0)[:60])
        return m.group(0)

    text = _NARRATIVE_RE.sub(repl_narrative, text)
    text = _GROUP_RE.sub(repl_group, text)
    return text


# ── Tables: markdown pipe tables → real tabular ─────────────────────────

def _split_row(line: str):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_separator(line: str) -> bool:
    return bool(re.fullmatch(r"\|[\s:|-]+\|", line.strip()))


def build_table(block, caption, label, total_width_cm=15.5):
    """Convert a markdown pipe-table block into a LaTeX table float."""
    rows = [_split_row(r) for r in block if not _is_separator(r)]
    rows = [r for r in rows if any(c for c in r)]  # drop all-empty rows
    if not rows:
        return ""

    ncols = max(len(r) for r in rows)
    rows = [r + [""] * (ncols - len(r)) for r in rows]

    # Rows whose cells all repeat the same text were \multicolumn spanning
    # headers in the original paper; the extractor duplicated them across
    # every cell.
    def is_span(r):
        vals = [c for c in r if c]
        return len(vals) == ncols and len(set(vals)) == 1

    # Column widths from content, for deciding l/r vs p{}
    maxlen = [max(len(r[i]) for r in rows if not is_span(r)) if any(not is_span(r) for r in rows) else 0
              for i in range(ncols)]
    numericish = [
        all(re.fullmatch(r"[\d.,%$/+\-\s]*", r[i] or "") for r in rows[1:] if not is_span(r))
        for i in range(ncols)
    ]

    WIDE = 22
    wide_idx = [i for i in range(ncols) if maxlen[i] > WIDE]
    narrow_w = sum(min(maxlen[i], WIDE) for i in range(ncols) if i not in wide_idx)
    # crude char->cm at \small; leaves room for inter-column padding
    budget = max(total_width_cm - narrow_w * 0.16 - ncols * 0.25, 2.0)
    wide_total = sum(maxlen[i] for i in wide_idx) or 1

    spec = []
    for i in range(ncols):
        if i in wide_idx:
            w = budget * maxlen[i] / wide_total
            spec.append(f"p{{{max(w, 1.2):.2f}cm}}")
        elif numericish[i]:
            spec.append("r")
        else:
            spec.append("l")

    out = ["\\begin{table}[t]", "\\centering", "\\small",
           "\\begin{tabular}{" + "".join(spec) + "}", "\\toprule"]
    for idx, r in enumerate(rows):
        cells = [clean_icons(c) for c in r]
        if is_span(r):
            out.append(f"\\multicolumn{{{ncols}}}{{l}}{{{escape(cells[0])}}} \\\\")
            out.append("\\midrule")
            continue
        out.append(" & ".join(escape(c) for c in cells) + " \\\\")
        if idx == 0:
            out.append("\\midrule")
    out.append("\\bottomrule")
    out.append("\\end{tabular}")
    if caption:
        out.append("\\caption{" + escape(clean_icons(caption)) + "}")
    out.append(f"\\label{{tab:{label}}}")
    out.append("\\end{table}")
    return "\n".join(out)


# ── Main conversion ─────────────────────────────────────────────────────

_CAPTION_RE = re.compile(r"^(Table|Figure)\s+\d+\s*[:.]")


def convert(md: str, bib_keys: set, bib_name: str, strip_section: str,
            title_override=None, authors_override=None, drop_headings=(),
            aliases=None, drop_lines=(), affiliations=None, emails=(),
            author_latex=None):
    md = re.sub(r"---\s*PAGE\s+\d+\s*---\n?", "", md)

    # Remove the Related Work section — the whole point of the fixture.
    # Must run to the next *top-level* section: stopping at the next
    # heading of any level left the "6.1"/"6.2" subsections behind, so
    # most of Related Work survived while the strip still reported success.
    if strip_section:
        esc = re.escape(strip_section)
        top_m = re.match(r"^(\d+)", strip_section.strip())
        if top_m:
            top = top_m.group(1)
            nxt = rf"(?=^##\s+(?!{top}[\s.])\d|\Z)"
        else:
            nxt = r"(?=^##\s+\S|\Z)"
        md, n = re.subn(rf"^##\s+{esc}.*?{nxt}", "", md,
                        flags=re.DOTALL | re.MULTILINE)
        if n == 0:
            raise ValueError(f"--strip-section {strip_section!r} matched nothing")

    # Drop the plain-text reference list / appendices (redundant with .bib)
    md = re.sub(r"^##\s+References\b.*$", "", md, flags=re.DOTALL | re.MULTILINE)

    lines = md.split("\n")

    # --- front matter -------------------------------------------------
    title, authors, abstract = title_override, authors_override, []
    body_start, i = 0, 0
    while i < len(lines):
        ln = lines[i].strip()
        m = re.match(r"^##\s+(.*)$", ln)
        if m and title is None:
            title = m.group(1).strip()
            i += 1
            continue
        if m and re.fullmatch(r"abstract", m.group(1).strip(), re.I):
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("## "):
                if lines[i].strip():
                    abstract.append(lines[i].strip())
                i += 1
            body_start = i
            break
        if m and re.match(r"^\d+(\.\d+)*\s", m.group(1).strip()):
            body_start = i  # no abstract heading; body starts here
            break
        if ln and not m and authors is None and title is not None:
            authors = ln
        i += 1
    else:
        body_start = i

    stats = {"converted": 0, "left_as_prose": 0, "unresolved": set()}
    aliases = aliases or {}
    affiliations = affiliations or {}
    # --author-latex wins outright; otherwise rebuild the flattened
    # "Name 1 Name 2" extraction into a real superscripted block.
    if author_latex:
        author_block = author_latex
    else:
        author_block = build_author_block(
            normalize_spacing(clean_icons(authors or "Anonymous")),
            affiliations, emails)

    # --- body ---------------------------------------------------------
    out, i, tbl_n = [], body_start, 0
    while i < len(lines):
        raw = lines[i]
        ln = raw.strip()

        if ln.startswith("|"):
            block = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                block.append(lines[i])
                i += 1
            caption = ""
            j = i
            while j < len(lines) and j < i + 3:
                if _CAPTION_RE.match(lines[j].strip()):
                    caption = lines[j].strip()
                    i = j + 1
                    break
                if lines[j].strip():
                    break
                j += 1
            tbl_n += 1
            out.append(build_table(block, caption, f"fixture{tbl_n}"))
            out.append("")
            continue

        m = re.match(r"^(#{2,4})\s+(.*)$", ln)
        if m:
            heading = m.group(2).strip()
            if any(d.lower() == heading.lower() for d in drop_headings):
                i += 1
                continue
            num = re.match(r"^(\d+(?:\.\d+)*)\s+(.*)$", heading)
            if num:
                depth = num.group(1).count(".")
                cmd = ["section", "subsection", "subsubsection"][min(depth, 2)]
                out.append(f"\\{cmd}{{{escape(normalize_spacing(clean_icons(num.group(2))))}}}")
            else:
                out.append(f"\\section*{{{escape(normalize_spacing(clean_icons(heading)))}}}")
            out.append("")
            i += 1
            continue

        if not ln:
            out.append("")
            i += 1
            continue

        # Two-column PDF extraction drops affiliation/email fragments
        # into the middle of body paragraphs; they are not content.
        # Compare against the icon-stripped form: the source carries these
        # as "/envelope vmanjuna@adobe.com", so matching the raw line would
        # silently miss them.
        if any(d.strip().lower() == clean_icons(ln).strip().lower() for d in drop_lines):
            i += 1
            continue

        if _CAPTION_RE.match(ln):  # orphan caption with no table/figure block
            out.append(r"\begin{quote}\small\textit{" +
                       escape(clean_icons(ln)) + r"}\end{quote}")
            out.append("")
            i += 1
            continue

        para = escape(normalize_spacing(clean_icons(ln)))
        para = convert_citations(para, bib_keys, stats, aliases)
        out.append(para)
        out.append("")
        i += 1

    body = "\n".join(out)
    body = re.sub(r"\n{3,}", "\n\n", body)

    # --- assemble -----------------------------------------------------
    preamble = "\n".join([
        r"\documentclass[11pt]{article}",
        r"\usepackage[margin=1in]{geometry}",
        r"\usepackage[T1]{fontenc}",
        r"\usepackage{amssymb}",
        r"\usepackage{booktabs}",
        r"\usepackage{graphicx}",
        r"\usepackage{natbib}",
        r"\usepackage{hyperref}",
        "",
        r"\title{" + escape(normalize_spacing(clean_icons(title or "Untitled"))) + "}",
        r"\author{" + author_block + "}",
        r"\date{}",
        "",
        r"\begin{document}",
        r"\maketitle",
    ])

    abs_block = ""
    if abstract:
        abs_text = convert_citations(
            escape(normalize_spacing(clean_icons(" ".join(abstract)))),
            bib_keys, stats, aliases)
        abs_block = "\n".join([r"\begin{abstract}", abs_text, r"\end{abstract}", ""])

    tail = "\n".join(["", r"\bibliographystyle{plainnat}",
                      r"\bibliography{" + bib_name + "}", "", r"\end{document}", ""])

    return preamble + "\n\n" + abs_block + "\n" + body + tail, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--md", required=True)
    ap.add_argument("--bib-file", required=True, help="path to the .bib, for key validation")
    ap.add_argument("--bib", required=True, help="name used in \\bibliography{}")
    ap.add_argument("--out", required=True)
    ap.add_argument("--strip-section", required=True,
                    help='heading text of the Related Work section, e.g. "6 Related work"')
    ap.add_argument("--title")
    ap.add_argument("--authors")
    ap.add_argument("--drop-heading", action="append", default=[])
    ap.add_argument("--affiliation", action="append", default=[],
                    help='map a superscript marker, e.g. "1=University of Maryland"')
    ap.add_argument("--email", action="append", default=[],
                    help="corresponding-author email to show under the authors")
    ap.add_argument("--author-latex",
                    help="verbatim LaTeX for the author block (overrides all auto-parsing)")
    ap.add_argument("--drop-line", action="append", default=[],
                    help="remove a body line verbatim (PDF column artifact)")
    ap.add_argument("--cite-alias", action="append", default=[],
                    help='declare an underivable key, e.g. "Team:2025=terminalbenchteam2025"')
    args = ap.parse_args()

    bib_keys = load_bib_keys(Path(args.bib_file))
    md = Path(args.md).read_text(encoding="utf-8")
    aliases = {}
    for spec in args.cite_alias:
        lhs, _, key = spec.partition("=")
        surname, _, year = lhs.partition(":")
        if not (surname and year and key):
            raise SystemExit(f"bad --cite-alias {spec!r}, want Surname:YYYY=bibkey")
        if key not in bib_keys:
            raise SystemExit(f"--cite-alias target {key!r} is not in the .bib")
        aliases[(re.sub(r'[^a-z]', '', surname.lower()), year)] = key

    affiliations = {}
    for spec in args.affiliation:
        marker, _, name = spec.partition("=")
        if not (marker.strip() and name.strip()):
            raise SystemExit(f"bad --affiliation {spec!r}, want N=Institution")
        affiliations[marker.strip()] = name.strip()

    tex, stats = convert(md, bib_keys, args.bib, args.strip_section,
                         args.title, args.authors, args.drop_heading, aliases,
                         args.drop_line, affiliations, args.email,
                         args.author_latex)
    Path(args.out).write_text(tex, encoding="utf-8")

    print(f"wrote {args.out}  ({len(tex)} chars)")
    print(f"  bib keys available : {len(bib_keys)}")
    print(f"  citations converted: {stats['converted']}")
    print(f"  left as prose      : {stats['left_as_prose']}")
    if stats["unresolved"]:
        for u in sorted(stats["unresolved"]):
            print(f"      unresolved: {u}")


if __name__ == "__main__":
    sys.exit(main())
