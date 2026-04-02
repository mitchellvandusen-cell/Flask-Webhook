/**
 * Omnisconn Email Handler — Cloudflare Email Worker
 *
 * Intercepts inbound email on agent domains.
 * - Twilio verification emails → auto-reply via Flask API (Mailgun)
 * - Everything else → forward to agent's personal email
 */

export default {
  async email(message, env, ctx) {
    const from = message.from || '';
    const to = message.to || '';
    const subject = message.headers.get('subject') || '';

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
        const apiUrl = `${env.API_BASE_URL || 'https://app.insurancegrokbot.click'}/api/domain/auto-reply`;
        await fetch(apiUrl, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            from_email: from,
            to_email: to,
            subject: subject,
            body_preview: body.substring(0, 2000),
            domain: to.split('@')[1] || '',
          }),
        });
      } catch (e) {
        // Log but don't fail — still forward the email
        console.error('Auto-reply API call failed:', e);
      }
    }

    // Always forward to the agent's personal email
    // The forwarding destination is configured in Cloudflare Email Routing
    // This worker just handles the Twilio interception before forwarding
    await message.forward(env.FORWARD_TO || message.to);
  },
};
