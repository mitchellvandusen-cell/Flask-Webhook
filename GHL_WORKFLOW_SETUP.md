# 🔧 GoHighLevel Workflow Configuration Guide

## CRITICAL: Field Names Must Match Exactly

When configuring your GHL workflow custom action to call the InsuranceGrokBot webhook, you **MUST** use these exact field names (lowercase, with underscores):

## Required Field Names

### Webhook Configuration in GHL Workflow

**Webhook URL:** `https://yourdomain.com/webhook`
**Method:** `POST`
**Content-Type:** `application/json`

### Field Name Mapping (CASE SENSITIVE!)

| GHL Workflow Field Name | GHL Custom Value | Required? |
|------------------------|------------------|-----------|
| `contact_id` | `{{contact.id}}` | ✅ YES |
| `location_id` | `{{location.id}}` | ✅ YES |
| `first_name` | `{{contact.first_name}}` | ⚠️ Recommended |
| `phone` | `{{contact.phone}}` | ⚠️ Recommended |
| `message` | `{{message.body}}` or `{{message}}` | ✅ YES (if processing message) |
| `age` | `{{contact.date_of_birth}}` | ❌ Optional |
| `address` | `{{contact.address}}` | ❌ Optional |
| `intent` | `message` or custom value | ❌ Optional |

### Example Correct Configuration

```json
{
  "contact_id": "{{contact.id}}",
  "location_id": "{{location.id}}",
  "first_name": "{{contact.first_name}}",
  "phone": "{{contact.phone}}",
  "message": "{{message.body}}",
  "age": "{{contact.date_of_birth}}",
  "intent": "life_insurance"
}
```

## Common Mistakes

### ❌ WRONG - Using Uppercase or Spaces
```json
{
  "CONTACT ID": "{{contact.id}}",           // ❌ Wrong - uppercase with space
  "CONTACT FIRST NAME": "{{contact.first_name}}",  // ❌ Wrong
  "Contact ID": "{{contact.id}}",           // ❌ Wrong - mixed case with space
}
```

### ✅ CORRECT - Lowercase with Underscores
```json
{
  "contact_id": "{{contact.id}}",           // ✅ Correct
  "first_name": "{{contact.first_name}}",   // ✅ Correct
  "phone": "{{contact.phone}}"              // ✅ Correct
}
```

## Why Your Webhook Was Rejected

Based on your screenshot, you likely configured your GHL workflow with field names like:
- `CONTACT ID` (uppercase with space)
- `CONTACT FIRST NAME` (uppercase with space)
- `CONTACT PHONE NUMBER` (uppercase with space)

GHL sends these **exact field names** in the payload, but the webhook handler is looking for:
- `contact_id` (lowercase with underscore)
- `first_name` (lowercase with underscore)
- `phone` (lowercase, no spaces)

### What GHL Sent (Your Current Config):
```json
{
  "CONTACT ID": "3BKttSk7A2Bo186g67ge",
  "CONTACT FIRST NAME": "John",
  "CONTACT PHONE NUMBER": "+1-555-1234",
  "INTENT": "message"
}
```

### What the Webhook Expected:
```json
{
  "contact_id": "3BKttSk7A2Bo186g67ge",
  "location_id": "loc_abc123",
  "first_name": "John",
  "phone": "+1-555-1234",
  "message": "I'm interested in life insurance"
}
```

## How to Fix

1. **Open your GHL workflow**
2. **Find the InsuranceGrokBot custom action step**
3. **Edit the field names** to use lowercase with underscores:
   - Change `CONTACT ID` → `contact_id`
   - Change `CONTACT FIRST NAME` → `first_name`
   - Change `CONTACT PHONE NUMBER` → `phone`
   - Change `INTENT` → `intent` (lowercase)
4. **Keep the GHL custom values** ({{contact.id}}, etc.) the same
5. **Save and test**

## Step-by-Step GHL Configuration

### 1. Create Custom Webhook Action in Workflow

1. In your GHL workflow, add a "Custom Webhook" action
2. Set the webhook URL to your endpoint
3. Choose "POST" method
4. Select "Send custom data"

### 2. Configure Body Fields (EXACT NAMES)

Click "Add Field" for each of these:

| Field Name | Field Value |
|------------|-------------|
| `contact_id` | Click "Custom Value" → Select `{{contact.id}}` |
| `location_id` | Click "Custom Value" → Select `{{location.id}}` |
| `first_name` | Click "Custom Value" → Select `{{contact.first_name}}` |
| `phone` | Click "Custom Value" → Select `{{contact.phone}}` |
| `message` | Click "Custom Value" → Select `{{message.body}}` |

**IMPORTANT:** The field name on the LEFT must be typed EXACTLY as shown (lowercase, underscores). The GHL custom value on the RIGHT should be selected from the dropdown.

## Verification Steps

After updating your workflow configuration:

1. **Check your logs** (the new logging will show):
   ```
   🔍 WEBHOOK RECEIVED - FULL PAYLOAD:
   📦 Payload Keys: ['contact_id', 'location_id', 'first_name', 'phone', 'message']
   📦 Full Payload JSON: { "contact_id": "3BKttSk7A2Bo186g67ge", ... }
   🔍 EXTRACTED VALUES | contact_id=3BKttSk7A2Bo186g67ge | location_id=loc_abc | ...
   ✅ WEBHOOK ACCEPTED | contact_id=3BKttSk7A2Bo186g67ge
   ```

2. **If still rejected**, check the logs for:
   ```
   🚨 REJECTION REASON: Expected 'contact_id' field with valid value (5+ chars), but got: None
   🚨 Available fields in payload: ['CONTACT ID', 'CONTACT FIRST NAME', ...]
   ```

   This tells you the exact field names GHL is sending vs what's expected.

## Alternative: Nested Message Object

If you're sending message data, you can also structure it as a nested object:

```json
{
  "contact_id": "{{contact.id}}",
  "location_id": "{{location.id}}",
  "message": {
    "body": "{{message.body}}",
    "type": "{{message.type}}",
    "direction": "inbound"
  }
}
```

The webhook handler will extract `message.body` automatically.

## Testing Your Configuration

Send a test message through your GHL workflow and check your application logs. You should see:

```
✅ WEBHOOK ACCEPTED | contact_id=3BKttSk7A2Bo186g67ge | Passing to Redis AS-IS
```

If you see:

```
🚨 WEBHOOK REJECTED | contact_id=None is clearly invalid
```

Then your field names are still incorrect. Check the `Available fields in payload` log line to see what names GHL is sending.

## Summary

- ✅ Use **lowercase field names with underscores** (`contact_id`, `first_name`)
- ❌ Do NOT use uppercase or spaces (`CONTACT ID`, `Contact ID`)
- ✅ Keep GHL custom values as dropdown selections (`{{contact.id}}`)
- ✅ Check logs to verify exact payload structure
- ✅ Required fields: `contact_id`, `location_id`, `message` (or `message.body`)
