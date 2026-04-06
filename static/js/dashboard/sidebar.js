        // ══════════════════════════════════
        // SIDEBAR NAVIGATION (2026)
        // ══════════════════════════════════

        // Safe localStorage wrapper — cross-origin iframes can block storage access
        function _lsGet(k) { try { return localStorage.getItem(k); } catch(e) { return null; } }
        function _lsSet(k,v) { try { localStorage.setItem(k,v); } catch(e) {} }

        // Page title map
        const _pageTitles = {
            voicedialer: 'Workspace', config: 'SMS Config', voice: 'Voice Config',
            workflows: 'Workflows', connect: 'Integrations',
            carriers: 'Contracted Carriers', advanced: 'Advanced', aiminutes: 'AI Minutes',
            billing: 'Billing', logs: 'Activity Logs',
            'agency-members': 'Members', 'agency-kpis': 'Statistics',
            whitelabel: 'White Label', team: 'Teams',
            profile: 'Profile', domain: 'Domain & Website',
            // Sub-panel direct IDs
            'phone-numbers': 'Phone Numbers',
            'spam-monitoring': 'Spam Monitoring',
            'voice-ai': 'Voice AI',
            'dialer-settings': 'Dialer Settings',
            'bot-identity': 'Bot Identity',
            'calendar-crm': 'Calendar & CRM',
            'sms-numbers': 'SMS Numbers',
            'a2p-10dlc': 'A2P 10DLC',
            'business-profile': 'Business Profile',
            'integration-ghl': 'LeadConnector',
            'integration-hubspot': 'HubSpot',
            'integration-crms': 'Other CRMs',
            'integration-api': 'API Access',
            'integration-outbound': 'Outbound Calls',
            'integration-google': 'Google Calendar',
            'integration-discord': 'Discord',
            'integration-slack': 'Slack',
            'integration-training': 'Training API',
        };

        // Sub-panel route map: logical ID → [baseTabId, panelName]
        // Each sidebar click now has a unique logical ID that maps to the right tab + sub-panel.
        const _subPanelRoutes = {
            'phone-numbers':        ['voice',  'numbers'],
            'spam-monitoring':      ['voice',  'spammonitoring'],
            'voice-ai':             ['voice',  'settings'],
            'dialer-settings':      ['voice',  'dialer'],
            'bot-identity':         ['config', 'identity'],
            'calendar-crm':         ['config', 'calendar'],
            'sms-numbers':          ['config', 'smsnumbers'],
            'a2p-10dlc':            ['config', 'a2p'],
            'integration-ghl':      ['connect','ghl'],
            'integration-hubspot':  ['connect','hubspot'],
            'integration-crms':     ['connect','other-crms'],
            'integration-api':      ['connect','api'],
            'integration-outbound': ['connect','outbound'],
            'integration-google':   ['connect','google'],
            'integration-discord':  ['connect','discord'],
            'integration-slack':    ['connect','slack'],
            'integration-training': ['connect','training'],
        };

        // Map of tab IDs → sidebar element IDs (used by mobileNav for active-state passthrough)
        // New accordion nav manages its own active state via activateL2/L3 functions.
        // All entries point to sbnWorkspace so mobileNav gets a valid element, not null.
        const _tabToBtn = {
            voicedialer: 'sbnWorkspace', config: 'sbnWorkspace', voice: 'sbnWorkspace',
            workflows: 'sbnWorkspace', connect: 'sbnWorkspace',
            carriers: 'sbnWorkspace', advanced: 'sbnWorkspace', aiminutes: 'sbnWorkspace',
            billing: 'sbnWorkspace', logs: 'sbnWorkspace',
            'agency-members': 'sbnWorkspace', 'agency-kpis': 'sbnWorkspace',
            whitelabel: 'sbnWorkspace', team: 'sbnWorkspace',
            profile: 'sbnWorkspace', domain: 'sbnWorkspace',
            'business-profile': 'sbnWorkspace',
            'phone-numbers': 'sbnWorkspace', 'spam-monitoring': 'sbnWorkspace',
            'voice-ai': 'sbnWorkspace', 'dialer-settings': 'sbnWorkspace',
            'bot-identity': 'sbnWorkspace', 'calendar-crm': 'sbnWorkspace',
            'sms-numbers': 'sbnWorkspace', 'a2p-10dlc': 'sbnWorkspace',
            'integration-ghl': 'sbnWorkspace', 'integration-hubspot': 'sbnWorkspace', 'integration-crms': 'sbnWorkspace',
            'integration-api': 'sbnWorkspace', 'integration-outbound': 'sbnWorkspace',
            'integration-google': 'sbnWorkspace', 'integration-discord': 'sbnWorkspace',
            'integration-slack': 'sbnWorkspace', 'integration-training': 'sbnWorkspace',
        };

        function sidebarNavigate(tabId, btnEl) {
            // Resolve sub-panel routes: 'phone-numbers' → ['voice', 'numbers']
            const route = _subPanelRoutes[tabId];
            const baseTabId = route ? route[0] : tabId;
            const subPanel  = route ? route[1] : null;

            // Update active state on all sidebar nav items
            document.querySelectorAll('.sb-nav-item').forEach(b => b.classList.remove('active'));
            if (btnEl) btnEl.classList.add('active');

            // Update page title using logical ID first, then base tab ID
            const titleEl = document.getElementById('dashPageTitle');
            if (titleEl) titleEl.textContent = _pageTitles[tabId] || _pageTitles[baseTabId] || 'Dashboard';

            // Unified: hide ALL panes first, then show only the target.
            document.querySelectorAll('#dashTabsContent > .tab-pane').forEach(p => {
                p.classList.remove('show', 'active');
            });
            const pane = document.getElementById(baseTabId);
            if (pane) pane.classList.add('show', 'active');

            // Switch to the specific sub-panel within the base tab
            if (subPanel) {
                if (baseTabId === 'voice' && typeof switchVoicePanel === 'function') {
                    setTimeout(function() { switchVoicePanel(subPanel); }, 50);
                } else if (baseTabId === 'config' && typeof switchConfigPanel === 'function') {
                    setTimeout(function() { switchConfigPanel(subPanel); }, 50);
                } else if (baseTabId === 'connect' && typeof switchConnectPanel === 'function') {
                    setTimeout(function() { switchConnectPanel(subPanel); }, 50);
                }
            }

            if (baseTabId === 'logs' || tabId === 'logs') loadLogs();
            if ((baseTabId === 'whitelabel' || tabId === 'whitelabel') && typeof wlInit === 'function') wlInit();
            if ((baseTabId === 'agency-members' || tabId === 'agency-members') && typeof agencyLoadMembers === 'function') agencyLoadMembers();
            if ((baseTabId === 'agency-kpis' || tabId === 'agency-kpis') && typeof agencyLoadKpis === 'function') agencyLoadKpis('today');
            // Business Profile tab — always refresh status from Twilio on nav
            if ((baseTabId === 'business-profile' || tabId === 'business-profile') && typeof loadSpamProtectionStatus === 'function') {
                loadSpamProtectionStatus();
            }
        }

        // Legacy compat — kept for any code that still calls showSidebarTab() or navTo()
        function showSidebarTab(tabId) {
            const btnId = _tabToBtn[tabId];
            const btnEl = btnId ? document.getElementById(btnId) : null;
            sidebarNavigate(tabId, btnEl);
        }
        function navTo(tabId) { showSidebarTab(tabId); }

        // Sidebar collapse toggle — icon flips to show expand/collapse direction
        function toggleSidebar() {
            const sb = document.getElementById('mainSidebar');
            const icon = document.getElementById('sbToggleIcon');
            sb.classList.toggle('collapsed');
            const isCollapsed = sb.classList.contains('collapsed');
            document.body.classList.toggle('sidebar-collapsed', isCollapsed);
            // Collapsed → chevron-right (click to expand); Expanded → bars (click to collapse)
            if (icon) icon.className = isCollapsed ? 'fa-solid fa-chevron-right' : 'fa-solid fa-bars';
            document.getElementById('sbToggle').title = isCollapsed ? 'Expand sidebar' : 'Collapse sidebar';
            _lsSet('sidebar_collapsed', isCollapsed ? '1' : '0');
        }

        // ── Accordion: L1 sections (one open at a time) ──
        function toggleL1(el) {
            const body = el.nextElementSibling;
            const wasOpen = el.classList.contains('open');
            document.querySelectorAll('.l1-acc').forEach(function(a) {
                a.classList.remove('open');
                if (a.nextElementSibling) a.nextElementSibling.classList.remove('open');
            });
            document.querySelectorAll('.l1-direct').forEach(function(d) { d.classList.remove('active'); });
            if (!wasOpen) { el.classList.add('open'); body.classList.add('open'); }
        }

        // ── Accordion: L2 sub-sections (one open per L1) ──
        function toggleL2(el) {
            const body = el.nextElementSibling;
            const wasOpen = el.classList.contains('open');
            const parent = el.closest('.l1-inner');
            if (parent) {
                parent.querySelectorAll('.l2-acc').forEach(function(a) {
                    a.classList.remove('open');
                    if (a.nextElementSibling) a.nextElementSibling.classList.remove('open');
                });
            }
            var dialerSub = document.getElementById('dialerSub');
            var dialerArrow = document.getElementById('dialerArrow');
            if (dialerSub) dialerSub.style.maxHeight = '0';
            if (dialerArrow) dialerArrow.style.transform = '';
            if (!wasOpen) { el.classList.add('open'); body.classList.add('open'); }
        }

        // ── Toggle Dialer Settings sub-list (L3 item that expands to L4) ──
        function toggleDialerSub(el) {
            activateL3(el);
            var sub = document.getElementById('dialerSub');
            var arr = document.getElementById('dialerArrow');
            if (!sub) return;
            var isOpen = sub.style.maxHeight !== '0px' && sub.style.maxHeight !== '';
            sub.style.maxHeight = isOpen ? '0' : '120px';
            if (arr) arr.style.transform = isOpen ? '' : 'rotate(90deg)';
        }

        // ── Active state helpers ──
        function activateDirect(el) {
            document.querySelectorAll('.l1-acc').forEach(function(a) {
                a.classList.remove('open');
                if (a.nextElementSibling) a.nextElementSibling.classList.remove('open');
            });
            document.querySelectorAll('.l1-direct').forEach(function(d) { d.classList.remove('active'); });
            el.classList.add('active');
        }
        function activateL2(el) {
            document.querySelectorAll('.l2-item').forEach(function(i) { i.classList.remove('active'); });
            el.classList.add('active');
        }
        function activateL3(el) {
            document.querySelectorAll('.l3-item').forEach(function(i) { i.classList.remove('active'); });
            el.classList.add('active');
        }
        function activateL4(el) {
            document.querySelectorAll('.l4-item').forEach(function(i) { i.classList.remove('active'); });
            el.classList.add('active');
        }

        // ── Nav helpers: navigate to tab + switch internal sub-panel ──
        function navToSms(panel) {
            sidebarNavigate('config', null);
            if (typeof switchConfigPanel === 'function') {
                setTimeout(function() { switchConfigPanel(panel); }, 50);
            }
        }
        function navToVoice(panel) {
            sidebarNavigate('voice', null);
            if (typeof switchVoicePanel === 'function') {
                setTimeout(function() { switchVoicePanel(panel); }, 50);
            }
        }
        function navToConnect(panel) {
            sidebarNavigate('connect', null);
            if (typeof switchConnectPanel === 'function') {
                setTimeout(function() { switchConnectPanel(panel); }, 50);
            }
        }

        // Restore sidebar + theme state on page load
        document.addEventListener('DOMContentLoaded', function() {
            if (_lsGet('sidebar_collapsed') === '1') {
                const sb = document.getElementById('mainSidebar');
                if (sb) sb.classList.add('collapsed');
                document.body.classList.add('sidebar-collapsed');
                const icon = document.getElementById('sbToggleIcon');
                if (icon) icon.className = 'fa-solid fa-chevron-right';
                const btn = document.getElementById('sbToggle');
                if (btn) btn.title = 'Expand sidebar';
            }
            // Restore light/dark theme
            const savedTheme = _lsGet('dash_theme') || 'light';
            applyTheme(savedTheme, false);
        });

        // ── Light / Dark Theme ──
        function applyTheme(theme, save) {
            const html = document.documentElement;
            const btn = document.getElementById('themeToggleBtn');
            html.setAttribute('data-theme', theme);
            // Keep body class for any legacy selectors that weren't caught by the migration
            if (theme === 'light') {
                document.body.classList.add('light-theme');
                if (btn) { btn.innerHTML = '<i class="fa-solid fa-moon"></i>'; btn.title = 'Switch to dark mode'; }
            } else {
                document.body.classList.remove('light-theme');
                if (btn) { btn.innerHTML = '<i class="fa-solid fa-sun"></i>'; btn.title = 'Switch to light mode'; }
            }
            if (save) _lsSet('dash_theme', theme);
        }
        function toggleTheme() {
            const current = document.documentElement.getAttribute('data-theme') || 'light';
            applyTheme(current === 'light' ? 'dark' : 'light', true);
        }

        // ── Dialer settings panel toggle ──
        function toggleDialerSettings() {
            // Settings have moved to Voice Config > Dialer tab
            navTo('voice');
            setTimeout(() => switchVoicePanel('dialer'), 100);
        }

        // ── Voice Config column menu switching ──
        function switchVoicePanel(name) {
            // Backward compatibility: map old panel names to new consolidated names
            if (name === 'enable') name = 'settings';
            if (name === 'trusthub' || name === 'cnam' || name === 'numberintegrity' || name === 'numberhealth') name = 'spammonitoring';

            const panels = ['activation', 'settings', 'numbers', 'dialer', 'spammonitoring'];
            panels.forEach(p => {
                const panel = document.getElementById('vstab-panel-' + p);
                const menuBtn = document.getElementById('vmenu-' + p);
                const active = p === name;
                if (panel) panel.style.display = active ? 'block' : 'none';
                if (menuBtn) {
                    // Toggle active class for mobile CSS !important rules
                    if (active) { menuBtn.classList.add('active'); } else { menuBtn.classList.remove('active'); }
                    menuBtn.style.background = active ? _tc.cyanBg : 'transparent';
                    menuBtn.style.border = active ? '1px solid ' + _themeColor('rgba(0,217,255,0.15)', 'rgba(0,120,184,0.15)') : '1px solid transparent';
                    menuBtn.style.color = active ? _tc.cyan : _tc.textMut;
                }
            });
            if (name === 'numbers') loadNumbersTab();
            if (name === 'spammonitoring') {
                loadTrustHubData();
                if (typeof loadCnamMonitor === 'function') loadCnamMonitor();
                if (typeof loadNumberIntegrity === 'function') loadNumberIntegrity();
                if (typeof loadNumberHealth === 'function') loadNumberHealth();
            }
        }

        function copyToClipboard(text) {
            navigator.clipboard.writeText(text).then(() => {
                const el = document.activeElement;
                const originalHTML = el.innerHTML;
                el.innerHTML = '<i class="fa-solid fa-check"></i>';
                setTimeout(() => el.innerHTML = originalHTML, 1500);
            });
        }

        function saveProfile() {
            const name = document.getElementById('user_name').value;
            const phone = document.getElementById('phone').value;
            const bio = document.getElementById('bio').value;
            const btn = document.querySelector('button[onclick="saveProfile()"]');
            const originalText = btn.innerHTML;
            btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Saving...';

            fetch('/save-profile', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({name, phone, bio})
            }).then(r => r.json()).then(d => {
                btn.innerHTML = '<i class="fa-solid fa-check"></i> Saved!';
                setTimeout(() => btn.innerHTML = originalText, 2000);
            }).catch(e => { btn.innerHTML = 'Error'; });
        }

        function loadCalendars() {
            const btn = event.target.closest('button');
            const originalHTML = btn.innerHTML;
            btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';
            btn.disabled = true;

            fetch('/api/fetch-calendars')
                .then(r => r.json())
                .then(data => {
                    const select = document.getElementById('calendar_select');
                    const currentValue = select.value;

                    // Clear existing options
                    select.innerHTML = '<option value="">-- Select Calendar --</option>';

                    if (data.calendars && data.calendars.length > 0) {
                        data.calendars.forEach(cal => {
                            const option = document.createElement('option');
                            option.value = cal.id;
                            option.textContent = cal.name;
                            option.setAttribute('data-name', cal.name);
                            if (cal.id === currentValue) {
                                option.selected = true;
                            }
                            select.appendChild(option);
                        });
                        btn.innerHTML = '<i class="fa-solid fa-check"></i> Loaded';
                    } else {
                        btn.innerHTML = '<i class="fa-solid fa-exclamation"></i> None';
                    }

                    setTimeout(() => {
                        btn.innerHTML = originalHTML;
                        btn.disabled = false;
                    }, 2000);
                })
                .catch(e => {
                    console.error('Failed to load calendars:', e);
                    btn.innerHTML = '<i class="fa-solid fa-times"></i> Error';
                    setTimeout(() => {
                        btn.innerHTML = originalHTML;
                        btn.disabled = false;
                    }, 2000);
                });
        }

        // Workflow tab switching
        function switchWorkflow(id) {
            document.querySelectorAll('.workflow-tab-btn').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.workflow-content').forEach(c => c.classList.remove('active'));
            event.target.closest('.workflow-tab-btn').classList.add('active');
            document.getElementById('wf-' + id).classList.add('active');
            // Hide video walkthrough on Smart Tags tab (not relevant)
            var videoWrap = document.getElementById('wf-video-wrapper');
            if (videoWrap) videoWrap.style.display = (id === 'smarttags') ? 'none' : '';
        }

        // ═══════════════════════════════════════════════
        // MOBILE APP-LIKE NAVIGATION
        // ═══════════════════════════════════════════════

        // ── Bottom nav — switches dashboard tabs ──
        function mobileNav(tabId, btnEl) {
            // Update bottom nav active state
            document.querySelectorAll('.mobile-nav-btn').forEach(b => b.classList.remove('active'));
            if (btnEl) btnEl.classList.add('active');
            // Reuse existing sidebar navigate
            var sidebarBtnId = _tabToBtn[tabId];
            var sidebarBtn = sidebarBtnId ? document.getElementById(sidebarBtnId) : null;
            sidebarNavigate(tabId, sidebarBtn);
            // Close more sheet if open
            mobileCloseMoreSheet();
        }

        // ── More sheet ──
        function mobileOpenMoreSheet() {
            var sheet = document.getElementById('mobileMoreSheet');
            var backdrop = document.getElementById('mobileMoreBackdrop');
            if (sheet) { sheet.style.display = 'block'; requestAnimationFrame(function() { sheet.classList.add('open'); }); }
            if (backdrop) backdrop.classList.add('open');
        }
        function mobileCloseMoreSheet() {
            var sheet = document.getElementById('mobileMoreSheet');
            var backdrop = document.getElementById('mobileMoreBackdrop');
            if (sheet) { sheet.classList.remove('open'); setTimeout(function() { if (!sheet.classList.contains('open')) sheet.style.display = 'none'; }, 300); }
            if (backdrop) backdrop.classList.remove('open');
        }

        // ── Sidebar drawer (full sidebar, slide from left) ──
        function mobileOpenSidebar() {
            var sb = document.getElementById('mainSidebar');
            var backdrop = document.getElementById('mobileSidebarBackdrop');
            if (sb) sb.classList.add('mobile-open');
            if (backdrop) backdrop.classList.add('open');
        }
        function mobileCloseSidebar() {
            var sb = document.getElementById('mainSidebar');
            var backdrop = document.getElementById('mobileSidebarBackdrop');
            if (sb) sb.classList.remove('mobile-open');
            if (backdrop) backdrop.classList.remove('open');
        }

        // ── Dialer mobile tab switching ──
        function dlrMobileSwitch(panel) {
            // Update action bar buttons
            document.querySelectorAll('.dlr-mobile-action-btn').forEach(function(b) { b.classList.remove('active'); });
            var btnMap = { contacts: 'dlrMobBtnContacts', intel: 'dlrMobBtnIntel' };
            var activeBtn = document.getElementById(btnMap[panel]);
            if (activeBtn) activeBtn.classList.add('active');
            // Hide all columns
            var cols = document.querySelectorAll('#dlr3colLayout > .dlr-col, #dlr3colLayout > div');
            cols.forEach(function(c) { c.classList.remove('dlr-mobile-active'); });
            // Show the target
            var map = { contacts: 'dlrColContacts', intel: 'dlrColIntel' };
            var target = document.getElementById(map[panel]);
            if (target) target.classList.add('dlr-mobile-active');
            // Close apps popup if open
            dlrMobileCloseApps();
        }

        // ── Mobile: open an app in a full-screen overlay on mobile ──
        function dlrMobileOpenApp(appName) {
            dlrMobileCloseApps();
            if (typeof iosOpenApp === 'function') iosOpenApp(appName);
            // Show the phone column (it hosts the app views)
            var phoneCol = document.getElementById('dlrColPhone');
            if (phoneCol && window.innerWidth <= 768) {
                // Temporarily show phone col as full-screen overlay
                // Use setProperty with !important to override the CSS display:none !important rule
                phoneCol.style.setProperty('display', 'flex', 'important');
                phoneCol.style.position = 'fixed';
                phoneCol.style.inset = '0';
                phoneCol.style.zIndex = '400';
                phoneCol.style.background = '#000';
                phoneCol.style.width = '100%';
                phoneCol.style.height = '100%';
                phoneCol.style.borderRadius = '0';
                // Hide the iPhone frame chrome on mobile — just show the app content
                var frame = phoneCol.querySelector('.iphone-frame');
                if (frame) {
                    frame.style.maxWidth = '100%';
                    frame.style.width = '100%';
                    frame.style.height = '100%';
                    frame.style.aspectRatio = 'unset';
                }
                var bezel = phoneCol.querySelector('.iphone-bezel');
                if (bezel) {
                    bezel.style.borderRadius = '0';
                    bezel.style.padding = '0';
                    bezel.style.background = '#000';
                    bezel.style.boxShadow = 'none';
                }
                var screen = phoneCol.querySelector('.iphone-screen');
                if (screen) { screen.style.borderRadius = '0'; }
                // Hide bezel buttons
                phoneCol.querySelectorAll('.iphone-btn-right,.iphone-btn-left1,.iphone-btn-left2').forEach(function(b) { b.style.display = 'none'; });
                // Hide status bar and dynamic island
                var statusBar = phoneCol.querySelector('.ios-status-bar');
                if (statusBar) statusBar.style.display = 'none';
            }
        }

        // ── Mobile: close full-screen app overlay ──
        function dlrMobileCloseApp() {
            var phoneCol = document.getElementById('dlrColPhone');
            if (phoneCol && window.innerWidth <= 768) {
                phoneCol.style.removeProperty('display');
                phoneCol.style.position = '';
                phoneCol.style.inset = '';
                phoneCol.style.zIndex = '';
                phoneCol.style.background = '';
                phoneCol.style.width = '';
                phoneCol.style.height = '';
                phoneCol.style.borderRadius = '';
                // Restore iPhone frame
                var frame = phoneCol.querySelector('.iphone-frame');
                if (frame) { frame.style.maxWidth = ''; frame.style.width = ''; frame.style.height = ''; frame.style.aspectRatio = ''; }
                var bezel = phoneCol.querySelector('.iphone-bezel');
                if (bezel) { bezel.style.borderRadius = ''; bezel.style.padding = ''; bezel.style.background = ''; bezel.style.boxShadow = ''; }
                var screen = phoneCol.querySelector('.iphone-screen');
                if (screen) { screen.style.borderRadius = ''; }
                phoneCol.querySelectorAll('.iphone-btn-right,.iphone-btn-left1,.iphone-btn-left2').forEach(function(b) { b.style.display = ''; });
                var statusBar = phoneCol.querySelector('.ios-status-bar');
                if (statusBar) statusBar.style.display = '';
            }
            if (typeof iosGoHome === 'function') iosGoHome();
        }

        // ── Mobile apps popup ──
        function dlrMobileToggleApps() {
            var popup = document.getElementById('dlrMobileAppsPopup');
            var overlay = document.getElementById('dlrMobileAppsOverlay');
            if (popup && overlay) {
                var isOpen = popup.classList.contains('open');
                popup.classList.toggle('open', !isOpen);
                overlay.classList.toggle('open', !isOpen);
            }
        }
        function dlrMobileCloseApps() {
            var popup = document.getElementById('dlrMobileAppsPopup');
            var overlay = document.getElementById('dlrMobileAppsOverlay');
            if (popup) popup.classList.remove('open');
            if (overlay) overlay.classList.remove('open');
        }

        // ── Auto-detect mobile and show/hide elements ──
        function _setupMobileLayout() {
            var isMobile = window.innerWidth <= 768;
            var mobileActionBar = document.getElementById('dlrMobileActionBar');
            var bottomNav = document.getElementById('mobileBottomNav');
            if (mobileActionBar) mobileActionBar.style.display = isMobile ? 'flex' : 'none';
            // On mobile, make sure the active column is shown
            if (isMobile) {
                var contactsCol = document.getElementById('dlrColContacts');
                if (contactsCol && !document.querySelector('.dlr-col.dlr-mobile-active')) {
                    contactsCol.classList.add('dlr-mobile-active');
                }
            } else {
                // On desktop, show all columns
                document.querySelectorAll('#dlr3colLayout > .dlr-col, #dlr3colLayout > div').forEach(function(c) {
                    c.style.display = '';
                    c.classList.remove('dlr-mobile-active');
                });
                // Close mobile sidebar if resizing to desktop
                mobileCloseSidebar();
                mobileCloseMoreSheet();
                dlrMobileCloseApps();
            }
        }
        window.addEventListener('resize', _setupMobileLayout);

        // Update calendar_name when selection changes
        document.addEventListener('DOMContentLoaded', function() {
            const calendarSelect = document.getElementById('calendar_select');
            const calendarNameInput = document.getElementById('calendar_name');

            if (calendarSelect && calendarNameInput) {
                calendarSelect.addEventListener('change', function() {
                    const selectedOption = this.options[this.selectedIndex];
                    if (selectedOption && selectedOption.value) {
                        calendarNameInput.value = selectedOption.getAttribute('data-name') || selectedOption.textContent;
                    } else {
                        calendarNameInput.value = '';
                    }
                });
            }

            // Auto-load logs when Logs tab is shown
            const logsTab = document.querySelector('[data-bs-target="#logs"]');
            if (logsTab) {
                logsTab.addEventListener('shown.bs.tab', () => loadLogs());
            }

            // Init mobile layout
            _setupMobileLayout();

            // Poll carrier verification status on dashboard load
            var _boot = window.DASHBOARD_BOOT || {};
            if (_boot.trustHubProfileSid && _boot.trustHubReviewStatus !== 'twilio-approved') {
                fetch('/api/trust-hub-status')
                    .then(function (r) { return r.json(); })
                    .then(function (data) {
                        if (data.status === 'twilio-approved' && typeof _showDashToast === 'function') {
                            _showDashToast(true, 'Carrier verification approved. Caller ID is now active.', 6000);
                        }
                    })
                    .catch(function () { /* silent — non-critical */ });
            }
        });

