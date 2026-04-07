# db/quoting.py — Data access layer for the quoting engine
#
# Pure deterministic quoting — zero AI. All data sourced from
# carrier documentation. Functions use get_db_connection() /
# return_db_connection() in try/finally per project convention.

import logging
from db.pool import get_db_connection, return_db_connection

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
# CONDITION SEARCH & QUESTIONNAIRES
# ═══════════════════════════════════════════════════════════════

def search_conditions(query, limit=20):
    """Trigram search on condition name + aliases. Returns list of dicts."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, name, slug, category, aliases, severity_default
                FROM uw_conditions
                WHERE name ILIKE %s
                   OR EXISTS (
                       SELECT 1 FROM unnest(aliases) AS a
                       WHERE a ILIKE %s
                   )
                ORDER BY similarity(name, %s) DESC, name
                LIMIT %s
            """, (f"%{query}%", f"%{query}%", query, limit))
            return cur.fetchall() or []
    finally:
        return_db_connection(conn)


def get_all_conditions():
    """Get all conditions grouped by category."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, name, slug, category, aliases, severity_default
                FROM uw_conditions
                ORDER BY category, name
            """)
            return cur.fetchall() or []
    finally:
        return_db_connection(conn)


def get_condition_questions(condition_id):
    """Get ordered questionnaire for a condition."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, question_text, question_type, options, required, help_text
                FROM uw_condition_questions
                WHERE condition_id = %s
                ORDER BY sort_order
            """, (condition_id,))
            return cur.fetchall() or []
    finally:
        return_db_connection(conn)


# ═══════════════════════════════════════════════════════════════
# MEDICATION SEARCH & DRUG-CONDITION MAPPING
# ═══════════════════════════════════════════════════════════════

def search_medications(query, limit=20):
    """Trigram search on medication name, generic_name, and brand_names."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, name, generic_name, brand_names, drug_class, rxcui
                FROM uw_medications
                WHERE name ILIKE %s
                   OR generic_name ILIKE %s
                   OR EXISTS (
                       SELECT 1 FROM unnest(brand_names) AS b
                       WHERE b ILIKE %s
                   )
                ORDER BY similarity(name, %s) DESC, name
                LIMIT %s
            """, (f"%{query}%", f"%{query}%", f"%{query}%", query, limit))
            return cur.fetchall() or []
    finally:
        return_db_connection(conn)


def get_drug_conditions(medication_id):
    """Get conditions mapped to a medication."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.id, c.name, c.slug, c.category, dcm.is_primary
                FROM uw_drug_condition_map dcm
                JOIN uw_conditions c ON c.id = dcm.condition_id
                WHERE dcm.medication_id = %s
                ORDER BY dcm.is_primary DESC, c.name
            """, (medication_id,))
            return cur.fetchall() or []
    finally:
        return_db_connection(conn)


# ═══════════════════════════════════════════════════════════════
# UNDERWRITING RULE EVALUATION
# ═══════════════════════════════════════════════════════════════

def _match_criteria(rule_criteria, answers):
    """Check if all rule criteria are satisfied by the given answers.

    Rule criteria is a flat JSONB dict like {"a1c": "below_7", "insulin": "no"}.
    Answers is a dict of the same shape from the questionnaire.
    Every key in rule_criteria must match the corresponding answer.
    """
    if not rule_criteria:
        return True
    for key, required_value in rule_criteria.items():
        answer_value = answers.get(key)
        if answer_value is None:
            return False
        if str(answer_value).lower() != str(required_value).lower():
            return False
    return True


# Outcome severity ordering — lower index = better outcome
_OUTCOME_SEVERITY = {
    "preferred_plus": 0,
    "preferred": 1,
    "standard_plus": 2,
    "standard": 3,
    "level": 4,
    "table_rated": 5,
    "graded_modified": 6,
    "graded": 7,
    "guaranteed_issue": 8,
    "postpone": 9,
    "decline": 10,
}


def _outcome_severity(outcome):
    """Get numeric severity for an outcome. Higher = worse."""
    return _OUTCOME_SEVERITY.get(outcome.lower(), 10)


