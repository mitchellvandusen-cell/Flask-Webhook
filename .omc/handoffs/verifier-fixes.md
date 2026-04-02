# Verifier Fixes Log

**Author:** verifier agent
**Date:** 2026-04-02

## Fix 1: Local import in refresh_subscribers() (LOW risk)

**Line:** 775 (original)
**Issue:** `from ghl_api import get_valid_token_with_status` was a local import inside `refresh_subscribers()`. While `ghl_api` was already imported at top level (line 40) for other symbols, `get_valid_token_with_status` was missing from the top-level import. Per project rules, local imports are banned because they previously caused UnboundLocalError in production.
**Fix:** Added `get_valid_token_with_status` to the top-level import on line 40, removed the local import from the function body.

## Fix 2: DB connection leak in install_completed_at (MEDIUM risk)

**Line:** 1519-1532 (original)
**Issue:** `return_db_connection(_conn)` was inside the `try` block, not in a `finally` block. If `_cur.execute()` or `_conn.commit()` threw an exception, the connection would leak back to the pool without being returned.
**Fix:** Moved `_conn = None` before the try, moved `return_db_connection` into a `finally` block.

## Scenarios Verified

All 6 scenarios traced line-by-line through the code:

- **Scenario A (New individual agent):** PASS — correct primary selection, single subscriber row, welcome email sent
- **Scenario B (Agency owner first install):** PASS — all locations provisioned, agency_billing created, welcome email sent
- **Scenario C (Individual reconnect):** PASS — tokens updated, no welcome email, email corrected to login email
- **Scenario D (Agency owner reconnect):** PASS — tokens updated, agency_billing updated, no welcome email
- **Scenario E (Location-scoped token):** PASS — individual flow, single location, welcome email sent
- **Scenario F (API failures fallback):** PASS — user_location_ids fallback, synthesized sub_account, persistent alert

## Critical Bug Checks

1. **Local imports:** Fixed (Fix 1)
2. **Email overwrite:** Safe — correction only happens for matching location_id
3. **Agency false positive:** Safe — role_type='account' always returns False
4. **Token encryption:** All DB writes use enc_access_token or encrypt_token()
5. **DB connection leak:** Fixed (Fix 2). All other connections properly handled in finally blocks.
6. **Welcome email on reconnects:** Properly gated by is_new_install + existing_row checks
7. **Scope validation:** Fails gracefully with user-friendly flash message
8. **CSRF state validation:** Intact with secrets.compare_digest for website_user flow
