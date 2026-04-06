/* ================================================================
   ONBOARDING WIZARD — 6-Step Pre-Qualification + Guided Setup
   Step 1: LLC?  Step 2: EIN?  Step 3: Website?
   Step 4: Address & Contact  Step 5: Review & Submit
   Step 6: Phone Numbers
   Self-gates via DASHBOARD_BOOT.needsOnboarding.
   ================================================================ */
(function () {
    'use strict';

    /* ── Gate ── */
    var BOOT = window.DASHBOARD_BOOT;
    if (!BOOT || !BOOT.needsOnboarding) return;

    var currentStep = 1;
    var TOTAL_STEPS = 6;
    var saving = false;
    var introPlayed = sessionStorage.getItem('onb_intro_done') === '1';

    /* ── Wizard state ── */
    var hasLlc = null;         // true/false/null
    var hasDba = null;         // true/false/null (sole prop DBA question)
    var hasEin = null;         // true/false/null
    var einIsNew = null;       // true/false/null (sole prop 30-day question)
    var hasWebsite = null;     // true/false/null
    var selectedNumbers = [];
    var selectedDomain = '';
    var einFileData = null;    // { data: base64, type: mime, name: filename }

    /* ── Restore step from sessionStorage ── */
    // Only restore step if wizard state (Yes/No answers) is also preserved.
    // Since JS state resets on reload, always restart from step 1 to avoid
    // landing on a step with hidden conditional sections and empty fields.
    // The intro skip IS preserved so they don't re-watch the animation.

    /* ── Init on DOM ready ── */
    document.addEventListener('DOMContentLoaded', function () {
        // If voice not provisioned, show alternate message on step 5
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

        // Enable drag-and-drop on upload area
        var dropArea = document.getElementById('onbUploadArea');
        if (dropArea) {
            ['dragenter', 'dragover'].forEach(function (evt) {
                dropArea.addEventListener(evt, function (e) {
                    e.preventDefault();
                    e.stopPropagation();
                    dropArea.classList.add('drag-over');
                });
            });
            ['dragleave', 'drop'].forEach(function (evt) {
                dropArea.addEventListener(evt, function (e) {
                    e.preventDefault();
                    e.stopPropagation();
                    dropArea.classList.remove('drag-over');
                });
            });
            dropArea.addEventListener('drop', function (e) {
                var files = e.dataTransfer && e.dataTransfer.files;
                if (files && files.length) {
                    document.getElementById('onbEinFile').files = files;
                    onbFileSelected(document.getElementById('onbEinFile'));
                }
            });
        }

        if (!introPlayed) { playIntro(); } else { skipToSetup(); }
    });

    /* ════════════════════════════════════════════════════════════════
       INTRO ANIMATION
       ════════════════════════════════════════════════════════════════ */

    function playIntro() {
        var intro = document.getElementById('onbIntro');
        var wizard = document.getElementById('onboardingWizard');
        if (!intro || !wizard) { skipToSetup(); return; }
        setTimeout(function () {
            intro.classList.add('fade-out');
            sessionStorage.setItem('onb_intro_done', '1');
            introPlayed = true;
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

    window.goToStep = function goToStep(step, instant) {
        currentStep = step;

        var slides = document.getElementById('onbSlides');
        if (!slides) return;

        if (instant) {
            slides.style.transition = 'none';
            slides.style.transform = 'translateX(-' + ((step - 1) * 100) + '%)';
            slides.offsetHeight;
            slides.style.transition = '';
        } else {
            slides.style.transform = 'translateX(-' + ((step - 1) * 100) + '%)';
        }

        // Update dots
        document.querySelectorAll('.onb-dot').forEach(function (dot) {
            var ds = parseInt(dot.dataset.step, 10);
            dot.classList.toggle('active', ds === step);
            dot.classList.toggle('done', ds < step);
        });

        var lbl = document.getElementById('onbStepNum');
        if (lbl) lbl.textContent = step;

        // Auto-fill contact name from Step 1 legal name (don't overwrite if already typed)
        if (step === 4) {
            var contactInput = document.getElementById('onbContactName');
            if (contactInput && !contactInput.value.trim()) {
                contactInput.value = valOf('onbSolePropName') || valOf('onbBizName') || '';
            }
        }

        // Pre-fill review step when arriving at step 5
        if (step === 5) prefillReview();
    };

    window.onbNext = function () {
        if (saving) return;
        if (!validateStep(currentStep)) return;
        if (currentStep < TOTAL_STEPS) goToStep(currentStep + 1);
    };

    window.onbPrev = function () {
        if (currentStep > 1) goToStep(currentStep - 1);
    };

    /* ════════════════════════════════════════════════════════════════
       STEP 1: LLC QUESTION
       ════════════════════════════════════════════════════════════════ */

    window.onbSetLlc = function (val) {
        hasLlc = val;
        hasDba = null; // reset DBA when toggling LLC
        toggle('onbLlcYes', val === true);
        toggle('onbLlcNo', val === false);
        // Clear DBA card visual state so stale selection doesn't show
        toggle('onbDbaYes', false);
        toggle('onbDbaNo', false);
        showIf('onbDbaFields', false);
        showIf('onbNoDbaMsg', false);
        showIf('onbLlcFields', val === true);
        showIf('onbSolePropMsg', val === false);
        // LLC: enable immediately. Sole prop: wait for DBA answer.
        enableBtn('onbStep1Btn', val === true);
    };

    /* ── DBA sub-question (sole prop only) ── */

    window.onbSetDba = function (val) {
        hasDba = val;
        toggle('onbDbaYes', val === true);
        toggle('onbDbaNo', val === false);
        showIf('onbDbaFields', val === true);
        showIf('onbNoDbaMsg', val === false);
        // Now that DBA is answered, enable Continue
        enableBtn('onbStep1Btn', true);
    };

    /* ════════════════════════════════════════════════════════════════
       STEP 2: EIN QUESTION + 30-DAY + UPLOAD
       ════════════════════════════════════════════════════════════════ */

    window.onbSetHasEin = function (val) {
        hasEin = val;
        einIsNew = null;
        einFileData = null;
        toggle('onbEinYes', val === true);
        toggle('onbEinNo', val === false);
        showIf('onbEinFields', val === true);
        showIf('onbNoEinGuide', val === false);
        // Show 30-day question only for sole props
        showIf('onbEinAgeQuestion', val === true && hasLlc === false);
        showIf('onbEinUpload', false);
        // Reset sub-selections
        toggle('onbEinNewYes', false);
        toggle('onbEinNewNo', false);
        // For LLC users with EIN, enable continue immediately
        enableBtn('onbStep2Btn', val === true && hasLlc === true);
        // Hide nav when showing IRS guide
        showIf('onbStep2Nav', val !== false);
    };

    window.onbSetEinNew = function (val) {
        einIsNew = val;
        toggle('onbEinNewYes', val === true);
        toggle('onbEinNewNo', val === false);
        var needsUpload = (val === true);
        showIf('onbEinUpload', needsUpload);
        // Enable continue if no upload needed, or if file already selected
        enableBtn('onbStep2Btn', !needsUpload || !!einFileData);
    };

    window.onbGotEin = function () {
        // User clicked "I just got my EIN" — they literally just got it, so ein_is_new = true
        hasEin = true;
        einIsNew = true;
        toggle('onbEinYes', true);
        toggle('onbEinNo', false);
        showIf('onbEinFields', true);
        showIf('onbNoEinGuide', false);
        showIf('onbStep2Nav', true);
        // Sole prop always for this path
        showIf('onbEinAgeQuestion', false); // Skip 30-day question — we know it's new
        showIf('onbEinUpload', true);       // Require upload
        enableBtn('onbStep2Btn', false);    // Disabled until file uploaded
    };

    /* ── File upload handlers ── */

    window.onbFileSelected = function (input) {
        var file = input.files && input.files[0];
        if (!file) return;

        if (file.size > 5 * 1024 * 1024) {
            if (typeof _showDashToast === 'function') _showDashToast(false, 'File too large. Maximum 5 MB.');
            input.value = '';
            return;
        }

        var reader = new FileReader();
        reader.onload = function (e) {
            var base64 = e.target.result.split(',')[1]; // strip data:mime;base64, prefix
            einFileData = {
                data: base64,
                type: file.type || 'application/octet-stream',
                name: file.name
            };
            // Show preview
            document.getElementById('onbFileName').textContent = file.name;
            document.getElementById('onbFileSize').textContent = formatFileSize(file.size);
            showIf('onbFilePreview', true);
            document.getElementById('onbUploadArea').classList.add('has-file');
            enableBtn('onbStep2Btn', true);
        };
        reader.readAsDataURL(file);
    };

    window.onbRemoveFile = function () {
        einFileData = null;
        document.getElementById('onbEinFile').value = '';
        showIf('onbFilePreview', false);
        document.getElementById('onbUploadArea').classList.remove('has-file');
        enableBtn('onbStep2Btn', false);
    };

    /* ════════════════════════════════════════════════════════════════
       STEP 3: WEBSITE QUESTION
       ════════════════════════════════════════════════════════════════ */

    window.onbSetHasWeb = function (val) {
        hasWebsite = val;
        selectedDomain = '';
        toggle('onbWebYes', val === true);
        toggle('onbWebNo', val === false);
        showIf('onbWebHave', val === true);
        showIf('onbWebNeed', val === false);
        showIf('onbDomainConfirm', false);
        // Has website: enable once URL is entered (checked by onbCheckWebUrl)
        // Needs website: enable once domain is selected (checked by domain click)
        enableBtn('onbStep3Btn', false);
        if (val === true) onbCheckWebUrl(); // re-check in case URL already typed
    };

    // Enable Continue only when a real URL is entered
    window.onbCheckWebUrl = function () {
        if (!hasWebsite) return;
        var url = valOf('onbWebsite');
        enableBtn('onbStep3Btn', url.length > 5 && url.indexOf('.') > 0);
    };

    window.onbSearchDomain = function () {
        var query = valOf('onbDomainSearch');
        if (!query) { markError('onbDomainSearch', 'Enter a business name to search'); return; }

        var resultsDiv = document.getElementById('onbDomainResults');
        resultsDiv.innerHTML = '<div class="onb-loading-state"><i class="fa-solid fa-spinner fa-spin"></i> Searching...</div>';
        showIf('onbDomainConfirm', false);
        enableBtn('onbStep3Btn', false);

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
            var domains = data.domains || data.results || data.suggestions || [];
            if (!domains.length) {
                resultsDiv.innerHTML = '<div class="onb-info"><div class="onb-info-text">No domains found. Try a different name.</div></div>';
                return;
            }
            var html = '';
            domains.forEach(function (d) {
                var name = typeof d === 'string' ? d : (d.domain || d.name || '');
                if (!name) return;
                html += '<div class="onb-domain-option" data-domain="' + escData(name) + '">';
                html += '<span class="onb-domain-name">' + escHtml(name) + '</span>';
                html += '<span class="onb-domain-avail"><i class="fa-solid fa-circle-check"></i> Available</span>';
                html += '</div>';
            });
            resultsDiv.innerHTML = html;
            resultsDiv.querySelectorAll('.onb-domain-option').forEach(function (el) {
                el.addEventListener('click', function () {
                    selectedDomain = el.dataset.domain;
                    document.getElementById('onbSelectedDomain').value = selectedDomain;
                    resultsDiv.querySelectorAll('.onb-domain-option').forEach(function (o) { o.classList.remove('selected'); });
                    el.classList.add('selected');
                    // Show confirmation with generated website + email
                    var urlSpan = document.getElementById('onbConfirmUrl');
                    var emailSpan = document.getElementById('onbConfirmEmail');
                    if (urlSpan) urlSpan.textContent = 'https://' + selectedDomain;
                    if (emailSpan) emailSpan.textContent = 'you@' + selectedDomain;
                    showIf('onbDomainConfirm', true);
                    enableBtn('onbStep3Btn', true);
                });
            });
        })
        .catch(function () {
            resultsDiv.innerHTML = '<div class="onb-info"><div class="onb-info-text">Search failed. Please try again.</div></div>';
        });
    };

    /* ════════════════════════════════════════════════════════════════
       STEP 5: REVIEW & SUBMIT
       ════════════════════════════════════════════════════════════════ */

    function prefillReview() {
        var bizName = hasLlc ? valOf('onbBizName') : (hasDba ? valOf('onbDbaName') : valOf('onbSolePropName'));
        var bizType = hasLlc ? valOf('onbBizType') : 'Sole Proprietorship';
        var ein = valOf('onbEIN');
        var web = hasWebsite ? valOf('onbWebsite') : (selectedDomain ? ('https://' + selectedDomain) : '');

        setText('onbRevBizName', bizName || '—');
        setText('onbRevBizType', bizType || '—');
        setText('onbRevEin', ein || '—');

        // For sole prop + DBA: show both names so they can verify the split
        var bizNameLabel = document.getElementById('onbRevBizNameLabel');
        var legalNameWrap = document.getElementById('onbRevLegalNameWrap');
        if (!hasLlc && hasDba) {
            if (bizNameLabel) bizNameLabel.textContent = 'Business Name (DBA)';
            setText('onbRevLegalName', valOf('onbSolePropName') || '—');
            if (legalNameWrap) legalNameWrap.style.display = '';
        } else {
            if (bizNameLabel) bizNameLabel.textContent = hasLlc ? 'Legal Business Name' : 'Business Name';
            if (legalNameWrap) legalNameWrap.style.display = 'none';
        }

        // EIN document
        if (einFileData) {
            setText('onbRevDoc', einFileData.name);
            showIf('onbRevDocWrap', true);
        } else {
            showIf('onbRevDocWrap', false);
        }

        // Website (required — either their own or our domain)
        setText('onbRevWebsite', web || '—');

        // Show business email if they picked our domain service
        var emailDomainWrap = document.getElementById('onbRevEmailDomainWrap');
        if (!hasWebsite && selectedDomain) {
            setText('onbRevEmailDomain', 'you@' + selectedDomain);
            if (emailDomainWrap) emailDomainWrap.style.display = '';
        } else {
            if (emailDomainWrap) emailDomainWrap.style.display = 'none';
        }

        // Address
        var street = valOf('onbStreet');
        var city = valOf('onbCity');
        var state = valOf('onbState');
        var zip = valOf('onbZip');
        var addr = [street, city, state, zip].filter(Boolean).join(', ');
        setText('onbRevAddress', addr || '—');

        // Contact
        setText('onbRevContact', valOf('onbContactName') || '—');
        setText('onbRevRole', valOf('onbContactRole') || '—');
        setText('onbRevEmail', valOf('onbContactEmail') || '—');
        setText('onbRevPhone', valOf('onbContactPhone') || '—');
    }

    window.onbSubmitProfile = function () {
        if (saving) return;
        saveProfile(function () {
            goToStep(6);
        });
    };

    /* ════════════════════════════════════════════════════════════════
       STEP 5: PHONE NUMBERS
       ════════════════════════════════════════════════════════════════ */

    window.onbSearchNumbers = function () {
        var ac = valOf('onbAreaCode');
        if (!ac || ac.length < 3) { markError('onbAreaCode', 'Enter a 3-digit area code'); return; }

        var grid = document.getElementById('onbNumberGrid');
        grid.innerHTML = '<div class="onb-loading-state onb-loading-full"><i class="fa-solid fa-spinner fa-spin"></i> Searching...</div>';

        fetch('/voice/numbers/search?area_code=' + encodeURIComponent(ac) + '&number_type=local&limit=10')
        .then(function (r) { return r.json(); })
        .then(function (data) {
            var nums = data.numbers || [];
            if (!nums.length) {
                grid.innerHTML = '<div class="onb-loading-state onb-loading-full">No numbers found for that area code. Try another.</div>';
                return;
            }
            selectedNumbers = [];
            var html = '';
            nums.forEach(function (n) {
                var phone = n.phone || n.phoneNumber || n.friendly_name || '';
                var raw = n.phone_number || n.phoneNumber || phone;
                html += '<div class="onb-number-card" data-phone="' + escData(raw) + '">';
                html += '<span class="onb-number-check"><i class="fa-solid fa-check"></i></span>';
                html += '<span class="onb-number-text">' + escHtml(formatPhone(phone)) + '</span>';
                html += '</div>';
            });
            grid.innerHTML = html;
            grid.querySelectorAll('.onb-number-card').forEach(function (el) {
                el.addEventListener('click', function () {
                    var phone = el.dataset.phone;
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
                });
            });
            updateNumberCount();
        })
        .catch(function () {
            grid.innerHTML = '<div class="onb-loading-state onb-loading-full">Search failed. Please try again.</div>';
        });
    };

    function updateNumberCount() {
        var el = document.getElementById('onbNumberCount');
        if (!el) return;
        var remaining = 5 - selectedNumbers.length;
        el.innerHTML = remaining > 0
            ? 'Selected ' + selectedNumbers.length + ' of 5 — <strong>' + remaining + '</strong> more free'
            : '<strong>5 of 5</strong> selected';
    }

    window.onbGetNumbers = function () {
        if (!selectedNumbers.length) { onbFinish(); return; }
        saving = true;
        setBtnLoading('onbGetNumbersBtn', true);
        var bought = 0, errors = 0, total = selectedNumbers.length;

        function buyNext() {
            if (bought + errors >= total) {
                saving = false;
                setBtnLoading('onbGetNumbersBtn', false);
                if (errors > 0 && bought > 0 && typeof _showDashToast === 'function')
                    _showDashToast(true, 'Got ' + bought + ' of ' + total + ' numbers. ' + errors + ' unavailable.');
                if (errors > 0 && bought === 0) {
                    if (typeof _showDashToast === 'function')
                        _showDashToast(false, 'Could not get any numbers — they may have been claimed. Try again.');
                    return;
                }
                onbFinish();
                return;
            }
            fetch('/voice/numbers/buy', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ phone_number: selectedNumbers[bought + errors], number_type: 'local' })
            })
            .then(function (r) { return r.json(); })
            .then(function (res) { res.error ? errors++ : bought++; buyNext(); })
            .catch(function () { errors++; buyNext(); });
        }
        buyNext();
    };

    window.onbSkipNumbers = function () { onbFinish(); };

    /* ════════════════════════════════════════════════════════════════
       VALIDATION
       ════════════════════════════════════════════════════════════════ */

    var EIN_RE = /^\d{2}-?\d{7}$/;
    var EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

    function validateStep(step) {
        clearErrors();

        if (step === 1) {
            if (hasLlc === null) return false;
            if (hasLlc) {
                var ok = true;
                if (!valOf('onbBizName')) { markError('onbBizName', 'Legal business name is required'); ok = false; }
                if (!valOf('onbBizType')) { markError('onbBizType', 'Select an entity type'); ok = false; }
                return ok;
            }
            if (!hasLlc && !valOf('onbSolePropName')) {
                markError('onbSolePropName', 'Your legal name is required');
                return false;
            }
            if (!hasLlc && hasDba && !valOf('onbDbaName')) {
                markError('onbDbaName', 'DBA name is required');
                return false;
            }
            return true;
        }

        if (step === 2) {
            if (hasEin === null) return false;
            if (!hasEin) return false; // Blocked by IRS guide
            var ein = valOf('onbEIN');
            if (!ein) { markError('onbEIN', 'EIN is required'); return false; }
            if (!EIN_RE.test(ein)) { markError('onbEIN', 'Enter a valid EIN (XX-XXXXXXX)'); return false; }
            // Sole prop with new EIN needs upload
            if (!hasLlc && einIsNew && !einFileData) {
                if (typeof _showDashToast === 'function') _showDashToast(false, 'Please upload your EIN confirmation letter.');
                return false;
            }
            return true;
        }

        if (step === 3) {
            if (hasWebsite === null) return false;
            if (hasWebsite) {
                var url = valOf('onbWebsite');
                if (!url) { markError('onbWebsite', 'Enter your website URL'); return false; }
                if (url.indexOf('.') < 0) { markError('onbWebsite', 'Enter a valid website URL (e.g. https://yourbusiness.com)'); return false; }
            } else {
                if (!selectedDomain) {
                    if (typeof _showDashToast === 'function') _showDashToast(false, 'Please search and select a domain before continuing.');
                    return false;
                }
            }
            return true;
        }

        if (step === 4) {
            var fields = [
                ['onbStreet', 'Street address is required'],
                ['onbCity', 'City is required'],
                ['onbState', 'Select a state'],
                ['onbZip', 'ZIP code is required'],
                ['onbContactName', 'Contact name is required'],
                ['onbContactEmail', 'Email is required']
            ];
            var ok4 = true;
            fields.forEach(function (pair) {
                if (!valOf(pair[0])) { markError(pair[0], pair[1]); ok4 = false; }
            });
            var email = valOf('onbContactEmail');
            if (email && !EMAIL_RE.test(email)) { markError('onbContactEmail', 'Enter a valid email'); ok4 = false; }
            return ok4;
        }

        return true;
    }

    /* ════════════════════════════════════════════════════════════════
       SAVE PROFILE (Step 4 → /voice/trust-hub/save)
       ════════════════════════════════════════════════════════════════ */

    function saveProfile(callback) {
        saving = true;
        setBtnLoading('onbSubmitBtn', true);

        // For sole prop + DBA: business_name = DBA name, legal name stays as contact_name
        var bizName = hasLlc ? valOf('onbBizName') : (hasDba ? valOf('onbDbaName') : valOf('onbSolePropName'));
        var bizType = hasLlc ? valOf('onbBizType') : 'Sole Proprietorship';
        var website = '';
        if (hasWebsite) {
            website = valOf('onbWebsite');
        } else if (selectedDomain) {
            website = 'https://' + selectedDomain;
        }

        var data = {
            business_name:  bizName,
            business_type:  bizType,
            ein:            valOf('onbEIN'),
            has_llc:        hasLlc,
            has_dba:        !!(hasDba),
            dba_name:       hasDba ? valOf('onbDbaName') : '',
            legal_name:     valOf('onbSolePropName'),
            ein_is_new:     !!(einIsNew),
            street:         valOf('onbStreet'),
            city:           valOf('onbCity'),
            state:          valOf('onbState'),
            zip:            valOf('onbZip'),
            website:        website,
            contact_name:   valOf('onbContactName'),
            contact_title:  valOf('onbContactRole'),
            contact_email:  valOf('onbContactEmail'),
            contact_phone:  valOf('onbContactPhone')
        };

        // Include EIN document if uploaded
        if (einFileData) {
            data.ein_document_data = einFileData.data;
            data.ein_document_type = einFileData.type;
            data.ein_document_name = einFileData.name;
        }

        fetch('/voice/trust-hub/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        })
        .then(function (r) { return r.json(); })
        .then(function (result) {
            saving = false;
            setBtnLoading('onbSubmitBtn', false);
            if (result.error) {
                if (typeof _showDashToast === 'function') _showDashToast(false, result.error);
                return;
            }
            if (typeof _showDashToast === 'function')
                _showDashToast(true, 'Business profile saved. Carrier verification submitted.');
            if (callback) callback();
        })
        .catch(function () {
            saving = false;
            setBtnLoading('onbSubmitBtn', false);
            if (typeof _showDashToast === 'function') _showDashToast(false, 'Save failed. Please try again.');
        });
    }

    /* ════════════════════════════════════════════════════════════════
       CLOSING ANIMATION
       ════════════════════════════════════════════════════════════════ */

    function onbFinish() {
        var wizard = document.getElementById('onboardingWizard');
        var closing = document.getElementById('onbClosing');
        var closingLine = document.getElementById('onbClosingLine');

        if (!closing || !closingLine) {
            sessionStorage.removeItem('onb_step');
            sessionStorage.removeItem('onb_intro_done');
            window.location.reload();
            return;
        }

        if (wizard) wizard.style.display = 'none';
        closing.style.display = '';
        closing.offsetHeight;
        closing.classList.add('visible');

        setTimeout(function () { closingLine.classList.add('animate'); }, 600);

        setTimeout(function () {
            closing.classList.add('brightening');
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
       INFO TOOLTIPS — tap/click to toggle, input focus to show
       ════════════════════════════════════════════════════════════════ */

    function dismissAllTips() {
        document.querySelectorAll('.onb-info-tip.active').forEach(function (t) {
            t.classList.remove('active');
        });
    }

    function showTip(tipEl) {
        dismissAllTips();
        if (!tipEl) return;
        tipEl.classList.add('active');
        // Check if tooltip would overflow left edge and anchor differently
        var rect = tipEl.getBoundingClientRect();
        if (rect.left < 140) {
            tipEl.classList.add('anchor-left');
        } else {
            tipEl.classList.remove('anchor-left');
        }
    }

    // Tap/click on the circle-i icon itself
    document.addEventListener('click', function (e) {
        var tip = e.target.closest('.onb-info-tip');
        if (tip) {
            e.preventDefault();
            e.stopPropagation();
            if (tip.classList.contains('active')) {
                tip.classList.remove('active');
            } else {
                showTip(tip);
            }
            return;
        }
        // Click anywhere else → dismiss
        dismissAllTips();
    });

    // Input focus → show the tooltip for that field's label
    document.addEventListener('focusin', function (e) {
        var input = e.target;
        if (!input.matches || !input.matches('.onb-input, .onb-select')) return;
        var group = input.closest('.onb-field-group');
        if (!group) return;
        var tip = group.querySelector('.onb-info-tip');
        if (tip) showTip(tip);
    });

    // Input blur → dismiss after short delay (lets click-on-tip work)
    document.addEventListener('focusout', function (e) {
        if (!e.target.matches || !e.target.matches('.onb-input, .onb-select')) return;
        setTimeout(dismissAllTips, 200);
    });

    /* ════════════════════════════════════════════════════════════════
       HELPERS
       ════════════════════════════════════════════════════════════════ */

    window.onbToggle = function (id) {
        var el = document.getElementById(id);
        if (el) el.classList.toggle('open');
    };

    function valOf(id) {
        var el = document.getElementById(id);
        return el ? el.value.trim() : '';
    }

    function setText(id, text) {
        var el = document.getElementById(id);
        if (el) el.textContent = text;
    }

    function toggle(id, selected) {
        var el = document.getElementById(id);
        if (el) el.classList.toggle('selected', selected);
    }

    function showIf(id, condition) {
        var el = document.getElementById(id);
        if (!el) return;
        if (condition) {
            el.style.display = '';
            el.classList.remove('onb-conditional'); // let CSS-defined display (block/flex) take over
        } else {
            el.style.display = 'none';
        }
    }

    function enableBtn(id, enabled) {
        var el = document.getElementById(id);
        if (el) el.disabled = !enabled;
    }

    function markError(id, msg) {
        var el = document.getElementById(id);
        if (!el) return;
        el.classList.add('onb-error');
        if (msg) {
            var existing = el.parentNode.querySelector('.onb-error-msg');
            if (!existing) {
                var span = document.createElement('span');
                span.className = 'onb-error-msg';
                span.textContent = msg;
                el.parentNode.appendChild(span);
            }
        }
    }

    function clearErrors() {
        document.querySelectorAll('.onb-error').forEach(function (el) { el.classList.remove('onb-error'); });
        document.querySelectorAll('.onb-error-msg').forEach(function (el) { el.remove(); });
    }

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
        if (d.length === 10) return '(' + d.slice(0, 3) + ') ' + d.slice(3, 6) + '-' + d.slice(6);
        return p;
    }

    function formatFileSize(bytes) {
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
        return (bytes / 1048576).toFixed(1) + ' MB';
    }

    function escHtml(s) {
        var d = document.createElement('div');
        d.appendChild(document.createTextNode(s));
        return d.innerHTML;
    }

    function escData(s) {
        return (s || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;')
                        .replace(/'/g, '&#39;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

})();
