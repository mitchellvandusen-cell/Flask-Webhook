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
                text.style.color = '#ef4444';
                setTimeout(() => {
                    overlay.classList.remove('active');
                    btn.disabled = false;
                    text.style.color = '#fff';
                }, 2500);
            }
        })
        .catch(err => {
            spinner.style.display = 'none';
            text.textContent = 'Network error. Please try again.';
            text.style.color = '#ef4444';
            setTimeout(() => {
                overlay.classList.remove('active');
                btn.disabled = false;
                text.style.color = '#fff';
            }, 2500);
        });
    }

    // ── SMS Channel Picker: Load available Twilio numbers ──────────────────
    function loadSmsChannelNumbers() {
        const container = document.getElementById('sms-twilio-numbers');
        const loading = document.getElementById('sms-channel-loading');
        const noNumbers = document.getElementById('sms-no-numbers');
        if (!container) return;

        loading.style.display = '';
        noNumbers.style.display = 'none';
        container.innerHTML = '';

        // Current selection
        const currentVal = document.querySelector('input[name="sms_send_via"]:checked')?.value || 'ghl';

        // Fetch IGB Twilio numbers
        const twilioFetch = fetch('/voice/numbers').then(r => r.ok ? r.json() : {numbers: []}).catch(() => ({numbers: []}));
        // Fetch GHL numbers
        const ghlFetch = fetch('/api/ghl-phone-numbers').then(r => r.ok ? r.json() : {numbers: []}).catch(() => ({numbers: []}));

        Promise.all([twilioFetch, ghlFetch]).then(([twilioData, ghlData]) => {
            loading.style.display = 'none';

            const twilioNums = (twilioData.numbers || []).filter(n => n.phone);
            const ghlNums = (ghlData.numbers || []).filter(n => n.number);
            const allNums = [];

            // IGB Twilio numbers (robot icon)
            twilioNums.forEach(n => {
                allNums.push({
                    number: n.phone,
                    label: n.nickname || n.phone,
                    source: 'igb',
                    icon: 'fa-solid fa-robot',
                    iconColor: 'var(--accent)',
                    badge: 'IGB',
                    badgeColor: 'rgba(0,255,136,0.12)',
                    badgeText: 'var(--accent)',
                    smsCapable: n.capabilities?.sms !== false,
                });
            });

            // GHL numbers (LeadConnector logo)
            ghlNums.forEach(n => {
                // Skip if same number already in Twilio list
                if (allNums.some(a => a.number === n.number)) return;
                allNums.push({
                    number: n.number,
                    label: n.name || n.number,
                    source: 'ghl',
                    icon: null, // uses logo image
                    logoUrl: 'https://images.leadconnectorhq.com/image/f_webp/q_80/r_1200/u_https://assets.cdn.filesafe.space/WDJNKXKiQj2XODO7jYzV/media/66be66f4b5e9ba05e7a7c191.png',
                    badge: 'GHL',
                    badgeColor: 'rgba(59,130,246,0.12)',
                    badgeText: '#60a5fa',
                    smsCapable: n.capabilities?.sms !== false,
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
                el.style.cssText = 'display:flex;align-items:center;gap:12px;padding:12px 16px;background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.08);border-radius:10px;cursor:pointer;transition:all .15s;' + (isChecked ? 'border-color:rgba(0,255,136,0.3);background:rgba(0,255,136,0.04);' : '') + (isDisabled ? 'opacity:0.4;cursor:not-allowed;' : '');

                const iconHtml = num.icon
                    ? `<i class="${num.icon}" style="color:${num.iconColor};font-size:1.1rem;flex-shrink:0;width:22px;text-align:center;"></i>`
                    : `<img src="${num.logoUrl}" alt="${num.source}" style="width:22px;height:22px;border-radius:4px;flex-shrink:0;" onerror="this.style.display='none'">`;

                el.innerHTML = `
                    <input type="radio" name="sms_send_via" value="${num.number}"
                        ${isChecked ? 'checked' : ''} ${isDisabled ? 'disabled' : ''}
                        style="accent-color:var(--accent);width:16px;height:16px;flex-shrink:0;">
                    ${iconHtml}
                    <div style="flex:1;min-width:0;">
                        <div style="font-size:0.85rem;font-weight:600;color:#fff;">${_formatPhone(num.number)}</div>
                        <div style="font-size:0.72rem;color:#888;">${num.label !== num.number ? num.label + ' — ' : ''}Direct Twilio SMS${isDisabled ? ' (no SMS capability)' : ''}</div>
                    </div>
                    <span style="font-size:0.65rem;background:${num.badgeColor};color:${num.badgeText};padding:2px 8px;border-radius:6px;font-weight:600;flex-shrink:0;">${num.badge}</span>
                `;

                // Highlight selected option
                el.querySelector('input').addEventListener('change', function() {
                    document.querySelectorAll('.sms-channel-option').forEach(opt => {
                        opt.style.borderColor = 'rgba(255,255,255,0.08)';
                        opt.style.background = 'rgba(255,255,255,0.03)';
                    });
                    if (this.checked) {
                        el.style.borderColor = 'rgba(0,255,136,0.3)';
                        el.style.background = 'rgba(0,255,136,0.04)';
                    }
                });

                container.appendChild(el);
            });

            // Also highlight the GHL default option if selected
            const ghlDefault = document.querySelector('input[name="sms_send_via"][value="ghl"]');
            if (ghlDefault) {
                ghlDefault.addEventListener('change', function() {
                    document.querySelectorAll('.sms-channel-option').forEach(opt => {
                        opt.style.borderColor = 'rgba(255,255,255,0.08)';
                        opt.style.background = 'rgba(255,255,255,0.03)';
                    });
                    if (this.checked) {
                        this.closest('.sms-channel-option').style.borderColor = 'rgba(0,255,136,0.3)';
                        this.closest('.sms-channel-option').style.background = 'rgba(0,255,136,0.04)';
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

    // Auto-load SMS numbers when config tab opens
    document.addEventListener('DOMContentLoaded', function() {
        // Load when switching to config tab
        const configTab = document.querySelector('[data-bs-target="#config"]') || document.querySelector('[href="#config"]');
        if (configTab) {
            configTab.addEventListener('shown.bs.tab', loadSmsChannelNumbers);
            configTab.addEventListener('click', function() { setTimeout(loadSmsChannelNumbers, 100); });
        }
        // Also load if config tab is already visible
        const configPane = document.getElementById('config');
        if (configPane && configPane.classList.contains('active')) {
            loadSmsChannelNumbers();
        }
        // Load when identity panel is switched to (it's default, so load immediately)
        setTimeout(loadSmsChannelNumbers, 500);
    });
