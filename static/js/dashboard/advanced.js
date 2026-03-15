    // ADVANCED SETTINGS
    // ═══════════════════════════════════════
    const profLabels = ['Casual', 'Relaxed', 'Balanced', 'Polished', 'Formal', 'Ultra Professional'];
    function updateProfLabel(val) {
        document.getElementById('profLevelLabel').textContent = profLabels[val] || 'Balanced';
    }
    updateProfLabel(document.getElementById('professionalism_level')?.value || 0);

    function selectResponseLength(val) {
        document.querySelectorAll('.resp-len-btn').forEach(btn => {
            const isActive = btn.dataset.value === val;
            btn.style.border = isActive ? '1px solid ' + _tc.accent : '1px solid ' + _tc.border;
            btn.style.background = isActive ? _tc.accentBg : _tc.surface;
            btn.style.color = isActive ? _tc.accent : _tc.textMut;
            if (isActive) btn.classList.add('active'); else btn.classList.remove('active');
        });
    }

    function addOutboundMsg(prefill) {
        const container = document.getElementById('outboundContainer');
        const idx = container.children.length + 1;
        const row = document.createElement('div');
        row.className = 'outbound-msg-row';
        row.style = 'display: flex; gap: 8px; margin-bottom: 8px;';
        row.innerHTML = `
            <span class="adv-outbound-num">${idx}.</span>
            <textarea class="outbound-msg-input adv-outbound-textarea" rows="2" placeholder="Type your outbound message...">${prefill || ''}</textarea>
            <button type="button" onclick="removeOutboundMsg(this)" class="adv-remove-btn">
                <i class="fa-solid fa-xmark"></i>
            </button>`;
        container.appendChild(row);
        renumberOutbound();
    }

    function removeOutboundMsg(btn) {
        btn.closest('.outbound-msg-row').remove();
        renumberOutbound();
    }

    function renumberOutbound() {
        document.querySelectorAll('#outboundContainer .outbound-msg-row').forEach((row, i) => {
            row.querySelector('span').textContent = (i + 1) + '.';
        });
    }

    function collectSettings() {
        const msgs = [];
        document.querySelectorAll('.outbound-msg-input').forEach(ta => {
            const v = ta.value.trim();
            if (v) msgs.push(v);
        });
        const activeLen = document.querySelector('.resp-len-btn.active');
        return {
            humor_enabled: document.getElementById('humor_enabled').checked,
            professionalism_level: parseInt(document.getElementById('professionalism_level').value),
            custom_behavior: document.getElementById('custom_behavior').value.trim(),
            outbound_messages: msgs,
            auto_emoji: document.getElementById('auto_emoji').checked,
            after_hours_enabled: document.getElementById('after_hours_enabled').checked,
            after_hours_start: document.getElementById('after_hours_start').value,
            after_hours_end: document.getElementById('after_hours_end').value,
            response_length: activeLen ? activeLen.dataset.value : 'balanced',
            booking_confirmation: document.getElementById('booking_confirmation').checked,
            objection_persistence: parseInt(document.getElementById('objection_persistence').value),
            lead_reengagement: document.getElementById('lead_reengagement').checked,
            conversation_memory: document.getElementById('conversation_memory').checked,
            speed_to_lead: document.getElementById('speed_to_lead').checked,
            multi_language: document.getElementById('multi_language').checked,
        };
    }

    function saveAdvancedSettings() {
        const btn = document.getElementById('saveAdvancedBtn');
        const status = document.getElementById('settingsStatus');
        btn.disabled = true;
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-2"></i> Saving...';
        fetch('/api/bot-settings', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({settings: collectSettings()})
        })
        .then(r => r.json())
        .then(data => {
            btn.disabled = false;
            btn.innerHTML = '<i class="fa-solid fa-floppy-disk me-2"></i> Save Settings';
            if (data.status === 'success') {
                _showDashToast(true, 'Settings saved!');
                status.textContent = 'Settings saved!';
                status.style.opacity = '1';
                setTimeout(() => { status.style.opacity = '0'; }, 3000);
            } else {
                _showDashToast(false, 'Failed to save: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(() => {
            btn.disabled = false;
            btn.innerHTML = '<i class="fa-solid fa-floppy-disk me-2"></i> Save Settings';
            _showDashToast(false, 'Network error saving settings.');
        });
    }

    function resetAdvancedSettings() {
        if (!confirm('Reset all advanced settings to defaults? This cannot be undone.')) return;
        document.getElementById('professionalism_level').value = 0; updateProfLabel(0);
        document.getElementById('humor_enabled').checked = true;
        document.getElementById('auto_emoji').checked = true;
        document.getElementById('after_hours_enabled').checked = false;
        document.getElementById('afterHoursRange').style.display = 'none';
        document.getElementById('after_hours_start').value = '18:00';
        document.getElementById('after_hours_end').value = '09:00';
        selectResponseLength('balanced');
        document.getElementById('booking_confirmation').checked = true;
        document.getElementById('objection_persistence').value = 3;
        document.getElementById('persistenceLabel').textContent = '3 angles';
        document.getElementById('lead_reengagement').checked = true;
        document.getElementById('conversation_memory').checked = true;
        document.getElementById('speed_to_lead').checked = true;
        document.getElementById('multi_language').checked = false;
        document.getElementById('custom_behavior').value = '';
        document.getElementById('outboundContainer').innerHTML = '';
        saveAdvancedSettings();
    }

    // ═══════════════════════════════════════════
    // API KEY MANAGEMENT
    // ═══════════════════════════════════════════
