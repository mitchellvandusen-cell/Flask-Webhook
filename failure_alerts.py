# failure_alerts.py — Branded "Uh Oh" failure notification emails
#
# Single entry point: send_failure_alert(email, failure_type, details)
# Sends a branded, non-technical email with a clear action button.
# Uses Mailgun via send_email_api.py (already configured).
#
# Rate-limited: max 1 email per failure_type per user per 6 hours
# to prevent email bombing during persistent failures.

import os
import logging
import time
from datetime import datetime

logger = logging.getLogger(__name__)

YOUR_DOMAIN = os.getenv('YOUR_DOMAIN', 'https://insurancegrokbot.click')

# Rate limit: {email:failure_type: timestamp} — in-memory, resets on deploy
_sent_cache = {}
_RATE_LIMIT_SECONDS = 6 * 3600  # 6 hours between duplicate alerts


# ── Failure Type Definitions ─────────────────────────────────────────────────

FAILURE_TEMPLATES = {
    'token_expired': {
        'subject': 'Your CRM connection needs a quick reconnect',
        'headline': 'Your GoHighLevel connection expired',
        'body': (
            'Your AI assistant lost its connection to GoHighLevel, which means '
            'it can\'t read your contacts or send messages right now. '
            'This happens occasionally when tokens expire — it\'s an easy fix.'
        ),
        'action_text': 'Reconnect GoHighLevel',
        'action_url': '/oauth/initiate',
        'tip': 'This usually happens every few weeks. One click and you\'re back up.',
    },
    'payment_failed': {
        'subject': 'Your payment didn\'t go through',
        'headline': 'We couldn\'t process your payment',
        'body': (
            'Your most recent payment attempt was declined. Your subscription '
            'is still active for now, but if we can\'t collect payment soon, '
            'your AI dialer and texting bot will be paused.'
        ),
        'action_text': 'Update Payment Method',
        'action_url': '/dashboard?tab=billing',
        'tip': 'You can update your card in the Billing tab of your dashboard.',
    },
    'webhook_error': {
        'subject': 'Your AI assistant hit a snag',
        'headline': 'Something went wrong processing a message',
        'body': (
            'Your AI assistant ran into an issue while trying to handle '
            'an incoming message. Our system logged the error and will '
            'retry automatically, but you may want to check your recent conversations.'
        ),
        'action_text': 'Open Dashboard',
        'action_url': '/dashboard',
        'tip': 'If this keeps happening, try reconnecting your CRM from the Connect tab.',
    },
    'number_spam': {
        'subject': 'One of your phone numbers needs attention',
        'headline': 'A phone number was flagged by carriers',
        'body': (
            'One of your outbound numbers has been flagged as potential spam '
            'by carriers. This can reduce your answer rates. We\'ve automatically '
            'rotated it to resting status, but you should check your number health.'
        ),
        'action_text': 'Check Number Health',
        'action_url': '/dashboard?tab=voicedialer',
        'tip': 'Smart rotation is protecting your other numbers. Consider registering for Voice Integrity.',
    },
    'a2p_rejected': {
        'subject': 'Your SMS registration needs an update',
        'headline': 'A2P 10DLC registration was rejected',
        'body': (
            'Your A2P 10DLC brand or campaign registration was rejected by '
            'the carriers. This means your SMS messages may be filtered or blocked. '
            'You\'ll need to update your registration details and resubmit.'
        ),
        'action_text': 'View A2P Guide',
        'action_url': '/a2p-guide',
        'tip': 'Common reasons: incorrect EIN, mismatched business name, or unsupported use case.',
    },
    'ai_minutes_low': {
        'subject': 'Your AI minutes are running low',
        'headline': 'You\'re almost out of AI minutes',
        'body': (
            'Your AI voice agent uses minutes from your balance for each call. '
            'You\'re running low — once they\'re gone, AI calls will stop working '
            'until you top up. Human dialing is unaffected.'
        ),
        'action_text': 'Top Up AI Minutes',
        'action_url': '/dashboard?tab=aiminutes',
        'tip': 'Pro tip: set up auto-refill so you never run out mid-session.',
    },
    'ai_minutes_empty': {
        'subject': 'You\'re out of AI minutes',
        'headline': 'Your AI minute balance hit zero',
        'body': (
            'Your AI voice agent can\'t make calls right now because your '
            'AI minute balance is empty. Human dialing still works fine — '
            'just top up to get AI calls back online.'
        ),
        'action_text': 'Buy AI Minutes',
        'action_url': '/dashboard?tab=aiminutes',
        'tip': None,
    },
}


# ── Email Builder ────────────────────────────────────────────────────────────

