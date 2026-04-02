# GHL OAuth Enterprise Flow -- Design Spec

**Author:** researcher agent
**Date:** 2026-04-02
**Scope:** `blueprints/oauth.py` OAuth callback flow redesign

---

## CRITICAL BUG: Agency Owner Install Provisions 0 Sub-Account Locations

**Production evidence (2026-04-01, John Burkett install):**
```
Token exchange SUCCESS with user_type=Company. locationId=oI2ieFbTRIO04BNBgtwE, companyId=Ja7ckCu437FjQkBMePep
User roles: type=agency, locationIds=['oI2ieFbTRIO04BNBgtwE']
Agency owner with primary location detected
Step 4 complete: 0 locations fetched
Step 7c: Provisioning 0 NEW subscriber rows (agency_flow=True)
```

**Root cause:** Two compounding issues:

### Issue A: installedLocations may have returned empty/failed
The log shows "0 locations fetched" at Step 4. Either `installedLocations` API failed, returned empty, or all `locationToken` calls failed. When `sub_accounts` is empty, the fallback at lines 915-964 only runs if `user_location_ids` is present AND `primary_location_id` is not yet set. Since the token exchange already returned `locationId=oI2ieFbTRIO04BNBgtwE`, `primary_location_id` was set, so the fallback was skipped. Result: `sub_accounts = []`, `num_subs = 0`.

The fallback at lines 1067-1076 then synthesizes a minimal entry `[{'id': primary_location_id}]` so `num_subs = 1`. But this synthetic entry has no location token -- it just preserves the company token.

### Issue B (THE REAL BUG): Line 1369 filters to ONLY the primary location
Even when `installedLocations` succeeds and `sub_accounts` has multiple entries with per-location tokens, the provisioning filter is:
```python
locations_to_provision = [s for s in sub_accounts if s['id'] == primary_location_id]
```
This **explicitly discards all sub-account locations**. The comment at lines 1365-1368 says:
> "Sub-account locations are NOT provisioned here -- each agent creates their own row when they install or get invited via auto-link (companyId match)."

This "iFrame self-provision" approach means agents must individually install the app from the GHL marketplace. The agency owner's install does NOT pre-provision their agents. This is the core design flaw.

### What the enterprise flow MUST do instead:
When an agency owner installs with a Company token:
1. **ALWAYS call `installedLocations`** to discover all sub-account locations
2. **Generate `locationToken` for EACH** installed location
3. **Create/update subscriber rows for ALL locations** -- not just the agency owner's primary
4. Agency owner's row gets `role='agency_owner'`; agent locations get pre-populated rows with `parent_agency_email` set and tokens stored
5. If an agent location already has a subscriber row (existing agent), refresh their tokens and set `parent_agency_email` + `company_id`
6. Only skip locations that are NOT installed (i.e., not in `installedLocations` response)

### Impact of current bug:
- Agency owner installs -> gets their own row -> agents get NOTHING
- Agents must individually find and install the app from GHL marketplace
- No token pre-population, no auto-linking at install time
- Agency KPI dashboard shows 0 agents until they self-install

### Additional finding: GHL CAN return locationId in Company token
The production log shows `locationId=oI2ieFbTRIO04BNBgtwE` in the Company token exchange response. This is the agency owner's primary location. The code correctly captures it at line 363. This is valid and should be used as the agency owner's `location_id` in their subscriber row.

---

## Current State Analysis

### What the Code Does Today (1878 lines)

The OAuth callback (`GET /oauth/callback`) handles three flows:
1. **Marketplace installs** (no state param) -- GHL user installs from marketplace
2. **Website user reconnects** (state="website_user:nonce") -- logged-in user reconnects CRM
3. **GHL SSO** (state="ghl_sso:nonce") -- one-click login via GHL

The flow executes 9 steps:
1. Token exchange (Company first, Location fallback)
2. Get user info (`GET /users/{userId}`)
3. Detect agency status (role_type + companyId + DB check)
4. Fetch all locations (installedLocations + locationToken)
5-6. Determine tier and primary location
7. Database operations (agency_billing + subscribers upsert)
8. Post-onboarding (logging, email)
9. Login and redirect

### Bugs and Fragile Patterns Found

#### BUG 0: Agency Owner Install Does NOT Provision Sub-Account Locations (CRITICAL -- PRODUCTION BROKEN)

**Location:** Line 1369
**Issue:** `locations_to_provision = [s for s in sub_accounts if s['id'] == primary_location_id]` explicitly filters to ONLY the agency owner's primary location. All sub-account locations in `sub_accounts` are discarded. Even when `installedLocations` + `locationToken` successfully discovers and generates tokens for 10 agent locations, only the owner's 1 location gets a subscriber row.

**Production impact:** John Burkett (agency owner) installed 2026-04-01. Result: 0 agent locations provisioned. Agents must individually install from marketplace -- defeats the purpose of agency install.

