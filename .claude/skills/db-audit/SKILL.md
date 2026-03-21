---
name: db-audit
description: "Database security and health audit for InsuranceGrokBot. Checks for SQL injection, connection pool safety, missing indexes, multi-tenant isolation in queries, and N+1 patterns."
allowed-tools:
  - Read
  - Glob
  - Grep
  - Bash
---

# Database Audit Skill

You are auditing the PostgreSQL database layer of InsuranceGrokBot for security vulnerabilities, performance issues, and correctness. The DB layer is in `db.py` (~4200 lines) with additional queries in `ghl_sync.py`, `workflow_engine.py`, `lead_intelligence.py`, `tasks.py`, and blueprint files.

## Pre-Audit: Load Context

1. Read `CLAUDE.md` for the full database schema (30 tables) and connection pool configuration.
2. Note the pool config: `ThreadedConnectionPool`, min=2, max=20, 500 waiters, 10s timeout.
3. Note the DB access pattern: `get_db_connection()` / `return_db_connection()` in `try/finally`.

## Step 1: SQL Injection Audit

### 1a. Find All SQL Queries

Scan every Python file for SQL statements:

```
Grep for: cursor\.execute|\.execute\(|SELECT |INSERT |UPDATE |DELETE |CREATE |ALTER |DROP  in *.py
```

### 1b. Check Query Construction Method

For each query found, classify it:

**SAFE** — Parameterized with `%s` placeholders:
```python
cursor.execute("SELECT * FROM subscribers WHERE location_id = %s", (location_id,))
```

**UNSAFE** — String formatting or concatenation:
```python
cursor.execute(f"SELECT * FROM subscribers WHERE location_id = '{location_id}'")
cursor.execute("SELECT * FROM subscribers WHERE location_id = '%s'" % location_id)
cursor.execute("SELECT * FROM subscribers WHERE location_id = " + location_id)
```

