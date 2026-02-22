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
                } else {
                    resultDiv.innerHTML = `<span style="color:#ef4444;"><i class="fa-solid fa-times me-1"></i> ${d.error || 'Save failed'}</span>`;
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
                } else {
                    resultDiv.innerHTML = `<span style="color:#ef4444;"><i class="fa-solid fa-times-circle me-1"></i> ${d.message}</span>`;
                }
                setTimeout(() => resultDiv.innerHTML = '', 6000);
            }).catch(e => {
                resultDiv.innerHTML = '<span style="color:#ef4444;">Network error</span>';
            });
        }

