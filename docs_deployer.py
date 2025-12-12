import os
import pickle
import markdown2
import io
import datetime
import time 
import struct
from urllib.request import Request, urlopen
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request as GoogleRequest
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
ENABLE_IMAGE_LOGO = True
LOGO_URL = 'https://github.com/callistofarm/markdown-handler/blob/main/logo.png'
TARGET_HEIGHT_PT = 37.5 

def authenticate():
    creds = None
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
        else:
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(host='0.0.0.0', port=8080, open_browser=False)
        with open('token.pickle', 'wb') as token:
            pickle.dump(creds, token)
    return creds

def get_services(creds):
    return build('drive', 'v3', credentials=creds), build('docs', 'v1', credentials=creds)

def sanitize_logo_url(url):
    if 'github.com' in url and '/blob/' in url:
        return url.replace('github.com', 'raw.githubusercontent.com').replace('/blob/', '/')
    return url

def get_png_ratio(url):
    try:
        print(f"    [i] Analysing logo dimensions: {url}")
        req = Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        data = urlopen(req, timeout=5).read(24)
        if data[:8] != b'\x89PNG\r\n\x1a\n': return 2.0
        w, h = struct.unpack('>LL', data[16:24])
        print(f"    [i] Detected Dimensions: {w}x{h} (Ratio: {w/h:.2f})")
        return w / h
    except:
        return 2.0

def execute_with_retry(request_object, retries=3, delay=2, strict=True):
    """
    strict=True: Raises exception if all retries fail (Critical steps)
    strict=False: Returns None if all retries fail (Optional steps like Logo)
    """
    for n in range(retries):
        try:
            return request_object.execute()
        except HttpError as e:
            # 500s are retryable, 400s are usually logic errors (except for images)
            if e.resp.status >= 500:
                print(f"    [~] API {e.resp.status}. Retrying in {delay}s... ({n+1}/{retries})")
                time.sleep(delay)
                delay *= 2 
            else:
                if strict: raise e
                else: 
                    print(f"    [!] Non-Critical Error {e.resp.status}: {e}")
                    return None
    
    if strict:
        raise Exception("API Retry Limit Exceeded")
    else:
        print("    [!] Soft Fail: All retries failed for optional step.")
        return None

def get_or_create_drive_path(service, folder_path):
    parts = folder_path.strip('/').split('/')
    parent_id = 'root'
    for part in parts:
        query = f"mimeType='application/vnd.google-apps.folder' and name='{part}' and '{parent_id}' in parents and trashed=false"
        results = service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
        items = results.get('files', [])
        if not items:
            metadata = {'name': part, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent_id]}
            parent_id = service.files().create(body=metadata, fields='id').execute().get('id')
        else:
            parent_id = items[0]['id']
    return parent_id

def load_style():
    if os.path.exists(STYLE_FILE):
        with open(STYLE_FILE, 'r') as f: return f.read()
    return ""

