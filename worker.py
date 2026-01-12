# worker.py
import os
import redis
import logging
from rq import Worker, Queue
from dotenv import load_dotenv

# Load env vars
load_dotenv()

# Setup Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s | WORKER | %(levelname)s | %(message)s')
logger = logging.getLogger(__name__)

listen = ['default']

# Get Redis URL
redis_url = os.getenv('REDIS_URL', 'redis://localhost:6379')

if __name__ == '__main__':
    try:
        # Establish connection
        conn = redis.from_url(redis_url)
        
        # Initialize Queues
        queues = [Queue(name, connection=conn) for name in listen]
        
        logger.info("✅ WORKER STARTED: Listening for jobs...")
        
        # Start the worker with the connection explicitly passed
        worker = Worker(queues, connection=conn)
        worker.work()
        
    except Exception as e:
        logger.error(f"❌ WORKER FAILED TO START: {e}")