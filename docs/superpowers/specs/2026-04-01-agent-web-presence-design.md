# Agent Web Presence System — Design Spec

**Date:** 2026-04-01
**Status:** Draft
**Author:** Claude + Mitchell

---

## Problem

Twilio requires a live business website and domain-matching email for Secondary Business Profile approval (Voice Integrity, CNAM, SHAKEN/STIR). Most insurance agents don't have either. Competitors like WAVV abstract this away. We need to do the same.

## Solution

One-click domain + landing page + email provisioning. Agent enters their DBA name, picks a domain, pays $10/month. Everything auto-provisions in under 60 seconds — domain, DNS, SSL, landing page, email, and Twilio verification.

---

## User Flow

### Step 1: Agent enters business info

In the dashboard (Spam Protection tab or new "Web Presence" tab), agent sees:

```
┌─────────────────────────────────────────────────┐
│  Get Your Business Domain                       │
│                                                 │
│  Business Name (DBA): [Smith Life Insurance   ] │
│                                                 │
│  Available domains:                             │
│   ✓ smithlifeinsurance.com        $10/mo        │
│   ✓ smithlifeins.com              $10/mo        │
│   ✗ smithinsurance.com            taken         │
│                                                 │
│  Your email will be:                            │
│   john@smithlifeinsurance.com                   │
│                                                 │
│  ☐ I represent that this business name/DBA is   │
│    legally registered to me. Omnisconn    │
│    is not responsible for verifying business     │
│    registration, DBA filings, or compliance     │
│    with state/federal requirements. Domain       │
│    registration and web hosting are provided     │
│    as-is. I am solely responsible for ensuring   │
│    my business information is accurate and       │
│    legally compliant.                            │
│                                                 │
│  [Get My Domain — $10/month]                    │
└─────────────────────────────────────────────────┘
```

### Step 2: Payment via Stripe

Standard Stripe checkout. Recurring $10/month subscription.

### Step 3: Auto-provisioning (60 seconds, fully automated)

After payment confirms:

1. **Register domain** via Cloudflare Registrar API
2. **Configure DNS** via Cloudflare API:
   - A/AAAA records → Cloudflare Pages
   - MX records → Cloudflare Email Routing
   - TXT record → SPF for Mailgun
   - CNAME records → DKIM for Mailgun
   - TXT record → domain verification for Mailgun
