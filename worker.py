# worker.py - RQ Background Worker (Flawless 2026)
import os
import redis
import logging
from rq import Worker, Queue, Connection
from dotenv import load_dotenv
from rq.logutils import setup_loghandlers

# === Load Environment ===
load_dotenv()

# === Logging (structured, production-ready) ===
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(name)s | %(levelname)s | %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Optional: RQ native logging (uncomment if you want RQ debug output)
# setup_loghandlers(level=logging.INFO)

# === Configuration ===
REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379')
LISTEN_QUEUES = ['default']  # Add more queues here if needed (e.g., ['high', 'low'])

def main():
    """
    Starts the RQ worker, listening on specified queues.
    Resilient to Redis disconnects, logs clearly.
    """
    logger.info("Starting RQ Worker...")

    try:
        # Redis connection with timeout & health check
        redis_conn = redis.from_url(
            REDIS_URL,
            socket_timeout=10,
            socket_connect_timeout=10,
            retry_on_timeout=True
        )

        # Health check (fail fast if Redis is down)
        redis_conn.ping()
        logger.info(f"Redis connected successfully: {REDIS_URL}")

        # Create queues
        queues = [Queue(name, connection=redis_conn) for name in LISTEN_QUEUES]

        # Start worker with connection context
        with Connection(redis_conn):
            worker = Worker(
                queues,
                name=f"insurance-grok-worker-{os.getpid()}",
                default_worker_ttl=600,       # 10 min TTL for stuck jobs
            )

            logger.info(f"Worker listening on queues: {', '.join(LISTEN_QUEUES)}")
            worker.work(with_scheduler=True)  # Enable scheduler if you use scheduled jobs

    except redis.ConnectionError as e:
        logger.critical(f"Redis connection failed: {e}", exc_info=True)
        raise SystemExit(1)
    except Exception as e:
        logger.critical(f"Worker startup failed: {e}", exc_info=True)
        raise SystemExit(1)

if __name__ == '__main__':
    main()