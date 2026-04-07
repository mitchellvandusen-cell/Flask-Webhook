# blueprints/quoting.py — Life insurance quoting engine API
#
# Pure deterministic quoting — zero AI calls. All data sourced from
# carrier documentation. Replaces InsuranceToolKits ($3K/mo).
#
# Routes:
#   GET  /api/quote/conditions?q=        — Search conditions (autocomplete)
#   GET  /api/quote/medications?q=       — Search medications (autocomplete)
#   GET  /api/quote/drug-conditions/<id> — Get conditions for a medication
#   GET  /api/quote/questions/<id>       — Get questionnaire for a condition
#   POST /api/quote/run                  — Run quote engine
#   GET  /api/quote/carrier/<key>        — Get carrier detail

import logging
from flask import Blueprint, request, jsonify
from flask_login import login_required

from db.quoting import (
    search_conditions,
    get_all_conditions,
    get_condition_questions,
    search_medications,
    get_drug_conditions,
    get_quotes,
    get_quotes_by_premium,
    get_carrier_detail,
    calculate_bmi,
)

logger = logging.getLogger(__name__)

quoting_bp = Blueprint("quoting", __name__)


@quoting_bp.route("/api/quote/conditions", methods=["GET"])
@login_required
def api_quote_conditions():
    """Search conditions by name/alias. Returns top 20 matches."""
    q = request.args.get("q", "").strip()
    if not q:
        # Return all conditions grouped by category
        rows = get_all_conditions()
        return jsonify([dict(r) for r in rows])

    if len(q) < 2:
        return jsonify([])

    rows = search_conditions(q, limit=20)
    return jsonify([dict(r) for r in rows])


@quoting_bp.route("/api/quote/medications", methods=["GET"])
@login_required
def api_quote_medications():
    """Search medications by name/generic/brand. Returns top 20 matches."""
    q = request.args.get("q", "").strip()
    if not q or len(q) < 2:
        return jsonify([])

    rows = search_medications(q, limit=20)
    results = []
    for r in rows:
        d = dict(r)
        # Convert brand_names array to list for JSON
        if d.get("brand_names"):
            d["brand_names"] = list(d["brand_names"])
        results.append(d)
    return jsonify(results)


@quoting_bp.route("/api/quote/drug-conditions/<int:med_id>", methods=["GET"])
@login_required
def api_quote_drug_conditions(med_id):
    """Get conditions mapped to a medication (for 'what is this for?' prompt)."""
    rows = get_drug_conditions(med_id)
    return jsonify([dict(r) for r in rows])


@quoting_bp.route("/api/quote/questions/<int:condition_id>", methods=["GET"])
@login_required
def api_quote_questions(condition_id):
    """Get questionnaire for a condition. All dropdown-based, no free text."""
    rows = get_condition_questions(condition_id)
    return jsonify([dict(r) for r in rows])


@quoting_bp.route("/api/quote/run", methods=["POST"])
@login_required
def api_quote_run():
    """Run the deterministic quote engine.

    Body JSON:
    {
        "age": 62,
        "gender": "female",
        "tobacco_class": "non_tobacco",
        "state": "TX",
        "face_amount": 15000,         // OR "max_premium": 50.00
        "product_type": "final_expense",
        "payment_mode": "bank_draft",
        "coverage_type_filter": "all",
        "height_inches": 66,          // optional
        "weight_lbs": 155,            // optional
        "conditions": [
            {
                "condition_id": 1,
                "condition_name": "Type 2 Diabetes",
                "answers": {"a1c": "below_7", "insulin": "no"}
            }
        ]
    }
    """
    data = request.get_json(silent=True) or {}

    # Validate required fields
    age = data.get("age")
    gender = data.get("gender")
    state = data.get("state")
    product_type = data.get("product_type")

    if not all([age, gender, state, product_type]):
        return jsonify({"error": "Missing required fields: age, gender, state, product_type"}), 400

    try:
        age = int(age)
    except (ValueError, TypeError):
        return jsonify({"error": "Age must be a number"}), 400

    tobacco_class = data.get("tobacco_class", "non_tobacco")
    payment_mode = data.get("payment_mode", "bank_draft")
    coverage_type_filter = data.get("coverage_type_filter", "all")
    conditions = data.get("conditions", [])
    height_inches = data.get("height_inches")
    weight_lbs = data.get("weight_lbs")

    # Calculate BMI if height/weight provided
    bmi = None
    if height_inches and weight_lbs:
        try:
            bmi = calculate_bmi(int(height_inches), int(weight_lbs))
        except (ValueError, TypeError):
            pass

    face_amount = data.get("face_amount")
    max_premium = data.get("max_premium")

    if not face_amount and not max_premium:
        return jsonify({"error": "Provide either face_amount or max_premium"}), 400

    try:
        if face_amount:
            face_amount = int(face_amount)
            results = get_quotes(
                age=age,
                gender=gender,
                tobacco_class=tobacco_class,
                state=state.upper(),
                face_amount=face_amount,
                product_type=product_type,
                payment_mode=payment_mode,
                coverage_type_filter=coverage_type_filter,
                conditions_with_answers=conditions,
                height_inches=int(height_inches) if height_inches else None,
                weight_lbs=int(weight_lbs) if weight_lbs else None,
            )
        else:
            max_premium = float(max_premium)
            results = get_quotes_by_premium(
                age=age,
                gender=gender,
                tobacco_class=tobacco_class,
                state=state.upper(),
                max_premium=max_premium,
                product_type=product_type,
                payment_mode=payment_mode,
                coverage_type_filter=coverage_type_filter,
                conditions_with_answers=conditions,
            )
    except Exception as e:
        logger.error(f"Quote engine error: {e}", exc_info=True)
        return jsonify({"error": "Quote engine error"}), 500

    return jsonify({
        "results": results,
        "input": {
            "age": age,
            "gender": gender,
            "tobacco_class": tobacco_class,
            "state": state.upper(),
            "face_amount": face_amount,
            "max_premium": max_premium,
            "product_type": product_type,
            "payment_mode": payment_mode,
            "coverage_type_filter": coverage_type_filter,
            "bmi": bmi,
            "conditions_count": len(conditions),
        },
        "total_results": len(results),
        "carriers_with_rates": len([r for r in results if r.get("monthly_premium")]),
    })


@quoting_bp.route("/api/quote/carrier/<carrier_key>", methods=["GET"])
@login_required
def api_quote_carrier(carrier_key):
    """Get carrier detail: available products, age ranges, face amounts."""
    detail = get_carrier_detail(carrier_key)
    if not detail or not detail.get("products"):
        return jsonify({"error": "Carrier not found"}), 404
    return jsonify(detail)
