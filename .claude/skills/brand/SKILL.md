---
name: brand
description: InsuranceGrokBot brand identity, design system, theme rules, and visual standards. Auto-invoked when working on UI, CSS, templates, themes, colors, logos, styling, design, glassmorphism, white-label, or any visual element.
allowed-tools: Read, Glob, Grep, Edit, Write, Bash, WebFetch
---

# InsuranceGrokBot Brand Identity & Design System

**This skill is the single source of truth for all visual and brand decisions.** It MUST be followed when touching ANY UI surface — templates, CSS, JavaScript UI, marketing pages, dashboard, or white-label features.

## Trigger Words

This skill auto-activates when the task involves any of these:
- **brand**, **branding**, **logo**, **favicon**, **identity**
- **design**, **UI**, **UX**, **layout**, **visual**
- **theme**, **dark mode**, **light mode**, **light-theme**, **toggle**
- **color**, **accent**, **neon green**, **#00ff88**
- **glass**, **glassmorphism**, **liquid glass**, **frosted**, **blur**
- **CSS**, **style**, **stylesheet**, **style.css**
- **font**, **typography**, **Outfit**, **Inter**, **JetBrains Mono**
- **template**, **Jinja**, **HTML**, **dashboard.html**, **base.html**
- **card**, **panel**, **modal**, **dropdown**, **sidebar**, **topbar**
- **white-label**, **whitelabel**, **agency branding**, **custom brand**
- **icon**, **carrier**, **AT&T**, **Verizon**, **T-Mobile**
- **marketing page**, **landing page**, **home page**
- **inline style** (to block it)

---

## 1. Brand Identity

### Product Name
- **Full name**: InsuranceGrokBot
- **Tagline**: "Stop Chasing Leads. Let AI Close Them."
- **Product type**: White-label AI-powered SMS and Voice bot platform for insurance agents

### Logo (Favicon)
The logo is a **neon green robot face** — a friendly, rounded robot head with antenna, ears, eyes with white highlights, and a rectangular mouth. Located at `static/favicon.svg`.

**Logo construction:**
- 64x64 SVG viewBox
- **Gradient fill**: `#00ff88` → `#00cc66` (135deg linear gradient)
- **Antenna**: vertical line + circle on top, stroke `#00ff88`
- **Head**: rounded rectangle (`rx="12"`), gradient fill
- **Eyes**: dark circles (`#050505`) with white highlight dots
- **Mouth**: dark rounded rectangle
- **Ears**: side rounded rectangles in `#00cc66`
- **Background**: transparent (adapts to any surface)

**Usage rules:**
- Never distort aspect ratio
- Minimum size: 32x32px
- Always use the SVG for crisp rendering at any size
- On dark backgrounds, the green glows naturally
- On light backgrounds, ensure sufficient contrast

### PWA Icons
Full icon set at `static/icons/`:
- `apple-touch-icon.png`, `icon-72x72.png`, `icon-96x96.png`, `icon-128x128.png`
- `icon-144x144.png`, `icon-152x152.png`, `icon-192x192.png`
- `icon-384x384.png`, `icon-512x512.png`

---

## 2. Color System

### Dark Theme (Default)

```css
/* Primary accent — the signature neon green */
--accent:       #00ff88;
--accent-hover: #ffffff;
--accent-dim:   rgba(0, 255, 136, 0.12);

/* Secondary accent — cyan (hero gradients, highlights) */
--accent-secondary: #00d9ff;

/* Backgrounds */
--dark-bg:       #030305;
--dark-surface:  #0d0d12;
--dark-surface-2: #12121a;

/* Text hierarchy */
--text-primary:   #ffffff;
--text-secondary: #a0a0a0;
--text-muted:     #95a0be;

/* Typography scale */
--fs-xs: 0.75rem;  --fs-sm: 0.8125rem;  --fs-base: 0.875rem;
--fs-md: 1rem;     --fs-lg: 1.125rem;   --fs-xl: 1.25rem;   --fs-2xl: 1.5rem;
--lh-tight: 1.3;   --lh-normal: 1.5;    --lh-relaxed: 1.65;

/* Effects */
--glow: 0 0 30px rgba(0, 255, 136, 0.15);
--transition: all 0.4s cubic-bezier(0.25, 1, 0.5, 1);

/* Brand colors */
--agency-blue: #007AFF;
--alert: #ff4444;

/* Status colors */
--status-success: #00ff88;
--status-error:   #ef4444;
--status-warning: #f59e0b;
--status-info:    #3b82f6;

/* Temperature colors (Smart Filters) */
--temp-hot:    #ef4444;  /* red */
--temp-warm:   #f59e0b;  /* amber */
--temp-cool:   #3b82f6;  /* blue */
--temp-cold:   #6b7280;  /* gray */
```

