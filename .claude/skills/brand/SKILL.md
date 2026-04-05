---
name: brand
description: Omnisconn brand identity, design system, theme rules, and visual standards. Single-accent luxury discipline, post-Phase-5 glassmorphism + rainbow-drift sweep. Auto-invoked when working on UI, CSS, templates, themes, colors, logos, styling, design, white-label, or any visual element.
allowed-tools: Read, Glob, Grep, Edit, Write, Bash, WebFetch
---

# Omnisconn Brand Identity & Design System

**This skill is the single source of truth for all visual and brand decisions.** It MUST be followed when touching ANY UI surface — templates, CSS, JavaScript UI, marketing pages, dashboard, or white-label features.

**Product positioning:** Omnisconn is a **high-end, luxurious, classy, professional** AI SMS + voice platform for insurance agents ($99–$350/mo). Every UI surface must look like it costs $500+/mo. Think Linear / Stripe / Vercel in aesthetic register — sharp, solid, restrained, single-accent.

## Trigger Words

This skill auto-activates when the task involves any of these:
- **brand**, **branding**, **logo**, **favicon**, **identity**
- **design**, **UI**, **UX**, **layout**, **visual**, **luxurious**, **high-end**, **classy**, **professional**
- **theme**, **dark mode**, **light mode**, **light-theme**, **toggle**
- **color**, **accent**, **neon green**, **#00ff88**, **single accent**, **rainbow**
- **CSS**, **style**, **stylesheet**, **style.css**
- **font**, **typography**, **Outfit**, **Inter**
- **template**, **Jinja**, **HTML**, **dashboard.html**, **base.html**
- **card**, **panel**, **modal**, **dropdown**, **sidebar**, **topbar**
- **white-label**, **whitelabel**, **agency branding**, **custom brand**
- **glassmorphism**, **backdrop-filter**, **gradient**, **glass**
- **icon**, **carrier**, **AT&T**, **Verizon**, **T-Mobile**
- **marketing page**, **landing page**, **home page**
- **inline style** (to block it)

---

## 1. Brand Identity

### Product Name
- **Primary brand**: **Omnisconn** (full brand across all marketing + dashboard chrome)
- **Legacy name**: InsuranceGrokBot — still appears in some internal code paths, CLAUDE.md, memory files, and certain templates that haven't been migrated. When touching those surfaces, migrate to `Omnisconn`. Do NOT rename internal Python variables or DB columns that reference `igb_*` / `grok_*` — only user-visible strings.
- **Tagline**: "The Power Dialer Built for Insurance Agents"
- **Product type**: White-label AI-powered SMS and Voice bot platform for insurance agents

### Marketing Site Logo
```html
<a class="navbar-brand" href="/">Omnis<span class="text-accent">conn</span></a>
```
- "Omnis" in white, "conn" in neon green (`var(--accent)`)
- Font: Outfit, weight 800+, letter-spacing -0.5px

### Logo (Favicon)
The logo is a **neon green robot face** at `static/favicon.svg` — rounded robot head with antenna, ears, eyes with white highlights, and a rectangular mouth.
- 64x64 SVG viewBox
- Gradient fill: `#00ff88` → `#00cc66` (135deg)
- Background: transparent (adapts to any surface)

---

## 2. ⛔ THE SINGLE RULE: Accent As Signal

**Green is a signal, not decoration.** This is the most important brand rule. It is what took the product from 3.2/5 to 4.6/5 luxury in Phases 1–5.

Green (`#00ff88` dark / `#059669` light) appears on **exactly four things**:

1. **Active / selected state** — the currently-selected tab, radio card, filter pill, plan card border
2. **Primary CTA** — the one button a user is expected to click on that screen
3. **The one accent word in a headline** — "No More **Spam Likely**", "Pick Your Plan. **Start Closing**", "Your Leads Text Back. **You Never Miss One**"
4. **Icons and micro-accents inside solid dark cards** — feature checkmarks, success pills, status dots, section header icons

**Nothing else is green.**

### The Rainbow-Drift Violations That Are Prohibited

These are specific failure modes seen in Phase 0 audit that must never return:

