// assistant.js — In-dashboard AI Agent Assistant
// Persistent footer widget with tool-calling capabilities.
// Can search contacts, book appointments, send SMS, navigate tabs, check stats.

(function() {
    'use strict';

    var _open = false;
    var _history = [];
    var _loading = false;
    var _pendingError = null;
    var _initialized = false;
    var STORAGE_KEY = 'igb_assistant_history';

    // ── Toggle panel ─────────────────────────────────────────────────────────

    window.assistantToggle = function() {
        var panel = document.getElementById('assistantPanel');
        var fab = document.getElementById('assistantFab');
        if (!panel || !fab) return;

        _open = !_open;
        panel.classList.toggle('assistant-panel--open', _open);
        fab.classList.toggle('assistant-fab--hidden', _open);

        if (_open && !_initialized) {
            _initialized = true;
            // Restore history or show greeting
            if (!_restoreHistory()) {
                _sendMessage('INIT_CHAT', true);
            }
        }

        if (_open) {
            var input = document.getElementById('assistantInput');
            if (input) setTimeout(function() { input.focus(); }, 100);

            // Clear error badge
            var badge = document.getElementById('assistantErrorBadge');
            if (badge) badge.classList.remove('assistant-error-badge--visible');
        }
    };

    // ── Send message ─────────────────────────────────────────────────────────

    window.assistantSend = function() {
        _sendMessage();
    };

    function _sendMessage(textOverride, isSystem) {
        if (_loading) return;

        var input = document.getElementById('assistantInput');
        var text = textOverride || (input ? input.value.trim() : '');
        if (!text) return;

        if (!isSystem) {
            _renderMessage('user', text);
            if (input) input.value = '';
            _autoResize(input);
        }

        _loading = true;
        _showTyping(true);

        var body = {
            message: text,
            history: _history.slice(-20),
        };

        // Attach error context if auto-surfaced
        if (_pendingError) {
            body.error_context = _pendingError;
            _pendingError = null;
        }

        fetch('/api/assistant/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            _loading = false;
            _showTyping(false);

            if (data.text) {
                _renderMessage('assistant', data.text);
            }
            if (data.navigate && typeof sidebarNavigate === 'function') {
                // Navigate to the tab
                var btn = document.getElementById('sbn' + data.navigate.charAt(0).toUpperCase() + data.navigate.slice(1));
                if (btn) {
                    sidebarNavigate(data.navigate, btn);
                } else {
                    // Fallback: try clicking sidebar nav by data-tab attribute
                    var navBtn = document.querySelector('.sb-nav-item[onclick*="' + data.navigate + '"]');
                    if (navBtn) navBtn.click();
                }
            }
        })
        .catch(function(err) {
            _loading = false;
            _showTyping(false);
            _renderMessage('assistant', 'Connection error. Try again in a moment.');
        });
    }

    // ── Keyboard handling ────────────────────────────────────────────────────

    window.assistantKeydown = function(e) {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            _sendMessage();
        }
    };

    // Global shortcut: Ctrl+/ or Cmd+/
    document.addEventListener('keydown', function(e) {
        if ((e.ctrlKey || e.metaKey) && e.key === '/') {
            e.preventDefault();
            assistantToggle();
        }
    });

    // ── Render message ───────────────────────────────────────────────────────

    function _renderMessage(role, text, skipSave) {
        var container = document.getElementById('assistantMessages');
        if (!container || !text) return;

        var div = document.createElement('div');
        div.className = 'assistant-msg assistant-msg--' + role;

        var bubble = document.createElement('div');
        bubble.className = 'assistant-bubble';

        if (role === 'assistant') {
            bubble.innerHTML = _renderMarkdown(text);
        } else {
            bubble.textContent = text;
        }

        div.appendChild(bubble);
        container.appendChild(div);
        container.scrollTop = container.scrollHeight;

        // Track history
        _history.push({ role: role, content: text });
        if (!skipSave) _saveHistory();
    }

    // ── Markdown-lite renderer ───────────────────────────────────────────────

    function _renderMarkdown(text) {
        var s = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        s = s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
        s = s.replace(/(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)/g, '<em>$1</em>');
        s = s.replace(/`([^`]+)`/g, '<code style="background:rgba(255,255,255,0.06);padding:1px 5px;border-radius:4px;font-size:0.85em">$1</code>');
        s = s.replace(/^(\d+)\.\s+(.+)$/gm, '<div style="padding-left:14px;margin:2px 0"><strong>$1.</strong> $2</div>');
        s = s.replace(/^[-•]\s+(.+)$/gm, '<div style="padding-left:14px;margin:2px 0">&#8226; $1</div>');
        s = s.replace(/\n/g, '<br>');
        return s;
    }

    // ── Typing indicator ─────────────────────────────────────────────────────

    function _showTyping(show) {
        var el = document.getElementById('assistantTyping');
        if (el) el.style.display = show ? 'flex' : 'none';
    }

    // ── Auto-resize textarea ─────────────────────────────────────────────────

    function _autoResize(textarea) {
        if (!textarea) return;
        textarea.style.height = 'auto';
        textarea.style.height = Math.min(textarea.scrollHeight, 100) + 'px';
    }

    // Wire up auto-resize
    var input = document.getElementById('assistantInput');
    if (input) {
        input.addEventListener('input', function() { _autoResize(this); });
    }

    // ── History persistence (localStorage) ───────────────────────────────────

    function _saveHistory() {
        try {
            var trimmed = _history.slice(-40);
            localStorage.setItem(STORAGE_KEY, JSON.stringify(trimmed));
        } catch(e) {}
    }

    function _restoreHistory() {
        try {
            var saved = localStorage.getItem(STORAGE_KEY);
            if (!saved) return false;
            var msgs = JSON.parse(saved);
            if (!msgs || !msgs.length) return false;
            _history = msgs;
            msgs.forEach(function(m) {
                _renderMessage(m.role, m.content, true);
            });
            return true;
        } catch(e) { return false; }
    }

    // ── Error interception (monkey-patch fetch) ──────────────────────────────

    (function() {
        var _origFetch = window.fetch;
        window.fetch = function(url, opts) {
            return _origFetch.apply(this, arguments).then(function(response) {
                // Only intercept dashboard API errors, not the assistant's own calls
                if (response.status >= 400
                    && typeof url === 'string'
                    && url.startsWith('/api/')
                    && !url.startsWith('/api/assistant/')
                    && !url.startsWith('/api/cron/')) {

                    response.clone().text().then(function(body) {
                        _interceptError(url, response.status, body);
                    }).catch(function() {});
                }
                return response;
            });
        };
    })();

    function _interceptError(url, status, body) {
        _pendingError = { url: url, status: status, body: (body || '').slice(0, 500) };

        // Show error badge on FAB
        var badge = document.getElementById('assistantErrorBadge');
        if (badge) badge.classList.add('assistant-error-badge--visible');

        // If panel is already open, show the error context
        if (_open) {
            _renderMessage('assistant', 'I noticed an error just occurred (' + status + ' on ' + url.replace('/api/', '') + '). Let me help troubleshoot — what were you trying to do?');
        }
    }

    // ── Public: surface error programmatically ───────────────────────────────

    window.assistantReportError = function(url, status, body) {
        _interceptError(url, status, body);
    };

})();
