#!/usr/bin/env python3
"""
Test script for Mailgun email configuration
Usage: python test_email.py your-test-email@example.com
"""
import sys
import os
from dotenv import load_dotenv
from flask import Flask
from flask_mail import Mail, Message

# Load environment variables
load_dotenv()

# Create minimal Flask app
app = Flask(__name__)

# Configure Flask-Mail
app.config['MAIL_SERVER'] = os.getenv('MAIL_SERVER')
app.config['MAIL_PORT'] = int(os.getenv('MAIL_PORT', '587'))
app.config['MAIL_USE_TLS'] = os.getenv('MAIL_USE_TLS', 'True').lower() == 'true'
app.config['MAIL_USE_SSL'] = os.getenv('MAIL_USE_SSL', 'False').lower() == 'true'
app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.getenv('MAIL_DEFAULT_SENDER')

mail = Mail(app)

def send_test_email(to_email):
    """Send a test email to verify configuration"""

    print(f"\n🔧 Email Configuration:")
    print(f"   Server: {app.config['MAIL_SERVER']}")
    print(f"   Port: {app.config['MAIL_PORT']}")
    print(f"   From: {app.config['MAIL_DEFAULT_SENDER']}")
    print(f"   To: {to_email}")
    print(f"\n📤 Sending test email...\n")

    with app.app_context():
        try:
            msg = Message(
                subject="🎉 Mailgun Test - InsuranceGrokBot",
                recipients=[to_email],
                html="""
                <html>
                <body style="font-family: Arial, sans-serif; padding: 20px;">
                    <h2 style="color: #2563eb;">✅ Email Configuration Successful!</h2>
                    <p>Your Mailgun SMTP configuration is working correctly.</p>
                    <p><strong>From:</strong> support@insurancegrokbot.com</p>
                    <p><strong>Server:</strong> smtp.mailgun.org</p>
                    <hr style="border: 1px solid #eee; margin: 20px 0;">
                    <p style="color: #666; font-size: 14px;">
                        This is a test email from InsuranceGrokBot's sub-user onboarding system.
                        You're ready to send invitation emails! 🚀
                    </p>
                </body>
                </html>
                """,
                body="""
✅ Email Configuration Successful!

Your Mailgun SMTP configuration is working correctly.

From: support@insurancegrokbot.com
Server: smtp.mailgun.org

This is a test email from InsuranceGrokBot's sub-user onboarding system.
You're ready to send invitation emails! 🚀
                """
            )

            mail.send(msg)
            print("✅ SUCCESS! Email sent successfully!")
            print(f"📬 Check {to_email} for the test email.")
            print("\n💡 Next Steps:")
            print("   1. Check your inbox (and spam folder)")
            print("   2. If received, your onboarding system is ready!")
            print("   3. Test the full invite flow from /agency-dashboard\n")
            return True

        except Exception as e:
            print(f"❌ ERROR: Failed to send email")
            print(f"   {str(e)}")
            print("\n🔍 Troubleshooting:")
            print("   1. Verify Mailgun domain is verified in dashboard")
            print("   2. Check DNS records are properly configured")
            print("   3. Confirm SMTP credentials are correct")
            print("   4. Check Mailgun logs at https://app.mailgun.com/logs\n")
            return False

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("\n❌ Usage: python test_email.py your-email@example.com\n")
        sys.exit(1)

    to_email = sys.argv[1]
    success = send_test_email(to_email)
    sys.exit(0 if success else 1)