3. **Add domain to Mailgun** via API (for outbound sending)
4. **Create email routing rule** via Cloudflare API (forward to agent's Gmail)
5. **Generate landing page** via Cloudflare Pages (or Workers)
6. **Save to DB** — domain, email, status in `voice_config` or new `web_presence` JSONB

### Step 4: Agent sees confirmation

```
┌─────────────────────────────────────────────────┐
│  ✓ Your domain is live!                         │
│                                                 │
│  Website: https://smithlifeinsurance.com        │
│  Email:   john@smithlifeinsurance.com            │
│                                                 │
│  Your website is live and your business email    │
│  is active. You can now register for Spam        │
│  Protection to get Voice Integrity, CNAM, and    │
│  SHAKEN/STIR caller ID.                          │
│                                                 │
│  [Register for Spam Protection →]               │
└─────────────────────────────────────────────────┘
```

---

## Architecture

```
Agent Dashboard
    │
    ▼
Flask API (new routes in blueprints/domain.py)
    │
    ├── Cloudflare Registrar API ── register domain ($9.15/yr .com)
    ├── Cloudflare DNS API ──────── MX, SPF, DKIM, A records
    ├── Cloudflare Email Routing ── forward inbound to agent's Gmail
    ├── Cloudflare Email Workers ── intercept Twilio verification emails
    ├── Mailgun API ─────────────── add sending domain + auto-reply
    ├── Cloudflare Pages/Workers ── serve landing page
    └── Stripe API ──────────────── $10/mo subscription
```

### Hosting: Cloudflare Workers

A single Cloudflare Worker serves all agent landing pages. It reads the `Host` header, looks up the agent's config from Workers KV, and renders the page.

```
Request: GET https://smithlifeinsurance.com/
    ↓
Cloudflare Worker reads Host header: "smithlifeinsurance.com"
    ↓
KV lookup: agents/{smithlifeinsurance.com} → {name, phone, states, etc.}
    ↓
Render HTML template with agent's data
    ↓
Return response (cached at edge)
```

### Email: Receive + Auto-Reply

```
Twilio sends email to john@smithlifeinsurance.com
    ↓
Cloudflare Email Routing → Email Worker
    ↓
Worker checks sender:
  - From Twilio? → Call our API endpoint
  - Not Twilio?  → Forward to agent's personal email
    ↓
Our API calls Mailgun: POST /v3/smithlifeinsurance.com/messages
  from: john@smithlifeinsurance.com
  to: [twilio sender]
  body: "Confirmed."
    ↓
Twilio receives reply → email verified
```

For non-Twilio emails, the Email Worker just forwards to the agent's personal email (Gmail, etc.) like normal Cloudflare Email Routing.

---

## Landing Page Design

Simple, professional, mobile-responsive. One page. No navigation. Sharp, bold, solid (matches Omnisconn design language but white-labeled to agent).

### Content

- Agent's name (large, bold)
- "Licensed Life Insurance Agent"
- Licensed states (badges)
- Phone number (click-to-call on mobile)
- Business name / DBA
- Contact form (name, email, phone, message)
- Brief "About" paragraph (auto-generated or agent-provided)
- Footer: "Powered by Omnisconn" (small, subtle) + disclaimer

### Contact Form

Submits to our API. Creates a lead in the agent's CRM (GHL or HubSpot) via existing CRM integration. This is the A2P lead form — pre-fills the opt-in for SMS consent.

Form fields:
- First Name (required)
- Last Name (required)
- Phone Number (required)
- Email (optional)
- SMS consent checkbox (**unchecked by default** — TCPA requirement) with full A2P disclosure
- Honeypot field (hidden, catches bots)
- Submit button

On submit: creates contact in agent's GHL/HubSpot via existing CRM integration, triggers Speed to Lead workflow if enabled. If no CRM connected, stores lead in `contact_cache` table with `source='web_form'` for later sync.

### Styling

- **Light mode by default** — insurance leads expect professional, clean, trustworthy pages
- Agent's white-label accent color if set, otherwise Omnisconn green
- Font: Outfit
- Solid backgrounds, sharp edges, mobile-first responsive

---

## Data Model

### Option A: JSONB on subscribers table

```json
// subscribers.voice_config.web_presence
{
    "domain": "smithlifeinsurance.com",
    "dba_name": "Smith Life Insurance",
    "email": "john@smithlifeinsurance.com",
    "email_forward_to": "john@gmail.com",
    "cloudflare_zone_id": "abc123...",
    "mailgun_domain_verified": true,
    "landing_page_published": true,
    "stripe_subscription_id": "sub_...",
    "stripe_price_id": "price_...",
    "provisioned_at": "2026-04-01T...",
    "status": "active",
    "disclaimer_accepted": true,
    "disclaimer_accepted_at": "2026-04-01T...",
    "licensed_states": ["TX", "CA", "FL"],
    "bio": "Helping families protect their future...",
    "phone_display": "(214) 555-1234"
}
```

### Option B: New table `agent_domains`

Not needed for MVP. JSONB on subscribers is simpler and follows existing patterns (voice_config, crm_config, whitelabel_config).

---

## API Routes (blueprints/domain.py)

| Route | Method | Purpose |
|-------|--------|---------|
| `/api/domain/search` | POST | Search available domains by DBA name |
| `/api/domain/checkout` | POST | Create Stripe subscription + start provisioning |
| `/api/domain/status` | GET | Current domain provisioning status |
| `/api/domain/update-page` | POST | Update landing page content (bio, phone, states) |
| `/api/domain/cancel` | POST | Cancel domain subscription |
| `/api/domain/contact-form` | POST | Public — receives contact form submissions |

Plus Stripe webhook handler for subscription lifecycle (reuse existing billing blueprint).

---

## Cloudflare API Calls (Provisioning Sequence)

### 1. Register Domain
```
POST /accounts/{account_id}/registrar/domains
{
    "name": "smithlifeinsurance.com",
    "auto_renew": true
}
```

### 2. Create DNS Zone (auto-created with registration)

### 3. Add DNS Records
```
# A record → Cloudflare Pages/Workers
POST /zones/{zone_id}/dns_records
{ "type": "A", "name": "@", "content": "192.0.2.1", "proxied": true }

# MX records → Cloudflare Email Routing
POST /zones/{zone_id}/dns_records
{ "type": "MX", "name": "@", "content": "route1.mx.cloudflare.net", "priority": 1 }

# SPF for Mailgun
POST /zones/{zone_id}/dns_records
{ "type": "TXT", "name": "@", "content": "v=spf1 include:mailgun.org ~all" }

# DKIM for Mailgun (records from Mailgun API response)
POST /zones/{zone_id}/dns_records
{ "type": "TXT", "name": "smtp._domainkey", "content": "..." }
```

### 4. Configure Email Routing
```
POST /zones/{zone_id}/email/routing/rules
{
    "matchers": [{ "type": "all" }],
    "actions": [{ "type": "worker", "value": ["email-handler"] }]
}
```

### 5. Add Domain to Mailgun
```
POST https://api.mailgun.net/v4/domains
{ "name": "smithlifeinsurance.com" }
```

### 6. Store Landing Page Config in Workers KV
```
PUT /accounts/{account_id}/storage/kv/namespaces/{ns_id}/values/smithlifeinsurance.com
{
    "agent_name": "John Smith",
    "dba_name": "Smith Life Insurance",
    "phone": "(214) 555-1234",
    "licensed_states": ["TX", "CA"],
    "bio": "...",
    "accent_color": "#00ff88"
}
```

---

## Costs

### Our Costs (Per Agent)

| Item | Cost | Frequency |
|------|------|-----------|
| .com domain | $9.15 | Per year |
| Cloudflare hosting | $0 | — |
| Cloudflare DNS | $0 | — |
| Cloudflare SSL | $0 | — |
| Cloudflare Email Routing | $0 | — |
| Mailgun (sending domain) | $0 | Existing account |
| Custom Hostname | $0.10 | Per month |
| **Total** | **~$10.35/year** | **$0.86/month** |

### Revenue (Per Agent)

| Plan | Price | Margin |
|------|-------|--------|
| $10/month | $120/year | 91% ($109.65 profit) |

### Platform Fixed Costs

| Item | Cost |
|------|------|
| Cloudflare Free plan | $0 |
| Cloudflare Custom Hostnames base | $2/month |
| Workers KV | $0 (free tier: 100K reads/day) |
| **Total** | **$2/month** |

---

## Stripe Integration

New price: `STRIPE_DOMAIN_PRICE_ID` — $10/month recurring.

On subscription creation → trigger provisioning.
On subscription cancellation → mark domain inactive, optionally release after grace period.
On payment failure → email warning, suspend after 30 days.

---

## Security & Legal

### Disclaimer (Required before purchase)

"I represent that the business name/DBA entered is legally registered to me or my business entity. Omnisconn provides domain registration and web hosting services as-is and assumes no responsibility for verifying business registrations, DBA filings, or compliance with state or federal regulations. I am solely responsible for ensuring all business information is accurate, truthful, and legally compliant. Domain registration is subject to ICANN policies and the Cloudflare Registrar terms of service."

### Data Handling

- Agent's EIN/SSN is never stored on the landing page or in Workers KV
- Contact form submissions go through our API (HTTPS, CSRF-protected)
- Landing pages are static HTML served from Cloudflare's edge (no server-side rendering of sensitive data)

### WHOIS

- Cloudflare includes free WHOIS privacy
- Registrant info: agent's business name + our platform address (or agent's address per their preference)

