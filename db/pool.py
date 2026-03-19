# db/pool.py — Connection pool management
#
# Thread-safe PostgreSQL connection pool with semaphore queuing.
# All database operations across the application use get_db_connection()
# and return_db_connection() from this module.

from db_legacy import (
    get_db_connection,
    return_db_connection,
    get_db_connection_with_retry,
    get_pool_stats,
)

__all__ = [
    "get_db_connection",
    "return_db_connection",
    "get_db_connection_with_retry",
    "get_pool_stats",
]
