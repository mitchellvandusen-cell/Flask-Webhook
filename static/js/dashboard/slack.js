// ─── Slack State ─────────────────────────────────────────────────────────────
const Slack = {
    connected: false,
    user: null,
    workspaces: [],
    activeChannelId: null,
    activeChannelName: null,
    activeWorkspaceName: null,
    channelUnread: {},
    lastMessageIds: {},
    totalUnread: 0,
    pollTimer: null,
    sending: false,
    lastRendered: {},
    channels: [],          // cached channel list
};

// ─── Init ────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', loadSlackStatus);

function loadSlackStatus() {
    fetch('/api/slack/status')
        .then(r => r.json())
        .then(data => {
            if (!data.connected) return;
            Slack.connected = true;
            Slack.user = data.user;
            Slack.workspaces = data.workspaces || [];
            showSlackConnected(data.user, data.team_icon);
            document.getElementById('slackBellBtn').style.display = 'flex';
            loadSlackChannels();
            startSlackBgNotifPoll();
        })
        .catch(() => {});
}

function showSlackConnected(user, teamIcon) {
    document.getElementById('slackNotConnected').style.display = 'none';
    document.getElementById('slackConnected').style.display = 'block';
    const avatarEl = document.getElementById('slackUserAvatar');
    if (teamIcon) {
        avatarEl.style.backgroundImage = `url(${teamIcon})`;
        avatarEl.style.backgroundSize = 'cover';
        avatarEl.style.backgroundPosition = 'center';
        avatarEl.innerHTML = '';
    } else {
        avatarEl.innerHTML = (user.team_name || user.name || 'S')[0].toUpperCase();
    }
    document.getElementById('slackUsername').textContent =
        user.name || 'Slack User';
    document.getElementById('slackTeamName').textContent =
        user.team_name || '';
}

// ─── Channel loading ─────────────────────────────────────────────────────────
function loadSlackChannels() {
    const listEl = document.getElementById('slackChannelList');
    listEl.innerHTML = slackSkeletonChannels();

    fetch('/api/slack/channels')
        .then(r => r.json())
        .then(data => {
            if (data.needs_reconnect) {
                listEl.innerHTML = `<div style="padding:8px 12px;">
                    <a href="/slack/connect" style="color:#36c5f0;font-size:0.82rem;">
                        <i class="fa-brands fa-slack me-1"></i>Reconnect Slack
                    </a></div>`;
                return;
            }
            if (data.error) {
                listEl.innerHTML = `<div style="padding:6px 14px;font-size:0.8rem;color:#f0a500;line-height:1.5;">
                    <i class="fa-solid fa-triangle-exclamation me-1"></i>${_slackEscapeHtml(data.error)}</div>`;
                return;
            }
            const channels = data.channels || [];
            Slack.channels = channels;
            Slack.activeWorkspaceName = data.team_name || '';
            if (!channels.length) {
                listEl.innerHTML = '<div style="padding:6px 10px;font-size:0.78rem;color:#444;">No channels found. Make sure the bot is added to channels.</div>';
                return;
            }
            listEl.innerHTML = channels.map(ch => {
                const unread = Slack.channelUnread[ch.id] || 0;
                return `
                <button class="slack-channel-btn" id="slkChBtn_${ch.id}"
                    onclick="openSlackChannel('${ch.id}','${_slackEscapeJs(ch.name)}')">
                    <span class="ch-hash">#</span>
                    <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${_slackEscapeHtml(ch.name)}</span>
                    ${unread ? `<span class="slack-notif-count ch-notif">${unread > 99 ? '99+' : unread}</span>` : ''}
                </button>`;
            }).join('');
        })
        .catch(() => {
            listEl.innerHTML = `<div style="padding:6px 14px;font-size:0.8rem;color:#f0a500;">
                <i class="fa-solid fa-wifi me-1"></i>Network error loading channels.</div>`;
        });
}

function slackSkeletonChannels() {
    return [1, 2, 3, 4, 5].map(() => `
        <div class="slack-ch-skel" style="padding:5px 10px;display:flex;align-items:center;gap:8px;">
            <div class="skel" style="width:12px;height:12px;border-radius:3px;flex-shrink:0;"></div>
            <div class="skel skel-line" style="flex:1;height:10px;"></div>
        </div>`).join('');
}

