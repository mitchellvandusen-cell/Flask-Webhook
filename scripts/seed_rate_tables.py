#!/usr/bin/env python
"""Seed uw_rate_tables with carrier rate data.

Rate data sourced from publicly available carrier rate cards.
Each carrier publishes rate cards for their agents — these are
public knowledge available from carrier agent portals and FMO sites.

Usage:
    python scripts/seed_rate_tables.py

To add a new carrier's rates:
1. Find their rate card (usually PDF from agent portal)
2. Add a CARRIER_RATES entry below
3. Include source_document for auditability

Rate format: list of (age, face_amount, monthly_premium) tuples per
carrier/product/class/state/gender/tobacco combination.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db_legacy import get_db_connection, return_db_connection


# ═══════════════════════════════════════════════════════════════
# RATE TABLE DATA
# ═══════════════════════════════════════════════════════════════
#
# Format:
# {
#     "carrier_key": "mutual_of_omaha",
#     "source_document": "Mutual of Omaha FE Rate Card 2025",
#     "rates": [
#         {
#             "product_type": "final_expense",
#             "rate_class": "level",
#             "state": "TX",  (or "*" for all states with same rate)
#             "gender": "male",
#             "tobacco_class": "non_tobacco",
#             "payment_mode": "bank_draft",
#             "data": [
#                 (age, face_amount, monthly_premium),
#                 (50, 5000, 18.50),
#                 (50, 10000, 33.20),
#                 ...
#             ]
#         }
#     ]
# }

# Common state list (for carriers with same rates across all states)
ALL_STATES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC",
]

# Sample rate data — these are EXAMPLE rates for demonstration.
# Real rates must be sourced from each carrier's published rate card.
# NOTE: These are approximate/sample rates and MUST be verified against
# actual carrier rate cards before production use.

CARRIER_RATES = [
    {
        "carrier_key": "mutual_of_omaha",
        "source_document": "Mutual of Omaha Living Promise Rate Card (Sample)",
        "rates": [
            # Level benefit, Male, Non-tobacco, Bank Draft
            # Common face amounts: $5K, $10K, $15K, $20K, $25K
            {
                "product_type": "final_expense",
                "rate_class": "level",
                "state": "*",
                "gender": "male",
                "tobacco_class": "non_tobacco",
                "payment_mode": "bank_draft",
                "data": [
                    # (age, face_amount, monthly_premium)
                    (50, 5000, 16.75), (50, 10000, 29.90), (50, 15000, 43.05), (50, 20000, 56.20), (50, 25000, 69.35),
                    (55, 5000, 20.10), (55, 10000, 36.40), (55, 15000, 52.70), (55, 20000, 69.00), (55, 25000, 85.30),
                    (60, 5000, 24.80), (60, 10000, 45.90), (60, 15000, 67.00), (60, 20000, 88.10), (60, 25000, 109.20),
                    (65, 5000, 31.50), (65, 10000, 59.30), (65, 15000, 87.10), (65, 20000, 114.90), (65, 25000, 142.70),
                    (70, 5000, 41.20), (70, 10000, 78.60), (70, 15000, 116.00), (70, 20000, 153.40), (70, 25000, 190.80),
                    (75, 5000, 55.80), (75, 10000, 107.80), (75, 15000, 159.80), (75, 20000, 211.80), (75, 25000, 263.80),
                    (80, 5000, 76.40), (80, 10000, 149.00), (80, 15000, 221.60), (80, 20000, 294.20), (80, 25000, 366.80),
                ],
            },
            # Level benefit, Female, Non-tobacco, Bank Draft
            {
                "product_type": "final_expense",
                "rate_class": "level",
                "state": "*",
                "gender": "female",
                "tobacco_class": "non_tobacco",
                "payment_mode": "bank_draft",
                "data": [
                    (50, 5000, 13.90), (50, 10000, 24.20), (50, 15000, 34.50), (50, 20000, 44.80), (50, 25000, 55.10),
                    (55, 5000, 16.50), (55, 10000, 29.30), (55, 15000, 42.10), (55, 20000, 54.90), (55, 25000, 67.70),
                    (60, 5000, 20.40), (60, 10000, 37.00), (60, 15000, 53.60), (60, 20000, 70.20), (60, 25000, 86.80),
                    (65, 5000, 25.80), (65, 10000, 47.80), (65, 15000, 69.80), (65, 20000, 91.80), (65, 25000, 113.80),
                    (70, 5000, 33.60), (70, 10000, 63.40), (70, 15000, 93.20), (70, 20000, 123.00), (70, 25000, 152.80),
                    (75, 5000, 45.20), (75, 10000, 86.60), (75, 15000, 128.00), (75, 20000, 169.40), (75, 25000, 210.80),
                    (80, 5000, 61.80), (80, 10000, 119.80), (80, 15000, 177.80), (80, 20000, 235.80), (80, 25000, 293.80),
                ],
            },
            # Level benefit, Male, Tobacco, Bank Draft
            {
                "product_type": "final_expense",
                "rate_class": "level",
                "state": "*",
                "gender": "male",
                "tobacco_class": "tobacco",
                "payment_mode": "bank_draft",
                "data": [
                    (50, 5000, 22.90), (50, 10000, 42.00), (50, 15000, 61.10), (50, 20000, 80.20), (50, 25000, 99.30),
                    (55, 5000, 28.50), (55, 10000, 53.20), (55, 15000, 77.90), (55, 20000, 102.60), (55, 25000, 127.30),
                    (60, 5000, 36.80), (60, 10000, 69.80), (60, 15000, 102.80), (60, 20000, 135.80), (60, 25000, 168.80),
                    (65, 5000, 48.50), (65, 10000, 93.20), (65, 15000, 137.90), (65, 20000, 182.60), (65, 25000, 227.30),
                    (70, 5000, 64.80), (70, 10000, 125.80), (70, 15000, 186.80), (70, 20000, 247.80), (70, 25000, 308.80),
                    (75, 5000, 89.20), (75, 10000, 174.60), (75, 15000, 260.00), (75, 20000, 345.40), (75, 25000, 430.80),
                ],
            },
            # Level benefit, Female, Tobacco, Bank Draft
            {
                "product_type": "final_expense",
                "rate_class": "level",
                "state": "*",
                "gender": "female",
                "tobacco_class": "tobacco",
                "payment_mode": "bank_draft",
                "data": [
                    (50, 5000, 18.60), (50, 10000, 33.40), (50, 15000, 48.20), (50, 20000, 63.00), (50, 25000, 77.80),
                    (55, 5000, 23.20), (55, 10000, 42.60), (55, 15000, 62.00), (55, 20000, 81.40), (55, 25000, 100.80),
                    (60, 5000, 30.00), (60, 10000, 56.20), (60, 15000, 82.40), (60, 20000, 108.60), (60, 25000, 134.80),
                    (65, 5000, 39.40), (65, 10000, 75.00), (65, 15000, 110.60), (65, 20000, 146.20), (65, 25000, 181.80),
                    (70, 5000, 52.80), (70, 10000, 101.80), (70, 15000, 150.80), (70, 20000, 199.80), (70, 25000, 248.80),
                    (75, 5000, 72.60), (75, 10000, 141.40), (75, 15000, 210.20), (75, 20000, 279.00), (75, 25000, 347.80),
                ],
            },
            # Graded benefit, Male, Non-tobacco (higher premiums)
            {
                "product_type": "final_expense",
                "rate_class": "graded",
                "state": "*",
                "gender": "male",
                "tobacco_class": "non_tobacco",
                "payment_mode": "bank_draft",
                "data": [
                    (50, 5000, 21.50), (50, 10000, 39.20), (50, 15000, 56.90), (50, 20000, 74.60),
                    (55, 5000, 26.30), (55, 10000, 48.80), (55, 15000, 71.30), (55, 20000, 93.80),
                    (60, 5000, 33.40), (60, 10000, 63.00), (60, 15000, 92.60), (60, 20000, 122.20),
                    (65, 5000, 43.80), (65, 10000, 83.80), (65, 15000, 123.80), (65, 20000, 163.80),
                    (70, 5000, 58.60), (70, 10000, 113.40), (70, 15000, 168.20), (70, 20000, 223.00),
                    (75, 5000, 80.20), (75, 10000, 156.60), (75, 15000, 233.00), (75, 20000, 309.40),
                ],
            },
            # Graded benefit, Female, Non-tobacco
            {
                "product_type": "final_expense",
                "rate_class": "graded",
                "state": "*",
                "gender": "female",
                "tobacco_class": "non_tobacco",
                "payment_mode": "bank_draft",
                "data": [
                    (50, 5000, 17.80), (50, 10000, 31.80), (50, 15000, 45.80), (50, 20000, 59.80),
                    (55, 5000, 21.60), (55, 10000, 39.40), (55, 15000, 57.20), (55, 20000, 75.00),
                    (60, 5000, 27.40), (60, 10000, 51.00), (60, 15000, 74.60), (60, 20000, 98.20),
                    (65, 5000, 35.80), (65, 10000, 67.80), (65, 15000, 99.80), (65, 20000, 131.80),
                    (70, 5000, 47.80), (70, 10000, 91.80), (70, 15000, 135.80), (70, 20000, 179.80),
                    (75, 5000, 65.40), (75, 10000, 127.00), (75, 15000, 188.60), (75, 20000, 250.20),
                ],
            },
        ],
    },

    # More carriers would follow...
    # TODO: Add remaining carriers' rate data from their published rate cards.
]


def seed():
    """Insert all carrier rate data."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Check if already seeded
            cur.execute("SELECT COUNT(*) AS cnt FROM uw_rate_tables")
            existing = cur.fetchone()["cnt"]
            if existing > 0:
                print(f"uw_rate_tables already has {existing} rows. Skipping seed.")
                print("To re-seed, run: DELETE FROM uw_rate_tables;")
                return

            rate_count = 0

            for carrier in CARRIER_RATES:
                carrier_key = carrier["carrier_key"]
                source_doc = carrier["source_document"]

                for rate_set in carrier["rates"]:
                    product_type = rate_set["product_type"]
                    rate_class = rate_set["rate_class"]
                    state_spec = rate_set["state"]
                    gender = rate_set["gender"]
                    tobacco_class = rate_set["tobacco_class"]
                    payment_mode = rate_set["payment_mode"]

                    # Determine which states to insert for
                    states = ALL_STATES if state_spec == "*" else [state_spec]

                    for state in states:
                        for age, face_amount, monthly in rate_set["data"]:
                            # Calculate annual (monthly × 12, with slight discount)
                            annual = round(monthly * 11.5, 2)  # ~4% annual discount

                            cur.execute("""
                                INSERT INTO uw_rate_tables (
                                    carrier_key, product_type, rate_class, state,
                                    gender, tobacco_class, age, face_amount,
                                    payment_mode, monthly_premium, annual_premium,
                                    source_document
                                ) VALUES (
                                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                                )
                            """, (
                                carrier_key, product_type, rate_class, state,
                                gender, tobacco_class, age, face_amount,
                                payment_mode, monthly, annual,
                                source_doc,
                            ))
                            rate_count += 1

                    if rate_count % 5000 == 0:
                        print(f"  ... inserted {rate_count} rates so far")

            conn.commit()
            print(f"Seeded {rate_count} rate table entries for {len(CARRIER_RATES)} carriers.")
            print(f"NOTE: Rate data is SAMPLE data. Must be verified against actual carrier rate cards.")

    except Exception as e:
        conn.rollback()
        print(f"Error seeding rates: {e}")
        raise
    finally:
        return_db_connection(conn)


if __name__ == "__main__":
    seed()
