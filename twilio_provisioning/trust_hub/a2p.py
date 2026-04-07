"""Module extracted from twilio_provisioning.py."""

import logging
import requests
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

# ──────────────────────────────────────────────────────────────
# A2P 10DLC — BRAND & CAMPAIGN REGISTRATION
# ──────────────────────────────────────────────────────────────
#
# Twilio A2P 10DLC flow:
#   1. Create a Trust Product (Brand) under the master account
#   2. Submit Brand for vetting (Twilio charges a one-time fee)
#   3. Create a Messaging Service on the sub-account
#   4. Associate phone number(s) with the Messaging Service
#   5. Create a Campaign under the Brand and link to Messaging Service
#
# OR — import an already-approved Brand/Campaign from another provider
# (e.g., GHL/LeadConnector) using the BrandRegistration + external
# campaign import flow.
#
# All state is stored in voice_config["a2p"] JSONB.

# ── Valid A2P use-case categories for campaign registration ──
A2P_USE_CASES = [
    "2FA", "ACCOUNT_NOTIFICATION", "CUSTOMER_CARE", "DELIVERY_NOTIFICATION",
    "FRAUD_ALERT", "HIGHER_EDUCATION", "LOW_VOLUME", "MARKETING",
    "MIXED", "POLLING_VOTING", "PUBLIC_SERVICE_ANNOUNCEMENT",
    "SECURITY_ALERT", "SOLE_PROPRIETOR",
]


def create_a2p_brand(sub_account_sid: str,
                     business_name: str, ein: str,
                     street: str, city: str, state: str, zip_code: str,
                     contact_email: str, contact_phone: str,
                     business_type: str = "private_profit",
                     stock_exchange: str = "NONE",
                     stock_ticker: str = "",
                     website: str = "",
                     vertical: str = "INSURANCE",
                     sub_account_auth_token: str = "",
                     first_name: str = "", last_name: str = "",
                     brand_type: str = "LOW_VOLUME",
                     job_title: str = "", job_position: str = "CEO") -> dict:
    """
    Register a new A2P 10DLC Brand via Twilio's Trust Hub + Brand
    Registration API. Supports three brand types with distinct flows:

    SOLE PROPRIETOR (brand_type='SOLE_PROPRIETOR'):
      - Uses Starter Customer Profile (policy RN806dd6cd175f314e1f96a9727ee271f4)
      - EndUser type: sole_proprietor_information
      - Trust Bundle policy: RN670d5d2e282a6130ae063b234b6019c8
      - No EIN required (uses mobile phone OTP verification)
      Docs: https://www.twilio.com/docs/messaging/compliance/a2p-10dlc/onboarding-isv-api-sole-prop-new

    LOW_VOLUME / STANDARD (brand_type='LOW_VOLUME' or 'STANDARD'):
      - Uses Secondary Customer Profile (policy RNdfbf3fae0e1107f8aded0e7cead80bf5)
      - EndUser type: us_a2p_messaging_profile_information
      - Trust Product policy: RNb0d4771c2c98518d916a3d4cd70a8f8b
      - EIN required
      Docs: https://www.twilio.com/docs/messaging/compliance/a2p-10dlc/onboarding-isv-api
    """
    sub_account_auth_token = _ensure_sub_account_auth_token(
        sub_account_sid, sub_account_auth_token
    )
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)

    # ── Route to the correct flow based on brand type ──
    if brand_type == "SOLE_PROPRIETOR":
        return _create_a2p_brand_sole_prop(
            client, sub_account_sid, sub_account_auth_token,
            business_name, contact_email, contact_phone,
            first_name, last_name, vertical,
        )
    else:
        return _create_a2p_brand_standard(
            client, sub_account_sid, sub_account_auth_token,
            business_name, ein, street, city, state, zip_code,
            contact_email, contact_phone, business_type,
            stock_exchange, stock_ticker, website, vertical,
            first_name=first_name, last_name=last_name,
            job_title=job_title, job_position=job_position,
        )


