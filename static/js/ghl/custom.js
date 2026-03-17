var igbServerUrl = 'https://app.insurancegrokbot.click';
var igbKeyStorageKey = 'igb_jwt';
var igbKeyExpiryKey = 'igb_jwt_exp';
var igbCallPollMs = 2000;
var igbRefreshIntervalMs = 60000;
var igbMaxVisibleToasts = 3;

var igbKey = '';
var igbLocationId = '';
var igbSubscriptionTier = 'individual';
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

function igbMakeElement(tagName, cssClass, htmlContent) {
    var el = document.createElement(tagName);
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
    var minutes = Math.floor(totalSeconds / 60);
    var seconds = Math.floor(totalSeconds % 60);
    return minutes + ':' + (seconds < 10 ? '0' : '') + seconds;
}

function igbSafeText(rawText) {
    var div = document.createElement('div');
    div.textContent = rawText;
    return div.innerHTML;
}

function igbCapitalize(str) {
    return str ? str.charAt(0).toUpperCase() + str.slice(1) : '';
}

function igbGetContactIdFromUrl() {
    var match = window.location.pathname.match(/\/contacts\/(?:detail\/)?([a-zA-Z0-9]+)/);
    return match ? match[1] : '';
}

async function igbApiRequest(httpMethod, apiPath, requestBody) {
    var fetchOptions = {
        method: httpMethod,
        headers: {
            'Authorization': 'Bearer ' + igbKey,
            'Content-Type': 'application/json',
        },
    };
    if (requestBody) {
        fetchOptions.body = JSON.stringify(requestBody);
    }
    var response = await fetch(igbServerUrl + apiPath, fetchOptions);
    if (response.status === 401) {
        igbLog('Session expired, refreshing');
        await igbAuthenticate();
        fetchOptions.headers['Authorization'] = 'Bearer ' + igbKey;
        response = await fetch(igbServerUrl + apiPath, fetchOptions);
    }
    return response.json();
}

async function igbAuthenticate() {
    try {
        igbLocationId = await igbGetLocationId();
        if (!igbLocationId) {
            igbLog('Could not determine location ID');
            return false;
        }

        var requestPayload = {
            location_id: igbLocationId,
            timestamp: Math.floor(Date.now() / 1000),
        };

        var userAccess = await igbGetGhlUserAccess();
        if (userAccess) {
            requestPayload.ghl_token = userAccess;
        }

        var resp = await fetch(igbServerUrl + '/api/ghl/auth/token', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(requestPayload),
        });

        var data = await resp.json();
        if (data.token) {
            igbKey = data.token;
            igbSubscriptionTier = data.tier || 'individual';
            localStorage.setItem(igbKeyStorageKey, igbKey);
            var expiresAt = Date.now() + (data.expires_in || 7200) * 1000;
            localStorage.setItem(igbKeyExpiryKey, String(expiresAt));
            igbLog('Authenticated, tier: ' + igbSubscriptionTier);
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
            var locationData = await AppUtils.Utilities.getCurrentLocation();
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
        } catch (e) {}
    }
    return '';
}

function igbIsKeyValid() {
    var expiry = parseInt(localStorage.getItem(igbKeyExpiryKey) || '0');
    return igbKey && Date.now() < expiry - 60000;
}

function igbShowToast(message, toastType) {
    toastType = toastType || 'info';
    var toast = igbMakeElement('div', 'igb-toast igb-toast-' + toastType);
    toast.innerHTML = message + '<button class="igb-toast-close" onclick="this.parentElement.remove()">&times;</button>';

    var container = igbFind('#igb-toast-container');
    if (!container) {
        container = igbMakeElement('div', '');
        container.id = 'igb-toast-container';
        document.body.appendChild(container);
    }
    container.appendChild(toast);
    igbToastList.push(toast);

    while (igbToastList.length > igbMaxVisibleToasts) {
        var oldToast = igbToastList.shift();
        if (oldToast.parentElement) {
            oldToast.remove();
        }
    }

    setTimeout(function() {
        toast.classList.add('igb-toast-fade');
        setTimeout(function() { toast.remove(); }, 300);
    }, 5000);
}

async function igbRenderAiMinutesChip() {
    var existing = igbFind('#igb-minutes-chip');
    if (existing) {
        existing.remove();
    }

    try {
        var balanceData = await igbApiRequest('GET', '/api/ghl/ai-minutes/balance');
        igbCurrentAiMinutes = balanceData.balance_minutes || 0;
    } catch (e) {
        return;
    }

    var chip = igbMakeElement('div', 'igb-chip igb-minutes-chip');
    chip.id = 'igb-minutes-chip';

    var colorClass = 'igb-chip-green';
    if (igbCurrentAiMinutes <= 500) { colorClass = 'igb-chip-yellow'; }
    if (igbCurrentAiMinutes <= 100) { colorClass = 'igb-chip-red'; }
    if (igbCurrentAiMinutes < 50)  { colorClass += ' igb-chip-pulse'; }

    chip.classList.add(colorClass);
    chip.innerHTML = '<i class="fa-solid fa-bolt"></i> ' + igbFormatNumber(igbCurrentAiMinutes) + ' min';
    chip.title = 'AI Minutes balance';
    chip.onclick = igbToggleMinutesPanel;

    igbInjectIntoTopNav(chip);
}

