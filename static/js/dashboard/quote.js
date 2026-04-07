// static/js/dashboard/quote.js — Life insurance quoting engine frontend
//
// Pure JavaScript — zero dependencies. Integrates with Omnisconn dialer UI.
// Calls backend API endpoints for deterministic quoting.
//
// ── STATE ──
let _qtConditions = [];        // Array of {condition_id, condition_name, answers: {}}
let _qtSearchTimer = null;     // Debounce timer for autocomplete
let _qtResults = [];           // Last quote results
let _qtDeclinedVisible = false;

// ── INITIALIZATION ──
function iosQuoteInit() {
    _qtConditions = [];
    _qtResults = [];
    _qtDeclinedVisible = false;
    document.getElementById('qtConditionCards').innerHTML = '';
    document.getElementById('qtNoConditions').style.display = 'block';
    document.getElementById('qtResultsWrap').style.display = 'none';
    document.getElementById('qtLoading').style.display = 'none';
    document.getElementById('qtDetailPanel').style.display = 'none';
    document.getElementById('qtAge').value = '';
    document.getElementById('qtGender').value = 'male';
    document.getElementById('qtTobacco').value = 'non_tobacco';
    document.getElementById('qtState').value = '';
    document.getElementById('qtFaceAmount').value = '';
    document.getElementById('qtMaxPremium').value = '';
    document.getElementById('qtProductType').value = 'final_expense';
    document.getElementById('qtPayment').value = 'bank_draft';
    document.getElementById('qtCoverageType').value = 'all';
    document.getElementById('qtHeight').value = '';
    document.getElementById('qtWeight').value = '';
    // Reset gender toggle buttons to match default
    const toggleGroup = document.querySelector('#iosAppQuote .qt-toggle-group');
    if (toggleGroup) {
        for (const btn of toggleGroup.children) {
            btn.classList.toggle('qt-toggle-active', btn.dataset.val === 'male');
        }
    }
    document.getElementById('qtAge').focus();
}

// ── GENDER TOGGLE ──
function qtToggle(btn, hiddenId) {
    const siblings = btn.parentNode.children;
    for (let sib of siblings) {
        sib.classList.remove('qt-toggle-active');
    }
    btn.classList.add('qt-toggle-active');
    document.getElementById(hiddenId).value = btn.dataset.val;
}

// ── DOB TO AGE ──
function qtDobToAge() {
    const dobInput = document.getElementById('qtDob');
    if (!dobInput.value) return;
    const dob = new Date(dobInput.value);
    const today = new Date();
    let age = today.getFullYear() - dob.getFullYear();
    const monthDiff = today.getMonth() - dob.getMonth();
    if (monthDiff < 0 || (monthDiff === 0 && today.getDate() < dob.getDate())) {
        age--;
    }
    document.getElementById('qtAge').value = age;
}

// ── CONDITION SEARCH (debounced autocomplete) ──
function quoteSearchConditions(query) {
    if (_qtSearchTimer) clearTimeout(_qtSearchTimer);
    _qtSearchTimer = setTimeout(() => {
        if (query.length < 2) {
            document.getElementById('qtConditionResults').style.display = 'none';
            return;
        }
        fetch(`/api/quote/conditions?q=${encodeURIComponent(query)}`)
            .then(r => r.json())
            .then(data => {
                const resultsDiv = document.getElementById('qtConditionResults');
                resultsDiv.innerHTML = '';
                if (data.length === 0) {
                    resultsDiv.style.display = 'none';
                    return;
                }
                data.forEach(item => {
                    const div = document.createElement('div');
                    div.className = 'qt-autocomplete-item';
                    div.innerHTML = `<strong>${item.name}</strong> <span class="qt-autocomplete-sub">${item.category}</span>`;
                    div.onclick = () => quoteAddCondition(item.id, item.name);
                    resultsDiv.appendChild(div);
                });
                resultsDiv.style.display = 'block';
            })
            .catch(err => console.error('Condition search error:', err));
    }, 300);
}

