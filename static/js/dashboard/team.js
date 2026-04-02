// static/js/dashboard/team.js — Team (seat user) management module

var _teamMembers = [];
var _teamGhlUsers = [];
var _teamRoles = [];

var PERM_LABELS = {
    can_dial:                'Can use dialer',
    can_text:                'Can send SMS',
    can_view_all_leads:      'View all leads (not just assigned)',
    can_import_leads:        'Import leads',
    can_change_bot_config:   'Change bot configuration',
    can_view_call_recordings:'View call recordings',
    can_manage_numbers:      'Manage phone numbers',
    can_view_billing:        'View billing info',
    can_invite_users:        'Invite other users',
};

var ROLE_ICONS = {
    admin:   'fa-shield-halved',
    manager: 'fa-user-tie',
    agent:   'fa-headset',
};

var ROLE_COLORS = {
    admin:   '#f59e0b',
    manager: '#3b82f6',
    agent:   '#888',
};

// ── Load Members ─────────────────────────────────────────────────────────────

function teamLoadMembers() {
    fetch('/api/team/members')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.error) return;
            _teamMembers = data.members || [];
            teamRenderMembers();
            teamUpdateStats();
            // Update seat limit display
            var limitEl = document.getElementById('teamSeatLimit');
            if (limitEl) {
                limitEl.textContent = (data.active_seats || 0) + ' / ' + (data.max_seats || 10);
            }
            // Show invite button if there are paid seats with no invite sent yet
            var paidCount = _teamMembers.filter(function(m) { return m.has_paid_seat; }).length;
            var invitedCount = _teamMembers.filter(function(m) { return m.is_active; }).length;
            var invBtn = document.getElementById('teamInviteUserBtn');
            if (invBtn) {
                // Show invite button if they already have seats (to invite pending ones)
                invBtn.style.display = _teamMembers.length > 0 ? '' : 'none';
            }
        })
        .catch(function(e) { console.error('Team load error:', e); });
}

function teamUpdateStats() {
    var active = 0, pending = 0, voice = 0, inactive = 0;
    _teamMembers.forEach(function(m) {
        if (!m.is_active) { inactive++; return; }
        if (m.onboarding_status === 'invited') pending++;
        else active++;
        if (m.voice_activated) voice++;
    });
    var el;
    el = document.getElementById('teamStatActive');   if (el) el.textContent = active;
    el = document.getElementById('teamStatPending');   if (el) el.textContent = pending;
    el = document.getElementById('teamStatVoice');     if (el) el.textContent = voice;
    el = document.getElementById('teamStatInactive');  if (el) el.textContent = inactive;
}

