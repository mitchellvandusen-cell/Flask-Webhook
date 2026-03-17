const igbServerUrl = 'https://app.insurancegrokbot.click';
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
    console.log('[IGB]', message);
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
    // If subscription has lapsed mid-session, show upgrade prompt and throw so callers bail out
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
            // Silently fail if user access methods unavailable
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
    closeBtn.textContent = '\u00D7';
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

    // Build header
    const header = igbMakeElement('div', 'igb-panel-header');
    const headerIcon = document.createElement('i');
    headerIcon.className = 'fa-solid fa-bolt';
    header.appendChild(headerIcon);
    header.appendChild(document.createTextNode(' AI Minutes '));
    const closeBtn = document.createElement('button');
    closeBtn.className = 'igb-panel-close';
    closeBtn.textContent = '\u00D7';
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

        // Clear and rebuild panel body with DOM methods
        panelBody.innerHTML = '';

        // Stats section
        const statsDiv = igbMakeElement('div', 'igb-minutes-stats');
        statsDiv.innerHTML = `
            <div class="igb-stat-row"><span>Available</span><strong>${igbFormatNumber(available)} min</strong></div>
            <div class="igb-stat-row"><span>Purchased</span><span>${igbFormatNumber(totalPurchased)}</span></div>
            <div class="igb-stat-row"><span>Used</span><span>${igbFormatNumber(totalUsed)}</span></div>
            <div class="igb-progress-bar"><div class="igb-progress-fill" style="width:${Math.min(usedPercent, 100)}%"></div></div>
            <div class="igb-stat-row igb-text-muted"><span>${usedPercent}% used</span></div>
        `;
        panelBody.appendChild(statsDiv);

        // Section label
        const sectionLabel = igbMakeElement('div', 'igb-section-label', 'Buy More Minutes');
        panelBody.appendChild(sectionLabel);

        // Packages grid
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
        // Stats unavailable, skip chip
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

    // Build header
    const header = igbMakeElement('div', 'igb-panel-header');
    const headerIcon = document.createElement('i');
    headerIcon.className = 'fa-solid fa-chart-simple';
    header.appendChild(headerIcon);
    header.appendChild(document.createTextNode(" Today's Stats "));
    const closeBtn = document.createElement('button');
    closeBtn.className = 'igb-panel-close';
    closeBtn.textContent = '\u00D7';
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
        // Stats unavailable
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

    // Build header
    const previewHeader = igbMakeElement('div', 'igb-preview-header');
    previewHeader.appendChild(document.createTextNode('AI Draft '));
    const closeBtn = document.createElement('button');
    closeBtn.className = 'igb-panel-close';
    closeBtn.textContent = '\u00D7';
    closeBtn.addEventListener('click', () => preview.remove());
    previewHeader.appendChild(closeBtn);
    preview.appendChild(previewHeader);

    // Build textarea
    const textarea = document.createElement('textarea');
    textarea.className = 'igb-preview-text';
    textarea.rows = 4;
    textarea.value = draftText;
    preview.appendChild(textarea);

    // Build action buttons
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

            // Add action buttons via DOM instead of inline onclick
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
    // Header
    const header = igbMakeElement('div', 'igb-dialer-header');
    header.appendChild(document.createTextNode(`IGB Dialer \u2014 ${igbSafeText(dialerTitle)}`));
    const closeBtn = document.createElement('button');
    closeBtn.className = 'igb-panel-close';
    closeBtn.textContent = '\u00D7';
    closeBtn.addEventListener('click', igbCloseDialer);
    header.appendChild(closeBtn);
    popup.appendChild(header);

    // Body
    const body = igbMakeElement('div', 'igb-dialer-body');

    // Queue info
    const queueInfo = igbMakeElement('div', 'igb-dialer-queue-info', `Queue: ${igbCallQueue.length} contacts`);
    body.appendChild(queueInfo);

    // Current contact
    const currentContact = igbMakeElement('div', 'igb-current-contact');
    currentContact.id = 'igb-current-contact';
    body.appendChild(currentContact);

    // Dial controls
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

    // Line selection (multi-line)
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

    // Call status bar (hidden initially)
    const statusBar = igbMakeElement('div', 'igb-call-status');
    statusBar.id = 'igb-call-status-bar';
    statusBar.style.display = 'none';
    body.appendChild(statusBar);

    // Call action buttons (hidden initially)
    const actionBtns = igbMakeElement('div', 'igb-call-controls');
    actionBtns.id = 'igb-call-action-btns';
    actionBtns.style.display = 'none';
    body.appendChild(actionBtns);

    // Disposition panel (hidden initially)
    const dispPanel = igbMakeElement('div', 'igb-disposition');
    dispPanel.id = 'igb-disposition-panel';
    dispPanel.style.display = 'none';
    body.appendChild(dispPanel);

    // Queue header and list
    const queueHeader = igbMakeElement('div', 'igb-dialer-queue-header', 'Up Next');
    body.appendChild(queueHeader);

    const queueList = document.createElement('div');
    queueList.id = 'igb-queue-list';
    body.appendChild(queueList);

    popup.appendChild(body);

    // Footer — website link
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
        // Hangup best-effort
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
        // Disposition save best-effort
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
                statusBar.innerHTML += ` \u00B7 ${igbFormatDuration(statusData.duration)}`;
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
        // Poll error, will retry next interval
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

        // Decode a single mulaw (G.711 u-law) byte to a linear PCM float [-1, 1]
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
            // Send init message — backend accepts call_sid from either URL params or first message
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
            // Decode base64 → Uint8Array of mulaw bytes
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
            // Convert mulaw bytes → float32 PCM samples
            const floatSamples = new Float32Array(mulawBytes.length);
            for (let i = 0; i < mulawBytes.length; i++) {
                floatSamples[i] = igbMulawToFloat(mulawBytes[i]);
            }
            // Schedule for gapless playback
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

        // Play button
        const playBtn = document.createElement('button');
        playBtn.className = 'igb-play-btn';
        playBtn.innerHTML = '<i class="fa-solid fa-play"></i>';
        playBtn.addEventListener('click', () => igbPlayRecording(playBtn, callSid));
        audioPlayer.appendChild(playBtn);

        // Progress bar
        const audioBar = igbMakeElement('div', 'igb-audio-bar');
        audioBar.innerHTML = '<div class="igb-audio-progress"></div>';
        audioPlayer.appendChild(audioBar);

        // Time display
        const timeSpan = igbMakeElement('span', 'igb-audio-time', '0:00');
        audioPlayer.appendChild(timeSpan);

        // Transcript button
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
    // Show a fixed upgrade banner — only one at a time
    if (igbFind('#igb-upgrade-banner')) {
        return;
    }
    const banner = igbMakeElement('div', 'igb-upgrade-banner');
    banner.id = 'igb-upgrade-banner';

    const logo = igbMakeElement('div', 'igb-upgrade-logo', '&#129302; <strong>InsuranceGrokBot</strong>');
    banner.appendChild(logo);

    const msg = igbMakeElement('p', 'igb-upgrade-msg', 'Start your subscription to unlock AI texting, voice dialing, and lead intelligence inside GHL.');
    banner.appendChild(msg);

    const btn = igbMakeElement('a', 'igb-upgrade-btn', 'Pick Your Plan \u2192');
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

    // If not subscribed, show upgrade prompt and stop — no other features injected
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
        // Subscription info unavailable, use defaults
    }

    await Promise.all([
        igbRenderStatsChip(),
        igbRenderAiMinutesChip(),
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
