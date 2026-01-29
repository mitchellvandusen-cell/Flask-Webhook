# 🌐 Universal Webhook Field Support

## Overview

The webhook handler now accepts **ALL naming conventions** for field names. You can send fields in any format and they'll be automatically normalized to snake_case for internal processing.

## Supported Naming Variations

For **ANY** field (contact_id, location_id, user_id, calendar_id, etc.), the system accepts:

### snake_case (Standard)
```json
{
  "contact_id": "3BKttSk7A2Bo186g67ge",
  "location_id": "k7l0zdwaMruhP7NZHin2",
  "user_id": "usr_abc123",
  "calendar_id": "cal_xyz789"
}
```

### camelCase (GHL Marketplace Apps)
```json
{
  "contactId": "3BKttSk7A2Bo186g67ge",
  "locationId": "k7l0zdwaMruhP7NZHin2",
  "userId": "usr_abc123",
  "calendarId": "cal_xyz789"
}
```

### PascalCase
```json
{
  "ContactId": "3BKttSk7A2Bo186g67ge",
  "LocationId": "k7l0zdwaMruhP7NZHin2",
  "UserId": "usr_abc123",
  "CalendarId": "cal_xyz789"
}
```

### UPPERCASE
```json
{
  "CONTACT_ID": "3BKttSk7A2Bo186g67ge",
  "LOCATION_ID": "k7l0zdwaMruhP7NZHin2",
  "USER_ID": "usr_abc123",
  "CALENDAR_ID": "cal_xyz789"
}
```

### UPPERCASE No Underscore
```json
{
  "CONTACTID": "3BKttSk7A2Bo186g67ge",
  "LOCATIONID": "k7l0zdwaMruhP7NZHin2",
  "USERID": "usr_abc123",
  "CALENDARID": "cal_xyz789"
}
```

### With Spaces
```json
{
  "contact id": "3BKttSk7A2Bo186g67ge",
  "location id": "k7l0zdwaMruhP7NZHin2",
  "CONTACT ID": "3BKttSk7A2Bo186g67ge",
  "Contact Id": "3BKttSk7A2Bo186g67ge"
}
```

### Mixed/Custom Variations
```json
{
  "ContactID": "3BKttSk7A2Bo186g67ge",
  "locationID": "k7l0zdwaMruhP7NZHin2",
  "User_ID": "usr_abc123",
  "CALENDAR_Id": "cal_xyz789"
}
```

## How It Works

### Step 1: Receive Payload
```json
{
  "isMarketplaceAction": true,
  "extras": {
    "contactId": "3BKttSk7A2Bo186g67ge",
    "locationId": "k7l0zdwaMruhP7NZHin2"
  },
  "data": {
    "first_name": "John",
    "PHONE": "+1-555-1234"
  }
}
```

### Step 2: Universal Normalization
The `normalize_payload_universal()` function:
1. Searches for each field in **all possible variations**
2. Checks root level and nested structures (extras, data, meta)
3. Returns first valid value found
4. Normalizes everything to snake_case

### Step 3: Normalized Result
```json
{
  "contact_id": "3BKttSk7A2Bo186g67ge",
  "location_id": "k7l0zdwaMruhP7NZHin2",
  "first_name": "John",
  "phone": "+1-555-1234",
  "_original_payload": { ... },
  "_is_marketplace": true
}
```

### Step 4: Processing
All downstream code (tasks.py, contact_validator.py, etc.) receives consistent snake_case fields.

## Supported Field Types

