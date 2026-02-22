    (function() {
        const SEVERITY_MAP = {
            warning: { cls: 'alert-warning-persistent', icon: 'fa-triangle-exclamation' },
            error:   { cls: 'alert-error-persistent',   icon: 'fa-circle-xmark' },
            info:    { cls: 'alert-info-persistent',     icon: 'fa-circle-info' }
        };

        function renderAlerts(alerts) {
            const container = document.getElementById('persistent-alerts-container');
            if (!container) return;
            if (!alerts || alerts.length === 0) { container.innerHTML = ''; return; }

            container.innerHTML = alerts.map(a => {
                const sev = SEVERITY_MAP[a.severity] || SEVERITY_MAP.warning;
                const created = a.created_at ? new Date(a.created_at).toLocaleString() : '';
                return `<div class="persistent-alert ${sev.cls}" data-alert-id="${a.id}">
                    <i class="fa-solid ${sev.icon} pa-icon"></i>
                    <div class="pa-body">
                        <div class="pa-title">${escapeHtml(a.title)}</div>
                        <div class="pa-message">${escapeHtml(a.message)}</div>
                        ${created ? `<div class="pa-time">${created}</div>` : ''}
                    </div>
                    <button class="pa-dismiss" onclick="dismissAlert(${a.id}, this)" title="Dismiss">
                        <i class="fa-solid fa-xmark"></i>
                    </button>
                </div>`;
            }).join('');
        }

        function escapeHtml(str) {
            if (!str) return '';
            const div = document.createElement('div');
            div.textContent = str;
            return div.innerHTML;
        }

        window.dismissAlert = function(alertId, btn) {
            const alertEl = btn.closest('.persistent-alert');
            if (alertEl) alertEl.style.opacity = '0.5';
            fetch(`/api/alerts/${alertId}/dismiss`, { method: 'POST' })
                .then(r => r.json())
                .then(resp => {
                    if (resp.success && alertEl) {
                        alertEl.style.transition = 'all 0.3s';
                        alertEl.style.opacity = '0';
                        alertEl.style.maxHeight = '0';
                        alertEl.style.padding = '0';
                        alertEl.style.margin = '0';
                        alertEl.style.overflow = 'hidden';
                        setTimeout(() => alertEl.remove(), 350);
                    }
                })
                .catch(() => {
                    if (alertEl) alertEl.style.opacity = '1';
                });
        };

        // Fetch alerts on page load
        fetch('/api/alerts')
            .then(r => r.json())
            .then(data => renderAlerts(data.alerts || []))
            .catch(() => {});
    })();
