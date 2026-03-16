// workflows.js — Visual Workflow Builder Canvas
// Node-based drag-and-drop editor with SVG connections, AI builder, and premium animations.

(function() {
    'use strict';

    // ── State ────────────────────────────────────────────────────────────────
    var _workflows = [];
    var _ghlWorkflows = [];
    var _ghlLoaded = false;
    var _currentWorkflow = null;
    var _nodes = new Map();
    var _connections = [];
    var _selectedNode = null;
    var _draggingNode = null;
    var _dragOffsetX = 0, _dragOffsetY = 0;
    var _drawingConn = null;
    var _customActions = [];
    var _zoom = 1;
    var _panX = 0, _panY = 0;
    var _isPanning = false;
    var _panStartX = 0, _panStartY = 0;
    var _gridSize = 20;
    var _saveTimer = null;
    var _undoStack = [];
    var _initDone = false;

    // ── Node Definitions ────────────────────────────────────────────────────
    var NODE_DEFS = {
        contact_created:     { label: 'Contact Created',     icon: 'fa-solid fa-user-plus',        category: 'trigger' },
        sms_received:        { label: 'SMS Received',        icon: 'fa-solid fa-comment-sms',      category: 'trigger' },
        inbound_call:        { label: 'Inbound Call',        icon: 'fa-solid fa-phone',            category: 'trigger' },
        missed_call:         { label: 'Missed Call',         icon: 'fa-solid fa-phone-slash',      category: 'trigger' },
        voicemail_received:  { label: 'Voicemail',           icon: 'fa-solid fa-voicemail',        category: 'trigger' },
        tag_added:           { label: 'Tag Added',           icon: 'fa-solid fa-tag',              category: 'trigger' },
        tag_removed:         { label: 'Tag Removed',         icon: 'fa-solid fa-tags',             category: 'trigger' },
        lead_age:            { label: 'Lead Age',            icon: 'fa-solid fa-hourglass-half',   category: 'trigger' },
        no_response:         { label: 'No Response',         icon: 'fa-solid fa-clock',            category: 'trigger' },
        scheduled:           { label: 'Scheduled',           icon: 'fa-solid fa-calendar-days',    category: 'trigger' },
        manual:              { label: 'Manual Trigger',      icon: 'fa-solid fa-hand-pointer',     category: 'trigger' },
        birthday_approaching:{ label: 'Birthday',            icon: 'fa-solid fa-cake-candles',     category: 'trigger' },
        field_updated:       { label: 'Field Updated',       icon: 'fa-solid fa-pen',              category: 'trigger' },
        contact_dnd:         { label: 'Opted Out',           icon: 'fa-solid fa-ban',              category: 'trigger' },
        appointment_booked:  { label: 'Appointment Booked',  icon: 'fa-solid fa-calendar-check',   category: 'trigger' },
        appointment_noshow:  { label: 'No-Show',             icon: 'fa-solid fa-calendar-xmark',   category: 'trigger' },
        stage_changed:       { label: 'Stage Changed',       icon: 'fa-solid fa-chart-simple',     category: 'trigger' },
        send_sms:            { label: 'Send SMS',            icon: 'fa-solid fa-message',          category: 'action' },
        ai_call:             { label: 'AI Call',             icon: 'fa-solid fa-phone-volume',     category: 'action' },
        add_tag:             { label: 'Add Tag',             icon: 'fa-solid fa-tag',              category: 'action' },
        remove_tag:          { label: 'Remove Tag',          icon: 'fa-solid fa-tags',             category: 'action' },
        update_field:        { label: 'Update Field',        icon: 'fa-solid fa-pen-to-square',    category: 'action' },
        add_note:            { label: 'Add Note',            icon: 'fa-solid fa-note-sticky',      category: 'action' },
        send_igb_message:    { label: 'IGB AI Message',       icon: 'fa-solid fa-robot',            category: 'action' },
        send_webhook:        { label: 'Send Webhook',        icon: 'fa-solid fa-globe',            category: 'action' },
        assign_agent:        { label: 'Assign Agent',        icon: 'fa-solid fa-user-gear',        category: 'action' },
        move_stage:          { label: 'Move Stage',          icon: 'fa-solid fa-chart-simple',     category: 'coming_soon' },
        if_else:             { label: 'If / Then',           icon: 'fa-solid fa-code-branch',      category: 'condition' },
        loop:                { label: 'Loop',                icon: 'fa-solid fa-repeat',           category: 'condition' },
        goto:                { label: 'Go To',               icon: 'fa-solid fa-arrow-turn-down',  category: 'logic' },
        wait:                { label: 'Wait / Delay',        icon: 'fa-solid fa-clock',            category: 'delay' },
        wait_until:          { label: 'Wait Until',          icon: 'fa-solid fa-hourglass-end',    category: 'delay' },
        state_query:         { label: 'Query State',         icon: 'fa-solid fa-database',         category: 'logic' },
        exit:                { label: 'Exit',                icon: 'fa-solid fa-right-from-bracket',category: 'exit' },
        custom:              { label: 'Custom Action',       icon: 'fa-solid fa-puzzle-piece',     category: 'action' }
    };

    // ── Helpers ──────────────────────────────────────────────────────────────
    function _esc(s) { var d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }
    function _genId() { return 'n_' + Date.now() + '_' + Math.random().toString(36).substr(2, 6); }
    function _snap(v) { return Math.round(v / _gridSize) * _gridSize; }
    function _getDef(subtype) { return NODE_DEFS[subtype] || NODE_DEFS.custom; }
    function _getIcon(subtype) { return _getDef(subtype).icon; }
    function _getLabel(subtype) { return _getDef(subtype).label; }
    function _getCat(subtype) { return _getDef(subtype).category; }

    function _getSummary(node) {
        var c = node.config || {};
        var st = node.subtype;
        if (st === 'send_sms') return c.message ? c.message.substring(0, 40) + (c.message.length > 40 ? '...' : '') : 'Configure message';
        if (st === 'send_igb_message') return c.mode === 'manual' ? (c.manual_message ? c.manual_message.substring(0, 30) + '...' : 'Set message') : (c.prompt_hint ? 'AI: ' + c.prompt_hint.substring(0, 28) + '...' : 'AI auto-compose');
        if (st === 'ai_call') return c.voice_prompt ? 'Prompt set' : 'Configure prompt';
        if (st === 'add_tag' || st === 'remove_tag') return c.tag_name || c.tag || 'Set tag';
        if (st === 'wait') return (c.duration || '?') + ' ' + (c.unit || 'hours');
        if (st === 'wait_until') return c.condition ? (c.condition.field || 'condition') + ' (max ' + (c.max_wait_hours || 72) + 'h)' : 'Set condition';
        if (st === 'state_query') return c.query_type ? c.query_type.replace(/_/g, ' ') + ' → ' + (c.store_as || 'result') : 'Set query';
        if (st === 'if_else') return (c.conditions && c.conditions.length) ? c.conditions.length + ' condition(s)' : 'Set conditions';
        if (st === 'loop') return 'Max ' + (c.max_iterations || '?') + ' iterations';
        if (st === 'send_webhook') return c.url ? c.url.substring(0, 30) : 'Set URL';
        if (st === 'update_field') return c.field || c.field_key || 'Set field';
        if (st === 'add_note') return c.note || c.body ? (c.note || c.body).substring(0, 30) : 'Set note';
        if (st === 'assign_agent') return c.assigned_to || 'Set agent';
        if (st === 'move_stage') return c.pipeline_id ? 'Pipeline set' : 'Set pipeline';
        if (st === 'custom') return c.action_name || c.description ? (c.action_name || c.description).substring(0, 30) : 'Custom action';
        if (st === 'tag_added' || st === 'tag_removed') return c.tag_name || c.tag || '';
        if (st === 'scheduled') return c.cron || '';
        if (st === 'no_response') return (c.days || '3') + ' days';
        if (st === 'lead_age') return (c.days_since_import || '60') + ' days';
        if (st === 'birthday_approaching') return (c.days_before || '7') + ' days before';
        if (st === 'goto') return c.target_step_id ? 'Target set' : 'Set target';
        return '';
    }

    // ── View Switching ──────────────────────────────────────────────────────
    window.wfbShowList = function() {
        var lv = document.getElementById('wfbListView');
        var ev = document.getElementById('wfbEditorView');
        if (lv) lv.classList.remove('wfb-hidden');
        if (ev) ev.classList.add('wfb-hidden');
        _currentWorkflow = null;
        _closeConfigPanel();
        _loadWorkflows();
    };

    window.wfbShowEditor = function(workflowId) {
        var lv = document.getElementById('wfbListView');
        var ev = document.getElementById('wfbEditorView');
        if (lv) lv.classList.add('wfb-hidden');
        if (ev) ev.classList.remove('wfb-hidden');
        _loadWorkflow(workflowId);
    };

    // ── Workflow List ────────────────────────────────────────────────────────
    function _loadWorkflows() {
        fetch('/api/workflows').then(function(r) { return r.json(); }).then(function(data) {
            _workflows = data.workflows || [];
            _renderList();
        }).catch(function() {
            _workflows = [];
            _renderList();
        });
    }

    function _renderList() {
        var grid = document.getElementById('wfbGrid');
        var emptyState = document.getElementById('wfbEmptyState');
        if (!grid) return;

        if (!_workflows.length) {
            grid.innerHTML = '';
            if (emptyState) emptyState.style.display = '';
            return;
        }

        if (emptyState) emptyState.style.display = 'none';
        var html = '';
        _workflows.forEach(function(wf) {
            var trigDef = NODE_DEFS[wf.trigger_type] || NODE_DEFS.manual;
            var badge = wf.status === 'active' ? 'wfb-badge-active' : wf.status === 'paused' ? 'wfb-badge-paused' : 'wfb-badge-draft';
            var stats = wf.stats || {};
            html += '<div class="wfb-card" onclick="wfbShowEditor(\'' + _esc(wf.id) + '\')">' +
                '<button class="wfb-card-menu-btn" onclick="event.stopPropagation();wfbCardMenu(this,\'' + _esc(wf.id) + '\')"><i class="fa-solid fa-ellipsis-vertical"></i></button>' +
                '<div class="wfb-card-header">' +
                    '<div class="wfb-card-trigger-icon"><i class="' + trigDef.icon + '"></i></div>' +
                    '<div class="wfb-card-name-wrap">' +
                        '<div class="wfb-card-name">' + _esc(wf.name) + '</div>' +
                        '<div class="wfb-card-desc">' + _esc(wf.description || trigDef.label + ' trigger') + '</div>' +
                    '</div>' +
                    '<span class="wfb-badge ' + badge + '">' + _esc(wf.status) + '</span>' +
                '</div>' +
                '<div class="wfb-card-footer">' +
                    '<span class="wfb-card-stats">' + (stats.runs || 0) + ' runs</span>' +
                '</div>' +
            '</div>';
        });
        grid.innerHTML = html;

        // Re-render GHL section if loaded
        if (_ghlLoaded) _renderGhlWorkflows();
    }

    window.wfbCardMenu = function(btn, wfId) {
        var existing = document.querySelector('.wfb-card-menu');
        if (existing) { existing.remove(); return; }

        var dd = document.createElement('div');
        dd.className = 'wfb-card-menu';
        dd.innerHTML =
            '<button class="wfb-card-menu-item" onclick="event.stopPropagation();wfbDuplicateWf(\'' + wfId + '\')"><i class="fa-solid fa-copy"></i> Duplicate</button>' +
            '<button class="wfb-card-menu-item" onclick="event.stopPropagation();wfbToggleWf(\'' + wfId + '\')"><i class="fa-solid fa-power-off"></i> Toggle Status</button>' +
            '<button class="wfb-card-menu-item wfb-card-menu-item-danger" onclick="event.stopPropagation();wfbDeleteWf(\'' + wfId + '\')"><i class="fa-solid fa-trash"></i> Delete</button>';
        var card = btn.closest('.wfb-card');
        card.appendChild(dd);
        setTimeout(function() {
            document.addEventListener('click', function _cl() { dd.remove(); document.removeEventListener('click', _cl); }, { once: true });
        }, 10);
    };

    window.wfbNewWorkflow = function() {
        fetch('/api/workflows', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: 'New Workflow', trigger_type: 'manual' })
        }).then(function(r) { return r.json(); }).then(function(data) {
            if (data.workflow) wfbShowEditor(data.workflow.id);
        });
    };

    window.wfbDeleteWf = function(id) {
        if (!confirm('Delete this workflow?')) return;
        fetch('/api/workflows/' + id, { method: 'DELETE' }).then(function() { _loadWorkflows(); });
    };

    window.wfbDuplicateWf = function(id) {
        fetch('/api/workflows/' + id + '/duplicate', { method: 'POST' }).then(function() { _loadWorkflows(); });
    };

    window.wfbToggleWf = function(id) {
        var wf = _workflows.find(function(w) { return w.id === id; });
        if (!wf) return;
        var action = wf.status === 'active' ? 'pause' : 'activate';
        fetch('/api/workflows/' + id + '/' + action, { method: 'POST' }).then(function() { _loadWorkflows(); });
    };

    // ── LeadConnector Workflow Import ─────────────────────────────────────────
    window.wfbImportGhl = function() {
        var btn = document.getElementById('wfbImportGhlBtn');
        if (btn) { btn.disabled = true; btn.querySelector('span').textContent = 'Loading...'; }

        fetch('/api/workflows/ghl').then(function(r) { return r.json(); }).then(function(data) {
            if (btn) { btn.disabled = false; btn.querySelector('span').textContent = 'Import from LeadConnector'; }
            if (data.error) {
                if (typeof _showDashToast === 'function') _showDashToast(false, data.error);
                return;
            }
            _ghlWorkflows = data.workflows || [];
            _ghlLoaded = true;
            _renderGhlWorkflows();
            if (typeof _showDashToast === 'function') {
                _showDashToast(true, _ghlWorkflows.length + ' LeadConnector workflow' + (_ghlWorkflows.length !== 1 ? 's' : '') + ' found');
            }
        }).catch(function() {
            if (btn) { btn.disabled = false; btn.querySelector('span').textContent = 'Import from LeadConnector'; }
            if (typeof _showDashToast === 'function') _showDashToast(false, 'Failed to fetch LeadConnector workflows');
        });
    };

    window.wfbShowGhlDetail = function(wfId) {
        var wf = null;
        for (var i = 0; i < _ghlWorkflows.length; i++) {
            if (_ghlWorkflows[i].id === wfId) { wf = _ghlWorkflows[i]; break; }
        }
        if (!wf) return;

        var created = wf.created_at ? new Date(wf.created_at).toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' }) : 'Unknown';
        var updated = wf.updated_at ? new Date(wf.updated_at).toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' }) : 'Unknown';
        var status = wf.status === 'published' ? 'Active' : (wf.status || 'Draft');
        var statusClass = wf.status === 'published' ? 'wfb-badge-active' : 'wfb-badge-draft';

        var existing = document.getElementById('wfbGhlDetailModal');
        if (existing) existing.remove();

        var modal = document.createElement('div');
        modal.id = 'wfbGhlDetailModal';
        modal.className = 'wfb-ai-modal wfb-ai-modal-open';
        modal.innerHTML =
            '<div class="wfb-ai-modal-content wfb-ghl-detail-modal-content">' +
                '<div class="wfb-ai-modal-header">' +
                    '<div class="wfb-ai-modal-title-group">' +
                        '<div class="wfb-card-trigger-icon wfb-ghl-detail-icon"><i class="fa-solid fa-diagram-project"></i></div>' +
                        '<div>' +
                            '<h3 class="wfb-ai-modal-title">' + _esc(wf.name) + '</h3>' +
                            '<p class="wfb-ai-modal-subtitle">LeadConnector Workflow</p>' +
                        '</div>' +
                    '</div>' +
                    '<button class="wfb-ai-modal-close" onclick="document.getElementById(\'wfbGhlDetailModal\').remove()" type="button"><i class="fa-solid fa-xmark"></i></button>' +
                '</div>' +
                '<div class="wfb-ghl-detail-body">' +
                    '<div class="wfb-ghl-detail-row">' +
                        '<span class="wfb-ghl-detail-label">Status</span>' +
                        '<span class="wfb-badge ' + statusClass + '"><span class="wfb-badge-dot"></span> ' + _esc(status) + '</span>' +
                    '</div>' +
                    '<div class="wfb-ghl-detail-row">' +
                        '<span class="wfb-ghl-detail-label">Created</span>' +
                        '<span class="wfb-ghl-detail-value">' + _esc(created) + '</span>' +
                    '</div>' +
                    '<div class="wfb-ghl-detail-row">' +
                        '<span class="wfb-ghl-detail-label">Last Updated</span>' +
                        '<span class="wfb-ghl-detail-value">' + _esc(updated) + '</span>' +
                    '</div>' +
                    '<div class="wfb-ghl-detail-row">' +
                        '<span class="wfb-ghl-detail-label">Version</span>' +
                        '<span class="wfb-ghl-detail-value">v' + (wf.version || 1) + '</span>' +
                    '</div>' +
                    '<div class="wfb-ghl-detail-row">' +
                        '<span class="wfb-ghl-detail-label">Source</span>' +
                        '<span class="wfb-source-badge wfb-source-ghl">LeadConnector</span>' +
                    '</div>' +
                    '<div class="wfb-ghl-detail-note">' +
                        '<i class="fa-solid fa-circle-info"></i> ' +
                        'This workflow is managed in LeadConnector. To edit triggers, steps, and actions, open it in your LeadConnector dashboard.' +
                    '</div>' +
                '</div>' +
            '</div>';

        modal.addEventListener('click', function(e) {
            if (e.target === modal) modal.remove();
        });

        document.body.appendChild(modal);
    };

    function _renderGhlWorkflows() {
        var existing = document.getElementById('wfbGhlSection');
        if (existing) existing.remove();

        if (!_ghlWorkflows.length) return;

        var listView = document.getElementById('wfbListView');
        if (!listView) return;

        var section = document.createElement('div');
        section.className = 'wfb-ghl-section';
        section.id = 'wfbGhlSection';

        var header = '<div class="wfb-ghl-section-header">' +
            '<i class="fa-solid fa-cloud-arrow-down wfb-ghl-section-icon"></i>' +
            '<span class="wfb-ghl-section-title">LeadConnector Workflows</span>' +
            '<span class="wfb-ghl-section-count">' + _ghlWorkflows.length + ' workflow' + (_ghlWorkflows.length !== 1 ? 's' : '') + '</span>' +
        '</div>';

        var cards = '<div class="wfb-grid">';
        _ghlWorkflows.forEach(function(wf) {
            var statusClass = wf.status === 'published' ? 'wfb-badge-active' : 'wfb-badge-draft';
            var statusText = wf.status === 'published' ? 'active' : (wf.status || 'draft');
            var updated = wf.updated_at ? new Date(wf.updated_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) : '';
            cards += '<div class="wfb-card wfb-ghl-card" onclick="wfbShowGhlDetail(\'' + _esc(wf.id) + '\')">' +
                '<div class="wfb-card-header">' +
                    '<div class="wfb-card-trigger-icon"><i class="fa-solid fa-diagram-project"></i></div>' +
                    '<div class="wfb-card-name-wrap">' +
                        '<div class="wfb-card-name">' + _esc(wf.name) + '</div>' +
                        '<div class="wfb-card-desc"><span class="wfb-source-badge wfb-source-ghl">LeadConnector</span></div>' +
                    '</div>' +
                    '<span class="wfb-badge ' + statusClass + '">' + _esc(statusText) + '</span>' +
                '</div>' +
                '<div class="wfb-card-footer">' +
                    '<span class="wfb-card-stats wfb-ghl-readonly">Read-only · Managed in LeadConnector</span>' +
                    (updated ? '<span class="wfb-card-stats wfb-ghl-readonly">' + updated + '</span>' : '') +
                '</div>' +
            '</div>';
        });
        cards += '</div>';

        section.innerHTML = header + cards;
        listView.appendChild(section);
    }

    // ── Load Single Workflow ────────────────────────────────────────────────
    function _loadWorkflow(wfId) {
        fetch('/api/workflows/' + wfId).then(function(r) { return r.json(); }).then(function(data) {
            _currentWorkflow = data.workflow;
            _clearCanvas();

            // Set toolbar
            var nameEl = document.getElementById('wfbWorkflowName');
            if (nameEl) nameEl.value = _currentWorkflow.name || 'Untitled';
            var badgeEl = document.getElementById('wfbEditorBadge');
            if (badgeEl) {
                var bc = _currentWorkflow.status === 'active' ? 'wfb-badge-active' : _currentWorkflow.status === 'paused' ? 'wfb-badge-paused' : 'wfb-badge-draft';
                badgeEl.className = 'wfb-badge ' + bc;
                badgeEl.textContent = _currentWorkflow.status;
            }

            // Create trigger node
            var trigId = 'trigger_' + wfId;
            _addNode(_currentWorkflow.trigger_type, 400, 60, _currentWorkflow.trigger_config || {}, trigId, 'trigger');

            // Create step nodes
            var steps = data.steps || [];
            var stepMap = {};
            steps.forEach(function(s) {
                stepMap[s.id] = s;
                _addNode(s.step_subtype, s.position_x || 400, s.position_y || 200, s.config || {}, s.id, s.step_type);
            });

            // Create connections
            var conns = data.connections || [];
            conns.forEach(function(c) {
                _addConnection(c.from_step_id, c.to_step_id, c.branch_key || 'default', c.id);
            });

            _updateAllConnections();
            _updateEmptyState();
        });
    }

    // ── Canvas Transform ────────────────────────────────────────────────────
    function _updateCanvasTransform() {
        var canvas = document.getElementById('wfbCanvas');
        var svg = document.getElementById('wfbSvgLayer');
        if (canvas) canvas.style.transform = 'scale(' + _zoom + ') translate(' + _panX + 'px, ' + _panY + 'px)';
        if (svg) svg.style.transform = 'scale(' + _zoom + ') translate(' + _panX + 'px, ' + _panY + 'px)';
        var lvl = document.getElementById('wfbZoomLevel');
        if (lvl) lvl.textContent = Math.round(_zoom * 100) + '%';
    }

    window.wfbZoomIn = function() { _zoom = Math.min(2, _zoom + 0.1); _updateCanvasTransform(); };
    window.wfbZoomOut = function() { _zoom = Math.max(0.3, _zoom - 0.1); _updateCanvasTransform(); };
    window.wfbZoomFit = function() { _zoomToFit(true); };

    function _zoomToFit(animated) {
        if (!_nodes.size) return;
        var minX = 9999, minY = 9999, maxX = 0, maxY = 0;
        _nodes.forEach(function(n) {
            if (n.x < minX) minX = n.x;
            if (n.y < minY) minY = n.y;
            if (n.x + 220 > maxX) maxX = n.x + 220;
            if (n.y + 80 > maxY) maxY = n.y + 80;
        });
        var wrap = document.getElementById('wfbCanvasWrap');
        if (!wrap) return;
        var ww = wrap.clientWidth, wh = wrap.clientHeight - 56;
        var cw = maxX - minX + 100, ch = maxY - minY + 100;
        _zoom = Math.min(1.5, Math.max(0.3, Math.min(ww / cw, wh / ch)));
        _panX = -minX + 50;
        _panY = -minY + 50;
        if (animated) {
            var canvas = document.getElementById('wfbCanvas');
            var svg = document.getElementById('wfbSvgLayer');
            if (canvas) canvas.style.transition = 'transform 0.4s ease';
            if (svg) svg.style.transition = 'transform 0.4s ease';
            _updateCanvasTransform();
            setTimeout(function() {
                if (canvas) canvas.style.transition = '';
                if (svg) svg.style.transition = '';
            }, 450);
        } else {
            _updateCanvasTransform();
        }
    }

    // ── Node Creation ────────────────────────────────────────────────────────
    function _addNode(subtype, x, y, config, id, type) {
        id = id || _genId();
        type = type || (NODE_DEFS[subtype] ? _getCat(subtype) : 'action');
        if (type === 'trigger' || type === 'condition' || type === 'delay' || type === 'logic' || type === 'exit' || type === 'coming_soon') {
            // keep type
        } else {
            type = 'action';
        }
        var node = { id: id, type: type, subtype: subtype, config: config || {}, x: _snap(x), y: _snap(y), el: null };
        var el = _createNodeEl(node);
        node.el = el;
        _nodes.set(id, node);
        var canvas = document.getElementById('wfbCanvas');
        if (canvas) canvas.appendChild(el);
        return node;
    }

    function _createNodeEl(node) {
        var cat = _getCat(node.subtype);
        var el = document.createElement('div');
        el.className = 'wfb-node wfb-node-' + cat;
        el.setAttribute('data-node-id', node.id);
        el.style.transform = 'translate(' + node.x + 'px, ' + node.y + 'px)';

        // Input port (not for triggers)
        if (cat !== 'trigger') {
            var inp = document.createElement('div');
            inp.className = 'wfb-port wfb-port-input';
            el.appendChild(inp);
        }

        // Icon
        var ico = document.createElement('div');
        ico.className = 'wfb-node-icon';
        ico.innerHTML = '<i class="' + _getIcon(node.subtype) + '"></i>';
        el.appendChild(ico);

        // Content
        var cnt = document.createElement('div');
        cnt.className = 'wfb-node-content';
        cnt.innerHTML = '<div class="wfb-node-title">' + _esc(_getLabel(node.subtype)) + '</div>' +
                        '<div class="wfb-node-summary">' + _esc(_getSummary(node)) + '</div>';
        el.appendChild(cnt);

        // Output ports
        if (node.subtype === 'if_else') {
            var tp = document.createElement('div');
            tp.className = 'wfb-port wfb-port-output wfb-port-true';
            tp.setAttribute('data-branch', 'true');
            tp.innerHTML = '<span>Y</span>';
            var fp = document.createElement('div');
            fp.className = 'wfb-port wfb-port-output wfb-port-false';
            fp.setAttribute('data-branch', 'false');
            fp.innerHTML = '<span>N</span>';
            el.appendChild(tp);
            el.appendChild(fp);
        } else if (node.subtype === 'wait_until') {
            var mp = document.createElement('div');
            mp.className = 'wfb-port wfb-port-output wfb-port-true';
            mp.setAttribute('data-branch', 'condition_met');
            mp.innerHTML = '<span>Met</span>';
            var top = document.createElement('div');
            top.className = 'wfb-port wfb-port-output wfb-port-false';
            top.setAttribute('data-branch', 'timeout');
            top.innerHTML = '<span>T/O</span>';
            el.appendChild(mp);
            el.appendChild(top);
        } else if (node.subtype === 'loop') {
            var lp = document.createElement('div');
            lp.className = 'wfb-port wfb-port-output wfb-port-true';
            lp.setAttribute('data-branch', 'loop');
            lp.innerHTML = '<span>Loop</span>';
            var ep = document.createElement('div');
            ep.className = 'wfb-port wfb-port-output wfb-port-false';
            ep.setAttribute('data-branch', 'exit');
            ep.innerHTML = '<span>Exit</span>';
            el.appendChild(lp);
            el.appendChild(ep);
        } else if (cat !== 'exit') {
            var outp = document.createElement('div');
            outp.className = 'wfb-port wfb-port-output';
            el.appendChild(outp);
        }

        return el;
    }

    function _updateNodeSummary(nodeId) {
        var node = _nodes.get(nodeId);
        if (!node || !node.el) return;
        var sumEl = node.el.querySelector('.wfb-node-summary');
        if (sumEl) sumEl.textContent = _getSummary(node);
    }

    function _deleteNode(nodeId) {
        var node = _nodes.get(nodeId);
        if (!node) return;
        if (node.el && node.el.parentNode) node.el.parentNode.removeChild(node.el);
        _connections = _connections.filter(function(c) {
            if (c.from === nodeId || c.to === nodeId) {
                if (c.pathEl && c.pathEl.parentNode) c.pathEl.parentNode.removeChild(c.pathEl);
                return false;
            }
            return true;
        });
        _nodes.delete(nodeId);
        if (_selectedNode === nodeId) { _selectedNode = null; _closeConfigPanel(); }
        _markDirty();
        _updateEmptyState();
    }

    function _updateEmptyState() {
        var empty = document.getElementById('wfbCanvasEmpty');
        if (empty) empty.style.display = _nodes.size > 0 ? 'none' : '';
    }

    // ── Connections ──────────────────────────────────────────────────────────
    function _addConnection(fromId, toId, branch, connId) {
        branch = branch || 'default';
        connId = connId || _genId();
        var svg = document.getElementById('wfbSvgLayer');
        var path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        path.classList.add('wfb-connection');
        if (branch === 'true') {
            path.classList.add('wfb-connection-true');
            path.setAttribute('marker-end', 'url(#wfbArrowTrue)');
        } else if (branch === 'false') {
            path.classList.add('wfb-connection-false');
            path.setAttribute('marker-end', 'url(#wfbArrowFalse)');
        } else {
            path.setAttribute('marker-end', 'url(#wfbArrow)');
        }
        if (svg) svg.appendChild(path);
        var conn = { id: connId, from: fromId, to: toId, branch: branch, pathEl: path };
        _connections.push(conn);
        _updateConnectionPath(conn);
        return conn;
    }

    function _updateConnectionPath(conn) {
        var fromNode = _nodes.get(conn.from);
        var toNode = _nodes.get(conn.to);
        if (!fromNode || !toNode || !conn.pathEl) return;

        var fromEl = fromNode.el;
        var toEl = toNode.el;
        if (!fromEl || !toEl) return;

        // Find the output port
        var outPort;
        if (conn.branch === 'true') outPort = fromEl.querySelector('.wfb-port-true');
        else if (conn.branch === 'false') outPort = fromEl.querySelector('.wfb-port-false');
        else outPort = fromEl.querySelector('.wfb-port-output');
        var inPort = toEl.querySelector('.wfb-port-input');

        if (!outPort || !inPort) return;

        // Calculate positions relative to the canvas (no zoom/pan needed since SVG is also transformed)
        var x1 = fromNode.x + outPort.offsetLeft + 6;
        var y1 = fromNode.y + outPort.offsetTop + 6;
        var x2 = toNode.x + inPort.offsetLeft + 6;
        var y2 = toNode.y + inPort.offsetTop + 6;

        var dy = Math.abs(y2 - y1);
        var cy = Math.max(60, dy * 0.5);
        var d = 'M ' + x1 + ' ' + y1 + ' C ' + x1 + ' ' + (y1 + cy) + ', ' + x2 + ' ' + (y2 - cy) + ', ' + x2 + ' ' + y2;
        conn.pathEl.setAttribute('d', d);
    }

    function _updateAllConnections() {
        _connections.forEach(function(c) { _updateConnectionPath(c); });
    }

    function _removeConnection(connId) {
        _connections = _connections.filter(function(c) {
            if (c.id === connId) {
                if (c.pathEl && c.pathEl.parentNode) c.pathEl.parentNode.removeChild(c.pathEl);
                return false;
            }
            return true;
        });
        _markDirty();
    }

    // ── Clear Canvas ────────────────────────────────────────────────────────
    function _clearCanvas() {
        _nodes.forEach(function(n) { if (n.el && n.el.parentNode) n.el.parentNode.removeChild(n.el); });
        _nodes.clear();
        _connections.forEach(function(c) { if (c.pathEl && c.pathEl.parentNode) c.pathEl.parentNode.removeChild(c.pathEl); });
        _connections = [];
        _selectedNode = null;
        _closeConfigPanel();
        _zoom = 1; _panX = 0; _panY = 0;
        _updateCanvasTransform();
    }

    // ── Canvas Events ────────────────────────────────────────────────────────
    function _setupCanvasEvents() {
        var wrap = document.getElementById('wfbCanvasWrap');
        var canvas = document.getElementById('wfbCanvas');
        if (!wrap || !canvas) return;

        // Zoom with mouse wheel
        wrap.addEventListener('wheel', function(e) {
            e.preventDefault();
            var delta = e.deltaY > 0 ? -0.08 : 0.08;
            _zoom = Math.min(2, Math.max(0.3, _zoom + delta));
            _updateCanvasTransform();
        }, { passive: false });

        // Pan with middle mouse or space+drag
        wrap.addEventListener('mousedown', function(e) {
            if (e.button === 1 || (e.button === 0 && e.target === wrap)) {
                _isPanning = true;
                _panStartX = e.clientX - _panX * _zoom;
                _panStartY = e.clientY - _panY * _zoom;
                wrap.style.cursor = 'grabbing';
                e.preventDefault();
            }
        });

        document.addEventListener('mousemove', function(e) {
            if (_isPanning) {
                _panX = (e.clientX - _panStartX) / _zoom;
                _panY = (e.clientY - _panStartY) / _zoom;
                _updateCanvasTransform();
            }
            if (_draggingNode) {
                var wrapRect = wrap.getBoundingClientRect();
                var nx = (e.clientX - wrapRect.left) / _zoom - _panX - _dragOffsetX;
                var ny = (e.clientY - wrapRect.top) / _zoom - _panY - _dragOffsetY;
                nx = _snap(nx); ny = _snap(ny);
                _draggingNode.x = nx; _draggingNode.y = ny;
                _draggingNode.el.style.transform = 'translate(' + nx + 'px, ' + ny + 'px)';
                _updateAllConnections();
            }
            if (_drawingConn) {
                var wRect = wrap.getBoundingClientRect();
                var mx = (e.clientX - wRect.left) / _zoom - _panX;
                var my = (e.clientY - wRect.top) / _zoom - _panY;
                var x1 = _drawingConn.x1, y1 = _drawingConn.y1;
                var dy = Math.abs(my - y1);
                var cy = Math.max(60, dy * 0.5);
                var d = 'M ' + x1 + ' ' + y1 + ' C ' + x1 + ' ' + (y1 + cy) + ', ' + mx + ' ' + (my - cy) + ', ' + mx + ' ' + my;
                _drawingConn.tempPath.setAttribute('d', d);
            }
        });

        document.addEventListener('mouseup', function(e) {
            if (_isPanning) {
                _isPanning = false;
                wrap.style.cursor = '';
            }
            if (_draggingNode) {
                _draggingNode.el.style.zIndex = '';
                _draggingNode = null;
                _markDirty();
            }
            if (_drawingConn) {
                // Check if released on an input port
                var target = document.elementFromPoint(e.clientX, e.clientY);
                if (target && target.classList.contains('wfb-port-input')) {
                    var toNodeEl = target.closest('.wfb-node');
                    if (toNodeEl) {
                        var toId = toNodeEl.getAttribute('data-node-id');
                        if (toId && toId !== _drawingConn.fromId) {
                            _addConnection(_drawingConn.fromId, toId, _drawingConn.branch);
                            _markDirty();
                        }
                    }
                }
                if (_drawingConn.tempPath && _drawingConn.tempPath.parentNode) {
                    _drawingConn.tempPath.parentNode.removeChild(_drawingConn.tempPath);
                }
                _drawingConn = null;
            }
        });

        // Click on canvas background — deselect
        canvas.addEventListener('click', function(e) {
            if (e.target === canvas) {
                _selectNode(null);
            }
        });

        // Drag and drop from palette
        wrap.addEventListener('dragover', function(e) { e.preventDefault(); });
        wrap.addEventListener('drop', function(e) {
            e.preventDefault();
            var subtype = e.dataTransfer.getData('text/subtype');
            var stepType = e.dataTransfer.getData('text/steptype');
            if (!subtype) return;
            if (_getCat(subtype) === 'coming_soon') {
                if (typeof _showDashToast === 'function') _showDashToast(false, 'Coming soon! Pipeline move requires opportunities.write scope.');
                return;
            }
            var wRect = wrap.getBoundingClientRect();
            var nx = (e.clientX - wRect.left) / _zoom - _panX - 110;
            var ny = (e.clientY - wRect.top) / _zoom - _panY - 32;
            _addNode(subtype, nx, ny, {}, null, stepType);
            _markDirty();
            _updateEmptyState();
        });
    }

    // ── Node Mouse Events (delegation on canvas) ─────────────────────────────
    function _setupNodeEvents() {
        var canvas = document.getElementById('wfbCanvas');
        if (!canvas) return;

        canvas.addEventListener('mousedown', function(e) {
            // Port click — start connection
            if (e.target.classList.contains('wfb-port-output') || e.target.classList.contains('wfb-port-true') || e.target.classList.contains('wfb-port-false')) {
                var nodeEl = e.target.closest('.wfb-node');
                if (!nodeEl) return;
                var nodeId = nodeEl.getAttribute('data-node-id');
                var node = _nodes.get(nodeId);
                if (!node) return;
                var branch = e.target.getAttribute('data-branch') || 'default';
                var x1 = node.x + e.target.offsetLeft + 6;
                var y1 = node.y + e.target.offsetTop + 6;
                var tempPath = document.createElementNS('http://www.w3.org/2000/svg', 'path');
                tempPath.classList.add('wfb-connection', 'wfb-connection-drawing');
                var svg = document.getElementById('wfbSvgLayer');
                if (svg) svg.appendChild(tempPath);
                _drawingConn = { fromId: nodeId, branch: branch, x1: x1, y1: y1, tempPath: tempPath };
                e.preventDefault();
                e.stopPropagation();
                return;
            }

            // Node click — start drag
            var nodeEl = e.target.closest('.wfb-node');
            if (nodeEl && !e.target.classList.contains('wfb-port-input')) {
                var nodeId = nodeEl.getAttribute('data-node-id');
                var node = _nodes.get(nodeId);
                if (!node) return;
                var wrap = document.getElementById('wfbCanvasWrap');
                var wRect = wrap.getBoundingClientRect();
                _dragOffsetX = (e.clientX - wRect.left) / _zoom - _panX - node.x;
                _dragOffsetY = (e.clientY - wRect.top) / _zoom - _panY - node.y;
                _draggingNode = node;
                node.el.style.zIndex = '50';
                _selectNode(nodeId);
                e.preventDefault();
            }
        });

        // Double-click to open config
        canvas.addEventListener('dblclick', function(e) {
            var nodeEl = e.target.closest('.wfb-node');
            if (nodeEl) {
                var nodeId = nodeEl.getAttribute('data-node-id');
                _selectNode(nodeId);
                _openConfigPanel(nodeId);
            }
        });

        // Right-click context menu
        canvas.addEventListener('contextmenu', function(e) {
            e.preventDefault();
            var nodeEl = e.target.closest('.wfb-node');
            if (nodeEl) {
                var nodeId = nodeEl.getAttribute('data-node-id');
                _showContextMenu(e.clientX, e.clientY, nodeId);
            }
        });
    }

    function _selectNode(nodeId) {
        // Deselect previous
        if (_selectedNode) {
            var prev = _nodes.get(_selectedNode);
            if (prev && prev.el) prev.el.classList.remove('wfb-node-selected');
        }
        _selectedNode = nodeId;
        if (nodeId) {
            var node = _nodes.get(nodeId);
            if (node && node.el) node.el.classList.add('wfb-node-selected');
        } else {
            _closeConfigPanel();
        }
    }

    // ── Context Menu ────────────────────────────────────────────────────────
    function _showContextMenu(x, y, nodeId) {
        _removeContextMenu();
        var menu = document.createElement('div');
        menu.className = 'wfb-context-menu';
        menu.style.left = x + 'px';
        menu.style.top = y + 'px';
        menu.innerHTML =
            '<button class="wfb-dropdown-item" onclick="wfbCtxEdit(\'' + nodeId + '\')"><i class="fa-solid fa-gear"></i> Configure</button>' +
            '<button class="wfb-dropdown-item wfb-dropdown-item--danger" onclick="wfbCtxDelete(\'' + nodeId + '\')"><i class="fa-solid fa-trash"></i> Delete</button>';
        document.body.appendChild(menu);
        setTimeout(function() {
            document.addEventListener('click', function _cl() { _removeContextMenu(); document.removeEventListener('click', _cl); }, { once: true });
        }, 10);
    }

    function _removeContextMenu() {
        var m = document.querySelector('.wfb-context-menu');
        if (m) m.remove();
    }

    window.wfbCtxEdit = function(nodeId) { _removeContextMenu(); _selectNode(nodeId); _openConfigPanel(nodeId); };
    window.wfbCtxDelete = function(nodeId) { _removeContextMenu(); _deleteNode(nodeId); };

    // ── Config Panel ────────────────────────────────────────────────────────
    function _openConfigPanel(nodeId) {
        var node = _nodes.get(nodeId);
        if (!node) return;
        _selectedNode = nodeId;

        var panel = document.getElementById('wfbConfigPanel');
        if (!panel) return;
        panel.classList.add('wfb-config-open');

        // Set header
        var hdrIcon = panel.querySelector('.wfb-config-header-icon');
        if (hdrIcon) {
            hdrIcon.className = 'wfb-config-header-icon';
            hdrIcon.innerHTML = '<i class="' + _getIcon(node.subtype) + '"></i>';
        }
        var hdrTitle = panel.querySelector('.wfb-config-header-title');
        if (hdrTitle) hdrTitle.textContent = _getLabel(node.subtype);

        // Build form
        var form = document.getElementById('wfbConfigForm');
        if (!form) return;
        form.innerHTML = _buildConfigForm(node);
    }

    function _closeConfigPanel() {
        var panel = document.getElementById('wfbConfigPanel');
        if (panel) panel.classList.remove('wfb-config-open');
    }

    window.wfbCloseConfig = function() { _closeConfigPanel(); };

    function _buildConfigForm(node) {
        var c = node.config || {};
        var h = '';

        if (node.subtype === 'send_sms') {
            h += '<div class="wfb-config-field"><label class="wfb-config-label">Message</label>' +
                '<textarea class="wfb-config-textarea" id="cfgMsg" placeholder="Hi {{firstName}}, ...">' + _esc(c.message || '') + '</textarea>' +
                '<div class="wfb-merge-btns">' +
                    '<button class="wfb-merge-btn" onclick="wfbInsertMerge(\'firstName\')">{{firstName}}</button>' +
                    '<button class="wfb-merge-btn" onclick="wfbInsertMerge(\'lastName\')">{{lastName}}</button>' +
                    '<button class="wfb-merge-btn" onclick="wfbInsertMerge(\'phone\')">{{phone}}</button>' +
                    '<button class="wfb-merge-btn" onclick="wfbInsertMerge(\'city\')">{{city}}</button>' +
                    '<button class="wfb-merge-btn" onclick="wfbInsertMerge(\'state\')">{{state}}</button>' +
                '</div></div>';
            h += '<div class="wfb-config-field"><label class="wfb-config-label">From Strategy</label>' +
                '<select class="wfb-config-select" id="cfgFromStrategy">' +
                    '<option value="default"' + (c.from_strategy === 'default' ? ' selected' : '') + '>Default Number</option>' +
                    '<option value="closest_state"' + (c.from_strategy === 'closest_state' ? ' selected' : '') + '>Closest to State</option>' +
                    '<option value="rotate"' + (c.from_strategy === 'rotate' ? ' selected' : '') + '>Rotate Numbers</option>' +
                '</select></div>';
        } else if (node.subtype === 'send_igb_message') {
            var igbMode = c.mode || 'ai';
            h += '<div class="wfb-config-field"><label class="wfb-config-label">Mode</label>' +
                '<select class="wfb-config-select" id="cfgIgbMode" onchange="wfbIgbModeToggle(this.value)">' +
                    '<option value="ai"' + (igbMode === 'ai' ? ' selected' : '') + '>AI Auto-Compose (reads conversation)</option>' +
                    '<option value="manual"' + (igbMode === 'manual' ? ' selected' : '') + '>Manual Message (exact text via IGB channel)</option>' +
                '</select></div>';
            h += '<div id="cfgIgbAi" class="' + (igbMode !== 'ai' ? 'wfb-hidden' : '') + '">' +
                '<div class="wfb-config-field"><label class="wfb-config-label">AI Prompt Hint (optional)</label>' +
                '<textarea class="wfb-config-textarea" id="cfgPromptHint" placeholder="Guide the AI, e.g. \'Re-engage about their quote\'...">' + _esc(c.prompt_hint || '') + '</textarea>' +
                '<div class="wfb-config-hint">The AI reads the full conversation and composes a contextual reply.</div></div></div>';
            h += '<div id="cfgIgbManual" class="' + (igbMode !== 'manual' ? 'wfb-hidden' : '') + '">' +
                '<div class="wfb-config-field"><label class="wfb-config-label">Message</label>' +
                '<textarea class="wfb-config-textarea" id="cfgIgbMsg" placeholder="Hi {{firstName}}, ...">' + _esc(c.manual_message || '') + '</textarea>' +
                '<div class="wfb-merge-btns">' +
                    '<button class="wfb-merge-btn" onclick="wfbInsertMerge(\'firstName\',\'cfgIgbMsg\')">{{firstName}}</button>' +
                    '<button class="wfb-merge-btn" onclick="wfbInsertMerge(\'lastName\',\'cfgIgbMsg\')">{{lastName}}</button>' +
                    '<button class="wfb-merge-btn" onclick="wfbInsertMerge(\'phone\',\'cfgIgbMsg\')">{{phone}}</button>' +
                '</div>' +
                '<div class="wfb-config-hint">Sent through your configured SMS channel (GHL or Twilio).</div></div></div>';
        } else if (node.subtype === 'ai_call') {
            h += '<div class="wfb-config-field"><label class="wfb-config-label">Voice Prompt</label>' +
                '<textarea class="wfb-config-textarea" id="cfgPrompt" placeholder="Describe what the AI should say...">' + _esc(c.voice_prompt || '') + '</textarea></div>';
            h += '<div class="wfb-config-field"><label class="wfb-config-label">Ring Timeout (seconds)</label>' +
                '<input class="wfb-config-input" type="number" id="cfgTimeout" value="' + (c.ring_timeout || 30) + '" min="10" max="60"></div>';
        } else if (node.subtype === 'add_tag' || node.subtype === 'remove_tag' || node.subtype === 'tag_added' || node.subtype === 'tag_removed') {
            h += '<div class="wfb-config-field"><label class="wfb-config-label">Tag Name</label>' +
                '<input class="wfb-config-input" id="cfgTag" value="' + _esc(c.tag_name || '') + '" placeholder="e.g. hot-lead"></div>';
        } else if (node.subtype === 'wait') {
            h += '<div class="wfb-config-field"><label class="wfb-config-label">Duration</label>' +
                '<div style="display:flex;gap:8px">' +
                    '<input class="wfb-config-input" type="number" id="cfgDuration" value="' + (c.duration || 1) + '" min="1" style="flex:1">' +
                    '<select class="wfb-config-select" id="cfgUnit" style="flex:1">' +
                        '<option value="minutes"' + (c.unit === 'minutes' ? ' selected' : '') + '>Minutes</option>' +
                        '<option value="hours"' + ((c.unit === 'hours' || !c.unit) ? ' selected' : '') + '>Hours</option>' +
                        '<option value="days"' + (c.unit === 'days' ? ' selected' : '') + '>Days</option>' +
                    '</select></div></div>';
        } else if (node.subtype === 'if_else') {
            var conditions = c.conditions || [{ field: '', operator: 'equals', value: '' }];
            h += '<div class="wfb-config-field"><label class="wfb-config-label">Conditions</label>' +
                '<div id="cfgConditions">';
            conditions.forEach(function(cond, i) {
                h += '<div class="wfb-condition-row">' +
                    '<select onchange="wfbCondField(this,' + i + ')"><option value="">Field...</option>' +
                        '<option value="firstName"' + (cond.field === 'firstName' ? ' selected' : '') + '>First Name</option>' +
                        '<option value="lastName"' + (cond.field === 'lastName' ? ' selected' : '') + '>Last Name</option>' +
                        '<option value="phone"' + (cond.field === 'phone' ? ' selected' : '') + '>Phone</option>' +
                        '<option value="email"' + (cond.field === 'email' ? ' selected' : '') + '>Email</option>' +
                        '<option value="tags"' + (cond.field === 'tags' ? ' selected' : '') + '>Tags</option>' +
                        '<option value="state"' + (cond.field === 'state' ? ' selected' : '') + '>State</option>' +
                        '<option value="city"' + (cond.field === 'city' ? ' selected' : '') + '>City</option>' +
                        '<option value="source"' + (cond.field === 'source' ? ' selected' : '') + '>Source</option>' +
                        '<option value="temperature"' + (cond.field === 'temperature' ? ' selected' : '') + '>AI Temperature</option>' +
                        '<option value="score"' + (cond.field === 'score' ? ' selected' : '') + '>AI Score</option>' +
                    '</select>' +
                    '<select><option value="equals"' + (cond.operator === 'equals' ? ' selected' : '') + '>equals</option>' +
                        '<option value="not_equals"' + (cond.operator === 'not_equals' ? ' selected' : '') + '>not equals</option>' +
                        '<option value="contains"' + (cond.operator === 'contains' ? ' selected' : '') + '>contains</option>' +
                        '<option value="starts_with"' + (cond.operator === 'starts_with' ? ' selected' : '') + '>starts with</option>' +
                        '<option value="is_empty"' + (cond.operator === 'is_empty' ? ' selected' : '') + '>is empty</option>' +
                        '<option value="is_not_empty"' + (cond.operator === 'is_not_empty' ? ' selected' : '') + '>is not empty</option>' +
                        '<option value="greater_than"' + (cond.operator === 'greater_than' ? ' selected' : '') + '>greater than</option>' +
                        '<option value="less_than"' + (cond.operator === 'less_than' ? ' selected' : '') + '>less than</option>' +
                        '<option value="has_tag"' + (cond.operator === 'has_tag' ? ' selected' : '') + '>has tag</option>' +
                        '<option value="no_tag"' + (cond.operator === 'no_tag' ? ' selected' : '') + '>no tag</option>' +
                        '<option value="in_state"' + (cond.operator === 'in_state' ? ' selected' : '') + '>in state</option>' +
                        '<option value="lead_age_days"' + (cond.operator === 'lead_age_days' ? ' selected' : '') + '>lead age (days)</option>' +
                        '<option value="temperature_is"' + (cond.operator === 'temperature_is' ? ' selected' : '') + '>temperature is</option>' +
                        '<option value="score_above"' + (cond.operator === 'score_above' ? ' selected' : '') + '>score above</option>' +
                        '<option value="score_below"' + (cond.operator === 'score_below' ? ' selected' : '') + '>score below</option>' +
                        '<option value="responded_within"' + (cond.operator === 'responded_within' ? ' selected' : '') + '>responded within (min)</option>' +
                        '<option value="total_messages_sent"' + (cond.operator === 'total_messages_sent' ? ' selected' : '') + '>total messages sent</option>' +
                        '<option value="time_is_between"' + (cond.operator === 'time_is_between' ? ' selected' : '') + '>time is between</option>' +
                    '</select>' +
                    '<input value="' + _esc(cond.value || '') + '" placeholder="Value">' +
                    '<button class="wfb-condition-remove" onclick="this.parentNode.remove()"><i class="fa-solid fa-xmark"></i></button>' +
                '</div>';
            });
            h += '</div><button class="wfb-add-condition" onclick="wfbAddCondition()">+ Add Condition</button></div>';
        } else if (node.subtype === 'loop') {
            h += '<div class="wfb-config-field"><label class="wfb-config-label">Max Iterations</label>' +
                '<input class="wfb-config-input" type="number" id="cfgMaxIter" value="' + (c.max_iterations || 15) + '" min="1" max="100"></div>';
        } else if (node.subtype === 'send_webhook') {
            h += '<div class="wfb-config-field"><label class="wfb-config-label">URL</label>' +
                '<input class="wfb-config-input" id="cfgUrl" value="' + _esc(c.url || '') + '" placeholder="https://..."></div>';
            h += '<div class="wfb-config-field"><label class="wfb-config-label">Method</label>' +
                '<select class="wfb-config-select" id="cfgMethod">' +
                    '<option value="POST"' + (c.method === 'POST' ? ' selected' : '') + '>POST</option>' +
                    '<option value="GET"' + (c.method === 'GET' ? ' selected' : '') + '>GET</option>' +
                    '<option value="PUT"' + (c.method === 'PUT' ? ' selected' : '') + '>PUT</option>' +
                '</select></div>';
        } else if (node.subtype === 'update_field') {
            h += '<div class="wfb-config-field"><label class="wfb-config-label">Field</label>' +
                '<input class="wfb-config-input" id="cfgField" value="' + _esc(c.field || '') + '" placeholder="e.g. firstName"></div>';
            h += '<div class="wfb-config-field"><label class="wfb-config-label">Value</label>' +
                '<input class="wfb-config-input" id="cfgValue" value="' + _esc(c.value || '') + '"></div>';
        } else if (node.subtype === 'add_note') {
            h += '<div class="wfb-config-field"><label class="wfb-config-label">Note</label>' +
                '<textarea class="wfb-config-textarea" id="cfgNote">' + _esc(c.note || '') + '</textarea></div>';
        } else if (node.subtype === 'scheduled') {
            h += '<div class="wfb-config-field"><label class="wfb-config-label">Cron Expression</label>' +
                '<input class="wfb-config-input" id="cfgCron" value="' + _esc(c.cron || '0 9 * * *') + '" placeholder="0 9 * * *">' +
                '<div class="wfb-config-hint">e.g. 0 9 * * * = every day at 9am</div></div>';
        } else if (node.subtype === 'no_response') {
            h += '<div class="wfb-config-field"><label class="wfb-config-label">Days without response</label>' +
                '<input class="wfb-config-input" type="number" id="cfgDays" value="' + (c.days || 3) + '" min="1"></div>';
        } else if (node.subtype === 'lead_age') {
            h += '<div class="wfb-config-field"><label class="wfb-config-label">Days since import</label>' +
                '<input class="wfb-config-input" type="number" id="cfgDays" value="' + (c.days_since_import || 60) + '" min="1"></div>';
        } else if (node.subtype === 'birthday_approaching') {
            h += '<div class="wfb-config-field"><label class="wfb-config-label">Days before birthday</label>' +
                '<input class="wfb-config-input" type="number" id="cfgDaysBefore" value="' + (c.days_before || 7) + '" min="1" max="30"></div>';
        } else if (node.subtype === 'wait_until') {
            var cond = c.condition || {};
            h += '<div class="wfb-config-field"><label class="wfb-config-label">Wait until condition is true</label>' +
                '<select class="wfb-config-select" id="cfgWuField">' +
                    '<option value="">Select field...</option>' +
                    '<option value="responded_within"' + (cond.field === 'responded_within' ? ' selected' : '') + '>Contact replied</option>' +
                    '<option value="has_tag"' + (cond.field === 'has_tag' ? ' selected' : '') + '>Has tag</option>' +
                    '<option value="score_above"' + (cond.field === 'score_above' ? ' selected' : '') + '>AI score above</option>' +
                    '<option value="temperature_is"' + (cond.field === 'temperature_is' ? ' selected' : '') + '>AI temperature is</option>' +
                    '<option value="is_not_empty"' + (cond.field === 'is_not_empty' ? ' selected' : '') + '>Field is not empty</option>' +
                '</select></div>';
            h += '<div class="wfb-config-field"><label class="wfb-config-label">Operator</label>' +
                '<select class="wfb-config-select" id="cfgWuOp">' +
                    '<option value="responded_within"' + (cond.operator === 'responded_within' ? ' selected' : '') + '>responded within</option>' +
                    '<option value="has_tag"' + (cond.operator === 'has_tag' ? ' selected' : '') + '>has tag</option>' +
                    '<option value="equals"' + (cond.operator === 'equals' ? ' selected' : '') + '>equals</option>' +
                    '<option value="greater_than"' + (cond.operator === 'greater_than' ? ' selected' : '') + '>greater than</option>' +
                    '<option value="score_above"' + (cond.operator === 'score_above' ? ' selected' : '') + '>score above</option>' +
                    '<option value="temperature_is"' + (cond.operator === 'temperature_is' ? ' selected' : '') + '>temperature is</option>' +
                    '<option value="is_not_empty"' + (cond.operator === 'is_not_empty' ? ' selected' : '') + '>is not empty</option>' +
                '</select></div>';
            h += '<div class="wfb-config-field"><label class="wfb-config-label">Value</label>' +
                '<input class="wfb-config-input" id="cfgWuValue" value="' + _esc(cond.value || '') + '" placeholder="e.g. 60 (minutes), hot, tag-name"></div>';
            h += '<div class="wfb-config-field"><label class="wfb-config-label">Max wait (hours)</label>' +
                '<input class="wfb-config-input" type="number" id="cfgWuMaxHours" value="' + (c.max_wait_hours || 72) + '" min="1" max="720"></div>';
            h += '<div class="wfb-config-field"><label class="wfb-config-label">Check every (minutes)</label>' +
                '<input class="wfb-config-input" type="number" id="cfgWuInterval" value="' + (c.check_interval_minutes || 5) + '" min="1" max="60"></div>';
            h += '<div class="wfb-config-hint">Branches: "Met" when condition is true, "T/O" on timeout.</div>';
        } else if (node.subtype === 'state_query') {
            h += '<div class="wfb-config-field"><label class="wfb-config-label">Query Type</label>' +
                '<select class="wfb-config-select" id="cfgSqType">' +
                    '<option value="">Select query...</option>' +
                    '<option value="days_since_contact"' + (c.query_type === 'days_since_contact' ? ' selected' : '') + '>Days since last contact</option>' +
                    '<option value="message_count"' + (c.query_type === 'message_count' ? ' selected' : '') + '>Message count</option>' +
                    '<option value="call_count"' + (c.query_type === 'call_count' ? ' selected' : '') + '>Call count</option>' +
                    '<option value="last_outbound_message"' + (c.query_type === 'last_outbound_message' ? ' selected' : '') + '>Last outbound message date</option>' +
                    '<option value="last_inbound_message"' + (c.query_type === 'last_inbound_message' ? ' selected' : '') + '>Last inbound message date</option>' +
                    '<option value="last_call_date"' + (c.query_type === 'last_call_date' ? ' selected' : '') + '>Last call date</option>' +
                    '<option value="contact_field"' + (c.query_type === 'contact_field' ? ' selected' : '') + '>Contact field value</option>' +
                    '<option value="workflow_run_count"' + (c.query_type === 'workflow_run_count' ? ' selected' : '') + '>Workflow run count</option>' +
                '</select></div>';
            h += '<div class="wfb-config-field"><label class="wfb-config-label">Store as variable</label>' +
                '<input class="wfb-config-input" id="cfgSqStore" value="' + _esc(c.store_as || '') + '" placeholder="e.g. days_silent"></div>';
            h += '<div class="wfb-config-field"><label class="wfb-config-label">Field name (for contact_field)</label>' +
                '<input class="wfb-config-input" id="cfgSqField" value="' + _esc(c.field || '') + '" placeholder="e.g. email, state, customField.key"></div>';
            h += '<div class="wfb-config-hint">Result stored in context — use the variable name as "field" in a downstream If/Then.</div>';
        } else if (node.subtype === 'assign_agent') {
            h += '<div class="wfb-config-field"><label class="wfb-config-label">User ID</label>' +
                '<input class="wfb-config-input" id="cfgAssignTo" value="' + _esc(c.assigned_to || '') + '" placeholder="GHL user ID"></div>';
        } else if (node.subtype === 'move_stage') {
            h += '<div class="wfb-config-field"><label class="wfb-config-label">Pipeline ID</label>' +
                '<input class="wfb-config-input" id="cfgPipelineId" value="' + _esc(c.pipeline_id || '') + '" placeholder="GHL pipeline ID"></div>';
            h += '<div class="wfb-config-field"><label class="wfb-config-label">Stage ID</label>' +
                '<input class="wfb-config-input" id="cfgStageId" value="' + _esc(c.stage_id || '') + '" placeholder="GHL stage ID"></div>';
        } else if (node.subtype === 'custom') {
            h += '<div class="wfb-config-field"><label class="wfb-config-label">Action Name</label>' +
                '<input class="wfb-config-input" id="cfgCustomName" value="' + _esc(c.action_name || '') + '" placeholder="e.g. timezone-aware-text"></div>';
            h += '<div class="wfb-config-field"><label class="wfb-config-label">Description</label>' +
                '<textarea class="wfb-config-textarea" id="cfgCustomDesc" placeholder="Describe what this action should do...">' + _esc(c.description || '') + '</textarea>' +
                '<div class="wfb-config-hint">AI interprets this at runtime and maps to real actions.</div></div>';
        } else if (node.subtype === 'goto') {
            h += '<div class="wfb-config-field"><label class="wfb-config-label">Target Step ID</label>' +
                '<input class="wfb-config-input" id="cfgGotoTarget" value="' + _esc(c.target_step_id || '') + '" placeholder="Step ID to jump to">' +
                '<div class="wfb-config-hint">Tip: Copy the step ID from the step you want to jump to.</div></div>';
        } else {
            h += '<div class="wfb-config-hint">No additional configuration needed.</div>';
        }

        h += '<button class="wfb-config-save" onclick="wfbSaveConfig()">Save</button>';
        return h;
    }

    window.wfbCondField = function(sel, idx) {
        // No-op for now — field selection tracked on save via DOM
    };

    window.wfbInsertMerge = function(field, targetId) {
        var ta = document.getElementById(targetId || 'cfgMsg');
        if (ta) {
            var start = ta.selectionStart;
            var val = ta.value;
            var insert = '{{' + field + '}}';
            ta.value = val.substring(0, start) + insert + val.substring(ta.selectionEnd);
            ta.selectionStart = ta.selectionEnd = start + insert.length;
            ta.focus();
        }
    };

    window.wfbIgbModeToggle = function(mode) {
        var aiDiv = document.getElementById('cfgIgbAi');
        var manDiv = document.getElementById('cfgIgbManual');
        if (aiDiv) aiDiv.classList.toggle('wfb-hidden', mode !== 'ai');
        if (manDiv) manDiv.classList.toggle('wfb-hidden', mode !== 'manual');
    };

    window.wfbAddCondition = function() {
        var wrap = document.getElementById('cfgConditions');
        if (!wrap) return;
        var row = document.createElement('div');
        row.className = 'wfb-condition-row';
        row.innerHTML = '<select><option value="">Field...</option><option value="firstName">First Name</option><option value="lastName">Last Name</option><option value="phone">Phone</option>' +
            '<option value="email">Email</option><option value="tags">Tags</option><option value="state">State</option><option value="city">City</option>' +
            '<option value="source">Source</option><option value="temperature">AI Temperature</option><option value="score">AI Score</option></select>' +
            '<select><option value="equals">equals</option><option value="not_equals">not equals</option><option value="contains">contains</option><option value="starts_with">starts with</option>' +
            '<option value="is_empty">is empty</option><option value="is_not_empty">is not empty</option><option value="greater_than">greater than</option><option value="less_than">less than</option>' +
            '<option value="has_tag">has tag</option><option value="no_tag">no tag</option><option value="in_state">in state</option><option value="lead_age_days">lead age (days)</option>' +
            '<option value="temperature_is">temperature is</option><option value="score_above">score above</option><option value="score_below">score below</option>' +
            '<option value="responded_within">responded within (min)</option><option value="total_messages_sent">total messages sent</option><option value="time_is_between">time is between</option></select>' +
            '<input placeholder="Value"><button class="wfb-condition-remove" onclick="this.parentNode.remove()"><i class="fa-solid fa-xmark"></i></button>';
        wrap.appendChild(row);
    };

    window.wfbSaveConfig = function() {
        if (!_selectedNode) return;
        var node = _nodes.get(_selectedNode);
        if (!node) return;
        var c = {};

        if (node.subtype === 'send_sms') {
            var msg = document.getElementById('cfgMsg');
            var strat = document.getElementById('cfgFromStrategy');
            c.message = msg ? msg.value : '';
            c.from_strategy = strat ? strat.value : 'default';
        } else if (node.subtype === 'send_igb_message') {
            var modeEl = document.getElementById('cfgIgbMode');
            c.mode = modeEl ? modeEl.value : 'ai';
            var hintEl = document.getElementById('cfgPromptHint');
            c.prompt_hint = hintEl ? hintEl.value : '';
            var manMsgEl = document.getElementById('cfgIgbMsg');
            c.manual_message = manMsgEl ? manMsgEl.value : '';
        } else if (node.subtype === 'ai_call') {
            var pr = document.getElementById('cfgPrompt');
            var to = document.getElementById('cfgTimeout');
            c.voice_prompt = pr ? pr.value : '';
            c.ring_timeout = to ? parseInt(to.value) || 30 : 30;
        } else if (node.subtype === 'add_tag' || node.subtype === 'remove_tag' || node.subtype === 'tag_added' || node.subtype === 'tag_removed') {
            var t = document.getElementById('cfgTag');
            c.tag_name = t ? t.value : '';
        } else if (node.subtype === 'wait') {
            var d = document.getElementById('cfgDuration');
            var u = document.getElementById('cfgUnit');
            c.duration = d ? parseInt(d.value) || 1 : 1;
            c.unit = u ? u.value : 'hours';
        } else if (node.subtype === 'if_else') {
            var rows = document.querySelectorAll('#cfgConditions .wfb-condition-row');
            c.conditions = [];
            rows.forEach(function(row) {
                var sels = row.querySelectorAll('select');
                var inp = row.querySelector('input');
                c.conditions.push({
                    field: sels[0] ? sels[0].value : '',
                    operator: sels[1] ? sels[1].value : 'equals',
                    value: inp ? inp.value : ''
                });
            });
            c.logic = 'and';
        } else if (node.subtype === 'loop') {
            var mi = document.getElementById('cfgMaxIter');
            c.max_iterations = mi ? parseInt(mi.value) || 15 : 15;
        } else if (node.subtype === 'send_webhook') {
            var url = document.getElementById('cfgUrl');
            var meth = document.getElementById('cfgMethod');
            c.url = url ? url.value : '';
            c.method = meth ? meth.value : 'POST';
        } else if (node.subtype === 'update_field') {
            var f = document.getElementById('cfgField');
            var v = document.getElementById('cfgValue');
            c.field = f ? f.value : '';
            c.value = v ? v.value : '';
        } else if (node.subtype === 'add_note') {
            var n = document.getElementById('cfgNote');
            c.note = n ? n.value : '';
        } else if (node.subtype === 'scheduled') {
            var cr = document.getElementById('cfgCron');
            c.cron = cr ? cr.value : '0 9 * * *';
        } else if (node.subtype === 'no_response') {
            var dy = document.getElementById('cfgDays');
            c.days = dy ? parseInt(dy.value) || 3 : 3;
        } else if (node.subtype === 'lead_age') {
            var da = document.getElementById('cfgDays');
            c.days_since_import = da ? parseInt(da.value) || 60 : 60;
        } else if (node.subtype === 'birthday_approaching') {
            var db = document.getElementById('cfgDaysBefore');
            c.days_before = db ? parseInt(db.value) || 7 : 7;
        } else if (node.subtype === 'wait_until') {
            var wuf = document.getElementById('cfgWuField');
            var wuo = document.getElementById('cfgWuOp');
            var wuv = document.getElementById('cfgWuValue');
            var wuh = document.getElementById('cfgWuMaxHours');
            var wui = document.getElementById('cfgWuInterval');
            c.condition = {
                field: wuf ? wuf.value : '',
                operator: wuo ? wuo.value : 'equals',
                value: wuv ? wuv.value : ''
            };
            c.max_wait_hours = wuh ? parseInt(wuh.value) || 72 : 72;
            c.check_interval_minutes = wui ? parseInt(wui.value) || 5 : 5;
        } else if (node.subtype === 'state_query') {
            var sqt = document.getElementById('cfgSqType');
            var sqs = document.getElementById('cfgSqStore');
            var sqf = document.getElementById('cfgSqField');
            c.query_type = sqt ? sqt.value : '';
            c.store_as = sqs ? sqs.value : c.query_type;
            c.field = sqf ? sqf.value : '';
        } else if (node.subtype === 'assign_agent') {
            var aa = document.getElementById('cfgAssignTo');
            c.assigned_to = aa ? aa.value : '';
        } else if (node.subtype === 'move_stage') {
            var pid = document.getElementById('cfgPipelineId');
            var sid = document.getElementById('cfgStageId');
            c.pipeline_id = pid ? pid.value : '';
            c.stage_id = sid ? sid.value : '';
        } else if (node.subtype === 'custom') {
            var cn = document.getElementById('cfgCustomName');
            var cd = document.getElementById('cfgCustomDesc');
            c.action_name = cn ? cn.value : '';
            c.description = cd ? cd.value : '';
        } else if (node.subtype === 'goto') {
            var gt = document.getElementById('cfgGotoTarget');
            c.target_step_id = gt ? gt.value : '';
        }

        node.config = c;
        _updateNodeSummary(node.id);
        _closeConfigPanel();
        _markDirty();
        if (typeof _showDashToast === 'function') _showDashToast(true, 'Configuration saved');
    };

    // ── Palette Events ──────────────────────────────────────────────────────
    function _setupPaletteEvents() {
        document.querySelectorAll('.wfb-palette-item').forEach(function(item) {
            item.addEventListener('dragstart', function(e) {
                e.dataTransfer.setData('text/subtype', item.getAttribute('data-subtype'));
                e.dataTransfer.setData('text/steptype', item.getAttribute('data-type') || 'action');
            });
        });

        // Category collapse
        document.querySelectorAll('.wfb-palette-cat-header').forEach(function(hdr) {
            hdr.addEventListener('click', function() {
                var cat = hdr.closest('.wfb-palette-category');
                if (cat) cat.classList.toggle('wfb-cat-collapsed');
                var chevron = hdr.querySelector('.wfb-palette-cat-chevron');
                if (chevron) chevron.classList.toggle('wfb-cat-open');
            });
        });

        // "Not seeing what you need?" toggles
        document.querySelectorAll('.wfb-palette-feedback-link').forEach(function(link) {
            link.addEventListener('click', function() {
                var feedbackWrap = link.closest('.wfb-palette-feedback');
                var input = feedbackWrap ? feedbackWrap.querySelector('.wfb-palette-feedback-input') : null;
                if (input) input.classList.toggle('wfb-hidden');
            });
        });

        // Palette search
        var paletteSearch = document.getElementById('wfbPaletteSearch');
        if (paletteSearch) {
            paletteSearch.addEventListener('input', function() {
                var q = paletteSearch.value.toLowerCase().trim();
                document.querySelectorAll('.wfb-palette-item').forEach(function(item) {
                    var text = item.textContent.toLowerCase();
                    item.style.display = (!q || text.indexOf(q) !== -1) ? '' : 'none';
                });
            });
        }
    }

    // ── Save ────────────────────────────────────────────────────────────────
    function _markDirty() {
        if (_saveTimer) clearTimeout(_saveTimer);
        _saveTimer = setTimeout(function() { _saveFullWorkflow(); }, 3000);
    }

    function _saveFullWorkflow() {
        if (!_currentWorkflow) return;

        var steps = [];
        var connections = [];
        var triggerNode = null;

        _nodes.forEach(function(n) {
            if (_getCat(n.subtype) === 'trigger') {
                triggerNode = n;
            } else {
                steps.push({
                    id: n.id,
                    step_type: n.type || 'action',
                    step_subtype: n.subtype,
                    config: n.config || {},
                    position_x: n.x,
                    position_y: n.y
                });
            }
        });

        _connections.forEach(function(c) {
            connections.push({
                from_step_id: c.from,
                to_step_id: c.to,
                branch_key: c.branch || 'default'
            });
        });

        var body = {
            steps: steps,
            connections: connections
        };

        if (triggerNode) {
            body.trigger_type = triggerNode.subtype;
            body.trigger_config = triggerNode.config || {};
        }

        var nameEl = document.getElementById('wfbWorkflowName');
        if (nameEl) body.name = nameEl.value;

        fetch('/api/workflows/' + _currentWorkflow.id + '/save-full', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        }).then(function(r) { return r.json(); }).then(function(data) {
            if (data.ok && data.step_id_map) {
                // Update node IDs to match server-assigned IDs
                var map = data.step_id_map;
                for (var oldId in map) {
                    var newId = map[oldId];
                    if (oldId !== newId && _nodes.has(oldId)) {
                        var node = _nodes.get(oldId);
                        _nodes.delete(oldId);
                        node.id = newId;
                        node.el.setAttribute('data-node-id', newId);
                        _nodes.set(newId, node);
                        _connections.forEach(function(c) {
                            if (c.from === oldId) c.from = newId;
                            if (c.to === oldId) c.to = newId;
                        });
                        if (_selectedNode === oldId) _selectedNode = newId;
                    }
                }
            }
        });
    }

    window.wfbSave = function() { _saveFullWorkflow(); if (typeof _showDashToast === 'function') _showDashToast(true, 'Workflow saved'); };

    // ── Toolbar Actions ─────────────────────────────────────────────────────
    window.wfbActivate = function() {
        if (!_currentWorkflow) return;
        var action = _currentWorkflow.status === 'active' ? 'pause' : 'activate';
        fetch('/api/workflows/' + _currentWorkflow.id + '/' + action, { method: 'POST' })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.workflow) {
                    _currentWorkflow = data.workflow;
                    var badge = document.getElementById('wfbEditorBadge');
                    if (badge) {
                        var bc = _currentWorkflow.status === 'active' ? 'wfb-badge-active' : _currentWorkflow.status === 'paused' ? 'wfb-badge-paused' : 'wfb-badge-draft';
                        badge.className = 'wfb-badge ' + bc;
                        badge.textContent = _currentWorkflow.status;
                    }
                }
            });
    };

    window.wfbDeleteCurrent = function() {
        if (!_currentWorkflow || !confirm('Delete this workflow?')) return;
        fetch('/api/workflows/' + _currentWorkflow.id, { method: 'DELETE' }).then(function() {
            wfbShowList();
        });
    };

    // ── AI Builder ──────────────────────────────────────────────────────────
    window.wfbOpenAiModal = function() {
        var modal = document.getElementById('wfbAiModal');
        if (modal) modal.classList.add('wfb-ai-modal-open');
    };

    window.wfbCloseAiModal = function() {
        var modal = document.getElementById('wfbAiModal');
        if (modal) modal.classList.remove('wfb-ai-modal-open');
    };

    window.wfbAiExample = function(text) {
        var ta = document.getElementById('wfbAiPrompt');
        if (ta) ta.value = text;
    };

    window.wfbBuildWithAi = function() {
        var ta = document.getElementById('wfbAiPrompt');
        var prompt = ta ? ta.value.trim() : '';
        if (!prompt) {
            if (typeof _showDashToast === 'function') _showDashToast(false, 'Please describe what you want to automate');
            return;
        }

        var btn = document.getElementById('wfbAiBuildBtn');
        var loader = document.getElementById('wfbAiLoading');
        // Show loading indicator, hide build button
        if (btn) btn.style.display = 'none';
        if (loader) loader.classList.add('wfb-ai-loading-active');

        function _resetBuildBtn() {
            if (btn) btn.style.display = '';
            if (loader) loader.classList.remove('wfb-ai-loading-active');
        }

        fetch('/api/workflows/build-with-ai', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ prompt: prompt })
        }).then(function(r) { return r.json(); }).then(function(data) {
            if (data.error) {
                _resetBuildBtn();
                if (typeof _showDashToast === 'function') _showDashToast(false, data.error);
                return;
            }
            _resetBuildBtn();
            wfbCloseAiModal();
            _animateAiWorkflow(data.workflow);
        }).catch(function(err) {
            _resetBuildBtn();
            if (typeof _showDashToast === 'function') _showDashToast(false, 'AI build failed: ' + (err.message || 'Network error'));
        });
    };

    // ── AI Animation Sequence ───────────────────────────────────────────────
    function _animateAiWorkflow(wfData) {
        // Create workflow on backend
        fetch('/api/workflows', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                name: wfData.name || 'AI-Built Workflow',
                trigger_type: wfData.trigger_type || 'manual',
                trigger_config: wfData.trigger_config || {},
                description: wfData.description || ''
            })
        }).then(function(r) { return r.json(); }).then(function(result) {
            _currentWorkflow = result.workflow;

            // Switch to editor
            var lv = document.getElementById('wfbListView');
            var ev = document.getElementById('wfbEditorView');
            if (lv) lv.classList.add('wfb-hidden');
            if (ev) ev.classList.remove('wfb-hidden');
            _clearCanvas();

            var nameEl = document.getElementById('wfbWorkflowName');
            if (nameEl) nameEl.value = _currentWorkflow.name;

            // Show AI building overlay
            var overlay = document.getElementById('wfbAiOverlay');
            if (overlay) overlay.classList.add('wfb-ai-building');

            var steps = wfData.steps || [];
            var startX = 400, startY = 80, spacingY = 150;

            // Prepare trigger
            var triggerData = {
                id: 'trigger_' + Date.now(),
                subtype: wfData.trigger_type || 'manual',
                config: wfData.trigger_config || {},
                x: startX, y: startY, type: 'trigger'
            };

            // Assign positions
            steps.forEach(function(s, i) {
                if (!s.position_x) s.position_x = startX;
                if (!s.position_y) s.position_y = startY + (i + 1) * spacingY;
            });

            setTimeout(function() {
                if (overlay) overlay.classList.remove('wfb-ai-building');

                // Animate trigger node
                _animateNodeIn(triggerData, 0);

                // Animate steps
                steps.forEach(function(s, i) {
                    _animateNodeIn({
                        id: s.temp_id || ('step_' + i),
                        subtype: s.step_subtype,
                        config: s.config || {},
                        x: s.position_x, y: s.position_y,
                        type: s.step_type || 'action'
                    }, (i + 1) * 250 + 400);
                });

                // Animate connections after nodes
                var totalNodeDelay = (steps.length + 1) * 250 + 700;
                var conns = wfData.connections || [];

                // Auto-connect trigger to first step
                if (steps.length > 0) {
                    conns.unshift({
                        from_temp_id: triggerData.id,
                        to_temp_id: steps[0].temp_id || 'step_0',
                        branch_key: 'default'
                    });
                }

                conns.forEach(function(c, i) {
                    setTimeout(function() {
                        _animateConnectionIn(c.from_temp_id, c.to_temp_id, c.branch_key || 'default');
                    }, totalNodeDelay + i * 200);
                });

                // Zoom to fit and save
                var finalDelay = totalNodeDelay + conns.length * 200 + 400;
                setTimeout(function() {
                    _zoomToFit(true);
                    if (typeof _showDashToast === 'function') _showDashToast(true, 'Workflow built! Review and customize your steps.');
                    _updateEmptyState();
                    _saveFullWorkflow();
                }, finalDelay);

            }, 1200);
        });
    }

    function _animateNodeIn(data, delay) {
        setTimeout(function() {
            var node = _addNode(data.subtype, data.x, data.y, data.config, data.id, data.type);
            var el = node.el;
            el.style.opacity = '0';
            el.style.transform = 'translate(' + data.x + 'px, ' + data.y + 'px) scale(0)';
            el.offsetHeight; // force reflow
            el.style.transition = 'transform 0.5s cubic-bezier(0.34, 1.56, 0.64, 1), opacity 0.3s ease';
            el.style.opacity = '1';
            el.style.transform = 'translate(' + data.x + 'px, ' + data.y + 'px) scale(1)';
            setTimeout(function() { el.style.transition = ''; }, 600);
        }, delay);
    }

    function _animateConnectionIn(fromId, toId, branch) {
        var conn = _addConnection(fromId, toId, branch);
        if (!conn || !conn.pathEl) return;
        var path = conn.pathEl;
        var length = path.getTotalLength ? path.getTotalLength() : 200;
        path.style.strokeDasharray = length;
        path.style.strokeDashoffset = length;
        path.classList.add('wfb-connection-animating');
        path.getBoundingClientRect(); // force reflow
        path.style.transition = 'stroke-dashoffset 0.6s ease-out';
        path.style.strokeDashoffset = '0';
        setTimeout(function() {
            path.classList.remove('wfb-connection-animating');
            path.style.strokeDasharray = '';
            path.style.strokeDashoffset = '';
            path.style.transition = '';
        }, 700);
    }

    // ── Custom Actions ──────────────────────────────────────────────────────
    function _loadCustomActions() {
        fetch('/api/workflows/custom-actions').then(function(r) { return r.json(); }).then(function(data) {
            _customActions = data.custom_actions || [];
            _renderCustomActions();
        }).catch(function() {});
    }

    function _renderCustomActions() {
        var wrap = document.getElementById('wfbCustomActions');
        if (!wrap) return;
        if (!_customActions.length) {
            wrap.innerHTML = '<div class="wfb-config-hint" style="padding:4px 12px">No custom actions yet</div>';
            return;
        }
        var html = '';
        _customActions.forEach(function(a) {
            html += '<div class="wfb-palette-item wfb-palette-item--action" draggable="true" data-subtype="custom" data-type="action" data-custom-id="' + _esc(a.id) + '">' +
                '<i class="' + (a.icon || 'fa-solid fa-puzzle-piece') + '"></i><span>' + _esc(a.name) + '</span></div>';
        });
        wrap.innerHTML = html;
        // Re-bind drag events
        wrap.querySelectorAll('.wfb-palette-item').forEach(function(item) {
            item.addEventListener('dragstart', function(e) {
                e.dataTransfer.setData('text/subtype', 'custom');
                e.dataTransfer.setData('text/steptype', 'action');
            });
        });
    }

    window.wfbCreateCustomAction = function(btn) {
        var wrap = btn.closest('.wfb-palette-category');
        var input = wrap ? wrap.querySelector('.wfb-custom-input') : null;
        if (!input || !input.value.trim()) return;
        var desc = input.value.trim();

        fetch('/api/workflows/custom-actions', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: desc.substring(0, 50), description: desc })
        }).then(function(r) { return r.json(); }).then(function(data) {
            if (data.custom_action) {
                _customActions.push(data.custom_action);
                _renderCustomActions();
                input.value = '';
                if (typeof _showDashToast === 'function') _showDashToast(true, 'Custom action created!');
            }
        });
    };

    // ── Keyboard Shortcuts ──────────────────────────────────────────────────
    function _setupKeyboard() {
        document.addEventListener('keydown', function(e) {
            // Only when editor is visible
            var ev = document.getElementById('wfbEditorView');
            if (!ev || ev.classList.contains('wfb-hidden')) return;

            if ((e.key === 'Delete' || e.key === 'Backspace') && _selectedNode && document.activeElement.tagName !== 'INPUT' && document.activeElement.tagName !== 'TEXTAREA') {
                _deleteNode(_selectedNode);
                e.preventDefault();
            }
            if (e.key === 's' && (e.ctrlKey || e.metaKey)) {
                e.preventDefault();
                _saveFullWorkflow();
                if (typeof _showDashToast === 'function') _showDashToast(true, 'Saved');
            }
            if (e.key === 'Escape') {
                _selectNode(null);
                _closeConfigPanel();
                wfbCloseAiModal();
            }
        });
    }

    // ── Init ────────────────────────────────────────────────────────────────
    // ── Button Event Bindings ────────────────────────────────────────────────
    function _setupButtonEvents() {
        // List view buttons
        var newBtn = document.getElementById('wfbNewBtn');
        var newEmptyBtn = document.getElementById('wfbNewEmptyBtn');
        var aiListBtn = document.getElementById('wfbAiListBtn');
        var aiEmptyBtn = document.getElementById('wfbAiEmptyBtn');
        var importGhlBtn = document.getElementById('wfbImportGhlBtn');
        if (newBtn) newBtn.addEventListener('click', function() { wfbNewWorkflow(); });
        if (newEmptyBtn) newEmptyBtn.addEventListener('click', function() { wfbNewWorkflow(); });
        if (aiListBtn) aiListBtn.addEventListener('click', function() { wfbOpenAiModal(); });
        if (aiEmptyBtn) aiEmptyBtn.addEventListener('click', function() { wfbOpenAiModal(); });
        if (importGhlBtn) importGhlBtn.addEventListener('click', function() { wfbImportGhl(); });

        // Editor toolbar buttons
        var backBtn = document.getElementById('wfbBackBtn');
        var saveBtn = document.getElementById('wfbSaveBtn');
        var aiEditorBtn = document.getElementById('wfbAiEditorBtn');
        var activeToggle = document.getElementById('wfbActiveToggle');
        if (backBtn) backBtn.addEventListener('click', function() { wfbShowList(); });
        if (saveBtn) saveBtn.addEventListener('click', function() { wfbSave(); });
        if (aiEditorBtn) aiEditorBtn.addEventListener('click', function() { wfbOpenAiModal(); });
        if (activeToggle) activeToggle.addEventListener('click', function() { wfbActivate(); });

        // AI modal buttons
        var aiClose = document.getElementById('wfbAiClose');
        var aiBuildBtn = document.getElementById('wfbAiBuildBtn');
        if (aiClose) aiClose.addEventListener('click', function() { wfbCloseAiModal(); });
        if (aiBuildBtn) aiBuildBtn.addEventListener('click', function() { wfbBuildWithAi(); });

        // AI example chips
        document.querySelectorAll('.wfb-ai-chip[data-prompt]').forEach(function(chip) {
            chip.addEventListener('click', function() {
                wfbAiExample(chip.getAttribute('data-prompt'));
            });
        });

        // Config panel
        var configClose = document.getElementById('wfbConfigClose');
        var configSave = document.getElementById('wfbConfigSave');
        if (configClose) configClose.addEventListener('click', function() { wfbCloseConfig(); });
        if (configSave) configSave.addEventListener('click', function() { wfbSaveConfig(); });

        // Zoom buttons
        var zoomIn = document.getElementById('wfbZoomIn');
        var zoomOut = document.getElementById('wfbZoomOut');
        var zoomFit = document.getElementById('wfbZoomFit');
        if (zoomIn) zoomIn.addEventListener('click', function() { wfbZoomIn(); });
        if (zoomOut) zoomOut.addEventListener('click', function() { wfbZoomOut(); });
        if (zoomFit) zoomFit.addEventListener('click', function() { wfbZoomFit(); });

        // Palette toggle
        var paletteToggle = document.getElementById('wfbPaletteToggle');
        if (paletteToggle) paletteToggle.addEventListener('click', function() {
            var palette = document.getElementById('wfbPalette');
            if (palette) palette.classList.toggle('wfb-palette-collapsed');
        });

        // More menu
        var moreBtn = document.getElementById('wfbMoreBtn');
        if (moreBtn) moreBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            var existing = document.querySelector('.wfb-dropdown.wfb-toolbar-dropdown');
            if (existing) { existing.remove(); return; }
            var dd = document.createElement('div');
            dd.className = 'wfb-dropdown wfb-toolbar-dropdown';
            dd.innerHTML = '<div class="wfb-dropdown-item" id="wfbMoreDuplicate"><i class="fa-solid fa-copy"></i> Duplicate</div>' +
                '<div class="wfb-dropdown-item wfb-dropdown-item--danger" id="wfbMoreDelete"><i class="fa-solid fa-trash"></i> Delete</div>';
            moreBtn.parentElement.appendChild(dd);
            dd.querySelector('#wfbMoreDuplicate').addEventListener('click', function() {
                dd.remove();
                if (_currentWorkflow) wfbDuplicateWf(_currentWorkflow.id);
            });
            dd.querySelector('#wfbMoreDelete').addEventListener('click', function() {
                dd.remove();
                wfbDeleteCurrent();
            });
            setTimeout(function() {
                document.addEventListener('click', function _cl() { dd.remove(); document.removeEventListener('click', _cl); }, { once: true });
            }, 0);
        });
    }

    window.wfbInit = function() {
        if (_initDone) {
            _loadWorkflows();
            return;
        }
        _initDone = true;
        _setupButtonEvents();
        _setupCanvasEvents();
        _setupNodeEvents();
        _setupPaletteEvents();
        _setupKeyboard();
        _loadWorkflows();
        _loadCustomActions();
    };

    // Auto-init when workflows tab becomes visible
    var _observer = new MutationObserver(function(mutations) {
        mutations.forEach(function(m) {
            if (m.target.id === 'workflows' && m.target.classList.contains('show')) {
                wfbInit();
            }
        });
    });
    var wfTab = document.getElementById('workflows');
    if (wfTab) _observer.observe(wfTab, { attributes: true, attributeFilter: ['class'] });

    // Also init if already visible
    if (wfTab && wfTab.classList.contains('show')) wfbInit();

})();