### Light Theme (`body.light-theme`)

Activated via `body.light-theme` CSS class. Toggle in topbar (`#themeToggleBtn`), persisted in `localStorage` key `dash_theme`. JavaScript: `toggleTheme()` in `sidebar.js`.

```css
--lt-bg:             #f4f6fa;
--lt-surface:        #ffffff;
--lt-surface-hover:  #f8f9fb;
--lt-border:         rgba(0, 0, 0, 0.08);
--lt-border-strong:  rgba(0, 0, 0, 0.14);
--lt-text-primary:   #1a1a2e;
--lt-text-secondary: #4a5568;
--lt-text-muted:     #718096;
--lt-text-faint:     #a0aec0;
--lt-accent:         #059669;
--lt-accent-bg:      rgba(5, 150, 105, 0.08);
--lt-accent-border:  rgba(5, 150, 105, 0.2);
--lt-info:           #2563eb;
--lt-input-bg:       #f7f8fa;
--lt-input-border:   #d2d6dc;
--lt-input-text:     #1a1a2e;

/* Light theme overrides the accent to darker green for readability */
--accent: #059669;
--accent-dim: rgba(5, 150, 105, 0.12);

/* Light Liquid Glass (overrides dark glass vars) */
--glass-bg: rgba(255, 255, 255, 0.55);
--glass-bg-strong: rgba(255, 255, 255, 0.72);
--glass-border: rgba(255, 255, 255, 0.75);
--glass-blur: blur(40px) saturate(180%) brightness(1.04) contrast(1.02);
--glass-shadow: 0 16px 48px rgba(31,38,135,0.10), inset 0 1.5px 0 rgba(255,255,255,0.98);
--glass-shadow-hover: 0 24px 64px rgba(31,38,135,0.16), inset 0 1.5px 0 rgba(255,255,255,1);

/* Light theme background gradient */
background: linear-gradient(135deg, #f0f4ff 0%, #e8ecf8 50%, #f0f0f8 100%);
background-attachment: fixed;
```

**Rule**: Every dark-mode style MUST have a `body.light-theme` counterpart. No exceptions.

### Light Theme Coverage (Comprehensive)
The light theme has overrides for ALL dashboard surfaces:
- Sidebar, topbar, glass panels, form inputs, dropdowns
- Dialer columns, contact rows, message bubbles, date separators
- iPhone/iOS UI (nav bar, thread scroll, composer, tab bar, call log, voicemail)
- Billing plan cards, pricing cards, metallic price text
- Discord panel, Slack panel, modals, tooltips, toasts
- Smart Filters, AI intelligence panels, workflow tabs
- Scrollbars, carrier chips, CRM buttons, advanced settings toggleles
- All inline dark backgrounds/text colors overridden via attribute selectors

---

## 3. Typography

### Font Stack

| Font | Weight | Usage |
|------|--------|-------|
| **Outfit** | 300, 400, 500, 700, 800, 900 | Headings, UI labels, buttons, marketing copy |
| **Inter** | 300, 400, 500, 600 | Body text, sidebar, topbar, form inputs |
| **JetBrains Mono** | 400, 500 | Code, technical values, API keys, phone numbers, logs |

### Google Fonts Import (use in `<head>`)
```html
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;700;800&family=Inter:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
```

