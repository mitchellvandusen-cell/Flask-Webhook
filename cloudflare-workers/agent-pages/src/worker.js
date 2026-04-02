/**
 * Omnisconn Agent Pages — Cloudflare Worker
 *
 * Serves auto-generated landing pages for insurance agents.
 * One Worker handles ALL agent domains by reading the Host header
 * and looking up the agent's config from Workers KV.
 *
 * Routes:
 *   /          → Landing page with contact form
 *   /privacy   → Privacy Policy (A2P 10DLC compliant)
 *   /terms     → Terms of Service (A2P 10DLC compliant)
 *   /submit    → Contact form handler (POST → forwards to Flask API)
 */

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const hostname = url.hostname;
    const path = url.pathname;

    // Look up agent config from KV
    const configRaw = await env.AGENT_CONFIG.get(hostname);
    if (!configRaw) {
      return new Response(notFoundPage(hostname), {
        status: 404,
        headers: { 'Content-Type': 'text/html;charset=UTF-8' },
      });
    }

    let config;
    try {
      config = JSON.parse(configRaw);
    } catch {
      return new Response('Configuration error', { status: 500 });
    }

    // Route
    if (request.method === 'POST' && path === '/submit') {
      return handleFormSubmit(request, config, env);
    }
    if (path === '/privacy') {
      return htmlResponse(privacyPage(config));
    }
    if (path === '/terms') {
      return htmlResponse(termsPage(config));
    }
    // Default: landing page
    return htmlResponse(landingPage(config, hostname));
  },
};

function htmlResponse(html) {
  return new Response(html, {
    headers: {
      'Content-Type': 'text/html;charset=UTF-8',
      'Cache-Control': 'public, max-age=3600',
      'X-Content-Type-Options': 'nosniff',
      'X-Frame-Options': 'DENY',
    },
  });
}

// ─── Contact Form Handler ────────────────────────────────────

