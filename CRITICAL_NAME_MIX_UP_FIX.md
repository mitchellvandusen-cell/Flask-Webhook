# 🚨 CRITICAL BUG: Wrong Names Being Used

## The Problem

**Bot is using "Dennis" for multiple different contacts:**
- Phillip Rochester → Bot says "Dennis, you were looking into life insurance..."
- Victor Rosales → Bot says "Dennis, you were looking into life insurance..."
- John Tyler → Bot says "Dennis, you were looking into life insurance..."

**Impact:** LOST LEADS. Contacts immediately know it's a bot mistake and stop responding.

---

## What I've Done (Step 1: Debugging)

I've added **critical debug logging** to track down the issue. The logs will now show:

### In tasks.py (Webhook Handler):
```
🔍 CONTACT DEBUG | contact_id=ABC123 | first_name_from_payload=Phillip | location_id=LOC456
```

### In sales_director.py (Profile Builder):
```
🔍 SALES DIRECTOR | contact_id=ABC123 | first_name=Phillip
🔍 NARRATIVE CHECK | contact_id=ABC123 | narrative_preview=Phillip is interested in term life...
🔍 PROFILE BUILT | contact_id=ABC123 | profile_preview=Basics: Phillip is 35 years old...
```

### In memory.py (Database Queries):
```
🔍 QUERY NARRATIVE | contact_id=ABC123
🔍 NARRATIVE RETRIEVED | contact_id=ABC123 | has_narrative=True | preview=Phillip is interested...
```

---

## How to Diagnose the Issue

### Step 1: Check Railway Logs

1. Go to Railway dashboard
2. Select your Flask-Webhook service
3. Click "Deployments" → "Logs"
4. Wait for the next webhook to fire
5. Search for `🔍 CONTACT DEBUG`
6. Check if:
   - `contact_id` is unique for each person
   - `first_name` matches the actual contact name
   - `narrative_preview` contains the correct name

### Step 2: Identify the Root Cause

**Scenario A: GHL Sending Wrong Data**
```
🔍 CONTACT DEBUG | contact_id=ABC123 | first_name_from_payload=Dennis
```
- If `first_name` is "Dennis" for everyone, **GHL is sending bad data**
- Fix: Check GHL custom fields mapping

**Scenario B: Database Corruption**
```
🔍 CONTACT DEBUG | contact_id=ABC123 | first_name_from_payload=Phillip
🔍 NARRATIVE RETRIEVED | contact_id=ABC123 | preview=Dennis is interested in...
```
- If `first_name` is correct but `narrative` has "Dennis", **database has wrong data**
- Fix: Clear corrupted narratives

**Scenario C: Contact ID Collision**
```
🔍 CONTACT DEBUG | contact_id=SAME_ID | first_name_from_payload=Phillip
🔍 CONTACT DEBUG | contact_id=SAME_ID | first_name_from_payload=Victor
```
- If multiple contacts have the **same contact_id**, **GHL webhook is broken**
- Fix: Check GHL webhook configuration

---

## Immediate Fix Options

### Fix 1: Clear All Narratives (Nuclear Option)

If the database is corrupted with Dennis's data everywhere:

```sql
-- Connect to Neon database
-- Run this query to clear ALL narratives
DELETE FROM contact_narratives;
DELETE FROM contact_facts;
DELETE FROM contact_messages WHERE message_type = 'system';

-- Narratives will rebuild automatically on next conversation
```

**Pros:** Guaranteed to fix the issue
**Cons:** Loses all conversation history (but better than wrong names!)

### Fix 2: Clear Only Dennis's Contaminated Data

If Dennis's data is leaking into other contacts:

```sql
-- Find which contact_id belongs to Dennis
SELECT contact_id, story_narrative
FROM contact_narratives
WHERE story_narrative ILIKE '%dennis%';

-- If you find a specific contact_id that shouldn't have Dennis:
DELETE FROM contact_narratives WHERE contact_id = 'WRONG_ID';
DELETE FROM contact_facts WHERE contact_id = 'WRONG_ID';
```

### Fix 3: Add Validation to Prevent This

I'll add a validation check that ensures the narrative doesn't contradict the webhook data:

```python
# In sales_director.py after retrieving narrative
if first_name and story_narrative:
    # Check if narrative mentions a DIFFERENT name
    narrative_lower = story_narrative.lower()
    first_lower = first_name.lower()

    # If narrative has a name that's not the current name, clear it
    if " is " in narrative_lower and first_lower not in narrative_lower:
        logger.warning(f"NARRATIVE MISMATCH: contact_id has narrative about different person. Clearing.")
        update_narrative(contact_id, f"New lead: {first_name}. Building profile.")
```

---

## Prevention Strategy

### 1. Add Contact ID Validation

Ensure contact_ids are unique and valid:

```python
# In tasks.py after extracting contact_id
if not contact_id or contact_id == "unknown" or len(contact_id) < 5:
    logger.error(f"INVALID CONTACT ID: {contact_id} | Aborting")
    return {"status": "error", "reason": "invalid contact_id"}
```

### 2. Add Name Consistency Check

Before saving a narrative, verify it mentions the correct name:

```python
# In memory.py update_narrative function
def update_narrative(contact_id: str, new_story: str, expected_name: str = None) -> bool:
    if expected_name and expected_name.lower() not in new_story.lower():
        logger.warning(f"Narrative doesn't mention {expected_name}, which may indicate data corruption")
    # Continue with save...
```

### 3. Add Database Constraints

Prevent NULL or duplicate contact_ids:

```sql
-- Add constraint to ensure contact_id is never null
ALTER TABLE contact_narratives
ADD CONSTRAINT contact_narratives_contact_id_not_null
CHECK (contact_id IS NOT NULL AND contact_id != '');

-- Add unique index to prevent duplicates
CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_contact_narrative
ON contact_narratives(contact_id);
```

---

## What to Do RIGHT NOW

1. **Check Railway logs** - Look for the debug messages with the 🔍 emoji
2. **Identify the pattern** - Are contact_ids unique? Is first_name correct?
3. **Choose a fix**:
   - If DB is corrupted → Run Fix 1 (clear narratives)
   - If GHL is sending bad data → Check GHL custom fields
   - If contact_ids are colliding → Check GHL webhook config

4. **Test immediately** - Send a test message to verify the fix worked

5. **Monitor** - Watch logs for the next few conversations to ensure it doesn't happen again

---

## Long-Term Solution

After we identify the root cause, I'll implement:

1. ✅ Validation layer that rejects invalid contact_ids
2. ✅ Name consistency checks before using narratives
3. ✅ Automatic corruption detection and self-healing
4. ✅ Database constraints to prevent invalid data
5. ✅ Alerting system that notifies you if this happens again

---

## Need Help?

**Look at the Railway logs and send me:**
1. A screenshot of the `🔍 CONTACT DEBUG` log lines
2. A screenshot of the `🔍 NARRATIVE RETRIEVED` log lines
3. The contact_id of one person who got the wrong name

I'll tell you exactly what's wrong and how to fix it.

---

## Current Status

🟡 **Debugging mode active** - Logs will show exactly what's happening
🔴 **Issue not yet fixed** - Waiting for diagnostic data
⏳ **Next step** - Check logs when next message arrives

**The logging is now live. Next time a message comes in, we'll see exactly what's causing this.**
