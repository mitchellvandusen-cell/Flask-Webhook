web: gunicorn main:app --threads 40 --timeout 0 --limit-request-line 8190
worker-prod-1: python worker.py production
worker-prod-2: python worker.py production
worker-prod-3: python worker.py production
worker-prod-4: python worker.py production
worker-website-1: python worker.py website
worker-website-2: python worker.py website
worker-website-3: python worker.py website
worker-website-4: python worker.py website
worker-demo: python worker.py demo