// ── MEDICATION SEARCH (debounced autocomplete) ──
function quoteSearchMedications(query) {
    if (_qtSearchTimer) clearTimeout(_qtSearchTimer);
    _qtSearchTimer = setTimeout(() => {
        if (query.length < 2) {
            document.getElementById('qtMedResults').style.display = 'none';
            return;
        }
        fetch(`/api/quote/medications?q=${encodeURIComponent(query)}`)
            .then(r => r.json())
            .then(data => {
                const resultsDiv = document.getElementById('qtMedResults');
                resultsDiv.innerHTML = '';
                if (data.length === 0) {
                    resultsDiv.style.display = 'none';
                    return;
                }
                data.forEach(item => {
                    const div = document.createElement('div');
                    div.className = 'qt-autocomplete-item';
                    const generic = item.generic_name ? ` (${item.generic_name})` : '';
                    div.innerHTML = `<strong>${item.name}</strong>${generic}`;
                    div.onclick = () => quoteSelectMedication(item.id, item.name);
                    resultsDiv.appendChild(div);
                });
                resultsDiv.style.display = 'block';
            })
            .catch(err => console.error('Medication search error:', err));
    }, 300);
}

// ── MEDICATION → CONDITION PICKER ──
function quoteSelectMedication(medId, medName) {
    document.getElementById('qtMedResults').style.display = 'none';
    document.getElementById('qtMedSearch').value = '';
    fetch(`/api/quote/drug-conditions/${medId}`)
        .then(r => r.json())
        .then(data => {
            if (data.length === 1) {
                quoteAddCondition(data[0].id, data[0].name);
                return;
            }
            // Multiple conditions — show picker
            const picker = document.getElementById('qtMedConditionPicker');
            const titleEl = picker.querySelector('.qt-med-picker-title');
            titleEl.textContent = `What is ${medName} for?`;
            const btnsDiv = document.getElementById('qtMedConditionList');
            btnsDiv.innerHTML = '';
            data.forEach(cond => {
                const btn = document.createElement('button');
                btn.className = 'qt-med-picker-btn';
                btn.textContent = cond.name;
                btn.onclick = () => {
                    quoteAddCondition(cond.id, cond.name);
                    picker.style.display = 'none';
                };
                btnsDiv.appendChild(btn);
            });
            picker.style.display = 'block';
        })
        .catch(err => console.error('Drug conditions error:', err));
}

// ── ADD CONDITION (fetch questionnaire, render card) ──
function quoteAddCondition(conditionId, conditionName) {
    if (_qtConditions.some(c => c.condition_id === conditionId)) return;
    document.getElementById('qtConditionResults').style.display = 'none';
    document.getElementById('qtMedResults').style.display = 'none';
    document.getElementById('qtMedConditionPicker').style.display = 'none';
    document.getElementById('qtConditionSearch').value = '';
    document.getElementById('qtMedSearch').value = '';
    _renderConditionCard(conditionId, conditionName, {});
}

