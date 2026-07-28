import base64
import hmac as _hmac_module
from io import BytesIO
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.core.config import settings
from app.core.database import engine, get_public_session, get_tenant_session, tenant_schema_name
from app.core.http.deps import get_client_ip
from app.core.http.errors import AppError
from app.core.auth.security import (
    DUMMY_HASH,
    compute_token_lookup,
    create_access_token,
    create_refresh_token,
    decode_token,
    get_password_hash,
    verify_password,
)
from app.core.tenancy.tenant import create_tenant_schema
from app.modules.auth.models import RefreshToken, User

# DDL repliques depuis la migration 0002_tenant_tables + colonne token_lookup (0003).
# Cette liste est la source de verite pour le provisioning des nouveaux tenants.
# [PROD] Tout changement de schema doit etre repercute ici ET dans une migration Alembic.
_TENANT_DDL_STATEMENTS: list[str] = [
    """CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        email VARCHAR(255) NOT NULL,
        password_hash VARCHAR(255) NOT NULL,
        full_name VARCHAR(255),
        phone VARCHAR(20),
        role VARCHAR(32) NOT NULL DEFAULT 'customer',
        permissions JSON,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        email_verification_token VARCHAR(64) UNIQUE,
        email_verification_expires_at TIMESTAMPTZ,
        email_verified_at TIMESTAMPTZ,
        password_reset_token VARCHAR(64) UNIQUE,
        password_reset_expires_at TIMESTAMPTZ,
        must_change_password BOOLEAN NOT NULL DEFAULT FALSE,
        mfa_secret VARCHAR(255),
        mfa_enabled BOOLEAN NOT NULL DEFAULT FALSE,
        mfa_backup_codes JSON,
        CONSTRAINT uq_users_email UNIQUE (email)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_users_email ON users (email)",
    "CREATE INDEX IF NOT EXISTS ix_users_verification_token ON users (email_verification_token)",
    "CREATE INDEX IF NOT EXISTS ix_users_password_reset_token ON users (password_reset_token)",
    """CREATE TABLE IF NOT EXISTS refresh_tokens (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        token_hash VARCHAR(255) NOT NULL,
        token_lookup VARCHAR(64) UNIQUE,
        expires_at TIMESTAMPTZ NOT NULL,
        revoked_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        user_agent VARCHAR(512),
        ip_address VARCHAR(45)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_refresh_tokens_token_hash ON refresh_tokens (token_hash)",
    "CREATE INDEX IF NOT EXISTS ix_refresh_tokens_token_lookup ON refresh_tokens (token_lookup)",
    """CREATE TABLE IF NOT EXISTS tenant_config (
        id SERIAL PRIMARY KEY,
        is_temporarily_closed BOOLEAN NOT NULL DEFAULT FALSE,
        temporary_closure_message TEXT,
        default_closure_message TEXT NOT NULL DEFAULT 'Nous sommes temporairement fermes. Nous vous accueillons bientot !',
        prep_time_normal_minutes INTEGER NOT NULL DEFAULT 25,
        prep_time_peak_minutes INTEGER NOT NULL DEFAULT 45,
        peak_orders_threshold INTEGER NOT NULL DEFAULT 5,
        auto_calc_prep_time BOOLEAN NOT NULL DEFAULT TRUE,
        overhead_per_order_minutes INTEGER NOT NULL DEFAULT 3,
        timezone VARCHAR(64) NOT NULL DEFAULT 'Europe/Paris',
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        scheduled_close_at TIMESTAMPTZ,
        stock_alert_cooldown_hours INTEGER NOT NULL DEFAULT 4,
        large_stock_adjustment_threshold NUMERIC(12,3) NOT NULL DEFAULT 10,
        print_enabled BOOLEAN NOT NULL DEFAULT FALSE,
        print_config JSON,
        display_name VARCHAR(120),
        logo_url TEXT,
        primary_color VARCHAR(7),
        secondary_color VARCHAR(7),
        font_family VARCHAR(50)
    )""",
    """CREATE TABLE IF NOT EXISTS business_hours (
        id SERIAL PRIMARY KEY,
        day_of_week INTEGER NOT NULL,
        slot_index INTEGER NOT NULL,
        opens_at TIME NOT NULL,
        closes_at TIME NOT NULL,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        CONSTRAINT ck_business_hours_closes_after_opens CHECK (closes_at > opens_at)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_business_hours_day_slot ON business_hours (day_of_week, slot_index)",
    """CREATE TABLE IF NOT EXISTS exceptional_closures (
        id SERIAL PRIMARY KEY,
        closure_date DATE NOT NULL UNIQUE,
        custom_message TEXT,
        use_default_message BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    "CREATE INDEX IF NOT EXISTS ix_exceptional_closures_date ON exceptional_closures (closure_date)",
    """CREATE TABLE IF NOT EXISTS tenant_config_audits (
        id SERIAL PRIMARY KEY,
        changed_by_user_id INTEGER NOT NULL,
        user_email VARCHAR(255),
        changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        field_name VARCHAR(255) NOT NULL,
        old_value TEXT,
        new_value TEXT,
        ip_address VARCHAR(45),
        user_agent TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS ix_tenant_config_audits_changed_at ON tenant_config_audits (changed_at)",
    """CREATE TABLE IF NOT EXISTS categories (
        id SERIAL PRIMARY KEY,
        name VARCHAR(128) NOT NULL,
        display_order INTEGER NOT NULL DEFAULT 0,
        preparation_station VARCHAR(16) NOT NULL DEFAULT 'kitchen',
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        CONSTRAINT ck_categories_preparation_station CHECK (preparation_station IN ('kitchen', 'counter', 'none'))
    )""",
    """CREATE TABLE IF NOT EXISTS products (
        id SERIAL PRIMARY KEY,
        category_id INTEGER REFERENCES categories(id),
        name VARCHAR(255) NOT NULL,
        description TEXT,
        base_price NUMERIC(10,2) NOT NULL,
        image_url VARCHAR(512),
        preparation_station VARCHAR(16),
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        is_featured BOOLEAN NOT NULL DEFAULT FALSE,
        CONSTRAINT ck_products_preparation_station CHECK (preparation_station IS NULL OR preparation_station IN ('kitchen', 'counter', 'none'))
    )""",
    """CREATE TABLE IF NOT EXISTS product_availability_overrides (
        id SERIAL PRIMARY KEY,
        product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
        available BOOLEAN NOT NULL,
        reason TEXT,
        changed_by_user_id INTEGER NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    "CREATE INDEX IF NOT EXISTS ix_product_availability_overrides_product_created ON product_availability_overrides (product_id, created_at)",
    """CREATE TABLE IF NOT EXISTS product_variants (
        id SERIAL PRIMARY KEY,
        product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
        name VARCHAR(128) NOT NULL,
        price_delta NUMERIC(10,2) NOT NULL,
        is_active BOOLEAN NOT NULL DEFAULT TRUE
    )""",
    """CREATE TABLE IF NOT EXISTS extras (
        id SERIAL PRIMARY KEY,
        name VARCHAR(128) NOT NULL,
        price NUMERIC(10,2) NOT NULL,
        is_active BOOLEAN NOT NULL DEFAULT TRUE
    )""",
    """CREATE TABLE IF NOT EXISTS product_extras (
        product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
        extra_id INTEGER NOT NULL REFERENCES extras(id) ON DELETE CASCADE,
        PRIMARY KEY (product_id, extra_id)
    )""",
    """CREATE TABLE IF NOT EXISTS media_images (
        id SERIAL PRIMARY KEY,
        entity_type VARCHAR(32) NOT NULL,
        entity_id INTEGER NOT NULL,
        cloudinary_public_id VARCHAR(256) NOT NULL UNIQUE,
        url VARCHAR(512) NOT NULL,
        url_thumbnail VARCHAR(512) NOT NULL,
        url_medium VARCHAR(512) NOT NULL,
        format VARCHAR(10) NOT NULL,
        size_bytes INTEGER NOT NULL,
        width INTEGER NOT NULL,
        height INTEGER NOT NULL,
        is_primary BOOLEAN NOT NULL DEFAULT FALSE,
        display_order INTEGER NOT NULL DEFAULT 0,
        alt_text VARCHAR(256),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    "CREATE INDEX IF NOT EXISTS ix_media_images_entity ON media_images (entity_type, entity_id)",
    """CREATE TABLE IF NOT EXISTS allergen_definitions (
        id SERIAL PRIMARY KEY,
        name VARCHAR(128) NOT NULL,
        slug VARCHAR(64) NOT NULL UNIQUE,
        is_regulatory BOOLEAN NOT NULL DEFAULT FALSE,
        description TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    "CREATE INDEX IF NOT EXISTS ix_allergen_definitions_slug ON allergen_definitions (slug)",
    """CREATE TABLE IF NOT EXISTS ingredients (
        id SERIAL PRIMARY KEY,
        name VARCHAR(128) NOT NULL,
        unit VARCHAR(32) NOT NULL,
        current_qty NUMERIC(12,3) NOT NULL,
        alert_threshold NUMERIC(12,3) NOT NULL,
        last_alert_sent_at TIMESTAMPTZ
    )""",
    """CREATE TABLE IF NOT EXISTS ingredient_allergens (
        ingredient_id INTEGER NOT NULL REFERENCES ingredients(id) ON DELETE CASCADE,
        allergen_id INTEGER NOT NULL REFERENCES allergen_definitions(id) ON DELETE CASCADE,
        level VARCHAR(10) NOT NULL,
        PRIMARY KEY (ingredient_id, allergen_id),
        CONSTRAINT ck_ingredient_allergen_level CHECK (level IN ('present', 'traces', 'absent'))
    )""",
    """CREATE TABLE IF NOT EXISTS product_allergens (
        product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
        allergen_id INTEGER NOT NULL REFERENCES allergen_definitions(id) ON DELETE CASCADE,
        level VARCHAR(10) NOT NULL,
        source VARCHAR(10) NOT NULL DEFAULT 'ingredient',
        PRIMARY KEY (product_id, allergen_id),
        CONSTRAINT ck_product_allergen_level CHECK (level IN ('present', 'traces', 'absent')),
        CONSTRAINT ck_product_allergen_source CHECK (source IN ('ingredient', 'manual'))
    )""",
    """CREATE TABLE IF NOT EXISTS dietary_tags (
        id SERIAL PRIMARY KEY,
        name VARCHAR(128) NOT NULL,
        slug VARCHAR(64) NOT NULL UNIQUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    "CREATE INDEX IF NOT EXISTS ix_dietary_tags_slug ON dietary_tags (slug)",
    """CREATE TABLE IF NOT EXISTS product_dietary_tags (
        product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
        dietary_tag_id INTEGER NOT NULL REFERENCES dietary_tags(id) ON DELETE CASCADE,
        PRIMARY KEY (product_id, dietary_tag_id)
    )""",
    """CREATE TABLE IF NOT EXISTS allergen_change_audits (
        id SERIAL PRIMARY KEY,
        product_id INTEGER NOT NULL,
        allergen_id INTEGER NOT NULL,
        changed_by_user_id INTEGER NOT NULL,
        changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        old_level VARCHAR(10),
        new_level VARCHAR(10) NOT NULL,
        old_source VARCHAR(10),
        new_source VARCHAR(10) NOT NULL,
        ip_address VARCHAR(45),
        reason TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS ix_allergen_change_audits_product_changed ON allergen_change_audits (product_id, changed_at)",
    """CREATE TABLE IF NOT EXISTS extra_ingredients (
        id SERIAL PRIMARY KEY,
        extra_id INTEGER NOT NULL REFERENCES extras(id) ON DELETE CASCADE,
        ingredient_id INTEGER NOT NULL REFERENCES ingredients(id),
        quantity NUMERIC(12,3) NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS ix_extra_ingredients_extra ON extra_ingredients (extra_id)",
    "CREATE INDEX IF NOT EXISTS ix_extra_ingredients_ingredient ON extra_ingredients (ingredient_id)",
    """CREATE TABLE IF NOT EXISTS product_recommendations (
        id SERIAL PRIMARY KEY,
        product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
        recommended_product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
        display_order INTEGER NOT NULL DEFAULT 0,
        label VARCHAR(128),
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        CONSTRAINT uq_product_recommendations_pair UNIQUE (product_id, recommended_product_id)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_product_recommendations_product ON product_recommendations (product_id)",
    "CREATE INDEX IF NOT EXISTS ix_product_recommendations_recommended ON product_recommendations (recommended_product_id)",
    """CREATE TABLE IF NOT EXISTS catalog_price_audits (
        id SERIAL PRIMARY KEY,
        entity_type VARCHAR(16) NOT NULL,
        entity_id INTEGER NOT NULL,
        old_price NUMERIC(10,2),
        new_price NUMERIC(10,2) NOT NULL,
        changed_by_user_id INTEGER,
        source VARCHAR(16) NOT NULL,
        reason TEXT,
        changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    "CREATE INDEX IF NOT EXISTS ix_catalog_price_audits_entity ON catalog_price_audits (entity_type, entity_id, changed_at)",
    """CREATE TABLE IF NOT EXISTS catalog_import_batches (
        id SERIAL PRIMARY KEY,
        token VARCHAR(64) NOT NULL UNIQUE,
        filename VARCHAR(255),
        csv_text TEXT NOT NULL,
        status VARCHAR(16) NOT NULL DEFAULT 'dry_run',
        validation_report JSON NOT NULL,
        created_by_user_id INTEGER,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    """CREATE TABLE IF NOT EXISTS orders (
        id SERIAL PRIMARY KEY,
        user_id INTEGER,
        customer_email VARCHAR(255),
        customer_name VARCHAR(255),
        customer_phone VARCHAR(32),
        order_type VARCHAR(16) NOT NULL DEFAULT 'delivery',
        status VARCHAR(32) NOT NULL DEFAULT 'pending',
        payment_status VARCHAR(32) NOT NULL DEFAULT 'pending',
        source VARCHAR(16) NOT NULL DEFAULT 'customer',
        created_by_user_id INTEGER,
        subtotal NUMERIC(10,2) NOT NULL,
        discount_total NUMERIC(10,2) NOT NULL,
        delivery_fee NUMERIC(10,2) NOT NULL,
        total NUMERIC(10,2) NOT NULL,
        delivery_address TEXT,
        delivery_zone_id INTEGER,
        table_number VARCHAR(32),
        estimated_delivery_at TIMESTAMPTZ,
        idempotency_key VARCHAR(128),
        promo_code VARCHAR(64),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT ck_orders_order_type CHECK (order_type IN ('delivery', 'pickup', 'dine_in')),
        CONSTRAINT ck_orders_source CHECK (source IN ('customer', 'manual', 'system')),
        CONSTRAINT uq_orders_user_id_idempotency_key UNIQUE (user_id, idempotency_key)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_orders_user_id ON orders (user_id)",
    "CREATE INDEX IF NOT EXISTS ix_orders_status ON orders (status)",
    "CREATE INDEX IF NOT EXISTS ix_orders_created_at ON orders (created_at)",
    """CREATE TABLE IF NOT EXISTS order_items (
        id SERIAL PRIMARY KEY,
        order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
        product_id INTEGER NOT NULL,
        variant_id INTEGER,
        product_name_snapshot VARCHAR(255),
        variant_name_snapshot VARCHAR(128),
        extras_snapshot JSON,
        extras_total NUMERIC(10,2) NOT NULL DEFAULT 0,
        quantity INTEGER NOT NULL,
        unit_price NUMERIC(10,2) NOT NULL,
        total NUMERIC(10,2) NOT NULL,
        preparation_status VARCHAR(16) NOT NULL DEFAULT 'pending',
        preparation_station VARCHAR(16) NOT NULL DEFAULT 'kitchen',
        prepared_at TIMESTAMPTZ,
        prepared_by_user_id INTEGER,
        CONSTRAINT ck_order_items_preparation_status CHECK (preparation_status IN ('pending', 'preparing', 'ready')),
        CONSTRAINT ck_order_items_preparation_station CHECK (preparation_station IN ('kitchen', 'counter', 'none'))
    )""",
    """CREATE TABLE IF NOT EXISTS order_status_history (
        id SERIAL PRIMARY KEY,
        order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
        status VARCHAR(32) NOT NULL,
        note TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    """CREATE TABLE IF NOT EXISTS payments (
        id SERIAL PRIMARY KEY,
        order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
        provider VARCHAR(32) NOT NULL,
        provider_payment_id VARCHAR(255),
        provider_account_id VARCHAR(255),
        external_reference VARCHAR(255),
        amount NUMERIC(10,2) NOT NULL,
        amount_received NUMERIC(10,2),
        currency VARCHAR(8) NOT NULL,
        status VARCHAR(32) NOT NULL,
        expires_at TIMESTAMPTZ,
        created_by_user_id INTEGER,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    "CREATE INDEX IF NOT EXISTS ix_payments_provider_payment_id ON payments (provider_payment_id)",
    """CREATE TABLE IF NOT EXISTS refunds (
        id SERIAL PRIMARY KEY,
        order_id INTEGER NOT NULL REFERENCES orders(id),
        payment_id INTEGER NOT NULL REFERENCES payments(id),
        stripe_refund_id VARCHAR(128) NOT NULL UNIQUE,
        amount INTEGER NOT NULL,
        reason VARCHAR(256),
        status VARCHAR(32) NOT NULL DEFAULT 'pending',
        failure_reason VARCHAR(512),
        created_by_user_id INTEGER,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    "CREATE INDEX IF NOT EXISTS ix_refunds_order_id ON refunds (order_id)",
    "CREATE INDEX IF NOT EXISTS ix_refunds_payment_id ON refunds (payment_id)",
    """CREATE TABLE IF NOT EXISTS product_ingredients (
        id SERIAL PRIMARY KEY,
        product_id INTEGER NOT NULL,
        ingredient_id INTEGER NOT NULL REFERENCES ingredients(id),
        quantity NUMERIC(12,3) NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS variant_ingredients (
        id SERIAL PRIMARY KEY,
        variant_id INTEGER NOT NULL,
        ingredient_id INTEGER NOT NULL REFERENCES ingredients(id),
        quantity NUMERIC(12,3) NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS stock_movements (
        id SERIAL PRIMARY KEY,
        ingredient_id INTEGER NOT NULL REFERENCES ingredients(id),
        quantity_delta NUMERIC(12,3) NOT NULL,
        reason VARCHAR(64) NOT NULL,
        user_id INTEGER,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    """CREATE TABLE IF NOT EXISTS ingredient_batches (
        id SERIAL PRIMARY KEY,
        ingredient_id INTEGER NOT NULL REFERENCES ingredients(id),
        quantity NUMERIC(12,3) NOT NULL,
        received_at TIMESTAMPTZ NOT NULL,
        expires_at TIMESTAMPTZ,
        opened_at TIMESTAMPTZ,
        use_within_hours_after_opening INTEGER,
        status VARCHAR(16) NOT NULL DEFAULT 'sealed',
        created_by_user_id INTEGER,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT ck_ingredient_batches_status CHECK (status IN ('sealed', 'opened', 'expired', 'consumed', 'discarded'))
    )""",
    "CREATE INDEX IF NOT EXISTS ix_ingredient_batches_ingredient ON ingredient_batches (ingredient_id)",
    "CREATE INDEX IF NOT EXISTS ix_ingredient_batches_expires_at ON ingredient_batches (expires_at)",
    """CREATE TABLE IF NOT EXISTS stock_adjustment_requests (
        id SERIAL PRIMARY KEY,
        ingredient_id INTEGER NOT NULL REFERENCES ingredients(id),
        quantity_delta NUMERIC(12,3) NOT NULL,
        reason VARCHAR(64) NOT NULL,
        note TEXT,
        status VARCHAR(16) NOT NULL DEFAULT 'pending',
        requested_by_user_id INTEGER NOT NULL,
        reviewed_by_user_id INTEGER,
        reviewed_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT ck_stock_adjustment_requests_status CHECK (status IN ('pending', 'approved', 'rejected'))
    )""",
    "CREATE INDEX IF NOT EXISTS ix_stock_adjustment_requests_status ON stock_adjustment_requests (status)",
    "CREATE INDEX IF NOT EXISTS ix_stock_adjustment_requests_ingredient ON stock_adjustment_requests (ingredient_id)",
    """CREATE TABLE IF NOT EXISTS product_stock (
        id SERIAL PRIMARY KEY,
        product_id INTEGER NOT NULL,
        available_qty INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS loyalty_accounts (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL,
        points INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT uq_loyalty_accounts_user_id UNIQUE (user_id)
    )""",
    """CREATE TABLE IF NOT EXISTS loyalty_transactions (
        id SERIAL PRIMARY KEY,
        account_id INTEGER NOT NULL REFERENCES loyalty_accounts(id) ON DELETE CASCADE,
        points_delta INTEGER NOT NULL,
        reason VARCHAR(64) NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    """CREATE TABLE IF NOT EXISTS promotion_campaigns (
        id SERIAL PRIMARY KEY,
        name VARCHAR(128) NOT NULL,
        prefix VARCHAR(32) NOT NULL,
        description VARCHAR(255),
        created_by_user_id INTEGER,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    """CREATE TABLE IF NOT EXISTS promotions (
        id SERIAL PRIMARY KEY,
        code VARCHAR(64) NOT NULL,
        description VARCHAR(255),
        discount_type VARCHAR(16) NOT NULL,
        discount_value NUMERIC(10,2) NOT NULL,
        min_order_amount NUMERIC(10,2) NOT NULL,
        starts_at TIMESTAMPTZ,
        ends_at TIMESTAMPTZ,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        max_uses INTEGER,
        max_uses_per_user INTEGER,
        current_uses INTEGER NOT NULL DEFAULT 0,
        first_order_only BOOLEAN NOT NULL DEFAULT FALSE,
        campaign_id INTEGER REFERENCES promotion_campaigns(id) ON DELETE SET NULL,
        user_id INTEGER,
        is_public BOOLEAN NOT NULL DEFAULT TRUE,
        is_stackable BOOLEAN NOT NULL DEFAULT FALSE,
        email_verified_required BOOLEAN NOT NULL DEFAULT FALSE,
        CONSTRAINT uq_promotions_code UNIQUE (code)
    )""",
    """CREATE TABLE IF NOT EXISTS promo_code_usages (
        id SERIAL PRIMARY KEY,
        promo_code_id INTEGER NOT NULL REFERENCES promotions(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL,
        order_id INTEGER NOT NULL,
        used_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT uq_promo_usage_code_order UNIQUE (promo_code_id, order_id)
    )""",
    """CREATE INDEX IF NOT EXISTS ix_promo_code_usages_promo_user
        ON promo_code_usages (promo_code_id, user_id)""",
    """CREATE TABLE IF NOT EXISTS promotion_target_categories (
        id SERIAL PRIMARY KEY,
        promotion_id INTEGER NOT NULL REFERENCES promotions(id) ON DELETE CASCADE,
        category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
        CONSTRAINT uq_promo_target_category UNIQUE (promotion_id, category_id)
    )""",
    """CREATE TABLE IF NOT EXISTS promotion_target_products (
        id SERIAL PRIMARY KEY,
        promotion_id INTEGER NOT NULL REFERENCES promotions(id) ON DELETE CASCADE,
        product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
        CONSTRAINT uq_promo_target_product UNIQUE (promotion_id, product_id)
    )""",
    """CREATE INDEX IF NOT EXISTS ix_promotions_campaign_id ON promotions (campaign_id)""",
    """CREATE INDEX IF NOT EXISTS ix_promotions_user_id ON promotions (user_id)""",
    """CREATE INDEX IF NOT EXISTS ix_promotions_active_dates ON promotions (is_active, starts_at, ends_at)""",
    """CREATE INDEX IF NOT EXISTS ix_promo_target_categories_promo ON promotion_target_categories (promotion_id)""",
    """CREATE INDEX IF NOT EXISTS ix_promo_target_categories_category ON promotion_target_categories (category_id)""",
    """CREATE INDEX IF NOT EXISTS ix_promo_target_products_promo ON promotion_target_products (promotion_id)""",
    """CREATE INDEX IF NOT EXISTS ix_promo_target_products_product ON promotion_target_products (product_id)""",
    """CREATE TABLE IF NOT EXISTS delivery_zones (
        id SERIAL PRIMARY KEY,
        name VARCHAR(128) NOT NULL,
        polygon JSON NOT NULL,
        fee NUMERIC(10,2) NOT NULL,
        min_order_amount NUMERIC(10,2) NOT NULL,
        estimated_minutes INTEGER NOT NULL,
        is_active BOOLEAN NOT NULL DEFAULT TRUE
    )""",
    """CREATE TABLE IF NOT EXISTS processed_webhook_events (
        id SERIAL PRIMARY KEY,
        stripe_event_id VARCHAR(255) NOT NULL UNIQUE,
        event_type VARCHAR(128) NOT NULL,
        processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
]


async def _provision_tenant_schema(conn, slug: str) -> None:
    """Cree toutes les tables applicatives dans le schema tenant via DDL explicite.

    Args:
        conn: Connexion SQLAlchemy async deja ouverte (dans une transaction).
        slug: Slug tenant valide, utilise pour construire le nom du schema.
    """
    schema = tenant_schema_name(slug)
    await conn.execute(text(f'SET search_path TO "{schema}", public'))
    for stmt in _TENANT_DDL_STATEMENTS:
        await conn.execute(text(stmt))


async def _create_tenant_tables(tenant_slug: str) -> None:
    """Wrapper transactionnel autour de _provision_tenant_schema.

    Args:
        tenant_slug: Slug tenant dont le schema doit etre provisionne.
    """
    async with engine.begin() as conn:
        await _provision_tenant_schema(conn, tenant_slug)


async def register(body, arq_pool=None) -> tuple[User, str, str, int]:
    """Cree un nouveau tenant et son premier utilisateur admin.

    Genere un token de verification email (UUID4, expiry 24h) et enqueue
    send_verification_email si arq_pool est fourni.

    Args:
        body: Payload RegisterRequest valide par Pydantic.
        arq_pool: Pool arq optionnel pour l'envoi de l'email de verification.

    Returns:
        Tuple (user, access_token, refresh_token, session_id).

    Raises:
        AppError: TENANT_EXISTS (409) si le slug est deja pris.
    """
    async with get_public_session() as session:
        existing = await session.scalar(
            text("SELECT id FROM public.tenants WHERE slug = :slug"),
            {"slug": body.tenant_slug},
        )
        if existing:
            raise AppError("TENANT_EXISTS", "Tenant already exists", 409, "tenant_slug")
        row = await session.execute(
            text(
                "INSERT INTO public.tenants (slug, name, plan) "
                "VALUES (:slug, :name, 'starter') RETURNING id"
            ),
            {"slug": body.tenant_slug, "name": body.tenant_name},
        )
        tenant_id = row.scalar_one()
        await session.commit()

    await create_tenant_schema(body.tenant_slug)
    await _create_tenant_tables(body.tenant_slug)
    async with get_tenant_session(body.tenant_slug) as session:
        from app.modules.catalog.allergen.allergen_service import seed_regulatory_allergens

        await seed_regulatory_allergens(session)

    verification_token = str(uuid.uuid4())
    verification_expires_at = datetime.now(timezone.utc) + timedelta(hours=24)

    async with get_tenant_session(body.tenant_slug) as session:
        # Vérification email unique avant insert — la contrainte DB UNIQUE catcherait
        # l'IntegrityError mais retournerait un 500 sans ce check explicite.
        existing_user = await session.scalar(
            select(User).where(User.email == body.email)
        )
        if existing_user is not None:
            raise AppError("EMAIL_ALREADY_EXISTS", "Email already registered", 409, "email")

        user = User(
            email=body.email,
            password_hash=get_password_hash(body.password),
            full_name=body.full_name,
            role="admin",
            email_verification_token=verification_token,
            email_verification_expires_at=verification_expires_at,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        access, refresh, session_id = await issue_tokens(session, user, tenant_id, body.tenant_slug)
        await session.commit()

    if arq_pool is not None:
        try:
            await arq_pool.enqueue_job(
                "send_verification_email",
                tenant_slug=body.tenant_slug,
                user_id=user.id,
                token=verification_token,
            )
        except Exception:
            pass  # Non critique : le user peut demander un renvoi

    return user, access, refresh, session_id


async def verify_email(token: str, tenant_slug: str) -> dict:
    """Marque l'email d'un utilisateur comme verifie a partir du token de confirmation.

    Args:
        token: Token UUID4 recu en query param depuis le lien d'email.
        tenant_slug: Slug du tenant (passe en query param dans l'URL de verification).

    Returns:
        Dict avec message de confirmation et email verifie.

    Raises:
        AppError: INVALID_TOKEN (400) si le token est invalide, introuvable ou expire.
    """
    async with get_tenant_session(tenant_slug) as session:
        user = await session.scalar(
            select(User).where(User.email_verification_token == token)
        )
        if user is None:
            raise AppError("INVALID_TOKEN", "Verification token invalid or already used", 400)

        now = datetime.now(timezone.utc)
        if user.email_verification_expires_at is None or user.email_verification_expires_at < now:
            raise AppError("INVALID_TOKEN", "Verification token has expired", 400)

        user.email_verified_at = now
        user.email_verification_token = None
        user.email_verification_expires_at = None
        await session.commit()
        return {"message": "Email verified successfully", "email": user.email}


def _generate_backup_codes(count: int = 10) -> list[str]:
    return [secrets.token_hex(4).upper() for _ in range(count)]


def _hash_backup_codes(codes: list[str]) -> list[str]:
    return [get_password_hash(code) for code in codes]


def _verify_totp(secret: str, code: str | None) -> bool:
    if not code:
        return False
    import pyotp

    return bool(pyotp.TOTP(secret).verify(code.strip(), valid_window=1))


def _build_mfa_payload(user: User, secret: str, backup_codes: list[str]) -> dict:
    import pyotp
    import qrcode

    otpauth_uri = pyotp.TOTP(secret).provisioning_uri(
        name=user.email,
        issuer_name="Pizzeria API",
    )
    qr_image = qrcode.make(otpauth_uri)
    buffer = BytesIO()
    qr_image.save(buffer, format="PNG")
    qr_code_png_base64 = base64.b64encode(buffer.getvalue()).decode("ascii")
    return {
        "secret": secret,
        "otpauth_uri": otpauth_uri,
        "qr_code_png_base64": qr_code_png_base64,
        "backup_codes": backup_codes,
    }


async def setup_mfa(tenant_slug: str, user_id: int) -> dict:
    async with get_tenant_session(tenant_slug) as session:
        user = await session.get(User, user_id)
        if user is None:
            raise AppError("UNAUTHORIZED", "User not found", 401)
        if user.role not in ("super-admin", "admin"):
            raise AppError("FORBIDDEN", "MFA setup is reserved to admin/super-admin users", 403)
        if user.mfa_enabled:
            raise AppError("MFA_ALREADY_ENABLED", "MFA is already enabled", 409)

        import pyotp

        secret = pyotp.random_base32()
        backup_codes = _generate_backup_codes()
        user.mfa_secret = secret
        user.mfa_enabled = False
        user.mfa_backup_codes = _hash_backup_codes(backup_codes)
        await session.commit()
        await session.refresh(user)
        return _build_mfa_payload(user, secret, backup_codes)


async def confirm_mfa(tenant_slug: str, user_id: int, totp_code: str | None) -> dict:
    async with get_tenant_session(tenant_slug) as session:
        user = await session.get(User, user_id)
        if user is None:
            raise AppError("UNAUTHORIZED", "User not found", 401)
        if user.role not in ("super-admin", "admin"):
            raise AppError("FORBIDDEN", "MFA confirmation is reserved to admin/super-admin users", 403)
        if not user.mfa_secret or not _verify_totp(user.mfa_secret, totp_code):
            raise AppError("INVALID_MFA_CODE", "Invalid MFA code", 400, "totp_code")

        user.mfa_enabled = True
        await session.commit()
        return {"message": "MFA enabled"}


async def regenerate_mfa_backup_codes(
    tenant_slug: str,
    user_id: int,
    totp_code: str | None,
) -> dict:
    async with get_tenant_session(tenant_slug) as session:
        user = await session.get(User, user_id)
        if user is None:
            raise AppError("UNAUTHORIZED", "User not found", 401)
        if user.role not in ("super-admin", "admin"):
            raise AppError("FORBIDDEN", "MFA backup codes are reserved to admin/super-admin users", 403)
        if not user.mfa_enabled or not user.mfa_secret:
            raise AppError("MFA_NOT_ENABLED", "MFA is not enabled", 400)
        if not _verify_totp(user.mfa_secret, totp_code):
            raise AppError("INVALID_MFA_CODE", "Invalid MFA code", 400, "totp_code")

        backup_codes = _generate_backup_codes()
        user.mfa_backup_codes = _hash_backup_codes(backup_codes)
        await session.commit()
        return {"backup_codes": backup_codes}


async def _verify_login_mfa(session: AsyncSession, user: User, mfa_code: str | None) -> None:
    if not mfa_code:
        raise AppError("MFA_REQUIRED", "MFA code required", 401, "mfa_code")
    if not user.mfa_secret:
        raise AppError("MFA_REQUIRED", "MFA setup incomplete", 401, "mfa_code")

    if _verify_totp(user.mfa_secret, mfa_code):
        return

    backup_hashes = list(user.mfa_backup_codes or [])
    for index, hashed_code in enumerate(backup_hashes):
        if verify_password(mfa_code.strip(), hashed_code):
            user.mfa_backup_codes = backup_hashes[:index] + backup_hashes[index + 1 :]
            await session.flush()
            return

    raise AppError("INVALID_MFA_CODE", "Invalid MFA code", 401, "mfa_code")


async def authenticate(
    session: AsyncSession,
    tenant_id: int,
    tenant_slug: str,
    email: str,
    password: str,
    mfa_code: str | None = None,
) -> tuple[User, str, str, int]:
    """Authentifie un utilisateur de facon timing-safe.

    Si l'email est introuvable, un bcrypt dummy est quand meme calcule pour que
    le temps de reponse soit identique (evite le timing oracle sur l'existence
    des comptes).

    [SECURITE] pwd_context.verify utilise une comparaison a temps constant en
    interne. Le dummy_verify sert uniquement a maintenir la duree de traitement.

    Args:
        session: Session SQLAlchemy async dans le schema tenant courant.
        tenant_id: Identifiant numerique du tenant.
        tenant_slug: Slug tenant (pour le payload JWT).
        email: Email soumis par le client.
        password: Mot de passe en clair soumis par le client.

    Returns:
        Tuple (user, access_token, refresh_token, session_id).

    Raises:
        AppError: INVALID_CREDENTIALS (401) si email ou mot de passe invalide.
    """
    user = await session.scalar(
        select(User).where(User.email == email, User.is_active.is_(True))
    )

    # [SECURITE] Timing-safe : si user introuvable, on execute quand meme bcrypt
    # pour ne pas reveler via difference de temps qu'un email n'existe pas.
    if user is None:
        verify_password(password, DUMMY_HASH)  # dummy -- resultat intentionnellement ignore
        raise AppError("INVALID_CREDENTIALS", "Invalid email or password", 401)

    if not verify_password(password, user.password_hash):
        raise AppError("INVALID_CREDENTIALS", "Invalid email or password", 401)

    if user.role in ("super-admin", "admin") and user.mfa_enabled:
        await _verify_login_mfa(session, user, mfa_code)

    access, refresh, session_id = await issue_tokens(session, user, tenant_id, tenant_slug)
    await session.commit()
    return user, access, refresh, session_id


async def issue_tokens(
    session: AsyncSession,
    user: User,
    tenant_id: int,
    tenant_slug: str,
    request=None,  # Optional FastAPI Request for user_agent/ip_address
) -> tuple[str, str, int]:
    """Genere une paire access/refresh token et persiste le refresh en base.

    Le refresh token est stocke sous deux formes :
    - token_hash : bcrypt pour la verification securisee.
    - token_lookup : HMAC-SHA256 indexe pour le lookup O(1).

    Args:
        session: Session SQLAlchemy async (flush/commit a la charge du caller).
        user: Utilisateur pour lequel les tokens sont emis.
        tenant_id: Identifiant du tenant (inclus dans le payload JWT).
        tenant_slug: Slug du tenant (inclus dans le payload JWT).
        request: Requete FastAPI optionnelle pour extraire user_agent et ip_address.

    Returns:
        Tuple (access_token, refresh_token, session_id) — session_id est l'ID
        de la ligne refresh_token inseree.
    """
    user_agent = None
    ip_address = None
    if request is not None:
        user_agent = request.headers.get("user-agent", "")[:512] or None
        ip_address = get_client_ip(request)

    payload = {
        "sub": str(user.id),
        "email": user.email,
        "role": user.role,
        "permissions": user.permissions,
        "tenant_id": tenant_id,
        "tenant_slug": tenant_slug,
        "must_change_password": user.must_change_password,
    }
    access = create_access_token(payload)
    refresh = create_refresh_token(payload)
    refresh_row = RefreshToken(
        user_id=user.id,
        token_hash=get_password_hash(refresh),
        token_lookup=compute_token_lookup(refresh),
        expires_at=datetime.now(timezone.utc) + timedelta(days=settings.jwt_refresh_expire_days),
        user_agent=user_agent,
        ip_address=ip_address,
    )
    session.add(refresh_row)
    await session.flush()  # populate refresh_row.id
    return access, refresh, refresh_row.id


async def login(body) -> tuple[User, str, str, int]:
    async with get_public_session() as public:
        result = await public.execute(
            text("SELECT id FROM public.tenants WHERE slug = :slug"),
            {"slug": body.tenant_slug},
        )
        tenant_id = result.scalar_one_or_none()
    if tenant_id is None:
        raise AppError("TENANT_NOT_FOUND", "Tenant not found", 404, "tenant_slug")
    async with get_tenant_session(body.tenant_slug) as session:
        return await authenticate(
            session,
            tenant_id,
            body.tenant_slug,
            body.email,
            body.password,
            mfa_code=body.mfa_code,
        )


async def refresh_token(token: str) -> dict:
    """Echange un refresh token valide contre une nouvelle paire de tokens.

    Strategie de lookup en deux phases :
    1. Calcul du token_lookup (HMAC-SHA256) -> SELECT WHERE token_lookup = ? -> O(1).
    2. Fallback O(n*bcrypt) pour les tokens sans token_lookup (pre-migration 0003).
    3. Verification bcrypt sur le seul enregistrement trouve.

    [SECURITE] Le token revoque est marque revoked_at avant l'emission du nouveau
    (rotation monotone). En cas d'erreur, le rollback annule la revocation.

    Args:
        token: Refresh token JWT en clair extrait du corps de la requete.

    Returns:
        Dictionnaire {"access_token": str, "refresh_token": str}.

    Raises:
        AppError: INVALID_TOKEN (401) si le token est invalide, expire ou revoque.
        AppError: UNAUTHORIZED (401) si l'utilisateur associe est introuvable.
    """
    try:
        payload = decode_token(token)
    except Exception as exc:
        raise AppError("INVALID_TOKEN", "Refresh token is invalid", 401) from exc
    if payload.get("type") != "refresh":
        raise AppError("INVALID_TOKEN", "Refresh token required", 401)

    tenant_slug = payload["tenant_slug"]
    user_id = int(payload["sub"])
    lookup = compute_token_lookup(token)

    async with get_tenant_session(tenant_slug) as session:
        # Phase 1 : lookup O(1) via HMAC index.
        current = await session.scalar(
            select(RefreshToken).where(
                RefreshToken.token_lookup == lookup,
                RefreshToken.revoked_at.is_(None),
            )
        )

        # Phase 2 : fallback O(n*bcrypt) pour les tokens sans token_lookup (pre-0003).
        if current is None:
            rows = await session.execute(
                select(RefreshToken).where(
                    RefreshToken.user_id == user_id,
                    RefreshToken.token_lookup.is_(None),
                    RefreshToken.revoked_at.is_(None),
                )
            )
            candidates = [r for r in rows.scalars() if verify_password(token, r.token_hash)]
            if not candidates:
                raise AppError("INVALID_TOKEN", "Refresh token is invalid or revoked", 401)
            current = candidates[0]
        else:
            if not verify_password(token, current.token_hash):
                raise AppError("INVALID_TOKEN", "Refresh token is invalid or revoked", 401)

        current.revoked_at = datetime.now(timezone.utc)
        user = await session.get(User, user_id)
        if user is None:
            raise AppError("UNAUTHORIZED", "User not found", 401)
        access, refresh, session_id = await issue_tokens(session, user, payload["tenant_id"], tenant_slug)
        await session.commit()
        return {"access_token": access, "refresh_token": refresh, "session_id": session_id}


async def logout(token: str, tenant_slug: str, user_id: int) -> None:
    async with get_tenant_session(tenant_slug) as session:
        await session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(timezone.utc))
        )
        await session.commit()


async def get_sessions(
    user_id: int,
    tenant_slug: str,
    current_session_id: int | None = None,
) -> list[dict]:
    """Return active (non-revoked, non-expired) sessions for a user.

    Args:
        user_id: ID of the authenticated user.
        tenant_slug: Tenant schema to query.
        current_session_id: When provided, the matching session gets is_current=True.

    Returns:
        List of dicts compatible with SessionOut schema.
    """
    now = datetime.now(timezone.utc)
    async with get_tenant_session(tenant_slug) as session:
        result = await session.execute(
            select(RefreshToken).where(
                RefreshToken.user_id == user_id,
                RefreshToken.revoked_at.is_(None),
                RefreshToken.expires_at > now,
            )
        )
        rows = result.scalars().all()
        return [
            {
                "id": r.id,
                "created_at": r.created_at,
                "expires_at": r.expires_at,
                "user_agent": r.user_agent,
                "ip_address": r.ip_address,
                "is_current": r.id == current_session_id,
            }
            for r in rows
        ]


async def revoke_session(
    session_id: int,
    user_id: int,
    tenant_slug: str,
    redis=None,
) -> None:
    """Revoke a specific refresh token session (ownership-checked).

    Args:
        session_id: ID of the RefreshToken row to revoke.
        user_id: ID of the authenticated user (ownership check).
        tenant_slug: Tenant schema to query.
        redis: Unused — reserved for future JTI revocation.

    Raises:
        AppError: NOT_FOUND (404) if session doesn't exist or belongs to another user.
    """
    now = datetime.now(timezone.utc)
    async with get_tenant_session(tenant_slug) as session:
        token_row = await session.scalar(
            select(RefreshToken).where(
                RefreshToken.id == session_id,
                RefreshToken.user_id == user_id,
                RefreshToken.revoked_at.is_(None),
            )
        )
        if token_row is None:
            raise AppError("NOT_FOUND", "Session not found", 404)
        token_row.revoked_at = now
        await session.commit()


async def revoke_all_sessions(
    user_id: int,
    tenant_slug: str,
    current_session_id: int | None = None,
    revoke_current: bool = False,
    redis=None,
) -> None:
    """Revoke all active sessions for a user, optionally keeping the current one.

    Args:
        user_id: ID of the authenticated user.
        tenant_slug: Tenant schema to query.
        current_session_id: ID of the current refresh token session to preserve
            when revoke_current=False.
        revoke_current: When True, revoke ALL sessions including the current one.
            When False, keep the session identified by current_session_id.
        redis: Unused at service level — JTI revocation is handled in the router.
    """
    now = datetime.now(timezone.utc)
    async with get_tenant_session(tenant_slug) as session:
        stmt = update(RefreshToken).where(
            RefreshToken.user_id == user_id,
            RefreshToken.revoked_at.is_(None),
        )
        if not revoke_current and current_session_id is not None:
            stmt = stmt.where(RefreshToken.id != current_session_id)
        await session.execute(stmt.values(revoked_at=now))
        await session.commit()


async def forgot_password(body, arq_pool=None) -> None:
    """Genere un token de reinitialisation et l'envoie par email. Toujours sans erreur.

    Si le tenant ou l'email est inconnu, la fonction retourne silencieusement
    pour ne pas reveler l'existence des comptes (anti-enumeration).

    Args:
        body: Payload ForgotPasswordRequest valide par Pydantic.
        arq_pool: Pool arq optionnel pour l'envoi de l'email de reinitialisation.
    """
    try:
        async with get_public_session() as pub:
            result = await pub.execute(
                text("SELECT id FROM public.tenants WHERE slug = :slug"),
                {"slug": body.tenant_slug},
            )
            if result.scalar_one_or_none() is None:
                return  # Tenant inconnu — reponse silencieuse

        async with get_tenant_session(body.tenant_slug) as session:
            user = await session.scalar(
                select(User).where(User.email == body.email, User.is_active.is_(True))
            )
            if user is None:
                return  # Email inconnu — reponse silencieuse

            # 256 bits d'entropie (32 bytes) — envoye en clair par email, seul le hash bcrypt est stocke.
            token = secrets.token_urlsafe(32)
            user.password_reset_token = get_password_hash(token)
            user.password_reset_expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)
            await session.commit()

        if arq_pool is not None:
            try:
                await arq_pool.enqueue_job(
                    "send_password_reset_email",
                    tenant_slug=body.tenant_slug,
                    user_id=user.id,
                    token=token,
                )
            except Exception:
                pass
    except Exception:
        pass  # Absorbe toutes les erreurs — jamais de leak via timing


async def reset_password(body, redis=None) -> dict:
    """Reinitialise le mot de passe via le token recu par email.

    Args:
        body: Payload ResetPasswordRequest valide par Pydantic.
        redis: Client Redis optionnel pour flaguer le user comme desactive.

    Returns:
        Dict {"message": "Password reset successfully"}.

    Raises:
        AppError: INVALID_TOKEN (400) si le token est invalide, expire ou introuvable.
    """
    async with get_public_session() as pub:
        result = await pub.execute(
            text("SELECT id FROM public.tenants WHERE slug = :slug"),
            {"slug": body.tenant_slug},
        )
        if result.scalar_one_or_none() is None:
            raise AppError("INVALID_TOKEN", "Invalid or expired token", 400)

    async with get_tenant_session(body.tenant_slug) as session:
        user = await session.scalar(
            select(User).where(User.email == body.email, User.is_active.is_(True))
        )
        if user is None or user.password_reset_token is None:
            raise AppError("INVALID_TOKEN", "Invalid or expired token", 400)

        now = datetime.now(timezone.utc)
        if user.password_reset_expires_at is None or user.password_reset_expires_at < now:
            raise AppError("INVALID_TOKEN", "Invalid or expired token", 400)

        if not verify_password(body.token, user.password_reset_token):
            raise AppError("INVALID_TOKEN", "Invalid or expired token", 400)

        user.password_hash = get_password_hash(body.new_password)
        user.password_reset_token = None
        user.password_reset_expires_at = None
        user.must_change_password = False

        # Revoque tous les refresh tokens actifs
        await session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        await session.commit()

    # Force re-login via user_disabled flag Redis
    if redis is not None:
        from app.core.auth.token_revocation import flag_user_disabled
        await flag_user_disabled(redis, user.id, body.tenant_slug)

    return {"message": "Password reset successfully"}


async def resend_verification(user_id: int, tenant_slug: str, arq_pool=None) -> None:
    """Genere un nouveau token de verification email et l'envoie par email.

    Args:
        user_id: ID de l'utilisateur demandant le renvoi.
        tenant_slug: Slug du tenant de l'utilisateur.
        arq_pool: Pool arq optionnel pour l'envoi de l'email.

    Raises:
        AppError: UNAUTHORIZED (401) si l'utilisateur est introuvable.
        AppError: ALREADY_VERIFIED (400) si l'email est deja verifie.
    """
    async with get_tenant_session(tenant_slug) as session:
        user = await session.get(User, user_id)
        if user is None:
            raise AppError("UNAUTHORIZED", "User not found", 401)
        if user.email_verified_at is not None:
            raise AppError("ALREADY_VERIFIED", "Email is already verified", 400)

        user.email_verification_token = str(uuid.uuid4())
        user.email_verification_expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
        await session.commit()

    if arq_pool is not None:
        try:
            await arq_pool.enqueue_job(
                "send_verification_email",
                tenant_slug=tenant_slug,
                user_id=user_id,
                token=user.email_verification_token,
            )
        except Exception:
            pass


async def change_password(
    user_id: int,
    tenant_slug: str,
    body,
    current_refresh_token_id: int | None = None,
    redis=None,
) -> dict:
    """Change le mot de passe d'un utilisateur authentifie.

    Si must_change_password est True, le mot de passe actuel n'est pas requis.
    Sinon, current_password doit etre fourni et valide.
    Tous les refresh tokens actifs sont revoques apres le changement.

    Args:
        user_id: ID de l'utilisateur.
        tenant_slug: Slug du tenant.
        body: Payload ChangePasswordRequest valide par Pydantic.
        current_refresh_token_id: ID du refresh token courant (reserve pour Task 9).
        redis: Client Redis optionnel (non utilise actuellement).

    Returns:
        Dict {"message": "Password changed successfully"}.

    Raises:
        AppError: UNAUTHORIZED (401) si l'utilisateur est introuvable.
        AppError: VALIDATION_ERROR (422) si current_password manquant quand requis.
        AppError: INVALID_CREDENTIALS (401) si le mot de passe actuel est incorrect.
    """
    async with get_tenant_session(tenant_slug) as session:
        user = await session.get(User, user_id)
        if user is None:
            raise AppError("UNAUTHORIZED", "User not found", 401)

        if not user.must_change_password:
            if not body.current_password:
                raise AppError("VALIDATION_ERROR", "current_password is required", 422)
            if not verify_password(body.current_password, user.password_hash):
                raise AppError("INVALID_CREDENTIALS", "Current password is incorrect", 401)

        user.password_hash = get_password_hash(body.new_password)
        user.must_change_password = False

        now = datetime.now(timezone.utc)
        stmt = (
            update(RefreshToken)
            .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        )
        if current_refresh_token_id is not None:
            stmt = stmt.where(RefreshToken.id != current_refresh_token_id)
        await session.execute(stmt.values(revoked_at=now))
        await session.commit()

    return {"message": "Password changed successfully"}