function teamRenderMembers() {
    var container = document.getElementById('teamMembersList');
    if (!container) return;

    if (_teamMembers.length === 0) {
        container.innerHTML =
            '<div style="text-align:center;padding:40px;">' +
            '<i class="fa-solid fa-user-group" style="font-size:2.5rem;margin-bottom:16px;display:block;color:var(--accent);opacity:0.5;"></i>' +
            '<h5 style="color:#fff;font-weight:700;margin-bottom:8px;">Build Your Team</h5>' +
            '<p style="color:#888;font-size:0.88rem;margin-bottom:20px;max-width:360px;margin-left:auto;margin-right:auto;">Add seat users so your agents can dial, text, and manage leads from their own dashboard. Each seat gets their own phone number, call history, and permissions.</p>' +
            '<div style="display:inline-flex;align-items:center;gap:8px;background:rgba(0,255,136,0.06);border:1px solid rgba(0,255,136,0.15);border-radius:12px;padding:12px 24px;margin-bottom:20px;">' +
            '<span style="font-size:1.8rem;font-weight:800;color:var(--accent);">$50</span>' +
            '<span style="color:#aaa;font-size:0.82rem;line-height:1.3;">/seat<br>per month</span>' +
            '</div><br>' +
            '<button onclick="teamAddSeat()" style="background:var(--accent);color:#000;font-weight:700;padding:12px 28px;border-radius:10px;font-size:0.92rem;border:none;cursor:pointer;">' +
            '<i class="fa-solid fa-plus me-2"></i>Add Your First Seat</button>' +
            '</div>';
        return;
    }

    var html = '';
    _teamMembers.forEach(function(m) {
        var statusColor = '#888';
        var statusText = 'Unknown';
        var statusIcon = 'fa-circle-question';

        if (!m.is_active) {
            statusColor = '#ef4444'; statusText = 'Deactivated'; statusIcon = 'fa-circle-xmark';
        } else if (m.onboarding_status === 'invited') {
            statusColor = '#facc15'; statusText = 'Invite Pending'; statusIcon = 'fa-clock';
        } else if (m.onboarding_status === 'claimed') {
            statusColor = '#22c55e'; statusText = 'Active'; statusIcon = 'fa-circle-check';
        }

        // Expiry countdown for pending invites
        var expiryBadge = '';
        if (m.onboarding_status === 'invited') {
            if (m.invite_expired) {
                expiryBadge = '<span style="background:rgba(239,68,68,0.15);color:#f87171;padding:2px 8px;border-radius:6px;font-size:0.7rem;font-weight:600;">EXPIRED</span>';
            } else if (m.invite_expires_in_hours !== undefined) {
                var hrs = m.invite_expires_in_hours;
                var timeStr = hrs >= 24 ? Math.floor(hrs / 24) + 'd ' + Math.round(hrs % 24) + 'h' : Math.round(hrs) + 'h';
                expiryBadge = '<span style="background:rgba(250,204,21,0.12);color:#facc15;padding:2px 8px;border-radius:6px;font-size:0.7rem;font-weight:600;">Expires in ' + timeStr + '</span>';
            }
        }

        // Role badge
        var roleIcon = ROLE_ICONS[m.role] || 'fa-user';
        var roleColor = ROLE_COLORS[m.role] || '#888';
        var roleBadge = '<span style="color:' + roleColor + ';font-size:0.72rem;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;"><i class="fa-solid ' + roleIcon + ' me-1"></i>' + (m.role || 'agent') + '</span>';

        var voiceBadge = '';
        if (m.voice_activated) {
            voiceBadge = '<span style="background:rgba(0,255,136,0.12);color:#00ff88;padding:2px 8px;border-radius:6px;font-size:0.72rem;font-weight:600;"><i class="fa-solid fa-phone me-1"></i>' + (m.phone_number || 'No Number') + '</span>';
        }

        html +=
            '<div class="glass-panel" style="padding:16px 20px;border-radius:14px;display:flex;align-items:center;gap:14px;flex-wrap:wrap;">' +
                '<div style="width:38px;height:38px;border-radius:50%;background:rgba(255,255,255,0.06);display:flex;align-items:center;justify-content:center;flex-shrink:0;">' +
                    '<i class="fa-solid ' + roleIcon + '" style="color:' + roleColor + ';font-size:0.9rem;"></i>' +
                '</div>' +
                '<div style="flex:1;min-width:140px;">' +
                    '<div style="font-weight:600;color:#fff;font-size:0.9rem;">' + (m.full_name || m.email) + '</div>' +
                    '<div style="font-size:0.78rem;color:#666;display:flex;gap:8px;align-items:center;flex-wrap:wrap;">' + m.email + ' ' + roleBadge + '</div>' +
                '</div>' +
                '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">' +
                    '<span style="color:' + statusColor + ';font-size:0.78rem;font-weight:600;"><i class="fa-solid ' + statusIcon + ' me-1"></i>' + statusText + '</span>' +
                    expiryBadge +
                    voiceBadge +
                '</div>' +
                '<div style="display:flex;gap:6px;flex-shrink:0;">' +
                    (m.onboarding_status === 'invited' ?
                        '<button onclick="teamResendInvite(' + m.id + ')" title="Resend invite" style="background:rgba(250,204,21,0.1);border:1px solid rgba(250,204,21,0.2);color:#facc15;width:32px;height:32px;border-radius:8px;cursor:pointer;font-size:0.78rem;"><i class="fa-solid fa-paper-plane"></i></button>' : '') +
                    (m.is_active && m.onboarding_status === 'claimed' && !m.voice_activated ?
                        '<button onclick="teamActivateVoice(' + m.id + ')" title="Activate Voice" style="background:rgba(0,255,136,0.1);border:1px solid rgba(0,255,136,0.2);color:#00ff88;width:32px;height:32px;border-radius:8px;cursor:pointer;font-size:0.78rem;"><i class="fa-solid fa-phone"></i></button>' : '') +
                    '<button onclick="teamShowRoleModal(' + m.id + ')" title="Change Role" style="background:rgba(59,130,246,0.1);border:1px solid rgba(59,130,246,0.2);color:#3b82f6;width:32px;height:32px;border-radius:8px;cursor:pointer;font-size:0.78rem;"><i class="fa-solid fa-user-tag"></i></button>' +
                    '<button onclick="teamShowPerms(' + m.id + ')" title="Permissions" style="background:rgba(250,204,21,0.1);border:1px solid rgba(250,204,21,0.2);color:#facc15;width:32px;height:32px;border-radius:8px;cursor:pointer;font-size:0.78rem;"><i class="fa-solid fa-shield-halved"></i></button>' +
                    (m.is_active ?
                        '<button onclick="teamToggleActive(' + m.id + ',false)" title="Deactivate" style="background:rgba(239,68,68,0.1);border:1px solid rgba(239,68,68,0.2);color:#ef4444;width:32px;height:32px;border-radius:8px;cursor:pointer;font-size:0.78rem;"><i class="fa-solid fa-user-slash"></i></button>' :
                        '<button onclick="teamToggleActive(' + m.id + ',true)" title="Reactivate" style="background:rgba(34,197,94,0.1);border:1px solid rgba(34,197,94,0.2);color:#22c55e;width:32px;height:32px;border-radius:8px;cursor:pointer;font-size:0.78rem;"><i class="fa-solid fa-user-check"></i></button>') +
                '</div>' +
            '</div>';
    });

    container.innerHTML = html;
}

