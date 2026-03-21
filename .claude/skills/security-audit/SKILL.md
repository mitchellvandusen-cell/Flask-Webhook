---
name: security-audit
description: "Trail of Bits-style security audit for InsuranceGrokBot. Scans for OWASP Top 10, multi-tenant isolation, Twilio ISV credential handling, OAuth token security, and Flask-specific vulnerabilities."
allowed-tools:
  - Read
  - Glob
  - Grep
  - Bash
  - Edit
---

# Security Audit Skill

You are performing a comprehensive security audit of InsuranceGrokBot, a multi-tenant white-label AI SMS/voice SaaS for insurance agents. Follow Trail of Bits methodology: enumerate attack surface, identify vulnerabilities, classify severity, and provide actionable remediation.

## Pre-Audit: Read Project Rules

1. Read `CLAUDE.md` at the project root for architecture context and mandatory rules.
2. Note the hybrid architecture: Flask/Gunicorn main + standalone async FastAPI voice server.
3. Note the multi-tenant model: each agency gets isolated Twilio sub-account, DB rows keyed by `subscriber_id` or `location_id`.

## Audit Steps (Execute in Order)

### Phase 1: SQL Injection & Query Safety

1. Search all `.py` files for raw SQL string formatting — these are potential SQLi vectors:
   ```
   Grep for: f"SELECT|f"INSERT|f"UPDATE|f"DELETE|f"ALTER|%s.*format|\.format\(.*SELECT|+ .*SELECT|"SELECT.*" \+
   ```
2. Verify ALL user-supplied values use parameterized queries (`%s` placeholders with psycopg2 tuple params).
3. Check `db.py` (~4200 lines) thoroughly — it contains all data access functions.
4. Check `ghl_sync.py` (~900 lines) for sync queries.
5. Check `workflow_engine.py` (~2300 lines) for dynamic query construction.
6. Flag any use of `cursor.execute(f"...")` or string concatenation in SQL.

### Phase 2: Cross-Tenant Data Leakage

1. Search for DB queries that lack `location_id` or `subscriber_id` filtering:
   ```
   Grep for SELECT/UPDATE/DELETE queries in db.py, tasks.py, workflow_engine.py, lead_intelligence.py
   ```
