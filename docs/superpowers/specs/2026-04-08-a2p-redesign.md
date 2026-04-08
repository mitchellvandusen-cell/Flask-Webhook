# A2P 10DLC Registration Redesign — Design Spec

**Date:** 2026-04-08
**Status:** Approved
**Author:** Mitchell + Claude

## Overview

Redesign the A2P 10DLC registration flow from a payment-first blocked wizard into two separate, clean flows — Brand Registration and Campaign Registration — each with their own sidebar sub-menu, form-first UX, auto-populated fields, info tooltips with pre-approved samples, and pay-at-submit Stripe integration.

## Sidebar Navigation

Three-level nesting under Numbers:

```
Numbers
  ├ My Numbers
  ├ Number Health
  └ A2P 10DLC
      ├ Brand
      └ Campaign
```

Clicking "Brand" navigates to the Brand Registration panel. Clicking "Campaign" navigates to the Campaign Registration panel. Each is independent — not steps in a wizard.

## Brand Registration Panel

### States

1. **No brand registered:** "Create New Brand" primary CTA button centered in panel.
2. **Form open:** Expanded form with fields (see below). Submit button at bottom.
3. **Pending review:** Status card with brand info, status badge (Pending), auto-poll every 30s.
4. **Approved:** Green status card with brand details.
5. **Failed:** Red status card with failure reason + "Edit & Resubmit" button.

### Form Fields (auto-populated from voice_config.trust_hub)

| Field | Source | Editable |
|-------|--------|----------|
| Business Name | trust_hub.business_name | Yes |
| EIN | trust_hub.ein | Yes |
| Business Type | trust_hub.business_type | Yes (dropdown) |
| Street | trust_hub.street | Yes |
| City | trust_hub.city | Yes |
| State | trust_hub.state | Yes |
| ZIP | trust_hub.zip | Yes |
| Website | trust_hub.website | Yes |
| Contact Email | trust_hub.contact_email | Yes |
| Contact Phone | trust_hub.contact_phone | Yes |
| First Name | trust_hub.contact_name (split) | Yes |
| Last Name | trust_hub.contact_name (split) | Yes |
| Job Title | trust_hub.contact_title | Yes |
| Job Position | Mapped from title (CEO/Director/etc.) | Yes (dropdown) |
| Brand Type | User selects | Yes (radio: Sole Prop / Low Volume / Standard) |

### Brand Type Pricing

| Type | Brand Fee | Use Case |
|------|-----------|----------|
| Sole Proprietor | $4.50 | Individual agents without an LLC |
| Low Volume | $4.50 | Small agencies, <6000 msgs/day |
| Standard | $46.00 | High-volume agencies |

### Submit Flow

1. Agent fills/reviews form, clicks "Register Brand"
2. Frontend validates all required fields
3. POST to `/a2p/brand-checkout` — creates Stripe Checkout Session for brand fee
4. Agent completes Stripe payment
5. On success redirect: form data (saved in sessionStorage) submitted to `/voice/a2p/register-brand`
6. Brand status set to pending, form collapses, status card appears with polling

## Campaign Registration Panel

### States

1. **No brand:** Disabled panel with message "Register and get your Brand approved before creating a Campaign."
2. **Brand pending:** Same disabled state with "Brand is under review. Campaign registration will unlock once approved."
3. **Brand approved, no campaign:** "Create New Campaign" primary CTA button.
4. **Form open:** Expanded form with fields (see below).
5. **Pending review:** Status card with campaign info + auto-poll.
6. **Approved/Verified:** Green status card — A2P fully compliant.

### Form Fields

| Field | Info Tooltip | Required |
|-------|-------------|----------|
| Use Case | Dropdown, default "Lead Generation" | Yes |
| Campaign Description | (i) tooltip with example text | Yes (min 40 chars) |
| Message Flow (opt-in explanation) | (i) tooltip with example | Yes (min 40 chars) |
| Sample Message 1 | (i) tooltip with pre-approved example | Yes (min 20 chars) |
| Sample Message 2 | (i) tooltip with pre-approved example | Yes (min 20 chars) |
| Sample Message 3 | (i) tooltip with pre-approved example | No |
| Sample Message 4 | (i) tooltip with pre-approved example | No |
| Includes embedded links | Checkbox | Yes |
| Includes embedded phone numbers | Checkbox | Yes |
| Phone Numbers | Multi-select checkboxes | Yes (min 1) |

### Info Tooltip Content

**Campaign Description tooltip:**
> "We send personalized text messages to leads who have expressed interest in life insurance coverage. Messages include policy information, appointment reminders, and follow-up communications."

**Message Flow tooltip:**
> "Leads opt in by submitting a contact form on our website (yourdomain.com) which includes SMS consent language. They can opt out at any time by replying STOP."

**Sample Message 1 tooltip:**
> "Hi {First Name}, this is {Agent Name} with {Business Name}. I wanted to follow up on your interest in life insurance coverage. Do you have a few minutes to chat about your options?"

