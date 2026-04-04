        // ===== AI MINUTES MARKETPLACE =====
        let _aimHistoryData = { purchases: [], usage: [] };
        let _aimCurrentView = 'purchases';

        // Arc circumference: 2 * π * 50 ≈ 314.16
        const AIM_ARC_CIRC = 314.16;

        // Per-package metadata (minutes → config)
        const AIM_TIER_META = {
            500:   { cls: 'aim-pkg--green',  tier: 'Starter',  desc: 'Perfect for getting started with AI voice.' },
            2000:  { cls: 'aim-pkg--blue',   tier: 'Standard', desc: 'Ideal for active agents making daily calls.' },
            5000:  { cls: 'aim-pkg--purple', tier: 'Power',    desc: 'Built for high-volume dialers.', popular: true },
            10000: { cls: 'aim-pkg--gold',   tier: 'Elite',    desc: 'Maximum capacity, best value per minute.' },
        };

        async function loadAiMinutes() {
            // Load balance + arc
            try {
                const bRes = await fetch('/ai-minutes/balance');
                const bal  = await bRes.json();
                const balance   = bal.balance_minutes  || 0;
                const purchased = bal.total_purchased  || 0;
                const used      = bal.total_used       || 0;

                document.getElementById('aimBalanceValue').textContent   = balance.toLocaleString() + ' min';
                document.getElementById('aimTotalPurchased').textContent = purchased.toLocaleString() + ' min';
                document.getElementById('aimTotalUsed').textContent      = used.toLocaleString() + ' min';

                // Status pill
                const pill = document.getElementById('aimStatusPill');
                if (pill) pill.textContent = balance.toLocaleString() + ' min remaining';

                // Runway
                const hrs = (balance / 60).toFixed(1);
                document.getElementById('aimRunway').textContent = `≈ ${hrs} hours of AI voice`;

                // Arc ring
                const arcFill = document.getElementById('aimArcFill');
                const arcPct  = document.getElementById('aimArcPct');
                if (arcFill && purchased > 0) {
                    const pct     = Math.min(100, Math.round(balance / purchased * 100));
                    const offset  = AIM_ARC_CIRC * (1 - pct / 100);
                    arcFill.style.strokeDashoffset = offset;
                    arcPct.textContent = pct + '%';
                    arcFill.classList.remove('aim-arc--mid', 'aim-arc--low');
                    if (pct <= 15) arcFill.classList.add('aim-arc--low');
                    else if (pct <= 35) arcFill.classList.add('aim-arc--mid');
                } else if (arcPct) {
                    arcPct.textContent = '—';
                }
            } catch (e) {
                document.getElementById('aimBalanceValue').textContent = '0 min';
                document.getElementById('aimRunway').textContent = '—';
            }

            // Load packages
            try {
                const pRes  = await fetch('/ai-minutes/packages');
                const pData = await pRes.json();
                const grid  = document.getElementById('aimPackages');

                if (!pData.packages || pData.packages.length === 0) {
                    grid.innerHTML = '<div class="aim-loading-state">No packages available. Contact support.</div>';
                    return;
                }

                // Calculate base rate (cheapest package) for savings %
                const available = pData.packages.filter(p => p.price_cents && p.available !== false);
                const baseRate  = available.length ? (available[0].price_cents / available[0].minutes) : null;

                grid.innerHTML = available.map(pkg => {
                    const meta     = AIM_TIER_META[pkg.minutes] || AIM_TIER_META[500];
                    const priceStr = '$' + (pkg.price_cents / 100).toFixed(2);
                    const perMin   = '$' + (pkg.price_cents / 100 / pkg.minutes).toFixed(3) + ' / min';
                    const savings  = baseRate && pkg.minutes > available[0].minutes
                        ? Math.round((1 - (pkg.price_cents / pkg.minutes) / baseRate) * 100)
                        : 0;

                    const popularBadge = meta.popular
                        ? '<span class="aim-pkg-stamp aim-pkg-stamp--popular">Most Popular</span>'
                        : '';
                    const savingsBadge = !meta.popular && savings > 0
                        ? `<span class="aim-pkg-stamp aim-pkg-stamp--savings">Save ${savings}%</span>`
                        : '';

                    return `
                        <div class="aim-pkg-card ${meta.cls}${meta.popular ? ' aim-pkg-popular' : ''}">
                            ${popularBadge}${savingsBadge}
                            <div class="aim-pkg-tier">${meta.tier}</div>
                            <div class="aim-pkg-minutes">${pkg.minutes.toLocaleString()}</div>
                            <div class="aim-pkg-unit">minutes</div>
                            <div class="aim-pkg-desc">${meta.desc}</div>
                            <div class="aim-pkg-price-row">
                                <div class="aim-pkg-price">${priceStr}</div>
                                <div class="aim-pkg-per">${perMin}</div>
                                <button class="aim-pkg-btn" onclick="buyAiMinutes(${pkg.minutes})">
                                    Buy Now
                                </button>
                            </div>
                        </div>`;
                }).join('');
            } catch (e) {
                document.getElementById('aimPackages').innerHTML =
                    '<div class="aim-loading-state aim-loading-error">Failed to load packages.</div>';
            }

            loadAimHistory();
        }

        async function buyAiMinutes(minutes) {
            const btn      = event.currentTarget;
            const origHTML = btn.innerHTML;
            btn.disabled   = true;
            btn.innerHTML  = '<i class="fa-solid fa-spinner fa-spin"></i>';
            try {
                const res  = await fetch('/ai-minutes/checkout', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ minutes }),
                });
                const data = await res.json();
                if (data.checkout_url) {
                    window.top.location.href = data.checkout_url;
                } else {
                    alert(data.error || 'Failed to create checkout session.');
                    btn.disabled  = false;
                    btn.innerHTML = origHTML;
                }
            } catch (e) {
                alert('Network error. Please try again.');
                btn.disabled  = false;
                btn.innerHTML = origHTML;
            }
        }

        async function loadAimHistory() {
            try {
                const res        = await fetch('/ai-minutes/usage');
                _aimHistoryData  = await res.json();
                renderAimHistory();
            } catch (e) {
                document.getElementById('aimHistoryContent').innerHTML =
                    '<div class="aim-history-empty">Failed to load history.</div>';
            }
        }

        function switchAimHistory(view) {
            _aimCurrentView = view;
            document.getElementById('aimHistPurchases').classList.toggle('aim-tab-active', view === 'purchases');
            document.getElementById('aimHistUsage').classList.toggle('aim-tab-active', view === 'usage');
            renderAimHistory();
        }

        function renderAimHistory() {
            const el = document.getElementById('aimHistoryContent');

            if (_aimCurrentView === 'purchases') {
                const items = _aimHistoryData.purchases || [];
                if (items.length === 0) {
                    el.innerHTML = '<div class="aim-history-empty">No purchases yet &mdash; buy a package above to get started.</div>';
                    return;
                }
                el.innerHTML = `
                    <div class="aim-history--purchases">
                        <div class="aim-history-header">
                            <div class="aim-history-col-label">Package</div>
                            <div class="aim-history-col-label">Minutes</div>
                            <div class="aim-history-col-label">Amount</div>
                            <div class="aim-history-col-label">Date</div>
                        </div>
                        ${items.map(p => {
                            const amt = p.amount_cents ? '$' + (p.amount_cents / 100).toFixed(2) : '—';
                            const dt  = p.completed_at ? new Date(p.completed_at).toLocaleDateString() : '—';
                            return `<div class="aim-history-row">
                                <div class="aim-cell-pkg">${p.package_label || '—'}</div>
                                <div class="aim-cell-minutes">${(p.package_minutes || 0).toLocaleString()}</div>
                                <div class="aim-cell-amount">${amt}</div>
                                <div class="aim-cell-date">${dt}</div>
                            </div>`;
                        }).join('')}
                    </div>`;
            } else {
                const items = _aimHistoryData.usage || [];
                if (items.length === 0) {
                    el.innerHTML = '<div class="aim-history-empty">No call usage recorded yet.</div>';
                    return;
                }
                el.innerHTML = `
                    <div class="aim-history--usage">
                        <div class="aim-history-header">
                            <div class="aim-history-col-label">Phone</div>
                            <div class="aim-history-col-label">Direction</div>
                            <div class="aim-history-col-label">Duration</div>
                            <div class="aim-history-col-label">Used</div>
                            <div class="aim-history-col-label">Date</div>
                        </div>
                        ${items.map(u => {
                            const durStr  = u.duration_seconds >= 60
                                ? Math.floor(u.duration_seconds / 60) + 'm ' + (u.duration_seconds % 60) + 's'
                                : u.duration_seconds + 's';
                            const dt      = u.created_at ? new Date(u.created_at).toLocaleDateString() : '—';
                            const dirIcon = u.direction === 'inbound'
                                ? '<i class="fa-solid fa-phone-arrow-down-left aim-dir-icon-in"></i>'
                                : '<i class="fa-solid fa-phone-arrow-up-right aim-dir-icon-out"></i>';
                            return `<div class="aim-history-row">
                                <div class="aim-cell-phone">${u.phone || '—'}</div>
                                <div class="aim-cell-dur">${dirIcon} ${u.direction || '—'}</div>
                                <div class="aim-cell-dur">${durStr}</div>
                                <div class="aim-cell-debit">&minus;${u.minutes_deducted}</div>
                                <div class="aim-cell-date">${dt}</div>
                            </div>`;
                        }).join('')}
                    </div>`;
            }
        }

        // Auto-switch to AI Minutes tab after a successful purchase
        if (window.location.search.includes('ai_minutes_success=1')) {
            setTimeout(() => {
                document.querySelector('[data-bs-target="#aiminutes"]')?.click();
            }, 500);
        }
