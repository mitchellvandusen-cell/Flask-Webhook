# HighLevel API v2 Location Path Requirements

## 🎯 The Core Issue

**Your calendar endpoints are using the WRONG path structure for HighLevel API v2.**

The HighLevel API has two major versions with different URL patterns:

| Version | Path Pattern | Example |
|---------|-------------|---------|
| **v1 (Legacy)** | `/calendars/?locationId={id}` | Query parameter |
| **v2 (Current)** | `/locations/{locationId}/calendars` | Path parameter |

## 🔍 Current State: What Your Code Is Doing (WRONG)

### File: `ghl_calendar.py` (Lines 14-20)

```python
# THESE ENDPOINTS ARE INCORRECT FOR v2!
GHL_CALENDARS_LIST = "https://services.leadconnectorhq.com/calendars/"
GHL_FREE_SLOTS_URL = "https://services.leadconnectorhq.com/calendars/{cal_id}/free-slots"
GHL_CREATE_APPOINTMENT_URL = "https://services.leadconnectorhq.com/calendars/events/appointments"
```

### What's Wrong?

1. **Line 182**: You list calendars with:
   ```python
   cal_list_url = f"https://services.leadconnectorhq.com/calendars/?locationId={location_id}"
   ```
   ❌ This passes `locationId` as a **query parameter** (v1 style)

2. **Line 371**: You fetch free slots with:
   ```python
   url = "https://services.leadconnectorhq.com/calendars/{cal_id}/free-slots"
   ```
   ❌ This is missing `/locations/{locationId}/` prefix

3. **Line 521**: You book appointments with:
   ```python
   booking_url = "https://services.leadconnectorhq.com/calendars/events/appointments"
   ```
   ❌ This is missing `/locations/{locationId}/` prefix

## ✅ Correct v2 Paths for Sub-Accounts

According to the official HighLevel API v2 documentation, calendars in a **sub-account (location)** require:

### **1. List Calendars in a Sub-Account**
```
GET /locations/{locationId}/calendars
```

**Full URL:**
```
https://services.leadconnectorhq.com/locations/{locationId}/calendars
```

---

### **2. Get Free Slots for a Calendar**
```
GET /locations/{locationId}/calendars/{calendarId}/free-slots
```

**Full URL:**
```
https://services.leadconnectorhq.com/locations/{locationId}/calendars/{calendarId}/free-slots
```

**Query Parameters:**
- `startDate` (timestamp in milliseconds)
- `endDate` (timestamp in milliseconds)
- `timezone` (e.g., "America/Chicago")
- `userId` (optional - filter by assigned user)

---

### **3. Create Appointment**
```
POST /locations/{locationId}/calendars/events/appointments
```

**Full URL:**
```
https://services.leadconnectorhq.com/locations/{locationId}/calendars/events/appointments
```

**Body:**
```json
{
  "calendarId": "calendar_id_here",
  "locationId": "location_id_here",
  "contactId": "contact_id_here",
  "startTime": "2024-01-29T14:00:00-06:00",
  "endTime": "2024-01-29T14:30:00-06:00",
  "title": "Life Insurance Review",
  "appointmentStatus": "confirmed",
  "assignedUserId": "user_id_here",
  "selectedTimezone": "America/Chicago"
}
```

---

## 🔑 Why This Is Confusing

### Token Type Does NOT Affect Path Structure

Many developers think:
- "OAuth tokens use v2 paths"
- "PIT tokens use v1 paths"

**❌ THIS IS FALSE!**

The token type (OAuth vs PIT) determines **authorization scope**, not the URL path structure.

### The Real Rule

| Resource Scope | Correct Path |
|---------------|-------------|
| **Sub-account (Location) resource** | `/locations/{locationId}/...` |
| **Agency resource** | `/agencies/...` |
| **User resource** | `/users/...` or `/oauth/userinfo` |

Since **calendars belong to locations (sub-accounts)**, they MUST use:
```
/locations/{locationId}/calendars
```

This is true whether you're using:
- ✅ OAuth access token
- ✅ Personal Integration Token (PIT)
- ✅ Private app token
- ✅ Marketplace app token

---

## 🧪 Evidence from Your Codebase

### Line 2200 in `main.py` - CORRECT Usage
```python
sub_accounts = fetch_all_ghl_items(
    "https://services.leadconnectorhq.com/locations/",
    headers,
    item_key='locations'
)
```
✅ This correctly uses `/locations/` to fetch all locations.

### Why That Works But Calendars Don't

The `/locations/` endpoint (without a specific locationId) is an **agency-level** endpoint that lists all locations under the authenticated agency.

But once you're working with a **specific sub-account's resources** (like calendars), you must include the locationId in the path:
```
/locations/{locationId}/calendars
```

---

## 🚨 Why You're Getting 404 Errors

Looking at your recent commits:
- "🔧 FIX: Handle v1 free-slots 404 gracefully"
- "🔧 FIX: Correct GHL API URLs for Personal Integration Tokens (PITs)"

These 404 errors are happening because:

