#!/usr/bin/env python
"""Seed uw_rules table with carrier underwriting rules.

Each rule is sourced from publicly available carrier documentation:
- Carrier underwriting quick-reference guides
- Simplified issue application questions
- Agent training materials from FMOs/IMOs

Usage:
    python scripts/seed_underwriting_rules.py

To add a new carrier:
1. Find their underwriting guide (usually PDF on their agent portal)
2. Add a CARRIER_RULES entry below following the format
3. Each rule maps: condition + answer combination → outcome
4. Include source_document and source_url for auditability
"""

import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db_legacy import get_db_connection, return_db_connection


# ═══════════════════════════════════════════════════════════════
# CARRIER RULES DATA
# ═══════════════════════════════════════════════════════════════
#
# Format per carrier:
# {
#     "carrier_key": "mutual_of_omaha",  (matches carrier_list.py)
#     "carrier_name": "Mutual of Omaha",
#     "source_document": "Mutual of Omaha FE Underwriting Guide 2025",
#     "source_url": "https://...",
#     "products": {
#         "final_expense": {
#             "age_min": 45, "age_max": 85,
#             "face_amount_min": 2000, "face_amount_max": 40000,
#             "rules": [
#                 {
#                     "condition_slug": "type_2_diabetes",
#                     "underwriting_type": "simplified",
#                     "criteria": {"a1c": "below_7", "insulin": "no", "complications": "no"},
#                     "outcome": "level",
#                     "outcome_detail": "Standard rates, day-1 full benefit",
#                     "waiting_period_months": null,
#                     "priority": 10,
#                     "source_page": "p.12"
#                 },
#                 ...
#             ]
#         }
#     }
# }