// ─── Message Panel ───────────────────────────────────────────────────────────
function openSlackChannel(channelId, channelName) {
    document.querySelectorAll('.slack-channel-btn').forEach(b => b.classList.remove('active-channel'));
    const btn = document.getElementById(`slkChBtn_${channelId}`);
    if (btn) btn.classList.add('active-channel');

    Slack.activeChannelId = channelId;
    Slack.activeChannelName = channelName;
    Slack.lastRendered[channelId] = null;

    const prev = Slack.channelUnread[channelId] || 0;
    Slack.channelUnread[channelId] = 0;
    updateSlackChannelNotif(channelId, 0);
    Slack.totalUnread = Math.max(0, Slack.totalUnread - prev);
    updateSlackBell();

    document.getElementById('slackPanelChannelName').textContent = channelName;
    document.getElementById('slackPanelWorkspaceName').textContent = Slack.activeWorkspaceName || '';
    document.getElementById('slackReplyText').placeholder = `Message #${channelName}…`;

    document.getElementById('slackPanel').classList.add('open');
    document.body.classList.add('slack-open');
    updateSlackToggleBtn(true);
    fetchSlackMessages(channelId, true);
    startSlackPoll();
}

function openLastSlackChannel() {
    if (Slack.activeChannelId) {
        document.getElementById('slackPanel').classList.add('open');
        document.body.classList.add('slack-open');
        updateSlackToggleBtn(true);
        return;
    }
    // Open first channel
    if (Slack.channels.length) {
        openSlackChannel(Slack.channels[0].id, Slack.channels[0].name);
    }
}

function closeSlackPanel() {
    document.getElementById('slackPanel').classList.remove('open');
    document.body.classList.remove('slack-open');
    updateSlackToggleBtn(false);
    stopSlackPoll();
    document.querySelectorAll('.slack-channel-btn').forEach(b => b.classList.remove('active-channel'));
    Slack.activeChannelId = null;
}

function toggleSlackPanel() {
    const panel = document.getElementById('slackPanel');
    if (panel.classList.contains('open')) {
        closeSlackPanel();
    } else {
        openLastSlackChannel();
    }
}

function updateSlackToggleBtn(isOpen) {
    const btn = document.getElementById('slackChatToggleBtn');
    if (!btn) return;
    btn.style.background = isOpen ? 'rgba(54,197,240,0.12)' : '';
    btn.style.color = isOpen ? '#fff' : '#36c5f0';
}

// ─── Message Fetching ────────────────────────────────────────────────────────
function fetchSlackMessages(channelId, initial) {
    const messagesEl = document.getElementById('slackMessages');
    if (initial) messagesEl.innerHTML = buildSlackMessageSkeleton();

    fetch(`/api/slack/messages/${channelId}`)
        .then(r => r.json())
        .then(data => {
            if (data.error) {
                messagesEl.innerHTML = `
                    <div class="slack-state-msg">
                        <div class="state-icon"><i class="fa-solid fa-triangle-exclamation"></i></div>
                        <span class="state-title">Couldn't load messages</span>
                        <span class="state-sub">${_slackEscapeHtml(data.error)}</span>
                    </div>`;
                return;
            }
            const messages = data.messages || [];
            if (initial) {
                Slack.lastRendered[channelId] = null;
                if (!messages.length) {
                    messagesEl.innerHTML = `
                        <div class="slack-state-msg">
                            <div class="state-icon"><i class="fa-regular fa-comment-dots"></i></div>
                            <span class="state-title">No messages yet</span>
                            <span class="state-sub">Be the first to say something in #${_slackEscapeHtml(Slack.activeChannelName || channelId)}.</span>
                        </div>`;
                } else {
                    messagesEl.innerHTML = '';
                    messages.forEach(m => appendSlackMessage(m, false, channelId));
                    scrollSlackToBottom();
                }
            } else {
                const lastTs = Slack.lastMessageIds[channelId];
                const newMsgs = lastTs ? messages.filter(m => parseFloat(m.id) > parseFloat(lastTs)) : [];
                if (newMsgs.length) {
                    const atBottom = isSlackAtBottom();
                    newMsgs.forEach(m => appendSlackMessage(m, true, channelId));
                    if (atBottom) scrollSlackToBottom();
                    else showSlackScrollBtn();
                }
            }
            if (messages.length) Slack.lastMessageIds[channelId] = messages[messages.length - 1].id;
        })
        .catch(() => {
            if (initial) {
                messagesEl.innerHTML = `
                    <div class="slack-state-msg">
                        <div class="state-icon"><i class="fa-solid fa-wifi"></i></div>
                        <span class="state-title">Connection error</span>
                        <span class="state-sub">Check your network and try again.</span>
                    </div>`;
            }
        });
}

