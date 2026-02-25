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

            /* ── Keyframes ───────────────────────────────────────────── */
            '@keyframes igbTutIn {',
            '  from { opacity:0; transform:scale(0.96) translateY(8px); filter:blur(4px); }',
            '  to   { opacity:1; transform:scale(1)    translateY(0);   filter:blur(0);   }',
            '}',
            '@keyframes igbRingGlow {',
            '  0%,100% { box-shadow: 0 0 0 2px rgba(0,255,136,0.45), 0 0 20px rgba(0,255,136,0.18), 0 0 40px rgba(0,255,136,0.06); }',
            '  50%     { box-shadow: 0 0 0 3px rgba(0,255,136,0.30), 0 0 32px rgba(0,255,136,0.12), 0 0 60px rgba(0,255,136,0.04); }',
            '}',

            /* ── Overlay ─────────────────────────────────────────────── */
            '.driver-overlay { background: rgba(0,0,0,0) !important; }',

            /* ════════════════════════════════════════════════════════════
               GLASSMORPHISM POPOVER
               ─ Multi-layer frosted glass with refraction gradients
               ─ Top-edge light streak via ::before pseudo
               ─ SVG noise grain for texture depth
               ─ 6-layer box-shadow for natural elevation
            ════════════════════════════════════════════════════════════ */
            '.driver-popover {',
            '  background:',
            '    radial-gradient(ellipse at 15% -10%, rgba(0,217,255,0.07) 0%, transparent 55%),',
            '    radial-gradient(ellipse at 85% 110%, rgba(0,255,136,0.04) 0%, transparent 55%),',
            '    linear-gradient(168deg, rgba(18,18,44,0.88) 0%, rgba(10,10,28,0.82) 45%, rgba(14,10,34,0.86) 100%) !important;',
            '  backdrop-filter: blur(40px) saturate(200%) brightness(1.05) !important;',
            '  -webkit-backdrop-filter: blur(40px) saturate(200%) brightness(1.05) !important;',
            '  border: 1px solid rgba(255,255,255,0.07) !important;',
            '  border-top-color: rgba(255,255,255,0.14) !important;',
            '  border-radius: 20px !important;',
            '  box-shadow:',
            '    0 0 0 0.5px rgba(255,255,255,0.05),',
            '    0 4px 16px  rgba(0,0,0,0.35),',
            '    0 16px 48px rgba(0,0,0,0.40),',
            '    0 40px 100px rgba(0,0,0,0.28),',
            '    inset 0 1px 0 rgba(255,255,255,0.14),',
            '    inset 0 0 40px rgba(0,255,136,0.015) !important;',
            '  padding: 24px 26px 18px !important;',
            '  min-height: unset !important;',
            '  max-width: 400px !important;',
            '  font-family: "Outfit", sans-serif !important;',
            '  animation: igbTutIn 0.35s cubic-bezier(0.22,1,0.36,1) !important;',
            '}',

            /* ── Top-edge light streak (glass refraction highlight) ── */
            '.driver-popover::before {',
            '  content: "" !important;',
            '  position: absolute !important;',
            '  top: 0 !important; left: 20px !important; right: 20px !important;',
            '  height: 1px !important;',
            '  background: linear-gradient(90deg, transparent, rgba(255,255,255,0.15) 20%, rgba(255,255,255,0.35) 50%, rgba(255,255,255,0.15) 80%, transparent) !important;',
            '  pointer-events: none !important;',
            '  z-index: 1 !important;',
            '}',

            /* ── Title ───────────────────────────────────────────────── */
            '.driver-popover-title {',
            '  color: #f0f0f8 !important;',
            '  font-weight: 700 !important;',
            '  font-size: 1rem !important;',
            '  line-height: 1.35 !important;',
            '  margin: 0 0 8px !important;',
            '  padding: 0 !important;',
            '  letter-spacing: -0.015em !important;',
            '}',

            /* ── Description ─────────────────────────────────────────── */
            '.driver-popover-description {',
            '  color: rgba(215,215,235,0.85) !important;',
            '  font-size: 0.84rem !important;',
            '  line-height: 1.65 !important;',
            '  margin: 0 !important;',
            '  padding: 0 !important;',
            '  font-weight: 400 !important;',
            '}',
            '.driver-popover-description strong { color: #fff !important; font-weight: 600 !important; }',
            '.driver-popover-description em { color: #8edfc0 !important; font-style: normal !important; }',
            '.driver-popover-description code {',
            '  background: rgba(0,255,136,0.08); border: 1px solid rgba(0,255,136,0.15);',
            '  color: #00e87a; border-radius: 5px; padding: 2px 7px;',
            '  font-size: 0.76rem; font-family: "JetBrains Mono", monospace;',
            '}',
            '.driver-popover-description p { margin: 0 0 6px !important; }',

            /* ── Footer / nav bar ────────────────────────────────────── */
            '.driver-popover-footer {',
            '  display: flex !important;',
            '  align-items: center !important;',
            '  margin-top: 16px !important;',
            '  padding-top: 12px !important;',
            '  padding-bottom: 0 !important;',
            '  border-top: 1px solid rgba(255,255,255,0.06) !important;',
            '  gap: 8px !important;',
            '}',

            /* ── Progress text (hidden, but styled if ever shown) ──── */
            '.driver-popover-progress-text {',
            '  color: rgba(255,255,255,0.22) !important;',
            '  font-size: 0.68rem !important;',
            '  font-family: "JetBrains Mono", monospace !important;',
            '  letter-spacing: 0.04em !important;',
            '  flex: 1 !important;',
            '}',

            /* ── Button container ────────────────────────────────────── */
            '.driver-popover-navigation-btns { gap: 8px !important; flex-shrink: 0 !important; }',

            /* ── Next / Done button — frosted green pill ─────────────── */
            '.driver-popover-next-btn {',
            '  background: linear-gradient(135deg, #00ff88 0%, #00d96e 100%) !important;',
            '  color: #041a0e !important;',
            '  border: none !important;',
            '  border-radius: 50px !important;',
            '  font-weight: 700 !important;',
            '  font-size: 0.78rem !important;',
            '  padding: 8px 22px !important;',
            '  letter-spacing: 0.02em !important;',
            '  text-shadow: 0 1px 0 rgba(255,255,255,0.15) !important;',
            '  box-shadow:',
            '    0 1px 3px rgba(0,0,0,0.2),',
            '    0 4px 16px rgba(0,255,136,0.25),',
            '    inset 0 1px 0 rgba(255,255,255,0.25) !important;',
            '  transition: all 0.2s cubic-bezier(0.22,1,0.36,1) !important;',
            '  cursor: pointer !important;',
            '}',
            '.driver-popover-next-btn:hover {',
            '  transform: translateY(-1px) !important;',
            '  box-shadow:',
            '    0 2px 6px rgba(0,0,0,0.25),',
            '    0 8px 28px rgba(0,255,136,0.35),',
            '    inset 0 1px 0 rgba(255,255,255,0.30) !important;',
            '}',
            '.driver-popover-next-btn:active { transform: translateY(0) !important; }',

            /* ── Back button — glass ghost pill ──────────────────────── */
            '.driver-popover-prev-btn {',
            '  background: rgba(255,255,255,0.04) !important;',
            '  color: rgba(255,255,255,0.50) !important;',
            '  border: 1px solid rgba(255,255,255,0.08) !important;',
            '  border-radius: 50px !important;',
            '  font-weight: 500 !important;',
            '  font-size: 0.78rem !important;',
            '  padding: 7px 18px !important;',
            '  cursor: pointer !important;',
            '  transition: all 0.2s cubic-bezier(0.22,1,0.36,1) !important;',
            '  backdrop-filter: blur(8px) !important;',
            '}',
            '.driver-popover-prev-btn:hover {',
            '  background: rgba(255,255,255,0.10) !important;',
            '  color: rgba(255,255,255,0.85) !important;',
            '  border-color: rgba(255,255,255,0.15) !important;',
            '  transform: translateY(-1px) !important;',
            '}',

            /* ── Close button ────────────────────────────────────────── */
            '.driver-popover-close-btn {',
            '  color: rgba(255,255,255,0.25) !important;',
            '  font-size: 1.05rem !important;',
            '  top: 16px !important; right: 18px !important;',
            '  transition: all 0.2s !important;',
            '  width: 26px !important; height: 26px !important;',
            '  display: flex !important; align-items: center !important; justify-content: center !important;',
            '  border-radius: 50% !important;',
            '}',
            '.driver-popover-close-btn:hover {',
            '  color: rgba(255,255,255,0.8) !important;',
            '  background: rgba(255,255,255,0.06) !important;',
            '}',

            /* ── Arrow — matched to glass tint ───────────────────────── */
            '.driver-popover-arrow-side-left.driver-popover-arrow  { border-right-color:  rgba(16,16,38,0.92) !important; }',
            '.driver-popover-arrow-side-right.driver-popover-arrow { border-left-color:   rgba(16,16,38,0.92) !important; }',
            '.driver-popover-arrow-side-top.driver-popover-arrow   { border-bottom-color: rgba(16,16,38,0.92) !important; }',
            '.driver-popover-arrow-side-bottom.driver-popover-arrow{ border-top-color:    rgba(16,16,38,0.92) !important; }',

            /* ════════════════════════════════════════════════════════════
               ELEMENT HIGHLIGHT — refined green glow
               Softer, wider glow with slower pulsation
            ════════════════════════════════════════════════════════════ */
            '.driver-active-element, .driver-highlighted-element {',
            '  outline: 2px solid rgba(0,255,136,0.55) !important;',
            '  outline-offset: 4px !important;',
            '  border-radius: 10px !important;',
            '  animation: igbRingGlow 2.5s ease-in-out infinite !important;',
            '}',

            /* ════════════════════════════════════════════════════════════
               CHAPTER MARKER — centered cyan-tinted glass
            ════════════════════════════════════════════════════════════ */
            '.igb-tut-chapter {',
            '  max-width: 460px !important;',
            '  background:',
            '    radial-gradient(ellipse at 50% -20%, rgba(0,217,255,0.10) 0%, transparent 60%),',
            '    radial-gradient(ellipse at 50% 120%, rgba(0,120,255,0.04) 0%, transparent 60%),',
            '    linear-gradient(168deg, rgba(14,14,40,0.90) 0%, rgba(10,10,30,0.85) 100%) !important;',
            '  border: 1px solid rgba(0,217,255,0.10) !important;',
            '  border-top-color: rgba(0,217,255,0.22) !important;',
            '  box-shadow:',
            '    0 0 0 0.5px rgba(0,217,255,0.08),',
            '    0 4px 16px rgba(0,0,0,0.35),',
            '    0 16px 48px rgba(0,0,0,0.40),',
            '    0 40px 100px rgba(0,0,0,0.28),',
            '    inset 0 1px 0 rgba(0,217,255,0.12),',
            '    inset 0 0 40px rgba(0,217,255,0.015) !important;',
            '}',
            '.igb-tut-chapter::before {',
            '  background: linear-gradient(90deg, transparent, rgba(0,217,255,0.15) 20%, rgba(0,217,255,0.3) 50%, rgba(0,217,255,0.15) 80%, transparent) !important;',
            '}',
            '.igb-tut-chapter .driver-popover-title { font-size: 1.15rem !important; text-align: center !important; color: #fff !important; }',
            '.igb-tut-chapter .driver-popover-description { text-align: center !important; color: rgba(200,210,230,0.82) !important; }',

            /* ════════════════════════════════════════════════════════════
               FINISH — green-tinted celebratory glass
            ════════════════════════════════════════════════════════════ */
            '.igb-tut-finish {',
            '  max-width: 480px !important;',
            '  background:',
            '    radial-gradient(ellipse at 50% -20%, rgba(0,255,136,0.10) 0%, transparent 60%),',
            '    radial-gradient(ellipse at 50% 120%, rgba(0,180,100,0.04) 0%, transparent 60%),',
            '    linear-gradient(168deg, rgba(12,18,30,0.90) 0%, rgba(8,14,24,0.85) 100%) !important;',
            '  border: 1px solid rgba(0,255,136,0.10) !important;',
            '  border-top-color: rgba(0,255,136,0.25) !important;',
            '  box-shadow:',
            '    0 0 0 0.5px rgba(0,255,136,0.10),',
            '    0 4px 16px rgba(0,0,0,0.35),',
            '    0 16px 48px rgba(0,0,0,0.40),',
            '    0 40px 100px rgba(0,0,0,0.28),',
            '    inset 0 1px 0 rgba(0,255,136,0.15),',
            '    inset 0 0 40px rgba(0,255,136,0.02) !important;',
            '}',
            '.igb-tut-finish::before {',
            '  background: linear-gradient(90deg, transparent, rgba(0,255,136,0.12) 20%, rgba(0,255,136,0.28) 50%, rgba(0,255,136,0.12) 80%, transparent) !important;',
            '}',
            '.igb-tut-finish .driver-popover-title { font-size: 1.2rem !important; text-align: center !important; color: #00ff88 !important; }',
            '.igb-tut-finish .driver-popover-description { text-align: center !important; }',

            /* ── Chapter badge pill ───────────────────────────────────── */
            '.igb-ch-badge {',
            '  display: inline-block;',
            '  font-size: 0.56rem;',
            '  font-weight: 700;',
            '  text-transform: uppercase;',
            '  letter-spacing: 3px;',
            '  color: #00d9ff;',
            '  background: rgba(0,217,255,0.06);',
            '  border: 1px solid rgba(0,217,255,0.18);',
            '  padding: 3px 12px;',
            '  border-radius: 20px;',
            '  margin-bottom: 8px;',
            '}',

            /* ── Skip chapter button ─────────────────────────────────── */
            '.igb-tut-skip {',
            '  display: inline-block;',
            '  margin-top: 12px;',
            '  background: rgba(255,255,255,0.03);',
            '  border: 1px solid rgba(255,255,255,0.08);',
            '  color: rgba(255,255,255,0.35);',
            '  font-size: 0.71rem;',
            '  padding: 6px 18px;',
            '  border-radius: 50px;',
            '  cursor: pointer;',
            '  font-family: "Outfit", sans-serif;',
            '  transition: all 0.2s cubic-bezier(0.22,1,0.36,1);',
            '  backdrop-filter: blur(8px);',
            '}',
            '.igb-tut-skip:hover {',
            '  border-color: rgba(0,217,255,0.30);',
            '  color: #00d9ff;',
            '  background: rgba(0,217,255,0.05);',
            '  transform: translateY(-1px);',
            '}',

            /* ── Keyboard hint ───────────────────────────────────────── */
            '.igb-key-hint { display: block; margin-top: 10px; font-size: 0.67rem; color: rgba(255,255,255,0.25); }',
            '.igb-key-hint kbd {',
            '  background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.10);',
            '  border-radius: 4px; padding: 1px 6px;',
            '  font-family: "JetBrains Mono", monospace; font-size: 0.63rem;',
            '  color: rgba(255,255,255,0.45);',
            '}'

        ].join('\n');
        document.head.appendChild(style);
    }

    // ── Persistence ────────────────────────────────────────────
    function storageKey() {
        var boot = window.DASHBOARD_BOOT || {};
        var id = boot.userEmail || boot.locationId || 'anon';
        return STORAGE_PREFIX + id;
    }
    function isTutorialDone()  { try { return localStorage.getItem(storageKey()) === '1'; } catch(e) { return false; } }
    function markTutorialDone(){ try { localStorage.setItem(storageKey(), '1'); } catch(e) {} }

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

        // Topbar
        steps.push({ element: '#themeToggleBtn',
            popover: { title: 'Theme Toggle', description: 'Switch between <strong>dark mode</strong> and <strong>light mode</strong>. Your preference is saved automatically.', side: 'bottom' }
        });

        // Sidebar footer — tutorial button itself
        steps.push({ element: '#sbnTutorial',
            popover: { title: 'Tutorial Button', description: 'This button. Click it <strong>any time</strong> to replay this entire walkthrough from the beginning. It\'s always here in the footer.', side: 'right' }
        });

        // Operator Profile
        steps.push({ element: '#user_name',
            popover: { title: 'Operator Name', description: 'Your name as it appears in the system. The AI uses this to sign off messages and in the voice intro — <em>"This is [Your Name] from ABC Insurance."</em>', side: 'right' },
            onHighlightStarted: function() { ensureOpen('sbSectionMore'); }
        });
        steps.push({ element: '#bio',
            popover: { title: 'Agent Notes', description: 'Internal notes about yourself or your agency. The AI can reference this context when personalizing conversations — licenses held, specialties, territories.', side: 'right' }
        });

        // System Params
        steps.push({ element: '.sb-params',
            popover: { title: 'System Parameters', description: 'Technical readout showing your <strong>Location ID</strong> (copy button included), OAuth <strong>access &amp; refresh tokens</strong>, and CRM connection status. If the bot stops working, check here first.', side: 'right' },
            onHighlightStarted: function() {
                var p = document.querySelector('.sb-params');
                if (p) p.style.display = 'block';
            }
        });
        steps.push({ element: '#connectBtn',
            popover: { title: 'Connect / Reauthorize CRM', description: 'If your CRM connection expires or needs to be refreshed, click this button to re-run the OAuth flow. The green pulsing button means action is needed.', side: 'right' }
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
            popover: { title: 'Call Statistics', description: 'Opens the stats panel — click it now to see what\'s inside.', side: 'bottom' }
        });
        steps.push({ element: '#dialerStatsPanel',
            popover: { title: 'Stats Panel', description: 'Your call performance at a glance. Filter by <strong>Today, 7 Days, 30 Days, or All Time</strong>. Shows total calls made, connection rate, average duration, and an hourly breakdown chart. Hit the refresh icon to update.', side: 'bottom' },
            onHighlightStarted: function() {
                if (typeof dialerToggleStats === 'function') {
                    var panel = document.getElementById('dialerStatsPanel');
                    if (panel && panel.style.display === 'none') dialerToggleStats();
                }
            },
            onDeselected: function() {
                var panel = document.getElementById('dialerStatsPanel');
                if (panel && panel.style.display !== 'none' && typeof dialerToggleStats === 'function') dialerToggleStats();
            }
        });
        steps.push({ element: '#dialerSettingsToggle',
            popover: { title: 'Dialer Settings', description: 'Opens the settings panel — let\'s walk through every option inside.', side: 'bottom' }
        });
        steps.push({ element: '#dialerSettingsPanel',
            popover: { title: 'Settings Panel', description: 'All dialer configuration in one place. Expand to see each setting below.', side: 'bottom' },
            onHighlightStarted: function() {
                if (typeof toggleDialerSettings === 'function') {
                    var panel = document.getElementById('dialerSettingsPanel');
                    if (panel && panel.style.display === 'none') toggleDialerSettings();
                }
            }
        });
        steps.push({ element: '#voiceDialAttempts',
            popover: { title: 'Dial Attempts', description: 'How many times the auto-dialer retries a <strong>no-answer</strong> before moving to the next contact. 2 is the sweet spot — aggressive enough to reach leads, not so many you annoy them.', side: 'left', align: 'start' }
        });
        steps.push({ element: '#voiceAutoRecord',
            popover: { title: 'Auto-Record', description: 'Automatically records every outbound call. Recordings appear in the Recordings tab and can be played back, downloaded, or transcribed.', side: 'left', align: 'start' }
        });
        steps.push({ element: '#voiceAutoTranscribe',
            popover: { title: 'Auto-Transcribe', description: 'Generates a full <strong>word-for-word transcript</strong> of every recorded call. Transcripts are labeled by speaker (Lead / AI Agent) and stored permanently.', side: 'left', align: 'start' }
        });
        steps.push({ element: '#voiceLocalPresence',
            popover: { title: 'Local Presence', description: 'Calls leads from a number that <strong>matches their area code</strong>, dramatically improving answer rates. Requires multiple local numbers in your pool.', side: 'left', align: 'start' }
        });
        steps.push({ element: '#voiceVoicemailDrop',
            popover: { title: 'Voicemail Drop', description: 'When the AI detects a voicemail, it automatically leaves a <strong>pre-recorded message</strong> and moves to the next contact — no dead air, no wasted time.', side: 'left', align: 'start' }
        });
        steps.push({ element: '#voiceTransferNumber',
            popover: { title: 'Transfer Number', description: 'The phone number calls are transferred to when a lead asks to speak to someone live. Usually your <strong>personal cell or office line</strong>. Enter in E.164 format (+1XXXXXXXXXX).', side: 'left', align: 'start' }
        });
        steps.push({ element: '#audioInputDevice',
            popover: { title: 'Microphone Selection', description: 'Choose which microphone to use when you <strong>take over a call</strong> (intercept) or make a Human-mode call. If you\'re on a headset, select it here.', side: 'left', align: 'start' }
        });
        steps.push({ element: '#audioOutputDevice',
            popover: { title: 'Speaker Selection', description: 'Choose where call audio plays when you <strong>listen in</strong> or intercept. Use headphones for privacy and better audio quality.', side: 'left', align: 'start' },
            onDeselected: function() {
                // close settings panel when done
                var panel = document.getElementById('dialerSettingsPanel');
                if (panel && panel.style.display !== 'none' && typeof toggleDialerSettings === 'function') toggleDialerSettings();
            }
        });
        steps.push({ element: '#voipSetupBanner',
            popover: { title: 'VoIP Setup', description: 'If you haven\'t set up browser calling yet, this orange banner appears. Click <strong>"Setup VoIP"</strong> to register your browser as a voice endpoint — required for Human-mode calls and intercept.', side: 'bottom' },
            onHighlightStarted: function() {
                var banner = document.getElementById('voipSetupBanner');
                if (banner) { banner.style.display = 'flex'; banner.dataset.tutorialShown = '1'; }
            },
            onDeselected: function() {
                var banner = document.getElementById('voipSetupBanner');
                if (banner && banner.dataset.tutorialShown) { banner.style.display = 'none'; delete banner.dataset.tutorialShown; }
            }
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
            popover: { title: 'Call Disposition Panel', description: 'After each call ends, this row appears so you can <strong>tag the outcome</strong>. Selecting a disposition logs the result and helps the AI adapt its approach on future calls with that lead.', side: 'bottom' },
            onHighlightStarted: function() { showEl('dialerDisposition', 'flex'); }
        });
        steps.push({ element: '[onclick*="not_answered"]',
            popover: { title: 'Not Answered', description: '<strong>Nobody picked up.</strong> The auto-dialer logs this as a missed attempt and will retry based on your Dial Attempts setting. AI may follow up via SMS automatically.', side: 'bottom' },
            onHighlightStarted: function() { showEl('dialerDisposition', 'flex'); }
        });
        steps.push({ element: '[onclick*="hung_up"]',
            popover: { title: 'Hung Up', description: '<strong>Lead answered but disconnected.</strong> Common with cold leads. The AI notes this in the contact\'s history and may adapt its opener on the next attempt.', side: 'bottom' },
            onHighlightStarted: function() { showEl('dialerDisposition', 'flex'); }
        });
        steps.push({ element: '[onclick*="not_interested"]',
            popover: { title: 'Not Interested', description: '<strong>Lead declined.</strong> Tags this contact as opted-out of active outreach. The AI will back off and not aggressively re-engage unless the lead initiates contact again.', side: 'bottom' },
            onHighlightStarted: function() { showEl('dialerDisposition', 'flex'); }
        });
        steps.push({ element: '[onclick*="left_voicemail"]',
            popover: { title: 'Left Voicemail', description: '<strong>Voicemail was left.</strong> Logs the attempt so the AI knows a message was sent. Prevents immediate re-voicemail on the next dial attempt — respects the lead\'s inbox.', side: 'bottom' },
            onHighlightStarted: function() { showEl('dialerDisposition', 'flex'); }
        });
        steps.push({ element: '[onclick="dialerSetDisposition(\'none\')"]',
            popover: { title: 'No Disposition', description: '<strong>Skip tagging</strong> and move on without logging an outcome. Useful for test calls, system checks, or when no label accurately describes what happened.', side: 'bottom' },
            onHighlightStarted: function() { showEl('dialerDisposition', 'flex'); },
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
            popover: { title: 'AI Draft Reply', description: 'Click to have the AI <strong>generate a contextual reply</strong> based on the full conversation. It drafts the message — you review, edit if needed, and send. Click again for a fresh draft if the first isn\'t right.', side: 'bottom' },
            onHighlightStarted: function() {
                var c = document.getElementById('dlrSmsComposer');
                if (c) { c.style.display = 'block'; c.dataset.tutorialShown = '1'; }
            }
        });
        steps.push({ element: '.dlr-qr-btn',
            popover: { title: 'Quick Reply Templates', description: 'One-tap pre-written responses: <strong>Follow-up, Check-in, Coverage, Schedule,</strong> and <strong>On it</strong>. Click any chip to instantly fill the compose box — edit the text before sending if needed. Great for high-volume dialing sessions.', side: 'top' },
            onHighlightStarted: function() {
                var c = document.getElementById('dlrSmsComposer');
                if (c) c.style.display = 'block';
            }
        });
        steps.push({ element: '#dlrSmsText',
            popover: { title: 'Message Composer', description: 'Type your SMS message here. The box <strong>auto-grows</strong> as you type. Press <strong>Enter</strong> to send instantly, or <strong>Shift+Enter</strong> for a new line. AI-generated drafts also populate here for review.', side: 'top' },
            onHighlightStarted: function() {
                var c = document.getElementById('dlrSmsComposer');
                if (c) c.style.display = 'block';
            }
        });
        steps.push({ element: '#dlrSmsSendBtn',
            popover: { title: 'Send Message', description: 'Send your composed message through the CRM SMS channel. The message appears in the thread immediately with a status icon: <em>green checkmark = delivered, orange = pending, red = failed.</em>', side: 'left' },
            onHighlightStarted: function() {
                var c = document.getElementById('dlrSmsComposer');
                if (c) c.style.display = 'block';
            }
        });
        steps.push({ element: '#dlrCharCount',
            popover: { title: 'Character Counter', description: 'Live SMS character count. Turns <strong>orange at 140 chars</strong> (approaching limit) and <strong>red at 160+</strong> (splits into multi-part SMS, which costs extra). Keeping messages under 160 chars maximizes deliverability.', side: 'top' },
            onHighlightStarted: function() {
                var c = document.getElementById('dlrSmsComposer');
                if (c) c.style.display = 'block';
            },
            onDeselected: function() {
                var c = document.getElementById('dlrSmsComposer');
                if (c && c.dataset.tutorialShown) { c.style.display = 'none'; delete c.dataset.tutorialShown; }
            }
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
            popover: { title: 'Calendar Selection', description: 'Click <strong>"Load Calendars"</strong> to pull your CRM calendars, then select the one the bot should book appointments into. This is critical for the booking flow to work.', side: 'left', align: 'start' }
        });
        steps.push({ element: '#bot_name',
            popover: { title: 'Bot Name', description: 'The name the SMS bot introduces itself as. Example: "Sarah" or "Alex". This appears in outbound messages — <em>"Hi! This is Sarah from..."</em>', side: 'left', align: 'start' }
        });
        steps.push({ element: '#initial_message',
            popover: { title: 'Initial Outreach Message', description: 'The <strong>first SMS</strong> the bot sends to new leads. Make it warm, personal, and action-oriented. The bot sends this when a new lead webhook fires.', side: 'left', align: 'start' }
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
            popover: { title: 'Voice AI Status', description: 'Shows whether Voice AI is currently <strong>enabled</strong> (green) or <strong>disabled</strong> (red). Toggle with the switch to the right.', side: 'left', align: 'start' },
            onHighlightStarted: function() { goTab('voice'); }
        });
        steps.push({ element: '#voiceEnabled',
            popover: { title: 'Enable / Disable Voice', description: 'Master switch for the voice AI agent. When <strong>off</strong>, all outbound and inbound voice AI features are disabled. Turn it on to start using the dialer\'s AI mode.', side: 'left', align: 'start' }
        });
        steps.push({ element: '#voiceSelection',
            popover: { title: 'AI Voice Selection', description: 'Choose from <strong>7 AI voices</strong>: Ara, Eve, Leo, Rex, Sal, Mika, and Vale. Each has a unique tone — from warm and friendly to confident and authoritative.', side: 'bottom', align: 'start' }
        });
        steps.push({ element: '#voicePreviewBtn',
            popover: { title: 'Preview Voice', description: 'Click to hear a <strong>sample of the selected voice</strong>. Test different voices to find the one that best matches your agency\'s brand.', side: 'left', align: 'start' }
        });
        steps.push({ element: '#voiceBotName',
            popover: { title: 'Voice Agent Name', description: 'The name the AI introduces itself as on calls. Example: <em>"Hi, this is Sarah calling from ABC Insurance."</em>', side: 'bottom', align: 'start' }
        });
        steps.push({ element: '#voiceCallScript',
            popover: { title: 'Call Script', description: 'The <strong>reference script</strong> your AI agent follows. Write your ideal call flow — greeting, qualification questions, objection handling, and booking close. The AI adapts it naturally while staying on-script.', side: 'bottom', align: 'start' }
        });
        steps.push({ element: '#voiceInstructions',
            popover: { title: 'Behavior Instructions', description: 'Custom <strong>behavioral rules</strong> for the voice AI. Example: "Never offer quotes over the phone" or "Always mention our 30-year track record." These instructions shape how the AI handles conversations.', side: 'bottom', align: 'start' }
        });

        // Voice sub-tabs
        steps.push({ element: '#vmenu-activation',
            popover: { title: 'Activate Voice', description: 'This panel provisions your <strong>voice sub-account, phone number, and voice app</strong>. Click "Activate Voice" once to set everything up. After activation, your dialer is ready to make calls.', side: 'bottom' }
        });
        steps.push({ element: '#vmenu-numbers',
            popover: { title: 'Phone Numbers', description: 'Manage your voice phone numbers. <strong>Buy additional numbers</strong> (local, toll-free, or mobile), view your active numbers, enable CNAM caller ID, and release numbers you no longer need.', side: 'bottom' }
        });
        steps.push({ element: '#vmenu-trusthub',
            popover: { title: 'Spam Protection', description: 'Register your business with the Trust Hub to <strong>reduce spam flagging</strong>. Enter your business name, EIN, and address. Protected numbers show your real business name on caller ID.', side: 'bottom' }
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
            popover: { title: 'Professionalism Slider', description: 'Slide from <strong>Casual</strong> (friendly, conversational, emojis welcome) to <strong>Ultra Professional</strong> (formal, corporate, no slang). Most insurance agents land at level 3–4 — approachable but credible.', side: 'bottom', align: 'start' },
            onHighlightStarted: function() { goTab('advanced'); }
        });
        steps.push({ element: '#auto_emoji',
            popover: { title: 'Emoji Usage', description: 'Allow the AI to use <strong>emojis in SMS messages</strong>. On = warm and expressive. Off = clean, text-only — better for formal or older demographics. You can toggle this independently of the professionalism level.', side: 'left', align: 'start' }
        });
        steps.push({ element: '.resp-len-btn',
            popover: { title: 'Response Length', description: 'Control how verbose the AI\'s replies are:<br><strong>Short</strong> — 1–2 sentences, crisp and punchy.<br><strong>Balanced</strong> — 2–4 sentences, recommended for most agents.<br><strong>Detailed</strong> — Full explanations, great for complex coverage questions where leads need education.', side: 'bottom', align: 'start' }
        });
        steps.push({ element: '#humor_enabled',
            popover: { title: 'Humor Mode', description: 'After 5+ unanswered messages, the AI sends a <strong>light, tasteful joke</strong> to re-engage cold leads. It sounds natural and disarming — and it works. Leads who ghosted often respond to the joke and re-enter the conversation.', side: 'left', align: 'start' }
        });
        steps.push({ element: '#lead_reengagement',
            popover: { title: 'Lead Re-engagement', description: 'Automatically <strong>follows up with silent leads</strong> on a spaced schedule. The AI sends varied nudge messages so it never feels repetitive. Runs until the lead responds, books, or asks to stop.', side: 'left', align: 'start' }
        });
        steps.push({ element: '#booking_confirmation',
            popover: { title: 'Booking Confirmation', description: 'Before officially writing the appointment to your calendar, the AI <strong>re-confirms the date and time</strong> with the lead. Eliminates the "I didn\'t know we had a meeting" situation and reduces no-shows significantly.', side: 'left', align: 'start' }
        });
        steps.push({ element: '#speed_to_lead',
            popover: { title: 'Speed to Lead', description: 'Fires an <strong>instant AI reply</strong> the moment a new lead arrives. Studies show contacting a lead within 5 minutes is 9× more effective than waiting 30 minutes. This toggle ensures your bot beats every competitor to the conversation.', side: 'left', align: 'start' }
        });
        steps.push({ element: '#conversation_memory',
            popover: { title: 'Conversation Memory', description: 'The AI <strong>remembers details from past conversations</strong> — their spouse\'s name, health concerns, coverage objections, previous quotes. When leads return, they feel recognized, not cold-called all over again.', side: 'left', align: 'start' }
        });
        steps.push({ element: '#multi_language',
            popover: { title: 'Multi-Language Detection', description: 'Automatically <strong>detects the lead\'s language and replies in kind</strong>. If they text in Spanish, the AI responds in Spanish. Supports 30+ languages. Essential if you serve diverse markets.', side: 'left', align: 'start' }
        });
        steps.push({ element: '#objection_persistence',
            popover: { title: 'Objection Persistence', description: 'How many angles the AI tries when a lead pushes back before offering a <strong>graceful exit</strong>. Level 1 = soft and accepting. Level 5 = tenacious multi-angle rebuttal. Most agents find levels 2–3 strike the right balance.', side: 'bottom', align: 'start' }
        });
        steps.push({ element: '#after_hours_enabled',
            popover: { title: 'After Hours Mode', description: 'Define your <strong>business hours</strong>. Outside those hours, instead of doing a full AI conversation, the bot informs the lead it\'s after hours and offers to follow up first thing in the morning — professional and on-brand.', side: 'left', align: 'start' }
        });
        steps.push({ element: '#outboundContainer',
            popover: { title: 'Custom Outbound Messages', description: '<strong>Build a text drip sequence.</strong> Each message is sent once per lead in order — GrokBot checks them off as they go. After all custom messages are exhausted, the AI switches to fully autonomous mode. The result: <em>your curated drip + the AI\'s intelligence.</em>', side: 'top', align: 'start' }
        });
        steps.push({ element: '[onclick*="addOutboundMsg"]',
            popover: { title: 'Add Outbound Message', description: 'Click to add a new message to your drip sequence. Write the message, save — and GrokBot will start using it for every new lead from that point forward. There\'s no limit to how many messages you can add.', side: 'left', align: 'start' }
        });
        steps.push({ element: '#custom_behavior',
            popover: { title: 'Custom Behavior Instructions', description: 'Free-form rules that <strong>override the AI\'s defaults</strong>. Write anything:<br><em>"Always mention our 5-star Google rating"</em><br><em>"Never discuss policy prices — get them on a call first"</em><br><em>"Thank veterans for their service"</em><br>The more specific, the better the AI performs.', side: 'top', align: 'start' }
        });
        steps.push({ element: '[onclick*="resetAdvancedSettings"]',
            popover: { title: 'Reset to Defaults', description: 'Reverts all advanced settings back to factory defaults. <strong>Use carefully</strong> — this will overwrite all customizations. Good for troubleshooting if behavior feels off after recent changes.', side: 'top' }
        });
        steps.push({ element: '#saveAdvancedBtn',
            popover: { title: 'Save Advanced Settings', description: 'Saves all your personality, behavior, and drip message changes. They apply immediately to <strong>all future conversations</strong> — both SMS and voice AI.', side: 'top' }
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
            popover: { title: 'Purchase Packages', description: 'Buy AI minute bundles. Larger packages offer <strong>better per-minute pricing</strong>. Minutes never expire and stack on top of your existing balance — buy in bulk for the best rate.', side: 'top' }
        });

        // Billing
        steps.push({ element: '#billing',
            popover: { title: 'Billing & Subscription', description: 'Manage your <strong>InsuranceGrokBot subscription</strong> here. Click <strong>"Open Stripe Portal"</strong> to access the secure Stripe dashboard — update your payment card, view past invoices, download receipts, or cancel your plan. If you installed via GHL Marketplace, manage billing from within your Lead Connector account.', side: 'top' },
            onHighlightStarted: function() { goTab('billing'); }
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
            popover: { title: 'Discord Chat Toggle', description: 'Opens the <strong>Discord chat panel</strong> that slides out from the sidebar. View messages, reply, send new messages — all without switching apps. The badge shows unread message count.', side: 'right' }
        });
        steps.push({ element: '#discordMessages',
            popover: { title: 'Discord Message Feed', description: 'Live messages from your selected Discord channel appear here. The panel <strong>polls every 4 seconds</strong> while open, and checks for unread messages every 45 seconds in the background — keeping you connected without refreshing the page.', side: 'left' },
            onHighlightStarted: function() {
                if (typeof openDiscordPanel === 'function') openDiscordPanel();
            }
        });
        steps.push({ element: '#discordReplyText',
            popover: { title: 'Discord Reply Box', description: 'Type a message to your team here. Press <strong>Ctrl+Enter</strong> to send, or <strong>Shift+Enter</strong> for a new line. Supports standard Discord markdown: **bold**, _italic_, `code blocks`. Great for quick handoff notes during live calls.', side: 'top' }
        });
        steps.push({ element: '#discordSendBtn',
            popover: { title: 'Send to Discord', description: 'Sends your message to the Discord channel. Your team sees it instantly in Discord (on any device) as well as here in the panel. Use it for <strong>real-time handoffs</strong> — "Hot lead on the line, name: John Smith, call him now!"', side: 'top' }
        });
        steps.push({ element: '#discordScrollBtn',
            popover: { title: 'Jump to Latest', description: 'Appears when you\'ve <strong>scrolled up</strong> in message history. Click to jump back to the most recent messages. Automatically hides when you\'re already at the bottom.', side: 'left' },
            onHighlightStarted: function() {
                var btn = document.getElementById('discordScrollBtn');
                if (btn) { btn.style.display = 'block'; btn.dataset.tutorialShown = '1'; }
            },
            onDeselected: function() {
                var btn = document.getElementById('discordScrollBtn');
                if (btn && btn.dataset.tutorialShown) { btn.style.display = 'none'; delete btn.dataset.tutorialShown; }
            }
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

        // Skip-to-chapter: try moveTo, fall back to drive(idx)
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
            steps: steps,
            showProgress: false,
            showButtons: ['next', 'previous', 'close'],
            allowClose: true,
            overlayColor: '#000',
            overlayOpacity: 0.7,
            stagePadding: 10,
            stageRadius: 10,
            popoverOffset: 14,
            animate: true,
            smoothScroll: true,
            allowKeyboardControl: true,
            doneBtnText: 'Finish',
            nextBtnText: 'Next &rarr;',
            prevBtnText: '&larr; Back',
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
