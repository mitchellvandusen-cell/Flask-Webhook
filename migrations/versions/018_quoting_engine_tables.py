"""Add quoting engine tables for in-house life insurance quoter.

Six new tables: uw_conditions, uw_condition_questions, uw_medications,
uw_drug_condition_map, uw_rules, uw_rate_tables.

Replaces InsuranceToolKits ($3K/mo) with carrier-documentation-sourced
deterministic quoting engine. Zero AI — pure rule matching.

Revision ID: 018_quoting_engine
Revises: 017_biz_profile_submitted
Create Date: 2026-04-06
"""
from typing import Sequence, Union
from alembic import op

revision: str = '018_quoting_engine'
down_revision: Union[str, None] = '017_biz_profile_submitted'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # pg_trgm extension for fuzzy autocomplete search
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")

    # 1. Medical conditions relevant to life insurance underwriting
    op.execute("""
        CREATE TABLE IF NOT EXISTS uw_conditions (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            slug TEXT NOT NULL UNIQUE,
            category TEXT NOT NULL,
            aliases TEXT[] DEFAULT '{}',
            severity_default TEXT DEFAULT 'moderate',
            notes TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_uw_conditions_slug ON uw_conditions(slug);")
    op.execute("CREATE INDEX IF NOT EXISTS idx_uw_conditions_category ON uw_conditions(category);")
    op.execute("CREATE INDEX IF NOT EXISTS idx_uw_conditions_name_trgm ON uw_conditions USING gin(name gin_trgm_ops);")

    # 2. Per-condition follow-up questions
    op.execute("""
        CREATE TABLE IF NOT EXISTS uw_condition_questions (
            id SERIAL PRIMARY KEY,
            condition_id INTEGER NOT NULL REFERENCES uw_conditions(id) ON DELETE CASCADE,
            sort_order INTEGER NOT NULL DEFAULT 0,
            question_text TEXT NOT NULL,
            question_type TEXT NOT NULL,
            options JSONB,
            required BOOLEAN DEFAULT TRUE,
            help_text TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_uw_cq_condition ON uw_condition_questions(condition_id, sort_order);")

    # 3. Medications (seeded from NIH RxNorm)
    op.execute("""
        CREATE TABLE IF NOT EXISTS uw_medications (
            id SERIAL PRIMARY KEY,
            rxcui TEXT,
            name TEXT NOT NULL,
            generic_name TEXT,
            brand_names TEXT[] DEFAULT '{}',
            drug_class TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_uw_medications_name_trgm ON uw_medications USING gin(name gin_trgm_ops);")
    op.execute("CREATE INDEX IF NOT EXISTS idx_uw_medications_rxcui ON uw_medications(rxcui);")

    # 4. Drug → Condition mapping
    op.execute("""
        CREATE TABLE IF NOT EXISTS uw_drug_condition_map (
            id SERIAL PRIMARY KEY,
            medication_id INTEGER NOT NULL REFERENCES uw_medications(id) ON DELETE CASCADE,
            condition_id INTEGER NOT NULL REFERENCES uw_conditions(id) ON DELETE CASCADE,
            is_primary BOOLEAN DEFAULT TRUE,
            UNIQUE(medication_id, condition_id)
        );
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_uw_dcm_med ON uw_drug_condition_map(medication_id);")

    # 5. Underwriting rules per carrier per condition
    op.execute("""
        CREATE TABLE IF NOT EXISTS uw_rules (
            id SERIAL PRIMARY KEY,
            carrier_key TEXT NOT NULL,
            carrier_name TEXT NOT NULL,
            condition_id INTEGER NOT NULL REFERENCES uw_conditions(id) ON DELETE CASCADE,
            product_type TEXT NOT NULL,
            underwriting_type TEXT NOT NULL DEFAULT 'simplified',
            rule_criteria JSONB NOT NULL,
            outcome TEXT NOT NULL,
            outcome_detail TEXT,
            waiting_period_months INTEGER,
            age_min INTEGER,
            age_max INTEGER,
            face_amount_min INTEGER,
            face_amount_max INTEGER,
            priority INTEGER DEFAULT 0,
            state_restrictions TEXT[],
            source_document TEXT,
            source_url TEXT,
            source_page TEXT,
            verified_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        );
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_uw_rules_carrier ON uw_rules(carrier_key, condition_id);")
    op.execute("CREATE INDEX IF NOT EXISTS idx_uw_rules_condition ON uw_rules(condition_id, product_type);")
    op.execute("CREATE INDEX IF NOT EXISTS idx_uw_rules_product ON uw_rules(product_type, underwriting_type);")

    # 6. Rate tables per carrier
    op.execute("""
        CREATE TABLE IF NOT EXISTS uw_rate_tables (
            id SERIAL PRIMARY KEY,
            carrier_key TEXT NOT NULL,
            product_type TEXT NOT NULL,
            rate_class TEXT NOT NULL,
            state TEXT NOT NULL,
            gender TEXT NOT NULL,
            tobacco_class TEXT NOT NULL DEFAULT 'non_tobacco',
            age INTEGER NOT NULL,
            face_amount INTEGER NOT NULL,
            payment_mode TEXT NOT NULL DEFAULT 'bank_draft',
            monthly_premium NUMERIC(8,2) NOT NULL,
            annual_premium NUMERIC(10,2),
            effective_date DATE,
            source_document TEXT,
            source_url TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        );
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_uw_rates_lookup
        ON uw_rate_tables(carrier_key, product_type, rate_class, state, gender, tobacco_class, age);
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_uw_rates_carrier ON uw_rate_tables(carrier_key);")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS uw_rate_tables CASCADE;")
    op.execute("DROP TABLE IF EXISTS uw_rules CASCADE;")
    op.execute("DROP TABLE IF EXISTS uw_drug_condition_map CASCADE;")
    op.execute("DROP TABLE IF EXISTS uw_medications CASCADE;")
    op.execute("DROP TABLE IF EXISTS uw_condition_questions CASCADE;")
    op.execute("DROP TABLE IF EXISTS uw_conditions CASCADE;")
