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
            container.innerHTML = '<div class="text-center py-4" style="color:' + _tc.textMut + ';"><i class="fa-solid fa-spinner fa-spin me-2"></i> Loading...</div>';

            fetch(url)
                .then(r => r.json())
                .then(data => {
                    if (!data.logs || data.logs.length === 0) {
                        container.innerHTML = '<div class="text-center py-5" style="color:' + _tc.textFaint + ';"><i class="fa-solid fa-inbox" style="font-size:2rem; margin-bottom:12px; display:block;"></i>No logs yet. Logs appear here when your webhook receives messages.</div>';
                        document.getElementById('logsPagination').style.display = 'none';
                        return;
                    }

                    let html = '';
                    data.logs.forEach(log => {
                        const time = log.created_at ? new Date(log.created_at).toLocaleString() : '';
                        const evt = (log.event_type || '').replace(/_/g, ' ');
                        html += `<div class="log-row">
                            <span class="log-badge ${log.status || 'info'}">${log.status || 'info'}</span>
                            <span class="log-time">${time}</span>
                            <span class="log-event">${evt}</span>
                            <span class="log-summary">${log.summary || ''}</span>
                            ${log.contact_id ? `<span class="log-contact">${log.contact_id.substring(0,12)}...</span>` : ''}
                        </div>`;
                    });
                    container.innerHTML = html;

                    const pagination = document.getElementById('logsPagination');
                    pagination.style.display = 'flex';
                    pagination.style.cssText = 'display:flex!important';
                    document.getElementById('logsPageInfo').textContent = `Showing ${logsOffset + 1}-${logsOffset + data.logs.length}`;
                    document.getElementById('logsNewer').disabled = logsOffset === 0;
                    document.getElementById('logsOlder').disabled = data.logs.length < LOGS_PER_PAGE;
                })
                .catch(e => {
                    container.innerHTML = '<div class="text-center py-4" style="color:' + _tc.red + ';"><i class="fa-solid fa-exclamation-triangle me-2"></i> Failed to load logs</div>';
                });
        }

