/**
 * InsuranceGrokBot GHL Extension — Config (3 of 3)
 * Spam protection, business profile, number management,
 * voice integrity, A2P 10DLC, voice config, SMS bot config.
 * Fully self-contained — no dependencies on other files.
 */

// Shared infrastructure — guarded so only the first loaded file initializes
if (typeof igbServerUrl === 'undefined') {

var igbServerUrl = 'https://insurancegrokbot.click';
var igbCallPollMs = 2000;
var igbRefreshIntervalMs = 60000;
var igbMaxVisibleToasts = 3;

var igbKey = '';
var igbKeyExpiresAt = 0;
var igbLocationId = '';
var igbSubscriptionTier = 'individual';
var igbSubscribed = false;
var igbMaxDialLines = 1;
var igbBalanceRefreshTimer = null;
var igbStatsRefreshTimer = null;
var igbDialerPopupEl = null;
var igbCallQueue = [];
var igbCallQueueIndex = 0;
var igbActiveCallMap = new Map();
var igbCallPollTimer = null;
var igbListenSocket = null;
var igbCurrentCallSid = '';
var igbCurrentCallMode = '';
var igbToastList = [];
var igbCurrentAiMinutes = 0;
var igbConfigPanelOpen = false;
var igbConfigSection = 'overview';

function igbMakeElement(tagName, cssClass, htmlContent) {
    const el = document.createElement(tagName);
    if (cssClass) {
        el.className = cssClass;
    }
    if (htmlContent) {
        el.innerHTML = htmlContent;
    }
    return el;
}
function igbFind(selector, root) {
    return (root || document).querySelector(selector);
}
function igbFindAll(selector, root) {
    return (root || document).querySelectorAll(selector);
}
function igbLog(message) {
}
function igbFormatNumber(num) {
    return Number(num).toLocaleString('en-US');
}
function igbFormatDuration(totalSeconds) {
    if (!totalSeconds) {
        return '0:00';
    }
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = Math.floor(totalSeconds % 60);
    return `${minutes}:${seconds < 10 ? '0' : ''}${seconds}`;
}
function igbSafeText(rawText) {
    const div = document.createElement('div');
    div.textContent = rawText;
    return div.innerHTML;
}
function igbCapitalize(str) {
    return str ? str.charAt(0).toUpperCase() + str.slice(1) : '';
}
// Extract contact ID from the current GHL URL path
function igbGetContactIdFromUrl() {
    const match = window.location.pathname.match(/\/contacts\/(?:detail\/)?([a-zA-Z0-9]+)/);
    return match ? match[1] : '';
}

// Make an authenticated API request to the IGB server, auto-refreshing token on 401
async function igbApiRequest(httpMethod, apiPath, requestBody) {
    const fetchOptions = {
        method: httpMethod,
        headers: {
            'Authorization': 'Bearer ' + igbKey,
            'Content-Type': 'application/json',
        },
    };
    if (requestBody) {
        fetchOptions.body = JSON.stringify(requestBody);
    }
    let response = await fetch(igbServerUrl + apiPath, fetchOptions);
    if (response.status === 401) {
        igbLog('Session expired, refreshing');
        await igbAuthenticate();
        fetchOptions.headers['Authorization'] = 'Bearer ' + igbKey;
        response = await fetch(igbServerUrl + apiPath, fetchOptions);
    }
    const data = await response.json();
    if (response.status === 402 && data.subscription_required) {
        igbLog('Subscription required - showing upgrade prompt');
        igbSubscribed = false;
        igbShowUpgradePrompt();
        throw new Error('subscription_required');
    }
    return data;
}

// Authenticate with IGB server using GHL location ID and user token
async function igbAuthenticate() {
    try {
        igbLocationId = await igbGetLocationId();
        if (!igbLocationId) {
            igbLog('Could not determine location ID');
            return false;
        }
        const requestPayload = {
            location_id: igbLocationId,
            timestamp: Math.floor(Date.now() / 1000),
        };
        const userAccess = await igbGetGhlUserAccess();
        if (userAccess) {
            requestPayload.ghl_token = userAccess;
        }
        const resp = await fetch(igbServerUrl + '/api/ghl/auth/token', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(requestPayload),
        });
        const data = await resp.json();
        if (data.token) {
            igbKey = data.token;
            igbSubscriptionTier = data.tier || 'individual';
            igbSubscribed = data.subscribed !== false;
            igbKeyExpiresAt = Date.now() + (data.expires_in || 7200) * 1000;
            igbLog('Authenticated, tier: ' + igbSubscriptionTier + ', subscribed: ' + igbSubscribed);
            return true;
        }
        igbLog('Auth failed: ' + (data.error || 'unknown'));
        return false;
    } catch (authError) {
        igbLog('Auth error: ' + authError.message);
        return false;
    }
}

// Get the current GHL location ID from the AppUtils SDK
async function igbGetLocationId() {
    if (typeof AppUtils !== 'undefined' && AppUtils.Utilities) {
        try {
            const locationData = await AppUtils.Utilities.getCurrentLocation();
            return locationData.id || locationData.locationId || '';
        } catch (e) {
            igbLog('Could not get location from AppUtils: ' + e);
        }
    }
    return '';
}

