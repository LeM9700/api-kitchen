"""Service UNIQUE de provisioning tenant.

Utilise par TOUS les parcours de creation de tenant -- inscription standard
(``app/modules/auth/service.py::register``), creation super-admin
(``app/modules/admin/tenants/lifecycle_router.py::create_tenant``), et tout
futur parcours -- pour garantir qu'un tenant a EXACTEMENT la meme structure
quel que soit son parcours de creation.

[SECURITE] ``provision_tenant()`` effectue TOUT (ligne ``public.tenants``,
schema Postgres, tables/contraintes/index applicatifs, donnees minimales,
premier compte admin optionnel) sur une seule connexion, dans une seule
transaction PostgreSQL (``engine.begin()``). PostgreSQL supporte le DDL
transactionnel : ``CREATE SCHEMA`` et ``CREATE TABLE`` sont annules par un
``ROLLBACK`` exactement comme n'importe quel ``INSERT``. Un echec a
N'IMPORTE QUELLE etape (collision de schema, contrainte violee sur le premier
admin...) annule donc TOUT -- aucune compensation applicative (pas de
``DELETE`` manuel en cas d'echec) n'est necessaire ni utilisee ici : c'est
PostgreSQL, pas ce module, qui garantit qu'aucun etat partiel ne peut
subsister.
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.database import Base, engine, tenant_schema_name
from app.core.database import tenant_models  # noqa: F401 -- peuple Base.metadata (tables tenant)
from app.core.http.errors import AppError
from app.core.tenancy.tenant import create_tenant_schema_on
from app.modules.auth.models import User


@dataclass(frozen=True)
class ProvisionedTenant:
    tenant_id: int
    tenant_slug: str
    admin_user_id: int | None


async def _provision_tenant_schema(conn: AsyncConnection, slug: str) -> None:
    """Cree toutes les tables applicatives depuis les modeles SQLAlchemy
    (``Base.metadata``, alimente par ``app.core.database.tenant_models``) --
    meme source de verite que les migrations Alembic -- puis seed les
    donnees applicatives minimales necessaires au fonctionnement du tenant :
    etablissement HR par defaut et referentiel allergenes/tags dietary
    (reglementation UE 1169/2011).

    [SECURITE] Ne remet PAS le search_path a ``public`` en sortie : le
    search_path reste positionne sur le schema tenant, pour que l'appelant
    (``provision_tenant``) puisse encore y creer le premier compte admin SUR
    LA MEME CONNEXION sans jamais repasser -- meme brievement -- par un
    search_path incluant ``public`` (qui contient des tables historiques
    homonymes, voir ci-dessous). C'est la responsabilite de l'appelant de
    remettre le search_path a ``public`` une fois tout le travail tenant
    termine.

    Args:
        conn: Connexion deja ouverte DANS LA TRANSACTION du provisioning --
            jamais une connexion ou transaction separee (voir
            ``provision_tenant`` : c'est cette unicite de connexion/transaction
            qui garantit l'atomicite du provisioning complet).
        slug: Slug deja valide (forme + longueur -- voir
            ``NEW_TENANT_SLUG_RE``/``TENANT_SLUG_MAX_LENGTH_FOR_CREATION``).
    """
    schema = tenant_schema_name(slug)
    # Pas de fallback ", public" ici : Base.metadata.create_all(checkfirst=True)
    # resout les noms de table non qualifies via le search_path. public contient
    # des tables historiques homonymes (users, orders, products...) -- cf.
    # migration 0002 -- donc un fallback public ferait croire a tort que les
    # tables du tenant existent deja et create_all() ne les creerait jamais.
    await conn.execute(text(f'SET search_path TO "{schema}"'))
    await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn))
    await conn.execute(
        text(
            """INSERT INTO establishments (name, timezone)
               SELECT 'Établissement principal', 'Europe/Paris'
               WHERE NOT EXISTS (SELECT 1 FROM establishments)"""
        )
    )
    await conn.execute(
        text(
            """INSERT INTO establishment_hr_config (establishment_id)
               SELECT id FROM establishments
               WHERE id NOT IN (SELECT establishment_id FROM establishment_hr_config)"""
        )
    )

    # Import local : evite de charger tout le module catalog/allergen (et ses
    # propres dependances) au chargement de ce module, pour les appelants qui
    # n'ont besoin que de create_tenant_schema_on/ProvisionedTenant.
    from app.modules.catalog.allergen.allergen_service import seed_regulatory_allergens

    await seed_regulatory_allergens(conn)


async def provision_tenant(
    *,
    slug: str,
    name: str,
    plan: str = "starter",
    admin_fields: dict[str, Any] | None = None,
) -> ProvisionedTenant:
    """Provisionne un tenant complet, de maniere atomique.

    Effectue, sur UNE SEULE connexion et DANS UNE SEULE transaction :
        1. Insertion de la ligne ``public.tenants``.
        2. Creation du schema PostgreSQL (``create_tenant_schema_on`` --
           jamais de reutilisation silencieuse d'un schema existant, voir
           app/core/tenancy/tenant.py).
        3. Creation de toutes les tables/contraintes/index applicatifs +
           donnees minimales (etablissement, allergenes reglementaires).
        4. Creation du premier compte admin, si ``admin_fields`` est fourni.

    Args:
        slug: Slug deja VALIDE par le schema Pydantic de l'appelant
            (``RegisterRequest``, ``TenantCreate``...) -- ce service ne
            revalide pas la forme/longueur : lever l'erreur 422 avant toute
            I/O est la responsabilite de la couche HTTP, pas de ce service.
        name: Nom commercial du tenant.
        plan: Plan tarifaire (defaut "starter").
        admin_fields: Colonnes du premier utilisateur ``User`` a creer dans
            le nouveau schema (email, password_hash, full_name,
            must_change_password, email_verification_token...), ou ``None``
            pour ne creer aucun admin ici. ``role`` vaut "admin" par defaut
            si absent. Chaque appelant fournit les champs propres a son
            parcours (token de verification email pour l'inscription
            standard, ``must_change_password=True`` pour la creation
            super-admin...) -- ce service se contente d'inserer ces colonnes
            dans la MEME transaction que le reste : un email deja pris (ou
            toute autre contrainte violee) fait echouer TOUT le provisioning,
            pas seulement la creation de l'admin.

    Returns:
        ProvisionedTenant(tenant_id, tenant_slug, admin_user_id) --
        ``admin_user_id`` est ``None`` si ``admin_fields`` n'etait pas fourni.

    Raises:
        AppError: TENANT_EXISTS (409) si le slug est deja pris.
        AppError: TENANT_SCHEMA_COLLISION (409) si un schema du meme nom
            existe deja (voir app/core/tenancy/tenant.py).
    """
    async with engine.begin() as conn:
        existing = await conn.scalar(
            text("SELECT id FROM public.tenants WHERE slug = :slug"),
            {"slug": slug},
        )
        if existing:
            raise AppError("TENANT_EXISTS", "Tenant already exists", 409, "tenant_slug")

        tenant_id = await conn.scalar(
            text(
                "INSERT INTO public.tenants (slug, name, plan) "
                "VALUES (:slug, :name, :plan) RETURNING id"
            ),
            {"slug": slug, "name": name, "plan": plan},
        )

        await create_tenant_schema_on(conn, slug)
        await _provision_tenant_schema(conn, slug)

        # [SECURITE] L'admin, si demande, est cree ICI -- avant tout retour a
        # ``public`` -- pendant que le search_path pointe ENCORE uniquement sur
        # le schema tenant (positionne par _provision_tenant_schema ci-dessus,
        # jamais reinitialise entre-temps). Un admin cree APRES un retour au
        # search_path public irait silencieusement s'inserer dans la table
        # ``public.users`` historique (migration 0002) au lieu du schema tenant
        # -- exactement le risque de repli documente dans get_tenant_session.
        admin_user_id = None
        if admin_fields is not None:
            fields = {"role": "admin", **admin_fields}
            stmt = insert(User).values(**fields).returning(User.id)
            admin_user_id = await conn.scalar(stmt)

        # Hygiene de connexion pour le pool : remis a public seulement
        # maintenant que tout le travail tenant-scope est termine.
        await conn.execute(text("SET search_path TO public"))

    return ProvisionedTenant(tenant_id=tenant_id, tenant_slug=slug, admin_user_id=admin_user_id)
