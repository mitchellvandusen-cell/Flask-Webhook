# Refactor OAuth/Agency Flow — Implementation Plan

## Summary

Restructure the OAuth flow, agency model, and dashboard architecture to:
1. **Simplify OAuth**: Remove `/agencies/` API calls and location-count classification
2. **Auto-link agencies**: Use `companyId` from OAuth to auto-associate individual agents with their agency, capturing all available company/owner metadata
3. **Unified dashboard**: Merge agency dashboard into main dashboard (one dashboard to rule everything)
4. **White-label**: Agency owners can customize company name + logo (light/dark theme handles color schemes)
5. **Agency-level predictive dialer**: Predictive dialer becomes agency-scoped (5+ agents required), coordinating across all agents with shared TCPA compliance, agent state management, and Erlang-C pacing
6. **Per-user billing**: Each agent picks their own tier (SMS Bot $99/mo, Power Dialer $149.99/mo, Pro Dialer $224.99/mo). Only predictive dialer is billed at agency level.

---

## Phase 1: Database Schema Changes (`db.py`)

### 1.1 Add `company_id` and company metadata columns to `agency_billing`
- Add `company_id TEXT` — GHL companyId (primary key for agency matching)
- Add `company_name TEXT` — company/agency name from GHL
- Add `company_owner_name TEXT` — owner's full name (None if unavailable)
- Add `company_owner_email TEXT` — owner's email (None if unavailable)
- Add `company_owner_phone TEXT` — owner's phone (None if unavailable)
- Add index on `company_id` for fast lookups

### 1.2 Add `company_id` to `subscribers`
- Add `company_id TEXT` — links individual agents to their agency
- Add index on `company_id` for fast lookups

### 1.3 Add `whitelabel_config` JSONB to `agency_billing`
Schema:
```json
{
  "company_name": "Acme Insurance Group",
  "logo_url": "https://..."
}
```
No color scheme or font — light/dark theme toggle is sufficient.

### 1.4 Remove seat-count gating
- Keep `max_seats` and `active_seats` columns for backward compat but stop enforcing them in code
- Agency tier no longer depends on location count

---

## Phase 2: OAuth Flow Refactor (`blueprints/oauth.py`)

### 2.1 Remove `/agencies/` API call
- Delete the code block (lines ~612-638) that calls `GET /agencies/` to detect agency owner
- Remove the location-count-based tier classification (agency_starter vs agency_pro)

### 2.2 New agency detection: companyId-based
- When OAuth callback receives token response with `companyId` but NO `locationId` → agency owner install
  - Save to `agency_billing` with ALL available metadata:
    - `company_id` — from token response `companyId`
    - `company_name` — from GHL company data (if available, else None)
    - `company_owner_name` — owner's name from GHL user data (if available, else None)
    - `company_owner_email` — owner's email from GHL user data (if available, else None)
    - `company_owner_phone` — owner's phone from GHL user data (if available, else None)
  - Do NOT pull all locations or provision subscriber rows
  - Extract whatever fields GHL provides in the token response / user info endpoint, leave None for anything unavailable

- When OAuth callback receives `locationId` (individual install):
  - Save to `subscribers` as individual (existing flow)
  - Also save `company_id` from token response
  - Check if `company_id` matches any `agency_billing.company_id` → if yes, auto-set `parent_agency_email`

### 2.3 Simplify provisioning
- Agency owner: ONLY creates `agency_billing` row with full metadata (no subscriber rows for sub-accounts)
- Individual: Creates `subscribers` row, checks for agency match
- Remove bulk location provisioning loop

---

## Phase 3: Unified Dashboard (`blueprints/dashboard.py`, `blueprints/agency.py`, templates)

### 3.1 Merge routes
- Remove redirect from `/dashboard` that bounces agency owners to `/agency-dashboard`
- Remove redirect from `/login` that sends agency owners to `/agency-dashboard`
- Single `/dashboard` route handles both agency owners and individuals
- Keep `/agency-dashboard` as a redirect to `/dashboard` for backward compat

### 3.2 Merge templates
- Expand `dashboard.html` with conditional sections for agency owners
- Add agency-specific sidebar items (conditionally shown):
  - "Agency Members" (under Team section) — shows all linked users + KPIs
  - Agency KPIs tab
  - Agency Call Log tab
  - Agency Settings tab (white-label config)
- All existing individual features (Dialer, SMS Config, Voice, Workflows, etc.) available to agency owners too
- Move relevant agency API endpoints to work within the unified dashboard context

### 3.3 Sidebar changes (`_sidebar.html`)
- Agency owners see everything individuals see PLUS:
  - "Agency Members" nav item (shows sub-users from `subscribers WHERE company_id = agency.company_id`)
  - "Agency KPIs" nav item
  - "White Label" nav item (in Settings)

### 3.4 Agency member discovery
- Query changes from `WHERE parent_agency_email = %s` to `WHERE company_id = %s`
- Sub-users appear automatically as they subscribe (no manual provisioning)
- Keep manual invite for agents who haven't subscribed yet

---

## Phase 4: White-Label System

### 4.1 White-label setup flow
- Agency owner sees "White Label" section in Settings tab
- Form: company name (pre-filled from GHL company metadata), logo URL
- No color scheme or font pickers — light/dark theme toggle handles that
- "Save" button saves to `agency_billing.whitelabel_config`

### 4.2 Brand injection (`_topbar.html`, `_sidebar.html`)
- Brand name replaces "InsuranceGrokBot" in topbar, sidebar logo text, page titles
- Logo URL replaces default logo if provided
- No CSS variable overrides needed — existing light/dark theme is sufficient

