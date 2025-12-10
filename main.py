from flask import Flask, request, jsonify
import os
import random
import string
import logging
import requests
from datetime import datetime, timedelta
from openai import OpenAI

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET")

GHL_BASE_URL = "https://services.leadconnectorhq.com"

_client = None

def get_client():
    global _client
    if _client is None:
        api_key = os.environ.get("XAI_API_KEY")
        if not api_key:
            raise ValueError("XAI_API_KEY environment variable is not set")
        _client = OpenAI(base_url="https://api.x.ai/v1", api_key=api_key)
    return _client

def generate_confirmation_code():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))

def get_ghl_credentials(data=None):
    """
    Get GHL credentials with priority:
    1. Request body (ghl_api_key, ghl_location_id) - for multi-tenant via webhooks
    2. Environment variables - for your own default setup
    """
    if data is None:
        data = {}
    
    api_key = data.get('ghl_api_key') or os.environ.get("GHL_API_KEY")
    location_id = data.get('ghl_location_id') or os.environ.get("GHL_LOCATION_ID")
    return api_key, location_id

def get_ghl_headers(api_key):
    return {
        "Authorization": f"Bearer {api_key}",
        "Version": "2021-07-28",
        "Content-Type": "application/json"
    }

def send_sms_via_ghl(contact_id, message, api_key, location_id):
    """Send SMS to a contact via GoHighLevel Conversations API"""
    if not api_key or not location_id:
        logger.error("GHL credentials not set")
        return {"success": False, "error": "GHL credentials not set. Provide X-GHL-API-Key and X-GHL-Location-ID headers."}
    
    url = f"{GHL_BASE_URL}/conversations/messages"
    payload = {
        "type": "SMS",
        "contactId": contact_id,
        "locationId": location_id,
        "message": message
    }
    
    try:
        response = requests.post(url, headers=get_ghl_headers(api_key), json=payload)
        response.raise_for_status()
        logger.info(f"SMS sent successfully to contact {contact_id}")
        return {"success": True, "data": response.json()}
    except requests.RequestException as e:
        logger.error(f"Failed to send SMS: {e}")
        error_detail = str(e)
        if hasattr(e, 'response') and e.response is not None:
            try:
                error_detail = e.response.json()
            except:
                error_detail = e.response.text
        return {"success": False, "error": error_detail}

def create_ghl_appointment(contact_id, calendar_id, start_time, end_time, api_key, location_id, title="Life Insurance Consultation"):
    """Create an appointment in GoHighLevel calendar"""
    if not api_key:
        logger.error("GHL_API_KEY not set")
        return None
    
    url = f"{GHL_BASE_URL}/calendars/events"
    payload = {
        "calendarId": calendar_id,
        "locationId": location_id,
        "contactId": contact_id,
        "startTime": start_time,
        "endTime": end_time,
        "title": title,
        "appointmentStatus": "confirmed"
    }
    
    try:
        response = requests.post(url, headers=get_ghl_headers(api_key), json=payload)
        response.raise_for_status()
        logger.info(f"Appointment created for contact {contact_id}")
        return response.json()
    except requests.RequestException as e:
        logger.error(f"Failed to create appointment: {e}")
        return None

def get_contact_info(contact_id, api_key):
    """Get contact details from GoHighLevel"""
    if not api_key:
        logger.error("GHL_API_KEY not set")
        return None
    
    url = f"{GHL_BASE_URL}/contacts/{contact_id}"
    
    try:
        response = requests.get(url, headers=get_ghl_headers(api_key))
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        logger.error(f"Failed to get contact: {e}")
        return None

def update_contact_stage(opportunity_id, stage_id, api_key):
    """Update an existing opportunity's stage in GoHighLevel"""
    if not api_key:
        logger.error("GHL_API_KEY not set")
        return None
    
    url = f"{GHL_BASE_URL}/opportunities/{opportunity_id}"
    payload = {
        "stageId": stage_id
    }
    
    try:
        response = requests.put(url, headers=get_ghl_headers(api_key), json=payload)
        response.raise_for_status()
        logger.info(f"Opportunity {opportunity_id} moved to stage {stage_id}")
        return response.json()
    except requests.RequestException as e:
        logger.error(f"Failed to update stage: {e}")
        return None

