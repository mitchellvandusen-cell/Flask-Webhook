// agency.js — Agency members + KPIs management for agency owners
// Loads agency member list, KPIs, and agent stats.

(function() {
    'use strict';

    // ── Members ──────────────────────────────────────────────────────────────
    window.agencyLoadMembers = function() {
        var tbody = document.getElementById('agencyMembersBody');
        if (!tbody) return;
        tbody.innerHTML = '<tr><td colspan="5" class="text-center dash-text-muted">Loading members...</td></tr>';

        fetch('/api/agency/members')
            .then(function(r) { return r.json(); })
            .then(function(data) {
                var members = data.members || [];
                // Update stat cards
                var total = members.length;
                var active = members.filter(function(m) { return m.status === 'Active'; }).length;
                var pending = total - active;
                var el;
                el = document.getElementById('agencyMemberCount'); if (el) el.textContent = total;
                el = document.getElementById('agencyActiveCount'); if (el) el.textContent = active;
                el = document.getElementById('agencyPendingCount'); if (el) el.textContent = pending;

                if (!members.length) {
                    tbody.innerHTML = '<tr><td colspan="5" class="text-center dash-text-muted">No members found. Agents will appear here when they connect their GHL account under your company.</td></tr>';
                    return;
                }

                var html = '';
                for (var i = 0; i < members.length; i++) {
                    var m = members[i];
                    var tier = m.subscription_tier || 'individual';
                    var tierLabel = tier === 'sms_bot' ? 'SMS Bot' :
                                   tier === 'pro_dialer' ? 'Pro Dialer' :
                                   tier === 'solo_predictive' ? 'Solo Predictive' :
                                   tier === 'individual' ? 'Power Dialer' : tier;
                    var statusClass = m.status === 'Active' ? 'active' : 'pending';
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

    // ── KPIs ─────────────────────────────────────────────────────────────────
    window.agencyLoadKpis = function(period, btn) {
        period = period || 'today';

        // Update period button active state
        if (btn) {
            var btns = document.querySelectorAll('.agency-period-btn');
            for (var i = 0; i < btns.length; i++) btns[i].classList.remove('active');
            btn.classList.add('active');
        }

        fetch('/api/agency/kpis?period=' + encodeURIComponent(period))
            .then(function(r) { return r.json(); })
            .then(function(d) {
                _setText('akTotalCalls', _fmtNum(d.total_calls));
                _setText('akConnected', _fmtNum(d.connected_calls));
                _setText('akConnectRate', (d.connect_rate || 0).toFixed(1) + '%');
                _setText('akAvgDuration', _fmtDuration(d.avg_duration));
                _setText('akMessages', _fmtNum(d.total_messages));
                _setText('akActiveAgents', d.active_agents + '/' + d.total_agents);

                // Deltas
                if (d.prior) {
                    _setDelta('akDeltaCalls', d.prior.delta_calls);
                    _setDelta('akDeltaConnected', d.prior.delta_connected);
                    _setDelta('akDeltaDuration', d.prior.delta_duration);
                }
            })
            .catch(function(e) { console.error('agencyLoadKpis error:', e); });

        // Also load agent stats
        _loadAgentStats(period);
    };

    function _loadAgentStats(period) {
        fetch('/api/agency/agent-stats?period=' + encodeURIComponent(period))
            .then(function(r) { return r.json(); })
            .then(function(data) {
                var agents = data.agents || [];
                var tbody = document.getElementById('agencyAgentStatsBody');
                if (!tbody) return;

                if (!agents.length) {
                    tbody.innerHTML = '<tr><td colspan="6" class="text-center dash-text-muted">No agent activity for this period.</td></tr>';
                    return;
                }

                var html = '';
                for (var i = 0; i < agents.length; i++) {
                    var a = agents[i];
                    html += '<tr>' +
                        '<td>' + _esc(a.name || a.email || '—') + '</td>' +
                        '<td>' + _fmtNum(a.total_calls) + '</td>' +
                        '<td>' + _fmtNum(a.connected) + '</td>' +
                        '<td>' + _fmtDuration(a.total_secs) + '</td>' +
                        '<td>' + _fmtDuration(a.avg_duration) + '</td>' +
                        '<td>' + _fmtNum(a.messages) + '</td>' +
                        '</tr>';
                }
                tbody.innerHTML = html;
            })
            .catch(function(e) {
                console.error('_loadAgentStats error:', e);
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

    function _setDelta(id, val) {
        var el = document.getElementById(id);
        if (!el) return;
        if (val === null || val === undefined) {
            el.textContent = '';
            return;
        }
        var sign = val >= 0 ? '+' : '';
        el.textContent = sign + val.toFixed(1) + '%';
        el.className = 'agency-kpi-delta ' + (val >= 0 ? 'positive' : 'negative');
    }

})();
