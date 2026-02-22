    function saveConfig() {
        const form = document.getElementById('main-config-form');
        const overlay = document.getElementById('save-overlay');
        const spinner = document.getElementById('save-spinner');
        const check = document.getElementById('save-check');
        const text = document.getElementById('save-text');
        const btn = document.getElementById('save-config-btn');

        // Collect form data
        const data = {
            location_id: form.querySelector('[name="location_id"]')?.value || '',
            calendar_id: form.querySelector('[name="calendar_id"]')?.value || '',
            calendar_name: form.querySelector('[name="calendar_name"]')?.value || '',
            crm_user_id: form.querySelector('[name="crm_user_id"]')?.value || '',
            bot_name: form.querySelector('[name="bot_name"]')?.value || '',
            timezone: form.querySelector('[name="timezone"]')?.value || '',
            initial_message: form.querySelector('[name="initial_message"]')?.value || '',
            personal_website: form.querySelector('[name="personal_website"]')?.value || ''
        };

        // Show overlay in "saving" state
        spinner.style.display = 'block';
        check.style.display = 'none';
        text.textContent = 'Saving to database...';
        overlay.classList.add('active');
        btn.disabled = true;

        fetch('/api/save-config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(data)
        })
        .then(r => r.json())
        .then(resp => {
            if (resp.success) {
                // Switch to success state
                spinner.style.display = 'none';
                check.style.display = 'block';
                text.textContent = 'Saved to database';
                setTimeout(() => {
                    overlay.classList.remove('active');
                    btn.disabled = false;
                }, 1500);
            } else {
                spinner.style.display = 'none';
                text.textContent = 'Error: ' + (resp.error || 'Save failed');
                text.style.color = '#ef4444';
                setTimeout(() => {
                    overlay.classList.remove('active');
                    btn.disabled = false;
                    text.style.color = '#fff';
                }, 2500);
            }
        })
        .catch(err => {
            spinner.style.display = 'none';
            text.textContent = 'Network error. Please try again.';
            text.style.color = '#ef4444';
            setTimeout(() => {
                overlay.classList.remove('active');
                btn.disabled = false;
                text.style.color = '#fff';
            }, 2500);
        });
    }
