"""Run the test suite against a throwaway embedded Postgres (local helper; CI uses a service container)."""
import os
import subprocess
import sys
import tempfile

import pgserver

server = pgserver.get_server(tempfile.mkdtemp())
try:
    env = {**os.environ, "TEST_DATABASE_URL": server.get_uri()}
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", "-q", *sys.argv[1:]], env=env))
finally:
    server.cleanup()