| Violation | Example | Why prohibited |
|---|---|---|
| **Multi-color plan cards** | Billing with green/orange/purple/red tiers | Kills trust at the moment of payment |
| **Multi-color action buttons in a row** | Workflows: orange Import + purple Build-with-AI + green New | Three accents fighting in 300px |
| **Multi-color stat tiles for zero states** | Team: green 0 ACTIVE / orange 0 PENDING / red 0 VOICE / gray 0 INACTIVE | Color for color's sake |
| **Gradient social-login buttons** | Login: orange LeadConnector + coral HubSpot dominating Sign In | Inverts CTA hierarchy |
| **Rainbow duration-bucket legends** | Agency Statistics: red/orange/yellow/blue/purple dots | Rainbow charts are retired |
| **Purple/blue/cyan utility classes** | `.ic-purple`, `.ic-cyan`, `.ic-orange`, `.trn-*-purple` | All must alias to `var(--accent)` |
| **Gradient fills on CTAs** | `linear-gradient(135deg, #a78bfa, #3b82f6, var(--accent))` | Flat depth only — no CTA gradients |
| **Multi-color rainbow audio player** | Play green / Pause yellow / Download blue / Transcribe orange / Transcript purple | Same action family = same color |
| **Purple "coming soon" / orange "trial" / gold "super admin"** | Multi-color status badges on admin surfaces | Admin tools stay restrained |

### Semantic Colors ARE Allowed

These are NOT rainbow drift — they communicate state:

```css
--status-success: #00ff88;  /* = var(--accent) */
--status-error:   #ef4444;  /* red — failures, cancelled members, blocked contacts */
--status-warning: #f59e0b;  /* amber — warnings, pending verification */
--status-info:    #3b82f6;  /* blue — info banners, INFO log badges */
```

**The rule:** Red/amber/blue are only legal for meaningful state. If a tile, badge, or icon is not communicating an error, a warning, or an info message, it must be green.

### Contact Temperature (AI Intelligence)

Lead temperature colors ARE allowed because they communicate meaningful state:

```css
--temp-hot:  #ef4444;  /* red — actively buying */
--temp-warm: #f59e0b;  /* amber — engaged */
--temp-cool: #3b82f6;  /* blue — went quiet */
--temp-cold: #6b7280;  /* gray — cold/dead */
```

Use these ONLY on the AI Smart Filter temperature badges, lead intelligence dossiers, and dialer contact rows. Never elsewhere.

### Legacy #00d9ff Cyan — Retired

The old secondary cyan `#00d9ff` was retired in Phase 3. Do not add new uses. Any remaining reference in code must be migrated to `var(--accent)` or removed.

---

## 3. Color System

### Dark Theme (Default)

```css
/* Primary accent — the signature neon green */
--accent:       #00ff88;
--accent-hover: #00cc6a;
--accent-dim:   rgba(0, 255, 136, 0.12);
--accent-ring:  rgba(0, 255, 136, 0.35);   /* focus rings, selected borders */

/* Backgrounds — solid hex only, never rgba */
--dark-bg:        #050505;
--dark-surface:   #0a0a0a;   /* canonical card background */
--dark-surface-2: #111111;   /* elevated surface */
--dark-surface-3: #1a1a1a;   /* subtle highlight */

/* Borders — solid hex */
--border-subtle:   #1a1a1a;   /* canonical card border */
--border-medium:   #2a2a2a;
--border-accent:   rgba(0,255,136,0.25);   /* accent ghost outline */

/* Text hierarchy */
--text-primary:   #ffffff;
--text-secondary: #a0a0a0;
--text-muted:     #666666;
--text-faint:     #444444;
```

### Light Theme (`body.light-theme` AND `[data-theme="light"]`)

**Critical gotcha:** The theme toggle (`sidebar.js:249-255`) sets BOTH `data-theme="light"` on `<html>` AND `body.light-theme`. CSS rules can target either — both work. But every new dark-mode rule MUST have a light-mode counterpart.

```css
/* Light theme — pure white base, no gray wash */
--lt-bg:            #ffffff;
--lt-surface:       #ffffff;
--lt-surface-hover: #fafafa;
--lt-border:        #e5e7eb;
--lt-text-primary:   #111827;
--lt-text-secondary: #374151;
--lt-text-muted:     #6b7280;
--lt-accent:         #059669;   /* darker green for light mode */
--lt-accent-hover:   #047857;
--lt-accent-bg:      rgba(5, 150, 105, 0.08);
--lt-accent-ring:    rgba(5, 150, 105, 0.35);
```

