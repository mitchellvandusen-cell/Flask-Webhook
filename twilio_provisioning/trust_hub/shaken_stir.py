"""Module extracted from twilio_provisioning.py."""

import logging
from twilio.base.exceptions import TwilioRestException

from ..client import (
    get_sub_account_client_native,
    _ensure_sub_account_auth_token,
    _trusthub_update_status,
)
from .base import (
    SECONDARY_CUSTOMER_PROFILE_POLICY_SID,
    unassign_numbers_from_trust_product,
    _find_primary_profile_sid,
    _find_or_create_secondary_profile,
)

logger = logging.getLogger("twilio_provisioning")

SHAKEN_STIR_POLICY_SID = "RN7a97559effdf62d00f4298208492a5ea"

# ──────────────────────────────────────────────────────────────
# SHAKEN/STIR — ISV Sub-Account Trust Product
# ──────────────────────────────────────────────────────────────
# Per Twilio ISV docs:
#   https://www.twilio.com/docs/voice/trusted-calling-with-shakenstir/
#   shakenstir-onboarding/shaken-stir-trust-hub-api-isvs-subaccounts
#
# SHAKEN/STIR provides full attestation (A-level) for outbound calls,
# telling carriers "this call is legitimate." Improves answer rates
# beyond Voice Integrity alone.
#
# Simpler than Voice Integrity — no EndUser needed. Just:
#   1. Secondary Customer Profile (reuse existing)
#   2. SHAKEN/STIR Trust Product (policy_sid below)
#   3. Link Profile → Trust Product
#   4. Assign phone numbers
#   5. Evaluate + submit for review


def create_shaken_stir_trust_product(
    sub_account_sid: str,
    business_name: str,
    contact_email: str,
    sub_account_auth_token: str = "",
    existing_profile_sid: str = "",
) -> dict:
    """
    Create a SHAKEN/STIR Trust Product (ISV sub-account flow).

    Per Twilio ISV SHAKEN/STIR docs, the flow is:
      1. Find or create Secondary Customer Profile on sub-account
      2. Create SHAKEN/STIR Trust Product (policy_sid=RN7a97...)
      3. Link Profile → Trust Product (EntityAssignment)

    No EndUser needed (unlike Voice Integrity).
    Returns dict with trust_product_sid, profile_sid, status.
    """
    sub_account_auth_token = _ensure_sub_account_auth_token(
        sub_account_sid, sub_account_auth_token
    )
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)

    try:
        # ── Step 1: Secondary Customer Profile (ISV flow) ──
        primary_sid = _find_primary_profile_sid()
        profile_sid = _find_or_create_secondary_profile(
            client=client,
            sub_account_sid=sub_account_sid,
            business_name=business_name,
            contact_email=contact_email,
            existing_profile_sid=existing_profile_sid,
            primary_profile_sid=primary_sid,
            sub_account_auth_token=sub_account_auth_token,
        )

        # ── Step 2: SHAKEN/STIR Trust Product ──
        trust_product = client.trusthub.v1.trust_products.create(
            friendly_name=f"SHAKEN/STIR: {business_name}",
            email=contact_email,
            policy_sid=SHAKEN_STIR_POLICY_SID,
        )
        logger.info(f"[SHAKEN/STIR] Created Trust Product: {trust_product.sid}")

        # ── Step 3: Link Profile → Trust Product ──
        client.trusthub.v1.trust_products(trust_product.sid) \
            .trust_products_entity_assignments.create(
                object_sid=profile_sid,
            )
        logger.info(f"[SHAKEN/STIR] Linked profile {profile_sid} → {trust_product.sid}")

        return {
            "trust_product_sid": trust_product.sid,
            "profile_sid": profile_sid,
            "status": "draft",
            "business_name": business_name,
        }

    except TwilioRestException as e:
        logger.error(f"[SHAKEN/STIR] Trust Product creation failed: {e}")
        raise


