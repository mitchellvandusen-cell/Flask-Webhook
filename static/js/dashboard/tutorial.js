// ================================================================
// InsuranceGrokBot — Dashboard Interactive Tutorial (driver.js)
// Streamlined menu-focused walkthrough with Liquid Glass UI
// ================================================================
(function () {
    'use strict';

    var STORAGE_PREFIX = 'igb_tutorial_v2_';
    var TOTAL_CHAPTERS = 7;

    // ── CSS Injection (Liquid Glass — Dark + Light theme) ────────
    function injectStyles() {
        if (document.getElementById('igb-tutorial-css')) return;
        var s = document.createElement('style');
        s.id = 'igb-tutorial-css';
        s.textContent = [

            /* ── Keyframes ───────────────────────────────────── */
            '@keyframes igbTutIn {',
            '  from { opacity:0; transform:scale(0.97) translateY(6px); }',
            '  to   { opacity:1; transform:scale(1) translateY(0); }',
            '}',
            '@keyframes igbGlow {',
            '  0%,100% { box-shadow: 0 0 0 2px rgba(0,255,136,0.40), 0 0 16px rgba(0,255,136,0.12); }',
            '  50%     { box-shadow: 0 0 0 3px rgba(0,255,136,0.25), 0 0 28px rgba(0,255,136,0.08); }',
            '}',

            /* ── Overlay ─────────────────────────────────────── */
            '.driver-overlay { background: rgba(0,0,0,0) !important; }',

            /* ═══════════════════════════════════════════════════
               DARK THEME — Liquid Glass Popover
               Frosted glass with asymmetric edge lighting,
               multi-layer depth shadows, refraction gradients
            ═══════════════════════════════════════════════════ */
            '.driver-popover {',
            '  background:',
            '    radial-gradient(ellipse at 15% -10%, rgba(0,217,255,0.06) 0%, transparent 50%),',
            '    radial-gradient(ellipse at 85% 110%, rgba(0,255,136,0.03) 0%, transparent 50%),',
            '    linear-gradient(168deg, rgba(18,18,44,0.90) 0%, rgba(10,10,28,0.85) 50%, rgba(14,10,34,0.88) 100%) !important;',
            '  backdrop-filter: blur(36px) saturate(180%) brightness(1.05) !important;',
            '  -webkit-backdrop-filter: blur(36px) saturate(180%) brightness(1.05) !important;',
            '  border: 1px solid rgba(255,255,255,0.07) !important;',
            '  border-top-color: rgba(255,255,255,0.14) !important;',
            '  border-left-color: rgba(255,255,255,0.10) !important;',
            '  border-radius: 16px !important;',
            '  box-shadow:',
            '    0 0 0 0.5px rgba(255,255,255,0.04),',
            '    0 4px 12px rgba(0,0,0,0.30),',
            '    0 12px 40px rgba(0,0,0,0.35),',
            '    inset 0 1px 0 rgba(255,255,255,0.12),',
            '    inset 0 0 30px rgba(0,255,136,0.01) !important;',
            '  padding: 20px 22px 16px !important;',
            '  min-height: unset !important;',
            '  max-width: 380px !important;',
            '  font-family: "Outfit", sans-serif !important;',
            '  animation: igbTutIn 0.3s cubic-bezier(0.22,1,0.36,1) !important;',
            '}',

            /* Top-edge refraction streak */
            '.driver-popover::before {',
            '  content: "" !important;',
            '  position: absolute !important;',
            '  top: 0 !important; left: 16px !important; right: 16px !important;',
            '  height: 1px !important;',
            '  background: linear-gradient(90deg, transparent, rgba(255,255,255,0.12) 20%, rgba(255,255,255,0.30) 50%, rgba(255,255,255,0.12) 80%, transparent) !important;',
            '  pointer-events: none !important; z-index: 1 !important;',
            '}',

            /* Title */
            '.driver-popover-title {',
            '  color: #f0f0f8 !important;',
            '  font-weight: 700 !important;',
            '  font-size: 0.95rem !important;',
            '  line-height: 1.35 !important;',
            '  margin: 0 0 6px !important;',
            '  padding: 0 !important;',
            '  letter-spacing: -0.01em !important;',
            '}',

            /* Description */
            '.driver-popover-description {',
            '  color: rgba(215,215,235,0.85) !important;',
            '  font-size: 0.82rem !important;',
            '  line-height: 1.6 !important;',
            '  margin: 0 !important; padding: 0 !important;',
            '  font-weight: 400 !important;',
            '}',
            '.driver-popover-description strong { color: #fff !important; font-weight: 600 !important; }',
            '.driver-popover-description em { color: #8edfc0 !important; font-style: normal !important; }',
            '.driver-popover-description code {',
            '  background: rgba(0,255,136,0.08); border: 1px solid rgba(0,255,136,0.12);',
            '  color: #00e87a; border-radius: 4px; padding: 1px 6px;',
            '  font-size: 0.75rem; font-family: "JetBrains Mono", monospace;',
            '}',
            '.driver-popover-description p { margin: 0 0 4px !important; }',

            /* Footer */
            '.driver-popover-footer {',
            '  display: flex !important; align-items: center !important;',
            '  margin-top: 14px !important; padding-top: 10px !important; padding-bottom: 0 !important;',
            '  border-top: 1px solid rgba(255,255,255,0.06) !important;',
            '  gap: 8px !important;',
            '}',

            /* Progress text */
            '.driver-popover-progress-text {',
            '  color: rgba(255,255,255,0.20) !important;',
            '  font-size: 0.72rem !important;',
            '  font-family: "JetBrains Mono", monospace !important;',
            '  flex: 1 !important;',
            '}',

            '.driver-popover-navigation-btns { gap: 8px !important; flex-shrink: 0 !important; }',

            /* Next / Done — green pill */
            '.driver-popover-next-btn {',
            '  background: linear-gradient(135deg, #00ff88 0%, #00d96e 100%) !important;',
            '  color: #041a0e !important;',
            '  border: none !important; border-radius: 50px !important;',
            '  font-weight: 700 !important; font-size: 0.76rem !important;',
            '  padding: 7px 20px !important;',
            '  text-shadow: 0 1px 0 rgba(255,255,255,0.12) !important;',
            '  box-shadow: 0 1px 3px rgba(0,0,0,0.2), 0 4px 14px rgba(0,255,136,0.22), inset 0 1px 0 rgba(255,255,255,0.20) !important;',
            '  transition: all 0.2s cubic-bezier(0.22,1,0.36,1) !important;',
            '  cursor: pointer !important;',
            '}',
            '.driver-popover-next-btn:hover {',
            '  transform: translateY(-1px) !important;',
            '  box-shadow: 0 2px 6px rgba(0,0,0,0.25), 0 8px 24px rgba(0,255,136,0.30), inset 0 1px 0 rgba(255,255,255,0.25) !important;',
            '}',
            '.driver-popover-next-btn:active { transform: translateY(0) !important; }',

            /* Back — ghost pill */
            '.driver-popover-prev-btn {',
            '  background: rgba(255,255,255,0.04) !important;',
            '  color: rgba(255,255,255,0.45) !important;',
            '  border: 1px solid rgba(255,255,255,0.08) !important;',
            '  border-radius: 50px !important;',
            '  font-weight: 500 !important; font-size: 0.76rem !important;',
            '  padding: 6px 16px !important; cursor: pointer !important;',
            '  transition: all 0.2s cubic-bezier(0.22,1,0.36,1) !important;',
            '  backdrop-filter: blur(8px) !important;',
            '}',
            '.driver-popover-prev-btn:hover {',
            '  background: rgba(255,255,255,0.08) !important;',
            '  color: rgba(255,255,255,0.80) !important;',
            '  border-color: rgba(255,255,255,0.14) !important;',
            '  transform: translateY(-1px) !important;',
            '}',

            /* Close button */
            '.driver-popover-close-btn {',
            '  color: rgba(255,255,255,0.22) !important;',
            '  font-size: 1rem !important;',
            '  top: 14px !important; right: 16px !important;',
            '  transition: all 0.2s !important;',
            '  width: 24px !important; height: 24px !important;',
            '  display: flex !important; align-items: center !important; justify-content: center !important;',
            '  border-radius: 50% !important;',
            '}',
            '.driver-popover-close-btn:hover {',
            '  color: rgba(255,255,255,0.75) !important;',
            '  background: rgba(255,255,255,0.06) !important;',
            '}',

            /* Arrows — match glass tint */
            '.driver-popover-arrow-side-left.driver-popover-arrow  { border-right-color:  rgba(16,16,38,0.92) !important; }',
            '.driver-popover-arrow-side-right.driver-popover-arrow { border-left-color:   rgba(16,16,38,0.92) !important; }',
            '.driver-popover-arrow-side-top.driver-popover-arrow   { border-bottom-color: rgba(16,16,38,0.92) !important; }',
            '.driver-popover-arrow-side-bottom.driver-popover-arrow{ border-top-color:    rgba(16,16,38,0.92) !important; }',

            /* Element highlight glow */
            '.driver-active-element, .driver-highlighted-element {',
            '  outline: 2px solid rgba(0,255,136,0.50) !important;',
            '  outline-offset: 3px !important;',
            '  border-radius: 8px !important;',
            '  animation: igbGlow 2.5s ease-in-out infinite !important;',
            '}',

            /* Chapter marker — centered, cyan tint */
            '.igb-tut-chapter { max-width: 420px !important; text-align: center !important; }',
            '.igb-tut-chapter .driver-popover-title { font-size: 1.05rem !important; }',
            '.igb-tut-chapter .driver-popover-description { color: rgba(200,210,230,0.80) !important; }',

            /* Finish — green celebration */
            '.igb-tut-finish { max-width: 440px !important; text-align: center !important; }',
            '.igb-tut-finish .driver-popover-title { font-size: 1.1rem !important; color: #00ff88 !important; }',

            /* Chapter badge pill */
            '.igb-ch-badge {',
            '  display: inline-block; text-transform: uppercase; letter-spacing: 2.5px;',
            '  color: #00d9ff; font-size: 0.62rem; font-weight: 700;',
            '  background: rgba(0,217,255,0.06); border: 1px solid rgba(0,217,255,0.15);',
            '  padding: 2px 10px; border-radius: 16px; margin-bottom: 2px;',
            '}',

            /* Skip chapter button */
            '.igb-tut-skip {',
            '  display: inline-block; margin-top: 10px;',
            '  background: rgba(255,255,255,0.03); color: rgba(255,255,255,0.30);',
            '  font: 500 0.72rem "Outfit", sans-serif;',
            '  border: 1px solid rgba(255,255,255,0.07); border-radius: 50px;',
            '  padding: 4px 14px; cursor: pointer;',
            '  transition: all 0.2s; backdrop-filter: blur(8px);',
            '}',
            '.igb-tut-skip:hover {',
            '  color: #00d9ff; border-color: rgba(0,217,255,0.25);',
            '  background: rgba(0,217,255,0.06); transform: translateY(-1px);',
            '}',

            /* Keyboard hint */
            '.igb-key-hint { display: block; margin-top: 8px; font-size: 0.72rem; color: rgba(255,255,255,0.22); }',
            '.igb-key-hint kbd {',
            '  background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.08);',
            '  border-radius: 3px; padding: 1px 5px;',
            '  font: 0.72rem "JetBrains Mono", monospace; color: rgba(255,255,255,0.40);',
            '}',

            /* ═══════════════════════════════════════════════════
               LIGHT THEME — Liquid Glass overrides
               Bright frosted surface with subtle shadows
            ═══════════════════════════════════════════════════ */
            'body.light-theme .driver-popover {',
            '  background:',
            '    radial-gradient(ellipse at 15% -10%, rgba(0,120,180,0.04) 0%, transparent 50%),',
            '    radial-gradient(ellipse at 85% 110%, rgba(0,170,94,0.03) 0%, transparent 50%),',
            '    linear-gradient(168deg, rgba(255,255,255,0.92) 0%, rgba(248,249,252,0.88) 50%, rgba(255,255,255,0.90) 100%) !important;',
            '  backdrop-filter: blur(36px) saturate(140%) brightness(1.02) !important;',
            '  -webkit-backdrop-filter: blur(36px) saturate(140%) brightness(1.02) !important;',
            '  border-color: rgba(0,0,0,0.08) !important;',
            '  border-top-color: rgba(255,255,255,0.90) !important;',
            '  border-left-color: rgba(255,255,255,0.60) !important;',
            '  box-shadow:',
            '    0 0 0 0.5px rgba(0,0,0,0.04),',
            '    0 4px 12px rgba(0,0,0,0.08),',
            '    0 12px 40px rgba(0,0,0,0.10),',
            '    inset 0 1px 0 rgba(255,255,255,0.95),',
            '    inset 0 0 30px rgba(0,170,94,0.02) !important;',
            '}',

            /* Light — top streak */
            'body.light-theme .driver-popover::before {',
            '  background: linear-gradient(90deg, transparent, rgba(255,255,255,0.60) 20%, rgba(255,255,255,0.90) 50%, rgba(255,255,255,0.60) 80%, transparent) !important;',
            '}',

            /* Light — title */
            'body.light-theme .driver-popover-title { color: #0d0d12 !important; }',

            /* Light — description */
            'body.light-theme .driver-popover-description { color: #374151 !important; }',
            'body.light-theme .driver-popover-description strong { color: #111827 !important; }',
            'body.light-theme .driver-popover-description em { color: #047857 !important; }',
            'body.light-theme .driver-popover-description code {',
            '  background: rgba(0,170,94,0.08); border-color: rgba(0,170,94,0.15);',
            '  color: #047857;',
            '}',

            /* Light — footer */
            'body.light-theme .driver-popover-footer { border-top-color: rgba(0,0,0,0.06) !important; }',

            /* Light — progress text */
            'body.light-theme .driver-popover-progress-text { color: rgba(0,0,0,0.25) !important; }',

            /* Light — next button */
            'body.light-theme .driver-popover-next-btn {',
            '  background: linear-gradient(135deg, #00aa5e 0%, #009150 100%) !important;',
            '  color: #fff !important;',
            '  text-shadow: 0 1px 0 rgba(0,0,0,0.10) !important;',
            '  box-shadow: 0 1px 3px rgba(0,0,0,0.10), 0 4px 14px rgba(0,170,94,0.20), inset 0 1px 0 rgba(255,255,255,0.15) !important;',
            '}',
            'body.light-theme .driver-popover-next-btn:hover {',
            '  box-shadow: 0 2px 6px rgba(0,0,0,0.12), 0 8px 24px rgba(0,170,94,0.28), inset 0 1px 0 rgba(255,255,255,0.20) !important;',
            '}',

            /* Light — back button */
            'body.light-theme .driver-popover-prev-btn {',
            '  background: rgba(0,0,0,0.03) !important;',
            '  color: #6b7280 !important;',
            '  border-color: rgba(0,0,0,0.08) !important;',
            '  backdrop-filter: blur(8px) !important;',
            '}',
            'body.light-theme .driver-popover-prev-btn:hover {',
            '  background: rgba(0,0,0,0.06) !important;',
            '  color: #374151 !important;',
            '  border-color: rgba(0,0,0,0.12) !important;',
            '}',

            /* Light — close button */
            'body.light-theme .driver-popover-close-btn { color: rgba(0,0,0,0.25) !important; }',
            'body.light-theme .driver-popover-close-btn:hover { color: rgba(0,0,0,0.65) !important; background: rgba(0,0,0,0.04) !important; }',

            /* Light — arrows */
            'body.light-theme .driver-popover-arrow-side-left.driver-popover-arrow  { border-right-color:  rgba(252,252,255,0.94) !important; }',
            'body.light-theme .driver-popover-arrow-side-right.driver-popover-arrow { border-left-color:   rgba(252,252,255,0.94) !important; }',
            'body.light-theme .driver-popover-arrow-side-top.driver-popover-arrow   { border-bottom-color: rgba(252,252,255,0.94) !important; }',
            'body.light-theme .driver-popover-arrow-side-bottom.driver-popover-arrow{ border-top-color:    rgba(252,252,255,0.94) !important; }',

            /* Light — highlight glow */
            'body.light-theme .driver-active-element, body.light-theme .driver-highlighted-element {',
            '  outline-color: rgba(0,170,94,0.50) !important;',
            '}',

            /* Light — chapter badge */
            'body.light-theme .igb-ch-badge { color: #0078b8; background: rgba(0,120,184,0.06); border-color: rgba(0,120,184,0.15); }',

            /* Light — finish title */
            'body.light-theme .igb-tut-finish .driver-popover-title { color: #00aa5e !important; }',

            /* Light — skip button */
            'body.light-theme .igb-tut-skip {',
            '  background: rgba(0,0,0,0.02); color: #9ca3af; border-color: rgba(0,0,0,0.06);',
            '}',
            'body.light-theme .igb-tut-skip:hover {',
            '  color: #0078b8; border-color: rgba(0,120,184,0.20); background: rgba(0,120,184,0.04);',
            '}',

            /* Light — key hint */
            'body.light-theme .igb-key-hint { color: rgba(0,0,0,0.25); }',
            'body.light-theme .igb-key-hint kbd {',
            '  background: rgba(0,0,0,0.04); border-color: rgba(0,0,0,0.08); color: #6b7280;',
            '}',

        ].join('\n');
        document.head.appendChild(s);
    }

    // ── Persistence ──────────────────────────────────────────────
    function storageKey() {
        var boot = window.DASHBOARD_BOOT || {};
        return STORAGE_PREFIX + (boot.userEmail || boot.locationId || 'anon');
    }
    function isTutorialDone()  { try { return localStorage.getItem(storageKey()) === '1'; } catch(e) { return false; } }
    function markTutorialDone(){ try { localStorage.setItem(storageKey(), '1'); } catch(e) {} }

    // ── Navigation Helpers ───────────────────────────────────────
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
        logs:        'sbnLogs',
        team:        'sbnTeam'
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

    // ── Step Builder Helpers ─────────────────────────────────────
    function chapterStep(num, title, desc) {
        return {
            popover: {
                title: '<span class="igb-ch-badge">Chapter ' + num + ' of ' + TOTAL_CHAPTERS + '</span><br>' + title,
                description: desc,
                popoverClass: 'igb-tut-chapter'
            }
        };
    }

    // ── Build All Steps ──────────────────────────────────────────
    function buildSteps() {
        var steps = [];
        var chapterStarts = [];

        // ============================================================
        // CHAPTER 1 — Welcome & Navigation
        // ============================================================
        chapterStarts.push(steps.length);
        steps.push({
            popover: {
                title: '<span class="igb-ch-badge">Welcome</span><br>Welcome to InsuranceGrokBot',
                description: 'Your AI-powered insurance sales command center. This quick tour walks you through the <strong>main menu sections</strong> so you know where everything lives.<br><br>Takes about 2 minutes. Replay anytime from the sidebar.<span class="igb-key-hint">Use <kbd>\u2192</kbd> <kbd>\u2190</kbd> to navigate, <kbd>Esc</kbd> to close.</span>',
                popoverClass: 'igb-tut-chapter'
            }
        });

        // Sidebar overview
        steps.push({
            element: '#dashSidebar',
            popover: {
                title: 'Sidebar Navigation',
                description: 'This is your main navigation hub. Every tool and setting is organized into sections. Click any item to switch tabs. The sidebar collapses on smaller screens.',
                side: 'right', align: 'start'
            }
        });

        // Dialer tab
        steps.push({
            element: '#sbnDialer',
            popover: {
                title: 'Power Dialer',
                description: 'Your daily driver. Load contacts, build call queues, use <strong>AI Smart Filters</strong> to prioritize leads by temperature, and run auto-dial sessions with up to 4 concurrent lines.',
                side: 'right', align: 'start'
            },
            onHighlightStarted: function() { goTab('voicedialer'); }
        });

        // Theme toggle
        steps.push({
            element: '#themeToggleBtn',
            popover: {
                title: 'Theme Toggle',
                description: 'Switch between <strong>dark</strong> and <strong>light</strong> mode. Your preference is saved automatically.',
                side: 'bottom', align: 'center'
            }
        });

        // ============================================================
        // CHAPTER 2 — SMS Bot Configuration
        // ============================================================
        chapterStarts.push(steps.length);
        steps.push(chapterStep(2, 'SMS Bot Configuration', 'Configure how your AI-powered SMS bot talks to leads. Set the bot\'s identity, booking calendar, initial outreach message, and SMS delivery channel.'));

        steps.push({
            element: '#sbnSmsConfig',
            popover: {
                title: 'SMS Config Tab',
                description: 'Click here to access all SMS bot settings.',
                side: 'right', align: 'start'
            },
            onHighlightStarted: function() { goTab('config'); }
        });

        steps.push({
            element: '#configSettingsMenu',
            popover: {
                title: 'Settings Menu',
                description: 'Use these sub-menus to configure different aspects:<br><br><strong>Identity</strong> \u2014 Bot name and how it introduces itself<br><strong>Calendar</strong> \u2014 Select which calendar the bot books into<br><strong>Outreach</strong> \u2014 First message sent to new leads<br><strong>SMS Channel</strong> \u2014 Send via LeadConnector or your own IGB number',
                side: 'right', align: 'start'
            }
        });

        steps.push({
            element: '#main-config-form',
            popover: {
                title: 'Configuration Form',
                description: 'Fill in your settings and click <strong>Save Configuration</strong> at the bottom. Changes take effect immediately for new conversations.',
                side: 'left', align: 'start'
            }
        });

        // ============================================================
        // CHAPTER 3 — Voice AI Configuration
        // ============================================================
        chapterStarts.push(steps.length);
        steps.push(chapterStep(3, 'Voice AI Configuration', 'Set up your AI voice agent \u2014 choose a voice, write a call script, configure behavior, and manage phone numbers.'));

        steps.push({
            element: '#sbnVoiceConfig',
            popover: {
                title: 'Voice Config Tab',
                description: 'All voice AI settings live here.',
                side: 'right', align: 'start'
            },
            onHighlightStarted: function() { goTab('voice'); }
        });

        steps.push({
            element: '#voiceSettingsMenu',
            popover: {
                title: 'Voice Settings Menu',
                description: 'Navigate between voice sub-sections:<br><br><strong>AI Agent</strong> \u2014 Enable/disable, pick a voice, set the script and behavior rules<br><strong>Activate Voice</strong> \u2014 Provision your voice account and phone number<br><strong>Phone Numbers</strong> \u2014 Buy numbers, manage health scores, set up geo-routing<br><strong>Spam Monitoring</strong> \u2014 CNAM, Voice Integrity, number protection<br><strong>10DLC Registration</strong> \u2014 A2P compliance for SMS delivery',
                side: 'right', align: 'start'
            }
        });

        // ============================================================
        // CHAPTER 4 — Spam Monitoring & Number Health
        // ============================================================
        chapterStarts.push(steps.length);
        steps.push(chapterStep(4, 'Spam Monitoring &amp; Number Health', 'Protect your phone numbers from spam flags. Register with carrier databases, monitor health scores, and rotate numbers automatically.'));

        // Navigate to voice tab spam sub-section
        steps.push({
            element: '#sbnVoiceConfig',
            popover: {
                title: 'Spam Protection',
                description: 'Spam monitoring lives under <strong>Voice Config \u2192 Spam Monitoring</strong>. It includes:<br><br><strong>CNAM Registration</strong> \u2014 Display your business name on caller ID<br><strong>Voice Integrity</strong> \u2014 Register with AT&T/Hiya, T-Mobile, Verizon to clear spam labels<br><strong>Number Health</strong> \u2014 Track spam scores, auto-rotate flagged numbers<br><strong>Smart Rotation</strong> \u2014 Rest flagged numbers and rotate to clean ones automatically',
                side: 'right', align: 'start'
            },
            onHighlightStarted: function() {
                goTab('voice');
                // Try to switch to spam sub-panel
                if (typeof switchVoicePanel === 'function') {
                    setTimeout(function() { switchVoicePanel('spam'); }, 200);
                }
            }
        });

        steps.push({
            element: '#sbnVoiceConfig',
            popover: {
                title: 'A2P 10DLC Registration',
                description: 'Also under Voice Config \u2192 <strong>10DLC Registration</strong>. Required for SMS delivery:<br><br><strong>Register New</strong> \u2014 Submit your brand and campaign for carrier approval<br><strong>Import Existing</strong> \u2014 Bring in an already-approved brand/campaign from GHL or another provider<br><br>Without A2P registration, carriers may filter your SMS messages.',
                side: 'right', align: 'start'
            }
        });

        // ============================================================
        // CHAPTER 5 — Workflows
        // ============================================================
        chapterStarts.push(steps.length);
        steps.push(chapterStep(5, 'Automation Workflows', 'Build automated sequences that respond to events like new leads, missed calls, or cold lead re-engagement. Drag-and-drop visual builder with AI assistance.'));

        steps.push({
            element: '#sbnWorkflows',
            popover: {
                title: 'Workflows Tab',
                description: 'Your automation command center.',
                side: 'right', align: 'start'
            },
            onHighlightStarted: function() { goTab('workflows'); }
        });

        steps.push({
            element: '#sbnWorkflows',
            popover: {
                title: 'Pre-Built Templates',
                description: 'You start with <strong>4 ready-made workflows</strong> (all in draft mode):<br><br><strong>Speed to Lead</strong> \u2014 Instant SMS + AI call on new contacts<br><strong>Aged Lead Re-engagement</strong> \u2014 Warm check-in for 30+ day old leads<br><strong>SMS Response Handler</strong> \u2014 Smart routing based on lead temperature<br><strong>Re-engage Cold Leads</strong> \u2014 7-day no-response follow-up sequence<br><br>Open any workflow to customize, then <strong>activate</strong> when ready.',
                side: 'right', align: 'start'
            }
        });

        steps.push({
            element: '#sbnWorkflows',
            popover: {
                title: 'Build with AI',
                description: 'Click <strong>Build with AI</strong> to describe what you want in plain English. The AI generates a complete workflow with triggers, steps, and conditions. You can also use preset templates like <em>Speed to Lead</em> or <em>Missed Call Recovery</em>.',
                side: 'right', align: 'start'
            }
        });

        // ============================================================
        // CHAPTER 6 — Advanced Settings
        // ============================================================
        chapterStarts.push(steps.length);
        steps.push(chapterStep(6, 'Advanced Settings &amp; More', 'Fine-tune bot behavior, select carriers, connect your CRM, manage your team, and configure billing.'));

        steps.push({
            element: '#sbnCarriers',
            popover: {
                title: 'Carriers',
                description: 'Select which insurance carriers you\'re contracted with. The AI bot will <strong>only reference carriers you\'ve selected</strong> when talking to leads. 63 carriers available.',
                side: 'right', align: 'start'
            },
            onHighlightStarted: function() { goTab('carriers'); }
        });

        steps.push({
            element: '#sbnAdvanced',
            popover: {
                title: 'Advanced Settings',
                description: 'Fine-tune the AI\'s personality and behavior:<br><br><strong>Professionalism</strong> \u2014 Casual to ultra-professional tone<br><strong>Response length</strong> \u2014 Short, balanced, or detailed<br><strong>Speed to lead</strong> \u2014 Instant reply on new contacts<br><strong>After hours mode</strong> \u2014 Define business hours<br><strong>Multi-language</strong> \u2014 Auto-detect 30+ languages<br><strong>Custom outbound messages</strong> \u2014 Text drip sequences<br><strong>Custom behavior rules</strong> \u2014 Free-form instructions',
                side: 'right', align: 'start'
            },
            onHighlightStarted: function() { goTab('advanced'); }
        });

        steps.push({
            element: '#sbnConnect',
            popover: {
                title: 'CRM Connection',
                description: 'Connect your CRM (GoHighLevel, HubSpot, Salesforce, etc.), set up your webhook URL, and manage external API keys for third-party integrations.',
                side: 'right', align: 'start'
            }
        });

        steps.push({
            element: '#sbnTeam',
            popover: {
                title: 'Team Management',
                description: 'Invite team members, assign roles (admin/agent/viewer), set permissions, and track per-agent KPIs. Each member can get their own voice sub-account.',
                side: 'right', align: 'start'
            }
        });

        steps.push({
            element: '#sbnBilling',
            popover: {
                title: 'Billing & AI Minutes',
                description: '<strong>Billing</strong> \u2014 Manage subscription, switch plans, access Stripe portal<br><strong>AI Minutes</strong> \u2014 Check balance, purchase packages for AI voice calls<br><strong>Logs</strong> \u2014 Activity log with event filtering and status badges',
                side: 'right', align: 'start'
            }
        });

        // ============================================================
        // CHAPTER 7 — Finish
        // ============================================================
        chapterStarts.push(steps.length);
        steps.push({
            popover: {
                title: '<span class="igb-ch-badge">Tutorial Complete</span><br>You\'re All Set!',
                description: 'Here\'s the recommended <strong>setup order</strong>:<br><br><strong>1.</strong> Connect CRM<br><strong>2.</strong> Select Carriers<br><strong>3.</strong> Configure SMS Bot<br><strong>4.</strong> Configure Voice AI<br><strong>5.</strong> Activate Voice<br><strong>6.</strong> Review Workflows<br><strong>7.</strong> Start Dialing!<br><br>Replay this tour anytime from the <strong>Tutorial</strong> button in the sidebar.',
                popoverClass: 'igb-tut-finish'
            }
        });

        // Add "Skip chapter" buttons to chapter markers
        for (var i = 0; i < chapterStarts.length - 1; i++) {
            var idx = chapterStarts[i];
            var nextIdx = chapterStarts[i + 1];
            var marker = steps[idx];
            if (marker.popover && !marker.element) {
                marker.popover.description += '<br><button class="igb-tut-skip" onclick="window._igbSkipTo(' + nextIdx + ')">Skip this chapter \u2192</button>';
            }
        }

        return { steps: steps, chapterStarts: chapterStarts };
    }

    // ── Driver Instance ──────────────────────────────────────────
    var _driverObj = null;

    function startTutorial() {
        injectStyles();

        if (!window.driver || !window.driver.js || !window.driver.js.driver) {
            console.warn('[Tutorial] driver.js not loaded');
            return;
        }

        if (_driverObj && _driverObj.isActive()) {
            _driverObj.destroy();
        }

        var built = buildSteps();

        window._igbSkipTo = function(idx) {
            if (!_driverObj) return;
            if (typeof _driverObj.moveTo === 'function') {
                _driverObj.moveTo(idx);
            } else {
                _driverObj.destroy();
                setTimeout(function() { _driverObj.drive(idx); }, 50);
            }
        };

        _driverObj = window.driver.js.driver({
            steps: built.steps,
            showProgress: false,
            showButtons: ['next', 'previous', 'close'],
            allowClose: true,
            overlayColor: '#000',
            overlayOpacity: 0.65,
            stagePadding: 8,
            stageRadius: 8,
            popoverOffset: 12,
            animate: true,
            smoothScroll: true,
            allowKeyboardControl: true,
            doneBtnText: 'Finish',
            nextBtnText: 'Next \u2192',
            prevBtnText: '\u2190 Back',
            onDestroyStarted: function() {
                markTutorialDone();
                if (_driverObj) _driverObj.destroy();
            }
        });

        goTab('voicedialer');
        setTimeout(function() { _driverObj.drive(); }, 150);
    }

    // ── First-Login Auto-Trigger ─────────────────────────────────
    function checkFirstLogin() {
        if (isTutorialDone()) return;
        if (window.driver && window.driver.js && window.driver.js.driver) {
            startTutorial();
        } else {
            var attempts = 0;
            var iv = setInterval(function() {
                attempts++;
                if (window.driver && window.driver.js && window.driver.js.driver) {
                    clearInterval(iv);
                    startTutorial();
                } else if (attempts > 20) {
                    clearInterval(iv);
                }
            }, 500);
        }
    }

    // ── Public API ───────────────────────────────────────────────
    window.startDashboardTutorial = startTutorial;

    if (document.readyState === 'complete' || document.readyState === 'interactive') {
        setTimeout(checkFirstLogin, 1500);
    } else {
        document.addEventListener('DOMContentLoaded', function() {
            setTimeout(checkFirstLogin, 1500);
        });
    }

})();
