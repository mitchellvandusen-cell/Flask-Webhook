# blueprints/domain.py — Agent Web Presence: domain provisioning + management
#
# Routes:
#   POST /api/domain/search          — Search available .com domains
#   POST /api/domain/checkout        — Create Stripe subscription + provision
#   GET  /api/domain/status          — Current provisioning status
#   POST /api/domain/update-page     — Update landing page content
#   POST /api/domain/cancel          — Cancel domain subscription
#   POST /api/domain/contact-form    — Public: receive lead form submissions
#   POST /api/domain/auto-reply      — Internal: Twilio email auto-reply

import json
import logging
import os
import re
import time
import requests

from flask import Blueprint, request, jsonify
from flask_login import current_user, login_required

from db import get_db_connection, return_db_connection, log_webhook_event

logger = logging.getLogger(__name__)

domain_bp = Blueprint('domain', __name__)

# ── API Credentials ──
PORKBUN_API_KEY = os.getenv('PORKBUN_API_KEY', '')
PORKBUN_SECRET_KEY = os.getenv('PORKBUN_SECRET_KEY', '')
CLOUDFLARE_API_TOKEN = os.getenv('CLOUDFLARE_API_TOKEN', '')
CLOUDFLARE_ACCOUNT_ID = os.getenv('CLOUDFLARE_ACCOUNT_ID', '')
MAILGUN_API_KEY = os.getenv('MAILGUN_API_KEY', '')
MAILGUN_DOMAIN = os.getenv('MAILGUN_DOMAIN', '')  # existing mg.insurancegrokbot.com

PORKBUN_API_BASE = 'https://api-ipv4.porkbun.com/api/json/v3'
CLOUDFLARE_API_BASE = 'https://api.cloudflare.com/client/v4'


# ═══════════════════════════════════════════════════════════════
# PORKBUN API HELPERS
# ═══════════════════════════════════════════════════════════════

def _porkbun_post(endpoint, extra_data=None):
    """Make authenticated POST to Porkbun API."""
    data = {
        'apikey': PORKBUN_API_KEY,
        'secretapikey': PORKBUN_SECRET_KEY,
    }
    if extra_data:
        data.update(extra_data)
    resp = requests.post(f'{PORKBUN_API_BASE}{endpoint}',
                         json=data, timeout=30)
    return resp.json()


def _porkbun_check_available(domain):
    """Check if a .com domain is available via Porkbun."""
    # Porkbun doesn't have a single-domain check — use pricing endpoint
    # and catch errors for taken domains
    try:
        result = _porkbun_post(f'/domain/checkAvailability/{domain}')
        if result.get('status') == 'SUCCESS':
            avail = result.get('avail', result.get('available', False))
            price = result.get('pricing', {}).get('registration', '11.08')
            return {'available': bool(avail), 'price': price}
        return {'available': False, 'price': '0'}
    except Exception as e:
        logger.error(f"[Domain] Porkbun availability check failed for {domain}: {e}")
        return {'available': False, 'price': '0'}


def _porkbun_register(domain, contact_info):
    """Register a .com domain via Porkbun API."""
    data = {
        'domain': domain,
        'years': 1,
        'autoRenew': True,
        'whoisPrivacy': True,
    }
    # Add contact info
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

    result = _porkbun_post(f'/domain/register/{domain}', data)
    return result


def _porkbun_set_nameservers(domain, nameservers):
    """Update nameservers for a domain on Porkbun."""
    data = {'ns': nameservers}
    result = _porkbun_post(f'/domain/updateNs/{domain}', data)
    return result


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
    # Zone might already exist
    errors = data.get('errors', [])
    for err in errors:
        if err.get('code') == 1061:  # zone already exists
            # Look it up
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
    """Add a DNS record to a Cloudflare zone."""
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
        # Might already exist — not fatal
        logger.warning(f"[Domain] DNS record creation note: {result.get('errors', [])}")
    return result