// ── GHL Users Detection ──────────────────────────────────────────────────────

function teamCheckGhlUsers() {
    var banner = document.getElementById('teamGhlBanner');
    var text = document.getElementById('teamGhlBannerText');
    var syncBtn = document.getElementById('teamGhlSyncBtn');
    // Always show the banner so user can scan for GHL users
    if (banner) banner.style.display = 'block';
    if (text) text.textContent = 'Scanning GHL for users...';
    if (syncBtn) syncBtn.disabled = true;

    fetch('/api/team/ghl-users')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (syncBtn) syncBtn.disabled = false;
            if (data.error || !data.users) {
                if (text) text.textContent = 'Could not reach GHL. Click Scan to retry.';
                return;
            }
            _teamGhlUsers = data.users;
            if (data.count > 1) {
                var myEmail = (window.DASHBOARD_BOOT?.userEmail || '').toLowerCase();
                var existingEmails = {};
                _teamMembers.forEach(function(m) { existingEmails[m.email.toLowerCase()] = true; });
                var otherUsers = data.users.filter(function(u) {
                    return u.email.toLowerCase() !== myEmail && !existingEmails[u.email.toLowerCase()];
                });
                if (otherUsers.length > 0) {
                    var names = otherUsers.map(function(u) { return u.name || u.email; });
                    var nameStr = names.length <= 3 ? names.join(', ') : names.slice(0, 3).join(', ') + ' +' + (names.length - 3) + ' more';
                    if (text) text.innerHTML = '<strong>' + otherUsers.length + ' user' + (otherUsers.length > 1 ? 's' : '') + '</strong> found on this GHL location: ' + nameStr + '. You can invite them as seat users.';
                } else {
                    var available = data.count - 1;
                    if (text) text.textContent = available + ' other user' + (available > 1 ? 's' : '') + ' found on this GHL location (already invited). Add more users in GHL, then click Scan.';
                }
            } else {
                if (text) text.textContent = 'No other users found on this GHL location. Add users in GHL, then click Scan.';
            }
            teamPopulateGhlDropdown();
        })
        .catch(function() {
            if (syncBtn) syncBtn.disabled = false;
            if (text) text.textContent = 'Could not reach GHL. Click Scan to retry.';
        });
}

