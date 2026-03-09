// ─── Discord State ────────────────────────────────────────────────────────────
const Discord = {
    connected: false,
    user: null,
    servers: [],          // saved servers from DB [{guild_id, name, icon_url, bot_in_server}]
    guilds: [],           // all user guilds from OAuth (shown in modal)
    activeGuildId: null,
    activeChannelId: null,
    activeChannelName: null,
    activeServerName: null,
    channelUnread: {},
    lastMessageIds: {},
    totalUnread: 0,
    pollTimer: null,
    sending: false,
    lastRendered: {},
    botCheckTimers: {},   // polling timers per guild waiting for bot invite
};

// ─── Init ─────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', loadDiscordStatus);

function loadDiscordStatus() {
    fetch('/api/discord/status')
        .then(r => r.json())
        .then(data => {
            if (!data.connected) return;
            Discord.connected = true;
            Discord.user = data.user;
            showDiscordConnected(data.user, data.needs_reauth);
            Discord.servers = data.servers || [];
            renderDiscordServers(Discord.servers);
            document.getElementById('discordBellBtn').style.display = 'flex';
            startBgNotifPoll();
        })
        .catch(() => {});
}

function showDiscordConnected(user, needsReauth) {
    document.getElementById('discordNotConnected').style.display = 'none';
    document.getElementById('discordConnected').style.display = 'block';
    const avatarEl = document.getElementById('discordUserAvatar');
    if (user.avatar_url) {
        avatarEl.style.backgroundImage = `url(${user.avatar_url})`;
        avatarEl.style.backgroundSize = 'cover';
        avatarEl.style.backgroundPosition = 'center';
        avatarEl.innerHTML = '';
    } else {
        avatarEl.innerHTML = (user.global_name || user.username || 'D')[0].toUpperCase();
    }
    document.getElementById('discordUsername').textContent =
        user.global_name || user.username || 'Discord User';
    const reauthBanner = document.getElementById('discordReauthBanner');
    if (reauthBanner) reauthBanner.style.display = needsReauth ? 'block' : 'none';
}

// ─── Server list rendering ────────────────────────────────────────────────────
function renderDiscordServers(servers) {
    const list = document.getElementById('discordServerList');
    document.getElementById('discordServerCount').textContent =
        `${servers.length} server${servers.length !== 1 ? 's' : ''}`;

    if (!servers.length) {
        list.innerHTML = '<div style="padding:6px 14px 10px;font-size:0.8rem;color:#444;line-height:1.5;">No servers added yet.<br>Click <strong style="color:#7289da;">+ Add Server</strong> to get started.</div>';
        return;
    }

    list.innerHTML = servers.map(s => {
        const iconHtml = s.icon_url
            ? `<img src="${s.icon_url}" style="width:26px;height:26px;border-radius:7px;object-fit:cover;" alt="">`
            : `<div class="discord-server-icon">${(s.name || 'S')[0].toUpperCase()}</div>`;

        const statusDot = s.bot_in_server
            ? `<span title="Bot active" style="width:7px;height:7px;border-radius:50%;background:#00ff88;flex-shrink:0;"></span>`
            : `<span title="Bot not in server" style="width:7px;height:7px;border-radius:50%;background:#f0a500;flex-shrink:0;"></span>`;

        return `
        <div class="discord-server-item" id="discordServer_${s.guild_id}">
            <button class="discord-server-header" onclick="toggleDiscordServer('${s.guild_id}', ${s.bot_in_server})">
                ${iconHtml}
                <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(s.name)}</span>
                ${statusDot}
                <span class="discord-notif-count" id="srvNotif_${s.guild_id}" style="display:none;"></span>
                <i class="fa-solid fa-chevron-right discord-server-chevron" style="font-size:0.75rem;color:#444;transition:transform 0.2s;margin-left:4px;"></i>
            </button>
            <div class="discord-channel-list" id="chList_${s.guild_id}"></div>
        </div>`;
    }).join('');
}

function toggleDiscordServer(guildId, botInServer) {
    const item = document.getElementById(`discordServer_${guildId}`);
    const isOpen = item.classList.contains('open');

    // Collapse all
    document.querySelectorAll('.discord-server-item').forEach(el => {
        el.classList.remove('open');
        const ch = el.querySelector('.discord-server-chevron');
        if (ch) ch.style.transform = '';
    });
    if (isOpen) return;

    item.classList.add('open');
    const chevron = item.querySelector('.discord-server-chevron');
    if (chevron) chevron.style.transform = 'rotate(90deg)';

    if (botInServer) {
        loadDiscordChannels(guildId);
    } else {
        showBotInvitePrompt(guildId);
    }
}