**Sample Message 2 tooltip:**
> "Hey {First Name}, just checking in — I put together some coverage options based on what we discussed. When works best for a quick call to go over them?"

**Sample Message 3 tooltip:**
> "Hi {First Name}, this is {Agent Name}. I noticed you were looking into life insurance options. I'd love to help you find the right coverage for your family. Is now a good time?"

**Sample Message 4 tooltip:**
> "Hi {First Name}, friendly reminder about our appointment tomorrow at {Time}. Looking forward to helping you find the right coverage. Reply YES to confirm or let me know if you need to reschedule."

### Campaign Fee

$15.00 flat — same for all brand types.

### Submit Flow

1. Agent fills form, clicks "Submit Campaign"
2. Frontend validates fields (description 40+ chars, 2+ sample messages 20+ chars each, phone selected)
3. POST to `/a2p/campaign-checkout` — creates Stripe Checkout Session for $15.00
4. Agent completes Stripe payment
5. On success redirect: form data submitted to `/voice/a2p/create-campaign`
6. Campaign status set to pending, form collapses, status card appears with polling

## API Changes

### New Endpoints

| Route | Method | Purpose |
|-------|--------|---------|
| `/a2p/brand-checkout` | POST | Create Stripe session for brand fee. Body: `{brand_type}` |
| `/a2p/campaign-checkout` | POST | Create Stripe session for campaign fee ($15) |

### Modified Endpoints

| Route | Change |
|-------|--------|
| `/voice/a2p/register-brand` | Remove payment gate check (`a2p_fee_paid`). Payment now verified via Stripe session metadata on redirect. |
| `/voice/a2p/create-campaign` | Same — remove payment gate. |

### Unchanged Endpoints

- `GET /voice/a2p/status` — still returns full A2P state
- `GET /voice/a2p/brand-status` — still polls Twilio
- `GET /voice/a2p/campaign-status` — still polls Twilio
- `POST /voice/a2p/import` — external brand/campaign import unchanged

## Stripe Integration

### Brand Checkout (`/a2p/brand-checkout`)

```python
# Metadata on Stripe session:
{
    "purchase_type": "a2p_brand",
    "brand_type": "LOW_VOLUME",  # or SOLE_PROPRIETOR, STANDARD
    "total_cents": 450,  # or 4600 for STANDARD
}
```

Success URL: `/dashboard?a2p_brand_paid=1`
Cancel URL: `/dashboard?a2p_brand_cancel=1`

### Campaign Checkout (`/a2p/campaign-checkout`)

```python
{
    "purchase_type": "a2p_campaign",
    "total_cents": 1500,
}
```

Success URL: `/dashboard?a2p_campaign_paid=1`
Cancel URL: `/dashboard?a2p_campaign_cancel=1`

### Webhook Handler

Existing Stripe webhook handler in `billing.py` extended to handle `a2p_brand` and `a2p_campaign` purchase types. Sets `voice_config.a2p.brand_fee_paid` and `voice_config.a2p.campaign_fee_paid` respectively.

### Backward Compatibility

Existing `a2p_fee_paid` flag still works for users who already paid the combined fee. The register-brand and create-campaign endpoints accept either the old combined flag OR the new per-step flags.

## Frontend Files

### Modified

- `templates/dashboard/_sidebar.html` — Add Brand and Campaign as L3 sub-items under A2P 10DLC
- `static/js/dashboard/numbers.js` — New Brand panel + Campaign panel rendering, form validation, auto-populate from trust_hub, info tooltips, Stripe redirect handling, status polling
- `templates/dashboard/tabs/config.html` — Remove old A2P wizard section (replaced by new panels)

### New CSS

Added to existing `static/css/dashboard/` — A2P form styles, info tooltip styles, status cards. Follows Omnisconn brand discipline: solid backgrounds, single accent, dark + light theme.

## Info Tooltip UI

Small `(i)` icon (circle with "i") next to the textarea label. On click/hover:
- Popup appears anchored to the icon
- Contains the pre-approved example text
- "Copy" button to copy text to clipboard
- Click outside or click icon again to dismiss
- Styled: dark card, subtle border, small text, accent icon

## voice_config.a2p Schema Changes

```json
{
  "brand_fee_paid": true,
  "brand_fee_paid_at": "2026-04-08T...",
  "brand_stripe_session_id": "cs_xxx",
  "campaign_fee_paid": true,
  "campaign_fee_paid_at": "2026-04-08T...",
  "campaign_stripe_session_id": "cs_xxx",
  // ... existing fields unchanged
}
```

## Out of Scope

- A2P import flow (external brand/campaign IDs) — unchanged
- Brand/campaign editing after submission (Twilio doesn't support this)
- Multiple brands per account
- Multiple campaigns per brand (Twilio supports this but we don't need it)
- Auto-selection of use case based on business type
