# Sub-User Onboarding System

## Overview

This system enables agency owners to invite sub-account users (agents) to create their own individual login credentials for the InsuranceGrokBot platform.

## Features

### For Agency Owners
- **Automatic Email Detection**: When you complete OAuth, the system automatically fetches user emails from GoHighLevel for each location
- **Dashboard Management**: View all sub-accounts with their onboarding status
- **One-Click Invitations**: Send invitation emails directly from the agency dashboard
- **Bulk Invites**: Invite all pending users at once
- **Status Tracking**: Monitor which users have claimed their accounts

### For Sub-Users (Agents)
- **Email Invitation**: Receive a professional onboarding email with activation link
- **Simple Setup**: Click link, set password, and start using the platform
- **Individual Login**: Each agent gets their own credentials (no shared passwords)
- **7-Day Validity**: Invitation links expire after 7 days for security

## How It Works

### 1. OAuth Flow (Automatic)

When an agency owner connects via GoHighLevel OAuth:

```
1. Agency owner clicks "Connect with GoHighLevel"
2. System exchanges code for access tokens
3. System fetches all locations/sub-accounts
4. For EACH location, system fetches assigned users
5. System stores:
   - location_id
   - email (owner's email for billing)
   - agent_email (individual agent's email)
   - onboarding_status (set to 'pending')
```

**Database Schema:**
```sql
ALTER TABLE subscribers ADD COLUMN agent_email TEXT;
ALTER TABLE subscribers ADD COLUMN invite_token TEXT;
ALTER TABLE subscribers ADD COLUMN invite_sent_at TIMESTAMP;
ALTER TABLE subscribers ADD COLUMN invite_claimed_at TIMESTAMP;
ALTER TABLE subscribers ADD COLUMN onboarding_status TEXT DEFAULT 'pending';
```

### 2. Sending Invitations

**From Agency Dashboard:**

1. Navigate to `/agency-dashboard`
2. Expand a sub-account accordion
3. Click "Send Invite" button (for pending users)
4. Or click "Resend Invite" (for invited but unclaimed users)
5. Or use "Invite All Pending Users" for bulk action

**API Endpoint:**
```bash
POST /api/agency/invite-sub-user
Content-Type: application/json

{
  "location_id": "loc_xyz123",
  "email": "agent@example.com"  # Optional override
}
```

**What Happens:**
1. System generates secure random token (32 bytes)
2. Updates database:
   - `invite_token` = generated token
   - `invite_sent_at` = NOW()
   - `onboarding_status` = 'invited'
3. Sends email with claim link:
   ```
   https://yourdomain.com/claim-account?token=abc123...
   ```

### 3. User Claims Account

**User Flow:**
1. Agent receives email
2. Clicks "Activate My Account" button
3. Lands on `/claim-account?token=abc123...`
4. Sees pre-filled email (read-only)
5. Creates password (8+ characters)
6. Confirms password
7. Clicks "Activate Account"

**Backend Processing:**
1. Validates token exists and hasn't expired (7 days)
2. Checks onboarding_status != 'claimed'
3. Hashes password with bcrypt
4. Updates database:
   - `password_hash` = hashed password
   - `email` = agent_email (replaces owner email)
   - `invite_token` = NULL
   - `invite_claimed_at` = NOW()
   - `onboarding_status` = 'claimed'
5. Redirects to `/login`

### 4. Agent Logs In

Agent can now log in normally at `/login` with:
- Email: their individual agent email
- Password: the password they set

## Email Configuration

### Setup (Choose One)

#### Option 1: Gmail (Testing)
```env
MAIL_SERVER=smtp.gmail.com
MAIL_PORT=587
MAIL_USE_TLS=True
MAIL_USERNAME=your-email@gmail.com
MAIL_PASSWORD=your-app-password
MAIL_DEFAULT_SENDER=your-email@gmail.com
```

**Note:** Enable "App Passwords" in Google Account settings.

#### Option 2: SendGrid (Recommended for Production)
```env
MAIL_SERVER=smtp.sendgrid.net
MAIL_PORT=587
MAIL_USE_TLS=True
MAIL_USERNAME=apikey
MAIL_PASSWORD=your-sendgrid-api-key
MAIL_DEFAULT_SENDER=noreply@yourdomain.com
```

#### Option 3: AWS SES
```env
MAIL_SERVER=email-smtp.us-east-1.amazonaws.com
MAIL_PORT=587
MAIL_USE_TLS=True
MAIL_USERNAME=your-ses-smtp-username
MAIL_PASSWORD=your-ses-smtp-password
MAIL_DEFAULT_SENDER=noreply@yourdomain.com
```

