# Subscription Flows - Complete Guide

This document explains how all three subscription tiers work with your **white-label compatible Private App OAuth** setup.

---

## 🔑 Key Constraint: White-Label Compatibility

**You CANNOT use marketplace installs** because of white-label requirements. All OAuth connections use your **Private App** credentials.

This means:
- ✅ Users can subscribe via your website
- ✅ Users connect via Private App OAuth (white-label safe)
- ❌ Users cannot install from GHL Marketplace

---

## Flow 1: Individual Plan ($98.99/month)

### User Journey
```
1. User visits insurancegrokbot.click
2. Clicks "Start Individual"
3. Stripe checkout (guest, no login required)
4. Pays $98.99/month
5. Redirected to success page
6. Receives email: "Connect your Lead Connector account"
7. Clicks OAuth link → Private App OAuth
8. System provisions individual account
9. User sets password via /register or /claim-account
10. Logs in → Dashboard → Configures bot
```

### Technical Flow
- **Route**: `/checkout` → Stripe guest checkout
- **Webhook**: `stripe_webhook` creates pending account
- **OAuth**: `/oauth/callback` provisions full account (state=private_app)
- **Registration**: `/register` lets user set password
- **Login**: Standard email/password

### Database
- Creates record in `subscribers` table
- `subscription_tier` = 'individual'
- `role` = 'individual'
- `onboarding_status` = 'pending_claim' (until password set)

---

## Flow 2: Agency Starter ($797.99/month)

### User Journey
```
1. User visits insurancegrokbot.click
2. Clicks "Start Agency Starter"
3. Stripe checkout (guest, no login required) ✅ NEW!
4. Pays $797.99/month
5. Redirected to success page
6. Receives email: "Connect your Lead Connector account"
7. Clicks OAuth link → Private App OAuth
8. System counts their GHL sub-accounts
9. Provisions agency account
10. User sets password
11. Logs in → Agency Dashboard → Manages sub-accounts
```

### What Changed (2026-01-27)
**BEFORE (Broken):**
- ❌ Required login before checkout
- ❌ Login required OAuth first
- ❌ OAuth required marketplace install
- ❌ Chicken and egg problem

**AFTER (Fixed):**
- ✅ Guest checkout (no login)
- ✅ Pay first, OAuth later
- ✅ Works with Private App
- ✅ White-label compatible

### Technical Flow
- **Route**: `/checkout/agency-starter` → Stripe guest checkout
- **Webhook**: `stripe_webhook` creates pending agency account
- **OAuth**: `/oauth/callback` counts sub-accounts and provisions
- **Seat Count**: Verified AFTER OAuth (not before payment)

### Seat Count Logic
The system counts GHL sub-accounts during OAuth:
- **1-10 sub-accounts**: Agency Starter tier ✅
- **11+ sub-accounts**: System auto-upgrades to Agency Pro tier
- **0 sub-accounts**: Warning logged, user can still use system

### Database
- Creates record in `agency_billing` table
- `subscription_tier` = 'agency_starter'
- `max_seats` = 10
- `active_seats` = actual count from GHL

---

## Flow 3: Agency Pro ($1,597.99/month)

### User Journey
```
1. User visits insurancegrokbot.click
2. Clicks "Start Agency Pro"
3. Stripe checkout (guest, no login required)
4. Enters GHL whitelabel domain (validation field)
5. Pays $1,597.99/month
6. Redirected to success page
7. Receives email: "Connect your Lead Connector account"
8. Clicks OAuth link → Private App OAuth
9. System provisions unlimited agency account
10. User sets password
11. Logs in → Agency Dashboard → Manages unlimited sub-accounts
```

### Technical Flow
- **Route**: `/checkout/agency-pro` → Stripe guest checkout
- **Validation**: Requires GHL whitelabel domain (deters single users)
- **Webhook**: `stripe_webhook` creates pending agency account
- **OAuth**: `/oauth/callback` provisions unlimited seats

### Database
- Creates record in `agency_billing` table
- `subscription_tier` = 'agency_pro'
- `max_seats` = 9999 (unlimited)
- `active_seats` = actual count from GHL