# ── Policy SIDs for A2P registration ──
_A2P_STARTER_PROFILE_POLICY = "RN806dd6cd175f314e1f96a9727ee271f4"  # Sole Prop Starter Profile
_A2P_SOLE_PROP_BUNDLE_POLICY = "RN670d5d2e282a6130ae063b234b6019c8"  # Sole Prop Trust Bundle
_A2P_STANDARD_PROFILE_POLICY = "RNb0d4771c2c98518d916a3d4cd70a8f8b"  # Standard/LV Trust Product


def _create_a2p_brand_sole_prop(client, sub_sid, sub_auth,
                                 business_name, email, phone,
                                 first_name, last_name, vertical):
    """
    Sole Proprietor A2P registration — different flow than Standard/LV.

    1. Create Starter Customer Profile (policy RN806dd6...)
    2. Create EndUser (sole_proprietor_information) with mobile phone for OTP
    3. Create Sole Prop Trust Bundle (policy RN670d5d...)
    4. Attach EndUser + Profile to Bundle
    5. Submit for evaluation
    6. Create Brand Registration

    Twilio sends OTP to the mobile_phone_number for verification.
    """
    try:
        primary_sid = _find_primary_profile_sid()

        # Step 1: Starter Customer Profile
        profile = client.trusthub.v1.customer_profiles.create(
            friendly_name=f"SP Profile: {business_name}",
            email=email,
            policy_sid=_A2P_STARTER_PROFILE_POLICY,
        )
        profile_sid = profile.sid
        logger.info(f"Created Sole Prop Starter Profile: {profile_sid}")

        # Link to Primary Business Profile
        if primary_sid:
            try:
                client.trusthub.v1.customer_profiles(profile_sid) \
                    .customer_profiles_entity_assignments.create(object_sid=primary_sid)
            except Exception as link_err:
                logger.warning(f"Primary profile link failed (non-fatal): {link_err}")

        # Step 2: EndUser (sole_proprietor_information)
        end_user = client.trusthub.v1.end_users.create(
            friendly_name=f"{business_name} SP EndUser",
            type="sole_proprietor_information",
            attributes={
                "business_name": business_name,
                "mobile_phone_number": phone,
                "vertical": vertical,
            },
        )
        logger.info(f"Created Sole Prop EndUser: {end_user.sid}")

        # Attach EndUser to Profile
        client.trusthub.v1.customer_profiles(profile_sid) \
            .customer_profiles_entity_assignments.create(object_sid=end_user.sid)

        # Submit Profile for evaluation
        _trusthub_update_status(
            "CustomerProfiles", profile_sid, "pending-review",
            sub_sid, sub_auth,
        )

        # Step 3: Sole Prop Trust Bundle
        trust_bundle = client.trusthub.v1.trust_products.create(
            friendly_name=f"SP Bundle: {business_name}",
            email=email,
            policy_sid=_A2P_SOLE_PROP_BUNDLE_POLICY,
        )
        logger.info(f"Created Sole Prop Trust Bundle: {trust_bundle.sid}")

        # Step 4: Attach EndUser + Profile to Bundle
        client.trusthub.v1.trust_products(trust_bundle.sid) \
            .trust_products_entity_assignments.create(object_sid=end_user.sid)
        client.trusthub.v1.trust_products(trust_bundle.sid) \
            .trust_products_entity_assignments.create(object_sid=profile_sid)

        # Step 5: Submit Bundle for evaluation
        _trusthub_update_status(
            "TrustProducts", trust_bundle.sid, "pending-review",
            sub_sid, sub_auth,
        )

        # Step 6: Register Brand
        brand = client.messaging.v1.brand_registrations.create(
            customer_profile_bundle_sid=profile_sid,
            a2p_profile_bundle_sid=trust_bundle.sid,
        )

        logger.info(f"Created Sole Prop A2P Brand: {brand.sid} status={brand.status}")
        return {
            "brand_sid": brand.sid,
            "status": brand.status,
            "profile_sid": profile_sid,
            "trust_product_sid": trust_bundle.sid,
            "end_user_sid": end_user.sid,
            "business_name": business_name,
            "brand_type": "SOLE_PROPRIETOR",
        }
    except TwilioRestException as e:
        logger.error(f"Sole Prop A2P Brand registration failed: {e}")
        raise


