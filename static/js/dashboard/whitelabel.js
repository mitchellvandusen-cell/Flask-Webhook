// whitelabel.js — White-label branding management for agency owners
// Handles enable/disable toggle, logo, company name, accent color, dashboard font.

(function() {
    'use strict';

    var BRAND_SUFFIX = ' AI Dialer & SMS';

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
                // Accent color
                if (wl.accent_color) {
                    var colorPicker = document.getElementById('wlAccentColor');
                    var colorHex = document.getElementById('wlAccentColorHex');
                    if (colorPicker) colorPicker.value = wl.accent_color;
                    if (colorHex) colorHex.value = wl.accent_color;
                }
                // Dashboard font
                if (wl.font_family) {
                    var dashFont = document.getElementById('wlDashFont');
                    if (dashFont) dashFont.value = wl.font_family;
                }
                // Enabled toggle
                var toggle = document.getElementById('wlEnabled');
                if (toggle && wl.enabled) {
                    toggle.checked = true;
                    _wlSetBodyEnabled(true);
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

        // Accent color sync between picker and hex input
        var colorPicker = document.getElementById('wlAccentColor');
        var colorHex = document.getElementById('wlAccentColorHex');
        if (colorPicker && colorHex) {
            colorPicker.addEventListener('input', function() {
                colorHex.value = this.value;
            });
            colorHex.addEventListener('input', function() {
                var v = this.value.trim();
                if (/^#[0-9a-fA-F]{6}$/.test(v)) {
                    colorPicker.value = v;
                }
            });
        }

        // Dashboard font preview
        var dashFontSelect = document.getElementById('wlDashFont');
        if (dashFontSelect) {
            dashFontSelect.addEventListener('change', function() {
                if (this.value) {
                    var link = document.createElement('link');
                    link.rel = 'stylesheet';
                    link.href = 'https://fonts.googleapis.com/css2?family=' +
                                encodeURIComponent(this.value) + ':wght@300;400;500;600;700&display=swap';
                    document.head.appendChild(link);
                }
            });
        }

        // Logo file upload
        var dropZone = document.getElementById('wlLogoDropZone');
        var fileInput = document.getElementById('wlLogoFile');
        if (dropZone && fileInput) {
            dropZone.addEventListener('click', function() { fileInput.click(); });
            fileInput.addEventListener('change', function() {
                if (this.files && this.files[0]) _wlHandleLogoFile(this.files[0]);
            });
            dropZone.addEventListener('dragover', function(e) {
                e.preventDefault(); e.stopPropagation();
                this.querySelector('.wl-logo-preview').style.borderColor = 'var(--accent)';
            });
            dropZone.addEventListener('dragleave', function(e) {
                e.preventDefault();
                this.querySelector('.wl-logo-preview').style.borderColor = '';
            });
            dropZone.addEventListener('drop', function(e) {
                e.preventDefault(); e.stopPropagation();
                this.querySelector('.wl-logo-preview').style.borderColor = '';
                if (e.dataTransfer.files && e.dataTransfer.files[0]) _wlHandleLogoFile(e.dataTransfer.files[0]);
            });
        }

        // Logo URL paste
        var logoUrlInput = document.getElementById('wlLogoUrl');
        if (logoUrlInput) {
            logoUrlInput.addEventListener('change', function() {
                var url = this.value.trim();
                if (url && (url.startsWith('https://') || url.startsWith('http://'))) _wlShowLogoPreview(url);
            });
        }
    };

    // ── Enable/disable toggle ────────────────────────────────────────────────
    window.wlToggleEnabled = function() {
        var checked = document.getElementById('wlEnabled').checked;
        _wlSetBodyEnabled(checked);
        // Auto-save the toggle immediately
        fetch('/api/agency/whitelabel', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: checked }),
        })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.status === 'ok') {
                if (typeof _showDashToast === 'function')
                    _showDashToast(true, checked ? 'White label enabled. Reload to see changes.' : 'White label disabled. Reload to see default branding.');
            }
        })
        .catch(function() {});
    };

    function _wlSetBodyEnabled(enabled) {
        var body = document.getElementById('wlConfigBody');
        if (body) {
            body.style.opacity = enabled ? '' : '0.4';
            body.style.pointerEvents = enabled ? '' : 'none';
        }
    }

    // ── Toggle bold/italic/underline ─────────────────────────────────────────
    window.wlToggleStyle = function(style) {
        _wlState[style] = !_wlState[style];
        var btnId = 'wl' + style.charAt(0).toUpperCase() + style.slice(1) + 'Btn';
        var btn = document.getElementById(btnId);
        if (btn) btn.classList.toggle('active', _wlState[style]);
        _wlUpdatePreview();
    };

    // ── Update accent color preview ──────────────────────────────────────────
    window.wlUpdateAccentPreview = function() {
        var colorPicker = document.getElementById('wlAccentColor');
        var colorHex = document.getElementById('wlAccentColorHex');
        if (colorPicker && colorHex) colorHex.value = colorPicker.value;
    };

    // ── Update name preview ──────────────────────────────────────────────────
    function _wlUpdatePreview() {
        var name = (document.getElementById('wlCompanyName').value || '').trim() || 'Your Agency Name';
        var el = document.getElementById('wlPreviewText');
        if (!el) return;
        el.textContent = name + BRAND_SUFFIX;
        el.style.fontFamily = _wlState.font ? ("'" + _wlState.font + "', sans-serif") : '';
        el.style.fontWeight = _wlState.bold ? '700' : '400';
        el.style.fontStyle = _wlState.italic ? 'italic' : 'normal';
        el.style.textDecoration = _wlState.underline ? 'underline' : 'none';
    }

    // ── Handle logo file ─────────────────────────────────────────────────────
    function _wlHandleLogoFile(file) {
        var maxSize = 2 * 1024 * 1024;
        if (file.size > maxSize) {
            if (typeof _showDashToast === 'function') _showDashToast(false, 'Logo must be under 2MB');
            return;
        }
        var validTypes = ['image/png', 'image/svg+xml', 'image/jpeg', 'image/webp'];
        if (validTypes.indexOf(file.type) === -1) {
            if (typeof _showDashToast === 'function') _showDashToast(false, 'Invalid file type. Use PNG, SVG, JPG, or WebP.');
            return;
        }
        var reader = new FileReader();
        reader.onload = function(e) {
            var img = new Image();
            img.onload = function() {
                if (img.width > 400 || img.height > 120) {
                    if (typeof _showDashToast === 'function')
                        _showDashToast(false, 'Logo too large: max 400x120px. Yours is ' + img.width + 'x' + img.height + 'px.');
                    return;
                }
                _wlShowLogoPreview(e.target.result);
                document.getElementById('wlLogoUrl').value = '';
                if (typeof _showDashToast === 'function')
                    _showDashToast(true, 'Logo preview loaded. Paste a hosted URL to save permanently.');
            };
            img.src = e.target.result;
        };
        reader.readAsDataURL(file);
    }

    function _wlShowLogoPreview(url) {
        var preview = document.getElementById('wlLogoPreview');
        if (!preview) return;
        preview.innerHTML = '';
        var img = document.createElement('img');
        img.src = url; img.alt = 'Logo preview'; img.id = 'wlLogoImg';
        preview.appendChild(img);
        var btn = document.createElement('button');
        btn.type = 'button'; btn.className = 'btn btn-sm wl-logo-remove'; btn.title = 'Remove logo';
        btn.onclick = function() { wlRemoveLogo(); };
        var icon = document.createElement('i'); icon.className = 'fa-solid fa-xmark';
        btn.appendChild(icon); preview.appendChild(btn);
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
        var accentColor = (document.getElementById('wlAccentColorHex').value || '').trim();
        var dashFont = document.getElementById('wlDashFont').value;
        var enabled = document.getElementById('wlEnabled').checked;

        if (accentColor && !/^#[0-9a-fA-F]{6}$/.test(accentColor)) {
            if (typeof _showDashToast === 'function') _showDashToast(false, 'Invalid color format. Use #RRGGBB.');
            return;
        }

        var payload = {
            enabled: enabled,
            company_name: companyName || undefined,
            logo_url: logoUrl || undefined,
            name_font: nameFont || undefined,
            name_bold: _wlState.bold,
            name_italic: _wlState.italic,
            name_underline: _wlState.underline,
            accent_color: accentColor || undefined,
            font_family: dashFont || undefined,
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

    // ── Apply white-label overrides on page load ─────────────────────────────
    window.wlApplyOverrides = function(wl) {
        if (!wl || !wl.enabled) return;

        // Accent color override
        if (wl.accent_color) {
            document.documentElement.style.setProperty('--accent', wl.accent_color);
            var r = parseInt(wl.accent_color.slice(1, 3), 16);
            var g = parseInt(wl.accent_color.slice(3, 5), 16);
            var b = parseInt(wl.accent_color.slice(5, 7), 16);
            document.documentElement.style.setProperty('--accent-dim', 'rgba(' + r + ',' + g + ',' + b + ',0.12)');
        }

        // Dashboard font override
        if (wl.font_family) {
            var link = document.createElement('link');
            link.rel = 'stylesheet';
            link.href = 'https://fonts.googleapis.com/css2?family=' +
                        encodeURIComponent(wl.font_family) + ':wght@300;400;500;600;700;800&display=swap';
            document.head.appendChild(link);
            document.documentElement.style.setProperty('--font-primary', "'" + wl.font_family + "', sans-serif");
            document.body.style.fontFamily = "'" + wl.font_family + "', sans-serif";
        }
    };

})();