function buildSlackMessageSkeleton() {
    const rows = [
        { av: 38, lines: [{ w: '30%' }, { w: '80%' }, { w: '60%' }] },
        { av: 38, lines: [{ w: '25%' }, { w: '55%' }] },
        { av: 38, lines: [{ w: '35%' }, { w: '70%' }, { w: '45%' }] },
    ];
    return rows.map(row => `
        <div data-skel style="display:flex;gap:14px;padding:14px 18px 4px;">
            <div class="skel skel-circle" style="width:${row.av}px;height:${row.av}px;flex-shrink:0;margin-top:2px;"></div>
            <div style="flex:1;">
                <div class="skel skel-line" style="width:${row.lines[0].w};margin-bottom:10px;"></div>
                ${row.lines.slice(1).map(l => `<div class="skel skel-line" style="width:${l.w};"></div>`).join('')}
            </div>
        </div>`).join('');
}

// ─── Grouped Message Rendering ───────────────────────────────────────────────
const SLACK_GROUP_GAP_MS = 5 * 60 * 1000;

function appendSlackMessage(msg, isNew, channelId) {
    const messagesEl = document.getElementById('slackMessages');
    const ph = messagesEl.querySelector('.slack-state-msg');
    if (ph) ph.remove();
    messagesEl.querySelectorAll('[data-skel]').forEach(el => el.remove());

    const ts    = msg.timestamp ? new Date(msg.timestamp) : null;
    const tsMs  = ts ? ts.getTime() : 0;
    const timeStr = ts ? ts.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';
    const relStr  = ts ? _slackRelativeTime(msg.timestamp) : '';
    const isSelf  = Slack.user && msg.author_id === Slack.user.slack_user_id;

    const prev = Slack.lastRendered[channelId];
    const isContinued = prev
        && prev.authorId === msg.author_id
        && (tsMs - prev.tsMs) < SLACK_GROUP_GAP_MS;

    Slack.lastRendered[channelId] = { authorId: msg.author_id, tsMs };

    const div = document.createElement('div');
    div.className = 'slack-msg' + (isContinued ? ' continued' : ' group-start') + (isNew ? ' new-msg' : '');
    div.setAttribute('data-msg-id', msg.id);

    const avatarHtml = msg.avatar_url
        ? `<img src="${msg.avatar_url}" class="slack-msg-avatar" alt="" loading="lazy">`
        : `<div class="slack-msg-avatar">${(msg.author || '?')[0].toUpperCase()}</div>`;

    const metaHtml = isContinued ? '' : `
        <div class="slack-msg-meta">
            <span class="slack-msg-author${isSelf ? ' is-self' : ''}">${_slackEscapeHtml(msg.author || 'Unknown')}</span>
            <span class="slack-msg-time" title="${_slackEscapeHtml(timeStr)}">${relStr}</span>
        </div>`;

    const attachHtml = (msg.attachments || []).map(att => {
        if (!att.url) return '';
        const isImg = att.content_type && att.content_type.startsWith('image/');
        if (isImg) return `<div class="slk-attachment"><img src="${_slackEscapeHtml(att.url)}" class="slk-attachment-img" alt="${_slackEscapeHtml(att.filename)}" loading="lazy"></div>`;
        return `<div class="slk-attachment slk-attachment-file"><i class="fa-solid fa-file me-1" style="color:#36c5f0;"></i><span class="slk-link">${_slackEscapeHtml(att.filename)}</span></div>`;
    }).join('');

    const reactHtml = (msg.reactions || []).length ? `
        <div class="slk-reactions">${(msg.reactions || []).map(r =>
            `<span class="slk-reaction"><span class="slk-reaction-emoji">:${_slackEscapeHtml(r.emoji)}:</span><span class="slk-reaction-count">${r.count}</span></span>`
        ).join('')}</div>` : '';

    div.innerHTML = `
        <div class="slack-msg-avatar-wrap">${avatarHtml}</div>
        <div class="slack-msg-body">
            ${metaHtml}
            ${msg.content ? `<div class="slack-msg-text">${renderSlackMarkdown(msg.content)}</div>` : ''}
            ${attachHtml}${reactHtml}
        </div>
        ${isContinued ? `<span class="slack-msg-hover-time" title="${_slackEscapeHtml(timeStr)}">${relStr}</span>` : ''}`;

    messagesEl.appendChild(div);
}