// Try to get a GHL user access token or shared secret for auth
async function igbGetGhlUserAccess() {
    if (typeof AppUtils !== 'undefined' && AppUtils.Utilities) {
        try {
            if (AppUtils.Utilities.getUserToken) {
                return await AppUtils.Utilities.getUserToken();
            }
            if (AppUtils.Utilities.getSharedSecret) {
                return await AppUtils.Utilities.getSharedSecret();
            }
        } catch (e) {
        }
    }
    return '';
}
// Check if the current auth token is still valid (with 60s buffer)
function igbIsKeyValid() {
    return igbKey && Date.now() < igbKeyExpiresAt - 60000;
}

function igbShowToast(message, toastType) {
    toastType = toastType || 'info';
    const toast = igbMakeElement('div', `igb-toast igb-toast-${toastType}`);
    const messageSpan = document.createElement('span');
    messageSpan.textContent = message;
    toast.appendChild(messageSpan);
    const closeBtn = document.createElement('button');
    closeBtn.className = 'igb-toast-close';
    closeBtn.textContent = 'x';
    closeBtn.addEventListener('click', () => toast.remove());
    toast.appendChild(closeBtn);
    let container = igbFind('#igb-toast-container');
    if (!container) {
        container = igbMakeElement('div', '');
        container.id = 'igb-toast-container';
        document.body.appendChild(container);
    }
    container.appendChild(toast);
    igbToastList.push(toast);
    while (igbToastList.length > igbMaxVisibleToasts) {
        const oldToast = igbToastList.shift();
        if (oldToast.parentElement) {
            oldToast.remove();
        }
    }
    setTimeout(() => {
        toast.classList.add('igb-toast-fade');
        setTimeout(() => toast.remove(), 300);
    }, 5000);
}

// Insert a chip element into the GHL top navigation bar
function igbInjectIntoTopNav(element) {
    const navSelectors = [
        'nav [class*="right-section"]',
        'header [class*="actions"]',
        '[class*="topbar"] [class*="right"]',
        '.hl_topbar .right-section',
        'nav.hl_topbar',
    ];
    let targetEl = null;
    for (let sIdx = 0; sIdx < navSelectors.length; sIdx++) {
        targetEl = igbFind(navSelectors[sIdx]);
        if (targetEl) { break; }
    }
    if (targetEl) {
        targetEl.insertBefore(element, targetEl.firstChild);
    } else {
        let chipContainer = igbFind('#igb-floating-chips');
        if (!chipContainer) {
            chipContainer = igbMakeElement('div', 'igb-floating-chips');
            chipContainer.id = 'igb-floating-chips';
            document.body.appendChild(chipContainer);
        }
        chipContainer.appendChild(element);
    }
}

} // end shared infrastructure guard

// Reusable CSS class name strings for the config panel form elements.
var igbCssLabel = 'igb-cfg-label';
var igbCssInput = 'igb-cfg-input';
var igbCssSelect = 'igb-cfg-input igb-cfg-select';
var igbCssTextarea = 'igb-cfg-input igb-cfg-textarea';
var igbCssRow = 'igb-cfg-row';
var igbCssCol = 'igb-cfg-col';
var igbCssSaveBtn = 'igb-cfg-btn-action igb-cfg-btn-save';

