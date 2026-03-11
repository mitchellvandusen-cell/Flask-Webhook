# CHANGELOG — InsuranceGrokBot Flask-Webhook

> **Note on git history:** This repository's earliest visible commit in this clone is from **2026-02-19** (around PR #190). All history prior to that point is not available. PR numbers run from #190 through #239+. This changelog covers 74 code commits across three days.

---

## Key Milestones Summary

| Date | Milestone |
|------|-----------|
| 2026-03-11 | Multi-line dialer (up to 4 lines), predictive dialer, Pro Dialer tier ($224.99/mo), plan switching |
| 2026-02-19 | Initial visible history begins; voice dialer UI, Trust Hub tabs, AMD fixes, AI latency improvements | 
| 2026-02-19 | AI Minutes Marketplace launched (purchase bundles, auto-deduct on calls) |
| 2026-02-20 | Complete voice infrastructure migration from Telnyx to white-label Twilio sub-accounts | 
| 2026-02-20 | Super Admin God Mode with user impersonation added (RBAC) |
| 2026-02-20 | Power dialer triple-retry logic, audio anti-aliasing pipeline, AMD overhaul |
| 2026-02-20 | Twilio Voice SDK upgraded from 1.x to 2.x; self-hosted SDK to fix VoIP connectivity |
| 2026-02-20 | Major dashboard restructure: 3-tab layout with column menu navigation |
| 2026-02-22 | Dashboard refactored from monolithic 7000-line HTML into 32 component files |
| 2026-02-22 | Full Discord OAuth integration with persistent auth, team chat panel in sidebar |
| 2026-02-22 | CLAUDE.md documentation added; dismissable banners; Discord side panel repositioned |
| 2026-02-24 | Enterprise OAuth token refresh, proactive token cron, webhook scourer + backfill recovery |
| 2026-02-24 | A2P 10DLC compliance system: brand/campaign registration + external import from GHL |
| 2026-02-25 | Enterprise OAuth proactive refresh, import consolidation, website honesty audit, transfer hangup fix, timezone-aware stats |
| 2026-02-26 | iPhone 15 Pro UI for dialer, OAuth security hardening (CSRF/PKCE/encryption), GHL iframe embedding fixes |
| 2026-02-27 | Lead Intelligence + Smart Filters, Training integration, persistent contact cache, CRM 429 elimination |
| 2026-02-28 | GHL Data Sync Engine, SMS channel selection (GHL vs Twilio), unified Inbox app, AI-powered intelligence via xAI Grok |
| 2026-02-28 | AI-powered Smart Filters (replaced rule-based), training platform recording fix, human-mode recording fix |
| 2026-03-01 | OAuth flow fixes: removed invalid scopes, synced to 18 approved marketplace scopes, removed unsupported PKCE |
| 2026-03-01 | Website polish: product screenshots, mobile responsive CSS, removed reviews page, Calendar booking app in dialer |
| 2026-03-01 | Contact sync fixes: 401 token refresh mid-pagination, deep sync reset, progress bar fix |
| 2026-03-01 | iMessage-style Inbox app redesign with search, filters, date grouping, proper conversation sorting |
| 2026-03-01 | Mobile nav menu fix: solid background, custom toggle (replaces broken Bootstrap collapse) |
| 2026-03-01 | Pricing update: $149.99/month across all pages, bot, and documentation |
| 2026-03-10 | Hamburger menu fix, login crash fix, Remember Me (30-day), mobile dashboard redesign, agency dashboard revamp |
| 2026-03-10 | Full QA audit: 6 critical bug fixes, 7 security fixes, 5 reliability fixes across 16 files |

---

## 2026-03-11 (Multi-Line Dialer + Predictive Dialing)

### Multi-Line Dialer (Pro Dialer Tier)
- **Multi-line dialing engine**: Up to 4 concurrent outbound calls via `POST /voice/multi-dial`. Each call independently tracked in `active_calls` dict with `_multi_line: True` flag
- **Batch status polling**: `POST /voice/multi-status` polls all active call SIDs in a single request (reduces HTTP overhead vs N individual polls)
- **Multi-hangup**: `POST /voice/multi-hangup` terminates all active lines at once
- **Active lines API**: `GET /voice/active-lines` returns current line count, details, and tier-based max
- **Subscription gating**: Multi-line routes enforce `subscription_tier == 'pro_dialer'` with admin bypass. Returns `upgrade_required: true` for non-Pro users
- **Frontend multi-line engine**: `_multiLineActive` Map tracks concurrent calls; `multiLineDialBatch()` dials N contacts from queue; `multiLinePollAll()` batch-polls every 1.5s; `multiLineRenderBanner()` renders per-line status with connect/hangup controls
- **Line switching**: Agent can connect to any active line via `multiLineConnectToLine(callSid)` — switches detail panel, listen stream, and call controls
- **Queue integration**: Multi-line queue automatically refills available lines as calls terminate, respecting retry logic and DnD guards

### Predictive Dialer
- **Connect rate analytics**: `GET /voice/predictive-stats` queries 7-day `call_history` for connect rate, avg duration, avg talk time
- **AI-optimized dial ratio**: Algorithm calculates `min(4.0, max(1.0, 100 / connect_rate))` — lower connect rates automatically increase simultaneous lines
- **Recommended lines**: Rounded ratio capped at 4, displayed in predictive stats panel
- **Frontend stats panel**: Shows connect rate %, recommended lines, and dial ratio in real-time above dialer

### Pro Dialer Subscription ($224.99/mo)
- **New tier**: `subscription_tier = 'pro_dialer'` stored in `subscribers` table
- **Stripe checkout**: `GET /checkout/pro-dialer` creates Stripe session with `STRIPE_PRO_DIALER_PRICE_ID`, 7-day trial, promo codes
- **Plan switching**: `POST /change-plan` calls `stripe.Subscription.modify()` with proration. Accepts `target_tier: "individual"|"pro_dialer"`. Updates DB + returns new tier info
- **Subscription info API**: `GET /subscription-info` returns tier, max_lines, features, admin status for dynamic UI rendering
- **Billing tab redesign**: Two plan cards (Power Dialer $149.99, Pro Dialer $224.99) with current plan badge, feature comparison, and one-click plan switch button

### Marketing & Website Updates
- **3-tier pricing grid**: Home page pricing updated from 2-column (Individual + Agency) to 3-column (Power Dialer + Pro Dialer + Agency) with responsive mobile breakpoint
- **Pro Dialer featured card**: Orange gradient badge "Most Popular", scaled 1.03x, with multi-line and predictive features highlighted
- **Smart Dialer pillar**: Added "Multi-Line" tag to the Three Pillars section
- **Capabilities grid**: Added "Multi-Line Dialing" capability item with predictive pacing description
- **Comparison page**: Updated cost comparison table to include multi-line capability with "Pro" badge
- **Hero CTA**: Updated from specific price to "Plans from $149.99/mo"

### Dashboard UI
- **Multi-line banner**: `#multiLineBanner` div shows per-line status (L1-L4) with name, status color, duration, connect/hangup buttons
- **Predictive stats panel**: `#predictiveStatsPanel` shows connect rate, recommended lines, dial ratio
- **Pro badge**: `#multiLineBadge` "PRO" badge shown next to mode toggle for pro_dialer users
- **Line selector**: `#multiLineToggle` dropdown (1-4 lines) visible only for pro_dialer tier
- **Billing plan cards**: Interactive plan selection with CURRENT badge, feature lists, and change plan CTA

### Multi-Line Dialer Settings (11 new configurable settings)
All settings stored in `voice_config` JSONB, validated server-side in `blueprints/dashboard.py`, enforced in `voice/dialer.py`:
- **Max concurrent lines** (`max_lines_setting`): 1-4 lines, default 3. Server caps batch size in `multi_dial()`
- **Wrap-up time** (`wrap_up_time`): 0-120 seconds after-call work timer between batches, default 15s
- **Require disposition** (`require_disposition`): Gates next batch until all completed calls are dispositioned (works with and without wrap-up)
- **Calling hours** (`calling_hours_start`/`calling_hours_end`): TCPA-compliant calling window with pytz timezone support and midnight wrap-around (e.g., 22:00-06:00)
- **Same-number cooldown** (`same_number_cooldown_hours`): 0-72 hours before re-dialing same number, default 4h. Batch SQL with `make_interval()`
- **Daily max per contact** (`same_contact_daily_max`): 0-10 calls per contact per day, default 3. Batch SQL enforcement
- **On-machine action** (`on_machine_action`): hangup/voicemail_drop/continue when AMD detects answering machine
- **Auto-disposition toggles** (`auto_disposition_no_answer`/`auto_disposition_voicemail`): Auto-mark terminal no-answer and voicemail calls
- **Max abandon rate** (`max_abandon_rate_pct`): 1-10%, default 3.0% (FTC safe harbor for TCPA compliance)

