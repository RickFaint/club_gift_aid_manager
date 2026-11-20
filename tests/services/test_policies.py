"""Object-level authorization policy tests."""

from club_gift_aid_manager.domain.policies import (
    can_assign_imported_trips,
    can_create_trip_for,
    can_view_person,
    can_view_trip,
    trip_scope_person_id,
)


def user(role, person_id=None):
    """Build the minimal authenticated-actor mapping required by policy functions."""
    return {"role": role, "person_id": person_id}


def test_volunteer_policies_are_limited_to_linked_person():
    """Verify volunteers can operate only on records belonging to their linked person."""
    volunteer = user("volunteer", 7)
    assert can_view_person(volunteer, 7)
    assert can_view_trip(volunteer, {"person_id": 7})
    assert can_create_trip_for(volunteer, 7)
    assert not can_view_person(volunteer, 8)
    assert not can_view_trip(volunteer, {"person_id": 8})
    assert not can_create_trip_for(volunteer, 8)
    assert trip_scope_person_id(volunteer) == 7
    assert not can_assign_imported_trips(volunteer)


def test_administrator_policies_allow_assignment_and_unscoped_queries():
    """Verify administrators retain club-wide visibility and assignment authority."""
    administrator = user("admin")
    assert can_view_person(administrator, 8)
    assert can_view_trip(administrator, {"person_id": 8})
    assert can_create_trip_for(administrator, 8)
    assert trip_scope_person_id(administrator) is None
    assert can_assign_imported_trips(administrator)


def test_treasurer_can_view_but_not_assign_another_person_trip():
    """Verify treasurers may review club records without impersonating another claimant."""
    treasurer = user("treasurer", 7)
    assert can_view_person(treasurer, 8)
    assert can_view_trip(treasurer, {"person_id": 8})
    assert not can_create_trip_for(treasurer, 8)
    assert trip_scope_person_id(treasurer) is None
