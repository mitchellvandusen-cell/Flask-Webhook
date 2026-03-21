# GoHighLevel (GHL) / Lead Connector Integration Reference

## OAuth Connection
- Users connect via "Connect Lead Connector" button on dashboard
- Redirects to GHL authorization page with 18 required scopes
- Tokens stored in subscribers table (access_token, refresh_token)
- Tokens expire every 24 hours — auto-refreshed by cron every 15 minutes
- If refresh fails, user sees "Token Expired" and must reconnect manually

## Required OAuth Scopes (18)
contacts.readonly, contacts.write, conversations.readonly, conversations.write,
conversations/message.readonly, conversations/message.write, calendars.readonly,
calendars.write, calendars/events.readonly, calendars/events.write,
opportunities.readonly, opportunities.write, users.readonly, locations.readonly,
custom-fields.readonly, custom-fields.write, custom-values.readonly, custom-values.write

## Key API Endpoints Used

### Contacts
- GET /contacts/{id} — Fetch contact details
- GET /contacts/search — Search contacts by phone/email/name
- POST /contacts — Create contact
- PUT /contacts/{id} — Update contact

### Conversations
- GET /conversations/search — Search conversations
- POST /conversations/messages — Send message
- POST /conversations/messages/outbound — Log outbound message (Conversation Provider)
- POST /conversations/messages/inbound — Log inbound message (Conversation Provider)

### Calendars
- GET /calendars — List calendars
- GET /calendars/{id}/free-slots — Check available time slots
- POST /calendars/events/appointments — Book appointment
- Parameters: calendarId, startTime, selectedTimezone, contactId

### Opportunities
- GET /pipelines — List pipelines
- GET /pipelines/{id}/stages — List stages
- GET /opportunities/search — Search deals

## Conversation Providers
InsuranceGrokBot is registered as a GHL Conversation Provider:
- SMS Provider ID: 699c84aef36d66cc10a56e82
- Call Provider ID: 699c83535fc465bbff87a78d
- This allows IGB messages/calls to appear in GHL's conversation UI

## Common Issues

### Token expired repeatedly
- Auto-refresh runs every 15 minutes
- If fails: app scopes may have changed, or user revoked access
- Fix: Dashboard → Connect Lead Connector → re-approve

### Webhook not firing
- GHL webhooks point to /webhook endpoint
- Verified with MARKETPLACE_WEBHOOK_SECRET
- If stopped: check GHL marketplace app settings
- Common cause: GHL subscription expired on user's end

### Calendar showing wrong times
- Timezone mismatch between IGB and GHL calendar
- GHL calendars use the location's timezone
- Ensure bot timezone matches the GHL location timezone

### Contact not found
- Contact may have been deleted in GHL
- Phone number format must match (E.164)
- Try searching by phone number, then email

## GHL Data Sync
- Incremental sync pulls conversations, opportunities, phone numbers
- Triggered by cron: POST /api/cron/sync-ghl-data
- Uses cursor-based pagination with backoff
- Handles 429 rate limits and 401 auto-refresh
- Data stored in: ghl_conversations, ghl_opportunities, ghl_sync_state tables
