# A2P 10DLC Redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split A2P 10DLC from a payment-gated wizard into two separate Brand and Campaign panels with form-first UX, auto-populated fields, info tooltips, and pay-at-submit Stripe integration.

**Architecture:** Sidebar gets L3 sub-items (Brand, Campaign) under A2P 10DLC. Each panel is a separate view in the existing config.html, rendered by new JS functions in numbers.js. Two new Stripe checkout endpoints replace the combined payment. Existing Twilio API calls (`register-brand`, `create-campaign`) unchanged.

**Tech Stack:** Flask (Stripe endpoints), Jinja2 (sidebar + panels), vanilla JS (form logic, tooltips, Stripe redirect), Stripe Checkout (one-time payments), existing CSS patterns.

---

## File Map

**Modify:**
- `templates/dashboard/_sidebar.html` — Add L3 Brand + Campaign sub-items under A2P 10DLC
- `templates/dashboard/tabs/config.html` — Replace A2P wizard section with Brand panel + Campaign panel HTML
- `static/js/dashboard/numbers.js` — Rewrite A2P functions: separate brand/campaign renders, auto-populate, tooltips, Stripe redirect, status polling
- `blueprints/billing.py` — Add `/a2p/brand-checkout` and `/a2p/campaign-checkout` endpoints, update webhook handler
- `voice/a2p.py` — Remove `a2p_fee_paid` gate from `register-brand` and `create-campaign`

**Create:**
- `static/css/dashboard/a2p.css` — A2P form styles, info tooltips, status cards (dark + light theme)

---

## Phase 1: Backend — Stripe Endpoints + Gate Removal

### Task 1.1: Split Stripe Checkout into Brand + Campaign

**Files:**
- Modify: `blueprints/billing.py`

- [ ] **Step 1: Add brand-checkout endpoint**

Add after the existing `a2p_checkout` function (keep the old one for backward compat):

```python
@billing_bp.route("/a2p/brand-checkout", methods=["POST"])
@login_required
def a2p_brand_checkout():
    """Create Stripe session for A2P brand registration fee only."""
    data = request.get_json(silent=True) or {}
    brand_type = (data.get("brand_type") or "LOW_VOLUME").upper().strip()

    fee_info = A2P_FEE_SCHEDULE.get(brand_type)
    if not fee_info:
        return flask_jsonify({"error": f"Unknown brand type: {brand_type}"}), 400

    brand_cents = fee_info["brand_fee"]
    label = fee_info["label"]

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="payment",
            customer_email=current_user.email,
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "unit_amount": brand_cents,
                    "product_data": {
                        "name": f"A2P Brand Registration — {label}",
                        "description": f"Brand vetting fee (${brand_cents / 100:.2f})",
                    },
                },
                "quantity": 1,
            }],
            metadata={
                "purchase_type": "a2p_brand",
                "user_email": current_user.email,
                "brand_type": brand_type,
                "total_cents": str(brand_cents),
            },
            success_url=f"{YOUR_DOMAIN}/dashboard?a2p_brand_paid=1",
            cancel_url=f"{YOUR_DOMAIN}/dashboard?a2p_brand_cancel=1",
        )
        return flask_jsonify({"checkout_url": session.url})
    except Exception as e:
        logger.error(f"A2P brand checkout error: {e}")
        return flask_jsonify({"error": "Unable to create checkout session."}), 500


@billing_bp.route("/a2p/campaign-checkout", methods=["POST"])
@login_required
def a2p_campaign_checkout():
    """Create Stripe session for A2P campaign vetting fee ($15)."""
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="payment",
            customer_email=current_user.email,
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "unit_amount": 1500,
                    "product_data": {
                        "name": "A2P Campaign Registration",
                        "description": "Campaign vetting fee ($15.00)",
                    },
                },
                "quantity": 1,
            }],
            metadata={
                "purchase_type": "a2p_campaign",
                "user_email": current_user.email,
                "total_cents": "1500",
            },
            success_url=f"{YOUR_DOMAIN}/dashboard?a2p_campaign_paid=1",
            cancel_url=f"{YOUR_DOMAIN}/dashboard?a2p_campaign_cancel=1",
        )
        return flask_jsonify({"checkout_url": session.url})
    except Exception as e:
        logger.error(f"A2P campaign checkout error: {e}")
        return flask_jsonify({"error": "Unable to create checkout session."}), 500
```

- [ ] **Step 2: Update Stripe webhook handler for new purchase types**

In the existing `checkout.session.completed` handler in `billing.py`, add handling for `a2p_brand` and `a2p_campaign` purchase types alongside the existing `a2p_registration`:

