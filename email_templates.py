# email_templates.py — Premium HTML email builder functions for InsuranceGrokBot
#
# All functions here are pure: they take string parameters and return HTML strings.
# No Flask context, no database calls, no side effects.


def _email_wrapper(inner_html: str, domain_url: str) -> str:
    """Wrap email content in the premium dark-themed InsuranceGrokBot email shell."""
    return f'''<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin: 0; padding: 0; background-color: #0a0a0a; font-family: 'Segoe UI', Arial, sans-serif;">
<table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="background-color: #0a0a0a;">
<tr><td align="center" style="padding: 40px 20px;">

<!-- Main Card -->
<table cellpadding="0" cellspacing="0" width="600" style="max-width: 600px; width: 100%; background: linear-gradient(145deg, #141428 0%, #0d0d1a 100%); border-radius: 20px; border: 1px solid rgba(255,255,255,0.06); box-shadow: 0 20px 60px rgba(0,0,0,0.5);">

<!-- Header Bar -->
<tr>
<td style="padding: 0;">
    <div style="height: 4px; background: linear-gradient(90deg, #00c853, #00e676, #69f0ae, #00c853); border-radius: 20px 20px 0 0;"></div>
</td>
</tr>

<!-- Logo -->
<tr>
<td align="center" style="padding: 35px 40px 20px;">
    <table cellpadding="0" cellspacing="0"><tr>
        <td style="background: rgba(0,200,83,0.1); border: 1px solid rgba(0,200,83,0.2); border-radius: 14px; padding: 12px 24px;">
            <span style="font-size: 22px; font-weight: 800; color: #ffffff; letter-spacing: -0.5px;">Insurance<span style="color: #00c853;">Grok</span>Bot</span>
        </td>
    </tr></table>
</td>
</tr>

<!-- Content -->
{inner_html}

<!-- Footer -->
<tr>
<td style="padding: 30px 40px 35px; border-top: 1px solid rgba(255,255,255,0.05);">
    <table cellpadding="0" cellspacing="0" width="100%">
    <tr>
        <td align="center">
            <p style="margin: 0 0 12px; font-size: 13px; color: #555;">
                <a href="{domain_url}/support" style="color: #00c853; text-decoration: none;">Support</a>
                &nbsp;&nbsp;|&nbsp;&nbsp;
                <a href="{domain_url}/dashboard" style="color: #00c853; text-decoration: none;">Dashboard</a>
                &nbsp;&nbsp;|&nbsp;&nbsp;
                <a href="{domain_url}/terms" style="color: #00c853; text-decoration: none;">Terms</a>
            </p>
            <p style="margin: 0; font-size: 12px; color: #444;">
                InsuranceGrokBot &mdash; AI-Powered Insurance Sales Assistant
            </p>
        </td>
    </tr>
    </table>
</td>
</tr>

</table>
<!-- End Main Card -->

</td></tr></table>
</body>
</html>'''


def _build_setup_checklist_html(missing: list, domain_url: str, user_type: str) -> str:
    """Build a visual setup checklist showing what's done and what's remaining."""
    dashboard = f"{domain_url}/agency-dashboard" if user_type == "agency_owner" else f"{domain_url}/dashboard"
    steps = [
        ("account", "Create Account", dashboard),
        ("crm_connection", "Connect Your CRM", f"{domain_url}/oauth/initiate"),
        ("location_id", "Link Location", dashboard),
        ("calendar", "Set Up Calendar", dashboard),
        ("subscription", "Activate Subscription", dashboard),
    ]
    rows = ""
    for key, label, link in steps:
        is_missing = key in missing
        if key == "account":
            is_missing = False  # they have an account if they're getting this email
        if is_missing:
            rows += f'''
            <tr>
                <td style="padding: 12px 16px; border-bottom: 1px solid #f0f0f0; width: 40px; vertical-align: middle;">
                    <div style="width: 28px; height: 28px; border-radius: 50%; border: 2px solid #ff6b35; display: flex; align-items: center; justify-content: center;">
                        <span style="color: #ff6b35; font-size: 16px; font-weight: bold; line-height: 28px;">&bull;</span>
                    </div>
                </td>
                <td style="padding: 12px 0; border-bottom: 1px solid #f0f0f0; vertical-align: middle;">
                    <a href="{link}" style="color: #ff6b35; font-weight: 600; text-decoration: none; font-size: 15px;">{label}</a>
                    <span style="background: #fff3e0; color: #e65100; font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 4px; margin-left: 8px;">NEEDED</span>
                </td>
            </tr>'''
        else:
            rows += f'''
            <tr>
                <td style="padding: 12px 16px; border-bottom: 1px solid #f0f0f0; width: 40px; vertical-align: middle;">
                    <div style="width: 28px; height: 28px; border-radius: 50%; background: #00c853; display: flex; align-items: center; justify-content: center;">
                        <span style="color: white; font-size: 14px; font-weight: bold; line-height: 28px;">&#10003;</span>
                    </div>
                </td>
                <td style="padding: 12px 0; border-bottom: 1px solid #f0f0f0; vertical-align: middle;">
                    <span style="color: #888; font-size: 15px; text-decoration: line-through;">{label}</span>
                    <span style="color: #00c853; font-size: 11px; font-weight: 700; margin-left: 8px;">DONE</span>
                </td>
            </tr>'''
    return f'<table cellpadding="0" cellspacing="0" style="width: 100%;">{rows}</table>'


def _build_uninstall_feedback_email(name: str, domain_url: str, feedback_id: int) -> str:
    """Build farewell email asking for uninstall feedback with link to feedback page."""
    feedback_url = f"{domain_url}/uninstall-feedback?id={feedback_id}"
    inner = f'''
<tr>
<td style="padding: 0 40px 10px;">
    <h1 style="margin: 0 0 8px; font-size: 26px; font-weight: 800; color: #ffffff; line-height: 1.2;">
        We're sorry to see you go, {name}.
    </h1>
    <p style="margin: 0; font-size: 15px; color: #aaa; line-height: 1.6;">
        Your InsuranceGrokBot app has been uninstalled. We hope we served you well &mdash; and we'd love to know how we can improve.
    </p>
</td>
</tr>

<!-- Feedback CTA -->
<tr>
<td style="padding: 20px 40px 10px;">
    <div style="background: rgba(255,107,53,0.08); border: 1px solid rgba(255,107,53,0.2); border-radius: 14px; padding: 28px 24px; text-align: center;">
        <p style="margin: 0 0 6px; font-size: 18px; font-weight: 700; color: #ff6b35;">
            &#128172; Quick Feedback (30 seconds)
        </p>
        <p style="margin: 0 0 20px; font-size: 14px; color: #bbb; line-height: 1.5;">
            Your feedback helps us build a better product for insurance agents like you. Just pick a reason &mdash; it means a lot.
        </p>
        <a href="{feedback_url}" style="display: inline-block; padding: 14px 36px; background: linear-gradient(135deg, #ff6b35, #ff8f00); color: #ffffff; font-weight: 700; font-size: 16px; text-decoration: none; border-radius: 12px; letter-spacing: 0.3px;">
            Share Your Feedback
        </a>
    </div>
</td>
</tr>

<!-- Come back message -->
<tr>
<td style="padding: 20px 40px 10px;">
    <p style="margin: 0; font-size: 14px; color: #888; line-height: 1.7; text-align: center;">
        Changed your mind? You can always reinstall from the
        <a href="https://marketplace.gohighlevel.com/" style="color: #00c853; text-decoration: none; font-weight: 600;">GHL Marketplace</a>
        &mdash; your settings will be waiting.
    </p>
</td>
</tr>

<!-- Support -->
<tr>
<td style="padding: 10px 40px 10px;">
    <p style="margin: 0; font-size: 13px; color: #666; line-height: 1.6; text-align: center;">
        Questions? Reach us at <a href="mailto:support@insurancegrokbot.com" style="color: #00c853; text-decoration: none;">support@insurancegrokbot.com</a>
    </p>
</td>
</tr>
'''
    return _email_wrapper(inner, domain_url)


