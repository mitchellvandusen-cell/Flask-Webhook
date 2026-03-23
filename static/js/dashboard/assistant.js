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
            // Handle navigation
            if (data.navigate && typeof sidebarNavigate === 'function') {
                var btn = document.getElementById('sbn' + data.navigate.charAt(0).toUpperCase() + data.navigate.slice(1));
                if (btn) {
                    sidebarNavigate(data.navigate, btn);
                } else {
                    var navBtn = document.querySelector('.sb-nav-item[onclick*="' + data.navigate + '"]');
                    if (navBtn) navBtn.click();
                }
            }
            // Handle call mode choice (AI or human?)
            if (data.call_choice) {
                _renderCallChoice(data.call_choice);
            }
            // Handle call initiation
            if (data.call) {
                _initiateCall(data.call);
            }
            // Handle dial queue (power dial session)
            if (data.dial_queue) {
                _loadDialQueue(data.dial_queue);
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

    // ── Call choice buttons ─────────────────────────────────────────────────

    function _renderCallChoice(info) {
        var container = document.getElementById('assistantMessages');
        if (!container) return;

        var wrap = document.createElement('div');
        wrap.className = 'd-flex gap-2 flex-wrap mt-1 mb-2';
        wrap.style.paddingLeft = '4px';

        var aiBtn = document.createElement('button');
        aiBtn.className = 'btn btn-sm';
        aiBtn.style.cssText = 'background:rgba(0,255,136,0.12);border:1px solid rgba(0,255,136,0.25);color:#00ff88;border-radius:20px;font-size:0.8rem;font-weight:600;padding:6px 16px;';
        aiBtn.innerHTML = '<i class="fa-solid fa-robot" style="margin-right:5px"></i>AI Call';
        aiBtn.onclick = function() {
            wrap.remove();
            _renderMessage('user', 'AI call');
            info.dial_mode = 'ai';
            _initiateCall(info);
        };

        var humanBtn = document.createElement('button');
        humanBtn.className = 'btn btn-sm';
        humanBtn.style.cssText = 'background:rgba(0,217,255,0.12);border:1px solid rgba(0,217,255,0.25);color:#00d9ff;border-radius:20px;font-size:0.8rem;font-weight:600;padding:6px 16px;';
        humanBtn.innerHTML = '<i class="fa-solid fa-phone" style="margin-right:5px"></i>I\'ll Talk';
        humanBtn.onclick = function() {
            wrap.remove();
            _renderMessage('user', 'I\'ll talk');
            info.dial_mode = 'human';
            _initiateCall(info);
        };

        wrap.appendChild(aiBtn);
        wrap.appendChild(humanBtn);
        container.appendChild(wrap);
        container.scrollTop = container.scrollHeight;
    }

    function _initiateCall(info) {
        var mode = info.dial_mode || 'ai';
        _renderMessage('assistant', 'Calling ' + (info.first_name || 'contact') + ' now...');

        if (typeof dialContact === 'function') {
            dialContact(info.contact_id, info.phone, info.first_name, mode);
        } else {
            fetch('/voice/dial', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    contact_id: info.contact_id,
                    phone: info.phone,
                    first_name: info.first_name || 'there',
                    dial_mode: mode,
                }),
            }).then(function(r) { return r.json(); })
            .then(function(d) {
                if (d.error) _renderMessage('assistant', 'Call failed: ' + d.error);
            })
            .catch(function() {
                _renderMessage('assistant', 'Could not start the call. Check your voice setup.');
            });
        }
    }

    // ── Dial queue loader ──────────────────────────────────────────────────

    function _loadDialQueue(data) {
        var contacts = data.contacts || [];
        if (!contacts.length) return;

        // Navigate to dialer tab first
        if (typeof sidebarNavigate === 'function') {
            var btn = document.getElementById('sbnVoicedialer');
            if (btn) sidebarNavigate('voicedialer', btn);
        }

        // Load contacts into the dialer queue
        setTimeout(function() {
            // dialerQueue is the global queue array in dialer.js
            if (typeof dialerQueue !== 'undefined') {
                contacts.forEach(function(c) {
                    if (!dialerQueue.some(function(q) { return q.id === c.id; })) {
                        dialerQueue.push({
                            id: c.id,
                            name: c.name,
                            firstName: c.firstName,
                            phone: c.phone,
                            status: 'pending',
                        });
                    }
                });
                // Re-render the queue UI
                if (typeof dialerRenderQueue === 'function') dialerRenderQueue();
                // Open the queue panel if it's collapsed
                if (typeof dialerToggleQueue === 'function') dialerToggleQueue();

                // Show start button with mode choice
                _renderQueueStartChoice(contacts.length, data.dial_mode);
            } else {
                _renderMessage('assistant', 'Contacts queued but the dialer needs to load first. Switch to the Dialer tab and the queue will be ready.');
            }
        }, 500);
    }

    function _renderQueueStartChoice(count, defaultMode) {
        var container = document.getElementById('assistantMessages');
        if (!container) return;

        var wrap = document.createElement('div');
        wrap.className = 'd-flex gap-2 flex-wrap mt-1 mb-2';
        wrap.style.paddingLeft = '4px';

        var startBtn = document.createElement('button');
        startBtn.className = 'btn btn-sm';
        startBtn.style.cssText = 'background:rgba(0,255,136,0.15);border:1px solid rgba(0,255,136,0.3);color:#00ff88;border-radius:20px;font-size:0.8rem;font-weight:600;padding:6px 16px;';
        startBtn.innerHTML = '<i class="fa-solid fa-play" style="margin-right:5px"></i>Start Dialing (' + count + ')';
        startBtn.onclick = function() {
            wrap.remove();
            _renderMessage('user', 'Start dialing');
            _renderMessage('assistant', 'Dialing session started with ' + count + ' contacts.');
            // Start the queue
            if (typeof multiLineStartQueue === 'function') {
                multiLineStartQueue();
            } else if (typeof dialerToggleQueue === 'function') {
                // Single-line fallback: just start the queue
                if (typeof dialerQueueRunning !== 'undefined' && !dialerQueueRunning) {
                    dialerToggleQueue();
                }
            }
        };

        var cancelBtn = document.createElement('button');
        cancelBtn.className = 'btn btn-sm';
        cancelBtn.style.cssText = 'background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.1);color:#888;border-radius:20px;font-size:0.8rem;font-weight:600;padding:6px 16px;';
        cancelBtn.textContent = 'Cancel';
        cancelBtn.onclick = function() {
            wrap.remove();
            _renderMessage('user', 'Cancel');
            _renderMessage('assistant', 'Queue loaded but not started. You can start it manually from the dialer.');
        };

        wrap.appendChild(startBtn);
        wrap.appendChild(cancelBtn);
        container.appendChild(wrap);
        container.scrollTop = container.scrollHeight;
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
                if (response.status >= 500
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