def _cf_setup_dns(zone_id, domain, mailgun_dkim_records=None):
    """Configure all DNS records for an agent domain."""
    records_added = []

    # A record for landing page (proxied through Cloudflare)
    # Points to a dummy IP since Cloudflare Worker handles the actual serving
    _cf_add_dns_record(zone_id, 'A', '@', '192.0.2.1', proxied=True)
    records_added.append('A @')

    # CNAME for www
    _cf_add_dns_record(zone_id, 'CNAME', 'www', domain, proxied=True)
    records_added.append('CNAME www')

    # MX records for Cloudflare Email Routing
    _cf_add_dns_record(zone_id, 'MX', '@', 'route1.mx.cloudflare.net', priority=1)
    _cf_add_dns_record(zone_id, 'MX', '@', 'route2.mx.cloudflare.net', priority=2)
    _cf_add_dns_record(zone_id, 'MX', '@', 'route3.mx.cloudflare.net', priority=3)
    records_added.append('MX (email routing)')

    # SPF for Mailgun sending
    _cf_add_dns_record(zone_id, 'TXT', '@', 'v=spf1 include:mailgun.org ~all')
    records_added.append('TXT SPF')

    # DKIM records from Mailgun (if provided)
    if mailgun_dkim_records:
        for rec in mailgun_dkim_records:
            _cf_add_dns_record(zone_id, rec['type'], rec['name'], rec['value'])
            records_added.append(f"{rec['type']} {rec['name']}")

    return records_added


def _cf_setup_email_routing(zone_id, domain, forward_to):
    """Enable email routing and create catch-all forward rule."""
    # Enable email routing on the zone
    try:
        resp = requests.put(
            f'{CLOUDFLARE_API_BASE}/zones/{zone_id}/email/routing/enable',
            headers=_cf_headers(),
            json={'enabled': True},
            timeout=30,
        )
        logger.info(f"[Domain] Email routing enable response: {resp.status_code}")
    except Exception as e:
        logger.warning(f"[Domain] Email routing enable failed (may already be enabled): {e}")

    # Create catch-all rule to forward to agent's personal email
    try:
        # First, ensure the destination address is verified
        _cf_verify_email_destination(forward_to)

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
        result = resp.json()
        if result.get('success'):
            logger.info(f"[Domain] Email routing rule created: all → {forward_to}")
        else:
            logger.warning(f"[Domain] Email routing rule creation: {result.get('errors', [])}")
        return result
    except Exception as e:
        logger.error(f"[Domain] Email routing setup failed: {e}")
        return {'success': False, 'error': str(e)}


def _cf_verify_email_destination(email):
    """Add an email destination for routing (Cloudflare sends verification)."""
    try:
        resp = requests.post(
            f'{CLOUDFLARE_API_BASE}/accounts/{CLOUDFLARE_ACCOUNT_ID}/email/routing/addresses',
            headers=_cf_headers(),
            json={'email': email},
            timeout=30,
        )
        result = resp.json()
        # Might already be verified — that's fine
        if result.get('success') or any(e.get('code') == 1000 for e in result.get('errors', [])):
            return True
        logger.info(f"[Domain] Email destination verification sent to {email}")
        return True
    except Exception as e:
        logger.warning(f"[Domain] Email destination verification failed: {e}")
        return False


def _cf_setup_worker_route(zone_id, domain):
    """Create a Worker Route to serve the landing page on this domain."""
    try:
        resp = requests.post(
            f'{CLOUDFLARE_API_BASE}/zones/{zone_id}/workers/routes',
            headers=_cf_headers(),
            json={
                'pattern': f'{domain}/*',
                'script': 'omnisconn-agent-pages',
            },
            timeout=30,
        )
        result = resp.json()
        if result.get('success'):
            logger.info(f"[Domain] Worker route created: {domain}/* → omnisconn-agent-pages")
        else:
            logger.warning(f"[Domain] Worker route: {result.get('errors', [])}")

        # Also for www
        requests.post(
            f'{CLOUDFLARE_API_BASE}/zones/{zone_id}/workers/routes',
            headers=_cf_headers(),
            json={
                'pattern': f'www.{domain}/*',
                'script': 'omnisconn-agent-pages',
            },
            timeout=30,
        )
        return result
    except Exception as e:
        logger.error(f"[Domain] Worker route setup failed: {e}")
        return {'success': False}


def _cf_store_agent_config(domain, config):
    """Store agent config in Workers KV for the landing page Worker."""
    try:
        # We need the KV namespace ID — get it from env or create one
        kv_ns_id = os.getenv('CLOUDFLARE_KV_NAMESPACE_ID', '')
        if not kv_ns_id:
            logger.error("[Domain] CLOUDFLARE_KV_NAMESPACE_ID not set")
            return False

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
        if result.get('success'):
            logger.info(f"[Domain] Stored agent config in KV for {domain}")
        return result.get('success', False)
    except Exception as e:
        logger.error(f"[Domain] KV store failed for {domain}: {e}")
        return False


# ═══════════════════════════════════════════════════════════════
# MAILGUN HELPERS
# ═══════════════════════════════════════════════════════════════

