// agency.js — Agency dashboard: comprehensive KPIs, charts, leaderboards
// Uses /api/agency/dashboard-stats for tiled stats + Chart.js for visuals.

(function() {
    'use strict';

    // Chart instances (destroyed on reload to prevent canvas reuse errors)
    var _connectRateChart = null;
    var _durationChart = null;
    var _dailyChart = null;
    var _hourlyChart = null;

    // ── Members ──────────────────────────────────────────────────────────────
    window.agencyLoadMembers = function() {
        var tbody = document.getElementById('agencyMembersBody');
        if (!tbody) return;
        tbody.innerHTML = '<tr><td colspan="5" class="text-center dash-text-muted">Loading members...</td></tr>';

        fetch('/api/agency/members')
            .then(function(r) { return r.json(); })
            .then(function(data) {
                var members = data.members || [];
                var el;
                el = document.getElementById('agencyMemberCount'); if (el) el.textContent = members.length;
                el = document.getElementById('agencyActiveCount'); if (el) el.textContent = members.filter(function(m) { return m.status === 'Active'; }).length;
                el = document.getElementById('agencyPendingCount'); if (el) el.textContent = members.filter(function(m) { return m.status !== 'Active' && m.status !== 'Cancelled'; }).length;

                if (!members.length) {
                    tbody.innerHTML = '<tr><td colspan="5" class="text-center dash-text-muted">No members found. Agents will appear here when they connect their GHL account under your company.</td></tr>';
                    return;
                }

                var html = '';
                for (var i = 0; i < members.length; i++) {
                    var m = members[i];
                    var tier = m.subscription_tier || 'individual';
                    var tierLabel = !tier ? '—' :
                                   tier === 'sms_bot' ? 'SMS Bot' :
                                   tier === 'pro_dialer' ? 'Pro Dialer' :
                                   tier === 'solo_predictive' ? 'Solo Predictive' :
                                   tier === 'individual' ? 'Power Dialer' : tier;
                    var statusClass = m.status === 'Active' ? 'active' :
                                     m.status === 'Cancelled' ? 'cancelled' :
                                     m.status === 'Token Expired' ? 'expired' : 'pending';
                    var joined = m.created_at ? new Date(m.created_at).toLocaleDateString() : '—';

                    html += '<tr>' +
                        '<td>' + _esc(m.full_name || '—') + '</td>' +
                        '<td>' + _esc(m.email || '—') + '</td>' +
                        '<td>' + _esc(tierLabel) + '</td>' +
                        '<td><span class="agency-badge ' + statusClass + '">' + _esc(m.status) + '</span></td>' +
                        '<td>' + joined + '</td>' +
                        '</tr>';
                }
                tbody.innerHTML = html;
            })
            .catch(function(e) {
                tbody.innerHTML = '<tr><td colspan="5" class="text-center dash-text-muted">Error loading members</td></tr>';
                console.error('agencyLoadMembers error:', e);
            });
    };

    // ── KPIs (enhanced dashboard stats) ─────────────────────────────────────
    window.agencyLoadKpis = function(period, btn) {
        period = period || 'today';

        if (btn) {
            var btns = document.querySelectorAll('.agency-period-btn');
            for (var i = 0; i < btns.length; i++) btns[i].classList.remove('active');
            btn.classList.add('active');
        }

        fetch('/api/agency/dashboard-stats?period=' + encodeURIComponent(period))
            .then(function(r) { return r.json(); })
            .then(function(d) {
                _renderCoreTiles(d);
                _renderDurationBuckets(d);
                _renderAverages(d);
                _renderLeaderboards(d);
                _renderAllAgentsTable(d.agents || []);
                _renderConnectRateChart(d);
                _renderDurationDonut(d);
                _renderDailyChart(d.daily || []);
                _renderHourlyChart(d.hourly || []);
            })
            .catch(function(e) { console.error('agencyLoadKpis error:', e); });
    };

    // ── Core Tiles ──────────────────────────────────────────────────────────
    function _renderCoreTiles(d) {
        _setText('akTotalCalls', _fmtNum(d.total_calls));
        _setText('akConnected', _fmtNum(d.connected_calls));
        _setText('akMessages', _fmtNum(d.total_messages));
        _setText('akActiveAgents', (d.active_agents || 0) + '/' + (d.total_agents || 0));
        _setText('akUniqueContacts', _fmtNum(d.unique_contacts));

        // Speed to lead
        if (d.avg_speed_to_lead_secs != null) {
            var mins = Math.floor(d.avg_speed_to_lead_secs / 60);
            var secs = Math.round(d.avg_speed_to_lead_secs % 60);
            _setText('akSpeedToLead', mins > 0 ? mins + 'm ' + secs + 's' : secs + 's');
        } else {
            _setText('akSpeedToLead', '—');
        }

        // Deltas
        if (d.prior) {
            _setDelta('akDeltaCalls', d.prior.delta_calls);
            _setDelta('akDeltaConnected', d.prior.delta_connected);
            _setDelta('akDeltaConnectRate', d.prior.delta_connect_rate);
        } else {
            _setDelta('akDeltaCalls', null);
            _setDelta('akDeltaConnected', null);
            _setDelta('akDeltaConnectRate', null);
        }
    }

    // ── Duration Buckets ────────────────────────────────────────────────────
    function _renderDurationBuckets(d) {
        _setText('akOver45s', _fmtNum(d.over_45s));
        _setText('akOver2min', _fmtNum(d.over_2min));
        _setText('akOver5min', _fmtNum(d.over_5min));
        _setText('akOver10min', _fmtNum(d.over_10min));
        _setText('akPct45s', d.pct_45s + '%');
        _setText('akPct2min', d.pct_2min + '%');
        _setText('akPct5min', d.pct_5min + '%');
        _setText('akPct10min', d.pct_10min + '%');

        // Animated fill bars
        _setBarWidth('akBar45s', d.pct_45s);
        _setBarWidth('akBar2min', d.pct_2min);
        _setBarWidth('akBar5min', d.pct_5min);
        _setBarWidth('akBar10min', d.pct_10min);
    }

    function _setBarWidth(id, pct) {
        var el = document.getElementById(id);
        if (el) el.style.width = Math.min(pct, 100) + '%';
    }

    // ── Averages ────────────────────────────────────────────────────────────
    function _renderAverages(d) {
        _setText('akAvgDailyDials', _fmtNum(d.avg_daily_dials));
        _setText('akAvgDailyTotal', _fmtNum(d.avg_daily_dials_total));
        _setText('akAvgConnectRate', (d.avg_connect_rate_per_agent || 0) + '%');
        _setText('akTotalTalkTime', _fmtDurationLong(d.total_duration));
        _setText('akAvgDuration', _fmtDuration(d.avg_duration));
    }

    // ── Leaderboards ────────────────────────────────────────────────────────
    function _renderLeaderboards(d) {
        _renderLeaderList('akLeaderConnect', d.top_connect_rate || [], function(a) {
            return a.connect_rate + '% (' + a.total_calls + ' calls)';
        });
        _renderLeaderList('akLeaderCalls', d.top_by_calls || [], function(a) {
            return _fmtNum(a.total_calls) + ' calls (' + a.connect_rate + '%)';
        });
        _renderLeaderList('akLeaderDuration', d.top_by_duration || [], function(a) {
            return _fmtDuration(a.avg_duration) + ' avg (' + a.connected + ' connected)';
        });
    }

    function _renderLeaderList(containerId, agents, statFn) {
        var el = document.getElementById(containerId);
        if (!el) return;
        if (!agents.length) {
            el.innerHTML = '<div class="ak-leader-empty">No data yet</div>';
            return;
        }
        var html = '';
        for (var i = 0; i < agents.length; i++) {
            var a = agents[i];
            var medal = i === 0 ? 'ak-medal-gold' : i === 1 ? 'ak-medal-silver' : i === 2 ? 'ak-medal-bronze' : '';
            html += '<div class="ak-leader-row">' +
                '<span class="ak-leader-rank ' + medal + '">' + (i + 1) + '</span>' +
                '<span class="ak-leader-name">' + _esc(a.name) + '</span>' +
                '<span class="ak-leader-stat">' + statFn(a) + '</span>' +
                '</div>';
        }
        el.innerHTML = html;
    }

    // ── All Agents Table ────────────────────────────────────────────────────
    function _renderAllAgentsTable(agents) {
        var tbody = document.getElementById('akAllAgentsBody');
        if (!tbody) return;
        if (!agents.length) {
            tbody.innerHTML = '<tr><td colspan="11" class="text-center dash-text-muted">No agent activity.</td></tr>';
            return;
        }
        var html = '';
        for (var i = 0; i < agents.length; i++) {
            var a = agents[i];
            html += '<tr>' +
                '<td>' + _esc(a.name) + '</td>' +
                '<td>' + _fmtNum(a.total_calls) + '</td>' +
                '<td>' + _fmtNum(a.connected) + '</td>' +
                '<td>' + a.connect_rate + '%</td>' +
                '<td>' + a.daily_avg_dials + '</td>' +
                '<td>' + _fmtDuration(a.total_secs) + '</td>' +
                '<td>' + _fmtDuration(a.avg_duration) + '</td>' +
                '<td>' + _fmtNum(a.over_45s) + '</td>' +
                '<td>' + _fmtNum(a.over_2min) + '</td>' +
                '<td>' + _fmtNum(a.over_5min) + '</td>' +
                '<td>' + _fmtNum(a.messages) + '</td>' +
                '</tr>';
        }
        tbody.innerHTML = html;

        // Show/hide see-more based on agent count
        var wrap = document.getElementById('akSeeMoreWrap');
        if (wrap) wrap.style.display = agents.length > 5 ? '' : 'none';
    }

    // Toggle all agents table
    window.akToggleAllAgents = function() {
        var el = document.getElementById('akAllAgents');
        if (!el) return;
        var visible = el.style.display !== 'none';
        el.style.display = visible ? 'none' : '';
    };

    // ── Charts ──────────────────────────────────────────────────────────────

    function _chartColors() {
        var isLight = document.body.classList.contains('light-theme');
        return {
            text: isLight ? '#374151' : 'rgba(255,255,255,0.7)',
            grid: isLight ? 'rgba(0,0,0,0.06)' : 'rgba(255,255,255,0.06)',
            accent: '#00ff88',
            accentDim: 'rgba(0,255,136,0.3)',
            blue: '#3b82f6',
            blueDim: 'rgba(59,130,246,0.3)',
        };
    }

    // Connect Rate Donut
    function _renderConnectRateChart(d) {
        var canvas = document.getElementById('akConnectRateChart');
        if (!canvas || typeof Chart === 'undefined') return;
        if (_connectRateChart) _connectRateChart.destroy();

        var rate = d.connect_rate || 0;
        _setText('akConnectRateVal', rate + '%');

        _connectRateChart = new Chart(canvas.getContext('2d'), {
            type: 'doughnut',
            data: {
                labels: ['Connected', 'Not Connected'],
                datasets: [{
                    data: [rate, 100 - rate],
                    backgroundColor: ['#00ff88', 'rgba(255,255,255,0.08)'],
                    borderWidth: 0,
                    borderRadius: 4,
                }]
            },
            options: {
                cutout: '75%',
                responsive: true,
                maintainAspectRatio: true,
                plugins: { legend: { display: false }, tooltip: { enabled: false } },
            }
        });
    }

    // Duration Buckets Donut
    function _renderDurationDonut(d) {
        var canvas = document.getElementById('akDurationChart');
        if (!canvas || typeof Chart === 'undefined') return;
        if (_durationChart) _durationChart.destroy();

        var connected = d.connected_calls || 0;
        var b10 = d.over_10min || 0;
        var b5 = (d.over_5min || 0) - b10;
        var b2 = (d.over_2min || 0) - (d.over_5min || 0);
        var b45 = (d.over_45s || 0) - (d.over_2min || 0);
        var bShort = connected - (d.over_45s || 0);

        _durationChart = new Chart(canvas.getContext('2d'), {
            type: 'doughnut',
            data: {
                labels: ['<45s', '45s–2m', '2–5m', '5–10m', '10m+'],
                datasets: [{
                    data: [Math.max(0, bShort), Math.max(0, b45), Math.max(0, b2), Math.max(0, b5), Math.max(0, b10)],
                    backgroundColor: ['#ef4444', '#f59e0b', '#00ff88', '#3b82f6', '#8b5cf6'],
                    borderWidth: 0,
                    borderRadius: 4,
                }]
            },
            options: {
                cutout: '75%',
                responsive: true,
                maintainAspectRatio: true,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        callbacks: {
                            label: function(ctx) {
                                var total = ctx.dataset.data.reduce(function(a, b) { return a + b; }, 0);
                                var pct = total ? Math.round(ctx.raw / total * 100) : 0;
                                return ctx.label + ': ' + ctx.raw + ' (' + pct + '%)';
                            }
                        }
                    }
                },
            }
        });
    }

    // Daily Volume Bar Chart
    function _renderDailyChart(daily) {
        var canvas = document.getElementById('akDailyChart');
        if (!canvas || typeof Chart === 'undefined') return;
        if (_dailyChart) _dailyChart.destroy();

        var c = _chartColors();
        var labels = daily.map(function(d) {
            var parts = d.day.split('-');
            return parts[1] + '/' + parts[2];
        });

        _dailyChart = new Chart(canvas.getContext('2d'), {
            type: 'bar',
            data: {
                labels: labels,
                datasets: [
                    {
                        label: 'Total Calls',
                        data: daily.map(function(d) { return d.calls; }),
                        backgroundColor: c.accentDim,
                        borderColor: c.accent,
                        borderWidth: 1,
                        borderRadius: 4,
                    },
                    {
                        label: 'Connected',
                        data: daily.map(function(d) { return d.connected; }),
                        backgroundColor: c.blueDim,
                        borderColor: c.blue,
                        borderWidth: 1,
                        borderRadius: 4,
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    x: { ticks: { color: c.text, maxRotation: 45 }, grid: { display: false } },
                    y: { beginAtZero: true, ticks: { color: c.text }, grid: { color: c.grid } },
                },
                plugins: {
                    legend: { labels: { color: c.text, boxWidth: 12 } },
                }
            }
        });
    }

    // Hourly Distribution Bar Chart
    function _renderHourlyChart(hourly) {
        var canvas = document.getElementById('akHourlyChart');
        if (!canvas || typeof Chart === 'undefined') return;
        if (_hourlyChart) _hourlyChart.destroy();

        var c = _chartColors();
        var labels = hourly.map(function(h) {
            var hr = h.hour;
            var ampm = hr >= 12 ? 'p' : 'a';
            var display = hr === 0 ? 12 : hr > 12 ? hr - 12 : hr;
            return display + ampm;
        });

        _hourlyChart = new Chart(canvas.getContext('2d'), {
            type: 'bar',
            data: {
                labels: labels,
                datasets: [
                    {
                        label: 'Calls',
                        data: hourly.map(function(h) { return h.calls; }),
                        backgroundColor: hourly.map(function(h) {
                            return h.calls > 0 ? c.accentDim : 'rgba(255,255,255,0.03)';
                        }),
                        borderColor: c.accent,
                        borderWidth: 1,
                        borderRadius: 4,
                    },
                    {
                        label: 'Connected',
                        data: hourly.map(function(h) { return h.connected; }),
                        backgroundColor: c.blueDim,
                        borderColor: c.blue,
                        borderWidth: 1,
                        borderRadius: 4,
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    x: { ticks: { color: c.text }, grid: { display: false } },
                    y: { beginAtZero: true, ticks: { color: c.text }, grid: { color: c.grid } },
                },
                plugins: {
                    legend: { labels: { color: c.text, boxWidth: 12 } },
                }
            }
        });
    }

    // ── Helpers ──────────────────────────────────────────────────────────────
    function _esc(s) {
        if (!s) return '';
        var d = document.createElement('div');
        d.textContent = s;
        return d.innerHTML;
    }

    function _setText(id, val) {
        var el = document.getElementById(id);
        if (el) el.textContent = val;
    }

    function _fmtNum(n) {
        return (n || 0).toLocaleString();
    }

    function _fmtDuration(secs) {
        secs = Math.round(secs || 0);
        if (secs < 60) return secs + 's';
        var m = Math.floor(secs / 60);
        var s = secs % 60;
        return m + 'm ' + s + 's';
    }

    function _fmtDurationLong(secs) {
        secs = Math.round(secs || 0);
        if (secs < 60) return secs + 's';
        if (secs < 3600) return Math.floor(secs / 60) + 'm ' + (secs % 60) + 's';
        var h = Math.floor(secs / 3600);
        var m = Math.floor((secs % 3600) / 60);
        return h + 'h ' + m + 'm';
    }

    function _setDelta(id, val) {
        var el = document.getElementById(id);
        if (!el) return;
        if (val === null || val === undefined) {
            el.textContent = '';
            el.className = 'ak-tile-delta';
            return;
        }
        var sign = val >= 0 ? '+' : '';
        el.textContent = sign + val.toFixed(1) + '%';
        el.className = 'ak-tile-delta ' + (val >= 0 ? 'positive' : 'negative');
    }

})();
