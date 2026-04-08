/**
 * domain.js — Agent Web Presence management
 *
 * Domain search, purchase, provisioning status, and landing page editing.
 */

(function () {
    'use strict';

    // Assign immediately so sidebar can call it even if later code throws
    window.domainTabInit = function () {
        var loading = document.getElementById('domainLoading');
        var setup = document.getElementById('domainSetup');
        if (loading) loading.style.display = 'none';
        if (setup) setup.style.display = 'block';
    };

    let _selectedDomain = '';
    let _selectedDba = '';

    // ── Init: check if agent already has a domain ──
    function _showSetup() {
        var loading = document.getElementById('domainLoading');
        var setup = document.getElementById('domainSetup');
        if (loading) loading.style.display = 'none';
        if (setup) setup.style.display = 'block';
        _setPill('Not Set Up', '#666');
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

    function _setPill(text, color) {
        var pill = document.getElementById('domainStatusPill');
        if (pill) { pill.textContent = text; pill.style.color = color || ''; }
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
                    _setPill('Live', 'var(--accent, #00ff88)');
                } else if (d.has_domain && d.status === 'provisioning') {
                    var prov = document.getElementById('domainProvisioning');
                    if (prov) prov.style.display = 'block';
                    _setPill('Provisioning...', '#f59e0b');
                } else if (d.has_domain && d.status === 'error') {
                    _showActiveDomain(d);
                    _setPill('Error', '#ef4444');
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

        // Pre-populate edit form with current data
        var editName = document.getElementById('domainEditName');
        var editPhone = document.getElementById('domainEditPhone');
        var editStates = document.getElementById('domainEditStates');
        var editBio = document.getElementById('domainEditBio');
        if (editName && d.agent_name) editName.value = d.agent_name;
        if (editPhone && d.phone_display) editPhone.value = d.phone_display;
        if (editStates && d.licensed_states) editStates.value = Array.isArray(d.licensed_states) ? d.licensed_states.join(', ') : d.licensed_states;
        if (editBio && d.bio) editBio.value = d.bio;
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
                    // Promo applied — button stays clean, status line shows the discount

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
                        // Cloudflare email verification toast
                        if (d.email_verification_needed) {
                            setTimeout(() => {
                                _showEmailVerificationToast(d.email_notice);
                            }, 1500);
                        }
                    }, 2000);
                } else {
                    _updateProvisioningSteps(d.provisioning_log || [], false, d.error);
                    btn.disabled = false;
                    btn.innerHTML = 'Get My Domain';
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

    // ── Cloudflare Email Verification Toast ──
    // Persistent notification reminding the user to verify their forwarding email.
    // Shows after domain provisioning and on page load if verification is still pending.
    function _showEmailVerificationToast(message) {
        // Don't show if already dismissed this session
        if (sessionStorage.getItem('cf_email_toast_dismissed')) return;
        // Remove any existing toast
        var existing = document.getElementById('cfEmailVerifyToast');
        if (existing) existing.remove();

        var toast = document.createElement('div');
        toast.id = 'cfEmailVerifyToast';
        toast.className = 'cf-verify-toast';
        toast.innerHTML =
            '<div class="cf-verify-toast-inner">' +
                '<div class="cf-verify-toast-icon"><i class="fa-solid fa-envelope-circle-check"></i></div>' +
                '<div class="cf-verify-toast-body">' +
                    '<div class="cf-verify-toast-title">Verify Your Email</div>' +
                    '<div class="cf-verify-toast-text">' +
                        _esc(message || 'Check your inbox (and spam folder) for a verification email from Cloudflare. Click the link to activate email forwarding for your new domain.') +
                    '</div>' +
                '</div>' +
                '<button class="cf-verify-toast-close" onclick="document.getElementById(\'cfEmailVerifyToast\').remove();sessionStorage.setItem(\'cf_email_toast_dismissed\',\'1\')">' +
                    '<i class="fa-solid fa-xmark"></i>' +
                '</button>' +
            '</div>';
        document.body.appendChild(toast);
        // Animate in
        requestAnimationFrame(function () { toast.classList.add('visible'); });
    }

    // Check on domain tab load if verification is still pending
    var _origInit = window.domainTabInit;
    window.domainTabInit = function () {
        if (_origInit) _origInit();
        // Check if domain has pending email verification
        fetch('/api/domain/status')
            .then(function (r) { return r.json(); })
            .then(function (d) {
                if (d.has_domain && d.email_verification_needed) {
                    var fwd = d.email_forward_to || 'your inbox';
                    _showEmailVerificationToast(
                        'Check ' + fwd + ' (and spam folder) for a verification email from Cloudflare. Click the link to activate email forwarding to your business email.'
                    );
                }
            })
            .catch(function () {});
    };

    // ── Utility ──
    function _esc(str) {
        if (!str) return '';
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    // ═══════════════════════════════════════════════════════════════════
    // Phase 3 & 4 — Layout Editor + AI Questionnaire
    // ═══════════════════════════════════════════════════════════════════

    let _pbSections = [];
    let _pbConfig = {};
    let _pbAiState = null;
    let _pbDirty = false;

    const _PB_ICONS = {
        hero: 'fa-star', about: 'fa-user', services: 'fa-briefcase',
        why_me: 'fa-trophy', carriers: 'fa-building-columns',
        testimonials: 'fa-quote-left', faq: 'fa-circle-question',
        contact_form: 'fa-envelope', footer: 'fa-ellipsis'
    };
    const _PB_NAMES = {
        hero: 'Hero', about: 'About Me', services: 'Services I Offer',
        why_me: 'Why Choose Me', carriers: 'Carriers I Represent',
        testimonials: 'Testimonials', faq: 'FAQ',
        contact_form: 'Contact Form', footer: 'Footer'
    };
    const _PB_FIXED = ['hero', 'contact_form', 'footer'];

    const _PB_AI_QUESTIONS = {
        about: [
            { q: 'How long have you been in the insurance industry?', options: ['Less than 1 year', '1-3 years', '3-5 years', '5-10 years', '10+ years'], multi: false },
            { q: 'What got you into insurance?', options: ['Helping families', 'Financial opportunity', 'Career change', 'Family business'], multi: false },
            { q: 'What makes your approach unique?', options: ['Personalized service', 'Deep product knowledge', 'Fast response time', 'Holistic financial planning'], multi: false },
            { q: 'What do clients say about working with you?', options: ['Always available', 'Explains things clearly', 'Finds the best rates', 'Feels like family'], multi: false },
            { q: 'Anything else you want visitors to know about you?', options: [], multi: false }
        ],
        services: [
            { q: 'Which insurance products do you offer?', options: ['Term Life', 'Whole Life', 'IUL / Indexed Universal Life', 'Final Expense', 'Annuities', 'Medicare Supplements', 'Health Insurance', 'Disability Insurance'], multi: true },
            { q: 'Who is your ideal client?', options: ['Young families', 'Retirees', 'Business owners', 'High net-worth individuals', 'Anyone who needs coverage'], multi: false },
            { q: 'What is your service style?', options: ['Consultative — I educate first', 'Efficient — quick quotes, fast answers', 'Relationship-based — long-term partnerships', 'Tech-forward — everything online'], multi: false }
        ],
        why_me: [
            { q: 'What sets you apart from other agents?', options: ['Independent — I shop dozens of carriers', 'Local expertise — I know my community', 'Specialized knowledge in my niche', 'Exceptional follow-up and service'], multi: false },
            { q: 'Do you have any credentials or achievements?', options: ['MDRT qualifier', 'LUTCF / CLU / ChFC designation', 'Top producer award', 'None yet — just getting started'], multi: false },
            { q: 'What should a prospect feel after visiting your page?', options: ['Trust and confidence', 'Urgency to act now', 'Relief that help is available', 'Excitement about their options'], multi: false }
        ]
    };

    // ── Layout Editor ──

    window.pbOpenEditor = function () {
        var editorEl = document.getElementById('pbEditor');
        var oldForm = document.getElementById('domainEditForm');
        if (!editorEl) return;

        editorEl.innerHTML = '<div class="pb-loading"><i class="fa-solid fa-spinner fa-spin"></i> Loading sections...</div>';
        if (oldForm) oldForm.style.display = 'none';
        editorEl.style.display = 'block';

        fetch('/api/domain/sections')
            .then(function (r) {
                if (!r.ok) throw new Error('HTTP ' + r.status);
                return r.json();
            })
            .then(function (d) {
                _pbConfig = d;
                _pbSections = d.sections || [];
                _pbDirty = false;
                _pbRenderAccordion();
            })
            .catch(function () {
                editorEl.innerHTML = '<div class="pb-error">Failed to load sections. <button class="pb-retry-btn" onclick="pbOpenEditor()">Retry</button></div>';
            });
    };

    function _pbRenderAccordion() {
        var editorEl = document.getElementById('pbEditor');
        if (!editorEl) return;

        var html = '<div class="pb-toolbar">' +
            '<button class="pb-save-btn" onclick="pbSaveLayout()"><i class="fa-solid fa-floppy-disk"></i> Save Layout</button>' +
            '<button class="pb-preview-btn" onclick="pbPreviewSite()"><i class="fa-solid fa-eye"></i> Preview</button>' +
            '</div><div class="pb-accordion">';

        for (var i = 0; i < _pbSections.length; i++) {
            var s = _pbSections[i];
            var isFixed = _PB_FIXED.indexOf(s.type) !== -1;
            var icon = _PB_ICONS[s.type] || 'fa-puzzle-piece';
            var name = _PB_NAMES[s.type] || s.type;
            var enabled = s.enabled !== false;
            var badge = isFixed ? 'Always On' : (enabled ? 'On' : 'Off');
            var badgeClass = isFixed ? 'pb-badge-fixed' : (enabled ? 'pb-badge-on' : 'pb-badge-off');

            html += '<div class="pb-section-card" data-index="' + i + '">' +
                '<div class="pb-section-header" onclick="pbToggleSection(this)">' +
                    '<div class="pb-section-left">' +
                        '<div class="pb-reorder-btns">' +
                            '<button class="pb-reorder-btn" onclick="event.stopPropagation();pbMoveSection(' + i + ',-1)"' + (isFixed || i === 0 ? ' disabled' : '') + '><i class="fa-solid fa-chevron-up"></i></button>' +
                            '<button class="pb-reorder-btn" onclick="event.stopPropagation();pbMoveSection(' + i + ',1)"' + (isFixed || i === _pbSections.length - 1 ? ' disabled' : '') + '><i class="fa-solid fa-chevron-down"></i></button>' +
                        '</div>' +
                        '<i class="fa-solid ' + icon + ' pb-section-icon"></i>' +
                        '<span class="pb-section-name">' + _esc(name) + '</span>' +
                        '<span class="pb-badge ' + badgeClass + '">' + badge + '</span>' +
                    '</div>' +
                    '<div class="pb-section-right">' +
                        (isFixed ? '' : '<label class="pb-toggle" onclick="event.stopPropagation()"><input type="checkbox"' + (enabled ? ' checked' : '') + ' onchange="pbToggleEnabled(' + i + ')"><span class="pb-toggle-slider"></span></label>') +
                        '<i class="fa-solid fa-chevron-down pb-expand-arrow"></i>' +
                    '</div>' +
                '</div>' +
                '<div class="pb-section-body">' + _pbRenderSectionBody(s, i) + '</div>' +
            '</div>';
        }

        html += '</div>';
        editorEl.innerHTML = html;
    }

    function _pbRenderSectionBody(section, index) {
        var t = section.type;
        var html = '';

        if (t === 'hero') {
            var photoUrl = (section.content && section.content.photo_url) || '';
            html += '<div class="pb-hero-body">' +
                '<div class="pb-photo-zone">' +
                    (photoUrl
                        ? '<img src="' + _esc(photoUrl) + '" class="pb-photo-preview" id="pbPhotoPreview">'
                        : '<div class="pb-photo-placeholder" id="pbPhotoPreview"><i class="fa-solid fa-camera"></i><span>Upload Photo</span></div>') +
                    '<div class="pb-photo-actions">' +
                        '<label class="pb-photo-upload-btn"><i class="fa-solid fa-upload"></i> Upload<input type="file" accept="image/*" onchange="pbPhotoUpload(this)" style="display:none"></label>' +
                        (photoUrl ? '<button class="pb-photo-remove-btn" onclick="pbPhotoRemove()"><i class="fa-solid fa-trash"></i> Remove</button>' : '') +
                    '</div>' +
                '</div>' +
                '<div class="pb-hero-fields">' +
                    '<label class="pb-field-label">Display Name</label>' +
                    '<input type="text" class="pb-input" id="pbHeroName" value="' + _esc((section.content && section.content.name) || '') + '" placeholder="Your name">' +
                    '<label class="pb-field-label">Phone Number</label>' +
                    '<input type="text" class="pb-input" id="pbHeroPhone" value="' + _esc((section.content && section.content.phone) || '') + '" placeholder="(555) 123-4567">' +
                '</div>' +
            '</div>';
        } else if (t === 'about' || t === 'services' || t === 'why_me') {
            var content = (section.content && typeof section.content === 'string') ? section.content : (section.content && section.content.text) || '';
            html += '<div class="pb-content-body">' +
                '<button class="pb-ai-btn" onclick="pbStartAI(\'' + t + '\',' + index + ')"><i class="fa-solid fa-wand-magic-sparkles"></i> Build with AI</button>' +
                '<textarea class="pb-textarea" id="pbContent_' + index + '" rows="6" placeholder="Write your ' + _esc(_PB_NAMES[t] || t) + ' content...">' + _esc(content) + '</textarea>' +
                '<div class="pb-ai-flow" id="pbAiFlow_' + index + '" style="display:none"></div>' +
            '</div>';
        } else if (t === 'carriers') {
            var carriers = (_pbConfig.carriers || []);
            html += '<div class="pb-carriers-body">' +
                '<div class="pb-carrier-chips">';
            for (var c = 0; c < carriers.length; c++) {
                html += '<span class="pb-carrier-chip">' + _esc(carriers[c]) + '</span>';
            }
            if (!carriers.length) {
                html += '<span class="pb-no-data">No carriers selected yet.</span>';
            }
            html += '</div>' +
                '<a href="#" class="pb-edit-link" onclick="event.preventDefault();sidebarNavigate(\'carriers\')"><i class="fa-solid fa-pen"></i> Edit in Carriers tab</a>' +
            '</div>';
        } else if (t === 'testimonials') {
            html += '<div class="pb-testimonials-body">' +
                '<div class="pb-review-list" id="pbReviewList"><div class="pb-loading-sm"><i class="fa-solid fa-spinner fa-spin"></i> Loading reviews...</div></div>' +
                '<div class="pb-add-review">' +
                    '<h4 class="pb-sub-heading">Add Testimonial</h4>' +
                    '<input type="text" class="pb-input" id="pbReviewName" placeholder="Client name">' +
                    '<div class="pb-star-picker" id="pbStarPicker">' +
                        '<span class="pb-star" onclick="pbPickStar(1)"><i class="fa-solid fa-star"></i></span>' +
                        '<span class="pb-star" onclick="pbPickStar(2)"><i class="fa-solid fa-star"></i></span>' +
                        '<span class="pb-star" onclick="pbPickStar(3)"><i class="fa-solid fa-star"></i></span>' +
                        '<span class="pb-star" onclick="pbPickStar(4)"><i class="fa-solid fa-star"></i></span>' +
                        '<span class="pb-star selected" onclick="pbPickStar(5)"><i class="fa-solid fa-star"></i></span>' +
                    '</div>' +
                    '<textarea class="pb-textarea" id="pbReviewText" rows="3" placeholder="What did they say?"></textarea>' +
                    '<button class="pb-add-btn" onclick="pbAddTestimonial()"><i class="fa-solid fa-plus"></i> Add</button>' +
                '</div>' +
                '<div class="pb-review-link">' +
                    '<span class="pb-field-label">Share Review Link</span>' +
                    '<div class="pb-copy-row">' +
                        '<input type="text" class="pb-input pb-copy-input" readonly value="' + _esc((_pbConfig.domain ? 'https://' + _pbConfig.domain + '/review' : '')) + '">' +
                        '<button class="pb-copy-btn" onclick="pbCopyReviewLink()"><i class="fa-solid fa-copy"></i></button>' +
                    '</div>' +
                '</div>' +
            '</div>';
            // Kick off review load
            setTimeout(function () { pbLoadReviews(); }, 50);
        } else if (t === 'faq') {
            var items = (section.content && Array.isArray(section.content.items)) ? section.content.items : [];
            html += '<div class="pb-faq-body">';
            for (var f = 0; f < items.length; f++) {
                var faq = items[f];
                var faqEnabled = faq.enabled !== false;
                html += '<div class="pb-faq-item">' +
                    '<div class="pb-faq-header">' +
                        '<label class="pb-toggle pb-toggle-sm" onclick="event.stopPropagation()"><input type="checkbox"' + (faqEnabled ? ' checked' : '') + ' onchange="pbToggleFaq(' + index + ',' + f + ')"><span class="pb-toggle-slider"></span></label>' +
                        '<span class="pb-faq-question">' + _esc(faq.question || '') + '</span>' +
                    '</div>' +
                    '<textarea class="pb-textarea pb-faq-answer" rows="2" onchange="pbEditFaqAnswer(' + index + ',' + f + ',this.value)">' + _esc(faq.answer || '') + '</textarea>' +
                '</div>';
            }
            html += '<div class="pb-add-faq">' +
                '<h4 class="pb-sub-heading">Add Custom FAQ</h4>' +
                '<input type="text" class="pb-input" id="pbFaqQuestion" placeholder="Question">' +
                '<textarea class="pb-textarea" id="pbFaqAnswer" rows="2" placeholder="Answer"></textarea>' +
                '<button class="pb-add-btn" onclick="pbAddFaq()"><i class="fa-solid fa-plus"></i> Add FAQ</button>' +
            '</div></div>';
        } else {
            // contact_form, footer
            html += '<div class="pb-info-note"><i class="fa-solid fa-info-circle"></i> Always displayed. No configuration needed.</div>';
        }

        return html;
    }

    window.pbToggleSection = function (headerEl) {
        var card = headerEl.closest('.pb-section-card');
        if (card) card.classList.toggle('pb-expanded');
    };

    window.pbMoveSection = function (index, direction) {
        var target = index + direction;
        if (target < 0 || target >= _pbSections.length) return;
        // Don't move into or out of fixed positions
        if (_PB_FIXED.indexOf(_pbSections[index].type) !== -1) return;
        if (_PB_FIXED.indexOf(_pbSections[target].type) !== -1) return;

        var tmp = _pbSections[index];
        _pbSections[index] = _pbSections[target];
        _pbSections[target] = tmp;
        _pbDirty = true;
        _pbRenderAccordion();
    };

    window.pbToggleEnabled = function (index) {
        if (index < 0 || index >= _pbSections.length) return;
        _pbSections[index].enabled = !_pbSections[index].enabled;
        _pbDirty = true;
        // Update badge in place
        var card = document.querySelector('.pb-section-card[data-index="' + index + '"]');
        if (card) {
            var badge = card.querySelector('.pb-badge');
            if (badge) {
                var on = _pbSections[index].enabled;
                badge.textContent = on ? 'On' : 'Off';
                badge.className = 'pb-badge ' + (on ? 'pb-badge-on' : 'pb-badge-off');
            }
        }
    };

    window.pbSaveLayout = function () {
        var saveBtn = document.querySelector('.pb-save-btn');
        if (saveBtn) { saveBtn.disabled = true; saveBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Saving...'; }

        // Collect any inline edits from textareas/inputs
        for (var i = 0; i < _pbSections.length; i++) {
            var s = _pbSections[i];
            if (s.type === 'hero') {
                var nameEl = document.getElementById('pbHeroName');
                var phoneEl = document.getElementById('pbHeroPhone');
                if (!s.content || typeof s.content !== 'object') s.content = {};
                if (nameEl) s.content.name = nameEl.value.trim();
                if (phoneEl) s.content.phone = phoneEl.value.trim();
            } else if (s.type === 'about' || s.type === 'services' || s.type === 'why_me') {
                var ta = document.getElementById('pbContent_' + i);
                if (ta) s.content = ta.value.trim();
            }
        }

        fetch('/api/domain/sections', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ sections: _pbSections })
        })
            .then(function (r) { return r.json(); })
            .then(function (d) {
                if (saveBtn) { saveBtn.disabled = false; saveBtn.innerHTML = '<i class="fa-solid fa-floppy-disk"></i> Save Layout'; }
                if (d.status === 'ok' || d.ok) {
                    _pbDirty = false;
                    if (typeof _showDashToast === 'function') _showDashToast(true, 'Layout saved');
                } else {
                    if (typeof _showDashToast === 'function') _showDashToast(false, d.error || 'Save failed');
                }
            })
            .catch(function () {
                if (saveBtn) { saveBtn.disabled = false; saveBtn.innerHTML = '<i class="fa-solid fa-floppy-disk"></i> Save Layout'; }
                if (typeof _showDashToast === 'function') _showDashToast(false, 'Network error');
            });
    };

    window.pbPreviewSite = function () {
        var domain = _pbConfig.domain || '';
        if (domain) {
            window.open('https://' + domain, '_blank');
        } else {
            if (typeof _showDashToast === 'function') _showDashToast(false, 'No domain configured');
        }
    };

    // ── Photo Upload ──

    window.pbPhotoUpload = function (inputEl) {
        if (!inputEl || !inputEl.files || !inputEl.files[0]) return;
        var file = inputEl.files[0];
        if (file.size > 2 * 1024 * 1024) {
            if (typeof _showDashToast === 'function') _showDashToast(false, 'Photo must be under 2MB');
            inputEl.value = '';
            return;
        }

        var reader = new FileReader();
        reader.onload = function (e) {
            var base64 = e.target.result;
            // Show preview immediately
            var preview = document.getElementById('pbPhotoPreview');
            if (preview) {
                var img = document.createElement('img');
                img.src = base64;
                img.className = 'pb-photo-preview';
                img.id = 'pbPhotoPreview';
                preview.replaceWith(img);
            }

            fetch('/api/domain/photo', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ photo_data: base64 })
            })
                .then(function (r) { return r.json(); })
                .then(function (d) {
                    if (d.photo_url) {
                        // Update section state
                        for (var i = 0; i < _pbSections.length; i++) {
                            if (_pbSections[i].type === 'hero') {
                                if (!_pbSections[i].content || typeof _pbSections[i].content !== 'object') _pbSections[i].content = {};
                                _pbSections[i].content.photo_url = d.photo_url;
                                break;
                            }
                        }
                        if (typeof _showDashToast === 'function') _showDashToast(true, 'Photo uploaded');
                    } else {
                        if (typeof _showDashToast === 'function') _showDashToast(false, d.error || 'Upload failed');
                    }
                })
                .catch(function () {
                    if (typeof _showDashToast === 'function') _showDashToast(false, 'Upload failed');
                });
        };
        reader.readAsDataURL(file);
    };

    window.pbPhotoRemove = function () {
        fetch('/api/domain/photo', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ photo_url: '' })
        })
            .then(function (r) { return r.json(); })
            .then(function (d) {
                if (d.status === 'ok' || d.ok) {
                    for (var i = 0; i < _pbSections.length; i++) {
                        if (_pbSections[i].type === 'hero') {
                            if (_pbSections[i].content) _pbSections[i].content.photo_url = '';
                            break;
                        }
                    }
                    // Re-render hero section
                    _pbRenderAccordion();
                    if (typeof _showDashToast === 'function') _showDashToast(true, 'Photo removed');
                }
            })
            .catch(function () {
                if (typeof _showDashToast === 'function') _showDashToast(false, 'Failed to remove photo');
            });
    };

    // ── AI Questionnaire Flows ──

    window.pbStartAI = function (sectionType, index) {
        var questions = _PB_AI_QUESTIONS[sectionType];
        if (!questions || !questions.length) return;

        _pbAiState = { type: sectionType, index: index, questionIndex: 0, answers: [] };
        var flowEl = document.getElementById('pbAiFlow_' + index);
        if (flowEl) flowEl.style.display = 'block';
        _pbRenderAiQuestion();
    };

    function _pbRenderAiQuestion() {
        if (!_pbAiState) return;
        var questions = _PB_AI_QUESTIONS[_pbAiState.type];
        if (!questions) return;

        var qi = _pbAiState.questionIndex;
        var q = questions[qi];
        var flowEl = document.getElementById('pbAiFlow_' + _pbAiState.index);
        if (!flowEl) return;

        var isLast = qi === questions.length - 1;
        var prevAnswer = _pbAiState.answers[qi];

        var html = '<div class="pb-ai-question">' +
            '<div class="pb-ai-progress">' + (qi + 1) + ' / ' + questions.length + '</div>' +
            '<h4 class="pb-ai-q-text">' + _esc(q.q) + '</h4>';

        if (q.options && q.options.length) {
            html += '<div class="pb-ai-options">';
            for (var o = 0; o < q.options.length; o++) {
                var selected = false;
                if (prevAnswer) {
                    if (q.multi && Array.isArray(prevAnswer.selected)) {
                        selected = prevAnswer.selected.indexOf(q.options[o]) !== -1;
                    } else if (!q.multi && prevAnswer.selected === q.options[o]) {
                        selected = true;
                    }
                }
                html += '<button class="pb-ai-option' + (selected ? ' selected' : '') + '" onclick="pbAiSelectOption(this,' + (q.multi ? 'true' : 'false') + ')">' + _esc(q.options[o]) + '</button>';
            }
            html += '</div>';
        }

        // Other / open text input
        var otherVal = (prevAnswer && prevAnswer.other) || '';
        html += '<input type="text" class="pb-input pb-ai-other" id="pbAiOther" placeholder="' + (q.options && q.options.length ? 'Or type your own...' : 'Type your answer...') + '" value="' + _esc(otherVal) + '">';

        html += '<div class="pb-ai-nav">';
        if (qi > 0) {
            html += '<button class="pb-ai-back-btn" onclick="pbAiBack()"><i class="fa-solid fa-arrow-left"></i> Back</button>';
        } else {
            html += '<span></span>';
        }
        if (isLast) {
            html += '<button class="pb-ai-generate-btn" onclick="pbAiNext()"><i class="fa-solid fa-wand-magic-sparkles"></i> Generate</button>';
        } else {
            html += '<button class="pb-ai-next-btn" onclick="pbAiNext()">Next <i class="fa-solid fa-arrow-right"></i></button>';
        }
        html += '</div></div>';

        flowEl.innerHTML = html;
    }

    window.pbAiSelectOption = function (btn, multi) {
        if (multi) {
            btn.classList.toggle('selected');
        } else {
            var siblings = btn.parentElement.querySelectorAll('.pb-ai-option');
            for (var s = 0; s < siblings.length; s++) siblings[s].classList.remove('selected');
            btn.classList.add('selected');
        }
    };

    function _pbCollectCurrentAnswer() {
        if (!_pbAiState) return null;
        var questions = _PB_AI_QUESTIONS[_pbAiState.type];
        var q = questions[_pbAiState.questionIndex];
        var flowEl = document.getElementById('pbAiFlow_' + _pbAiState.index);
        if (!flowEl) return null;

        var answer = { question: q.q, selected: null, other: '' };
        var otherInput = document.getElementById('pbAiOther');
        if (otherInput) answer.other = otherInput.value.trim();

        var selectedBtns = flowEl.querySelectorAll('.pb-ai-option.selected');
        if (q.multi) {
            answer.selected = [];
            for (var s = 0; s < selectedBtns.length; s++) answer.selected.push(selectedBtns[s].textContent);
        } else if (selectedBtns.length) {
            answer.selected = selectedBtns[0].textContent;
        }

        return answer;
    }

    window.pbAiNext = function () {
        if (!_pbAiState) return;
        var answer = _pbCollectCurrentAnswer();
        _pbAiState.answers[_pbAiState.questionIndex] = answer;

        var questions = _PB_AI_QUESTIONS[_pbAiState.type];
        var isLast = _pbAiState.questionIndex === questions.length - 1;

        if (isLast) {
            pbAiGenerate();
        } else {
            _pbAiState.questionIndex++;
            _pbRenderAiQuestion();
        }
    };

    window.pbAiBack = function () {
        if (!_pbAiState || _pbAiState.questionIndex <= 0) return;
        var answer = _pbCollectCurrentAnswer();
        _pbAiState.answers[_pbAiState.questionIndex] = answer;
        _pbAiState.questionIndex--;
        _pbRenderAiQuestion();
    };

    window.pbAiGenerate = function () {
        if (!_pbAiState) return;
        var flowEl = document.getElementById('pbAiFlow_' + _pbAiState.index);
        if (!flowEl) return;

        // Build answers payload
        var payload = { section_type: _pbAiState.type, answers: {} };
        for (var a = 0; a < _pbAiState.answers.length; a++) {
            var ans = _pbAiState.answers[a];
            if (!ans) continue;
            var val = '';
            if (ans.other) {
                val = ans.other;
            } else if (Array.isArray(ans.selected)) {
                val = ans.selected.join(', ');
            } else if (ans.selected) {
                val = ans.selected;
            }
            payload.answers['q' + (a + 1)] = val;
        }

        flowEl.innerHTML = '<div class="pb-ai-shimmer"><div class="pb-shimmer-bar"></div><div class="pb-shimmer-bar"></div><div class="pb-shimmer-bar"></div><p class="pb-shimmer-text">Generating content...</p></div>';

        fetch('/api/domain/section-ai', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        })
            .then(function (r) { return r.json(); })
            .then(function (d) {
                if (d.content) {
                    var idx = _pbAiState.index;
                    flowEl.innerHTML = '<div class="pb-ai-result">' +
                        '<h4 class="pb-sub-heading">Generated Content</h4>' +
                        '<textarea class="pb-textarea pb-ai-result-text" id="pbAiResultText" rows="8">' + _esc(d.content) + '</textarea>' +
                        '<div class="pb-ai-result-actions">' +
                            '<button class="pb-ai-use-btn" onclick="pbAiUseResult(' + idx + ')"><i class="fa-solid fa-check"></i> Use This</button>' +
                            '<button class="pb-ai-regen-btn" onclick="pbAiGenerate()"><i class="fa-solid fa-rotate"></i> Regenerate</button>' +
                        '</div>' +
                    '</div>';
                } else {
                    flowEl.innerHTML = '<div class="pb-ai-error"><i class="fa-solid fa-triangle-exclamation"></i> ' + _esc(d.error || 'Generation failed') + ' <button class="pb-ai-regen-btn" onclick="pbAiGenerate()">Retry</button></div>';
                }
            })
            .catch(function () {
                flowEl.innerHTML = '<div class="pb-ai-error"><i class="fa-solid fa-triangle-exclamation"></i> Network error. <button class="pb-ai-regen-btn" onclick="pbAiGenerate()">Retry</button></div>';
            });
    };

    window.pbAiUseResult = function (index) {
        var resultText = document.getElementById('pbAiResultText');
        if (!resultText) return;
        var text = resultText.value.trim();

        _pbSections[index].content = text;
        _pbDirty = true;

        // Put text in the main textarea
        var ta = document.getElementById('pbContent_' + index);
        if (ta) ta.value = text;

        // Hide AI flow
        var flowEl = document.getElementById('pbAiFlow_' + index);
        if (flowEl) flowEl.style.display = 'none';

        _pbAiState = null;
        if (typeof _showDashToast === 'function') _showDashToast(true, 'Content applied');
    };

    // ── Testimonials ──

    var _pbStarRating = 5;

    window.pbPickStar = function (n) {
        _pbStarRating = n;
        var stars = document.querySelectorAll('#pbStarPicker .pb-star');
        for (var s = 0; s < stars.length; s++) {
            if (s < n) stars[s].classList.add('selected');
            else stars[s].classList.remove('selected');
        }
    };

    window.pbLoadReviews = function () {
        var listEl = document.getElementById('pbReviewList');
        if (!listEl) return;

        fetch('/api/domain/reviews')
            .then(function (r) { return r.json(); })
            .then(function (d) {
                var reviews = d.reviews || [];
                if (!reviews.length) {
                    listEl.innerHTML = '<div class="pb-no-data">No testimonials yet.</div>';
                    return;
                }
                var html = '';
                for (var r = 0; r < reviews.length; r++) {
                    var rev = reviews[r];
                    var stars = '';
                    for (var s = 0; s < 5; s++) {
                        stars += '<i class="fa-solid fa-star' + (s < (rev.stars || 5) ? ' pb-star-filled' : ' pb-star-empty') + '"></i>';
                    }
                    html += '<div class="pb-review-item' + (rev.approved ? '' : ' pb-review-pending') + '">' +
                        '<div class="pb-review-top">' +
                            '<span class="pb-review-name">' + _esc(rev.name || 'Anonymous') + '</span>' +
                            '<span class="pb-review-stars">' + stars + '</span>' +
                            (!rev.approved ? '<span class="pb-badge pb-badge-off">Pending</span>' : '') +
                        '</div>' +
                        '<p class="pb-review-text">' + _esc(rev.text || '') + '</p>' +
                        '<div class="pb-review-actions">' +
                            (!rev.approved ? '<button class="pb-approve-btn" onclick="pbApproveReview(' + r + ',true)"><i class="fa-solid fa-check"></i> Approve</button>' : '') +
                            '<button class="pb-delete-btn" onclick="pbDeleteReview(' + r + ')"><i class="fa-solid fa-trash"></i></button>' +
                        '</div>' +
                    '</div>';
                }
                listEl.innerHTML = html;
            })
            .catch(function () {
                listEl.innerHTML = '<div class="pb-no-data">Failed to load reviews.</div>';
            });
    };

    window.pbAddTestimonial = function () {
        var nameEl = document.getElementById('pbReviewName');
        var textEl = document.getElementById('pbReviewText');
        if (!nameEl || !textEl) return;

        var name = nameEl.value.trim();
        var text = textEl.value.trim();
        if (!name || !text) {
            if (typeof _showDashToast === 'function') _showDashToast(false, 'Name and testimonial text are required');
            return;
        }

        // Add to the testimonials section content
        for (var i = 0; i < _pbSections.length; i++) {
            if (_pbSections[i].type === 'testimonials') {
                if (!_pbSections[i].content || typeof _pbSections[i].content !== 'object') _pbSections[i].content = { items: [] };
                if (!Array.isArray(_pbSections[i].content.items)) _pbSections[i].content.items = [];
                _pbSections[i].content.items.push({ name: name, stars: _pbStarRating, text: text, approved: true });
                _pbDirty = true;
                break;
            }
        }

        // Clear form
        nameEl.value = '';
        textEl.value = '';
        _pbStarRating = 5;
        pbPickStar(5);

        // Refresh the review list display
        pbLoadReviews();
        if (typeof _showDashToast === 'function') _showDashToast(true, 'Testimonial added');
    };

    window.pbApproveReview = function (reviewIndex, approved) {
        fetch('/api/domain/reviews/approve', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ index: reviewIndex, approved: approved })
        })
            .then(function (r) { return r.json(); })
            .then(function (d) {
                if (d.status === 'ok' || d.ok) {
                    pbLoadReviews();
                    if (typeof _showDashToast === 'function') _showDashToast(true, approved ? 'Review approved' : 'Review hidden');
                } else {
                    if (typeof _showDashToast === 'function') _showDashToast(false, d.error || 'Failed');
                }
            })
            .catch(function () {
                if (typeof _showDashToast === 'function') _showDashToast(false, 'Network error');
            });
    };

    window.pbDeleteReview = function (reviewIndex) {
        fetch('/api/domain/reviews/approve', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ index: reviewIndex, delete: true })
        })
            .then(function (r) { return r.json(); })
            .then(function (d) {
                if (d.status === 'ok' || d.ok) {
                    pbLoadReviews();
                    if (typeof _showDashToast === 'function') _showDashToast(true, 'Review deleted');
                } else {
                    if (typeof _showDashToast === 'function') _showDashToast(false, d.error || 'Failed');
                }
            })
            .catch(function () {
                if (typeof _showDashToast === 'function') _showDashToast(false, 'Network error');
            });
    };

    window.pbCopyReviewLink = function () {
        var domain = _pbConfig.domain || '';
        if (!domain) {
            if (typeof _showDashToast === 'function') _showDashToast(false, 'No domain configured');
            return;
        }
        var url = 'https://' + domain + '/review';
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(url).then(function () {
                if (typeof _showDashToast === 'function') _showDashToast(true, 'Review link copied');
            });
        } else {
            // Fallback
            var ta = document.createElement('textarea');
            ta.value = url;
            document.body.appendChild(ta);
            ta.select();
            document.execCommand('copy');
            document.body.removeChild(ta);
            if (typeof _showDashToast === 'function') _showDashToast(true, 'Review link copied');
        }
    };

    // ── FAQ ──

    window.pbToggleFaq = function (sectionIndex, faqIndex) {
        if (!_pbSections[sectionIndex] || !_pbSections[sectionIndex].content) return;
        var items = _pbSections[sectionIndex].content.items;
        if (!items || !items[faqIndex]) return;
        items[faqIndex].enabled = !items[faqIndex].enabled;
        _pbDirty = true;
    };

    window.pbAddFaq = function () {
        var qEl = document.getElementById('pbFaqQuestion');
        var aEl = document.getElementById('pbFaqAnswer');
        if (!qEl || !aEl) return;

        var question = qEl.value.trim();
        var answer = aEl.value.trim();
        if (!question || !answer) {
            if (typeof _showDashToast === 'function') _showDashToast(false, 'Question and answer are required');
            return;
        }

        for (var i = 0; i < _pbSections.length; i++) {
            if (_pbSections[i].type === 'faq') {
                if (!_pbSections[i].content || typeof _pbSections[i].content !== 'object') _pbSections[i].content = { items: [] };
                if (!Array.isArray(_pbSections[i].content.items)) _pbSections[i].content.items = [];
                _pbSections[i].content.items.push({ question: question, answer: answer, enabled: true });
                _pbDirty = true;
                // Re-render to show new item
                _pbRenderAccordion();
                if (typeof _showDashToast === 'function') _showDashToast(true, 'FAQ added');
                return;
            }
        }
    };

    window.pbEditFaqAnswer = function (sectionIndex, faqIndex, newText) {
        if (!_pbSections[sectionIndex] || !_pbSections[sectionIndex].content) return;
        var items = _pbSections[sectionIndex].content.items;
        if (!items || !items[faqIndex]) return;
        items[faqIndex].answer = newText;
        _pbDirty = true;
    };

})();