**Both-theme rule is mandatory:** Every dark-mode color-carrying rule MUST have a `body.light-theme` counterpart. Per `feedback_always_both_themes.md` memory. No exceptions.

---

## 4. Typography

### Font Stack

| Font | Weight | Usage |
|------|--------|-------|
| **Outfit** | 300, 400, 500, 700, 800, 900 | Headings, UI labels, hero copy, section headers, buttons, marketing display |
| **Inter** | 300, 400, 500, 600 | Body text, sidebar, topbar, form inputs, long-form content |
| **JetBrains Mono** | 400, 500 | ONLY: code blocks, API keys, phone numbers in tables, terminal output, SIDs |

### Critical: Mono is NOT for marketing subheads

Phase 1 killed the `JetBrains Mono` subhead on the home hero because mono reads "hacker/DIY" not "luxurious." **Mono is permitted ONLY for genuine data values** (phone numbers, timestamps, hex IDs, API tokens) where its "literal bytes" connotation earns its keep. Do NOT use mono for:
- Marketing page subheads
- Dashboard helper text (`vc-hint`, `form-text`)
- Page descriptions
- Placeholder text

All of those use Outfit 400 or Inter 400.

### Hero Headline Pattern

The two-beat accent pattern is the signature headline treatment across the site:

```html
<h1>No More<br><em>Spam Likely.</em></h1>
```

```css
.hero-h1 {
    font-family: 'Outfit', sans-serif;
    font-weight: 900;
    color: #ffffff;             /* white primary line */
    font-size: clamp(3.5rem, 7.5vw, 6.5rem);
    letter-spacing: -3px;
    line-height: 1.0;
}
.hero-h1 em {
    font-style: normal;
    color: var(--accent);       /* green accent second line */
    position: relative;
}
.hero-h1 em::after {
    /* thin underline under the accent word */
    content: ''; position: absolute; bottom: 4px; left: 0; right: 0;
    height: 3px; background: var(--accent);
}
body.light-theme .hero-h1 { color: #0f172a; }
body.light-theme .hero-h1 em { color: #059669; }
body.light-theme .hero-h1 em::after { background: #059669; }
```

Reference pages that get this right (use as templates):
- `/sms` — "Your Leads Text Back. **You Never Miss One.**"
- `/workflows` — "Leads Don't Wait. **Your Workflows Don't Either.**"
- `/spam-protection` — "Your Calls Look Like a **Business, Not a Scammer.**"
- `/` (home) — "No More **Spam Likely.**"

---

## 5. ⛔ NO GLASSMORPHISM — Permanently Retired

Phase 2 audit traced the root cause of SMS Config glassiness to **CSS @import load order**:

- `static/css/style.css` imports `dashboard/forms.css` at line 28 (solid `.form-control` rules)
- Same file imports `auth/auth.css` at line 66 (legacy glass `.form-control` with `!important`)
- Auth.css wins because it loads later → every dashboard form was glassing

Phase 2 flattened the auth.css `.form-control` rule. If you ever see translucent form inputs again, check whether a later-loaded CSS file has a `.form-control` rule with `!important`.

### The Rules

> **Never write `backdrop-filter`, `blur()`, or translucent `rgba(255,255,255,0.0x)` backgrounds on new elements.**

- **NO** `backdrop-filter: blur(...)` — ever
- **NO** `background: rgba(255,255,255,0.04)` or 0.03 or 0.06 or 0.08 translucent surfaces
- **NO** asymmetric border lighting (`--glass-border-top`, `--glass-border-left` etc.)
- **NO** `--glass-*` variables in new code (legacy backward-compat only; migrate on touch)
- **NO** inset shimmer, chromatic bevel, or frosted edge tricks

### The New Design Language

