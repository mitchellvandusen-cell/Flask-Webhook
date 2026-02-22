        // ===== LOCAL UTILITIES =====
        function _esc(s) { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }
        function _fmtPhone(p) {
            if (!p) return '';
            const d = p.replace(/\D/g, '');
            if (d.length === 11 && d[0] === '1') return '(' + d.substr(1,3) + ') ' + d.substr(4,3) + '-' + d.substr(7);
            if (d.length === 10) return '(' + d.substr(0,3) + ') ' + d.substr(3,3) + '-' + d.substr(6);
            return p;
        }

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
                                '<button onclick="document.getElementById(\'spamProtectionForm\').style.display=document.getElementById(\'spamProtectionForm\').style.display===\'none\'?\'block\':\'none\'" style="background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);color:#aaa;border-radius:6px;padding:5px 12px;font-size:0.72rem;cursor:pointer;white-space:nowrap;">' +
                                    '<i class="fa-solid fa-pen me-1"></i>Edit' +
                                '</button>' +
                            '</div>' +
                        '</div>';
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
                                    (!n.cnam_enabled ? '<button onclick="enableCnamSingle(\'' + n.id + '\')" style="background:rgba(0,255,136,0.08);border:1px solid rgba(0,255,136,0.15);color:#00ff88;border-radius:5px;padding:3px 10px;font-size:.7rem;font-weight:600;cursor:pointer;"><i class="fa-solid fa-shield-halved me-1"></i>Enable</button>' : '') +
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
                const priceMap = { local: '$1.15', toll_free: '$2.15', mobile: '$1.15' };
                const monthlyPrice = priceMap[numberType] || '$1.15';
                list.innerHTML = nums.map(n => {
                    const loc = [n.locality, n.region].filter(Boolean).join(', ');
                    const caps = [];
                    if (n.capabilities && n.capabilities.voice) caps.push('Voice');
                    if (n.capabilities && n.capabilities.sms) caps.push('SMS');
                    if (n.capabilities && n.capabilities.mms) caps.push('MMS');
                    return '<div style="display:flex;align-items:center;justify-content:space-between;padding:8px 4px;border-bottom:1px solid rgba(255,255,255,0.03);font-size:.78rem;">' +
                        '<div style="flex:1;min-width:0;">' +
                            '<span style="color:#fff;font-weight:600;">' + _esc(_fmtPhone(n.phone)) + '</span>' +
                            (loc ? '<span style="color:#666;font-size:.68rem;margin-left:6px;">' + _esc(loc) + '</span>' : '') +
                            '<div style="margin-top:2px;">' + caps.map(c => '<span style="background:rgba(0,217,255,0.08);color:#00d9ff;padding:1px 6px;border-radius:4px;font-size:.65rem;margin-right:3px;">' + c + '</span>').join('') + '</div>' +
                        '</div>' +
                        '<div style="display:flex;align-items:center;gap:8px;flex-shrink:0;">' +
                            '<span style="color:#00ff88;font-size:.72rem;font-weight:600;">' + monthlyPrice + '/mo</span>' +
                            '<button onclick="buyNumber(\'' + n.phone + '\')" style="background:linear-gradient(135deg,#00d9ff,#0099cc);border:none;color:#000;border-radius:4px;padding:3px 10px;font-size:.72rem;font-weight:700;cursor:pointer;">Buy</button>' +
                        '</div>' +
                    '</div>';
                }).join('');
            } catch(e) { list.innerHTML = '<div style="color:#ef4444;">Network error</div>'; }
        }

        async function buyNumber(phone) {
            if (!confirm('Purchase ' + phone + '?\nThis will be added to your account.')) return;
            try {
                const r = await fetch('/voice/numbers/buy', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ phone_number: phone }) });
                const d = await r.json();
                if (!r.ok) { alert(d.error || 'Failed'); return; }
                alert('Purchased: ' + d.phone);
                const buyPanel = document.getElementById('vstabBuyPanel'); if (buyPanel) buyPanel.style.display = 'none';
                loadNumbersTab();
            } catch(e) { alert('Network error'); }
        }
        async function releaseNumber(sid, phone) {
            if (!confirm('Release ' + phone + '?\n\nThis cannot be undone. The number will be removed from your account.')) return;
            try {
                const r = await fetch('/voice/numbers/release', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ sid: sid }) });
                const d = await r.json();
                if (!d.error) loadNumbersTab();
                else alert(d.error);
            } catch(e) { alert('Network error'); }
        }

        // ── Init dialer (runs on page load since Dialer is default tab) ──
        function initDialerTab() {
            dialerLoadPipelines();
            if (!dialerContacts.length) dialerFetchContacts();
            dialerStartPing();
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