---

## Implementation Phases

### Phase 1: Domain Search + Registration (MVP)
- Domain search UI in dashboard
- Stripe checkout for $10/mo
- Cloudflare Registrar API integration
- DNS auto-configuration
- Basic landing page template

### Phase 2: Email + Auto-Verification
- Cloudflare Email Routing setup
- Mailgun domain addition
- Email Worker for Twilio auto-reply
- Email forwarding to agent's personal inbox

### Phase 3: Landing Page Customization
- Agent can edit bio, phone, licensed states
- Contact form with CRM integration
- White-label accent color support

### Phase 4: Contact Form → CRM Pipeline
- Form submissions create contacts in GHL/HubSpot
- Trigger Speed to Lead workflow
- A2P opt-in consent tracking

---

## Dependencies

- Cloudflare account with Registrar enabled
- Cloudflare API token with DNS + Registrar + Email Routing + Workers permissions
- Mailgun API key (existing)
- Stripe (existing)
- New Stripe Price ID for domain subscription

## Decisions Made

1. **.com only** — simplest, cheapest ($9.15/yr), most professional
2. **30-day grace period** on cancellation before domain release
3. **Omnisconn branding** — "Powered by Omnisconn" footer, not Omnisconn
4. **Full A2P 10DLC compliance** — landing page includes Privacy Policy + Terms of Service pages with all carrier-required SMS disclosures

---

## A2P 10DLC Compliance — Website Content Requirements

The landing page system **doubles as the agent's A2P compliance website.** Every auto-generated site includes three pages:

### Page 1: Landing Page (homepage)
- Agent name, DBA, licensed states, phone, contact form
- Contact form with TCPA-compliant opt-in checkbox (unchecked by default)
- Links to Privacy Policy and Terms of Service in footer

