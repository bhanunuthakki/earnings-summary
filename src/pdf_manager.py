from pypdf import PdfWriter, PdfReader
import os

def build_final_master(output_path, toc_path, content_path, toc_entries):
    """
    Merges TOC and Content, adding bookmarks.
    toc_entries: list of {'quarter', 'year', 'start_page'}
    """
    merger = PdfWriter()
    
    # 1. Add TOC
    merger.append(toc_path, import_outline=False)
    toc_reader = PdfReader(toc_path)
    toc_pages = len(toc_reader.pages)
    
    # 2. Add Content
    merger.append(content_path, import_outline=False)
    
    # 3. Add Bookmarks
    # The content starts after the TOC pages.
    # toc_entries 'start_page' is relative to the start of content (0-indexed).
    # So absolute page = toc_pages + start_page
    
    for entry in toc_entries:
        title = f"{entry['quarter']} {entry['year']}"
        page_num = toc_pages + entry['start_page']
        merger.add_outline_item(title, page_num)
        
    merger.write(output_path)
    merger.close()

def merge_pdfs(output_path, pdf_paths):
    """
    Simple merge for creating the content block.
    """
    merger = PdfWriter()
    for pdf in pdf_paths:
        merger.append(pdf, import_outline=False)
    merger.write(output_path)
    merger.close()
