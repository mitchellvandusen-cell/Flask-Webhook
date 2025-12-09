import logging
from datetime import datetime

from flask import request, jsonify

from app import app
from grok import generate_nepq_response

logger = logging.getLogger(__name__)


@app.route('/webhook', methods=['POST'])
def webhook():
    """
    Webhook endpoint to receive CRM data and return NEPQ-style responses.
    
    Expected JSON payload:
    {
        "first_name": "John",
        "message": "Hi, I'm interested in your services"
    }
    
    Returns:
    {
        "status": "success",
        "reply": "AI-generated NEPQ response",
        "metadata": {
            "processed_at": "ISO timestamp",
            "recipient": "first_name"
        }
    }
    """
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
        
        reply = generate_nepq_response(first_name, message)
        
        response_data = {
            "status": "success",
            "reply": reply,
            "metadata": {
                "processed_at": datetime.utcnow().isoformat() + "Z",
                "recipient": first_name
            }
        }
        
        logger.info(f"Successfully processed webhook for {first_name}")
        return jsonify(response_data), 200
        
    except Exception as e:
        logger.error(f"Error processing webhook: {str(e)}")
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
                    "message": "string (required)"
                },
                "response": {
                    "status": "success|error",
                    "reply": "AI-generated response",
                    "metadata": {
                        "processed_at": "ISO timestamp",
                        "recipient": "first_name"
                    }
                }
            },
            "GET /health": {
                "description": "Health check endpoint"
            }
        }
    }), 200
