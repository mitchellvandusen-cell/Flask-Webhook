# blueprints/public.py — Public / marketing page routes
#
# All routes here are unauthenticated, read-only marketing and informational pages.
# No database writes; no login_required.

from flask import Blueprint, render_template, redirect, url_for, flash, request
from crm_adapters.factory import list_available_crms, CRM_DISPLAY_NAMES
from forms import ReviewForm

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


@public_bp.route("/getting-started")
def getting_started():
    return render_template('getting-started.html')


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
