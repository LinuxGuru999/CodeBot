"""Example module demonstrating proper exception handling.

This module shows how to handle exceptions correctly:
- Specify exception types explicitly
- Log errors before handling
- Re-raise unexpected exceptions
"""
import logging
from typing import Any

logger = logging.getLogger(__name__)

# CB-5664147-0107: Fixed dependency_auditor finding


def process_data(data: Any) -> Any:
    """Process data with proper exception handling.
    
    Args:
        data: Input data to process.
        
    Returns:
        Processed data.
        
    Raises:
        ValueError: If data is invalid.
        RuntimeError: If processing fails unexpectedly.
    """
    try:
        # Simulate processing
        if data is None:
            raise ValueError("Data cannot be None")
        result = str(data).upper()
        return result
    except ValueError as e:
        logger.error("ValueError in process_data: %s", e)
        raise
    except TypeError as e:
        logger.error("TypeError in process_data: %s", e)
        raise
    except Exception as e:
        logger.error("Unexpected error in process_data: %s", e, exc_info=True)
        raise RuntimeError(f"Processing failed: {e}") from e


def safe_operation(operation_name: str) -> bool:
    """Perform a safe operation with proper error handling.
    
    Args:
        operation_name: Name of the operation to perform.
        
    Returns:
        True if operation succeeded, False otherwise.
    """
    try:
        logger.info("Starting operation: %s", operation_name)
        # Simulate operation
        if operation_name == "fail":
            raise RuntimeError("Simulated failure")
        return True
    except RuntimeError as e:
        logger.error("Operation '%s' failed: %s", operation_name, e)
        return False
    except Exception as e:
        logger.error("Unexpected error in operation '%s': %s", operation_name, e, exc_info=True)
        return False
