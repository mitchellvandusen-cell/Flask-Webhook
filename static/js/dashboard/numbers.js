        // ===== LOCAL UTILITIES =====
        function _esc(s) { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }
        function _fmtPhone(p) {
            if (!p) return '';
            const d = p.replace(/\D/g, '');
            if (d.length === 11 && d[0] === '1') return '(' + d.substr(1,3) + ') ' + d.substr(4,3) + '-' + d.substr(7);
            if (d.length === 10) return '(' + d.substr(0,3) + ') ' + d.substr(3,3) + '-' + d.substr(6);
            return p;
        }

        // ===== NUMBER CART =====
        var _numCart = []; // [{phone, numberType, priceLabel, priceCents}]

        function _numCartSave() {
            try { localStorage.setItem('numCart', JSON.stringify(_numCart)); } catch(e) {}
        }
        function _numCartLoad() {
            try {
                var saved = localStorage.getItem('numCart');
                if (saved) _numCart = JSON.parse(saved);
            } catch(e) { _numCart = []; }
            _numCartUpdateUI();
        }

        function numCartAdd(phone, numberType) {
            if (_numCart.find(function(i) { return i.phone === phone; })) return;
            var priceCents = numberType === 'toll_free' ? 215 : 90;
            var priceLabel = numberType === 'toll_free' ? '$2.15/mo' : '$0.90/mo';
            _numCart.push({ phone: phone, numberType: numberType, priceLabel: priceLabel, priceCents: priceCents });
            _numCartSave();
            _numCartUpdateUI();
            if (typeof _showDashToast === 'function') _showDashToast(true, _fmtPhone(phone) + ' added to cart');
        }

        function numCartRemove(phone) {
            _numCart = _numCart.filter(function(i) { return i.phone !== phone; });
            _numCartSave();
            _numCartUpdateUI();
        }

        function numCartClear() {
            _numCart = [];
            _numCartSave();
            _numCartUpdateUI();
        }

        function numCartToggle() {
            var overlay = document.getElementById('numCartOverlay');
            if (!overlay) return;
            overlay.style.display = overlay.style.display === 'none' ? 'block' : 'none';
            _numCartRenderItems();
        }

        function _numCartUpdateUI() {
            var btn = document.getElementById('numCartBtn');
            var countEl = document.getElementById('numCartCount');
            if (btn) btn.style.display = _numCart.length > 0 ? 'inline-flex' : 'none';
            if (countEl) countEl.textContent = _numCart.length;
            _numCartRenderItems();
        }

        function _numCartRenderItems() {
            var itemsEl = document.getElementById('numCartItems');
            var summaryEl = document.getElementById('numCartSummary');
            if (!itemsEl || !summaryEl) return;

            if (!_numCart.length) {
                itemsEl.innerHTML = '<div style="text-align:center;padding:12px;color:#666;">Cart is empty</div>';
                summaryEl.innerHTML = '';
                return;
            }

            // Calculate pricing: first 5 numbers total are free, rest are paid
            var freeRemaining = _numFreeRemaining !== null ? _numFreeRemaining : 0;
            var totalCents = 0;
            var freeCount = 0;
            var paidCount = 0;

            itemsEl.innerHTML = _numCart.map(function(item, idx) {
                var isFree = freeRemaining > 0;
                if (isFree) { freeRemaining--; freeCount++; }
                else { totalCents += item.priceCents; paidCount++; }
                var priceDisplay = isFree
                    ? '<span style="color:#00ff88;font-weight:700;">FREE</span>'
                    : '<span style="color:#00ff88;">' + item.priceLabel + '</span>';
                return '<div style="display:flex;align-items:center;justify-content:space-between;padding:8px 4px;border-bottom:1px solid rgba(255,255,255,0.04);">' +
                    '<div style="flex:1;">' +
                        '<span style="color:#fff;font-weight:600;">' + _esc(_fmtPhone(item.phone)) + '</span>' +
                        '<span style="margin-left:8px;font-size:0.75rem;color:#888;">' + item.numberType + '</span>' +
                    '</div>' +
                    '<div style="display:flex;align-items:center;gap:8px;">' +
                        priceDisplay +
                        '<button onclick="numCartRemove(\'' + item.phone + '\')" style="background:none;border:none;color:#ef4444;cursor:pointer;font-size:0.75rem;padding:2px 4px;" title="Remove"><i class="fa-solid fa-xmark"></i></button>' +
                    '</div>' +
                '</div>';
            }).join('');

            var totalDollars = (totalCents / 100).toFixed(2);
            summaryEl.innerHTML =
                '<div style="display:flex;justify-content:space-between;color:#aaa;margin-bottom:4px;">' +
                    '<span>' + _numCart.length + ' number' + (_numCart.length > 1 ? 's' : '') + '</span>' +
                    (freeCount > 0 ? '<span style="color:#00ff88;">' + freeCount + ' free</span>' : '') +
                '</div>' +
                (paidCount > 0 ? '<div style="display:flex;justify-content:space-between;font-weight:700;font-size:0.95rem;">' +
                    '<span style="color:#fff;">Total</span>' +
                    '<span style="color:#00ff88;">$' + totalDollars + '/mo</span>' +
                '</div>' : '<div style="color:#00ff88;font-weight:700;">All numbers are free!</div>');
        }

        async function numCartCheckout() {
            if (!_numCart.length) return;

            var btn = document.getElementById('numCartCheckoutBtn');
            if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-1"></i>Processing...'; }

            try {
                var r = await fetch('/voice/numbers/cart-checkout', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ items: _numCart.map(function(i) { return { phone_number: i.phone, number_type: i.numberType }; }) })
                });
                var d = await r.json();

                if (d.all_free) {
                    // All numbers were free — they've been provisioned
                    _numCart = [];
                    _numCartSave();
                    _numCartUpdateUI();
                    var overlay = document.getElementById('numCartOverlay');
                    if (overlay) overlay.style.display = 'none';
                    if (typeof _showDashToast === 'function') _showDashToast(true, d.provisioned + ' number(s) added!');
                    loadNumbersTab();
                    return;
                }

                if (d.checkout_url) {
                    // Store cart items pending in localStorage for post-checkout provisioning
                    localStorage.setItem('numCartPending', JSON.stringify(_numCart));
                    _numCart = [];
                    _numCartSave();
                    window.location.href = d.checkout_url;
                    return;
                }

                alert(d.error || 'Checkout failed');
            } catch(e) {
                alert('Network error during checkout');
            } finally {
                if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-credit-card me-1"></i>Checkout'; }
            }
        }

        // Post-purchase: complete provisioning after Stripe redirect
        (function() {
            var params = new URLSearchParams(window.location.search);
            if (params.get('cart_purchased') === '1') {
                var pending = [];
                try { pending = JSON.parse(localStorage.getItem('numCartPending') || '[]'); } catch(e) {}
                if (pending.length) {
                    localStorage.removeItem('numCartPending');
                    fetch('/voice/numbers/complete-cart-purchase', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({ items: pending.map(function(i) { return { phone_number: i.phone, number_type: i.numberType }; }) })
                    }).then(function(r) { return r.json(); }).then(function(d) {
                        if (d.provisioned > 0) {
                            if (typeof _showDashToast === 'function') _showDashToast(true, d.provisioned + ' number(s) purchased and activated!');
                        }
                        if (d.errors && d.errors.length) {
                            alert('Some numbers failed to provision:\n' + d.errors.join('\n'));
                        }
                        // Clean URL
                        var url = new URL(window.location);
                        url.searchParams.delete('cart_purchased');
                        window.history.replaceState({}, '', url);
                        if (typeof loadNumbersTab === 'function') loadNumbersTab();
                    }).catch(function() {});
                }
            }
        })();

        // Load cart from localStorage on script init
        _numCartLoad();

        // ===== SPAM PROTECTION TAB =====
        async function loadTrustHubData() { loadSpamProtectionStatus(); }

        async function loadSpamProtectionStatus() {
            const statusEl = document.getElementById('spamProtectionStatus');
            const formEl = document.getElementById('spamProtectionForm');
            const numsEl = document.getElementById('spNumbersContent');
            try {
                const r = await fetch('/voice/spam-protection/status');
                const d = await r.json();
                console.log('[SpamProtection] Status:', r.status, d);
                if (!r.ok || d.error) {
                    if (numsEl) numsEl.innerHTML = '<div style="color:#ef4444;font-size:.78rem;padding:8px;"><i class="fa-solid fa-triangle-exclamation me-1"></i>' + _esc(d.error || 'Failed to load') + '</div>';
                    return;
                }

                // Show status banner if protection is active
                if (d.protection_active && statusEl) {
                    statusEl.style.display = 'block';
                    statusEl.innerHTML =
                        '<div class="mb-3 p-3" style="background:rgba(0,255,136,0.06);border:1px solid rgba(0,255,136,0.2);border-radius:10px;">' +
                            '<div class="d-flex align-items-center gap-3">' +
                                '<div style="width:36px;height:36px;border-radius:50%;background:rgba(0,255,136,0.15);display:flex;align-items:center;justify-content:center;flex-shrink:0;">' +
                                    '<i class="fa-solid fa-shield-halved" style="color:#00ff88;"></i>' +
                                '</div>' +
                                '<div style="flex:1;">' +
                                    '<div style="font-weight:700;color:#00ff88;font-size:0.9rem;">Spam Protection Active</div>' +
                                    '<div style="font-size:0.75rem;color:#aaa;">' +
                                        '<strong>' + _esc(d.business_name) + '</strong> &mdash; ' +
                                        d.numbers_protected + '/' + d.numbers_total + ' numbers protected' +
                                        (d.auto_cnam ? ' &bull; Auto-protect ON' : '') +
                                        ' &bull; STIR/SHAKEN A &bull; Registered ' + (d.registered_at ? new Date(d.registered_at).toLocaleDateString() : '') +
                                    '</div>' +
                                '</div>' +
                                '<button onclick="document.getElementById(\'spamProtectionForm\').style.display=document.getElementById(\'spamProtectionForm\').style.display===\'none\'?\'block\':\'none\'" style="background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);color:#aaa;border-radius:6px;padding:5px 12px;font-size:0.75rem;cursor:pointer;white-space:nowrap;">' +
                                    '<i class="fa-solid fa-pen me-1"></i>Edit' +
                                '</button>' +
                            '</div>' +
                        '</div>';
                    // Pre-fill edit form with current registered values from API
                    var _sv = function(id, val) { var el = document.getElementById(id); if (el) el.value = val || ''; };
                    _sv('spBizName',      d.business_name);
                    _sv('spEIN',          d.ein);
                    _sv('spStreet',       d.street);
                    _sv('spCity',         d.city);
                    _sv('spState',        d.state);
                    _sv('spZip',          d.zip);
                    _sv('spContactName',  d.contact_name);
                    _sv('spContactEmail', d.contact_email);
                    _sv('spContactPhone', d.contact_phone);
                    // Collapse form if already registered
                    if (formEl) formEl.style.display = 'none';
                }

                // Render number protection list
                if (numsEl) {
                    const nums = d.numbers || [];
                    if (!nums.length) {
                        numsEl.innerHTML = '<div style="color:#888;text-align:center;padding:12px;">No numbers found. Buy a number in the Numbers tab to get started.</div>';
                    } else {
                        let html = '';
                        nums.forEach(n => {
                            const icon = n.cnam_enabled
                                ? '<i class="fa-solid fa-shield-halved" style="color:#00ff88;"></i>'
                                : '<i class="fa-solid fa-shield" style="color:#444;"></i>';
                            const label = n.cnam_enabled
                                ? '<span style="color:#00ff88;font-weight:600;">Protected</span>'
                                : '<span style="color:#888;">Not protected</span>';
                            html += '<div style="display:flex;align-items:center;justify-content:space-between;padding:8px 0;border-bottom:1px solid rgba(255,255,255,0.03);">' +
                                '<div style="display:flex;align-items:center;gap:8px;">' + icon +
                                    '<span style="color:#ccc;">' + _esc(_fmtPhone(n.phone)) + '</span>' +
                                '</div>' +
                                '<div style="display:flex;align-items:center;gap:8px;">' +
                                    label +
                                    (!n.cnam_enabled ? '<button onclick="enableCnamSingle(\'' + n.id + '\')" style="background:rgba(0,255,136,0.08);border:1px solid rgba(0,255,136,0.15);color:#00ff88;border-radius:5px;padding:3px 10px;font-size:.75rem;font-weight:600;cursor:pointer;"><i class="fa-solid fa-shield-halved me-1"></i>Enable</button>' : '') +
                                '</div>' +
                            '</div>';
                        });
                        numsEl.innerHTML = html;
                    }
                }
            } catch(e) {
                console.error('[SpamProtection] Error:', e);
                if (numsEl) numsEl.innerHTML = '<div style="color:#ef4444;font-size:.78rem;padding:8px;">Network error</div>';
            }
        }

        async function registerSpamProtection() {
            const btn = document.getElementById('spRegisterBtn');
            const result = document.getElementById('spRegisterResult');
            const payload = {
                business_name: document.getElementById('spBizName')?.value?.trim() || '',
                ein: document.getElementById('spEIN')?.value?.trim() || '',
                street: document.getElementById('spStreet')?.value?.trim() || '',
                city: document.getElementById('spCity')?.value?.trim() || '',
                state: document.getElementById('spState')?.value?.trim() || '',
                zip: document.getElementById('spZip')?.value?.trim() || '',
                contact_name: document.getElementById('spContactName')?.value?.trim() || '',
                contact_email: document.getElementById('spContactEmail')?.value?.trim() || '',
                contact_phone: document.getElementById('spContactPhone')?.value?.trim() || '',
            };
            if (!payload.business_name) { result.innerHTML = '<span style="color:#ef4444;">Business name is required</span>'; return; }
            if (!payload.ein) { result.innerHTML = '<span style="color:#ef4444;">EIN is required</span>'; return; }

            btn.disabled = true;
            btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-2"></i>Registering & protecting numbers...';
            result.innerHTML = '';
            try {
                const r = await fetch('/voice/spam-protection/register', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(payload),
                });
                const d = await r.json();
                if (r.ok) {
                    result.innerHTML = '<span style="color:#00ff88;"><i class="fa-solid fa-circle-check me-1"></i>' +
                        (d.numbers_protected || 0) + ' number' + ((d.numbers_protected || 0) !== 1 ? 's' : '') + ' protected! ' +
                        (d.numbers_failed > 0 ? '<span style="color:#ffa500;">(' + d.numbers_failed + ' failed)</span>' : '') +
                        '</span>';
                    // Reload status to show active badge
                    setTimeout(() => loadSpamProtectionStatus(), 500);
                } else {
                    result.innerHTML = '<span style="color:#ef4444;"><i class="fa-solid fa-triangle-exclamation me-1"></i>' + _esc(d.error || 'Registration failed') + '</span>';
                }
            } catch(e) {
                result.innerHTML = '<span style="color:#ef4444;">Network error — check your connection</span>';
            }
            btn.disabled = false;
            btn.innerHTML = '<i class="fa-solid fa-shield-halved me-2"></i>Register & Protect All Numbers';
        }

        async function enableCnamSingle(numberId) {
            try {
                const r = await fetch('/voice/numbers/' + numberId + '/cnam', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ enable: true }),
                });
                if (r.ok) loadSpamProtectionStatus();
                else alert('Failed to enable CNAM');
            } catch(e) { alert('Network error'); }
        }

        // Backward compatibility aliases
        function saveTrustHub() { registerSpamProtection(); }
        function markCarrierRegistered() { /* no-op, carriers handled automatically now */ }

        // Keep old function name as alias for backward compatibility
        function loadTrustHubNumbers() { loadNumbersTab(); }

        // ===== CNAM MONITOR & LOOKUP =====

        async function loadCnamMonitor() {
            var listEl = document.getElementById('cnamNumbersList');
            var bannerEl = document.getElementById('cnamSummaryBanner');
            if (listEl) listEl.innerHTML = '<div class="cnam-loading"><i class="fa-solid fa-spinner fa-spin me-1"></i>Loading numbers...</div>';

            try {
                var r = await fetch('/voice/cnam/monitor');
                if (!r.ok) {
                    var d = {};
                    try { d = await r.json(); } catch(_) {}
                    if (listEl) listEl.innerHTML = '<div class="cnam-error"><i class="fa-solid fa-triangle-exclamation me-1"></i>' + _esc(d.error || 'Failed to load') + '</div>';
                    return;
                }
                var d = await r.json();

                // Summary banner
                if (bannerEl) {
                    var total = d.total || 0;
                    var cnamSet = d.cnam_set || 0;
                    var matching = d.cnam_matching || 0;
                    var displayName = d.cnam_display_name || '';
                    var allGood = cnamSet === total && matching === total && total > 0;

                    bannerEl.innerHTML =
                        '<div class="d-flex align-items-center gap-2 flex-wrap">' +
                            '<div class="cnam-stat-pill ' + (allGood ? 'cnam-stat-good' : 'cnam-stat-warn') + '">' +
                                '<i class="fa-solid ' + (allGood ? 'fa-circle-check' : 'fa-triangle-exclamation') + ' me-1"></i>' +
                                cnamSet + '/' + total + ' CNAM set' +
                            '</div>' +
                            (displayName ?
                                '<div class="cnam-stat-pill cnam-stat-info">' +
                                    '<i class="fa-solid fa-id-card me-1"></i>Display: <strong>' + _esc(displayName) + '</strong>' +
                                '</div>' : '') +
                            (matching < cnamSet ?
                                '<div class="cnam-stat-pill cnam-stat-warn">' +
                                    '<i class="fa-solid fa-exclamation me-1"></i>' + (cnamSet - matching) + ' mismatched' +
                                '</div>' : '') +
                        '</div>';
                }

                // Numbers list
                if (listEl) {
                    var nums = d.numbers || [];
                    if (!nums.length) {
                        listEl.innerHTML = '<div class="cnam-empty">No numbers found. Buy a number in the Numbers tab first.</div>';
                        return;
                    }

                    var html = '<div class="cnam-table-header">' +
                        '<div class="cnam-col-phone">Phone</div>' +
                        '<div class="cnam-col-name">CNAM Name</div>' +
                        '<div class="cnam-col-status">Status</div>' +
                        '<div class="cnam-col-actions">Actions</div>' +
                    '</div>';

                    nums.forEach(function(n) {
                        var enabled = n.cnam_enabled;
                        var matches = n.cnam_matches_business;
                        var statusIcon, statusLabel, statusClass;

                        if (!enabled) {
                            statusIcon = 'fa-circle-xmark';
                            statusLabel = 'Not Set';
                            statusClass = 'cnam-status-off';
                        } else if (matches) {
                            statusIcon = 'fa-circle-check';
                            statusLabel = 'Active';
                            statusClass = 'cnam-status-ok';
                        } else {
                            statusIcon = 'fa-triangle-exclamation';
                            statusLabel = 'Mismatched';
                            statusClass = 'cnam-status-warn';
                        }

                        html += '<div class="cnam-row">' +
                            '<div class="cnam-col-phone">' + _esc(_fmtPhone(n.phone)) + '</div>' +
                            '<div class="cnam-col-name">' +
                                '<span class="cnam-name-display" id="cnam-name-' + n.sid + '">' + _esc(n.cnam_name || '—') + '</span>' +
                            '</div>' +
                            '<div class="cnam-col-status">' +
                                '<span class="cnam-status-badge ' + statusClass + '">' +
                                    '<i class="fa-solid ' + statusIcon + ' me-1"></i>' + statusLabel +
                                '</span>' +
                            '</div>' +
                            '<div class="cnam-col-actions">' +
                                '<button onclick="cnamEditInline(\'' + n.sid + '\', \'' + _esc(n.cnam_name || '') + '\')" class="cnam-action-btn" title="Edit CNAM">' +
                                    '<i class="fa-solid fa-pen"></i>' +
                                '</button>' +
                                (!enabled ?
                                    '<button onclick="cnamQuickSet(\'' + n.sid + '\')" class="cnam-action-btn cnam-action-set" title="Set to business name">' +
                                        '<i class="fa-solid fa-bolt"></i>' +
                                    '</button>' : '') +
                            '</div>' +
                        '</div>';
                    });

                    listEl.innerHTML = html;
                }
            } catch(e) {
                console.error('[CNAM Monitor]', e);
                if (listEl) listEl.innerHTML = '<div class="cnam-error">Network error</div>';
            }
        }

        async function cnamEditInline(sid, currentName) {
            var el = document.getElementById('cnam-name-' + sid);
            if (!el) return;
            var newName = prompt('Enter CNAM name (max 15 characters):', currentName);
            if (newName === null) return;
            newName = newName.trim().substring(0, 15);

            el.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
            try {
                var r = await fetch('/voice/cnam/update', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ number_sid: sid, cnam_name: newName }),
                });
                var d = await r.json();
                if (r.ok && d.status === 'ok') {
                    if (typeof _showDashToast === 'function') _showDashToast(true, 'CNAM updated');
                    loadCnamMonitor();
                } else {
                    alert(d.error || 'Update failed');
                    el.textContent = currentName || '—';
                }
            } catch(e) {
                alert('Network error');
                el.textContent = currentName || '—';
            }
        }

        async function cnamQuickSet(sid) {
            var el = document.getElementById('cnam-name-' + sid);
            if (el) el.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
            try {
                var r = await fetch('/voice/cnam/update', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ number_sid: sid, cnam_name: '' }), // empty = use business name
                });
                if (r.ok) {
                    if (typeof _showDashToast === 'function') _showDashToast(true, 'CNAM set');
                    loadCnamMonitor();
                } else {
                    var d = await r.json();
                    alert(d.error || 'Failed');
                }
            } catch(e) { alert('Network error'); }
        }

        async function cnamSyncAll() {
            var btn = document.getElementById('cnamSyncAllBtn');
            if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-1"></i>Syncing...'; }
            try {
                var r = await fetch('/voice/cnam/update-all', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                });
                var d = await r.json();
                if (r.ok) {
                    if (typeof _showDashToast === 'function') {
                        _showDashToast(true, d.updated + ' updated, ' + d.already_set + ' already set' + (d.failed > 0 ? ', ' + d.failed + ' failed' : ''));
                    }
                    loadCnamMonitor();
                } else {
                    alert(d.error || 'Sync failed');
                }
            } catch(e) { alert('Network error'); }
            if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-arrows-rotate me-1"></i>Sync All to Business Name'; }
        }

        async function cnamLookup() {
            var phone = (document.getElementById('cnamLookupPhone') || {}).value || '';
            var resultEl = document.getElementById('cnamLookupResult');
            var btn = document.getElementById('cnamLookupBtn');
            if (!phone.trim()) { if (resultEl) resultEl.innerHTML = '<div class="cnam-error">Enter a phone number</div>'; return; }

            if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-1"></i>Looking up...'; }
            if (resultEl) resultEl.innerHTML = '';

            try {
                var r = await fetch('/voice/cnam/lookup', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ phone: phone.trim() }),
                });
                var d = await r.json();
                if (d.error && !d.caller_name) {
                    resultEl.innerHTML = '<div class="cnam-lookup-card cnam-lookup-error">' +
                        '<i class="fa-solid fa-triangle-exclamation me-2"></i>Lookup failed: ' + _esc(d.error) +
                    '</div>';
                } else {
                    var callerName = d.caller_name || 'Not available';
                    var callerType = d.caller_type || 'Unknown';
                    var typeIcon = callerType === 'BUSINESS' ? 'fa-building' : callerType === 'CONSUMER' ? 'fa-user' : 'fa-question';

                    resultEl.innerHTML = '<div class="cnam-lookup-card">' +
                        '<div class="cnam-lookup-row">' +
                            '<span class="cnam-lookup-label">Phone</span>' +
                            '<span class="cnam-lookup-value">' + _esc(_fmtPhone(d.phone || phone)) + '</span>' +
                        '</div>' +
                        '<div class="cnam-lookup-row">' +
                            '<span class="cnam-lookup-label">Caller Name</span>' +
                            '<span class="cnam-lookup-value cnam-lookup-name">' + _esc(callerName) + '</span>' +
                        '</div>' +
                        '<div class="cnam-lookup-row">' +
                            '<span class="cnam-lookup-label">Type</span>' +
                            '<span class="cnam-lookup-value"><i class="fa-solid ' + typeIcon + ' me-1"></i>' + _esc(callerType) + '</span>' +
                        '</div>' +
                    '</div>';
                }
            } catch(e) {
                if (resultEl) resultEl.innerHTML = '<div class="cnam-lookup-card cnam-lookup-error"><i class="fa-solid fa-wifi me-2"></i>Network error</div>';
            }
            if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-magnifying-glass me-1"></i>Lookup'; }
        }

        async function cnamLookupOwn() {
            var resultEl = document.getElementById('cnamLookupOwnResult');
            var btn = document.getElementById('cnamLookupOwnBtn');
            if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-1"></i>Checking all numbers...'; }
            if (resultEl) resultEl.innerHTML = '';

            try {
                var r = await fetch('/voice/cnam/lookup-own');
                var d = await r.json();
                if (!r.ok || d.error) {
                    resultEl.innerHTML = '<div class="cnam-error">' + _esc(d.error || 'Failed') + '</div>';
                    if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-list-check me-1"></i>Check All My Numbers Against Carrier Database'; }
                    return;
                }

                var nums = d.numbers || [];
                if (!nums.length) {
                    resultEl.innerHTML = '<div class="cnam-empty">No numbers to check.</div>';
                    if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-list-check me-1"></i>Check All My Numbers Against Carrier Database'; }
                    return;
                }

                var html = '<div class="cnam-propagation-summary mb-2">' +
                    '<span class="cnam-stat-pill ' + (d.propagated === d.total ? 'cnam-stat-good' : 'cnam-stat-warn') + '">' +
                        d.propagated + '/' + d.total + ' propagated to carriers' +
                    '</span>' +
                '</div>';

                html += '<div class="cnam-table-header">' +
                    '<div class="cnam-col-phone">Phone</div>' +
                    '<div class="cnam-col-name">Set As</div>' +
                    '<div class="cnam-col-name">Carrier Sees</div>' +
                    '<div class="cnam-col-status">Match</div>' +
                '</div>';

                nums.forEach(function(n) {
                    var propClass = n.propagated ? 'cnam-status-ok' : (n.error ? 'cnam-status-off' : 'cnam-status-warn');
                    var propIcon = n.propagated ? 'fa-circle-check' : (n.error ? 'fa-circle-xmark' : 'fa-triangle-exclamation');
                    var propLabel = n.propagated ? 'Match' : (n.error ? 'Error' : 'Mismatch');

                    html += '<div class="cnam-row">' +
                        '<div class="cnam-col-phone">' + _esc(_fmtPhone(n.phone)) + '</div>' +
                        '<div class="cnam-col-name">' + _esc(n.set_name || '—') + '</div>' +
                        '<div class="cnam-col-name">' + _esc(n.carrier_name || '—') + '</div>' +
                        '<div class="cnam-col-status">' +
                            '<span class="cnam-status-badge ' + propClass + '">' +
                                '<i class="fa-solid ' + propIcon + ' me-1"></i>' + propLabel +
                            '</span>' +
                        '</div>' +
                    '</div>';
                });

                resultEl.innerHTML = html;
            } catch(e) {
                if (resultEl) resultEl.innerHTML = '<div class="cnam-error">Network error</div>';
            }
            if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-list-check me-1"></i>Check All My Numbers Against Carrier Database'; }
        }

        // ===== NUMBER INTEGRITY (Voice Integrity) =====
        var _niData = null; // cached status data
        var _niSelectedSids = new Set();

        async function loadNumberIntegrity() {
            var listEl = document.getElementById('niNumbersList');
            var bannerEl = document.getElementById('niStatusBanner');
            var registerBtn = document.getElementById('niRegisterBtn');
            var remediateBtn = document.getElementById('niRemediateBtn');
            var carrierListEl = document.getElementById('niCarrierList');
            var resultEl = document.getElementById('niActionResult');
            if (listEl) listEl.innerHTML = '<div class="ni-loading"><i class="fa-solid fa-spinner fa-spin me-1"></i>Loading numbers...</div>';
            if (resultEl) resultEl.innerHTML = '';

            try {
                var r = await fetch('/voice/number-integrity/status');
                if (!r.ok) {
                    var d = {};
                    try { d = await r.json(); } catch(_) {}
                    if (listEl) listEl.innerHTML = '<div class="ni-error"><i class="fa-solid fa-triangle-exclamation me-1"></i>' + _esc(d.error || 'Failed to load (HTTP ' + r.status + ')') + '</div>';
                    return;
                }
                var d = await r.json();
                if (d.error) {
                    if (listEl) listEl.innerHTML = '<div class="ni-error"><i class="fa-solid fa-triangle-exclamation me-1"></i>' + _esc(d.error) + '</div>';
                    return;
                }
                _niData = d;

                var isActive = d.status === 'twilio-approved';
                var isPending = d.status === 'pending-review' || d.status === 'in-review';
                var isRejected = d.status === 'twilio-rejected';

                // Render carrier cards
                if (carrierListEl && d.carriers) {
                    var carrierHtml = '';
                    d.carriers.forEach(function(c) {
                        var statusClass = isActive ? 'ni-carrier-active' : (isPending ? 'ni-carrier-pending' : 'ni-carrier-inactive');
                        var statusIcon = isActive ? 'fa-circle-check' : (isPending ? 'fa-clock' : 'fa-circle-xmark');
                        var statusLabel = isActive ? 'Registered' : (isPending ? 'Pending' : (isRejected ? 'Rejected' : 'Not Registered'));
                        carrierHtml += '<div class="col-md-4">' +
                            '<div class="ni-carrier-card ' + statusClass + '">' +
                                '<div class="ni-carrier-icon"><i class="fa-solid ' + c.icon + '"></i></div>' +
                                '<div class="ni-carrier-name">' + _esc(c.name) + '</div>' +
                                '<div class="ni-carrier-status"><i class="fa-solid ' + statusIcon + ' me-1"></i>' + statusLabel + '</div>' +
                                '<div class="ni-carrier-desc">' + _esc(c.description) + '</div>' +
                            '</div>' +
                        '</div>';
                    });
                    carrierListEl.innerHTML = carrierHtml;
                }

                // Show status banner if registered
                if (bannerEl && d.status !== 'not_registered') {
                    var disp = d.display || {};
                    var bannerClass = 'ni-banner-' + (disp.color || 'gray');
                    bannerEl.style.display = 'block';
                    bannerEl.className = 'ni-status-banner ' + bannerClass + ' mb-3 p-3';
                    var detailText = d.business_name ? '<strong>' + _esc(d.business_name) + '</strong> &mdash; ' : '';
                    if (isRejected) {
                        detailText += 'Registration was rejected. Please review and re-submit.';
                    } else {
                        detailText += d.assigned_count + ' number' + (d.assigned_count !== 1 ? 's' : '') + ' registered';
                        if (d.registered_at) detailText += ' &bull; Since ' + new Date(d.registered_at).toLocaleDateString();
                    }
                    bannerEl.innerHTML =
                        '<div class="d-flex align-items-center gap-3">' +
                            '<div class="ni-banner-icon"><i class="fa-solid ' + (disp.icon || 'fa-circle-info') + '"></i></div>' +
                            '<div class="ni-banner-body">' +
                                '<div class="ni-banner-title">' + _esc(disp.label || d.status) + '</div>' +
                                '<div class="ni-banner-detail">' + detailText + '</div>' +
                            '</div>' +
                            (isActive || isRejected ?
                                '<button onclick="niRemediate()" class="ni-banner-remediate-btn"><i class="fa-solid fa-wrench me-1"></i>Remediate</button>' : '') +
                        '</div>';
                } else if (bannerEl) {
                    bannerEl.style.display = 'none';
                }

                // Render phone numbers list
                if (listEl) {
                    var nums = d.numbers || [];
                    if (!nums.length) {
                        listEl.innerHTML = '<div class="ni-empty">No numbers found. Buy a number in the Numbers tab first.</div>';
                    } else {
                        var html = '';
                        nums.forEach(function(n) {
                            var checked = n.registered ? ' checked disabled' : '';
                            var regBadge = n.registered ? '<span class="ni-badge-registered"><i class="fa-solid fa-circle-check me-1"></i>Registered</span>' : '';
                            html += '<label class="ni-number-row">' +
                                '<input type="checkbox" class="ni-number-cb" data-sid="' + _esc(n.sid) + '" onchange="niUpdateSelection()"' + checked + '>' +
                                '<span class="ni-number-phone">' + _esc(_fmtPhone(n.phone)) + '</span>' +
                                (n.friendly_name ? '<span class="ni-number-name">' + _esc(n.friendly_name) + '</span>' : '') +
                                regBadge +
                            '</label>';
                        });
                        listEl.innerHTML = html;
                    }
                }

                // Show/hide action buttons based on status
                // Register: show when not yet submitted, approved (add more), or rejected (re-register)
                if (registerBtn) {
                    var showRegister = d.status === 'not_registered' || d.status === 'draft' || isActive || isRejected;
                    registerBtn.style.display = showRegister ? '' : 'none';
                    if (isActive) {
                        registerBtn.innerHTML = '<i class="fa-solid fa-plus me-2"></i>Add More Numbers';
                    } else if (isRejected) {
                        registerBtn.innerHTML = '<i class="fa-solid fa-redo me-2"></i>Re-Register Numbers';
                    } else {
                        registerBtn.innerHTML = '<i class="fa-solid fa-tower-broadcast me-2"></i>Register Selected Numbers';
                    }
                }
                // Remediate: show when approved or rejected
                if (remediateBtn) remediateBtn.style.display = (isActive || isRejected) ? '' : 'none';

                _niSelectedSids.clear();
                niUpdateSelection();

            } catch(e) {
                console.error('[NumberIntegrity] Load error:', e);
                if (listEl) listEl.innerHTML = '<div class="ni-error">Network error &mdash; check your connection</div>';
            }
        }

        function niUpdateSelection() {
            _niSelectedSids.clear();
            var cbs = document.querySelectorAll('.ni-number-cb:checked:not(:disabled)');
            cbs.forEach(function(cb) { _niSelectedSids.add(cb.dataset.sid); });
            var countEl = document.getElementById('niSelectedCount');
            if (countEl) countEl.textContent = _niSelectedSids.size + ' selected';
        }

        function niSelectAll() {
            document.querySelectorAll('.ni-number-cb:not(:disabled)').forEach(function(cb) { cb.checked = true; });
            niUpdateSelection();
        }

        function niDeselectAll() {
            document.querySelectorAll('.ni-number-cb:not(:disabled)').forEach(function(cb) { cb.checked = false; });
            niUpdateSelection();
        }

        async function niRegister() {
            var resultEl = document.getElementById('niActionResult');
            if (_niSelectedSids.size === 0) {
                if (resultEl) resultEl.innerHTML = '<span class="ni-result-error"><i class="fa-solid fa-triangle-exclamation me-1"></i>Select at least one number to register</span>';
                return;
            }
            var btn = document.getElementById('niRegisterBtn');
            var isAddMode = _niData && _niData.trust_product_sid;
            if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-2"></i>Registering with carrier networks...'; }
            if (resultEl) resultEl.innerHTML = '';

            var endpoint = isAddMode ? '/voice/number-integrity/add-numbers' : '/voice/number-integrity/register';

            try {
                var r = await fetch(endpoint, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ phone_number_sids: Array.from(_niSelectedSids) }),
                });
                var d = await r.json();
                if (r.ok && d.status === 'ok') {
                    var count = d.numbers_assigned || 0;
                    var msg = count + ' number' + (count !== 1 ? 's' : '') + ' submitted for carrier registration.';
                    if (d.numbers_failed > 0) msg += ' ' + d.numbers_failed + ' failed.';
                    msg += ' Registration takes 24\u201348 hours.';
                    if (resultEl) resultEl.innerHTML = '<span class="ni-result-success"><i class="fa-solid fa-circle-check me-1"></i>' + msg + '</span>';
                    setTimeout(function() { loadNumberIntegrity(); }, 1500);
                } else {
                    if (resultEl) resultEl.innerHTML = '<span class="ni-result-error"><i class="fa-solid fa-triangle-exclamation me-1"></i>' + _esc(d.error || 'Registration failed') + '</span>';
                }
            } catch(e) {
                console.error('[NumberIntegrity] Register error:', e);
                if (resultEl) resultEl.innerHTML = '<span class="ni-result-error">Network error &mdash; check your connection</span>';
            }
            if (btn) {
                btn.disabled = false;
                btn.innerHTML = isAddMode
                    ? '<i class="fa-solid fa-plus me-2"></i>Add More Numbers'
                    : '<i class="fa-solid fa-tower-broadcast me-2"></i>Register Selected Numbers';
            }
        }

        async function niRemediate() {
            var btn = document.getElementById('niRemediateBtn');
            var resultEl = document.getElementById('niActionResult');
            if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-2"></i>Submitting remediation...'; }
            if (resultEl) resultEl.innerHTML = '';

            try {
                var r = await fetch('/voice/number-integrity/remediate', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: '{}',
                });
                var d = await r.json();
                if (r.ok && d.status === 'ok') {
                    var msg = d.message || 'Remediation submitted successfully.';
                    if (resultEl) resultEl.innerHTML = '<span class="ni-result-success"><i class="fa-solid fa-circle-check me-1"></i>' + _esc(msg) + '</span>';
                    setTimeout(function() { loadNumberIntegrity(); }, 1500);
                } else {
                    // Covers 409 (already pending), 400 (draft), and 500 errors
                    var errMsg = d.error || d.message || 'Remediation failed';
                    var cssClass = r.status === 409 ? 'ni-result-warning' : 'ni-result-error';
                    var icon = r.status === 409 ? 'fa-clock' : 'fa-triangle-exclamation';
                    if (resultEl) resultEl.innerHTML = '<span class="' + cssClass + '"><i class="fa-solid ' + icon + ' me-1"></i>' + _esc(errMsg) + '</span>';
                }
            } catch(e) {
                console.error('[NumberIntegrity] Remediate error:', e);
                if (resultEl) resultEl.innerHTML = '<span class="ni-result-error">Network error &mdash; check your connection</span>';
            }
            if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-wrench me-2"></i>Remediate Spam Labels'; }
        }

        async function searchAvailableNumbers() {
            const numberType = document.getElementById('buyNumberType').value;
            const area = document.getElementById('buyAreaCode').value.trim();
            const state = document.getElementById('buyState').value.trim();
            const city = document.getElementById('buyCity').value.trim();
            const zip = document.getElementById('buyZip').value.trim();
            const contains = document.getElementById('buyContains').value.trim();
            const list = document.getElementById('availableNumbersList');
            list.innerHTML = '<div style="text-align:center;padding:8px;"><i class="fa-solid fa-spinner fa-spin" style="color:#00d9ff;"></i></div>';
            try {
                const params = new URLSearchParams();
                params.set('number_type', numberType);
                if (area) params.set('area_code', area);
                if (state) params.set('state', state);
                if (city) params.set('city', city);
                if (zip) params.set('zip_code', zip);
                if (contains) params.set('contains', contains);
                const r = await fetch('/voice/numbers/search?' + params.toString());
                const d = await r.json();
                if (!r.ok) { list.innerHTML = '<div style="color:#ef4444;padding:4px;">' + _esc(d.error || 'Failed') + '</div>'; return; }
                const nums = d.numbers || [];
                if (!nums.length) { list.innerHTML = '<div style="color:#888;padding:4px;font-size:.75rem;">No numbers found for that search. Try different filters.</div>'; return; }
                const priceMap = { local: '$0.90', toll_free: '$2.15', mobile: '$0.90' };
                const monthlyPrice = priceMap[numberType] || '$0.90';
                const isFree = _numFreeRemaining !== null && _numFreeRemaining > 0;
                list.innerHTML = nums.map(n => {
                    const loc = [n.locality, n.region].filter(Boolean).join(', ');
                    const caps = [];
                    if (n.capabilities && n.capabilities.voice) caps.push('Voice');
                    if (n.capabilities && n.capabilities.sms) caps.push('SMS');
                    if (n.capabilities && n.capabilities.mms) caps.push('MMS');
                    const priceLabel = isFree
                        ? '<span style="color:#00ff88;font-size:.75rem;font-weight:700;">FREE</span>'
                        : '<span style="color:#00ff88;font-size:.75rem;font-weight:600;">' + monthlyPrice + '/mo</span>';
                    var inCart = _numCart.find(function(ci) { return ci.phone === n.phone; });
                    var cartBtn = inCart
                        ? '<span style="color:#00ff88;font-size:.7rem;font-weight:600;"><i class="fa-solid fa-check me-1"></i>In Cart</span>'
                        : '<button onclick="numCartAdd(\'' + n.phone + '\',\'' + numberType + '\')" style="background:rgba(0,255,136,0.1);border:1px solid rgba(0,255,136,0.2);color:#00ff88;border-radius:4px;padding:3px 8px;font-size:.7rem;font-weight:600;cursor:pointer;" title="Add to cart"><i class="fa-solid fa-cart-plus"></i></button>';
                    return '<div style="display:flex;align-items:center;justify-content:space-between;padding:8px 4px;border-bottom:1px solid rgba(255,255,255,0.03);font-size:.78rem;">' +
                        '<div style="flex:1;min-width:0;">' +
                            '<span style="color:#fff;font-weight:600;">' + _esc(_fmtPhone(n.phone)) + '</span>' +
                            (loc ? '<span style="color:#666;font-size:.75rem;margin-left:6px;">' + _esc(loc) + '</span>' : '') +
                            '<div style="margin-top:2px;">' + caps.map(c => '<span style="background:rgba(0,217,255,0.08);color:#00d9ff;padding:1px 6px;border-radius:4px;font-size:.75rem;margin-right:3px;">' + c + '</span>').join('') + '</div>' +
                        '</div>' +
                        '<div style="display:flex;align-items:center;gap:6px;flex-shrink:0;">' +
                            priceLabel +
                            cartBtn +
                            '<button onclick="buyNumber(\'' + n.phone + '\',\'' + numberType + '\')" style="background:linear-gradient(135deg,#00d9ff,#0099cc);border:none;color:#000;border-radius:4px;padding:3px 10px;font-size:.75rem;font-weight:700;cursor:pointer;">' + (isFree ? 'Add Free' : 'Buy') + '</button>' +
                        '</div>' +
                    '</div>';
                }).join('');
            } catch(e) { list.innerHTML = '<div style="color:#ef4444;">Network error</div>'; }
        }

        var _numFreeRemaining = null; // populated by loadNumbersTab

        async function buyNumber(phone, numberType) {
            numberType = numberType || document.getElementById('buyNumberType')?.value || 'local';
            var isFree = _numFreeRemaining !== null && _numFreeRemaining > 0;
            var msg = isFree
                ? 'Add ' + phone + ' to your account?\n\nThis number is FREE (' + _numFreeRemaining + ' free remaining).'
                : 'Purchase ' + phone + ' for $' + (numberType === 'toll_free' ? '2.15' : '0.90') + '/mo?\n\nThis will be charged to your card.';
            if (!confirm(msg)) return;

            try {
                // Try the direct buy first (works if free numbers remain)
                var r = await fetch('/voice/numbers/buy', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ phone_number: phone, number_type: numberType })
                });
                var d = await r.json();

                if (r.status === 402 && d.payment_required) {
                    // Free allowance exhausted — route through Stripe checkout
                    var cr = await fetch('/voice/numbers/checkout', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({ phone_number: phone, number_type: numberType })
                    });
                    var cd = await cr.json();
                    if (!cr.ok) { alert(cd.error || 'Checkout failed'); return; }
                    // Redirect to Stripe
                    window.location.href = cd.checkout_url;
                    return;
                }

                if (!r.ok) { alert(d.error || 'Failed'); return; }
                alert('Number added: ' + d.phone);
                var buyPanel = document.getElementById('vstabBuyPanel'); if (buyPanel) buyPanel.style.display = 'none';
                loadNumbersTab();
                loadNumberHealth();
            } catch(e) { alert('Network error'); }
        }
        async function releaseNumber(sid, phone) {
            if (!confirm('Release ' + phone + '?\n\nThis cannot be undone. The number will be removed from your account.')) return;
            try {
                const r = await fetch('/voice/numbers/release', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ sid: sid }) });
                const d = await r.json();
                if (!d.error) { loadNumbersTab(); loadNumberHealth(); }
                else alert(d.error);
            } catch(e) { alert('Network error'); }
        }

        // ── Init dialer (runs on page load since Dialer is default tab) ──
        function initDialerTab() {
            dialerLoadPipelines();
            if (!dialerContacts.length) dialerFetchContacts();
            dialerLoadAllCallHistory();
            // Auto-init VoIP if credential exists (one-time connect)
            if (voipSetupDone && !voipReady && !_voipInitializing) initVoIPDevice();
            // Always refresh audio device list
            refreshAudioDevices();
        }
        document.querySelector('[data-bs-target="#voicedialer"]')?.addEventListener('shown.bs.tab', initDialerTab);
        // Also init on page load since Dialer is the default active tab
        document.addEventListener('DOMContentLoaded', initDialerTab);

        // Auto-load Numbers/Trust Hub when Voice tab is shown
        document.querySelector('[data-bs-target="#voice"]')?.addEventListener('shown.bs.tab', function() {
            // Pre-load data so Numbers/Trust Hub tabs render instantly
            loadNumbersTab();
        });

        // ===== NUMBER HEALTH & SMART ROTATION =====
        var _nhData = null;

        async function loadNumberHealth() {
            const el = document.getElementById('numberHealthContent');
            if (!el) return;
            // Show loading spinner
            el.innerHTML = '<div style="text-align:center;padding:20px;color:#555;"><i class="fa-solid fa-spinner fa-spin" style="font-size:1rem;color:#ff6b9d;display:block;margin-bottom:6px;"></i><span style="font-size:.75rem;">Loading number health...</span></div>';
            try {
                const r = await fetch('/voice/number-health');
                if (!r.ok) {
                    el.innerHTML = '<div style="text-align:center;padding:16px;color:#ef4444;font-size:.75rem;"><i class="fa-solid fa-triangle-exclamation me-1"></i>Failed to load number health</div>';
                    return;
                }
                const d = await r.json();
                _nhData = d;
                _renderNumberHealth(el, d);
            } catch(e) {
                console.error('[NumberHealth] Load error:', e);
                el.innerHTML = '<div style="text-align:center;padding:16px;color:#ef4444;font-size:.75rem;"><i class="fa-solid fa-triangle-exclamation me-1"></i>Network error loading health data</div>';
            }
        }

        function _renderNumberHealth(el, d) {
            const nums = d.numbers || [];
            const sum = d.summary || {};
            const rotationEnabled = d.rotation_enabled;
            const strategy = d.rotation_strategy || 'weighted_health';

            let html = '';

            // ── Header with toggle ──
            html += '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;">';
            html += '<h6 style="margin:0;font-weight:700;color:#fff;font-size:.88rem;"><i class="fa-solid fa-heart-pulse me-2" style="color:#ff6b9d;"></i>Number Health & Smart Rotation</h6>';
            html += '<label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:.75rem;color:#aaa;">';
            html += '<span>' + (rotationEnabled ? 'Enabled' : 'Disabled') + '</span>';
            html += '<div onclick="nhToggleRotation()" style="width:38px;height:20px;border-radius:10px;background:' + (rotationEnabled ? 'rgba(0,255,136,0.4)' : 'rgba(255,255,255,0.08)') + ';position:relative;cursor:pointer;transition:background .2s;">';
            html += '<div style="width:16px;height:16px;border-radius:50%;background:' + (rotationEnabled ? '#00ff88' : '#555') + ';position:absolute;top:2px;' + (rotationEnabled ? 'right:2px' : 'left:2px') + ';transition:all .2s;"></div>';
            html += '</div></label></div>';

            if (!rotationEnabled) {
                html += '<div style="padding:16px;background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.06);border-radius:10px;text-align:center;color:#666;font-size:.78rem;">';
                html += '<i class="fa-solid fa-shuffle" style="font-size:1.2rem;display:block;margin-bottom:6px;color:#444;"></i>';
                html += 'Enable Smart Rotation to automatically distribute calls across your numbers,<br>prevent burnout, and maximize connection rates.';
                html += '</div>';
                el.innerHTML = html;
                return;
            }

            // ── Strategy selector ──
            html += '<div style="display:flex;gap:6px;margin-bottom:14px;">';
            var strategies = [
                { key: 'weighted_health', label: 'Weighted Health', icon: 'fa-scale-balanced', desc: 'Higher health = more calls' },
                { key: 'round_robin', label: 'Round Robin', icon: 'fa-arrows-spin', desc: 'Even distribution' },
                { key: 'highest_health', label: 'Top Health', icon: 'fa-trophy', desc: 'Always use healthiest' },
            ];
            strategies.forEach(function(s) {
                var active = strategy === s.key;
                html += '<button onclick="nhSetStrategy(\'' + s.key + '\')" title="' + _esc(s.desc) + '" style="flex:1;padding:6px 8px;border-radius:6px;font-size:.75rem;font-weight:600;cursor:pointer;border:1px solid ' + (active ? 'rgba(0,217,255,0.3)' : 'rgba(255,255,255,0.06)') + ';background:' + (active ? 'rgba(0,217,255,0.08)' : 'rgba(255,255,255,0.02)') + ';color:' + (active ? '#00d9ff' : '#888') + ';">';
                html += '<i class="fa-solid ' + s.icon + ' me-1"></i>' + s.label;
                html += '</button>';
            });
            html += '</div>';

            // ── Summary KPIs ──
            if (sum.total_numbers) {
                var avgHealthColor = sum.avg_health >= 80 ? '#00ff88' : (sum.avg_health >= 60 ? '#4ade80' : (sum.avg_health >= 40 ? '#ffa500' : '#ef4444'));
                html += '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:14px;">';

                html += _nhKpiCard('Avg Health', Math.round(sum.avg_health), avgHealthColor, 'fa-heart-pulse');
                html += _nhKpiCard('Active', sum.active_count + '/' + sum.total_numbers, '#4ade80', 'fa-circle-check');
                html += _nhKpiCard('Today', sum.daily_calls + ' calls', '#00d9ff', 'fa-phone');
                var dailyCR = sum.daily_connect_rate || 0;
                var dailyCRColor = sum.daily_calls < 50 ? '#888' : (dailyCR >= 30 ? '#00ff88' : '#ffa500');
                html += _nhKpiCard('Connect', sum.daily_calls < 50 ? '—' : dailyCR + '%', dailyCRColor, 'fa-link');

                html += '</div>';
            }

            // ── Per-number health cards ──
            if (nums.length) {
                html += '<div style="display:flex;flex-direction:column;gap:6px;">';
                nums.forEach(function(n) {
                    html += _nhNumberCard(n);
                });
                html += '</div>';
            }

            // ── States Licensed In ──
            html += '<div style="margin-top:16px;">';
            html += '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;">';
            html += '<h6 style="margin:0;font-weight:700;color:#fff;font-size:.82rem;"><i class="fa-solid fa-id-card me-2" style="color:#a78bfa;"></i>States Licensed In</h6>';
            html += '<button onclick="nhOpenStatePicker()" style="background:rgba(167,139,250,0.08);border:1px solid rgba(167,139,250,0.2);color:#a78bfa;border-radius:6px;padding:4px 12px;font-size:.75rem;font-weight:600;cursor:pointer;"><i class="fa-solid fa-pen me-1"></i>Edit States</button>';
            html += '</div>';

            var licensedStates = d.licensed_states || [];
            var licensedCoverage = d.licensed_coverage || [];

            if (!licensedStates.length) {
                html += '<div style="padding:20px;background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.06);border-radius:10px;text-align:center;">';
                html += '<i class="fa-solid fa-map-location-dot" style="font-size:1.4rem;display:block;margin-bottom:8px;color:#444;"></i>';
                html += '<div style="color:#888;font-size:.78rem;margin-bottom:10px;">Select the states you\'re licensed to sell in.<br>We\'ll show your number coverage for each state.</div>';
                html += '<button onclick="nhOpenStatePicker()" style="background:linear-gradient(135deg,#a78bfa,#7c3aed);border:none;color:#fff;border-radius:8px;padding:8px 20px;font-size:.82rem;font-weight:700;cursor:pointer;"><i class="fa-solid fa-plus me-1"></i>Select States</button>';
                html += '</div>';
            } else {
                // Coverage table for licensed states
                html += '<div style="display:flex;flex-direction:column;gap:4px;">';
                licensedCoverage.forEach(function(s) {
                    var covered = s.owned >= 2;
                    var partial = s.owned === 1;
                    var bgColor = covered ? 'rgba(0,255,136,0.03)' : (partial ? 'rgba(255,165,0,0.04)' : 'rgba(239,68,68,0.04)');
                    var borderColor = covered ? 'rgba(0,255,136,0.1)' : (partial ? 'rgba(255,165,0,0.1)' : 'rgba(239,68,68,0.1)');
                    var statusColor = covered ? '#00ff88' : (partial ? '#ffa500' : '#ef4444');
                    var statusIcon = covered ? 'fa-circle-check' : (partial ? 'fa-circle-half-stroke' : 'fa-circle-xmark');
                    var statusText = covered ? s.owned + ' numbers' : (partial ? '1 number' : 'No numbers');

                    html += '<div style="display:flex;align-items:center;justify-content:space-between;padding:8px 10px;background:' + bgColor + ';border:1px solid ' + borderColor + ';border-radius:6px;">';
                    html += '<div style="display:flex;align-items:center;gap:8px;">';
                    html += '<i class="fa-solid ' + statusIcon + '" style="color:' + statusColor + ';font-size:.75rem;"></i>';
                    html += '<span style="color:#ccc;font-size:.78rem;font-weight:600;">' + _esc(s.state_name) + '</span>';
                    html += '<span style="color:' + statusColor + ';font-size:.75rem;">' + statusText + '</span>';
                    html += '</div>';

                    if (!covered) {
                        html += '<button onclick="nhBuyForState(\'' + _esc(s.state) + '\')" style="background:linear-gradient(135deg,#00d9ff,#0099cc);border:none;color:#000;border-radius:5px;padding:3px 10px;font-size:.75rem;font-weight:700;cursor:pointer;white-space:nowrap;">';
                        html += '<i class="fa-solid fa-plus me-1"></i>Buy Number';
                        html += '</button>';
                    }

                    html += '</div>';
                });
                html += '</div>';
            }

            // State picker modal (hidden by default)
            html += '<div id="nhStatePickerModal" style="display:none;"></div>';

            html += '</div>';

            el.innerHTML = html;
        }

        function _nhKpiCard(label, value, color, icon) {
            return '<div style="padding:10px;background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.06);border-radius:8px;text-align:center;">' +
                '<div style="font-size:1.1rem;font-weight:800;color:' + color + ';">' +
                '<i class="fa-solid ' + icon + '" style="font-size:.75rem;opacity:.6;margin-right:3px;"></i>' + value +
                '</div>' +
                '<div style="font-size:.75rem;color:#666;text-transform:uppercase;letter-spacing:.5px;margin-top:2px;">' + label + '</div>' +
                '</div>';
        }

        function _nhNumberCard(n) {
            var score = n.health_score || 0;
            var barColor, statusColor, statusBg, statusIcon;

            if (n.status === 'frozen') {
                barColor = '#8b5cf6';
                statusColor = '#8b5cf6';
                statusBg = 'rgba(139,92,246,0.1)';
                statusIcon = 'fa-snowflake';
            } else if (n.status === 'resting') {
                barColor = '#fbbf24';
                statusColor = '#fbbf24';
                statusBg = 'rgba(251,191,36,0.1)';
                statusIcon = 'fa-moon';
            } else if (score >= 80) {
                barColor = '#00ff88';
                statusColor = '#00ff88';
                statusBg = 'rgba(0,255,136,0.06)';
                statusIcon = 'fa-heart';
            } else if (score >= 60) {
                barColor = '#4ade80';
                statusColor = '#4ade80';
                statusBg = 'rgba(74,222,128,0.06)';
                statusIcon = 'fa-heart';
            } else if (score >= 40) {
                barColor = '#ffa500';
                statusColor = '#ffa500';
                statusBg = 'rgba(255,165,0,0.06)';
                statusIcon = 'fa-heart-crack';
            } else {
                barColor = '#ef4444';
                statusColor = '#ef4444';
                statusBg = 'rgba(239,68,68,0.06)';
                statusIcon = 'fa-heart-crack';
            }

            var dailyPct = n.daily_cap > 0 ? Math.min(100, Math.round(n.daily_calls / n.daily_cap * 100)) : 0;
            var connectRate = n.connect_rate || 0;
            var totalCalls = n.total_calls || 0;

            var html = '<div style="padding:10px 12px;background:' + statusBg + ';border:1px solid rgba(255,255,255,0.06);border-radius:8px;">';

            // Row 1: Phone + badges + health score
            html += '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;gap:8px;">';
            html += '<div style="display:flex;align-items:center;gap:6px;flex:1;min-width:0;overflow:hidden;flex-wrap:wrap;">';
            html += '<div style="display:flex;flex-direction:column;gap:2px;min-width:0;">';
            html += '<span style="color:#fff;font-weight:600;font-size:.8rem;">' + _esc(_fmtPhone(n.phone)) + '</span>';
            if (n.is_primary) html += '<span style="background:rgba(0,217,255,0.15);color:#00d9ff;padding:1px 5px;border-radius:3px;font-size:.65rem;font-weight:700;width:fit-content;">PRIMARY</span>';
            html += '</div>';
            if (n.state) html += '<span style="background:rgba(0,217,255,0.08);color:#00d9ff;padding:1px 5px;border-radius:3px;font-size:.75rem;font-weight:700;letter-spacing:.3px;">' + _esc(n.state) + '</span>';
            if (n.nickname) html += '<span style="color:#666;font-size:.75rem;">(' + _esc(n.nickname) + ')</span>';
            if (_nhData && _nhData.spam_protected) html += '<span title="A2P Registered — STIR/SHAKEN verified" style="background:rgba(0,255,136,0.08);color:#00ff88;padding:1px 5px;border-radius:3px;font-size:.75rem;font-weight:700;cursor:help;"><i class="fa-solid fa-shield-halved" style="font-size:.75rem;margin-right:2px;"></i>Protected</span>';
            html += '</div>';

            // Health score badge
            html += '<div style="display:flex;align-items:center;gap:6px;flex-shrink:0;">';
            if (n.status === 'frozen' || n.status === 'resting') {
                html += '<span style="background:' + statusBg + ';color:' + statusColor + ';padding:2px 8px;border-radius:4px;font-size:.75rem;font-weight:700;text-transform:uppercase;">';
                html += '<i class="fa-solid ' + statusIcon + ' me-1"></i>' + n.status;
                html += '</span>';
            }
            html += '<div style="width:36px;height:36px;border-radius:50%;background:rgba(0,0,0,0.3);border:2px solid ' + barColor + ';display:flex;align-items:center;justify-content:center;">';
            html += '<span style="font-size:.75rem;font-weight:800;color:' + barColor + ';">' + Math.round(score) + '</span>';
            html += '</div></div></div>';

            // Row 2: Metric pills
            html += '<div style="display:flex;gap:4px;flex-wrap:wrap;margin-bottom:6px;">';
            html += _nhPill('fa-phone', n.daily_calls + '/' + n.daily_cap + ' today', dailyPct >= 80 ? '#ffa500' : '#888');
            // Connect rate: show neutral gray when insufficient data (under 100 calls),
            // only color-code when there's enough signal to be meaningful
            var crColor = totalCalls < 100 ? '#888' : (connectRate >= 30 ? '#4ade80' : (connectRate >= 15 ? '#ffa500' : '#ef4444'));
            var crLabel = totalCalls < 100 ? (totalCalls === 0 ? 'No data' : connectRate.toFixed(0) + '%') : connectRate.toFixed(0) + '% connect';
            html += _nhPill('fa-link', crLabel, crColor);
            var blocked = n.total_carrier_blocked || 0;
            if (blocked > 0) html += _nhPill('fa-shield-halved', blocked + ' blocked', blocked >= 10 ? '#ef4444' : '#ffa500');
            html += _nhPill('fa-chart-line', totalCalls + ' lifetime', '#888');
            var warmupColors = { 0: '#888', 1: '#ffa500', 2: '#fbbf24', 3: '#00d9ff', 4: '#4ade80' };
            html += _nhPill('fa-seedling', n.warmup_label || 'Stage ' + n.warmup_stage, warmupColors[n.warmup_stage] || '#00d9ff');
            html += '</div>';

            // Row 3: Health bar
            html += '<div style="height:3px;background:rgba(255,255,255,0.06);border-radius:2px;overflow:hidden;">';
            html += '<div style="height:100%;width:' + score + '%;background:' + barColor + ';border-radius:2px;transition:width .5s ease;"></div>';
            html += '</div>';

            // Row 4: Actions
            if (n.status === 'frozen' || n.status === 'resting') {
                html += '<div style="margin-top:6px;text-align:right;">';
                html += '<button onclick="nhSetNumberStatus(\'' + _esc(n.phone) + '\',\'active\')" style="background:rgba(0,255,136,0.08);border:1px solid rgba(0,255,136,0.15);color:#00ff88;border-radius:4px;padding:3px 10px;font-size:.75rem;font-weight:600;cursor:pointer;"><i class="fa-solid fa-play me-1"></i>Reactivate</button>';
                html += '</div>';
            } else if (score < 40 && totalCalls >= 5) {
                html += '<div style="margin-top:6px;display:flex;justify-content:flex-end;gap:6px;">';
                html += '<button onclick="nhSetNumberStatus(\'' + _esc(n.phone) + '\',\'resting\',24)" style="background:rgba(251,191,36,0.08);border:1px solid rgba(251,191,36,0.15);color:#fbbf24;border-radius:4px;padding:3px 10px;font-size:.75rem;font-weight:600;cursor:pointer;"><i class="fa-solid fa-moon me-1"></i>Rest 24h</button>';
                html += '<button onclick="nhSetNumberStatus(\'' + _esc(n.phone) + '\',\'frozen\')" style="background:rgba(139,92,246,0.08);border:1px solid rgba(139,92,246,0.15);color:#8b5cf6;border-radius:4px;padding:3px 10px;font-size:.75rem;font-weight:600;cursor:pointer;"><i class="fa-solid fa-snowflake me-1"></i>Freeze</button>';
                html += '</div>';
            }

            html += '</div>';
            return html;
        }

        function _nhPill(icon, text, color) {
            return '<span style="display:inline-flex;align-items:center;gap:3px;background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.05);border-radius:4px;padding:2px 6px;font-size:.75rem;color:' + (color || '#888') + ';">' +
                '<i class="fa-solid ' + icon + '" style="font-size:.75rem;opacity:.7;"></i>' + text +
                '</span>';
        }

        async function nhToggleRotation() {
            var enabled = !(_nhData && _nhData.rotation_enabled);
            try {
                var r = await fetch('/voice/number-health/toggle', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ enabled: enabled, strategy: (_nhData && _nhData.rotation_strategy) || 'weighted_health' }),
                });
                if (r.ok) {
                    if (typeof _showDashToast === 'function') _showDashToast(true, 'Smart Rotation ' + (enabled ? 'enabled' : 'disabled'));
                    loadNumberHealth();
                } else {
                    if (typeof _showDashToast === 'function') _showDashToast(false, 'Failed to toggle rotation');
                }
            } catch(e) {
                console.error('[NumberHealth] Toggle error:', e);
                if (typeof _showDashToast === 'function') _showDashToast(false, 'Network error');
            }
        }

        async function nhSetStrategy(strategy) {
            try {
                var r = await fetch('/voice/number-health/toggle', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ enabled: true, strategy: strategy }),
                });
                if (r.ok) {
                    if (typeof _showDashToast === 'function') _showDashToast(true, 'Strategy: ' + strategy.replace(/_/g, ' '));
                    loadNumberHealth();
                } else {
                    if (typeof _showDashToast === 'function') _showDashToast(false, 'Failed to set strategy');
                }
            } catch(e) {
                console.error('[NumberHealth] Strategy error:', e);
                if (typeof _showDashToast === 'function') _showDashToast(false, 'Network error');
            }
        }

        async function nhSetNumberStatus(phone, status, restHours) {
            // Confirmation for potentially impactful actions
            if (status === 'resting' && !confirm('Rest ' + _fmtPhone(phone) + ' for ' + (restHours || 24) + ' hours?\nThis number will be removed from the active rotation pool.')) return;
            if (status === 'frozen' && !confirm('Freeze ' + _fmtPhone(phone) + '?\nThis number will be quarantined until manually reactivated.')) return;
            try {
                var r = await fetch('/voice/number-health/set-status', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ phone: phone, status: status, rest_hours: restHours || null }),
                });
                if (r.ok) {
                    if (typeof _showDashToast === 'function') _showDashToast(true, _fmtPhone(phone) + ' → ' + status);
                    loadNumberHealth();
                } else {
                    if (typeof _showDashToast === 'function') _showDashToast(false, 'Failed to update status');
                }
            } catch(e) {
                console.error('[NumberHealth] Set status error:', e);
                if (typeof _showDashToast === 'function') _showDashToast(false, 'Network error');
            }
        }

        // ── Licensed States: state picker + buy-for-state ──
        var _allStates = [
            ["AL","Alabama"],["AK","Alaska"],["AZ","Arizona"],["AR","Arkansas"],
            ["CA","California"],["CO","Colorado"],["CT","Connecticut"],["DE","Delaware"],
            ["FL","Florida"],["GA","Georgia"],["HI","Hawaii"],["ID","Idaho"],
            ["IL","Illinois"],["IN","Indiana"],["IA","Iowa"],["KS","Kansas"],
            ["KY","Kentucky"],["LA","Louisiana"],["ME","Maine"],["MD","Maryland"],
            ["MA","Massachusetts"],["MI","Michigan"],["MN","Minnesota"],["MS","Mississippi"],
            ["MO","Missouri"],["MT","Montana"],["NE","Nebraska"],["NV","Nevada"],
            ["NH","New Hampshire"],["NJ","New Jersey"],["NM","New Mexico"],["NY","New York"],
            ["NC","North Carolina"],["ND","North Dakota"],["OH","Ohio"],["OK","Oklahoma"],
            ["OR","Oregon"],["PA","Pennsylvania"],["RI","Rhode Island"],["SC","South Carolina"],
            ["SD","South Dakota"],["TN","Tennessee"],["TX","Texas"],["UT","Utah"],
            ["VT","Vermont"],["VA","Virginia"],["WA","Washington"],["WV","West Virginia"],
            ["WI","Wisconsin"],["WY","Wyoming"]
        ];

        function nhOpenStatePicker() {
            var modal = document.getElementById('nhStatePickerModal');
            if (!modal) return;
            var current = (_nhData && _nhData.licensed_states) || [];
            var html = '';
            html += '<div style="position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.6);z-index:9998;display:flex;align-items:center;justify-content:center;" onclick="if(event.target===this)nhCloseStatePicker()">';
            html += '<div style="background:#1a1a2e;border:1px solid rgba(255,255,255,0.1);border-radius:16px;padding:24px;width:520px;max-height:80vh;overflow-y:auto;" onclick="event.stopPropagation()">';
            html += '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;">';
            html += '<h6 style="margin:0;font-weight:700;color:#fff;"><i class="fa-solid fa-id-card me-2" style="color:#a78bfa;"></i>States Licensed In</h6>';
            html += '<button onclick="nhCloseStatePicker()" style="background:none;border:none;color:#666;cursor:pointer;font-size:1.1rem;"><i class="fa-solid fa-xmark"></i></button>';
            html += '</div>';
            html += '<p style="color:#888;font-size:.78rem;margin-bottom:14px;">Select every state where you hold an active insurance license. We\'ll track your number coverage for each.</p>';

            // Select all / clear
            html += '<div style="display:flex;gap:8px;margin-bottom:12px;">';
            html += '<button onclick="document.querySelectorAll(\'#nhStateGrid input\').forEach(function(c){c.checked=true})" style="background:rgba(0,217,255,0.06);border:1px solid rgba(0,217,255,0.12);color:#00d9ff;border-radius:5px;padding:3px 10px;font-size:.75rem;cursor:pointer;">Select All</button>';
            html += '<button onclick="document.querySelectorAll(\'#nhStateGrid input\').forEach(function(c){c.checked=false})" style="background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.06);color:#888;border-radius:5px;padding:3px 10px;font-size:.75rem;cursor:pointer;">Clear All</button>';
            html += '</div>';

            // State grid
            html += '<div id="nhStateGrid" style="display:grid;grid-template-columns:repeat(3,1fr);gap:4px;margin-bottom:18px;">';
            _allStates.forEach(function(s) {
                var code = s[0], name = s[1];
                var checked = current.indexOf(code) >= 0 ? ' checked' : '';
                html += '<label style="display:flex;align-items:center;gap:6px;padding:5px 8px;border-radius:6px;cursor:pointer;font-size:.75rem;color:#ccc;background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.04);transition:background .15s;" onmouseover="this.style.background=\'rgba(167,139,250,0.06)\'" onmouseout="this.style.background=\'rgba(255,255,255,0.02)\'">';
                html += '<input type="checkbox" value="' + code + '"' + checked + ' style="accent-color:#a78bfa;width:14px;height:14px;cursor:pointer;">';
                html += '<span><strong>' + code + '</strong> ' + name + '</span>';
                html += '</label>';
            });
            html += '</div>';

            // Save button
            html += '<button onclick="nhSaveLicensedStates()" id="nhSaveStatesBtn" style="background:linear-gradient(135deg,#a78bfa,#7c3aed);border:none;color:#fff;border-radius:10px;padding:10px 0;font-size:.88rem;font-weight:700;cursor:pointer;width:100%;"><i class="fa-solid fa-check me-2"></i>Save Licensed States</button>';
            html += '</div></div>';

            modal.innerHTML = html;
            modal.style.display = 'block';
        }

        function nhCloseStatePicker() {
            var modal = document.getElementById('nhStatePickerModal');
            if (modal) { modal.innerHTML = ''; modal.style.display = 'none'; }
        }

        async function nhSaveLicensedStates() {
            var checkboxes = document.querySelectorAll('#nhStateGrid input:checked');
            var states = [];
            checkboxes.forEach(function(c) { states.push(c.value); });
            var btn = document.getElementById('nhSaveStatesBtn');
            if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-2"></i>Saving...'; }
            try {
                var r = await fetch('/voice/licensed-states', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ states: states }),
                });
                if (r.ok) {
                    nhCloseStatePicker();
                    if (typeof _showDashToast === 'function') _showDashToast(true, states.length + ' licensed state' + (states.length !== 1 ? 's' : '') + ' saved');
                    loadNumberHealth();
                } else {
                    if (typeof _showDashToast === 'function') _showDashToast(false, 'Failed to save');
                    if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-check me-2"></i>Save Licensed States'; }
                }
            } catch(e) {
                console.error('[LicensedStates] Save error:', e);
                if (typeof _showDashToast === 'function') _showDashToast(false, 'Network error');
                if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-check me-2"></i>Save Licensed States'; }
            }
        }

        function nhBuyForState(stateCode) {
            // Open the buy panel with state pre-filled
            var buyPanel = document.getElementById('vstabBuyPanel');
            if (buyPanel) buyPanel.style.display = 'block';
            var stateInput = document.getElementById('buyState');
            if (stateInput) stateInput.value = stateCode;
            // Clear other fields so the search is state-only
            var areaInput = document.getElementById('buyAreaCode');
            if (areaInput) areaInput.value = '';
            var cityInput = document.getElementById('buyCity');
            if (cityInput) cityInput.value = '';
            var zipInput = document.getElementById('buyZip');
            if (zipInput) zipInput.value = '';
            var containsInput = document.getElementById('buyContains');
            if (containsInput) containsInput.value = '';
            // Auto-trigger search
            searchAvailableNumbers();
            // Scroll buy panel into view
            buyPanel.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }

        // Number health now has its own panel — no longer auto-loads with numbers tab


        // ===== A2P 10DLC TAB =====
        var _a2pStatus = null;
        var _a2pNumbersCache = [];

        async function a2pLoadStatus() {
            try {
                const r = await fetch('/voice/a2p/status');
                const d = await r.json();
                if (!r.ok) { console.warn('[A2P] Status error:', d); return; }
                _a2pStatus = d;
                a2pRenderUI(d);
            } catch(e) { console.error('[A2P] Load status error:', e); }
        }

        function a2pRenderUI(d) {
            const banner = document.getElementById('a2pStatusBanner');
            const payGate = document.getElementById('a2pPaymentGate');
            const brandForm = document.getElementById('a2pBrandForm');
            const campaignForm = document.getElementById('a2pCampaignForm');
            const statusPanel = document.getElementById('a2pBrandStatusPanel');
            const registerPanel = document.getElementById('a2pRegisterPanel');

            // Payment gate for sub-users
            if (d.is_sub_user && !d.a2p_fee_paid && !d.registered) {
                if (payGate) payGate.style.display = 'block';
                if (registerPanel) registerPanel.style.display = 'none';
                if (banner) banner.style.display = 'none';
                if (statusPanel) statusPanel.style.display = 'none';
                return;
            } else {
                if (payGate) payGate.style.display = 'none';
            }

            // Has both brand AND campaign (registered or pending)
            if (d.brand_sid && d.campaign_sid) {
                if (registerPanel) registerPanel.style.display = 'none';
                if (statusPanel) statusPanel.style.display = 'none';
                if (banner) {
                    banner.style.display = 'block';
                    var brandSt = (d.brand_status || 'PENDING').toUpperCase();
                    var campSt = (d.campaign_status || 'PENDING').toUpperCase();
                    var brandColor = brandSt === 'APPROVED' ? '#00ff88' : brandSt === 'FAILED' ? '#ef4444' : '#fbbf24';
                    var campColor = (campSt === 'VERIFIED' || campSt === 'APPROVED') ? '#00ff88' : campSt === 'FAILED' ? '#ef4444' : '#fbbf24';
                    var brandLabel = brandSt === 'APPROVED' ? 'Approved' : brandSt;
                    var campLabel = (campSt === 'VERIFIED' || campSt === 'APPROVED') ? 'Approved' : campSt;
                    var allGood = brandSt === 'APPROVED' && (campSt === 'VERIFIED' || campSt === 'APPROVED');
                    var registeredSids = d.registered_number_sids || [];
                    var headerColor = allGood ? '#00ff88' : '#fbbf24';
                    var headerIcon = allGood ? 'fa-certificate' : 'fa-hourglass-half';
                    var headerText = allGood ? 'A2P 10DLC Registered' : 'A2P 10DLC Registration In Progress';
                    var headerBg = allGood ? 'rgba(0,255,136,0.06)' : 'rgba(251,191,36,0.06)';
                    var headerBorder = allGood ? 'rgba(0,255,136,0.2)' : 'rgba(251,191,36,0.2)';
                    var numCountHtml = allGood && registeredSids.length > 0
                        ? '<div style="font-size:0.75rem;color:#888;">' + registeredSids.length + ' number' + (registeredSids.length !== 1 ? 's' : '') + ' in messaging service</div>'
                        : '';

                    banner.innerHTML =
                        '<div class="p-3" style="background:' + headerBg + ';border:1px solid ' + headerBorder + ';border-radius:12px;">' +
                            '<div class="d-flex align-items-center gap-3 mb-3">' +
                                '<div style="width:36px;height:36px;border-radius:50%;background:' + (allGood ? 'rgba(0,255,136,0.15)' : 'rgba(251,191,36,0.15)') + ';display:flex;align-items:center;justify-content:center;flex-shrink:0;">' +
                                    '<i class="fa-solid ' + headerIcon + '" style="color:' + headerColor + ';"></i>' +
                                '</div>' +
                                '<div style="flex:1;">' +
                                    '<div style="font-weight:700;color:' + headerColor + ';font-size:0.9rem;">' + headerText + '</div>' +
                                    (d.registered_at ? '<div style="font-size:0.75rem;color:#666;">Registered ' + new Date(d.registered_at).toLocaleDateString() + '</div>' : '') +
                                    numCountHtml +
                                '</div>' +
                                '<button onclick="a2pRefreshStatus()" style="background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);color:#aaa;border-radius:6px;padding:5px 12px;font-size:0.75rem;cursor:pointer;white-space:nowrap;">' +
                                    '<i class="fa-solid fa-arrows-rotate me-1"></i>Refresh' +
                                '</button>' +
                            '</div>' +
                            // Brand row
                            '<div style="display:flex;align-items:center;gap:10px;padding:8px 12px;background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.04);border-radius:8px;margin-bottom:6px;">' +
                                '<i class="fa-solid fa-building" style="color:#a78bfa;width:16px;text-align:center;"></i>' +
                                '<span style="font-weight:600;color:#ccc;font-size:0.78rem;width:70px;">Brand</span>' +
                                '<span style="background:' + brandColor + '20;color:' + brandColor + ';padding:2px 8px;border-radius:4px;font-size:0.75rem;font-weight:700;">' + _esc(brandLabel) + '</span>' +
                                '<span style="color:#555;font-family:\'JetBrains Mono\',monospace;font-size:0.75rem;margin-left:auto;">' + _esc(d.brand_sid) + '</span>' +
                            '</div>' +
                            // Campaign row
                            '<div style="display:flex;align-items:center;gap:10px;padding:8px 12px;background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.04);border-radius:8px;margin-bottom:6px;">' +
                                '<i class="fa-solid fa-bullhorn" style="color:#00d9ff;width:16px;text-align:center;"></i>' +
                                '<span style="font-weight:600;color:#ccc;font-size:0.78rem;width:70px;">Campaign</span>' +
                                '<span style="background:' + campColor + '20;color:' + campColor + ';padding:2px 8px;border-radius:4px;font-size:0.75rem;font-weight:700;">' + _esc(campLabel) + '</span>' +
                                '<span style="color:#555;font-family:\'JetBrains Mono\',monospace;font-size:0.75rem;margin-left:auto;">' + _esc(d.campaign_sid) + '</span>' +
                            '</div>' +
                            // Messaging service row (if present)
                            (d.messaging_service_sid ?
                                '<div style="display:flex;align-items:center;gap:10px;padding:8px 12px;background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.04);border-radius:8px;">' +
                                    '<i class="fa-solid fa-envelope" style="color:#ffa500;width:16px;text-align:center;"></i>' +
                                    '<span style="font-weight:600;color:#ccc;font-size:0.78rem;width:70px;">Msg Svc</span>' +
                                    '<span style="background:rgba(0,255,136,0.12);color:#00ff88;padding:2px 8px;border-radius:4px;font-size:0.75rem;font-weight:700;">Active</span>' +
                                    '<span style="color:#555;font-family:\'JetBrains Mono\',monospace;font-size:0.75rem;margin-left:auto;">' + _esc(d.messaging_service_sid) + '</span>' +
                                '</div>' : ''
                            ) +
                            // Pending note
                            (!allGood ?
                                '<div style="margin-top:10px;padding:8px 12px;background:rgba(251,191,36,0.05);border:1px solid rgba(251,191,36,0.1);border-radius:8px;font-size:0.75rem;color:#fbbf24;">' +
                                    '<i class="fa-solid fa-clock me-1"></i>Registration is pending carrier approval. Click Refresh to check for updates.' +
                                '</div>' : ''
                            ) +
                        '</div>';
                }
                return;
            }

            // Brand submitted but no campaign yet
            if (d.brand_sid && !d.campaign_sid) {
                if (banner) banner.style.display = 'none';
                if (statusPanel) {
                    statusPanel.style.display = 'block';
                    a2pRenderBrandStatus(d);
                }
                // Show campaign form if brand is approved
                const approved = (d.brand_status || '').toUpperCase() === 'APPROVED';
                if (approved && registerPanel) {
                    registerPanel.style.display = 'block';
                    if (brandForm) brandForm.style.display = 'none';
                    if (campaignForm) campaignForm.style.display = 'block';
                    a2pUpdateStepPills(2);
                    a2pLoadNumbersForCampaign('a2pCampaignNumbersList');
                } else if (registerPanel) {
                    registerPanel.style.display = 'none';
                }
                return;
            }

            // Fresh state: show registration form directly
            if (banner) banner.style.display = 'none';
            if (statusPanel) statusPanel.style.display = 'none';
            if (registerPanel) {
                registerPanel.style.display = 'block';
                if (brandForm) brandForm.style.display = 'block';
                if (campaignForm) campaignForm.style.display = 'none';
                a2pUpdateStepPills(1);
            }
        }

        function a2pRenderBrandStatus(d) {
            const el = document.getElementById('a2pBrandStatusContent');
            if (!el) return;
            const s = (d.brand_status || 'PENDING').toUpperCase();
            const colors = { APPROVED: '#00ff88', PENDING: '#fbbf24', IN_REVIEW: '#00d9ff', FAILED: '#ef4444' };
            const color = colors[s] || '#888';
            el.innerHTML =
                '<div style="display:flex;align-items:center;gap:10px;padding:8px 0;">' +
                    '<div style="display:flex;align-items:center;gap:6px;">' +
                        '<span style="font-weight:700;color:#ccc;">Brand:</span>' +
                        '<span style="color:' + color + ';font-weight:600;">' + _esc(s) + '</span>' +
                        '<span style="color:#555;font-family:\'JetBrains Mono\',monospace;font-size:0.75rem;">' + _esc(d.brand_sid || '') + '</span>' +
                    '</div>' +
                '</div>' +
                (d.campaign_sid ?
                    '<div style="display:flex;align-items:center;gap:6px;padding:4px 0;">' +
                        '<span style="font-weight:700;color:#ccc;">Campaign:</span>' +
                        '<span style="color:#00d9ff;font-weight:600;">' + _esc(d.campaign_status || 'PENDING') + '</span>' +
                        '<span style="color:#555;font-family:\'JetBrains Mono\',monospace;font-size:0.75rem;">' + _esc(d.campaign_sid) + '</span>' +
                    '</div>' : ''
                ) +
                (s !== 'APPROVED' && s !== 'FAILED' ?
                    '<div style="margin-top:6px;font-size:0.75rem;color:#888;"><i class="fa-solid fa-clock me-1" style="color:#fbbf24;"></i>Brand vetting typically takes 1-7 business days. Click Refresh to check.</div>' : ''
                );
        }




        function a2pUpdateStepPills(step) {
            var s1 = document.getElementById('a2pStep1Pill');
            var s2 = document.getElementById('a2pStep2Pill');
            if (s1) {
                s1.style.background = step >= 1 ? 'rgba(167,139,250,0.1)' : 'rgba(255,255,255,0.02)';
                s1.style.border = step >= 1 ? '1px solid rgba(167,139,250,0.25)' : '1px solid rgba(255,255,255,0.06)';
                s1.style.color = step >= 1 ? '#a78bfa' : '#555';
            }
            if (s2) {
                s2.style.background = step >= 2 ? 'rgba(0,217,255,0.1)' : 'rgba(255,255,255,0.02)';
                s2.style.border = step >= 2 ? '1px solid rgba(0,217,255,0.25)' : '1px solid rgba(255,255,255,0.06)';
                s2.style.color = step >= 2 ? '#00d9ff' : '#555';
            }
        }

        async function a2pLoadNumbersForCampaign(containerId) {
            const el = document.getElementById(containerId);
            if (!el) return;
            if (_a2pNumbersCache.length) { a2pRenderNumberCheckboxes(el, _a2pNumbersCache); return; }
            el.innerHTML = '<span style="color:#555;"><i class="fa-solid fa-spinner fa-spin me-1" style="color:#00d9ff;"></i>Loading...</span>';
            try {
                const r = await fetch('/voice/numbers');
                const d = await r.json();
                if (!r.ok || !d.numbers) { el.innerHTML = '<span style="color:#888;">No numbers found.</span>'; return; }
                _a2pNumbersCache = d.numbers;
                a2pRenderNumberCheckboxes(el, d.numbers);
            } catch(e) { el.innerHTML = '<span style="color:#ef4444;">Failed to load numbers.</span>'; }
        }

        function a2pRenderNumberCheckboxes(el, numbers) {
            if (!numbers.length) { el.innerHTML = '<span style="color:#888;">No numbers. Buy a number in the Numbers tab first.</span>'; return; }
            el.innerHTML = numbers.map(function(n) {
                return '<label style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid rgba(255,255,255,0.03);cursor:pointer;">' +
                    '<input type="checkbox" class="a2p-number-cb" value="' + _esc(n.sid) + '" checked style="accent-color:#00d9ff;">' +
                    '<span style="color:#ccc;">' + _esc(_fmtPhone(n.phone)) + '</span>' +
                    (n.nickname ? '<span style="color:#555;font-size:0.75rem;">(' + _esc(n.nickname) + ')</span>' : '') +
                    '<span style="margin-left:auto;font-size:0.75rem;color:#00d9ff;">' +
                        (n.capabilities && n.capabilities.sms ? 'SMS ' : '') +
                        (n.capabilities && n.capabilities.voice ? 'Voice' : '') +
                    '</span>' +
                '</label>';
            }).join('');
        }

        function a2pGetSelectedNumberSids(containerId) {
            var el = document.getElementById(containerId);
            if (!el) return [];
            return Array.from(el.querySelectorAll('.a2p-number-cb:checked')).map(function(cb) { return cb.value; });
        }

        // ── Register New Brand ──
        async function a2pRegisterBrand() {
            var result = document.getElementById('a2pBrandResult');
            var btn = document.getElementById('a2pRegisterBrandBtn');
            var brandType = (document.getElementById('a2pBrandType')?.value || 'LOW_VOLUME');
            var payload = {
                business_name: (document.getElementById('a2pBizName')?.value || '').trim(),
                ein: (document.getElementById('a2pEIN')?.value || '').trim(),
                street: (document.getElementById('a2pStreet')?.value || '').trim(),
                city: (document.getElementById('a2pCity')?.value || '').trim(),
                state: (document.getElementById('a2pState')?.value || '').trim(),
                zip: (document.getElementById('a2pZip')?.value || '').trim(),
                contact_email: (document.getElementById('a2pContactEmail')?.value || '').trim(),
                contact_phone: (document.getElementById('a2pContactPhone')?.value || '').trim(),
                website: (document.getElementById('a2pWebsite')?.value || '').trim(),
                brand_type: brandType,
            };
            if (!payload.business_name) { result.innerHTML = '<span style="color:#ef4444;">Business name required</span>'; return; }
            if (brandType !== 'SOLE_PROPRIETOR' && !payload.ein) { result.innerHTML = '<span style="color:#ef4444;">EIN required for ' + (_a2pFees[brandType]?.label || brandType) + ' brands</span>'; return; }
            if (!payload.contact_email) { result.innerHTML = '<span style="color:#ef4444;">Contact email required</span>'; return; }

            btn.disabled = true;
            btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-2"></i>Submitting brand...';
            result.innerHTML = '';
            try {
                var r = await fetch('/voice/a2p/register-brand', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(payload),
                });
                var d = await r.json();
                if (r.ok) {
                    result.innerHTML = '<span style="color:#00ff88;"><i class="fa-solid fa-circle-check me-1"></i>' + _esc(d.message || 'Brand submitted!') + '</span>';
                    setTimeout(function() { a2pLoadStatus(); }, 500);
                } else {
                    if (d.payment_required) {
                        result.innerHTML = '<span style="color:#ffa500;">Payment required. Redirecting...</span>';
                        a2pPayFee();
                    } else {
                        result.innerHTML = '<span style="color:#ef4444;"><i class="fa-solid fa-triangle-exclamation me-1"></i>' + _esc(d.error || 'Registration failed') + '</span>';
                    }
                }
            } catch(e) {
                result.innerHTML = '<span style="color:#ef4444;">Network error</span>';
            }
            btn.disabled = false;
            btn.innerHTML = '<i class="fa-solid fa-paper-plane me-2"></i>Submit Brand for Vetting';
        }

        // ── Create Campaign ──
        async function a2pCreateCampaign() {
            var result = document.getElementById('a2pCampaignResult');
            var btn = document.getElementById('a2pCreateCampaignBtn');
            var description = (document.getElementById('a2pDescription')?.value || '').trim();
            var samplesRaw = (document.getElementById('a2pSampleMessages')?.value || '').trim();
            var messageFlow = (document.getElementById('a2pMessageFlow')?.value || '').trim();
            var useCase = document.getElementById('a2pUseCase')?.value || 'LOW_VOLUME';
            var numberSids = a2pGetSelectedNumberSids('a2pCampaignNumbersList');

            if (description.length < 40) { result.innerHTML = '<span style="color:#ef4444;">Description must be at least 40 characters</span>'; return; }
            var samples = samplesRaw.split('\n').map(function(s) { return s.trim(); }).filter(function(s) { return s.length >= 20; });
            if (samples.length < 2) { result.innerHTML = '<span style="color:#ef4444;">At least 2 sample messages required (min 20 chars each)</span>'; return; }
            if (messageFlow.length < 40) { result.innerHTML = '<span style="color:#ef4444;">Message flow must be at least 40 characters</span>'; return; }

            btn.disabled = true;
            btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-2"></i>Submitting campaign...';
            result.innerHTML = '';
            try {
                var r = await fetch('/voice/a2p/create-campaign', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        description: description,
                        use_case: useCase,
                        sample_messages: samples,
                        message_flow: messageFlow,
                        has_embedded_links: document.getElementById('a2pEmbeddedLinks')?.checked || false,
                        has_embedded_phone: document.getElementById('a2pEmbeddedPhone')?.checked || false,
                        phone_number_sids: numberSids,
                    }),
                });
                var d = await r.json();
                if (r.ok) {
                    result.innerHTML = '<span style="color:#00ff88;"><i class="fa-solid fa-circle-check me-1"></i>' + _esc(d.message || 'Campaign submitted!') + '</span>';
                    setTimeout(function() { a2pLoadStatus(); }, 500);
                } else {
                    result.innerHTML = '<span style="color:#ef4444;"><i class="fa-solid fa-triangle-exclamation me-1"></i>' + _esc(d.error || 'Campaign creation failed') + '</span>';
                }
            } catch(e) {
                result.innerHTML = '<span style="color:#ef4444;">Network error</span>';
            }
            btn.disabled = false;
            btn.innerHTML = '<i class="fa-solid fa-bullhorn me-2"></i>Submit Campaign for Approval';
        }

        // ── Refresh Status ──
        async function a2pRefreshStatus() {
            try {
                if (!_a2pStatus || !_a2pStatus.brand_sid) {
                    // No brand known — try syncing from Twilio first
                    await a2pSyncFromTwilio();
                    return; // a2pSyncFromTwilio already calls a2pLoadStatus
                }
                // Refresh brand status if exists
                if (_a2pStatus.brand_sid) {
                    var r = await fetch('/voice/a2p/brand-status');
                    if (r.ok) {
                        var d = await r.json();
                        _a2pStatus.brand_status = d.status;
                    }
                }
                // Refresh campaign status if exists
                if (_a2pStatus && _a2pStatus.campaign_sid) {
                    var r2 = await fetch('/voice/a2p/campaign-status');
                    if (r2.ok) {
                        var d2 = await r2.json();
                        _a2pStatus.campaign_status = d2.campaign_status;
                    }
                }
            } catch(e) { console.error('[A2P] Refresh error:', e); }
            a2pLoadStatus();
        }

        // ── A2P Fee Schedule (matches backend A2P_FEE_SCHEDULE) ──
        var _a2pFees = {
            SOLE_PROPRIETOR: { brand: 4.50, campaign: 15, label: 'Sole Proprietor' },
            LOW_VOLUME:      { brand: 4.50, campaign: 15, label: 'Low Volume Standard' },
            STANDARD:        { brand: 46,   campaign: 15, label: 'Standard' },
        };

        function a2pGetSelectedBrandType() {
            // Payment gate has its own selector; brand form has another.
            // Payment gate shown for sub-users, brand form for everyone.
            var paySelect = document.getElementById('a2pPayBrandType');
            var brandSelect = document.getElementById('a2pBrandType');
            if (paySelect && paySelect.offsetParent !== null) return paySelect.value;
            if (brandSelect) return brandSelect.value;
            return 'LOW_VOLUME';
        }

        function a2pGetFeeTotal(brandType) {
            var info = _a2pFees[brandType] || _a2pFees.LOW_VOLUME;
            return info.brand + info.campaign;
        }

        // Update the payment gate button price when brand type changes
        function a2pUpdatePaymentPrice() {
            var select = document.getElementById('a2pPayBrandType');
            var label = document.getElementById('a2pPayBtnLabel');
            if (!select || !label) return;
            var total = a2pGetFeeTotal(select.value);
            label.textContent = 'Pay Registration Fee — $' + total.toFixed(2);
            // Sync the brand form selector if it exists
            var brandSelect = document.getElementById('a2pBrandType');
            if (brandSelect) brandSelect.value = select.value;
            a2pUpdateBrandFeeDisplay();
        }

        // Update the fee note in the brand registration form + toggle EIN/SSN label
        function a2pUpdateBrandFeeDisplay() {
            var select = document.getElementById('a2pBrandType');
            var note = document.getElementById('a2pBrandFeeNote');
            if (!select || !note) return;
            var info = _a2pFees[select.value] || _a2pFees.LOW_VOLUME;
            var total = info.brand + info.campaign;
            note.textContent = 'One-time brand fee: $' + info.brand.toFixed(2) + ' + $' + info.campaign.toFixed(2) + ' campaign vetting = $' + total.toFixed(2) + ' total';

            // Toggle EIN/SSN label — sole proprietors use SSN, others use EIN
            var einLabel = document.getElementById('a2pEINLabel');
            var einInput = document.getElementById('a2pEIN');
            if (einLabel && einInput) {
                if (select.value === 'SOLE_PROPRIETOR') {
                    einLabel.textContent = 'SSN (last 4 digits)';
                    einInput.placeholder = 'XXXX';
                    einInput.maxLength = 4;
                } else {
                    einLabel.textContent = 'EIN (Tax ID)';
                    einInput.placeholder = 'XX-XXXXXXX';
                    einInput.maxLength = 10;
                }
            }

            // Sync the payment gate selector if it exists
            var paySelect = document.getElementById('a2pPayBrandType');
            if (paySelect) paySelect.value = select.value;
            a2pUpdatePaymentPrice();
        }

        // ── Pay A2P Fee (Stripe) ──
        async function a2pPayFee() {
            var btn = document.getElementById('a2pPayFeeBtn');
            var result = document.getElementById('a2pPayResult');
            var brandType = a2pGetSelectedBrandType();
            var total = a2pGetFeeTotal(brandType);

            if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-2"></i>Redirecting to payment...'; }
            try {
                var r = await fetch('/a2p/checkout', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ brand_type: brandType }),
                });
                var d = await r.json();
                if (d.checkout_url) {
                    window.top.location.href = d.checkout_url;
                } else {
                    if (result) result.innerHTML = '<span style="color:#ef4444;">' + _esc(d.error || 'Failed to create checkout') + '</span>';
                }
            } catch(e) {
                if (result) result.innerHTML = '<span style="color:#ef4444;">Network error</span>';
            }
            if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-lock me-2"></i><span id="a2pPayBtnLabel">Pay Registration Fee — $' + total.toFixed(2) + '</span>'; }
        }

        // Check URL params for phone number purchase success
        (function() {
            var params = new URLSearchParams(window.location.search);
            if (params.get('number_purchased') === '1') {
                var phone = params.get('phone') || '';
                if (phone) {
                    // Complete the purchase — provision the number after Stripe payment
                    fetch('/voice/numbers/complete-purchase', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({ phone_number: phone })
                    }).then(r => r.json()).then(d => {
                        if (d.status === 'purchased') {
                            if (typeof _showDashToast === 'function') _showDashToast(true, 'Number purchased: ' + (d.phone || phone));
                        } else {
                            if (typeof _showDashToast === 'function') _showDashToast(false, d.error || 'Number provisioning failed');
                        }
                        // Switch to Voice > Numbers tab and reload
                        if (typeof switchVoicePanel === 'function') switchVoicePanel('numbers');
                        if (typeof loadNumbersTab === 'function') loadNumbersTab();
                    }).catch(() => {
                        if (typeof _showDashToast === 'function') _showDashToast(false, 'Network error completing number purchase');
                    });
                }
                // Clean URL
                var url = new URL(window.location);
                url.searchParams.delete('number_purchased');
                url.searchParams.delete('phone');
                window.history.replaceState({}, '', url);
            }
        })();

        // Check URL params for A2P payment success
        (function() {
            var params = new URLSearchParams(window.location.search);
            if (params.get('a2p_payment_success') === '1') {
                // Mark fee as paid via API
                fetch('/voice/a2p/mark-fee-paid', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}' })
                    .then(function() {
                        // Switch to Voice Config > 10DLC tab
                        if (typeof switchVoicePanel === 'function') switchVoicePanel('a2p');
                    });
                // Clean URL
                var url = new URL(window.location);
                url.searchParams.delete('a2p_payment_success');
                window.history.replaceState({}, '', url);
            }
        })();
