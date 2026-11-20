"""Architecture tests for models, DTO boundaries, and repository contracts."""

import ast
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError

import club_gift_aid_manager.db as db_module
from club_gift_aid_manager.database import new_session, unit_of_work
from club_gift_aid_manager.dtos import PersonCreateDTO, PersonDTO, UserDTO
from club_gift_aid_manager.models import Base, Person
from club_gift_aid_manager.mfa.repository import SQLAlchemyMFARepository
from club_gift_aid_manager.repositories import (
    SQLAlchemyAuditRepository,
    SQLAlchemyDeclarationRepository,
    SQLAlchemyDestinationRepository,
    SQLAlchemyDonationPaymentRepository,
    SQLAlchemyDonationRepository,
    SQLAlchemyLoginAttemptRepository,
    SQLAlchemyMileageRateRepository,
    SQLAlchemyPaymentRepository,
    SQLAlchemyPaymentTripRepository,
    SQLAlchemyPersonRepository,
    SQLAlchemyPrivacyNoticeRepository,
    SQLAlchemyTripRepository,
    SQLAlchemyUserRepository,
)


def test_every_persistent_table_has_a_sqlalchemy_model_and_repository():
    """The ORM and repository catalog covers the complete existing schema."""
    expected_tables = {
        "people",
        "users",
        "privacy_notice_versions",
        "gift_aid_declarations",
        "destinations",
        "mileage_rate_schedules",
        "trips",
        "donations",
        "payments",
        "donation_payments",
        "payment_trips",
        "audit_events",
        "login_attempts",
        "user_mfa",
    }
    repository_types = {
        SQLAlchemyPersonRepository,
        SQLAlchemyUserRepository,
        SQLAlchemyPrivacyNoticeRepository,
        SQLAlchemyDeclarationRepository,
        SQLAlchemyDestinationRepository,
        SQLAlchemyMileageRateRepository,
        SQLAlchemyTripRepository,
        SQLAlchemyDonationRepository,
        SQLAlchemyPaymentRepository,
        SQLAlchemyDonationPaymentRepository,
        SQLAlchemyPaymentTripRepository,
        SQLAlchemyAuditRepository,
        SQLAlchemyLoginAttemptRepository,
        SQLAlchemyMFARepository,
    }

    assert set(Base.metadata.tables) == expected_tables
    assert len(repository_types) == len(expected_tables)


def test_repository_persists_encrypted_model_and_dto_serializes_it():
    """ORM writes remain encrypted on disk while DTO output is plaintext."""
    with unit_of_work() as session:
        model = SQLAlchemyPersonRepository(session).add(
            Person(
                name="ORM volunteer",
                email="orm@example.test",
                phone=None,
                role="volunteer",
                active=1,
                notes="private",
            )
        )
        person_id = model.id
        response = PersonDTO.model_validate(model).model_dump()

    raw = (
        sqlite3.connect(db_module.DB_PATH)
        .execute("SELECT name, email, notes FROM people WHERE id = ?", (person_id,))
        .fetchone()
    )
    assert raw[0] != "ORM volunteer"
    assert raw[1] != "orm@example.test"
    assert raw[2] != "private"
    assert response["name"] == "ORM volunteer"
    assert response["email"] == "orm@example.test"


def test_request_dto_forbids_unknown_mass_assignment_fields():
    """A client cannot smuggle database-only attributes through a create DTO."""
    with pytest.raises(PydanticValidationError):
        PersonCreateDTO.model_validate({"name": "Valid", "id": 999})


def test_user_response_dto_cannot_expose_credentials():
    """The public user schema has no password hash or blind-index properties."""
    public_fields = set(UserDTO.model_fields)

    assert "password_hash" not in public_fields
    assert "username_bidx" not in public_fields


def test_runtime_domain_functions_do_not_open_raw_sqlite_connections():
    """Keep direct SQLite access confined to connection and schema infrastructure."""
    source_path = Path(db_module.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8-sig"))
    allowed = {"get_connection", "begin_request_transaction", "create_app_database"}
    violations = []
    for function in (node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)):
        opens_connection = any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "get_connection"
            for call in ast.walk(function)
        )
        if opens_connection and function.name not in allowed:
            violations.append(function.name)

    assert violations == []


def test_view_blueprints_do_not_use_wildcard_imports():
    """Keep every Flask Blueprint's collaborators explicit and reviewable."""
    views_directory = Path(db_module.__file__).parent / "views"
    violations = []
    for path in views_directory.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        if any(
            isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names)
            for node in ast.walk(tree)
        ):
            violations.append(path.name)

    assert violations == []
