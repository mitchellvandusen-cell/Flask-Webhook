# Text Messaging Registration (A2P 10DLC) Reference

## What is A2P 10DLC?
Application-to-Person (A2P) 10-Digit Long Code (10DLC) is the carrier-mandated system for business SMS. Without registration, text messages from standard phone numbers may be filtered or blocked by carriers (AT&T, T-Mobile, Verizon).

## Registration Flow

### Step 1: Brand Registration
Verifies your business identity with The Campaign Registry (TCR).

**Required information:**
- Legal business name (MUST match IRS records exactly)
- EIN (Employer Identification Number)
- Business address
- Business website (must be live and accessible)
- Business type (LLC, Corporation, Sole Proprietorship, etc.)
- Contact information (name, email, phone)

**Vetting outcomes:**
- APPROVED: Business verified, proceed to campaign
- FAILED: Usually a name/EIN mismatch. Check IRS letter (SS-4 or CP-575)
- PENDING: Under review, wait

**Common brand failures:**
- "Tax ID does not match Legal Name" — use EXACT name from IRS
- "Unable to verify business" — check address and website
- "EIN not found" — verify EIN is correct, not SSN

### Step 2: Campaign Registration
Describes how you'll use SMS.

**Required information:**
- Use case description: "Insurance appointment booking and lead follow-up"
- Sample messages (2-5 examples of texts you'll send)
- Opt-in description: how leads give consent (e.g., "Leads submit form on website")
- Opt-out keywords: STOP, UNSUBSCRIBE (handled automatically)
- Help keywords: HELP (handled automatically)
- Message volume estimate

**Campaign statuses:**
- PENDING: Submitted to carriers
- IN_PROGRESS: Being processed
- VERIFIED: Approved — text messages will be delivered
- FAILED: Rejected — review requirements and resubmit

### Step 3: Messaging Service + Number Assignment
- A messaging service is created automatically during campaign setup
- Phone numbers are linked to the messaging service
- Only linked numbers can send A2P-compliant messages

## Import Flow (External Migration)
For customers who already have A2P registration through another provider (e.g., GHL/LeadConnector):

1. Get Brand SID and Campaign SID from the external provider
2. Use the import endpoint to register these IDs with the platform
3. Create a messaging service and link numbers
4. No additional carrier approval needed (already approved)

## Pricing
- Registration fee: $19 one-time (sub-account users)
- Agency owners and admins: fee waived
- No monthly recurring cost for registration itself

## Rate Limits After Registration
- Registered numbers get higher throughput limits
- Unregistered: may be throttled to 1 msg/sec or blocked entirely
- Registered: typical 75-100 msg/sec depending on carrier

## Troubleshooting

### Brand failed — what to do
1. Verify exact legal name from IRS documents
2. Confirm EIN is correct (not SSN)
3. Ensure business address is valid
4. Make website accessible (not under construction)
5. Resubmit with corrected information

### Campaign rejected — what to do
1. Review sample messages for compliance
2. Ensure opt-in description is clear
3. Add STOP/HELP handling info
4. Describe the use case clearly (not generic "marketing")
5. Resubmit

### Messages still being filtered after approval
1. Check number health — numbers may be individually flagged
2. Verify numbers are assigned to the messaging service
3. Enable smart number rotation
4. Wait 24-48 hours for carrier databases to update
5. Avoid sending too many messages too quickly from a single number
