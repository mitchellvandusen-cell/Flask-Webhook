        // ===== AI MINUTES MARKETPLACE =====
        let _aimHistoryData = { purchases: [], usage: [] };
        let _aimCurrentView = 'purchases';

        async function loadAiMinutes() {
            // Load balance
            try {
                const bRes = await fetch('/ai-minutes/balance');
                const bal = await bRes.json();
                document.getElementById('aimBalanceValue').textContent = (bal.balance_minutes || 0).toLocaleString() + ' min';
                document.getElementById('aimTotalPurchased').textContent = (bal.total_purchased || 0).toLocaleString() + ' min';
                document.getElementById('aimTotalUsed').textContent = (bal.total_used || 0).toLocaleString() + ' min';
            } catch (e) {
                document.getElementById('aimBalanceValue').textContent = '0 min';
            }

            // Load packages
            try {
                const pRes = await fetch('/ai-minutes/packages');
                const pData = await pRes.json();
                const grid = document.getElementById('aimPackages');
                if (!pData.packages || pData.packages.length === 0) {
                    grid.innerHTML = '<div class="col-12 text-center py-3" style="color:#888;">No packages available. Contact support.</div>';
                } else {
                    grid.innerHTML = pData.packages.map(pkg => {
                        const priceStr = pkg.price_cents ? '$' + (pkg.price_cents / 100).toFixed(2) : 'Contact Sales';
                        const perMin = pkg.price_cents ? '$' + (pkg.price_cents / 100 / pkg.minutes).toFixed(3) + '/min' : '';
                        const colors = {
                            500:   { bg: 'rgba(0,255,136,0.06)', border: 'rgba(0,255,136,0.2)', accent: '#00ff88' },
                            2000:  { bg: 'rgba(0,153,255,0.06)', border: 'rgba(0,153,255,0.2)', accent: '#0099ff' },
                            5000:  { bg: 'rgba(168,85,247,0.06)', border: 'rgba(168,85,247,0.2)', accent: '#a855f7' },
                            10000: { bg: 'rgba(255,170,0,0.06)',  border: 'rgba(255,170,0,0.2)',  accent: '#ffaa00' },
                        };
                        const c = colors[pkg.minutes] || colors[500];
                        return `
                            <div class="col-6 col-md-3">
                                <div style="background:${c.bg}; border:1px solid ${c.border}; border-radius:16px; padding:24px 16px; text-align:center; height:100%; display:flex; flex-direction:column; justify-content:space-between;">
                                    <div>
                                        <div style="font-size:2rem; font-weight:900; color:${c.accent}; line-height:1;">${pkg.minutes.toLocaleString()}</div>
                                        <div style="font-size:0.8rem; color:#aaa; margin:4px 0 2px;">minutes</div>
                                        <div style="font-size:0.75rem; font-weight:600; color:#666; text-transform:uppercase; letter-spacing:1px; margin-bottom:12px;">${pkg.label}</div>
                                    </div>
                                    <div>
                                        <div style="font-size:1.3rem; font-weight:700; color:#fff; margin-bottom:4px;">${priceStr}</div>
                                        ${perMin ? `<div style="font-size:0.7rem; color:#888; margin-bottom:12px;">${perMin}</div>` : '<div style="margin-bottom:12px;"></div>'}
                                        <button class="btn btn-sm w-100" style="background:${c.accent}; color:#000; font-weight:700; border-radius:10px; padding:8px 0;"
                                            onclick="buyAiMinutes(${pkg.minutes})" ${!pkg.available ? 'disabled' : ''}>
                                            Buy Now
                                        </button>
                                    </div>
                                </div>
                            </div>`;
                    }).join('');
                }
            } catch (e) {
                document.getElementById('aimPackages').innerHTML = '<div class="col-12 text-center py-3" style="color:#ff6b6b;">Failed to load packages.</div>';
            }

            // Load usage history
            loadAimHistory();
        }

        async function buyAiMinutes(minutes) {
            const btn = event.target;
            const origText = btn.textContent;
            btn.disabled = true;
            btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
            try {
                const res = await fetch('/ai-minutes/checkout', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ minutes })
                });
                const data = await res.json();
                if (data.checkout_url) {
                    window.location.href = data.checkout_url;
                } else {
                    alert(data.error || 'Failed to create checkout session');
                    btn.disabled = false;
                    btn.textContent = origText;
                }
            } catch (e) {
                alert('Network error. Please try again.');
                btn.disabled = false;
                btn.textContent = origText;
            }
        }

        async function loadAimHistory() {
            try {
                const res = await fetch('/ai-minutes/usage');
                _aimHistoryData = await res.json();
                renderAimHistory();
            } catch (e) {
                document.getElementById('aimHistoryContent').innerHTML = '<div class="text-center py-4" style="color:#ff6b6b;">Failed to load history.</div>';
            }
        }

        function switchAimHistory(view) {
            _aimCurrentView = view;
            document.getElementById('aimHistPurchases').classList.toggle('active', view === 'purchases');
            document.getElementById('aimHistUsage').classList.toggle('active', view === 'usage');
            renderAimHistory();
        }

        function renderAimHistory() {
            const el = document.getElementById('aimHistoryContent');
            if (_aimCurrentView === 'purchases') {
                const items = _aimHistoryData.purchases || [];
                if (items.length === 0) {
                    el.innerHTML = '<div class="text-center py-4" style="color:#888;">No purchases yet. Buy a package above to get started!</div>';
                    return;
                }
                el.innerHTML = `
                    <div style="display:grid; grid-template-columns:1fr 1fr 1fr 1fr; gap:0; font-size:0.75rem; color:#888; text-transform:uppercase; letter-spacing:0.5px; padding:8px 12px; border-bottom:1px solid rgba(255,255,255,0.05);">
                        <div>Package</div><div>Minutes</div><div>Amount</div><div>Date</div>
                    </div>
                    ${items.map(p => {
                        const amt = p.amount_cents ? '$' + (p.amount_cents / 100).toFixed(2) : '—';
                        const dt = p.completed_at ? new Date(p.completed_at).toLocaleDateString() : '—';
                        return `<div style="display:grid; grid-template-columns:1fr 1fr 1fr 1fr; gap:0; padding:10px 12px; border-bottom:1px solid rgba(255,255,255,0.03); font-size:0.85rem;">
                            <div style="color:var(--accent); font-weight:600;">${p.package_label || '—'}</div>
                            <div style="color:#fff;">${(p.package_minutes || 0).toLocaleString()}</div>
                            <div style="color:#aaa;">${amt}</div>
                            <div style="color:#888;">${dt}</div>
                        </div>`;
                    }).join('')}`;
            } else {
                const items = _aimHistoryData.usage || [];
                if (items.length === 0) {
                    el.innerHTML = '<div class="text-center py-4" style="color:#888;">No call usage recorded yet.</div>';
                    return;
                }
                el.innerHTML = `
                    <div style="display:grid; grid-template-columns:1fr 1fr 1fr 1fr 1fr; gap:0; font-size:0.75rem; color:#888; text-transform:uppercase; letter-spacing:0.5px; padding:8px 12px; border-bottom:1px solid rgba(255,255,255,0.05);">
                        <div>Phone</div><div>Direction</div><div>Duration</div><div>Minutes</div><div>Date</div>
                    </div>
                    ${items.map(u => {
                        const durStr = u.duration_seconds >= 60 ? Math.floor(u.duration_seconds/60) + 'm ' + (u.duration_seconds%60) + 's' : u.duration_seconds + 's';
                        const dt = u.created_at ? new Date(u.created_at).toLocaleDateString() : '—';
                        const dirIcon = u.direction === 'inbound' ? '<i class="fa-solid fa-phone-arrow-down-left" style="color:#0099ff;"></i>' : '<i class="fa-solid fa-phone-arrow-up-right" style="color:var(--accent);"></i>';
                        return `<div style="display:grid; grid-template-columns:1fr 1fr 1fr 1fr 1fr; gap:0; padding:10px 12px; border-bottom:1px solid rgba(255,255,255,0.03); font-size:0.85rem;">
                            <div style="color:#fff; font-family:monospace;">${u.phone || '—'}</div>
                            <div>${u.direction || '—'}</div>
                            <div style="color:#aaa;">${durStr}</div>
                            <div style="color:#ff6b6b; font-weight:600;">-${u.minutes_deducted}</div>
                            <div style="color:#888;">${dt}</div>
                        </div>`;
                    }).join('')}`;
            }
        }

        // Auto-load AI minutes if user arrived from a successful purchase
        if (window.location.search.includes('ai_minutes_success=1')) {
            setTimeout(() => {
                document.querySelector('[data-bs-target="#aiminutes"]')?.click();
            }, 500);
        }