1. **You're using v1 paths** (`/calendars/{calendarId}/free-slots`)
2. **HighLevel API v2 expects** (`/locations/{locationId}/calendars/{calendarId}/free-slots`)
3. **The v1 endpoint may have been deprecated** or doesn't work with newer tokens

### From `ghl_calendar.py` Line 392-395:
```python
if resp.status_code == 404:
    # Calendar not found or no slots available
    logger.warning(f"⚠️ Free-slots endpoint returned 404")
    logger.warning(f"   The calendar '{cal_id}' might not exist or has no availability")
```

**Root Cause:** The 404 isn't because the calendar doesn't exist. It's because you're using the wrong endpoint path!

---

## 🔧 Required Fixes

### Fix #1: Update Endpoint Constants
**File:** `ghl_calendar.py` (Lines 14-20)

**Current (WRONG):**
```python
GHL_CALENDARS_LIST = "https://services.leadconnectorhq.com/calendars/"
GHL_FREE_SLOTS_URL = "https://services.leadconnectorhq.com/calendars/{cal_id}/free-slots"
GHL_CREATE_APPOINTMENT_URL = "https://services.leadconnectorhq.com/calendars/events/appointments"
```

**Should Be:**
```python
# v2 endpoints require /locations/{locationId}/ prefix
GHL_CALENDARS_LIST = "https://services.leadconnectorhq.com/locations/{location_id}/calendars"
GHL_FREE_SLOTS_URL = "https://services.leadconnectorhq.com/locations/{location_id}/calendars/{cal_id}/free-slots"
GHL_CREATE_APPOINTMENT_URL = "https://services.leadconnectorhq.com/locations/{location_id}/calendars/events/appointments"
```

---

### Fix #2: Update Calendar List Call
**File:** `ghl_calendar.py` (Line 182)

**Current (WRONG):**
```python
cal_list_url = f"https://services.leadconnectorhq.com/calendars/?locationId={location_id}"
```

**Should Be:**
```python
cal_list_url = f"https://services.leadconnectorhq.com/locations/{location_id}/calendars"
```

---

### Fix #3: Update Free Slots Call
**File:** `ghl_calendar.py` (Line 371)

**Current (WRONG):**
```python
url = GHL_FREE_SLOTS_URL.format(cal_id=cal_id)
# Becomes: /calendars/{cal_id}/free-slots
```

**Should Be:**
```python
url = GHL_FREE_SLOTS_URL.format(location_id=location_id, cal_id=cal_id)
# Becomes: /locations/{location_id}/calendars/{cal_id}/free-slots
```

---

### Fix #4: Update Booking Call
**File:** `ghl_calendar.py` (Line 521)

**Current (WRONG):**
```python
booking_url = GHL_CREATE_APPOINTMENT_URL
# Becomes: /calendars/events/appointments
```

**Should Be:**
```python
booking_url = GHL_CREATE_APPOINTMENT_URL.format(location_id=location_id)
# Becomes: /locations/{location_id}/calendars/events/appointments
```

---

## 📋 Implementation Checklist

- [ ] Update `GHL_CALENDARS_LIST` constant with `/locations/{location_id}/` prefix
- [ ] Update `GHL_FREE_SLOTS_URL` constant with `/locations/{location_id}/` prefix
- [ ] Update `GHL_CREATE_APPOINTMENT_URL` constant with `/locations/{location_id}/` prefix
- [ ] Update all `.format()` calls to include `location_id=location_id`
- [ ] Verify `location_id` is available in all functions that make calendar API calls
- [ ] Test with both OAuth and PIT tokens to ensure both work
- [ ] Remove 404 workaround logic (lines 392-397) since 404s should no longer occur

---

## 💡 Final Notes

### OAuth vs PIT - What ACTUALLY Differs

| Aspect | OAuth Token | PIT Token |
|--------|------------|-----------|
| **URL Path Structure** | ✅ Same (`/locations/{locationId}/...`) | ✅ Same (`/locations/{locationId}/...`) |
| **Authorization Scope** | Scoped to specific locations | Can be location-scoped or agency-wide |
| **Token Format** | JWT-like string | Starts with `pit-` |
| **Expiration** | Yes (needs refresh) | No (persistent) |
| **Detection** | Call `/oauth/userinfo` or `/v2/me` | Starts with `pit-` prefix |

### Your Code Already Detects Token Type Correctly

In `ghl_calendar.py` lines 23-102, you have a `detect_token_type()` function that correctly identifies OAuth vs PIT.

**The problem is NOT token detection.**

**The problem is using v1 URL paths when v2 requires `/locations/{locationId}/` prefix.**

---

## 🎯 Bottom Line

**For ANY calendar operation in a sub-account (location):**
- ✅ Use `/locations/{locationId}/calendars`
- ✅ Works with OAuth tokens
- ✅ Works with PIT tokens
- ✅ Required by HighLevel API v2

**Your current code uses:**
- ❌ `/calendars/?locationId={id}` (v1 style)
- ❌ Missing `/locations/{locationId}/` prefix
- ❌ Causing 404 errors

**Fix:** Add `/locations/{locationId}/` prefix to all calendar endpoint constants.