def create_opportunity(contact_id, pipeline_id, stage_id, api_key, location_id, name="Life Insurance Lead"):
    """Create a new opportunity for a contact in GoHighLevel"""
    if not api_key or not location_id:
        logger.error("GHL credentials not set")
        return None
    
    url = f"{GHL_BASE_URL}/opportunities/"
    payload = {
        "pipelineId": pipeline_id,
        "locationId": location_id,
        "contactId": contact_id,
        "stageId": stage_id,
        "status": "open",
        "name": name
    }
    
    try:
        response = requests.post(url, headers=get_ghl_headers(api_key), json=payload)
        response.raise_for_status()
        logger.info(f"Opportunity created for contact {contact_id}")
        return response.json()
    except requests.RequestException as e:
        logger.error(f"Failed to create opportunity: {e}")
        return None

def search_contacts_by_phone(phone, api_key, location_id):
    """Search for a contact by phone number"""
    if not api_key or not location_id:
        logger.error("GHL credentials not set")
        return None
    
    url = f"{GHL_BASE_URL}/contacts/search"
    payload = {
        "locationId": location_id,
        "query": phone
    }
    
    try:
        response = requests.post(url, headers=get_ghl_headers(api_key), json=payload)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        logger.error(f"Failed to search contacts: {e}")
        return None

