"""Module extracted from twilio_provisioning.py."""

import logging
import io
from twilio.base.exceptions import TwilioRestException

from ..client import (
    get_sub_account_client_native,
    _ensure_sub_account_auth_token,
    _trusthub_update_status,
)
from .base import (
    SECONDARY_CUSTOMER_PROFILE_POLICY_SID,
    _find_primary_profile_sid,
    _find_or_create_secondary_profile,
)

logger = logging.getLogger("twilio_provisioning")


def register_business_profile(sub_account_sid: str, business_name: str,
                                ein: str, street: str, city: str,
                                state: str, zip_code: str,
                                contact_name: str, contact_email: str,
                                contact_phone: str,
                                sub_account_auth_token: str = "",
                                business_type: str = "",
                                website: str = "",
                                contact_title: str = "",
                                existing_profile_sid: str = "",
                                ein_document_data: str = "",
                                ein_document_type: str = "",
                                ein_document_name: str = "") -> dict:
    """
    Register a business profile for CNAM / spam protection on a Twilio sub-account.

    ISV/Subaccounts flow:
      1. Find or create a Secondary Customer Profile (correct ISV policy)
      2. Create EndUser (customer_profile_business_information) with biz details
      3. Create Authorized Representative EndUser
      4. Create Address on the sub-account
      5. Assign all entities to the Secondary Profile (EntityAssignments)
      6. Link Secondary Profile to Primary Business Profile on master account
      7. Assign phone numbers to the profile
      8. Run evaluation and submit for review
      9. Set friendly_name on all numbers (internal label only — real CNAM
         registration requires a separate CNAM Trust Product via create_cnam_trust_product())

    The Secondary Profile is reusable across A2P, Voice Integrity, SHAKEN/STIR, CNAM.
    """
    results = {"steps": [], "errors": []}
    profile_sid = ""

    # All Trust Hub operations are ISV-only (sub-accounts).
    # Master account manages its own profiles via the Twilio Console.
    try:
        sub_account_auth_token = _ensure_sub_account_auth_token(
            sub_account_sid, sub_account_auth_token
        )
    except ValueError as e:
        logger.error(f"[SpamProtection] {e}")
        results["errors"].append(str(e))
        return results

    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)

    # ── Step 1: Secondary Customer Profile (ISV flow) ──
    # Per Twilio ISV docs: create a Secondary Customer Profile on the sub-account,
    # linked to the master's Primary Business Profile.
    # https://www.twilio.com/docs/trust-hub/trusthub-rest-api/api-create-secondary-customer-profile
    try:
        primary_profile_sid = _find_primary_profile_sid()
        profile_sid = _find_or_create_secondary_profile(
            client=client,
            sub_account_sid=sub_account_sid,
            business_name=business_name,
            contact_email=contact_email,
            existing_profile_sid=existing_profile_sid,
            primary_profile_sid=primary_profile_sid,
            sub_account_auth_token=sub_account_auth_token,
        )
        results["steps"].append({
            "name": "secondary_profile",
            "status": "ok",
            "sid": profile_sid,
        })
        logger.info(f"[SpamProtection] Secondary Profile: {profile_sid}")
    except Exception as e:
        logger.error(f"[SpamProtection] Customer Profile failed: {e}")
        results["errors"].append(f"Customer Profile: {e}")
        # Can't proceed without a profile — still do CNAM at minimum

    # ── Step 2: Create EndUser (business info) ──
    end_user_sid = ""
    state_upper = (state or "").strip().upper()
    if profile_sid:
        try:
            # Normalize state to 2-letter abbreviation
            if len(state_upper) > 2:
                state_upper = US_STATE_ABBREVS.get(state_upper.lower(), state_upper[:2])

            # Map UI business_type to Twilio's expected enum values.
            # Twilio requires EXACT strings — "Limited Liability Corporation"
            # (NOT "Limited Liability Company" which is the common legal term).
            _BIZ_TYPE_MAP = {
                "Corporation": "Corporation",
                "Limited Liability Company": "Limited Liability Corporation",
                "LLC": "Limited Liability Corporation",
                "Partnership": "Partnership",
                "Sole Proprietorship": "Sole Proprietorship",
                "Non-profit Corporation": "Non-profit Corporation",
                "Non-Profit": "Non-profit Corporation",
                "Co-operative": "Co-operative",
            }
            resolved_biz_type = _BIZ_TYPE_MAP.get(
                business_type, business_type or "Corporation"
            )

            # Registration identifier: use EIN when the user provided one,
            # SSN only for sole props who explicitly don't have an EIN.
            # Our onboarding wizard requires an EIN for ALL users, so this
            # should always be "EIN" in practice.  The old code forced "SSN"
            # for every sole prop, which caused evaluation to fail when the
            # number was actually an EIN (wrong identifier → noncompliant).
            is_sole_prop = resolved_biz_type == "Sole Proprietorship"
            ein_looks_valid = bool(ein and len(ein.replace("-", "")) == 9)
            reg_identifier = "EIN" if ein_looks_valid else ("SSN" if is_sole_prop else "EIN")

            # business_identity: sub-account customers are "direct_customer"
            # (the platform/ISV is "isv_reseller_or_partner" — but that's the
            # master account, not the sub-account's customer)
            biz_identity = "direct_customer"

            end_user = client.trusthub.v1.end_users.create(
                friendly_name=f"Business: {business_name}",
                type="customer_profile_business_information",
                attributes={
                    "business_name": business_name,
                    "business_identity": biz_identity,
                    "business_type": resolved_biz_type,
                    "business_industry": "INSURANCE",
                    "business_registration_identifier": reg_identifier,
                    "business_registration_number": ein,
                    "business_regions_of_operation": "USA_AND_CANADA",
                    "website_url": website or "",
                    "social_media_profile_urls": "",
                },
            )
            end_user_sid = end_user.sid
            results["end_user_sid"] = end_user_sid
            results["steps"].append({
                "name": "end_user_business",
                "status": "ok",
                "sid": end_user_sid,
            })
            logger.info(f"[SpamProtection] Created business EndUser: {end_user_sid}")
        except TwilioRestException as e:
            logger.error(f"[SpamProtection] Business EndUser creation failed: {e}")
            results["errors"].append(f"Business EndUser: {e}")

    # ── Step 3: Create Authorized Representative EndUser ──
    auth_rep_sid = ""
    if profile_sid and contact_name:
        try:
            # Split contact name into first/last — last_name is REQUIRED by Twilio
            name_parts = contact_name.strip().split(None, 1)
            first_name = name_parts[0] if name_parts else contact_name
            last_name = name_parts[1] if len(name_parts) > 1 else first_name

            # Use user-provided title, fallback to "Owner"
            resolved_title = contact_title or "Owner"

            # Twilio requires job_position to be one of these exact enum values:
            # Director, GM, VP, CEO, CFO, General Counsel
            # business_title is free-text. Map common titles to valid job_position.
            _JOB_POSITION_MAP = {
                "owner": "CEO", "ceo": "CEO", "president": "CEO",
                "cfo": "CFO", "finance": "CFO",
                "director": "Director", "manager": "Director",
                "vp": "VP", "vice president": "VP",
                "gm": "GM", "general manager": "GM",
                "counsel": "General Counsel", "attorney": "General Counsel",
                "agent": "Director", "broker": "Director",
            }
            job_position = _JOB_POSITION_MAP.get(
                resolved_title.lower().strip(), "CEO"
            )

            # Normalize phone to E.164 format for Twilio
            norm_phone = _normalize_phone_e164(contact_phone)

            auth_rep = client.trusthub.v1.end_users.create(
                friendly_name=f"Auth Rep: {contact_name}",
                type="authorized_representative_1",
                attributes={
                    "first_name": first_name,
                    "last_name": last_name,
                    "email": contact_email,
                    "phone_number": norm_phone,
                    "business_title": resolved_title,
                    "job_position": job_position,
                },
            )
            auth_rep_sid = auth_rep.sid
            results["steps"].append({
                "name": "auth_representative",
                "status": "ok",
                "sid": auth_rep_sid,
            })
            logger.info(f"[SpamProtection] Created Auth Rep: {auth_rep_sid}")
        except TwilioRestException as e:
            logger.error(f"[SpamProtection] Auth Rep creation failed: {e}")
            results["errors"].append(f"Auth Rep: {e}")

    # ── Step 4: Create Address + SupportingDocument ──
    # Twilio requires a TWO-STEP address process:
    #   1. Create an Address resource (AD... SID)
    #   2. Wrap it in a SupportingDocument of type "customer_profile_address"
    #      with attributes.address_sids = the AD SID (RD... SID)
    #   3. Assign the RD SID (not AD SID) to the profile
    # Without the SupportingDocument, evaluation fails with "Address sids list is empty".
    address_sid = ""
    supporting_doc_sid = ""
    if profile_sid and street:
        try:
            address = client.addresses.create(
                friendly_name=f"{business_name} Address",
                customer_name=business_name,
                street=street,
                city=city,
                region=state_upper,
                postal_code=zip_code,
                iso_country="US",
            )
            address_sid = address.sid
            logger.info(f"[SpamProtection] Created Address: {address_sid}")

            # Wrap in SupportingDocument — this is what gets assigned to the profile
            supporting_doc = client.trusthub.v1.supporting_documents.create(
                friendly_name=f"{business_name} Address Document",
                type="customer_profile_address",
                attributes={
                    "address_sids": address_sid,
                },
            )
            supporting_doc_sid = supporting_doc.sid
            results["steps"].append({
                "name": "address",
                "status": "ok",
                "sid": supporting_doc_sid,
                "address_sid": address_sid,
            })
            logger.info(f"[SpamProtection] Created SupportingDocument: {supporting_doc_sid} (wraps {address_sid})")

        except TwilioRestException as e:
            logger.error(f"[SpamProtection] Address/SupportingDocument creation failed: {e}")
            results["errors"].append(f"Address: {e}")

    # ── Step 5: EntityAssignments — link entities to profile ──
    # NOTE: Address is assigned via its SupportingDocument SID (RD...), not the
    # raw Address SID (AD...). This is per Twilio docs.
    if profile_sid:
        for entity_name, entity_sid in [
            ("end_user_business", end_user_sid),
            ("auth_representative", auth_rep_sid),
            ("address_document", supporting_doc_sid),
        ]:
            if not entity_sid:
                continue
            try:
                client.trusthub.v1.customer_profiles(profile_sid) \
                    .customer_profiles_entity_assignments.create(
                        object_sid=entity_sid,
                    )
                logger.info(f"[SpamProtection] Assigned {entity_name} ({entity_sid}) → profile {profile_sid}")
            except TwilioRestException as e:
                if e.code == 20409:
                    logger.info(f"[SpamProtection] {entity_name} already assigned (20409)")
                else:
                    logger.warning(f"[SpamProtection] EntityAssignment for {entity_name} failed: {e}")
                    results["errors"].append(f"Assign {entity_name}: {e}")

    # ── Step 5b: Upload EIN document (if provided) ──
    # Must happen BEFORE evaluation/submission — once a profile is in
    # pending-review, entity assignments may be rejected by Twilio.
    # Sole proprietors with new EINs upload their CP 575 letter.
    # Twilio SDK doesn't support file uploads on Trust Hub supporting docs,
    # so we use the REST API directly with multipart form data.
    ein_doc_sid = ""
    if profile_sid and ein_document_data:
        try:
            ein_doc_sid = _upload_ein_supporting_document(
                sub_account_sid=sub_account_sid,
                sub_account_auth_token=sub_account_auth_token,
                business_name=business_name,
                ein=ein,
                contact_name=contact_name,
                ein_document_data=ein_document_data,
                ein_document_type=ein_document_type,
                ein_document_name=ein_document_name,
            )
            if ein_doc_sid:
                # Assign to profile
                client.trusthub.v1.customer_profiles(profile_sid) \
                    .customer_profiles_entity_assignments.create(
                        object_sid=ein_doc_sid,
                    )
                results["steps"].append({
                    "name": "ein_document",
                    "status": "ok",
                    "sid": ein_doc_sid,
                })
                results["ein_document_sid"] = ein_doc_sid
                logger.info(f"[SpamProtection] EIN document uploaded and assigned: {ein_doc_sid}")
        except TwilioRestException as e:
            if e.code == 20409:
                logger.info(f"[SpamProtection] EIN document already assigned (20409)")
                results["steps"].append({"name": "ein_document", "status": "ok", "sid": ein_doc_sid})
                results["ein_document_sid"] = ein_doc_sid
            else:
                logger.error(f"[SpamProtection] EIN document assignment failed: {e}")
                results["errors"].append(f"EIN document: {e}")
        except Exception as e:
            logger.error(f"[SpamProtection] EIN document upload failed: {e}")
            results["errors"].append(f"EIN document: {e}")

    # ── Step 6: Assign phone numbers to profile ──
    if profile_sid:
        try:
            numbers = client.incoming_phone_numbers.list()
            nums_assigned = 0
            for num in numbers:
                try:
                    client.trusthub.v1.customer_profiles(profile_sid) \
                        .customer_profiles_channel_endpoint_assignment.create(
                            channel_endpoint_type="phone-number",
                            channel_endpoint_sid=num.sid,
                        )
                    nums_assigned += 1
                except TwilioRestException as e:
                    if e.code == 20409:
                        nums_assigned += 1  # already assigned
                    else:
                        logger.warning(f"[SpamProtection] Number assign failed {num.phone_number}: {e}")
            results["steps"].append({
                "name": "assign_numbers",
                "status": "ok",
                "assigned": nums_assigned,
                "total": len(numbers),
            })
        except Exception as e:
            logger.error(f"[SpamProtection] Number assignment failed: {e}")
            results["errors"].append(f"Number assignment: {e}")

    # ── Step 7: Evaluate and submit Secondary Profile for review ──
    # Per Twilio ISV docs: evaluate with Secondary Customer Profile policy,
    # then submit for Twilio review.
    if profile_sid:
        try:
            eval_result = client.trusthub.v1.customer_profiles(profile_sid) \
                .customer_profiles_evaluations.create(
                    policy_sid=SECONDARY_CUSTOMER_PROFILE_POLICY_SID,
                )
            eval_status = getattr(eval_result, "status", "unknown")
            logger.info(f"[SpamProtection] Evaluation: {eval_status}")

            if eval_status == "noncompliant":
                eval_results = getattr(eval_result, "results", None) or []
                noncompliant = [r.get("friendly_name", "unknown")
                                for r in eval_results
                                if isinstance(r, dict) and r.get("status") == "noncompliant"]
                results["steps"].append({
                    "name": "evaluation",
                    "status": "noncompliant",
                    "issues": noncompliant,
                })
                noncompliant_str = ", ".join(noncompliant) if noncompliant else "unknown requirements"
                logger.error(f"[SpamProtection] Evaluation NONCOMPLIANT: {noncompliant_str}")
                results["errors"].append(
                    f"Profile evaluation failed — missing: {noncompliant_str}. "
                    f"Fix these and re-submit."
                )
            else:
                # Submit for review — use direct HTTP to bypass SDK enum issues
                profile_resp = _trusthub_update_status(
                    "CustomerProfiles", profile_sid, "pending-review",
                    sub_account_sid, sub_account_auth_token,
                )
                results["steps"].append({
                    "name": "submit_review",
                    "status": "ok",
                    "profile_status": profile_resp.get("status", "unknown"),
                })
                logger.info(
                    f"[SpamProtection] Submitted profile {profile_sid} for review → "
                    f"{profile_resp.get('status', 'unknown')}"
                )
        except TwilioRestException as e:
            logger.warning(f"[SpamProtection] Evaluation/submit failed: {e}")
            results["errors"].append(f"Submit for review: {e}")

    # ── Step 8: Set friendly_name on all numbers ──
    # NOTE: This sets the Twilio-internal label only. Real CNAM registration
    # with carriers requires a separate CNAM Trust Product (see create_cnam_trust_product).
    # friendly_name does NOT propagate to carrier CNAM databases.
    # ONLY set friendly_name if a profile was actually created — otherwise
    # the label gives a false impression of "protection" in the dashboard.
    if profile_sid:
        try:
            numbers = client.incoming_phone_numbers.list()
            cnam_success = 0
            for num in numbers:
                try:
                    client.incoming_phone_numbers(num.sid).update(
                        friendly_name=business_name[:15],
                    )
                    cnam_success += 1
                except Exception as e:
                    logger.warning(f"CNAM update failed for {num.phone_number}: {e}")

            results["steps"].append({
                "name": "cnam_all_numbers",
                "status": "ok",
                "enabled": cnam_success,
                "total": len(numbers),
            })

        except TwilioRestException as e:
            logger.error(f"CNAM registration failed: {e}")
            results["errors"].append(str(e))
    else:
        logger.warning("[SpamProtection] Skipping friendly_name update — no profile created")
        results["steps"].append({
            "name": "cnam_all_numbers",
            "status": "skipped",
            "reason": "No Customer Profile created — cannot set CNAM labels",
        })

    # Add profile_sid to results for caller to save
    if profile_sid:
        results["profile_sid"] = profile_sid
    if end_user_sid:
        results["end_user_sid"] = end_user_sid

    return results


