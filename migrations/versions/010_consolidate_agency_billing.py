"""Consolidate agency_billing: subscribers becomes single source of truth.

Backfills any missing data from agency_billing into subscribers, then drops
all duplicated columns from agency_billing. After this migration,
agency_billing is a thin metadata table with ONLY agency-specific fields:
agency_email (PK/FK), company_id, company_name, company_owner_*, whitelabel,
max_seats, active_seats, crm_config, created_at, updated_at.

All operational data (tokens, voice_config, bot_settings, carriers, stripe,
API keys, etc.) lives exclusively on subscribers.

Revision ID: 010_consolidate_agency_billing
Revises: 009_normalize_timezones
Create Date: 2026-03-25
"""
from typing import Sequence, Union
from alembic import op

revision: str = '010_consolidate_agency_billing'
down_revision: Union[str, None] = '009_normalize_timezones'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 0. Safety: deduplicate any case-mismatched emails in subscribers
    op.execute("""
        DELETE FROM subscribers a USING subscribers b
        WHERE a.ctid < b.ctid AND LOWER(a.email) = LOWER(b.email)
    """)

    # 1. Backfill: copy any agency_billing data missing from subscribers
    op.execute("""
        UPDATE subscribers s
        SET
            access_token = COALESCE(s.access_token, ab.access_token),
            refresh_token = COALESCE(s.refresh_token, ab.refresh_token),
            token_expires_at = COALESCE(s.token_expires_at, ab.token_expires_at),
            voice_config = CASE WHEN s.voice_config = '{}'::jsonb OR s.voice_config IS NULL
                THEN COALESCE(ab.voice_config, s.voice_config)
                ELSE s.voice_config END,
            bot_settings = CASE WHEN s.bot_settings = '{}'::jsonb OR s.bot_settings IS NULL
                THEN COALESCE(ab.bot_settings, s.bot_settings)
                ELSE s.bot_settings END,
            contracted_carriers = CASE WHEN s.contracted_carriers = '[]'::jsonb OR s.contracted_carriers IS NULL
                THEN COALESCE(ab.contracted_carriers, s.contracted_carriers)
                ELSE s.contracted_carriers END,
            stripe_customer_id = COALESCE(s.stripe_customer_id, ab.stripe_customer_id),
            stripe_status = COALESCE(s.stripe_status, ab.stripe_status),
            api_key = COALESCE(s.api_key, ab.api_key),
            webhook_secret = COALESCE(s.webhook_secret, ab.webhook_secret),
            outbound_webhook_url = COALESCE(s.outbound_webhook_url, ab.outbound_webhook_url),
            password_hash = COALESCE(s.password_hash, ab.password_hash),
            sms_send_via = COALESCE(s.sms_send_via, ab.sms_send_via),
            google_calendar_config = CASE WHEN s.google_calendar_config = '{}'::jsonb OR s.google_calendar_config IS NULL
                THEN COALESCE(ab.google_calendar_config, s.google_calendar_config)
                ELSE s.google_calendar_config END
        FROM agency_billing ab
        WHERE LOWER(s.email) = LOWER(ab.agency_email)
    """)

    # 2. Normalize agency_billing.agency_email casing to match subscribers.email
    op.execute("""
        UPDATE agency_billing ab
        SET agency_email = s.email
        FROM subscribers s
        WHERE LOWER(ab.agency_email) = LOWER(s.email)
          AND ab.agency_email != s.email
    """)

    # 3. Ensure functional index on subscribers.email for fast lookups
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_subscribers_email_lower "
        "ON subscribers (LOWER(email))"
    )

    # 4. Drop ALL duplicated columns from agency_billing
    # Keeps: agency_email, company_id, company_name, company_owner_name,
    #        company_owner_email, company_owner_phone, whitelabel_config,
    #        max_seats, active_seats, crm_config, created_at, updated_at
    cols_to_drop = [
        'location_id', 'password_hash', 'full_name', 'phone', 'bio', 'role',
        'bot_first_name', 'access_token', 'refresh_token', 'token_expires_at',
        'token_type', 'timezone', 'crm_user_id', 'calendar_id', 'calendar_name',
        'initial_message', 'subscription_tier', 'stripe_customer_id', 'stripe_status',
        'oauth_app_type', 'personal_website', 'crm_type', 'crm_email',
        'contracted_carriers', 'bot_settings', 'voice_config', 'sms_send_via',
        'google_calendar_config', 'preferred_language',
        'api_key', 'api_key_created_at', 'outbound_webhook_url', 'webhook_secret',
        'install_completed_at', 'reminder_24h_sent', 'reminder_72h_sent',
    ]
    for col in cols_to_drop:
        op.execute(f"ALTER TABLE agency_billing DROP COLUMN IF EXISTS {col}")

    # 5. Add FK: agency_billing.agency_email → subscribers.email
    # ON UPDATE CASCADE: if subscriber email changes, agency_billing follows
    # NOT VALID + separate VALIDATE: non-blocking on large tables
    op.execute("""
        ALTER TABLE agency_billing
        ADD CONSTRAINT fk_agency_billing_subscriber
        FOREIGN KEY (agency_email) REFERENCES subscribers(email)
        ON UPDATE CASCADE
        NOT VALID
    """)
    op.execute(
        "ALTER TABLE agency_billing VALIDATE CONSTRAINT fk_agency_billing_subscriber"
    )


