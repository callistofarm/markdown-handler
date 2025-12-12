import os
import pickle
import markdown2
import io
import datetime
import time 
import random
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from googleapiclient.errors import HttpError

# --- CONFIGURATION ---
SCOPES = [
    'https://www.googleapis.com/auth/drive.file',
    'https://www.googleapis.com/auth/documents'
]
LOCAL_DOCS_DIR = './artifacts'
STYLE_FILE = 'corporate_style.css'
DRIVE_FOLDER_NAME = 'CSCADA/docs_deployer_output'

# --- LOGO CONFIGURATION ---
# We keep the logo enabled, but the new Retry Logic will handle timeouts.
ENABLE_IMAGE_LOGO = True
LOGO_URL = 'https://www.ppa.org.fj/wp-content/uploads/2017/04/ComAp-master-logo-with-R-300x73.png'

# Safe Dimensions (50px Height) to prevent layout crashes
LOGO_HEIGHT_PT = 37.5
LOGO_WIDTH_PT = 154.1

def authenticate():
    creds = None
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            print("\n--- GOOGLE AUTHENTICATION REQUIRED ---")
            print("1. Copy URL. 2. Paste in Browser. 3. If fails, use curl \"URL\" in WSL.")
            creds = flow.run_local_server(host='0.0.0.0', port=8080, open_browser=False)
        with open('token.pickle', 'wb') as token:
            pickle.dump(creds, token)
    return creds

def get_drive_service(creds):
    return build('drive', 'v3', credentials=creds)

def get_docs_service(creds):
    return build('docs', 'v1', credentials=creds)

def execute_with_retry(request_object, retries=3, delay=2):
    """
    Executes a Google API request with exponential backoff for 500 errors.
    """
    for n in range(retries):
        try:
            return request_object.execute()
        except HttpError as e:
            if e.resp.status in [500, 502, 503, 504]:
                print(f"    [~] API {e.resp.status} Error. Retrying in {delay}s... ({n+1}/{retries})")
                time.sleep(delay)
                delay *= 2 # Exponential backoff
            else:
                raise e # Re-raise 4xx errors immediately
    print("    [!] All retries failed.")
    raise Exception("API Retry Limit Exceeded")

def get_or_create_drive_path(service, folder_path):
    parts = folder_path.strip('/').split('/')
    parent_id = 'root'
    for part in parts:
        query = f"mimeType='application/vnd.google-apps.folder' and name='{part}' and '{parent_id}' in parents and trashed=false"
        results = service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
        items = results.get('files', [])
        if not items:
            file_metadata = {'name': part, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent_id]}
            folder = service.files().create(body=file_metadata, fields='id').execute()
            parent_id = folder.get('id')
        else:
            parent_id = items[0]['id']
    return parent_id

def load_style():
    if os.path.exists(STYLE_FILE):
        with open(STYLE_FILE, 'r') as f:
            return f.read()
    return ""

def insert_toc_placeholder(docs_service, doc_id):
    requests = [
        {'insertPageBreak': {'location': {'index': 1}}},
        {'insertText': {'location': {'index': 1}, 'text': "\n[ACTION: Insert Table of Contents Here]\n"}},
        {'insertText': {'location': {'index': 1}, 'text': "Table of Contents\n"}},
        {'updateParagraphStyle': {'range': {'startIndex': 1, 'endIndex': 15}, 'paragraphStyle': {'namedStyleType': 'HEADING_1'}, 'fields': 'namedStyleType'}}
    ]
    try:
        execute_with_retry(docs_service.documents().batchUpdate(documentId=doc_id, body={'requests': requests}))
        print(f"  [+] Formatting: ToC Placeholder inserted.")
    except Exception as e:
        print(f"  [!] ToC Warning: {str(e)}")

def enable_table_header_repeat(docs_service, doc_id):
    try:
        doc = execute_with_retry(docs_service.documents().get(documentId=doc_id))
        body_content = doc.get('body', {}).get('content', [])
        requests = []
        for element in body_content:
            if 'table' in element:
                start_index = element['startIndex']
                requests.append({
                    'updateTableRowStyle': {
                        'tableStartLocation': {'index': start_index},
                        'rowIndices': [0],
                        'tableRowStyle': {'tableHeader': True},
                        'fields': 'tableHeader'
                    }
                })
        if requests:
            execute_with_retry(docs_service.documents().batchUpdate(documentId=doc_id, body={'requests': requests}))
            print(f"  [+] Formatting: 'Repeat Header Row' enabled.")
    except Exception as e:
        print(f"  [!] Table Header Warning: {str(e)}")