**Sharp · Bold · Solid · Authoritative**
- Solid hex backgrounds: `#050505`, `#0a0a0a`, `#111`, `#1a1a1a` on dark; `#ffffff`, `#fafafa` on light
- Hard `1px solid` borders: `#1a1a1a` dark / `#e5e7eb` light
- Flat depth — use subtle `box-shadow` for elevation, never blur
- Green accent on black — maximum contrast, maximum authority
- Bold Outfit typography carries the hierarchy

### Canonical Solid Card Pattern

```css
.my-card {
    background: #0a0a0a;
    border: 1px solid #1a1a1a;
    border-radius: 12px;
    transition: border-color 0.18s ease;
}
.my-card:hover {
    border-color: rgba(0,255,136,0.25);
}
.my-card.selected {
    border-color: rgba(0,255,136,0.45);
    background: rgba(0,255,136,0.03);
}
body.light-theme .my-card {
    background: #ffffff;
    border-color: #e5e7eb;
}
body.light-theme .my-card:hover {
    border-color: rgba(5,150,105,0.35);
}
body.light-theme .my-card.selected {
    border-color: rgba(5,150,105,0.55);
    background: rgba(5,150,105,0.04);
}
```

### Canonical Ghost Button Pattern (secondary actions)

```css
.my-ghost-btn {
    background: #0a0a0a;
    color: #cccccc;
    border: 1px solid #1e1e1e;
    transition: border-color 0.18s, color 0.18s, background 0.18s;
}
.my-ghost-btn:hover {
    background: #0a0a0a;
    color: #ffffff;
    border-color: rgba(0,255,136,0.35);
}
.my-ghost-btn i { color: #888; transition: color 0.18s; }
.my-ghost-btn:hover i { color: var(--accent); }
body.light-theme .my-ghost-btn {
    background: #ffffff;
    color: #374151;
    border-color: #e5e7eb;
}
body.light-theme .my-ghost-btn:hover {
    border-color: rgba(5,150,105,0.40);
    color: #111827;
}
```

### Canonical Primary CTA (flat, single color)

```css
.my-primary-btn {
    background: var(--accent);
    color: #000000;
    border: 1px solid var(--accent);
    font-weight: 800;
}
.my-primary-btn:hover {
    background: #00cc6a;
    border-color: #00cc6a;
}
body.light-theme .my-primary-btn {
    background: #059669;
    color: #ffffff;
    border-color: #059669;
}
body.light-theme .my-primary-btn:hover {
    background: #047857;
}
```

---

## 6. ⛔ NO INLINE STYLING + Attribute-Selector Override System

**Inline `style="..."` is prohibited in all Jinja2/HTML templates.** Only exception: JavaScript-driven dynamic values (e.g. `style="width: 0%"` where JS updates it).

### Why

Inline styles bypass the CSS variable system and can't respond to theme toggle. They also produce the rainbow-drift effect: templates reach for inline `color:#a78bfa` / `background:rgba(255,255,255,0.04)` because adding a new class felt heavier, and the cumulative result is a multi-color UI.

### The Backstop: Global Attribute-Selector Override

Because there are ~1,200 pre-existing inline styles across legacy templates that can't all be rewritten at once, the brand system ships a backstop in `static/css/dashboard/middle-column.css`:

```css
/* Catches any inline translucent-white background and flattens it */
:not([data-theme="light"]) [style*="background:rgba(255,255,255,0.04)"],
:not([data-theme="light"]) [style*="background: rgba(255,255,255,0.04)"],
:not([data-theme="light"]) [style*="background:rgba(255,255,255,0.03)"],
... {
    background: #0a0a0a !important;
    border-color: #1a1a1a !important;
}

/* Catches off-brand text colors (purple, orange, blue, yellow) inside dashboard */
.dash-main [style*="color:#a78bfa"],
.dash-main [style*="color:#8b5cf6"],
.dash-main [style*="color:#ffa500"],
.dash-main [style*="color:#3b82f6"],
... {
    color: var(--accent) !important;
}

/* Catches inline linear-gradient buttons and flattens them */
.dash-main [style*="linear-gradient(135deg,#ffa500"],
.dash-main [style*="linear-gradient(135deg,#a78bfa"],
... {
    background: var(--accent) !important;
    color: #000 !important;
}
```

**This is a backstop, not a license.** Continue to write new templates without inline styles. When editing an existing template, migrate its inline styles to classes. Every class you add makes one more attribute-selector fallback unnecessary.