def assign_numbers_to_shaken_stir(
    sub_account_sid: str,
    trust_product_sid: str,
    phone_number_sids: list,
    sub_account_auth_token: str = "",
    profile_sid: str = "",
) -> dict:
    """
    Assign phone numbers to a SHAKEN/STIR Trust Product.

    Numbers are first assigned to the Customer Profile (required by Twilio),
    then to the Trust Product. Same conflict resolution as Voice Integrity.
    """
    sub_account_auth_token = _ensure_sub_account_auth_token(
        sub_account_sid, sub_account_auth_token
    )
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    assigned = 0
    failed = []

    for pn_sid in phone_number_sids:
        try:
            # Assign to Customer Profile first (if provided)
            if profile_sid:
                try:
                    client.trusthub.v1.customer_profiles(profile_sid) \
                        .customer_profiles_channel_endpoint_assignment.create(
                            channel_endpoint_type="phone-number",
                            channel_endpoint_sid=pn_sid,
                        )
                    logger.info(f"[SHAKEN/STIR] Assigned {pn_sid} to profile {profile_sid}")
                except TwilioRestException as e:
                    if e.code == 20409 or e.status == 409:
                        logger.info(f"[SHAKEN/STIR] {pn_sid} already on profile (code={e.code})")
                    else:
                        raise

            # Then assign to Trust Product
            client.trusthub.v1.trust_products(trust_product_sid) \
                .trust_products_channel_endpoint_assignment.create(
                    channel_endpoint_type="phone-number",
                    channel_endpoint_sid=pn_sid,
                )
            assigned += 1
            logger.info(f"[SHAKEN/STIR] Assigned {pn_sid} to {trust_product_sid}")
        except TwilioRestException as e:
            if e.code == 20409 or e.status == 409:
                import re as _re
                err_str = str(e)
                conflict_match = _re.search(r'(?:already (?:assigned|mapped) to (?:a trust_product_sid: )?)(BU[a-f0-9A-F]+)', err_str)
                if not conflict_match:
                    conflict_match = _re.search(r'(BU[a-f0-9]{32})', err_str)
                conflict_sid = conflict_match.group(1) if conflict_match else None

                if conflict_sid and conflict_sid == trust_product_sid:
                    assigned += 1
                    logger.info(f"[SHAKEN/STIR] {pn_sid} already assigned to target TP (ok)")
                elif conflict_sid and conflict_sid != trust_product_sid:
                    logger.info(f"[SHAKEN/STIR] {pn_sid} stuck on {conflict_sid}, unassigning and retrying")
                    try:
                        unassign_numbers_from_trust_product(
                            sub_account_sid, conflict_sid, [pn_sid], sub_account_auth_token)
                        client.trusthub.v1.trust_products(trust_product_sid) \
                            .trust_products_channel_endpoint_assignment.create(
                                channel_endpoint_type="phone-number",
                                channel_endpoint_sid=pn_sid,
                            )
                        assigned += 1
                        logger.info(f"[SHAKEN/STIR] Retry succeeded: {pn_sid} → {trust_product_sid}")
                    except TwilioRestException as retry_err:
                        failed.append({"sid": pn_sid, "error": str(retry_err)})
                        logger.warning(f"[SHAKEN/STIR] Retry failed for {pn_sid}: {retry_err}")
                else:
                    if e.code == 20409:
                        assigned += 1
                        logger.info(f"[SHAKEN/STIR] {pn_sid} already assigned (20409)")
                    else:
                        failed.append({"sid": pn_sid, "error": str(e)})
            else:
                failed.append({"sid": pn_sid, "error": str(e)})
                logger.warning(f"[SHAKEN/STIR] Failed to assign {pn_sid}: {e}")

    return {"assigned": assigned, "failed": failed, "total": len(phone_number_sids)}