**Root design flaw:** The comment at line 1365 says "Sub-account locations are NOT provisioned here -- each agent creates their own row when they install." This was a shortcut that broke the product. Enterprise flow requires agency owner install to pre-provision ALL discovered locations.

**Fix:** For `use_agency_flow=True`, set `locations_to_provision = sub_accounts` (all installed locations). For each:
- If location matches `primary_location_id`: create with `role='agency_owner'`
- If location already has a subscriber row: UPDATE tokens + set `parent_agency_email`
- If location is new: INSERT with `role='individual'`, `parent_agency_email=agency_email`, tokens from `locationToken`

#### BUG 1: Welcome Email Sent to Wrong User on Reconnect (CRITICAL)

**Location:** Lines 1686-1720
**Issue:** Welcome email is sent unconditionally after every OAuth callback, including reconnects. When an agency owner reconnects, the "Welcome to InsuranceGrokBot" email fires again. Worse, when a marketplace install hits an existing subscriber's location (reconnect path at line 1251), `user_email` gets corrected to the existing subscriber's login email (line 1262-1268), but the email still sends -- meaning the existing user gets a confusing "welcome" email triggered by someone else's install.

**Fix:** Only send welcome email when `locations_to_provision` is non-empty (new install), not on reconnects.

#### BUG 2: Multiple New Locations -- Last-One-Wins Heuristic is Fragile (MEDIUM)

**Location:** Lines 891-898
**Issue:** When multiple locations are new (not in DB), the code picks `new_loc_ids[-1]` assuming "most recently added tends to be last." This is undocumented GHL behavior and may not hold. If GHL returns locations in alphabetical or random order, the wrong location gets picked.

**Fix:** Use `url_location_id` as the primary signal (GHL passes the specific installed location). Only fall back to DB-check heuristic when `url_location_id` is absent.

#### BUG 3: Agency Owner Reconnect Creates Duplicate subscribers Row (MEDIUM)

**Location:** Lines 1150-1232
**Issue:** The agency flow at Step 7A always runs an INSERT...ON CONFLICT(email) into subscribers, then Step 7B runs a separate UPDATE by location_id. When an agency owner reconnects with a different email (e.g., CRM email vs login email), the ON CONFLICT(email) clause in Step 7A may create a NEW row if the email doesn't match, while Step 7B updates the existing row. This could leave orphaned subscribers rows.

**Fix:** Step 7A should check by location_id first (like Step 7B does), not rely solely on email conflict.

#### BUG 4: companyId Used as location_id Fallback Creates Invalid State (LOW)

**Location:** Lines 1081-1109
**Issue:** When ALL location discovery fails, `company_id` is used as `location_id` in the subscribers table. This creates a row with a fake `location_id` that will never match any GHL webhook payloads. The persistent alert is logged but there's no automatic recovery path.

**Fix:** This is acceptable as a last-resort safety net, but should be flagged in admin dashboard for manual correction. Currently handled (persistent alert exists).

#### BUG 5: Token Storage Race Between Step 7A and Step 7B (LOW)

**Location:** Lines 1150-1304
**Issue:** For agency owners, Step 7A inserts into subscribers with encrypted tokens, then Step 7B overwrites those same tokens with a separate UPDATE. The Step 7B UPDATE uses the same `enc_access_token`/`enc_refresh_token` so no data loss occurs, but the double-write is wasteful and confusing.

**Fix:** For agency owners with existing rows, skip Step 7A insert and let Step 7B handle the update.

#### FRAGILE 1: Email Resolution Has 5 Fallbacks with No Clear Priority

**Location:** Lines 514-632
**Issue:** The email resolution chain tries: (1) logged-in user, (2) token_data, (3) marketplace_installs, (4) subscribers by userId, (5) placeholder. This is robust but makes it hard to reason about which email wins. For marketplace installs where the user isn't logged in, the email comes from GHL's `/users/` endpoint -- this is the CRM email, not necessarily the user's login email.

**Impact:** When user's CRM email differs from their login email, the welcome email goes to the CRM email (which may not be monitored). The Step 7B correction at line 1262-1268 fixes the DB but the email was already composed with the wrong address.

#### FRAGILE 2: installedLocations API Has Two Parsing Attempts

**Location:** Lines 754-801
**Issue:** The response parsing tries `locations` key, then `installedLocations` key, then checks if the response itself is a list. Then if that fails, retries without the `appId` parameter. This multi-attempt parsing suggests GHL's API is unstable and returns different shapes.

**Impact:** Works but is brittle. Any new GHL API version change could break parsing.

#### FRAGILE 3: SAVEPOINT/ROLLBACK Pattern for Email Uniqueness

**Location:** Lines 1514-1579
**Issue:** The code uses PostgreSQL savepoints to handle email uniqueness violations. If INSERT fails on email conflict, it rolls back to savepoint and falls back to UPDATE by email. This is correct but complex and hard to follow.

#### FRAGILE 4: oauth.write Scope Now Present but Memory Says Otherwise