```python
elif purchase_type == "a2p_brand":
    # Brand fee paid
    a2p = vc.get('a2p', {})
    a2p['brand_fee_paid'] = True
    a2p['brand_fee_paid_at'] = datetime.utcnow().isoformat()
    a2p['brand_stripe_session_id'] = session_id
    a2p['paid_brand_type'] = meta.get('brand_type', '')
    a2p['paid_brand_cents'] = int(meta.get('total_cents', 0))
    vc['a2p'] = a2p
    # Save
    cur.execute("UPDATE subscribers SET voice_config = %s WHERE email = %s",
                (json.dumps(vc), user_email))
    conn.commit()
    logger.info(f"[Stripe] A2P brand fee paid for {user_email}")

elif purchase_type == "a2p_campaign":
    a2p = vc.get('a2p', {})
    a2p['campaign_fee_paid'] = True
    a2p['campaign_fee_paid_at'] = datetime.utcnow().isoformat()
    a2p['campaign_stripe_session_id'] = session_id
    vc['a2p'] = a2p
    cur.execute("UPDATE subscribers SET voice_config = %s WHERE email = %s",
                (json.dumps(vc), user_email))
    conn.commit()
    logger.info(f"[Stripe] A2P campaign fee paid for {user_email}")
```

- [ ] **Step 3: Commit**

```bash
git add blueprints/billing.py
git commit -m "feat(a2p): separate brand + campaign Stripe checkout endpoints"
```

### Task 1.2: Remove Payment Gate from A2P Registration Endpoints

**Files:**
- Modify: `voice/a2p.py`

- [ ] **Step 1: Update register-brand to accept either old or new payment flag**

In `register_brand()` (around line 372-380), replace the hard `a2p_fee_paid` gate with a check that accepts either the old combined flag OR the new per-step flag:

```python
# Payment gate: accept old combined flag OR new per-step brand flag
# Agency owners and admins bypass payment requirement
is_admin = current_user.email in ADMIN_EMAILS
is_agency_owner = bool(subscriber.get('parent_agency_email') is None and subscriber.get('agency_billing_id'))
a2p = vc.get('a2p', {})
brand_paid = a2p.get('a2p_fee_paid') or a2p.get('brand_fee_paid') or is_admin or is_agency_owner
if is_sub_user and not brand_paid:
    return jsonify({'error': 'Brand registration fee required', 'payment_required': True}), 402
```

- [ ] **Step 2: Update create-campaign similarly**

Same pattern — accept old `a2p_fee_paid` OR new `campaign_fee_paid`:

```python
a2p = vc.get('a2p', {})
campaign_paid = a2p.get('a2p_fee_paid') or a2p.get('campaign_fee_paid') or is_admin or is_agency_owner
if is_sub_user and not campaign_paid:
    return jsonify({'error': 'Campaign registration fee required', 'payment_required': True}), 402
```

- [ ] **Step 3: Update status endpoint to return new payment flags**

In `a2p_status()`, add to the response:

```python
"brand_fee_paid": a2p.get('a2p_fee_paid') or a2p.get('brand_fee_paid', False),
"campaign_fee_paid": a2p.get('a2p_fee_paid') or a2p.get('campaign_fee_paid', False),
```

- [ ] **Step 4: Commit**

```bash
git add voice/a2p.py
git commit -m "feat(a2p): accept per-step payment flags, backward compat with combined flag"
```

---

## Phase 2: Sidebar Navigation

### Task 2.1: Add L3 Sub-Items for Brand + Campaign

**Files:**
- Modify: `templates/dashboard/_sidebar.html`

- [ ] **Step 1: Replace single A2P item with expandable sub-menu**

Replace line 41 (the single A2P 10DLC item):

```html
<div class="l2-item" onclick="activateL2(this); sidebarNavigate('a2p-10dlc', null);">A2P 10DLC</div>
```

With an L2 accordion containing L3 items:

```html
<div class="l2-acc" onclick="toggleL2(this)">A2P 10DLC <i class="fa-solid fa-chevron-right nav-chevron"></i></div>
<div class="l2-body">
    <div class="l3-item" onclick="activateL3(this); sidebarNavigate('a2p-brand', null);">Brand</div>
    <div class="l3-item" onclick="activateL3(this); sidebarNavigate('a2p-campaign', null);">Campaign</div>
</div>
```

- [ ] **Step 2: Verify L2/L3 accordion CSS and JS already exist**