def submit_shaken_stir_for_review(
    sub_account_sid: str,
    trust_product_sid: str,
    sub_account_auth_token: str = "",
) -> dict:
    """
    Submit the SHAKEN/STIR Trust Product for Twilio review.
    Runs evaluation first, then sets status to pending-review.
    """
    sub_account_auth_token = _ensure_sub_account_auth_token(
        sub_account_sid, sub_account_auth_token
    )
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    try:
        # ── Check linked Customer Profile status first ──
        try:
            assignments = client.trusthub.v1.trust_products(trust_product_sid) \
                .trust_products_entity_assignments.list(limit=20)
            for a in assignments:
                obj_sid = getattr(a, "object_sid", "")
                if obj_sid.startswith("BU"):
                    try:
                        profile = client.trusthub.v1.customer_profiles(obj_sid).fetch()
                        profile_status = getattr(profile, "status", "unknown")
                        logger.info(f"[SHAKEN/STIR] Linked profile {obj_sid} status: {profile_status}")
                        if profile_status == "draft":
                            logger.warning(f"[SHAKEN/STIR] Profile {obj_sid} still in draft, submitting first")
                            _trusthub_update_status(
                                "CustomerProfiles", obj_sid, "pending-review",
                                sub_account_sid, sub_account_auth_token,
                            )
                    except TwilioRestException as profile_err:
                        logger.warning(f"[SHAKEN/STIR] Could not check/submit profile {obj_sid}: {profile_err}")
        except Exception as assign_err:
            logger.warning(f"[SHAKEN/STIR] Could not check profile status: {assign_err}")

        # ── Run evaluation ──
        eval_compliant = False
        try:
            evaluation = client.trusthub.v1.trust_products(trust_product_sid) \
                .trust_products_evaluations.create(policy_sid=SHAKEN_STIR_POLICY_SID)
            eval_status = getattr(evaluation, "status", "unknown")
            eval_results = getattr(evaluation, "results", None) or []
            logger.info(f"[SHAKEN/STIR] Evaluation: status={eval_status}, results={eval_results}")
            if eval_status == "compliant":
                eval_compliant = True
            elif eval_status == "noncompliant":
                error_details = []
                for r in eval_results:
                    if isinstance(r, dict) and r.get("status") == "noncompliant":
                        error_details.append(r.get("friendly_name", r.get("requirement_key", "unknown")))
                detail_msg = ", ".join(error_details) if error_details else "evaluation returned noncompliant"
                raise TwilioRestException(
                    status=400, uri="", msg=f"SHAKEN/STIR evaluation failed: {detail_msg}")
            else:
                logger.warning(f"[SHAKEN/STIR] Unexpected eval status '{eval_status}' — attempting submission")
        except TwilioRestException:
            raise
        except Exception as eval_err:
            logger.warning(f"[SHAKEN/STIR] Evaluation check failed (proceeding): {eval_err}")

        # ── Submit for review (direct HTTP POST — SDK bug workaround) ──
        resp_data = _trusthub_update_status(
            "TrustProducts", trust_product_sid, "pending-review",
            sub_account_sid, sub_account_auth_token,
        )
        tp_status = resp_data.get("status", "unknown")
        logger.info(f"[SHAKEN/STIR] Submitted {trust_product_sid} for review → {tp_status}")

        # ── Verify status actually changed ──
        if tp_status == "draft":
            logger.error(
                f"[SHAKEN/STIR] Trust Product {trust_product_sid} STILL in draft after "
                f"POST Status=pending-review. Eval: {'compliant' if eval_compliant else 'unconfirmed'}. "
                f"Response: {resp_data}"
            )
            raise TwilioRestException(
                status=400, uri="",
                msg="SHAKEN/STIR submission failed: Trust Product remained in 'draft'. "
                    "Check the Twilio Console or contact support.")

        return {"trust_product_sid": trust_product_sid, "status": tp_status}
    except TwilioRestException as e:
        logger.error(f"[SHAKEN/STIR] Submit for review failed: {e}")
        raise