function teamPopulateGhlDropdown() {
    var picker = document.getElementById('teamGhlUserPicker');
    var select = document.getElementById('teamGhlSelect');
    if (!picker || !select || _teamGhlUsers.length <= 1) return;

    var existingEmails = {};
    _teamMembers.forEach(function(m) { existingEmails[m.email.toLowerCase()] = true; });
    var myEmail = (window.DASHBOARD_BOOT?.userEmail || '').toLowerCase();

    var options = '<option value="">-- Choose a GHL user or enter manually --</option>';
    _teamGhlUsers.forEach(function(u) {
        if (existingEmails[u.email.toLowerCase()] || u.email.toLowerCase() === myEmail) return;
        options += '<option value="' + u.id + '" data-email="' + u.email + '" data-name="' + u.name + '">' + u.name + ' (' + u.email + ')</option>';
    });

    select.innerHTML = options;
    picker.style.display = 'block';
}

function teamFillFromGhl() {
    var select = document.getElementById('teamGhlSelect');
    if (!select) return;
    var opt = select.options[select.selectedIndex];
    if (!opt || !opt.value) return;
    document.getElementById('teamInviteEmail').value = opt.dataset.email || '';
    document.getElementById('teamInviteName').value = opt.dataset.name || '';
    document.getElementById('teamInviteGhlId').value = opt.value;
}

function teamSyncGhlUsers() {
    teamCheckGhlUsers();
}

// ── Add Seat (Stripe Checkout) ──────────────────────────────────────────────

async function teamStartMeet() {
    const btn = document.querySelector('.team-meet-btn');
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-2"></i>Creating...'; }
    try {
        const r = await fetch('/google-calendar/meet/team', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ summary: 'Team Meeting' })
        });
        const d = await r.json();
        if (!r.ok) {
            if (typeof _showDashToast === 'function') _showDashToast(false, d.error || 'Failed to create meeting');
        } else if (d.meet_link) {
            window.open(d.meet_link, '_blank');
            if (typeof _showDashToast === 'function') _showDashToast(true, 'Team meeting created — ' + d.attendee_count + ' members invited');
        }
    } catch(e) {
        if (typeof _showDashToast === 'function') _showDashToast(false, 'Network error creating meeting');
    }
    if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-video me-2"></i>Team Meeting'; }
}

function teamAddSeat() {
    if (typeof _showDashToast === 'function') _showDashToast(true, 'Redirecting to checkout...');
    fetch('/api/team/checkout', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({})
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        if (data.error) {
            if (typeof _showDashToast === 'function') _showDashToast(false, data.error);
            return;
        }
        if (data.checkout_url) {
            window.location.href = data.checkout_url;
        }
    })
    .catch(function(e) {
        if (typeof _showDashToast === 'function') _showDashToast(false, 'Failed to start checkout');
    });
}

// ── Invite Modal ─────────────────────────────────────────────────────────────

function teamShowInviteModal() {
    document.getElementById('teamInviteEmail').value = '';
    document.getElementById('teamInviteName').value = '';
    document.getElementById('teamInviteGhlId').value = '';
    var modal = document.getElementById('teamInviteModal');
    if (modal) modal.style.display = 'flex';
    teamPopulateGhlDropdown();
}

function teamCloseInviteModal() {
    var modal = document.getElementById('teamInviteModal');
    if (modal) modal.style.display = 'none';
}

