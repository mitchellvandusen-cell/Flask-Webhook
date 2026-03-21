# HubSpot CRM Integration Reference

## OAuth Connection
- Users connect via Integrations page on dashboard
- Redirects to HubSpot authorization page
- Tokens stored in subscribers.crm_config JSONB
- Tokens expire every 6 HOURS (much faster than GHL — auto-refreshed every 15 min)
- Client secret is dual-purpose: OAuth token exchange + webhook signature verification

## Required OAuth Scopes
- crm.objects.contacts.read / write
- crm.objects.deals.read / write
- crm.objects.communications.read / write
- crm.objects.meetings.write (optional)
- timeline (optional)

## Key API Endpoints

### Contacts
- GET /crm/v3/objects/contacts — List contacts
- POST /crm/v3/objects/contacts/search — Search with filters
- POST /crm/v3/objects/contacts — Create contact
- PATCH /crm/v3/objects/contacts/{id} — Update contact

### Deals
- GET /crm/v3/objects/deals — List deals
- POST /crm/v3/objects/deals/search — Search deals

### Communications (SMS logging)
- POST /crm/v3/objects/communications — Log outbound SMS
- Channel type: SMS
- Association type ID: 81 (contact → communication)

### Calls (Call logging)
- POST /crm/v3/objects/calls — Log call activity
- Association type ID: 194 (contact → call)

## Webhook Events
- contact.creation — New contact created in HubSpot
- contact.propertyChange — Contact field changed
- deal.creation — New deal created
- deal.propertyChange — Deal stage changed

Webhook payload: JSON array of batched events (must iterate)
Signature: HMAC-SHA256 v3 (X-HubSpot-Signature-v3 header)

## CRM Card
- Displays AI intelligence in HubSpot sidebar
- Data fetch URL: GET /hubspot/crm-card
- Returns temperature, score, summary, recommended actions
- Zero AI cost — reads from cache only
- Action buttons: "Full AI Intelligence" (iframe), "Open Dialer" (iframe)

## Rate Limits
- OAuth apps: 40 requests per 10 seconds
- Private apps: 100 requests per 10 seconds
- Sync engine includes exponential backoff and 429 handling

## Common Issues

### Token expired
- HubSpot tokens expire every 6 hours (very fast)
- Auto-refresh runs every 15 minutes
- If fails: check HubSpot app permissions in developer portal
- Fix: Disconnect and reconnect from Integrations page

### Webhook delivery failing
- Check HubSpot developer portal → Webhooks tab
- Verify target URL points to correct deployment domain
- Verify HMAC signature verification is using correct client secret
- Events are batched — handler must iterate the array

### CRM Card showing "No intelligence"
- First load always shows empty (cache not populated yet)
- Bot needs to process conversations first
- Check if contact_intelligence table has data for the contact

### Contact search returning nothing
- Phone normalization: strips to digits, removes country code
- Email search is case-insensitive
- Name search is fuzzy via CRM v3 Search API
