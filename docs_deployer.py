import os
import pickle
import markdown2
import io
import datetime
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# --- CONFIGURATION ---
SCOPES = [
    'https://www.googleapis.com/auth/drive.file',
    'https://www.googleapis.com/auth/documents'
]
LOCAL_DOCS_DIR = './artifacts'
STYLE_FILE = 'corporate_style.css'
DRIVE_FOLDER_NAME = 'ISMS_v1_Staging'

# REPLACE THIS with a direct link to your logo image (Must be public or accessible by the API)
LOGO_URL = 'https://upload.wikimedia.org/wikipedia/commons/b/b1/ComAp_master_logo.png' 

def authenticate():
    """Handles OAuth 2.0 Authentication."""
    creds = None
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                'credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.pickle', 'wb') as token:
            pickle.dump(creds, token)
    return creds

def get_drive_service(creds):
    return build('drive', 'v3', credentials=creds)

def get_docs_service(creds):
    return build('docs', 'v1', credentials=creds)

def create_folder(service, folder_name):
    """Creates or retrieves the target folder ID."""
    file_metadata = {
        'name': folder_name,
        'mimeType': 'application/vnd.google-apps.folder'
    }
    results = service.files().list(
        q=f"mimeType='application/vnd.google-apps.folder' and name='{folder_name}' and trashed=false",
        spaces='drive').execute()
    items = results.get('files', [])
    
    if not items:
        folder = service.files().create(body=file_metadata, fields='id').execute()
        print(f"Directory Created: {folder_name}")
        return folder.get('id')
    else:
        return items[0]['id']

def load_style():
    if os.path.exists(STYLE_FILE):
        with open(STYLE_FILE, 'r') as f:
            return f.read()
    return ""

def insert_toc(docs_service, doc_id):
    """Inserts native Table of Contents."""
    requests = [
        {'insertTableOfContents': {'location': {'index': 1}, 'type': 'DEFAULT'}},
        {'insertText': {'location': {'index': 1}, 'text': "Table of Contents\n"}},
        {'updateParagraphStyle': {'range': {'startIndex': 1, 'endIndex': 15}, 'paragraphStyle': {'namedStyleType': 'HEADING_2'}, 'fields': 'namedStyleType'}},
        {'insertPageBreak': {'location': {'index': 2}}}
    ]
    try:
        docs_service.documents().batchUpdate(documentId=doc_id, body={'requests': requests}).execute()
        print(f"  [+] Formatting: Table of Contents inserted.")
    except Exception as e:
        print(f"  [!] ToC Error: {str(e)}")

def enable_table_header_repeat(docs_service, doc_id):
    """
    Scans document for Tables and sets the 'tableHeader' property on the first row 
    of every table to True. This ensures the red header repeats across pages.
    """
    try:
        doc = docs_service.documents().get(documentId=doc_id).execute()
        body_content = doc.get('body', {}).get('content', [])
        
        requests = []
        
        for element in body_content:
            if 'table' in element:
                # Found a table. Get its start index.
                table = element['table']
                start_index = element['startIndex']
                
                # Request to update the first row (index 0) of this table
                requests.append({
                    'updateTableRowStyle': {
                        'tableStartLocation': {
                            'index': start_index
                        },
                        'rowIndices': [0], # Target first row
                        'tableRowStyle': {
                            'tableHeader': True
                        },
                        'fields': 'tableHeader'
                    }
                })
        
        if requests:
            docs_service.documents().batchUpdate(documentId=doc_id, body={'requests': requests}).execute()
            print(f"  [+] Formatting: 'Repeat Header Row' enabled for {len(requests)} tables.")
        
    except Exception as e:
        print(f"  [!] Table Header Error: {str(e)}")