// ─── Slack Markdown Renderer ─────────────────────────────────────────────────
function renderSlackMarkdown(text) {
    if (!text) return '';
    let s = text.replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>');
    s = s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    s = s.replace(/```([\s\S]*?)```/g, (_, code) =>
        `<pre class="slk-code-block"><code>${code.trim()}</code></pre>`);
    s = s.replace(/`([^`\n]+)`/g, '<code class="slk-inline-code">$1</code>');
    s = s.replace(/\*([^*\n]+)\*/g, '<strong>$1</strong>');
    s = s.replace(/_([^_\n]+)_/g, '<em>$1</em>');
    s = s.replace(/~([^~\n]+)~/g, '<s>$1</s>');
    s = s.replace(/&gt; (.+)/gm, '<div class="slk-blockquote">$1</div>');
    s = s.replace(/(https?:\/\/[^\s&<>"]+)/g, '<a href="$1" target="_blank" rel="noopener noreferrer" class="slk-link">$1</a>');
    s = s.replace(/<\/pre>\n/g, '</pre>');
    s = s.replace(/\n/g, '<br>');
    return s;
}

// ─── Relative Timestamps ─────────────────────────────────────────────────────
function _slackRelativeTime(isoString) {
    if (!isoString) return '';
    const diff = Date.now() - new Date(isoString).getTime();
    const sec = Math.floor(diff / 1000);
    if (sec < 10)  return 'just now';
    if (sec < 60)  return `${sec}s ago`;
    const min = Math.floor(sec / 60);
    if (min < 60)  return `${min}m ago`;
    const hr = Math.floor(min / 60);
    if (hr < 24)   return `${hr}h ago`;
    const day = Math.floor(hr / 24);
    if (day < 7)   return `${day}d ago`;
    return new Date(isoString).toLocaleDateString([], { month: 'short', day: 'numeric' });
}

// ─── Scroll helpers ──────────────────────────────────────────────────────────
function isSlackAtBottom() {
    const el = document.getElementById('slackMessages');
    return el.scrollHeight - el.scrollTop - el.clientHeight < 80;
}
function scrollSlackToBottom() {
    const el = document.getElementById('slackMessages');
    el.scrollTop = el.scrollHeight;
    hideSlackScrollBtn();
}
function onSlackScroll() { if (isSlackAtBottom()) hideSlackScrollBtn(); else showSlackScrollBtn(); }
function showSlackScrollBtn() { document.getElementById('slackScrollBtn').classList.add('visible'); }
function hideSlackScrollBtn() { document.getElementById('slackScrollBtn').classList.remove('visible'); }

// ─── Polling ─────────────────────────────────────────────────────────────────
function startSlackPoll() {
    stopSlackPoll();
    Slack.pollTimer = setInterval(() => {
        if (Slack.activeChannelId) fetchSlackMessages(Slack.activeChannelId, false);
        pollSlackBackgroundChannels();
    }, 4000);
}
function stopSlackPoll() {
    if (Slack.pollTimer) { clearInterval(Slack.pollTimer); Slack.pollTimer = null; }
}

// ─── Background Notification Poll ────────────────────────────────────────────
let _slackBgNotifTimer = null;
function startSlackBgNotifPoll() {
    if (_slackBgNotifTimer) return;
    _slackBgNotifTimer = setInterval(_slackBgNotifTick, 45000);
}
function _slackBgNotifTick() {
    if (!Slack.connected) { clearInterval(_slackBgNotifTimer); _slackBgNotifTimer = null; return; }
    if (Slack.pollTimer) return;
    pollSlackBackgroundChannels();
}

function pollSlackBackgroundChannels() {
    const listEl = document.getElementById('slackChannelList');
    if (!listEl) return;
    listEl.querySelectorAll('.slack-channel-btn').forEach(btn => {
        const chId = btn.id.replace('slkChBtn_', '');
        if (chId === Slack.activeChannelId) return;
        fetch(`/api/slack/messages/${chId}?limit=1`)
            .then(r => r.json())
            .then(data => {
                const msgs = data.messages || [];
                if (!msgs.length) return;
                const lastTs = Slack.lastMessageIds[chId];
                if (!lastTs) { Slack.lastMessageIds[chId] = msgs[msgs.length - 1].id; return; }
                if (parseFloat(msgs[msgs.length - 1].id) > parseFloat(lastTs)) {
                    Slack.channelUnread[chId] = (Slack.channelUnread[chId] || 0) + 1;
                    Slack.totalUnread++;
                    Slack.lastMessageIds[chId] = msgs[msgs.length - 1].id;
                    updateSlackChannelNotif(chId, Slack.channelUnread[chId]);
                    updateSlackBell();
                }
            })
            .catch(() => {});
    });
}

// ─── Notification UI ─────────────────────────────────────────────────────────
function updateSlackChannelNotif(channelId, count) {
    const btn = document.getElementById(`slkChBtn_${channelId}`);
    if (!btn) return;
    const old = btn.querySelector('.slack-notif-count.ch-notif');
    if (old) old.remove();
    if (count > 0) {
        const el = document.createElement('span');
        el.className = 'slack-notif-count ch-notif';
        el.textContent = count > 99 ? '99+' : count;
        btn.appendChild(el);
    }
}

function updateSlackBell() {
    const bell  = document.getElementById('slackBellBtn');
    const badge = document.getElementById('slackBellBadge');
    if (bell) {
        if (Slack.totalUnread > 0) {
            bell.classList.add('has-unread');
            if (badge) badge.textContent = Slack.totalUnread > 99 ? '99+' : Slack.totalUnread;
        } else {
            bell.classList.remove('has-unread');
        }
    }
    const footerBadge = document.getElementById('slackFooterBadge');
    if (footerBadge) {
        if (Slack.totalUnread > 0) {
            footerBadge.style.display = '';
            footerBadge.textContent = Slack.totalUnread > 99 ? '99+' : Slack.totalUnread;
        } else {
            footerBadge.style.display = 'none';
        }
    }
}

// ─── Send Message ────────────────────────────────────────────────────────────
function sendSlackMessage() {
    if (Slack.sending) return;
    const textEl = document.getElementById('slackReplyText');
    const text   = textEl.value.trim();
    if (!text || !Slack.activeChannelId) return;

    Slack.sending = true;
    const btn = document.getElementById('slackSendBtn');
    btn.disabled = true;
    btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';

    fetch(`/api/slack/messages/${Slack.activeChannelId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: text })
    })
    .then(r => r.json())
    .then(data => {
        Slack.sending = false;
        btn.disabled = false;
        btn.innerHTML = '<i class="fa-solid fa-paper-plane"></i>';
        if (data.error) { _showSlackToast(data.error, 'error'); return; }
        textEl.value = '';
        textEl.style.height = 'auto';
        fetchSlackMessages(Slack.activeChannelId, false);
    })
    .catch(() => {
        Slack.sending = false;
        btn.disabled = false;
        btn.innerHTML = '<i class="fa-solid fa-paper-plane"></i>';
        _showSlackToast('Network error. Please try again.', 'error');
    });
}

