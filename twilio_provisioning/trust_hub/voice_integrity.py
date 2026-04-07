"""Module extracted from twilio_provisioning.py."""

import logging
from twilio.base.exceptions import TwilioRestException

from ..client import (
    get_sub_account_client_native,
    _ensure_sub_account_auth_token,
    _trusthub_update_status,
)
from .base import (
    VOICE_INTEGRITY_POLICY_SID,
    VOICE_INTEGRITY_CARRIERS,
    unassign_numbers_from_trust_product,
    _find_primary_profile_sid,
    _find_or_create_secondary_profile,
)

logger = logging.getLogger("twilio_provisioning")


def create_voice_integrity_trust_product(
    sub_account_sid: str,
    business_name: str,
    contact_email: str,
    sub_account_auth_token: str = "",
    existing_profile_sid: str = "",
    business_employee_count: str = "1",
    average_call_volume: str = "500",
    use_case: str = "Lead Management",
) -> dict:
    """
    Create a Voice Integrity Trust Product (ISV sub-account flow).

    Per Twilio ISV docs:
      https://www.twilio.com/docs/voice/spam-monitoring-with-voiceintegrity/
      voice-integrity-onboarding/voice-integrity-trust-hub-api-isvs-subaccounts

    ISV flow:
      1. Find or create Secondary Customer Profile on sub-account
      2. Create EndUser (voice_integrity_information) with use case + call volume
      3. Create Voice Integrity Trust Product (policy_sid=RN5b3660f9598883b1df4e77f77acefba0)
      4. Link Profile → Trust Product (EntityAssignment)
      5. Link EndUser → Trust Product (EntityAssignment)

    Master account manages its own Voice Integrity via the Twilio Console.
    Returns dict with trust_product_sid, profile_sid, end_user_sid, status.
    """
    sub_account_auth_token = _ensure_sub_account_auth_token(
        sub_account_sid, sub_account_auth_token
    )
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)

    try:
        # ── Step 1: Secondary Customer Profile (ISV flow) ──
        # Per Twilio ISV docs: find or create a Secondary Customer Profile
        # on the sub-account, linked to the master's Primary Business Profile.
        # https://www.twilio.com/docs/voice/spam-monitoring-with-voiceintegrity/
        # voice-integrity-onboarding/voice-integrity-trust-hub-api-isvs-subaccounts
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

        # ── Step 2: EndUser with voice_integrity_information ──
        end_user = client.trusthub.v1.end_users.create(
            friendly_name=f"Voice Integrity EndUser: {business_name}",
            type="voice_integrity_information",
            attributes={
                "use_case": use_case,
                "business_employee_count": str(int(business_employee_count)),
                "average_business_day_call_volume": str(int(average_call_volume)),
                "notes": f"Insurance agency outbound dialer for {business_name}",
            },
        )
        logger.info(f"[VoiceIntegrity] Created EndUser: {end_user.sid}")

        # ── Step 3: Voice Integrity Trust Product ──
        trust_product = client.trusthub.v1.trust_products.create(
            friendly_name=f"Voice Integrity: {business_name}",
            email=contact_email,
            policy_sid=VOICE_INTEGRITY_POLICY_SID,
        )
        logger.info(f"[VoiceIntegrity] Created Trust Product: {trust_product.sid}")

        # ── Step 4: Link Profile → Trust Product ──
        client.trusthub.v1.trust_products(trust_product.sid) \
            .trust_products_entity_assignments.create(
                object_sid=profile_sid,
            )
        logger.info(f"[VoiceIntegrity] Linked profile {profile_sid} → {trust_product.sid}")

        # ── Step 5: Link EndUser → Trust Product ──
        client.trusthub.v1.trust_products(trust_product.sid) \
            .trust_products_entity_assignments.create(
                object_sid=end_user.sid,
            )
        logger.info(f"[VoiceIntegrity] Linked EndUser {end_user.sid} → {trust_product.sid}")

        return {
            "trust_product_sid": trust_product.sid,
            "profile_sid": profile_sid,
            "end_user_sid": end_user.sid,
            "status": "draft",
            "business_name": business_name,
        }

    except TwilioRestException as e:
        logger.error(f"[VoiceIntegrity] Trust Product creation failed: {e}")
        raise


