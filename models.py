from datetime import datetime
from app import db


class WebhookLog(db.Model):
    """Stores all webhook requests and responses for logging and analytics."""
    id = db.Column(db.Integer, primary_key=True)
    first_name = db.Column(db.String(100), nullable=False)
    incoming_message = db.Column(db.Text, nullable=False)
    response_message = db.Column(db.Text)
    status = db.Column(db.String(20), nullable=False, default='success')
    error_message = db.Column(db.Text)
    persona_id = db.Column(db.Integer, db.ForeignKey('nepq_persona.id'), nullable=True)
    ip_address = db.Column(db.String(45))
    processing_time_ms = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    
    persona = db.relationship('NEPQPersona', backref='logs')


class ConversationHistory(db.Model):
    """Stores conversation history for context continuity."""
    id = db.Column(db.Integer, primary_key=True)
    contact_identifier = db.Column(db.String(255), nullable=False, index=True)
    first_name = db.Column(db.String(100))
    message_role = db.Column(db.String(20), nullable=False)
    message_content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class NEPQPersona(db.Model):
    """Stores different NEPQ response templates/personas."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    description = db.Column(db.Text)
    system_prompt = db.Column(db.Text, nullable=False)
    is_default = db.Column(db.Boolean, default=False)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class RateLimitEntry(db.Model):
    """Tracks rate limiting per IP address."""
    id = db.Column(db.Integer, primary_key=True)
    ip_address = db.Column(db.String(45), nullable=False, index=True)
    request_count = db.Column(db.Integer, nullable=False, default=1)
    window_start = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