**Issue:** The reference memory (`reference_ghl_oauth_company_token.md`) says "We do NOT have oauth.write scope" but the current code at line 61 includes `oauth.write` in `GHL_OAUTH_SCOPES`. The code actively uses `POST /oauth/locationToken` (line 810-851). Memory is outdated.

---

## GHL API Reference

### POST /oauth/token (Token Exchange)

**Request body:**
```
client_id: string (GHL_CLIENT_ID)
client_secret: string (GHL_CLIENT_SECRET)
grant_type: "authorization_code"
code: string (from callback URL)
redirect_uri: string (must match registered)
user_type: "Company" | "Location"
```

**Response (Company user_type):**
```json
{
  "access_token": "eyJ...",
  "token_type": "Bearer",
  "expires_in": 86400,
  "refresh_token": "abc...",
  "scope": "contacts.readonly conversations.write ...",
  "userType": "Company",
  "companyId": "GNb7aIv4rQFVb9iwNl5K",
  "locationId": null,
  "userId": "usr_abc123"
}
```

**Response (Location user_type):**
```json
{
  "access_token": "eyJ...",
  "token_type": "Bearer",
  "expires_in": 86400,
  "refresh_token": "abc...",
  "scope": "contacts.readonly conversations.write ...",
  "userType": "Location",
  "companyId": "GNb7aIv4rQFVb9iwNl5K",
  "locationId": "HjiMUOsCCHCjtxzEf8PR",
  "userId": "usr_abc123"
}
```

**Key observations:**
- Company tokens have `locationId: null` -- they are company-scoped
- Both return `companyId` and `userId`
- `expires_in` can be `null` (not just absent) -- must coerce with `int(x or 86400)`
- Company exchange may fail for sub-account users (they lack company-level access)
- Location exchange may succeed for agency admins (gives location-scoped token, less powerful)

### GET /users/{userId}

**Requires:** Company-scoped token (returns 401/403 with Location token)

**Response:**
```json
{
  "id": "usr_abc123",
  "name": "John Doe",
  "firstName": "John",
  "lastName": "Doe",
  "email": "john@example.com",
  "phone": "+1234567890",
  "roles": {
    "type": "agency",
    "locationIds": []
  }
}
```

**Key fields:**
- `roles.type`: `"agency"` = agency-level user, `"account"` = sub-account/location user
- `roles.locationIds`: array of location IDs the user has access to
  - Agency owners: typically empty `[]` (they have access to all locations)
  - Sub-account users: contains specific location IDs
- When `roles.type == "account"` and `locationIds` has entries, user is definitively an individual agent

### GET /oauth/installedLocations

**URL:** `GET /oauth/installedLocations?companyId={id}&appId={id}`
**Requires:** Company-scoped token

**Response:**
```json
{
  "locations": [
    {
      "_id": "HjiMUOsCCHCjtxzEf8PR",
      "name": "Devon's Location",
      "timezone": "US/Central"
    }
  ],
  "isInstalled": true,
  "installToFutureLocations": false
}
```

**Quirks:**
- Response key may be `"locations"` or `"installedLocations"` depending on GHL version
- May also return a raw array (no wrapper object)
- Location objects use `_id`, `id`, or `locationId` inconsistently
- Can fail with 400/422 if `appId` param is incorrect -- retry without `appId`
- Only returns locations where the app is actually installed (vs `/locations/search` which returns ALL)

### POST /oauth/locationToken

**URL:** `POST /oauth/locationToken`
**Requires:** Company-scoped token + `oauth.write` scope
**Request body:**
```json
{
  "companyId": "GNb7aIv4rQFVb9iwNl5K",
  "locationId": "HjiMUOsCCHCjtxzEf8PR"
}
```

**Response:**
```json
{
  "access_token": "eyJ...",
  "token_type": "Bearer",
  "expires_in": 86400,
  "refresh_token": "abc...",
  "scope": "contacts.readonly ..."
}
```

**Key:** This generates a location-scoped token from a company token. Required for per-location API calls. The IGB codebase now has `oauth.write` scope and uses this endpoint.

### GET /locations/search

**URL:** `GET /locations/search?companyId={id}`
**Requires:** Company-scoped token + `locations.readonly` scope

Returns all locations under the company (not just installed ones). Useful as a fallback when `installedLocations` fails.

### Callback URL Parameters

When GHL redirects to `/oauth/callback`, it passes:
- `code` -- authorization code
- `state` -- echo of the state parameter from initiate (or absent for marketplace installs)
- `locationId` -- (sometimes) the specific location being installed. NOT always present.

---

## Enterprise Flow Decision Tree