// ─── Bot-not-in-server prompt ─────────────────────────────────────────────────
function showBotInvitePrompt(guildId) {
    const listEl = document.getElementById(`chList_${guildId}`);
    listEl.innerHTML = `
        <div style="padding:10px 12px;">
            <div style="font-size:0.8rem;color:#aaa;margin-bottom:8px;line-height:1.5;">
                <i class="fa-solid fa-robot" style="color:#f0a500;margin-right:5px;"></i>
                The bot needs to join this server first.
            </div>
            <button onclick="inviteBotToServer('${guildId}')"
                style="width:100%;background:#5865f2;border:none;color:#fff;border-radius:8px;
                       padding:8px 12px;font-size:0.82rem;font-weight:700;cursor:pointer;">
                <i class="fa-brands fa-discord me-1"></i>Invite Bot to Server
            </button>
            <div id="botWaitMsg_${guildId}" style="display:none;font-size:0.75rem;color:#555;margin-top:8px;text-align:center;">
                <i class="fa-solid fa-spinner fa-spin me-1"></i>Waiting for bot to join…
            </div>
        </div>`;
}

function inviteBotToServer(guildId) {
    fetch(`/api/discord/bot-invite/${guildId}`)
        .then(r => r.json())
        .then(data => {
            if (data.error) { alert(data.error); return; }
            // Open invite in new tab, then poll for bot joining
            window.open(data.invite_url, '_blank', 'width=500,height=700');
            const waitMsg = document.getElementById(`botWaitMsg_${guildId}`);
            if (waitMsg) waitMsg.style.display = 'block';
            startBotJoinPoll(guildId);
        })
        .catch(() => alert('Could not generate invite link.'));
}

function startBotJoinPoll(guildId) {
    // Stop any existing poll for this guild
    if (Discord.botCheckTimers[guildId]) {
        clearInterval(Discord.botCheckTimers[guildId]);
    }
    let attempts = 0;
    Discord.botCheckTimers[guildId] = setInterval(() => {
        attempts++;
        if (attempts > 60) { // 5 minutes max
            clearInterval(Discord.botCheckTimers[guildId]);
            return;
        }
        fetch(`/api/discord/bot-check/${guildId}`)
            .then(r => r.json())
            .then(data => {
                if (data.in_server) {
                    clearInterval(Discord.botCheckTimers[guildId]);
                    // Update local server record and reload channels
                    Discord.servers = Discord.servers.map(s =>
                        s.guild_id === guildId ? { ...s, bot_in_server: true } : s
                    );
                    renderDiscordServers(Discord.servers);
                    // Re-open the server and load channels
                    const item = document.getElementById(`discordServer_${guildId}`);
                    if (item) {
                        item.classList.add('open');
                        const ch = item.querySelector('.discord-server-chevron');
                        if (ch) ch.style.transform = 'rotate(90deg)';
                        loadDiscordChannels(guildId);
                    }
                }
            })
            .catch(() => {});
    }, 5000); // check every 5 seconds
}

// ─── Channel loading (bot token, via backend) ─────────────────────────────────
function loadDiscordChannels(guildId) {
    const listEl = document.getElementById(`chList_${guildId}`);
    listEl.innerHTML = skeletonChannels();

    fetch(`/api/discord/channels/${guildId}`)
        .then(r => r.json())
        .then(data => {
            if (data.needs_invite) {
                showBotInvitePrompt(guildId);
                // Mark bot as not in server in local state
                Discord.servers = Discord.servers.map(s =>
                    s.guild_id === guildId ? { ...s, bot_in_server: false } : s
                );
                renderDiscordServers(Discord.servers);
                return;
            }
            if (data.error) {
                listEl.innerHTML = `<div class="dc-ch-error">
                    <i class="fa-solid fa-triangle-exclamation" style="color:#f0a500;"></i>
                    <span>${escapeHtml(data.error)}</span>
                </div>`;
                return;
            }
            const channels = data.channels || [];
            if (!channels.length) {
                listEl.innerHTML = '<div style="padding:6px 10px;font-size:0.78rem;color:#444;">No text channels found.</div>';
                return;
            }
            listEl.innerHTML = channels.map(ch => {
                const unread = Discord.channelUnread[ch.id] || 0;
                return `
                <button class="discord-channel-btn" id="chBtn_${ch.id}"
                    onclick="openDiscordChannel('${guildId}','${ch.id}','${escapeJs(ch.name)}','${escapeJs(data.guild_name || '')}')">
                    <span class="ch-hash">#</span>
                    <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(ch.name)}</span>
                    ${unread ? `<span class="discord-notif-count ch-notif">${unread > 99 ? '99+' : unread}</span>` : ''}
                </button>`;
            }).join('');
        })
        .catch(() => {
            listEl.innerHTML = `<div class="dc-ch-error">
                <i class="fa-solid fa-wifi" style="color:#f0a500;"></i>
                <span>Network error loading channels.</span>
            </div>`;
        });
}

