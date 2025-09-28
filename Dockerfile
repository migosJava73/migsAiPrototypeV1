FROM python:3.12-slim

# Install system deps for Tesseract OCR (robust for scanned policies)
RUN apt-get update && apt-get install -y tesseract-ocr libtesseract-dev libleptonica-dev tesseract-ocr-eng && rm -rf /var/lib/apt/lists/*

# Set workdir & copy code
WORKDIR /app
COPY . /app

# Install Python deps
RUN pip install --no-cache-dir -r requirements.txt

# Tesseract env (ensures OCR fallback for fine-print extraction)
ENV PATH="/usr/bin:${PATH}"
ENV TESSDATA_PREFIX="/usr/share/tesseract-ocr/5/tessdata"

# Run with gunicorn for prod (scales workers)
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--timeout", "60", "app:app"]