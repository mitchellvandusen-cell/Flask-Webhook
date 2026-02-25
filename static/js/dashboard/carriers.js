    // Toggle carrier chip on click
    document.querySelectorAll('.carrier-chip').forEach(chip => {
        chip.addEventListener('click', function() {
            const cb = this.querySelector('input[type="checkbox"]');
            cb.checked = !cb.checked;
            this.classList.toggle('selected', cb.checked);
            updateCarrierCount();
        });
    });

    function updateCarrierCount() {
        const checked = document.querySelectorAll('.carrier-chip input:checked').length;
        const el = document.getElementById('carrierCount');
        if (el) el.textContent = checked + ' selected';
    }

    function filterCarriers(query) {
        const q = query.toLowerCase().trim();
        document.querySelectorAll('.carrier-chip').forEach(chip => {
            const name = chip.getAttribute('data-name') || '';
            chip.classList.toggle('hidden', q && !name.includes(q));
        });
    }

    function selectAllCarriers() {
        document.querySelectorAll('.carrier-chip:not(.hidden)').forEach(chip => {
            chip.classList.add('selected');
            chip.querySelector('input[type="checkbox"]').checked = true;
        });
        updateCarrierCount();
    }

    function clearAllCarriers() {
        document.querySelectorAll('.carrier-chip').forEach(chip => {
            chip.classList.remove('selected');
            chip.querySelector('input[type="checkbox"]').checked = false;
        });
        updateCarrierCount();
    }

    function saveCarriers() {
        const btn = document.getElementById('saveCarriersBtn');
        const status = document.getElementById('carrierSaveStatus');
        const selected = [];
        document.querySelectorAll('.carrier-chip input:checked').forEach(cb => {
            selected.push(cb.value);
        });

        btn.disabled = true;
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin me-2"></i> Saving...';

        fetch('/api/carriers', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({carriers: selected})
        })
        .then(r => r.json())
        .then(data => {
            btn.disabled = false;
            btn.innerHTML = '<i class="fa-solid fa-floppy-disk me-2"></i> Save Carriers';
            if (data.status === 'success') {
                _showDashToast(true, 'Carriers saved!');
                status.style.display = 'inline';
                setTimeout(() => { status.style.display = 'none'; }, 3000);
            } else {
                _showDashToast(false, 'Failed to save carriers: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(err => {
            btn.disabled = false;
            btn.innerHTML = '<i class="fa-solid fa-floppy-disk me-2"></i> Save Carriers';
            _showDashToast(false, 'Network error saving carriers.');
        });
    }

    // ═══════════════════════════════════════
