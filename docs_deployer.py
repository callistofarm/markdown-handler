import os
import pickle
import markdown2
import io
import datetime
import time 
import struct
import json
import base64
from urllib.request import Request, urlopen
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from googleapiclient.errors import HttpError

# --- CONFIGURATION ---
SCOPES = [
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/documents'
]

LOCAL_DOCS_DIR = './artifacts'
STYLE_FILE = 'corporate_style.css'

# PASTE YOUR EXISTING CSCADA FOLDER ID HERE
ROOT_FOLDER_ID = '1pn4twkjjG_VlJOpAvrcdvE4C2PISyR3e' 

OUTPUT_FOLDER_NAME = 'docs_deployer_output'

ENABLE_IMAGE_LOGO = True
LOGO_URL = 'https://github.com/callistofarm/markdown-handler/blob/main/logo.png'
TARGET_HEIGHT_PT = 37.5 

def authenticate():
    creds = None
    env_sa = os.environ.get('GOOGLE_CREDENTIALS_B64')
    if env_sa:
        print("  [i] Auth: Found Service Account in Env Var...")
        try:
            key_dict = json.loads(base64.b64decode(env_sa))
            return service_account.Credentials.from_service_account_info(key_dict, scopes=SCOPES)
        except Exception as e:
            print(f"  [!] Env Var Auth Failed: {e}")

    if os.path.exists('service_account.json'):
        print("  [i] Auth: Found 'service_account.json'. Using Service Account...")
        return service_account.Credentials.from_service_account_file('service_account.json', scopes=SCOPES)

    print("  [i] Auth: Defaulting to User Credentials (credentials.json)...")
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)
            
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest())
        else:
            if not os.path.exists('credentials.json'):
                raise FileNotFoundError("CRITICAL: No 'service_account.json' OR 'credentials.json' found.")
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            print("\n--- GOOGLE USER AUTHENTICATION REQUIRED ---")
            print("1. Copy the URL below.")
            print("2. Paste it into your Windows Browser.")
            print("3. Login and click 'Allow'.")
            print("4. IMPORTANT: If the browser site fails to load (localhost refused):")
            print("   Copy the URL from the browser address bar (http://localhost:8080/?code=...)")
            print("   Open a new WSL terminal tab and run: curl \"<PASTED_URL>\"")
            creds = flow.run_local_server(host='localhost', port=8080, open_browser=False)
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

def execute_with_retry(request_object, retries=8, delay=5, strict=True):
    for n in range(retries):
        try:
            return request_object.execute()
        except HttpError as e:
            if e.resp.status >= 500:
                print(f"    [~] API {e.resp.status}. Retrying in {delay}s... ({n+1}/{retries})")
                time.sleep(delay)
                delay = min(delay * 1.5, 60)
            else:
                if strict: raise e
                else: 
                    print(f"    [!] Non-Critical Error {e.resp.status}: {e}")
                    return None
    if strict:
        raise Exception("API Retry Limit Exceeded")
    return None

def get_or_create_output_folder(service, root_id, folder_name):
    target_parent = root_id if root_id else 'root'
    print(f"Resolving Output Folder '{folder_name}' inside Parent ID: {target_parent}")
    query = f"mimeType='application/vnd.google-apps.folder' and name='{folder_name}' and '{target_parent}' in parents and trashed=false"
    results = service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
    items = results.get('files', [])
    if not items:
        metadata = {'name': folder_name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [target_parent]}
        try:
            folder = service.files().create(body=metadata, fields='id').execute()
            return folder.get('id')
        except HttpError as e:
             if e.resp.status == 404:
                 print("  [!] Error 404: The script cannot find the Parent Folder.")
             raise e
    else:
        print(f"  [.] Found Existing Output Folder: {folder_name}")
        return items[0]['id']

def load_style():
    if os.path.exists(STYLE_FILE):
        with open(STYLE_FILE, 'r') as f: return f.read()
    return ""

def find_table_cells(content_list):
    for element in content_list:
        if 'table' in element:
            row = element['table']['tableRows'][0]
            idx_l = row['tableCells'][0]['startIndex']
            idx_r = row['tableCells'][1]['startIndex']
            # CRITICAL FIX: Return Index + 1 (Start of Content, not Start of Cell)
            return idx_l + 1, idx_r + 1
    return None, None