def _build_alert_html(template: dict, details: str = '') -> str:
    """Build a branded alert email from a template dict."""
    headline = template['headline']
    body = template['body']
    action_text = template.get('action_text', 'Open Dashboard')
    action_url = YOUR_DOMAIN + template.get('action_url', '/dashboard')
    tip = template.get('tip')

    details_html = ''
    if details:
        details_html = f'''
        <div style="background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08);
                    border-radius: 8px; padding: 14px 18px; margin: 20px 0; font-family: monospace;
                    font-size: 0.82rem; color: #888; word-break: break-word;">
            {details}
        </div>
        '''

    tip_html = ''
    if tip:
        tip_html = f'''
        <div style="background: rgba(0,255,136,0.04); border-left: 3px solid rgba(0,255,136,0.3);
                    padding: 12px 16px; border-radius: 0 8px 8px 0; margin: 20px 0;">
            <span style="color: #00ff88; font-weight: 600; font-size: 0.82rem;">TIP:</span>
            <span style="color: #aaa; font-size: 0.88rem;"> {tip}</span>
        </div>
        '''

    return f'''
    <div style="font-family: 'Outfit', Arial, sans-serif; max-width: 600px; margin: 0 auto;
                background: #0a0a0a; border-radius: 20px; overflow: hidden;">

        <!-- Header -->
        <div style="background: linear-gradient(135deg, #1a0000 0%, #0a0a0a 100%);
                    padding: 30px 40px 20px; text-align: center;
                    border-bottom: 1px solid rgba(239,68,68,0.15);">
            <div style="font-size: 2rem; margin-bottom: 8px;">&#9888;&#65039;</div>
            <h1 style="color: #f87171; font-size: 1.1rem; font-weight: 700; margin: 0;">
                Uh oh — heads up
            </h1>
        </div>

        <!-- Body -->
        <div style="padding: 30px 40px;">
            <h2 style="color: #ffffff; font-size: 1.2rem; font-weight: 700; margin: 0 0 16px;">
                {headline}
            </h2>

            <p style="color: #cccccc; font-size: 0.95rem; line-height: 1.7; margin: 0 0 20px;">
                {body}
            </p>

            {details_html}

            <!-- Action Button -->
            <div style="text-align: center; margin: 30px 0;">
                <a href="{action_url}"
                   style="background: linear-gradient(135deg, #00ff88, #00cc6a);
                          color: #000000; padding: 14px 36px; border-radius: 10px;
                          text-decoration: none; font-weight: 700; font-size: 1rem;
                          display: inline-block; box-shadow: 0 4px 20px rgba(0,255,136,0.25);">
                    {action_text}
                </a>
            </div>

            {tip_html}

            <p style="color: #888; font-size: 0.88rem; line-height: 1.6; margin-top: 24px;">
                If you need help, just reply to this email or
                <a href="{YOUR_DOMAIN}/dashboard" style="color: #00ff88; text-decoration: none;">
                    chat with your AI assistant
                </a> in the dashboard.
            </p>
        </div>

        <!-- Footer -->
        <div style="border-top: 1px solid rgba(255,255,255,0.06); padding: 20px 40px;
                    text-align: center;">
            <p style="color: #444; font-size: 0.78rem; margin: 0;">
                Omnisconn — AI-Powered Insurance Sales
            </p>
        </div>
    </div>
    '''


# ── Public API ───────────────────────────────────────────────────────────────

def send_failure_alert(email: str, failure_type: str, details: str = '') -> bool:
    """Send a branded failure notification email.

    Args:
        email: recipient email address
        failure_type: one of FAILURE_TEMPLATES keys
        details: optional technical detail string (shown in monospace block)

    Returns:
        True if sent, False if skipped (rate limited, unknown type, or send error)
    """
    if not email:
        return False

    template = FAILURE_TEMPLATES.get(failure_type)
    if not template:
        logger.warning(f"Unknown failure_type '{failure_type}' — skipping alert to {email}")
        return False

    # Rate limit: max 1 per type per user per 6 hours
    cache_key = f"{email}:{failure_type}"
    last_sent = _sent_cache.get(cache_key, 0)
    if time.time() - last_sent < _RATE_LIMIT_SECONDS:
        logger.debug(f"Rate limited: {failure_type} alert to {email} (sent {int(time.time() - last_sent)}s ago)")
        return False

    try:
        from send_email_api import send_email_via_api
        html = _build_alert_html(template, details)
        success = send_email_via_api(
            to_email=email,
            subject=template['subject'],
            html_body=html,
        )
        if success:
            _sent_cache[cache_key] = time.time()
            logger.info(f"Failure alert sent: {failure_type} → {email}")
        else:
            logger.warning(f"Failure alert send failed: {failure_type} → {email}")
        return bool(success)
    except Exception as e:
        logger.error(f"Failure alert error ({failure_type} → {email}): {e}")
        return False
