# Agent Page Builder — Design Spec

**Date:** 2026-04-07
**Status:** Draft
**Author:** Mitchell + Claude

## Overview

Transform the static agent landing page into a section-based page builder. Agents answer AI-powered questionnaires per section, and xAI Grok writes professional copy for them. Sections are toggleable, reorderable, and always look premium. Profile photo support. Public review page for client testimonials. Carrier section auto-synced from dashboard.

Target user: solo insurance agent, non-technical, expects luxury UI.

## Constraints

- All page data stored in Cloudflare Workers KV (existing namespace)
- Rendered by existing `omnisconn-agent-pages` Worker (expanded, not replaced)
- AI copy generation via xAI Grok (`grok-4-1-fast-non-reasoning`) through existing `llm_caller.py`
- Dashboard editor lives in the existing Domain tab (`templates/dashboard/tabs/domain.html` + `static/js/dashboard/domain.js`)
- Must follow Omnisconn brand discipline (single-accent luxury, no glassmorphism, solid backgrounds, dark+light theme)
- Backward compatible: existing KV configs without `sections` key render as before

## Sections

9 sections total. 3 fixed (always on), 6 toggleable.

| # | Section | Toggleable | AI Questionnaire | Data Source |
|---|---------|-----------|-----------------|-------------|
| 1 | **Hero** | No (always on) | No | KV: agent_name, phone, photo_url |
| 2 | **About Me** | Yes | Yes (5 questions) | AI-generated, editable |
| 3 | **Services I Offer** | Yes | Yes (3 questions) | AI-generated + product checkboxes |
| 4 | **Why Choose Me** | Yes | Yes (3 questions) | AI-generated |
| 5 | **Carriers I Represent** | Yes | No (auto-synced) | Dashboard Carriers tab data |
| 6 | **Testimonials** | Yes | No | Manual + /review page submissions |
| 7 | **FAQ** | Yes | No | Pre-written toggleable Q&As |
| 8 | **Contact Form** | No (always on) | No | Existing form |
| 9 | **Footer** | No (always on) | No | Business name, links, Omnisconn |

### Section Details

#### 1. Hero (fixed)
- Agent name (large, Playfair Display)
- Circular profile photo next to name (hidden if no photo uploaded)
- "Licensed Life Insurance Professional" subtitle
- Licensed states badges
- Phone CTA button
- Business name (DBA) eyebrow text

#### 2. About Me (toggleable, AI)
**Questionnaire (5 questions, multiple choice + "Other" text field):**
1. How long have you been in insurance? → 1-3 years / 3-10 / 10+ / Other
2. What got you into insurance? → Help families / Financial freedom / Career change / Family business / Other
3. What's your specialty? → Final expense / Term life / IUL / Medicare / Retirement planning / Other
4. What makes you different? → Always available / Education-first / Local presence / Bilingual / Other
5. How do you work with clients? → No-pressure / Consultative / Fast & efficient / Long-term relationship / Other

**AI prompt:** Takes answers + agent name + DBA → generates 2-3 paragraph professional bio (~150-200 words). Warm, trustworthy tone. No AI slop.

**Output:** Editable text block. Agent can tweak wording after generation. "Regenerate" button available.

#### 3. Services I Offer (toggleable, AI)
**Questionnaire (3 questions):**
1. What types of coverage do you offer? → Term Life / Whole Life / IUL / Final Expense / Annuities / Medicare / Group Benefits / Other (multi-select)
2. Who are your typical clients? → Young families / Seniors / Business owners / Middle-income earners / Other
3. What's the most important thing clients should know about coverage? → (open text)

**AI prompt:** Takes answers → generates a professional services overview paragraph + a structured list of services with 1-line descriptions.

**Output:** Intro paragraph (editable) + service list (auto-generated from checkboxes, each with a short description).

#### 4. Why Choose Me (toggleable, AI)
**Questionnaire (3 questions):**
1. What do clients say they appreciate most about working with you? → Responsiveness / Clear explanations / No pressure / Competitive rates / Other
2. Any credentials or achievements? → MDRT / Top producer / Certifications / 100+ families helped / Other
3. What's your promise to clients? → (open text)

**AI prompt:** Takes answers → generates 3-4 value proposition cards (icon + headline + 1-2 sentence description).

**Output:** Grid of value prop cards. Editable headlines and descriptions.

#### 5. Carriers I Represent (toggleable, auto-synced)
- Reads from `voice_config.contracted_carriers` (existing data from Carriers tab)
- Displays carrier names in a clean grid
- No questionnaire needed — data already exists
- Agent toggles section on/off; carrier selection happens in the Carriers tab
- If no carriers selected, section auto-hides even if toggled on