def _build_uninstall_admin_notification(location_id: str, company_id: str,
                                         user_email: str, user_name: str,
                                         feedback_id: int) -> str:
    """Build admin notification email about an uninstall event."""
    return f'''<html><body style="font-family: Arial, sans-serif; background: #f5f5f5; padding: 20px;">
<div style="max-width: 500px; margin: 0 auto; background: #fff; border-radius: 8px; padding: 24px; border-left: 4px solid #ff6b35;">
    <h2 style="margin: 0 0 12px; color: #333;">App Uninstalled</h2>
    <table style="width: 100%; font-size: 14px; color: #555;">
        <tr><td style="padding: 4px 0; font-weight: bold;">User:</td><td>{user_name or "Unknown"} ({user_email or "no email"})</td></tr>
        <tr><td style="padding: 4px 0; font-weight: bold;">Location ID:</td><td>{location_id or "N/A"}</td></tr>
        <tr><td style="padding: 4px 0; font-weight: bold;">Company ID:</td><td>{company_id or "N/A"}</td></tr>
        <tr><td style="padding: 4px 0; font-weight: bold;">Feedback ID:</td><td>#{feedback_id}</td></tr>
    </table>
    <p style="margin: 16px 0 0; font-size: 13px; color: #888;">Feedback email has been sent to the user (if email was available). You'll receive a follow-up when they submit their reason.</p>
</div>
</body></html>'''


def _build_install_welcome_email(name: str, domain_url: str) -> str:
    """Build premium welcome email for marketplace install — guides them through full setup flow."""
    inner = f'''
<tr>
<td style="padding: 0 40px 30px;">
    <h1 style="margin: 0 0 8px; font-size: 28px; font-weight: 800; color: #ffffff; line-height: 1.2;">
        Welcome aboard, {name}!
    </h1>
    <p style="margin: 0; font-size: 16px; color: #aaa; line-height: 1.5;">
        You just installed <strong style="color: #00c853;">InsuranceGrokBot</strong> &mdash; the all-in-one AI sales
        platform built for insurance agents. AI texting, voice AI, smart dialer, and lead intelligence &mdash; all in one place.
    </p>
</td>
</tr>

<!-- Setup Flow Notice -->
<tr>
<td style="padding: 0 40px 25px;">
    <table cellpadding="0" cellspacing="0" width="100%" style="background: rgba(255,107,53,0.08); border: 1px solid rgba(255,107,53,0.2); border-radius: 12px;">
    <tr>
    <td style="padding: 20px 24px;">
        <p style="margin: 0 0 4px; font-size: 15px; font-weight: 700; color: #ff6b35;">
            3 Quick Steps to Go Live
        </p>
        <p style="margin: 0; font-size: 14px; color: #ccc; line-height: 1.6;">
            <strong style="color: #fff;">1.</strong> Connect your CRM (click below)<br>
            <strong style="color: #fff;">2.</strong> Start free trial ($149.98/mo after 7 days &mdash; cancel anytime)<br>
            <strong style="color: #fff;">3.</strong> Set your password &amp; configure your dashboard
        </p>
        <p style="margin: 8px 0 0; font-size: 13px; color: #999;">
            The whole process takes about 2 minutes. Let's start with step 1:
        </p>
    </td>
    </tr>
    </table>
</td>
</tr>

<!-- CTA Button -->
<tr>
<td align="center" style="padding: 0 40px 30px;">
    <table cellpadding="0" cellspacing="0">
    <tr>
    <td style="background: linear-gradient(135deg, #00c853 0%, #00e676 100%); border-radius: 12px; padding: 16px 48px;">
        <a href="{domain_url}/oauth/initiate" style="color: #000; font-size: 17px; font-weight: 800; text-decoration: none; letter-spacing: 0.5px;">
            Step 1: Connect Your CRM &rarr;
        </a>
    </td>
    </tr>
    </table>
</td>
</tr>

<!-- What You Get -->
<tr>
<td style="padding: 0 40px 25px;">
    <h2 style="margin: 0 0 16px; font-size: 18px; font-weight: 700; color: #fff;">What's Included:</h2>
    <table cellpadding="0" cellspacing="0" width="100%">
    <tr>
        <td style="padding: 10px 0; border-bottom: 1px solid rgba(255,255,255,0.06);">
            <table cellpadding="0" cellspacing="0"><tr>
                <td style="width: 36px; vertical-align: top;">
                    <div style="width: 28px; height: 28px; background: rgba(0,200,83,0.15); border-radius: 8px; text-align: center; line-height: 28px; font-size: 14px;">&#9889;</div>
                </td>
                <td style="padding-left: 12px;">
                    <p style="margin: 0; font-size: 14px; color: #ddd;"><strong style="color: #fff;">AI Text Messaging</strong> &mdash; 5-second replies, insurance-trained, 24/7 lead engagement</p>
                </td>
            </tr></table>
        </td>
    </tr>
    <tr>
        <td style="padding: 10px 0; border-bottom: 1px solid rgba(255,255,255,0.06);">
            <table cellpadding="0" cellspacing="0"><tr>
                <td style="width: 36px; vertical-align: top;">
                    <div style="width: 28px; height: 28px; background: rgba(0,217,255,0.15); border-radius: 8px; text-align: center; line-height: 28px; font-size: 14px;">&#128222;</div>
                </td>
                <td style="padding-left: 12px;">
                    <p style="margin: 0; font-size: 14px; color: #ddd;"><strong style="color: #fff;">AI Voice Agent</strong> &mdash; Real-time voice calls powered by AI, handles inbound &amp; outbound</p>
                </td>
            </tr></table>
        </td>
    </tr>
    <tr>
        <td style="padding: 10px 0; border-bottom: 1px solid rgba(255,255,255,0.06);">
            <table cellpadding="0" cellspacing="0"><tr>
                <td style="width: 36px; vertical-align: top;">
                    <div style="width: 28px; height: 28px; background: rgba(233,30,99,0.15); border-radius: 8px; text-align: center; line-height: 28px; font-size: 14px;">&#128241;</div>
                </td>
                <td style="padding-left: 12px;">
                    <p style="margin: 0; font-size: 14px; color: #ddd;"><strong style="color: #fff;">Smart Dialer</strong> &mdash; Power dial leads, AI-ranked Smart Filters, call recording &amp; transcription</p>
                </td>
            </tr></table>
        </td>
    </tr>
    <tr>
        <td style="padding: 10px 0;">
            <table cellpadding="0" cellspacing="0"><tr>
                <td style="width: 36px; vertical-align: top;">
                    <div style="width: 28px; height: 28px; background: rgba(255,193,7,0.15); border-radius: 8px; text-align: center; line-height: 28px; font-size: 14px;">&#129504;</div>
                </td>
                <td style="padding-left: 12px;">
                    <p style="margin: 0; font-size: 14px; color: #ddd;"><strong style="color: #fff;">Lead Intelligence</strong> &mdash; AI scores every lead hot/warm/cool/cold with next-best-actions</p>
                </td>
            </tr></table>
        </td>
    </tr>
    </table>
</td>
</tr>

<!-- Stats Row -->
<tr>
<td style="padding: 0 40px 30px;">
    <table cellpadding="0" cellspacing="0" width="100%" style="background: rgba(255,255,255,0.03); border-radius: 12px; border: 1px solid rgba(255,255,255,0.06);">
    <tr>
        <td align="center" style="padding: 20px; width: 25%; border-right: 1px solid rgba(255,255,255,0.06);">
            <div style="font-size: 24px; font-weight: 800; color: #00c853;">5s</div>
            <div style="font-size: 11px; color: #888; margin-top: 4px;">Response</div>
        </td>
        <td align="center" style="padding: 20px; width: 25%; border-right: 1px solid rgba(255,255,255,0.06);">
            <div style="font-size: 24px; font-weight: 800; color: #00d9ff;">63+</div>
            <div style="font-size: 11px; color: #888; margin-top: 4px;">Carriers</div>
        </td>
        <td align="center" style="padding: 20px; width: 25%; border-right: 1px solid rgba(255,255,255,0.06);">
            <div style="font-size: 24px; font-weight: 800; color: #e91e63;">AI</div>
            <div style="font-size: 11px; color: #888; margin-top: 4px;">Voice &amp; Text</div>
        </td>
        <td align="center" style="padding: 20px; width: 25%;">
            <div style="font-size: 24px; font-weight: 800; color: #ffc107;">24/7</div>
            <div style="font-size: 11px; color: #888; margin-top: 4px;">Always On</div>
        </td>
    </tr>
    </table>
</td>
</tr>

<!-- Secondary CTA -->
<tr>
<td style="padding: 0 40px 20px;">
    <p style="margin: 0; font-size: 13px; color: #777; text-align: center;">
        Need help? Reply to this email or visit
        <a href="{domain_url}/support" style="color: #00c853; text-decoration: none;">our support page</a>.
    </p>
</td>
</tr>
'''
    return _email_wrapper(inner, domain_url)


