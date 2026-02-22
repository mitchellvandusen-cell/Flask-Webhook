# Edit Process & Development Workflow

This document defines the exact process any developer — or AI assistant — must follow when making changes to the InsuranceGrokBot codebase. Following this process ensures changes are safe, well-documented, and do not break existing functionality.

---

## 1. Before Making Any Changes — Read First

- Read `CLAUDE.md` in full to understand the current architecture: all HTTP routes, DB schema, environment variables, core file responsibilities, and key coding patterns.
- Read `OWNERS_MANUAL.md` Table of Contents and the sections relevant to the area you are about to change.
- Check `CHANGELOG.md` for recent changes that may affect the area you are working in. Another change made yesterday could conflict with what you are about to do.
- Never modify code you have not read. Skipping this step is the primary cause of regressions.

---

## 2. Understand the Request

- Clearly define in plain language what is being changed and why before touching any file.
- Identify every file that will be affected. Use Grep to search for existing usages of the function, route, or variable being changed. Use Glob to find related templates or modules.
- Check for existing functionality before writing new code — the feature you are about to add may already exist in a different form.
- Identify any validation or testing steps that should be performed after the change (e.g., "verify the dashboard loads", "verify the webhook deduplication still works").

---

## 3. Make the Changes

- Work on the designated feature branch. Branch naming convention: `claude/[feature-name]-[sessionId]`. Never work directly on `main` or `master`.
- Make focused, minimal changes. Do not refactor unrelated code, rename variables outside the scope of the request, or reorganize files unless explicitly instructed.
- Follow existing patterns throughout the codebase:
  - **Database access**: Always use `get_db_connection()` / `return_db_connection()` inside `try/finally` blocks. Connections must be returned even if an exception is raised.
  - **Redis access**: Use `ensure_redis()` before interacting with Redis. It auto-reconnects on failure.
  - **RQ jobs**: Name jobs `worker-{queue}-{uuid8}` for consistent debuggability.
  - **API key auth**: Use `hmac.compare_digest` for constant-time comparison. Never use `==` for secret comparison.
  - **LLM calls**: Use the `llm_caller.py` wrapper rather than calling the OpenAI-compatible client directly.
- Never break existing routes, DB schema, or API contracts without an explicit instruction to do so. Additive changes are safe; destructive changes require explicit approval.

---

## 4. Document the Change — After Every Significant Edit

- Add an entry to `CHANGELOG.md` immediately after completing the change. Each entry must include:
  - Date and time (UTC)
  - A short description of what changed
  - Which files were modified
  - Why the change was made
- If the change adds or modifies HTTP routes, update the route table in `CLAUDE.md`.
- If the change adds or modifies database tables or columns, update the DB schema section in `CLAUDE.md`.
- If the change introduces new environment variables, add them to the relevant section in `CLAUDE.md` and to `.env.example`.
- If the change introduces new failure modes or operational concerns, add a troubleshooting entry in `OWNERS_MANUAL.md`.

---

## 5. Update CLAUDE.md

- `CLAUDE.md` is the single source of truth for the architecture of this project. It must always reflect the actual current state of the codebase.
- After any change, update the relevant section of `CLAUDE.md`:
  - New or modified routes: update the HTTP Routes section.
  - New or modified DB tables: update the Database Schema section.
  - New env vars: update the Environment Variables section.
  - New files or renamed files: update the Core Files table.
  - New key patterns or conventions: update the Development Notes / Key Patterns section.
- Do not let `CLAUDE.md` drift out of sync with the code. A stale architecture doc is worse than no doc.

---

## 6. Commit Standards

- One commit per logical change. Do not bundle multiple unrelated changes into a single commit.
- Commit message format:
  ```
  [verb]: [what changed] — [brief why]
  ```
  Examples:
  ```
  add: /api/v1/chat/completions rate limit header — expose remaining quota to clients
  fix: DB connection leak in /webhook handler — connection not returned on timeout path
  update: CLAUDE.md route table — reflect new agency invite endpoints
  ```
- Include the session URL at the end of the commit message body (not the subject line).
- Stage only the files that are part of the logical change. Never use `git add .` blindly — review what is staged before committing.
- Never commit `.env`, `.env.local`, or any file containing real API keys or secrets. If a secrets file was accidentally modified, unstage it before committing.

---

## 7. Push & Verify

- Push to the feature branch (never directly to `main` or `master`).
- After pushing, verify the following:
  - No Python import errors in modified files: run `python -c "import main"` (or the relevant module) locally if possible.
  - No Jinja2 template syntax errors: Flask/Jinja2 will raise a `TemplateSyntaxError` on startup for unclosed tags, undefined filters, or mismatched blocks. Check any modified `.html` templates carefully.
  - No broken DB migrations: if a new table or column was added, confirm `init_db()` creates it correctly without destroying existing data.
  - No disrupted existing routes: if routes were modified, confirm existing integrations (GHL webhooks, Stripe webhooks, Twilio callbacks) still point to valid endpoints.

---

## 8. The Golden Rules

- **Never push to `main` or `master` directly.** All changes go through a feature branch.
- **Never skip reading `CLAUDE.md` before starting.** The architecture document exists so that changes are made with full context.
- **Never commit `.env` files or API keys.** These must never appear in version control.
- **Never modify more than what was requested.** Scope creep introduces unintended side effects.
- **Always return DB connections in `finally` blocks.** A leaked connection will eventually exhaust the pool under load.
- **Always log changes in `CHANGELOG.md`.** The changelog is the historical record; gaps make debugging future issues significantly harder.
- **Always verify Jinja2 templates after edits.** A single unclosed `{% block %}` or misplaced `}}` will take down the entire web server.
