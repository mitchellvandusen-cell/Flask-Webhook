# OAuth Enterprise Rewrite — Implementation Complete

**Author:** implementer agent
**Date:** 2026-04-02
**File:** `blueprints/oauth.py`

---

## What Was Changed

The monolithic `oauth_callback()` function (~750 lines) was decomposed into a clean 13-step flow with 8 extracted helper functions:

### New Helper Functions
1. `_get_existing_location_ids(conn, location_ids)` — Check which locations already have subscriber rows
2. `_determine_primary_location(...)` — 6-priority location selection algorithm with DB check
3. `_detect_agency_owner(...)` — Agency detection using role_type as primary signal
4. `_fetch_installed_locations(headers, company_id, client_id)` — GHL installedLocations API with fallback URLs
5. `_generate_location_tokens(headers, company_id, installed_locs)` — Per-location token generation
6. `_resolve_user_email(token_data, location_id, company_id)` — 4-source email recovery chain
7. `_send_ghost_install_alert(...)` — Admin notification for placeholder accounts
8. `_build_company_metadata(me_data, company_id)` — Company metadata extraction
9. `_upsert_agency_owner(...)` — Agency owner DB operations (subscribers + agency_billing)
10. `_update_existing_location(...)` — Reconnect: update tokens on existing subscriber row
11. `_provision_new_subscriber(...)` — New subscriber row with savepoint/rollback for email conflicts

### 13-Step Flow
1. Validate state (CSRF)
2. Token exchange (Company first, Location fallback)
3. Extract core fields
4. Validate critical scopes
5. Get user info via /users/{userId}
6. Fetch installed locations + location tokens (Company token only)
7. Determine primary_location_id via `_determine_primary_location()`
8. Detect agency owner via `_detect_agency_owner()`
9. Email recovery chain (5 sources)
10. DB operations (single connection, transaction)
11. Post-onboarding (logging, alerts, install timestamp)
12. Welcome email (NEW installs only — not reconnects)
13. Login and redirect

---

## Scenario Handling

### Scenario A: New individual agent (Company token, role_type=account)
- `_detect_agency_owner()` returns False (role_type=account)
- `_determine_primary_location()` finds the NEW location not in DB
- Single subscriber row inserted with `role='individual'`
- Auto-links to agency if companyId matches

### Scenario B: Agency owner first install (Company token, role_type=agency)
- `_detect_agency_owner()` returns True (role_type=agency)
- `_upsert_agency_owner()` creates subscribers + agency_billing rows
- **CRITICAL FIX (BUG 0):** ALL installed locations are now provisioned via `locations_to_provision = list(sub_accounts)`, not just the primary. Agent locations get pre-populated rows with `parent_agency_email` and per-location tokens.
- Existing agent locations get their tokens refreshed

### Scenario C: Individual agent reconnects
- `_update_existing_location()` finds existing row by location_id
- Updates tokens, corrects user_email to subscriber's login email
- Cleans up orphaned temp_* rows
- **No welcome email sent** (BUG 1 fix)

### Scenario D: Agency owner reconnects
- Same as C but with agency_billing update via `_upsert_agency_owner()`
- **No welcome email sent**

### Scenario E: Location-scoped token install
- `token_user_type_used = 'Location'`
- Individual agent flow always (no installedLocations call)
- Single location fetched via GET /locations/{locationId}

### Scenario F: All API calls fail, no locationId
- `_determine_primary_location()` returns (None, 'failed')
- Falls back to companyId as location_id
- Admin persistent alert created
- Minimal subscriber row created

---

## Key Bug Fixes

1. **BUG 0 (CRITICAL):** Agency owner install now provisions ALL sub-account locations, not just primary. `locations_to_provision = list(sub_accounts)` for agency flow. Each agent location gets:
   - Per-location token from `locationToken` API
   - `parent_agency_email` set to agency owner
   - `company_id` set
   - Placeholder email `install_{loc_id}@pending.grokbot` (real email set when agent logs in)
   - `role='individual'`, `onboarding_status='pending'`
   - If location already has a real agent row: tokens refreshed silently, no row overwrite
2. **BUG 1:** Welcome email only sent for new installs (`is_new_install=True`), not reconnects.
3. **BUG 2:** URL location_id is Priority 1 in `_determine_primary_location()`, before DB heuristic.
4. **BUG 3:** Agency owner reconnect uses `_update_existing_location()` by location_id first.
5. **Placeholder emails for agent locations:** Agency sub-account locations use `install_{loc_id}@pending.grokbot` to avoid UNIQUE constraint violations on the email column (agency owner's email already used for primary row).

---

## Open Questions / Edge Cases

1. **Agency owner with 100+ locations:** `_generate_location_tokens()` makes a sequential HTTP call per location. At 100 locations with 10s timeout each, this could take 15+ minutes. Consider batching or async in future.
2. **Location token generation failure:** If `locationToken` fails for some locations, those agents get a subscriber row but with the Company token (not location-scoped). They'll need to reconnect individually.
3. **Race condition:** If two agency installs for the same company happen simultaneously, both may try to create subscriber rows. The savepoint/rollback pattern handles this gracefully.

---

## Preserved Unchanged
- `oauth_initiate()` — No changes
- `refresh_subscribers()` — No changes
- `oauth_loading()` — No changes
- `GHL_OAUTH_SCOPES` — No changes
- All imports — Identical to original
- GHL SSO flow — Preserved as-is
