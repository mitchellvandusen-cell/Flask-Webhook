// agency.js — Agency dashboard: comprehensive KPIs, charts, leaderboards
// Uses /api/agency/dashboard-stats for tiled stats + Chart.js for visuals.

(function() {
    'use strict';

    // Chart instances (destroyed on reload to prevent canvas reuse errors)
    var _connectRateChart = null;
    var _durationChart = null;
    var _dailyChart = null;
    var _hourlyChart = null;

    // Track current period + agent filter for KPI view
    var _currentStatsPeriod = 'today';
    var _currentAgentFilter = '';  // '' = all agents, otherwise location_id
    var _agentList = [];           // [{location_id, name}] populated on first load

    // Audio playback state
    var _currentAudio = null;
    var _currentAudioUrl = null;

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
                    tbody.innerHTML = '<tr><td colspan="5" class="text-center dash-text-muted">No members found. Agents will appear here when they connect their LeadConnector account under your company.</td></tr>';
                    return;
                }

                window._agencyMembers = members;
                var html = '';
                for (var i = 0; i < members.length; i++) {
                    var m = members[i];
                    var loc = _esc(m.location_id || '');
                    var email = _esc(m.email || '');
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

                    html += '<tr class="agency-member-row" onclick="agencyToggleMember(\'' + loc + '\')" style="cursor:pointer;">' +
                        '<td><i class="fa-solid fa-chevron-right agency-chevron" id="agencyChev_' + loc + '" style="font-size:.65rem;color:#555;margin-right:6px;transition:transform .2s;"></i>' + _esc(m.full_name || '—') + '</td>' +
                        '<td>' + email + '</td>' +
                        '<td>' + _esc(tierLabel) + '</td>' +
                        '<td><span class="agency-badge ' + statusClass + '">' + _esc(m.status) + '</span></td>' +
                        '<td>' + joined + '</td>' +
                        '</tr>';
                    html += '<tr class="agency-member-detail" id="agencyDetail_' + loc + '" style="display:none;">' +
                        '<td colspan="5" style="padding:0;border-top:none;">' +
                        '<div class="agency-detail-panel">' +
                        '<div class="agency-detail-actions">' +
                        '<button onclick="event.stopPropagation();agencyDetailTab(\'' + loc + '\',\'recordings\',this)" class="agency-detail-btn active">Recordings</button>' +
                        '<button onclick="event.stopPropagation();agencyDetailTab(\'' + loc + '\',\'stats\',this)" class="agency-detail-btn">Stats</button>' +
                        '<button onclick="event.stopPropagation();agencyStartMeet(\'' + email + '\')" class="agency-detail-btn agency-detail-btn-meet">Meet</button>' +
                        '</div>' +
                        '<div id="agencyDetailContent_' + loc + '" class="agency-detail-content"><div class="agency-detail-loading"><i class="fa-solid fa-spinner fa-spin"></i></div></div>' +
                        '</div></td></tr>';
                }
                tbody.innerHTML = html;
            })
            .catch(function(e) {
                tbody.innerHTML = '<tr><td colspan="5" class="text-center dash-text-muted">Error loading members</td></tr>';
                console.error('agencyLoadMembers error:', e);
            });
    };

    // ── Member Detail Accordion ──────────────────────────────────────────────
    var _agencyOpenMember = null;

    window.agencyToggleMember = function(locId) {
        var detail = document.getElementById('agencyDetail_' + locId);
        var chev = document.getElementById('agencyChev_' + locId);
        if (!detail) return;
        // Collapse previous
        if (_agencyOpenMember && _agencyOpenMember !== locId) {
            var prev = document.getElementById('agencyDetail_' + _agencyOpenMember);
            var prevChev = document.getElementById('agencyChev_' + _agencyOpenMember);
            if (prev) prev.style.display = 'none';
            if (prevChev) prevChev.style.transform = '';
        }
        // Toggle current
        var isOpen = detail.style.display !== 'none';
        detail.style.display = isOpen ? 'none' : '';
        if (chev) chev.style.transform = isOpen ? '' : 'rotate(90deg)';
        _agencyOpenMember = isOpen ? null : locId;
        // Auto-load recordings on first open
        if (!isOpen) agencyLoadRecordings(locId);
    };

    window.agencyDetailTab = function(locId, tab, btnEl) {
        // Update active button
        var panel = document.getElementById('agencyDetail_' + locId);
        if (panel) panel.querySelectorAll('.agency-detail-btn').forEach(function(b) { b.classList.remove('active'); });
        if (btnEl) btnEl.classList.add('active');
        // Load content
        if (tab === 'recordings') agencyLoadRecordings(locId);
        else if (tab === 'stats') agencyLoadStats(locId);
    };

    window.agencyLoadRecordings = function(locId) {
        var el = document.getElementById('agencyDetailContent_' + locId);
        if (!el) return;
        el.innerHTML = '<div class="agency-detail-loading"><i class="fa-solid fa-spinner fa-spin"></i></div>';
        fetch('/api/agency/call-log?agent=' + encodeURIComponent(locId) + '&limit=50')
            .then(function(r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
            .then(function(d) {
                var calls = d.calls || [];
                if (!calls.length) { el.innerHTML = '<div class="agency-detail-empty">No recordings found</div>'; return; }
                el.innerHTML = calls.map(function(c) {
                    var dur = c.duration ? Math.floor(c.duration/60) + ':' + String(c.duration%60).padStart(2,'0') : '--:--';
                    var dt = c.created_at ? new Date(c.created_at).toLocaleString([], {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}) : '';
                    var sc = {completed:'var(--accent)','no-answer':'#ffa500',busy:'#ffa500',failed:'#ef4444'}[c.status] || '#888';
                    var hasRec = !!c.recording_url;
                    var hasTx = c.transcript && c.transcript.length > 0;
                    var callSid = c.call_sid || '';

                    var row = '<div class="agency-rec-row" data-url="' + _esc(c.recording_url || '') + '">';
                    row += '<div style="display:flex;align-items:center;gap:6px;min-width:0;flex:1;">';
                    row += '<span class="chr-name">' + _esc(c.contact_name || c.to_number || 'Unknown') + '</span>';
                    row += '<span class="chr-dir">' + _esc(c.direction || 'outbound') + '</span>';
                    row += '<span class="chr-date">' + dt + '</span>';
                    row += '</div>';
                    row += '<div style="display:flex;flex-direction:column;gap:4px;margin-top:2px;flex:1;">';
                    row += '<div style="display:flex;align-items:center;gap:6px;">';
                    row += '<span class="chr-status" style="color:' + sc + ';">' + _esc((c.status||'').replace(/-/g,' ')) + '</span>';
                    row += '<span class="chr-dur">' + dur + '</span>';
                    row += '</div>';

                    if (hasRec) {
                        row += '<div class="audio-controls">';
                        row += '<button onclick="playRecording(\'' + _esc(c.recording_url) + '\')" class="audio-btn audio-play-btn" title="Play"><i class="fa-solid fa-play"></i></button>';
                        row += '<button onclick="pauseRecording(\'' + _esc(c.recording_url) + '\')" class="audio-btn audio-pause-btn" title="Pause" style="display:none;"><i class="fa-solid fa-pause"></i></button>';
                        row += '<button onclick="stopRecording(\'' + _esc(c.recording_url) + '\')" class="audio-btn audio-stop-btn" title="Stop"><i class="fa-solid fa-stop"></i></button>';
                        row += '<input type="range" class="audio-scrubber" min="0" max="100" value="0" oninput="seekRecording(\'' + _esc(c.recording_url) + '\', this.value)">';
                        row += '<span class="audio-current-time">0:00</span>';
                        row += '<span class="audio-duration">' + (c.duration ? formatTime(c.duration) : '0:00') + '</span>';
                        row += '<a href="' + _esc(c.recording_url) + '?dl=1" download class="audio-btn audio-download-btn" title="Download"><i class="fa-solid fa-download"></i></a>';
                        row += '<button onclick="transcribeRecording(\'' + _esc(c.recording_url) + '\', \'' + _esc(callSid) + '\')" class="audio-btn audio-transcribe-btn" title="Transcribe"><i class="fa-solid fa-wand-magic-sparkles"></i></button>';
                        row += '</div>';
                    }

                    if (hasTx) {
                        row += '<button onclick=\'showTranscript(' + JSON.stringify(c.transcript).replace(/'/g,"\\'") + ')\' class="audio-btn audio-transcript-btn" title="View Transcript"><i class="fa-solid fa-file-lines"></i> View Transcript</button>';
                    }

                    row += '</div></div>';
                    return row;
                }).join('');
            })
            .catch(function(e) { console.error('[Agency] Recordings error:', e); el.innerHTML = '<div class="agency-detail-empty">Error loading recordings</div>'; });
    };

    window.agencyLoadStats = function(locId) {
        var el = document.getElementById('agencyDetailContent_' + locId);
        if (!el) return;
        el.innerHTML = '<div class="agency-detail-loading"><i class="fa-solid fa-spinner fa-spin"></i></div>';
        fetch('/api/agency/agent-stats?period=' + encodeURIComponent(_currentStatsPeriod))
            .then(function(r) { return r.json(); })
            .then(function(d) {
                var agents = d.agents || [];
                var agent = agents.find(function(a) { return a.location_id === locId; });
                if (!agent) { el.innerHTML = '<div class="agency-detail-empty">No stats available for this agent</div>'; return; }
                var talkMin = Math.floor((agent.total_talk_time || 0) / 60);
                var avgDur = agent.avg_duration ? Math.floor(agent.avg_duration) + 's' : '—';
                el.innerHTML = '<div class="agency-stats-grid">' +
                    '<div class="agency-stat-mini"><div class="agency-stat-mini-val">' + (agent.total_calls || 0) + '</div><div class="agency-stat-mini-lbl">Total Calls</div></div>' +
                    '<div class="agency-stat-mini"><div class="agency-stat-mini-val">' + (agent.connected_calls || 0) + '</div><div class="agency-stat-mini-lbl">Connected</div></div>' +
                    '<div class="agency-stat-mini"><div class="agency-stat-mini-val">' + talkMin + 'm</div><div class="agency-stat-mini-lbl">Talk Time</div></div>' +
                    '<div class="agency-stat-mini"><div class="agency-stat-mini-val">' + avgDur + '</div><div class="agency-stat-mini-lbl">Avg Duration</div></div>' +
                    '</div>';
            })
            .catch(function() { el.innerHTML = '<div class="agency-detail-empty">Error loading stats</div>'; });
    };

    window.agencyStartMeet = function(email) {
        if (!email) return;
        if (typeof _showDashToast === 'function') _showDashToast(true, 'Creating meeting...');
        fetch('/google-calendar/meet/start-now', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ contact_name: email.split('@')[0], attendee_email: email })
        }).then(function(r) { return r.json(); })
        .then(function(d) {
            if (d.meet_link) {
                window.open(d.meet_link, '_blank');
                if (typeof _showDashToast === 'function') _showDashToast(true, 'Meeting created — invite sent to ' + email);
            } else {
                if (typeof _showDashToast === 'function') _showDashToast(false, d.error || 'Failed to create meeting');
            }
        }).catch(function() {
            if (typeof _showDashToast === 'function') _showDashToast(false, 'Network error creating meeting');
        });
    };

    // ── Period dropdown ──────────────────────────────────────────────────────
    window.akTogglePeriodDropdown = function(e) {
        e.stopPropagation();
        var dd = document.getElementById('akPeriodDropdown');
        var btn = document.getElementById('akPeriodTrigger');
        var isOpen = dd.classList.toggle('open');
        btn.classList.toggle('open', isOpen);
        // Close member dropdown if open
        document.getElementById('akMemberDropdown').classList.remove('open');
        document.getElementById('akMemberTrigger').classList.remove('open');
    };

    window.akSelectPeriod = function(period, label, itemEl) {
        _currentStatsPeriod = period;
        document.getElementById('akPeriodLabel').textContent = label;
        // Mark active
        var items = document.querySelectorAll('#akPeriodDropdown .ak-dropdown-item');
        for (var i = 0; i < items.length; i++) items[i].classList.remove('active');
        if (itemEl) itemEl.classList.add('active');
        // Close dropdown
        document.getElementById('akPeriodDropdown').classList.remove('open');
        document.getElementById('akPeriodTrigger').classList.remove('open');
        _loadKpiData();
    };

    // ── Member dropdown ──────────────────────────────────────────────────────
    window.akToggleMemberDropdown = function(e) {
        e.stopPropagation();
        var dd = document.getElementById('akMemberDropdown');
        var btn = document.getElementById('akMemberTrigger');
        var isOpen = dd.classList.toggle('open');
        btn.classList.toggle('open', isOpen);
        // Close period dropdown if open
        document.getElementById('akPeriodDropdown').classList.remove('open');
        document.getElementById('akPeriodTrigger').classList.remove('open');
        // Focus search on open
        if (isOpen) {
            var s = document.getElementById('akMemberSearch');
            if (s) setTimeout(function() { s.focus(); }, 50);
        }
    };

    window.akSelectMember = function(locationId, name, itemEl) {
        _currentAgentFilter = locationId;
        document.getElementById('akMemberLabel').textContent = name;
        var items = document.querySelectorAll('#akMemberList .ak-dropdown-item');
        for (var i = 0; i < items.length; i++) items[i].classList.remove('active');
        if (itemEl) itemEl.classList.add('active');
        document.getElementById('akMemberDropdown').classList.remove('open');
        document.getElementById('akMemberTrigger').classList.remove('open');
        // Update header title
        var titleEl = document.getElementById('akHeaderTitle');
        if (titleEl) titleEl.textContent = locationId ? name + ' — Performance' : 'Agency Performance';
        _loadKpiData();
    };

    window.akFilterMemberList = function(q) {
        q = q.toLowerCase();
        var items = document.querySelectorAll('#akMemberList .ak-dropdown-item[data-agent]');
        for (var i = 0; i < items.length; i++) {
            var txt = items[i].textContent.toLowerCase();
            items[i].style.display = (!q || txt.indexOf(q) !== -1) ? '' : 'none';
        }
    };

    function _populateMemberDropdown(agents) {
        _agentList = agents || [];
        var list = document.getElementById('akMemberList');
        if (!list) return;
        // Rebuild list — keep "All Agents" first
        var html = '<button class="ak-dropdown-item' + (_currentAgentFilter === '' ? ' active' : '') +
            '" data-agent="" onclick="akSelectMember(\'\', \'All Agents\', this)">' +
            '<i class="fa-solid fa-users"></i>All Agents</button>';
        for (var i = 0; i < _agentList.length; i++) {
            var a = _agentList[i];
            var isActive = _currentAgentFilter === a.location_id;
            html += '<button class="ak-dropdown-item' + (isActive ? ' active' : '') +
                '" data-agent="' + a.location_id + '" onclick="akSelectMember(\'' +
                a.location_id.replace(/'/g, "\\'") + '\', \'' +
                (a.name || a.email || 'Agent').replace(/'/g, "\\'") + '\', this)">' +
                '<i class="fa-solid fa-user"></i>' +
                _escHtml(a.name || a.email || 'Unknown') + '</button>';
        }
        list.innerHTML = html;
    }

    // ── KPIs (enhanced dashboard stats) ─────────────────────────────────────
    function _loadKpiData() {
        var url = '/api/agency/dashboard-stats?period=' + encodeURIComponent(_currentStatsPeriod);
        if (_currentAgentFilter) url += '&agent=' + encodeURIComponent(_currentAgentFilter);

        fetch(url)
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
                // Populate member dropdown from agents list on first load
                if (_agentList.length === 0 && d.agents && d.agents.length > 0) {
                    _populateMemberDropdown(d.agents);
                }
            })
            .catch(function(e) { console.error('agencyLoadKpis error:', e); });
    }

    window.agencyLoadKpis = function(period, _btn) {
        // Called from sidebar on tab open — sync period state but don't override
        // if the user already selected a custom period this session
        if (period && !_agentList.length) {
            _currentStatsPeriod = period;
        }
        _loadKpiData();
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

    function _escHtml(s) {
        return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
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

    // ── Audio Playback Controls ──────────────────────────────────────────────

    function formatTime(seconds) {
        if (!seconds || isNaN(seconds)) return '0:00';
        var mins = Math.floor(seconds / 60);
        var secs = Math.floor(seconds % 60);
        return mins + ':' + String(secs).padStart(2, '0');
    }

    window.playRecording = function(url) {
        // Same recording clicked — toggle play/pause
        if (_currentAudioUrl === url && _currentAudio) {
            if (_currentAudio.paused) {
                _currentAudio.play();
            } else {
                _currentAudio.pause();
            }
            return;
        }

        // Different recording — stop current first
        if (_currentAudio) {
            _currentAudio.pause();
            _currentAudio.currentTime = 0;
            updatePlayButton(_currentAudioUrl, false);
        }

        // New recording - create new audio element
        _currentAudio = new Audio(url);
        _currentAudioUrl = url;

        // Set up event listeners
        _currentAudio.addEventListener('loadedmetadata', function() {
            var duration = _currentAudio.duration;
            updateDurationDisplay(url, duration);
            updateScrubber(url, 0, duration);
        });

        _currentAudio.addEventListener('timeupdate', function() {
            var currentTime = _currentAudio.currentTime;
            var duration = _currentAudio.duration;
            updateScrubber(url, currentTime, duration);
            updateCurrentTimeDisplay(url, currentTime);
        });

        _currentAudio.addEventListener('ended', function() {
            updatePlayButton(url, false);
            _currentAudio.currentTime = 0;
        });

        _currentAudio.addEventListener('pause', function() {
            updatePlayButton(url, false);
        });

        _currentAudio.addEventListener('play', function() {
            updatePlayButton(url, true);
        });

        // Start playing
        _currentAudio.play();
        updatePlayButton(url, true);
    };

    window.pauseRecording = function(url) {
        if (_currentAudio && _currentAudioUrl === url) {
            _currentAudio.pause();
        }
    };

    window.stopRecording = function(url) {
        if (_currentAudio && _currentAudioUrl === url) {
            _currentAudio.pause();
            _currentAudio.currentTime = 0;
            updatePlayButton(url, false);
            updateScrubber(url, 0, _currentAudio.duration);
        }
    };

    window.seekRecording = function(url, percentage) {
        if (_currentAudio && _currentAudioUrl === url) {
            var duration = _currentAudio.duration;
            _currentAudio.currentTime = (percentage / 100) * duration;
        }
    };

    function updatePlayButton(url, isPlaying) {
        var playBtn = document.querySelector('.agency-rec-row[data-url="' + url.replace(/'/g, "\\'") + '"] .audio-play-btn');
        var pauseBtn = document.querySelector('.agency-rec-row[data-url="' + url.replace(/'/g, "\\'") + '"] .audio-pause-btn');
        if (playBtn) playBtn.style.display = isPlaying ? 'none' : 'inline-flex';
        if (pauseBtn) pauseBtn.style.display = isPlaying ? 'inline-flex' : 'none';
    }

    function updateDurationDisplay(url, duration) {
        var durationEl = document.querySelector('.agency-rec-row[data-url="' + url.replace(/'/g, "\\'") + '"] .audio-duration');
        if (durationEl) {
            durationEl.textContent = formatTime(duration);
        }
    }

    function updateCurrentTimeDisplay(url, currentTime) {
        var timeEl = document.querySelector('.agency-rec-row[data-url="' + url.replace(/'/g, "\\'") + '"] .audio-current-time');
        if (timeEl) {
            timeEl.textContent = formatTime(currentTime);
        }
    }

    function updateScrubber(url, currentTime, duration) {
        var scrubber = document.querySelector('.agency-rec-row[data-url="' + url.replace(/'/g, "\\'") + '"] .audio-scrubber');
        if (scrubber) {
            var percentage = duration > 0 ? (currentTime / duration) * 100 : 0;
            scrubber.value = percentage;
        }
    }

    window.transcribeRecording = function(url, callSid) {
        var btn = document.querySelector('.agency-rec-row[data-url="' + url.replace(/'/g, "\\'") + '"] .audio-transcribe-btn');
        if (btn) {
            btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
            btn.disabled = true;
        }

        fetch('/voice/transcribe-recording', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({call_sid: callSid, recording_url: url})
        })
        .then(function(r) { return r.json(); })
        .then(function(d) {
            if (d.transcript) {
                if (typeof _showDashToast === 'function') _showDashToast(true, 'Transcription complete');
                if (typeof showTranscript === 'function') showTranscript(d.transcript);
            } else {
                if (typeof _showDashToast === 'function') _showDashToast(false, d.error || 'Transcription failed');
            }
        })
        .catch(function(e) {
            console.error('Transcription error:', e);
            if (typeof _showDashToast === 'function') _showDashToast(false, 'Transcription failed');
        })
        .finally(function() {
            if (btn) {
                btn.innerHTML = '<i class="fa-solid fa-file-lines"></i>';
                btn.disabled = false;
            }
        });
    };

    // ── Close dropdowns on outside click ────────────────────────────────────
    document.addEventListener('click', function() {
        ['akPeriodDropdown', 'akMemberDropdown'].forEach(function(id) {
            var dd = document.getElementById(id);
            if (dd) dd.classList.remove('open');
        });
        var pt = document.getElementById('akPeriodTrigger');
        var mt = document.getElementById('akMemberTrigger');
        if (pt) pt.classList.remove('open');
        if (mt) mt.classList.remove('open');
    });

})();