### The Rule

1. Add a semantic CSS class to the element (e.g. `class="stat-card"`)
2. Define the visual properties in the appropriate CSS file (never style.css directly — pick the namespace: `forms.css`, `panels.css`, `left-column.css`, etc.)
3. Add a `body.light-theme` override for the same class

---

## 7. CSS Architecture (Post-Phase 5)

### Load Order (critical for debugging cascade wars)

`static/css/style.css` @imports in this order. Later files override earlier ones on tied specificity:

1. `base/variables.css` — CSS custom properties (both themes)
2. `base/reset.css`
3. `base/utilities.css`
4. `marketing/marketing.css` — all public pages
5. `marketing/chain-spam.css`
6. `dashboard/layout.css`
7. `dashboard/left-column.css` — sidebar
8. `dashboard/buttons.css`
9. `dashboard/text.css`
10. `dashboard/forms.css` — `.vc-*` design system + `.form-control` solid
11. `dashboard/panels.css` — cards, modals, billing, cfg-* config panels, tier badges
12. `dashboard/middle-column.css` — **inline-style override backstop lives here**
13. `dashboard/power-dialer.css` — `.dlr-*` namespace
14. `dashboard/phone/phone-ui.css` — iPhone simulator frame
15. `dashboard/phone/apps/*.css` — per-app iOS styling (messages, calls, voicemail, etc.)
16. `dashboard/import-contacts.css`, `business-profile.css`, `onboarding.css`, `aiminutes.css`
17. `agency-dashboard/agency.css` — agency dashboard
18. **`auth/auth.css` — loads LATE; any `.form-control` `!important` here will win on every form across the site** (this was the root cause of Phase 0 SMS Config glassiness)
19. `base/mobile.css`

### Rainbow Drift Defenses in Place (do not remove)

| File | Selector pattern | Purpose |
|---|---|---|
| `panels.css` final block | `.team-meet-btn, .conn-google-btn, .wfb-ai-build-btn, .ni-register-btn, .ni-remediate-btn, .ni-edit-submit-btn, .cnam-sync-all-btn` → `!important` green | Catches gradient buttons that templates might re-introduce |
| `panels.css` | `.metallic-price--cyan, --orange, --purple` aliased to `--green` | Keeps all plan prices in single green |
| `panels.css` | `.billing-check-accent, --cyan, --orange, --purple` all → `var(--accent)` | Keeps plan checkmarks unified |
| `panels.css` | `.ic-cyan, .ic-purple, .ic-orange` all → `var(--accent)` | Utility icon classes can't drift |
| `panels.css` | `.trn-*-purple` (training tab) → `var(--accent)` | Training tab unified |
| `agency.css` | `.audio-play-btn, .audio-pause-btn, .audio-download-btn, .audio-transcribe-btn, .audio-transcript-btn` all → single accent | Audio player rainbow killed |
| `agency.css` | `.ak-dot-red/amber/green/blue/purple` → single accent with 0.28/0.46/0.64/0.82/1.0 opacity progression | Duration bucket legend unified |
| `marketing.css` | `.article-badge--red/cyan/green/purple/orange` → single accent | Article category badges unified |
| `marketing.css` | `.connect-follow-btn--facebook/--linkedin` → single accent | Social login buttons unified |
| `auth/auth.css` | `.ghl-sso-btn, .hs-sso-btn` → ghost outline | No more gradient SSO buttons |
| `middle-column.css` | `.dash-main [style*="color:#a78bfa"]` etc. → `var(--accent)` | Backstop for inline purple/orange/blue drift |
| `middle-column.css` | `[style*="background:rgba(255,255,255,0.04)"]` → solid hex | Backstop for inline translucent backgrounds |

**When you see rainbow drift return, check these rules first — someone may have deleted them.**

---

## 8. White-Label System

Agency owners can fully replace the Omnisconn brand with their own.

### What's Customizable