def add_corporate_header_footer(docs_service, doc_id, doc_title):
    """
    Applies Corporate Header (Logo + Doc ID) and Footer (Version/Date + Page #).
    Uses 1x2 Tables for layout.
    """
    # 1. Create Header/Footer containers
    init_requests = [
        {'createHeader': {'type': 'DEFAULT', 'sectionBreakLocation': {'index': 1}}},
        {'createFooter': {'type': 'DEFAULT', 'sectionBreakLocation': {'index': 1}}}
    ]
    
    try:
        docs_service.documents().batchUpdate(documentId=doc_id, body={'requests': init_requests}).execute()

        # 2. Get IDs for the new Header/Footer
        doc = docs_service.documents().get(documentId=doc_id).execute()
        
        # Guard clause in case headers weren't created
        if 'headers' not in doc or 'footers' not in doc:
             print("  [!] Error: Headers/Footers could not be initialized.")
             return

        header_id = list(doc.get('headers', {}).keys())[0]
        footer_id = list(doc.get('footers', {}).keys())[0]

        # 3. Insert Layout Tables (1 Row, 2 Columns) into Header and Footer
        table_requests = [
            {'insertTable': {'segmentId': header_id, 'location': {'index': 0}, 'columns': 2, 'rows': 1}},
            {'insertTable': {'segmentId': footer_id, 'location': {'index': 0}, 'columns': 2, 'rows': 1}}
        ]
        docs_service.documents().batchUpdate(documentId=doc_id, body={'requests': table_requests}).execute()

        # 4. Get Table Cell Locations (Requires re-fetching doc to get structure)
        doc = docs_service.documents().get(documentId=doc_id).execute()
        
        # Extract Header Table Cells
        header_content = doc['headers'][header_id]['content']
        h_table = header_content[0]['table']
        h_cell_left_idx = h_table['tableRows'][0]['tableCells'][0]['startIndex']
        h_cell_right_idx = h_table['tableRows'][0]['tableCells'][1]['startIndex']

        # Extract Footer Table Cells
        footer_content = doc['footers'][footer_id]['content']
        f_table = footer_content[0]['table']
        f_cell_left_idx = f_table['tableRows'][0]['tableCells'][0]['startIndex']
        f_cell_right_idx = f_table['tableRows'][0]['tableCells'][1]['startIndex']

        # 5. Insert Content & Styling
        logo_w_pt = 110.55
        logo_h_pt = 34.02
        today_str = datetime.date.today().strftime("%d-%b-%Y")
        
        content_requests = [
            # --- HEADER ---
            {
                'insertInlineImage': {
                    'segmentId': header_id,
                    'location': {'index': h_cell_left_idx},
                    'uri': LOGO_URL,
                    'objectSize': {'height': {'magnitude': logo_h_pt, 'unit': 'PT'}, 'width': {'magnitude': logo_w_pt, 'unit': 'PT'}}
                }
            },
            {
                'insertText': {
                    'segmentId': header_id,
                    'location': {'index': h_cell_right_idx},
                    'text': f"Ref: {doc_title}"
                }
            },
            {
                'updateParagraphStyle': {
                    'segmentId': header_id,
                    'range': {'startIndex': h_cell_right_idx, 'endIndex': h_cell_right_idx + 1},
                    'paragraphStyle': {'alignment': 'END'},
                    'fields': 'alignment'
                }
            },

            # --- FOOTER ---
            {
                'insertText': {
                    'segmentId': footer_id,
                    'location': {'index': f_cell_left_idx},
                    'text': f"Version: 1.0 | Updated: {today_str}"
                }
            },
            {
                'insertText': {
                    'segmentId': footer_id,
                    'location': {'index': f_cell_right_idx},
                    'text': "Page "
                }
            },
            {
                'insertPageNumber': {
                    'segmentId': footer_id,
                    'location': {'index': f_cell_right_idx + 5}
                }
            },
            {
                'updateParagraphStyle': {
                    'segmentId': footer_id,
                    'range': {'startIndex': f_cell_right_idx, 'endIndex': f_cell_right_idx + 1},
                    'paragraphStyle': {'alignment': 'END'},
                    'fields': 'alignment'
                }
            },

            # --- CLEANUP (Remove Borders) ---
            {
                'updateTableCellStyle': {
                    'tableStartLocation': {'segmentId': header_id, 'index': 0},
                    'fields': 'borderTop,borderBottom,borderLeft,borderRight',
                    'tableCellStyle': {
                        'borderTop': {'style': 'NONE', 'width': {'magnitude': 0, 'unit': 'PT'}},
                        'borderBottom': {'style': 'NONE', 'width': {'magnitude': 0, 'unit': 'PT'}},
                        'borderLeft': {'style': 'NONE', 'width': {'magnitude': 0, 'unit': 'PT'}},
                        'borderRight': {'style': 'NONE', 'width': {'magnitude': 0, 'unit': 'PT'}}
                    }
                }
            },
            {
                'updateTableCellStyle': {
                    'tableStartLocation': {'segmentId': footer_id, 'index': 0},
                    'fields': 'borderTop,borderBottom,borderLeft,borderRight',
                    'tableCellStyle': {
                        'borderTop': {'style': 'NONE', 'width': {'magnitude': 0, 'unit': 'PT'}},
                        'borderBottom': {'style': 'NONE', 'width': {'magnitude': 0, 'unit': 'PT'}},
                        'borderLeft': {'style': 'NONE', 'width': {'magnitude': 0, 'unit': 'PT'}},
                        'borderRight': {'style': 'NONE', 'width': {'magnitude': 0, 'unit': 'PT'}}
                    }
                }
            }
        ]

        docs_service.documents().batchUpdate(documentId=doc_id, body={'requests': content_requests}).execute()
        print(f"  [+] Formatting: Corporate Headers & Footers applied.")
    except Exception as e:
        print(f"  [!] Formatting Error: {str(e)}")

