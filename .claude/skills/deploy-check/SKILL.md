---
name: deploy-check
description: "Pre-deployment verification for InsuranceGrokBot. Runs test suite, checks for breaking changes, verifies Alembic migrations, Railway service health, and environment variables."
allowed-tools:
  - Read
  - Glob
  - Grep
  - Bash
  - WebFetch
  - mcp__railway__get-logs
  - mcp__railway__list-services
  - mcp__railway__list-deployments
  - mcp__railway__list-projects
  - mcp__railway__check-railway-status
  - mcp__railway__list-variables
---

# Deploy Check Skill

You are the deployment gatekeeper for InsuranceGrokBot. Before any code goes to production, you verify everything is safe to ship.

## Pre-Check: Load Context

1. Read `CLAUDE.md` for architecture context and test requirements.
2. Understand what's being deployed: check `git log --oneline -10` and `git diff main...HEAD` (if on a feature branch) or `git diff HEAD~3` (if on main).

## Step 1: Code Syntax & Import Verification

Run these checks to ensure the app can start:

```bash
# Verify main Flask app imports cleanly
python -c "import main" 2>&1

# Verify all blueprint imports
python -c "
from blueprints import auth, public, webhooks, oauth, dashboard, billing, admin, agency, demo, discord, slack, cron, inbox, calendar, google_calendar, team, embed, workflows
print('All blueprints import OK')
" 2>&1

# Verify voice package imports
python -c "
from voice import numbers, dialer, predictive_engine, contacts, stream, call_history, voice_prompt, a2p, outbound, intelligence, twiml_routes, stats, recordings, setup, audio, helpers, voice_tools, insights, redis_state, call_state
print('Voice package imports OK')
" 2>&1

# Verify CRM providers import
python -c "
from crm_providers import get_provider
from crm_providers.base import CRMProvider
print('CRM providers import OK')
" 2>&1

# Verify worker imports
python -c "import worker" 2>&1

# Check for syntax errors across all Python files
python -m py_compile main.py 2>&1
python -m py_compile db.py 2>&1
python -m py_compile tasks.py 2>&1
python -m py_compile voice_bridge.py 2>&1
```

## Step 2: Test Suite

Run the full test suite:

```bash
# Core tests
python -m pytest test_app_routes.py -x -v 2>&1 | tail -40
python -m pytest test_synthetic_e2e.py -x -v 2>&1 | tail -40
python -m pytest test_crm_adapters.py -x -v 2>&1 | tail -40
python -m pytest test_voice_server.py -x -v 2>&1 | tail -40
python -m pytest test_booking_time_parsing.py -x -v 2>&1 | tail -40

# Run all tests if individual suites pass
python -m pytest --tb=short 2>&1 | tail -60
```

If any test fails:
- Read the failure output carefully
- Determine if the failure is related to the current changes or pre-existing
- If related to current changes: STOP — do not approve deployment
- If pre-existing: note it but continue with other checks

## Step 3: Breaking Change Detection

### 3a. Database Schema Changes
```bash
# Check for new Alembic migrations
ls -la db/migrations/versions/ 2>/dev/null || ls -la migrations/versions/ 2>/dev/null

# Check for schema changes in db.py init_db()
git diff HEAD~5 -- db.py | head -200
```

If schema changes detected:
- Verify Alembic migration exists for the change
- Check migration is reversible (has `downgrade()`)
- Verify migration doesn't drop columns or tables without data migration
- Check that `db_legacy.py` compatibility is maintained

### 3b. API Breaking Changes
```
Grep for changes to route definitions:
git diff HEAD~5 -- blueprints/ api_v1.py voice_bridge.py | grep -E "^[+-].*@.*route|^[+-].*def " | head -50
```

Check for:
- Removed or renamed endpoints
- Changed request/response formats
- Changed authentication requirements
- Changed query parameter names

### 3c. Environment Variable Changes
```
Grep for new os.environ or os.getenv calls:
git diff HEAD~5 -- *.py | grep -E "^\+.*os\.(environ|getenv)" | head -20
```

If new env vars are required:
- Verify they're documented in CLAUDE.md
- Verify they're set in Railway (use `list-variables` MCP tool)
- Verify `.env.example` is updated