def _mailgun_add_domain(domain):
    """Add a sending domain to Mailgun."""
    try:
        resp = requests.post(
            'https://api.mailgun.net/v4/domains',
            auth=('api', MAILGUN_API_KEY),
            data={'name': domain, 'web_scheme': 'https'},
            timeout=30,
        )
        data = resp.json()
        # Extract DKIM records for DNS setup
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
            'domain': data.get('domain', {}).get('name', domain),
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
            data={
                'from': from_email,
                'to': to_email,
                'subject': subject,
                'text': text,
            },
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
    # Clean the name
    clean = dba_name.lower().strip()
    # Remove common suffixes
    for suffix in ['llc', 'inc', 'corp', 'ltd', 'co', 'company', 'services',
                   'agency', 'group', 'associates', 'insurance agency',
                   'insurance services', 'insurance group']:
        clean = re.sub(rf'\b{re.escape(suffix)}\b', '', clean)
    clean = re.sub(r'[^a-z0-9\s]', '', clean).strip()
    clean = re.sub(r'\s+', '', clean)  # collapse to single string

    suggestions = []
    if clean:
        suggestions.append(f'{clean}.com')
        suggestions.append(f'{clean}insurance.com')
        suggestions.append(f'{clean}ins.com')
        if len(clean) > 12:
            # Try shorter version
            suggestions.append(f'{clean[:10]}.com')

    # Name-based suggestions
    if first_name and last_name:
        fn = re.sub(r'[^a-z]', '', first_name.lower())
        ln = re.sub(r'[^a-z]', '', last_name.lower())
        suggestions.append(f'{fn}{ln}insurance.com')
        suggestions.append(f'{fn}{ln}ins.com')

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for s in suggestions:
        if s not in seen and len(s) > 5:
            seen.add(s)
            unique.append(s)

    return unique[:6]  # Max 6 suggestions


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

    first_name = (data.get('first_name') or '').strip()
    last_name = (data.get('last_name') or '').strip()

    suggestions = _generate_domain_suggestions(dba_name, first_name, last_name)

    results = []
    for domain in suggestions:
        check = _porkbun_check_available(domain)
        results.append({
            'domain': domain,
            'available': check['available'],
            'price_yearly': check['price'],
        })

    return jsonify({'suggestions': results})


