# Error Codes Reference

## Phone System Error Codes

### Number Errors
- **21211**: Invalid phone number format — number cannot be reached
- **21214**: Cannot route to this number — carrier issue or invalid destination
- **21217**: Number is not SMS-capable — try a different number
- **21610**: Number has been blacklisted/opted out — recipient sent STOP
- **21612**: Number cannot receive SMS — landline or unsupported carrier
- **21408**: Account not allowed to call this number — permissions issue

### Authentication Errors
- **20003**: Authentication failed — account credentials are invalid or expired
- **20429**: Too many requests — rate limited, wait and retry

### Registration/Trust Hub Statuses
- **draft**: Registration created but not submitted
- **pending-review**: Submitted, waiting for review (24-48 hours typical)
- **in-review**: Currently being reviewed by carrier/compliance team
- **twilio-approved**: Approved and active
- **twilio-rejected**: Denied — needs correction and resubmission

### A2P Brand Statuses
- **PENDING**: Brand registration submitted, under review
- **APPROVED**: Brand verified successfully
- **FAILED**: Brand verification failed — usually name/EIN mismatch
- **VETTED_REJECT**: Secondary vetting rejected

### A2P Campaign Statuses
- **PENDING**: Campaign submitted to carriers
- **VERIFIED**: Campaign approved by carriers
- **FAILED**: Campaign rejected
- **IN_PROGRESS**: Being processed

## CRM Error Patterns

### GHL (GoHighLevel)
- **401 Unauthorized**: OAuth token expired — needs CRM reconnection
- **403 Forbidden**: Insufficient scopes or account access revoked
- **429 Too Many Requests**: API rate limit — 10 req/sec for OAuth apps
- **422 Unprocessable**: Invalid data format in API request

### HubSpot
- **401 Unauthorized**: Token expired (every 6 hours) — auto-refresh should handle
- **403 Forbidden**: Missing required OAuth scopes
- **429 Rate Limited**: 40 req/10sec for OAuth apps
- **400 Bad Request**: Invalid property name or value format

## Internal Error Reasons

### Webhook Processing
- **auth**: CRM token expired — needs reconnection
- **rate_limit**: Too many API calls — transient, auto-retries
- **safety**: Reply sanitizer blocked the message — AI output was flagged
- **duplicate**: Same message already sent recently — dedup protection
- **invalid**: Missing required parameters or invalid format
- **network**: Connection timeout — transient, auto-retries
- **error**: General processing error — check logs for details

### AI/LLM Errors
- Empty response: AI model returned no content — retries automatically
- Contaminated output: AI revealed bot identity — blocked by sanitizer, retried
- Token limit: Response exceeded max_tokens — truncated but still sent

## Stripe Error Codes
- **card_declined**: Customer's payment method declined
- **expired_card**: Card has expired
- **insufficient_funds**: Not enough balance
- **processing_error**: Temporary issue with payment processor
- **incorrect_cvc**: Wrong security code entered
