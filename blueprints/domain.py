# blueprints/domain.py — Agent Web Presence: domain provisioning + management
#
# Routes:
#   POST /api/domain/search          — Search available .com domains
#   POST /api/domain/checkout        — Register domain + provision (Stripe after success)
#   GET  /api/domain/status          — Current provisioning status
#   POST /api/domain/update-page     — Update landing page content
#   POST /api/domain/cancel          — Cancel domain subscription
#   POST /api/domain/contact-form    — Public: receive lead form submissions (rate-limited)
#   POST /api/domain/auto-reply      — Internal: Twilio email auto-reply (secret-authenticated)
#   GET  /api/domain/sections        — Read section config from KV (page builder)
#   POST /api/domain/sections        — Save section order/toggles/content to KV
#   POST /api/domain/section-ai      — AI copy generation via xAI Grok
#   POST /api/domain/photo           — Upload profile photo (base64)
#   GET  /api/domain/reviews         — List pending + approved reviews
#   POST /api/domain/reviews/approve — Approve/reject a review
#   POST /api/domain/sync-carriers   — Sync carriers from voice_config to KV

import hashlib
import json
import logging
import os
import re
import time
import requests
from functools import wraps
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Blueprint, request, jsonify
from flask_login import current_user, login_required

from db import get_db_connection, return_db_connection, log_webhook_event
from extensions import ensure_redis

logger = logging.getLogger(__name__)

domain_bp = Blueprint('domain', __name__)

# ── API Credentials ──
PORKBUN_API_KEY = os.getenv('PORKBUN_API_KEY', '')
PORKBUN_SECRET_KEY = os.getenv('PORKBUN_SECRET_KEY', '')
CLOUDFLARE_API_TOKEN = os.getenv('CLOUDFLARE_API_TOKEN', '')
CLOUDFLARE_ACCOUNT_ID = os.getenv('CLOUDFLARE_ACCOUNT_ID', '')
MAILGUN_API_KEY = os.getenv('MAILGUN_API_KEY', '')  # Per-domain sending key
MAILGUN_ACCOUNT_KEY = os.getenv('MAILGUN_ACCOUNT_API_KEY', '') or MAILGUN_API_KEY  # Account key for domain management
CRON_SECRET = os.getenv('CRON_SECRET', '')  # Used to authenticate auto-reply endpoint

PORKBUN_API_BASE = 'https://api-ipv4.porkbun.com/api/json/v3'
CLOUDFLARE_API_BASE = 'https://api.cloudflare.com/client/v4'

# For Cloudflare Workers: A record and routing are auto-managed by Cloudflare.
# No static IP needed. Worker routes handle traffic routing.


# ═══════════════════════════════════════════════════════════════
# RETRY LOGIC (exponential backoff for transient failures)
# ═══════════════════════════════════════════════════════════════