| Field | Type | UI Component |
|-------|------|-------------|
| `company_name` | String (max 100) | Text input |
| `logo_url` | URL (max 500) | File upload + URL paste |
| `name_font` | Google Font name | Select dropdown (10 fonts) |
| `name_bold` / `name_italic` / `name_underline` | Boolean | Toggle buttons |
| `accent_color` | Hex | Color picker (defaults to `#00ff88`) |
| `dashboard_font` | Google Font | Select dropdown |

### Implementation

- **Settings UI**: `templates/dashboard/tabs/whitelabel.html` (icon: `fa-palette`)
- **JavaScript**: `static/js/dashboard/whitelabel.js`
- **API**: `POST /api/agency/whitelabel`
- **Storage**: `agency_billing.whitelabel_config` JSONB column
- **Sidebar logo**: `.sb-brand-logo` (32x32 container) — shows custom logo or falls back to `favicon.svg`
- **Sidebar name**: `.sb-brand-text` — shows custom company name
- **CSS injection**: CSS custom properties overridden dynamically on dashboard load
- **Scope**: Only logged-in dashboard experience. Marketing pages stay Omnisconn-branded.

### White-Label Development Rules

1. Never hardcode `Omnisconn` or `InsuranceGrokBot` in dashboard templates — use a template variable
2. Never hardcode `#00ff88` in dashboard HTML — always use `var(--accent)`
3. All accent-colored elements must use CSS variables so white-label overrides work
4. Marketing pages (`/`, `/for-agencies`, `/sms`, etc.) are NOT white-labeled

---

## 9. Layout & Spacing

### CSS Variables

```css
--sidebar-width:           260px;
--sidebar-collapsed-width: 62px;
--topbar-height:           48px;
```

### Dashboard Structure

```
┌─────────────────────────────────────────────┐
│ Topbar (fixed, solid #0a0a0a)                │
├──────┬──────────────────────────────────────┤
│      │                                       │
│ Side │  Main Content Area (.dash-main)       │
│ bar  │  (tab panes)                          │
│ L1   │                                       │
│ + L2 │                                       │
├──────┴──────────────────────────────────────┤
```

### Sidebar Accordion Structure (L1 + L2)

The sidebar uses a two-level accordion. L1 sections expand/collapse L2 children. Agency features nest under **Business → TEAM MANAGEMENT**:
- Business → REGULATORY → Business Profile, Domain & Website
- Business → PRODUCTIVITY → CRM, Contact Management, Contracted Carriers, Activity Logs
- Business → TEAM MANAGEMENT → Teams, Members, Statistics, White Label
- Business → SUBSCRIPTION → Billing

See `memory/reference_dashboard_sidebar_structure.md` for the full map.

### Border Radius Scale

- Small (inputs, chips, badges): `2px` (sharp/premium) or `8px` (softer)
- Medium (cards, panels): `10–12px`
- Large (modals, dropdowns): `14–16px`
- Extra-large (hero cards, pricing): `20–22px`

### Standard Transition

```css
transition: border-color 0.18s ease, background 0.18s ease, color 0.18s ease;
```

Never use `transition: all 0.2s` — it causes layout-thrash and performance hits.

---

## 10. Marketing Page Aesthetic

### Style: Dark Premium SaaS — Sharp, Bold, Solid

- Deep black backgrounds (`#000`, `#050505`) — solid, never translucent
- Two-beat headline pattern (white primary + green accent line)
- Large bold Outfit typography (up to 900 weight)
- Hero stat tiles, pricing cards, testimonials — all solid cards with 1px borders
- CRM integration chip badges
- No hero-text gradients, no glass, no glow overlays

### Pricing Section (`/#pricing`)

Reference implementation for 4-tier pricing that works in both themes:
- 4 cards in a symmetric 4-column grid (`grid-template-columns: repeat(4, 1fr)`)
- 2×2 at `max-width: 1100px`, 1 column at `max-width: 560px`
- Featured card gets 2px top border + solid green CTA
- All prices in green via `metallic-price--green` class
- All feature checkmarks in accent via `billing-check-accent`
- `CURRENT` / `MOST POPULAR` badges in green ghost outline

### SEO Article Components

All articles use:
- `.article-page` — page layout
- `.article-card-badge` + `.article-badge--{green,cyan,red,purple,orange}` — all unified to single accent (Phase 5)
- `.article-card` — solid dark card with 1px border
- `.article-tag` — muted chip