async function handleFormSubmit(request, config, env) {
  try {
    const formData = await request.formData();

    // Honeypot check
    if (formData.get('website_url')) {
      // Bot detected — silently accept
      return Response.redirect(new URL('/?thanks=1', request.url).toString(), 303);
    }

    // Extract fields
    const firstName = (formData.get('first_name') || '').trim();
    const lastName = (formData.get('last_name') || '').trim();
    const phone = (formData.get('phone') || '').trim();
    const email = (formData.get('email') || '').trim();
    const smsConsent = formData.get('sms_consent') === 'on';

    if (!firstName || !lastName || !phone) {
      return htmlResponse(landingPage(config, new URL(request.url).hostname, 'Please fill in all required fields.'));
    }

    if (!smsConsent) {
      return htmlResponse(landingPage(config, new URL(request.url).hostname, 'Please agree to receive text messages to submit.'));
    }

    // Forward to Flask API
    const apiUrl = `${env.API_BASE_URL || 'https://app.insurancegrokbot.click'}/api/domain/contact-form`;
    const payload = {
      location_id: config.location_id,
      first_name: firstName,
      last_name: lastName,
      phone: phone,
      email: email,
      sms_consent: smsConsent,
      consent_text: consentText(config.dba_name),
      consent_timestamp: new Date().toISOString(),
      consent_ip: request.headers.get('CF-Connecting-IP') || '',
      consent_page: request.url,
      source: 'web_form',
      domain: new URL(request.url).hostname,
    };

    const apiResp = await fetch(apiUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    // Redirect to thank you regardless (don't leak API errors to leads)
    return Response.redirect(new URL('/?thanks=1', request.url).toString(), 303);

  } catch (e) {
    return Response.redirect(new URL('/?thanks=1', request.url).toString(), 303);
  }
}

function consentText(businessName) {
  return `I agree to receive recurring automated text messages from ${businessName} at the phone number provided. Consent is not a condition of purchase. Msg & data rates may apply. Msg frequency varies. Reply STOP to cancel. Reply HELP for help.`;
}

// ─── Landing Page ────────────────────────────────────────────

function landingPage(config, hostname, errorMsg = '') {
  const c = config;
  const showThanks = false; // handled via ?thanks=1 query param in client JS
  const statesBadges = (c.licensed_states || [])
    .map(s => `<span class="state-badge">${esc(s)}</span>`)
    .join('');

  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>${esc(c.agent_name)} — ${esc(c.dba_name)}</title>
  <meta name="description" content="${esc(c.agent_name)}, licensed life insurance agent at ${esc(c.dba_name)}. Get a free quote today.">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,300;0,9..40,400;0,9..40,500;0,9..40,600;0,9..40,700&family=Playfair+Display:wght@600;700;800&display=swap" rel="stylesheet">
  <style>${landingStyles(c)}</style>
</head>
<body>

  <!-- Hero -->
  <header class="hero">
    <div class="hero-inner">
      <div class="hero-text">
        <p class="hero-eyebrow">${esc(c.dba_name)}</p>
        <h1 class="hero-title">${esc(c.agent_name)}</h1>
        <p class="hero-subtitle">Licensed Life Insurance Professional</p>
        ${statesBadges ? `<div class="states-row">${statesBadges}</div>` : ''}
        <a href="tel:${esc(c.phone_raw || c.phone_display || '')}" class="hero-cta">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45 12.84 12.84 0 0 0 2.81.7A2 2 0 0 1 22 16.92z"></path></svg>
          Call ${esc(c.phone_display || '')}
        </a>
      </div>
    </div>
  </header>

  <!-- About -->
  ${c.bio ? `
  <section class="about">
    <div class="container">
      <h2>About</h2>
      <p>${esc(c.bio)}</p>
    </div>
  </section>` : ''}

  <!-- Contact Form -->
  <section class="contact" id="contact">
    <div class="container">
      <h2>Get a Free Quote</h2>
      <p class="contact-sub">Fill out the form below and I'll reach out to discuss your coverage options.</p>

      <div id="thankYou" class="thank-you" style="display:none;">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path><polyline points="22 4 12 14.01 9 11.01"></polyline></svg>
        <h3>Thank you!</h3>
        <p>I'll be in touch shortly to discuss your coverage options.</p>
      </div>

      <form id="contactForm" method="POST" action="/submit" class="lead-form">
        ${errorMsg ? `<div class="form-error">${esc(errorMsg)}</div>` : ''}

        <div class="form-row">
          <div class="form-group">
            <label for="first_name">First Name <span class="req">*</span></label>
            <input type="text" id="first_name" name="first_name" required autocomplete="given-name" placeholder="John">
          </div>
          <div class="form-group">
            <label for="last_name">Last Name <span class="req">*</span></label>
            <input type="text" id="last_name" name="last_name" required autocomplete="family-name" placeholder="Smith">
          </div>
        </div>

        <div class="form-group">
          <label for="phone">Phone Number <span class="req">*</span></label>
          <input type="tel" id="phone" name="phone" required autocomplete="tel" placeholder="(555) 123-4567">
        </div>

        <div class="form-group">
          <label for="email">Email <span class="optional">(optional)</span></label>
          <input type="email" id="email" name="email" autocomplete="email" placeholder="john@example.com">
        </div>

        <!-- Honeypot -->
        <div style="position:absolute;left:-9999px;top:-9999px;"><input type="text" name="website_url" tabindex="-1" autocomplete="off"></div>

        <div class="consent-box">
          <label class="consent-label">
            <input type="checkbox" name="sms_consent" required>
            <span class="consent-text">I agree to receive recurring automated text messages from <strong>${esc(c.dba_name)}</strong> at the phone number provided. Consent is not a condition of purchase. Msg &amp; data rates may apply. Msg frequency varies. Reply STOP to cancel. Reply HELP for help. <a href="/privacy">Privacy Policy</a> &amp; <a href="/terms">Terms</a>.</span>
          </label>
        </div>

        <button type="submit" class="submit-btn">Get My Free Quote</button>
      </form>
    </div>
  </section>

  <!-- Footer -->
  <footer class="footer">
    <div class="container footer-inner">
      <div class="footer-left">
        <p class="footer-business">${esc(c.dba_name)}</p>
        <p class="footer-agent">${esc(c.agent_name)} &mdash; Licensed Life Insurance Professional</p>
      </div>
      <div class="footer-links">
        <a href="/privacy">Privacy Policy</a>
        <a href="/terms">Terms of Service</a>
      </div>
      <div class="footer-powered">
        Powered by <a href="https://omnisconn.com" target="_blank" rel="noopener">Omnisconn</a>
      </div>
    </div>
  </footer>

  <script>
    // Show thank you message if redirected after form submit
    if (new URLSearchParams(window.location.search).get('thanks') === '1') {
      var form = document.getElementById('contactForm');
      var ty = document.getElementById('thankYou');
      if (form) form.style.display = 'none';
      if (ty) ty.style.display = 'flex';
      history.replaceState({}, '', '/');
    }
  </script>

</body>
</html>`;
}

// ─── Privacy Policy ──────────────────────────────────────────

function privacyPage(config) {
  const c = config;
  const today = new Date().toLocaleDateString('en-US', { year: 'numeric', month: 'long', day: 'numeric' });

  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Privacy Policy — ${esc(c.dba_name)}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=DM+Sans:opsz,wght@9..40,400;9..40,500;9..40,600&display=swap" rel="stylesheet">
  <style>${legalStyles(c)}</style>
</head>
<body>
  <nav class="legal-nav">
    <a href="/">&larr; Back to ${esc(c.dba_name)}</a>
  </nav>
  <main class="legal-content">
    <h1>Privacy Policy</h1>
    <p class="effective">Effective Date: ${today}</p>

    <p>${esc(c.dba_name)} ("we," "us," or "our") respects your privacy. This policy describes how we collect, use, and protect your information.</p>

    <h2>Information We Collect</h2>
    <p>When you visit our website, submit a form, or opt in to receive text messages, we may collect:</p>
    <ul>
      <li>Your name, phone number, and email address</li>
      <li>Your consent to receive text messages</li>
      <li>Technical data such as IP address, browser type, and pages visited</li>
    </ul>

    <h2>How We Use Your Information</h2>
    <p>We use the information you provide to:</p>
    <ul>
      <li>Respond to your inquiry about life insurance coverage</li>
      <li>Send you recurring automated text messages regarding insurance quotes, policy information, and appointment reminders</li>
      <li>Contact you by phone to discuss coverage options</li>
    </ul>

    <h2>SMS/Text Messaging</h2>
    <p>By providing your phone number and agreeing to receive text messages, you expressly consent to receive recurring automated text messages from ${esc(c.dba_name)} at the number provided. Message frequency varies.</p>

    <h3>Opt-Out</h3>
    <p>You may opt out of receiving text messages at any time by replying <strong>STOP</strong> to any message. After you send STOP, you will receive one final confirmation message, and no further text messages will be sent. To opt back in, you may submit a new request through our website.</p>

    <h3>Help</h3>
    <p>For help or questions about our messaging program, reply <strong>HELP</strong> to any message, or contact us at ${esc(c.phone_display || '')} or ${esc(c.email || '')}.</p>

    <h3>Message and Data Rates</h3>
    <p>Message and data rates may apply. Please check with your mobile carrier for details about your text and data plan.</p>

    <h3>Carriers</h3>
    <p>Carriers are not liable for delayed or undelivered messages.</p>

    <h3>Consent</h3>
    <p>Consent to receive text messages is not a condition of purchasing any goods or services.</p>

    <h2>Data Sharing</h2>
    <p>We will <strong>not</strong> sell, rent, or share your phone number or personal information collected through our SMS program with third parties or affiliates for their own marketing purposes. We may share your information with service providers who assist in delivering our messages, solely for that purpose.</p>

    <h2>Data Retention</h2>
    <p>We retain your opt-in consent records (phone number, timestamp, consent language) for as long as you are opted in, plus four (4) years after you opt out, as required by applicable law.</p>

    <h2>Your Rights</h2>
    <p>Depending on your location, you may have the right to access, correct, or delete your personal information. To exercise these rights, contact us using the information below.</p>

    <h2>Contact Us</h2>
    <p>${esc(c.dba_name)}<br>
    ${esc(c.agent_name)}<br>
    ${esc(c.phone_display || '')}<br>
    ${esc(c.email || '')}</p>

    <h2>Changes to This Policy</h2>
    <p>We may update this privacy policy from time to time. Changes will be posted on this page with an updated effective date.</p>
  </main>
  <footer class="legal-footer">
    <a href="/privacy">Privacy Policy</a> &middot; <a href="/terms">Terms of Service</a> &middot; <a href="/">Home</a>
    <p>Powered by <a href="https://omnisconn.com" target="_blank" rel="noopener">Omnisconn</a></p>
  </footer>
</body>
</html>`;
}

// ─── Terms of Service ────────────────────────────────────────

function termsPage(config) {
  const c = config;
  const today = new Date().toLocaleDateString('en-US', { year: 'numeric', month: 'long', day: 'numeric' });

  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Terms of Service — ${esc(c.dba_name)}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=DM+Sans:opsz,wght@9..40,400;9..40,500;9..40,600&display=swap" rel="stylesheet">
  <style>${legalStyles(c)}</style>
</head>
<body>
  <nav class="legal-nav">
    <a href="/">&larr; Back to ${esc(c.dba_name)}</a>
  </nav>
  <main class="legal-content">
    <h1>Terms of Service</h1>
    <p class="effective">Effective Date: ${today}</p>

    <h2>SMS/Text Messaging Program</h2>
    <p>By opting in to receive text messages from ${esc(c.dba_name)}, you agree to the following terms:</p>

    <h3>Program Description</h3>
    <p>${esc(c.dba_name)} offers a text messaging program that provides insurance quotes, policy information, appointment reminders, and related communications to opted-in users.</p>

    <h3>Opt-In</h3>
    <p>You may opt in to our messaging program by submitting a form on our website or providing your phone number during a consultation. By opting in, you consent to receive recurring automated text messages from ${esc(c.dba_name)}. You do not need to opt in to SMS to purchase any products or services.</p>

    <h3>Message Frequency</h3>
    <p>Message frequency varies based on your interactions with us.</p>

    <h3>Costs</h3>
    <p>Message and data rates may apply. ${esc(c.dba_name)} does not charge for text messages, but your carrier's standard messaging rates may apply.</p>

    <h3>Opt-Out</h3>
    <p>To stop receiving messages, reply <strong>STOP</strong> to any text message from us. You will receive a one-time confirmation that you have been unsubscribed. No further messages will be sent unless you re-opt in.</p>

    <h3>Help</h3>
    <p>For support, reply <strong>HELP</strong> to any message, or contact us at ${esc(c.phone_display || '')} or ${esc(c.email || '')}.</p>

    <h3>Carrier Liability</h3>
    <p>${esc(c.dba_name)} and mobile carriers are not liable for delayed or undelivered messages.</p>

    <h2>Website Terms</h2>
    <p>This website is provided by ${esc(c.dba_name)} for informational purposes. The information on this site does not constitute financial or legal advice. Insurance products are subject to underwriting approval.</p>

    <h2>Privacy</h2>
    <p>Your information is handled in accordance with our <a href="/privacy">Privacy Policy</a>.</p>

    <h2>Limitation of Liability</h2>
    <p>This website and all content are provided "as is" without warranties of any kind. ${esc(c.dba_name)} shall not be liable for any damages arising from the use of this website or our messaging services.</p>

    <h2>Modifications</h2>
    <p>We reserve the right to modify these terms at any time. Continued participation in the messaging program after changes constitutes acceptance of the updated terms.</p>

    <h2>Contact</h2>
    <p>${esc(c.dba_name)}<br>
    ${esc(c.agent_name)}<br>
    ${esc(c.phone_display || '')}<br>
    ${esc(c.email || '')}</p>
  </main>
  <footer class="legal-footer">
    <a href="/privacy">Privacy Policy</a> &middot; <a href="/terms">Terms of Service</a> &middot; <a href="/">Home</a>
    <p>Powered by <a href="https://omnisconn.com" target="_blank" rel="noopener">Omnisconn</a></p>
  </footer>
</body>
</html>`;
}

// ─── 404 Page ────────────────────────────────────────────────

function notFoundPage(hostname) {
  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Page Not Found</title>
  <style>
    body { font-family: 'DM Sans', sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; background: #fafafa; color: #333; }
    .msg { text-align: center; max-width: 400px; padding: 2rem; }
    h1 { font-size: 1.5rem; margin-bottom: 0.5rem; }
    p { color: #666; }
  </style>
</head>
<body>
  <div class="msg">
    <h1>This page isn't available</h1>
    <p>The site at ${esc(hostname)} hasn't been set up yet.</p>
  </div>
</body>
</html>`;
}

// ─── Styles ──────────────────────────────────────────────────

function landingStyles(config) {
  const accent = config.accent_color || '#1a6b4a';
  const accentLight = config.accent_light || '#e8f5ee';

  return `
    :root {
      --accent: ${accent};
      --accent-light: ${accentLight};
      --text: #1a1a1a;
      --text-secondary: #555;
      --bg: #ffffff;
      --bg-warm: #faf9f7;
      --border: #e5e2dd;
      --radius: 6px;
    }

    * { margin: 0; padding: 0; box-sizing: border-box; }

    body {
      font-family: 'DM Sans', -apple-system, BlinkMacSystemFont, sans-serif;
      color: var(--text);
      background: var(--bg);
      line-height: 1.6;
      -webkit-font-smoothing: antialiased;
    }

    .container { max-width: 680px; margin: 0 auto; padding: 0 1.5rem; }

    /* ── Hero ── */
    .hero {
      background: var(--bg-warm);
      border-bottom: 1px solid var(--border);
      padding: 4rem 1.5rem 3.5rem;
    }
    .hero-inner { max-width: 680px; margin: 0 auto; }
    .hero-text { }
    .hero-eyebrow {
      font-size: 0.8rem;
      font-weight: 600;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--accent);
      margin-bottom: 0.75rem;
    }
    .hero-title {
      font-family: 'Playfair Display', Georgia, serif;
      font-size: clamp(2.2rem, 5vw, 3.2rem);
      font-weight: 700;
      line-height: 1.15;
      color: var(--text);
      margin-bottom: 0.5rem;
      letter-spacing: -0.01em;
    }
    .hero-subtitle {
      font-size: 1.1rem;
      color: var(--text-secondary);
      margin-bottom: 1.5rem;
      font-weight: 400;
    }
    .states-row { display: flex; flex-wrap: wrap; gap: 0.4rem; margin-bottom: 1.75rem; }
    .state-badge {
      display: inline-block;
      padding: 0.2rem 0.6rem;
      background: var(--accent-light);
      color: var(--accent);
      font-size: 0.75rem;
      font-weight: 600;
      border-radius: 3px;
      letter-spacing: 0.04em;
    }
    .hero-cta {
      display: inline-flex;
      align-items: center;
      gap: 0.5rem;
      padding: 0.8rem 1.6rem;
      background: var(--accent);
      color: #fff;
      text-decoration: none;
      font-weight: 600;
      font-size: 0.95rem;
      border-radius: var(--radius);
      transition: opacity 0.15s;
    }
    .hero-cta:hover { opacity: 0.9; }

    /* ── About ── */
    .about { padding: 3rem 0; border-bottom: 1px solid var(--border); }
    .about h2 {
      font-family: 'Playfair Display', Georgia, serif;
      font-size: 1.5rem;
      margin-bottom: 0.75rem;
    }
    .about p { color: var(--text-secondary); font-size: 1rem; }

    /* ── Contact Form ── */
    .contact { padding: 3rem 0 4rem; }
    .contact h2 {
      font-family: 'Playfair Display', Georgia, serif;
      font-size: 1.5rem;
      margin-bottom: 0.25rem;
    }
    .contact-sub { color: var(--text-secondary); margin-bottom: 2rem; font-size: 0.95rem; }

    .lead-form { max-width: 520px; }
    .form-row { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
    .form-group { margin-bottom: 1.25rem; }
    .form-group label {
      display: block;
      font-size: 0.85rem;
      font-weight: 500;
      color: var(--text);
      margin-bottom: 0.35rem;
    }
    .req { color: var(--accent); }
    .optional { color: #999; font-weight: 400; font-size: 0.8rem; }
    .form-group input {
      width: 100%;
      padding: 0.7rem 0.85rem;
      border: 1px solid var(--border);
      border-radius: var(--radius);
      font-size: 0.95rem;
      font-family: inherit;
      color: var(--text);
      background: var(--bg);
      transition: border-color 0.15s;
    }
    .form-group input:focus {
      outline: none;
      border-color: var(--accent);
      box-shadow: 0 0 0 3px var(--accent-light);
    }
    .form-group input::placeholder { color: #bbb; }

    .consent-box { margin: 1.5rem 0; }
    .consent-label {
      display: flex;
      gap: 0.6rem;
      align-items: flex-start;
      cursor: pointer;
    }
    .consent-label input[type="checkbox"] {
      margin-top: 0.25rem;
      flex-shrink: 0;
      width: 16px;
      height: 16px;
      accent-color: var(--accent);
    }
    .consent-text {
      font-size: 0.78rem;
      color: var(--text-secondary);
      line-height: 1.5;
    }
    .consent-text a { color: var(--accent); }
    .consent-text strong { color: var(--text); font-weight: 600; }

    .submit-btn {
      width: 100%;
      padding: 0.85rem;
      background: var(--accent);
      color: #fff;
      border: none;
      border-radius: var(--radius);
      font-size: 1rem;
      font-weight: 600;
      font-family: inherit;
      cursor: pointer;
      transition: opacity 0.15s;
    }
    .submit-btn:hover { opacity: 0.9; }

    .form-error {
      background: #fef2f2;
      border: 1px solid #fecaca;
      color: #991b1b;
      padding: 0.75rem 1rem;
      border-radius: var(--radius);
      font-size: 0.85rem;
      margin-bottom: 1.25rem;
    }

    .thank-you {
      display: flex;
      flex-direction: column;
      align-items: center;
      text-align: center;
      padding: 3rem 0;
      color: var(--accent);
    }
    .thank-you h3 { font-size: 1.5rem; margin: 1rem 0 0.5rem; color: var(--text); }
    .thank-you p { color: var(--text-secondary); }

    /* ── Footer ── */
    .footer {
      background: var(--bg-warm);
      border-top: 1px solid var(--border);
      padding: 2rem 1.5rem;
    }
    .footer-inner {
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
      gap: 1rem;
    }
    .footer-business { font-weight: 600; font-size: 0.9rem; }
    .footer-agent { font-size: 0.8rem; color: var(--text-secondary); }
    .footer-links { display: flex; gap: 1.25rem; }
    .footer-links a { font-size: 0.8rem; color: var(--text-secondary); text-decoration: none; }
    .footer-links a:hover { color: var(--accent); }
    .footer-powered { font-size: 0.72rem; color: #aaa; }
    .footer-powered a { color: #999; text-decoration: none; }
    .footer-powered a:hover { color: var(--accent); }

    /* ── Responsive ── */
    @media (max-width: 600px) {
      .hero { padding: 3rem 1.25rem 2.5rem; }
      .form-row { grid-template-columns: 1fr; }
      .footer-inner { flex-direction: column; align-items: flex-start; }
    }
  `;
}

function legalStyles(config) {
  const accent = config.accent_color || '#1a6b4a';
  return `
    :root { --accent: ${accent}; }
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
      font-family: 'DM Sans', -apple-system, BlinkMacSystemFont, sans-serif;
      color: #1a1a1a;
      background: #fff;
      line-height: 1.7;
      -webkit-font-smoothing: antialiased;
    }
    .legal-nav {
      padding: 1rem 1.5rem;
      border-bottom: 1px solid #e5e2dd;
    }
    .legal-nav a {
      font-size: 0.85rem;
      color: var(--accent);
      text-decoration: none;
      font-weight: 500;
    }
    .legal-content {
      max-width: 680px;
      margin: 0 auto;
      padding: 2.5rem 1.5rem 4rem;
    }
    .legal-content h1 {
      font-size: 1.8rem;
      font-weight: 700;
      margin-bottom: 0.25rem;
    }
    .effective {
      color: #888;
      font-size: 0.85rem;
      margin-bottom: 2rem;
    }
    .legal-content h2 {
      font-size: 1.15rem;
      font-weight: 600;
      margin-top: 2rem;
      margin-bottom: 0.5rem;
      color: #111;
    }
    .legal-content h3 {
      font-size: 1rem;
      font-weight: 600;
      margin-top: 1.25rem;
      margin-bottom: 0.35rem;
    }
    .legal-content p { margin-bottom: 0.75rem; color: #444; font-size: 0.92rem; }
    .legal-content ul { margin: 0.5rem 0 0.75rem 1.5rem; color: #444; font-size: 0.92rem; }
    .legal-content li { margin-bottom: 0.3rem; }
    .legal-content a { color: var(--accent); }
    .legal-content strong { color: #111; }
    .legal-footer {
      max-width: 680px;
      margin: 0 auto;
      padding: 1.5rem;
      border-top: 1px solid #e5e2dd;
      font-size: 0.8rem;
      color: #999;
      text-align: center;
    }
    .legal-footer a { color: #888; text-decoration: none; }
    .legal-footer a:hover { color: var(--accent); }
    .legal-footer p { margin-top: 0.5rem; }
  `;
}

// ─── Utilities ───────────────────────────────────────────────

function esc(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
