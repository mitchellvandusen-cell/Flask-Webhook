---
name: dependency-audit
description: "Audit InsuranceGrokBot dependencies for CVEs, outdated packages, license compliance, and unused imports. Scans requirements.txt and all Python imports."
allowed-tools:
  - Read
  - Glob
  - Grep
  - Bash
  - WebFetch
  - WebSearch
---

# Dependency Audit Skill

You are auditing the dependencies of InsuranceGrokBot for security vulnerabilities, outdated packages, license issues, and unused dependencies.

## Pre-Audit: Load Context

1. Read `CLAUDE.md` for architecture context — note the key dependencies listed in the Development Notes section.
2. Read `requirements.txt` at the project root.

## Step 1: Inventory All Dependencies

1. Read `requirements.txt` and catalog every package with its pinned version.
2. Grep all `.py` files for `import` and `from ... import` statements to find actual usage:
   ```
   Grep for: ^import |^from .* import
   ```
3. Cross-reference: which packages in requirements.txt are actually imported in code?
4. Flag any packages in requirements.txt that are never imported (potential unused deps).
5. Flag any imports that reference packages NOT in requirements.txt (missing deps — may be stdlib or transitive).

## Step 2: Known CVE Scan

1. Run pip-audit if available:
   ```bash
   pip install pip-audit 2>/dev/null && pip-audit -r requirements.txt 2>&1
   ```
2. If pip-audit is not available, run safety check:
   ```bash
   pip install safety 2>/dev/null && safety check -r requirements.txt 2>&1
   ```
3. If neither tool works, manually check critical packages against known CVEs using web search for each package with pinned version.

### Critical Packages to Check (InsuranceGrokBot-Specific)

These packages handle sensitive operations and must be current:

| Package | Purpose | Risk if Outdated |
|---------|---------|-----------------|
| `flask` | Web framework | XSS, CSRF, session vulnerabilities |
| `gunicorn` | WSGI server | DoS, request smuggling |
| `werkzeug` | Flask dependency | Debug mode exploits, path traversal |
| `psycopg2-binary` | PostgreSQL driver | SQLi edge cases, connection security |
| `redis` | Redis client | Connection security, command injection |
| `twilio` | Twilio SDK | Auth issues, API compatibility |
| `stripe` | Payment processing | Payment bypass, webhook validation |
| `openai` | xAI API client | Token handling, API security |
| `flask-login` | Auth sessions | Session fixation, auth bypass |
| `flask-wtf` | CSRF protection | CSRF bypass |
| `itsdangerous` | Token signing | Token forgery |
| `cryptography` | Fernet encryption | Crypto vulnerabilities |
| `requests` | HTTP client | SSRF, certificate validation |
| `urllib3` | HTTP library | Various CVEs, SSRF |
| `certifi` | SSL certificates | Outdated CA bundle |
| `jinja2` | Template engine | SSTI (Server-Side Template Injection) |
| `markupsafe` | HTML escaping | XSS bypass |

## Step 3: Version Currency Check

1. For each package in requirements.txt, check if there's a newer version:
   ```bash
   pip list --outdated 2>&1
   ```
2. Classify outdated packages:
   - **Security update available**: Package has a newer version that fixes a CVE
   - **Major version behind**: Package is 1+ major versions behind (potential breaking changes)
   - **Minor version behind**: Package is minor versions behind (features + fixes)
   - **Patch behind**: Package is patch versions behind (bug fixes only)

## Step 4: License Compliance

1. Check licenses for all dependencies:
   ```bash
   pip install pip-licenses 2>/dev/null && pip-licenses --format=table 2>&1
   ```
2. Flag any packages with:
   - **GPL/AGPL licenses** — copyleft, may require open-sourcing InsuranceGrokBot
   - **Unknown licenses** — risk of future legal issues
   - **No license** — cannot legally use
3. Acceptable licenses for a proprietary SaaS:
   - MIT, BSD (2/3-clause), Apache 2.0, ISC, PSF, MPL 2.0

## Step 5: Transitive Dependency Analysis

1. Check for vulnerable transitive dependencies:
   ```bash
   pip install pipdeptree 2>/dev/null && pipdeptree --warn silence 2>&1 | head -200
   ```
2. Look for dependency conflicts (version incompatibilities between packages).
3. Check if any transitive dependency is known-vulnerable even if the direct dependency is fine.

## Step 6: Unused Dependency Detection

1. Compare imports found in Step 1 with requirements.txt.
2. For each potentially unused package, verify it's truly unused:
   - Could be used as a CLI tool (e.g., `gunicorn`, `rq`)
   - Could be a transitive dependency required by another package
   - Could be used in scripts not in the main codebase
   - Could be used in `Procfile` or Railway build commands
3. Read `Procfile` or any Railway config to check for CLI usage.

## Step 7: Python Version Compatibility

1. Check `NIXPACKS_PYTHON_VERSION` env var for the target Python version.
2. Verify all packages support that Python version.
3. Flag any packages with upcoming Python version deprecations.

## Output Format

```markdown
# Dependency Audit Report — InsuranceGrokBot

## Summary
- **Total packages**: [count]
- **Vulnerable (CVE)**: [count]
- **Outdated**: [count]
- **License issues**: [count]
- **Unused**: [count]

## Critical Vulnerabilities (Fix Immediately)

### [Package Name] v[current] — [CVE ID]
- **Severity**: [Critical/High/Medium/Low]
- **Description**: [What the vulnerability allows]
- **Fixed in**: v[version]
- **Impact on IGB**: [How this affects InsuranceGrokBot specifically]
- **Action**: `pip install [package]==[fixed_version]`

## Outdated Packages (Security Patches Available)

| Package | Current | Latest | Type | Risk |
|---------|---------|--------|------|------|
| [name] | [ver] | [ver] | [major/minor/patch] | [description] |

## License Issues

| Package | License | Issue |
|---------|---------|-------|
| [name] | [license] | [why it's a problem] |

## Potentially Unused Dependencies

| Package | In requirements.txt | Imported | Notes |
|---------|-------------------|----------|-------|
| [name] | Yes | No | [may be CLI tool, etc.] |

## Recommendations

### Immediate (P0)
- [ ] Upgrade [package] to fix [CVE]

### This Sprint (P1)
- [ ] Upgrade [packages] to latest minor versions
- [ ] Remove [unused packages] from requirements.txt

### Next Sprint (P2)
- [ ] Plan [major version] upgrade for [package]
- [ ] Evaluate alternatives for [problematic package]

## Dependency Tree (Key Branches)
[Simplified tree showing critical dependency chains]
```