### Social Links

- **Facebook**: `https://www.facebook.com/profile.php?id=61587536844180`
- **LinkedIn**: `https://www.linkedin.com/company/insurancegrokbot`

### Connect Page CTAs

`.connect-follow-btn--facebook` and `.connect-follow-btn--linkedin` are unified to ghost accent — NOT Facebook blue / LinkedIn blue. The CTA is to follow Omnisconn, not to showcase the platforms.

---

## 11. Dashboard Design Language

### Authoritative Form Controls (`.vc-*` system)

The `vc-*` namespace in `dashboard/forms.css` is the canonical form system used across SMS Config, Voice Config, Advanced Settings, etc.

```css
.vc-container   { padding: 0; }
.vc-status-bar  { display:flex; border-bottom:1px solid #1a1a1a; padding:18px 0; }
.vc-label       { font-size:0.7rem; font-weight:800; letter-spacing:1px; text-transform:uppercase; color:#666; }
.vc-input       { background:#0a0a0a; border:1px solid #1a1a1a; border-radius:2px; color:#fff; padding:10px 14px; }
.vc-input:focus { border-color:#00ff88; box-shadow:0 0 0 1px rgba(0,255,136,0.15); }
.vc-hint        { font-size:0.72rem; color:#555; }
.vc-btn-primary { background:#00ff88; color:#000; font-weight:800; padding:10px 24px; }
.vc-btn-primary:hover { background:#00cc66; }
```

**Critical**: `.vc-btn-primary` has a `!important` override in light mode to force `background:#059669; color:#ffffff` — some ancestor rule was setting computed color to `#444` (looking disabled). If you see the Save Configuration button looking disabled in light mode, check the `!important` override in `forms.css` lines 799-803.

### Dashboard Tab Reference Patterns

- **SMS Config** (`templates/dashboard/tabs/config.html`) — gold standard for forms. Radio-card selection with `:has(input:checked)` green border state.
- **Billing** (`templates/dashboard/tabs/billing.html`) — gold standard for pricing grid. All `metallic-price--*` classes alias to green.
- **White Label Branding** (`templates/dashboard/tabs/whitelabel.html`) — gold standard for settings with live preview.
- **Agency Statistics** (`templates/dashboard/tabs/agency_kpis.html`) — gold standard for KPI dashboards. 6-up metric row + 3-up charts + leaderboards.

---

## 12. Quick Reference: Do's and Don'ts

### DO
- Use CSS custom properties for ALL colors (`var(--accent)`, `#0a0a0a`, `#1a1a1a`)
- Add `body.light-theme` (and/or `[data-theme="light"]`) overrides for every new dark-mode style
- Use the solid card pattern (`background:#0a0a0a; border:1px solid #1a1a1a`)
- Use the ghost button pattern for secondary actions
- Use semantic CSS class names
- Use FontAwesome for icons
- Test both dark AND light themes before committing
- Use Outfit for headings + UI, Inter for body, JetBrains Mono ONLY for genuine data values

### DON'T
- Write `style="..."` inline in templates (use a class)
- Use `backdrop-filter`, `blur()`, or translucent `rgba(255,255,255,0.0x)` backgrounds
- Use `--glass-*` variables in new code (legacy only)
- Hardcode `#00ff88` in HTML (use `var(--accent)`)
- Hardcode `Omnisconn` or `InsuranceGrokBot` in dashboard templates
- Introduce a new accent color (purple, orange, blue, cyan, yellow) for ANY reason except semantic state (error red, warning amber, info blue, temperature red/amber/blue/gray)
- Use gradient fills on CTAs — flat depth only
- Use JetBrains Mono for marketing subheads (retro/hacker, not luxurious)
- Create new CSS files per component (extend the existing namespace files)
- Use `!important` unless absolutely necessary (and when you do, comment why)
- Skip light-theme parity

### Test Loop Before Committing

1. Is every color in this commit `var(--accent)` or a semantic state color?
2. Does every dark-mode rule I added have a matching light-mode rule?
3. Did I use a solid hex background, not rgba?
4. Did I use a class instead of inline style?
5. Is the CTA the single primary green button on the screen, or are there competing accents?

If all five answer yes, commit.
