/**
 * domain.js — Agent Web Presence management
 *
 * Domain search, purchase, provisioning status, and landing page editing.
 */

(function () {
    'use strict';

    let _selectedDomain = '';
    let _selectedDba = '';

    // ── Init: check if agent already has a domain ──
    function _showSetup() {
        var loading = document.getElementById('domainLoading');
        var setup = document.getElementById('domainSetup');
        if (loading) loading.style.display = 'none';
        if (setup) setup.style.display = 'block';
        // Pre-fill domain from onboarding handoff (?onb_domain=...)
        try {
            var params = new URLSearchParams(window.location.search);
            var onbDomain = params.get('onb_domain');
            if (onbDomain) {
                var searchInput = document.getElementById('domainDbaInput');
                if (searchInput) searchInput.value = onbDomain.replace(/\.com$/i, '');
                if (typeof domainSearch === 'function') domainSearch();
            }
        } catch (e) { /* URLSearchParams not supported — ignore */ }
    }

    window.domainTabInit = function () {
        fetch('/api/domain/status')
            .then(function (r) {
                if (!r.ok) throw new Error('HTTP ' + r.status);
                return r.json();
            })
            .then(function (d) {
                var loading = document.getElementById('domainLoading');
                if (loading) loading.style.display = 'none';
                if (d.has_domain && d.status === 'active') {
                    _showActiveDomain(d);
                } else if (d.has_domain && d.status === 'provisioning') {
                    var prov = document.getElementById('domainProvisioning');
                    if (prov) prov.style.display = 'block';
                } else if (d.has_domain && d.status === 'error') {
                    _showActiveDomain(d);
                } else {
                    _showSetup();
                }
            })
            .catch(function () {
                _showSetup();
            });
    };

    function _showActiveDomain(d) {
        const el = document.getElementById('domainActive');
        el.style.display = 'block';

        document.getElementById('domainActiveName').textContent = d.domain;
        document.getElementById('domainActiveUrl').href = d.website;
        document.getElementById('domainActiveUrl').textContent = d.website;
        document.getElementById('domainPrivacyUrl').href = d.website + '/privacy';
        document.getElementById('domainTermsUrl').href = d.website + '/terms';
        document.getElementById('domainActiveEmail').textContent = d.email || '';
        document.getElementById('domainActiveDba').textContent = d.dba_name || '';

        const statusEl = document.getElementById('domainActiveStatus');
        if (d.status === 'active') {
            statusEl.innerHTML = '<span class="domain-badge-live">Live</span>';
        } else if (d.status === 'error') {
            statusEl.innerHTML = '<span class="domain-badge-error">Error</span>';
        } else {
            statusEl.textContent = d.status;
        }

        const dateEl = document.getElementById('domainActiveDate');
        if (d.provisioned_at) {
            dateEl.textContent = new Date(d.provisioned_at).toLocaleDateString();
        }
    }

    // ── Domain Search ──
    window.domainSearch = function () {
        const dba = document.getElementById('domainDbaInput').value.trim();
        if (!dba) {
            if (typeof _showDashToast === 'function') _showDashToast(false, 'Enter your business name to search');
            return;
        }

        const btn = document.getElementById('domainSearchBtn');
        btn.disabled = true;
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Searching...';

        const resultsEl = document.getElementById('domainResults');
        const listEl = document.getElementById('domainResultsList');
        resultsEl.style.display = 'none';
        listEl.innerHTML = '';

        _selectedDba = dba;

        fetch('/api/domain/search', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                dba_name: dba,
                first_name: (window.DASHBOARD_BOOT?.operatorName || '').split(' ')[0] || '',
                last_name: (window.DASHBOARD_BOOT?.operatorName || '').split(' ').slice(1).join(' ') || '',
            }),
        })
            .then(r => r.json())
            .then(d => {
                btn.disabled = false;
                btn.innerHTML = 'Search Available Domains';

                if (d.error) {
                    if (typeof _showDashToast === 'function') _showDashToast(false, d.error);
                    return;
                }

                const suggestions = d.suggestions || [];
                if (!suggestions.length) {
                    listEl.innerHTML = '<p class="domain-no-results">No domains found. Try a different name.</p>';
                    resultsEl.style.display = 'block';
                    return;
                }

                suggestions.forEach(s => {
                    const row = document.createElement('div');
                    row.className = 'domain-result-row' + (s.available ? '' : ' domain-result-taken');

                    if (s.available) {
                        row.innerHTML = `
                            <div class="domain-result-name">
                                <i class="fa-solid fa-circle-check domain-avail-icon"></i>
                                <span>${_esc(s.domain)}</span>
                            </div>
                            <div class="domain-result-right">
                                <span class="domain-result-price">$10/mo</span>
                                <button class="domain-select-btn" onclick="domainSelect('${_esc(s.domain)}')">Select</button>
                            </div>
                        `;
                    } else {
                        row.innerHTML = `
                            <div class="domain-result-name">
                                <i class="fa-solid fa-circle-xmark domain-taken-icon"></i>
                                <span>${_esc(s.domain)}</span>
                            </div>
                            <div class="domain-result-right">
                                <span class="domain-result-taken-label">Taken</span>
                            </div>
                        `;
                    }
                    listEl.appendChild(row);
                });

                resultsEl.style.display = 'block';
            })
            .catch(err => {
                btn.disabled = false;
                btn.innerHTML = 'Search Available Domains';
                if (typeof _showDashToast === 'function') _showDashToast(false, 'Search failed. Try again.');
            });
    };

    // ── Select Domain ──
    window.domainSelect = function (domain) {
        _selectedDomain = domain;

        // Highlight selected
        document.querySelectorAll('.domain-result-row').forEach(r => r.classList.remove('domain-result-selected'));
        document.querySelectorAll('.domain-select-btn').forEach(b => {
            if (b.onclick.toString().includes(domain)) {
                b.closest('.domain-result-row').classList.add('domain-result-selected');
                b.textContent = 'Selected';
                b.classList.add('domain-select-btn-active');
            } else {
                b.textContent = 'Select';
                b.classList.remove('domain-select-btn-active');
            }
        });

        // Show details form
        document.getElementById('domainDetailsForm').style.display = 'block';
        document.getElementById('domainEmailDomain').textContent = domain;

        // Pre-fill agent name if available
        const opName = window.DASHBOARD_BOOT?.operatorName || '';
        if (opName) document.getElementById('domainAgentName').value = opName;

        // Scroll to form
        document.getElementById('domainDetailsForm').scrollIntoView({ behavior: 'smooth', block: 'start' });
    };

    // ── Promo Code ──
    window.domainApplyPromo = function () {
        const input = document.getElementById('domainPromoCode');
        const status = document.getElementById('domainPromoStatus');
        const btn = document.getElementById('domainPromoApplyBtn');
        const code = (input ? input.value : '').trim();
        if (!code) {
            if (status) { status.textContent = 'Enter a code'; status.className = 'domain-promo-status error'; }
            return;
        }
        if (btn) { btn.disabled = true; btn.textContent = '...'; }
        fetch('/api/domain/validate-promo', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ code: code }),
        })
            .then(r => r.json())
            .then(d => {
                if (btn) { btn.disabled = false; btn.textContent = 'Apply'; }
                if (d.valid) {
                    if (status) {
                        status.innerHTML = '<i class="fa-solid fa-circle-check"></i> ' + (d.discount || 'Discount applied');
                        status.className = 'domain-promo-status success';
                    }
                    // Update purchase button to reflect discount
                    var purchaseBtn = document.getElementById('domainPurchaseBtn');
                    if (purchaseBtn) {
                        var priceText = (d.price_after || '$0') + ' first month, then $10/mo';
                        purchaseBtn.innerHTML = '<i class="fa-solid fa-rocket"></i> Get My Domain — ' + priceText;
                    }
                } else {
                    if (status) {
                        status.textContent = d.error || 'Invalid code';
                        status.className = 'domain-promo-status error';
                    }
                }
            })
            .catch(() => {
                if (btn) { btn.disabled = false; btn.textContent = 'Apply'; }
                if (status) { status.textContent = 'Could not validate'; status.className = 'domain-promo-status error'; }
            });
    };

    // ── Purchase Domain ──
    window.domainPurchase = function () {
        if (!_selectedDomain) {
            if (typeof _showDashToast === 'function') _showDashToast(false, 'Select a domain first');
            return;
        }

        const agentName = document.getElementById('domainAgentName').value.trim();
        const phone = document.getElementById('domainPhone').value.trim();
        const emailPrefix = document.getElementById('domainEmailPrefix').value.trim();
        const forwardTo = document.getElementById('domainForwardTo').value.trim();
        const states = document.getElementById('domainStates').value.trim();
        const bio = document.getElementById('domainBio').value.trim();
        const street = document.getElementById('domainStreet').value.trim();
        const city = document.getElementById('domainCity').value.trim();
        const stateReg = document.getElementById('domainStateReg').value.trim().toUpperCase();
        const zip = document.getElementById('domainZip').value.trim();
        const disclaimer = document.getElementById('domainDisclaimer').checked;

        if (!agentName) { if (typeof _showDashToast === 'function') _showDashToast(false, 'Your name is required'); return; }
        if (!phone) { if (typeof _showDashToast === 'function') _showDashToast(false, 'Phone number is required'); return; }
        if (!emailPrefix) { if (typeof _showDashToast === 'function') _showDashToast(false, 'Email prefix is required'); return; }
        if (!forwardTo) { if (typeof _showDashToast === 'function') _showDashToast(false, 'Forwarding email is required'); return; }
        if (!street) { if (typeof _showDashToast === 'function') _showDashToast(false, 'Street address is required (ICANN)'); return; }
        if (!city) { if (typeof _showDashToast === 'function') _showDashToast(false, 'City is required (ICANN)'); return; }
        if (!stateReg) { if (typeof _showDashToast === 'function') _showDashToast(false, 'State is required (ICANN)'); return; }
        if (!zip) { if (typeof _showDashToast === 'function') _showDashToast(false, 'ZIP code is required (ICANN)'); return; }
        if (!disclaimer) { if (typeof _showDashToast === 'function') _showDashToast(false, 'You must accept the disclaimer'); return; }

        const btn = document.getElementById('domainPurchaseBtn');
        btn.disabled = true;
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Provisioning...';

        // Show provisioning view
        document.getElementById('domainSetup').style.display = 'none';
        document.getElementById('domainProvisioning').style.display = 'block';
        _showProvisioningSteps();

        const licensedStates = states ? states.split(',').map(s => s.trim().toUpperCase()).filter(s => s.length === 2) : [];

        const promoCode = (document.getElementById('domainPromoCode') || {}).value || '';

        fetch('/api/domain/checkout', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                domain: _selectedDomain,
                dba_name: _selectedDba,
                agent_name: agentName,
                phone_display: phone,
                email_prefix: emailPrefix,
                forward_to: forwardTo,
                licensed_states: licensedStates,
                bio: bio,
                street: street,
                city: city,
                state: stateReg,
                zip: zip,
                disclaimer_accepted: true,
                promo_code: promoCode.trim(),
            }),
        })
            .then(r => r.json())
            .then(d => {
                if (d.status === 'active') {
                    _updateProvisioningSteps(d.provisioning_log || [], true);
                    setTimeout(() => {
                        document.getElementById('domainProvisioning').style.display = 'none';
                        _showActiveDomain({
                            domain: d.domain,
                            email: d.email,
                            website: d.website,
                            dba_name: _selectedDba,
                            status: 'active',
                            provisioned_at: new Date().toISOString(),
                            has_domain: true,
                        });
                        if (typeof _showDashToast === 'function') _showDashToast(true, 'Your domain is live!');
                    }, 2000);
                } else {
                    _updateProvisioningSteps(d.provisioning_log || [], false, d.error);
                    btn.disabled = false;
                    btn.innerHTML = '<i class="fa-solid fa-rocket"></i> Get My Domain — $10/month';
                    if (typeof _showDashToast === 'function') _showDashToast(false, d.error || 'Provisioning failed');
                }
            })
            .catch(err => {
                _updateProvisioningSteps([], false, 'Network error');
                btn.disabled = false;
                btn.innerHTML = '<i class="fa-solid fa-rocket"></i> Get My Domain — $10/month';
                document.getElementById('domainProvisioning').style.display = 'none';
                document.getElementById('domainSetup').style.display = 'block';
                if (typeof _showDashToast === 'function') _showDashToast(false, 'Network error. Please try again.');
            });
    };

    // ── Provisioning Steps UI ──
    const PROV_STEPS = [
        { key: 'domain_registered', label: 'Registering domain', icon: 'fa-globe' },
        { key: 'zone_created', label: 'Creating DNS zone', icon: 'fa-server' },
        { key: 'nameservers_updated', label: 'Configuring nameservers', icon: 'fa-network-wired' },
        { key: 'mailgun_domain_added', label: 'Setting up email sending', icon: 'fa-envelope' },
        { key: 'dns_configured', label: 'Configuring DNS records', icon: 'fa-list-check' },
        { key: 'email_routing_configured', label: 'Enabling email forwarding', icon: 'fa-paper-plane' },
        { key: 'worker_route_created', label: 'Publishing landing page', icon: 'fa-file-lines' },
        { key: 'kv_config_stored', label: 'Finalizing setup', icon: 'fa-check-double' },
    ];

    function _showProvisioningSteps() {
        const el = document.getElementById('domainProvSteps');
        el.innerHTML = PROV_STEPS.map(s => `
            <div class="domain-prov-step" id="provStep_${s.key}">
                <div class="domain-prov-icon domain-prov-pending">
                    <i class="fa-solid ${s.icon}"></i>
                </div>
                <span class="domain-prov-label">${s.label}</span>
                <span class="domain-prov-status domain-prov-waiting">Waiting</span>
            </div>
        `).join('');
    }

    function _updateProvisioningSteps(completedKeys, allDone, errorMsg) {
        const completedSet = new Set(completedKeys);
        let hitFirst = false;

        PROV_STEPS.forEach(s => {
            const el = document.getElementById('provStep_' + s.key);
            if (!el) return;
            const iconEl = el.querySelector('.domain-prov-icon');
            const statusEl = el.querySelector('.domain-prov-status');

            if (completedSet.has(s.key)) {
                iconEl.className = 'domain-prov-icon domain-prov-done';
                statusEl.className = 'domain-prov-status domain-prov-complete';
                statusEl.textContent = 'Done';
            } else if (!hitFirst && !allDone) {
                hitFirst = true;
                if (errorMsg) {
                    iconEl.className = 'domain-prov-icon domain-prov-error';
                    statusEl.className = 'domain-prov-status domain-prov-failed';
                    statusEl.textContent = 'Failed';
                } else {
                    iconEl.className = 'domain-prov-icon domain-prov-active';
                    statusEl.className = 'domain-prov-status domain-prov-running';
                    statusEl.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
                }
            }
        });

        if (errorMsg) {
            const stepsEl = document.getElementById('domainProvSteps');
            stepsEl.insertAdjacentHTML('afterend', `
                <div class="domain-prov-error-msg">
                    <i class="fa-solid fa-triangle-exclamation"></i>
                    ${_esc(errorMsg)}
                </div>
            `);
        }
    }

    // ── Save Landing Page Edits ──
    window.domainSavePage = function () {
        const name = document.getElementById('domainEditName').value.trim();
        const phone = document.getElementById('domainEditPhone').value.trim();
        const statesRaw = document.getElementById('domainEditStates').value.trim();
        const bio = document.getElementById('domainEditBio').value.trim();

        const states = statesRaw ? statesRaw.split(',').map(s => s.trim().toUpperCase()).filter(s => s.length === 2) : undefined;

        const payload = {};
        if (name) payload.agent_name = name;
        if (phone) payload.phone_display = phone;
        if (states) payload.licensed_states = states;
        if (bio !== undefined) payload.bio = bio;

        fetch('/api/domain/update-page', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        })
            .then(r => r.json())
            .then(d => {
                if (d.status === 'ok') {
                    if (typeof _showDashToast === 'function') _showDashToast(true, 'Landing page updated');
                } else {
                    if (typeof _showDashToast === 'function') _showDashToast(false, d.error || 'Update failed');
                }
            })
            .catch(() => {
                if (typeof _showDashToast === 'function') _showDashToast(false, 'Network error');
            });
    };

    // ── Utility ──
    function _esc(str) {
        if (!str) return '';
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }
})();
