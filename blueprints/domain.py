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

import hashlib
import json
import logging
import os
import re
import time
import requests
from functools import wraps

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
MAILGUN_API_KEY = os.getenv('MAILGUN_API_KEY', '')
CRON_SECRET = os.getenv('CRON_SECRET', '')  # Used to authenticate auto-reply endpoint

PORKBUN_API_BASE = 'https://api-ipv4.porkbun.com/api/json/v3'
CLOUDFLARE_API_BASE = 'https://api.cloudflare.com/client/v4'

# Cloudflare Worker IP for landing page routing (your deployed worker endpoint)
CLOUDFLARE_WORKER_IP = os.getenv('CLOUDFLARE_WORKER_IP', '192.0.2.1')  # NOTE: TEST-NET placeholder — set to actual IP


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
    """Make authenticated POST to Porkbun API. Handles non-JSON responses."""
    data = {
        'apikey': PORKBUN_API_KEY,
        'secretapikey': PORKBUN_SECRET_KEY,
    }
    if extra_data:
        data.update(extra_data)
    try:
        resp = requests.post(f'{PORKBUN_API_BASE}{endpoint}',
                             json=data, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.JSONDecodeError:
        logger.error(f"[Domain] Porkbun non-JSON response for {endpoint}: {resp.status_code} {resp.text[:200]}")
        return {'status': 'ERROR', 'message': f'Porkbun returned non-JSON response (HTTP {resp.status_code})'}
    except requests.exceptions.RequestException as e:
        logger.error(f"[Domain] Porkbun request failed for {endpoint}: {e}")
        return {'status': 'ERROR', 'message': str(e)}


def _porkbun_check_available(domain):
    """Check if a .com domain is available via RDAP (ICANN public protocol).
    RDAP 404 = available, 200 = taken. No API key needed."""
    try:
        resp = requests.get(
            f'https://rdap.verisign.com/com/v1/domain/{domain}',
            timeout=10,
            headers={'Accept': 'application/json'},
        )
        available = (resp.status_code == 404)
        return {'available': available, 'price': '11.08'}
    except Exception as e:
        logger.warning(f"[Domain] RDAP check failed for {domain}: {e}")
        return {'available': False, 'price': '11.08'}


def _porkbun_register(domain, contact_info):
    """Register a .com domain via Porkbun API."""
    data = {
        'domain': domain,
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

    return _porkbun_post('/domain/register', data)


@_retry_with_backoff(max_attempts=4, backoff_base=3)
def _porkbun_set_nameservers(domain, nameservers):
    """Update nameservers for a domain on Porkbun. Returns True on success.
    Retries up to 4 times with exponential backoff (3s, 9s, 27s) for activation timing.
    """
    result = _porkbun_post('/domain/updateNs', {'domain': domain, 'ns': nameservers})
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


def _cf_create_zone(domain):
    """Add a domain to Cloudflare as a new zone."""
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


def _cf_setup_dns(zone_id, domain, mailgun_dkim_records=None):
    """Configure all DNS records. Raises on critical failures (A record, MX)."""
    # A record — critical (landing page won't work without this)
    # Points to Cloudflare Worker for handling landing page
    if not _cf_add_dns_record(zone_id, 'A', '@', CLOUDFLARE_WORKER_IP, proxied=True):
        raise Exception("Failed to create A record — landing page will not be reachable")

    # A record for www (same Worker, proxied)
    # Using A record instead of CNAME to avoid CNAME chain issues
    if not _cf_add_dns_record(zone_id, 'A', 'www', CLOUDFLARE_WORKER_IP, proxied=True):
        logger.warning("[Domain] www A record failed — www subdomain may not work")

    # MX records — critical (email won't work without these)
    mx_ok = all([
        _cf_add_dns_record(zone_id, 'MX', '@', 'route1.mx.cloudflare.net', priority=1),
        _cf_add_dns_record(zone_id, 'MX', '@', 'route2.mx.cloudflare.net', priority=2),
        _cf_add_dns_record(zone_id, 'MX', '@', 'route3.mx.cloudflare.net', priority=3),
    ])
    if not mx_ok:
        logger.warning("[Domain] Some MX records failed — email routing may not work")

    # SPF — important for email deliverability
    _cf_add_dns_record(zone_id, 'TXT', '@', 'v=spf1 include:mailgun.org ~all')

    # DKIM from Mailgun — important for email deliverability
    if mailgun_dkim_records:
        for rec in mailgun_dkim_records:
            _cf_add_dns_record(zone_id, rec['type'], rec['name'], rec['value'])


def _cf_setup_email_routing(zone_id, domain, forward_to):
    """Enable email routing and create catch-all forward rule.
    Returns dict with email_verification_needed flag."""
    result = {'email_routing_ok': False, 'email_verification_needed': False}

    # Enable email routing on the zone
    try:
        requests.put(
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

    # Create catch-all rule
    try:
        resp = requests.post(
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
        resp_data = resp.json()
        result['email_routing_ok'] = resp_data.get('success', False)
    except Exception as e:
        logger.error(f"[Domain] Email routing rule failed: {e}")

    return result


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
    """Add a sending domain to Mailgun. Uses v3 API (not v4)."""
    try:
        resp = requests.post(
            'https://api.mailgun.net/v3/domains',
            auth=('api', MAILGUN_API_KEY),
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

    results = []
    for domain in suggestions:
        check = _porkbun_check_available(domain)
        results.append({
            'domain': domain,
            'available': check['available'],
            'price_yearly': check['price'],
        })
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

    # ── Step 1: Check domain is STILL available (race condition guard) ──
    avail_check = _porkbun_check_available(domain)
    if not avail_check.get('available'):
        return jsonify({'error': f'{domain} is no longer available. Please search again.'}), 409

    # ── Provision FIRST, charge AFTER ──
    provisioning_log = []
    web_presence = {
        'domain': domain,
        'dba_name': dba_name,
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

    try:
        # Step 1: Register domain on Porkbun
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
        reg_result = _porkbun_register(domain, contact)
        if reg_result.get('status') != 'SUCCESS':
            raise Exception(f"Domain registration failed: {reg_result.get('message', 'Unknown error')}")
        provisioning_log.append('domain_registered')
        logger.info(f"[Domain] Registered {domain} via Porkbun")

        # Step 2: Add zone to Cloudflare
        zone = _cf_create_zone(domain)
        web_presence['cloudflare_zone_id'] = zone['zone_id']
        provisioning_log.append('zone_created')

        # Step 3: Update nameservers (raises on failure)
        if zone.get('nameservers'):
            _porkbun_set_nameservers(domain, zone['nameservers'])
            provisioning_log.append('nameservers_updated')

        # Step 4: Add domain to Mailgun (v3 API)
        mg_result = _mailgun_add_domain(domain)
        web_presence['mailgun_domain_added'] = mg_result.get('success', False)
        provisioning_log.append('mailgun_domain_added')

        # Step 5: Configure DNS (raises on critical failures)
        _cf_setup_dns(zone['zone_id'], domain, mg_result.get('dkim_records', []))
        provisioning_log.append('dns_configured')

        # Step 6: Set up email routing
        email_result = _cf_setup_email_routing(zone['zone_id'], domain, forward_to)
        web_presence['email_verification_needed'] = email_result.get('email_verification_needed', False)
        provisioning_log.append('email_routing_configured')

        # Step 7: Worker route (raises on failure)
        _cf_setup_worker_route(zone['zone_id'], domain)
        provisioning_log.append('worker_route_created')

        # Step 8: Store KV config (raises on failure)
        accent = vc.get('whitelabel_config', {}).get('accent_color', '#1a6b4a')
        _cf_store_agent_config(domain, {
            'agent_name': agent_name,
            'dba_name': dba_name,
            'phone_display': phone_display,
            'phone_raw': phone_raw,
            'email': agent_email,
            'licensed_states': licensed_states,
            'bio': bio,
            'accent_color': accent,
            'location_id': location_id,
        })
        provisioning_log.append('kv_config_stored')

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

    log_webhook_event(location_id, 'domain_provisioned', 'success',
                      f"Domain {domain} is live",
                      details={'domain': domain, 'steps': provisioning_log})

    response = {
        'status': 'active',
        'domain': domain,
        'email': agent_email,
        'website': f'https://{domain}',
        'provisioning_log': provisioning_log,
    }
    if web_presence.get('email_verification_needed'):
        response['email_notice'] = (
            f'Email forwarding requires verification. Check {forward_to} for a '
            f'verification email from Cloudflare and click the link to activate forwarding.'
        )
    return jsonify(response)


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
