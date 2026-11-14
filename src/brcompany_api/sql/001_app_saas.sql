-- 001_app_saas.sql
-- PanoramaPJ SaaS Transactional Schema (isolated from Receita Federal ETL blue/green swap)

CREATE SCHEMA IF NOT EXISTS app_saas;

-- 1. Platform-wide Administrative Settings
CREATE TABLE IF NOT EXISTS app_saas.platform_settings (
    key TEXT PRIMARY KEY,
    value_json JSONB NOT NULL,
    updated_by UUID NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

INSERT INTO app_saas.platform_settings (key, value_json)
VALUES ('export_deduplication_enabled', 'true'::jsonb)
ON CONFLICT (key) DO NOTHING;

-- 2. Users
CREATE TABLE IF NOT EXISTS app_saas.users (
    id UUID PRIMARY KEY,
    email VARCHAR(255) NOT NULL UNIQUE,
    hashed_password TEXT NOT NULL,
    full_name VARCHAR(255) NOT NULL,
    phone VARCHAR(30),
    tax_id VARCHAR(20), -- CPF or CNPJ (collected at checkout or profile)
    role VARCHAR(20) NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'admin')),
    is_active BOOLEAN NOT NULL DEFAULT true,
    is_email_verified BOOLEAN NOT NULL DEFAULT false,
    asaas_customer_id VARCHAR(100),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS idx_saas_users_email ON app_saas.users (email);