### Bug Fixes (Audit Findings)
- **CRITICAL: SQL injection via string interpolation**: `INTERVAL '%s hours'` in cooldown query — psycopg2 doesn't interpolate inside string literals. Fixed with `make_interval(hours => %s)` parameterized form
- **CRITICAL: Night-shift calling hours always blocked**: Midnight wrap-around logic (start > end, e.g., 22:00-06:00) had inverted condition. Fixed with separate branch for normal vs wrapping ranges
- **CRITICAL: AMD result field mismatch**: Client read `serverInfo.amd_result` but server stored `_amd_result`. Normalized key in `multi_call_status()` response
- **CRITICAL: Wrap-up timer bypassed**: Poll cycle at 1.5s called `multiLineDialBatch()` immediately, ignoring pending wrap-up. Added `if (_dialerWrapUpTimer) return;` guard at top of `multiLinePollAll`
- **HIGH: DASHBOARD_BOOT null coalescing broken**: `(window.DASHBOARD_BOOT && window.DASHBOARD_BOOT.X) ?? default` — `&&` returns `false` (not `null`), so `??` won't coalesce. Fixed with optional chaining `window.DASHBOARD_BOOT?.X ?? default`
- **HIGH: Wrap-up time 0 treated as 15**: `config.wrap_up_time || 15` in voice.js live-update coerced `0` to `15`. Fixed with `?? 15`
- **MEDIUM: Require-disposition only enforced during wrap-up**: Non-wrap-up path (wrap_up_time=0) skipped disposition check. Added check to else branch
- **LOW: Non-numeric input crash**: `int(data.get(...))` in dashboard.py could throw ValueError on malformed input. Added `_safe_int()` / `_safe_float()` helpers with fallback defaults
- **BUG: multi_hangup marks completed on failure**: Now sets 'hangup-failed' status and skips DB update when `_twilio_hangup()` returns False
- **BUG: Missing list() wrapper on active_calls.items()**: Could raise RuntimeError under concurrent thread access
- **BUG: Wrong element ID**: `getElementById('dialer')` should be `'voicedialer'` for SSE auto-init

---

## 2026-03-10 (QA Audit)

### Critical Bug Fixes
- **GHL CRM adapter completely broken**: `ghl_adapter.py` had wrong kwarg `message_body=` (actual param: `message=`) and missing `location_id` arg — all GHL adapter SMS sends and contact fetches were throwing TypeError
- **Duplicate facts saved**: `tasks.py` had duplicate `if lead_vendor:` line saving the same fact twice per contact
- **Intelligence queue misrouted**: `tasks.py` and `voice/intelligence.py` queued AI re-analysis jobs to `website` queue. These short-latency intelligence tasks (30s/600s) belong on `production` to avoid queuing behind 2-hour sync jobs. Moved to `production` queue
- **Uninstall data leak**: `db.py` `delete_subscriber_data()` used `location_id` on 6 email-keyed tables (`ai_minute_balances`, `discord_connections`, etc.) — orphaned data on app uninstall. Fixed to look up email first, then delete by email
- **Contact validator broken**: `contact_validator.py` read raw encrypted tokens without calling `decrypt_token()`, and used wrong API base URL (`leadconnector.io` vs `leadconnectorhq.com`)
- **Phone lookup corrupted**: `voice/helpers.py` SQL `REPLACE(..., '1', '')` stripped ALL `1` digits from phone numbers, not just the country code prefix. Fixed with `REGEXP_REPLACE(..., '^\+?1', '')`

