        // ===== LOGS =====
        let logsOffset = 0;
        const LOGS_PER_PAGE = 50;

        function loadLogs(direction) {
            if (direction === 'older') logsOffset += LOGS_PER_PAGE;
            else if (direction === 'newer') logsOffset = Math.max(0, logsOffset - LOGS_PER_PAGE);
            else logsOffset = 0;

            const eventType = document.getElementById('logFilterType').value;
            const status = document.getElementById('logFilterStatus').value;
            let url = `/api/logs?limit=${LOGS_PER_PAGE}&offset=${logsOffset}`;
            if (eventType) url += `&event_type=${eventType}`;
            if (status) url += `&status=${status}`;

            const container = document.getElementById('logsContainer');
            container.innerHTML = '<div class="log-empty-state"><i class="fa-solid fa-spinner fa-spin"></i><span>Loading...</span></div>';

            fetch(url)
                .then(r => r.json())
                .then(data => {
                    if (!data.logs || data.logs.length === 0) {
                        container.innerHTML = '<div class="log-empty-state"><i class="fa-solid fa-inbox"></i><span>No logs yet. Logs appear here when your webhook receives messages.</span></div>';
                        document.getElementById('logsPagination').style.display = 'none';
                        return;
                    }

                    let html = '';
                    data.logs.forEach(log => {
                        const time = log.created_at ? new Date(log.created_at).toLocaleString() : '';
                        const evt = (log.event_type || '').replace(/_/g, ' ');
                        const statusClass = log.status || 'info';
                        html += `<div class="log-row">
                            <span class="log-badge ${statusClass}">${statusClass}</span>
                            <span class="log-time">${time}</span>
                            <span class="log-event">${evt}</span>
                            <span class="log-summary">${log.summary || ''}</span>
                            <span class="log-contact">${log.contact_id ? log.contact_id.substring(0,12) + '...' : ''}</span>
                        </div>`;
                    });
                    container.innerHTML = html;

                    const pagination = document.getElementById('logsPagination');
                    pagination.style.display = 'flex';
                    pagination.style.cssText = 'display:flex!important';
                    document.getElementById('logsPageInfo').textContent = `${logsOffset + 1}\u2013${logsOffset + data.logs.length}`;
                    document.getElementById('logsNewer').disabled = logsOffset === 0;
                    document.getElementById('logsOlder').disabled = data.logs.length < LOGS_PER_PAGE;
                })
                .catch(e => {
                    container.innerHTML = '<div class="log-empty-state"><i class="fa-solid fa-exclamation-triangle"></i><span>Failed to load logs</span></div>';
                });
        }

