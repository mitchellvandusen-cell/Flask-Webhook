/* popout-dialer.js — Standalone top-level dialer (full feature)
 *
 * Owns Twilio Voice Device. The GHL iframe talks to us via the
 * "omnisconn-dialer" BroadcastChannel. Features:
 *
 *   · Outbound + inbound bridged calls with state color cascade
 *   · Mute, hold (local), transfer, DTMF, hangup, Google Meet, recording indicator
 *   · Queue streamed from iframe (mirrored, searchable, click-to-dial)
 *   · Queue run commands (start/stop/skip) proxied back to iframe
 *   · AI intelligence drawer: fetched from /voice/contact-intelligence-bulk
 *   · Notes drawer (live notes persisted with disposition)
 *   · Recent calls drawer (fetched from /voice/call-history)
 *   · Audio device picker with test tone + localStorage persistence
 *   · Keyboard shortcuts: M, H, Space, K, T, Esc, 1-9
 *   · Session stats footer (calls, connected, talk time)
 *   · Incoming call ring overlay (for non-auto-accepted calls)
 */
(function () {
    'use strict';

    // ═══ DOM refs ═════════════════════════════════════════════════════════════
    const $ = (id) => document.getElementById(id);
    const body = document.body;

    const el = {
        // Call card
        statusPill: $('popStatusPill'),
        statusText: $('popStatusText'),
        recIndicator: $('popRecIndicator'),
        timer: $('popTimer'),
        contactAvatar: $('popContactAvatar'),
        contactName: $('popContactName'),
        contactPhone: $('popContactPhone'),
        contactTemp: $('popContactTemp'),
        contactTempLabel: $('popContactTempLabel'),
        contactScore: $('popContactScore'),
        manualDial: $('popManualDial'),
        manualPhone: $('popManualPhone'),
        manualDialBtn: $('popManualDialBtn'),
        controls: $('popControls'),
        muteBtn: $('popMuteBtn'),
        holdBtn: $('popHoldBtn'),
        keypadBtn: $('popKeypadBtn'),
        transferBtn: $('popTransferBtn'),
        voicemailBtn: $('popVoicemailBtn'),
        meetBtn: $('popMeetBtn'),
        hangupBtn: $('popHangupBtn'),
        keypad: $('popKeypad'),

        // Intelligence drawer
        intelDrawer: $('popIntelDrawer'),
        intelToggle: $('popIntelToggle'),
        intelBody: $('popIntelBody'),
        intelBadge: $('popIntelBadge'),
        intelLoading: $('popIntelLoading'),
        intelEmpty: $('popIntelEmpty'),
        intelCard: $('popIntelCard'),
        intelRing: $('popIntelRing'),
        intelScoreRing: null,                 // set after DOM ready
        intelScoreNum: $('popIntelScoreNum'),
        intelTempLabel: $('popIntelTempLabel'),
        intelTempReason: $('popIntelTempReason'),
        intelSummary: $('popIntelSummary'),
        intelActions: $('popIntelActions'),

        // Queue
        queueToggle: $('popQueueToggle'),
        queueBody: $('popQueueBody'),
        queueList: $('popQueueList'),
        queueEmpty: $('popQueueEmpty'),
        queueCount: $('popQueueCount'),
        queueSearch: $('popQueueSearch'),
        queueStart: $('popQueueStart'),
        queueStop: $('popQueueStop'),
        queueSkip: $('popQueueSkip'),

        // Notes
        notesToggle: $('popNotesToggle'),
        notesBody: $('popNotesBody'),
        notesArea: $('popNotesArea'),
        notesClear: $('popNotesClear'),
        notesIndicator: $('popNotesIndicator'),

        // History
        historyToggle: $('popHistoryToggle'),
        historyBody: $('popHistoryBody'),
        historyList: $('popHistoryList'),
        historyEmpty: $('popHistoryEmpty'),

        // Disposition
        dispDrawer: $('popDispDrawer'),
        dispSkip: $('popDispSkip'),

        // Footer / device
        deviceDot: $('popDeviceDot'),
        deviceLabel: $('popDeviceLabel'),
        statCalls: $('popStatCalls'),
        statConnected: $('popStatConnected'),
        statTalk: $('popStatTalk'),

        // Header
        syncDot: $('popSyncDot'),
        syncLabel: $('popSyncLabel'),
        settingsBtn: $('popSettingsBtn'),

        // Transfer modal
        transferModal: $('popTransferModal'),
        transferPhone: $('popTransferPhone'),
        transferConfirm: $('popTransferConfirm'),
        transferRecents: $('popTransferRecents'),

        // Settings modal
        settingsModal: $('popSettingsModal'),
        inputDevice: $('popInputDevice'),
        outputDevice: $('popOutputDevice'),
        testTone: $('popTestTone'),

        // Incoming
        incoming: $('popIncoming'),
        incomingAvatar: $('popIncomingAvatar'),
        incomingName: $('popIncomingName'),
        incomingPhone: $('popIncomingPhone'),
        incomingAccept: $('popIncomingAccept'),
        incomingDecline: $('popIncomingDecline'),

        // Toast
        toastStack: $('popToastStack'),
    };
    el.intelScoreRing = document.querySelector('.pop-intel-score-ring');

    // ═══ State ════════════════════════════════════════════════════════════════
    let voipDevice = null;
    let voipCall = null;
    let pendingIncoming = null;
    let deviceReady = false;
    let deviceInitInFlight = false;
    let isMuted = false;
    let isOnHold = false;
    let keypadOpen = false;
    let callStartMs = 0;
    let timerInterval = null;
    let currentContact = null;
    let lastEndedCall = null;
    let mirroredQueue = [];
    let intelCache = {};              // contactId → intel
    let intelFetchInFlight = new Set();
    let selectedInputDeviceId = null;
    let selectedOutputDeviceId = null;
    let lastChannelPingMs = 0;
    const session = {
        calls: 0,
        connected: 0,
        talkMs: 0,
        history: [],                  // { sid, contact, direction, durationMs, endedAt }
        recentXferNumbers: [],
    };
    const LS = {
        input:  'omc_pop_input_device',
        output: 'omc_pop_output_device',
        xfer:   'omc_pop_xfer_recents',
    };

    try { selectedInputDeviceId  = localStorage.getItem(LS.input)  || null; } catch (e) {}
    try { selectedOutputDeviceId = localStorage.getItem(LS.output) || null; } catch (e) {}
    try { session.recentXferNumbers = JSON.parse(localStorage.getItem(LS.xfer) || '[]'); } catch (e) {}

    // ── Restore queue from localStorage (fallback when BroadcastChannel
    //    message hasn't arrived yet or iframe isn't running) ──
    try {
        const storedQueue = JSON.parse(localStorage.getItem('igb_dialer_queue') || '[]');
        if (Array.isArray(storedQueue) && storedQueue.length) {
            mirroredQueue = storedQueue.map(c => ({
                contactId: c.contactId || c.id,
                name: c.name || c.firstName || 'Lead',
                firstName: c.firstName,
                phone: c.phone,
                status: c.status || 'pending',
                temperature: c.temperature,
                score: c.score,
            }));
        }
    } catch (e) { /* corrupt data — ignore */ }

    // ═══ BroadcastChannel ═════════════════════════════════════════════════════
    let bus = null;
    try { bus = new BroadcastChannel('omnisconn-dialer'); }
    catch (e) { console.warn('[popout] BroadcastChannel unsupported:', e); }

    function sendToIframe(msg) {
        // BroadcastChannel (works same-partition)
        if (bus) {
            try { bus.postMessage(msg); } catch (e) { /* closed */ }
        }
        // postMessage to opener (works cross-partition — bypasses Chrome storage partitioning)
        if (window.opener && !window.opener.closed) {
            try { window.opener.postMessage({ _omcDialer: true, ...msg }, '*'); } catch (e) { /* closed */ }
        }
    }

    // Shared message handler (used by both BroadcastChannel and postMessage)
    function handleBusMessage(msg) {
        switch (msg.type) {
                case 'IFRAME_HELLO':
                case 'IFRAME_PING':
                    lastChannelPingMs = Date.now();
                    setSyncStatus(true);
                    if (msg.type === 'IFRAME_HELLO') {
                        sendToIframe({
                            type: 'POPUP_HELLO',
                            deviceReady,
                            call: voipCall ? {
                                state: currentStateName(),
                                contact: currentContact,
                                durationMs: callStartMs ? Date.now() - callStartMs : 0,
                            } : null,
                        });
                    } else {
                        sendToIframe({ type: 'POPUP_PONG' });
                    }
                    break;
                case 'DIAL':
                    handleDialCommand(msg.contact);
                    break;
                case 'HANGUP':
                    if (voipCall) { try { voipCall.disconnect(); } catch (e) {} }
                    break;
                case 'QUEUE_UPDATE':
                    mirroredQueue = Array.isArray(msg.queue) ? msg.queue : [];
                    renderQueue();
                    break;
                case 'QUEUE_STATE':
                    // { running: bool }
                    el.queueStart.disabled = !!msg.running;
                    el.queueStop.disabled = !msg.running;
                    el.queueStart.classList.toggle('is-active', !msg.running);
                    el.queueStop.classList.toggle('is-active',  !!msg.running);
                    break;
                case 'FOCUS_CONTACT':
                    if (msg.contact && !voipCall) {
                        setContactPreview(msg.contact);
                        fetchIntelligence(msg.contact.contactId);
                    }
                    break;
                case 'IFRAME_DISPOSITION_SAVED':
                    // Iframe confirmed save, clear our pending note marker
                    el.notesIndicator.hidden = true;
                    break;
                default:
                    break;
            }
    }

    // Wire BroadcastChannel to shared handler
    if (bus) {
        bus.onmessage = (ev) => handleBusMessage(ev.data || {});
    }

    // Wire postMessage to shared handler (cross-partition fallback)
    window.addEventListener('message', (ev) => {
        const msg = ev.data;
        if (!msg || !msg._omcDialer) return;  // ignore non-dialer messages
        handleBusMessage(msg);
    });

    setInterval(() => {
        if (lastChannelPingMs && Date.now() - lastChannelPingMs > 12000) setSyncStatus(false);
    }, 4000);

    function setSyncStatus(linked) {
        el.syncDot.classList.toggle('is-offline', !linked);
        el.syncLabel.textContent = linked ? 'LINKED' : 'STANDALONE';
    }

    window.addEventListener('load', () => {
        sendToIframe({ type: 'POPUP_HELLO', deviceReady: false, call: null });
        // Ask iframe to send current queue (in case it's running)
        sendToIframe({ type: 'QUEUE_REQUEST' });
        // Render localStorage-restored queue immediately
        if (mirroredQueue.length) renderQueue();
    });
    window.addEventListener('beforeunload', () => {
        sendToIframe({ type: 'POPUP_CLOSING' });
    });

    // Listen for localStorage changes from the main dashboard (fallback sync)
    window.addEventListener('storage', (ev) => {
        if (ev.key !== 'igb_dialer_queue') return;
        try {
            const items = JSON.parse(ev.newValue || '[]');
            if (!Array.isArray(items)) return;
            mirroredQueue = items.map(c => ({
                contactId: c.contactId || c.id,
                name: c.name || c.firstName || 'Lead',
                firstName: c.firstName,
                phone: c.phone,
                status: c.status || 'pending',
                temperature: c.temperature,
                score: c.score,
            }));
            renderQueue();
        } catch (e) { /* ignore */ }
    });

    // ── Poll localStorage as bulletproof fallback (BroadcastChannel + storage
    //    events can both silently fail in cross-origin iframe scenarios) ──
    let _lastLsHash = localStorage.getItem('igb_dialer_queue') || '[]';
    setInterval(() => {
        try {
            const raw = localStorage.getItem('igb_dialer_queue') || '[]';
            // Quick hash check to avoid expensive parse on every tick
            if (raw === _lastLsHash) return;
            _lastLsHash = raw;
            const items = JSON.parse(raw);
            if (!Array.isArray(items)) return;
            mirroredQueue = items.map(c => ({
                contactId: c.contactId || c.id,
                name: c.name || c.firstName || 'Lead',
                firstName: c.firstName,
                phone: c.phone,
                status: c.status || 'pending',
                temperature: c.temperature,
                score: c.score,
            }));
            renderQueue();
        } catch (e) { /* ignore */ }
    }, 2000);

    // ═══ State cascade ════════════════════════════════════════════════════════
    const STATE_CLASSES = ['pop-state-idle','pop-state-dialing','pop-state-ringing',
                           'pop-state-connected','pop-state-hold','pop-state-ending','pop-state-error'];
    const STATE_LABELS = {
        idle: 'READY', dialing: 'DIALING', ringing: 'RINGING',
        connected: 'CONNECTED', hold: 'ON HOLD', ending: 'ENDED', error: 'ERROR',
    };

    function setState(state) {
        STATE_CLASSES.forEach(c => body.classList.remove(c));
        body.classList.add('pop-state-' + state);
        el.statusText.textContent = STATE_LABELS[state] || state.toUpperCase();

        const inCall = ['dialing','ringing','connected','hold'].includes(state);
        el.controls.hidden = !inCall;
        el.manualDial.style.display = inCall ? 'none' : 'flex';
        el.recIndicator.hidden = state !== 'connected';

        sendToIframe({
            type: 'POPUP_STATE',
            state,
            contact: currentContact,
            durationMs: callStartMs ? Date.now() - callStartMs : 0,
        });
    }

    function currentStateName() {
        const cls = body.className.match(/pop-state-(\S+)/);
        return cls ? cls[1] : 'idle';
    }

    // ═══ Contact preview ══════════════════════════════════════════════════════
    function setContactPreview(c) {
        currentContact = c || null;
        if (!c) {
            el.contactName.textContent = 'No active call';
            el.contactPhone.textContent = '—';
            el.contactAvatar.textContent = '?';
            el.contactTemp.classList.add('hidden');
            el.intelDrawer.hidden = true;
            return;
        }
        const name = c.name || c.displayName || c.firstName || 'Lead';
        el.contactName.textContent = name;
        el.contactPhone.textContent = c.phone || '—';
        el.contactAvatar.textContent = (name[0] || '?').toUpperCase();

        if (c.temperature) applyContactTemp(c.temperature, c.score);
        else el.contactTemp.classList.add('hidden');
    }

    function applyContactTemp(temp, score) {
        const t = String(temp || '').toLowerCase();
        el.contactTemp.classList.remove('hidden','temp-warm','temp-cool','temp-cold');
        if (t === 'warm') el.contactTemp.classList.add('temp-warm');
        else if (t === 'cool') el.contactTemp.classList.add('temp-cool');
        else if (t === 'cold') el.contactTemp.classList.add('temp-cold');
        el.contactTempLabel.textContent = t.toUpperCase() || 'UNKNOWN';
        el.contactScore.textContent = (score != null) ? String(score) : '';
    }

    // ═══ Timer ════════════════════════════════════════════════════════════════
    function startTimer() {
        stopTimer();
        callStartMs = Date.now();
        timerInterval = setInterval(() => {
            const s = Math.floor((Date.now() - callStartMs) / 1000);
            el.timer.textContent = `${String(Math.floor(s/60)).padStart(2,'0')}:${String(s%60).padStart(2,'0')}`;
            // Live talk-time footer stat
            el.statTalk.textContent = formatDuration(session.talkMs + (Date.now() - callStartMs));
        }, 500);
    }
    function stopTimer() { if (timerInterval) clearInterval(timerInterval); timerInterval = null; }

    function formatDuration(ms) {
        const s = Math.floor(ms / 1000);
        const mm = String(Math.floor(s / 60)).padStart(2, '0');
        const ss = String(s % 60).padStart(2, '0');
        return `${mm}:${ss}`;
    }

    // ═══ Twilio Device bootstrap ══════════════════════════════════════════════
    async function initDevice() {
        if (deviceReady || deviceInitInFlight) return deviceReady;
        deviceInitInFlight = true;
        setDeviceLabel('Fetching token…');

        try {
            const tokRes = await fetch('/voice/token', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
            });
            const tokData = await tokRes.json();
            if (!tokRes.ok || !tokData.token) {
                setDeviceLabel(tokData.error || 'Token error', 'error');
                toast(tokData.error || 'Voice token error', 'error');
                deviceInitInFlight = false;
                return false;
            }

            setDeviceLabel('Requesting microphone…');
            try {
                const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
                stream.getTracks().forEach(t => t.stop());
            } catch (micErr) {
                console.error('[popout] mic denied:', micErr);
                setDeviceLabel('Microphone blocked — allow in address bar', 'error');
                toast('Microphone blocked. Click the lock icon in the address bar, allow microphone, then reload.', 'error', 9000);
                setState('error');
                deviceInitInFlight = false;
                return false;
            }

            if (!window.Twilio || !window.Twilio.Device) {
                setDeviceLabel('Voice SDK failed to load', 'error');
                deviceInitInFlight = false;
                return false;
            }

            setDeviceLabel('Connecting to voice server…');
            voipDevice = new Twilio.Device(tokData.token, {
                logLevel: 'warn',
                closeProtection: true,
                codecPreferences: ['opus', 'pcmu'],
                audioConstraints: {
                    autoGainControl: false,
                    noiseSuppression: false,
                    echoCancellation: true,
                },
                edge: ['ashburn', 'umatilla', 'roaming'],
            });

            voipDevice.on('registered', async () => {
                deviceReady = true;
                deviceInitInFlight = false;
                setDeviceLabel('Ready · ' + (tokData.identity || 'agent'), 'ready');
                sendToIframe({ type: 'POPUP_DEVICE_READY' });
                await populateAudioDevices();
                applyPersistedAudioDevices();
            });
            voipDevice.on('unregistered', () => {
                deviceReady = false;
                setDeviceLabel('Disconnected', 'error');
            });
            voipDevice.on('error', (err) => {
                console.error('[popout] device error:', err);
                setDeviceLabel('Error: ' + (err?.message || 'voice error'), 'error');
                if (!voipCall) setState('error');
            });
            voipDevice.on('tokenWillExpire', async () => {
                try {
                    const r = await fetch('/voice/token', { method: 'POST', headers: { 'Content-Type': 'application/json' } });
                    const d = await r.json();
                    if (r.ok && d.token) voipDevice.updateToken(d.token);
                } catch (e) { /* non-fatal */ }
            });
            voipDevice.on('incoming', (call) => {
                // Human-mode bridges auto-accept; surprise inbounds show the overlay.
                if (voipCall) {
                    console.warn('[popout] already on a call — rejecting incoming');
                    try { call.reject(); } catch (e) {}
                    return;
                }
                // If the iframe is linked and actively dialing (human mode), auto-accept.
                // Otherwise surface the ring UI so the agent can accept/decline.
                const autoAccept = lastChannelPingMs && (Date.now() - lastChannelPingMs < 15000);
                if (autoAccept) {
                    call.accept();
                    bindCall(call);
                } else {
                    showIncomingRing(call);
                }
            });

            if (voipDevice.audio) {
                voipDevice.audio.on('deviceChange', () => { populateAudioDevices(); });
            }

            await voipDevice.register();
            return true;
        } catch (e) {
            console.error('[popout] initDevice failed:', e);
            setDeviceLabel('Init failed: ' + (e.message || e), 'error');
            deviceInitInFlight = false;
            return false;
        }
    }

    function setDeviceLabel(text, status) {
        el.deviceLabel.textContent = text;
        el.deviceDot.classList.remove('is-ready', 'is-error');
        if (status === 'ready') el.deviceDot.classList.add('is-ready');
        else if (status === 'error') el.deviceDot.classList.add('is-error');
    }

    // ═══ Audio devices ════════════════════════════════════════════════════════
    async function populateAudioDevices() {
        try {
            const devices = await navigator.mediaDevices.enumerateDevices();
            const inputs  = devices.filter(d => d.kind === 'audioinput');
            const outputs = devices.filter(d => d.kind === 'audiooutput');

            el.inputDevice.innerHTML = '';
            inputs.forEach(d => {
                const o = document.createElement('option');
                o.value = d.deviceId;
                o.textContent = d.label || `Microphone (${d.deviceId.slice(0, 6)})`;
                if (d.deviceId === selectedInputDeviceId) o.selected = true;
                el.inputDevice.appendChild(o);
            });

            el.outputDevice.innerHTML = '';
            outputs.forEach(d => {
                const o = document.createElement('option');
                o.value = d.deviceId;
                o.textContent = d.label || `Speaker (${d.deviceId.slice(0, 6)})`;
                if (d.deviceId === selectedOutputDeviceId) o.selected = true;
                el.outputDevice.appendChild(o);
            });

            if (!outputs.length) {
                const o = document.createElement('option');
                o.textContent = 'Default (browser-controlled)';
                o.disabled = true;
                el.outputDevice.appendChild(o);
            }
        } catch (e) {
            console.warn('[popout] populateAudioDevices:', e);
        }
    }

    async function applyPersistedAudioDevices() {
        if (!voipDevice || !voipDevice.audio) return;
        try {
            if (selectedInputDeviceId && voipDevice.audio.setInputDevice) {
                await voipDevice.audio.setInputDevice(selectedInputDeviceId);
            }
            if (selectedOutputDeviceId && voipDevice.audio.speakerDevices?.set) {
                await voipDevice.audio.speakerDevices.set([selectedOutputDeviceId]);
            }
        } catch (e) {
            console.warn('[popout] applyPersistedAudioDevices:', e);
        }
    }

    el.inputDevice.addEventListener('change', async (ev) => {
        selectedInputDeviceId = ev.target.value;
        try { localStorage.setItem(LS.input, selectedInputDeviceId); } catch (e) {}
        try {
            if (voipDevice?.audio?.setInputDevice) {
                await voipDevice.audio.setInputDevice(selectedInputDeviceId);
                toast('Microphone updated');
            }
        } catch (e) { toast('Could not switch microphone', 'error'); }
    });
    el.outputDevice.addEventListener('change', async (ev) => {
        selectedOutputDeviceId = ev.target.value;
        try { localStorage.setItem(LS.output, selectedOutputDeviceId); } catch (e) {}
        try {
            if (voipDevice?.audio?.speakerDevices?.set) {
                await voipDevice.audio.speakerDevices.set([selectedOutputDeviceId]);
                toast('Speaker updated');
            }
        } catch (e) { toast('Could not switch speaker', 'error'); }
    });

    el.testTone.addEventListener('click', () => {
        try {
            const ctx = new (window.AudioContext || window.webkitAudioContext)();
            const osc = ctx.createOscillator();
            const gain = ctx.createGain();
            osc.frequency.value = 440;
            osc.type = 'sine';
            gain.gain.value = 0.12;
            osc.connect(gain); gain.connect(ctx.destination);
            osc.start();
            setTimeout(() => { osc.stop(); ctx.close().catch(() => {}); }, 600);
        } catch (e) { toast('Test tone failed', 'error'); }
    });

    // ═══ Outbound dial ════════════════════════════════════════════════════════
    async function dialContact(contact) {
        if (voipCall) { toast('Already on a call — hang up first', 'warn'); return; }
        if (!deviceReady) {
            const ok = await initDevice();
            if (!ok) return;
            await new Promise(r => setTimeout(r, 800));
            if (!deviceReady) { toast('Voice not ready yet', 'warn'); return; }
        }

        setContactPreview(contact);
        if (contact.contactId) fetchIntelligence(contact.contactId);
        setState('dialing');
        stopTimer();
        el.timer.textContent = '00:00';
        session.calls++;
        updateSessionStats();

        try {
            const params = { params: { To: contact.phone } };
            if (selectedInputDeviceId) {
                params.rtcConstraints = { audio: { deviceId: { ideal: selectedInputDeviceId } } };
            }
            const call = await voipDevice.connect(params);
            bindCall(call);
        } catch (e) {
            console.error('[popout] connect failed:', e);
            toast('Call failed: ' + (e.message || e), 'error');
            setState('error');
            setTimeout(() => setState('idle'), 2200);
        }
    }

    function bindCall(call) {
        voipCall = call;
        isMuted = false;
        isOnHold = false;
        el.muteBtn.setAttribute('aria-pressed', 'false');
        el.holdBtn.setAttribute('aria-pressed', 'false');
        const muteIcon = el.muteBtn.querySelector('i');
        if (muteIcon) muteIcon.className = 'fa-solid fa-microphone';

        call.on('ringing', () => setState('ringing'));
        call.on('accept', () => {
            setState('connected');
            startTimer();
            session.connected++;
            updateSessionStats();
        });
        call.on('disconnect', () => finalizeCall('ended'));
        call.on('cancel',    () => finalizeCall('canceled'));
        call.on('error',     (err) => {
            console.error('[popout] call error:', err);
            finalizeCall('error');
        });
    }

    function finalizeCall(reason) {
        if (!voipCall && reason !== 'error') return;
        const sid = voipCall?.parameters?.CallSid || null;
        const durationMs = callStartMs ? Date.now() - callStartMs : 0;
        session.talkMs += durationMs;
        updateSessionStats();

        lastEndedCall = {
            contactId: currentContact?.contactId || null,
            sid, endedAt: Date.now(), durationMs, reason,
        };
        if (currentContact) {
            session.history.unshift({
                sid, direction: 'out',
                contact: { ...currentContact },
                durationMs, endedAt: Date.now(),
                reason,
            });
            if (session.history.length > 30) session.history.pop();
            renderHistory();
        }

        voipCall = null;
        stopTimer();
        setState(reason === 'error' ? 'error' : 'ending');
        sendToIframe({ type: 'POPUP_CALL_ENDED', contactId: lastEndedCall.contactId, sid, reason, durationMs });

        if (currentContact?.contactId || sid) showDisposition();

        setTimeout(() => {
            if (!voipCall) {
                setState('idle');
                el.timer.textContent = '00:00';
            }
        }, 2800);
    }

    function handleDialCommand(contact) {
        if (!contact || !contact.phone) { toast('Missing phone number', 'warn'); return; }
        dialContact(contact);
    }

    // ═══ Controls ═════════════════════════════════════════════════════════════
    el.muteBtn.addEventListener('click', () => toggleMute());
    el.holdBtn.addEventListener('click', () => toggleHold());
    el.keypadBtn.addEventListener('click', () => toggleKeypad());
    el.hangupBtn.addEventListener('click', () => { if (voipCall) { try { voipCall.disconnect(); } catch (e) {} } });
    el.transferBtn.addEventListener('click', () => openTransferModal());
    el.voicemailBtn.addEventListener('click', () => dropVoicemail());
    el.meetBtn.addEventListener('click', () => {
        // Opens a new Google Meet instant room. The user copies the link
        // to send to the lead on the line.
        window.open('https://meet.google.com/new', '_blank', 'noopener');
    });

    // ─── Voicemail drop ─────────────────────────────────────────────
    // One-click: server redirects the live call leg to play the agent's
    // pre-recorded greeting, then hangs up. The agent's client leg
    // disconnects naturally via the normal `disconnect` event.
    let _vmDropInFlight = false;
    async function dropVoicemail() {
        if (!voipCall) { toast('No active call', 'warn'); return; }
        if (_vmDropInFlight) return;
        const callSid = voipCall.parameters?.CallSid || '';
        if (!callSid) { toast('Call SID not available yet — try again in a moment', 'warn'); return; }

        _vmDropInFlight = true;
        el.voicemailBtn.classList.add('is-dropping');
        el.voicemailBtn.disabled = true;
        try {
            const r = await fetch('/voice/voicemail-drop', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ call_sid: callSid }),
            });
            const d = await r.json();
            if (r.ok && d.status === 'dropped') {
                toast('Voicemail playing \u2014 will hang up automatically');
                // Do nothing else — Twilio will run the TwiML, play the
                // greeting, then Hangup. Our local `disconnect` handler
                // will fire and finalize the call normally.
            } else if (r.status === 400 && /No voicemail greeting/i.test(d.error || '')) {
                toast('No voicemail uploaded. Upload one in Voice Config first.', 'error', 6000);
            } else {
                toast(d.error || 'Voicemail drop failed', 'error', 5000);
            }
        } catch (e) {
            toast('Voicemail drop failed: ' + (e.message || e), 'error');
        } finally {
            _vmDropInFlight = false;
            el.voicemailBtn.disabled = false;
            // Keep `is-dropping` class for ~2s so the purple pulse is visible,
            // then clear. If the call already ended, the idle reset will run.
            setTimeout(() => el.voicemailBtn.classList.remove('is-dropping'), 2000);
        }
    }

    function toggleMute() {
        if (!voipCall) return;
        isMuted = !isMuted;
        voipCall.mute(isMuted || isOnHold);
        el.muteBtn.setAttribute('aria-pressed', isMuted ? 'true' : 'false');
        const icon = el.muteBtn.querySelector('i');
        if (icon) icon.className = isMuted ? 'fa-solid fa-microphone-slash' : 'fa-solid fa-microphone';
        sendToIframe({ type: 'POPUP_MUTE_CHANGED', muted: isMuted });
    }
    function toggleHold() {
        if (!voipCall) return;
        isOnHold = !isOnHold;
        voipCall.mute(isOnHold || isMuted);
        el.holdBtn.setAttribute('aria-pressed', isOnHold ? 'true' : 'false');
        setState(isOnHold ? 'hold' : 'connected');
    }
    function toggleKeypad() {
        keypadOpen = !keypadOpen;
        el.keypad.hidden = !keypadOpen;
        el.keypadBtn.setAttribute('aria-pressed', keypadOpen ? 'true' : 'false');
    }

    // DTMF
    el.keypad.addEventListener('click', (ev) => {
        const btn = ev.target.closest('.pop-key');
        if (!btn) return;
        const digit = btn.dataset.dtmf;
        if (voipCall && typeof voipCall.sendDigits === 'function') voipCall.sendDigits(digit);
    });

    // Manual dial
    el.manualDialBtn.addEventListener('click', () => {
        const raw = (el.manualPhone.value || '').trim();
        if (!raw) { toast('Enter a phone number', 'warn'); return; }
        dialContact({ phone: normalizePhone(raw), firstName: 'Manual', name: 'Manual dial', contactId: null });
    });
    el.manualPhone.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') el.manualDialBtn.click(); });

    function normalizePhone(raw) {
        let n = String(raw || '').replace(/[^\d+]/g, '');
        if (n.startsWith('+')) return n;
        if (n.length === 10) return '+1' + n;
        if (n.length === 11 && n.startsWith('1')) return '+' + n;
        return '+' + n;
    }

    // ═══ Drawers ══════════════════════════════════════════════════════════════
    function wireDrawer(toggle, body) {
        if (!toggle || !body) return;
        toggle.addEventListener('click', () => {
            const open = body.classList.toggle('is-open');
            toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
            if (open && body === el.historyBody) loadServerHistory();
        });
    }
    wireDrawer(el.queueToggle,   el.queueBody);
    wireDrawer(el.notesToggle,   el.notesBody);
    wireDrawer(el.historyToggle, el.historyBody);
    wireDrawer(el.intelToggle,   el.intelBody);

    // ═══ Queue ════════════════════════════════════════════════════════════════
    function renderQueue() {
        const filter = (el.queueSearch.value || '').trim().toLowerCase();
        const visible = filter
            ? mirroredQueue.filter(c => (
                (c.name || '').toLowerCase().includes(filter) ||
                (c.phone || '').includes(filter) ||
                (c.firstName || '').toLowerCase().includes(filter)
              ))
            : mirroredQueue;

        el.queueCount.textContent = String(mirroredQueue.length);

        if (!visible.length) {
            el.queueEmpty.style.display = 'flex';
            el.queueList.innerHTML = '';
            return;
        }
        el.queueEmpty.style.display = 'none';
        const frag = document.createDocumentFragment();
        visible.forEach((c, i) => {
            const li = document.createElement('li');
            li.className = 'pop-queue-item';
            if (currentContact && c.contactId === currentContact.contactId) li.classList.add('is-active');
            const statusClass =
                c.status === 'completed' || c.status === 'connected' ? 'is-done' :
                c.status === 'in-progress' || c.status === 'dialing' ? 'is-calling' :
                c.status === 'failed' || c.status === 'no-answer' ? 'is-failed' : '';
            li.innerHTML = `
                <span class="pop-queue-idx">${String(i + 1).padStart(2, '0')}</span>
                <span class="pop-queue-name">${escapeHtml(c.name || c.firstName || 'Lead')}</span>
                <span class="pop-queue-phone">${escapeHtml(c.phone || '')}</span>
                <span class="pop-queue-status ${statusClass}"></span>
            `;
            li.addEventListener('click', () => {
                if (voipCall) { toast('Hang up current call first', 'warn'); return; }
                dialContact(c);
            });
            frag.appendChild(li);
        });
        el.queueList.innerHTML = '';
        el.queueList.appendChild(frag);
    }

    el.queueSearch.addEventListener('input', renderQueue);

    // Queue run controls — proxy to iframe (iframe is source of truth)
    el.queueStart.addEventListener('click', () => {
        sendToIframe({ type: 'QUEUE_START' });
        toast('Queue start requested');
    });
    el.queueStop.addEventListener('click', () => {
        sendToIframe({ type: 'QUEUE_STOP' });
    });
    el.queueSkip.addEventListener('click', () => {
        if (voipCall) { try { voipCall.disconnect(); } catch (e) {} }
        sendToIframe({ type: 'QUEUE_SKIP' });
    });

    // ═══ Intelligence ═════════════════════════════════════════════════════════
    async function fetchIntelligence(contactId) {
        if (!contactId) { el.intelDrawer.hidden = true; return; }
        el.intelDrawer.hidden = false;

        // Cache hit
        if (intelCache[contactId]) { renderIntelligence(intelCache[contactId]); return; }
        // Already in flight
        if (intelFetchInFlight.has(contactId)) return;

        showIntelLoading();
        intelFetchInFlight.add(contactId);
        try {
            const r = await fetch('/voice/contact-intelligence-bulk?ids=' + encodeURIComponent(contactId));
            const d = await r.json();
            const cached = (d && d.cached && d.cached[contactId]) || null;
            if (cached) {
                intelCache[contactId] = cached;
                renderIntelligence(cached);
            } else {
                // Not cached — request analysis
                showIntelEmpty();
                try {
                    await fetch('/voice/contact-intelligence-analyze', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ contact_ids: [contactId] }),
                    });
                    // Poll once after a few seconds
                    setTimeout(async () => {
                        try {
                            const rr = await fetch('/voice/contact-intelligence-bulk?ids=' + encodeURIComponent(contactId));
                            const dd = await rr.json();
                            const c2 = (dd && dd.cached && dd.cached[contactId]) || null;
                            if (c2) { intelCache[contactId] = c2; renderIntelligence(c2); }
                        } catch (e) { /* ignore */ }
                    }, 6000);
                } catch (e) { /* ignore */ }
            }
        } catch (e) {
            console.warn('[popout] intel fetch failed:', e);
            showIntelEmpty();
        } finally {
            intelFetchInFlight.delete(contactId);
        }
    }

    function showIntelLoading() {
        el.intelLoading.classList.remove('hidden');
        el.intelEmpty.classList.add('hidden');
        el.intelCard.classList.add('hidden');
        el.intelBadge.textContent = '';
        el.intelBadge.className = 'pop-intel-badge';
    }
    function showIntelEmpty() {
        el.intelLoading.classList.add('hidden');
        el.intelEmpty.classList.remove('hidden');
        el.intelCard.classList.add('hidden');
    }
    function renderIntelligence(intel) {
        el.intelLoading.classList.add('hidden');
        el.intelEmpty.classList.add('hidden');
        el.intelCard.classList.remove('hidden');

        const temp = String(intel.temperature || '').toLowerCase();
        const score = Number(intel.score ?? 0);
        const reason = intel.temperature_reason || intel.reason || '';
        const summary = intel.summary || '';
        const actions = Array.isArray(intel.actions) ? intel.actions : [];

        // Ring
        const circumference = 113.097; // 2π × 18
        const pct = Math.max(0, Math.min(100, score));
        el.intelRing.style.strokeDashoffset = String(circumference * (1 - pct / 100));
        el.intelScoreNum.textContent = String(score || '—');

        // Temperature classes
        el.intelScoreRing.classList.remove('is-hot','is-warm','is-cool','is-cold');
        el.intelTempLabel.classList.remove('is-hot','is-warm','is-cool','is-cold');
        el.intelBadge.classList.remove('is-hot','is-warm','is-cool','is-cold');
        if (['hot','warm','cool','cold'].includes(temp)) {
            el.intelScoreRing.classList.add('is-' + temp);
            el.intelTempLabel.classList.add('is-' + temp);
            el.intelBadge.classList.add('is-' + temp);
        }
        el.intelTempLabel.textContent = (temp || 'unknown').toUpperCase();
        el.intelTempReason.textContent = reason || '—';
        el.intelSummary.textContent = summary || 'No summary available.';
        el.intelBadge.textContent = temp ? temp.toUpperCase() : '';

        // Actions
        el.intelActions.innerHTML = '';
        actions.slice(0, 4).forEach(a => {
            const div = document.createElement('div');
            const p = String(a.priority || '').toLowerCase();
            div.className = 'pop-intel-action' +
                (p === 'high' ? ' is-priority-high' : p === 'medium' ? ' is-priority-med' : '');
            const icon = a.icon && String(a.icon).startsWith('fa-') ? a.icon : 'fa-arrow-right';
            div.innerHTML = `<i class="fa-solid ${escapeHtml(icon)}"></i><span>${escapeHtml(a.action || a.text || '')}</span>`;
            el.intelActions.appendChild(div);
        });

        // Mirror to avatar temp badge on call card
        if (currentContact) {
            currentContact.temperature = temp;
            currentContact.score = score;
            applyContactTemp(temp, score);
        }
    }

    // ═══ Notes ════════════════════════════════════════════════════════════════
    el.notesArea.addEventListener('input', () => {
        el.notesIndicator.hidden = !el.notesArea.value.trim();
    });
    el.notesClear.addEventListener('click', () => {
        el.notesArea.value = '';
        el.notesIndicator.hidden = true;
    });

    // ═══ History ══════════════════════════════════════════════════════════════
    async function loadServerHistory() {
        try {
            const r = await fetch('/voice/call-history?limit=20&offset=0');
            if (!r.ok) return;
            const d = await r.json();
            const items = Array.isArray(d.calls) ? d.calls : (Array.isArray(d) ? d : []);
            // Merge with session history, dedupe by sid
            const merged = [...session.history];
            items.forEach(it => {
                if (!merged.some(m => m.sid === it.call_sid)) {
                    merged.push({
                        sid: it.call_sid,
                        direction: it.direction || 'out',
                        contact: {
                            name: it.contact_name || it.to_number || 'Unknown',
                            phone: it.to_number || it.from_number || '',
                        },
                        durationMs: (it.duration_seconds || 0) * 1000,
                        endedAt: it.ended_at ? new Date(it.ended_at).getTime() : Date.now(),
                    });
                }
            });
            merged.sort((a, b) => b.endedAt - a.endedAt);
            session.history = merged.slice(0, 30);
            renderHistory();
        } catch (e) { console.warn('[popout] history load:', e); }
    }

    function renderHistory() {
        if (!session.history.length) {
            el.historyEmpty.style.display = 'flex';
            el.historyList.innerHTML = '';
            return;
        }
        el.historyEmpty.style.display = 'none';
        const frag = document.createDocumentFragment();
        session.history.forEach(h => {
            const li = document.createElement('li');
            li.className = 'pop-history-item';
            const dirClass = h.direction === 'in' ? 'is-in' : 'is-out';
            const dirIcon  = h.direction === 'in' ? 'fa-arrow-down' : 'fa-arrow-up';
            const timeStr = formatRelativeTime(h.endedAt);
            li.innerHTML = `
                <span class="pop-history-dir ${dirClass}"><i class="fa-solid ${dirIcon}"></i></span>
                <span class="pop-history-name">${escapeHtml(h.contact?.name || 'Unknown')}</span>
                <span class="pop-history-dur">${formatDuration(h.durationMs || 0)}</span>
                <span class="pop-history-time">${escapeHtml(timeStr)}</span>
            `;
            li.addEventListener('click', () => {
                if (voipCall) return;
                if (h.contact?.phone) dialContact(h.contact);
            });
            frag.appendChild(li);
        });
        el.historyList.innerHTML = '';
        el.historyList.appendChild(frag);
    }

    function formatRelativeTime(ts) {
        if (!ts) return '';
        const diff = Date.now() - ts;
        if (diff < 60_000)    return 'just now';
        if (diff < 3_600_000) return Math.floor(diff / 60_000) + 'm';
        if (diff < 86_400_000) return Math.floor(diff / 3_600_000) + 'h';
        return Math.floor(diff / 86_400_000) + 'd';
    }

    // ═══ Disposition ══════════════════════════════════════════════════════════
    function showDisposition() { el.dispDrawer.classList.remove('hidden'); }
    function hideDisposition() {
        el.dispDrawer.classList.add('hidden');
        el.notesArea.value = '';
        el.notesIndicator.hidden = true;
    }
    el.dispSkip.addEventListener('click', hideDisposition);

    el.dispDrawer.querySelectorAll('.pop-disp-btn').forEach(btn => {
        btn.addEventListener('click', async () => {
            const disp = btn.dataset.disp;
            const notes = (el.notesArea.value || '').trim();
            const payload = {
                call_sid: lastEndedCall?.sid || null,
                contact_id: lastEndedCall?.contactId || null,
                disposition: disp,
                notes,
            };

            // Relay to iframe (iframe updates its own dialerQueue) AND save directly
            sendToIframe({ type: 'POPUP_DISPOSITION', ...payload });

            try {
                if (payload.call_sid) {
                    await fetch('/voice/call-disposition', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ call_sid: payload.call_sid, disposition: disp }),
                    });
                }
            } catch (e) { /* iframe is authoritative fallback */ }

            toast('Saved: ' + disp.replace(/_/g, ' '));
            hideDisposition();
        });
    });

    // ═══ Transfer modal ═══════════════════════════════════════════════════════
    function openTransferModal() {
        if (!voipCall) { toast('No active call to transfer', 'warn'); return; }
        renderTransferRecents();
        el.transferModal.classList.remove('hidden');
        setTimeout(() => el.transferPhone.focus(), 50);
    }
    function closeTransferModal() { el.transferModal.classList.add('hidden'); }

    el.transferModal.querySelectorAll('[data-close-modal]').forEach(b =>
        b.addEventListener('click', closeTransferModal));

    el.transferConfirm.addEventListener('click', async () => {
        const raw = (el.transferPhone.value || '').trim();
        if (!raw) { toast('Enter a transfer number', 'warn'); return; }
        if (!voipCall || !lastEndedCall?.sid && !voipCall.parameters?.CallSid) {
            // voipCall.parameters.CallSid is the live sid
        }
        const callSid = voipCall.parameters?.CallSid || '';
        if (!callSid) { toast('Active call SID not available yet', 'error'); return; }
        const number = normalizePhone(raw);
        try {
            el.transferConfirm.disabled = true;
            const r = await fetch('/voice/transfer', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ call_sid: callSid, transfer_to: number }),
            });
            const d = await r.json();
            if (r.ok && d.status === 'transferred') {
                toast('Transferred to ' + number);
                // Save to recents
                session.recentXferNumbers = [number, ...session.recentXferNumbers.filter(n => n !== number)].slice(0, 6);
                try { localStorage.setItem(LS.xfer, JSON.stringify(session.recentXferNumbers)); } catch (e) {}
                closeTransferModal();
                // The transfer detaches our leg; let existing disconnect handler clean up.
            } else {
                toast(d.error || 'Transfer failed', 'error');
            }
        } catch (e) {
            toast('Transfer failed: ' + (e.message || e), 'error');
        } finally {
            el.transferConfirm.disabled = false;
        }
    });

    function renderTransferRecents() {
        el.transferRecents.innerHTML = '';
        if (!session.recentXferNumbers.length) return;
        session.recentXferNumbers.forEach(n => {
            const chip = document.createElement('button');
            chip.type = 'button';
            chip.className = 'pop-recent-chip';
            chip.textContent = n;
            chip.addEventListener('click', () => { el.transferPhone.value = n; });
            el.transferRecents.appendChild(chip);
        });
    }

    // ═══ Settings modal ═══════════════════════════════════════════════════════
    el.settingsBtn.addEventListener('click', () => {
        populateAudioDevices();
        el.settingsModal.classList.remove('hidden');
    });
    el.settingsModal.querySelectorAll('[data-close-modal]').forEach(b =>
        b.addEventListener('click', () => el.settingsModal.classList.add('hidden')));

    // ═══ Incoming ring ════════════════════════════════════════════════════════
    function showIncomingRing(call) {
        pendingIncoming = call;
        const from = call.parameters?.From || 'Unknown';
        const name = call.customParameters?.get?.('ContactName') || 'Incoming caller';
        el.incomingAvatar.textContent = (name[0] || '?').toUpperCase();
        el.incomingName.textContent = name;
        el.incomingPhone.textContent = from;
        el.incoming.classList.remove('hidden');

        call.on('cancel', hideIncomingRing);
        call.on('disconnect', hideIncomingRing);
        call.on('error', hideIncomingRing);
    }
    function hideIncomingRing() {
        pendingIncoming = null;
        el.incoming.classList.add('hidden');
    }
    el.incomingAccept.addEventListener('click', () => {
        if (!pendingIncoming) return;
        const c = pendingIncoming;
        hideIncomingRing();
        try {
            c.accept();
            bindCall(c);
            setContactPreview({
                name: c.parameters?.From || 'Incoming',
                phone: c.parameters?.From || '',
                contactId: null,
            });
        } catch (e) { toast('Accept failed', 'error'); }
    });
    el.incomingDecline.addEventListener('click', () => {
        if (!pendingIncoming) return;
        try { pendingIncoming.reject(); } catch (e) {}
        hideIncomingRing();
    });

    // ═══ Session stats ════════════════════════════════════════════════════════
    function updateSessionStats() {
        el.statCalls.textContent = String(session.calls);
        el.statConnected.textContent = String(session.connected);
        el.statTalk.textContent = formatDuration(session.talkMs);
    }

    // ═══ Keyboard shortcuts ═══════════════════════════════════════════════════
    document.addEventListener('keydown', (ev) => {
        // Skip if typing in inputs
        const tag = (ev.target?.tagName || '').toLowerCase();
        if (['input','textarea','select'].includes(tag)) {
            if (ev.key === 'Escape') ev.target.blur();
            return;
        }
        // Close modals on Esc
        if (ev.key === 'Escape') {
            if (!el.transferModal.classList.contains('hidden')) { closeTransferModal(); return; }
            if (!el.settingsModal.classList.contains('hidden')) { el.settingsModal.classList.add('hidden'); return; }
            if (!el.incoming.classList.contains('hidden')) { el.incomingDecline.click(); return; }
            if (keypadOpen) { toggleKeypad(); return; }
        }
        // In-call shortcuts
        if (voipCall) {
            if (ev.key === 'm' || ev.key === 'M') { ev.preventDefault(); toggleMute(); return; }
            if (ev.key === 'h' || ev.key === 'H') { ev.preventDefault(); try { voipCall.disconnect(); } catch (e) {} return; }
            if (ev.key === ' ')                     { ev.preventDefault(); toggleHold(); return; }
            if (ev.key === 'k' || ev.key === 'K') { ev.preventDefault(); toggleKeypad(); return; }
            if (ev.key === 't' || ev.key === 'T') { ev.preventDefault(); openTransferModal(); return; }
            if (ev.key === 'v' || ev.key === 'V') { ev.preventDefault(); dropVoicemail(); return; }
        } else {
            // Queue slot dialing 1-9
            if (/^[1-9]$/.test(ev.key)) {
                const idx = parseInt(ev.key, 10) - 1;
                const c = mirroredQueue[idx];
                if (c) { ev.preventDefault(); dialContact(c); }
            }
        }
    });

    // ═══ Toasts ═══════════════════════════════════════════════════════════════
    function toast(msg, kind, ms) {
        const t = document.createElement('div');
        t.className = 'pop-toast' + (kind === 'error' ? ' is-error' : kind === 'warn' ? ' is-warn' : '');
        t.textContent = msg;
        el.toastStack.appendChild(t);
        setTimeout(() => {
            t.style.transition = 'opacity 200ms ease';
            t.style.opacity = '0';
            setTimeout(() => t.remove(), 220);
        }, ms || 3400);
    }

    function escapeHtml(s) {
        return String(s || '')
            .replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    // ═══ Boot ═════════════════════════════════════════════════════════════════
    setState('idle');
    setContactPreview(null);
    renderQueue();
    renderHistory();
    updateSessionStats();
    initDevice();
})();