```
OAuth Callback (/oauth/callback)
  |
  +-- Validate state (CSRF)
  |     marketplace install: state=None (bypass)
  |     website_user: state="website_user:nonce" (validate against session)
  |     ghl_sso: state="ghl_sso:nonce" (validate against session)
  |
  +-- Step 1: Token Exchange
  |     Try Company user_type first (stronger token, can discover locations)
  |     Fallback to Location user_type (sub-account users who can't get Company)
  |     |
  |     +-- Company succeeds:
  |     |     access_token = company-scoped
  |     |     locationId = null (usually)
  |     |     companyId = present
  |     |     Can call: /users/{userId}, /oauth/installedLocations, /oauth/locationToken
  |     |
  |     +-- Company fails, Location succeeds:
  |     |     access_token = location-scoped
  |     |     locationId = present (usually, sometimes null -- use url_location_id fallback)
  |     |     companyId = present (usually)
  |     |     Cannot call: /users/{userId} (401), /oauth/installedLocations (requires Company)
  |     |
  |     +-- Both fail: abort with error
  |
  +-- Step 2: Get User Info
  |     GET /users/{userId} -- only works with Company token
  |     Extract: email, name, roles.type, roles.locationIds
  |     Location token: 401/403 -- graceful fallback to token_data fields
  |
  +-- Step 3: Agency Detection
  |     PRIMARY SIGNAL: roles.type from /users/{userId}
  |     |
  |     +-- roles.type == "account":
  |     |     DEFINITIVELY NOT agency owner. Individual agent.
  |     |
  |     +-- roles.type == "agency" + companyId present:
  |     |     Check DB: get_agency_by_company_id(company_id)
  |     |     |
  |     |     +-- No existing agency, or existing agency email == this user's email:
  |     |     |     IS agency owner (new or reconnect)
  |     |     |
  |     |     +-- Existing agency owned by different email:
  |     |           NOT agency owner. Individual under existing agency.
  |     |
  |     +-- No roles.type (Location token, /users/ failed):
  |           companyId + no locationId + no locationIds + Company token = agency owner
  |           Otherwise = individual (safe default)
  |
  +-- Step 4: Location Discovery (Company token path)
  |     |
  |     +-- GET /oauth/installedLocations?companyId={id}&appId={id}
  |     |     Returns locations where THIS APP is installed
  |     |     Fallback: retry without appId
  |     |
  |     +-- For each installed location: POST /oauth/locationToken
  |     |     Generate per-location access token
  |     |     Build sub_accounts[] with location-specific tokens
  |     |
  |     +-- Primary Location Selection (CRITICAL):
  |           Priority 1: url_location_id (from GHL callback URL param)
  |           Priority 2: DB check -- find location NOT in subscribers table (new install)
  |           Priority 3: If all locations exist in DB -- reconnect (use first)
  |           Priority 4: user_location_ids from /users/ roles (fallback)
  |
  +-- Step 4 (Location token path):
  |     GET /locations/{locationId} -- fetch single location details
  |     Or synthesize minimal entry from token data
  |
  +-- Steps 5-6: Determine Tier & Primary
  |     Agency owners: plan_tier='agency_owner', use_agency_flow=True
  |     Website users: keep existing tier
  |     Marketplace: plan_tier='individual'
  |
  +-- Step 7: Database Operations
  |     |
  |     +-- 7A: Agency owner flow
  |     |     INSERT/UPSERT subscribers for owner's primary location (role=agency_owner)
  |     |     INSERT/UPSERT agency_billing (metadata only)
  |     |     Auto-populate whitelabel_config with company name
  |     |
  |     +-- 7B: Reconnect/Reinstall Sync (for primary location)
  |     |     Look up by location_id first (definitive match)
  |     |     If found: UPDATE tokens, crm_email. CORRECT user_email to existing login email.
  |     |     If not found by location, check by email (may own different location)
  |     |
  |     +-- 7C: Provision Subscriber Rows
  |     |     |
  |     |     +-- Agency flow (use_agency_flow=True):
  |     |     |     locations_to_provision = ALL sub_accounts (not just primary!)
  |     |     |     For each location:
  |     |     |       - If location == primary AND already handled in 7A/7B: skip
  |     |     |       - If location already has subscriber row (different user):
  |     |     |           UPDATE tokens + parent_agency_email + company_id
  |     |     |           NEVER overwrite email/role/stripe
  |     |     |       - If location is new (no subscriber row):
  |     |     |           INSERT with email=NULL, role=individual,
  |     |     |           parent_agency_email=owner, tokens from locationToken
  |     |     |
  |     |     +-- Individual flow (use_agency_flow=False):
  |     |           locations_to_provision = [primary_location_id only]
  |     |           Exactly ONE location per email (UNIQUE constraint)
  |     |           Auto-link to agency via companyId match
  |     |           SAVEPOINT for email uniqueness handling
  |     |
  |     +-- COMMIT
  |
  +-- Step 8: Post-Onboarding
  |     Log to webhook_logs
  |     Stamp install_completed_at
  |     Mark marketplace install as OAuth-complete
  |     Location fallback alert (if API returned 0 locations)
  |     Welcome email (agency vs individual variant)
  |
  +-- Step 9: Login & Redirect
        session.clear() (prevent session fixation)
        User.get(user_email) -> login_user()
        Redirect: marketplace -> dashboard, SSO -> dashboard, website -> dashboard
        No password? -> /set-password
```