def wait_and_get_indices(docs_service, doc_id, header_id, footer_id):
    print("    [...] Verifying Layout Propagation...")
    for i in range(10): 
        doc = execute_with_retry(docs_service.documents().get(documentId=doc_id))
        h_content = doc.get('headers', {}).get(header_id, {}).get('content', [])
        f_content = doc.get('footers', {}).get(footer_id, {}).get('content', [])
        h_L, h_R = find_table_cells(h_content)
        f_L, f_R = find_table_cells(f_content)
        if h_L is not None and f_L is not None:
            return h_L, h_R, f_L, f_R
        time.sleep(3)
    raise Exception("Timeout: Tables failed to propagate.")

def apply_structure_and_branding(docs_service, doc_id, doc_title, logo_width_pt, logo_url):
    today_str = datetime.date.today().strftime("%d-%b-%Y")
    try:
        # 1. Structure
        print("    [1/4] Creating Headers & Footers...")
        reqs_p1 = [{'createHeader': {'type': 'DEFAULT'}}, {'createFooter': {'type': 'DEFAULT'}}]
        execute_with_retry(docs_service.documents().batchUpdate(documentId=doc_id, body={'requests': reqs_p1}))
        
        # 2. Tables
        print("    [2/4] Inserting Layout Tables...")
        doc = execute_with_retry(docs_service.documents().get(documentId=doc_id))
        header_id = list(doc.get('headers', {}).keys())[0]
        footer_id = list(doc.get('footers', {}).keys())[0]
        
        reqs_p2 = [
            {'insertTable': {'location': {'segmentId': header_id, 'index': 0}, 'columns': 2, 'rows': 1}},
            {'insertTable': {'location': {'segmentId': footer_id, 'index': 0}, 'columns': 2, 'rows': 1}}
        ]
        execute_with_retry(docs_service.documents().batchUpdate(documentId=doc_id, body={'requests': reqs_p2}))

        # 3. Content
        print("    [3/4] Inserting Corporate Text...")
        h_L, h_R, f_L, f_R = wait_and_get_indices(docs_service, doc_id, header_id, footer_id)

        reqs_p3 = [
            {'insertText': {'location': {'segmentId': header_id, 'index': h_R}, 'text': f"Ref: {doc_title}"}},
            {'insertText': {'location': {'segmentId': footer_id, 'index': f_L}, 'text': f"Version: 1.0 | {today_str}"}},
            {'insertText': {'location': {'segmentId': footer_id, 'index': f_R}, 'text': "Page "}},
            
            # Alignment (Index is now correct)
            {'updateParagraphStyle': {'range': {'segmentId': header_id, 'startIndex': h_R, 'endIndex': h_R + 1}, 'paragraphStyle': {'alignment': 'END'}, 'fields': 'alignment'}},
            {'updateParagraphStyle': {'range': {'segmentId': footer_id, 'startIndex': f_R, 'endIndex': f_R + 1}, 'paragraphStyle': {'alignment': 'END'}, 'fields': 'alignment'}},
            
            # Borders (Back to Index - 1 because styling applies to the Cell Wall, not the Content)
            {'updateTableCellStyle': {
                'tableStartLocation': {'segmentId': header_id, 'index': 0}, 
                'fields': 'borderTop,borderBottom,borderLeft,borderRight', 
                'tableCellStyle': {
                    'borderTop': {'dashStyle': 'SOLID', 'width': {'magnitude': 0, 'unit': 'PT'}, 'color': {'color': {}}}, 
                    'borderBottom': {'dashStyle': 'SOLID', 'width': {'magnitude': 0, 'unit': 'PT'}, 'color': {'color': {}}}, 
                    'borderLeft': {'dashStyle': 'SOLID', 'width': {'magnitude': 0, 'unit': 'PT'}, 'color': {'color': {}}}, 
                    'borderRight': {'dashStyle': 'SOLID', 'width': {'magnitude': 0, 'unit': 'PT'}, 'color': {'color': {}}}
                }
            }},
            {'updateTableCellStyle': {
                'tableStartLocation': {'segmentId': footer_id, 'index': 0}, 
                'fields': 'borderTop,borderBottom,borderLeft,borderRight', 
                'tableCellStyle': {
                    'borderTop': {'dashStyle': 'SOLID', 'width': {'magnitude': 0, 'unit': 'PT'}, 'color': {'color': {}}}, 
                    'borderBottom': {'dashStyle': 'SOLID', 'width': {'magnitude': 0, 'unit': 'PT'}, 'color': {'color': {}}}, 
                    'borderLeft': {'dashStyle': 'SOLID', 'width': {'magnitude': 0, 'unit': 'PT'}, 'color': {'color': {}}}, 
                    'borderRight': {'dashStyle': 'SOLID', 'width': {'magnitude': 0, 'unit': 'PT'}, 'color': {'color': {}}}
                }
            }}
        ]
        execute_with_retry(docs_service.documents().batchUpdate(documentId=doc_id, body={'requests': reqs_p3}))

        # 4. Logo
        print("    [4/4] Inserting Logo...")
        logo_inserted = False
        if ENABLE_IMAGE_LOGO:
            logo_req = [{
                'insertInlineImage': {
                    'location': {'segmentId': header_id, 'index': h_L},
                    'uri': logo_url,
                    'objectSize': {'height': {'magnitude': TARGET_HEIGHT_PT, 'unit': 'PT'}, 'width': {'magnitude': logo_width_pt, 'unit': 'PT'}}
                }
            }]
            result = execute_with_retry(docs_service.documents().batchUpdate(documentId=doc_id, body={'requests': logo_req}), strict=False)
            if result: logo_inserted = True

        if not logo_inserted:
            print("    [!] Logo failed. Inserting Fallback.")
            fallback = [{'insertText': {'location': {'segmentId': header_id, 'index': h_L}, 'text': "[LOGO]"}}]
            execute_with_retry(docs_service.documents().batchUpdate(documentId=doc_id, body={'requests': fallback}))

        print("  [+] Branding Complete.")
    except Exception as e:
        print(f"  [!] CRITICAL FORMATTING FAILURE: {e}")