def add_corporate_header_footer(docs_service, doc_id, doc_title):
    try:
        # STEP 1: Init Containers
        init_reqs = [
            {'createHeader': {'type': 'DEFAULT', 'sectionBreakLocation': {'index': 1}}},
            {'createFooter': {'type': 'DEFAULT', 'sectionBreakLocation': {'index': 1}}}
        ]
        execute_with_retry(docs_service.documents().batchUpdate(documentId=doc_id, body={'requests': init_reqs}))
        time.sleep(1) # Sync Wait

        # STEP 2: Fetch IDs
        doc = execute_with_retry(docs_service.documents().get(documentId=doc_id))
        header_id = list(doc.get('headers', {}).keys())[0]
        footer_id = list(doc.get('footers', {}).keys())[0]

        # STEP 3: Create Tables
        table_reqs = [
            {'insertTable': {'segmentId': header_id, 'location': {'index': 0}, 'columns': 2, 'rows': 1}},
            {'insertTable': {'segmentId': footer_id, 'location': {'index': 0}, 'columns': 2, 'rows': 1}}
        ]
        execute_with_retry(docs_service.documents().batchUpdate(documentId=doc_id, body={'requests': table_reqs}))
        time.sleep(1) # Sync Wait

        # STEP 4: Calculate Indices (Fresh Fetch)
        doc = execute_with_retry(docs_service.documents().get(documentId=doc_id))
        h_table = doc['headers'][header_id]['content'][0]['table']
        h_cell_L = h_table['tableRows'][0]['tableCells'][0]['startIndex']
        h_cell_R = h_table['tableRows'][0]['tableCells'][1]['startIndex']
        
        f_table = doc['footers'][footer_id]['content'][0]['table']
        f_cell_L = f_table['tableRows'][0]['tableCells'][0]['startIndex']
        f_cell_R = f_table['tableRows'][0]['tableCells'][1]['startIndex']

        today_str = datetime.date.today().strftime("%d-%b-%Y")
        
        # STEP 5: Insert Text
        text_reqs = [
            {'insertText': {'segmentId': header_id, 'location': {'index': h_cell_R}, 'text': f"Ref: {doc_title}"}},
            {'insertText': {'segmentId': footer_id, 'location': {'index': f_cell_L}, 'text': f"Version: 1.0 | {today_str}"}},
            {'insertText': {'segmentId': footer_id, 'location': {'index': f_cell_R}, 'text': "Page "}},
            {'insertPageNumber': {'segmentId': footer_id, 'location': {'index': f_cell_R + 5}}}
        ]
        execute_with_retry(docs_service.documents().batchUpdate(documentId=doc_id, body={'requests': text_reqs}))
        time.sleep(0.5)

        # STEP 6: Alignment
        align_reqs = [
            {'updateParagraphStyle': {'segmentId': header_id, 'range': {'startIndex': h_cell_R, 'endIndex': h_cell_R + 1}, 'paragraphStyle': {'alignment': 'END'}, 'fields': 'alignment'}},
            {'updateParagraphStyle': {'segmentId': footer_id, 'range': {'startIndex': f_cell_R, 'endIndex': f_cell_R + 1}, 'paragraphStyle': {'alignment': 'END'}, 'fields': 'alignment'}}
        ]
        execute_with_retry(docs_service.documents().batchUpdate(documentId=doc_id, body={'requests': align_reqs}))

        # STEP 7: Branding (With Fallback)
        if ENABLE_IMAGE_LOGO:
            try:
                logo_req = [{
                    'insertInlineImage': {
                        'segmentId': header_id, 
                        'location': {'index': h_cell_L},
                        'uri': LOGO_URL,
                        'objectSize': {
                            'height': {'magnitude': LOGO_HEIGHT_PT, 'unit': 'PT'}, 
                            'width': {'magnitude': LOGO_WIDTH_PT, 'unit': 'PT'}
                        }
                    }
                }]
                # We use the retry wrapper here too, because 500s are common with images
                execute_with_retry(docs_service.documents().batchUpdate(documentId=doc_id, body={'requests': logo_req}), retries=2, delay=3)
            except Exception as e:
                print(f"  [!] Logo Timed Out. Applying Fallback Text.")
                fallback = [{'insertText': {'segmentId': header_id, 'location': {'index': h_cell_L}, 'text': "[ComAp LOGO]"}}]
                execute_with_retry(docs_service.documents().batchUpdate(documentId=doc_id, body={'requests': fallback}))
        else:
            fallback = [{'insertText': {'segmentId': header_id, 'location': {'index': h_cell_L}, 'text': "[ComAp LOGO]"}}]
            execute_with_retry(docs_service.documents().batchUpdate(documentId=doc_id, body={'requests': fallback}))

        # STEP 8: Styling/Borders
        style_reqs = [
            {'updateTableCellStyle': {'tableStartLocation': {'segmentId': header_id, 'index': 0}, 'fields': 'borderTop,borderBottom,borderLeft,borderRight', 'tableCellStyle': {'borderTop': {'style': 'NONE', 'width': {'magnitude': 0, 'unit': 'PT'}}, 'borderBottom': {'style': 'NONE', 'width': {'magnitude': 0, 'unit': 'PT'}}, 'borderLeft': {'style': 'NONE', 'width': {'magnitude': 0, 'unit': 'PT'}}, 'borderRight': {'style': 'NONE', 'width': {'magnitude': 0, 'unit': 'PT'}}}}},
            {'updateTableCellStyle': {'tableStartLocation': {'segmentId': footer_id, 'index': 0}, 'fields': 'borderTop,borderBottom,borderLeft,borderRight', 'tableCellStyle': {'borderTop': {'style': 'NONE', 'width': {'magnitude': 0, 'unit': 'PT'}}, 'borderBottom': {'style': 'NONE', 'width': {'magnitude': 0, 'unit': 'PT'}}, 'borderLeft': {'style': 'NONE', 'width': {'magnitude': 0, 'unit': 'PT'}}, 'borderRight': {'style': 'NONE', 'width': {'magnitude': 0, 'unit': 'PT'}}}}}
        ]
        execute_with_retry(docs_service.documents().batchUpdate(documentId=doc_id, body={'requests': style_reqs}))

        print(f"  [+] Formatting: Headers & Footers applied.")

    except Exception as e:
        print(f"  [!] Formatting Critical Error: {str(e)}")