---

## OAuth Callback Logic

The `/oauth/callback` route handles ALL three flows:

### Detection Logic
```python
# Line 1791-1800
is_private_app = (state == "private_app")

if is_private_app:
    # User came from website (Stripe payment)
    # Use private app credentials
    client_id = PRIVATE_APP_CLIENT_ID
    client_secret = PRIVATE_APP_SECRET_ID
else:
    # User came from marketplace (should not happen due to white-label)
    # Use marketplace credentials
    client_id = GHL_CLIENT_ID
    client_secret = GHL_CLIENT_SECRET
```

### Tier Detection
```python
# Line 1844-1847
plan_tier = 'individual'
if is_agency_owner:
    plan_tier = 'agency_pro' if num_subs >= 10 else 'agency_starter'
```

### Account Provisioning
1. **Agency Owner**: Creates/updates record in `agency_billing`
2. **Individual User**: Creates/updates record in `subscribers`
3. **Sub-users**: Creates records in `subscribers` with `parent_agency_email`

---

## Email Triggers

After successful Stripe payment, users receive emails with OAuth links:

### Individual
```
Subject: Welcome to InsuranceGrokBot!
Body: Click here to connect your Lead Connector account
Link: https://marketplace.gohighlevel.com/oauth/chooselocation?response_type=code&redirect_uri=https://insurancegrokbot.click/oauth/callback&client_id=YOUR_PRIVATE_APP_ID&scope=...&state=private_app
```

### Agency Starter / Pro
```
Subject: Welcome to InsuranceGrokBot Agency!
Body: Click here to connect your Lead Connector agency account
Link: [Same OAuth link with state=private_app]
```

---

## Common Issues & Solutions

### Issue 1: "Please log in to verify eligibility"
**Cause**: Old Agency Starter checkout required login
**Fix**: ✅ Fixed! Now uses guest checkout

### Issue 2: "White-label breach detected"
**Cause**: Directing users to marketplace.gohighlevel.com
**Fix**: Use Private App OAuth with state=private_app

### Issue 3: "User can't find OAuth link"
**Cause**: Email not sent or spam filtered
**Fix**:
- Check Stripe webhook logs
- Resend email manually
- Provide OAuth link in dashboard

### Issue 4: "Wrong seat count after OAuth"
**Cause**: GHL sub-account count doesn't match purchased plan
**Fix**: System auto-adjusts tier or logs warning. User can contact support for plan change.

---

## Testing Checklist

### Individual Plan
- [ ] Click "Start Individual" → Guest checkout works
- [ ] Pay with test card → Webhook provisions account
- [ ] Receive OAuth email
- [ ] Click OAuth link → Private app flow works
- [ ] Set password → Can log in
- [ ] Dashboard shows individual features

### Agency Starter
- [ ] Click "Start Agency Starter" → Guest checkout works (no login required)
- [ ] Pay with test card → Webhook provisions account
- [ ] Receive OAuth email
- [ ] Click OAuth link → Private app flow works
- [ ] System counts sub-accounts correctly
- [ ] Set password → Can log in
- [ ] Agency dashboard shows up to 10 seats

### Agency Pro
- [ ] Click "Start Agency Pro" → Guest checkout works
- [ ] Enter whitelabel domain → Validation passes
- [ ] Pay with test card → Webhook provisions account
- [ ] Receive OAuth email
- [ ] Click OAuth link → Private app flow works
- [ ] System provisions unlimited seats
- [ ] Set password → Can log in
- [ ] Agency dashboard shows unlimited seats

---

## Summary: Why This Works Now

**Before**: Agency Starter was broken because it required login → OAuth → marketplace install (not white-label compatible)

**After**: All three tiers now use the same flow:
1. ✅ Guest checkout (pay first)
2. ✅ Private App OAuth (white-label safe)
3. ✅ Account provisioning based on GHL data
4. ✅ Password setup
5. ✅ Login and configure

**Result**: White-label compatible, no marketplace needed, smooth user experience across all tiers.