---

## Scenario Specifications

### Scenario 1: New Individual Agent Installs (Company Owns Multiple Locations)

**Context:** Devon (agency owner) has 10 GHL locations. Nick (new agent) installs from marketplace on his specific location.

**Inputs:**
- `code` from GHL callback
- `url_location_id` = Nick's location ID (usually present)
- Token exchange: Company succeeds (Nick has company-level access as admin user)
- `/users/{userId}` returns: `roles.type = "account"`, `locationIds = ["nick_loc_id", "devon_loc_id", ...]`

**Decisions:**
- `token_user_type_used = "Company"`
- `/users/` returns `roles.type = "account"` -> `is_agency_owner = False`
- `installedLocations` returns locations where app is installed (including Nick's)
- `locationToken` generates per-location tokens
- Primary selection: `url_location_id` matches Nick's location -> selected
- DB check confirms Nick's location is NOT in subscribers yet -> new install
- `auto_linked_agency_email` set if Devon's agency exists in `agency_billing` for this `company_id`

**DB Writes:**
- `subscribers`: INSERT new row for Nick's `location_id`
  - `email = nick_email`, `role = 'individual'`
  - `access_token = location-scoped token` (from locationToken)
  - `company_access_token = company-scoped token`
  - `parent_agency_email = devon_email` (auto-linked)
  - `company_id = companyId`
- Other agents' locations: tokens refreshed via UPDATE (if company install), `parent_agency_email` set
- `agency_billing`: NOT touched (Nick is not agency owner)

**Email:** Welcome email to Nick (individual variant)

**Redirect:** `/dashboard` (or `/set-password` if no password)

### Scenario 2: Agency Owner Installs for First Time

**Context:** Devon installs from marketplace. First install for this company.

**Inputs:**
- Token exchange: Company succeeds
- `/users/{userId}` returns: `roles.type = "agency"`, `locationIds = []`
- `get_agency_by_company_id(company_id)` returns None (no existing agency)

**Decisions:**
- `is_agency_owner = True` (roles.type=agency + no existing agency)
- `installedLocations` discovers all installed locations
- `locationToken` generates per-location tokens
- Primary: `url_location_id` if present, else first installed location
- Agency flow enabled

**DB Writes:**
- `subscribers`: INSERT new row
  - `email = devon_email`, `location_id = primary_location_id`
  - `role = 'agency_owner'`
  - `access_token = location-scoped token`
  - `company_access_token = company-scoped token`
- `agency_billing`: INSERT new row
  - `agency_email = devon_email`
  - `company_id`, `company_name`, `company_owner_name/email/phone`
  - `whitelabel_config` auto-populated with company name
- Other installed locations: tokens refreshed for existing agents, `parent_agency_email` set

**Email:** Agency owner welcome email to Devon

**Redirect:** `/agency-dashboard` (or `/set-password`)

### Scenario 3: Agency Owner Reconnects

**Context:** Devon already in `agency_billing` and `subscribers`. Reconnects OAuth.

**Inputs:**
- Token exchange: Company succeeds
- `/users/{userId}` returns: `roles.type = "agency"`
- `get_agency_by_company_id(company_id)` returns Devon's existing row

**Decisions:**
- `is_agency_owner = True` (existing agency email matches)
- Step 7A: UPSERT subscribers ON CONFLICT(email) -- updates tokens
- Step 7A: UPSERT agency_billing ON CONFLICT(agency_email) -- updates metadata
- Step 7B: existing_by_location found -> UPDATE tokens
- Step 7C: `locations_to_provision` filtered to exclude existing primary -> empty
- `user_email` corrected to existing login email if different from CRM email

**DB Writes:**
- `subscribers`: UPDATE existing row (tokens, crm_email, company tokens)
- `agency_billing`: UPDATE existing row (company metadata)
- Other agents: tokens refreshed

**Email:** Welcome email sends (BUG -- should NOT send on reconnect)

**Redirect:** `/agency-dashboard`

### Scenario 4: Individual Agent Reconnects

**Context:** Nick already in `subscribers`. Reconnects from dashboard.

**Inputs:**
- `is_website_user = True` (state="website_user:nonce")
- `current_user.is_authenticated = True`
- Token exchange: Company or Location succeeds

**Decisions:**
- Agency detection: roles.type=account -> NOT agency
- Step 7B: `existing_by_location` found -> UPDATE tokens
- Step 7C: primary already exists -> `locations_to_provision` empty
- `user_email = current_user.email` (from logged-in session -- canonical)

**DB Writes:**
- `subscribers`: UPDATE existing row (tokens, crm_email)

**Email:** Welcome email sends (BUG -- should NOT send on reconnect)

**Redirect:** `/dashboard`

### Scenario 5: Location-Scoped Token Install

**Context:** Sub-account user installs. Only gets Location token (Company exchange failed).

**Inputs:**
- Token exchange: Company fails (400), Location succeeds
- `locationId` from token response (or `url_location_id` fallback)
- `/users/{userId}` returns 401 (Location token can't access)
- `user_role_type = ""` (unknown)

**Decisions:**
- `is_agency_owner = False` (no roles.type, defaults to individual)
- No `installedLocations` (requires Company token)
- No `locationToken` (requires Company token)
- Primary = `locationId` from token or URL
- Single location fetched via `GET /locations/{locationId}`
- Email from `token_data.userEmail` (no /users/ access)

**DB Writes:**
- `subscribers`: INSERT or UPDATE for the single location
  - `access_token = location-scoped token`
  - `company_access_token = NULL` (no company token)

**Email:** Welcome email to user

**Redirect:** `/dashboard`

### Scenario 6: Agency Owner Installs -- Enterprise Multi-Location Provisioning (NEW REQUIRED BEHAVIOR)

**Context:** John Burkett (agency owner) installs from marketplace. Company has 5 locations with agents.

**Inputs:**
- Token exchange: Company succeeds
- `locationId` = `oI2ieFbTRIO04BNBgtwE` (owner's primary, returned in Company token response)
- `/users/{userId}` returns: `roles.type = "agency"`, `locationIds = ['oI2ieFbTRIO04BNBgtwE']`
- `get_agency_by_company_id(company_id)` returns None (first install)
- `installedLocations` returns 5 locations where app is installed
- `locationToken` succeeds for all 5

**Decisions:**
- `is_agency_owner = True`
- `primary_location_id = oI2ieFbTRIO04BNBgtwE` (from token response)
- `sub_accounts` = 5 entries with per-location tokens
- **NEW: `locations_to_provision = sub_accounts` (ALL 5, not just primary)**

**DB Writes:**
- `subscribers` row for owner's primary location:
  - `email = john_email`, `location_id = oI2ieFbTRIO04BNBgtwE`
  - `role = 'agency_owner'`
  - `access_token = location-scoped token` (from locationToken for this location)
  - `company_access_token = company-scoped token`
  - `parent_agency_email = john_email` (self-referential for owner)
  - `company_id = Ja7ckCu437FjQkBMePep`
- `agency_billing` row:
  - `agency_email = john_email`, `company_id`, metadata
- For each of the other 4 agent locations:
  - **If location already has subscriber row (existing agent):**
    - UPDATE: `access_token`, `refresh_token`, `token_expires_at` (refreshed via locationToken)
    - UPDATE: `parent_agency_email = john_email`, `company_id`
    - DO NOT change: `email`, `role`, `subscription_tier`, `stripe_customer_id`
  - **If location is NEW (no subscriber row):**
    - INSERT: `location_id`, `email = NULL or placeholder`, `role = 'individual'`
    - `access_token` = location-scoped token, `parent_agency_email = john_email`
    - `company_id`, `onboarding_status = 'pending'`
    - Note: email is unknown for unregistered agents. The row pre-populates tokens + agency linkage. When the agent later claims or registers, their email fills in.

**Email:** Agency owner welcome email to John. No email to agents (they haven't registered yet).

**Redirect:** `/agency-dashboard`

**Key constraint:** Agents' subscriber rows must NEVER have their `email` column overwritten by the agency owner's email. Each location = one subscriber. The agency owner's install populates tokens and linkage, not identity.

### Scenario 7: installedLocations API Fails

**Context:** Company token obtained but `installedLocations` returns error/empty.

**Inputs:**
- Token exchange: Company succeeds
- `/oauth/installedLocations` returns 400/422/500/empty
- Retry without `appId` also fails

**Decisions:**
- `installed_locs = []`, `sub_accounts = []`
- Fallback to `user_location_ids` from `/users/{userId}` roles
- DB check on `user_location_ids`: find which are new vs existing
- If all exist: reconnect. If one new: use it. If multiple new: use last.
- If no `user_location_ids` either: use `url_location_id` or `company_id` as last resort

**DB Writes:** Same as whichever scenario matches (new install or reconnect)

**Additional:** `using_location_fallback = True` -> persistent alert saved

---

## DB Write Specification

### subscribers Table Operations

| Scenario | Operation | Conflict Key | Fields Written |
|----------|-----------|-------------|----------------|
| New individual install | INSERT | location_id (PK) | email, location_id, crm_email, full_name, role='individual', subscription_tier, parent_agency_email, company_id, access_token, refresh_token, token_expires_at, timezone, crm_user_id, onboarding_status='pending', oauth_app_type, company_access_token, company_refresh_token |
| New agency owner (primary loc) | INSERT | location_id (PK) | email, location_id, role='agency_owner', + all tokens (location-scoped + company-scoped) |
| **Agency install: existing agent loc** | **UPDATE** | **WHERE location_id=%s** | **access_token, refresh_token, token_expires_at, parent_agency_email, company_id. NEVER touch email/role/stripe** |
| **Agency install: new agent loc** | **INSERT** | **location_id (PK)** | **location_id, email=NULL, role='individual', parent_agency_email, company_id, access_token, refresh_token, onboarding_status='pending'** |
| Reconnect (by location_id) | UPDATE | WHERE location_id=%s | crm_email, access_token, refresh_token, token_expires_at, crm_user_id, oauth_app_type, role (if agency), parent_agency_email (if agency), company_id, company_access_token, company_refresh_token, onboarding_status (pending->claimed) |
| Reconnect (by email, different location) | UPDATE | WHERE email=%s | crm_email, access_token, refresh_token, token_expires_at, crm_user_id, oauth_app_type, company_access_token, company_refresh_token |
| Company install refreshes agent tokens | UPDATE | WHERE location_id=%s | parent_agency_email, company_id, access_token, refresh_token, token_expires_at |
| Email conflict on INSERT | ROLLBACK+UPDATE | WHERE email=%s | location_id (changes!), crm_email, access_token, refresh_token, token_expires_at, crm_user_id, oauth_app_type, company_id, parent_agency_email, company_access_token, company_refresh_token |

**NEW: Agency owner install multi-location provisioning rules:**
1. Owner's primary location: full INSERT with `role='agency_owner'`, all tokens
2. Each other installed location: check `SELECT email FROM subscribers WHERE location_id = %s`
   - If row exists with a DIFFERENT email: UPDATE tokens + set parent_agency_email + company_id. **Never change email, role, subscription_tier, stripe_customer_id.**
   - If row exists with same email as owner: this shouldn't happen (two locations same email). Log warning, skip.
   - If no row exists: INSERT with `location_id`, `email = NULL` (agent hasn't registered yet), `role = 'individual'`, `parent_agency_email = owner_email`, `company_id`, tokens from locationToken, `onboarding_status = 'pending'`
3. The `email = NULL` case requires `subscribers.email` to accept NULL. Currently it's `TEXT UNIQUE` -- a NULL email is allowed by PostgreSQL's UNIQUE constraint (NULLs are not equal), but code that does `WHERE email = %s` won't find these rows. The agent later fills in their email when they register/claim.
4. Alternative to NULL email: use a placeholder like `pending_{location_id}@placeholder.grokbot` -- but this pollutes the email space. NULL is cleaner.

### agency_billing Table Operations

| Scenario | Operation | Conflict Key | Fields Written |
|----------|-----------|-------------|----------------|
| New agency owner | INSERT | agency_email (PK) | agency_email, company_id, company_name, company_owner_name, company_owner_email, company_owner_phone |
| Agency reconnect | UPSERT | ON CONFLICT(agency_email) | company_id, company_name, company_owner_name, company_owner_email, company_owner_phone, whitelabel_config (if company_name present) |

### Token Storage Strategy

| Token Type | Column | When Stored |
|------------|--------|-------------|
| Location-scoped access | `access_token` | Always (primary operational token) |
| Location-scoped refresh | `refresh_token` | Always |
| Company-scoped access | `company_access_token` | When Company token exchange succeeds |
| Company-scoped refresh | `company_refresh_token` | When Company token exchange succeeds |
| Token expiry | `token_expires_at` | NOW() + expires_in seconds |
| Company token expiry | `company_token_expires_at` | NOW() + expires_in seconds (when company token present) |

**Key rule:** When `POST /oauth/locationToken` generates a location-specific token, THAT becomes `access_token` (primary). The original company token goes to `company_access_token`. This ensures per-location API calls work correctly.

---

## Email Specification

| Scenario | Recipient | Email Type | Template |
|----------|-----------|------------|----------|
| New individual install | user_email | Individual welcome | `_build_welcome_email()` |
| New agency owner | user_email | Agency owner welcome | `_build_agency_owner_welcome_email()` |
| Reconnect (any) | **NONE** (BUG: currently sends) | -- | -- |
| Ghost install (no email found) | ADMIN_EMAILS[0] | Ghost install alert | Inline HTML |
| companyId fallback | user_email (via persistent_alert) | Location discovery issue | Persistent alert |

**Required fix:** Welcome email should ONLY send when `locations_to_provision` is non-empty (i.e., new rows were created). The boolean check:
```python
is_new_install = len(locations_to_provision) > 0 and not existing_row
```

---

## Error Handling Specification

### Token Exchange Failures

| Failure | Current Handling | Recommended |
|---------|-----------------|-------------|
| Company 400 | Try Location | Correct |
| Location 400 | Abort | Correct |
| Company 5xx | Try Location | Should retry once before fallback |
| Both fail | Flash error, redirect /home | Correct |
| No access_token in response | Abort | Correct |
| `expires_in: null` | Coerced to 86400 | Correct |

### /users/{userId} Failures

| Failure | Current Handling | Recommended |
|---------|-----------------|-------------|
| 401/403 (Location token) | Graceful skip, use token_data | Correct |
| 5xx | Log warning, continue | Should retry once |
| Non-JSON response | Log warning, continue | Correct |
| No userId in token_data | Skip entirely | Correct |

### installedLocations Failures

| Failure | Current Handling | Recommended |
|---------|-----------------|-------------|
| 400/422 | Retry without appId | Correct |
| 5xx | Log, fall to user_location_ids | Should retry once |
| Empty/null response | Fall to user_location_ids | Correct |
| Parse error | Log, fall to user_location_ids | Correct |
| All fallbacks fail | Use url_location_id or companyId | Correct (with alert) |

### locationToken Failures

| Failure | Current Handling | Recommended |
|---------|-----------------|-------------|
| Per-location failure | Log warning, skip location | Correct |
| All locations fail | Fall to company token as-is | Correct |
| No access_token in response | Skip location | Correct |

### Database Failures

| Failure | Current Handling | Recommended |
|---------|-----------------|-------------|
| Connection failed | Retry 3x, abort with flash | Correct |
| Email uniqueness violation | SAVEPOINT + UPDATE fallback | Correct but complex |
| location_id conflict | ON CONFLICT DO UPDATE | Correct |
| General exception | Rollback, flash error | Correct |

---

## Implementation Notes

### Critical Gotchas for the Implementer

1. **Token exchange order MUST be Company first, Location second.** Company tokens are strictly more powerful -- they can do everything Location tokens can, plus `/users/`, `/oauth/installedLocations`, and `/oauth/locationToken`. The current code already does this correctly (line 298).

2. **`oauth.write` scope is now active.** The memory file says otherwise -- it's outdated. The code at line 61 includes `oauth.write` and actively uses `POST /oauth/locationToken` at line 818.

3. **Never trust `roles.type` alone for agency detection.** Always cross-reference with `get_agency_by_company_id()`. A user with `roles.type="agency"` might be a second admin, not the agency owner.

4. **`url_location_id` is the most reliable location signal.** GHL passes this on the callback redirect when the user picks a specific location during install. It should always be Priority 1 for primary location selection.

5. **Email correction at line 1262 is critical.** When a marketplace install hits an existing subscriber's location, `user_email` must be corrected to the existing subscriber's login email. Otherwise `User.get(user_email)` at Step 9 may find a wrong/stale account (e.g., temp_* from Stripe checkout).

6. **Welcome email must be gated on new install, not sent on every callback.** This is the most impactful remaining bug.

7. **`expires_in` can be `null`, not just absent.** Python's `.get('expires_in')` returns `None` when the key exists with value `null`. The `int(x or 86400)` pattern at line 368 handles this correctly.

8. **GHL's `installedLocations` response shape is unstable.** Parse defensively: try `locations` key, `installedLocations` key, and raw list. Location objects may use `_id`, `id`, or `locationId`.

9. **The SAVEPOINT pattern for email uniqueness is necessary.** PostgreSQL rolls back the entire transaction on constraint violation unless you use savepoints. The current implementation is correct.

10. **For company installs, existing agents' tokens should be refreshed.** When an agency owner reconnects, `locationToken` generates fresh tokens for all installed locations. These should be written to existing agents' subscriber rows (line 1444-1464). This is high value -- agents don't need to re-install.

11. **CRM email vs login email distinction is critical.** `crm_email` column stores the GHL email (for CRM API calls). `email` column stores the login email (for Flask-Login). Never overwrite `email` with CRM email on an existing row.

12. **Session must be cleared before login (line 1725).** Prevents session fixation attacks. OAuth state/PKCE tokens already consumed at this point.

### Recommended Priority Fixes

1. **CRITICAL: Agency owner install must provision ALL installed locations (BUG 0)** -- Line 1369 filters to primary only. Change to `locations_to_provision = sub_accounts` for agency flow. Add per-location logic: existing agents get token refresh + parent_agency_email; new locations get pre-populated rows. This is the highest-impact fix -- it's the core agency product.
2. **CRITICAL: Ensure installedLocations is always called for agency owners** -- Even if `primary_location_id` is already set from token response, still call `installedLocations` + `locationToken` to discover and provision all agent locations. The current code does call it (line 744), but the results are discarded at line 1369.
3. **HIGH: Gate welcome email on new install** -- prevents confusing duplicate emails on reconnects
4. **MEDIUM: Add retry-once for /users/ and installedLocations on 5xx** -- improves reliability
5. **MEDIUM: Simplify Step 7A/7B overlap for agency owners** -- reduce double-writes
6. **LOW: Update memory files** -- `reference_ghl_oauth_company_token.md` and `project_oauth_critical.md` have outdated claims about oauth.write scope and token exchange order
7. **LOW: Handle email=NULL for pre-provisioned agent locations** -- Ensure Flask-Login, User.get(), and dashboard queries gracefully handle subscribers rows with NULL email (agents who haven't registered yet)