function teamSendInvite() {
    var email = (document.getElementById('teamInviteEmail').value || '').trim();
    var name = (document.getElementById('teamInviteName').value || '').trim();
    var ghlId = (document.getElementById('teamInviteGhlId').value || '').trim();
    var roleSelect = document.getElementById('teamInviteRole');
    var role = roleSelect ? roleSelect.value : 'agent';

    if (!email) {
        if (typeof _showDashToast === 'function') _showDashToast(false, 'Email is required');
        return;
    }

    var btn = document.getElementById('teamInviteBtn');
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-2"></i>Sending...'; }

    fetch('/api/team/invite', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({email: email, full_name: name, ghl_user_id: ghlId, role: role})
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-paper-plane me-2"></i>Send Invite'; }
        if (data.error) {
            if (typeof _showDashToast === 'function') _showDashToast(false, data.error);
            return;
        }
        if (typeof _showDashToast === 'function') _showDashToast(true, data.message || 'Invite sent!');
        teamCloseInviteModal();
        teamLoadMembers();
    })
    .catch(function(e) {
        if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-paper-plane me-2"></i>Send Invite'; }
        if (typeof _showDashToast === 'function') _showDashToast(false, 'Failed to send invite');
    });
}

// ── Resend Invite ────────────────────────────────────────────────────────────

function teamResendInvite(memberId) {
    fetch('/api/team/resend-invite', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({member_id: memberId})
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        if (typeof _showDashToast === 'function') _showDashToast(!data.error, data.message || data.error);
        teamLoadMembers();
    });
}

// ── Role Modal ───────────────────────────────────────────────────────────────

function teamShowRoleModal(memberId) {
    var member = null;
    _teamMembers.forEach(function(m) { if (m.id === memberId) member = m; });
    if (!member) return;

    document.getElementById('teamRoleMemberId').value = memberId;
    document.getElementById('teamRoleName').textContent = (member.full_name || member.email);

    // Highlight current role
    document.querySelectorAll('.team-role-option').forEach(function(el) {
        el.classList.remove('team-role-selected');
        if (el.dataset.role === member.role) el.classList.add('team-role-selected');
    });

    document.getElementById('teamRoleModal').style.display = 'flex';
}

function teamCloseRoleModal() {
    document.getElementById('teamRoleModal').style.display = 'none';
}

function teamSelectRole(el) {
    document.querySelectorAll('.team-role-option').forEach(function(o) { o.classList.remove('team-role-selected'); });
    el.classList.add('team-role-selected');
}

function teamSaveRole() {
    var memberId = parseInt(document.getElementById('teamRoleMemberId').value);
    var selected = document.querySelector('.team-role-selected');
    if (!selected) return;
    var role = selected.dataset.role;

    fetch('/api/team/role', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({member_id: memberId, role: role})
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        if (data.error) {
            if (typeof _showDashToast === 'function') _showDashToast(false, data.error);
            return;
        }
        if (typeof _showDashToast === 'function') _showDashToast(true, 'Role updated to ' + role);
        teamCloseRoleModal();
        teamLoadMembers();
    });
}

// ── Permissions Modal ────────────────────────────────────────────────────────

function teamShowPerms(memberId) {
    var member = null;
    _teamMembers.forEach(function(m) { if (m.id === memberId) member = m; });
    if (!member) return;

    document.getElementById('teamPermsMemberId').value = memberId;
    document.getElementById('teamPermsName').textContent = (member.full_name || member.email) + '\'s permissions';

    var container = document.getElementById('teamPermsChecks');
    var perms = member.permissions || {};
    var html = '';

    Object.keys(PERM_LABELS).forEach(function(key) {
        var checked = perms[key] ? 'checked' : '';
        html +=
            '<label style="display:flex;align-items:center;gap:10px;cursor:pointer;padding:8px 12px;border-radius:8px;background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.05);">' +
                '<input type="checkbox" id="perm_' + key + '" ' + checked + ' style="accent-color:var(--accent);width:16px;height:16px;cursor:pointer;">' +
                '<span style="font-size:0.85rem;color:#ccc;">' + PERM_LABELS[key] + '</span>' +
            '</label>';
    });

    container.innerHTML = html;
    document.getElementById('teamPermsModal').style.display = 'flex';
}

function teamClosePermsModal() {
    document.getElementById('teamPermsModal').style.display = 'none';
}