NEPQ_SYSTEM_PROMPT = """
You are an elite life-insurance re-engagement closer using pure NEPQ (Neuro-Emotional Persuasion Questioning) methodology by Jeremy Miner.

=== WHO THESE LEADS ARE ===
These are people who 30-120 days ago were online looking at life insurance. They went through the cart on ethos.com but never actually purchased. You haven't been able to reach them yet.

Common reasons they didn't buy:
- Price seemed too high
- They were just quoting/comparing
- Got busy and forgot about it
- Life got in the way

Important: They may have bought a policy elsewhere since then. That's STILL an opportunity because:
- Most people don't get the right policy the first time
- They may have overpaid or gotten the wrong type
- EVERYONE needs a policy review

=== EXPLORATORY QUESTIONS TO UNCOVER ===
If they did get coverage elsewhere, find out:
- When did they get it?
- Why did they want coverage in the first place?
- How much is the policy worth?
- Is it Term, Whole Life, or IUL?
- Is it guaranteed issue? (usually means they overpaid)
- Is there a waiting period? (red flag for bad policy)

These questions determine if they were properly helped. The answer is almost always NO, they need a policy review.

=== BACKGROUND (rarely mention, but know it) ===
- You are underwritten with the state and work with ALL insurance carriers
- This means you can shop the market to find the right fit for each client
- You're not tied to one company, so you can be objective
- Only mention this if they ask who you work with or seem skeptical

=== DRIP CAMPAIGN CONTEXT ===
This is part of a 12-month drip campaign. You'll keep reaching out until they book an appointment or 12 months pass. Be persistent but not annoying. Each message should feel natural, not robotic.

=== YOUR ONLY GOAL ===
Book a 30-minute phone appointment TODAY or TOMORROW. Every response must move toward this goal.

=== CRITICAL RULES ===
1. NEVER greet ("Hi", "Hello", "Hope you're well") - jump straight into the question
2. Reply with ONE short message only (15-40 words max)
3. ALWAYS end with a question mark (?)
4. NEVER explain insurance products, features, or benefits
5. NEVER answer questions about rates, costs, or coverage details - redirect to booking
6. If they show ANY buying signal, immediately offer time slots and confirmation code
7. Generate truly random 4-character codes (letters + numbers) for confirmations
8. Be conversational, curious, and empathetic - NOT pushy or salesy
9. Use their first name naturally when appropriate
10. NEVER use em dashes (--) or (—) in your responses - use commas or periods instead

=== NEPQ QUESTION FRAMEWORK ===
Follow this sequence based on conversation stage:

**STAGE 1: PROBLEM AWARENESS (Discover their pain)**
- "What originally got you looking at life insurance?"
- "What's been on your mind lately about protecting your family?"
- "What worries you most about your current situation?"
- "How long has this been weighing on you?"
- "What would happen to your family's lifestyle if something unexpected happened?"

**STAGE 2: CONSEQUENCE QUESTIONS (Deepen urgency)**
- "What happens to your mortgage if you're not around to pay it?"
- "How would your spouse manage the bills without your income?"
- "What would your kids' college fund look like without you?"
- "If you wait and your health changes, how does that affect your options?"

**STAGE 3: SOLUTION AWARENESS (Guide to next step)**
- "What would it mean for you to have that peace of mind?"
- "How important is it to get this handled sooner rather than later?"
- "What's been stopping you from getting this sorted out?"
- "If there was a simple way to lock this in, would that interest you?"

**STAGE 4: COMMITMENT (Book the appointment)**
- "I have 6:30pm tonight or 10:15am tomorrow - which works better for you?"
- "Would morning or afternoon work better for a quick call?"
- "I can squeeze you in at [time] today - does that work?"

=== OBJECTION HANDLING ===
Handle ALL objections with curiosity, not defensiveness. Never argue.

**"I can't afford it" / "It's too expensive"**
→ "I hear you - what does your budget look like for protecting your family right now?"
→ "What would it cost your family if something happened and there was nothing in place?"
→ "If I could show you options under $50/month, would that be worth a quick conversation?"

**"I already have insurance through work"**
→ "That's smart - do you know what happens to that coverage if you change jobs?"
→ "What worries you most if that coverage isn't enough or disappears when you switch jobs?"
→ "Most employer plans are 1-2x salary - would that cover your mortgage and kids' education?"

**"I need to think about it" / "Let me talk to my spouse"**
→ "That makes sense - what specifically would you like more clarity on?"
→ "What would help you feel confident making this decision?"
→ "Would it help if your spouse joined our call tomorrow so you can decide together?"

**"I don't trust insurance companies"**
→ "I get that - what happened that made you feel that way?"
→ "What would it take for you to feel confident about moving forward?"

**"I'm too young" / "I don't need it"**
→ "That's exactly why now's the best time - what would it cost you if your health changes in 10 years?"
→ "What would happen to your family if something unexpected happened tomorrow?"

**"I'm too old"**
→ "There are actually options designed specifically for your age - would it help to explore those?"

**"Send me information" / "Email me details"**
→ "I can definitely do that - what specifically are you hoping to see in that info?"
→ "What questions would you want answered before we talk?"
→ "Most people find a quick 10-minute call answers more than any email - when works for you?"

**"I'm busy" / "Not a good time"**
→ "No problem - when's a better time this week?"
→ "Would a quick 10-minute call work better than going back and forth by text?"

**"Not interested" / "No thanks"**
→ "I respect that - was there something specific that didn't feel right?"
→ "What would have to change for this to be something worth looking at?"

**"How much does it cost?"**
→ "Great question - it depends on a few things about your situation. What coverage amount were you thinking?"
→ "That's exactly what we'd figure out on a quick call - do you have 10 minutes today or tomorrow?"

**"What company is this?" / "Who are you?"**
→ "I work with families to make sure they're protected - what got you interested in looking at this?"

=== HANDLING WEIRD/OFF-TOPIC QUESTIONS ===
If they ask ANYTHING you cannot answer or that's off-topic:
- Do NOT attempt to answer
- Redirect with empathy to booking
- Examples:
  → "Great question - that's actually something we'd cover on our call. When works for you?"
  → "I want to make sure I give you accurate info - that's exactly what we'd go over together. Does 6pm work?"
  → "That depends on your specific situation - easiest to sort out on a quick call. Morning or afternoon better?"

=== BUYING SIGNALS - MOVE TO CLOSE ===
When they show interest (ask about times, pricing, process, say "yes", "okay", "sure", "sounds good"):
IMMEDIATELY offer specific time slots:
"Perfect! I have 6:30pm tonight or 10:15am tomorrow - which works better?"

When they pick a time:
"Locked in. Your confirmation code is {CODE} - reply {CODE} and I'll send the calendar invite."

=== TONE & STYLE ===
- Calm, curious, conversational
- Empathetic, not pushy
- Ask questions, don't lecture
- Short and punchy (SMS-appropriate)
- Use "you" and their name, not "we" or "I"
- Sound like a trusted advisor, not a salesperson

=== EXAMPLES ===

Lead: "initial outreach - contact just entered pipeline, send first message to start conversation"
→ "Hey {first_name}, you were looking at life insurance a little while back but never ended up getting it. What stopped you?"

Lead: "I already got coverage"
→ "Nice, when did you get it? Just want to make sure you got set up right."

Lead: "I got it through work"
→ "That's a start. Do you know what happens to that coverage if you change jobs or get laid off?"

Lead: "I got a policy a few months ago"
→ "Got it. Was it term or whole life? Just want to make sure they didn't stick you with a waiting period."

Lead: "Hi, I saw your ad about life insurance"
→ "What originally got you looking at life insurance?"

Lead: "I'm not sure if I need it"
→ "What would happen to your family's lifestyle if something unexpected happened to you?"

Lead: "How much is it?"
→ "Great question - that depends on your situation. What coverage amount did you have in mind?"

Lead: "Maybe, let me think about it"
→ "What specifically would help you feel confident about this?"

Lead: "Yeah I guess I should look into it"
→ "I have 6:30pm tonight or 10:15am tomorrow open - which works better for you?"

Lead: "Tomorrow morning works"
→ "Locked in. Your confirmation code is 7K9X - reply 7K9X and I'll send the calendar invite."

Lead: "What's the weather like there?"
→ "Ha - that's a first! But hey, let's get your family protected. When works for a quick call?"
"""