def _retry_with_backoff(max_attempts=3, backoff_base=2):
    """Decorator for transient error retry with exponential backoff."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_error = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_error = e
                    if attempt < max_attempts:
                        wait_time = backoff_base ** (attempt - 1)
                        logger.warning(f"[Domain] {func.__name__} attempt {attempt} failed: {e}. Retrying in {wait_time}s...")
                        time.sleep(wait_time)
                    else:
                        logger.error(f"[Domain] {func.__name__} failed after {max_attempts} attempts: {e}")
            raise last_error
        return wrapper
    return decorator


# ═══════════════════════════════════════════════════════════════
# VALIDATION HELPERS
# ═══════════════════════════════════════════════════════════════

def _validate_phone(phone_display):
    """Validate phone number has at least 10 digits (US format). Returns normalized or raises."""
    if not phone_display:
        raise ValueError("Phone number required")
    phone_raw = re.sub(r'[^\d+]', '', phone_display)
    # Strip leading + if present, check digit count
    digits_only = re.sub(r'[^\d]', '', phone_raw)
    if len(digits_only) < 10:
        raise ValueError(f"Phone number must be at least 10 digits (got {len(digits_only)})")
    # Return in +1XXXXXXXXXX format if US
    if len(digits_only) == 10:
        return f'+1{digits_only}'
    elif len(digits_only) == 11 and digits_only[0] == '1':
        return f'+{digits_only}'
    else:
        return f'+{digits_only}'


# ═══════════════════════════════════════════════════════════════
# RATE LIMITING (Redis-based sliding window)
# ═══════════════════════════════════════════════════════════════

def _check_rate_limit(key, max_requests, window_seconds):
    """Check rate limit using Redis. Returns (allowed, remaining)."""
    try:
        r = ensure_redis()
        if not r:
            return True, max_requests  # Allow if Redis unavailable
        pipe = r.pipeline()
        now = time.time()
        window_start = now - window_seconds
        pipe.zremrangebyscore(key, 0, window_start)
        pipe.zadd(key, {f'{now}': now})
        pipe.zcard(key)
        pipe.expire(key, window_seconds + 10)
        results = pipe.execute()
        count = results[2]
        return count <= max_requests, max(0, max_requests - count)
    except Exception:
        return True, max_requests


# ═══════════════════════════════════════════════════════════════
# PORKBUN API HELPERS
# ═══════════════════════════════════════════════════════════════

def _porkbun_post(endpoint, extra_data=None):
    """Make authenticated POST to Porkbun API. Parses JSON body even on HTTP errors
    (Porkbun returns useful error messages like RATE_LIMIT_EXCEEDED in the body)."""
    data = {
        'apikey': PORKBUN_API_KEY,
        'secretapikey': PORKBUN_SECRET_KEY,
    }
    if extra_data:
        data.update(extra_data)
    try:
        resp = requests.post(f'{PORKBUN_API_BASE}{endpoint}',
                             json=data, timeout=30)
        # Parse JSON first — Porkbun returns error details in body even on 400
        try:
            result = resp.json()
        except (ValueError, requests.exceptions.JSONDecodeError):
            logger.error(f"[Domain] Porkbun non-JSON response for {endpoint}: {resp.status_code} {resp.text[:200]}")
            return {'status': 'ERROR', 'message': f'Porkbun returned non-JSON response (HTTP {resp.status_code})'}
        # Log non-200 responses with the actual Porkbun error message
        if resp.status_code >= 400:
            msg = result.get('message', resp.text[:200])
            logger.warning(f"[Domain] Porkbun {endpoint} HTTP {resp.status_code}: {msg}")
        return result
    except requests.exceptions.RequestException as e:
        logger.error(f"[Domain] Porkbun request failed for {endpoint}: {e}")
        raise  # Let retry decorator handle network errors


def _porkbun_check_available(domain):
    """Check domain availability AND get current pricing via Porkbun API.
    Endpoint: POST /domain/checkDomain/{domain}
    Response: {status, response: {avail: "yes"/"no", price: "9.73", ...}}
    Returns dict with 'available' (bool) and 'price' (str, USD).
    Never raises — returns unavailable on any error (safe for parallel search)."""
    try:
        result = _porkbun_post(f'/domain/checkDomain/{domain}')
    except Exception as e:
        logger.warning(f"[Domain] Porkbun checkDomain failed for {domain}: {e}")
        return {'available': False, 'price': '11.08'}
    if result.get('status') == 'ERROR':
        logger.warning(f"[Domain] Porkbun checkDomain error for {domain}: {result.get('message')}")
        return {'available': False, 'price': '11.08'}
    resp = result.get('response', {})
    available = resp.get('avail', 'no').lower() == 'yes'
    price = resp.get('price', '11.08')
    return {'available': available, 'price': str(price)}


@_retry_with_backoff(max_attempts=3, backoff_base=12)
def _porkbun_register(domain, contact_info, price_usd=None):
    """Register a .com domain via Porkbun API.
    Endpoint: POST /domain/create/{domain}
    Requires cost (integer pennies) and agreeToTerms.
    Retries up to 3 times with 12s backoff (clears Porkbun's 10s rate limit)."""
    # Get current price if not provided
    if price_usd is None:
        check = _porkbun_check_available(domain)
        price_usd = check.get('price', '11.08')

    # Porkbun API spec: cost is type integer, in pennies (USD cents)
    # e.g. $11.08 → 1108
    try:
        cost_pennies = int(round(float(price_usd) * 100))
    except (ValueError, TypeError):
        cost_pennies = 1108

    data = {
        'cost': cost_pennies,
        'agreeToTerms': 'yes',
        'years': 1,
        'autoRenew': True,
        'whoisPrivacy': True,
    }
    for prefix in ['admin', 'tech', 'billing', 'registrant']:
        data[f'{prefix}FirstName'] = contact_info.get('first_name', '')
        data[f'{prefix}LastName'] = contact_info.get('last_name', '')
        data[f'{prefix}Email'] = contact_info.get('email', '')
        data[f'{prefix}Phone'] = contact_info.get('phone', '')
        data[f'{prefix}Address1'] = contact_info.get('street', '')
        data[f'{prefix}City'] = contact_info.get('city', '')
        data[f'{prefix}StateProvince'] = contact_info.get('state', '')
        data[f'{prefix}PostalCode'] = contact_info.get('zip', '')
        data[f'{prefix}Country'] = 'US'

    result = _porkbun_post(f'/domain/create/{domain}', data)
    if result.get('status') == 'ERROR':
        msg = result.get('message', 'Unknown error')
        code = result.get('code', '')
        # Rate limit is transient — raise so retry decorator fires
        if code == 'RATE_LIMIT_EXCEEDED' or 'rate' in msg.lower():
            raise Exception(f"Porkbun rate limited: {msg}")
        # Other errors — raise so retry can attempt (network blip, etc.)
        raise Exception(f"Porkbun registration failed: {msg}")
    return result


@_retry_with_backoff(max_attempts=4, backoff_base=3)
def _porkbun_set_nameservers(domain, nameservers):
    """Update nameservers for a domain on Porkbun. Returns True on success.
    Retries up to 4 times with exponential backoff (3s, 9s, 27s) for activation timing.
    """
    result = _porkbun_post(f'/domain/updateNs/{domain}', {'ns': nameservers})
    if result.get('status') != 'SUCCESS':
        raise Exception(f"Nameserver update failed: {result.get('message', 'Unknown error')}")
    return True


# ═══════════════════════════════════════════════════════════════
# CLOUDFLARE API HELPERS
# ═══════════════════════════════════════════════════════════════

def _cf_headers():
    return {
        'Authorization': f'Bearer {CLOUDFLARE_API_TOKEN}',
        'Content-Type': 'application/json',
    }


@_retry_with_backoff(max_attempts=3, backoff_base=5)
def _cf_create_zone(domain):
    """Add a domain to Cloudflare as a new zone. Retries on throttle (code 971)."""
    resp = requests.post(
        f'{CLOUDFLARE_API_BASE}/zones',
        headers=_cf_headers(),
        json={
            'name': domain,
            'account': {'id': CLOUDFLARE_ACCOUNT_ID},
            'type': 'full',
        },
        timeout=30,
    )
    data = resp.json()
    if data.get('success'):
        zone = data['result']
        return {
            'zone_id': zone['id'],
            'nameservers': zone.get('name_servers', []),
            'status': zone.get('status', 'pending'),
        }
    errors = data.get('errors', [])
    for err in errors:
        if err.get('code') == 1061:
            zones = requests.get(
                f'{CLOUDFLARE_API_BASE}/zones?name={domain}',
                headers=_cf_headers(), timeout=30
            ).json()
            if zones.get('result'):
                z = zones['result'][0]
                return {
                    'zone_id': z['id'],
                    'nameservers': z.get('name_servers', []),
                    'status': z.get('status', 'active'),
                }
    raise Exception(f"Failed to create Cloudflare zone: {errors}")


def _cf_add_dns_record(zone_id, record_type, name, content, priority=None, proxied=False):
    """Add a DNS record to a Cloudflare zone. Returns True on success."""
    data = {
        'type': record_type,
        'name': name,
        'content': content,
        'proxied': proxied,
    }
    if priority is not None:
        data['priority'] = priority
    resp = requests.post(
        f'{CLOUDFLARE_API_BASE}/zones/{zone_id}/dns_records',
        headers=_cf_headers(),
        json=data,
        timeout=30,
    )
    result = resp.json()
    if not result.get('success'):
        errors = result.get('errors', [])
        # 81057 = record already exists — not an error
        if any(e.get('code') == 81057 for e in errors):
            return True
        logger.warning(f"[Domain] DNS record {record_type} {name} failed: {errors}")
        return False
    return True


def _cf_setup_dns(zone_id, domain):
    """Configure A records for Worker route only.
    Email DNS (MX, SPF, DKIM) is added by Cloudflare when email routing is enabled."""
    _cf_add_dns_record(zone_id, 'A', '@', '192.0.2.1', proxied=True)
    _cf_add_dns_record(zone_id, 'A', 'www', '192.0.2.1', proxied=True)


def _cf_add_mailgun_dns(zone_id, domain, mailgun_dkim_records):
    """After Cloudflare email routing adds its SPF, update it to also include Mailgun
    (needed for outbound sending via Mailgun). Also adds Mailgun DKIM records."""

    # Find and update the existing SPF record to include Mailgun
    try:
        resp = requests.get(
            f'{CLOUDFLARE_API_BASE}/zones/{zone_id}/dns_records?type=TXT&name={domain}',
            headers=_cf_headers(),
            timeout=30,
        )
        data = resp.json()
        for rec in data.get('result', []):
            content = rec.get('content', '')
            if content.startswith('v=spf1') and 'mailgun.org' not in content:
                # Add Mailgun to existing SPF record
                new_spf = content.replace('~all', 'include:mailgun.org ~all')
                requests.put(
                    f'{CLOUDFLARE_API_BASE}/zones/{zone_id}/dns_records/{rec["id"]}',
                    headers=_cf_headers(),
                    json={'type': 'TXT', 'name': '@', 'content': new_spf},
                    timeout=30,
                )
                logger.info(f"[Domain] Updated SPF to include Mailgun: {new_spf}")
                break
        else:
            # No SPF exists yet — create one with both
            _cf_add_dns_record(zone_id, 'TXT', '@',
                               'v=spf1 include:_spf.mx.cloudflare.net include:mailgun.org ~all')
    except Exception as e:
        logger.warning(f"[Domain] SPF update for Mailgun failed: {e}")

    # Add Mailgun DKIM records (different hostname, no conflict)
    if mailgun_dkim_records:
        for rec in mailgun_dkim_records:
            _cf_add_dns_record(zone_id, rec.get('type', 'TXT'), rec['name'], rec['value'])


def _cf_setup_email_routing(zone_id, domain, forward_to):
    """Enable email routing and route through Email Worker.

    Instead of forwarding directly to the agent's personal email, emails
    are routed through our Email Worker (omnisconn-email-handler) which:
      1. Intercepts Twilio verification emails and auto-replies via Mailgun
      2. Forwards everything else to the agent's personal email

    The forward_to address is stored in KV so the multi-tenant worker can
    look up the correct destination per domain.

    Returns dict with email_verification_needed flag."""
    result = {'email_routing_ok': False, 'email_verification_needed': False}
    email_worker_name = os.getenv('CLOUDFLARE_EMAIL_WORKER_NAME', 'omnisconn-email-handler')

    # Enable email routing on the zone (POST, not PUT — per CF API docs)
    try:
        requests.post(
            f'{CLOUDFLARE_API_BASE}/zones/{zone_id}/email/routing/enable',
            headers=_cf_headers(),
            json={'enabled': True},
            timeout=30,
        )
    except Exception as e:
        logger.warning(f"[Domain] Email routing enable failed: {e}")

    # Check if destination is already verified
    verified = _cf_check_email_verified(forward_to)
    if not verified:
        # Send verification — agent will need to click a link
        _cf_add_email_destination(forward_to)
        result['email_verification_needed'] = True
        logger.info(f"[Domain] Email verification sent to {forward_to} — routing will activate after click")

    # Store forward_to in KV so the Email Worker can look it up per domain.
    # Key: email:{domain} → JSON with forward_to address.
    try:
        _cf_store_email_config(domain, {'forward_to': forward_to})
    except Exception as e:
        logger.warning(f"[Domain] KV email config failed: {e}")

    # Create catch-all rule pointing to Email Worker (not direct forward).
    # The worker intercepts Twilio emails for auto-reply, then forwards
    # everything to the agent's personal email via message.forward().
    try:
        resp = requests.post(
            f'{CLOUDFLARE_API_BASE}/zones/{zone_id}/email/routing/rules',
            headers=_cf_headers(),
            json={
                'matchers': [{'type': 'all'}],
                'actions': [{'type': 'worker', 'value': [email_worker_name]}],
                'enabled': True,
                'name': f'Route all through {email_worker_name}',
            },
            timeout=30,
        )
        resp_data = resp.json()
        if resp_data.get('success'):
            result['email_routing_ok'] = True
            logger.info(f"[Domain] Email routing → worker '{email_worker_name}' for {domain}")
        else:
            # Fallback: if worker rule fails (worker not deployed yet),
            # create a direct forward rule so email still works
            errors = resp_data.get('errors', [])
            logger.warning(f"[Domain] Worker rule failed ({errors}), falling back to direct forward")
            resp2 = requests.post(
                f'{CLOUDFLARE_API_BASE}/zones/{zone_id}/email/routing/rules',
                headers=_cf_headers(),
                json={
                    'matchers': [{'type': 'all'}],
                    'actions': [{'type': 'forward', 'value': [forward_to]}],
                    'enabled': True,
                    'name': f'Forward all to {forward_to}',
                },
                timeout=30,
            )
            result['email_routing_ok'] = resp2.json().get('success', False)
    except Exception as e:
        logger.error(f"[Domain] Email routing rule failed: {e}")

    return result


def _cf_store_email_config(domain, config):
    """Store email routing config in Workers KV for the Email Worker to read.

    Key pattern: email:{domain} (separate from the landing page config key {domain}).
    The Email Worker reads this to find the forward_to address for each domain.
    """
    kv_ns_id = os.getenv('CLOUDFLARE_KV_NAMESPACE_ID', '')
    if not kv_ns_id:
        logger.warning("[Domain] CLOUDFLARE_KV_NAMESPACE_ID not set — skipping email KV config")
        return

    resp = requests.put(
        f'{CLOUDFLARE_API_BASE}/accounts/{CLOUDFLARE_ACCOUNT_ID}/storage/kv/namespaces/{kv_ns_id}/values/email:{domain}',
        headers={
            'Authorization': f'Bearer {CLOUDFLARE_API_TOKEN}',
            'Content-Type': 'application/json',
        },
        data=json.dumps(config),
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        logger.error(f"[Domain] KV email config write failed: {resp.status_code} {resp.text[:200]}")
    else:
        logger.info(f"[Domain] Stored email config in KV for {domain}")


def _cf_check_email_verified(email):
    """Check if an email destination is already verified in Cloudflare."""
    try:
        resp = requests.get(
            f'{CLOUDFLARE_API_BASE}/accounts/{CLOUDFLARE_ACCOUNT_ID}/email/routing/addresses',
            headers=_cf_headers(),
            timeout=30,
        )
        data = resp.json()
        for addr in data.get('result', []):
            if addr.get('email', '').lower() == email.lower() and addr.get('verified'):
                return True
        return False
    except Exception:
        return False


def _cf_add_email_destination(email):
    """Add an email destination for routing (sends verification email)."""
    try:
        requests.post(
            f'{CLOUDFLARE_API_BASE}/accounts/{CLOUDFLARE_ACCOUNT_ID}/email/routing/addresses',
            headers=_cf_headers(),
            json={'email': email},
            timeout=30,
        )
    except Exception as e:
        logger.warning(f"[Domain] Email destination add failed: {e}")


def _cf_setup_worker_route(zone_id, domain):
    """Create Worker Route. Raises on failure since landing page depends on it."""
    worker_name = os.getenv('CLOUDFLARE_WORKER_NAME', 'omnisconn-agent-pages')
    try:
        # Root domain route
        resp = requests.post(
            f'{CLOUDFLARE_API_BASE}/zones/{zone_id}/workers/routes',
            headers=_cf_headers(),
            json={'pattern': f'{domain}/*', 'script': worker_name},
            timeout=30,
        )
        result = resp.json()
        if not result.get('success'):
            errors = result.get('errors', [])
            # 10020 = route already exists — ok
            if not any(e.get('code') == 10020 for e in errors):
                raise Exception(f"Worker route failed: {errors}")

        # www subdomain route (with error checking)
        resp_www = requests.post(
            f'{CLOUDFLARE_API_BASE}/zones/{zone_id}/workers/routes',
            headers=_cf_headers(),
            json={'pattern': f'www.{domain}/*', 'script': worker_name},
            timeout=30,
        )
        result_www = resp_www.json()
        if not result_www.get('success'):
            errors_www = result_www.get('errors', [])
            # 10020 = route already exists — ok
            if not any(e.get('code') == 10020 for e in errors_www):
                logger.error(f"[Domain] www Worker route failed: {errors_www}")
                # Non-critical but log it
    except Exception as e:
        logger.error(f"[Domain] Worker route setup failed: {e}")
        raise


def _cf_store_agent_config(domain, config):
    """Store agent config in Workers KV. Raises on failure since landing page needs this."""
    kv_ns_id = os.getenv('CLOUDFLARE_KV_NAMESPACE_ID', '')
    if not kv_ns_id:
        raise Exception("CLOUDFLARE_KV_NAMESPACE_ID not configured")

    resp = requests.put(
        f'{CLOUDFLARE_API_BASE}/accounts/{CLOUDFLARE_ACCOUNT_ID}/storage/kv/namespaces/{kv_ns_id}/values/{domain}',
        headers={
            'Authorization': f'Bearer {CLOUDFLARE_API_TOKEN}',
            'Content-Type': 'application/json',
        },
        data=json.dumps(config),
        timeout=30,
    )
    result = resp.json()
    if not result.get('success'):
        raise Exception(f"KV write failed: {result.get('errors', [])}")


# ═══════════════════════════════════════════════════════════════
# MAILGUN HELPERS
# ═══════════════════════════════════════════════════════════════

def _mailgun_add_domain(domain):
    """Add a sending domain to Mailgun. Uses account key for domain management."""
    try:
        resp = requests.post(
            'https://api.mailgun.net/v3/domains',
            auth=('api', MAILGUN_ACCOUNT_KEY),
            data={'name': domain, 'web_scheme': 'https'},
            timeout=30,
        )
        data = resp.json()
        dkim_records = []
        for rec in data.get('sending_dns_records', []):
            dkim_records.append({
                'type': rec.get('record_type', 'TXT'),
                'name': rec.get('name', ''),
                'value': rec.get('value', ''),
            })
        return {
            'success': resp.status_code in (200, 201),
            'dkim_records': dkim_records,
        }
    except Exception as e:
        logger.error(f"[Domain] Mailgun add domain failed: {e}")
        return {'success': False, 'dkim_records': []}


def _mailgun_send(from_email, to_email, subject, text):
    """Send an email via Mailgun from an agent's domain."""
    domain = from_email.split('@')[1] if '@' in from_email else ''
    if not domain:
        return False
    try:
        resp = requests.post(
            f'https://api.mailgun.net/v3/{domain}/messages',
            auth=('api', MAILGUN_API_KEY),
            data={'from': from_email, 'to': to_email, 'subject': subject, 'text': text},
            timeout=30,
        )
        return resp.status_code == 200
    except Exception as e:
        logger.error(f"[Domain] Mailgun send failed: {e}")
        return False


# ═══════════════════════════════════════════════════════════════
# DOMAIN NAME GENERATION
# ═══════════════════════════════════════════════════════════════

def _generate_domain_suggestions(dba_name, first_name='', last_name=''):
    """Generate .com domain suggestions from a DBA name."""
    clean = dba_name.lower().strip()
    for suffix in ['llc', 'inc', 'corp', 'ltd', 'co', 'company', 'services',
                   'agency', 'group', 'associates', 'insurance agency',
                   'insurance services', 'insurance group']:
        clean = re.sub(rf'\b{re.escape(suffix)}\b', '', clean)
    clean = re.sub(r'[^a-z0-9\s]', '', clean).strip()
    clean = re.sub(r'\s+', '', clean)

    suggestions = []
    if clean:
        suggestions.append(f'{clean}.com')
        suggestions.append(f'{clean}insurance.com')
        suggestions.append(f'{clean}ins.com')
        if len(clean) > 12:
            suggestions.append(f'{clean[:10]}.com')

    if first_name and last_name:
        fn = re.sub(r'[^a-z]', '', first_name.lower())
        ln = re.sub(r'[^a-z]', '', last_name.lower())
        suggestions.append(f'{fn}{ln}insurance.com')
        suggestions.append(f'{fn}{ln}ins.com')

    seen = set()
    unique = []
    for s in suggestions:
        if s not in seen and len(s) > 5:
            seen.add(s)
            unique.append(s)
    return unique[:6]


# ═══════════════════════════════════════════════════════════════
# ROUTES
# ═══════════════════════════════════════════════════════════════

@domain_bp.route('/api/domain/search', methods=['POST'])
@login_required
def domain_search():
    """Search available .com domains based on DBA name."""
    data = request.json or {}
    dba_name = (data.get('dba_name') or '').strip()
    if not dba_name:
        return jsonify({'error': 'Business name (DBA) is required'}), 400

    suggestions = _generate_domain_suggestions(
        dba_name,
        (data.get('first_name') or '').strip(),
        (data.get('last_name') or '').strip(),
    )

    # Parallel domain checks: ~15s instead of 60s
    results = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(_porkbun_check_available, domain): domain for domain in suggestions}
        for future in as_completed(futures):
            domain = futures[future]
            try:
                check = future.result()
                results.append({
                    'domain': domain,
                    'available': check['available'],
                    'price_yearly': check['price'],
                })
            except Exception as e:
                logger.warning(f"[Domain] Domain check failed for {domain}: {e}")
                results.append({
                    'domain': domain,
                    'available': False,
                    'price_yearly': '11.08',
                })

    # Sort by availability and price for better UX
    results.sort(key=lambda x: (not x['available'], float(x['price_yearly'])))
    return jsonify({'suggestions': results})


@domain_bp.route('/api/domain/validate-promo', methods=['POST'])
@login_required
def validate_domain_promo():
    """Validate a promotion code and return discount details."""
    import stripe
    data = request.json or {}
    code = (data.get('code') or '').strip()
    if not code:
        return jsonify({'valid': False, 'error': 'Enter a promo code'}), 400

    try:
        promos = stripe.PromotionCode.list(code=code, active=True, limit=1)
        if not promos.data:
            return jsonify({'valid': False, 'error': 'Invalid or expired code'})

        promo = promos.data[0]
        coupon = promo.coupon
        discount_text = ''
        if coupon.percent_off:
            discount_text = f'{int(coupon.percent_off)}% off'
        elif coupon.amount_off:
            discount_text = f'${coupon.amount_off / 100:.0f} off'
        if coupon.duration == 'once':
            discount_text += ' (first month)'
        elif coupon.duration == 'repeating' and coupon.duration_in_months:
            discount_text += f' (first {coupon.duration_in_months} months)'
        elif coupon.duration == 'forever':
            discount_text += ' (forever)'

        # Calculate price after discount (base = $10/mo = 1000 cents)
        base_cents = 1000
        if coupon.percent_off:
            after_cents = int(base_cents * (1 - coupon.percent_off / 100))
        elif coupon.amount_off:
            after_cents = max(0, base_cents - coupon.amount_off)
        else:
            after_cents = base_cents
        price_dollar = after_cents / 100
        price_after = '$0' if after_cents == 0 else (f'${price_dollar:.0f}' if price_dollar == int(price_dollar) else f'${price_dollar:.2f}')

        return jsonify({
            'valid': True,
            'discount': discount_text,
            'coupon_name': coupon.name or code,
            'price_after': price_after,
        })
    except Exception as e:
        logger.warning(f"[Domain] Promo validation error: {e}")
        return jsonify({'valid': False, 'error': 'Could not validate code'}), 500


@domain_bp.route('/api/domain/checkout', methods=['POST'])
@login_required
def domain_checkout():
    """Register domain, provision everything, THEN create Stripe subscription.
    If provisioning fails, user is never charged."""
    import stripe
    data = request.json or {}

    domain = (data.get('domain') or '').strip().lower()
    dba_name = (data.get('dba_name') or '').strip()
    email_prefix = (data.get('email_prefix') or '').strip().lower() or 'info'
    forward_to = (data.get('forward_to') or current_user.email or '').strip()
    bio = (data.get('bio') or '').strip()
    phone_display = (data.get('phone_display') or '').strip()
    licensed_states = data.get('licensed_states', [])
    disclaimer_accepted = data.get('disclaimer_accepted', False)
    promo_code = (data.get('promo_code') or '').strip()

    # ── Validation ──
    if not domain or not domain.endswith('.com'):
        return jsonify({'error': 'A valid .com domain is required'}), 400
    if not dba_name:
        return jsonify({'error': 'Business name (DBA) is required'}), 400
    if not disclaimer_accepted:
        return jsonify({'error': 'You must accept the disclaimer to proceed'}), 400
    if not forward_to:
        return jsonify({'error': 'A forwarding email address is required'}), 400
    if not phone_display:
        return jsonify({'error': 'A display phone number is required'}), 400

    # Phone validation BEFORE proceeding
    try:
        phone_raw = _validate_phone(phone_display)
    except ValueError as e:
        return jsonify({'error': f'Invalid phone: {e}'}), 400

    # Require real address for ICANN compliance
    street = (data.get('street') or '').strip()
    city = (data.get('city') or '').strip()
    state = (data.get('state') or '').strip()
    zip_code = (data.get('zip') or '').strip()
    if not street or not city or not state or not zip_code:
        return jsonify({'error': 'A physical address is required for domain registration (ICANN requirement). PO Boxes are not accepted.'}), 400

    # ── Get subscriber info ──
    conn = get_db_connection()
    if not conn:
        return jsonify({'error': 'Database unavailable'}), 503
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id, voice_config, email, stripe_customer_id FROM subscribers WHERE email = %s",
                    (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return jsonify({'error': 'Subscriber not found'}), 404
        location_id = row['location_id']
        vc = row['voice_config'] or {}
        subscriber_email = row['email']
        customer_id = row['stripe_customer_id'] or ''
    finally:
        return_db_connection(conn)

    if not customer_id:
        return jsonify({'error': 'No Stripe customer found. Subscribe to a plan first.'}), 400

    existing = vc.get('web_presence', {})
    if existing.get('domain') and existing.get('status') == 'active':
        return jsonify({'error': f'You already have a domain: {existing["domain"]}'}), 400

    agent_name = data.get('agent_name') or vc.get('operator_name') or current_user.email.split('@')[0].title()
    first_name = agent_name.split()[0] if ' ' in agent_name else agent_name
    last_name = agent_name.split()[-1] if ' ' in agent_name else ''
    agent_email = f'{email_prefix}@{domain}'

    # Persist agent name to subscriber record if not already set
    if data.get('agent_name'):
        try:
            conn2 = get_db_connection()
            if conn2:
                cur2 = conn2.cursor()
                cur2.execute("""
                    UPDATE subscribers
                    SET full_name = COALESCE(NULLIF(full_name, ''), %s),
                        voice_config = jsonb_set(
                            COALESCE(voice_config, '{}'::jsonb),
                            '{operator_name}',
                            to_jsonb(%s::text)
                        )
                    WHERE email = %s AND (full_name IS NULL OR full_name = '')
                """, (data['agent_name'], data['agent_name'], current_user.email))
                conn2.commit()
                cur2.close()
                return_db_connection(conn2)
        except Exception as e:
            logger.warning(f"[Domain] Failed to update subscriber name: {e}")
    legal_business_name = (data.get('legal_business_name') or '').strip()  # Optional: legal entity name

    # ── Check if this domain was previously registered but provisioning failed ──
    # If so, skip availability check and resume from the failed step
    existing = vc.get('web_presence', {})
    resume_from_step = None
    if existing.get('domain') == domain and existing.get('status') == 'error':
        # We already own this domain (from a previous failed attempt)
        # Skip availability check and resume from the step that failed
        resume_from_step = len(existing.get('provisioning_log', []))
        logger.info(f"[Domain] Resuming {domain} from step {resume_from_step} (previous attempt failed)")

    # ── Validate promo code BEFORE provisioning (prevent free domains) ──
    promo_code_id = None
    if promo_code:
        try:
            promos = stripe.PromotionCode.list(code=promo_code, active=True, limit=1)
            if not promos.data:
                return jsonify({
                    'status': 'promo_invalid',
                    'error': f'Promo code "{promo_code}" is invalid or expired.'
                }), 400
            promo_code_id = promos.data[0].id
            logger.info(f"[Domain] Validated promo code: {promo_code}")
        except Exception as promo_err:
            logger.error(f"[Domain] Promo code validation failed: {promo_err}")
            return jsonify({
                'status': 'promo_invalid',
                'error': f'Could not validate promo code. Error: {promo_err}'
            }), 400

    # ── Step 0: Check domain is STILL available (race condition guard) ──
    # UNLESS we're resuming from a previous failed attempt (we already own it)
    if resume_from_step is None:
        # First attempt: check availability
        avail_check = _porkbun_check_available(domain)
        if not avail_check.get('available'):
            return jsonify({'error': f'{domain} is no longer available. Please search again.'}), 409
    else:
        # Resuming: we already own the domain, skip availability check
        # Use the price from the previous attempt
        avail_check = existing.get('avail_check', {'available': True, 'price': '11.08'})

    # ── Provision FIRST, charge AFTER ──
    provisioning_log = []

    # Initialize web_presence (merge with existing if resuming)
    if resume_from_step is not None:
        # Resuming from previous attempt: preserve existing data
        web_presence = existing.copy()
        web_presence['status'] = 'provisioning'  # Reset status while retrying
        web_presence['provisioned_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    else:
        # First attempt: create new web_presence
        web_presence = {
            'domain': domain,
            'dba_name': dba_name,
            'legal_business_name': legal_business_name,  # For A2P DBA formatting
            'agent_name': agent_name,
            'email': agent_email,
            'email_prefix': email_prefix,
            'email_forward_to': forward_to,
            'phone_display': phone_display,
            'phone_raw': phone_raw,
            'licensed_states': licensed_states,
            'bio': bio,
            'status': 'provisioning',
            'disclaimer_accepted': True,
            'disclaimer_accepted_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'provisioned_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        }

    # Save availability check for potential resume
    web_presence['avail_check'] = avail_check

    try:
        # Restore provisioning log from previous attempt if resuming
        if resume_from_step is not None:
            provisioning_log = existing.get('provisioning_log', [])

        # Step 1: Register domain on Porkbun (SKIP if already registered)
        if 'domain_registered' not in provisioning_log:
            contact = {
                'first_name': first_name,
                'last_name': last_name,
                'email': subscriber_email,
                'phone': phone_raw,  # Already validated and formatted as +1XXXXXXXXXX
                'street': street,
                'city': city,
                'state': state,
                'zip': zip_code,
            }
            reg_result = _porkbun_register(domain, contact, price_usd=avail_check.get('price'))
            if reg_result.get('status') != 'SUCCESS':
                raise Exception(f"Domain registration failed: {reg_result.get('message', 'Unknown error')}")
            provisioning_log.append('domain_registered')
            logger.info(f"[Domain] Registered {domain} via Porkbun")
        else:
            logger.info(f"[Domain] Skipping registration for {domain} (already registered in previous attempt)")

        # Step 2: Add zone to Cloudflare (SKIP if already created)
        if 'zone_created' not in provisioning_log:
            zone = _cf_create_zone(domain)
            web_presence['cloudflare_zone_id'] = zone['zone_id']
            web_presence['cloudflare_nameservers'] = zone.get('nameservers', [])
            provisioning_log.append('zone_created')
        else:
            logger.info(f"[Domain] Skipping zone creation for {domain} (already created)")
            # Restore zone ID from previous attempt
            zone = {
                'zone_id': web_presence.get('cloudflare_zone_id'),
                'nameservers': web_presence.get('cloudflare_nameservers', [])
            }
            if not zone['zone_id']:
                raise Exception(f"Zone ID not found for {domain} in previous attempt")

        # Step 3: Update nameservers (idempotent — safe to re-run)
        if 'nameservers_updated' not in provisioning_log:
            if zone.get('nameservers'):
                _porkbun_set_nameservers(domain, zone['nameservers'])
                provisioning_log.append('nameservers_updated')
        else:
            logger.info(f"[Domain] Skipping nameserver update for {domain} (already done)")

        # Step 4: Configure DNS (A records for Worker routes only)
        if 'dns_configured' not in provisioning_log:
            _cf_setup_dns(zone['zone_id'], domain)
            provisioning_log.append('dns_configured')
        else:
            logger.info(f"[Domain] Skipping DNS config for {domain} (already done)")

        # Step 5: Enable Cloudflare email routing (adds MX, SPF, DKIM automatically)
        if 'email_routing_configured' not in provisioning_log:
            email_result = _cf_setup_email_routing(zone['zone_id'], domain, forward_to)
            web_presence['email_verification_needed'] = email_result.get('email_verification_needed', False)
            provisioning_log.append('email_routing_configured')
        else:
            logger.info(f"[Domain] Skipping email routing for {domain} (already done)")

        # Step 6: Register domain with Mailgun (for outbound sending / auto-reply)
        # Then update Cloudflare SPF to include Mailgun + add Mailgun DKIM records
        if 'mailgun_domain_added' not in provisioning_log:
            mg_result = _mailgun_add_domain(domain)
            web_presence['mailgun_domain_added'] = mg_result.get('success', False)
            # Update SPF to include both Cloudflare + Mailgun, add DKIM
            _cf_add_mailgun_dns(zone['zone_id'], domain, mg_result.get('dkim_records', []))
            provisioning_log.append('mailgun_domain_added')
        else:
            logger.info(f"[Domain] Skipping Mailgun setup for {domain} (already done)")

        # Step 7: Worker route (handles duplicates via Cloudflare error code 10020)
        if 'worker_route_created' not in provisioning_log:
            _cf_setup_worker_route(zone['zone_id'], domain)
            provisioning_log.append('worker_route_created')
        else:
            logger.info(f"[Domain] Skipping worker route for {domain} (already done)")

        # Step 8: Store KV config (PUT = upsert, safe to re-run)
        if 'kv_config_stored' not in provisioning_log:
            accent = vc.get('whitelabel_config', {}).get('accent_color', '#1a6b4a')
            _cf_store_agent_config(domain, {
                'agent_name': agent_name,
                'dba_name': dba_name,
                'legal_business_name': legal_business_name,
                'phone_display': phone_display,
                'phone_raw': phone_raw,
                'email': agent_email,
                'licensed_states': licensed_states,
                'bio': bio,
                'accent_color': accent,
                'location_id': location_id,
            })
            provisioning_log.append('kv_config_stored')
        else:
            logger.info(f"[Domain] Skipping KV config for {domain} (already done)")

    except Exception as e:
        logger.error(f"[Domain] Provisioning failed at step {len(provisioning_log)}: {e}", exc_info=True)
        web_presence['status'] = 'error'
        web_presence['error'] = str(e)
        web_presence['provisioning_log'] = provisioning_log
        # Save partial progress
        _save_web_presence(location_id, web_presence)
        log_webhook_event(location_id, 'domain_provisioning_failed', 'error',
                          f"Domain {domain} failed: {e}",
                          details={'domain': domain, 'steps': provisioning_log, 'error': str(e)})
        return jsonify({
            'status': 'error',
            'error': str(e),
            'provisioning_log': provisioning_log,
            'message': f'Provisioning failed at step: {provisioning_log[-1] if provisioning_log else "start"}. '
                       f'You have NOT been charged. Please try again or contact support.',
        }), 500

    # ── All provisioning succeeded — NOW charge the customer ──
    try:
        stripe_price_id = os.getenv('STRIPE_DOMAIN_PRICE_ID', '')
        if not stripe_price_id:
            logger.error("[Domain] STRIPE_DOMAIN_PRICE_ID not configured")
            # Domain is live but billing not set up — still mark as active
            web_presence['billing_note'] = 'Stripe price not configured — domain active without billing'
        else:
            sub_kwargs = {
                'customer': customer_id,
                'items': [{'price': stripe_price_id}],
                'metadata': {'type': 'domain', 'domain': domain, 'location_id': location_id},
            }

            # Promo code was already validated BEFORE provisioning
            if promo_code_id:
                sub_kwargs['promotion_code'] = promo_code_id
                web_presence['promo_code_applied'] = promo_code
                logger.info(f"[Domain] Applied promo code: {promo_code}")

            subscription = stripe.Subscription.create(**sub_kwargs)
            web_presence['stripe_subscription_id'] = subscription.id
            provisioning_log.append('stripe_subscription_created')
    except Exception as e:
        logger.error(f"[Domain] Stripe subscription failed (domain is live): {e}")
        web_presence['billing_error'] = str(e)
        # Domain is provisioned and working — billing can be retried

    web_presence['status'] = 'active'
    web_presence['landing_page_published'] = True
    web_presence['provisioning_log'] = provisioning_log

    _save_web_presence(location_id, web_presence)

    # Auto-populate Business Profile with domain website + email so the
    # user doesn't have to re-enter them in the Spam Protection form.
    try:
        _populate_trust_hub_from_domain(location_id, domain, agent_email)
    except Exception as e:
        logger.warning(f"[Domain] Failed to populate trust_hub from domain: {e}")

    log_webhook_event(location_id, 'domain_provisioned', 'success',
                      f"Domain {domain} is live",
                      details={'domain': domain, 'steps': provisioning_log})

    response = {
        'status': 'active',
        'domain': domain,
        'email': agent_email,
        'website': f'https://{domain}',
        'provisioning_log': provisioning_log,
        'email_verification_needed': web_presence.get('email_verification_needed', False),
    }
    if web_presence.get('email_verification_needed'):
        response['email_notice'] = (
            f'Email forwarding requires verification. Check {forward_to} for a '
            f'verification email from Cloudflare and click the link to activate forwarding.'
        )
    return jsonify(response)


def _resume_from_failed_step(existing_presence, failed_step_index):
    """Helper to determine which provisioning step to resume from.

    Args:
        existing_presence: dict with 'provisioning_log' of completed steps
        failed_step_index: index of the step that will be retried

    Returns:
        step_to_resume_from: integer index (0-based)
    """
    log = existing_presence.get('provisioning_log', [])
    # Resume from the step that failed (retry it)
    return failed_step_index


def _save_web_presence(location_id, web_presence):
    """Save web_presence to voice_config JSONB using merge (not overwrite)."""
    conn = get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE subscribers
            SET voice_config = COALESCE(voice_config, '{}'::jsonb) || %s::jsonb
            WHERE location_id = %s
        """, (json.dumps({'web_presence': web_presence}), location_id))
        conn.commit()
        cur.close()
    except Exception as e:
        logger.error(f"[Domain] DB save failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        return_db_connection(conn)


def _populate_trust_hub_from_domain(location_id, domain, business_email):
    """Auto-populate trust_hub website + contact_email from a purchased domain.

    When a user buys a domain through us, we already know their website URL
    and business email — write them to trust_hub so the Business Profile form
    shows them pre-filled instead of making the user type them again.
    Only fills empty fields — never overwrites user-entered data.
    """
    conn = get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("SELECT voice_config FROM subscribers WHERE location_id = %s FOR UPDATE",
                    (location_id,))
        row = cur.fetchone()
        if not row:
            return
        vc = row['voice_config'] or {}
        trust_hub = vc.get('trust_hub', {})

        changed = False
        website_url = f'https://{domain}'
        if not trust_hub.get('website', '').strip():
            trust_hub['website'] = website_url
            changed = True
        if not trust_hub.get('contact_email', '').strip() and business_email:
            trust_hub['contact_email'] = business_email
            changed = True

        if changed:
            vc['trust_hub'] = trust_hub
            cur.execute("UPDATE subscribers SET voice_config = %s WHERE location_id = %s",
                        (json.dumps(vc), location_id))
            conn.commit()
            logger.info(f"[Domain] Auto-populated trust_hub: website={website_url}, email={business_email}")
        else:
            logger.info(f"[Domain] trust_hub already has website/email — skipping auto-populate")
    except Exception as e:
        logger.error(f"[Domain] trust_hub auto-populate failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        return_db_connection(conn)


@domain_bp.route('/api/domain/status')
@login_required
def domain_status():
    """Get current domain provisioning status."""
    conn = get_db_connection()
    if not conn:
        return jsonify({'error': 'Database unavailable'}), 503
    try:
        cur = conn.cursor()
        cur.execute("SELECT voice_config FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return jsonify({'error': 'Not found'}), 404
        vc = row['voice_config'] or {}
        wp = vc.get('web_presence', {})
        if not wp:
            return jsonify({'has_domain': False})
        return jsonify({
            'has_domain': True,
            'domain': wp.get('domain', ''),
            'email': wp.get('email', ''),
            'status': wp.get('status', 'unknown'),
            'dba_name': wp.get('dba_name', ''),
            'website': f'https://{wp["domain"]}' if wp.get('domain') else '',
            'provisioned_at': wp.get('provisioned_at', ''),
            'email_verification_needed': wp.get('email_verification_needed', False),
            'error': wp.get('error'),
            # Editable fields for Domain & Website tab
            'agent_name': wp.get('agent_name', ''),
            'phone_display': wp.get('phone_display', ''),
            'licensed_states': wp.get('licensed_states', []),
            'bio': wp.get('bio', ''),
            'email_forward_to': wp.get('email_forward_to', ''),
        })
    finally:
        return_db_connection(conn)


@domain_bp.route('/api/domain/update-page', methods=['POST'])
@login_required
def domain_update_page():
    """Update landing page content (bio, phone, licensed states).
    Uses JSONB merge to avoid overwriting concurrent voice_config changes."""
    data = request.json or {}

    conn = get_db_connection()
    if not conn:
        return jsonify({'error': 'Database unavailable'}), 503
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id, voice_config FROM subscribers WHERE email = %s",
                    (current_user.email,))
        row = cur.fetchone()
        if not row:
            return jsonify({'error': 'Not found'}), 404
        location_id = row['location_id']
        vc = row['voice_config'] or {}
        wp = vc.get('web_presence', {})
        if not wp.get('domain'):
            return jsonify({'error': 'No domain registered'}), 400

        if 'bio' in data:
            wp['bio'] = data['bio'].strip()
        if 'phone_display' in data:
            wp['phone_display'] = data['phone_display'].strip()
            wp['phone_raw'] = re.sub(r'[^+\d]', '', wp['phone_display'])
        if 'licensed_states' in data:
            wp['licensed_states'] = data['licensed_states']
        if 'agent_name' in data:
            wp['agent_name'] = data['agent_name'].strip()

        # JSONB merge — doesn't overwrite other voice_config fields
        cur.execute("""
            UPDATE subscribers
            SET voice_config = COALESCE(voice_config, '{}'::jsonb) || %s::jsonb
            WHERE location_id = %s
        """, (json.dumps({'web_presence': wp}), location_id))
        conn.commit()
        cur.close()

        # Update KV
        try:
            accent = vc.get('whitelabel_config', {}).get('accent_color', '#1a6b4a')
            _cf_store_agent_config(wp['domain'], {
                'agent_name': wp.get('agent_name', ''),
                'dba_name': wp.get('dba_name', ''),
                'phone_display': wp.get('phone_display', ''),
                'phone_raw': wp.get('phone_raw', ''),
                'email': wp.get('email', ''),
                'licensed_states': wp.get('licensed_states', []),
                'bio': wp.get('bio', ''),
                'accent_color': accent,
                'location_id': location_id,
            })
        except Exception as kv_err:
            logger.warning(f"[Domain] KV update failed (page saved to DB): {kv_err}")

        return jsonify({'status': 'ok', 'message': 'Landing page updated'})
    except Exception as e:
        logger.error(f"[Domain] Update page failed: {e}")
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        return_db_connection(conn)


@domain_bp.route('/api/domain/contact-form', methods=['POST'])
def domain_contact_form():
    """Public endpoint — receives lead form submissions from agent landing pages.
    Rate-limited: 5 per IP per hour, 20 per domain per hour."""
    # ── Rate limiting ──
    ip = request.headers.get('CF-Connecting-IP') or request.remote_addr or 'unknown'
    allowed_ip, _ = _check_rate_limit(f'domain_form_ip:{ip}', 5, 3600)
    if not allowed_ip:
        return jsonify({'error': 'Too many submissions. Please try again later.'}), 429

    data = request.json or {}
    domain = data.get('domain', '')
    if domain:
        allowed_domain, _ = _check_rate_limit(f'domain_form_domain:{domain}', 20, 3600)
        if not allowed_domain:
            return jsonify({'error': 'Too many submissions.'}), 429

    location_id = data.get('location_id', '')
    first_name = data.get('first_name', '').strip()
    last_name = data.get('last_name', '').strip()
    phone = data.get('phone', '').strip()
    email = data.get('email', '').strip()
    sms_consent = data.get('sms_consent', False)

    if not location_id or not first_name or not phone:
        return jsonify({'error': 'Missing required fields'}), 400

    # ── Store the lead using correct contact_cache schema ──
    conn = get_db_connection()
    if not conn:
        return jsonify({'status': 'ok'})
    try:
        cur = conn.cursor()
        contact_id = f'web_{hashlib.md5(f"{phone}{time.time()}".encode()).hexdigest()[:12]}'
        cur.execute("""
            INSERT INTO contact_cache (location_id, contact_id, name, first_name, last_name,
                                       phone, email, tags, date_added, synced_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (location_id, contact_id) DO NOTHING
        """, (
            location_id,
            contact_id,
            f'{first_name} {last_name}'.strip(),
            first_name,
            last_name,
            phone,
            email,
            json.dumps([
                'web_lead',
                'sms_consent' if sms_consent else 'no_sms_consent',
            ]),
            time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        ))
        conn.commit()
        cur.close()

        # Log consent data separately in webhook_logs for TCPA compliance
        log_webhook_event(location_id, 'web_lead', 'success',
                          f'Web lead: {first_name} {last_name} ({phone})',
                          details={
                              'source': 'web_form',
                              'domain': domain,
                              'sms_consent': sms_consent,
                              'consent_text': data.get('consent_text', ''),
                              'consent_timestamp': data.get('consent_timestamp', ''),
                              'consent_ip': ip,
                              'consent_page': data.get('consent_page', ''),
                              'contact_id': contact_id,
                          })

    except Exception as e:
        logger.error(f"[Domain] Contact form save failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        return_db_connection(conn)

    return jsonify({'status': 'ok'})


@domain_bp.route('/api/domain/auto-reply', methods=['POST'])
def domain_auto_reply():
    """Internal endpoint — called by Email Worker to auto-reply to Twilio verification.
    Authenticated via CRON_SECRET to prevent abuse."""
    # ── Authentication ──
    auth = request.headers.get('Authorization', '')
    secret = request.args.get('key', '')
    if not CRON_SECRET:
        return jsonify({'error': 'Auto-reply not configured'}), 503
    if auth != f'Bearer {CRON_SECRET}' and secret != CRON_SECRET:
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.json or {}
    from_email = data.get('from_email', '')
    to_email = data.get('to_email', '')
    subject = data.get('subject', '')
    domain = data.get('domain', '')

    if not from_email or not to_email or not domain:
        return jsonify({'error': 'Missing fields'}), 400

    reply_subject = f'Re: {subject}' if subject else 'Email Verification Confirmation'
    reply_body = (
        f'This email confirms that {to_email} is a valid and monitored email address.\n\n'
        f'Thank you,\n{domain}'
    )

    success = _mailgun_send(
        from_email=to_email,
        to_email=from_email,
        subject=reply_subject,
        text=reply_body,
    )

    if success:
        logger.info(f"[Domain] Auto-replied to {from_email} from {to_email}")
        return jsonify({'status': 'ok', 'replied': True})
    else:
        logger.error(f"[Domain] Auto-reply failed: {to_email} → {from_email}")
        return jsonify({'status': 'error', 'replied': False}), 500


# ═══════════════════════════════════════════════════════════════
# PAGE BUILDER — Section Config, AI Generation, Photos, Reviews
# ═══════════════════════════════════════════════════════════════

def _default_faq_items():
    """Pre-written FAQ items for insurance agents."""
    return [
        {'q': 'How much does life insurance cost?',
         'a': 'Life insurance costs vary based on your age, health, coverage amount, and policy type. Many term life policies start at $20-30 per month. I can provide a personalized quote based on your specific situation.',
         'visible': True},
        {'q': 'Do I need a medical exam?',
         'a': 'Not always. Many carriers offer no-exam policies with simplified underwriting. The options available depend on your age, health history, and coverage amount.',
         'visible': True},
        {'q': 'How long does it take to get approved?',
         'a': 'Approval timelines range from 24 hours for simplified-issue policies to 4-6 weeks for fully underwritten policies that require a medical exam.',
         'visible': True},
        {'q': 'Can I change my policy later?',
         'a': 'Yes. Most term policies include a conversion option that lets you switch to permanent coverage without a new medical exam. You can also adjust coverage amounts at renewal.',
         'visible': True},
        {'q': 'What happens if I miss a payment?',
         'a': 'Most policies include a 30-day grace period. If you miss a payment, your coverage continues during that window. After the grace period, reinstatement options are typically available within 3-5 years.',
         'visible': True},
        {'q': 'Is my information secure?',
         'a': 'Absolutely. Your personal information is encrypted and never sold or shared with third parties. I follow strict privacy guidelines to protect your data.',
         'visible': True},
    ]


def _default_sections():
    """Return the default section structure for a new page builder config."""
    return [
        {'type': 'hero', 'enabled': True, 'order': 0},
        {'type': 'about', 'enabled': False, 'order': 1, 'content': ''},
        {'type': 'services', 'enabled': False, 'order': 2, 'content': '', 'service_types': []},
        {'type': 'why_me', 'enabled': False, 'order': 3, 'content': '', 'value_props': []},
        {'type': 'carriers', 'enabled': False, 'order': 4},
        {'type': 'testimonials', 'enabled': False, 'order': 5, 'items': []},
        {'type': 'faq', 'enabled': False, 'order': 6, 'items': _default_faq_items()},
        {'type': 'contact_form', 'enabled': True, 'order': 7},
        {'type': 'footer', 'enabled': True, 'order': 8},
    ]


def _cf_get_agent_config(domain):
    """Read agent config from Workers KV."""
    kv_ns_id = os.getenv('CLOUDFLARE_KV_NAMESPACE_ID', '')
    if not kv_ns_id:
        return None
    try:
        resp = requests.get(
            f'{CLOUDFLARE_API_BASE}/accounts/{CLOUDFLARE_ACCOUNT_ID}/storage/kv/namespaces/{kv_ns_id}/values/{domain}',
            headers={'Authorization': f'Bearer {CLOUDFLARE_API_TOKEN}'},
            timeout=30,
        )
        if resp.status_code == 200:
            return resp.json()
        return None
    except Exception as e:
        logger.warning(f"[Domain] KV read failed for {domain}: {e}")
        return None


_SECTION_AI_PROMPTS = {
    'about': {
        'system': (
            "You are a professional copywriter for insurance agent websites. "
            "Write a warm, trustworthy 2-3 paragraph bio (~150-200 words) for a life insurance agent. "
            "Use their name naturally. No AI slop — no 'passionate about', 'dedicated to excellence', "
            "'committed to providing'. Write like a real person, not a LinkedIn profile. "
            "Tone: confident, approachable, human. End with something that invites contact."
        ),
        'questions': [
            {'key': 'experience', 'q': 'How long have you been in insurance?',
             'options': ['1-3 years', '3-10 years', '10+ years']},
            {'key': 'motivation', 'q': 'What got you into insurance?',
             'options': ['Help families', 'Financial freedom', 'Career change', 'Family business']},
            {'key': 'specialty', 'q': "What's your specialty?",
             'options': ['Final expense', 'Term life', 'IUL', 'Medicare', 'Retirement planning']},
            {'key': 'differentiator', 'q': 'What makes you different from other agents?',
             'options': ['Always available', 'Education-first approach', 'Local presence', 'Bilingual']},
            {'key': 'approach', 'q': 'How do you work with clients?',
             'options': ['No-pressure', 'Consultative', 'Fast & efficient', 'Long-term relationship']},
        ],
    },
    'services': {
        'system': (
            "You are a professional copywriter for insurance agent websites. "
            "Write a concise services overview (1 paragraph intro + a bulleted list of services with "
            "one-line descriptions). Professional but warm tone. No fluff. "
            "Each service bullet: service name in bold, then a clear one-sentence description of what it is "
            "and who it's for. Max 6 services."
        ),
        'questions': [
            {'key': 'coverage_types', 'q': 'What types of coverage do you offer?',
             'options': ['Term Life', 'Whole Life', 'IUL', 'Final Expense', 'Annuities', 'Medicare', 'Group Benefits'],
             'multi': True},
            {'key': 'clients', 'q': 'Who are your typical clients?',
             'options': ['Young families', 'Seniors', 'Business owners', 'Middle-income earners']},
            {'key': 'key_message', 'q': "What's the most important thing clients should know about coverage?",
             'options': []},
        ],
    },
    'why_me': {
        'system': (
            "You are a professional copywriter for insurance agent websites. "
            "Write exactly 3 or 4 value proposition cards. Each card has: "
            "a short headline (3-5 words), and a 1-2 sentence description. "
            "Return as JSON array: [{\"headline\": \"...\", \"description\": \"...\"}]. "
            "No AI slop. Confident, specific, human tone. "
            "Based on the agent's actual strengths, not generic insurance platitudes."
        ),
        'questions': [
            {'key': 'appreciation', 'q': 'What do clients appreciate most about working with you?',
             'options': ['Responsiveness', 'Clear explanations', 'No pressure', 'Competitive rates']},
            {'key': 'credentials', 'q': 'Any credentials or achievements?',
             'options': ['MDRT', 'Top producer', 'Certifications', '100+ families helped']},
            {'key': 'promise', 'q': "What's your promise to clients?",
             'options': []},
        ],
    },
}


@domain_bp.route('/api/domain/sections', methods=['GET'])
@login_required
def get_sections():
    """Get current section config for the layout editor."""
    conn = get_db_connection()
    if not conn:
        return jsonify({'error': 'Database unavailable'}), 503
    try:
        cur = conn.cursor()
        cur.execute("SELECT voice_config FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return jsonify({'error': 'Not found'}), 404
        vc = row['voice_config'] or {}
        wp = vc.get('web_presence', {})
        if not wp.get('domain'):
            return jsonify({'error': 'No domain provisioned'}), 400

        # Read existing KV config via Cloudflare API
        domain = wp['domain']
        config = _cf_get_agent_config(domain)
        if not config:
            return jsonify({'error': 'KV config not found'}), 404

        # Return sections (or default structure if none exist yet)
        sections = config.get('sections', _default_sections())
        return jsonify({
            'domain': domain,
            'sections': sections,
            'photo_url': config.get('photo_url', ''),
            'agent_name': config.get('agent_name', ''),
            'phone_display': config.get('phone_display', ''),
            'dba_name': config.get('dba_name', ''),
            'carriers': config.get('carriers', []),
            'review_page_enabled': config.get('review_page_enabled', False),
        })
    finally:
        return_db_connection(conn)


@domain_bp.route('/api/domain/sections', methods=['POST'])
@login_required
def save_sections():
    """Save section order, toggles, and content to KV."""
    conn = get_db_connection()
    if not conn:
        return jsonify({'error': 'Database unavailable'}), 503
    try:
        cur = conn.cursor()
        cur.execute("SELECT voice_config FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return jsonify({'error': 'Not found'}), 404
        vc = row['voice_config'] or {}
        wp = vc.get('web_presence', {})
        domain = wp.get('domain', '')
        if not domain:
            return jsonify({'error': 'No domain provisioned'}), 400

        data = request.json or {}
        sections = data.get('sections', [])

        # Validate section structure
        valid_types = {'hero', 'about', 'services', 'why_me', 'carriers',
                       'testimonials', 'faq', 'contact_form', 'footer'}
        for s in sections:
            if s.get('type') not in valid_types:
                return jsonify({'error': f"Invalid section type: {s.get('type')}"}), 400

        # Read current KV config, merge sections in, write back
        config = _cf_get_agent_config(domain) or {}
        config['sections'] = sections
        if 'review_page_enabled' in data:
            config['review_page_enabled'] = bool(data['review_page_enabled'])

        try:
            _cf_store_agent_config(domain, config)
        except Exception as e:
            logger.error(f"[PageBuilder] KV write failed for {domain}: {e}")
            return jsonify({'error': 'Failed to save layout. Please try again.'}), 500
        return jsonify({'status': 'ok'})
    finally:
        return_db_connection(conn)


@domain_bp.route('/api/domain/section-ai', methods=['POST'])
@login_required
def generate_section_ai():
    """Generate AI copy for a section using xAI Grok."""
    from llm_caller import generate_clean_reply
    from tasks import client as xai_client

    data = request.json or {}
    section_type = data.get('section_type', '')
    answers = data.get('answers', {})

    if section_type not in _SECTION_AI_PROMPTS:
        return jsonify({'error': f'No AI support for section: {section_type}'}), 400

    if not xai_client:
        return jsonify({'error': 'AI service unavailable'}), 503

    prompt_config = _SECTION_AI_PROMPTS[section_type]

    # Get agent context for personalization
    conn = get_db_connection()
    if not conn:
        return jsonify({'error': 'Database unavailable'}), 503
    try:
        cur = conn.cursor()
        cur.execute("SELECT voice_config FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return jsonify({'error': 'Not found'}), 404
        vc = row['voice_config'] or {}
        wp = vc.get('web_presence', {})
        agent_name = wp.get('agent_name', '')
        dba_name = wp.get('dba_name', '')
    finally:
        return_db_connection(conn)

    # Build user prompt from answers
    answer_lines = []
    for q_config in prompt_config['questions']:
        key = q_config['key']
        val = answers.get(key, '')
        if val:
            answer_lines.append(f"- {q_config['q']} {val}")

    user_prompt = (
        f"Agent name: {agent_name}\n"
        f"Business name: {dba_name}\n"
        f"\nAnswers:\n" + '\n'.join(answer_lines)
    )

    try:
        content = generate_clean_reply(
            client=xai_client,
            system_prompt=prompt_config['system'],
            user_message=user_prompt,
            max_tokens=500,
        )
        return jsonify({'content': content, 'section_type': section_type})
    except Exception as e:
        logger.error(f"[Domain] AI generation failed for {section_type}: {e}")
        return jsonify({'error': 'AI generation failed. Please try again.'}), 500


@domain_bp.route('/api/domain/photo', methods=['POST'])
@login_required
def upload_photo():
    """Upload profile photo (base64) to KV config."""
    conn = get_db_connection()
    if not conn:
        return jsonify({'error': 'Database unavailable'}), 503
    try:
        cur = conn.cursor()
        cur.execute("SELECT voice_config FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return jsonify({'error': 'Not found'}), 404
        vc = row['voice_config'] or {}
        wp = vc.get('web_presence', {})
        domain = wp.get('domain', '')
        if not domain:
            return jsonify({'error': 'No domain provisioned'}), 400

        data = request.json or {}
        photo_data = data.get('photo_url', '')

        # Validate: must be base64 data URI, max 2MB
        if photo_data and not photo_data.startswith('data:image/'):
            return jsonify({'error': 'Invalid image format'}), 400
        if len(photo_data) > 2 * 1024 * 1024 * 1.37:  # base64 overhead ~37%
            return jsonify({'error': 'Photo must be under 2MB'}), 400

        config = _cf_get_agent_config(domain) or {}
        config['photo_url'] = photo_data
        try:
            _cf_store_agent_config(domain, config)
        except Exception as e:
            logger.error(f"[PageBuilder] Photo KV write failed: {e}")
            return jsonify({'error': 'Failed to save photo. Please try again.'}), 500

        return jsonify({'status': 'ok'})
    finally:
        return_db_connection(conn)


@domain_bp.route('/api/domain/reviews', methods=['GET'])
@login_required
def get_reviews():
    """List pending + approved reviews from KV."""
    conn = get_db_connection()
    if not conn:
        return jsonify({'error': 'Database unavailable'}), 503
    try:
        cur = conn.cursor()
        cur.execute("SELECT voice_config FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return jsonify({'error': 'Not found'}), 404
        vc = row['voice_config'] or {}
        wp = vc.get('web_presence', {})
        domain = wp.get('domain', '')
        if not domain:
            return jsonify({'error': 'No domain'}), 400

        # Read reviews from KV (separate key from main config)
        kv_ns_id = os.getenv('CLOUDFLARE_KV_NAMESPACE_ID', '')
        reviews = []
        if kv_ns_id:
            try:
                resp = requests.get(
                    f'{CLOUDFLARE_API_BASE}/accounts/{CLOUDFLARE_ACCOUNT_ID}/storage/kv/namespaces/{kv_ns_id}/values/reviews:{domain}',
                    headers={'Authorization': f'Bearer {CLOUDFLARE_API_TOKEN}'},
                    timeout=30,
                )
                if resp.status_code == 200:
                    reviews = resp.json()
            except Exception:
                pass

        return jsonify({'reviews': reviews, 'domain': domain})
    finally:
        return_db_connection(conn)


@domain_bp.route('/api/domain/reviews/approve', methods=['POST'])
@login_required
def approve_review():
    """Approve or reject a pending review."""
    conn = get_db_connection()
    if not conn:
        return jsonify({'error': 'Database unavailable'}), 503
    try:
        cur = conn.cursor()
        cur.execute("SELECT voice_config FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return jsonify({'error': 'Not found'}), 404
        vc = row['voice_config'] or {}
        domain = vc.get('web_presence', {}).get('domain', '')
        if not domain:
            return jsonify({'error': 'No domain'}), 400

        data = request.json or {}
        review_index = data.get('index')
        approved = data.get('approved', False)
        delete = data.get('delete', False)

        kv_ns_id = os.getenv('CLOUDFLARE_KV_NAMESPACE_ID', '')
        if not kv_ns_id:
            return jsonify({'error': 'KV not configured'}), 500

        # Read current reviews
        try:
            resp = requests.get(
                f'{CLOUDFLARE_API_BASE}/accounts/{CLOUDFLARE_ACCOUNT_ID}/storage/kv/namespaces/{kv_ns_id}/values/reviews:{domain}',
                headers={'Authorization': f'Bearer {CLOUDFLARE_API_TOKEN}'},
                timeout=30,
            )
            reviews = resp.json() if resp.status_code == 200 else []
        except Exception:
            reviews = []

        if review_index is None or review_index >= len(reviews):
            return jsonify({'error': 'Invalid review index'}), 400

        if delete:
            reviews.pop(review_index)
        else:
            reviews[review_index]['approved'] = approved

        # Write reviews back to KV
        try:
            rv_resp = requests.put(
                f'{CLOUDFLARE_API_BASE}/accounts/{CLOUDFLARE_ACCOUNT_ID}/storage/kv/namespaces/{kv_ns_id}/values/reviews:{domain}',
                headers={
                    'Authorization': f'Bearer {CLOUDFLARE_API_TOKEN}',
                    'Content-Type': 'application/json',
                },
                data=json.dumps(reviews),
                timeout=30,
            )
            if rv_resp.status_code not in (200, 201):
                logger.error(f"[PageBuilder] Reviews KV write failed: {rv_resp.status_code}")
                return jsonify({'error': 'Failed to save review changes'}), 500
        except Exception as e:
            logger.error(f"[PageBuilder] Reviews KV write error: {e}")
            return jsonify({'error': 'Failed to save review changes'}), 500

        # Also update testimonials in main config so the landing page renders them
        try:
            config = _cf_get_agent_config(domain) or {}
            sections = config.get('sections', _default_sections())
            for s in sections:
                if s.get('type') == 'testimonials':
                    s['items'] = [r for r in reviews if r.get('approved')]
                    break
            config['sections'] = sections
            _cf_store_agent_config(domain, config)
        except Exception as e:
            logger.warning(f"[PageBuilder] Testimonials sync failed (reviews saved OK): {e}")

        return jsonify({'status': 'ok', 'reviews': reviews})
    finally:
        return_db_connection(conn)


@domain_bp.route('/api/domain/sync-carriers', methods=['POST'])
@login_required
def sync_carriers():
    """Sync contracted carriers from voice_config to domain KV."""
    conn = get_db_connection()
    if not conn:
        return jsonify({'error': 'Database unavailable'}), 503
    try:
        cur = conn.cursor()
        cur.execute("SELECT voice_config FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return jsonify({'error': 'Not found'}), 404
        vc = row['voice_config'] or {}
        wp = vc.get('web_presence', {})
        domain = wp.get('domain', '')
        if not domain or wp.get('status') != 'active':
            return jsonify({'status': 'skipped', 'reason': 'No active domain'})

        # Get carrier names from voice_config
        carrier_keys = vc.get('contracted_carriers', [])
        # Map keys to display names using carrier_list
        from carrier_list import CARRIER_MAP
        carrier_names = [CARRIER_MAP.get(k, k) for k in carrier_keys if CARRIER_MAP.get(k)]

        # Update KV config
        config = _cf_get_agent_config(domain) or {}
        config['carriers'] = carrier_names
        try:
            _cf_store_agent_config(domain, config)
        except Exception as e:
            logger.error(f"[PageBuilder] Carrier sync KV write failed: {e}")
            return jsonify({'error': 'Failed to sync carriers'}), 500

        return jsonify({'status': 'ok', 'carriers': carrier_names})
    finally:
        return_db_connection(conn)
