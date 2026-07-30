import os
import weasyprint

def find_and_convert():
    base_dir = "/Users/raza/Documents/Research/"
    for root, dirs, files in os.walk(base_dir):
        for file in files:
            if file == "questionnaire_blind.html":
                html_path = os.path.join(root, file)
                pdf_path = os.path.join(root, "questionnaire_blind.pdf")
                print(f"Converting {html_path} to {pdf_path}...")
                try:
                    weasyprint.HTML(html_path).write_pdf(pdf_path)
                    print(f"Successfully created {pdf_path}")
                except Exception as e:
                    print(f"Error converting {html_path}: {e}")

if __name__ == "__main__":
    find_and_convert()
