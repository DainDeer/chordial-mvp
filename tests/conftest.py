"""global test setup.

Forces DATABASE_URL to a throwaway temp sqlite file *before* anything imports
config/database. Without this, whichever test module imports
`src.database.database` first binds the engine to the real chordial.db (the
default in config.py), and DB-backed tests write their fixtures - e.g.
`tester` users with a fake discord id - straight into production. conftest.py
is imported by pytest before any test module, so setting it here wins the race
globally.
"""
import os
import tempfile

_fd, _path = tempfile.mkstemp(suffix="_test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_path}"
