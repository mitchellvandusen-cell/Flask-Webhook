# ghl_auth.py — JWT authentication for GHL Custom JS integration
#
# Provides jwt_or_session_required decorator that allows endpoints to accept
# either a Flask session (normal dashboard) or a JWT token (GHL Custom JS).
#
# When JWT is used, a minimal User-like object is set as current_user via
# Flask-Login so existing code that references current_user.email,
# current_user.location_id, etc. continues to work unchanged.

import os
import functools
import logging

import jwt as pyjwt
from flask import request, jsonify
from flask_login import current_user, login_user

from db import User

logger = logging.getLogger(__name__)

JWT_SECRET = os.getenv('SESSION_SECRET') or os.getenv('SECRET_KEY', 'fallback-jwt-secret')


def _decode_jwt(token):
    """Decode and verify a JWT token. Returns payload dict or None."""
    try:
        return pyjwt.decode(token, JWT_SECRET, algorithms=['HS256'])
    except pyjwt.ExpiredSignatureError:
        return None
    except pyjwt.InvalidTokenError:
        return None


def _get_jwt_from_request():
    """Extract JWT from Authorization: Bearer header."""
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        return auth_header[7:]
    return None


def jwt_or_session_required(f):
    """
    Decorator that accepts either a JWT Bearer token OR a Flask session.

    When JWT is used:
    - Decodes the token to get location_id and email
    - Loads the real User object from DB via User.get(email)
    - Logs them in for the duration of this request (login_user with remember=False)
    - Sets request._ghl_jwt with the payload

    When session is used:
    - Normal Flask-Login current_user behavior
    - request._ghl_jwt is set to None
    """
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        # Try JWT first
        token = _get_jwt_from_request()
        if token:
            payload = _decode_jwt(token)
            if not payload:
                return jsonify({"error": "Invalid or expired token"}), 401

            # Load real User object so current_user works normally
            email = payload.get('email', '')
            if email:
                user = User.get(email)
                if user:
                    login_user(user, remember=False)
                else:
                    return jsonify({"error": "User not found"}), 401
            else:
                return jsonify({"error": "Invalid token payload"}), 401

            request._ghl_jwt = payload
            return f(*args, **kwargs)

        # Fall back to Flask session
        if current_user and current_user.is_authenticated:
            request._ghl_jwt = None
            return f(*args, **kwargs)

        return jsonify({"error": "Authentication required"}), 401
    return decorated
