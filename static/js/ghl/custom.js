/**
 * InsuranceGrokBot — GHL Custom JS
 *
 * This script runs inside GoHighLevel's parent window on every page.
 * It injects: pipeline Dial buttons, temperature badges, AI Reply,
 * intelligence cards, mini dialer popup, AI Minutes chip, stats chip,
 * call recording players, SSE notifications, and bulk call actions.
 *
 * Auth: HMAC-signed request → JWT token stored in localStorage.
 * All API calls use Bearer JWT to IGB server endpoints.
 *
 * Paste this entire file into GHL Developer Portal → Custom JS → JavaScript field.
 */

(function() {
'use strict';

// ── Configuration ───────────────────────────────────────────────────────────
var IGB_SERVER = 'https://app.insurancegrokbot.click';
var SHARED_SECRET = ''; // Set via AppUtils.Utilities.getSharedSecret() at runtime
var JWT_KEY = 'igb_jwt';
var JWT_EXPIRY_KEY = 'igb_jwt_exp';
var POLL_INTERVAL = 2000;     // Call status poll interval (ms)
var BALANCE_POLL = 60000;     // AI Minutes / stats refresh (ms)
var SSE_RECONNECT_BASE = 1000;
var SSE_RECONNECT_MAX = 30000;
var MAX_TOASTS = 3;

// ── State ───────────────────────────────────────────────────────────────────
var _jwt = '';
var _locationId = '';
var _tier = 'individual';
var _maxLines = 1;
var _sseSource = null;
var _sseReconnectDelay = SSE_RECONNECT_BASE;
var _balanceTimer = null;
var _statsTimer = null;
var _dialerPopup = null;
var _dialerQueue = [];
var _dialerIdx = 0;
var _activeCalls = new Map();       // callSid → {contactId, phone, name, status, dialMode}
var _pollTimer = null;
var _listenWs = null;
var _currentCallSid = '';
var _currentDialMode = '';          // 'ai' or 'human'
var _toasts = [];

// ── Helpers ─────────────────────────────────────────────────────────────────

function _log(msg) { console.log('[IGB]', msg); }

function _el(tag, cls, html) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html) e.innerHTML = html;
    return e;
}

function _qs(sel, root) { return (root || document).querySelector(sel); }
function _qsa(sel, root) { return (root || document).querySelectorAll(sel); }

async function _api(method, path, body) {
    var opts = {
        method: method,
        headers: {
            'Authorization': 'Bearer ' + _jwt,
            'Content-Type': 'application/json',
        },
    };
    if (body) opts.body = JSON.stringify(body);
    var resp = await fetch(IGB_SERVER + path, opts);
    if (resp.status === 401) {
        // JWT expired — re-authenticate
        _log('JWT expired, re-authenticating...');
        await _authenticate();
        opts.headers['Authorization'] = 'Bearer ' + _jwt;
        resp = await fetch(IGB_SERVER + path, opts);
    }
    return resp.json();
}

// ── Auth ────────────────────────────────────────────────────────────────────

async function _authenticate() {
    try {
        _locationId = await _getLocationId();
        if (!_locationId) { _log('No locationId found'); return false; }

        var timestamp = Math.floor(Date.now() / 1000);
        var msg = _locationId + timestamp;
        var secret = await _getSharedSecret();
        var signature = await _hmacSha256(secret, msg);

        var resp = await fetch(IGB_SERVER + '/api/ghl/auth/token', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                location_id: _locationId,
                timestamp: timestamp,
                signature: signature,
            }),
        });
        var data = await resp.json();
        if (data.token) {
            _jwt = data.token;
            _tier = data.tier || 'individual';
            localStorage.setItem(JWT_KEY, _jwt);
            localStorage.setItem(JWT_EXPIRY_KEY, String(Date.now() + (data.expires_in || 7200) * 1000));
            _log('Authenticated, tier=' + _tier);
            return true;
        }
        _log('Auth failed: ' + (data.error || 'unknown'));
        return false;
    } catch (e) {
        _log('Auth error: ' + e.message);
        return false;
    }
}

async function _getLocationId() {
    // GHL Custom JS provides AppUtils
    if (typeof AppUtils !== 'undefined' && AppUtils.Utilities && AppUtils.Utilities.getCurrentLocation) {
        try {
            var loc = await AppUtils.Utilities.getCurrentLocation();
            return loc.id || loc.locationId || '';
        } catch (e) { _log('AppUtils.getCurrentLocation failed: ' + e); }
    }
    return '';
}

async function _getSharedSecret() {
    if (typeof AppUtils !== 'undefined' && AppUtils.Utilities && AppUtils.Utilities.getSharedSecret) {
        try { return await AppUtils.Utilities.getSharedSecret(); } catch (e) {}
    }
    return SHARED_SECRET;
}

