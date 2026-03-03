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

        // Legacy compat — kept for any code that still calls showSidebarTab()
        function showSidebarTab(tabId) {
            const btnId = _tabToBtn[tabId];
            const btnEl = btnId ? document.getElementById(btnId) : null;
            sidebarNavigate(tabId, btnEl);
        }

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
            const panel = document.getElementById('dialerSettingsPanel');
            const btn = document.getElementById('dialerSettingsToggle');
            if (panel.style.display === 'none') {
                panel.style.display = 'block';
                btn.style.background = 'rgba(0,217,255,0.12)';
                btn.style.borderColor = 'rgba(0,217,255,0.3)';
                btn.style.color = '#00d9ff';
            } else {
                panel.style.display = 'none';
                btn.style.background = 'rgba(255,255,255,0.04)';
                btn.style.borderColor = 'rgba(255,255,255,0.08)';
                btn.style.color = '#aaa';
            }
        }

        // ── Voice Config column menu switching ──
        function switchVoicePanel(name) {
            const panels = ['settings', 'activation', 'numbers', 'trusthub', 'a2p', 'training'];
            panels.forEach(p => {
                const panel = document.getElementById('vstab-panel-' + p);
                const menuBtn = document.getElementById('vmenu-' + p);
                const active = p === name;
                if (panel) panel.style.display = active ? 'block' : 'none';
                if (menuBtn) {
                    menuBtn.style.background = active ? 'rgba(0,217,255,0.08)' : 'transparent';
                    menuBtn.style.border = active ? '1px solid rgba(0,217,255,0.15)' : '1px solid transparent';
                    menuBtn.style.color = active ? '#00d9ff' : '#888';
                }
            });
            if (name === 'numbers') loadNumbersTab();
            if (name === 'trusthub') loadTrustHubData();
            if (name === 'a2p') a2pLoadStatus();
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
        });

