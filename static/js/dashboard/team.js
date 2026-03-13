// static/js/dashboard/team.js — Team (seat user) management module

var _teamMembers = [];
var _teamGhlUsers = [];

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

// ── Load Members ─────────────────────────────────────────────────────────────

function teamLoadMembers() {
    fetch('/api/team/members')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.error) return;
            _teamMembers = data.members || [];
            teamRenderMembers();
            teamUpdateStats();
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
            '<div style="text-align:center;padding:40px;color:#555;">' +
            '<i class="fa-solid fa-user-group" style="font-size:2rem;margin-bottom:12px;display:block;"></i>' +
            '<p style="margin:0;font-size:0.9rem;">No team members yet. Invite your first agent!</p>' +
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

        var voiceBadge = '';
        if (m.voice_activated) {
            voiceBadge = '<span style="background:rgba(0,217,255,0.12);color:#00d9ff;padding:2px 8px;border-radius:6px;font-size:0.72rem;font-weight:600;"><i class="fa-solid fa-phone me-1"></i>' + (m.phone_number || 'No Number') + '</span>';
        }

        html +=
            '<div class="glass-panel" style="padding:16px 20px;border-radius:14px;display:flex;align-items:center;gap:14px;flex-wrap:wrap;">' +
                '<div style="width:38px;height:38px;border-radius:50%;background:rgba(255,255,255,0.06);display:flex;align-items:center;justify-content:center;flex-shrink:0;">' +
                    '<i class="fa-solid fa-user" style="color:#888;font-size:0.9rem;"></i>' +
                '</div>' +
                '<div style="flex:1;min-width:140px;">' +
                    '<div style="font-weight:600;color:#fff;font-size:0.9rem;">' + (m.full_name || m.email) + '</div>' +
                    '<div style="font-size:0.78rem;color:#666;">' + m.email + '</div>' +
                '</div>' +
                '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">' +
                    '<span style="color:' + statusColor + ';font-size:0.78rem;font-weight:600;"><i class="fa-solid ' + statusIcon + ' me-1"></i>' + statusText + '</span>' +
                    voiceBadge +
                '</div>' +
                '<div style="display:flex;gap:6px;flex-shrink:0;">' +
                    (m.onboarding_status === 'invited' ?
                        '<button onclick="teamResendInvite(' + m.id + ')" title="Resend invite" style="background:rgba(250,204,21,0.1);border:1px solid rgba(250,204,21,0.2);color:#facc15;width:32px;height:32px;border-radius:8px;cursor:pointer;font-size:0.78rem;"><i class="fa-solid fa-paper-plane"></i></button>' : '') +
                    (m.is_active && m.onboarding_status === 'claimed' && !m.voice_activated ?
                        '<button onclick="teamActivateVoice(' + m.id + ')" title="Activate Voice" style="background:rgba(0,217,255,0.1);border:1px solid rgba(0,217,255,0.2);color:#00d9ff;width:32px;height:32px;border-radius:8px;cursor:pointer;font-size:0.78rem;"><i class="fa-solid fa-phone"></i></button>' : '') +
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
    fetch('/api/team/ghl-users')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.error || !data.users) return;
            _teamGhlUsers = data.users;
            if (data.count > 1) {
                var banner = document.getElementById('teamGhlBanner');
                var text = document.getElementById('teamGhlBannerText');
                if (banner && text) {
                    text.textContent = data.count + ' users found on this GHL location. You can invite them as seat users.';
                    banner.style.display = 'block';
                }
            }
            // Populate invite modal dropdown
            teamPopulateGhlDropdown();
        })
        .catch(function() {});
}

function teamPopulateGhlDropdown() {
    var picker = document.getElementById('teamGhlUserPicker');
    var select = document.getElementById('teamGhlSelect');
    if (!picker || !select || _teamGhlUsers.length <= 1) return;

    // Filter out users that are already members
    var existingEmails = {};
    _teamMembers.forEach(function(m) { existingEmails[m.email.toLowerCase()] = true; });
    // Also filter out the current user
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
    if (typeof _showDashToast === 'function') _showDashToast(true, 'Syncing GHL users...');
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

    if (!email) {
        if (typeof _showDashToast === 'function') _showDashToast(false, 'Email is required');
        return;
    }

    var btn = document.getElementById('teamInviteBtn');
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-2"></i>Sending...'; }

    fetch('/api/team/invite', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({email: email, full_name: name, ghl_user_id: ghlId})
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
    if (!activate && !confirm('Deactivate this user? They will lose access to the dashboard.')) return;

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
    if (!confirm('This will create a Twilio sub-account for this user. Continue?')) return;

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

// ── Init on tab switch ───────────────────────────────────────────────────────

var _teamLoaded = false;
function teamInit() {
    if (_teamLoaded) return;
    _teamLoaded = true;
    teamLoadMembers();
    teamCheckGhlUsers();
}

// Auto-init when Team tab is shown
document.addEventListener('DOMContentLoaded', function() {
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
