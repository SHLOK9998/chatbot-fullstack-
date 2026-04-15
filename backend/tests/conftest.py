# tests/conftest.py
import pytest

# Makes all async tests in this package use asyncio automatically
pytest_plugins = ["pytest_asyncio"]
