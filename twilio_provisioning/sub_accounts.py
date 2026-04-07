"""Module extracted from twilio_provisioning.py."""


def create_sub_account(friendly_name: str) -> dict:
    """
    Create a Twilio sub-account for a subscriber.
    Returns dict with sub-account details.
    """
    client = get_master_client()
    try:
        account = client.api.accounts.create(friendly_name=friendly_name)
        logger.info(f"Created Twilio sub-account: {account.sid} ({friendly_name})")
        return {
            "sid": account.sid,
            "auth_token": account.auth_token,
            "friendly_name": account.friendly_name,
            "status": account.status,
        }
    except TwilioRestException as e:
        logger.error(f"Failed to create sub-account: {e}")
        raise


def suspend_sub_account(sub_account_sid: str) -> bool:
    """Suspend a sub-account (e.g. when subscription lapses)."""
    client = get_master_client()
    try:
        client.api.accounts(sub_account_sid).update(status="suspended")
        logger.info(f"Suspended sub-account: {sub_account_sid}")
        return True
    except TwilioRestException as e:
        logger.error(f"Failed to suspend sub-account {sub_account_sid}: {e}")
        return False


def reactivate_sub_account(sub_account_sid: str) -> bool:
    """Re-activate a suspended sub-account."""
    client = get_master_client()
    try:
        client.api.accounts(sub_account_sid).update(status="active")
        logger.info(f"Reactivated sub-account: {sub_account_sid}")
        return True
    except TwilioRestException as e:
        logger.error(f"Failed to reactivate sub-account {sub_account_sid}: {e}")
        return False


# ──────────────────────────────────────────────────────────────
# TwiML APP MANAGEMENT
# ──────────────────────────────────────────────────────────────

def create_twiml_app(sub_account_sid: str, webhook_base_url: str) -> dict:
    """
    Create a TwiML Application for call handling on a sub-account.
    This routes inbound calls and status callbacks to our server.
    """
    client = get_sub_account_client(sub_account_sid)
    try:
        app = client.applications.create(
            friendly_name="GrokBot Voice",
            voice_url=f"{webhook_base_url}/voice/inbound",
            voice_method="POST",
            status_callback=f"{webhook_base_url}/voice/status",
            status_callback_method="POST",
        )
        logger.info(f"Created TwiML App: {app.sid} for sub-account {sub_account_sid}")
        return {
            "twiml_app_sid": app.sid,
        }
    except TwilioRestException as e:
        logger.error(f"Failed to create TwiML App: {e}")
        raise


def update_twiml_app(sub_account_sid: str, twiml_app_sid: str,
                      webhook_base_url: str) -> bool:
    """
    Update a TwiML Application's voice_url and status_callback to point
    to the current server.  Called during token generation so the TwiML
    app always reaches the live server even after URL changes.
    """
    client = get_sub_account_client(sub_account_sid)
    try:
        client.applications(twiml_app_sid).update(
            voice_url=f"{webhook_base_url}/voice/inbound",
            voice_method="POST",
            status_callback=f"{webhook_base_url}/voice/status",
            status_callback_method="POST",
        )
        logger.info(f"Updated TwiML App {twiml_app_sid} voice_url -> {webhook_base_url}/voice/inbound")
        return True
    except TwilioRestException as e:
        logger.error(f"Failed to update TwiML App {twiml_app_sid}: {e}")
        return False


def create_api_key(account_sid: str) -> dict:
    """
    Create an API Key on a specific account (master or sub-account).
    Required for generating AccessTokens — the API key must belong to
    the same account that will be used in the token.
    """
    client = get_sub_account_client(account_sid)
    try:
        key = client.new_keys.create(friendly_name="GrokBot VoIP Key")
        logger.info(f"Created API Key: {key.sid} for account {account_sid}")
        return {
            "api_key_sid": key.sid,
            "api_key_secret": key.secret,
        }
    except TwilioRestException as e:
        logger.error(f"Failed to create API Key on {account_sid}: {e}")
        raise


# ──────────────────────────────────────────────────────────────
# FULL PROVISIONING
# ──────────────────────────────────────────────────────────────

def provision_subscriber(subscriber_email: str, location_id: str,
                          webhook_base_url: str) -> dict:
    """
    Provision a sub-user (customer):
    1. Create sub-account under master
    2. Create TwiML App
    3. Create API Key on sub-account (for AccessToken generation)

    The user buys their own phone number afterwards via the Numbers tab.
    Returns all the IDs needed for voice_config.
    """
    friendly_name = f"GrokBot_{location_id[:30]}_{subscriber_email[:20]}"

    # 1. Create sub-account
    sub_account = create_sub_account(friendly_name)
    sub_sid = sub_account["sid"]

    # 2. Create TwiML App
    twiml_app = create_twiml_app(sub_sid, webhook_base_url)
    twiml_app_sid = twiml_app["twiml_app_sid"]

    # 3. Create API Key on the sub-account (required for valid AccessTokens)
    api_key = create_api_key(sub_sid)

    # 4. Enable Voice Insights Advanced Features (non-fatal if fails)
    try:
        enable_voice_insights_advanced(sub_sid)
    except Exception as e:
        logger.warning(f"Voice Insights enable failed (non-fatal): {e}")

    result = {
        "twilio_sub_account_sid": sub_sid,
        "twilio_auth_token": sub_account["auth_token"],
        "twilio_twiml_app_sid": twiml_app_sid,
        "twilio_api_key_sid": api_key["api_key_sid"],
        "twilio_api_key_secret": api_key["api_key_secret"],
        "twilio_phone_number": "",
        "twilio_number_sid": "",
    }

    # Persist credentials to dedicated columns (not just voice_config JSONB)
    try:
        from db_legacy import get_db_connection, return_db_connection
        conn = get_db_connection()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("""
                    UPDATE subscribers
                    SET twilio_sub_account_sid = %s,
                        twilio_sub_account_auth_token = %s,
                        updated_at = NOW()
                    WHERE email = %s
                """, (sub_sid, sub_account["auth_token"], subscriber_email))
                conn.commit()
                cur.close()
                logger.info(f"[Provision] Saved Twilio credentials to dedicated columns for {subscriber_email}")
            except Exception as e:
                conn.rollback()
                logger.warning(f"[Provision] Could not save to dedicated columns (non-fatal): {e}")
            finally:
                return_db_connection(conn)
    except ImportError:
        pass

    logger.info(f"Subscriber provisioned: {subscriber_email} -> sub_account={sub_sid} (no number — user buys their own)")
    return result


