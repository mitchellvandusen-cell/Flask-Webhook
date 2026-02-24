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
                return;
            } else {
                if (payGate) payGate.style.display = 'none';
            }

            // Fully registered: show success banner
            if (d.registered && d.campaign_sid) {
                if (banner) {
                    banner.style.display = 'block';
                    const statusColor = d.campaign_status === 'VERIFIED' ? '#00ff88' : '#fbbf24';
                    const statusLabel = d.campaign_status === 'VERIFIED' ? 'Approved' : (d.campaign_status || 'Pending');
                    banner.innerHTML =
                        '<div class="mb-3 p-3" style="background:rgba(0,255,136,0.06);border:1px solid rgba(0,255,136,0.2);border-radius:10px;">' +
                            '<div class="d-flex align-items-center gap-3">' +
                                '<div style="width:36px;height:36px;border-radius:50%;background:rgba(0,255,136,0.15);display:flex;align-items:center;justify-content:center;flex-shrink:0;">' +
                                    '<i class="fa-solid fa-certificate" style="color:#00ff88;"></i>' +
                                '</div>' +
                                '<div style="flex:1;">' +
                                    '<div style="font-weight:700;color:#00ff88;font-size:0.9rem;">A2P 10DLC Registered</div>' +
                                    '<div style="font-size:0.75rem;color:#aaa;">' +
                                        'Brand: <span style="color:#ccc;font-family:\'JetBrains Mono\',monospace;font-size:0.7rem;">' + _esc(d.brand_sid) + '</span>' +
                                        ' &bull; Campaign: <span style="color:' + statusColor + ';font-weight:600;">' + _esc(statusLabel) + '</span>' +
                                        (d.registered_at ? ' &bull; ' + new Date(d.registered_at).toLocaleDateString() : '') +
                                    '</div>' +
                                '</div>' +
                                '<button onclick="a2pRefreshStatus()" style="background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);color:#aaa;border-radius:6px;padding:5px 12px;font-size:0.72rem;cursor:pointer;white-space:nowrap;">' +
                                    '<i class="fa-solid fa-arrows-rotate me-1"></i>Refresh' +
                                '</button>' +
                            '</div>' +
                        '</div>';
                }
                if (registerPanel) registerPanel.style.display = 'none';
                if (statusPanel) statusPanel.style.display = 'none';
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
                        '<span style="color:#555;font-family:\'JetBrains Mono\',monospace;font-size:0.68rem;">' + _esc(d.brand_sid || '') + '</span>' +
                    '</div>' +
                '</div>' +
                (d.campaign_sid ?
                    '<div style="display:flex;align-items:center;gap:6px;padding:4px 0;">' +
                        '<span style="font-weight:700;color:#ccc;">Campaign:</span>' +
                        '<span style="color:#00d9ff;font-weight:600;">' + _esc(d.campaign_status || 'PENDING') + '</span>' +
                        '<span style="color:#555;font-family:\'JetBrains Mono\',monospace;font-size:0.68rem;">' + _esc(d.campaign_sid) + '</span>' +
                    '</div>' : ''
                ) +
                (s !== 'APPROVED' && s !== 'FAILED' ?
                    '<div style="margin-top:6px;font-size:0.72rem;color:#888;"><i class="fa-solid fa-clock me-1" style="color:#fbbf24;"></i>Brand vetting typically takes 1-7 business days. Click Refresh to check.</div>' : ''
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
                    (n.nickname ? '<span style="color:#555;font-size:0.7rem;">(' + _esc(n.nickname) + ')</span>' : '') +
                    '<span style="margin-left:auto;font-size:0.65rem;color:#00d9ff;">' +
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
                // Refresh brand status if exists
                if (_a2pStatus && _a2pStatus.brand_sid) {
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
                    window.location.href = d.checkout_url;
                } else {
                    if (result) result.innerHTML = '<span style="color:#ef4444;">' + _esc(d.error || 'Failed to create checkout') + '</span>';
                }
            } catch(e) {
                if (result) result.innerHTML = '<span style="color:#ef4444;">Network error</span>';
            }
            if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-lock me-2"></i><span id="a2pPayBtnLabel">Pay Registration Fee — $' + total.toFixed(2) + '</span>'; }
        }

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
