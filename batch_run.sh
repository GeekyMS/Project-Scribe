#!/bin/bash
# Batch run: Phase 1 (extraction) + Phase 2 (generation + evaluation) for multiple papers.
# Usage: source my_venv/bin/activate && bash batch_run.sh

set -e

PAPERS=(3 4 5 8 9 10 14 17)
BASE_DIR="test_outputs"

for P in "${PAPERS[@]}"; do
    PDF="test_data/paper${P}.pdf"
    OUT_DIR="${BASE_DIR}/paper${P}_run"

    echo ""
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║  Processing paper ${P}: ${PDF}"
    echo "╚══════════════════════════════════════════════════════════════╝"

    # ── Phase 1: Extract related work, generate bib ──
    if [ -f "${OUT_DIR}/paper_without_related.md" ] && [ -f "${OUT_DIR}/references.bib" ] && [ -f "${OUT_DIR}/related_work.txt" ]; then
        echo "   ⏭️  Phase 1 outputs already exist for paper ${P}, skipping extraction."
    else
        echo "   📄 Phase 1: Extracting from PDF..."
        python process_paper_v4.py "${PDF}" --output-dir "${OUT_DIR}" || {
            echo "   ❌ Phase 1 FAILED for paper ${P}, skipping."
            continue
        }

        # Check required outputs exist
        if [ ! -f "${OUT_DIR}/references.bib" ] || [ ! -f "${OUT_DIR}/related_work.txt" ]; then
            echo "   ❌ Phase 1 missing outputs for paper ${P}, skipping."
            continue
        fi

        # Generate markdown from the PDF if not present
        if [ ! -f "${OUT_DIR}/paper_without_related.md" ]; then
            echo "   ⚠️  No paper_without_related.md generated for paper ${P}."
            echo "   Checking if paper_without_related.pdf exists..."
            if [ -f "${OUT_DIR}/paper_without_related.pdf" ]; then
                echo "   📄 Converting PDF to markdown using docling..."
                python -c "
from pipeline.docling_extractor import extract_text_docling
text = extract_text_docling('${OUT_DIR}/paper_without_related.pdf')
with open('${OUT_DIR}/paper_without_related.md', 'w') as f:
    f.write(text)
print(f'   ✅ Wrote {len(text)} chars to paper_without_related.md')
" || {
                    echo "   ❌ Markdown conversion failed for paper ${P}, skipping."
                    continue
                }
            else
                echo "   ❌ No paper_without_related.pdf either. Skipping paper ${P}."
                continue
            fi
        fi
    fi

    # ── Phase 2: Generate related work + evaluate ──
    echo "   🖊️  Phase 2: Generating related work..."
    python generate_related_work.py \
        --paper "${OUT_DIR}/paper_without_related.md" \
        --bib "${OUT_DIR}/references.bib" \
        --ground-truth "${OUT_DIR}/related_work.txt" \
        --output-dir "${OUT_DIR}/generation" \
        --no-enrich || {
        echo "   ❌ Phase 2 FAILED for paper ${P}"
        continue
    }

    echo "   ✅ Paper ${P} complete!"
done

echo ""
echo "════════════════════════════════════════════════════════════════"
echo "🏁 Batch run complete!"
echo "════════════════════════════════════════════════════════════════"