### Page 2: Privacy Policy (`/privacy`)
Auto-generated with agent's business name. Must include all of:

- SMS data collection disclosure
- How phone numbers are used (recurring automated texts for insurance quotes, policy info, appointment reminders)
- **Phone numbers NOT sold/shared** (TCR specifically checks this)
- Opt-out instructions (reply STOP)
- HELP instructions (reply HELP + contact info)
- "Message frequency varies"
- "Message and data rates may apply"
- "Carriers are not liable for delayed or undelivered messages"
- "Consent is not a condition of purchase"
- Data retention period
- Effective date

### Page 3: Terms of Service (`/terms`)
Auto-generated with agent's business name. Must include:

- Program description (what messages the agent sends)
- Opt-in mechanism description
- Message frequency disclosure
- Cost disclosure (carrier rates may apply)
- Opt-out instructions (STOP keyword)
- HELP instructions
- Carrier liability disclaimer
- Link to Privacy Policy
- Modification rights

### Contact Form Opt-In Disclosure (on homepage)
Must appear **adjacent to the phone field, before the submit button:**

```
By providing your phone number and submitting this form, you agree to receive
recurring automated text messages from [Business Name] regarding insurance
quotes, policy information, and appointment reminders. Message frequency
varies. Message and data rates may apply. Reply STOP to opt out at any time.
Reply HELP for help. View our Privacy Policy and Terms of Service. Consent
is not a condition of purchase.
```

### TCPA-Compliant Checkbox (required, unchecked by default)
```html
<label>
  <input type="checkbox" name="sms_consent" required>
  I agree to receive recurring automated text messages from [Business Name]
  at the phone number provided. Consent is not a condition of purchase.
  Msg & data rates may apply. Msg frequency varies. Reply STOP to cancel.
  Reply HELP for help.
  <a href="/privacy">Privacy Policy</a> & <a href="/terms">Terms</a>.
</label>
```

### Consent Record Storage
On form submission, store in DB:
- Phone number
- Timestamp (UTC)
- IP address
- Exact disclosure text shown
- Checkbox state (must be checked)
- Page URL where consent was given

This satisfies TCPA written consent requirements and provides evidence for carrier audits.

### A2P Campaign Registration Alignment
When the agent registers their A2P campaign via Omnisconn:
- Campaign `message_flow` description references the website opt-in form
- Campaign `sample_messages` match the use cases described on the website
- Campaign `opt_in_type` = "WEB_FORM"
- Campaign `opt_in_url` = the agent's landing page URL

### Common Rejection Reasons This Prevents

| Rejection Reason | How Our Pages Prevent It |
|------------------|------------------------|
| No Privacy Policy | Auto-generated, always present |
| Privacy Policy doesn't mention SMS | SMS section is mandatory content |
| No Terms of Service | Auto-generated, always present |
| Pages behind login wall | All pages are public, no auth |
| No opt-in mechanism | Contact form with full disclosure |
| Missing STOP/HELP instructions | In Privacy Policy, Terms, and opt-in disclosure |
| "Message frequency varies" missing | In opt-in disclosure and Privacy Policy |
| "Message and data rates" missing | In opt-in disclosure and Privacy Policy |
| Phone numbers sold/shared | Privacy Policy explicitly says NOT shared |
| Pre-checked consent checkbox | Checkbox is unchecked by default |
| Website doesn't match brand | Domain IS the brand (DBA name) |
| Website is down | Hosted on Cloudflare edge, 99.99% uptime |

---

## Liability Disclaimer (Purchase-Time)

Before domain purchase, agent must accept:

"I represent that the business name/DBA entered is legally registered to me or my
business entity. Omnisconn provides domain registration, web hosting, and email
services as-is and assumes no responsibility for verifying business registrations,
DBA filings, or compliance with state or federal regulations. I am solely responsible
for ensuring all business information is accurate, truthful, and legally compliant.
The auto-generated privacy policy and terms of service are provided as templates and
do not constitute legal advice. I am responsible for reviewing and ensuring these
documents meet my specific legal requirements. Domain registration is subject to
ICANN policies and the Cloudflare Registrar terms of service."

---

## Domain Name Generation Logic

When agent enters their DBA name, generate domain suggestions:

1. Strip "LLC", "Inc", "Corp", "Insurance Agency", "Insurance Services" suffixes
2. Lowercase, remove special characters, collapse spaces
3. Generate variations:
   - `{name}insurance.com` (e.g., `smithlifeinsurance.com`)
   - `{name}ins.com` (e.g., `smithlifeins.com`)
   - `{name}.com` (e.g., `smithlife.com`)
   - `{firstname}{lastname}insurance.com` (e.g., `johnsmithinsurance.com`)
   - `{name}agency.com` (e.g., `smithlifeagency.com`)
