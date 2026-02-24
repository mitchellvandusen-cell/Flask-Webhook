# CHANGELOG — InsuranceGrokBot Flask-Webhook

> **Note on git history:** This repository's earliest visible commit in this clone is from **2026-02-19** (around PR #190). All history prior to that point is not available. PR numbers run from #190 through #239+. This changelog covers 74 code commits across three days.

---

## Key Milestones Summary

| Date | Milestone |
|------|-----------|
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

---

## 2026-02-24

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
