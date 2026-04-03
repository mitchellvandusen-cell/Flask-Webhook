    function loadApiStatus() {
        fetch('/api/api-status')
            .then(r => r.json())
            .then(data => {
                if (data.has_key) {
                    document.getElementById('apiKeyDisplay').textContent = data.key_prefix || '••••••••';
                    document.getElementById('btnGenKey').style.display = 'none';
                    document.getElementById('btnRevokeKey').style.display = '';
                    document.getElementById('apiWebhookUrl').value = data.webhook_url || '';
                    document.getElementById('webhookSecretPreview').textContent = data.webhook_secret_preview || '—';
                } else {
                    document.getElementById('apiKeyDisplay').textContent = 'No key generated';
                    document.getElementById('btnGenKey').style.display = '';
                    document.getElementById('btnRevokeKey').style.display = 'none';
                    document.getElementById('webhookSecretPreview').textContent = '—';
                }
            })
            .catch(() => {
                document.getElementById('apiKeyDisplay').textContent = 'Error loading';
            });
    }

    function generateApiKey() {
        if (!confirm('Generate a new API key? Any existing key will be replaced.')) return;
        fetch('/api/generate-key', { method: 'POST', headers: {'Content-Type': 'application/json'} })
            .then(r => r.json())
            .then(data => {
                if (data.api_key) {
                    alert('API Key (copy now — shown only once):\n\n' + data.api_key + '\n\nWebhook Secret:\n' + data.webhook_secret);
                    loadApiStatus();
                } else {
                    alert('Error: ' + (data.error || 'Unknown error'));
                }
            })
            .catch(err => alert('Request failed: ' + err));
    }

    function revokeApiKey() {
        if (!confirm('Revoke your API key? All API integrations using this key will stop working.')) return;
        fetch('/api/revoke-key', { method: 'POST', headers: {'Content-Type': 'application/json'} })
            .then(r => r.json())
            .then(data => {
                if (data.status === 'success') {
                    alert('API key revoked.');
                    loadApiStatus();
                } else {
                    alert('Error: ' + (data.error || 'Unknown error'));
                }
            })
            .catch(err => alert('Request failed: ' + err));
    }

    function saveWebhookUrl() {
        const url = document.getElementById('apiWebhookUrl').value.trim();
        if (!url) { alert('Enter a webhook URL'); return; }
        if (!url.startsWith('https://')) { alert('Webhook URL must use HTTPS'); return; }
        fetch('/api/webhook-url', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ url: url })
        })
        .then(r => r.json())
        .then(data => {
            if (data.status === 'success') {
                alert('Webhook URL saved.');
            } else {
                alert('Error: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(err => alert('Request failed: ' + err));
    }

    // Load API status on page load
    document.addEventListener('DOMContentLoaded', loadApiStatus);

    // Auto-initialize VoIP device on page load if already provisioned
    // This ensures intercept works immediately without requiring "Setup VoIP" click
    // Critical for GHL iframe where the agent needs instant intercept capability
    document.addEventListener('DOMContentLoaded', function() {
        if (window.DASHBOARD_BOOT && window.DASHBOARD_BOOT.voipSetupDone && typeof initVoIPDevice === 'function') {
            console.log('[VoIP] Auto-initializing VoIP device on page load (provisioned)');
            // Small delay to allow page to finish rendering
            setTimeout(() => {
                if (!voipReady && !_voipInitializing) {
                    initVoIPDevice().catch(e => console.warn('[VoIP] Auto-init failed (non-critical):', e));
                }
            }, 1500);
        }
    });
