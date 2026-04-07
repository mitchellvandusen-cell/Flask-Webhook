"""Module extracted from twilio_provisioning.py."""

import logging
import requests
from twilio.base.exceptions import TwilioRestException

from ..client import (
    get_sub_account_client_native,
    _trusthub_update_status,
    TWILIO_ACCOUNT_SID,
)

logger = logging.getLogger("twilio_provisioning")

# CNAM Trust Product policy SID — static across all Twilio accounts.
# https://www.twilio.com/docs/voice/brand-your-calls-using-cnam
CNAM_TRUST_PRODUCT_POLICY_SID = "RNf3db3cd1fe25fcfd3c3ded065c8fea53"



# ──────────────────────────────────────────────────────────────
# VOICE INTEGRITY (NUMBER INTEGRITY)
# ──────────────────────────────────────────────────────────────
#
# Twilio Voice Integrity registers phone numbers with carrier spam
# analytics engines (AT&T/Hiya, T-Mobile/CallHub, Verizon) to
# remediate spam labels and improve call answer rates.
#
# Flow (ISV/sub-account):
#   1. Create or reuse an approved Customer Profile (Business Profile)
#   2. Create a Voice Integrity Trust Product (policy_sid specific to VI)
#   3. Link Customer Profile → Trust Product (EntityAssignment)
#   4. Assign phone numbers → Trust Product (ChannelEndpointAssignment)
#   5. Submit Trust Product for review (status → pending-review)
#   6. Twilio reviews + registers with carriers (24–48 hours)
#
# State stored in voice_config["number_integrity"] JSONB.

# Voice Integrity Trust Product policy SID — different from A2P/SHAKEN.
# This is Twilio's static Voice Integrity policy (same across all accounts).
# Used ONLY for TrustProducts, NOT for CustomerProfiles.
VOICE_INTEGRITY_POLICY_SID = "RN5b3660f9598883b1df4e77f77acefba0"

# Secondary Customer Profile policy SID — used when creating a profile
# on a sub-account that links back to the master's Primary Business Profile.
# Same across all accounts. See:
# https://www.twilio.com/docs/trust-hub/trusthub-rest-api/api-create-secondary-customer-profile
SECONDARY_CUSTOMER_PROFILE_POLICY_SID = "RNdfbf3fae0e1107f8aded0e7cead80bf5"


def check_secondary_profile_status(
    sub_account_sid: str,
    sub_account_auth_token: str,
    profile_sid: str = "",
) -> dict:
    """
    Check the approval status of the Secondary Customer Profile on a sub-account.

    ISV Trust Hub requires an approved Secondary Customer Profile before
    Trust Products (Voice Integrity, CNAM, A2P) can be submitted. This function
    verifies the profile exists and is approved, preventing wasted API calls
    and stuck Trust Products.

    Returns:
        dict with keys:
            - approved (bool): True if profile is twilio-approved
            - status (str): Current profile status
            - profile_sid (str): The checked profile SID
            - message (str): Human-readable status message
    """
    if not profile_sid:
        return {
            "approved": False,
            "status": "missing",
            "profile_sid": "",
            "message": "No Secondary Customer Profile found. Register in Spam Protection first.",
        }

    try:
        client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
        profile = client.trusthub.v1.customer_profiles(profile_sid).fetch()
        status = getattr(profile, "status", "unknown")
        logger.info(f"[TrustHub] Secondary Profile {profile_sid} status: {status}")

        if status == "twilio-approved":
            return {
                "approved": True,
                "status": status,
                "profile_sid": profile_sid,
                "message": "Secondary Customer Profile approved.",
            }
        elif status in ("in-review", "pending-review"):
            return {
                "approved": False,
                "status": status,
                "profile_sid": profile_sid,
                "message": "Your business profile is still under review. "
                           "This typically takes ~24 hours. You can proceed "
                           "with other registrations once it's approved.",
            }
        elif status == "twilio-rejected":
            # Get rejection reasons if available
            reasons = []
            try:
                evals = client.trusthub.v1.customer_profiles(profile_sid) \
                    .customer_profiles_evaluations.list(limit=1)
                if evals:
                    results = getattr(evals[0], "results", []) or []
                    reasons = [
                        r.get("friendly_name", r.get("requirement_key", "unknown"))
                        for r in results
                        if isinstance(r, dict) and r.get("status") == "noncompliant"
                    ]
            except Exception:
                pass
            reason_str = f" Reasons: {', '.join(reasons)}" if reasons else ""
            return {
                "approved": False,
                "status": status,
                "profile_sid": profile_sid,
                "message": f"Your business profile was rejected.{reason_str} "
                           "Please update your Spam Protection registration and resubmit.",
            }
        else:
            return {
                "approved": False,
                "status": status,
                "profile_sid": profile_sid,
                "message": f"Your business profile is in '{status}' state. "
                           "Please complete Spam Protection registration first.",
            }
    except TwilioRestException as e:
        logger.warning(f"[TrustHub] Could not fetch profile {profile_sid}: {e}")
        return {
            "approved": False,
            "status": "error",
            "profile_sid": profile_sid,
            "message": f"Could not verify profile status: {str(e)}",
        }