def process_file(drive_service, docs_service, filename, local_path, folder_id, css_style):
    with open(local_path, 'r', encoding='utf-8') as f:
        md_content = f.read()
    
    html_body = markdown2.markdown(md_content, extras=["tables", "fenced-code-blocks"])
    full_html = f"<html><head><style>{css_style}</style></head><body>{html_body}</body></html>"
    
    file_metadata = {'name': filename.replace('.md', ''), 'parents': [folder_id], 'mimeType': 'application/vnd.google-apps.document'}
    media = MediaIoBaseUpload(io.BytesIO(full_html.encode('utf-8')), mimetype='text/html', resumable=True)
    
    # Retry the file upload/creation itself
    file = execute_with_retry(drive_service.files().create(body=file_metadata, media_body=media, fields='id'))
    doc_id = file.get('id')
    print(f"Deployed: {file_metadata['name']} (ID: {doc_id})")
    
    time.sleep(2) # Initial propagation wait
    
    insert_toc_placeholder(docs_service, doc_id)
    enable_table_header_repeat(docs_service, doc_id)
    add_corporate_header_footer(docs_service, doc_id, file_metadata['name'])

def main():
    creds = authenticate()
    drive_service = get_drive_service(creds)
    docs_service = get_docs_service(creds)
    folder_id = get_or_create_drive_path(drive_service, DRIVE_FOLDER_NAME)
    css_style = load_style()
    
    print("--- Starting ISMS Deployment (Resilience Mode) ---")
    for filename in os.listdir(LOCAL_DOCS_DIR):
        if filename.endswith(".md"):
            try:
                process_file(drive_service, docs_service, filename, os.path.join(LOCAL_DOCS_DIR, filename), folder_id, css_style)
                time.sleep(2) # Inter-file delay
            except Exception as e:
                print(f"Failed to process {filename}: {e}")
    print("--- Deployment Complete ---")

if __name__ == '__main__':
    main()