async function _hmacSha256(secret, message) {
    var enc = new TextEncoder();
    var key = await crypto.subtle.importKey('raw', enc.encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
    var sig = await crypto.subtle.sign('HMAC', key, enc.encode(message));
    return Array.from(new Uint8Array(sig)).map(function(b) { return b.toString(16).padStart(2, '0'); }).join('');
}

function _isJwtValid() {
    var exp = parseInt(localStorage.getItem(JWT_EXPIRY_KEY) || '0');
    return _jwt && Date.now() < exp - 60000; // 1min buffer
}

// ── SSE Notifications ───────────────────────────────────────────────────────

function _startSSE() {
    if (_sseSource) { _sseSource.close(); _sseSource = null; }
    // SSE with JWT — use custom EventSource via fetch since native EventSource doesn't support headers
    _pollSSE();
}

async function _pollSSE() {
    // Lightweight SSE polling fallback — native EventSource can't send auth headers
    // Poll every 5 seconds for new events
    try {
        var data = await _api('GET', '/api/ghl/stream/notifications');
        // Process would need SSE support — for now use polling approach
    } catch (e) {}
    setTimeout(_pollSSE, 5000);
}

function _showToast(html, type) {
    type = type || 'info';
    var toast = _el('div', 'igb-toast igb-toast-' + type);
    toast.innerHTML = html + '<button class="igb-toast-close" onclick="this.parentElement.remove()">&times;</button>';
    var container = _qs('#igb-toast-container');
    if (!container) {
        container = _el('div', '');
        container.id = 'igb-toast-container';
        document.body.appendChild(container);
    }
    container.appendChild(toast);
    _toasts.push(toast);
    // Limit visible toasts
    while (_toasts.length > MAX_TOASTS) {
        var old = _toasts.shift();
        if (old.parentElement) old.remove();
    }
    setTimeout(function() {
        toast.classList.add('igb-toast-fade');
        setTimeout(function() { toast.remove(); }, 300);
    }, 5000);
}

// ── AI Minutes Chip ─────────────────────────────────────────────────────────

var _aiMinutesBalance = 0;

async function _renderAiMinutesChip() {
    var existing = _qs('#igb-minutes-chip');
    if (existing) existing.remove();

    try {
        var data = await _api('GET', '/api/ghl/ai-minutes/balance');
        _aiMinutesBalance = data.balance_minutes || 0;
    } catch (e) { return; }

    var chip = _el('div', 'igb-chip igb-minutes-chip');
    chip.id = 'igb-minutes-chip';
    var colorCls = _aiMinutesBalance > 500 ? 'igb-chip-green' :
                   _aiMinutesBalance > 100 ? 'igb-chip-yellow' : 'igb-chip-red';
    if (_aiMinutesBalance < 50) colorCls += ' igb-chip-pulse';
    chip.classList.add(colorCls);
    chip.innerHTML = '<i class="fa-solid fa-bolt"></i> ' + _formatNum(_aiMinutesBalance) + ' min';
    chip.title = 'AI Minutes — click to buy more';
    chip.onclick = _toggleMinutesPanel;

    _injectIntoTopNav(chip);
}

async function _toggleMinutesPanel() {
    var existing = _qs('#igb-minutes-panel');
    if (existing) { existing.remove(); return; }

    var panel = _el('div', 'igb-dropdown-panel');
    panel.id = 'igb-minutes-panel';
    panel.innerHTML = '<div class="igb-panel-header"><i class="fa-solid fa-bolt"></i> AI Minutes <button class="igb-panel-close" onclick="document.getElementById(\'igb-minutes-panel\').remove()">&times;</button></div><div class="igb-panel-body"><div class="igb-loading">Loading...</div></div>';
    document.body.appendChild(panel);

    // Position near the chip
    var chipEl = _qs('#igb-minutes-chip');
    if (chipEl) {
        var rect = chipEl.getBoundingClientRect();
        panel.style.top = (rect.bottom + 8) + 'px';
        panel.style.right = (window.innerWidth - rect.right) + 'px';
    }

    try {
        var [balance, packages] = await Promise.all([
            _api('GET', '/api/ghl/ai-minutes/balance'),
            _api('GET', '/api/ghl/ai-minutes/packages'),
        ]);
        var bal = balance.balance_minutes || 0;
        var purchased = balance.total_purchased || 0;
        var used = balance.total_used || 0;
        var pct = purchased > 0 ? Math.round(used / purchased * 100) : 0;

        var body = _qs('#igb-minutes-panel .igb-panel-body');
        if (!body) return;

        var html = '<div class="igb-minutes-stats">' +
            '<div class="igb-stat-row"><span>Available</span><strong>' + _formatNum(bal) + ' min</strong></div>' +
            '<div class="igb-stat-row"><span>Purchased</span><span>' + _formatNum(purchased) + '</span></div>' +
            '<div class="igb-stat-row"><span>Used</span><span>' + _formatNum(used) + '</span></div>' +
            '<div class="igb-progress-bar"><div class="igb-progress-fill" style="width:' + Math.min(pct, 100) + '%"></div></div>' +
            '<div class="igb-stat-row igb-text-muted"><span>' + pct + '% used</span></div>' +
            '</div>';

        html += '<div class="igb-section-label">Buy More Minutes</div><div class="igb-packages-grid">';
        var pkgs = packages.packages || [];
        var pkgClasses = { 500: 'igb-pkg-green', 2000: 'igb-pkg-blue', 5000: 'igb-pkg-purple', 10000: 'igb-pkg-gold' };
        pkgs.forEach(function(p) {
            var price = p.price_cents ? '$' + (p.price_cents / 100).toFixed(2) : 'N/A';
            var cls = pkgClasses[p.minutes] || 'igb-pkg-green';
            html += '<div class="igb-package-card ' + cls + '">' +
                '<div class="igb-package-minutes"><i class="fa-solid fa-bolt"></i> ' + _formatNum(p.minutes) + '</div>' +
                '<div class="igb-package-label">' + p.label + '</div>' +
                '<div class="igb-package-price">' + price + '</div>' +
                '<button class="igb-btn igb-btn-sm" onclick="window._igbBuyMinutes(' + p.minutes + ')" ' + (p.available ? '' : 'disabled') + '>Buy</button>' +
                '</div>';
        });
        html += '</div>';
        body.innerHTML = html;
    } catch (e) {
        _log('Minutes panel error: ' + e);
    }
}

window._igbBuyMinutes = async function(minutes) {
    try {
        var data = await _api('POST', '/api/ghl/ai-minutes/checkout', { minutes: minutes });
        if (data.checkout_url) {
            window.open(data.checkout_url, '_blank');
        } else {
            _showToast('Checkout failed: ' + (data.error || 'Unknown error'), 'error');
        }
    } catch (e) {
        _showToast('Checkout error', 'error');
    }
};

// ── Stats Chip ──────────────────────────────────────────────────────────────

async function _renderStatsChip() {
    var existing = _qs('#igb-stats-chip');
    if (existing) existing.remove();

    try {
        var data = await _api('GET', '/api/ghl/stats?period=today');
        var total = data.total_calls || 0;
        var connected = data.connected || 0;

        var chip = _el('div', 'igb-chip igb-stats-chip');
        chip.id = 'igb-stats-chip';
        chip.innerHTML = '<i class="fa-solid fa-chart-simple"></i> ' + total + '/' + connected;
        chip.title = 'Today: ' + total + ' calls, ' + connected + ' connected, ' +
            (data.connect_rate || 0) + '% rate, ' + _formatDuration(data.avg_duration || 0) + ' avg';
        chip.onclick = _toggleStatsPanel;

        _injectIntoTopNav(chip);
    } catch (e) {}
}

async function _toggleStatsPanel() {
    var existing = _qs('#igb-stats-panel');
    if (existing) { existing.remove(); return; }

    var panel = _el('div', 'igb-dropdown-panel');
    panel.id = 'igb-stats-panel';
    panel.innerHTML = '<div class="igb-panel-header"><i class="fa-solid fa-chart-simple"></i> Today\'s Stats <button class="igb-panel-close" onclick="document.getElementById(\'igb-stats-panel\').remove()">&times;</button></div><div class="igb-panel-body"><div class="igb-loading">Loading...</div></div>';
    document.body.appendChild(panel);

    var chipEl = _qs('#igb-stats-chip');
    if (chipEl) {
        var rect = chipEl.getBoundingClientRect();
        panel.style.top = (rect.bottom + 8) + 'px';
        panel.style.right = (window.innerWidth - rect.right) + 'px';
    }

    try {
        var [today, week] = await Promise.all([
            _api('GET', '/api/ghl/stats?period=today'),
            _api('GET', '/api/ghl/stats?period=week'),
        ]);

        var body = _qs('#igb-stats-panel .igb-panel-body');
        if (!body) return;

        body.innerHTML =
            '<div class="igb-minutes-stats">' +
            '<div class="igb-stat-row"><span>Calls Made</span><strong>' + (today.total_calls || 0) + '</strong></div>' +
            '<div class="igb-stat-row"><span>Connected</span><strong>' + (today.connected || 0) + ' (' + (today.connect_rate || 0) + '%)</strong></div>' +
            '<div class="igb-stat-row"><span>Avg Duration</span><span>' + _formatDuration(today.avg_duration || 0) + '</span></div>' +
            '<div class="igb-stat-row"><span>Voicemails</span><span>' + (today.voicemail || 0) + '</span></div>' +
            '<div class="igb-stat-row"><span>No Answer</span><span>' + (today.no_answer || 0) + '</span></div>' +
            '</div>' +
            '<div class="igb-section-label">This Week</div>' +
            '<div class="igb-minutes-stats">' +
            '<div class="igb-stat-row"><span>Calls</span><span>' + (week.total_calls || 0) + '</span></div>' +
            '<div class="igb-stat-row"><span>Connected</span><span>' + (week.connected || 0) + '</span></div>' +
            '<div class="igb-stat-row"><span>Talk Time</span><span>' + _formatDuration(week.total_duration || 0) + '</span></div>' +
            '</div>';
    } catch (e) {}
}

// ── Pipeline Dial Buttons ───────────────────────────────────────────────────

function _injectPipelineButtons() {
    // Find pipeline stage headers and inject Dial buttons
    var headers = _qsa('[class*="pipeline"] [class*="stage-header"], [class*="pipeline"] [class*="column-header"], .board-column .column-header');
    if (!headers.length) {
        // Try alternate GHL pipeline selectors
        headers = _qsa('.opportunity-board .board-column > div:first-child, .pipeline-view .stage-column > div:first-child');
    }

    headers.forEach(function(header) {
        if (header.querySelector('.igb-dial-btn')) return; // already injected

        var btn = _el('button', 'igb-dial-btn');
        btn.innerHTML = '<i class="fa-solid fa-phone"></i> Dial';
        btn.title = 'Dial all contacts in this stage with IGB';
        btn.onclick = function(e) {
            e.stopPropagation();
            _dialPipelineStage(header);
        };
        header.appendChild(btn);
    });
}

function _dialPipelineStage(headerEl) {
    // Extract contact IDs from the opportunity cards in this column
    var column = headerEl.closest('[class*="column"], .board-column, .stage-column');
    if (!column) return;

    var cards = column.querySelectorAll('[class*="opportunity-card"], [class*="deal-card"], .board-card');
    var contacts = [];
    cards.forEach(function(card) {
        // Try to extract contact ID from card's data attributes or links
        var link = card.querySelector('a[href*="/contacts/"]');
        var contactId = '';
        if (link) {
            var match = link.href.match(/\/contacts\/([a-zA-Z0-9]+)/);
            if (match) contactId = match[1];
        }
        if (!contactId) {
            contactId = card.getAttribute('data-contact-id') || card.getAttribute('data-id') || '';
        }

        var nameEl = card.querySelector('[class*="name"], [class*="title"]');
        var name = nameEl ? nameEl.textContent.trim() : 'Unknown';

        if (contactId) {
            contacts.push({ contactId: contactId, name: name });
        }
    });

    if (contacts.length === 0) {
        _showToast('No contacts found in this stage', 'error');
        return;
    }

    // Get stage name from header
    var stageName = headerEl.textContent.replace(/Dial$/i, '').trim();
    _openDialerPopup(contacts, stageName);
}

// ── Temperature Badges ──────────────────────────────────────────────────────

async function _injectTemperatureBadges() {
    var cards = _qsa('[class*="opportunity-card"], [class*="deal-card"], .board-card');
    var contactIds = [];
    var cardMap = {};

    cards.forEach(function(card) {
        if (card.querySelector('.igb-temp-badge')) return;
        var link = card.querySelector('a[href*="/contacts/"]');
        var contactId = '';
        if (link) {
            var match = link.href.match(/\/contacts\/([a-zA-Z0-9]+)/);
            if (match) contactId = match[1];
        }
        if (!contactId) contactId = card.getAttribute('data-contact-id') || '';
        if (contactId) {
            contactIds.push(contactId);
            if (!cardMap[contactId]) cardMap[contactId] = [];
            cardMap[contactId].push(card);
        }
    });

    if (contactIds.length === 0) return;

    try {
        var data = await _api('GET', '/api/ghl/intelligence/bulk?ids=' + contactIds.slice(0, 300).join(','));
        var cached = data.cached || {};
        var uncached = data.uncached || [];

        Object.keys(cached).forEach(function(id) {
            var intel = cached[id];
            var temp = intel.temperature || '';
            var score = intel.score || 0;
            var badge = _createTempBadge(temp, score);
            (cardMap[id] || []).forEach(function(card) {
                if (!card.querySelector('.igb-temp-badge')) {
                    card.style.position = 'relative';
                    card.appendChild(badge.cloneNode(true));
                }
            });
        });

        // Queue uncached for background analysis
        if (uncached.length > 0) {
            _api('POST', '/voice/contact-intelligence-analyze', { contact_ids: uncached.slice(0, 5) }).catch(function() {});
        }
    } catch (e) { _log('Badge error: ' + e); }
}

function _createTempBadge(temp, score) {
    var badge = _el('div', 'igb-temp-badge');
    var icons = { hot: 'fa-fire', warm: 'fa-temperature-half', cool: 'fa-snowflake', cold: 'fa-icicles' };
    var icon = icons[temp] || 'fa-circle';
    var colorClasses = { hot: 'igb-color-hot', warm: 'igb-color-warm', cool: 'igb-color-cool', cold: 'igb-color-cold' };
    badge.innerHTML = '<i class="fa-solid ' + icon + ' ' + (colorClasses[temp] || 'igb-color-cold') + '"></i>';
    badge.title = (temp || 'unknown') + ' | Score: ' + score;
    if (temp === 'hot') badge.classList.add('igb-temp-hot');
    return badge;
}

// ── AI Reply Button (Conversations) ─────────────────────────────────────────

function _injectAiReplyButton() {
    // Detect conversation compose area
    var compose = _qs('[class*="message-composer"], [class*="compose"], [class*="reply-box"], .hl_message-composer');
    if (!compose || compose.querySelector('.igb-ai-reply-btn')) return;

    var contactId = _extractContactIdFromUrl();
    if (!contactId) return;

    var btn = _el('button', 'igb-ai-reply-btn');
    btn.innerHTML = '<i class="fa-solid fa-robot"></i> AI Reply';
    btn.title = 'Generate AI reply draft with InsuranceGrokBot';
    btn.onclick = function() { _generateAiReply(contactId, compose); };
    compose.appendChild(btn);
}

async function _generateAiReply(contactId, composeEl) {
    var btn = composeEl.querySelector('.igb-ai-reply-btn');
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Generating...'; }

    try {
        var data = await _api('POST', '/api/ghl/ai-suggest/' + contactId);
        if (data.draft) {
            _showAiReplyPreview(data.draft, contactId, composeEl);
        } else {
            _showToast('AI Reply: ' + (data.error || 'No draft generated'), 'error');
        }
    } catch (e) {
        _showToast('AI Reply failed', 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-robot"></i> AI Reply'; }
    }
}

function _showAiReplyPreview(draft, contactId, composeEl) {
    var existing = _qs('#igb-ai-preview');
    if (existing) existing.remove();

    var preview = _el('div', 'igb-ai-preview');
    preview.id = 'igb-ai-preview';
    preview.innerHTML =
        '<div class="igb-preview-header">AI Draft <button class="igb-panel-close" onclick="document.getElementById(\'igb-ai-preview\').remove()">&times;</button></div>' +
        '<textarea class="igb-preview-text" rows="4">' + _escHtml(draft) + '</textarea>' +
        '<div class="igb-preview-actions">' +
        '<button class="igb-btn igb-btn-primary" id="igb-send-draft">Send</button>' +
        '<button class="igb-btn igb-btn-secondary" onclick="document.getElementById(\'igb-ai-preview\').remove()">Cancel</button>' +
        '</div>';

    // Position above compose area
    var rect = composeEl.getBoundingClientRect();
    preview.style.bottom = (window.innerHeight - rect.top + 8) + 'px';
    preview.style.left = rect.left + 'px';
    preview.style.width = rect.width + 'px';
    document.body.appendChild(preview);

    _qs('#igb-send-draft').onclick = async function() {
        var text = preview.querySelector('textarea').value.trim();
        if (!text) return;
        this.disabled = true;
        this.textContent = 'Sending...';
        try {
            var resp = await _api('POST', '/api/ghl/send-sms/' + contactId, { message: text });
            if (resp.status === 'sent') {
                _showToast('Message sent', 'success');
                preview.remove();
            } else {
                _showToast('Send failed: ' + (resp.error || ''), 'error');
            }
        } catch (e) {
            _showToast('Send error', 'error');
        }
    };
}

// ── Intelligence Card (Contact Detail) ──────────────────────────────────────

async function _injectIntelligenceCard() {
    var contactId = _extractContactIdFromUrl();
    if (!contactId) return;

    // Only inject on contact detail pages
    if (!window.location.pathname.match(/\/contacts\/detail\//)) return;

    // Don't duplicate
    if (_qs('#igb-intelligence-card')) return;

    var card = _el('div', 'igb-intelligence-card');
    card.id = 'igb-intelligence-card';
    card.innerHTML = '<div class="igb-intel-shimmer"></div>';

    // Insert into the page — try to find the right sidebar area
    var sidebar = _qs('[class*="contact-detail-sidebar"], [class*="right-panel"], .contact-details aside');
    if (sidebar) {
        sidebar.prepend(card);
    } else {
        // Fallback: floating card
        card.classList.add('igb-intel-floating');
        document.body.appendChild(card);
    }

    try {
        var data = await _api('GET', '/api/ghl/intelligence/' + contactId);
        if (data.status === 'ok' && data.intelligence) {
            var intel = data.intelligence;
            var temp = intel.temperature || 'unknown';
            var icons = { hot: 'fa-fire', warm: 'fa-temperature-half', cool: 'fa-snowflake', cold: 'fa-icicles' };

            card.innerHTML =
                '<div class="igb-intel-header">' +
                '<span class="igb-intel-temp ' + ({ hot: 'igb-color-hot', warm: 'igb-color-warm', cool: 'igb-color-cool', cold: 'igb-color-cold' }[temp] || 'igb-color-cold') + '"><i class="fa-solid ' + (icons[temp] || 'fa-circle') + '"></i> ' + _capitalize(temp) + '</span>' +
                '<span class="igb-intel-score">Score: ' + (intel.score || 0) + '</span>' +
                '</div>' +
                '<div class="igb-intel-summary">' + _escHtml(intel.summary || '') + '</div>' +
                '<div class="igb-intel-actions">' +
                (intel.actions || []).map(function(a) {
                    return '<div class="igb-intel-action"><i class="fa-solid ' + (a.icon || 'fa-circle') + '"></i> ' + _escHtml(a.text || a.action || '') + '</div>';
                }).join('') +
                '</div>' +
                '<div class="igb-intel-buttons">' +
                '<button class="igb-btn igb-btn-sm igb-btn-primary" onclick="window._igbDialContact(\'' + contactId + '\')"><i class="fa-solid fa-phone"></i> Dial</button>' +
                '<button class="igb-btn igb-btn-sm" onclick="window._igbAiReplyContact(\'' + contactId + '\')"><i class="fa-solid fa-robot"></i> AI Reply</button>' +
                '</div>';
        } else {
            card.innerHTML = '<div class="igb-intel-empty"><i class="fa-solid fa-brain"></i> No AI intelligence yet</div>';
        }
    } catch (e) {
        card.innerHTML = '<div class="igb-intel-empty">Intelligence unavailable</div>';
    }
}

window._igbDialContact = function(contactId) {
    _openDialerPopup([{ contactId: contactId, name: '' }], 'Single Contact');
};
window._igbAiReplyContact = function(contactId) {
    var compose = _qs('[class*="message-composer"], [class*="compose"]');
    if (compose) _generateAiReply(contactId, compose);
};

// ── Bulk Call Action (Contacts Page) ────────────────────────────────────────

function _injectBulkCallButton() {
    // Detect bulk action bar on contacts list page
    var bulkBar = _qs('[class*="bulk-actions"], [class*="selection-actions"], .bulk-action-bar');
    if (!bulkBar || bulkBar.querySelector('.igb-bulk-call-btn')) return;

    var btn = _el('button', 'igb-bulk-call-btn igb-btn');
    btn.innerHTML = '<i class="fa-solid fa-phone"></i> Call with IGB';
    btn.onclick = function() {
        var checkboxes = _qsa('input[type="checkbox"]:checked');
        var contacts = [];
        checkboxes.forEach(function(cb) {
            var row = cb.closest('tr, [class*="contact-row"]');
            if (!row) return;
            var link = row.querySelector('a[href*="/contacts/"]');
            var contactId = '';
            if (link) {
                var match = link.href.match(/\/contacts\/([a-zA-Z0-9]+)/);
                if (match) contactId = match[1];
            }
            var nameEl = row.querySelector('[class*="name"]');
            if (contactId) {
                contacts.push({ contactId: contactId, name: nameEl ? nameEl.textContent.trim() : '' });
            }
        });
        if (contacts.length > 0) {
            _openDialerPopup(contacts, 'Selected Contacts');
        } else {
            _showToast('No contacts selected', 'error');
        }
    };
    bulkBar.appendChild(btn);
}

// ── Mini Dialer Popup ───────────────────────────────────────────────────────

function _openDialerPopup(contacts, stageName) {
    _closeDialerPopup();
    _dialerQueue = contacts;
    _dialerIdx = 0;
    _activeCalls.clear();

    var popup = _el('div', 'igb-dialer-popup');
    popup.id = 'igb-dialer-popup';
    popup.innerHTML = _buildDialerHtml(stageName);
    document.body.appendChild(popup);
    _dialerPopup = popup;

    _renderDialerQueue();
    _renderCurrentContact();
}

function _closeDialerPopup() {
    if (_dialerPopup) { _dialerPopup.remove(); _dialerPopup = null; }
    _stopPolling();
    _stopListenStream();
    _activeCalls.clear();
    _currentCallSid = '';
}

function _buildDialerHtml(stageName) {
    var linesHtml = '';
    if (_maxLines > 1) {
        linesHtml = '<div class="igb-dialer-lines">Lines: ';
        for (var i = 1; i <= 4; i++) {
            var enabled = i <= _maxLines;
            linesHtml += '<button class="igb-line-btn' + (i === 1 ? ' active' : '') + (!enabled ? ' disabled' : '') + '" data-lines="' + i + '"' + (!enabled ? ' disabled title="Upgrade to Pro Dialer"' : '') + '>' + i + '</button> ';
        }
        linesHtml += '<span class="igb-active-count">Active: 0</span></div>';
    }

    return '<div class="igb-dialer-header">' +
        '<span>IGB Dialer &mdash; ' + _escHtml(stageName) + '</span>' +
        '<button class="igb-panel-close" onclick="window._igbCloseDialer()">&times;</button>' +
        '</div>' +
        '<div class="igb-dialer-body">' +
        '<div class="igb-dialer-queue-info">Queue: ' + _dialerQueue.length + ' contacts</div>' +
        '<div id="igb-current-contact" class="igb-current-contact"></div>' +
        '<div class="igb-dialer-controls">' +
        '<button class="igb-btn igb-btn-ai" id="igb-btn-ai" onclick="window._igbDial(\'ai\')"><i class="fa-solid fa-robot"></i> AI Call</button>' +
        '<button class="igb-btn igb-btn-human" id="igb-btn-human" onclick="window._igbDial(\'human\')"><i class="fa-solid fa-phone"></i> Human Call</button>' +
        '</div>' +
        linesHtml +
        '<div id="igb-call-status" class="igb-call-status" style="display:none"></div>' +
        '<div id="igb-call-controls" class="igb-call-controls" style="display:none"></div>' +
        '<div id="igb-disposition" class="igb-disposition" style="display:none"></div>' +
        '<div class="igb-dialer-queue-header">Up Next</div>' +
        '<div id="igb-queue-list" class="igb-queue-list"></div>' +
        '</div>';
}

function _renderCurrentContact() {
    var el = _qs('#igb-current-contact');
    if (!el || _dialerIdx >= _dialerQueue.length) {
        if (el) el.innerHTML = '<div class="igb-text-muted">Queue complete</div>';
        return;
    }
    var c = _dialerQueue[_dialerIdx];
    el.innerHTML = '<div class="igb-contact-name">' + _escHtml(c.name || 'Contact') + '</div>' +
        '<button class="igb-btn igb-btn-sm igb-btn-secondary" onclick="window._igbSkip()">Skip</button>';
}

function _renderDialerQueue() {
    var el = _qs('#igb-queue-list');
    if (!el) return;
    var html = '';
    for (var i = _dialerIdx + 1; i < Math.min(_dialerIdx + 6, _dialerQueue.length); i++) {
        html += '<div class="igb-queue-item">' + (i + 1) + '. ' + _escHtml(_dialerQueue[i].name || 'Contact') + '</div>';
    }
    var remaining = _dialerQueue.length - _dialerIdx - 6;
    if (remaining > 0) html += '<div class="igb-queue-item igb-text-muted">...' + remaining + ' more</div>';
    el.innerHTML = html;
}

// ── Dialer Call Actions ─────────────────────────────────────────────────────

window._igbCloseDialer = function() { _closeDialerPopup(); };

window._igbDial = async function(mode) {
    if (_dialerIdx >= _dialerQueue.length) return;
    var contact = _dialerQueue[_dialerIdx];
    _currentDialMode = mode;

    // Disable dial buttons
    var btnAi = _qs('#igb-btn-ai');
    var btnHuman = _qs('#igb-btn-human');
    if (btnAi) btnAi.disabled = true;
    if (btnHuman) btnHuman.disabled = true;

    // Show status
    var statusEl = _qs('#igb-call-status');
    if (statusEl) {
        statusEl.style.display = 'block';
        statusEl.innerHTML = '<span class="igb-status-dot igb-status-ringing"></span> Dialing ' + _escHtml(contact.name) + '...';
    }

    try {
        var data = await _api('POST', '/voice/dial', {
            contact_id: contact.contactId,
            dial_mode: mode,
        });
        if (data.call_sid) {
            _currentCallSid = data.call_sid;
            _activeCalls.set(data.call_sid, {
                contactId: contact.contactId,
                name: contact.name,
                status: 'initiated',
                dialMode: mode,
            });
            _startPolling();
            _showCallControls(mode);
        } else {
            _showToast('Dial failed: ' + (data.error || 'Unknown error'), 'error');
            if (btnAi) btnAi.disabled = false;
            if (btnHuman) btnHuman.disabled = false;
        }
    } catch (e) {
        _showToast('Dial error: ' + e.message, 'error');
        if (btnAi) btnAi.disabled = false;
        if (btnHuman) btnHuman.disabled = false;
    }
};

window._igbSkip = function() {
    _dialerIdx++;
    _renderCurrentContact();
    _renderDialerQueue();
};

window._igbHangup = async function() {
    if (!_currentCallSid) return;
    try { await _api('POST', '/voice/hangup', { call_sid: _currentCallSid }); }
    catch (e) {}
};

window._igbListen = function() {
    if (_currentDialMode !== 'ai' || !_currentCallSid) return;
    _startListenStream(_currentCallSid);
};

window._igbStopListen = function() { _stopListenStream(); };

window._igbTakeover = async function() {
    if (!_currentCallSid) return;
    try {
        var data = await _api('POST', '/voice/takeover', { call_sid: _currentCallSid, location_id: _locationId });
        if (data.status === 'ok' || data.success) {
            _showToast('Call intercepted — you are now on the line', 'success');
            _stopListenStream();
            _currentDialMode = 'human'; // Agent is now handling
            _showCallControls('human');
        } else {
            _showToast('Intercept: ' + (data.error || 'failed'), 'error');
        }
    } catch (e) {
        _showToast('Intercept error', 'error');
    }
};

window._igbDisposition = async function(disp) {
    if (!_currentCallSid) return;
    try {
        await _api('POST', '/voice/call-disposition', { call_sid: _currentCallSid, disposition: disp });
        _showToast('Disposition: ' + disp, 'success');
    } catch (e) {}
    // Advance to next
    _currentCallSid = '';
    _dialerIdx++;
    _renderCurrentContact();
    _renderDialerQueue();
    _hideCallControls();
    // Re-enable dial buttons
    var btnAi = _qs('#igb-btn-ai');
    var btnHuman = _qs('#igb-btn-human');
    if (btnAi) btnAi.disabled = false;
    if (btnHuman) btnHuman.disabled = false;
};

function _showCallControls(mode) {
    var el = _qs('#igb-call-controls');
    if (!el) return;
    el.style.display = 'flex';
    var html = '';
    if (mode === 'ai') {
        html = '<button class="igb-ctrl-btn" onclick="window._igbListen()" title="Listen"><i class="fa-solid fa-headphones"></i></button>' +
            '<button class="igb-ctrl-btn" onclick="window._igbTakeover()" title="Intercept"><i class="fa-solid fa-bolt"></i></button>';
    }
    html += '<button class="igb-ctrl-btn igb-ctrl-hangup" onclick="window._igbHangup()" title="Hangup"><i class="fa-solid fa-phone-slash"></i></button>';
    el.innerHTML = html;
}

function _hideCallControls() {
    var el = _qs('#igb-call-controls');
    if (el) el.style.display = 'none';
    var status = _qs('#igb-call-status');
    if (status) status.style.display = 'none';
    var disp = _qs('#igb-disposition');
    if (disp) disp.style.display = 'none';
}

function _showDispositionUI() {
    var el = _qs('#igb-disposition');
    if (!el) return;
    el.style.display = 'block';
    el.innerHTML = '<div class="igb-disp-label">Disposition:</div>' +
        '<div class="igb-disp-grid">' +
        '<button class="igb-disp-btn" onclick="window._igbDisposition(\'connected\')">Connected</button>' +
        '<button class="igb-disp-btn" onclick="window._igbDisposition(\'voicemail\')">Voicemail</button>' +
        '<button class="igb-disp-btn" onclick="window._igbDisposition(\'no_answer\')">No Answer</button>' +
        '<button class="igb-disp-btn" onclick="window._igbDisposition(\'callback\')">Callback</button>' +
        '<button class="igb-disp-btn" onclick="window._igbDisposition(\'interested\')">Interested</button>' +
        '<button class="igb-disp-btn" onclick="window._igbDisposition(\'not_interested\')">Not Interested</button>' +
        '<button class="igb-disp-btn igb-disp-dnc" onclick="window._igbDisposition(\'do_not_call\')">DNC</button>' +
        '</div>';
    var controls = _qs('#igb-call-controls');
    if (controls) controls.style.display = 'none';
}

// ── Call Status Polling ─────────────────────────────────────────────────────

function _startPolling() {
    _stopPolling();
    _pollTimer = setInterval(_pollCallStatus, POLL_INTERVAL);
}

function _stopPolling() {
    if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
}

async function _pollCallStatus() {
    if (!_currentCallSid) { _stopPolling(); return; }

    try {
        var data = await _api('GET', '/voice/call-status/' + _currentCallSid);
        var status = data.status || 'unknown';
        var statusEl = _qs('#igb-call-status');
        var contact = _activeCalls.get(_currentCallSid);
        var name = contact ? contact.name : '';

        var dotClass = 'igb-status-ringing';
        var label = status;
        if (status === 'in-progress') { dotClass = 'igb-status-connected'; label = (_currentDialMode === 'ai' ? 'AI talking to ' : 'Connected to ') + name; }
        else if (status === 'ringing') { label = 'Ringing ' + name + '...'; }
        else if (status === 'initiated') { label = 'Dialing ' + name + '...'; }

        if (statusEl) {
            statusEl.innerHTML = '<span class="igb-status-dot ' + dotClass + '"></span> ' + label;
            if (data.duration) statusEl.innerHTML += ' &middot; ' + _formatDuration(data.duration);
        }

        // Terminal states
        if (['completed', 'busy', 'no-answer', 'failed', 'canceled'].indexOf(status) >= 0) {
            _stopPolling();
            _stopListenStream();
            if (statusEl) {
                statusEl.innerHTML = '<span class="igb-status-dot igb-status-ended"></span> Call ended (' + status + ')';
            }
            _showDispositionUI();
        }
    } catch (e) {}
}

// ── Live Listen WebSocket ───────────────────────────────────────────────────

function _startListenStream(callSid) {
    _stopListenStream();
    try {
        var wsUrl = IGB_SERVER.replace('https://', 'wss://').replace('http://', 'ws://') + '/voice/listen-stream?call_sid=' + callSid + '&token=' + encodeURIComponent(_jwt);
        _listenWs = new WebSocket(wsUrl);
        _listenWs.binaryType = 'arraybuffer';

        var audioCtx = null;
        _listenWs.onopen = function() {
            _log('Listen stream connected');
            _showToast('Listening to call...', 'info');
            try { audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 8000 }); } catch (e) {}
        };
        _listenWs.onmessage = function(evt) {
            // Audio frames would be decoded here — simplified for now
            // In production, decode mulaw 8kHz and play via AudioContext
        };
        _listenWs.onclose = function() { _log('Listen stream closed'); };
        _listenWs.onerror = function() { _log('Listen stream error'); };
    } catch (e) {
        _log('Listen error: ' + e);
    }
}

function _stopListenStream() {
    if (_listenWs) {
        _listenWs.onclose = null;
        _listenWs.close();
        _listenWs = null;
    }
}

// ── Top Nav Injection ───────────────────────────────────────────────────────

function _injectIntoTopNav(element) {
    // Find GHL's top navigation bar and inject chips
    var selectors = [
        'nav [class*="right-section"]',
        'header [class*="actions"]',
        '[class*="topbar"] [class*="right"]',
        '.hl_topbar .right-section',
        'nav.hl_topbar',
    ];
    var target = null;
    for (var i = 0; i < selectors.length; i++) {
        target = _qs(selectors[i]);
        if (target) break;
    }
    if (target) {
        target.insertBefore(element, target.firstChild);
    } else {
        // Fallback: create floating chip container
        var container = _qs('#igb-floating-chips');
        if (!container) {
            container = _el('div', 'igb-floating-chips');
            container.id = 'igb-floating-chips';
            document.body.appendChild(container);
        }
        container.appendChild(element);
    }
}

// ── Recording Playback in Conversations ─────────────────────────────────────

function _injectRecordingPlayers() {
    // Find call entries in conversation threads
    var callEntries = _qsa('[class*="call-message"], [class*="call-entry"], [data-message-type="Call"]');
    callEntries.forEach(function(entry) {
        if (entry.querySelector('.igb-audio-player')) return;
        // Try to extract call_sid from entry
        var callSid = entry.getAttribute('data-call-sid') || '';
        if (!callSid) {
            // Try to find in the entry text or link
            var text = entry.textContent;
            var match = text.match(/CA[a-f0-9]{32}/);
            if (match) callSid = match[0];
        }
        if (!callSid) return;

        var player = _el('div', 'igb-audio-player');
        player.innerHTML =
            '<button class="igb-play-btn" data-sid="' + callSid + '" onclick="window._igbPlayRecording(this, \'' + callSid + '\')"><i class="fa-solid fa-play"></i></button>' +
            '<div class="igb-audio-bar"><div class="igb-audio-progress"></div></div>' +
            '<span class="igb-audio-time">0:00</span>' +
            '<button class="igb-transcript-btn" onclick="window._igbToggleTranscript(this, \'' + callSid + '\')" title="Transcript"><i class="fa-solid fa-file-lines"></i></button>';
        entry.appendChild(player);
    });
}

window._igbPlayRecording = async function(btn, callSid) {
    var player = btn.closest('.igb-audio-player');
    var existing = player.querySelector('audio');
    if (existing) {
        if (existing.paused) { existing.play(); btn.innerHTML = '<i class="fa-solid fa-pause"></i>'; }
        else { existing.pause(); btn.innerHTML = '<i class="fa-solid fa-play"></i>'; }
        return;
    }
    btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
    var audio = document.createElement('audio');
    audio.src = IGB_SERVER + '/voice/recording/' + callSid + '?token=' + encodeURIComponent(_jwt);
    audio.style.display = 'none';
    player.appendChild(audio);
    audio.onloadedmetadata = function() {
        player.querySelector('.igb-audio-time').textContent = _formatDuration(Math.round(audio.duration));
    };
    audio.ontimeupdate = function() {
        var pct = audio.duration ? (audio.currentTime / audio.duration * 100) : 0;
        var bar = player.querySelector('.igb-audio-progress');
        if (bar) bar.style.width = pct + '%';
        player.querySelector('.igb-audio-time').textContent = _formatDuration(Math.round(audio.currentTime)) + '/' + _formatDuration(Math.round(audio.duration || 0));
    };
    audio.onended = function() { btn.innerHTML = '<i class="fa-solid fa-play"></i>'; };
    try { await audio.play(); btn.innerHTML = '<i class="fa-solid fa-pause"></i>'; }
    catch (e) { btn.innerHTML = '<i class="fa-solid fa-play"></i>'; }
};

window._igbToggleTranscript = async function(btn, callSid) {
    var player = btn.closest('.igb-audio-player');
    var existing = player.querySelector('.igb-transcript-panel');
    if (existing) { existing.remove(); return; }

    var panel = _el('div', 'igb-transcript-panel');
    panel.innerHTML = '<div class="igb-loading">Loading transcript...</div>';
    player.appendChild(panel);

    // For now, show a transcribe button — transcripts are fetched on demand
    try {
        var data = await _api('GET', '/voice/call-status/' + callSid);
        if (data.transcript) {
            panel.innerHTML = '<div class="igb-transcript-text">' + _escHtml(data.transcript) + '</div>';
        } else {
            panel.innerHTML = '<button class="igb-btn igb-btn-sm" onclick="window._igbTranscribe(\'' + callSid + '\', this)">Transcribe</button>';
        }
    } catch (e) {
        panel.innerHTML = '<div class="igb-text-muted">Transcript unavailable</div>';
    }
};

window._igbTranscribe = async function(callSid, btn) {
    btn.disabled = true;
    btn.textContent = 'Transcribing...';
    try {
        await _api('POST', '/voice/transcribe-recording', { call_sid: callSid });
        _showToast('Transcription started — check back in a moment', 'info');
    } catch (e) {
        _showToast('Transcription failed', 'error');
    }
};

// ── Utility Functions ───────────────────────────────────────────────────────

function _formatNum(n) {
    return n.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

function _formatDuration(seconds) {
    if (!seconds) return '0:00';
    var m = Math.floor(seconds / 60);
    var s = Math.floor(seconds % 60);
    return m + ':' + (s < 10 ? '0' : '') + s;
}

function _escHtml(str) {
    var d = document.createElement('div');
    d.textContent = str;
    return d.innerHTML;
}

function _capitalize(s) { return s ? s.charAt(0).toUpperCase() + s.slice(1) : ''; }

function _extractContactIdFromUrl() {
    var match = window.location.pathname.match(/\/contacts\/(?:detail\/)?([a-zA-Z0-9]+)/);
    return match ? match[1] : '';
}

// ── Page Detection & MutationObserver ───────────────────────────────────────

function _onPageChange() {
    var path = window.location.pathname;

    // Pipeline / Opportunities page
    if (path.includes('/opportunities') || path.includes('/pipeline')) {
        setTimeout(function() {
            _injectPipelineButtons();
            _injectTemperatureBadges();
        }, 500);
    }

    // Conversation page
    if (path.includes('/conversations') || path.includes('/messages')) {
        setTimeout(function() {
            _injectAiReplyButton();
            _injectRecordingPlayers();
        }, 500);
    }

    // Contact detail page
    if (path.includes('/contacts/detail/')) {
        setTimeout(_injectIntelligenceCard, 500);
    }

    // Contacts list page
    if (path.match(/\/contacts\/?$/) || path.includes('/contacts?')) {
        setTimeout(_injectBulkCallButton, 500);
    }
}

// MutationObserver for dynamic content
var _observer = new MutationObserver(function(mutations) {
    // Debounce
    clearTimeout(_observer._timer);
    _observer._timer = setTimeout(function() {
        _onPageChange();
    }, 300);
});

// ── Init ────────────────────────────────────────────────────────────────────

async function _init() {
    _log('Initializing InsuranceGrokBot GHL integration...');

    // Check for cached JWT
    _jwt = localStorage.getItem(JWT_KEY) || '';
    if (!_isJwtValid()) {
        var ok = await _authenticate();
        if (!ok) { _log('Authentication failed — IGB features disabled'); return; }
    }

    // Get subscription info
    try {
        var info = await _api('GET', '/api/ghl/subscription-info');
        _tier = info.tier || 'individual';
        _maxLines = info.max_lines || 1;
    } catch (e) {}

    // Inject top nav chips
    await Promise.all([
        _renderStatsChip(),
        _renderAiMinutesChip(),
    ]);

    // Start periodic refresh
    _balanceTimer = setInterval(_renderAiMinutesChip, BALANCE_POLL);
    _statsTimer = setInterval(_renderStatsChip, BALANCE_POLL);

    // Start SSE for notifications
    _startSSE();

    // Initial page injection
    _onPageChange();

    // Listen for GHL SPA navigation
    if (typeof window.addEventListener === 'function') {
        window.addEventListener('routeChangeEvent', _onPageChange);
        // Also watch for popstate (browser back/forward)
        window.addEventListener('popstate', _onPageChange);
    }

    // MutationObserver for dynamic content
    _observer.observe(document.body, { childList: true, subtree: true });

    _log('IGB GHL integration ready (tier=' + _tier + ', maxLines=' + _maxLines + ')');
}

// Wait for DOM ready then init
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _init);
} else {
    _init();
}

})();
