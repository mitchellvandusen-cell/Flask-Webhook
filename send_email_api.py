#!/usr/bin/env python3
"""
Mailgun API Email Sender (Alternative to SMTP)
More reliable for production use
Usage: python send_email_api.py recipient@example.com
"""
import sys
import os
import logging
import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

def send_email_via_api(to_email, subject="Test Email", html_body="", text_body=""):
    """Send email using Mailgun API (more reliable than SMTP)"""

    api_key = os.getenv('MAILGUN_API_KEY')
    domain = os.getenv('MAILGUN_DOMAIN')
    from_email = os.getenv('MAIL_DEFAULT_SENDER')

    if not all([api_key, domain, from_email]):
        logger.error("Missing Mailgun config: MAILGUN_API_KEY=%s MAILGUN_DOMAIN=%s MAIL_DEFAULT_SENDER=%s",
                      bool(api_key), bool(domain), bool(from_email))
        return False

    try:
        response = requests.post(
            f"https://api.mailgun.net/v3/{domain}/messages",
            auth=("api", api_key),
            data={
                "from": f"InsuranceGrokBot <{from_email}>",
                "to": to_email,
                "subject": subject,
                "text": text_body or "Test email sent successfully via Mailgun API!",
                "html": html_body or """
                <html>
                <body style="font-family: Arial, sans-serif; padding: 20px;">
                    <h2 style="color: #2563eb;">Mailgun API Test Successful!</h2>
                    <p>Your Mailgun API configuration is working correctly.</p>
                    <p><strong>From:</strong> {from_email}</p>
                    <p><strong>Domain:</strong> {domain}</p>
                    <hr style="border: 1px solid #eee; margin: 20px 0;">
                    <p style="color: #666; font-size: 14px;">
                        This is a test email from InsuranceGrokBot's sub-user onboarding system.
                    </p>
                </body>
                </html>
                """.replace("{from_email}", from_email).replace("{domain}", domain)
            },
            timeout=10
        )

        if response.status_code == 200:
            logger.info("Email sent to %s via Mailgun API", to_email)
            return True
        else:
            logger.error("Mailgun API failed for %s: status=%d response=%s",
                         to_email, response.status_code, response.text)
            return False

    except Exception as e:
        logger.error("Mailgun API exception for %s: %s", to_email, e)
        return False

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("\n❌ Usage: python send_email_api.py your-email@example.com\n")
        sys.exit(1)

    to_email = sys.argv[1]
    success = send_email_via_api(
        to_email=to_email,
        subject="🎉 Mailgun API Test - InsuranceGrokBot",
    )
    sys.exit(0 if success else 1)