def generate_nepq_response(first_name, message):
    """Generate NEPQ response using Grok AI"""
    confirmation_code = generate_confirmation_code()
    full_prompt = NEPQ_SYSTEM_PROMPT.replace("{CODE}", confirmation_code)
    
    user_content = f"""
Lead name: {first_name}
Last message from lead: "{message}"
Confirmation code to use if booking: {confirmation_code}

Generate ONE short NEPQ-style response. No JSON, no markdown, no extra text. Just the response message.
"""

    client = get_client()
    response = client.chat.completions.create(
        model="grok-2-1212",
        messages=[
            {"role": "system", "content": full_prompt},
            {"role": "user", "content": user_content}
        ],
        max_tokens=100,
        temperature=0.7
    )

    content = response.choices[0].message.content or ""
    reply = content.strip()
    reply = reply.replace("—", ",").replace("--", ",").replace("–", ",").replace(" - ", ", ").replace(" -", ",").replace("- ", ", ")
    return reply, confirmation_code


@app.route('/ghl', methods=['POST'])
def ghl_unified():
    """
    Unified GoHighLevel endpoint. Handles all GHL actions via a single URL.
    
    Multi-tenant: Pass GHL credentials in the JSON body:
    - ghl_api_key: Your GHL Private Integration Token
    - ghl_location_id: Your GHL Location ID
    
    If not provided, falls back to environment variables (for your own setup).
    
    Actions (specified via 'action' field in JSON body):
    
    1. "respond" - Generate NEPQ response and send SMS
       Required: contact_id, message
       Optional: first_name
    
    2. "appointment" - Create calendar appointment
       Required: contact_id, calendar_id, start_time
       Optional: duration_minutes (default: 30), title
    
    3. "stage" - Update or create opportunity
       For update: opportunity_id, stage_id
       For create: contact_id, pipeline_id, stage_id, name (optional)
    
    4. "contact" - Get contact info
       Required: contact_id
    
    5. "search" - Search contacts by phone
       Required: phone
    """
    data = request.json or {}
    action = data.get('action', 'respond')
    
    api_key, location_id = get_ghl_credentials(data)
    
    safe_data = {k: v for k, v in data.items() if k not in ('ghl_api_key', 'ghl_location_id')}
    logger.debug(f"GHL unified request - action: {action}, data: {safe_data}")
    
    if action == 'respond':
        contact_id = data.get('contact_id') or data.get('contactId')
        first_name = data.get('first_name') or data.get('firstName') or data.get('name', 'there')
        message = data.get('message') or data.get('body') or data.get('text', '')
        
        if not contact_id:
            return jsonify({"error": "contact_id required"}), 400
        if not message:
            return jsonify({"error": "message required"}), 400
        
        try:
            reply, confirmation_code = generate_nepq_response(first_name, message)
            sms_result = send_sms_via_ghl(contact_id, reply, api_key, location_id)
            
            if sms_result.get("success"):
                return jsonify({
                    "success": True,
                    "reply": reply,
                    "contact_id": contact_id,
                    "sms_sent": True,
                    "confirmation_code": confirmation_code
                })
            else:
                return jsonify({
                    "success": False,
                    "reply": reply,
                    "contact_id": contact_id,
                    "sms_sent": False,
                    "sms_error": sms_result.get("error"),
                    "confirmation_code": confirmation_code
                }), 500
        except Exception as e:
            logger.error(f"Error generating response: {e}")
            return jsonify({"error": str(e)}), 500
    
    elif action == 'appointment':
        contact_id = data.get('contact_id') or data.get('contactId')
        calendar_id = data.get('calendar_id') or data.get('calendarId')
        start_time = data.get('start_time') or data.get('startTime')
        duration_minutes = data.get('duration_minutes', 30)
        title = data.get('title', 'Life Insurance Consultation')
        
        if not contact_id or not calendar_id or not start_time:
            return jsonify({"error": "contact_id, calendar_id, and start_time required"}), 400
        
        try:
            start_dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
            end_dt = start_dt + timedelta(minutes=duration_minutes)
            end_time = end_dt.isoformat()
            
            result = create_ghl_appointment(contact_id, calendar_id, start_time, end_time, api_key, location_id, title)
            
            if result:
                return jsonify({"success": True, "appointment": result})
            else:
                return jsonify({"error": "Failed to create appointment"}), 500
        except Exception as e:
            logger.error(f"Error creating appointment: {e}")
            return jsonify({"error": str(e)}), 500
    
    elif action == 'stage':
        opportunity_id = data.get('opportunity_id') or data.get('opportunityId')
        contact_id = data.get('contact_id') or data.get('contactId')
        pipeline_id = data.get('pipeline_id') or data.get('pipelineId')
        stage_id = data.get('stage_id') or data.get('stageId')
        name = data.get('name', 'Life Insurance Lead')
        
        if not stage_id:
            return jsonify({"error": "stage_id required"}), 400
        
        if opportunity_id:
            result = update_contact_stage(opportunity_id, stage_id, api_key)
            if result:
                return jsonify({"success": True, "opportunity": result})
            else:
                return jsonify({"error": "Failed to update stage"}), 500
        elif contact_id and pipeline_id:
            result = create_opportunity(contact_id, pipeline_id, stage_id, api_key, location_id, name)
            if result:
                return jsonify({"success": True, "opportunity": result, "created": True})
            else:
                return jsonify({"error": "Failed to create opportunity"}), 500
        else:
            return jsonify({"error": "Either opportunity_id OR (contact_id and pipeline_id) required"}), 400
    
    elif action == 'contact':
        contact_id = data.get('contact_id') or data.get('contactId')
        if not contact_id:
            return jsonify({"error": "contact_id required"}), 400
        
        result = get_contact_info(contact_id, api_key)
        if result:
            return jsonify({"success": True, "contact": result})
        else:
            return jsonify({"error": "Failed to get contact"}), 500
    
    elif action == 'search':
        phone = data.get('phone')
        if not phone:
            return jsonify({"error": "phone required"}), 400
        
        result = search_contacts_by_phone(phone, api_key, location_id)
        if result:
            return jsonify({"success": True, "contacts": result})
        else:
            return jsonify({"error": "Failed to search contacts"}), 500
    
    else:
        return jsonify({"error": f"Unknown action: {action}. Valid actions: respond, appointment, stage, contact, search"}), 400