### Font Assignment
```css
body              { font-family: 'Inter', 'Outfit', sans-serif; }
h1, h2, h3        { font-family: 'Outfit', sans-serif; }
.sidebar, .topbar  { font-family: 'Inter', 'Outfit', sans-serif; }
.tech-value, code  { font-family: 'JetBrains Mono', monospace; }
```

### Hero Text Gradient
```css
.hero-title {
    background: radial-gradient(135deg, var(--accent), #00d9ff);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    font-size: clamp(2.6rem, 5vw, 4rem);
    font-weight: 900;
    font-family: 'Outfit', sans-serif;
}
```

---

## 4. Liquid Glass Design System

### Core Concept

Liquid Glass simulates a physical pane of frosted glass lit from the top-left. It combines:
- **`backdrop-filter`** for blur + saturation (the "frosted" part)
- **Layered `rgba` backgrounds** for translucency
- **Asymmetric borders** — top/left edges are brighter (catch the light), bottom/right are darker (in shadow)
- **`inset` box-shadows** to simulate glass thickness and edge glow

### CSS Custom Properties

All glass values defined in `:root` in `static/css/style.css`:

```css
/* Background gradients */
--glass-bg:         linear-gradient(135deg, rgba(255,255,255,0.08) 0%, rgba(255,255,255,0.02) 100%);
--glass-bg-strong:  linear-gradient(135deg, rgba(255,255,255,0.12) 0%, rgba(255,255,255,0.04) 100%);

/* Blur filter */
--glass-blur:       blur(20px) saturate(160%) brightness(1.1);

/* 3D Edge Lighting — top/left bright, bottom darker */
--glass-border-top:    rgba(255,255,255,0.25);
--glass-border-left:   rgba(255,255,255,0.15);
--glass-border-bottom: rgba(255,255,255,0.05);
--glass-border:        rgba(255,255,255,0.15);   /* legacy alias */

/* Depth shadows */
--glass-shadow:       0 16px 32px -8px rgba(0,0,0,0.5), inset 0 1px 1px rgba(255,255,255,0.15);
--glass-shadow-hover: 0 24px 48px -12px rgba(0,255,136,0.15), inset 0 1px 2px rgba(255,255,255,0.25);
```

### Canonical Glass Pattern

Apply this to ANY card, panel, dropdown, or modal:

```css
.my-glass-element {
    background: var(--glass-bg);
    backdrop-filter: var(--glass-blur);
    -webkit-backdrop-filter: var(--glass-blur);
    border-width: 1px;
    border-style: solid;
    border-color: var(--glass-border-top) var(--glass-border-bottom) var(--glass-border-bottom) var(--glass-border-left);
    /* shorthand: top  right  bottom  left */
    border-radius: 12px;
    box-shadow: var(--glass-shadow);
}
.my-glass-element:hover {
    background: var(--glass-bg-strong);
    box-shadow: var(--glass-shadow-hover);
}
```

### Light Theme Glass Override

```css
body.light-theme .my-glass-element {
    background: rgba(255,255,255,0.85);
    border-color: rgba(0,0,0,0.10) rgba(0,0,0,0.06) rgba(0,0,0,0.06) rgba(0,0,0,0.08);
    box-shadow: 0 8px 24px rgba(0,0,0,0.08), inset 0 1px 0 rgba(255,255,255,0.9);
}
```

### Reference Implementation

The `Choices.js` custom select overrides in `static/css/style.css` (search for `/* Choices.js */`) are the canonical gold-standard implementation of a fully-themed Liquid Glass interactive component with dark and light variants. Model any new interactive components after that section.

---

## 5. NO INLINE STYLING — Mandatory Rule

**Inline styles are strictly prohibited in all Jinja2/HTML templates.**

### Why
Inline `style=""` attributes bypass the CSS variable system. They cannot respond to `body.light-theme` toggling, causing elements to stay dark-colored in light mode. The extensive `[style*="background:#1a1a2e"]`-type attribute selectors in `style.css` are band-aid workarounds for this exact problem.

### The Rule