def _create_a2p_brand_standard(client, sub_sid, sub_auth,
                                business_name, ein, street, city, state, zip_code,
                                contact_email, contact_phone, business_type,
                                stock_exchange, stock_ticker, website, vertical,
                                first_name="", last_name="",
                                job_title="", job_position="CEO"):
    """
    Standard / Low-Volume Standard A2P registration.

    1. Secondary Customer Profile (ISV pattern)
    2. EndUser (us_a2p_messaging_profile_information)
    3. TrustProduct (A2P Messaging Profile Bundle)
    4. Attach EndUser + Profile to TrustProduct
    5. Submit for evaluation
    6. Create Brand Registration
    """
    try:
        primary_sid = _find_primary_profile_sid()
        profile_sid = _find_or_create_secondary_profile(
            client, sub_sid, business_name, contact_email,
            primary_profile_sid=primary_sid,
            sub_account_auth_token=sub_auth,
        )
        logger.info(f"Created/reused A2P Secondary Customer Profile: {profile_sid}")

        # EndUser with business information + authorized rep
        attrs = {
            "company_type": business_type,
            "stock_exchange": stock_exchange,
            "stock_ticker": stock_ticker,
            "brand_name": business_name,
            "ein": ein,
            "ein_issuing_country": "US",
            "street": street,
            "city": city,
            "state": state,
            "postal_code": zip_code,
            "country": "US",
            "website": website or "",
            "vertical": vertical,
            "phone_number": contact_phone,
            "email": contact_email,
            "business_regions_of_operation": "USA_AND_CANADA",
        }
        # Authorized representative info (Twilio docs: required for brand vetting)
        if first_name:
            attrs["first_name"] = first_name
        if last_name:
            attrs["last_name"] = last_name
        if job_title:
            attrs["business_title"] = job_title
        if job_position:
            attrs["job_position"] = job_position

        end_user = client.trusthub.v1.end_users.create(
            friendly_name=f"{business_name} A2P EndUser",
            type="us_a2p_messaging_profile_information",
            attributes=attrs,
        )
        logger.info(f"Created A2P EndUser: {end_user.sid}")

        # TrustProduct
        trust_product = client.trusthub.v1.trust_products.create(
            friendly_name=f"A2P Profile: {business_name}",
            email=contact_email,
            policy_sid=_A2P_STANDARD_PROFILE_POLICY,
        )
        logger.info(f"Created A2P TrustProduct: {trust_product.sid}")

        # Attach EndUser + Profile
        client.trusthub.v1.trust_products(trust_product.sid) \
            .trust_products_entity_assignments.create(object_sid=end_user.sid)
        client.trusthub.v1.trust_products(trust_product.sid) \
            .trust_products_entity_assignments.create(object_sid=profile_sid)

        # Submit for evaluation
        _trusthub_update_status(
            "TrustProducts", trust_product.sid, "pending-review",
            sub_sid, sub_auth,
        )
        logger.info(f"Submitted TrustProduct {trust_product.sid} for review")

        # Register Brand
        brand = client.messaging.v1.brand_registrations.create(
            customer_profile_bundle_sid=profile_sid,
            a2p_profile_bundle_sid=trust_product.sid,
        )

        logger.info(f"Created A2P Brand: {brand.sid} status={brand.status}")
        return {
            "brand_sid": brand.sid,
            "status": brand.status,
            "profile_sid": profile_sid,
            "trust_product_sid": trust_product.sid,
            "end_user_sid": end_user.sid,
            "business_name": business_name,
            "brand_type": "STANDARD",
        }
    except TwilioRestException as e:
        logger.error(f"A2P Brand registration failed: {e}")
        raise