-- 3. Email OTP Verifications & Anti-Abuse Tracking
CREATE TABLE IF NOT EXISTS app_saas.email_otp_codes (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES app_saas.users(id) ON DELETE CASCADE,
    code_hash TEXT NOT NULL,
    ip_address VARCHAR(64),
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS idx_saas_otp_user ON app_saas.email_otp_codes (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_saas_otp_ip ON app_saas.email_otp_codes (ip_address, created_at DESC);

-- 4. User Wallets
CREATE TABLE IF NOT EXISTS app_saas.user_wallets (
    user_id UUID PRIMARY KEY REFERENCES app_saas.users(id) ON DELETE CASCADE,
    credit_balance INTEGER NOT NULL DEFAULT 0 CHECK (credit_balance >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

-- 5. Orders (PIX / Credit Card / Subscriptions)
CREATE TABLE IF NOT EXISTS app_saas.orders (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES app_saas.users(id) ON DELETE CASCADE,
    plan_code VARCHAR(40) NOT NULL, -- 'starter', 'pro', 'scale', 'smart_monthly'
    gateway_order_id VARCHAR(120),
    gateway_subscription_id VARCHAR(120),
    payment_method VARCHAR(30) NOT NULL CHECK (payment_method IN ('pix', 'credit_card')),
    status VARCHAR(30) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'paid', 'expired', 'refunded')),
    amount_cents INTEGER NOT NULL,
    credits_amount INTEGER NOT NULL,
    pix_qr_code TEXT,
    pix_copy_paste TEXT,
    nfse_url TEXT,
    nfse_status VARCHAR(40),
    expires_at TIMESTAMPTZ,
    paid_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS idx_saas_orders_user ON app_saas.orders (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_saas_orders_gateway ON app_saas.orders (gateway_order_id);

-- 6. Subscriptions (Assinatura Smart)
CREATE TABLE IF NOT EXISTS app_saas.subscriptions (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES app_saas.users(id) ON DELETE CASCADE,
    plan_code VARCHAR(40) NOT NULL DEFAULT 'smart_monthly',
    gateway_subscription_id VARCHAR(120),
    payment_method VARCHAR(30) NOT NULL DEFAULT 'pix',
    status VARCHAR(30) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'past_due', 'canceled')),
    current_period_end TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS idx_saas_subscriptions_user ON app_saas.subscriptions (user_id, status);

-- 7. Credit Lots (FIFO Expiration Management: 90 days for Smart, permanent for Avulso)
CREATE TABLE IF NOT EXISTS app_saas.credit_lots (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES app_saas.users(id) ON DELETE CASCADE,
    source_type VARCHAR(30) NOT NULL CHECK (source_type IN ('trial', 'avulso', 'subscription', 'bonus')),
    order_id UUID NULL REFERENCES app_saas.orders(id) ON DELETE SET NULL,
    initial_amount INTEGER NOT NULL CHECK (initial_amount > 0),
    remaining_amount INTEGER NOT NULL CHECK (remaining_amount >= 0),
    expires_at TIMESTAMPTZ NULL, -- NULL means non-expiring (avulso/trial), set to +90d for smart_monthly
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS idx_saas_credit_lots_fifo
    ON app_saas.credit_lots (user_id, expires_at ASC NULLS LAST, created_at ASC)
    WHERE remaining_amount > 0;

-- 8. Exported Lists (30-Day Physical File Retention)
CREATE TABLE IF NOT EXISTS app_saas.export_lists (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES app_saas.users(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    filters_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    total_records INTEGER NOT NULL,
    credits_charged INTEGER NOT NULL,
    deduplicated BOOLEAN NOT NULL DEFAULT true,
    csv_file_path TEXT,
    xlsx_file_path TEXT,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS idx_saas_export_lists_user ON app_saas.export_lists (user_id, created_at DESC);

-- 9. Immutable Credit Transactions Ledger
CREATE TABLE IF NOT EXISTS app_saas.credit_transactions (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES app_saas.users(id) ON DELETE CASCADE,
    order_id UUID NULL REFERENCES app_saas.orders(id) ON DELETE SET NULL,
    export_list_id UUID NULL REFERENCES app_saas.export_lists(id) ON DELETE SET NULL,
    transaction_type VARCHAR(30) NOT NULL CHECK (transaction_type IN ('purchase', 'usage', 'bonus', 'expiration')),
    amount INTEGER NOT NULL, -- positive for credit, negative for debit
    balance_after INTEGER NOT NULL,
    description TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS idx_saas_credit_tx_user ON app_saas.credit_transactions (user_id, created_at DESC);

-- 10. User Exported CNPJs (Automatic Deduplication Registry)
CREATE TABLE IF NOT EXISTS app_saas.user_exported_cnpjs (
    user_id UUID NOT NULL REFERENCES app_saas.users(id) ON DELETE CASCADE,
    cnpj CHAR(14) NOT NULL,
    first_export_list_id UUID NULL REFERENCES app_saas.export_lists(id) ON DELETE SET NULL,
    exported_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (user_id, cnpj)
);

CREATE INDEX IF NOT EXISTS idx_saas_user_exported_cnpjs_user ON app_saas.user_exported_cnpjs (user_id);

-- 11. Webhook Events (Idempotency)
CREATE TABLE IF NOT EXISTS app_saas.webhook_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id VARCHAR(255) NOT NULL UNIQUE,
    gateway VARCHAR(40) NOT NULL DEFAULT 'asaas',
    event_type VARCHAR(100),
    payload JSONB NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

-- 12. API Keys (DaaS Programmatic Access)
CREATE TABLE IF NOT EXISTS app_saas.api_keys (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES app_saas.users(id) ON DELETE CASCADE,
    name VARCHAR(120) NOT NULL,
    key_prefix VARCHAR(24) NOT NULL,
    key_hash VARCHAR(128) NOT NULL UNIQUE,
    is_active BOOLEAN NOT NULL DEFAULT true,
    last_used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS idx_saas_api_keys_user ON app_saas.api_keys (user_id);

-- 13. Saved Radars (Weekly Niche Alerts for Assinatura Smart)
CREATE TABLE IF NOT EXISTS app_saas.saved_radars (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES app_saas.users(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    filters_json JSONB NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT true,
    last_notified_at TIMESTAMPTZ,
    last_known_count INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS idx_saas_radars_user ON app_saas.saved_radars (user_id);

-- 14. LGPD Opt-Out Registry
CREATE TABLE IF NOT EXISTS app_saas.company_opt_outs (
    cnpj CHAR(14) PRIMARY KEY,
    requester_name VARCHAR(255) NOT NULL,
    requester_email VARCHAR(255) NOT NULL,
    requester_role VARCHAR(100),
    reason TEXT,
    status VARCHAR(30) NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'reverted')),
    cloudflare_purged BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS idx_saas_opt_outs_status ON app_saas.company_opt_outs (status);

-- 15. Geo Coordinate Cache (bridging registry establishments and br_company.geo)
CREATE TABLE IF NOT EXISTS app_saas.company_geo_cache (
    cnpj CHAR(14) PRIMARY KEY,
    latitude DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    precision VARCHAR(32) NOT NULL CHECK (precision IN ('exact_number', 'street', 'cep_centroid', 'city_centroid')),
    postal_code VARCHAR(16),
    resolved_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS app_saas.geo_postcode_cache (
    region VARCHAR(10) NOT NULL,
    postcode_digits VARCHAR(8) NOT NULL,
    latitude DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    point_count INTEGER NOT NULL DEFAULT 1,
    resolved_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (region, postcode_digits)
);
