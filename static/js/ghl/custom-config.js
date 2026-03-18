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
    form.innerHTML = '<input type="text" class="igb-cfg-input" id="igb-ns-area" placeholder="Area code" maxlength="6">'
        + '<select class="igb-cfg-input igb-cfg-select" id="igb-ns-type"><option value="local">Local ($0.90/mo)</option><option value="toll_free">Toll-Free ($2.15/mo)</option></select>'
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
