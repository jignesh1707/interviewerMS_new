"""Small helpers for tests that read results the service writes in the background."""

import time

HEADERS = {"X-API-Key": "test-key"}


def wait_for_analyses(client, interview_id, headers=None, count=1, timeout=5.0):
    """The answers of an interview once `count` of them carry their AI analysis (written in the background)."""
    deadline = time.monotonic() + timeout
    while True:
        response = client.get(f"/api/v1/interviews/{interview_id}/answers", headers=headers or HEADERS)
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        if sum(1 for item in items if item.get("analysis")) >= count or time.monotonic() > deadline:
            return items
        time.sleep(0.02)