Check that `toggleL2()`, `activateL3()`, `.l2-acc`, `.l2-body`, `.l3-item` are already defined in the sidebar CSS/JS. If not, add them following the existing L1/L2 pattern.

- [ ] **Step 3: Commit**

```bash
git add templates/dashboard/_sidebar.html
git commit -m "feat(a2p): sidebar L3 sub-items for Brand and Campaign"
```

---

## Phase 3: Frontend — Brand + Campaign Panels

### Task 3.1: Create A2P CSS

**Files:**
- Create: `static/css/dashboard/a2p.css`

- [ ] **Step 1: Write A2P panel styles**

CSS classes for:
- `.a2p-panel` — panel container
- `.a2p-create-btn` — "Create New Brand" / "Create New Campaign" primary CTA
- `.a2p-form` — form wrapper (hidden by default, shown on create click)
- `.a2p-field-group` — form field with label
- `.a2p-input`, `.a2p-select`, `.a2p-textarea` — form inputs following Omnisconn brand
- `.a2p-info-btn` — small (i) circle icon next to textarea labels
- `.a2p-info-tooltip` — tooltip popup with example text + copy button
- `.a2p-status-card` — registration status display (pending/approved/failed variants)
- `.a2p-status-badge` — status pill badge
- `.a2p-fee-display` — price display next to brand type selector
- `.a2p-submit-btn` — submit + pay button
- `.a2p-disabled-panel` — grayed-out state for Campaign when no brand

All with `body.light-theme` overrides. Solid backgrounds, single accent, no glassmorphism.

- [ ] **Step 2: Add CSS import to _head.html**

```html
<link rel="stylesheet" href="{{ url_for('static', filename='css/dashboard/a2p.css') }}?v={{ _sv }}">
```

- [ ] **Step 3: Commit**

```bash
git add static/css/dashboard/a2p.css templates/dashboard/_head.html
git commit -m "feat(a2p): luxury A2P panel CSS with dark+light themes"
```

### Task 3.2: Replace A2P HTML in config.html

**Files:**
- Modify: `templates/dashboard/tabs/config.html`

- [ ] **Step 1: Replace old A2P wizard section**

Replace the entire A2P section (lines ~255-490 containing `a2pPaymentGate`, `a2pBrandForm`, `a2pCampaignForm`, `a2pBrandStatusPanel`) with two clean panel containers:

```html
<!-- A2P Brand Panel -->
<div id="a2p-brand-panel" class="cfg-panel" style="display:none;">
    <div class="vc-status-bar">
        <div class="vc-status-left">
            <i class="fa-solid fa-tag"></i>
            <span class="vc-status-label">A2P 10DLC</span>
            <span class="vc-status-value">Brand Registration</span>
        </div>
        <div class="vc-status-right">
            <span class="a2p-status-badge" id="a2pBrandBadge"></span>
        </div>
    </div>
    <div id="a2pBrandContent"></div>
</div>

<!-- A2P Campaign Panel -->
<div id="a2p-campaign-panel" class="cfg-panel" style="display:none;">
    <div class="vc-status-bar">
        <div class="vc-status-left">
            <i class="fa-solid fa-bullhorn"></i>
            <span class="vc-status-label">A2P 10DLC</span>
            <span class="vc-status-value">Campaign Registration</span>
        </div>
        <div class="vc-status-right">
            <span class="a2p-status-badge" id="a2pCampaignBadge"></span>
        </div>
    </div>
    <div id="a2pCampaignContent"></div>
</div>
```

Both panels are JS-rendered (content divs populated by `numbers.js`).

- [ ] **Step 2: Update sidebarNavigate to handle new panel IDs**

In the sidebar navigation JS, ensure `a2p-brand` shows `a2p-brand-panel` and `a2p-campaign` shows `a2p-campaign-panel`. This likely means adding cases to `switchVoicePanel()` or `switchConfigPanel()` in the existing nav code.

- [ ] **Step 3: Commit**

```bash
git add templates/dashboard/tabs/config.html
git commit -m "feat(a2p): replace A2P wizard with Brand + Campaign panel containers"
```

### Task 3.3: Rewrite A2P JavaScript

**Files:**
- Modify: `static/js/dashboard/numbers.js`

- [ ] **Step 1: Replace entire A2P section (lines ~1904-2400)**

Remove the old `a2pLoadStatus`, `a2pRenderUI`, `a2pRenderBrandStatus`, `a2pUpdateStepPills`, `a2pRegisterBrand`, `a2pSubmitCampaign`, `a2pPayFee`, etc.

Replace with new functions:

**Brand Panel Functions:**

