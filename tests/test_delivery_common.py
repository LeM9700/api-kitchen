"""Tests unitaires des conventions communes du reseau de livreurs independants.

Ce module ne depend d'aucune base de donnees : les enums et le helper
d'audit de `app.modules.delivery.common` sont des utilitaires purs (Plan 01,
Tache 1). Le reseau reste desactive par defaut
(`settings.delivery_network_enabled = False`) et aucune route n'est montee ici.
"""

import logging

import pytest

from app.core.config.settings import settings
from app.core.http.errors import AppError
from app.modules.delivery.common.audit import (
    ALLOWED_AUDIT_METADATA_KEYS,
    record_delivery_audit_event,
)
from app.modules.delivery.common.enums import (
    DeliveryAuditActor,
    DeliveryCheckoutStatus,
    DeliveryHandoffMode,
    DeliveryRequestStatus,
    DeliveryVehicleType,
)
from app.modules.delivery.common.errors import ForbiddenAuditMetadataError


def test_delivery_network_disabled_by_default():
    # Le plan exige explicitement que le reseau reste desactive par defaut
    # tant que les fondations (ce plan) n'ont pas ete completees.
    assert settings.delivery_network_enabled is False


def test_enums_are_string_enums_without_native_pg_type():
    # Contrainte du plan : pas d'ENUM Postgres natif, uniquement des
    # `str, Enum` Python cote application.
    for enum_cls in (
        DeliveryHandoffMode,
        DeliveryCheckoutStatus,
        DeliveryRequestStatus,
        DeliveryVehicleType,
        DeliveryAuditActor,
    ):
        assert issubclass(enum_cls, str)
        members = list(enum_cls)
        assert members, f"{enum_cls.__name__} must not be empty"
        for member in members:
            assert isinstance(member.value, str)


def test_record_delivery_audit_event_accepts_clean_metadata(caplog):
    caplog.set_level(logging.INFO, logger="app.modules.delivery.common.audit")

    record_delivery_audit_event(
        actor=DeliveryAuditActor.ADMIN,
        actor_id=42,
        entity_type="delivery_request",
        entity_id=7,
        transition="pending->accepted",
        metadata={"reason": "manual override", "previous_status": "pending"},
    )

    assert any("delivery_audit_event" in record.message for record in caplog.records)


def test_record_delivery_audit_event_accepts_no_metadata():
    # metadata est optionnel : ne doit pas exiger un dict vide explicite.
    record_delivery_audit_event(
        actor=DeliveryAuditActor.SYSTEM,
        actor_id=None,
        entity_type="delivery_request",
        entity_id=1,
        transition="pending->expired",
    )


@pytest.mark.parametrize(
    "forbidden_key",
    [
        "address",
        "adresse",
        "gps",
        "lat",
        "lng",
        "gps_coordinates",
        "token",
        "access_token",
        "document_content",
        "id_document",
        "stripe_secret",
        "stripe_secret_key",
        "iban",
        "password",
    ],
)
def test_record_delivery_audit_event_rejects_forbidden_metadata_keys(forbidden_key):
    with pytest.raises((ForbiddenAuditMetadataError, AppError)):
        record_delivery_audit_event(
            actor=DeliveryAuditActor.COURIER,
            actor_id=1,
            entity_type="delivery_request",
            entity_id=1,
            transition="pending->accepted",
            metadata={forbidden_key: "should never be logged"},
        )


def test_record_delivery_audit_event_rejects_mixed_clean_and_forbidden_keys():
    with pytest.raises(ForbiddenAuditMetadataError) as exc_info:
        record_delivery_audit_event(
            actor=DeliveryAuditActor.COURIER,
            actor_id=1,
            entity_type="delivery_request",
            entity_id=1,
            transition="pending->accepted",
            metadata={"reason": "ok", "gps_lat": 48.85},
        )
    assert "gps_lat" in exc_info.value.forbidden_keys


def test_forbidden_audit_metadata_error_is_an_app_error():
    error = ForbiddenAuditMetadataError(["token"])
    assert isinstance(error, AppError)
    assert error.status_code == 400
    assert error.code == "DELIVERY_AUDIT_FORBIDDEN_METADATA"


def test_allowlist_does_not_contain_sensitive_looking_keys():
    sensitive_markers = ("address", "adresse", "gps", "lat", "lng", "token", "document", "stripe", "iban", "password")
    for key in ALLOWED_AUDIT_METADATA_KEYS:
        lowered = key.lower()
        assert not any(marker in lowered for marker in sensitive_markers), key
