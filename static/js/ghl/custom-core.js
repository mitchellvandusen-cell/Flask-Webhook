/**
 * InsuranceGrokBot GHL Extension — Core (1 of 3)
 * Stats chips, AI Minutes, page observer, and app initialization.
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

async function igbRenderAiMinutesChip() {
    const existing = igbFind('#igb-minutes-chip');
    if (existing) {
        existing.remove();
    }
    try {
        const balanceData = await igbApiRequest('GET', '/api/ghl/ai-minutes/balance');
        igbCurrentAiMinutes = balanceData.balance_minutes || 0;
    } catch (e) {
        return;
    }
    const chip = igbMakeElement('div', 'igb-chip igb-minutes-chip');
    chip.id = 'igb-minutes-chip';
    let colorClass = 'igb-chip-green';
    if (igbCurrentAiMinutes <= 500) { colorClass = 'igb-chip-yellow'; }
    if (igbCurrentAiMinutes <= 100) { colorClass = 'igb-chip-red'; }
    if (igbCurrentAiMinutes < 50)  { colorClass += ' igb-chip-pulse'; }
    chip.classList.add(...colorClass.split(' '));
    const boltIcon = document.createElement('i');
    boltIcon.className = 'fa-solid fa-bolt';
    chip.appendChild(boltIcon);
    chip.appendChild(document.createTextNode(` ${igbFormatNumber(igbCurrentAiMinutes)} min`));
    chip.title = 'AI Minutes balance';
    chip.addEventListener('click', igbToggleMinutesPanel);
    igbInjectIntoTopNav(chip);
}
async function igbToggleMinutesPanel() {
    const existing = igbFind('#igb-minutes-panel');
    if (existing) {
        existing.remove();
        return;
    }
    const panel = igbMakeElement('div', 'igb-dropdown-panel');
    panel.id = 'igb-minutes-panel';
    const header = igbMakeElement('div', 'igb-panel-header');
    const headerIcon = document.createElement('i');
    headerIcon.className = 'fa-solid fa-bolt';
    header.appendChild(headerIcon);
    header.appendChild(document.createTextNode(' AI Minutes '));
    const closeBtn = document.createElement('button');
    closeBtn.className = 'igb-panel-close';
    closeBtn.textContent = 'x';
    closeBtn.addEventListener('click', () => {
        const p = document.getElementById('igb-minutes-panel');
        if (p) p.remove();
    });
    header.appendChild(closeBtn);
    panel.appendChild(header);
    const body = igbMakeElement('div', 'igb-panel-body');
    body.innerHTML = '<div class="igb-loading">Loading...</div>';
    panel.appendChild(body);
    document.body.appendChild(panel);
    const chipEl = igbFind('#igb-minutes-chip');
    if (chipEl) {
        const chipRect = chipEl.getBoundingClientRect();
        panel.style.top = (chipRect.bottom + 8) + 'px';
        panel.style.right = (window.innerWidth - chipRect.right) + 'px';
    }
    try {
        const results = await Promise.all([
            igbApiRequest('GET', '/api/ghl/ai-minutes/balance'),
            igbApiRequest('GET', '/api/ghl/ai-minutes/packages'),
        ]);
        const balanceResult = results[0];
        const packagesResult = results[1];
        const available = balanceResult.balance_minutes || 0;
        const totalPurchased = balanceResult.total_purchased || 0;
        const totalUsed = balanceResult.total_used || 0;
        const usedPercent = totalPurchased > 0 ? Math.round(totalUsed / totalPurchased * 100) : 0;
        const panelBody = igbFind('#igb-minutes-panel .igb-panel-body');
        if (!panelBody) { return; }
        panelBody.innerHTML = '';
        const statsDiv = igbMakeElement('div', 'igb-minutes-stats');
        statsDiv.innerHTML = `
            <div class="igb-stat-row"><span>Available</span><strong>${igbFormatNumber(available)} min</strong></div>
            <div class="igb-stat-row"><span>Purchased</span><span>${igbFormatNumber(totalPurchased)}</span></div>
            <div class="igb-stat-row"><span>Used</span><span>${igbFormatNumber(totalUsed)}</span></div>
            <div class="igb-progress-bar"><div class="igb-progress-fill" style="width:${Math.min(usedPercent, 100)}%"></div></div>
            <div class="igb-stat-row igb-text-muted"><span>${usedPercent}% used</span></div>
        `;
        panelBody.appendChild(statsDiv);
        const sectionLabel = igbMakeElement('div', 'igb-section-label', 'Buy More Minutes');
        panelBody.appendChild(sectionLabel);
        const grid = igbMakeElement('div', 'igb-packages-grid');
        const packageList = packagesResult.packages || [];
        const packageColorMap = { 500: 'igb-pkg-green', 2000: 'igb-pkg-blue', 5000: 'igb-pkg-purple', 10000: 'igb-pkg-gold' };
        packageList.forEach((pkg) => {
            const displayPrice = pkg.price_cents ? '$' + (pkg.price_cents / 100).toFixed(2) : 'N/A';
            const packageClass = packageColorMap[pkg.minutes] || 'igb-pkg-green';
            const card = igbMakeElement('div', `igb-package-card ${packageClass}`);
            card.innerHTML = `
                <div class="igb-package-minutes"><i class="fa-solid fa-bolt"></i> ${igbFormatNumber(pkg.minutes)}</div>
                <div class="igb-package-label">${igbSafeText(pkg.label)}</div>
                <div class="igb-package-price">${displayPrice}</div>
            `;
            const buyBtn = document.createElement('button');
            buyBtn.className = 'igb-btn igb-btn-sm';
            buyBtn.textContent = 'Buy';
            if (!pkg.available) {
                buyBtn.disabled = true;
            }
            buyBtn.addEventListener('click', () => igbPurchaseMinutes(pkg.minutes));
            card.appendChild(buyBtn);
            grid.appendChild(card);
        });
        panelBody.appendChild(grid);
    } catch (e) {
        igbLog('Minutes panel error: ' + e);
    }
}
async function igbPurchaseMinutes(minuteAmount) {
    try {
        const checkoutData = await igbApiRequest('POST', '/api/ghl/ai-minutes/checkout', { minutes: minuteAmount });
        if (checkoutData.checkout_url) {
            window.open(checkoutData.checkout_url, '_blank');
        } else {
            igbShowToast('Checkout failed: ' + (checkoutData.error || 'Unknown error'), 'error');
        }
    } catch (e) {
        igbShowToast('Checkout error', 'error');
    }
}
async function igbRenderStatsChip() {
    const existing = igbFind('#igb-stats-chip');
    if (existing) {
        existing.remove();
    }
    try {
        const statsData = await igbApiRequest('GET', '/api/ghl/stats?period=today');
        const callsMade = statsData.total_calls || 0;
        const callsConnected = statsData.connected || 0;
        const chip = igbMakeElement('div', 'igb-chip igb-stats-chip');
        chip.id = 'igb-stats-chip';
        const chartIcon = document.createElement('i');
        chartIcon.className = 'fa-solid fa-chart-simple';
        chip.appendChild(chartIcon);
        chip.appendChild(document.createTextNode(` ${callsMade}/${callsConnected}`));
        chip.title = `Today: ${callsMade} calls, ${callsConnected} connected, ${statsData.connect_rate || 0}% rate, ${igbFormatDuration(statsData.avg_duration || 0)} avg`;
        chip.addEventListener('click', igbToggleStatsPanel);
        igbInjectIntoTopNav(chip);
    } catch (e) {
    }
}
async function igbToggleStatsPanel() {
    const existing = igbFind('#igb-stats-panel');
    if (existing) {
        existing.remove();
        return;
    }
    const panel = igbMakeElement('div', 'igb-dropdown-panel');
    panel.id = 'igb-stats-panel';
    const header = igbMakeElement('div', 'igb-panel-header');
    const headerIcon = document.createElement('i');
    headerIcon.className = 'fa-solid fa-chart-simple';
    header.appendChild(headerIcon);
    header.appendChild(document.createTextNode(" Today's Stats "));
    const closeBtn = document.createElement('button');
    closeBtn.className = 'igb-panel-close';
    closeBtn.textContent = 'x';
    closeBtn.addEventListener('click', () => {
        const p = document.getElementById('igb-stats-panel');
        if (p) p.remove();
    });
    header.appendChild(closeBtn);
    panel.appendChild(header);
    const body = igbMakeElement('div', 'igb-panel-body');
    body.innerHTML = '<div class="igb-loading">Loading...</div>';
    panel.appendChild(body);
    document.body.appendChild(panel);
    const statsChip = igbFind('#igb-stats-chip');
    if (statsChip) {
        const chipRect = statsChip.getBoundingClientRect();
        panel.style.top = (chipRect.bottom + 8) + 'px';
        panel.style.right = (window.innerWidth - chipRect.right) + 'px';
    }
    try {
        const allStats = await Promise.all([
            igbApiRequest('GET', '/api/ghl/stats?period=today'),
            igbApiRequest('GET', '/api/ghl/stats?period=week'),
        ]);
        const todayStats = allStats[0];
        const weekStats = allStats[1];
        const panelBody = igbFind('#igb-stats-panel .igb-panel-body');
        if (!panelBody) { return; }
        panelBody.innerHTML = `
            <div class="igb-minutes-stats">
                <div class="igb-stat-row"><span>Calls Made</span><strong>${todayStats.total_calls || 0}</strong></div>
                <div class="igb-stat-row"><span>Connected</span><strong>${todayStats.connected || 0} (${todayStats.connect_rate || 0}%)</strong></div>
                <div class="igb-stat-row"><span>Avg Duration</span><span>${igbFormatDuration(todayStats.avg_duration || 0)}</span></div>
                <div class="igb-stat-row"><span>Voicemails</span><span>${todayStats.voicemail || 0}</span></div>
                <div class="igb-stat-row"><span>No Answer</span><span>${todayStats.no_answer || 0}</span></div>
            </div>
            <div class="igb-section-label">This Week</div>
            <div class="igb-minutes-stats">
                <div class="igb-stat-row"><span>Calls</span><span>${weekStats.total_calls || 0}</span></div>
                <div class="igb-stat-row"><span>Connected</span><span>${weekStats.connected || 0}</span></div>
                <div class="igb-stat-row"><span>Talk Time</span><span>${igbFormatDuration(weekStats.total_duration || 0)}</span></div>
            </div>
        `;
    } catch (e) {
    }
}

// ---------------------------------------------------------------------------
// Page change detection and feature injection
// ---------------------------------------------------------------------------

function igbHandlePageChange() {
    var currentPath = window.location.pathname;
    if (currentPath.indexOf('/opportunities') >= 0 || currentPath.indexOf('/pipeline') >= 0) {
        setTimeout(function() {
            if (typeof igbInjectPipelineButtons === 'function') { igbInjectPipelineButtons(); }
            if (typeof igbInjectTemperatureBadges === 'function') { igbInjectTemperatureBadges(); }
        }, 500);
    }
    if (currentPath.indexOf('/conversations') >= 0 || currentPath.indexOf('/messages') >= 0) {
        setTimeout(function() {
            if (typeof igbInjectAiReplyButton === 'function') { igbInjectAiReplyButton(); }
            if (typeof igbInjectRecordingPlayers === 'function') { igbInjectRecordingPlayers(); }
        }, 500);
    }
    if (currentPath.indexOf('/contacts/detail/') >= 0) {
        setTimeout(function() {
            if (typeof igbInjectIntelligenceCard === 'function') { igbInjectIntelligenceCard(); }
        }, 500);
    }
    if (currentPath.match(/\/contacts\/?$/) || currentPath.indexOf('/contacts?') >= 0) {
        setTimeout(function() {
            if (typeof igbInjectBulkCallButton === 'function') { igbInjectBulkCallButton(); }
        }, 500);
    }
}
var igbDomObserver = new MutationObserver(function() {
    clearTimeout(igbDomObserver.debounceTimer);
    igbDomObserver.debounceTimer = setTimeout(igbHandlePageChange, 300);
});

function igbShowUpgradePrompt() {
    if (igbFind('#igb-upgrade-banner')) {
        return;
    }
    const banner = igbMakeElement('div', 'igb-upgrade-banner');
    banner.id = 'igb-upgrade-banner';
    const logo = igbMakeElement('div', 'igb-upgrade-logo', '<strong>InsuranceGrokBot</strong>');
    banner.appendChild(logo);
    const msg = igbMakeElement('p', 'igb-upgrade-msg', 'Start your subscription to unlock AI texting, voice dialing, and lead intelligence inside GHL.');
    banner.appendChild(msg);
    const btn = igbMakeElement('a', 'igb-upgrade-btn', 'Pick Your Plan');
    btn.href = igbServerUrl + '/dashboard?tab=billing';
    btn.target = '_blank';
    btn.rel = 'noopener';
    banner.appendChild(btn);
    document.body.appendChild(banner);
}

// ---------------------------------------------------------------------------
// App initialization
// ---------------------------------------------------------------------------

async function igbInit() {
    igbLog('Initializing');
    if (!igbIsKeyValid()) {
        var authSuccess = await igbAuthenticate();
        if (!authSuccess) {
            igbLog('Auth failed, features disabled');
            return;
        }
    }
    if (!igbSubscribed) {
        igbLog('No active subscription - showing upgrade prompt');
        igbShowUpgradePrompt();
        return;
    }
    try {
        var subscriptionInfo = await igbApiRequest('GET', '/api/ghl/subscription-info');
        igbSubscriptionTier = subscriptionInfo.tier || 'individual';
        igbMaxDialLines = subscriptionInfo.max_lines || 1;
    } catch (e) {
    }
    var chipPromises = [
        igbRenderStatsChip(),
        igbRenderAiMinutesChip(),
    ];
    if (typeof igbRenderConfigChip === 'function') {
        chipPromises.push(igbRenderConfigChip());
    }
    await Promise.all(chipPromises);
    igbBalanceRefreshTimer = setInterval(igbRenderAiMinutesChip, igbRefreshIntervalMs);
    igbStatsRefreshTimer = setInterval(igbRenderStatsChip, igbRefreshIntervalMs);
    igbHandlePageChange();
    window.addEventListener('routeChangeEvent', igbHandlePageChange);
    window.addEventListener('popstate', igbHandlePageChange);
    igbDomObserver.observe(document.body, { childList: true, subtree: true });
    igbLog('Ready, tier: ' + igbSubscriptionTier + ', lines: ' + igbMaxDialLines);
}
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', igbInit);
} else {
    igbInit();
}