### 4.3 Cascade to sub-users
- When a sub-user logs in, check `company_id` → load agency's `whitelabel_config`
- Sub-users see the agency's branding (company name + logo), not IGB branding
- Only agency owner can modify white-label settings

---

## Phase 5: Agency-Level Predictive Dialer

### 5.1 Concept: Centralized predictive dialer for the agency
Based on industry best practices for predictive dialers:

**How it works:**
1. Agency owner (or designated supervisor) manages the dialer from the agency dashboard
2. Individual agents log in to their own dashboard and set their agent state (Ready/Not Ready/Break/Wrap-Up)
3. The predictive engine auto-dials leads based on the number of Ready agents
4. When a lead answers, the system routes the connected call to the next available Ready agent
5. Agent automatically moves to "On Call" state, then "Wrap-Up" after call ends
6. Erlang-C model calculates optimal dial ratio based on ALL agents' combined metrics

**Key differences from current per-individual dialer:**
- TCPA compliance tracked at agency level (rolling 30-day abandon rate across ALL agents)
- Agent state tracked across all agency members (shared Ready/Not Ready pool)
- Callback queue is agency-wide
- Predictive stats computed from all agents' combined call history
- Dial ratio recommendations consider total available agents (not just one)

### 5.2 Gate: 5+ linked users required
- Check `SELECT COUNT(*) FROM subscribers WHERE company_id = %s` >= 5
- Show upgrade prompt if fewer than 5 agents

### 5.3 Predictive engine refactor (`voice/predictive_engine.py`)
- Add `company_id` parameter to `TCPAComplianceTracker` methods
- Add `company_id` grouping to `AgentStateManager` (pool agents by company)
- Add `company_id` grouping to `CallbackQueue`
- `calculate_optimal_dial_ratio()` accepts `company_id` and queries all company agents' call history
- Individual agents' power/pro dialers remain per-individual (unchanged)

### 5.4 New routes for agency predictive dialer
- `GET /voice/agency-predictive/status` — Agency-wide predictive dialer status (active agents, dial ratio, compliance)
- `POST /voice/agency-predictive/dial` — Agency-wide predictive dial (system auto-selects leads, auto-routes to Ready agents)
- `GET /voice/agency-predictive/compliance` — Agency-wide TCPA compliance dashboard
- `GET /voice/agency-predictive/agent-states` — All agent states for the agency

### 5.5 Agent-side integration
- Individual agents see "Agent State" control in their dashboard when part of a predictive-dialer agency
- States: Ready, Not Ready, Break, Wrap-Up (auto-transitions)
- When Ready, calls are auto-routed to them
- Agent hears a beep/tone when call connects, then is bridged to the lead

### 5.6 Call routing logic
- When predictive engine connects a call, find first Ready agent (longest-idle-first)
- Bridge the call to that agent's Twilio device
- If no agent available within threshold, play hold music → if still no agent after X seconds, abandon tracking kicks in

---

## Phase 6: Billing Adjustments (`blueprints/billing.py`)

### 6.1 Simplify agency tiers
- Remove `agency_starter` vs `agency_pro` distinction based on seat count
- Agency-level billing exists ONLY for the predictive dialer ($349.98/mo billed to agency owner)
- All other plans are per-user, chosen individually by each agent

### 6.2 Per-user billing (unchanged)
Each agent picks their own tier independently:
- **SMS Bot**: $99/mo — AI texting only, no dialer
- **Power Dialer**: $149.99/mo — single-line dialing + AI texting
- **Pro Dialer**: $224.99/mo — multi-line (up to 4 lines) + AI texting

### 6.3 Agency predictive dialer billing
- Predictive dialer ($349.98/mo) is the only agency-level subscription
- Billed to agency owner, shared across all agency agents
- Requires 5+ linked agents to activate
- Individual agents do NOT need to be on any specific tier to participate in agency predictive dialing

---

## File Change Summary

| File | Changes |
|------|---------|
| `db.py` | Add `company_id` + company metadata columns to `agency_billing`, `company_id` to `subscribers`, `whitelabel_config` JSONB, schema migration in `init_db()` |
| `blueprints/oauth.py` | Remove `/agencies/` call, add companyId-based auto-linking, save all available company/owner metadata, simplify provisioning |
| `blueprints/dashboard.py` | Merge agency dashboard logic, unified route |
| `blueprints/agency.py` | Keep API endpoints but change queries to use `company_id`, add white-label save endpoint |
| `blueprints/auth.py` | Remove agency-specific redirect on login |
| `blueprints/billing.py` | Remove agency_starter/agency_pro tiers, keep only agency predictive dialer billing |
| `templates/dashboard.html` | Add conditional agency sections |
| `templates/dashboard/_sidebar.html` | Add agency nav items (conditional) |
| `templates/dashboard/_topbar.html` | White-label company name/logo injection |
| `templates/dashboard/tabs/team.html` | Add "Agency Members" sub-section |
| `voice/predictive_engine.py` | Add `company_id` grouping to all singletons |
| `voice/dialer.py` | Add agency predictive dialer routes |
| `static/js/dashboard/dialer.js` | Agent state controls for predictive dialer agents |
| `static/js/dashboard/sidebar.js` | Agency nav items |
| `static/css/style.css` | Agency dashboard styles |

---

## Implementation Order

1. **Phase 1** — DB schema changes (foundation)
2. **Phase 2** — OAuth refactor (new flow)
3. **Phase 3** — Unified dashboard (merge templates + routes)
4. **Phase 4** — White-label system (cosmetic, low risk)
5. **Phase 5** — Agency predictive dialer (most complex, builds on 1-4)
6. **Phase 6** — Billing adjustments (final polish)

Each phase is independently deployable and backward-compatible.