function skeletonChannels() {
    return [1,2,3].map(() => `
        <div class="discord-ch-skel">
            <div class="skel" style="width:12px;height:12px;border-radius:3px;flex-shrink:0;"></div>
            <div class="skel skel-line" style="flex:1;height:10px;"></div>
        </div>`).join('');
}

// ─── Message Panel ────────────────────────────────────────────────────────────
function openDiscordChannel(guildId, channelId, channelName, serverName) {
    document.querySelectorAll('.discord-channel-btn').forEach(b => b.classList.remove('active-channel'));
    const btn = document.getElementById(`chBtn_${channelId}`);
    if (btn) btn.classList.add('active-channel');

    Discord.activeGuildId = guildId;
    Discord.activeChannelId = channelId;
    Discord.activeChannelName = channelName;
    Discord.activeServerName = serverName;
    Discord.lastRendered[channelId] = null;

    const prev = Discord.channelUnread[channelId] || 0;
    Discord.channelUnread[channelId] = 0;
    updateChannelNotif(channelId, 0);
    Discord.totalUnread = Math.max(0, Discord.totalUnread - prev);
    updateBell();

    document.getElementById('discordPanelChannelName').textContent = channelName;
    document.getElementById('discordPanelServerName').textContent = serverName;
    document.getElementById('discordReplyText').placeholder = `Message #${channelName}…`;

    document.getElementById('discordPanel').classList.add('open');
    document.body.classList.add('discord-open');
    updateDiscordToggleBtn(true);
    fetchDiscordMessages(channelId, true);
    startDiscordPoll();
}

function openLastDiscordChannel() {
    if (Discord.activeChannelId) {
        document.getElementById('discordPanel').classList.add('open');
        document.body.classList.add('discord-open');
        updateDiscordToggleBtn(true);
        return;
    }
    const botServer = Discord.servers.find(s => s.bot_in_server);
    if (botServer) toggleDiscordServer(botServer.guild_id, true);
}

function closeDiscordPanel() {
    document.getElementById('discordPanel').classList.remove('open');
    document.body.classList.remove('discord-open');
    updateDiscordToggleBtn(false);
    stopDiscordPoll();
    document.querySelectorAll('.discord-channel-btn').forEach(b => b.classList.remove('active-channel'));
    Discord.activeChannelId = null;
}

function toggleDiscordPanel() {
    const panel = document.getElementById('discordPanel');
    if (panel.classList.contains('open')) {
        closeDiscordPanel();
    } else {
        openLastDiscordChannel();
    }
}

function updateDiscordToggleBtn(isOpen) {
    const btn = document.getElementById('discordChatToggleBtn');
    if (!btn) return;
    btn.style.background = isOpen ? 'rgba(88,101,242,0.12)' : '';
    btn.style.color = isOpen ? '#fff' : '#7289da';
}

