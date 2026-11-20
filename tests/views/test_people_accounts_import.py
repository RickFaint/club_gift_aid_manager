"""CSV import coverage for people and application accounts."""

from io import BytesIO

from club_gift_aid_manager.db import create_person, list_people, list_users
from tests.helpers import signed_in_client


def _upload(client, path: str, contents: str, filename: str):
    """Post one UTF-8 CSV file with the test client's CSRF token."""
    return client.post(
        path,
        data={"file": (BytesIO(contents.encode("utf-8")), filename)},
        headers={"X-CSRF-Token": "test-csrf-token"},
        content_type="multipart/form-data",
    )


def test_staff_can_import_people_atomically() -> None:
    """A valid people CSV creates every supported contact field."""
    response = _upload(
        signed_in_client("treasurer"),
        "/people/import",
        "name,email,phone,role,active,notes\n"
        "Alex Example,alex@example.org,01234567890,volunteer,yes,Driver\n"
        "Pat Parent,,,parent,no,Emergency contact\n",
        "people.csv",
    )

    assert response.status_code == 201
    assert response.get_json()["imported"] == 2
    people = {person["name"]: person for person in list_people()}
    assert people["Alex Example"]["email"] == "alex@example.org"
    assert people["Pat Parent"]["role"] == "parent"
    assert people["Pat Parent"]["active"] == 0


def test_invalid_people_csv_creates_nothing() -> None:
    """One invalid row rejects the complete people batch."""
    response = _upload(
        signed_in_client(),
        "/people/import",
        "name,email,role\nValid Person,valid@example.org,volunteer\nBad Person,not-email,coach\n",
        "people.csv",
    )

    assert response.status_code == 400
    assert "Row 3" in response.get_json()["error"]
    assert list_people() == []


def test_admin_can_import_linked_accounts_atomically() -> None:
    """Account CSV rows enforce roles and optional person relationships."""
    person = create_person(name="Imported volunteer")
    response = _upload(
        signed_in_client(),
        "/admin/users/import",
        "username,password,role,person_id\n"
        f"csv-volunteer,Secure-CSV-Password1!,volunteer,{person['id']}\n"
        "csv-treasurer,Another-CSV-Password2!,treasurer,\n",
        "accounts.csv",
    )

    assert response.status_code == 201
    assert response.get_json()["imported"] == 2
    users = {user["username"]: user for user in list_users()}
    assert users["csv-volunteer"]["person_id"] == person["id"]
    assert users["csv-treasurer"]["role"] == "treasurer"


def test_invalid_account_row_creates_no_imported_accounts() -> None:
    """A volunteer without a person link rejects the complete account batch."""
    client = signed_in_client()
    before = len(list_users())
    response = _upload(
        client,
        "/admin/users/import",
        "username,password,role,person_id\n"
        "valid-treasurer,Secure-CSV-Password1!,treasurer,\n"
        "invalid-volunteer,Another-CSV-Password2!,volunteer,\n",
        "accounts.csv",
    )

    assert response.status_code == 400
    assert "No accounts were imported" in response.get_json()["error"]
    assert len(list_users()) == before


def test_import_templates_expose_expected_headers() -> None:
    """Staff can download exact headers without copying documentation text."""
    admin = signed_in_client()
    assert admin.get("/people/import/template").get_data(as_text=True) == (
        "name,email,phone,role,active,notes\r\n"
    )
    assert admin.get("/admin/users/import/template").get_data(as_text=True) == (
        "username,password,role,person_id\r\n"
    )