// ── RENDER CONDITION CARD (shared by add + load) ──
function _renderConditionCard(conditionId, conditionName, savedAnswers) {
    fetch(`/api/quote/questions/${conditionId}`)
        .then(r => r.json())
        .then(questions => {
            // Only push to state if not already there (add path)
            if (!_qtConditions.some(c => c.condition_id === conditionId)) {
                _qtConditions.push({ condition_id: conditionId, condition_name: conditionName, answers: savedAnswers });
            }
            const cardsDiv = document.getElementById('qtConditionCards');
            const card = document.createElement('div');
            card.className = 'qt-condition-card';
            card.setAttribute('data-cid', conditionId);
            card.innerHTML = `
                <div class="qt-condition-header">
                    <span>${conditionName}</span>
                    <button onclick="quoteRemoveCondition(${conditionId})" class="qt-close-btn">&times;</button>
                </div>
                <div class="qt-condition-questions">
                    ${questions.map(q => {
                        const key = q.question_text.toLowerCase().replace(/[^a-z0-9]/g, '_').substring(0, 20);
                        const saved = savedAnswers[key] || '';
                        if (q.question_type === 'single_choice') {
                            return `
                                <div class="qt-q-row">
                                    <label class="qt-q-label">${q.question_text}</label>
                                    <select class="qt-select" onchange="qtUpdateAnswer(${conditionId}, '${key}', this.value)">
                                        <option value="">Select</option>
                                        ${q.options.map(o => `<option value="${o.value}"${saved === o.value ? ' selected' : ''}>${o.label}</option>`).join('')}
                                    </select>
                                </div>
                            `;
                        } else if (q.question_type === 'yes_no') {
                            return `
                                <div class="qt-q-row">
                                    <label class="qt-q-label">${q.question_text}</label>
                                    <select class="qt-select" onchange="qtUpdateAnswer(${conditionId}, '${key}', this.value)">
                                        <option value="">Select</option>
                                        <option value="yes"${saved === 'yes' ? ' selected' : ''}>Yes</option>
                                        <option value="no"${saved === 'no' ? ' selected' : ''}>No</option>
                                    </select>
                                </div>
                            `;
                        } else if (q.question_type === 'multi_choice') {
                            const checkedArr = Array.isArray(saved) ? saved : [];
                            return `
                                <div class="qt-q-row">
                                    <label class="qt-q-label">${q.question_text}</label>
                                    <div>
                                        ${q.options.map(o => `
                                            <label class="qt-checkbox-label"><input type="checkbox" onchange="qtUpdateMultiAnswer(${conditionId}, '${key}', '${o.value}', this.checked)"${checkedArr.includes(o.value) ? ' checked' : ''}> ${o.label}</label>
                                        `).join('')}
                                    </div>
                                </div>
                            `;
                        } else if (q.question_type === 'date') {
                            return `
                                <div class="qt-q-row">
                                    <label class="qt-q-label">${q.question_text}</label>
                                    <input type="date" class="qt-input" value="${saved}" onchange="qtUpdateAnswer(${conditionId}, '${key}', this.value)">
                                </div>
                            `;
                        } else if (q.question_type === 'number') {
                            return `
                                <div class="qt-q-row">
                                    <label class="qt-q-label">${q.question_text}</label>
                                    <input type="number" class="qt-input" value="${saved}" onchange="qtUpdateAnswer(${conditionId}, '${key}', this.value)">
                                </div>
                            `;
                        }
                        return '';
                    }).join('')}
                </div>
            `;
            cardsDiv.appendChild(card);
            document.getElementById('qtNoConditions').style.display = 'none';
        })
        .catch(err => console.error('Questions fetch error:', err));
}

// ── UPDATE ANSWER ──
function qtUpdateAnswer(conditionId, key, value) {
    const cond = _qtConditions.find(c => c.condition_id === conditionId);
    if (cond) cond.answers[key] = value;
}

// ── UPDATE MULTI ANSWER ──
function qtUpdateMultiAnswer(conditionId, key, value, checked) {
    const cond = _qtConditions.find(c => c.condition_id === conditionId);
    if (!cond) return;
    if (!cond.answers[key]) cond.answers[key] = [];
    if (checked) {
        if (!cond.answers[key].includes(value)) cond.answers[key].push(value);
    } else {
        cond.answers[key] = cond.answers[key].filter(v => v !== value);
    }
}

// ── REMOVE CONDITION ──
function quoteRemoveCondition(conditionId) {
    _qtConditions = _qtConditions.filter(c => c.condition_id !== conditionId);
    const card = document.querySelector(`.qt-condition-card[data-cid="${conditionId}"]`);
    if (card) card.remove();
    if (_qtConditions.length === 0) {
        document.getElementById('qtNoConditions').style.display = 'block';
    }
}