// ─── Message Fetching ─────────────────────────────────────────────────────────
function fetchDiscordMessages(channelId, initial) {
    const messagesEl = document.getElementById('discordMessages');
    if (initial) messagesEl.innerHTML = buildMessageSkeleton();

    fetch(`/api/discord/messages/${channelId}`)
        .then(r => r.json())
        .then(data => {
            if (data.error) {
                messagesEl.innerHTML = `
                    <div class="discord-state-msg">
                        <div class="state-icon"><i class="fa-solid fa-triangle-exclamation"></i></div>
                        <span class="state-title">Couldn't load messages</span>
                        <span class="state-sub">${escapeHtml(data.error)}</span>
                    </div>`;
                return;
            }
            const messages = data.messages || [];
            if (initial) {
                Discord.lastRendered[channelId] = null;
                if (!messages.length) {
                    messagesEl.innerHTML = `
                        <div class="discord-state-msg">
                            <div class="state-icon"><i class="fa-regular fa-comment-dots"></i></div>
                            <span class="state-title">No messages yet</span>
                            <span class="state-sub">Be the first to say something in #${escapeHtml(Discord.activeChannelName || channelId)}.</span>
                        </div>`;
                } else {
                    messagesEl.innerHTML = '';
                    messages.forEach(m => appendDiscordMessage(m, false, channelId));
                    scrollDiscordToBottom();
                }
            } else {
                const lastId = Discord.lastMessageIds[channelId];
                const newMsgs = lastId ? messages.filter(m => BigInt(m.id) > BigInt(lastId)) : [];
                if (newMsgs.length) {
                    const atBottom = isDiscordAtBottom();
                    if (document.getElementById('discordPanel').classList.contains('open')) {
                        insertNewDivider(messagesEl);
                    }
                    newMsgs.forEach(m => appendDiscordMessage(m, true, channelId));
                    if (atBottom) scrollDiscordToBottom();
                    else showScrollBtn();
                }
            }
            if (messages.length) Discord.lastMessageIds[channelId] = messages[messages.length - 1].id;
        })
        .catch(() => {
            if (initial) {
                messagesEl.innerHTML = `
                    <div class="discord-state-msg">
                        <div class="state-icon"><i class="fa-solid fa-wifi"></i></div>
                        <span class="state-title">Connection error</span>
                        <span class="state-sub">Check your network and try again.</span>
                    </div>`;
            }
        });
}

