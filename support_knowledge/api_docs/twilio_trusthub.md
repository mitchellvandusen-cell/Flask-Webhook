# Phone System Registration Guide (Trust Hub / Carrier Registration)

This document covers the carrier registration system that protects phone numbers from spam labeling and ensures text messages are delivered. All references use plain English — never mention internal vendor names to customers.

## Overview

Three types of registration protect your phone system:

1. **Spam Protection** (Voice Integrity) — Registers your numbers with carrier spam engines (AT&T, T-Mobile, Verizon) to prevent "Spam Likely" labels
2. **Caller ID** (CNAM) — Makes your business name show on the recipient's phone when you call
3. **Text Messaging Registration** (A2P 10DLC) — Required by carriers for business SMS delivery

## How Registration Works

### Business Profile (Secondary Customer Profile)
- Every account needs a verified business profile before any registration
- Profile contains: legal business name, address, EIN, contact info
- Created automatically during onboarding
- Must be approved before other registrations can proceed
- Policy: links back to the platform's primary business profile

### Spam Protection Registration (Voice Integrity)

**What it does**: Tells carriers your calls are legitimate business calls, not spam.

**Process**:
1. Business profile must be approved first
2. System creates an EndUser record with business details
3. Creates a Voice Integrity registration linked to the profile
4. Assigns phone numbers to the registration
5. Submits for review (status → pending-review)
6. Carriers review and approve (24-48 hours typically)

**Required EndUser fields** (all must be strings of positive integers):
- business_employee_count: e.g. "10" (never "0", never ranges)
- average_business_day_call_volume: e.g. "500" (never "0")
- business use case description

**Common rejection reasons**:
- Employee count or call volume was "0" or empty
- Business name doesn't match records exactly
- Missing website or invalid URL
- Address verification failed

**Fix for rejections**: Update the EndUser data with correct information, then resubmit the registration. The support bot can do this with customer consent.

### Caller ID Registration (CNAM)

**What it does**: Makes your business name (e.g., "Acme Insurance") appear on the recipient's phone screen instead of just the phone number.

**Limitations**:
- Display name limited to 15 characters
- Takes hours to propagate after setting
- Not all carriers display CNAM
- Some carriers have their own databases that override

**Process**:
1. Create a CNAM registration linked to the business profile
2. Assign phone numbers
3. Submit for review
4. Carriers update their databases

### Text Messaging Registration (A2P 10DLC)

**What it does**: Registers your business and messaging campaign with carriers so your texts aren't filtered as spam.

**Process**:
1. **Brand Registration**: Submit business identity (name, EIN, address) for vetting
2. **Campaign Registration**: Describe your messaging use case, sample messages, opt-in/opt-out flow
3. **Messaging Service**: Created automatically to link numbers to campaigns
4. **Number Assignment**: Phone numbers linked to messaging service

**Brand vetting common issues**:
- Legal name must EXACTLY match IRS records (check SS-4 or CP-575 form)
- EIN must be valid and match the legal name
- Address must be a real business address
- Website must be accessible and match the business

**Registration fee**: $19 one-time for sub-account users

## Status Meanings (All Registration Types)

| Status | Meaning | Action Required |
|--------|---------|-----------------|
| draft | Saved but not submitted | Submit for review |
| pending-review | Submitted, waiting | Be patient (24-48 hrs) |
| in-review | Actively being reviewed | Wait |
| twilio-approved | Approved and active | None — you're protected |
| twilio-rejected | Denied | Fix issues and resubmit |

## Troubleshooting Decision Tree

### "My texts aren't going through"
1. Is A2P registered? → check_registrations tool
2. Brand status APPROVED? → If FAILED, fix business name/EIN
3. Campaign status VERIFIED? → If not, complete campaign setup
4. Numbers assigned to messaging service? → If not, assign them
5. Number health OK? → Check number_health table

### "Calls show as Spam Likely"
1. Is Voice Integrity registered? → check_registrations tool
2. Status approved? → If rejected, fix and resubmit
3. Numbers assigned? → Ensure all active numbers are in the registration
4. Smart rotation enabled? → Reduces per-number call volume
5. Registration takes 24-48 hrs to propagate

### "Caller ID shows wrong name / phone number only"
1. Is CNAM set up? → check_registrations tool
2. Display name set? → Must be ≤15 chars
3. Wait time: several hours to propagate
4. Some carriers may not display CNAM
