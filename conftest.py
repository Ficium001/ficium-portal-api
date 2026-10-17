import os
import sys

# Make the package importable and provide a dummy DSN so Settings() loads
# during unit tests that don't touch the database.
sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_module_licence_cache():
    """require_module caches licensing per institution; isolate tests."""
    from app import deps
    deps._modules_cache.clear()
    yield
    deps._modules_cache.clear()