async function igbToggleMinutesPanel() {
    var existing = igbFind('#igb-minutes-panel');
    if (existing) {
        existing.remove();
        return;
    }

    var panel = igbMakeElement('div', 'igb-dropdown-panel');
    panel.id = 'igb-minutes-panel';
    panel.innerHTML = '<div class="igb-panel-header"><i class="fa-solid fa-bolt"></i> AI Minutes <button class="igb-panel-close" onclick="document.getElementById(\'igb-minutes-panel\').remove()">&times;</button></div><div class="igb-panel-body"><div class="igb-loading">Loading...</div></div>';
    document.body.appendChild(panel);

    var chipEl = igbFind('#igb-minutes-chip');
    if (chipEl) {
        var chipRect = chipEl.getBoundingClientRect();
        panel.style.top = (chipRect.bottom + 8) + 'px';
        panel.style.right = (window.innerWidth - chipRect.right) + 'px';
    }

    try {
        var results = await Promise.all([
            igbApiRequest('GET', '/api/ghl/ai-minutes/balance'),
            igbApiRequest('GET', '/api/ghl/ai-minutes/packages'),
        ]);
        var balanceResult = results[0];
        var packagesResult = results[1];

        var available = balanceResult.balance_minutes || 0;
        var totalPurchased = balanceResult.total_purchased || 0;
        var totalUsed = balanceResult.total_used || 0;
        var usedPercent = totalPurchased > 0 ? Math.round(totalUsed / totalPurchased * 100) : 0;

        var panelBody = igbFind('#igb-minutes-panel .igb-panel-body');
        if (!panelBody) { return; }

        var panelHtml = '<div class="igb-minutes-stats">'
            + '<div class="igb-stat-row"><span>Available</span><strong>' + igbFormatNumber(available) + ' min</strong></div>'
            + '<div class="igb-stat-row"><span>Purchased</span><span>' + igbFormatNumber(totalPurchased) + '</span></div>'
            + '<div class="igb-stat-row"><span>Used</span><span>' + igbFormatNumber(totalUsed) + '</span></div>'
            + '<div class="igb-progress-bar"><div class="igb-progress-fill" style="width:' + Math.min(usedPercent, 100) + '%"></div></div>'
            + '<div class="igb-stat-row igb-text-muted"><span>' + usedPercent + '% used</span></div>'
            + '</div>';

        panelHtml += '<div class="igb-section-label">Buy More Minutes</div><div class="igb-packages-grid">';

        var packageList = packagesResult.packages || [];
        var packageColorMap = { 500: 'igb-pkg-green', 2000: 'igb-pkg-blue', 5000: 'igb-pkg-purple', 10000: 'igb-pkg-gold' };

        packageList.forEach(function(pkg) {
            var displayPrice = pkg.price_cents ? '$' + (pkg.price_cents / 100).toFixed(2) : 'N/A';
            var packageClass = packageColorMap[pkg.minutes] || 'igb-pkg-green';
            var disabledAttr = pkg.available ? '' : ' disabled';
            panelHtml += '<div class="igb-package-card ' + packageClass + '">'
                + '<div class="igb-package-minutes"><i class="fa-solid fa-bolt"></i> ' + igbFormatNumber(pkg.minutes) + '</div>'
                + '<div class="igb-package-label">' + pkg.label + '</div>'
                + '<div class="igb-package-price">' + displayPrice + '</div>'
                + '<button class="igb-btn igb-btn-sm" onclick="igbPurchaseMinutes(' + pkg.minutes + ')"' + disabledAttr + '>Buy</button>'
                + '</div>';
        });

        panelHtml += '</div>';
        panelBody.innerHTML = panelHtml;
    } catch (e) {
        igbLog('Minutes panel error: ' + e);
    }
}

