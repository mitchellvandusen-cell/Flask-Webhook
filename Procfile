web: gunicorn main:app --threads 40 --timeout 0
worker-prod-1: python worker.py production
worker-prod-2: python worker.py production
worker-prod-3: python worker.py production
worker-prod-4: python worker.py production
worker-background-1: python worker.py background
worker-background-2: python worker.py background
worker-demo: python worker.py demo