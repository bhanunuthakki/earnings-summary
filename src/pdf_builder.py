from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
import re

def create_cover_page(output_path, company, quarter, year):
    """
    Creates a cover page for the transcript PDF.
    """
    doc = SimpleDocTemplate(output_path, pagesize=letter)
    styles = getSampleStyleSheet()
    
    story = []
    
    # Add some vertical space to center the text roughly
    story.append(Spacer(1, 200))
    
    # Title Style
    title_style = styles["Title"]
    title_style.alignment = TA_CENTER
    title_style.fontSize = 30
    title_style.leading = 36
    
    # Subtitle Style
    subtitle_style = styles["Heading2"]
    subtitle_style.alignment = TA_CENTER
    subtitle_style.fontSize = 20
    subtitle_style.leading = 24
    
    story.append(Paragraph(company, title_style))
    story.append(Spacer(1, 20))
    story.append(Paragraph(f"{quarter} {year} Earnings Call", subtitle_style))
    
    doc.build(story)

def create_master_toc(output_path, company, entries):
    """
    Creates a Table of Contents PDF.
    entries: list of dicts {'quarter': 'Q1', 'year': '2024', 'page': 123}
    """
    doc = SimpleDocTemplate(output_path, pagesize=letter)
    styles = getSampleStyleSheet()
    
    story = []
    
    # Title
    title_style = styles["Title"]
    title_style.alignment = TA_CENTER
    story.append(Paragraph(f"{company} Earnings Transcripts", title_style))
    story.append(Spacer(1, 36))
    
    # TOC Header
    h2_style = styles["Heading2"]
    h2_style.alignment = TA_LEFT
    story.append(Paragraph("Table of Contents", h2_style))
    story.append(Spacer(1, 12))
    
    # Entries
    normal_style = styles["Normal"]
    normal_style.fontSize = 14
    normal_style.leading = 18
    
    for entry in entries:
        text = f"{entry['quarter']} {entry['year']} - Page {entry['page']}"
        story.append(Paragraph(text, normal_style))
        story.append(Spacer(1, 12))
        
    doc.build(story)

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
    formatted_text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', summary_text)
    
    # Sanitize <br> tags for ReportLab (must be self-closing)
    formatted_text = re.sub(r'<br\s*/?>', '<br/>', formatted_text, flags=re.IGNORECASE)
    
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
            clean_text = re.sub(r'<[^>]+>', '', para).strip()
            
            if clean_text.startswith("#") or (clean_text and clean_text[0].isdigit() and ":" in clean_text):
                 display_text = para.replace("#", "").strip()
                 story.append(Paragraph(display_text, styles["Heading2"]))
            else:
                 story.append(Paragraph(para, normal_style))
            story.append(Spacer(1, 12))
            
    doc.build(story)

def create_analysis_pdf(output_path, analysis_text):
    """
    Creates a PDF from the strategic analysis text.
    Similar to summary pdf but with different styling/title.
    """
    doc = SimpleDocTemplate(output_path, pagesize=letter,
                            rightMargin=72, leftMargin=72,
                            topMargin=72, bottomMargin=18)
    
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='Justify', alignment=TA_JUSTIFY))
    
    # Text formatting
    formatted_text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', analysis_text)
    formatted_text = re.sub(r'<br\s*/?>', '<br/>', formatted_text, flags=re.IGNORECASE)
    
    story = []
    
    # Title
    title_style = styles["Heading1"]
    title_style.alignment = TA_CENTER
    title_style.textColor = "darkblue" # Distinction
    story.append(Paragraph("Strategic Performance Analysis", title_style))
    story.append(Spacer(1, 4))
    story.append(Paragraph("Expectations vs. Reality", styles["Normal"]))
    story.append(Spacer(1, 24))
    
    normal_style = styles["Normal"]
    
    paragraphs = formatted_text.split('\n')
    for para in paragraphs:
        if para.strip():
            clean_text = re.sub(r'<[^>]+>', '', para).strip()
            
            # Formatting heuristics
            if clean_text.startswith("#") or (clean_text and clean_text[0].isdigit() and "." in clean_text[:3]):
                 # Headings
                 display_text = para.replace("#", "").strip()
                 story.append(Paragraph(display_text, styles["Heading2"]))
            elif clean_text.startswith("*") or clean_text.startswith("-"):
                 # Bullets
                 bullet_text = para.strip()[1:].strip()
                 story.append(Paragraph(f"• {bullet_text}", normal_style))
            else:
                 story.append(Paragraph(para, normal_style))
            story.append(Spacer(1, 10))
            
    doc.build(story)
