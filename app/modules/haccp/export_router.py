"""Router d'export HACCP — PDF et CSV.

Endpoints admin-only :
  GET /haccp/export/pdf   → rapport PDF complet (WeasyPrint)
  GET /haccp/export/csv   → export CSV par type de données

[🔒 SÉCURITÉ] Ces endpoints sont réservés aux utilisateurs admin
car les documents exportés contiennent l'ensemble des données
HACCP de l'établissement (températures, NC, formations, etc.).
"""

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_tenant_session
from app.core.http.deps import require_role
from app.modules.admin.tenants.models import TenantConfig
from app.modules.haccp.export_service import generate_csv, generate_pdf

router = APIRouter(prefix="/export", tags=["haccp-export"])


async def _get_restaurant_name(db: AsyncSession) -> str:
    """Récupère le nom d'affichage du restaurant depuis tenant_config."""
    config = (await db.execute(select(TenantConfig))).scalars().first()
    if config and config.display_name:
        return config.display_name
    return "Établissement"


@router.get("/pdf", summary="Export rapport HACCP — PDF")
async def export_pdf(
    from_date: date = Query(..., alias="from", description="Date de début (YYYY-MM-DD)"),
    to_date: date = Query(..., alias="to", description="Date de fin (YYYY-MM-DD)"),
    current_user: dict = Depends(require_role("admin")),
) -> Response:
    """Génère le rapport HACCP complet en PDF pour la période donnée.

    Le rapport inclut :
    - Sessions d'ouverture/fermeture avec statut
    - Relevés de température par équipement
    - Vérifications DLC 1/2/3
    - Plan de nettoyage / désinfection
    - Non-conformités et actions correctives
    - Contrôles réception fournisseurs
    - Suivi refroidissement rapide
    - Registre de formation hygiène

    [⚠️ PROD] WeasyPrint est synchrone — exécuté dans un thread pool
    pour ne pas bloquer l'event loop FastAPI.
    """
    if to_date < from_date:
        raise HTTPException(status_code=422, detail="La date de fin doit être >= date de début.")

    async with get_tenant_session(current_user["tenant_slug"]) as db:
        restaurant_name = await _get_restaurant_name(db)
        pdf_bytes = await generate_pdf(db, from_date, to_date, restaurant_name)

    period = f"{from_date.strftime('%Y%m%d')}_{to_date.strftime('%Y%m%d')}"
    filename = f"haccp_{period}.pdf"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(pdf_bytes)),
        },
    )


@router.get("/csv", summary="Export données HACCP — CSV")
async def export_csv(
    from_date: date = Query(..., alias="from", description="Date de début (YYYY-MM-DD)"),
    to_date: date = Query(..., alias="to", description="Date de fin (YYYY-MM-DD)"),
    data_type: Literal["all", "temperatures", "dlc", "nc", "reception", "cooling"] = Query(
        "all", description="Type de données à exporter"
    ),
    current_user: dict = Depends(require_role("admin")),
) -> Response:
    """Exporte les données HACCP au format CSV (UTF-8 BOM, compatible Excel FR).

    Types disponibles :
    - ``all``          → toutes les données dans un seul fichier
    - ``temperatures`` → relevés de température uniquement
    - ``dlc``          → vérifications DLC uniquement
    - ``nc``           → non-conformités uniquement
    - ``reception``    → contrôles réception uniquement
    - ``cooling``      → refroidissement rapide uniquement
    """
    if to_date < from_date:
        raise HTTPException(status_code=422, detail="La date de fin doit être >= date de début.")

    async with get_tenant_session(current_user["tenant_slug"]) as db:
        restaurant_name = await _get_restaurant_name(db)
        filename, csv_bytes = await generate_csv(db, from_date, to_date, restaurant_name, data_type)

    return Response(
        content=csv_bytes,
        media_type="text/csv; charset=utf-8-sig",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(csv_bytes)),
        },
    )
