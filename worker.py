# worker.py - Multi-Process Queue Worker
# Usage:
#   python worker.py production                  # 1 worker on production queue
#   python worker.py website demo                 # 1 worker on website+demo
#   python worker.py --workers=4 production       # 4 workers on production queue
#   python worker.py --workers=3 website demo     # 3 workers on website+demo

import os
import signal
import sys
import logging
import uuid
import multiprocessing
import redis
from dotenv import load_dotenv
from rq import Worker, Queue

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(name)s | %(levelname)s | %(message)s')
logger = logging.getLogger(__name__)

REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379')


def run_worker(listen_queues, worker_num):
    """Run a single RQ worker. Called in each child process."""
    # Each child needs its own DB pool + encryption init
    from db import init_db
    init_db()
    from token_encryption import initialize_encryption
    initialize_encryption()

    try:
        redis_conn = redis.from_url(REDIS_URL)
        redis_conn.ping()
    except redis.ConnectionError as e:
        logger.critical(f"Worker-{worker_num} Redis connection failed: {e}")
        return

    unique_id = uuid.uuid4().hex[:8]
    worker_name = f"worker-{listen_queues[0]}-{worker_num}-{unique_id}"
    queues = [Queue(name, connection=redis_conn) for name in listen_queues]

    try:
        worker = Worker(
            queues,
            connection=redis_conn,
            name=worker_name
        )
        logger.info(f"Worker {worker_name} started (process {worker_num})")
        worker.work()
    except Exception as e:
        logger.critical(f"Worker {worker_name} failed: {e}", exc_info=True)


def main():
    # Parse --workers=N flag
    num_workers = 1
    queue_args = []
    for arg in sys.argv[1:]:
        if arg.startswith('--workers='):
            try:
                num_workers = max(1, int(arg.split('=')[1]))
            except ValueError:
                pass
        else:
            queue_args.append(arg)

    listen_queues = queue_args if queue_args else ['production']

    if num_workers == 1:
        # Single worker — run in main process (backward compatible)
        logger.info(f"Starting Worker for queues: {listen_queues}")
        run_worker(listen_queues, 1)
    else:
        # Multi-worker — fork child processes
        logger.info(f"Starting {num_workers} Workers for queues: {listen_queues}")
        children = []

        for i in range(1, num_workers + 1):
            p = multiprocessing.Process(target=run_worker, args=(listen_queues, i))
            p.start()
            children.append(p)

        # Parent process: forward SIGTERM/SIGINT to all children for clean shutdown
        def shutdown(signum, frame):
            logger.info(f"Received signal {signum}, shutting down {len(children)} workers...")
            for p in children:
                if p.is_alive():
                    p.terminate()
            for p in children:
                p.join(timeout=30)
            sys.exit(0)

        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)

        # Wait for all children — restart any that die unexpectedly
        while True:
            for i, p in enumerate(children):
                p.join(timeout=5)
                if not p.is_alive() and p.exitcode != 0:
                    logger.warning(f"Worker {i+1} died (exit={p.exitcode}), restarting...")
                    new_p = multiprocessing.Process(
                        target=run_worker, args=(listen_queues, i + 1))
                    new_p.start()
                    children[i] = new_p


if __name__ == '__main__':
    main()
