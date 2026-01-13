# worker.py (fixed version)
import os
import redis
import logging
from rq import Worker, Queue
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(name)s | %(levelname)s | %(message)s')
logger = logging.getLogger(__name__)

REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379')
LISTEN_QUEUES = ['default']

def main():
    logger.info("Starting RQ Worker...")

    try:
        redis_conn = redis.from_url(
            REDIS_URL,
            socket_timeout=10,
            socket_connect_timeout=10,
            retry_on_timeout=True
        )
        redis_conn.ping()
        logger.info(f"Redis connected successfully: {REDIS_URL}")

        queues = [Queue(name, connection=redis_conn) for name in LISTEN_QUEUES]

        # Modern way: use context manager (no explicit Connection import needed)
        with redis_conn:
            worker = Worker(
                queues,
                name=f"insurance-grok-worker-{os.getpid()}",
            )
            logger.info(f"Worker listening on: {', '.join(LISTEN_QUEUES)}")
            worker.work(with_scheduler=True)

    except redis.ConnectionError as e:
        logger.critical(f"Redis connection failed: {e}", exc_info=True)
        raise SystemExit(1)
    except Exception as e:
        logger.critical(f"Worker startup failed: {e}", exc_info=True)
        raise SystemExit(1)

if __name__ == '__main__':
    main()