#### 6. Testimonials (toggleable, manual + review page)

**Manual entry:** Agent adds testimonials via dashboard form (client name + review text + optional star rating).

**Public review page (`/review`):**
- Worker serves a clean form at `yourdomain.com/review`
- Fields: Name, Star rating (1-5 clickable stars), Review text, Submit
- POST to `/review-submit` → Worker appends to `reviews:{domain}` KV key as pending
- Honeypot field for bot protection

**Approval flow:**
- Dashboard shows pending reviews with approve/reject buttons
- Only approved reviews render on the live page
- Agent can also delete approved reviews

**Display:** Testimonial cards with name, stars, quote text. Max 6 displayed (most recent approved).

#### 7. FAQ (toggleable, pre-written)
Pre-built FAQ items relevant to insurance agents:
1. "How much does life insurance cost?" → varies by age/health/coverage
2. "Do I need a medical exam?" → not always, many no-exam options
3. "How long does it take to get approved?" → 24 hours to 6 weeks depending on type
4. "Can I change my policy later?" → yes, most policies have conversion/adjustment options
5. "What happens if I miss a payment?" → grace period, reinstatement options
6. "Is my information secure?" → yes, encrypted and never shared

Each FAQ item has a toggle (show/hide on the live page). Agent can edit the answer text. Agent can add custom FAQ items (question + answer text fields).

#### 8. Contact Form (fixed)
Existing form — no changes. First name, last name, phone, email (optional), SMS consent checkbox, honeypot.

#### 9. Footer (fixed)
Existing footer — no changes. Business name, agent name, privacy/terms links, "Powered by Omnisconn."

## Profile Photo

- Upload button in the Hero section editor
- Accepts JPG/PNG, max 2MB
- Client-side circular crop preview
- Stored as base64 in KV config (`photo_url` field) — KV supports 25MB values, photos are small
- Rendered as a circular image next to agent name in the hero section
- If no photo, hero renders exactly as it does today (no empty circle, no placeholder)

## KV Config Schema (expanded)

```json
{
  "agent_name": "Aramis Gorra",
  "dba_name": "Gorra Life Insurance",
  "phone_display": "(555) 123-4567",
  "phone_raw": "+15551234567",
  "email": "aramis@gorralifeinsurance.com",
  "licensed_states": ["TX", "FL"],
  "accent_color": "#1a6b4a",
  "location_id": "abc123",
  "photo_url": "data:image/jpeg;base64,...",
  "review_page_enabled": true,
  "carriers": ["Mutual of Omaha", "National Life Group", "Americo"],
  "sections": [
    {
      "type": "hero",
      "enabled": true,
      "order": 0
    },
    {
      "type": "about",
      "enabled": true,
      "order": 1,
      "content": "AI-generated bio text that the agent can edit..."
    },
    {
      "type": "services",
      "enabled": true,
      "order": 2,
      "content": "AI-generated services overview...",
      "service_types": ["Term Life", "IUL", "Final Expense"]
    },
    {
      "type": "why_me",
      "enabled": false,
      "order": 3,
      "content": "",
      "value_props": []
    },
    {
      "type": "carriers",
      "enabled": true,
      "order": 4
    },
    {
      "type": "testimonials",
      "enabled": true,
      "order": 5,
      "items": [
        {"name": "John D.", "text": "Great experience...", "stars": 5, "approved": true}
      ]
    },
    {
      "type": "faq",
      "enabled": true,
      "order": 6,
      "items": [
        {"q": "How much does life insurance cost?", "a": "...", "visible": true},
        {"q": "Do I need a medical exam?", "a": "...", "visible": true}
      ]
    },
    {
      "type": "contact_form",
      "enabled": true,
      "order": 7
    },
    {
      "type": "footer",
      "enabled": true,
      "order": 8
    }
  ]
}
```

**Backward compatibility:** Worker checks for `sections` key. If missing, renders the current flat layout using existing fields. No migration needed for existing domains.

## Dashboard UI: Edit Layout

### Entry Point
Domain tab → "Edit Layout" button (appears when domain is active). Opens a full-width editor view replacing the current 4-field form.

### Layout Editor
Accordion list of section cards. Each card shows:
- **Left:** Drag handle area (up/down slide)
- **Center:** Section icon + name + enabled/disabled badge
- **Right:** Toggle switch (on/off)

Clicking a section card expands it to show:
- For AI sections: "Build with AI" button (starts questionnaire) or editable text if already generated
- For Testimonials: List of testimonials + "Add Testimonial" form + pending reviews
- For FAQ: Toggleable FAQ items + "Add Custom FAQ" form
- For Carriers: Read-only list synced from Carriers tab with link to "Edit in Carriers tab"
- For Hero: Photo upload + name/phone fields (existing)