> **Never write `style="..."` on any element in any `.html` template file.**

Instead:
1. Add a semantic CSS class to the element (e.g. `class="stat-card"`)
2. Define the visual properties for that class in `static/css/style.css`
3. Add the light-theme override inside `body.light-theme { }` in the same CSS file

### Only Exception
JavaScript-driven dynamic values that cannot be known at render time:
```html
<div class="progress-fill" style="width: 0%"></div>
```
```js
el.style.width = score + '%';  // JS sets it dynamically
```

### Migrating Existing Inline Styles
When editing a template that has inline styles:
1. Extract the inline properties into a new class in `static/css/style.css`
2. Replace `style="..."` with `class="..."`
3. Add corresponding `body.light-theme .your-class` overrides
4. Remove the corresponding `[style*="..."]` band-aid selectors from `style.css`

---

## 6. Carrier Brand Assets

### Carrier Logos (for spam protection, A2P, Voice Integrity UI)

| Carrier | SVG | PNG | Usage |
|---------|-----|-----|-------|
| AT&T | `static/img/carriers/att.svg` | — | Spam protection dashboard, carrier registration |
| T-Mobile | `static/img/carriers/tmobile.svg` | `static/img/carriers/tmobile.png`, `static/images/icons8-t-mobile-48.png` | Spam protection, number health |
| Verizon | `static/img/carriers/verizon.svg` | — | Spam protection dashboard |

**Additional T-Mobile assets:**
- `static/images/T-Mobile_logo_2022.svg` — official 2022 logo
- `static/images/t-mobile-svgrepo-com.svg` — alternate SVG

**Usage rules:**
- Prefer SVG over PNG for crisp rendering
- Use `static/img/carriers/` directory for new carrier logos
- Display at consistent size within carrier chip/badge components
- On dark backgrounds: logos need sufficient contrast (add subtle white glow if needed)
- On light backgrounds: logos display naturally

### CRM Integration Logos
Referenced in marketing pages for: GoHighLevel, Salesforce, HubSpot, Pipedrive, Zoho, Insureio

---

## 7. Icons

### FontAwesome
Primary icon library throughout the app. Used for:
- Navigation: `fa-phone`, `fa-sms`, `fa-chart-bar`, `fa-cog`, `fa-users`
- Status: `fa-check-circle`, `fa-exclamation-triangle`, `fa-times-circle`
- Actions: `fa-play`, `fa-pause`, `fa-stop`, `fa-microphone`
- AI Intelligence: `fa-fire` (hot), `fa-thermometer-half` (warm), `fa-snowflake` (cold)
- Theme toggle: `fa-sun` (light), `fa-moon` (dark) in `#themeToggleBtn`

### Product Screenshots
Located in `static/images/`:
- `dialer-overview.png` — dialer UI screenshot
- `call-connected.png`, `call-ringing.png` — call states
- `number-health.png` — number health dashboard
- `spam-protection.png` — spam protection UI
- `activity-log.png` — activity log screenshot
- `carriers-grid.png` — carrier selection grid
- `a2p-registration.png` — A2P registration flow

---

## 8. White-Label System

Agency owners can fully replace the InsuranceGrokBot brand with their own.

### What's Customizable

| Field | Type | UI Component |
|-------|------|-------------|
| `company_name` | String (max 100) | Text input |
| `logo_url` | URL (max 500) | File upload + URL paste |
| `name_font` | Google Font name | Select dropdown (10 fonts) |
| `name_bold` | Boolean | Toggle button |
| `name_italic` | Boolean | Toggle button |
| `name_underline` | Boolean | Toggle button |

**Available fonts**: Default (System), Inter, Roboto, Poppins, Montserrat, Open Sans, Lato, Oswald, Raleway, Playfair Display, Merriweather

**Logo specs**: Recommended 200x60px, max 400x120px, max 2MB. PNG/SVG/JPG/WebP. Transparent background recommended.

