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


def _build_install_welcome_email(name: str, domain_url: str) -> str:
    """Build premium welcome email for marketplace install — guides them through OAuth setup."""
    inner = f'''
<tr>
<td style="padding: 0 40px 30px;">
    <h1 style="margin: 0 0 8px; font-size: 28px; font-weight: 800; color: #ffffff; line-height: 1.2;">
        Welcome aboard, {name}!
    </h1>
    <p style="margin: 0; font-size: 16px; color: #aaa; line-height: 1.5;">
        You just installed <strong style="color: #00c853;">InsuranceGrokBot</strong> &mdash; your AI-powered
        insurance sales assistant that works your leads 24/7.
    </p>
</td>
</tr>

<!-- Setup Required Notice -->
<tr>
<td style="padding: 0 40px 25px;">
    <table cellpadding="0" cellspacing="0" width="100%" style="background: rgba(255,107,53,0.08); border: 1px solid rgba(255,107,53,0.2); border-radius: 12px;">
    <tr>
    <td style="padding: 20px 24px;">
        <p style="margin: 0 0 4px; font-size: 15px; font-weight: 700; color: #ff6b35;">
            One Step Left to Activate Your Bot
        </p>
        <p style="margin: 0; font-size: 14px; color: #ccc; line-height: 1.5;">
            Click the button below to connect your Lead Connector CRM. This authorizes InsuranceGrokBot
            to respond to your leads, book appointments, and manage conversations automatically.
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
            Connect Your CRM Now &rarr;
        </a>
    </td>
    </tr>
    </table>
</td>
</tr>

<!-- What Happens Next -->
<tr>
<td style="padding: 0 40px 25px;">
    <h2 style="margin: 0 0 16px; font-size: 18px; font-weight: 700; color: #fff;">What Happens After You Connect:</h2>
    <table cellpadding="0" cellspacing="0" width="100%">
    <tr>
        <td style="padding: 10px 0; border-bottom: 1px solid rgba(255,255,255,0.06);">
            <table cellpadding="0" cellspacing="0"><tr>
                <td style="width: 36px; vertical-align: top;">
                    <div style="width: 28px; height: 28px; background: rgba(0,200,83,0.15); border-radius: 8px; text-align: center; line-height: 28px; font-size: 14px;">1</div>
                </td>
                <td style="padding-left: 12px;">
                    <p style="margin: 0; font-size: 14px; color: #ddd;"><strong style="color: #fff;">Instant Lead Response</strong> &mdash; Bot replies within 5 seconds, 24/7</p>
                </td>
            </tr></table>
        </td>
    </tr>
    <tr>
        <td style="padding: 10px 0; border-bottom: 1px solid rgba(255,255,255,0.06);">
            <table cellpadding="0" cellspacing="0"><tr>
                <td style="width: 36px; vertical-align: top;">
                    <div style="width: 28px; height: 28px; background: rgba(0,200,83,0.15); border-radius: 8px; text-align: center; line-height: 28px; font-size: 14px;">2</div>
                </td>
                <td style="padding-left: 12px;">
                    <p style="margin: 0; font-size: 14px; color: #ddd;"><strong style="color: #fff;">Smart Qualification</strong> &mdash; Asks the right insurance questions automatically</p>
                </td>
            </tr></table>
        </td>
    </tr>
    <tr>
        <td style="padding: 10px 0;">
            <table cellpadding="0" cellspacing="0"><tr>
                <td style="width: 36px; vertical-align: top;">
                    <div style="width: 28px; height: 28px; background: rgba(0,200,83,0.15); border-radius: 8px; text-align: center; line-height: 28px; font-size: 14px;">3</div>
                </td>
                <td style="padding-left: 12px;">
                    <p style="margin: 0; font-size: 14px; color: #ddd;"><strong style="color: #fff;">Auto Book Appointments</strong> &mdash; Checks your calendar and books meetings</p>
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
        <td align="center" style="padding: 20px; width: 33%; border-right: 1px solid rgba(255,255,255,0.06);">
            <div style="font-size: 28px; font-weight: 800; color: #00c853;">5s</div>
            <div style="font-size: 12px; color: #888; margin-top: 4px;">Response Time</div>
        </td>
        <td align="center" style="padding: 20px; width: 34%; border-right: 1px solid rgba(255,255,255,0.06);">
            <div style="font-size: 28px; font-weight: 800; color: #00c853;">24/7</div>
            <div style="font-size: 12px; color: #888; margin-top: 4px;">Always On</div>
        </td>
        <td align="center" style="padding: 20px; width: 33%;">
            <div style="font-size: 28px; font-weight: 800; color: #00c853;">Auto</div>
            <div style="font-size: 12px; color: #888; margin-top: 4px;">Booking</div>
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
        action_text = "Activate My Bot"
        action_url = dashboard
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
        You signed up for InsuranceGrokBot 24 hours ago. You're almost there — your AI-powered sales assistant is configured and ready to start responding to leads, qualifying prospects, and booking appointments on your calendar.
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

<!-- Stats Row -->
<tr>
<td style="padding: 0 40px 25px;">
    <table cellpadding="0" cellspacing="0" width="100%">
    <tr>
        <td width="33%" align="center" style="padding: 16px 8px; background: rgba(0,200,83,0.06); border-radius: 12px 0 0 12px; border: 1px solid rgba(0,200,83,0.1);">
            <div style="font-size: 28px; font-weight: 800; color: #00c853; line-height: 1;">5s</div>
            <div style="font-size: 11px; color: #888; margin-top: 4px; text-transform: uppercase; letter-spacing: 0.5px;">Response Time</div>
        </td>
        <td width="34%" align="center" style="padding: 16px 8px; background: rgba(0,200,83,0.06); border-left: 1px solid rgba(0,200,83,0.1); border-right: 1px solid rgba(0,200,83,0.1); border-top: 1px solid rgba(0,200,83,0.1); border-bottom: 1px solid rgba(0,200,83,0.1);">
            <div style="font-size: 28px; font-weight: 800; color: #00c853; line-height: 1;">24/7</div>
            <div style="font-size: 11px; color: #888; margin-top: 4px; text-transform: uppercase; letter-spacing: 0.5px;">Availability</div>
        </td>
        <td width="33%" align="center" style="padding: 16px 8px; background: rgba(0,200,83,0.06); border-radius: 0 12px 12px 0; border: 1px solid rgba(0,200,83,0.1);">
            <div style="font-size: 28px; font-weight: 800; color: #00c853; line-height: 1;">Auto</div>
            <div style="font-size: 11px; color: #888; margin-top: 4px; text-transform: uppercase; letter-spacing: 0.5px;">Booking</div>
        </td>
    </tr>
    </table>
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
    <p style="margin: 14px 0 0; font-size: 13px; color: #666;">Takes less than 2 minutes to complete</p>
</td>
</tr>

<!-- Testimonial -->
<tr>
<td style="padding: 0 40px 25px;">
    <div style="background: rgba(255,255,255,0.02); border-left: 3px solid #00c853; padding: 16px 20px; border-radius: 0 12px 12px 0;">
        <p style="margin: 0 0 8px; font-size: 14px; color: #ccc; font-style: italic; line-height: 1.6;">
            "I had 3 appointments booked by the end of my first week without lifting a finger. GrokBot qualifies leads better than most humans."
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
        Need help setting up? <a href="{domain_url}/support" style="color: #00c853; text-decoration: none; font-weight: 600;">Visit our support page</a> or just reply to this email.
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
        action_text = "Subscribe & Activate Now"
        action_url = dashboard
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
        It's been 3 days since you created your InsuranceGrokBot account. Every hour your bot isn't active, new leads are going unworked, follow-ups are being missed, and potential clients are moving on to the next agent who responds first.
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
                            <span style="color: #fff; font-weight: 600; font-size: 14px;">Leads go cold in 5 minutes</span><br>
                            <span style="color: #999; font-size: 13px;">78% of buyers choose the agent who responds first. Your bot responds in seconds.</span>
                        </td>
                    </tr></table>
                </td>
            </tr>
            <tr>
                <td style="padding: 10px 0; border-bottom: 1px solid rgba(255,255,255,0.05);">
                    <table cellpadding="0" cellspacing="0" width="100%"><tr>
                        <td width="40" style="vertical-align: top;"><span style="font-size: 20px;">&#128197;</span></td>
                        <td style="vertical-align: top;">
                            <span style="color: #fff; font-weight: 600; font-size: 14px;">Missed appointments = missed commission</span><br>
                            <span style="color: #999; font-size: 13px;">GrokBot qualifies and books automatically — no back-and-forth texting.</span>
                        </td>
                    </tr></table>
                </td>
            </tr>
            <tr>
                <td style="padding: 10px 0;">
                    <table cellpadding="0" cellspacing="0" width="100%"><tr>
                        <td width="40" style="vertical-align: top;"><span style="font-size: 20px;">&#127769;</span></td>
                        <td style="vertical-align: top;">
                            <span style="color: #fff; font-weight: 600; font-size: 14px;">Nights and weekends covered</span><br>
                            <span style="color: #999; font-size: 13px;">Leads come in at 11 PM. Your bot is there. Without it, they text your competitor.</span>
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
    <p style="margin: 14px 0 0; font-size: 13px; color: #666;">Your competitors are already using AI. Don't fall behind.</p>
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
                &#10060; Manual follow-up texting<br>
                &#10060; Missed after-hours leads<br>
                &#10060; No qualifying before calls
            </p>
        </td>
        <td width="4%">&nbsp;</td>
        <td width="48%" style="background: rgba(0,200,83,0.06); border: 1px solid rgba(0,200,83,0.15); border-radius: 12px; padding: 20px; vertical-align: top;">
            <p style="margin: 0 0 10px; font-size: 12px; font-weight: 700; color: #00c853; text-transform: uppercase; letter-spacing: 1px;">With GrokBot</p>
            <p style="margin: 0; font-size: 13px; color: #999; line-height: 1.7;">
                &#10004; 5-second response time<br>
                &#10004; Automated smart follow-ups<br>
                &#10004; 24/7 lead coverage<br>
                &#10004; Pre-qualified appointments
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
    """Build the post-OAuth welcome email — sent after CRM connection is complete."""
    inner = f'''
