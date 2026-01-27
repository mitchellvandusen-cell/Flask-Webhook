# UNUSED FILES AUDIT
**Date**: 2026-01-27
**Purpose**: Identify and safely remove unused code files

---

## FILES SAFE TO DELETE (Never Imported)

### 1. **unified_brain.py** (496 lines)
- **Status**: ❌ NEVER IMPORTED
- **Content**: Legacy prompt system with extensive insurance knowledge
- **Why Not Used**: System now uses simplified prompts in `prompt.py`
- **References**: Only mentioned in `attached_assets/` (old notes, not active code)
- **Safe to Delete**: ✅ YES

**Analysis**:
- Contains "UNIFIED_KNOWLEDGE" with detailed insurance product knowledge
- Contains "DECISION_PROMPT" with thinking framework
- This was the OLD prompt system before simplification
- Current system uses `prompt.py` with CORE_UNIFIED_MINDSET (much simpler)
- No active code imports this file

---

### 2. **outcome_learning.py** (500+ lines estimated)
- **Status**: ❌ NEVER IMPORTED
- **Content**: Machine learning system for pattern optimization
- **Why Not Used**: Not integrated into current workflow
- **References**: Mentioned as "optional" in `requirements.txt`
- **Safe to Delete**: ✅ YES

**Analysis**:
- Implements self-improving pattern system
- Learns from conversation outcomes
- Has database tables for pattern storage
- Complete feature but not connected to main flow
- Database tables may exist but aren't actively used
- No current code imports or calls this module

**Note**: If you want to keep this for future ML features, it's a complete module. But currently inactive.

---

### 3. **send_email_api.py** (101 lines)
- **Status**: ❌ NEVER IMPORTED
- **Content**: Standalone Mailgun API test utility
- **Why Not Used**: Test script, not part of production code
- **Usage**: `python send_email_api.py email@example.com`
- **Safe to Delete**: ⚠️ MAYBE (Useful for testing, but not required)

**Analysis**:
- Standalone script for testing Mailgun API configuration
- Not imported by any production code
- Can be useful for debugging email issues
- Small file (101 lines)

**Recommendation**: Keep if you ever need to test email, delete if you never use it

---

### 4. **test_email.py** (99 lines)
- **Status**: ❌ NEVER IMPORTED
- **Content**: Standalone Flask-Mail SMTP test utility
- **Why Not Used**: Test script, not part of production code
- **Usage**: `python test_email.py email@example.com`
- **Safe to Delete**: ⚠️ MAYBE (Useful for testing, but not required)

**Analysis**:
- Standalone script for testing SMTP configuration
- Not imported by any production code
- Similar to send_email_api.py but uses Flask-Mail instead of API
- Small file (99 lines)

**Recommendation**: Keep if you ever need to test email, delete if you never use it

---

## FILES TO KEEP (Active Use)

### ✅ **worker.py**
- **Status**: ✅ USED (Run as standalone script)
- **Purpose**: RQ worker process for background job processing
- **Usage**: `python worker.py production` or `python worker.py demo`
- **Why Keep**: Required for production - processes webhook tasks

---

## DETAILED DEPENDENCY MAP

### Core Production Files (KEEP ALL):
```
main.py (Flask app)
├── imports: db, tasks, sync_subscribers, utils, prompt, memory, individual_profile, sales_director
├── dynamic imports: sales_director, prompt (in routes)
└── RQ enqueues → tasks.process_webhook_task

tasks.py (Webhook processor)
├── imports: db, memory, sales_director, age, prompt, ghl_message, ghl_calendar, ghl_api
└── Called by: main.py via RQ queue

sales_director.py (Strategic directive generator)
├── imports: conversation_engine, individual_profile, underwriting, insurance_companies, memory
└── Called by: tasks.py, main.py (demo)

memory.py (Memory management)
├── imports: db
└── Called by: sales_director.py, tasks.py, main.py

db.py (Database operations)
├── imports: none (base utility)
└── Called by: ghl_api, ghl_message, main, memory, tasks

ghl_api.py (Token management)
├── imports: db
└── Called by: tasks.py

ghl_calendar.py (Calendar operations)
├── imports: none (uses tokens passed in)
└── Called by: tasks.py

ghl_message.py (Message sending)
├── imports: db
└── Called by: tasks.py

prompt.py (System prompts)
├── imports: none
└── Called by: main.py, tasks.py

individual_profile.py (Profile builder)
├── imports: none
└── Called by: sales_director.py, main.py

conversation_engine.py (Logic flow analyzer)
├── imports: none
└── Called by: sales_director.py

underwriting.py (Underwriting context)
├── imports: none
└── Called by: sales_director.py

insurance_companies.py (Company context)
├── imports: none
└── Called by: sales_director.py

age.py (Age calculator)
├── imports: none
└── Called by: tasks.py

utils.py (Utilities)
├── imports: none
└── Called by: main.py

sync_subscribers.py (Sheet sync)
├── imports: none
└── Called by: main.py on startup

worker.py (RQ worker) - STANDALONE SCRIPT
└── Runs: python worker.py production
```

### Unused Files (SAFE TO DELETE):
```
❌ unified_brain.py - Legacy prompt system (replaced by prompt.py)
❌ outcome_learning.py - ML system (not integrated)
⚠️ send_email_api.py - Test utility (optional)
⚠️ test_email.py - Test utility (optional)
```

---

## RECOMMENDATION

### Delete Immediately:
1. **unified_brain.py** - Old system, completely replaced
2. **outcome_learning.py** - Complete feature but not integrated (can archive if you want ML later)

### Optional Delete:
3. **send_email_api.py** - Useful for debugging, but not required
4. **test_email.py** - Useful for debugging, but not required

### Keep:
- All other files are actively used in production

---

## CLEANUP COMMAND

```bash
# Delete unused files (after review)
rm /home/user/Flask-Webhook/unified_brain.py
rm /home/user/Flask-Webhook/outcome_learning.py

# Optional: Delete test utilities if you never use them
# rm /home/user/Flask-Webhook/send_email_api.py
# rm /home/user/Flask-Webhook/test_email.py
```

---

## NOTES

- All 4 unused files total ~1,200 lines of dead code
- Removing them will make codebase cleaner and easier to maintain
- No production impact - these files are completely disconnected
- If you ever want outcome_learning.py back, it's in git history
- unified_brain.py has valuable insurance knowledge that could be useful for reference, but it's not used by the system

---

**Validation**: All imports across entire codebase checked ✅
**Safety**: No production code depends on these files ✅
**Recommendation**: Safe to delete ✅