@app.route('/grok', methods=['POST'])
def grok_insurance():
    """Legacy endpoint - generates NEPQ response without GHL integration"""
    data = request.json
    name = data.get('first_name', 'there')
    lead_msg = data.get('message', '')
    
    reply, _ = generate_nepq_response(name, lead_msg)
    return jsonify({"reply": reply})


@app.route('/webhook', methods=['POST'])
def webhook():
    return grok_insurance()


@app.route("/outreach", methods=["GET", "POST"])
def outreach():
    if request.method == "POST":
        return "OK", 200
    return "Up and running", 200


@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy", "service": "NEPQ Webhook API"})


@app.route('/ghl-webhook', methods=['POST'])
def ghl_webhook():
    """Legacy endpoint - redirects to unified /ghl endpoint with action=respond"""
    data = request.json or {}
    data['action'] = 'respond'
    return ghl_unified()


@app.route('/ghl-appointment', methods=['POST'])
def ghl_appointment():
    """Legacy endpoint - redirects to unified /ghl endpoint with action=appointment"""
    data = request.json or {}
    data['action'] = 'appointment'
    return ghl_unified()


@app.route('/ghl-stage', methods=['POST'])
def ghl_stage():
    """Legacy endpoint - redirects to unified /ghl endpoint with action=stage"""
    data = request.json or {}
    data['action'] = 'stage'
    return ghl_unified()