### Reordering
Up/down arrow buttons on each section card. Click up → section swaps with the one above. Click down → swaps with below. Fixed sections (hero, form, footer) are locked in position and grayed-out arrows.

### AI Questionnaire Flow
1. Agent clicks "Build with AI" on a section
2. Questions appear one at a time inside the expanded section card
3. Each question: multiple choice buttons + "Other" text field
4. After last question: "Generate" button
5. Loading shimmer → AI-generated text appears
6. Agent reads it. Options: "Use This" (saves), "Regenerate" (new generation), or edit the text directly
7. Section is now populated and enabled

### Preview
"Preview Site" button at the top of the editor. Opens live domain URL in new tab. KV updates are instant, so save → preview works immediately.

### Save
"Save Layout" button saves entire section config to KV via `/api/domain/sections`. Individual section edits (AI generation, testimonial add) save immediately via their own endpoints.

## API Endpoints

| Route | Method | Purpose |
|-------|--------|---------|
| `GET /api/domain/sections` | GET | Get current section config for editor |
| `POST /api/domain/sections` | POST | Save section order, toggles, content |
| `POST /api/domain/section-ai` | POST | AI questionnaire: `{section_type, answers}` → generated text |
| `POST /api/domain/photo` | POST | Upload profile photo (base64) |
| `GET /api/domain/reviews` | GET | List pending + approved reviews |
| `POST /api/domain/reviews/approve` | POST | Approve or reject a review |
| `DELETE /api/domain/reviews` | DELETE | Delete a review |

### AI Generation Endpoint Detail

`POST /api/domain/section-ai`

Request:
```json
{
  "section_type": "about",
  "answers": {
    "experience": "3-10 years",
    "motivation": "Help families",
    "specialty": "Final expense",
    "differentiator": "Always available",
    "approach": "No-pressure"
  }
}
```

Response:
```json
{
  "content": "Generated bio text...",
  "tokens_used": 180
}
```

Uses `llm_caller.py` → `grok-4-1-fast-non-reasoning`. System prompt per section type instructs the model to write professional insurance agent copy. No AI slop (enforced by reply_sanitizer patterns adapted for web copy).

## Worker Changes (agent-pages)

### New Routes
- `GET /review` → Review submission form (public)
- `POST /review-submit` → Handle review submission → append to `reviews:{domain}` KV

### Rendering Changes
- `landingPage()` function refactored to loop through `sections` array
- Each section type has its own render function: `renderHero()`, `renderAbout()`, `renderServices()`, `renderWhyMe()`, `renderCarriers()`, `renderTestimonials()`, `renderFaq()`, `renderContactForm()`, `renderFooter()`
- Sections rendered in `order` sequence, only if `enabled: true`
- Hero renders profile photo as circular image if `photo_url` exists
- Carriers section reads from `carriers` array in config
- Testimonials only renders approved items
- FAQ only renders items with `visible: true`

### Backward Compatibility
If KV config has no `sections` key, render using the current flat HTML (exact same output as today). Zero breaking changes for existing domains.

## Carrier Sync

When agent updates contracted carriers in the Carriers tab, the save handler also writes the carrier names to the domain KV config's `carriers` array. This keeps the website in sync without the agent doing anything extra.

**Implementation:** In the existing carrier save endpoint, after saving to `voice_config`, also call `_cf_store_agent_config()` to update the KV value with the new carrier list. Only if a domain is provisioned (`web_presence.status === 'active'`).

## Luxury UI Design Guidelines

All editor UI follows Omnisconn brand discipline:
- Solid backgrounds (#0a0a0a dark, #ffffff light)
- Single accent (#00ff88 dark, #059669 light) for active states and primary CTAs only
- Hard edges, 1px borders, no glassmorphism
- Section cards: `.biz-prof-*` pattern from business profile CSS
- AI questionnaire buttons: ghost buttons, accent on selected state
- Shimmer loading states during AI generation
- Both dark and light theme overrides required

Landing page itself:
- Clean, modern, minimal — Playfair Display headings, DM Sans body
- Agent's accent color used for CTA buttons and section accent borders
- White/light page background (this is a public-facing site, not the dashboard)
- Testimonial cards with subtle shadows
- Carrier grid clean and professional
- Mobile-first responsive design

## Out of Scope

- Image uploads for sections (only profile photo in hero)
- Custom CSS or font selection per agent
- Multi-page sites (no /about, /services routes — single page with sections)
- Drag-and-drop (up/down arrows only)
- AI-generated FAQ answers (pre-written only, agent can edit)
- Analytics per agent page (future feature)