function buildMessageSkeleton() {
    const rows = [
        { av: 38, lines: [{ w: '30%' }, { w: '80%' }, { w: '60%' }] },
        { av: 38, lines: [{ w: '25%' }, { w: '55%' }] },
        { av: 38, lines: [{ w: '35%' }, { w: '70%' }, { w: '45%' }, { w: '65%' }] },
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

// ─── Grouped Message Rendering ────────────────────────────────────────────────
const GROUP_GAP_MS = 5 * 60 * 1000;

function appendDiscordMessage(msg, isNew, channelId) {
    const messagesEl = document.getElementById('discordMessages');
    const ph = messagesEl.querySelector('.discord-state-msg');
    if (ph) ph.remove();
    messagesEl.querySelectorAll('[data-skel]').forEach(el => el.remove());

    const ts    = msg.timestamp ? new Date(msg.timestamp) : null;
    const tsMs  = ts ? ts.getTime() : 0;
    const timeStr = ts ? ts.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';
    const relStr  = ts ? relativeTime(msg.timestamp) : '';
    const isSelf  = Discord.user && msg.author_id === Discord.user.discord_id;

    const prev = Discord.lastRendered[channelId];
    const isContinued = prev
        && prev.authorId === msg.author_id
        && (tsMs - prev.tsMs) < GROUP_GAP_MS
        && !msg.reply_to;

    Discord.lastRendered[channelId] = { authorId: msg.author_id, tsMs };

    const div = document.createElement('div');
    div.className = 'discord-msg' + (isContinued ? ' continued' : ' group-start') + (isNew ? ' new-msg' : '');
    div.setAttribute('data-msg-id', msg.id);

    const avatarHtml = msg.avatar_url
        ? `<img src="${msg.avatar_url}" class="discord-msg-avatar" alt="" loading="lazy">`
        : `<div class="discord-msg-avatar">${(msg.author || '?')[0].toUpperCase()}</div>`;

    const metaHtml = isContinued ? '' : `
        <div class="discord-msg-meta">
            <span class="discord-msg-author${isSelf ? ' is-self' : ''}">${escapeHtml(msg.author || 'Unknown')}</span>
            <span class="discord-msg-time" title="${escapeHtml(timeStr)}">${relStr}</span>
        </div>`;

    const replyHtml = msg.reply_to ? `
        <div class="dc-reply-ref">
            <i class="fa-solid fa-reply fa-flip-horizontal"></i>
            <strong>${escapeHtml(msg.reply_to.author)}</strong>
            <span>${escapeHtml(msg.reply_to.content || '')}</span>
        </div>` : '';

    const attachHtml = (msg.attachments || []).map(att => {
        const isImg = att.content_type && att.content_type.startsWith('image/');
        if (isImg) return `<div class="dc-attachment"><img src="${escapeHtml(att.url)}" class="dc-attachment-img" alt="${escapeHtml(att.filename)}" loading="lazy" onclick="window.open('${escapeHtml(att.url)}','_blank')"></div>`;
        return `<div class="dc-attachment dc-attachment-file"><i class="fa-solid fa-file me-1" style="color:#7289da;"></i><a href="${escapeHtml(att.url)}" target="_blank" rel="noopener noreferrer" class="dc-link">${escapeHtml(att.filename)}</a></div>`;
    }).join('');

    const reactHtml = (msg.reactions || []).length ? `
        <div class="dc-reactions">${(msg.reactions || []).map(r =>
            `<span class="dc-reaction"><span class="dc-reaction-emoji">${escapeHtml(r.emoji)}</span><span class="dc-reaction-count">${r.count}</span></span>`
        ).join('')}</div>` : '';

    div.innerHTML = `
        <div class="discord-msg-avatar-wrap">${avatarHtml}</div>
        <div class="discord-msg-body">
            ${replyHtml}${metaHtml}
            ${msg.content ? `<div class="discord-msg-text">${renderDiscordMarkdown(msg.content)}</div>` : ''}
            ${attachHtml}${reactHtml}
        </div>
        ${isContinued ? `<span class="discord-msg-hover-time" title="${escapeHtml(timeStr)}">${relStr}</span>` : ''}`;

    messagesEl.appendChild(div);
}

function insertNewDivider(messagesEl) {
    const existing = messagesEl.querySelector('.discord-new-divider');
    if (existing) existing.remove();
    const div = document.createElement('div');
    div.className = 'discord-new-divider';
    div.innerHTML = 'New Messages';
    messagesEl.appendChild(div);
}

// ─── Discord Markdown Renderer ────────────────────────────────────────────────
function renderDiscordMarkdown(text) {
    if (!text) return '';
    let s = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    s = s.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, _l, code) =>
        `<pre class="dc-code-block"><code>${code.trim()}</code></pre>`);
    s = s.replace(/`([^`\n]+)`/g, '<code class="dc-inline-code">$1</code>');
    s = s.replace(/\*\*\*([^*\n]+)\*\*\*/g, '<strong><em>$1</em></strong>');
    s = s.replace(/\*\*([^*\n]+)\*\*/g,   '<strong>$1</strong>');
    s = s.replace(/\*([^*\n]+)\*/g,        '<em>$1</em>');
    s = s.replace(/_([^_\n]+)_/g,          '<em>$1</em>');
    s = s.replace(/~~([^~\n]+)~~/g,        '<s>$1</s>');
    s = s.replace(/__([^_\n]+)__/g,        '<u>$1</u>');
    s = s.replace(/\|\|([^|]+)\|\|/g, '<span class="dc-spoiler" onclick="this.classList.toggle(\'revealed\')">$1</span>');
    s = s.replace(/^&gt; (.+)/gm, '<div class="dc-blockquote">$1</div>');
    s = s.replace(/(https?:\/\/[^\s&<>"]+)/g, '<a href="$1" target="_blank" rel="noopener noreferrer" class="dc-link">$1</a>');
    s = s.replace(/<\/pre>\n/g, '</pre>');
    s = s.replace(/\n/g, '<br>');
    return s;
}

// ─── Relative Timestamps ──────────────────────────────────────────────────────
function relativeTime(isoString) {
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

// ─── Scroll helpers ───────────────────────────────────────────────────────────
function isDiscordAtBottom() {
    const el = document.getElementById('discordMessages');
    return el.scrollHeight - el.scrollTop - el.clientHeight < 80;
}
function scrollDiscordToBottom() {
    const el = document.getElementById('discordMessages');
    el.scrollTop = el.scrollHeight;
    hideScrollBtn();
}
function onDiscordScroll() { if (isDiscordAtBottom()) hideScrollBtn(); else showScrollBtn(); }
function showScrollBtn() { document.getElementById('discordScrollBtn').classList.add('visible'); }
function hideScrollBtn() { document.getElementById('discordScrollBtn').classList.remove('visible'); }

// ─── Polling ──────────────────────────────────────────────────────────────────
function startDiscordPoll() {
    stopDiscordPoll();
    Discord.pollTimer = setInterval(() => {
        if (Discord.activeChannelId) fetchDiscordMessages(Discord.activeChannelId, false);
        pollBackgroundChannels();
    }, 4000);
}
function stopDiscordPoll() {
    if (Discord.pollTimer) { clearInterval(Discord.pollTimer); Discord.pollTimer = null; }
}

// ─── Background Notification Poll ────────────────────────────────────────────
let _bgNotifTimer = null;
function startBgNotifPoll() {
    if (_bgNotifTimer) return;
    _bgNotifTimer = setInterval(_bgNotifTick, 45000);
}
function stopBgNotifPoll() {
    if (_bgNotifTimer) { clearInterval(_bgNotifTimer); _bgNotifTimer = null; }
}
function _bgNotifTick() {
    if (!Discord.connected) { stopBgNotifPoll(); return; }
    if (Discord.pollTimer) return;
    pollBackgroundChannels();
}

function pollBackgroundChannels() {
    // Poll the first visible rendered channel button per server
    Discord.servers.forEach(srv => {
        if (!srv.bot_in_server) return;
        const listEl = document.getElementById(`chList_${srv.guild_id}`);
        if (!listEl) return;
        listEl.querySelectorAll('.discord-channel-btn').forEach(btn => {
            const chId = btn.id.replace('chBtn_', '');
            if (chId === Discord.activeChannelId) return;
            fetch(`/api/discord/messages/${chId}?limit=1`)
                .then(r => r.json())
                .then(data => {
                    const msgs = data.messages || [];
                    if (!msgs.length) return;
                    const lastId = Discord.lastMessageIds[chId];
                    if (!lastId) { Discord.lastMessageIds[chId] = msgs[0].id; return; }
                    if (BigInt(msgs[0].id) > BigInt(lastId)) {
                        Discord.channelUnread[chId] = (Discord.channelUnread[chId] || 0) + 1;
                        Discord.totalUnread++;
                        Discord.lastMessageIds[chId] = msgs[0].id;
                        updateChannelNotif(chId, Discord.channelUnread[chId]);
                        updateBell();
                    }
                })
                .catch(() => {});
        });
    });
}

// ─── Notification UI ──────────────────────────────────────────────────────────
function updateChannelNotif(channelId, count) {
    const btn = document.getElementById(`chBtn_${channelId}`);
    if (!btn) return;
    const old = btn.querySelector('.discord-notif-count.ch-notif');
    if (old) old.remove();
    if (count > 0) {
        const el = document.createElement('span');
        el.className = 'discord-notif-count ch-notif';
        el.textContent = count > 99 ? '99+' : count;
        btn.appendChild(el);
    }
    Discord.servers.forEach(srv => {
        const listEl = document.getElementById(`chList_${srv.guild_id}`);
        if (!listEl) return;
        const total = Array.from(listEl.querySelectorAll('.discord-channel-btn'))
            .reduce((sum, b) => sum + (Discord.channelUnread[b.id.replace('chBtn_', '')] || 0), 0);
        const srvBadge = document.getElementById(`srvNotif_${srv.guild_id}`);
        if (srvBadge) {
            srvBadge.style.display = total > 0 ? '' : 'none';
            srvBadge.textContent = total > 99 ? '99+' : total;
        }
    });
}
function updateBell() {
    const bell  = document.getElementById('discordBellBtn');
    const badge = document.getElementById('discordBellBadge');
    if (bell) {
        if (Discord.totalUnread > 0) {
            bell.classList.add('has-unread');
            if (badge) badge.textContent = Discord.totalUnread > 99 ? '99+' : Discord.totalUnread;
        } else {
            bell.classList.remove('has-unread');
        }
    }
    // Also update the sidebar footer badge
    const footerBadge = document.getElementById('discordFooterBadge');
    if (footerBadge) {
        if (Discord.totalUnread > 0) {
            footerBadge.style.display = '';
            footerBadge.textContent = Discord.totalUnread > 99 ? '99+' : Discord.totalUnread;
        } else {
            footerBadge.style.display = 'none';
        }
    }
}

// ─── Send Message ─────────────────────────────────────────────────────────────
function sendDiscordMessage() {
    if (Discord.sending) return;
    const textEl = document.getElementById('discordReplyText');
    const text   = textEl.value.trim();
    if (!text || !Discord.activeChannelId) return;

    Discord.sending = true;
    const btn = document.getElementById('discordSendBtn');
    btn.disabled = true;
    btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';

    fetch(`/api/discord/messages/${Discord.activeChannelId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: text })
    })
    .then(r => r.json())
    .then(data => {
        Discord.sending = false;
        btn.disabled = false;
        btn.innerHTML = '<i class="fa-solid fa-paper-plane"></i>';
        if (data.error) { showDiscordToast(data.error, 'error'); return; }
        textEl.value = '';
        textEl.style.height = 'auto';
        fetchDiscordMessages(Discord.activeChannelId, false);
    })
    .catch(() => {
        Discord.sending = false;
        btn.disabled = false;
        btn.innerHTML = '<i class="fa-solid fa-paper-plane"></i>';
        showDiscordToast('Network error. Please try again.', 'error');
    });
}