### ID Fields (Automatically Detected)
- `contact_id` / `contactId` / `ContactId` / `CONTACT_ID`
- `location_id` / `locationId` / `LocationId` / `LOCATION_ID`
- `user_id` / `userId` / `UserId` / `USER_ID`
- `calendar_id` / `calendarId` / `CalendarId` / `CALENDAR_ID`
- `appointment_id` / `appointmentId` / `AppointmentId` / `APPOINTMENT_ID`
- `opportunity_id` / `opportunityId` / `OpportunityId` / `OPPORTUNITY_ID`
- `workflow_id` / `workflowId` / `WorkflowId` / `WORKFLOW_ID`
- `company_id` / `companyId` / `CompanyId` / `COMPANY_ID`
- `conversation_id` / `conversationId` / `ConversationId` / `CONVERSATION_ID`
- `message_id` / `messageId` / `MessageId` / `MESSAGE_ID`
- `task_id` / `taskId` / `TaskId` / `TASK_ID`
- `pipeline_id` / `pipelineId` / `PipelineId` / `PIPELINE_ID`

### Data Fields (Automatically Detected)
- `first_name` / `firstName` / `FirstName` / `FIRST_NAME` / `FIRSTNAME`
- `last_name` / `lastName` / `LastName` / `LAST_NAME` / `LASTNAME`
- `full_name` / `fullName` / `FullName` / `FULL_NAME` / `FULLNAME`
- `email` / `Email` / `EMAIL`
- `phone` / `Phone` / `PHONE`
- `address` / `Address` / `ADDRESS`
- `city` / `City` / `CITY`
- `state` / `State` / `STATE`
- `zip` / `Zip` / `ZIP`
- `country` / `Country` / `COUNTRY`
- `age` / `Age` / `AGE`
- `date_of_birth` / `dateOfBirth` / `DateOfBirth` / `DATE_OF_BIRTH`
- `gender` / `Gender` / `GENDER`
- `intent` / `Intent` / `INTENT`
- `message` / `Message` / `MESSAGE`
- `body` / `Body` / `BODY`
- `agent` / `Agent` / `AGENT`
- `status` / `Status` / `STATUS`
- `type` / `Type` / `TYPE`
- `direction` / `Direction` / `DIRECTION`

## Nested Structure Support

The system automatically searches these nested keys:
- `extras` (GHL marketplace apps)
- `data` (GHL marketplace apps)
- `meta` (GHL marketplace apps)
- `contact` (nested contact objects)
- `location` (nested location objects)
- `user` (nested user objects)
- `calendar` (nested calendar objects)

### Example: Deeply Nested
```json
{
  "extras": {
    "contactId": "3BKttSk7A2Bo186g67ge"
  },
  "data": {
    "personal": {
      "firstName": "John"  // Not found yet - only 1 level deep search
    },
    "firstName": "John"  // ✅ Found here
  }
}
```

## Logging & Debugging

When a webhook is received, you'll see:

```
🔍 WEBHOOK RECEIVED - FULL PAYLOAD:
📦 Payload Keys: ['extras', 'data', 'meta', 'isMarketplaceAction']
📦 Full Payload JSON: { ... }

🔄 NORMALIZING PAYLOAD - Accepting all field name variations

✅ NORMALIZED PAYLOAD: {
  "contact_id": "3BKttSk7A2Bo186g67ge",
  "location_id": "k7l0zdwaMruhP7NZHin2",
  ...
}

🔍 EXTRACTED VALUES | contact_id=3BKttSk7A2Bo186g67ge | location_id=k7l0zdwaMruhP7NZHin2 | ...

✅ WEBHOOK ACCEPTED | contact_id=3BKttSk7A2Bo186g67ge | Passing to Redis AS-IS
```

## Error Handling

If contact_id still can't be found after trying all variations:

```
🚨 WEBHOOK REJECTED | contact_id=None is clearly invalid
🚨 REJECTION REASON: Expected 'contact_id' field with valid value (5+ chars), but got: None
🚨 Searched all variations: contact_id, contactId, ContactId, CONTACT_ID, CONTACTID, etc.
🚨 Available fields in normalized payload: ['location_id', 'message', 'first_name']
🚨 Original payload keys: ['extras', 'data', 'meta', 'isMarketplaceAction']
```

This helps you see:
1. What fields were successfully extracted
2. What was in the original payload
3. Why contact_id couldn't be found

## Use Cases

