---
name: fix-production
description: "Diagnose and fix production errors in InsuranceGrokBot. Fetches errors from Railway logs, traces root cause in codebase, applies fix, tests, commits, and pushes."
allowed-tools:
  - Read
  - Glob
  - Grep
  - Bash
  - Edit
  - WebFetch
  - mcp__railway__get-logs
  - mcp__railway__list-services
  - mcp__railway__list-deployments
  - mcp__railway__list-projects
---

# Fix Production Skill

You are the on-call engineer for InsuranceGrokBot. A production error has been reported or detected. Your job: diagnose, fix, verify, ship.

## Pre-Fix: Load Context

1. Read `CLAUDE.md` at the project root for architecture context.
2. Understand the deployment topology:
   - **Flask-Webhook**: Gunicorn main app (webhooks, blueprints, dashboard)
   - **worker**: RQ worker for `production` + `intelligence` queues
   - **worker-bg**: RQ worker for `website` + `demo` queues
   - **Redis**: Managed Redis instance
   - All deployed on Railway

## Step 1: Gather Error Context

If the user provides an error message or stack trace, use that directly.

If not, gather errors from available sources:

### Railway Logs
Use the Railway MCP tools to fetch recent logs:
1. `list-projects` to find the InsuranceGrokBot project
2. `list-services` to identify which service has errors
3. `get-logs` for each service to find recent errors

Look for:
- Python tracebacks (most common)
- Gunicorn worker timeouts
- Redis connection errors (`ConnectionError`, `TimeoutError`)
- PostgreSQL errors (`OperationalError`, `InterfaceError`, pool exhaustion)
- Twilio API errors (401, 403, 404, 429)
- GHL/HubSpot OAuth token expiry (401/403)
- RQ job failures (check worker logs)

### Error Patterns to Recognize

| Error | Likely Cause | Common Fix |
|-------|-------------|------------|
| `psycopg2.pool.PoolError: connection pool exhausted` | Connection leak — missing `return_db_connection()` in `finally` | Find the leaking function, add `finally` block |
| `psycopg2.InterfaceError: connection already closed` | Stale connection from pool | Verify pool health check; may need `DB_POOL_MAX` increase |
| `redis.ConnectionError` | Redis down or URL changed | Check `REDIS_URL` env var, verify Redis service is running |
| `twilio.rest.TwilioRestException 20003` | Auth failure — wrong credentials for account type | Check `is_master_account()` / `get_sub_account_client_native()` usage |
| `openai.AuthenticationError` | xAI API key invalid or expired | Check `XAI_API_KEY` env var |
| `KeyError: 'location_id'` | Missing data in session or webhook payload | Check auth flow, verify webhook payload structure |
| `rq.exceptions.NoSuchJobError` | Job expired from Redis before worker picked it up | Check Redis memory, job TTL |
| `GHL 401/403` | OAuth token expired, refresh failed | Check `refresh_tokens` cron, verify `update_subscriber_token()` |
| `HubSpot 401` | Token expired (6hr expiry) | Check `crm_config` token refresh, verify cron interval |
| `stripe.error.SignatureVerificationError` | Wrong webhook secret or replay | Check `STRIPE_WEBHOOK_SECRET` |

## Step 2: Trace Root Cause

1. From the error/traceback, identify the exact file and line number.
2. Read the relevant source file(s).
3. Trace the call chain backward to find the root cause:
   - Is it a data issue? (missing field, wrong type, null where not expected)
   - Is it a resource issue? (pool exhaustion, Redis down, API rate limit)
   - Is it a logic bug? (wrong branch, missing check, race condition)
   - Is it a configuration issue? (wrong env var, missing secret)
   - Is it a multi-tenant issue? (wrong credentials used, cross-tenant data)
4. Check if this error has happened before — search git log for related fixes.
5. Check if there's a related test that should have caught this.

## Step 3: Implement Fix

1. Make the minimal, targeted fix. Do not refactor unrelated code.
2. Follow all CLAUDE.md rules (especially Twilio ISV, multi-tenant isolation, DB connection safety).
3. If the fix involves:
   - **DB connection leak**: Add proper `try/finally` with `return_db_connection()`.
   - **OAuth token issue**: Verify refresh logic, check `pg_advisory_xact_lock` usage.
   - **Twilio credential issue**: Verify correct client (master vs sub-account).
   - **Missing null check**: Add defensive checks with sensible defaults or early returns.
   - **Race condition**: Add appropriate locking (DB advisory lock, Redis atomic ops, threading.Lock).

## Step 4: Verify Fix Locally

1. Run relevant tests:
   ```bash
   python -m pytest test_app_routes.py -x -v 2>&1 | tail -30
   python -m pytest test_synthetic_e2e.py -x -v 2>&1 | tail -30
   python -m pytest test_crm_adapters.py -x -v 2>&1 | tail -30
   ```
2. If the fix touches voice code:
   ```bash
   python -m pytest test_voice_server.py -x -v 2>&1 | tail -30
   ```
3. If the fix touches booking/time parsing:
   ```bash
   python -m pytest test_booking_time_parsing.py -x -v 2>&1 | tail -30
   ```
4. Check for syntax errors:
   ```bash
   python -c "import main" 2>&1
   ```

## Step 5: Commit and Push

1. Stage only the fix files (no unrelated changes).
2. Commit with a descriptive message:
   ```
   fix: [brief description of what was broken and why]

   Root cause: [1-2 sentences on the root cause]

   Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
   ```
3. Push to the current branch.
4. If on `main`, push directly. If on a feature branch, note that a PR may be needed.

## Step 6: Verify in Production

1. After push, check Railway deployment status:
   ```bash
   # Use Railway MCP tools to verify deployment
   ```
2. Wait for deployment to complete.
3. Check logs again to verify the error is resolved.
4. If the error persists, go back to Step 2 with the new information.

## Step 7: Post-Fix Documentation

1. Update `CHANGELOG.md` with the fix.
2. If the fix reveals a pattern that should be prevented in the future, add a rule to `CLAUDE.md`.
3. If a test was missing, note that a test should be added (but don't add it in the hotfix — that's a follow-up).

## Output Format

```markdown
# Production Fix Report

## Error
[Error message / traceback]

## Root Cause
[What caused the error and why]

## Fix Applied
**File(s):** [list of changed files]
**Change:** [Description of the fix]

## Verification
- [ ] Tests pass
- [ ] Syntax check passes
- [ ] Committed and pushed
- [ ] Deployed to Railway
- [ ] Error resolved in production logs

## Follow-Up Items
- [ ] [Any tests to add]
- [ ] [Any monitoring to set up]
- [ ] [Any related issues to investigate]
```
