"""Test for unbounded read in _read_secret_file (CB-3389467-9754)."""
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# We need to import the function under test.
# Since _read_secret_file is private, we might need to access it via the module.
from codebot.credentials import _read_secret_file


def test_oversized_secret_file_is_rejected():
    """Verify that _read_secret_file returns empty string for files exceeding 4KB."""
    large_content = "A" * 5000 + "\nSECRET=visible_part"

    with patch('codebot.credentials.Path.home') as mock_home:
        mock_home.return_value = Path(tempfile.gettempdir())

        config_dir = Path(tempfile.gettempdir()) / ".config" / "codebot"
        config_dir.mkdir(parents=True, exist_ok=True)
        secret_file = config_dir / "test_secret.txt"

        try:
            secret_file.write_text(large_content)

            result = _read_secret_file("test_secret")

            assert result == "", f"Expected empty string for oversized file, got {len(result)} chars"
        finally:
            import shutil
            if config_dir.parent.exists():
                shutil.rmtree(config_dir.parent)


def test_normal_secret_file_reads_correctly():
    """Verify that normal small secret files are read correctly."""
    secret_value = "my_super_secret_token"
    
    with patch('codebot.credentials.Path.home') as mock_home:
        mock_home.return_value = Path(tempfile.gettempdir())
        
        config_dir = Path(tempfile.gettempdir()) / ".config" / "codebot"
        config_dir.mkdir(parents=True, exist_ok=True)
        secret_file = config_dir / "normal_secret.txt"
        
        secret_file.write_text(secret_value)
        
        result = _read_secret_file("normal_secret")
        
        assert result == secret_value
        
        # Cleanup
        import shutil
        shutil.rmtree(config_dir.parent)