@domain_bp.route('/api/domain/checkout', methods=['POST'])
@login_required
def domain_checkout():
    """Start domain provisioning after Stripe payment confirmation."""
    import stripe
    data = request.json or {}

    domain = (data.get('domain') or '').strip().lower()
    dba_name = (data.get('dba_name') or '').strip()
    email_prefix = (data.get('email_prefix') or '').strip().lower() or 'info'
    forward_to = (data.get('forward_to') or current_user.email or '').strip()
    bio = (data.get('bio') or '').strip()
    phone_display = (data.get('phone_display') or '').strip()
    phone_raw = re.sub(r'[^+\d]', '', phone_display)
    licensed_states = data.get('licensed_states', [])
    disclaimer_accepted = data.get('disclaimer_accepted', False)

    if not domain or not domain.endswith('.com'):
        return jsonify({'error': 'A valid .com domain is required'}), 400
    if not dba_name:
        return jsonify({'error': 'Business name (DBA) is required'}), 400
    if not disclaimer_accepted:
        return jsonify({'error': 'You must accept the disclaimer to proceed'}), 400
    if not forward_to:
        return jsonify({'error': 'A forwarding email address is required'}), 400

    # Get subscriber info
    conn = get_db_connection()
    if not conn:
        return jsonify({'error': 'Database unavailable'}), 503
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id, voice_config, email FROM subscribers WHERE email = %s",
                    (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return jsonify({'error': 'Subscriber not found'}), 404
        location_id = row[0]
        vc = row[1] or {}
        subscriber_email = row[2]
    finally:
        return_db_connection(conn)

    # Check if already has a domain
    existing = vc.get('web_presence', {})
    if existing.get('domain') and existing.get('status') == 'active':
        return jsonify({'error': f'You already have a domain: {existing["domain"]}'}), 400

    # Get agent name from subscriber info
    agent_name = data.get('agent_name') or vc.get('operator_name') or current_user.email.split('@')[0].title()
    first_name = agent_name.split()[0] if ' ' in agent_name else agent_name
    last_name = agent_name.split()[-1] if ' ' in agent_name else ''

    agent_email = f'{email_prefix}@{domain}'

    # ── Create Stripe subscription ──
    try:
        stripe_price_id = os.getenv('STRIPE_DOMAIN_PRICE_ID', '')
        if not stripe_price_id:
            return jsonify({'error': 'Domain pricing not configured'}), 500

        customer_id = vc.get('stripe_customer_id', '')
        if not customer_id:
            # Look up from subscribers table
            conn2 = get_db_connection()
            try:
                cur2 = conn2.cursor()
                cur2.execute("SELECT stripe_customer_id FROM subscribers WHERE email = %s",
                             (current_user.email,))
                r2 = cur2.fetchone()
                cur2.close()
                customer_id = r2[0] if r2 else ''
            finally:
                return_db_connection(conn2)

        if not customer_id:
            return jsonify({'error': 'No Stripe customer found. Subscribe to a plan first.'}), 400

        subscription = stripe.Subscription.create(
            customer=customer_id,
            items=[{'price': stripe_price_id}],
            metadata={
                'type': 'domain',
                'domain': domain,
                'location_id': location_id,
            },
        )
    except Exception as e:
        logger.error(f"[Domain] Stripe subscription failed: {e}")
        return jsonify({'error': f'Payment failed: {str(e)}'}), 500

    # ── Provision everything ──
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
        'stripe_subscription_id': subscription.id,
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
            'phone': phone_raw or '+10000000000',
            'street': data.get('street', '123 Main St'),
            'city': data.get('city', 'Dallas'),
            'state': data.get('state', 'TX'),
            'zip': data.get('zip', '75201'),
        }
        reg_result = _porkbun_register(domain, contact)
        if reg_result.get('status') != 'SUCCESS':
            raise Exception(f"Domain registration failed: {reg_result.get('message', 'Unknown error')}")
        provisioning_log.append('domain_registered')

        # Step 2: Add zone to Cloudflare
        zone = _cf_create_zone(domain)
        web_presence['cloudflare_zone_id'] = zone['zone_id']
        provisioning_log.append('zone_created')

        # Step 3: Update nameservers on Porkbun to point to Cloudflare
        if zone.get('nameservers'):
            _porkbun_set_nameservers(domain, zone['nameservers'])
            provisioning_log.append('nameservers_updated')

        # Step 4: Add domain to Mailgun for outbound sending
        mg_result = _mailgun_add_domain(domain)
        web_presence['mailgun_domain_added'] = mg_result.get('success', False)
        provisioning_log.append('mailgun_domain_added')

        # Step 5: Configure DNS records
        _cf_setup_dns(zone['zone_id'], domain, mg_result.get('dkim_records', []))
        provisioning_log.append('dns_configured')

        # Step 6: Set up email routing
        _cf_setup_email_routing(zone['zone_id'], domain, forward_to)
        provisioning_log.append('email_routing_configured')

        # Step 7: Set up Worker route for landing page
        _cf_setup_worker_route(zone['zone_id'], domain)
        provisioning_log.append('worker_route_created')

        # Step 8: Store agent config in KV for the Worker
        accent = vc.get('whitelabel_config', {}).get('accent_color', '#1a6b4a')
        kv_config = {
            'agent_name': agent_name,
            'dba_name': dba_name,
            'phone_display': phone_display,
            'phone_raw': phone_raw,
            'email': agent_email,
            'licensed_states': licensed_states,
            'bio': bio,
            'accent_color': accent,
            'location_id': location_id,
        }
        _cf_store_agent_config(domain, kv_config)
        provisioning_log.append('kv_config_stored')

        web_presence['status'] = 'active'
        web_presence['landing_page_published'] = True

    except Exception as e:
        logger.error(f"[Domain] Provisioning failed at step {len(provisioning_log)}: {e}", exc_info=True)
        web_presence['status'] = 'error'
        web_presence['error'] = str(e)
        web_presence['provisioning_log'] = provisioning_log

    # Save to DB regardless of success (partial progress is saved)
    conn3 = get_db_connection()
    if conn3:
        try:
            cur3 = conn3.cursor()
            cur3.execute("""
                UPDATE subscribers
                SET voice_config = voice_config || %s::jsonb
                WHERE location_id = %s
            """, (json.dumps({'web_presence': web_presence}), location_id))
            conn3.commit()
            cur3.close()
        except Exception as db_err:
            logger.error(f"[Domain] DB save failed: {db_err}")
            conn3.rollback()
        finally:
            return_db_connection(conn3)

    log_webhook_event(location_id, 'domain_provisioned', 'success' if web_presence['status'] == 'active' else 'error',
                      f"Domain {domain}: {web_presence['status']}",
                      details={'domain': domain, 'steps': provisioning_log})

    status_code = 200 if web_presence['status'] == 'active' else 500
    return jsonify({
        'status': web_presence['status'],
        'domain': domain,
        'email': agent_email,
        'website': f'https://{domain}',
        'provisioning_log': provisioning_log,
        'error': web_presence.get('error'),
    }), status_code


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
        vc = row[0] or {}
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
        })
    finally:
        return_db_connection(conn)