function discordReplyKeydown(e) {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); sendDiscordMessage(); }
}
function autoResizeDiscordReply(el) {
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 120) + 'px';
}
function openEmojiPicker() {
    const tmp = document.createElement('input');
    tmp.setAttribute('type', 'text');
    tmp.style.cssText = 'position:fixed;top:-999px;opacity:0;';
    document.body.appendChild(tmp);
    tmp.addEventListener('input', () => {
        const ta = document.getElementById('discordReplyText');
        ta.value += tmp.value;
        autoResizeDiscordReply(ta);
        tmp.remove();
    });
    tmp.focus();
    setTimeout(() => { if (document.body.contains(tmp)) tmp.remove(); }, 500);
}

// ─── Toast ────────────────────────────────────────────────────────────────────
function showDiscordToast(msg, type) {
    type = type || 'error';
    const panel = document.getElementById('discordPanel');
    const old = panel.querySelector('.dc-toast');
    if (old) old.remove();
    const t = document.createElement('div');
    t.className = 'dc-toast dc-toast-' + type;
    const icon = type === 'error' ? 'circle-exclamation' : 'triangle-exclamation';
    t.innerHTML = `<i class="fa-solid fa-${icon} me-1"></i>${escapeHtml(msg)}`;
    panel.appendChild(t);
    setTimeout(() => { if (t.parentNode) t.remove(); }, 4000);
}

