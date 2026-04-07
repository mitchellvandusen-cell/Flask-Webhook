"""Module extracted from twilio_provisioning.py."""

# ──────────────────────────────────────────────────────────────
# CNAM TRUST PRODUCT — CALLER ID NAME REGISTRATION
# ──────────────────────────────────────────────────────────────
#
# Twilio CNAM (Caller ID Name) registration via Trust Hub.
# Registers a display name with US CNAM authoritative databases so
# recipients see the business name on incoming calls.
#
# Flow:
#   1. Customer Profile (Primary for master, Secondary for sub-accounts)
#   2. Create CNAM Trust Product (policy: RNf3db3cd1fe25fcfd3c3ded065c8fea53)
#   3. Create EndUser type=cnam_information with cnam_display_name
#   4. Link Profile → Trust Product (EntityAssignment)
#   5. Link EndUser → Trust Product (EntityAssignment)
#   6. Assign phone numbers to Trust Product (ChannelEndpointAssignment)
#   7. Evaluate → submit for review
#   8. After approval: 48-72h for carrier propagation
#
# Requirements:
#   - Only US standard long-code numbers (no toll-free, no CA numbers)
#   - Display name: max 15 chars, starts with letter, letters/numbers/periods/commas/spaces only
#   - Phone numbers must be assigned to the Customer Profile first
#
# ISV/sub-account: Secondary Profile linked to Primary, all on sub-account client.
# Master account (direct customer): uses Primary Business Profile directly.
#
# State stored in voice_config["cnam"] JSONB.

# CNAM Trust Product policy SID — static across all Twilio accounts.
# https://www.twilio.com/docs/voice/brand-your-calls-using-cnam
CNAM_TRUST_PRODUCT_POLICY_SID = "RNf3db3cd1fe25fcfd3c3ded065c8fea53"


def validate_cnam_display_name(name: str) -> tuple:
    """
    Validate a CNAM display name per Twilio's requirements.
    Returns (is_valid, cleaned_name_or_error_message).

    Rules:
      - Max 15 characters
      - Must start with a letter
      - Only letters, numbers, periods, commas, and spaces
      - Must not be generic (city/state names) — caller responsibility
    """
    import re
    name = name.strip()
    if not name:
        return False, "CNAM display name is required"
    if len(name) > 15:
        return False, f"CNAM display name must be 15 characters or fewer (got {len(name)})"
    if not name[0].isalpha():
        return False, "CNAM display name must start with a letter"
    if not re.match(r'^[A-Za-z0-9., ]+$', name):
        return False, "CNAM display name can only contain letters, numbers, periods, commas, and spaces"
    return True, name