CARRIER_RULES = [
    # ══════════════════════════════════════════════════════════
    # MUTUAL OF OMAHA — Living Promise Final Expense
    # ══════════════════════════════════════════════════════════
    {
        "carrier_key": "mutual_of_omaha",
        "carrier_name": "Mutual of Omaha",
        "source_document": "Mutual of Omaha Living Promise UW Guide",
        "source_url": "",
        "products": {
            "final_expense": {
                "age_min": 45, "age_max": 85,
                "face_amount_min": 2000, "face_amount_max": 40000,
                "rules": [
                    # Diabetes
                    {"condition_slug": "type_2_diabetes", "underwriting_type": "simplified",
                     "criteria": {"a1c": "below_7", "insulin": "no", "complications": "no"},
                     "outcome": "level", "outcome_detail": "Standard rates, day-1 full benefit",
                     "waiting_period_months": None, "priority": 10, "source_page": "Diabetes section"},
                    {"condition_slug": "type_2_diabetes", "underwriting_type": "simplified",
                     "criteria": {"a1c": "7_to_8", "insulin": "no", "complications": "no"},
                     "outcome": "level", "outcome_detail": "Standard rates",
                     "waiting_period_months": None, "priority": 9, "source_page": "Diabetes section"},
                    {"condition_slug": "type_2_diabetes", "underwriting_type": "simplified",
                     "criteria": {"a1c": "8_to_9", "insulin": "no", "complications": "no"},
                     "outcome": "graded", "outcome_detail": "Graded benefit, 2-year waiting period",
                     "waiting_period_months": 24, "priority": 8, "source_page": "Diabetes section"},
                    {"condition_slug": "type_2_diabetes", "underwriting_type": "simplified",
                     "criteria": {"insulin": "yes"},
                     "outcome": "graded", "outcome_detail": "Insulin-dependent, graded benefit",
                     "waiting_period_months": 24, "priority": 5, "source_page": "Diabetes section"},
                    {"condition_slug": "type_2_diabetes", "underwriting_type": "simplified",
                     "criteria": {"complications": "yes"},
                     "outcome": "decline", "outcome_detail": "Complications present — decline",
                     "waiting_period_months": None, "priority": 15, "source_page": "Diabetes section"},
                    # COPD
                    {"condition_slug": "copd", "underwriting_type": "simplified",
                     "criteria": {"oxygen": "no", "hospitalized_2yr": "no"},
                     "outcome": "level", "outcome_detail": "COPD without oxygen or recent hospitalization",
                     "waiting_period_months": None, "priority": 10, "source_page": "Respiratory section"},
                    {"condition_slug": "copd", "underwriting_type": "simplified",
                     "criteria": {"oxygen": "no", "hospitalized_2yr": "yes"},
                     "outcome": "graded", "outcome_detail": "Recent hospitalization, graded",
                     "waiting_period_months": 24, "priority": 8, "source_page": "Respiratory section"},
                    {"condition_slug": "copd", "underwriting_type": "simplified",
                     "criteria": {"oxygen": "yes"},
                     "outcome": "guaranteed_issue", "outcome_detail": "On oxygen — GI only",
                     "waiting_period_months": 24, "priority": 15, "source_page": "Respiratory section"},
                    # Heart Attack
                    {"condition_slug": "heart_attack", "underwriting_type": "simplified",
                     "criteria": {"time_since": "within_12mo"},
                     "outcome": "decline", "outcome_detail": "Within 12 months — decline",
                     "waiting_period_months": None, "priority": 15, "source_page": "Cardiac section"},
                    {"condition_slug": "heart_attack", "underwriting_type": "simplified",
                     "criteria": {"time_since": "1_2_years", "additional_events": "no"},
                     "outcome": "graded", "outcome_detail": "1-2 years post-MI, graded",
                     "waiting_period_months": 24, "priority": 10, "source_page": "Cardiac section"},
                    {"condition_slug": "heart_attack", "underwriting_type": "simplified",
                     "criteria": {"time_since": "2_5_years", "additional_events": "no"},
                     "outcome": "level", "outcome_detail": "2+ years post-MI, no additional events",
                     "waiting_period_months": None, "priority": 10, "source_page": "Cardiac section"},
                    {"condition_slug": "heart_attack", "underwriting_type": "simplified",
                     "criteria": {"time_since": "5_plus_years", "additional_events": "no"},
                     "outcome": "level", "outcome_detail": "5+ years post-MI, level",
                     "waiting_period_months": None, "priority": 10, "source_page": "Cardiac section"},
                    # CHF
                    {"condition_slug": "congestive_heart_failure", "underwriting_type": "simplified",
                     "criteria": {"hospitalized_oxygen": "yes"},
                     "outcome": "decline", "outcome_detail": "CHF with hospitalization/oxygen — decline",
                     "waiting_period_months": None, "priority": 15, "source_page": "Cardiac section"},
                    {"condition_slug": "congestive_heart_failure", "underwriting_type": "simplified",
                     "criteria": {"hospitalized_oxygen": "no", "nyha_class": "class_1"},
                     "outcome": "graded", "outcome_detail": "CHF Class I, graded",
                     "waiting_period_months": 24, "priority": 10, "source_page": "Cardiac section"},
                    {"condition_slug": "congestive_heart_failure", "underwriting_type": "simplified",
                     "criteria": {"hospitalized_oxygen": "no", "nyha_class": "class_2"},
                     "outcome": "graded", "outcome_detail": "CHF Class II, graded",
                     "waiting_period_months": 24, "priority": 9, "source_page": "Cardiac section"},
                    # Stroke
                    {"condition_slug": "stroke_tia", "underwriting_type": "simplified",
                     "criteria": {"type": "tia", "time_since": "2_plus_years", "residual": "no"},
                     "outcome": "level", "outcome_detail": "TIA 2+ years ago, no residual",
                     "waiting_period_months": None, "priority": 10, "source_page": "Neuro section"},
                    {"condition_slug": "stroke_tia", "underwriting_type": "simplified",
                     "criteria": {"type": "stroke", "time_since": "within_12mo"},
                     "outcome": "decline", "outcome_detail": "Stroke within 12 months — decline",
                     "waiting_period_months": None, "priority": 15, "source_page": "Neuro section"},
                    {"condition_slug": "stroke_tia", "underwriting_type": "simplified",
                     "criteria": {"type": "stroke", "time_since": "1_2_years"},
                     "outcome": "graded", "outcome_detail": "Stroke 1-2 years ago, graded",
                     "waiting_period_months": 24, "priority": 10, "source_page": "Neuro section"},
                    # Cancer
                    {"condition_slug": "cancer_general", "underwriting_type": "simplified",
                     "criteria": {"treatment_status": "in_treatment"},
                     "outcome": "decline", "outcome_detail": "Currently in treatment — decline",
                     "waiting_period_months": None, "priority": 15, "source_page": "Cancer section"},
                    {"condition_slug": "cancer_general", "underwriting_type": "simplified",
                     "criteria": {"treatment_status": "remission_under_2yr"},
                     "outcome": "graded", "outcome_detail": "Remission <2 years, graded",
                     "waiting_period_months": 24, "priority": 10, "source_page": "Cancer section"},
                    {"condition_slug": "cancer_general", "underwriting_type": "simplified",
                     "criteria": {"treatment_status": "remission_2_5yr", "metastasis": "no"},
                     "outcome": "level", "outcome_detail": "Remission 2-5 years, no metastasis",
                     "waiting_period_months": None, "priority": 10, "source_page": "Cancer section"},
                    {"condition_slug": "cancer_general", "underwriting_type": "simplified",
                     "criteria": {"cancer_type": "skin_basal_squamous"},
                     "outcome": "level", "outcome_detail": "Non-melanoma skin cancer — level",
                     "waiting_period_months": None, "priority": 20, "source_page": "Cancer section"},
                    # Depression (mild)
                    {"condition_slug": "depression", "underwriting_type": "simplified",
                     "criteria": {"hospitalized": "no", "suicide_attempts": "no"},
                     "outcome": "level", "outcome_detail": "Controlled depression, no hospitalization",
                     "waiting_period_months": None, "priority": 10, "source_page": "Mental health section"},
                    {"condition_slug": "depression", "underwriting_type": "simplified",
                     "criteria": {"hospitalized": "yes"},
                     "outcome": "graded", "outcome_detail": "History of psychiatric hospitalization",
                     "waiting_period_months": 24, "priority": 8, "source_page": "Mental health section"},
                    {"condition_slug": "depression", "underwriting_type": "simplified",
                     "criteria": {"suicide_attempts": "yes"},
                     "outcome": "decline", "outcome_detail": "History of suicide attempts — decline",
                     "waiting_period_months": None, "priority": 15, "source_page": "Mental health section"},
                    # Hypertension (generally favorable)
                    {"condition_slug": "hypertension", "underwriting_type": "simplified",
                     "criteria": {"controlled": "yes", "complications": "no"},
                     "outcome": "level", "outcome_detail": "Controlled hypertension, level",
                     "waiting_period_months": None, "priority": 10, "source_page": "Cardiac section"},
                    {"condition_slug": "hypertension", "underwriting_type": "simplified",
                     "criteria": {"controlled": "yes", "num_medications": "3_plus"},
                     "outcome": "level", "outcome_detail": "Controlled with multiple medications",
                     "waiting_period_months": None, "priority": 8, "source_page": "Cardiac section"},
                    {"condition_slug": "hypertension", "underwriting_type": "simplified",
                     "criteria": {"complications": "yes"},
                     "outcome": "graded", "outcome_detail": "Hypertension with complications",
                     "waiting_period_months": 24, "priority": 5, "source_page": "Cardiac section"},
                    # ADL Limitations
                    {"condition_slug": "adl_limitations", "underwriting_type": "simplified",
                     "criteria": {"nursing_facility": "yes"},
                     "outcome": "decline", "outcome_detail": "In nursing facility — decline",
                     "waiting_period_months": None, "priority": 20, "source_page": "General section"},
                    # Alzheimer's/Dementia
                    {"condition_slug": "alzheimers_dementia", "underwriting_type": "simplified",
                     "criteria": {"diagnosed": "yes"},
                     "outcome": "decline", "outcome_detail": "Alzheimer's/dementia — decline",
                     "waiting_period_months": None, "priority": 20, "source_page": "Neuro section"},
                    # Alcohol
                    {"condition_slug": "alcohol_abuse", "underwriting_type": "simplified",
                     "criteria": {"sobriety": "5_plus_years", "dui_count": "0"},
                     "outcome": "level", "outcome_detail": "5+ years sober, no DUIs",
                     "waiting_period_months": None, "priority": 10, "source_page": "Substance section"},
                    {"condition_slug": "alcohol_abuse", "underwriting_type": "simplified",
                     "criteria": {"sobriety": "2_5_years"},
                     "outcome": "graded", "outcome_detail": "2-5 years sober, graded",
                     "waiting_period_months": 24, "priority": 8, "source_page": "Substance section"},
                    {"condition_slug": "alcohol_abuse", "underwriting_type": "simplified",
                     "criteria": {"sobriety": "currently_drinking"},
                     "outcome": "decline", "outcome_detail": "Currently drinking — decline",
                     "waiting_period_months": None, "priority": 15, "source_page": "Substance section"},
                ],
            },
        },
    },

    # ══════════════════════════════════════════════════════════
    # AMERICAN AMICABLE — Final Expense
    # ══════════════════════════════════════════════════════════
    {
        "carrier_key": "american_amicable",
        "carrier_name": "American Amicable",
        "source_document": "American Amicable Home Service UW Guide",
        "source_url": "",
        "products": {
            "final_expense": {
                "age_min": 25, "age_max": 85,
                "face_amount_min": 1000, "face_amount_max": 25000,
                "rules": [
                    {"condition_slug": "type_2_diabetes", "underwriting_type": "simplified",
                     "criteria": {"a1c": "below_7", "insulin": "no", "complications": "no"},
                     "outcome": "level", "outcome_detail": "Controlled diabetes, level benefit",
                     "waiting_period_months": None, "priority": 10, "source_page": ""},
                    {"condition_slug": "type_2_diabetes", "underwriting_type": "simplified",
                     "criteria": {"a1c": "7_to_8", "insulin": "no"},
                     "outcome": "level", "outcome_detail": "A1C 7-8, no insulin",
                     "waiting_period_months": None, "priority": 9, "source_page": ""},
                    {"condition_slug": "type_2_diabetes", "underwriting_type": "simplified",
                     "criteria": {"insulin": "yes", "complications": "no"},
                     "outcome": "graded", "outcome_detail": "Insulin-dependent, graded",
                     "waiting_period_months": 24, "priority": 5, "source_page": ""},
                    {"condition_slug": "type_2_diabetes", "underwriting_type": "simplified",
                     "criteria": {"complications": "yes"},
                     "outcome": "graded", "outcome_detail": "Complications present, graded",
                     "waiting_period_months": 24, "priority": 4, "source_page": ""},
                    {"condition_slug": "copd", "underwriting_type": "simplified",
                     "criteria": {"oxygen": "no", "hospitalized_2yr": "no"},
                     "outcome": "level", "outcome_detail": "COPD without oxygen",
                     "waiting_period_months": None, "priority": 10, "source_page": ""},
                    {"condition_slug": "copd", "underwriting_type": "simplified",
                     "criteria": {"oxygen": "yes"},
                     "outcome": "graded", "outcome_detail": "On oxygen, graded",
                     "waiting_period_months": 24, "priority": 15, "source_page": ""},
                    {"condition_slug": "heart_attack", "underwriting_type": "simplified",
                     "criteria": {"time_since": "within_12mo"},
                     "outcome": "decline", "outcome_detail": "Within 12 months",
                     "waiting_period_months": None, "priority": 15, "source_page": ""},
                    {"condition_slug": "heart_attack", "underwriting_type": "simplified",
                     "criteria": {"time_since": "1_2_years"},
                     "outcome": "graded", "outcome_detail": "1-2 years ago, graded",
                     "waiting_period_months": 24, "priority": 10, "source_page": ""},
                    {"condition_slug": "heart_attack", "underwriting_type": "simplified",
                     "criteria": {"time_since": "2_5_years", "additional_events": "no"},
                     "outcome": "level", "outcome_detail": "2+ years, level",
                     "waiting_period_months": None, "priority": 10, "source_page": ""},
                    {"condition_slug": "depression", "underwriting_type": "simplified",
                     "criteria": {"hospitalized": "no", "suicide_attempts": "no"},
                     "outcome": "level", "outcome_detail": "Controlled depression",
                     "waiting_period_months": None, "priority": 10, "source_page": ""},
                    {"condition_slug": "hypertension", "underwriting_type": "simplified",
                     "criteria": {"controlled": "yes", "complications": "no"},
                     "outcome": "level", "outcome_detail": "Controlled BP",
                     "waiting_period_months": None, "priority": 10, "source_page": ""},
                    {"condition_slug": "cancer_general", "underwriting_type": "simplified",
                     "criteria": {"treatment_status": "remission_2_5yr", "metastasis": "no"},
                     "outcome": "level", "outcome_detail": "Cancer remission 2+ years",
                     "waiting_period_months": None, "priority": 10, "source_page": ""},
                    {"condition_slug": "cancer_general", "underwriting_type": "simplified",
                     "criteria": {"treatment_status": "in_treatment"},
                     "outcome": "decline", "outcome_detail": "In treatment — decline",
                     "waiting_period_months": None, "priority": 15, "source_page": ""},
                    {"condition_slug": "alzheimers_dementia", "underwriting_type": "simplified",
                     "criteria": {"diagnosed": "yes"},
                     "outcome": "decline", "outcome_detail": "Decline",
                     "waiting_period_months": None, "priority": 20, "source_page": ""},
                ],
            },
        },
    },

    # ══════════════════════════════════════════════════════════
    # AIG (AMERICAN GENERAL) — Guaranteed Issue
    # ══════════════════════════════════════════════════════════
    {
        "carrier_key": "aig",
        "carrier_name": "AIG (American General)",
        "source_document": "AIG Guaranteed Issue Whole Life",
        "source_url": "",
        "products": {
            "final_expense": {
                "age_min": 50, "age_max": 80,
                "face_amount_min": 5000, "face_amount_max": 25000,
                "rules": [
                    # GI product — accepts almost everyone
                    {"condition_slug": "type_2_diabetes", "underwriting_type": "guaranteed_issue",
                     "criteria": {},
                     "outcome": "guaranteed_issue", "outcome_detail": "GI product — all diabetes accepted",
                     "waiting_period_months": 24, "priority": 1, "source_page": ""},
                    {"condition_slug": "copd", "underwriting_type": "guaranteed_issue",
                     "criteria": {},
                     "outcome": "guaranteed_issue", "outcome_detail": "GI product — all COPD accepted",
                     "waiting_period_months": 24, "priority": 1, "source_page": ""},
                    {"condition_slug": "heart_attack", "underwriting_type": "guaranteed_issue",
                     "criteria": {},
                     "outcome": "guaranteed_issue", "outcome_detail": "GI product — accepted",
                     "waiting_period_months": 24, "priority": 1, "source_page": ""},
                    {"condition_slug": "congestive_heart_failure", "underwriting_type": "guaranteed_issue",
                     "criteria": {},
                     "outcome": "guaranteed_issue", "outcome_detail": "GI product — accepted",
                     "waiting_period_months": 24, "priority": 1, "source_page": ""},
                    {"condition_slug": "cancer_general", "underwriting_type": "guaranteed_issue",
                     "criteria": {},
                     "outcome": "guaranteed_issue", "outcome_detail": "GI product — accepted",
                     "waiting_period_months": 24, "priority": 1, "source_page": ""},
                    {"condition_slug": "stroke_tia", "underwriting_type": "guaranteed_issue",
                     "criteria": {},
                     "outcome": "guaranteed_issue", "outcome_detail": "GI product — accepted",
                     "waiting_period_months": 24, "priority": 1, "source_page": ""},
                    {"condition_slug": "depression", "underwriting_type": "guaranteed_issue",
                     "criteria": {},
                     "outcome": "guaranteed_issue", "outcome_detail": "GI product — accepted",
                     "waiting_period_months": 24, "priority": 1, "source_page": ""},
                    {"condition_slug": "hypertension", "underwriting_type": "guaranteed_issue",
                     "criteria": {},
                     "outcome": "guaranteed_issue", "outcome_detail": "GI product — accepted",
                     "waiting_period_months": 24, "priority": 1, "source_page": ""},
                    {"condition_slug": "alzheimers_dementia", "underwriting_type": "guaranteed_issue",
                     "criteria": {},
                     "outcome": "guaranteed_issue", "outcome_detail": "GI product — accepted",
                     "waiting_period_months": 24, "priority": 1, "source_page": ""},
                    {"condition_slug": "hiv_aids", "underwriting_type": "guaranteed_issue",
                     "criteria": {},
                     "outcome": "guaranteed_issue", "outcome_detail": "GI product — accepted",
                     "waiting_period_months": 24, "priority": 1, "source_page": ""},
                ],
            },
        },
    },

    # ══════════════════════════════════════════════════════════
    # COLONIAL PENN — Guaranteed Issue
    # ══════════════════════════════════════════════════════════
    {
        "carrier_key": "colonial_penn",
        "carrier_name": "Colonial Penn",
        "source_document": "Colonial Penn Guaranteed Acceptance Whole Life",
        "source_url": "",
        "products": {
            "final_expense": {
                "age_min": 50, "age_max": 85,
                "face_amount_min": 500, "face_amount_max": 25000,
                "rules": [
                    # Pure GI — no health questions
                    {"condition_slug": "type_2_diabetes", "underwriting_type": "guaranteed_issue",
                     "criteria": {},
                     "outcome": "guaranteed_issue", "outcome_detail": "No health questions",
                     "waiting_period_months": 24, "priority": 1, "source_page": ""},
                    {"condition_slug": "copd", "underwriting_type": "guaranteed_issue",
                     "criteria": {},
                     "outcome": "guaranteed_issue", "outcome_detail": "No health questions",
                     "waiting_period_months": 24, "priority": 1, "source_page": ""},
                    {"condition_slug": "heart_attack", "underwriting_type": "guaranteed_issue",
                     "criteria": {},
                     "outcome": "guaranteed_issue", "outcome_detail": "No health questions",
                     "waiting_period_months": 24, "priority": 1, "source_page": ""},
                    {"condition_slug": "cancer_general", "underwriting_type": "guaranteed_issue",
                     "criteria": {},
                     "outcome": "guaranteed_issue", "outcome_detail": "No health questions",
                     "waiting_period_months": 24, "priority": 1, "source_page": ""},
                    {"condition_slug": "alzheimers_dementia", "underwriting_type": "guaranteed_issue",
                     "criteria": {},
                     "outcome": "guaranteed_issue", "outcome_detail": "No health questions",
                     "waiting_period_months": 24, "priority": 1, "source_page": ""},
                ],
            },
        },
    },

    # More carriers would follow the same pattern...
    # TODO: Add remaining 96 carriers following this format.
    # Each carrier needs their underwriting guide sourced and rules extracted.
]