def _build_reminder_24h_email(name: str, domain_url: str, user_type: str, missing: list = None) -> str:
    """Build the 24-hour onboarding reminder — premium marketing email with setup checklist."""
    missing = missing or []
    dashboard = f"{domain_url}/agency-dashboard" if user_type == "agency_owner" else f"{domain_url}/dashboard"
    checklist = _build_setup_checklist_html(missing, domain_url, user_type)

    if "crm_connection" in missing:
        hero_subtitle = "Connect your CRM to unleash your AI assistant"
        action_text = "Connect My CRM"
        action_url = f"{domain_url}/oauth/initiate"
    elif "subscription" in missing:
        hero_subtitle = "Subscribe to activate your AI sales machine"
        action_text = "Start Free Trial &mdash; $149.98/mo after"
        action_url = f"{domain_url}/checkout"
    else:
        hero_subtitle = "Complete your setup to start closing leads"
        action_text = "Finish Setup"
        action_url = dashboard

    inner = f'''
<tr>
<td align="center" style="padding: 0 40px 10px;">
    <div style="width: 80px; height: 80px; border-radius: 50%; background: linear-gradient(135deg, rgba(0,200,83,0.15) 0%, rgba(0,200,83,0.05) 100%); border: 2px solid rgba(0,200,83,0.25); margin: 0 auto 20px; line-height: 80px; text-align: center;">
        <span style="font-size: 36px;">&#9889;</span>
    </div>
    <h1 style="margin: 0 0 8px; font-size: 28px; font-weight: 800; color: #ffffff; line-height: 1.2;">Your Bot is Almost Live</h1>
    <p style="margin: 0; font-size: 16px; color: #aaa; line-height: 1.5;">{hero_subtitle}</p>
</td>
</tr>

<tr>
<td style="padding: 25px 40px 15px;">
    <p style="margin: 0; font-size: 16px; color: #ddd; line-height: 1.7;">
        Hi {name},
    </p>
    <p style="margin: 12px 0 0; font-size: 15px; color: #bbb; line-height: 1.7;">
        You installed InsuranceGrokBot 24 hours ago. You're almost there &mdash; your AI-powered sales platform is
        ready to start working leads with AI texting, voice AI, smart dialer, and lead intelligence.
    </p>
</td>
</tr>

<!-- Setup Progress -->
<tr>
<td style="padding: 10px 40px 25px;">
    <div style="background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08); border-radius: 16px; padding: 24px; margin: 10px 0;">
        <p style="margin: 0 0 16px; font-size: 13px; font-weight: 700; color: #00c853; text-transform: uppercase; letter-spacing: 1.5px;">Setup Progress</p>
        {checklist}
    </div>
</td>
</tr>

<!-- What You're Missing -->
<tr>
<td style="padding: 0 40px 25px;">
    <div style="background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08); border-radius: 16px; padding: 24px;">
        <p style="margin: 0 0 16px; font-size: 13px; font-weight: 700; color: #00c853; text-transform: uppercase; letter-spacing: 1.5px;">What You're Missing</p>
        <table cellpadding="0" cellspacing="0" width="100%">
            <tr>
                <td width="50%" style="padding: 8px 8px 8px 0; vertical-align: top;">
                    <div style="font-size: 14px; color: #fff; font-weight: 600;">&#9889; AI Text Messaging</div>
                    <div style="font-size: 12px; color: #888;">5-second replies to every lead, 24/7</div>
                </td>
                <td width="50%" style="padding: 8px 0 8px 8px; vertical-align: top;">
                    <div style="font-size: 14px; color: #fff; font-weight: 600;">&#128222; AI Voice Agent</div>
                    <div style="font-size: 12px; color: #888;">Handles calls with real-time AI conversation</div>
                </td>
            </tr>
            <tr>
                <td width="50%" style="padding: 8px 8px 8px 0; vertical-align: top;">
                    <div style="font-size: 14px; color: #fff; font-weight: 600;">&#128241; Smart Dialer</div>
                    <div style="font-size: 12px; color: #888;">Power dial with AI-ranked lead scoring</div>
                </td>
                <td width="50%" style="padding: 8px 0 8px 8px; vertical-align: top;">
                    <div style="font-size: 14px; color: #fff; font-weight: 600;">&#129504; Lead Intelligence</div>
                    <div style="font-size: 12px; color: #888;">AI classifies every lead hot/warm/cool/cold</div>
                </td>
            </tr>
            <tr>
                <td width="50%" style="padding: 8px 8px 0 0; vertical-align: top;">
                    <div style="font-size: 14px; color: #fff; font-weight: 600;">&#128197; Auto Booking</div>
                    <div style="font-size: 12px; color: #888;">Checks your calendar, books meetings</div>
                </td>
                <td width="50%" style="padding: 8px 0 0 8px; vertical-align: top;">
                    <div style="font-size: 14px; color: #fff; font-weight: 600;">&#127981; 63+ Carriers</div>
                    <div style="font-size: 12px; color: #888;">Carrier-aware conversations</div>
                </td>
            </tr>
        </table>
    </div>
</td>
</tr>

<!-- CTA Button -->
<tr>
<td align="center" style="padding: 5px 40px 30px;">
    <table cellpadding="0" cellspacing="0"><tr>
        <td style="background: linear-gradient(135deg, #00c853 0%, #00e676 100%); border-radius: 14px; box-shadow: 0 4px 20px rgba(0,200,83,0.3);">
            <a href="{action_url}" style="display: inline-block; padding: 18px 48px; color: #000000; font-size: 17px; font-weight: 800; text-decoration: none; letter-spacing: -0.3px;">
                {action_text} &rarr;
            </a>
        </td>
    </tr></table>
    <p style="margin: 14px 0 0; font-size: 13px; color: #666;">Cancel anytime &bull; No contracts &bull; Takes 2 minutes</p>
</td>
</tr>

<!-- Testimonial -->
<tr>
<td style="padding: 0 40px 25px;">
    <div style="background: rgba(255,255,255,0.02); border-left: 3px solid #00c853; padding: 16px 20px; border-radius: 0 12px 12px 0;">
        <p style="margin: 0 0 8px; font-size: 14px; color: #ccc; font-style: italic; line-height: 1.6;">
            "The AI dialer alone changed my workflow. Smart Filters sort my leads so I always call the hottest ones first. I closed 4 policies my first week."
        </p>
        <p style="margin: 0; font-size: 12px; color: #00c853; font-weight: 600;">
            &mdash; Independent Agent, Texas
        </p>
    </div>
</td>
</tr>

<tr>
<td style="padding: 0 40px 10px;">
    <p style="margin: 0; font-size: 14px; color: #888; line-height: 1.6; text-align: center;">
        Need help? <a href="{domain_url}/support" style="color: #00c853; text-decoration: none; font-weight: 600;">Visit support</a> or just reply to this email.
    </p>
</td>
</tr>
'''
    return _email_wrapper(inner, domain_url)


