-- Seed complet pour le tenant de test api-pizza.
-- Tenant: pizza_test / schema: tenant_pizza_test
-- Mot de passe de test commun: Testpizza123@
-- Le mot de passe n'est jamais stocke en clair: hash bcrypt genere via
-- app.core.auth.security.get_password_hash("Testpizza123@").

BEGIN;

-- ---------------------------------------------------------------------------
-- 00. Contexte public et schema tenant
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS public.tenants (
    id SERIAL PRIMARY KEY,
    slug VARCHAR(64) NOT NULL UNIQUE,
    name VARCHAR(255) NOT NULL,
    plan VARCHAR(32) NOT NULL DEFAULT 'starter',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE public.tenants
    ADD COLUMN IF NOT EXISTS is_suspended BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS suspended_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS suspension_message TEXT;

CREATE TABLE IF NOT EXISTS public.tenant_configs (
    id SERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES public.tenants(id),
    delivery_zones JSON,
    stripe_account_id VARCHAR(255),
    currency VARCHAR(8) DEFAULT 'EUR',
    timezone VARCHAR(64) DEFAULT 'Europe/Paris',
    logo_url VARCHAR(512)
);

INSERT INTO public.tenants (slug, name, plan, is_suspended, suspended_at, suspension_message)
VALUES ('pizza_test', 'PizzaTEST', 'starter', FALSE, NULL, NULL)
ON CONFLICT (slug) DO UPDATE
SET name = EXCLUDED.name,
    plan = EXCLUDED.plan,
    is_suspended = FALSE,
    suspended_at = NULL,
    suspension_message = NULL;

DO $$
DECLARE
    v_tenant_id INTEGER;
BEGIN
    SELECT id INTO v_tenant_id FROM public.tenants WHERE slug = 'pizza_test';

    UPDATE public.tenant_configs
    SET delivery_zones = '[{"name":"Centre-ville","fee":2.50},{"name":"Peripherie","fee":4.00}]'::json,
        stripe_account_id = 'acct_pizza_test_seed',
        currency = 'EUR',
        timezone = 'Europe/Paris',
        logo_url = 'https://images-platform.99static.com/68-VMPXLUt7aMCVfwq_mnsHXl5Y=/0x0:1200x1200/500x500/top/smart/99designs-contests-attachments/101/101459/attachment_101459022'
    WHERE id = (
        SELECT id FROM public.tenant_configs
        WHERE tenant_id = v_tenant_id
        ORDER BY id
        LIMIT 1
    );

    IF NOT FOUND THEN
        INSERT INTO public.tenant_configs (
            tenant_id, delivery_zones, stripe_account_id, currency, timezone, logo_url
        )
        VALUES (
            v_tenant_id,
            '[{"name":"Centre-ville","fee":2.50},{"name":"Peripherie","fee":4.00}]'::json,
            'acct_pizza_test_seed',
            'EUR',
            'Europe/Paris',
            'https://images-platform.99static.com/68-VMPXLUt7aMCVfwq_mnsHXl5Y=/0x0:1200x1200/500x500/top/smart/99designs-contests-attachments/101/101459/attachment_101459022'
        );
    END IF;

    DELETE FROM public.tenant_configs
    WHERE tenant_id = v_tenant_id
    AND id <> (
        SELECT MIN(id)
        FROM public.tenant_configs
        WHERE tenant_id = v_tenant_id
    );
END $$;

CREATE SCHEMA IF NOT EXISTS tenant_pizza_test;
SET search_path TO tenant_pizza_test, public;

-- ---------------------------------------------------------------------------
-- 01. DDL tenant defensif
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS users (
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
);
CREATE INDEX IF NOT EXISTS ix_users_email ON users (email);
CREATE INDEX IF NOT EXISTS ix_users_verification_token ON users (email_verification_token);
CREATE INDEX IF NOT EXISTS ix_users_password_reset_token ON users (password_reset_token);

CREATE TABLE IF NOT EXISTS refresh_tokens (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash VARCHAR(255) NOT NULL,
    token_lookup VARCHAR(64) UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    user_agent VARCHAR(512),
    ip_address VARCHAR(45)
);
CREATE INDEX IF NOT EXISTS ix_refresh_tokens_token_hash ON refresh_tokens (token_hash);
CREATE INDEX IF NOT EXISTS ix_refresh_tokens_token_lookup ON refresh_tokens (token_lookup);

CREATE TABLE IF NOT EXISTS tenant_config (
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
);

CREATE TABLE IF NOT EXISTS business_hours (
    id SERIAL PRIMARY KEY,
    day_of_week INTEGER NOT NULL,
    slot_index INTEGER NOT NULL,
    opens_at TIME NOT NULL,
    closes_at TIME NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT ck_business_hours_closes_after_opens CHECK (closes_at > opens_at)
);
CREATE INDEX IF NOT EXISTS ix_business_hours_day_slot ON business_hours (day_of_week, slot_index);

CREATE TABLE IF NOT EXISTS exceptional_closures (
    id SERIAL PRIMARY KEY,
    closure_date DATE NOT NULL UNIQUE,
    custom_message TEXT,
    use_default_message BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_exceptional_closures_date ON exceptional_closures (closure_date);

CREATE TABLE IF NOT EXISTS tenant_config_audits (
    id SERIAL PRIMARY KEY,
    changed_by_user_id INTEGER NOT NULL,
    user_email VARCHAR(255),
    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    field_name VARCHAR(255) NOT NULL,
    old_value TEXT,
    new_value TEXT,
    ip_address VARCHAR(45),
    user_agent TEXT
);
CREATE INDEX IF NOT EXISTS ix_tenant_config_audits_changed_at ON tenant_config_audits (changed_at);

CREATE TABLE IF NOT EXISTS categories (
    id SERIAL PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    display_order INTEGER NOT NULL DEFAULT 0,
    preparation_station VARCHAR(16) NOT NULL DEFAULT 'kitchen',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT ck_categories_preparation_station CHECK (preparation_station IN ('kitchen', 'counter', 'none'))
);

CREATE TABLE IF NOT EXISTS products (
    id SERIAL PRIMARY KEY,
    category_id INTEGER REFERENCES categories(id),
    name VARCHAR(255) NOT NULL,
    description TEXT,
    base_price NUMERIC(10,2) NOT NULL,
    image_url VARCHAR(512),
    preparation_station VARCHAR(16),
    is_delivery_prohibited BOOLEAN NOT NULL DEFAULT FALSE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    is_featured BOOLEAN NOT NULL DEFAULT FALSE,
    CONSTRAINT ck_products_preparation_station CHECK (preparation_station IS NULL OR preparation_station IN ('kitchen', 'counter', 'none'))
);

CREATE TABLE IF NOT EXISTS product_availability_overrides (
    id SERIAL PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    available BOOLEAN NOT NULL,
    reason TEXT,
    changed_by_user_id INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_product_availability_overrides_product_created
    ON product_availability_overrides (product_id, created_at);

CREATE TABLE IF NOT EXISTS product_variants (
    id SERIAL PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    name VARCHAR(128) NOT NULL,
    price_delta NUMERIC(10,2) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS extras (
    id SERIAL PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    price NUMERIC(10,2) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS product_extras (
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    extra_id INTEGER NOT NULL REFERENCES extras(id) ON DELETE CASCADE,
    PRIMARY KEY (product_id, extra_id)
);

CREATE TABLE IF NOT EXISTS media_images (
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
);
CREATE INDEX IF NOT EXISTS ix_media_images_entity ON media_images (entity_type, entity_id);

CREATE TABLE IF NOT EXISTS ingredients (
    id SERIAL PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    unit VARCHAR(32) NOT NULL,
    current_qty NUMERIC(12,3) NOT NULL,
    alert_threshold NUMERIC(12,3) NOT NULL,
    last_alert_sent_at TIMESTAMPTZ,
    CONSTRAINT ck_ingredients_current_qty_non_negative CHECK (current_qty >= 0)
);

CREATE TABLE IF NOT EXISTS allergen_definitions (
    id SERIAL PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    slug VARCHAR(64) NOT NULL UNIQUE,
    is_regulatory BOOLEAN NOT NULL DEFAULT FALSE,
    description TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_allergen_definitions_slug ON allergen_definitions (slug);

CREATE TABLE IF NOT EXISTS ingredient_allergens (
    ingredient_id INTEGER NOT NULL REFERENCES ingredients(id) ON DELETE CASCADE,
    allergen_id INTEGER NOT NULL REFERENCES allergen_definitions(id) ON DELETE CASCADE,
    level VARCHAR(10) NOT NULL,
    PRIMARY KEY (ingredient_id, allergen_id),
    CONSTRAINT ck_ingredient_allergen_level CHECK (level IN ('present', 'traces', 'absent'))
);

CREATE TABLE IF NOT EXISTS product_allergens (
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    allergen_id INTEGER NOT NULL REFERENCES allergen_definitions(id) ON DELETE CASCADE,
    level VARCHAR(10) NOT NULL,
    source VARCHAR(10) NOT NULL DEFAULT 'ingredient',
    PRIMARY KEY (product_id, allergen_id),
    CONSTRAINT ck_product_allergen_level CHECK (level IN ('present', 'traces', 'absent')),
    CONSTRAINT ck_product_allergen_source CHECK (source IN ('ingredient', 'manual'))
);

CREATE TABLE IF NOT EXISTS dietary_tags (
    id SERIAL PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    slug VARCHAR(64) NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_dietary_tags_slug ON dietary_tags (slug);

CREATE TABLE IF NOT EXISTS product_dietary_tags (
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    dietary_tag_id INTEGER NOT NULL REFERENCES dietary_tags(id) ON DELETE CASCADE,
    PRIMARY KEY (product_id, dietary_tag_id)
);

CREATE TABLE IF NOT EXISTS allergen_change_audits (
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
);
CREATE INDEX IF NOT EXISTS ix_allergen_change_audits_product_changed
    ON allergen_change_audits (product_id, changed_at);

CREATE TABLE IF NOT EXISTS extra_ingredients (
    id SERIAL PRIMARY KEY,
    extra_id INTEGER NOT NULL REFERENCES extras(id) ON DELETE CASCADE,
    ingredient_id INTEGER NOT NULL REFERENCES ingredients(id),
    quantity NUMERIC(12,3) NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_extra_ingredients_extra ON extra_ingredients (extra_id);
CREATE INDEX IF NOT EXISTS ix_extra_ingredients_ingredient ON extra_ingredients (ingredient_id);

CREATE TABLE IF NOT EXISTS product_recommendations (
    id SERIAL PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    recommended_product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    display_order INTEGER NOT NULL DEFAULT 0,
    label VARCHAR(128),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT uq_product_recommendations_pair UNIQUE (product_id, recommended_product_id)
);
CREATE INDEX IF NOT EXISTS ix_product_recommendations_product ON product_recommendations (product_id);
CREATE INDEX IF NOT EXISTS ix_product_recommendations_recommended ON product_recommendations (recommended_product_id);

CREATE TABLE IF NOT EXISTS catalog_price_audits (
    id SERIAL PRIMARY KEY,
    entity_type VARCHAR(16) NOT NULL,
    entity_id INTEGER NOT NULL,
    old_price NUMERIC(10,2),
    new_price NUMERIC(10,2) NOT NULL,
    changed_by_user_id INTEGER,
    source VARCHAR(16) NOT NULL,
    reason TEXT,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_catalog_price_audits_entity
    ON catalog_price_audits (entity_type, entity_id, changed_at);

CREATE TABLE IF NOT EXISTS catalog_import_batches (
    id SERIAL PRIMARY KEY,
    token VARCHAR(64) NOT NULL UNIQUE,
    filename VARCHAR(255),
    csv_text TEXT NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'dry_run',
    validation_report JSON NOT NULL,
    created_by_user_id INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS orders (
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
);
CREATE INDEX IF NOT EXISTS ix_orders_user_id ON orders (user_id);
CREATE INDEX IF NOT EXISTS ix_orders_status ON orders (status);
CREATE INDEX IF NOT EXISTS ix_orders_created_at ON orders (created_at);
CREATE INDEX IF NOT EXISTS ix_orders_status_created_at_pizza_test ON orders (status, created_at);

CREATE TABLE IF NOT EXISTS order_items (
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
);

CREATE TABLE IF NOT EXISTS order_status_history (
    id SERIAL PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    status VARCHAR(32) NOT NULL,
    note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS payments (
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
);
CREATE INDEX IF NOT EXISTS ix_payments_provider_payment_id ON payments (provider_payment_id);

CREATE TABLE IF NOT EXISTS refunds (
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
);
CREATE INDEX IF NOT EXISTS ix_refunds_order_id ON refunds (order_id);
CREATE INDEX IF NOT EXISTS ix_refunds_payment_id ON refunds (payment_id);

CREATE TABLE IF NOT EXISTS product_ingredients (
    id SERIAL PRIMARY KEY,
    product_id INTEGER NOT NULL,
    ingredient_id INTEGER NOT NULL REFERENCES ingredients(id),
    quantity NUMERIC(12,3) NOT NULL
);

CREATE TABLE IF NOT EXISTS variant_ingredients (
    id SERIAL PRIMARY KEY,
    variant_id INTEGER NOT NULL,
    ingredient_id INTEGER NOT NULL REFERENCES ingredients(id),
    quantity NUMERIC(12,3) NOT NULL
);

CREATE TABLE IF NOT EXISTS stock_movements (
    id SERIAL PRIMARY KEY,
    ingredient_id INTEGER NOT NULL REFERENCES ingredients(id),
    quantity_delta NUMERIC(12,3) NOT NULL,
    reason VARCHAR(64) NOT NULL,
    user_id INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_stock_movements_ingredient_id_pizza_test ON stock_movements (ingredient_id);

CREATE TABLE IF NOT EXISTS ingredient_batches (
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
);
CREATE INDEX IF NOT EXISTS ix_ingredient_batches_ingredient ON ingredient_batches (ingredient_id);
CREATE INDEX IF NOT EXISTS ix_ingredient_batches_expires_at ON ingredient_batches (expires_at);

CREATE TABLE IF NOT EXISTS stock_adjustment_requests (
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
);
CREATE INDEX IF NOT EXISTS ix_stock_adjustment_requests_status ON stock_adjustment_requests (status);
CREATE INDEX IF NOT EXISTS ix_stock_adjustment_requests_ingredient ON stock_adjustment_requests (ingredient_id);

CREATE TABLE IF NOT EXISTS product_stock (
    id SERIAL PRIMARY KEY,
    product_id INTEGER NOT NULL,
    available_qty INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS loyalty_accounts (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    points INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_loyalty_accounts_user_id UNIQUE (user_id)
);

CREATE TABLE IF NOT EXISTS loyalty_transactions (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES loyalty_accounts(id) ON DELETE CASCADE,
    points_delta INTEGER NOT NULL,
    reason VARCHAR(64) NOT NULL,
    transaction_type VARCHAR(32) NOT NULL DEFAULT 'adjustment',
    source VARCHAR(32) NOT NULL DEFAULT 'system',
    changed_by_user_id INTEGER,
    order_id INTEGER,
    reward_id INTEGER,
    reservation_id INTEGER,
    metadata JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS loyalty_config (
    id SERIAL PRIMARY KEY,
    base_ratio NUMERIC(10,4) NOT NULL DEFAULT 1.0,
    points_expiry_days INTEGER,
    points_to_euro_rate NUMERIC(10,4) NOT NULL DEFAULT 0.0100,
    max_cumulative_multiplier NUMERIC(6,2) NOT NULL DEFAULT 20.00,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS loyalty_rules (
    id SERIAL PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    rule_type VARCHAR(32) NOT NULL,
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    multiplier NUMERIC(6,4) NOT NULL,
    start_date DATE,
    end_date DATE,
    days_of_week INTEGER[],
    priority INTEGER NOT NULL DEFAULT 0,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_loyalty_rule_multiplier_range CHECK (multiplier >= 1.0 AND multiplier <= 10.0),
    CONSTRAINT ck_loyalty_rule_priority_range CHECK (priority >= 0 AND priority <= 100)
);

CREATE TABLE IF NOT EXISTS loyalty_rewards (
    id SERIAL PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    reward_type VARCHAR(32) NOT NULL,
    points_required INTEGER NOT NULL,
    discount_amount NUMERIC(10,2),
    product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_loyalty_rewards_points_required ON loyalty_rewards (points_required);

CREATE TABLE IF NOT EXISTS loyalty_point_reservations (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    order_id INTEGER NOT NULL,
    points_reserved INTEGER NOT NULL,
    discount_amount NUMERIC(10,2) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'reserved',
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    confirmed_at TIMESTAMPTZ,
    cancelled_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_loyalty_reservations_user_status_pizza_test
    ON loyalty_point_reservations (user_id, status);
CREATE INDEX IF NOT EXISTS ix_loyalty_reservations_order_pizza_test
    ON loyalty_point_reservations (order_id);

CREATE TABLE IF NOT EXISTS promotion_campaigns (
    id SERIAL PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    prefix VARCHAR(32) NOT NULL,
    description VARCHAR(255),
    created_by_user_id INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS promotions (
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
);
CREATE INDEX IF NOT EXISTS ix_promotions_campaign_id ON promotions (campaign_id);
CREATE INDEX IF NOT EXISTS ix_promotions_user_id ON promotions (user_id);
CREATE INDEX IF NOT EXISTS ix_promotions_active_dates ON promotions (is_active, starts_at, ends_at);

CREATE TABLE IF NOT EXISTS promo_code_usages (
    id SERIAL PRIMARY KEY,
    promo_code_id INTEGER NOT NULL REFERENCES promotions(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    order_id INTEGER NOT NULL,
    used_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_promo_usage_code_order UNIQUE (promo_code_id, order_id)
);
CREATE INDEX IF NOT EXISTS ix_promo_code_usages_promo_user
    ON promo_code_usages (promo_code_id, user_id);

CREATE TABLE IF NOT EXISTS promotion_target_categories (
    id SERIAL PRIMARY KEY,
    promotion_id INTEGER NOT NULL REFERENCES promotions(id) ON DELETE CASCADE,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    CONSTRAINT uq_promo_target_category UNIQUE (promotion_id, category_id)
);
CREATE INDEX IF NOT EXISTS ix_promo_target_categories_promo ON promotion_target_categories (promotion_id);
CREATE INDEX IF NOT EXISTS ix_promo_target_categories_category ON promotion_target_categories (category_id);

CREATE TABLE IF NOT EXISTS promotion_target_products (
    id SERIAL PRIMARY KEY,
    promotion_id INTEGER NOT NULL REFERENCES promotions(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    CONSTRAINT uq_promo_target_product UNIQUE (promotion_id, product_id)
);
CREATE INDEX IF NOT EXISTS ix_promo_target_products_promo ON promotion_target_products (promotion_id);
CREATE INDEX IF NOT EXISTS ix_promo_target_products_product ON promotion_target_products (product_id);

CREATE TABLE IF NOT EXISTS delivery_zones (
    id SERIAL PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    polygon JSON NOT NULL,
    fee NUMERIC(10,2) NOT NULL,
    min_order_amount NUMERIC(10,2) NOT NULL,
    estimated_minutes INTEGER NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS processed_webhook_events (
    id SERIAL PRIMARY KEY,
    stripe_event_id VARCHAR(255) NOT NULL UNIQUE,
    event_type VARCHAR(128) NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS device_tokens (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    platform VARCHAR(10) NOT NULL,
    token VARCHAR(512) NOT NULL,
    device_name VARCHAR(128),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ,
    CONSTRAINT uq_device_token_user UNIQUE (user_id, token)
);
CREATE INDEX IF NOT EXISTS ix_device_tokens_user_active ON device_tokens (user_id, is_active);

CREATE TABLE IF NOT EXISTS restaurant_delivery_settings (
    id SERIAL PRIMARY KEY,
    restaurant_lat DOUBLE PRECISION,
    restaurant_lng DOUBLE PRECISION,
    display_address TEXT,
    independent_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    internal_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    pickup_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    internal_delivery_fee NUMERIC(10,2),
    internal_delivery_minutes INTEGER,
    internal_max_eta_minutes INTEGER,
    restaurant_share_giveaway_points INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_restaurant_delivery_settings_share_giveaway_points
        CHECK (restaurant_share_giveaway_points IN (0, 5, 10, 15))
);

CREATE TABLE IF NOT EXISTS restaurant_delivery_settings_audits (
    id SERIAL PRIMARY KEY,
    changed_by_user_id INTEGER NOT NULL,
    user_email VARCHAR(255),
    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    field_name VARCHAR(255) NOT NULL,
    old_value TEXT,
    new_value TEXT,
    ip_address VARCHAR(45),
    user_agent TEXT
);
CREATE INDEX IF NOT EXISTS ix_restaurant_delivery_settings_audits_changed_at
    ON restaurant_delivery_settings_audits (changed_at);

-- Colonnes ajoutees par migrations, pour schemas partiellement provisionnes.
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS phone VARCHAR(20),
    ADD COLUMN IF NOT EXISTS permissions JSON,
    ADD COLUMN IF NOT EXISTS email_verification_token VARCHAR(64),
    ADD COLUMN IF NOT EXISTS email_verification_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS email_verified_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS password_reset_token VARCHAR(64),
    ADD COLUMN IF NOT EXISTS password_reset_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS mfa_secret VARCHAR(255),
    ADD COLUMN IF NOT EXISTS mfa_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS mfa_backup_codes JSON;

ALTER TABLE refresh_tokens
    ADD COLUMN IF NOT EXISTS token_lookup VARCHAR(64),
    ADD COLUMN IF NOT EXISTS user_agent VARCHAR(512),
    ADD COLUMN IF NOT EXISTS ip_address VARCHAR(45);

ALTER TABLE tenant_config
    ADD COLUMN IF NOT EXISTS timezone VARCHAR(64) NOT NULL DEFAULT 'Europe/Paris',
    ADD COLUMN IF NOT EXISTS scheduled_close_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS stock_alert_cooldown_hours INTEGER NOT NULL DEFAULT 4,
    ADD COLUMN IF NOT EXISTS large_stock_adjustment_threshold NUMERIC(12,3) NOT NULL DEFAULT 10,
    ADD COLUMN IF NOT EXISTS print_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS print_config JSON,
    ADD COLUMN IF NOT EXISTS display_name VARCHAR(120),
    ADD COLUMN IF NOT EXISTS logo_url TEXT,
    ADD COLUMN IF NOT EXISTS primary_color VARCHAR(7),
    ADD COLUMN IF NOT EXISTS secondary_color VARCHAR(7),
    ADD COLUMN IF NOT EXISTS font_family VARCHAR(50);

ALTER TABLE categories
    ADD COLUMN IF NOT EXISTS preparation_station VARCHAR(16) NOT NULL DEFAULT 'kitchen';

ALTER TABLE products
    ADD COLUMN IF NOT EXISTS preparation_station VARCHAR(16),
    ADD COLUMN IF NOT EXISTS is_delivery_prohibited BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS is_featured BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS customer_name VARCHAR(255),
    ADD COLUMN IF NOT EXISTS customer_phone VARCHAR(32),
    ADD COLUMN IF NOT EXISTS order_type VARCHAR(16) NOT NULL DEFAULT 'delivery',
    ADD COLUMN IF NOT EXISTS payment_status VARCHAR(32) NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS source VARCHAR(16) NOT NULL DEFAULT 'customer',
    ADD COLUMN IF NOT EXISTS created_by_user_id INTEGER,
    ADD COLUMN IF NOT EXISTS delivery_zone_id INTEGER,
    ADD COLUMN IF NOT EXISTS table_number VARCHAR(32),
    ADD COLUMN IF NOT EXISTS estimated_delivery_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(128),
    ADD COLUMN IF NOT EXISTS promo_code VARCHAR(64);

ALTER TABLE order_items
    ADD COLUMN IF NOT EXISTS product_name_snapshot VARCHAR(255),
    ADD COLUMN IF NOT EXISTS variant_name_snapshot VARCHAR(128),
    ADD COLUMN IF NOT EXISTS extras_snapshot JSON,
    ADD COLUMN IF NOT EXISTS extras_total NUMERIC(10,2) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS preparation_status VARCHAR(16) NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS preparation_station VARCHAR(16) NOT NULL DEFAULT 'kitchen',
    ADD COLUMN IF NOT EXISTS prepared_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS prepared_by_user_id INTEGER;

ALTER TABLE payments
    ADD COLUMN IF NOT EXISTS provider_account_id VARCHAR(255),
    ADD COLUMN IF NOT EXISTS external_reference VARCHAR(255),
    ADD COLUMN IF NOT EXISTS amount_received NUMERIC(10,2),
    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS created_by_user_id INTEGER;

ALTER TABLE refunds
    ADD COLUMN IF NOT EXISTS failure_reason VARCHAR(512),
    ADD COLUMN IF NOT EXISTS created_by_user_id INTEGER;

ALTER TABLE ingredients
    ADD COLUMN IF NOT EXISTS last_alert_sent_at TIMESTAMPTZ;

ALTER TABLE stock_movements
    ADD COLUMN IF NOT EXISTS user_id INTEGER;

ALTER TABLE loyalty_transactions
    ADD COLUMN IF NOT EXISTS transaction_type VARCHAR(32) NOT NULL DEFAULT 'adjustment',
    ADD COLUMN IF NOT EXISTS source VARCHAR(32) NOT NULL DEFAULT 'system',
    ADD COLUMN IF NOT EXISTS changed_by_user_id INTEGER,
    ADD COLUMN IF NOT EXISTS order_id INTEGER,
    ADD COLUMN IF NOT EXISTS reward_id INTEGER,
    ADD COLUMN IF NOT EXISTS reservation_id INTEGER,
    ADD COLUMN IF NOT EXISTS metadata JSONB;

ALTER TABLE loyalty_config
    ADD COLUMN IF NOT EXISTS points_to_euro_rate NUMERIC(10,4) NOT NULL DEFAULT 0.0100,
    ADD COLUMN IF NOT EXISTS max_cumulative_multiplier NUMERIC(6,2) NOT NULL DEFAULT 20.00;

ALTER TABLE promotions
    ADD COLUMN IF NOT EXISTS max_uses INTEGER,
    ADD COLUMN IF NOT EXISTS max_uses_per_user INTEGER,
    ADD COLUMN IF NOT EXISTS current_uses INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS first_order_only BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS campaign_id INTEGER REFERENCES promotion_campaigns(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS user_id INTEGER,
    ADD COLUMN IF NOT EXISTS is_public BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS is_stackable BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS email_verified_required BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_products_fts_pizza_test
    ON products USING GIN (to_tsvector('french', name || ' ' || COALESCE(description, '')));

-- ---------------------------------------------------------------------------
-- 02. Donnees tenant, utilisateurs, horaires
-- ---------------------------------------------------------------------------

INSERT INTO tenant_config (
    id,
    is_temporarily_closed,
    temporary_closure_message,
    default_closure_message,
    prep_time_normal_minutes,
    prep_time_peak_minutes,
    peak_orders_threshold,
    auto_calc_prep_time,
    overhead_per_order_minutes,
    timezone,
    stock_alert_cooldown_hours,
    large_stock_adjustment_threshold,
    print_enabled,
    print_config,
    display_name,
    logo_url,
    primary_color,
    secondary_color,
    font_family
)
VALUES (
    1,
    FALSE,
    NULL,
    'Nous sommes temporairement fermes. Nous vous accueillons bientot !',
    20,
    40,
    4,
    TRUE,
    3,
    'Europe/Paris',
    4,
    10.000,
    TRUE,
    '{"printer":"test-kitchen","paper_width":"80mm","copies":1}'::json,
    'PizzaTEST',
    'https://example.test/assets/pizza-test/logo.png',
    '#D72638',
    '#1B998B',
    'poppins'
)
ON CONFLICT (id) DO UPDATE
SET is_temporarily_closed = EXCLUDED.is_temporarily_closed,
    temporary_closure_message = EXCLUDED.temporary_closure_message,
    default_closure_message = EXCLUDED.default_closure_message,
    prep_time_normal_minutes = EXCLUDED.prep_time_normal_minutes,
    prep_time_peak_minutes = EXCLUDED.prep_time_peak_minutes,
    peak_orders_threshold = EXCLUDED.peak_orders_threshold,
    auto_calc_prep_time = EXCLUDED.auto_calc_prep_time,
    overhead_per_order_minutes = EXCLUDED.overhead_per_order_minutes,
    timezone = EXCLUDED.timezone,
    stock_alert_cooldown_hours = EXCLUDED.stock_alert_cooldown_hours,
    large_stock_adjustment_threshold = EXCLUDED.large_stock_adjustment_threshold,
    print_enabled = EXCLUDED.print_enabled,
    print_config = EXCLUDED.print_config,
    display_name = EXCLUDED.display_name,
    logo_url = EXCLUDED.logo_url,
    primary_color = EXCLUDED.primary_color,
    secondary_color = EXCLUDED.secondary_color,
    font_family = EXCLUDED.font_family,
    updated_at = NOW();

INSERT INTO users (
    email,
    password_hash,
    full_name,
    phone,
    role,
    permissions,
    is_active,
    email_verified_at,
    must_change_password,
    mfa_enabled
)
VALUES
    ('pizza@test.com', '$2b$12$e4ja2MjZ9yZnpMR5FpuKtu4mDXjvL9QWP76ZjAdsCGno5MzT4dpXO', 'Momo', '+33600000001', 'admin', '["*"]'::json, TRUE, NOW(), FALSE, FALSE),
    ('staff.cuisine@test.com', '$2b$12$e4ja2MjZ9yZnpMR5FpuKtu4mDXjvL9QWP76ZjAdsCGno5MzT4dpXO', 'Sam Cuisine', '+33600000002', 'staff', '["orders:read","orders:write","stock:read","stock:write","catalog:read"]'::json, TRUE, NOW(), FALSE, FALSE),
    ('staff.livraison@test.com', '$2b$12$e4ja2MjZ9yZnpMR5FpuKtu4mDXjvL9QWP76ZjAdsCGno5MzT4dpXO', 'Lina Livraison', '+33600000003', 'staff', '["orders:read","orders:write","delivery:read"]'::json, TRUE, NOW(), FALSE, FALSE),
    ('client.alice@test.com', '$2b$12$e4ja2MjZ9yZnpMR5FpuKtu4mDXjvL9QWP76ZjAdsCGno5MzT4dpXO', 'Alice Martin', '+33600000011', 'customer', NULL, TRUE, NOW(), FALSE, FALSE),
    ('client.yanis@test.com', '$2b$12$e4ja2MjZ9yZnpMR5FpuKtu4mDXjvL9QWP76ZjAdsCGno5MzT4dpXO', 'Yanis Petit', '+33600000012', 'customer', NULL, TRUE, NOW(), FALSE, FALSE),
    ('client.inactive@test.com', '$2b$12$e4ja2MjZ9yZnpMR5FpuKtu4mDXjvL9QWP76ZjAdsCGno5MzT4dpXO', 'Client Inactif', '+33600000013', 'customer', NULL, FALSE, NOW(), FALSE, FALSE)
ON CONFLICT (email) DO UPDATE
SET password_hash = EXCLUDED.password_hash,
    full_name = EXCLUDED.full_name,
    phone = EXCLUDED.phone,
    role = EXCLUDED.role,
    permissions = EXCLUDED.permissions,
    is_active = EXCLUDED.is_active,
    email_verified_at = EXCLUDED.email_verified_at,
    must_change_password = EXCLUDED.must_change_password,
    mfa_enabled = EXCLUDED.mfa_enabled;

DROP TABLE IF EXISTS _seed_hours;
CREATE TEMP TABLE _seed_hours (
    day_of_week INTEGER,
    slot_index INTEGER,
    opens_at TIME,
    closes_at TIME,
    is_active BOOLEAN
) ON COMMIT DROP;

INSERT INTO _seed_hours VALUES
    (0, 0, '11:30', '14:30', TRUE), (0, 1, '18:00', '22:30', TRUE),
    (1, 0, '11:30', '14:30', TRUE), (1, 1, '18:00', '22:30', TRUE),
    (2, 0, '11:30', '14:30', TRUE), (2, 1, '18:00', '22:30', TRUE),
    (3, 0, '11:30', '14:30', TRUE), (3, 1, '18:00', '23:00', TRUE),
    (4, 0, '11:30', '14:30', TRUE), (4, 1, '18:00', '23:30', TRUE),
    (5, 0, '11:30', '15:00', TRUE), (5, 1, '18:00', '23:30', TRUE),
    (6, 0, '18:00', '22:30', TRUE);

DO $$
DECLARE
    r RECORD;
    v_id INTEGER;
BEGIN
    FOR r IN SELECT * FROM _seed_hours LOOP
        SELECT id INTO v_id
        FROM business_hours
        WHERE day_of_week = r.day_of_week AND slot_index = r.slot_index
        LIMIT 1;

        IF v_id IS NULL THEN
            INSERT INTO business_hours (day_of_week, slot_index, opens_at, closes_at, is_active)
            VALUES (r.day_of_week, r.slot_index, r.opens_at, r.closes_at, r.is_active);
        ELSE
            UPDATE business_hours
            SET opens_at = r.opens_at,
                closes_at = r.closes_at,
                is_active = r.is_active
            WHERE id = v_id;
        END IF;
    END LOOP;
END $$;

INSERT INTO exceptional_closures (closure_date, custom_message, use_default_message)
VALUES ((CURRENT_DATE + INTERVAL '30 days')::date, 'Fermeture test inventaire mensuel', FALSE)
ON CONFLICT (closure_date) DO UPDATE
SET custom_message = EXCLUDED.custom_message,
    use_default_message = EXCLUDED.use_default_message;

INSERT INTO restaurant_delivery_settings (
    id,
    restaurant_lat,
    restaurant_lng,
    display_address,
    independent_enabled,
    internal_enabled,
    pickup_enabled,
    internal_delivery_fee,
    internal_delivery_minutes,
    internal_max_eta_minutes,
    restaurant_share_giveaway_points,
    version,
    updated_at
)
VALUES (
    1,
    48.8566,
    2.3522,
    '12 rue de la Testerie, 75001 Paris',
    FALSE,
    TRUE,
    TRUE,
    2.50,
    20,
    55,
    5,
    1,
    NOW()
)
ON CONFLICT (id) DO UPDATE
SET restaurant_lat = EXCLUDED.restaurant_lat,
    restaurant_lng = EXCLUDED.restaurant_lng,
    display_address = EXCLUDED.display_address,
    independent_enabled = EXCLUDED.independent_enabled,
    internal_enabled = EXCLUDED.internal_enabled,
    pickup_enabled = EXCLUDED.pickup_enabled,
    internal_delivery_fee = EXCLUDED.internal_delivery_fee,
    internal_delivery_minutes = EXCLUDED.internal_delivery_minutes,
    internal_max_eta_minutes = EXCLUDED.internal_max_eta_minutes,
    restaurant_share_giveaway_points = EXCLUDED.restaurant_share_giveaway_points,
    version = restaurant_delivery_settings.version + 1,
    updated_at = NOW();

-- ---------------------------------------------------------------------------
-- 03. Catalogue: categories, produits, tailles, extras
-- ---------------------------------------------------------------------------

DROP TABLE IF EXISTS _seed_categories;
CREATE TEMP TABLE _seed_categories (
    name VARCHAR(128),
    display_order INTEGER,
    preparation_station VARCHAR(16),
    is_active BOOLEAN
) ON COMMIT DROP;

INSERT INTO _seed_categories VALUES
    ('Pizzas rouges', 10, 'kitchen', TRUE),
    ('Pizzas blanches', 20, 'kitchen', TRUE),
    ('Calzones', 30, 'kitchen', TRUE),
    ('Boissons', 40, 'counter', TRUE),
    ('Desserts', 50, 'counter', TRUE);

DO $$
DECLARE
    r RECORD;
    v_id INTEGER;
BEGIN
    FOR r IN SELECT * FROM _seed_categories LOOP
        SELECT id INTO v_id FROM categories WHERE name = r.name LIMIT 1;
        IF v_id IS NULL THEN
            INSERT INTO categories (name, display_order, preparation_station, is_active)
            VALUES (r.name, r.display_order, r.preparation_station, r.is_active);
        ELSE
            UPDATE categories
            SET display_order = r.display_order,
                preparation_station = r.preparation_station,
                is_active = r.is_active
            WHERE id = v_id;
        END IF;
    END LOOP;
END $$;

DROP TABLE IF EXISTS _seed_products;
CREATE TEMP TABLE _seed_products (
    category_name VARCHAR(128),
    name VARCHAR(255),
    description TEXT,
    base_price NUMERIC(10,2),
    image_url VARCHAR(512),
    preparation_station VARCHAR(16),
    is_delivery_prohibited BOOLEAN,
    is_active BOOLEAN,
    is_featured BOOLEAN
) ON COMMIT DROP;

INSERT INTO _seed_products VALUES
    ('Pizzas rouges', 'Margherita', 'Sauce tomate, mozzarella fior di latte, basilic frais.', 9.90, 'https://res.cloudinary.com/de4wqnklh/image/upload/w_800,q_auto,f_auto/pizza/pizza_test/products/1/whqwkqqkwwgd3iqkxkej', NULL, FALSE, TRUE, TRUE),
    ('Pizzas rouges', 'Reine', 'Sauce tomate, mozzarella, jambon blanc, champignons.', 12.50, 'https://res.cloudinary.com/de4wqnklh/image/upload/w_800,q_auto,f_auto/pizza/pizza_test/products/2/lvwhfxj98cftrwt4gjqc', NULL, FALSE, TRUE, FALSE),
    ('Pizzas blanches', 'Quatre Fromages', 'Creme, mozzarella, gorgonzola, chevre, parmesan.', 13.90, 'https://res.cloudinary.com/de4wqnklh/image/upload/w_800,q_auto,f_auto/pizza/pizza_test/products/3/h9jln5yn9ik1vghhji0y', NULL, FALSE, TRUE, TRUE),
    ('Pizzas rouges', 'Pepperoni', 'Sauce tomate, mozzarella, pepperoni legerement piquant.', 13.50, 'https://res.cloudinary.com/de4wqnklh/image/upload/w_800,q_auto,f_auto/pizza/pizza_test/products/4/hzbb4vuiugkjli8x6fao', NULL, FALSE, TRUE, TRUE),
    ('Pizzas rouges', 'Vegetarienne', 'Sauce tomate, mozzarella, champignons, olives, roquette.', 12.90, 'https://res.cloudinary.com/de4wqnklh/image/upload/w_800,q_auto,f_auto/pizza/pizza_test/products/5/zapcdbw7oypfgfalv905', NULL, FALSE, TRUE, FALSE),
    ('Calzones', 'Calzone Classique', 'Chausson pizza tomate, mozzarella, jambon, oeuf.', 12.00, 'https://res.cloudinary.com/de4wqnklh/image/upload/w_800,q_auto,f_auto/pizza/pizza_test/products/6/e4u2rix5qzn1mnjitcjy', NULL, FALSE, TRUE, FALSE),
    ('Boissons', 'Coca-Cola 33cl', 'Canette fraiche 33cl.', 2.50, 'https://res.cloudinary.com/de4wqnklh/image/upload/w_800,q_auto,f_auto/pizza/pizza_test/products/7/mfnlhsji80rpmujosrzd', 'counter', FALSE, TRUE, FALSE),
    ('Desserts', 'Tiramisu maison', 'Mascarpone, cafe, cacao et biscuit.', 4.90, 'https://res.cloudinary.com/de4wqnklh/image/upload/w_800,q_auto,f_auto/pizza/pizza_test/products/8/ykkm8mjsyhrzwkt7sclp', 'counter', TRUE, TRUE, FALSE),
    ('Pizzas rouges', 'Marinara rupture test', 'Sauce tomate, ail, origan. Produit actif mais stock volontairement limite.', 8.50, 'https://res.cloudinary.com/de4wqnklh/image/upload/w_800,q_auto,f_auto/pizza/pizza_test/products/9/vblglltg0pfkahcu2yxe', NULL, FALSE, TRUE, FALSE);

DO $$
DECLARE
    r RECORD;
    v_id INTEGER;
    v_category_id INTEGER;
BEGIN
    FOR r IN SELECT * FROM _seed_products LOOP
        SELECT id INTO v_category_id FROM categories WHERE name = r.category_name LIMIT 1;
        SELECT id INTO v_id FROM products WHERE name = r.name LIMIT 1;
        IF v_id IS NULL THEN
            INSERT INTO products (
                category_id, name, description, base_price, image_url,
                preparation_station, is_delivery_prohibited, is_active, is_featured
            )
            VALUES (
                v_category_id, r.name, r.description, r.base_price, r.image_url,
                r.preparation_station, r.is_delivery_prohibited, r.is_active, r.is_featured
            );
        ELSE
            UPDATE products
            SET category_id = v_category_id,
                description = r.description,
                base_price = r.base_price,
                image_url = r.image_url,
                preparation_station = r.preparation_station,
                is_delivery_prohibited = r.is_delivery_prohibited,
                is_active = r.is_active,
                is_featured = r.is_featured
            WHERE id = v_id;
        END IF;
    END LOOP;
END $$;

DROP TABLE IF EXISTS _seed_variants;
CREATE TEMP TABLE _seed_variants (
    product_name VARCHAR(255),
    name VARCHAR(128),
    price_delta NUMERIC(10,2),
    is_active BOOLEAN
) ON COMMIT DROP;

INSERT INTO _seed_variants
SELECT p.name, v.name, v.price_delta, TRUE
FROM (VALUES
    ('Petite', -2.00::numeric),
    ('Classique', 0.00::numeric),
    ('Grande', 4.00::numeric)
) AS v(name, price_delta)
CROSS JOIN (
    VALUES
        ('Margherita'),
        ('Reine'),
        ('Quatre Fromages'),
        ('Pepperoni'),
        ('Vegetarienne'),
        ('Calzone Classique'),
        ('Marinara rupture test')
) AS p(name);

DO $$
DECLARE
    r RECORD;
    v_id INTEGER;
    v_product_id INTEGER;
BEGIN
    FOR r IN SELECT * FROM _seed_variants LOOP
        SELECT id INTO v_product_id FROM products WHERE name = r.product_name LIMIT 1;
        SELECT id INTO v_id
        FROM product_variants
        WHERE product_id = v_product_id AND name = r.name
        LIMIT 1;

        IF v_id IS NULL THEN
            INSERT INTO product_variants (product_id, name, price_delta, is_active)
            VALUES (v_product_id, r.name, r.price_delta, r.is_active);
        ELSE
            UPDATE product_variants
            SET price_delta = r.price_delta,
                is_active = r.is_active
            WHERE id = v_id;
        END IF;
    END LOOP;
END $$;

DROP TABLE IF EXISTS _seed_extras;
CREATE TEMP TABLE _seed_extras (
    name VARCHAR(128),
    price NUMERIC(10,2),
    is_active BOOLEAN
) ON COMMIT DROP;

INSERT INTO _seed_extras VALUES
    ('Mozzarella supplementaire', 1.50, TRUE),
    ('Champignons supplementaires', 1.00, TRUE),
    ('Pepperoni supplementaire', 1.80, TRUE),
    ('Jambon supplementaire', 1.60, TRUE),
    ('Olives', 0.80, TRUE),
    ('Oeuf', 1.20, TRUE),
    ('Sauce piquante', 0.50, TRUE);

DO $$
DECLARE
    r RECORD;
    v_id INTEGER;
BEGIN
    FOR r IN SELECT * FROM _seed_extras LOOP
        SELECT id INTO v_id FROM extras WHERE name = r.name LIMIT 1;
        IF v_id IS NULL THEN
            INSERT INTO extras (name, price, is_active)
            VALUES (r.name, r.price, r.is_active);
        ELSE
            UPDATE extras SET price = r.price, is_active = r.is_active WHERE id = v_id;
        END IF;
    END LOOP;
END $$;

INSERT INTO product_extras (product_id, extra_id)
SELECT p.id, e.id
FROM products p
CROSS JOIN extras e
WHERE p.name IN (
    'Margherita', 'Reine', 'Quatre Fromages', 'Pepperoni',
    'Vegetarienne', 'Calzone Classique', 'Marinara rupture test'
)
AND e.name IN (
    'Mozzarella supplementaire', 'Champignons supplementaires',
    'Pepperoni supplementaire', 'Jambon supplementaire',
    'Olives', 'Oeuf', 'Sauce piquante'
)
ON CONFLICT (product_id, extra_id) DO NOTHING;

-- Images primaires Cloudinary pour tester galerie et catalogue enrichi.
-- Les routes FastAPI utilisent le type DB "product" pour /catalog/products/{id}/images.
DELETE FROM media_images
WHERE entity_type = 'products'
  AND cloudinary_public_id LIKE 'pizza/pizza_test/products/%/seed-primary'
  AND url LIKE 'https://example.test/assets/pizza-test/%';

DROP TABLE IF EXISTS _seed_product_images;
CREATE TEMP TABLE _seed_product_images (
    product_name VARCHAR(255),
    cloudinary_public_id VARCHAR(256),
    url VARCHAR(512),
    url_thumbnail VARCHAR(512),
    url_medium VARCHAR(512),
    format VARCHAR(10),
    size_bytes INTEGER,
    width INTEGER,
    height INTEGER,
    alt_text VARCHAR(256)
) ON COMMIT DROP;

INSERT INTO _seed_product_images VALUES
    ('Margherita', 'pizza/pizza_test/products/1/whqwkqqkwwgd3iqkxkej', 'https://res.cloudinary.com/de4wqnklh/image/upload/v1785182839/pizza/pizza_test/products/1/whqwkqqkwwgd3iqkxkej.png', 'https://res.cloudinary.com/de4wqnklh/image/upload/c_fill,w_300,h_300,q_auto,f_auto/pizza/pizza_test/products/1/whqwkqqkwwgd3iqkxkej', 'https://res.cloudinary.com/de4wqnklh/image/upload/w_800,q_auto,f_auto/pizza/pizza_test/products/1/whqwkqqkwwgd3iqkxkej', 'png', 2603033, 1254, 1254, 'Photo de la pizza Margherita'),
    ('Reine', 'pizza/pizza_test/products/2/lvwhfxj98cftrwt4gjqc', 'https://res.cloudinary.com/de4wqnklh/image/upload/v1785182842/pizza/pizza_test/products/2/lvwhfxj98cftrwt4gjqc.png', 'https://res.cloudinary.com/de4wqnklh/image/upload/c_fill,w_300,h_300,q_auto,f_auto/pizza/pizza_test/products/2/lvwhfxj98cftrwt4gjqc', 'https://res.cloudinary.com/de4wqnklh/image/upload/w_800,q_auto,f_auto/pizza/pizza_test/products/2/lvwhfxj98cftrwt4gjqc', 'png', 2587562, 1254, 1254, 'Photo de la pizza Reine'),
    ('Quatre Fromages', 'pizza/pizza_test/products/3/h9jln5yn9ik1vghhji0y', 'https://res.cloudinary.com/de4wqnklh/image/upload/v1785182844/pizza/pizza_test/products/3/h9jln5yn9ik1vghhji0y.png', 'https://res.cloudinary.com/de4wqnklh/image/upload/c_fill,w_300,h_300,q_auto,f_auto/pizza/pizza_test/products/3/h9jln5yn9ik1vghhji0y', 'https://res.cloudinary.com/de4wqnklh/image/upload/w_800,q_auto,f_auto/pizza/pizza_test/products/3/h9jln5yn9ik1vghhji0y', 'png', 2492949, 1254, 1254, 'Photo de la pizza Quatre Fromages'),
    ('Pepperoni', 'pizza/pizza_test/products/4/hzbb4vuiugkjli8x6fao', 'https://res.cloudinary.com/de4wqnklh/image/upload/v1785182851/pizza/pizza_test/products/4/hzbb4vuiugkjli8x6fao.png', 'https://res.cloudinary.com/de4wqnklh/image/upload/c_fill,w_300,h_300,q_auto,f_auto/pizza/pizza_test/products/4/hzbb4vuiugkjli8x6fao', 'https://res.cloudinary.com/de4wqnklh/image/upload/w_800,q_auto,f_auto/pizza/pizza_test/products/4/hzbb4vuiugkjli8x6fao', 'png', 2533564, 1254, 1254, 'Photo de la pizza Pepperoni'),
    ('Vegetarienne', 'pizza/pizza_test/products/5/zapcdbw7oypfgfalv905', 'https://res.cloudinary.com/de4wqnklh/image/upload/v1785182854/pizza/pizza_test/products/5/zapcdbw7oypfgfalv905.png', 'https://res.cloudinary.com/de4wqnklh/image/upload/c_fill,w_300,h_300,q_auto,f_auto/pizza/pizza_test/products/5/zapcdbw7oypfgfalv905', 'https://res.cloudinary.com/de4wqnklh/image/upload/w_800,q_auto,f_auto/pizza/pizza_test/products/5/zapcdbw7oypfgfalv905', 'png', 2820087, 1254, 1254, 'Photo de la pizza Vegetarienne'),
    ('Calzone Classique', 'pizza/pizza_test/products/6/e4u2rix5qzn1mnjitcjy', 'https://res.cloudinary.com/de4wqnklh/image/upload/v1785182857/pizza/pizza_test/products/6/e4u2rix5qzn1mnjitcjy.png', 'https://res.cloudinary.com/de4wqnklh/image/upload/c_fill,w_300,h_300,q_auto,f_auto/pizza/pizza_test/products/6/e4u2rix5qzn1mnjitcjy', 'https://res.cloudinary.com/de4wqnklh/image/upload/w_800,q_auto,f_auto/pizza/pizza_test/products/6/e4u2rix5qzn1mnjitcjy', 'png', 2208245, 1254, 1254, 'Photo de la Calzone Classique'),
    ('Coca-Cola 33cl', 'pizza/pizza_test/products/7/mfnlhsji80rpmujosrzd', 'https://res.cloudinary.com/de4wqnklh/image/upload/v1785182859/pizza/pizza_test/products/7/mfnlhsji80rpmujosrzd.png', 'https://res.cloudinary.com/de4wqnklh/image/upload/c_fill,w_300,h_300,q_auto,f_auto/pizza/pizza_test/products/7/mfnlhsji80rpmujosrzd', 'https://res.cloudinary.com/de4wqnklh/image/upload/w_800,q_auto,f_auto/pizza/pizza_test/products/7/mfnlhsji80rpmujosrzd', 'png', 2092908, 1254, 1254, 'Photo de la boisson cola 33cl'),
    ('Tiramisu maison', 'pizza/pizza_test/products/8/ykkm8mjsyhrzwkt7sclp', 'https://res.cloudinary.com/de4wqnklh/image/upload/v1785182862/pizza/pizza_test/products/8/ykkm8mjsyhrzwkt7sclp.png', 'https://res.cloudinary.com/de4wqnklh/image/upload/c_fill,w_300,h_300,q_auto,f_auto/pizza/pizza_test/products/8/ykkm8mjsyhrzwkt7sclp', 'https://res.cloudinary.com/de4wqnklh/image/upload/w_800,q_auto,f_auto/pizza/pizza_test/products/8/ykkm8mjsyhrzwkt7sclp', 'png', 2102767, 1254, 1254, 'Photo du tiramisu maison'),
    ('Marinara rupture test', 'pizza/pizza_test/products/9/vblglltg0pfkahcu2yxe', 'https://res.cloudinary.com/de4wqnklh/image/upload/v1785183489/pizza/pizza_test/products/9/vblglltg0pfkahcu2yxe.png', 'https://res.cloudinary.com/de4wqnklh/image/upload/c_fill,w_300,h_300,q_auto,f_auto/pizza/pizza_test/products/9/vblglltg0pfkahcu2yxe', 'https://res.cloudinary.com/de4wqnklh/image/upload/w_800,q_auto,f_auto/pizza/pizza_test/products/9/vblglltg0pfkahcu2yxe', 'png', 2866059, 1254, 1254, 'Photo de la pizza Marinara rupture test');

UPDATE products p
SET image_url = spi.url_medium
FROM _seed_product_images spi
WHERE p.name = spi.product_name;

UPDATE media_images mi
SET is_primary = FALSE
FROM products p
JOIN _seed_product_images spi ON spi.product_name = p.name
WHERE mi.entity_type = 'product'
  AND mi.entity_id = p.id
  AND mi.cloudinary_public_id <> spi.cloudinary_public_id
  AND mi.is_primary = TRUE;

INSERT INTO media_images (
    entity_type, entity_id, cloudinary_public_id, url, url_thumbnail, url_medium,
    format, size_bytes, width, height, is_primary, display_order, alt_text
)
SELECT
    'product',
    p.id,
    spi.cloudinary_public_id,
    spi.url,
    spi.url_thumbnail,
    spi.url_medium,
    spi.format,
    spi.size_bytes,
    spi.width,
    spi.height,
    TRUE,
    0,
    spi.alt_text
FROM _seed_product_images spi
JOIN products p ON p.name = spi.product_name
ON CONFLICT (cloudinary_public_id) DO UPDATE
SET entity_type = EXCLUDED.entity_type,
    entity_id = EXCLUDED.entity_id,
    url = EXCLUDED.url,
    url_thumbnail = EXCLUDED.url_thumbnail,
    url_medium = EXCLUDED.url_medium,
    format = EXCLUDED.format,
    size_bytes = EXCLUDED.size_bytes,
    width = EXCLUDED.width,
    height = EXCLUDED.height,
    is_primary = TRUE,
    display_order = 0,
    alt_text = EXCLUDED.alt_text;

-- ---------------------------------------------------------------------------
-- 04. Ingredients, recettes, stock
-- ---------------------------------------------------------------------------

DROP TABLE IF EXISTS _seed_ingredients;
CREATE TEMP TABLE _seed_ingredients (
    name VARCHAR(128),
    unit VARCHAR(32),
    opening_qty NUMERIC(12,3),
    alert_threshold NUMERIC(12,3)
) ON COMMIT DROP;

INSERT INTO _seed_ingredients VALUES
    ('Farine T00', 'kg', 35.000, 5.000),
    ('Eau', 'L', 22.000, 4.000),
    ('Levure', 'kg', 1.200, 0.200),
    ('Sel', 'kg', 3.000, 0.400),
    ('Sauce tomate', 'L', 18.000, 3.000),
    ('Creme fraiche', 'L', 8.000, 1.500),
    ('Mozzarella', 'kg', 20.000, 3.000),
    ('Basilic', 'kg', 1.500, 0.150),
    ('Jambon blanc', 'kg', 8.000, 1.000),
    ('Champignons', 'kg', 7.000, 1.000),
    ('Pepperoni', 'kg', 5.000, 0.800),
    ('Gorgonzola', 'kg', 4.000, 0.600),
    ('Chevre', 'kg', 4.000, 0.600),
    ('Parmesan', 'kg', 4.000, 0.600),
    ('Olives noires', 'kg', 5.000, 0.700),
    ('Oeufs', 'piece', 120.000, 24.000),
    ('Roquette', 'kg', 2.000, 0.300),
    ('Canettes Coca-Cola 33cl', 'piece', 96.000, 24.000),
    ('Mascarpone', 'kg', 5.000, 0.800),
    ('Cafe', 'L', 6.000, 1.000),
    ('Cacao', 'kg', 2.000, 0.300),
    ('Ail', 'kg', 0.300, 0.500),
    ('Origan', 'kg', 0.500, 0.100);

DO $$
DECLARE
    r RECORD;
    v_id INTEGER;
BEGIN
    FOR r IN SELECT * FROM _seed_ingredients LOOP
        SELECT id INTO v_id FROM ingredients WHERE name = r.name LIMIT 1;
        IF v_id IS NULL THEN
            INSERT INTO ingredients (name, unit, current_qty, alert_threshold)
            VALUES (r.name, r.unit, r.opening_qty, r.alert_threshold);
        ELSE
            UPDATE ingredients
            SET unit = r.unit,
                current_qty = r.opening_qty,
                alert_threshold = r.alert_threshold,
                last_alert_sent_at = NULL
            WHERE id = v_id;
        END IF;
    END LOOP;
END $$;

DROP TABLE IF EXISTS _seed_product_recipes;
CREATE TEMP TABLE _seed_product_recipes (
    product_name VARCHAR(255),
    ingredient_name VARCHAR(128),
    quantity NUMERIC(12,3)
) ON COMMIT DROP;

INSERT INTO _seed_product_recipes VALUES
    ('Margherita', 'Farine T00', 0.220), ('Margherita', 'Eau', 0.150), ('Margherita', 'Levure', 0.006), ('Margherita', 'Sel', 0.004), ('Margherita', 'Sauce tomate', 0.120), ('Margherita', 'Mozzarella', 0.120), ('Margherita', 'Basilic', 0.010),
    ('Reine', 'Farine T00', 0.220), ('Reine', 'Eau', 0.150), ('Reine', 'Levure', 0.006), ('Reine', 'Sel', 0.004), ('Reine', 'Sauce tomate', 0.120), ('Reine', 'Mozzarella', 0.120), ('Reine', 'Jambon blanc', 0.080), ('Reine', 'Champignons', 0.070),
    ('Quatre Fromages', 'Farine T00', 0.220), ('Quatre Fromages', 'Eau', 0.150), ('Quatre Fromages', 'Levure', 0.006), ('Quatre Fromages', 'Sel', 0.004), ('Quatre Fromages', 'Creme fraiche', 0.080), ('Quatre Fromages', 'Mozzarella', 0.100), ('Quatre Fromages', 'Gorgonzola', 0.050), ('Quatre Fromages', 'Chevre', 0.050), ('Quatre Fromages', 'Parmesan', 0.030),
    ('Pepperoni', 'Farine T00', 0.220), ('Pepperoni', 'Eau', 0.150), ('Pepperoni', 'Levure', 0.006), ('Pepperoni', 'Sel', 0.004), ('Pepperoni', 'Sauce tomate', 0.120), ('Pepperoni', 'Mozzarella', 0.120), ('Pepperoni', 'Pepperoni', 0.090),
    ('Vegetarienne', 'Farine T00', 0.220), ('Vegetarienne', 'Eau', 0.150), ('Vegetarienne', 'Levure', 0.006), ('Vegetarienne', 'Sel', 0.004), ('Vegetarienne', 'Sauce tomate', 0.120), ('Vegetarienne', 'Mozzarella', 0.100), ('Vegetarienne', 'Champignons', 0.080), ('Vegetarienne', 'Olives noires', 0.050), ('Vegetarienne', 'Roquette', 0.030),
    ('Calzone Classique', 'Farine T00', 0.240), ('Calzone Classique', 'Eau', 0.160), ('Calzone Classique', 'Levure', 0.006), ('Calzone Classique', 'Sel', 0.004), ('Calzone Classique', 'Sauce tomate', 0.090), ('Calzone Classique', 'Mozzarella', 0.130), ('Calzone Classique', 'Jambon blanc', 0.080), ('Calzone Classique', 'Oeufs', 1.000),
    ('Coca-Cola 33cl', 'Canettes Coca-Cola 33cl', 1.000),
    ('Tiramisu maison', 'Mascarpone', 0.120), ('Tiramisu maison', 'Cafe', 0.040), ('Tiramisu maison', 'Cacao', 0.010), ('Tiramisu maison', 'Oeufs', 0.500),
    ('Marinara rupture test', 'Farine T00', 0.220), ('Marinara rupture test', 'Eau', 0.150), ('Marinara rupture test', 'Levure', 0.006), ('Marinara rupture test', 'Sel', 0.004), ('Marinara rupture test', 'Sauce tomate', 0.120), ('Marinara rupture test', 'Ail', 0.050), ('Marinara rupture test', 'Origan', 0.010);

DO $$
DECLARE
    r RECORD;
    v_id INTEGER;
    v_product_id INTEGER;
    v_ingredient_id INTEGER;
BEGIN
    FOR r IN SELECT * FROM _seed_product_recipes LOOP
        SELECT id INTO v_product_id FROM products WHERE name = r.product_name LIMIT 1;
        SELECT id INTO v_ingredient_id FROM ingredients WHERE name = r.ingredient_name LIMIT 1;
        SELECT id INTO v_id
        FROM product_ingredients
        WHERE product_id = v_product_id AND ingredient_id = v_ingredient_id
        LIMIT 1;

        IF v_id IS NULL THEN
            INSERT INTO product_ingredients (product_id, ingredient_id, quantity)
            VALUES (v_product_id, v_ingredient_id, r.quantity);
        ELSE
            UPDATE product_ingredients SET quantity = r.quantity WHERE id = v_id;
        END IF;
    END LOOP;
END $$;

-- Les variantes "Grande" ajoutent un supplement de pate/garniture a la recette.
INSERT INTO variant_ingredients (variant_id, ingredient_id, quantity)
SELECT pv.id, i.id, x.quantity
FROM product_variants pv
JOIN products p ON p.id = pv.product_id
JOIN (VALUES
    ('Farine T00', 0.080::numeric),
    ('Eau', 0.050::numeric),
    ('Levure', 0.002::numeric),
    ('Sel', 0.001::numeric),
    ('Mozzarella', 0.040::numeric)
) AS x(ingredient_name, quantity) ON TRUE
JOIN ingredients i ON i.name = x.ingredient_name
WHERE pv.name = 'Grande'
AND p.name IN ('Margherita', 'Reine', 'Quatre Fromages', 'Pepperoni', 'Vegetarienne', 'Calzone Classique', 'Marinara rupture test')
AND NOT EXISTS (
    SELECT 1 FROM variant_ingredients existing
    WHERE existing.variant_id = pv.id AND existing.ingredient_id = i.id
);

DROP TABLE IF EXISTS _seed_extra_recipes;
CREATE TEMP TABLE _seed_extra_recipes (
    extra_name VARCHAR(128),
    ingredient_name VARCHAR(128),
    quantity NUMERIC(12,3)
) ON COMMIT DROP;

INSERT INTO _seed_extra_recipes VALUES
    ('Mozzarella supplementaire', 'Mozzarella', 0.050),
    ('Champignons supplementaires', 'Champignons', 0.050),
    ('Pepperoni supplementaire', 'Pepperoni', 0.040),
    ('Jambon supplementaire', 'Jambon blanc', 0.050),
    ('Olives', 'Olives noires', 0.035),
    ('Oeuf', 'Oeufs', 1.000),
    ('Sauce piquante', 'Origan', 0.003);

DO $$
DECLARE
    r RECORD;
    v_id INTEGER;
    v_extra_id INTEGER;
    v_ingredient_id INTEGER;
BEGIN
    FOR r IN SELECT * FROM _seed_extra_recipes LOOP
        SELECT id INTO v_extra_id FROM extras WHERE name = r.extra_name LIMIT 1;
        SELECT id INTO v_ingredient_id FROM ingredients WHERE name = r.ingredient_name LIMIT 1;
        SELECT id INTO v_id
        FROM extra_ingredients
        WHERE extra_id = v_extra_id AND ingredient_id = v_ingredient_id
        LIMIT 1;

        IF v_id IS NULL THEN
            INSERT INTO extra_ingredients (extra_id, ingredient_id, quantity)
            VALUES (v_extra_id, v_ingredient_id, r.quantity);
        ELSE
            UPDATE extra_ingredients SET quantity = r.quantity WHERE id = v_id;
        END IF;
    END LOOP;
END $$;

-- Lots HACCP et ajustements de stock utiles aux tests staff/admin.
DELETE FROM ingredient_batches
WHERE created_by_user_id IN (SELECT id FROM users WHERE email IN ('pizza@test.com', 'staff.cuisine@test.com'))
AND created_at >= NOW() - INTERVAL '365 days'
AND ingredient_id IN (SELECT id FROM ingredients WHERE name IN (SELECT name FROM _seed_ingredients));

INSERT INTO ingredient_batches (
    ingredient_id, quantity, received_at, expires_at, opened_at,
    use_within_hours_after_opening, status, created_by_user_id
)
SELECT i.id, x.quantity, NOW() - x.received_age, NOW() + x.expires_in, x.opened_at,
       x.use_within_hours_after_opening, x.status, u.id
FROM (VALUES
    ('Farine T00', 20.000::numeric, INTERVAL '5 days', INTERVAL '120 days', NULL::timestamptz, NULL::integer, 'sealed'),
    ('Mozzarella', 8.000::numeric, INTERVAL '1 day', INTERVAL '9 days', NOW() - INTERVAL '4 hours', 72, 'opened'),
    ('Jambon blanc', 4.000::numeric, INTERVAL '1 day', INTERVAL '5 days', NOW() - INTERVAL '2 hours', 48, 'opened'),
    ('Ail', 0.300::numeric, INTERVAL '8 days', INTERVAL '2 days', NULL::timestamptz, NULL::integer, 'sealed')
) AS x(ingredient_name, quantity, received_age, expires_in, opened_at, use_within_hours_after_opening, status)
JOIN ingredients i ON i.name = x.ingredient_name
JOIN users u ON u.email = 'staff.cuisine@test.com';

DELETE FROM stock_adjustment_requests
WHERE requested_by_user_id IN (SELECT id FROM users WHERE email IN ('staff.cuisine@test.com', 'pizza@test.com'))
AND ingredient_id IN (SELECT id FROM ingredients WHERE name IN ('Mozzarella', 'Ail', 'Jambon blanc'));

INSERT INTO stock_adjustment_requests (
    ingredient_id, quantity_delta, reason, note, status,
    requested_by_user_id, reviewed_by_user_id, reviewed_at
)
SELECT i.id, -0.350, 'waste', 'Perte test: mozzarella tombee au sol', 'pending', staff.id, NULL, NULL
FROM ingredients i
JOIN users staff ON staff.email = 'staff.cuisine@test.com'
WHERE i.name = 'Mozzarella'
UNION ALL
SELECT i.id, 0.800, 'inventory', 'Correction inventaire test validee', 'approved', staff.id, admin.id, NOW() - INTERVAL '1 day'
FROM ingredients i
JOIN users staff ON staff.email = 'staff.cuisine@test.com'
JOIN users admin ON admin.email = 'pizza@test.com'
WHERE i.name = 'Jambon blanc';

-- ---------------------------------------------------------------------------
-- 05. Allergenes, tags alimentaires, recommandations
-- ---------------------------------------------------------------------------

INSERT INTO allergen_definitions (name, slug, description, is_regulatory)
VALUES
    ('Gluten', 'gluten', 'Cereales contenant du gluten', TRUE),
    ('Crustaces', 'crustaces', 'Crustaces et produits a base de crustaces', TRUE),
    ('Oeufs', 'oeufs', 'Oeufs et produits a base d''oeufs', TRUE),
    ('Poisson', 'poisson', 'Poissons et produits a base de poissons', TRUE),
    ('Cacahuetes', 'cacahuetes', 'Arachides et produits a base d''arachides', TRUE),
    ('Soja', 'soja', 'Soja et produits a base de soja', TRUE),
    ('Lait', 'lait', 'Lait et produits laitiers', TRUE),
    ('Fruits a coque', 'fruits-a-coque', 'Noix, noisettes, amandes, etc.', TRUE),
    ('Celeri', 'celeri', 'Celeri et produits a base de celeri', TRUE),
    ('Moutarde', 'moutarde', 'Moutarde et produits a base de moutarde', TRUE),
    ('Sesame', 'sesame', 'Graines de sesame et produits a base de sesame', TRUE),
    ('Sulfites', 'sulfites', 'Anhydride sulfureux et sulfites > 10 mg/kg ou mg/litre', TRUE),
    ('Lupin', 'lupin', 'Lupin et produits a base de lupin', TRUE),
    ('Mollusques', 'mollusques', 'Mollusques et produits a base de mollusques', TRUE)
ON CONFLICT (slug) DO UPDATE
SET name = EXCLUDED.name,
    description = EXCLUDED.description,
    is_regulatory = EXCLUDED.is_regulatory;

INSERT INTO dietary_tags (name, slug)
VALUES
    ('Vegetarien', 'vegetarien'),
    ('Vegan', 'vegan'),
    ('Sans gluten', 'sans-gluten'),
    ('Halal', 'halal'),
    ('Casher', 'casher'),
    ('Sans lactose', 'sans-lactose'),
    ('Sans noix', 'sans-noix'),
    ('Bio', 'bio')
ON CONFLICT (slug) DO UPDATE
SET name = EXCLUDED.name;

INSERT INTO ingredient_allergens (ingredient_id, allergen_id, level)
SELECT i.id, a.id, x.level
FROM (VALUES
    ('Farine T00', 'gluten', 'present'),
    ('Mozzarella', 'lait', 'present'),
    ('Creme fraiche', 'lait', 'present'),
    ('Gorgonzola', 'lait', 'present'),
    ('Chevre', 'lait', 'present'),
    ('Parmesan', 'lait', 'present'),
    ('Mascarpone', 'lait', 'present'),
    ('Oeufs', 'oeufs', 'present'),
    ('Jambon blanc', 'sulfites', 'traces'),
    ('Pepperoni', 'sulfites', 'traces')
) AS x(ingredient_name, allergen_slug, level)
JOIN ingredients i ON i.name = x.ingredient_name
JOIN allergen_definitions a ON a.slug = x.allergen_slug
ON CONFLICT (ingredient_id, allergen_id) DO UPDATE
SET level = EXCLUDED.level;

-- Declaration complete des 14 allergenes reglementaires pour chaque produit actif.
INSERT INTO product_allergens (product_id, allergen_id, level, source)
SELECT p.id,
       a.id,
       CASE
           WHEN a.slug = 'gluten' AND p.name NOT IN ('Coca-Cola 33cl') THEN 'present'
           WHEN a.slug = 'lait' AND p.name IN ('Margherita', 'Reine', 'Quatre Fromages', 'Pepperoni', 'Vegetarienne', 'Calzone Classique', 'Tiramisu maison') THEN 'present'
           WHEN a.slug = 'oeufs' AND p.name IN ('Calzone Classique', 'Tiramisu maison') THEN 'present'
           WHEN a.slug = 'sulfites' AND p.name IN ('Reine', 'Pepperoni') THEN 'traces'
           ELSE 'absent'
       END,
       'ingredient'
FROM products p
CROSS JOIN allergen_definitions a
WHERE p.name IN (SELECT name FROM _seed_products)
AND a.is_regulatory IS TRUE
ON CONFLICT (product_id, allergen_id) DO UPDATE
SET level = EXCLUDED.level,
    source = EXCLUDED.source;

INSERT INTO product_dietary_tags (product_id, dietary_tag_id)
SELECT p.id, d.id
FROM products p
JOIN dietary_tags d ON (
    (p.name IN ('Margherita', 'Quatre Fromages', 'Vegetarienne', 'Marinara rupture test') AND d.slug = 'vegetarien')
    OR (p.name IN ('Marinara rupture test', 'Coca-Cola 33cl') AND d.slug = 'vegan')
    OR (p.name = 'Coca-Cola 33cl' AND d.slug = 'sans-lactose')
)
ON CONFLICT (product_id, dietary_tag_id) DO NOTHING;

INSERT INTO product_recommendations (product_id, recommended_product_id, display_order, label, is_active)
SELECT p.id, r.id, x.display_order, x.label, TRUE
FROM (VALUES
    ('Margherita', 'Coca-Cola 33cl', 1, 'Boisson conseillee'),
    ('Reine', 'Tiramisu maison', 1, 'Dessert conseille'),
    ('Pepperoni', 'Coca-Cola 33cl', 1, 'Combo epice'),
    ('Vegetarienne', 'Tiramisu maison', 1, 'Finir sur une note douce')
) AS x(product_name, recommended_name, display_order, label)
JOIN products p ON p.name = x.product_name
JOIN products r ON r.name = x.recommended_name
ON CONFLICT (product_id, recommended_product_id) DO UPDATE
SET display_order = EXCLUDED.display_order,
    label = EXCLUDED.label,
    is_active = TRUE;

INSERT INTO catalog_price_audits (entity_type, entity_id, old_price, new_price, changed_by_user_id, source, reason)
SELECT 'product', p.id, NULL, p.base_price, u.id, 'admin', 'Seed PizzaTEST'
FROM products p
JOIN users u ON u.email = 'pizza@test.com'
WHERE p.name IN (SELECT name FROM _seed_products)
AND NOT EXISTS (
    SELECT 1 FROM catalog_price_audits c
    WHERE c.entity_type = 'product'
    AND c.entity_id = p.id
    AND c.reason = 'Seed PizzaTEST'
);

-- ---------------------------------------------------------------------------
-- 06. Livraison, promotions, fidelite
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    v_id INTEGER;
BEGIN
    SELECT id INTO v_id FROM delivery_zones WHERE name = 'Centre-ville' LIMIT 1;
    IF v_id IS NULL THEN
        INSERT INTO delivery_zones (name, polygon, fee, min_order_amount, estimated_minutes, is_active)
        VALUES (
            'Centre-ville',
            '{"type":"Polygon","coordinates":[[[2.330,48.845],[2.385,48.845],[2.385,48.875],[2.330,48.875],[2.330,48.845]]]}'::json,
            2.50,
            12.00,
            20,
            TRUE
        );
    ELSE
        UPDATE delivery_zones
        SET polygon = '{"type":"Polygon","coordinates":[[[2.330,48.845],[2.385,48.845],[2.385,48.875],[2.330,48.875],[2.330,48.845]]]}'::json,
            fee = 2.50,
            min_order_amount = 12.00,
            estimated_minutes = 20,
            is_active = TRUE
        WHERE id = v_id;
    END IF;

    SELECT id INTO v_id FROM delivery_zones WHERE name = 'Peripherie' LIMIT 1;
    IF v_id IS NULL THEN
        INSERT INTO delivery_zones (name, polygon, fee, min_order_amount, estimated_minutes, is_active)
        VALUES (
            'Peripherie',
            '{"type":"Polygon","coordinates":[[[2.280,48.815],[2.430,48.815],[2.430,48.900],[2.280,48.900],[2.280,48.815]]]}'::json,
            4.00,
            22.00,
            35,
            TRUE
        );
    ELSE
        UPDATE delivery_zones
        SET polygon = '{"type":"Polygon","coordinates":[[[2.280,48.815],[2.430,48.815],[2.430,48.900],[2.280,48.900],[2.280,48.815]]]}'::json,
            fee = 4.00,
            min_order_amount = 22.00,
            estimated_minutes = 35,
            is_active = TRUE
        WHERE id = v_id;
    END IF;
END $$;

INSERT INTO loyalty_config (
    id, base_ratio, points_expiry_days, points_to_euro_rate,
    max_cumulative_multiplier, is_active, updated_at
)
VALUES (1, 1.0000, 365, 0.0100, 20.00, TRUE, NOW())
ON CONFLICT (id) DO UPDATE
SET base_ratio = EXCLUDED.base_ratio,
    points_expiry_days = EXCLUDED.points_expiry_days,
    points_to_euro_rate = EXCLUDED.points_to_euro_rate,
    max_cumulative_multiplier = EXCLUDED.max_cumulative_multiplier,
    is_active = TRUE,
    updated_at = NOW();

DO $$
DECLARE
    v_campaign_id INTEGER;
    v_admin_id INTEGER;
BEGIN
    SELECT id INTO v_admin_id FROM users WHERE email = 'pizza@test.com';
    SELECT id INTO v_campaign_id FROM promotion_campaigns WHERE name = 'Seed PizzaTEST ouverture' LIMIT 1;
    IF v_campaign_id IS NULL THEN
        INSERT INTO promotion_campaigns (name, prefix, description, created_by_user_id)
        VALUES ('Seed PizzaTEST ouverture', 'PIZZA', 'Campagne de test pour parcours commandes', v_admin_id)
        RETURNING id INTO v_campaign_id;
    ELSE
        UPDATE promotion_campaigns
        SET prefix = 'PIZZA',
            description = 'Campagne de test pour parcours commandes',
            created_by_user_id = v_admin_id
        WHERE id = v_campaign_id;
    END IF;

    INSERT INTO promotions (
        code, description, discount_type, discount_value, min_order_amount,
        starts_at, ends_at, is_active, max_uses, max_uses_per_user,
        current_uses, first_order_only, campaign_id, user_id, is_public,
        is_stackable, email_verified_required
    )
    VALUES
        ('PIZZA10', '10 pourcent sur les pizzas rouges', 'percent', 10.00, 15.00, NOW() - INTERVAL '1 day', NOW() + INTERVAL '30 days', TRUE, 100, 1, 0, FALSE, v_campaign_id, NULL, TRUE, FALSE, FALSE),
        ('WELCOME5', '5 EUR sur une premiere commande', 'fixed', 5.00, 20.00, NOW() - INTERVAL '1 day', NOW() + INTERVAL '60 days', TRUE, 100, 1, 0, TRUE, v_campaign_id, NULL, TRUE, FALSE, TRUE)
    ON CONFLICT (code) DO UPDATE
    SET description = EXCLUDED.description,
        discount_type = EXCLUDED.discount_type,
        discount_value = EXCLUDED.discount_value,
        min_order_amount = EXCLUDED.min_order_amount,
        starts_at = EXCLUDED.starts_at,
        ends_at = EXCLUDED.ends_at,
        is_active = EXCLUDED.is_active,
        max_uses = EXCLUDED.max_uses,
        max_uses_per_user = EXCLUDED.max_uses_per_user,
        current_uses = EXCLUDED.current_uses,
        first_order_only = EXCLUDED.first_order_only,
        campaign_id = EXCLUDED.campaign_id,
        user_id = EXCLUDED.user_id,
        is_public = EXCLUDED.is_public,
        is_stackable = EXCLUDED.is_stackable,
        email_verified_required = EXCLUDED.email_verified_required;
END $$;

INSERT INTO promotion_target_categories (promotion_id, category_id)
SELECT promo.id, cat.id
FROM promotions promo
JOIN categories cat ON cat.name = 'Pizzas rouges'
WHERE promo.code = 'PIZZA10'
ON CONFLICT (promotion_id, category_id) DO NOTHING;

INSERT INTO promotion_target_products (promotion_id, product_id)
SELECT promo.id, p.id
FROM promotions promo
JOIN products p ON p.name = 'Vegetarienne'
WHERE promo.code = 'WELCOME5'
ON CONFLICT (promotion_id, product_id) DO NOTHING;

DO $$
DECLARE
    v_id INTEGER;
    v_cat_id INTEGER;
    v_product_id INTEGER;
BEGIN
    SELECT id INTO v_cat_id FROM categories WHERE name = 'Pizzas rouges';
    SELECT id INTO v_id FROM loyalty_rules WHERE name = 'Bonus pizzas rouges' LIMIT 1;
    IF v_id IS NULL THEN
        INSERT INTO loyalty_rules (name, rule_type, category_id, multiplier, priority, is_active)
        VALUES ('Bonus pizzas rouges', 'category_multiplier', v_cat_id, 1.5000, 10, TRUE);
    ELSE
        UPDATE loyalty_rules
        SET rule_type = 'category_multiplier',
            category_id = v_cat_id,
            multiplier = 1.5000,
            priority = 10,
            is_active = TRUE
        WHERE id = v_id;
    END IF;

    SELECT id INTO v_id FROM loyalty_rules WHERE name = 'Bonus week-end' LIMIT 1;
    IF v_id IS NULL THEN
        INSERT INTO loyalty_rules (name, rule_type, days_of_week, multiplier, priority, is_active)
        VALUES ('Bonus week-end', 'day_multiplier', ARRAY[5,6], 2.0000, 20, TRUE);
    ELSE
        UPDATE loyalty_rules
        SET rule_type = 'day_multiplier',
            days_of_week = ARRAY[5,6],
            multiplier = 2.0000,
            priority = 20,
            is_active = TRUE
        WHERE id = v_id;
    END IF;

    SELECT id INTO v_product_id FROM products WHERE name = 'Margherita';

    SELECT id INTO v_id FROM loyalty_rewards WHERE name = '5 EUR de remise' LIMIT 1;
    IF v_id IS NULL THEN
        INSERT INTO loyalty_rewards (name, reward_type, points_required, discount_amount, product_id, is_active)
        VALUES ('5 EUR de remise', 'discount_euros', 500, 5.00, NULL, TRUE);
    ELSE
        UPDATE loyalty_rewards
        SET reward_type = 'discount_euros',
            points_required = 500,
            discount_amount = 5.00,
            product_id = NULL,
            is_active = TRUE
        WHERE id = v_id;
    END IF;

    SELECT id INTO v_id FROM loyalty_rewards WHERE name = 'Margherita offerte' LIMIT 1;
    IF v_id IS NULL THEN
        INSERT INTO loyalty_rewards (name, reward_type, points_required, discount_amount, product_id, is_active)
        VALUES ('Margherita offerte', 'free_product', 900, NULL, v_product_id, TRUE);
    ELSE
        UPDATE loyalty_rewards
        SET reward_type = 'free_product',
            points_required = 900,
            discount_amount = NULL,
            product_id = v_product_id,
            is_active = TRUE
        WHERE id = v_id;
    END IF;
END $$;

INSERT INTO loyalty_accounts (user_id, points)
SELECT u.id, x.points
FROM (VALUES
    ('client.alice@test.com', 240),
    ('client.yanis@test.com', 780)
) AS x(email, points)
JOIN users u ON u.email = x.email
ON CONFLICT (user_id) DO UPDATE
SET points = EXCLUDED.points;

DELETE FROM loyalty_transactions
WHERE reason LIKE 'seed_%'
AND account_id IN (
    SELECT la.id FROM loyalty_accounts la
    JOIN users u ON u.id = la.user_id
    WHERE u.email IN ('client.alice@test.com', 'client.yanis@test.com')
);

INSERT INTO loyalty_transactions (
    account_id, points_delta, reason, transaction_type, source, changed_by_user_id, metadata
)
SELECT la.id, x.points_delta, x.reason, x.transaction_type, x.source, admin.id, x.metadata::jsonb
FROM (VALUES
    ('client.alice@test.com', 240, 'seed_initial_points_alice', 'manual', 'admin', '{"note":"points de test"}'),
    ('client.yanis@test.com', 780, 'seed_initial_points_yanis', 'manual', 'admin', '{"note":"points de test"}')
) AS x(email, points_delta, reason, transaction_type, source, metadata)
JOIN users u ON u.email = x.email
JOIN loyalty_accounts la ON la.user_id = u.id
JOIN users admin ON admin.email = 'pizza@test.com';

-- ---------------------------------------------------------------------------
-- 07. Commandes et paiements de test
-- ---------------------------------------------------------------------------

DROP TABLE IF EXISTS _seed_existing_orders;
CREATE TEMP TABLE _seed_existing_orders (id INTEGER PRIMARY KEY) ON COMMIT DROP;

INSERT INTO _seed_existing_orders (id)
SELECT id FROM orders
WHERE idempotency_key LIKE 'seed-pizza-test-%';

DELETE FROM refunds
WHERE payment_id IN (SELECT id FROM payments WHERE order_id IN (SELECT id FROM _seed_existing_orders));
DELETE FROM loyalty_point_reservations WHERE order_id IN (SELECT id FROM _seed_existing_orders);
DELETE FROM promo_code_usages WHERE order_id IN (SELECT id FROM _seed_existing_orders);
DELETE FROM payments WHERE order_id IN (SELECT id FROM _seed_existing_orders);
DELETE FROM order_items WHERE order_id IN (SELECT id FROM _seed_existing_orders);
DELETE FROM order_status_history WHERE order_id IN (SELECT id FROM _seed_existing_orders);
DELETE FROM stock_movements
WHERE reason IN (
    SELECT 'order:' || id::text FROM _seed_existing_orders
    UNION ALL
    SELECT 'cancel:' || id::text FROM _seed_existing_orders
)
OR reason IN ('seed_supply', 'seed_waste');
DELETE FROM orders WHERE id IN (SELECT id FROM _seed_existing_orders);

DROP TABLE IF EXISTS _seed_order_map;
CREATE TEMP TABLE _seed_order_map (
    key VARCHAR(128) PRIMARY KEY,
    order_id INTEGER NOT NULL
) ON COMMIT DROP;

DO $$
DECLARE
    v_alice_id INTEGER;
    v_yanis_id INTEGER;
    v_staff_id INTEGER;
    v_admin_id INTEGER;
    v_zone_centre_id INTEGER;
    v_order_id INTEGER;
BEGIN
    SELECT id INTO v_alice_id FROM users WHERE email = 'client.alice@test.com';
    SELECT id INTO v_yanis_id FROM users WHERE email = 'client.yanis@test.com';
    SELECT id INTO v_staff_id FROM users WHERE email = 'staff.cuisine@test.com';
    SELECT id INTO v_admin_id FROM users WHERE email = 'pizza@test.com';
    SELECT id INTO v_zone_centre_id FROM delivery_zones WHERE name = 'Centre-ville';

    INSERT INTO orders (
        user_id, customer_email, customer_name, customer_phone, order_type,
        status, payment_status, source, created_by_user_id, subtotal,
        discount_total, delivery_fee, total, delivery_address, delivery_zone_id,
        estimated_delivery_at, idempotency_key, promo_code, created_at
    )
    VALUES (
        v_alice_id, 'client.alice@test.com', 'Alice Martin', '+33600000011',
        'delivery', 'pending', 'pending', 'customer', NULL,
        16.40, 0.00, 2.50, 18.90,
        '15 rue Exemple, 75001 Paris', v_zone_centre_id,
        NOW() + INTERVAL '45 minutes', 'seed-pizza-test-pending-alice', NULL,
        NOW() - INTERVAL '20 minutes'
    )
    RETURNING id INTO v_order_id;
    INSERT INTO _seed_order_map VALUES ('pending-alice', v_order_id);

    INSERT INTO orders (
        user_id, customer_email, customer_name, customer_phone, order_type,
        status, payment_status, source, created_by_user_id, subtotal,
        discount_total, delivery_fee, total, delivery_address, delivery_zone_id,
        estimated_delivery_at, idempotency_key, promo_code, created_at
    )
    VALUES (
        v_alice_id, 'client.alice@test.com', 'Alice Martin', '+33600000011',
        'delivery', 'confirmed', 'paid', 'customer', NULL,
        22.40, 2.24, 2.50, 22.66,
        '15 rue Exemple, 75001 Paris', v_zone_centre_id,
        NOW() + INTERVAL '35 minutes', 'seed-pizza-test-confirmed-alice', 'PIZZA10',
        NOW() - INTERVAL '12 minutes'
    )
    RETURNING id INTO v_order_id;
    INSERT INTO _seed_order_map VALUES ('confirmed-alice', v_order_id);

    INSERT INTO orders (
        user_id, customer_email, customer_name, customer_phone, order_type,
        status, payment_status, source, created_by_user_id, subtotal,
        discount_total, delivery_fee, total, delivery_address, delivery_zone_id,
        table_number, estimated_delivery_at, idempotency_key, promo_code, created_at
    )
    VALUES (
        NULL, 'manual.table7@test.com', 'Table 7', NULL,
        'dine_in', 'preparing', 'paid', 'manual', v_staff_id,
        26.20, 0.00, 0.00, 26.20,
        NULL, NULL, '7', NOW() + INTERVAL '18 minutes',
        'seed-pizza-test-manual-table7', NULL, NOW() - INTERVAL '8 minutes'
    )
    RETURNING id INTO v_order_id;
    INSERT INTO _seed_order_map VALUES ('manual-table7', v_order_id);

    INSERT INTO orders (
        user_id, customer_email, customer_name, customer_phone, order_type,
        status, payment_status, source, created_by_user_id, subtotal,
        discount_total, delivery_fee, total, delivery_address, delivery_zone_id,
        estimated_delivery_at, idempotency_key, promo_code, created_at
    )
    VALUES (
        v_yanis_id, 'client.yanis@test.com', 'Yanis Petit', '+33600000012',
        'pickup', 'delivered', 'paid', 'customer', NULL,
        29.40, 5.00, 0.00, 24.40,
        NULL, NULL, NOW() - INTERVAL '40 minutes',
        'seed-pizza-test-delivered-yanis', 'WELCOME5',
        NOW() - INTERVAL '90 minutes'
    )
    RETURNING id INTO v_order_id;
    INSERT INTO _seed_order_map VALUES ('delivered-yanis', v_order_id);

    INSERT INTO orders (
        user_id, customer_email, customer_name, customer_phone, order_type,
        status, payment_status, source, created_by_user_id, subtotal,
        discount_total, delivery_fee, total, delivery_address, delivery_zone_id,
        estimated_delivery_at, idempotency_key, promo_code, created_at
    )
    VALUES (
        v_yanis_id, 'client.yanis@test.com', 'Yanis Petit', '+33600000012',
        'delivery', 'cancelled', 'failed', 'customer', NULL,
        12.50, 0.00, 2.50, 15.00,
        '30 avenue Fictive, 75002 Paris', v_zone_centre_id,
        NULL, 'seed-pizza-test-cancelled-failed-yanis', NULL,
        NOW() - INTERVAL '2 hours'
    )
    RETURNING id INTO v_order_id;
    INSERT INTO _seed_order_map VALUES ('cancelled-failed-yanis', v_order_id);
END $$;

-- Items des commandes seed.
INSERT INTO order_items (
    order_id, product_id, variant_id, product_name_snapshot, variant_name_snapshot,
    extras_snapshot, extras_total, quantity, unit_price, total,
    preparation_status, preparation_station, prepared_at, prepared_by_user_id
)
SELECT m.order_id, p.id, pv.id, p.name, pv.name,
       json_build_array(json_build_object('extra_id', e.id, 'name', e.name, 'quantity', 1, 'unit_price', 1.50, 'total', 1.50)),
       1.50, 1, 11.40, 11.40, 'pending', 'kitchen', NULL, NULL
FROM _seed_order_map m
JOIN products p ON p.name = 'Margherita'
JOIN product_variants pv ON pv.product_id = p.id AND pv.name = 'Classique'
JOIN extras e ON e.name = 'Mozzarella supplementaire'
WHERE m.key = 'pending-alice';

INSERT INTO order_items (
    order_id, product_id, variant_id, product_name_snapshot, variant_name_snapshot,
    extras_snapshot, extras_total, quantity, unit_price, total,
    preparation_status, preparation_station
)
SELECT m.order_id, p.id, NULL, p.name, NULL, '[]'::json, 0.00, 2, 2.50, 5.00, 'pending', 'counter'
FROM _seed_order_map m
JOIN products p ON p.name = 'Coca-Cola 33cl'
WHERE m.key = 'pending-alice';

INSERT INTO order_items (
    order_id, product_id, variant_id, product_name_snapshot, variant_name_snapshot,
    extras_snapshot, extras_total, quantity, unit_price, total,
    preparation_status, preparation_station
)
SELECT m.order_id, p.id, pv.id, p.name, pv.name,
       json_build_array(json_build_object('extra_id', e.id, 'name', e.name, 'quantity', 1, 'unit_price', 1.00, 'total', 1.00)),
       1.00, 1, 17.50, 17.50, 'pending', 'kitchen'
FROM _seed_order_map m
JOIN products p ON p.name = 'Reine'
JOIN product_variants pv ON pv.product_id = p.id AND pv.name = 'Grande'
JOIN extras e ON e.name = 'Champignons supplementaires'
WHERE m.key = 'confirmed-alice';

INSERT INTO order_items (
    order_id, product_id, variant_id, product_name_snapshot, variant_name_snapshot,
    extras_snapshot, extras_total, quantity, unit_price, total,
    preparation_status, preparation_station
)
SELECT m.order_id, p.id, NULL, p.name, NULL, '[]'::json, 0.00, 1, 4.90, 4.90, 'pending', 'counter'
FROM _seed_order_map m
JOIN products p ON p.name = 'Tiramisu maison'
WHERE m.key = 'confirmed-alice';

INSERT INTO order_items (
    order_id, product_id, variant_id, product_name_snapshot, variant_name_snapshot,
    extras_snapshot, extras_total, quantity, unit_price, total,
    preparation_status, preparation_station
)
SELECT m.order_id, p.id, pv.id, p.name, pv.name,
       json_build_array(json_build_object('extra_id', e.id, 'name', e.name, 'quantity', 1, 'unit_price', 0.80, 'total', 0.80)),
       0.80, 1, 14.70, 14.70, 'preparing', 'kitchen'
FROM _seed_order_map m
JOIN products p ON p.name = 'Quatre Fromages'
JOIN product_variants pv ON pv.product_id = p.id AND pv.name = 'Classique'
JOIN extras e ON e.name = 'Olives'
WHERE m.key = 'manual-table7';

INSERT INTO order_items (
    order_id, product_id, variant_id, product_name_snapshot, variant_name_snapshot,
    extras_snapshot, extras_total, quantity, unit_price, total,
    preparation_status, preparation_station
)
SELECT m.order_id, p.id, pv.id, p.name, pv.name, '[]'::json,
       0.00, 1, 11.50, 11.50, 'pending', 'kitchen'
FROM _seed_order_map m
JOIN products p ON p.name = 'Pepperoni'
JOIN product_variants pv ON pv.product_id = p.id AND pv.name = 'Petite'
WHERE m.key = 'manual-table7';

INSERT INTO order_items (
    order_id, product_id, variant_id, product_name_snapshot, variant_name_snapshot,
    extras_snapshot, extras_total, quantity, unit_price, total,
    preparation_status, preparation_station, prepared_at,
    prepared_by_user_id
)
SELECT m.order_id, p.id, pv.id, p.name, pv.name,
       json_build_array(json_build_object('extra_id', e.id, 'name', e.name, 'quantity', 1, 'unit_price', 0.50, 'total', 0.50)),
       0.50, 1, 17.40, 17.40, 'ready', 'kitchen', NOW() - INTERVAL '55 minutes', staff.id
FROM _seed_order_map m
JOIN products p ON p.name = 'Vegetarienne'
JOIN product_variants pv ON pv.product_id = p.id AND pv.name = 'Grande'
JOIN extras e ON e.name = 'Sauce piquante'
JOIN users staff ON staff.email = 'staff.cuisine@test.com'
WHERE m.key = 'delivered-yanis';

INSERT INTO order_items (
    order_id, product_id, variant_id, product_name_snapshot, variant_name_snapshot,
    extras_snapshot, extras_total, quantity, unit_price, total,
    preparation_status, preparation_station, prepared_at,
    prepared_by_user_id
)
SELECT m.order_id, p.id, pv.id, p.name, pv.name, '[]'::json,
       0.00, 1, 12.00, 12.00, 'ready', 'kitchen', NOW() - INTERVAL '55 minutes', staff.id
FROM _seed_order_map m
JOIN products p ON p.name = 'Calzone Classique'
JOIN product_variants pv ON pv.product_id = p.id AND pv.name = 'Classique'
JOIN users staff ON staff.email = 'staff.cuisine@test.com'
WHERE m.key = 'delivered-yanis';

INSERT INTO order_items (
    order_id, product_id, variant_id, product_name_snapshot, variant_name_snapshot,
    extras_snapshot, extras_total, quantity, unit_price, total,
    preparation_status, preparation_station
)
SELECT m.order_id, p.id, pv.id, p.name, pv.name, '[]'::json,
       0.00, 1, 12.50, 12.50, 'pending', 'kitchen'
FROM _seed_order_map m
JOIN products p ON p.name = 'Reine'
JOIN product_variants pv ON pv.product_id = p.id AND pv.name = 'Classique'
WHERE m.key = 'cancelled-failed-yanis';

-- Historique de statuts.
INSERT INTO order_status_history (order_id, status, note, created_at)
SELECT order_id, status, note, created_at
FROM (
    SELECT m.order_id, 'pending'::varchar AS status, 'Seed: commande creee' AS note, o.created_at AS created_at
    FROM _seed_order_map m JOIN orders o ON o.id = m.order_id
    UNION ALL
    SELECT m.order_id, 'confirmed', 'Seed: paiement valide', o.created_at + INTERVAL '2 minutes'
    FROM _seed_order_map m JOIN orders o ON o.id = m.order_id WHERE m.key IN ('confirmed-alice', 'manual-table7', 'delivered-yanis')
    UNION ALL
    SELECT m.order_id, 'preparing', 'Seed: preparation demarree', o.created_at + INTERVAL '5 minutes'
    FROM _seed_order_map m JOIN orders o ON o.id = m.order_id WHERE m.key IN ('manual-table7', 'delivered-yanis')
    UNION ALL
    SELECT m.order_id, 'ready', 'Seed: commande prete', o.created_at + INTERVAL '25 minutes'
    FROM _seed_order_map m JOIN orders o ON o.id = m.order_id WHERE m.key = 'delivered-yanis'
    UNION ALL
    SELECT m.order_id, 'delivered', 'Seed: commande livree', o.created_at + INTERVAL '50 minutes'
    FROM _seed_order_map m JOIN orders o ON o.id = m.order_id WHERE m.key = 'delivered-yanis'
    UNION ALL
    SELECT m.order_id, 'cancelled', 'Seed: paiement refuse', o.created_at + INTERVAL '4 minutes'
    FROM _seed_order_map m JOIN orders o ON o.id = m.order_id WHERE m.key = 'cancelled-failed-yanis'
) AS history;

-- Paiements: local_* est finalisable en environnement local/dev/test par le code.
INSERT INTO payments (
    order_id, provider, provider_payment_id, provider_account_id,
    external_reference, amount, amount_received, currency, status,
    expires_at, created_by_user_id, created_at
)
SELECT m.order_id, 'stripe', 'pi_seed_pizza_test_pending_001', 'acct_pizza_test_seed',
       NULL::varchar, 18.90::numeric, NULL::numeric, 'EUR', 'pending',
       NOW() + INTERVAL '24 hours', NULL::integer, NOW() - INTERVAL '19 minutes'
FROM _seed_order_map m WHERE m.key = 'pending-alice'
UNION ALL
SELECT m.order_id, 'stripe', 'local_seed_paid_001', 'acct_pizza_test_seed',
       NULL::varchar, 22.66::numeric, 22.66::numeric, 'EUR', 'paid',
       NULL::timestamptz, NULL::integer, NOW() - INTERVAL '10 minutes'
FROM _seed_order_map m WHERE m.key = 'confirmed-alice'
UNION ALL
SELECT m.order_id, 'cash', NULL::varchar, NULL::varchar,
       'CASH-TABLE-7-SEED', 26.20::numeric, 30.00::numeric, 'EUR', 'paid',
       NULL::timestamptz, staff.id, NOW() - INTERVAL '7 minutes'
FROM _seed_order_map m JOIN users staff ON staff.email = 'staff.cuisine@test.com' WHERE m.key = 'manual-table7'
UNION ALL
SELECT m.order_id, 'stripe', 'local_seed_paid_002', 'acct_pizza_test_seed',
       NULL::varchar, 24.40::numeric, 24.40::numeric, 'EUR', 'partially_refunded',
       NULL::timestamptz, NULL::integer, NOW() - INTERVAL '85 minutes'
FROM _seed_order_map m WHERE m.key = 'delivered-yanis'
UNION ALL
SELECT m.order_id, 'stripe', 'pi_seed_pizza_test_failed_001', 'acct_pizza_test_seed',
       NULL::varchar, 15.00::numeric, 0.00::numeric, 'EUR', 'failed',
       NULL::timestamptz, NULL::integer, NOW() - INTERVAL '115 minutes'
FROM _seed_order_map m WHERE m.key = 'cancelled-failed-yanis';

INSERT INTO refunds (
    order_id, payment_id, stripe_refund_id, amount, reason, status,
    failure_reason, created_by_user_id, created_at
)
SELECT o.id, p.id, 're_seed_pizza_test_partial_001', 500,
       'Geste commercial seed', 'succeeded', NULL, admin.id, NOW() - INTERVAL '30 minutes'
FROM orders o
JOIN payments p ON p.order_id = o.id
JOIN users admin ON admin.email = 'pizza@test.com'
WHERE o.idempotency_key = 'seed-pizza-test-delivered-yanis';

INSERT INTO processed_webhook_events (stripe_event_id, event_type, processed_at)
VALUES
    ('evt_seed_pizza_test_paid_001', 'payment_intent.succeeded', NOW() - INTERVAL '10 minutes'),
    ('evt_seed_pizza_test_failed_001', 'payment_intent.payment_failed', NOW() - INTERVAL '115 minutes')
ON CONFLICT (stripe_event_id) DO UPDATE
SET event_type = EXCLUDED.event_type,
    processed_at = EXCLUDED.processed_at;

-- Utilisation promo coherente avec les commandes seed.
INSERT INTO promo_code_usages (promo_code_id, user_id, order_id, used_at)
SELECT promo.id, o.user_id, o.id, o.created_at + INTERVAL '2 minutes'
FROM orders o
JOIN promotions promo ON promo.code = o.promo_code
WHERE o.idempotency_key IN ('seed-pizza-test-confirmed-alice', 'seed-pizza-test-delivered-yanis')
AND o.user_id IS NOT NULL
ON CONFLICT (promo_code_id, order_id) DO NOTHING;

UPDATE promotions p
SET current_uses = usage_counts.count_used
FROM (
    SELECT promo_code_id, COUNT(*)::integer AS count_used
    FROM promo_code_usages
    GROUP BY promo_code_id
) usage_counts
WHERE p.id = usage_counts.promo_code_id
AND p.code IN ('PIZZA10', 'WELCOME5');

-- Reservation fidelite checkout active pour tester abandon/panier.
INSERT INTO loyalty_point_reservations (
    user_id, order_id, points_reserved, discount_amount, status,
    expires_at, created_at
)
SELECT o.user_id, o.id, 100, 1.00, 'reserved', NOW() + INTERVAL '30 minutes', NOW() - INTERVAL '5 minutes'
FROM orders o
WHERE o.idempotency_key = 'seed-pizza-test-pending-alice';

-- Mouvements de stock: approvisionnement seed puis deduction des commandes confirmees/livrees.
INSERT INTO stock_movements (ingredient_id, quantity_delta, reason, user_id, created_at)
SELECT i.id, s.opening_qty, 'seed_supply', admin.id, NOW() - INTERVAL '1 day'
FROM _seed_ingredients s
JOIN ingredients i ON i.name = s.name
JOIN users admin ON admin.email = 'pizza@test.com';

INSERT INTO stock_movements (ingredient_id, quantity_delta, reason, user_id, created_at)
SELECT ingredient_id,
       -SUM(required_qty)::numeric(12,3),
       'order:' || order_id::text,
       (SELECT id FROM users WHERE email = 'staff.cuisine@test.com'),
       NOW()
FROM (
    SELECT oi.order_id, pi.ingredient_id, pi.quantity * oi.quantity AS required_qty
    FROM order_items oi
    JOIN orders o ON o.id = oi.order_id
    JOIN product_ingredients pi ON pi.product_id = oi.product_id
    WHERE o.idempotency_key LIKE 'seed-pizza-test-%'
    AND o.status IN ('confirmed', 'preparing', 'ready', 'out_for_delivery', 'delivered')

    UNION ALL

    SELECT oi.order_id, vi.ingredient_id, vi.quantity * oi.quantity AS required_qty
    FROM order_items oi
    JOIN orders o ON o.id = oi.order_id
    JOIN variant_ingredients vi ON vi.variant_id = oi.variant_id
    WHERE o.idempotency_key LIKE 'seed-pizza-test-%'
    AND o.status IN ('confirmed', 'preparing', 'ready', 'out_for_delivery', 'delivered')

    UNION ALL

    SELECT oi.order_id,
           ei.ingredient_id,
           ei.quantity * oi.quantity * ((extra_item.value ->> 'quantity')::integer) AS required_qty
    FROM order_items oi
    JOIN orders o ON o.id = oi.order_id
    CROSS JOIN LATERAL json_array_elements(COALESCE(oi.extras_snapshot, '[]'::json)) AS extra_item(value)
    JOIN extra_ingredients ei ON ei.extra_id = ((extra_item.value ->> 'extra_id')::integer)
    WHERE o.idempotency_key LIKE 'seed-pizza-test-%'
    AND o.status IN ('confirmed', 'preparing', 'ready', 'out_for_delivery', 'delivered')
) AS recipe_deltas
GROUP BY order_id, ingredient_id;

UPDATE ingredients i
SET current_qty = GREATEST(0, totals.current_qty)::numeric(12,3)
FROM (
    SELECT ingredient_id, SUM(quantity_delta) AS current_qty
    FROM stock_movements
    WHERE reason = 'seed_supply'
    OR reason IN (
        SELECT 'order:' || id::text
        FROM orders
        WHERE idempotency_key LIKE 'seed-pizza-test-%'
    )
    GROUP BY ingredient_id
) totals
WHERE i.id = totals.ingredient_id;

-- Force un scenario de rupture lisible pour GET /stock/alerts et disponibilite.
INSERT INTO stock_movements (ingredient_id, quantity_delta, reason, user_id, created_at)
SELECT i.id, -0.280, 'seed_waste', staff.id, NOW() - INTERVAL '6 hours'
FROM ingredients i
JOIN users staff ON staff.email = 'staff.cuisine@test.com'
WHERE i.name = 'Ail';

UPDATE ingredients SET current_qty = 0.020 WHERE name = 'Ail';

DELETE FROM product_stock
WHERE product_id IN (SELECT id FROM products WHERE name IN (SELECT name FROM _seed_products));

INSERT INTO product_stock (product_id, available_qty)
SELECT p.id,
       CASE p.name
           WHEN 'Marinara rupture test' THEN 0
           WHEN 'Coca-Cola 33cl' THEN 96
           WHEN 'Tiramisu maison' THEN 25
           ELSE 45
       END
FROM products p
WHERE p.name IN (SELECT name FROM _seed_products);

-- Token push factice pour tester les endpoints notifications/devices.
INSERT INTO device_tokens (user_id, platform, token, device_name, is_active, last_used_at)
SELECT u.id, 'ios', 'apns_seed_pizza_test_alice', 'iPhone test Alice', TRUE, NOW() - INTERVAL '1 day'
FROM users u
WHERE u.email = 'client.alice@test.com'
ON CONFLICT (user_id, token) DO UPDATE
SET platform = EXCLUDED.platform,
    device_name = EXCLUDED.device_name,
    is_active = TRUE,
    last_used_at = EXCLUDED.last_used_at;

COMMIT;

-- ---------------------------------------------------------------------------
-- 08. Requetes de verification
-- ---------------------------------------------------------------------------

SET search_path TO tenant_pizza_test, public;

SELECT 'tenant_public' AS check_name, t.id, t.slug, t.name, t.is_suspended
FROM public.tenants t
WHERE t.slug = 'pizza_test';

SELECT 'users_by_role' AS check_name, role, COUNT(*) AS count
FROM users
GROUP BY role
ORDER BY role;

SELECT 'catalog_counts' AS check_name,
       (SELECT COUNT(*) FROM categories) AS categories,
       (SELECT COUNT(*) FROM products WHERE is_active) AS active_products,
       (SELECT COUNT(*) FROM product_variants) AS variants,
       (SELECT COUNT(*) FROM extras WHERE is_active) AS extras,
       (SELECT COUNT(*) FROM product_extras) AS product_extra_links;

SELECT 'catalog_images' AS check_name,
       COUNT(*) FILTER (WHERE p.image_url LIKE '%w_800,q_auto,f_auto%') AS optimized_image_urls,
       COUNT(*) FILTER (WHERE mi.id IS NOT NULL AND mi.url_thumbnail LIKE '%c_fill,w_300,h_300,q_auto,f_auto%') AS primary_media_with_thumbnail,
       COUNT(*) AS total_products
FROM products p
LEFT JOIN media_images mi
  ON mi.entity_type = 'product'
 AND mi.entity_id = p.id
 AND mi.is_primary = TRUE;

SELECT 'stock_counts' AS check_name,
       (SELECT COUNT(*) FROM ingredients) AS ingredients,
       (SELECT COUNT(*) FROM product_ingredients) AS product_recipes,
       (SELECT COUNT(*) FROM variant_ingredients) AS variant_recipes,
       (SELECT COUNT(*) FROM extra_ingredients) AS extra_recipes,
       (SELECT COUNT(*) FROM stock_movements) AS stock_movements,
       (SELECT COUNT(*) FROM ingredients WHERE current_qty < alert_threshold) AS low_stock_alerts;

SELECT 'orders_by_status' AS check_name, status, payment_status, COUNT(*) AS count
FROM orders
WHERE idempotency_key LIKE 'seed-pizza-test-%'
GROUP BY status, payment_status
ORDER BY status, payment_status;

SELECT 'payments_by_status' AS check_name, provider, status, COUNT(*) AS count, SUM(amount) AS amount_eur
FROM payments
WHERE order_id IN (SELECT id FROM orders WHERE idempotency_key LIKE 'seed-pizza-test-%')
GROUP BY provider, status
ORDER BY provider, status;

SELECT 'seed_login' AS check_name, email, role, email_verified_at IS NOT NULL AS email_verified
FROM users
WHERE email IN ('pizza@test.com', 'staff.cuisine@test.com', 'client.alice@test.com')
ORDER BY role, email;