async function igbPurchaseMinutes(minuteAmount) {
    try {
        var checkoutData = await igbApiRequest('POST', '/api/ghl/ai-minutes/checkout', { minutes: minuteAmount });
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
    var existing = igbFind('#igb-stats-chip');
    if (existing) {
        existing.remove();
    }

    try {
        var statsData = await igbApiRequest('GET', '/api/ghl/stats?period=today');
        var callsMade = statsData.total_calls || 0;
        var callsConnected = statsData.connected || 0;

        var chip = igbMakeElement('div', 'igb-chip igb-stats-chip');
        chip.id = 'igb-stats-chip';
        chip.innerHTML = '<i class="fa-solid fa-chart-simple"></i> ' + callsMade + '/' + callsConnected;
        chip.title = 'Today: ' + callsMade + ' calls, ' + callsConnected + ' connected, '
            + (statsData.connect_rate || 0) + '% rate, '
            + igbFormatDuration(statsData.avg_duration || 0) + ' avg';
        chip.onclick = igbToggleStatsPanel;

        igbInjectIntoTopNav(chip);
    } catch (e) {}
}

async function igbToggleStatsPanel() {
    var existing = igbFind('#igb-stats-panel');
    if (existing) {
        existing.remove();
        return;
    }

    var panel = igbMakeElement('div', 'igb-dropdown-panel');
    panel.id = 'igb-stats-panel';
    panel.innerHTML = '<div class="igb-panel-header"><i class="fa-solid fa-chart-simple"></i> Today\'s Stats <button class="igb-panel-close" onclick="document.getElementById(\'igb-stats-panel\').remove()">&times;</button></div><div class="igb-panel-body"><div class="igb-loading">Loading...</div></div>';
    document.body.appendChild(panel);

    var statsChip = igbFind('#igb-stats-chip');
    if (statsChip) {
        var chipRect = statsChip.getBoundingClientRect();
        panel.style.top = (chipRect.bottom + 8) + 'px';
        panel.style.right = (window.innerWidth - chipRect.right) + 'px';
    }

    try {
        var allStats = await Promise.all([
            igbApiRequest('GET', '/api/ghl/stats?period=today'),
            igbApiRequest('GET', '/api/ghl/stats?period=week'),
        ]);
        var todayStats = allStats[0];
        var weekStats = allStats[1];

        var panelBody = igbFind('#igb-stats-panel .igb-panel-body');
        if (!panelBody) { return; }

        panelBody.innerHTML = '<div class="igb-minutes-stats">'
            + '<div class="igb-stat-row"><span>Calls Made</span><strong>' + (todayStats.total_calls || 0) + '</strong></div>'
            + '<div class="igb-stat-row"><span>Connected</span><strong>' + (todayStats.connected || 0) + ' (' + (todayStats.connect_rate || 0) + '%)</strong></div>'
            + '<div class="igb-stat-row"><span>Avg Duration</span><span>' + igbFormatDuration(todayStats.avg_duration || 0) + '</span></div>'
            + '<div class="igb-stat-row"><span>Voicemails</span><span>' + (todayStats.voicemail || 0) + '</span></div>'
            + '<div class="igb-stat-row"><span>No Answer</span><span>' + (todayStats.no_answer || 0) + '</span></div>'
            + '</div>'
            + '<div class="igb-section-label">This Week</div>'
            + '<div class="igb-minutes-stats">'
            + '<div class="igb-stat-row"><span>Calls</span><span>' + (weekStats.total_calls || 0) + '</span></div>'
            + '<div class="igb-stat-row"><span>Connected</span><span>' + (weekStats.connected || 0) + '</span></div>'
            + '<div class="igb-stat-row"><span>Talk Time</span><span>' + igbFormatDuration(weekStats.total_duration || 0) + '</span></div>'
            + '</div>';
    } catch (e) {}
}

function igbInjectPipelineButtons() {
    var stageHeaders = igbFindAll(
        '[class*="pipeline"] [class*="stage-header"], '
        + '[class*="pipeline"] [class*="column-header"], '
        + '.board-column .column-header, '
        + '.opportunity-board .board-column > div:first-child, '
        + '.pipeline-view .stage-column > div:first-child'
    );

    stageHeaders.forEach(function(header) {
        if (header.querySelector('.igb-dial-btn')) {
            return;
        }

        var dialButton = igbMakeElement('button', 'igb-dial-btn');
        dialButton.innerHTML = '<i class="fa-solid fa-phone"></i> Dial';
        dialButton.title = 'Dial all contacts in this stage';
        dialButton.onclick = function(event) {
            event.stopPropagation();
            igbDialFromPipelineStage(header);
        };
        header.appendChild(dialButton);
    });
}

function igbDialFromPipelineStage(headerElement) {
    var column = headerElement.closest('[class*="column"], .board-column, .stage-column');
    if (!column) { return; }

    var opportunityCards = column.querySelectorAll('[class*="opportunity-card"], [class*="deal-card"], .board-card');
    var contactList = [];

    opportunityCards.forEach(function(card) {
        var contactLink = card.querySelector('a[href*="/contacts/"]');
        var contactId = '';

        if (contactLink) {
            var urlMatch = contactLink.href.match(/\/contacts\/([a-zA-Z0-9]+)/);
            if (urlMatch) { contactId = urlMatch[1]; }
        }

        if (!contactId) {
            contactId = card.getAttribute('data-contact-id') || card.getAttribute('data-id') || '';
        }

        var nameElement = card.querySelector('[class*="name"], [class*="title"]');
        var contactName = nameElement ? nameElement.textContent.trim() : 'Unknown';

        if (contactId) {
            contactList.push({ contactId: contactId, name: contactName });
        }
    });

    if (contactList.length === 0) {
        igbShowToast('No contacts found in this stage', 'error');
        return;
    }

    var stageName = headerElement.textContent.replace(/Dial$/i, '').trim();
    igbOpenDialer(contactList, stageName);
}

async function igbInjectTemperatureBadges() {
    var opportunityCards = igbFindAll('[class*="opportunity-card"], [class*="deal-card"], .board-card');
    var contactIdList = [];
    var cardsByContactId = {};

    opportunityCards.forEach(function(card) {
        if (card.querySelector('.igb-temp-badge')) { return; }

        var contactLink = card.querySelector('a[href*="/contacts/"]');
        var contactId = '';

        if (contactLink) {
            var urlMatch = contactLink.href.match(/\/contacts\/([a-zA-Z0-9]+)/);
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
        var bulkData = await igbApiRequest('GET', '/api/ghl/intelligence/bulk?ids=' + contactIdList.slice(0, 300).join(','));
        var cachedResults = bulkData.cached || {};
        var uncachedIds = bulkData.uncached || [];

        Object.keys(cachedResults).forEach(function(contactId) {
            var intelligence = cachedResults[contactId];
            var temperature = intelligence.temperature || '';
            var score = intelligence.score || 0;
            var badge = igbMakeTempBadge(temperature, score);

            (cardsByContactId[contactId] || []).forEach(function(card) {
                if (!card.querySelector('.igb-temp-badge')) {
                    card.style.position = 'relative';
                    card.appendChild(badge.cloneNode(true));
                }
            });
        });

        if (uncachedIds.length > 0) {
            igbApiRequest('POST', '/voice/contact-intelligence-analyze', { contact_ids: uncachedIds.slice(0, 5) }).catch(function() {});
        }
    } catch (e) {
        igbLog('Temperature badge error: ' + e);
    }
}

function igbMakeTempBadge(temperature, score) {
    var badge = igbMakeElement('div', 'igb-temp-badge');
    var iconMap = { hot: 'fa-fire', warm: 'fa-temperature-half', cool: 'fa-snowflake', cold: 'fa-icicles' };
    var colorClassMap = { hot: 'igb-color-hot', warm: 'igb-color-warm', cool: 'igb-color-cool', cold: 'igb-color-cold' };
    var iconName = iconMap[temperature] || 'fa-circle';
    var colorClass = colorClassMap[temperature] || 'igb-color-cold';
    badge.innerHTML = '<i class="fa-solid ' + iconName + ' ' + colorClass + '"></i>';
    badge.title = (temperature || 'unknown') + ' | Score: ' + score;
    if (temperature === 'hot') {
        badge.classList.add('igb-temp-hot');
    }
    return badge;
}

function igbInjectAiReplyButton() {
    var composeArea = igbFind('[class*="message-composer"], [class*="compose"], [class*="reply-box"], .hl_message-composer');
    if (!composeArea || composeArea.querySelector('.igb-ai-reply-btn')) { return; }

    var contactId = igbGetContactIdFromUrl();
    if (!contactId) { return; }

    var replyButton = igbMakeElement('button', 'igb-ai-reply-btn');
    replyButton.innerHTML = '<i class="fa-solid fa-robot"></i> AI Reply';
    replyButton.title = 'Generate AI reply draft';
    replyButton.onclick = function() {
        igbGenerateAiReply(contactId, composeArea);
    };
    composeArea.appendChild(replyButton);
}

async function igbGenerateAiReply(contactId, composeElement) {
    var replyBtn = composeElement.querySelector('.igb-ai-reply-btn');
    if (replyBtn) {
        replyBtn.disabled = true;
        replyBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Generating...';
    }

    try {
        var replyData = await igbApiRequest('POST', '/api/ghl/ai-suggest/' + contactId);
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
    var existing = igbFind('#igb-ai-preview');
    if (existing) { existing.remove(); }

    var preview = igbMakeElement('div', 'igb-ai-preview');
    preview.id = 'igb-ai-preview';
    preview.innerHTML = '<div class="igb-preview-header">AI Draft <button class="igb-panel-close" onclick="document.getElementById(\'igb-ai-preview\').remove()">&times;</button></div>'
        + '<textarea class="igb-preview-text" rows="4">' + igbSafeText(draftText) + '</textarea>'
        + '<div class="igb-preview-actions">'
        + '<button class="igb-btn igb-btn-primary" id="igb-send-draft-btn">Send</button>'
        + '<button class="igb-btn igb-btn-secondary" onclick="document.getElementById(\'igb-ai-preview\').remove()">Cancel</button>'
        + '</div>';

    var composeRect = composeElement.getBoundingClientRect();
    preview.style.bottom = (window.innerHeight - composeRect.top + 8) + 'px';
    preview.style.left = composeRect.left + 'px';
    preview.style.width = composeRect.width + 'px';
    document.body.appendChild(preview);

    igbFind('#igb-send-draft-btn').onclick = async function() {
        var messageText = preview.querySelector('textarea').value.trim();
        if (!messageText) { return; }
        this.disabled = true;
        this.textContent = 'Sending...';
        try {
            var sendResult = await igbApiRequest('POST', '/api/ghl/send-sms/' + contactId, { message: messageText });
            if (sendResult.status === 'sent') {
                igbShowToast('Message sent', 'success');
                preview.remove();
            } else {
                igbShowToast('Send failed: ' + (sendResult.error || ''), 'error');
            }
        } catch (e) {
            igbShowToast('Send error', 'error');
        }
    };
}

async function igbInjectIntelligenceCard() {
    var contactId = igbGetContactIdFromUrl();
    if (!contactId) { return; }
    if (!window.location.pathname.match(/\/contacts\/detail\//)) { return; }
    if (igbFind('#igb-intelligence-card')) { return; }

    var card = igbMakeElement('div', 'igb-intelligence-card');
    card.id = 'igb-intelligence-card';
    card.innerHTML = '<div class="igb-intel-shimmer"></div>';

    var sidebarArea = igbFind('[class*="contact-detail-sidebar"], [class*="right-panel"], .contact-details aside');
    if (sidebarArea) {
        sidebarArea.prepend(card);
    } else {
        card.classList.add('igb-intel-floating');
        document.body.appendChild(card);
    }

    try {
        var intelligenceData = await igbApiRequest('GET', '/api/ghl/intelligence/' + contactId);
        if (intelligenceData.status === 'ok' && intelligenceData.intelligence) {
            var intel = intelligenceData.intelligence;
            var temp = intel.temperature || 'unknown';
            var tempColorMap = { hot: 'igb-color-hot', warm: 'igb-color-warm', cool: 'igb-color-cool', cold: 'igb-color-cold' };
            var tempIconMap = { hot: 'fa-fire', warm: 'fa-temperature-half', cool: 'fa-snowflake', cold: 'fa-icicles' };
            var tempColorClass = tempColorMap[temp] || 'igb-color-cold';
            var tempIconName = tempIconMap[temp] || 'fa-circle';

            var actionsHtml = (intel.actions || []).map(function(action) {
                return '<div class="igb-intel-action"><i class="fa-solid ' + (action.icon || 'fa-circle') + '"></i> ' + igbSafeText(action.text || action.action || '') + '</div>';
            }).join('');

            card.innerHTML = '<div class="igb-intel-header">'
                + '<span class="igb-intel-temp ' + tempColorClass + '"><i class="fa-solid ' + tempIconName + '"></i> ' + igbCapitalize(temp) + '</span>'
                + '<span class="igb-intel-score">Score: ' + (intel.score || 0) + '</span>'
                + '</div>'
                + '<div class="igb-intel-summary">' + igbSafeText(intel.summary || '') + '</div>'
                + '<div class="igb-intel-actions">' + actionsHtml + '</div>'
                + '<div class="igb-intel-buttons">'
                + '<button class="igb-btn igb-btn-sm igb-btn-primary" onclick="igbDialSingleContact(\'' + contactId + '\')"><i class="fa-solid fa-phone"></i> Dial</button>'
                + '<button class="igb-btn igb-btn-sm" onclick="igbAiReplySingleContact(\'' + contactId + '\')"><i class="fa-solid fa-robot"></i> AI Reply</button>'
                + '</div>';
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
    var composeArea = igbFind('[class*="message-composer"], [class*="compose"]');
    if (composeArea) {
        igbGenerateAiReply(contactId, composeArea);
    }
}

function igbInjectBulkCallButton() {
    var bulkActionBar = igbFind('[class*="bulk-actions"], [class*="selection-actions"], .bulk-action-bar');
    if (!bulkActionBar || bulkActionBar.querySelector('.igb-bulk-call-btn')) { return; }

    var callButton = igbMakeElement('button', 'igb-bulk-call-btn igb-btn');
    callButton.innerHTML = '<i class="fa-solid fa-phone"></i> Call with IGB';
    callButton.onclick = function() {
        var checkedBoxes = igbFindAll('input[type="checkbox"]:checked');
        var selectedContacts = [];

        checkedBoxes.forEach(function(checkbox) {
            var row = checkbox.closest('tr, [class*="contact-row"]');
            if (!row) { return; }
            var contactLink = row.querySelector('a[href*="/contacts/"]');
            var contactId = '';
            if (contactLink) {
                var urlMatch = contactLink.href.match(/\/contacts\/([a-zA-Z0-9]+)/);
                if (urlMatch) { contactId = urlMatch[1]; }
            }
            var nameEl = row.querySelector('[class*="name"]');
            if (contactId) {
                selectedContacts.push({ contactId: contactId, name: nameEl ? nameEl.textContent.trim() : '' });
            }
        });

        if (selectedContacts.length > 0) {
            igbOpenDialer(selectedContacts, 'Selected Contacts');
        } else {
            igbShowToast('No contacts selected', 'error');
        }
    };

    bulkActionBar.appendChild(callButton);
}

function igbOpenDialer(contacts, dialerTitle) {
    igbCloseDialer();
    igbCallQueue = contacts;
    igbCallQueueIndex = 0;
    igbActiveCallMap.clear();

    var popup = igbMakeElement('div', 'igb-dialer-popup');
    popup.id = 'igb-dialer-popup';
    popup.innerHTML = igbBuildDialerHtml(dialerTitle);
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

function igbBuildDialerHtml(dialerTitle) {
    var lineSelectionHtml = '';
    if (igbMaxDialLines > 1) {
        lineSelectionHtml = '<div class="igb-dialer-lines">Lines: ';
        for (var lineNum = 1; lineNum <= 4; lineNum++) {
            var isEnabled = lineNum <= igbMaxDialLines;
            var activeClass = lineNum === 1 ? ' active' : '';
            var disabledClass = !isEnabled ? ' disabled' : '';
            var disabledAttr = !isEnabled ? ' disabled title="Upgrade for more lines"' : '';
            lineSelectionHtml += '<button class="igb-line-btn' + activeClass + disabledClass + '" data-lines="' + lineNum + '"' + disabledAttr + '>' + lineNum + '</button> ';
        }
        lineSelectionHtml += '<span class="igb-active-count">Active: 0</span></div>';
    }

    return '<div class="igb-dialer-header">'
        + '<span>IGB Dialer &mdash; ' + igbSafeText(dialerTitle) + '</span>'
        + '<button class="igb-panel-close" onclick="igbCloseDialer()">&times;</button>'
        + '</div>'
        + '<div class="igb-dialer-body">'
        + '<div class="igb-dialer-queue-info">Queue: ' + igbCallQueue.length + ' contacts</div>'
        + '<div id="igb-current-contact" class="igb-current-contact"></div>'
        + '<div class="igb-dialer-controls">'
        + '<button class="igb-btn igb-btn-ai" id="igb-dial-ai-btn" onclick="igbStartDial(\'ai\')"><i class="fa-solid fa-robot"></i> AI Call</button>'
        + '<button class="igb-btn igb-btn-human" id="igb-dial-human-btn" onclick="igbStartDial(\'human\')"><i class="fa-solid fa-phone"></i> Human Call</button>'
        + '</div>'
        + lineSelectionHtml
        + '<div id="igb-call-status-bar" class="igb-call-status" style="display:none"></div>'
        + '<div id="igb-call-action-btns" class="igb-call-controls" style="display:none"></div>'
        + '<div id="igb-disposition-panel" class="igb-disposition" style="display:none"></div>'
        + '<div class="igb-dialer-queue-header">Up Next</div>'
        + '<div id="igb-queue-list"></div>'
        + '</div>';
}

function igbRefreshCurrentContact() {
    var contactEl = igbFind('#igb-current-contact');
    if (!contactEl || igbCallQueueIndex >= igbCallQueue.length) {
        if (contactEl) {
            contactEl.innerHTML = '<div class="igb-text-muted">Queue complete</div>';
        }
        return;
    }
    var currentContact = igbCallQueue[igbCallQueueIndex];
    contactEl.innerHTML = '<div class="igb-contact-name">' + igbSafeText(currentContact.name || 'Contact') + '</div>'
        + '<button class="igb-btn igb-btn-sm igb-btn-secondary" onclick="igbSkipContact()">Skip</button>';
}

function igbRefreshQueueDisplay() {
    var queueListEl = igbFind('#igb-queue-list');
    if (!queueListEl) { return; }

    var listHtml = '';
    var displayLimit = Math.min(igbCallQueueIndex + 6, igbCallQueue.length);
    for (var idx = igbCallQueueIndex + 1; idx < displayLimit; idx++) {
        listHtml += '<div class="igb-queue-item">' + (idx + 1) + '. ' + igbSafeText(igbCallQueue[idx].name || 'Contact') + '</div>';
    }
    var remainingCount = igbCallQueue.length - igbCallQueueIndex - 6;
    if (remainingCount > 0) {
        listHtml += '<div class="igb-queue-item igb-text-muted">...' + remainingCount + ' more</div>';
    }
    queueListEl.innerHTML = listHtml;
}

async function igbStartDial(callMode) {
    if (igbCallQueueIndex >= igbCallQueue.length) { return; }

    var contact = igbCallQueue[igbCallQueueIndex];
    igbCurrentCallMode = callMode;

    var aiBtn = igbFind('#igb-dial-ai-btn');
    var humanBtn = igbFind('#igb-dial-human-btn');
    if (aiBtn) { aiBtn.disabled = true; }
    if (humanBtn) { humanBtn.disabled = true; }

    var statusBar = igbFind('#igb-call-status-bar');
    if (statusBar) {
        statusBar.style.display = 'block';
        statusBar.innerHTML = '<span class="igb-status-dot igb-status-ringing"></span> Dialing ' + igbSafeText(contact.name) + '...';
    }

    try {
        var dialResponse = await igbApiRequest('POST', '/voice/dial', {
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
    } catch (e) {}
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
        var interceptResult = await igbApiRequest('POST', '/voice/takeover', {
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
    } catch (e) {}

    igbCurrentCallSid = '';
    igbCallQueueIndex++;
    igbRefreshCurrentContact();
    igbRefreshQueueDisplay();
    igbHideCallControls();

    var aiBtn = igbFind('#igb-dial-ai-btn');
    var humanBtn = igbFind('#igb-dial-human-btn');
    if (aiBtn) { aiBtn.disabled = false; }
    if (humanBtn) { humanBtn.disabled = false; }
}

function igbShowCallActionButtons(callMode) {
    var actionBtns = igbFind('#igb-call-action-btns');
    if (!actionBtns) { return; }
    actionBtns.style.display = 'flex';

    var buttonsHtml = '';
    if (callMode === 'ai') {
        buttonsHtml = '<button class="igb-ctrl-btn" onclick="igbStartListening()" title="Listen live"><i class="fa-solid fa-headphones"></i></button>'
            + '<button class="igb-ctrl-btn" onclick="igbInterceptCall()" title="Take over call"><i class="fa-solid fa-bolt"></i></button>';
    }
    buttonsHtml += '<button class="igb-ctrl-btn igb-ctrl-hangup" onclick="igbHangupCall()" title="Hang up"><i class="fa-solid fa-phone-slash"></i></button>';
    actionBtns.innerHTML = buttonsHtml;
}

function igbHideCallControls() {
    var actionBtns = igbFind('#igb-call-action-btns');
    if (actionBtns) { actionBtns.style.display = 'none'; }
    var statusBar = igbFind('#igb-call-status-bar');
    if (statusBar) { statusBar.style.display = 'none'; }
    var dispositionPanel = igbFind('#igb-disposition-panel');
    if (dispositionPanel) { dispositionPanel.style.display = 'none'; }
}

function igbShowDispositionPanel() {
    var dispositionPanel = igbFind('#igb-disposition-panel');
    if (!dispositionPanel) { return; }
    dispositionPanel.style.display = 'block';
    dispositionPanel.innerHTML = '<div class="igb-disp-label">Disposition:</div>'
        + '<div class="igb-disp-grid">'
        + '<button class="igb-disp-btn" onclick="igbSaveDisposition(\'connected\')">Connected</button>'
        + '<button class="igb-disp-btn" onclick="igbSaveDisposition(\'voicemail\')">Voicemail</button>'
        + '<button class="igb-disp-btn" onclick="igbSaveDisposition(\'no_answer\')">No Answer</button>'
        + '<button class="igb-disp-btn" onclick="igbSaveDisposition(\'callback\')">Callback</button>'
        + '<button class="igb-disp-btn" onclick="igbSaveDisposition(\'interested\')">Interested</button>'
        + '<button class="igb-disp-btn" onclick="igbSaveDisposition(\'not_interested\')">Not Interested</button>'
        + '<button class="igb-disp-btn igb-disp-dnc" onclick="igbSaveDisposition(\'do_not_call\')">DNC</button>'
        + '</div>';

    var actionBtns = igbFind('#igb-call-action-btns');
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
        var statusData = await igbApiRequest('GET', '/voice/call-status/' + igbCurrentCallSid);
        var callStatus = statusData.status || 'unknown';
        var statusBar = igbFind('#igb-call-status-bar');
        var callInfo = igbActiveCallMap.get(igbCurrentCallSid);
        var contactName = callInfo ? callInfo.name : '';

        var dotClass = 'igb-status-ringing';
        var statusLabel = callStatus;

        if (callStatus === 'in-progress') {
            dotClass = 'igb-status-connected';
            statusLabel = (igbCurrentCallMode === 'ai' ? 'AI talking to ' : 'Connected to ') + contactName;
        } else if (callStatus === 'ringing') {
            statusLabel = 'Ringing ' + contactName + '...';
        } else if (callStatus === 'initiated') {
            statusLabel = 'Dialing ' + contactName + '...';
        }

        if (statusBar) {
            statusBar.innerHTML = '<span class="igb-status-dot ' + dotClass + '"></span> ' + statusLabel;
            if (statusData.duration) {
                statusBar.innerHTML += ' &middot; ' + igbFormatDuration(statusData.duration);
            }
        }

        var terminalStatuses = ['completed', 'busy', 'no-answer', 'failed', 'canceled'];
        if (terminalStatuses.indexOf(callStatus) >= 0) {
            igbStopCallPolling();
            igbStopListenStream();
            if (statusBar) {
                statusBar.innerHTML = '<span class="igb-status-dot igb-status-ended"></span> Call ended (' + callStatus + ')';
            }
            igbShowDispositionPanel();
        }
    } catch (e) {}
}

function igbOpenListenStream(callSid) {
    igbStopListenStream();
    try {
        var wsUrl = igbServerUrl.replace('https://', 'wss://').replace('http://', 'ws://')
            + '/voice/listen-stream?call_sid=' + callSid
            + '&key=' + encodeURIComponent(igbKey);

        igbListenSocket = new WebSocket(wsUrl);
        igbListenSocket.binaryType = 'arraybuffer';

        var audioContext = null;
        igbListenSocket.onopen = function() {
            igbLog('Live listen connected');
            igbShowToast('Listening to call...', 'info');
            try {
                audioContext = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 8000 });
            } catch (audioError) {}
        };
        igbListenSocket.onmessage = function(messageEvent) {
        };
        igbListenSocket.onclose = function() {
            igbLog('Live listen disconnected');
        };
        igbListenSocket.onerror = function() {
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
    var navSelectors = [
        'nav [class*="right-section"]',
        'header [class*="actions"]',
        '[class*="topbar"] [class*="right"]',
        '.hl_topbar .right-section',
        'nav.hl_topbar',
    ];

    var targetEl = null;
    for (var sIdx = 0; sIdx < navSelectors.length; sIdx++) {
        targetEl = igbFind(navSelectors[sIdx]);
        if (targetEl) { break; }
    }

    if (targetEl) {
        targetEl.insertBefore(element, targetEl.firstChild);
    } else {
        var chipContainer = igbFind('#igb-floating-chips');
        if (!chipContainer) {
            chipContainer = igbMakeElement('div', 'igb-floating-chips');
            chipContainer.id = 'igb-floating-chips';
            document.body.appendChild(chipContainer);
        }
        chipContainer.appendChild(element);
    }
}

function igbInjectRecordingPlayers() {
    var callMessageEls = igbFindAll('[class*="call-message"], [class*="call-entry"], [data-message-type="Call"]');

    callMessageEls.forEach(function(callEntry) {
        if (callEntry.querySelector('.igb-audio-player')) { return; }

        var callSid = callEntry.getAttribute('data-call-sid') || '';
        if (!callSid) {
            var sidMatch = callEntry.textContent.match(/CA[a-f0-9]{32}/);
            if (sidMatch) { callSid = sidMatch[0]; }
        }
        if (!callSid) { return; }

        var audioPlayer = igbMakeElement('div', 'igb-audio-player');
        audioPlayer.innerHTML = '<button class="igb-play-btn" onclick="igbPlayRecording(this, \'' + callSid + '\')"><i class="fa-solid fa-play"></i></button>'
            + '<div class="igb-audio-bar"><div class="igb-audio-progress"></div></div>'
            + '<span class="igb-audio-time">0:00</span>'
            + '<button class="igb-transcript-btn" onclick="igbToggleTranscript(this, \'' + callSid + '\')" title="Transcript"><i class="fa-solid fa-file-lines"></i></button>';
        callEntry.appendChild(audioPlayer);
    });
}

async function igbPlayRecording(playBtn, callSid) {
    var playerEl = playBtn.closest('.igb-audio-player');
    var existingAudio = playerEl.querySelector('audio');

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
    var audioEl = document.createElement('audio');
    audioEl.src = igbServerUrl + '/voice/recording/' + callSid + '?key=' + encodeURIComponent(igbKey);
    audioEl.style.display = 'none';
    playerEl.appendChild(audioEl);

    audioEl.onloadedmetadata = function() {
        playerEl.querySelector('.igb-audio-time').textContent = igbFormatDuration(Math.round(audioEl.duration));
    };
    audioEl.ontimeupdate = function() {
        var progressPercent = audioEl.duration ? (audioEl.currentTime / audioEl.duration * 100) : 0;
        var progressBar = playerEl.querySelector('.igb-audio-progress');
        if (progressBar) { progressBar.style.width = progressPercent + '%'; }
        playerEl.querySelector('.igb-audio-time').textContent =
            igbFormatDuration(Math.round(audioEl.currentTime)) + '/' + igbFormatDuration(Math.round(audioEl.duration || 0));
    };
    audioEl.onended = function() {
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
    var playerEl = transcriptBtn.closest('.igb-audio-player');
    var existingPanel = playerEl.querySelector('.igb-transcript-panel');
    if (existingPanel) {
        existingPanel.remove();
        return;
    }

    var transcriptPanel = igbMakeElement('div', 'igb-transcript-panel');
    transcriptPanel.innerHTML = '<div class="igb-loading">Loading transcript...</div>';
    playerEl.appendChild(transcriptPanel);

    try {
        var callData = await igbApiRequest('GET', '/voice/call-status/' + callSid);
        if (callData.transcript) {
            transcriptPanel.innerHTML = '<div class="igb-transcript-text">' + igbSafeText(callData.transcript) + '</div>';
        } else {
            transcriptPanel.innerHTML = '<button class="igb-btn igb-btn-sm" onclick="igbRequestTranscription(\'' + callSid + '\', this)">Transcribe</button>';
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
    var currentPath = window.location.pathname;

    if (currentPath.indexOf('/opportunities') >= 0 || currentPath.indexOf('/pipeline') >= 0) {
        setTimeout(function() {
            igbInjectPipelineButtons();
            igbInjectTemperatureBadges();
        }, 500);
    }

    if (currentPath.indexOf('/conversations') >= 0 || currentPath.indexOf('/messages') >= 0) {
        setTimeout(function() {
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

var igbDomObserver = new MutationObserver(function() {
    clearTimeout(igbDomObserver.debounceTimer);
    igbDomObserver.debounceTimer = setTimeout(igbHandlePageChange, 300);
});

async function igbInit() {
    igbLog('Initializing');

    igbKey = localStorage.getItem(igbKeyStorageKey) || '';
    if (!igbIsKeyValid()) {
        var authSuccess = await igbAuthenticate();
        if (!authSuccess) {
            igbLog('Auth failed, features disabled');
            return;
        }
    }

    try {
        var subscriptionInfo = await igbApiRequest('GET', '/api/ghl/subscription-info');
        igbSubscriptionTier = subscriptionInfo.tier || 'individual';
        igbMaxDialLines = subscriptionInfo.max_lines || 1;
    } catch (e) {}

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
