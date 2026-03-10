// ── PWA: Service Worker Registration + Install Prompt ──────────────────────

(function() {
    'use strict';

    // ── Register Service Worker ──
    if ('serviceWorker' in navigator) {
        window.addEventListener('load', function() {
            navigator.serviceWorker.register('/sw.js')
                .then(function(reg) {
                    console.log('[PWA] SW registered, scope:', reg.scope);
                })
                .catch(function(err) {
                    console.warn('[PWA] SW registration failed:', err);
                });
        });
    }

    // ── Install Prompt (Android / Desktop Chrome) ──
    var _deferredPrompt = null;
    var _installDismissed = localStorage.getItem('pwa_install_dismissed');

    window.addEventListener('beforeinstallprompt', function(e) {
        e.preventDefault();
        _deferredPrompt = e;

        // Don't show if user dismissed recently (24h cooldown)
        if (_installDismissed) {
            var dismissedAt = parseInt(_installDismissed, 10);
            if (Date.now() - dismissedAt < 86400000) return;
        }

        // Don't show if already installed
        if (window.matchMedia('(display-mode: standalone)').matches) return;
        if (window.navigator.standalone) return;

        showInstallBanner();
    });

    // Detect if running as installed PWA
    window.addEventListener('appinstalled', function() {
        hideInstallBanner();
        _deferredPrompt = null;
        localStorage.removeItem('pwa_install_dismissed');
    });

    function showInstallBanner() {
        if (document.getElementById('pwaBanner')) return;

        var banner = document.createElement('div');
        banner.id = 'pwaBanner';
        banner.innerHTML =
            '<div class="pwa-banner-inner">' +
                '<div class="pwa-banner-icon">' +
                    '<img src="/static/icons/icon-96x96.png" alt="IGB" width="40" height="40" style="border-radius:10px;">' +
                '</div>' +
                '<div class="pwa-banner-text">' +
                    '<strong>Install InsuranceGrokBot</strong>' +
                    '<span>Add to your home screen for quick access</span>' +
                '</div>' +
                '<div class="pwa-banner-actions">' +
                    '<button class="pwa-install-btn" id="pwaBtnInstall">Install</button>' +
                    '<button class="pwa-dismiss-btn" id="pwaBtnDismiss">&times;</button>' +
                '</div>' +
            '</div>';
        document.body.appendChild(banner);

        // Animate in
        requestAnimationFrame(function() {
            requestAnimationFrame(function() {
                banner.classList.add('visible');
            });
        });

        document.getElementById('pwaBtnInstall').addEventListener('click', function() {
            if (_deferredPrompt) {
                _deferredPrompt.prompt();
                _deferredPrompt.userChoice.then(function(result) {
                    if (result.outcome === 'accepted') {
                        hideInstallBanner();
                    }
                    _deferredPrompt = null;
                });
            }
        });

        document.getElementById('pwaBtnDismiss').addEventListener('click', function() {
            hideInstallBanner();
            localStorage.setItem('pwa_install_dismissed', Date.now().toString());
        });
    }

    function hideInstallBanner() {
        var banner = document.getElementById('pwaBanner');
        if (banner) {
            banner.classList.remove('visible');
            setTimeout(function() { banner.remove(); }, 350);
        }
    }

    // ── iOS: Show manual "Add to Home Screen" prompt ──
    // iOS doesn't fire beforeinstallprompt, so we detect Safari and show a hint
    var isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) && !window.MSStream;
    var isSafari = /Safari/.test(navigator.userAgent) && !/CriOS|FxiOS|Chrome/.test(navigator.userAgent);
    var isStandalone = window.navigator.standalone || window.matchMedia('(display-mode: standalone)').matches;

    if (isIOS && isSafari && !isStandalone) {
        var iosDismissed = localStorage.getItem('pwa_ios_dismissed');
        if (!iosDismissed || (Date.now() - parseInt(iosDismissed, 10)) > 86400000 * 7) {
            // Show after a short delay so it doesn't interrupt initial load
            setTimeout(function() {
                showIOSPrompt();
            }, 3000);
        }
    }

    function showIOSPrompt() {
        if (document.getElementById('pwaBanner')) return;

        var banner = document.createElement('div');
        banner.id = 'pwaBanner';
        banner.innerHTML =
            '<div class="pwa-banner-inner">' +
                '<div class="pwa-banner-icon">' +
                    '<img src="/static/icons/icon-96x96.png" alt="IGB" width="40" height="40" style="border-radius:10px;">' +
                '</div>' +
                '<div class="pwa-banner-text">' +
                    '<strong>Install InsuranceGrokBot</strong>' +
                    '<span>Tap <i class="fa-solid fa-arrow-up-from-bracket" style="color:var(--accent);"></i> then "Add to Home Screen"</span>' +
                '</div>' +
                '<div class="pwa-banner-actions">' +
                    '<button class="pwa-dismiss-btn" id="pwaBtnDismiss">&times;</button>' +
                '</div>' +
            '</div>';
        document.body.appendChild(banner);

        requestAnimationFrame(function() {
            requestAnimationFrame(function() {
                banner.classList.add('visible');
            });
        });

        document.getElementById('pwaBtnDismiss').addEventListener('click', function() {
            hideInstallBanner();
            localStorage.setItem('pwa_ios_dismissed', Date.now().toString());
        });
    }

})();
