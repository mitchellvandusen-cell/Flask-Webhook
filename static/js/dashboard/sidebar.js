        // ══════════════════════════════════
        // SIDEBAR NAVIGATION (2026)
        // ══════════════════════════════════

        // Safe localStorage wrapper — cross-origin iframes can block storage access
        function _lsGet(k) { try { return localStorage.getItem(k); } catch(e) { return null; } }
        function _lsSet(k,v) { try { localStorage.setItem(k,v); } catch(e) {} }

        // Page title map
        const _pageTitles = {
            voicedialer: 'Dialer', config: 'SMS Config', voice: 'Voice Config',
            workflows: 'Workflows', connect: 'Connect CRM',
            carriers: 'Carriers', advanced: 'Advanced Settings', aiminutes: 'AI Minutes',
            billing: 'Billing', logs: 'Activity Logs',
        };

        // Map of tab IDs to sidebar nav button IDs
        const _tabToBtn = {
            voicedialer: 'sbnDialer', config: 'sbnSmsConfig', voice: 'sbnVoiceConfig',
            workflows: 'sbnWorkflows', connect: 'sbnConnect',
            carriers: 'sbnCarriers', advanced: 'sbnAdvanced', aiminutes: 'sbnAiMinutes',
            billing: 'sbnBilling', logs: 'sbnLogs',
        };

        function sidebarNavigate(tabId, btnEl) {
            // Update active state on all sidebar nav items
            document.querySelectorAll('.sb-nav-item').forEach(b => b.classList.remove('active'));
            if (btnEl) btnEl.classList.add('active');

            // Update page title
            const titleEl = document.getElementById('dashPageTitle');
            if (titleEl) titleEl.textContent = _pageTitles[tabId] || 'Dashboard';

            // Unified: hide ALL panes first, then show only the target.
            // This prevents stacking when switching between Bootstrap-managed and
            // manually-managed panes. We bypass Bootstrap's tab JS entirely since
            // the nav is hidden — we own the show/active classes directly.
            document.querySelectorAll('#dashTabsContent > .tab-pane').forEach(p => {
                p.classList.remove('show', 'active');
            });
            const pane = document.getElementById(tabId);
            if (pane) pane.classList.add('show', 'active');

            if (tabId === 'logs') loadLogs();
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

        // Collapsible section toggle
        function toggleSbSection(sectionId) {
            const section = document.getElementById(sectionId);
            if (section) section.classList.toggle('open');
        }

        // Collapsible system params toggle
        function toggleSbParams() {
            const body = document.getElementById('sbParamsBody');
            const chevron = document.getElementById('sbParamsChevron');
            if (body) body.classList.toggle('open');
            if (chevron) chevron.style.transform = body && body.classList.contains('open') ? 'rotate(180deg)' : '';
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
            const savedTheme = _lsGet('dash_theme') || 'dark';
            applyTheme(savedTheme, false);
        });

        // ── Light / Dark Theme ──
        function applyTheme(theme, save) {
            const body = document.body;
            const btn = document.getElementById('themeToggleBtn');
            if (theme === 'light') {
                body.classList.add('light-theme');
                if (btn) { btn.innerHTML = '<i class="fa-solid fa-moon"></i>'; btn.title = 'Switch to dark mode'; }
            } else {
                body.classList.remove('light-theme');
                if (btn) { btn.innerHTML = '<i class="fa-solid fa-sun"></i>'; btn.title = 'Switch to light mode'; }
            }
            if (save) _lsSet('dash_theme', theme);
        }
        function toggleTheme() {
            const isLight = document.body.classList.contains('light-theme');
            applyTheme(isLight ? 'dark' : 'light', true);
        }

        // ── Dialer settings panel toggle ──
        function toggleDialerSettings() {
            // Settings have moved to Voice Config > Dialer tab
            navTo('voice');
            setTimeout(() => switchVoicePanel('dialer'), 100);
        }

        // ── Voice Config column menu switching ──
        function switchVoicePanel(name) {
            const panels = ['activation', 'enable', 'settings', 'numbers', 'dialer', 'numberhealth', 'trusthub', 'cnam', 'numberintegrity', 'training'];
            panels.forEach(p => {
                const panel = document.getElementById('vstab-panel-' + p);
                const menuBtn = document.getElementById('vmenu-' + p);
                const active = p === name;
                if (panel) panel.style.display = active ? 'block' : 'none';
                if (menuBtn) {
                    // Toggle active class for mobile CSS !important rules
                    if (active) { menuBtn.classList.add('active'); } else { menuBtn.classList.remove('active'); }
                    menuBtn.style.background = active ? 'rgba(0,217,255,0.08)' : 'transparent';
                    menuBtn.style.border = active ? '1px solid rgba(0,217,255,0.15)' : '1px solid transparent';
                    menuBtn.style.color = active ? '#00d9ff' : '#888';
                }
            });
            if (name === 'numbers') loadNumbersTab();
            if (name === 'numberhealth') loadNumberHealth();
            if (name === 'trusthub') loadTrustHubData();
            if (name === 'cnam' && typeof loadCnamMonitor === 'function') loadCnamMonitor();
            if (name === 'numberintegrity' && typeof loadNumberIntegrity === 'function') loadNumberIntegrity();
            if (name === 'training') loadTrainingStatus();
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
        });

