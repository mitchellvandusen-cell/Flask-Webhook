/**
 * Omnisconn Email Handler — Cloudflare Email Worker
 *
 * Multi-tenant email worker. Routes through Workers KV to find the
 * forward_to address per domain (stored as key "email:{domain}").
 *
 * Flow:
 *   1. Extract domain from recipient address
 *   2. Look up forward_to in KV (key: "email:{domain}")
 *   3. If from Twilio → auto-reply via Flask API (Mailgun)
 *   4. Forward everything to the agent's personal email
 */

export default {
  async email(message, env, ctx) {
    const from = message.from || '';
    const to = message.to || '';
    const subject = message.headers.get('subject') || '';
    const domain = to.split('@')[1] || '';

    // Look up forward_to from KV (multi-tenant: each domain has its own destination)
    let forwardTo = '';
    if (env.AGENT_KV && domain) {
      try {
        const raw = await env.AGENT_KV.get(`email:${domain}`);
        if (raw) {
          const config = JSON.parse(raw);
          forwardTo = config.forward_to || '';
        }
      } catch (e) {
        console.error(`KV lookup failed for email:${domain}:`, e);
      }
    }
    // Fallback chain: KV → env → original recipient
    if (!forwardTo) forwardTo = env.FORWARD_TO || to;

    // Check if this is from Twilio (verification email)
    const isTwilio = from.includes('twilio.com') ||
                     from.includes('sendgrid.net') ||
                     subject.toLowerCase().includes('verify') ||
                     subject.toLowerCase().includes('confirmation');

    if (isTwilio) {
      // Read the email body for context
      let body = '';
      try {
        const reader = message.raw.getReader();
        const chunks = [];
        let done = false;
        while (!done) {
          const { value, done: d } = await reader.read();
          if (value) chunks.push(value);
          done = d;
        }
        body = new TextDecoder().decode(
          new Uint8Array(chunks.reduce((acc, chunk) => [...acc, ...chunk], []))
        );
      } catch {
        body = '';
      }

      // Call our API to auto-reply via Mailgun
      try {
        const apiUrl = `${env.API_BASE_URL || 'https://app.omnisconn.com'}/api/domain/auto-reply`;
        await fetch(apiUrl, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Authorization': `Bearer ${env.CRON_SECRET || ''}`,
          },
          body: JSON.stringify({
            from_email: from,
            to_email: to,
            subject: subject,
            body_preview: body.substring(0, 2000),
            domain: domain,
          }),
        });
      } catch (e) {
        console.error('Auto-reply API call failed:', e);
      }
    }

    // Forward to agent's personal email
    await message.forward(forwardTo);
  },
};