function teamSavePerms() {
    var memberId = parseInt(document.getElementById('teamPermsMemberId').value);
    var perms = {};
    Object.keys(PERM_LABELS).forEach(function(key) {
        var cb = document.getElementById('perm_' + key);
        perms[key] = cb ? cb.checked : false;
    });

    fetch('/api/team/permissions', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({member_id: memberId, permissions: perms})
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        if (data.error) {
            if (typeof _showDashToast === 'function') _showDashToast(false, data.error);
            return;
        }
        if (typeof _showDashToast === 'function') _showDashToast(true, 'Permissions updated');
        teamClosePermsModal();
        teamLoadMembers();
    });
}

// ── Toggle Active ────────────────────────────────────────────────────────────

function teamToggleActive(memberId, activate) {
    if (!activate && !confirm('Deactivate this user? They will lose access immediately.')) return;

    fetch('/api/team/toggle-active', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({member_id: memberId, is_active: activate})
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        if (typeof _showDashToast === 'function') _showDashToast(!data.error, data.message || data.error);
        teamLoadMembers();
    });
}

// ── Activate Voice ───────────────────────────────────────────────────────────

function teamActivateVoice(memberId) {
    if (!confirm('This will provision a voice account for this user. Continue?')) return;

    fetch('/api/team/activate-voice', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({member_id: memberId})
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
        if (typeof _showDashToast === 'function') _showDashToast(!data.error, data.message || data.error);
        teamLoadMembers();
    });
}

// ── Agent KPIs ───────────────────────────────────────────────────────────────

function teamLoadKPIs(period) {
    period = period || 'week';
    var container = document.getElementById('teamKpisContent');
    if (!container) return;

    container.innerHTML = '<div style="text-align:center;padding:20px;color:#555;"><i class="fa-solid fa-spinner fa-spin"></i> Loading KPIs...</div>';

    fetch('/api/team/agent-kpis?period=' + period)
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.error) { container.innerHTML = '<div style="color:#f87171;padding:12px;">' + data.error + '</div>'; return; }
            var agents = data.agents || [];
            if (agents.length === 0) {
                container.innerHTML = '<div style="text-align:center;padding:20px;color:#555;">No active agents yet.</div>';
                return;
            }

            var html = '<div style="overflow-x:auto;"><table style="width:100%;border-collapse:collapse;font-size:0.82rem;">' +
                '<thead><tr style="color:#888;text-align:left;border-bottom:1px solid rgba(255,255,255,0.06);">' +
                '<th style="padding:10px 8px;">Agent</th>' +
                '<th style="padding:10px 8px;text-align:center;">Calls</th>' +
                '<th style="padding:10px 8px;text-align:center;">Connected</th>' +
                '<th style="padding:10px 8px;text-align:center;">Rate</th>' +
                '<th style="padding:10px 8px;text-align:center;">Talk Time</th>' +
                '<th style="padding:10px 8px;text-align:center;">Avg Call</th>' +
                '<th style="padding:10px 8px;text-align:center;">Dials/Day</th>' +
                '<th style="padding:10px 8px;">Last Call</th>' +
                '</tr></thead><tbody>';

            agents.forEach(function(a) {
                var rateColor = a.connect_rate >= 30 ? '#22c55e' : a.connect_rate >= 15 ? '#facc15' : '#f87171';
                var talkMin = Math.floor(a.total_talk_time / 60);
                var avgSec = Math.round(a.avg_talk_time);
                var lastCall = a.last_call ? new Date(a.last_call).toLocaleDateString() : '—';

                html += '<tr style="border-bottom:1px solid rgba(255,255,255,0.03);">' +
                    '<td style="padding:10px 8px;"><div style="font-weight:600;color:#fff;">' + (a.full_name || a.email) + '</div>' +
                    '<div style="font-size:0.72rem;color:#666;">' + (a.role || 'agent') + '</div></td>' +
                    '<td style="padding:10px 8px;text-align:center;color:#fff;font-weight:600;">' + a.total_calls + '</td>' +
                    '<td style="padding:10px 8px;text-align:center;color:#22c55e;font-weight:600;">' + a.connected_calls + '</td>' +
                    '<td style="padding:10px 8px;text-align:center;color:' + rateColor + ';font-weight:700;">' + a.connect_rate + '%</td>' +
                    '<td style="padding:10px 8px;text-align:center;color:#ccc;">' + talkMin + 'm</td>' +
                    '<td style="padding:10px 8px;text-align:center;color:#ccc;">' + avgSec + 's</td>' +
                    '<td style="padding:10px 8px;text-align:center;color:#00ff88;font-weight:600;">' + a.dials_per_day + '</td>' +
                    '<td style="padding:10px 8px;color:#888;">' + lastCall + '</td>' +
                    '</tr>';
            });

            html += '</tbody></table></div>';
            container.innerHTML = html;
        })
        .catch(function() { container.innerHTML = '<div style="color:#f87171;padding:12px;">Failed to load KPIs</div>'; });
}