### ✅ GHL Marketplace Apps
```json
{
  "isMarketplaceAction": true,
  "extras": { "contactId": "...", "locationId": "..." }
}
```
→ Automatically normalized

### ✅ Custom Workflows (Manual Configuration)
```json
{
  "contact_id": "...",
  "location_id": "..."
}
```
→ Already in correct format, passes through

### ✅ Custom Workflows (User Misconfigured)
```json
{
  "CONTACT ID": "...",
  "Location_ID": "..."
}
```
→ Automatically detected and normalized

### ✅ Third-Party Integrations
```json
{
  "ContactID": "...",
  "LocationID": "..."
}
```
→ Automatically normalized

### ✅ Zapier/Make.com Webhooks
```json
{
  "Contact Id": "...",
  "location id": "..."
}
```
→ Even spaces work!

## Benefits

1. **No Configuration Needed** - Send any format, it just works
2. **Backwards Compatible** - All existing integrations continue working
3. **Future Proof** - New GHL formats automatically supported
4. **Error Resistant** - User misconfiguration doesn't break webhooks
5. **Integration Friendly** - Third-party tools can use their own conventions

## Technical Implementation

The universal extraction uses two key functions:

### `extract_field_flexible(payload, field_name, search_nested=True)`
- Generates all possible variations of a field name
- Searches root and nested structures
- Returns first valid value found

### `normalize_payload_universal(payload)`
- Calls `extract_field_flexible()` for all known fields
- Returns consistent snake_case structure
- Preserves original payload for debugging

## Adding New Fields

To add support for a new field (e.g., `subscription_id`):

1. Add to the `id_fields` or `data_fields` list in `normalize_payload_universal()`
2. That's it! All variations automatically supported:
   - `subscription_id`
   - `subscriptionId`
   - `SubscriptionId`
   - `SUBSCRIPTION_ID`
   - `SUBSCRIPTIONID`
   - `Subscription Id`
   - etc.

## Migration Notes

### Before (Old System)
- Only accepted exact field names (`contact_id`, `location_id`)
- Marketplace apps required manual normalization
- User errors caused webhook rejections

### After (New System)
- Accepts ALL naming variations automatically
- Marketplace apps work out of the box
- User errors are auto-corrected
- Same normalized output for all downstream code

## Performance

- **Minimal overhead**: Only searches variations when field not found in root
- **Early exit**: Stops searching once valid value found
- **Cached variations**: Variations generated once per field
- **No regex**: Fast string operations only

## Testing

You can now test webhooks with any naming convention:

```bash
# snake_case
curl -X POST https://yourdomain.com/webhook \
  -H "Content-Type: application/json" \
  -d '{"contact_id": "123", "location_id": "456", "message": "test"}'

# camelCase
curl -X POST https://yourdomain.com/webhook \
  -H "Content-Type: application/json" \
  -d '{"contactId": "123", "locationId": "456", "message": "test"}'

# UPPERCASE
curl -X POST https://yourdomain.com/webhook \
  -H "Content-Type: application/json" \
  -d '{"CONTACT_ID": "123", "LOCATION_ID": "456", "MESSAGE": "test"}'

# Mixed
curl -X POST https://yourdomain.com/webhook \
  -H "Content-Type: application/json" \
  -d '{"ContactID": "123", "locationId": "456", "MESSAGE": "test"}'
```

All will be normalized and processed identically.

## Summary

✅ **Works with ANY naming convention** - snake_case, camelCase, PascalCase, UPPERCASE, spaces
✅ **Auto-detects marketplace apps** - No manual configuration needed
✅ **Searches nested structures** - Handles complex payload hierarchies
✅ **Backwards compatible** - Existing integrations unaffected
✅ **Comprehensive logging** - Easy debugging with detailed logs
✅ **Future proof** - New GHL formats automatically supported
✅ **Error resistant** - User mistakes auto-corrected

**You can now configure your GHL workflows however you want - the webhook will understand it!**