def _build_reminder_72h_email(name: str, domain_url: str, user_type: str, missing: list = None) -> str:
    """Build the 72-hour onboarding reminder — urgency-driven premium marketing email."""
    missing = missing or []
    dashboard = f"{domain_url}/agency-dashboard" if user_type == "agency_owner" else f"{domain_url}/dashboard"
    checklist = _build_setup_checklist_html(missing, domain_url, user_type)

    if "crm_connection" in missing:
        action_text = "Connect CRM & Go Live"
        action_url = f"{domain_url}/oauth/initiate"
    elif "subscription" in missing:
        action_text = "Start Free Trial &mdash; $149.98/mo after"
        action_url = f"{domain_url}/checkout"
    else:
        action_text = "Complete Setup Now"
        action_url = dashboard

    inner = f'''
<tr>
<td align="center" style="padding: 0 40px 10px;">
    <div style="width: 80px; height: 80px; border-radius: 50%; background: linear-gradient(135deg, rgba(255,107,53,0.15) 0%, rgba(255,152,0,0.05) 100%); border: 2px solid rgba(255,107,53,0.3); margin: 0 auto 20px; line-height: 80px; text-align: center;">
        <span style="font-size: 36px;">&#9203;</span>
    </div>
    <h1 style="margin: 0 0 8px; font-size: 28px; font-weight: 800; color: #ffffff; line-height: 1.2;">Leads Are Slipping Away</h1>
    <p style="margin: 0; font-size: 16px; color: #ff9800; line-height: 1.5; font-weight: 600;">3 days without your bot = missed revenue</p>
</td>
</tr>

<tr>
<td style="padding: 25px 40px 15px;">
    <p style="margin: 0; font-size: 16px; color: #ddd; line-height: 1.7;">
        Hi {name},
    </p>
    <p style="margin: 12px 0 0; font-size: 15px; color: #bbb; line-height: 1.7;">
        It's been 3 days since you installed InsuranceGrokBot. Every hour your AI sales platform isn't active, leads are going unworked, calls are going unmade, and potential clients are moving to the agent who responds first.
    </p>
</td>
</tr>

<!-- The Cost of Waiting -->
<tr>
<td style="padding: 10px 40px 20px;">
    <div style="background: linear-gradient(135deg, rgba(255,107,53,0.08) 0%, rgba(255,152,0,0.04) 100%); border: 1px solid rgba(255,152,0,0.2); border-radius: 16px; padding: 24px;">
        <p style="margin: 0 0 16px; font-size: 13px; font-weight: 700; color: #ff9800; text-transform: uppercase; letter-spacing: 1.5px;">The Cost of Waiting</p>
        <table cellpadding="0" cellspacing="0" width="100%">
            <tr>
                <td style="padding: 10px 0; border-bottom: 1px solid rgba(255,255,255,0.05);">
                    <table cellpadding="0" cellspacing="0" width="100%"><tr>
                        <td width="40" style="vertical-align: top;"><span style="font-size: 20px;">&#128168;</span></td>
                        <td style="vertical-align: top;">
                            <span style="color: #fff; font-weight: 600; font-size: 14px;">AI texting responds in 5 seconds</span><br>
                            <span style="color: #999; font-size: 13px;">78% of buyers choose the first agent to respond. Your bot texts back instantly, 24/7.</span>
                        </td>
                    </tr></table>
                </td>
            </tr>
            <tr>
                <td style="padding: 10px 0; border-bottom: 1px solid rgba(255,255,255,0.05);">
                    <table cellpadding="0" cellspacing="0" width="100%"><tr>
                        <td width="40" style="vertical-align: top;"><span style="font-size: 20px;">&#128222;</span></td>
                        <td style="vertical-align: top;">
                            <span style="color: #fff; font-weight: 600; font-size: 14px;">Voice AI handles calls you can't take</span><br>
                            <span style="color: #999; font-size: 13px;">Inbound and outbound AI calls that qualify leads and book appointments for you.</span>
                        </td>
                    </tr></table>
                </td>
            </tr>
            <tr>
                <td style="padding: 10px 0; border-bottom: 1px solid rgba(255,255,255,0.05);">
                    <table cellpadding="0" cellspacing="0" width="100%"><tr>
                        <td width="40" style="vertical-align: top;"><span style="font-size: 20px;">&#128241;</span></td>
                        <td style="vertical-align: top;">
                            <span style="color: #fff; font-weight: 600; font-size: 14px;">Smart Dialer prioritizes your hottest leads</span><br>
                            <span style="color: #999; font-size: 13px;">AI-ranked Smart Filters so you always call the leads most likely to buy first.</span>
                        </td>
                    </tr></table>
                </td>
            </tr>
            <tr>
                <td style="padding: 10px 0;">
                    <table cellpadding="0" cellspacing="0" width="100%"><tr>
                        <td width="40" style="vertical-align: top;"><span style="font-size: 20px;">&#129504;</span></td>
                        <td style="vertical-align: top;">
                            <span style="color: #fff; font-weight: 600; font-size: 14px;">Lead Intelligence scores every contact</span><br>
                            <span style="color: #999; font-size: 13px;">AI reads conversations and classifies leads hot/warm/cool/cold with next-best-actions.</span>
                        </td>
                    </tr></table>
                </td>
            </tr>
        </table>
    </div>
</td>
</tr>

<!-- Setup Progress -->
<tr>
<td style="padding: 5px 40px 20px;">
    <div style="background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08); border-radius: 16px; padding: 24px;">
        <p style="margin: 0 0 16px; font-size: 13px; font-weight: 700; color: #ff9800; text-transform: uppercase; letter-spacing: 1.5px;">Your Setup Status</p>
        {checklist}
    </div>
</td>
</tr>

<!-- CTA Button -->
<tr>
<td align="center" style="padding: 10px 40px 25px;">
    <table cellpadding="0" cellspacing="0"><tr>
        <td style="background: linear-gradient(135deg, #ff6b35 0%, #ff9800 100%); border-radius: 14px; box-shadow: 0 4px 20px rgba(255,107,53,0.35);">
            <a href="{action_url}" style="display: inline-block; padding: 18px 48px; color: #ffffff; font-size: 17px; font-weight: 800; text-decoration: none; letter-spacing: -0.3px;">
                {action_text} &rarr;
            </a>
        </td>
    </tr></table>
    <p style="margin: 14px 0 0; font-size: 13px; color: #666;">Cancel anytime &bull; No contracts &bull; Your competitors are already using AI.</p>
</td>
</tr>

<!-- Before/After -->
<tr>
<td style="padding: 0 40px 25px;">
    <table cellpadding="0" cellspacing="0" width="100%">
    <tr>
        <td width="48%" style="background: rgba(239,68,68,0.06); border: 1px solid rgba(239,68,68,0.15); border-radius: 12px; padding: 20px; vertical-align: top;">
            <p style="margin: 0 0 10px; font-size: 12px; font-weight: 700; color: #ef4444; text-transform: uppercase; letter-spacing: 1px;">Without GrokBot</p>
            <p style="margin: 0; font-size: 13px; color: #999; line-height: 1.7;">
                &#10060; Leads wait hours for a reply<br>
                &#10060; Manual dialing, no prioritization<br>
                &#10060; Missed after-hours calls &amp; texts<br>
                &#10060; No idea which leads are hot
            </p>
        </td>
        <td width="4%">&nbsp;</td>
        <td width="48%" style="background: rgba(0,200,83,0.06); border: 1px solid rgba(0,200,83,0.15); border-radius: 12px; padding: 20px; vertical-align: top;">
            <p style="margin: 0 0 10px; font-size: 12px; font-weight: 700; color: #00c853; text-transform: uppercase; letter-spacing: 1px;">With GrokBot</p>
            <p style="margin: 0; font-size: 13px; color: #999; line-height: 1.7;">
                &#10004; 5-second AI text responses<br>
                &#10004; Smart Dialer + AI voice calls<br>
                &#10004; 24/7 coverage, every channel<br>
                &#10004; AI lead scoring &amp; Smart Filters
            </p>
        </td>
    </tr>
    </table>
</td>
</tr>

<tr>
<td style="padding: 0 40px 10px;">
    <p style="margin: 0; font-size: 14px; color: #888; line-height: 1.6; text-align: center;">
        Stuck on something? <a href="{domain_url}/support" style="color: #ff9800; text-decoration: none; font-weight: 600;">Get help here</a> or reply to this email and we'll walk you through it.
    </p>
</td>
</tr>
'''
    return _email_wrapper(inner, domain_url)