```javascript
// State
var _a2pData = null;

async function a2pBrandInit() {
    // Fetch status + trust_hub data for auto-populate
    const [statusResp, spResp] = await Promise.all([
        fetch('/voice/a2p/status'),
        fetch('/voice/spam-protection/status'),
    ]);
    const status = await statusResp.json();
    const sp = await spResp.json();
    _a2pData = { status, trustHub: sp };
    _a2pRenderBrandPanel(status, sp);
}

function _a2pRenderBrandPanel(d, sp) {
    const el = document.getElementById('a2pBrandContent');
    // Update badge
    _a2pUpdateBadge('a2pBrandBadge', d.brand_status);

    if (d.brand_sid && d.brand_status) {
        // Show status card (pending/approved/failed)
        el.innerHTML = _a2pBrandStatusCard(d);
        if (d.brand_status === 'PENDING') _a2pStartBrandPoll();
        return;
    }
    // No brand — show "Create New Brand" button
    el.innerHTML = '<div class="a2p-create-section">' +
        '<p class="a2p-create-desc">Register your business with carriers to send SMS from your phone numbers.</p>' +
        '<button class="a2p-create-btn" onclick="a2pShowBrandForm()"><i class="fa-solid fa-plus me-2"></i>Create New Brand</button>' +
        '</div>';
}

function a2pShowBrandForm() {
    const el = document.getElementById('a2pBrandContent');
    const sp = _a2pData.trustHub || {};
    // Build form HTML with auto-populated fields from trust_hub
    // ... (full form with all fields, brand type selector with pricing, submit button)
}
```

**Campaign Panel Functions:**

```javascript
async function a2pCampaignInit() {
    if (!_a2pData) {
        const r = await fetch('/voice/a2p/status');
        _a2pData = { status: await r.json() };
    }
    _a2pRenderCampaignPanel(_a2pData.status);
}

function _a2pRenderCampaignPanel(d) {
    const el = document.getElementById('a2pCampaignContent');
    _a2pUpdateBadge('a2pCampaignBadge', d.campaign_status);

    // No brand or brand not approved — disabled state
    if (!d.brand_sid || d.brand_status !== 'APPROVED') {
        var msg = !d.brand_sid
            ? 'Register and get your Brand approved before creating a Campaign.'
            : 'Brand is under review. Campaign registration will unlock once approved.';
        el.innerHTML = '<div class="a2p-disabled-panel"><i class="fa-solid fa-lock me-2"></i>' + msg + '</div>';
        return;
    }

    if (d.campaign_sid && d.campaign_status) {
        el.innerHTML = _a2pCampaignStatusCard(d);
        if (['PENDING', 'IN_PROGRESS'].includes(d.campaign_status)) _a2pStartCampaignPoll();
        return;
    }
    // Brand approved, no campaign — show create button
    el.innerHTML = '<div class="a2p-create-section">' +
        '<p class="a2p-create-desc">Create a messaging campaign and link your phone numbers.</p>' +
        '<button class="a2p-create-btn" onclick="a2pShowCampaignForm()"><i class="fa-solid fa-plus me-2"></i>Create New Campaign</button>' +
        '</div>';
}
```

**Info Tooltip Function:**

```javascript
function _a2pInfoTooltip(text) {
    // Returns HTML for (i) icon that shows tooltip on click
    var id = 'a2pTip_' + Math.random().toString(36).substr(2, 6);
    return '<button class="a2p-info-btn" onclick="event.preventDefault();a2pToggleTip(\'' + id + '\')">' +
        '<i class="fa-solid fa-circle-info"></i></button>' +
        '<div class="a2p-info-tooltip" id="' + id + '" style="display:none;">' +
        '<div class="a2p-info-tooltip-text">' + _esc(text) + '</div>' +
        '<button class="a2p-info-copy-btn" onclick="navigator.clipboard.writeText(\'' +
        text.replace(/'/g, "\\'") + '\');this.textContent=\'Copied!\'">Copy</button>' +
        '</div>';
}

function a2pToggleTip(id) {
    var el = document.getElementById(id);
    if (!el) return;
    // Close all other tooltips
    document.querySelectorAll('.a2p-info-tooltip').forEach(t => { if (t.id !== id) t.style.display = 'none'; });
    el.style.display = el.style.display === 'none' ? 'block' : 'none';
}
```

**Stripe Submit Flows:**

