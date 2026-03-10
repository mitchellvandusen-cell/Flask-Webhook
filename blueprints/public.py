# blueprints/public.py — Public / marketing page routes
#
# All routes here are unauthenticated, read-only marketing and informational pages.
# No database writes; no login_required.
# Exception: /uninstall-feedback accepts POST for feedback submission.

import logging

from flask import Blueprint, render_template, redirect, url_for, flash, request
from crm_adapters.factory import list_available_crms, CRM_DISPLAY_NAMES
from forms import ReviewForm
from db import get_uninstall_feedback, save_uninstall_feedback
from send_email_api import send_email_via_api

logger = logging.getLogger(__name__)

public_bp = Blueprint('public', __name__)


# ── Core marketing pages ──────────────────────────────────────────────────────

@public_bp.route("/")
def home():
    return render_template('home.html')


@public_bp.route("/comparison")
def comparison():
    return render_template('comparison.html')


@public_bp.route("/comparison/text-drip")
def comparison_text_drip():
    return render_template('comparison-text-drip.html')


@public_bp.route("/dialer")
def dialer():
    return render_template('dialer.html')


@public_bp.route("/comparison/dialers")
def comparison_dialers():
    return render_template('comparison-dialers.html')


@public_bp.route("/getting-started")
def getting_started():
    return render_template('getting-started.html')


@public_bp.route("/a2p-guide")
def a2p_guide():
    return render_template('a2p-guide.html')


@public_bp.route("/about")
def about():
    return render_template('about.html')


@public_bp.route("/affiliate")
def affiliate():
    return render_template('affiliate.html')


@public_bp.route("/support")
def support_page():
    """Self-service support and troubleshooting hub."""
    return render_template('support.html')


@public_bp.route("/setup-guide")
def setup_guide():
    """Comprehensive step-by-step setup guide."""
    return render_template('setup-guide.html')


@public_bp.route("/faq")
def faq():
    return render_template('faq.html')


@public_bp.route("/integrations")
def integrations_page():
    """Public-facing integrations page showing supported CRM platforms."""
    crms = list_available_crms()
    return render_template('integrations.html', crms=crms, crm_names=CRM_DISPLAY_NAMES)


# ── Legal / policy pages ──────────────────────────────────────────────────────

@public_bp.route("/disclaimers")
def disclaimers():
    return render_template('disclaimers.html')


@public_bp.route("/terms")
def terms():
    return render_template('terms.html')


@public_bp.route("/contact")
def contact():
    return render_template('contact.html')


@public_bp.route("/privacy")
def privacy():
    return render_template('privacy.html')


# ── Reviews ───────────────────────────────────────────────────────────────────

@public_bp.route("/reviews", methods=["GET", "POST"])
def reviews():
    form = ReviewForm()

    if form.validate_on_submit():
        flash("Thank you! Your review has been submitted for approval.", "success")
        return redirect(url_for('public.reviews'))

    all_reviews = [
        {"name": "Sarah Jenkins", "role": "Agency Owner", "text": "This bot literally saved my business. I went from booking 2 appointments a week to 15.", "stars": 5},
        {"name": "Mike Ross", "role": "Solo Agent", "text": "It works okay, but I had some issues with the setup.", "stars": 3},
        {"name": "David K.", "role": "Life Insurance Broker", "text": "I was skeptical about the AI, but it handles objections better than my human setters.", "stars": 5},
        {"name": "Emily Chen", "role": "Marketing Director", "text": "Good tool, decent price. Not perfect though.", "stars": 4},
        {"name": "Marcus T.", "role": "Independent Agent", "text": "The integration is seamless. It feels native to Lead Connector.", "stars": 5},
        {"name": "Jason V.", "role": "Independent Agent", "text": "I've tried every bot on the market. This is the only one that understands underwriting.", "stars": 5}
    ]

    visible_reviews = [r for r in all_reviews if r['stars'] == 5]
    return render_template('reviews.html', reviews=visible_reviews, form=form)


# ── Uninstall feedback ────────────────────────────────────────────────────────

UNINSTALL_REASONS = [
    ("didnt_know_subscription", "Didn't know it was subscription based"),
    ("too_complicated", "Too complicated"),
    ("too_expensive", "Too expensive"),
    ("not_my_industry", "Not my industry"),
    ("didnt_know_next", "Didn't know what to do next"),
    ("other", "Other"),
]


@public_bp.route("/uninstall-feedback", methods=["GET", "POST"])
def uninstall_feedback():
    """Public feedback page for users who uninstalled the app."""
    feedback_id = request.args.get("id") or request.form.get("id")
    if not feedback_id:
        return render_template("uninstall-feedback.html", valid=False)

    try:
        feedback_id = int(feedback_id)
    except (ValueError, TypeError):
        return render_template("uninstall-feedback.html", valid=False)

    record = get_uninstall_feedback(feedback_id)
    if not record:
        return render_template("uninstall-feedback.html", valid=False)

    # Already submitted
    if record.get("feedback_submitted_at"):
        return render_template("uninstall-feedback.html", valid=True, already_submitted=True)

    if request.method == "POST":
        reason = request.form.get("reason", "").strip()
        other_text = request.form.get("other_text", "").strip()[:1000]

        if reason:
            save_uninstall_feedback(feedback_id, reason, other_text)

            # Send notification to mitch with the feedback
            reason_label = dict(UNINSTALL_REASONS).get(reason, reason)
            try:
                notify_html = f'''<html><body style="font-family: Arial, sans-serif; background: #f5f5f5; padding: 20px;">
<div style="max-width: 500px; margin: 0 auto; background: #fff; border-radius: 8px; padding: 24px; border-left: 4px solid #00c853;">
    <h2 style="margin: 0 0 12px; color: #333;">Uninstall Feedback Received</h2>
    <table style="width: 100%; font-size: 14px; color: #555;">
        <tr><td style="padding: 4px 0; font-weight: bold;">User:</td><td>{record.get("user_name") or "Unknown"} ({record.get("user_email") or "no email"})</td></tr>
        <tr><td style="padding: 4px 0; font-weight: bold;">Location:</td><td>{record.get("location_id") or "N/A"}</td></tr>
        <tr><td style="padding: 4px 0; font-weight: bold;">Reason:</td><td style="color: #ff6b35; font-weight: bold;">{reason_label}</td></tr>
        {"<tr><td style='padding: 4px 0; font-weight: bold;'>Details:</td><td>" + other_text + "</td></tr>" if other_text else ""}
    </table>
</div>
</body></html>'''
                send_email_via_api(
                    to_email="mitch@insurancegrokbot.com",
                    subject=f"Uninstall Feedback: {reason_label} — {record.get('user_name') or 'Unknown'}",
                    html_body=notify_html,
                    text_body=f"Reason: {reason_label}. Details: {other_text or 'N/A'}. User: {record.get('user_name')} ({record.get('user_email')})",
                )
            except Exception as e:
                logger.error(f"Failed to send feedback notification: {e}")

            return render_template("uninstall-feedback.html", valid=True, submitted=True)

    return render_template("uninstall-feedback.html", valid=True,
                           feedback_id=feedback_id, reasons=UNINSTALL_REASONS)