def _build_welcome_email(user_name: str, dashboard_link: str, domain_url: str) -> str:
    """Build the post-OAuth welcome email — CRM connected, now subscribe + set password."""
    checkout_url = f"{domain_url}/checkout"
    set_pw_url = f"{domain_url}/set-password?type=individual"
    inner = f'''
<tr>
<td align="center" style="padding: 0 40px 10px;">
    <div style="width: 80px; height: 80px; border-radius: 50%; background: linear-gradient(135deg, rgba(0,200,83,0.15) 0%, rgba(0,200,83,0.05) 100%); border: 2px solid rgba(0,200,83,0.25); margin: 0 auto 20px; line-height: 80px; text-align: center;">
        <span style="font-size: 36px;">&#10003;</span>
    </div>
    <h1 style="margin: 0 0 8px; font-size: 28px; font-weight: 800; color: #ffffff; line-height: 1.2;">CRM Connected &mdash; You're Almost Live</h1>
    <p style="margin: 0; font-size: 16px; color: #aaa; line-height: 1.5;">Two quick steps left to activate your AI sales machine</p>
</td>
</tr>

<tr>
<td style="padding: 25px 40px 15px;">
    <p style="margin: 0; font-size: 16px; color: #ddd; line-height: 1.7;">Hi {user_name},</p>
    <p style="margin: 12px 0 0; font-size: 15px; color: #bbb; line-height: 1.7;">
        Your Lead Connector account is connected. Your AI assistant is configured and standing by.
        Complete these two steps to go live:
    </p>
</td>
</tr>

<!-- Numbered Next Steps -->
<tr>
<td style="padding: 10px 40px 20px;">
    <div style="background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08); border-radius: 16px; padding: 24px;">
        <p style="margin: 0 0 16px; font-size: 13px; font-weight: 700; color: #00c853; text-transform: uppercase; letter-spacing: 1.5px;">Next Steps</p>
        <table cellpadding="0" cellspacing="0" width="100%">
            <tr>
                <td style="padding: 14px 0; border-bottom: 1px solid rgba(255,255,255,0.05);">
                    <table cellpadding="0" cellspacing="0" width="100%"><tr>
                        <td width="44" style="vertical-align: top;">
                            <div style="width: 32px; height: 32px; background: linear-gradient(135deg, #00c853, #00e676); border-radius: 50%; text-align: center; line-height: 32px; color: #000; font-weight: 900; font-size: 15px;">1</div>
                        </td>
                        <td style="vertical-align: middle;">
                            <a href="{checkout_url}" style="color: #00c853; font-weight: 700; text-decoration: none; font-size: 16px;">Start Free Trial &mdash; $149.98/mo after</a>
                            <div style="color: #888; font-size: 13px; margin-top: 2px;">7-day free trial. Cancel anytime, no contracts.</div>
                        </td>
                    </tr></table>
                </td>
            </tr>
            <tr>
                <td style="padding: 14px 0; border-bottom: 1px solid rgba(255,255,255,0.05);">
                    <table cellpadding="0" cellspacing="0" width="100%"><tr>
                        <td width="44" style="vertical-align: top;">
                            <div style="width: 32px; height: 32px; background: rgba(255,255,255,0.1); border: 1px solid rgba(255,255,255,0.2); border-radius: 50%; text-align: center; line-height: 32px; color: #fff; font-weight: 900; font-size: 15px;">2</div>
                        </td>
                        <td style="vertical-align: middle;">
                            <a href="{set_pw_url}" style="color: #ddd; font-weight: 700; text-decoration: none; font-size: 16px;">Set Your Password</a>
                            <div style="color: #888; font-size: 13px; margin-top: 2px;">So you can log back in anytime at {domain_url}/login</div>
                        </td>
                    </tr></table>
                </td>
            </tr>
            <tr>
                <td style="padding: 14px 0;">
                    <table cellpadding="0" cellspacing="0" width="100%"><tr>
                        <td width="44" style="vertical-align: top;">
                            <div style="width: 32px; height: 32px; background: rgba(255,255,255,0.1); border: 1px solid rgba(255,255,255,0.2); border-radius: 50%; text-align: center; line-height: 32px; color: #fff; font-weight: 900; font-size: 15px;">3</div>
                        </td>
                        <td style="vertical-align: middle;">
                            <a href="{dashboard_link}" style="color: #ddd; font-weight: 700; text-decoration: none; font-size: 16px;">Configure Your Dashboard</a>
                            <div style="color: #888; font-size: 13px; margin-top: 2px;">Pick carriers, connect calendar, set your bot's tone &amp; personality</div>
                        </td>
                    </tr></table>
                </td>
            </tr>
        </table>
    </div>
</td>
</tr>

<!-- Primary CTA -->
<tr>
<td align="center" style="padding: 5px 40px 25px;">
    <table cellpadding="0" cellspacing="0"><tr>
        <td style="background: linear-gradient(135deg, #00c853 0%, #00e676 100%); border-radius: 14px; box-shadow: 0 4px 20px rgba(0,200,83,0.3);">
            <a href="{checkout_url}" style="display: inline-block; padding: 18px 48px; color: #000000; font-size: 17px; font-weight: 800; text-decoration: none; letter-spacing: -0.3px;">
                Subscribe &amp; Activate &rarr;
            </a>
        </td>
    </tr></table>
    <p style="margin: 10px 0 0; font-size: 12px; color: #666;">7-day free trial &bull; $149.98/mo after &bull; Cancel anytime</p>
</td>
</tr>

<!-- What's Included -->
<tr>
<td style="padding: 5px 40px 20px;">
    <table cellpadding="0" cellspacing="0" width="100%">
    <tr>
        <td width="25%" align="center" style="padding: 16px 4px; background: rgba(0,200,83,0.06); border-radius: 12px 0 0 12px; border: 1px solid rgba(0,200,83,0.1);">
            <div style="font-size: 20px; margin-bottom: 4px;">&#9889;</div>
            <div style="font-size: 12px; color: #ccc; font-weight: 600;">AI Texting</div>
        </td>
        <td width="25%" align="center" style="padding: 16px 4px; background: rgba(0,217,255,0.06); border: 1px solid rgba(0,217,255,0.1);">
            <div style="font-size: 20px; margin-bottom: 4px;">&#128222;</div>
            <div style="font-size: 12px; color: #ccc; font-weight: 600;">Voice AI</div>
        </td>
        <td width="25%" align="center" style="padding: 16px 4px; background: rgba(233,30,99,0.06); border: 1px solid rgba(233,30,99,0.1);">
            <div style="font-size: 20px; margin-bottom: 4px;">&#128241;</div>
            <div style="font-size: 12px; color: #ccc; font-weight: 600;">Smart Dialer</div>
        </td>
        <td width="25%" align="center" style="padding: 16px 4px; background: rgba(255,193,7,0.06); border-radius: 0 12px 12px 0; border: 1px solid rgba(255,193,7,0.1);">
            <div style="font-size: 20px; margin-bottom: 4px;">&#129504;</div>
            <div style="font-size: 12px; color: #ccc; font-weight: 600;">Lead Intel</div>
        </td>
    </tr>
    </table>
</td>
</tr>

<!-- Onboarding tracker link -->
<tr>
<td style="padding: 0 40px 10px;">
    <p style="margin: 0; font-size: 14px; color: #888; line-height: 1.6; text-align: center;">
        Track your progress: <a href="{domain_url}/onboarding-status" style="color: #00c853; text-decoration: none; font-weight: 600;">Onboarding Status</a>
        &nbsp;|&nbsp;
        <a href="{domain_url}/support" style="color: #00c853; text-decoration: none; font-weight: 600;">Support</a>
    </p>
</td>
</tr>
'''
    return _email_wrapper(inner, domain_url)