// ── RUN QUOTE ──
function quoteGetQuote() {
    const age = document.getElementById('qtAge').value;
    const gender = document.getElementById('qtGender').value;
    const state = document.getElementById('qtState').value;
    const productType = document.getElementById('qtProductType').value;
    if (!age || !gender || !state || !productType) {
        _showDashToast(false, 'Fill in age, gender, state, and product type.');
        return;
    }
    const faceAmount = document.getElementById('qtFaceAmount').value;
    const maxPremium = document.getElementById('qtMaxPremium').value;
    if (!faceAmount && !maxPremium) {
        _showDashToast(false, 'Enter either face amount or budget.');
        return;
    }
    document.getElementById('qtLoading').style.display = 'block';
    document.getElementById('qtResultsWrap').style.display = 'none';
    const body = {
        age: parseInt(age),
        gender: gender,
        tobacco_class: document.getElementById('qtTobacco').value,
        state: state,
        face_amount: faceAmount ? parseInt(faceAmount) : null,
        max_premium: maxPremium ? parseFloat(maxPremium) : null,
        product_type: productType,
        payment_mode: document.getElementById('qtPayment').value,
        coverage_type_filter: document.getElementById('qtCoverageType').value,
        height_inches: document.getElementById('qtHeight').value ? parseInt(document.getElementById('qtHeight').value) : null,
        weight_lbs: document.getElementById('qtWeight').value ? parseInt(document.getElementById('qtWeight').value) : null,
        conditions: _qtConditions
    };
    fetch('/api/quote/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
    })
        .then(r => r.json())
        .then(data => {
            document.getElementById('qtLoading').style.display = 'none';
            document.getElementById('qtResultsWrap').style.display = 'block';
            _qtResults = data.results;
            document.getElementById('qtResultsSummary').textContent =
                `${data.total_results} carriers evaluated, ${data.carriers_with_rates} with rates`;
            _qtRenderResults();
        })
        .catch(err => {
            console.error('Quote error:', err);
            document.getElementById('qtLoading').style.display = 'none';
            _showDashToast(false, 'Error running quote. Please try again.');
        });
}

// ── RENDER RESULTS (separated for toggle reuse) ──
function _qtRenderResults() {
    const tbody = document.getElementById('qtResultsBody');
    tbody.innerHTML = '';
    _qtResults.forEach(result => {
        if (!_qtDeclinedVisible && (result.outcome === 'decline' || result.outcome === 'postpone')) return;
        const tr = document.createElement('tr');
        tr.onclick = () => quoteShowDetail(result.carrier_key);
        const premium = (result.outcome === 'decline' || result.outcome === 'postpone')
            ? 'N/A'
            : `$${result.monthly_premium.toFixed(2)}/mo`;
        tr.innerHTML = `
            <td>${result.carrier_name}</td>
            <td>${premium}</td>
            <td><span class="qt-badge qt-badge-${result.outcome}">${result.outcome}</span></td>
            <td><button class="qt-detail-btn">Detail</button></td>
        `;
        tbody.appendChild(tr);
    });
    // Update declined toggle
    const declinedCount = _qtResults.filter(r => r.outcome === 'decline' || r.outcome === 'postpone').length;
    const toggleWrap = document.getElementById('qtDeclinedToggle');
    if (declinedCount > 0) {
        toggleWrap.style.display = 'block';
        document.getElementById('qtDeclinedCount').textContent = declinedCount;
    } else {
        toggleWrap.style.display = 'none';
    }
}

// ── TOGGLE DECLINED ──
function quoteToggleDeclined() {
    _qtDeclinedVisible = !_qtDeclinedVisible;
    _qtRenderResults();
}