def _upload_ein_supporting_document(
    sub_account_sid: str,
    sub_account_auth_token: str,
    business_name: str,
    ein: str,
    contact_name: str,
    ein_document_data: str,
    ein_document_type: str,
    ein_document_name: str,
) -> str:
    """Upload an EIN confirmation letter (CP 575) to Twilio Trust Hub.

    The SDK's trusthub.v1.supporting_documents.create() only supports
    form-urlencoded (no file uploads), so we POST directly to the REST API
    with multipart/form-data.

    Returns the SupportingDocument SID (RD...) or empty string on failure.
    """
    import requests as _requests
    import base64
    import io

    if not ein_document_data or not sub_account_sid or not sub_account_auth_token:
        return ""

    # Decode base64 document to bytes
    # Frontend sends "data:application/pdf;base64,..." or raw base64
    raw_b64 = ein_document_data
    if "," in raw_b64:
        raw_b64 = raw_b64.split(",", 1)[1]
    try:
        file_bytes = base64.b64decode(raw_b64)
    except Exception as e:
        logger.error(f"[SpamProtection] EIN document base64 decode failed: {e}")
        return ""

    if len(file_bytes) > 5 * 1024 * 1024:
        logger.error("[SpamProtection] EIN document exceeds 5MB limit")
        return ""

    # Determine MIME type
    mime = ein_document_type or "application/octet-stream"
    if mime == "application/octet-stream":
        if ein_document_name.lower().endswith(".pdf"):
            mime = "application/pdf"
        elif ein_document_name.lower().endswith((".jpg", ".jpeg")):
            mime = "image/jpeg"
        elif ein_document_name.lower().endswith(".png"):
            mime = "image/png"

    # Split contact name for attributes
    name_parts = (contact_name or "").strip().split(None, 1)
    first_name = name_parts[0] if name_parts else ""
    last_name = name_parts[1] if len(name_parts) > 1 else first_name

    # Twilio requires ALL attribute fields present for government_issued_document
    attributes = {
        "first_name": first_name,
        "last_name": last_name,
        "business_name": business_name,
        "document_number": ein,
        "tax_id_number": ein,
        "issuing_country": "US",
        "issuing_authority": "IRS",
        "issue_date": "",
        "expiry_date": "",
        "nationality": "US",
        "place_of_issue": "US",
        "series": "",
        "address_sids": "",
        "personal_id_number": "",
        "birth_date": "",
        "business_description": "Insurance Agency",
    }

    filename = ein_document_name or "ein_confirmation.pdf"

    resp = _requests.post(
        "https://trusthub.twilio.com/v1/SupportingDocuments",
        auth=(sub_account_sid, sub_account_auth_token),
        data={
            "FriendlyName": f"{business_name} EIN Confirmation",
            "Type": "government_issued_document",
            "Attributes": json.dumps(attributes),
        },
        files={
            "File": (filename, io.BytesIO(file_bytes), mime),
        },
        timeout=30,
    )

    if resp.status_code == 201:
        sid = resp.json().get("sid", "")
        logger.info(f"[SpamProtection] EIN document uploaded: {sid}")
        return sid
    else:
        try:
            error_msg = resp.json().get("message", resp.text[:200])
        except Exception:
            error_msg = resp.text[:200]
        logger.error(
            f"[SpamProtection] EIN document upload failed ({resp.status_code}): {error_msg}"
        )
        return ""