def _build_agency_owner_welcome_email(user_name: str, dashboard_link: str, domain_url: str) -> str:
    """Build post-OAuth welcome email for agency owners — FREE account, no subscription step."""
    set_pw_url = f"{domain_url}/forgot-password"
    inner = f'''
<tr>
<td align="center" style="padding: 0 40px 10px;">
    <div style="width: 80px; height: 80px; border-radius: 50%; background: linear-gradient(135deg, rgba(139,92,246,0.15) 0%, rgba(139,92,246,0.05) 100%); border: 2px solid rgba(139,92,246,0.25); margin: 0 auto 20px; line-height: 80px; text-align: center;">
        <span style="font-size: 36px;">&#10003;</span>
    </div>
    <h1 style="margin: 0 0 8px; font-size: 28px; font-weight: 800; color: #ffffff; line-height: 1.2;">Your Agency Dashboard is Ready</h1>
    <p style="margin: 0; font-size: 16px; color: #aaa; line-height: 1.5;">InsuranceGrokBot is now installed across your agency</p>
</td>
</tr>

<tr>
<td style="padding: 25px 40px 15px;">
    <p style="margin: 0; font-size: 16px; color: #ddd; line-height: 1.7;">Hi {user_name},</p>
    <p style="margin: 12px 0 0; font-size: 15px; color: #bbb; line-height: 1.7;">
        Your agency account is set up. The app is now visible on your agents' GoHighLevel sidebar.
        As they subscribe, they'll automatically appear in your agency dashboard.
    </p>
</td>
</tr>

<tr>
<td style="padding: 10px 40px 20px;">
    <div style="background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08); border-radius: 16px; padding: 24px;">
        <p style="margin: 0 0 16px; font-size: 13px; font-weight: 700; color: #8b5cf6; text-transform: uppercase; letter-spacing: 1.5px;">Get Started</p>
        <table cellpadding="0" cellspacing="0" width="100%">
            <tr>
                <td style="padding: 14px 0; border-bottom: 1px solid rgba(255,255,255,0.05);">
                    <table cellpadding="0" cellspacing="0" width="100%"><tr>
                        <td width="44" style="vertical-align: top;">
                            <div style="width: 32px; height: 32px; background: linear-gradient(135deg, #8b5cf6, #a78bfa); border-radius: 50%; text-align: center; line-height: 32px; color: #fff; font-weight: 900; font-size: 15px;">1</div>
                        </td>
                        <td style="vertical-align: middle;">
                            <a href="{set_pw_url}" style="color: #a78bfa; font-weight: 700; text-decoration: none; font-size: 16px;">Set Your Password</a>
                            <div style="color: #888; font-size: 13px; margin-top: 2px;">So you can log into your agency dashboard anytime</div>
                        </td>
                    </tr></table>
                </td>
            </tr>
            <tr>
                <td style="padding: 14px 0; border-bottom: 1px solid rgba(255,255,255,0.05);">
                    <table cellpadding="0" cellspacing="0" width="100%"><tr>
                        <td width="44" style="vertical-align: top;">
                            <div style="width: 32px; height: 32px; background: rgba(255,255,255,0.1); border: 1px solid rgba(255,255,255,0.2); border-radius: 50%; text-align: center; line-height: 32px; color: #fff; font-weight: 900; font-size: 15px;">2</div>
                        </td>
                        <td style="vertical-align: middle;">
                            <a href="{dashboard_link}" style="color: #ddd; font-weight: 700; text-decoration: none; font-size: 16px;">Open Your Agency Dashboard</a>
                            <div style="color: #888; font-size: 13px; margin-top: 2px;">View KPIs, manage white-label branding, see agent activity</div>
                        </td>
                    </tr></table>
                </td>
            </tr>
            <tr>
                <td style="padding: 14px 0;">
                    <table cellpadding="0" cellspacing="0" width="100%"><tr>
                        <td width="44" style="vertical-align: top;">
                            <div style="width: 32px; height: 32px; background: rgba(255,255,255,0.1); border: 1px solid rgba(255,255,255,0.2); border-radius: 50%; text-align: center; line-height: 32px; color: #fff; font-weight: 900; font-size: 15px;">3</div>
                        </td>
                        <td style="vertical-align: middle;">
                            <span style="color: #ddd; font-weight: 700; font-size: 16px;">Your Agents Subscribe Themselves</span>
                            <div style="color: #888; font-size: 13px; margin-top: 2px;">As they sign up, they auto-appear in your Members tab. No manual setup needed.</div>
                        </td>
                    </tr></table>
                </td>
            </tr>
        </table>
    </div>
</td>
</tr>

<tr>
<td align="center" style="padding: 5px 40px 25px;">
    <table cellpadding="0" cellspacing="0"><tr>
        <td style="background: linear-gradient(135deg, #8b5cf6 0%, #a78bfa 100%); border-radius: 14px; box-shadow: 0 4px 20px rgba(139,92,246,0.3);">
            <a href="{set_pw_url}" style="display: inline-block; padding: 18px 48px; color: #ffffff; font-size: 17px; font-weight: 800; text-decoration: none; letter-spacing: -0.3px;">
                Set Password &amp; Log In &rarr;
            </a>
        </td>
    </tr></table>
    <p style="margin: 10px 0 0; font-size: 12px; color: #666;">Your agency dashboard is free &mdash; your agents pay for their own plans</p>
</td>
</tr>

<tr>
<td style="padding: 0 40px 10px;">
    <p style="margin: 0; font-size: 14px; color: #888; line-height: 1.6; text-align: center;">
        <a href="{domain_url}/support" style="color: #8b5cf6; text-decoration: none; font-weight: 600;">Support</a>
    </p>
</td>
</tr>
'''
    return _email_wrapper(inner, domain_url)


