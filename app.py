from flask import Flask, request, jsonify
import fitz  # PyMuPDF
import pytesseract
from PIL import Image
import io
import os
import requests
from supabase import create_client, Client
from datetime import datetime
import logging
import json

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Supabase configuration (match Cloud Run secret names)
SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_SERVICE_ROLE_KEY = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')

# Debug env vars and fail fast if missing
logger.info("=== Flask app starting ===")
logger.info(f"Env: SUPABASE_URL={SUPABASE_URL[:20] if SUPABASE_URL else None}..., SERVICE_ROLE_KEY={SUPABASE_SERVICE_ROLE_KEY[:10] if SUPABASE_SERVICE_ROLE_KEY else None}...")
if not all([SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY]):
    logger.error("Missing Supabase environment variables: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY")
    raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
logger.info("Supabase client initialized")

# Tesseract env setup
os.environ['PATH'] += os.pathsep + '/usr/bin'
os.environ['TESSDATA_PREFIX'] = '/usr/share/tesseract-ocr/5/tessdata'
logger.info("Tesseract env set")

def extract_text_from_pdf(pdf_url):
    """Extract text from PDF with OCR fallback for scanned pages"""
    try:
        logger.info(f"Downloading PDF from: {pdf_url}")
        response = requests.get(pdf_url)
        response.raise_for_status()
        
        pdf_bytes = response.content
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        text = ""
        
        for page_num in range(len(doc)):
            page = doc.load_page(page_num)
            page_text = page.get_text()
            
            if len(page_text.strip()) < 50:  # Likely scanned page
                logger.info(f"Page {page_num + 1} appears to be scanned, using OCR")
                pix = page.get_pixmap()
                img_data = pix.tobytes("png")
                img = Image.open(io.BytesIO(img_data))
                ocr_text = pytesseract.image_to_string(img)
                text += f"\n--- Page {page_num + 1} (OCR) ---\n{ocr_text}"
            else:
                text += f"\n--- Page {page_num + 1} ---\n{page_text}"
        
        doc.close()
        logger.info(f"Extracted {len(text)} characters from PDF")
        return text
        
    except Exception as e:
        logger.error(f"Error extracting text from PDF: {str(e)}")
        raise

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    logger.info("Health check called")
    return jsonify({"status": "healthy"}), 200

@app.route('/', methods=['GET', 'POST'])
def process_contract():
    """Main endpoint for processing contracts"""
    if request.method == 'GET':
        logger.info("Ping received")
        return jsonify({"success": True, "message": "Pong! GCP ready"}), 200
    
    try:
        # Get ALL webhook data for debugging
        webhook_data = {}
        
        # Check JSON body
        if request.is_json:
            webhook_data['json_body'] = request.get_json()
        
        # Check form data
        if request.form:
            webhook_data['form_data'] = request.form.to_dict()
        
        # Check query parameters
        if request.args:
            webhook_data['query_params'] = request.args.to_dict()
        
        # Check headers (for debugging)
        webhook_data['headers'] = dict(request.headers)
        
        # Log everything for debugging
        logger.info(f"=== WEBHOOK DEBUG INFO ===")
        logger.info(f"Content-Type: {request.content_type}")
        logger.info(f"Method: {request.method}")
        logger.info(f"Raw data: {request.get_data()}")
        logger.info(f"Webhook data: {json.dumps(webhook_data, indent=2)}")
        logger.info(f"========================")
        
        # Try to extract contract_id and upload_status from various sources
        contract_id = None
        upload_status = None
        
        # Priority 1: JSON body
        if webhook_data.get('json_body'):
            contract_id = webhook_data['json_body'].get('contract_id')
            upload_status = webhook_data['json_body'].get('upload_status')
        
        # Priority 2: Form data
        if not contract_id and webhook_data.get('form_data'):
            contract_id = webhook_data['form_data'].get('contract_id')
            upload_status = webhook_data['form_data'].get('upload_status')
        
        # Priority 3: Query parameters
        if not contract_id and webhook_data.get('query_params'):
            contract_id = webhook_data['query_params'].get('contract_id')
            upload_status = webhook_data['query_params'].get('upload_status')
        
        logger.info(f"Extracted - contract_id: {contract_id}, upload_status: {upload_status}")
        
        if not contract_id:
            logger.error("No contract_id found in any data source")
            return jsonify({
                "success": False, 
                "error": "contract_id is required",
                "debug_info": webhook_data
            }), 400
        
        # Check if upload_status is "processing" (if provided by webhook)
        if upload_status and upload_status != "processing":
            logger.info(f"Webhook upload_status is '{upload_status}' - skipping processing")
            return jsonify({
                "success": True, 
                "message": f"Skipped - webhook status is {upload_status}",
                "debug_info": webhook_data
            }), 200
        
        # Fetch contract details from Supabase to double-check
        logger.info(f"Fetching contract details for ID: {contract_id}")
        result = supabase.table('contracts').select('*').eq('id', contract_id).execute()
        
        if not result.data:
            logger.error(f"Contract not found: {contract_id}")
            return jsonify({
                "success": False, 
                "error": "Contract not found",
                "contract_id": contract_id
            }), 404
        
        contract = result.data[0]
        storage_path = contract.get('storage_path')
        db_upload_status = contract.get('upload_status')
        
        logger.info(f"Database upload_status: {db_upload_status}")
        
        # Double-check the status from database
        if db_upload_status != 'processing':
            logger.info(f"Database status is '{db_upload_status}' - skipping processing")
            return jsonify({
                "success": True, 
                "message": f"Skipped - database status is {db_upload_status}",
                "webhook_status": upload_status,
                "database_status": db_upload_status
            }), 200
        
        if not storage_path:
            logger.error(f"No storage_path for contract: {contract_id}")
            return jsonify({
                "success": False, 
                "error": "No storage_path found",
                "contract_id": contract_id
            }), 400
        
        # Extract text from PDF
        logger.info(f"Starting text extraction for contract: {contract_id}")
        raw_text = extract_text_from_pdf(storage_path)
        
        # Update contract with extracted text
        update_data = {
            'raw_text': raw_text,
            'upload_status': 'completed',
            'processed_at': datetime.utcnow().isoformat()
        }
        
        logger.info(f"Updating contract {contract_id} with extracted text")
        supabase.table('contracts').update(update_data).eq('id', contract_id).execute()
        
        logger.info(f"Extraction successful for contract: {contract_id}")
        return jsonify({
            "success": True, 
            "message": "Extraction successful!",
            "contract_id": contract_id,
            "text_length": len(raw_text),
            "webhook_status": upload_status,
            "database_status": db_upload_status
        }), 200
        
    except Exception as e:
        logger.error(f"Error processing contract: {str(e)}")
        return jsonify({
            "success": False, 
            "error": str(e),
            "debug_info": webhook_data if 'webhook_data' in locals() else None
        }), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