<tr>
<td align="center" style="padding: 0 40px 10px;">
    <div style="width: 80px; height: 80px; border-radius: 50%; background: linear-gradient(135deg, rgba(0,200,83,0.15) 0%, rgba(0,200,83,0.05) 100%); border: 2px solid rgba(0,200,83,0.25); margin: 0 auto 20px; line-height: 80px; text-align: center;">
        <span style="font-size: 36px;">&#127881;</span>
    </div>
    <h1 style="margin: 0 0 8px; font-size: 28px; font-weight: 800; color: #ffffff; line-height: 1.2;">Welcome to InsuranceGrokBot</h1>
    <p style="margin: 0; font-size: 16px; color: #aaa; line-height: 1.5;">Your AI-powered insurance sales assistant is ready</p>
</td>
</tr>

<tr>
<td style="padding: 25px 40px 15px;">
    <p style="margin: 0; font-size: 16px; color: #ddd; line-height: 1.7;">Hi {user_name},</p>
    <p style="margin: 12px 0 0; font-size: 15px; color: #bbb; line-height: 1.7;">
        Your Lead Connector account has been successfully connected. GrokBot is configured and standing by to handle your leads, qualify prospects with real insurance knowledge, and book appointments directly on your calendar.
    </p>
</td>
</tr>

