import logging
import sys
from datetime import datetime

# Setup basic configuration for demonstration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(request_id)s] - [%(user)s] - %(name)s: %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

# Custom logging class to inject extra context easily
class EnhancedLogger:
    def __init__(self, name="my_app", request_id="N/A", user="anonymous"):
        """Initializes the logger with specific contextual data."""
        self._logger = logging.getLogger(name)
        # Set initial extra values for all logs created by this instance
        self._extra = {
            'request_id': request_id,
            'user': user
        }

    def debug(self, message, **kwargs):
        """Logs a debug message with context."""
        extra = self._extra.copy()
        extra.update(kwargs) # Allows overriding default context for specific calls
        self._logger.debug(message, extra=extra)

    def info(self, message, **kwargs):
        """Logs an informational message with context."""
        extra = self._extra.copy()
        extra.update(kwargs)
        self._logger.info(message, extra=extra)

    def error(self, message, exc_info=False, **kwargs):
        """Logs an error message with context and optional exception info."""
        extra = self._extra.copy()
        extra.update(kwargs)
        self._logger.error(message, exc_info=exc_info, extra=extra)

# --- Example Usage ---
def process_request(user_name: str, request_id: str):
    """Simulates handling a user request."""
    print("\n" + "="*30)
    print("Starting Request Processing Simulation")
    logger = EnhancedLogger(name="core.api", request_id=request_id, user=user_name)

    # 1. Debugging initial setup details
    logger.debug("Request started successfully.", component="init")

    try:
        # 2. Info log on core action
        logger.info(f"User '{user_name}' accessing main resource.", endpoint="/api/data")
        
        # Simulate a database interaction failure (requires explicit logging)
        if user_name == "admin":
            raise ConnectionError("Database connection lost.")

        # 3. Success log
        logger.info("Data processed and returned successfully.", duration="50ms", status=200)

    except Exception as e:
        # 4. Error logging with exception details
        logger.error(f"Failed to process request due to error: {e}", exc_info=True, failure_type="Database")


if __name__ == "__main__":
    # Scenario 1: Successful run for a regular user
    process_request("alice", "a9b3d4c5")

    print("\n" + "="*30)

    # Scenario 2: Failure/Error run for admin
    process_request("admin", "f7e8g9h1")