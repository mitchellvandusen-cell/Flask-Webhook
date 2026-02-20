# Complete Automation Summary

## 🎯 You Don't Have to Worry About Anything!

Everything is now **100% automated**. When an agency installs your app here's what happens automatically:

---

## 🔄 Automated Flow for Agency Owners 

### 1. **Agency Connects via OAuth**
```
User clicks "Connect with GoHighLevel"
   ↓
OAuth redirect to GHL
   ↓
GHL redirects back with authorization code
   ↓
AUTOMATIC: System exchanges code for tokens
```

### 2. **System Automatically:**
✅ Fetches user's email and name from GHL
✅ Detects if user is agency owner (has multiple locations)
✅ Fetches ALL locations/sub-accounts from GHL
✅ **For EACH location, fetches assigned users and their emails**
✅ Determines subscription tier (starter/pro based on count)
✅ Calculates max_seats and active_seats

### 3. **Database Automatically Populated:**

**In `agency_billing` table:**
- agency_email (owner's email)
- location_id (primary location)
- full_name, timezone, crm_user_id
- access_token, refresh_token, token_expires_at
- subscription_tier, max_seats, active_seats
- **NOTE:** password_hash is NULL (will be set next)

**In `subscribers` table (for EACH sub-account):**
- location_id (unique per location)
- email (owner's email for billing link)
- **agent_email** (individual agent's email from GHL) ⭐
- full_name (location name)
- role ('agency_sub_account_user')
- parent_agency_email (points to owner)
- **onboarding_status** ('pending') ⭐
- access_token/refresh_token (NULL except primary)

### 4. **User is Automatically Logged In:**
✅ OAuth callback creates Flask-Login session
✅ User is already logged in (no need to login again)

### 5. **Password Setup (One-Time):**
```
Agency owner has NO password yet
   ↓
System detects this automatically
   ↓
Redirects to: /set-password?type=agency
   ↓
Owner sets password (8+ chars)
   ↓
Password saved to agency_billing table
   ↓
Redirected to: /agency-dashboard
```

### 6. **Agency Dashboard Shows:**
✅ All sub-accounts in accordion list
✅ Each sub-account shows:
- Location name
- **Agent email** (auto-populated from GHL)
- **Onboarding status badge**:
  - 🟢 "Claimed" (agent has account)
  - 🟡 "Invited" (email sent, awaiting claim)
  - ⚪ "Pending" (not invited yet)
- Bot configuration
- Access token status
- **"Send Invite" button** (for pending)
- **"Resend Invite" button** (for invited)

---

## 🔄 Automated Flow for Sub-Users (Agents)

### 1. **Agency Owner Sends Invite**
```
Owner clicks "Send Invite" on agency dashboard
   ↓
System checks agent_email exists (from OAuth)
   ↓
Generates secure 32-byte token
   ↓
Updates subscribers table:
  - invite_token = generated token
  - invite_sent_at = NOW()
  - onboarding_status = 'invited'
   ↓
Sends email to agent_email with claim link
```

**Email Contains:**
- Professional HTML template
- "Activate My Account" button
- Link: `https://yoursite.com/claim-account?token=abc123...`
- 7-day expiry notice

### 2. **Agent Claims Account**
```
Agent clicks link in email
   ↓
Lands on /claim-account?token=abc123...
   ↓
Sees pre-filled email (read-only)
   ↓
Creates password (8+ chars)
   ↓
System validates token (not expired, not used)
   ↓
Updates subscribers table:
  - password_hash = hashed password
  - email = agent_email (replaces owner email)
  - invite_token = NULL
  - invite_claimed_at = NOW()
  - onboarding_status = 'claimed'
   ↓
Agent redirected to /login
   ↓
Agent logs in with their individual email + password
```

### 3. **Agent Dashboard Automatically Shows:**
✅ Their location-specific configuration
✅ Bot settings (timezone, calendar_id, etc.)
✅ Access tokens (if they have them)
✅ Conversation logs
✅ All data pulled from `subscribers` table automatically

---

## 🔄 Automated Flow for Individual Users

### 1. **Individual Connects via OAuth**
```
User clicks "Connect with GoHighLevel"
   ↓
OAuth flow (same as agency)
   ↓
System detects: is_agency_owner = False
   ↓
Creates entry in subscribers table:
  - location_id (their single location)
  - email (their email)
  - role ('individual')
  - access_token, refresh_token
  - onboarding_status ('pending')
```

### 2. **Password Setup (One-Time):**
```
Individual has NO password yet
   ↓
System detects this automatically
   ↓
Redirects to: /register?location_id=abc123
   ↓
User sets password
   ↓
Password saved to subscribers table
   ↓
Redirected to: /dashboard
```

---

## 📊 What Data is Fetched Automatically

### From GoHighLevel API (During OAuth):

**User Info (`/users/me`):**
- email
- name
- user_id (crm_user_id)

**Agencies (`/agencies/`):**
- agencies[] array (empty = individual, populated = agency owner)

**Locations (`/locations/`):**
- locations[] array (all locations/sub-accounts)
- For each location:
  - id (location_id)
  - name
  - timezone
  - address, city, state, etc.

**Users per Location (`/locations/{id}/users`):** ⭐ **NEW!**
- users[] array (all users assigned to this location)
- For each user:
  - email (**agent_email**)
  - name
  - id (user_id)
  - role

---

## 🎯 Dashboard Data Loading (Automatic)

### Agency Dashboard (`/agency-dashboard`):

**Query:**
```sql
SELECT
    location_id,
    full_name,          -- Location name
    email,              -- Owner's email (for parent link)
    agent_email,        -- Individual agent's email ⭐
    bot_first_name,
    timezone,
    access_token,
    subscription_tier,
    token_expires_at,
    onboarding_status,  -- pending/invited/claimed ⭐
    invite_sent_at      -- When invite was sent ⭐
FROM subscribers
WHERE parent_agency_email = %s  -- Current user's email
ORDER BY created_at DESC
```

**Template Displays:**
- ✅ Sub-account accordion list
- ✅ Onboarding status badges
- ✅ Agent emails (auto-populated)
- ✅ Invite/Resend buttons (contextual)
- ✅ Connection status (token expiry)

### Individual Dashboard (`/dashboard`):

**Query:**
```sql
-- Automatic via Flask-Login's current_user
-- Loads from subscribers table WHERE email = current_user.email
```

**Template Displays:**
- ✅ Location configuration
- ✅ Bot settings
- ✅ Access token status
- ✅ Profile info
- ✅ All from `current_user` object

---

## 🛡️ Security (Automatic)

✅ **Token Expiry:** 7-day expiry on invite tokens
✅ **One-Time Use:** Tokens deleted after claim
✅ **Password Hashing:** Bcrypt with salt
✅ **Email Verification:** Only invited emails can claim
✅ **Role Checking:** @login_required + role validation
✅ **SQL Injection Protection:** Parameterized queries
✅ **HTTPS Required:** All sensitive routes

---

## 🔄 Token Refresh (Automatic)

**Function:** `get_valid_token(location_id)` in `ghl_api.py`

```python
1. Check if token exists
2. Check if token expired (5-minute buffer)
3. If expired and has refresh_token:
   - POST to GHL OAuth endpoint
   - Get new access_token
   - Update database automatically
   - Return new token
4. If no refresh_token:
   - Use persistent token
   - Return existing token
```

**Used automatically in:**
- Webhook processing
- API calls to GHL
- Calendar operations
- CRM updates

---

## 📋 Complete Database Schema

### `subscribers` table:
```sql
location_id          TEXT PRIMARY KEY
email                TEXT UNIQUE           -- Owner's email or agent's email after claim
password_hash        TEXT                  -- Set during register or claim
full_name            TEXT                  -- Location name or user name
phone                TEXT
bio                  TEXT
role                 TEXT                  -- 'individual', 'agency_owner', 'agency_sub_account_user'

bot_first_name       TEXT DEFAULT 'Grok'
access_token         TEXT                  -- OAuth access token
refresh_token        TEXT                  -- OAuth refresh token
token_expires_at     TIMESTAMP
token_type           TEXT DEFAULT 'Bearer'
timezone             TEXT DEFAULT 'America/Chicago'
crm_user_id          TEXT
calendar_id          TEXT
initial_message      TEXT

parent_agency_email  TEXT                  -- Link to agency owner (NULL for individuals/owners)
subscription_tier    TEXT                  -- 'individual', 'agency_starter', 'agency_pro'
confirmation_code    TEXT
stripe_customer_id   TEXT

-- NEW: Onboarding system fields
agent_email          TEXT                  -- Individual agent's email from GHL ⭐
invite_token         TEXT                  -- Secure token for claiming account ⭐
invite_sent_at       TIMESTAMP             -- When invitation was sent ⭐
invite_claimed_at    TIMESTAMP             -- When agent claimed account ⭐
onboarding_status    TEXT DEFAULT 'pending' -- pending/invited/claimed ⭐

created_at           TIMESTAMP DEFAULT NOW()
updated_at           TIMESTAMP DEFAULT NOW()
```

### `agency_billing` table:
```sql
agency_email         TEXT PRIMARY KEY      -- Agency owner's email
location_id          TEXT UNIQUE           -- Primary location ID
password_hash        TEXT                  -- Set during /set-password
full_name            TEXT
phone                TEXT
bio                  TEXT
role                 TEXT DEFAULT 'agency_owner'

-- (Same OAuth and config fields as subscribers)

max_seats            INTEGER DEFAULT 10    -- Seat limit based on tier
active_seats         INTEGER DEFAULT 0     -- Count of sub-accounts

created_at           TIMESTAMP DEFAULT NOW()
updated_at           TIMESTAMP DEFAULT NOW()
```

---

## ✅ What You DON'T Have to Do

❌ Manually create sub-user accounts
❌ Manually fetch agent emails
❌ Manually send invitation emails
❌ Manually track onboarding status
❌ Manually update database records
❌ Manually refresh OAuth tokens
❌ Manually log in users after OAuth
❌ Manually check password status
❌ Manually redirect to correct dashboard

**Everything is automated!**

---

## 🚀 What Happens When Agency Installs

### **Complete Automated Flow:**

```
1. Agency owner clicks "Install" on your marketplace listing
      ↓
2. Redirected to GHL OAuth consent screen
      ↓
3. Owner approves permissions
      ↓
4. GHL redirects to: /oauth/callback?code=abc123
      ↓
5. AUTOMATIC: System does ALL of this:
   ✅ Exchanges code for tokens
   ✅ Fetches user info (email, name)
   ✅ Detects agency status
   ✅ Fetches all locations
   ✅ Fetches users for EACH location
   ✅ Inserts agency_billing entry
   ✅ Inserts subscribers entry for each sub-account
   ✅ Populates agent_email for each
   ✅ Sets onboarding_status to 'pending'
   ✅ Logs in the user via Flask-Login
   ✅ Detects if password needed
   ✅ Redirects to /set-password
      ↓
6. Owner sets password (one-time, 30 seconds)
      ↓
7. Redirected to /agency-dashboard
      ↓
8. Owner sees ALL sub-accounts with:
   ✅ Agent emails (already populated!)
   ✅ Onboarding status (all showing "Pending")
   ✅ "Send Invite" buttons
      ↓
9. Owner clicks "Invite All Pending Users" (one click!)
      ↓
10. AUTOMATIC: System sends emails to ALL agent_email addresses
       ↓
11. Agents receive beautiful emails with claim links
       ↓
12. Agents click link → Set password → Log in
       ↓
13. DONE! Everyone can use the platform
```

**Total manual work required: 2 clicks (set password + invite all)**

---

## 🎉 Result

**For Agency Owners:**
- OAuth → Set password → See all agents → Click "Invite All" → Done

**For Sub-Users:**
- Receive email → Click link → Set password → Log in → Use platform

**For You (Developer):**
- Zero manual intervention
- Zero worrying about installations
- System handles everything automatically
- Just monitor logs for any errors

**That's it! Your system is fully automated! 🚀**