<!-- Quick Links -->
<tr>
<td style="padding: 10px 40px 20px;">
    <div style="background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08); border-radius: 16px; padding: 24px;">
        <p style="margin: 0 0 16px; font-size: 13px; font-weight: 700; color: #00c853; text-transform: uppercase; letter-spacing: 1.5px;">Get Started</p>
        <table cellpadding="0" cellspacing="0" width="100%">
            <tr>
                <td style="padding: 12px 0; border-bottom: 1px solid rgba(255,255,255,0.05);">
                    <table cellpadding="0" cellspacing="0" width="100%"><tr>
                        <td width="36" style="vertical-align: middle;"><span style="font-size: 18px;">&#128640;</span></td>
                        <td style="vertical-align: middle;">
                            <a href="{dashboard_link}" style="color: #00c853; font-weight: 600; text-decoration: none; font-size: 15px;">Your Dashboard</a>
                            <span style="color: #888; font-size: 13px;"> &mdash; Configure your bot, set your calendar, customize settings</span>
                        </td>
                    </tr></table>
                </td>
            </tr>
            <tr>
                <td style="padding: 12px 0; border-bottom: 1px solid rgba(255,255,255,0.05);">
                    <table cellpadding="0" cellspacing="0" width="100%"><tr>
                        <td width="36" style="vertical-align: middle;"><span style="font-size: 18px;">&#128172;</span></td>
                        <td style="vertical-align: middle;">
                            <a href="{domain_url}/support" style="color: #00c853; font-weight: 600; text-decoration: none; font-size: 15px;">Support & FAQ</a>
                            <span style="color: #888; font-size: 13px;"> &mdash; Questions about setup, integration, or billing</span>
                        </td>
                    </tr></table>
                </td>
            </tr>
            <tr>
                <td style="padding: 12px 0;">
                    <table cellpadding="0" cellspacing="0" width="100%"><tr>
                        <td width="36" style="vertical-align: middle;"><span style="font-size: 18px;">&#128203;</span></td>
                        <td style="vertical-align: middle;">
                            <a href="{domain_url}/onboarding-status" style="color: #00c853; font-weight: 600; text-decoration: none; font-size: 15px;">Onboarding Status</a>
                            <span style="color: #888; font-size: 13px;"> &mdash; Track your setup progress in real time</span>
                        </td>
                    </tr></table>
                </td>
            </tr>
        </table>
    </div>
