        // ===== VOICE DIALER TAB =====
        let dialerContacts = [];
        let dialerSelected = new Set();
        let dialerQueue = [];
        let dialerQueueRunning = false;
        // InsuranceGrokBot engagement data cache: contactId → { messages, calls }
        let _igbEngagementCache = {};
        // InsuranceGrokBot AI intelligence cache: contactId → { temperature, score, summary }
        // Populated fresh on every dialer load from server cache (24h TTL).
        let _igbIntelCache = {};
        // Contacts that need AI analysis (no cached intelligence)
        let _igbUncachedIds = [];
        // Whether batch AI analysis is currently running
        let _igbAnalyzing = false;
        // InsuranceGrokBot Smart Filter collapsed state
        let _igbFilterCollapsed = { not_showing: true };
        let dialerCallSid = null;
        let dialerCallIdx = -1;
        let dialerPollTimer = null;
        let _dialerCallConnected = false;

        // ── Caller Number Selection State ──
        let _dialerNumbers = [];          // [{phone, nickname, is_primary, capabilities}]
        let _dialerSelectedNumber = localStorage.getItem('dialerFromNumber') || '';  // '' = auto

        // ── AI Minutes Balance State ──
        let _aiMinutesBalance = null;  // null = not loaded yet, number = current balance
        let _aiMinutesHasPurchased = false;  // true if user has ever purchased minutes

        // ── Multi-Line Dialer State ──
        // Map<call_sid, {contact_id, phone, name, status, duration, dial_mode, attempt, line_index}>
        let _multiLineActive = new Map();
        let _multiLineEnabled = false;   // Set true when user has pro_dialer tier
        let _multiLineMaxLines = 1;       // Default 1 (single line); up to 4 for pro_dialer
        let _multiLinePollTimer = null;   // Interval for batch status polling
        let _multiLineQueueIdx = -1;     // Current position in queue for multi-line
        let _multiLineConnectedSid = null; // The call_sid the agent is currently talking to

        // ── Predictive Dialer State ──
        let _predictiveEnabled = false;
        let _predictiveStats = null;     // {connect_rate, recommended_lines, dial_ratio}
        let _predictiveAutoLines = 3;    // AI-recommended concurrent lines
        let dialerSearchTimer = null;
        let _dialerAllContacts = [];  // Full unfiltered contact list for local search
        let dialerActiveContact = null; // currently selected contact in middle panel
        let dialerPipelines = [];
        let _dialerFetchGen = 0;       // Generation counter to discard stale contact fetches
        let _dialerRefreshPoll = null;  // Interval ID for background refresh polling
        let dialerMaxAttempts = window.DASHBOARD_BOOT?.dialMaxAttempts ?? 2;
        // Enterprise dialer settings (loaded from voice_config via DASHBOARD_BOOT)
        let _dialerRingTimeout = (window.DASHBOARD_BOOT?.ringTimeout ?? 45) * 1000; // ms
        let _dialerPauseBetween = (window.DASHBOARD_BOOT?.pauseBetween ?? 1) * 1000; // ms
        let _dialerRetryDelay = (window.DASHBOARD_BOOT?.retryDelay ?? 2) * 1000; // ms
        let _dialerMaxCallDuration = (window.DASHBOARD_BOOT?.maxCallDuration ?? 0) * 60 * 1000; // ms (0=no limit)
        let _dialerAutoCallback = window.DASHBOARD_BOOT?.autoCallback ?? false;
        let _dialerCallDurationTimer = null; // Timer for max call duration enforcement
        // Multi-line dialer settings (loaded from voice_config via DASHBOARD_BOOT)
        let _dialerWrapUpTime = (window.DASHBOARD_BOOT?.wrapUpTime ?? 15) * 1000; // ms
        let _dialerRequireDisposition = window.DASHBOARD_BOOT?.requireDisposition ?? true;
        let _dialerAutoDispNoAnswer = window.DASHBOARD_BOOT?.autoDispositionNoAnswer ?? true;
        let _dialerAutoDispVoicemail = window.DASHBOARD_BOOT?.autoDispositionVoicemail ?? true;
        let _dialerOnMachineAction = window.DASHBOARD_BOOT?.onMachineAction ?? 'hangup';
        let _dialerWrapUpTimer = null; // Timer for wrap-up countdown
        let _dialerWrapUpActive = false; // Whether wrap-up is currently in progress
        let _multiLineDialingInProgress = false; // Guard against concurrent multiLineDialBatch calls
        let _multiLinePauseTimer = null; // Timer for pause-between-calls delay
        let _multiLinePollErrors = 0; // Error counter for poll backoff
        let _multiLineDispToastShown = false; // Debounce disposition toast

        // ── Solo Predictive Auto-Pacing State ──
        let _sessionCallsTotal = 0;         // Calls dialed this session
        let _sessionCallsConnected = 0;     // Calls connected this session
        let _sessionTotalTalkTime = 0;      // Total talk seconds this session
        let _predictiveRecommendedLines = 2; // Live Erlang-C recommendation
        let _predictivePollCount = 0;        // Counter for periodic re-fetch
        let _isSoloPredictive = (window.DASHBOARD_BOOT?.subscriptionTier === 'solo_predictive');
        let _aiMinutesWarning = null;        // Current warning level: ok, low, critical, empty
        let _aiMinutesBannerShown = false;   // Prevent duplicate banners
        let _overflowAlertPollTimer = null;  // Timer for polling overflow transfer alerts
        let _overflowAlertShown = {};        // Track shown alerts by call_sid to prevent duplicates

        // ── iPhone 15 Pro UI bridge ──
        let _iosCurrentApp = null;

        function iosOpenApp(app) {
            _iosCurrentApp = app;
            const home = document.getElementById('iosHome');
            const apps = { messages: 'iosAppMessages', calls: 'iosAppCalls', recordings: 'iosAppRecordings', voicemail: 'iosAppVoicemail', inbox: 'iosAppInbox', calendar: 'iosAppCalendar', discord: 'iosAppDiscord', slack: 'iosAppSlack', insights: 'iosAppInsights', stats: 'iosAppStats' };
            if (home) home.style.display = 'none';
            Object.keys(apps).forEach(k => {
                const el = document.getElementById(apps[k]);
                if (el) { el.style.display = k === app ? 'flex' : 'none'; el.style.animation = k === app ? 'iosAppOpen 0.3s cubic-bezier(0.2,0.9,0.3,1)' : ''; }
            });
            // Trigger data load
            if (app === 'messages') dlrRefreshMessages();
            if (app === 'calls') dialerLoadAllCallHistory();
            if (app === 'recordings') dialerLoadRecordings();
            if (app === 'voicemail') vmLoad();
            if (app === 'inbox') inboxRefresh();
            if (app === 'calendar') calendarInit();
            if (app === 'discord') iosDiscordInit();
            if (app === 'slack') iosSlackInit();
            if (app === 'insights') iosInsightsInit();
            if (app === 'stats') iosStatsLoad();
        }

        function iosGoHome() {
            const home = document.getElementById('iosHome');
            const apps = ['iosAppMessages', 'iosAppCalls', 'iosAppRecordings', 'iosAppVoicemail', 'iosAppInbox', 'iosAppCalendar', 'iosAppDiscord', 'iosAppSlack', 'iosAppInsights', 'iosAppStats'];
            apps.forEach(id => { const el = document.getElementById(id); if (el) el.style.display = 'none'; });
            if (home) home.style.display = 'flex';
            _iosCurrentApp = null;
            // On mobile, close the full-screen app overlay
            if (window.innerWidth <= 768 && typeof dlrMobileCloseApp === 'function') {
                dlrMobileCloseApp();
            }
        }

        // Live clock in status bar
        (function _iosClock() {
            function _upd() {
                const el = document.getElementById('iosTime');
                if (!el) return;
                const d = new Date();
                let h = d.getHours(), m = d.getMinutes();
                el.textContent = (h > 12 ? h - 12 : h || 12) + ':' + (m < 10 ? '0' : '') + m;
            }
            _upd(); setInterval(_upd, 30000);
        })();

        // ── Calendar App Icon: show today's date ──
        (function _iosCalIcon() {
            const days = ['SUN','MON','TUE','WED','THU','FRI','SAT'];
            const d = new Date();
            const dayEl = document.getElementById('iosCalIconDay');
            const dateEl = document.getElementById('iosCalIconDate');
            if (dayEl) dayEl.textContent = days[d.getDay()];
            if (dateEl) dateEl.textContent = d.getDate();
        })();

        // ═══════════════════════════════════════════════
        //  CALENDAR APP — Full Event Management UI
        // ═══════════════════════════════════════════════
        let _calViewYear, _calViewMonth;
        let _calSlotData = {};        // { "2026-03-01": ["iso_time",...], ... }
        let _calEventData = {};       // { "2026-03-01": [{id,title,startTime,endTime,status,...}], ... }
        let _calAllEvents = [];       // flat list of all events for current month
        let _calSelectedDate = null;
        let _calSelectedSlot = null;
        let _calCalendars = [];
        let _calActiveCalId = '';
        let _calInitialized = false;
        let _calEditingEvent = null;  // event being edited
        let _calGoogleConnected = false;  // Google Calendar connection state
        let _calGoogleEmail = '';         // Connected Google email

        async function calendarInit() {
            const picker = document.getElementById('iosCalendarPicker');
            if (!_calInitialized || !_calCalendars.length) {
                if (picker) picker.innerHTML = '<option value="">Loading calendars...</option>';
                let loaded = false;

                // Fetch GHL calendars and Google Calendar status in parallel
                const [ghlResult, googleResult] = await Promise.allSettled([
                    (async () => {
                        for (let attempt = 0; attempt < 3; attempt++) {
                            try {
                                if (attempt > 0) await new Promise(r => setTimeout(r, attempt * 2000));
                                const r = await fetch('/api/fetch-calendars', { signal: AbortSignal.timeout(12000) });
                                if (r.ok) {
                                    const d = await r.json();
                                    return d.calendars || [];
                                }
                                const errData = await r.json().catch(() => ({}));
                                console.warn(`[Calendar] Fetch calendars HTTP ${r.status} (attempt ${attempt + 1}):`, errData.error || '');
                                if (r.status === 401 || r.status === 403) {
                                    if (picker) picker.innerHTML = '<option value="">Reconnect CRM to load calendars</option>';
                                    return null;
                                }
                            } catch(e) {
                                console.warn(`[Calendar] Fetch calendars failed (attempt ${attempt + 1}):`, e.message || e);
                            }
                        }
                        return null;
                    })(),
                    fetch('/api/google-calendar/status').then(r => r.ok ? r.json() : null).catch(() => null),
                ]);

                // Process GHL calendars
                if (ghlResult.status === 'fulfilled' && ghlResult.value) {
                    _calCalendars = ghlResult.value;
                    if (picker && _calCalendars.length) {
                        picker.innerHTML = _calCalendars.map(c =>
                            `<option value="${dialerEsc(c.id)}">${dialerEsc(c.name)}</option>`
                        ).join('');
                        _calActiveCalId = _calCalendars[0].id;
                    } else if (picker && !picker.innerHTML.includes('Reconnect')) {
                        picker.innerHTML = '<option value="">No calendars found</option>';
                    }
                    loaded = true;
                }
                if (!loaded && picker && !picker.innerHTML.includes('Reconnect')) {
                    picker.innerHTML = '<option value="">Failed to load — tap refresh</option>';
                }
                _calInitialized = loaded;

                // Process Google Calendar status
                if (googleResult.status === 'fulfilled' && googleResult.value) {
                    _calGoogleConnected = googleResult.value.connected || false;
                    _calGoogleEmail = googleResult.value.email || '';
                }
                _calUpdateGoogleBtn();
            }
            const now = new Date();
            _calViewYear = now.getFullYear();
            _calViewMonth = now.getMonth();
            _calSelectedDate = `${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,'0')}-${String(now.getDate()).padStart(2,'0')}`;
            _calSlotData = {};
            _calEventData = {};
            calendarRenderMonth();
            calendarFetchMonthData();
        }

        function _calUpdateGoogleBtn() {
            const btn = document.getElementById('iosCalGoogleBtn');
            if (!btn) return;
            if (_calGoogleConnected) {
                btn.innerHTML = '<i class="fa-brands fa-google" style="color:#34C759;"></i>';
                btn.title = 'Google Calendar connected' + (_calGoogleEmail ? ' (' + _calGoogleEmail + ')' : '');
                btn.onclick = function() { _calShowGoogleMenu(); };
            } else {
                btn.innerHTML = '<i class="fa-brands fa-google" style="color:#666;"></i>';
                btn.title = 'Connect Google Calendar';
                btn.onclick = function() { window.location.href = '/google-calendar/connect'; };
            }
        }

        function _calShowGoogleMenu() {
            const existing = document.getElementById('iosCalGoogleMenu');
            if (existing) { existing.remove(); return; }
            const btn = document.getElementById('iosCalGoogleBtn');
            const menu = document.createElement('div');
            menu.id = 'iosCalGoogleMenu';
            menu.style.cssText = 'position:absolute;top:100%;right:0;background:rgba(30,30,30,0.95);border:1px solid rgba(255,255,255,0.1);border-radius:10px;padding:8px 0;min-width:180px;z-index:100;backdrop-filter:blur(12px);box-shadow:0 8px 24px rgba(0,0,0,0.4);';
            menu.innerHTML = `
                <div style="padding:6px 14px;font-size:0.75rem;color:#34C759;font-weight:600;"><i class="fa-brands fa-google" style="margin-right:4px;"></i> Connected${_calGoogleEmail ? ' — ' + dialerEsc(_calGoogleEmail) : ''}</div>
                <div style="height:1px;background:rgba(255,255,255,0.06);margin:4px 0;"></div>
                <div onclick="window.location.href='/google-calendar/disconnect'" style="padding:8px 14px;font-size:0.75rem;color:#FF3B30;cursor:pointer;" onmouseover="this.style.background='rgba(255,59,48,0.1)'" onmouseout="this.style.background='none'"><i class="fa-solid fa-unlink" style="margin-right:6px;"></i>Disconnect</div>
            `;
            btn.parentElement.style.position = 'relative';
            btn.parentElement.appendChild(menu);
            setTimeout(() => {
                const close = (e) => { if (!menu.contains(e.target) && e.target !== btn) { menu.remove(); document.removeEventListener('click', close); } };
                document.addEventListener('click', close);
            }, 10);
        }

        function calendarSwitchCalendar() {
            const picker = document.getElementById('iosCalendarPicker');
            if (picker) _calActiveCalId = picker.value;
            _calSlotData = {};
            _calEventData = {};
            _calSelectedDate = null;
            calendarRenderMonth();
            calendarFetchMonthData();
        }

        async function calendarFetchMonthData() {
            const calId = _calActiveCalId;
            // Fetch events, slots, and Google Calendar events in parallel
            const startDate = `${_calViewYear}-${String(_calViewMonth+1).padStart(2,'0')}-01`;
            const daysInMonth = new Date(_calViewYear, _calViewMonth + 1, 0).getDate();
            const endDate = `${_calViewYear}-${String(_calViewMonth+1).padStart(2,'0')}-${String(daysInMonth).padStart(2,'0')}`;

            // Build parallel fetch array
            const fetches = [
                calId ? fetch(`/api/calendar/events?calendar_id=${encodeURIComponent(calId)}&start=${startDate}&end=${endDate}`).then(r => r.ok ? r.json() : null) : Promise.resolve(null),
                calId ? fetch(`/api/calendar/slots?calendar_id=${encodeURIComponent(calId)}&days=45`).then(r => r.ok ? r.json() : null) : Promise.resolve(null),
                _calGoogleConnected ? fetch(`/api/google-calendar/events?start=${startDate}&end=${endDate}`).then(r => r.ok ? r.json() : null) : Promise.resolve(null),
            ];

            const [eventsRes, slotsRes, googleRes] = await Promise.allSettled(fetches);

            // Process GHL events
            _calEventData = {};
            _calAllEvents = [];
            if (eventsRes.status === 'fulfilled' && eventsRes.value) {
                const events = eventsRes.value.events || [];
                events.forEach(ev => {
                    ev.source = ev.source || 'ghl';
                    _calAllEvents.push(ev);
                    const st = ev.startTime || ev.start;
                    if (!st) return;
                    const dateKey = new Date(st).toLocaleDateString('en-CA'); // YYYY-MM-DD
                    if (!_calEventData[dateKey]) _calEventData[dateKey] = [];
                    _calEventData[dateKey].push(ev);
                });
            }

            // Merge Google Calendar events
            if (googleRes.status === 'fulfilled' && googleRes.value) {
                const gEvents = googleRes.value.events || [];
                gEvents.forEach(ev => {
                    ev.source = 'google';
                    _calAllEvents.push(ev);
                    const st = ev.startTime || ev.start;
                    if (!st) return;
                    // For all-day events, the date is just YYYY-MM-DD
                    let dateKey;
                    if (ev.allDay && st.length === 10) {
                        dateKey = st;
                    } else {
                        dateKey = new Date(st).toLocaleDateString('en-CA');
                    }
                    if (!_calEventData[dateKey]) _calEventData[dateKey] = [];
                    _calEventData[dateKey].push(ev);
                });
            }

            // Process slots
            _calSlotData = {};
            if (slotsRes.status === 'fulfilled' && slotsRes.value) {
                _calSlotData = slotsRes.value.slots || {};
            }

            calendarRenderMonth();
        }

        function calendarPrevMonth() {
            _calViewMonth--;
            if (_calViewMonth < 0) { _calViewMonth = 11; _calViewYear--; }
            _calSelectedDate = null;
            calendarRenderMonth();
            calendarFetchMonthData();
        }

        function calendarNextMonth() {
            _calViewMonth++;
            if (_calViewMonth > 11) { _calViewMonth = 0; _calViewYear++; }
            _calSelectedDate = null;
            calendarRenderMonth();
            calendarFetchMonthData();
        }

        function _calFormatTime(dt) {
            const h = dt.getHours(), m = dt.getMinutes();
            const ampm = h >= 12 ? 'PM' : 'AM';
            const h12 = h > 12 ? h - 12 : (h === 0 ? 12 : h);
            return `${h12}:${String(m).padStart(2,'0')} ${ampm}`;
        }

        function calendarRenderMonth() {
            const months = ['January','February','March','April','May','June','July','August','September','October','November','December'];
            const title = document.getElementById('iosCalMonthTitle');
            if (title) title.textContent = `${months[_calViewMonth]} ${_calViewYear}`;

            const grid = document.getElementById('iosCalGrid');
            if (!grid) return;

            const today = new Date();
            const todayStr = `${today.getFullYear()}-${String(today.getMonth()+1).padStart(2,'0')}-${String(today.getDate()).padStart(2,'0')}`;
            const firstDay = new Date(_calViewYear, _calViewMonth, 1).getDay();
            const daysInMonth = new Date(_calViewYear, _calViewMonth + 1, 0).getDate();

            let html = '';
            for (let i = 0; i < firstDay; i++) {
                html += '<div style="aspect-ratio:1;"></div>';
            }
            for (let d = 1; d <= daysInMonth; d++) {
                const dateStr = `${_calViewYear}-${String(_calViewMonth+1).padStart(2,'0')}-${String(d).padStart(2,'0')}`;
                const isToday = dateStr === todayStr;
                const hasEvents = _calEventData[dateStr] && _calEventData[dateStr].length > 0;
                const hasSlots = _calSlotData[dateStr] && _calSlotData[dateStr].length > 0;
                const isSelected = dateStr === _calSelectedDate;
                const isPast = new Date(_calViewYear, _calViewMonth, d) < new Date(today.getFullYear(), today.getMonth(), today.getDate());

                let bg = 'transparent';
                let color = isPast ? '#555' : '#fff';
                let border = 'transparent';
                let cursor = 'pointer';

                if (isSelected) { bg = '#FF3B30'; color = '#fff'; border = '#FF3B30'; }
                else if (isToday) { bg = 'rgba(255,59,48,0.15)'; border = 'rgba(255,59,48,0.4)'; color = '#FF3B30'; }

                // Dots: green for GHL events, blue for Google events, red for available slots
                const hasGhlEvents = hasEvents && _calEventData[dateStr].some(e => e.source !== 'google');
                const hasGoogleEvents = hasEvents && _calEventData[dateStr].some(e => e.source === 'google');
                let dots = '';
                if (hasGhlEvents || hasGoogleEvents || (hasSlots && !isPast)) {
                    dots = '<div style="display:flex;gap:2px;justify-content:center;margin-top:1px;">';
                    if (hasGhlEvents) dots += `<div style="width:4px;height:4px;border-radius:50%;background:${isSelected?'#fff':'#34C759'};"></div>`;
                    if (hasGoogleEvents) dots += `<div style="width:4px;height:4px;border-radius:50%;background:${isSelected?'rgba(200,220,255,0.8)':'#4285F4'};"></div>`;
                    if (hasSlots && !isPast) dots += `<div style="width:4px;height:4px;border-radius:50%;background:${isSelected?'rgba(255,255,255,0.5)':'#FF3B30'};"></div>`;
                    dots += '</div>';
                }

                html += `<div onclick="calendarSelectDate('${dateStr}')" style="aspect-ratio:1;display:flex;flex-direction:column;align-items:center;justify-content:center;border-radius:10px;background:${bg};border:1px solid ${border};cursor:${cursor};transition:all 0.15s;">`;
                html += `<span style="font-size:0.78rem;font-weight:${isToday||isSelected?'700':'500'};color:${color};line-height:1;">${d}</span>`;
                html += dots;
                html += '</div>';
            }
            grid.innerHTML = html;

            // Update content area below calendar
            const dateHdr = document.getElementById('iosCalDateHeader');
            const slotsDiv = document.getElementById('iosCalSlots');
            const emptyDiv = document.getElementById('iosCalEmpty');
            if (_calSelectedDate) {
                const dayEvents = _calEventData[_calSelectedDate] || [];
                const daySlots = _calSlotData[_calSelectedDate] || [];
                if (dayEvents.length || daySlots.length) {
                    if (dateHdr) dateHdr.style.display = 'block';
                    if (emptyDiv) emptyDiv.style.display = 'none';
                    const selDate = new Date(_calSelectedDate + 'T12:00:00');
                    const dayNames = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
                    const monthNames = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
                    const label = document.getElementById('iosCalDateLabel');
                    if (label) label.textContent = `${dayNames[selDate.getDay()]}, ${monthNames[selDate.getMonth()]} ${selDate.getDate()}`;
                    calendarRenderDayContent(dayEvents, daySlots);
                } else {
                    if (dateHdr) dateHdr.style.display = 'block';
                    if (emptyDiv) emptyDiv.style.display = 'none';
                    const selDate = new Date(_calSelectedDate + 'T12:00:00');
                    const dayNames = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
                    const monthNames = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
                    const label = document.getElementById('iosCalDateLabel');
                    if (label) label.textContent = `${dayNames[selDate.getDay()]}, ${monthNames[selDate.getMonth()]} ${selDate.getDate()}`;
                    if (slotsDiv) slotsDiv.innerHTML = '<div style="text-align:center;color:#666;font-size:0.78rem;padding:24px 0;">No events or available slots</div>';
                }
            } else {
                if (dateHdr) dateHdr.style.display = 'none';
                if (slotsDiv) slotsDiv.innerHTML = '';
                if (emptyDiv) emptyDiv.style.display = 'flex';
            }
        }

        function calendarSelectDate(dateStr) {
            _calSelectedDate = dateStr;
            calendarRenderMonth();
        }

        function calendarRenderDayContent(events, slots) {
            const container = document.getElementById('iosCalSlots');
            if (!container) return;
            let html = '';

            // Split events by source
            const ghlEvents = events.filter(e => e.source !== 'google');
            const googleEvents = events.filter(e => e.source === 'google');

            // ── GHL Appointments ──
            if (ghlEvents.length) {
                html += '<div style="font-size:0.75rem;color:#34C759;font-weight:600;text-transform:uppercase;letter-spacing:0.3px;padding:6px 0 4px;"><i class="fa-solid fa-calendar-check" style="margin-right:4px;"></i>Appointments</div>';
                ghlEvents.sort((a,b) => new Date(a.startTime || a.start) - new Date(b.startTime || b.start));
                ghlEvents.forEach(ev => {
                    const startDt = new Date(ev.startTime || ev.start);
                    const endDt = ev.endTime ? new Date(ev.endTime) : null;
                    const timeStr = _calFormatTime(startDt) + (endDt ? ' – ' + _calFormatTime(endDt) : '');
                    const title = ev.title || 'Appointment';

                    const statusColors = {
                        'confirmed': '#34C759', 'new': '#007AFF', 'showed': '#34C759',
                        'noshow': '#FF9500', 'cancelled': '#FF3B30', 'invalid': '#666',
                    };
                    const statusColor = statusColors[ev.status] || '#888';
                    const statusLabel = ev.status ? ev.status.charAt(0).toUpperCase() + ev.status.slice(1) : '';

                    html += `<div onclick="calendarOpenEvent('${dialerEsc(ev.id)}')" style="background:rgba(52,199,89,0.06);border:1px solid rgba(52,199,89,0.12);border-radius:10px;padding:10px 12px;margin-bottom:6px;cursor:pointer;transition:all 0.15s;" onmouseover="this.style.background='rgba(52,199,89,0.12)'" onmouseout="this.style.background='rgba(52,199,89,0.06)'">`;
                    html += `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:2px;">`;
                    html += `<span style="font-size:0.82rem;font-weight:700;color:#fff;">${dialerEsc(title)}</span>`;
                    if (statusLabel) html += `<span style="font-size:0.75rem;color:${statusColor};font-weight:600;background:${statusColor}18;padding:2px 6px;border-radius:4px;">${statusLabel}</span>`;
                    html += '</div>';
                    html += `<div style="font-size:0.75rem;color:#aaa;"><i class="fa-regular fa-clock" style="margin-right:3px;"></i>${timeStr}</div>`;
                    if (ev.notes) html += `<div style="font-size:0.75rem;color:#777;margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"><i class="fa-regular fa-note-sticky" style="margin-right:3px;"></i>${dialerEsc(ev.notes).substring(0, 60)}</div>`;
                    html += '</div>';
                });
            }

            // ── Google Calendar Events ──
            if (googleEvents.length) {
                html += '<div style="font-size:0.75rem;color:#4285F4;font-weight:600;text-transform:uppercase;letter-spacing:0.3px;padding:8px 0 4px;"><i class="fa-brands fa-google" style="margin-right:4px;"></i>Google Calendar</div>';
                googleEvents.sort((a,b) => {
                    if (a.allDay && !b.allDay) return -1;
                    if (!a.allDay && b.allDay) return 1;
                    return new Date(a.startTime || a.start || 0) - new Date(b.startTime || b.start || 0);
                });
                googleEvents.forEach(ev => {
                    const title = ev.title || 'No Title';
                    let timeStr = '';
                    if (ev.allDay) {
                        timeStr = 'All day';
                    } else {
                        const startDt = new Date(ev.startTime || ev.start);
                        const endDt = ev.endTime ? new Date(ev.endTime) : null;
                        timeStr = _calFormatTime(startDt) + (endDt ? ' – ' + _calFormatTime(endDt) : '');
                    }

                    html += `<div style="background:rgba(66,133,244,0.06);border:1px solid rgba(66,133,244,0.15);border-left:3px solid #4285F4;border-radius:10px;padding:10px 12px;margin-bottom:6px;transition:all 0.15s;">`;
                    html += `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:2px;">`;
                    html += `<span style="font-size:0.82rem;font-weight:700;color:#fff;">${dialerEsc(title)}</span>`;
                    html += `<span style="font-size:0.75rem;color:#4285F4;font-weight:600;background:rgba(66,133,244,0.12);padding:2px 6px;border-radius:4px;"><i class="fa-brands fa-google" style="margin-right:2px;"></i>Google</span>`;
                    html += '</div>';
                    html += `<div style="font-size:0.75rem;color:#aaa;"><i class="fa-regular fa-clock" style="margin-right:3px;"></i>${timeStr}</div>`;
                    if (ev.location) html += `<div style="font-size:0.75rem;color:#777;margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"><i class="fa-solid fa-location-dot" style="margin-right:3px;"></i>${dialerEsc(ev.location).substring(0, 60)}</div>`;
                    if (ev.description) html += `<div style="font-size:0.75rem;color:#777;margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"><i class="fa-regular fa-note-sticky" style="margin-right:3px;"></i>${dialerEsc(ev.description).substring(0, 60)}</div>`;
                    html += '</div>';
                });
            }

            // ── Available Slots for booking ──
            if (slots.length) {
                html += '<div style="font-size:0.75rem;color:#FF3B30;font-weight:600;text-transform:uppercase;letter-spacing:0.3px;padding:8px 0 4px;"><i class="fa-regular fa-clock" style="margin-right:4px;"></i>Available Times</div>';
                const parsed = slots.map(iso => {
                    const dt = new Date(iso);
                    return { iso, dt, hour: dt.getHours(), min: dt.getMinutes() };
                }).sort((a,b) => a.dt - b.dt);

                const morning = parsed.filter(s => s.hour < 12);
                const afternoon = parsed.filter(s => s.hour >= 12 && s.hour < 17);
                const evening = parsed.filter(s => s.hour >= 17);

                function renderSlotGroup(label, items) {
                    if (!items.length) return '';
                    let h = `<div style="font-size:0.75rem;color:#555;font-weight:600;text-transform:uppercase;padding:4px 0 3px;">${label}</div>`;
                    h += '<div style="display:flex;flex-wrap:wrap;gap:5px;margin-bottom:6px;">';
                    items.forEach(s => {
                        const timeStr = _calFormatTime(s.dt);
                        h += `<button onclick="calendarPickSlot('${s.iso}')" style="padding:7px 12px;border-radius:8px;border:1px solid rgba(255,59,48,0.2);background:rgba(255,59,48,0.06);color:#fff;font-size:0.75rem;font-weight:600;cursor:pointer;transition:all 0.15s;" onmouseover="this.style.background='rgba(255,59,48,0.2)'" onmouseout="this.style.background='rgba(255,59,48,0.06)'">${timeStr}</button>`;
                    });
                    h += '</div>';
                    return h;
                }

                html += renderSlotGroup('Morning', morning);
                html += renderSlotGroup('Afternoon', afternoon);
                html += renderSlotGroup('Evening', evening);
            }

            container.innerHTML = html;
        }

        function calendarOpenEvent(eventId) {
            const ev = _calAllEvents.find(e => e.id === eventId);
            if (!ev) return;

            // Google Calendar events are read-only — don't show edit overlay
            if (ev.source === 'google') return;

            _calEditingEvent = ev;

            const startDt = new Date(ev.startTime || ev.start);
            const endDt = ev.endTime ? new Date(ev.endTime) : null;
            const dayNames = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
            const monthNames = ['January','February','March','April','May','June','July','August','September','October','November','December'];
            const dateStr = `${dayNames[startDt.getDay()]}, ${monthNames[startDt.getMonth()]} ${startDt.getDate()}, ${startDt.getFullYear()}`;
            const timeStr = _calFormatTime(startDt) + (endDt ? ' – ' + _calFormatTime(endDt) : '');

            // Populate detail view
            const overlay = document.getElementById('iosCalConfirm');
            document.getElementById('iosCalConfirmContent').style.display = 'none';
            document.getElementById('iosCalSuccessContent').style.display = 'none';
            document.getElementById('iosCalErrorContent').style.display = 'none';
            document.getElementById('iosCalEventDetail').style.display = 'block';

            document.getElementById('iosCalEvTitle').textContent = ev.title || 'Appointment';
            document.getElementById('iosCalEvDate').textContent = dateStr;
            document.getElementById('iosCalEvTime').textContent = timeStr;

            const statusSel = document.getElementById('iosCalEvStatus');
            if (statusSel) statusSel.value = ev.status || 'new';

            const notesInput = document.getElementById('iosCalEvNotes');
            if (notesInput) notesInput.value = ev.notes || '';

            overlay.style.display = 'flex';
            overlay.style.animation = 'iosAppOpen 0.25s cubic-bezier(0.2,0.9,0.3,1)';
        }

        async function calendarSaveEvent() {
            if (!_calEditingEvent) return;
            const btn = document.getElementById('iosCalEvSaveBtn');
            if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-1"></i> Saving...'; }

            const status = document.getElementById('iosCalEvStatus').value;
            const notes = document.getElementById('iosCalEvNotes').value;

            try {
                const r = await fetch(`/api/calendar/events/${_calEditingEvent.id}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ appointmentStatus: status, notes: notes })
                });
                const d = await r.json();
                if (r.ok && d.success) {
                    // Update local cache
                    _calEditingEvent.status = status;
                    _calEditingEvent.notes = notes;
                    calendarDismissConfirm();
                    calendarRenderMonth();
                    if (typeof _showDashToast === 'function') _showDashToast(true, 'Event updated');
                } else {
                    if (typeof _showDashToast === 'function') _showDashToast(false, d.error || 'Failed to update');
                }
            } catch(e) {
                if (typeof _showDashToast === 'function') _showDashToast(false, 'Network error');
            }
            if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-check me-1"></i> Save Changes'; }
        }

        async function calendarDeleteEvent() {
            if (!_calEditingEvent) return;
            if (!confirm('Delete this appointment? This cannot be undone.')) return;

            const btn = document.getElementById('iosCalEvDeleteBtn');
            if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>'; }

            try {
                const r = await fetch(`/api/calendar/events/${_calEditingEvent.id}`, { method: 'DELETE' });
                const d = await r.json();
                if (r.ok && d.success) {
                    // Remove from local cache
                    _calAllEvents = _calAllEvents.filter(e => e.id !== _calEditingEvent.id);
                    Object.keys(_calEventData).forEach(k => {
                        _calEventData[k] = _calEventData[k].filter(e => e.id !== _calEditingEvent.id);
                    });
                    _calEditingEvent = null;
                    calendarDismissConfirm();
                    calendarRenderMonth();
                    if (typeof _showDashToast === 'function') _showDashToast(true, 'Event deleted');
                } else {
                    if (typeof _showDashToast === 'function') _showDashToast(false, d.error || 'Failed to delete');
                }
            } catch(e) {
                if (typeof _showDashToast === 'function') _showDashToast(false, 'Network error');
            }
            if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-trash"></i>'; }
        }

        function calendarPickSlot(isoTime) {
            _calSelectedSlot = isoTime;
            const dt = new Date(isoTime);
            const dayNames = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
            const monthNames = ['January','February','March','April','May','June','July','August','September','October','November','December'];

            const timeStr = _calFormatTime(dt);
            const dateStr = `${dayNames[dt.getDay()]}, ${monthNames[dt.getMonth()]} ${dt.getDate()}, ${dt.getFullYear()}`;

            const name = dialerActiveContact ? (dialerActiveContact.name || dialerActiveContact.firstName || 'Contact') : 'Contact';

            document.getElementById('iosCalConfirmName').textContent = name;
            document.getElementById('iosCalConfirmDate').textContent = dateStr;
            document.getElementById('iosCalConfirmTime').textContent = timeStr;

            document.getElementById('iosCalConfirmContent').style.display = 'block';
            document.getElementById('iosCalSuccessContent').style.display = 'none';
            document.getElementById('iosCalErrorContent').style.display = 'none';
            document.getElementById('iosCalEventDetail').style.display = 'none';
            const overlay = document.getElementById('iosCalConfirm');
            overlay.style.display = 'flex';
            overlay.style.animation = 'iosAppOpen 0.25s cubic-bezier(0.2,0.9,0.3,1)';
        }

        async function calendarConfirmBooking() {
            if (!_calSelectedSlot || !dialerActiveContact) return;
            const btn = document.getElementById('iosCalConfirmBtn');
            if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-1"></i> Booking...'; }

            try {
                const r = await fetch('/api/calendar/book', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        contact_id: dialerActiveContact.id,
                        slot_time: _calSelectedSlot,
                        calendar_id: _calActiveCalId,
                        first_name: dialerActiveContact.firstName || dialerActiveContact.name || 'Lead',
                    })
                });
                const d = await r.json();
                if (r.ok && d.success) {
                    document.getElementById('iosCalConfirmContent').style.display = 'none';
                    document.getElementById('iosCalSuccessContent').style.display = 'block';
                    document.getElementById('iosCalSuccessMsg').textContent = d.message || 'Appointment confirmed';
                    calendarFetchMonthData();
                } else {
                    document.getElementById('iosCalConfirmContent').style.display = 'none';
                    document.getElementById('iosCalErrorContent').style.display = 'block';
                    document.getElementById('iosCalErrorMsg').textContent = d.error || 'Something went wrong';
                }
            } catch(e) {
                document.getElementById('iosCalConfirmContent').style.display = 'none';
                document.getElementById('iosCalErrorContent').style.display = 'block';
                document.getElementById('iosCalErrorMsg').textContent = 'Network error. Please try again.';
            }
            if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-check me-1"></i> Book Appointment'; }
        }

        function calendarDismissConfirm() {
            const overlay = document.getElementById('iosCalConfirm');
            if (overlay) overlay.style.display = 'none';
            _calEditingEvent = null;
        }

        function calendarShowCreateForm() {
            const overlay = document.getElementById('iosCalConfirm');
            // Hide all other states
            document.getElementById('iosCalConfirmContent').style.display = 'none';
            document.getElementById('iosCalSuccessContent').style.display = 'none';
            document.getElementById('iosCalErrorContent').style.display = 'none';
            document.getElementById('iosCalEventDetail').style.display = 'none';
            document.getElementById('iosCalCreateForm').style.display = 'block';

            // Pre-fill date
            const dateInput = document.getElementById('iosCalCreateDate');
            if (dateInput) {
                dateInput.value = _calSelectedDate || new Date().toLocaleDateString('en-CA');
            }

            // Pre-fill time to next hour
            const now = new Date();
            const nextHour = new Date(now);
            nextHour.setHours(nextHour.getHours() + 1, 0, 0, 0);
            const endHour = new Date(nextHour);
            endHour.setMinutes(endHour.getMinutes() + 30);

            const startInput = document.getElementById('iosCalCreateStart');
            const endInput = document.getElementById('iosCalCreateEnd');
            if (startInput) startInput.value = `${String(nextHour.getHours()).padStart(2,'0')}:${String(nextHour.getMinutes()).padStart(2,'0')}`;
            if (endInput) endInput.value = `${String(endHour.getHours()).padStart(2,'0')}:${String(endHour.getMinutes()).padStart(2,'0')}`;

            // Pre-fill contact
            const contactDiv = document.getElementById('iosCalCreateContact');
            if (contactDiv) {
                if (dialerActiveContact) {
                    const name = dialerActiveContact.name || `${dialerActiveContact.firstName || ''} ${dialerActiveContact.lastName || ''}`.trim() || 'Contact';
                    contactDiv.innerHTML = `<i class="fa-solid fa-user" style="margin-right:6px;color:#FF3B30;"></i>${dialerEsc(name)}`;
                } else {
                    contactDiv.innerHTML = '<span style="color:#666;">No contact selected</span>';
                }
            }

            // Default title based on contact
            const titleInput = document.getElementById('iosCalCreateTitle');
            if (titleInput) {
                if (dialerActiveContact) {
                    const firstName = dialerActiveContact.firstName || dialerActiveContact.name || 'Lead';
                    titleInput.value = `Appointment with ${firstName}`;
                } else {
                    titleInput.value = '';
                }
            }
            const notesInput = document.getElementById('iosCalCreateNotes');
            if (notesInput) notesInput.value = '';

            overlay.style.display = 'flex';
            overlay.style.animation = 'iosAppOpen 0.25s cubic-bezier(0.2,0.9,0.3,1)';
        }

        async function calendarCreateEvent() {
            const title = (document.getElementById('iosCalCreateTitle').value || '').trim();
            const date = document.getElementById('iosCalCreateDate').value;
            const startTime = document.getElementById('iosCalCreateStart').value;
            const endTime = document.getElementById('iosCalCreateEnd').value;
            const notes = (document.getElementById('iosCalCreateNotes').value || '').trim();

            if (!title) { if (typeof _showDashToast === 'function') _showDashToast(false, 'Title is required'); return; }
            if (!date || !startTime || !endTime) { if (typeof _showDashToast === 'function') _showDashToast(false, 'Date and times are required'); return; }

            const btn = document.getElementById('iosCalCreateBtn');
            if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-1"></i> Creating...'; }

            const payload = {
                title: title,
                date: date,
                start_time: startTime,
                end_time: endTime,
                calendar_id: _calActiveCalId,
                notes: notes,
            };
            if (dialerActiveContact) {
                payload.contact_id = dialerActiveContact.id;
            }

            try {
                const r = await fetch('/api/calendar/create-event', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                });
                const d = await r.json();
                if (r.ok && d.success) {
                    document.getElementById('iosCalCreateForm').style.display = 'none';
                    document.getElementById('iosCalSuccessContent').style.display = 'block';
                    document.getElementById('iosCalSuccessMsg').textContent = d.message || 'Event created in your CRM';
                    calendarFetchMonthData();
                } else {
                    if (typeof _showDashToast === 'function') _showDashToast(false, d.error || 'Failed to create event');
                }
            } catch(e) {
                if (typeof _showDashToast === 'function') _showDashToast(false, 'Network error');
            }
            if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-plus me-1"></i> Create Event'; }
        }

        // Schedule button handler — opens calendar app with current contact
        function dialerScheduleActiveContact() {
            if (!dialerActiveContact) return;
            iosOpenApp('calendar');
        }

        // ── Add to Workflow ──────────────────────────────────────────────────
        function dialerAddToWorkflow() {
            if (!dialerActiveContact) return;
            const c = dialerActiveContact;
            // Fetch available workflows and show picker
            fetch('/api/workflows')
                .then(r => r.json())
                .then(data => {
                    const workflows = (data.workflows || []).filter(w => w.status === 'active');
                    if (!workflows.length) {
                        if (typeof _showDashToast === 'function') _showDashToast(false, 'No active workflows. Create one in the Workflows tab.');
                        return;
                    }
                    _showWorkflowPicker(c, workflows);
                })
                .catch(() => { if (typeof _showDashToast === 'function') _showDashToast(false, 'Could not load workflows'); });
        }

        function _showWorkflowPicker(contact, workflows) {
            // Remove existing picker if any
            const existing = document.getElementById('wfPickerOverlay');
            if (existing) existing.remove();

            const overlay = document.createElement('div');
            overlay.id = 'wfPickerOverlay';
            overlay.style.cssText = 'position:fixed;inset:0;z-index:99999;background:rgba(0,0,0,0.5);display:flex;align-items:center;justify-content:center;';
            overlay.onclick = function(e) { if (e.target === overlay) overlay.remove(); };

            let html = '<div style="background:rgba(16,16,24,0.98);border:1px solid rgba(255,255,255,0.1);border-radius:14px;padding:20px;max-width:340px;width:90%;">';
            html += '<h4 style="color:#fff;font-size:0.95rem;margin:0 0 4px;">Add to Workflow</h4>';
            html += '<p style="color:#888;font-size:0.8rem;margin:0 0 14px;">Enroll <strong style="color:#ccc;">' + (contact.name || 'Contact') + '</strong> into a workflow:</p>';
            workflows.forEach(function(wf) {
                html += '<button class="wf-pick-btn" data-wf-id="' + wf.id + '" data-wf-name="' + (wf.name || '') + '" style="display:block;width:100%;text-align:left;padding:10px 14px;margin-bottom:6px;border-radius:8px;border:1px solid rgba(139,92,246,0.2);background:rgba(139,92,246,0.06);color:#a78bfa;font-size:0.82rem;font-weight:600;cursor:pointer;">';
                html += '<i class="fa-solid fa-diagram-project" style="margin-right:6px;"></i>' + (wf.name || 'Unnamed');
                html += '</button>';
            });
            html += '<button onclick="this.closest(\'#wfPickerOverlay\').remove()" style="display:block;width:100%;margin-top:8px;padding:8px;border-radius:8px;border:1px solid rgba(255,255,255,0.08);background:none;color:#888;font-size:0.8rem;cursor:pointer;">Cancel</button>';
            html += '</div>';
            overlay.innerHTML = html;

            // Wire up workflow buttons
            overlay.querySelectorAll('.wf-pick-btn').forEach(function(btn) {
                btn.onclick = function() {
                    const wfId = this.dataset.wfId;
                    const wfName = this.dataset.wfName;
                    overlay.remove();
                    fetch('/api/workflows/' + wfId + '/test', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ contact_id: contact.id }),
                    })
                    .then(r => r.json())
                    .then(d => {
                        if (d.error) {
                            if (typeof _showDashToast === 'function') _showDashToast(false, d.error);
                        } else {
                            if (typeof _showDashToast === 'function') _showDashToast(true, (contact.name || 'Contact') + ' enrolled in ' + wfName);
                        }
                    })
                    .catch(() => {
                        if (typeof _showDashToast === 'function') _showDashToast(false, 'Failed to enroll contact');
                    });
                };
            });

            document.body.appendChild(overlay);
        }

        // ═══════════════════════════════════════════════
        //  VOICEMAIL APP — iPhone-style voicemail inbox
        // ═══════════════════════════════════════════════
        let _vmData = [];
        let _vmPlaying = null;

        function vmRefresh() {
            const icon = document.getElementById('vmRefreshIcon');
            if (icon) icon.classList.add('fa-spin');
            vmLoad().finally(() => {
                if (icon) icon.classList.remove('fa-spin');
            });
        }

        async function vmLoad() {
            const list = document.getElementById('vmList');
            if (!list) return;
            list.innerHTML = '<div style="text-align:center;padding:40px;"><i class="fa-solid fa-spinner fa-spin" style="color:#FF9500;font-size:1.2rem;"></i></div>';
            try {
                const r = await fetch('/voice/voicemails?limit=100');
                if (!r.ok) {
                    list.innerHTML = '<div style="text-align:center;padding:40px;color:#888;font-size:.88rem;">Failed to load voicemails</div>';
                    return;
                }
                const d = await r.json();
                _vmData = d.voicemails || [];

                // Update badge
                const badge = document.getElementById('iosBadgeVoicemail');
                if (badge) {
                    const unread = d.unread || 0;
                    if (unread > 0) { badge.textContent = unread; badge.style.display = 'flex'; }
                    else { badge.style.display = 'none'; }
                }

                if (!_vmData.length) {
                    list.innerHTML = '<div class="ios-empty-state" style="padding-top:60px;">' +
                        '<div class="ios-empty-icon" style="background:rgba(255,149,0,0.08);border-color:rgba(255,149,0,0.15);">' +
                        '<i class="fa-solid fa-voicemail" style="color:#FF9500;"></i></div>' +
                        '<div class="ios-empty-title">No Voicemail</div>' +
                        '<div class="ios-empty-sub">Missed calls with messages will appear here</div></div>';
                    return;
                }

                list.innerHTML = _vmData.map((vm, idx) => _vmRow(vm, idx)).join('');
            } catch(e) {
                console.error('[Voicemail] Load error:', e);
                list.innerHTML = '<div style="text-align:center;padding:40px;color:#ef4444;font-size:.88rem;">Error loading voicemails</div>';
            }
        }

        function _vmRow(vm, idx) {
            const name = vm.contact_name || vm.phone || 'Unknown';
            const dt = vm.created_at ? new Date(vm.created_at).toLocaleString([], {month:'short', day:'numeric', hour:'numeric', minute:'2-digit'}) : '';
            const dur = vm.duration ? Math.floor(vm.duration / 60) + ':' + String(vm.duration % 60).padStart(2, '0') : '0:00';
            const isNew = vm.is_new;
            const preview = vm.transcript_preview || '';
            const nameWt = isNew ? '700' : '500';
            const nameClr = isNew ? '#fff' : '#aaa';
            const dotClr = isNew ? '#FF9500' : 'transparent';

            let html = '<div class="call-history-row" onclick="vmToggleExpand(' + idx + ')">';
            // Line 1: dot · name · date
            html += '<div style="display:flex;align-items:center;gap:6px;min-width:0;">';
            html += '<div class="call-history-dot" style="background:' + dotClr + ';flex-shrink:0;"></div>';
            html += '<span class="chr-name" style="font-weight:' + nameWt + ';">' + dialerEsc(name) + '</span>';
            html += '<span class="chr-date">' + dt + '</span>';
            html += '</div>';
            // Line 2: duration · play/dl/transcribe · call back
            html += '<div style="display:flex;align-items:center;gap:6px;margin-top:3px;padding-left:16px;">';
            html += '<span class="chr-dur">' + dur + '</span>';
            if (vm.recording_url) {
                html += '<button onclick="event.stopPropagation();vmPlay(' + idx + ')" id="vmPlayBtn' + idx + '" class="chr-action-btn chr-btn-play" title="Play"><i class="fa-solid fa-play"></i></button>';
                html += '<a href="' + dialerEsc(vm.recording_url) + '?dl=1" download class="chr-action-btn chr-btn-download" title="Download" onclick="event.stopPropagation();"><i class="fa-solid fa-download"></i></a>';
            }
            if (preview) {
                html += '<button onclick=\'event.stopPropagation();showTranscript([{role:"call_recording",text:"' + dialerEsc(preview).replace(/"/g,'&quot;') + '"}])\' class="chr-action-btn chr-btn-transcript" title="Transcript"><i class="fa-solid fa-file-lines"></i></button>';
            } else if (vm.recording_url && vm.call_sid) {
                html += '<button onclick="event.stopPropagation();vmTranscribe(' + idx + ')" class="chr-action-btn chr-btn-transcribe" title="Transcribe"><i class="fa-solid fa-wand-magic-sparkles"></i></button>';
            }
            if (vm.phone) {
                html += '<button onclick="event.stopPropagation();vmCallback(' + idx + ')" class="chr-action-btn chr-btn-callback" title="Call Back"><i class="fa-solid fa-phone"></i></button>';
            }
            html += '</div>';
            // Expanded area (hidden by default)
            html += '<div id="vmExpand' + idx + '" class="chr-expand" style="display:none;margin-top:8px;padding:8px 10px 4px 16px;">';
            if (preview) {
                html += '<div class="chr-preview">' + dialerEsc(preview) + '</div>';
            } else {
                html += '<div class="chr-hint">Tap to transcribe</div>';
            }
            html += '</div>';
            html += '</div>';
            return html;
        }

        function vmToggleExpand(idx) {
            const el = document.getElementById('vmExpand' + idx);
            if (!el) return;
            el.style.display = el.style.display === 'none' ? 'block' : 'none';
        }

        function vmPlay(idx) {
            const vm = _vmData[idx];
            if (!vm || !vm.recording_url) return;
            const audio = document.getElementById('vmAudioPlayer');
            const bar = document.getElementById('vmPlayerBar');
            const content = document.getElementById('vmPlayerContent');
            if (!audio || !bar) return;

            // If same voicemail playing, toggle pause/play
            if (_vmPlaying === idx && !audio.paused) {
                audio.pause();
                const btn = document.getElementById('vmPlayBtn' + idx);
                if (btn) btn.innerHTML = '<i class="fa-solid fa-play me-1"></i>Play';
                return;
            }

            // Reset previous voicemail's button if switching
            if (_vmPlaying !== null && _vmPlaying !== idx) {
                const prevBtn = document.getElementById('vmPlayBtn' + _vmPlaying);
                if (prevBtn) prevBtn.innerHTML = '<i class="fa-solid fa-play me-1"></i>Play';
            }
            audio.src = vm.recording_url;
            audio.play().catch(e => console.warn('[VM] Play error:', e));
            _vmPlaying = idx;

            const name = vm.contact_name || vm.phone || 'Unknown';
            content.innerHTML = '<div style="display:flex;align-items:center;gap:10px;">' +
                '<button onclick="vmStopPlay()" style="background:none;border:none;color:#FF9500;cursor:pointer;font-size:1rem;"><i class="fa-solid fa-stop"></i></button>' +
                '<div style="flex:1;min-width:0;">' +
                '<div style="font-weight:600;font-size:.82rem;color:#fff;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">' + dialerEsc(name) + '</div>' +
                '<div id="vmPlayerTime" style="font-size:.75rem;color:#888;">Playing...</div>' +
                '</div></div>';
            bar.style.display = 'block';

            const btn = document.getElementById('vmPlayBtn' + idx);
            if (btn) btn.innerHTML = '<i class="fa-solid fa-pause me-1"></i>Pause';

            audio.onended = function() {
                bar.style.display = 'none';
                _vmPlaying = null;
                if (btn) btn.innerHTML = '<i class="fa-solid fa-play me-1"></i>Play';
            };
            audio.ontimeupdate = function() {
                const el = document.getElementById('vmPlayerTime');
                if (el) {
                    const cur = Math.floor(audio.currentTime);
                    const total = Math.floor(audio.duration || 0);
                    el.textContent = Math.floor(cur/60) + ':' + String(cur%60).padStart(2,'0') + ' / ' + Math.floor(total/60) + ':' + String(total%60).padStart(2,'0');
                }
            };
        }

        function vmStopPlay() {
            const audio = document.getElementById('vmAudioPlayer');
            const bar = document.getElementById('vmPlayerBar');
            if (audio) { audio.pause(); audio.src = ''; }
            if (bar) bar.style.display = 'none';
            if (_vmPlaying !== null) {
                const btn = document.getElementById('vmPlayBtn' + _vmPlaying);
                if (btn) btn.innerHTML = '<i class="fa-solid fa-play me-1"></i>Play';
            }
            _vmPlaying = null;
        }

        async function vmTranscribe(idx) {
            const vm = _vmData[idx];
            if (!vm || !vm.call_sid || !vm.recording_url) return;
            const txEl = document.getElementById('vmTranscript' + idx);
            if (txEl) txEl.innerHTML = '<div style="text-align:center;padding:8px;"><i class="fa-solid fa-spinner fa-spin" style="color:#ffb400;"></i> Transcribing...</div>';
            try {
                const r = await fetch('/voice/transcribe-recording', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ call_sid: vm.call_sid, recording_url: vm.recording_url })
                });
                const d = await r.json();
                if (r.ok && d.transcript && d.transcript.length) {
                    const text = d.transcript[0].text || '';
                    vm.transcript_preview = text.substring(0, 120);
                    vm.is_new = false;
                    if (txEl) txEl.innerHTML = '<div style="color:#ccc;font-size:.82rem;line-height:1.5;background:rgba(255,255,255,0.02);padding:8px 10px;border-radius:8px;">' + dialerEsc(text) + '</div>';
                } else {
                    if (txEl) txEl.innerHTML = '<div style="color:#ef4444;font-size:.82rem;">Transcription failed: ' + dialerEsc(d.error || 'Unknown error') + '</div>';
                }
            } catch(e) {
                console.error('[VM] Transcribe error:', e);
                if (txEl) txEl.innerHTML = '<div style="color:#ef4444;font-size:.82rem;">Network error</div>';
            }
        }

        function vmCallback(idx) {
            const vm = _vmData[idx];
            if (!vm || !vm.phone) return;
            iosGoHome();
            // Select the contact and initiate call
            if (vm.contact_id) {
                dialerSelectContact(vm.contact_id);
            }
            dialerStartCall(vm.phone, vm.contact_name || vm.phone, vm.contact_id || '', vm.contact_name || vm.phone);
        }


        // ── Production-grade fetch wrapper with retry + timeout ──
        async function _fetchRetry(url, opts = {}, { retries = 2, timeout = 15000, label = '' } = {}) {
            for (let attempt = 0; attempt <= retries; attempt++) {
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), timeout);
                try {
                    const r = await fetch(url, { ...opts, signal: controller.signal });
                    clearTimeout(timer);
                    return r;
                } catch(e) {
                    clearTimeout(timer);
                    if (attempt < retries) {
                        const delay = Math.min(1000 * Math.pow(2, attempt), 4000);
                        console.warn(`[Retry] ${label || url} attempt ${attempt + 1} failed, retrying in ${delay}ms:`, e.message);
                        await new Promise(r => setTimeout(r, delay));
                    } else {
                        console.error(`[Retry] ${label || url} all ${retries + 1} attempts failed:`, e.message);
                        throw e;
                    }
                }
            }
        }

        function dialerEsc(s) { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }
        function formatPhone(p) {
            if (!p) return '';
            const d = p.replace(/\D/g, '');
            if (d.length === 11 && d[0] === '1') return '(' + d.substr(1,3) + ') ' + d.substr(4,3) + '-' + d.substr(7);
            if (d.length === 10) return '(' + d.substr(0,3) + ') ' + d.substr(3,3) + '-' + d.substr(6);
            return p;
        }

        // ── Pipeline filter ──
        async function dialerLoadPipelines() {
            const sel = document.getElementById('dialerPipelineFilter');
            const manualWrap = document.getElementById('dialerPipelineManual');
            const stageSel = document.getElementById('dialerStageFilter');
            if (!sel || !stageSel) return;
            try {
                console.log('[Dialer] Fetching pipelines...');
                const r = await fetch('/voice/pipelines');
                if (!r.ok) {
                    console.warn('[Dialer] Pipelines fetch failed:', r.status, r.statusText);
                    sel.innerHTML = '<option value="">All Contacts</option>';
                    return;
                }
                const d = await r.json();
                dialerPipelines = d.pipelines || [];
                console.log('[Dialer] Pipelines loaded:', dialerPipelines.length, dialerPipelines.map(p => p.name));
                if (d.scope_missing) {
                    // opportunities.readonly not in current token — reconnect OAuth to fix
                    sel.style.display = 'none';
                    manualWrap.style.display = 'block';
                    stageSel.disabled = true;
                    stageSel.innerHTML = '<option value="">All Stages (reconnect CRM to enable)</option>';
                } else if (dialerPipelines.length === 0) {
                    sel.style.display = 'block';
                    manualWrap.style.display = 'none';
                    sel.innerHTML = '<option value="">All Contacts</option><option value="" disabled style="color:#555;">No pipelines found in LeadConnector</option>';
                    stageSel.disabled = true;
                    stageSel.innerHTML = '<option value="">All Stages (no pipelines)</option>';
                } else {
                    sel.style.display = 'block';
                    manualWrap.style.display = 'none';
                    sel.innerHTML = '<option value="">All Contacts</option>' +
                        dialerPipelines.map(p => '<option value="' + p.id + '">' + dialerEsc(p.name) + '</option>').join('');
                    stageSel.disabled = true;
                    stageSel.innerHTML = '<option value="">All Stages (select a pipeline)</option>';
                }
            } catch(e) {
                console.error('[Dialer] Pipeline load error:', e);
                sel.innerHTML = '<option value="">All Contacts</option>';
            }
        }

        let dialerManualPipelineTimer = null;
        function dialerOnManualPipelineInput() {
            clearTimeout(dialerManualPipelineTimer);
            dialerManualPipelineTimer = setTimeout(() => dialerFetchContacts(), 600);
        }

        function dialerOnPipelineChange() {
            const pipelineId = document.getElementById('dialerPipelineFilter').value;
            const stageSel = document.getElementById('dialerStageFilter');
            if (pipelineId) {
                const p = dialerPipelines.find(x => x.id === pipelineId);
                if (p && p.stages.length) {
                    stageSel.innerHTML = '<option value="">All Stages</option>' +
                        p.stages.map(s => '<option value="' + s.id + '">' + dialerEsc(s.name) + '</option>').join('');
                    stageSel.disabled = false;
                    stageSel.style.display = '';
                } else {
                    stageSel.innerHTML = '<option value="">No stages in this pipeline</option>';
                    stageSel.disabled = true;
                    stageSel.style.display = '';
                }
            } else {
                stageSel.innerHTML = '<option value="">All Stages (select a pipeline)</option>';
                stageSel.disabled = true;
                stageSel.style.display = 'none';
            }
            dialerFetchContacts();
        }

        // ── Search: filter locally when contacts are loaded, else fetch ──
        let _dialerActiveTagFilter = null; // When set, filter by exact tag match

        function dialerDebounceSearch() {
            clearTimeout(dialerSearchTimer);
            dialerSearchTimer = setTimeout(() => {
                _dialerActiveTagFilter = null; // Clear tag filter on new typing
                if (_dialerAllContacts.length > 0) {
                    dialerFilterLocal();
                    dialerShowTagSuggestions();
                } else {
                    dialerHideTagSuggestions();
                    dialerFetchContacts();
                }
            }, 250);
        }

        function dialerFilterLocal() {
            const q = (document.getElementById('dialerSearch').value || '').trim().toLowerCase();
            if (_dialerActiveTagFilter) {
                // Exact tag filter mode
                const tag = _dialerActiveTagFilter.toLowerCase();
                dialerContacts = _dialerAllContacts.filter(c =>
                    (c.tags || []).some(t => t.toLowerCase() === tag)
                );
            } else if (!q) {
                dialerContacts = [..._dialerAllContacts];
            } else {
                dialerContacts = _dialerAllContacts.filter(c =>
                    (c.name || '').toLowerCase().includes(q) ||
                    (c.phone || '').includes(q) ||
                    (c.email || '').toLowerCase().includes(q) ||
                    (c.tags || []).some(t => t.toLowerCase().includes(q))
                );
            }
            dialerRenderContacts();
            dialerUpdateSelectionUI();
        }

        // ── Tag autocomplete suggestions ──
        function _dialerCollectAllTags() {
            const tagMap = {};
            for (const c of _dialerAllContacts) {
                for (const t of (c.tags || [])) {
                    const key = t.toLowerCase();
                    if (!tagMap[key]) tagMap[key] = { name: t, count: 0 };
                    tagMap[key].count++;
                }
            }
            return Object.values(tagMap).sort((a, b) => b.count - a.count);
        }

        function dialerShowTagSuggestions() {
            const q = (document.getElementById('dialerSearch').value || '').trim().toLowerCase();
            let dropdown = document.getElementById('dialerTagSuggestions');
            if (!q || q.length < 1) { dialerHideTagSuggestions(); return; }
            const allTags = _dialerCollectAllTags();
            const matches = allTags.filter(t => t.name.toLowerCase().includes(q)).slice(0, 6);
            if (!matches.length) { dialerHideTagSuggestions(); return; }
            if (!dropdown) {
                dropdown = document.createElement('div');
                dropdown.id = 'dialerTagSuggestions';
                dropdown.style.cssText = 'position:absolute;left:0;right:0;top:100%;background:rgba(20,20,30,0.98);border:1px solid rgba(0,217,255,0.15);border-radius:0 0 8px 8px;z-index:100;max-height:200px;overflow-y:auto;box-shadow:0 4px 16px rgba(0,0,0,0.4);';
                const wrap = document.getElementById('dialerSearch').parentElement;
                wrap.appendChild(dropdown);
            }
            dropdown.innerHTML = matches.map(t =>
                `<div onclick="dialerSelectTag('${t.name.replace(/'/g, "\\'")}')" style="display:flex;align-items:center;gap:8px;padding:8px 12px;cursor:pointer;transition:background .12s;border-bottom:1px solid rgba(255,255,255,0.04);" onmouseenter="this.style.background='rgba(0,217,255,0.08)'" onmouseleave="this.style.background='transparent'"><i class="fa-solid fa-tag" style="color:#00d9ff;font-size:0.7rem;"></i><span style="color:#ccc;font-size:0.85rem;">${dialerEsc(t.name)}</span><span style="margin-left:auto;color:#555;font-size:0.75rem;">${t.count}</span></div>`
            ).join('');
            dropdown.style.display = 'block';
        }

        function dialerHideTagSuggestions() {
            const d = document.getElementById('dialerTagSuggestions');
            if (d) d.style.display = 'none';
        }

        function dialerSelectTag(tag) {
            _dialerActiveTagFilter = tag;
            const input = document.getElementById('dialerSearch');
            input.value = tag;
            dialerHideTagSuggestions();
            dialerFilterLocal();
        }

        // Hide tag suggestions when clicking outside
        document.addEventListener('click', function(e) {
            if (!e.target.closest('#dialerSearch') && !e.target.closest('#dialerTagSuggestions')) {
                dialerHideTagSuggestions();
            }
        });

        // ── Fetch ALL contacts (paginated + cached on backend) ──
        let _dialerCacheLoaded = false; // Track if we've loaded from cache on first open

        // ── Contact Management Modal ──
        function dlrOpenContactMgmt() {
            const m = document.getElementById('dlrContactMgmtModal');
            if (m) { m.style.display = 'flex'; }
        }
        function dlrCloseContactMgmt() {
            const m = document.getElementById('dlrContactMgmtModal');
            if (m) { m.style.display = 'none'; }
        }

        // ── Search bar toggle (magnifying glass icon) ──
        function dlrToggleSearch() {
            const row = document.getElementById('dlrSearchRow');
            const btn = document.getElementById('dlrSearchToggleBtn');
            if (!row) return;
            const visible = row.style.display !== 'none';
            row.style.display = visible ? 'none' : '';
            if (btn) btn.style.color = visible ? '' : 'var(--accent)';
            if (!visible) {
                const input = document.getElementById('dialerSearch');
                if (input) { input.value = ''; input.focus(); }
            } else {
                // Clear search when hiding
                const input = document.getElementById('dialerSearch');
                if (input && input.value) { input.value = ''; dialerDebounceSearch(); }
            }
        }

        // ── Call History app tab switch ──
        function iosCallsSetTab(tab) {
            const single = document.getElementById('dlrHistoryCalls');
            const campaigns = document.getElementById('dlrHistoryCampaigns');
            const tabSingle = document.getElementById('iosCallsTabSingle');
            const tabCampaigns = document.getElementById('iosCallsTabCampaigns');
            if (!single || !campaigns) return;
            if (tab === 'single') {
                single.style.display = '';
                campaigns.style.display = 'none';
                if (tabSingle) tabSingle.classList.add('active');
                if (tabCampaigns) tabCampaigns.classList.remove('active');
            } else {
                single.style.display = 'none';
                campaigns.style.display = '';
                if (tabSingle) tabSingle.classList.remove('active');
                if (tabCampaigns) tabCampaigns.classList.add('active');
                _campaignRenderHistory();
            }
        }

        async function dialerFetchContacts(forceRefresh) {
            // Reset intel column to hidden state when reloading contacts
            const _ic = document.getElementById('dlrColIntel');
            if (_ic) { _ic.classList.add('dlr-col-intel-hidden'); _ic.classList.remove('dlr-col-intel-visible'); }

            // Cancel any in-flight background refresh poll (prevents stale data overwriting)
            if (_dialerRefreshPoll) {
                clearInterval(_dialerRefreshPoll);
                _dialerRefreshPoll = null;
                const syncIcon = document.getElementById('dialerSyncIcon');
                if (syncIcon) syncIcon.classList.remove('fa-spin');
            }

            const myGen = ++_dialerFetchGen; // Tag this request

            const list = document.getElementById('dialerContactList');
            const btn = document.getElementById('dialerGetContactsBtn');
            const manualWrap = document.getElementById('dialerPipelineManual');
            const pipeline = (manualWrap && manualWrap.style.display !== 'none')
                ? (document.getElementById('dialerPipelineManualInput').value || '').trim()
                : document.getElementById('dialerPipelineFilter').value;
            const stage = document.getElementById('dialerStageFilter').value;

            if (btn) { btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-1"></i>Loading...'; btn.disabled = true; }

            // Only show full loading spinner if no contacts loaded yet
            if (!_dialerAllContacts.length) {
                list.innerHTML = '<div style="text-align:center;padding:30px;color:#555;"><i class="fa-solid fa-spinner fa-spin" style="color:#00d9ff;font-size:1.2rem;"></i><p style="margin-top:8px;font-size:.78rem;">Loading contacts...</p></div>';
            }

            try {
                let url = '/voice/contacts?';
                if (pipeline) url += 'pipeline=' + encodeURIComponent(pipeline) + '&';
                if (stage) url += 'stage=' + encodeURIComponent(stage) + '&';
                if (forceRefresh) url += 'refresh=1&';
                const r = await _fetchRetry(url, {}, { retries: 2, timeout: 30000, label: 'contacts' });
                const d = await r.json();

                // Discard if a newer fetch was started while we were waiting
                if (myGen !== _dialerFetchGen) return;

                if (!r.ok) {
                    if (!_dialerAllContacts.length) {
                        list.innerHTML = '<div style="text-align:center;padding:16px;color:#ef4444;font-size:.78rem;"><i class="fa-solid fa-triangle-exclamation me-1"></i>' + (d.error || 'Failed to load contacts') + '</div>';
                    }
                    return;
                }
                _dialerAllContacts = d.contacts || [];
                dialerContacts = [..._dialerAllContacts];
                dialerSelected.clear();
                // Apply any current search filter
                const q = (document.getElementById('dialerSearch').value || '').trim();
                if (q) dialerFilterLocal();
                else { dialerRenderContacts(); dialerUpdateSelectionUI(); }
                // Show local (dialer) counts immediately, then upgrade with GHL+WAVV in background
                dialerFetchCallCounts().then(() => dialerFetchMergedCounts());
                // InsuranceGrokBot: always load engagement + intelligence for Smart Filters
                igbFetchBulkEngagement();
                // One-time deep historical pull (auto-triggers on first use, then done forever)
                _deepSyncCheck();

                // Update cache status indicator
                _dialerUpdateCacheStatus(d);

                // If background refresh is happening, poll for completion
                if (d.refreshing) {
                    _dialerPollRefresh(pipeline, stage, myGen);
                }
            } catch(e) {
                if (myGen !== _dialerFetchGen) return; // Stale — ignore
                console.error('[Dialer] Contact fetch failed:', e);
                if (!_dialerAllContacts.length) {
                    list.innerHTML = '<div style="text-align:center;padding:16px;color:#ef4444;font-size:.78rem;"><i class="fa-solid fa-triangle-exclamation me-1"></i>Network error loading contacts — click Get Contacts to retry</div>';
                }
            } finally {
                if (btn) { btn.innerHTML = '<i class="fa-solid fa-download me-1"></i>Get Contacts'; btn.disabled = false; }
            }
        }

        // ── Cache status display ──
        function _dialerUpdateCacheStatus(data) {
            const el = document.getElementById('dialerCacheStatus');
            if (!el) return;
            if (data.cached && data.cached_at) {
                el.style.display = 'flex';
                let html = '<i class="fa-solid fa-database" style="color:#444;"></i>';
                html += '<span>' + (data.contacts || []).length + ' contacts</span>';
                if (data.refreshing) {
                    html += '<span style="color:#00d9ff;"><i class="fa-solid fa-rotate fa-spin me-1"></i>refreshing...</span>';
                }
                el.innerHTML = html;
            } else if (!data.cached) {
                el.style.display = 'flex';
                el.innerHTML = '<i class="fa-solid fa-cloud-arrow-down" style="color:#4ade80;"></i><span style="color:#4ade80;">Fresh from CRM</span>';
                setTimeout(() => { el.style.display = 'none'; }, 5000);
            } else {
                el.style.display = 'none';
            }
        }

        // ── Poll for background refresh completion ──
        function _dialerPollRefresh(pipeline, stage, gen) {
            // Cancel any prior poll
            if (_dialerRefreshPoll) clearInterval(_dialerRefreshPoll);

            const syncIcon = document.getElementById('dialerSyncIcon');
            if (syncIcon) syncIcon.classList.add('fa-spin');
            let attempts = 0;
            _dialerRefreshPoll = setInterval(async () => {
                // Abort if a newer fetch has started (user changed filters)
                if (gen !== _dialerFetchGen) {
                    clearInterval(_dialerRefreshPoll);
                    _dialerRefreshPoll = null;
                    if (syncIcon) syncIcon.classList.remove('fa-spin');
                    return;
                }
                attempts++;
                if (attempts > 20) { // 60 seconds max
                    clearInterval(_dialerRefreshPoll);
                    _dialerRefreshPoll = null;
                    if (syncIcon) syncIcon.classList.remove('fa-spin');
                    return;
                }
                try {
                    let url = '/voice/contacts?';
                    if (pipeline) url += 'pipeline=' + encodeURIComponent(pipeline) + '&';
                    if (stage) url += 'stage=' + encodeURIComponent(stage);
                    const r = await fetch(url);
                    const d = await r.json();
                    // Double-check generation is still current before writing results
                    if (gen !== _dialerFetchGen) {
                        clearInterval(_dialerRefreshPoll);
                        _dialerRefreshPoll = null;
                        if (syncIcon) syncIcon.classList.remove('fa-spin');
                        return;
                    }
                    if (r.ok && !d.refreshing) {
                        clearInterval(_dialerRefreshPoll);
                        _dialerRefreshPoll = null;
                        if (syncIcon) syncIcon.classList.remove('fa-spin');
                        // Update contacts with fresh data
                        _dialerAllContacts = d.contacts || [];
                        dialerContacts = [..._dialerAllContacts];
                        const q = (document.getElementById('dialerSearch').value || '').trim();
                        if (q) dialerFilterLocal();
                        else dialerRenderContacts();
                        _dialerUpdateCacheStatus(d);
                    }
                } catch(e) { /* ignore poll errors */ }
            }, 3000);
        }

        // ── Manual sync button ──
        async function dialerSyncContacts() {
            const syncIcon = document.getElementById('dialerSyncIcon');
            if (syncIcon) syncIcon.classList.add('fa-spin');
            try {
                await fetch('/voice/contacts/sync', { method: 'POST' });
                // Poll for completion using current generation
                const gen = ++_dialerFetchGen;
                _dialerPollRefresh(
                    document.getElementById('dialerPipelineFilter').value,
                    document.getElementById('dialerStageFilter').value,
                    gen
                );
            } catch(e) {
                if (syncIcon) syncIcon.classList.remove('fa-spin');
            }
        }

        // ── InsuranceGrokBot: engagement level (0-3 dots) — AI only, no rules ──
        // Uses AI engagement_level directly. No heuristic fallback.
        // If AI hasn't analyzed yet, returns -1 (pending state).
        function _igbEngageLevel(contactId, contactObj) {
            const eng = _igbEngagementCache[contactId];
            if (_igbIsOptedOut(eng, contactObj)) return 0;

            const intel = _igbIntelCache[contactId];
            if (intel) {
                // AI engagement_level (0-3) returned directly from Grok
                if (typeof intel.engagement_level === 'number') return intel.engagement_level;
                // Backward compat: derive from temperature if engagement_level missing
                if (intel.temperature === 'hot') return 3;
                if (intel.temperature === 'warm') return 2;
                if (intel.temperature === 'cool') return 1;
                if (intel.temperature === 'cold') return 0;
            }

            return -1; // Not analyzed yet — show neutral/pending dots
        }

        // ── InsuranceGrokBot Smart Filter: group contacts by AI intelligence + dispositions ──
        // Priority: Should Respond > Callback > Hot > Warm > Interested > Cool > Cold > Not Interested > DnC > Analyzing
        // Dispositions override AI temperature grouping when set (agent's manual classification takes priority).
        // Check if a contact has a bad/missing name (e.g. "None None", empty, null)
        function _igbIsBadName(c) {
            const name = (c.name || '').trim().toLowerCase();
            if (!name) return true;
            if (name === 'none none' || name === 'none' || name === 'null null' || name === 'null') return true;
            // Both first+last are empty/none
            const fn = (c.firstName || '').trim().toLowerCase();
            const ln = (c.lastName || '').trim().toLowerCase();
            if ((!fn || fn === 'none' || fn === 'null') && (!ln || ln === 'none' || ln === 'null')) return true;
            return false;
        }

        function _igbGroupContacts(contacts) {
            const respond = [], callbacks = [], hot = [], warm = [], interested = [], cool = [], cold = [], notInterested = [], underContract = [], dnc = [], unanalyzed = [], notShowing = [];
            const hasAI = Object.keys(_igbIntelCache).length > 0;

            contacts.forEach(c => {
                // Hide contacts with bad/missing names in "Not Showing" group
                if (_igbIsBadName(c)) { notShowing.push(c); return; }

                const eng = _igbEngagementCache[c.id];
                // DnD / opt-out contacts always go to DnC group
                if (_igbIsOptedOut(eng, c)) { dnc.push(c); return; }

                // ── Disposition override: agent's manual classification takes priority ──
                const disp = eng && eng.disposition;
                if (disp === 'callback') { callbacks.push(c); return; }
                if (disp === 'not_interested') { notInterested.push(c); return; }
                if (disp === 'interested') { interested.push(c); return; }

                const intel = _igbIntelCache[c.id];

                // ── AI detected existing client (sold policy, in service pipeline) ──
                // Always takes priority over temperature — clients under contract
                // are not active sales leads and should never appear in hot/warm/cool/cold.
                if (intel && intel.under_contract) { underContract.push(c); return; }

                if (intel && intel.temperature && intel.temperature !== 'neutral') {
                    // ── AI-powered classification ──
                    // "Should Respond" = AI says hot/warm AND lead's last message is unanswered
                    const shouldRespond = _igbShouldRespond(c.id, intel);
                    if (shouldRespond) { respond.push(c); return; }

                    switch (intel.temperature) {
                        case 'hot':  hot.push(c); break;
                        case 'warm': warm.push(c); break;
                        case 'cool': cool.push(c); break;
                        case 'cold': cold.push(c); break;
                        default:     cool.push(c); break;
                    }
                } else {
                    // No AI data yet — still loading or pending analysis
                    unanalyzed.push(c);
                }
            });

            const groups = [
                { key: 'respond', label: 'Should Respond', icon: 'fa-reply', color: '#ff3b30', contacts: respond },
                { key: 'callback', label: 'Callback', icon: 'fa-phone-volume', color: '#007AFF', contacts: callbacks },
                { key: 'hot', label: 'Hot Leads', icon: 'fa-fire', color: '#4ade80', contacts: hot },
                { key: 'warm', label: 'Warm Leads', icon: 'fa-temperature-half', color: '#ffa500', contacts: warm },
                { key: 'interested', label: 'Interested', icon: 'fa-thumbs-up', color: '#4ade80', contacts: interested },
                { key: 'cool', label: 'Cool', icon: 'fa-snowflake', color: '#5B7FFF', contacts: cool },
                { key: 'cold', label: 'Cold', icon: 'fa-icicles', color: '#888', contacts: cold },
                { key: 'not_interested', label: 'Not Interested', icon: 'fa-thumbs-down', color: '#ef4444', contacts: notInterested },
                { key: 'under_contract', label: 'Clients Under Contract', icon: 'fa-file-shield', color: '#10b981', contacts: underContract },
                { key: 'dnc', label: 'Do Not Contact', icon: 'fa-ban', color: '#ef4444', contacts: dnc },
            ];
            if (unanalyzed.length > 0) {
                groups.push({ key: 'unanalyzed', label: 'Analyzing...', icon: 'fa-spinner fa-spin', color: '#555', contacts: unanalyzed });
            }
            if (notShowing.length > 0) {
                groups.push({ key: 'not_showing', label: 'Not Showing', icon: 'fa-eye-slash', color: '#444', contacts: notShowing });
            }
            return groups;
        }

        // ── Should Respond: AI determines if the agent needs to act NOW ──
        // Uses AI should_respond field (reads conversation to decide),
        // NOT just timestamp comparison. The AI knows whether the lead
        // asked a question, expressed interest, or said "stop."
        function _igbShouldRespond(contactId, intel) {
            if (!intel) return false;
            // AI explicitly says should_respond — trust it
            if (typeof intel.should_respond === 'boolean') return intel.should_respond;
            // Backward compat: if AI didn't return should_respond field,
            // fall back to temperature + timestamp check
            if (intel.temperature !== 'hot' && intel.temperature !== 'warm') return false;
            const eng = _igbEngagementCache[contactId];
            if (!eng || !eng.messages) return false;
            const lastLead = eng.messages.last_lead_at;
            const lastBot = eng.messages.last_assistant_at;
            if (lastLead && (!lastBot || lastLead > lastBot)) return true;
            return false;
        }

        function igbToggleFilter(key) {
            _igbFilterCollapsed[key] = !_igbFilterCollapsed[key];
            dialerRenderContacts();
        }

        // ── Disposition color map for contact row highlighting ──
        const _dispColors = {
            not_interested: { border: '#ef4444', bg: 'rgba(239,68,68,0.05)', icon: 'fa-thumbs-down', label: 'Not Interested' },
            callback:       { border: '#007AFF', bg: 'rgba(0,122,255,0.05)', icon: 'fa-phone-arrow-down-left', label: 'Callback' },
            interested:     { border: '#4ade80', bg: 'rgba(74,222,128,0.05)', icon: 'fa-thumbs-up', label: 'Interested' },
        };

        // ── Render single contact row (shared by grouped + ungrouped) ──
        function _igbRenderContactRow(c) {
            const init = (c.firstName || c.name || '?')[0].toUpperCase();
            const sel = dialerSelected.has(c.id);
            const isActive = dialerActiveContact && dialerActiveContact.id === c.id;
            const inQ = dialerQueue.some(q => q.id === c.id);
            const callCount = _dialerCallCounts[c.id] || 0;
            const level = _igbEngageLevel(c.id, c);

            // Live status dot
            const isLive = typeof _active_calls !== 'undefined' && _active_calls && _active_calls[c.id];
            let liveDotCls = 'igb-live-dot idle';
            if (isLive) liveDotCls = 'igb-live-dot live-call';

            // Engagement dots HTML — level -1 = pending (all neutral), 0-3 = AI engagement
            const isPendingLevel = level < 0;
            const dotClass = isPendingLevel ? '' : (level >= 3 ? 'lit-hot' : (level >= 2 ? 'lit-warm' : (level >= 1 ? 'lit' : '')));
            const dots = '<span class="igb-engage-dots"' + (isPendingLevel ? ' title="AI analyzing..."' : '') + '>' +
                '<span class="igb-dot' + (!isPendingLevel && level >= 1 ? ' ' + dotClass : '') + '"></span>' +
                '<span class="igb-dot' + (!isPendingLevel && level >= 2 ? ' ' + dotClass : '') + '"></span>' +
                '<span class="igb-dot' + (!isPendingLevel && level >= 3 ? ' ' + dotClass : '') + '"></span>' +
            '</span>';

            // ── Disposition color-coding ──
            // Red = not_interested, Blue = callback, Green = interested
            const eng = _igbEngagementCache[c.id];
            const disp = eng && eng.disposition;
            const dc = _dispColors[disp];
            let rowStyle = sel ? 'background:rgba(0,217,255,0.04);' : '';
            if (dc && !isActive) {
                rowStyle += 'border-left:3px solid ' + dc.border + ';background:' + dc.bg + ';';
            }
            // Disposition mini-badge (shows inline next to phone number)
            let dispLabel = dc ? dc.label : '';
            // Show callback time if scheduled
            if (disp === 'callback' && eng && eng.callback_at) {
                try {
                    const cbDate = new Date(eng.callback_at);
                    const now = new Date();
                    const isPast = cbDate < now;
                    const timeStr = cbDate.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
                    const dayStr = cbDate.toLocaleDateString() === now.toLocaleDateString() ? 'Today' :
                                   cbDate.toLocaleDateString() === new Date(now.getTime() + 86400000).toLocaleDateString() ? 'Tomorrow' :
                                   cbDate.toLocaleDateString([], { month: 'short', day: 'numeric' });
                    dispLabel = (isPast ? 'Due: ' : '') + dayStr + ' ' + timeStr;
                } catch(e) {}
            }
            const dispBadge = dc ? '<span style="display:inline-flex;align-items:center;gap:2px;font-size:.75rem;color:' + dc.border + ';margin-left:6px;opacity:.85;"><i class="fa-solid ' + dc.icon + '" style="font-size:.75rem;"></i>' + dispLabel + '</span>' : '';

            const isLight = document.body.classList.contains('light-theme');
            const accentClr = isLight ? '#0078b8' : '#00d9ff';
            const avatarActiveBg = isLight ? 'rgba(0,120,184,0.15)' : 'rgba(0,217,255,0.15)';
            const avatarIdleBg = isLight ? 'rgba(0,120,184,0.08)' : 'rgba(0,217,255,0.06)';
            const avatarActiveBorder = isLight ? '#0078b8' : '#00d9ff';
            const avatarIdleBorder = isLight ? 'rgba(0,120,184,0.25)' : 'rgba(0,217,255,0.1)';
            const phoneColor = isLight ? '#6b7280' : '#555';

            return '<div class="dlr-contact-row' + (isActive ? ' active' : '') + '" onclick="dialerSelectContact(\'' + c.id + '\')" style="' + rowStyle + '">' +
                '<input type="checkbox" ' + (sel ? 'checked' : '') + ' onclick="event.stopPropagation()" onchange="dialerToggleSelect(\'' + c.id + '\')" style="accent-color:' + accentClr + ';width:14px;height:14px;cursor:pointer;flex-shrink:0;">' +
                '<div style="width:30px;height:30px;border-radius:50%;background:' + (isActive ? avatarActiveBg : avatarIdleBg) + ';border:1px solid ' + (isActive ? avatarActiveBorder : avatarIdleBorder) + ';display:flex;align-items:center;justify-content:center;font-weight:700;font-size:0.75rem;color:' + accentClr + ';flex-shrink:0;position:relative;">' + init +
                    '<span class="' + liveDotCls + '"></span>' +
                '</div>' +
                '<div style="flex:1;min-width:0;">' +
                    '<div style="display:flex;align-items:center;justify-content:space-between;gap:4px;">' +
                        '<span style="font-weight:600;font-size:0.88rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0;">' + dialerEsc(c.name) + '</span>' +
                        '<div style="display:flex;align-items:center;gap:4px;flex-shrink:0;">' +
                            dots +
                            '<span class="dlr-call-badge" data-call-badge="' + c.id + '" style="font-size:0.75rem;padding:1px 6px;">Dials: ' + callCount + '</span>' +
                        '</div>' +
                    '</div>' +
                    '<div style="font-size:0.78rem;color:' + phoneColor + ';">' + dialerEsc(c.phone) + dispBadge + '</div>' +
                '</div>' +
                (inQ ? '<i class="fa-solid fa-list-ol" style="color:' + accentClr + ';font-size:0.75rem;" title="In queue"></i>' : '') +
                '<button class="mvp-row-btn" onclick="event.stopPropagation();_mvpOpenSingle(\'' + c.id.replace(/'/g, "\\'") + '\')">Move</button>' +
            '</div>';
        }

        // ── Render contact list (left panel) with InsuranceGrokBot Smart Filters ──
        function dialerRenderContacts() {
            const list = document.getElementById('dialerContactList');
            const actionsBar = document.getElementById('dialerActionsBar');

            if (!dialerContacts.length) {
                list.innerHTML = '<div style="text-align:center;padding:30px;color:#444;font-size:.78rem;"><i class="fa-solid fa-user-slash" style="font-size:1.5rem;color:#2a2a3e;margin-bottom:8px;display:block;"></i>No contacts found</div>';
                actionsBar.style.display = 'none';
                return;
            }

            actionsBar.style.display = 'block';

            // Check if a pipeline or stage filter is active
            const manualWrap = document.getElementById('dialerPipelineManual');
            const pipelineVal = (manualWrap && manualWrap.style.display !== 'none')
                ? (document.getElementById('dialerPipelineManualInput').value || '').trim()
                : (document.getElementById('dialerPipelineFilter').value || '');
            const stageVal = (document.getElementById('dialerStageFilter').value || '');
            const pipelineActive = !!(pipelineVal || stageVal);

            // If pipeline filter is active, skip Smart Filters and show flat list with pipeline header
            if (pipelineActive) {
                const pipelineName = document.getElementById('dialerPipelineFilter');
                const stageName = document.getElementById('dialerStageFilter');
                const pLabel = pipelineName ? (pipelineName.options[pipelineName.selectedIndex] || {}).text || '' : '';
                const sLabel = stageName ? (stageName.options[stageName.selectedIndex] || {}).text || '' : '';
                const filterDesc = sLabel && sLabel !== 'All Stages' ? pLabel + ' — ' + sLabel : pLabel;
                const isLightPipe = document.body.classList.contains('light-theme');
                const pipeIconColor = isLightPipe ? '#0078b8' : '#00d9ff';
                const pipeLabelColor = isLightPipe ? '#374151' : '#888';
                const pipeCountColor = isLightPipe ? '#6b7280' : '#555';
                let html = '<div style="padding:4px 10px 2px;display:flex;align-items:center;gap:5px;">' +
                    '<i class="fa-solid fa-filter" style="color:' + pipeIconColor + ';font-size:.75rem;"></i>' +
                    '<span style="font-size:.75rem;color:' + pipeLabelColor + ';letter-spacing:.3px;text-transform:uppercase;font-weight:700;">Pipeline: ' + filterDesc + '</span>' +
                    '<span style="font-size:.75rem;color:' + pipeCountColor + ';margin-left:auto;">' + dialerContacts.length + ' contacts</span>' +
                '</div>';
                html += dialerContacts.map(c => _igbRenderContactRow(c)).join('');
                list.innerHTML = html;
                return;
            }

            // Always show Smart Filters when contacts exist
            const groups = _igbGroupContacts(dialerContacts);
            const aiReady = Object.keys(_igbIntelCache).length > 0;
            const filterLabel = aiReady ? 'AI-Powered Smart Filters' : 'Smart Filters (loading AI...)';
            const isLight = document.body.classList.contains('light-theme');
            const filterIconColor = aiReady ? '#5B7FFF' : (isLight ? '#0078b8' : '#00d9ff');
            const filterLabelColor = isLight ? '#374151' : '#444';
            let html = '<div style="padding:4px 10px 2px;display:flex;align-items:center;gap:5px;"><i class="fa-solid ' + (aiReady ? 'fa-brain' : 'fa-robot') + '" style="color:' + filterIconColor + ';font-size:.75rem;"></i><span style="font-size:.75rem;color:' + filterLabelColor + ';letter-spacing:.3px;text-transform:uppercase;font-weight:700;">' + filterLabel + '</span></div>';
            groups.forEach(g => {
                if (!g.contacts.length) return;
                const isCollapsed = _igbFilterCollapsed[g.key] || false;
                const allGrpSelected = g.contacts.length > 0 && g.contacts.every(c => dialerSelected.has(c.id));
                const selectAllBg = allGrpSelected ? g.color : (isLight ? 'rgba(0,0,0,0.06)' : 'rgba(255,255,255,0.07)');
                const selectAllColor = allGrpSelected ? (isLight ? '#fff' : '#000') : g.color;
                html += '<div class="igb-filter-hdr" data-group="' + g.key + '" onclick="igbToggleFilter(\'' + g.key + '\')" style="position:relative;">' +
                    '<i class="fa-solid fa-chevron-down igb-filter-icon' + (isCollapsed ? ' collapsed' : '') + '" style="color:' + g.color + ';"></i>' +
                    '<i class="fa-solid ' + g.icon + '" style="color:' + g.color + ';font-size:.75rem;"></i>' +
                    '<span class="igb-filter-label" style="color:' + g.color + ';">' + g.label + '</span>' +
                    '<span class="igb-filter-count">' + g.contacts.length + '</span>' +
                    '<button onclick="event.stopPropagation();_igbSelectAllInGroup(\'' + g.key + '\')" title="Select all ' + dialerEsc(g.label) + '" style="margin-left:4px;background:' + selectAllBg + ';border:1px solid ' + g.color + ';color:' + selectAllColor + ';border-radius:4px;font-size:.65rem;font-weight:700;padding:1px 6px;cursor:pointer;line-height:1.4;white-space:nowrap;">Select All</button>' +
                '</div>';
                if (!isCollapsed) {
                    html += g.contacts.map(c => _igbRenderContactRow(c)).join('');
                }
            });
            list.innerHTML = html;
        }

        // ── Select contact → load detail + messages + contact-specific calls/recordings ──
        function dialerSelectContact(id) {
            const c = dialerContacts.find(x => x.id === id);
            if (!c) return;
            dialerActiveContact = c;
            _dialerCallHistoryShowAll = false;
            _dialerRecordingsShowAll = false;
            // Show middle intel column when a contact is selected
            const intelCol = document.getElementById('dlrColIntel');
            if (intelCol) {
                intelCol.classList.remove('dlr-col-intel-hidden');
                intelCol.classList.add('dlr-col-intel-visible');
            }
            dialerRenderContacts(); // highlight active
            dialerLoadContactDetail(c.id);
            dialerLoadContactMessages(c.id);
            // Reload calls/recordings filtered to this contact
            dialerLoadAllCallHistory();
            dialerLoadRecordings();
            // Fetch merged GHL + local call count and update badge
            dialerFetchMergedCallCount(c.id);
            // Auto-scroll contact list to active row
            _jtcScrollToActiveContact();
            // Update Jump to Contact pill state
            _jtcUpdatePill();
            // On mobile, auto-switch to Intel view so user sees lead intel immediately
            if (window.innerWidth <= 768 && typeof dlrMobileSwitch === 'function') {
                dlrMobileSwitch('intel');
            }
        }

        // ═══════════════════════════════════════════════
        //   Jump to Contact — WAVV-style active contact tracking
        // ═══════════════════════════════════════════════

        // The contact ID that the power dialer is currently calling.
        // This is separate from dialerActiveContact which tracks what the user is VIEWING.
        let _jtcDialingContactId = null;

        // Jump to the contact that the power dialer is currently calling.
        // Works from: pill click, banner name click, queue row click.
        function dialerJumpToContact(targetId) {
            const id = targetId || _jtcDialingContactId;
            if (!id) return;
            const c = dialerContacts.find(x => x.id === id);
            if (!c) return;

            // Update active contact and reload panels
            dialerActiveContact = c;
            _dialerCallHistoryShowAll = false;
            _dialerRecordingsShowAll = false;
            dialerRenderContacts();
            dialerLoadContactDetail(c.id);
            dialerLoadContactMessages(c.id);
            dialerLoadAllCallHistory();
            dialerLoadRecordings();
            dialerFetchMergedCallCount(c.id);

            // Visual flash on the contact row + detail panel
            _jtcFlashContact(c.id);

            // Auto-scroll to this contact in the list
            _jtcScrollToActiveContact();

            // Auto-scroll queue to the active item
            _jtcScrollToActiveQueueItem();

            // Hide the pill since we just jumped back
            _jtcUpdatePill();
        }

        // Flash the contact row and detail panel to show the jump happened
        function _jtcFlashContact(contactId) {
            // Flash contact row in left column
            requestAnimationFrame(() => {
                const rows = document.querySelectorAll('#dialerContactList .dlr-contact-row');
                rows.forEach(row => {
                    if (row.getAttribute('onclick') && row.getAttribute('onclick').includes(contactId)) {
                        row.classList.remove('jtc-flash');
                        void row.offsetWidth; // reflow
                        row.classList.add('jtc-flash');
                    }
                });
                // Flash detail panel
                const panel = document.getElementById('dlrDetailContent');
                if (panel) {
                    panel.classList.remove('jtc-panel-flash');
                    void panel.offsetWidth;
                    panel.classList.add('jtc-panel-flash');
                }
            });
        }

        // Scroll the contact list so the active contact row is visible
        function _jtcScrollToActiveContact() {
            if (!dialerActiveContact) return;
            requestAnimationFrame(() => {
                const list = document.getElementById('dialerContactList');
                if (!list) return;
                const rows = list.querySelectorAll('.dlr-contact-row');
                for (const row of rows) {
                    const onclick = row.getAttribute('onclick') || '';
                    if (onclick.includes(dialerActiveContact.id)) {
                        row.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
                        break;
                    }
                }
            });
        }

        // Scroll the queue list so the active queue item is visible
        function _jtcScrollToActiveQueueItem() {
            if (dialerCallIdx < 0) return;
            requestAnimationFrame(() => {
                const queueList = document.getElementById('dialerQueueList');
                if (!queueList) return;
                const items = queueList.children;
                if (dialerCallIdx < items.length) {
                    items[dialerCallIdx].scrollIntoView({ behavior: 'smooth', block: 'nearest' });
                }
            });
        }

        // Show/hide the floating Jump to Contact pill based on whether user
        // has navigated away from the currently-dialing contact.
        function _jtcUpdatePill() {
            const pill = document.getElementById('jtcPill');
            if (!pill) return;

            // Only show pill when the dialer is actively calling someone
            // AND the user is viewing a DIFFERENT contact (or no contact)
            const isDialing = dialerQueueRunning && _jtcDialingContactId && dialerCallSid;
            const isViewingDifferent = !dialerActiveContact || dialerActiveContact.id !== _jtcDialingContactId;

            if (isDialing && isViewingDifferent) {
                // Find the dialing contact to show their name
                const dialingContact = dialerContacts.find(x => x.id === _jtcDialingContactId);
                if (dialingContact) {
                    document.getElementById('jtcPillName').textContent = dialingContact.name || 'Unknown';
                    // Reflect call status
                    const statusEl = document.getElementById('dialerCallStatus');
                    const statusText = statusEl ? statusEl.textContent : 'In Call';
                    document.getElementById('jtcPillStatus').textContent = statusText;
                    pill.style.display = 'flex';
                    return;
                }
            }
            pill.style.display = 'none';
        }

        // ── InsuranceGrokBot: lead score — AI only, no rule-based heuristic ──
        // Returns the AI-generated score (0-100) based on conversation analysis.
        // Returns -1 if AI hasn't analyzed yet (pending state — shows "?" in UI).
        // NEVER fakes a score with rules. If AI hasn't read the conversation, we don't guess.
        function _igbLeadScore(eng, contactObj) {
            if (_igbIsOptedOut(eng, contactObj)) return 0;

            const contactId = contactObj && contactObj.id;
            if (contactId) {
                const intel = _igbIntelCache[contactId];
                if (intel && typeof intel.score === 'number') return intel.score;
            }

            return -1; // Not analyzed yet — pending state
        }

        // ── Check if contact has opted out (DnD or "stop" message) ──
        function _igbIsOptedOut(eng, contactObj) {
            if (contactObj && contactObj.dnd) return true;
            if (eng && eng.opted_out) return true;
            return false;
        }

        // ── Score color/label — AI only, pending state when not analyzed ──
        function _igbScoreColor(score, optedOut, contactId) {
            if (optedOut) return '#ef4444';
            const intel = contactId ? _igbIntelCache[contactId] : null;
            if (intel && intel.temperature) {
                switch (intel.temperature) {
                    case 'hot': return '#4ade80';
                    case 'warm': return '#ffa500';
                    case 'cool': return '#5B7FFF';
                    case 'cold': return '#888';
                }
            }
            return '#333'; // Pending — neutral dark
        }

        function _igbScoreLabel(score, optedOut, contactId) {
            if (optedOut) return 'DnD';
            const intel = contactId ? _igbIntelCache[contactId] : null;
            if (intel && intel.temperature) {
                return intel.temperature.charAt(0).toUpperCase() + intel.temperature.slice(1);
            }
            return '...'; // Pending — AI hasn't analyzed yet
        }

        function _igbTimeAgo(isoStr) {
            if (!isoStr) return '';
            const diff = Date.now() - new Date(isoStr).getTime();
            const mins = Math.floor(diff / 60000);
            if (mins < 60) return mins + 'm ago';
            const hrs = Math.floor(mins / 60);
            if (hrs < 24) return hrs + 'h ago';
            const days = Math.floor(hrs / 24);
            return days + 'd ago';
        }

        // ── Middle panel: Lead Intelligence Dossier ──
        async function dialerLoadContactDetail(contactId) {
            const panel = document.getElementById('dlrDetailContent');
            panel.innerHTML = '<div style="text-align:center;padding:40px;"><i class="fa-solid fa-spinner fa-spin" style="color:#00d9ff;font-size:1.2rem;"></i></div>';
            try {
                const r = await _fetchRetry('/voice/contact/' + contactId, {}, { retries: 1, timeout: 15000, label: 'contact-detail' });
                if (!r.ok) { panel.innerHTML = '<div style="color:#888;padding:20px;text-align:center;">Could not load contact</div>'; return; }
                const c = await r.json();
                const eng = c.igb_engagement;

                // Cache engagement data for Smart Filters + dots
                if (eng) {
                    _igbEngagementCache[contactId] = eng;
                    dialerRenderContacts(); // Re-render to update dots + filters
                }

                const optedOut = _igbIsOptedOut(eng, c);
                const rawScore = _igbLeadScore(eng, c);
                const isPending = rawScore < 0;
                const score = isPending ? 0 : rawScore;
                const scoreDisplay = isPending ? '?' : score;
                const scoreColor = _igbScoreColor(score, optedOut, contactId);
                const circumference = 2 * Math.PI * 35; // r=35
                const dashOffset = isPending ? circumference : circumference * (1 - score / 100);

                let html = '';

                // ── Action Strip (moved to top for immediate access) ──
                html += '<div class="igb-action-strip">';
                html += '<button class="igb-action-btn act-call" onclick="dialerCallActiveContact()"><i class="fa-solid fa-phone"></i> Call</button>';
                html += '<button class="igb-action-btn act-sms" onclick="if(_iosCurrentApp!==\'messages\')iosOpenApp(\'messages\');"><i class="fa-solid fa-message"></i> SMS</button>';
                html += '<button class="igb-action-btn act-queue" onclick="dialerAddActiveToQueue()"><i class="fa-solid fa-plus"></i> Queue</button>';
                html += '<button class="igb-action-btn act-schedule" onclick="dialerScheduleActiveContact()"><i class="fa-solid fa-calendar"></i> Schedule</button>';
                html += '<button class="igb-action-btn act-workflow" onclick="dialerAddToWorkflow()"><i class="fa-solid fa-diagram-project"></i> Workflow</button>';
                html += '</div>';

                // ── Name Header with Lead Score Ring ──
                html += '<div style="display:flex;align-items:center;gap:14px;margin-bottom:14px;padding-bottom:12px;border-bottom:1px solid rgba(255,255,255,0.06);">';
                // Lead Score Ring
                html += '<div class="igb-score-ring">';
                html += '<svg viewBox="0 0 80 80"><circle class="igb-ring-bg" cx="40" cy="40" r="35"/>';
                html += '<circle class="igb-ring-fg" cx="40" cy="40" r="35" stroke="' + scoreColor + '" stroke-dasharray="' + circumference + '" stroke-dashoffset="' + dashOffset + '"/></svg>';
                html += '<div class="igb-score-label"><span class="igb-score-num" style="color:' + scoreColor + ';">' + scoreDisplay + '</span><span class="igb-score-sub">' + _igbScoreLabel(score, optedOut, contactId) + '</span></div>';
                html += '</div>';
                // Name + phone
                html += '<div style="flex:1;min-width:0;">';
                html += '<div style="font-weight:700;font-size:1.25rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">' + dialerEsc(c.name || 'Unknown') + '</div>';
                html += '<div style="font-size:0.95rem;color:#888;">' + dialerEsc(formatPhone(c.phone)) + '</div>';
                if (c.email) html += '<div style="font-size:0.78rem;color:#666;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">' + dialerEsc(c.email) + '</div>';
                html += '</div></div>';

                // ── DnD / Opted Out Warning ──
                if (optedOut) {
                    html += '<div style="background:rgba(239,68,68,0.1);border:1px solid rgba(239,68,68,0.25);border-radius:6px;padding:8px 12px;margin-bottom:10px;display:flex;align-items:center;gap:8px;">';
                    html += '<i class="fa-solid fa-ban" style="color:#ef4444;font-size:0.85rem;"></i>';
                    html += '<span style="color:#ef4444;font-size:0.82rem;font-weight:600;">Do Not Contact</span>';
                    html += '<span style="color:#888;font-size:0.75rem;margin-left:auto;">' + (c.dnd ? 'CRM DnD enabled' : 'Lead sent "Stop"') + '</span>';
                    html += '</div>';
                }

                // ── Quick Intel Pills ──
                if (eng) {
                    html += '<div class="igb-intel-row">';
                    const totalMsgs = (eng.messages.lead || 0) + (eng.messages.assistant || 0);
                    if (totalMsgs > 0) html += '<span class="igb-intel-pill"><i class="fa-solid fa-message"></i><span class="igb-pill-val">' + totalMsgs + '</span> SMS</span>';
                    if (eng.calls.total_calls > 0) {
                        html += '<span class="igb-intel-pill"><i class="fa-solid fa-phone"></i><span class="igb-pill-val">' + eng.calls.total_calls + '</span> calls (' + eng.calls.connected + ' connected)</span>';
                    }
                    if (eng.calls.total_duration > 0) {
                        const mins = Math.floor(eng.calls.total_duration / 60);
                        const secs = eng.calls.total_duration % 60;
                        html += '<span class="igb-intel-pill"><i class="fa-solid fa-clock"></i><span class="igb-pill-val">' + mins + ':' + String(secs).padStart(2, '0') + '</span> talk time</span>';
                    }
                    if (eng.calls.recordings > 0) html += '<span class="igb-intel-pill"><i class="fa-solid fa-microphone"></i><span class="igb-pill-val">' + eng.calls.recordings + '</span> recordings</span>';
                    // Last active
                    const lastMsg = eng.messages.last_message_at;
                    const lastCall = eng.calls.last_call_at;
                    const latest = (lastMsg && lastCall) ? (lastMsg > lastCall ? lastMsg : lastCall) : (lastMsg || lastCall);
                    if (latest) html += '<span class="igb-intel-pill"><i class="fa-solid fa-signal"></i>Active <span class="igb-pill-val">' + _igbTimeAgo(latest) + '</span></span>';
                    // Dispositions
                    if (eng.calls.dispositions) {
                        const disps = eng.calls.dispositions;
                        if (disps.left_voicemail) html += '<span class="igb-intel-pill"><i class="fa-solid fa-voicemail"></i><span class="igb-pill-val">' + disps.left_voicemail + '</span> VM left</span>';
                    }
                    html += '</div>';
                }

                // ── Engagement Timeline ──
                if (eng) {
                    const totalMsgs = (eng.messages.lead || 0) + (eng.messages.assistant || 0);
                    const steps = [
                        { label: 'Added', done: true },
                        { label: 'Contacted', done: eng.messages.assistant > 0 || eng.calls.total_calls > 0 },
                        { label: 'Replied', done: eng.messages.lead > 0 },
                        { label: 'Called', done: eng.calls.total_calls > 0 },
                        { label: 'Connected', done: eng.calls.connected > 0 },
                    ];
                    html += '<div class="igb-timeline">';
                    steps.forEach((s, i) => {
                        if (i > 0) html += '<div class="igb-tl-line' + (s.done ? ' completed' : '') + '"></div>';
                        html += '<div class="igb-tl-node"><div class="igb-tl-dot' + (s.done ? ' completed' : '') + '"></div><div class="igb-tl-label">' + s.label + '</div></div>';
                    });
                    html += '</div>';
                }

                // ── CRM Contact Info (compact) ──
                const fields = [
                    { label: 'Phone', value: formatPhone(c.phone), icon: 'fa-phone' },
                    { label: 'Address', value: [c.address, c.city, c.state].filter(Boolean).join(', '), icon: 'fa-location-dot' },
                    { label: 'Source', value: c.source, icon: 'fa-link' },
                    { label: 'Date Added', value: c.dateAdded ? new Date(c.dateAdded).toLocaleDateString() : '', icon: 'fa-calendar' },
                ];
                const hasFields = fields.some(f => f.value);
                if (hasFields) {
                    html += '<div style="margin-top:6px;padding-top:8px;border-top:1px solid rgba(255,255,255,0.04);">';
                    html += '<div style="font-size:.75rem;font-weight:700;color:#888;margin-bottom:5px;text-transform:uppercase;letter-spacing:.5px;">Contact Info</div>';
                    fields.forEach(f => {
                        if (f.value) {
                            html += '<div class="dlr-field-label"><i class="fa-solid ' + f.icon + ' me-1" style="width:12px;"></i>' + f.label + '</div>';
                            html += '<div class="dlr-field-value">' + dialerEsc(f.value) + '</div>';
                        }
                    });
                    html += '</div>';
                }

                // Tags
                if (c.tags && c.tags.length) {
                    html += '<div style="margin-top:6px;"><div class="dlr-field-label">Tags</div>';
                    html += '<div style="margin-bottom:8px;">' + c.tags.map(t => '<span style="display:inline-block;background:rgba(0,217,255,0.06);border:1px solid rgba(0,217,255,0.12);color:#00d9ff;padding:2px 8px;border-radius:4px;font-size:0.75rem;margin:0 4px 4px 0;">' + dialerEsc(t) + '</span>').join('') + '</div></div>';
                }

                // Custom Fields
                if (c.customFields && c.customFields.length) {
                    const filled = c.customFields.filter(cf => cf.value);
                    if (filled.length) {
                        html += '<div style="margin-top:6px;padding-top:8px;border-top:1px solid rgba(255,255,255,0.04);">';
                        html += '<div style="font-size:.75rem;font-weight:700;color:#00d9ff;margin-bottom:6px;text-transform:uppercase;letter-spacing:.5px;">CRM Custom Fields</div>';
                        filled.forEach(cf => {
                            const name = cf.name || cf.fieldKey || 'Field';
                            html += '<div class="dlr-field-label">' + dialerEsc(name) + '</div>';
                            html += '<div class="dlr-field-value">' + dialerEsc(String(cf.value)) + '</div>';
                        });
                        html += '</div>';
                    }
                }

                // ── AI Summary (below CRM fields, toggle-controlled, 30-word max) ──
                const _showSummary = window.DASHBOARD_BOOT && window.DASHBOARD_BOOT.showAiSummary !== false;
                if (_showSummary && eng && eng.narrative && eng.narrative.summary) {
                    const contactFirstName = (c.firstName || '').toLowerCase().trim();
                    let narrText = eng.narrative.summary || '';
                    // Cross-validate: suppress if it names a different person
                    let narrativeValid = true;
                    if (contactFirstName && contactFirstName.length > 1) {
                        const narrLower = narrText.toLowerCase();
                        if (!narrLower.includes(contactFirstName)) {
                            const namePattern = /^([A-Z][a-z]+)\s+(is|was|has|had|called|messaged|replied|expressed|mentioned|asked|wants|needs|said)/;
                            const match = narrText.match(namePattern);
                            if (match && match[1].toLowerCase() !== contactFirstName) {
                                narrativeValid = false;
                            }
                        }
                    }
                    // Frontend 30-word hard cap
                    if (narrativeValid && narrText) {
                        const words = narrText.split(/\s+/);
                        if (words.length > 30) narrText = words.slice(0, 30).join(' ') + '...';
                        html += '<div class="igb-summary-card" style="margin-top:8px;">';
                        html += '<div class="igb-summary-hdr"><i class="fa-solid fa-robot"></i><span>AI Summary</span>';
                        if (eng.narrative.updated_at) html += '<span style="margin-left:auto;font-size:.75rem;color:#444;">' + _igbTimeAgo(eng.narrative.updated_at) + '</span>';
                        html += '</div>';
                        html += '<div class="igb-summary-body">' + dialerEsc(narrText) + '</div>';
                        html += '</div>';
                    }
                }

                // ── Known Facts (below AI Summary, toggle-controlled, 10-word max per line) ──
                const _showFacts = window.DASHBOARD_BOOT && window.DASHBOARD_BOOT.showKnownFacts !== false;
                if (_showFacts && eng && eng.facts && eng.facts.length) {
                    html += '<div style="margin-top:8px;margin-bottom:10px;">';
                    html += '<div style="font-size:.75rem;font-weight:700;color:#4ade80;margin-bottom:5px;text-transform:uppercase;letter-spacing:.5px;"><i class="fa-solid fa-brain me-1"></i>Known Facts</div>';
                    html += '<ul class="igb-facts-list">';
                    eng.facts.forEach(function(f) {
                        // Frontend 10-word hard cap
                        var words = f.split(/\s+/);
                        if (words.length > 10) f = words.slice(0, 10).join(' ');
                        html += '<li>' + dialerEsc(f) + '</li>';
                    });
                    html += '</ul>';
                    html += '</div>';
                }

                // ── AI Intelligence (loaded async) ──
                html += '<div id="igb-ai-summary" style="margin-top:8px;"></div>';
                html += '<div id="igb-nba-section" style="margin-top:8px;"></div>';
                html += '<div id="igb-pipeline-badge" style="margin-top:6px;"></div>';

                // Notes + Add Note form
                html += '<div style="margin-top:6px;padding-top:8px;border-top:1px solid rgba(255,255,255,0.04);">';
                html += '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:5px;">'
                    + '<span style="font-size:.75rem;font-weight:700;color:#aaa;text-transform:uppercase;letter-spacing:.5px;">Notes</span>'
                    + '<button onclick="_dlrToggleAddNote(' + "'" + 'dlrAddNote_' + c.id + "'" + ')" style="background:rgba(0,255,136,0.08);border:1px solid rgba(0,255,136,0.2);color:var(--accent);border-radius:4px;font-size:.65rem;font-weight:700;padding:2px 8px;cursor:pointer;"><i class=\"fa-solid fa-plus\" style=\"margin-right:3px;\"></i>Add Note</button>'
                    + '</div>';
                html += '<div id="dlrAddNote_' + c.id + '" style="display:none;margin-bottom:8px;">'
                    + '<textarea id="dlrNoteText_' + c.id + '" placeholder="Type your note..." rows="3" style="width:100%;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.1);border-radius:6px;color:#ddd;font-size:0.82rem;padding:6px 8px;resize:vertical;box-sizing:border-box;"></textarea>'
                    + '<button onclick="_dlrSaveNote(' + "'" + c.id + "'" + ')" style="margin-top:4px;width:100%;padding:5px;background:linear-gradient(135deg,var(--accent),#00b36b);border:none;border-radius:5px;color:#000;font-weight:700;font-size:.75rem;cursor:pointer;">Save Note to GHL</button>'
                    + '</div>';
                if (c.notes && c.notes.length) {
                    c.notes.forEach(n => {
                        html += '<div style="background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.04);border-radius:6px;padding:8px 10px;margin-bottom:6px;font-size:0.82rem;color:#bbb;">' + dialerEsc(n.body) + '<div style="font-size:.75rem;color:#555;margin-top:3px;">' + (n.dateAdded ? new Date(n.dateAdded).toLocaleDateString() : '') + '</div></div>';
                    });
                } else {
                    html += '<div style="color:#555;font-size:.75rem;font-style:italic;">No notes yet</div>';
                }
                html += '</div>';

                // Edit Contact section
                html += '<div style="margin-top:8px;padding-top:8px;border-top:1px solid rgba(255,255,255,0.04);">'
                    + '<div id="dlrEditContactToggle_' + c.id + '">'
                    + '<button onclick="_dlrOpenEditContact(' + "'" + c.id + "'" + ')" style="width:100%;padding:5px;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);border-radius:6px;color:#aaa;font-size:.75rem;font-weight:600;cursor:pointer;"><i class=\"fa-solid fa-pen\" style=\"margin-right:5px;color:#5B7FFF;\"></i>Edit Contact</button>'
                    + '</div>'
                    + '<div id="dlrEditContactForm_' + c.id + '" style="display:none;"></div>'
                    + '</div>';

                // Powered by footer
                html += '<div style="text-align:center;margin-top:12px;padding-top:8px;border-top:1px solid rgba(255,255,255,0.03);"><span style="font-size:.75rem;color:#333;letter-spacing:.3px;">Powered by InsuranceGrokBot</span></div>';

                panel.innerHTML = html;
                document.getElementById('dlrDetailActions').style.display = 'none';

                // Async load: Next-Best-Actions + Pipeline from intelligence API
                _loadContactIntelligence(contactId);
            } catch(e) {
                panel.innerHTML = '<div style="color:#888;padding:20px;text-align:center;">Failed to load contact</div>';
            }
        }

        // ── Note Creation + Contact Edit Helpers ──

        function _dlrToggleAddNote(formId) {
            const el = document.getElementById(formId);
            if (!el) return;
            el.style.display = el.style.display === 'none' ? 'block' : 'none';
        }

        async function _dlrSaveNote(contactId) {
            const ta = document.getElementById('dlrNoteText_' + contactId);
            if (!ta) return;
            const body = ta.value.trim();
            if (!body) { _showDashToast(false, 'Note cannot be empty'); return; }
            try {
                const r = await fetch('/voice/contact/' + contactId + '/notes', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ body }),
                });
                const d = await r.json();
                if (d.ok) {
                    _showDashToast(true, 'Note saved to GHL');
                    ta.value = '';
                    document.getElementById('dlrAddNote_' + contactId).style.display = 'none';
                    // Reload contact detail to show new note
                    if (dialerActiveContact && dialerActiveContact.id === contactId) dialerLoadContactDetail(contactId);
                } else {
                    _showDashToast(false, d.error || 'Failed to save note');
                }
            } catch(e) {
                _showDashToast(false, 'Network error saving note');
            }
        }

        function _dlrOpenEditContact(contactId) {
            const c = dialerContacts.find(x => x.id === contactId) || (dialerActiveContact && dialerActiveContact.id === contactId ? dialerActiveContact : null);
            const toggleEl = document.getElementById('dlrEditContactToggle_' + contactId);
            const formEl = document.getElementById('dlrEditContactForm_' + contactId);
            if (!formEl) return;
            if (toggleEl) toggleEl.style.display = 'none';
            const fld = (k, label, val) => '<div style="margin-bottom:6px;">'
                + '<label style="font-size:.7rem;color:#888;display:block;margin-bottom:2px;">' + label + '</label>'
                + '<input data-field="' + k + '" value="' + dialerEsc(val || '') + '" style="width:100%;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:5px;color:#ddd;font-size:0.82rem;padding:4px 7px;box-sizing:border-box;">'
                + '</div>';
            const data = c || {};
            formEl.innerHTML = ''
                + fld('firstName', 'First Name', data.firstName)
                + fld('lastName', 'Last Name', data.lastName)
                + fld('phone', 'Phone', data.phone)
                + fld('email', 'Email', data.email)
                + fld('address1', 'Address', data.address)
                + fld('city', 'City', data.city)
                + fld('state', 'State', data.state)
                + fld('companyName', 'Company', data.companyName)
                + '<div style="display:flex;gap:6px;margin-top:8px;">'
                + '<button onclick="_dlrSaveContact(\'' + contactId + '\')" style="flex:1;padding:5px;background:linear-gradient(135deg,var(--accent),#00b36b);border:none;border-radius:5px;color:#000;font-weight:700;font-size:.75rem;cursor:pointer;">Save to GHL</button>'
                + '<button onclick="document.getElementById(\'dlrEditContactForm_' + contactId + '\').style.display=\'none\';document.getElementById(\'dlrEditContactToggle_' + contactId + '\').style.display=\'\'" style="padding:5px 10px;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);border-radius:5px;color:#888;font-size:.75rem;cursor:pointer;">Cancel</button>'
                + '</div>';
            formEl.style.display = 'block';
        }

        async function _dlrSaveContact(contactId) {
            const formEl = document.getElementById('dlrEditContactForm_' + contactId);
            if (!formEl) return;
            const payload = {};
            formEl.querySelectorAll('input[data-field]').forEach(inp => {
                const v = inp.value.trim();
                if (v) payload[inp.dataset.field] = v;
            });
            if (!Object.keys(payload).length) { _showDashToast(false, 'No changes to save'); return; }
            try {
                const r = await fetch('/voice/contact/' + contactId, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                });
                const d = await r.json();
                if (d.ok) {
                    _showDashToast(true, 'Contact updated in GHL');
                    // Update local copy
                    const local = dialerContacts.find(x => x.id === contactId);
                    if (local) {
                        if (payload.firstName) local.firstName = payload.firstName;
                        if (payload.lastName) local.lastName = payload.lastName;
                        if (payload.firstName || payload.lastName) local.name = ((payload.firstName || local.firstName || '') + ' ' + (payload.lastName || local.lastName || '')).trim();
                        if (payload.phone) local.phone = payload.phone;
                    }
                    // Reload detail panel
                    if (dialerActiveContact && dialerActiveContact.id === contactId) dialerLoadContactDetail(contactId);
                } else {
                    _showDashToast(false, d.error || 'Failed to update contact');
                }
            } catch(e) {
                _showDashToast(false, 'Network error updating contact');
            }
        }

        // ── Contact Intelligence Loader (async, non-blocking) ──
        async function _loadContactIntelligence(contactId) {
            // Show loading shimmer while AI thinks
            const summaryEl = document.getElementById('igb-ai-summary');
            if (summaryEl) {
                summaryEl.innerHTML = '<div style="padding:10px 12px;background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.05);border-radius:8px;">'
                    + '<div style="display:flex;align-items:center;gap:8px;">'
                    + '<i class="fa-solid fa-brain" style="color:#5B7FFF;font-size:.75rem;"></i>'
                    + '<span style="font-size:.75rem;color:#666;">AI is analyzing this lead...</span>'
                    + '<i class="fa-solid fa-spinner fa-spin" style="color:#444;font-size:.75rem;margin-left:auto;"></i>'
                    + '</div></div>';
            }

            try {
                const r = await fetch('/api/contact/' + contactId + '/intelligence');
                if (!r.ok) { if (summaryEl) summaryEl.innerHTML = ''; return; }
                const intel = await r.json();

                // Update Smart Filter cache so contact moves to correct group
                if (intel.temperature && intel.temperature !== 'unknown') {
                    const aiScore = typeof intel.score === 'object' ? intel.score.score : intel.score;
                    _igbIntelCache[contactId] = {
                        temperature: intel.temperature,
                        score: typeof aiScore === 'number' ? aiScore : 50,
                        summary: intel.summary || '',
                        temperature_reason: intel.temperature_reason || '',
                        should_respond: intel.should_respond || false,
                        under_contract: intel.under_contract || false,
                    };
                    dialerRenderContacts();
                }

                // ── AI Summary + Temperature Badge ──
                if (summaryEl && (intel.summary || intel.temperature)) {
                    const tempColors = {hot: '#ff3b30', warm: '#ff9500', cool: '#5B7FFF', cold: '#888'};
                    const tempBgs = {hot: 'rgba(255,59,48,0.08)', warm: 'rgba(255,149,0,0.08)', cool: 'rgba(91,127,255,0.08)', cold: 'rgba(255,255,255,0.03)'};
                    const tempIcons = {hot: 'fa-fire', warm: 'fa-temperature-half', cool: 'fa-snowflake', cold: 'fa-icicles'};
                    const temp = intel.temperature || 'warm';
                    const tColor = tempColors[temp] || '#888';
                    const tBg = tempBgs[temp] || 'rgba(255,255,255,0.03)';
                    const tIcon = tempIcons[temp] || 'fa-temperature-half';
                    const score = intel.score ? (typeof intel.score === 'object' ? intel.score.score : intel.score) : '?';

                    let sHtml = '<div style="padding:10px 12px;background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.05);border-radius:8px;">';

                    // Temperature + Score row
                    sHtml += '<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;">';
                    sHtml += '<div style="display:flex;align-items:center;gap:5px;padding:3px 10px;background:' + tBg + ';border:1px solid ' + tColor + '33;border-radius:20px;">';
                    sHtml += '<i class="fa-solid ' + tIcon + '" style="color:' + tColor + ';font-size:.75rem;"></i>';
                    sHtml += '<span style="font-size:.75rem;font-weight:700;color:' + tColor + ';text-transform:uppercase;">' + temp + '</span>';
                    sHtml += '</div>';
                    sHtml += '<div style="display:flex;align-items:center;gap:4px;margin-left:auto;">';
                    sHtml += '<span style="font-size:.75rem;color:#666;">Score</span>';
                    sHtml += '<span style="font-size:.8rem;font-weight:800;color:' + tColor + ';">' + score + '</span>';
                    sHtml += '</div>';
                    sHtml += '</div>';

                    // AI Summary text
                    if (intel.summary) {
                        sHtml += '<div style="font-size:.76rem;color:#ccc;line-height:1.4;">';
                        sHtml += '<i class="fa-solid fa-brain" style="color:#5B7FFF;font-size:.75rem;margin-right:4px;"></i>';
                        sHtml += dialerEsc(intel.summary);
                        sHtml += '</div>';
                    }

                    // Temperature reason
                    if (intel.temperature_reason) {
                        sHtml += '<div style="font-size:.75rem;color:#777;margin-top:4px;font-style:italic;">' + dialerEsc(intel.temperature_reason) + '</div>';
                    }

                    sHtml += '</div>';
                    summaryEl.innerHTML = sHtml;
                } else if (summaryEl) {
                    summaryEl.innerHTML = '';
                }

                // ── AI-Powered Next-Best-Actions ──
                const nbaSection = document.getElementById('igb-nba-section');
                if (nbaSection && intel.actions && intel.actions.length) {
                    let nbaHtml = '<div style="padding-top:6px;border-top:1px solid rgba(255,255,255,0.04);">';
                    nbaHtml += '<div style="font-size:.75rem;font-weight:700;color:#ff9500;margin-bottom:6px;text-transform:uppercase;letter-spacing:.5px;"><i class="fa-solid fa-lightbulb me-1"></i>Recommended Actions</div>';
                    intel.actions.forEach(function(a) {
                        const priColor = a.priority === 'high' ? '#ff3b30' : a.priority === 'medium' ? '#ff9500' : '#888';
                        const priBg = a.priority === 'high' ? 'rgba(255,59,48,0.08)' : a.priority === 'medium' ? 'rgba(255,149,0,0.08)' : 'rgba(255,255,255,0.02)';
                        nbaHtml += '<div style="display:flex;align-items:flex-start;gap:8px;padding:6px 8px;margin-bottom:4px;background:' + priBg + ';border:1px solid ' + priColor + '22;border-radius:6px;">';
                        nbaHtml += '<i class="' + (a.icon || 'fa-solid fa-circle') + '" style="color:' + priColor + ';font-size:.75rem;margin-top:2px;flex-shrink:0;"></i>';
                        nbaHtml += '<div style="flex:1;min-width:0;">';
                        nbaHtml += '<div style="font-size:.75rem;font-weight:600;color:#ddd;">' + dialerEsc(a.action) + '</div>';
                        if (a.reason) nbaHtml += '<div style="font-size:.75rem;color:#888;margin-top:1px;">' + dialerEsc(a.reason) + '</div>';
                        nbaHtml += '</div></div>';
                    });
                    nbaHtml += '</div>';
                    nbaSection.innerHTML = nbaHtml;
                }

                // ── Pipeline Badge ──
                const pipelineBadge = document.getElementById('igb-pipeline-badge');
                if (pipelineBadge && intel.pipeline) {
                    const p = intel.pipeline;
                    const statusColor = p.status === 'won' ? '#34C759' : p.status === 'lost' ? '#ff3b30' : '#5B7FFF';
                    const statusBg = p.status === 'won' ? 'rgba(52,199,89,0.08)' : p.status === 'lost' ? 'rgba(255,59,48,0.08)' : 'rgba(91,127,255,0.08)';
                    let pHtml = '<div style="display:flex;align-items:center;gap:8px;padding:6px 10px;background:' + statusBg + ';border:1px solid ' + statusColor + '22;border-radius:6px;margin-bottom:4px;">';
                    pHtml += '<i class="fa-solid fa-chart-line" style="color:' + statusColor + ';font-size:.75rem;"></i>';
                    pHtml += '<span style="font-size:.75rem;color:#ccc;">' + dialerEsc(p.pipeline_name || 'Pipeline') + '</span>';
                    pHtml += '<span style="font-size:.75rem;font-weight:700;color:' + statusColor + ';margin-left:auto;">' + dialerEsc(p.stage_name || 'Unknown') + '</span>';
                    if (p.monetary_value) pHtml += '<span style="font-size:.75rem;color:#888;margin-left:4px;">$' + Number(p.monetary_value).toLocaleString() + '</span>';
                    pHtml += '</div>';
                    pipelineBadge.innerHTML = pHtml;
                }
            } catch(e) {
                // Intelligence is supplementary — silent fail
                if (summaryEl) summaryEl.innerHTML = '';
            }
        }

        // ══════════════════════════════════════════════════════
        // FULL-FEATURED SMS THREAD UI
        // ══════════════════════════════════════════════════════
        let _dlrSmsContactId = null;
        let _dlrSmsContactName = '';
        let _dlrSmsContactPhone = '';
        let _dlrAvailableChannels = ['ghl'];
        let _dlrTwilioNumber = '';
        let _dlrChannelsLoaded = false;

        // ── Fetch available SMS channels from backend ──
        async function dlrLoadChannels() {
            if (_dlrChannelsLoaded) return;
            try {
                const r = await fetch('/voice/sms-channels');
                if (!r.ok) return;
                const d = await r.json();
                _dlrAvailableChannels = d.channels || ['ghl'];
                _dlrTwilioNumber = d.twilio_number || '';
                _dlrChannelsLoaded = true;
                dlrUpdateChannelUI();
            } catch(e) {
                console.warn('Failed to load SMS channels:', e);
            }
        }

        // ── Update channel selector UI based on available channels ──
        function dlrUpdateChannelUI() {
            const row = document.getElementById('dlrChannelRow');
            const sel = document.getElementById('dlrChannelSelect');
            const badge = document.getElementById('dlrChannelBadge');
            if (!sel) return;

            // Rebuild options
            sel.innerHTML = '';
            if (_dlrAvailableChannels.includes('ghl')) {
                var opt = document.createElement('option');
                opt.value = 'ghl';
                opt.textContent = 'GHL';
                sel.appendChild(opt);
            }
            if (_dlrAvailableChannels.includes('twilio')) {
                var opt = document.createElement('option');
                opt.value = 'twilio';
                opt.textContent = 'IGB ' + formatPhone(_dlrTwilioNumber);
                sel.appendChild(opt);
            }

            // Show selector if multiple channels available
            if (row) row.style.display = _dlrAvailableChannels.length > 1 ? 'flex' : 'none';

            // Update header badge
            dlrOnChannelChange();
        }

        // ── Handle channel change ──
        function dlrOnChannelChange() {
            var sel = document.getElementById('dlrChannelSelect');
            var badge = document.getElementById('dlrChannelBadge');
            var note = document.getElementById('dlrChannelNote');
            if (!sel) return;
            var ch = sel.value;
            if (badge) {
                if (ch === 'twilio') {
                    badge.textContent = 'via InsuranceGrokBot';
                    badge.style.background = 'rgba(0,217,255,0.07)';
                    badge.style.borderColor = 'rgba(0,217,255,0.15)';
                    badge.style.color = '#00d9ff';
                } else {
                    badge.textContent = 'via LeadConnector';
                    badge.style.background = 'rgba(74,222,128,0.07)';
                    badge.style.borderColor = 'rgba(74,222,128,0.15)';
                    badge.style.color = '#4ade80';
                }
            }
            if (note) {
                note.textContent = ch === 'twilio' ? 'A2P 10DLC registered number' : '';
            }
        }

        // ── Get current selected channel ──
        function dlrGetChannel() {
            var sel = document.getElementById('dlrChannelSelect');
            return (sel && sel.value) || 'ghl';
        }

        // ── Date separator helpers ──
        function _dlrDateLabel(ts) {
            if (!ts) return '';
            const d = new Date(ts);
            const today = new Date(); today.setHours(0,0,0,0);
            const yesterday = new Date(today); yesterday.setDate(today.getDate()-1);
            const msgDay = new Date(d); msgDay.setHours(0,0,0,0);
            if (msgDay.getTime() === today.getTime()) return 'Today';
            if (msgDay.getTime() === yesterday.getTime()) return 'Yesterday';
            return d.toLocaleDateString([], {weekday:'short',month:'short',day:'numeric'});
        }
        function _dlrTimeLabel(ts) {
            if (!ts) return '';
            return new Date(ts).toLocaleTimeString([], {hour:'numeric',minute:'2-digit'});
        }
        function _dlrIsSameDay(ts1, ts2) {
            if (!ts1 || !ts2) return false;
            const a = new Date(ts1), b = new Date(ts2);
            return a.getFullYear()===b.getFullYear() && a.getMonth()===b.getMonth() && a.getDate()===b.getDate();
        }

        // ── Message type badge ──
        function _dlrTypeBadge(type) {
            const icons = { SMS:'fa-message', Email:'fa-envelope', Call:'fa-phone', Note:'fa-note-sticky', Activity:'fa-bolt', TYPE_SMS:'fa-message', TYPE_EMAIL:'fa-envelope' };
            const labels = { SMS:'SMS', Email:'Email', Call:'Call', Note:'Note', Activity:'Activity', TYPE_SMS:'SMS', TYPE_EMAIL:'Email' };
            const label = labels[type] || (type ? type.replace('TYPE_','') : '');
            if (!label || label === 'SMS') return ''; // SMS is the default — skip badge
            const icon = icons[type] || 'fa-message';
            return '<span class="dlr-type-badge"><i class="fa-solid ' + icon + '"></i> ' + dialerEsc(label) + '</span>';
        }

        // ── Render full thread ──
        function _dlrRenderThread(msgs) {
            if (!msgs.length) {
                return '<div id="dlrMsgEmptyState" style="text-align:center;padding:50px 16px;">' +
                    '<div style="width:44px;height:44px;border-radius:50%;background:rgba(0,217,255,0.05);border:1px solid rgba(0,217,255,0.08);display:flex;align-items:center;justify-content:center;margin:0 auto 10px;">' +
                    '<i class="fa-solid fa-comment-slash" style="font-size:1.1rem;color:#2a3a4a;"></i></div>' +
                    '<div style="color:#555;font-size:0.8rem;font-weight:600;">No messages yet</div>' +
                    '<div style="color:#3a3a4a;font-size:0.75rem;margin-top:3px;">Send the first one below</div></div>';
            }
            let html = '';
            let lastDay = null;
            msgs.forEach((m, i) => {
                const isOut = m.role !== 'lead'; // outbound = bot/assistant/agent
                const ts = m.timestamp || null;
                const day = ts ? _dlrDateLabel(ts) : null;

                // Date separator
                if (day && day !== lastDay) {
                    html += '<div class="dlr-date-sep"><span>' + dialerEsc(day) + '</span></div>';
                    lastDay = day;
                }

                const timeLbl = _dlrTimeLabel(ts);
                const typeBadge = _dlrTypeBadge(m.type || m.messageType || '');

                html += '<div class="dlr-msg-row ' + (isOut ? 'outbound' : 'inbound') + '">';
                html += '<div class="dlr-bubble ' + (isOut ? 'outbound sent' : 'inbound') + '">' + dialerEsc(m.text || '') + '</div>';
                html += '<div class="dlr-msg-meta">';
                if (typeBadge) html += typeBadge;
                if (timeLbl) html += '<span>' + timeLbl + '</span>';
                if (isOut) html += '<i class="fa-solid fa-check-double dlr-status-icon sent" title="Sent"></i>';
                html += '</div>';
                html += '</div>';
            });
            return html;
        }

        // ── Load contact messages ──
        async function dialerLoadContactMessages(contactId) {
            _dlrSmsContactId = contactId;
            const c = dialerActiveContact;
            _dlrSmsContactName = (c && (c.name || c.firstName)) || '';
            _dlrSmsContactPhone = (c && c.phone) || '';

            // Update iOS Messages nav header
            const avatar = document.getElementById('iosMsgAvatar');
            const nameEl = document.getElementById('iosMsgContactName');
            const phoneEl = document.getElementById('iosMsgContactPhone');
            if (avatar) avatar.textContent = (_dlrSmsContactName || '?')[0].toUpperCase();
            if (nameEl) nameEl.textContent = _dlrSmsContactName || 'Contact';
            if (phoneEl) phoneEl.textContent = formatPhone(_dlrSmsContactPhone);
            // Auto-open Messages app when contact selected
            if (_iosCurrentApp !== 'messages') iosOpenApp('messages');

            const msgPanel = document.getElementById('dlrMessagesList');
            const composer = document.getElementById('dlrSmsComposer');
            if (!msgPanel) return;
            msgPanel.innerHTML = '<div style="text-align:center;padding:30px;"><i class="fa-solid fa-spinner fa-spin" style="color:#00d9ff;"></i></div>';
            if (composer) composer.style.display = 'none';
            try {
                const r = await _fetchRetry('/voice/contact/' + contactId + '/messages', {}, { retries: 1, timeout: 15000, label: 'messages' });
                if (!r.ok) { msgPanel.innerHTML = '<div style="color:#888;padding:24px;text-align:center;font-size:.82rem;">Could not load messages</div>'; return; }
                const d = await r.json();
                const msgs = d.messages || [];
                const calls = d.calls || [];

                msgPanel.innerHTML = _dlrRenderThread(msgs);
                msgPanel.scrollTop = msgPanel.scrollHeight;

                // Show composer
                if (composer) composer.style.display = 'block';
                // Load available SMS channels (async, non-blocking)
                dlrLoadChannels();
                // Reset textarea
                const ta = document.getElementById('dlrSmsText');
                if (ta) { ta.value = ''; ta.style.height = ''; dlrUpdateCharCount(''); }

                // Render contact-specific call history in calls tab
                const callPanel = document.getElementById('dialerHistoryList');
                if (!calls.length) {
                    callPanel.innerHTML = '<div style="text-align:center;padding:20px;color:#444;font-size:.82rem;">No calls with this contact</div>';
                } else {
                    callPanel.innerHTML = calls.map(c => {
                        const statusColors = { completed:'var(--accent)', 'no-answer':'#ffa500', busy:'#ffa500', failed:'#ef4444' };
                        const statusColor = statusColors[c.status] || '#00d9ff';
                        const durMin = c.duration ? Math.floor(c.duration / 60) + ':' + String(c.duration % 60).padStart(2, '0') : '--:--';
                        const dt = c.started_at ? new Date(c.started_at).toLocaleString() : '';
                        const hasRec = !!c.recording_url;
                        const hasTx = c.transcript && c.transcript.length > 0;
                        return '<div style="display:flex;align-items:center;gap:8px;padding:8px;border-bottom:1px solid rgba(255,255,255,0.03);font-size:.78rem;">' +
                            '<div style="width:6px;height:6px;border-radius:50%;background:' + statusColor + ';flex-shrink:0;"></div>' +
                            '<div style="flex:1;min-width:0;"><div style="font-weight:600;">' + dialerEsc(c.direction || 'outbound') + '</div><div style="font-size:.75rem;color:#555;">' + dt + '</div></div>' +
                            '<div style="color:' + statusColor + ';font-size:.75rem;font-weight:600;">' + dialerEsc((c.status || '').replace('-',' ')) + '</div>' +
                            '<div style="color:#888;font-size:.75rem;">' + durMin + '</div>' +
                            (hasRec ? '<button onclick="playRecording(\'' + dialerEsc(c.recording_url) + '\')" class="chr-action-btn chr-btn-play" title="Play"><i class="fa-solid fa-play"></i></button>' : '') +
                            (hasTx ? '<button onclick=\'showTranscript(' + JSON.stringify(c.transcript).replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/</g, '\\x3c') + ')\' class="chr-action-btn chr-btn-transcript" title="Transcript"><i class="fa-solid fa-file-lines"></i></button>' : '') +
                        '</div>';
                    }).join('');
                }
            } catch(e) {
                msgPanel.innerHTML = '<div style="color:#ef4444;padding:16px;text-align:center;font-size:.82rem;">Error loading messages</div>';
            }
        }

        // ── History tab switching (now opens iPhone apps) ──
        function dialerSwitchHistoryTab(tab) {
            iosOpenApp(tab);
        }

        // ── SMS: textarea auto-grow ──
        function dlrAutoGrow(el) {
            el.style.height = 'auto';
            el.style.height = Math.min(el.scrollHeight, 100) + 'px';
        }

        // ── SMS: character counter with segment math ──
        function dlrUpdateCharCount(val) {
            const len = (val || '').length;
            const ccEl = document.getElementById('dlrCharCount');
            if (!ccEl) return;
            // SMS segment boundaries (GSM-7)
            let limit, segments;
            if (len <= 160) { limit = 160; segments = 1; }
            else { segments = Math.ceil(len / 153); limit = segments * 153; }
            const remaining = limit - len;
            const segsText = segments > 1 ? ' · ' + segments + ' seg' : '';
            ccEl.textContent = len + ' / ' + limit + segsText;
            ccEl.className = len > limit - 10 ? 'dlr-char-danger' : len > limit - 30 ? 'dlr-char-warn' : 'dlr-char-ok';
        }

        // ── SMS: quick reply ──
        function dlrSetQuickReply(text) {
            const ta = document.getElementById('dlrSmsText');
            if (!ta) return;
            ta.value = text;
            ta.focus();
            dlrAutoGrow(ta);
            dlrUpdateCharCount(text);
        }

        // ── Quick Options popup menu (portaled to body to escape overflow:hidden) ──
        let _dlrQuickOptionsPortal = null;
        function _dlrEnsureQuickOptionsPortal() {
            if (_dlrQuickOptionsPortal) return _dlrQuickOptionsPortal;
            const src = document.getElementById('dlrQuickOptionsMenu');
            if (!src) return null;
            // Clone to body so it escapes .iphone-screen overflow:hidden
            _dlrQuickOptionsPortal = src.cloneNode(true);
            _dlrQuickOptionsPortal.id = 'dlrQuickOptionsMenuPortal';
            document.body.appendChild(_dlrQuickOptionsPortal);
            // Hide the original
            src.style.display = 'none';
            return _dlrQuickOptionsPortal;
        }
        function dlrToggleQuickOptions(e) {
            e.stopPropagation();
            const menu = _dlrEnsureQuickOptionsPortal();
            if (!menu) return;
            const wasOpen = menu.classList.contains('open');
            menu.classList.remove('open');
            menu.style.top = ''; menu.style.bottom = ''; menu.style.left = ''; menu.style.right = '';
            if (wasOpen) return;
            // Position using fixed coords relative to the button
            const btn = e.currentTarget;
            const rect = btn.getBoundingClientRect();
            const menuHeight = 220;
            const menuWidth = 220;
            const spaceAbove = rect.top;
            const spaceBelow = window.innerHeight - rect.bottom;
            // Horizontal: align right edge with button right edge
            let left = rect.right - menuWidth;
            if (left < 8) left = 8;
            if (spaceAbove > menuHeight || spaceAbove > spaceBelow) {
                // Open upward
                menu.style.bottom = (window.innerHeight - rect.top + 6) + 'px';
                menu.style.left = left + 'px';
            } else {
                // Open downward
                menu.style.top = (rect.bottom + 6) + 'px';
                menu.style.left = left + 'px';
            }
            menu.classList.add('open');
        }
        function dlrQuickOption(text) {
            dlrSetQuickReply(text);
            const menu = document.getElementById('dlrQuickOptionsMenuPortal');
            if (menu) { menu.classList.remove('open'); menu.style.top = ''; menu.style.bottom = ''; menu.style.left = ''; menu.style.right = ''; }
        }
        // Close menu when clicking outside
        document.addEventListener('click', function(e) {
            const menu = document.getElementById('dlrQuickOptionsMenuPortal');
            if (menu && menu.classList.contains('open') && !e.target.closest('.ios-quick-options-wrap') && !e.target.closest('#dlrQuickOptionsMenuPortal')) {
                menu.classList.remove('open'); menu.style.top = ''; menu.style.bottom = ''; menu.style.left = ''; menu.style.right = '';
            }
        });

        // ── SMS: AI draft (InsuranceGrokBot suggest) ──
        async function dlrAiSuggest() {
            const btn = document.getElementById('dlrAiDraftBtn');
            const label = document.getElementById('dlrAiDraftLabel');
            const ta = document.getElementById('dlrSmsText');
            const cidEl = document.getElementById('dialerContactId');
            const cid = cidEl ? cidEl.value : (_dlrSmsContactId || '');
            if (!cid || cid === 'unknown') {
                alert('Select a contact first.');
                return;
            }
            if (btn) { btn.disabled = true; btn.style.opacity = '0.6'; }
            if (label) label.textContent = 'Drafting…';
            try {
                const resp = await fetch('/voice/contact/' + encodeURIComponent(cid) + '/ai-suggest', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({})
                });
                if (!resp.ok) throw new Error('Server ' + resp.status);
                const data = await resp.json();
                const draft = (data.suggestion || data.reply || '').trim();
                if (!draft) throw new Error('Empty AI response');
                if (ta) { ta.value = draft; ta.focus(); dlrAutoGrow(ta); dlrUpdateCharCount(draft); }
            } catch(e) {
                console.error('AI suggest failed:', e);
                const statusEl = document.getElementById('dlrSmsStatus');
                if (statusEl) {
                    statusEl.textContent = 'AI draft failed — try again';
                    statusEl.style.color = '#ef4444';
                    setTimeout(() => { statusEl.textContent = ''; }, 3500);
                }
            } finally {
                if (btn) { btn.disabled = false; btn.style.opacity = '1'; }
                if (label) label.textContent = 'AI Reply';
            }
        }

        // ── Dialer: keypad input mode ──
        let _dialInputMode = 'type';
        function dialerSetInputMode(mode) {
            _dialInputMode = mode;
            const typeDiv = document.getElementById('dialModeType');
            const kbdDiv = document.getElementById('dialModeKeypad');
            const tabType = document.getElementById('dialKbdTabType');
            const tabKbd = document.getElementById('dialKbdTabKeypad');
            if (mode === 'keypad') {
                if (typeDiv) typeDiv.style.display = 'none';
                if (kbdDiv) kbdDiv.style.display = 'flex';
                if (tabType) { tabType.style.background = 'transparent'; tabType.style.color = '#666'; }
                if (tabKbd) { tabKbd.style.background = 'rgba(0,217,255,0.15)'; tabKbd.style.color = '#00d9ff'; }
                const ph = document.getElementById('dialerManualPhone');
                const disp = document.getElementById('dialKbdDisplay');
                if (disp && ph) disp.textContent = ph.value || '';
            } else {
                if (typeDiv) typeDiv.style.display = 'flex';
                if (kbdDiv) kbdDiv.style.display = 'none';
                if (tabType) { tabType.style.background = 'rgba(0,217,255,0.15)'; tabType.style.color = '#00d9ff'; }
                if (tabKbd) { tabKbd.style.background = 'transparent'; tabKbd.style.color = '#666'; }
            }
        }

        function dialKbdPress(digit) {
            const ph = document.getElementById('dialerManualPhone');
            const disp = document.getElementById('dialKbdDisplay');
            if (ph) ph.value = (ph.value || '') + digit;
            if (disp) disp.textContent = ph ? ph.value : '';
        }

        function dialKbdBackspace() {
            const ph = document.getElementById('dialerManualPhone');
            const disp = document.getElementById('dialKbdDisplay');
            if (ph && ph.value.length > 0) ph.value = ph.value.slice(0, -1);
            if (disp) disp.textContent = ph ? ph.value : '';
        }

        // ── SMS: refresh thread ──
        async function dlrRefreshMessages() {
            if (!_dlrSmsContactId) return;
            const btn = document.getElementById('dlrRefreshBtn');
            if (btn) { btn.style.pointerEvents = 'none'; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>'; }
            await dialerLoadContactMessages(_dlrSmsContactId);
            if (btn) { btn.style.pointerEvents = ''; btn.innerHTML = '<i class="fa-solid fa-arrows-rotate"></i>'; }
        }

        // ── SMS: append optimistic sent bubble ──
        function _dlrAppendSentBubble(msgPanel, text, pendingId) {
            const timeLbl = _dlrTimeLabel(new Date());
            const div = document.createElement('div');
            div.className = 'dlr-msg-row outbound';
            div.id = pendingId;
            div.innerHTML = '<div class="dlr-bubble outbound pending" id="' + pendingId + '_bubble">' + dialerEsc(text) + '</div>' +
                '<div class="dlr-msg-meta">' +
                '<span>' + timeLbl + '</span>' +
                '<i class="fa-solid fa-clock dlr-status-icon pending" id="' + pendingId + '_icon" title="Sending…"></i>' +
                '</div>';
            msgPanel.appendChild(div);
            msgPanel.scrollTop = msgPanel.scrollHeight;
        }

        // ── SMS: mark sent bubble delivered ──
        function _dlrMarkBubbleSent(pendingId) {
            const bubble = document.getElementById(pendingId + '_bubble');
            const icon = document.getElementById(pendingId + '_icon');
            if (bubble) { bubble.classList.remove('pending'); bubble.classList.add('sent'); }
            if (icon) { icon.className = 'fa-solid fa-check-double dlr-status-icon sent'; icon.title = 'Sent'; }
        }

        // ── SMS: mark sent bubble errored ──
        function _dlrMarkBubbleError(pendingId) {
            const bubble = document.getElementById(pendingId + '_bubble');
            const icon = document.getElementById(pendingId + '_icon');
            if (bubble) { bubble.style.borderColor = 'rgba(239,68,68,0.3)'; bubble.style.background = 'rgba(239,68,68,0.08)'; }
            if (icon) { icon.className = 'fa-solid fa-circle-exclamation dlr-status-icon error'; icon.title = 'Failed to send'; }
        }

        // ── SMS Send via selected channel (GHL or Twilio) ──
        let _smsSending = false;
        async function dialerSendSms() {
            if (_smsSending || !_dlrSmsContactId) return;
            const textEl = document.getElementById('dlrSmsText');
            const statusEl = document.getElementById('dlrSmsStatus');
            const sendBtn = document.getElementById('dlrSmsSendBtn');
            const msgPanel = document.getElementById('dlrMessagesList');
            if (!textEl) return;
            const msg = textEl.value.trim();
            if (!msg) return;

            // Validate length
            if (msg.length > 1600) {
                if (statusEl) { statusEl.textContent = 'Message too long (max 1600 chars)'; statusEl.style.color = '#ef4444'; }
                return;
            }

            const channel = dlrGetChannel();
            const channelLabel = channel === 'twilio' ? 'InsuranceGrokBot' : 'LeadConnector';

            // Optimistic UI — append pending bubble immediately
            const pendingId = 'sms_' + Date.now();
            _dlrAppendSentBubble(msgPanel, msg, pendingId);

            // Clear composer
            textEl.value = '';
            textEl.style.height = '';
            dlrUpdateCharCount('');

            _smsSending = true;
            if (sendBtn) { sendBtn.disabled = true; sendBtn.style.opacity = '0.5'; }
            if (statusEl) { statusEl.textContent = 'Sending via ' + channelLabel + '…'; statusEl.style.color = '#888'; }

            try {
                const payload = { message: msg, channel: channel };
                // Include contact phone for Twilio channel
                if (channel === 'twilio' && _dlrSmsContactPhone) {
                    payload.contact_phone = _dlrSmsContactPhone;
                }
                const r = await _fetchRetry('/voice/contact/' + _dlrSmsContactId + '/send-sms', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                }, { retries: 1, timeout: 15000, label: 'send-sms' });
                const d = await r.json();
                if (r.ok) {
                    _dlrMarkBubbleSent(pendingId);
                    if (statusEl) { statusEl.textContent = '\u2713 Sent via ' + channelLabel; statusEl.style.color = '#007AFF'; }
                    setTimeout(() => { if (statusEl) statusEl.textContent = ''; }, 4000);
                } else {
                    _dlrMarkBubbleError(pendingId);
                    const err = d.error || 'Send failed';
                    if (statusEl) { statusEl.textContent = err; statusEl.style.color = '#ef4444'; }
                    console.error('[SMS] Send failed:', err);
                    // Restore text so agent can retry
                    textEl.value = msg;
                    dlrAutoGrow(textEl);
                    dlrUpdateCharCount(msg);
                }
            } catch(e) {
                _dlrMarkBubbleError(pendingId);
                if (statusEl) { statusEl.textContent = 'Network error \u2014 tap to retry'; statusEl.style.color = '#ef4444'; }
                textEl.value = msg;
                dlrAutoGrow(textEl);
                dlrUpdateCharCount(msg);
                console.error('[SMS] Network error:', e);
            } finally {
                _smsSending = false;
                if (sendBtn) { sendBtn.disabled = false; sendBtn.style.opacity = '1'; }
            }
        }

        // ── Contact actions from middle panel ──
        function dialerCallActiveContact() {
            if (!dialerActiveContact) return;
            const c = dialerActiveContact;
            dialerStartCall(c.phone, c.firstName || c.name, c.id, c.name);
        }
        function dialerAddActiveToQueue() {
            if (!dialerActiveContact) return;
            const c = dialerActiveContact;
            // Skip DnD / opted-out contacts — they must never enter the queue
            if (_igbIsOptedOut(_igbEngagementCache[c.id], c)) return;
            if (!dialerQueue.some(q => q.id === c.id)) {
                dialerQueue.push({ id: c.id, name: c.name, firstName: c.firstName, phone: c.phone, status: 'pending' });
                dialerRenderContacts();
                dialerRenderQueue();
            }
        }

        // ── Selection ──
        function _igbSelectAllInGroup(key) {
            const groups = _igbGroupContacts(dialerContacts);
            const g = groups.find(x => x.key === key);
            if (!g) return;
            const allSelected = g.contacts.every(c => dialerSelected.has(c.id));
            if (allSelected) {
                g.contacts.forEach(c => dialerSelected.delete(c.id));
            } else {
                g.contacts.forEach(c => { if (!_igbIsOptedOut(_igbEngagementCache[c.id], c)) dialerSelected.add(c.id); });
            }
            dialerUpdateSelectionUI();
            dialerRenderContacts();
        }
        function dialerToggleSelect(id) {
            if (dialerSelected.has(id)) dialerSelected.delete(id);
            else dialerSelected.add(id);
            dialerUpdateSelectionUI();
        }
        function dialerToggleSelectAll() {
            const cb = document.getElementById('dialerSelectAll');
            if (cb.checked) dialerContacts.forEach(c => dialerSelected.add(c.id));
            else dialerSelected.clear();
            dialerUpdateSelectionUI();
            dialerRenderContacts();
        }
        function dialerUpdateSelectionUI() {
            const cnt = dialerSelected.size;
            const selCount = document.getElementById('dialerSelectedCount');
            if (selCount) selCount.textContent = cnt > 0 ? '(' + cnt + ')' : '';
            const addBtn = document.getElementById('dialerAddSelectedBtn');
            if (addBtn) addBtn.disabled = cnt === 0;
            const callBtn = document.getElementById('dialerCallSelectedBtn');
            if (callBtn) callBtn.disabled = cnt === 0;
            const moveBtn = document.getElementById('dialerMoveSelectedBtn');
            if (moveBtn) moveBtn.disabled = cnt === 0;
            const sa = document.getElementById('dialerSelectAll');
            if (sa) sa.checked = dialerContacts.length > 0 && cnt === dialerContacts.length;
        }

        function dialerAddSelectedToQueue() {
            let skippedDnc = 0;
            dialerSelected.forEach(id => {
                if (!dialerQueue.some(q => q.id === id)) {
                    const c = dialerContacts.find(x => x.id === id);
                    if (!c) return;
                    // Skip DnD / opted-out contacts — they never enter the queue
                    if (_igbIsOptedOut(_igbEngagementCache[c.id], c)) { skippedDnc++; return; }
                    dialerQueue.push({ id: c.id, name: c.name, firstName: c.firstName, phone: c.phone, status: 'pending' });
                }
            });
            dialerSelected.clear();
            dialerRenderContacts();
            dialerRenderQueue();
            dialerUpdateSelectionUI();
            if (skippedDnc > 0) _showDashToast(true, skippedDnc + ' Do Not Contact lead' + (skippedDnc > 1 ? 's' : '') + ' skipped');
        }

        function dialerCallSelected() {
            dialerAddSelectedToQueue();
            if (dialerQueue.length > 0 && !dialerQueueRunning) dialerToggleQueue();
        }

        function dialerManualCall() {
            let ph = document.getElementById('dialerManualPhone').value.trim().replace(/[\s\-\(\)\.]/g, '');
            if (!ph) return;
            if (!ph.startsWith('+')) ph = '+1' + ph;
            dialerStartCall(ph, 'Manual', '', ph);
        }

        // ── Calling (AI mode) ──
        let _isDialing     = false; // blocks double-dial while /voice/dial request is in-flight
        let _hangupPending = false; // set when Hang Up is clicked while _isDialing; dialerStartCall will immediately cancel the new call

        async function dialerStartCall(phone, firstName, contactId, displayName) {
            // Guard: block if a call is already active OR if we're mid-dial-request.
            // dialerCallSid is only set after the API responds, so _isDialing covers
            // the window between button click and the server response.
            if (dialerCallSid || _isDialing) {
                console.warn('[Dialer] Blocked double-dial: call active or request in-flight');
                return;
            }
            _isDialing = true;
            // Pre-call AI minutes check: block if balance is 0 (AI mode only)
            if (dialerMode === 'ai' && _aiMinutesHasPurchased && _aiMinutesBalance !== null && _aiMinutesBalance <= 0) {
                _isDialing = false;
                _dialerShowOutOfMinutes();
                if (dialerQueueRunning) { dialerStopQueue(); }
                return;
            }
            // Validate phone before attempting
            if (!phone || phone.replace(/[^0-9+]/g, '').length < 10) {
                _isDialing = false;
                // Mark queue item as failed so advance() handles it correctly (retry or skip)
                if (dialerQueueRunning && dialerCallIdx >= 0 && dialerCallIdx < dialerQueue.length) {
                    dialerQueue[dialerCallIdx].status = 'failed';
                }
                dialerShowBanner(displayName || firstName, 'Invalid phone number', true);
                _dialerQueueTimeout(dialerHideBanner, 3000);
                if (dialerQueueRunning) _dialerQueueTimeout(dialerAdvance, 1500);
                return;
            }
            dialerShowBanner(displayName || firstName, 'Initiating...');
            try {
                // retries: 0 — dialing is NOT idempotent; a retry creates a duplicate call
                const r = await _fetchRetry('/voice/dial', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ phone, first_name: firstName, contact_id: contactId, dial_mode: dialerMode, dial_attempt: (dialerCallIdx >= 0 && dialerQueue[dialerCallIdx]) ? (dialerQueue[dialerCallIdx].attempts || 1) : 1, from_number: _dialerGetFromNumber() })
                }, { retries: 0, timeout: 25000, label: 'dial' });
                const d = await r.json();
                if (!r.ok) {
                    _isDialing = false;
                    _hangupPending = false;  // Clear stale hangup flag on dial failure
                    // Handle 402 AI minutes required — show modal and stop queue
                    if (r.status === 402 && d.minutes_required) {
                        _aiMinutesBalance = 0;
                        _dialerUpdateMinutesChip();
                        _dialerShowOutOfMinutes();
                        if (dialerQueueRunning) dialerStopQueue();
                        return;
                    }
                    // Mark queue item as failed so advance() handles it correctly (retry or skip)
                    if (dialerQueueRunning && dialerCallIdx >= 0 && dialerCallIdx < dialerQueue.length) {
                        dialerQueue[dialerCallIdx].status = 'failed';
                    }
                    dialerShowBanner(displayName, d.error || 'Failed to initiate call', true);
                    _dialerQueueTimeout(dialerHideBanner, 4000);
                    if (dialerQueueRunning) _dialerQueueTimeout(dialerAdvance, 2000);
                    return;
                }
                dialerCallSid = d.call_sid;
                _isDialing = false; // SID locked in — safe to clear the in-flight guard

                // Update banner with actual from number (server picks for auto rotation)
                if (d.from_number) {
                    const fromEl = document.getElementById('dialerCallFrom');
                    if (fromEl) {
                        const isAuto = !_dialerGetFromNumber();
                        fromEl.textContent = 'from ' + (isAuto ? 'auto \u2014 ' : '') + _dialerFmtPhone(d.from_number);
                    }
                }

                // Race: Hang Up was clicked while this API request was in-flight.
                // dialerCallSid was null then, so dialerStopQueue() set _hangupPending
                // instead. Now that we have the real SID, hang it up immediately.
                if (_hangupPending) {
                    _hangupPending = false;
                    const sidToKill = dialerCallSid;
                    dialerCallSid = null;
                    console.warn('[Dialer] Race hangup — Hang Up was clicked mid-dial, cancelling call', sidToKill);
                    _fetchRetry('/voice/hangup', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({ call_sid: sidToKill })
                    }, { retries: 2, timeout: 10000, label: 'hangup-race' })
                        .then(async r => {
                            try {
                                const rd = await r.json();
                                if (rd.success === false) {
                                    console.warn('[Dialer] Race hangup: Twilio says call may already have ended for', sidToKill);
                                } else {
                                    console.log('[Dialer] Race hangup confirmed for', sidToKill);
                                }
                            } catch(e) { console.log('[Dialer] Race hangup sent for', sidToKill); }
                        })
                        .catch(e => console.error('[Dialer] Race hangup request failed:', e.message));
                    dialerHideBanner();
                    dialerStopAiTimer();
                    return;
                }

                // Select contact in detail panel only after call is confirmed (SID locked in)
                if (contactId) dialerSelectContact(contactId);
                _dialerCallConnected = false;
                dialerShowBanner(displayName, 'Ringing...');
                dialerStartPoll();

                // ── Start ring tone for ALL dial modes ──
                _startRingTone();

                // ── Auto-speaker: play ring tone + prepare listen stream in AI mode ──
                if (dialerMode === 'ai') {
                    _autoListenActive = true;
                    _dialerListening = true;
                    _startRingTone();
                    // Pre-create listen AudioContext inside this user gesture chain
                    // to avoid autoplay policy blocks when the call connects
                    if (!_listenAudioCtx || _listenAudioCtx.state === 'closed') {
                        try {
                            _listenAudioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 8000 });
                            if (_listenAudioCtx.state === 'suspended') _listenAudioCtx.resume();
                        } catch(e) { /* non-fatal, _startListenStream will retry */ }
                    }
                    // Update Listen button to show active state
                    const lBtn = document.getElementById('dialerListenBtn');
                    if (lBtn) {
                        lBtn.style.background = 'rgba(0,217,255,0.15)';
                        lBtn.style.color = '#00d9ff';
                        const lIcon = lBtn.querySelector('i');
                        const lSpan = lBtn.querySelector('span');
                        if (lIcon) lIcon.className = 'fa-solid fa-ear-listen';
                        if (lSpan) lSpan.textContent = 'Listening...';
                    }
                    console.log('[Dialer] Auto-speaker: ring tone started');
                }
            } catch(e) {
                _isDialing = false;
                _hangupPending = false;  // Clear stale hangup flag on network failure
                console.error('[Dialer] startCall network error:', e);
                // Mark queue item as failed so advance() handles it correctly (retry or skip)
                if (dialerQueueRunning && dialerCallIdx >= 0 && dialerCallIdx < dialerQueue.length) {
                    dialerQueue[dialerCallIdx].status = 'failed';
                }
                dialerShowBanner(displayName, 'Network error — retrying...', true);
                _dialerQueueTimeout(dialerHideBanner, 4000);
                if (dialerQueueRunning) _dialerQueueTimeout(dialerAdvance, 3000);
            }
        }

        // Sets banner dot + border color based on state: 'ringing' | 'connected' | 'error' | 'ended'
        function _dialerBannerState(state) {
            const banner = document.getElementById('dialerCallBanner');
            const dot    = document.getElementById('dialerCallDot');
            if (!banner || !dot) return;
            const colors = {
                ringing:   { bg: 'rgba(0,217,255,0.06)', border: 'rgba(0,217,255,0.2)',   dot: '#00d9ff', anim: 'dialerPulse 1.5s infinite' },
                connected: { bg: 'rgba(74,222,128,0.06)', border: 'rgba(74,222,128,0.2)',   dot: 'var(--accent)', anim: 'dialerPulse 1.5s infinite' },
                error:     { bg: 'rgba(239,68,68,0.06)', border: 'rgba(239,68,68,0.2)',   dot: '#ef4444', anim: 'none' },
                ended:     { bg: 'rgba(74,222,128,0.04)', border: 'rgba(255,255,255,0.06)', dot: '#555',  anim: 'none' },
            };
            const c = colors[state] || colors.ringing;
            banner.style.background = c.bg;
            banner.style.border = '1px solid ' + c.border;
            dot.style.background = c.dot;
            dot.style.animation  = c.anim;
            // Keep Jump to Contact pill in sync with call state
            _jtcUpdatePill();
        }

        // Increment dial count badge after any call ends (completed, no-answer, busy, voicemail, etc.)
        function _dialerIncrementDialBadge() {
            const cid = (dialerCallIdx >= 0 && dialerCallIdx < dialerQueue.length)
                ? dialerQueue[dialerCallIdx].id
                : (dialerActiveContact ? dialerActiveContact.id : null);
            if (!cid) return;
            _dialerCallCounts[cid] = (_dialerCallCounts[cid] || 0) + 1;
            dialerUpdateContactBadge(cid, _dialerCallCounts[cid]);
            setTimeout(() => dialerFetchMergedCallCount(cid), 2000);
        }

        function dialerStartPoll() {
            if (dialerPollTimer) clearInterval(dialerPollTimer);
            let pollCount = 0, errorCount = 0;
            const POLL_INTERVAL = 1500; // ms between status checks
            const MAX_POLLS_RINGING = Math.ceil(_dialerRingTimeout / POLL_INTERVAL); // configurable ring timeout
            const MAX_ERRORS = 10;
            dialerPollTimer = setInterval(async () => {
                if (!dialerCallSid) { clearInterval(dialerPollTimer); dialerHideBanner(); dialerStopAiTimer(); _dialerClearCallDurationTimer(); _stopRingTone(); if (_autoListenActive) { _stopListenStream(); _resetListenBtn(); _autoListenActive = false; } return; }
                ++pollCount;
                // Only enforce poll limit while ringing/initiated — once connected, poll indefinitely
                if (pollCount > MAX_POLLS_RINGING && !_dialerCallConnected) {
                    clearInterval(dialerPollTimer); dialerPollTimer = null;
                    _dialerIncrementDialBadge();
                    // Mark as no-answer so retry logic picks it up
                    if (dialerCallIdx >= 0 && dialerCallIdx < dialerQueue.length) {
                        dialerQueue[dialerCallIdx].status = 'no-answer';
                    }
                    _dialerLastCallSid = dialerCallSid;
                    dialerCallSid = null;
                    dialerStopAiTimer(); _dialerClearCallDurationTimer();
                    _stopRingTone(); if (_autoListenActive) { _stopListenStream(); _resetListenBtn(); _autoListenActive = false; }
                    dialerRenderQueue();
                    if (!dialerQueueRunning) { dialerHideBanner(); dialerShowDisposition(); }
                    else { _dialerQueueTimeout(dialerAdvance, 1200); }
                    return;
                }
                try {
                    const r = await fetch('/voice/call-status/' + dialerCallSid);
                    if (!r.ok) {
                        // 404 means call no longer in Redis — treat as completed immediately
                        if (r.status === 404) {
                            console.log('[Dialer] Poll 404 — call no longer tracked, treating as completed');
                            clearInterval(dialerPollTimer); dialerPollTimer = null;
                            _dialerIncrementDialBadge();
                            dialerStopAiTimer(); _dialerClearCallDurationTimer();
                            _stopRingTone(); if (_autoListenActive) { _stopListenStream(); _resetListenBtn(); _autoListenActive = false; }
                            _dialerLastCallSid = dialerCallSid;
                            if (dialerCallIdx >= 0 && dialerCallIdx < dialerQueue.length) dialerQueue[dialerCallIdx].status = 'completed';
                            dialerCallSid = null; dialerRenderQueue();
                            if (!dialerQueueRunning) { dialerHideBanner(); dialerShowDisposition(); }
                            else { _dialerQueueTimeout(dialerAdvance, 1200); }
                            return;
                        }
                        if (++errorCount >= MAX_ERRORS) { clearInterval(dialerPollTimer); dialerCallSid = null; dialerHideBanner(); dialerStopAiTimer(); _dialerClearCallDurationTimer(); }
                        return;
                    }
                    errorCount = 0;
                    const d = await r.json();
                    const el = document.getElementById('dialerCallStatus');
                    // ── AMD / Voicemail detection (human mode) ──
                    // Server sets _amd_result when machine detected and auto-hangs up.
                    // Frontend must: stop poll, mark as no-answer for retry, advance queue.
                    if (d._amd_result && d.status !== 'ringing' && d.status !== 'initiated') {
                        console.log(`[Dialer] AMD detected: ${d._amd_result} — action: ${_dialerOnMachineAction}`);
                        clearInterval(dialerPollTimer);
                        dialerPollTimer = null;
                        _dialerIncrementDialBadge();
                        dialerStopAiTimer();
                        _dialerClearCallDurationTimer();
                        _stopRingTone();
                        if (_autoListenActive) { _stopListenStream(); _resetListenBtn(); _autoListenActive = false; }
                        el.textContent = 'Voicemail'; el.style.color = '#ffa500';
                        _dialerBannerState('ended');
                        _dialerLastCallSid = dialerCallSid;
                        // Mark queue item as no-answer so retry logic picks it up
                        if (dialerCallIdx >= 0 && dialerCallIdx < dialerQueue.length) {
                            dialerQueue[dialerCallIdx].status = 'no-answer';
                            if (_dialerAutoDispVoicemail) dialerQueue[dialerCallIdx].disposition = 'voicemail';
                        }
                        dialerCallSid = null;
                        dialerRenderQueue();
                        if (!dialerQueueRunning) {
                            dialerHideBanner();
                            dialerShowDisposition();
                        } else {
                            _dialerQueueTimeout(dialerAdvance, 1500);
                        }
                        return;
                    }
                    if (d.status === 'in-progress') {
                        _dialerCallConnected = true;
                        el.textContent = 'Connected'; el.style.color = 'var(--accent)';
                        _dialerBannerState('connected');
                        dialerStartAiTimer();
                        document.getElementById('dialerBannerTimer').style.display = 'block';
                        // Enable all call controls
                        _dialerEnableControls(true);
                        // ── Auto-speaker: stop ring tone, start real listen stream ──
                        _stopRingTone();
                        if (_autoListenActive && _dialerListening && !_listenConnected) {
                            _listenReconnects = 0;
                            _startListenStream();
                            console.log('[Dialer] Auto-speaker: ring tone stopped, live listen started');
                        }
                        // ── Max call duration enforcement ──
                        if (_dialerMaxCallDuration > 0 && !_dialerCallDurationTimer) {
                            _dialerCallDurationTimer = setTimeout(() => {
                                console.log('[Dialer] Max call duration reached (' + (_dialerMaxCallDuration / 60000) + ' min), ending call');
                                _dialerClearCallDurationTimer();
                                dialerStopQueue();
                            }, _dialerMaxCallDuration);
                        }
                        // Pre-warm VoIP device in background so Intercept is instant
                        if (voipSetupDone && !voipReady && !_voipInitializing) {
                            console.log('[VoIP] Pre-warming: initializing device in background for instant intercept');
                            initVoIPDevice().catch(e => console.warn('[VoIP] Pre-warm failed (non-fatal):', e));
                        }
                    } else if (d.status === 'ringing' || d.status === 'initiated') {
                        el.textContent = 'Ringing...'; el.style.color = '#00d9ff';
                        _dialerBannerState('ringing');
                    } else if (d.status === 'transferred') {
                        // Stop polling IMMEDIATELY to prevent duplicate advance calls
                        clearInterval(dialerPollTimer);
                        dialerPollTimer = null;
                        el.textContent = 'Transferred to Agent'; el.style.color = '#ffa500';
                        _dialerBannerState('connected');
                        // Keep banner visible briefly, then clean up and advance
                        _dialerClearCallDurationTimer();
                        // ── Auto-speaker cleanup ──
                        _stopRingTone();
                        if (_autoListenActive) { _stopListenStream(); _resetListenBtn(); _autoListenActive = false; }
                        _dialerQueueTimeout(() => {
                            dialerStopAiTimer();
                            _dialerLastCallSid = dialerCallSid;
                            dialerCallSid = null;
                            dialerHideBanner();
                            dialerRenderQueue();
                            if (dialerQueueRunning) dialerAdvance();
                        }, 2500);
                        return;
                    } else if (['completed','busy','no-answer','failed','canceled'].includes(d.status)) {
                        clearInterval(dialerPollTimer);
                        dialerStopAiTimer();
                        _dialerClearCallDurationTimer();
                        // Refresh KPI banner so today's numbers update after each call
                        setTimeout(dialerLoadKpiBanner, 2000);
                        // Refresh per-contact dial count badge in left column
                        _dialerIncrementDialBadge();
                        // Refresh AI minutes balance after AI call ends
                        if (dialerMode === 'ai' && _aiMinutesHasPurchased) {
                            setTimeout(() => {
                                _dialerFetchMinutes().then(() => {
                                    if (_aiMinutesBalance !== null && _aiMinutesBalance <= 0) {
                                        _dialerShowOutOfMinutes();
                                        if (dialerQueueRunning) dialerStopQueue();
                                    }
                                });
                            }, 2000);
                        }
                        // ── Auto-speaker cleanup ──
                        _stopRingTone();
                        if (_autoListenActive) { _stopListenStream(); _resetListenBtn(); _autoListenActive = false; }
                        _dialerLastCallSid = dialerCallSid;
                        // Short calls (<10s) that "completed" are likely voicemail that
                        // slipped through AMD — treat as no-answer so retry picks them up
                        let effectiveStatus = d.status;
                        if (d.status === 'completed' && parseInt(d.duration || 0) < 10) {
                            effectiveStatus = 'no-answer';
                            console.log(`[Dialer] Short call (${d.duration}s) — treating as no-answer for retry`);
                        }
                        if (dialerCallIdx >= 0 && dialerCallIdx < dialerQueue.length) dialerQueue[dialerCallIdx].status = effectiveStatus;
                        dialerCallSid = null;
                        dialerRenderQueue();
                        if (!dialerQueueRunning) {
                            // Not in queue mode — hide banner, show disposition
                            dialerHideBanner();
                            dialerShowDisposition();
                        } else {
                            // Queue mode: check if retry is coming — show status instead of hiding
                            const current = (dialerCallIdx >= 0 && dialerCallIdx < dialerQueue.length) ? dialerQueue[dialerCallIdx] : null;
                            const retryStatuses = ['no-answer', 'busy', 'failed', 'canceled'];
                            if (current && retryStatuses.includes(current.status) && (current.attempts || 0) < dialerMaxAttempts) {
                                // Retry coming — keep banner visible with retry message
                                const statusEl = document.getElementById('dialerCallStatus');
                                statusEl.textContent = 'Retrying in 3s...';
                                statusEl.style.color = '#ffa500';
                                _dialerBannerState('ended');
                            } else {
                                dialerHideBanner();
                            }
                            _dialerQueueTimeout(dialerAdvance, 1200);
                        }
                    }
                } catch(e) { console.error('[Dialer] Poll fetch error (errorCount=' + (errorCount+1) + '):', e.message || e); if (++errorCount >= MAX_ERRORS) { clearInterval(dialerPollTimer); dialerCallSid = null; dialerHideBanner(); dialerStopAiTimer(); _dialerClearCallDurationTimer(); } }
            }, 1500);
        }

        function dialerShowBanner(name, status, isErr) {
            const _tb = document.querySelector('.dlr-top-bar');
            if (_tb) _tb.style.display = 'none';
            document.getElementById('dialerCallName').textContent = name;
            const s = document.getElementById('dialerCallStatus');
            s.textContent = status;
            if (isErr) {
                s.style.color = '#ef4444';
                _dialerBannerState('error');
            } else {
                s.style.color = '#00d9ff';
                _dialerBannerState('ringing');
            }
            // Show which number we're calling from (updated after dial response)
            const fromEl = document.getElementById('dialerCallFrom');
            if (fromEl) {
                const manualNum = _dialerGetFromNumber();
                if (manualNum) {
                    fromEl.textContent = 'from ' + _dialerFmtPhone(manualNum);
                } else {
                    fromEl.textContent = 'from auto';
                }
                fromEl.style.display = '';
            }
            // Reset timer
            document.getElementById('dialerBannerTimer').style.display = 'none';
            document.getElementById('dialerBannerTimer').textContent = '00:00';
            // Controls are always visible but disabled until connected
            _dialerEnableControls(false);
            // Hide disposition if showing
            document.getElementById('dialerDisposition').style.display = 'none';
            document.getElementById('dialerCallBanner').style.display = 'block';
        }
        function dialerHideBanner() {
            document.getElementById('dialerCallBanner').style.display = 'none';
            const _tb2 = document.querySelector('.dlr-top-bar');
            if (_tb2) _tb2.style.display = '';
            dialerStopAiTimer();
            // Clear Jump to Contact state when call ends
            if (!dialerQueueRunning) { _jtcDialingContactId = null; _jtcUpdatePill(); }
            // Stop live listen if active
            _stopListenStream();
            _dialerListening = false;
            // Reset all control buttons for next call
            _dialerEnableControls(false);
            const tkBtn = document.getElementById('dialerTakeoverBtn');
            tkBtn.innerHTML = '<i class="fa-solid fa-hand"></i><span>Intercept</span>';
            const trBtn = document.getElementById('dialerTransferBtn');
            if (trBtn) trBtn.innerHTML = '<i class="fa-solid fa-arrow-right-arrow-left"></i><span>Transfer</span>';
            const muteBtn = document.getElementById('dialerMuteBtn');
            muteBtn.innerHTML = '<i class="fa-solid fa-volume-xmark"></i><span>Mute AI</span>';
            const muteMicBtn = document.getElementById('dialerMuteMicBtn');
            if (muteMicBtn) muteMicBtn.innerHTML = '<i class="fa-solid fa-microphone"></i><span>Mute Mic</span>';
            const listenBtn = document.getElementById('dialerListenBtn');
            listenBtn.innerHTML = '<i class="fa-solid fa-volume-high"></i><span>Listen</span>';
        }

        // ── AI Call Timer ──
        let _aiTimerInterval = null, _aiTimerSeconds = 0;
        function dialerStartAiTimer() {
            if (_aiTimerInterval) return; // already running
            _aiTimerSeconds = 0;
            const timerEl = document.getElementById('dialerBannerTimer');
            const headerTimer = document.getElementById('dialerAiTimer');
            timerEl.style.display = 'block';
            headerTimer.style.display = 'block';
            _aiTimerInterval = setInterval(() => {
                _aiTimerSeconds++;
                const m = Math.floor(_aiTimerSeconds/60);
                const s = _aiTimerSeconds%60;
                const txt = String(m).padStart(2,'0') + ':' + String(s).padStart(2,'0');
                timerEl.textContent = txt;
                headerTimer.textContent = txt;
            }, 1000);
        }
        function dialerStopAiTimer() {
            if (_aiTimerInterval) { clearInterval(_aiTimerInterval); _aiTimerInterval = null; }
            document.getElementById('dialerAiTimer').style.display = 'none';
        }

        // ── In-call toggle state — declared before _dialerEnableControls so it can reset them ──
        let _dialerListening = false;   // live listen WebSocket active
        let _dialerMuted     = false;   // AI audio muted (listen speaker)
        let _autoListenActive = false;  // auto-speaker mode: listen starts automatically in AI mode

        // ── Ringback tone generator (US standard: 440+480 Hz, 2s on / 4s off) ──
        let _ringAudioCtx = null;
        let _ringOsc1 = null;
        let _ringOsc2 = null;
        let _ringGain = null;
        let _ringTimer = null;

        function _startRingTone() {
            _stopRingTone();
            try {
                _ringAudioCtx = new (window.AudioContext || window.webkitAudioContext)();
                _ringGain = _ringAudioCtx.createGain();
                _ringGain.connect(_ringAudioCtx.destination);
                _ringOsc1 = _ringAudioCtx.createOscillator();
                _ringOsc2 = _ringAudioCtx.createOscillator();
                _ringOsc1.frequency.value = 440;
                _ringOsc2.frequency.value = 480;
                const mix1 = _ringAudioCtx.createGain();
                const mix2 = _ringAudioCtx.createGain();
                mix1.gain.value = 0.15;             // moderate volume
                mix2.gain.value = 0.15;
                _ringOsc1.connect(mix1); mix1.connect(_ringGain);
                _ringOsc2.connect(mix2); mix2.connect(_ringGain);
                _ringOsc1.start(); _ringOsc2.start();
                // US ringback cadence: 2s on, 4s off, repeating
                let phase = 0; // 0=on, 1=off
                function tick() {
                    if (!_ringGain) return;
                    if (phase === 0) {
                        _ringGain.gain.value = 1;
                        _ringTimer = setTimeout(() => { phase = 1; tick(); }, 2000);
                    } else {
                        _ringGain.gain.value = 0;
                        _ringTimer = setTimeout(() => { phase = 0; tick(); }, 4000);
                    }
                }
                tick();
            } catch(e) { console.warn('[Ring] Failed to start ring tone:', e); }
        }

        function _stopRingTone() {
            if (_ringTimer) { clearTimeout(_ringTimer); _ringTimer = null; }
            if (_ringOsc1)     { try { _ringOsc1.stop(); } catch(e) {} _ringOsc1 = null; }
            if (_ringOsc2)     { try { _ringOsc2.stop(); } catch(e) {} _ringOsc2 = null; }
            _ringGain = null;
            if (_ringAudioCtx) { try { _ringAudioCtx.close(); } catch(e) {} _ringAudioCtx = null; }
        }
        let _dialerMicMuted  = false;   // agent mic muted (post-intercept VoIP)

        // ── Enable / disable all in-call control buttons ──
        function _dialerEnableControls(enabled) {
            const btns = ['dialerListenBtn', 'dialerMuteBtn', 'dialerMuteMicBtn', 'dialerTakeoverBtn', 'dialerTransferBtn', 'dialerMeetBtn'];
            btns.forEach(id => {
                const btn = document.getElementById(id);
                if (!btn) return;
                btn.disabled = !enabled;
                btn.style.cursor = enabled ? 'pointer' : 'not-allowed';
            });
            if (enabled) {
                const listen = document.getElementById('dialerListenBtn');
                const mute = document.getElementById('dialerMuteBtn');
                const muteMic = document.getElementById('dialerMuteMicBtn');
                const takeover = document.getElementById('dialerTakeoverBtn');
                const transfer = document.getElementById('dialerTransferBtn');
                if (_autoListenActive) {
                    listen.style.color = '#00d9ff'; listen.style.background = 'rgba(0,217,255,0.15)'; listen.style.borderColor = 'rgba(0,217,255,0.3)';
                    const li = listen.querySelector('i'); const ls = listen.querySelector('span');
                    if (li) li.className = 'fa-solid fa-ear-listen'; if (ls) ls.textContent = 'Listening...';
                } else {
                    listen.style.color = '#ccc'; listen.style.background = 'rgba(255,255,255,0.04)'; listen.style.borderColor = 'rgba(255,255,255,0.1)';
                }
                mute.style.color = '#ccc'; mute.style.background = 'rgba(255,255,255,0.04)'; mute.style.borderColor = 'rgba(255,255,255,0.1)';
                if (muteMic) { muteMic.style.color = '#ccc'; muteMic.style.background = 'rgba(255,255,255,0.04)'; muteMic.style.borderColor = 'rgba(255,255,255,0.1)'; }
                takeover.style.color = '#ffa500'; takeover.style.background = 'rgba(255,165,0,0.12)'; takeover.style.borderColor = 'rgba(255,165,0,0.3)';
                transfer.style.color = '#00d9ff'; transfer.style.background = 'rgba(0,217,255,0.08)'; transfer.style.borderColor = 'rgba(0,217,255,0.15)';
                const meetBtn = document.getElementById('dialerMeetBtn');
                if (meetBtn) { meetBtn.style.color = '#ccc'; meetBtn.style.background = 'rgba(255,255,255,0.04)'; meetBtn.style.borderColor = 'rgba(255,255,255,0.1)'; }
            } else {
                // Grayed out / disabled look
                const allBtns = ['dialerListenBtn', 'dialerMuteBtn', 'dialerMuteMicBtn', 'dialerTakeoverBtn', 'dialerTransferBtn', 'dialerMeetBtn'];
                allBtns.forEach(id => {
                    const btn = document.getElementById(id);
                    if (!btn) return;
                    btn.style.color = '#444'; btn.style.background = 'rgba(255,255,255,0.03)'; btn.style.borderColor = 'rgba(255,255,255,0.06)';
                });
            }
            // Reset toggle states (preserve auto-listen if active)
            if (!_autoListenActive) _dialerListening = false;
            _dialerMuted = false;
            _dialerMicMuted = false;
        }

        // ── Live Listen (AI speakerphone — streams call audio to browser) ──
        let _listenWs = null;
        let _listenAudioCtx = null;
        let _listenReconnects = 0;
        let _listenReconnectTimer = null;  // prevent duplicate reconnect timers
        const _LISTEN_MAX_RECONNECTS = 5;

        async function dialerToggleListen() {
            if (document.getElementById('dialerListenBtn').disabled) return;
            _dialerListening = !_dialerListening;
            const btn = document.getElementById('dialerListenBtn');

            if (_dialerListening) {
                btn.style.background = 'rgba(0,217,255,0.15)';
                btn.style.color = '#00d9ff';
                btn.querySelector('i').className = 'fa-solid fa-ear-listen';
                btn.querySelector('span').textContent = 'Listening...';
                _listenReconnects = 0;
                await _startListenStream();
            } else {
                _stopRingTone();    // stop ring tone if still playing
                _stopListenStream();
                _autoListenActive = false;
                btn.style.background = 'rgba(255,255,255,0.04)';
                btn.style.color = '#ccc';
                btn.querySelector('i').className = 'fa-solid fa-volume-high';
                btn.querySelector('span').textContent = 'Listen';
            }
        }

        let _listenNextTime = 0; // scheduled playback time for gapless audio
        let _listenConnected = false; // tracks whether WS confirmed listening

        async function _startListenStream() {
            if (!dialerCallSid) { _dialerListening = false; _resetListenBtn(); return; }

            // Capture call SID immediately — BEFORE any awaits — so the poll timer
            // can't clear dialerCallSid underneath us during AudioContext.resume()
            const listenCallSid = dialerCallSid;

            // Clean up any previous WebSocket connection
            if (_listenWs) { try { _listenWs.close(); } catch(e) {} _listenWs = null; }
            _listenConnected = false;

            // Reuse existing AudioContext if available (e.g. pre-created during auto-speaker),
            // otherwise create a new one — must happen inside user gesture context
            // to bypass browser autoplay policy (Chrome/Safari block otherwise)
            if (!_listenAudioCtx || _listenAudioCtx.state === 'closed') {
                if (_listenAudioCtx) { try { _listenAudioCtx.close(); } catch(e) {} }
                try {
                    _listenAudioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 8000 });
                } catch(e) {
                    console.error('[Listen] AudioContext creation failed:', e);
                    _resetListenBtn();
                    return;
                }
            }
            if (_listenAudioCtx.state === 'suspended') {
                try { await _listenAudioCtx.resume(); console.log('[Listen] AudioContext resumed'); } catch(e) {}
            }
            _listenNextTime = 0;

            try {
                const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
                const voiceHost = window.DASHBOARD_BOOT?.voiceWssHost || location.host;
                _listenWs = new WebSocket(`${proto}//${voiceHost}/voice/listen-stream`);
            } catch(e) {
                console.error('[Listen] WebSocket creation failed:', e);
                if (_listenAudioCtx) { try { _listenAudioCtx.close(); } catch(x) {} _listenAudioCtx = null; }
                _resetListenBtn();
                return;
            }

            // Connection timeout: if WS doesn't confirm within 8s, something is wrong
            const connectTimeout = setTimeout(() => {
                if (!_listenConnected && _dialerListening) {
                    console.error('[Listen] Connection timeout — no listening confirmation after 8s');
                    _stopListenStream();
                    _resetListenBtn();
                }
            }, 8000);

            const ws = _listenWs;  // capture reference for stale socket guards

            ws.onopen = () => {
                if (ws !== _listenWs) return;  // stale socket — ignore
                console.log('[Listen] WebSocket connected, subscribing to', listenCallSid);
                if (!listenCallSid) {
                    console.error('[Listen] No call SID available — closing');
                    ws.close();
                    return;
                }
                ws.send(JSON.stringify({ call_sid: listenCallSid }));
            };

            ws.onmessage = (evt) => {
                if (ws !== _listenWs) return;  // stale socket — ignore
                try {
                    const msg = JSON.parse(evt.data);
                    if (msg.status === 'listening') {
                        _listenConnected = true;
                        clearTimeout(connectTimeout);
                        _listenReconnects = 0; // reset on successful connect
                        console.log('[Listen] Server confirmed listening');
                    } else if (msg.audio) {
                        _playMulawChunk(msg.audio);
                    } else if (msg.status === 'call_ended') {
                        console.log('[Listen] Call ended');
                        clearTimeout(connectTimeout);
                        _stopListenStream();
                        _resetListenBtn();
                    } else if (msg.error) {
                        console.error('[Listen] Server error:', msg.error);
                        clearTimeout(connectTimeout);
                        _stopListenStream();
                        _resetListenBtn();
                    }
                    // keepalive messages are silently ignored
                } catch(e) { console.error('[Listen] Message parse error:', e); }
            };

            ws.onclose = () => {
                clearTimeout(connectTimeout);
                if (ws === _listenWs) _listenWs = null;  // clear immediately
                console.log('[Listen] WebSocket closed');
                // Auto-reconnect with limit (prevent duplicate timers)
                if (_listenReconnectTimer) { clearTimeout(_listenReconnectTimer); _listenReconnectTimer = null; }
                if (_dialerListening && dialerCallSid && _listenReconnects < _LISTEN_MAX_RECONNECTS) {
                    _listenReconnects++;
                    const delay = Math.min(1500 * _listenReconnects, 5000);
                    console.log(`[Listen] Auto-reconnecting in ${delay}ms (attempt ${_listenReconnects}/${_LISTEN_MAX_RECONNECTS})...`);
                    _listenReconnectTimer = setTimeout(() => {
                        _listenReconnectTimer = null;
                        if (_dialerListening && dialerCallSid) {
                            _startListenStream();
                        } else {
                            _resetListenBtn();
                        }
                    }, delay);
                } else {
                    if (_listenReconnects >= _LISTEN_MAX_RECONNECTS) {
                        console.error('[Listen] Max reconnect attempts reached');
                    }
                    _resetListenBtn();
                }
            };

            ws.onerror = (err) => {
                if (ws !== _listenWs) return;  // stale socket — ignore
                console.error('[Listen] WebSocket error:', err);
            };
        }

        function _stopListenStream() {
            _listenConnected = false;
            if (_listenReconnectTimer) { clearTimeout(_listenReconnectTimer); _listenReconnectTimer = null; }
            if (_listenWs) { try { _listenWs.onclose = null; _listenWs.close(); } catch(e) {} _listenWs = null; }
            if (_listenAudioCtx) { try { _listenAudioCtx.close(); } catch(e) {} _listenAudioCtx = null; }
            _listenNextTime = 0;
        }

        function _resetListenBtn() {
            _dialerListening = false;
            _listenConnected = false;
            const btn = document.getElementById('dialerListenBtn');
            if (btn) {
                btn.style.background = 'rgba(255,255,255,0.04)';
                btn.style.color = '#ccc';
                btn.querySelector('i').className = 'fa-solid fa-volume-high';
                btn.querySelector('span').textContent = 'Listen';
            }
        }

        // Decode mulaw base64 → PCM and play via Web Audio API
        function _playMulawChunk(b64) {
            if (!_listenAudioCtx || _listenAudioCtx.state === 'closed') return;
            if (_dialerMuted) return; // Muted: discard chunk (do NOT resume — that would undo the mute)
            // Resume only for initial autoplay-policy unlock (not during intentional mute)
            if (_listenAudioCtx.state === 'suspended') { _listenAudioCtx.resume(); }
            try {
                const raw = atob(b64);
                const mulaw = new Uint8Array(raw.length);
                for (let i = 0; i < raw.length; i++) mulaw[i] = raw.charCodeAt(i);

                // Decode mulaw to float PCM
                const pcm = new Float32Array(mulaw.length);
                for (let i = 0; i < mulaw.length; i++) {
                    pcm[i] = _decodeMulaw(mulaw[i]);
                }

                const buf = _listenAudioCtx.createBuffer(1, pcm.length, 8000);
                buf.getChannelData(0).set(pcm);
                const src = _listenAudioCtx.createBufferSource();
                src.buffer = buf;
                src.connect(_listenAudioCtx.destination);

                // Schedule chunks sequentially for gapless playback
                const now = _listenAudioCtx.currentTime;
                const startAt = Math.max(now, _listenNextTime);
                src.start(startAt);
                _listenNextTime = startAt + buf.duration;
            } catch(e) { console.error('[Listen] Playback error:', e); }
        }

        // ITU-T G.711 mulaw decoder (standard formula)
        function _decodeMulaw(sample) {
            sample = ~sample & 0xFF;
            const sign = (sample & 0x80) ? -1 : 1;
            const exponent = (sample >> 4) & 0x07;
            const mantissa = sample & 0x0F;
            const magnitude = ((mantissa << 3) + 0x84) << exponent;
            return sign * (magnitude - 0x84) / 32768.0;
        }

        // ── Mute AI: mutes call audio you hear (listen speaker) ──
        function dialerToggleMuteAI() {
            if (document.getElementById('dialerMuteBtn').disabled) return;
            _dialerMuted = !_dialerMuted;
            const btn = document.getElementById('dialerMuteBtn');
            btn.style.background = _dialerMuted ? 'rgba(239,68,68,0.15)' : 'rgba(255,255,255,0.04)';
            btn.style.color = _dialerMuted ? '#ef4444' : '#ccc';
            btn.querySelector('i').className = _dialerMuted ? 'fa-solid fa-volume-xmark' : 'fa-solid fa-volume-high';
            btn.querySelector('span').textContent = _dialerMuted ? 'Unmute AI' : 'Mute AI';

            // Muting is handled by the _dialerMuted flag in _playMulawChunk — chunks are
            // discarded rather than using AudioContext.suspend(), which is unreliable:
            // suspend() gets overridden by _playMulawChunk's autoplay-unlock resume() call.
            // When unmuting, reset the scheduled playback time so audio starts clean from
            // "now" rather than playing a backlog of buffered-but-muted chunks.
            if (!_dialerMuted) {
                _listenNextTime = 0;
            }
        }

        // ── Mute Mic: mutes your microphone (only works after VoIP intercept) ──
        function dialerToggleMuteMic() {
            const btn = document.getElementById('dialerMuteMicBtn');
            if (!btn || btn.disabled) return;
            if (!voipConnection) {
                // No VoIP connection yet — show brief tooltip
                btn.querySelector('span').textContent = 'No mic active';
                btn.style.color = '#888';
                setTimeout(() => { btn.querySelector('span').textContent = 'Mute Mic'; btn.style.color = '#ccc'; }, 1500);
                return;
            }
            _dialerMicMuted = !_dialerMicMuted;
            voipConnection.mute(_dialerMicMuted);
            btn.style.background = _dialerMicMuted ? 'rgba(239,68,68,0.15)' : 'rgba(255,255,255,0.04)';
            btn.style.color = _dialerMicMuted ? '#ef4444' : '#ccc';
            btn.querySelector('i').className = _dialerMicMuted ? 'fa-solid fa-microphone-slash' : 'fa-solid fa-microphone';
            btn.querySelector('span').textContent = _dialerMicMuted ? 'Unmute Mic' : 'Mute Mic';
        }

        // ── Takeover / Human Intercept: agent barges into AI call ──
        // Flag signals VoIP incoming handler to auto-accept this intercept call
        let _takingOver = false;
        async function dialerTakeover() {
            if (!dialerCallSid || document.getElementById('dialerTakeoverBtn').disabled) return;
            // Capture call SID NOW — before any awaits that could let the poll clear it
            const takeoverCallSid = dialerCallSid;
            const btn = document.getElementById('dialerTakeoverBtn');
            btn.disabled = true;
            btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i><span>Intercepting...</span>';
            const statusEl = document.getElementById('dialerCallStatus');
            if (statusEl) { statusEl.textContent = 'Intercepting...'; statusEl.style.color = '#ffa500'; }

            // Immediately stop listen stream to prevent echo/feedback during handover
            if (_dialerListening) {
                _stopRingTone();
                _stopListenStream();
                _resetListenBtn();
                _autoListenActive = false;
            }

            try {
                // Always use VoIP for browser intercept — initialize device if needed
                let useVoip = false;
                if (voipReady && voipDevice) {
                    useVoip = true;
                } else if (voipSetupDone && !_voipInitializing) {
                    // VoIP is configured but not yet connected — init it now
                    if (statusEl) statusEl.textContent = 'Connecting VoIP...';
                    await initVoIPDevice();
                    // Wait up to 6s for registration
                    for (let i = 0; i < 12; i++) {
                        if (voipReady) break;
                        await new Promise(res => setTimeout(res, 500));
                    }
                    useVoip = !!voipReady;
                } else if (!voipSetupDone) {
                    // VoIP not configured — fall back to phone transfer
                    useVoip = false;
                }

                // Set flag so incoming handler auto-accepts this specific intercept call
                if (useVoip) _takingOver = true;
                console.log('[Dialer] Intercept: use_voip=' + useVoip + ' voipReady=' + voipReady + ' callSid=' + takeoverCallSid);

                const r = await _fetchRetry('/voice/takeover', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ call_sid: takeoverCallSid, use_voip: useVoip })
                }, { retries: 0, timeout: 15000, label: 'takeover' });
                const d = await r.json();
                if (r.ok) {
                    const isStopped = d.status === 'stopped';
                    btn.innerHTML = isStopped
                        ? '<i class="fa-solid fa-stop"></i><span>AI Stopped</span>'
                        : '<i class="fa-solid fa-check"></i><span>Intercepted</span>';
                    btn.style.color = isStopped ? '#ffa500' : 'var(--accent)';
                    btn.style.borderColor = isStopped ? 'rgba(255,165,0,0.3)' : 'rgba(74,222,128,0.3)';
                    btn.style.background = isStopped ? 'rgba(255,165,0,0.08)' : 'rgba(74,222,128,0.08)';
                    if (statusEl) {
                        if (isStopped) {
                            statusEl.textContent = 'AI stopped — call ended';
                            statusEl.style.color = '#ffa500';
                        } else {
                            statusEl.textContent = useVoip ? 'You are now on the call (browser)' : 'Call transferred to your phone';
                            statusEl.style.color = 'var(--accent)';
                        }
                    }
                    // Stop listen stream on successful intercept (agent is now on the call)
                    if (_dialerListening) { _stopListenStream(); _resetListenBtn(); _autoListenActive = false; }
                    // Enable mic mute button now (only for live intercept, not stop)
                    if (!isStopped) {
                        const muteMicBtn = document.getElementById('dialerMuteMicBtn');
                        if (muteMicBtn) { muteMicBtn.disabled = false; muteMicBtn.style.cursor = 'pointer'; }
                    }
                } else {
                    _takingOver = false;
                    const errMsg = d.error || 'Intercept failed';
                    // Detect "call already ended" from server (400 + Twilio 21220 or status message)
                    const callEnded = r.status === 400 && /already ended|not in-progress|21220/i.test(errMsg);
                    if (callEnded) {
                        btn.disabled = true;
                        btn.innerHTML = '<i class="fa-solid fa-phone-slash"></i><span>Call Ended</span>';
                        btn.style.color = '#888';
                        btn.style.borderColor = 'rgba(255,255,255,0.08)';
                        btn.style.background = 'rgba(255,255,255,0.03)';
                        if (statusEl) { statusEl.textContent = errMsg; statusEl.style.color = '#888'; }
                    } else {
                        btn.disabled = false;
                        btn.innerHTML = '<i class="fa-solid fa-hand"></i><span>Intercept</span>';
                        if (statusEl) { statusEl.textContent = errMsg; statusEl.style.color = '#ef4444'; }
                    }
                    console.error('[Intercept] Failed:', errMsg);
                }
            } catch(e) {
                _takingOver = false;
                btn.disabled = false;
                btn.innerHTML = '<i class="fa-solid fa-hand"></i><span>Intercept</span>';
                if (statusEl) { statusEl.textContent = 'Network error — try again'; statusEl.style.color = '#ef4444'; }
                console.error('[Intercept] Network error:', e);
            }
        }

        // ── Google Meet — Start video call during live call ──
        async function dialerStartMeet() {
            if (!dialerCallSid) return;
            const contact = dialerActiveContact;
            const phone = contact ? contact.phone : '';
            const name = contact ? (contact.firstName || contact.name || '') : '';
            const email = contact ? (contact.email || '') : '';
            const btn = document.getElementById('dialerMeetBtn');
            if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i><span>Meet</span>'; }
            try {
                const r = await fetch('/google-calendar/meet/start-now', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ contact_name: name, attendee_email: email, attendee_phone: phone })
                });
                const d = await r.json();
                if (!r.ok) {
                    _showDashToast(false, d.error || 'Failed to create Meet');
                    if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-video"></i><span>Meet</span>'; }
                    return;
                }
                if (d.meet_link) {
                    window.open(d.meet_link, '_blank');
                    if (d.sms_sent) {
                        _showDashToast(true, 'Google Meet started — link sent via SMS. Use Present to share your screen.');
                    } else if (email) {
                        _showDashToast(true, 'Google Meet started — invite sent to ' + email + '. Use Present to share your screen.');
                    } else {
                        _showDashToast(false, 'Meet started — share link with lead: ' + d.meet_link);
                    }
                }
            } catch(e) {
                _showDashToast(false, 'Network error creating Meet');
            }
            if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-video"></i><span>Meet</span>'; }
        }

        // ── Warm Transfer (Conference-based enterprise transfer) ──
        let _warmXferState = 'idle'; // idle | consulting | completed
        let _warmXferTimerInterval = null;
        let _warmXferTimerStart = 0;

        function _toggleWarmTransferPopover() {
            const pop = document.getElementById('warmXferPopover');
            if (!pop) return;
            if (pop.style.display === 'block') {
                _closeWarmXferPopover();
            } else {
                _openWarmXferPopover();
            }
        }

        function _openWarmXferPopover() {
            const pop = document.getElementById('warmXferPopover');
            if (!pop || !dialerCallSid) return;

            // Load preset numbers from voice config (DASHBOARD_BOOT)
            const presets = window.DASHBOARD_BOOT?.transfer_numbers || [];
            const primaryNum = (document.getElementById('voiceTransferNumber')?.value || '').trim();
            const presetsEl = document.getElementById('warmXferPresets');
            const dividerEl = document.getElementById('warmXferDivider');
            presetsEl.innerHTML = '';

            // Add primary transfer number as first preset if set
            const allPresets = [];
            if (primaryNum) allPresets.push({ label: 'Primary', number: primaryNum });
            presets.forEach(p => { if (p.number && p.number !== primaryNum) allPresets.push(p); });

            allPresets.forEach(p => {
                const chip = document.createElement('div');
                chip.className = 'warm-xfer-chip';
                chip.innerHTML = '<i class="warm-xfer-chip-icon fa-solid fa-phone"></i>'
                    + '<span class="warm-xfer-chip-label">' + (p.label || 'Transfer') + '</span>'
                    + '<span class="warm-xfer-chip-number">' + p.number + '</span>';
                chip.onclick = () => { document.getElementById('warmXferInput').value = p.number; _warmXferDial(); };
                presetsEl.appendChild(chip);
            });

            dividerEl.style.display = allPresets.length > 0 ? '' : 'none';

            // Reset to dial state
            document.getElementById('warmXferDialState').style.display = '';
            document.getElementById('warmXferConsultState').style.display = 'none';
            document.getElementById('warmXferInput').value = '';
            _warmXferState = 'idle';

            pop.style.display = 'block';
            document.getElementById('warmXferInput').focus();
        }

        function _closeWarmXferPopover() {
            const pop = document.getElementById('warmXferPopover');
            if (pop) pop.style.display = 'none';
            if (_warmXferTimerInterval) { clearInterval(_warmXferTimerInterval); _warmXferTimerInterval = null; }
        }

        function _warmXferKey(digit) {
            const input = document.getElementById('warmXferInput');
            if (input) input.value += digit;
        }

        function _warmXferBackspace() {
            const input = document.getElementById('warmXferInput');
            if (input && input.value.length > 0) input.value = input.value.slice(0, -1);
        }

        async function _warmXferDial() {
            if (!dialerCallSid) return;
            const input = document.getElementById('warmXferInput');
            let target = (input?.value || '').trim();
            if (!target) return;

            let clean = target.replace(/[\s\-\(\)\.]/g, '');
            if (!clean.startsWith('+')) clean = '+1' + clean.replace(/^1/, '');
            if (clean.replace(/[^0-9]/g, '').length < 10) {
                _showDashToast(false, 'Invalid number — must be at least 10 digits');
                return;
            }

            const dialBtn = document.getElementById('warmXferDialBtn');
            dialBtn.disabled = true;
            dialBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Dialing...';

            try {
                const r = await _fetchRetry('/voice/warm-transfer/initiate', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ call_sid: dialerCallSid, transfer_to: clean })
                }, { retries: 0, timeout: 20000, label: 'warm-transfer-initiate' });
                const d = await r.json();

                if (r.ok) {
                    _warmXferState = 'consulting';
                    // Switch to consulting UI
                    document.getElementById('warmXferDialState').style.display = 'none';
                    document.getElementById('warmXferConsultState').style.display = '';
                    document.getElementById('warmXferConsultTarget').textContent = clean;

                    // Start timer
                    _warmXferTimerStart = Date.now();
                    if (_warmXferTimerInterval) clearInterval(_warmXferTimerInterval);
                    _warmXferTimerInterval = setInterval(() => {
                        const elapsed = Math.floor((Date.now() - _warmXferTimerStart) / 1000);
                        const m = Math.floor(elapsed / 60);
                        const s = elapsed % 60;
                        const el = document.getElementById('warmXferConsultTimer');
                        if (el) el.textContent = m + ':' + String(s).padStart(2, '0');
                    }, 1000);

                    // Update transfer button
                    const btn = document.getElementById('dialerTransferBtn');
                    if (btn) {
                        btn.innerHTML = '<i class="fa-solid fa-arrow-right-arrow-left"></i><span>Consulting</span>';
                    }
                    document.getElementById('dialerCallStatus').textContent = 'Warm Transfer';
                } else {
                    _showDashToast(false, d.error || 'Transfer failed');
                    dialBtn.disabled = false;
                    dialBtn.innerHTML = '<i class="fa-solid fa-phone"></i> Dial';
                }
            } catch(e) {
                console.error('[WarmXfer] Initiate error:', e);
                _showDashToast(false, 'Network error during transfer');
                dialBtn.disabled = false;
                dialBtn.innerHTML = '<i class="fa-solid fa-phone"></i> Dial';
            }
        }

        async function _warmXferComplete() {
            if (!dialerCallSid || _warmXferState !== 'consulting') return;
            const btn = document.getElementById('warmXferCompleteBtn');
            btn.disabled = true;
            btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Completing...';

            try {
                const r = await _fetchRetry('/voice/warm-transfer/complete', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ call_sid: dialerCallSid })
                }, { retries: 0, timeout: 15000, label: 'warm-transfer-complete' });
                const d = await r.json();

                if (r.ok) {
                    _warmXferState = 'completed';
                    _showDashToast(true, 'Transfer completed — caller connected to target');
                    _closeWarmXferPopover();
                    const xferBtn = document.getElementById('dialerTransferBtn');
                    if (xferBtn) {
                        xferBtn.innerHTML = '<i class="fa-solid fa-check"></i><span>Transferred</span>';
                        xferBtn.disabled = true;
                    }
                    document.getElementById('dialerCallStatus').textContent = 'Transferred';
                } else {
                    _showDashToast(false, d.error || 'Complete transfer failed');
                    btn.disabled = false;
                    btn.innerHTML = '<i class="fa-solid fa-check"></i> Complete Transfer';
                }
            } catch(e) {
                console.error('[WarmXfer] Complete error:', e);
                _showDashToast(false, 'Network error');
                btn.disabled = false;
                btn.innerHTML = '<i class="fa-solid fa-check"></i> Complete Transfer';
            }
        }

        async function _warmXferCancel() {
            if (!dialerCallSid || _warmXferState !== 'consulting') return;
            const btn = document.getElementById('warmXferCancelBtn');
            btn.disabled = true;
            btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Canceling...';

            try {
                const r = await _fetchRetry('/voice/warm-transfer/cancel', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ call_sid: dialerCallSid })
                }, { retries: 0, timeout: 15000, label: 'warm-transfer-cancel' });
                const d = await r.json();

                if (r.ok) {
                    _warmXferState = 'idle';
                    _showDashToast(true, 'Transfer canceled — reconnected to caller');
                    _closeWarmXferPopover();
                    const xferBtn = document.getElementById('dialerTransferBtn');
                    if (xferBtn) {
                        xferBtn.innerHTML = '<i class="fa-solid fa-arrow-right-arrow-left"></i><span>Transfer</span>';
                        xferBtn.disabled = false;
                    }
                    document.getElementById('dialerCallStatus').textContent = 'Connected';
                } else {
                    _showDashToast(false, d.error || 'Cancel failed');
                    btn.disabled = false;
                    btn.innerHTML = '<i class="fa-solid fa-xmark"></i> Cancel';
                }
            } catch(e) {
                console.error('[WarmXfer] Cancel error:', e);
                _showDashToast(false, 'Network error');
                btn.disabled = false;
                btn.innerHTML = '<i class="fa-solid fa-xmark"></i> Cancel';
            }
        }

        // Legacy alias for any remaining references
        function dialerTransfer() { _toggleWarmTransferPopover(); }

        // ── Call Disposition ──
        let _dialerLastCallSid = null;
        function dialerShowDisposition() {
            document.getElementById('dialerDisposition').style.display = 'block';
        }
        function dialerDismissDisposition() {
            document.getElementById('dialerDisposition').style.display = 'none';
        }
        function dialerSetDisposition(disp) {
            // Highlight immediately (before any async work — window.event is only valid synchronously)
            const btns = document.querySelectorAll('#dialerDisposition .disp-btn');
            btns.forEach(b => { b.style.background = 'rgba(255,255,255,0.04)'; b.style.color = '#ccc'; });
            const clicked = event && event.target ? event.target.closest('.disp-btn') : null;
            if (clicked) {
                clicked.style.background = 'rgba(74,222,128,0.15)';
                clicked.style.color = 'var(--accent)';
            }
            setTimeout(dialerDismissDisposition, 1200);

            // Update local engagement cache immediately for instant color-coding
            if (dialerActiveContact && dialerActiveContact.id) {
                const cid = dialerActiveContact.id;
                if (!_igbEngagementCache[cid]) {
                    _igbEngagementCache[cid] = {
                        messages: { lead: 0, assistant: 0, last_message_at: null, last_lead_at: null, last_assistant_at: null },
                        calls: { total_calls: 0, connected: 0, total_duration: 0, last_call_at: null, recordings: 0 }
                    };
                }
                _igbEngagementCache[cid].disposition = disp;
                dialerRenderContacts(); // Re-render for instant color update
                console.log('[Dialer] Disposition set locally:', disp, 'for', cid);
            }

            // Save to backend with retry (disposition is important data)
            if (_dialerLastCallSid) {
                _fetchRetry('/voice/call-disposition', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ call_sid: _dialerLastCallSid, disposition: disp })
                }, { retries: 2, timeout: 10000, label: 'disposition' }).catch(e => {
                    console.error('[Dialer] Disposition save failed after retries:', e.message);
                });
            }
        }

        // ── Call History / Recordings scope (contact-specific vs all) ──
        let _dialerCallHistoryShowAll = false;
        let _dialerRecordingsShowAll = false;

        function dialerToggleCallHistoryScope() {
            _dialerCallHistoryShowAll = !_dialerCallHistoryShowAll;
            dialerLoadAllCallHistory();
        }
        function dialerToggleRecordingsScope() {
            _dialerRecordingsShowAll = !_dialerRecordingsShowAll;
            dialerLoadRecordings();
        }

        // ── Load Call History (filtered to active contact unless "View All") ──
        async function dialerLoadAllCallHistory() {
            const panel = document.getElementById('dialerHistoryList');
            const label = document.getElementById('dialerCallsLabel');
            const viewBtn = document.getElementById('dialerCallsViewAllBtn');
            panel.innerHTML = '<div style="text-align:center;padding:20px;"><i class="fa-solid fa-spinner fa-spin" style="color:#00d9ff;"></i></div>';

            const filterContact = (!_dialerCallHistoryShowAll && dialerActiveContact) ? dialerActiveContact : null;
            if (label) label.textContent = filterContact ? (dialerActiveContact.firstName || dialerActiveContact.name) + "'s Calls" : 'All Calls';
            if (viewBtn) {
                viewBtn.textContent = filterContact ? 'View All' : (dialerActiveContact ? (dialerActiveContact.firstName || dialerActiveContact.name) + ' Only' : 'View All');
                viewBtn.style.display = dialerActiveContact ? 'inline-block' : 'none';
            }

            try {
                const r = await _fetchRetry('/voice/call-history?limit=100', {}, { retries: 1, timeout: 15000, label: 'call-history' });
                if (!r.ok) { panel.innerHTML = '<div style="color:#888;padding:16px;text-align:center;font-size:.88rem;">Failed to load</div>'; return; }
                const d = await r.json();
                let calls = d.calls || [];
                // Filter by contact if scoped
                if (filterContact) {
                    calls = calls.filter(c => c.contact_id === filterContact.id || c.phone === filterContact.phone);
                }
                if (!calls.length) {
                    panel.innerHTML = '<div style="text-align:center;padding:20px;color:#555;font-size:.92rem;">' +
                        (filterContact ? 'No calls with ' + dialerEsc(filterContact.name) + ' yet' : 'No call history yet') + '</div>';
                    return;
                }
                panel.innerHTML = calls.map(c => {
                    const statusColors = { completed:'var(--accent)', 'no-answer':'#ffa500', busy:'#ffa500', failed:'#ef4444', initiated:'#00d9ff', canceled:'#888' };
                    const sc = statusColors[c.status] || '#888';
                    const dur = c.duration ? Math.floor(c.duration/60) + ':' + String(c.duration%60).padStart(2,'0') : '--:--';
                    const dt = c.created_at ? new Date(c.created_at).toLocaleString([], {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}) : '';
                    const hasRec = !!c.recording_url;
                    const hasTx = c.transcript && c.transcript.length > 0;
                    const stirBadge = c.stir_status ? '<span class="stir-badge stir-badge-' + dialerEsc(c.stir_status).toLowerCase() + '">' + dialerEsc(c.stir_status) + '</span>' : '';
                    const dirIcon = (c.direction||'outbound') === 'inbound' ? '<i class="fa-solid fa-phone-arrow-down-left" style="font-size:.65rem;"></i>' : '<i class="fa-solid fa-phone-arrow-up-right" style="font-size:.65rem;"></i>';
                    // Line 1: name · direction · date
                    let row = '<div class="call-history-row" onclick="dialerHistoryClickContact(\'' + (c.contact_id||'') + '\')">';
                    row += '<div style="display:flex;align-items:center;gap:6px;min-width:0;">';
                    row += '<div class="call-history-dot" style="background:' + sc + ';flex-shrink:0;"></div>';
                    row += '<span class="chr-name">' + dialerEsc(c.contact_name || c.phone || 'Unknown') + '</span>';
                    row += '<span class="chr-dir">' + dirIcon + ' ' + (c.direction||'outbound') + '</span>';
                    row += '<span class="chr-date">' + dt + '</span>';
                    row += '</div>';
                    // Line 2: stir · status · duration · actions
                    row += '<div style="display:flex;align-items:center;gap:6px;margin-top:3px;padding-left:16px;">';
                    row += stirBadge;
                    row += '<span class="chr-status" style="color:' + sc + ';">' + (c.status||'').replace(/-/g,' ') + '</span>';
                    row += '<span class="chr-dur">' + dur + '</span>';
                    if (hasRec) row += '<button onclick="event.stopPropagation();playRecording(\'' + dialerEsc(c.recording_url) + '\')" class="chr-action-btn chr-btn-play" title="Play"><i class="fa-solid fa-play"></i></button>';
                    if (hasRec) row += '<a href="' + dialerEsc(c.recording_url) + '?dl=1" download class="chr-action-btn chr-btn-download" title="Download" onclick="event.stopPropagation();"><i class="fa-solid fa-download"></i></a>';
                    if (hasTx) row += '<button onclick=\'event.stopPropagation();showTranscript(' + JSON.stringify(c.transcript).replace(/'/g, "\\'") + ')\' class="chr-action-btn chr-btn-transcript" title="Transcript"><i class="fa-solid fa-file-lines"></i></button>';
                    if (!hasTx && hasRec && c.call_sid) row += '<button onclick="event.stopPropagation();transcribeNow(\'' + dialerEsc(c.call_sid) + '\',\'' + dialerEsc(c.recording_url) + '\',this)" class="chr-action-btn chr-btn-transcribe" title="Transcribe"><i class="fa-solid fa-wand-magic-sparkles"></i></button>';
                    row += '</div>';
                    row += '</div>';
                    return row;
                }).join('');
            } catch(e) { panel.innerHTML = '<div style="color:#ef4444;padding:12px;text-align:center;font-size:.88rem;">Error loading history</div>'; }
        }

        function dialerHistoryClickContact(contactId) {
            if (!contactId) return;
            const c = dialerContacts.find(x => x.id === contactId);
            if (c) dialerSelectContact(contactId);
        }

        // ── Load Recordings (filtered to active contact unless "View All") ──
        async function dialerLoadRecordings() {
            const panel = document.getElementById('dialerRecordingsList');
            const label = document.getElementById('dialerRecordingsLabel');
            const viewBtn = document.getElementById('dialerRecsViewAllBtn');
            panel.innerHTML = '<div style="text-align:center;padding:20px;"><i class="fa-solid fa-spinner fa-spin" style="color:#00d9ff;"></i></div>';

            const filterContact = (!_dialerRecordingsShowAll && dialerActiveContact) ? dialerActiveContact : null;
            if (label) label.textContent = filterContact ? (dialerActiveContact.firstName || dialerActiveContact.name) + "'s Recordings" : 'All Recordings';
            if (viewBtn) {
                viewBtn.textContent = filterContact ? 'View All' : (dialerActiveContact ? (dialerActiveContact.firstName || dialerActiveContact.name) + ' Only' : 'View All');
                viewBtn.style.display = dialerActiveContact ? 'inline-block' : 'none';
            }

            try {
                const r = await _fetchRetry('/voice/call-history?limit=100', {}, { retries: 1, timeout: 15000, label: 'call-history' });
                if (!r.ok) { panel.innerHTML = '<div style="color:#888;padding:16px;text-align:center;font-size:.88rem;">Failed to load</div>'; return; }
                const d = await r.json();
                let recordings = (d.calls || []).filter(c => c.recording_url);
                // Filter by contact if scoped
                if (filterContact) {
                    recordings = recordings.filter(c => c.contact_id === filterContact.id || c.phone === filterContact.phone);
                }
                if (!recordings.length) {
                    panel.innerHTML = '<div style="text-align:center;padding:20px;color:#555;font-size:.92rem;"><i class="fa-solid fa-record-vinyl" style="font-size:1.3rem;color:#333;margin-bottom:6px;display:block;"></i>' +
                        (filterContact ? 'No recordings for ' + dialerEsc(filterContact.name) : 'No recordings yet') + '</div>';
                    return;
                }
                panel.innerHTML = recordings.map(c => {
                    const dur = c.duration ? Math.floor(c.duration/60) + ':' + String(c.duration%60).padStart(2,'0') : '--:--';
                    const dt = c.created_at ? new Date(c.created_at).toLocaleString([], {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}) : '';
                    const hasTx = c.transcript && c.transcript.length > 0;
                    const sid = dialerEsc(c.call_sid || '');
                    const recUrl = dialerEsc(c.recording_url);
                    const dirIcon = (c.direction||'outbound') === 'inbound' ? '<i class="fa-solid fa-phone-arrow-down-left" style="font-size:.65rem;"></i>' : '<i class="fa-solid fa-phone-arrow-up-right" style="font-size:.65rem;"></i>';
                    let row = '<div class="call-history-row">';
                    // Line 1: name · direction · date
                    row += '<div style="display:flex;align-items:center;gap:6px;min-width:0;">';
                    row += '<span class="chr-name">' + dialerEsc(c.contact_name || c.phone || 'Unknown') + '</span>';
                    row += '<span class="chr-dir">' + dirIcon + ' ' + (c.direction||'outbound') + '</span>';
                    row += '<span class="chr-date">' + dt + '</span>';
                    row += '</div>';
                    // Line 2: duration · action buttons
                    row += '<div style="display:flex;align-items:center;gap:6px;margin-top:3px;">';
                    row += '<span class="chr-dur">' + dur + '</span>';
                    row += '<button onclick="playRecording(\'' + recUrl + '\')" style="background:#1e3a5f;color:#fff;border:none;border-radius:4px;padding:4px 8px;font-size:.72rem;font-weight:600;cursor:pointer;" title="Play"><i class="fa-solid fa-play"></i></button>';
                    row += '<a href="' + recUrl + '?dl=1" download style="background:#14532d;color:#fff;border:none;border-radius:4px;padding:4px 8px;font-size:.72rem;font-weight:600;cursor:pointer;text-decoration:none;" title="Download" onclick="event.stopPropagation();"><i class="fa-solid fa-download"></i></a>';
                    row += hasTx ? '<button onclick=\'showTranscript(' + JSON.stringify(c.transcript).replace(/'/g, "\\'") + ')\' style="background:#1e3a5f;color:#fff;border:none;border-radius:4px;padding:4px 8px;font-size:.72rem;font-weight:600;cursor:pointer;" title="Transcript"><i class="fa-solid fa-file-lines"></i></button>' : '';
                    row += !hasTx && sid ? '<button onclick="transcribeNow(\'' + sid + '\',\'' + recUrl + '\',this)" style="background:#1e3a5f;color:#fff;border:none;border-radius:4px;padding:4px 8px;font-size:.72rem;font-weight:600;cursor:pointer;" title="Transcribe"><i class="fa-solid fa-wand-magic-sparkles"></i></button>' : '';
                    row += '</div>';
                    row += '</div>';
                    return row +
                    '</div>';
                }).join('');
            } catch(e) { panel.innerHTML = '<div style="color:#ef4444;padding:12px;text-align:center;font-size:.88rem;">Error loading recordings</div>'; }
        }

        // Registry for pending queue timers so we can cancel them on stop
        let _dialerQueueTimers = [];
        function _dialerQueueTimeout(fn, ms) {
            const id = setTimeout(() => {
                _dialerQueueTimers = _dialerQueueTimers.filter(t => t !== id);
                fn();
            }, ms);
            _dialerQueueTimers.push(id);
            return id;
        }
        function _dialerCancelQueueTimers() {
            _dialerQueueTimers.forEach(id => clearTimeout(id));
            _dialerQueueTimers = [];
        }
        function _dialerClearCallDurationTimer() {
            if (_dialerCallDurationTimer) { clearTimeout(_dialerCallDurationTimer); _dialerCallDurationTimer = null; }
        }

        function dialerStopQueue() {
            dialerQueueRunning = false;
            _advanceLocked = false;
            _jtcDialingContactId = null;
            _jtcUpdatePill();
            const _wasDialing = _isDialing; // capture before clearing
            _isDialing = false; // release any in-flight dial guard
            dialerUpdateBtn();
            // Cancel ALL pending advance/retry/next timers to prevent ghost callbacks
            _dialerCancelQueueTimers();
            _dialerClearCallDurationTimer();
            // Hang up any active VoIP call immediately
            if (voipConnection) voipHangup();

            if (dialerCallSid) {
                const sid = dialerCallSid;

                // ── Synchronous cleanup BEFORE the async hangup request ──
                // Null the SID immediately so the poll can't re-fire against a
                // "dead" call and overwrite "Hanging up..." with "Connected".
                dialerCallSid = null;

                // Kill the poll immediately — same reason.
                if (dialerPollTimer) { clearInterval(dialerPollTimer); dialerPollTimer = null; }

                // Stop ring tone + listen stream now while we still have accurate state.
                _stopRingTone();
                _stopListenStream();
                _autoListenActive = false;

                // Show "Hanging up..." — update DOM directly to avoid dialerShowBanner's
                // ringing-blue side-effect; just keep the existing contact name in place.
                const statusEl = document.getElementById('dialerCallStatus');
                if (statusEl) { statusEl.textContent = 'Hanging up...'; statusEl.style.color = '#aaa'; }
                _dialerBannerState('ended');
                _dialerEnableControls(false);
                document.getElementById('dialerCallBanner').style.display = 'block';

                // Safety: force-close banner if the network is unreachable
                const forceCleanup = setTimeout(() => {
                    console.warn('[Dialer] Hangup timeout — force cleanup');
                    dialerHideBanner();
                    dialerStopAiTimer();
                }, 6000);

                // Send hangup to Twilio
                _fetchRetry('/voice/hangup', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ call_sid: sid })
                }, { retries: 2, timeout: 10000, label: 'hangup' }).then(async r => {
                    try {
                        const rd = await r.json();
                        if (rd.success === false) {
                            console.warn('[Dialer] Hangup: Twilio reports call may already have ended for', sid, '—', rd.note || '');
                        } else {
                            console.log('[Dialer] Hangup confirmed for', sid);
                        }
                    } catch(e) { console.log('[Dialer] Hangup sent for', sid); }
                }).catch(e => {
                    console.error('[Dialer] Hangup request failed:', e.message);
                }).finally(() => {
                    clearTimeout(forceCleanup);
                    // Brief delay so the user sees "Hanging up..." before it disappears
                    setTimeout(() => {
                        dialerHideBanner();
                        dialerStopAiTimer();
                    }, 400);
                });
            } else {
                if (_wasDialing) {
                    // Hang Up clicked while /voice/dial was still in-flight.
                    // dialerCallSid is still null so we can't hang up yet —
                    // flag it so dialerStartCall cancels the moment the SID arrives.
                    _hangupPending = true;
                    console.warn('[Dialer] Hang Up mid-dial — will cancel call as soon as API responds');
                }
                // No active call — clean up immediately
                if (dialerPollTimer) { clearInterval(dialerPollTimer); dialerPollTimer = null; }
                dialerHideBanner();
                dialerStopAiTimer();
            }
        }

        // ── Queue management ──
        function dialerRenderQueue() {
            const list = document.getElementById('dialerQueueList');
            const cnt = document.getElementById('dialerQueueCount');
            const btn = document.getElementById('dialerStartBtn');
            cnt.textContent = dialerQueue.length;
            btn.disabled = !dialerQueue.length;
            // Auto-expand queue body when contacts are added
            if (dialerQueue.length > 0) {
                const qBodyAuto = document.getElementById('dialerQueueBody');
                if (qBodyAuto && qBodyAuto.style.display === 'none') qBodyAuto.style.display = 'block';
            }
            if (!dialerQueue.length) { list.innerHTML = '<div style="text-align:center;padding:10px;color:#555;font-size:.75rem;">Empty queue</div>'; return; }
            const icons = { pending:'<span style="color:#555;">Wait</span>', initiated:'<i class="fa-solid fa-spinner fa-spin" style="color:#00d9ff;"></i>', ringing:'<span style="color:#00d9ff;">Ring</span>', 'in-progress':'<span style="color:var(--accent);">Live</span>', completed:'<i class="fa-solid fa-check" style="color:var(--accent);"></i>', 'no-answer':'<span style="color:#ffa500;">N/A</span>', busy:'<span style="color:#ffa500;">Busy</span>', failed:'<i class="fa-solid fa-xmark" style="color:#ef4444;"></i>', skipped:'<i class="fa-solid fa-ban" style="color:#ef4444;" title="DnD — skipped"></i>' };
            list.innerHTML = dialerQueue.map((q, i) => {
                const active = dialerQueueRunning && i === dialerCallIdx;
                return '<div class="dlr-queue-row-clickable" onclick="dialerJumpToContact(\'' + q.id + '\')" style="display:flex;align-items:center;gap:6px;padding:3px 4px;border-radius:4px;font-size:.75rem;' + (active ? 'background:rgba(74,222,128,0.05);' : '') + '">' +
                    '<span style="color:' + (active ? 'var(--accent)' : '#555') + ';font-weight:700;width:16px;text-align:center;">' + (i+1) + '</span>' +
                    '<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">' + dialerEsc(q.name) + '</span>' +
                    ((q.attempts && q.attempts > 1) ? '<span style="color:#888;font-size:.75rem;">' + q.attempts + '/' + dialerMaxAttempts + '</span>' : '') +
                    '<span>' + (icons[q.status] || q.status) + '</span>' +
                    (!dialerQueueRunning ? '<button onclick="event.stopPropagation();dialerQueue.splice('+i+',1);dialerRenderContacts();dialerRenderQueue();" style="background:none;border:none;color:#444;cursor:pointer;font-size:.75rem;padding:0 2px;"><i class="fa-solid fa-xmark"></i></button>' : '') +
                '</div>';
            }).join('');
        }

        function dialerClearQueue() { if (dialerQueueRunning) return; dialerQueue = []; dialerCallIdx = -1; dialerRenderContacts(); dialerRenderQueue(); }

        // ═══════════════════════════════════════════════════════════════
        //   Campaign History — queues saved as named campaigns in localStorage
        // ═══════════════════════════════════════════════════════════════
        const _CAMPAIGNS_KEY = 'igb_dialer_campaigns';
        function _campaignLoad() { try { return JSON.parse(localStorage.getItem(_CAMPAIGNS_KEY) || '[]'); } catch(e) { return []; } }
        function _campaignSave(list) { try { localStorage.setItem(_CAMPAIGNS_KEY, JSON.stringify(list)); } catch(e) {} }

        function _campaignStart() {
            if (!dialerQueue.length) return;
            const campaigns = _campaignLoad();
            const existing = campaigns.find(c => c.id === _activeCampaignId);
            if (existing) return; // already saved for this session
            const id = 'cmp_' + Date.now();
            _activeCampaignId = id;
            campaigns.unshift({
                id,
                name: 'Campaign ' + new Date().toLocaleDateString('en-US', { month:'short', day:'numeric', hour:'2-digit', minute:'2-digit' }),
                startedAt: Date.now(),
                completedAt: null,
                contacts: dialerQueue.map(q => ({ id: q.id, name: q.name, firstName: q.firstName, phone: q.phone })),
                progress: { total: dialerQueue.length, completed: 0 },
            });
            // Keep max 20 campaigns
            _campaignSave(campaigns.slice(0, 20));
            _campaignRenderHistory();
        }

        function _campaignFinish() {
            if (!_activeCampaignId) return;
            const campaigns = _campaignLoad();
            const idx = campaigns.findIndex(c => c.id === _activeCampaignId);
            if (idx >= 0) {
                campaigns[idx].completedAt = Date.now();
                campaigns[idx].progress.completed = dialerQueue.filter(q => q.status === 'completed').length;
                _campaignSave(campaigns);
                _campaignRenderHistory();
            }
        }

        function _campaignResume(id) {
            if (dialerQueueRunning) { _showDashToast(false, 'Stop the current session before resuming a campaign'); return; }
            const campaigns = _campaignLoad();
            const c = campaigns.find(x => x.id === id);
            if (!c) return;
            dialerQueue = c.contacts.map(ct => ({ id: ct.id, name: ct.name, firstName: ct.firstName, phone: ct.phone, status: 'pending' }));
            dialerCallIdx = -1;
            _activeCampaignId = id;
            dialerRenderContacts();
            dialerRenderQueue();
            _showDashToast(true, 'Campaign loaded — ' + c.contacts.length + ' contacts ready');
            // Ensure queue panel is open
            const qBody = document.getElementById('dialerQueueBody');
            if (qBody) qBody.style.display = 'block';
        }

        function _campaignRenderHistory() {
            const el = document.getElementById('dlrCampaignHistory');
            if (!el) return;
            const campaigns = _campaignLoad();
            if (!campaigns.length) { el.innerHTML = '<div style="text-align:center;padding:10px;color:#555;font-size:.75rem;">No campaigns yet</div>'; return; }
            el.innerHTML = campaigns.map(c => {
                const date = new Date(c.startedAt).toLocaleDateString('en-US', { month:'short', day:'numeric', hour:'2-digit', minute:'2-digit' });
                const done = c.completedAt ? '<i class="fa-solid fa-check" style="color:var(--accent);font-size:.65rem;"></i>' : '<span style="font-size:.65rem;color:#ffa500;">In Progress</span>';
                const pct = c.progress.total ? Math.round(c.progress.completed / c.progress.total * 100) : 0;
                const isActive = c.id === _activeCampaignId;
                return '<div style="display:flex;align-items:center;gap:6px;padding:5px 4px;border-radius:4px;font-size:.75rem;' + (isActive ? 'background:rgba(0,255,136,0.05);' : '') + '">' +
                    '<div style="flex:1;overflow:hidden;">' +
                    '<div style="font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">' + dialerEsc(c.name) + '</div>' +
                    '<div style="color:#555;font-size:.7rem;">' + date + ' · ' + c.progress.total + ' contacts' + (c.progress.completed ? ' · ' + pct + '% done' : '') + '</div>' +
                    '</div>' +
                    done +
                    (!isActive ? '<button onclick="_campaignResume(\'' + c.id + '\')" style="background:rgba(255,255,255,0.07);border:1px solid rgba(255,255,255,0.12);color:#ccc;border-radius:4px;font-size:.65rem;font-weight:700;padding:2px 7px;cursor:pointer;">Resume</button>' : '<span style="font-size:.65rem;color:var(--accent);font-weight:700;">Active</span>') +
                '</div>';
            }).join('');
        }

        let _activeCampaignId = null;

        function dialerUpdateBtn() {
            const btn = document.getElementById('dialerStartBtn');
            if (!btn) return;
            if (dialerQueueRunning) { btn.innerHTML = '<i class="fa-solid fa-stop me-1"></i>Stop'; btn.style.background = 'linear-gradient(135deg,#ef4444,#cc3333)'; btn.style.color = '#fff'; }
            else { btn.innerHTML = '<i class="fa-solid fa-play me-1"></i>Auto-Dial'; btn.style.background = 'linear-gradient(135deg,var(--accent),#00b36b)'; btn.style.color = '#000'; }
        }
        function dialerToggleQueue() {
            if (dialerQueueRunning) { dialerQueueRunning = false; _advanceLocked = false; _jtcDialingContactId = null; _dialerCancelQueueTimers(); _dialerClearCallDurationTimer(); dialerUpdateBtn(); _jtcUpdatePill(); return; }
            dialerCallIdx = dialerQueue.findIndex(q => q.status === 'pending');
            if (dialerCallIdx < 0) { dialerQueue.forEach(q => { if (q.status !== 'completed' && q.status !== 'skipped') q.status = 'pending'; }); dialerCallIdx = dialerQueue.findIndex(q => q.status === 'pending'); if (dialerCallIdx < 0) return; }
            dialerQueueRunning = true;
            // Auto-expand queue body so user can see progression
            const qBody = document.getElementById('dialerQueueBody');
            if (qBody) qBody.style.display = 'block';
            dialerUpdateBtn();
            _campaignStart();
            dialerDialNext();
        }
        function dialerDialNext() {
            if (!dialerQueueRunning || dialerCallIdx < 0 || dialerCallIdx >= dialerQueue.length) { dialerQueueRunning = false; _jtcDialingContactId = null; _campaignFinish(); dialerUpdateBtn(); dialerHideBanner(); dialerRenderQueue(); _jtcUpdatePill(); return; }
            const item = dialerQueue[dialerCallIdx];
            // Safety net: skip DnD contacts even if they got into the queue
            const contactObj = dialerContacts.find(x => x.id === item.id) || _dialerAllContacts.find(x => x.id === item.id);
            if (contactObj && _igbIsOptedOut(_igbEngagementCache[item.id], contactObj)) {
                const eng = _igbEngagementCache[item.id];
                console.warn(`[Dialer] SKIP — ${item.name} (${item.id}): dnd=${contactObj.dnd}, eng.opted_out=${eng && eng.opted_out}, cache=${JSON.stringify(eng)}`);
                item.status = 'skipped';
                dialerRenderQueue();
                dialerAdvance();
                return;
            }
            if (!item.attempts) item.attempts = 0;
            item.attempts++;
            item.status = 'initiated';
            dialerRenderQueue();
            // Track dialing contact for Jump to Contact
            _jtcDialingContactId = item.id;
            // Flash the contact row + panel to signal the jump
            _jtcFlashContact(item.id);
            // Auto-scroll queue to the active item
            _jtcScrollToActiveQueueItem();
            console.log(`[Dialer] Calling ${item.name} (${item.phone}) — attempt ${item.attempts}/${dialerMaxAttempts}`);
            dialerStartCall(item.phone, item.firstName || item.name, item.id, item.name);
        }
        // Guard: prevent concurrent advance calls (the root cause of queue skipping)
        let _advanceLocked = false;
        function dialerAdvance() {
            if (!dialerQueueRunning) { _advanceLocked = false; dialerHideBanner(); return; }
            // Prevent re-entrant calls — multiple timeouts can fire dialerAdvance simultaneously
            if (_advanceLocked) { console.log('[Dialer] Advance blocked (already advancing)'); return; }
            _advanceLocked = true;

            // Check if current contact needs a retry (no-answer, busy, failed, voicemail)
            if (dialerCallIdx >= 0 && dialerCallIdx < dialerQueue.length) {
                const current = dialerQueue[dialerCallIdx];
                const retryStatuses = ['no-answer', 'busy', 'failed', 'canceled'];
                if (retryStatuses.includes(current.status) && (current.attempts || 0) < dialerMaxAttempts) {
                    const retryMs = _dialerRetryDelay || 2000;
                    console.log(`[Dialer] Retrying ${current.name} — attempt ${(current.attempts || 0) + 1}/${dialerMaxAttempts} in ${retryMs}ms`);
                    current.status = 'pending';
                    dialerRenderQueue();
                    // Configurable retry delay; use queue timer so it's cancelable
                    _dialerQueueTimeout(() => { _advanceLocked = false; if (dialerQueueRunning) dialerDialNext(); }, retryMs);
                    return;
                }
                // Max attempts exhausted — mark as final status
                if (retryStatuses.includes(current.status)) {
                    console.log(`[Dialer] ${current.name} — max attempts (${dialerMaxAttempts}) reached, moving on`);
                }
                // Unexpected: advance fired while contact is still initiated (double-advance bug indicator)
                if (current.status === 'initiated') {
                    console.warn(`[Dialer] WARN: advance() fired for "${current.name}" while status=initiated — possible double-advance race`);
                }
            }

            // Move to next pending contact
            dialerCallIdx = dialerQueue.findIndex((q, i) => i > dialerCallIdx && q.status === 'pending');
            if (dialerCallIdx < 0) { _advanceLocked = false; dialerQueueRunning = false; _jtcDialingContactId = null; dialerUpdateBtn(); dialerHideBanner(); dialerRenderQueue(); _jtcUpdatePill(); return; }
            // Configurable pause before next contact; use queue timer so it's cancelable
            const pauseMs = _dialerPauseBetween ?? 1000;
            _dialerQueueTimeout(() => { _advanceLocked = false; if (dialerQueueRunning) dialerDialNext(); }, pauseMs);
        }

        // ── Recording + Transcript ──
        let _audioPlayer = null;
        function playRecording(url) {
            if (_audioPlayer) { _audioPlayer.pause(); _audioPlayer = null; }
            _audioPlayer = new Audio(url);
            _audioPlayer.play().catch(e => alert('Unable to play: ' + e.message));
        }
        function showTranscript(transcript) {
            const modal = document.getElementById('transcriptModal');
            const content = document.getElementById('transcriptContent');
            if (!transcript || !transcript.length) { content.innerHTML = '<p style="color:#888;">No transcript</p>'; }
            else {
                content.innerHTML = transcript.map(t => {
                    const isLead = t.role === 'lead';
                    const isRec = t.role === 'call_recording';
                    if (isRec) {
                        return '<div style="margin-bottom:10px;padding:12px;background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.06);border-radius:10px;font-size:.88rem;color:#ccc;line-height:1.6;">' +
                            '<div style="font-size:.75rem;color:#888;margin-bottom:6px;"><i class="fa-solid fa-microphone me-1"></i>Recording Transcript</div>' +
                            dialerEsc(t.text) + '</div>';
                    }
                    return '<div style="margin-bottom:10px;display:flex;flex-direction:column;align-items:' + (isLead ? 'flex-start' : 'flex-end') + ';">' +
                        '<div style="font-size:.75rem;color:#888;margin-bottom:2px;">' + (isLead ? 'Lead' : 'AI Agent') + '</div>' +
                        '<div class="dlr-msg-bubble ' + (isLead ? 'dlr-msg-lead' : 'dlr-msg-bot') + '">' + dialerEsc(t.text) + '</div></div>';
                }).join('');
            }
            modal.style.display = 'flex';
        }
        function closeTranscriptModal() { document.getElementById('transcriptModal').style.display = 'none'; }

        async function transcribeNow(callSid, recordingUrl, btnEl) {
            if (!callSid || !recordingUrl) return;
            const origHTML = btnEl.innerHTML;
            btnEl.disabled = true;
            btnEl.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
            try {
                const r = await _fetchRetry('/voice/transcribe-recording', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({call_sid: callSid, recording_url: recordingUrl})
                }, {retries: 1, timeout: 60000, label: 'transcribe'});
                const d = await r.json();
                if (r.ok && d.transcript && d.transcript.length) {
                    // Swap button to "View Transcript"
                    btnEl.innerHTML = '<i class="fa-solid fa-file-lines"></i>';
                    btnEl.style.background = 'rgba(74,222,128,0.08)';
                    btnEl.style.border = '1px solid rgba(74,222,128,0.12)';
                    btnEl.style.color = 'var(--accent)';
                    btnEl.disabled = false;
                    btnEl.title = 'View Transcript';
                    const tx = d.transcript;
                    btnEl.setAttribute('onclick', '');
                    btnEl.onclick = () => showTranscript(tx);
                    _showDashToast(true, 'Transcription complete!');
                } else {
                    btnEl.disabled = false;
                    btnEl.innerHTML = origHTML;
                    _showDashToast(false, d.error || 'Transcription failed');
                }
            } catch(e) {
                btnEl.disabled = false;
                btnEl.innerHTML = origHTML;
                _showDashToast(false, 'Network error — transcription failed');
            }
        }

        // ===== AI MINUTES BALANCE (Dialer Integration) =====
        async function _dialerFetchMinutes() {
            try {
                const r = await fetch('/ai-minutes/balance');
                if (!r.ok) return;
                const d = await r.json();
                _aiMinutesBalance = d.balance_minutes || 0;
                _aiMinutesHasPurchased = (d.total_purchased || 0) > 0;
                _dialerUpdateMinutesChip();
            } catch(e) { console.debug('[AI Minutes] Fetch failed:', e); }
        }

        function _dialerUpdateMinutesChip() {
            const chip = document.getElementById('dialerMinutesChip');
            if (!chip) return;
            // Only show chip if user has purchased minutes AND mode is AI
            if (!_aiMinutesHasPurchased || dialerMode !== 'ai') { chip.style.display = 'none'; return; }
            chip.style.display = 'inline-flex';
            const bal = _aiMinutesBalance || 0;
            chip.textContent = bal + ' min';
            if (bal <= 0) {
                chip.style.background = 'rgba(239,68,68,0.15)';
                chip.style.color = '#ef4444';
            } else if (bal <= 25) {
                chip.style.background = 'rgba(255,165,0,0.15)';
                chip.style.color = '#ffa500';
            } else {
                chip.style.background = 'rgba(0,217,255,0.12)';
                chip.style.color = '#00d9ff';
            }
        }

        function _dialerShowOutOfMinutes() {
            const existing = document.getElementById('dialerOutOfMinutesModal');
            if (existing) { existing.style.display = 'flex'; return; }
            const modal = document.createElement('div');
            modal.id = 'dialerOutOfMinutesModal';
            modal.style.cssText = 'position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,0.6)';
            modal.innerHTML = `
                <div style="background:#1a1a2e;border:1px solid rgba(255,255,255,0.1);border-radius:16px;padding:32px;max-width:400px;text-align:center;box-shadow:0 20px 60px rgba(0,0,0,0.5)">
                    <div style="font-size:48px;margin-bottom:12px">⏱️</div>
                    <h3 style="color:#fff;margin:0 0 8px">Out of AI Minutes</h3>
                    <p style="color:#aaa;margin:0 0 20px;font-size:14px">Purchase more minutes to continue making AI-powered calls.</p>
                    <div style="display:flex;gap:12px;justify-content:center">
                        <button onclick="document.getElementById('dialerOutOfMinutesModal').style.display='none'"
                            style="padding:10px 20px;border-radius:8px;border:1px solid rgba(255,255,255,0.1);background:transparent;color:#aaa;cursor:pointer">Close</button>
                        <button onclick="document.getElementById('dialerOutOfMinutesModal').style.display='none';document.querySelector('[data-tab=\\'aiminutes\\']')?.click()"
                            style="padding:10px 20px;border-radius:8px;border:none;background:linear-gradient(135deg,#00d9ff,#0099cc);color:#000;font-weight:600;cursor:pointer">Buy Minutes</button>
                    </div>
                </div>`;
            document.body.appendChild(modal);
        }

        // Fetch balance on dialer tab show + 2s after page load
        setTimeout(_dialerFetchMinutes, 2000);

        // ===== CALL MODE MANAGEMENT =====
        let dialerMode = 'human';
        let voipDevice = null;
        let voipConnection = null;  // Active Call object (v2 SDK)
        let voipReady = false;
        let voipTimerInterval = null;
        let voipTimerSeconds = 0;
        // Credential already exists in DB — skip one-time setup, just need to connect SDK
        let voipSetupDone = !!(window.DASHBOARD_BOOT && window.DASHBOARD_BOOT.voipSetupDone);
        let voipCurrentContact = null;
        let _voipInitializing = false;  // prevent duplicate init calls
        // Safe localStorage wrapper — cross-origin iframes (Safari ITP, third-party
        // cookie restrictions) can block localStorage entirely. Graceful degradation:
        // device prefs just reset each session instead of crashing VOIP.
        function _lsGet(k) { try { return localStorage.getItem(k); } catch(e) { return null; } }
        function _lsSet(k,v) { try { localStorage.setItem(k,v); } catch(e) {} }
        function _lsDel(k) { try { localStorage.removeItem(k); } catch(e) {} }
        let _selectedInputDeviceId = _lsGet('voip_input_device') || '';
        let _selectedOutputDeviceId = _lsGet('voip_output_device') || '';

        function setDialerMode(mode) {
            dialerMode = mode;
            const aiBtn = document.getElementById('modeAiBtn');
            const humanBtn = document.getElementById('modeHumanBtn');
            // Sync Dynamic Island buttons
            const iosDmAi = document.getElementById('iosDmAi');
            const iosDmHuman = document.getElementById('iosDmHuman');
            if (iosDmAi && iosDmHuman) {
                iosDmAi.classList.toggle('ios-dm-active', mode === 'ai');
                iosDmHuman.classList.toggle('ios-dm-active', mode !== 'ai');
            }
            if (mode === 'ai') {
                aiBtn.style.background = 'linear-gradient(135deg,#00d9ff,#0099cc)'; aiBtn.style.color = '#000';
                humanBtn.style.background = 'transparent'; humanBtn.style.color = '#888';
                document.getElementById('voipSetupBanner').style.display = 'none';
            } else {
                humanBtn.style.background = 'linear-gradient(135deg,var(--accent),#00b36b)'; humanBtn.style.color = '#000';
                aiBtn.style.background = 'transparent'; aiBtn.style.color = '#888';
                if (!voipSetupDone) {
                    // Silently set up VoIP in background so it's ready when they dial
                    setupVoIP().catch(e => console.error('[VoIP] Background setup failed:', e));
                } else if (!voipReady && !_voipInitializing) {
                    initVoIPDevice();
                }
            }
            // Show/hide AI-only call controls (Listen, Mute AI, Intercept) based on mode
            // These only apply when AI is handling the call
            const _aiOnlyBtns = ['dialerListenBtn', 'dialerMuteBtn', 'dialerTakeoverBtn'];
            _aiOnlyBtns.forEach(id => {
                const btn = document.getElementById(id);
                if (btn) btn.style.display = (mode === 'ai') ? 'flex' : 'none';
            });
            // Multi-line toggle: visible in BOTH modes for pro_dialer+
            // Human vs AI just changes who talks — dialing capability stays the same
            // Show/hide AI minutes chip based on mode
            _dialerUpdateMinutesChip();
        }

        // ===== CALLER NUMBER PICKER =====
        async function _dialerLoadNumbers() {
            try {
                const r = await fetch('/voice/numbers');
                if (!r.ok) return;
                const d = await r.json();
                _dialerNumbers = (d.numbers || []).filter(n => n.capabilities && n.capabilities.voice);
                _dialerRenderNumberPicker();
            } catch(e) {
                console.warn('[Dialer] Failed to load numbers:', e);
            }
        }

        function _dialerRenderNumberPicker() {
            const menu = document.getElementById('dfnMenu');
            const trigger = document.getElementById('dfnTriggerText');
            if (!menu || !trigger) return;

            // Build menu items
            let html = '<div class="dfn-item' + (!_dialerSelectedNumber ? ' active' : '') + '" data-val="" onclick="_dfnSelect(this,\'\')">' +
                '<span class="dfn-dot"></span><span class="dfn-item-label">Auto (Smart Rotation)</span></div>';
            _dialerNumbers.forEach(n => {
                const isActive = _dialerSelectedNumber === n.phone;
                const esc = n.phone.replace(/'/g, "\\'");
                const label = n.nickname ? _dialerEscHtml(n.nickname) : _dialerFmtPhone(n.phone);
                const sub = n.nickname ? _dialerFmtPhone(n.phone) : '';
                html += '<div class="dfn-item' + (isActive ? ' active' : '') + '" data-val="' + n.phone + '" onclick="_dfnSelect(this,\'' + esc + '\')">' +
                    '<span class="dfn-dot"></span>' +
                    '<span class="dfn-item-label">' + label + '</span>' +
                    (sub ? '<span class="dfn-item-sub">' + sub + '</span>' : '') +
                    '</div>';
            });
            menu.innerHTML = html;

            // Validate saved selection still exists
            if (_dialerSelectedNumber && !_dialerNumbers.some(n => n.phone === _dialerSelectedNumber)) {
                _dialerSelectedNumber = '';
                _lsDel('dialerFromNumber');
            }
            // Update trigger text
            _dfnUpdateTrigger();

            // Show picker if user has 2+ voice numbers
            const wrap = document.getElementById('dialerFromNumberWrap');
            if (wrap) wrap.style.display = _dialerNumbers.length >= 2 ? '' : 'none';
        }

        function _dfnUpdateTrigger() {
            const trigger = document.getElementById('dfnTriggerText');
            if (!trigger) return;
            if (!_dialerSelectedNumber) {
                trigger.textContent = 'Auto (Smart Rotation)';
                return;
            }
            const num = _dialerNumbers.find(n => n.phone === _dialerSelectedNumber);
            trigger.textContent = num && num.nickname ? num.nickname + ' — ' + _dialerFmtPhone(num.phone) : _dialerFmtPhone(_dialerSelectedNumber);
        }

        function _dfnToggle() {
            const menu = document.getElementById('dfnMenu');
            const trig = document.getElementById('dfnTrigger');
            if (!menu) return;
            const open = menu.classList.toggle('show');
            if (trig) trig.classList.toggle('open', open);
            if (open) {
                // Close on outside click
                setTimeout(() => document.addEventListener('click', _dfnOutside, { once: true }), 0);
            }
        }
        function _dfnOutside(e) {
            const wrap = document.getElementById('dialerFromNumberWrap');
            if (wrap && !wrap.contains(e.target)) _dfnClose();
            else setTimeout(() => document.addEventListener('click', _dfnOutside, { once: true }), 0);
        }
        function _dfnClose() {
            const menu = document.getElementById('dfnMenu');
            const trig = document.getElementById('dfnTrigger');
            if (menu) menu.classList.remove('show');
            if (trig) trig.classList.remove('open');
        }

        function _dfnSelect(el, val) {
            _dialerSelectedNumber = val;
            if (val) { _lsSet('dialerFromNumber', val); } else { _lsDel('dialerFromNumber'); }
            // Update active state
            const menu = document.getElementById('dfnMenu');
            if (menu) menu.querySelectorAll('.dfn-item').forEach(i => i.classList.toggle('active', i.dataset.val === val));
            _dfnUpdateTrigger();
            _dfnClose();
        }

        function _dialerEscHtml(s) { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }

        function _dialerFmtPhone(p) {
            if (!p) return '';
            const d = p.replace(/\D/g, '');
            if (d.length === 11 && d[0] === '1') return '+1 (' + d.substr(1,3) + ') ' + d.substr(4,3) + '-' + d.substr(7);
            if (d.length === 10) return '(' + d.substr(0,3) + ') ' + d.substr(3,3) + '-' + d.substr(6);
            return p;
        }

        function _dialerGetFromNumber() {
            return _dialerSelectedNumber || '';
        }

        // Load numbers on dialer init (after a short delay to prioritize contacts)
        setTimeout(_dialerLoadNumbers, 1500);

        // Show VoIP status messages (visible in banner and setup areas)
        function _showVoipStatus(msg) {
            const el = document.getElementById('voipSetupStatus');
            if (el) { el.textContent = msg; el.style.display = msg ? 'block' : 'none'; }
            const el2 = document.getElementById('voipSetupBtnDialerStatus');
            if (el2 && msg) el2.textContent = msg;
        }

        // Set visual state on ALL VoIP setup buttons (banner, dialer settings, alt)
        function setVoipBtnState(state) {
            const btns = [
                document.getElementById('voipSetupBtn'),
                document.getElementById('voipSetupBtnDialer'),
            ];
            const map = {
                idle:      { bg: 'linear-gradient(135deg,#00d9ff,#0099cc)', icon: 'fa-headset',          label: 'Setup Browser VoIP', disabled: false },
                loading:   { bg: 'linear-gradient(135deg,#ffa500,#cc8400)', icon: 'fa-spinner fa-spin',  label: 'Connecting…',        disabled: true  },
                connected: { bg: 'linear-gradient(135deg,#4ade80,#22c55e)', icon: 'fa-circle-check',     label: 'VoIP Connected',     disabled: false },
                error:     { bg: 'linear-gradient(135deg,#ef4444,#cc2222)', icon: 'fa-rotate-right',     label: 'Retry VoIP Setup',   disabled: false },
            };
            const s = map[state] || map.idle;
            btns.forEach(b => {
                if (!b) return;
                b.style.background = s.bg;
                b.style.color = '#000';
                b.innerHTML = `<i class="fa-solid ${s.icon} me-1"></i>${s.label}`;
                b.disabled = s.disabled;
            });
            // Update the dialer settings status text
            const statusSpan = document.getElementById('voipSetupBtnDialerStatus');
            if (statusSpan) {
                if (state === 'connected') statusSpan.textContent = 'VoIP connected';
                else if (state === 'loading') statusSpan.textContent = 'Connecting…';
                else if (state === 'error') statusSpan.textContent = 'Setup failed — click to retry';
            }
        }

        async function setupVoIP() {
            console.log('[VoIP] setupVoIP() called');
            setVoipBtnState('loading');
            _showVoipStatus('Creating voice credential…');
            try {
                const r = await fetch('/voice/setup-voip', { method: 'POST', headers: {'Content-Type': 'application/json'} });
                const d = await r.json();
                console.log('[VoIP] setup-voip response:', r.status, d);
                if (!r.ok) {
                    setVoipBtnState('error');
                    _showVoipStatus(d.error || 'Setup failed');
                    return;
                }
                voipSetupDone = true;
                document.getElementById('voipSetupBanner').style.display = 'none';
                _showVoipStatus('Credential created — initializing…');
                await initVoIPDevice();
            } catch(e) {
                console.error('[VoIP] setupVoIP error:', e);
                setVoipBtnState('error');
                const errMsg = (e && e.message) ? e.message : (typeof e === 'string' ? e : JSON.stringify(e));
                _showVoipStatus('Setup error: ' + errMsg);
            }
        }

        async function initVoIPDevice() {
            if (_voipInitializing) { console.log('[VoIP] Already initializing, skipping'); return; }
            _voipInitializing = true;
            console.log('[VoIP] initVoIPDevice() called');

            try {
                // Step 1: Fetch token
                _showVoipStatus('Fetching voice token…');
                setVoipBtnState('loading');
                const r = await fetch('/voice/token', { method: 'POST', headers: {'Content-Type': 'application/json'} });
                const d = await r.json();
                console.log('[VoIP] token response:', r.status, d.token ? 'token OK (len=' + d.token.length + ')' : d);
                if (!r.ok) {
                    if (d.error && d.error.includes('not set up')) {
                        document.getElementById('voipSetupBanner').style.display = 'block';
                        voipSetupDone = false;
                    }
                    _showVoipStatus(d.error || 'Token error');
                    setVoipBtnState('error');
                    _voipInitializing = false;
                    return;
                }

                // Step 2: Request microphone permission BEFORE connecting SDK
                _showVoipStatus('Requesting microphone access…');
                try {
                    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
                    stream.getTracks().forEach(t => t.stop());
                    console.log('[VoIP] Microphone permission granted');
                } catch(micErr) {
                    console.error('[VoIP] Microphone permission denied:', micErr);
                    _showVoipStatus('Microphone access denied — please allow mic and retry');
                    setVoipBtnState('error');
                    _voipInitializing = false;
                    return;
                }

                // Step 3: Load Twilio Voice SDK 2.x (self-hosted)
                if (!window.Twilio || !window.Twilio.Device) {
                    _showVoipStatus('Loading Voice SDK…');
                    await new Promise((resolve, reject) => {
                        const s = document.createElement('script');
                        s.src = '/static/js/twilio-voice-sdk-2.18.0.min.js';
                        s.onload = () => { console.log('[VoIP] Voice SDK 2.18.0 loaded'); resolve(); };
                        s.onerror = (e) => { console.error('[VoIP] SDK load failed:', e); reject(new Error('Failed to load Voice SDK')); };
                        document.head.appendChild(s);
                    });
                } else {
                    console.log('[VoIP] Voice SDK already loaded');
                }

                if (!window.Twilio || !window.Twilio.Device) {
                    throw new Error('Voice SDK loaded but Twilio.Device not found');
                }

                // Step 4: Create Device instance (V2 SDK — constructor-based)
                _showVoipStatus('Connecting to voice server…');

                // Destroy previous device if any
                if (voipDevice) {
                    // Clear dangling connection ref before destroying device
                    if (voipConnection) {
                        try { voipConnection.disconnect(); } catch(e) {}
                        voipConnection = null;
                    }
                    try { voipDevice.destroy(); } catch(e) {}
                    voipDevice = null;
                }

                voipDevice = new Twilio.Device(d.token, {
                    logLevel: 'debug',
                    closeProtection: true,
                    codecPreferences: ['opus', 'pcmu'],
                    // Disable browser audio processing that causes mic breakup/pumping artifacts.
                    // AGC and noise suppression fight each other on fast connections and cause
                    // the "breaking out" choppy mic effect. Echo cancellation stays on.
                    audioConstraints: {
                        autoGainControl: false,
                        noiseSuppression: false,
                        echoCancellation: true,
                    },
                    // Prefer US edges to minimize latency-induced jitter
                    edge: ['ashburn', 'umatilla', 'roaming'],
                });

                console.log('[VoIP] Device created, SDK version:', Twilio.Device.packageName || 'unknown');

                // Step 5: Register (async — opens WebSocket to Twilio signaling)
                let registrationDone = false;

                voipDevice.on('registered', () => {
                    registrationDone = true;
                    console.log('[VoIP] Device registered — ready for calls!');
                    voipReady = true;
                    voipSetupDone = true;
                    _voipInitializing = false;
                    document.getElementById('voipStatus').style.display = 'block';
                    document.getElementById('voipSetupBanner').style.display = 'none';
                    _showVoipStatus('VoIP connected');
                    setVoipBtnState('connected');
                    // Populate audio device lists after registration
                    refreshAudioDevices();
                });

                voipDevice.on('unregistered', () => {
                    console.log('[VoIP] Device unregistered');
                    voipReady = false;
                    document.getElementById('voipStatus').style.display = 'none';
                    _showVoipStatus('VoIP disconnected — click to reconnect');
                    setVoipBtnState('error');
                    _voipInitializing = false;
                });

                voipDevice.on('error', (err) => {
                    console.error('[VoIP] Device error:', err);
                    console.error('[VoIP] Error code:', err?.code, 'twilioError:', err?.twilioError);
                    const msg = err?.message || JSON.stringify(err);
                    _showVoipStatus('VoIP error: ' + msg);
                    // Only show error state if we're not actively on a call
                    if (!voipConnection) {
                        setVoipBtnState('error');
                        voipReady = false;
                    }
                    _voipInitializing = false;
                });

                voipDevice.on('tokenWillExpire', async () => {
                    console.log('[VoIP] Token expiring — refreshing…');
                    try {
                        const tr = await fetch('/voice/token', { method: 'POST', headers: {'Content-Type': 'application/json'} });
                        const td = await tr.json();
                        if (tr.ok && td.token) {
                            voipDevice.updateToken(td.token);
                            console.log('[VoIP] Token refreshed');
                        }
                    } catch(e) {
                        console.error('[VoIP] Token refresh failed:', e);
                    }
                });

                // Handle incoming calls (agent intercept OR human dialer mode)
                voipDevice.on('incoming', (call) => {
                    console.log('[VoIP] Incoming call from:', call.parameters?.From || 'unknown', '| _takingOver:', _takingOver, '| mode:', dialerMode, '| hasActive:', !!voipConnection);
                    // Accept if: (a) agent just clicked Intercept, OR (b) already in human dialer mode
                    const shouldAccept = _takingOver || dialerMode === 'human';
                    // Multi-line guard: if we already have an active VoIP call, reject
                    // the extra incoming call (Pro Dialer fires multiple calls, only
                    // the first to connect bridges to the human — the rest drop)
                    if (shouldAccept && voipConnection) {
                        console.log('[VoIP] Already on a call — rejecting extra incoming (multi-line collision)');
                        call.reject();
                        return;
                    }
                    if (shouldAccept) {
                        const reason = _takingOver ? 'intercept takeover' : 'human mode';
                        console.log('[VoIP] Auto-accepting incoming call (' + reason + ')');
                        _takingOver = false; // reset flag immediately

                        _stopRingTone(); // <-- Stop the ringing when the agent connects
                        
                        call.accept();
                        voipConnection = call;
                        voipStartTimer();
                        // In human dial mode, the ringing banner is shown by _origStartCall.
                        // Hide it now — voipCallPanel takes over as the in-call UI.
                        if (dialerMode === 'human') dialerHideBanner();
                        // Show call panel and update contact info
                        const callPanel = document.getElementById('voipCallPanel');
                        if (callPanel) callPanel.style.display = 'flex';
                        // Update name/phone display if we have active contact
                        if (dialerActiveContact) {
                            const nameEl = document.getElementById('voipCallName');
                            const phoneEl = document.getElementById('voipCallPhone');
                            if (nameEl) nameEl.textContent = dialerActiveContact.name || dialerActiveContact.firstName || 'Lead';
                            if (phoneEl) phoneEl.textContent = dialerActiveContact.phone || '';
                        }
                        // Mic mute button is now active
                        const muteMicBtn = document.getElementById('dialerMuteMicBtn');
                        if (muteMicBtn) { muteMicBtn.disabled = false; muteMicBtn.style.cursor = 'pointer'; }

                        call.on('disconnect', () => {
                            console.log('[VoIP] Incoming call disconnected');
                            const _endedSid = dialerCallSid;
                            voipConnection = null;
                            voipStopTimer();
                            const cp = document.getElementById('voipCallPanel');
                            if (cp) cp.style.display = 'none';
                            const kp = document.getElementById('voipKeypad');
                            if (kp) kp.style.display = 'none';
                            dialerHideBanner();
                            if (dialerQueueRunning) {
                                // Fetch final status for AMD/retry parity with AI mode
                                (async () => {
                                    let finalStatus = 'completed';
                                    if (_endedSid) {
                                        try {
                                            const sr = await fetch('/voice/call-status/' + _endedSid);
                                            if (sr.ok) {
                                                const sd = await sr.json();
                                                if (sd._amd_result) {
                                                    finalStatus = 'no-answer';
                                                } else if (['no-answer','busy','failed','canceled'].includes(sd.status)) {
                                                    finalStatus = sd.status;
                                                }
                                            }
                                        } catch(e) { /* non-fatal */ }
                                    }
                                    if (dialerCallIdx >= 0 && dialerCallIdx < dialerQueue.length) {
                                        dialerQueue[dialerCallIdx].status = finalStatus;
                                    }
                                    dialerCallSid = null;
                                    dialerRenderQueue();
                                    _dialerQueueTimeout(dialerAdvance, 1200);
                                })();
                            }
                        });
                        call.on('cancel', () => {
                            console.log('[VoIP] Incoming call cancelled');
                            voipConnection = null;
                            voipStopTimer();
                            const cp = document.getElementById('voipCallPanel');
                            if (cp) cp.style.display = 'none';
                            const kp = document.getElementById('voipKeypad');
                            if (kp) kp.style.display = 'none';
                            if (dialerQueueRunning) _dialerQueueTimeout(dialerAdvance, 2000);
                        });
                        call.on('error', (err) => {
                            console.error('[VoIP] Incoming call error:', err);
                            voipConnection = null;
                            voipStopTimer();
                            const cp = document.getElementById('voipCallPanel');
                            if (cp) cp.style.display = 'none';
                            dialerHideBanner();
                            if (dialerQueueRunning) _dialerQueueTimeout(dialerAdvance, 2000);
                        });
                    } else {
                        console.log('[VoIP] Rejecting incoming call (not in intercept/human mode)');
                        call.reject();
                    }
                });

                // Listen for device changes (headset plugged in/out)
                if (voipDevice.audio) {
                    voipDevice.audio.on('deviceChange', () => {
                        console.log('[VoIP] Audio device change detected');
                        refreshAudioDevices();
                    });
                }

                // Register with Twilio signaling
                console.log('[VoIP] Calling voipDevice.register()...');
                try {
                    await voipDevice.register();
                    console.log('[VoIP] register() promise resolved, state:', voipDevice.state);
                } catch(regErr) {
                    console.error('[VoIP] register() rejected:', regErr);
                    console.error('[VoIP] register() rejection type:', typeof regErr);
                    if (regErr && typeof regErr === 'object') {
                        console.error('[VoIP] register() rejection keys:', Object.keys(regErr));
                        console.error('[VoIP] register() rejection code:', regErr.code, 'twilioError:', regErr.twilioError);
                    }
                    const regMsg = (regErr && regErr.message) ? regErr.message
                                 : (typeof regErr === 'string' ? regErr : 'Registration failed (check console for details)');
                    _showVoipStatus('VoIP error: ' + regMsg);
                    setVoipBtnState('error');
                    _voipInitializing = false;
                    return;
                }

                // Timeout: if not registered within 15s, something is wrong
                setTimeout(() => {
                    if (!registrationDone && _voipInitializing) {
                        console.error('[VoIP] Registration timeout — no registered event after 15s');
                        console.log('[VoIP] Device state:', voipDevice?.state);
                        _showVoipStatus('Connection timeout — click to retry');
                        setVoipBtnState('error');
                        _voipInitializing = false;
                    }
                }, 15000);

            } catch(e) {
                console.error('[VoIP] initVoIPDevice error:', e);
                console.error('[VoIP] Error type:', typeof e, 'keys:', e ? Object.keys(e) : 'null/undefined');
                const errMsg = (e && e.message) ? e.message : (typeof e === 'string' ? e : JSON.stringify(e));
                _showVoipStatus('Error: ' + errMsg);
                setVoipBtnState('error');
                _voipInitializing = false;
            }
        }

        // ── Audio Device Management ──
        async function refreshAudioDevices() {
            try {
                const devices = await navigator.mediaDevices.enumerateDevices();
                const inputSelect = document.getElementById('audioInputDevice');
                const outputSelect = document.getElementById('audioOutputDevice');
                if (!inputSelect || !outputSelect) return;

                // Save current selections
                const curInput = inputSelect.value || _selectedInputDeviceId;
                const curOutput = outputSelect.value || _selectedOutputDeviceId;

                const optStyle = 'background:#2a2a35;color:#fff;';

                // Clear and rebuild input list
                inputSelect.innerHTML = '<option value="" style="' + optStyle + '">Default microphone</option>';
                devices.filter(d => d.kind === 'audioinput').forEach(d => {
                    const opt = document.createElement('option');
                    opt.value = d.deviceId;
                    opt.textContent = d.label || ('Microphone ' + d.deviceId.slice(0,8));
                    opt.style.cssText = optStyle;
                    if (d.deviceId === curInput) opt.selected = true;
                    inputSelect.appendChild(opt);
                });

                // Clear and rebuild output list
                outputSelect.innerHTML = '<option value="" style="' + optStyle + '">Default speaker</option>';
                devices.filter(d => d.kind === 'audiooutput').forEach(d => {
                    const opt = document.createElement('option');
                    opt.value = d.deviceId;
                    opt.textContent = d.label || ('Speaker ' + d.deviceId.slice(0,8));
                    opt.style.cssText = optStyle;
                    if (d.deviceId === curOutput) opt.selected = true;
                    outputSelect.appendChild(opt);
                });

                const statusEl = document.getElementById('audioDeviceStatus');
                const inputCount = devices.filter(d => d.kind === 'audioinput').length;
                const outputCount = devices.filter(d => d.kind === 'audiooutput').length;
                if (statusEl) statusEl.textContent = `${inputCount} mic${inputCount !== 1 ? 's' : ''}, ${outputCount} speaker${outputCount !== 1 ? 's' : ''} found`;

            } catch(e) {
                console.error('[Audio] Failed to enumerate devices:', e);
                const statusEl = document.getElementById('audioDeviceStatus');
                if (statusEl) statusEl.textContent = 'Could not list devices — check mic permission';
            }
        }

        function setAudioInputDevice(deviceId) {
            _selectedInputDeviceId = deviceId;
            _lsSet('voip_input_device', deviceId);
            // Apply to Twilio Device audio helper if available
            if (voipDevice && voipDevice.audio && deviceId) {
                voipDevice.audio.setInputDevice(deviceId)
                    .then(() => console.log('[Audio] Input device set:', deviceId))
                    .catch(e => console.error('[Audio] Failed to set input:', e));
            }
        }

        function setAudioOutputDevice(deviceId) {
            _selectedOutputDeviceId = deviceId;
            _lsSet('voip_output_device', deviceId);
            // Apply to Twilio Device speaker output if available
            if (voipDevice && voipDevice.audio && voipDevice.audio.speakerDevices && deviceId) {
                voipDevice.audio.speakerDevices.set(deviceId)
                    .then(() => console.log('[Audio] Output device set:', deviceId))
                    .catch(e => console.error('[Audio] Failed to set output:', e));
            }
        }

        function testAudioOutput() {
            // Play a short test tone through the selected output
            if (voipDevice && voipDevice.audio && voipDevice.audio.speakerDevices) {
                voipDevice.audio.speakerDevices.test()
                    .then(() => {
                        const s = document.getElementById('audioDeviceStatus');
                        if (s) s.textContent = 'Test tone played';
                    })
                    .catch(e => {
                        console.error('[Audio] Speaker test failed:', e);
                        // Fallback: play a tone via AudioContext
                        _playTestTone();
                    });
            } else {
                _playTestTone();
            }
        }

        function _playTestTone() {
            try {
                const ctx = new (window.AudioContext || window.webkitAudioContext)();
                const osc = ctx.createOscillator();
                const gain = ctx.createGain();
                osc.connect(gain); gain.connect(ctx.destination);
                osc.frequency.value = 440;
                gain.gain.value = 0.3;
                osc.start(); osc.stop(ctx.currentTime + 0.5);
                const s = document.getElementById('audioDeviceStatus');
                if (s) s.textContent = 'Test tone played';
            } catch(e) {
                console.error('[Audio] Test tone failed:', e);
            }
        }

        // Override startCall for dual mode
        const _origStartCall = dialerStartCall;
        dialerStartCall = async function(phone, firstName, contactId, displayName) {
            if (dialerMode === 'human') {
                if (!voipReady || !voipDevice) {
                    if (_voipInitializing) {
                        _showVoipStatus('Connecting — try again in a moment…');
                        return;
                    }
                    // Silently set up VoIP on first dial — no manual step needed
                    if (!voipSetupDone) {
                        _showVoipStatus('Setting up voice — one moment…');
                        await setupVoIP(); // creates credential + inits device
                    } else {
                        _showVoipStatus('Connecting voice…');
                        await initVoIPDevice();
                    }
                    // Wait for registration to complete
                    await new Promise(r => setTimeout(r, 2500));
                    if (!voipReady) {
                        _showVoipStatus('Voice connecting — try dialing again in a moment');
                        return;
                    }
                }
                // Route through /voice/dial (same as AI mode) so call history, Redis
                // tracking, AMD detection, and retry logic all work correctly.
                // The backend bridges to browser via <Dial><Client>, VoIP auto-accepts below.
                await _origStartCall(phone, firstName, contactId, displayName);
            } else {
                await _origStartCall(phone, firstName, contactId, displayName);
            }
        };

        async function voipMakeCall(phone, firstName, contactId, displayName) {
            if (!voipDevice || !voipReady) {
                _showVoipStatus('VoIP not ready. Click Setup VoIP first.');
                return;
            }
            // Block double-connect — reject if a VoIP call is already active or in-flight
            if (voipConnection || _isDialing) {
                console.warn('[VoIP] Blocked double-dial: VoIP call already active or in-flight');
                return;
            }
            _isDialing = true;  // Guard against double-dial during connect()
            voipCurrentContact = { phone, firstName, contactId, displayName };
            document.getElementById('voipCallName').textContent = displayName || firstName;
            document.getElementById('voipCallPhone').textContent = phone;
            document.getElementById('voipCallTimer').textContent = '00:00';

            try {
                // V2 SDK: device.connect() returns a Promise<Call>
                const connectParams = { params: { To: phone } };

                // Apply selected input device if still available, fall back gracefully
                if (_selectedInputDeviceId) {
                    try {
                        const devices = await navigator.mediaDevices.enumerateDevices();
                        const stillExists = devices.some(d => d.kind === 'audioinput' && d.deviceId === _selectedInputDeviceId);
                        if (stillExists) {
                            connectParams.rtcConstraints = { audio: { deviceId: { ideal: _selectedInputDeviceId } } };
                        } else {
                            console.warn('[VoIP] Saved input device no longer available, using default mic');
                            _selectedInputDeviceId = '';
                            _lsDel('voip_input_device');
                        }
                    } catch(devErr) {
                        console.warn('[VoIP] Could not enumerate devices:', devErr);
                    }
                }

                const call = await voipDevice.connect(connectParams);
                voipConnection = call;
                _isDialing = false;  // Connect resolved — guard released
                console.log('[VoIP] Call initiated, waiting for connection...');

                // Handle hang-up-while-dialing: if dialerStopQueue() fired during connect()
                if (_hangupPending) {
                    _hangupPending = false;
                    console.warn('[VoIP] Hang up was pending — disconnecting immediately');
                    voipConnection.disconnect();
                    voipConnection = null;
                    return;
                }

                // Show call panel immediately with connecting status
                document.getElementById('voipCallPanel').style.display = 'flex';
                document.getElementById('voipCallTimer').textContent = 'Connecting...';
                // Reset transfer button for new call
                const _xferBtn = document.getElementById('voipTransferBtn');
                if (_xferBtn) { _xferBtn.disabled = false; _xferBtn.innerHTML = '<i class="fa-solid fa-arrow-right-arrow-left"></i>'; }

                // V2 SDK: attach event listeners to the Call object
                call.on('ringing', (hasEarlyMedia) => {
                    console.log('[VoIP] Call ringing, earlyMedia:', hasEarlyMedia);
                    document.getElementById('voipCallTimer').textContent = 'Ringing...';
                });

                call.on('accept', () => {
                    console.log('[VoIP] Call accepted/connected');
                    // Capture Twilio call SID so we can poll final status on disconnect
                    const _voipCallSid = call.parameters?.CallSid || '';
                    if (_voipCallSid) dialerCallSid = _voipCallSid;
                    voipStartTimer();
                });

                call.on('disconnect', () => {
                    console.log('[VoIP] Call disconnected');
                    const _endedSid = dialerCallSid;
                    voipConnection = null;
                    voipStopTimer();
                    document.getElementById('voipCallPanel').style.display = 'none';
                    document.getElementById('voipKeypad').style.display = 'none';
                    dialerHideBanner();
                    // Fetch final call status for AMD detection + correct queue item status (enables retry logic)
                    const _finalize = async () => {
                        let finalStatus = 'completed';
                        if (_endedSid) {
                            try {
                                const sr = await fetch('/voice/call-status/' + _endedSid);
                                if (sr.ok) {
                                    const sd = await sr.json();
                                    if (sd._amd_result) {
                                        finalStatus = 'no-answer';
                                        if (dialerCallIdx >= 0 && dialerCallIdx < dialerQueue.length) {
                                            if (_dialerAutoDispVoicemail) dialerQueue[dialerCallIdx].disposition = 'voicemail';
                                        }
                                        console.log('[VoIP] AMD detected: ' + sd._amd_result + ' — marking no-answer for retry');
                                    } else if (['no-answer','busy','failed','canceled'].includes(sd.status)) {
                                        finalStatus = sd.status;
                                    }
                                }
                            } catch(e) { console.warn('[VoIP] Final status check failed (non-fatal):', e.message); }
                        }
                        if (dialerCallIdx >= 0 && dialerCallIdx < dialerQueue.length) {
                            dialerQueue[dialerCallIdx].status = finalStatus;
                        }
                        dialerCallSid = null;
                        dialerRenderQueue();
                        if (!dialerQueueRunning) {
                            dialerShowDisposition();
                        } else {
                            _dialerQueueTimeout(dialerAdvance, 1200);
                        }
                    };
                    _finalize();
                });

                call.on('cancel', () => {
                    console.log('[VoIP] Call cancelled (no-answer)');
                    voipConnection = null;
                    voipStopTimer();
                    dialerCallSid = null;
                    document.getElementById('voipCallPanel').style.display = 'none';
                    document.getElementById('voipKeypad').style.display = 'none';
                    // Mark no-answer so retry logic fires in dialerAdvance()
                    if (dialerCallIdx >= 0 && dialerCallIdx < dialerQueue.length) {
                        dialerQueue[dialerCallIdx].status = 'no-answer';
                        if (_dialerAutoDispNoAnswer) dialerQueue[dialerCallIdx].disposition = 'not_answered';
                        dialerRenderQueue();
                    }
                    if (!dialerQueueRunning) {
                        dialerShowDisposition();
                    } else {
                        _dialerQueueTimeout(dialerAdvance, 1200);
                    }
                });

                call.on('error', (err) => {
                    console.error('[VoIP] Call error:', err);
                    console.error('[VoIP] Error details — code:', err?.code, 'twilioError:', err?.twilioError, 'message:', err?.message);
                    _showVoipStatus('Call error: ' + (err?.message || JSON.stringify(err)));
                    voipConnection = null;
                    voipStopTimer();
                    document.getElementById('voipCallPanel').style.display = 'none';
                    // Mark queue item as failed so retry logic can kick in
                    if (dialerCallIdx >= 0 && dialerCallIdx < dialerQueue.length) {
                        dialerQueue[dialerCallIdx].status = 'failed';
                        dialerRenderQueue();
                    }
                    dialerHideBanner();
                    if (dialerQueueRunning) _dialerQueueTimeout(dialerAdvance, 2000);
                });

                call.on('warning', (name, data) => {
                    console.warn('[VoIP] Call warning:', name, data);
                });

                if (dialerCallIdx >= 0 && dialerCallIdx < dialerQueue.length) {
                    dialerQueue[dialerCallIdx].status = 'in-progress';
                    dialerRenderQueue();
                }
            } catch(e) {
                _isDialing = false;  // Release guard on failure
                _hangupPending = false;
                console.error('[VoIP] Connect failed:', e);
                _showVoipStatus('Call failed: ' + e.message);
            }
        }

        let _voipMuted = false;
        function voipToggleMute() {
            if (!voipConnection) return;
            _voipMuted = !_voipMuted;
            voipConnection.mute(_voipMuted);
            const btn = document.getElementById('voipMuteBtn');
            btn.style.background = _voipMuted ? 'rgba(239,68,68,0.15)' : 'rgba(255,255,255,0.04)';
            btn.style.color = _voipMuted ? '#ef4444' : '#ccc';
            btn.innerHTML = _voipMuted ? '<i class="fa-solid fa-microphone-slash"></i>' : '<i class="fa-solid fa-microphone"></i>';
        }
        function voipToggleKeypad() { const kp = document.getElementById('voipKeypad'); kp.style.display = kp.style.display === 'none' ? 'block' : 'none'; }
        function voipSendDTMF(digit) { if (voipConnection) voipConnection.sendDigits(digit); }
        function voipHangup() { if (voipConnection) voipConnection.disconnect(); else if (voipDevice) voipDevice.disconnectAll(); }

        // VoIP transfer now uses the same warm transfer system
        function voipTransfer() { _toggleWarmTransferPopover(); }

        function voipStartTimer() {
            voipTimerSeconds = 0; voipStopTimer();
            voipTimerInterval = setInterval(() => { voipTimerSeconds++; const m = Math.floor(voipTimerSeconds/60); const s = voipTimerSeconds%60; document.getElementById('voipCallTimer').textContent = String(m).padStart(2,'0')+':'+String(s).padStart(2,'0'); }, 1000);
        }
        function voipStopTimer() { if (voipTimerInterval) { clearInterval(voipTimerInterval); voipTimerInterval = null; } }

        // ===== NUMBERS TAB =====
        let _numbersCache = null;
        let _trustCache = null;

        async function loadNumbersTab() {
            const container = document.getElementById('numbersListContainer');
            if (!container) { console.error('[Numbers] numbersListContainer not found'); return; }
            container.innerHTML = '<div style="text-align:center;padding:20px;"><i class="fa-solid fa-spinner fa-spin" style="color:#00d9ff;font-size:1.2rem;"></i><div style="color:#888;font-size:.78rem;margin-top:6px;">Loading numbers...</div></div>';
            try {
                const r = await fetch('/voice/numbers');
                const d = await r.json();
                console.log('[Numbers] Response:', r.status, d);
                if (!r.ok || d.error) {
                    container.innerHTML = '<div style="padding:16px;background:rgba(239,68,68,0.08);border:1px solid rgba(239,68,68,0.2);border-radius:8px;color:#ef4444;font-size:.82rem;"><i class="fa-solid fa-triangle-exclamation me-1"></i>' + _esc(d.error || 'Failed to load numbers') + '</div>';
                    return;
                }
                _numbersCache = d.numbers || [];
                // Track free numbers remaining for the buy flow
                if (typeof _numFreeRemaining !== 'undefined') {
                    _numFreeRemaining = d.free_remaining ?? null;
                } else {
                    window._numFreeRemaining = d.free_remaining ?? null;
                }
                if (!_numbersCache.length) {
                    container.innerHTML = '<div style="text-align:center;padding:30px 20px;color:#888;font-size:.82rem;">' +
                        '<i class="fa-solid fa-phone-slash" style="font-size:1.5rem;display:block;margin-bottom:8px;color:#444;"></i>' +
                        'No numbers found on your account.<br>Click <strong style="color:#00d9ff;">Buy Number</strong> above to get started.</div>';
                    return;
                }
                _renderNumbersTable(_numbersCache, container);
            } catch(e) {
                console.error('[Numbers] Error:', e);
                container.innerHTML = '<div style="padding:16px;background:rgba(239,68,68,0.08);border:1px solid rgba(239,68,68,0.2);border-radius:8px;color:#ef4444;font-size:.82rem;"><i class="fa-solid fa-triangle-exclamation me-1"></i>Network error loading numbers. Check console for details.</div>';
            }
        }

        function _esc(s) { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }
        function _fmtPhone(p) {
            if (!p) return '';
            const d = p.replace(/\D/g, '');
            if (d.length === 11 && d[0] === '1') return '+1 (' + d.substr(1,3) + ') ' + d.substr(4,3) + '-' + d.substr(7);
            if (d.length === 10) return '(' + d.substr(0,3) + ') ' + d.substr(3,3) + '-' + d.substr(6);
            return p;
        }

        function _renderNumbersTable(numbers, container) {
            let html = '<table class="vnum-table" style="width:100%;border-collapse:collapse;">';
            // Header
            html += '<thead><tr class="numbers-grid-header">';
            html += '<th class="vnum-hdr" style="text-align:left;">Number</th>';
            html += '<th class="vnum-hdr" style="text-align:center;width:80px;">Status</th>';
            html += '<th class="vnum-hdr" style="text-align:center;width:55px;">Voice</th>';
            html += '<th class="vnum-hdr" style="text-align:center;width:55px;">SMS</th>';
            html += '<th class="vnum-hdr" style="text-align:center;width:55px;">CNAM</th>';
            html += '<th class="vnum-hdr" style="text-align:center;width:44px;"></th>';
            html += '</tr></thead><tbody>';
            // Rows
            numbers.forEach(n => {
                const statusClass = n.status === 'active' ? 'vnum-status-active' : (n.status === 'pending' ? 'vnum-status-pending' : 'vnum-status-other');
                const primaryBadge = n.is_primary ? '<span class="vnum-primary">PRIMARY</span>' : '';
                const nickname = n.nickname ? '<span class="vnum-nickname">(' + _esc(n.nickname) + ')</span>' : '';
                const voiceIcon = n.capabilities?.voice ? '<i class="fa-solid fa-circle-check vnum-check"></i>' : '<i class="fa-solid fa-circle-xmark vnum-xmark"></i>';
                const smsIcon = n.capabilities?.sms ? '<i class="fa-solid fa-circle-check vnum-check"></i>' : '<i class="fa-solid fa-circle-xmark vnum-xmark"></i>';
                const cnamIcon = n.cnam_listed ? '<i class="fa-solid fa-circle-check vnum-cnam-on" title="CNAM enabled — click to disable" onclick="toggleCNAM(\'' + n.sid + '\',false)"></i>' : '<i class="fa-regular fa-circle vnum-cnam-off" title="CNAM disabled — click to enable" onclick="toggleCNAM(\'' + n.sid + '\',true)"></i>';
                html += '<tr class="numbers-grid-row">';
                html += '<td class="vnum-cell"><span class="vnum-phone">' + _esc(_fmtPhone(n.phone)) + '</span>' + primaryBadge + nickname + '<br><span class="vnum-type">' + _esc(n.number_type || 'local') + '</span></td>';
                html += '<td class="vnum-cell" style="text-align:center;"><span class="' + statusClass + '">' + (n.status || 'active') + '</span></td>';
                html += '<td class="vnum-cell" style="text-align:center;">' + voiceIcon + '</td>';
                html += '<td class="vnum-cell" style="text-align:center;">' + smsIcon + '</td>';
                html += '<td class="vnum-cell" style="text-align:center;">' + cnamIcon + '</td>';
                html += '<td class="vnum-cell" style="text-align:center;"><div style="position:relative;display:inline-block;">' +
                    '<button onclick="toggleNumMenu(this)" class="vnum-menu-btn">&#8942;</button>' +
                    '<div class="num-menu vnum-menu">' +
                    (!n.is_primary ? '<button onclick="setPrimaryNumber(\'' + _esc(n.phone) + '\')" class="vnum-menu-item">Set as Primary</button>' : '') +
                    '<button onclick="promptNickname(\'' + _esc(n.phone) + '\')" class="vnum-menu-item">Edit Nickname</button>' +
                    '<button onclick="releaseNumber(\'' + (n.sid || '') + '\',\'' + _esc(n.phone) + '\')" class="vnum-menu-item vnum-menu-item-danger">Release Number</button>' +
                    '</div></div></td>';
                html += '</tr>';
            });
            html += '</tbody></table>';
            // Summary info
            html += '<div class="vnum-summary">' +
                '<strong>' + numbers.length + ' number' + (numbers.length !== 1 ? 's' : '') + '</strong> on your account. ' +
                'STIR/SHAKEN is auto-managed. Register with carriers in the <strong class="vnum-summary-link" onclick="switchVoiceSubtab(\'spammonitoring\')">Spam Monitoring</strong> tab to reduce spam flags.' +
                '</div>';
            container.innerHTML = html;
        }

        function toggleNumMenu(btn) {
            const menu = btn.nextElementSibling;
            const isOpen = menu.style.display === 'block';
            document.querySelectorAll('.num-menu').forEach(m => m.style.display = 'none');
            if (isOpen) return;
            menu.style.display = 'block';
            const closer = (e) => { if (!btn.contains(e.target) && !menu.contains(e.target)) { menu.style.display = 'none'; document.removeEventListener('click', closer); } };
            setTimeout(() => document.addEventListener('click', closer), 0);
        }

        async function toggleCNAM(numberId, enable) {
            try {
                const r = await fetch('/voice/numbers/' + numberId + '/cnam', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ enable: enable }) });
                const d = await r.json();
                if (r.ok) loadNumbersTab();
                else alert(d.error || 'Failed to update CNAM');
            } catch(e) { console.error('[Numbers] toggleCNAM network error:', e); alert('Network error'); }
        }

        async function setPrimaryNumber(phone) {
            try {
                const r = await fetch('/voice/numbers/set-primary', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ phone: phone }) });
                if (r.ok) { loadNumbersTab(); alert('Primary number set to ' + phone); }
            } catch(e) { console.error('[Numbers] setPrimaryNumber network error:', e); alert('Network error'); }
        }

        function promptNickname(phone) {
            const current = (_numbersCache || []).find(n => n.phone === phone);
            const name = prompt('Nickname for ' + phone + ':', current?.nickname || '');
            if (name === null) return;
            fetch('/voice/numbers/nickname', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ phone: phone, nickname: name }) })
                .then(r => { if (r.ok) loadNumbersTab(); })
                .catch(e => { console.error('[Numbers] promptNickname network error:', e); alert('Network error'); });
        }

        // ── Call Count Badge ────────────────────────────────────────────────────

        // contactId → local call count (loaded in batch after fetchContacts)
        let _dialerCallCounts = {};

        // POST bulk IDs as JSON body to avoid Gunicorn URL length limits (4094 default)
        function _bulkPost(url, ids) {
            return fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ids: ids})});
        }

        // After contacts are loaded, batch-fetch local counts and re-render badges
        async function dialerFetchCallCounts() {
            if (!dialerContacts.length) return;
            const CHUNK = 100;
            try {
                for (let i = 0; i < dialerContacts.length; i += CHUNK) {
                    const chunk = dialerContacts.slice(i, i + CHUNK);
                    const ids = chunk.map(c => c.id).join(',');
                    const r = await _bulkPost('/voice/contact-call-counts', ids);
                    if (!r.ok) continue;
                    const counts = await r.json();
                    Object.assign(_dialerCallCounts, counts);
                }
                dialerRenderContactBadges();
            } catch(e) {
                // Non-critical; badges just stay empty
            }
        }

        // ── InsuranceGrokBot: Bulk Engagement Fetch ──
        async function igbFetchBulkEngagement() {
            if (!dialerContacts.length) return;
            const CHUNK = 100;
            for (let i = 0; i < dialerContacts.length; i += CHUNK) {
                const chunk = dialerContacts.slice(i, i + CHUNK);
                const ids = chunk.map(c => c.id).join(',');
                try {
                    const r = await _bulkPost('/voice/contact-engagement', ids);
                    if (!r.ok) continue;
                    const data = await r.json();
                    Object.assign(_igbEngagementCache, data);
                } catch(e) {
                    console.error('[IGB] Engagement fetch failed:', e);
                }
            }
            // Re-render with engagement data, then fetch AI intelligence
            dialerRenderContacts();
            igbFetchBulkIntelligence();
        }

        // ── InsuranceGrokBot: Bulk AI Intelligence Fetch ──
        // Fetches cached AI data (24h TTL), shows it immediately, then queues
        // ALL contacts for fresh analysis. Server skips contacts with fresh cache.
        async function igbFetchBulkIntelligence() {
            if (!dialerContacts.length) return;
            const CHUNK = 300;
            _igbUncachedIds = [];

            // Skip contacts with bad names — no point analyzing "None None" contacts
            const validContacts = dialerContacts.filter(c => !_igbIsBadName(c));
            for (let i = 0; i < validContacts.length; i += CHUNK) {
                const chunk = validContacts.slice(i, i + CHUNK);
                const ids = chunk.map(c => c.id).join(',');
                try {
                    const r = await _bulkPost('/voice/contact-intelligence-bulk', ids);
                    if (!r.ok) continue;
                    const data = await r.json();
                    // Merge cached AI data (show immediately while fresh analysis runs)
                    if (data.cached) {
                        Object.assign(_igbIntelCache, data.cached);
                    }
                    // Track uncached contacts for batch analysis
                    if (data.uncached) _igbUncachedIds.push(...data.uncached);
                } catch(e) {
                    console.error('[IGB] Intelligence bulk fetch failed:', e);
                }
            }

            // Re-render with whatever cache we have
            dialerRenderContacts();

            // Only queue contacts that have no fresh cache — never re-analyze on every load.
            if (_igbUncachedIds.length > 0 && !_igbAnalyzing) {
                igbRunBatchAnalysis();
            }
        }

        // ── InsuranceGrokBot: Batch Analysis (inline, rule-based) ──
        // Scoring is rule-based (no AI cost, milliseconds) — server scores inline
        // and results are available immediately. No RQ polling needed.
        async function igbRunBatchAnalysis() {
            if (_igbAnalyzing || !_igbUncachedIds.length) return;
            _igbAnalyzing = true;
            const totalPending = _igbUncachedIds.length;
            console.log('[IGB] Scoring', totalPending, 'contacts (rule-based, inline)');

            // Send uncached IDs in chunks — server scores them inline and caches
            const CHUNK = 300;
            for (let i = 0; i < _igbUncachedIds.length; i += CHUNK) {
                const batch = _igbUncachedIds.slice(i, i + CHUNK);
                try {
                    const r = await fetch('/voice/contact-intelligence-analyze', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ contact_ids: batch })
                    });
                    if (!r.ok) continue;
                    const data = await r.json();
                    console.log('[IGB] Scored chunk:', data.analyzed, 'new,', data.cached, 'already cached');
                } catch(e) {
                    console.error('[IGB] Scoring request failed:', e);
                }
            }

            // Results are now cached — fetch them all in one bulk read
            try {
                const allIds = _igbUncachedIds.slice();
                let totalNew = 0;
                for (let i = 0; i < allIds.length; i += CHUNK) {
                    const chunkIds = allIds.slice(i, i + CHUNK).join(',');
                    const r = await _bulkPost('/voice/contact-intelligence-bulk', chunkIds);
                    if (!r.ok) continue;
                    const data = await r.json();
                    if (data.cached) {
                        Object.entries(data.cached).forEach(function([cid, intel]) {
                            if (!_igbIntelCache[cid]) totalNew++;
                            _igbIntelCache[cid] = intel;
                        });
                    }
                }
                _igbUncachedIds = _igbUncachedIds.filter(function(id) { return !_igbIntelCache[id]; });
                console.log('[IGB] Analysis complete: +' + totalNew + ' scored, ' + _igbUncachedIds.length + ' remaining, total cached:', Object.keys(_igbIntelCache).length);
            } catch(e) {
                console.error('[IGB] Bulk fetch after scoring failed:', e);
            }

            _igbAnalyzing = false;
            dialerRenderContacts();
        }

        // After local counts show, upgrade badges with GHL+WAVV counts from synced DB
        async function dialerFetchMergedCounts() {
            if (!dialerContacts.length) return;
            const CHUNK_SIZE = 300;
            for (let i = 0; i < dialerContacts.length; i += CHUNK_SIZE) {
                const chunk = dialerContacts.slice(i, i + CHUNK_SIZE);
                const ids = chunk.map(c => c.id).join(',');
                try {
                    const r = await _bulkPost('/voice/contact-call-counts/merged', ids);
                    if (!r.ok) continue;
                    const merged = await r.json();
                    // Only update badges where merged count is higher (additive; never regress)
                    Object.entries(merged).forEach(([id, total]) => {
                        if (total > (_dialerCallCounts[id] || 0)) {
                            _dialerCallCounts[id] = total;
                            const badge = document.querySelector('[data-call-badge="' + id + '"]');
                            if (badge) {
                                badge.textContent = 'Dials: ' + total;
                                badge.style.display = 'inline-flex';
                            }
                        }
                    });
                } catch(e) {
                    console.error('[Dialer] Merged call counts chunk failed:', e);
                }
            }
        }

        // Update call count badge for a specific contact row (without full re-render)
        function dialerUpdateContactBadge(contactId, total) {
            _dialerCallCounts[contactId] = total;
            const badge = document.querySelector('[data-call-badge="' + contactId + '"]');
            if (badge) {
                badge.textContent = 'Dials: ' + total;
            }
        }

        // Re-render just the badges without rebuilding the full list
        function dialerRenderContactBadges() {
            dialerContacts.forEach(c => {
                const count = _dialerCallCounts[c.id] || 0;
                const badge = document.querySelector('[data-call-badge="' + c.id + '"]');
                if (badge) {
                    badge.textContent = 'Dials: ' + count;
                }
            });
        }

        // Fetch merged (GHL + local) count when a contact is selected; update badge
        async function dialerFetchMergedCallCount(contactId) {
            try {
                const r = await fetch('/voice/contact/' + contactId + '/ghl-call-count');
                if (!r.ok) return;
                const d = await r.json();
                dialerUpdateContactBadge(contactId, d.total || 0);
            } catch(e) {
                // Silently fail; local count badge is already shown
            }
        }

        // ── Deep Sync (One-Time Historical GHL Pull — silent, no UI) ────────────

        let _deepSyncTriggered = false;
        let _deepSyncPollTimer = null;

        // Auto-trigger deep sync on first dialer load (runs silently in background)
        async function _deepSyncCheck() {
            if (_deepSyncTriggered) return;
            _deepSyncTriggered = true;

            try {
                const sr = await fetch('/api/sync/deep-pull/status');
                if (!sr.ok) return;
                const status = await sr.json();

                if (status.status === 'completed') return;

                if (status.status === 'running') {
                    _deepSyncPollSilent();
                    return;
                }

                // Not started, failed, or stale — trigger it silently
                const tr = await fetch('/api/sync/deep-pull', { method: 'POST' });
                if (!tr.ok) return;
                const result = await tr.json();

                if (result.status === 'started' || result.status === 'already_running') {
                    _deepSyncPollSilent();
                }
            } catch(e) {
                console.error('[DeepSync] Check failed:', e);
            }
        }

        // Poll silently until complete, then refresh contacts + call counts
        function _deepSyncPollSilent() {
            if (_deepSyncPollTimer) clearInterval(_deepSyncPollTimer);
            _deepSyncPollTimer = setInterval(async () => {
                try {
                    const r = await fetch('/api/sync/deep-pull/status');
                    if (!r.ok) return;
                    const status = await r.json();

                    if (status.status === 'completed') {
                        clearInterval(_deepSyncPollTimer);
                        _deepSyncPollTimer = null;
                        // Refresh contacts + call counts with new data
                        if (status.messages_synced > 0) {
                            dialerFetchCallCounts().then(() => dialerFetchMergedCounts());
                            dialerFetchContacts(true);
                        }
                    } else if (status.status === 'failed' || status.status === 'stale' || status.status === 'not_started') {
                        clearInterval(_deepSyncPollTimer);
                        _deepSyncPollTimer = null;
                    }
                } catch(e) {
                    // Keep polling — transient error
                }
            }, 8000); // Poll every 8 seconds (no UI to update, so less frequent)
        }


        // ── Statistics Panel ────────────────────────────────────────────────────

        let _dialerStatsPeriod = 'today';

        function dialerToggleStats() {
            const panel = document.getElementById('dialerStatsPanel');
            const isOpen = panel.style.display !== 'none';
            panel.style.display = isOpen ? 'none' : 'block';
            if (!isOpen) dialerLoadStats();
            // Update button highlight
            const btn = document.getElementById('dialerStatsToggle');
            if (btn) {
                btn.classList.toggle('dlr-stats-active', !isOpen);
                btn.style.color = '';  // Clear inline — let CSS handle theme
            }
        }

        function dialerSetStatsPeriod(period) {
            _dialerStatsPeriod = period;
            document.querySelectorAll('.dlr-stat-period').forEach(b => {
                const active = b.dataset.period === period;
                b.classList.toggle('active', active);
                b.style.background = active ? 'rgba(0,217,255,0.15)' : 'transparent';
                b.style.color = active ? '#00d9ff' : '#666';
            });
            dialerLoadStats();
        }

        async function dialerLoadStats() {
            const container = document.getElementById('dialerStatsContent');
            if (!container) return;
            container.innerHTML = '<div style="text-align:center;padding:40px;color:#555;"><i class="fa-solid fa-spinner fa-spin" style="color:#00d9ff;font-size:1.4rem;"></i></div>';
            try {
                const r = await _fetchRetry('/voice/stats?period=' + _dialerStatsPeriod, {}, { retries: 1, timeout: 15000, label: 'dialer-stats' });
                if (!r.ok) { container.innerHTML = '<div style="color:#888;text-align:center;padding:20px;">Could not load statistics.</div>'; return; }
                const s = await r.json();
                container.innerHTML = dialerRenderStats(s);
            } catch(e) {
                container.innerHTML = '<div style="color:#888;text-align:center;padding:20px;">Error loading statistics.</div>';
            }
        }

        // ── Stats Phone App ─────────────────────────────────────────────────────
        let _iosStatsPeriod = 'today';

        async function iosStatsLoad() {
            const container = document.getElementById('iosStatsContent');
            if (!container) return;
            container.innerHTML = '<div style="text-align:center;padding:40px;"><i class="fa-solid fa-spinner fa-spin" style="color:#00d9ff;font-size:1.4rem;"></i></div>';
            try {
                const r = await _fetchRetry('/voice/stats?period=' + _iosStatsPeriod, {}, { retries: 1, timeout: 15000, label: 'ios-stats' });
                if (!r.ok) { container.innerHTML = '<div style="color:#888;text-align:center;padding:20px;">Could not load statistics.</div>'; return; }
                const s = await r.json();
                container.innerHTML = dialerRenderStats(s);
            } catch(e) {
                container.innerHTML = '<div style="color:#888;text-align:center;padding:20px;">Error loading statistics.</div>';
            }
        }

        window.iosStatsRefresh = function() {
            const icon = document.getElementById('iosStatsRefreshIcon');
            if (icon) { icon.classList.add('fa-spin'); setTimeout(() => icon.classList.remove('fa-spin'), 1500); }
            iosStatsLoad();
        };

        window.iosStatsSetPeriod = function(period) {
            _iosStatsPeriod = period;
            document.querySelectorAll('.ios-stats-period').forEach(b => {
                b.classList.toggle('active', b.dataset.period === period);
            });
            iosStatsLoad();
        };

        // ── KPI Banner (top bar: Dials / Pickups / >2min / >10min) ──────────────
        async function dialerLoadKpiBanner() {
            try {
                const r = await fetch('/voice/stats?period=today');
                if (!r.ok) return;
                const s = await r.json();
                const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val ?? '0'; };
                set('kpiDials',   s.total_calls    ?? 0);
                set('kpiPickups', s.connected_calls ?? 0);
                set('kpiOver2',   s.over_2min      ?? 0);
                set('kpiOver10',  s.over_10min     ?? 0);
            } catch(e) { /* silent — banner stays at dashes */ }
        }

        function _fmtDuration(secs) {
            secs = Math.round(secs);
            if (secs < 60) return secs + 's';
            const m = Math.floor(secs / 60), s = secs % 60;
            if (m < 60) return m + 'm ' + (s ? s + 's' : '');
            const h = Math.floor(m / 60), rm = m % 60;
            return h + 'h ' + (rm ? rm + 'm' : '');
        }

        function dialerRenderStats(s) {
            const p = s.prior || {};

            // Delta badge: % change for counts, pp for rates
            function _delta(val, isPP) {
                if (val === null || val === undefined) return '';
                const sign  = val > 0 ? '+' : '';
                const color = val > 0 ? '#4ade80' : val < 0 ? '#ef4444' : '#888';
                const arrow = val > 0 ? '▲' : val < 0 ? '▼' : '';
                const suffix = isPP ? 'pp' : '%';
                return '<div style="font-size:0.78rem;margin-top:3px;color:' + color + ';font-weight:600;">' + arrow + ' ' + sign + val + suffix + '</div>';
            }

            // ── KPI CARDS ──
            const connectColor = s.connect_rate >= 20 ? '#4ade80' : s.connect_rate >= 10 ? '#ffa500' : '#ef4444';
            let html = '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:10px;margin-bottom:20px;">';
            html += _kpiCard(s.total_calls,                   'Total Dials',    '',          _delta(p.delta_calls));
            html += _kpiCard(s.outbound_calls,                'Outbound',       '',          '');
            html += _kpiCard(s.connected_calls,               'Connected',      '',          _delta(p.delta_connected));
            html += _kpiCard(s.connect_rate + '%',            'Connect Rate',   connectColor,_delta(p.delta_rate, true));
            html += _kpiCard(_fmtDuration(s.avg_duration),    'Avg Duration',   '',          '');
            html += _kpiCard(_fmtDuration(s.total_duration),  'Total Talk Time','#00d9ff',   _delta(p.delta_duration));
            html += _kpiCard(s.unique_contacts,               'Leads Dialed',   '',          '');
            html += _kpiCard(s.calls_per_day,                 'Calls / Day',    '',          '');
            html += '</div>';

            // ── DURATION | OUTCOME BREAKDOWN ──
            html += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:24px;margin-bottom:24px;">';

            // Left: Duration bars
            html += '<div>';
            html += '<div style="font-size:0.92rem;font-weight:700;color:#ccc;margin-bottom:12px;text-transform:uppercase;letter-spacing:0.5px;">Duration Breakdown</div>';
            const maxDur = Math.max(s.over_6s, 1);
            html += _durBar('6s+',    s.over_6s,    maxDur, '#00d9ff');
            html += _durBar('1 min',  s.over_1min,  maxDur, '#00b8d4');
            html += _durBar('2 min',  s.over_2min,  maxDur, '#00916a');
            html += _durBar('5 min',  s.over_5min,  maxDur, '#4ade80');
            html += _durBar('10 min', s.over_10min, maxDur, '#ffa500');
            html += '</div>';

            // Right: Outcome (disposition) breakdown
            html += '<div>';
            html += '<div style="font-size:0.92rem;font-weight:700;color:#ccc;margin-bottom:12px;text-transform:uppercase;letter-spacing:0.5px;">Outcome Breakdown</div>';
            const DISP_MAP = {
                'left_voicemail': { label: 'Left Voicemail',  color: '#a855f7' },
                'not_answered':   { label: 'No Answer',       color: '#6b7280' },
                'hung_up':        { label: 'Hung Up',         color: '#ef4444' },
                'not_interested': { label: 'Not Interested',  color: '#f97316' },
                'none':           { label: 'No Disposition',  color: '#4b5563' },
            };
            const disps = s.dispositions || {};
            const totalDisp = Object.values(disps).reduce((a, b) => a + b, 0);
            let outcomeList = Object.entries(disps).map(([k, v]) => {
                const info = DISP_MAP[k] || { label: k.replace(/_/g,' '), color: '#555' };
                return { label: info.label, count: v, color: info.color };
            });
            const undisposed = Math.max(0, s.total_calls - totalDisp);
            if (undisposed > 0) outcomeList.push({ label: 'Not Dispositioned', count: undisposed, color: '#2a2a3e' });
            outcomeList.sort((a, b) => b.count - a.count);
            const maxOutcome = Math.max(...outcomeList.map(o => o.count), 1);
            if (!outcomeList.length || s.total_calls === 0) {
                html += '<div style="color:#888;font-size:0.88rem;padding:8px 0;line-height:1.6;">No disposition data yet.<br><span style="color:#666;font-size:0.82rem;">Set call dispositions after each call to see your outcome breakdown here.</span></div>';
            } else {
                outcomeList.forEach(o => {
                    const pct = s.total_calls > 0 ? Math.round(o.count / s.total_calls * 100) : 0;
                    html += '<div class="dlr-bar-row" style="margin-bottom:6px;">' +
                        '<div style="min-width:120px;max-width:120px;font-size:0.88rem;color:#ccc;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">' + dialerEsc(o.label) + '</div>' +
                        '<div class="dlr-bar-track" style="height:10px;"><div class="dlr-bar-fill" style="width:' + Math.round(o.count / maxOutcome * 100) + '%;background:' + o.color + ';"></div></div>' +
                        '<div style="font-size:0.85rem;color:#ddd;min-width:64px;text-align:right;font-family:\'JetBrains Mono\',monospace;">' + pct + '% <span style="color:#888;">(' + o.count + ')</span></div>' +
                    '</div>';
                });
            }
            html += '</div>';
            html += '</div>'; // end 2-col grid

            // ── BEST HOURS TO CALL ──
            if (s.hourly && s.hourly.some(h => h.calls > 0)) {
                const HOUR_LABELS = ['12am','1am','2am','3am','4am','5am','6am','7am','8am','9am','10am','11am',
                                     '12pm','1pm','2pm','3pm','4pm','5pm','6pm','7pm','8pm','9pm','10pm','11pm'];
                const top3 = [...s.hourly].filter(h => h.calls > 0).sort((a,b) => b.calls - a.calls).slice(0, 3);
                if (top3.length) {
                    const rankColors = ['#4ade80','#00d9ff','#a78bfa'];
                    html += '<div style="margin-bottom:20px;padding:14px 18px;background:rgba(74,222,128,0.03);border:1px solid rgba(74,222,128,0.08);border-radius:10px;display:flex;align-items:center;gap:16px;">';
                    html += '<i class="fa-solid fa-clock" style="color:#4ade80;font-size:1.2rem;flex-shrink:0;"></i>';
                    html += '<div style="flex:1;">';
                    html += '<div style="font-size:0.92rem;font-weight:700;color:#ccc;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:10px;">Best Hours to Call</div>';
                    html += '<div style="display:flex;gap:10px;flex-wrap:wrap;">';
                    top3.forEach((h, i) => {
                        html += '<div style="background:rgba(255,255,255,0.03);border:1px solid ' + rankColors[i] + '55;border-radius:8px;padding:10px 20px;text-align:center;">' +
                            '<div style="font-size:1.15rem;font-weight:800;color:' + rankColors[i] + ';">' + HOUR_LABELS[h.hour] + '</div>' +
                            '<div style="font-size:0.82rem;color:#aaa;margin-top:3px;">' + h.calls + ' call' + (h.calls !== 1 ? 's' : '') + '</div>' +
                        '</div>';
                    });
                    html += '</div></div></div>';
                }
            }

            // ── DAILY VOLUME (dual-tone: total + connected) ──
            if (s.daily && s.daily.length > 1) {
                html += '<div style="margin-bottom:20px;">';
                html += '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;">';
                html += '<div style="font-size:0.92rem;font-weight:700;color:#ccc;text-transform:uppercase;letter-spacing:0.5px;">Daily Volume</div>';
                html += '<div style="display:flex;gap:14px;">' +
                    '<span style="font-size:0.82rem;color:#aaa;"><span style="color:rgba(0,217,255,0.6);">■</span> Dials</span>' +
                    '<span style="font-size:0.82rem;color:#aaa;"><span style="color:#4ade80;">■</span> Connected</span>' +
                '</div>';
                html += '</div>';
                const maxDay = Math.max(...s.daily.map(d => d.calls), 1);
                const barH   = 80;
                html += '<div style="display:flex;align-items:flex-end;gap:3px;height:' + (barH + 36) + 'px;overflow-x:auto;padding-bottom:2px;">';
                s.daily.forEach(d => {
                    const hTotal = Math.max(2, Math.round(d.calls / maxDay * barH));
                    const hConn  = d.calls > 0 ? Math.max(1, Math.round(d.connected / d.calls * hTotal)) : 0;
                    const label  = d.day ? d.day.substr(5) : '';
                    const talkLbl = d.total_secs ? Math.round(d.total_secs / 60) + 'm talk' : '';
                    html += '<div style="display:flex;flex-direction:column;align-items:center;gap:2px;flex:1;min-width:26px;" title="' + d.day + ': ' + d.calls + ' dials, ' + d.connected + ' connected' + (talkLbl ? ', ' + talkLbl : '') + '">' +
                        '<div style="font-size:0.75rem;color:#aaa;font-weight:600;">' + (d.calls > 0 ? d.calls : '') + '</div>' +
                        '<div style="width:100%;position:relative;height:' + hTotal + 'px;">' +
                            '<div style="position:absolute;bottom:0;left:0;right:0;height:' + hTotal + 'px;background:rgba(0,217,255,0.25);border-radius:3px 3px 0 0;"></div>' +
                            (hConn > 0 ? '<div style="position:absolute;bottom:0;left:0;right:0;height:' + hConn + 'px;background:rgba(74,222,128,0.65);border-radius:2px 2px 0 0;"></div>' : '') +
                        '</div>' +
                        '<div style="font-size:0.75rem;color:#aaa;white-space:nowrap;">' + label + '</div>' +
                    '</div>';
                });
                html += '</div></div>';
            }

            // ── MOST CONTACTED ──
            if (s.top_contacts && s.top_contacts.length) {
                html += '<div>';
                html += '<div style="font-size:0.92rem;font-weight:700;color:#ccc;margin-bottom:12px;text-transform:uppercase;letter-spacing:0.5px;">Most Contacted</div>';
                const maxTop = Math.max(...s.top_contacts.map(t => t.count), 1);
                s.top_contacts.forEach(tc => {
                    html += '<div class="dlr-bar-row" style="margin-bottom:5px;">' +
                        '<div class="dlr-bar-label" style="min-width:110px;max-width:110px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-align:left;font-size:0.88rem;color:#ccc;">' + dialerEsc(tc.name) + '</div>' +
                        '<div class="dlr-bar-track" style="height:10px;"><div class="dlr-bar-fill" style="width:' + Math.round(tc.count / maxTop * 100) + '%;background:rgba(0,217,255,0.5);"></div></div>' +
                        '<div class="dlr-bar-count" style="font-size:0.88rem;color:#ddd;">' + tc.count + '</div>' +
                    '</div>';
                });
                html += '</div>';
            }

            return html;
        }

        function _kpiCard(val, label, color, deltaHtml) {
            return '<div class="dlr-kpi-card"><div class="dlr-kpi-val" style="' + (color ? 'color:' + color + ';' : '') + '">' + val + '</div><div class="dlr-kpi-label">' + label + '</div>' + (deltaHtml || '') + '</div>';
        }

        function _durBar(label, count, maxVal, color) {
            const pct = maxVal > 0 ? Math.round(count / maxVal * 100) : 0;
            return '<div class="dlr-bar-row" style="margin-bottom:6px;">' +
                '<div class="dlr-bar-label" style="font-size:0.88rem;color:#ccc;">' + label + '</div>' +
                '<div class="dlr-bar-track" style="height:10px;"><div class="dlr-bar-fill" style="width:' + pct + '%;background:' + color + ';"></div></div>' +
                '<div class="dlr-bar-count" style="font-size:0.88rem;color:#ddd;">' + count + '</div>' +
            '</div>';
        }


        // ═══════════════════════════════════════════════════════════════════════
        // ═══ INBOX APP — Unified Conversations ═══════════════════════════════
        // ═══════════════════════════════════════════════════════════════════════

        let _inboxData = [];
        let _inboxAllData = [];
        let _inboxFilter = 'all';
        let _inboxSearchTimer = null;
        let _inboxOffset = 0;
        let _inboxHasMore = false;
        let _inboxLoading = false;
        // Track which contact IDs have truly-new unread messages since last opened
        let _inboxUnreadIds = new Set();
        // Background refresh interval (fallback for messages that don't hit webhook)
        let _inboxBgRefreshTimer = null;

        function inboxRefresh() {
            _inboxOffset = 0;
            _inboxHasMore = false;
            const icon = document.getElementById('inboxRefreshIcon');
            if (icon) icon.classList.add('fa-spin');
            _inboxFetch(false).finally(() => {
                if (icon) icon.classList.remove('fa-spin');
            });
        }

        function _inboxStartBgRefresh() {
            if (_inboxBgRefreshTimer) return;
            // Silent background refresh every 30s as fallback for any missed SSE events
            _inboxBgRefreshTimer = setInterval(() => {
                _inboxSilentRefresh();
            }, 30000);
        }

        function _inboxStopBgRefresh() {
            if (_inboxBgRefreshTimer) { clearInterval(_inboxBgRefreshTimer); _inboxBgRefreshTimer = null; }
        }

        async function _inboxSilentRefresh() {
            // Fetch latest conversations without showing a spinner or clobbering state
            if (_inboxLoading) return;
            const search = (document.getElementById('inboxSearchInput') || {}).value || '';
            if (search.trim()) return; // don't clobber search results
            try {
                const r = await fetch('/api/inbox/conversations?limit=50&offset=0');
                const data = await r.json();
                const convos = data.conversations || [];
                // Merge: if any contact_id in new data isn't in current list, or timestamp changed — update
                let changed = false;
                const existingMap = {};
                _inboxAllData.forEach(c => { existingMap[c.contact_id] = c; });
                convos.forEach(c => {
                    const old = existingMap[c.contact_id];
                    if (!old || old.date !== c.date || old.last_message !== c.last_message) {
                        changed = true;
                        // Mark as unread if new inbound message arrived
                        if (!old || (c.last_direction === 'inbound' && (!old.last_message || old.date !== c.date))) {
                            if (c.contact_id !== _inboxThreadContactId) {
                                _inboxUnreadIds.add(c.contact_id);
                            }
                        }
                    }
                });
                if (changed) {
                    _inboxAllData = convos;
                    _inboxApplyFilter();
                }
            } catch(e) {}
        }

        async function _inboxFetch(append) {
            if (_inboxLoading) return;
            _inboxLoading = true;
            const list = document.getElementById('inboxConversationList');
            if (!list) { _inboxLoading = false; return; }

            if (!append) {
                list.innerHTML = '<div style="padding:30px;text-align:center;"><div style="width:24px;height:24px;border:2px solid rgba(0,122,255,0.3);border-top-color:#007AFF;border-radius:50%;animation:spin .6s linear infinite;margin:0 auto;"></div><style>@keyframes spin{to{transform:rotate(360deg)}}</style></div>';
            }

            const search = (document.getElementById('inboxSearchInput') || {}).value || '';
            let url = '/api/inbox/conversations?limit=50&offset=' + _inboxOffset;
            if (search.trim()) url += '&q=' + encodeURIComponent(search.trim());

            try {
                const r = await fetch(url);
                const data = await r.json();
                const convos = data.conversations || [];
                _inboxHasMore = data.has_more || false;

                if (append) {
                    _inboxAllData = _inboxAllData.concat(convos);
                } else {
                    _inboxAllData = convos;
                }

                _inboxApplyFilter();

                // Update badge
                const badge = document.getElementById('iosBadgeInbox');
                if (badge) {
                    const total = data.total || _inboxAllData.length;
                    if (total > 0) { badge.textContent = total > 99 ? '99+' : total; badge.style.display = ''; }
                    else badge.style.display = 'none';
                }
            } catch(e) {
                if (!append) {
                    list.innerHTML = '<div style="padding:40px 20px;text-align:center;"><div style="color:#8E8E93;font-size:0.85rem;">Waiting for sync...</div><div style="font-size:0.75rem;color:#555;margin-top:6px;">Conversations appear after LeadConnector data syncs</div></div>';
                }
            } finally {
                _inboxLoading = false;
            }
        }

        function inboxSetFilter(filter) {
            _inboxFilter = filter;
            document.querySelectorAll('#inboxFilterRow .imsg-filter-pill').forEach(b => {
                b.classList.toggle('active', b.dataset.filter === filter);
            });
            _inboxApplyFilter();
        }

        function _inboxApplyFilter() {
            if (_inboxFilter === 'all') {
                _inboxData = [..._inboxAllData];
            } else if (_inboxFilter === 'unread') {
                // "Unread" = last message was inbound (they messaged, we haven't replied yet)
                _inboxData = _inboxAllData.filter(c => c.last_direction === 'inbound');
            } else {
                _inboxData = _inboxAllData.filter(c => c.last_direction === _inboxFilter);
            }
            _renderInboxList();
        }

        function inboxDebounceSearch() {
            clearTimeout(_inboxSearchTimer);
            _inboxSearchTimer = setTimeout(() => {
                _inboxOffset = 0;
                _inboxFetch(false);
            }, 300);
        }

        function _renderInboxList() {
            const list = document.getElementById('inboxConversationList');
            if (!list) return;

            if (!_inboxData.length) {
                const search = (document.getElementById('inboxSearchInput') || {}).value || '';
                if (search.trim()) {
                    list.innerHTML = '<div style="padding:40px 20px;text-align:center;"><div style="font-size:2rem;margin-bottom:8px;">🔍</div><div style="color:#8E8E93;font-size:0.82rem;">No results for "' + search.trim().replace(/</g,'&lt;') + '"</div></div>';
                } else {
                    list.innerHTML = '<div style="padding:40px 20px;text-align:center;"><div style="width:50px;height:50px;border-radius:50%;background:rgba(0,122,255,0.08);display:flex;align-items:center;justify-content:center;margin:0 auto 12px;"><i class="fa-solid fa-message" style="color:#007AFF;font-size:1.1rem;"></i></div><div style="color:#fff;font-size:0.88rem;font-weight:600;">No Messages</div><div style="color:#8E8E93;font-size:0.75rem;margin-top:4px;">Conversations will appear here</div></div>';
                }
                return;
            }

            // Group by date sections (Today, Yesterday, This Week, Earlier)
            let html = '';
            let lastSection = '';

            _inboxData.forEach(c => {
                const section = _inboxDateSection(c.date);
                if (section !== lastSection) {
                    html += '<div style="padding:6px 16px 2px;font-size:0.75rem;font-weight:700;color:#8E8E93;text-transform:uppercase;letter-spacing:0.5px;">' + section + '</div>';
                    lastSection = section;
                }

                const cName = c.contact_name || 'Unknown';
                const cNameSafe = dialerEsc(cName);
                const initials = cName.split(' ').map(w => (w||'')[0]).join('').slice(0, 2).toUpperCase();
                const isInbound = c.last_direction === 'inbound';
                const isUnread = _inboxUnreadIds.has(c.contact_id);
                const timeStr = _inboxFormatTime(c.date);
                const preview = c.last_message || 'No messages yet';
                // Color hash for avatar
                const hue = _inboxNameHue(cName);

                html += '<div class="imsg-convo-row' + (isUnread ? ' imsg-convo-unread' : '') + '" onclick="inboxOpenThread(\'' + c.contact_id + '\', \'' + cNameSafe.replace(/'/g, "&#39;") + '\', \'' + (c.contact_phone || '').replace(/'/g, '') + '\')">' +
                    '<div class="imsg-avatar" style="background:linear-gradient(135deg,hsl(' + hue + ',55%,45%),hsl(' + (hue+30) + ',60%,35%));">' + dialerEsc(initials) + '</div>' +
                    '<div style="flex:1;min-width:0;">' +
                        '<div style="display:flex;align-items:center;justify-content:space-between;gap:6px;">' +
                            '<span class="imsg-name"' + (isUnread || isInbound ? ' style="font-weight:700;"' : '') + '>' + cNameSafe + '</span>' +
                            '<div style="display:flex;align-items:center;gap:4px;">' +
                                '<span class="imsg-time"' + (isUnread ? ' style="color:#007AFF;font-weight:700;"' : '') + '>' + timeStr + '</span>' +
                                '<i class="fa-solid fa-chevron-right imsg-chevron"></i>' +
                            '</div>' +
                        '</div>' +
                        '<div class="imsg-preview"' + (isUnread ? ' style="color:#fff;font-weight:600;"' : '') + '>' + (isInbound ? '' : '<span style="color:#8E8E93;">You: </span>') + preview.replace(/</g,'&lt;') + '</div>' +
                    '</div>' +
                    (isUnread ? '<div class="imsg-unread-dot" style="background:#007AFF;animation:imsgPulse 1.5s ease-in-out infinite;"></div>' : (isInbound ? '<div class="imsg-unread-dot"></div>' : '')) +
                '</div>';
            });

            // Load more sentinel
            if (_inboxHasMore) {
                html += '<div id="inboxLoadMore" style="padding:16px;text-align:center;"><button onclick="inboxLoadMore()" style="background:none;border:none;color:#007AFF;font-size:0.75rem;font-weight:600;cursor:pointer;padding:8px 16px;">Load More Conversations</button></div>';
            }

            list.innerHTML = html;
        }

        function inboxLoadMore() {
            _inboxOffset += 50;
            const btn = document.getElementById('inboxLoadMore');
            if (btn) btn.innerHTML = '<div style="width:18px;height:18px;border:2px solid rgba(0,122,255,0.3);border-top-color:#007AFF;border-radius:50%;animation:spin .6s linear infinite;margin:0 auto;"></div>';
            _inboxFetch(true);
        }

        function _inboxDateSection(dateStr) {
            if (!dateStr) return 'Earlier';
            try {
                const d = new Date(dateStr);
                const now = new Date();
                const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
                const msgDay = new Date(d.getFullYear(), d.getMonth(), d.getDate());
                const diffDays = Math.floor((today - msgDay) / 86400000);
                if (diffDays === 0) return 'Today';
                if (diffDays === 1) return 'Yesterday';
                if (diffDays < 7) return 'This Week';
                if (diffDays < 30) return 'This Month';
                return 'Earlier';
            } catch(e) { return 'Earlier'; }
        }

        function _inboxNameHue(name) {
            let h = 0;
            for (let i = 0; i < (name || '').length; i++) h = ((h << 5) - h + name.charCodeAt(i)) | 0;
            return Math.abs(h) % 360;
        }

        let _inboxThreadContactId = null;
        let _inboxThreadContactName = '';
        let _inboxThreadContactPhone = '';
        let _inboxSending = false;

        function inboxOpenThread(contactId, contactName, contactPhone) {
            _inboxThreadContactId = contactId;
            _inboxThreadContactName = contactName || '';
            _inboxThreadContactPhone = contactPhone || '';

            // Clear unread state for this contact and re-render the list row
            if (_inboxUnreadIds.has(contactId)) {
                _inboxUnreadIds.delete(contactId);
                _renderInboxList();
            }

            const threadView = document.getElementById('inboxThreadView');
            const threadName = document.getElementById('inboxThreadName');
            const threadPhone = document.getElementById('inboxThreadPhone');
            const threadAvatar = document.getElementById('inboxThreadAvatar');
            const msgContainer = document.getElementById('inboxThreadMessages');

            const initials = (contactName || '?').split(' ').map(w => (w||'')[0]).join('').slice(0, 2).toUpperCase();
            const hue = _inboxNameHue(contactName || '');

            // Populate channel selector from main dialer's loaded channels
            const inboxChSel = document.getElementById('inboxChannelSelect');
            const mainChSel = document.getElementById('dlrChannelSelect');
            if (inboxChSel && mainChSel) {
                inboxChSel.innerHTML = mainChSel.innerHTML;
                inboxChSel.value = mainChSel.value;
            }
            const inboxChRow = document.getElementById('inboxChannelRow');
            if (inboxChRow && _dlrAvailableChannels && _dlrAvailableChannels.length > 1) {
                inboxChRow.style.display = 'flex';
            }

            // Reset composer
            const inboxText = document.getElementById('inboxSmsText');
            if (inboxText) { inboxText.value = ''; inboxText.style.height = ''; }
            const inboxStatus = document.getElementById('inboxSmsStatus');
            if (inboxStatus) inboxStatus.textContent = '';
            const inboxCount = document.getElementById('inboxCharCount');
            if (inboxCount) inboxCount.textContent = '0 / 160';

            if (threadName) threadName.textContent = contactName || 'Contact';
            if (threadPhone) threadPhone.textContent = contactPhone || '';
            if (threadAvatar) {
                threadAvatar.textContent = initials;
                threadAvatar.style.background = 'linear-gradient(135deg,hsl(' + hue + ',55%,45%),hsl(' + (hue+30) + ',60%,35%))';
            }
            if (msgContainer) msgContainer.innerHTML = '<div style="text-align:center;padding:40px;color:#8E8E93;"><i class="fa-solid fa-spinner fa-spin"></i></div>';
            if (threadView) { threadView.style.display = 'flex'; threadView.style.flexDirection = 'column'; }

            fetch('/api/inbox/thread/' + contactId + '?limit=200')
                .then(r => r.json())
                .then(data => {
                    const msgs = data.messages || [];
                    const pipeline = data.pipeline;

                    // Pipeline badge
                    const badgeEl = document.getElementById('inboxThreadBadge');
                    if (badgeEl && pipeline) {
                        badgeEl.textContent = pipeline.stage_name;
                        badgeEl.style.display = '';
                        badgeEl.style.background = pipeline.status === 'won' ? 'rgba(52,199,89,0.15)' : pipeline.status === 'lost' ? 'rgba(255,59,48,0.15)' : 'rgba(91,127,255,0.15)';
                        badgeEl.style.color = pipeline.status === 'won' ? '#34C759' : pipeline.status === 'lost' ? '#FF3B30' : '#5B7FFF';
                    } else if (badgeEl) {
                        badgeEl.style.display = 'none';
                    }

                    let html = '';
                    let lastDateLabel = '';

                    msgs.forEach(m => {
                        const isOutbound = m.direction === 'outbound';
                        const isCall = m.type === 'call' || m.type === 'voicemail';

                        // Date separator
                        const dateLabel = _inboxThreadDateLabel(m.date);
                        if (dateLabel !== lastDateLabel) {
                            html += '<div class="imsg-date-sep">' + dateLabel + '</div>';
                            lastDateLabel = dateLabel;
                        }

                        if (isCall) {
                            const callIcon = m.type === 'voicemail' ? 'fa-voicemail' : 'fa-phone';
                            const callLabel = m.type === 'voicemail' ? 'Voicemail' : (isOutbound ? 'Outgoing Call' : 'Incoming Call');
                            html += '<div class="imsg-call-pill"><span><i class="fa-solid ' + callIcon + '" style="font-size:0.75rem;"></i>' + callLabel + ' · ' + _inboxFormatTimeShort(m.date) + '</span></div>';
                        } else {
                            const body = (m.body || '').replace(/</g, '&lt;').replace(/>/g, '&gt;');
                            html += '<div class="imsg-bubble ' + (isOutbound ? 'outbound' : 'inbound') + '">' +
                                body +
                                '<div class="imsg-bubble-time">' + _inboxFormatTimeShort(m.date) + '</div>' +
                            '</div>';
                        }
                    });

                    if (!msgs.length) {
                        html = '<div style="text-align:center;padding:40px;color:#8E8E93;font-size:0.82rem;">No messages yet</div>';
                    }

                    if (msgContainer) {
                        msgContainer.innerHTML = html;
                        msgContainer.scrollTop = msgContainer.scrollHeight;
                    }
                })
                .catch(() => {
                    if (msgContainer) msgContainer.innerHTML = '<div style="text-align:center;padding:40px;color:#8E8E93;">Failed to load messages</div>';
                });
        }

        function inboxBackToList() {
            const threadView = document.getElementById('inboxThreadView');
            if (threadView) threadView.style.display = 'none';
            const badge = document.getElementById('inboxThreadBadge');
            if (badge) badge.style.display = 'none';
            _inboxThreadContactId = null;
        }

        // ── Inbox Thread Composer Functions ──

        function inboxAutoGrow(el) {
            el.style.height = '';
            el.style.height = Math.min(el.scrollHeight, 120) + 'px';
        }

        function inboxUpdateCharCount(val) {
            const el = document.getElementById('inboxCharCount');
            if (el) el.textContent = (val || '').length + ' / 160';
        }

        function inboxGetChannel() {
            const sel = document.getElementById('inboxChannelSelect');
            return (sel && sel.value) || 'ghl';
        }

        async function inboxSendSms() {
            if (_inboxSending || !_inboxThreadContactId) return;
            const textEl = document.getElementById('inboxSmsText');
            const statusEl = document.getElementById('inboxSmsStatus');
            const sendBtn = document.getElementById('inboxSmsSendBtn');
            const msgContainer = document.getElementById('inboxThreadMessages');
            if (!textEl) return;
            const msg = textEl.value.trim();
            if (!msg) return;
            if (msg.length > 1600) {
                if (statusEl) { statusEl.textContent = 'Message too long (max 1600 chars)'; statusEl.style.color = '#ef4444'; }
                return;
            }

            const channel = inboxGetChannel();
            const channelLabel = channel === 'twilio' ? 'InsuranceGrokBot' : 'LeadConnector';

            // Optimistic UI — append bubble
            const pendingId = 'inbox_sms_' + Date.now();
            if (msgContainer) {
                const bubble = document.createElement('div');
                bubble.className = 'imsg-bubble outbound';
                bubble.id = pendingId;
                bubble.style.opacity = '0.6';
                const body = msg.replace(/</g, '&lt;').replace(/>/g, '&gt;');
                const now = new Date().toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true });
                bubble.innerHTML = body + '<div class="imsg-bubble-time">' + now + ' · Sending…</div>';
                msgContainer.appendChild(bubble);
                msgContainer.scrollTop = msgContainer.scrollHeight;
            }

            textEl.value = '';
            textEl.style.height = '';
            inboxUpdateCharCount('');

            _inboxSending = true;
            if (sendBtn) { sendBtn.disabled = true; sendBtn.style.opacity = '0.5'; }
            if (statusEl) { statusEl.textContent = 'Sending via ' + channelLabel + '…'; statusEl.style.color = '#888'; }

            try {
                const payload = { message: msg, channel: channel };
                if (channel === 'twilio' && _inboxThreadContactPhone) {
                    payload.contact_phone = _inboxThreadContactPhone;
                }
                const r = await fetch('/voice/contact/' + _inboxThreadContactId + '/send-sms', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                const d = await r.json();
                const bubble = document.getElementById(pendingId);
                if (r.ok) {
                    if (bubble) { bubble.style.opacity = '1'; bubble.querySelector('.imsg-bubble-time').textContent = new Date().toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true }); }
                    if (statusEl) { statusEl.textContent = '\u2713 Sent via ' + channelLabel; statusEl.style.color = '#007AFF'; }
                    setTimeout(() => { if (statusEl) statusEl.textContent = ''; }, 4000);
                } else {
                    if (bubble) { bubble.style.opacity = '0.4'; bubble.style.borderLeft = '2px solid #ef4444'; }
                    const err = d.error || 'Send failed';
                    if (statusEl) { statusEl.textContent = err; statusEl.style.color = '#ef4444'; }
                    textEl.value = msg;
                    inboxAutoGrow(textEl);
                    inboxUpdateCharCount(msg);
                }
            } catch(e) {
                const bubble = document.getElementById(pendingId);
                if (bubble) { bubble.style.opacity = '0.4'; bubble.style.borderLeft = '2px solid #ef4444'; }
                if (statusEl) { statusEl.textContent = 'Network error'; statusEl.style.color = '#ef4444'; }
            } finally {
                _inboxSending = false;
                if (sendBtn) { sendBtn.disabled = false; sendBtn.style.opacity = '1'; }
            }
        }

        function inboxAiReply() {
            if (!_inboxThreadContactId) return;
            const btn = document.getElementById('inboxAiDraftBtn');
            const label = document.getElementById('inboxAiDraftLabel');
            if (btn) btn.disabled = true;
            if (label) label.textContent = 'Thinking…';

            fetch('/voice/contact/' + encodeURIComponent(_inboxThreadContactId) + '/ai-suggest', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({})
            })
                .then(r => r.json())
                .then(d => {
                    if (d.suggestion) {
                        const textEl = document.getElementById('inboxSmsText');
                        if (textEl) {
                            textEl.value = d.suggestion;
                            inboxAutoGrow(textEl);
                            inboxUpdateCharCount(d.suggestion);
                            textEl.focus();
                        }
                    } else if (d.error) {
                        _showDashToast(false, d.error);
                    }
                })
                .catch(() => { _showDashToast(false, 'AI Reply failed'); })
                .finally(() => {
                    if (btn) btn.disabled = false;
                    if (label) label.textContent = 'AI Reply';
                });
        }

        function inboxToggleQuickOptions(event) {
            event.stopPropagation();
            const menu = document.getElementById('inboxQuickOptionsMenu');
            if (!menu) return;
            const isOpen = menu.classList.contains('open');
            // Close all quick option menus first
            document.querySelectorAll('.ios-quick-options-menu.open').forEach(m => m.classList.remove('open'));
            if (!isOpen) {
                menu.classList.add('open');
                // Position above button
                const btn = event.currentTarget;
                const rect = btn.getBoundingClientRect();
                menu.style.bottom = (window.innerHeight - rect.top + 4) + 'px';
                menu.style.right = (window.innerWidth - rect.right) + 'px';
                menu.style.animation = 'qoSlideUp 0.15s ease-out';
                // Close on next click
                setTimeout(() => {
                    document.addEventListener('click', function _closeInboxQO() {
                        menu.classList.remove('open');
                        document.removeEventListener('click', _closeInboxQO);
                    }, { once: true });
                }, 10);
            }
        }

        function inboxQuickOption(text) {
            const textEl = document.getElementById('inboxSmsText');
            if (textEl) {
                textEl.value = text;
                inboxAutoGrow(textEl);
                inboxUpdateCharCount(text);
                textEl.focus();
            }
            const menu = document.getElementById('inboxQuickOptionsMenu');
            if (menu) menu.classList.remove('open');
        }

        function _inboxFormatTime(dateStr) {
            if (!dateStr) return '';
            try {
                const d = new Date(dateStr);
                const now = new Date();
                const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
                const msgDay = new Date(d.getFullYear(), d.getMonth(), d.getDate());
                const diffDays = Math.floor((today - msgDay) / 86400000);
                if (diffDays === 0) return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true });
                if (diffDays === 1) return 'Yesterday';
                if (diffDays < 7) return d.toLocaleDateString('en-US', { weekday: 'short' });
                return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
            } catch (e) { return ''; }
        }

        function _inboxFormatTimeShort(dateStr) {
            if (!dateStr) return '';
            try {
                return new Date(dateStr).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true });
            } catch(e) { return ''; }
        }

        function _inboxThreadDateLabel(dateStr) {
            if (!dateStr) return '';
            try {
                const d = new Date(dateStr);
                const now = new Date();
                const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
                const msgDay = new Date(d.getFullYear(), d.getMonth(), d.getDate());
                const diffDays = Math.floor((today - msgDay) / 86400000);
                if (diffDays === 0) return 'Today';
                if (diffDays === 1) return 'Yesterday';
                if (diffDays < 7) return d.toLocaleDateString('en-US', { weekday: 'long' });
                return d.toLocaleDateString('en-US', { weekday: 'long', month: 'long', day: 'numeric', year: d.getFullYear() !== now.getFullYear() ? 'numeric' : undefined });
            } catch(e) { return ''; }
        }


        // ═══════════════════════════════════════════════════════════════════════
        // ═══ SSE NOTIFICATIONS — Real-time Dashboard Events ══════════════════
        // ═══════════════════════════════════════════════════════════════════════

        let _sseSource = null;
        let _sseRetries = 0;

        function _initSSENotifications() {
            if (_sseSource) return; // Already connected
            if (typeof EventSource === 'undefined') return;

            try {
                _sseSource = new EventSource('/api/stream/notifications');
                _sseRetries = 0;

                _sseSource.onmessage = function(event) {
                    try {
                        const data = JSON.parse(event.data);
                        if (data.type === 'heartbeat' || data.type === 'connected') return;

                        if (data.type === 'new_inbound_message') {
                            _inboxHandleLiveMessage(data);
                        } else if (data.type === 'ghl_sync_complete') {
                            // Sync just finished — silently refresh the list to pick up any new messages
                            _inboxSilentRefresh();
                        } else if (data.type === 'webhook_received') {
                            // Legacy: show notification only (no contact info available)
                            _showIosNotification({ contact_name: 'Contact', message_preview: 'New message received' });
                        }
                    } catch (e) {}
                };

                _sseSource.onerror = function() {
                    _sseSource.close();
                    _sseSource = null;
                    _sseRetries++;
                    if (_sseRetries < 5) {
                        setTimeout(_initSSENotifications, Math.min(5000 * _sseRetries, 30000));
                    }
                };
            } catch (e) {}
        }

        function _showIosNotification(data) {
            const banner = document.getElementById('iosNotificationBanner');
            if (!banner) return;

            const title = document.getElementById('iosNotifTitle');
            const body = document.getElementById('iosNotifBody');
            const time = document.getElementById('iosNotifTime');

            const senderName = data.contact_name || 'Messages';
            const preview = data.message_preview || 'New message received';

            if (title) title.textContent = senderName;
            if (body) body.textContent = preview;
            if (time) time.textContent = 'now';

            // Slide in
            banner.style.display = 'block';
            requestAnimationFrame(() => {
                banner.style.opacity = '1';
                banner.style.transform = 'translateY(0)';
            });

            // Click to open inbox
            banner.onclick = function() {
                banner.style.opacity = '0';
                banner.style.transform = 'translateY(-20px)';
                setTimeout(() => { banner.style.display = 'none'; }, 300);
                iosOpenApp('inbox');
            };

            // Auto-dismiss after 5s
            setTimeout(() => {
                banner.style.opacity = '0';
                banner.style.transform = 'translateY(-20px)';
                setTimeout(() => { banner.style.display = 'none'; }, 300);
            }, 5000);

            // Update inbox badge
            const badge = document.getElementById('iosBadgeInbox');
            if (badge) {
                const current = parseInt(badge.textContent) || 0;
                badge.textContent = current + 1;
                badge.style.display = '';
            }
        }

        function _inboxHandleLiveMessage(data) {
            // Called when SSE delivers a new_inbound_message event.
            // Updates the conversation list in-place and appends to the open thread if relevant.
            const contactId = data.contact_id;
            const contactName = data.contact_name || '';
            const preview = data.message_preview || '';
            const direction = data.direction || 'inbound';
            const now = new Date().toISOString();

            // 1. Mark as unread (only if not the thread currently open)
            if (direction === 'inbound' && contactId && contactId !== _inboxThreadContactId) {
                _inboxUnreadIds.add(contactId);
            }

            // 2. Move/update this contact to the top of the conversation list
            if (contactId && _inboxAllData.length) {
                const idx = _inboxAllData.findIndex(c => c.contact_id === contactId);
                if (idx !== -1) {
                    const updated = Object.assign({}, _inboxAllData[idx], {
                        last_message: preview || _inboxAllData[idx].last_message,
                        last_direction: direction,
                        date: now,
                    });
                    _inboxAllData.splice(idx, 1);
                    _inboxAllData.unshift(updated);
                } else if (contactName) {
                    // New contact not yet in list — prepend a stub so it shows immediately
                    _inboxAllData.unshift({
                        contact_id: contactId,
                        contact_name: contactName,
                        contact_phone: '',
                        last_message: preview,
                        last_direction: direction,
                        date: now,
                        source: 'live',
                    });
                }
                _inboxApplyFilter();
            } else if (contactId) {
                // List not loaded yet — trigger a full fetch
                _inboxFetch(false);
            }

            // 3. If this contact's thread is open, append the message bubble live
            if (contactId && contactId === _inboxThreadContactId) {
                const msgContainer = document.getElementById('inboxThreadMessages');
                if (msgContainer && preview) {
                    const bubble = document.createElement('div');
                    bubble.className = 'imsg-bubble ' + (direction === 'inbound' ? 'inbound' : 'outbound');
                    const timeStr = new Date().toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true });
                    bubble.innerHTML = preview.replace(/</g, '&lt;').replace(/>/g, '&gt;') +
                        '<div class="imsg-bubble-time">' + timeStr + '</div>';
                    msgContainer.appendChild(bubble);
                    msgContainer.scrollTop = msgContainer.scrollHeight;
                }
            }

            // 4. Show notification banner (only for inbound, and only if not already in that thread)
            if (direction === 'inbound' && contactId !== _inboxThreadContactId) {
                _showIosNotification(data);
            }
        }

        // Start SSE when dialer tab is active; also start background refresh
        document.addEventListener('DOMContentLoaded', function() {
            const dialerTab = document.querySelector('[data-bs-target="#dialer"]') || document.querySelector('[href="#dialer"]');
            if (dialerTab) {
                dialerTab.addEventListener('shown.bs.tab', function() {
                    _initSSENotifications();
                    _inboxStartBgRefresh();
                    // Auto-load contacts on tab open
                    if (!_dialerAllContacts || !_dialerAllContacts.length) {
                        dialerSyncContacts(); // sync from CRM on first open
                    } else {
                        dialerFetchContacts(false); // reload from cache on subsequent opens
                    }
                });
                dialerTab.addEventListener('click', function() {
                    setTimeout(_initSSENotifications, 500);
                    setTimeout(_inboxStartBgRefresh, 500);
                });
            }
            const dialerPane = document.getElementById('voicedialer');
            if (dialerPane && dialerPane.classList.contains('active')) {
                setTimeout(_initSSENotifications, 1000);
                setTimeout(_inboxStartBgRefresh, 1000);
                setTimeout(function() {
                    if (!_dialerAllContacts || !_dialerAllContacts.length) {
                        dialerSyncContacts();
                    } else {
                        dialerFetchContacts(false);
                    }
                }, 1200);
            }
        });

        // ═══════════════════════════════════════════════════════════════════════
        // ═══ DISCORD PHONE APP — bridges existing Discord.* state + API ══════
        // ═══════════════════════════════════════════════════════════════════════
        let _iosDiscordPollTimer = null;
        let _iosDiscordActiveChannel = null;
        let _iosDiscordLastMsgIds = {};

        function iosDiscordInit() {
            // Check if Discord is connected via the existing global state
            if (typeof Discord !== 'undefined' && Discord.connected) {
                document.getElementById('iosDiscordNotConnected').style.display = 'none';
                const conn = document.getElementById('iosDiscordConnected');
                conn.style.display = 'flex';
                _iosDiscordRenderServers();
            } else {
                document.getElementById('iosDiscordNotConnected').style.display = 'flex';
                document.getElementById('iosDiscordConnected').style.display = 'none';
                // Try fetching status in case Discord.js hasn't loaded yet
                fetch('/api/discord/status').then(r => r.json()).then(data => {
                    if (!data.connected) return;
                    document.getElementById('iosDiscordNotConnected').style.display = 'none';
                    document.getElementById('iosDiscordConnected').style.display = 'flex';
                    if (typeof Discord !== 'undefined') {
                        Discord.connected = true;
                        Discord.user = data.user;
                        Discord.servers = data.servers || [];
                    }
                    _iosDiscordRenderServers();
                }).catch(() => {});
            }
        }

        function _iosDiscordRenderServers() {
            const servers = (typeof Discord !== 'undefined') ? Discord.servers : [];
            const el = document.getElementById('iosDiscordServers');
            if (!servers.length) {
                el.innerHTML = '<div style="font-size:0.78rem;color:#72767d;padding:4px 0;">No servers added. Click <strong style="color:#5865f2;">+ Add</strong> to get started.</div>';
                return;
            }
            el.innerHTML = servers.map(s => {
                const icon = s.icon_url
                    ? `<img src="${s.icon_url}" style="width:28px;height:28px;border-radius:7px;object-fit:cover;">`
                    : `<div style="width:28px;height:28px;border-radius:7px;background:rgba(88,101,242,0.2);display:flex;align-items:center;justify-content:center;font-size:0.7rem;font-weight:800;color:#5865f2;">${(s.name||'S')[0].toUpperCase()}</div>`;
                const dot = s.bot_in_server
                    ? '<span style="width:6px;height:6px;border-radius:50%;background:#43b581;flex-shrink:0;"></span>'
                    : '<span style="width:6px;height:6px;border-radius:50%;background:#faa61a;flex-shrink:0;"></span>';
                return `<button onclick="_iosDiscordLoadChannels('${s.guild_id}', ${s.bot_in_server}, '${(s.name||'').replace(/'/g,"\\'")}')" style="display:flex;align-items:center;gap:8px;padding:8px;border-radius:8px;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.06);cursor:pointer;width:100%;text-align:left;color:#dcddde;font-size:0.8rem;">
                    ${icon}
                    <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${_iosEscHtml(s.name)}</span>
                    ${dot}
                </button>`;
            }).join('');
        }

        function _iosDiscordLoadChannels(guildId, botInServer, serverName) {
            const el = document.getElementById('iosDiscordChannels');
            document.getElementById('iosDiscordServerName').textContent = serverName;
            if (!botInServer) {
                el.innerHTML = `<div style="padding:20px;text-align:center;">
                    <div style="font-size:0.82rem;color:#faa61a;margin-bottom:12px;"><i class="fa-solid fa-robot me-1"></i>Bot needs to join this server</div>
                    <button onclick="inviteBotToServer('${guildId}')" style="background:#5865f2;border:none;color:#fff;border-radius:8px;padding:10px 16px;font-size:0.82rem;font-weight:700;cursor:pointer;">
                        <i class="fa-brands fa-discord me-1"></i>Invite Bot
                    </button>
                </div>`;
                return;
            }
            el.innerHTML = '<div style="padding:16px;text-align:center;color:#72767d;font-size:0.78rem;"><i class="fa-solid fa-spinner fa-spin me-1"></i>Loading channels...</div>';
            fetch(`/api/discord/channels/${guildId}`)
                .then(r => r.json())
                .then(data => {
                    if (data.error || data.needs_invite) {
                        el.innerHTML = `<div style="padding:16px;color:#faa61a;font-size:0.82rem;text-align:center;">${_iosEscHtml(data.error || 'Bot not in server')}</div>`;
                        return;
                    }
                    const channels = data.channels || [];
                    if (!channels.length) { el.innerHTML = '<div style="padding:16px;color:#72767d;font-size:0.78rem;">No text channels found.</div>'; return; }
                    el.innerHTML = '<div style="font-size:0.72rem;color:#72767d;text-transform:uppercase;letter-spacing:0.5px;font-weight:700;padding:8px 0 4px;">Text Channels</div>' +
                        channels.map(ch => `<button onclick="_iosDiscordOpenChat('${guildId}','${ch.id}','${(ch.name||'').replace(/'/g,"\\'")}','${serverName.replace(/'/g,"\\'")}')" style="display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:6px;background:none;border:none;cursor:pointer;width:100%;text-align:left;color:#8e9297;font-size:0.82rem;transition:background 0.15s,color 0.15s;" onmouseover="this.style.background='rgba(255,255,255,0.06)';this.style.color='#dcddde'" onmouseout="this.style.background='none';this.style.color='#8e9297'">
                            <span style="color:#72767d;font-weight:500;">#</span>
                            <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${_iosEscHtml(ch.name)}</span>
                        </button>`).join('');
                })
                .catch(() => { el.innerHTML = '<div style="padding:16px;color:#faa61a;font-size:0.82rem;">Network error</div>'; });
        }

        function _iosDiscordOpenChat(guildId, channelId, channelName, serverName) {
            _iosDiscordActiveChannel = channelId;
            document.getElementById('iosDiscordChannelName').textContent = '#' + channelName;
            document.getElementById('iosDiscordServerName').textContent = serverName;
            document.getElementById('iosDiscordReplyText').placeholder = `Message #${channelName}...`;

            // Show chat, hide picker
            document.getElementById('iosDiscordConnected').style.display = 'none';
            const chat = document.getElementById('iosDiscordChat');
            chat.style.display = 'flex';

            // Also update the existing Discord state for panel sync
            if (typeof Discord !== 'undefined') {
                Discord.activeGuildId = guildId;
                Discord.activeChannelId = channelId;
                Discord.activeChannelName = channelName;
                Discord.activeServerName = serverName;
            }

            _iosDiscordFetchMessages(channelId, true);
            _iosDiscordStartPoll();
        }

        function _iosDiscordFetchMessages(channelId, initial) {
            const msgEl = document.getElementById('iosDiscordMessages');
            if (initial) msgEl.innerHTML = '<div style="padding:24px;text-align:center;color:#72767d;font-size:0.78rem;"><i class="fa-solid fa-spinner fa-spin me-1"></i>Loading...</div>';

            fetch(`/api/discord/messages/${channelId}`)
                .then(r => r.json())
                .then(data => {
                    if (data.error) { msgEl.innerHTML = `<div style="padding:24px;text-align:center;color:#faa61a;font-size:0.82rem;">${_iosEscHtml(data.error)}</div>`; return; }
                    const messages = data.messages || [];
                    if (initial) {
                        _iosDiscordLastMsgIds[channelId] = null;
                        if (!messages.length) {
                            msgEl.innerHTML = '<div style="padding:24px;text-align:center;color:#72767d;font-size:0.82rem;">No messages yet</div>';
                        } else {
                            msgEl.innerHTML = '';
                            messages.forEach(m => _iosDiscordAppendMsg(m, channelId));
                            msgEl.scrollTop = msgEl.scrollHeight;
                        }
                    } else {
                        const lastId = _iosDiscordLastMsgIds[channelId];
                        const newMsgs = lastId ? messages.filter(m => BigInt(m.id) > BigInt(lastId)) : [];
                        if (newMsgs.length) {
                            newMsgs.forEach(m => _iosDiscordAppendMsg(m, channelId));
                            msgEl.scrollTop = msgEl.scrollHeight;
                        }
                    }
                    if (messages.length) _iosDiscordLastMsgIds[channelId] = messages[messages.length-1].id;
                })
                .catch(() => { if (initial) msgEl.innerHTML = '<div style="padding:24px;text-align:center;color:#faa61a;">Network error</div>'; });
        }

        function _iosDiscordAppendMsg(msg, channelId) {
            const msgEl = document.getElementById('iosDiscordMessages');
            const isSelf = (typeof Discord !== 'undefined') && Discord.user && msg.author_id === Discord.user.discord_id;
            const ts = msg.timestamp ? new Date(msg.timestamp) : null;
            const timeStr = ts ? ts.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}) : '';

            const avatar = msg.avatar_url
                ? `<img src="${msg.avatar_url}" style="width:32px;height:32px;border-radius:50%;object-fit:cover;flex-shrink:0;">`
                : `<div style="width:32px;height:32px;border-radius:50%;background:#5865f2;display:flex;align-items:center;justify-content:center;font-size:0.7rem;font-weight:800;color:#fff;flex-shrink:0;">${(msg.author||'?')[0].toUpperCase()}</div>`;

            const div = document.createElement('div');
            div.style.cssText = 'display:flex;gap:10px;padding:4px 0;';
            div.innerHTML = `${avatar}
                <div style="flex:1;min-width:0;">
                    <div style="display:flex;align-items:baseline;gap:6px;">
                        <span style="font-weight:600;font-size:0.82rem;color:${isSelf ? '#5865f2' : '#fff'};">${_iosEscHtml(msg.author||'Unknown')}</span>
                        <span style="font-size:0.65rem;color:#72767d;">${timeStr}</span>
                    </div>
                    <div style="font-size:0.82rem;color:#dcddde;line-height:1.4;word-break:break-word;">${_iosEscHtml(msg.content||'')}</div>
                </div>`;
            msgEl.appendChild(div);
        }

        function _iosDiscordStartPoll() {
            _iosDiscordStopPoll();
            _iosDiscordPollTimer = setInterval(() => {
                if (_iosDiscordActiveChannel) _iosDiscordFetchMessages(_iosDiscordActiveChannel, false);
            }, 4000);
        }
        function _iosDiscordStopPoll() {
            if (_iosDiscordPollTimer) { clearInterval(_iosDiscordPollTimer); _iosDiscordPollTimer = null; }
        }

        function iosDiscordSend() {
            const textEl = document.getElementById('iosDiscordReplyText');
            const text = textEl.value.trim();
            if (!text || !_iosDiscordActiveChannel) return;
            textEl.disabled = true;
            fetch(`/api/discord/messages/${_iosDiscordActiveChannel}`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({content: text})
            }).then(r => r.json()).then(data => {
                textEl.disabled = false;
                if (data.error) return;
                textEl.value = '';
                textEl.style.height = 'auto';
                _iosDiscordFetchMessages(_iosDiscordActiveChannel, false);
            }).catch(() => { textEl.disabled = false; });
        }

        // Back from chat to server/channel picker
        function _iosDiscordBackToServers() {
            _iosDiscordStopPoll();
            _iosDiscordActiveChannel = null;
            document.getElementById('iosDiscordChat').style.display = 'none';
            document.getElementById('iosDiscordConnected').style.display = 'flex';
        }

        // ═══════════════════════════════════════════════════════════════════════
        // ═══ SLACK PHONE APP — bridges existing Slack.* state + API ══════════
        // ═══════════════════════════════════════════════════════════════════════
        let _iosSlackPollTimer = null;
        let _iosSlackActiveChannel = null;
        let _iosSlackLastMsgIds = {};

        function iosSlackInit() {
            if (typeof Slack !== 'undefined' && Slack.connected) {
                document.getElementById('iosSlackNotConnected').style.display = 'none';
                const conn = document.getElementById('iosSlackConnected');
                conn.style.display = 'flex';
                _iosSlackRenderWorkspace();
                _iosSlackLoadChannels();
            } else {
                document.getElementById('iosSlackNotConnected').style.display = 'flex';
                document.getElementById('iosSlackConnected').style.display = 'none';
                fetch('/api/slack/status').then(r => r.json()).then(data => {
                    if (!data.connected) return;
                    document.getElementById('iosSlackNotConnected').style.display = 'none';
                    document.getElementById('iosSlackConnected').style.display = 'flex';
                    if (typeof Slack !== 'undefined') {
                        Slack.connected = true;
                        Slack.user = data.user;
                    }
                    _iosSlackRenderWorkspace();
                    _iosSlackLoadChannels();
                }).catch(() => {});
            }
        }

        function _iosSlackRenderWorkspace() {
            const user = (typeof Slack !== 'undefined') ? Slack.user : null;
            if (!user) return;
            const iconEl = document.getElementById('iosSlackWorkspaceIcon');
            if (iconEl) iconEl.textContent = (user.team_name || 'S')[0].toUpperCase();
            const teamEl = document.getElementById('iosSlackTeamDisplay');
            if (teamEl) teamEl.textContent = user.team_name || 'Workspace';
            const userEl = document.getElementById('iosSlackUserDisplay');
            if (userEl) userEl.textContent = user.name || 'User';
        }

        function _iosSlackLoadChannels() {
            const listEl = document.getElementById('iosSlackChannelList');
            listEl.innerHTML = '<div style="padding:8px 0;color:#ababad;font-size:0.78rem;"><i class="fa-solid fa-spinner fa-spin me-1"></i>Loading...</div>';

            fetch('/api/slack/channels')
                .then(r => r.json())
                .then(data => {
                    if (data.error || data.needs_reconnect) {
                        listEl.innerHTML = `<div style="padding:8px 0;color:#e01e5a;font-size:0.82rem;">${_iosEscHtml(data.error || 'Please reconnect Slack')}</div>`;
                        return;
                    }
                    const channels = data.channels || [];
                    if (typeof Slack !== 'undefined') { Slack.channels = channels; Slack.activeWorkspaceName = data.team_name || ''; }
                    if (!channels.length) { listEl.innerHTML = '<div style="padding:8px 0;color:#ababad;font-size:0.78rem;">No channels found.</div>'; return; }
                    listEl.innerHTML = channels.map(ch =>
                        `<button onclick="_iosSlackOpenChat('${ch.id}','${(ch.name||'').replace(/'/g,"\\'")}')" style="display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:6px;background:none;border:none;cursor:pointer;width:100%;text-align:left;color:#ababad;font-size:0.82rem;transition:background 0.15s,color 0.15s;" onmouseover="this.style.background='rgba(255,255,255,0.06)';this.style.color='#d1d2d3'" onmouseout="this.style.background='none';this.style.color='#ababad'">
                            <span style="color:#ababad;font-weight:500;">#</span>
                            <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${_iosEscHtml(ch.name)}</span>
                        </button>`
                    ).join('');
                })
                .catch(() => { listEl.innerHTML = '<div style="padding:8px 0;color:#e01e5a;">Network error</div>'; });
        }

        function _iosSlackOpenChat(channelId, channelName) {
            _iosSlackActiveChannel = channelId;
            document.getElementById('iosSlackChannelName').textContent = '#' + channelName;
            document.getElementById('iosSlackReplyText').placeholder = `Message #${channelName}...`;
            const wsName = (typeof Slack !== 'undefined') ? (Slack.activeWorkspaceName || '') : '';
            document.getElementById('iosSlackWorkspaceName').textContent = wsName;

            document.getElementById('iosSlackConnected').style.display = 'none';
            document.getElementById('iosSlackChat').style.display = 'flex';

            if (typeof Slack !== 'undefined') {
                Slack.activeChannelId = channelId;
                Slack.activeChannelName = channelName;
            }

            _iosSlackFetchMessages(channelId, true);
            _iosSlackStartPoll();
        }

        function _iosSlackFetchMessages(channelId, initial) {
            const msgEl = document.getElementById('iosSlackMessages');
            if (initial) msgEl.innerHTML = '<div style="padding:24px;text-align:center;color:#ababad;font-size:0.78rem;"><i class="fa-solid fa-spinner fa-spin me-1"></i>Loading...</div>';

            fetch(`/api/slack/messages/${channelId}`)
                .then(r => r.json())
                .then(data => {
                    if (data.error) { msgEl.innerHTML = `<div style="padding:24px;text-align:center;color:#e01e5a;font-size:0.82rem;">${_iosEscHtml(data.error)}</div>`; return; }
                    const messages = data.messages || [];
                    if (initial) {
                        _iosSlackLastMsgIds[channelId] = null;
                        if (!messages.length) {
                            msgEl.innerHTML = '<div style="padding:24px;text-align:center;color:#ababad;font-size:0.82rem;">No messages yet</div>';
                        } else {
                            msgEl.innerHTML = '';
                            messages.forEach(m => _iosSlackAppendMsg(m));
                            msgEl.scrollTop = msgEl.scrollHeight;
                        }
                    } else {
                        const lastTs = _iosSlackLastMsgIds[channelId];
                        const newMsgs = lastTs ? messages.filter(m => parseFloat(m.id) > parseFloat(lastTs)) : [];
                        if (newMsgs.length) {
                            newMsgs.forEach(m => _iosSlackAppendMsg(m));
                            msgEl.scrollTop = msgEl.scrollHeight;
                        }
                    }
                    if (messages.length) _iosSlackLastMsgIds[channelId] = messages[messages.length-1].id;
                })
                .catch(() => { if (initial) msgEl.innerHTML = '<div style="padding:24px;text-align:center;color:#e01e5a;">Network error</div>'; });
        }

        function _iosSlackAppendMsg(msg) {
            const msgEl = document.getElementById('iosSlackMessages');
            const isSelf = (typeof Slack !== 'undefined') && Slack.user && msg.author_id === Slack.user.slack_user_id;
            const ts = msg.timestamp ? new Date(msg.timestamp) : null;
            const timeStr = ts ? ts.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}) : '';

            const avatar = msg.avatar_url
                ? `<img src="${msg.avatar_url}" style="width:32px;height:32px;border-radius:4px;object-fit:cover;flex-shrink:0;">`
                : `<div style="width:32px;height:32px;border-radius:4px;background:rgba(54,197,240,0.15);display:flex;align-items:center;justify-content:center;font-size:0.7rem;font-weight:800;color:#36c5f0;flex-shrink:0;">${(msg.author||'?')[0].toUpperCase()}</div>`;

            const div = document.createElement('div');
            div.style.cssText = 'display:flex;gap:10px;padding:4px 0;';
            div.innerHTML = `${avatar}
                <div style="flex:1;min-width:0;">
                    <div style="display:flex;align-items:baseline;gap:6px;">
                        <span style="font-weight:700;font-size:0.82rem;color:${isSelf ? '#36c5f0' : '#d1d2d3'};">${_iosEscHtml(msg.author||'Unknown')}</span>
                        <span style="font-size:0.65rem;color:#ababad;">${timeStr}</span>
                    </div>
                    <div style="font-size:0.82rem;color:#d1d2d3;line-height:1.4;word-break:break-word;">${_iosEscHtml(msg.content||'')}</div>
                </div>`;
            msgEl.appendChild(div);
        }

        function _iosSlackStartPoll() {
            _iosSlackStopPoll();
            _iosSlackPollTimer = setInterval(() => {
                if (_iosSlackActiveChannel) _iosSlackFetchMessages(_iosSlackActiveChannel, false);
            }, 4000);
        }
        function _iosSlackStopPoll() {
            if (_iosSlackPollTimer) { clearInterval(_iosSlackPollTimer); _iosSlackPollTimer = null; }
        }

        function iosSlackSend() {
            const textEl = document.getElementById('iosSlackReplyText');
            const text = textEl.value.trim();
            if (!text || !_iosSlackActiveChannel) return;
            textEl.disabled = true;
            fetch(`/api/slack/messages/${_iosSlackActiveChannel}`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({content: text})
            }).then(r => r.json()).then(data => {
                textEl.disabled = false;
                if (data.error) return;
                textEl.value = '';
                textEl.style.height = 'auto';
                _iosSlackFetchMessages(_iosSlackActiveChannel, false);
            }).catch(() => { textEl.disabled = false; });
        }

        function _iosSlackBackToChannels() {
            _iosSlackStopPoll();
            _iosSlackActiveChannel = null;
            document.getElementById('iosSlackChat').style.display = 'none';
            document.getElementById('iosSlackConnected').style.display = 'flex';
        }

        // Shared HTML escape
        function _iosEscHtml(str) {
            if (!str) return '';
            const d = document.createElement('div');
            d.textContent = str;
            return d.innerHTML;
        }

        // ═══════════════════════════════════════════════════════════════
        //  MULTI-LINE DIALER ENGINE
        //  Requires pro_dialer subscription tier.
        //  Fires up to 4 concurrent Twilio calls via POST /voice/multi-dial
        //  and polls all active calls via POST /voice/multi-status.
        // ═══════════════════════════════════════════════════════════════

        /**
         * Initialize multi-line dialer on page load.
         * Checks subscription tier and enables/disables multi-line UI.
         */
        async function multiLineInit() {
            try {
                const r = await fetch('/subscription-info');
                if (!r.ok) return;
                const info = await r.json();
                const tier = info.tier || 'individual';
                _multiLineEnabled = (tier === 'pro_dialer' || tier === 'solo_predictive');
                _predictiveEnabled = (tier === 'solo_predictive');
                // Use user-configured max_lines_setting, capped by subscription tier
                const configuredLines = window.DASHBOARD_BOOT?.maxLinesSetting ?? 3;
                _multiLineMaxLines = _multiLineEnabled ? Math.min(info.max_lines || 4, configuredLines) : 1;

                // Update dialer header to reflect actual tier
                const titleEl = document.getElementById('dialerTierTitle');
                if (titleEl) {
                    if (_predictiveEnabled) titleEl.textContent = 'Solo Predictive';
                    else if (_multiLineEnabled) titleEl.textContent = 'Pro Dialer';
                    else titleEl.textContent = 'Power Dialer';
                }

                // Show/hide multi-line UI elements
                const mlToggle = document.getElementById('multiLineToggle');
                if (mlToggle) mlToggle.style.display = _multiLineEnabled ? 'flex' : 'none';

                const mlBadge = document.getElementById('multiLineBadge');
                if (mlBadge) {
                    if (_predictiveEnabled) {
                        mlBadge.style.display = 'inline-flex';
                        mlBadge.textContent = 'PREDICTIVE';
                        mlBadge.style.background = 'linear-gradient(135deg,#8b5cf6,#6366f1)';
                    } else if (_multiLineEnabled) {
                        mlBadge.style.display = 'inline-flex';
                        mlBadge.textContent = 'MULTI-LINE';
                        mlBadge.style.background = 'linear-gradient(135deg,#ff6b35,#ff3366)';
                    } else {
                        mlBadge.style.display = 'none';
                    }
                }

                // Show/hide multi-line settings section in settings panel
                const mlSection = document.getElementById('multiLineSettingsSection');
                if (mlSection) mlSection.style.display = _multiLineEnabled ? '' : 'none';
                document.querySelectorAll('.multiline-setting').forEach(el => {
                    el.style.display = _multiLineEnabled ? '' : 'none';
                });

                // Show/hide pro-only settings in Voice Config dialer tab
                const proSection = document.getElementById('proDialerSection');
                if (proSection) proSection.style.display = _multiLineEnabled ? '' : 'none';

                // Load predictive stats for pro_dialer+ (basic stats for pro, full Erlang-C for predictive)
                if (_multiLineEnabled) {
                    multiLineLoadPredictiveStats();
                }

                // Hide enterprise-only panels for non-predictive tiers (Erlang-C, TCPA, agent state, callbacks)
                if (!_predictiveEnabled) {
                    const ids = ['complianceBanner', 'agentStatePanel', 'callbackQueueBanner', 'erlangCMetrics'];
                    ids.forEach(id => { const el = document.getElementById(id); if (el) el.style.display = 'none'; });
                }
            } catch (e) {
                console.error('[MultiLine] Init failed:', e);
            }
        }

        /**
         * Load predictive dialing statistics from the server.
         * Enhanced: supports Erlang-C metrics + AI overflow for solo_predictive tier.
         */
        async function multiLineLoadPredictiveStats() {
            try {
                const r = await fetch('/voice/predictive-stats');
                if (!r.ok) return;
                _predictiveStats = await r.json();
                _predictiveAutoLines = _predictiveStats.recommended_lines || 3;

                // Update UI with predictive stats
                const statsEl = document.getElementById('predictiveStatsPanel');
                if (statsEl && _predictiveStats) {
                    statsEl.style.display = 'block';
                    const crEl = document.getElementById('predictiveConnectRate');
                    const rlEl = document.getElementById('predictiveRecommendedLines');
                    const drEl = document.getElementById('predictiveDialRatio');
                    if (crEl) crEl.textContent = _predictiveStats.connect_rate + '%';
                    if (rlEl) rlEl.textContent = _predictiveStats.recommended_lines;
                    if (drEl) drEl.textContent = _predictiveStats.dial_ratio + ':1';

                    // Show algorithm badge
                    const algoEl = document.getElementById('predictiveAlgorithm');
                    if (algoEl && _predictiveStats.algorithm === 'erlang_c') {
                        algoEl.style.display = '';
                        algoEl.textContent = 'Erlang-C';
                    }

                    // Show Erlang-C extended metrics
                    const erlangEl = document.getElementById('erlangCMetrics');
                    if (erlangEl && _predictiveStats.algorithm === 'erlang_c') {
                        erlangEl.style.display = '';
                        const arEl = document.getElementById('predictiveAbandonRate');
                        const ecEl = document.getElementById('predictiveErlangC');
                        const ttEl = document.getElementById('predictiveThrottleTag');
                        if (arEl) {
                            const rate = _predictiveStats.current_abandon_rate || 0;
                            arEl.textContent = rate.toFixed(1) + '%';
                            arEl.style.color = rate > 3 ? '#ef4444' : rate > 2 ? '#fbbf24' : '#4ade80';
                        }
                        if (ecEl) ecEl.textContent = (_predictiveStats.erlang_c_probability || 0).toFixed(3);
                        if (ttEl) ttEl.style.display = _predictiveStats.throttled ? '' : 'none';
                    }
                }

                // Load compliance & agent state for solo_predictive tier
                if (_predictiveStats.algorithm === 'erlang_c') {
                    pdLoadCompliance();
                    pdLoadAgentState();
                    pdLoadCallbackQueue();
                }
            } catch (e) {
                console.error('[MultiLine] Predictive stats load failed:', e);
            }
        }

        // ═══ Predictive Dialer Enterprise Features ═══════════════════════════

        /** Load TCPA compliance status */
        async function pdLoadCompliance() {
            try {
                const r = await fetch('/voice/compliance');
                if (!r.ok) return;
                const d = await r.json();

                const banner = document.getElementById('complianceBanner');
                if (!banner) return;
                banner.style.display = 'block';

                const icon = document.getElementById('complianceIcon');
                const label = document.getElementById('complianceLabel');
                const rate = document.getElementById('complianceRate');
                const score = document.getElementById('complianceScore');

                if (d.tcpa?.critical) {
                    banner.style.background = 'rgba(239,68,68,0.06)';
                    banner.style.borderColor = 'rgba(239,68,68,0.2)';
                    if (icon) icon.textContent = '\u26a0\ufe0f';
                    if (label) { label.textContent = 'TCPA CRITICAL'; label.style.color = '#ef4444'; }
                } else if (d.tcpa?.warning) {
                    banner.style.background = 'rgba(251,191,36,0.06)';
                    banner.style.borderColor = 'rgba(251,191,36,0.2)';
                    if (icon) icon.textContent = '\u26a0\ufe0f';
                    if (label) { label.textContent = 'TCPA WARNING'; label.style.color = '#fbbf24'; }
                } else {
                    banner.style.background = 'rgba(74,222,128,0.04)';
                    banner.style.borderColor = 'rgba(74,222,128,0.12)';
                    if (icon) icon.textContent = '\u2705';
                    if (label) { label.textContent = 'TCPA COMPLIANT'; label.style.color = '#4ade80'; }
                }
                if (rate) rate.textContent = `Abandon: ${d.tcpa?.abandon_rate_30d ?? 0}% (30d) | ${d.tcpa?.abandon_rate_1h ?? 0}% (1h)`;
                if (score) {
                    score.textContent = `Score: ${d.compliance_score}/100`;
                    score.style.color = d.compliance_score >= 80 ? '#4ade80' : d.compliance_score >= 50 ? '#fbbf24' : '#ef4444';
                }
            } catch (e) { console.error('[PD] Compliance load failed:', e); }
        }

        /** Load and render agent state */
        async function pdLoadAgentState() {
            try {
                const r = await fetch('/voice/agent-state');
                if (!r.ok) return;
                const d = await r.json();

                const panel = document.getElementById('agentStatePanel');
                if (panel) panel.style.display = 'block';

                const sel = document.getElementById('agentStateSelect');
                const dot = document.getElementById('agentStateDot');
                const dur = document.getElementById('agentStateDuration');
                const wt = document.getElementById('agentWrapUpTimer');

                const state = d.my_state?.state || 'ready';
                if (sel) sel.value = state;

                const colors = { ready: '#4ade80', on_call: '#3b82f6', wrap_up: '#ff6b35', not_ready: '#ef4444', break: '#fbbf24', extended_away: '#888' };
                if (dot) dot.style.background = colors[state] || '#888';

                if (dur && d.my_state?.duration_sec != null) {
                    const mins = Math.floor(d.my_state.duration_sec / 60);
                    const secs = d.my_state.duration_sec % 60;
                    dur.textContent = `${mins}m ${secs}s`;
                }
                if (wt && d.my_state?.wrap_up_remaining_sec != null && state === 'wrap_up') {
                    wt.style.display = '';
                    wt.textContent = `Wrap-up: ${d.my_state.wrap_up_remaining_sec}s`;
                } else if (wt) {
                    wt.style.display = 'none';
                }
            } catch (e) { console.error('[PD] Agent state load failed:', e); }
        }

        /** Set agent state */
        async function pdSetAgentState(state) {
            try {
                const r = await fetch('/voice/agent-state', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': typeof csrf_token !== 'undefined' ? csrf_token : '' },
                    body: JSON.stringify({ state })
                });
                if (r.ok) pdLoadAgentState();
            } catch (e) { console.error('[PD] Set agent state failed:', e); }
        }

        /** Load callback queue count */
        async function pdLoadCallbackQueue() {
            try {
                const r = await fetch('/voice/callback-queue');
                if (!r.ok) return;
                const d = await r.json();

                const banner = document.getElementById('callbackQueueBanner');
                const countEl = document.getElementById('callbackQueueCount');
                const dueEl = document.getElementById('callbackDueCount');

                if (d.queue_size > 0 && banner) {
                    banner.style.display = 'block';
                    if (countEl) countEl.textContent = d.queue_size;
                    if (dueEl && d.due_count > 0) {
                        dueEl.style.display = '';
                        dueEl.textContent = `${d.due_count} due`;
                    } else if (dueEl) {
                        dueEl.style.display = 'none';
                    }
                } else if (banner) {
                    banner.style.display = 'none';
                }
            } catch (e) { console.error('[PD] Callback queue load failed:', e); }
        }

        /** Show callback queue details (placeholder — opens modal or panel) */
        function pdShowCallbackQueue() {
            _showDashToast(true, 'Callback queue panel coming soon');
        }

        /** Schedule a callback for a contact */
        async function pdScheduleCallback(contactId, phone, name, delayMinutes = 30, reason = 'no_answer') {
            try {
                const r = await fetch('/voice/callback-queue', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': typeof csrf_token !== 'undefined' ? csrf_token : '' },
                    body: JSON.stringify({ contact_id: contactId, phone, name, delay_minutes: delayMinutes, reason })
                });
                if (r.ok) {
                    _showDashToast(true, `Callback scheduled in ${delayMinutes} min`);
                    pdLoadCallbackQueue();
                }
            } catch (e) { console.error('[PD] Schedule callback failed:', e); }
        }

        /** Check recording consent for a contact and show banner if two-party state */
        async function pdCheckRecordingConsent(phone) {
            try {
                const r = await fetch(`/voice/recording-consent?phone=${encodeURIComponent(phone)}`);
                if (!r.ok) return;
                const d = await r.json();
                const banner = document.getElementById('recordingConsentBanner');
                const stateEl = document.getElementById('consentStateName');
                if (d.two_party_consent && banner) {
                    banner.style.display = 'block';
                    if (stateEl) stateEl.textContent = d.state || 'This state';
                } else if (banner) {
                    banner.style.display = 'none';
                }
            } catch (e) { /* silent */ }
        }

        /**
         * Start multi-line queue dialing.
         * Dials multiple contacts simultaneously from the queue.
         */
        async function multiLineStartQueue() {
            if (!_multiLineEnabled) {
                _showDashToast(false, 'Multi-line dialing requires Pro Dialer or Predictive Dialer plan');
                return;
            }
            if (dialerQueue.length === 0) {
                _showDashToast(false, 'Queue is empty');
                return;
            }

            // Client-side calling hours check (server also enforces)
            // Only runs when quiet hours are opt-in enabled
            const B = window.DASHBOARD_BOOT || {};
            const chStart = B.callingHoursStart || '08:00';
            const chEnd = B.callingHoursEnd || '21:00';
            if (B.quietHoursEnabled && chStart && chEnd) {
                const now = new Date();
                const nowMins = now.getHours() * 60 + now.getMinutes();
                const [sh, sm] = chStart.split(':').map(Number);
                const [eh, em] = chEnd.split(':').map(Number);
                const startMins = sh * 60 + sm;
                const endMins = eh * 60 + em;
                let outsideHours;
                if (endMins > startMins) {
                    // Normal range (e.g., 08:00-21:00)
                    outsideHours = nowMins < startMins || nowMins >= endMins;
                } else {
                    // Midnight wrap-around (e.g., 22:00-06:00): allowed if after start OR before end
                    outsideHours = nowMins < startMins && nowMins >= endMins;
                }
                if (outsideHours) {
                    _showDashToast(false, `Outside calling hours (${chStart}–${chEnd})`);
                    return;
                }
            }

            dialerQueueRunning = true;
            _multiLineQueueIdx = 0;
            dialerUpdateBtn();

            // Reset session tracking for auto-pacing
            _sessionCallsTotal = 0;
            _sessionCallsConnected = 0;
            _sessionTotalTalkTime = 0;
            _predictivePollCount = 0;
            _aiMinutesBannerShown = false;
            _overflowAlertShown = {};

            // For solo_predictive: fetch Erlang-C recommendation before first batch
            if (_isSoloPredictive) {
                try {
                    const statsR = await fetch('/voice/predictive-stats');
                    if (statsR.ok) {
                        const stats = await statsR.json();
                        _predictiveRecommendedLines = stats.recommended_lines || 2;
                        _multiLineMaxLines = Math.min(
                            _predictiveRecommendedLines,
                            window.DASHBOARD_BOOT?.maxLinesSetting || 4
                        );
                        const pct = stats.connect_rate || 0;
                        _showDashToast(true,
                            `Erlang-C: ${_predictiveRecommendedLines} lines (${pct}% connect rate)`
                        );
                    }
                } catch (e) { /* non-fatal */ }

                // Set agent state to READY
                try {
                    await fetch('/voice/agent-state', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({state: 'ready'})
                    });
                } catch (e) { /* non-fatal */ }

                // Start polling overflow transfer alerts
                if (_overflowAlertPollTimer) clearInterval(_overflowAlertPollTimer);
                _overflowAlertPollTimer = setInterval(_pollOverflowAlerts, 2000);
            }

            // Auto-expand queue body
            const qBody = document.getElementById('dialerQueueBody');
            if (qBody) qBody.style.display = 'block';

            // Dial first batch
            await multiLineDialBatch();

            // Start batch status polling (every 1.5s)
            if (_multiLinePollTimer) clearInterval(_multiLinePollTimer);
            _multiLinePollTimer = setInterval(multiLinePollAll, 1500);
        }

        /**
         * Dial a batch of contacts from the queue.
         * Picks the next N pending contacts where N = available lines.
         */
        async function multiLineDialBatch() {
            if (!dialerQueueRunning) return;
            if (_multiLineDialingInProgress) return; // Prevent concurrent batch dials
            _multiLineDialingInProgress = true;

            try {
            // For solo_predictive: use Erlang-C recommended lines (may differ from max setting)
            const effectiveMaxLines = _isSoloPredictive ? _predictiveRecommendedLines : _multiLineMaxLines;
            // Collect pending contacts up to available lines
            const activeCount = _multiLineActive.size;
            const available = effectiveMaxLines - activeCount;
            if (available <= 0) return;

            const pendingContacts = [];
            for (let i = 0; i < dialerQueue.length && pendingContacts.length < available; i++) {
                const item = dialerQueue[i];
                if (item.status !== 'pending') continue;

                // DnD check
                const contactObj = dialerContacts.find(x => x.id === item.id) || _dialerAllContacts.find(x => x.id === item.id);
                if (contactObj && _igbIsOptedOut(_igbEngagementCache[item.id], contactObj)) {
                    item.status = 'skipped';
                    continue;
                }

                if (!item.attempts) item.attempts = 0;
                item.attempts++;
                item.status = 'initiated';

                pendingContacts.push({
                    contact_id: item.id,
                    phone: item.phone,
                    first_name: item.firstName || item.name,
                    dial_attempt: item.attempts
                });
            }

            if (pendingContacts.length === 0) {
                // Check if there are any active calls still running
                if (_multiLineActive.size === 0) {
                    multiLineStopQueue();
                    _showDashToast(true, 'Queue complete');
                }
                return;
            }

            dialerRenderQueue();

            try {
                const r = await fetch('/voice/multi-dial', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        contacts: pendingContacts,
                        dial_mode: dialerMode,
                        max_lines: _multiLineMaxLines
                    })
                });

                if (!r.ok) {
                    const err = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
                    if (err.upgrade_required) {
                        _showDashToast(false, 'Upgrade to Pro Dialer for multi-line');
                        multiLineStopQueue();
                        return;
                    }
                    if (err.calling_hours_blocked) {
                        _showDashToast(false, err.error || 'Outside calling hours');
                        multiLineStopQueue();
                        return;
                    }
                    console.error('[MultiLine] Dial batch failed:', err.error || r.status);
                    // Mark failed contacts
                    pendingContacts.forEach(pc => {
                        const qi = dialerQueue.find(q => q.id === pc.contact_id);
                        if (qi) qi.status = 'failed';
                    });
                    dialerRenderQueue();
                    return;
                }

                const data = await r.json();
                (data.results || []).forEach(result => {
                    const qi = dialerQueue.find(q => q.id === result.contact_id);
                    if (!qi) { console.warn('[MultiLine] Result for unknown contact:', result.contact_id); return; }

                    if (result.status === 'initiated' && result.call_sid) {
                        qi.status = 'initiated';
                        qi._call_sid = result.call_sid;
                        _multiLineActive.set(result.call_sid, {
                            contact_id: result.contact_id,
                            phone: qi.phone,
                            name: qi.name,
                            status: 'initiated',
                            duration: 0,
                            dial_mode: dialerMode,
                            attempt: qi.attempts
                        });
                    } else if (result.status === 'skipped' || result.status === 'error') {
                        qi.status = result.status === 'skipped' ? 'skipped' : 'failed';
                        qi._error = result.error;
                    } else if (result.status === 'already_active') {
                        qi.status = 'in-progress';
                        qi._call_sid = result.call_sid;
                    }
                });

                // Handle AI minutes warning from server (solo_predictive overflow)
                if (data.minutes_warning && _isSoloPredictive) {
                    _aiMinutesWarning = data.minutes_warning;
                    if (data.minutes_warning === 'empty' && !_aiMinutesBannerShown) {
                        _aiMinutesBannerShown = true;
                        _showDashToast(false,
                            'Out of AI minutes \u2014 single-line mode (no overflow). Top up to resume predictive dialing.',
                            6000);
                        _predictiveRecommendedLines = 1;
                    } else if (data.minutes_warning === 'critical' && !_aiMinutesBannerShown) {
                        _aiMinutesBannerShown = true;
                        _showDashToast(false,
                            'AI Minutes low \u2014 overflow calls may fail. Top up now.',
                            5000);
                    }
                }

                // Track session stats for live pacing
                if (_isSoloPredictive) {
                    const initiated = (data.results || []).filter(r => r.status === 'initiated').length;
                    _sessionCallsTotal += initiated;
                }

                multiLineRenderBanner();
                dialerRenderQueue();

            } catch (e) {
                console.error('[MultiLine] Dial batch network error:', e);
                pendingContacts.forEach(pc => {
                    const qi = dialerQueue.find(q => q.id === pc.contact_id);
                    if (qi) qi.status = 'failed';
                });
                dialerRenderQueue();
            }
            } finally {
                _multiLineDialingInProgress = false;
            }
        }

        /**
         * Poll status of all active multi-line calls.
         */
        async function multiLinePollAll() {
            // If wrap-up timer is pending, don't advance — let it expire first
            if (_dialerWrapUpTimer) return;

            if (_multiLineActive.size === 0) {
                // No active calls — try to dial more or stop
                if (dialerQueueRunning) {
                    // Check disposition gate before advancing
                    if (_dialerRequireDisposition) {
                        const undispositioned = dialerQueue.filter(q =>
                            q.status === 'completed' && !q.disposition
                        );
                        if (undispositioned.length > 0) {
                            if (!_multiLineDispToastShown) {
                                _showDashToast(false, 'Set disposition before next batch');
                                _multiLineDispToastShown = true;
                            }
                            return; // Wait for user to set dispositions
                        }
                        _multiLineDispToastShown = false;
                    }
                    const hasPending = dialerQueue.some(q => q.status === 'pending');
                    if (hasPending) {
                        await multiLineDialBatch();
                    } else {
                        multiLineStopQueue();
                        _showDashToast(true, 'Queue complete — all contacts dialed');
                    }
                }
                return;
            }

            const sids = Array.from(_multiLineActive.keys());

            try {
                const r = await fetch('/voice/multi-status', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ call_sids: sids })
                });
                if (!r.ok) {
                    _multiLinePollErrors++;
                    console.warn('[MultiLine] Poll failed:', r.status, `(error ${_multiLinePollErrors})`);
                    if (_multiLinePollErrors >= 10) {
                        _showDashToast(false, 'Lost connection to server — stopping dialer');
                        multiLineStopQueue();
                    }
                    return;
                }
                _multiLinePollErrors = 0; // Reset on success
                const data = await r.json();
                const statuses = data.statuses || {};

                const terminalStatuses = new Set(['completed', 'busy', 'no-answer', 'failed', 'canceled', 'unknown', 'transferred']);
                let anyTerminated = false;
                let anyConnected = false;

                // Safety net: if server didn't return a SID we asked about, treat as completed
                for (const sid of sids) {
                    if (!statuses[sid]) {
                        console.log(`[MultiLine] SID ${sid.slice(0,16)} missing from server response — treating as completed`);
                        statuses[sid] = { status: 'completed' };
                    }
                }

                for (const [sid, serverInfo] of Object.entries(statuses)) {
                    const lineInfo = _multiLineActive.get(sid);
                    if (!lineInfo) continue;

                    const oldStatus = lineInfo.status;
                    lineInfo.status = serverInfo.status;
                    lineInfo.duration = serverInfo.duration || 0;

                    // Update queue item status
                    const qi = dialerQueue.find(q => q._call_sid === sid);
                    if (qi) qi.status = serverInfo.status;

                    // Detect connection (someone picked up)
                    if (serverInfo.status === 'in-progress' && oldStatus !== 'in-progress') {
                        anyConnected = true;
                        // Track session connection for live pacing
                        if (_isSoloPredictive) _sessionCallsConnected++;
                        // First connected call becomes the primary line
                        if (!_multiLineConnectedSid) {
                            _multiLineConnectedSid = sid;
                            // Auto-select this contact in detail panel
                            if (lineInfo.contact_id) {
                                dialerSelectContact(lineInfo.contact_id);
                                _jtcFlashContact(lineInfo.contact_id);
                            }
                        } else {
                            // Additional lines connected — show notification
                            _showDashToast(true, `${lineInfo.name || 'Contact'} picked up on line ${Array.from(_multiLineActive.keys()).indexOf(sid) + 1}`);
                        }
                    }

                    // Handle terminal states
                    if (terminalStatuses.has(serverInfo.status)) {
                        anyTerminated = true;
                        _multiLineActive.delete(sid);

                        // Auto-disposition: set disposition automatically for no-answer/voicemail
                        if (qi) {
                            if (_dialerAutoDispNoAnswer && serverInfo.status === 'no-answer') {
                                qi.disposition = 'no_answer';
                            }
                            if (_dialerAutoDispVoicemail && serverInfo.amd_result) {
                                // Server sets amd_result='no-answer' for ALL AMD detections (machine_start, fax, etc.)
                                // The field only exists when AMD was triggered, so its presence means voicemail
                                qi.disposition = 'voicemail';
                                qi.status = 'no-answer'; // AMD-detected VM — use no-answer so retry logic picks it up
                            }
                        }

                        // If this was the connected call, trigger wrap-up and auto-select next active line
                        if (_multiLineConnectedSid === sid) {
                            // Try to find another in-progress line to auto-switch to
                            const nextActive = Array.from(_multiLineActive.entries()).find(([s, li]) => s !== sid && li.status === 'in-progress');
                            _multiLineConnectedSid = nextActive ? nextActive[0] : null;
                            if (nextActive && nextActive[1].contact_id) {
                                dialerSelectContact(nextActive[1].contact_id);
                            }
                            if (serverInfo.status === 'completed' && _dialerWrapUpTime > 0) {
                                _dialerWrapUpActive = true;
                            }
                        }

                        // Check retry logic (same as single-line: respects dialerMaxAttempts)
                        if (qi && ['no-answer', 'busy', 'failed', 'canceled'].includes(serverInfo.status)) {
                            if ((qi.attempts || 0) < dialerMaxAttempts) {
                                qi.status = 'pending';
                            }
                        }
                    }
                }

                // Track completed call duration for session pacing
                if (_isSoloPredictive && anyTerminated) {
                    for (const [sid, serverInfo] of Object.entries(statuses)) {
                        if (terminalStatuses.has(serverInfo.status) && serverInfo.duration > 0) {
                            _sessionTotalTalkTime += (serverInfo.duration || 0);
                        }
                    }
                }

                // Periodic live pacing re-fetch (every ~15 seconds = 10 poll cycles)
                if (_isSoloPredictive && dialerQueueRunning) {
                    _predictivePollCount++;
                    if (_predictivePollCount % 10 === 0) {
                        _refreshLivePacing();
                    }
                }

                multiLineRenderBanner();
                dialerRenderQueue();

                // If any calls terminated, try to dial more from queue
                if (anyTerminated && dialerQueueRunning) {
                    // If wrap-up is active, wait for wrap-up time before advancing
                    if (_dialerWrapUpActive && _dialerWrapUpTime > 0) {
                        _dialerWrapUpActive = false;
                        if (_dialerWrapUpTimer) clearTimeout(_dialerWrapUpTimer);
                        _dialerWrapUpTimer = setTimeout(() => {
                            _dialerWrapUpTimer = null;
                            if (!dialerQueueRunning) return;

                            // If require_disposition is on, check that the last connected call was dispositioned
                            if (_dialerRequireDisposition) {
                                const undispositioned = dialerQueue.filter(q =>
                                    q.status === 'completed' && !q.disposition
                                );
                                if (undispositioned.length > 0) {
                                    if (!_multiLineDispToastShown) {
                                        _showDashToast(false, 'Set disposition before next batch');
                                        _multiLineDispToastShown = true;
                                    }
                                    return; // Poll will retry on next cycle
                                }
                            }
                            _multiLineDispToastShown = false;

                            const hasPending = dialerQueue.some(q => q.status === 'pending');
                            if (hasPending) {
                                multiLineDialBatch();
                            } else if (_multiLineActive.size === 0) {
                                multiLineStopQueue();
                                _showDashToast(true, 'Queue complete — all contacts dialed');
                            }
                        }, _dialerWrapUpTime);
                    } else {
                        // Even without wrap-up, check disposition requirement
                        if (_dialerRequireDisposition) {
                            const undispositioned = dialerQueue.filter(q =>
                                q.status === 'completed' && !q.disposition
                            );
                            if (undispositioned.length > 0) {
                                if (!_multiLineDispToastShown) {
                                    _showDashToast(false, 'Set disposition before next batch');
                                    _multiLineDispToastShown = true;
                                }
                                return; // Poll will retry on next cycle
                            }
                        }
                        _multiLineDispToastShown = false;
                        const hasPending = dialerQueue.some(q => q.status === 'pending');
                        if (hasPending) {
                            // Use configured pause between calls — track timer for cleanup
                            if (_multiLinePauseTimer) clearTimeout(_multiLinePauseTimer);
                            _multiLinePauseTimer = setTimeout(() => { _multiLinePauseTimer = null; multiLineDialBatch(); }, _dialerPauseBetween || 1000);
                        } else if (_multiLineActive.size === 0) {
                            multiLineStopQueue();
                            _showDashToast(true, 'Queue complete — all contacts dialed');
                        }
                    }
                }

            } catch (e) {
                _multiLinePollErrors++;
                console.error('[MultiLine] Poll error:', e, `(error ${_multiLinePollErrors})`);
                if (_multiLinePollErrors >= 10) {
                    _showDashToast(false, 'Lost connection to server — stopping dialer');
                    multiLineStopQueue();
                }
            }
        }

        /**
         * Stop multi-line queue and hang up all active calls.
         */
        function multiLineStopQueue() {
            dialerQueueRunning = false;
            _multiLineQueueIdx = -1;
            _multiLineConnectedSid = null;
            _dialerWrapUpActive = false;
            _multiLineDialingInProgress = false;
            _multiLinePollErrors = 0;
            _multiLineDispToastShown = false;
            _aiMinutesBannerShown = false;
            _aiMinutesWarning = null;
            if (_dialerWrapUpTimer) { clearTimeout(_dialerWrapUpTimer); _dialerWrapUpTimer = null; }
            if (_multiLinePauseTimer) { clearTimeout(_multiLinePauseTimer); _multiLinePauseTimer = null; }
            if (_overflowAlertPollTimer) { clearInterval(_overflowAlertPollTimer); _overflowAlertPollTimer = null; }
            dialerUpdateBtn();

            if (_multiLinePollTimer) {
                clearInterval(_multiLinePollTimer);
                _multiLinePollTimer = null;
            }

            // Hang up all active calls
            if (_multiLineActive.size > 0) {
                const sids = Array.from(_multiLineActive.keys());
                _multiLineActive.clear();

                fetch('/voice/multi-hangup', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ call_sids: sids })
                }).then(async r => {
                    if (r.ok) {
                        const d = await r.json();
                        console.log('[MultiLine] Hung up', d.results?.length || 0, 'calls');
                    }
                }).catch(e => console.error('[MultiLine] Multi-hangup error:', e));
            }

            multiLineRenderBanner();
            dialerRenderQueue();
        }

        /**
         * Hang up a specific line in multi-line mode.
         */
        function multiLineHangupLine(callSid) {
            const lineInfo = _multiLineActive.get(callSid);
            _multiLineActive.delete(callSid);

            if (_multiLineConnectedSid === callSid) {
                _multiLineConnectedSid = null;
            }

            fetch('/voice/hangup', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ call_sid: callSid })
            }).catch(e => console.error('[MultiLine] Hangup line error:', e));

            // Update queue item
            const qi = dialerQueue.find(q => q._call_sid === callSid);
            if (qi) qi.status = 'completed';

            multiLineRenderBanner();
            dialerRenderQueue();
        }

        /**
         * Connect to a specific line (take over / listen to that call).
         */
        function multiLineConnectToLine(callSid) {
            _multiLineConnectedSid = callSid;
            const lineInfo = _multiLineActive.get(callSid);
            if (lineInfo && lineInfo.contact_id) {
                dialerSelectContact(lineInfo.contact_id);
            }
            multiLineRenderBanner();
        }

        // ── Solo Predictive: Live Pacing Re-fetch ──
        async function _refreshLivePacing() {
            if (!_isSoloPredictive || !dialerQueueRunning) return;
            try {
                const sessionAHT = _sessionCallsConnected > 0
                    ? (_sessionTotalTalkTime / _sessionCallsConnected)
                    : 0;
                const url = '/voice/predictive-stats'
                    + `?session_calls_total=${_sessionCallsTotal}`
                    + `&session_calls_connected=${_sessionCallsConnected}`
                    + `&session_avg_handle_time=${Math.round(sessionAHT)}`;
                const r = await fetch(url);
                if (!r.ok) return;
                const stats = await r.json();
                const prev = _predictiveRecommendedLines;
                _predictiveRecommendedLines = stats.recommended_lines || prev;
                // Cap by user's max setting
                _predictiveRecommendedLines = Math.min(
                    _predictiveRecommendedLines,
                    window.DASHBOARD_BOOT?.maxLinesSetting || 4
                );
                if (_predictiveRecommendedLines !== prev) {
                    const dir = _predictiveRecommendedLines > prev ? 'up' : 'down';
                    const pct = stats.blended_connect_rate || stats.connect_rate || 0;
                    _showDashToast(true,
                        `Pacing adjusted ${dir} to ${_predictiveRecommendedLines} lines (${pct}% connect rate)`
                    );
                }
            } catch (e) { /* non-fatal */ }
        }

        // ── Solo Predictive: Overflow Transfer Alert Polling ──
        async function _pollOverflowAlerts() {
            if (!_isSoloPredictive || !dialerQueueRunning) return;
            try {
                const r = await fetch('/voice/overflow-alerts');
                if (!r.ok) return;
                const data = await r.json();
                const alerts = data.alerts || [];
                for (const alert of alerts) {
                    if (_overflowAlertShown[alert.call_sid]) continue;
                    _overflowAlertShown[alert.call_sid] = true;
                    _showOverflowTransferAlert(alert);
                }
            } catch (e) { /* non-fatal */ }
        }

        function _showOverflowTransferAlert(alert) {
            // Create a persistent notification card for hot lead transfer request
            const el = document.createElement('div');
            el.id = 'overflow-alert-' + (alert.call_sid || '').slice(0, 12);
            el.className = 'overflow-transfer-alert';
            el.style.cssText = 'position:fixed;top:80px;right:24px;z-index:99998;'
                + 'background:linear-gradient(135deg,rgba(255,80,40,0.95),rgba(200,40,20,0.95));'
                + 'color:#fff;padding:16px 20px;border-radius:12px;'
                + 'box-shadow:0 8px 32px rgba(0,0,0,0.4);max-width:340px;'
                + 'animation:_toastSlideIn .3s ease;font-size:0.9rem;';
            const name = alert.contact_name || 'Lead';
            el.innerHTML = '<div style="font-weight:700;font-size:1rem;margin-bottom:6px;">'
                + '<i class="fa-solid fa-fire" style="margin-right:6px;"></i>Hot Lead: ' + name + '</div>'
                + '<div style="margin-bottom:12px;opacity:0.95;">AI overflow call \u2014 wants to talk to you. AI is keeping them engaged.</div>'
                + '<div style="display:flex;gap:8px;">'
                + '<button onclick="_acceptOverflowTransfer(\'' + (alert.call_sid || '') + '\',this)" '
                + 'style="flex:1;padding:8px 12px;border:none;border-radius:8px;'
                + 'background:#fff;color:#c02820;font-weight:700;cursor:pointer;">Accept Transfer</button>'
                + '<button onclick="_dismissOverflowTransfer(\'' + (alert.call_sid || '') + '\',this)" '
                + 'style="flex:1;padding:8px 12px;border:none;border-radius:8px;'
                + 'background:rgba(255,255,255,0.2);color:#fff;font-weight:600;cursor:pointer;">Let AI Book</button>'
                + '</div>';
            document.body.appendChild(el);
            // Auto-dismiss after 30 seconds
            setTimeout(() => {
                const existing = document.getElementById(el.id);
                if (existing) {
                    existing.style.transition = 'opacity .3s';
                    existing.style.opacity = '0';
                    setTimeout(() => existing.remove(), 300);
                }
            }, 30000);
        }

        // Accept: takeover the overflow call
        window._acceptOverflowTransfer = async function(callSid, btn) {
            if (btn) btn.disabled = true;
            try {
                const r = await fetch('/voice/takeover', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({call_sid: callSid})
                });
                if (r.ok) {
                    _showDashToast(true, 'Transfer accepted \u2014 connecting to lead');
                } else {
                    const err = await r.json().catch(() => ({}));
                    _showDashToast(false, err.error || 'Could not transfer \u2014 AI will book instead');
                }
            } catch (e) {
                _showDashToast(false, 'Transfer failed \u2014 AI will book instead');
            }
            const el = btn?.closest('.overflow-transfer-alert');
            if (el) { el.style.transition = 'opacity .3s'; el.style.opacity = '0'; setTimeout(() => el.remove(), 300); }
        };

        // Dismiss: tell server, AI continues to booking
        window._dismissOverflowTransfer = async function(callSid, btn) {
            try {
                await fetch('/voice/overflow-alerts/dismiss', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({call_sid: callSid})
                });
            } catch (e) { /* non-fatal */ }
            const el = btn?.closest('.overflow-transfer-alert');
            if (el) { el.style.transition = 'opacity .3s'; el.style.opacity = '0'; setTimeout(() => el.remove(), 300); }
        };

        /**
         * Render the multi-line call banner showing all active lines.
         */
        function multiLineRenderBanner() {
            const banner = document.getElementById('multiLineBanner');
            if (!banner) return;

            if (_multiLineActive.size === 0 && !dialerQueueRunning) {
                banner.style.display = 'none';
                return;
            }

            banner.style.display = 'block';

            const lines = Array.from(_multiLineActive.entries());
            const statusColors = {
                'initiated': '#00d9ff',
                'ringing': '#00d9ff',
                'in-progress': '#4ade80',
                'completed': '#666',
                'busy': '#ffa500',
                'no-answer': '#ffa500',
                'failed': '#ef4444',
            };

            let html = '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">';
            html += '<div style="font-size:0.82rem;font-weight:700;color:#fff;"><i class="fa-solid fa-phone-volume me-2" style="color:#ff6b35;"></i>Multi-Line Dialer';
            html += '<span style="margin-left:8px;font-size:0.75rem;color:#888;">(' + lines.length + '/' + _multiLineMaxLines + ' lines)</span></div>';
            html += '<button onclick="multiLineStopQueue()" style="background:rgba(239,68,68,0.15);border:1px solid rgba(239,68,68,0.3);color:#ef4444;border-radius:6px;padding:3px 10px;font-size:0.75rem;font-weight:700;cursor:pointer;">Stop All</button>';
            html += '</div>';

            if (lines.length === 0 && dialerQueueRunning) {
                html += '<div style="font-size:0.78rem;color:#888;text-align:center;padding:6px;">Dialing next batch...</div>';
            }

            lines.forEach(([sid, info], idx) => {
                const color = statusColors[info.status] || '#888';
                const isConnected = info.status === 'in-progress';
                const isPrimary = sid === _multiLineConnectedSid;
                const dur = info.duration ? Math.floor(info.duration / 60) + ':' + String(info.duration % 60).padStart(2, '0') : '';

                html += '<div style="display:flex;align-items:center;gap:8px;padding:5px 8px;margin-bottom:3px;border-radius:8px;background:' +
                    (isPrimary ? 'rgba(74,222,128,0.06)' : 'rgba(255,255,255,0.02)') +
                    ';border:1px solid ' + (isPrimary ? 'rgba(74,222,128,0.15)' : 'rgba(255,255,255,0.04)') + ';">';

                // Line number
                html += '<span style="width:20px;height:20px;border-radius:50%;background:' + color + '20;color:' + color + ';display:flex;align-items:center;justify-content:center;font-size:0.72rem;font-weight:800;flex-shrink:0;">L' + (idx + 1) + '</span>';

                // Contact name + status
                html += '<div style="flex:1;min-width:0;">';
                html += '<div style="font-size:0.82rem;font-weight:600;color:#fff;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">' + dialerEsc(info.name || info.phone) + '</div>';
                html += '<div style="font-size:0.72rem;color:' + color + ';">' + (info.status || 'unknown');
                if (dur) html += ' &middot; ' + dur;
                html += '</div></div>';

                // Action buttons
                if (isConnected && !isPrimary) {
                    html += '<button onclick="multiLineConnectToLine(\'' + sid + '\')" style="background:rgba(74,222,128,0.1);border:1px solid rgba(74,222,128,0.2);color:#4ade80;border-radius:5px;padding:2px 8px;font-size:0.72rem;cursor:pointer;" title="Switch to this line"><i class="fa-solid fa-headset"></i></button>';
                }
                if (isPrimary) {
                    html += '<span style="font-size:0.68rem;color:#4ade80;font-weight:700;padding:2px 6px;">ACTIVE</span>';
                }
                html += '<button onclick="multiLineHangupLine(\'' + sid + '\')" style="background:rgba(239,68,68,0.1);border:1px solid rgba(239,68,68,0.2);color:#ef4444;border-radius:5px;padding:2px 8px;font-size:0.72rem;cursor:pointer;" title="Hang up this line"><i class="fa-solid fa-phone-slash"></i></button>';

                html += '</div>';
            });

            banner.innerHTML = html;
        }

        /**
         * Override dialerToggleQueue to support multi-line mode.
         * If multi-line is enabled, use multi-line queue; otherwise use single-line.
         */
        const _originalDialerToggleQueue = dialerToggleQueue;
        dialerToggleQueue = function() {
            if (_multiLineEnabled && _multiLineMaxLines > 1) {
                if (dialerQueueRunning) {
                    multiLineStopQueue();
                } else {
                    // Reset pending items
                    dialerQueue.forEach(q => {
                        if (q.status !== 'completed' && q.status !== 'skipped') q.status = 'pending';
                    });
                    multiLineStartQueue();
                }
            } else {
                _originalDialerToggleQueue();
            }
        };

        // ═══════════════════════════════════════════════════════════════
        //  BILLING PLAN MANAGEMENT (for billing tab)
        // ═══════════════════════════════════════════════════════════════

        let _billingCurrentTier = 'individual';
        let _billingSelectedTier = null;
        let _billingHasSubscription = false;

        async function billingLoadPlanInfo() {
            try {
                const r = await fetch('/subscription-info');
                if (!r.ok) return;
                const info = await r.json();
                _billingCurrentTier = info.tier || 'individual';
                _billingHasSubscription = info.has_subscription || false;

                const planEl = document.getElementById('billingCurrentPlan');
                if (planEl) {
                    if (_billingHasSubscription) {
                        planEl.textContent = 'Current plan: ' + info.name + ' (' + info.price + ')';
                    } else {
                        planEl.textContent = 'No active subscription — select a plan to get started';
                    }
                }

                // Highlight current plan card
                const smsCard = document.getElementById('billingPlanSmsBbot');
                const indCard = document.getElementById('billingPlanIndividual');
                const proCard = document.getElementById('billingPlanProDialer');
                const predCard = document.getElementById('billingPlanPredictiveDialer');
                const smsBadge = document.getElementById('billingBadgeSmsBbot');
                const indBadge = document.getElementById('billingBadgeIndividual');
                const proBadge = document.getElementById('billingBadgeProDialer');
                const predBadge = document.getElementById('billingBadgePredictiveDialer');

                // Reset all cards
                const allCards = [smsCard, indCard, proCard, predCard];
                const allBadges = [smsBadge, indBadge, proBadge, predBadge];
                allCards.forEach(c => { if (c) c.style.borderColor = 'rgba(255,255,255,0.08)'; });
                allBadges.forEach(b => { if (b) b.style.display = 'none'; });

                if (_billingCurrentTier === 'solo_predictive') {
                    if (predCard) predCard.style.borderColor = 'rgba(139,92,246,0.4)';
                    if (predBadge) predBadge.style.display = 'block';
                } else if (_billingCurrentTier === 'pro_dialer') {
                    if (proCard) proCard.style.borderColor = 'rgba(255,107,53,0.4)';
                    if (proBadge) proBadge.style.display = 'block';
                } else if (_billingCurrentTier === 'sms_bot') {
                    if (smsCard) smsCard.style.borderColor = 'rgba(0,217,255,0.4)';
                    if (smsBadge) smsBadge.style.display = 'block';
                } else {
                    if (indCard) indCard.style.borderColor = 'rgba(0,255,136,0.4)';
                    if (indBadge) indBadge.style.display = 'block';
                }
            } catch (e) {
                console.error('[Billing] Load plan info failed:', e);
            }
        }

        function billingSelectPlan(tier) {
            if (tier === _billingCurrentTier) {
                _billingSelectedTier = null;
                const btn = document.getElementById('billingChangePlanBtn');
                if (btn) btn.style.display = 'none';
                return;
            }
            _billingSelectedTier = tier;

            const smsCard = document.getElementById('billingPlanSmsBbot');
            const indCard = document.getElementById('billingPlanIndividual');
            const proCard = document.getElementById('billingPlanProDialer');
            const predCard = document.getElementById('billingPlanPredictiveDialer');
            [smsCard, indCard, proCard, predCard].forEach(c => { if (c) c.style.borderColor = 'rgba(255,255,255,0.08)'; });
            if (tier === 'sms_bot' && smsCard) smsCard.style.borderColor = 'rgba(0,217,255,0.4)';
            if (tier === 'individual' && indCard) indCard.style.borderColor = 'rgba(0,255,136,0.4)';
            if (tier === 'pro_dialer' && proCard) proCard.style.borderColor = 'rgba(255,107,53,0.4)';
            if (tier === 'solo_predictive' && predCard) predCard.style.borderColor = 'rgba(139,92,246,0.4)';

            const btn = document.getElementById('billingChangePlanBtn');
            if (btn) {
                btn.style.display = 'inline-flex';
                const tierNames = { sms_bot: 'SMS Bot ($99.98/mo)', individual: 'Power Dialer ($149.99/mo)', pro_dialer: 'Pro Dialer ($224.99/mo)', solo_predictive: 'Solo Predictive ($349.98/mo)' };
                if (_billingHasSubscription) {
                    btn.innerHTML = '<i class="fa-solid fa-arrows-rotate me-2"></i>Switch to ' + (tierNames[tier] || tier);
                    btn.onclick = billingChangePlan;
                } else {
                    btn.innerHTML = '<i class="fa-solid fa-credit-card me-2"></i>Subscribe to ' + (tierNames[tier] || tier);
                    btn.onclick = function() { billingCheckout(tier); };
                }
            }
        }

        function billingCheckout(tier) {
            const checkoutUrls = {
                sms_bot: '/checkout/sms-bot',
                individual: '/checkout',
                pro_dialer: '/checkout/pro-dialer',
                solo_predictive: '/checkout/predictive-dialer',
            };
            const url = checkoutUrls[tier] || '/checkout';
            window.location.href = url;
        }

        async function billingChangePlan() {
            if (!_billingSelectedTier || _billingSelectedTier === _billingCurrentTier) return;

            const btn = document.getElementById('billingChangePlanBtn');
            if (!btn) return;
            const origHTML = btn.innerHTML;
            btn.disabled = true;
            btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-2"></i>Changing plan...';

            try {
                const r = await fetch('/change-plan', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ target_tier: _billingSelectedTier })
                });
                let data;
                try { data = await r.json(); } catch (_) { data = {}; }
                if (r.ok && data.success) {
                    _showDashToast(true, data.message || 'Plan changed successfully');
                    _billingCurrentTier = _billingSelectedTier;
                    _billingSelectedTier = null;
                    btn.style.display = 'none';
                    // Reload plan info
                    billingLoadPlanInfo();
                    // Re-init multi-line state
                    multiLineInit();
                } else {
                    _showDashToast(false, data.error || 'Failed to change plan');
                }
            } catch (e) {
                _showDashToast(false, 'Network error — plan change failed');
            } finally {
                btn.disabled = false;
                btn.innerHTML = origHTML;
            }
        }

        /* ═══════════════════════════════════════════════════════════
         *  EXPORT CONTACTS — CSV Download with Filters
         * ═══════════════════════════════════════════════════════════ */
        let _dlrExportSelectedTags = new Set();

        function dlrExportOpen() {
            const modal = document.getElementById('dlrExportModal');
            if (!modal) return;
            modal.style.display = 'flex';
            _dlrExportSelectedTags.clear();
            // Reset filters
            const s = document.getElementById('dlrExportSearch'); if (s) s.value = '';
            const df = document.getElementById('dlrExportDateFrom'); if (df) df.value = '';
            const dt = document.getElementById('dlrExportDateTo'); if (dt) dt.value = '';
            const dnd = document.getElementById('dlrExportDnd'); if (dnd) dnd.value = '';
            // Load tags
            _dlrExportLoadTags();
        }
        window.dlrExportOpen = dlrExportOpen;

        function dlrExportClose() {
            const modal = document.getElementById('dlrExportModal');
            if (modal) modal.style.display = 'none';
        }
        window.dlrExportClose = dlrExportClose;

        async function _dlrExportLoadTags() {
            const container = document.getElementById('dlrExportTags');
            if (!container) return;
            try {
                const resp = await fetch('/voice/contacts/tags');
                const data = await resp.json();
                const tags = data.tags || [];
                if (!tags.length) {
                    container.innerHTML = '<span style="color:#555;font-size:0.75rem;">No tags found</span>';
                    return;
                }
                container.innerHTML = tags.map(t => {
                    const esc = t.replace(/"/g, '&quot;').replace(/</g, '&lt;');
                    return `<button class="dlr-export-tag-chip" data-tag="${esc}" onclick="dlrExportToggleTag(this)">${esc}</button>`;
                }).join('');
            } catch (e) {
                container.innerHTML = '<span style="color:#f44;font-size:0.75rem;">Failed to load tags</span>';
            }
        }

        function dlrExportToggleTag(btn) {
            const tag = btn.dataset.tag;
            if (_dlrExportSelectedTags.has(tag)) {
                _dlrExportSelectedTags.delete(tag);
                btn.classList.remove('active');
            } else {
                _dlrExportSelectedTags.add(tag);
                btn.classList.add('active');
            }
        }
        window.dlrExportToggleTag = dlrExportToggleTag;

        async function dlrExportDownload() {
            const btn = document.getElementById('dlrExportBtn');
            if (!btn) return;
            const origHTML = btn.innerHTML;
            btn.disabled = true;
            btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-1"></i>Exporting...';

            const body = {};
            const search = (document.getElementById('dlrExportSearch') || {}).value || '';
            if (search.trim()) body.search = search.trim();
            const dateFrom = (document.getElementById('dlrExportDateFrom') || {}).value || '';
            if (dateFrom) body.date_from = dateFrom;
            const dateTo = (document.getElementById('dlrExportDateTo') || {}).value || '';
            if (dateTo) body.date_to = dateTo;
            const dndVal = (document.getElementById('dlrExportDnd') || {}).value || '';
            if (dndVal === 'true') body.dnd = true;
            else if (dndVal === 'false') body.dnd = false;
            if (_dlrExportSelectedTags.size > 0) body.tags = [..._dlrExportSelectedTags];

            try {
                const resp = await fetch('/voice/contacts/export', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': _csrfToken },
                    body: JSON.stringify(body)
                });
                if (!resp.ok) {
                    const errData = await resp.json().catch(() => ({}));
                    throw new Error(errData.error || `HTTP ${resp.status}`);
                }
                // Download the CSV blob
                const blob = await resp.blob();
                const url = window.URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = resp.headers.get('Content-Disposition')?.split('filename=')[1]?.replace(/"/g, '') || 'contacts-export.csv';
                document.body.appendChild(a);
                a.click();
                a.remove();
                window.URL.revokeObjectURL(url);
                if (typeof _showDashToast === 'function') _showDashToast(true, 'Contacts exported successfully');
                dlrExportClose();
            } catch (e) {
                if (typeof _showDashToast === 'function') _showDashToast(false, 'Export failed: ' + e.message);
            } finally {
                btn.disabled = false;
                btn.innerHTML = origHTML;
            }
        }
        window.dlrExportDownload = dlrExportDownload;

        // ══════════════════════════════════════════════
        // ── Call Insights App ──
        // ══════════════════════════════════════════════
        var insightsDetailVisible = false;
        var _insightsCache = [];
        window.insightsDetailVisible = false;

        async function iosInsightsInit() {
            insightsDetailVisible = false;
            window.insightsDetailVisible = false;
            const listView = document.getElementById('insightsListView');
            const detailView = document.getElementById('insightsDetailView');
            if (listView) listView.style.display = '';
            if (detailView) detailView.style.display = 'none';

            const panel = document.getElementById('insightsList');
            if (!panel) return;
            panel.innerHTML = '<div style="text-align:center;padding:40px;"><i class="fa-solid fa-spinner fa-spin insights-empty-icon-color" style="font-size:1.2rem;"></i></div>';

            try {
                const r = await _fetchRetry('/voice/call-insights?limit=100', {}, { retries: 1, timeout: 15000, label: 'insights-list' });
                if (!r.ok) { panel.innerHTML = '<div class="insights-disconnected" style="padding:20px;text-align:center;">Failed to load insights</div>'; return; }
                const d = await r.json();
                const calls = d.calls || [];
                _insightsCache = calls;

                if (!calls.length) {
                    panel.innerHTML = '<div class="ios-empty-state insights-empty">' +
                        '<div class="ios-empty-icon dlr-bg-bordered"><i class="fa-solid fa-chart-line insights-empty-icon-color"></i></div>' +
                        '<div class="ios-empty-title">No Insights Yet</div>' +
                        '<div class="ios-empty-sub">Call insights appear ~90s after each call ends.<br>Make sure Advanced Features is enabled in your Twilio Console.</div>' +
                        '</div>';
                    return;
                }

                panel.innerHTML = calls.map(function(c, idx) {
                    var dirIcon = (c.direction || 'outbound') === 'inbound'
                        ? '<i class="fa-solid fa-phone-arrow-down-left" style="font-size:.62rem;"></i>'
                        : '<i class="fa-solid fa-phone-arrow-up-right" style="font-size:.62rem;"></i>';
                    var dt = c.created_at ? new Date(c.created_at).toLocaleString([], {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}) : '';
                    var sipCode = c.last_sip_response;
                    var sipClass = _insightsSipClass(sipCode);
                    var sipHtml = sipCode ? '<span class="insights-sip-badge ' + sipClass + '">SIP ' + dialerEsc(String(sipCode)) + '</span>' : '';
                    var pddHtml = c.pdd_ms != null ? '<span class="insights-pdd">' + c.pdd_ms + 'ms PDD</span>' : '';
                    var carrierHtml = c.to_carrier ? '<span class="insights-carrier"><i class="fa-solid fa-tower-cell" style="font-size:.6rem;margin-right:2px;"></i>' + dialerEsc(c.to_carrier) + '</span>' : '';
                    var discHtml = c.disconnected_by ? '<span class="insights-disconnected"><i class="fa-solid fa-phone-slash" style="font-size:.58rem;margin-right:2px;"></i>' + dialerEsc(c.disconnected_by) + '</span>' : '';
                    var tagsHtml = (c.quality_tags || []).map(function(t) { return '<span class="insights-tag">' + dialerEsc(t) + '</span>'; }).join('');
                    var dur = c.duration ? Math.floor(c.duration / 60) + ':' + String(c.duration % 60).padStart(2, '0') : '--:--';

                    var row = '<div class="insights-row" onclick="insightsShowDetail(' + idx + ')">';
                    row += '<div class="insights-row-top">';
                    row += '<span class="insights-row-name">' + dialerEsc(c.contact_name) + '</span>';
                    row += '<span class="insights-row-dir">' + dirIcon + '</span>';
                    row += '<span class="chr-dur">' + dur + '</span>';
                    row += '<span class="insights-row-date">' + dt + '</span>';
                    row += '</div>';
                    row += '<div class="insights-row-bottom">';
                    row += sipHtml + pddHtml + carrierHtml + discHtml + tagsHtml;
                    row += '</div>';
                    row += '</div>';
                    return row;
                }).join('');
            } catch (e) {
                panel.innerHTML = '<div class="insights-disconnected" style="padding:20px;text-align:center;">Error loading insights</div>';
            }
        }
        window.iosInsightsInit = iosInsightsInit;

        function _insightsSipClass(code) {
            if (!code) return '';
            var n = parseInt(code, 10);
            if (n >= 200 && n < 300) return 'insights-sip-2xx';
            if (n >= 300 && n < 400) return 'insights-sip-3xx';
            if (n >= 400 && n < 500) return 'insights-sip-4xx';
            if (n >= 500 && n < 600) return 'insights-sip-5xx';
            if (n >= 600) return 'insights-sip-6xx';
            return '';
        }

        function _insightsSipColorClass(code) {
            if (!code) return '';
            var n = parseInt(code, 10);
            if (n >= 200 && n < 300) return 'sip-ok';
            if (n >= 300 && n < 500) return 'sip-warn';
            return 'sip-err';
        }

        var _sipDescriptions = {
            '180': 'Ringing', '183': 'Session Progress', '200': 'OK (Connected)',
            '400': 'Bad Request', '401': 'Unauthorized', '403': 'Forbidden',
            '404': 'Not Found', '408': 'Request Timeout', '480': 'Temporarily Unavailable',
            '484': 'Address Incomplete', '486': 'Busy Here', '487': 'Request Terminated',
            '488': 'Not Acceptable Here', '500': 'Server Internal Error',
            '502': 'Bad Gateway', '503': 'Service Unavailable',
            '504': 'Server Timeout', '600': 'Busy Everywhere',
            '603': 'Decline', '604': 'Does Not Exist Anywhere'
        };

        async function insightsShowDetail(idx) {
            var c = _insightsCache[idx];
            if (!c) return;

            insightsDetailVisible = true;
            window.insightsDetailVisible = true;
            var listView = document.getElementById('insightsListView');
            var detailView = document.getElementById('insightsDetailView');
            if (listView) listView.style.display = 'none';
            if (detailView) { detailView.style.display = 'flex'; detailView.style.animation = 'iosAppOpen 0.25s cubic-bezier(0.2,0.9,0.3,1)'; }

            var content = document.getElementById('insightsDetailContent');
            if (!content) return;

            // Fetch full detail
            var full = c;
            try {
                var r = await _fetchRetry('/voice/call-insights/' + encodeURIComponent(c.call_sid), {}, { retries: 1, timeout: 10000, label: 'insights-detail' });
                if (r.ok) {
                    var fd = await r.json();
                    if (fd && fd.call_sid) full = Object.assign({}, c, fd);
                }
            } catch (e) { /* use cached data */ }

            var sipCode = full.last_sip_response;
            var sipDesc = sipCode ? (_sipDescriptions[String(sipCode)] || 'Response ' + sipCode) : 'No SIP Data';
            var sipColorClass = _insightsSipColorClass(sipCode);
            var dur = full.duration ? Math.floor(full.duration / 60) + ':' + String(full.duration % 60).padStart(2, '0') : '--:--';
            var dt = full.created_at ? new Date(full.created_at).toLocaleString() : '';

            // Trust / STIR info
            var trust = full.trust || {};
            var stirVerstat = trust.verified_caller || trust.verstat || '';
            var trustHtml = '';
            if (stirVerstat) {
                var tClass = stirVerstat.toLowerCase().indexOf('tn-validation') >= 0 || stirVerstat.toLowerCase().indexOf('pass') >= 0
                    ? 'insights-trust-verified' : stirVerstat.toLowerCase().indexOf('fail') >= 0
                    ? 'insights-trust-unverified' : 'insights-trust-unknown';
                trustHtml = '<span class="insights-trust-badge ' + tClass + '"><i class="fa-solid fa-shield-check" style="font-size:.65rem;"></i> ' + dialerEsc(stirVerstat) + '</span>';
            }

            // Carrier edge
            var ce = full.carrier_edge || {};
            var props = full.properties || {};

            var html = '';

            // Hero SIP card
            html += '<div class="insights-card">';
            html += '<div class="insights-hero-sip ' + sipColorClass + '">' + (sipCode ? 'SIP ' + dialerEsc(String(sipCode)) : '—') + '</div>';
            html += '<div class="insights-hero-label">' + dialerEsc(sipDesc) + '</div>';
            if (trustHtml) html += '<div style="text-align:center;margin-bottom:4px;">' + trustHtml + '</div>';
            html += '</div>';

            // Call Summary card
            html += '<div class="insights-card">';
            html += '<div class="insights-card-title"><i class="fa-solid fa-phone"></i> Call Summary</div>';
            html += _insightsMetric('Contact', dialerEsc(full.contact_name || full.phone || 'Unknown'));
            html += _insightsMetric('Direction', (full.direction || 'outbound'));
            html += _insightsMetric('Status', (full.status || '').replace(/-/g, ' '));
            html += _insightsMetric('Duration', dur);
            html += _insightsMetric('Call State', full.call_state || '—');
            html += _insightsMetric('Call Type', full.call_type || '—');
            html += _insightsMetric('Disconnected By', full.disconnected_by || '—');
            html += _insightsMetric('Date', dt);
            html += '</div>';

            // Network & Timing card
            html += '<div class="insights-card">';
            html += '<div class="insights-card-title"><i class="fa-solid fa-gauge-high"></i> Network &amp; Timing</div>';
            html += _insightsMetric('Post-Dial Delay', full.pdd_ms != null ? full.pdd_ms + ' ms' : '—');
            if (ce.metrics) {
                var m = ce.metrics;
                html += _insightsMetric('Jitter (avg)', m.jitter ? (m.jitter.avg != null ? m.jitter.avg.toFixed(1) + ' ms' : '—') : '—');
                html += _insightsMetric('Packet Loss', m.packet_loss ? (m.packet_loss.avg != null ? (m.packet_loss.avg * 100).toFixed(2) + '%' : '—') : '—');
                html += _insightsMetric('Latency (avg)', m.latency ? (m.latency.avg != null ? m.latency.avg.toFixed(0) + ' ms' : '—') : '—');
            }
            html += '</div>';

            // Carrier Info card
            html += '<div class="insights-card">';
            html += '<div class="insights-card-title"><i class="fa-solid fa-tower-cell"></i> Carrier Info</div>';
            html += _insightsMetric('From Carrier', full.from_carrier || '—');
            html += _insightsMetric('To Carrier', full.to_carrier || '—');
            if (full.stir_status) html += _insightsMetric('STIR/SHAKEN', full.stir_status);
            html += '</div>';

            // Quality Tags card
            var tags = full.quality_tags || full.tags || [];
            if (tags.length) {
                html += '<div class="insights-card">';
                html += '<div class="insights-card-title"><i class="fa-solid fa-tags"></i> Quality Tags</div>';
                html += '<div style="display:flex;flex-wrap:wrap;gap:6px;">';
                tags.forEach(function(t) {
                    html += '<span class="insights-tag" style="font-size:0.75rem;padding:3px 8px;">' + dialerEsc(t) + '</span>';
                });
                html += '</div></div>';
            }

            // Raw Properties card (expandable)
            var propKeys = Object.keys(props);
            if (propKeys.length) {
                html += '<div class="insights-card">';
                html += '<div class="insights-card-title" style="cursor:pointer;" onclick="this.nextElementSibling.style.display=this.nextElementSibling.style.display===\'none\'?\'block\':\'none\'">';
                html += '<i class="fa-solid fa-code"></i> Raw Properties <i class="fa-solid fa-chevron-down" style="font-size:.55rem;margin-left:auto;"></i></div>';
                html += '<div style="display:none;">';
                propKeys.forEach(function(k) {
                    html += _insightsMetric(k, String(props[k] != null ? props[k] : '—'));
                });
                html += '</div></div>';
            }

            content.innerHTML = html;
        }
        window.insightsShowDetail = insightsShowDetail;

        function insightsBackToList() {
            insightsDetailVisible = false;
            window.insightsDetailVisible = false;
            var listView = document.getElementById('insightsListView');
            var detailView = document.getElementById('insightsDetailView');
            if (listView) listView.style.display = '';
            if (detailView) detailView.style.display = 'none';
        }
        window.insightsBackToList = insightsBackToList;

        function _insightsMetric(label, value) {
            return '<div class="insights-metric-row">' +
                '<span class="insights-metric-label">' + dialerEsc(label) + '</span>' +
                '<span class="insights-metric-value">' + dialerEsc(String(value)) + '</span>' +
                '</div>';
        }

        // ═══════════════════════════════════════════════════════════════
        //  MOVE PIPELINE PANEL
        // ═══════════════════════════════════════════════════════════════

        let _mvpContactIds    = [];   // contact IDs being moved
        let _mvpPipelineId    = null; // selected pipeline id
        let _mvpPipelineName  = '';
        let _mvpStageId       = null; // selected stage id (optional)
        let _mvpStageName     = '';
        let _mvpOpenPipe      = null; // which pipeline accordion is expanded

        // Open for a single contact row button
        function _mvpOpenSingle(contactId) {
            _mvpContactIds = [contactId];
            _mvpShow();
        }

        // Open for the bulk selection
        function _mvpOpen() {
            if (dialerSelected.size === 0) return;
            _mvpContactIds = Array.from(dialerSelected);
            _mvpShow();
        }

        function _mvpShow() {
            // Reset state
            _mvpPipelineId   = null;
            _mvpPipelineName = '';
            _mvpStageId      = null;
            _mvpStageName    = '';
            _mvpOpenPipe     = null;

            // Update count label
            const countEl = document.getElementById('mvpCountLabel');
            if (countEl) countEl.textContent = _mvpContactIds.length === 1 ? '1 contact' : _mvpContactIds.length + ' contacts';

            // Clear search
            const search = document.getElementById('mvpSearch');
            if (search) search.value = '';

            // Reset footer
            _mvpUpdateFooter();

            // Render pipeline list
            _mvpRender('');

            // Show overlay
            document.getElementById('mvpOverlay').classList.add('open');
        }

        function _mvpClose() {
            document.getElementById('mvpOverlay').classList.remove('open');
        }

        function _mvpFilter() {
            const q = (document.getElementById('mvpSearch').value || '').trim().toLowerCase();
            _mvpRender(q);
        }

        function _mvpRender(q) {
            const list = document.getElementById('mvpList');
            if (!list) return;

            const pipes = (dialerPipelines || []).filter(p =>
                !q || p.name.toLowerCase().includes(q)
            );

            if (!pipes.length) {
                list.innerHTML = '<div class="mvp-none">' + (q ? 'No pipelines match' : 'No pipelines found') + '</div>';
                return;
            }

            list.innerHTML = pipes.map(p => {
                const isSelectedPipe = _mvpPipelineId === p.id;
                const isOpen = _mvpOpenPipe === p.id;
                let stagesHtml = '';
                if (isOpen && p.stages && p.stages.length) {
                    stagesHtml = p.stages.map(s => {
                        const isSel = _mvpStageId === s.id;
                        return '<div class="mvp-stage-item' + (isSel ? ' selected' : '') + '" onclick="_mvpSelectStage(\'' +
                            _mvpEsc(p.id) + '\',\'' + _mvpEsc(p.name) + '\',\'' + _mvpEsc(s.id) + '\',\'' + _mvpEsc(s.name) + '\')">' +
                            '<span class="mvp-stage-dot"></span>' + _mvpEscHtml(s.name) + '</div>';
                    }).join('');
                }
                return '<div class="mvp-pipe-item">' +
                    '<div class="mvp-pipe-hdr' + (isSelectedPipe ? ' selected' : '') + (isOpen ? ' open' : '') +
                    '" onclick="_mvpTogglePipe(\'' + _mvpEsc(p.id) + '\',\'' + _mvpEsc(p.name) + '\')">' +
                    _mvpEscHtml(p.name) +
                    (p.stages && p.stages.length ? '<span class="mvp-pipe-arrow fa-solid fa-chevron-down"></span>' : '') +
                    '</div>' +
                    (isOpen ? '<div class="mvp-stages open">' + stagesHtml + '</div>' : '') +
                    '</div>';
            }).join('');
        }

        function _mvpTogglePipe(pipelineId, pipelineName) {
            if (_mvpOpenPipe === pipelineId) {
                // Collapse — keep pipeline selected, clear stage
                _mvpOpenPipe    = null;
                _mvpPipelineId  = pipelineId;
                _mvpPipelineName = pipelineName;
                _mvpStageId     = null;
                _mvpStageName   = '';
            } else {
                // Expand this pipeline
                _mvpOpenPipe    = pipelineId;
                _mvpPipelineId  = pipelineId;
                _mvpPipelineName = pipelineName;
                _mvpStageId     = null;
                _mvpStageName   = '';
            }
            _mvpUpdateFooter();
            const q = (document.getElementById('mvpSearch').value || '').trim().toLowerCase();
            _mvpRender(q);
        }

        function _mvpSelectStage(pipelineId, pipelineName, stageId, stageName) {
            _mvpPipelineId   = pipelineId;
            _mvpPipelineName = pipelineName;
            _mvpStageId      = stageId;
            _mvpStageName    = stageName;
            _mvpUpdateFooter();
            const q = (document.getElementById('mvpSearch').value || '').trim().toLowerCase();
            _mvpRender(q);
        }

        function _mvpUpdateFooter() {
            const dest = document.getElementById('mvpDestLabel');
            const btn  = document.getElementById('mvpMoveBtn');
            if (!dest || !btn) return;
            if (_mvpPipelineId) {
                dest.textContent = _mvpStageName
                    ? _mvpPipelineName + '  —  ' + _mvpStageName
                    : _mvpPipelineName;
                btn.disabled = false;
            } else {
                dest.textContent = '';
                btn.disabled = true;
            }
        }

        async function _mvpExecute() {
            if (!_mvpPipelineId || !_mvpContactIds.length) return;
            const btn = document.getElementById('mvpMoveBtn');
            btn.disabled = true;
            btn.textContent = 'Moving...';
            try {
                const body = { contact_ids: _mvpContactIds, pipeline_id: _mvpPipelineId };
                if (_mvpStageId) body.stage_id = _mvpStageId;
                const r = await fetch('/voice/move-pipeline', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body),
                });
                const d = await r.json();
                if (!r.ok) {
                    _showDashToast(false, d.error || 'Move failed');
                } else {
                    const total = (d.moved || 0) + (d.created || 0);
                    const fail  = d.failed || 0;
                    if (fail === 0) {
                        _showDashToast(true, total + ' contact' + (total === 1 ? '' : 's') + ' moved to ' + _mvpPipelineName + (_mvpStageName ? ' — ' + _mvpStageName : ''));
                    } else {
                        _showDashToast(false, total + ' moved, ' + fail + ' failed');
                    }
                    _mvpClose();
                    dialerSelected.clear();
                    dialerUpdateSelectionUI();
                    dialerRenderContacts();
                }
            } catch(e) {
                _showDashToast(false, 'Network error');
            } finally {
                btn.disabled = false;
                btn.textContent = 'Move';
            }
        }

        function _mvpEsc(s) {
            return (s || '').replace(/\\/g, '\\\\').replace(/'/g, "\\'");
        }
        function _mvpEscHtml(s) {
            const d = document.createElement('div');
            d.textContent = s || '';
            return d.innerHTML;
        }

        // Auto-init on page load
        function _dialerPageInit() {
            multiLineInit();
            billingLoadPlanInfo();
            dialerLoadKpiBanner();
            // Human mode is default — apply initial mode state (button styling, VoIP init, AI-only controls hidden)
            setDialerMode(dialerMode);
        }
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', _dialerPageInit);
        } else {
            _dialerPageInit();
        }

