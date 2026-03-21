---
name: env-secrets-scan
description: "Scan InsuranceGrokBot for leaked secrets in code, git history, and templates. Verify token encryption, OAuth storage, HMAC verification, and .env safety."
allowed-tools:
  - Read
  - Glob
  - Grep
  - Bash
---

# Environment & Secrets Scan Skill

You are scanning InsuranceGrokBot for leaked secrets, insecure credential storage, and missing security controls around sensitive data. This is a focused audit of secrets management only.

## Pre-Scan: Load Context

1. Read `CLAUDE.md` for the full list of environment variables and their purposes.
2. Note the sensitive credentials this app handles:
   - Twilio master account SID + auth token
   - Twilio sub-account SIDs + auth tokens (per subscriber)
   - GHL OAuth tokens (access + refresh, per subscriber)
   - HubSpot OAuth tokens (access + refresh, per subscriber)
   - Stripe secret key + webhook secret
   - xAI API key
   - Flask session secret
   - Fernet encryption key
   - Discord bot token + OAuth credentials
   - Slack OAuth credentials
   - Mailgun API key
   - Google service account JSON
   - Cron secret
   - API keys (user-generated, stored in DB)
   - Training tokens (`trn_` prefix)

## Step 1: Scan Code for Hardcoded Secrets

Search all source files for patterns that indicate hardcoded credentials:

### 1a. Direct Secret Patterns
```
Grep for these patterns across all .py, .html, .js files (excluding .env.example, CLAUDE.md, SKILL.md):

- SK_[a-zA-Z0-9]{20,}          (Stripe secret keys)
- sk_live_[a-zA-Z0-9]+          (Stripe live keys)
- sk_test_[a-zA-Z0-9]+          (Stripe test keys)
- AC[a-f0-9]{32}                (Twilio Account SIDs)
- SK[a-f0-9]{32}                (Twilio API Key SIDs)
- whsec_[a-zA-Z0-9]+            (Stripe webhook secrets)
- xai-[a-zA-Z0-9]+              (xAI API keys)
- ghp_[a-zA-Z0-9]+              (GitHub personal access tokens)
- gho_[a-zA-Z0-9]+              (GitHub OAuth tokens)
- [a-f0-9]{32}                  (generic hex tokens — check context)
- Bearer [a-zA-Z0-9\-._~+/]+=* (Bearer tokens in code)
- -----BEGIN.*PRIVATE KEY-----  (Private keys)
- password\s*=\s*["'][^"']+     (Hardcoded passwords)
- secret\s*=\s*["'][^"']+       (Hardcoded secrets)
- api_key\s*=\s*["'][^"']+      (Hardcoded API keys)
```

### 1b. Configuration Defaults
```
Grep for suspicious default values:
- default.*password
- default.*secret
- default.*token
- fallback.*key
- os.environ.get\(.*,\s*["'][a-zA-Z0-9]  (env vars with non-empty defaults that look like real values)
```

### 1c. Comment Secrets
```
Grep for secrets accidentally left in comments:
- #.*sk_live
- #.*AC[a-f0-9]{32}
- #.*password.*=
- #.*TODO.*secret
```

## Step 2: Check .gitignore Coverage

1. Read `.gitignore` and verify these are excluded:
   - `.env` (all variants: `.env.local`, `.env.production`, `.env.development`)
   - `*.pem`, `*.key` (private keys)
   - `credentials.json`, `service-account*.json` (Google credentials)
   - `hubspot.config.yml` (HubSpot CLI config)
   - `__pycache__/`, `*.pyc`
   - `*.db`, `*.sqlite3` (local databases)
   - `.vscode/`, `.idea/` (IDE settings that may contain tokens)

2. Check that `.env.example` exists and contains ONLY placeholder values (no real secrets).

3. Verify no `.env` file is currently tracked:
   ```bash
   git ls-files | grep -i '\.env'
   ```

## Step 3: Git History Scan

Scan recent git history for accidentally committed secrets:

```bash
# Check last 50 commits for secret patterns (sample, don't scan entire history)
git log --oneline -50 --diff-filter=A -- '*.env' '*.pem' '*.key' 'credentials*' '*secret*'

# Check if .env was ever committed
git log --all --oneline -- '.env'

# Check for large diffs that might contain credentials
git log --oneline -20 --stat | head -80
```

Do NOT run `git log -p` on the full repo (too large). Sample specific suspect commits if found.

## Step 4: Token Encryption Verification

1. Read `token_encryption.py` and verify:
   - Uses Fernet (AES-128-CBC with HMAC-SHA256) or better
   - Encryption key is derived from environment variable, not hardcoded
   - `encrypt_token()` and `decrypt_token()` are used consistently

2. Check OAuth token storage in `db.py`:
   ```
   Grep for: crm_config|access_token|refresh_token|auth_token in db.py
   ```
   - Verify tokens stored in `subscribers.crm_config` are encrypted before DB write
   - Verify tokens are decrypted only when needed for API calls
   - Check if `update_subscriber_token()` encrypts before storing