@domain_bp.route('/api/domain/update-page', methods=['POST'])
@login_required
def domain_update_page():
    """Update landing page content (bio, phone, licensed states)."""
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
        location_id, vc = row[0], row[1] or {}
        wp = vc.get('web_presence', {})
        if not wp.get('domain'):
            return jsonify({'error': 'No domain registered'}), 400

        # Update fields
        if 'bio' in data:
            wp['bio'] = data['bio'].strip()
        if 'phone_display' in data:
            wp['phone_display'] = data['phone_display'].strip()
            wp['phone_raw'] = re.sub(r'[^+\d]', '', wp['phone_display'])
        if 'licensed_states' in data:
            wp['licensed_states'] = data['licensed_states']
        if 'agent_name' in data:
            wp['agent_name'] = data['agent_name'].strip()

        vc['web_presence'] = wp
        cur.execute("UPDATE subscribers SET voice_config = %s WHERE location_id = %s",
                    (json.dumps(vc), location_id))
        conn.commit()
        cur.close()

        # Update KV
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

        return jsonify({'status': 'ok', 'message': 'Landing page updated'})
    except Exception as e:
        logger.error(f"[Domain] Update page failed: {e}")
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        return_db_connection(conn)


@domain_bp.route('/api/domain/contact-form', methods=['POST'])
def domain_contact_form():
    """Public endpoint — receives lead form submissions from agent landing pages."""
    data = request.json or {}

    location_id = data.get('location_id', '')
    first_name = data.get('first_name', '').strip()
    last_name = data.get('last_name', '').strip()
    phone = data.get('phone', '').strip()
    email = data.get('email', '').strip()
    sms_consent = data.get('sms_consent', False)

    if not location_id or not first_name or not phone:
        return jsonify({'error': 'Missing required fields'}), 400

    # Store the lead
    conn = get_db_connection()
    if not conn:
        return jsonify({'status': 'ok'})  # Don't leak DB errors to public
    try:
        cur = conn.cursor()
        # Store in contact_cache as a web form lead
        cur.execute("""
            INSERT INTO contact_cache (location_id, contact_id, first_name, last_name,
                                       phone, email, source, cached_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT DO NOTHING
        """, (
            location_id,
            f'web_{phone}_{int(time.time())}',
            first_name,
            last_name,
            phone,
            email,
            'web_form',
        ))

        # Store consent record for TCPA compliance
        cur.execute("""
            INSERT INTO contact_cache (location_id, contact_id, first_name, last_name,
                                       phone, email, source, cached_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT DO NOTHING
        """, (
            location_id,
            f'consent_{phone}_{int(time.time())}',
            first_name,
            last_name,
            phone,
            json.dumps({
                'sms_consent': sms_consent,
                'consent_text': data.get('consent_text', ''),
                'consent_timestamp': data.get('consent_timestamp', ''),
                'consent_ip': data.get('consent_ip', ''),
                'consent_page': data.get('consent_page', ''),
            }),
            'consent_record',
        ))

        conn.commit()
        cur.close()

        log_webhook_event(location_id, 'web_lead', 'success',
                          f'Web lead: {first_name} {last_name} ({phone})',
                          details={'source': 'web_form', 'domain': data.get('domain', '')})

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
    """Internal endpoint — called by Email Worker to auto-reply to Twilio verification."""
    data = request.json or {}
    from_email = data.get('from_email', '')
    to_email = data.get('to_email', '')
    subject = data.get('subject', '')
    domain = data.get('domain', '')

    if not from_email or not to_email or not domain:
        return jsonify({'error': 'Missing fields'}), 400

    # Send reply via Mailgun from the agent's domain
    reply_subject = f'Re: {subject}' if subject else 'Email Verification Confirmation'
    reply_body = (
        f'This email confirms that {to_email} is a valid and monitored email address.\n\n'
        f'Thank you,\n{domain}'
    )

    success = _mailgun_send(
        from_email=to_email,  # Reply FROM the agent's address
        to_email=from_email,  # TO Twilio's sender
        subject=reply_subject,
        text=reply_body,
    )

    if success:
        logger.info(f"[Domain] Auto-replied to {from_email} from {to_email}")
        return jsonify({'status': 'ok', 'replied': True})
    else:
        logger.error(f"[Domain] Auto-reply failed: {to_email} → {from_email}")
        return jsonify({'status': 'error', 'replied': False}), 500
