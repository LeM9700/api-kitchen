"""Erreurs internes du module delivery (reseau de livreurs independants).

Reutilise `AppError` (voir `app/core/http/errors.py`) plutot que de definir
une famille d'exceptions separee : cela garde le meme contrat que le reste
du codebase (`app_error_handler` transforme toute `AppError` en payload JSON
uniforme `{code, detail, field}`), meme si aucune route de ce plan ne monte
encore cette erreur cote HTTP.
"""

from app.core.http.errors import AppError


class ForbiddenAuditMetadataError(AppError):
    """Levee quand une metadonnee d'audit non autorisee est passee au helper.

    Le futur reseau de livreurs manipule des donnees sensibles (adresses,
    coordonnees GPS, tokens, contenu de documents d'identite, secrets Stripe
    Connect). Le helper d'audit ne doit jamais pouvoir les journaliser, meme
    par erreur d'appel d'un plan futur -- on refuse donc explicitement toute
    cle hors allowlist plutot que de l'ignorer silencieusement.
    """

    def __init__(self, forbidden_keys: list[str]):
        self.forbidden_keys = list(forbidden_keys)
        super().__init__(
            code="DELIVERY_AUDIT_FORBIDDEN_METADATA",
            detail=(
                "Metadonnees d'audit refusees (cles non autorisees): "
                + ", ".join(sorted(self.forbidden_keys))
            ),
            status_code=400,
        )


class InvalidDeliveryPolygonError(AppError):
    """Levee quand `DeliveryZoneCreate.polygon` ne respecte pas le sous-ensemble
    GeoJSON Polygon strict accepte par le module (voir
    `app.modules.delivery.common.geo.validate_delivery_polygon`).

    Deliberement levee directement (pas un `ValueError` intercepte par
    Pydantic et transforme en `pydantic.ValidationError`) : Pydantic v2 ne
    capture que `ValueError`/`TypeError`/`AssertionError` dans un
    `field_validator` pour les envelopper dans son format de validation
    generique (`{"detail": [{"loc": ..., "msg": ...}]}`, sans code metier).
    En levant une `AppError` (type non intercepte par pydantic-core), elle
    traverse la validation telle quelle et est geree par le handler global
    `app_error_handler` (voir `app/core/http/errors.py`), produisant le
    contrat `{code, detail, field}` standard du projet avec le code metier
    `INVALID_DELIVERY_POLYGON` demande par le plan, plutot que le format
    generique FastAPI/Pydantic.
    """

    def __init__(self, detail: str):
        super().__init__(
            code="INVALID_DELIVERY_POLYGON",
            detail=detail,
            status_code=422,
            field="polygon",
        )