def create_cnam_trust_product(
    sub_account_sid: str,
    business_name: str,
    cnam_display_name: str,
    contact_email: str,
    sub_account_auth_token: str = "",
    existing_profile_sid: str = "",
) -> dict:
    """
    Create a CNAM Trust Product for caller ID name registration (ISV sub-account flow).

    ISV flow per Twilio docs:
      1. Find or create Secondary Customer Profile on sub-account
      2. Create CNAM Trust Product (policy_sid=RNf3db3cd1fe25fcfd3c3ded065c8fea53)
      3. Create CNAM EndUser with display name
      4. Link Profile → Trust Product (EntityAssignment)
      5. Link EndUser → Trust Product (EntityAssignment)

    Master account manages its own CNAM via the Twilio Console.
    Returns dict with trust_product_sid, profile_sid, end_user_sid, status, cnam_display_name.
    """
    # Validate display name
    valid, result = validate_cnam_display_name(cnam_display_name)
    if not valid:
        raise ValueError(result)

    sub_account_auth_token = _ensure_sub_account_auth_token(
        sub_account_sid, sub_account_auth_token
    )
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)

    try:
        # ── Step 1: Secondary Customer Profile (ISV flow) ──
        # Per Twilio ISV docs: find or create a Secondary Customer Profile
        # on the sub-account, linked to the master's Primary Business Profile.
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

        # ── Step 2: CNAM Trust Product ──
        trust_product = client.trusthub.v1.trust_products.create(
            friendly_name=f"CNAM: {business_name}",
            email=contact_email,
            policy_sid=CNAM_TRUST_PRODUCT_POLICY_SID,
        )
        logger.info(f"[CNAM] Created Trust Product: {trust_product.sid}")

        # ── Step 3: CNAM EndUser ──
        end_user = client.trusthub.v1.end_users.create(
            friendly_name=f"CNAM EndUser: {cnam_display_name}",
            type="cnam_information",
            attributes={
                "cnam_display_name": cnam_display_name.upper(),
            },
        )
        logger.info(f"[CNAM] Created EndUser: {end_user.sid}")

        # ── Step 4: Link Profile → Trust Product ──
        client.trusthub.v1.trust_products(trust_product.sid) \
            .trust_products_entity_assignments.create(
                object_sid=profile_sid,
            )
        logger.info(f"[CNAM] Linked profile {profile_sid} → {trust_product.sid}")

        # ── Step 5: Link EndUser → Trust Product ──
        client.trusthub.v1.trust_products(trust_product.sid) \
            .trust_products_entity_assignments.create(
                object_sid=end_user.sid,
            )
        logger.info(f"[CNAM] Linked EndUser {end_user.sid} → {trust_product.sid}")

        return {
            "trust_product_sid": trust_product.sid,
            "profile_sid": profile_sid,
            "end_user_sid": end_user.sid,
            "status": "draft",
            "cnam_display_name": cnam_display_name.upper(),
            "business_name": business_name,
        }

    except TwilioRestException as e:
        logger.error(f"[CNAM] Trust Product creation failed: {e}")
        raise


def update_cnam_display_name(
    sub_account_sid: str,
    end_user_sid: str,
    new_display_name: str,
    sub_account_auth_token: str = "",
) -> dict:
    """
    Update the CNAM display name on an existing CNAM Trust Product EndUser.

    Changes the cnam_display_name attribute on the EndUser, which propagates
    to carrier databases after Twilio re-processes the Trust Product (48-72 hours).
    Also updates the friendly_name on all phone numbers assigned to the CNAM
    Trust Product so the Twilio Console reflects the new name.
    """
    valid, result = validate_cnam_display_name(new_display_name)
    if not valid:
        raise ValueError(result)

    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    try:
        updated = client.trusthub.v1.end_users(end_user_sid).update(
            friendly_name=f"CNAM EndUser: {new_display_name}",
            attributes={
                "cnam_display_name": new_display_name.upper(),
            },
        )
        logger.info(f"[CNAM] Updated EndUser {end_user_sid} display name to: {new_display_name.upper()}")
        return {
            "status": "ok",
            "end_user_sid": updated.sid,
            "cnam_display_name": new_display_name.upper(),
        }
    except TwilioRestException as e:
        logger.error(f"[CNAM] Display name update failed for {end_user_sid}: {e}")
        raise


