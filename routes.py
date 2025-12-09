import logging
import hashlib
import hmac
import os
import time
from datetime import datetime, timedelta
from functools import wraps

from flask import request, jsonify, render_template, session, redirect, url_for

from app import app, db
from models import WebhookLog, ConversationHistory, NEPQPersona, RateLimitEntry
from grok import generate_nepq_response

logger = logging.getLogger(__name__)

RATE_LIMIT_REQUESTS = 60
RATE_LIMIT_WINDOW_SECONDS = 60


def check_rate_limit(ip_address: str) -> tuple[bool, int]:
    """Check if IP is rate limited. Returns (is_allowed, remaining_requests)."""
    now = datetime.utcnow()
    window_start = now - timedelta(seconds=RATE_LIMIT_WINDOW_SECONDS)
    
    entry = RateLimitEntry.query.filter_by(ip_address=ip_address).first()
    
    if not entry:
        entry = RateLimitEntry(ip_address=ip_address, request_count=1, window_start=now)
        db.session.add(entry)
        db.session.commit()
        return True, RATE_LIMIT_REQUESTS - 1
    
    if entry.window_start < window_start:
        entry.request_count = 1
        entry.window_start = now
        db.session.commit()
        return True, RATE_LIMIT_REQUESTS - 1
    
    if entry.request_count >= RATE_LIMIT_REQUESTS:
        return False, 0
    
    entry.request_count += 1
    db.session.commit()
    return True, RATE_LIMIT_REQUESTS - entry.request_count


def rate_limit(f):
    """Decorator to apply rate limiting to endpoints."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        if ip:
            ip = ip.split(',')[0].strip()
        
        allowed, remaining = check_rate_limit(ip)
        
        if not allowed:
            logger.warning(f"Rate limit exceeded for IP: {ip}")
            return jsonify({
                "status": "error",
                "error": "Rate limit exceeded. Please try again later.",
                "code": "RATE_LIMIT_EXCEEDED"
            }), 429
        
        response = f(*args, **kwargs)
        return response
    return decorated_function


def verify_webhook_signature(f):
    """Decorator to verify webhook signature if WEBHOOK_SECRET is configured."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        webhook_secret = os.environ.get('WEBHOOK_SECRET')
        
        if not webhook_secret:
            return f(*args, **kwargs)
        
        signature = request.headers.get('X-Webhook-Signature')
        
        if not signature:
            logger.warning("Missing webhook signature")
            return jsonify({
                "status": "error",
                "error": "Missing webhook signature",
                "code": "MISSING_SIGNATURE"
            }), 401
        
        payload = request.get_data()
        expected_signature = hmac.new(
            webhook_secret.encode(),
            payload,
            hashlib.sha256
        ).hexdigest()
        
        if not hmac.compare_digest(signature, expected_signature):
            logger.warning("Invalid webhook signature")
            return jsonify({
                "status": "error",
                "error": "Invalid webhook signature",
                "code": "INVALID_SIGNATURE"
            }), 401
        
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    """Decorator to require admin authentication."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('admin_authenticated'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated_function


def get_active_persona(persona_name: str = None):
    """Get the active persona by name or return the default."""
    if persona_name:
        persona = NEPQPersona.query.filter_by(name=persona_name, is_active=True).first()
        if persona:
            return persona
    
    default_persona = NEPQPersona.query.filter_by(is_default=True, is_active=True).first()
    return default_persona


def get_conversation_history(contact_identifier: str, limit: int = 10):
    """Get recent conversation history for a contact."""
    history = ConversationHistory.query.filter_by(
        contact_identifier=contact_identifier
    ).order_by(ConversationHistory.created_at.desc()).limit(limit).all()
    
    messages = []
    for h in reversed(history):
        messages.append({"role": h.message_role, "content": h.message_content})
    
    return messages


def save_conversation(contact_identifier: str, first_name: str, user_message: str, assistant_response: str):
    """Save conversation to history."""
    user_entry = ConversationHistory(
        contact_identifier=contact_identifier,
        first_name=first_name,
        message_role="user",
        message_content=user_message
    )
    assistant_entry = ConversationHistory(
        contact_identifier=contact_identifier,
        first_name=first_name,
        message_role="assistant",
        message_content=assistant_response
    )
    db.session.add(user_entry)
    db.session.add(assistant_entry)
    db.session.commit()


@app.route('/webhook', methods=['POST'])
@verify_webhook_signature
@rate_limit
def webhook():
    """
    Webhook endpoint to receive CRM data and return NEPQ-style responses.
    
    Expected JSON payload:
    {
        "first_name": "John",
        "message": "Hi, I'm interested in your services",
        "contact_id": "optional-unique-identifier",
        "persona": "optional-persona-name"
    }
    """
    start_time = time.time()
    ip_address = request.headers.get('X-Forwarded-For', request.remote_addr)
    if ip_address:
        ip_address = ip_address.split(',')[0].strip()
    
    try:
        if not request.is_json:
            logger.warning("Received non-JSON request")
            return jsonify({
                "status": "error",
                "error": "Content-Type must be application/json",
                "code": "INVALID_CONTENT_TYPE"
            }), 400
        
        data = request.get_json()
        
        if not data:
            logger.warning("Empty request body received")
            return jsonify({
                "status": "error",
                "error": "Request body cannot be empty",
                "code": "EMPTY_BODY"
            }), 400
        
        first_name = data.get('first_name')
        message = data.get('message')
        contact_id = data.get('contact_id', first_name)
        persona_name = data.get('persona')
        
        if not first_name:
            logger.warning("Missing first_name in request")
            return jsonify({
                "status": "error",
                "error": "Missing required field: first_name",
                "code": "MISSING_FIRST_NAME"
            }), 400
        
        if not message:
            logger.warning("Missing message in request")
            return jsonify({
                "status": "error",
                "error": "Missing required field: message",
                "code": "MISSING_MESSAGE"
            }), 400
        
        logger.info(f"Processing webhook for {first_name}: {message[:50]}...")
        
        persona = get_active_persona(persona_name)
        system_prompt = persona.system_prompt if persona else None
        
        conversation_history = get_conversation_history(contact_id)
        
        reply = generate_nepq_response(first_name, message, system_prompt, conversation_history)
        
        save_conversation(contact_id, first_name, message, reply)
        
        processing_time = int((time.time() - start_time) * 1000)
        
        log_entry = WebhookLog(
            first_name=first_name,
            incoming_message=message,
            response_message=reply,
            status='success',
            persona_id=persona.id if persona else None,
            ip_address=ip_address,
            processing_time_ms=processing_time
        )
        db.session.add(log_entry)
        db.session.commit()
        
        response_data = {
            "status": "success",
            "reply": reply,
            "metadata": {
                "processed_at": datetime.utcnow().isoformat() + "Z",
                "recipient": first_name,
                "persona": persona.name if persona else "default",
                "processing_time_ms": processing_time
            }
        }
        
        logger.info(f"Successfully processed webhook for {first_name}")
        return jsonify(response_data), 200
        
    except Exception as e:
        processing_time = int((time.time() - start_time) * 1000)
        logger.error(f"Error processing webhook: {str(e)}")
        
        try:
            log_entry = WebhookLog(
                first_name=data.get('first_name', 'unknown') if data else 'unknown',
                incoming_message=data.get('message', '') if data else '',
                status='error',
                error_message=str(e),
                ip_address=ip_address,
                processing_time_ms=processing_time
            )
            db.session.add(log_entry)
            db.session.commit()
        except:
            pass
        
        return jsonify({
            "status": "error",
            "error": str(e),
            "code": "PROCESSING_ERROR"
        }), 500


@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint for monitoring."""
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat() + "Z"
    }), 200