def evaluate_underwriting(carrier_key, condition_id, product_type, answers, age=None, face_amount=None, state=None):
    """Evaluate a single condition against a carrier's underwriting rules.

    Returns the matching rule dict or None if no rule matches.
    Most specific rule (highest priority) wins.
    """
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, rule_criteria, outcome, outcome_detail,
                       waiting_period_months, age_min, age_max,
                       face_amount_min, face_amount_max, priority,
                       state_restrictions, source_document, source_url
                FROM uw_rules
                WHERE carrier_key = %s
                  AND condition_id = %s
                  AND product_type = %s
                ORDER BY priority DESC
            """, (carrier_key, condition_id, product_type))
            rules = cur.fetchall() or []
    finally:
        return_db_connection(conn)

    for rule in rules:
        # Check age restrictions
        if age is not None:
            if rule["age_min"] is not None and age < rule["age_min"]:
                continue
            if rule["age_max"] is not None and age > rule["age_max"]:
                continue
        # Check face amount restrictions
        if face_amount is not None:
            if rule["face_amount_min"] is not None and face_amount < rule["face_amount_min"]:
                continue
            if rule["face_amount_max"] is not None and face_amount > rule["face_amount_max"]:
                continue
        # Check state restrictions
        if state and rule["state_restrictions"]:
            if state.upper() not in [s.upper() for s in rule["state_restrictions"]]:
                continue
        # Check criteria match
        if _match_criteria(rule["rule_criteria"], answers):
            return dict(rule)

    return None


# ═══════════════════════════════════════════════════════════════
# RATE TABLE LOOKUP
# ═══════════════════════════════════════════════════════════════

def get_rate(carrier_key, product_type, rate_class, state, gender, tobacco_class, age, face_amount, payment_mode="bank_draft"):
    """Look up monthly premium from rate tables.

    If exact face_amount not found, finds nearest available.
    Returns dict with monthly_premium, annual_premium, or None.
    """
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Try exact match first
            cur.execute("""
                SELECT monthly_premium, annual_premium, face_amount,
                       effective_date, source_document
                FROM uw_rate_tables
                WHERE carrier_key = %s
                  AND product_type = %s
                  AND rate_class = %s
                  AND state = %s
                  AND gender = %s
                  AND tobacco_class = %s
                  AND age = %s
                  AND face_amount = %s
                  AND payment_mode = %s
                LIMIT 1
            """, (carrier_key, product_type, rate_class, state, gender,
                  tobacco_class, age, face_amount, payment_mode))
            row = cur.fetchone()
            if row:
                return dict(row)

            # Find nearest face amount
            cur.execute("""
                SELECT monthly_premium, annual_premium, face_amount,
                       effective_date, source_document
                FROM uw_rate_tables
                WHERE carrier_key = %s
                  AND product_type = %s
                  AND rate_class = %s
                  AND state = %s
                  AND gender = %s
                  AND tobacco_class = %s
                  AND age = %s
                  AND payment_mode = %s
                ORDER BY ABS(face_amount - %s)
                LIMIT 1
            """, (carrier_key, product_type, rate_class, state, gender,
                  tobacco_class, age, payment_mode, face_amount))
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        return_db_connection(conn)


def get_available_face_amounts(carrier_key, product_type, rate_class, state, gender, tobacco_class, age, payment_mode="bank_draft"):
    """Get all available face amounts for a given carrier/product combo."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT face_amount
                FROM uw_rate_tables
                WHERE carrier_key = %s
                  AND product_type = %s
                  AND rate_class = %s
                  AND state = %s
                  AND gender = %s
                  AND tobacco_class = %s
                  AND age = %s
                  AND payment_mode = %s
                ORDER BY face_amount
            """, (carrier_key, product_type, rate_class, state, gender,
                  tobacco_class, age, payment_mode))
            return [r["face_amount"] for r in cur.fetchall()]
    finally:
        return_db_connection(conn)


# ═══════════════════════════════════════════════════════════════
# BMI CALCULATION
# ═══════════════════════════════════════════════════════════════

def calculate_bmi(height_inches, weight_lbs):
    """Calculate BMI from height (inches) and weight (lbs).
    Returns float BMI value or None if inputs invalid.
    """
    if not height_inches or not weight_lbs or height_inches <= 0:
        return None
    return round((weight_lbs * 703) / (height_inches ** 2), 1)


# ═══════════════════════════════════════════════════════════════
# FULL QUOTE ENGINE
# ═══════════════════════════════════════════════════════════════

def get_carrier_products(product_type):
    """Get all carriers that have rules for a given product type."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT carrier_key, carrier_name
                FROM uw_rules
                WHERE product_type = %s
                ORDER BY carrier_name
            """, (product_type,))
            return cur.fetchall() or []
    finally:
        return_db_connection(conn)


def get_quotes(age, gender, tobacco_class, state, face_amount, product_type,
               payment_mode="bank_draft", coverage_type_filter="all",
               conditions_with_answers=None, height_inches=None, weight_lbs=None):
    """Run the full quote engine. Pure deterministic rule matching.

    Args:
        age: Client's age (integer)
        gender: "male" or "female"
        tobacco_class: "non_tobacco", "tobacco", "cigarette", "cigar", etc.
        state: 2-letter state code
        face_amount: Desired coverage amount in dollars
        product_type: "final_expense", "term_10", "term_20", etc.
        payment_mode: "bank_draft" or "direct_bill"
        coverage_type_filter: "all", "level", "graded", "guaranteed_issue"
        conditions_with_answers: List of {condition_id, condition_name, answers: {}}
        height_inches: Optional height for BMI
        weight_lbs: Optional weight for BMI

    Returns:
        List of result dicts sorted by class then premium, each containing:
        - carrier_key, carrier_name, outcome (class), monthly_premium
        - per_condition_results: [{condition_name, outcome, detail, source}]
        - waiting_period_months, rate_info
    """
    if conditions_with_answers is None:
        conditions_with_answers = []

    # Get all carriers offering this product
    carriers = get_carrier_products(product_type)
    if not carriers:
        return []

    results = []

    for carrier in carriers:
        carrier_key = carrier["carrier_key"]
        carrier_name = carrier["carrier_name"]

        # Evaluate each condition against this carrier
        per_condition = []
        worst_outcome = "level"  # Start with best possible
        worst_waiting = None

        for cond in conditions_with_answers:
            rule_result = evaluate_underwriting(
                carrier_key=carrier_key,
                condition_id=cond["condition_id"],
                product_type=product_type,
                answers=cond.get("answers", {}),
                age=age,
                face_amount=face_amount,
                state=state,
            )

            if rule_result:
                outcome = rule_result["outcome"]
                per_condition.append({
                    "condition_id": cond["condition_id"],
                    "condition_name": cond.get("condition_name", ""),
                    "outcome": outcome,
                    "outcome_detail": rule_result.get("outcome_detail", ""),
                    "waiting_period_months": rule_result.get("waiting_period_months"),
                    "source_document": rule_result.get("source_document", ""),
                    "source_url": rule_result.get("source_url", ""),
                })

                # Track worst outcome
                if _outcome_severity(outcome) > _outcome_severity(worst_outcome):
                    worst_outcome = outcome
                    worst_waiting = rule_result.get("waiting_period_months")
            else:
                # No rule found for this condition — carrier may not cover it
                per_condition.append({
                    "condition_id": cond["condition_id"],
                    "condition_name": cond.get("condition_name", ""),
                    "outcome": "unknown",
                    "outcome_detail": "No underwriting rule found for this condition",
                    "waiting_period_months": None,
                    "source_document": "",
                    "source_url": "",
                })

        # If no conditions, client is healthy — default to level
        if not conditions_with_answers:
            worst_outcome = "level"

        # Skip declined/postponed carriers
        if worst_outcome in ("decline", "postpone"):
            # Still include in results but mark as declined
            results.append({
                "carrier_key": carrier_key,
                "carrier_name": carrier_name,
                "outcome": worst_outcome,
                "monthly_premium": None,
                "annual_premium": None,
                "face_amount": face_amount,
                "waiting_period_months": worst_waiting,
                "per_condition_results": per_condition,
                "rate_info": None,
            })
            continue

        # Map outcome to rate class for lookup
        rate_class = worst_outcome
        if worst_outcome in ("preferred_plus", "preferred", "standard_plus", "standard"):
            rate_class = "level"  # These are all level benefit
        elif worst_outcome == "graded_modified":
            rate_class = "graded"

        # Look up rate
        rate = get_rate(
            carrier_key=carrier_key,
            product_type=product_type,
            rate_class=rate_class,
            state=state,
            gender=gender,
            tobacco_class=tobacco_class,
            age=age,
            face_amount=face_amount,
            payment_mode=payment_mode,
        )

        results.append({
            "carrier_key": carrier_key,
            "carrier_name": carrier_name,
            "outcome": worst_outcome,
            "monthly_premium": float(rate["monthly_premium"]) if rate else None,
            "annual_premium": float(rate["annual_premium"]) if rate and rate.get("annual_premium") else None,
            "face_amount": rate["face_amount"] if rate else face_amount,
            "waiting_period_months": worst_waiting,
            "per_condition_results": per_condition,
            "rate_info": {
                "effective_date": str(rate["effective_date"]) if rate and rate.get("effective_date") else None,
                "source_document": rate.get("source_document", "") if rate else "",
            } if rate else None,
        })

    # Filter by coverage type if requested
    if coverage_type_filter and coverage_type_filter != "all":
        filter_outcomes = {
            "level": {"level", "preferred_plus", "preferred", "standard_plus", "standard"},
            "graded": {"graded", "graded_modified"},
            "guaranteed_issue": {"guaranteed_issue"},
        }
        allowed = filter_outcomes.get(coverage_type_filter, set())
        results = [r for r in results if r["outcome"] in allowed]

    # Sort: by outcome severity (level first), then by premium ascending
    def sort_key(r):
        sev = _outcome_severity(r["outcome"])
        prem = r["monthly_premium"] if r["monthly_premium"] is not None else 999999
        return (sev, prem)

    results.sort(key=sort_key)
    return results


def get_quotes_by_premium(age, gender, tobacco_class, state, max_premium,
                          product_type, payment_mode="bank_draft",
                          coverage_type_filter="all",
                          conditions_with_answers=None):
    """Reverse lookup: given a budget, find max face amount per carrier.

    For each carrier, finds the highest face amount whose monthly premium
    is at or below max_premium.
    """
    if conditions_with_answers is None:
        conditions_with_answers = []

    carriers = get_carrier_products(product_type)
    if not carriers:
        return []

    results = []

    for carrier in carriers:
        carrier_key = carrier["carrier_key"]
        carrier_name = carrier["carrier_name"]

        # Evaluate conditions to determine rate class
        worst_outcome = "level"
        worst_waiting = None
        per_condition = []

        for cond in conditions_with_answers:
            # Use a mid-range face amount for rule evaluation (rules are usually amount-independent)
            rule_result = evaluate_underwriting(
                carrier_key=carrier_key,
                condition_id=cond["condition_id"],
                product_type=product_type,
                answers=cond.get("answers", {}),
                age=age,
                state=state,
            )
            if rule_result:
                outcome = rule_result["outcome"]
                per_condition.append({
                    "condition_id": cond["condition_id"],
                    "condition_name": cond.get("condition_name", ""),
                    "outcome": outcome,
                    "outcome_detail": rule_result.get("outcome_detail", ""),
                })
                if _outcome_severity(outcome) > _outcome_severity(worst_outcome):
                    worst_outcome = outcome
                    worst_waiting = rule_result.get("waiting_period_months")

        if worst_outcome in ("decline", "postpone"):
            continue

        rate_class = worst_outcome
        if worst_outcome in ("preferred_plus", "preferred", "standard_plus", "standard"):
            rate_class = "level"
        elif worst_outcome == "graded_modified":
            rate_class = "graded"

        # Find max face amount within budget
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT face_amount, monthly_premium, annual_premium
                    FROM uw_rate_tables
                    WHERE carrier_key = %s
                      AND product_type = %s
                      AND rate_class = %s
                      AND state = %s
                      AND gender = %s
                      AND tobacco_class = %s
                      AND age = %s
                      AND payment_mode = %s
                      AND monthly_premium <= %s
                    ORDER BY face_amount DESC
                    LIMIT 1
                """, (carrier_key, product_type, rate_class, state, gender,
                      tobacco_class, age, payment_mode, max_premium))
                rate = cur.fetchone()
        finally:
            return_db_connection(conn)

        if rate:
            results.append({
                "carrier_key": carrier_key,
                "carrier_name": carrier_name,
                "outcome": worst_outcome,
                "monthly_premium": float(rate["monthly_premium"]),
                "annual_premium": float(rate["annual_premium"]) if rate.get("annual_premium") else None,
                "face_amount": rate["face_amount"],
                "waiting_period_months": worst_waiting,
                "per_condition_results": per_condition,
            })

    # Filter
    if coverage_type_filter and coverage_type_filter != "all":
        filter_outcomes = {
            "level": {"level", "preferred_plus", "preferred", "standard_plus", "standard"},
            "graded": {"graded", "graded_modified"},
            "guaranteed_issue": {"guaranteed_issue"},
        }
        allowed = filter_outcomes.get(coverage_type_filter, set())
        results = [r for r in results if r["outcome"] in allowed]

    # Sort by face amount descending (most coverage for the budget)
    results.sort(key=lambda r: r["face_amount"], reverse=True)
    return results


def get_carrier_detail(carrier_key):
    """Get carrier info: available products, age ranges, face amounts."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Get distinct products and their ranges
            cur.execute("""
                SELECT DISTINCT product_type,
                       MIN(age_min) AS min_age,
                       MAX(age_max) AS max_age,
                       MIN(face_amount_min) AS min_face,
                       MAX(face_amount_max) AS max_face
                FROM uw_rules
                WHERE carrier_key = %s
                GROUP BY product_type
                ORDER BY product_type
            """, (carrier_key,))
            products = cur.fetchall() or []

            # Get carrier name
            cur.execute("""
                SELECT DISTINCT carrier_name
                FROM uw_rules
                WHERE carrier_key = %s
                LIMIT 1
            """, (carrier_key,))
            name_row = cur.fetchone()

            return {
                "carrier_key": carrier_key,
                "carrier_name": name_row["carrier_name"] if name_row else carrier_key,
                "products": [dict(p) for p in products],
            }
    finally:
        return_db_connection(conn)
