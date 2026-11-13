"""Background CSV import submission, polling, and access-control tests."""

from io import BytesIO
import time

from app import app
from tests.helpers import signed_in_client


def test_location_import_runs_as_background_job_and_is_owner_scoped():
    """Normal operation returns 202 and only the submitter can poll the job."""
    app.config["IMPORTS_SYNCHRONOUS"] = False
    owner = signed_in_client("admin")
    submitted = owner.post(
        "/destinations/import",
        data={"file": (BytesIO(b"name,home_ground\nQueued ground,no\n"), "locations.csv")},
        headers={"X-CSRF-Token": "test-csrf-token"},
    )

    assert submitted.status_code == 202
    status_url = submitted.get_json()["status_url"]
    assert signed_in_client("treasurer").get(status_url).status_code == 404

    # The file is tiny, but polling is deliberately tolerant of either queued
    # or running states because thread scheduling differs across platforms.
    for _ in range(100):
        status = owner.get(status_url)
        body = status.get_json()
        if body["status"] in {"completed", "failed"}:
            break
        time.sleep(0.01)

    assert status.status_code == 200
    assert body["status"] == "completed"
    assert body["http_status"] == 201
    assert body["result"]["imported"] == 1
