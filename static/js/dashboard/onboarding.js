/* ================================================================
   ONBOARDING WIZARD
   Intro animation → 4-step setup → Closing tagline → Workspace
   Self-gates via DASHBOARD_BOOT. Saves profile locally.
   ================================================================ */
(function () {
    'use strict';

    /* ── Gate ── */
    var BOOT = window.DASHBOARD_BOOT;
    if (!BOOT || !BOOT.needsOnboarding) return;

    var currentStep = 1;
    var TOTAL_STEPS = 4;
    var selectedNumbers = [];
    var selectedDomain = '';
    var webPath = 'have'; // 'have' or 'need'
    var saving = false;
    var introPlayed = sessionStorage.getItem('onb_intro_done') === '1';

    /* ── Restore step from sessionStorage ── */
    var saved = parseInt(sessionStorage.getItem('onb_step'), 10);
    if (saved && saved >= 1 && saved <= TOTAL_STEPS) {
        currentStep = saved;
    }

    /* ── Init on DOM ready ── */
    document.addEventListener('DOMContentLoaded', function () {

        // If voice not provisioned, show the alternate message on step 4
        if (!BOOT.voipSetupDone) {
            var ready = document.getElementById('onbNumbersReady');
            var notReady = document.getElementById('onbNumbersNotReady');
            if (ready) ready.style.display = 'none';
            if (notReady) notReady.style.display = 'block';
            var btn = document.getElementById('onbGetNumbersBtn');
            if (btn) {
                btn.textContent = 'Continue ';
                var icon = document.createElement('i');
                icon.className = 'fa-solid fa-arrow-right';
                btn.appendChild(icon);
                btn.onclick = function () { onbFinish(); };
            }
        }

        // Phase 1: Play intro or skip to setup
        if (!introPlayed) {
            playIntro();
        } else {
            skipToSetup();
        }
    });

    /* ════════════════════════════════════════════════════════════════
       PHASE 1: INTRO ANIMATION — "Welcome to Omnisconn"
       ════════════════════════════════════════════════════════════════ */

    function playIntro() {
        var intro = document.getElementById('onbIntro');
        var wizard = document.getElementById('onboardingWizard');
        if (!intro || !wizard) { skipToSetup(); return; }

        // Intro animations: brand finishes ~2.0s, tagline ~2.1s
        // Hold the full display for ~1.5s before fading
        setTimeout(function () {
            intro.classList.add('fade-out');
            sessionStorage.setItem('onb_intro_done', '1');
            introPlayed = true;

            // After fade-out transition (0.8s), show wizard
            setTimeout(function () {
                intro.style.display = 'none';
                wizard.style.display = '';
                goToStep(currentStep, true);
            }, 800);
        }, 3600);
    }

    function skipToSetup() {
        var intro = document.getElementById('onbIntro');
        var wizard = document.getElementById('onboardingWizard');
        if (intro) intro.style.display = 'none';
        if (wizard) wizard.style.display = '';
        goToStep(currentStep, true);
    }

    /* ════════════════════════════════════════════════════════════════
       NAVIGATION
       ════════════════════════════════════════════════════════════════ */

    function goToStep(step, instant) {
        currentStep = step;
        sessionStorage.setItem('onb_step', step);

        var slides = document.getElementById('onbSlides');
        if (!slides) return;

        if (instant) {
            slides.style.transition = 'none';
            slides.style.transform = 'translateX(-' + ((step - 1) * 100) + '%)';
            slides.offsetHeight; // force reflow
            slides.style.transition = '';
        } else {
            slides.style.transform = 'translateX(-' + ((step - 1) * 100) + '%)';
        }

        // Update dots
        var dots = document.querySelectorAll('.onb-dot');
        dots.forEach(function (dot) {
            var ds = parseInt(dot.dataset.step, 10);
            dot.classList.toggle('active', ds === step);
            dot.classList.toggle('done', ds < step);
        });

        // Update step label
        var lbl = document.getElementById('onbStepNum');
        if (lbl) lbl.textContent = step;
    }

    window.onbNext = function () {
        if (saving) return;
        if (!validateStep(currentStep)) return;

        // Step 3: save business profile before advancing
        if (currentStep === 3) {
            saveProfile(function () {
                goToStep(4);
            });
            return;
        }

        if (currentStep < TOTAL_STEPS) {
            goToStep(currentStep + 1);
        }
    };

    window.onbPrev = function () {
        if (currentStep > 1) {
            goToStep(currentStep - 1);
        }
    };

    /* ════════════════════════════════════════════════════════════════
       VALIDATION
       ════════════════════════════════════════════════════════════════ */

    function validateStep(step) {
        clearErrors();

        if (step === 1) {
            var name = val('onbBizName');
            var type = val('onbBizType');
            var ein = val('onbEIN');
            var ok = true;
            if (!name) { markError('onbBizName'); ok = false; }
            if (!type) { markError('onbBizType'); ok = false; }
            if (!ein) { markError('onbEIN'); ok = false; }
            return ok;
        }

        if (step === 2) {
            var fields = ['onbStreet', 'onbCity', 'onbState', 'onbZip', 'onbContactName', 'onbContactEmail'];
            var ok2 = true;
            fields.forEach(function (id) {
                if (!val(id)) { markError(id); ok2 = false; }
            });
            return ok2;
        }

        if (step === 3) {
            if (webPath === 'have') {
                var url = val('onbWebsite');
                if (!url) { markError('onbWebsite'); return false; }
            }
            return true;
        }

        return true;
    }

    function val(id) {
        var el = document.getElementById(id);
        return el ? el.value.trim() : '';
    }

    function markError(id) {
        var el = document.getElementById(id);
        if (el) el.classList.add('onb-error');
    }

    function clearErrors() {
        document.querySelectorAll('.onb-error').forEach(function (el) {
            el.classList.remove('onb-error');
        });
    }

    /* ════════════════════════════════════════════════════════════════
       SAVE BUSINESS PROFILE (Steps 1-3 → /voice/trust-hub/save)
       Local save only — no Twilio API calls.
       ════════════════════════════════════════════════════════════════ */

    function saveProfile(callback) {
        saving = true;
        setBtnLoading('onbStep3Btn', true);

        var website = webPath === 'have' ? val('onbWebsite') : '';
        if (webPath === 'need' && selectedDomain) {
            website = 'https://' + selectedDomain;
        }

        var data = {
            business_name:  val('onbBizName'),
            business_type:  val('onbBizType'),
            ein:            val('onbEIN'),
            street:         val('onbStreet'),
            city:           val('onbCity'),
            state:          val('onbState'),
            zip:            val('onbZip'),
            website:        website,
            contact_name:   val('onbContactName'),
            contact_title:  val('onbContactTitle'),
            contact_email:  val('onbContactEmail'),
            contact_phone:  val('onbContactPhone')
        };

        fetch('/voice/trust-hub/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        })
        .then(function (r) { return r.json(); })
        .then(function (result) {
            saving = false;
            setBtnLoading('onbStep3Btn', false);
            if (result.error) {
                if (typeof _showDashToast === 'function') _showDashToast(false, result.error);
                return;
            }
            if (callback) callback();
        })
        .catch(function () {
            saving = false;
            setBtnLoading('onbStep3Btn', false);
            if (typeof _showDashToast === 'function') _showDashToast(false, 'Save failed. Please try again.');
        });
    }

    /* ════════════════════════════════════════════════════════════════
       STEP 3: WEBSITE / DOMAIN
       ════════════════════════════════════════════════════════════════ */

    window.onbWebPath = function (path) {
        webPath = path;
        var haveBtn = document.getElementById('onbPathHave');
        var needBtn = document.getElementById('onbPathNeed');
        var haveDiv = document.getElementById('onbWebHave');
        var needDiv = document.getElementById('onbWebNeed');

        if (path === 'have') {
            haveBtn.classList.add('selected');
            needBtn.classList.remove('selected');
            haveDiv.style.display = '';
            needDiv.style.display = 'none';
        } else {
            haveBtn.classList.remove('selected');
            needBtn.classList.add('selected');
            haveDiv.style.display = 'none';
            needDiv.style.display = '';
        }
    };

    window.onbSearchDomain = function () {
        var query = val('onbDomainSearch');
        if (!query) { markError('onbDomainSearch'); return; }

        var resultsDiv = document.getElementById('onbDomainResults');
        resultsDiv.innerHTML = '<div style="text-align:center;padding:16px;color:var(--text-muted)"><i class="fa-solid fa-spinner fa-spin"></i> Searching...</div>';

        fetch('/api/domain/search', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ dba_name: query })
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (data.error) {
                resultsDiv.innerHTML = '<div class="onb-info"><div class="onb-info-text">' + escHtml(data.error) + '</div></div>';
                return;
            }
            var domains = data.domains || data.results || [];
            if (!domains.length) {
                resultsDiv.innerHTML = '<div class="onb-info"><div class="onb-info-text">No domains found. Try a different name.</div></div>';
                return;
            }
            var html = '';
            domains.forEach(function (d) {
                var name = typeof d === 'string' ? d : (d.domain || d.name || '');
                if (!name) return;
                html += '<div class="onb-domain-option" onclick="onbSelectDomain(this, \'' + escAttr(name) + '\')">';
                html += '<span class="onb-domain-name">' + escHtml(name) + '</span>';
                html += '<span class="onb-domain-avail"><i class="fa-solid fa-circle-check"></i> Available</span>';
                html += '</div>';
            });
            resultsDiv.innerHTML = html;
        })
        .catch(function () {
            resultsDiv.innerHTML = '<div class="onb-info"><div class="onb-info-text">Search failed. Please try again.</div></div>';
        });
    };

    window.onbSelectDomain = function (el, domain) {
        selectedDomain = domain;
        document.getElementById('onbSelectedDomain').value = domain;
        document.querySelectorAll('.onb-domain-option').forEach(function (o) {
            o.classList.remove('selected');
        });
        el.classList.add('selected');
    };

    window.onbSkipDomain = function () {
        selectedDomain = '';
        document.getElementById('onbSelectedDomain').value = '';
        saveProfile(function () { goToStep(4); });
    };

    /* ════════════════════════════════════════════════════════════════
       STEP 4: PHONE NUMBERS
       ════════════════════════════════════════════════════════════════ */

    window.onbSearchNumbers = function () {
        var ac = val('onbAreaCode');
        if (!ac || ac.length < 3) { markError('onbAreaCode'); return; }

        var grid = document.getElementById('onbNumberGrid');
        grid.innerHTML = '<div style="text-align:center;padding:16px;color:var(--text-muted);grid-column:1/-1"><i class="fa-solid fa-spinner fa-spin"></i> Searching...</div>';

        fetch('/voice/numbers/search?area_code=' + encodeURIComponent(ac) + '&number_type=local&limit=10')
        .then(function (r) { return r.json(); })
        .then(function (data) {
            var nums = data.numbers || [];
            if (!nums.length) {
                grid.innerHTML = '<div style="text-align:center;padding:16px;color:var(--text-muted);grid-column:1/-1">No numbers found for that area code. Try another.</div>';
                return;
            }
            selectedNumbers = [];
            var html = '';
            nums.forEach(function (n) {
                var phone = n.phone || n.phoneNumber || n.friendly_name || '';
                var raw = n.phone_number || n.phoneNumber || phone;
                html += '<div class="onb-number-card" onclick="onbToggleNumber(this, \'' + escAttr(raw) + '\')">';
                html += '<span class="onb-number-check"><i class="fa-solid fa-check"></i></span>';
                html += '<span class="onb-number-text">' + escHtml(formatPhone(phone)) + '</span>';
                html += '</div>';
            });
            grid.innerHTML = html;
            updateNumberCount();
        })
        .catch(function () {
            grid.innerHTML = '<div style="text-align:center;padding:16px;color:var(--text-muted);grid-column:1/-1">Search failed. Please try again.</div>';
        });
    };

    window.onbToggleNumber = function (el, phone) {
        var idx = selectedNumbers.indexOf(phone);
        if (idx >= 0) {
            selectedNumbers.splice(idx, 1);
            el.classList.remove('selected');
        } else {
            if (selectedNumbers.length >= 5) return;
            selectedNumbers.push(phone);
            el.classList.add('selected');
        }
        updateNumberCount();
    };

    function updateNumberCount() {
        var el = document.getElementById('onbNumberCount');
        if (!el) return;
        var remaining = 5 - selectedNumbers.length;
        if (remaining > 0) {
            el.innerHTML = 'Selected ' + selectedNumbers.length + ' of 5 — <strong>' + remaining + '</strong> more free';
        } else {
            el.innerHTML = '<strong>5 of 5</strong> selected';
        }
    }

    window.onbGetNumbers = function () {
        if (selectedNumbers.length === 0) {
            onbFinish();
            return;
        }

        saving = true;
        setBtnLoading('onbGetNumbersBtn', true);

        var bought = 0;
        var errors = 0;

        function buyNext() {
            if (bought + errors >= selectedNumbers.length) {
                saving = false;
                setBtnLoading('onbGetNumbersBtn', false);
                onbFinish();
                return;
            }

            var phone = selectedNumbers[bought + errors];
            fetch('/voice/numbers/buy', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ phone_number: phone, number_type: 'local' })
            })
            .then(function (r) { return r.json(); })
            .then(function (result) {
                if (result.error) {
                    errors++;
                } else {
                    bought++;
                }
                buyNext();
            })
            .catch(function () {
                errors++;
                buyNext();
            });
        }

        buyNext();
    };

    window.onbSkipNumbers = function () {
        onbFinish();
    };

    /* ════════════════════════════════════════════════════════════════
       PHASE 3: CLOSING — "Connect everything. Close everyone."
       Screen goes dark → tagline fades in → screen brightens → workspace
       ════════════════════════════════════════════════════════════════ */

    function onbFinish() {
        var wizard = document.getElementById('onboardingWizard');
        var closing = document.getElementById('onbClosing');
        var closingLine = document.getElementById('onbClosingLine');

        if (!closing || !closingLine) {
            // Fallback: just reload
            sessionStorage.removeItem('onb_step');
            sessionStorage.removeItem('onb_intro_done');
            window.location.reload();
            return;
        }

        // Hide wizard
        if (wizard) wizard.style.display = 'none';

        // Show closing overlay (dark, opacity 0 → 1)
        closing.style.display = '';
        // Force reflow before adding class
        closing.offsetHeight; // eslint-disable-line no-unused-expressions
        closing.classList.add('visible');

        // After dark screen settles, animate the tagline
        setTimeout(function () {
            closingLine.classList.add('animate');
        }, 600);

        // Hold the tagline for 2.5 seconds, then brighten and fade
        setTimeout(function () {
            closing.classList.add('brightening');

            // After brighten transition, reload to workspace
            setTimeout(function () {
                sessionStorage.removeItem('onb_step');
                sessionStorage.removeItem('onb_intro_done');
                if (selectedDomain) {
                    window.location.href = '/dashboard?tab=domain&onb_domain=' + encodeURIComponent(selectedDomain);
                } else {
                    window.location.reload();
                }
            }, 1000);
        }, 3200);
    }

    /* ════════════════════════════════════════════════════════════════
       HELPERS
       ════════════════════════════════════════════════════════════════ */

    window.onbToggle = function (id) {
        var el = document.getElementById(id);
        if (el) el.classList.toggle('open');
    };

    function setBtnLoading(id, loading) {
        var btn = document.getElementById(id);
        if (!btn) return;
        if (loading) {
            btn.disabled = true;
            btn._origHtml = btn.innerHTML;
            btn.innerHTML = '<span class="onb-spinner"></span> Saving...';
        } else {
            btn.disabled = false;
            if (btn._origHtml) btn.innerHTML = btn._origHtml;
        }
    }

    function formatPhone(p) {
        var d = (p || '').replace(/\D/g, '');
        if (d.length === 11 && d[0] === '1') d = d.slice(1);
        if (d.length === 10) {
            return '(' + d.slice(0, 3) + ') ' + d.slice(3, 6) + '-' + d.slice(6);
        }
        return p;
    }

    function escHtml(s) {
        var d = document.createElement('div');
        d.appendChild(document.createTextNode(s));
        return d.innerHTML;
    }

    function escAttr(s) {
        return s.replace(/'/g, "\\'").replace(/"/g, '&quot;');
    }

})();
