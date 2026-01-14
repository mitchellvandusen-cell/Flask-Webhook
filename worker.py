# worker.py - Clean, unique-name RQ worker for Railway
import os
import redis
import logging
import uuid
from rq import Worker, Queue

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(name)s | %(levelname)s | %(message)s')
logger = logging.getLogger(__name__)

# Load env (optional if using Railway vars)
REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379')
LISTEN_QUEUES = ['default']

def main():
    logger.info("Starting RQ Worker...")

    # 1. Connect to Redis first (fail fast if can't)
    try:
        redis_conn = redis.from_url(REDIS_URL)
        redis_conn.ping()
        logger.info(f"Redis connected successfully")
    except redis.ConnectionError as e:
        logger.critical(f"Redis connection failed: {e}", exc_info=True)
        raise SystemExit(1)

    # 2. GENERATE UNIQUE NAME (Fixes the crash)
    # We use UUID to ensure even if PID is 1, the name is unique (e.g. worker-a1b2c3d)
    unique_id = uuid.uuid4().hex[:8]
    worker_name = os.getenv('RQ_WORKER_NAME', f"worker-{unique_id}")
    
    logger.info(f"Worker name: {worker_name}")

    # 3. Create queues
    queues = [Queue(name, connection=redis_conn) for name in LISTEN_QUEUES]

    # 4. Start worker
    try:
        worker = Worker(
            queues,
            connection=redis_conn,
            name=worker_name
        )
        logger.info(f"Worker listening on queues: {', '.join(LISTEN_QUEUES)}")
        worker.work()
    except Exception as e:
        logger.critical(f"Worker startup failed: {e}", exc_info=True)
        raise SystemExit(1)

if __name__ == '__main__':
    main()