function slackReplyKeydown(e) {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); sendSlackMessage(); }
}
function autoResizeSlackReply(el) {
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 120) + 'px';
}

// ─── Toast ───────────────────────────────────────────────────────────────────
function _showSlackToast(msg, type) {
    type = type || 'error';
    const panel = document.getElementById('slackPanel');
    const old = panel.querySelector('.slk-toast');
    if (old) old.remove();
    const t = document.createElement('div');
    t.className = 'slk-toast slk-toast-' + type;
    t.style.cssText = 'position:absolute;bottom:70px;left:16px;right:16px;padding:8px 12px;border-radius:8px;font-size:0.82rem;font-weight:600;z-index:20;animation:msgSlideIn 0.2s ease;';
    t.style.background = type === 'error' ? 'rgba(237,66,69,0.15)' : 'rgba(54,197,240,0.15)';
    t.style.color = type === 'error' ? '#ed4245' : '#36c5f0';
    t.style.border = `1px solid ${type === 'error' ? 'rgba(237,66,69,0.3)' : 'rgba(54,197,240,0.3)'}`;
    const icon = type === 'error' ? 'circle-exclamation' : 'check';
    t.innerHTML = `<i class="fa-solid fa-${icon} me-1"></i>${_slackEscapeHtml(msg)}`;
    panel.appendChild(t);
    setTimeout(() => { if (t.parentNode) t.remove(); }, 4000);
}

// ─── Utilities ───────────────────────────────────────────────────────────────
function _slackEscapeHtml(str) {
    if (!str) return '';
    const d = document.createElement('div');
    d.textContent = str;
    return d.innerHTML;
}
function _slackEscapeJs(str) {
    if (!str) return '';
    return str.replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/"/g, '\\"');
}
