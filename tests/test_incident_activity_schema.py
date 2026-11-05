from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.schemas import IncidentActivityCreate


@pytest.mark.parametrize(
    "activity_type",
    ["observation", "situation_update", "resource_request", "access_constraint", "handover", "note"],
)
def test_supported_incident_activity_types(activity_type: str) -> None:
    payload = IncidentActivityCreate(
        activity_type=activity_type,
        message="Responder update with enough meaningful content.",
    )
    assert payload.activity_type == activity_type


@pytest.mark.parametrize("activity_type", ["", "dispatch", "status_change", "incident_close", "<script>"])
def test_unsupported_incident_activity_types_are_rejected(activity_type: str) -> None:
    with pytest.raises(ValidationError):
        IncidentActivityCreate(activity_type=activity_type, message="This message is long enough.")


def test_activity_message_is_trimmed_and_rejects_whitespace_only() -> None:
    payload = IncidentActivityCreate(
        activity_type="note", message="  Field team checked the west entrance.  "
    )
    assert payload.message == "Field team checked the west entrance."
    with pytest.raises(ValidationError):
        IncidentActivityCreate(activity_type="note", message="             ")


def test_activity_coordinates_must_be_a_valid_pair() -> None:
    with pytest.raises(ValidationError):
        IncidentActivityCreate(
            activity_type="observation", message="Observed debris near the crossing.", latitude=23.0
        )
    with pytest.raises(ValidationError):
        IncidentActivityCreate(
            activity_type="observation", message="Observed debris near the crossing.",
            latitude=91.0, longitude=72.0,
        )
    payload = IncidentActivityCreate(
        activity_type="observation", message="Observed debris near the crossing.",
        latitude=23.0, longitude=72.0,
    )
    assert payload.latitude == 23.0 and payload.longitude == 72.0


def test_activity_location_normalizes_blank_to_null() -> None:
    payload = IncidentActivityCreate(
        activity_type="note", message="Road access remains limited at this time.", location_text="   "
    )
    assert payload.location_text is None


def test_activity_rejects_null_bytes_that_postgresql_text_cannot_store() -> None:
    with pytest.raises(ValidationError):
        IncidentActivityCreate(
            activity_type="note", message="A meaningful note with a null byte \x00 inside."
        )
    with pytest.raises(ValidationError):
        IncidentActivityCreate(
            activity_type="note", message="A meaningful incident note.", location_text="Sector 4\x00"
        )
