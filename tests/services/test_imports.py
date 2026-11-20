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


def test_large_location_import_matches_preview_and_commits_every_valid_row():
    """Previewing a realistic location file must not prevent its later background save."""
    app.config["IMPORTS_SYNCHRONOUS"] = False
    owner = signed_in_client("admin")
    rows = ["name,address,town_city,postcode,home_ground"]
    rows.extend(
        f"Rugby Club {number},Road {number},Town,AB{number % 10} {number:03d},no"
        for number in range(750)
    )
    rows.append("rugby club 0,Duplicate Road,Town,AB0 000,no")
    contents = ("\n".join(rows) + "\n").encode()

    def submit_and_wait(*, preview: bool):
        """Submit fresh multipart bytes and wait for the bounded worker result."""
        endpoint = "/destinations/import?preview=1" if preview else "/destinations/import"
        submitted = owner.post(
            endpoint,
            data={"file": (BytesIO(contents), "locations.csv")},
            headers={"X-CSRF-Token": "test-csrf-token"},
        )
        assert submitted.status_code == 202
        status_url = submitted.get_json()["status_url"]
        for _ in range(1000):
            polled = owner.get(status_url)
            body = polled.get_json()
            assert polled.status_code == 200, body
            if body["status"] in {"completed", "failed"}:
                return body
            time.sleep(0.01)
        raise AssertionError("The background location import did not finish.")

    preview = submit_and_wait(preview=True)
    assert preview["status"] == "completed"
    assert preview["result"]["valid_rows"] == 750
    assert preview["result"]["skipped_duplicate_rows_count"] == 1

    imported = submit_and_wait(preview=False)
    assert imported["status"] == "completed"
    assert imported["http_status"] == 201
    assert imported["result"]["imported"] == 750
    assert imported["result"]["skipped_duplicate_rows_count"] == 1
