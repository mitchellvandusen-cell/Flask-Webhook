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
        // Clean up ping interval on page unload to avoid timer leaks
        window.addEventListener('beforeunload', () => { if (typeof dialerStopPing === 'function') dialerStopPing(); });

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
            el.innerHTML = '<div style="text-align:center;padding:20px;color:#555;"><i class="fa-solid fa-spinner fa-spin" style="font-size:1rem;color:#ff6b9d;display:block;margin-bottom:6px;"></i><span style="font-size:.72rem;">Loading number health...</span></div>';
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

            if (!nums.length && !rotationEnabled) {
                el.innerHTML = '';
                return;
            }

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
                html += '<i class="fa-solid fa-shield-halved" style="font-size:1.2rem;display:block;margin-bottom:6px;color:#444;"></i>';
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
                html += '<button onclick="nhSetStrategy(\'' + s.key + '\')" title="' + _esc(s.desc) + '" style="flex:1;padding:6px 8px;border-radius:6px;font-size:.68rem;font-weight:600;cursor:pointer;border:1px solid ' + (active ? 'rgba(0,217,255,0.3)' : 'rgba(255,255,255,0.06)') + ';background:' + (active ? 'rgba(0,217,255,0.08)' : 'rgba(255,255,255,0.02)') + ';color:' + (active ? '#00d9ff' : '#888') + ';">';
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
                html += _nhKpiCard('Connect', (sum.daily_connect_rate || 0) + '%', sum.daily_connect_rate >= 30 ? '#00ff88' : '#ffa500', 'fa-link');

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

            // ── State Coverage Recommendations ──
            var recs = d.recommendations || [];
            if (recs.length) {
                html += '<div style="margin-top:16px;">';
                html += '<h6 style="margin:0 0 10px;font-weight:700;color:#fff;font-size:.82rem;"><i class="fa-solid fa-map-location-dot me-2" style="color:#00d9ff;"></i>State Coverage <span style="color:#666;font-weight:400;font-size:.68rem;">(2 numbers per state recommended)</span></h6>';

                // Critical/High priority — states needing numbers
                var needAction = recs.filter(function(r) { return r.priority === 'critical' || r.priority === 'high'; });
                var covered = recs.filter(function(r) { return r.priority === 'good'; });
                var medium = recs.filter(function(r) { return r.priority === 'medium' || r.priority === 'low'; });

                if (needAction.length) {
                    html += '<div style="margin-bottom:8px;">';
                    needAction.forEach(function(r) {
                        var isCritical = r.priority === 'critical';
                        var bgColor = isCritical ? 'rgba(239,68,68,0.06)' : 'rgba(255,165,0,0.06)';
                        var borderColor = isCritical ? 'rgba(239,68,68,0.15)' : 'rgba(255,165,0,0.15)';
                        var textColor = isCritical ? '#ef4444' : '#ffa500';
                        var icon = isCritical ? 'fa-triangle-exclamation' : 'fa-circle-exclamation';

                        html += '<div style="display:flex;align-items:center;justify-content:space-between;padding:8px 10px;background:' + bgColor + ';border:1px solid ' + borderColor + ';border-radius:6px;margin-bottom:4px;">';
                        html += '<div style="display:flex;align-items:center;gap:8px;">';
                        html += '<i class="fa-solid ' + icon + '" style="color:' + textColor + ';font-size:.7rem;"></i>';
                        html += '<span style="color:#ccc;font-size:.75rem;font-weight:600;">' + _esc(r.state_name) + ' <span style="color:#555;">(' + r.state + ')</span></span>';
                        html += '<span style="color:' + textColor + ';font-size:.65rem;">' + r.owned + '/' + r.recommended + ' numbers</span>';
                        if (r.contacts > 0) html += '<span style="color:#888;font-size:.6rem;">' + r.contacts + ' contacts</span>';
                        html += '</div>';
                        html += '<span style="background:' + bgColor + ';color:' + textColor + ';padding:2px 8px;border-radius:4px;font-size:.6rem;font-weight:700;text-transform:uppercase;">';
                        html += isCritical ? 'Need ' + r.need + ' numbers' : 'Need ' + r.need + ' more';
                        html += '</span>';
                        html += '</div>';
                    });
                    html += '</div>';
                }

                if (medium.length) {
                    html += '<div style="margin-bottom:8px;">';
                    medium.forEach(function(r) {
                        html += '<div style="display:flex;align-items:center;justify-content:space-between;padding:6px 10px;border-bottom:1px solid rgba(255,255,255,0.03);font-size:.7rem;">';
                        html += '<div style="display:flex;align-items:center;gap:6px;">';
                        html += '<span style="color:#aaa;">' + _esc(r.state_name) + '</span>';
                        html += '<span style="color:#555;">(' + r.owned + '/' + r.recommended + ')</span>';
                        if (r.contacts > 0) html += '<span style="color:#666;font-size:.6rem;">' + r.contacts + ' contacts</span>';
                        html += '</div>';
                        html += '<span style="color:#ffa500;font-size:.6rem;">+' + r.need + ' recommended</span>';
                        html += '</div>';
                    });
                    html += '</div>';
                }

                if (covered.length) {
                    html += '<div style="display:flex;flex-wrap:wrap;gap:4px;">';
                    covered.forEach(function(r) {
                        html += '<span style="display:inline-flex;align-items:center;gap:3px;background:rgba(0,255,136,0.04);border:1px solid rgba(0,255,136,0.1);border-radius:4px;padding:2px 7px;font-size:.6rem;color:#4ade80;">';
                        html += '<i class="fa-solid fa-circle-check" style="font-size:.45rem;"></i>' + r.state + ' (' + r.owned + ')';
                        html += '</span>';
                    });
                    html += '</div>';
                }

                html += '</div>';
            }

            el.innerHTML = html;
        }

        function _nhKpiCard(label, value, color, icon) {
            return '<div style="padding:10px;background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.06);border-radius:8px;text-align:center;">' +
                '<div style="font-size:1.1rem;font-weight:800;color:' + color + ';">' +
                '<i class="fa-solid ' + icon + '" style="font-size:.65rem;opacity:.6;margin-right:3px;"></i>' + value +
                '</div>' +
                '<div style="font-size:.62rem;color:#666;text-transform:uppercase;letter-spacing:.5px;margin-top:2px;">' + label + '</div>' +
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
            html += '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;">';
            html += '<div style="display:flex;align-items:center;gap:6px;">';
            html += '<span style="color:#fff;font-weight:600;font-size:.8rem;">' + _esc(_fmtPhone(n.phone)) + '</span>';
            if (n.state) html += '<span style="background:rgba(0,217,255,0.08);color:#00d9ff;padding:1px 5px;border-radius:3px;font-size:.5rem;font-weight:700;letter-spacing:.3px;">' + _esc(n.state) + '</span>';
            if (n.is_primary) html += '<span style="background:rgba(0,217,255,0.15);color:#00d9ff;padding:1px 5px;border-radius:3px;font-size:.55rem;font-weight:700;">PRIMARY</span>';
            if (n.nickname) html += '<span style="color:#666;font-size:.65rem;">(' + _esc(n.nickname) + ')</span>';
            html += '</div>';

            // Health score badge
            html += '<div style="display:flex;align-items:center;gap:6px;">';
            if (n.status === 'frozen' || n.status === 'resting') {
                html += '<span style="background:' + statusBg + ';color:' + statusColor + ';padding:2px 8px;border-radius:4px;font-size:.65rem;font-weight:700;text-transform:uppercase;">';
                html += '<i class="fa-solid ' + statusIcon + ' me-1"></i>' + n.status;
                html += '</span>';
            }
            html += '<div style="width:36px;height:36px;border-radius:50%;background:rgba(0,0,0,0.3);border:2px solid ' + barColor + ';display:flex;align-items:center;justify-content:center;">';
            html += '<span style="font-size:.65rem;font-weight:800;color:' + barColor + ';">' + Math.round(score) + '</span>';
            html += '</div></div></div>';

            // Row 2: Metric pills
            html += '<div style="display:flex;gap:4px;flex-wrap:wrap;margin-bottom:6px;">';
            html += _nhPill('fa-phone', n.daily_calls + '/' + n.daily_cap + ' today', dailyPct >= 80 ? '#ffa500' : '#888');
            html += _nhPill('fa-link', connectRate.toFixed(0) + '% connect', connectRate >= 30 ? '#4ade80' : (connectRate >= 15 ? '#ffa500' : '#ef4444'));
            html += _nhPill('fa-chart-line', totalCalls + ' lifetime', '#888');
            var warmupColors = { 0: '#ef4444', 1: '#ffa500', 2: '#fbbf24', 3: '#00d9ff', 4: '#4ade80' };
            html += _nhPill('fa-seedling', n.warmup_label || 'Stage ' + n.warmup_stage, warmupColors[n.warmup_stage] || '#00d9ff');
            html += '</div>';

            // Row 3: Health bar
            html += '<div style="height:3px;background:rgba(255,255,255,0.06);border-radius:2px;overflow:hidden;">';
            html += '<div style="height:100%;width:' + score + '%;background:' + barColor + ';border-radius:2px;transition:width .5s ease;"></div>';
            html += '</div>';

            // Row 4: Actions
            if (n.status === 'frozen' || n.status === 'resting') {
                html += '<div style="margin-top:6px;text-align:right;">';
                html += '<button onclick="nhSetNumberStatus(\'' + _esc(n.phone) + '\',\'active\')" style="background:rgba(0,255,136,0.08);border:1px solid rgba(0,255,136,0.15);color:#00ff88;border-radius:4px;padding:3px 10px;font-size:.65rem;font-weight:600;cursor:pointer;"><i class="fa-solid fa-play me-1"></i>Reactivate</button>';
                html += '</div>';
            } else if (score < 40 && totalCalls >= 5) {
                html += '<div style="margin-top:6px;display:flex;justify-content:flex-end;gap:6px;">';
                html += '<button onclick="nhSetNumberStatus(\'' + _esc(n.phone) + '\',\'resting\',24)" style="background:rgba(251,191,36,0.08);border:1px solid rgba(251,191,36,0.15);color:#fbbf24;border-radius:4px;padding:3px 10px;font-size:.65rem;font-weight:600;cursor:pointer;"><i class="fa-solid fa-moon me-1"></i>Rest 24h</button>';
                html += '<button onclick="nhSetNumberStatus(\'' + _esc(n.phone) + '\',\'frozen\')" style="background:rgba(139,92,246,0.08);border:1px solid rgba(139,92,246,0.15);color:#8b5cf6;border-radius:4px;padding:3px 10px;font-size:.65rem;font-weight:600;cursor:pointer;"><i class="fa-solid fa-snowflake me-1"></i>Freeze</button>';
                html += '</div>';
            }

            html += '</div>';
            return html;
        }

        function _nhPill(icon, text, color) {
            return '<span style="display:inline-flex;align-items:center;gap:3px;background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.05);border-radius:4px;padding:2px 6px;font-size:.6rem;color:' + (color || '#888') + ';">' +
                '<i class="fa-solid ' + icon + '" style="font-size:.5rem;opacity:.7;"></i>' + text +
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

        // Auto-load number health when numbers tab renders
        if (typeof loadNumbersTab === 'function') {
            var _origLoadNumbersTab = loadNumbersTab;
            loadNumbersTab = async function() {
                await _origLoadNumbersTab();
                loadNumberHealth();
            };
        }


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
                    window.top.location.href = d.checkout_url;
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