@app.route('/', methods=['GET'])
def index():
    """Root endpoint with API documentation."""
    return jsonify({
        "service": "NEPQ Webhook API",
        "version": "1.0.0",
        "endpoints": {
            "POST /webhook": {
                "description": "Process incoming SMS and generate NEPQ response",
                "payload": {
                    "first_name": "string (required)",
                    "message": "string (required)",
                    "contact_id": "string (optional) - unique identifier for conversation history",
                    "persona": "string (optional) - name of NEPQ persona to use"
                },
                "response": {
                    "status": "success|error",
                    "reply": "AI-generated response",
                    "metadata": {
                        "processed_at": "ISO timestamp",
                        "recipient": "first_name",
                        "persona": "persona name used",
                        "processing_time_ms": "processing time"
                    }
                }
            },
            "GET /health": {
                "description": "Health check endpoint"
            },
            "GET /admin": {
                "description": "Admin dashboard for logs and analytics"
            }
        }
    }), 200


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    """Admin login page."""
    error = None
    if request.method == 'POST':
        password = request.form.get('password')
        admin_password = os.environ.get('ADMIN_PASSWORD')
        
        if not admin_password:
            error = "Admin access is not configured. Set ADMIN_PASSWORD environment variable."
        elif password == admin_password:
            session['admin_authenticated'] = True
            return redirect(url_for('admin_dashboard'))
        else:
            error = "Invalid password"
    
    return render_template('login.html', error=error)


@app.route('/admin/logout')
def admin_logout():
    """Admin logout."""
    session.pop('admin_authenticated', None)
    return redirect(url_for('admin_login'))


@app.route('/admin')
@admin_required
def admin_dashboard():
    """Admin dashboard to view webhook logs and analytics."""
    page = request.args.get('page', 1, type=int)
    per_page = 20
    
    logs = WebhookLog.query.order_by(WebhookLog.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )
    
    total_requests = WebhookLog.query.count()
    success_count = WebhookLog.query.filter_by(status='success').count()
    error_count = WebhookLog.query.filter_by(status='error').count()
    
    avg_time = db.session.query(db.func.avg(WebhookLog.processing_time_ms)).filter(
        WebhookLog.processing_time_ms.isnot(None)
    ).scalar() or 0
    
    personas = NEPQPersona.query.filter_by(is_active=True).all()
    
    return render_template('admin.html',
        logs=logs,
        total_requests=total_requests,
        success_count=success_count,
        error_count=error_count,
        avg_processing_time=round(avg_time, 2),
        personas=personas
    )


@app.route('/admin/personas', methods=['GET', 'POST'])
@admin_required
def manage_personas():
    """Manage NEPQ personas."""
    if request.method == 'POST':
        data = request.form
        name = data.get('name')
        description = data.get('description')
        system_prompt = data.get('system_prompt')
        is_default = data.get('is_default') == 'on'
        
        if is_default:
            NEPQPersona.query.update({NEPQPersona.is_default: False})
        
        persona = NEPQPersona(
            name=name,
            description=description,
            system_prompt=system_prompt,
            is_default=is_default
        )
        db.session.add(persona)
        db.session.commit()
        
        return jsonify({"status": "success", "id": persona.id}), 201
    
    personas = NEPQPersona.query.all()
    return render_template('personas.html', personas=personas)