// ── Audit Log ────────────────────────────────────────────────────────────────

function teamLoadAuditLog() {
    var container = document.getElementById('teamAuditContent');
    if (!container) return;

    fetch('/api/team/audit-log?limit=25')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.error || !data.entries || data.entries.length === 0) {
                container.innerHTML = '<div style="text-align:center;padding:20px;color:#555;">No audit entries yet.</div>';
                return;
            }

            var html = '';
            data.entries.forEach(function(e) {
                var icon = 'fa-circle-info';
                var color = '#888';
                if (e.action.includes('invite')) { icon = 'fa-paper-plane'; color = '#facc15'; }
                else if (e.action.includes('deactivated') || e.action.includes('removed')) { icon = 'fa-user-slash'; color = '#ef4444'; }
                else if (e.action.includes('activated')) { icon = 'fa-user-check'; color = '#22c55e'; }
                else if (e.action.includes('role')) { icon = 'fa-user-tag'; color = '#3b82f6'; }
                else if (e.action.includes('permission')) { icon = 'fa-shield-halved'; color = '#f59e0b'; }
                else if (e.action.includes('voice')) { icon = 'fa-phone'; color = '#00ff88'; }
                else if (e.action.includes('claimed')) { icon = 'fa-user-check'; color = '#22c55e'; }

                var time = e.created_at ? new Date(e.created_at).toLocaleString() : '';

                html += '<div style="display:flex;gap:10px;padding:8px 0;border-bottom:1px solid rgba(255,255,255,0.03);font-size:0.82rem;">' +
                    '<i class="fa-solid ' + icon + '" style="color:' + color + ';margin-top:3px;width:16px;text-align:center;"></i>' +
                    '<div style="flex:1;">' +
                    '<span style="color:#fff;">' + e.actor_email + '</span> ' +
                    '<span style="color:#888;">' + e.action.replace(/_/g, ' ') + '</span>' +
                    (e.target_email ? ' <span style="color:#ccc;">' + e.target_email + '</span>' : '') +
                    '</div>' +
                    '<span style="color:#555;font-size:0.75rem;white-space:nowrap;">' + time + '</span>' +
                    '</div>';
            });
            container.innerHTML = html;
        });
}

// ── Seat Onboarding (for seat users themselves) ──────────────────────────────