### Email Template

The invitation email includes:
- Personalized greeting with agent name
- Agency name
- Call-to-action button
- Expiry notice (7 days)
- Both HTML and plain text versions

Preview: `main.py:1734-1807`

## API Reference

### POST /api/agency/invite-sub-user
Send invitation to single sub-user.

**Request:**
```json
{
  "location_id": "loc_xyz123",
  "email": "agent@example.com"  // Optional
}
```

**Response (Success):**
```json
{
  "status": "success",
  "message": "Invite sent to agent@example.com"
}
```

**Response (Partial - Email Failed):**
```json
{
  "status": "partial",
  "message": "Invite created but email failed to send",
  "invite_url": "https://yourdomain.com/claim-account?token=..."
}
```

### POST /api/agency/resend-invite
Resend invitation to sub-user.

**Request:**
```json
{
  "location_id": "loc_xyz123"
}
```

### POST /api/agency/invite-all
Bulk invite all pending sub-users.

**Response:**
```json
{
  "status": "success",
  "invited": 5,
  "failed": 1,
  "message": "Invited 5 users (1 failed)"
}
```

### GET /claim-account?token=...
Display claim account form.

### POST /claim-account
Process account claim.

**Form Data:**
```
token: abc123...
password: newpassword123
confirm_password: newpassword123
```

## Onboarding Status States

| Status | Description | Actions Available |
|--------|-------------|-------------------|
| `pending` | User detected but not invited | Send Invite |
| `invited` | Invitation sent, awaiting claim | Resend Invite |
| `claimed` | User has set password and can log in | None (completed) |

## Security Features

1. **Secure Tokens**: 32-byte URL-safe random tokens
2. **Token Expiry**: 7-day validity period
3. **One-Time Use**: Tokens deleted after claim
4. **Password Hashing**: Bcrypt with salt
5. **Email Verification**: Only invited emails can claim
6. **HTTPS Required**: All invite links should use HTTPS

## Troubleshooting

### Email Not Sending

1. Check Flask logs for email errors
2. Verify SMTP credentials in `.env`
3. Test SMTP connection:
   ```python
   from flask_mail import Mail, Message
   # ... configure mail ...
   msg = Message("Test", recipients=["test@example.com"])
   mail.send(msg)
   ```

### Agent Email Not Detected

If `agent_email` is `null` after OAuth:
1. Check GoHighLevel API response for users
2. Verify location has assigned users in GHL dashboard
3. Manually set email in agency dashboard (future feature)

### Invite Link Expired

Agent must request a new invite from agency owner via "Resend Invite" button.

### Token Already Claimed

If user tries to use token twice, they're redirected to login.

## Database Queries

### Check Onboarding Status
```sql
SELECT
    location_id,
    full_name,
    agent_email,
    onboarding_status,
    invite_sent_at,
    invite_claimed_at
FROM subscribers
WHERE parent_agency_email = 'owner@agency.com'
ORDER BY onboarding_status, created_at DESC;
```

### Reset Invite (if needed)
```sql
UPDATE subscribers
SET
    onboarding_status = 'pending',
    invite_token = NULL,
    invite_sent_at = NULL,
    invite_claimed_at = NULL
WHERE location_id = 'loc_xyz123';
```

## Files Modified

1. **db.py**: Added invite system columns to `subscribers` table
2. **main.py**:
   - Updated `oauth_callback()` to fetch per-location users
   - Added `send_invite_email()` helper
   - Added `/api/agency/invite-sub-user` route
   - Added `/claim-account` route (GET/POST)
   - Added `/api/agency/resend-invite` route
   - Added `/api/agency/invite-all` route
   - Updated `agency_dashboard()` query
3. **requirements.txt**: Added `flask-mail`
4. **templates/claim_account.html**: New claim account form
5. **templates/agency-dashboard.html**: Added onboarding UI

## Testing Checklist

- [ ] OAuth flow populates `agent_email` correctly
- [ ] Agency dashboard shows onboarding status badges
- [ ] "Send Invite" button sends email
- [ ] Email arrives with correct formatting
- [ ] Invite link opens claim account page
- [ ] Claim account form validates passwords
- [ ] Account activation updates database
- [ ] Agent can log in with new credentials
- [ ] "Resend Invite" generates new token
- [ ] "Invite All" sends bulk emails
- [ ] Expired tokens show error message

## Future Enhancements

- [ ] Manual email entry/override in dashboard
- [ ] Custom email templates per agency
- [ ] Webhook notifications for claimed accounts
- [ ] SMS invitation option
- [ ] Multi-language support for emails
- [ ] Invitation analytics dashboard