def get_a2p_brand_status(brand_sid: str, sub_account_sid: str = "",
                          sub_account_auth_token: str = "") -> dict:
    """Check the vetting status of an A2P Brand.
    Always uses sub-account client when sub_account_sid is provided."""
    if not sub_account_sid:
        raise ValueError("sub_account_sid is required for A2P brand status — do not use master account")
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    try:
        brand = client.messaging.v1.brand_registrations(brand_sid).fetch()
        return {
            "brand_sid": brand.sid,
            "status": brand.status,
            "brand_score": getattr(brand, "brand_score", None),
            "brand_feedback": getattr(brand, "brand_feedback", None),
            "errors": getattr(brand, "errors", []),
        }
    except TwilioRestException as e:
        logger.error(f"Failed to fetch brand status: {e}")
        raise


def create_messaging_service(sub_account_sid: str,
                              friendly_name: str,
                              sub_account_auth_token: str = "") -> dict:
    """
    Create a Messaging Service on the sub-account.
    This is required to associate phone numbers with an A2P campaign.
    """
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    try:
        svc = client.messaging.v1.services.create(
            friendly_name=friendly_name,
            inbound_request_url="",  # We handle SMS via GHL, not Twilio inbound
            inbound_method="POST",
            use_inbound_webhook_on_number=True,
        )
        logger.info(f"Created Messaging Service: {svc.sid} on {sub_account_sid}")
        return {"messaging_service_sid": svc.sid}
    except TwilioRestException as e:
        logger.error(f"Failed to create Messaging Service: {e}")
        raise


def add_phone_to_messaging_service(sub_account_sid: str,
                                    messaging_service_sid: str,
                                    phone_number_sid: str,
                                    sub_account_auth_token: str = "") -> bool:
    """Associate a phone number with a Messaging Service."""
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    try:
        client.messaging.v1.services(messaging_service_sid).phone_numbers.create(
            phone_number_sid=phone_number_sid,
        )
        logger.info(f"Added {phone_number_sid} to MessagingService {messaging_service_sid}")
        return True
    except TwilioRestException as e:
        logger.error(f"Failed to add phone to messaging service: {e}")
        raise


def create_a2p_campaign(messaging_service_sid: str,
                         brand_registration_sid: str,
                         description: str,
                         use_case: str = "LOW_VOLUME",
                         sample_messages: list = None,
                         has_embedded_links: bool = False,
                         has_embedded_phone: bool = False,
                         message_flow: str = "",
                         sub_account_sid: str = "",
                         sub_account_auth_token: str = "") -> dict:
    """
    Create an A2P 10DLC Campaign and associate it with a Messaging Service.

    Twilio endpoint: POST /v1/Services/{MessagingServiceSid}/UsAppToPerson
    This is the final step — once the campaign is approved, the number can
    send 10DLC-compliant SMS.
    """
    if not sub_account_sid:
        raise ValueError("sub_account_sid is required for A2P campaign creation — do not use master account")
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)

    if not sample_messages:
        sample_messages = [
            "Hi {name}, this is {agent} from {agency}. I wanted to follow up on your insurance inquiry.",
            "Thanks for your interest! I have a few options that might work for you. When is a good time to chat?",
        ]
    if not message_flow:
        message_flow = (
            "Consumers opt-in by filling out an online form requesting "
            "an insurance quote. An agent replies via SMS to schedule "
            "a consultation."
        )

    try:
        campaign = client.messaging.v1.services(
            messaging_service_sid
        ).us_app_to_person.create(
            brand_registration_sid=brand_registration_sid,
            description=description[:4096],
            message_flow=message_flow[:2048],
            message_samples=sample_messages[:5],
            us_app_to_person_usecase=use_case,
            has_embedded_links=has_embedded_links,
            has_embedded_phone=has_embedded_phone,
            opt_in_message="Reply YES to confirm you'd like to receive messages from us.",
            opt_out_message="Reply STOP to unsubscribe. You will no longer receive messages from us.",
            help_message="Reply HELP for support. Msg & data rates may apply.",
            opt_in_keywords=["START", "YES"],
            opt_out_keywords=["STOP", "UNSUBSCRIBE", "CANCEL"],
            help_keywords=["HELP", "INFO"],
        )
        logger.info(
            f"Created A2P Campaign: {campaign.sid} "
            f"status={campaign.campaign_status} "
            f"on MessagingService {messaging_service_sid}"
        )
        return {
            "campaign_sid": campaign.sid,
            "campaign_status": campaign.campaign_status,
            "messaging_service_sid": messaging_service_sid,
            "use_case": use_case,
        }
    except TwilioRestException as e:
        logger.error(f"A2P Campaign creation failed: {e}")
        raise


