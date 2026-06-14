import os
import sys

# Make the package importable and provide a dummy DSN so Settings() loads
# during unit tests that don't touch the database.
sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
