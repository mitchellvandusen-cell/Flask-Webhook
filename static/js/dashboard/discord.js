    <script>
    // ─── Discord State ───────────────────────────────────────────────────────
    const Discord = {
        connected: false,
        user: null,
        servers: [],
        guilds: [],
        selectedGuilds: [],
        activeGuildId: null,
        activeChannelId: null,
        activeChannelName: null,
        activeServerName: null,
        channelUnread: {},      // { channelId: count }
        lastMessageIds: {},     // { channelId: lastSnowflake }
        totalUnread: 0,
        pollTimer: null,
        sending: false,
        // For grouping: track last rendered message per channel
        lastRendered: {},       // { channelId: { authorId, timestamp } }
    };

    // ─── Init ────────────────────────────────────────────────────────────────
    document.addEventListener('DOMContentLoaded', loadDiscordStatus);

    function loadDiscordStatus() {
        fetch('/api/discord/status')
            .then(r => r.json())
            .then(data => {
                if (!data.connected) return;
                Discord.connected = true;
                Discord.user = data.user;
                showDiscordConnected(data.user);
                Discord.servers = data.servers || [];
                renderDiscordServers(Discord.servers);
                // Show bell
                document.getElementById('discordBellBtn').style.display = 'flex';
            })
            .catch(() => {});
    }

    function showDiscordConnected(user) {
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
    }

    // ─── Server list rendering ───────────────────────────────────────────────
    function renderDiscordServers(servers) {
        const list = document.getElementById('discordServerList');
        document.getElementById('discordServerCount').textContent = `${servers.length}/3 servers`;

        if (!servers.length) {
            list.innerHTML = '<div style="padding:6px 14px 10px;font-size:0.8rem;color:#444;line-height:1.5;">No servers added yet.<br>Click <strong style="color:#7289da;">Add</strong> to connect a work server.</div>';
            return;
        }

        list.innerHTML = servers.map(s => {
            const iconHtml = s.icon_url
                ? `<img src="${s.icon_url}" style="width:26px;height:26px;border-radius:7px;object-fit:cover;" alt="">`
                : `<div class="discord-server-icon">${(s.name || 'S')[0].toUpperCase()}</div>`;
            return `
            <div class="discord-server-item" id="discordServer_${s.guild_id}">
                <button class="discord-server-header" onclick="toggleDiscordServer('${s.guild_id}')">
                    ${iconHtml}
                    <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(s.name)}</span>
                    <span class="discord-notif-count" id="srvNotif_${s.guild_id}" style="display:none;"></span>
                    <i class="fa-solid fa-chevron-right discord-server-chevron" style="font-size:0.5rem;color:#444;transition:transform 0.2s;margin-left:auto;"></i>
                </button>
                <div class="discord-channel-list" id="chList_${s.guild_id}">
                    ${skeletonChannels()}
                </div>
            </div>`;
        }).join('');
    }

    function skeletonChannels() {
        return [1,2,3].map(() => `
            <div class="discord-ch-skel">
                <div class="skel" style="width:12px;height:12px;border-radius:3px;flex-shrink:0;"></div>
                <div class="skel skel-line" style="flex:1;height:10px;"></div>
            </div>`).join('');
    }

    function toggleDiscordServer(guildId) {
        const item = document.getElementById(`discordServer_${guildId}`);
        const isOpen = item.classList.contains('open');
        document.querySelectorAll('.discord-server-item').forEach(el => {
            el.classList.remove('open');
            const ch = el.querySelector('.discord-server-chevron');
            if (ch) ch.style.transform = '';
        });
        if (isOpen) return;
        item.classList.add('open');
        const chevron = item.querySelector('.discord-server-chevron');
        if (chevron) chevron.style.transform = 'rotate(90deg)';
        loadDiscordChannels(guildId);
    }

    function loadDiscordChannels(guildId) {
        const listEl = document.getElementById(`chList_${guildId}`);
        // Show skeleton while loading
        listEl.innerHTML = skeletonChannels();

        fetch(`/api/discord/channels/${guildId}`)
            .then(r => r.json())
            .then(data => {
                if (data.error) {
                    listEl.innerHTML = `<div style="padding:6px 10px;font-size:0.78rem;color:#ef4444;">
                        <i class="fa-solid fa-triangle-exclamation me-1"></i>${escapeHtml(data.error)}</div>`;
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
                listEl.innerHTML = '<div style="padding:6px 10px;font-size:0.78rem;color:#ef4444;">Failed to load channels.</div>';
            });
    }

    // ─── Message Panel ───────────────────────────────────────────────────────
    function openDiscordChannel(guildId, channelId, channelName, serverName) {
        document.querySelectorAll('.discord-channel-btn').forEach(b => b.classList.remove('active-channel'));
        const btn = document.getElementById(`chBtn_${channelId}`);
        if (btn) btn.classList.add('active-channel');

        Discord.activeGuildId = guildId;
        Discord.activeChannelId = channelId;
        Discord.activeChannelName = channelName;
        Discord.activeServerName = serverName;
        Discord.lastRendered[channelId] = null;

        // Clear unread for this channel
        const prev = Discord.channelUnread[channelId] || 0;
        Discord.channelUnread[channelId] = 0;
        updateChannelNotif(channelId, 0);
        Discord.totalUnread = Math.max(0, Discord.totalUnread - prev);
        updateBell();

        // Update panel header
        document.getElementById('discordPanelChannelName').textContent = channelName;
        document.getElementById('discordPanelServerName').textContent = serverName;
        // Update composer placeholder
        document.getElementById('discordReplyText').placeholder = `Message #${channelName}…`;

        document.getElementById('discordPanel').classList.add('open');
        fetchDiscordMessages(channelId, true);
        startDiscordPoll();
    }

    function openLastDiscordChannel() {
        // Bell click: open most recently active channel, or first channel of first server
        if (Discord.activeChannelId) {
            document.getElementById('discordPanel').classList.add('open');
            return;
        }
        // Try to open first available channel
        if (Discord.servers.length) {
            toggleDiscordServer(Discord.servers[0].guild_id);
        }
    }

    function closeDiscordPanel() {
        document.getElementById('discordPanel').classList.remove('open');
        stopDiscordPoll();
        document.querySelectorAll('.discord-channel-btn').forEach(b => b.classList.remove('active-channel'));
        Discord.activeChannelId = null;
    }

    // ─── Message Fetching ────────────────────────────────────────────────────
    function fetchDiscordMessages(channelId, initial) {
        const messagesEl = document.getElementById('discordMessages');

        if (initial) {
            // Skeleton loader
            messagesEl.innerHTML = buildMessageSkeleton();
        }

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
                                <span class="state-sub">Be the first to say something in #${escapeHtml(channelId)}.</span>
                            </div>`;
                    } else {
                        messagesEl.innerHTML = '';
                        messages.forEach(m => appendDiscordMessage(m, false, channelId));
                        scrollDiscordToBottom();
                    }
                } else {
                    // Poll: only render genuinely new messages
                    const lastId = Discord.lastMessageIds[channelId];
                    const newMsgs = lastId ? messages.filter(m => BigInt(m.id) > BigInt(lastId)) : [];
                    if (newMsgs.length) {
                        const atBottom = isDiscordAtBottom();
                        // Insert "NEW MESSAGES" divider if panel is visible
                        if (document.getElementById('discordPanel').classList.contains('open')) {
                            insertNewDivider(messagesEl);
                        }
                        newMsgs.forEach(m => appendDiscordMessage(m, true, channelId));
                        if (atBottom) scrollDiscordToBottom();
                        else showScrollBtn();
                    }
                }

                if (messages.length) {
                    Discord.lastMessageIds[channelId] = messages[messages.length - 1].id;
                }
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
            <div style="display:flex;gap:14px;padding:14px 18px 4px;">
                <div class="skel skel-circle" style="width:${row.av}px;height:${row.av}px;flex-shrink:0;margin-top:2px;"></div>
                <div style="flex:1;">
                    <div class="skel skel-line" style="width:${row.lines[0].w};margin-bottom:10px;"></div>
                    ${row.lines.slice(1).map(l => `<div class="skel skel-line" style="width:${l.w};"></div>`).join('')}
                </div>
            </div>`).join('');
    }

    // ─── Grouped Message Rendering ───────────────────────────────────────────
    const GROUP_GAP_MS = 5 * 60 * 1000; // 5 minutes → new group

    function appendDiscordMessage(msg, isNew, channelId) {
        const messagesEl = document.getElementById('discordMessages');
        const ph = messagesEl.querySelector('.discord-state-msg');
        if (ph) ph.remove();
        const skelEls = messagesEl.querySelectorAll('[style*="display:flex;gap:14px;padding:14px"]');
        skelEls.forEach(el => el.remove());

        const ts = msg.timestamp ? new Date(msg.timestamp) : null;
        const tsMs = ts ? ts.getTime() : 0;
        const timeStr = ts ? ts.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';
        const isSelf = Discord.user && msg.author_id === Discord.user.discord_id;

        const prev = Discord.lastRendered[channelId];
        const isContinued = prev
            && prev.authorId === msg.author_id
            && (tsMs - prev.tsMs) < GROUP_GAP_MS;

        Discord.lastRendered[channelId] = { authorId: msg.author_id, tsMs };

        const div = document.createElement('div');
        div.className = 'discord-msg' + (isContinued ? ' continued' : ' group-start') + (isNew ? ' new-msg' : '');
        div.setAttribute('data-msg-id', msg.id);

        let avatarHtml;
        if (msg.avatar_url) {
            avatarHtml = `<img src="${msg.avatar_url}" class="discord-msg-avatar" alt="" loading="lazy">`;
        } else {
            avatarHtml = `<div class="discord-msg-avatar">${(msg.author || '?')[0].toUpperCase()}</div>`;
        }

        const metaHtml = isContinued ? '' : `
            <div class="discord-msg-meta">
                <span class="discord-msg-author${isSelf ? ' is-self' : ''}">${escapeHtml(msg.author || 'Unknown')}</span>
                <span class="discord-msg-time">${timeStr}</span>
            </div>`;

        div.innerHTML = `
            <div class="discord-msg-avatar-wrap">${avatarHtml}</div>
            <div class="discord-msg-body">
                ${metaHtml}
                <div class="discord-msg-text">${escapeHtml(msg.content || '')}</div>
            </div>
            ${isContinued ? `<span class="discord-msg-hover-time">${timeStr}</span>` : ''}`;

        messagesEl.appendChild(div);
    }

    function insertNewDivider(messagesEl) {
        // Only insert once per poll cycle — remove previous ones first
        const existing = messagesEl.querySelector('.discord-new-divider');
        if (existing) existing.remove();
        const div = document.createElement('div');
        div.className = 'discord-new-divider';
        div.innerHTML = 'New Messages';
        messagesEl.appendChild(div);
    }

    // ─── Scroll helpers ───────────────────────────────────────────────────────
    function isDiscordAtBottom() {
        const el = document.getElementById('discordMessages');
        return el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    }

    function scrollDiscordToBottom() {
        const el = document.getElementById('discordMessages');
        el.scrollTop = el.scrollHeight;
        hideScrollBtn();
    }

    function onDiscordScroll() {
        if (isDiscordAtBottom()) hideScrollBtn(); else showScrollBtn();
    }

    function showScrollBtn() {
        document.getElementById('discordScrollBtn').classList.add('visible');
    }
    function hideScrollBtn() {
        document.getElementById('discordScrollBtn').classList.remove('visible');
    }

    // ─── Polling ─────────────────────────────────────────────────────────────
    function startDiscordPoll() {
        stopDiscordPoll();
        Discord.pollTimer = setInterval(() => {
            if (Discord.activeChannelId) {
                fetchDiscordMessages(Discord.activeChannelId, false);
            }
            pollBackgroundChannels();
        }, 4000);
    }

    function stopDiscordPoll() {
        if (Discord.pollTimer) { clearInterval(Discord.pollTimer); Discord.pollTimer = null; }
    }

    function pollBackgroundChannels() {
        Discord.servers.forEach(srv => {
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

    // ─── Notification UI ─────────────────────────────────────────────────────
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
        // Server-level rollup
        if (Discord.servers) {
            Discord.servers.forEach(srv => {
                const total = Object.entries(Discord.channelUnread)
                    .filter(([id]) => {
                        const chBtn = document.getElementById(`chBtn_${id}`);
                        return chBtn && document.getElementById(`chList_${srv.guild_id}`)?.contains(chBtn);
                    })
                    .reduce((sum, [, n]) => sum + n, 0);
                const srvBadge = document.getElementById(`srvNotif_${srv.guild_id}`);
                if (srvBadge) {
                    srvBadge.style.display = total > 0 ? '' : 'none';
                    srvBadge.textContent = total > 99 ? '99+' : total;
                }
            });
        }
    }

    function updateBell() {
        const bell = document.getElementById('discordBellBtn');
        const badge = document.getElementById('discordBellBadge');
        if (!bell) return;
        const total = Discord.totalUnread;
        if (total > 0) {
            bell.classList.add('has-unread');
            badge.textContent = total > 99 ? '99+' : total;
        } else {
            bell.classList.remove('has-unread');
        }
    }

    // ─── Send Message ─────────────────────────────────────────────────────────
    function sendDiscordMessage() {
        if (Discord.sending) return;
        const textEl = document.getElementById('discordReplyText');
        const text = textEl.value.trim();
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
            if (data.error) { alert('Failed to send: ' + data.error); return; }
            textEl.value = '';
            textEl.style.height = 'auto';
            fetchDiscordMessages(Discord.activeChannelId, false);
        })
        .catch(() => {
            Discord.sending = false;
            btn.disabled = false;
            btn.innerHTML = '<i class="fa-solid fa-paper-plane"></i>';
            alert('Network error sending message.');
        });
    }

    function discordReplyKeydown(e) {
        if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
            e.preventDefault();
            sendDiscordMessage();
        }
        // Shift+Enter = natural newline (default behavior, nothing to do)
    }

    function autoResizeDiscordReply(el) {
        el.style.height = 'auto';
        el.style.height = Math.min(el.scrollHeight, 120) + 'px';
    }

    function openEmojiPicker() {
        // Trigger native emoji picker via a hidden input (works in Chrome 90+)
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
        // Fallback: if browser doesn't support it, remove after 500ms
        setTimeout(() => { if (document.body.contains(tmp)) tmp.remove(); }, 500);
    }

    // ─── Server Modal ─────────────────────────────────────────────────────────
    function openServerModal() {
        Discord.selectedGuilds = Discord.servers.map(s => s.guild_id);
        document.getElementById('discordServerModal').classList.add('open');
        loadDiscordGuilds();
    }

    function closeServerModal() {
        document.getElementById('discordServerModal').classList.remove('open');
    }

    // Close modal on backdrop click
    document.getElementById('discordServerModal').addEventListener('click', function(e) {
        if (e.target === this) closeServerModal();
    });

    function loadDiscordGuilds() {
        const listEl = document.getElementById('discordGuildList');
        listEl.innerHTML = buildMessageSkeleton();
        fetch('/api/discord/guilds')
            .then(r => r.json())
            .then(data => {
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
        const atLimit = Discord.selectedGuilds.length >= 3;
        listEl.innerHTML = Discord.guilds.map(g => {
            const isSelected = Discord.selectedGuilds.includes(g.id);
            const isDisabled = !isSelected && atLimit;
            const iconHtml = g.icon
                ? `<img src="https://cdn.discordapp.com/icons/${g.id}/${g.icon}.png?size=64" class="discord-guild-icon" alt="">`
                : `<div class="discord-guild-icon">${(g.name || 'S')[0].toUpperCase()}</div>`;
            return `
            <div class="discord-guild-row ${isSelected ? 'selected' : ''} ${isDisabled ? 'disabled' : ''}"
                 id="guild_${g.id}"
                 onclick="${isDisabled ? '' : `toggleGuildSelect('${escapeJs(g.id)}')`}">
                ${iconHtml}
                <span class="discord-guild-name">${escapeHtml(g.name)}</span>
                ${isDisabled ? '<span style="font-size:0.72rem;color:#555;margin-left:auto;margin-right:4px;">3 max</span>' : ''}
                <div class="discord-guild-check"><i class="fa-solid fa-check"></i></div>
            </div>`;
        }).join('');
    }

    function toggleGuildSelect(guildId) {
        const idx = Discord.selectedGuilds.indexOf(guildId);
        if (idx > -1) Discord.selectedGuilds.splice(idx, 1);
        else { if (Discord.selectedGuilds.length >= 3) return; Discord.selectedGuilds.push(guildId); }
        renderGuildModal();
    }

    function saveSelectedServers() {
        const btn = document.getElementById('saveServersBtn');
        btn.disabled = true;
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-1"></i>Saving…';
        const selected = Discord.guilds
            .filter(g => Discord.selectedGuilds.includes(g.id))
            .map(g => ({ guild_id: g.id, name: g.name, icon: g.icon }));
        fetch('/api/discord/servers', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ servers: selected })
        })
        .then(r => r.json())
        .then(data => {
            btn.disabled = false;
            btn.innerHTML = '<i class="fa-solid fa-floppy-disk me-1"></i>Save Servers';
            if (data.error) { alert('Error: ' + data.error); return; }
            closeServerModal();
            fetch('/api/discord/status').then(r => r.json()).then(d => {
                Discord.servers = d.servers || [];
                renderDiscordServers(Discord.servers);
            });
        })
        .catch(() => {
            btn.disabled = false;
            btn.innerHTML = '<i class="fa-solid fa-floppy-disk me-1"></i>Save Servers';
            alert('Network error. Try again.');
        });
    }

    // ─── Utilities ────────────────────────────────────────────────────────────
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
    </script>