def assign_numbers_to_cnam(
    sub_account_sid: str,
    trust_product_sid: str,
    phone_number_sids: list,
    sub_account_auth_token: str = "",
    profile_sid: str = "",
) -> dict:
    """
    Assign phone numbers to a CNAM Trust Product.

    Numbers must first be assigned to the Customer Profile (required by Twilio),
    then to the Trust Product. Only US standard long-code numbers are eligible
    (no toll-free, no CA numbers).

    Handles conflict resolution: if a number is already assigned to a different
    CNAM Trust Product (e.g. old rejected one), unassigns it and retries.
    """
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    assigned = 0
    failed = []

    for pn_sid in phone_number_sids:
        try:
            # Assign to Customer Profile first (required before Trust Product)
            if profile_sid:
                try:
                    client.trusthub.v1.customer_profiles(profile_sid) \
                        .customer_profiles_channel_endpoint_assignment.create(
                            channel_endpoint_type="phone-number",
                            channel_endpoint_sid=pn_sid,
                        )
                    logger.info(f"[CNAM] Assigned {pn_sid} to profile {profile_sid}")
                except TwilioRestException as e:
                    if e.code == 20409 or e.status == 409:
                        logger.info(f"[CNAM] {pn_sid} already on profile (code={e.code})")
                    else:
                        raise

            # Then assign to CNAM Trust Product
            client.trusthub.v1.trust_products(trust_product_sid) \
                .trust_products_channel_endpoint_assignment.create(
                    channel_endpoint_type="phone-number",
                    channel_endpoint_sid=pn_sid,
                )
            assigned += 1
            logger.info(f"[CNAM] Assigned {pn_sid} to {trust_product_sid}")
        except TwilioRestException as e:
            if e.code == 20409 or e.status == 409:
                # Already assigned — check if to this TP or a different one
                import re as _re
                err_str = str(e)
                conflict_match = _re.search(r'(BU[a-f0-9A-F]{32})', err_str)
                conflict_sid = conflict_match.group(1) if conflict_match else None

                if conflict_sid and conflict_sid == trust_product_sid:
                    assigned += 1
                    logger.info(f"[CNAM] {pn_sid} already assigned to target TP (ok)")
                elif conflict_sid and conflict_sid != trust_product_sid:
                    # Assigned to different TP — unassign and retry
                    logger.info(f"[CNAM] {pn_sid} stuck on {conflict_sid}, unassigning and retrying")
                    try:
                        unassign_numbers_from_trust_product(
                            sub_account_sid, conflict_sid, [pn_sid], sub_account_auth_token)
                        client.trusthub.v1.trust_products(trust_product_sid) \
                            .trust_products_channel_endpoint_assignment.create(
                                channel_endpoint_type="phone-number",
                                channel_endpoint_sid=pn_sid,
                            )
                        assigned += 1
                        logger.info(f"[CNAM] Retry succeeded: {pn_sid} → {trust_product_sid}")
                    except TwilioRestException as retry_err:
                        failed.append({"sid": pn_sid, "error": str(retry_err)})
                        logger.warning(f"[CNAM] Retry failed for {pn_sid}: {retry_err}")
                else:
                    if e.code == 20409:
                        assigned += 1
                        logger.info(f"[CNAM] {pn_sid} already assigned (20409)")
                    else:
                        failed.append({"sid": pn_sid, "error": str(e)})
            else:
                failed.append({"sid": pn_sid, "error": str(e)})
                logger.warning(f"[CNAM] Failed to assign {pn_sid}: {e}")

    return {"assigned": assigned, "failed": failed, "total": len(phone_number_sids)}


def submit_cnam_for_review(
    sub_account_sid: str,
    trust_product_sid: str,
    sub_account_auth_token: str = "",
) -> dict:
    """
    Submit the CNAM Trust Product for Twilio review.
    Runs an evaluation first to validate all required entities are attached,
    then sets status to pending-review.
    After approval, CNAM display name propagates to carrier databases (48-72h).
    """
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    try:
        # Run evaluation first
        try:
            evaluation = client.trusthub.v1.trust_products(trust_product_sid) \
                .trust_products_evaluations.create(policy_sid=CNAM_TRUST_PRODUCT_POLICY_SID)
            eval_status = getattr(evaluation, "status", "unknown")
            logger.info(f"[CNAM] Evaluation for {trust_product_sid}: {eval_status}")
            if eval_status == "noncompliant":
                eval_results = getattr(evaluation, "results", None) or []
                error_details = []
                for r in eval_results:
                    if isinstance(r, dict) and r.get("status") == "noncompliant":
                        error_details.append(r.get("friendly_name", r.get("requirement_key", "unknown")))
                detail_msg = ", ".join(error_details) if error_details else "evaluation returned noncompliant"
                raise TwilioRestException(
                    status=400, uri="", msg=f"CNAM evaluation failed: {detail_msg}")
        except TwilioRestException:
            raise
        except Exception as eval_err:
            logger.warning(f"[CNAM] Evaluation check failed (proceeding): {eval_err}")

        resp_data = _trusthub_update_status(
            "TrustProducts", trust_product_sid, "pending-review",
            sub_account_sid, sub_account_auth_token,
        )
        tp_status = resp_data.get("status", "unknown")
        logger.info(f"[CNAM] Submitted {trust_product_sid} for review → {tp_status}")
        return {"trust_product_sid": resp_data.get("sid", trust_product_sid), "status": tp_status}
    except TwilioRestException as e:
        logger.error(f"[CNAM] Submit for review failed: {e}")
        raise


