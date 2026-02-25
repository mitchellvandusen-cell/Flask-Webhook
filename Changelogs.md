# Changelog

All notable changes to InsuranceGrokBot are documented here.

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
- **faq.html**: Updated "Does it only work in Lead Connector?" from "Right now, yes" to accurately list all 7 CRM integrations (LeadConnector, Salesforce, HubSpot, Pipedrive, Zoho, Insureio, Zapier).

### Voice & Dialer Fixes
- **Transfer hangup fix**: Added `action` URL to `<Dial>` in both `/voice/transfer-twiml` and `/voice/intercept-twiml`. New `/voice/transfer-complete` endpoint returns `<Hangup/>` so parent calls are released when transfer ends — no more lingering "transferred" calls.
- **Hangup for transferred calls**: `/voice/hangup` now detects transferred calls and completes child call legs via Twilio REST API before completing the parent.
- **Terminal state cleanup**: `transferred` added to terminal status lists in call-status polling and voice status webhook.
- **Timezone-aware stats**: `/voice/stats` now uses the subscriber's configured timezone instead of UTC for all date calculations — "today" means today in user's local time, hourly distribution shows local hours.
- **Gunicorn threads**: Increased from 4 → 40 with `--timeout 0` for WebSocket support.
- **Hangup race fix**: Server-side success flag for reliable call termination.
- **Worker timeout fix**: Resolved issue where worker timeouts killed active calls.
- **Dialer cleanup**: Structural cleanup, double-dial guard, instant hangup, timer cleanup, heavy logging audit.
- **Listen/intercept fixes**: Fixed listen WebSocket receive compatibility, intercept call SID race condition, intercept double-fire race.

## 2026-02-24

### A2P 10DLC Compliance
- **Full A2P backend**: Brand/campaign registration, import flow, and Stripe fee gate via `twilio_provisioning.py`.
- **Full A2P frontend**: Import/register UI, campaign form, Stripe payment gate in `numbers.js`.
- **Dynamic Stripe pricing**: Pricing varies by brand type (sole proprietor, LLC, etc.) with SSN field for sole proprietors.
- **Aligned with Twilio ISV**: Onboarding matches Twilio ISV docs and ERC API specs.
- **Removed Import Existing flow**: Simplified to Register New only.

### SMS Channel Selector
- **Send From picker**: Users can choose between GHL (LeadConnector) or InsuranceGrokBot (Twilio A2P) for outbound SMS delivery.

### UI/UX
- **Enterprise light mode**: Full theme overhaul with glassmorphism styling.
- **Tutorial redesign**: Premium glassmorphism with refraction and light streak effects.
- **Tutorial positioning fix**: Removed `position:relative` that broke driver.js centering.

### Documentation
- Updated CLAUDE.md, OWNERS_MANUAL.md with A2P 10DLC sections.