def is_master_account(sub_account_sid: str) -> bool:
    """DEPRECATED — All subscribers are sub-accounts. Omnisconn (master) is
    managed via Twilio Console only. Kept for backward compat but always
    returns False for Trust Hub purposes.

    Master account: uses Primary Business Profile for Voice Integrity (direct customer flow).
    Sub-account:    uses Secondary Customer Profile linked to Primary (ISV flow).
    """
    return sub_account_sid == TWILIO_ACCOUNT_SID


# Carrier analytics engines that Voice Integrity registers with
VOICE_INTEGRITY_CARRIERS = [
    {"key": "att", "name": "AT&T / Hiya", "icon": "fa-signal",
     "description": "Registers with Hiya analytics to clear spam labels on AT&T devices."},
    {"key": "tmobile", "name": "T-Mobile / CallHub", "icon": "fa-tower-cell",
     "description": "Registers with T-Mobile CallHub for verified caller status."},
    {"key": "verizon", "name": "Verizon", "icon": "fa-shield-halved",
     "description": "Registers with Verizon spam analytics to prevent spam flagging."},
]


def _find_or_create_secondary_profile(
    client,
    sub_account_sid: str,
    business_name: str,
    contact_email: str,
    existing_profile_sid: str = "",
    primary_profile_sid: str = "",
    sub_account_auth_token: str = "",
) -> str:
    """
    Find or create a Secondary Customer Profile on the sub-account.

    ISV/Subaccounts flow per Twilio docs:
      https://www.twilio.com/docs/trust-hub/trusthub-rest-api/api-create-secondary-customer-profile

    1. If existing_profile_sid provided and valid, reuse it.
    2. Otherwise discover approved profiles on the sub-account.
    3. If none found, create a new Secondary Profile with the correct policy
       and link it to the Primary Business Profile on the master account.

    Returns the profile SID (BU...).
    """
    # ── Try reusing an existing profile ──
    # IMPORTANT: Never reuse the Primary Business Profile as a Secondary Profile.
    # The Primary is on the master account and has a different policy. Twilio
    # evaluations will reject Trust Products linked to the wrong profile type.
    # Accept any non-terminal status: draft, pending-review, in-review, twilio-approved.
    # Only skip twilio-rejected profiles (they need to be recreated).
    _REUSABLE_STATUSES = ("draft", "pending-review", "in-review", "twilio-approved")
    if existing_profile_sid and existing_profile_sid != primary_profile_sid:
        try:
            profile = client.trusthub.v1.customer_profiles(existing_profile_sid).fetch()
            status = getattr(profile, "status", "")
            policy = getattr(profile, "policy_sid", "")
            logger.info(f"[SecondaryProfile] Existing profile {existing_profile_sid} status: {status}, policy: {policy}")
            if status in _REUSABLE_STATUSES:
                # Verify it's actually a Secondary profile (correct policy), not a Primary
                if policy and policy != SECONDARY_CUSTOMER_PROFILE_POLICY_SID:
                    logger.warning(
                        f"[SecondaryProfile] Profile {existing_profile_sid} has policy {policy}, "
                        f"expected Secondary policy {SECONDARY_CUSTOMER_PROFILE_POLICY_SID}. Skipping."
                    )
                else:
                    return existing_profile_sid
            else:
                logger.warning(
                    f"[SecondaryProfile] Existing profile {existing_profile_sid} status '{status}' "
                    "is terminal (rejected); will search for another."
                )
        except TwilioRestException as e:
            logger.warning(f"[SecondaryProfile] Could not fetch profile {existing_profile_sid}: {e}")
    elif existing_profile_sid == primary_profile_sid:
        logger.info(f"[SecondaryProfile] Skipping existing_profile_sid — it's the Primary Business Profile")

    # ── Discover reusable Secondary profiles on the sub-account ──
    # Search ALL non-terminal statuses (approved first, then in-review, pending, draft).
    # Skip the Primary Business Profile and any profiles with the wrong policy.
    for search_status in ("twilio-approved", "in-review", "pending-review", "draft"):
        try:
            profiles = client.trusthub.v1.customer_profiles.list(
                status=search_status, limit=20)
            for p in profiles:
                # Skip the Primary Business Profile
                if p.sid == primary_profile_sid:
                    continue
                # Only reuse profiles with the correct Secondary policy
                policy = getattr(p, "policy_sid", "")
                if policy and policy != SECONDARY_CUSTOMER_PROFILE_POLICY_SID:
                    logger.info(f"[SecondaryProfile] Skipping profile {p.sid} (policy={policy}, not Secondary)")
                    continue
                logger.info(
                    f"[SecondaryProfile] Discovered reusable Secondary profile: {p.sid} "
                    f"(status={search_status}, {getattr(p, 'friendly_name', '')})"
                )
                return p.sid
        except Exception as e:
            logger.warning(f"[SecondaryProfile] Profile discovery (status={search_status}) failed: {e}")

    # ── Create a new Secondary Customer Profile ──
    # Uses the Secondary Customer Profile policy, NOT the Voice Integrity policy.
    profile = client.trusthub.v1.customer_profiles.create(
        friendly_name=f"Secondary Profile: {business_name}",
        email=contact_email,
        policy_sid=SECONDARY_CUSTOMER_PROFILE_POLICY_SID,
    )
    profile_sid = profile.sid
    logger.info(f"[SecondaryProfile] Created Secondary Customer Profile: {profile_sid}")

    # ── Link to Primary Business Profile on master account ──
    if primary_profile_sid:
        try:
            client.trusthub.v1.customer_profiles(profile_sid) \
                .customer_profiles_entity_assignments.create(
                    object_sid=primary_profile_sid,
                )
            logger.info(
                f"[SecondaryProfile] Linked Secondary {profile_sid} → "
                f"Primary {primary_profile_sid}"
            )
        except TwilioRestException as e:
            # 20409 = already assigned
            if e.code == 20409:
                logger.info(f"[SecondaryProfile] Secondary already linked to Primary (20409)")
            else:
                logger.warning(f"[SecondaryProfile] Could not link to Primary: {e}")

    # NOTE: Do NOT evaluate or submit the Secondary Profile for review here.
    # The profile is created empty (no EndUser, Address, or Auth Rep attached yet).
    # Evaluating/submitting an empty profile causes Twilio to reject it with
    # "Business Information is missing", "Address sids list is empty", etc.
    #
    # The CALLER (register_business_profile or create_voice_integrity_trust_product)
    # is responsible for attaching all entities and THEN evaluating + submitting.

    return profile_sid


