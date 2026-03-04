FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Enterprise Gunicorn config for WebSockets: 40 threads, 4-hour timeout, 8KB request line limit
CMD ["sh", "-c", "gunicorn main:app --worker-class gthread --threads 40 --timeout 14400 --limit-request-line 8190 --bind 0.0.0.0:$PORT"]




