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
                // Enterprise dialer tuning
                ring_timeout: parseInt(document.getElementById('voiceRingTimeout')?.value || '45'),
                pause_between_calls: parseInt(document.getElementById('voicePauseBetween')?.value || '1'),
                use_amd: document.getElementById('voiceAMD')?.checked ?? false,
                max_call_duration: parseInt(document.getElementById('voiceMaxCallDuration')?.value || '0'),
                retry_delay: parseInt(document.getElementById('voiceRetryDelay')?.value || '2'),
                auto_callback: document.getElementById('voiceAutoCallback')?.checked ?? false,
                // Multi-line / predictive dialer settings
                max_lines_setting: parseInt(document.getElementById('voiceMaxLinesSetting')?.value || '3'),
                wrap_up_time: parseInt(document.getElementById('voiceWrapUpTime')?.value || '15'),
                require_disposition: document.getElementById('voiceRequireDisposition')?.checked ?? true,
                calling_hours_start: document.getElementById('voiceCallingHoursStart')?.value || '08:00',
                calling_hours_end: document.getElementById('voiceCallingHoursEnd')?.value || '21:00',
                same_number_cooldown_hours: parseInt(document.getElementById('voiceCooldownHours')?.value || '4'),
                same_contact_daily_max: parseInt(document.getElementById('voiceDailyMaxContact')?.value || '3'),
                on_machine_action: document.getElementById('voiceOnMachineAction')?.value || 'hangup',
                auto_disposition_no_answer: document.getElementById('voiceAutoDispNoAnswer')?.checked ?? true,
                auto_disposition_voicemail: document.getElementById('voiceAutoDispVoicemail')?.checked ?? true,
                max_abandon_rate_pct: parseFloat(document.getElementById('voiceMaxAbandonRate')?.value || '3'),
                quiet_hours_enabled: document.getElementById('voiceQuietHoursEnabled')?.checked ?? false,
                bypass_calling_hours: document.getElementById('voiceBypassCallingHours')?.checked ?? false,
                // Dossier display settings
                show_ai_summary: document.getElementById('voiceShowAiSummary')?.checked ?? true,
                show_known_facts: document.getElementById('voiceShowKnownFacts')?.checked ?? true,
            };

            _fetchRetry('/api/voice-config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(config)
            }, { retries: 2, timeout: 15000, label: 'save-config' }).then(r => r.json()).then(d => {
                if (d.status === 'success') {
                    resultDiv.innerHTML = '<span style="color:var(--accent);"><i class="fa-solid fa-check-circle me-1"></i> Voice settings saved successfully!</span>';
                    _showDashToast(true, 'Voice settings saved!');
                    // Live-update dialer settings so queue uses new values immediately
                    dialerMaxAttempts = config.dial_attempts || 2;
                    if (typeof _dialerRingTimeout !== 'undefined') _dialerRingTimeout = (config.ring_timeout || 45) * 1000;
                    if (typeof _dialerPauseBetween !== 'undefined') _dialerPauseBetween = (config.pause_between_calls ?? 1) * 1000;
                    if (typeof _dialerRetryDelay !== 'undefined') _dialerRetryDelay = (config.retry_delay || 2) * 1000;
                    if (typeof _dialerMaxCallDuration !== 'undefined') _dialerMaxCallDuration = (config.max_call_duration || 0) * 60 * 1000;
                    if (typeof _dialerAutoCallback !== 'undefined') _dialerAutoCallback = config.auto_callback || false;
                    // Live-update multi-line settings
                    if (typeof _dialerWrapUpTime !== 'undefined') _dialerWrapUpTime = (config.wrap_up_time ?? 15) * 1000;
                    if (typeof _dialerRequireDisposition !== 'undefined') _dialerRequireDisposition = config.require_disposition ?? true;
                    if (typeof _dialerAutoDispNoAnswer !== 'undefined') _dialerAutoDispNoAnswer = config.auto_disposition_no_answer ?? true;
                    if (typeof _dialerAutoDispVoicemail !== 'undefined') _dialerAutoDispVoicemail = config.auto_disposition_voicemail ?? true;
                    if (typeof _dialerOnMachineAction !== 'undefined') _dialerOnMachineAction = config.on_machine_action || 'hangup';
                    if (typeof _multiLineMaxLines !== 'undefined' && typeof _multiLineEnabled !== 'undefined' && _multiLineEnabled) {
                        _multiLineMaxLines = Math.min(4, Math.max(1, config.max_lines_setting || 3));
                    }
                    // Live-update dossier toggles
                    if (window.DASHBOARD_BOOT) {
                        window.DASHBOARD_BOOT.showAiSummary = config.show_ai_summary;
                        window.DASHBOARD_BOOT.showKnownFacts = config.show_known_facts;
                    }
                    // Update badge
                    const badge = document.getElementById('voiceStatusBadge');
                    if (config.enabled) {
                        badge.style.background = 'rgba(0,255,136,0.15)';
                        badge.style.color = '#00ff88';
                        badge.style.border = '1px solid rgba(0,255,136,0.3)';
                        badge.innerHTML = '<i class="fa-solid fa-circle me-1" style="font-size:0.75rem; vertical-align:middle;"></i> Active';
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

        // ── Training integration code ──

        function loadTrainingStatus() {
            fetch('/api/training/status')
                .then(r => r.json())
                .then(data => {
                    if (data.has_token) {
                        document.getElementById('trainingNoToken').style.display = 'none';
                        document.getElementById('trainingHasToken').style.display = 'block';
                        document.getElementById('trainingTokenDisplay').value = data.training_token;
                        if (data.created_at) {
                            const d = new Date(data.created_at);
                            document.getElementById('trainingTokenCreated').textContent = 'Created ' + d.toLocaleDateString() + ' at ' + d.toLocaleTimeString();
                        }
                    } else {
                        document.getElementById('trainingNoToken').style.display = 'block';
                        document.getElementById('trainingHasToken').style.display = 'none';
                    }
                })
                .catch(() => {});
        }

        function generateTrainingCode() {
            const btn = document.getElementById('btnGenerateTraining');
            btn.disabled = true;
            btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-2"></i>Generating...';
            const statusMsg = document.getElementById('trainingStatusMsg');

            fetch('/api/training/generate-code', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'}
            })
            .then(r => r.json())
            .then(data => {
                if (data.training_token) {
                    document.getElementById('trainingNoToken').style.display = 'none';
                    document.getElementById('trainingHasToken').style.display = 'block';
                    document.getElementById('trainingTokenDisplay').value = data.training_token;
                    const now = new Date();
                    document.getElementById('trainingTokenCreated').textContent = 'Created ' + now.toLocaleDateString() + ' at ' + now.toLocaleTimeString();
                    statusMsg.style.display = 'block';
                    statusMsg.style.background = 'rgba(0,255,136,0.06)';
                    statusMsg.style.border = '1px solid rgba(0,255,136,0.15)';
                    statusMsg.style.color = '#00ff88';
                    statusMsg.innerHTML = '<i class="fa-solid fa-check-circle me-1"></i> Training code generated! Copy it and paste into InsuranceGrokBot Training.';
                    _showDashToast(true, 'Training code generated!');
                    setTimeout(() => { statusMsg.style.display = 'none'; }, 6000);
                } else {
                    statusMsg.style.display = 'block';
                    statusMsg.style.background = 'rgba(239,68,68,0.06)';
                    statusMsg.style.border = '1px solid rgba(239,68,68,0.15)';
                    statusMsg.style.color = '#ef4444';
                    statusMsg.innerHTML = '<i class="fa-solid fa-times-circle me-1"></i> ' + (data.error || 'Failed to generate code');
                    _showDashToast(false, data.error || 'Failed to generate training code');
                }
            })
            .catch(() => {
                statusMsg.style.display = 'block';
                statusMsg.style.background = 'rgba(239,68,68,0.06)';
                statusMsg.style.border = '1px solid rgba(239,68,68,0.15)';
                statusMsg.style.color = '#ef4444';
                statusMsg.innerHTML = '<i class="fa-solid fa-times-circle me-1"></i> Network error — please try again';
                _showDashToast(false, 'Network error generating training code');
            })
            .finally(() => {
                btn.disabled = false;
                btn.innerHTML = '<i class="fa-solid fa-wand-magic-sparkles me-2"></i>Generate Training Code';
            });
        }

        function regenerateTrainingCode() {
            if (!confirm('Regenerate your training code? The old code will stop working in InsuranceGrokBot Training.')) return;
            generateTrainingCode();
        }

        function revokeTrainingCode() {
            if (!confirm('Revoke your training code? InsuranceGrokBot Training will no longer be able to access your call recordings.')) return;
            const statusMsg = document.getElementById('trainingStatusMsg');

            fetch('/api/training/revoke-code', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'}
            })
            .then(r => r.json())
            .then(data => {
                if (data.status === 'success') {
                    document.getElementById('trainingNoToken').style.display = 'block';
                    document.getElementById('trainingHasToken').style.display = 'none';
                    statusMsg.style.display = 'block';
                    statusMsg.style.background = 'rgba(239,68,68,0.06)';
                    statusMsg.style.border = '1px solid rgba(239,68,68,0.15)';
                    statusMsg.style.color = '#ef4444';
                    statusMsg.innerHTML = '<i class="fa-solid fa-check-circle me-1"></i> Training code revoked.';
                    _showDashToast(true, 'Training code revoked');
                    setTimeout(() => { statusMsg.style.display = 'none'; }, 5000);
                } else {
                    _showDashToast(false, data.error || 'Failed to revoke training code');
                }
            })
            .catch(() => {
                _showDashToast(false, 'Network error revoking training code');
            });
        }

        function copyTrainingCode() {
            const input = document.getElementById('trainingTokenDisplay');
            const btn = document.getElementById('btnCopyTraining');
            navigator.clipboard.writeText(input.value).then(() => {
                btn.innerHTML = '<i class="fa-solid fa-check" style="color:#00ff88;"></i>';
                _showDashToast(true, 'Training code copied to clipboard!');
                setTimeout(() => {
                    btn.innerHTML = '<i class="fa-regular fa-copy"></i>';
                }, 2000);
            }).catch(() => {
                // Fallback: select the input
                input.select();
                document.execCommand('copy');
                btn.innerHTML = '<i class="fa-solid fa-check" style="color:#00ff88;"></i>';
                setTimeout(() => {
                    btn.innerHTML = '<i class="fa-regular fa-copy"></i>';
                }, 2000);
            });
        }

        // configureVoiceNumber() removed — routing is auto-provisioned.

