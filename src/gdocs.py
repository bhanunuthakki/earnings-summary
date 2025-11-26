from googleapiclient.errors import HttpError

def find_or_create_doc(drive_service, docs_service, company_name):
    """
    Searches for a document named "{company_name} Transcripts".
    If found, returns its ID.
    If not found, creates it and returns the new ID.
    """
    doc_title = f"{company_name} Transcripts"
    
    # Search for existing file
    query = f"name = '{doc_title}' and mimeType = 'application/vnd.google-apps.document' and trashed = false"
    results = drive_service.files().list(q=query, fields="nextPageToken, files(id, name)").execute()
    items = results.get('files', [])

    if items:
        print(f"Found existing document: {doc_title} ({items[0]['id']})")
        return items[0]['id']
    else:
        print(f"Creating new document: {doc_title}")
        doc_body = {
            'title': doc_title
        }
        doc = docs_service.documents().create(body=doc_body).execute()
        return doc.get('documentId')

def transcript_exists(docs_service, doc_id, quarter, year):
    """
    Checks if the transcript for the given quarter and year already exists.
    """
    header_text = f"{quarter} {year}"
    doc = docs_service.documents().get(documentId=doc_id).execute()
    content = doc.get('body').get('content')
    
    for element in content:
        if 'paragraph' in element:
            for run in element['paragraph']['elements']:
                if 'textRun' in run:
                    if header_text in run['textRun']['content']:
                        return True
    return False

def append_transcript(docs_service, doc_id, quarter, year, text):
    """
    Appends a header and the transcript text to the document.
    """
    header_text = f"\n\n{quarter} {year}\n"
    
    requests = [
        {
            'insertText': {
                'text': header_text + text,
                'endOfSegmentLocation': {
                    'segmentId': '' # Empty segmentId indicates the body
                }
            }
        },
    ]
    
    result = docs_service.documents().batchUpdate(documentId=doc_id, body={'requests': requests}).execute()
    return result
