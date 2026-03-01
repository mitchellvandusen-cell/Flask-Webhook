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

            // GHL numbers first
            ghlNums.forEach(n => {
                const num = n.number;
                if (seen.has(num)) return;
                seen.add(num);
                allNums.push({
                    number: num,
                    label: n.name || '',
                    smsCapable: n.capabilities?.sms !== false,
                });
            });

            // Provisioned numbers (skip duplicates)
            provNums.forEach(n => {
                const num = n.phone;
                if (seen.has(num)) return;
                seen.add(num);
                allNums.push({
                    number: num,
                    label: n.nickname || '',
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

                const desc = num.label && num.label !== num.number ? num.label : 'Send from this number';

                el.innerHTML = `
                    <input type="radio" name="sms_send_via" value="${num.number}"
                        ${isChecked ? 'checked' : ''} ${isDisabled ? 'disabled' : ''}
                        style="accent-color:var(--accent);width:16px;height:16px;flex-shrink:0;">
                    <i class="fa-solid fa-phone" style="color:var(--accent);font-size:1rem;flex-shrink:0;width:22px;text-align:center;"></i>
                    <div style="flex:1;min-width:0;">
                        <div style="font-size:0.85rem;font-weight:600;color:#fff;">${_formatPhone(num.number)}</div>
                        <div style="font-size:0.72rem;color:#888;">${desc}${isDisabled ? ' (no SMS capability)' : ''}</div>
                    </div>
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