</td>
</tr>

<!-- What GrokBot Does -->
<tr>
<td style="padding: 5px 40px 20px;">
    <table cellpadding="0" cellspacing="0" width="100%">
    <tr>
        <td width="33%" align="center" style="padding: 16px 8px; background: rgba(0,200,83,0.06); border-radius: 12px 0 0 12px; border: 1px solid rgba(0,200,83,0.1);">
            <div style="font-size: 24px; margin-bottom: 6px;">&#9889;</div>
            <div style="font-size: 13px; color: #ccc; font-weight: 600;">Instant Replies</div>
            <div style="font-size: 11px; color: #888; margin-top: 2px;">5-second response</div>
        </td>
        <td width="34%" align="center" style="padding: 16px 8px; background: rgba(0,200,83,0.06); border: 1px solid rgba(0,200,83,0.1);">
            <div style="font-size: 24px; margin-bottom: 6px;">&#128218;</div>
            <div style="font-size: 13px; color: #ccc; font-weight: 600;">Expert Knowledge</div>
            <div style="font-size: 11px; color: #888; margin-top: 2px;">Real insurance IQ</div>
        </td>
        <td width="33%" align="center" style="padding: 16px 8px; background: rgba(0,200,83,0.06); border-radius: 0 12px 12px 0; border: 1px solid rgba(0,200,83,0.1);">
            <div style="font-size: 24px; margin-bottom: 6px;">&#128197;</div>
            <div style="font-size: 13px; color: #ccc; font-weight: 600;">Auto Booking</div>
            <div style="font-size: 11px; color: #888; margin-top: 2px;">Straight to calendar</div>
        </td>
    </tr>
    </table>
</td>
</tr>

<!-- CTA -->
<tr>
<td align="center" style="padding: 10px 40px 25px;">
    <table cellpadding="0" cellspacing="0"><tr>
        <td style="background: linear-gradient(135deg, #00c853 0%, #00e676 100%); border-radius: 14px; box-shadow: 0 4px 20px rgba(0,200,83,0.3);">
            <a href="{dashboard_link}" style="display: inline-block; padding: 18px 48px; color: #000000; font-size: 17px; font-weight: 800; text-decoration: none; letter-spacing: -0.3px;">
                Open My Dashboard &rarr;
            </a>
        </td>
    </tr></table>
</td>
</tr>

<tr>
<td style="padding: 0 40px 10px;">
    <p style="margin: 0; font-size: 14px; color: #888; line-height: 1.6; text-align: center;">
        Questions? <a href="{domain_url}/support" style="color: #00c853; text-decoration: none; font-weight: 600;">Visit support</a> or just reply to this email.
    </p>
</td>
</tr>
'''
    return _email_wrapper(inner, domain_url)
