# Use official Python 3.12 slim image (small, fast, secure)
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies (needed for psycopg2, spacy, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first (cache layer optimization)
COPY requirements.txt .

# Upgrade pip and install dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy the rest of your app
COPY . .

# Expose port (Railway uses PORT env var anyway, but good practice)
EXPOSE 8080

# Run with gunicorn (production-ready)
CMD ["gunicorn", "--bind", "0.0.0.0:${PORT:-8080}", "main:app"]