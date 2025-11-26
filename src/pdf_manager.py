from pypdf import PdfWriter, PdfReader
import os

def append_to_master(master_path, new_parts):
    """
    Appends new parts (list of pdf paths) to the master PDF.
    If master doesn't exist, creates it.
    """
    merger = PdfWriter()
    
    # If master exists, append it first
    if os.path.exists(master_path):
        merger.append(master_path)
    
    # Append new parts
    for pdf_path in new_parts:
        merger.append(pdf_path)
    
    merger.write(master_path)
    merger.close()