def main():
    creds = authenticate()
    drive, docs = get_services(creds)
    folder_id = get_or_create_output_folder(drive, ROOT_FOLDER_ID, OUTPUT_FOLDER_NAME)
    css_style = load_style()
    
    final_logo_url = sanitize_logo_url(LOGO_URL)
    ratio = get_png_ratio(final_logo_url)
    logo_width_pt = TARGET_HEIGHT_PT * ratio
    
    print("--- Starting ISMS Deployment (Index Corrected) ---")
    for filename in os.listdir(LOCAL_DOCS_DIR):
        if filename.endswith(".md"):
            print(f"\nProcessing: {filename}")
            try:
                with open(os.path.join(LOCAL_DOCS_DIR, filename), 'r', encoding='utf-8') as f: content = f.read()
                html = markdown2.markdown(content, extras=["tables"])
                full_html = f"<html><head><style>{css_style}</style></head><body>{html}</body></html>"
                
                meta = {'name': filename.replace('.md', ''), 'parents': [folder_id], 'mimeType': 'application/vnd.google-apps.document'}
                media = MediaIoBaseUpload(io.BytesIO(full_html.encode('utf-8')), mimetype='text/html')
                
                file = execute_with_retry(drive.files().create(body=meta, media_body=media, fields='id'))
                doc_id = file.get('id')
                print(f"  [+] Uploaded (ID: {doc_id})")
                
                toc_req = [
                    {'insertPageBreak': {'location': {'index': 1}}},
                    {'insertText': {'location': {'index': 1}, 'text': "Table of Contents\n"}},
                    {'updateParagraphStyle': {'range': {'startIndex': 1, 'endIndex': 15}, 'paragraphStyle': {'namedStyleType': 'HEADING_1'}, 'fields': 'namedStyleType'}}
                ]
                print("  [.] Waiting 10s for propagation...")
                time.sleep(10)
                execute_with_retry(docs.documents().batchUpdate(documentId=doc_id, body={'requests': toc_req}))
                print("  [+] ToC Inserted")

                apply_structure_and_branding(docs, doc_id, meta['name'], logo_width_pt, final_logo_url)
                
            except Exception as e:
                print(f"  [!] FAILED to process {filename}: {e}")
    print("\n--- Deployment Complete ---")

if __name__ == '__main__':
    main()