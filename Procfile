web: gunicorn main:app --threads 4
worker-prod-1: python worker.py production
worker-prod-2: python worker.py production
worker-prod-3: python worker.py production
worker-prod-4: python worker.py production
worker-demo: python worker.py demo