"""The committed OpenAPI file is the contract the main app is built against; it must match the running app."""

import json
from pathlib import Path

from app.main import app

SNAPSHOT = Path(__file__).resolve().parents[3] / "docs" / "docs" / "openapi.json"


def test_openapi_snapshot_matches_the_app():
    committed = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert committed == app.openapi(), (
        "docs/docs/openapi.json is out of date. Regenerate it from backend/backend with:\n"
        "python -c \"import json; from app.main import app; "
        "open('../../docs/docs/openapi.json','w',newline='\n').write(json.dumps(app.openapi(), indent=2, sort_keys=True)+'\n')\""
    )