3. Check Twilio sub-account credentials:
   - How are `sub_account_sid` and `sub_account_auth_token` stored?
   - Are they in the `subscribers` table? Encrypted?
   - Verify `get_sub_account_client_native()` handles decryption

## Step 5: OAuth Security Patterns

1. **CSRF Protection on OAuth Flows**:
   ```
   Grep for: state.*nonce|state.*random|state.*token in blueprints/oauth.py, crm_providers/hubspot/oauth.py
   ```
   - Verify OAuth state parameter uses cryptographic nonce
   - Verify state is validated on callback

2. **Token Refresh Race Conditions**:
   ```
   Grep for: pg_advisory_xact_lock|advisory_lock in db.py
   ```
   - Verify `update_subscriber_token()` uses advisory lock
   - Verify concurrent refresh attempts don't cause token invalidation

3. **Token in Logs**:
   ```
   Grep for: log.*token|log.*access_token|log.*auth_token|print.*token in *.py
   ```
   - Verify PII redaction filter catches tokens
   - Check that error handlers don't include tokens in error responses

4. **Token in URLs**:
   ```
   Grep for: token=|access_token=|auth_token= in *.py, *.html
   ```
   - Tokens should never be in URL query parameters (they end up in server logs, browser history)

## Step 6: Webhook Signature Verification

1. **GHL Webhooks** (`blueprints/webhooks.py`):
   - Verify `MARKETPLACE_WEBHOOK_SECRET` is used for signature verification
   - Check that verification happens BEFORE processing the payload

2. **Stripe Webhooks** (`blueprints/billing.py`):
   - Verify `stripe.Webhook.construct_event()` uses `STRIPE_WEBHOOK_SECRET`
   - Check that raw body (not parsed JSON) is used for verification

3. **HubSpot Webhooks** (`crm_providers/hubspot/inbound.py`):
   - Verify HMAC-SHA256 v3 signature verification
   - Check that `X-HubSpot-Signature-v3` and `X-HubSpot-Request-Timestamp` are validated
   - Verify timestamp is checked for replay prevention

4. **Cron Endpoints**:
   - Verify all `/api/cron/*` endpoints check `CRON_SECRET`
   - Check both `?key=` query param and `Authorization: Bearer` header

## Step 7: Frontend Secret Exposure

1. Check JavaScript files and HTML templates:
   ```
   Grep for: api_key|secret|token|password|credential in static/js/, templates/
   ```
2. Verify only Stripe PUBLISHABLE key is exposed to frontend (never the secret key).
3. Check that `window.DASHBOARD_BOOT` data doesn't include sensitive fields.
4. Verify no OAuth tokens are passed to frontend JavaScript.
5. Check for secrets in HTML comments or data attributes.

## Step 8: API Key Security

1. Check how user API keys are generated (`blueprints/dashboard.py`):
   - Must use cryptographically secure random generation
   - Must be stored hashed (not plaintext) if possible
   - Must use constant-time comparison on authentication

2. Check training tokens (`trn_` prefix):
   - Same security requirements as API keys
   - Verify rate limiting is enforced

## Output Format

```markdown
# Secrets & Environment Security Scan — InsuranceGrokBot

## Summary
- **Hardcoded secrets found**: [count]
- **Git history leaks**: [count]
- **Token encryption issues**: [count]
- **OAuth security issues**: [count]
- **Webhook verification issues**: [count]
- **Frontend exposure issues**: [count]

## Critical Findings (Secrets Exposed)

### [Finding Title]
- **File**: `path/to/file.py:line`
- **Type**: [Hardcoded Secret | Git Leak | Unencrypted Token | etc.]
- **Secret Type**: [Twilio | Stripe | OAuth | etc.]
- **Exposure**: [In code | In git history | In logs | In frontend]
- **Remediation**: [Specific action to take]

## Token Encryption Audit
- [ ] Fernet encryption properly implemented
- [ ] GHL OAuth tokens encrypted at rest
- [ ] HubSpot OAuth tokens encrypted at rest
- [ ] Twilio sub-account credentials encrypted at rest
- [ ] Encryption key from environment (not hardcoded)

## OAuth Security Audit
- [ ] GHL OAuth uses CSRF state parameter
- [ ] HubSpot OAuth uses CSRF state parameter
- [ ] Token refresh uses advisory lock
- [ ] No tokens in logs or error responses
- [ ] No tokens in URL parameters

## Webhook Signature Audit
- [ ] GHL webhook signature verified
- [ ] Stripe webhook signature verified
- [ ] HubSpot webhook signature verified (HMAC-SHA256 v3)
- [ ] Cron endpoints require CRON_SECRET
- [ ] Verification happens before payload processing

## .gitignore Audit
- [ ] .env excluded
- [ ] Private keys excluded
- [ ] Credential files excluded
- [ ] No .env in git history

## Recommendations
[Prioritized list of fixes]
```