// ── SHOW DETAIL ──
function quoteShowDetail(carrierKey) {
    const result = _qtResults.find(r => r.carrier_key === carrierKey);
    if (!result) return;
    const panel = document.getElementById('qtDetailPanel');
    panel.style.display = 'block';
    document.getElementById('qtDetailCarrier').textContent = result.carrier_name;
    const detailsDiv = document.getElementById('qtDetailContent');
    let html = '';
    if (result.monthly_premium) {
        html += `<div class="qt-detail-row"><strong>Monthly Premium:</strong> $${result.monthly_premium.toFixed(2)}</div>`;
    }
    if (result.annual_premium) {
        html += `<div class="qt-detail-row"><strong>Annual Premium:</strong> $${result.annual_premium.toFixed(2)}</div>`;
    }
    if (result.face_amount) {
        html += `<div class="qt-detail-row"><strong>Face Amount:</strong> $${result.face_amount.toLocaleString()}</div>`;
    }
    html += `<div class="qt-detail-row"><strong>Outcome:</strong> <span class="qt-badge qt-badge-${result.outcome}">${result.outcome}</span></div>`;
    if (result.waiting_period_months) {
        html += `<div class="qt-detail-row"><strong>Waiting Period:</strong> ${result.waiting_period_months} months</div>`;
    }
    if (result.per_condition_results && result.per_condition_results.length > 0) {
        html += `<div class="qt-detail-row"><strong>Condition Details:</strong></div><ul>`;
        result.per_condition_results.forEach(pcr => {
            html += `<li style="color:#ccc;font-size:0.85rem;">${pcr.condition_name}: ${pcr.outcome_detail}</li>`;
        });
        html += `</ul>`;
    }
    if (result.rate_info) {
        html += `<div class="qt-detail-row"><strong>Rate Info:</strong> ${result.rate_info.source_document} (${result.rate_info.effective_date})</div>`;
    }
    detailsDiv.innerHTML = html;
    // Scroll detail into view
    panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

// ── CLEAR FIELDS ──
function quoteClearFields() {
    iosQuoteInit();
}

// ── SAVE PROFILE (localStorage) ──
function quoteSaveProfile() {
    const data = {
        age: document.getElementById('qtAge').value,
        gender: document.getElementById('qtGender').value,
        tobacco: document.getElementById('qtTobacco').value,
        state: document.getElementById('qtState').value,
        faceAmount: document.getElementById('qtFaceAmount').value,
        maxPremium: document.getElementById('qtMaxPremium').value,
        productType: document.getElementById('qtProductType').value,
        payment: document.getElementById('qtPayment').value,
        coverageType: document.getElementById('qtCoverageType').value,
        height: document.getElementById('qtHeight').value,
        weight: document.getElementById('qtWeight').value,
        conditions: _qtConditions
    };
    localStorage.setItem('quoteForm', JSON.stringify(data));
    _showDashToast(true, 'Quote form saved');
}

// ── LOAD PROFILE (localStorage, re-fetches questionnaires) ──
function quoteLoadProfile() {
    const raw = localStorage.getItem('quoteForm');
    if (!raw) {
        _showDashToast(false, 'No saved quote form found.');
        return;
    }
    const data = JSON.parse(raw);
    document.getElementById('qtAge').value = data.age || '';
    document.getElementById('qtGender').value = data.gender || 'male';
    document.getElementById('qtTobacco').value = data.tobacco || 'non_tobacco';
    document.getElementById('qtState').value = data.state || '';
    document.getElementById('qtFaceAmount').value = data.faceAmount || '';
    document.getElementById('qtMaxPremium').value = data.maxPremium || '';
    document.getElementById('qtProductType').value = data.productType || 'final_expense';
    document.getElementById('qtPayment').value = data.payment || 'bank_draft';
    document.getElementById('qtCoverageType').value = data.coverageType || 'all';
    document.getElementById('qtHeight').value = data.height || '';
    document.getElementById('qtWeight').value = data.weight || '';
    // Sync gender toggle buttons
    const toggleGroup = document.querySelector('#iosAppQuote .qt-toggle-group');
    if (toggleGroup) {
        const gender = data.gender || 'male';
        for (const btn of toggleGroup.children) {
            btn.classList.toggle('qt-toggle-active', btn.dataset.val === gender);
        }
    }
    // Re-render conditions with full questionnaires
    _qtConditions = [];
    const cardsDiv = document.getElementById('qtConditionCards');
    cardsDiv.innerHTML = '';
    const savedConditions = data.conditions || [];
    if (savedConditions.length > 0) {
        document.getElementById('qtNoConditions').style.display = 'none';
        savedConditions.forEach(cond => {
            _renderConditionCard(cond.condition_id, cond.condition_name, cond.answers || {});
        });
    } else {
        document.getElementById('qtNoConditions').style.display = 'block';
    }
    _showDashToast(true, 'Quote form loaded');
}