function teamLoadOnboarding() {
    if (!window.DASHBOARD_BOOT?.isSeatUser) return;

    fetch('/api/team/onboarding-status')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.error || data.all_done) return;

            var banner = document.getElementById('seatOnboardingBanner');
            if (!banner) {
                // Create onboarding banner at top of dashboard
                banner = document.createElement('div');
                banner.id = 'seatOnboardingBanner';
                banner.style.cssText = 'background:rgba(0,255,136,0.06);border:1px solid rgba(0,255,136,0.18);border-radius:14px;padding:18px 22px;margin-bottom:20px;';

                var mainWrapper = document.querySelector('.tab-content') || document.querySelector('#main-content');
                if (mainWrapper) mainWrapper.insertBefore(banner, mainWrapper.firstChild);
            }

            var html = '<div style="display:flex;align-items:center;gap:10px;margin-bottom:14px;">' +
                '<i class="fa-solid fa-rocket" style="color:var(--accent);font-size:1.2rem;"></i>' +
                '<span style="color:#fff;font-weight:700;font-size:0.95rem;">Getting Started</span>' +
                '<span style="color:#888;font-size:0.82rem;margin-left:auto;">' + data.completed + '/' + data.total + ' completed</span>' +
                '</div>';

            // Progress bar
            var pct = Math.round((data.completed / data.total) * 100);
            html += '<div style="background:rgba(255,255,255,0.06);border-radius:6px;height:6px;margin-bottom:14px;overflow:hidden;">' +
                '<div style="background:var(--accent);height:100%;width:' + pct + '%;border-radius:6px;transition:width 0.3s;"></div></div>';

            html += '<div style="display:flex;flex-wrap:wrap;gap:10px;">';
            data.checklist.forEach(function(item) {
                var check = item.done ? '✓' : '○';
                var color = item.done ? '#22c55e' : '#888';
                html += '<div style="display:flex;align-items:center;gap:8px;padding:8px 14px;border-radius:8px;background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.05);flex:1;min-width:200px;">' +
                    '<span style="color:' + color + ';font-weight:700;">' + check + '</span>' +
                    '<i class="fa-solid ' + item.icon + '" style="color:' + (item.done ? '#22c55e' : '#555') + ';"></i>' +
                    '<span style="color:' + (item.done ? '#ccc' : '#fff') + ';font-size:0.85rem;">' + item.label + '</span>';
                if (item.action && !item.done) {
                    html += '<button onclick="' + (item.action_tab ? "sidebarNavigate('" + item.action_tab + "')" : "fetch('" + item.action_url + "',{method:'POST'}).then(function(){teamLoadOnboarding()})") +
                        '" style="background:var(--accent);color:#000;border:none;padding:4px 10px;border-radius:6px;font-size:0.75rem;font-weight:700;cursor:pointer;margin-left:auto;">' + item.action + '</button>';
                }
                html += '</div>';
            });
            html += '</div>';

            banner.innerHTML = html;
        })
        .catch(function() {});
}

// ── Team Tab Panels ──────────────────────────────────────────────────────────

function teamSwitchPanel(panel) {
    document.querySelectorAll('.team-panel-btn').forEach(function(b) { b.classList.remove('active'); });
    document.querySelectorAll('.team-panel-content').forEach(function(p) { p.style.display = 'none'; });

    var btn = document.querySelector('[data-panel="' + panel + '"]');
    if (btn) btn.classList.add('active');
    var el = document.getElementById('teamPanel_' + panel);
    if (el) el.style.display = 'block';

    if (panel === 'kpis') teamLoadKPIs('week');
    if (panel === 'audit') teamLoadAuditLog();
}

// ── Init on tab switch ───────────────────────────────────────────────────────

var _teamLoaded = false;
function teamInit() {
    if (_teamLoaded) return;
    _teamLoaded = true;
    teamLoadMembers();
    teamCheckGhlUsers();

    // Load onboarding for seat users
    if (window.DASHBOARD_BOOT?.isSeatUser) {
        teamLoadOnboarding();
    }

    // Auto-open invite modal after Stripe seat purchase
    var params = new URLSearchParams(window.location.search);
    if (params.get('seat_added') === '1') {
        setTimeout(function() {
            teamShowInviteModal();
            if (typeof _showDashToast === 'function') _showDashToast(true, 'Seat purchased! Now invite your team member.');
        }, 500);
        // Clean URL
        var url = new URL(window.location);
        url.searchParams.delete('seat_added');
        window.history.replaceState({}, '', url);
    }
}

// Auto-init onboarding banner for seat users on page load
document.addEventListener('DOMContentLoaded', function() {
    if (window.DASHBOARD_BOOT?.isSeatUser) {
        teamLoadOnboarding();
    }

    var teamTab = document.getElementById('team');
    if (teamTab) {
        var observer = new MutationObserver(function(mutations) {
            mutations.forEach(function(m) {
                if (m.attributeName === 'class' && teamTab.classList.contains('active')) {
                    teamInit();
                }
            });
        });
        observer.observe(teamTab, {attributes: true});
    }
});