### Security Fixes
- **Webhook signature verification**: Added HMAC-SHA256 verification for GHL webhooks when `MARKETPLACE_WEBHOOK_SECRET` is configured. Gracefully skips if no signature header present (GHL doesn't sign all webhook types)
- **Agency logs IDOR**: Any agency owner could read any location's logs. Added ownership verification (checks `parent_agency_email` match). Returns 503 if DB unavailable instead of silently allowing access
- **Agency paywall bypass**: Checked `stripe_customer_id` existence, not subscription status. Cancelled users could access dashboard. Now checks `stripe_status in ('active', 'trialing')`
- **Malformed CSRF in agency dashboard**: `form.hidden_tag()` was placed inside a `value=""` attribute, producing nested HTML. CSRF token never validated correctly
- **Hardcoded fallback secret key**: `"fallback-insecure-key"` replaced with random generation + warning log if env var missing
- **Cron secret timing attack**: `_cron_authorized()` used `==` string comparison. Fixed to `hmac.compare_digest()`
- **TwiML XML injection**: `transfer_twiml` embedded phone number directly in XML. Fixed with `xml.sax.saxutils.escape()`

### Reliability Fixes
- **DB connection pool leak in dashboard**: `get_db_connection()` called on line 171 but only returned in POST `finally` block — leaked on every GET request. Added cleanup before `render_template`
- **DB connection double-return in demo**: `demo_init` called `return_db_connection(conn)` inline AND in `finally` block — returned same connection twice, corrupting pool. Removed inline returns, let `finally` handle cleanup
- **DB connection leak in demo reset**: Second `get_db_connection()` in reset had no `finally` block. Added try/finally
- **Transfer false success**: `voice_tools.py` returned "Transfer initiated" even when no matching active call was found. Now returns error message
- **Stripe null dereference**: `session.customer_details.email` crashed when `customer_details` was None. Added safe access with fallback

---

## 2026-03-10

### Bug Fixes
- **Hamburger menu fix**: `backdrop-filter: blur()` on `.navbar` was creating a containing block that clipped `position: fixed` children. Fixed with CSS `:has()` selector + JS `.nav-open` class fallback to remove backdrop-filter when menu is open
- **Login crash fix**: `BuildError: Could not build url for endpoint 'dashboard'` — the dashboard route is in a blueprint, so endpoint must be `dashboard.dashboard`. Fixed across all blueprint files (auth, slack, discord, google_calendar) and templates

### Authentication Improvements
- **Remember Me for 30 days**: Added `BooleanField` to `LoginForm`, `login_user(user, remember=form.remember.data)`, `REMEMBER_COOKIE_DURATION = timedelta(days=30)` with secure cookie settings
- **Dashboard button when authenticated**: Nav "Log In" button changes to "Dashboard" when `current_user.is_authenticated`

### Mobile Dashboard Redesign
- Complete mobile redesign (not just a wrapper/shrunk desktop) with fundamentally different sizing, spacing, and layout patterns
- Two-column tab layouts break to single-column with horizontal scrolling pill tabs for settings menus
- CSS attribute selectors `[style*="display:flex"][style*="min-height"]` override inline styles on Config/Voice/Connect tabs
- Compact topbar, touch-friendly form controls, simplified spacing throughout
- Workflow tabs, AI Minutes grid, and all `col-md-*` columns stack vertically on mobile

### Discord/Slack Panel Retirement
- Side panels hidden with `display: none !important` (DOM preserved for JS compatibility)
- Discord/Slack accessible as phone UI apps in dialer + buttons in mobile "More" bottom sheet
- Removed Discord/Slack bell buttons from topbar

### Agency Dashboard Revamp
- **Complete rewrite** from accordion layout to sidebar+tab architecture matching individual dashboard
- **New sidebar navigation**: Overview, Agents, Call Log, Activity, Agency Settings, Billing
- **KPI Overview tab**: 8 KPI cards (Total Calls, Connected, Connect Rate, Talk Hours, Messages, Active Agents, Avg Call, Calls/Day) with prior-period deltas
- **Daily volume chart** and **hourly heatmap** visualizations
- **Top Performing Agents** quick-view table on overview
- **Agents tab**: Agent cards with status, invite actions, and full performance stats table (calls, connected, rate, talk time, avg call, messages, last call)
- **Call Log tab**: Paginated call history across all agents with direction indicators, duration, recording/transcript badges, and agent filter dropdown
- **Activity tab**: Per-agent webhook activity logs
- **Agency Settings tab**: Profile form, CRM connection status
- **Billing tab**: Seat usage progress bar, subscription management
- **3 new API endpoints**: `GET /api/agency/kpis`, `GET /api/agency/agent-stats`, `GET /api/agency/call-log` — aggregate KPIs across all sub-user `location_id`s
- Light/dark theme support, mobile responsive layout

### Claim Account Email Redesign
- Redesigned from basic blue HTML to dark-themed email matching IGB brand
- Added gradient IGB logo header, "What's Included" feature list with checkmarks
- Gradient CTA button with box-shadow, improved copy and layout
- Dark background with glassmorphism card styling

---

## 2026-03-01 (continued)

### Website Polish & Mobile Experience
- **Product screenshots added**: Large dialer showcase on homepage, 3-up feature strip (spam protection, carriers, activity log), 2x2 screenshot grid on comparison page
- **Comprehensive mobile responsive CSS**: Full-screen mobile nav overlay, tablet/phone/small-phone breakpoints, touch targets (44px min), safe area insets for notched phones
- **Reviews page removed**: Removed from navbar and footer until more users acquired (route/template preserved)
- **Apple web app meta tags**: theme-color, apple-mobile-web-app-capable, viewport-fit=cover

### Calendar Booking App (Dialer iPhone UI)
- **New Calendar app**: iPhone-style calendar in the dialer phone UI pulling GHL/LeadConnector calendars
- **Month grid with slot indicators**: Red dots on dates with available slots, today highlight, month navigation
- **Slot picker**: Time pills grouped by Morning/Afternoon/Evening
- **Booking flow**: Confirm overlay → API call → success/error state, syncs immediately with GHL CRM
- **Schedule button**: Added to middle column action strip alongside Call, SMS, Queue

### Contact Sync Fixes
- **401 token refresh mid-pagination**: `_fetch_all_ghl_contacts()` now detects 401/403 responses during pagination, refreshes OAuth token via `get_valid_token()`, and retries — prevents silent stop at 100 contacts
- **Pagination limit increased**: From 50 to 100 pages (10,000 contacts max)
- **Progress bar JS bug fixed**: `_deepSyncUpdateBanner()` referenced undefined `contacts` variable instead of `convos` — progress bar never moved
- **Deep sync reset endpoint**: `POST /api/sync/deep-pull/reset` resets sync state and re-queues full historical pull
- **Re-sync button**: Added to deep sync banner (visible on completed/failed states)
- **Conversation listing auth fix**: `_deep_list_all_conversations()` in ghl_sync.py now handles auth errors with token refresh mid-pagination

### iMessage-Style Inbox App Redesign
- **Conversation list redesign**: Unique colored avatars per contact (hue derived from name), 2-line message previews with "You:" prefix for outbound, blue unread dots for inbound
- **Search bar**: 300ms debounce, searches by name or phone via backend query on `/api/inbox/conversations`
- **Filter pills**: All / Unread / Received / Sent — instant client-side filtering
- **Date section headers**: Today, Yesterday, This Week, This Month, Earlier
- **Thread view**: iMessage-style bubbles with date separators between days, call/voicemail pills, styled bubble tails
- **Green Messages icon**: Matches iOS Messages app (was blue Inbox icon)
- **Load More pagination**: Button at bottom for large conversation lists

### Mobile Navigation Fix
- **Replaced Bootstrap collapse**: Bootstrap 5.3 `collapse` class fought with custom CSS (`display: none` vs `display: flex !important`), causing menu to flash/disappear. Replaced with custom `toggleMobileNav()` JS function
- **Solid black background**: `#050505 !important` with `100vh`/`100dvh` height, no transparency
- **Auto-close on link click**: Nav links close the mobile menu when tapped

### Pricing Update ($149.99/month)
- **Updated all stale $98.99 references**: main.py, blueprints/dashboard.py, blueprints/webhooks.py, templates/support.html, templates/setup-guide.html, CLAUDE.md
- **Chat bot corrected**: Removed "7-day free trial" references from webhooks.py legacy bot. Main bot system prompt already had correct $149.99 and "no free trial"
- **Agency pricing**: Changed from hardcoded prices to "custom pricing — book a call"

### OAuth Flow Fixes — Scope Validation & PKCE Removal
- **Removed invalid `locations/tasks.readonly` scope**: GHL rejected this scope with 422 during OAuth authorization. Removed from `blueprints/oauth.py`.
- **Synced OAuth scopes to approved marketplace list**: Both `blueprints/oauth.py` (`GHL_OAUTH_SCOPES`) and legacy `main.py` scope lists now match the 18 approved scopes exactly. Added `workflows.readonly` and `twilioaccount.read` (were approved but missing from blueprint).
- **Removed PKCE (code_verifier/code_challenge)**: GHL OAuth rejects `code_verifier` with 422 "property code_verifier should not exist" for both public and private apps. PKCE is not supported by GHL's OAuth implementation. Removed code_challenge from authorization request, code_verifier from token exchange, and cleaned up unused `base64`/`hashlib` imports.

---

## 2026-02-28 (continued)

### AI-Powered Smart Filters — Complete Rewrite
- **Replaced rule-based classification with AI**: Smart Filters in the dialer no longer use heuristics like "responded within 48h = hot." Every contact is now classified by xAI Grok reading the full conversation history. A lead who texted "not interested" is correctly classified as **cold**, not hot.
- **New AI fields**: Enhanced the AI micro-prompt in `lead_intelligence.py` to return `should_respond` (bool — does the agent need to act NOW?), `should_respond_reason` (why), and `engagement_level` (0-3 depth of interaction). All fields validated and cached.
- **Critical classification rules**: Explicit instructions to AI: hot = actively buying/quoting, cold = said no/stop/not interested, should_respond = lead is waiting for a reply, engagement_level = depth of actual conversation (not just message count).
- **Removed ALL rule-based fallbacks**: `_igbLeadScore()` no longer has a 20-line heuristic with hardcoded point values. `_igbEngageLevel()` no longer counts calls/messages. `_igbShouldRespond()` no longer compares timestamps. All return -1 (pending) until AI has analyzed.
- **Pending state UI**: Score ring shows "?" with empty ring, engagement dots show neutral/dimmed with "AI analyzing..." tooltip, score label shows "..." — all instead of fake numbers from rules.
- **7 Smart Filter groups** (priority order): Should Respond (red), Hot (green), Warm (orange), Cool (blue), Cold (gray), Do Not Contact (red), Analyzing... (spinner).

### RQ Worker Offload for Smart Filters
- **Background AI processing**: All contact intelligence analysis runs on RQ workers (not gunicorn web threads). New tasks: `analyze_contact_intelligence_task()` (single) and `analyze_contacts_batch_task()` (batch of 10, 60s timeout).
- **Auto re-analysis**: After `process_webhook_task` processes an SMS, it auto-queues `analyze_contact_intelligence_task` to RQ so the contact's classification stays fresh without user action.
- **Persistent cache**: Results stored in `contact_intelligence` table with message-based invalidation (no time-based expiry). Once analyzed, classification persists until new messages arrive.
- **Poll-based frontend**: Frontend queues RQ jobs via `POST /voice/contact-intelligence-analyze`, then polls `GET /voice/contact-intelligence-bulk` every 4 seconds. As workers complete, contacts move from "Analyzing..." into correct groups.
- **Bulk zero-cost reads**: `get_bulk_cached_intelligence()` uses LEFT JOIN LATERAL for efficient bulk cache reads (zero AI cost on repeat loads).

### Training Platform Recording Fix (4 bugs)
- **BUG FIX: Human mode calls never recorded**: `outbound_twiml()` returned TwiML for human mode BEFORE the `start_recording()` call, so human-mode dialer calls had no recordings. Moved recording initiation above the human-mode early return — now ALL dial modes (AI + human) get recorded when `auto_record` is enabled.
- **BUG FIX: Training API `disposition` column crash**: `GET /api/v1/training/recordings` queried `disposition` column directly, but it's added dynamically via `ALTER TABLE IF NOT EXISTS` in other endpoints. If never called, the column didn't exist → 500 error. Added the same `ALTER TABLE IF NOT EXISTS` guard + `COALESCE(disposition, '')` safe fallback.
- **BUG FIX: Recording proxy requires `@login_required`**: Training platform authenticates via Bearer `trn_` token, but recording downloads at `/voice/recording/<sid>` required a Flask-Login session. Added new `GET /api/v1/training/recordings/<call_sid>/audio` endpoint authenticated via `@require_training_token` that proxies MP3 from Twilio.
- **BUG FIX: Relative recording URLs**: Training API returned relative paths like `/voice/recording/RE...` which the external training platform couldn't use. Now returns absolute `audio_url` field pointing to the new training-authenticated audio endpoint.

---

## 2026-02-28

### GHL Data Sync Engine (`ghl_sync.py` — new file)
- **Incremental GHL data sync**: New `ghl_sync.py` module (~900 lines) that pulls conversations, opportunities, phone numbers, and location data from GoHighLevel into local Postgres tables. Supports paginated fetching with cursor tracking.
- **3 new database tables**: `ghl_conversations` (synced message history), `ghl_opportunities` (pipeline/deal data), `ghl_sync_state` (sync progress tracking with cursor and status).
- **Enterprise retry pattern**: `_api_get()` helper with exponential backoff, rate limit respect (429 handling), and automatic token refresh on 401.
- **Cron endpoint**: `POST /api/cron/sync-ghl-data` queues incremental sync jobs via RQ.
- **Query functions**: `get_merged_call_count()`, `get_merged_call_history()`, `get_contact_pipeline_stage()`, `get_sync_stats_for_dashboard()`, `get_conversation_stats()`.

### SMS Channel Selection
- **GHL vs Twilio SMS routing**: Users can now choose how outbound SMS is delivered — via GoHighLevel (default) or via a specific Twilio phone number.
- **New `twilio_sms.py` module**: Direct Twilio SMS sender bypassing GHL API entirely. Returns 3-tuple matching `ghl_message.py` pattern for drop-in compatibility. Includes deduplication, safety filtering, auth error handling, and rate limiting.
- **`sms_send_via` column**: Added to both `subscribers` and `agency_billing` tables. Values: `'ghl'` (default) or a phone number like `'+15551234567'`.
- **Config UI**: New "Send SMS Via" radio picker in Bot Config tab. GHL numbers show LeadConnector logo + "DEFAULT" badge; IGB/Twilio numbers show robot icon + green "IGB" badge.
- **Pipeline routing**: `tasks.py` now checks `sms_send_via` before sending and routes through `send_sms_via_twilio()` when a Twilio number is selected. Falls back to GHL if credentials are missing.

### Unified Inbox App
- **4th iPhone app**: Added "Inbox" app to the dialer home screen (blue gradient, inbox icon). Pulls unified conversation list from synced GHL data.
- **Conversation list**: Fetches from `GET /api/inbox/conversations` — shows contact name, phone, last message preview, timestamp, and unread indicator.
- **Thread view**: `GET /api/inbox/thread/<contact_id>` returns full message thread rendered as iMessage-style bubbles with pipeline stage badge.

### AI-Powered Lead Intelligence (complete rewrite)
- **Replaced rule-based with AI**: Rewrote `lead_intelligence.py` from scratch. Previously used hardcoded rules for scoring and next-best-actions. Now fires a single micro-prompt to xAI Grok that analyzes all contact context and generates a JSON intelligence report.
- **Context builder**: `_gather_contact_context()` pulls last 30 messages, known facts, pipeline/opportunity, call history stats, tags, and existing narrative into a single context block.
- **AI micro-prompt**: Single call to `grok-4-1-fast-non-reasoning` (~200 tokens out, ~$0.001-0.003 per analysis) returns: 2-sentence summary, temperature (hot/warm/cool/cold) with reasoning, score (0-100), and 2-4 specific next actions with priorities and icons.
- **Smart caching**: New `contact_intelligence` table caches AI results. Cache invalidated when new messages arrive after the analysis, or after 6 hours. Repeat views are instant (zero AI cost).
- **Frontend**: Loading shimmer ("AI is analyzing this lead..."), temperature pill badge with color-coded icon (fire/thermometer/snowflake), score display, AI summary with brain icon, and temperature reasoning.

### Dashboard & API Updates
- **7 new API endpoints**: `/api/cron/sync-ghl-data`, `/api/ghl-phone-numbers`, `/api/inbox/conversations`, `/api/inbox/thread/<contact_id>`, `/api/stream/notifications` (SSE), `/api/contact/<contact_id>/intelligence`, `/api/sync-status`.
- **Voice call count rewrite**: `voice_bridge.py` replaced live GHL API pagination with local DB query on `ghl_conversations` table. Added `source_breakdown` (dialer/ghl_native/wavv/unknown) to `/voice/stats`.
- **Pipeline stage injection**: `tasks.py` now injects pipeline stage into AI system prompt context so the bot knows the deal status during conversations.
- **SSE notifications**: `GET /api/stream/notifications` endpoint for real-time dashboard events with iOS-style notification banner.

### Tutorial Module Fixes
- **Fixed broken element references**: Replaced dead `#dlrTabMessages`, `#dlrTabCalls`, `#dlrTabRecordings` tab IDs (removed during iPhone UI migration) with iOS app selectors (`[onclick="iosOpenApp('messages')"]`, etc.).
- **Removed dead `#dlrCallContactBtn`** reference — element no longer exists.
- **Added AI Intelligence steps**: New tutorial steps for `#igb-ai-summary` (temperature + score + AI summary), `#igb-nba-section` (recommended actions), `#igb-pipeline-badge` (pipeline stage).
- **Added iPhone home screen step**: Tutorial now explains the iOS-style app grid (`#iosHome`) and walks through all 4 apps.
- **Added Inbox app step**: Tutorial covers the new unified Inbox app.
- **Added SMS Channel Selection step**: Tutorial explains the GHL vs Twilio SMS routing picker.
- **Added 10DLC registration step**: Tutorial covers the A2P 10DLC compliance registration via `#vmenu-a2p`.
- **Added Training link step**: Tutorial mentions the external Training app link in the sidebar.

---

## 2026-02-27

### Lead Intelligence & Smart Filters
- **InsuranceGrokBot Lead Intelligence**: AI-powered lead scoring and profile enrichment displayed in the contact detail panel. Shows known facts, conversation narrative, and NLP-extracted information.
- **Smart Filters**: Pipeline filter dropdown in the dialer contact list for filtering contacts by CRM pipeline and stage.
- **iPhone app label rename**: Renamed dialer app labels from "Phone/Contacts/Call History" to "Messages/Calls/Voicemail" for clarity.
- **Blue iMessage checkmarks**: Sent messages now show blue double-check delivery indicators.
- **Training button in topbar**: Added quick-access Training link to the dialer top bar.

### Training Integration
- **Training data API**: `GET /api/training/carriers`, `/api/training/knowledge`, `/api/training/underwriting` endpoints serve carrier lists, insurance knowledge, and underwriting rules for the external Training app.
- **Voice Settings code generation**: New panel in Voice Config generates integration code snippets for embedding the Training app.
- **Sidebar Training link**: Added "Training" as a direct external link in the sidebar under Workflows.

### Performance & Reliability
- **Persistent contact cache**: Contacts loaded from CRM are cached locally, enabling instant loading on tab switch without re-fetching from the API.
- **CRM 429 elimination**: Replaced aggressive CRM API calls with local search + cache + exponential retry. Eliminates rate limit errors during high-volume dialing sessions.
- **Voice settings toggles**: Added toggles for concise AI content and improved readability in the voice configuration panel.

### Infrastructure
- **Docker CMD format fix**: Changed Dockerfile CMD from shell form to JSON array form for proper signal handling during container shutdown.
- **Worker token encryption**: Workers now initialize the token encryption module at startup so they can decrypt OAuth tokens stored in the database.
- **GHL API 401 retry**: `ghl_api.py` history fetch now retries with a refreshed token on HTTP 401, with error table rebranded for consistency.

---

## 2026-02-26

### iPhone 15 Pro Dialer UI
- **iPhone-style interface**: Complete redesign of the dialer right column as an iPhone 15 Pro with home screen app grid, app views with nav bars, and iMessage-style conversation bubbles.
- **3 built-in apps**: Messages (SMS thread), Calls (call history), Voicemail (recordings) — each accessible from the iOS home screen.

### OAuth Security Hardening
- **CSRF protection**: OAuth flow now includes state parameter with cryptographic nonce validation.
- **Token encryption**: OAuth tokens encrypted at rest in the database using Fernet symmetric encryption. Auto-bootstraps encryption key on Railway deploy.
- **Scope validation**: Verifies returned OAuth scopes match the requested set. Scopes must match exactly what's approved on the GHL marketplace app.
- **Updated OAuth scopes**: Aligned with marketplace-approved scope set, defaulting to public app credentials.
- **Proactive token refresh hardening**: Prevents tokens from ever reaching expiry by refreshing well in advance.
- **Note**: PKCE was added in this release but later removed (2026-03-01) — GHL rejects `code_verifier` with 422 for both public and private apps.

### GHL Iframe Embedding Fixes
- **Cross-origin localStorage safety**: Wrapped localStorage calls in try/catch for cross-origin iframe environments.
- **External redirect targeting**: Added `target=_top` to all external redirects for proper navigation when embedded in GHL iframes.

### Other Fixes
- **AI Minutes env var convention**: Renamed price ID environment variables to `AI_MINUTES_PRICE_ID_*` convention.
- **Demo import fix**: Corrected imports of `get_known_facts` and `get_narrative` from `memory` module instead of `db`.
- **Age-based product focus**: Hardcoded age-based insurance product recommendations into the AI decision pipeline.
- **Dark mode on public pages**: Force Bootstrap dark mode on all public-facing pages (support, FAQ, etc.).

---

## 2026-02-25 (Session 2)

### OAuth Hardening & CRM Token Persistence
- **Advisory lock on token refresh**: `update_subscriber_token()` in `db.py` now acquires `pg_advisory_xact_lock(hashtext(location_id))` before updating GHL tokens. Prevents 4 parallel RQ workers from racing on single-use GHL refresh tokens — the token is refreshed once and all workers see the updated value.
- **CRM adapter token persistence**: HubSpot, Salesforce, and Zoho CRM adapters now call `update_crm_config_token()` (new `db.py` function) after a successful token refresh. Previously refreshed tokens were only updated in-memory and lost on worker restart.
- **`update_crm_config_token()` function**: New DB function using JSONB `||` merge operator to update only `access_token` in `crm_config` without clobbering other fields.
- **SMS 401 auto-retry**: `ghl_message.py` now attempts a one-shot token refresh on HTTP 401/403 during SMS send. Uses `_token_refreshed` guard flag to prevent infinite retry loops.
- **Unused import cleanup**: Removed `import httpx` from `memory.py` and `import random` from `prompt.py` (confirmed unused by grep scan).

### Dashboard UX Overhaul
- **Shared save toast**: Added `_showDashToast(ok, msg)` global utility to `dashboard.html` (injected before JS modules load). Appears as a bottom-right fixed-position toast with green (success) or red (error) background and slide-in animation.
- **Save feedback consistency**: All save functions now use `_showDashToast()` for error and success feedback:
  - `carriers.js`: Replaced `alert()` calls with toast on success and error
  - `advanced.js`: Replaced `alert()` calls with toast on success and error
  - `voice.js`: Added toast on success and error alongside existing inline panel feedback
  - `save_config.js`: Existing save overlay unchanged (it already shows clear "Saving to database..." → checkmark UX)
- **Bot Config tab redesigned**: `config.html` converted from a flat grid form to a two-column side-menu layout matching the Voice Config tab. Three menu panels: "Bot Identity" (bot_name, initial_message, website), "Calendar & CRM" (location_id, crm_user_id, timezone, calendar), "Agency" (agency owners only — links to agency dashboard with seat count).
- **Integrations/Connect tab redesigned**: `connect.html` converted to two-column side-menu layout. Four menu panels: "LeadConnector" (GHL OAuth guide + status), "Other CRMs" (Salesforce/HubSpot/Pipedrive/Zoho/Zapier/Insureio setup guides + config form), "API Access" (key generation, webhook URL, quick start), "Outbound Calls" (POST endpoint reference). Webhook URL display moved to always-visible top section.
- **Light theme extended**: Added missing light-theme CSS overrides to `style.css` for:
  - `.dtmf-key` (dial keypad buttons)
  - `#dialerStatsPanel`, `#dialerSettingsPanel` (call statistics and settings overlays)
  - `.dlr-kpi-card`, `.dlr-kpi-val`, `.dlr-kpi-label` (stats KPI cards)
  - `.dlr-stat-period` (stats period selector buttons)
  - `.dlr-daily-bar`, `.dlr-call-badge` (call activity charts)
  - `#dialerHistoryList`, `#dialerRecordingsList` (call history and recording rows)
  - `#transcriptModal` (transcript view modal)
  - `.voice-menu-item`, `.cfg-menu-item`, `.conn-menu-item` (new side-menu buttons)
  - `#voiceSettingsMenu`, `#configSettingsMenu`, `#connectSettingsMenu` (new side-menu containers)

### Voice Dialer — On-Demand Transcription
- **"Transcribe Now" button**: Recordings in the Recordings tab and Call History tab now show a gold "Transcribe" button for calls that have a `recording_url` but no transcript yet.
- **`POST /voice/transcribe-recording` endpoint**: New route in `voice_bridge.py`. Downloads the recording MP3 from Twilio (using master account credentials), sends to xAI Whisper API (`whisper-large-3`), saves structured transcript to `call_history.transcript` via `save_call_transcript()`, returns the transcript JSON.
- **`transcribeNow()` JS function**: Handles button state (spinner → success/error), calls the endpoint, then swaps the "Transcribe" button to a "View Transcript" button on success and shows a toast notification.
- **Transcript modal improved**: `showTranscript()` now renders `role: "call_recording"` entries (from on-demand transcription) as a full-width transcript block with microphone icon, alongside the existing lead/AI bubble layout for real-time call transcripts.

### Agency Dashboard
- **Theme toggle added**: Agency dashboard now has a light/dark theme toggle button in the top-right utility bar. Uses the same `dash_theme` localStorage key as the main dashboard — theme state is shared between both pages.
- **Voice Dialer link**: Added a "Voice Dialer" link button in the agency dashboard utility bar (top) and bottom navigation section.

### Support Page
- **Error codes section**: Added new "Error Codes & Log Messages" section with 7 accordion groups: OAuth/Token errors, Webhook processing errors, SMS/Twilio errors, AI/LLM errors, Voice/Dialer errors, Billing/Stripe errors, Database/Server errors. Each error includes cause and fix instructions.
- **Quick link card**: Added "Error Codes" card to the top quick links grid.

### OWNERS_MANUAL.md
- **Troubleshooting expanded**: Added sections for recording transcription failures, dashboard save button issues, side-menu navigation debugging.
- **Error code quick reference table**: Added full table of all log messages with source file, severity, and meaning.

---

## 2026-02-25

### Enterprise OAuth & Code Quality
- **Proactive OAuth token refresh**: New `/api/cron/refresh-tokens` endpoint automatically refreshes all tokens expiring within 2 hours. Designed for 15-minute cron interval — tokens never expire mid-conversation again.
- **SQL safety fix**: `update_subscriber_token()` now uses `make_interval(secs => %s)` instead of string interpolation for the interval parameter.
- **Timezone-aware token comparison**: `get_valid_token()` handles both timezone-aware and naive datetime objects from the DB.
- **Dead code removal**: Removed unused `count_consecutive_bot_messages()` from tasks.py.
- **Import consolidation (round 1)**: Moved 40+ inline imports to module level across main.py, voice_bridge.py, and tasks.py. Eliminated `_re`, `_json`, `_dt`, `_td`, `_tz` aliases.
- **Import consolidation (round 2)**: Moved `send_email_via_api`, `get_webhook_logs`, `get_subscribers_needing_token_refresh`, `get_valid_token`, CRM factory imports, and `build_system_prompt` to module level in main.py. Moved `time`, `math`, `timezone` to module level in db.py. Moved `quote` to module level in twilio_provisioning.py.
- **Updated .gitignore**: Added `attached_assets/`, `check_accounts.py`, `*.pyc`, `*.log`, `.DS_Store`.

### Website Honesty Audit
- **comparison.html**: Fixed "Unlimited Minutes" claims to show "5,000 Minutes Included*" with fair-use disclaimer ($0.02/min overage). Updated footnote date to February 2026.
- **faq.html**: Updated "Does it only work in Lead Connector?" from "Right now, yes" to accurately list all 7 CRM integrations.

### Voice & Dialer Fixes
- **Transfer hangup fix**: Added `action` URL to `<Dial>` in both `/voice/transfer-twiml` and `/voice/intercept-twiml`. New `/voice/transfer-complete` endpoint returns `<Hangup/>` so parent calls are released when transfer ends.
- **Hangup for transferred calls**: `/voice/hangup` now detects transferred calls and completes child call legs via Twilio REST API before completing the parent.
- **Terminal state cleanup**: `transferred` added to terminal status lists in call-status polling and voice status webhook.
- **Timezone-aware stats**: `/voice/stats` now uses the subscriber's configured timezone instead of UTC for all date calculations.
- **Gunicorn threads**: Increased from 4 → 40 with `--timeout 0` for WebSocket support.
- **Hangup race fix**: Server-side success flag for reliable call termination.
- **Worker timeout fix**: Resolved issue where worker timeouts killed active calls.
- **Dialer cleanup**: Structural cleanup, double-dial guard, instant hangup, timer cleanup, heavy logging audit.
- **Listen/intercept fixes**: Fixed listen WebSocket receive compatibility, intercept call SID race condition, intercept double-fire race.

---

## 2026-02-24

**[Feature]** — Enterprise-grade OAuth token refresh + proactive cron endpoint — Rewrote the GHL OAuth token refresh logic for 10/10 reliability with retry, diagnostics, and last-resort fallback. Added `POST /api/cron/refresh-tokens` endpoint (auth via `CRON_SECRET`) to proactively refresh tokens expiring within 30 minutes. Schedule every 15 minutes via cron-job.org.

**[Feature]** — Token recovery + audit retry on SMS 401/403 — When an SMS send fails with HTTP 401 or 403, the pipeline now attempts a fresh token recovery and retries the send with the new token before giving up.

**[Feature]** — Webhook scourer: auto-recover token-failed tasks — Added `POST /api/cron/recover-failed-webhooks` endpoint that scans `failed_webhook_payloads` for tasks that failed due to token errors in the last 24 hours, fetches a fresh token, and re-queues them. Schedule every 15 minutes.

**[Feature]** — Backfill historical token failures from webhook_logs — Added `POST /api/cron/backfill-failed-webhooks` endpoint that recovers webhooks that failed before the `failed_webhook_payloads` table existed. Reconstructs payloads from `webhook_logs` entries (location_id, contact_id, message_preview) and re-queues through the full AI pipeline. Safe to run multiple times — entries are marked as retried. Catches both dropped webhooks and `sms_http_fail` entries.

**[Fix]** — Backfill runs as RQ background job to avoid gunicorn worker timeout.

**[Fix]** — Expanded backfill window from 48h to 96h.

**[Fix]** — Re-queue `sms_http_fail` contacts through full pipeline instead of re-calling GHL API directly.

**[Fix]** — Critical: retry DB token writes and verify persistence after OAuth refresh.
**[Feature]** — A2P 10DLC brand & campaign registration system — Full compliance workflow for registering brands and campaigns with Twilio's A2P 10DLC program, plus import of externally-approved brands/campaigns from GHL/LeadConnector.

### Backend (twilio_provisioning.py)
- 8 new A2P functions: `create_a2p_brand()`, `get_a2p_brand_status()`, `create_messaging_service()`, `add_phone_to_messaging_service()`, `create_a2p_campaign()`, `get_a2p_campaign_status()`, `import_external_brand()`, `import_external_campaign()`, plus `list_messaging_services()`.
- Brand registration creates Trust Hub Customer Profile + EndUser + TrustProduct + Brand Registration in one flow.
- Campaign registration creates a Messaging Service, links phone numbers to its sender pool, then submits the campaign with use case, sample messages, opt-in/out details, and help keywords.
- External import supports CNP migration of TCR-approved brands and campaigns from other CSPs (e.g., GHL/LeadConnector) into Twilio.
- `A2P_USE_CASES` constant defines all valid campaign categories (2FA, CUSTOMER_CARE, LOW_VOLUME, MARKETING, MIXED, etc.).

### Backend (voice_bridge.py)
- 7 new routes under `/voice/a2p/`:
  - `GET /voice/a2p/status` — Current A2P registration state from `voice_config.a2p`.
  - `POST /voice/a2p/register-brand` — Submit brand for vetting (sub-account users gated by payment check).
  - `GET /voice/a2p/brand-status` — Poll Twilio for brand vetting progress.
  - `POST /voice/a2p/create-campaign` — Create campaign + messaging service + link numbers.
  - `GET /voice/a2p/campaign-status` — Poll campaign approval status.
  - `POST /voice/a2p/import` — Import external brand ID + campaign ID.
  - `POST /voice/a2p/mark-fee-paid` — Mark A2P registration fee as paid.
- All routes persist state to `voice_config["a2p"]` JSONB — no new DB tables needed.

### Backend (main.py)
- `POST /a2p/checkout` — Creates Stripe checkout session for A2P registration fee ($19 = $4 brand vetting + $15 campaign review).
- Stripe webhook handler extended to process `purchase_type: "a2p_registration"` and set `voice_config.a2p.a2p_fee_paid = true`.
- New env var: `A2P_REGISTRATION_PRICE_ID` — Stripe price ID for the A2P fee.

### Frontend (voice.html)
- New "10DLC" sub-tab in Voice Config column menu with `fa-certificate` icon.
- Status banner showing current registration state (none → brand submitted → brand approved → campaign submitted → campaign approved).
- Mode selector: "Import Existing" vs. "Register New" with separate panels.
- Import panel: paste fields for Brand ID and Campaign ID from GHL/LeadConnector.
- Register panel: 2-step wizard — Step 1 (brand form with business details, pre-filled from Trust Hub) → Step 2 (campaign form with use case, sample messages, opt-in/out, help keywords, phone number checkboxes).
- Payment gate for sub-account users: Stripe checkout required before registration.
- Step indicator pills showing progress through the registration flow.

### Frontend (numbers.js)
- 13+ new functions: `a2pLoadStatus()`, `a2pRenderUI()`, `a2pSwitchMode()`, `a2pImportExternal()`, `a2pRegisterBrand()`, `a2pCreateCampaign()`, `a2pRefreshStatus()`, `a2pPayFee()`, `a2pLoadNumbersForCampaign()`, `a2pRenderNumberCheckboxes()`, `a2pGetSelectedNumberSids()`, `a2pRenderBrandStatus()`, `a2pUpdateStepPills()`.
- URL parameter handler for `a2p_payment_success` redirect after Stripe checkout.
- Number cache (`_a2pNumbersCache`) to avoid redundant fetches when switching tabs.

### Frontend (sidebar.js)
- Updated `switchVoicePanel()` to include `'a2p'` panel and trigger `a2pLoadStatus()`.

---

**[Enhancement]** — Glassmorphism tutorial popover redesign — Complete CSS overhaul of the driver.js tutorial popover with premium frosted-glass visual effects.

- **`tutorial.js`** — Rewrote all popover CSS with multi-layer radial gradient background, `backdrop-filter: blur(40px) saturate(200%)`, `::before` pseudo-element top-edge light streak, 6-layer box-shadow for depth/glow, refined entry animation with `filter:blur(4px)` fade, and improved button styling with cyan accent glow.

---

**[Fix]** — Tutorial popover centering broken — `position: relative !important` on `.driver-popover` overrode driver.js's own `position: fixed` used for centering popovers on screen.

- **`tutorial.js`** — Removed the single line `position: relative !important;` that was added for the `::before` pseudo-element (driver.js already creates a positioned ancestor).

---

**[Fix]** — Dialer UI, hangup reliability, KPI stats, and call count badges — Critical RealDictCursor compatibility fix, UI layout improvement, and hangup flow hardening.

### KPI Stats + Call Count Badges (root cause fix)
- **`voice_bridge.py`** — All stats/counts routes (`/voice/stats`, `/voice/contact-call-counts`, `/voice/contact-call-counts/merged`, `/voice/contact/<id>/ghl-call-count`) used integer index access (`row[0]`) on query results, but the DB pool uses `RealDictCursor` which returns dict-like rows. `row[0]` raised `KeyError: 0`, silently failing and returning empty data. **Fix:** Replaced all integer index access with column name key access (`row['location_id']`, `row['total_calls']`, etc.) across 8 queries.

### Hangup Button + Banner
- **`dialer.js`** — `dialerStopQueue()` previously nulled `dialerCallSid`, cleared the poll timer, and hid the banner instantly — before the hangup API even responded. If the call was still connecting on Twilio's side, the user lost all visibility. **Fix:** Now shows "Hanging up..." banner state, sends the hangup request, waits for the response, then cleans up after a brief delay.

### UI Layout
- **`dialer.html`** — Moved the Queue section above the Contact List in the left column (was at the bottom, now directly below the search/filters). Changed chevron direction to match new position.

### Call Count Badges
- **`dialer.js`** — Added phone icon (`fa-phone`) to call count badges for clearer visual identification. All badge render/update paths (`dialerRenderContacts`, `dialerRenderContactBadges`, `dialerUpdateContactBadge`, `dialerFetchMergedCounts`) now use consistent `innerHTML` with the icon.

### Stats Period
- **`dialer.js`** — Fixed default stats period mismatch: JS variable was `'month'` but HTML button showed "Today" as active. Aligned to `'today'`.

---

**[Fix]** — Enterprise-grade dialer: fix queue skipping, harden listen/intercept/mute — Three root-cause bugs in the power dialer queue caused contacts to be skipped. Listen, intercept, and mute hardened with proper error handling, VoIP pre-warming, and reconnection logic.

### Queue (3 bugs fixed)
- **`dialer.js`** — `dialerStartCall()` failures (invalid phone, API error, network error) left queue items stuck in `'initiated'` status. `dialerAdvance()` only retried `['no-answer','busy','failed','canceled']`, so initiated items were silently skipped. **Fix:** All failure paths now set `item.status = 'failed'` before advancing.
- **`dialer.js`** — The `'transferred'` poll handler kept the poll interval running for 2.5s after detection. Each subsequent poll iteration scheduled a duplicate `setTimeout → dialerAdvance`, causing double-advance (skipping the next contact). **Fix:** `clearInterval(dialerPollTimer)` is now called immediately when `'transferred'` is first seen.
- **`dialer.js`** — Added `_advanceLocked` guard to prevent concurrent `dialerAdvance()` calls from multiple timeouts firing simultaneously. Lock is released in every exit path (stop, retry, next, finish).

### Listen (Fly on the Wall)
- **`dialer.js`** — Hardened the listen stream with: connection timeout (8s), server-confirmed listening status, reconnection limit (5 attempts with exponential backoff), proper AudioContext error handling with try-catch, and clean teardown of previous connections before reconnecting.

### Intercept
- **`dialer.js`** — VoIP device is now pre-warmed in background when an AI call connects (`status === 'in-progress'`). Previously, clicking Intercept required loading the Twilio SDK + fetching token + mic permission + registration (~15s). Now VoIP is ready by the time the agent clicks Intercept.
- **`dialer.js`** — Listen stream is stopped via `_resetListenBtn()` before sending the takeover request, preventing echo/feedback during handover.

### Backend (voice_bridge.py)
- Added instant takeover detection in the `receive_from_xai()` relay loop — AI audio stops immediately when agent clicks Intercept (previously only checked in `receive_from_twilio()` direction).
- Sends Twilio `clear` event on intercept in both relay directions, flushing buffered AI audio.
- Added `'transferred'` to the listen stream's terminal status check.

---

## 2026-02-23

**[Feature]** — Dialer call statistics page and per-contact call count badge — Added a comprehensive statistics panel to the Power Dialer and a call-count badge on every contact row.

- **3 new backend routes** in `voice_bridge.py`:
  - `GET /voice/stats?period=<today|week|month|all>` — Returns SQL-aggregated KPIs: total dials, outbound/inbound split, connected calls, connect rate, avg & total talk time, calls over 6s/1m/2m/5m/10m, unique contacts dialed, calls-per-day, 30-day daily volume, hourly distribution, and top-5 most-called contacts.
  - `GET /voice/contact-call-counts?ids=<csv>` — Batch local call counts for up to 300 contact IDs.
  - `GET /voice/contact/<id>/ghl-call-count` — Returns merged count: local dialer DB calls + GHL conversation call messages (types 3, 4, TYPE_CALL), so GHL-native calls + WAVV calls + dialer calls all sum correctly.

- **`dialer.html`** — Added "Stats" button (`fa-chart-bar`) to the top bar, a full-width stats overlay panel with period selector (Today / 7 Days / 30 Days / All Time), and CSS for KPI cards, duration bars, and the call count badge.

- **`dialer.js`** — After `dialerFetchContacts()`, a batch call to `/voice/contact-call-counts` enriches every contact with a `×N` badge. When a contact is selected, `/voice/contact/<id>/ghl-call-count` fetches the merged total and updates the badge live. Stats panel functions: `dialerToggleStats()`, `dialerSetStatsPeriod()`, `dialerLoadStats()`, `dialerRenderStats()`, `_kpiCard()`, `_durBar()`, `_fmtDuration()`.

**[Fix]** — Remove stray `<script>` tags from extracted JS modules (`numbers.js`, `alerts.js`, `api_keys.js`, `carriers.js`) that caused SyntaxErrors preventing pipeline filter and other UI from loading.

**[Fix]** — Add missing `_esc()` and `_fmtPhone()` utilities to `numbers.js`.

**[Fix]** — Remove visible text from `discord_modal.html` (orphaned split HTML comment from monolithic extraction).

---

## 2026-02-22

**[08:39 UTC]** — Add CLAUDE.md, dismissable banners, and Discord side panel — Added comprehensive CLAUDE.md project documentation file, made green flash banners dismissable with a Bootstrap close button, repositioned the Discord chat panel to slide out from the sidebar's right edge instead of the screen's right edge, and added a Discord Chat toggle in the sidebar footer with an unread badge indicator.

**[08:10 UTC]** — Implement proper Discord bot-invite integration pattern — Rewrote the Discord OAuth and bot-invite flow across `db.py`, `main.py`, and `discord.js` to implement proper guild bot detection and generate correct bot invite URLs.

**[07:41 UTC]** — Fix Jinja2 TemplateSyntaxError: remove orphaned endif and structural tags from billing.html — Removed 3 orphaned Jinja2 template tags in `billing.html` that were causing parse errors at startup.

**[07:36 UTC]** — Fix Jinja2 TemplateSyntaxError: remove orphaned unclosed if block from _alerts.html — Removed 25 lines of orphaned template code in `_alerts.html` that were causing a Jinja2 parse error in the alerts include file.

**[07:33 UTC]** — Keep Discord permanently authorized across page refreshes — Updated `main.py` and `discord.js` to persist Discord OAuth state in the database so users remain connected across page reloads; added a sidebar Discord section for persistent access.

**[07:21 UTC]** — Fix Listen/Mute AI audio bugs in power dialer — Fixed listen/mute button logic in `dialer.js` for AI audio streaming during power dial sessions.

**[07:12 UTC]** — Upgrade Discord integration: scope fix, token refresh, bot fallback, markdown — Major Discord integration upgrade fixing OAuth scopes, adding token refresh logic, implementing a bot message fallback, and adding markdown rendering in the chat panel. Rewrote `discord.js` (1226 lines) and added associated CSS to `_head.html`.

**[06:58 UTC]** — Refactor dashboard.html into component-based structure with Discord OAuth integration — Massive refactor splitting a 7000-line monolithic `dashboard.html` into 32 separate component files, with JavaScript modules under `static/js/dashboard/` and template partials under `templates/dashboard/tabs/` and `templates/dashboard/modals/`.

**[06:37 UTC]** — Add Discord OAuth integration + dashboard readability improvements — Added 17 new database tables for Discord (`discord_connections`, `discord_servers`, `discord_webhook_channels`), new routes for Discord OAuth flow, and improved dashboard readability with CSS updates.

**[05:35 UTC]** — feat: InsuranceGrokBot Reply uses full bot pipeline (tasks.py) — Connected the voice bridge AI reply functionality to use the same full `tasks.py` pipeline instead of a lighter code path, ensuring consistent AI behavior across SMS and voice.

**[05:30 UTC]** — fix: menu stacking on section switch + upsize message UI — Fixed a CSS z-index/stacking bug that occurred when switching between dialer sections and increased message font sizes for readability.

**[05:26 UTC]** — feat: AI Draft SMS, phone keypad, light/dark theme, sidebar fixes — Added an AI-powered draft SMS generation button, a phone keypad UI component, a light/dark theme toggle, and sidebar layout fixes.

**[05:12 UTC]** — Upgrade SMS thread UI to 10/10 — full messaging experience — Complete SMS thread redesign in `dashboard.html` featuring a bubble layout, timestamps, contact info header, and a full send UI.

**[05:03 UTC]** — Fix intercept button, add SMS UI, modernize dashboard, fix VoIP iframe — Fixed call intercept button functionality, added an SMS conversation UI panel, modernized overall dashboard styling, and fixed VoIP iframe embedding.

---

## 2026-02-20

**[22:43 UTC]** — Production-grade dialer: split mute buttons, add failsafes and retry logic — Split mute/unmute into separate dedicated buttons, added failsafe retry logic for the call panel, and improved `voice_bridge.py` robustness for production use.

**[22:15 UTC]** — Fix dial attempts: honor user setting across all interfaces — Ensured the max dial attempts setting from the user's config is respected consistently across `call_panel.html`, `dialer.html`, and `voice_bridge.py`.

**[21:58 UTC]** — Fix number search: normalize state names and skip mobile for US — Updated `twilio_provisioning.py` to normalize state abbreviations to two-letter codes and filter out mobile number results for US local number searches.

**[20:57 UTC]** — Add Dockerfile as fallback builder for Railway — Added a 12-line `Dockerfile` as a Railway deployment fallback build option.

**[20:33 UTC]** — Add .dockerignore to speed up Railway builds — Added `.dockerignore` with 35 entries to exclude unnecessary files (venv, caches, test data, etc.) from the Docker build context and speed up Railway deployments.

**[20:18 UTC]** — Simplify AMD: detect machine, hang up, redial — no voicemail drop — Simplified answering machine detection logic to detect the machine, hang up, and schedule a redial. Removed the voicemail drop complexity entirely.

**[20:13 UTC]** — Fix AMD voicemail: leave message, preserve status, trigger redial — Fixed the AMD voicemail flow in `voice_bridge.py` to correctly preserve call status and trigger redial scheduling after leaving a message.

**[20:04 UTC]** — Fix listen button audio streaming and AI outbound call direction — Fixed the listen button to correctly stream audio during AI calls and fixed outbound AI call direction detection logic.

**[19:44 UTC]** — Fix VoIP media acquisition error (31402) from stale audio device ID — Added stale device ID cleanup to prevent Twilio VoIP SDK error 31402 on device reconnect.

**[19:39 UTC]** — Fix human dial crash: sync TwiML app + phone webhooks to current server URL — Updated `twilio_provisioning.py` to sync both TwiML app webhook URLs and phone number webhooks to the current server URL, fixing human-mode dial crashes caused by stale webhook targets.

**[19:32 UTC]** — Implement speaker mode, VoIP intercept, mute, and power dial retries — Added full speaker mode toggle, a VoIP call intercept button, mute functionality, and power dialer retry logic across `dashboard.html` and `voice_bridge.py`.

**[19:14 UTC]** — Fix human-mode calls dropping immediately, bridge backend calls to browser — Fixed an immediate call drop bug in human-mode calls by properly bridging backend-initiated calls through to the browser VoIP client.

**[18:33 UTC]** — Auto-migrate super_admin from sub-account to master account — `voice_bridge.py` now auto-detects the super_admin role and upgrades Twilio credentials from a sub-account to the master account automatically.

**[18:29 UTC]** — Create per-sub-account API keys for valid VoIP AccessTokens — Updated `twilio_provisioning.py` to create per-sub-account API key/secret pairs, which are required for generating valid Twilio Voice SDK AccessTokens.

**[18:19 UTC]** — Fix crash on undefined VoIP registration error, improve diagnostics — Added null-safety handling to the VoIP SDK error handler and added diagnostic console logging for registration failures.

**[18:16 UTC]** — Add Reauthorize CRM button for connected users — Added a "Reauthorize" button in the Connect CRM tab so users who are already connected can refresh their GHL OAuth tokens without fully disconnecting.

**[18:09 UTC]** — Add debug logging to VoIP SDK registration, add 15s timeout fallback — Added extensive `console.log` statements to the VoIP SDK registration path and a 15-second timeout fallback in case the registration event never fires.

**[18:06 UTC]** — God mode: show all subscribers and agency billing rows without deduplication — Fixed the god mode dashboard query to return all rows without the DISTINCT deduplication that was hiding agency billing records.

**[18:05 UTC]** — Fix god mode crash: safely detect missing columns before querying — Updated `db.py` to check for column existence before querying to prevent crashes when schema migrations have not yet run on a given environment.

**[17:57 UTC]** — Add oauth_app_type column to god mode, add Logs viewer for any customer — God mode now displays the `oauth_app_type` column and includes a live Logs viewer that admins can open for any customer's `location_id`.

**[17:54 UTC]** — Fix dual-app OAuth: auto-detect public vs private app credentials, add god mode logs — Updated `ghl_api.py` to auto-detect whether to use public or private GHL OAuth credentials based on the subscriber's registered app type.

**[17:33 UTC]** — Fix number dropdown menus cut off, add pricing to buy-number UI, dark-theme all selects — Fixed CSS overflow on dropdown menus causing them to be clipped, added price display to number search results, and applied dark theme styling to all `<select>` elements.

**[17:27 UTC]** — Self-host Twilio Voice SDK 2.18.0 — fix VoIP never connecting — Bundled `twilio-voice-sdk-2.18.0.min.js` locally as a self-hosted asset (previously loaded from CDN) to fix VoIP clients never reaching the Connected state due to CDN-related issues.

**[17:19 UTC]** — Fix AI caller error and buy-numbers dropdown styling — Fixed an AI outbound caller crash and corrected z-index/styling issues on the buy-numbers dropdown overlay.

**[17:17 UTC]** — Fix recording downloads: proxy through server instead of redirecting to expiring S3 URLs — Changed the recording download endpoint to proxy audio through the Flask server rather than redirecting users to expiring Twilio S3 URLs.

**[17:09 UTC]** — Filter call history/recordings by selected contact, increase dialer font sizes — Call history and recording lists now filter results by the currently selected contact; increased font sizes across the dialer UI for readability.

**[16:54 UTC]** — Upgrade VoIP to Twilio Voice SDK 2.x, add audio device selector, fix transfers — Major VoIP upgrade from SDK 1.x to 2.x; added an audio input/output device selector UI; fixed call transfer handling in the new SDK.

**[16:02 UTC]** — Major dashboard restructure: 3 tabs, column menu, pricing fix — Reorganized the dashboard into 3 primary tabs with a column menu navigation pattern and fixed the Stripe pricing display.

**[07:10 UTC]** — Remove A2P 10DLC from provisioning comments, update sub-account flow docs — Removed outdated A2P 10DLC registration steps from provisioning comments and updated sub-account flow documentation to reflect the current process.

**[07:05 UTC]** — Remove auto-buy number from provisioning, add Twilio-matching search filters — Removed the automatic phone number purchase step from the provisioning flow; added state/area-code search filters matching Twilio's search API parameters.

**[06:59 UTC]** — Simplify dialer page, fix navbar spacing, fix pricing badge cutoff — Simplified the dialer marketing page layout, fixed navbar spacing, and fixed pricing tier badge text being cut off.

**[06:56 UTC]** — Fix dialer marketing: auto-retry (same lead 3x), not triple-line simultaneous — Corrected the dialer marketing copy to accurately describe auto-retry (calling the same lead up to 3 times sequentially) rather than triple-line simultaneous dialing, which was inaccurate.

**[06:44 UTC]** — Update website to market dialer, AI calling, and texting features — Updated homepage and marketing pages to feature the power dialer, AI calling, and two-way texting capabilities prominently.

**[06:22 UTC]** — Only super_admin gets master Twilio — agency_owner gets sub-account — Clarified and enforced the Twilio account hierarchy: super_admin role uses master Twilio account credentials; agency_owner tier receives a dedicated Twilio sub-account.

**[06:20 UTC]** — Fix Twilio provisioning: super_admin uses master account, not sub-account — Fixed a bug where super_admin users were incorrectly being provisioned with a Twilio sub-account instead of the master account.

**[06:14 UTC]** — Fix is_agency_owner: super_admin is not an agency_owner tier — Corrected the role hierarchy check so that the super_admin role does not trigger agency_owner provisioning logic.

**[06:11 UTC]** — Add Super Admin God Mode with user impersonation (RBAC) — Added `/admin/god-mode` dashboard, visible only to emails in the `ADMIN_EMAILS` whitelist, showing a full subscriber list and providing one-click user impersonation for support and debugging.

**[06:04 UTC]** — Restyle agency dashboard: green text + collapsible accordion sections — Applied brand green (`#00ff88`) text color and converted agency dashboard sections to Bootstrap accordion collapsibles for a cleaner layout.

**[06:00 UTC]** — Add triple-dial retry: redial same lead up to 3x before moving on — Power dialer now retries each lead up to 3 times before marking them as attempted and moving to the next contact in the queue.

**[05:53 UTC]** — Add core persona block to voice system prompt — Added a structured persona description block to the xAI Grok voice system prompt in `voice_bridge.py` to give the AI a consistent identity during calls.

**[05:47 UTC]** — Eliminate robotic high-pitched voice with anti-aliased audio pipeline — Added a scipy Butterworth low-pass filter and soxr resampling anti-aliasing to the voice audio pipeline to eliminate the robotic and high-pitched sound artifact during AI calls.

**[05:34 UTC]** — Fix agency owner getting provisioned as sub-account instead of master — Fixed provisioning logic that was incorrectly assigning agency owners to Twilio sub-accounts when they should connect to the master account.

**[05:18 UTC]** — Fix JWT signature validation failure for browser calling — Fixed Twilio AccessToken JWT signing to use the correct API key/secret pair, resolving browser VoIP registration failures.

**[04:55 UTC]** — Agency owner uses master Twilio account, sub-accounts for customers only — Refactored the Twilio account model so agency owners connect directly to the master Twilio account while only end customers receive isolated sub-accounts.

**[04:51 UTC]** — Fix Twilio SDK: use sub-account client instead of account_sid param — Fixed Twilio client initialization to use the sub-account SID as the client directly rather than passing it as a parameter, resolving sub-account API call failures.

**[04:25 UTC]** — Migrate voice system from Telnyx to white-label Twilio — Complete migration of the voice infrastructure from Telnyx to Twilio sub-accounts, maintaining white-label appearance so end users never see Twilio branding.

**[02:02 UTC]** — Auto-connect VoIP when Telnyx credential already exists — Added auto-trigger of VoIP registration on page load when a valid VoIP credential is already stored in the database.

**[01:54 UTC]** — Fix double AMD streaming + human dial fallthrough to AI mode — Fixed a race condition where AMD was triggering audio streaming twice and fixed human-dial mode incorrectly falling through to AI mode.

**[01:51 UTC]** — Rename AI minutes env vars to 500AI_PRICE_ID, 2000AI_PRICE_ID, etc. — Renamed AI Minutes Stripe price ID environment variables to a clearer, consistent naming scheme: `500AI_PRICE_ID`, `2000AI_PRICE_ID`, `5000AI_PRICE_ID`.

**[01:41 UTC]** — Add AI Minutes Marketplace: purchase bundles, track usage, auto-deduct on calls — Added a full AI Minutes billing system allowing users to purchase minute bundles via Stripe (500, 2000, or 5000 minute packs), track their balance in the database, and automatically deduct minutes on each AI voice call.

---

## 2026-02-19

**[23:15 UTC]** — Automate spam protection: single-form carrier registration with auto-CNAM — Added a single-form carrier registration flow with automatic CNAM (caller ID name) setting to reduce the likelihood of outbound calls being flagged as spam.

**[23:01 UTC]** — Add mobile responsive design + full UI audit — Added responsive CSS breakpoints and mobile-friendly layout across the dashboard; conducted a full UI audit and fixed various layout issues discovered during the review.

**[20:38 UTC]** — Fix Numbers/Trust Hub blank tabs: unclosed div in Settings panel — Fixed an unclosed `<div>` tag in the voice settings panel that was causing the Numbers and Trust Hub tabs to render completely blank.

**[20:24 UTC]** — Fix AI latency/naturalness + intercept button direct transfer — Improved AI voice response latency and naturalness; fixed the intercept button to perform a direct call transfer instead of routing through a conference bridge.

**[20:10 UTC]** — Revamp Numbers & Trust Hub tabs with Wavv-like dialer features — Redesigned the Numbers and Trust Hub tabs to match a Wavv-style UI with number management, CNAM configuration, and Trust Hub registration forms.

**[19:50 UTC]** — Fix unclickable disposition buttons: async/event timing bug — Fixed an async timing issue where call disposition buttons (answered, voicemail, no answer) were unclickable due to an event handler attachment race condition.

**[19:45 UTC]** — Fix dead silence on calls: _pending_transfer crash + switch AMD to premium — Fixed a NoneType crash on `_pending_transfer` during calls that was causing dead silence; switched AMD to Twilio's premium tier for better machine detection accuracy.

**[19:42 UTC]** — Show all call controls in banner always (disabled until connected) — Made call control buttons (mute, hold, transfer, hang up) always visible in the call banner but disabled until a call is in the connected state.

**[19:34 UTC]** — Fix Numbers tab showing nothing: null-safety + row[0] KeyError — Fixed a `KeyError` crash and added null-safety checks in the Numbers tab backend route so the tab renders correctly even with incomplete data.



