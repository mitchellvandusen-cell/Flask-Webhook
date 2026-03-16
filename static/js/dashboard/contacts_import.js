// contacts_import.js — Contact Import Modal (CSV/Excel/TXT → GHL)
// Wizard flow: Upload → Map → Options → Import → Results

(function() {
    'use strict';

    let _importId = null;
    let _step = 1;
    let _uploadData = null;  // {headers, auto_mapping, preview, ghl_fields, total_rows}
    let _mapping = {};
    let _pollTimer = null;

    // ── Open / Close ──────────────────────────────────────────────────────
    window.ciOpen = function() {
        _importId = null;
        _step = 1;
        _uploadData = null;
        _mapping = {};
        if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }

        document.getElementById('ciModal').classList.add('ci-modal-open');
        _showStep(1);
        _resetUpload();
        _loadHistory();
    };

    window.ciClose = function() {
        document.getElementById('ciModal').classList.remove('ci-modal-open');
        if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
    };

    // ── Step Navigation ───────────────────────────────────────────────────
    window.ciNextStep = function() {
        if (_step === 1) {
            if (!_uploadData) {
                _showDashToast(false, 'Please upload a file first');
                return;
            }
            _buildMappingTable();
            _showStep(2);
        } else if (_step === 2) {
            _mapping = _collectMapping();
            var mapped = Object.values(_mapping);
            if (mapped.indexOf('phone') === -1 && mapped.indexOf('email') === -1) {
                _showDashToast(false, 'Map at least Phone or Email');
                return;
            }
            _buildPreview();
            _showStep(3);
        } else if (_step === 3) {
            _startImport();
        } else if (_step === 5) {
            ciClose();
        }
    };

    window.ciPrevStep = function() {
        if (_step > 1 && _step < 4) _showStep(_step - 1);
    };

    function _showStep(n) {
        _step = n;
        for (var i = 1; i <= 5; i++) {
            var el = document.getElementById('ciStep' + i);
            if (el) {
                el.classList.toggle('ci-hidden', i !== n);
                el.classList.toggle('ci-step-visible', i === n);
            }
        }
        // Step dots
        document.querySelectorAll('.ci-step-dot').forEach(function(dot) {
            var ds = parseInt(dot.getAttribute('data-step'));
            dot.classList.toggle('ci-step-active', ds <= n);
            dot.classList.toggle('ci-step-done', ds < n);
        });
        // Buttons
        var back = document.getElementById('ciBtnBack');
        var next = document.getElementById('ciBtnNext');
        back.classList.toggle('ci-hidden', n <= 1 || n >= 4);
        if (n === 1) { next.textContent = 'Continue'; next.classList.remove('ci-hidden'); }
        else if (n === 2) { next.textContent = 'Continue'; next.classList.remove('ci-hidden'); }
        else if (n === 3) { next.textContent = 'Start Import'; next.classList.remove('ci-hidden'); }
        else if (n === 4) { next.classList.add('ci-hidden'); }
        else if (n === 5) { next.textContent = 'Done'; next.classList.remove('ci-hidden'); }
    }

    // ── File Upload ───────────────────────────────────────────────────────
    var dropZone = document.getElementById('ciDropZone');
    var fileInput = document.getElementById('ciFileInput');

    if (dropZone) {
        dropZone.addEventListener('click', function() { fileInput.click(); });
        dropZone.addEventListener('dragover', function(e) {
            e.preventDefault(); dropZone.classList.add('ci-drop-active');
        });
        dropZone.addEventListener('dragleave', function() {
            dropZone.classList.remove('ci-drop-active');
        });
        dropZone.addEventListener('drop', function(e) {
            e.preventDefault(); dropZone.classList.remove('ci-drop-active');
            if (e.dataTransfer.files.length) _handleFile(e.dataTransfer.files[0]);
        });
    }
    if (fileInput) {
        fileInput.addEventListener('change', function() {
            if (this.files.length) _handleFile(this.files[0]);
        });
    }

    function _handleFile(file) {
        var ext = file.name.split('.').pop().toLowerCase();
        if (['csv', 'xlsx', 'xls', 'txt', 'tsv'].indexOf(ext) === -1) {
            _showDashToast(false, 'Unsupported file type');
            return;
        }
        if (file.size > 10 * 1024 * 1024) {
            _showDashToast(false, 'File too large (max 10MB)');
            return;
        }

        var fd = new FormData();
        fd.append('file', file);

        dropZone.classList.add('ci-uploading');
        dropZone.querySelector('.ci-drop-title').textContent = 'Uploading...';

        fetch('/api/contacts/upload', { method: 'POST', body: fd })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                dropZone.classList.remove('ci-uploading');
                if (data.error) {
                    _showDashToast(false, data.error);
                    _resetUpload();
                    return;
                }
                _importId = data.import_id;
                _uploadData = data;
                _mapping = data.auto_mapping || {};

                dropZone.classList.add('ci-hidden');
                var info = document.getElementById('ciFileInfo');
                info.classList.remove('ci-hidden');
                document.getElementById('ciFileName').textContent = data.filename;
                document.getElementById('ciFileRows').textContent = data.total_rows + ' contacts';
            })
            .catch(function(err) {
                dropZone.classList.remove('ci-uploading');
                _showDashToast(false, 'Upload failed: ' + err.message);
                _resetUpload();
            });
    }

    window.ciResetUpload = function() { _resetUpload(); };
    function _resetUpload() {
        _uploadData = null;
        _importId = null;
        _mapping = {};
        var dz = document.getElementById('ciDropZone');
        if (dz) {
            dz.classList.remove('ci-hidden', 'ci-uploading');
            dz.querySelector('.ci-drop-title').textContent = 'Drop your file here';
        }
        var info = document.getElementById('ciFileInfo');
        if (info) info.classList.add('ci-hidden');
        if (fileInput) fileInput.value = '';
    }

    // ── Column Mapping ────────────────────────────────────────────────────
    function _buildMappingTable() {
        var tbody = document.getElementById('ciMappingBody');
        if (!tbody || !_uploadData) return;
        tbody.innerHTML = '';

        var fields = _uploadData.ghl_fields || [];
        _uploadData.headers.forEach(function(h) {
            var tr = document.createElement('tr');
            // Column name
            var td1 = document.createElement('td');
            td1.textContent = h;
            td1.className = 'ci-col-name';
            tr.appendChild(td1);

            // Sample data
            var td2 = document.createElement('td');
            td2.className = 'ci-col-sample';
            var samples = [];
            (_uploadData.preview || []).slice(0, 3).forEach(function(row) {
                if (row[h]) samples.push(row[h]);
            });
            td2.textContent = samples.join(', ').substring(0, 60) || '—';
            tr.appendChild(td2);

            // Dropdown
            var td3 = document.createElement('td');
            var sel = document.createElement('select');
            sel.className = 'ci-map-select';
            sel.setAttribute('data-col', h);
            var opt0 = document.createElement('option');
            opt0.value = '';
            opt0.textContent = '— Skip —';
            sel.appendChild(opt0);
            fields.forEach(function(f) {
                var opt = document.createElement('option');
                opt.value = f.key;
                opt.textContent = f.label;
                if (_mapping[h] === f.key) opt.selected = true;
                sel.appendChild(opt);
            });
            td3.appendChild(sel);
            tr.appendChild(td3);

            tbody.appendChild(tr);
        });
    }

    function _collectMapping() {
        var m = {};
        document.querySelectorAll('.ci-map-select').forEach(function(sel) {
            var col = sel.getAttribute('data-col');
            var val = sel.value;
            if (val) m[col] = val;
        });
        return m;
    }

    // ── Preview ───────────────────────────────────────────────────────────
    function _buildPreview() {
        var table = document.getElementById('ciPreviewTable');
        if (!table || !_uploadData) return;
        table.innerHTML = '';

        var mappedFields = [];
        var mappedHeaders = [];
        for (var col in _mapping) {
            mappedHeaders.push(col);
            var ghl = _mapping[col];
            var label = ghl;
            (_uploadData.ghl_fields || []).forEach(function(f) { if (f.key === ghl) label = f.label; });
            mappedFields.push(label);
        }

        var thead = document.createElement('thead');
        var htr = document.createElement('tr');
        mappedFields.forEach(function(f) {
            var th = document.createElement('th');
            th.textContent = f;
            htr.appendChild(th);
        });
        thead.appendChild(htr);
        table.appendChild(thead);

        var tbody = document.createElement('tbody');
        (_uploadData.preview || []).slice(0, 5).forEach(function(row) {
            var tr = document.createElement('tr');
            mappedHeaders.forEach(function(col) {
                var td = document.createElement('td');
                td.textContent = (row[col] || '').substring(0, 40);
                tr.appendChild(td);
            });
            tbody.appendChild(tr);
        });
        table.appendChild(tbody);
    }

    // ── Start Import ──────────────────────────────────────────────────────
    function _startImport() {
        _showStep(4);
        var tags = (document.getElementById('ciTags').value || '')
            .split(',').map(function(t) { return t.trim(); }).filter(Boolean);
        var dupe = document.querySelector('input[name="ciDupe"]:checked');

        fetch('/api/contacts/import/' + _importId + '/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                column_mapping: _mapping,
                duplicate_strategy: dupe ? dupe.value : 'skip',
                apply_tags: tags,
            }),
        })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.error) {
                _showDashToast(false, data.error);
                _showStep(3);
                return;
            }
            _startPolling();
        })
        .catch(function(err) {
            _showDashToast(false, 'Failed to start import');
            _showStep(3);
        });
    }

    function _startPolling() {
        if (_pollTimer) clearInterval(_pollTimer);
        _pollTimer = setInterval(function() {
            fetch('/api/contacts/import/' + _importId + '/status')
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    if (data.error) return;
                    var total = data.total_rows || 1;
                    var done = (data.imported || 0) + (data.updated || 0) + (data.skipped || 0) + (data.failed || 0);
                    var pct = Math.min(100, Math.round(done / total * 100));

                    document.getElementById('ciProgressBar').style.width = pct + '%';
                    document.getElementById('ciStatImported').textContent = data.imported || 0;
                    document.getElementById('ciStatUpdated').textContent = data.updated || 0;
                    document.getElementById('ciStatSkipped').textContent = data.skipped || 0;
                    document.getElementById('ciStatFailed').textContent = data.failed || 0;

                    if (data.status === 'completed' || data.status === 'failed') {
                        clearInterval(_pollTimer);
                        _pollTimer = null;
                        _showResults(data);
                    }
                });
        }, 2000);
    }

    function _showResults(data) {
        document.getElementById('ciResImported').textContent = data.imported || 0;
        document.getElementById('ciResUpdated').textContent = data.updated || 0;
        document.getElementById('ciResSkipped').textContent = data.skipped || 0;
        document.getElementById('ciResFailed').textContent = data.failed || 0;

        if (data.error_count > 0) {
            document.getElementById('ciErrorsSection').classList.remove('ci-hidden');
        }

        _showStep(5);

        // Refresh dialer contacts if the function exists
        if (typeof window.refreshDialerContacts === 'function') {
            window.refreshDialerContacts();
        }
    }

    // ── Errors ────────────────────────────────────────────────────────────
    window.ciToggleErrors = function() {
        var list = document.getElementById('ciErrorsList');
        if (list.classList.contains('ci-hidden')) {
            list.classList.remove('ci-hidden');
            fetch('/api/contacts/import/' + _importId + '/errors')
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    var html = '';
                    (data.errors || []).forEach(function(e) {
                        html += '<div class="ci-error-row">Row ' + e.row + ': ' + e.error + '</div>';
                    });
                    list.innerHTML = html || '<div class="ci-error-row">No errors</div>';
                });
        } else {
            list.classList.add('ci-hidden');
        }
    };

    // ── History ───────────────────────────────────────────────────────────
    window.ciToggleHistory = function() {
        var el = document.getElementById('ciHistory');
        el.classList.toggle('ci-hidden');
    };

    function _loadHistory() {
        fetch('/api/contacts/imports')
            .then(function(r) { return r.json(); })
            .then(function(data) {
                var el = document.getElementById('ciHistory');
                if (!el) return;
                var imports = data.imports || [];
                if (!imports.length) {
                    el.innerHTML = '<p class="ci-no-history">No previous imports</p>';
                    return;
                }
                var html = '<div class="ci-history-list">';
                imports.slice(0, 5).forEach(function(imp) {
                    var date = new Date(imp.created_at).toLocaleDateString();
                    var status = imp.status === 'completed' ? '<i class="fa-solid fa-check ci-text-success"></i>'
                               : imp.status === 'failed' ? '<i class="fa-solid fa-xmark ci-text-fail"></i>'
                               : '<i class="fa-solid fa-spinner fa-spin"></i>';
                    html += '<div class="ci-history-row">' +
                        status + ' <span class="ci-history-name">' + imp.filename + '</span>' +
                        '<span class="ci-history-meta">' + imp.imported + ' imported, ' + date + '</span></div>';
                });
                html += '</div>';
                el.innerHTML = html;
            });
    }

})();