def get_a2p_campaign_status(messaging_service_sid: str,
                             campaign_sid: str,
                             sub_account_sid: str = "",
                             sub_account_auth_token: str = "") -> dict:
    """Check the approval status of an A2P Campaign."""
    if not sub_account_sid:
        raise ValueError("sub_account_sid is required for A2P campaign status — do not use master account")
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    try:
        campaign = client.messaging.v1.services(
            messaging_service_sid
        ).us_app_to_person(campaign_sid).fetch()
        return {
            "campaign_sid": campaign.sid,
            "campaign_status": campaign.campaign_status,
            "description": getattr(campaign, "description", ""),
            "use_case": getattr(campaign, "us_app_to_person_usecase", ""),
            "errors": getattr(campaign, "errors", []),
        }
    except TwilioRestException as e:
        logger.error(f"Failed to fetch campaign status: {e}")
        raise


def list_messaging_services(sub_account_sid: str,
                             sub_account_auth_token: str = "") -> list:
    """List all Messaging Services on a sub-account."""
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    try:
        services = client.messaging.v1.services.list(limit=50)
        return [
            {
                "sid": s.sid,
                "friendly_name": s.friendly_name,
                "date_created": s.date_created.isoformat() if s.date_created else "",
            }
            for s in services
        ]
    except TwilioRestException as e:
        logger.error(f"Failed to list messaging services: {e}")
        return []


def list_messaging_service_phone_numbers(messaging_service_sid: str,
                                          sub_account_sid: str = "",
                                          sub_account_auth_token: str = "") -> list:
    """
    List all phone numbers associated with a Messaging Service.
    Returns list of {sid, phone_number} dicts — these are the numbers
    actually registered for A2P via this messaging service.
    The `sid` is the PN... IncomingPhoneNumber SID.
    """
    if not sub_account_sid:
        raise ValueError("sub_account_sid is required for listing messaging service phone numbers")
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    try:
        numbers = client.messaging.v1.services(
            messaging_service_sid
        ).phone_numbers.list(limit=400)
        return [
            {
                "sid": n.sid,
                "phone_number": getattr(n, "phone_number", ""),
            }
            for n in numbers
        ]
    except TwilioRestException as e:
        logger.error(f"Failed to list phone numbers on MS {messaging_service_sid}: {e}")
        return []


# ──────────────────────────────────────────────────────────────
# A2P DISCOVERY — Sync existing registrations from Twilio
# ──────────────────────────────────────────────────────────────

def discover_a2p_brands(sub_account_sid: str = "",
                         sub_account_auth_token: str = "") -> list:
    """
    Discover all existing A2P Brand Registrations on a sub-account.
    Returns list of {brand_sid, status, brand_score, ...} dicts.
    """
    if not sub_account_sid:
        raise ValueError("sub_account_sid is required for A2P brand discovery — do not use master account")
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    try:
        brands = client.messaging.v1.brand_registrations.list(limit=100)
        results = []
        for b in brands:
            results.append({
                "brand_sid": b.sid,
                "status": b.status,
                "brand_score": getattr(b, "brand_score", None),
                "brand_feedback": getattr(b, "brand_feedback", None),
                "a2p_profile_bundle_sid": getattr(b, "a2p_profile_bundle_sid", ""),
                "customer_profile_bundle_sid": getattr(b, "customer_profile_bundle_sid", ""),
                "date_created": b.date_created.isoformat() if b.date_created else "",
                "date_updated": b.date_updated.isoformat() if b.date_updated else "",
            })
        logger.info(f"Discovered {len(results)} A2P brands on sub-account {sub_account_sid}")
        return results
    except TwilioRestException as e:
        logger.error(f"Failed to discover A2P brands: {e}")
        return []


