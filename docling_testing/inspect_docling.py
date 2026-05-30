from docling.document_converter import DocumentConverter
converter = DocumentConverter()
res = converter.convert("../test_data/paper4.pdf")
for item, depth in res.document.iterate_items():
    if hasattr(item, "label") and item.label == "list_item":
        print(f"ListItem attributes: {dir(item)}")
        if hasattr(item, "marker"):
            print(f"Marker: {item.marker}")
        if hasattr(item, "enumerated_marker"):
            print(f"Enum Marker: {item.enumerated_marker}")
        break