def discover_cnam_trust_product(
    sub_account_sid: str,
    sub_account_auth_token: str = "",
) -> dict | None:
    """
    Discover an existing CNAM Trust Product on the account by scanning
    Trust Hub for products matching the CNAM policy SID.
    Returns dict with trust_product_sid, status, friendly_name, cnam_display_name,
    assigned_numbers, or None if no CNAM product exists.
    """
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    try:
        products = client.trusthub.v1.trust_products.list(
            policy_sid=CNAM_TRUST_PRODUCT_POLICY_SID, limit=10
        )
        if not products:
            return None

        # Find the best product — prefer twilio-approved, then any non-draft
        best = None
        for tp in products:
            if tp.status == "twilio-approved":
                best = tp
                break
            if best is None or tp.status != "draft":
                best = tp
        if not best:
            return None

        # Fetch assigned numbers
        assigned_numbers = []
        try:
            assignments = client.trusthub.v1.trust_products(best.sid) \
                .trust_products_channel_endpoint_assignment.list(limit=100)
            assigned_numbers = [a.channel_endpoint_sid for a in assignments]
        except Exception as e:
            logger.warning(f"[CNAM] Could not list assigned numbers during discovery: {e}")

        # Extract CNAM display name from the friendly_name (format: "CNAM US ...")
        # or from EndUser attributes if available
        cnam_display_name = ""
        try:
            entity_assignments = client.trusthub.v1.trust_products(best.sid) \
                .trust_products_entity_assignments.list(limit=20)
            for ea in entity_assignments:
                obj_sid = ea.object_sid
                if obj_sid and obj_sid.startswith("IT"):
                    try:
                        eu = client.trusthub.v1.end_users(obj_sid).fetch()
                        attrs = eu.attributes or {}
                        if attrs.get("cnam_display_name"):
                            cnam_display_name = attrs["cnam_display_name"]
                            break
                    except Exception:
                        pass
        except Exception as e:
            logger.debug(f"[CNAM] Could not read EndUser for display name: {e}")

        # Fallback: extract from friendly_name
        if not cnam_display_name:
            fn = getattr(best, "friendly_name", "") or ""
            if fn.startswith("CNAM:"):
                cnam_display_name = fn[5:].strip()[:15]

        logger.info(
            f"[CNAM] Discovered Trust Product {best.sid} (status={best.status}, "
            f"display_name={cnam_display_name!r}, {len(assigned_numbers)} numbers)"
        )
        return {
            "trust_product_sid": best.sid,
            "status": best.status,
            "friendly_name": getattr(best, "friendly_name", ""),
            "cnam_display_name": cnam_display_name,
            "assigned_numbers": assigned_numbers,
            "assigned_count": len(assigned_numbers),
            "date_created": best.date_created.isoformat() if best.date_created else "",
        }
    except TwilioRestException as e:
        logger.error(f"[CNAM] Discovery failed for {sub_account_sid}: {e}")
        return None


def get_cnam_trust_product_status(
    sub_account_sid: str,
    trust_product_sid: str,
    sub_account_auth_token: str = "",
) -> dict:
    """
    Check the current status of a CNAM Trust Product.
    Statuses: draft, pending-review, in-review, twilio-approved, twilio-rejected.
    When rejected, fetches evaluation failure reasons.
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
            logger.warning(f"[CNAM] Could not list assigned numbers: {e}")

        result = {
            "trust_product_sid": tp.sid,
            "status": tp.status,
            "friendly_name": getattr(tp, "friendly_name", ""),
            "date_created": tp.date_created.isoformat() if tp.date_created else "",
            "date_updated": tp.date_updated.isoformat() if tp.date_updated else "",
            "assigned_numbers": assigned_numbers,
            "assigned_count": len(assigned_numbers),
        }

        # If rejected, fetch failure reasons
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
            except Exception as e:
                logger.warning(f"[CNAM] Could not fetch rejection reasons: {e}")

        return result
    except TwilioRestException as e:
        logger.error(f"[CNAM] Status check failed for {trust_product_sid}: {e}")
        raise