```javascript
async function a2pSubmitBrand() {
    // 1. Validate form fields
    // 2. Save form data to sessionStorage
    // 3. POST to /a2p/brand-checkout with brand_type
    // 4. Redirect to Stripe
}

async function a2pSubmitCampaign() {
    // 1. Validate form fields (description 40+, 2+ samples 20+, phone selected)
    // 2. Save form data to sessionStorage
    // 3. POST to /a2p/campaign-checkout
    // 4. Redirect to Stripe
}

// On page load: check URL params for payment success redirects
function a2pCheckPaymentRedirect() {
    var params = new URLSearchParams(window.location.search);
    if (params.get('a2p_brand_paid') === '1') {
        // Retrieve form data from sessionStorage, submit to /voice/a2p/register-brand
        var formData = JSON.parse(sessionStorage.getItem('a2p_brand_form') || '{}');
        if (formData.business_name) {
            _a2pSubmitBrandToTwilio(formData);
        }
        history.replaceState({}, '', '/dashboard');
    }
    if (params.get('a2p_campaign_paid') === '1') {
        var formData = JSON.parse(sessionStorage.getItem('a2p_campaign_form') || '{}');
        if (formData.description) {
            _a2pSubmitCampaignToTwilio(formData);
        }
        history.replaceState({}, '', '/dashboard');
    }
}
```

**Sample Message Tooltip Content (constants):**

```javascript
var _A2P_SAMPLE_MESSAGES = [
    'Hi {First Name}, this is {Agent Name} with {Business Name}. I wanted to follow up on your interest in life insurance coverage. Do you have a few minutes to chat about your options?',
    'Hey {First Name}, just checking in — I put together some coverage options based on what we discussed. When works best for a quick call to go over them?',
    'Hi {First Name}, this is {Agent Name}. I noticed you were looking into life insurance options. I\'d love to help you find the right coverage for your family. Is now a good time?',
    'Hi {First Name}, friendly reminder about our appointment tomorrow at {Time}. Looking forward to helping you find the right coverage. Reply YES to confirm or let me know if you need to reschedule.',
];

var _A2P_DESCRIPTION_EXAMPLE = 'We send personalized text messages to leads who have expressed interest in life insurance coverage. Messages include policy information, appointment reminders, and follow-up communications.';

var _A2P_MESSAGE_FLOW_EXAMPLE = 'Leads opt in by submitting a contact form on our website which includes SMS consent language. They can opt out at any time by replying STOP.';
```

- [ ] **Step 2: Wire sidebar navigation to new init functions**

In the `sidebarNavigate` handler, add:
- `a2p-brand` → show `a2p-brand-panel`, call `a2pBrandInit()`
- `a2p-campaign` → show `a2p-campaign-panel`, call `a2pCampaignInit()`

- [ ] **Step 3: Add payment redirect check on dashboard load**

Call `a2pCheckPaymentRedirect()` on page load (alongside existing redirect checks).

- [ ] **Step 4: Expose all functions on window**

```javascript
window.a2pBrandInit = a2pBrandInit;
window.a2pCampaignInit = a2pCampaignInit;
window.a2pShowBrandForm = a2pShowBrandForm;
window.a2pShowCampaignForm = a2pShowCampaignForm;
window.a2pSubmitBrand = a2pSubmitBrand;
window.a2pSubmitCampaign = a2pSubmitCampaign;
window.a2pToggleTip = a2pToggleTip;
window.a2pCheckPaymentRedirect = a2pCheckPaymentRedirect;
```

- [ ] **Step 5: Commit**

```bash
git add static/js/dashboard/numbers.js
git commit -m "feat(a2p): rewrite A2P JS — brand/campaign panels, auto-populate, tooltips, Stripe flow"
```

---

## Phase 4: Final Wiring + Push

### Task 4.1: Integration Test + Deploy

- [ ] **Step 1: Syntax check all modified Python files**

```bash
python -c "import py_compile; py_compile.compile('blueprints/billing.py', doraise=True); py_compile.compile('voice/a2p.py', doraise=True); print('All OK')"
```

- [ ] **Step 2: Verify JS functions exist**

```bash
python -c "
js = open('static/js/dashboard/numbers.js', 'r', encoding='utf-8').read()
for fn in ['a2pBrandInit', 'a2pCampaignInit', 'a2pShowBrandForm', 'a2pShowCampaignForm',
           'a2pSubmitBrand', 'a2pSubmitCampaign', 'a2pToggleTip', 'a2pCheckPaymentRedirect',
           '_A2P_SAMPLE_MESSAGES', '_A2P_DESCRIPTION_EXAMPLE']:
    assert fn in js, f'Missing: {fn}'
print('All A2P functions present')
"
```

- [ ] **Step 3: Push to main**

```bash
git push origin main
```