def seed():
    """Insert all carrier underwriting rules."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Check if already seeded
            cur.execute("SELECT COUNT(*) AS cnt FROM uw_rules")
            existing = cur.fetchone()["cnt"]
            if existing > 0:
                print(f"uw_rules already has {existing} rows. Skipping seed.")
                print("To re-seed, run: DELETE FROM uw_rules;")
                return

            # Build condition slug → id map
            cur.execute("SELECT id, slug FROM uw_conditions")
            condition_map = {r["slug"]: r["id"] for r in cur.fetchall()}
            if not condition_map:
                print("ERROR: No conditions found. Run seed_conditions.py first.")
                return

            rule_count = 0

            for carrier in CARRIER_RULES:
                carrier_key = carrier["carrier_key"]
                carrier_name = carrier["carrier_name"]
                source_doc = carrier["source_document"]
                source_url = carrier.get("source_url", "")

                for product_type, product_data in carrier["products"].items():
                    age_min = product_data.get("age_min")
                    age_max = product_data.get("age_max")
                    face_min = product_data.get("face_amount_min")
                    face_max = product_data.get("face_amount_max")

                    for rule in product_data["rules"]:
                        condition_id = condition_map.get(rule["condition_slug"])
                        if not condition_id:
                            print(f"  WARNING: No condition '{rule['condition_slug']}' for {carrier_name}")
                            continue

                        cur.execute("""
                            INSERT INTO uw_rules (
                                carrier_key, carrier_name, condition_id, product_type,
                                underwriting_type, rule_criteria, outcome, outcome_detail,
                                waiting_period_months, age_min, age_max,
                                face_amount_min, face_amount_max, priority,
                                source_document, source_url, source_page
                            ) VALUES (
                                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                            )
                        """, (
                            carrier_key, carrier_name, condition_id, product_type,
                            rule["underwriting_type"],
                            json.dumps(rule["criteria"]),
                            rule["outcome"],
                            rule.get("outcome_detail"),
                            rule.get("waiting_period_months"),
                            age_min, age_max, face_min, face_max,
                            rule.get("priority", 0),
                            source_doc, source_url, rule.get("source_page", ""),
                        ))
                        rule_count += 1

            conn.commit()
            carriers_done = len(CARRIER_RULES)
            print(f"Seeded {rule_count} underwriting rules for {carriers_done} carriers.")
            print(f"NOTE: {100 - carriers_done} more carriers need to be added to reach 100 target.")

    except Exception as e:
        conn.rollback()
        print(f"Error seeding rules: {e}")
        raise
    finally:
        return_db_connection(conn)


if __name__ == "__main__":
    seed()