// Render the shield chip in the top nav showing protection status
async function igbRenderConfigChip() {
    const existing = igbFind('#igb-config-chip');
    if (existing) { existing.remove(); }
    let statusData = {};
    try {
        statusData = await igbApiRequest('GET', '/api/ghl/spam-protection/status');
    } catch (e) { return; }
    const chip = igbMakeElement('div', 'igb-chip igb-config-chip');
    chip.id = 'igb-config-chip';
    const active = statusData.protection_active;
    chip.classList.add(active ? 'igb-chip-green' : 'igb-chip-yellow');
    chip.innerHTML = '<i class="fa-solid fa-shield-halved"></i> ' + (active ? 'Protected' : 'Setup');
    chip.title = 'Spam Protection & Config';
    chip.addEventListener('click', igbToggleConfigPanel);
    igbInjectIntoTopNav(chip);
}
function igbToggleConfigPanel() {
    const existing = igbFind('#igb-config-panel');
    if (existing) { existing.remove(); igbConfigPanelOpen = false; return; }
    igbConfigPanelOpen = true;
    igbConfigSection = 'overview';
    igbBuildConfigPanel();
}
function igbBuildConfigPanel() {
    const existing = igbFind('#igb-config-panel');
    if (existing) { existing.remove(); }
    const panel = igbMakeElement('div', 'igb-cfg-panel');
    panel.id = 'igb-config-panel';
    const header = igbMakeElement('div', 'igb-cfg-header');
    header.innerHTML = '<i class="fa-solid fa-shield-halved"></i> Spam Protection & Config';
    const closeBtn = document.createElement('button');
    closeBtn.className = 'igb-panel-close';
    closeBtn.textContent = 'x';
    closeBtn.addEventListener('click', () => { panel.remove(); igbConfigPanelOpen = false; });
    header.appendChild(closeBtn);
    panel.appendChild(header);
    const layout = igbMakeElement('div', 'igb-cfg-layout');
    const sidebar = igbMakeElement('div', 'igb-cfg-sidebar');
    const menuSections = [
        { key: 'overview',   label: 'Overview',           icon: 'fa-gauge-high' },
        { key: 'activate',   label: 'Voice Account',      icon: 'fa-tower-broadcast' },
        { key: 'numbers',    label: 'Buy Numbers',        icon: 'fa-cart-shopping' },
        { key: 'business',   label: 'Business Profile',   icon: 'fa-building' },
        { key: 'spam',       label: 'Spam Protection',    icon: 'fa-shield-check' },
        { key: 'integrity',  label: 'Number Integrity',   icon: 'fa-fingerprint' },
        { key: 'health',     label: 'Number Health',      icon: 'fa-heart-pulse' },
        { key: 'a2p',        label: 'A2P 10DLC',          icon: 'fa-message' },
        { key: 'voice',      label: 'Voice Config',       icon: 'fa-microphone' },
        { key: 'sms',        label: 'SMS Bot Config',     icon: 'fa-robot' },
    ];
    menuSections.forEach((sec) => {
        const item = igbMakeElement('button', 'igb-cfg-menu-item');
        if (sec.key === igbConfigSection) { item.classList.add('active'); }
        item.innerHTML = '<i class="fa-solid ' + sec.icon + '"></i><span>' + sec.label + '</span>';
        item.addEventListener('click', () => { igbConfigSection = sec.key; igbBuildConfigPanel(); });
        sidebar.appendChild(item);
    });
    const siteLink = igbMakeElement('a', 'igb-cfg-site-link', '<i class="fa-solid fa-arrow-up-right-from-square"></i> More info on the website');
    siteLink.href = igbServerUrl;
    siteLink.target = '_blank';
    siteLink.rel = 'noopener';
    sidebar.appendChild(siteLink);
    layout.appendChild(sidebar);
    const content = igbMakeElement('div', 'igb-cfg-content');
    content.id = 'igb-cfg-content';
    layout.appendChild(content);
    panel.appendChild(layout);
    document.body.appendChild(panel);
    igbLoadConfigSection(igbConfigSection, content);
}
async function igbLoadConfigSection(section, container) {
    container.innerHTML = '<div class="igb-cfg-loading"><i class="fa-solid fa-circle-notch fa-spin"></i> Loading...</div>';
    try {
        const renderers = {
            overview: igbSecOverview, activate: igbSecActivate, numbers: igbSecNumbers,
            business: igbSecBusiness, spam: igbSecSpam, integrity: igbSecIntegrity,
            health: igbSecHealth, a2p: igbSecA2p, voice: igbSecVoice, sms: igbSecSms,
        };
        await (renderers[section] || igbSecOverview)(container);
    } catch (e) {
        container.innerHTML = '<div class="igb-cfg-error"><i class="fa-solid fa-triangle-exclamation"></i> Failed to load. Try again.</div>';
    }
}
function igbInfoRow(label, value) {
    return '<div class="igb-cfg-info-label">' + igbSafeText(label) + '</div><div class="igb-cfg-info-value">' + igbSafeText(value || '-') + '</div>';
}
function igbBadge(text, ok) {
    return '<div class="igb-cfg-badge ' + (ok ? 'igb-badge-ok' : 'igb-badge-pending') + '">'
        + '<i class="fa-solid ' + (ok ? 'fa-circle-check' : 'fa-circle-xmark') + '"></i> ' + igbSafeText(text) + '</div>';
}
function igbDashLink(label, tab, section) {
    let url = igbServerUrl + '/dashboard?tab=' + tab;
    if (section) { url += '&section=' + section; }
    return '<a class="igb-cfg-dash-link" href="' + url + '" target="_blank" rel="noopener"><i class="fa-solid fa-arrow-up-right-from-square"></i> ' + igbSafeText(label) + '</a>';
}
// Config panel section: Overview with status checklist
async function igbSecOverview(c) {
    const [status, vc] = await Promise.all([
        igbApiRequest('GET', '/api/ghl/spam-protection/status'),
        igbApiRequest('GET', '/api/ghl/voice-config'),
    ]);
    c.innerHTML = '';
    const voiceCard = igbMakeElement('div', 'igb-cfg-card');
    voiceCard.innerHTML = '<div class="igb-cfg-card-title"><i class="fa-solid fa-tower-broadcast"></i> Voice Account</div>'
        + (vc.voice_activated
            ? '<div class="igb-cfg-card-status igb-cfg-ok"><i class="fa-solid fa-circle-check"></i> Active</div>'
            : '<div class="igb-cfg-card-status igb-cfg-warn"><i class="fa-solid fa-circle-xmark"></i> Not Activated</div>');
    voiceCard.addEventListener('click', () => { igbConfigSection = 'activate'; igbBuildConfigPanel(); });
    c.appendChild(voiceCard);
    const numCard = igbMakeElement('div', 'igb-cfg-card');
    numCard.innerHTML = '<div class="igb-cfg-card-title"><i class="fa-solid fa-phone"></i> Phone Numbers</div>'
        + '<div class="igb-cfg-card-status">' + status.numbers_total + ' number' + (status.numbers_total !== 1 ? 's' : '') + '</div>';
    numCard.addEventListener('click', () => { igbConfigSection = 'numbers'; igbBuildConfigPanel(); });
    c.appendChild(numCard);
    const checkTitle = igbMakeElement('div', 'igb-cfg-section-title', 'Protection Status');
    c.appendChild(checkTitle);
    const checks = [
        { label: 'Business Profile', ok: status.protection_active, go: 'business' },
        { label: 'CNAM (Caller ID Name)', ok: status.cnam.registered, go: 'spam' },
        { label: 'Number Integrity', ok: status.number_integrity.registered, go: 'integrity' },
        { label: 'A2P 10DLC (SMS)', ok: status.a2p.registered, go: 'a2p' },
    ];
    const list = igbMakeElement('div', 'igb-cfg-checklist');
    checks.forEach((ch) => {
        const row = igbMakeElement('div', 'igb-cfg-check-row');
        row.innerHTML = '<i class="fa-solid ' + (ch.ok ? 'fa-circle-check igb-cfg-ok' : 'fa-circle igb-cfg-muted') + '"></i> <span>' + ch.label + '</span>';
        row.addEventListener('click', () => { igbConfigSection = ch.go; igbBuildConfigPanel(); });
        list.appendChild(row);
    });
    c.appendChild(list);
    const cfgTitle = igbMakeElement('div', 'igb-cfg-section-title', 'Configuration');
    c.appendChild(cfgTitle);
    const cfgLinks = igbMakeElement('div', 'igb-cfg-checklist');
    [
        { label: 'Voice AI Settings', go: 'voice' },
        { label: 'SMS Bot Settings', go: 'sms' },
    ].forEach((lnk) => {
        const row = igbMakeElement('div', 'igb-cfg-check-row');
        row.innerHTML = '<i class="fa-solid fa-gear igb-cfg-muted"></i> <span>' + lnk.label + '</span>';
        row.addEventListener('click', () => { igbConfigSection = lnk.go; igbBuildConfigPanel(); });
        cfgLinks.appendChild(row);
    });
    c.appendChild(cfgLinks);
    if (!status.has_sub_account) {
        const notice = igbMakeElement('div', 'igb-cfg-notice');
        notice.innerHTML = '<i class="fa-solid fa-info-circle"></i> Activate your voice account first to unlock all features.';
        c.appendChild(notice);
    }
}
// Config panel section: Voice account activation
async function igbSecActivate(c) {
    const vc = await igbApiRequest('GET', '/api/ghl/voice-config');
    c.innerHTML = '';
    const title = igbMakeElement('div', 'igb-cfg-section-title', 'Voice Account');
    c.appendChild(title);
    if (vc.voice_activated) {
        c.innerHTML += igbBadge('Voice Account Active', true);
        const info = igbMakeElement('div', 'igb-cfg-info-grid');
        info.innerHTML = igbInfoRow('Phone Number', vc.has_phone_number ? 'Provisioned' : 'None yet')
            + igbInfoRow('Calling Hours', (vc.calling_hours_start || '08:00') + ' -' + (vc.calling_hours_end || '21:00'));
        c.appendChild(info);
        const desc = igbMakeElement('div', 'igb-cfg-desc', 'Your voice account is active. Buy numbers below, then configure spam protection to improve answer rates.');
        c.appendChild(desc);
    } else {
        const desc = igbMakeElement('div', 'igb-cfg-desc', 'Activate your voice account to start making AI and human calls. This provisions a dedicated Twilio sub-account for your phone numbers.');
        c.appendChild(desc);
        const activateBtn = igbMakeElement('button', 'igb-cfg-btn-action', '<i class="fa-solid fa-bolt"></i> Activate Voice Account');
        activateBtn.addEventListener('click', async () => {
            activateBtn.disabled = true;
            activateBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Activating...';
            try {
                await igbApiRequest('POST', '/api/ghl/voice/activate');
                igbShowToast('Voice account activated!', 'success');
                igbSecActivate(c);
            } catch (e) {
                igbShowToast('Activation failed: ' + (e.message || 'unknown error'), 'error');
                activateBtn.disabled = false;
                activateBtn.innerHTML = '<i class="fa-solid fa-bolt"></i> Activate Voice Account';
            }
        });
        c.appendChild(activateBtn);
    }
}
// Config panel section: Phone number management and purchase
async function igbSecNumbers(c) {
    c.innerHTML = '';
    const title = igbMakeElement('div', 'igb-cfg-section-title', 'Phone Numbers');
    c.appendChild(title);
    let numbersData = {};
    try { numbersData = await igbApiRequest('GET', '/api/ghl/numbers'); }
    catch (e) { c.appendChild(igbMakeElement('div', 'igb-cfg-error', 'Failed to load numbers.')); return; }
    const numbers = numbersData.numbers || [];
    const freeRemaining = numbersData.free_remaining || 0;
    const countBar = igbMakeElement('div', 'igb-cfg-count-bar');
    countBar.innerHTML = '<span>' + numbers.length + ' number' + (numbers.length !== 1 ? 's' : '') + '</span>'
        + '<span class="igb-cfg-muted">' + freeRemaining + ' of 5 free remaining</span>';
    c.appendChild(countBar);
    if (numbers.length > 0) {
        const list = igbMakeElement('div', 'igb-cfg-num-list');
        numbers.forEach((n) => {
            const row = igbMakeElement('div', 'igb-cfg-num-row');
            row.innerHTML = '<div class="igb-cfg-num-phone">' + igbSafeText(n.phone) + '</div>'
                + '<div class="igb-cfg-num-name">' + igbSafeText(n.friendly_name || '') + '</div>';
            const delBtn = igbMakeElement('button', 'igb-cfg-num-del', '<i class="fa-solid fa-trash-can"></i>');
            delBtn.title = 'Release number';
            delBtn.addEventListener('click', async (ev) => {
                ev.stopPropagation();
                if (!confirm('Release ' + n.phone + '? This cannot be undone.')) { return; }
                delBtn.disabled = true;
                try {
                    await igbApiRequest('POST', '/api/ghl/numbers/release', { sid: n.sid });
                    igbShowToast('Released ' + n.phone, 'success');
                    igbSecNumbers(c);
                    igbRenderConfigChip();
                } catch (err) { igbShowToast('Release failed', 'error'); delBtn.disabled = false; }
            });
            row.appendChild(delBtn);
            list.appendChild(row);
        });
        c.appendChild(list);
    }
    const searchTitle = igbMakeElement('div', 'igb-cfg-section-title igb-cfg-divider', 'Search Available Numbers');
    c.appendChild(searchTitle);
    const form = igbMakeElement('div', 'igb-cfg-search-form');
    form.innerHTML = '<input type="text" class="' + igbCssInput + '" id="igb-ns-area" placeholder="Area code" maxlength="6">'
        + '<select class="' + igbCssSelect + '" id="igb-ns-type"><option value="local">Local ($0.90/mo)</option><option value="toll_free">Toll-Free ($2.15/mo)</option></select>'
        + '<button class="igb-cfg-btn-action igb-cfg-btn-sm" id="igb-ns-btn"><i class="fa-solid fa-magnifying-glass"></i> Search</button>';
    c.appendChild(form);
    const results = igbMakeElement('div', 'igb-cfg-search-results');
    results.id = 'igb-ns-results';
    c.appendChild(results);
    form.querySelector('#igb-ns-btn').addEventListener('click', async () => {
        const areaCode = form.querySelector('#igb-ns-area').value.trim();
        const numType = form.querySelector('#igb-ns-type').value;
        const btn = form.querySelector('#igb-ns-btn');
        btn.disabled = true;
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
        results.innerHTML = '';
        try {
            let path = '/api/ghl/numbers/search?number_type=' + numType;
            if (areaCode) { path += '&area_code=' + encodeURIComponent(areaCode); }
            const sr = await igbApiRequest('GET', path);
            btn.disabled = false;
            btn.innerHTML = '<i class="fa-solid fa-magnifying-glass"></i> Search';
            const found = sr.numbers || [];
            if (found.length === 0) { results.innerHTML = '<div class="igb-cfg-empty">No numbers found. Try a different area code.</div>'; return; }
            found.forEach((num) => {
                const row = igbMakeElement('div', 'igb-cfg-num-result');
                row.innerHTML = '<div><div class="igb-cfg-num-phone">' + igbSafeText(num.phone) + '</div>'
                    + '<div class="igb-cfg-num-loc">' + igbSafeText([num.locality, num.region].filter(Boolean).join(', ')) + '</div></div>';
                const buyBtn = igbMakeElement('button', 'igb-cfg-btn-buy', freeRemaining > 0 ? 'Free' : 'Buy');
                buyBtn.addEventListener('click', async () => {
                    buyBtn.disabled = true; buyBtn.textContent = '...';
                    try {
                        await igbApiRequest('POST', '/api/ghl/numbers/buy', { phone_number: num.phone, number_type: numType });
                        igbShowToast('Purchased ' + num.phone, 'success');
                        buyBtn.textContent = 'OK'; buyBtn.classList.add('igb-cfg-btn-owned');
                        igbRenderConfigChip();
                    } catch (err) {
                        if (err.message && err.message.indexOf('402') >= 0) {
                            igbShowToast('Free allowance used. Purchase via the IGB Dashboard.', 'error');
                            buyBtn.textContent = '$';
                        } else { igbShowToast('Purchase failed', 'error'); }
                        buyBtn.disabled = false;
                    }
                });
                row.appendChild(buyBtn);
                results.appendChild(row);
            });
        } catch (e) {
            btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-magnifying-glass"></i> Search';
            results.innerHTML = '<div class="igb-cfg-error">Search failed.</div>';
        }
    });
}
async function igbSecBusiness(c) {
    const data = await igbApiRequest('GET', '/api/ghl/trust-hub');
    c.innerHTML = '';
    const title = igbMakeElement('div', 'igb-cfg-section-title', 'Business Profile');
    c.appendChild(title);
    const bp = data.business_profile || {};
    if (bp.registered) {
        c.innerHTML += igbBadge('Registered', true);
        const info = igbMakeElement('div', 'igb-cfg-info-grid');
        [['Business', bp.business_name], ['EIN', bp.ein],
         ['Address', [bp.street, bp.city, bp.state, bp.zip].filter(Boolean).join(', ')],
         ['Contact', bp.contact_name], ['Email', bp.contact_email], ['Phone', bp.contact_phone]]
            .forEach(([l, v]) => { if (v) { info.innerHTML += igbInfoRow(l, v); } });
        c.appendChild(info);
    } else {
        c.appendChild(igbMakeElement('div', 'igb-cfg-desc', 'Register your business profile with Twilio Trust Hub for carrier verification. Required for CNAM, Number Integrity, and A2P.'));
        c.innerHTML += igbDashLink('Register Business Profile', 'voice', 'spam-protection');
    }
    const cnam = data.cnam || {};
    const cnamTitle = igbMakeElement('div', 'igb-cfg-section-title igb-cfg-divider', 'CNAM (Caller ID Name)');
    c.appendChild(cnamTitle);
    if (cnam.registered) {
        c.innerHTML += igbBadge(cnam.status || 'Registered', true);
        const ci = igbMakeElement('div', 'igb-cfg-info-grid');
        ci.innerHTML = igbInfoRow('Display Name', cnam.display_name) + igbInfoRow('Status', cnam.status);
        c.appendChild(ci);
    } else {
        c.appendChild(igbMakeElement('div', 'igb-cfg-desc', 'Show your business name on caller ID instead of "Unknown Caller." Registered as part of Spam Protection.'));
        c.innerHTML += igbDashLink('Set Up CNAM', 'voice', 'spam-protection');
    }
}
async function igbSecSpam(c) {
    const data = await igbApiRequest('GET', '/api/ghl/spam-protection/status');
    c.innerHTML = '';
    const title = igbMakeElement('div', 'igb-cfg-section-title', 'Spam Protection');
    c.appendChild(title);
    c.appendChild(igbMakeElement('div', 'igb-cfg-desc',
        'One-click spam protection registers your business with carrier spam databases '
        + '(AT&T/Hiya, T-Mobile, Verizon) and sets up CNAM caller ID. '
        + 'This improves call answer rates and prevents "Spam Likely" labels.'));
    if (data.protection_active) {
        c.innerHTML += igbBadge('Protected', true);
        const info = igbMakeElement('div', 'igb-cfg-info-grid');
        info.innerHTML = igbInfoRow('Business', data.business_name)
            + igbInfoRow('CNAM', data.cnam.registered ? (data.cnam.display_name || 'Active') : 'Not set')
            + igbInfoRow('Numbers', data.numbers_total + ' on account');
        c.appendChild(info);
    } else {
        c.innerHTML += igbBadge('Not Protected', false);
        c.innerHTML += igbDashLink('Set Up Spam Protection', 'voice', 'spam-protection');
    }
}
async function igbSecIntegrity(c) {
    const data = await igbApiRequest('GET', '/api/ghl/number-integrity/status');
    c.innerHTML = '';
    const title = igbMakeElement('div', 'igb-cfg-section-title', 'Number Integrity');
    c.appendChild(title);
    c.appendChild(igbMakeElement('div', 'igb-cfg-desc',
        'Voice Integrity registers your numbers with carrier spam analytics engines '
        + 'to remediate spam labels and improve answer rates. '
        + 'Separate from A2P (which is for SMS).'));
    if (data.registered) {
        c.innerHTML += igbBadge(data.status || 'Registered', true);
        const info = igbMakeElement('div', 'igb-cfg-info-grid');
        info.innerHTML = igbInfoRow('Status', data.status) + igbInfoRow('Business', data.business_name)
            + igbInfoRow('Numbers Assigned', data.assigned_count) + igbInfoRow('Registered', data.registered_at);
        c.appendChild(info);
    } else {
        c.innerHTML += igbBadge('Not Registered', false);
        c.innerHTML += igbDashLink('Register for Voice Integrity', 'voice', 'number-integrity');
    }
}
async function igbSecHealth(c) {
    const data = await igbApiRequest('GET', '/api/ghl/number-health');
    c.innerHTML = '';
    const title = igbMakeElement('div', 'igb-cfg-section-title', 'Number Health');
    c.appendChild(title);
    const summary = data.summary || {};
    const stats = igbMakeElement('div', 'igb-cfg-health-grid');
    [['Avg Score', summary.avg_health_score || 0], ['Active', summary.active || 0],
     ['Resting', summary.resting || 0], ['Frozen', summary.frozen || 0]]
        .forEach(([lbl, val]) => {
            stats.innerHTML += '<div class="igb-cfg-health-stat"><div class="igb-cfg-health-num">' + val + '</div><div class="igb-cfg-health-lbl">' + lbl + '</div></div>';
        });
    c.appendChild(stats);
    const rot = igbMakeElement('div', 'igb-cfg-rotation');
    rot.innerHTML = '<span>Smart Number Rotation</span><span class="igb-cfg-rotation-val ' + (data.rotation_enabled ? 'igb-cfg-ok' : '') + '">' + (data.rotation_enabled ? 'ON' : 'OFF') + '</span>';
    c.appendChild(rot);
    const numbers = data.numbers || [];
    if (numbers.length === 0) {
        c.appendChild(igbMakeElement('div', 'igb-cfg-empty', 'No health data yet. Data appears after calls are made.'));
        return;
    }
    const list = igbMakeElement('div', 'igb-cfg-num-list');
    numbers.forEach((n) => {
        const score = n.health_score || 0;
        const cls = score >= 80 ? 'igb-hs-good' : score >= 50 ? 'igb-hs-warn' : 'igb-hs-bad';
        const row = igbMakeElement('div', 'igb-cfg-num-row');
        row.innerHTML = '<div class="igb-cfg-num-phone">' + igbSafeText(n.phone) + (n.nickname ? ' <span class="igb-cfg-muted">(' + igbSafeText(n.nickname) + ')</span>' : '') + '</div>'
            + '<div class="igb-cfg-num-meta"><span class="igb-cfg-hs ' + cls + '">' + score + '</span>'
            + '<span class="igb-cfg-ns igb-cfg-ns-' + n.status + '">' + igbCapitalize(n.status) + '</span></div>';
        list.appendChild(row);
    });
    c.appendChild(list);
}
async function igbSecA2p(c) {
    const data = await igbApiRequest('GET', '/api/ghl/a2p/status');
    c.innerHTML = '';
    const title = igbMakeElement('div', 'igb-cfg-section-title', 'A2P 10DLC (SMS Compliance)');
    c.appendChild(title);
    c.appendChild(igbMakeElement('div', 'igb-cfg-desc',
        'A2P 10DLC registers your brand and use case with mobile carriers for business SMS. '
        + 'This is optional -your SMS bot uses GHL by default. '
        + 'Register if you want to send SMS directly through your own Twilio numbers.'));
    if (data.registered) {
        c.innerHTML += igbBadge('Registered', true);
        const info = igbMakeElement('div', 'igb-cfg-info-grid');
        info.innerHTML = igbInfoRow('Brand Status', data.brand_status) + igbInfoRow('Campaign Status', data.campaign_status)
            + igbInfoRow('Registered', data.registered_at);
        c.appendChild(info);
    } else {
        c.innerHTML += igbBadge('Not Registered', false);
        c.appendChild(igbMakeElement('div', 'igb-cfg-desc', 'Your SMS bot currently sends through GHL (GoHighLevel). A2P registration is only needed if you switch to direct Twilio SMS.'));
        c.innerHTML += igbDashLink('Set Up A2P Registration', 'voice', 'a2p');
    }
}
// Config panel section: Voice AI settings form
async function igbSecVoice(c) {
    const vc = await igbApiRequest('GET', '/api/ghl/voice-config');
    c.innerHTML = '';
    const title = igbMakeElement('div', 'igb-cfg-section-title', 'Voice AI Configuration');
    c.appendChild(title);
    if (!vc.voice_activated) {
        c.appendChild(igbMakeElement('div', 'igb-cfg-notice', '<i class="fa-solid fa-info-circle"></i> Activate your voice account first.'));
        return;
    }
    const form = igbMakeElement('div', 'igb-cfg-form');
    form.innerHTML += '<label class="' + igbCssLabel + '">Voice Greeting</label>'
        + '<textarea class="' + igbCssTextarea + '" id="igb-vc-greeting" rows="2" maxlength="500">' + igbSafeText(vc.voice_greeting || '') + '</textarea>';
    form.innerHTML += '<label class="' + igbCssLabel + '">AI Personality / Instructions</label>'
        + '<textarea class="' + igbCssTextarea + '" id="igb-vc-instructions" rows="3" maxlength="2000">' + igbSafeText(vc.voice_instructions || '') + '</textarea>';
    form.innerHTML += '<label class="' + igbCssLabel + '">Transfer Number</label>'
        + '<input class="' + igbCssInput + '" id="igb-vc-transfer" type="text" value="' + igbSafeText(vc.transfer_number || '') + '" placeholder="+15551234567" maxlength="20">';
    form.innerHTML += '<div class="' + igbCssRow + '">'
        + '<div class="' + igbCssCol + '">'
        + '<label class="' + igbCssLabel + '">Calling Hours Start</label>'
        + '<input class="' + igbCssInput + '" id="igb-vc-hrs-start" type="time" value="'
        + (vc.calling_hours_start || '08:00') + '">'
        + '</div>'
        + '<div class="' + igbCssCol + '">'
        + '<label class="' + igbCssLabel + '">Calling Hours End</label>'
        + '<input class="' + igbCssInput + '" id="igb-vc-hrs-end" type="time" value="'
        + (vc.calling_hours_end || '21:00') + '">'
        + '</div>'
        + '</div>';
    form.innerHTML += '<label class="' + igbCssLabel + '">On Voicemail Action</label>'
        + '<select class="' + igbCssSelect + '" id="igb-vc-machine">'
        + '<option value="hangup"' + (vc.on_machine_action === 'hangup' ? ' selected' : '') + '>Hang Up</option>'
        + '<option value="voicemail_drop"' + (vc.on_machine_action === 'voicemail_drop' ? ' selected' : '') + '>Leave Voicemail</option>'
        + '<option value="continue"' + (vc.on_machine_action === 'continue' ? ' selected' : '') + '>Continue (AI talks to machine)</option>'
        + '</select>';
    form.innerHTML += '<div class="' + igbCssRow + '">'
        + '<div class="' + igbCssCol + '">'
        + '<label class="' + igbCssLabel + '">Max Call Duration (sec)</label>'
        + '<input class="' + igbCssInput + '" id="igb-vc-maxdur" type="number" min="30" max="3600" value="'
        + (vc.max_call_duration || 300) + '">'
        + '</div>'
        + '<div class="' + igbCssCol + '">'
        + '<label class="' + igbCssLabel + '">Wrap-Up Time (sec)</label>'
        + '<input class="' + igbCssInput + '" id="igb-vc-wrapup" type="number" min="0" max="120" value="'
        + (vc.wrap_up_time || 15) + '">'
        + '</div>'
        + '</div>';
    form.innerHTML += '<div class="' + igbCssRow + '">'
        + '<div class="' + igbCssCol + '">'
        + '<label class="' + igbCssLabel + '">Same # Cooldown (hrs)</label>'
        + '<input class="' + igbCssInput + '" id="igb-vc-cooldown" type="number" min="0" max="72" value="'
        + (vc.same_number_cooldown_hours || 4) + '">'
        + '</div>'
        + '<div class="' + igbCssCol + '">'
        + '<label class="' + igbCssLabel + '">Daily Max Per Contact</label>'
        + '<input class="' + igbCssInput + '" id="igb-vc-dailymax" type="number" min="0" max="10" value="'
        + (vc.same_contact_daily_max || 3) + '">'
        + '</div>'
        + '</div>';
    form.innerHTML += '<button class="' + igbCssSaveBtn + '" id="igb-vc-save"><i class="fa-solid fa-floppy-disk"></i> Save Voice Config</button>';
    c.appendChild(form);
    c.querySelector('#igb-vc-save').addEventListener('click', async () => {
        const saveBtn = c.querySelector('#igb-vc-save');
        saveBtn.disabled = true;
        saveBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Saving...';
        try {
            await igbApiRequest('POST', '/api/ghl/voice-config', {
                voice_greeting: c.querySelector('#igb-vc-greeting').value,
                voice_instructions: c.querySelector('#igb-vc-instructions').value,
                transfer_number: c.querySelector('#igb-vc-transfer').value,
                calling_hours_start: c.querySelector('#igb-vc-hrs-start').value,
                calling_hours_end: c.querySelector('#igb-vc-hrs-end').value,
                on_machine_action: c.querySelector('#igb-vc-machine').value,
                max_call_duration: parseInt(c.querySelector('#igb-vc-maxdur').value, 10),
                wrap_up_time: parseInt(c.querySelector('#igb-vc-wrapup').value, 10),
                same_number_cooldown_hours: parseInt(c.querySelector('#igb-vc-cooldown').value, 10),
                same_contact_daily_max: parseInt(c.querySelector('#igb-vc-dailymax').value, 10),
            });
            igbShowToast('Voice config saved', 'success');
        } catch (e) { igbShowToast('Save failed', 'error'); }
        saveBtn.disabled = false;
        saveBtn.innerHTML = '<i class="fa-solid fa-floppy-disk"></i> Save Voice Config';
    });
}
// Config panel section: SMS bot configuration form
async function igbSecSms(c) {
    const data = await igbApiRequest('GET', '/api/ghl/bot-config');
    c.innerHTML = '';
    const title = igbMakeElement('div', 'igb-cfg-section-title', 'SMS Bot Configuration');
    c.appendChild(title);
    const form = igbMakeElement('div', 'igb-cfg-form');
    form.innerHTML += '<label class="' + igbCssLabel + '">Bot Name (appears as sender)</label>'
        + '<input class="' + igbCssInput + '" id="igb-sc-name" type="text" value="' + igbSafeText(data.bot_first_name || '') + '" placeholder="Grok" maxlength="100">';
    form.innerHTML += '<label class="' + igbCssLabel + '">Timezone</label>'
        + '<input class="' + igbCssInput + '" id="igb-sc-tz" type="text" value="' + igbSafeText(data.timezone || 'America/Chicago') + '" placeholder="America/Chicago" maxlength="50">';
    form.innerHTML += '<label class="' + igbCssLabel + '">SMS Send Via</label>'
        + '<select class="' + igbCssSelect + '" id="igb-sc-via">'
        + '<option value="ghl"' + (data.sms_send_via === 'ghl' || !data.sms_send_via ? ' selected' : '') + '>GHL (GoHighLevel) -Default</option>'
        + '<option value="twilio"' + (data.sms_send_via && data.sms_send_via.startsWith('+') ? ' selected' : '') + '>Twilio (Direct) -Requires A2P</option>'
        + '</select>';
    c.appendChild(igbMakeElement('div', 'igb-cfg-desc',
        'Your SMS bot sends through GHL by default -no extra setup needed. '
        + 'Switch to Twilio only if you need direct number control '
        + 'with A2P 10DLC registration.'));
    form.innerHTML += '<label class="' + igbCssLabel + '">Initial Greeting Message</label>'
        + '<textarea class="' + igbCssTextarea + '" id="igb-sc-init" rows="2" maxlength="500">' + igbSafeText(data.initial_message || '') + '</textarea>';
    form.innerHTML += '<label class="' + igbCssLabel + '">Personal Website (optional)</label>'
        + '<input class="' + igbCssInput + '" id="igb-sc-web" type="text" value="' + igbSafeText(data.personal_website || '') + '" placeholder="https://..." maxlength="255">';
    form.innerHTML += '<button class="' + igbCssSaveBtn + '" id="igb-sc-save"><i class="fa-solid fa-floppy-disk"></i> Save SMS Config</button>';
    c.appendChild(form);
    c.querySelector('#igb-sc-save').addEventListener('click', async () => {
        const saveBtn = c.querySelector('#igb-sc-save');
        saveBtn.disabled = true;
        saveBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Saving...';
        const viaSelect = c.querySelector('#igb-sc-via').value;
        try {
            await igbApiRequest('POST', '/api/ghl/bot-config', {
                bot_first_name: c.querySelector('#igb-sc-name').value,
                timezone: c.querySelector('#igb-sc-tz').value,
                sms_send_via: viaSelect === 'twilio' ? '+0' : 'ghl',
                initial_message: c.querySelector('#igb-sc-init').value,
                personal_website: c.querySelector('#igb-sc-web').value,
            });
            igbShowToast('SMS config saved', 'success');
        } catch (e) { igbShowToast('Save failed', 'error'); }
        saveBtn.disabled = false;
        saveBtn.innerHTML = '<i class="fa-solid fa-floppy-disk"></i> Save SMS Config';
    });
}