### 3d. Dependency Changes
```bash
git diff HEAD~5 -- requirements.txt | head -30
```

If requirements changed:
- New packages: verify they're needed and license-compatible
- Version bumps: check for breaking changes in the package changelog
- Removed packages: verify nothing still imports them

## Step 4: Multi-Tenant Safety Review

For any changed files, verify:

1. **DB queries have tenant scoping**: Every SELECT/UPDATE/DELETE includes `location_id` or `subscriber_id` WHERE clause.
2. **Route handlers check auth**: `@login_required` + `current_user.location_id` scoping.
3. **Twilio operations use correct client**: Sub-account operations use `get_sub_account_client_native()`.
4. **Redis keys include tenant ID**: No global keys that could leak across tenants.

```bash
# Quick check: any new DB queries without location_id?
git diff HEAD~5 -- *.py | grep -E "^\+.*(SELECT|UPDATE|DELETE)" | grep -v location_id | grep -v subscriber_id | head -20
```

## Step 5: Railway Service Health

Use Railway MCP tools to verify current state:

1. **check-railway-status** — Platform health
2. **list-services** — All 4 services running
3. **list-deployments** — Last deployment status for each service
4. **get-logs** (last 5 min for each service) — No error spikes

### Expected Services
| Service | Expected State |
|---------|---------------|
| Flask-Webhook | Running, no OOM, no crash loops |
| worker | Running, processing jobs |
| worker-bg | Running, processing background jobs |
| Redis | Running, memory < 80% |

## Step 6: Environment Variable Verification

Use Railway MCP tools or check documentation:

### Required Variables (deployment will fail without these)
- `DATABASE_URL`
- `REDIS_URL`
- `SESSION_SECRET` or `SECRET_KEY`
- `YOUR_DOMAIN`
- `XAI_API_KEY`
- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `GHL_CLIENT_ID`
- `GHL_CLIENT_SECRET`
- `STRIPE_SECRET_KEY`
- `STRIPE_WEBHOOK_SECRET`
- `CRON_SECRET`

### Optional but Important
- `HUBSPOT_CLIENT_ID` / `HUBSPOT_CLIENT_SECRET` (if HubSpot enabled)
- `DISCORD_CLIENT_ID` / `DISCORD_BOT_TOKEN` (if Discord enabled)
- `SLACK_CLIENT_ID` / `SLACK_CLIENT_SECRET` (if Slack enabled)
- `MAILGUN_API_KEY` / `MAILGUN_DOMAIN` (for API email sending)

## Step 7: Deployment Checklist

Generate the final go/no-go decision:

```markdown
# Deploy Check Report — InsuranceGrokBot

## Changes Being Deployed
[Summary of changes from git log]

## Pre-Deploy Checklist

### Code Quality
- [ ] All Python files import cleanly (no syntax errors)
- [ ] All blueprints import successfully
- [ ] Voice package imports successfully
- [ ] CRM providers import successfully

### Tests
- [ ] test_app_routes.py — PASS / FAIL / SKIP
- [ ] test_synthetic_e2e.py — PASS / FAIL / SKIP
- [ ] test_crm_adapters.py — PASS / FAIL / SKIP
- [ ] test_voice_server.py — PASS / FAIL / SKIP
- [ ] test_booking_time_parsing.py — PASS / FAIL / SKIP

### Breaking Changes
- [ ] No database schema changes without migration
- [ ] No removed/renamed API endpoints
- [ ] No new required environment variables (or they're already set)
- [ ] No dependency changes (or they're compatible)

### Safety
- [ ] Multi-tenant isolation verified in changed code
- [ ] Twilio sub-account isolation verified
- [ ] DB connections use try/finally pattern
- [ ] No hardcoded secrets in changed code

### Infrastructure
- [ ] Railway platform healthy
- [ ] All 4 services running
- [ ] No active incidents
- [ ] Environment variables complete

## Decision: [GO / NO-GO]
**Reason**: [Why it's safe or unsafe to deploy]

## Post-Deploy Monitoring
After deployment, watch for:
- [ ] Application starts without errors (check Flask-Webhook logs)
- [ ] Workers reconnect to Redis (check worker logs)
- [ ] Webhook processing resumes (check production queue)
- [ ] No new error patterns in first 5 minutes
```
