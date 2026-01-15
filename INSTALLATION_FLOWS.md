# Installation Flows - Complete Guide

## Overview
Your app supports **THREE different installation methods** that all lead to the same `/oauth/callback` endpoint. Here's exactly how each one works.

---

## Environment Variables Required

Make sure these are set in Railway:

```bash
# Private App (for Stripe/website users)
PRIVATE_APP_CLIENT_ID=696955e41f0b9e81b5d3644c-mkfym1kx
PRIVATE_APP_SECRET_ID=[your private app secret]

# Public Marketplace App
GHL_CLIENT_ID=695e99698a5ae81dbb426515-mkaz316h
GHL_CLIENT_SECRET=[your public app secret]

# Domain
YOUR_DOMAIN=https://insurancegrokbot.click
```

---

## Flow 1: Website → Stripe → "Connect with GoHighLevel" Button

**User Journey:**
1. User visits insurancegrokbot.click
2. Clicks "Individual Plan" and pays via Stripe checkout
3. Redirected to `/success` page
4. Clicks through to `/register` page
5. Creates account with email/password
6. Redirected to `/login`, logs in
7. Goes to dashboard
8. **Clicks "Connect with GoHighLevel" button** on register page (or from dashboard instructions)

**Technical Flow:**
```
User clicks button
    ↓
GET /oauth/initiate
    ↓
Builds OAuth URL with:
    - client_id: PRIVATE_APP_CLIENT_ID (696955e41f0b9e81b5d3644c-mkfym1kx)
    - redirect_uri: https://insurancegrokbot.click/oauth/callback
    - scopes: calendars.readonly, calendars/events.readonly, etc.
    - state: "private_app" ← KEY IDENTIFIER
    ↓
Redirects to: https://marketplace.gohighlevel.com/oauth/chooselocation?...&state=private_app
    ↓
User approves in GHL
    ↓
GHL redirects back to: /oauth/callback?code=ABC123&state=private_app
    ↓
oauth_callback() detects state="private_app"
    ↓
Uses PRIVATE_APP_CLIENT_ID + PRIVATE_APP_SECRET_ID to exchange code for token
    ↓
Saves access_token, refresh_token to database (subscribers table)
    ↓
Automatically logs user in via login_user()
    ↓
Redirects to /dashboard (user is now connected!)
```

**Your Installation Link:**
```
https://marketplace.gohighlevel.com/oauth/chooselocation?response_type=code&redirect_uri=https%3A%2F%2Finsurancegrokbot.click%2Foauth%2Fcallback&client_id=696955e41f0b9e81b5d3644c-mkfym1kx&scope=calendars.readonly+calendars%2Fevents.readonly+calendars%2Fevents.write+conversations%2Fmessage.write+conversations%2Fmessage.readonly+contacts.readonly+locations.readonly+calendars%2Fgroups.readonly+conversations.write&version_id=696955e41f0b9e81b5d3644c
```

---

## Flow 2: GHL Marketplace → Install App

**User Journey:**
1. User browses GHL marketplace
2. Finds your app "Insurance Grok Bot"
3. Clicks "Install"
4. GHL prompts them to choose location and approve scopes
5. Automatically redirected to your app with authorization code

**Technical Flow:**
```
User clicks "Install" in GHL marketplace
    ↓
GHL shows consent screen with your public app scopes
    ↓
User approves
    ↓
GHL redirects to: /oauth/callback?code=XYZ789&version_id=695e99698a5ae869a9426516
    ↓
oauth_callback() detects NO state parameter (state is None)
    ↓
Uses GHL_CLIENT_ID + GHL_CLIENT_SECRET (public app credentials)
    ↓
Exchanges code for token
    ↓
Fetches user info, locations, determines if agency owner
    ↓
Creates/updates database entries (agency_billing or subscribers table)
    ↓
Saves access_token, refresh_token to database
    ↓
Automatically logs user in via login_user()
    ↓
IF password doesn't exist:
    Redirects to /set-password (user sets password while logged in)
ELSE:
    Redirects to /dashboard or /agency-dashboard (fully set up!)
```

**Your Installation Link:**
```
https://marketplace.gohighlevel.com/oauth/chooselocation?response_type=code&redirect_uri=https%3A%2F%2Finsurancegrokbot.click%2Foauth%2Fcallback&client_id=695e99698a5ae81dbb426515-mkaz316h&scope=calendars.readonly+calendars.write+calendars%2Fevents.readonly+calendars%2Fevents.write+conversations%2Fmessage.write+conversations%2Fmessage.readonly+contacts.readonly+locations%2FcustomValues.write+locations%2FcustomFields.write+locations%2FcustomFields.readonly+locations%2FcustomValues.readonly&version_id=695e99698a5ae869a9426516
```

---

## Flow 3: White Label Marketplace → Install App

