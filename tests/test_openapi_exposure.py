"""Exposition d'OpenAPI en production (Prompt 13).

Constat corrigé :
- Swagger UI (``/docs``) et ReDoc (``/redoc``) étaient déjà désactivés en
  production, mais ``/openapi.json`` (le schéma brut, servi par défaut par
  FastAPI dès qu'une app est construite, indépendamment de ``docs_url``/
  ``redoc_url``) restait exposé publiquement.

Décision appliquée (voir ``app.main.create_app``) : désactiver aussi
``openapi_url`` en production, pour la même raison et avec la même
réserve documentée que ``docs_url``/``redoc_url`` -- réduit l'exposition
DOCUMENTAIRE (liste structurée de routes/schémas prête à l'emploi pour une
reconnaissance automatisée), n'est PAS un contrôle de sécurité suffisant en
soi (l'auth/l'autorisation/la validation métier restent les vrais contrôles).

Ces tests prouvent :
1. en production : ``/openapi.json``, ``/docs`` et ``/redoc`` retournent
   tous les trois 404 (route jamais enregistrée par FastAPI) ;
2. hors production (dev/local/CI, environnement par défaut des tests) :
   ``/openapi.json`` reste servi normalement (schéma valide, exploitable
   par les outils de dev -- Postman, génération de client SDK, etc.).
"""

from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.main import create_app


async def test_openapi_docs_and_redoc_all_disabled_in_production(monkeypatch):
    """Décision volontaire : les trois surfaces documentaires (schéma brut
    inclus) sont coupées en production, pas seulement les UI Swagger/ReDoc."""
    monkeypatch.setattr(settings, "environment", "production")
    prod_app = create_app()

    async with AsyncClient(
        transport=ASGITransport(app=prod_app), base_url="http://test"
    ) as client:
        for path in ("/openapi.json", "/docs", "/redoc"):
            resp = await client.get(path)
            assert resp.status_code == 404, f"{path} devrait etre 404 en production"


async def test_openapi_schema_is_served_outside_production():
    """Hors production (environnement de test par défaut), le schéma
    OpenAPI reste servi normalement -- utile aux outils de développement."""
    assert (settings.environment or "").lower() != "production"
    dev_app = create_app()

    async with AsyncClient(
        transport=ASGITransport(app=dev_app), base_url="http://test"
    ) as client:
        resp = await client.get("/openapi.json")
        assert resp.status_code == 200
        schema = resp.json()
        assert schema["info"]["title"] == "Pizzeria API"

        resp_docs = await client.get("/docs")
        assert resp_docs.status_code == 200
        resp_redoc = await client.get("/redoc")
        assert resp_redoc.status_code == 200