4. Check availability for each via Cloudflare Registrar API
5. Show available options with price, hide taken ones

### Email Prefix

Agent chooses their email prefix during setup. Options:
- Their first name (default, e.g., `john@smithlifeinsurance.com`)
- `info@`
- `contact@`
- Custom prefix (validated: lowercase, alphanumeric, dots, hyphens)

---

## Provisioning Status & Error Handling

Domain registration and DNS are NOT instant. The provisioning flow is async:

### Status States
```
payment_confirmed → registering_domain → configuring_dns →
verifying_email → publishing_page → active
```

Each step can fail independently. On failure:
- Save partial progress to DB
- Show agent which step failed with clear error
- Retry button for failed step
- Refund via Stripe if domain registration itself fails

### DNS Propagation
- Cloudflare-managed domains propagate in seconds (not hours) because Cloudflare IS the authoritative nameserver
- Mailgun DKIM verification: poll every 30 seconds for up to 5 minutes
- If DKIM doesn't verify in 5 minutes: mark as `email_pending`, page still goes live, email verification retries in background via cron

### Error Scenarios

| Failure | Handling |
|---------|----------|
| Domain taken (race condition after search) | Refund, show error, suggest alternatives |
| Cloudflare API down | Queue for retry, notify agent |
| Mailgun DNS verification slow | Page goes live, email retries in background |
| Stripe payment fails after provisioning | 30-day grace, then suspend page |
| Agent cancels within 24 hours | Full refund, release domain |

---

## Cancellation & Grace Period

### When agent cancels ($10/mo subscription):
1. **Immediately:** Landing page shows "This business is no longer active" (not a dead 404)
2. **30-day grace period:** Domain stays registered, agent can reactivate by resubscribing
3. **After 30 days:** Domain auto-renew disabled, released at end of registration year
4. **Email forwarding:** Disabled immediately on cancellation (no forwarding of leads to non-paying agent)

### When payment fails:
1. **Day 0:** Stripe retries automatically (3 attempts over 7 days)
2. **Day 7:** Email warning to agent
3. **Day 14:** Landing page shows maintenance notice
4. **Day 30:** Full suspension — same as cancellation

---

## Contact Form Anti-Spam

### Honeypot field
```html
<div style="position:absolute;left:-9999px;top:-9999px;">
  <input type="text" name="website_url" tabindex="-1" autocomplete="off">
</div>
```
If `website_url` field has a value → bot. Silently reject.

### Rate limiting
- 5 submissions per IP per hour
- 20 submissions per domain per hour
- Redis-based sliding window (existing pattern from API rate limiter)

### No reCAPTCHA needed for MVP
Honeypot + rate limiting catches 99% of bots. reCAPTCHA adds friction for real leads. Add later only if spam becomes a problem.

---

## Infrastructure: Cloudflare Worker + Email Worker

### Deployment
Both Workers are deployed separately from the Flask app. They run on Cloudflare's edge.

**Landing Page Worker** (`omnisconn-pages`):
- Deployed via Wrangler CLI or Cloudflare Dashboard
- Single Worker serves ALL agent domains (reads Host header → KV lookup → render)
- Template is baked into the Worker code (HTML string with `{{placeholder}}` substitution)
- Privacy Policy and Terms pages are separate routes (`/privacy`, `/terms`)
- Contact form POST goes to our Flask API (CORS allowed from all agent domains)

**Email Worker** (`omnisconn-email`):
- Deployed via Wrangler CLI
- Receives all inbound email for all agent domains
- If sender is `*@twilio.com` → call our Flask API to trigger Mailgun reply
- Otherwise → forward to agent's personal email via Email Routing destination

### One-Time Setup
1. Create Cloudflare account (or use existing)
2. Generate API token with: Zone:Edit, DNS:Edit, Email Routing:Edit, Workers:Edit, Registrar:Edit
3. Store token as env var: `CLOUDFLARE_API_TOKEN`
4. Store account ID as env var: `CLOUDFLARE_ACCOUNT_ID`
5. Deploy both Workers via `wrangler deploy`
6. Create Workers KV namespace for agent configs

---

## IGB "Also known as Omnisconn" references

All references in the spec use Omnisconn branding. The Flask codebase is still InsuranceGrokBot internally — the rebrand is customer-facing only for now. The landing pages, footer, and agent-facing UI say "Omnisconn." Backend code, repo name, and CLAUDE.md stay as-is until full rebrand.
