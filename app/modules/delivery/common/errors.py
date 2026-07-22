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


class TenantRequiredError(AppError):
    """Levee quand `GET /delivery/zones` est appelee sans en-tete `X-Tenant-Slug`.

    Seule cette route lit encore le tenant depuis un header brut (les autres
    routes du module passent par `current_user["tenant_slug"]` via JWT). Le
    plan exige explicitement le code `TENANT_REQUIRED` -- distinct de
    `MISSING_TENANT_SLUG` deja utilise par `customer/router.py` pour une
    route differente -- pour que ce module ait ses propres codes d'erreur
    stables cote client (`app-client`).
    """

    def __init__(self):
        super().__init__(
            code="TENANT_REQUIRED",
            detail="X-Tenant-Slug header is required",
            status_code=400,
            field="X-Tenant-Slug",
        )


class TenantNotFoundError(AppError):
    """Levee quand le slug fourni via `X-Tenant-Slug` ne correspond a aucun
    tenant existant dans `public.tenants`.

    Empeche le fallback silencieux sur un tenant `"default"` inexistant que
    la route `GET /delivery/zones` acceptait auparavant (voir le "Constat
    actuel" du plan). Code `TENANT_NOT_FOUND` explicitement exige par le plan.
    """

    def __init__(self, slug: str):
        super().__init__(
            code="TENANT_NOT_FOUND",
            detail=f"Tenant '{slug}' not found",
            status_code=404,
            field="tenant_slug",
        )


class DeliveryZoneNotFoundError(AppError):
    """Levee quand `PUT`/`DELETE /delivery/zones/{id}` cible une zone absente.

    Avant ce durcissement, `PUT` faisait `session.get(...)` puis
    `setattr(zone, ...)` sans verifier que `zone` n'etait pas `None`, ce qui
    produisait une `AttributeError` non capturee (500) sur un id absent. Le
    plan exige que ce cas remonte systematiquement un 404 metier, jamais un 500.
    """

    def __init__(self, zone_id: int):
        super().__init__(
            code="DELIVERY_ZONE_NOT_FOUND",
            detail=f"Delivery zone {zone_id} not found",
            status_code=404,
        )