// ─── Server Modal (guild picker + bot-invite flow) ────────────────────────────
function openServerModal() {
    Discord.guilds = [];
    document.getElementById('discordServerModal').classList.add('open');
    loadDiscordGuilds();
}
function closeServerModal() {
    document.getElementById('discordServerModal').classList.remove('open');
}
document.getElementById('discordServerModal').addEventListener('click', function(e) {
    if (e.target === this) closeServerModal();
});

function loadDiscordGuilds() {
    const listEl = document.getElementById('discordGuildList');
    listEl.innerHTML = buildMessageSkeleton();
    fetch('/api/discord/guilds')
        .then(r => r.json())
        .then(data => {
            if (data.error) {
                const reconnect = data.needs_reconnect
                    ? `<br><a href="/discord/connect" style="color:#7289da;font-size:0.82rem;margin-top:8px;display:inline-block;"><i class="fa-brands fa-discord me-1"></i>Reconnect Discord</a>`
                    : '';
                listEl.innerHTML = `<div class="discord-state-msg" style="padding:20px;">
                    <div class="state-icon"><i class="fa-solid fa-triangle-exclamation"></i></div>
                    <span class="state-sub">${escapeHtml(data.error)}${reconnect}</span></div>`;
                return;
            }
            Discord.guilds = data.guilds || [];
            renderGuildModal();
        })
        .catch(() => {
            listEl.innerHTML = `<div class="discord-state-msg" style="padding:20px;">
                <div class="state-icon"><i class="fa-solid fa-triangle-exclamation"></i></div>
                <span class="state-sub">Couldn't load servers. Try reconnecting Discord.</span></div>`;
        });
}