### Implementation
- **Settings UI**: `dashboard/tabs/whitelabel.html` (icon: `fa-palette`)
- **JavaScript**: `static/js/dashboard/whitelabel.js` — functions: `wlInit()`, `wlToggleStyle()`, `wlUpdatePreview()`, `wlSave()`
- **API**: `POST /api/agency/whitelabel`
- **Storage**: `agency_billing.whitelabel_config` JSONB column
- **Sidebar logo**: `.sb-brand-logo` (32x32px container) — shows custom logo or falls back to `favicon.svg`
- **Sidebar name**: `.sb-brand-text` — shows custom company name or "IGB"
- **CSS injection**: On dashboard load, CSS custom properties overridden dynamically
- **Scope**: Only logged-in dashboard experience. Marketing pages stay InsuranceGrokBot branded.

### Marketing Site Logo (NOT white-labeled)
```html
<a class="navbar-brand" href="/">Insurance<span class="text-accent">Grok</span>Bot</a>
```
- "Insurance" in white, "Grok" in neon green (`var(--accent)`), "Bot" in white
- Font: Outfit, weight 800, size 1.5rem, letter-spacing -0.5px

### White-Label Rules for Development
1. Never hardcode `InsuranceGrokBot` in dashboard templates — use a template variable or CSS
2. Never hardcode `#00ff88` in dashboard HTML — always use `var(--accent)`
3. All accent-colored elements must use CSS variables so white-label overrides work
4. Marketing pages (`/`, `/for-agencies`, etc.) are NOT white-labeled

---

## 9. Layout & Spacing

### Key CSS Variables
```css
--sidebar-width:           195px;
--sidebar-collapsed-width: 47px;
--topbar-height:           39px;
--safe-top:                env(safe-area-inset-top, 15px);
--safe-bottom:             env(safe-area-inset-bottom, 15px);
```

### Dashboard Structure
```
┌─────────────────────────────────────────────┐
│ Topbar (fixed, glass)                        │
├──────┬──────────────────────────────────────┤
│      │                                       │
│ Side │  Main Content Area                    │
│ bar  │  (tab panes)                          │
│      │                                       │
│      │                                       │
├──────┴──────────────────────────────────────┤
```

### Border Radius Scale
- Small (inputs, chips): `8px`
- Medium (cards, panels): `12px`
- Large (modals, dropdowns): `16px`
- Extra-large (hero cards, pricing): `20-24px`

### Standard Transition
```css
transition: all 0.2s ease;
```

---

## 10. Marketing Page Aesthetic

### Style: Dark Cyberpunk SaaS
- Deep black backgrounds with subtle radial gradients
- Neon green (#00ff88) → cyan (#00d9ff) accent gradients
- Glassmorphic pricing cards with backdrop-filter blur
- Large bold Outfit typography (up to 900 weight)
- Phone mockups with animated SMS conversation bubbles
- Statistics blocks with large numerals
- CRM integration chip badges

### SEO Article Components
All articles use these CSS classes:
- `.article-page` — page layout
- `.article-section` — content sections
- `.article-highlight-box` — callout boxes
- `.article-stat-row` — statistic displays
- `.article-cta-box` — call-to-action blocks

### Social Links
- **Facebook**: `https://www.facebook.com/profile.php?id=61587536844180`
- **LinkedIn**: `https://www.linkedin.com/company/insurancegrokbot`

---

## Quick Reference: Do's and Don'ts

### DO
- Use CSS custom properties for ALL colors (`var(--accent)`, `var(--glass-bg)`)
- Add `body.light-theme` overrides for every new dark-mode style
- Use Liquid Glass pattern for new panels/cards/modals
- Use FontAwesome for icons
- Use `static/css/style.css` for all styling
- Use semantic CSS class names
- Test both dark AND light themes

### DON'T
- Write `style="..."` inline in templates (EVER)
- Hardcode `#00ff88` in HTML (use `var(--accent)`)
- Hardcode `InsuranceGrokBot` in dashboard templates
- Create new CSS files (extend `style.css`)
- Use non-standard fonts without adding to the Google Fonts import
- Skip light-theme overrides
- Use `!important` unless absolutely necessary
