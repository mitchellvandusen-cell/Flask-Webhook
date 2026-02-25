        // ===== VOICE TAB =====
        function clearCallScript() {
            document.getElementById('voiceCallScript').value = '';
        }

        // Voice preview
        let _voicePreviewPlayer = null;
        async function previewVoice() {
            const voice = document.getElementById('voiceSelection').value;
            const btn = document.getElementById('voicePreviewBtn');
            const status = document.getElementById('voicePreviewStatus');

            // If already playing, stop
            if (_voicePreviewPlayer) {
                _voicePreviewPlayer.pause();
                _voicePreviewPlayer = null;
                btn.innerHTML = '<i class="fa-solid fa-volume-high me-1"></i> Preview';
                status.style.display = 'none';
                return;
            }

            btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-1"></i> Loading...';
            btn.disabled = true;
            status.textContent = 'Generating voice sample...';
            status.style.display = 'block';
            status.style.color = '#3b82f6';

            try {
                const r = await fetch('/voice/preview/' + voice);
                if (!r.ok) {
                    const d = await r.json().catch(() => ({}));
                    throw new Error(d.error || 'Failed to generate preview');
                }
                const blob = await r.blob();
                const url = URL.createObjectURL(blob);
                _voicePreviewPlayer = new Audio(url);
                _voicePreviewPlayer.onended = function() {
                    _voicePreviewPlayer = null;
                    btn.innerHTML = '<i class="fa-solid fa-volume-high me-1"></i> Preview';
                    status.style.display = 'none';
                };
                _voicePreviewPlayer.play();
                btn.innerHTML = '<i class="fa-solid fa-stop me-1"></i> Stop';
                status.textContent = 'Playing ' + voice.charAt(0).toUpperCase() + voice.slice(1) + '...';
                status.style.color = 'var(--accent)';
            } catch(e) {
                status.textContent = e.message;
                status.style.color = '#ef4444';
                btn.innerHTML = '<i class="fa-solid fa-volume-high me-1"></i> Preview';
            } finally {
                btn.disabled = false;
            }
        }

        // ── Voice sub-tab switching (backward compat — delegates to new column menu) ──
        function switchVoiceSubtab(name) {
            switchVoicePanel(name);
        }

        // ── One-click voice service activation (white-label) ──
        async function activateVoiceService() {
            const btn = document.getElementById('activateVoiceBtn');
            const res = document.getElementById('activateVoiceResult');
            btn.disabled = true;
            btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-2"></i>Activating…';
            res.style.display = 'block';
            res.innerHTML = '<span style="color:#888;">Creating your voice account…</span>';
            try {
                const r = await fetch('/voice/automate-setup', {
                    method: 'POST',
                    headers: {'Content-Type':'application/json'},
                    body: JSON.stringify({})
                });
                const d = await r.json();
                if (d.status === 'success') {
                    res.innerHTML = '<span style="color:#00ff88;"><i class="fa-solid fa-circle-check me-1"></i>' + d.message + '</span>';
                    // Flash the status badges green without a reload
                    document.querySelectorAll('#vstab-panel-settings .fa-circle-xmark').forEach(el => {
                        el.className = 'fa-solid fa-circle-check';
                        el.style.color = '#00ff88';
                        if (el.nextElementSibling) el.nextElementSibling.style.color = '#ccc';
                    });
                    // Refresh the Numbers / Trust Hub lists so they populate immediately
                    if (typeof loadTrustHubNumbers === 'function') loadTrustHubNumbers();
                    // Reload page after short delay to show new provisioned state
                    setTimeout(() => location.reload(), 2000);
                } else {
                    res.innerHTML = '<span style="color:#ef4444;"><i class="fa-solid fa-times-circle me-1"></i>' + (d.error || 'Activation failed') + '</span>';
                }
            } catch(e) {
                res.innerHTML = '<span style="color:#ef4444;">Network error — check your connection and try again</span>';
            }
            btn.disabled = false;
            btn.innerHTML = '<i class="fa-solid fa-wand-magic-sparkles me-2"></i>Activate Voice';
        }

        function saveVoiceConfig() {
            const resultDiv = document.getElementById('voiceTestResult');
            resultDiv.style.display = 'block';
            resultDiv.innerHTML = '<span style="color:#3b82f6;"><i class="fa-solid fa-spinner fa-spin me-1"></i> Saving voice settings...</span>';

            const config = {
                enabled: document.getElementById('voiceEnabled').checked,
                voice: document.getElementById('voiceSelection').value,
                voice_bot_name: document.getElementById('voiceBotName').value.trim(),
                voice_instructions: document.getElementById('voiceInstructions').value.trim(),
                call_script: document.getElementById('voiceCallScript').value.trim(),
                // Dialer settings
                dial_attempts: parseInt(document.getElementById('voiceDialAttempts')?.value || '2'),
                auto_record: document.getElementById('voiceAutoRecord')?.checked ?? true,
                auto_transcribe: document.getElementById('voiceAutoTranscribe')?.checked ?? false,
                local_presence: document.getElementById('voiceLocalPresence')?.checked ?? false,
                transfer_number: document.getElementById('voiceTransferNumber')?.value?.trim() || '',
                voicemail_drop: document.getElementById('voiceVoicemailDrop')?.checked ?? false,
            };

            _fetchRetry('/api/voice-config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(config)
            }, { retries: 2, timeout: 15000, label: 'save-config' }).then(r => r.json()).then(d => {
                if (d.status === 'success') {
                    resultDiv.innerHTML = '<span style="color:var(--accent);"><i class="fa-solid fa-check-circle me-1"></i> Voice settings saved successfully!</span>';
                    _showDashToast(true, 'Voice settings saved!');
                    // Live-update dialer max attempts so queue uses new value immediately
                    dialerMaxAttempts = config.dial_attempts || 2;
                    // Update badge
                    const badge = document.getElementById('voiceStatusBadge');
                    if (config.enabled) {
                        badge.style.background = 'rgba(0,255,136,0.15)';
                        badge.style.color = '#00ff88';
                        badge.style.border = '1px solid rgba(0,255,136,0.3)';
                        badge.innerHTML = '<i class="fa-solid fa-circle me-1" style="font-size:0.5rem; vertical-align:middle;"></i> Active';
                    } else {
                        badge.style.background = 'rgba(255,255,255,0.06)';
                        badge.style.color = '#666';
                        badge.style.border = '1px solid rgba(255,255,255,0.08)';
                        badge.innerHTML = 'Inactive';
                    }
                } else {
                    resultDiv.innerHTML = '<span style="color:#ef4444;"><i class="fa-solid fa-times-circle me-1"></i> ' + (d.error || 'Failed to save') + '</span>';
                    _showDashToast(false, d.error || 'Failed to save voice settings');
                }
                setTimeout(() => { resultDiv.style.display = 'none'; }, 5000);
            }).catch(e => {
                console.error('[Settings] Save failed:', e);
                resultDiv.innerHTML = '<span style="color:#ef4444;">Network error saving voice config — please try again</span>';
                _showDashToast(false, 'Network error saving voice settings');
            });
        }

        function testVoiceConnection() {
            const resultDiv = document.getElementById('voiceTestResult');
            resultDiv.style.display = 'block';
            resultDiv.innerHTML = '<span style="color:#3b82f6;"><i class="fa-solid fa-spinner fa-spin me-1"></i> Testing connections...</span>';

            fetch('/voice/test', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ location_id: (window.DASHBOARD_BOOT && window.DASHBOARD_BOOT.locationId) || '' })
            }).then(r => r.json()).then(d => {
                let html = '<div style="font-size:0.85rem;">';
                html += '<div style="margin-bottom:6px;"><strong style="color:#ccc;">XAI Realtime API:</strong> ';
                html += d.xai ? '<span style="color:var(--accent);"><i class="fa-solid fa-check me-1"></i>Connected</span>' : '<span style="color:#ef4444;"><i class="fa-solid fa-times me-1"></i>Failed</span>';
                html += '</div>';
                html += '<div style="margin-bottom:6px;"><strong style="color:#ccc;">Voice Service:</strong> ';
                html += d.voice_service ? '<span style="color:var(--accent);"><i class="fa-solid fa-check me-1"></i>Active</span>' : '<span style="color:#ef4444;"><i class="fa-solid fa-times me-1"></i>Not provisioned — click Activate Voice</span>';
                html += '</div>';
                if (d.errors && d.errors.length > 0) {
                    html += '<div style="color:#ffa500; margin-top:8px; font-size:0.8rem;"><i class="fa-solid fa-triangle-exclamation me-1"></i>' + d.errors.join('<br>') + '</div>';
                }
                html += '</div>';
                resultDiv.innerHTML = html;
                setTimeout(() => { resultDiv.style.display = 'none'; }, 10000);
            }).catch(e => {
                resultDiv.innerHTML = '<span style="color:#ef4444;">Network error</span>';
            });
        }

        // configureVoiceNumber() removed — routing is auto-provisioned.

