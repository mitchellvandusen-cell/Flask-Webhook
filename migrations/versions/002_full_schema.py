"""Full schema creation — all 30+ tables, indexes, and constraints.

On EXISTING databases (production): Every statement uses IF NOT EXISTS,
so this migration is a complete no-op. Safe to run.

On FRESH databases: Creates the entire schema from scratch via Alembic,
eliminating the need for the legacy init_db() function.

This migration is the canonical, authoritative definition of the database
schema. All future changes go in subsequent numbered migrations.

Revision ID: 002_full_schema
Revises: 001_baseline
Create Date: 2026-03-19
"""
from typing import Sequence, Union

from alembic import op

revision: str = "002_full_schema"
down_revision: Union[str, None] = "001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ================================================================
    # TABLE 1: subscribers — Master user table (individual agents + sub-users)
    # ================================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS subscribers (
            location_id TEXT PRIMARY KEY,
            email TEXT UNIQUE,
            password_hash TEXT,
            full_name TEXT,
            phone TEXT,
            bio TEXT,
            role TEXT DEFAULT 'individual',

            bot_first_name TEXT DEFAULT 'Grok',
            access_token TEXT,
            refresh_token TEXT,
            token_expires_at TIMESTAMP,
            token_type TEXT DEFAULT 'Bearer',
            timezone TEXT DEFAULT 'America/Chicago',
            crm_user_id TEXT,
            calendar_id TEXT,
            calendar_name TEXT,
            initial_message TEXT,
            parent_agency_email TEXT,
            subscription_tier TEXT DEFAULT 'individual',
            confirmation_code TEXT,
            stripe_customer_id TEXT,

            agent_email TEXT,
            invite_token TEXT,
            invite_sent_at TIMESTAMP,
            invite_claimed_at TIMESTAMP,
            onboarding_status TEXT DEFAULT 'pending',
            oauth_app_type TEXT DEFAULT 'marketplace',
            personal_website TEXT,

            -- Multi-CRM support
            crm_type TEXT DEFAULT 'ghl',
            crm_config JSONB DEFAULT '{}'::jsonb,
            crm_email TEXT,

            -- Bot configuration
            contracted_carriers JSONB DEFAULT '[]',
            bot_settings JSONB DEFAULT '{}'::jsonb,
            voice_config JSONB DEFAULT '{}'::jsonb,
            sms_send_via TEXT DEFAULT 'ghl',
            google_calendar_config JSONB DEFAULT '{}'::jsonb,
            preferred_language TEXT DEFAULT 'en',

            -- API platform
            api_key TEXT UNIQUE,
            api_key_created_at TIMESTAMP,
            outbound_webhook_url TEXT,
            webhook_secret TEXT,

            -- Install tracking + reminders
            install_completed_at TIMESTAMP,
            reminder_24h_sent BOOLEAN DEFAULT FALSE,
            reminder_72h_sent BOOLEAN DEFAULT FALSE,

            -- Agency linkage
            company_id TEXT,

            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_subscribers_company_id ON subscribers (company_id)")

    # ================================================================
    # TABLE 2: agency_billing — Agency owners & billing records
    # ================================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS agency_billing (
            agency_email TEXT PRIMARY KEY,
            location_id TEXT UNIQUE,
            password_hash TEXT,
            full_name TEXT,
            phone TEXT,
            bio TEXT,
            role TEXT DEFAULT 'agency_owner',

            bot_first_name TEXT DEFAULT 'Grok',
            access_token TEXT,
            refresh_token TEXT,
            token_expires_at TIMESTAMP,
            token_type TEXT DEFAULT 'Bearer',
            timezone TEXT DEFAULT 'America/Chicago',
            crm_user_id TEXT,
            calendar_id TEXT,
            calendar_name TEXT,
            initial_message TEXT,
            subscription_tier TEXT DEFAULT 'agency_starter',
            max_seats INTEGER DEFAULT 10,
            active_seats INTEGER DEFAULT 0,
            stripe_customer_id TEXT,
            stripe_status TEXT,
            oauth_app_type TEXT DEFAULT 'marketplace',
            personal_website TEXT,

            -- Multi-CRM support
            crm_type TEXT DEFAULT 'ghl',
            crm_config JSONB DEFAULT '{}'::jsonb,
            crm_email TEXT,

            -- Bot configuration
            contracted_carriers JSONB DEFAULT '[]',
            bot_settings JSONB DEFAULT '{}'::jsonb,
            voice_config JSONB DEFAULT '{}'::jsonb,
            sms_send_via TEXT DEFAULT 'ghl',
            google_calendar_config JSONB DEFAULT '{}'::jsonb,
            preferred_language TEXT DEFAULT 'en',

            -- API platform
            api_key TEXT UNIQUE,
            api_key_created_at TIMESTAMP,
            outbound_webhook_url TEXT,
            webhook_secret TEXT,

            -- Install tracking + reminders
            install_completed_at TIMESTAMP,
            reminder_24h_sent BOOLEAN DEFAULT FALSE,
            reminder_72h_sent BOOLEAN DEFAULT FALSE,

            -- Agency-specific
            company_id TEXT,
            company_name TEXT,
            company_owner_name TEXT,
            company_owner_email TEXT,
            company_owner_phone TEXT,
            whitelabel_config JSONB DEFAULT '{}'::jsonb,

            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_agency_billing_company_id ON agency_billing (company_id)")

    # ================================================================
    # TABLE 3: contact_messages — Chat history per contact
    # ================================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS contact_messages (
            id SERIAL PRIMARY KEY,
            contact_id TEXT NOT NULL,
            message_type TEXT NOT NULL CHECK (message_type IN ('lead', 'assistant')),
            message_text TEXT NOT NULL,
            stage TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_contact_messages_contact_id ON contact_messages (contact_id)")
    # Partial unique index: only assistant messages are deduped (prevents bot looping)
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS unique_assistant_msg
        ON contact_messages (contact_id, message_text, message_type)
        WHERE message_type = 'assistant'
    """)

    # ================================================================
    # TABLE 4: contact_facts — NLP-extracted facts (spaCy)
    # ================================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS contact_facts (
            id SERIAL PRIMARY KEY,
            contact_id TEXT NOT NULL,
            fact_text TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(contact_id, fact_text)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_contact_facts_contact_id ON contact_facts (contact_id)")

    # ================================================================
    # TABLE 5: processed_webhooks — Webhook deduplication (idempotency)
    # ================================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS processed_webhooks (
            webhook_id TEXT PRIMARY KEY,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ================================================================
    # TABLE 6: contact_narratives — AI narrative summaries
    # ================================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS contact_narratives (
            contact_id TEXT PRIMARY KEY,
            story_narrative TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_contact_narratives_updated ON contact_narratives (updated_at)")

    # ================================================================
    # TABLE 7: webhook_logs — Activity/audit log per location
    # ================================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS webhook_logs (
            id SERIAL PRIMARY KEY,
            location_id TEXT NOT NULL,
            contact_id TEXT,
            event_type TEXT NOT NULL,
            status TEXT DEFAULT 'info',
            summary TEXT,
            details JSONB DEFAULT '{}'::jsonb,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_webhook_logs_location ON webhook_logs(location_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_webhook_logs_created ON webhook_logs(created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_webhook_logs_event ON webhook_logs(event_type)")

    # ================================================================
    # TABLE 8: persistent_alerts — Dashboard banner alerts
    # ================================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS persistent_alerts (
            id SERIAL PRIMARY KEY,
            email TEXT NOT NULL,
            location_id TEXT,
            alert_type TEXT NOT NULL,
            severity TEXT DEFAULT 'warning',
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            dismissed BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_persistent_alerts_email ON persistent_alerts(email, dismissed)")

    # ================================================================
    # TABLE 9: marketplace_installs — GHL marketplace install tracking
    # ================================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS marketplace_installs (
            id SERIAL PRIMARY KEY,
            app_id TEXT,
            company_id TEXT,
            location_id TEXT,
            user_id TEXT,
            user_email TEXT,
            user_name TEXT,
            plan_id TEXT,
            install_type TEXT,
            raw_payload JSONB DEFAULT '{}'::jsonb,
            oauth_completed BOOLEAN DEFAULT FALSE,
            oauth_completed_at TIMESTAMP,
            setup_email_sent BOOLEAN DEFAULT FALSE,
            setup_email_sent_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_mkt_installs_company ON marketplace_installs(company_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_mkt_installs_location ON marketplace_installs(location_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_mkt_installs_email ON marketplace_installs(user_email)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_mkt_installs_created ON marketplace_installs(created_at DESC)")

    # ================================================================
    # TABLE 10: api_usage_logs — External API key usage for rate limiting
    # ================================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS api_usage_logs (
            id SERIAL PRIMARY KEY,
            api_key_prefix TEXT NOT NULL,
            location_id TEXT,
            endpoint TEXT NOT NULL,
            method TEXT DEFAULT 'POST',
            status_code INTEGER,
            response_time_ms INTEGER,
            contact_id TEXT,
            ip_address TEXT,
            user_agent TEXT,
            error_message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_api_usage_created ON api_usage_logs (created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_api_usage_key_prefix ON api_usage_logs (api_key_prefix, created_at DESC)")

    # ================================================================
    # TABLE 11: call_history — Voice call records
    # ================================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS call_history (
            id SERIAL PRIMARY KEY,
            location_id TEXT NOT NULL,
            contact_id TEXT,
            contact_name TEXT,
            phone TEXT NOT NULL,
            direction TEXT DEFAULT 'outbound',
            call_sid TEXT UNIQUE,
            status TEXT DEFAULT 'initiated',
            duration INTEGER DEFAULT 0,
            recording_url TEXT,
            recording_sid TEXT,
            transcript JSONB DEFAULT '[]'::jsonb,
            started_at TIMESTAMP DEFAULT NOW(),
            ended_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT NOW(),

            -- Added via migrations
            disposition TEXT DEFAULT NULL,
            callback_at TIMESTAMP DEFAULT NULL,
            stir_status TEXT DEFAULT NULL,
            ring_confirmed BOOLEAN DEFAULT FALSE,
            insights JSONB DEFAULT NULL,
            pdd_ms INTEGER DEFAULT NULL,
            quality_tags TEXT[] DEFAULT NULL,
            from_number TEXT DEFAULT NULL
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_call_history_location ON call_history(location_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_call_history_call_sid ON call_history(call_sid)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_call_history_location_contact ON call_history(location_id, contact_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_call_history_from_number ON call_history(from_number) WHERE from_number IS NOT NULL")

    # ================================================================
    # TABLE 12: ai_minute_balances — Per-user AI minute credit balance
    # ================================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS ai_minute_balances (
            id SERIAL PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            balance_minutes INTEGER NOT NULL DEFAULT 0,
            total_purchased INTEGER NOT NULL DEFAULT 0,
            total_used INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_aim_balances_email ON ai_minute_balances(email)")

    # ================================================================
    # TABLE 13: ai_minute_purchases — AI minutes purchase history
    # ================================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS ai_minute_purchases (
            id SERIAL PRIMARY KEY,
            email TEXT NOT NULL,
            stripe_session_id TEXT UNIQUE,
            stripe_payment_intent TEXT,
            package_minutes INTEGER NOT NULL,
            package_label TEXT,
            amount_cents INTEGER,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT NOW(),
            completed_at TIMESTAMP
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_aim_purchases_email ON ai_minute_purchases(email)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_aim_purchases_session ON ai_minute_purchases(stripe_session_id)")

    # ================================================================
    # TABLE 14: ai_minute_usage_logs — AI minutes usage history
    # ================================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS ai_minute_usage_logs (
            id SERIAL PRIMARY KEY,
            email TEXT NOT NULL,
            call_sid TEXT,
            phone TEXT,
            direction TEXT DEFAULT 'outbound',
            duration_seconds INTEGER NOT NULL DEFAULT 0,
            minutes_deducted INTEGER NOT NULL DEFAULT 0,
            balance_after INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_aim_usage_email ON ai_minute_usage_logs(email)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_aim_usage_created ON ai_minute_usage_logs(created_at DESC)")

    # ================================================================
    # TABLE 15: discord_connections — Discord OAuth tokens per user
    # ================================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS discord_connections (
            id SERIAL PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            discord_user_id TEXT NOT NULL,
            username TEXT,
            global_name TEXT,
            avatar TEXT,
            access_token TEXT NOT NULL,
            refresh_token TEXT,
            token_expires_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_discord_conn_email ON discord_connections(email)")

    # ================================================================
    # TABLE 16: discord_servers — Saved Discord servers per user (max 3)
    # ================================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS discord_servers (
            id SERIAL PRIMARY KEY,
            email TEXT NOT NULL,
            guild_id TEXT NOT NULL,
            guild_name TEXT NOT NULL,
            guild_icon TEXT,
            position INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(email, guild_id)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_discord_servers_email ON discord_servers(email)")

    # ================================================================
    # TABLE 17: discord_webhook_channels — Webhook-connected Discord channels
    # ================================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS discord_webhook_channels (
            id SERIAL PRIMARY KEY,
            email TEXT NOT NULL,
            guild_id TEXT NOT NULL,
            guild_name TEXT NOT NULL,
            guild_icon TEXT,
            channel_id TEXT NOT NULL,
            channel_name TEXT NOT NULL,
            webhook_id TEXT,
            webhook_token TEXT,
            webhook_url TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(email, channel_id)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_discord_wh_email ON discord_webhook_channels(email)")

    # ================================================================
    # TABLE 18: failed_webhook_payloads — Failed webhook recovery
    # ================================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS failed_webhook_payloads (
            id SERIAL PRIMARY KEY,
            location_id TEXT NOT NULL,
            contact_id TEXT,
            payload JSONB NOT NULL,
            failure_reason TEXT NOT NULL,
            retried BOOLEAN DEFAULT FALSE,
            retry_result TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            retried_at TIMESTAMP
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_fwp_location_retried ON failed_webhook_payloads(location_id, retried)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_fwp_created ON failed_webhook_payloads(created_at)")

    # ================================================================
    # TABLE 19: app_settings — Global application settings (key-value)
    # ================================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ================================================================
    # TABLE 20: contact_cache — Cached CRM contact data for fast dialer loading
    # ================================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS contact_cache (
            location_id TEXT NOT NULL,
            contact_id TEXT NOT NULL,
            name TEXT,
            first_name TEXT,
            last_name TEXT,
            phone TEXT,
            email TEXT,
            tags JSONB DEFAULT '[]',
            date_added TEXT,
            dnd BOOLEAN DEFAULT FALSE,
            assigned_to TEXT DEFAULT NULL,
            synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (location_id, contact_id)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_contact_cache_loc ON contact_cache (location_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_contact_cache_search ON contact_cache (location_id, lower(name) text_pattern_ops)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_contact_cache_phone ON contact_cache (location_id, phone)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_contact_cache_synced ON contact_cache (location_id, synced_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_contact_cache_assigned ON contact_cache (location_id, assigned_to)")

    # ================================================================
    # TABLE 21: crm_conversations — Synced CRM message history
    # ================================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS crm_conversations (
            id SERIAL PRIMARY KEY,
            location_id TEXT NOT NULL,
            contact_id TEXT NOT NULL,
            contact_name TEXT,
            contact_phone TEXT,
            conversation_id TEXT,
            message_type TEXT DEFAULT 'sms',
            direction TEXT DEFAULT 'inbound',
            body TEXT,
            source TEXT DEFAULT 'ghl_native',
            external_message_id TEXT UNIQUE NOT NULL,
            crm_source TEXT DEFAULT 'ghl',
            date_added TEXT,
            synced_at TIMESTAMP DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_crm_conv_location ON crm_conversations(location_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_crm_conv_contact ON crm_conversations(location_id, contact_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_crm_conv_type ON crm_conversations(location_id, message_type)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_crm_conv_source ON crm_conversations(location_id, source)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_crm_conv_date ON crm_conversations(location_id, date_added DESC)")

    # ================================================================
    # TABLE 22: crm_deals — Synced CRM pipeline/deal data
    # ================================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS crm_deals (
            id SERIAL PRIMARY KEY,
            location_id TEXT NOT NULL,
            contact_id TEXT,
            pipeline_id TEXT,
            pipeline_name TEXT,
            stage_id TEXT,
            stage_name TEXT,
            status TEXT DEFAULT 'open',
            monetary_value NUMERIC DEFAULT 0,
            source TEXT,
            assigned_to TEXT,
            external_deal_id TEXT UNIQUE NOT NULL,
            crm_source TEXT DEFAULT 'ghl',
            created_at_ghl TEXT,
            updated_at_ghl TEXT,
            synced_at TIMESTAMP DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_crm_deals_location ON crm_deals(location_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_crm_deals_contact ON crm_deals(location_id, contact_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_crm_deals_pipeline ON crm_deals(location_id, pipeline_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_crm_deals_status ON crm_deals(location_id, status)")

    # ================================================================
    # TABLE 23: crm_sync_state — Sync progress tracking per CRM
    # ================================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS crm_sync_state (
            id SERIAL PRIMARY KEY,
            location_id TEXT NOT NULL,
            resource_type TEXT NOT NULL,
            crm_source TEXT DEFAULT 'ghl',
            last_sync_at TIMESTAMP,
            last_cursor TEXT,
            sync_status TEXT DEFAULT 'idle',
            error_message TEXT,
            total_synced INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE (location_id, resource_type)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_crm_sync_location ON crm_sync_state(location_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_crm_sync_status ON crm_sync_state(sync_status)")

    # ================================================================
    # TABLE 24: contacts — Universal multi-CRM contacts table
    # ================================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS contacts (
            id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
            location_id TEXT NOT NULL,
            external_id TEXT,
            crm_source TEXT DEFAULT 'standalone',
            first_name TEXT,
            last_name TEXT,
            email TEXT,
            phone TEXT,
            address TEXT,
            city TEXT,
            state TEXT,
            zip TEXT,
            tags JSONB DEFAULT '[]',
            custom_fields JSONB DEFAULT '{}',
            pipeline_stage TEXT,
            date_added TIMESTAMP DEFAULT NOW(),
            last_activity_at TIMESTAMP,
            do_not_contact BOOLEAN DEFAULT FALSE,
            UNIQUE(location_id, external_id, crm_source)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_contacts_location ON contacts(location_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_contacts_phone ON contacts(phone)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_contacts_external ON contacts(external_id, crm_source)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_contacts_crm ON contacts(location_id, crm_source)")

    # ================================================================
    # TABLE 25: number_health — Phone number health tracking
    # ================================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS number_health (
            id SERIAL PRIMARY KEY,
            location_id TEXT NOT NULL,
            phone TEXT NOT NULL,
            status TEXT DEFAULT 'active',
            warmup_stage INTEGER DEFAULT 0,
            health_score NUMERIC(5,1) DEFAULT 75.0,
            daily_calls_today INTEGER DEFAULT 0,
            daily_connected INTEGER DEFAULT 0,
            daily_no_answer INTEGER DEFAULT 0,
            daily_failed INTEGER DEFAULT 0,
            daily_busy INTEGER DEFAULT 0,
            daily_duration_secs INTEGER DEFAULT 0,
            daily_carrier_blocked INTEGER DEFAULT 0,
            daily_ring_confirmed INTEGER DEFAULT 0,
            total_calls INTEGER DEFAULT 0,
            total_connected INTEGER DEFAULT 0,
            total_no_answer INTEGER DEFAULT 0,
            total_failed INTEGER DEFAULT 0,
            total_busy INTEGER DEFAULT 0,
            total_duration_secs INTEGER DEFAULT 0,
            total_carrier_blocked INTEGER DEFAULT 0,
            total_ring_confirmed INTEGER DEFAULT 0,
            avg_pdd_ms NUMERIC(8,1) DEFAULT NULL,
            recent_pdd_trend NUMERIC(8,1) DEFAULT NULL,
            insights_quality_issues INTEGER DEFAULT 0,
            stir_a_rate NUMERIC(5,2) DEFAULT NULL,
            carrier_block_velocity NUMERIC(5,2) DEFAULT 0,
            rest_until TIMESTAMP,
            last_used_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW(),
            UNIQUE (location_id, phone)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_number_health_location ON number_health(location_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_number_health_status ON number_health(location_id, status)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_number_health_score ON number_health(location_id, health_score DESC)")

    # ================================================================
    # TABLE 26: slack_connections — Slack OAuth tokens per user
    # ================================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS slack_connections (
            id SERIAL PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            slack_team_id TEXT NOT NULL,
            slack_team_name TEXT,
            slack_user_id TEXT,
            bot_token TEXT NOT NULL,
            user_token TEXT,
            authed_user_name TEXT,
            bot_user_id TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_slack_conn_email ON slack_connections(email)")

    # ================================================================
    # TABLE 27: slack_workspaces — Saved Slack workspaces per user
    # ================================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS slack_workspaces (
            id SERIAL PRIMARY KEY,
            email TEXT NOT NULL,
            team_id TEXT NOT NULL,
            team_name TEXT NOT NULL,
            team_icon TEXT,
            position INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(email, team_id)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_slack_ws_email ON slack_workspaces(email)")

    # ================================================================
    # TABLE 28: uninstall_feedback — GHL marketplace uninstall feedback
    # ================================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS uninstall_feedback (
            id SERIAL PRIMARY KEY,
            location_id TEXT,
            company_id TEXT,
            user_email TEXT,
            user_name TEXT,
            reason TEXT,
            other_text TEXT,
            raw_webhook_payload JSONB DEFAULT '{}'::jsonb,
            feedback_submitted_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_uninstall_fb_location ON uninstall_feedback(location_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_uninstall_fb_created ON uninstall_feedback(created_at DESC)")

    # ================================================================
    # TABLE 29: location_users — Team members per location
    # ================================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS location_users (
            id SERIAL PRIMARY KEY,
            location_id TEXT NOT NULL REFERENCES subscribers(location_id),
            ghl_user_id TEXT,
            email TEXT UNIQUE,
            password_hash TEXT,
            full_name TEXT,
            role TEXT DEFAULT 'agent',
            permissions JSONB DEFAULT '{
                "can_dial": true,
                "can_text": true,
                "can_view_all_leads": false,
                "can_import_leads": false,
                "can_change_bot_config": false,
                "can_view_call_recordings": true,
                "can_manage_numbers": false,
                "can_view_billing": false,
                "can_invite_users": false
            }'::jsonb,
            voice_config JSONB DEFAULT '{}'::jsonb,
            voice_activated BOOLEAN DEFAULT false,
            invite_token TEXT,
            invite_sent_at TIMESTAMP,
            invite_claimed_at TIMESTAMP,
            onboarding_status TEXT DEFAULT 'pending',
            is_active BOOLEAN DEFAULT true,
            session_revoked_at TIMESTAMP,
            stripe_seat_subscription_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_location_users_location ON location_users(location_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_location_users_email ON location_users(email)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_location_users_ghl ON location_users(ghl_user_id)")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_location_users_loc_ghl ON location_users(location_id, ghl_user_id)")

    # ================================================================
    # TABLE 30: team_audit_log — Audit trail for team management
    # ================================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS team_audit_log (
            id SERIAL PRIMARY KEY,
            location_id TEXT NOT NULL,
            actor_email TEXT NOT NULL,
            action TEXT NOT NULL,
            target_email TEXT,
            details JSONB,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_team_audit_location ON team_audit_log(location_id, created_at DESC)")

    # ================================================================
    # TABLE 31: contact_imports — CSV/Excel/TXT import tracking
    # ================================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS contact_imports (
            id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
            location_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            total_rows INTEGER DEFAULT 0,
            imported INTEGER DEFAULT 0,
            updated INTEGER DEFAULT 0,
            skipped INTEGER DEFAULT 0,
            failed INTEGER DEFAULT 0,
            duplicate_strategy TEXT DEFAULT 'skip',
            apply_tags JSONB DEFAULT '[]',
            column_mapping JSONB DEFAULT '{}',
            preview_data JSONB DEFAULT '[]',
            error_log JSONB DEFAULT '[]',
            file_data JSONB DEFAULT '[]',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            created_by TEXT
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_contact_imports_loc ON contact_imports (location_id, created_at DESC)")

    # ================================================================
    # TABLE 32: workflows — Workflow automation definitions
    # ================================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS workflows (
            id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
            location_id TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT 'Untitled Workflow',
            description TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'draft',
            trigger_type TEXT NOT NULL DEFAULT 'manual',
            trigger_config JSONB DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_by TEXT,
            stats JSONB DEFAULT '{"runs": 0, "completed": 0, "failed": 0}'
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_workflows_loc ON workflows (location_id, status)")

    # ================================================================
    # TABLE 33: workflow_steps — Workflow step definitions
    # ================================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS workflow_steps (
            id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
            workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
            step_type TEXT NOT NULL,
            step_subtype TEXT NOT NULL,
            config JSONB DEFAULT '{}',
            position_x FLOAT DEFAULT 0,
            position_y FLOAT DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_wf_steps_wf ON workflow_steps (workflow_id)")

    # ================================================================
    # TABLE 34: workflow_connections — Workflow trigger/step routing graph
    # ================================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS workflow_connections (
            id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
            workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
            from_step_id TEXT NOT NULL,
            to_step_id TEXT NOT NULL,
            branch_key TEXT DEFAULT 'default',
            sort_order INTEGER DEFAULT 0
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_wf_conn_wf ON workflow_connections (workflow_id)")

    # ================================================================
    # TABLE 35: workflow_runs — Individual workflow execution runs
    # ================================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS workflow_runs (
            id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
            workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
            contact_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            current_step_id TEXT,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            next_execute_at TIMESTAMP,
            error TEXT,
            context JSONB DEFAULT '{}'
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_wf_runs_wf ON workflow_runs (workflow_id, status)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_wf_runs_contact ON workflow_runs (contact_id, status)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_wf_runs_next ON workflow_runs (next_execute_at) WHERE status = 'running'")
    # Performance index flagged by Gemini review — prevents full table scan on cron
    op.execute("CREATE INDEX IF NOT EXISTS idx_wf_runs_status_next ON workflow_runs (status, next_execute_at) WHERE status = 'running'")

    # ================================================================
    # TABLE 36: workflow_step_logs — Per-step execution logs
    # ================================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS workflow_step_logs (
            id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
            run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
            step_id TEXT NOT NULL,
            status TEXT NOT NULL,
            result JSONB DEFAULT '{}',
            executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_wf_logs_run ON workflow_step_logs (run_id, executed_at)")

    # ================================================================
    # TABLE 37: workflow_custom_actions — Custom workflow action definitions
    # ================================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS workflow_custom_actions (
            id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
            location_id TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            step_type TEXT NOT NULL DEFAULT 'action',
            icon TEXT DEFAULT 'fa-solid fa-puzzle-piece',
            color TEXT DEFAULT '#00b4ff',
            config_template JSONB DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_wf_custom_loc ON workflow_custom_actions (location_id)")

    # ================================================================
    # TABLE 38: ghl_trigger_subscriptions — GHL webhook subscription tracking
    # ================================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS ghl_trigger_subscriptions (
            id SERIAL PRIMARY KEY,
            location_id TEXT NOT NULL,
            trigger_type TEXT NOT NULL,
            webhook_url TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(location_id, trigger_type)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_ghl_trig_loc ON ghl_trigger_subscriptions (location_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_ghl_trig_type ON ghl_trigger_subscriptions (trigger_type)")

    # ================================================================
    # TABLE 39: ghl_action_loops — GHL action loop tracking
    # ================================================================
    op.execute("""
        CREATE TABLE IF NOT EXISTS ghl_action_loops (
            id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
            location_id TEXT NOT NULL,
            contact_id TEXT NOT NULL,
            phone TEXT NOT NULL DEFAULT '',
            first_name TEXT NOT NULL DEFAULT '',
            loop_action TEXT NOT NULL DEFAULT 'ai_sms',
            message_template TEXT DEFAULT '',
            duration_days INTEGER NOT NULL DEFAULT 3,
            interval_hours INTEGER NOT NULL DEFAULT 24,
            iteration INTEGER NOT NULL DEFAULT 0,
            max_iterations INTEGER NOT NULL DEFAULT 3,
            status TEXT NOT NULL DEFAULT 'active',
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            next_execute_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            config JSONB DEFAULT '{}'
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_ghl_loops_active ON ghl_action_loops (status, next_execute_at) WHERE status = 'active'")
    op.execute("CREATE INDEX IF NOT EXISTS idx_ghl_loops_loc ON ghl_action_loops (location_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_ghl_loops_contact ON ghl_action_loops (location_id, contact_id)")


def downgrade() -> None:
    """Cannot downgrade past full schema — this is the foundation."""
    raise RuntimeError(
        "Cannot downgrade the full schema migration. "
        "Dropping all tables would destroy all data. "
        "Use pg_dump for backups before any destructive operation."
    )