function renderGuildModal() {
    const listEl = document.getElementById('discordGuildList');
    if (!Discord.guilds.length) {
        listEl.innerHTML = `<div class="discord-state-msg" style="padding:24px;">
            <div class="state-icon"><i class="fa-brands fa-discord"></i></div>
            <span class="state-title">No servers found</span>
            <span class="state-sub">Make sure you're a member of at least one Discord server.</span></div>`;
        return;
    }

    const savedIds = new Set(Discord.servers.map(s => s.guild_id));

    listEl.innerHTML = Discord.guilds.map(g => {
        const iconHtml = g.icon
            ? `<img src="https://cdn.discordapp.com/icons/${g.id}/${g.icon}.png?size=64" class="discord-guild-icon" alt="">`
            : `<div class="discord-guild-icon">${(g.name || 'S')[0].toUpperCase()}</div>`;

        const alreadySaved = savedIds.has(g.id);

        if (g.bot_in_server) {
            // Bot already in server — show add/added button
            if (alreadySaved) {
                return `
                <div class="discord-guild-row" style="opacity:0.6;">
                    ${iconHtml}
                    <span class="discord-guild-name">${escapeHtml(g.name)}</span>
                    <span style="font-size:0.75rem;color:#00ff88;margin-left:auto;"><i class="fa-solid fa-check me-1"></i>Added</span>
                </div>`;
            }
            return `
            <div class="discord-guild-row" id="guild_${g.id}" onclick="addServerFromModal('${g.id}','${escapeJs(g.name)}','${g.icon || ''}')">
                ${iconHtml}
                <span class="discord-guild-name">${escapeHtml(g.name)}</span>
                <span style="font-size:0.75rem;color:#7289da;margin-left:auto;"><i class="fa-solid fa-plus me-1"></i>Add</span>
            </div>`;
        }

        // Bot NOT in server — show invite button
        return `
        <div class="discord-guild-row" id="guild_${g.id}">
            ${iconHtml}
            <div style="flex:1;min-width:0;">
                <div class="discord-guild-name">${escapeHtml(g.name)}</div>
                <div style="font-size:0.75rem;color:#555;">Bot not in server</div>
            </div>
            <button onclick="inviteBotFromModal('${g.id}','${escapeJs(g.name)}','${g.icon || ''}')"
                style="flex-shrink:0;background:#5865f2;border:none;color:#fff;border-radius:6px;
                       padding:5px 10px;font-size:0.75rem;font-weight:700;cursor:pointer;">
                <i class="fa-brands fa-discord me-1"></i>Invite Bot
            </button>
        </div>`;
    }).join('');
}

function addServerFromModal(guildId, guildName, guildIcon) {
    const alreadyIn = Discord.servers.some(s => s.guild_id === guildId);
    if (alreadyIn) return;

    const newServer = { guild_id: guildId, name: guildName, guild_icon: guildIcon || null };
    const updated   = [...Discord.servers.map(s => ({
        guild_id: s.guild_id, name: s.name, guild_icon: s.icon || null
    })), newServer];

    fetch('/api/discord/servers', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ servers: updated })
    })
    .then(r => r.json())
    .then(data => {
        if (data.error) { alert('Error: ' + data.error); return; }
        closeServerModal();
        // Reload full status to get fresh bot_in_server flags
        fetch('/api/discord/status').then(r => r.json()).then(d => {
            Discord.servers = d.servers || [];
            renderDiscordServers(Discord.servers);
        });
    })
    .catch(() => alert('Network error saving server.'));
}

function inviteBotFromModal(guildId, guildName, guildIcon) {
    fetch(`/api/discord/bot-invite/${guildId}`)
        .then(r => r.json())
        .then(data => {
            if (data.error) { alert(data.error); return; }
            window.open(data.invite_url, '_blank', 'width=500,height=700');

            // Replace that row with a "waiting…" state
            const row = document.getElementById(`guild_${guildId}`);
            if (row) {
                row.innerHTML = `
                    <div class="discord-guild-icon">${(guildName || 'S')[0].toUpperCase()}</div>
                    <div style="flex:1;min-width:0;">
                        <div class="discord-guild-name">${escapeHtml(guildName)}</div>
                        <div style="font-size:0.75rem;color:#555;"><i class="fa-solid fa-spinner fa-spin me-1"></i>Waiting for bot to join…</div>
                    </div>`;
            }

            // Poll until bot joins, then auto-add the server
            let attempts = 0;
            const timer = setInterval(() => {
                if (++attempts > 60) { clearInterval(timer); return; }
                fetch(`/api/discord/bot-check/${guildId}`)
                    .then(r => r.json())
                    .then(d => {
                        if (d.in_server) {
                            clearInterval(timer);
                            addServerFromModal(guildId, guildName, guildIcon);
                        }
                    })
                    .catch(() => {});
            }, 5000);
        })
        .catch(() => alert('Could not generate invite link.'));
}

// ─── Utilities ────────────────────────────────────────────────────────────────
function escapeHtml(str) {
    if (!str) return '';
    const d = document.createElement('div');
    d.textContent = str;
    return d.innerHTML;
}
function escapeJs(str) {
    if (!str) return '';
    return str.replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/"/g, '\\"');
}