def get_shaken_stir_status(
    sub_account_sid: str,
    trust_product_sid: str,
    sub_account_auth_token: str = "",
) -> dict:
    """
    Check the current status of a SHAKEN/STIR Trust Product.
    When rejected, fetches evaluation failure reasons.
    """
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    try:
        tp = client.trusthub.v1.trust_products(trust_product_sid).fetch()
        assigned_numbers = []
        try:
            assignments = client.trusthub.v1.trust_products(trust_product_sid) \
                .trust_products_channel_endpoint_assignment.list(limit=100)
            assigned_numbers = [a.channel_endpoint_sid for a in assignments]
        except Exception as e:
            logger.warning(f"[SHAKEN/STIR] Could not list assigned numbers: {e}")

        result = {
            "trust_product_sid": tp.sid,
            "status": tp.status,
            "friendly_name": getattr(tp, "friendly_name", ""),
            "date_created": tp.date_created.isoformat() if tp.date_created else "",
            "date_updated": tp.date_updated.isoformat() if tp.date_updated else "",
            "assigned_numbers": assigned_numbers,
            "assigned_count": len(assigned_numbers),
        }

        if tp.status == "twilio-rejected":
            try:
                evals = client.trusthub.v1.trust_products(trust_product_sid) \
                    .trust_products_evaluations.list(limit=5)
                if evals:
                    latest = evals[0]
                    eval_results = getattr(latest, "results", None) or []
                    failure_reasons = []
                    for r in eval_results:
                        if isinstance(r, dict) and r.get("status") in ("noncompliant", "failed"):
                            reason = r.get("friendly_name") or r.get("requirement_key") or "Unknown"
                            failure_reasons.append(reason)
                    result["failure_reasons"] = failure_reasons
                    result["evaluation_status"] = getattr(latest, "status", "")
            except Exception as eval_err:
                logger.warning(f"[SHAKEN/STIR] Could not fetch rejection reasons: {eval_err}")

        return result
    except TwilioRestException as e:
        logger.error(f"[SHAKEN/STIR] Status check failed: {e}")
        raise


def resubmit_shaken_stir(
    sub_account_sid: str,
    trust_product_sid: str,
    sub_account_auth_token: str = "",
    business_name: str = "",
    contact_email: str = "",
    existing_profile_sid: str = "",
) -> dict:
    """
    Resubmit a rejected SHAKEN/STIR registration.
    Creates a new Trust Product, re-links profile + numbers, submits fresh.
    """
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)

    # ── Gather existing entities from old TP ──
    old_assigned_numbers = []
    old_entity_sids = []
    try:
        assignments = client.trusthub.v1.trust_products(trust_product_sid) \
            .trust_products_channel_endpoint_assignment.list(limit=100)
        old_assigned_numbers = [a.channel_endpoint_sid for a in assignments]

        entity_assignments = client.trusthub.v1.trust_products(trust_product_sid) \
            .trust_products_entity_assignments.list(limit=100)
        old_entity_sids = [ea.object_sid for ea in entity_assignments]
    except Exception as e:
        logger.warning(f"[SHAKEN/STIR] Could not read old product entities: {e}")

    # ── Create new Trust Product ──
    try:
        new_tp = client.trusthub.v1.trust_products.create(
            friendly_name=f"SHAKEN/STIR: {business_name or sub_account_sid}",
            policy_sid=SHAKEN_STIR_POLICY_SID,
            email=contact_email or "support@omnisconn.com",
        )
        new_tp_sid = new_tp.sid
        logger.info(f"[SHAKEN/STIR] Created new Trust Product: {new_tp_sid} (replacing {trust_product_sid})")
    except TwilioRestException as e:
        logger.error(f"[SHAKEN/STIR] New Trust Product creation failed: {e}")
        raise

    # ── Re-link entities (profile only — no EndUser for SHAKEN/STIR) ──
    for entity_sid in old_entity_sids:
        try:
            client.trusthub.v1.trust_products(new_tp_sid) \
                .trust_products_entity_assignments.create(object_sid=entity_sid)
            logger.info(f"[SHAKEN/STIR] Re-linked entity {entity_sid} → {new_tp_sid}")
        except TwilioRestException as e:
            if e.code == 20409:
                logger.info(f"[SHAKEN/STIR] Entity {entity_sid} already linked (20409)")
            else:
                logger.warning(f"[SHAKEN/STIR] Entity re-link failed for {entity_sid}: {e}")

    # ── Re-assign phone numbers ──
    for pn_sid in old_assigned_numbers:
        try:
            client.trusthub.v1.trust_products(new_tp_sid) \
                .trust_products_channel_endpoint_assignment.create(
                    channel_endpoint_type="phone-number",
                    channel_endpoint_sid=pn_sid,
                )
            logger.info(f"[SHAKEN/STIR] Re-assigned number {pn_sid} → {new_tp_sid}")
        except TwilioRestException as e:
            if e.code == 20409:
                logger.info(f"[SHAKEN/STIR] Number {pn_sid} already assigned (20409)")
            else:
                logger.warning(f"[SHAKEN/STIR] Number re-assign failed for {pn_sid}: {e}")

    # ── Check linked profile status ──
    for entity_sid in old_entity_sids:
        if entity_sid.startswith("BU"):
            try:
                profile = client.trusthub.v1.customer_profiles(entity_sid).fetch()
                if getattr(profile, "status", "") == "draft":
                    _trusthub_update_status(
                        "CustomerProfiles", entity_sid, "pending-review",
                        sub_account_sid, sub_account_auth_token,
                    )
            except Exception as pe:
                logger.warning(f"[SHAKEN/STIR] Profile check failed for {entity_sid}: {pe}")

    # ── Evaluate and submit ──
    try:
        evaluation = client.trusthub.v1.trust_products(new_tp_sid) \
            .trust_products_evaluations.create(policy_sid=SHAKEN_STIR_POLICY_SID)
        eval_status = getattr(evaluation, "status", "unknown")
        eval_results = getattr(evaluation, "results", None) or []
        logger.info(f"[SHAKEN/STIR] Evaluation: status={eval_status}")

        if eval_status == "noncompliant":
            error_details = []
            for r in eval_results:
                if isinstance(r, dict) and r.get("status") == "noncompliant":
                    error_details.append(r.get("friendly_name", r.get("requirement_key", "unknown")))
            raise TwilioRestException(
                status=400, uri="",
                msg=f"SHAKEN/STIR evaluation failed on new product: {', '.join(error_details) or 'noncompliant'}. "
                    f"New product SID: {new_tp_sid}")
    except TwilioRestException:
        raise
    except Exception as eval_err:
        logger.warning(f"[SHAKEN/STIR] Evaluation check failed (proceeding): {eval_err}")

    resp_data = _trusthub_update_status(
        "TrustProducts", new_tp_sid, "pending-review",
        sub_account_sid, sub_account_auth_token,
    )
    tp_status = resp_data.get("status", "unknown")
    logger.info(f"[SHAKEN/STIR] Submitted new product {new_tp_sid} → {tp_status}")

    if tp_status == "draft":
        raise TwilioRestException(
            status=400, uri="",
            msg="SHAKEN/STIR submission failed: Trust Product remained in 'draft'.")

    return {
        "trust_product_sid": new_tp_sid,
        "old_trust_product_sid": trust_product_sid,
        "status": tp_status,
        "assigned_numbers": old_assigned_numbers,
    }


