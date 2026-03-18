// whitelabel.js — White-label branding management for agency owners
// Handles logo upload, company name styling, and save/load operations.

(function() {
    'use strict';

    // ── State ────────────────────────────────────────────────────────────────
    var _wlState = {
        bold: false,
        italic: false,
        underline: false,
        font: '',
    };

    // ── Init: load existing config into UI ───────────────────────────────────
    window.wlInit = function() {
        fetch('/api/agency/whitelabel')
            .then(function(r) { return r.json(); })
            .then(function(data) {
                var wl = data.whitelabel || {};
                if (wl.company_name) {
                    document.getElementById('wlCompanyName').value = wl.company_name;
                }
                if (wl.logo_url) {
                    document.getElementById('wlLogoUrl').value = wl.logo_url;
                }
                if (wl.name_font) {
                    document.getElementById('wlNameFont').value = wl.name_font;
                    _wlState.font = wl.name_font;
                }
                if (wl.name_bold) {
                    _wlState.bold = true;
                    document.getElementById('wlBoldBtn').classList.add('active');
                }
                if (wl.name_italic) {
                    _wlState.italic = true;
                    document.getElementById('wlItalicBtn').classList.add('active');
                }
                if (wl.name_underline) {
                    _wlState.underline = true;
                    document.getElementById('wlUnderlineBtn').classList.add('active');
                }
                _wlUpdatePreview();
            })
            .catch(function(e) { console.warn('wlInit error:', e); });

        // Wire up events
        var nameInput = document.getElementById('wlCompanyName');
        if (nameInput) {
            nameInput.addEventListener('input', _wlUpdatePreview);
        }
        var fontSelect = document.getElementById('wlNameFont');
        if (fontSelect) {
            fontSelect.addEventListener('change', function() {
                _wlState.font = this.value;
                // Load Google Font dynamically
                if (this.value) {
                    var link = document.createElement('link');
                    link.rel = 'stylesheet';
                    link.href = 'https://fonts.googleapis.com/css2?family=' +
                                encodeURIComponent(this.value) + ':wght@400;700&display=swap';
                    document.head.appendChild(link);
                }
                _wlUpdatePreview();
            });
        }

        // Logo file upload
        var dropZone = document.getElementById('wlLogoDropZone');
        var fileInput = document.getElementById('wlLogoFile');
        if (dropZone && fileInput) {
            dropZone.addEventListener('click', function() { fileInput.click(); });
            fileInput.addEventListener('change', function() {
                if (this.files && this.files[0]) {
                    _wlHandleLogoFile(this.files[0]);
                }
            });
            // Drag & drop
            dropZone.addEventListener('dragover', function(e) {
                e.preventDefault();
                e.stopPropagation();
                this.querySelector('.wl-logo-preview').style.borderColor = 'var(--accent)';
            });
            dropZone.addEventListener('dragleave', function(e) {
                e.preventDefault();
                this.querySelector('.wl-logo-preview').style.borderColor = '';
            });
            dropZone.addEventListener('drop', function(e) {
                e.preventDefault();
                e.stopPropagation();
                this.querySelector('.wl-logo-preview').style.borderColor = '';
                if (e.dataTransfer.files && e.dataTransfer.files[0]) {
                    _wlHandleLogoFile(e.dataTransfer.files[0]);
                }
            });
        }

        // Logo URL paste
        var logoUrlInput = document.getElementById('wlLogoUrl');
        if (logoUrlInput) {
            logoUrlInput.addEventListener('change', function() {
                var url = this.value.trim();
                if (url && (url.startsWith('https://') || url.startsWith('http://'))) {
                    _wlShowLogoPreview(url);
                }
            });
        }
    };

    // ── Toggle bold/italic/underline ─────────────────────────────────────────
    window.wlToggleStyle = function(style) {
        _wlState[style] = !_wlState[style];
        var btnId = 'wl' + style.charAt(0).toUpperCase() + style.slice(1) + 'Btn';
        var btn = document.getElementById(btnId);
        if (btn) {
            btn.classList.toggle('active', _wlState[style]);
        }
        _wlUpdatePreview();
    };

    // ── Update name preview ──────────────────────────────────────────────────
    function _wlUpdatePreview() {
        var text = (document.getElementById('wlCompanyName').value || '').trim() || 'Your Agency Name';
        var el = document.getElementById('wlPreviewText');
        if (!el) return;
        el.textContent = text;
        el.style.fontFamily = _wlState.font ? ("'" + _wlState.font + "', sans-serif") : '';
        el.style.fontWeight = _wlState.bold ? '700' : '400';
        el.style.fontStyle = _wlState.italic ? 'italic' : 'normal';
        el.style.textDecoration = _wlState.underline ? 'underline' : 'none';
    }

    // ── Handle logo file (convert to data URL for preview, store URL) ────────
    function _wlHandleLogoFile(file) {
        // Validate
        var maxSize = 2 * 1024 * 1024; // 2MB
        if (file.size > maxSize) {
            if (typeof _showDashToast === 'function') _showDashToast(false, 'Logo must be under 2MB');
            return;
        }
        var validTypes = ['image/png', 'image/svg+xml', 'image/jpeg', 'image/webp'];
        if (validTypes.indexOf(file.type) === -1) {
            if (typeof _showDashToast === 'function') _showDashToast(false, 'Invalid file type. Use PNG, SVG, JPG, or WebP.');
            return;
        }

        // Validate dimensions
        var reader = new FileReader();
        reader.onload = function(e) {
            var img = new Image();
            img.onload = function() {
                if (img.width > 400 || img.height > 120) {
                    if (typeof _showDashToast === 'function') {
                        _showDashToast(false, 'Logo too large: max 400x120px. Yours is ' + img.width + 'x' + img.height + 'px.');
                    }
                    return;
                }
                // Show preview
                _wlShowLogoPreview(e.target.result);
                // Store the data URL — on save, the backend will need a real URL
                // For now, users should paste a hosted URL instead
                document.getElementById('wlLogoUrl').value = '';
                if (typeof _showDashToast === 'function') {
                    _showDashToast(true, 'Logo preview loaded. Paste a hosted URL to save permanently.');
                }
            };
            img.src = e.target.result;
        };
        reader.readAsDataURL(file);
    }

    function _wlShowLogoPreview(url) {
        var preview = document.getElementById('wlLogoPreview');
        if (!preview) return;
        preview.innerHTML = '<img src="' + url + '" alt="Logo preview" id="wlLogoImg">' +
            '<button type="button" class="btn btn-sm wl-logo-remove" onclick="wlRemoveLogo()" title="Remove logo">' +
            '<i class="fa-solid fa-xmark"></i></button>';
    }

    window.wlRemoveLogo = function() {
        var preview = document.getElementById('wlLogoPreview');
        if (preview) {
            preview.innerHTML =
                '<div class="wl-logo-placeholder" id="wlLogoPlaceholder">' +
                '<i class="fa-solid fa-cloud-arrow-up"></i>' +
                '<span>Drop logo here or click to upload</span>' +
                '<small>PNG, SVG, JPG — max 2MB, 400x120px</small>' +
                '</div>';
        }
        document.getElementById('wlLogoUrl').value = '';
    };

    // ── Save ─────────────────────────────────────────────────────────────────
    window.wlSave = function() {
        var companyName = (document.getElementById('wlCompanyName').value || '').trim();
        var logoUrl = (document.getElementById('wlLogoUrl').value || '').trim();
        var nameFont = document.getElementById('wlNameFont').value;

        if (!companyName && !logoUrl) {
            if (typeof _showDashToast === 'function') _showDashToast(false, 'Enter a company name or logo URL.');
            return;
        }

        var payload = {
            company_name: companyName || undefined,
            logo_url: logoUrl || undefined,
            name_font: nameFont || undefined,
            name_bold: _wlState.bold,
            name_italic: _wlState.italic,
            name_underline: _wlState.underline,
        };

        fetch('/api/agency/whitelabel', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.status === 'ok') {
                if (typeof _showDashToast === 'function') _showDashToast(true, 'Branding saved! Reload to see changes.');
            } else {
                if (typeof _showDashToast === 'function') _showDashToast(false, data.error || 'Save failed');
            }
        })
        .catch(function(e) {
            if (typeof _showDashToast === 'function') _showDashToast(false, 'Network error: ' + e.message);
        });
    };

})();