**NEEDS REVIEW** — Dynamic table/column names (parameterization doesn't work for identifiers):
```python
cursor.execute(f"SELECT * FROM {table_name} WHERE id = %s", (id,))
```
These need allowlist validation on `table_name`.

### 1c. Priority Files to Audit

Check these files most carefully (ordered by risk):

1. `db.py` (~4200 lines) — All core data access
2. `ghl_sync.py` (~900 lines) — Sync engine with complex queries
3. `workflow_engine.py` (~2300 lines) — Dynamic workflow queries
4. `lead_intelligence.py` — Intelligence cache queries
5. `tasks.py` (~1400 lines) — Background job queries
6. `blueprints/agency.py` — Agency KPI aggregate queries
7. `blueprints/admin.py` — Admin queries (high privilege)
8. `voice/stats.py` — Call statistics queries
9. `voice/call_history.py` — Call history queries
10. `voice/dialer.py` — Dialer queries (cooldown, daily max)
11. `voice/numbers.py` — Number management queries
12. `crm_providers/hubspot/sync.py` — HubSpot sync queries

### 1d. Dynamic Query Patterns

Search for IN-clause construction (common injection vector):

```
Grep for: IN \(|\.join\(|','.join in *.py files that also contain execute
```

Verify IN-clauses use proper parameterization:
```python
# SAFE
placeholders = ','.join(['%s'] * len(ids))
cursor.execute(f"SELECT * FROM table WHERE id IN ({placeholders})", tuple(ids))

# UNSAFE
ids_str = ','.join(ids)
cursor.execute(f"SELECT * FROM table WHERE id IN ({ids_str})")
```

## Step 2: Connection Pool Safety

### 2a. Find All Connection Usage

```
Grep for: get_db_connection\(\)|return_db_connection\( in *.py
```

### 2b. Verify try/finally Pattern

For every `get_db_connection()` call, verify:
1. There is a matching `return_db_connection()` in a `finally` block
2. The connection variable is assigned BEFORE the `try` block
3. No code paths exist where the connection could leak (early returns, nested exceptions)

**Pattern check:**
```python
# CORRECT
conn = get_db_connection()
try:
    # ... use conn ...
finally:
    return_db_connection(conn)

# WRONG — connection leak on exception before try
conn = get_db_connection()
result = some_function_that_might_raise()  # LEAK if this raises
try:
    # ...
finally:
    return_db_connection(conn)

# WRONG — early return without returning connection
conn = get_db_connection()
try:
    if not valid:
        return None  # CONNECTION LEAKED
    # ...
finally:
    return_db_connection(conn)  # This is fine actually — finally runs on return
```

Note: In Python, `finally` blocks DO execute on early `return`, so the third example is actually safe. But verify each case.

### 2c. Connection Held Across Boundaries

Flag any pattern where a connection is:
- Passed to another function that doesn't return it
- Stored in a class attribute or global
- Used across `yield` boundaries (generators)
- Used in a callback or deferred execution

### 2d. Nested Connection Usage

Flag functions that call `get_db_connection()` while already holding a connection:
```python
def outer():
    conn = get_db_connection()
    try:
        inner()  # Does inner() also get a connection? Pool starvation risk
    finally:
        return_db_connection(conn)
```

## Step 3: Multi-Tenant Isolation in Queries

### 3a. Tables That Require Tenant Scoping

Every query on these tables MUST include `location_id` (or equivalent tenant key):

| Table | Tenant Key |
|-------|-----------|
| `subscribers` | `location_id` (PK) |
| `contact_messages` | `location_id` |
| `contact_facts` | `location_id` |
| `processed_webhooks` | `location_id` |
| `contact_narratives` | `location_id` |
| `webhook_logs` | `location_id` |
| `persistent_alerts` | `location_id` |
| `call_history` | `location_id` |
| `ai_minute_balances` | `location_id` |
| `ai_minute_usage_logs` | `location_id` |
| `contact_cache` | `location_id` |
| `ghl_conversations` | `location_id` |
| `ghl_opportunities` | `location_id` |
| `ghl_sync_state` | `location_id` |
| `number_health` | `location_id` |
| `contact_intelligence` | `location_id` |
| `location_users` | `location_id` |
| `team_audit_log` | `location_id` |
| `failed_webhook_payloads` | `location_id` |
| `discord_connections` | `user_email` (tied to subscriber) |
| `slack_connections` | `user_email` |

### 3b. Scan for Unscoped Queries

For each table above, find all queries and verify the tenant key is in the WHERE clause:

```
For each table:
  Grep for: FROM {table}|INTO {table}|UPDATE {table} in *.py
  Check each result has location_id (or equivalent) in WHERE clause
```

### 3c. Agency Queries

Agency dashboard queries aggregate across multiple subscribers. Verify:
- They scope to the agency owner's linked subscriber list
- They don't accidentally include unrelated subscribers
- The subscriber list is derived from `agency_billing.company_id` matching, not user input

## Step 4: Missing Indexes

### 4a. Read Current Schema

Read `db.py` `init_db()` to find all CREATE TABLE and CREATE INDEX statements.

### 4b. Identify Missing Indexes

Check for indexes on columns used in:
- WHERE clauses (equality and range filters)
- JOIN conditions
- ORDER BY clauses
- Frequently queried foreign keys

Common missing indexes to check:
- `contact_messages(contact_id, location_id)`
- `contact_messages(created_at)` (if queried by date range)
- `call_history(location_id, created_at)`
- `call_history(contact_id)`
- `webhook_logs(location_id, created_at)`
- `ghl_conversations(contact_id, location_id)`
- `ghl_opportunities(location_id)`
- `contact_intelligence(location_id, analyzed_at)`
- `processed_webhooks(webhook_id)` (for dedup lookups)
- `location_users(location_id)`
- `location_users(email)`

### 4c. Over-Indexing Check

Also check for indexes that are:
- Duplicates (same columns, different names)
- Never used (on columns that are never queried)
- Too broad (indexes on every column of a table)

## Step 5: N+1 Query Detection

### 5a. Find Loop-Query Patterns

```
Grep for patterns where a query is inside a loop:
- for.*in.*:\n.*cursor\.execute
- while.*:\n.*cursor\.execute
```

Read the surrounding code context. Common N+1 patterns:

```python
# N+1: Query per contact in a list
contacts = get_all_contacts(location_id)
for contact in contacts:
    messages = get_messages(contact['id'])  # This is a DB query per contact!
```

Should be refactored to:
```python
contacts = get_all_contacts(location_id)
contact_ids = [c['id'] for c in contacts]
all_messages = get_messages_bulk(contact_ids)  # Single query with IN clause
```

### 5b. Priority Areas to Check

- `tasks.py` — webhook processing pipeline (fetches contact + messages + facts + narrative)
- `lead_intelligence.py` — bulk analysis (should batch)
- `voice/contacts.py` — contact data fetching
- `voice/dialer.py` — multi-dial (checks cooldown per contact)
- `blueprints/agency.py` — KPI aggregation across subscribers
- `ghl_sync.py` — sync operations
- `workflow_engine.py` — workflow execution (evaluates conditions per contact)

## Step 6: Transaction Safety

### 6a. Check Commit/Rollback Patterns

```
Grep for: conn\.commit\(\)|conn\.rollback\(\) in *.py
```

Verify:
- Writes (INSERT/UPDATE/DELETE) are followed by `conn.commit()`
- Failed writes have `conn.rollback()` in exception handlers
- Multi-step operations use proper transaction boundaries

### 6b. Advisory Locks

```
Grep for: pg_advisory|advisory_lock in *.py
```

Verify advisory locks are used for:
- OAuth token refresh (prevent race conditions across workers)
- Any operation that must be serialized across workers

## Step 7: Data Integrity

### 7a. Foreign Key Constraints

Check if foreign keys are enforced:
- `contact_messages.location_id` → `subscribers.location_id`
- `call_history.location_id` → `subscribers.location_id`
- `location_users.location_id` → `subscribers.location_id`

Note: Many tables may lack formal FK constraints (common in large apps). Document which ones are missing.

### 7b. NOT NULL Constraints

Check for columns that should be NOT NULL but aren't:
- Tenant keys (`location_id`) should always be NOT NULL
- Primary keys should be NOT NULL
- Created/updated timestamps should have defaults

### 7c. UPSERT Correctness

```
Grep for: ON CONFLICT in *.py
```

Verify each ON CONFLICT clause:
- Has the correct conflict target (unique index or constraint)
- Updates the right columns on conflict
- Doesn't accidentally overwrite important data

## Output Format

```markdown
# Database Audit Report — InsuranceGrokBot

## Summary
| Category | Issues Found |
|----------|-------------|
| SQL Injection | [count] |
| Connection Leaks | [count] |
| Tenant Isolation | [count] |
| Missing Indexes | [count] |
| N+1 Queries | [count] |
| Transaction Issues | [count] |
| Data Integrity | [count] |

## Critical: SQL Injection Vulnerabilities
### [Finding]
- **File**: `path/to/file.py:line`
- **Query**: [the unsafe query]
- **Fix**: [parameterized version]

## Critical: Connection Pool Leaks
### [Finding]
- **File**: `path/to/file.py:line`
- **Issue**: [missing finally, early return, etc.]
- **Fix**: [corrected pattern]

## High: Multi-Tenant Isolation Gaps
### [Finding]
- **File**: `path/to/file.py:line`
- **Query**: [query missing tenant filter]
- **Impact**: [what data could leak]
- **Fix**: [add WHERE location_id = %s]

## Medium: Missing Indexes
| Table | Column(s) | Query Pattern | Estimated Impact |
|-------|-----------|--------------|-----------------|
| [table] | [cols] | [what queries would benefit] | [high/medium/low] |

## Medium: N+1 Query Patterns
### [Finding]
- **File**: `path/to/file.py:line`
- **Pattern**: [description of the loop-query]
- **Fix**: [batch query approach]

## Low: Data Integrity Issues
[Missing constraints, FKs, etc.]

## Recommendations
### Immediate (P0)
- [ ] Fix SQL injection in [file]
- [ ] Fix connection leak in [file]

### This Sprint (P1)
- [ ] Add tenant scoping to [queries]
- [ ] Add missing indexes

### Next Sprint (P2)
- [ ] Refactor N+1 patterns
- [ ] Add FK constraints
- [ ] Review transaction boundaries
```