# ── Transactional emails ─────────────────────────────────────────────────────

def _build_password_reset_html(reset_url: str, domain_url: str) -> str:
    """Build HTML for the password-reset email."""
    return f"""
    <html>
    <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
        <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
            <h2 style="color: #2563eb;">Password Reset</h2>
            <p>We received a request to reset your InsuranceGrokBot password.</p>
            <p>Click the button below to choose a new password:</p>
            <div style="text-align: center; margin: 30px 0;">
                <a href="{reset_url}"
                   style="background-color: #2563eb; color: white; padding: 14px 28px;
                          text-decoration: none; border-radius: 8px; font-weight: bold;
                          display: inline-block;">
                    Reset My Password
                </a>
            </div>
            <p style="color: #666; font-size: 14px;">
                This link expires in 30 minutes. If you didn't request this,
                you can safely ignore this email.
            </p>
            <hr style="border: none; border-top: 1px solid #eee; margin: 30px 0;">
            <p style="color: #999; font-size: 12px;">
                InsuranceGrokBot - AI-Powered Insurance Sales Assistant<br>
                <a href="{domain_url}" style="color: #2563eb;">{domain_url}</a>
            </p>
        </div>
    </body>
    </html>
    """


def _build_agency_invite_html(agent_name: str, agency_name: str,
                               invite_url: str, domain_url: str) -> tuple:
    """Build HTML + text body for the agency sub-user invite email.
    Returns (html_body, text_body).
    """
    html_body = f"""
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
    </head>
    <body style="margin:0;padding:0;background-color:#050505;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
        <div style="max-width:560px;margin:0 auto;padding:40px 20px;">
            <!-- Header -->
            <div style="text-align:center;margin-bottom:32px;">
                <div style="display:inline-block;background:linear-gradient(135deg,#00ff88,#00d9ff);width:56px;height:56px;border-radius:16px;line-height:56px;font-size:24px;font-weight:900;color:#000;margin-bottom:16px;">
                    IGB
                </div>
                <h1 style="color:#ffffff;font-size:26px;font-weight:800;margin:0 0 6px;letter-spacing:-0.5px;">
                    You're Invited
                </h1>
                <p style="color:#888;font-size:15px;margin:0;">
                    Your AI-powered sales assistant is ready
                </p>
            </div>

            <!-- Main card -->
            <div style="background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);border-radius:16px;padding:32px 28px;">
                <p style="color:#ddd;font-size:16px;line-height:1.6;margin:0 0 8px;">
                    Hi <strong style="color:#fff;">{agent_name}</strong>,
                </p>
                <p style="color:#aaa;font-size:15px;line-height:1.6;margin:0 0 24px;">
                    <strong style="color:#00ff88;">{agency_name}</strong> has set up an AI-powered
                    insurance sales assistant for your location. Your account is ready &mdash;
                    just set your password to get started.
                </p>

                <!-- What you get -->
                <div style="background:rgba(0,255,136,0.04);border:1px solid rgba(0,255,136,0.12);border-radius:12px;padding:18px 20px;margin-bottom:24px;">
                    <p style="color:#00ff88;font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:1px;margin:0 0 12px;">What's Included</p>
                    <table style="width:100%;border:0;" cellpadding="0" cellspacing="0">
                        <tr>
                            <td style="padding:4px 0;color:#ccc;font-size:14px;">&#x2713;&ensp; AI-powered SMS bot that responds to leads 24/7</td>
                        </tr>
                        <tr>
                            <td style="padding:4px 0;color:#ccc;font-size:14px;">&#x2713;&ensp; Smart dialer with AI call intelligence</td>
                        </tr>
                        <tr>
                            <td style="padding:4px 0;color:#ccc;font-size:14px;">&#x2713;&ensp; Real-time lead scoring and Smart Filters</td>
                        </tr>
                        <tr>
                            <td style="padding:4px 0;color:#ccc;font-size:14px;">&#x2713;&ensp; Automated workflows and follow-up sequences</td>
                        </tr>
                    </table>
                </div>

                <!-- CTA Button -->
                <div style="text-align:center;margin-bottom:8px;">
                    <a href="{invite_url}"
                       style="display:inline-block;background:linear-gradient(135deg,#00ff88,#00d9ff);color:#000;font-weight:800;font-size:16px;padding:16px 40px;border-radius:12px;text-decoration:none;letter-spacing:-0.3px;box-shadow:0 4px 20px rgba(0,255,136,0.25);">
                        Activate My Account
                    </a>
                </div>
                <p style="text-align:center;color:#666;font-size:13px;margin:12px 0 0;">
                    Takes less than 30 seconds
                </p>
            </div>

            <!-- Footer -->
            <div style="margin-top:32px;padding-top:20px;border-top:1px solid rgba(255,255,255,0.06);text-align:center;">
                <p style="color:#555;font-size:12px;margin:0 0 4px;">
                    This invitation expires in 7 days.
                </p>
                <p style="color:#444;font-size:12px;margin:0;">
                    InsuranceGrokBot &mdash; AI-Powered Insurance Sales Assistant<br>
                    <a href="{domain_url}" style="color:#00ff88;text-decoration:none;">{domain_url}</a>
                </p>
            </div>
        </div>
    </body>
    </html>
    """

    text_body = (
        f"You're Invited to InsuranceGrokBot!\n\n"
        f"Hi {agent_name},\n\n"
        f"{agency_name} has set up an AI-powered insurance sales assistant "
        f"for your location. Your account is ready — just set your password "
        f"to get started.\n\n"
        f"What's included:\n"
        f"- AI-powered SMS bot that responds to leads 24/7\n"
        f"- Smart dialer with AI call intelligence\n"
        f"- Real-time lead scoring and Smart Filters\n"
        f"- Automated workflows and follow-up sequences\n\n"
        f"Activate your account here:\n{invite_url}\n\n"
        f"This invitation expires in 7 days.\n\n"
        f"- InsuranceGrokBot Team\n{domain_url}"
    )

    return html_body, text_body