@app.route('/', methods=['POST'])
def index():
    """
    Main webhook - generates NEPQ response and sends SMS automatically.
    Just set URL to https://InsuranceGrokBot.replit.app/ with Custom Data.
    
    If no message is provided (like for tag/pipeline triggers), generates
    an initial outreach message to start the conversation.
    """
    data = request.json or {}
    
    api_key, location_id = get_ghl_credentials(data)
    
    contact_id = data.get('contact_id') or data.get('contactId')
    first_name = data.get('first_name') or data.get('firstName') or data.get('name', 'there')
    message = data.get('message') or data.get('body') or data.get('text', '')
    intent = data.get('intent', 'respond')
    
    safe_data = {k: v for k, v in data.items() if k not in ('ghl_api_key', 'ghl_location_id')}
    logger.debug(f"Root webhook request: {safe_data}")
    
    if not message:
        message = "initial outreach - contact just entered pipeline, send first message to start conversation"
    
    try:
        reply, confirmation_code = generate_nepq_response(first_name, message)
        
        if contact_id and api_key and location_id:
            sms_result = send_sms_via_ghl(contact_id, reply, api_key, location_id)
            return jsonify({
                "success": True,
                "reply": reply,
                "contact_id": contact_id,
                "sms_sent": sms_result.get("success", False),
                "confirmation_code": confirmation_code,
                "intent": intent
            })
        else:
            logger.warning(f"Missing credentials - contact_id: {contact_id}, api_key: {'set' if api_key else 'missing'}, location_id: {'set' if location_id else 'missing'}")
            return jsonify({
                "success": True,
                "reply": reply,
                "confirmation_code": confirmation_code,
                "sms_sent": False,
                "warning": "SMS not sent - missing contact_id or GHL credentials"
            })
    except Exception as e:
        logger.error(f"Error: {e}")
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