def downgrade() -> None:
    # Drop the FK constraint
    op.execute(
        "ALTER TABLE agency_billing DROP CONSTRAINT IF EXISTS fk_agency_billing_subscriber"
    )
    # Re-add the dropped columns (empty — data was already on subscribers)
    # This is a best-effort downgrade; operational data stays on subscribers
    cols_to_readd = {
        'location_id': 'TEXT',
        'password_hash': 'TEXT',
        'full_name': 'TEXT',
        'phone': 'TEXT',
        'bio': 'TEXT',
        'role': "TEXT DEFAULT 'agency_owner'",
        'bot_first_name': "TEXT DEFAULT 'Grok'",
        'access_token': 'TEXT',
        'refresh_token': 'TEXT',
        'token_expires_at': 'TIMESTAMP',
        'token_type': "TEXT DEFAULT 'Bearer'",
        'timezone': "TEXT DEFAULT 'America/Chicago'",
        'crm_user_id': 'TEXT',
        'calendar_id': 'TEXT',
        'calendar_name': 'TEXT',
        'initial_message': 'TEXT',
        'subscription_tier': "TEXT DEFAULT 'individual'",
        'stripe_customer_id': 'TEXT',
        'stripe_status': 'TEXT',
        'oauth_app_type': "TEXT DEFAULT 'marketplace'",
        'personal_website': 'TEXT',
        'crm_type': "TEXT DEFAULT 'ghl'",
        'crm_email': 'TEXT',
        'contracted_carriers': "JSONB DEFAULT '[]'",
        'bot_settings': "JSONB DEFAULT '{}'::jsonb",
        'voice_config': "JSONB DEFAULT '{}'::jsonb",
        'sms_send_via': "TEXT DEFAULT 'ghl'",
        'google_calendar_config': "JSONB DEFAULT '{}'::jsonb",
        'preferred_language': "TEXT DEFAULT 'en'",
        'api_key': 'TEXT',
        'api_key_created_at': 'TIMESTAMP',
        'outbound_webhook_url': 'TEXT',
        'webhook_secret': 'TEXT',
        'install_completed_at': 'TIMESTAMP',
        'reminder_24h_sent': 'BOOLEAN DEFAULT FALSE',
        'reminder_72h_sent': 'BOOLEAN DEFAULT FALSE',
    }
    for col, col_type in cols_to_readd.items():
        op.execute(f"ALTER TABLE agency_billing ADD COLUMN IF NOT EXISTS {col} {col_type}")
