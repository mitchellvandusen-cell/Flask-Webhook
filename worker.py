# worker.py - Flexible Queue Worker
import os
import redis
import logging
import uuid
import sys
from rq import Worker, Queue

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(name)s | %(levelname)s | %(message)s')
logger = logging.getLogger(__name__)

REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379')

def main():
    # 1. Determine which queue to listen to from command line args
    # Usage: python worker.py production OR python worker.py demo
    if len(sys.argv) > 1:
        listen_queues = sys.argv[1:]
    else:
        listen_queues = ['production'] # Default to production if unspecified

    logger.info(f"Starting Worker for queues: {listen_queues}")

    try:
        redis_conn = redis.from_url(REDIS_URL)
        redis_conn.ping()
    except redis.ConnectionError as e:
        logger.critical(f"Redis connection failed: {e}", exc_info=True)
        raise SystemExit(1)

    unique_id = uuid.uuid4().hex[:8]
    # Name the worker based on the queue it serves for easier debugging
    worker_name = f"worker-{listen_queues[0]}-{unique_id}"
    
    queues = [Queue(name, connection=redis_conn) for name in listen_queues]

    try:
        worker = Worker(
            queues,
            connection=redis_conn,
            name=worker_name
        )
        worker.work()
    except Exception as e:
        logger.critical(f"Worker startup failed: {e}", exc_info=True)
        raise SystemExit(1)

if __name__ == '__main__':
    main()