        // ===== CONNECT TAB =====
        const CRM_CONFIG_FIELDS = (window.DASHBOARD_BOOT && window.DASHBOARD_BOOT.crmConfigFields) || {};
        let currentCRM = 'ghl';

        function selectCRM(crmType) {
            currentCRM = crmType;
            document.querySelectorAll('.crm-select-btn').forEach(b => {
                b.classList.toggle('active', b.getAttribute('data-crm') === crmType);
            });
            document.querySelectorAll('.crm-guide').forEach(g => g.style.display = 'none');
            const guide = document.getElementById('crmGuide-' + crmType);
            if (guide) guide.style.display = 'block';

            // Show config form for non-LeadConnector CRMs
            const configForm = document.getElementById('integrationConfigForm');
            if (crmType === 'ghl') {
                configForm.style.display = 'none';
                return;
            }

            configForm.style.display = 'block';
            const fields = CRM_CONFIG_FIELDS[crmType];
            if (!fields || !fields.fields) return;

            document.getElementById('configFormTitle').textContent = fields.description || 'Configuration';
            const container = document.getElementById('configFields');
            container.innerHTML = '';

            fields.fields.forEach(f => {
                const col = document.createElement('div');
                col.className = 'col-md-6';
                const inputType = f.type === 'password' ? 'password' : f.type === 'url' ? 'url' : 'text';
                col.innerHTML = `
                    <label class="form-label">${f.label} ${f.required ? '<span style="color:var(--accent);">*</span>' : ''}</label>
                    <input type="${inputType}" class="form-control" data-key="${f.key}" placeholder="${f.help || ''}" />
                    ${f.help ? `<small style="color:#666;">${f.help}</small>` : ''}
                `;
                container.appendChild(col);
            });
        }

        function detectCRM() {
            const input = document.getElementById('quickConnectKey');
            const resultDiv = document.getElementById('quickConnectResult');
            const key = (input && input.value || '').trim();

            if (!key) {
                resultDiv.innerHTML = '<span style="color:#ef4444;">Please paste an API key first</span>';
                setTimeout(() => resultDiv.innerHTML = '', 3000);
                return;
            }

            resultDiv.innerHTML = '<span style="color:#3b82f6;"><i class="fa-solid fa-spinner fa-spin me-1"></i> Detecting CRM...</span>';

            fetch('/api/integrations/detect', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ api_key: key })
            }).then(r => r.json()).then(d => {
                if (d.detected) {
                    resultDiv.innerHTML = `<span style="color:var(--accent);"><i class="fa-solid fa-check-circle me-1"></i> Detected: <strong>${d.label}</strong></span>`;
                    // Auto-select the CRM
                    selectCRM(d.crm_type);
                    // Auto-fill the API key into the correct field
                    setTimeout(() => {
                        const inp = document.querySelector(`#configFields input[data-key="${d.field}"]`);
                        if (inp) inp.value = key;
                        // Fill extra fields (e.g. company_domain for Pipedrive)
                        if (d.extra) {
                            Object.entries(d.extra).forEach(([k, v]) => {
                                const f = document.querySelector(`#configFields input[data-key="${k}"]`);
                                if (f && v) f.value = v;
                            });
                        }
                    }, 100);
                    if (typeof _showDashToast === 'function') _showDashToast(true, `Detected ${d.label}! Fields auto-filled.`);
                } else {
                    resultDiv.innerHTML = `<span style="color:#f59e0b;"><i class="fa-solid fa-triangle-exclamation me-1"></i> ${d.message || 'Could not auto-detect. Select your CRM manually below.'}</span>`;
                }
                setTimeout(() => resultDiv.innerHTML = '', 6000);
            }).catch(e => {
                resultDiv.innerHTML = '<span style="color:#ef4444;">Network error — try again</span>';
                setTimeout(() => resultDiv.innerHTML = '', 4000);
            });
        }

        function saveIntegration() {
            const config = {};
            document.querySelectorAll('#configFields input').forEach(inp => {
                if (inp.value.trim()) config[inp.getAttribute('data-key')] = inp.value.trim();
            });

            const resultDiv = document.getElementById('integrationResult');
            resultDiv.innerHTML = '<span style="color:#3b82f6;"><i class="fa-solid fa-spinner fa-spin me-1"></i> Saving...</span>';

            fetch('/api/integrations/save', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ crm_type: currentCRM, crm_config: config })
            }).then(r => r.json()).then(d => {
                if (d.success) {
                    resultDiv.innerHTML = '<span style="color:var(--accent);"><i class="fa-solid fa-check me-1"></i> Configuration saved!</span>';
                    if (typeof _showDashToast === 'function') _showDashToast(true, 'CRM integration saved!');
                } else {
                    resultDiv.innerHTML = `<span style="color:#ef4444;"><i class="fa-solid fa-times me-1"></i> ${d.error || 'Save failed'}</span>`;
                    if (typeof _showDashToast === 'function') _showDashToast(false, d.error || 'Save failed');
                }
                setTimeout(() => resultDiv.innerHTML = '', 4000);
            }).catch(e => {
                resultDiv.innerHTML = '<span style="color:#ef4444;">Network error</span>';
            });
        }

        function testIntegration() {
            const config = {};
            document.querySelectorAll('#configFields input').forEach(inp => {
                if (inp.value.trim()) config[inp.getAttribute('data-key')] = inp.value.trim();
            });

            const resultDiv = document.getElementById('integrationResult');
            resultDiv.innerHTML = '<span style="color:#3b82f6;"><i class="fa-solid fa-spinner fa-spin me-1"></i> Testing connection...</span>';

            fetch('/api/integrations/test', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ crm_type: currentCRM, crm_config: config })
            }).then(r => r.json()).then(d => {
                if (d.valid) {
                    resultDiv.innerHTML = `<span style="color:var(--accent);"><i class="fa-solid fa-check-circle me-1"></i> ${d.message}</span>`;
                    if (typeof _showDashToast === 'function') _showDashToast(true, d.message);
                } else {
                    resultDiv.innerHTML = `<span style="color:#ef4444;"><i class="fa-solid fa-times-circle me-1"></i> ${d.message}</span>`;
                    if (typeof _showDashToast === 'function') _showDashToast(false, d.message);
                }
                setTimeout(() => resultDiv.innerHTML = '', 6000);
            }).catch(e => {
                resultDiv.innerHTML = '<span style="color:#ef4444;">Network error</span>';
            });
        }