def process_file(drive_service, docs_service, filename, local_path, folder_id, css_style):
    with open(local_path, 'r', encoding='utf-8') as f:
        md_content = f.read()
    
    html_body = markdown2.markdown(md_content, extras=["tables", "fenced-code-blocks"])
    
    # --- SPECIFIC TABLE STYLING INJECTION ---
    # Header: BG #F00000 (RGB 240,0,0), Text White, Bold.
    # Data: Text Black, BG Transparent.
    # Alignment: Left, Middle.
    
    table_css = """
    table { border-collapse: collapse; width: 100%; margin-bottom: 20px; }
    th { 
        background-color: #F00000; 
        color: #FFFFFF; 
        font-weight: bold; 
        text-align: left; 
        vertical-align: middle; 
        padding: 8px;
        border: 1px solid #999999;
    }
    td { 
        background-color: transparent; 
        color: #000000; 
        text-align: left; 
        vertical-align: middle; 
        padding: 8px;
        border: 1px solid #999999;
    }
    """
    
    full_html = f"""
    <html>
    <head>
        <style>
            {css_style}
            {table_css} 
        </style>
    </head>
    <body>{html_body}</body>
    </html>
    """
    
    doc_title = filename.replace('.md', '')
    
    file_metadata = {
        'name': doc_title,
        'parents': [folder_id],
        'mimeType': 'application/vnd.google-apps.document'
    }
    
    media = MediaIoBaseUpload(
        io.BytesIO(full_html.encode('utf-8')),
        mimetype='text/html',
        resumable=True
    )
    
    file = drive_service.files().create(body=file_metadata, media_body=media, fields='id').execute()
    doc_id = file.get('id')
    print(f"Deployed: {doc_title} (ID: {doc_id})")
    
    insert_toc(docs_service, doc_id)
    enable_table_header_repeat(docs_service, doc_id) # Apply repeat header logic
    add_corporate_header_footer(docs_service, doc_id, doc_title)

def main():
    creds = authenticate()
    drive_service = get_drive_service(creds)
    docs_service = get_docs_service(creds)
    
    folder_id = create_folder(drive_service, DRIVE_FOLDER_NAME)
    css_style = load_style()
    
    print("--- Starting ISMS Deployment ---")
    for filename in os.listdir(LOCAL_DOCS_DIR):
        if filename.endswith(".md"):
            try:
                process_file(drive_service, docs_service, filename, 
                           os.path.join(LOCAL_DOCS_DIR, filename), 
                           folder_id, css_style)
            except Exception as e:
                print(f"Failed to process {filename}: {e}")
    print("--- Deployment Complete ---")

if __name__ == '__main__':
    main()