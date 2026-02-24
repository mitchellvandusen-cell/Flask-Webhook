FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Enterprise Gunicorn config for WebSockets: 20 threads, 1-hour timeout
CMD ["sh", "-c", "gunicorn main:app --worker-class gthread --threads 20 --timeout 14400 --bind 0.0.0.0:${PORT:-8080}"]

