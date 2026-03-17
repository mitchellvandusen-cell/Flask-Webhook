    function saveConfig() {
        const form = document.getElementById('main-config-form');
        const overlay = document.getElementById('save-overlay');
        const spinner = document.getElementById('save-spinner');
        const check = document.getElementById('save-check');
        const text = document.getElementById('save-text');
        const btn = document.getElementById('save-config-btn');

        // Collect form data
        const smsRadio = form.querySelector('input[name="sms_send_via"]:checked');
        const data = {
            location_id: form.querySelector('[name="location_id"]')?.value || '',
            calendar_id: form.querySelector('[name="calendar_id"]')?.value || '',
            calendar_name: form.querySelector('[name="calendar_name"]')?.value || '',
            crm_user_id: form.querySelector('[name="crm_user_id"]')?.value || '',
            bot_name: form.querySelector('[name="bot_name"]')?.value || '',
            timezone: form.querySelector('[name="timezone"]')?.value || '',
            initial_message: form.querySelector('[name="initial_message"]')?.value || '',
            personal_website: form.querySelector('[name="personal_website"]')?.value || '',
            sms_send_via: smsRadio ? smsRadio.value : 'ghl'
        };

        // Show overlay in "saving" state
        spinner.style.display = 'block';
        check.style.display = 'none';
        text.textContent = 'Saving to database...';
        overlay.classList.add('active');
        btn.disabled = true;

        fetch('/api/save-config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(data)
        })
        .then(r => r.json())
        .then(resp => {
            if (resp.success) {
                // Switch to success state
                spinner.style.display = 'none';
                check.style.display = 'block';
                text.textContent = 'Saved to database';
                setTimeout(() => {
                    overlay.classList.remove('active');
                    btn.disabled = false;
                }, 1500);
            } else {
                spinner.style.display = 'none';
                text.textContent = 'Error: ' + (resp.error || 'Save failed');
                text.style.color = _tc.red;
                setTimeout(() => {
                    overlay.classList.remove('active');
                    btn.disabled = false;
                    text.style.color = _tc.text;
                }, 2500);
            }
        })
        .catch(err => {
            spinner.style.display = 'none';
            text.textContent = 'Network error. Please try again.';
            text.style.color = _tc.red;
            setTimeout(() => {
                overlay.classList.remove('active');
                btn.disabled = false;
                text.style.color = _tc.text;
            }, 2500);
        });
    }

    // ── SMS Channel Picker: Look up available phone numbers ──────────────────
    function loadSmsChannelNumbers() {
        const container = document.getElementById('sms-number-options');
        const loading = document.getElementById('sms-channel-loading');
        const noNumbers = document.getElementById('sms-no-numbers');
        const lookupBtn = document.getElementById('sms-lookup-btn');
        if (!container) return;

        loading.style.display = '';
        noNumbers.style.display = 'none';
        if (lookupBtn) lookupBtn.style.display = 'none';
        container.innerHTML = '';

        // Use saved value from data attribute (server-rendered), fall back to checked radio
        const savedVal = document.getElementById('sms-channel-picker')?.dataset.saved || 'ghl';
        const checkedRadio = document.querySelector('input[name="sms_send_via"]:checked');
        const currentVal = (checkedRadio && checkedRadio.value !== 'ghl') ? checkedRadio.value : savedVal;

        // Fetch GHL phone numbers
        const ghlFetch = fetch('/api/ghl-phone-numbers?refresh=true').then(r => r.ok ? r.json() : {numbers: []}).catch(() => ({numbers: []}));
        // Also fetch provisioned numbers
        const provFetch = fetch('/voice/numbers').then(r => r.ok ? r.json() : {numbers: []}).catch(() => ({numbers: []}));

        Promise.all([ghlFetch, provFetch]).then(([ghlData, provData]) => {
            loading.style.display = 'none';

            const ghlNums = (ghlData.numbers || []).filter(n => n.number);
            const provNums = (provData.numbers || []).filter(n => n.phone);
            const allNums = [];
            const seen = new Set();

            ghlNums.forEach(n => {
                const num = n.number;
                if (seen.has(num)) return;
                seen.add(num);
                allNums.push({
                    number: num,
                    label: n.name || '',
                    smsCapable: n.capabilities?.sms !== false,
                    source: 'ghl',
                });
            });

            // Provisioned IGB numbers (skip duplicates)
            provNums.forEach(n => {
                const num = n.phone;
                if (seen.has(num)) return;
                seen.add(num);
                allNums.push({
                    number: num,
                    label: n.nickname || '',
                    smsCapable: n.capabilities?.sms !== false,
                    source: 'igb',
                });
            });

            if (allNums.length === 0) {
                noNumbers.style.display = '';
                return;
            }

            allNums.forEach(num => {
                const isChecked = currentVal === num.number;
                const isDisabled = !num.smsCapable;

                const el = document.createElement('label');
                el.className = 'sms-channel-option';
                el.style.cssText = 'display:flex;align-items:center;gap:12px;padding:12px 16px;background:' + _tc.surface + ';border:1px solid ' + _tc.border + ';border-radius:10px;cursor:pointer;transition:all .15s;' + (isChecked ? 'border-color:' + _themeColor('rgba(0,255,136,0.3)', 'rgba(0,136,74,0.3)') + ';background:' + _tc.accentBg + ';' : '') + (isDisabled ? 'opacity:0.4;cursor:not-allowed;' : '');

                const desc = num.label && num.label !== num.number ? num.label : (num.source === 'ghl' ? 'GoHighLevel / LeadConnector number' : 'InsuranceGrokBot number');

                const iconHtml = num.source === 'ghl'
                    ? `<img src="https://images.leadconnectorhq.com/image/f_webp/q_80/r_1200/u_https://assets.cdn.filesafe.space/WDJNKXKiQj2XODO7jYzV/media/66be66f4b5e9ba05e7a7c191.png" alt="LC" style="width:22px;height:22px;border-radius:4px;flex-shrink:0;" onerror="this.style.display='none'">`
                    : `<i class="fa-solid fa-robot" style="color:var(--accent);font-size:1rem;flex-shrink:0;width:22px;text-align:center;"></i>`;

                el.innerHTML = `
                    <input type="radio" name="sms_send_via" value="${num.number}"
                        ${isChecked ? 'checked' : ''} ${isDisabled ? 'disabled' : ''}
                        style="accent-color:var(--accent);width:16px;height:16px;flex-shrink:0;">
                    ${iconHtml}
                    <div style="flex:1;min-width:0;">
                        <div style="font-size:0.85rem;font-weight:600;color:${_tc.text};">${_formatPhone(num.number)}</div>
                        <div style="font-size:0.75rem;color:${_tc.textMut};">${desc}${isDisabled ? ' (no SMS capability)' : ''}</div>
                    </div>
                `;

                // Highlight selected option
                el.querySelector('input').addEventListener('change', function() {
                    document.querySelectorAll('.sms-channel-option').forEach(opt => {
                        opt.style.borderColor = _tc.border;
                        opt.style.background = _tc.surface;
                    });
                    if (this.checked) {
                        el.style.borderColor = _themeColor('rgba(0,255,136,0.3)', 'rgba(0,136,74,0.3)');
                        el.style.background = _tc.accentBg;
                    }
                });

                container.appendChild(el);
            });

            // Also highlight the GHL default option if selected
            const ghlDefault = document.querySelector('input[name="sms_send_via"][value="ghl"]');
            if (ghlDefault) {
                ghlDefault.addEventListener('change', function() {
                    document.querySelectorAll('.sms-channel-option').forEach(opt => {
                        opt.style.borderColor = _tc.border;
                        opt.style.background = _tc.surface;
                    });
                    if (this.checked) {
                        this.closest('.sms-channel-option').style.borderColor = _themeColor('rgba(0,255,136,0.3)', 'rgba(0,136,74,0.3)');
                        this.closest('.sms-channel-option').style.background = _tc.accentBg;
                    }
                });
            }
        });
    }

    function _formatPhone(phone) {
        if (!phone) return '';
        const digits = phone.replace(/\D/g, '');
        if (digits.length === 11 && digits[0] === '1') {
            return `+1 (${digits.slice(1,4)}) ${digits.slice(4,7)}-${digits.slice(7)}`;
        }
        return phone;
    }

    // Auto-load SMS numbers if user already has a specific number saved
    document.addEventListener('DOMContentLoaded', function() {
        const savedVal = document.getElementById('sms-channel-picker')?.dataset.saved || 'ghl';
        const hasSavedNumber = savedVal && savedVal !== 'ghl' && savedVal.startsWith('+');

        if (hasSavedNumber) {
            // User has a saved phone number — auto-load to show their selection
            const configTab = document.querySelector('[data-bs-target="#config"]') || document.querySelector('[href="#config"]');
            if (configTab) {
                configTab.addEventListener('shown.bs.tab', loadSmsChannelNumbers);
                configTab.addEventListener('click', function() { setTimeout(loadSmsChannelNumbers, 100); });
            }
            const configPane = document.getElementById('config');
            if (configPane && configPane.classList.contains('active')) {
                loadSmsChannelNumbers();
            }
            setTimeout(loadSmsChannelNumbers, 500);
        }
        // Otherwise, user clicks "Look Up My Numbers" button manually
    });

    // ═══════════════════════════════════════════════════════════════
    // SMS Numbers & A2P Status Panel
    // ═══════════════════════════════════════════════════════════════

    var _smsNumbersCache = [];

    async function smsLoadNumbers() {
        const container = document.getElementById('smsNumbersListContainer');
        if (!container) return;
        container.innerHTML = '<div style="text-align:center;padding:20px;"><i class="fa-solid fa-spinner fa-spin" style="color:' + _tc.cyan + ';font-size:1.2rem;"></i><div style="color:' + _tc.textMut + ';font-size:.78rem;margin-top:6px;">Loading numbers &amp; A2P status...</div></div>';

        try {
            // Fetch numbers and A2P status in parallel
            const [numRes, a2pRes] = await Promise.all([
                fetch('/voice/numbers').then(r => r.ok ? r.json() : { numbers: [] }).catch(() => ({ numbers: [] })),
                fetch('/voice/a2p/status').then(r => r.ok ? r.json() : {}).catch(() => ({}))
            ]);

            _smsNumbersCache = numRes.numbers || [];
            if (!_smsNumbersCache.length) {
                container.innerHTML = '<div style="text-align:center;padding:30px 20px;color:' + _tc.textMut + ';font-size:.82rem;">' +
                    '<i class="fa-solid fa-phone-slash" style="font-size:1.5rem;display:block;margin-bottom:8px;color:' + _tc.textDim + ';"></i>' +
                    'No numbers found on your account.<br>Click <strong style="color:' + _tc.cyan + ';">Buy Number</strong> above to get started.</div>';
                return;
            }

            _renderSmsNumbers(_smsNumbersCache, a2pRes, container);
            _renderSmsA2pSummary(a2pRes);
        } catch (e) {
            console.error('[SmsNumbers] Error:', e);
            container.innerHTML = '<div style="padding:16px;background:' + _tc.redBg + ';border:1px solid ' + _themeColor('rgba(239,68,68,0.2)', 'rgba(220,38,38,0.2)') + ';border-radius:8px;color:' + _tc.red + ';font-size:.82rem;"><i class="fa-solid fa-triangle-exclamation me-1"></i>Network error loading numbers.</div>';
        }
    }

    function _renderSmsNumbers(numbers, a2p, container) {
        const isRegistered = a2p.registered || false;
        const campaignStatus = (a2p.campaign_status || '').toUpperCase();
        const brandStatus = (a2p.brand_status || '').toUpperCase();
        const msgServiceSid = a2p.messaging_service_sid || '';
        // Per-number A2P status: list of PN... SIDs actually in the messaging service
        const registeredSids = a2p.registered_number_sids || [];
        const registeredSet = new Set(registeredSids);

        const hdrStyle = 'padding:8px 10px;background:' + _tc.surface + ';font-weight:700;color:' + _tc.textMut + ';font-size:.75rem;text-transform:uppercase;letter-spacing:.5px;';
        const cellStyle = 'padding:8px 10px;border-top:1px solid ' + _tc.border + ';';

        let html = '<div style="border:1px solid ' + _tc.border + ';border-radius:8px;overflow:visible;">';
        // Header
        html += '<div style="display:grid;grid-template-columns:1fr 80px 100px 140px;gap:0;">';
        html += '<div style="' + hdrStyle + '">Number</div>';
        html += '<div style="' + hdrStyle + 'text-align:center;">SMS</div>';
        html += '<div style="' + hdrStyle + 'text-align:center;">A2P Status</div>';
        html += '<div style="' + hdrStyle + 'text-align:center;">Actions</div>';
        html += '</div>';

        // Rows
        numbers.forEach(n => {
            const hasSms = n.capabilities?.sms;
            const smsIcon = hasSms
                ? '<i class="fa-solid fa-circle-check" style="color:' + _tc.green + ';"></i>'
                : '<i class="fa-solid fa-circle-xmark" style="color:' + _tc.textDim + ';"></i>';

            // Per-number A2P status — check if this number's SID is in the messaging service
            const numberInMs = registeredSet.has(n.sid);
            let a2pBadge;
            if (!hasSms) {
                a2pBadge = '<span style="color:' + _tc.textDim + ';font-size:.75rem;">N/A</span>';
            } else if (isRegistered && numberInMs) {
                a2pBadge = '<span style="background:' + _tc.greenBg + ';color:' + _tc.green + ';padding:2px 8px;border-radius:4px;font-size:.75rem;font-weight:600;"><i class="fa-solid fa-shield-halved me-1"></i>Registered</span>';
            } else if (isRegistered && !numberInMs) {
                a2pBadge = '<span style="background:' + _tc.redBg + ';color:' + _tc.red + ';padding:2px 8px;border-radius:4px;font-size:.75rem;font-weight:600;">Not Registered</span>';
            } else if (brandStatus === 'APPROVED' && !a2p.campaign_sid) {
                a2pBadge = '<span style="background:' + _tc.orangeBg + ';color:' + _tc.orange + ';padding:2px 8px;border-radius:4px;font-size:.75rem;font-weight:600;">Campaign Needed</span>';
            } else if (brandStatus === 'PENDING' || brandStatus === 'IN_REVIEW') {
                a2pBadge = '<span style="background:' + _tc.orangeBg + ';color:' + _tc.orange + ';padding:2px 8px;border-radius:4px;font-size:.75rem;font-weight:600;">Pending</span>';
            } else {
                a2pBadge = '<span style="background:' + _tc.redBg + ';color:' + _tc.red + ';padding:2px 8px;border-radius:4px;font-size:.75rem;font-weight:600;">Not Registered</span>';
            }

            // Actions — per-number
            let actions = '';
            if (hasSms && isRegistered && numberInMs) {
                actions = '<span style="color:' + _tc.green + ';font-size:.75rem;"><i class="fa-solid fa-circle-check me-1"></i>Compliant</span>';
            } else if (hasSms && isRegistered && !numberInMs && msgServiceSid) {
                // A2P is set up but this number isn't in the messaging service — offer to add it
                actions = '<button onclick="smsAddNumberToA2p(\'' + (n.sid || '') + '\')" style="background:rgba(0,255,136,0.1);border:1px solid rgba(0,255,136,0.25);color:#00ff88;border-radius:5px;padding:3px 10px;font-size:.75rem;font-weight:600;cursor:pointer;white-space:nowrap;"><i class="fa-solid fa-plus me-1"></i>Add to A2P</button>';
            } else if (hasSms && !isRegistered) {
                actions = '<button onclick="switchConfigPanel(\'a2p\')" style="background:rgba(167,139,250,0.12);border:1px solid rgba(167,139,250,0.25);color:#a78bfa;border-radius:5px;padding:3px 10px;font-size:.75rem;font-weight:600;cursor:pointer;white-space:nowrap;"><i class="fa-solid fa-certificate me-1"></i>Register A2P</button>';
            }

            const nickname = n.nickname ? '<span style="color:' + _tc.textMut + ';font-size:.75rem;margin-left:4px;">(' + (typeof _esc === 'function' ? _esc(n.nickname) : n.nickname) + ')</span>' : '';
            const primaryBadge = n.is_primary ? '<span style="background:' + _tc.cyanBg + ';color:' + _tc.cyan + ';padding:1px 6px;border-radius:3px;font-size:.75rem;font-weight:700;margin-left:6px;">PRIMARY</span>' : '';

            html += '<div style="display:grid;grid-template-columns:1fr 80px 100px 140px;gap:0;align-items:center;">';
            html += '<div style="' + cellStyle + 'color:' + _tc.text + ';font-size:.8rem;">' + _formatPhone(n.phone) + primaryBadge + nickname + '<br><span style="color:' + _tc.textDim + ';font-size:.75rem;">' + (n.number_type || 'local') + '</span></div>';
            html += '<div style="' + cellStyle + 'text-align:center;">' + smsIcon + '</div>';
            html += '<div style="' + cellStyle + 'text-align:center;">' + a2pBadge + '</div>';
            html += '<div style="' + cellStyle + 'text-align:center;">' + actions + '</div>';
            html += '</div>';
        });
        html += '</div>';

        container.innerHTML = html;
    }

    function _renderSmsA2pSummary(a2p) {
        const el = document.getElementById('smsA2pSummary');
        if (!el) return;

        const isRegistered = a2p.registered || false;
        const brandStatus = (a2p.brand_status || '').toUpperCase();
        const campaignStatus = (a2p.campaign_status || '').toUpperCase();
        const registeredSids = a2p.registered_number_sids || [];
        const totalSms = (_smsNumbersCache || []).filter(n => n.capabilities?.sms).length;
        const registeredCount = registeredSids.length;
        const allRegistered = totalSms > 0 && registeredCount >= totalSms;

        if (isRegistered && (campaignStatus === 'VERIFIED' || campaignStatus === 'APPROVED')) {
            el.style.display = 'block';
            if (allRegistered) {
                // All numbers are in the messaging service
                el.innerHTML = '<div style="padding:14px;background:rgba(74,222,128,0.06);border:1px solid rgba(74,222,128,0.2);border-radius:10px;">' +
                    '<div class="d-flex align-items-center gap-2 mb-1">' +
                        '<i class="fa-solid fa-shield-halved" style="color:#4ade80;"></i>' +
                        '<span style="font-weight:700;color:#4ade80;font-size:.88rem;">A2P 10DLC Registered</span>' +
                        '<span style="font-size:.75rem;color:' + _tc.textMut + ';margin-left:4px;">' + registeredCount + '/' + totalSms + ' numbers</span>' +
                    '</div>' +
                    '<div style="font-size:.75rem;color:' + _tc.textMut + ';line-height:1.6;">' +
                        'All SMS numbers are registered for A2P 10DLC compliance. Messages are delivered at full throughput without carrier filtering.' +
                    '</div></div>';
            } else {
                // A2P approved but some numbers not in the messaging service
                const unregisteredCount = totalSms - registeredCount;
                el.innerHTML = '<div style="padding:14px;background:rgba(255,165,0,0.06);border:1px solid rgba(255,165,0,0.2);border-radius:10px;">' +
                    '<div class="d-flex align-items-center gap-2 mb-1">' +
                        '<i class="fa-solid fa-triangle-exclamation" style="color:#ffa500;"></i>' +
                        '<span style="font-weight:700;color:#ffa500;font-size:.88rem;">' + unregisteredCount + ' Number' + (unregisteredCount !== 1 ? 's' : '') + ' Not A2P Registered</span>' +
                    '</div>' +
                    '<div style="font-size:.75rem;color:' + _tc.textMut + ';line-height:1.6;">' +
                        'Your A2P brand and campaign are approved, but <strong style="color:#ffa500;">' + unregisteredCount + ' of ' + totalSms + '</strong> SMS numbers are not yet added to the messaging service. ' +
                        'Click <strong style="color:#00ff88;">Add to A2P</strong> next to each unregistered number to register it. Unregistered numbers may have messages filtered by carriers.' +
                    '</div></div>';
            }
        } else if (brandStatus && brandStatus !== 'FAILED') {
            el.style.display = 'block';
            el.innerHTML = '<div style="padding:14px;background:rgba(255,165,0,0.06);border:1px solid rgba(255,165,0,0.2);border-radius:10px;">' +
                '<div class="d-flex align-items-center gap-2 mb-1">' +
                    '<i class="fa-solid fa-clock" style="color:#ffa500;"></i>' +
                    '<span style="font-weight:700;color:#ffa500;font-size:.88rem;">A2P Registration In Progress</span>' +
                '</div>' +
                '<div style="font-size:.75rem;color:' + _tc.textMut + ';line-height:1.6;">' +
                    'Brand: ' + brandStatus + (campaignStatus ? ' &bull; Campaign: ' + campaignStatus : '') +
                    '. Check the <strong style="color:#a78bfa;cursor:pointer;" onclick="switchConfigPanel(\'a2p\')">A2P 10DLC</strong> tab for details.' +
                '</div></div>';
        } else {
            el.style.display = 'block';
            el.innerHTML = '<div style="padding:14px;background:rgba(239,68,68,0.04);border:1px solid rgba(239,68,68,0.12);border-radius:10px;">' +
                '<div class="d-flex align-items-center gap-2 mb-1">' +
                    '<i class="fa-solid fa-triangle-exclamation" style="color:#ef4444;"></i>' +
                    '<span style="font-weight:700;color:#ef4444;font-size:.88rem;">A2P Registration Required</span>' +
                '</div>' +
                '<div style="font-size:.75rem;color:' + _tc.textMut + ';line-height:1.6;">' +
                    'Your numbers are not registered for A2P 10DLC. Without registration, SMS messages may be filtered or blocked by carriers. ' +
                    '<strong style="color:#a78bfa;cursor:pointer;" onclick="switchConfigPanel(\'a2p\')">Register now &rarr;</strong>' +
                '</div></div>';
        }
    }

    async function smsSearchNumbers() {
        const numberType = document.getElementById('smsBuyNumberType').value;
        const area = document.getElementById('smsBuyAreaCode').value.trim();
        const state = document.getElementById('smsBuyState').value.trim();
        const city = document.getElementById('smsBuyCity').value.trim();
        const zip = document.getElementById('smsBuyZip').value.trim();
        const contains = document.getElementById('smsBuyContains').value.trim();
        const list = document.getElementById('smsAvailableNumbersList');
        list.innerHTML = '<div style="text-align:center;padding:8px;"><i class="fa-solid fa-spinner fa-spin" style="color:' + _tc.cyan + ';"></i></div>';
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
            if (!r.ok) { list.innerHTML = '<div style="color:' + _tc.red + ';padding:4px;">' + (d.error || 'Failed') + '</div>'; return; }
            const nums = d.numbers || [];
            if (!nums.length) { list.innerHTML = '<div style="color:' + _tc.textMut + ';padding:4px;font-size:.75rem;">No numbers found. Try different filters.</div>'; return; }
            const priceMap = { local: '$0.90', toll_free: '$2.15', mobile: '$0.90' };
            const monthlyPrice = priceMap[numberType] || '$0.90';
            list.innerHTML = nums.map(n => {
                const loc = [n.locality, n.region].filter(Boolean).join(', ');
                const caps = [];
                if (n.capabilities && n.capabilities.voice) caps.push('Voice');
                if (n.capabilities && n.capabilities.sms) caps.push('SMS');
                if (n.capabilities && n.capabilities.mms) caps.push('MMS');
                return '<div style="display:flex;align-items:center;justify-content:space-between;padding:8px 4px;border-bottom:1px solid ' + _tc.border + ';font-size:.78rem;">' +
                    '<div style="flex:1;min-width:0;">' +
                        '<span style="color:' + _tc.text + ';font-weight:600;">' + _formatPhone(n.phone) + '</span>' +
                        (loc ? '<span style="color:' + _tc.textFaint + ';font-size:.75rem;margin-left:6px;">' + loc + '</span>' : '') +
                        '<div style="margin-top:2px;">' + caps.map(c => '<span style="background:' + _tc.cyanBg + ';color:' + _tc.cyan + ';padding:1px 6px;border-radius:4px;font-size:.75rem;margin-right:3px;">' + c + '</span>').join('') + '</div>' +
                    '</div>' +
                    '<div style="display:flex;align-items:center;gap:8px;flex-shrink:0;">' +
                        '<span style="color:' + _tc.accent + ';font-size:.75rem;font-weight:600;">' + monthlyPrice + '/mo</span>' +
                        '<button onclick="smsBuyNumber(\'' + n.phone + '\')" style="background:linear-gradient(135deg,#00d9ff,#0099cc);border:none;color:#000;border-radius:4px;padding:3px 10px;font-size:.75rem;font-weight:700;cursor:pointer;">Buy</button>' +
                    '</div>' +
                '</div>';
            }).join('');
        } catch(e) { list.innerHTML = '<div style="color:' + _tc.red + ';">Network error</div>'; }
    }

    async function smsBuyNumber(phone) {
        if (!confirm('Purchase ' + phone + '?\nThis will be added to your account.')) return;
        try {
            const r = await fetch('/voice/numbers/buy', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ phone_number: phone }) });
            const d = await r.json();
            if (!r.ok) { alert(d.error || 'Failed'); return; }
            if (typeof _showDashToast === 'function') _showDashToast(true, 'Purchased: ' + d.phone);
            else alert('Purchased: ' + d.phone);
            const buyPanel = document.getElementById('smsBuyPanel'); if (buyPanel) buyPanel.style.display = 'none';
            smsLoadNumbers();
        } catch(e) { alert('Network error'); }
    }

    async function smsAddNumberToA2p(phoneSid) {
        if (!phoneSid) return;
        const btn = event?.target?.closest('button');
        if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-1"></i>Adding...'; }
        try {
            const r = await fetch('/voice/a2p/add-number', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ phone_number_sid: phoneSid })
            });
            const d = await r.json();
            if (!r.ok) {
                if (typeof _showDashToast === 'function') _showDashToast(false, d.error || 'Failed to add number');
                else alert(d.error || 'Failed');
                if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-plus me-1"></i>Add to A2P'; }
                return;
            }
            if (typeof _showDashToast === 'function') _showDashToast(true, 'Number added to A2P messaging service');
            smsLoadNumbers();
        } catch(e) {
            if (typeof _showDashToast === 'function') _showDashToast(false, 'Network error');
            if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-plus me-1"></i>Add to A2P'; }
        }
    }
