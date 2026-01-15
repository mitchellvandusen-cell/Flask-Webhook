#!/usr/bin/env python3
"""
Mailgun API Email Sender (Alternative to SMTP)
More reliable for production use
Usage: python send_email_api.py recipient@example.com
"""
import sys
import os
import requests
from dotenv import load_dotenv

load_dotenv()

def send_email_via_api(to_email, subject="Test Email", html_body="", text_body=""):
    """Send email using Mailgun API (more reliable than SMTP)"""

    api_key = os.getenv('MAILGUN_API_KEY')
    domain = os.getenv('MAILGUN_DOMAIN')
    from_email = os.getenv('MAIL_DEFAULT_SENDER')

    if not all([api_key, domain, from_email]):
        print("❌ ERROR: Missing Mailgun configuration")
        print("   Required: MAILGUN_API_KEY, MAILGUN_DOMAIN, MAIL_DEFAULT_SENDER")
        return False

    print(f"\n🔧 Mailgun API Configuration:")
    print(f"   Domain: {domain}")
    print(f"   From: {from_email}")
    print(f"   To: {to_email}")
    print(f"   API Key: {api_key[:10]}...{api_key[-6:]}")
    print(f"\n📤 Sending via Mailgun API...\n")

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
                    <h2 style="color: #2563eb;">✅ Mailgun API Test Successful!</h2>
                    <p>Your Mailgun API configuration is working correctly.</p>
                    <p><strong>From:</strong> {from_email}</p>
                    <p><strong>Domain:</strong> {domain}</p>
                    <hr style="border: 1px solid #eee; margin: 20px 0;">
                    <p style="color: #666; font-size: 14px;">
                        This is a test email from InsuranceGrokBot's sub-user onboarding system.
                        You're ready to send invitation emails! 🚀
                    </p>
                </body>
                </html>
                """.replace("{from_email}", from_email).replace("{domain}", domain)
            },
            timeout=10
        )

        if response.status_code == 200:
            print("✅ SUCCESS! Email sent via Mailgun API!")
            print(f"📬 Check {to_email} for the test email.")
            print(f"\n📋 Mailgun Response:")
            print(f"   {response.json()}")
            print("\n💡 Next Steps:")
            print("   1. Check your inbox (and spam folder)")
            print("   2. If received, your onboarding system is ready!")
            print("   3. Test the full invite flow from /agency-dashboard\n")
            return True
        else:
            print(f"❌ ERROR: API request failed")
            print(f"   Status Code: {response.status_code}")
            print(f"   Response: {response.text}")
            print("\n🔍 Troubleshooting:")
            print("   1. Verify domain is verified in Mailgun dashboard")
            print("   2. Check API key is correct")
            print("   3. Ensure domain DNS records are configured")
            print("   4. Check Mailgun logs: https://app.mailgun.com/logs\n")
            return False

    except Exception as e:
        print(f"❌ ERROR: {str(e)}")
        print("\n🔍 Possible Issues:")
        print("   1. Network connectivity")
        print("   2. Invalid API credentials")
        print("   3. Domain not verified in Mailgun\n")
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
