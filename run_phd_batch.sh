#!/bin/bash
# Full pipeline batch run for research_test_with_phd papers.
# For each PDF: creates a dedicated folder, moves the PDF, runs Phase 1 (extraction)
# and Phase 2 (generation + evaluation).
#
# Usage: source my_venv/bin/activate && bash run_phd_batch.sh

BASE_DIR="research_test_with_phd"

# Collect all PDFs currently in the base dir (not in subdirs)
PDFS=()
while IFS= read -r -d '' f; do
    PDFS+=("$f")
done < <(find "${BASE_DIR}" -maxdepth 1 -name "*.pdf" -print0)

if [ ${#PDFS[@]} -eq 0 ]; then
    echo "❌ No PDF files found in ${BASE_DIR}/"
    exit 1
fi

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Found ${#PDFS[@]} paper(s) to process"
echo "╚══════════════════════════════════════════════════════════════╝"
for f in "${PDFS[@]}"; do
    echo "   • $(basename "$f")"
done
echo ""

FAILED=()
SUCCESS=()

for PDF in "${PDFS[@]}"; do
    BASENAME=$(basename "$PDF")
    STEM="${BASENAME%.pdf}"
    PAPER_DIR="${BASE_DIR}/${STEM}"
    OUT_DIR="${PAPER_DIR}/results"

    echo ""
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║  Processing: ${BASENAME}"
    echo "╚══════════════════════════════════════════════════════════════╝"

    # Step 0: Create folder and move PDF
    mkdir -p "${PAPER_DIR}"
    if [ ! -f "${PAPER_DIR}/${BASENAME}" ]; then
        echo "   📁 Moving PDF → ${PAPER_DIR}/"
        mv "${PDF}" "${PAPER_DIR}/${BASENAME}"
    else
        echo "   ⏭️  PDF already in ${PAPER_DIR}/, skipping move."
    fi

    MOVED_PDF="${PAPER_DIR}/${BASENAME}"

    # Phase 1: Extract related work
    if [ -f "${OUT_DIR}/related_work.txt" ]; then
        echo "   ⏭️  Phase 1 outputs already exist, skipping extraction."
    else
        echo "   📄 Phase 1: Extracting (docling)..."
        python process_paper_v4.py "${MOVED_PDF}" \
            --extractor docling \
            --output-dir "${OUT_DIR}" || {
            echo "   ❌ Phase 1 FAILED for ${BASENAME}, skipping."
            FAILED+=("${BASENAME} (Phase 1)")
            continue
        }

        # Only related_work.txt is strictly required; bib extraction can legitimately
        # produce no output for some citation styles.
        if [ ! -f "${OUT_DIR}/related_work.txt" ]; then
            echo "   ❌ Phase 1 missing related_work.txt for ${BASENAME}, skipping."
            FAILED+=("${BASENAME} (Phase 1 missing outputs)")
            continue
        fi
    fi

    # Ensure paper_without_related.md exists for Phase 2
    if [ ! -f "${OUT_DIR}/paper_without_related.md" ]; then
        echo "   📄 Converting paper_without_related.pdf → .md via docling..."
        if [ -f "${OUT_DIR}/paper_without_related.pdf" ]; then
            python -c "
from pipeline.docling_extractor import extract_text_docling
text = extract_text_docling('${OUT_DIR}/paper_without_related.pdf')
with open('${OUT_DIR}/paper_without_related.md', 'w', encoding='utf-8') as f:
    f.write(text)
print(f'Wrote {len(text)} chars to paper_without_related.md')
" || {
                echo "   ❌ Markdown conversion failed for ${BASENAME}, skipping Phase 2."
                FAILED+=("${BASENAME} (MD conversion)")
                continue
            }
        else
            echo "   ❌ No paper_without_related.pdf found. Skipping Phase 2."
            FAILED+=("${BASENAME} (no paper_without_related.pdf)")
            continue
        fi
    fi

    # Phase 2: Generate related work + evaluate
    # (requires references.bib — skip gracefully if bib extraction found nothing)
    if [ ! -f "${OUT_DIR}/references.bib" ]; then
        echo "   ⚠️  No references.bib found (bib extraction matched 0 citations)."
        echo "   ⏭️  Skipping Phase 2 for ${BASENAME}."
        SUCCESS+=("${BASENAME} (Phase 1 only — no bib)")
        continue
    fi

    echo "   🖊️  Phase 2: Generating & evaluating related work..."
    python generate_related_work.py \
        --paper        "${OUT_DIR}/paper_without_related.md" \
        --bib          "${OUT_DIR}/references.bib" \
        --ground-truth "${OUT_DIR}/related_work.txt" \
        --output-dir   "${OUT_DIR}/generation" || {
        echo "   ❌ Phase 2 FAILED for ${BASENAME}"
        FAILED+=("${BASENAME} (Phase 2)")
        continue
    }

    echo "   ✅ ${BASENAME} complete!"
    SUCCESS+=("${BASENAME}")
done

echo ""
echo "════════════════════════════════════════════════════════════════"
echo "🏁 Batch run complete!"
echo "   ✅ Succeeded: ${#SUCCESS[@]} paper(s)"
for s in "${SUCCESS[@]}"; do echo "      • ${s}"; done
if [ ${#FAILED[@]} -gt 0 ]; then
    echo "   ❌ Failed:    ${#FAILED[@]} paper(s)"
    for f in "${FAILED[@]}"; do echo "      • ${f}"; done
fi
echo "════════════════════════════════════════════════════════════════"
