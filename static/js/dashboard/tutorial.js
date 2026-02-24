// ================================================================
// InsuranceGrokBot — Dashboard Interactive Tutorial (driver.js)
// Enterprise-grade onboarding walkthrough covering every UI element
// ================================================================
(function () {
    'use strict';

    // ── Constants ───────────────────────────────────────────────
    var STORAGE_PREFIX = 'igb_tutorial_v1_';
    var TOTAL_CHAPTERS = 11;

    // ── CSS Injection ──────────────────────────────────────────
    function injectStyles() {
        if (document.getElementById('igb-tutorial-css')) return;
        var style = document.createElement('style');
        style.id = 'igb-tutorial-css';
        style.textContent = [
            /* Overlay */
            '.driver-overlay { background: rgba(0,0,0,0.72) !important; }',

            /* Base popover — dark glass matching dashboard */
            '.driver-popover { background: #12121e !important; color: #d0d0d0 !important; border: 1px solid rgba(0,255,136,0.12) !important; border-radius: 14px !important; box-shadow: 0 24px 80px rgba(0,0,0,0.65), 0 0 40px rgba(0,255,136,0.04) !important; max-width: 420px !important; font-family: "Outfit", sans-serif !important; }',
            '.driver-popover-title { color: #00ff88 !important; font-weight: 600 !important; font-size: 0.95rem !important; line-height: 1.4 !important; }',
            '.driver-popover-description { color: #b0b0b8 !important; font-size: 0.82rem !important; line-height: 1.6 !important; margin-top: 6px !important; }',
            '.driver-popover-description p { margin: 0 0 6px; }',
            '.driver-popover-close-btn { color: #555 !important; }',
            '.driver-popover-close-btn:hover { color: #fff !important; }',

            /* Progress text */
            '.driver-popover-progress-text { color: #555 !important; font-size: 0.7rem !important; font-family: "JetBrains Mono", monospace !important; }',

            /* Navigation buttons */
            '.driver-popover-navigation-btns { gap: 8px !important; }',
            '.driver-popover-next-btn { background: linear-gradient(135deg, #00ff88, #00b36b) !important; color: #000 !important; border: none !important; border-radius: 8px !important; font-weight: 600 !important; font-size: 0.8rem !important; padding: 7px 18px !important; text-shadow: none !important; }',
            '.driver-popover-next-btn:hover { filter: brightness(1.1) !important; }',
            '.driver-popover-prev-btn { background: rgba(255,255,255,0.07) !important; color: #888 !important; border: 1px solid rgba(255,255,255,0.1) !important; border-radius: 8px !important; font-weight: 500 !important; font-size: 0.8rem !important; padding: 7px 18px !important; }',
            '.driver-popover-prev-btn:hover { background: rgba(255,255,255,0.12) !important; color: #bbb !important; }',

            /* Arrow */
            '.driver-popover-arrow-side-left.driver-popover-arrow { border-right-color: #12121e !important; }',
            '.driver-popover-arrow-side-right.driver-popover-arrow { border-left-color: #12121e !important; }',
            '.driver-popover-arrow-side-top.driver-popover-arrow { border-bottom-color: #12121e !important; }',
            '.driver-popover-arrow-side-bottom.driver-popover-arrow { border-top-color: #12121e !important; }',

            /* Chapter marker popovers */
            '.igb-tut-chapter .driver-popover-title { font-size: 1.15rem !important; text-align: center !important; }',
            '.igb-tut-chapter .driver-popover-description { text-align: center !important; }',
            '.igb-tut-chapter { max-width: 480px !important; border: 1px solid rgba(0,217,255,0.15) !important; }',

            /* Chapter number badge */
            '.igb-ch-badge { display: inline-block; font-size: 0.6rem; font-weight: 700; text-transform: uppercase; letter-spacing: 2.5px; color: #00d9ff; background: rgba(0,217,255,0.08); border: 1px solid rgba(0,217,255,0.18); padding: 2px 10px; border-radius: 20px; margin-bottom: 6px; }',

            /* Skip chapter button */
            '.igb-tut-skip { display: inline-block; margin-top: 10px; background: none; border: 1px solid rgba(255,255,255,0.12); color: #777; font-size: 0.72rem; padding: 4px 14px; border-radius: 6px; cursor: pointer; transition: all 0.15s; }',
            '.igb-tut-skip:hover { border-color: rgba(0,217,255,0.3); color: #00d9ff; background: rgba(0,217,255,0.05); }',

            /* Finish popover */
            '.igb-tut-finish { max-width: 500px !important; border: 1px solid rgba(0,255,136,0.2) !important; }',
            '.igb-tut-finish .driver-popover-title { font-size: 1.2rem !important; text-align: center !important; }',
            '.igb-tut-finish .driver-popover-description { text-align: center !important; }',

            /* Keyboard hint */
            '.igb-key-hint { display: block; margin-top: 8px; font-size: 0.68rem; color: #555; }',
            '.igb-key-hint kbd { background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.12); border-radius: 3px; padding: 1px 5px; font-family: "JetBrains Mono", monospace; font-size: 0.65rem; }'
        ].join('\n');
        document.head.appendChild(style);
    }

    // ── Persistence ────────────────────────────────────────────
    function storageKey() {
        var boot = window.DASHBOARD_BOOT || {};
        var id = boot.userEmail || boot.locationId || 'anon';
        return STORAGE_PREFIX + id;
    }
    function isTutorialDone()  { return localStorage.getItem(storageKey()) === '1'; }
    function markTutorialDone(){ localStorage.setItem(storageKey(), '1'); }

    // ── Navigation Helpers ─────────────────────────────────────
    var TAB_BTN_MAP = {
        voicedialer: 'sbnDialer',
        config:      'sbnSmsConfig',
        voice:       'sbnVoiceConfig',
        workflows:   'sbnWorkflows',
        connect:     'sbnConnect',
        carriers:    'sbnCarriers',
        advanced:    'sbnAdvanced',
        aiminutes:   'sbnAiMinutes',
        billing:     'sbnBilling',
        logs:        'sbnLogs'
    };

    function goTab(tabId) {
        if (typeof sidebarNavigate !== 'function') return;
        var btn = document.getElementById(TAB_BTN_MAP[tabId]);
        sidebarNavigate(tabId, btn);
    }

    function ensureOpen(sectionId) {
        var el = document.getElementById(sectionId);
        if (el && !el.classList.contains('open')) el.classList.add('open');
    }

    function showEl(id, disp) {
        var el = document.getElementById(id);
        if (el) el.style.display = disp || 'flex';
    }
    function hideEl(id) {
        var el = document.getElementById(id);
        if (el) el.style.display = 'none';
    }

    // ── Step Builder Helpers ───────────────────────────────────
    function chapterStep(num, title, desc) {
        return {
            popover: {
                title: '<span class="igb-ch-badge">Chapter ' + num + ' of ' + TOTAL_CHAPTERS + '</span><br>' + title,
                description: desc,
                popoverClass: 'igb-tut-chapter'
            }
        };
    }

    // ── Build All Steps ────────────────────────────────────────
    function buildSteps() {
        var steps = [];
        var chapterStarts = [];

        // ============================================================
        // CHAPTER 1 — Welcome
        // ============================================================
        chapterStarts.push(steps.length);
        steps.push({
            popover: {
                title: '<span class="igb-ch-badge">Welcome</span><br>Welcome to InsuranceGrokBot',
                description: 'Your AI-powered insurance sales command center. This interactive walkthrough covers <strong>every button and feature</strong> in your dashboard — from the power dialer to voice AI, SMS bot, CRM integrations, and more.<br><br>It takes about 5–7 minutes. You can replay it anytime from the sidebar.<span class="igb-key-hint">Use <kbd>→</kbd> and <kbd>←</kbd> arrow keys to navigate, or <kbd>Esc</kbd> to close.</span>',
                popoverClass: 'igb-tut-chapter'
            }
        });

        // ============================================================
        // CHAPTER 2 — Sidebar Navigation & Layout
        // ============================================================
        chapterStarts.push(steps.length);
        steps.push(chapterStep(2, 'Navigation &amp; Layout', 'The sidebar is your main navigation hub. Every tool and setting is one click away. Let\'s walk through each section.'));

        steps.push({ element: '#mainSidebar',
            popover: { title: 'Sidebar', description: 'Your main navigation panel. Everything in the dashboard is organized into tabs here. Click any item to switch views instantly.', side: 'right', align: 'start' }
        });
        steps.push({ element: '#sbToggle',
            popover: { title: 'Collapse / Expand', description: 'Toggle the sidebar between full and icon-only mode. Useful on smaller screens or when you want more workspace.', side: 'right' }
        });
        steps.push({ element: '#sbnDialer',
            popover: { title: 'Dialer', description: 'Your <strong>Power Dialer</strong> — the main workspace for calling leads. Includes contact lists, call queue, live controls, and messaging history.', side: 'right' },
            onHighlightStarted: function() { ensureOpen('sbSectionPrimary'); }
        });
        steps.push({ element: '#sbnSmsConfig',
            popover: { title: 'SMS Config', description: 'Configure your <strong>SMS bot</strong> — set the bot name, initial outbound message, calendar for bookings, and timezone.', side: 'right' }
        });
        steps.push({ element: '#sbnVoiceConfig',
            popover: { title: 'Voice Config', description: 'Configure your <strong>Voice AI agent</strong> — choose the AI voice, write the call script, set behavior rules, and activate voice calling.', side: 'right' }
        });
        steps.push({ element: '#sbnWorkflows',
            popover: { title: 'Workflows', description: 'Step-by-step guides for setting up <strong>automation workflows</strong> in your CRM — re-engagement loops and SMS auto-reply triggers.', side: 'right' }
        });
        steps.push({ element: '#sbnConnect',
            popover: { title: 'Connect CRM', description: 'Connect your <strong>CRM system</strong> (GoHighLevel, HubSpot, Salesforce, Pipedrive, Zoho, Zapier, or Insureio). Also manage API keys and webhook URLs.', side: 'right' },
            onHighlightStarted: function() { ensureOpen('sbSectionCRM'); }
        });
        steps.push({ element: '#sbnCarriers',
            popover: { title: 'Carriers', description: 'Select the <strong>insurance carriers you\'re contracted with</strong>. The AI only references carriers you\'ve chosen — no hallucinated quotes.', side: 'right' }
        });
        steps.push({ element: '#sbnAdvanced',
            popover: { title: 'Advanced Settings', description: 'Fine-tune the bot\'s personality — professionalism, humor, response length, objection handling, after-hours mode, and custom behavior instructions.', side: 'right' },
            onHighlightStarted: function() { ensureOpen('sbSectionMore'); }
        });
        steps.push({ element: '#sbnAiMinutes',
            popover: { title: 'AI Minutes', description: 'Check your <strong>AI voice minute balance</strong>, purchase additional minute packages, and view usage history.', side: 'right' }
        });
        steps.push({ element: '#sbnBilling',
            popover: { title: 'Billing', description: 'Manage your subscription and payment method through the Stripe billing portal.', side: 'right' }
        });
        steps.push({ element: '#sbnLogs',
            popover: { title: 'Activity Logs', description: 'Audit log of every webhook, message sent, booking attempt, and error — filterable by event type and status. Great for debugging.', side: 'right' }
        });

        // Topbar elements
        steps.push({ element: '#themeToggleBtn',
            popover: { title: 'Theme Toggle', description: 'Switch between <strong>dark mode</strong> and <strong>light mode</strong>. Your preference is saved automatically.', side: 'bottom' }
        });
        steps.push({ element: '#discordBellBtn',
            popover: { title: 'Discord Notifications', description: 'Quick access to your Discord team chat. The badge shows <strong>unread message count</strong> across your connected servers.', side: 'bottom' }
        });

        // ============================================================
        // CHAPTER 3 — Power Dialer: Contacts & Queue
        // ============================================================
        chapterStarts.push(steps.length);
        steps.push(chapterStep(3, 'Power Dialer — Contacts &amp; Queue', 'The Power Dialer is your daily driver. Let\'s start with how to load contacts, build a call queue, and launch auto-dial sessions.'));

        steps.push({ element: '.dlr-top-bar',
            popover: { title: 'Dialer Control Bar', description: 'Everything you need for dialing lives here — manual phone input, AI/Human mode, latency monitor, stats, and settings.', side: 'bottom' },
            onHighlightStarted: function() { goTab('voicedialer'); }
        });
        steps.push({ element: '#dialModeType',
            popover: { title: 'Manual Dial — Type Mode', description: 'Type any phone number in <strong>E.164 format</strong> (+1XXXXXXXXXX) and press Enter or click the green phone button to call. You can also just type 10 digits and we\'ll format it.', side: 'bottom' }
        });
        steps.push({ element: '#dialKbdTabKeypad',
            popover: { title: 'Keypad Mode', description: 'Switch to a classic <strong>phone keypad</strong> for dialing. Tap digits 0–9, *, # — just like a real phone. Great for quick-dialing numbers from memory.', side: 'bottom' }
        });
        steps.push({ element: '#modeAiBtn',
            popover: { title: 'AI Mode', description: 'In <strong>AI mode</strong>, the voice AI agent handles the conversation — greeting, qualifying, and booking leads. You can listen in, mute, or intercept anytime.', side: 'bottom' }
        });
        steps.push({ element: '#modeHumanBtn',
            popover: { title: 'Human / Browser Mode', description: 'In <strong>Human mode</strong>, YOU talk to the lead directly through your browser mic and speakers (VoIP). No AI involvement — just a regular phone call from your dashboard.', side: 'bottom' }
        });
        steps.push({ element: '#dialerLatency',
            popover: { title: 'Latency Monitor', description: 'Real-time <strong>ping indicator</strong> showing connection quality to the voice servers. Green = great (<100ms), Orange = okay (<300ms), Red = poor.', side: 'bottom' }
        });
        steps.push({ element: '#dialerStatsToggle',
            popover: { title: 'Call Statistics', description: 'Open the <strong>stats panel</strong> to view call KPIs — total calls, durations, hourly breakdown, top contacts. Filter by today, 7 days, 30 days, or all time.', side: 'bottom' }
        });
        steps.push({ element: '#dialerSettingsToggle',
            popover: { title: 'Dialer Settings', description: 'Configure dial attempts, auto-recording, auto-transcription, local presence, voicemail drop, transfer number, and audio device selection.', side: 'bottom' }
        });

        // Contacts column
        steps.push({ element: '#dialerGetContactsBtn',
            popover: { title: 'Get Contacts', description: 'Pull contacts from your connected CRM. They\'ll load into the list below, complete with names, phone numbers, and call count badges.', side: 'right' }
        });
        steps.push({ element: '#dialerSearch',
            popover: { title: 'Search Contacts', description: 'Instantly filter loaded contacts by <strong>name or phone number</strong>. Typing starts filtering immediately.', side: 'right' }
        });
        steps.push({ element: '#dialerPipelineFilter',
            popover: { title: 'Pipeline Filter', description: 'Filter contacts by CRM <strong>pipeline and stage</strong>. Perfect for targeting specific lead groups — e.g., "New Leads" or "Follow Up Required".', side: 'right' }
        });
        steps.push({ element: '#dialerAddSelectedBtn',
            popover: { title: 'Queue Button', description: 'Add selected contacts to the <strong>call queue</strong>. Select contacts using the checkboxes, then click this to queue them up for auto-dialing.', side: 'right' }
        });
        steps.push({ element: '#dialerCallSelectedBtn',
            popover: { title: 'Dial Now', description: 'Add selected contacts to the queue AND <strong>immediately start dialing</strong>. The auto-dialer will call each contact in order.', side: 'right' }
        });
        steps.push({ element: '#dialerStartBtn',
            popover: { title: 'Auto-Dial / Stop', description: 'Start or stop the <strong>power dialer queue</strong>. When running, it automatically calls each queued contact, waits for the call to end, then moves to the next.', side: 'right' }
        });

        // ============================================================
        // CHAPTER 4 — Power Dialer: Making Calls & Live Controls
        // ============================================================
        chapterStarts.push(steps.length);
        steps.push(chapterStep(4, 'Power Dialer — Call Controls', 'When a call is active, you have real-time controls for listening in, muting, intercepting, and transferring. Let\'s see each one.'));

        // Temporarily show call banner for the tutorial
        steps.push({ element: '#dialerCallBanner',
            popover: { title: 'Active Call Banner', description: 'When a call is in progress, this banner shows the <strong>contact name, call status</strong> (ringing, connected), and a live <strong>duration timer</strong>. The red button hangs up.', side: 'bottom' },
            onHighlightStarted: function() {
                goTab('voicedialer');
                showEl('dialerCallBanner', 'flex');
                var n = document.getElementById('dialerCallName');
                if (n && !n.textContent.trim()) n.textContent = 'John Smith';
                var s = document.getElementById('dialerCallStatus');
                if (s && !s.textContent.trim()) s.textContent = 'Connected';
            }
        });
        steps.push({ element: '#dialerAiTimer',
            popover: { title: 'AI Minutes Timer', description: 'Shows the <strong>elapsed AI minutes</strong> for the current call. This counts against your AI minute balance in real time.', side: 'bottom' },
            onHighlightStarted: function() { showEl('dialerAiTimer', 'inline-block'); }
        });
        steps.push({ element: '#dialerListenBtn',
            popover: { title: 'Listen In', description: 'Open a <strong>live audio stream</strong> of the AI conversation. Hear both the AI agent and the lead in real time through your browser speakers — without the lead knowing.', side: 'bottom' }
        });
        steps.push({ element: '#dialerMuteBtn',
            popover: { title: 'Mute AI Audio', description: 'Mute the AI agent\'s voice output to the lead. The AI <strong>stops talking but keeps listening</strong>. Useful when you want the AI to pause before you intervene.', side: 'bottom' }
        });
        steps.push({ element: '#dialerMuteMicBtn',
            popover: { title: 'Mute Microphone', description: 'Mute your microphone during a <strong>listen-in session</strong>. Prevents any background noise on your end from bleeding into the call.', side: 'bottom' }
        });
        steps.push({ element: '#dialerTakeoverBtn',
            popover: { title: 'Intercept / Takeover', description: 'The <strong>big orange button</strong>. Instantly takes over the call from the AI — disconnects the AI agent and connects YOUR browser mic directly to the lead. Use when a lead is hot and you want to close personally.', side: 'bottom' }
        });
        steps.push({ element: '#dialerTransferBtn',
            popover: { title: 'Transfer Call', description: 'Transfer the active call to <strong>another phone number</strong> — a closer, a manager, or your cell. Enter the target number in the prompt that appears.', side: 'bottom' }
        });

        // Disposition
        steps.push({ element: '#dialerDisposition',
            popover: { title: 'Call Disposition', description: 'After each call ends, this panel appears so you can tag the outcome: <strong>Not Answered, Hung Up, Not Interested, Left Voicemail, or None</strong>. Dispositions help track your pipeline.', side: 'bottom' },
            onHighlightStarted: function() {
                showEl('dialerDisposition', 'flex');
            },
            onDeselected: function() {
                hideEl('dialerDisposition');
                hideEl('dialerCallBanner');
                hideEl('dialerAiTimer');
            }
        });

        // ============================================================
        // CHAPTER 5 — Power Dialer: History & Messaging
        // ============================================================
        chapterStarts.push(steps.length);
        steps.push(chapterStep(5, 'Power Dialer — History &amp; Messaging', 'The right column gives you full conversation context — SMS history, call records, recordings, and an integrated message composer.'));

        // Center column
        steps.push({ element: '#dlrDetailContent',
            popover: { title: 'Contact Detail Panel', description: 'When you select a contact, their full <strong>CRM profile</strong> appears here — name, phone, email, address, tags, custom fields, and notes. All pulled from your CRM.', side: 'left' },
            onHighlightStarted: function() { goTab('voicedialer'); }
        });
        steps.push({ element: '#dlrCallContactBtn',
            popover: { title: 'Call This Contact', description: 'One-click call button for the <strong>currently selected contact</strong>. No need to queue — just click and dial.', side: 'left' }
        });

        // Right column tabs
        steps.push({ element: '#dlrTabMessages',
            popover: { title: 'Messages Tab', description: 'View the full <strong>SMS conversation history</strong> with the selected contact — both inbound (their messages) and outbound (your bot or manual replies). Messages come from your CRM.', side: 'bottom' }
        });
        steps.push({ element: '#dlrAiDraftBtn',
            popover: { title: 'AI Draft Reply', description: 'Click to have the AI <strong>generate a contextual reply</strong> based on the full conversation. It drafts the message — you review, edit if needed, and send. Quick replies below offer one-tap templates.', side: 'bottom' }
        });
        steps.push({ element: '#dlrTabCalls',
            popover: { title: 'Calls Tab', description: 'View <strong>call history</strong> for the selected contact (or all contacts). Each entry shows status, duration, direction, and disposition. Switch between contact-specific and "View All" mode.', side: 'bottom' }
        });
        steps.push({ element: '#dlrTabRecordings',
            popover: { title: 'Recordings Tab', description: 'Browse <strong>call recordings</strong>. Each recording shows the contact, date, and duration. Three actions available per recording:', side: 'bottom' }
        });
        // Explain play/download/transcript
        steps.push({
            popover: { title: 'Play, Download & Transcripts', description: '<strong>Play</strong> — Listen to the recording directly in your browser.<br><strong>Download</strong> — Save the audio file to your computer.<br><strong>Transcript</strong> — View the full AI-generated conversation transcript with lead and agent turns labeled.<br><br>All accessible via icons on each recording row.', popoverClass: 'igb-tut-chapter' }
        });

        // ============================================================
        // CHAPTER 6 — SMS Bot Configuration
        // ============================================================
        chapterStarts.push(steps.length);
        steps.push(chapterStep(6, 'SMS Bot Configuration', 'Configure how your AI-powered SMS bot talks to leads. These settings control the bot\'s identity and booking behavior.'));

        steps.push({ element: '#config',
            popover: { title: 'SMS Config Panel', description: 'This is where you configure the <strong>core parameters</strong> for your SMS bot — the identity it uses, the calendar it books into, and the initial outreach message.', side: 'top' },
            onHighlightStarted: function() { goTab('config'); }
        });
        steps.push({ element: '#calendar_select',
            popover: { title: 'Calendar Selection', description: 'Click <strong>"Load Calendars"</strong> to pull your CRM calendars, then select the one the bot should book appointments into. This is critical for the booking flow to work.', side: 'right' }
        });
        steps.push({ element: '#bot_name',
            popover: { title: 'Bot Name', description: 'The name the SMS bot introduces itself as. Example: "Sarah" or "Alex". This appears in outbound messages — <em>"Hi! This is Sarah from..."</em>', side: 'right' }
        });
        steps.push({ element: '#initial_message',
            popover: { title: 'Initial Outreach Message', description: 'The <strong>first SMS</strong> the bot sends to new leads. Make it warm, personal, and action-oriented. The bot sends this when a new lead webhook fires.', side: 'right' }
        });
        steps.push({ element: '#save-config-btn',
            popover: { title: 'Save Configuration', description: 'Always click <strong>Save</strong> after making changes. Your config is stored securely and takes effect immediately for all new conversations.', side: 'top' }
        });

        // ============================================================
        // CHAPTER 7 — Voice AI Configuration
        // ============================================================
        chapterStarts.push(steps.length);
        steps.push(chapterStep(7, 'Voice AI Agent', 'Configure the voice AI that handles phone calls. Choose the voice, write the script, set behavior rules, and activate your phone number.'));

        steps.push({ element: '#voiceStatusBadge',
            popover: { title: 'Voice AI Status', description: 'Shows whether Voice AI is currently <strong>enabled</strong> (green) or <strong>disabled</strong> (red). Toggle with the switch to the right.', side: 'right' },
            onHighlightStarted: function() { goTab('voice'); }
        });
        steps.push({ element: '#voiceEnabled',
            popover: { title: 'Enable / Disable Voice', description: 'Master switch for the voice AI agent. When <strong>off</strong>, all outbound and inbound voice AI features are disabled. Turn it on to start using the dialer\'s AI mode.', side: 'right' }
        });
        steps.push({ element: '#voiceSelection',
            popover: { title: 'AI Voice Selection', description: 'Choose from <strong>7 AI voices</strong>: Ara, Eve, Leo, Rex, Sal, Mika, and Vale. Each has a unique tone — from warm and friendly to confident and authoritative.', side: 'right' }
        });
        steps.push({ element: '#voicePreviewBtn',
            popover: { title: 'Preview Voice', description: 'Click to hear a <strong>sample of the selected voice</strong>. Test different voices to find the one that best matches your agency\'s brand.', side: 'right' }
        });
        steps.push({ element: '#voiceBotName',
            popover: { title: 'Voice Agent Name', description: 'The name the AI introduces itself as on calls. Example: <em>"Hi, this is Sarah calling from ABC Insurance."</em>', side: 'right' }
        });
        steps.push({ element: '#voiceCallScript',
            popover: { title: 'Call Script', description: 'The <strong>reference script</strong> your AI agent follows. Write your ideal call flow — greeting, qualification questions, objection handling, and booking close. The AI adapts it naturally while staying on-script.', side: 'right' }
        });
        steps.push({ element: '#voiceInstructions',
            popover: { title: 'Behavior Instructions', description: 'Custom <strong>behavioral rules</strong> for the voice AI. Example: "Never offer quotes over the phone" or "Always mention our 30-year track record." These instructions shape how the AI handles conversations.', side: 'right' }
        });

        // Voice sub-tabs
        steps.push({ element: '#vmenu-activation',
            popover: { title: 'Activate Voice', description: 'This panel provisions your <strong>Twilio sub-account, phone number, and voice app</strong>. Click "Activate Voice" once to set everything up. After activation, your dialer is ready to make calls.', side: 'right' }
        });
        steps.push({ element: '#vmenu-numbers',
            popover: { title: 'Phone Numbers', description: 'Manage your voice phone numbers. <strong>Buy additional numbers</strong> (local, toll-free, or mobile), view your active numbers, enable CNAM caller ID, and release numbers you no longer need.', side: 'right' }
        });
        steps.push({ element: '#vmenu-trusthub',
            popover: { title: 'Spam Protection', description: 'Register your business with Twilio\'s Trust Hub to <strong>reduce spam flagging</strong>. Enter your business name, EIN, and address. Protected numbers show your real business name on caller ID.', side: 'right' }
        });

        // ============================================================
        // CHAPTER 8 — Workflows & CRM Connection
        // ============================================================
        chapterStarts.push(steps.length);
        steps.push(chapterStep(8, 'Workflows &amp; CRM Connection', 'Set up automation workflows and connect your CRM to power the entire system.'));

        steps.push({ element: '#wf-reengage',
            popover: { title: 'Re-Engage Workflow', description: 'A step-by-step guide for creating a <strong>re-engagement loop</strong> in your CRM. Tags trigger the bot, which follows up automatically until the lead books or opts out.', side: 'top' },
            onHighlightStarted: function() { goTab('workflows'); }
        });

        // CRM connection
        steps.push({ element: '#webhookUrl',
            popover: { title: 'Webhook URL', description: 'Copy this URL and paste it into your CRM\'s webhook settings. This is how new leads and SMS replies reach InsuranceGrokBot. <strong>Without this, the bot can\'t receive events.</strong>', side: 'top' },
            onHighlightStarted: function() { goTab('connect'); }
        });
        steps.push({ element: '[data-crm="ghl"]',
            popover: { title: 'CRM Selection', description: 'Select your CRM provider. <strong>GoHighLevel/LeadConnector</strong> is the primary integration with full OAuth support. Other CRMs (HubSpot, Salesforce, Pipedrive, Zoho, Zapier, Insureio) use API key or webhook integration.', side: 'bottom' }
        });
        steps.push({ element: '#apiKeySection',
            popover: { title: 'External API Keys', description: 'Generate an <strong>API key</strong> for external integrations. This enables the OpenAI-compatible chat endpoint at <code>/api/v1/chat/completions</code> — useful for custom automations and third-party tools.', side: 'top' }
        });

        // ============================================================
        // CHAPTER 9 — Carriers & Advanced Settings
        // ============================================================
        chapterStarts.push(steps.length);
        steps.push(chapterStep(9, 'Carriers &amp; Advanced Settings', 'Tell the bot which carriers you work with, and fine-tune its personality, behavior, and after-hours rules.'));

        steps.push({ element: '#carrierGrid',
            popover: { title: 'Carrier Selection Grid', description: 'Select every <strong>insurance carrier you\'re contracted with</strong>. Click chips to toggle. The AI only mentions carriers you select — preventing it from referencing companies you don\'t represent.', side: 'top' },
            onHighlightStarted: function() { goTab('carriers'); }
        });
        steps.push({ element: '#saveCarriersBtn',
            popover: { title: 'Save Carriers', description: 'Click Save after selecting your carriers. Changes take effect immediately for all new AI conversations.', side: 'top' }
        });

        // Advanced settings
        steps.push({ element: '#professionalism_level',
            popover: { title: 'Professionalism Slider', description: 'Slide from <strong>Casual</strong> (friendly, emoji-heavy) to <strong>Ultra Professional</strong> (formal, corporate tone). Most insurance agents do well at level 3–4.', side: 'right' },
            onHighlightStarted: function() { goTab('advanced'); }
        });
        steps.push({ element: '#humor_enabled',
            popover: { title: 'Behavior Toggles', description: 'Toggle individual behaviors: <strong>Humor Mode</strong> (light jokes), <strong>Lead Re-engagement</strong> (follow-up loops), <strong>Booking Confirmation</strong> (confirm after booking), <strong>Speed to Lead</strong> (instant response), <strong>Conversation Memory</strong> (remember past interactions), and <strong>Multi-Language Detection</strong>.', side: 'right' }
        });
        steps.push({ element: '#after_hours_enabled',
            popover: { title: 'After Hours Mode', description: 'Set <strong>operating hours</strong> for the bot. When enabled, the AI adjusts its behavior outside business hours — offering to schedule a callback instead of transferring.', side: 'right' }
        });
        steps.push({ element: '#custom_behavior',
            popover: { title: 'Custom Behavior Instructions', description: 'Free-form text area for <strong>any additional rules</strong> you want the AI to follow. Examples: "Always ask about their family situation", "Never discuss competitor rates", "Mention our 5-star Google rating."', side: 'right' }
        });
        steps.push({ element: '#saveAdvancedBtn',
            popover: { title: 'Save Advanced Settings', description: 'Save all personality and behavior changes. These apply to both SMS and voice AI conversations.', side: 'top' }
        });

        // ============================================================
        // CHAPTER 10 — AI Minutes, Billing & Logs
        // ============================================================
        chapterStarts.push(steps.length);
        steps.push(chapterStep(10, 'AI Minutes, Billing &amp; Logs', 'Monitor your usage, manage your subscription, and debug with detailed activity logs.'));

        steps.push({ element: '#aimBalanceBanner',
            popover: { title: 'AI Minutes Balance', description: 'Your current <strong>AI voice minute balance</strong>. Each AI phone call deducts minutes in real time. When low, purchase additional minute packages below.', side: 'top' },
            onHighlightStarted: function() { goTab('aiminutes'); }
        });
        steps.push({ element: '#aimPackages',
            popover: { title: 'Purchase Packages', description: 'Buy AI minute bundles. Larger packages offer <strong>better per-minute pricing</strong>. Minutes never expire and stack on top of your existing balance.', side: 'top' }
        });

        // Logs
        steps.push({ element: '#logFilterType',
            popover: { title: 'Activity Log Filters', description: 'Filter logs by <strong>event type</strong> (webhook received, booking attempt, message sent, errors) and <strong>status</strong> (success, error, warning). The refresh button reloads the latest entries.', side: 'bottom' },
            onHighlightStarted: function() { goTab('logs'); }
        });
        steps.push({ element: '#logsContainer',
            popover: { title: 'Log Entries', description: 'Each row shows a timestamped event with a <strong>status badge</strong> (green/red/orange/blue), event type, contact info, and summary. Click any row to see full details. Great for debugging webhook issues.', side: 'top' }
        });

        // ============================================================
        // CHAPTER 11 — Discord Integration
        // ============================================================
        chapterStarts.push(steps.length);
        steps.push(chapterStep(11, 'Discord Team Chat', 'Stay connected with your team without leaving the dashboard. Read and reply to Discord messages right from the sidebar.'));

        steps.push({ element: '#sbSectionDiscord',
            popover: { title: 'Discord Section', description: 'Connect your Discord account, add up to <strong>3 servers</strong>, invite the bot, and browse channels directly in the sidebar. Click any channel to open the chat panel.', side: 'right' },
            onHighlightStarted: function() {
                goTab('voicedialer');
                ensureOpen('sbSectionDiscord');
            }
        });
        steps.push({ element: '#discordChatToggleBtn',
            popover: { title: 'Discord Chat Toggle', description: 'Opens the <strong>Discord chat panel</strong> that slides out from the sidebar. View messages, reply, send new messages — all without switching apps. The badge shows unread count.', side: 'right' }
        });

        // ============================================================
        // FINISH
        // ============================================================
        chapterStarts.push(steps.length); // virtual "end" chapter
        steps.push({
            popover: {
                title: '<span class="igb-ch-badge">Tutorial Complete</span><br>You\'re All Set!',
                description: 'You\'ve seen every feature in your dashboard. Here\'s the recommended <strong>setup order</strong>:<br><br><strong>1.</strong> Connect CRM &rarr; <strong>2.</strong> Set Carriers &rarr; <strong>3.</strong> Configure SMS Bot &rarr; <strong>4.</strong> Configure Voice AI &rarr; <strong>5.</strong> Activate Voice &rarr; <strong>6.</strong> Start Dialing!<br><br>To replay this tutorial anytime, click <strong>"Tutorial"</strong> in the sidebar footer.<br><br>Welcome to InsuranceGrokBot.',
                popoverClass: 'igb-tut-finish'
            }
        });

        // ── Post-process: add "Skip chapter" buttons to chapter markers ──
        for (var i = 0; i < chapterStarts.length - 1; i++) {
            var idx = chapterStarts[i];
            var nextIdx = chapterStarts[i + 1];
            var marker = steps[idx];
            if (marker.popover && !marker.element) {
                marker.popover.description += '<br><button class="igb-tut-skip" onclick="window._igbSkipTo(' + nextIdx + ')">Skip this chapter &rarr;</button>';
            }
        }

        return { steps: steps, chapterStarts: chapterStarts };
    }

    // ── Driver Instance ────────────────────────────────────────
    var _driverObj = null;

    function startTutorial() {
        injectStyles();

        // Ensure driver.js is loaded
        if (!window.driver || !window.driver.js || !window.driver.js.driver) {
            console.warn('[Tutorial] driver.js not loaded — cannot start tutorial');
            return;
        }

        // Destroy any existing instance
        if (_driverObj && _driverObj.isActive()) {
            _driverObj.destroy();
        }

        var built = buildSteps();
        var steps = built.steps;

        // Skip-to-chapter global function
        window._igbSkipTo = function(idx) {
            if (_driverObj) _driverObj.moveTo(idx);
        };

        _driverObj = window.driver.js.driver({
            steps: steps,
            showProgress: true,
            showButtons: ['next', 'previous', 'close'],
            allowClose: true,
            overlayColor: '#000',
            overlayOpacity: 0.72,
            stagePadding: 8,
            stageRadius: 10,
            popoverOffset: 12,
            animate: true,
            smoothScroll: true,
            allowKeyboardControl: true,
            doneBtnText: 'Finish',
            nextBtnText: 'Next',
            prevBtnText: 'Back',
            progressText: '{{current}} / {{total}}',
            onDestroyStarted: function() {
                markTutorialDone();
                // Clean up any UI elements we showed for demo purposes
                hideEl('dialerCallBanner');
                hideEl('dialerDisposition');
                hideEl('dialerAiTimer');
                if (_driverObj) _driverObj.destroy();
            }
        });

        // Navigate to dialer tab to start
        goTab('voicedialer');

        // Small delay to ensure tab is visible before first step
        setTimeout(function() {
            _driverObj.drive();
        }, 150);
    }

    // ── First-Login Auto-Trigger ───────────────────────────────
    function checkFirstLogin() {
        if (!isTutorialDone()) {
            // Wait for driver.js to be loaded (it's async from CDN)
            if (window.driver && window.driver.js && window.driver.js.driver) {
                startTutorial();
            } else {
                // Retry a few times
                var attempts = 0;
                var iv = setInterval(function() {
                    attempts++;
                    if (window.driver && window.driver.js && window.driver.js.driver) {
                        clearInterval(iv);
                        startTutorial();
                    } else if (attempts > 20) {
                        clearInterval(iv);
                        console.warn('[Tutorial] driver.js failed to load after 10s');
                    }
                }, 500);
            }
        }
    }

    // ── Public API ─────────────────────────────────────────────
    window.startDashboardTutorial = startTutorial;

    // ── Auto-check on page load ────────────────────────────────
    if (document.readyState === 'complete' || document.readyState === 'interactive') {
        setTimeout(checkFirstLogin, 1500);
    } else {
        document.addEventListener('DOMContentLoaded', function() {
            setTimeout(checkFirstLogin, 1500);
        });
    }

})();
