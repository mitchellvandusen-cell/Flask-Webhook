const igbServerUrl = 'https://insurancegrokbot.click';
const igbCallPollMs = 2000;
const igbRefreshIntervalMs = 60000;
const igbMaxVisibleToasts = 3;
let igbKey = '';
let igbKeyExpiresAt = 0;
let igbLocationId = '';
let igbSubscriptionTier = 'individual';
let igbSubscribed = false;
let igbMaxDialLines = 1;
let igbBalanceRefreshTimer = null;
let igbStatsRefreshTimer = null;
let igbDialerPopupEl = null;
let igbCallQueue = [];
let igbCallQueueIndex = 0;
const igbActiveCallMap = new Map();
let igbCallPollTimer = null;
let igbListenSocket = null;
let igbCurrentCallSid = '';
let igbCurrentCallMode = '';
let igbToastList = [];
let igbCurrentAiMinutes = 0;
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
    return num.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ',');
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
function igbGetContactIdFromUrl() {
    const match = window.location.pathname.match(/\/contacts\/(?:detail\/)?([a-zA-Z0-9]+)/);
    return match ? match[1] : '';
}
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
        igbLog('Subscription required — showing upgrade prompt');
        igbSubscribed = false;
        igbShowUpgradePrompt();
        throw new Error('subscription_required');
    }
    return data;
}
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
    closeBtn.textContent = '×';
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
    closeBtn.textContent = '×';
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
    closeBtn.textContent = '×';
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
function igbInjectPipelineButtons() {
    const stageHeaders = igbFindAll(
        '[class*="pipeline"] [class*="stage-header"], '
        + '[class*="pipeline"] [class*="column-header"], '
        + '.board-column .column-header, '
        + '.opportunity-board .board-column > div:first-child, '
        + '.pipeline-view .stage-column > div:first-child'
    );
    stageHeaders.forEach((header) => {
        if (header.querySelector('.igb-dial-btn')) {
            return;
        }
        const dialButton = igbMakeElement('button', 'igb-dial-btn');
        const phoneIcon = document.createElement('i');
        phoneIcon.className = 'fa-solid fa-phone';
        dialButton.appendChild(phoneIcon);
        dialButton.appendChild(document.createTextNode(' Dial'));
        dialButton.title = 'Dial all contacts in this stage';
        dialButton.addEventListener('click', (event) => {
            event.stopPropagation();
            igbDialFromPipelineStage(header);
        });
        header.appendChild(dialButton);
    });
}
function igbDialFromPipelineStage(headerElement) {
    const column = headerElement.closest('[class*="column"], .board-column, .stage-column');
    if (!column) { return; }
    const opportunityCards = column.querySelectorAll('[class*="opportunity-card"], [class*="deal-card"], .board-card');
    const contactList = [];
    opportunityCards.forEach((card) => {
        const contactLink = card.querySelector('a[href*="/contacts/"]');
        let contactId = '';
        if (contactLink) {
            const urlMatch = contactLink.href.match(/\/contacts\/([a-zA-Z0-9]+)/);
            if (urlMatch) { contactId = urlMatch[1]; }
        }
        if (!contactId) {
            contactId = card.getAttribute('data-contact-id') || card.getAttribute('data-id') || '';
        }
        const nameElement = card.querySelector('[class*="name"], [class*="title"]');
        const contactName = nameElement ? nameElement.textContent.trim() : 'Unknown';
        if (contactId) {
            contactList.push({ contactId: contactId, name: contactName });
        }
    });
    if (contactList.length === 0) {
        igbShowToast('No contacts found in this stage', 'error');
        return;
    }
    const stageName = headerElement.textContent.replace(/Dial$/i, '').trim();
    igbOpenDialer(contactList, stageName);
}
async function igbInjectTemperatureBadges() {
    const opportunityCards = igbFindAll('[class*="opportunity-card"], [class*="deal-card"], .board-card');
    const contactIdList = [];
    const cardsByContactId = {};
    opportunityCards.forEach((card) => {
        if (card.querySelector('.igb-temp-badge')) { return; }
        const contactLink = card.querySelector('a[href*="/contacts/"]');
        let contactId = '';
        if (contactLink) {
            const urlMatch = contactLink.href.match(/\/contacts\/([a-zA-Z0-9]+)/);
            if (urlMatch) { contactId = urlMatch[1]; }
        }
        if (!contactId) {
            contactId = card.getAttribute('data-contact-id') || '';
        }
        if (contactId) {
            contactIdList.push(contactId);
            if (!cardsByContactId[contactId]) {
                cardsByContactId[contactId] = [];
            }
            cardsByContactId[contactId].push(card);
        }
    });
    if (contactIdList.length === 0) { return; }
    try {
        const bulkData = await igbApiRequest('GET', '/api/ghl/intelligence/bulk?ids=' + contactIdList.slice(0, 300).join(','));
        const cachedResults = bulkData.cached || {};
        const uncachedIds = bulkData.uncached || [];
        Object.keys(cachedResults).forEach((contactId) => {
            const intelligence = cachedResults[contactId];
            const temperature = intelligence.temperature || '';
            const score = intelligence.score || 0;
            const badge = igbMakeTempBadge(temperature, score);
            (cardsByContactId[contactId] || []).forEach((card) => {
                if (!card.querySelector('.igb-temp-badge')) {
                    card.style.position = 'relative';
                    card.appendChild(badge.cloneNode(true));
                }
            });
        });
        if (uncachedIds.length > 0) {
            igbApiRequest('POST', '/voice/contact-intelligence-analyze', { contact_ids: uncachedIds.slice(0, 5) }).catch(() => {});
        }
    } catch (e) {
        igbLog('Temperature badge error: ' + e);
    }
}
function igbMakeTempBadge(temperature, score) {
    const badge = igbMakeElement('div', 'igb-temp-badge');
    const iconMap = { hot: 'fa-fire', warm: 'fa-temperature-half', cool: 'fa-snowflake', cold: 'fa-icicles' };
    const colorClassMap = { hot: 'igb-color-hot', warm: 'igb-color-warm', cool: 'igb-color-cool', cold: 'igb-color-cold' };
    const iconName = iconMap[temperature] || 'fa-circle';
    const colorClass = colorClassMap[temperature] || 'igb-color-cold';
    const icon = document.createElement('i');
    icon.className = `fa-solid ${iconName} ${colorClass}`;
    badge.appendChild(icon);
    badge.title = `${temperature || 'unknown'} | Score: ${score}`;
    if (temperature === 'hot') {
        badge.classList.add('igb-temp-hot');
    }
    return badge;
}
function igbInjectAiReplyButton() {
    const composeArea = igbFind('[class*="message-composer"], [class*="compose"], [class*="reply-box"], .hl_message-composer');
    if (!composeArea || composeArea.querySelector('.igb-ai-reply-btn')) { return; }
    const contactId = igbGetContactIdFromUrl();
    if (!contactId) { return; }
    const replyButton = igbMakeElement('button', 'igb-ai-reply-btn');
    const robotIcon = document.createElement('i');
    robotIcon.className = 'fa-solid fa-robot';
    replyButton.appendChild(robotIcon);
    replyButton.appendChild(document.createTextNode(' AI Reply'));
    replyButton.title = 'Generate AI reply draft';
    replyButton.addEventListener('click', () => {
        igbGenerateAiReply(contactId, composeArea);
    });
    composeArea.appendChild(replyButton);
}
async function igbGenerateAiReply(contactId, composeElement) {
    const replyBtn = composeElement.querySelector('.igb-ai-reply-btn');
    if (replyBtn) {
        replyBtn.disabled = true;
        replyBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Generating...';
    }
    try {
        const replyData = await igbApiRequest('POST', '/api/ghl/ai-suggest/' + contactId);
        if (replyData.draft) {
            igbShowAiReplyPreview(replyData.draft, contactId, composeElement);
        } else {
            igbShowToast('AI Reply: ' + (replyData.error || 'No draft generated'), 'error');
        }
    } catch (e) {
        igbShowToast('AI Reply failed', 'error');
    } finally {
        if (replyBtn) {
            replyBtn.disabled = false;
            replyBtn.innerHTML = '<i class="fa-solid fa-robot"></i> AI Reply';
        }
    }
}
function igbShowAiReplyPreview(draftText, contactId, composeElement) {
    const existing = igbFind('#igb-ai-preview');
    if (existing) { existing.remove(); }
    const preview = igbMakeElement('div', 'igb-ai-preview');
    preview.id = 'igb-ai-preview';
    const previewHeader = igbMakeElement('div', 'igb-preview-header');
    previewHeader.appendChild(document.createTextNode('AI Draft '));
    const closeBtn = document.createElement('button');
    closeBtn.className = 'igb-panel-close';
    closeBtn.textContent = '×';
    closeBtn.addEventListener('click', () => preview.remove());
    previewHeader.appendChild(closeBtn);
    preview.appendChild(previewHeader);
    const textarea = document.createElement('textarea');
    textarea.className = 'igb-preview-text';
    textarea.rows = 4;
    textarea.value = draftText;
    preview.appendChild(textarea);
    const actions = igbMakeElement('div', 'igb-preview-actions');
    const sendBtn = document.createElement('button');
    sendBtn.className = 'igb-btn igb-btn-primary';
    sendBtn.textContent = 'Send';
    sendBtn.addEventListener('click', async () => {
        const messageText = textarea.value.trim();
        if (!messageText) { return; }
        sendBtn.disabled = true;
        sendBtn.textContent = 'Sending...';
        try {
            const sendResult = await igbApiRequest('POST', '/api/ghl/send-sms/' + contactId, { message: messageText });
            if (sendResult.status === 'sent') {
                igbShowToast('Message sent', 'success');
                preview.remove();
            } else {
                igbShowToast('Send failed: ' + (sendResult.error || ''), 'error');
            }
        } catch (e) {
            igbShowToast('Send error', 'error');
        }
    });
    actions.appendChild(sendBtn);
    const cancelBtn = document.createElement('button');
    cancelBtn.className = 'igb-btn igb-btn-secondary';
    cancelBtn.textContent = 'Cancel';
    cancelBtn.addEventListener('click', () => preview.remove());
    actions.appendChild(cancelBtn);
    preview.appendChild(actions);
    const composeRect = composeElement.getBoundingClientRect();
    preview.style.bottom = (window.innerHeight - composeRect.top + 8) + 'px';
    preview.style.left = composeRect.left + 'px';
    preview.style.width = composeRect.width + 'px';
    document.body.appendChild(preview);
}
async function igbInjectIntelligenceCard() {
    const contactId = igbGetContactIdFromUrl();
    if (!contactId) { return; }
    if (!window.location.pathname.match(/\/contacts\/detail\//)) { return; }
    if (igbFind('#igb-intelligence-card')) { return; }
    const card = igbMakeElement('div', 'igb-intelligence-card');
    card.id = 'igb-intelligence-card';
    card.innerHTML = '<div class="igb-intel-shimmer"></div>';
    const sidebarArea = igbFind('[class*="contact-detail-sidebar"], [class*="right-panel"], .contact-details aside');
    if (sidebarArea) {
        sidebarArea.prepend(card);
    } else {
        card.classList.add('igb-intel-floating');
        document.body.appendChild(card);
    }
    try {
        const intelligenceData = await igbApiRequest('GET', '/api/ghl/intelligence/' + contactId);
        if (intelligenceData.status === 'ok' && intelligenceData.intelligence) {
            const intel = intelligenceData.intelligence;
            const temp = intel.temperature || 'unknown';
            const tempColorMap = { hot: 'igb-color-hot', warm: 'igb-color-warm', cool: 'igb-color-cool', cold: 'igb-color-cold' };
            const tempIconMap = { hot: 'fa-fire', warm: 'fa-temperature-half', cool: 'fa-snowflake', cold: 'fa-icicles' };
            const tempColorClass = tempColorMap[temp] || 'igb-color-cold';
            const tempIconName = tempIconMap[temp] || 'fa-circle';
            const actionsHtml = (intel.actions || []).map((action) => {
                const iconClass = action.icon || 'fa-circle';
                const actionText = igbSafeText(action.text || action.action || '');
                return `<div class="igb-intel-action"><i class="fa-solid ${iconClass}"></i> ${actionText}</div>`;
            }).join('');
            card.innerHTML = `
                <div class="igb-intel-header">
                    <span class="igb-intel-temp ${tempColorClass}"><i class="fa-solid ${tempIconName}"></i> ${igbCapitalize(temp)}</span>
                    <span class="igb-intel-score">Score: ${intel.score || 0}</span>
                </div>
                <div class="igb-intel-summary">${igbSafeText(intel.summary || '')}</div>
                <div class="igb-intel-actions">${actionsHtml}</div>
                <div class="igb-intel-buttons"></div>
            `;
            const buttonsDiv = card.querySelector('.igb-intel-buttons');
            const dialBtn = document.createElement('button');
            dialBtn.className = 'igb-btn igb-btn-sm igb-btn-primary';
            dialBtn.innerHTML = '<i class="fa-solid fa-phone"></i> Dial';
            dialBtn.addEventListener('click', () => igbDialSingleContact(contactId));
            buttonsDiv.appendChild(dialBtn);
            const aiReplyBtn = document.createElement('button');
            aiReplyBtn.className = 'igb-btn igb-btn-sm';
            aiReplyBtn.innerHTML = '<i class="fa-solid fa-robot"></i> AI Reply';
            aiReplyBtn.addEventListener('click', () => igbAiReplySingleContact(contactId));
            buttonsDiv.appendChild(aiReplyBtn);
        } else {
            card.innerHTML = '<div class="igb-intel-empty"><i class="fa-solid fa-brain"></i> No AI intelligence yet</div>';
        }
    } catch (e) {
        card.innerHTML = '<div class="igb-intel-empty">Intelligence unavailable</div>';
    }
}
function igbDialSingleContact(contactId) {
    igbOpenDialer([{ contactId: contactId, name: '' }], 'Single Contact');
}
function igbAiReplySingleContact(contactId) {
    const composeArea = igbFind('[class*="message-composer"], [class*="compose"]');
    if (composeArea) {
        igbGenerateAiReply(contactId, composeArea);
    }
}
function igbInjectBulkCallButton() {
    const bulkActionBar = igbFind('[class*="bulk-actions"], [class*="selection-actions"], .bulk-action-bar');
    if (!bulkActionBar || bulkActionBar.querySelector('.igb-bulk-call-btn')) { return; }
    const callButton = igbMakeElement('button', 'igb-bulk-call-btn igb-btn');
    callButton.innerHTML = '<i class="fa-solid fa-phone"></i> Call with IGB';
    callButton.addEventListener('click', () => {
        const checkedBoxes = igbFindAll('input[type="checkbox"]:checked');
        const selectedContacts = [];
        checkedBoxes.forEach((checkbox) => {
            const row = checkbox.closest('tr, [class*="contact-row"]');
            if (!row) { return; }
            const contactLink = row.querySelector('a[href*="/contacts/"]');
            let contactId = '';
            if (contactLink) {
                const urlMatch = contactLink.href.match(/\/contacts\/([a-zA-Z0-9]+)/);
                if (urlMatch) { contactId = urlMatch[1]; }
            }
            const nameEl = row.querySelector('[class*="name"]');
            if (contactId) {
                selectedContacts.push({ contactId: contactId, name: nameEl ? nameEl.textContent.trim() : '' });
            }
        });
        if (selectedContacts.length > 0) {
            igbOpenDialer(selectedContacts, 'Selected Contacts');
        } else {
            igbShowToast('No contacts selected', 'error');
        }
    });
    bulkActionBar.appendChild(callButton);
}
function igbOpenDialer(contacts, dialerTitle) {
    igbCloseDialer();
    igbCallQueue = contacts;
    igbCallQueueIndex = 0;
    igbActiveCallMap.clear();
    const popup = igbMakeElement('div', 'igb-dialer-popup');
    popup.id = 'igb-dialer-popup';
    igbBuildDialerContent(popup, dialerTitle);
    document.body.appendChild(popup);
    igbDialerPopupEl = popup;
    igbRefreshQueueDisplay();
    igbRefreshCurrentContact();
}
function igbCloseDialer() {
    if (igbDialerPopupEl) {
        igbDialerPopupEl.remove();
        igbDialerPopupEl = null;
    }
    igbStopCallPolling();
    igbStopListenStream();
    igbActiveCallMap.clear();
    igbCurrentCallSid = '';
}
function igbBuildDialerContent(popup, dialerTitle) {
    const header = igbMakeElement('div', 'igb-dialer-header');
    header.appendChild(document.createTextNode(`IGB Dialer — ${igbSafeText(dialerTitle)}`));
    const closeBtn = document.createElement('button');
    closeBtn.className = 'igb-panel-close';
    closeBtn.textContent = '×';
    closeBtn.addEventListener('click', igbCloseDialer);
    header.appendChild(closeBtn);
    popup.appendChild(header);
    const body = igbMakeElement('div', 'igb-dialer-body');
    const queueInfo = igbMakeElement('div', 'igb-dialer-queue-info', `Queue: ${igbCallQueue.length} contacts`);
    body.appendChild(queueInfo);
    const currentContact = igbMakeElement('div', 'igb-current-contact');
    currentContact.id = 'igb-current-contact';
    body.appendChild(currentContact);
    const controls = igbMakeElement('div', 'igb-dialer-controls');
    const aiBtn = document.createElement('button');
    aiBtn.className = 'igb-btn igb-btn-ai';
    aiBtn.id = 'igb-dial-ai-btn';
    aiBtn.innerHTML = '<i class="fa-solid fa-robot"></i> AI Call';
    aiBtn.addEventListener('click', () => igbStartDial('ai'));
    controls.appendChild(aiBtn);
    const humanBtn = document.createElement('button');
    humanBtn.className = 'igb-btn igb-btn-human';
    humanBtn.id = 'igb-dial-human-btn';
    humanBtn.innerHTML = '<i class="fa-solid fa-phone"></i> Human Call';
    humanBtn.addEventListener('click', () => igbStartDial('human'));
    controls.appendChild(humanBtn);
    body.appendChild(controls);
    if (igbMaxDialLines > 1) {
        const linesDiv = igbMakeElement('div', 'igb-dialer-lines');
        linesDiv.appendChild(document.createTextNode('Lines: '));
        for (let lineNum = 1; lineNum <= 4; lineNum++) {
            const isEnabled = lineNum <= igbMaxDialLines;
            const lineBtn = document.createElement('button');
            lineBtn.className = 'igb-line-btn';
            lineBtn.setAttribute('data-lines', lineNum);
            lineBtn.textContent = lineNum;
            if (lineNum === 1) lineBtn.classList.add('active');
            if (!isEnabled) {
                lineBtn.classList.add('disabled');
                lineBtn.disabled = true;
                lineBtn.title = 'Upgrade for more lines';
            }
            linesDiv.appendChild(lineBtn);
        }
        const activeCount = igbMakeElement('span', 'igb-active-count', 'Active: 0');
        linesDiv.appendChild(activeCount);
        body.appendChild(linesDiv);
    }
    const statusBar = igbMakeElement('div', 'igb-call-status');
    statusBar.id = 'igb-call-status-bar';
    statusBar.style.display = 'none';
    body.appendChild(statusBar);
    const actionBtns = igbMakeElement('div', 'igb-call-controls');
    actionBtns.id = 'igb-call-action-btns';
    actionBtns.style.display = 'none';
    body.appendChild(actionBtns);
    const dispPanel = igbMakeElement('div', 'igb-disposition');
    dispPanel.id = 'igb-disposition-panel';
    dispPanel.style.display = 'none';
    body.appendChild(dispPanel);
    const queueHeader = igbMakeElement('div', 'igb-dialer-queue-header', 'Up Next');
    body.appendChild(queueHeader);
    const queueList = document.createElement('div');
    queueList.id = 'igb-queue-list';
    body.appendChild(queueList);
    popup.appendChild(body);
    const footer = igbMakeElement('div', 'igb-dialer-footer');
    const footerLink = igbMakeElement('a', 'igb-dialer-footer-link', 'More information on the website');
    footerLink.href = igbServerUrl;
    footerLink.target = '_blank';
    footerLink.rel = 'noopener';
    footer.appendChild(footerLink);
    popup.appendChild(footer);
}
function igbRefreshCurrentContact() {
    const contactEl = igbFind('#igb-current-contact');
    if (!contactEl || igbCallQueueIndex >= igbCallQueue.length) {
        if (contactEl) {
            contactEl.innerHTML = '<div class="igb-text-muted">Queue complete</div>';
        }
        return;
    }
    const currentContact = igbCallQueue[igbCallQueueIndex];
    contactEl.innerHTML = '';
    const nameDiv = igbMakeElement('div', 'igb-contact-name', igbSafeText(currentContact.name || 'Contact'));
    contactEl.appendChild(nameDiv);
    const skipBtn = document.createElement('button');
    skipBtn.className = 'igb-btn igb-btn-sm igb-btn-secondary';
    skipBtn.textContent = 'Skip';
    skipBtn.addEventListener('click', igbSkipContact);
    contactEl.appendChild(skipBtn);
}
function igbRefreshQueueDisplay() {
    const queueListEl = igbFind('#igb-queue-list');
    if (!queueListEl) { return; }
    queueListEl.innerHTML = '';
    const displayLimit = Math.min(igbCallQueueIndex + 6, igbCallQueue.length);
    for (let idx = igbCallQueueIndex + 1; idx < displayLimit; idx++) {
        const item = igbMakeElement('div', 'igb-queue-item', `${idx + 1}. ${igbSafeText(igbCallQueue[idx].name || 'Contact')}`);
        queueListEl.appendChild(item);
    }
    const remainingCount = igbCallQueue.length - igbCallQueueIndex - 6;
    if (remainingCount > 0) {
        const moreItem = igbMakeElement('div', 'igb-queue-item igb-text-muted', `...${remainingCount} more`);
        queueListEl.appendChild(moreItem);
    }
}
async function igbStartDial(callMode) {
    if (igbCallQueueIndex >= igbCallQueue.length) { return; }
    const contact = igbCallQueue[igbCallQueueIndex];
    igbCurrentCallMode = callMode;
    const aiBtn = igbFind('#igb-dial-ai-btn');
    const humanBtn = igbFind('#igb-dial-human-btn');
    if (aiBtn) { aiBtn.disabled = true; }
    if (humanBtn) { humanBtn.disabled = true; }
    const statusBar = igbFind('#igb-call-status-bar');
    if (statusBar) {
        statusBar.style.display = 'block';
        statusBar.innerHTML = `<span class="igb-status-dot igb-status-ringing"></span> Dialing ${igbSafeText(contact.name)}...`;
    }
    try {
        const dialResponse = await igbApiRequest('POST', '/voice/dial', {
            contact_id: contact.contactId,
            dial_mode: callMode,
        });
        if (dialResponse.call_sid) {
            igbCurrentCallSid = dialResponse.call_sid;
            igbActiveCallMap.set(dialResponse.call_sid, {
                contactId: contact.contactId,
                name: contact.name,
                status: 'initiated',
                callMode: callMode,
            });
            igbStartCallPolling();
            igbShowCallActionButtons(callMode);
        } else {
            igbShowToast('Dial failed: ' + (dialResponse.error || 'Unknown error'), 'error');
            if (aiBtn) { aiBtn.disabled = false; }
            if (humanBtn) { humanBtn.disabled = false; }
        }
    } catch (e) {
        igbShowToast('Dial error: ' + e.message, 'error');
        if (aiBtn) { aiBtn.disabled = false; }
        if (humanBtn) { humanBtn.disabled = false; }
    }
}
function igbSkipContact() {
    igbCallQueueIndex++;
    igbRefreshCurrentContact();
    igbRefreshQueueDisplay();
}
async function igbHangupCall() {
    if (!igbCurrentCallSid) { return; }
    try {
        await igbApiRequest('POST', '/voice/hangup', { call_sid: igbCurrentCallSid });
    } catch (e) {
    }
}
function igbStartListening() {
    if (igbCurrentCallMode !== 'ai' || !igbCurrentCallSid) { return; }
    igbOpenListenStream(igbCurrentCallSid);
}
function igbStopListening() {
    igbStopListenStream();
}
async function igbInterceptCall() {
    if (!igbCurrentCallSid) { return; }
    try {
        const interceptResult = await igbApiRequest('POST', '/voice/takeover', {
            call_sid: igbCurrentCallSid,
            location_id: igbLocationId,
        });
        if (interceptResult.status === 'ok' || interceptResult.success) {
            igbShowToast('Call intercepted', 'success');
            igbStopListenStream();
            igbCurrentCallMode = 'human';
            igbShowCallActionButtons('human');
        } else {
            igbShowToast('Intercept: ' + (interceptResult.error || 'failed'), 'error');
        }
    } catch (e) {
        igbShowToast('Intercept error', 'error');
    }
}
async function igbSaveDisposition(dispositionValue) {
    if (!igbCurrentCallSid) { return; }
    try {
        await igbApiRequest('POST', '/voice/call-disposition', {
            call_sid: igbCurrentCallSid,
            disposition: dispositionValue,
        });
        igbShowToast('Disposition saved: ' + dispositionValue, 'success');
    } catch (e) {
    }
    igbCurrentCallSid = '';
    igbCallQueueIndex++;
    igbRefreshCurrentContact();
    igbRefreshQueueDisplay();
    igbHideCallControls();
    const aiBtn = igbFind('#igb-dial-ai-btn');
    const humanBtn = igbFind('#igb-dial-human-btn');
    if (aiBtn) { aiBtn.disabled = false; }
    if (humanBtn) { humanBtn.disabled = false; }
}
function igbShowCallActionButtons(callMode) {
    const actionBtns = igbFind('#igb-call-action-btns');
    if (!actionBtns) { return; }
    actionBtns.style.display = 'flex';
    actionBtns.innerHTML = '';
    if (callMode === 'ai') {
        const listenBtn = document.createElement('button');
        listenBtn.className = 'igb-ctrl-btn';
        listenBtn.title = 'Listen live';
        listenBtn.innerHTML = '<i class="fa-solid fa-headphones"></i>';
        listenBtn.addEventListener('click', igbStartListening);
        actionBtns.appendChild(listenBtn);
        const interceptBtn = document.createElement('button');
        interceptBtn.className = 'igb-ctrl-btn';
        interceptBtn.title = 'Take over call';
        interceptBtn.innerHTML = '<i class="fa-solid fa-bolt"></i>';
        interceptBtn.addEventListener('click', igbInterceptCall);
        actionBtns.appendChild(interceptBtn);
    }
    const hangupBtn = document.createElement('button');
    hangupBtn.className = 'igb-ctrl-btn igb-ctrl-hangup';
    hangupBtn.title = 'Hang up';
    hangupBtn.innerHTML = '<i class="fa-solid fa-phone-slash"></i>';
    hangupBtn.addEventListener('click', igbHangupCall);
    actionBtns.appendChild(hangupBtn);
}
function igbHideCallControls() {
    const actionBtns = igbFind('#igb-call-action-btns');
    if (actionBtns) { actionBtns.style.display = 'none'; }
    const statusBar = igbFind('#igb-call-status-bar');
    if (statusBar) { statusBar.style.display = 'none'; }
    const dispositionPanel = igbFind('#igb-disposition-panel');
    if (dispositionPanel) { dispositionPanel.style.display = 'none'; }
}
function igbShowDispositionPanel() {
    const dispositionPanel = igbFind('#igb-disposition-panel');
    if (!dispositionPanel) { return; }
    dispositionPanel.style.display = 'block';
    dispositionPanel.innerHTML = '';
    const label = igbMakeElement('div', 'igb-disp-label', 'Disposition:');
    dispositionPanel.appendChild(label);
    const grid = igbMakeElement('div', 'igb-disp-grid');
    const dispositions = [
        { value: 'connected', label: 'Connected', isDnc: false },
        { value: 'voicemail', label: 'Voicemail', isDnc: false },
        { value: 'no_answer', label: 'No Answer', isDnc: false },
        { value: 'callback', label: 'Callback', isDnc: false },
        { value: 'interested', label: 'Interested', isDnc: false },
        { value: 'not_interested', label: 'Not Interested', isDnc: false },
        { value: 'do_not_call', label: 'DNC', isDnc: true },
    ];
    dispositions.forEach((disp) => {
        const btn = document.createElement('button');
        btn.className = disp.isDnc ? 'igb-disp-btn igb-disp-dnc' : 'igb-disp-btn';
        btn.textContent = disp.label;
        btn.addEventListener('click', () => igbSaveDisposition(disp.value));
        grid.appendChild(btn);
    });
    dispositionPanel.appendChild(grid);
    const actionBtns = igbFind('#igb-call-action-btns');
    if (actionBtns) { actionBtns.style.display = 'none'; }
}
function igbStartCallPolling() {
    igbStopCallPolling();
    igbCallPollTimer = setInterval(igbPollCallStatus, igbCallPollMs);
}
function igbStopCallPolling() {
    if (igbCallPollTimer) {
        clearInterval(igbCallPollTimer);
        igbCallPollTimer = null;
    }
}
async function igbPollCallStatus() {
    if (!igbCurrentCallSid) {
        igbStopCallPolling();
        return;
    }
    try {
        const statusData = await igbApiRequest('GET', '/voice/call-status/' + igbCurrentCallSid);
        const callStatus = statusData.status || 'unknown';
        const statusBar = igbFind('#igb-call-status-bar');
        const callInfo = igbActiveCallMap.get(igbCurrentCallSid);
        const contactName = callInfo ? callInfo.name : '';
        let dotClass = 'igb-status-ringing';
        let statusLabel = callStatus;
        if (callStatus === 'in-progress') {
            dotClass = 'igb-status-connected';
            statusLabel = (igbCurrentCallMode === 'ai' ? 'AI talking to ' : 'Connected to ') + contactName;
        } else if (callStatus === 'ringing') {
            statusLabel = `Ringing ${contactName}...`;
        } else if (callStatus === 'initiated') {
            statusLabel = `Dialing ${contactName}...`;
        }
        if (statusBar) {
            statusBar.innerHTML = `<span class="igb-status-dot ${dotClass}"></span> ${statusLabel}`;
            if (statusData.duration) {
                statusBar.innerHTML += ` · ${igbFormatDuration(statusData.duration)}`;
            }
        }
        const terminalStatuses = ['completed', 'busy', 'no-answer', 'failed', 'canceled'];
        if (terminalStatuses.indexOf(callStatus) >= 0) {
            igbStopCallPolling();
            igbStopListenStream();
            if (statusBar) {
                statusBar.innerHTML = `<span class="igb-status-dot igb-status-ended"></span> Call ended (${callStatus})`;
            }
            igbShowDispositionPanel();
        }
    } catch (e) {
    }
}
function igbOpenListenStream(callSid) {
    igbStopListenStream();
    try {
        const wsUrl = igbServerUrl.replace('https://', 'wss://').replace('http://', 'ws://')
            + '/voice/listen-stream?call_sid=' + callSid
            + '&key=' + encodeURIComponent(igbKey);
        igbListenSocket = new WebSocket(wsUrl);
        igbListenSocket.binaryType = 'arraybuffer';
        let audioContext = null;
        let nextPlayAt = 0;  // Scheduled playback timestamp to prevent gaps/overlap
        function igbMulawToFloat(u) {
            u = ~u & 0xFF;
            const sign = (u & 0x80) ? -1 : 1;
            const exp = (u >> 4) & 0x07;
            const mantissa = u & 0x0F;
            const magnitude = ((mantissa << 1) + 33) << (exp + 2);
            return sign * magnitude / 32768.0;
        }
        igbListenSocket.onopen = () => {
            igbLog('Live listen connected');
            igbShowToast('Listening to call...', 'info');
            try {
                igbListenSocket.send(JSON.stringify({ call_sid: callSid }));
            } catch (sendErr) {
                igbLog('Listen stream send error: ' + sendErr);
            }
            try {
                audioContext = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 8000 });
                nextPlayAt = audioContext.currentTime;
            } catch (audioError) {
                igbLog('AudioContext unavailable: ' + audioError);
            }
        };
        igbListenSocket.onmessage = (event) => {
            let msg;
            try {
                msg = JSON.parse(event.data);
            } catch (e) {
                return;
            }
            if (msg.status === 'call_ended') {
                igbLog('Listen stream: call ended');
                igbStopListenStream();
                return;
            }
            if (!msg.audio || !audioContext) {
                return;
            }
            let mulawBytes;
            try {
                const binary = atob(msg.audio);
                mulawBytes = new Uint8Array(binary.length);
                for (let i = 0; i < binary.length; i++) {
                    mulawBytes[i] = binary.charCodeAt(i);
                }
            } catch (e) {
                return;
            }
            const floatSamples = new Float32Array(mulawBytes.length);
            for (let i = 0; i < mulawBytes.length; i++) {
                floatSamples[i] = igbMulawToFloat(mulawBytes[i]);
            }
            const buffer = audioContext.createBuffer(1, floatSamples.length, 8000);
            buffer.getChannelData(0).set(floatSamples);
            const source = audioContext.createBufferSource();
            source.buffer = buffer;
            source.connect(audioContext.destination);
            const now = audioContext.currentTime;
            if (nextPlayAt < now) {
                nextPlayAt = now;
            }
            source.start(nextPlayAt);
            nextPlayAt += buffer.duration;
        };
        igbListenSocket.onclose = () => {
            igbLog('Live listen disconnected');
            if (audioContext) {
                audioContext.close().catch(() => {});
                audioContext = null;
            }
        };
        igbListenSocket.onerror = () => {
            igbLog('Live listen error');
        };
    } catch (e) {
        igbLog('Listen stream error: ' + e);
    }
}
function igbStopListenStream() {
    if (igbListenSocket) {
        igbListenSocket.onclose = null;
        igbListenSocket.close();
        igbListenSocket = null;
    }
}
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
function igbInjectRecordingPlayers() {
    const callMessageEls = igbFindAll('[class*="call-message"], [class*="call-entry"], [data-message-type="Call"]');
    callMessageEls.forEach((callEntry) => {
        if (callEntry.querySelector('.igb-audio-player')) { return; }
        let callSid = callEntry.getAttribute('data-call-sid') || '';
        if (!callSid) {
            const sidMatch = callEntry.textContent.match(/CA[a-f0-9]{32}/);
            if (sidMatch) { callSid = sidMatch[0]; }
        }
        if (!callSid) { return; }
        const audioPlayer = igbMakeElement('div', 'igb-audio-player');
        const playBtn = document.createElement('button');
        playBtn.className = 'igb-play-btn';
        playBtn.innerHTML = '<i class="fa-solid fa-play"></i>';
        playBtn.addEventListener('click', () => igbPlayRecording(playBtn, callSid));
        audioPlayer.appendChild(playBtn);
        const audioBar = igbMakeElement('div', 'igb-audio-bar');
        audioBar.innerHTML = '<div class="igb-audio-progress"></div>';
        audioPlayer.appendChild(audioBar);
        const timeSpan = igbMakeElement('span', 'igb-audio-time', '0:00');
        audioPlayer.appendChild(timeSpan);
        const transcriptBtn = document.createElement('button');
        transcriptBtn.className = 'igb-transcript-btn';
        transcriptBtn.title = 'Transcript';
        transcriptBtn.innerHTML = '<i class="fa-solid fa-file-lines"></i>';
        transcriptBtn.addEventListener('click', () => igbToggleTranscript(transcriptBtn, callSid));
        audioPlayer.appendChild(transcriptBtn);
        callEntry.appendChild(audioPlayer);
    });
}
async function igbPlayRecording(playBtn, callSid) {
    const playerEl = playBtn.closest('.igb-audio-player');
    const existingAudio = playerEl.querySelector('audio');
    if (existingAudio) {
        if (existingAudio.paused) {
            existingAudio.play();
            playBtn.innerHTML = '<i class="fa-solid fa-pause"></i>';
        } else {
            existingAudio.pause();
            playBtn.innerHTML = '<i class="fa-solid fa-play"></i>';
        }
        return;
    }
    playBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
    const audioEl = document.createElement('audio');
    audioEl.src = igbServerUrl + '/voice/recording/' + callSid + '?key=' + encodeURIComponent(igbKey);
    audioEl.style.display = 'none';
    playerEl.appendChild(audioEl);
    audioEl.onloadedmetadata = () => {
        playerEl.querySelector('.igb-audio-time').textContent = igbFormatDuration(Math.round(audioEl.duration));
    };
    audioEl.ontimeupdate = () => {
        const progressPercent = audioEl.duration ? (audioEl.currentTime / audioEl.duration * 100) : 0;
        const progressBar = playerEl.querySelector('.igb-audio-progress');
        if (progressBar) { progressBar.style.width = progressPercent + '%'; }
        playerEl.querySelector('.igb-audio-time').textContent =
            igbFormatDuration(Math.round(audioEl.currentTime)) + '/' + igbFormatDuration(Math.round(audioEl.duration || 0));
    };
    audioEl.onended = () => {
        playBtn.innerHTML = '<i class="fa-solid fa-play"></i>';
    };
    try {
        await audioEl.play();
        playBtn.innerHTML = '<i class="fa-solid fa-pause"></i>';
    } catch (e) {
        playBtn.innerHTML = '<i class="fa-solid fa-play"></i>';
    }
}
async function igbToggleTranscript(transcriptBtn, callSid) {
    const playerEl = transcriptBtn.closest('.igb-audio-player');
    const existingPanel = playerEl.querySelector('.igb-transcript-panel');
    if (existingPanel) {
        existingPanel.remove();
        return;
    }
    const transcriptPanel = igbMakeElement('div', 'igb-transcript-panel');
    transcriptPanel.innerHTML = '<div class="igb-loading">Loading transcript...</div>';
    playerEl.appendChild(transcriptPanel);
    try {
        const callData = await igbApiRequest('GET', '/voice/call-status/' + callSid);
        if (callData.transcript) {
            const textDiv = igbMakeElement('div', 'igb-transcript-text', igbSafeText(callData.transcript));
            transcriptPanel.innerHTML = '';
            transcriptPanel.appendChild(textDiv);
        } else {
            transcriptPanel.innerHTML = '';
            const transcribeBtn = document.createElement('button');
            transcribeBtn.className = 'igb-btn igb-btn-sm';
            transcribeBtn.textContent = 'Transcribe';
            transcribeBtn.addEventListener('click', () => igbRequestTranscription(callSid, transcribeBtn));
            transcriptPanel.appendChild(transcribeBtn);
        }
    } catch (e) {
        transcriptPanel.innerHTML = '<div class="igb-text-muted">Transcript unavailable</div>';
    }
}
async function igbRequestTranscription(callSid, requestBtn) {
    requestBtn.disabled = true;
    requestBtn.textContent = 'Transcribing...';
    try {
        await igbApiRequest('POST', '/voice/transcribe-recording', { call_sid: callSid });
        igbShowToast('Transcription started', 'info');
    } catch (e) {
        igbShowToast('Transcription failed', 'error');
    }
}
let igbConfigPanelOpen = false;
let igbConfigSection = 'overview';
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
    closeBtn.textContent = '×';
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
    return '<div class="igb-cfg-info-label">' + igbSafeText(label) + '</div><div class="igb-cfg-info-value">' + igbSafeText(value || '—') + '</div>';
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
async function igbSecActivate(c) {
    const vc = await igbApiRequest('GET', '/api/ghl/voice-config');
    c.innerHTML = '';
    const title = igbMakeElement('div', 'igb-cfg-section-title', 'Voice Account');
    c.appendChild(title);
    if (vc.voice_activated) {
        c.innerHTML += igbBadge('Voice Account Active', true);
        const info = igbMakeElement('div', 'igb-cfg-info-grid');
        info.innerHTML = igbInfoRow('Phone Number', vc.has_phone_number ? 'Provisioned' : 'None yet')
            + igbInfoRow('Calling Hours', (vc.calling_hours_start || '08:00') + ' – ' + (vc.calling_hours_end || '21:00'));
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
                        buyBtn.textContent = '✓'; buyBtn.classList.add('igb-cfg-btn-owned');
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
    c.appendChild(igbMakeElement('div', 'igb-cfg-desc', 'One-click spam protection registers your business with carrier spam databases (AT&T/Hiya, T-Mobile, Verizon) and sets up CNAM caller ID. This improves call answer rates and prevents "Spam Likely" labels.'));
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
    c.appendChild(igbMakeElement('div', 'igb-cfg-desc', 'Voice Integrity registers your numbers with carrier spam analytics engines to remediate spam labels and improve answer rates. Separate from A2P (which is for SMS).'));
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
    c.appendChild(igbMakeElement('div', 'igb-cfg-desc', 'A2P 10DLC registers your brand and use case with mobile carriers for business SMS. This is optional — your SMS bot uses GHL by default. Register if you want to send SMS directly through your own Twilio numbers.'));
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
    form.innerHTML += '<label class="igb-cfg-label">Voice Greeting</label>'
        + '<textarea class="igb-cfg-input igb-cfg-textarea" id="igb-vc-greeting" rows="2" maxlength="500">' + igbSafeText(vc.voice_greeting || '') + '</textarea>';
    form.innerHTML += '<label class="igb-cfg-label">AI Personality / Instructions</label>'
        + '<textarea class="igb-cfg-input igb-cfg-textarea" id="igb-vc-instructions" rows="3" maxlength="2000">' + igbSafeText(vc.voice_instructions || '') + '</textarea>';
    form.innerHTML += '<label class="igb-cfg-label">Transfer Number</label>'
        + '<input class="igb-cfg-input" id="igb-vc-transfer" type="text" value="' + igbSafeText(vc.transfer_number || '') + '" placeholder="+15551234567" maxlength="20">';
    form.innerHTML += '<div class="igb-cfg-row">'
        + '<div class="igb-cfg-col"><label class="igb-cfg-label">Calling Hours Start</label><input class="igb-cfg-input" id="igb-vc-hrs-start" type="time" value="' + (vc.calling_hours_start || '08:00') + '"></div>'
        + '<div class="igb-cfg-col"><label class="igb-cfg-label">Calling Hours End</label><input class="igb-cfg-input" id="igb-vc-hrs-end" type="time" value="' + (vc.calling_hours_end || '21:00') + '"></div>'
        + '</div>';
    form.innerHTML += '<label class="igb-cfg-label">On Voicemail Action</label>'
        + '<select class="igb-cfg-input igb-cfg-select" id="igb-vc-machine">'
        + '<option value="hangup"' + (vc.on_machine_action === 'hangup' ? ' selected' : '') + '>Hang Up</option>'
        + '<option value="voicemail_drop"' + (vc.on_machine_action === 'voicemail_drop' ? ' selected' : '') + '>Leave Voicemail</option>'
        + '<option value="continue"' + (vc.on_machine_action === 'continue' ? ' selected' : '') + '>Continue (AI talks to machine)</option>'
        + '</select>';
    form.innerHTML += '<div class="igb-cfg-row">'
        + '<div class="igb-cfg-col"><label class="igb-cfg-label">Max Call Duration (sec)</label><input class="igb-cfg-input" id="igb-vc-maxdur" type="number" min="30" max="3600" value="' + (vc.max_call_duration || 300) + '"></div>'
        + '<div class="igb-cfg-col"><label class="igb-cfg-label">Wrap-Up Time (sec)</label><input class="igb-cfg-input" id="igb-vc-wrapup" type="number" min="0" max="120" value="' + (vc.wrap_up_time || 15) + '"></div>'
        + '</div>';
    form.innerHTML += '<div class="igb-cfg-row">'
        + '<div class="igb-cfg-col"><label class="igb-cfg-label">Same # Cooldown (hrs)</label><input class="igb-cfg-input" id="igb-vc-cooldown" type="number" min="0" max="72" value="' + (vc.same_number_cooldown_hours || 4) + '"></div>'
        + '<div class="igb-cfg-col"><label class="igb-cfg-label">Daily Max Per Contact</label><input class="igb-cfg-input" id="igb-vc-dailymax" type="number" min="0" max="10" value="' + (vc.same_contact_daily_max || 3) + '"></div>'
        + '</div>';
    form.innerHTML += '<button class="igb-cfg-btn-action igb-cfg-btn-save" id="igb-vc-save"><i class="fa-solid fa-floppy-disk"></i> Save Voice Config</button>';
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
async function igbSecSms(c) {
    const data = await igbApiRequest('GET', '/api/ghl/bot-config');
    c.innerHTML = '';
    const title = igbMakeElement('div', 'igb-cfg-section-title', 'SMS Bot Configuration');
    c.appendChild(title);
    const form = igbMakeElement('div', 'igb-cfg-form');
    form.innerHTML += '<label class="igb-cfg-label">Bot Name (appears as sender)</label>'
        + '<input class="igb-cfg-input" id="igb-sc-name" type="text" value="' + igbSafeText(data.bot_first_name || '') + '" placeholder="Grok" maxlength="100">';
    form.innerHTML += '<label class="igb-cfg-label">Timezone</label>'
        + '<input class="igb-cfg-input" id="igb-sc-tz" type="text" value="' + igbSafeText(data.timezone || 'America/Chicago') + '" placeholder="America/Chicago" maxlength="50">';
    form.innerHTML += '<label class="igb-cfg-label">SMS Send Via</label>'
        + '<select class="igb-cfg-input igb-cfg-select" id="igb-sc-via">'
        + '<option value="ghl"' + (data.sms_send_via === 'ghl' || !data.sms_send_via ? ' selected' : '') + '>GHL (GoHighLevel) — Default</option>'
        + '<option value="twilio"' + (data.sms_send_via && data.sms_send_via.startsWith('+') ? ' selected' : '') + '>Twilio (Direct) — Requires A2P</option>'
        + '</select>';
    c.appendChild(igbMakeElement('div', 'igb-cfg-desc', 'Your SMS bot sends through GHL by default — no extra setup needed. Switch to Twilio only if you need direct number control with A2P 10DLC registration.'));
    form.innerHTML += '<label class="igb-cfg-label">Initial Greeting Message</label>'
        + '<textarea class="igb-cfg-input igb-cfg-textarea" id="igb-sc-init" rows="2" maxlength="500">' + igbSafeText(data.initial_message || '') + '</textarea>';
    form.innerHTML += '<label class="igb-cfg-label">Personal Website (optional)</label>'
        + '<input class="igb-cfg-input" id="igb-sc-web" type="text" value="' + igbSafeText(data.personal_website || '') + '" placeholder="https://..." maxlength="255">';
    form.innerHTML += '<button class="igb-cfg-btn-action igb-cfg-btn-save" id="igb-sc-save"><i class="fa-solid fa-floppy-disk"></i> Save SMS Config</button>';
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
function igbHandlePageChange() {
    const currentPath = window.location.pathname;
    if (currentPath.indexOf('/opportunities') >= 0 || currentPath.indexOf('/pipeline') >= 0) {
        setTimeout(() => {
            igbInjectPipelineButtons();
            igbInjectTemperatureBadges();
        }, 500);
    }
    if (currentPath.indexOf('/conversations') >= 0 || currentPath.indexOf('/messages') >= 0) {
        setTimeout(() => {
            igbInjectAiReplyButton();
            igbInjectRecordingPlayers();
        }, 500);
    }
    if (currentPath.indexOf('/contacts/detail/') >= 0) {
        setTimeout(igbInjectIntelligenceCard, 500);
    }
    if (currentPath.match(/\/contacts\/?$/) || currentPath.indexOf('/contacts?') >= 0) {
        setTimeout(igbInjectBulkCallButton, 500);
    }
}
const igbDomObserver = new MutationObserver(() => {
    clearTimeout(igbDomObserver.debounceTimer);
    igbDomObserver.debounceTimer = setTimeout(igbHandlePageChange, 300);
});
function igbShowUpgradePrompt() {
    if (igbFind('#igb-upgrade-banner')) {
        return;
    }
    const banner = igbMakeElement('div', 'igb-upgrade-banner');
    banner.id = 'igb-upgrade-banner';
    const logo = igbMakeElement('div', 'igb-upgrade-logo', '&#129302; <strong>InsuranceGrokBot</strong>');
    banner.appendChild(logo);
    const msg = igbMakeElement('p', 'igb-upgrade-msg', 'Start your subscription to unlock AI texting, voice dialing, and lead intelligence inside GHL.');
    banner.appendChild(msg);
    const btn = igbMakeElement('a', 'igb-upgrade-btn', 'Pick Your Plan →');
    btn.href = igbServerUrl + '/dashboard?tab=billing';
    btn.target = '_blank';
    btn.rel = 'noopener';
    banner.appendChild(btn);
    document.body.appendChild(banner);
}
async function igbInit() {
    igbLog('Initializing');
    if (!igbIsKeyValid()) {
        const authSuccess = await igbAuthenticate();
        if (!authSuccess) {
            igbLog('Auth failed, features disabled');
            return;
        }
    }
    if (!igbSubscribed) {
        igbLog('No active subscription — showing upgrade prompt');
        igbShowUpgradePrompt();
        return;
    }
    try {
        const subscriptionInfo = await igbApiRequest('GET', '/api/ghl/subscription-info');
        igbSubscriptionTier = subscriptionInfo.tier || 'individual';
        igbMaxDialLines = subscriptionInfo.max_lines || 1;
    } catch (e) {
    }
    await Promise.all([
        igbRenderStatsChip(),
        igbRenderAiMinutesChip(),
        igbRenderConfigChip(),
    ]);
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