def apply_structure_and_branding(docs_service, doc_id, doc_title, logo_width_pt, logo_url):
    """
    Broken down into Atomic Phases. If Phase 4 (Logo) fails, the doc remains valid.
    """
    today_str = datetime.date.today().strftime("%d-%b-%Y")

    try:
        # --- PHASE 1: Structure (Headers/Footers) ---
        print("    [1/4] Creating Headers & Footers...")
        reqs_p1 = [
            {'createHeader': {'type': 'DEFAULT', 'sectionBreakLocation': {'index': 1}}},
            {'createFooter': {'type': 'DEFAULT', 'sectionBreakLocation': {'index': 1}}}
        ]
        execute_with_retry(docs_service.documents().batchUpdate(documentId=doc_id, body={'requests': reqs_p1}))
        
        # --- PHASE 2: Tables & Layout ---
        print("    [2/4] inserting Layout Tables...")
        # Need IDs first
        doc = execute_with_retry(docs_service.documents().get(documentId=doc_id))
        header_id = list(doc.get('headers', {}).keys())[0]
        footer_id = list(doc.get('footers', {}).keys())[0]
        
        reqs_p2 = [
            {'insertTable': {'segmentId': header_id, 'location': {'index': 0}, 'columns': 2, 'rows': 1}},
            {'insertTable': {'segmentId': footer_id, 'location': {'index': 0}, 'columns': 2, 'rows': 1}}
        ]
        execute_with_retry(docs_service.documents().batchUpdate(documentId=doc_id, body={'requests': reqs_p2}))

        # --- PHASE 3: Text Content ---
        print("    [3/4] Inserting Corporate Text...")
        # Re-fetch for indices
        doc = execute_with_retry(docs_service.documents().get(documentId=doc_id))
        h_table = doc['headers'][header_id]['content'][0]['table']
        h_cell_L = h_table['tableRows'][0]['tableCells'][0]['startIndex']
        h_cell_R = h_table['tableRows'][0]['tableCells'][1]['startIndex']
        
        f_table = doc['footers'][footer_id]['content'][0]['table']
        f_cell_L = f_table['tableRows'][0]['tableCells'][0]['startIndex']
        f_cell_R = f_table['tableRows'][0]['tableCells'][1]['startIndex']

        reqs_p3 = [
            {'insertText': {'segmentId': header_id, 'location': {'index': h_cell_R}, 'text': f"Ref: {doc_title}"}},
            {'insertText': {'segmentId': footer_id, 'location': {'index': f_cell_L}, 'text': f"Version: 1.0 | {today_str}"}},
            {'insertText': {'segmentId': footer_id, 'location': {'index': f_cell_R}, 'text': "Page "}},
            {'insertPageNumber': {'segmentId': footer_id, 'location': {'index': f_cell_R + 5}}},
            # Alignment & Borders
            {'updateParagraphStyle': {'segmentId': header_id, 'range': {'startIndex': h_cell_R, 'endIndex': h_cell_R + 1}, 'paragraphStyle': {'alignment': 'END'}, 'fields': 'alignment'}},
            {'updateParagraphStyle': {'segmentId': footer_id, 'range': {'startIndex': f_cell_R, 'endIndex': f_cell_R + 1}, 'paragraphStyle': {'alignment': 'END'}, 'fields': 'alignment'}},
            {'updateTableCellStyle': {'tableStartLocation': {'segmentId': header_id, 'index': 0}, 'fields': 'borderTop,borderBottom,borderLeft,borderRight', 'tableCellStyle': {'borderTop': {'style': 'NONE', 'width': {'magnitude': 0, 'unit': 'PT'}}, 'borderBottom': {'style': 'NONE', 'width': {'magnitude': 0, 'unit': 'PT'}}, 'borderLeft': {'style': 'NONE', 'width': {'magnitude': 0, 'unit': 'PT'}}, 'borderRight': {'style': 'NONE', 'width': {'magnitude': 0, 'unit': 'PT'}}}}},
            {'updateTableCellStyle': {'tableStartLocation': {'segmentId': footer_id, 'index': 0}, 'fields': 'borderTop,borderBottom,borderLeft,borderRight', 'tableCellStyle': {'borderTop': {'style': 'NONE', 'width': {'magnitude': 0, 'unit': 'PT'}}, 'borderBottom': {'style': 'NONE', 'width': {'magnitude': 0, 'unit': 'PT'}}, 'borderLeft': {'style': 'NONE', 'width': {'magnitude': 0, 'unit': 'PT'}}, 'borderRight': {'style': 'NONE', 'width': {'magnitude': 0, 'unit': 'PT'}}}}}
        ]
        execute_with_retry(docs_service.documents().batchUpdate(documentId=doc_id, body={'requests': reqs_p3}))

        # --- PHASE 4: Logo (FAULT TOLERANT) ---
        print("    [4/4] Inserting Logo...")
        logo_inserted = False
        if ENABLE_IMAGE_LOGO:
            logo_req = [{
                'insertInlineImage': {
                    'segmentId': header_id, 
                    'location': {'index': h_cell_L},
                    'uri': logo_url,
                    'objectSize': {'height': {'magnitude': TARGET_HEIGHT_PT, 'unit': 'PT'}, 'width': {'magnitude': logo_width_pt, 'unit': 'PT'}}
                }
            }]
            # STRICT=FALSE -> This will return None on failure instead of crashing the script
            result = execute_with_retry(docs_service.documents().batchUpdate(documentId=doc_id, body={'requests': logo_req}), strict=False)
            if result: logo_inserted = True

        if not logo_inserted:
            print("    [!] Logo failed or disabled. Inserting Fallback Text.")
            fallback = [{'insertText': {'segmentId': header_id, 'location': {'index': h_cell_L}, 'text': "[LOGO]"}}]
            execute_with_retry(docs_service.documents().batchUpdate(documentId=doc_id, body={'requests': fallback}))

        print("  [+] Branding Complete.")

    except Exception as e:
        print(f"  [!] CRITICAL FORMATTING FAILURE: {e}")
        print("      (The document content is safe, but headers/footers may be incomplete.)")

def main():
    creds = authenticate()
    drive, docs = get_services(creds)
    folder_id = get_or_create_drive_path(drive, DRIVE_FOLDER_NAME)
    css_style = load_style()
    
    final_logo_url = sanitize_logo_url(LOGO_URL)
    ratio = get_png_ratio(final_logo_url)
    logo_width_pt = TARGET_HEIGHT_PT * ratio
    
    print("--- Starting ISMS Deployment (Fault Tolerant Mode) ---")
    for filename in os.listdir(LOCAL_DOCS_DIR):
        if filename.endswith(".md"):
            print(f"\nProcessing: {filename}")
            try:
                # 1. Upload
                with open(os.path.join(LOCAL_DOCS_DIR, filename), 'r', encoding='utf-8') as f: content = f.read()
                html = markdown2.markdown(content, extras=["tables"])
                full_html = f"<html><head><style>{css_style}</style></head><body>{html}</body></html>"
                
                meta = {'name': filename.replace('.md', ''), 'parents': [folder_id], 'mimeType': 'application/vnd.google-apps.document'}
                media = MediaIoBaseUpload(io.BytesIO(full_html.encode('utf-8')), mimetype='text/html')
                
                file = execute_with_retry(drive.files().create(body=meta, media_body=media, fields='id'))
                doc_id = file.get('id')
                print(f"  [+] Uploaded (ID: {doc_id})")
                
                # 2. ToC
                toc_req = [
                    {'insertPageBreak': {'location': {'index': 1}}},
                    {'insertText': {'location': {'index': 1}, 'text': "Table of Contents\n"}},
                    {'updateParagraphStyle': {'range': {'startIndex': 1, 'endIndex': 15}, 'paragraphStyle': {'namedStyleType': 'HEADING_1'}, 'fields': 'namedStyleType'}}
                ]
                execute_with_retry(docs.documents().batchUpdate(documentId=doc_id, body={'requests': toc_req}))
                print("  [+] ToC Inserted")

                # 3. Branding (The risky part)
                apply_structure_and_branding(docs, doc_id, meta['name'], logo_width_pt, final_logo_url)
                
                time.sleep(1) # Breathe
                
            except Exception as e:
                print(f"  [!] FAILED to process {filename}: {e}")
    print("\n--- Deployment Complete ---")

if __name__ == '__main__':
    main()