**User Journey:**
1. User browses **LeadConnector HQ** marketplace (white label version of GHL)
2. Finds your app
3. Clicks "Install"
4. Exactly same as Flow 2, just different marketplace domain

**Technical Flow:**
```
IDENTICAL to Flow 2, except:
    ↓
Uses marketplace.leadconnectorhq.com instead of marketplace.gohighlevel.com
    ↓
Everything else is exactly the same
    ↓
Uses same public app credentials (GHL_CLIENT_ID + GHL_CLIENT_SECRET)
```

**Your Installation Link:**
```
https://marketplace.leadconnectorhq.com/oauth/chooselocation?response_type=code&redirect_uri=https%3A%2F%2Finsurancegrokbot.click%2Foauth%2Fcallback&client_id=695e99698a5ae81dbb426515-mkaz316h&scope=calendars.readonly+calendars.write+calendars%2Fevents.readonly+calendars%2Fevents.write+conversations%2Fmessage.write+conversations%2Fmessage.readonly+contacts.readonly+locations%2FcustomValues.write+locations%2FcustomFields.write+locations%2FcustomFields.readonly+locations%2FcustomValues.readonly&version_id=695e99698a5ae869a9426516
```

---

## How /oauth/callback Knows Which App To Use

The callback route uses a **state parameter** to distinguish between flows:

```python
state = request.args.get("state")

if state == "private_app":
    # Flow 1: Stripe/website user clicked "Connect with GoHighLevel"
    client_id = PRIVATE_APP_CLIENT_ID
    client_secret = PRIVATE_APP_SECRET_ID
else:
    # Flow 2 & 3: GHL marketplace or white label installation
    client_id = GHL_CLIENT_ID
    client_secret = GHL_CLIENT_SECRET
```

**Key Points:**
- Private app link (Flow 1): **INCLUDES** `state=private_app` parameter
- Marketplace links (Flow 2 & 3): **NO state parameter** (or different version_id)
- Same `/oauth/callback` endpoint handles all three flows intelligently

---

## What Gets Saved to Database

After any flow completes, these are saved:

### For Individual Users (subscribers table):
```sql
location_id          -- User's GHL location ID
full_name           -- Location or user name
user_email          -- User's email
password_hash       -- Hashed password (if set)
access_token        -- OAuth access token
refresh_token       -- OAuth refresh token
token_expires_at    -- Token expiry timestamp
plan_tier           -- individual, agency_starter, or agency_pro
billing_status      -- active, trialing, etc.
stripe_customer_id  -- Stripe customer (if from website)
role                -- individual or agency_sub_account_user
```

### For Agency Owners (agency_billing table):
```sql
agency_email        -- Owner's email
access_token        -- OAuth access token
refresh_token       -- OAuth refresh token
token_expires_at    -- Token expiry timestamp
plan_tier           -- agency_starter or agency_pro
[plus all location data]
```

---

## Dashboard Access After Installation

After completing ANY flow above:

1. ✅ User is **automatically logged in** (via Flask-Login)
2. ✅ User is redirected to:
   - `/dashboard` (individual users)
   - `/agency-dashboard` (agency owners)
3. ✅ Dashboard shows:
   - Location ID
   - Access token status (masked for security)
   - Token expiry
   - Bot configuration fields
   - Sub-accounts (for agencies)
   - Onboarding status

---

## What Happens When User Logs In Later

User goes to `/login`:
1. Enters email + password
2. System validates credentials
3. Loads user from database (with access_token and refresh_token already stored)
4. Logs them in
5. Redirects to dashboard

**The OAuth tokens persist in the database**, so the user doesn't need to re-authorize GHL every time they log in. Tokens are refreshed automatically when they expire.

---

## Troubleshooting

### "No authorization code received"
- User denied permission in GHL
- OAuth link is malformed
- Check client_id matches your app

### "Could not retrieve user email"
- GHL API error
- Access token is invalid
- Check scopes include necessary permissions

### "Token exchange failed"
- client_secret is wrong
- client_id doesn't match the app that initiated OAuth
- Check environment variables are set correctly

### User stuck in password setup loop
- Password hash wasn't saved to database
- Check database connection
- Verify UPDATE query succeeded

---

## Quick Reference: Client IDs

| Flow | Client ID | State Parameter |
|------|-----------|----------------|
| Private app (Stripe users) | `696955e41f0b9e81b5d3644c-mkfym1kx` | `private_app` |
| Public marketplace | `695e99698a5ae81dbb426515-mkaz316h` | None |
| White label marketplace | `695e99698a5ae81dbb426515-mkaz316h` | None |

---

## Summary

✅ **All three flows are working and automated**
✅ **Same callback handles everything intelligently**
✅ **Users are auto-logged in after OAuth**
✅ **Tokens are saved to database permanently**
✅ **Dashboard shows all connected GHL data**

You don't need to do anything manually - the system handles everything automatically!
