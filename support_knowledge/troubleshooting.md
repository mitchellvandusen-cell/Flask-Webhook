# InsuranceGrokBot Troubleshooting Guide

## Bot Not Responding to Messages

### Symptoms
- Leads text in but get no response
- Activity log shows no new entries
- Dashboard shows "last activity" as hours/days ago

### Diagnostic Steps
1. Check subscription status — is there an active plan? Look for "Subscription Required" banner
2. Check CRM connection — is the CRM token valid or expired?
3. Check calendar — is a calendar selected and does it have availability?
4. Check bot configuration — is the bot name set? Is a calendar selected?
5. Check error logs — look for "auth" errors (token expired) or "rate_limit" errors

### Common Causes
- **CRM token expired**: The most common cause. Tokens expire after 24 hours (GHL) or 6 hours (HubSpot). Fix: Dashboard → Connect Lead Connector → re-approve
- **No subscription**: User needs to activate a plan
- **Calendar not set**: Bot can't book appointments without a calendar. Fix: Bot Configuration → Load calendars → select one
- **Bot paused**: User may have toggled the bot off. Check bot settings
- **Message queue backup**: If many messages arrive at once, there can be a delay. Check worker logs

### Resolution
- CRM expired: Reconnect from dashboard (Connect Lead Connector button)
- No calendar: Load and select calendar in Bot Configuration
- Queue backup: Usually self-resolves within minutes. Check worker health if persists

---

## CRM Connection Issues

### "Token Expired" in sidebar
The CRM connection needs refreshing. This happens automatically but sometimes fails.

**Fix**: Dashboard → click "Connect Lead Connector" → approve the connection. Takes 30 seconds.

### GHL OAuth repeatedly expiring
- Check if the GHL marketplace app has the correct scopes (18 required)
- Ensure the redirect URI matches exactly
- Try revoking and re-installing the GHL marketplace app
- Our system auto-refreshes tokens every 15 minutes; if this fails consistently, there may be an issue with the GHL account

### HubSpot token issues
- HubSpot tokens expire every 6 HOURS (much faster than GHL)
- Automatic refresh runs every 15 minutes
- If refresh fails: check HubSpot app permissions haven't changed
- Try: Disconnect HubSpot → reconnect from Integrations page

---

## Calendar and Booking Issues

### Calendar slots not showing / wrong times
1. Make sure a calendar is selected in Bot Configuration
2. Check that the calendar has availability set up in the CRM (GoHighLevel → Calendars)
3. Make sure the timezone setting matches the CRM calendar timezone
4. Verify the calendar isn't fully booked for the requested dates
5. Try: Bot Configuration → click "Load" → reselect the calendar

### Bot says "I don't have my schedule pulled up right now"
- Calendar API returned an error (transient)
- Usually self-resolves on next attempt
- If persistent: check CRM connection status

### Wrong timezone on appointments
- Timezone in Bot Configuration must match the CRM calendar timezone
- Common issue: user is in EST but calendar is set to UTC
- Fix: Bot Configuration → set correct timezone → save

---

## Phone System Registration Issues

### Text messages not going through (A2P 10DLC)

**Background**: Phone carriers require business registration before allowing text messages. Without it, messages may be silently filtered.

**Check registration status using the check_registrations tool.**

Common states:
- **No brand registered**: User needs to register. Numbers tab → A2P Registration
- **Brand PENDING**: Waiting for verification (few hours to days). Be patient.
- **Brand FAILED**: Usually business name doesn't match IRS records for the EIN. User needs to use EXACT legal name from their IRS letter (SS-4 or CP-575)
- **Brand APPROVED but no campaign**: Brand passed but campaign not created yet. Complete the campaign registration
- **Campaign PENDING**: Waiting for carrier approval. Nothing to do but wait
- **Campaign VERIFIED**: All good — messages should go through. If still blocked, check number health

### Calls showing as "Spam Likely"

**Background**: Carriers flag numbers that make many outbound calls. Registration tells carriers your calls are legitimate.

**Steps to resolve:**
1. Register for Spam Protection: Numbers tab → Spam Protection
2. Register for Text Messaging (A2P): Numbers tab → A2P Registration
3. Enable Smart Number Rotation: Number Health settings
4. These registrations take 24-48 hours to propagate