def build_app_update_email(domain_url: str, update_notes: str = "") -> tuple:
    """
    Email sent to all subscribers when a new InsuranceGrokBot version is
    published to the GHL Marketplace and users need to accept updated permissions.

    Returns (html_body, text_body).
    """
    notes_block = ""
    if update_notes:
        notes_block = f"""
<!-- What's New -->
<tr>
<td style="padding: 0 40px 28px;">
    <div style="background: rgba(0,200,83,0.06); border: 1px solid rgba(0,200,83,0.15); border-radius: 14px; padding: 20px 24px;">
        <p style="margin: 0 0 10px; font-size: 13px; font-weight: 700; color: #00c853; text-transform: uppercase; letter-spacing: 0.5px;">What's New in This Update</p>
        <p style="margin: 0; font-size: 15px; color: #c8d6e5; line-height: 1.7;">{update_notes}</p>
    </div>
</td>
</tr>
"""

    inner_html = f"""
<!-- Hero -->
<tr>
<td align="center" style="padding: 10px 40px 28px;">
    <div style="display: inline-block; background: rgba(99,102,241,0.12); border: 1px solid rgba(99,102,241,0.25); border-radius: 50px; padding: 7px 18px; margin-bottom: 18px;">
        <span style="font-size: 12px; font-weight: 700; color: #a5b4fc; text-transform: uppercase; letter-spacing: 1px;">App Update Available</span>
    </div>
    <h1 style="margin: 0 0 12px; font-size: 28px; font-weight: 800; color: #ffffff; line-height: 1.25;">A New Version of InsuranceGrokBot<br>is Ready to Install</h1>
    <p style="margin: 0; font-size: 16px; color: #8892a4; line-height: 1.6;">We've pushed an update to the GoHighLevel Marketplace. To keep your bot running with the latest features and fixes, please update the app in your Lead Connector account — it only takes 30 seconds.</p>
</td>
</tr>

{notes_block}

<!-- Steps -->
<tr>
<td style="padding: 0 40px 32px;">
    <p style="margin: 0 0 16px; font-size: 14px; font-weight: 700; color: #ffffff; text-transform: uppercase; letter-spacing: 0.5px;">How to Update — Follow These Steps</p>

    <!-- Step 1 -->
    <table cellpadding="0" cellspacing="0" width="100%" style="margin-bottom: 10px;">
    <tr>
        <td width="40" valign="top" style="padding-top: 2px;">
            <div style="width: 28px; height: 28px; background: linear-gradient(135deg,#3b82f6,#6366f1); border-radius: 50%; text-align: center; line-height: 28px; font-size: 13px; font-weight: 800; color: #fff;">1</div>
        </td>
        <td style="padding-left: 10px;">
            <p style="margin: 0; font-size: 15px; color: #e2e8f0; line-height: 1.5;"><strong style="color:#ffffff;">Log into Lead Connector</strong><br><span style="color:#8892a4;font-size:13px;">Go to <a href="https://app.leadconnectorhq.com" style="color:#00c853;text-decoration:none;">app.leadconnectorhq.com</a> and sign in to your account.</span></p>
        </td>
    </tr>
    </table>

    <!-- Step 2 -->
    <table cellpadding="0" cellspacing="0" width="100%" style="margin-bottom: 10px;">
    <tr>
        <td width="40" valign="top" style="padding-top: 2px;">
            <div style="width: 28px; height: 28px; background: linear-gradient(135deg,#3b82f6,#6366f1); border-radius: 50%; text-align: center; line-height: 28px; font-size: 13px; font-weight: 800; color: #fff;">2</div>
        </td>
        <td style="padding-left: 10px;">
            <p style="margin: 0; font-size: 15px; color: #e2e8f0; line-height: 1.5;"><strong style="color:#ffffff;">Go to App Marketplace</strong><br><span style="color:#8892a4;font-size:13px;">In the left sidebar, click <strong style="color:#c8d6e5;">App Marketplace</strong>.</span></p>
        </td>
    </tr>
    </table>

    <!-- Step 3 -->
    <table cellpadding="0" cellspacing="0" width="100%" style="margin-bottom: 10px;">
    <tr>
        <td width="40" valign="top" style="padding-top: 2px;">
            <div style="width: 28px; height: 28px; background: linear-gradient(135deg,#3b82f6,#6366f1); border-radius: 50%; text-align: center; line-height: 28px; font-size: 13px; font-weight: 800; color: #fff;">3</div>
        </td>
        <td style="padding-left: 10px;">
            <p style="margin: 0; font-size: 15px; color: #e2e8f0; line-height: 1.5;"><strong style="color:#ffffff;">Go to Installed Apps</strong><br><span style="color:#8892a4;font-size:13px;">Click the <strong style="color:#c8d6e5;">Installed Apps</strong> tab at the top of the Marketplace page.</span></p>
        </td>
    </tr>
    </table>

    <!-- Step 4 -->
    <table cellpadding="0" cellspacing="0" width="100%" style="margin-bottom: 10px;">
    <tr>
        <td width="40" valign="top" style="padding-top: 2px;">
            <div style="width: 28px; height: 28px; background: linear-gradient(135deg,#3b82f6,#6366f1); border-radius: 50%; text-align: center; line-height: 28px; font-size: 13px; font-weight: 800; color: #fff;">4</div>
        </td>
        <td style="padding-left: 10px;">
            <p style="margin: 0; font-size: 15px; color: #e2e8f0; line-height: 1.5;"><strong style="color:#ffffff;">Select InsuranceGrokBot</strong><br><span style="color:#8892a4;font-size:13px;">Find <strong style="color:#c8d6e5;">InsuranceGrokBot</strong> in your installed apps list and click on it.</span></p>
        </td>
    </tr>
    </table>

    <!-- Step 5 -->
    <table cellpadding="0" cellspacing="0" width="100%">
    <tr>
        <td width="40" valign="top" style="padding-top: 2px;">
            <div style="width: 28px; height: 28px; background: linear-gradient(135deg,#00c853,#00e676); border-radius: 50%; text-align: center; line-height: 28px; font-size: 13px; font-weight: 800; color: #000;">5</div>
        </td>
        <td style="padding-left: 10px;">
            <p style="margin: 0; font-size: 15px; color: #e2e8f0; line-height: 1.5;"><strong style="color:#ffffff;">Click Update</strong><br><span style="color:#8892a4;font-size:13px;">Click the <strong style="color:#00c853;">Update</strong> button to accept the latest version. That's it — your bot will continue running automatically.</span></p>
        </td>
    </tr>
    </table>
</td>
</tr>

<!-- Help -->
<tr>
<td style="padding: 0 40px 36px;">
    <div style="background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.07); border-radius: 12px; padding: 18px 22px; text-align: center;">
        <p style="margin: 0; font-size: 14px; color: #8892a4; line-height: 1.6;">Questions or trouble updating? Reply to this email or visit your dashboard at <a href="{domain_url}/dashboard" style="color: #00c853; text-decoration: none;">{domain_url}/dashboard</a>.</p>
    </div>
</td>
</tr>
"""

    html_body = _email_wrapper(inner_html, domain_url)

    text_body = (
        "InsuranceGrokBot — App Update Available\n\n"
        "A new version of InsuranceGrokBot has been published to the GoHighLevel Marketplace.\n"
        "Please update the app in Lead Connector to stay current.\n\n"
        + (f"What's New:\n{update_notes}\n\n" if update_notes else "")
        + "How to Update:\n"
        "1. Log into Lead Connector at https://app.leadconnectorhq.com\n"
        "2. Go to App Marketplace (left sidebar)\n"
        "3. Click the Installed Apps tab\n"
        "4. Find InsuranceGrokBot and click on it\n"
        "5. Click Update\n\n"
        "Questions? Reply to this email or visit your dashboard:\n"
        f"{domain_url}/dashboard\n\n"
        "- InsuranceGrokBot Team"
    )

    return html_body, text_body
