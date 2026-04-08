        // ===== SPAM MONITORING ACCORDION =====
        function toggleSmAccordion(id) {
            const item = document.getElementById(id);
            if (!item) return;
            item.classList.toggle('open');
        }

        // ===== LOCAL UTILITIES =====
        function _esc(s) { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }
        function _fmtPhone(p) {
            if (!p) return '';
            const d = p.replace(/\D/g, '');
            if (d.length === 11 && d[0] === '1') return '(' + d.substr(1,3) + ') ' + d.substr(4,3) + '-' + d.substr(7);
            if (d.length === 10) return '(' + d.substr(0,3) + ') ' + d.substr(3,3) + '-' + d.substr(6);
            return p;
        }

        // ===== NUMBER CART =====
        var _numCart = []; // [{phone, numberType, priceLabel, priceCents}]

        function _numCartSave() {
            try { localStorage.setItem('numCart', JSON.stringify(_numCart)); } catch(e) {}
        }
        function _numCartLoad() {
            try {
                var saved = localStorage.getItem('numCart');
                if (saved) _numCart = JSON.parse(saved);
            } catch(e) { _numCart = []; }
            _numCartUpdateUI();
        }

        function numCartAdd(phone, numberType) {
            if (_numCart.find(function(i) { return i.phone === phone; })) return;
            var priceCents = numberType === 'toll_free' ? 215 : 90;
            var priceLabel = numberType === 'toll_free' ? '$2.15/mo' : '$0.90/mo';
            _numCart.push({ phone: phone, numberType: numberType, priceLabel: priceLabel, priceCents: priceCents });
            _numCartSave();
            _numCartUpdateUI();
            if (typeof _showDashToast === 'function') _showDashToast(true, _fmtPhone(phone) + ' added to cart');
        }

        function numCartRemove(phone) {
            _numCart = _numCart.filter(function(i) { return i.phone !== phone; });
            _numCartSave();
            _numCartUpdateUI();
        }

        function numCartClear() {
            _numCart = [];
            _numCartSave();
            _numCartUpdateUI();
        }

        function numCartToggle() {
            var overlay = document.getElementById('numCartOverlay');
            if (!overlay) return;
            overlay.style.display = overlay.style.display === 'none' ? 'block' : 'none';
            _numCartRenderItems();
        }

        function _numCartUpdateUI() {
            var btn = document.getElementById('numCartBtn');
            var countEl = document.getElementById('numCartCount');
            if (btn) btn.style.display = _numCart.length > 0 ? 'inline-flex' : 'none';
            if (countEl) countEl.textContent = _numCart.length;
            _numCartRenderItems();
        }

        function _numCartRenderItems() {
            var itemsEl = document.getElementById('numCartItems');
            var summaryEl = document.getElementById('numCartSummary');
            if (!itemsEl || !summaryEl) return;

            if (!_numCart.length) {
                itemsEl.innerHTML = '<div style="text-align:center;padding:12px;color:#666;">Cart is empty</div>';
                summaryEl.innerHTML = '';
                return;
            }

            // Calculate pricing: first 5 numbers total are free, rest are paid
            var freeRemaining = _numFreeRemaining !== null ? _numFreeRemaining : 0;
            var totalCents = 0;
            var freeCount = 0;
            var paidCount = 0;

            itemsEl.innerHTML = _numCart.map(function(item, idx) {
                var isFree = freeRemaining > 0;
                if (isFree) { freeRemaining--; freeCount++; }
                else { totalCents += item.priceCents; paidCount++; }
                var priceDisplay = isFree
                    ? '<span style="color:#00ff88;font-weight:700;">FREE</span>'
                    : '<span style="color:#00ff88;">' + item.priceLabel + '</span>';
                return '<div style="display:flex;align-items:center;justify-content:space-between;padding:8px 4px;border-bottom:1px solid rgba(255,255,255,0.04);">' +
                    '<div style="flex:1;">' +
                        '<span style="color:#fff;font-weight:600;">' + _esc(_fmtPhone(item.phone)) + '</span>' +
                        '<span style="margin-left:8px;font-size:0.75rem;color:#888;">' + item.numberType + '</span>' +
                    '</div>' +
                    '<div style="display:flex;align-items:center;gap:8px;">' +
                        priceDisplay +
                        '<button onclick="numCartRemove(\'' + item.phone + '\')" style="background:none;border:none;color:#ef4444;cursor:pointer;font-size:0.75rem;padding:2px 4px;" title="Remove"><i class="fa-solid fa-xmark"></i></button>' +
                    '</div>' +
                '</div>';
            }).join('');

            var totalDollars = (totalCents / 100).toFixed(2);
            summaryEl.innerHTML =
                '<div style="display:flex;justify-content:space-between;color:#aaa;margin-bottom:4px;">' +
                    '<span>' + _numCart.length + ' number' + (_numCart.length > 1 ? 's' : '') + '</span>' +
                    (freeCount > 0 ? '<span style="color:#00ff88;">' + freeCount + ' free</span>' : '') +
                '</div>' +
                (paidCount > 0 ? '<div style="display:flex;justify-content:space-between;font-weight:700;font-size:0.95rem;">' +
                    '<span style="color:#fff;">Total</span>' +
                    '<span style="color:#00ff88;">$' + totalDollars + '/mo</span>' +
                '</div>' : '<div style="color:#00ff88;font-weight:700;">All numbers are free!</div>');
        }

        async function numCartCheckout() {
            if (!_numCart.length) return;

            var btn = document.getElementById('numCartCheckoutBtn');
            if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-1"></i>Processing...'; }

            try {
                var r = await fetch('/voice/numbers/cart-checkout', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ items: _numCart.map(function(i) { return { phone_number: i.phone, number_type: i.numberType }; }) })
                });
                var d = await r.json();

                if (d.all_free) {
                    // All numbers were free — they've been provisioned
                    _numCart = [];
                    _numCartSave();
                    _numCartUpdateUI();
                    var overlay = document.getElementById('numCartOverlay');
                    if (overlay) overlay.style.display = 'none';
                    if (typeof _showDashToast === 'function') _showDashToast(true, d.provisioned + ' number(s) added!');
                    loadNumbersTab();
                    return;
                }

                if (d.checkout_url) {
                    // Store cart items pending in localStorage for post-checkout provisioning
                    localStorage.setItem('numCartPending', JSON.stringify(_numCart));
                    _numCart = [];
                    _numCartSave();
                    window.location.href = d.checkout_url;
                    return;
                }

                alert(d.error || 'Checkout failed');
            } catch(e) {
                alert('Network error during checkout');
            } finally {
                if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-credit-card me-1"></i>Checkout'; }
            }
        }

        // Post-purchase: complete provisioning after Stripe redirect
        (function() {
            var params = new URLSearchParams(window.location.search);
            if (params.get('cart_purchased') === '1') {
                var pending = [];
                try { pending = JSON.parse(localStorage.getItem('numCartPending') || '[]'); } catch(e) {}
                if (pending.length) {
                    localStorage.removeItem('numCartPending');
                    fetch('/voice/numbers/complete-cart-purchase', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({ items: pending.map(function(i) { return { phone_number: i.phone, number_type: i.numberType }; }) })
                    }).then(function(r) { return r.json(); }).then(function(d) {
                        if (d.provisioned > 0) {
                            if (typeof _showDashToast === 'function') _showDashToast(true, d.provisioned + ' number(s) purchased and activated!');
                        }
                        if (d.errors && d.errors.length) {
                            alert('Some numbers failed to provision:\n' + d.errors.join('\n'));
                        }
                        // Clean URL
                        var url = new URL(window.location);
                        url.searchParams.delete('cart_purchased');
                        window.history.replaceState({}, '', url);
                        if (typeof loadNumbersTab === 'function') loadNumbersTab();
                    }).catch(function() {});
                }
            }
        })();

        // Load cart from localStorage on script init
        _numCartLoad();

        // ===== SPAM PROTECTION TAB =====
        async function loadTrustHubData() { loadSpamProtectionStatus(); }

        // Auto-refresh timer: poll every 30s while profile is pending-review/in-review
        let _bizProfilePollTimer = null;
        function _bizProfileStartPoll() {
            if (_bizProfilePollTimer) return;
            _bizProfilePollTimer = setInterval(() => {
                // Only poll if the Business Profile tab is actually visible
                const pane = document.getElementById('business-profile');
                if (!pane || !pane.classList.contains('active')) {
                    _bizProfileStopPoll();
                    return;
                }
                loadSpamProtectionStatus();
            }, 30000);
        }
        function _bizProfileStopPoll() {
            if (_bizProfilePollTimer) {
                clearInterval(_bizProfilePollTimer);
                _bizProfilePollTimer = null;
            }
        }

        // Pre-fill business profile form fields from API data
        function _spPrefillForm(d) {
            var _sv = function(id, val) { var el = document.getElementById(id); if (el) el.value = val || ''; };
            _sv('spBizName',      d.business_name);
            _sv('spEIN',          d.ein);
            _sv('spStreet',       d.street);
            _sv('spCity',         d.city);
            _sv('spState',        d.state);
            _sv('spZip',          d.zip);
            _sv('spContactName',  d.contact_name);
            if (d.contact_name) {
                var _parts = d.contact_name.trim().split(/\s+/);
                _sv('spContactFirstName', _parts[0] || '');
                _sv('spContactLastName', _parts.slice(1).join(' ') || '');
            }
            _sv('spContactEmail', d.contact_email);
            _sv('spContactPhone', d.contact_phone);
            _sv('spContactTitle', d.contact_title);
            _sv('spWebsite',      d.website);
            var btSelect = document.getElementById('spBizType');
            if (btSelect && d.business_type) {
                btSelect.value = d.business_type;
                spBizTypeChanged();
            }
            if (d.ein_document_name) {
                var einDocSection = document.getElementById('spEinDocSection');
                var einUploadArea = document.getElementById('spEinUploadArea');
                var einUploadedInfo = document.getElementById('spEinUploadedInfo');
                var einUploadPrompt = document.getElementById('spEinUploadPrompt');
                var einFileName = document.getElementById('spEinFileName');
                if (einDocSection) einDocSection.style.display = 'block';
                if (einUploadArea) einUploadArea.style.display = 'block';
                if (einUploadedInfo) einUploadedInfo.style.display = 'flex';
                if (einUploadPrompt) einUploadPrompt.style.display = 'none';
                if (einFileName) einFileName.textContent = d.ein_document_name + ' (uploaded)';
            }
            if (d.ein_is_new) {
                var einDocSection = document.getElementById('spEinDocSection');
                if (einDocSection) einDocSection.style.display = 'block';
                spSetEinNew(true);
            }
        }

        async function loadSpamProtectionStatus() {
            const statusEl = document.getElementById('spamProtectionStatus');
            const formEl = document.getElementById('spamProtectionForm');
            const numsEl = document.getElementById('spNumbersContent');
            try {
                const r = await fetch('/voice/spam-protection/status');
                const d = await r.json();
                console.log('[SpamProtection] Status:', r.status, d);
                if (!r.ok || d.error) {
                    if (numsEl) numsEl.innerHTML = '<div style="color:#ef4444;font-size:.78rem;padding:8px;"><i class="fa-solid fa-triangle-exclamation me-1"></i>' + _esc(d.error || 'Failed to load') + '</div>';
                    return;
                }

                // Show status banner if protection has been submitted
                if (d.protection_active && statusEl) {
                    statusEl.style.display = 'block';
                    var rs = d.review_status || '';
                    var isApproved = (rs === 'twilio-approved' || rs === 'compliant');
                    var isPending = (rs === 'pending-review' || rs === 'in-review');
                    var isRejected = (rs === 'twilio-rejected' || rs === 'noncompliant');
                    var isDraft = (rs === 'draft');

                    // Auto-refresh every 30s while profile is pending review;
                    // stop once we reach a terminal state (approved/rejected).
                    if (isPending || isDraft) {
                        _bizProfileStartPoll();
                    } else {
                        _bizProfileStopPoll();
                    }

                    // Dynamic banner colors based on real review status
                    var bannerColor, bannerBg, bannerBorder, iconClass, statusLabel;
                    if (isApproved) {
                        bannerColor = '#00ff88'; bannerBg = 'rgba(0,255,136,0.06)'; bannerBorder = 'rgba(0,255,136,0.2)';
                        iconClass = 'fa-solid fa-shield-halved'; statusLabel = 'Spam Protection Active';
                    } else if (isPending) {
                        bannerColor = '#ffa500'; bannerBg = 'rgba(255,165,0,0.06)'; bannerBorder = 'rgba(255,165,0,0.2)';
                        iconClass = 'fa-solid fa-clock'; statusLabel = 'Profile Under Review';
                    } else if (isRejected) {
                        bannerColor = '#ef4444'; bannerBg = 'rgba(239,68,68,0.06)'; bannerBorder = 'rgba(239,68,68,0.2)';
                        iconClass = 'fa-solid fa-triangle-exclamation'; statusLabel = 'Profile Rejected — Needs Attention';
                    } else {
                        bannerColor = '#888'; bannerBg = 'rgba(255,255,255,0.03)'; bannerBorder = 'rgba(255,255,255,0.08)';
                        iconClass = 'fa-solid fa-shield'; statusLabel = 'Profile Submitted';
                    }

                    // Status detail line
                    var statusDetail = '<strong>' + _esc(d.business_name) + '</strong>';
                    if (isApproved) {
                        statusDetail += ' &mdash; ' + d.numbers_protected + '/' + d.numbers_total + ' numbers protected';
                        statusDetail += (d.auto_cnam ? ' &bull; Auto-protect ON' : '');
                        statusDetail += ' &bull; STIR/SHAKEN A';
                    } else if (isPending) {
                        statusDetail += ' &mdash; Your business profile is under review. Typical approval time is ~24 hours.';
                    } else if (isRejected) {
                        statusDetail += ' &mdash; Your profile was rejected. Edit your info below and re-submit.';
                    } else {
                        statusDetail += ' &mdash; Profile submitted, awaiting review (~24 hours).';
                    }
                    statusDetail += ' &bull; Registered ' + (d.registered_at ? new Date(d.registered_at).toLocaleDateString() : '');

                    // Evaluation issues
                    var issuesHtml = '';
                    var issues = d.evaluation_issues || [];
                    if (issues.length > 0) {
                        issuesHtml = '<div style="margin-top:6px;padding:6px 10px;background:rgba(239,68,68,0.06);border:1px solid rgba(239,68,68,0.12);border-radius:6px;font-size:0.72rem;color:#ef4444;">' +
                            '<i class="fa-solid fa-triangle-exclamation me-1"></i>Issues: ' + issues.map(function(i) { return _esc(i); }).join(', ') +
                        '</div>';
                    }
                    statusEl.innerHTML =
                        '<div class="mb-3 p-3" style="background:' + bannerBg + ';border:1px solid ' + bannerBorder + ';border-radius:10px;">' +
                            '<div class="d-flex align-items-center gap-3">' +
                                '<div style="width:36px;height:36px;border-radius:50%;background:' + bannerBg + ';display:flex;align-items:center;justify-content:center;flex-shrink:0;">' +
                                    '<i class="' + iconClass + '" style="color:' + bannerColor + ';"></i>' +
                                '</div>' +
                                '<div style="flex:1;">' +
                                    '<div style="font-weight:700;color:' + bannerColor + ';font-size:0.9rem;">' + statusLabel + '</div>' +
                                    '<div style="font-size:0.75rem;color:#aaa;">' + statusDetail + '</div>' +
                                    issuesHtml +
                                '</div>' +
                                '<button onclick="document.getElementById(\'spamProtectionForm\').style.display=document.getElementById(\'spamProtectionForm\').style.display===\'none\'?\'block\':\'none\'" style="background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);color:#aaa;border-radius:6px;padding:5px 12px;font-size:0.75rem;cursor:pointer;white-space:nowrap;">' +
                                    '<i class="fa-solid fa-pen me-1"></i>Edit' +
                                '</button>' +
                            '</div>' +
                        '</div>';
                    // Pre-fill edit form with current registered values
                    _spPrefillForm(d);
                    // Show form if rejected (user needs to fix and resubmit)
                    if (isRejected) {
                        if (formEl) formEl.style.display = 'block';
                    } else {
                        // Collapse form if already registered
                        if (formEl) formEl.style.display = 'none';
                    }
                } else if (d.business_name && !d.protection_active) {
                    // Profile data saved but not yet submitted — prepopulate
                    // form so user can resume where they left off
                    _spPrefillForm(d);
                }

                // Render number protection list
                if (numsEl) {
                    const nums = d.numbers || [];
                    if (!nums.length) {
                        numsEl.innerHTML = '<div style="color:#888;text-align:center;padding:12px;">No numbers found. Buy a number in the Numbers tab to get started.</div>';
                    } else {
                        let html = '';
                        nums.forEach(n => {
                            const icon = n.cnam_enabled
                                ? '<i class="fa-solid fa-shield-halved" style="color:#00ff88;"></i>'
                                : '<i class="fa-solid fa-shield" style="color:#444;"></i>';
                            const label = n.cnam_enabled
                                ? '<span style="color:#00ff88;font-weight:600;">Protected</span>'
                                : '<span style="color:#888;">Not protected</span>';
                            html += '<div style="display:flex;align-items:center;justify-content:space-between;padding:8px 0;border-bottom:1px solid rgba(255,255,255,0.03);">' +
                                '<div style="display:flex;align-items:center;gap:8px;">' + icon +
                                    '<span style="color:#ccc;">' + _esc(_fmtPhone(n.phone)) + '</span>' +
                                '</div>' +
                                '<div style="display:flex;align-items:center;gap:8px;">' +
                                    label +
                                '</div>' +
                            '</div>';
                        });
                        numsEl.innerHTML = html;
                    }
                }
                // Update accordion badges
                var bpBadge = document.getElementById('smBadgeBizProfile');
                var prBadge = document.getElementById('smBadgeProtection');
                if (bpBadge) {
                    var rs = d.review_status || '';
                    var badgeBase = 'display:inline-block;font-size:0.7rem;font-weight:600;padding:2px 8px;border-radius:10px;';
                    // Use review_status as primary signal (Twilio truth), not protection_active flag
                    if (rs === 'twilio-approved' || rs === 'compliant' || rs === 'approved') {
                        bpBadge.textContent = 'Approved';
                        bpBadge.style.cssText = badgeBase + 'background:rgba(0,255,136,0.12);color:#00ff88;';
                    } else if (rs === 'pending-review' || rs === 'in-review') {
                        bpBadge.textContent = 'Under Review';
                        bpBadge.style.cssText = badgeBase + 'background:rgba(255,165,0,0.12);color:#ffa500;';
                    } else if (rs === 'twilio-rejected' || rs === 'noncompliant') {
                        bpBadge.textContent = 'Rejected';
                        bpBadge.style.cssText = badgeBase + 'background:rgba(239,68,68,0.12);color:#ef4444;';
                    } else if (rs === 'draft') {
                        bpBadge.textContent = 'Draft';
                        bpBadge.style.cssText = badgeBase + 'background:rgba(255,255,255,0.06);color:#888;';
                    } else {
                        bpBadge.textContent = 'Not registered';
                        bpBadge.style.cssText = badgeBase + 'background:rgba(255,255,255,0.06);color:#888;';
                    }
                }
                if (prBadge && d.numbers_total > 0) {
                    prBadge.textContent = d.numbers_protected + '/' + d.numbers_total + ' protected';
                    var allProtected = d.numbers_protected === d.numbers_total;
                    prBadge.style.cssText = 'display:inline-block;background:' + (allProtected ? 'rgba(0,255,136,0.12);color:#00ff88' : 'rgba(255,165,0,0.12);color:#ffa500') + ';font-size:0.7rem;font-weight:600;padding:2px 8px;border-radius:10px;';
                }
            } catch(e) {
                console.error('[SpamProtection] Error:', e);
                if (numsEl) numsEl.innerHTML = '<div style="color:#ef4444;font-size:.78rem;padding:8px;">Network error</div>';
            }
        }

        // Toggle UI hints when business type changes (sole proprietorship vs other)
        function spBizTypeChanged() {
            var bt = document.getElementById('spBizType')?.value || '';
            var isSoleProp = bt === 'Sole Proprietorship';
            var hint = document.getElementById('spSolePropHint');
            var einLabel = document.getElementById('spEINLabel');
            var einInput = document.getElementById('spEIN');
            var bizNameLabel = document.getElementById('spBizNameLabel');
            var bizNameInput = document.getElementById('spBizName');

            if (hint) hint.style.display = isSoleProp ? 'block' : 'none';
            // Show EIN doc upload section for sole props
            var einDocSection = document.getElementById('spEinDocSection');
            if (einDocSection) einDocSection.style.display = isSoleProp ? 'block' : 'none';

            // Reset EIN toggle state when switching away from sole prop
            if (!isSoleProp) {
                var noCard = document.getElementById('spNoEinCard');
                var yesBtn = document.getElementById('spEinYesBtn');
                var noBtn = document.getElementById('spEinNoBtn');
                if (noCard) noCard.style.display = 'none';
                if (yesBtn) { yesBtn.style.background = 'rgba(0,0,0,0)'; yesBtn.style.color = '#aaa'; }
                if (noBtn) { noBtn.style.background = 'rgba(0,0,0,0)'; noBtn.style.color = '#aaa'; }
                _spEinBypassWarning = false;
                spClearEinFile(null);
            }

            if (einLabel) einLabel.textContent = 'EIN (Tax ID)';
            if (einInput) einInput.placeholder = 'XX-XXXXXXX';
            if (bizNameLabel) bizNameLabel.textContent = isSoleProp ? 'Full Legal Name (as on IRS EIN letter)' : 'Business Name (must match IRS docs)';
            if (bizNameInput) bizNameInput.placeholder = isSoleProp ? 'John Michael Doe' : 'ACME Insurance LLC';
        }
        window.spBizTypeChanged = spBizTypeChanged;

        var _spEinBypassWarning = false;

        // EIN yes/no toggle for sole proprietors
        function spSetEinAvailable(hasEin) {
            var yesBtn = document.getElementById('spEinYesBtn');
            var noBtn = document.getElementById('spEinNoBtn');
            var noCard = document.getElementById('spNoEinCard');
            var einLabel = document.getElementById('spEINLabel');
            var einInput = document.getElementById('spEIN');

            _spEinBypassWarning = false; // reset bypass when toggling

            if (hasEin) {
                if (yesBtn) { yesBtn.style.background = 'rgba(0,255,136,0.15)'; yesBtn.style.color = '#00ff88'; yesBtn.style.borderColor = 'rgba(0,255,136,0.5)'; }
                if (noBtn) { noBtn.style.background = 'rgba(0,0,0,0)'; noBtn.style.color = '#aaa'; noBtn.style.borderColor = 'rgba(255,255,255,0.1)'; }
                if (noCard) noCard.style.display = 'none';
                if (einLabel) einLabel.textContent = 'EIN (Tax ID)';
                if (einInput) einInput.placeholder = 'XX-XXXXXXX';
            } else {
                if (noBtn) { noBtn.style.background = 'rgba(255,165,0,0.12)'; noBtn.style.color = '#ffa500'; noBtn.style.borderColor = 'rgba(255,165,0,0.4)'; }
                if (yesBtn) { yesBtn.style.background = 'rgba(0,0,0,0)'; yesBtn.style.color = '#aaa'; yesBtn.style.borderColor = 'rgba(255,255,255,0.1)'; }
                if (noCard) noCard.style.display = 'block';
                if (einLabel) einLabel.textContent = 'SSN (Tax ID)';
                if (einInput) einInput.placeholder = 'XXX-XX-XXXX';
            }
        }
        window.spSetEinAvailable = spSetEinAvailable;

        // Show centered EIN warning modal — first click warns, second click bypasses
        function _showEinWarningModal() {
            var existing = document.getElementById('spEinWarningOverlay');
            if (existing) existing.remove();

            var overlay = document.createElement('div');
            overlay.id = 'spEinWarningOverlay';
            overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.7);z-index:99999;display:flex;align-items:center;justify-content:center;padding:20px;backdrop-filter:blur(4px);';

            overlay.innerHTML =
                '<div style="background:#0d0d0d;border:1px solid rgba(255,165,0,0.3);border-radius:16px;padding:32px 28px;max-width:400px;width:100%;text-align:center;box-shadow:0 32px 64px rgba(0,0,0,0.7);">' +
                    '<div style="margin-bottom:14px;"><i class="fa-solid fa-triangle-exclamation" style="font-size:2rem;color:#ffaa44;"></i></div>' +
                    '<div style="font-size:1rem;font-weight:800;color:#fff;margin-bottom:10px;">EIN Strongly Recommended</div>' +
                    '<div style="font-size:0.83rem;color:#aaa;line-height:1.75;margin-bottom:22px;">' +
                        'An EIN protects your Social Security Number and gives carriers the business identity they need to remove spam labels from your numbers. Getting one is <strong style="color:#fff;">completely free</strong> and takes about <strong style="color:#fff;">5 minutes</strong> on the IRS website.' +
                    '</div>' +
                    '<a href="/getting-ein" target="_blank" style="display:flex;align-items:center;justify-content:center;gap:8px;padding:12px;background:linear-gradient(135deg,#00ff88,#00cc6a);border-radius:9px;color:#000;font-size:0.88rem;font-weight:800;text-decoration:none;margin-bottom:10px;">' +
                        '<i class="fa-solid fa-arrow-up-right-from-square"></i> Get Your Free EIN from IRS.gov' +
                    '</a>' +
                    '<button onclick="_spContinueWithSsn()" style="width:100%;padding:11px;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.12);border-radius:9px;color:#888;font-size:0.82rem;font-weight:600;cursor:pointer;transition:all .2s;" onmouseover="this.style.color=\'#ccc\'" onmouseout="this.style.color=\'#888\'">' +
                        'Continue with SSN anyway' +
                    '</button>' +
                '</div>';

            document.body.appendChild(overlay);

            // Close on backdrop click
            overlay.addEventListener('click', function(e) {
                if (e.target === overlay) overlay.remove();
            });
        }

        function _spContinueWithSsn() {
            var overlay = document.getElementById('spEinWarningOverlay');
            if (overlay) overlay.remove();
            _spEinBypassWarning = true;
            registerSpamProtection();
        }
        window._spContinueWithSsn = _spContinueWithSsn;

        // ── EIN Document Upload ──
        var _spEinDocData = '';
        var _spEinDocType = '';
        var _spEinDocName = '';

        function spSetEinNew(isNew) {
            var yesBtn = document.getElementById('spEinNewYes');
            var noBtn = document.getElementById('spEinNewNo');
            var uploadArea = document.getElementById('spEinUploadArea');

            if (isNew) {
                if (yesBtn) { yesBtn.style.background = 'rgba(255,165,0,0.15)'; yesBtn.style.color = '#ffa500'; yesBtn.style.borderColor = 'rgba(255,165,0,0.5)'; }
                if (noBtn) { noBtn.style.background = 'rgba(0,0,0,0)'; noBtn.style.color = '#aaa'; noBtn.style.borderColor = 'rgba(255,255,255,0.1)'; }
                if (uploadArea) uploadArea.style.display = 'block';
            } else {
                if (noBtn) { noBtn.style.background = 'rgba(0,255,136,0.15)'; noBtn.style.color = '#00ff88'; noBtn.style.borderColor = 'rgba(0,255,136,0.5)'; }
                if (yesBtn) { yesBtn.style.background = 'rgba(0,0,0,0)'; yesBtn.style.color = '#aaa'; yesBtn.style.borderColor = 'rgba(255,255,255,0.1)'; }
                if (uploadArea) uploadArea.style.display = 'none';
                _spEinDocData = '';
                _spEinDocType = '';
                _spEinDocName = '';
            }
        }
        window.spSetEinNew = spSetEinNew;

        function spHandleEinFile(input) {
            var file = input.files && input.files[0];
            if (!file) return;

            // Validate file size (5MB max)
            if (file.size > 5 * 1024 * 1024) {
                alert('File must be under 5MB');
                input.value = '';
                return;
            }

            // Validate file type
            var validTypes = ['application/pdf', 'image/jpeg', 'image/jpg', 'image/png'];
            if (!validTypes.includes(file.type)) {
                alert('Please upload a PDF, JPG, or PNG file');
                input.value = '';
                return;
            }

            var reader = new FileReader();
            reader.onload = function(e) {
                _spEinDocData = e.target.result; // data:mimetype;base64,...
                _spEinDocType = file.type;
                _spEinDocName = file.name;

                var prompt = document.getElementById('spEinUploadPrompt');
                var info = document.getElementById('spEinUploadedInfo');
                var nameEl = document.getElementById('spEinFileName');
                if (prompt) prompt.style.display = 'none';
                if (info) info.style.display = 'flex';
                if (nameEl) nameEl.textContent = file.name;
            };
            reader.readAsDataURL(file);
        }
        window.spHandleEinFile = spHandleEinFile;

        function spClearEinFile(e) {
            if (e) e.stopPropagation();
            _spEinDocData = '';
            _spEinDocType = '';
            _spEinDocName = '';
            var input = document.getElementById('spEinFileInput');
            var prompt = document.getElementById('spEinUploadPrompt');
            var info = document.getElementById('spEinUploadedInfo');
            if (input) input.value = '';
            if (prompt) prompt.style.display = 'flex';
            if (info) info.style.display = 'none';
        }
        window.spClearEinFile = spClearEinFile;

        async function registerSpamProtection() {
            // If sole prop selected "No EIN" and hasn't acknowledged the warning yet, show modal first
            var isSoleProp = (document.getElementById('spBizType')?.value || '') === 'Sole Proprietorship';
            var noEinActive = document.getElementById('spEinNoBtn')?.style.color === 'rgb(255, 165, 0)';
            if (isSoleProp && noEinActive && !_spEinBypassWarning) {
                _showEinWarningModal();
                return;
            }

            const btn = document.getElementById('spRegisterBtn');
            const result = document.getElementById('spRegisterResult');
            // Build contact_name from separate first/last name fields
            var firstName = (document.getElementById('spContactFirstName')?.value || '').trim();
            var lastName = (document.getElementById('spContactLastName')?.value || '').trim();
            var contactName = (firstName + ' ' + lastName).trim();
            // Also populate the hidden field for backward compat
            var hiddenName = document.getElementById('spContactName');
            if (hiddenName) hiddenName.value = contactName;

            const payload = {
                business_name: document.getElementById('spBizName')?.value?.trim() || '',
                ein: document.getElementById('spEIN')?.value?.trim() || '',
                business_type: document.getElementById('spBizType')?.value || '',
                website: document.getElementById('spWebsite')?.value?.trim() || '',
                street: document.getElementById('spStreet')?.value?.trim() || '',
                city: document.getElementById('spCity')?.value?.trim() || '',
                state: document.getElementById('spState')?.value?.trim() || '',
                zip: document.getElementById('spZip')?.value?.trim() || '',
                contact_name: contactName,
                contact_title: document.getElementById('spContactTitle')?.value?.trim() || '',
                contact_email: document.getElementById('spContactEmail')?.value?.trim() || '',
                contact_phone: document.getElementById('spContactPhone')?.value?.trim() || '',
            };
            // Include EIN document if uploaded
            if (_spEinDocData) {
                payload.ein_document_data = _spEinDocData;
                payload.ein_document_type = _spEinDocType;
                payload.ein_document_name = _spEinDocName;
            }
            if (!payload.business_name) { result.innerHTML = '<span style="color:#ef4444;">Business name is required</span>'; return; }
            if (!payload.ein) { result.innerHTML = '<span style="color:#ef4444;">' + (payload.business_type === 'Sole Proprietorship' ? 'SSN is required' : 'EIN is required') + '</span>'; return; }
            if (!payload.business_type) { result.innerHTML = '<span style="color:#ef4444;">Business type is required</span>'; return; }
            if (!firstName || !lastName) { result.innerHTML = '<span style="color:#ef4444;">Authorized representative first and last name are required</span>'; return; }
            if (!payload.contact_email) { result.innerHTML = '<span style="color:#ef4444;">Authorized representative email is required</span>'; return; }
            if (!payload.contact_phone) { result.innerHTML = '<span style="color:#ef4444;">Authorized representative phone is required</span>'; return; }
            if (!payload.street || !payload.city || !payload.state || !payload.zip) { result.innerHTML = '<span style="color:#ef4444;">Complete business address is required</span>'; return; }

            btn.disabled = true;
            btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-2"></i>Registering & protecting numbers...';
            result.innerHTML = '';
            try {
                const r = await fetch('/voice/spam-protection/register', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(payload),
                });
                const d = await r.json();
                if (r.ok && d.has_profile) {
                    // Build step-by-step result display
                    var stepsHtml = '';
                    (d.results?.steps || []).forEach(function(s) {
                        var label = {'customer_profile':'Business Profile','secondary_profile':'Business Profile',
                            'end_user_business':'Business Identity','auth_representative':'Authorized Contact',
                            'address':'Business Address','assign_numbers':'Number Assignment',
                            'evaluation':'Profile Evaluation','submit_review':'Submit for Review',
                            'cnam_all_numbers':'Caller ID Labels'}[s.name] || s.name;
                        var icon = s.status === 'ok' ? '<i class="fa-solid fa-circle-check" style="color:#00ff88;"></i>' :
                                   s.status === 'skipped' ? '<i class="fa-solid fa-forward" style="color:#888;"></i>' :
                                   '<i class="fa-solid fa-triangle-exclamation" style="color:#ffa500;"></i>';
                        stepsHtml += '<div style="font-size:.75rem;color:#ccc;padding:2px 0;">' + icon + ' ' + _esc(label) + '</div>';
                    });
                    var cnamMsg = d.cnam?.status === 'ok' ? '<div style="color:#00ff88;font-size:.78rem;margin-top:4px;"><i class="fa-solid fa-circle-check me-1"></i>Caller ID: ' + _esc(d.cnam_display_name || '') + '</div>' :
                                  d.cnam?.status === 'deferred' ? '<div style="color:#ffa500;font-size:.78rem;margin-top:4px;"><i class="fa-solid fa-clock me-1"></i>Caller ID (CNAM) will register automatically once your profile is approved.</div>' :
                                  d.cnam?.status === 'error' ? '<div style="color:#ffa500;font-size:.78rem;margin-top:4px;"><i class="fa-solid fa-triangle-exclamation me-1"></i>Caller ID: ' + _esc(d.cnam.error || 'Failed') + '</div>' : '';
                    result.innerHTML = '<div style="color:#00ff88;font-weight:600;margin-bottom:4px;"><i class="fa-solid fa-circle-check me-1"></i>Registration submitted!</div>' + stepsHtml + cnamMsg;
                    setTimeout(() => loadSpamProtectionStatus(), 500);
                } else if (r.ok && !d.has_profile) {
                    // Profile creation failed — show detailed errors
                    var errList = (d.errors || []).map(function(e) { return '<div style="color:#ef4444;font-size:.75rem;padding:2px 0;"><i class="fa-solid fa-xmark me-1"></i>' + _esc(e) + '</div>'; }).join('');
                    result.innerHTML = '<div style="color:#ef4444;font-weight:600;margin-bottom:4px;"><i class="fa-solid fa-triangle-exclamation me-1"></i>Registration failed</div>' +
                        '<div style="font-size:.78rem;color:#ccc;margin-bottom:4px;">Business Profile could not be created. Check your details and try again.</div>' +
                        errList +
                        '<div style="font-size:.72rem;color:#888;margin-top:6px;">If this persists, contact support with the error details above.</div>';
                    if (typeof _showDashToast === 'function') _showDashToast(false, 'Spam Protection setup failed — see details');
                } else {
                    result.innerHTML = '<span style="color:#ef4444;"><i class="fa-solid fa-triangle-exclamation me-1"></i>' + _esc(d.error || 'Registration failed') + '</span>';
                }
            } catch(e) {
                result.innerHTML = '<span style="color:#ef4444;">Network error — check your connection</span>';
            }
            btn.disabled = false;
            btn.innerHTML = '<i class="fa-solid fa-shield-halved me-2"></i>Register & Protect All Numbers';
        }

        // Backward compatibility aliases
        function saveTrustHub() { registerSpamProtection(); }
        function markCarrierRegistered() { /* no-op, carriers handled automatically now */ }

        // Keep old function name as alias for backward compatibility
        function loadTrustHubNumbers() { loadNumbersTab(); }

        // ===== CNAM MONITOR & LOOKUP =====

        async function loadCnamMonitor() {
            var listEl = document.getElementById('cnamNumbersList');
            var nameInput = document.getElementById('cnamDisplayNameInput');
            if (listEl) listEl.innerHTML = '<div class="sm-loading"><i class="fa-solid fa-spinner fa-spin me-1"></i>Loading numbers...</div>';

            try {
                var r = await fetch('/voice/cnam/monitor');
                if (!r.ok) {
                    var d = {};
                    try { d = await r.json(); } catch(_) {}
                    if (listEl) listEl.innerHTML = '<div class="sm-error"><i class="fa-solid fa-triangle-exclamation me-1"></i>' + _esc(d.error || 'Failed to load') + '</div>';
                    return;
                }
                var d = await r.json();
                var total = d.total || 0;
                var compliant = d.cnam_compliant || 0;
                var displayName = d.cnam_display_name || '';
                var allGood = compliant === total && total > 0;

                // Populate the single display-name input with the current carrier-registered name.
                // Only fill it if the user hasn't typed anything yet (avoid clobbering edits).
                if (nameInput && !nameInput.dataset.userEdited) {
                    nameInput.value = displayName;
                    _cnamUpdateCharCount();
                }

                // Accordion badge — honor actual Twilio Trust Product status
                var tpStatus = (d.trust_product || {}).status || 'not_registered';
                var tpRegistered = (d.trust_product || {}).registered;
                var cBadge = document.getElementById('smBadgeCnam');
                if (cBadge) {
                    if (tpStatus === 'twilio-rejected') {
                        cBadge.textContent = 'Rejected';
                        cBadge.className = 'sm-accordion-badge sm-badge-err';
                    } else if (!tpRegistered || tpStatus === 'not_registered') {
                        cBadge.textContent = 'Not registered';
                        cBadge.className = 'sm-accordion-badge sm-badge-warn';
                    } else if (tpStatus === 'twilio-approved' || tpStatus === 'approved') {
                        cBadge.textContent = 'Live — ' + _esc(displayName);
                        cBadge.className = 'sm-accordion-badge sm-badge-ok';
                    } else {
                        // pending-review, in-review, draft
                        cBadge.textContent = 'Pending Review';
                        cBadge.className = 'sm-accordion-badge sm-badge-pending';
                    }
                    cBadge.style.display = 'inline-block';
                }

                // Numbers list — read-only view. All numbers share one carrier display name.
                if (listEl) {
                    var nums = d.numbers || [];
                    if (!nums.length) {
                        listEl.innerHTML = '<div class="sm-empty">No numbers found. Buy a number in the Numbers tab first.</div>';
                        return;
                    }

                    var tpIsRejected = tpStatus === 'twilio-rejected';
                    var tpIsApproved = tpStatus === 'twilio-approved' || tpStatus === 'approved';
                    var tpIsPending = tpRegistered && !tpIsApproved && !tpIsRejected;
                    var html = '<ul class="sm-numbers-list">';
                    var unregisteredCount = 0;
                    nums.forEach(function(n) {
                        var statusLabel, statusClass;
                        if (tpIsRejected && n.assigned_to_trust_product) {
                            statusLabel = 'Rejected — re-register'; statusClass = 'sm-dot-error';
                        } else if (n.cnam_compliant) {
                            statusLabel = 'Live at carrier'; statusClass = 'sm-dot-ok';
                        } else if (n.assigned_to_trust_product && tpIsPending) {
                            statusLabel = 'Pending Review'; statusClass = 'sm-dot-pending';
                        } else if (n.assigned_to_trust_product && tpIsApproved) {
                            statusLabel = 'Propagating (48–72h)'; statusClass = 'sm-dot-pending';
                        } else {
                            statusLabel = 'Not registered'; statusClass = 'sm-dot-off';
                            unregisteredCount++;
                        }
                        html += '<li class="sm-numbers-row">' +
                            '<span class="sm-num-phone">' + _esc(_fmtPhone(n.phone)) + '</span>' +
                            '<span class="sm-num-name">' + _esc(displayName || n.cnam_name || '—') + '</span>' +
                            '<span class="sm-num-status"><span class="sm-dot ' + statusClass + '"></span>' + statusLabel + '</span>' +
                        '</li>';
                    });
                    html += '</ul>';
                    listEl.innerHTML = html;

                    // Show "Register All" button when unregistered numbers exist and TP is approved
                    var regBtn = document.getElementById('cnamRegisterAllBtn');
                    if (regBtn) {
                        var showRegBtn = unregisteredCount > 0 && tpIsApproved;
                        regBtn.style.display = showRegBtn ? 'inline-flex' : 'none';
                        regBtn.innerHTML = '<i class="fa-solid fa-plus me-1"></i>Register ' +
                            (unregisteredCount === 1 ? '1 Number' : unregisteredCount + ' Numbers');
                    }

                    // Show rejection banner if Trust Product was rejected
                    if (tpIsRejected) {
                        listEl.insertAdjacentHTML('beforebegin',
                            '<div id="cnamRejectedBanner" class="sm-error" style="margin-bottom:10px;">' +
                            '<i class="fa-solid fa-circle-xmark me-1"></i>' +
                            'Your CNAM registration was rejected by Twilio. ' +
                            'Re-register via <strong>Spam Protection</strong> once your Business Profile is approved.' +
                            '</div>');
                    } else {
                        var oldBanner = document.getElementById('cnamRejectedBanner');
                        if (oldBanner) oldBanner.remove();
                    }
                }

                // Show/hide the name edit section based on registration status
                var nameCard = document.getElementById('cnamNameCard');
                if (nameCard) {
                    // Only show name editing when CNAM Trust Product exists and isn't rejected
                    nameCard.style.display = (tpRegistered && !tpIsRejected) ? 'block' : 'none';
                }
            } catch(e) {
                console.error('[CNAM Monitor]', e);
                if (listEl) listEl.innerHTML = '<div class="sm-error">Network error</div>';
            }
        }

        // Register all unregistered numbers with CNAM
        async function cnamRegisterUnregistered() {
            var btn = document.getElementById('cnamRegisterAllBtn');
            if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-1"></i>Registering...'; }
            try {
                var r = await fetch('/voice/cnam/add-numbers', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({})
                });
                var d = await r.json();
                if (r.ok) {
                    if (typeof _showDashToast === 'function') _showDashToast(true, (d.added || 0) + ' number(s) registered for CNAM');
                    loadCnamMonitor();
                } else {
                    if (typeof _showDashToast === 'function') _showDashToast(false, d.error || 'Registration failed');
                }
            } catch(e) {
                console.error('[CNAM Register]', e);
                if (typeof _showDashToast === 'function') _showDashToast(false, 'Network error');
            } finally {
                if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-plus me-1"></i>Register All'; }
            }
        }
        window.cnamRegisterUnregistered = cnamRegisterUnregistered;

        // Character counter + user-edit tracking for the display-name input
        function _cnamUpdateCharCount() {
            var inp = document.getElementById('cnamDisplayNameInput');
            var cc = document.getElementById('cnamCharCount');
            if (inp && cc) cc.textContent = inp.value.length;
        }
        (function() {
            if (typeof document === 'undefined') return;
            document.addEventListener('input', function(e) {
                if (e.target && e.target.id === 'cnamDisplayNameInput') {
                    e.target.dataset.userEdited = '1';
                    _cnamUpdateCharCount();
                }
            });
        })();

        // Update the carrier-registered CNAM display name.
        // Hits /voice/cnam/update-name which calls update_cnam_display_name()
        // on the Trust Product EndUser — this is the REAL carrier-visible name,
        // not the Twilio internal friendly_name label.
        async function cnamUpdateDisplayName() {
            var inp = document.getElementById('cnamDisplayNameInput');
            var btn = document.getElementById('cnamUpdateNameBtn');
            var resultEl = document.getElementById('cnamUpdateResult');
            if (!inp) return;
            var name = (inp.value || '').trim();
            if (resultEl) resultEl.innerHTML = '';

            if (!name) {
                if (resultEl) resultEl.innerHTML = '<div class="sm-error">Enter a display name.</div>';
                return;
            }
            if (name.length > 15) {
                if (resultEl) resultEl.innerHTML = '<div class="sm-error">Max 15 characters.</div>';
                return;
            }
            if (!/^[A-Za-z]/.test(name)) {
                if (resultEl) resultEl.innerHTML = '<div class="sm-error">Must start with a letter.</div>';
                return;
            }
            if (!/^[A-Za-z0-9., ]+$/.test(name)) {
                if (resultEl) resultEl.innerHTML = '<div class="sm-error">Only letters, numbers, periods, commas, and spaces are allowed.</div>';
                return;
            }

            if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-1"></i>Submitting to carriers...'; }
            try {
                var r = await fetch('/voice/cnam/update-name', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ display_name: name }),
                });
                var d = await r.json();
                if (r.ok && d.status === 'ok') {
                    if (resultEl) {
                        resultEl.innerHTML = '<div class="sm-success"><i class="fa-solid fa-circle-check me-1"></i>Submitted to carrier database. Propagation takes <strong>48–72 hours</strong>. New name: <strong>' + _esc(d.cnam_display_name || name) + '</strong></div>';
                    }
                    inp.dataset.userEdited = '';
                    if (typeof _showDashToast === 'function') _showDashToast(true, 'CNAM submitted to carriers');
                    loadCnamMonitor();
                } else {
                    var msg = d.error || 'Update failed';
                    if (resultEl) resultEl.innerHTML = '<div class="sm-error"><i class="fa-solid fa-triangle-exclamation me-1"></i>' + _esc(msg) + '</div>';
                }
            } catch(e) {
                if (resultEl) resultEl.innerHTML = '<div class="sm-error">Network error</div>';
            } finally {
                if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-paper-plane me-1"></i>Update Carrier CNAM'; }
            }
        }

        // Legacy per-number CNAM editing removed: CNAM is ONE carrier-registered
        // display name per account (via the Trust Product EndUser), not per number.
        // Editing is now handled by cnamUpdateDisplayName() above, which hits the
        // correct /voice/cnam/update-name endpoint.

        async function cnamLookup() {
            var phone = (document.getElementById('cnamLookupPhone') || {}).value || '';
            var resultEl = document.getElementById('cnamLookupResult');
            var btn = document.getElementById('cnamLookupBtn');
            if (!phone.trim()) { if (resultEl) resultEl.innerHTML = '<div class="cnam-error">Enter a phone number</div>'; return; }

            if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-1"></i>Looking up...'; }
            if (resultEl) resultEl.innerHTML = '';

            try {
                var r = await fetch('/voice/cnam/lookup', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ phone: phone.trim() }),
                });
                var d = await r.json();
                if (d.error && !d.caller_name) {
                    resultEl.innerHTML = '<div class="cnam-lookup-card cnam-lookup-error">' +
                        '<i class="fa-solid fa-triangle-exclamation me-2"></i>Lookup failed: ' + _esc(d.error) +
                    '</div>';
                } else {
                    var callerName = d.caller_name || 'Not available';
                    var callerType = d.caller_type || 'Unknown';
                    var typeIcon = callerType === 'BUSINESS' ? 'fa-building' : callerType === 'CONSUMER' ? 'fa-user' : 'fa-question';

                    resultEl.innerHTML = '<div class="cnam-lookup-card">' +
                        '<div class="cnam-lookup-row">' +
                            '<span class="cnam-lookup-label">Phone</span>' +
                            '<span class="cnam-lookup-value">' + _esc(_fmtPhone(d.phone || phone)) + '</span>' +
                        '</div>' +
                        '<div class="cnam-lookup-row">' +
                            '<span class="cnam-lookup-label">Caller Name</span>' +
                            '<span class="cnam-lookup-value cnam-lookup-name">' + _esc(callerName) + '</span>' +
                        '</div>' +
                        '<div class="cnam-lookup-row">' +
                            '<span class="cnam-lookup-label">Type</span>' +
                            '<span class="cnam-lookup-value"><i class="fa-solid ' + typeIcon + ' me-1"></i>' + _esc(callerType) + '</span>' +
                        '</div>' +
                    '</div>';
                }
            } catch(e) {
                if (resultEl) resultEl.innerHTML = '<div class="cnam-lookup-card cnam-lookup-error"><i class="fa-solid fa-wifi me-2"></i>Network error</div>';
            }
            if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-magnifying-glass me-1"></i>Lookup'; }
        }

        async function cnamLookupOwn() {
            var resultEl = document.getElementById('cnamLookupOwnResult');
            var btn = document.getElementById('cnamLookupOwnBtn');
            if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-1"></i>Checking all numbers...'; }
            if (resultEl) resultEl.innerHTML = '';

            try {
                var r = await fetch('/voice/cnam/lookup-own');
                var d = await r.json();
                if (!r.ok || d.error) {
                    resultEl.innerHTML = '<div class="cnam-error">' + _esc(d.error || 'Failed') + '</div>';
                    if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-list-check me-1"></i>Check All My Numbers Against Carrier Database'; }
                    return;
                }

                var nums = d.numbers || [];
                if (!nums.length) {
                    resultEl.innerHTML = '<div class="cnam-empty">No numbers to check.</div>';
                    if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-list-check me-1"></i>Check All My Numbers Against Carrier Database'; }
                    return;
                }

                var html = '<div class="cnam-propagation-summary mb-2">' +
                    '<span class="cnam-stat-pill ' + (d.propagated === d.total ? 'cnam-stat-good' : 'cnam-stat-warn') + '">' +
                        d.propagated + '/' + d.total + ' propagated to carriers' +
                    '</span>' +
                '</div>';

                html += '<div class="cnam-table-header">' +
                    '<div class="cnam-col-phone">Phone</div>' +
                    '<div class="cnam-col-name">Set As</div>' +
                    '<div class="cnam-col-name">Carrier Sees</div>' +
                    '<div class="cnam-col-status">Match</div>' +
                '</div>';

                nums.forEach(function(n) {
                    var propClass = n.propagated ? 'cnam-status-ok' : (n.error ? 'cnam-status-off' : 'cnam-status-warn');
                    var propIcon = n.propagated ? 'fa-circle-check' : (n.error ? 'fa-circle-xmark' : 'fa-triangle-exclamation');
                    var propLabel = n.propagated ? 'Propagated' : (n.error ? 'Error' : 'Not Propagated');

                    html += '<div class="cnam-row">' +
                        '<div class="cnam-col-phone">' + _esc(_fmtPhone(n.phone)) + '</div>' +
                        '<div class="cnam-col-name">' + _esc(n.set_name || '—') + '</div>' +
                        '<div class="cnam-col-name">' + _esc(n.carrier_name || '—') + '</div>' +
                        '<div class="cnam-col-status">' +
                            '<span class="cnam-status-badge ' + propClass + '">' +
                                '<i class="fa-solid ' + propIcon + ' me-1"></i>' + propLabel +
                            '</span>' +
                        '</div>' +
                    '</div>';
                });

                resultEl.innerHTML = html;
            } catch(e) {
                if (resultEl) resultEl.innerHTML = '<div class="cnam-error">Network error</div>';
            }
            if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-list-check me-1"></i>Check All My Numbers Against Carrier Database'; }
        }

        // ===== NUMBER INTEGRITY (Voice Integrity) =====
        var _niData = null; // cached status data
        var _niSelectedSids = new Set();

        async function loadNumberIntegrity() {
            var listEl = document.getElementById('niNumbersList');
            var bannerEl = document.getElementById('niStatusBanner');
            var registerBtn = document.getElementById('niRegisterBtn');
            var remediateBtn = document.getElementById('niRemediateBtn');
            var carrierListEl = document.getElementById('niCarrierList');
            var resultEl = document.getElementById('niActionResult');
            if (listEl) listEl.innerHTML = '<div class="ni-loading"><i class="fa-solid fa-spinner fa-spin me-1"></i>Loading numbers...</div>';
            if (resultEl) resultEl.innerHTML = '';

            try {
                var r = await fetch('/voice/number-integrity/status');
                if (!r.ok) {
                    var d = {};
                    try { d = await r.json(); } catch(_) {}
                    if (listEl) listEl.innerHTML = '<div class="ni-error"><i class="fa-solid fa-triangle-exclamation me-1"></i>' + _esc(d.error || 'Failed to load (HTTP ' + r.status + ')') + '</div>';
                    return;
                }
                var d = await r.json();
                if (d.error) {
                    if (listEl) listEl.innerHTML = '<div class="ni-error"><i class="fa-solid fa-triangle-exclamation me-1"></i>' + _esc(d.error) + '</div>';
                    return;
                }
                _niData = d;

                var isActive = d.status === 'twilio-approved';
                var isPending = d.status === 'pending-review' || d.status === 'in-review';
                var isRejected = d.status === 'twilio-rejected';

                // Render carrier cards
                if (carrierListEl && d.carriers) {
                    var carrierHtml = '';
                    d.carriers.forEach(function(c) {
                        var statusClass = isActive ? 'ni-carrier-active' : (isPending ? 'ni-carrier-pending' : (isRejected ? 'ni-carrier-rejected' : 'ni-carrier-inactive'));
                        var statusIcon = isActive ? 'fa-circle-check' : (isPending ? 'fa-clock' : 'fa-circle-xmark');
                        var statusLabel = isActive ? 'Registered' : (isPending ? 'Pending' : (isRejected ? 'Rejected' : 'Not Registered'));
                        var logoMap = { att: '/static/img/carriers/att.svg', tmobile: '/static/img/carriers/tmobile.png', verizon: '/static/img/carriers/verizon.svg' };
                        var logoSrc = logoMap[c.key] || '';
                        var iconHtml = logoSrc
                            ? '<img src="' + logoSrc + '" alt="' + _esc(c.name) + '" class="ni-carrier-logo">'
                            : '<i class="fa-solid ' + c.icon + '"></i>';
                        carrierHtml += '<div class="col-md-4">' +
                            '<div class="ni-carrier-card ' + statusClass + '">' +
                                '<div class="ni-carrier-icon">' + iconHtml + '</div>' +
                                '<div class="ni-carrier-name">' + _esc(c.name) + '</div>' +
                                '<div class="ni-carrier-status"><i class="fa-solid ' + statusIcon + ' me-1"></i>' + statusLabel + '</div>' +
                                '<div class="ni-carrier-desc">' + _esc(c.description) + '</div>' +
                            '</div>' +
                        '</div>';
                    });
                    carrierListEl.innerHTML = carrierHtml;
                }

                // Show status banner if registered
                if (bannerEl && d.status !== 'not_registered') {
                    var disp = d.display || {};
                    var bannerClass = 'ni-banner-' + (disp.color || 'gray');
                    bannerEl.style.display = 'block';
                    bannerEl.className = 'ni-status-banner ' + bannerClass + ' mb-3 p-3';
                    var detailText = d.business_name ? '<strong>' + _esc(d.business_name) + '</strong> &mdash; ' : '';

                    if (isRejected) {
                        // Build rejection banner with reasons + action buttons
                        detailText += 'Registration was rejected. See reasons below.';
                        var failureReasons = d.failure_reasons || [];
                        var bannerHtml =
                            '<div class="d-flex align-items-start gap-3">' +
                                '<div class="ni-banner-icon"><i class="fa-solid ' + (disp.icon || 'fa-circle-xmark') + '"></i></div>' +
                                '<div class="ni-banner-body" style="flex:1;">' +
                                    '<div class="ni-banner-title">' + _esc(disp.label || 'Rejected') + '</div>' +
                                    '<div class="ni-banner-detail">' + detailText + '</div>';

                        // Show failure reasons if available
                        if (failureReasons.length > 0) {
                            bannerHtml += '<div class="ni-rejection-reasons">' +
                                '<div class="ni-rejection-reasons-title"><i class="fa-solid fa-circle-exclamation me-1"></i>Rejection reasons:</div>' +
                                '<ul class="ni-rejection-reasons-list">';
                            failureReasons.forEach(function(reason) {
                                bannerHtml += '<li>' + _esc(reason) + '</li>';
                            });
                            bannerHtml += '</ul></div>';
                        } else {
                            bannerHtml += '<div class="ni-rejection-reasons">' +
                                '<div class="ni-rejection-reasons-hint"><i class="fa-solid fa-info-circle me-1"></i>No specific reason available. Try editing your info and resubmitting.</div>' +
                            '</div>';
                        }

                        // Action buttons for rejection
                        bannerHtml += '<div class="ni-rejection-actions">' +
                                '<button onclick="niShowEditForm()" class="ni-rejection-edit-btn"><i class="fa-solid fa-pen-to-square me-1"></i>Edit & Resubmit</button>' +
                                '<button onclick="niResubmit()" class="ni-rejection-resubmit-btn"><i class="fa-solid fa-rotate-right me-1"></i>Resubmit As-Is</button>' +
                            '</div>' +
                            '</div>' +  // close ni-banner-body
                        '</div>';      // close flex container

                        bannerEl.innerHTML = bannerHtml;
                    } else {
                        detailText += d.assigned_count + ' number' + (d.assigned_count !== 1 ? 's' : '') + ' registered';
                        if (d.registered_at) detailText += ' &bull; Since ' + new Date(d.registered_at).toLocaleDateString();
                        bannerEl.innerHTML =
                            '<div class="d-flex align-items-center gap-3">' +
                                '<div class="ni-banner-icon"><i class="fa-solid ' + (disp.icon || 'fa-circle-info') + '"></i></div>' +
                                '<div class="ni-banner-body">' +
                                    '<div class="ni-banner-title">' + _esc(disp.label || d.status) + '</div>' +
                                    '<div class="ni-banner-detail">' + detailText + '</div>' +
                                '</div>' +
                                (isActive ?
                                    '<button onclick="niRemediate()" class="ni-banner-remediate-btn"><i class="fa-solid fa-wrench me-1"></i>Remediate</button>' : '') +
                            '</div>';
                    }
                } else if (bannerEl) {
                    bannerEl.style.display = 'none';
                }

                // Update accordion badge
                var niBadge = document.getElementById('smBadgeVoiceIntegrity');
                if (niBadge) {
                    if (isActive) {
                        niBadge.textContent = 'Active';
                        niBadge.style.cssText = 'display:inline-block;background:rgba(0,255,136,0.12);color:#00ff88;font-size:0.7rem;font-weight:600;padding:2px 8px;border-radius:10px;';
                    } else if (isPending) {
                        niBadge.textContent = 'Pending';
                        niBadge.style.cssText = 'display:inline-block;background:rgba(255,165,0,0.12);color:#ffa500;font-size:0.7rem;font-weight:600;padding:2px 8px;border-radius:10px;';
                    } else if (isRejected) {
                        niBadge.textContent = 'Rejected';
                        niBadge.style.cssText = 'display:inline-block;background:rgba(239,68,68,0.12);color:#ef4444;font-size:0.7rem;font-weight:600;padding:2px 8px;border-radius:10px;';
                    } else {
                        niBadge.textContent = 'Not registered';
                        niBadge.style.cssText = 'display:inline-block;background:rgba(255,255,255,0.06);color:#888;font-size:0.7rem;font-weight:600;padding:2px 8px;border-radius:10px;';
                    }
                }

                // Show/hide edit form (hidden by default, shown when user clicks Edit & Resubmit)
                var editFormEl = document.getElementById('niEditForm');
                if (editFormEl) editFormEl.style.display = 'none';

                // Render phone numbers list
                if (listEl) {
                    var nums = d.numbers || [];
                    if (!nums.length) {
                        listEl.innerHTML = '<div class="ni-empty">No numbers found. Buy a number in the Numbers tab first.</div>';
                    } else {
                        var html = '';
                        nums.forEach(function(n) {
                            // Only lock checkboxes when truly registered (approved/pending Trust Product)
                            var checked = n.registered ? ' checked disabled' : '';
                            var badge = '';
                            if (n.registered) {
                                badge = '<span class="ni-badge-registered"><i class="fa-solid fa-circle-check me-1"></i>Registered</span>';
                            } else if (n.assigned && isRejected) {
                                // Was submitted but Trust Product was rejected — show as failed, keep selectable
                                checked = ' checked';
                                badge = '<span class="ni-badge-rejected"><i class="fa-solid fa-circle-xmark me-1"></i>Not Registered</span>';
                            }
                            html += '<label class="ni-number-row">' +
                                '<input type="checkbox" class="ni-number-cb" data-sid="' + _esc(n.sid) + '" onchange="niUpdateSelection()"' + checked + '>' +
                                '<span class="ni-number-phone">' + _esc(_fmtPhone(n.phone)) + '</span>' +
                                (n.friendly_name ? '<span class="ni-number-name">' + _esc(n.friendly_name) + '</span>' : '') +
                                badge +
                            '</label>';
                        });
                        listEl.innerHTML = html;
                    }
                }

                // Show/hide action buttons based on status
                // Register: show when not yet submitted, approved (add more), or rejected (re-register with new numbers)
                if (registerBtn) {
                    var showRegister = d.status === 'not_registered' || d.status === 'draft' || isActive || isRejected;
                    registerBtn.style.display = showRegister ? '' : 'none';
                    if (isActive) {
                        registerBtn.innerHTML = '<i class="fa-solid fa-plus me-2"></i>Add More Numbers';
                    } else if (isRejected) {
                        registerBtn.innerHTML = '<i class="fa-solid fa-plus me-2"></i>Add Numbers';
                    } else {
                        registerBtn.innerHTML = '<i class="fa-solid fa-tower-broadcast me-2"></i>Register Selected Numbers';
                    }
                }
                // Remediate: only show when approved (rejected uses Resubmit flow instead)
                if (remediateBtn) remediateBtn.style.display = isActive ? '' : 'none';

                _niSelectedSids.clear();
                niUpdateSelection();

            } catch(e) {
                console.error('[NumberIntegrity] Load error:', e);
                if (listEl) listEl.innerHTML = '<div class="ni-error">Network error &mdash; check your connection</div>';
            }
        }

        function niUpdateSelection() {
            _niSelectedSids.clear();
            var cbs = document.querySelectorAll('.ni-number-cb:checked:not(:disabled)');
            cbs.forEach(function(cb) { _niSelectedSids.add(cb.dataset.sid); });
            var countEl = document.getElementById('niSelectedCount');
            if (countEl) countEl.textContent = _niSelectedSids.size + ' selected';
        }

        function niSelectAll() {
            document.querySelectorAll('.ni-number-cb:not(:disabled)').forEach(function(cb) { cb.checked = true; });
            niUpdateSelection();
        }

        function niDeselectAll() {
            document.querySelectorAll('.ni-number-cb:not(:disabled)').forEach(function(cb) { cb.checked = false; });
            niUpdateSelection();
        }

        async function niRegister() {
            var resultEl = document.getElementById('niActionResult');
            if (_niSelectedSids.size === 0) {
                if (resultEl) resultEl.innerHTML = '<span class="ni-result-error"><i class="fa-solid fa-triangle-exclamation me-1"></i>Select at least one number to register</span>';
                return;
            }
            var btn = document.getElementById('niRegisterBtn');
            // Add mode only if trust product exists and is NOT rejected
            // Rejected = need fresh trust product via /register, not /add-numbers
            var isRejectedState = _niData && _niData.status === 'twilio-rejected';
            var isAddMode = _niData && _niData.trust_product_sid && !isRejectedState;
            if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-2"></i>Registering with carrier networks...'; }
            if (resultEl) resultEl.innerHTML = '';

            var endpoint = isAddMode ? '/voice/number-integrity/add-numbers' : '/voice/number-integrity/register';

            try {
                var r = await fetch(endpoint, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ phone_number_sids: Array.from(_niSelectedSids) }),
                });
                var d = await r.json();
                if (r.ok && d.status === 'ok') {
                    var count = d.numbers_assigned || 0;
                    var msg = count + ' number' + (count !== 1 ? 's' : '') + ' submitted for carrier registration.';
                    if (d.numbers_failed > 0) msg += ' ' + d.numbers_failed + ' failed.';
                    msg += ' Registration takes 24\u201348 hours.';
                    if (resultEl) resultEl.innerHTML = '<span class="ni-result-success"><i class="fa-solid fa-circle-check me-1"></i>' + msg + '</span>';
                    setTimeout(function() { loadNumberIntegrity(); }, 1500);
                } else {
                    if (resultEl) resultEl.innerHTML = '<span class="ni-result-error"><i class="fa-solid fa-triangle-exclamation me-1"></i>' + _esc(d.error || 'Registration failed') + '</span>';
                }
            } catch(e) {
                console.error('[NumberIntegrity] Register error:', e);
                if (resultEl) resultEl.innerHTML = '<span class="ni-result-error">Network error &mdash; check your connection</span>';
            }
            if (btn) {
                btn.disabled = false;
                btn.innerHTML = isAddMode
                    ? '<i class="fa-solid fa-plus me-2"></i>Add More Numbers'
                    : '<i class="fa-solid fa-tower-broadcast me-2"></i>Register Selected Numbers';
            }
        }

        async function niRemediate() {
            var btn = document.getElementById('niRemediateBtn');
            var resultEl = document.getElementById('niActionResult');
            if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-2"></i>Submitting remediation...'; }
            if (resultEl) resultEl.innerHTML = '';

            try {
                var r = await fetch('/voice/number-integrity/remediate', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: '{}',
                });
                var d = await r.json();
                if (r.ok && d.status === 'ok') {
                    var msg = d.message || 'Remediation submitted successfully.';
                    if (resultEl) resultEl.innerHTML = '<span class="ni-result-success"><i class="fa-solid fa-circle-check me-1"></i>' + _esc(msg) + '</span>';
                    setTimeout(function() { loadNumberIntegrity(); }, 1500);
                } else {
                    // Covers 409 (already pending), 400 (draft), and 500 errors
                    var errMsg = d.error || d.message || 'Remediation failed';
                    var cssClass = r.status === 409 ? 'ni-result-warning' : 'ni-result-error';
                    var icon = r.status === 409 ? 'fa-clock' : 'fa-triangle-exclamation';
                    if (resultEl) resultEl.innerHTML = '<span class="' + cssClass + '"><i class="fa-solid ' + icon + ' me-1"></i>' + _esc(errMsg) + '</span>';
                }
            } catch(e) {
                console.error('[NumberIntegrity] Remediate error:', e);
                if (resultEl) resultEl.innerHTML = '<span class="ni-result-error">Network error &mdash; check your connection</span>';
            }
            if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-wrench me-2"></i>Remediate Spam Labels'; }
        }

        async function niResubmit() {
            var resultEl = document.getElementById('niActionResult');
            if (resultEl) resultEl.innerHTML = '<span class="ni-result-info"><i class="fa-solid fa-spinner fa-spin me-1"></i>Resubmitting for review...</span>';

            try {
                var r = await fetch('/voice/number-integrity/resubmit', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: '{}',
                });
                var d = await r.json();
                if (r.ok && d.status === 'ok') {
                    var msg = d.message || 'Resubmitted successfully.';
                    if (resultEl) resultEl.innerHTML = '<span class="ni-result-success"><i class="fa-solid fa-circle-check me-1"></i>' + _esc(msg) + '</span>';
                    setTimeout(function() { loadNumberIntegrity(); }, 1500);
                } else {
                    var errMsg = d.error || 'Resubmit failed';
                    var cssClass = r.status === 409 ? 'ni-result-warning' : 'ni-result-error';
                    var icon = r.status === 409 ? 'fa-clock' : 'fa-triangle-exclamation';
                    if (resultEl) resultEl.innerHTML = '<span class="' + cssClass + '"><i class="fa-solid ' + icon + ' me-1"></i>' + _esc(errMsg) + '</span>';
                }
            } catch(e) {
                console.error('[NumberIntegrity] Resubmit error:', e);
                if (resultEl) resultEl.innerHTML = '<span class="ni-result-error">Network error</span>';
            }
        }

        function niShowEditForm() {
            var editFormEl = document.getElementById('niEditForm');
            if (editFormEl) editFormEl.style.display = 'block';
            // Scroll to the edit form
            if (editFormEl) editFormEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }

        function niHideEditForm() {
            var editFormEl = document.getElementById('niEditForm');
            if (editFormEl) editFormEl.style.display = 'none';
        }

        async function niUpdateAndResubmit() {
            var empCount = (document.getElementById('niEmployeeCount') || {}).value || '';
            var callVol = (document.getElementById('niCallVolume') || {}).value || '';
            var resultEl = document.getElementById('niEditResult');
            var btn = document.getElementById('niEditSubmitBtn');

            // Validate
            if (empCount && (!/^\d+$/.test(empCount) || parseInt(empCount) < 1)) {
                if (resultEl) resultEl.innerHTML = '<span class="ni-result-error">Employee count must be a positive number</span>';
                return;
            }
            if (callVol && (!/^\d+$/.test(callVol) || parseInt(callVol) < 1)) {
                if (resultEl) resultEl.innerHTML = '<span class="ni-result-error">Call volume must be a positive number</span>';
                return;
            }

            if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-1"></i>Updating & resubmitting...'; }
            if (resultEl) resultEl.innerHTML = '';

            try {
                // Step 1: Update EndUser info if provided
                if (empCount || callVol) {
                    var updateR = await fetch('/voice/number-integrity/update-info', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({ employee_count: empCount, call_volume: callVol }),
                    });
                    var updateD = await updateR.json();
                    if (!updateR.ok) {
                        if (resultEl) resultEl.innerHTML = '<span class="ni-result-error"><i class="fa-solid fa-triangle-exclamation me-1"></i>' + _esc(updateD.error || 'Update failed') + '</span>';
                        if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-paper-plane me-1"></i>Update & Resubmit'; }
                        return;
                    }
                }

                // Step 2: Resubmit
                var r = await fetch('/voice/number-integrity/resubmit', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: '{}',
                });
                var d = await r.json();
                if (r.ok && d.status === 'ok') {
                    if (resultEl) resultEl.innerHTML = '<span class="ni-result-success"><i class="fa-solid fa-circle-check me-1"></i>' + _esc(d.message || 'Resubmitted!') + '</span>';
                    setTimeout(function() { niHideEditForm(); loadNumberIntegrity(); }, 1500);
                } else {
                    if (resultEl) resultEl.innerHTML = '<span class="ni-result-error"><i class="fa-solid fa-triangle-exclamation me-1"></i>' + _esc(d.error || 'Resubmit failed') + '</span>';
                }
            } catch(e) {
                console.error('[NumberIntegrity] Update+Resubmit error:', e);
                if (resultEl) resultEl.innerHTML = '<span class="ni-result-error">Network error</span>';
            }
            if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-paper-plane me-1"></i>Update & Resubmit'; }
        }

        async function searchAvailableNumbers() {
            const numberType = document.getElementById('buyNumberType').value;
            const area = document.getElementById('buyAreaCode').value.trim();
            const state = document.getElementById('buyState').value.trim();
            const city = document.getElementById('buyCity').value.trim();
            const zip = document.getElementById('buyZip').value.trim();
            const contains = document.getElementById('buyContains').value.trim();
            const list = document.getElementById('availableNumbersList');
            list.innerHTML = '<div style="text-align:center;padding:8px;"><i class="fa-solid fa-spinner fa-spin" style="color:#00ff88;"></i></div>';
            try {
                const params = new URLSearchParams();
                params.set('number_type', numberType);
                if (area) params.set('area_code', area);
                if (state) params.set('state', state);
                if (city) params.set('city', city);
                if (zip) params.set('zip_code', zip);
                if (contains) params.set('contains', contains);
                const r = await fetch('/voice/numbers/search?' + params.toString());
                const d = await r.json();
                if (!r.ok) { list.innerHTML = '<div style="color:#ef4444;padding:4px;">' + _esc(d.error || 'Failed') + '</div>'; return; }
                const nums = d.numbers || [];
                if (!nums.length) { list.innerHTML = '<div style="color:#888;padding:4px;font-size:.75rem;">No numbers found for that search. Try different filters.</div>'; return; }
                const priceMap = { local: '$0.90', toll_free: '$2.15', mobile: '$0.90' };
                const monthlyPrice = priceMap[numberType] || '$0.90';
                const isFree = _numFreeRemaining !== null && _numFreeRemaining > 0;
                list.innerHTML = nums.map(n => {
                    const loc = [n.locality, n.region].filter(Boolean).join(', ');
                    const caps = [];
                    if (n.capabilities && n.capabilities.voice) caps.push('Voice');
                    if (n.capabilities && n.capabilities.sms) caps.push('SMS');
                    if (n.capabilities && n.capabilities.mms) caps.push('MMS');
                    const priceLabel = isFree
                        ? '<span style="color:#00ff88;font-size:.75rem;font-weight:700;">FREE</span>'
                        : '<span style="color:#00ff88;font-size:.75rem;font-weight:600;">' + monthlyPrice + '/mo</span>';
                    var inCart = _numCart.find(function(ci) { return ci.phone === n.phone; });
                    var cartBtn = inCart
                        ? '<span style="color:#00ff88;font-size:.7rem;font-weight:600;"><i class="fa-solid fa-check me-1"></i>In Cart</span>'
                        : '<button onclick="numCartAdd(\'' + n.phone + '\',\'' + numberType + '\')" style="background:rgba(0,255,136,0.1);border:1px solid rgba(0,255,136,0.2);color:#00ff88;border-radius:4px;padding:3px 8px;font-size:.7rem;font-weight:600;cursor:pointer;" title="Add to cart"><i class="fa-solid fa-cart-plus"></i></button>';
                    return '<div style="display:flex;align-items:center;justify-content:space-between;padding:8px 4px;border-bottom:1px solid rgba(255,255,255,0.03);font-size:.78rem;">' +
                        '<div style="flex:1;min-width:0;">' +
                            '<span style="color:#fff;font-weight:600;">' + _esc(_fmtPhone(n.phone)) + '</span>' +
                            (loc ? '<span style="color:#666;font-size:.75rem;margin-left:6px;">' + _esc(loc) + '</span>' : '') +
                            '<div style="margin-top:2px;">' + caps.map(c => '<span style="background:rgba(0,255,136,0.08);color:#00ff88;padding:1px 6px;border-radius:4px;font-size:.75rem;margin-right:3px;">' + c + '</span>').join('') + '</div>' +
                        '</div>' +
                        '<div style="display:flex;align-items:center;gap:6px;flex-shrink:0;">' +
                            priceLabel +
                            cartBtn +
                            '<button onclick="buyNumber(\'' + n.phone + '\',\'' + numberType + '\')" style="background:linear-gradient(135deg,#00ff88,#00cc6a);border:none;color:#000;border-radius:4px;padding:3px 10px;font-size:.75rem;font-weight:700;cursor:pointer;">' + (isFree ? 'Add Free' : 'Buy') + '</button>' +
                        '</div>' +
                    '</div>';
                }).join('');
            } catch(e) { list.innerHTML = '<div style="color:#ef4444;">Network error</div>'; }
        }

        var _numFreeRemaining = null; // populated by loadNumbersTab

        async function buyNumber(phone, numberType) {
            numberType = numberType || document.getElementById('buyNumberType')?.value || 'local';
            var isFree = _numFreeRemaining !== null && _numFreeRemaining > 0;
            var msg = isFree
                ? 'Add ' + phone + ' to your account?\n\nThis number is FREE (' + _numFreeRemaining + ' free remaining).'
                : 'Purchase ' + phone + ' for $' + (numberType === 'toll_free' ? '2.15' : '0.90') + '/mo?\n\nThis will be charged to your card.';
            if (!confirm(msg)) return;

            try {
                // Try the direct buy first (works if free numbers remain)
                var r = await fetch('/voice/numbers/buy', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ phone_number: phone, number_type: numberType })
                });
                var d = await r.json();

                if (r.status === 402 && d.payment_required) {
                    // Free allowance exhausted — route through Stripe checkout
                    var cr = await fetch('/voice/numbers/checkout', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({ phone_number: phone, number_type: numberType })
                    });
                    var cd = await cr.json();
                    if (!cr.ok) { alert(cd.error || 'Checkout failed'); return; }
                    // Redirect to Stripe
                    window.location.href = cd.checkout_url;
                    return;
                }

                if (!r.ok) { alert(d.error || 'Failed'); return; }
                alert('Number added: ' + d.phone);
                var buyPanel = document.getElementById('vstabBuyPanel'); if (buyPanel) buyPanel.style.display = 'none';
                loadNumbersTab();
                loadNumberHealth();
            } catch(e) { alert('Network error'); }
        }
        async function releaseNumber(sid, phone) {
            if (!confirm('Release ' + phone + '?\n\nThis cannot be undone. The number will be removed from your account.')) return;
            try {
                const r = await fetch('/voice/numbers/release', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ sid: sid }) });
                const d = await r.json();
                if (!d.error) { loadNumbersTab(); loadNumberHealth(); }
                else alert(d.error);
            } catch(e) { alert('Network error'); }
        }

        // ── Init dialer (runs on page load since Dialer is default tab) ──
        function initDialerTab() {
            dialerLoadPipelines();
            if (!dialerContacts.length) dialerFetchContacts();
            dialerLoadAllCallHistory();
            // Auto-init VoIP if credential exists (one-time connect)
            if (voipSetupDone && !voipReady && !_voipInitializing) initVoIPDevice();
            // Always refresh audio device list
            refreshAudioDevices();
        }
        document.querySelector('[data-bs-target="#voicedialer"]')?.addEventListener('shown.bs.tab', initDialerTab);
        // Also init on page load since Dialer is the default active tab
        document.addEventListener('DOMContentLoaded', initDialerTab);

        // Auto-load Numbers/Trust Hub when Voice tab is shown
        document.querySelector('[data-bs-target="#voice"]')?.addEventListener('shown.bs.tab', function() {
            // Pre-load data so Numbers/Trust Hub tabs render instantly
            loadNumbersTab();
        });

        // ===== NUMBER HEALTH & SMART ROTATION =====
        var _nhData = null;

        async function loadNumberHealth() {
            const el = document.getElementById('numberHealthContent');
            if (!el) return;
            // Show loading spinner
            el.innerHTML = '<div style="text-align:center;padding:20px;color:#555;"><i class="fa-solid fa-spinner fa-spin" style="font-size:1rem;color:#ff6b9d;display:block;margin-bottom:6px;"></i><span style="font-size:.75rem;">Loading number health...</span></div>';
            try {
                const r = await fetch('/voice/number-health');
                if (!r.ok) {
                    el.innerHTML = '<div style="text-align:center;padding:16px;color:#ef4444;font-size:.75rem;"><i class="fa-solid fa-triangle-exclamation me-1"></i>Failed to load number health</div>';
                    return;
                }
                const d = await r.json();
                _nhData = d;
                _renderNumberHealth(el, d);
            } catch(e) {
                console.error('[NumberHealth] Load error:', e);
                el.innerHTML = '<div style="text-align:center;padding:16px;color:#ef4444;font-size:.75rem;"><i class="fa-solid fa-triangle-exclamation me-1"></i>Network error loading health data</div>';
            }
        }

        function _renderNumberHealth(el, d) {
            const nums = d.numbers || [];
            const sum = d.summary || {};
            const rotationEnabled = d.rotation_enabled;
            const strategy = d.rotation_strategy || 'weighted_health';

            let html = '';

            // ── Header with toggle ──
            html += '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;">';
            html += '<h6 style="margin:0;font-weight:700;color:#fff;font-size:.88rem;"><i class="fa-solid fa-heart-pulse me-2" style="color:#ff6b9d;"></i>Number Health & Smart Rotation</h6>';
            html += '<label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:.75rem;color:#aaa;">';
            html += '<span>' + (rotationEnabled ? 'Enabled' : 'Disabled') + '</span>';
            html += '<div onclick="nhToggleRotation()" style="width:38px;height:20px;border-radius:10px;background:' + (rotationEnabled ? 'rgba(0,255,136,0.4)' : 'rgba(255,255,255,0.08)') + ';position:relative;cursor:pointer;transition:background .2s;">';
            html += '<div style="width:16px;height:16px;border-radius:50%;background:' + (rotationEnabled ? '#00ff88' : '#555') + ';position:absolute;top:2px;' + (rotationEnabled ? 'right:2px' : 'left:2px') + ';transition:all .2s;"></div>';
            html += '</div></label></div>';

            if (!rotationEnabled) {
                html += '<div style="padding:16px;background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.06);border-radius:10px;text-align:center;color:#666;font-size:.78rem;">';
                html += '<i class="fa-solid fa-shuffle" style="font-size:1.2rem;display:block;margin-bottom:6px;color:#444;"></i>';
                html += 'Enable Smart Rotation to automatically distribute calls across your numbers,<br>prevent burnout, and maximize connection rates.';
                html += '</div>';
                el.innerHTML = html;
                return;
            }

            // ── Strategy explainer + selector ──
            var strategies = [
                {
                    key: 'weighted_health',
                    label: 'Weighted Health',
                    short: 'Smart distribution — healthier numbers get more calls. Recommended for most accounts.',
                },
                {
                    key: 'round_robin',
                    label: 'Round Robin',
                    short: 'Even rotation across every active number. Prevents any single number from being overused and flagged by carriers on high-volume days.',
                },
                {
                    key: 'highest_health',
                    label: 'Top Health Only',
                    short: 'Always dials from the single healthiest number. Use during critical outreach when you need the best-performing caller ID.',
                },
            ];
            var activeStrategy = strategies.find(function(s) { return s.key === strategy; }) || strategies[0];
            html += '<div class="nh-strategy-explainer"><em>' + _esc(activeStrategy.label) + ':</em> ' + _esc(activeStrategy.short) + '</div>';
            html += '<div class="nh-strategy-row">';
            strategies.forEach(function(s) {
                var active = strategy === s.key;
                html += '<button class="nh-strategy-btn' + (active ? ' nh-strategy-active' : '') + '" onclick="nhSetStrategy(\'' + s.key + '\')">' + _esc(s.label) + '</button>';
            });
            html += '</div>';

            // ── Summary KPIs ──
            if (sum.total_numbers) {
                var avgHealthColor = sum.avg_health >= 80 ? '#00ff88' : (sum.avg_health >= 60 ? '#4ade80' : (sum.avg_health >= 40 ? '#ffa500' : '#ef4444'));
                html += '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:14px;">';

                html += _nhKpiCard('Avg Health', Math.round(sum.avg_health), avgHealthColor, 'fa-heart-pulse');
                html += _nhKpiCard('Active', sum.active_count + '/' + sum.total_numbers, '#4ade80', 'fa-circle-check');
                html += _nhKpiCard('Today', sum.daily_calls + ' calls', '#00ff88', 'fa-phone');
                var dailyCR = sum.daily_connect_rate || 0;
                var dailyCRColor = sum.daily_calls < 50 ? '#888' : (dailyCR >= 30 ? '#00ff88' : '#ffa500');
                html += _nhKpiCard('Connect', sum.daily_calls < 50 ? '—' : dailyCR + '%', dailyCRColor, 'fa-link');

                html += '</div>';
            }

            // ── Per-number health cards ──
            if (nums.length) {
                html += '<div style="display:flex;flex-direction:column;gap:6px;">';
                nums.forEach(function(n) {
                    html += _nhNumberCard(n);
                });
                html += '</div>';
            }

            // ── States Licensed In ──
            html += '<div class="nh-states-section">';
            html += '<div class="nh-states-header">';
            html += '<h6 class="nh-states-title">States Licensed In</h6>';
            html += '<button onclick="nhOpenStatePicker()" class="nh-states-edit-btn"><i class="fa-solid fa-pen me-1"></i>Edit States</button>';
            html += '</div>';

            var licensedStates = d.licensed_states || [];
            var licensedCoverage = d.licensed_coverage || [];

            if (!licensedStates.length) {
                html += '<div style="padding:20px;background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.06);border-radius:10px;text-align:center;">';
                html += '<i class="fa-solid fa-map-location-dot" style="font-size:1.4rem;display:block;margin-bottom:8px;color:#444;"></i>';
                html += '<div style="color:#888;font-size:.78rem;margin-bottom:10px;">Select the states you\'re licensed to sell in.<br>We\'ll show your number coverage for each state.</div>';
                html += '<button onclick="nhOpenStatePicker()" style="background:linear-gradient(135deg,#a78bfa,#7c3aed);border:none;color:#fff;border-radius:8px;padding:8px 20px;font-size:.82rem;font-weight:700;cursor:pointer;"><i class="fa-solid fa-plus me-1"></i>Select States</button>';
                html += '</div>';
            } else {
                // Coverage table for licensed states
                html += '<div style="display:flex;flex-direction:column;gap:4px;">';
                licensedCoverage.forEach(function(s) {
                    var covered = s.owned >= 2;
                    var partial = s.owned === 1;
                    var bgColor = covered ? 'rgba(0,255,136,0.03)' : (partial ? 'rgba(255,165,0,0.04)' : 'rgba(239,68,68,0.04)');
                    var borderColor = covered ? 'rgba(0,255,136,0.1)' : (partial ? 'rgba(255,165,0,0.1)' : 'rgba(239,68,68,0.1)');
                    var statusColor = covered ? '#00ff88' : (partial ? '#ffa500' : '#ef4444');
                    var statusIcon = covered ? 'fa-circle-check' : (partial ? 'fa-circle-half-stroke' : 'fa-circle-xmark');
                    var statusText = covered ? s.owned + ' numbers' : (partial ? '1 number' : 'No numbers');

                    html += '<div style="display:flex;align-items:center;justify-content:space-between;padding:8px 10px;background:' + bgColor + ';border:1px solid ' + borderColor + ';border-radius:6px;">';
                    html += '<div style="display:flex;align-items:center;gap:8px;">';
                    html += '<i class="fa-solid ' + statusIcon + '" style="color:' + statusColor + ';font-size:.75rem;"></i>';
                    html += '<span style="color:#ccc;font-size:.78rem;font-weight:600;">' + _esc(s.state_name) + '</span>';
                    html += '<span style="color:' + statusColor + ';font-size:.75rem;">' + statusText + '</span>';
                    html += '</div>';

                    if (!covered) {
                        html += '<button onclick="nhBuyForState(\'' + _esc(s.state) + '\')" style="background:linear-gradient(135deg,#00ff88,#00cc6a);border:none;color:#000;border-radius:5px;padding:3px 10px;font-size:.75rem;font-weight:700;cursor:pointer;white-space:nowrap;">';
                        html += '<i class="fa-solid fa-plus me-1"></i>Buy Number';
                        html += '</button>';
                    }

                    html += '</div>';
                });
                html += '</div>';
            }

            // State picker modal (hidden by default)
            html += '<div id="nhStatePickerModal" style="display:none;"></div>';

            html += '</div>';

            el.innerHTML = html;
        }

        function _nhKpiCard(label, value, color, icon) {
            return '<div style="padding:10px;background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.06);border-radius:8px;text-align:center;">' +
                '<div style="font-size:1.1rem;font-weight:800;color:' + color + ';">' +
                '<i class="fa-solid ' + icon + '" style="font-size:.75rem;opacity:.6;margin-right:3px;"></i>' + value +
                '</div>' +
                '<div style="font-size:.75rem;color:#666;text-transform:uppercase;letter-spacing:.5px;margin-top:2px;">' + label + '</div>' +
                '</div>';
        }

        function _nhNumberCard(n) {
            var score = n.health_score || 0;
            var barColor, statusColor, statusBg, statusIcon;

            if (n.status === 'frozen') {
                barColor = '#8b5cf6';
                statusColor = '#8b5cf6';
                statusBg = 'rgba(139,92,246,0.1)';
                statusIcon = 'fa-snowflake';
            } else if (n.status === 'resting') {
                barColor = '#fbbf24';
                statusColor = '#fbbf24';
                statusBg = 'rgba(251,191,36,0.1)';
                statusIcon = 'fa-moon';
            } else if (score >= 80) {
                barColor = '#00ff88';
                statusColor = '#00ff88';
                statusBg = 'rgba(0,255,136,0.06)';
                statusIcon = 'fa-heart';
            } else if (score >= 60) {
                barColor = '#4ade80';
                statusColor = '#4ade80';
                statusBg = 'rgba(74,222,128,0.06)';
                statusIcon = 'fa-heart';
            } else if (score >= 40) {
                barColor = '#ffa500';
                statusColor = '#ffa500';
                statusBg = 'rgba(255,165,0,0.06)';
                statusIcon = 'fa-heart-crack';
            } else {
                barColor = '#ef4444';
                statusColor = '#ef4444';
                statusBg = 'rgba(239,68,68,0.06)';
                statusIcon = 'fa-heart-crack';
            }

            var dailyPct = n.daily_cap > 0 ? Math.min(100, Math.round(n.daily_calls / n.daily_cap * 100)) : 0;
            var connectRate = n.connect_rate || 0;
            var totalCalls = n.total_calls || 0;

            var html = '<div style="padding:10px 12px;background:' + statusBg + ';border:1px solid rgba(255,255,255,0.06);border-radius:8px;">';

            // Row 1: Phone + badges + health score
            html += '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;gap:8px;">';
            html += '<div style="display:flex;align-items:center;gap:6px;flex:1;min-width:0;overflow:hidden;flex-wrap:wrap;">';
            html += '<div style="display:flex;flex-direction:column;gap:2px;min-width:0;">';
            html += '<span style="color:#fff;font-weight:600;font-size:.8rem;">' + _esc(_fmtPhone(n.phone)) + '</span>';
            if (n.is_primary) html += '<span style="background:rgba(0,255,136,0.15);color:#00ff88;padding:1px 5px;border-radius:3px;font-size:.65rem;font-weight:700;width:fit-content;">PRIMARY</span>';
            html += '</div>';
            if (n.state) html += '<span style="background:rgba(0,255,136,0.08);color:#00ff88;padding:1px 5px;border-radius:3px;font-size:.75rem;font-weight:700;letter-spacing:.3px;">' + _esc(n.state) + '</span>';
            if (n.nickname) html += '<span style="color:#666;font-size:.75rem;">(' + _esc(n.nickname) + ')</span>';
            if (_nhData && _nhData.spam_protected) html += '<span title="A2P Registered — STIR/SHAKEN verified" class="nh-protected-tag">Protected</span>';
            html += '</div>';

            // Health score badge
            html += '<div style="display:flex;align-items:center;gap:6px;flex-shrink:0;">';
            if (n.status === 'frozen' || n.status === 'resting') {
                html += '<span style="background:' + statusBg + ';color:' + statusColor + ';padding:2px 8px;border-radius:4px;font-size:.75rem;font-weight:700;text-transform:uppercase;">';
                html += '<i class="fa-solid ' + statusIcon + ' me-1"></i>' + n.status;
                html += '</span>';
            }
            html += '<div style="width:36px;height:36px;border-radius:50%;background:rgba(0,0,0,0.3);border:2px solid ' + barColor + ';display:flex;align-items:center;justify-content:center;">';
            html += '<span style="font-size:.75rem;font-weight:800;color:' + barColor + ';">' + Math.round(score) + '</span>';
            html += '</div></div></div>';

            // Row 2: Metric pills
            html += '<div style="display:flex;gap:4px;flex-wrap:wrap;margin-bottom:6px;">';
            html += _nhPill('fa-phone', n.daily_calls + '/' + n.daily_cap + ' today', dailyPct >= 80 ? '#ffa500' : '#888');
            // Connect rate: show neutral gray when insufficient data (under 100 calls),
            // only color-code when there's enough signal to be meaningful
            var crColor = totalCalls < 100 ? '#888' : (connectRate >= 30 ? '#4ade80' : (connectRate >= 15 ? '#ffa500' : '#ef4444'));
            var crLabel = totalCalls < 100 ? (totalCalls === 0 ? 'No data' : connectRate.toFixed(0) + '%') : connectRate.toFixed(0) + '% connect';
            html += _nhPill('fa-link', crLabel, crColor);
            var blocked = n.total_carrier_blocked || 0;
            if (blocked > 0) html += _nhPill('fa-shield-halved', blocked + ' blocked', blocked >= 10 ? '#ef4444' : '#ffa500');
            html += _nhPill('fa-chart-line', totalCalls + ' lifetime', '#888');
            var warmupColors = { 0: '#888', 1: '#ffa500', 2: '#fbbf24', 3: '#00ff88', 4: '#4ade80' };
            html += _nhPill('fa-seedling', n.warmup_label || 'Stage ' + n.warmup_stage, warmupColors[n.warmup_stage] || '#00ff88');
            html += '</div>';

            // Row 3: Health bar
            html += '<div style="height:3px;background:rgba(255,255,255,0.06);border-radius:2px;overflow:hidden;">';
            html += '<div style="height:100%;width:' + score + '%;background:' + barColor + ';border-radius:2px;transition:width .5s ease;"></div>';
            html += '</div>';

            // Row 4: Actions
            if (n.status === 'frozen' || n.status === 'resting') {
                html += '<div style="margin-top:6px;text-align:right;">';
                html += '<button onclick="nhSetNumberStatus(\'' + _esc(n.phone) + '\',\'active\')" style="background:rgba(0,255,136,0.08);border:1px solid rgba(0,255,136,0.15);color:#00ff88;border-radius:4px;padding:3px 10px;font-size:.75rem;font-weight:600;cursor:pointer;"><i class="fa-solid fa-play me-1"></i>Reactivate</button>';
                html += '</div>';
            } else if (score < 40 && totalCalls >= 5) {
                html += '<div style="margin-top:6px;display:flex;justify-content:flex-end;gap:6px;">';
                html += '<button onclick="nhSetNumberStatus(\'' + _esc(n.phone) + '\',\'resting\',24)" style="background:rgba(251,191,36,0.08);border:1px solid rgba(251,191,36,0.15);color:#fbbf24;border-radius:4px;padding:3px 10px;font-size:.75rem;font-weight:600;cursor:pointer;"><i class="fa-solid fa-moon me-1"></i>Rest 24h</button>';
                html += '<button onclick="nhSetNumberStatus(\'' + _esc(n.phone) + '\',\'frozen\')" style="background:rgba(139,92,246,0.08);border:1px solid rgba(139,92,246,0.15);color:#8b5cf6;border-radius:4px;padding:3px 10px;font-size:.75rem;font-weight:600;cursor:pointer;"><i class="fa-solid fa-snowflake me-1"></i>Freeze</button>';
                html += '</div>';
            }

            html += '</div>';
            return html;
        }

        function _nhPill(icon, text, color) {
            return '<span style="display:inline-flex;align-items:center;gap:3px;background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.05);border-radius:4px;padding:2px 6px;font-size:.75rem;color:' + (color || '#888') + ';">' +
                '<i class="fa-solid ' + icon + '" style="font-size:.75rem;opacity:.7;"></i>' + text +
                '</span>';
        }

        async function nhToggleRotation() {
            var enabled = !(_nhData && _nhData.rotation_enabled);
            try {
                var r = await fetch('/voice/number-health/toggle', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ enabled: enabled, strategy: (_nhData && _nhData.rotation_strategy) || 'weighted_health' }),
                });
                if (r.ok) {
                    if (typeof _showDashToast === 'function') _showDashToast(true, 'Smart Rotation ' + (enabled ? 'enabled' : 'disabled'));
                    loadNumberHealth();
                } else {
                    if (typeof _showDashToast === 'function') _showDashToast(false, 'Failed to toggle rotation');
                }
            } catch(e) {
                console.error('[NumberHealth] Toggle error:', e);
                if (typeof _showDashToast === 'function') _showDashToast(false, 'Network error');
            }
        }

        async function nhSetStrategy(strategy) {
            try {
                var r = await fetch('/voice/number-health/toggle', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ enabled: true, strategy: strategy }),
                });
                if (r.ok) {
                    if (typeof _showDashToast === 'function') _showDashToast(true, 'Strategy: ' + strategy.replace(/_/g, ' '));
                    loadNumberHealth();
                } else {
                    if (typeof _showDashToast === 'function') _showDashToast(false, 'Failed to set strategy');
                }
            } catch(e) {
                console.error('[NumberHealth] Strategy error:', e);
                if (typeof _showDashToast === 'function') _showDashToast(false, 'Network error');
            }
        }

        async function nhSetNumberStatus(phone, status, restHours) {
            // Confirmation for potentially impactful actions
            if (status === 'resting' && !confirm('Rest ' + _fmtPhone(phone) + ' for ' + (restHours || 24) + ' hours?\nThis number will be removed from the active rotation pool.')) return;
            if (status === 'frozen' && !confirm('Freeze ' + _fmtPhone(phone) + '?\nThis number will be quarantined until manually reactivated.')) return;
            try {
                var r = await fetch('/voice/number-health/set-status', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ phone: phone, status: status, rest_hours: restHours || null }),
                });
                if (r.ok) {
                    if (typeof _showDashToast === 'function') _showDashToast(true, _fmtPhone(phone) + ' → ' + status);
                    loadNumberHealth();
                } else {
                    if (typeof _showDashToast === 'function') _showDashToast(false, 'Failed to update status');
                }
            } catch(e) {
                console.error('[NumberHealth] Set status error:', e);
                if (typeof _showDashToast === 'function') _showDashToast(false, 'Network error');
            }
        }

        // ── Licensed States: state picker + buy-for-state ──
        var _allStates = [
            ["AL","Alabama"],["AK","Alaska"],["AZ","Arizona"],["AR","Arkansas"],
            ["CA","California"],["CO","Colorado"],["CT","Connecticut"],["DE","Delaware"],
            ["FL","Florida"],["GA","Georgia"],["HI","Hawaii"],["ID","Idaho"],
            ["IL","Illinois"],["IN","Indiana"],["IA","Iowa"],["KS","Kansas"],
            ["KY","Kentucky"],["LA","Louisiana"],["ME","Maine"],["MD","Maryland"],
            ["MA","Massachusetts"],["MI","Michigan"],["MN","Minnesota"],["MS","Mississippi"],
            ["MO","Missouri"],["MT","Montana"],["NE","Nebraska"],["NV","Nevada"],
            ["NH","New Hampshire"],["NJ","New Jersey"],["NM","New Mexico"],["NY","New York"],
            ["NC","North Carolina"],["ND","North Dakota"],["OH","Ohio"],["OK","Oklahoma"],
            ["OR","Oregon"],["PA","Pennsylvania"],["RI","Rhode Island"],["SC","South Carolina"],
            ["SD","South Dakota"],["TN","Tennessee"],["TX","Texas"],["UT","Utah"],
            ["VT","Vermont"],["VA","Virginia"],["WA","Washington"],["WV","West Virginia"],
            ["WI","Wisconsin"],["WY","Wyoming"]
        ];

        function nhOpenStatePicker() {
            var modal = document.getElementById('nhStatePickerModal');
            if (!modal) return;
            var current = (_nhData && _nhData.licensed_states) || [];
            var html = '';
            html += '<div style="position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.6);z-index:9998;display:flex;align-items:center;justify-content:center;" onclick="if(event.target===this)nhCloseStatePicker()">';
            html += '<div style="background:#1a1a2e;border:1px solid rgba(255,255,255,0.1);border-radius:16px;padding:24px;width:520px;max-height:80vh;overflow-y:auto;" onclick="event.stopPropagation()">';
            html += '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;">';
            html += '<h6 style="margin:0;font-weight:700;color:#fff;"><i class="fa-solid fa-id-card me-2" style="color:#a78bfa;"></i>States Licensed In</h6>';
            html += '<button onclick="nhCloseStatePicker()" style="background:none;border:none;color:#666;cursor:pointer;font-size:1.1rem;"><i class="fa-solid fa-xmark"></i></button>';
            html += '</div>';
            html += '<p style="color:#888;font-size:.78rem;margin-bottom:14px;">Select every state where you hold an active insurance license. We\'ll track your number coverage for each.</p>';

            // Select all / clear
            html += '<div style="display:flex;gap:8px;margin-bottom:12px;">';
            html += '<button onclick="document.querySelectorAll(\'#nhStateGrid input\').forEach(function(c){c.checked=true})" style="background:rgba(0,255,136,0.06);border:1px solid rgba(0,255,136,0.12);color:#00ff88;border-radius:5px;padding:3px 10px;font-size:.75rem;cursor:pointer;">Select All</button>';
            html += '<button onclick="document.querySelectorAll(\'#nhStateGrid input\').forEach(function(c){c.checked=false})" style="background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.06);color:#888;border-radius:5px;padding:3px 10px;font-size:.75rem;cursor:pointer;">Clear All</button>';
            html += '</div>';

            // State grid
            html += '<div id="nhStateGrid" style="display:grid;grid-template-columns:repeat(3,1fr);gap:4px;margin-bottom:18px;">';
            _allStates.forEach(function(s) {
                var code = s[0], name = s[1];
                var checked = current.indexOf(code) >= 0 ? ' checked' : '';
                html += '<label style="display:flex;align-items:center;gap:6px;padding:5px 8px;border-radius:6px;cursor:pointer;font-size:.75rem;color:#ccc;background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.04);transition:background .15s;" onmouseover="this.style.background=\'rgba(167,139,250,0.06)\'" onmouseout="this.style.background=\'rgba(255,255,255,0.02)\'">';
                html += '<input type="checkbox" value="' + code + '"' + checked + ' style="accent-color:#a78bfa;width:14px;height:14px;cursor:pointer;">';
                html += '<span><strong>' + code + '</strong> ' + name + '</span>';
                html += '</label>';
            });
            html += '</div>';

            // Save button
            html += '<button onclick="nhSaveLicensedStates()" id="nhSaveStatesBtn" style="background:linear-gradient(135deg,#a78bfa,#7c3aed);border:none;color:#fff;border-radius:10px;padding:10px 0;font-size:.88rem;font-weight:700;cursor:pointer;width:100%;"><i class="fa-solid fa-check me-2"></i>Save Licensed States</button>';
            html += '</div></div>';

            modal.innerHTML = html;
            modal.style.display = 'block';
        }

        function nhCloseStatePicker() {
            var modal = document.getElementById('nhStatePickerModal');
            if (modal) { modal.innerHTML = ''; modal.style.display = 'none'; }
        }

        async function nhSaveLicensedStates() {
            var checkboxes = document.querySelectorAll('#nhStateGrid input:checked');
            var states = [];
            checkboxes.forEach(function(c) { states.push(c.value); });
            var btn = document.getElementById('nhSaveStatesBtn');
            if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-2"></i>Saving...'; }
            try {
                var r = await fetch('/voice/licensed-states', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ states: states }),
                });
                if (r.ok) {
                    nhCloseStatePicker();
                    if (typeof _showDashToast === 'function') _showDashToast(true, states.length + ' licensed state' + (states.length !== 1 ? 's' : '') + ' saved');
                    loadNumberHealth();
                } else {
                    if (typeof _showDashToast === 'function') _showDashToast(false, 'Failed to save');
                    if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-check me-2"></i>Save Licensed States'; }
                }
            } catch(e) {
                console.error('[LicensedStates] Save error:', e);
                if (typeof _showDashToast === 'function') _showDashToast(false, 'Network error');
                if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-check me-2"></i>Save Licensed States'; }
            }
        }

        function nhBuyForState(stateCode) {
            // Open the buy panel with state pre-filled
            var buyPanel = document.getElementById('vstabBuyPanel');
            if (buyPanel) buyPanel.style.display = 'block';
            var stateInput = document.getElementById('buyState');
            if (stateInput) stateInput.value = stateCode;
            // Clear other fields so the search is state-only
            var areaInput = document.getElementById('buyAreaCode');
            if (areaInput) areaInput.value = '';
            var cityInput = document.getElementById('buyCity');
            if (cityInput) cityInput.value = '';
            var zipInput = document.getElementById('buyZip');
            if (zipInput) zipInput.value = '';
            var containsInput = document.getElementById('buyContains');
            if (containsInput) containsInput.value = '';
            // Auto-trigger search
            searchAvailableNumbers();
            // Scroll buy panel into view
            buyPanel.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }

        // Number health now has its own panel — no longer auto-loads with numbers tab


        // ===== A2P 10DLC — Brand & Campaign Panels =====

        // ── Constants ──
        var _A2P_SAMPLE_MESSAGES = [
            'Hi {First Name}, this is {Agent Name} with {Business Name}. I wanted to follow up on your interest in life insurance coverage. Do you have a few minutes to chat about your options?',
            'Hey {First Name}, just checking in \u2014 I put together some coverage options based on what we discussed. When works best for a quick call to go over them?',
            'Hi {First Name}, this is {Agent Name}. I noticed you were looking into life insurance options. I\'d love to help you find the right coverage for your family. Is now a good time?',
            'Hi {First Name}, friendly reminder about our appointment tomorrow at {Time}. Looking forward to helping you find the right coverage. Reply YES to confirm or let me know if you need to reschedule.',
        ];
        var _A2P_DESCRIPTION_EXAMPLE = 'We send personalized text messages to leads who have expressed interest in life insurance coverage. Messages include policy information, appointment reminders, and follow-up communications.';
        var _A2P_MESSAGE_FLOW_EXAMPLE = 'Leads opt in by submitting a contact form on our website which includes SMS consent language. They can opt out at any time by replying STOP.';

        var _a2pFees = {
            SOLE_PROPRIETOR: { brand_cents: 450, label: 'Sole Proprietor', desc: 'No EIN required, 1 number limit, 1 campaign limit' },
            LOW_VOLUME:      { brand_cents: 450, label: 'Low Volume Standard', desc: 'EIN required, up to 2,000 msgs/day, lowest cost' },
            STANDARD:        { brand_cents: 4600, label: 'Standard', desc: 'EIN required, 6,000+ msgs/day, includes secondary vetting' },
        };

        // ── State ──
        var _a2pData = null;
        var _a2pBrandPollTimer = null;
        var _a2pCampaignPollTimer = null;
        var _a2pNumbersCache = [];

        // ── Legacy compat ──
        function a2pLoadStatus() { a2pBrandInit(); }

        // ── Brand Panel ──

        async function a2pBrandInit() {
            try {
                var results = await Promise.all([fetch('/voice/a2p/status'), fetch('/voice/spam-protection/status')]);
                var status = await results[0].json();
                var sp = results[1].ok ? await results[1].json() : {};
                _a2pData = { status: status, trustHub: sp };
                _a2pRenderBrandPanel(status, sp);
            } catch(e) { console.error('[A2P] Brand init error:', e); }
        }

        function _a2pRenderBrandPanel(d, sp) {
            var el = document.getElementById('a2pBrandContent');
            if (!el) return;
            _a2pUpdateBadge('a2pBrandBadge', d.brand_status);
            if (d.brand_sid && d.brand_status) {
                el.innerHTML = _a2pBrandStatusCard(d);
                if (d.brand_status === 'PENDING' || d.brand_status === 'IN_PROGRESS') _a2pStartBrandPoll();
                return;
            }
            el.innerHTML = '<div class="a2p-create-section">' +
                '<p class="a2p-create-desc">Register your business with carriers to send SMS from your phone numbers. Brand vetting typically takes 1-7 business days.</p>' +
                '<button class="a2p-create-btn" onclick="a2pShowBrandForm()"><i class="fa-solid fa-plus"></i> Create New Brand</button></div>';
        }




        function a2pShowBrandForm() {
            var el = document.getElementById('a2pBrandContent');
            if (!el) return;
            var sp = (_a2pData && _a2pData.trustHub) || {};
            var th = sp.trust_hub || sp || {};
            var fullName = th.contact_name || '';
            var nameParts = fullName.trim().split(/\s+/);
            var firstName = nameParts[0] || '';
            var lastName = nameParts.length > 1 ? nameParts.slice(1).join(' ') : '';

            var brandTypes = [
                { key: 'SOLE_PROPRIETOR', name: 'Sole Proprietor', price: '$4.50' },
                { key: 'LOW_VOLUME', name: 'Low Volume Standard', price: '$4.50' },
                { key: 'STANDARD', name: 'Standard', price: '$46.00' },
            ];

            var html = '<div class="a2p-form">';
            html += '<div class="a2p-section-title"><i class="fa-solid fa-shapes"></i> Brand Type</div>';
            brandTypes.forEach(function(bt) {
                var info = _a2pFees[bt.key];
                html += '<div class="a2p-brand-type-card' + (bt.key === 'LOW_VOLUME' ? ' selected' : '') + '" onclick="a2pSelectBrandType(this, \'' + bt.key + '\')">' +
                    '<input type="radio" name="a2pBrandType" value="' + bt.key + '"' + (bt.key === 'LOW_VOLUME' ? ' checked' : '') + ' class="d-none">' +
                    '<div style="flex:1;"><div class="a2p-brand-type-name">' + _esc(bt.name) + '</div>' +
                    '<div class="a2p-brand-type-desc">' + _esc(info.desc) + '</div></div>' +
                    '<div class="a2p-brand-type-price">' + bt.price + '</div></div>';
            });

            html += '<div class="a2p-section-title"><i class="fa-solid fa-building"></i> Business Details</div>';
            html += '<div class="a2p-field-row"><div class="a2p-field-group" style="flex:2;"><label class="a2p-field-label">Business Name (must match IRS/EIN docs)</label>' +
                '<input type="text" id="a2pBizName" class="a2p-input" placeholder="ACME Insurance LLC" value="' + _esc(th.business_name || '') + '"></div>' +
                '<div class="a2p-field-group" style="flex:1;"><label class="a2p-field-label" id="a2pEINLabel">EIN (Tax ID)</label>' +
                '<input type="text" id="a2pEIN" class="a2p-input" placeholder="XX-XXXXXXX" value="' + _esc(th.ein || '') + '"></div></div>';
            html += '<div class="a2p-field-group"><label class="a2p-field-label">Business Type</label>' +
                '<select id="a2pBusinessType" class="a2p-select">' +
                '<option value="LLC"' + (th.business_type === 'LLC' ? ' selected' : '') + '>LLC</option>' +
                '<option value="Corporation"' + (th.business_type === 'Corporation' ? ' selected' : '') + '>Corporation</option>' +
                '<option value="Sole Proprietor"' + (th.business_type === 'Sole Proprietor' ? ' selected' : '') + '>Sole Proprietor</option>' +
                '<option value="Partnership"' + (th.business_type === 'Partnership' ? ' selected' : '') + '>Partnership</option>' +
                '<option value="Non-Profit"' + (th.business_type === 'Non-Profit' ? ' selected' : '') + '>Non-Profit</option></select></div>';

            html += '<div class="a2p-section-title"><i class="fa-solid fa-location-dot"></i> Business Address</div>';
            html += '<div class="a2p-field-group"><input type="text" id="a2pStreet" class="a2p-input" placeholder="123 Main Street, Suite 100" value="' + _esc(th.street || '') + '"></div>';
            html += '<div class="a2p-field-row">' +
                '<div class="a2p-field-group" style="flex:2;"><input type="text" id="a2pCity" class="a2p-input" placeholder="City" value="' + _esc(th.city || '') + '"></div>' +
                '<div class="a2p-field-group" style="flex:1;"><input type="text" id="a2pState" class="a2p-input" placeholder="State" maxlength="2" value="' + _esc(th.state || '') + '"></div>' +
                '<div class="a2p-field-group" style="flex:1;"><input type="text" id="a2pZip" class="a2p-input" placeholder="ZIP" value="' + _esc(th.zip || '') + '"></div></div>';

            html += '<div class="a2p-section-title"><i class="fa-solid fa-user"></i> Contact Info</div>';
            html += '<div class="a2p-field-row">' +
                '<div class="a2p-field-group"><input type="email" id="a2pContactEmail" class="a2p-input" placeholder="Email" value="' + _esc(th.contact_email || '') + '"></div>' +
                '<div class="a2p-field-group"><input type="tel" id="a2pContactPhone" class="a2p-input" placeholder="Phone (+1XXXXXXXXXX)" value="' + _esc(th.contact_phone || '') + '"></div></div>';
            html += '<div class="a2p-field-group"><label class="a2p-field-label">Website (optional, improves vetting score)</label>' +
                '<input type="url" id="a2pWebsite" class="a2p-input" placeholder="https://youragency.com" value="' + _esc(th.website || '') + '"></div>';

            html += '<div class="a2p-section-title"><i class="fa-solid fa-id-card"></i> Authorized Representative</div>';
            html += '<div class="a2p-field-row">' +
                '<div class="a2p-field-group"><input type="text" id="a2pFirstName" class="a2p-input" placeholder="First Name" value="' + _esc(firstName) + '"></div>' +
                '<div class="a2p-field-group"><input type="text" id="a2pLastName" class="a2p-input" placeholder="Last Name" value="' + _esc(lastName) + '"></div></div>';
            html += '<div class="a2p-field-row">' +
                '<div class="a2p-field-group"><input type="text" id="a2pJobTitle" class="a2p-input" placeholder="Job Title (e.g. Owner, Agent)" value="' + _esc(th.contact_title || '') + '"></div>' +
                '<div class="a2p-field-group"><select id="a2pJobPosition" class="a2p-select">' +
                '<option value="CEO">Owner / CEO</option><option value="Director">Director</option>' +
                '<option value="GM">General Manager</option><option value="VP">VP</option>' +
                '<option value="CFO">CFO</option><option value="General Counsel">General Counsel</option>' +
                '<option value="Other">Other</option></select></div></div>';

            html += '<div id="a2pBrandResult" class="a2p-result"></div>';
            html += '<button class="a2p-submit-btn" id="a2pSubmitBrandBtn" onclick="a2pSubmitBrand()"><i class="fa-solid fa-credit-card"></i> Register Brand &amp; Pay</button>';
            html += '</div>';
            el.innerHTML = html;
        }

        function a2pSelectBrandType(cardEl, key) {
            document.querySelectorAll('.a2p-brand-type-card').forEach(function(c) { c.classList.remove('selected'); });
            cardEl.classList.add('selected');
            cardEl.querySelector('input[type="radio"]').checked = true;
            var einLabel = document.getElementById('a2pEINLabel');
            var einInput = document.getElementById('a2pEIN');
            if (einLabel && einInput) {
                if (key === 'SOLE_PROPRIETOR') { einLabel.textContent = 'SSN (last 4 digits)'; einInput.placeholder = 'XXXX'; einInput.maxLength = 4; }
                else { einLabel.textContent = 'EIN (Tax ID)'; einInput.placeholder = 'XX-XXXXXXX'; einInput.maxLength = 10; }
            }
        }
        window.a2pSelectBrandType = a2pSelectBrandType;

        // ── Submit Brand: validate → save form → Stripe checkout → submit on return ──
        async function a2pSubmitBrand() {
            var result = document.getElementById('a2pBrandResult');
            var btn = document.getElementById('a2pSubmitBrandBtn');
            var brandType = (document.getElementById('a2pBrandType')?.value || 'LOW_VOLUME');
            var payload = {
                business_name: (document.getElementById('a2pBizName')?.value || '').trim(),
                ein: (document.getElementById('a2pEIN')?.value || '').trim(),
                street: (document.getElementById('a2pStreet')?.value || '').trim(),
                city: (document.getElementById('a2pCity')?.value || '').trim(),
                state: (document.getElementById('a2pState')?.value || '').trim(),
                zip: (document.getElementById('a2pZip')?.value || '').trim(),
                contact_email: (document.getElementById('a2pContactEmail')?.value || '').trim(),
                contact_phone: (document.getElementById('a2pContactPhone')?.value || '').trim(),
                website: (document.getElementById('a2pWebsite')?.value || '').trim(),
                first_name: (document.getElementById('a2pFirstName')?.value || '').trim(),
                last_name: (document.getElementById('a2pLastName')?.value || '').trim(),
                job_title: (document.getElementById('a2pJobTitle')?.value || '').trim(),
                job_position: (document.getElementById('a2pJobPosition')?.value || 'CEO').trim(),
                brand_type: brandType,
            };

            // Validate
            if (!payload.business_name) { result.innerHTML = '<span style="color:#ef4444;">Business name required</span>'; return; }
            if (brandType !== 'SOLE_PROPRIETOR' && !payload.ein) { result.innerHTML = '<span style="color:#ef4444;">EIN required</span>'; return; }
            if (!payload.contact_email) { result.innerHTML = '<span style="color:#ef4444;">Contact email required</span>'; return; }
            if (!payload.first_name || !payload.last_name) { result.innerHTML = '<span style="color:#ef4444;">First and last name required</span>'; return; }

            // Save form data before Stripe redirect so we can submit after payment
            sessionStorage.setItem('a2p_brand_form', JSON.stringify(payload));

            btn.disabled = true;
            btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-2"></i>Redirecting to payment...';
            result.innerHTML = '';
            try {
                var r = await fetch('/a2p/brand-checkout', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ brand_type: brandType }),
                });
                var d = await r.json();
                if (d.checkout_url) {
                    window.top.location.href = d.checkout_url;
                } else {
                    result.innerHTML = '<span style="color:#ef4444;">' + _esc(d.error || 'Checkout failed') + '</span>';
                    btn.disabled = false;
                    btn.innerHTML = '<i class="fa-solid fa-credit-card"></i> Register Brand & Pay';
                }
            } catch(e) {
                result.innerHTML = '<span style="color:#ef4444;">Network error</span>';
                btn.disabled = false;
                btn.innerHTML = '<i class="fa-solid fa-credit-card"></i> Register Brand & Pay';
            }
        }
        window.a2pSubmitBrand = a2pSubmitBrand;
        // Backward compat alias
        function a2pRegisterBrand() { a2pSubmitBrand(); }

        // ── Create Campaign ──
        async function a2pCreateCampaign() {
            var result = document.getElementById('a2pCampaignResult');
            var btn = document.getElementById('a2pCreateCampaignBtn');
            var description = (document.getElementById('a2pDescription')?.value || '').trim();
            var samplesRaw = (document.getElementById('a2pSampleMessages')?.value || '').trim();
            var messageFlow = (document.getElementById('a2pMessageFlow')?.value || '').trim();
            var useCase = document.getElementById('a2pUseCase')?.value || 'LOW_VOLUME';
            var numberSids = a2pGetSelectedNumberSids('a2pCampaignNumbersList');

            if (description.length < 40) { result.innerHTML = '<span style="color:#ef4444;">Description must be at least 40 characters</span>'; return; }
            var samples = samplesRaw.split('\n').map(function(s) { return s.trim(); }).filter(function(s) { return s.length >= 20; });
            if (samples.length < 2) { result.innerHTML = '<span style="color:#ef4444;">At least 2 sample messages required (min 20 chars each)</span>'; return; }
            if (messageFlow.length < 40) { result.innerHTML = '<span style="color:#ef4444;">Message flow must be at least 40 characters</span>'; return; }

            btn.disabled = true;
            btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-2"></i>Submitting campaign...';
            result.innerHTML = '';
            try {
                var r = await fetch('/voice/a2p/create-campaign', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        description: description,
                        use_case: useCase,
                        sample_messages: samples,
                        message_flow: messageFlow,
                        has_embedded_links: document.getElementById('a2pEmbeddedLinks')?.checked || false,
                        has_embedded_phone: document.getElementById('a2pEmbeddedPhone')?.checked || false,
                        phone_number_sids: numberSids,
                    }),
                });
                var d = await r.json();
                if (r.ok) {
                    result.innerHTML = '<span style="color:#00ff88;"><i class="fa-solid fa-circle-check me-1"></i>' + _esc(d.message || 'Campaign submitted!') + '</span>';
                    setTimeout(function() { a2pLoadStatus(); }, 500);
                } else {
                    result.innerHTML = '<span style="color:#ef4444;"><i class="fa-solid fa-triangle-exclamation me-1"></i>' + _esc(d.error || 'Campaign creation failed') + '</span>';
                }
            } catch(e) {
                result.innerHTML = '<span style="color:#ef4444;">Network error</span>';
            }
            btn.disabled = false;
            btn.innerHTML = '<i class="fa-solid fa-bullhorn me-2"></i>Submit Campaign for Approval';
        }

        // ── Refresh Status ──
        async function a2pRefreshStatus() {
            try {
                if (!_a2pStatus || !_a2pStatus.brand_sid) {
                    // No brand known — try syncing from Twilio first
                    await a2pSyncFromTwilio();
                    return; // a2pSyncFromTwilio already calls a2pLoadStatus
                }
                // Refresh brand status if exists
                if (_a2pStatus.brand_sid) {
                    var r = await fetch('/voice/a2p/brand-status');
                    if (r.ok) {
                        var d = await r.json();
                        _a2pStatus.brand_status = d.status;
                    }
                }
                // Refresh campaign status if exists
                if (_a2pStatus && _a2pStatus.campaign_sid) {
                    var r2 = await fetch('/voice/a2p/campaign-status');
                    if (r2.ok) {
                        var d2 = await r2.json();
                        _a2pStatus.campaign_status = d2.campaign_status;
                    }
                }
            } catch(e) { console.error('[A2P] Refresh error:', e); }
            a2pLoadStatus();
        }

        // ── A2P Fee Schedule (matches backend A2P_FEE_SCHEDULE) ──
        var _a2pFees = {
            SOLE_PROPRIETOR: { brand: 4.50, campaign: 15, label: 'Sole Proprietor' },
            LOW_VOLUME:      { brand: 4.50, campaign: 15, label: 'Low Volume Standard' },
            STANDARD:        { brand: 46,   campaign: 15, label: 'Standard' },
        };

        function a2pGetSelectedBrandType() {
            // Payment gate has its own selector; brand form has another.
            // Payment gate shown for sub-users, brand form for everyone.
            var paySelect = document.getElementById('a2pPayBrandType');
            var brandSelect = document.getElementById('a2pBrandType');
            if (paySelect && paySelect.offsetParent !== null) return paySelect.value;
            if (brandSelect) return brandSelect.value;
            return 'LOW_VOLUME';
        }

        function a2pGetFeeTotal(brandType) {
            var info = _a2pFees[brandType] || _a2pFees.LOW_VOLUME;
            return info.brand + info.campaign;
        }

        // Update the payment gate button price when brand type changes
        function a2pUpdatePaymentPrice() {
            var select = document.getElementById('a2pPayBrandType');
            var label = document.getElementById('a2pPayBtnLabel');
            if (!select || !label) return;
            var total = a2pGetFeeTotal(select.value);
            label.textContent = 'Pay Registration Fee — $' + total.toFixed(2);
            // Sync the brand form selector if it exists
            var brandSelect = document.getElementById('a2pBrandType');
            if (brandSelect) brandSelect.value = select.value;
            a2pUpdateBrandFeeDisplay();
        }

        // Update the fee note in the brand registration form + toggle EIN/SSN label
        function a2pUpdateBrandFeeDisplay() {
            var select = document.getElementById('a2pBrandType');
            var note = document.getElementById('a2pBrandFeeNote');
            if (!select || !note) return;
            var info = _a2pFees[select.value] || _a2pFees.LOW_VOLUME;
            var total = info.brand + info.campaign;
            note.textContent = 'One-time brand fee: $' + info.brand.toFixed(2) + ' + $' + info.campaign.toFixed(2) + ' campaign vetting = $' + total.toFixed(2) + ' total';

            // Toggle EIN/SSN label — sole proprietors use SSN, others use EIN
            var einLabel = document.getElementById('a2pEINLabel');
            var einInput = document.getElementById('a2pEIN');
            if (einLabel && einInput) {
                if (select.value === 'SOLE_PROPRIETOR') {
                    einLabel.textContent = 'SSN (last 4 digits)';
                    einInput.placeholder = 'XXXX';
                    einInput.maxLength = 4;
                } else {
                    einLabel.textContent = 'EIN (Tax ID)';
                    einInput.placeholder = 'XX-XXXXXXX';
                    einInput.maxLength = 10;
                }
            }

            // Sync the payment gate selector if it exists
            var paySelect = document.getElementById('a2pPayBrandType');
            if (paySelect) paySelect.value = select.value;
            a2pUpdatePaymentPrice();
        }

        // ── Pay A2P Fee (Stripe) ──
        async function a2pPayFee() {
            var btn = document.getElementById('a2pPayFeeBtn');
            var result = document.getElementById('a2pPayResult');
            var brandType = a2pGetSelectedBrandType();
            var total = a2pGetFeeTotal(brandType);

            if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-2"></i>Redirecting to payment...'; }
            try {
                var r = await fetch('/a2p/checkout', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ brand_type: brandType }),
                });
                var d = await r.json();
                if (d.checkout_url) {
                    window.top.location.href = d.checkout_url;
                } else {
                    if (result) result.innerHTML = '<span style="color:#ef4444;">' + _esc(d.error || 'Failed to create checkout') + '</span>';
                }
            } catch(e) {
                if (result) result.innerHTML = '<span style="color:#ef4444;">Network error</span>';
            }
            if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-lock me-2"></i><span id="a2pPayBtnLabel">Pay Registration Fee — $' + total.toFixed(2) + '</span>'; }
        }

        // Check URL params for phone number purchase success
        (function() {
            var params = new URLSearchParams(window.location.search);
            if (params.get('number_purchased') === '1') {
                var phone = params.get('phone') || '';
                if (phone) {
                    // Complete the purchase — provision the number after Stripe payment
                    fetch('/voice/numbers/complete-purchase', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({ phone_number: phone })
                    }).then(r => r.json()).then(d => {
                        if (d.status === 'purchased') {
                            if (typeof _showDashToast === 'function') _showDashToast(true, 'Number purchased: ' + (d.phone || phone));
                        } else {
                            if (typeof _showDashToast === 'function') _showDashToast(false, d.error || 'Number provisioning failed');
                        }
                        // Switch to Voice > Numbers tab and reload
                        if (typeof switchVoicePanel === 'function') switchVoicePanel('numbers');
                        if (typeof loadNumbersTab === 'function') loadNumbersTab();
                    }).catch(() => {
                        if (typeof _showDashToast === 'function') _showDashToast(false, 'Network error completing number purchase');
                    });
                }
                // Clean URL
                var url = new URL(window.location);
                url.searchParams.delete('number_purchased');
                url.searchParams.delete('phone');
                window.history.replaceState({}, '', url);
            }
        })();

        // Check URL params for A2P payment success (old combined + new split)
        (function() {
            var params = new URLSearchParams(window.location.search);
            // Old combined payment flow (backward compat)
            if (params.get('a2p_payment_success') === '1') {
                fetch('/voice/a2p/mark-fee-paid', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}' })
                    .then(function() { if (typeof switchVoicePanel === 'function') switchVoicePanel('a2p'); });
                var url = new URL(window.location);
                url.searchParams.delete('a2p_payment_success');
                window.history.replaceState({}, '', url);
            }
            // New split: brand payment success → submit saved form to Twilio
            if (params.get('a2p_brand_paid') === '1') {
                if (typeof _showDashToast === 'function') _showDashToast(true, 'Brand fee paid! Submitting registration...');
                var brandForm = JSON.parse(sessionStorage.getItem('a2p_brand_form') || '{}');
                if (brandForm.business_name) {
                    fetch('/voice/a2p/register-brand', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(brandForm),
                    }).then(function(r) { return r.json(); }).then(function(d) {
                        if (d.brand_sid || d.status === 'ok') {
                            if (typeof _showDashToast === 'function') _showDashToast(true, 'Brand submitted for vetting!');
                        } else {
                            if (typeof _showDashToast === 'function') _showDashToast(false, d.error || 'Brand submission failed');
                        }
                        sessionStorage.removeItem('a2p_brand_form');
                        setTimeout(function() { a2pBrandInit(); }, 500);
                    }).catch(function() {
                        if (typeof _showDashToast === 'function') _showDashToast(false, 'Network error submitting brand');
                    });
                }
                var url2 = new URL(window.location);
                url2.searchParams.delete('a2p_brand_paid');
                window.history.replaceState({}, '', url2);
            }
            // New split: campaign payment success → submit saved form to Twilio
            if (params.get('a2p_campaign_paid') === '1') {
                if (typeof _showDashToast === 'function') _showDashToast(true, 'Campaign fee paid! Submitting registration...');
                var campaignForm = JSON.parse(sessionStorage.getItem('a2p_campaign_form') || '{}');
                if (campaignForm.description) {
                    fetch('/voice/a2p/create-campaign', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(campaignForm),
                    }).then(function(r) { return r.json(); }).then(function(d) {
                        if (d.campaign_sid || d.status === 'ok') {
                            if (typeof _showDashToast === 'function') _showDashToast(true, 'Campaign submitted for approval!');
                        } else {
                            if (typeof _showDashToast === 'function') _showDashToast(false, d.error || 'Campaign submission failed');
                        }
                        sessionStorage.removeItem('a2p_campaign_form');
                        setTimeout(function() { a2pCampaignInit(); }, 500);
                    }).catch(function() {
                        if (typeof _showDashToast === 'function') _showDashToast(false, 'Network error submitting campaign');
                    });
                }
                var url3 = new URL(window.location);
                url3.searchParams.delete('a2p_campaign_paid');
                window.history.replaceState({}, '', url3);
            }
        })();

        // ── A2P Campaign Panel ──
        async function a2pCampaignInit() {
            try {
                if (!_a2pData) {
                    var r = await fetch('/voice/a2p/status');
                    var d = await r.json();
                    _a2pData = _a2pData || {};
                    _a2pData.status = d;
                }
                _a2pRenderCampaignPanel(_a2pData.status);
            } catch(e) { console.error('[A2P] Campaign init error:', e); }
        }
        window.a2pCampaignInit = a2pCampaignInit;

        function _a2pRenderCampaignPanel(d) {
            var el = document.getElementById('a2pCampaignContent');
            if (!el) return;
            var badge = document.getElementById('a2pCampaignBadge');

            // No brand or brand not approved — disabled
            if (!d.brand_sid || (d.brand_status || '').toUpperCase() !== 'APPROVED') {
                var msg = !d.brand_sid
                    ? 'Register and get your Brand approved before creating a Campaign.'
                    : 'Brand is under review. Campaign registration will unlock once approved.';
                el.innerHTML = '<div class="a2p-disabled-panel"><i class="fa-solid fa-lock"></i> ' + _esc(msg) + '</div>';
                if (badge) { badge.textContent = 'Locked'; badge.className = 'a2p-status-badge a2p-badge-off'; }
                return;
            }

            // Campaign exists — show status
            if (d.campaign_sid) {
                var cs = (d.campaign_status || 'PENDING').toUpperCase();
                var isOk = cs === 'VERIFIED' || cs === 'APPROVED';
                var isPending = cs === 'PENDING' || cs === 'IN_PROGRESS';
                if (badge) {
                    badge.textContent = isOk ? 'Approved' : (isPending ? 'Pending' : cs);
                    badge.className = 'a2p-status-badge ' + (isOk ? 'a2p-badge-approved' : (isPending ? 'a2p-badge-pending' : 'a2p-badge-failed'));
                }
                el.innerHTML =
                    '<div class="a2p-status-card ' + (isOk ? 'approved' : (isPending ? 'pending' : 'failed')) + '">' +
                    '<div class="a2p-status-card-header"><i class="fa-solid ' + (isOk ? 'fa-circle-check' : 'fa-clock') + '"></i> Campaign ' + _esc(cs) + '</div>' +
                    '<div class="a2p-status-card-detail">Use case: ' + _esc(d.use_case || '') + '</div>' +
                    (d.campaign_sid ? '<div class="a2p-status-card-detail">Campaign SID: ' + _esc(d.campaign_sid) + '</div>' : '') +
                    '</div>';
                if (isPending) _a2pStartCampaignPoll();
                return;
            }

            // Brand approved, no campaign — show create button
            if (badge) { badge.textContent = 'Not Registered'; badge.className = 'a2p-status-badge a2p-badge-off'; }
            el.innerHTML =
                '<div class="a2p-create-section">' +
                '<p class="a2p-create-desc">Create a messaging campaign and link your phone numbers for SMS compliance.</p>' +
                '<button class="a2p-create-btn" onclick="a2pShowCampaignForm()"><i class="fa-solid fa-plus"></i> Create New Campaign</button>' +
                '</div>';
        }

        function a2pShowCampaignForm() {
            var el = document.getElementById('a2pCampaignContent');
            if (!el) return;
            var html = '<div class="a2p-form">';

            // Use case
            html += '<div class="a2p-field-group"><label class="a2p-field-label">Use Case</label>';
            html += '<select id="a2pCmpUseCase" class="a2p-select"><option value="LOW_VOLUME">Low Volume</option><option value="MIXED">Mixed</option><option value="MARKETING">Marketing</option></select></div>';

            // Description
            html += '<div class="a2p-field-group"><label class="a2p-field-label">Campaign Description ' + _a2pInfoTooltip(_A2P_DESCRIPTION_EXAMPLE) + '</label>';
            html += '<textarea id="a2pCmpDesc" class="a2p-textarea" rows="3" placeholder="Describe what messages you send and why (min 40 chars)..."></textarea></div>';

            // Message flow
            html += '<div class="a2p-field-group"><label class="a2p-field-label">Message Flow / Opt-In ' + _a2pInfoTooltip(_A2P_MESSAGE_FLOW_EXAMPLE) + '</label>';
            html += '<textarea id="a2pCmpFlow" class="a2p-textarea" rows="3" placeholder="How do leads opt in to receive messages? (min 40 chars)..."></textarea></div>';

            // Sample messages (4 separate textareas)
            for (var i = 0; i < 4; i++) {
                var req = i < 2 ? ' <span style="color:#ef4444;">*</span>' : ' <span style="color:#555;">(optional)</span>';
                html += '<div class="a2p-field-group"><label class="a2p-field-label">Sample Message ' + (i + 1) + req + ' ' + _a2pInfoTooltip(_A2P_SAMPLE_MESSAGES[i]) + '</label>';
                html += '<textarea id="a2pCmpSample' + i + '" class="a2p-textarea" rows="2" placeholder="Min 20 characters..."></textarea></div>';
            }

            // Checkboxes
            html += '<div class="a2p-field-row">';
            html += '<label class="a2p-checkbox-label"><input type="checkbox" id="a2pCmpLinks"> Messages contain embedded links</label>';
            html += '<label class="a2p-checkbox-label"><input type="checkbox" id="a2pCmpPhone"> Messages contain phone numbers</label>';
            html += '</div>';

            // Phone numbers
            html += '<div class="a2p-field-group"><label class="a2p-field-label">Phone Numbers to Link</label>';
            html += '<div id="a2pCmpPhoneList" class="a2p-phone-list"><i class="fa-solid fa-spinner fa-spin"></i> Loading...</div></div>';

            // Submit
            html += '<div id="a2pCmpResult" class="a2p-form-result"></div>';
            html += '<button class="a2p-submit-btn" id="a2pSubmitCampaignBtn" onclick="a2pSubmitCampaign()"><i class="fa-solid fa-credit-card"></i> Submit Campaign &amp; Pay ($15)</button>';
            html += '</div>';

            el.innerHTML = html;
            _a2pLoadPhoneNumbers('a2pCmpPhoneList');
        }
        window.a2pShowCampaignForm = a2pShowCampaignForm;

        async function a2pSubmitCampaign() {
            var result = document.getElementById('a2pCmpResult');
            var btn = document.getElementById('a2pSubmitCampaignBtn');
            var desc = (document.getElementById('a2pCmpDesc')?.value || '').trim();
            var flow = (document.getElementById('a2pCmpFlow')?.value || '').trim();
            var useCase = document.getElementById('a2pCmpUseCase')?.value || 'LOW_VOLUME';

            // Collect sample messages
            var samples = [];
            for (var i = 0; i < 4; i++) {
                var s = (document.getElementById('a2pCmpSample' + i)?.value || '').trim();
                if (s) samples.push(s);
            }

            // Collect phone SIDs
            var sids = [];
            document.querySelectorAll('#a2pCmpPhoneList input[type="checkbox"]:checked').forEach(function(cb) {
                sids.push(cb.value);
            });

            // Validate
            if (desc.length < 40) { result.innerHTML = '<span style="color:#ef4444;">Description must be at least 40 characters</span>'; return; }
            if (flow.length < 40) { result.innerHTML = '<span style="color:#ef4444;">Message flow must be at least 40 characters</span>'; return; }
            if (samples.length < 2) { result.innerHTML = '<span style="color:#ef4444;">At least 2 sample messages required</span>'; return; }
            for (var j = 0; j < samples.length; j++) {
                if (samples[j].length < 20) { result.innerHTML = '<span style="color:#ef4444;">Sample message ' + (j + 1) + ' must be at least 20 characters</span>'; return; }
            }
            if (!sids.length) { result.innerHTML = '<span style="color:#ef4444;">Select at least one phone number</span>'; return; }

            // Save form to sessionStorage then redirect to Stripe
            sessionStorage.setItem('a2p_campaign_form', JSON.stringify({
                description: desc, use_case: useCase, sample_messages: samples,
                message_flow: flow, has_embedded_links: document.getElementById('a2pCmpLinks')?.checked || false,
                has_embedded_phone: document.getElementById('a2pCmpPhone')?.checked || false, phone_number_sids: sids,
            }));

            btn.disabled = true;
            btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-2"></i>Redirecting to payment...';
            try {
                var r = await fetch('/a2p/campaign-checkout', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: '{}',
                });
                var d = await r.json();
                if (d.checkout_url) {
                    window.top.location.href = d.checkout_url;
                } else {
                    result.innerHTML = '<span style="color:#ef4444;">' + _esc(d.error || 'Checkout failed') + '</span>';
                    btn.disabled = false;
                    btn.innerHTML = '<i class="fa-solid fa-credit-card"></i> Submit Campaign & Pay ($15)';
                }
            } catch(e) {
                result.innerHTML = '<span style="color:#ef4444;">Network error</span>';
                btn.disabled = false;
                btn.innerHTML = '<i class="fa-solid fa-credit-card"></i> Submit Campaign & Pay ($15)';
            }
        }
        window.a2pSubmitCampaign = a2pSubmitCampaign;

        // ── A2P Info Tooltips ──
        function _a2pInfoTooltip(text) {
            var id = 'a2pTip_' + Math.random().toString(36).substr(2, 6);
            return '<button type="button" class="a2p-info-btn" onclick="event.preventDefault();event.stopPropagation();a2pToggleTip(\'' + id + '\')">' +
                '<i class="fa-solid fa-circle-info"></i></button>' +
                '<div class="a2p-info-tooltip" id="' + id + '" style="display:none;">' +
                '<div class="a2p-info-tooltip-text">' + _esc(text) + '</div>' +
                '<button type="button" class="a2p-info-copy-btn" onclick="navigator.clipboard.writeText(document.getElementById(\'' + id + '\').querySelector(\'.a2p-info-tooltip-text\').textContent);this.textContent=\'Copied!\'">Copy</button>' +
                '</div>';
        }

        function a2pToggleTip(id) {
            var el = document.getElementById(id);
            if (!el) return;
            document.querySelectorAll('.a2p-info-tooltip').forEach(function(t) { if (t.id !== id) t.style.display = 'none'; });
            el.style.display = el.style.display === 'none' ? 'block' : 'none';
        }
        window.a2pToggleTip = a2pToggleTip;

        function _a2pLoadPhoneNumbers(containerId) {
            var el = document.getElementById(containerId);
            if (!el) return;
            fetch('/voice/numbers').then(function(r) { return r.json(); }).then(function(d) {
                var nums = d.numbers || [];
                if (!nums.length) { el.innerHTML = '<span style="color:#888;">No phone numbers. Buy numbers first.</span>'; return; }
                var html = '';
                nums.forEach(function(n) {
                    html += '<label class="a2p-phone-item"><input type="checkbox" value="' + _esc(n.id || n.sid || '') + '" checked> ' + _esc(n.phone || '') + '</label>';
                });
                el.innerHTML = html;
            }).catch(function() { el.innerHTML = '<span style="color:#ef4444;">Failed to load numbers</span>'; });
        }

        function _a2pStartCampaignPoll() {
            if (window._a2pCampaignPollTimer) return;
            window._a2pCampaignPollTimer = setInterval(function() {
                fetch('/voice/a2p/campaign-status').then(function(r) { return r.json(); }).then(function(d) {
                    var cs = (d.campaign_status || '').toUpperCase();
                    if (cs === 'VERIFIED' || cs === 'APPROVED' || cs === 'FAILED') {
                        clearInterval(window._a2pCampaignPollTimer);
                        window._a2pCampaignPollTimer = null;
                        a2pCampaignInit();
                    }
                }).catch(function() {});
            }, 30000);
        }

        function a2pCheckPaymentRedirect() {
            // Already handled by the IIFE above
        }
        window.a2pCheckPaymentRedirect = a2pCheckPaymentRedirect;