**Registration status meanings:**
- draft = not submitted yet
- pending-review = submitted, waiting (24-48 hrs typical)
- in-review = actively being reviewed
- approved = done, your numbers are protected
- rejected = denied, needs correction and resubmission

**Common rejection reasons for spam protection:**
- Business name doesn't match exactly
- Employee count or call volume was 0 or invalid (must be positive numbers)
- Address verification failed
- Missing required fields (website, description)

### Caller ID not showing

**Background**: Caller ID (CNAM) registration makes your business name display on the recipient's phone.

- Check if CNAM is set up in Numbers tab
- CNAM display name is limited to 15 characters
- Takes a few hours to propagate after setting
- Not all carriers support CNAM display

---

## Voice and Dialer Issues

### AI voice calls dropping
- Check AI Minutes balance (AI Minutes tab)
- If balance is 0, AI calls cannot be made
- Check internet/WebSocket connectivity on the Railway deployment

### No audio on calls
- May be a browser issue — try Chrome
- Check that microphone permissions are granted
- Verify the TwiML app webhook URLs are correct for the deployment

### Multi-line dialer not working
- Requires Pro Dialer subscription ($224.98/mo) or higher
- Check subscription_tier — must be "pro_dialer" or "solo_predictive"
- Max 4 concurrent lines

### Calls going to voicemail immediately
- Enable AMD (Answering Machine Detection) in voice config
- On-machine action can be: hangup, voicemail_drop, or continue
- If all calls hit voicemail: check if numbers are flagged as spam

---

## Billing Issues

### Managing subscription
Dashboard → Billing tab → "Manage Billing" opens Stripe billing portal.
From there: update payment method, view invoices, cancel.

### Switching plans
Dashboard → Billing tab → select new plan → "Change Plan"
Plan changes are prorated (credit for unused time on current plan).

### Double charged
Check Stripe billing portal for invoice dates. If actually double-charged, create a support ticket (severity: medium, category: billing).

### Subscription stuck / payment failed
Usually a card issue. Update payment method in billing portal. If subscription shows as cancelled but user claims they paid, escalate to admin.

---

## Login Issues

### Can't log in
1. Try "Forgot Password" on login page
2. Enter email used at signup (same as CRM email)
3. Check inbox AND spam folder for reset link
4. If installed from GHL Marketplace and never set password: use Forgot Password

### Agency login issues
- Agency owners log in at /agency-login (NOT /login)
- Must have entry in agency_billing table with role='agency_owner'
- If converted from individual: may need agency_billing record created

---

## Smart Filters and Lead Intelligence

### Contacts stuck on "Analyzing..."
- Contacts are being processed by AI in the background
- ~1000 contacts analyzed in ~30 seconds
- If stuck for more than 5 minutes: check if the intelligence worker is running
- Worker must be running the 'intelligence' queue

### Scores seem wrong
- AI reads FULL conversation history before scoring
- "Not interested" = cool/cold (15-30 score), NOT a dead lead
- Only explicit TCPA opt-out ("stop", "unsubscribe") = cold/5
- Scores refresh every 24 hours or when new messages arrive

---

## Workflow Issues

### Workflow not triggering
- Check workflow is in "active" status (not "draft")
- Check trigger conditions match the event
- Check if exit conditions were met (e.g., exit_on_reply)
- Verify the CRM webhook is delivering events

### Workflow sending at wrong times
- Check timezone settings in both the workflow and bot config
- Calling hours enforcement applies to workflow-triggered calls too

---

## Common Error Patterns in Logs

### "auth" failure_reason
CRM token expired. Fix: reconnect CRM from dashboard.

### "rate_limit" errors
API rate limit hit. Usually transient. If persistent: too many contacts being processed simultaneously.

### "safety" blocked
Reply sanitizer blocked the message. Usually means AI generated content that sounded too robotic. Message is not sent. Retries automatically.

### "invalid" number errors
Phone number format issue or number cannot receive SMS. Check the lead's phone number format.

### "network" errors
Transient connectivity issue. System retries automatically. If persistent: check deployment health.
