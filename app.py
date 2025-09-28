import os
import fitz
from supabase import create_client, Client
from datetime import datetime, timezone
from flask import Flask, request, jsonify, make_response
from tenacity import retry, stop_after_attempt, wait_fixed

SUPABASE_URL = os.getenv("SUPABASE_URL")
SERVICE_ROLE_KEY = os.getenv("SERVICE_ROLE_KEY")
print(f"Env check: SUPABASE_URL set? {bool(SUPABASE_URL)}, SERVICE_ROLE_KEY set? {bool(SERVICE_ROLE_KEY)}")
if not all([SUPABASE_URL, SERVICE_ROLE_KEY]):
    raise ValueError("Missing Supabase env vars—check settings.")

supabase: Client = create_client(SUPABASE_URL, SERVICE_ROLE_KEY)

os.environ['PATH'] += os.pathsep + '/usr/bin'
os.environ['TESSDATA_PREFIX'] = '/usr/share/tesseract-ocr/5/tessdata'

@retry(stop=stop_after_attempt(2), wait=wait_fixed(1))
def extract_raw_text(pdf_bytes, contract_name, num_pages):
    raw_text = ""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    for page_num in range(len(doc)):
        page = doc[page_num]
        text = page.get_text("text")
        if len(text.strip()) < 50:
            try:
                textpage = page.get_textpage_ocr(language="eng", dpi=200)
                text = page.get_text("text", textpage=textpage)
            except Exception as ocr_err:
                print(f"OCR failed: {ocr_err}")
                raise
        raw_text += f"{text}\n\n"
    doc.close()

    raw_text = raw_text.replace('\n{3,}', '\n\n').replace(r'\s{2,}', ' ').strip()

    doc_type = "Vehicle Rental Agreement" if "rental" in contract_name.lower() else "Insurance Policy" if "insurance" in contract_name.lower() else "Document"
    return f"DOCUMENT: {contract_name}\nDOCUMENT TYPE: {doc_type}\nPAGES: {num_pages}\nPROCESSED: {datetime.now(timezone.utc).isoformat()}\n\nCONTENT:\n{raw_text}\n\n---\nEND OF DOCUMENT"

app = Flask(__name__)

@app.route('/', methods=['POST', 'OPTIONS'])
def process_pdf():
    print("=== Extraction triggered ===")

    if request.method == 'OPTIONS':
        response = make_response("ok")
        response.headers.update({
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST",
            "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type"
        })
        return response

    try:
        body = request.json
        print(f"Payload: {body}")
    except Exception as e:
        print(f"Parse error: {e}")
        return jsonify({'success': False, 'error': 'Invalid JSON', 'timestamp': datetime.now(timezone.utc).isoformat()}), 400

    if body.get('test') == 'ping':
        return jsonify({'success': True, 'message': 'Pong! GCP ready'}), 200

    contract_id = body.get('contract_id')
    if not contract_id:
        return jsonify({'success': False, 'error': 'Missing contract_id', 'timestamp': datetime.now(timezone.utc).isoformat()}), 400

    try:
        # Updated query handling for supabase-py 2.9.0
        response = supabase.table("contracts").select("storage_path, contract_name, file_name").eq("id", contract_id).execute()
        print(f"Supabase response: data={response.data}, count={response.count}")
        if not response.data:
            raise ValueError(f"No contract found for ID: {contract_id}")

        contract = response.data[0]
        file_path = contract['storage_path'].split("/storage/v1/object/public/contracts/")[1] if "/storage/v1/object/public/contracts/" in contract['storage_path'] else None
        if not file_path:
            raise ValueError("Invalid storage path")

        print(f"Downloading: {file_path}")
        download_response = supabase.storage.from_("contracts").download(file_path)
        if not download_response:
            raise ValueError("Download failed")

        num_pages = len(fitz.open(stream=download_response, filetype="pdf"))
        extracted_text = extract_raw_text(download_response, contract['contract_name'], num_pages)
        if not extracted_text:
            raise ValueError("Extraction failed")

        print(f"Extracted text length: {len(extracted_text)}")
        update_data = {"raw_text": extracted_text, "upload_status": "completed", "processed_at": datetime.now(timezone.utc).isoformat()}
        update_response = supabase.table("contracts").update(update_data).eq("id", contract_id).execute()
        if not update_response.data:
            raise ValueError("Update failed—check Supabase logs")

        print("Extraction successful!")
        return jsonify({
            'success': True,
            'message': 'Processed',
            'contract_id': contract_id,
            'text_length': len(extracted_text),
            'preview': extracted_text[:500] + '...'
        }), 200

    except Exception as error:
        print(f"Error: {error}")
        if contract_id:
            supabase.table("contracts").update({
                "upload_status": "failed",
                "raw_text": f"Failed: {str(error)}.",
                "processed_at": datetime.now(timezone.utc).isoformat()
            }).eq("id", contract_id).execute()
        return jsonify({'success': False, 'error': str(error), 'timestamp': datetime.now(timezone.utc).isoformat()}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))