def assign_numbers_to_voice_integrity(
    sub_account_sid: str,
    trust_product_sid: str,
    phone_number_sids: list,
    sub_account_auth_token: str = "",
    profile_sid: str = "",
) -> dict:
    """
    Assign phone numbers to a Voice Integrity Trust Product.

    If profile_sid is provided, numbers are first assigned to the Customer Profile
    (required by Twilio for proper carrier registration), then to the Trust Product.
    Returns dict with assigned count and any failures.
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
                    logger.info(f"[VoiceIntegrity] Assigned {pn_sid} to profile {profile_sid}")
                except TwilioRestException as e:
                    # 20409 or HTTP 409 (code 70003) = already assigned to this profile — fine
                    if e.code == 20409 or e.status == 409:
                        logger.info(f"[VoiceIntegrity] {pn_sid} already on profile (code={e.code}, status={e.status})")
                    else:
                        raise

            # Then assign to Trust Product
            client.trusthub.v1.trust_products(trust_product_sid) \
                .trust_products_channel_endpoint_assignment.create(
                    channel_endpoint_type="phone-number",
                    channel_endpoint_sid=pn_sid,
                )
            assigned += 1
            logger.info(f"[VoiceIntegrity] Assigned {pn_sid} to {trust_product_sid}")
        except TwilioRestException as e:
            # 20409 or HTTP 409 (code 70003) = already assigned to a Trust Product.
            # If it's assigned to THIS TP, treat as success.
            # If it's assigned to ANOTHER TP (e.g. old rejected one), unassign and retry.
            if e.code == 20409 or e.status == 409:
                # Extract the conflicting BU SID from the error message
                # Twilio may say "already assigned to BU..." or "already mapped to a trust_product_sid: BU..."
                import re as _re
                err_str = str(e)
                conflict_match = _re.search(r'(?:already (?:assigned|mapped) to (?:a trust_product_sid: )?)(BU[a-f0-9A-F]+)', err_str)
                if not conflict_match:
                    # Fallback: find any BU SID in the error message
                    conflict_match = _re.search(r'(BU[a-f0-9]{32})', err_str)
                conflict_sid = conflict_match.group(1) if conflict_match else None

                if conflict_sid and conflict_sid == trust_product_sid:
                    # Already assigned to our target — treat as success
                    assigned += 1
                    logger.info(f"[VoiceIntegrity] {pn_sid} already assigned to target TP (ok)")
                elif conflict_sid and conflict_sid != trust_product_sid:
                    # Assigned to a different Trust Product — unassign from it and retry
                    logger.info(f"[VoiceIntegrity] {pn_sid} stuck on {conflict_sid}, unassigning and retrying")
                    try:
                        unassign_numbers_from_trust_product(
                            sub_account_sid, conflict_sid, [pn_sid], sub_account_auth_token)
                        # Retry assignment to our target TP
                        client.trusthub.v1.trust_products(trust_product_sid) \
                            .trust_products_channel_endpoint_assignment.create(
                                channel_endpoint_type="phone-number",
                                channel_endpoint_sid=pn_sid,
                            )
                        assigned += 1
                        logger.info(f"[VoiceIntegrity] Retry succeeded: {pn_sid} → {trust_product_sid}")
                    except TwilioRestException as retry_err:
                        failed.append({"sid": pn_sid, "error": str(retry_err)})
                        logger.warning(f"[VoiceIntegrity] Retry failed for {pn_sid}: {retry_err}")
                else:
                    # Can't determine conflict — treat 20409 as success, others as failure
                    if e.code == 20409:
                        assigned += 1
                        logger.info(f"[VoiceIntegrity] {pn_sid} already assigned (20409)")
                    else:
                        failed.append({"sid": pn_sid, "error": str(e)})
                        logger.warning(f"[VoiceIntegrity] Failed to assign {pn_sid}: {e}")
            else:
                failed.append({"sid": pn_sid, "error": str(e)})
                logger.warning(f"[VoiceIntegrity] Failed to assign {pn_sid}: {e}")

    return {"assigned": assigned, "failed": failed, "total": len(phone_number_sids)}


def submit_voice_integrity_for_review(
    sub_account_sid: str,
    trust_product_sid: str,
    sub_account_auth_token: str = "",
) -> dict:
    """
    Submit the Voice Integrity Trust Product for Twilio review.
    Runs an evaluation first to validate all required entities are attached,
    then sets status to pending-review.
    After approval, numbers are registered with carrier analytics (24–48h).
    """
    sub_account_auth_token = _ensure_sub_account_auth_token(
        sub_account_sid, sub_account_auth_token
    )
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    try:
        # ── Check linked Customer Profile status first ──
        # Per Twilio ISV docs, the Secondary Customer Profile must be approved
        # (or at least submitted) before the Trust Product can transition.
        try:
            assignments = client.trusthub.v1.trust_products(trust_product_sid) \
                .trust_products_entity_assignments.list(limit=20)
            for a in assignments:
                obj_sid = getattr(a, "object_sid", "")
                if obj_sid.startswith("BU"):
                    # This is the linked Customer Profile — check its status
                    try:
                        profile = client.trusthub.v1.customer_profiles(obj_sid).fetch()
                        profile_status = getattr(profile, "status", "unknown")
                        logger.info(
                            f"[VoiceIntegrity] Linked profile {obj_sid} status: {profile_status}"
                        )
                        if profile_status == "draft":
                            # Profile was never submitted — submit it now
                            logger.warning(
                                f"[VoiceIntegrity] Profile {obj_sid} still in draft, "
                                "submitting for review first"
                            )
                            profile_resp = _trusthub_update_status(
                                "CustomerProfiles", obj_sid, "pending-review",
                                sub_account_sid, sub_account_auth_token,
                            )
                            logger.info(
                                f"[VoiceIntegrity] Profile {obj_sid} submitted → "
                                f"{profile_resp.get('status', 'unknown')}"
                            )
                    except TwilioRestException as profile_err:
                        logger.warning(
                            f"[VoiceIntegrity] Could not check/submit profile {obj_sid}: "
                            f"{profile_err}"
                        )
        except Exception as assign_err:
            logger.warning(f"[VoiceIntegrity] Could not check profile status: {assign_err}")

        # ── Run evaluation to validate completeness before submitting ──
        eval_compliant = False
        try:
            evaluation = client.trusthub.v1.trust_products(trust_product_sid) \
                .trust_products_evaluations.create(policy_sid=VOICE_INTEGRITY_POLICY_SID)
            eval_status = getattr(evaluation, "status", "unknown")
            eval_results = getattr(evaluation, "results", None) or []
            logger.info(
                f"[VoiceIntegrity] Evaluation for {trust_product_sid}: "
                f"status={eval_status}, results={eval_results}"
            )
            if eval_status == "compliant":
                eval_compliant = True
            elif eval_status == "noncompliant":
                # Surface evaluation failures instead of proceeding to a guaranteed rejection
                error_details = []
                for r in eval_results:
                    if isinstance(r, dict) and r.get("status") == "noncompliant":
                        error_details.append(r.get("friendly_name", r.get("requirement_key", "unknown")))
                detail_msg = ", ".join(error_details) if error_details else "evaluation returned noncompliant"
                raise TwilioRestException(
                    status=400, uri="", msg=f"Voice Integrity evaluation failed: {detail_msg}")
            else:
                logger.warning(
                    f"[VoiceIntegrity] Unexpected evaluation status '{eval_status}' — "
                    "attempting submission anyway"
                )
        except TwilioRestException:
            raise  # re-raise evaluation noncompliant errors
        except Exception as eval_err:
            # Non-Twilio errors during evaluation — log but allow submission attempt
            logger.warning(f"[VoiceIntegrity] Evaluation check failed (proceeding): {eval_err}")

        # ── Submit for review ──
        # Use direct HTTP POST instead of SDK .update() — the twilio-python SDK
        # has casing issues with TrustHub status enums that silently drop the
        # Status parameter, leaving the Trust Product in 'draft'.
        resp_data = _trusthub_update_status(
            "TrustProducts", trust_product_sid, "pending-review",
            sub_account_sid, sub_account_auth_token,
        )
        tp_status = resp_data.get("status", "unknown")
        tp_sid = resp_data.get("sid", trust_product_sid)
        logger.info(f"[VoiceIntegrity] Submitted {trust_product_sid} for review → {tp_status}")

        # ── Verify the status actually changed ──
        if tp_status == "draft":
            logger.error(
                f"[VoiceIntegrity] Trust Product {trust_product_sid} STILL in draft after "
                f"POST Status=pending-review. Evaluation was: "
                f"{'compliant' if eval_compliant else 'not confirmed compliant'}. "
                f"Full response: {resp_data}"
            )
            raise TwilioRestException(
                status=400, uri="",
                msg="Voice Integrity submission failed: Trust Product remained in 'draft' status "
                    "after API submission. Please check the Twilio Console for details or "
                    "contact support."
            )

        return {"trust_product_sid": tp_sid, "status": tp_status}
    except TwilioRestException as e:
        logger.error(f"[VoiceIntegrity] Submit for review failed: {e}")
        raise


def get_voice_integrity_status(
    sub_account_sid: str,
    trust_product_sid: str,
    sub_account_auth_token: str = "",
) -> dict:
    """
    Check the current status of a Voice Integrity Trust Product.
    Statuses: draft, pending-review, in-review, twilio-approved, twilio-rejected.
    When rejected, attempts to fetch the evaluation failure reasons.
    """
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    try:
        tp = client.trusthub.v1.trust_products(trust_product_sid).fetch()
        # List assigned numbers
        assigned_numbers = []
        try:
            assignments = client.trusthub.v1.trust_products(trust_product_sid) \
                .trust_products_channel_endpoint_assignment.list(limit=100)
            assigned_numbers = [a.channel_endpoint_sid for a in assignments]
        except Exception as e:
            logger.warning(f"[VoiceIntegrity] Could not list assigned numbers: {e}")

        result = {
            "trust_product_sid": tp.sid,
            "status": tp.status,
            "friendly_name": getattr(tp, "friendly_name", ""),
            "date_created": tp.date_created.isoformat() if tp.date_created else "",
            "date_updated": tp.date_updated.isoformat() if tp.date_updated else "",
            "assigned_numbers": assigned_numbers,
            "assigned_count": len(assigned_numbers),
        }

        # If rejected, try to fetch evaluation failure reasons
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
                logger.warning(f"[VoiceIntegrity] Could not fetch rejection reasons: {eval_err}")

        return result
    except TwilioRestException as e:
        logger.error(f"[VoiceIntegrity] Status check failed: {e}")
        raise


def update_voice_integrity_end_user(
    sub_account_sid: str,
    end_user_sid: str,
    sub_account_auth_token: str = "",
    business_employee_count: str = "",
    average_call_volume: str = "",
) -> dict:
    """
    Update the EndUser attributes on a Voice Integrity Trust Product.
    Useful for correcting data after a rejection.
    """
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    try:
        attrs = {}
        if business_employee_count:
            attrs["business_employee_count"] = str(business_employee_count)
        if average_call_volume:
            attrs["average_business_day_call_volume"] = str(average_call_volume)

        if not attrs:
            return {"status": "ok", "message": "No attributes to update"}

        # Fetch current attributes and merge
        end_user = client.trusthub.v1.end_users(end_user_sid).fetch()
        current_attrs = getattr(end_user, "attributes", {}) or {}
        current_attrs.update(attrs)

        updated = client.trusthub.v1.end_users(end_user_sid).update(
            attributes=current_attrs,
        )
        logger.info(f"[VoiceIntegrity] Updated EndUser {end_user_sid}: {attrs}")
        return {
            "status": "ok",
            "end_user_sid": updated.sid,
            "attributes": getattr(updated, "attributes", {}),
        }
    except TwilioRestException as e:
        logger.error(f"[VoiceIntegrity] EndUser update failed: {e}")
        raise


def resubmit_voice_integrity(
    sub_account_sid: str,
    trust_product_sid: str,
    sub_account_auth_token: str = "",
    business_name: str = "",
    contact_email: str = "",
    existing_profile_sid: str = "",
    business_employee_count: str = "1",
    average_call_volume: str = "500",
) -> dict:
    """
    Resubmit a rejected Voice Integrity registration.

    Twilio does NOT allow resetting a twilio-rejected Trust Product back to draft.
    The correct approach is to create a brand new Trust Product, re-link all the
    existing entities (Secondary Profile, EndUser, numbers), and submit fresh.

    The old rejected Trust Product is abandoned (Twilio doesn't allow deletion).
    """
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)

    # ── Step 1: Gather existing entities from the old Trust Product ──
    old_assigned_numbers = []
    old_entity_sids = []
    try:
        # Get assigned phone numbers
        assignments = client.trusthub.v1.trust_products(trust_product_sid) \
            .trust_products_channel_endpoint_assignment.list(limit=100)
        old_assigned_numbers = [a.channel_endpoint_sid for a in assignments]
        logger.info(f"[VoiceIntegrity] Old product has {len(old_assigned_numbers)} numbers")

        # Get entity assignments (profile, end_user)
        entity_assignments = client.trusthub.v1.trust_products(trust_product_sid) \
            .trust_products_entity_assignments.list(limit=100)
        old_entity_sids = [ea.object_sid for ea in entity_assignments]
        logger.info(f"[VoiceIntegrity] Old product has {len(old_entity_sids)} entity assignments")
    except Exception as e:
        logger.warning(f"[VoiceIntegrity] Could not read old product entities: {e}")

    # ── Step 2: Create a new Trust Product ──
    try:
        new_tp = client.trusthub.v1.trust_products.create(
            friendly_name=f"Voice Integrity: {business_name or sub_account_sid}",
            policy_sid=VOICE_INTEGRITY_POLICY_SID,
            email=contact_email or "support@omnisconn.com",
        )
        new_tp_sid = new_tp.sid
        logger.info(f"[VoiceIntegrity] Created new Trust Product: {new_tp_sid} (replacing {trust_product_sid})")
    except TwilioRestException as e:
        logger.error(f"[VoiceIntegrity] New Trust Product creation failed: {e}")
        raise

    # ── Step 3: Re-link existing entities to the new Trust Product ──
    for entity_sid in old_entity_sids:
        try:
            client.trusthub.v1.trust_products(new_tp_sid) \
                .trust_products_entity_assignments.create(object_sid=entity_sid)
            logger.info(f"[VoiceIntegrity] Re-linked entity {entity_sid} → {new_tp_sid}")
        except TwilioRestException as e:
            if e.code == 20409:
                logger.info(f"[VoiceIntegrity] Entity {entity_sid} already linked (20409)")
            else:
                logger.warning(f"[VoiceIntegrity] Entity re-link failed for {entity_sid}: {e}")

    # ── Step 4: Re-assign phone numbers ──
    for pn_sid in old_assigned_numbers:
        try:
            client.trusthub.v1.trust_products(new_tp_sid) \
                .trust_products_channel_endpoint_assignment.create(
                    channel_endpoint_type="phone-number",
                    channel_endpoint_sid=pn_sid,
                )
            logger.info(f"[VoiceIntegrity] Re-assigned number {pn_sid} → {new_tp_sid}")
        except TwilioRestException as e:
            if e.code == 20409:
                logger.info(f"[VoiceIntegrity] Number {pn_sid} already assigned (20409)")
            else:
                logger.warning(f"[VoiceIntegrity] Number re-assign failed for {pn_sid}: {e}")

    # ── Step 5: Check linked profile status ──
    for entity_sid in old_entity_sids:
        if entity_sid.startswith("BU"):
            try:
                profile = client.trusthub.v1.customer_profiles(entity_sid).fetch()
                profile_status = getattr(profile, "status", "unknown")
                logger.info(f"[VoiceIntegrity] Linked profile {entity_sid} status: {profile_status}")
                if profile_status == "draft":
                    logger.warning(f"[VoiceIntegrity] Profile {entity_sid} in draft, submitting first")
                    _trusthub_update_status(
                        "CustomerProfiles", entity_sid, "pending-review",
                        sub_account_sid, sub_account_auth_token,
                    )
            except Exception as pe:
                logger.warning(f"[VoiceIntegrity] Profile check/submit failed for {entity_sid}: {pe}")

    # ── Step 6: Evaluate and submit ──
    try:
        evaluation = client.trusthub.v1.trust_products(new_tp_sid) \
            .trust_products_evaluations.create(policy_sid=VOICE_INTEGRITY_POLICY_SID)
        eval_status = getattr(evaluation, "status", "unknown")
        eval_results = getattr(evaluation, "results", None) or []
        logger.info(
            f"[VoiceIntegrity] New product evaluation: status={eval_status}, results={eval_results}"
        )

        if eval_status == "noncompliant":
            error_details = []
            for r in eval_results:
                if isinstance(r, dict) and r.get("status") == "noncompliant":
                    error_details.append(r.get("friendly_name", r.get("requirement_key", "unknown")))
            detail_msg = ", ".join(error_details) if error_details else "evaluation returned noncompliant"
            raise TwilioRestException(
                status=400, uri="",
                msg=f"Voice Integrity evaluation failed on new product: {detail_msg}. "
                    f"New product SID: {new_tp_sid}")
    except TwilioRestException:
        raise
    except Exception as eval_err:
        logger.warning(f"[VoiceIntegrity] Evaluation check failed (proceeding): {eval_err}")

    # Direct HTTP POST to bypass SDK enum/serialization issues with Status
    resp_data = _trusthub_update_status(
        "TrustProducts", new_tp_sid, "pending-review",
        sub_account_sid, sub_account_auth_token,
    )
    tp_status = resp_data.get("status", "unknown")
    logger.info(f"[VoiceIntegrity] Submitted new product {new_tp_sid} for review → {tp_status}")

    # Verify status actually changed
    if tp_status == "draft":
        logger.error(
            f"[VoiceIntegrity] New Trust Product {new_tp_sid} STILL in draft after "
            f"POST Status=pending-review. Full response: {resp_data}"
        )
        raise TwilioRestException(
            status=400, uri="",
            msg="Voice Integrity submission failed: Trust Product remained in 'draft' status. "
                "Please check the Twilio Console for details or contact support."
        )

    return {
        "trust_product_sid": new_tp_sid,
        "old_trust_product_sid": trust_product_sid,
        "status": tp_status,
        "assigned_numbers": old_assigned_numbers,
    }


def remove_number_from_voice_integrity(
    sub_account_sid: str,
    trust_product_sid: str,
    phone_number_sid: str,
    sub_account_auth_token: str = "",
) -> bool:
    """Remove a phone number from a Voice Integrity Trust Product."""
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    try:
        assignments = client.trusthub.v1.trust_products(trust_product_sid) \
            .trust_products_channel_endpoint_assignment.list(limit=100)
        for a in assignments:
            if a.channel_endpoint_sid == phone_number_sid:
                client.trusthub.v1.trust_products(trust_product_sid) \
                    .trust_products_channel_endpoint_assignment(a.sid).delete()
                logger.info(f"[VoiceIntegrity] Removed {phone_number_sid} from {trust_product_sid}")
                return True
        logger.warning(f"[VoiceIntegrity] {phone_number_sid} not found on {trust_product_sid}")
        return False
    except TwilioRestException as e:
        logger.error(f"[VoiceIntegrity] Remove number failed: {e}")
        raise


def discover_voice_integrity_products(
    sub_account_sid: str,
    sub_account_auth_token: str = "",
) -> list:
    """
    Discover existing Voice Integrity Trust Products on a sub-account.
    Returns list of {trust_product_sid, status, friendly_name} dicts.
    """
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    try:
        products = client.trusthub.v1.trust_products.list(limit=100)
        results = []
        for p in products:
            fn = getattr(p, "friendly_name", "") or ""
            if "voice integrity" in fn.lower():
                results.append({
                    "trust_product_sid": p.sid,
                    "status": p.status,
                    "friendly_name": fn,
                    "date_created": p.date_created.isoformat() if p.date_created else "",
                })
        logger.info(f"[VoiceIntegrity] Discovered {len(results)} Voice Integrity products")
        return results
    except TwilioRestException as e:
        logger.error(f"[VoiceIntegrity] Discovery failed: {e}")
        return []