def discover_a2p_campaigns(messaging_service_sid: str,
                            sub_account_sid: str = "",
                            sub_account_auth_token: str = "") -> list:
    """
    Discover all existing A2P campaigns on a Messaging Service.
    Returns list of {campaign_sid, campaign_status, description, use_case} dicts.
    """
    if not sub_account_sid:
        raise ValueError("sub_account_sid is required for A2P campaign discovery — do not use master account")
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    try:
        campaigns = client.messaging.v1.services(
            messaging_service_sid
        ).us_app_to_person.list(limit=50)
        results = []
        for c in campaigns:
            results.append({
                "campaign_sid": c.sid,
                "campaign_status": c.campaign_status,
                "description": getattr(c, "description", ""),
                "use_case": getattr(c, "us_app_to_person_usecase", ""),
                "brand_registration_sid": getattr(c, "brand_registration_sid", ""),
                "date_created": c.date_created.isoformat() if c.date_created else "",
            })
        logger.info(f"Discovered {len(results)} A2P campaigns on MS {messaging_service_sid}")
        return results
    except TwilioRestException as e:
        logger.error(f"Failed to discover campaigns on {messaging_service_sid}: {e}")
        return []


def discover_trust_hub_profiles(sub_account_sid: str = "",
                                 sub_account_auth_token: str = "") -> list:
    """
    Discover all Trust Hub Customer Profiles on a sub-account.
    Returns list of {profile_sid, status, friendly_name} dicts.
    """
    if not sub_account_sid:
        raise ValueError("sub_account_sid is required for Trust Hub profile discovery — do not use master account")
    client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    try:
        profiles = client.trusthub.v1.customer_profiles.list(limit=100)
        results = []
        for p in profiles:
            results.append({
                "profile_sid": p.sid,
                "status": p.status,
                "friendly_name": getattr(p, "friendly_name", ""),
                "date_created": p.date_created.isoformat() if p.date_created else "",
            })
        logger.info(f"Discovered {len(results)} Trust Hub profiles")
        return results
    except TwilioRestException as e:
        logger.error(f"Failed to discover Trust Hub profiles: {e}")
        return []


def discover_full_a2p_status(sub_account_sid: str,
                              sub_account_auth_token: str = "") -> dict:
    """
    Comprehensive A2P discovery: finds brands, messaging services, and campaigns.
    Always queries the sub-account directly — never the master account.
    Returns dict ready to merge into voice_config['a2p'].
    """
    result = {
        "brands": [],
        "messaging_services": [],
        "campaigns": [],
        "best_brand": None,
        "best_campaign": None,
    }

    # 1. Discover brands on the sub-account
    brands = discover_a2p_brands(sub_account_sid, sub_account_auth_token)
    result["brands"] = brands

    # Find the best brand (prefer APPROVED, then most recent)
    approved_brands = [b for b in brands if b.get("status", "").upper() == "APPROVED"]
    if approved_brands:
        result["best_brand"] = approved_brands[0]
    elif brands:
        result["best_brand"] = brands[0]

    # 2. Discover messaging services on the sub-account
    ms_list = list_messaging_services(sub_account_sid, sub_account_auth_token) if sub_account_sid else []
    result["messaging_services"] = ms_list

    # 3. Discover campaigns on each messaging service
    for ms in ms_list:
        campaigns = discover_a2p_campaigns(ms["sid"], sub_account_sid, sub_account_auth_token)
        for c in campaigns:
            c["messaging_service_sid"] = ms["sid"]
        result["campaigns"].extend(campaigns)

    # Find the best campaign (prefer VERIFIED/APPROVED)
    good_statuses = ("VERIFIED", "APPROVED", "IN_PROGRESS")
    good_campaigns = [c for c in result["campaigns"]
                      if c.get("campaign_status", "").upper() in good_statuses]
    if good_campaigns:
        result["best_campaign"] = good_campaigns[0]
    elif result["campaigns"]:
        result["best_campaign"] = result["campaigns"][0]

    return result



