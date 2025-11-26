from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.enums import TA_JUSTIFY, TA_CENTER
from reportlab.lib import colors
import os

def create_cover_page(output_path, company, quarter, year):
    """
    Creates a PDF cover page.
    """
    c = canvas.Canvas(output_path, pagesize=letter)
    width, height = letter
    
    c.setFont("Helvetica-Bold", 36)
    c.drawCentredString(width / 2, height / 2 + 50, f"{company}")
    
    c.setFont("Helvetica", 24)
    c.drawCentredString(width / 2, height / 2, f"{quarter} {year} Earnings Transcript")
    
    c.save()

import re

def create_summary_pdf(output_path, summary_text):
    """
    Creates a PDF from the summary text.
    """
    doc = SimpleDocTemplate(output_path, pagesize=letter,
                            rightMargin=72, leftMargin=72,
                            topMargin=72, bottomMargin=18)
    
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='Justify', alignment=TA_JUSTIFY))
    
    # Use regex to replace **text** with <b>text</b>
    # This handles the pairs correctly.
    formatted_text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', summary_text)
    
    # Split into paragraphs
    paragraphs = formatted_text.split('\n')
    
    story = []
    
    title_style = styles["Heading1"]
    title_style.alignment = TA_CENTER
    story.append(Paragraph("Earnings Call Summary", title_style))
    story.append(Spacer(1, 12))
    
    normal_style = styles["Normal"]
    
    for para in paragraphs:
        if para.strip():
            # Check if it looks like a header (e.g., "1. Executive Summary:")
            # We strip HTML tags for the check
            clean_text = re.sub(r'<[^>]+>', '', para).strip()
            
            if clean_text.startswith("#") or (clean_text and clean_text[0].isdigit() and ":" in clean_text):
                 # Remove markdown headers #
                 display_text = para.replace("#", "").strip()
                 story.append(Paragraph(display_text, styles["Heading2"]))
            else:
                 story.append(Paragraph(para, normal_style))
            story.append(Spacer(1, 12))
            
    doc.build(story)