2. Verify every route handler that accesses subscriber data checks `current_user.location_id` or `session['location_id']`.
3. Check these critical files for tenant isolation:
   - `blueprints/dashboard.py` — config save, bot settings
   - `blueprints/agency.py` — agency KPIs, agent stats (must filter by agency's subscriber list)
   - `blueprints/admin.py` — god mode (must be admin-gated)
   - `blueprints/webhooks.py` — webhook processing
   - `voice/` package — all voice routes
   - `api_v1.py` — external API
4. Check for IDOR (Insecure Direct Object Reference) in URL parameters like `contact_id`, `location_id`, `call_sid`.
5. Verify agency dashboard queries scope to the agency owner's linked subscribers only.

### Phase 3: Twilio ISV Credential Isolation

1. Verify ALL Twilio sub-account operations use `get_sub_account_client_native(sub_account_sid, sub_account_auth_token)`.
2. Search for uses of the master Twilio client (`TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN`) that should be using sub-account clients:
   ```
   Grep for: TWILIO_ACCOUNT_SID|TWILIO_AUTH_TOKEN in voice/, twilio_provisioning.py, twilio_sms.py
   ```
3. Verify `is_master_account()` checks exist before Trust Hub operations.
4. Check that no sub-account credentials are logged or exposed in error messages.
5. Verify phone number operations use the correct sub-account client.

### Phase 4: Authentication & Authorization

1. Check all route handlers for proper `@login_required` decorators.
2. Verify admin routes check `ADMIN_EMAILS` whitelist.
3. Check agency routes verify `role == 'agency_owner'` from `agency_billing` table.
4. Verify API key authentication uses `hmac.compare_digest` (constant-time comparison).
5. Check training token authentication (`trn_` prefix).
6. Verify Flask session configuration:
   - `SESSION_SECRET` / `SECRET_KEY` set and strong
   - `SESSION_COOKIE_HTTPONLY = True`
   - `SESSION_COOKIE_SECURE = True` (in production)
   - `SESSION_COOKIE_SAMESITE = 'Lax'` or `'Strict'`
7. Check password hashing (must use bcrypt or argon2, not MD5/SHA1).
8. Verify password reset tokens use `itsdangerous` with expiry.

### Phase 5: OAuth Token Security

1. Verify OAuth tokens are encrypted at rest using Fernet (`token_encryption.py`).
2. Check that `crm_config` JSONB in `subscribers` table stores encrypted tokens.
3. Verify token refresh uses `pg_advisory_xact_lock` to prevent race conditions.
4. Check HubSpot webhook signature verification uses HMAC-SHA256 v3.
5. Check GHL webhook signature verification uses `MARKETPLACE_WEBHOOK_SECRET`.
6. Verify Stripe webhook signature verification uses `STRIPE_WEBHOOK_SECRET`.
7. Look for tokens in logs, error messages, or API responses.

### Phase 6: Input Validation & XSS

1. Check all Jinja2 templates for unescaped output (`{{ ... | safe }}` or `{% autoescape false %}`).
2. Verify user input is sanitized before rendering in:
   - Dashboard templates
   - Agency dashboard
   - Demo chat
   - Inbox/conversation threads
3. Check for reflected XSS in URL parameters rendered in templates.
4. Verify `reply_sanitizer.py` properly sanitizes LLM output before display.
5. Check webhook payload processing for injection in `payload_utils.py`.

### Phase 7: CSRF Protection

1. Verify `Flask-WTF` CSRF protection is enabled globally.
2. Check that all POST/PUT/DELETE routes include CSRF tokens.
3. Verify API endpoints that skip CSRF have proper alternative auth (API keys, webhook signatures).
4. Check AJAX requests include CSRF tokens in headers.

### Phase 8: Secrets & Credentials

1. Search for hardcoded secrets, API keys, passwords:
   ```
   Grep for: password|secret|api_key|token|credential in *.py, *.html, *.js (excluding .env.example, CLAUDE.md)
   ```
2. Verify `.env` is in `.gitignore`.
3. Check git history for accidentally committed secrets (note: don't run `git log -p` on large repos, sample recent commits).
4. Verify `CRON_SECRET` is checked on all cron endpoints.
5. Check that Stripe keys are not exposed to frontend (only publishable key should be client-side).

### Phase 9: Rate Limiting & DoS

1. Check rate limiting on:
   - API endpoints (`api_v1.py` — should have Redis sliding window)
   - Login/register (brute force protection)
   - Webhook endpoints (abuse prevention)
   - Demo chat (resource exhaustion)
2. Verify Redis-based rate limiting uses atomic operations.
3. Check for resource exhaustion vectors (large file uploads, unbounded queries, infinite loops).

### Phase 10: Dependency & Configuration

1. Check `requirements.txt` for known-vulnerable versions.
2. Verify `DEBUG = False` in production.
3. Check for `app.run(debug=True)` in production code paths.
4. Verify error handlers don't leak stack traces to users.
5. Check CORS configuration if applicable.

## Output Format

Generate a security report with:

```markdown
# InsuranceGrokBot Security Audit Report

## Executive Summary
[1-2 paragraphs on overall security posture]

## Critical Findings (P0 - Fix Immediately)
### [Finding Title]
- **File**: path/to/file.py:line
- **Type**: [SQLi|XSS|AuthZ|Tenant Isolation|etc.]
- **Description**: [What the vulnerability is]
- **Impact**: [What an attacker could do]
- **Remediation**: [Specific code fix]

## High Findings (P1 - Fix This Sprint)
[Same format]

## Medium Findings (P2 - Fix Next Sprint)
[Same format]

## Low / Informational (P3)
[Same format]

## Positive Findings
[Security controls that are properly implemented]
```

Classify severity using:
- **P0 Critical**: RCE, SQLi with data exfil, auth bypass, cross-tenant data access
- **P1 High**: Stored XSS, IDOR, privilege escalation, credential exposure
- **P2 Medium**: Reflected XSS, missing rate limits, weak crypto, CSRF bypass
- **P3 Low**: Information disclosure, missing headers, configuration issues