def remove_number_from_shaken_stir(
    sub_account_sid: str,
    trust_product_sid: str,
    phone_number_sid: str,
    sub_account_auth_token: str = "",
) -> bool:
    """Remove a phone number from a SHAKEN/STIR Trust Product."""
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    try:
        assignments = client.trusthub.v1.trust_products(trust_product_sid) \
            .trust_products_channel_endpoint_assignment.list(limit=100)
        for a in assignments:
            if a.channel_endpoint_sid == phone_number_sid:
                client.trusthub.v1.trust_products(trust_product_sid) \
                    .trust_products_channel_endpoint_assignment(a.sid).delete()
                logger.info(f"[SHAKEN/STIR] Removed {phone_number_sid} from {trust_product_sid}")
                return True
        logger.warning(f"[SHAKEN/STIR] {phone_number_sid} not found on {trust_product_sid}")
        return False
    except TwilioRestException as e:
        logger.error(f"[SHAKEN/STIR] Remove number failed: {e}")
        raise


def discover_shaken_stir_products(
    sub_account_sid: str,
    sub_account_auth_token: str = "",
) -> list:
    """
    Discover existing SHAKEN/STIR Trust Products on a sub-account.
    Returns list of {trust_product_sid, status, friendly_name} dicts.
    """
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    try:
        products = client.trusthub.v1.trust_products.list(limit=100)
        results = []
        for p in products:
            fn = getattr(p, "friendly_name", "") or ""
            if "shaken" in fn.lower() or "stir" in fn.lower():
                results.append({
                    "trust_product_sid": p.sid,
                    "status": p.status,
                    "friendly_name": fn,
                    "date_created": p.date_created.isoformat() if p.date_created else "",
                })
        logger.info(f"[SHAKEN/STIR] Discovered {len(results)} products")
        return results
    except TwilioRestException as e:
        logger.error(f"[SHAKEN/STIR] Discovery failed: {e}")
        return []