def _find_primary_profile_sid() -> str:
    """
    Find the Primary Business Profile SID on the Omnisconn master account.
    This is created once in the Twilio Console and reused for all sub-accounts.

    REQUIRES TWILIO_PRIMARY_PROFILE_SID env var. Falls back to API discovery
    but logs a warning — set the env var for reliability.
    """
    # Fast path: env var set by operator (REQUIRED for production)
    env_sid = os.getenv("TWILIO_PRIMARY_PROFILE_SID", "").strip()
    if env_sid and env_sid.startswith("BU"):
        return env_sid

    # Fallback: discover via API — but warn loudly
    logger.warning(
        "[TrustHub] TWILIO_PRIMARY_PROFILE_SID not set! "
        "Attempting API discovery — set this env var for reliable Trust Hub operations."
    )
    try:
        master = get_master_client()
        profiles = master.trusthub.v1.customer_profiles.list(
            status="twilio-approved", limit=50)
        for p in profiles:
            fname = getattr(p, "friendly_name", "")
            if "primary" in fname.lower() or "business" in fname.lower():
                logger.info(f"[TrustHub] Discovered Primary Profile: {p.sid} ({fname}). "
                           f"Set TWILIO_PRIMARY_PROFILE_SID={p.sid} to skip this lookup.")
                return p.sid
        if profiles:
            logger.info(f"[TrustHub] Using first master profile as Primary: {profiles[0].sid}. "
                       f"Set TWILIO_PRIMARY_PROFILE_SID={profiles[0].sid} to skip this lookup.")
            return profiles[0].sid
    except Exception as e:
        logger.error(f"[TrustHub] Could not find Primary Profile on master: {e}")

    raise ValueError(
        "No Primary Business Profile found on master account. "
        "Create one in the Twilio Console and set TWILIO_PRIMARY_PROFILE_SID env var."
    )


def unassign_numbers_from_trust_product(
    sub_account_sid: str,
    trust_product_sid: str,
    phone_number_sids: list,
    sub_account_auth_token: str = "",
) -> dict:
    """
    Remove phone number ChannelEndpointAssignments from a Trust Product.

    Twilio only allows a phone number to be assigned to one Trust Product
    at a time. When re-registering after rejection, the old assignments
    must be removed before numbers can be assigned to a new Trust Product.
    """
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    removed = 0
    failed = []

    # Fetch all current assignments on the old trust product
    try:
        assignments = client.trusthub.v1.trust_products(trust_product_sid) \
            .trust_products_channel_endpoint_assignment.list(limit=200)
    except TwilioRestException as e:
        logger.warning(f"[TrustHub] Could not list assignments on {trust_product_sid}: {e}")
        return {"removed": 0, "failed": [], "total": len(phone_number_sids)}

    # Build lookup: channel_endpoint_sid → assignment SID
    assignment_map = {a.channel_endpoint_sid: a.sid for a in assignments}

    target_sids = set(phone_number_sids) if phone_number_sids else set(assignment_map.keys())

    for pn_sid in target_sids:
        assign_sid = assignment_map.get(pn_sid)
        if not assign_sid:
            continue  # not assigned to this trust product
        try:
            client.trusthub.v1.trust_products(trust_product_sid) \
                .trust_products_channel_endpoint_assignment(assign_sid).delete()
            removed += 1
            logger.info(f"[TrustHub] Unassigned {pn_sid} from {trust_product_sid}")
        except TwilioRestException as e:
            failed.append({"sid": pn_sid, "error": str(e)})
            logger.warning(f"[TrustHub] Failed to unassign {pn_sid}: {e}")

    return {"removed": removed, "failed": failed, "total": len(target_sids)}


