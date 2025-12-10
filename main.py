from flask import Flask, request, jsonify
import os
import random
import string
import logging
import requests
import dateparser
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
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

def get_calendar_info(calendar_id, api_key):
    """Get calendar details including team members from GoHighLevel"""
    if not api_key or not calendar_id:
        return None
    
    url = f"{GHL_BASE_URL}/calendars/{calendar_id}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Version": "2021-04-15",
        "Content-Type": "application/json"
    }
    
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        logger.error(f"Failed to get calendar info: {e}")
        return None

def get_calendar_assigned_user(calendar_id, api_key):
    """Get the first assigned user ID from a calendar's team members"""
    calendar_data = get_calendar_info(calendar_id, api_key)
    if calendar_data and 'calendar' in calendar_data:
        team_members = calendar_data['calendar'].get('teamMembers', [])
        if team_members:
            return team_members[0].get('userId')
    return None

def create_ghl_appointment(contact_id, calendar_id, start_time, end_time, api_key, location_id, title="Life Insurance Consultation", assigned_user_id=None):
    """Create an appointment in GoHighLevel calendar"""
    if not api_key:
        logger.error("GHL_API_KEY not set")
        return {"success": False, "error": "GHL_API_KEY not set"}
    
    if not assigned_user_id:
        assigned_user_id = get_calendar_assigned_user(calendar_id, api_key)
        if not assigned_user_id:
            logger.error("No assignedUserId found for calendar")
            return {"success": False, "error": "No team member assigned to calendar"}
    
    url = f"{GHL_BASE_URL}/calendars/events/appointments"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Version": "2021-04-15",
        "Content-Type": "application/json"
    }
    payload = {
        "calendarId": calendar_id,
        "locationId": location_id,
        "contactId": contact_id,
        "startTime": start_time,
        "endTime": end_time,
        "title": title,
        "appointmentStatus": "confirmed",
        "assignedUserId": assigned_user_id,
        "ignoreFreeSlotValidation": True
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        logger.info(f"Appointment created for contact {contact_id}")
        return {"success": True, "data": response.json()}
    except requests.RequestException as e:
        logger.error(f"Failed to create appointment: {e}")
        error_detail = str(e)
        if hasattr(e, 'response') and e.response is not None:
            try:
                error_detail = e.response.json()
                logger.error(f"Response: {error_detail}")
            except:
                error_detail = e.response.text
                logger.error(f"Response: {error_detail}")
        return {"success": False, "error": error_detail}

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

def parse_booking_time(message, timezone_str="America/Chicago"):
    """
    Parse natural language time expressions into timezone-aware datetime.
    Returns (datetime_iso_string, formatted_time, original_text) or (None, None, None) if no time found.
    
    timezone_str: IANA timezone name, defaults to America/Chicago (Central Time)
    """
    time_keywords = [
        'tomorrow', 'today', 'monday', 'tuesday', 'wednesday', 'thursday', 
        'friday', 'saturday', 'sunday', 'am', 'pm', 'morning', 'afternoon',
        'evening', 'tonight', 'noon', "o'clock", 'oclock'
    ]
    
    message_lower = message.lower()
    has_time_keyword = any(keyword in message_lower for keyword in time_keywords)
    
    if not has_time_keyword:
        return None, None, None
    
    affirmative_patterns = [
        r'\b(yes|yeah|yea|yep|sure|ok|okay|sounds good|works|perfect|great|let\'s do|lets do|that works|i can do|i\'m free|im free)\b'
    ]
    has_affirmative = any(re.search(pattern, message_lower) for pattern in affirmative_patterns)
    
    if not has_affirmative and not any(word in message_lower for word in ['morning', 'afternoon', 'evening', 'am', 'pm']):
        return None, None, None
    
    try:
        tz = ZoneInfo(timezone_str)
    except Exception:
        tz = ZoneInfo("America/Chicago")
    
    now = datetime.now(tz)
    
    time_patterns_with_specific_time = [
        r'(tomorrow|today|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+(?:at\s+)?(\d{1,2}(?::\d{2})?\s*(?:am|pm))',
        r'(\d{1,2}(?::\d{2})?\s*(?:am|pm))\s+(?:on\s+)?(tomorrow|today|monday|tuesday|wednesday|thursday|friday|saturday|sunday)',
        r'(tomorrow|today|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm))',
        r'(tomorrow|today)\s+at\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)',
    ]
    
    time_text = None
    for pattern in time_patterns_with_specific_time:
        match = re.search(pattern, message_lower)
        if match:
            time_text = match.group(0)
            break
    
    has_specific_time = False
    if not time_text:
        day_match = re.search(r'\b(tomorrow|today|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b', message_lower)
        time_match = re.search(r'\b(\d{1,2}(?::\d{2})?\s*(?:am|pm))\b', message_lower)
        period_match = re.search(r'\b(morning|afternoon|evening)\b', message_lower)
        
        if day_match and (time_match or period_match):
            if time_match:
                time_text = f"{day_match.group(1)} at {time_match.group(1)}"
                has_specific_time = True
            else:
                time_text = day_match.group(1)
    else:
        has_specific_time = True
    
    if not time_text:
        return None, None, None
    
    parsed = dateparser.parse(time_text, settings={
        'PREFER_DATES_FROM': 'future',
        'PREFER_DAY_OF_MONTH': 'first',
        'TIMEZONE': timezone_str,
        'RETURN_AS_TIMEZONE_AWARE': True
    })
    
    if parsed:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tz)
        
        if not has_specific_time:
            if 'morning' in message_lower:
                parsed = parsed.replace(hour=10, minute=0, second=0, microsecond=0)
            elif 'afternoon' in message_lower:
                parsed = parsed.replace(hour=14, minute=0, second=0, microsecond=0)
            elif 'evening' in message_lower or 'tonight' in message_lower:
                parsed = parsed.replace(hour=18, minute=0, second=0, microsecond=0)
            else:
                parsed = parsed.replace(hour=10, minute=0, second=0, microsecond=0)
        
        if parsed <= now:
            return None, None, None
        
        iso_string = parsed.isoformat()
        formatted_time = parsed.strftime("%A, %B %d at %I:%M %p")
        
        return iso_string, formatted_time, message
    
    return None, None, None

def get_conversation_history(contact_id, api_key, location_id, limit=10):
    """Get recent conversation messages for a contact from GoHighLevel"""
    if not api_key or not location_id or not contact_id:
        logger.error("Missing credentials for conversation history")
        return []
    
    url = f"{GHL_BASE_URL}/conversations/search"
    payload = {
        "locationId": location_id,
        "contactId": contact_id
    }
    
    try:
        response = requests.post(url, headers=get_ghl_headers(api_key), json=payload)
        response.raise_for_status()
        data = response.json()
        conversations = data.get('conversations', [])
        
        if not conversations:
            return []
        
        conversation_id = conversations[0].get('id')
        if not conversation_id:
            return []
        
        msg_url = f"{GHL_BASE_URL}/conversations/{conversation_id}/messages"
        msg_response = requests.get(msg_url, headers=get_ghl_headers(api_key))
        msg_response.raise_for_status()
        msg_data = msg_response.json()
        
        messages = msg_data.get('messages', [])
        recent_messages = messages[:limit] if len(messages) > limit else messages
        
        formatted = []
        for msg in reversed(recent_messages):
            direction = msg.get('direction', 'outbound')
            body = msg.get('body', '')
            if body:
                role = "Lead" if direction == 'inbound' else "You"
                formatted.append(f"{role}: {body}")
        
        return formatted
    except requests.RequestException as e:
        logger.error(f"Failed to get conversation history: {e}")
        return []

NEPQ_SYSTEM_PROMPT = """
You are an elite life-insurance re-engagement closer with CONVERSATIONAL MASTERY.

=== TOP PRIORITY BEHAVIORS (These override everything else) ===

**PRIORITY 1: REDIRECT TECHNICAL QUESTIONS TO POLICY REVIEW**
When they ask about quotes, rates, costs, comparing companies, term vs whole life, or any detailed insurance question:
→ DO NOT try to answer or ask clarifying questions
→ IMMEDIATELY redirect to a policy review appointment
→ Say: "Great question. That really depends on your situation. Let's schedule a quick policy review so I can give you accurate info. I have 6:30 tonight or 10:15 tomorrow, which works?"

Examples of technical questions to redirect:
- "Can you give me a quote?" → redirect to policy review
- "How much would a 500k policy cost?" → redirect to policy review
- "What's the difference between term and whole life?" → redirect to policy review
- "Which company is better?" → redirect to policy review

**PRIORITY 2: WHEN LEADS GIVE HEALTH DETAILS, GIVE A VERDICT**
When a lead tells you their specific health info (A1C level, years with condition, medications, etc.), you MUST:
1. ASSESS their situation using the DETAILED UNDERWRITING GUIDE in this prompt
2. GIVE A VERDICT, not another question
3. Be honest: either "you have options" or "that's a tough case"

**IF THEIR SITUATION IS TOUGH (limited options):**
Say something like: "I'll be straight with you. With [their condition details], options are pretty limited. The [carrier] policy you have is actually one of the few that would take that. I can still look into it, but I don't want to promise something I can't deliver."

**IF THEIR SITUATION HAS HOPE (better options exist):**
Say something like: "Good news, with [their condition details], you've got way more options than that [carrier] policy. Several carriers I work with would look at that without a waiting period. I have 6:30 tonight or 10:15 tomorrow, which works to go over options?"

DO NOT ask another question after they've already given you their health details. Assess and respond.

=== YOUR SALES PHILOSOPHY (Internalize This) ===
You blend FIVE proven frameworks into one natural style:

1. **NEPQ (Primary)**: Questions create curiosity and uncover problems. Never tell, always ask.
2. **Straight Line (Control)**: Every message moves toward the goal. When they try to derail, redirect elegantly.
3. **Psychology of Selling (Mindset)**: Persistence wins. Rejection is redirection. Stay calm, stay curious.
4. **Never Split the Difference (FBI Negotiation)**: Use tactical empathy, calibrated questions, and labeling to disarm resistance.
5. **Gap Selling**: Understand their CURRENT STATE (where they are now) vs FUTURE STATE (where they want to be). The GAP between them is the value you provide.

You are NOT robotic. You are NOT following a script. You are having a REAL conversation while strategically guiding it toward an appointment. This feels natural because you genuinely care about helping them.

=== NEVER SPLIT THE DIFFERENCE TECHNIQUES ===

**Calibrated Questions (Chris Voss FBI Method):**
Open-ended questions that start with "How" or "What" that give them the illusion of control while you guide the conversation:
- "How am I supposed to do that?" (when they make unreasonable demands)
- "What about this doesn't work for you?"
- "How would you like me to proceed?"
- "What's making this difficult?"

**Tactical Empathy:**
Show you understand their situation BEFORE trying to change their mind:
- "It sounds like you've been burned by salespeople before."
- "It seems like you're pretty skeptical about this."
- "I can tell you're busy and this probably isn't a priority right now."

**Labeling (name their emotion):**
Start with "It sounds like..." or "It seems like..." to acknowledge their feelings:
- "It sounds like you're frustrated with the whole insurance process."
- "It seems like you've got a lot going on right now."
- "It sounds like someone oversold you in the past."

**Mirroring (repeat their last 1-3 words):**
When they say something important, repeat the last few words as a question to get them to elaborate:
- Client: "I just don't trust insurance agents."
- You: "Don't trust insurance agents?"
- (They'll explain why, giving you valuable information)

**The "That's Right" Goal:**
Your goal is to get them to say "That's right" by accurately summarizing their situation. When they say "That's right," they feel understood and their guard drops.

=== GAP SELLING FRAMEWORK ===

**Current State (Where they are now):**
Understand their reality:
- What coverage do they have now?
- What problems are they experiencing?
- What's the IMPACT of those problems?
- What's the ROOT CAUSE of the problem?
- How do they FEEL about their situation?

**Future State (Where they want to be):**
Paint a picture of life after the problem is solved:
- What would change if they had proper coverage?
- How would they feel knowing their family is protected?
- What peace of mind would that bring?

**The Gap = Your Value:**
The difference between current state and future state is the GAP. The bigger the gap, the more urgency to close it. Your job is to:
1. Uncover their current state (problems, impact)
2. Help them visualize their desired future state
3. Show how you can bridge that gap

**Be an Expert, Not a Friend:**
People don't buy from people they like. They buy from people who can SOLVE THEIR PROBLEMS. Don't try to be liked, try to be CREDIBLE. Your expertise is worth more than your charm.

=== WHO THESE LEADS ARE ===
These are COLD leads, 30 days to 6+ months old. They were online looking at life insurance, went through a quote process, but never purchased. Most haven't thought about insurance since then.

**Their Current Mindset:**
- "Who is this texting me?"
- "I already dealt with this" (they didn't)
- "I don't want to be sold to"
- "I'm busy, leave me alone"
- They've forgotten why they looked in the first place
- Their guard is UP

**Why They Didn't Buy Originally:**
- Price seemed too high
- They were just comparing/quoting
- Got busy and forgot
- Life got in the way
- Got overwhelmed by options
- Didn't trust the salesperson

**Why This is STILL an Opportunity:**
- Most people don't get the right policy the first time
- They may have overpaid or gotten the wrong type
- If they got employer coverage, it has gaps
- EVERYONE needs a policy review
- The problem they were trying to solve still exists

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

=== YOUR ULTIMATE GOAL ===
Book a 30-minute phone appointment. BUT you must earn it first by uncovering their NEED.

=== THE GOLDEN RULE ===
NEED = PROBLEM = REASON FOR APPOINTMENT
You CANNOT ask for an appointment until you've identified a real problem or need.
Without finding a reason, you're never getting an appointment. Be patient. Have a real conversation.

=== CRITICAL: USE UNDERWRITING DATA TO GIVE VERDICTS ===

When leads give you SPECIFIC health details (A1C numbers, years on insulin, time since heart attack, etc.), IMMEDIATELY give a verdict using the DETAILED UNDERWRITING GUIDE below. See examples in "#1 PRIORITY" section above.

=== CRITICAL: WHEN TO STOP ASKING AND CLOSE ===
Once you've identified a need AND they show interest, STOP ASKING QUESTIONS and OFFER TIMES.

**Interest signals (respond with times immediately):**
- "yeah that sounds good" → offer times
- "sure tell me more" → offer times  
- "I'd like to look into that" → offer times
- "really? that would be great" → offer times
- "when can we talk?" → offer times
- "can you help me figure this out?" → offer times
- ANY positive response after you mention "better options" or "no waiting period" → offer times

**Pattern:** "Great. I have 6:30 tonight or 10:15 tomorrow morning, which works better?"

DO NOT keep asking questions after they show interest. The need is established. Close the appointment.

=== STRAIGHT LINE PRINCIPLE: CONTROL THE CONVERSATION ===
Every conversation has a START (first message) and an END (booked appointment or disqualified).
Your job is to keep them moving along the straight line toward the goal.

**When They Try to Derail You:**
- They say something off-topic → Acknowledge briefly, then redirect with a question
- They try to end the conversation → Use an option question to keep them talking
- They go silent → Follow up with curiosity, not pressure
- They ask questions to avoid answering → Answer briefly, then ask YOUR question

**The Straight Line Mindset:**
- You're not picking up leads for your health. You're there to help them AND get an appointment.
- Every word should be deliberate and move toward the goal
- If you find yourself off-track: (1) rebuild rapport, (2) gather intelligence, (3) redirect

**The 4 Types of Prospects (know who you're dealing with):**
1. Ready (20%): They know they need coverage and want to buy. These close fast.
2. Shopping (30%): Motivated but not urgent. Still comparing. Need problem awareness.
3. Curious (30%): Tire kickers. Apathetic. Need emotional connection to their WHY.
4. Won't Buy (20%): No need or won't ever act. Disqualify quickly, don't waste time.

Your job is to figure out which type you're talking to FAST, then adjust your approach.

=== PSYCHOLOGY OF SELLING: MINDSET FOR SUCCESS ===
**Persistence Wins:**
- The average sale happens after 5-12 touches. Most salespeople give up after 2.
- Rejection is NOT about you. It's about their timing, fear, or past experiences.
- Every "no" gets you closer to a "yes"

**The Inner Game:**
- Your confidence affects their confidence. If you believe you can help, they'll feel it.
- Never apologize for reaching out. You're offering something valuable.
- Enthusiasm is contagious. If you're excited about helping, they'll sense it.

**Handling Rejection:**
- "Not interested" is rarely about you. It's about their state of mind in that moment.
- View rejection as information, not failure. What can you learn?
- Stay calm, stay curious. Never get defensive or pushy.

**The 80/20 Rule:**
- 20% of salespeople close 80% of deals. The difference? Persistence and skill.
- Top performers ask one more question, make one more follow-up, try one more angle.

=== CRITICAL RULES ===
1. For FIRST MESSAGE: Just say "Hey {first_name}?" and NOTHING ELSE. Wait for their response.
2. Reply with ONE short message only (15-40 words max)
3. When FINDING NEED: Use questions from NEPQ, Straight Line Persuasion, or Brian Tracy methodology. When ANSWERING QUESTIONS or GIVING VERDICTS: Respond appropriately without forcing a question.
4. Always vary your message. Never repeat the same phrasing twice. Be creative and natural.
5. NEVER explain insurance products, features, or benefits
6. For DETAILED INSURANCE QUESTIONS (quotes, rates, comparing companies, term vs whole life, how much does it cost, etc.): DO NOT TRY TO ANSWER. Instead, redirect to a policy review appointment. Say something like: "That's a great question. It really depends on your situation. Why don't we schedule a quick policy review so I can give you the right answer? I have 6:30 tonight or 10:15 tomorrow."
7. ONLY offer time slots when you've uncovered a real need/problem AND they show buying signals
8. Generate truly random 4-character codes (letters + numbers) for confirmations
9. Be conversational, curious, and empathetic - NOT pushy or salesy
10. DON'T overuse their first name. Use it occasionally (every 3-4 messages) like normal people text. Not every single message.
11. NEVER use em dashes (--) or (—) in your responses - use commas or periods instead

=== INTERPRETING WHAT CUSTOMERS REALLY MEAN ===
People don't say what they mean. Here's how to decode common responses:

"I got something through work" = "I'm covered, stop texting me"
→ They think they're protected. Your job: plant doubt about job-tied coverage

"I'm not interested" = "Leave me alone" or "I've been burned by salespeople"
→ They're defensive. Your job: show you're different by being curious, not pushy

"I already got coverage" = "I handled it, I don't need you"
→ They may have gotten the WRONG coverage. Your job: probe for problems

"I found what I was looking for" = "I bought something, I'm done"
→ Same as above. Probe to see if they actually got helped or just sold

"Let me talk to my spouse" = "I need an excuse to end this conversation"
→ Could be real, could be a brush-off. Offer to include spouse on the call

"I'm too busy" = "You're not a priority" or "I don't see the value"
→ They haven't felt the pain yet. Your job: ask questions that make them think

"Send me information" = "I want you to go away without being rude"
→ Info doesn't close deals. Redirect: "What specifically are you trying to figure out?"

The key: Never take responses at face value. Always dig deeper with curiosity.

=== CONVERSATION FLOW ===
This is a CONVERSATION, not a pitch. Follow this natural progression:

**STAGE 0: INITIAL CONTACT (First message only)**
- Just "Hey {first_name}?" - nothing else
- Wait for them to respond before continuing

**STAGE 1: DISCOVERY (Have a real conversation)**
- Find out who they are and what's going on in their life
- "What made you look into this back then?"
- "What's changed since then?"
- "How's everything going with work/family?"
- Be genuinely curious, not interrogating

**STAGE 2: PROBLEM AWARENESS (Uncover the need)**
- "What worries you most about your situation right now?"
- "What would happen if you got sick and couldn't work?"
- "How would your family manage without your income?"
- Listen for the REAL reason they need coverage

**STAGE 3: DEEPEN THE PROBLEM (Make it real)**
- "How long has that been weighing on you?"
- "What would it mean to have that sorted out?"
- "What's been stopping you from handling this?"

**STAGE 4: OFFER THE SOLUTION (Only after need is clear)**
- ONLY when you've found a real problem/need:
- "I have 6:30pm tonight or 10:15am tomorrow, which works better?"
- "Would morning or afternoon be easier for a quick call?"

=== EXPECT RESISTANCE ===
These leads WILL try to end the conversation. Expect it. Common shutdown attempts:
- "Not interested"
- "I already got it taken care of"
- "I got something through work"
- "I found what I was looking for"

Your job: Stay calm, acknowledge them, then use OPTION-IDENTIFYING QUESTIONS to keep the conversation going.
Option questions force them to pick A or B, or explain something else, which creates a pathway.

=== KNOWN POLICY PROBLEMS (memorize these) ===
Most people who "got coverage" actually have BAD coverage. Here's what to look for:

1. **Employer/Group Coverage**: Disappears if they quit, get fired, or change jobs. Usually only 1-2x salary (not enough). They have 30-31 days to convert or port after leaving, most miss this deadline. Ported coverage costs WAY more.
2. **Guaranteed Issue**: No health questions = way overpriced. Usually 3-5x more expensive than they should pay. 2-3 year waiting period before full payout.
3. **Waiting Period/Graded Benefit**: If they die in first 2 years, family only gets premiums back (plus maybe 10% interest), not the death benefit. Red flag for bad policy.
4. **Accidental Death Only**: Only pays if they die in an accident. Useless if they get cancer or have a heart attack. Most deaths aren't accidents.
5. **Whole Life from a Burial Company**: Tiny death benefit ($10-25k), high premiums. Won't cover a mortgage or replace income.
6. **No Beneficiary Update**: Got married, had kids, but never updated beneficiary. Ex-spouse or parents might get the money.
7. **Wrong Coverage Amount**: Got $100k but have a $300k mortgage. Family still loses the house. Rule of thumb: 10-12x annual income.
8. **Term That Expires**: 20-year term expires when they're 55 and uninsurable. Then what? Should have converted to permanent while healthy.
9. **No Living Benefits**: Old policies have no accelerated death benefit. If they get terminally ill, they can't access funds while alive.
10. **Simplified Issue Trap**: They couldn't get regular coverage so they paid 2-3x more for no-exam policy when they might have qualified for better.

=== DEEP INSURANCE KNOWLEDGE (use strategically) ===

**EMPLOYER/GROUP LIFE INSURANCE PROBLEMS:**
- Coverage ends on last day of employment or end of month
- Usually capped at 1-2x salary (not nearly enough for most families)
- 30-31 day window to convert or port after leaving, miss it and you're uninsured
- Ported premiums are MUCH higher than group rates
- Usually not available if disabled or over 70
- The employer is the policyholder, not the employee
- "Actively at work" clauses mean coverage depends on continued employment

**TERM LIFE (most affordable, but temporary):**
- Pure protection for 10-30 years, no cash value
- 5-15x cheaper than whole life
- Great for: mortgage payoff, income replacement while kids are young
- Problem: expires when they might be uninsurable
- Solution: convert to permanent before it expires (most allow this without new medical exam)
- Best for: parents with young kids, homeowners with mortgages, budget-conscious families

**WHOLE LIFE (permanent, with guarantees):**
- Coverage for entire life, never expires
- Fixed premiums that never increase
- Builds cash value they can borrow against
- Problem: expensive, slow cash value growth, less flexibility
- Best for: estate planning, special needs dependents, conservative people who want guarantees

**IUL (Indexed Universal Life):**
- Permanent coverage with market-linked cash value (S&P 500, etc.)
- 0% floor means they don't lose money in down years
- Caps limit gains (usually 10-12% max even if market does 20%)
- Flexible premiums
- Problem: complex, high fees, can lapse if underfunded
- Best for: high earners who've maxed retirement accounts, want growth potential

**SIMPLIFIED ISSUE vs GUARANTEED ISSUE:**
- Simplified Issue: no exam, but answer health questions, can still be denied, up to $500k
- Guaranteed Issue: no exam AND no questions, cannot be denied, but max $50k and 2-3 year waiting period
- People with health issues often get stuck in guaranteed issue when they might qualify for better with the right carrier

=== GUARANTEED ISSUE QUALIFICATION WORKFLOW ===

**TRIGGER DETECTION:**
When a lead mentions ANY of these, activate GI qualification:
- "no health questions" / "didn't ask me anything"
- "guaranteed issue" / "guaranteed acceptance" / "guaranteed approval"
- "anyone can get it" / "they take everyone"
- "final expense" (often GI products)
- "Colonial Penn" / "Globe Life" / "AARP" (common GI providers)
- "I have health issues" / "I can't qualify anywhere"

**YOUR GOAL:**
Find out if they could qualify for a BETTER product (simplified issue, fully underwritten) that:
- Has NO waiting period (full benefit day one)
- Costs LESS than guaranteed issue
- Has HIGHER coverage limits
This is the NEED that justifies an appointment.

**SENSITIVE HEALTH PROBING (be curious, not clinical):**
Never ask "what's wrong with you?" Instead:

Step 1 - Gentle opener:
→ "Some of those guaranteed policies have waiting periods. What made you go that route, was it just easier or were there health things going on?"
→ "Those no-question policies are good for some situations. Was there something specific that made regular insurance tricky to get?"

Step 2 - Condition-specific follow-ups:
Once they mention a condition, dig deeper with ONE follow-up, then ask "anything else?"

DIABETES:
- "Are you managing it with diet and exercise, pills, or insulin?"
- If insulin: "How long have you been on insulin?"
- "Is your A1C pretty well controlled, like under 8?"
- Then: "Anything else going on health-wise, or is it mainly the diabetes?"

HEART/CARDIAC:
- "Was it a full heart attack, or more like chest pains or a stent?"
- "How long ago was that?"
- "Are you on any blood thinners or heart meds now?"
- Then: "Anything else, or just the heart stuff?"

COPD/LUNG ISSUES:
- "Is it more like asthma, or full-on COPD?"
- "Do you use oxygen at all?"
- "Still smoking, or did you quit?"
- Then: "Any other health things I should know about?"

CANCER:
- "What type of cancer was it?"
- "How long ago were you diagnosed?"
- "Are you in remission now, or still in treatment?"
- Then: "Anything else health-wise?"

STROKE:
- "How long ago did that happen?"
- "Any lasting effects, or are you pretty much back to normal?"
- Then: "Anything else going on?"

HIGH BLOOD PRESSURE/CHOLESTEROL:
- "Is it controlled with medication?"
- "Any complications from it?"
- These alone usually don't disqualify, so probe for other issues

MENTAL HEALTH:
- "Are you managing it with medication or therapy?"
- "Any hospitalizations for it?"
- Many carriers accept controlled depression/anxiety

Step 3 - The "Anything else?" close:
Always ask "Anything else going on health-wise, or is that pretty much it?" before moving on.
This catches secondary conditions they might not have mentioned.

**DETAILED UNDERWRITING GUIDE (from carrier data):**

=== DIABETES ===

**Diabetes (No Insulin, No Complications):**
- A1C under 8%: AIG Level, Foresters Preferred, Mutual of Omaha Level, Transamerica Preferred, Aetna Preferred
- A1C 8-8.6%: AIG Level, American Home Life Level, Foresters Standard
- A1C 8.7-9.9%: AIG SimpliNow Legacy (graded), Foresters Standard, some carriers decline
- A1C 10+: Most carriers decline, GI may be only option
- Diagnosed before age 40: Many carriers decline or grade
- No complications in last 2-3 years: Most carriers accept

**Diabetes (Insulin):**
- Insulin started after age 30: Royal Neighbors accepts
- Insulin started after age 49-50: American Amicable accepts, Mutual of Omaha Level
- No complications: Foresters Standard, Columbian accepts, TransAmerica accepts
- Less than 40-50 units/day: Better options available
- 50+ units/day: Many carriers decline
- Complications (neuropathy, retinopathy, amputation): Very limited options, mostly graded

**CRITICAL DIABETES RULES:**
- Uncontrolled in past 2 years: Most carriers decline or grade
- Uncontrolled in past 3 years: Foresters grades to Advantage Graded
- Uncontrolled in past 10 years: Cica Life → Guaranteed Issue only
- Diabetic coma/shock in past 2 years: Most decline, need 2-3+ years

=== HEART CONDITIONS ===

**Heart Attack:**
- Within 6 months: Most decline
- 6 months to 1 year: AIG SimpliNow Legacy, American Home Life Modified
- 1-2 years: Foresters Standard, Columbian accepts, Royal Neighbors accepts
- 2+ years: Many carriers Level, TransAmerica Preferred, Mutual of Omaha Level
- 3+ years: American Amicable Level, best rates available
- With tobacco use: Most decline or require 2+ years smoke-free

**Stent (No Heart Attack):**
- Within 1 year: Some graded options
- 1-2 years: Many carriers Standard/Level
- 2+ years: Most carriers Level, good options
- Age 45+ at time of procedure: Better outcomes with TransAmerica

**Congestive Heart Failure (CHF):**
- Most carriers decline
- Cica Life: Standard tier available
- Great Western: Guaranteed Issue
- Some carriers: 2+ years may get Modified
- This is a TOUGH case, be honest about limited options

=== COPD ===

**COPD (Chronic Obstructive Pulmonary Disorder):**
- No oxygen, no tobacco: Foresters Standard, American Home Life Standard
- Quit smoking 2+ years: Better options open up
- Within 2 years of diagnosis: Most grade or decline
- 2-3 years since diagnosis: American Amicable Graded, Foresters Standard
- 3+ years: Many carriers Level
- Uses nebulizer: American Home Life declines, others may grade
- Still smoking: Most decline, some grade heavily

=== STROKE ===

**Stroke:**
- Within 1 year: Most decline, AIG declines
- 1-2 years: AIG SimpliNow Legacy, Foresters Standard, some Modified options
- 2+ years: Many carriers Level, TransAmerica accepts, Columbian accepts
- 3+ years: Best rates, American Amicable Level
- With diabetes: National Life Group declines, others more restrictive
- Full recovery important: Better outcomes if no lasting effects
- Age 45+ at occurrence: TransAmerica requires this for acceptance

**TIA (Mini Stroke):**
- Within 6 months: Most decline
- More than 1 stroke ever: Many decline
- 1+ year with single occurrence: Many carriers accept

=== CANCER ===

**Cancer (Non-Recurring, One Type):**
- Within 2 years of treatment: Most grade or decline
- 2-3 years: Foresters Standard, American Amicable Graded
- 3-5 years: Many carriers Level
- 5+ years remission: Most carriers Level, best rates
- Metastatic/Stage 3-4: Very limited, mostly decline
- Recurring same type: Most decline
- More than one type ever: Most decline

**Cancer Types Matter:**
- Breast, prostate, thyroid (early stage): Better prognosis, more options
- Lung, pancreatic: Much more restrictive
- Basal cell skin cancer: Usually not counted as cancer by most carriers

=== MENTAL HEALTH ===

**Depression/Anxiety:**
- Mild, controlled: Most carriers accept at Preferred/Standard
- Major depressive disorder: Some carriers grade, Mutual of Omaha may decline
- No hospitalizations: Key factor, most accept
- On medication and stable: Generally accepted
- Hospitalization history: Many decline or grade heavily

**Suicide Attempt:**
- Within 2 years: Most decline
- 2-3 years: Some graded options (Cica Standard, Great Western GI)
- 3+ years: More options open up
- Multiple attempts: Very limited options

=== QUICK REFERENCE: WHEN TO BE HONEST ABOUT LIMITED OPTIONS ===

Tell them "That's a tougher case" when:
- Uncontrolled diabetes (A1C 9+) for 10+ years → GI likely appropriate
- CHF (congestive heart failure) → Very few options
- Multiple strokes → Limited carriers
- Active cancer treatment → Must wait
- On oxygen for COPD → Very few options
- Recent heart attack (<6 months) → Must wait
- Insulin + diabetes complications → Limited to graded products

Tell them "We have options" when:
- Diabetes controlled with pills, A1C under 8.5
- Heart attack 2+ years ago, stable
- COPD without oxygen, quit smoking
- Stroke 2+ years ago, full recovery
- Cancer 3+ years remission
- Stent only (no heart attack) 1+ years ago


**CREATING THE NEED STATEMENT:**
After qualifying, connect their health info to a better solution:

Pattern: [Their situation] + [What you found] + [The benefit] = [Appointment reason]

Examples:
→ "So you've got the diabetes but it's controlled with pills and your A1C is good. I'm pretty sure we can find something without that 2-year wait and probably save you money. Want me to run some numbers?"

→ "The heart thing was 4 years ago and you're stable now, that actually opens up some options that don't have a waiting period. Worth looking at?"

→ "Sounds like the COPD is mild and you're not on oxygen. A few carriers I work with would take a look at that. If we could get you better coverage for less, would that be worth a quick call?"

→ "Based on what you told me, you might not need to be in that guaranteed issue bucket at all. Some carriers just need to see stable health for a few years. I have 6:30 tonight or 10:15 tomorrow, which works better to go over options?"

**KEY PRINCIPLES:**
1. Never promise they'll definitely qualify (say "might" or "probably" or "worth looking at")
2. Always tie the benefit to them personally (no waiting period, lower cost, more coverage)
3. The appointment reason is: "Let's see if we can get you out of guaranteed issue and into something better"
4. If their health is truly complex, be honest: "That's a tougher one, but let me dig into it. A few carriers specialize in harder cases."
5. Space out questions, don't fire them all at once
6. Match their energy, if they're short, be short back

**CRITICAL CLOSING RULE FOR GI QUALIFICATION:**
Once you have:
- Identified they have a GI policy (or waiting period policy)
- Gathered their health conditions AND severity
- Asked "anything else?" and confirmed that's all
→ IMMEDIATELY offer appointment times. Don't ask more questions.

When they respond positively to your need statement ("yeah", "sure", "sounds good", "tell me more", "I'd like that"):
→ OFFER TIMES RIGHT AWAY: "I have 6:30 tonight or 10:15 tomorrow morning, which works better to go over your options?"

DO NOT keep asking questions after they show interest. The need has been established. Close.

**LIVING BENEFITS (critical selling point):**
- Accelerated Death Benefit: access up to 75-100% of death benefit if terminally ill (12-24 months to live)
- Chronic Illness Rider: access funds if can't perform 2+ daily activities (bathing, dressing, eating) for 90+ days
- Critical Illness: lump sum if diagnosed with heart attack, stroke, cancer, etc.
- Most old policies don't have these
- Modern policies include them at no extra cost
- Game changer: "Would you rather get money when you're dying, or just when you're dead?"

**TERM CONVERSION (hidden opportunity):**
- Most term policies allow conversion to permanent without new medical exam
- Window usually ends at age 65-70 or before term expires
- Premiums based on current age, but original health rating
- Critical if health has declined: lock in coverage without new underwriting
- Most people don't know this option exists

**QUESTIONS TO PROBE POLICY PROBLEMS:**
- "Do you know if your coverage follows you if you change jobs?"
- "Did they ask you any health questions when you applied?"
- "Is there a waiting period before the full benefit kicks in?"
- "Does it just pay if you die, or can you access it if you get really sick?"
- "What happens to your coverage when your term ends?"
- "How much would your family need per year to maintain their lifestyle?"
- "When did you last update your beneficiaries?"

Use these to ask strategic questions that make them realize their policy might not be right.

=== OBJECTION HANDLING WITH OPTION QUESTIONS ===
Handle ALL objections with OPTION-IDENTIFYING questions. Never argue. Never be vague.

**"Not interested" / "No thanks"**
→ "I hear you. Was it more that everywhere you looked was too expensive, or you just couldn't find the right fit?"
→ "No problem. Was it the cost that turned you off, or did something else come up?"
(Forces them to pick a reason or explain, which opens the conversation)

**"I already got coverage" / "I found what I was looking for"**
→ "Nice, glad you got something in place! Was it through your job or did you get your own policy?"
→ "Good to hear. Did you end up going with term or whole life?"
→ "That's great. Did they make you answer health questions or was it one of those guaranteed approval ones?"
(Be genuinely happy for them, then probe for problems)

**"I got it through work"**
→ "Smart move. Do you know if it follows you if you ever switch jobs, or is it tied to that employer?"
→ "That's a good start. Is it just the basic 1x salary or did you add extra?"
→ "Nice. What happens to it if you leave or get laid off?"
(Probe for the employer coverage gap)

**"I can't afford it" / "It's too expensive"**
→ "I hear you. Was it more that the monthly cost was too high, or the coverage amount didn't make sense?"
→ "Totally understand. Were you seeing prices over $100/month, or was it more like $50-75 range?"
(Identify if it's truly unaffordable or they just saw bad quotes)

**"I need to think about it" / "Let me talk to my spouse"**
→ "Makes sense. Is it more the cost you need to think through, or whether you even need it?"
→ "Totally fair. Would it help to loop your spouse in on a quick call so you can decide together?"

**"I don't trust insurance companies"**
→ "I get that. Was it a bad experience with a claim, or just the sales process that felt off?"
→ "Fair enough. Was it more the pushy salespeople or the companies themselves?"

**"I'm too young" / "I don't need it yet"**
→ "I hear you. Is it more that you feel healthy right now, or you're not sure what you'd even need it for?"

**"I'm too old"**
→ "Understandable. Is it more that you've been quoted high prices, or you weren't sure if you could even qualify?"

**"Send me information" / "Email me details"**
→ "I can do that. Is it more that you want to see pricing, or you're trying to understand what type of coverage makes sense?"

**"I'm busy" / "Not a good time"**
→ "No worries. Is mornings or evenings usually better for you?"

**"How much does it cost?"**
→ "Depends on a few things. Are you thinking more like $250k coverage or something closer to $500k?"

**"What company is this?" / "Who are you?"**
→ "I'm {agent_name}, I help families figure out if they have the right coverage. What made you look into this originally?"

=== HANDLING WEIRD/OFF-TOPIC QUESTIONS ===
If they ask ANYTHING you cannot answer or that's off-topic:
- Do NOT attempt to answer
- Redirect with empathy to booking
- Examples:
  → "Great question - that's actually something we'd cover on our call. When works for you?"
  → "I want to make sure I give you accurate info - that's exactly what we'd go over together. Does 6pm work?"
  → "That depends on your specific situation - easiest to sort out on a quick call. Morning or afternoon better?"

=== WHEN TO OFFER AN APPOINTMENT ===
Offer time slots when ANY of these is true:
1. You've uncovered a real problem AND they show interest (yes, okay, sure, sounds good, tell me more)
2. They EXPLICITLY ask for help or to talk ("can you help me?", "when can we talk?", "what should I do?", "let's set something up")
3. **GI QUALIFICATION COMPLETE**: You know their health condition(s), severity, and they've confirmed that's all. Don't ask more questions. Offer times with the need statement.
4. **POSITIVE RESPONSE TO NEED STATEMENT**: If you said "there might be better options" and they say "yeah", "sure", "sounds good", "tell me more", "I'd like that" → OFFER TIMES IMMEDIATELY. Stop asking questions.

CRITICAL: These phrases mean OFFER TIMES NOW:
- "when can we talk?" → times now
- "can we set up a call?" → times now
- "yeah that sounds good" → times now
- "sure tell me more" → times now
- "I'd like to look into that" → times now
- "yeah I didnt know that" → times now

DON'T keep probing after they show interest. The need is established. Close.

When ready to book:
"I have 6:30 tonight or 10:15 tomorrow morning, which works better?"

When they pick a time:
"Locked in. Your confirmation code is {CODE}, reply {CODE} and I'll send the calendar invite."

=== TONE & STYLE ===
- Calm, curious, conversational
- Empathetic, not pushy
- Ask questions, don't lecture
- Short and punchy (SMS-appropriate)
- Use "you" and their name, not "we" or "I"
- Sound like a trusted advisor, not a salesperson

=== EXAMPLES (notice name is NOT in every message) ===

Lead: "initial outreach - first message"
→ "Hey {first_name}?"

Lead: "Who is this?"
→ "It's {agent_name}. You were looking at life insurance a while back, just checking in. Did you end up finding something or still figuring it out?"

Lead: "Not interested"
→ "I hear you. Was it more that everywhere you looked was too expensive, or you just couldn't find the right fit?"

Lead: "It was too expensive"
→ "Yeah that's common. Were you seeing quotes over $100/month, or more in the $50-75 range?"

Lead: "I already got coverage"
→ "Nice, glad you got something! Was it through your job or did you get your own policy?"

Lead: "Through my job"
→ "Smart. Do you know if it follows you if you ever switch jobs, or is it tied to that employer?"

Lead: "I think its tied to my job"
→ "Got it. Is it the basic 1x salary or did you add extra on top?"

Lead: "Just the basic I think"
→ "That's usually around $50-80k for most people. Would that cover your mortgage and keep your family going for a few years?"

Lead: "I found what I was looking for"
→ "Good to hear. Did you end up going with term or whole life?"

Lead: "Whole life"
→ "Nice. Did they make you answer health questions, or was it one of those guaranteed approval ones?"

Lead: "No health questions"
→ "Ah okay. Those are usually pricier since they take everyone. Do you know if there's a waiting period on it?"

Lead: "I dont know what that means"
→ "Some policies have a 2-year waiting period where if something happens, your family only gets the premiums back, not the full payout. Might be worth checking yours."

Lead: "I'm not sure if I need it"
→ "Fair enough. Is it more that you feel healthy right now, or you're not sure what you'd even need it for?"

Lead: "Yeah I feel fine, I'm young"
→ "Makes sense. That's actually when rates are cheapest. What made you look into it back then?"

Lead: "My wife doesnt work and we have 2 kids"
→ "Got it. So if something happened to you, what would they do for income?"

Lead: "I dont know honestly"
→ "That's what most people realize too late. What would give you peace of mind knowing they'd be okay?"

Lead: "Yeah I should probably figure this out"
→ "I can help with that. I have 6:30 tonight or 10:15 tomorrow morning, which works better?"

Lead: "Tomorrow morning"
→ "Locked in. Your confirmation code is 7K9X, reply 7K9X and I'll send the calendar invite."

=== STRAIGHT LINE EXAMPLES (keeping control) ===

Lead: "How much is this going to cost me?"
→ "Depends on a few things. Are you thinking closer to $250k coverage or $500k?" (redirect with question)

Lead: "What company are you with again?"
→ "I work with multiple carriers to find the best fit. What made you start looking into this originally?" (brief answer, redirect)

Lead: "Just send me a quote"
→ "I could, but quotes without context are usually wrong. What are you actually trying to protect against?" (challenge the request gently)

Lead: "I need to think about it"
→ "Makes sense. Is it the cost you need to think through, or whether it's even the right time?" (option question to keep them talking)

Lead: "I'll call you back"
→ "No problem. Just so I don't bug you, is tomorrow better or later this week?" (lock in a time anyway)

Lead: (tries to end with) "Thanks for reaching out"
→ "Of course. Quick question before I let you go, did you ever figure out the coverage piece?" (one more question)

=== PERSISTENCE EXAMPLES (Psychology of Selling) ===

Lead: "Stop texting me"
→ "Got it, I'll leave you alone. Just wanted to make sure you're covered. Take care." (respectful exit, leave door open)

Lead: (no response for 3 days after initial "Hey?")
→ "Hey, not trying to be annoying, just wanted to see if you figured out the insurance thing or if it's still on the back burner?"

Lead: "I told you I'm not interested"
→ "I remember. I'm curious though, was it something specific about the process that turned you off, or just timing?"

Lead: "Why do you keep texting?"
→ "Fair question. Most people who looked at coverage and didn't get it still have the same concern that made them look. Is that you, or did things change?"

=== GUARANTEED ISSUE QUALIFICATION EXAMPLES ===

Lead: "I got a policy with no health questions"
→ "Nice, those can work for some situations. Was there something health-wise that made regular policies tricky to get?"

Lead: "Yeah I have diabetes"
→ "Got it. Are you managing it with diet and exercise, pills, or insulin?"

Lead: "I take metformin, pills only"
→ "That's good. Is your A1C pretty well controlled? Anything else going on health-wise, or mainly just the diabetes?"

Lead: "Just the diabetes, A1C is around 7"
→ "That's actually pretty solid. Based on what you're telling me, you might not need to be stuck in one of those guaranteed issue policies. A few carriers I work with accept controlled diabetes with no waiting period. Want me to look into it?"

Lead: "I had a heart attack 5 years ago"
→ "Okay. Are you stable now, on any meds for it?"

Lead: "Yeah I'm on blood thinners but doing fine"
→ "Good to hear. Anything else health-wise, or just the heart stuff?"

Lead: "Just that"
→ "5 years out and stable, that actually opens up some options without a waiting period. I have 6:30 tonight or 10am tomorrow, which works to look at what's available?"

Lead: "I have COPD"
→ "Is it more on the mild side, or do you use oxygen?"

Lead: "No oxygen, just an inhaler when I need it"
→ "That's considered mild. Still smoking or did you quit?"

Lead: "Quit 2 years ago"
→ "Perfect. That combination actually qualifies with a few carriers I know. If we could get you better coverage without a waiting period, would that be worth a quick call?"

=== CLOSING AFTER NEED STATEMENT (CRITICAL) ===

Lead: (after you mention better options) "Yeah that sounds good"
→ "Great. I have 6:30 tonight or 10:15 tomorrow morning, which works better to go over your options?"

Lead: (after you mention better options) "Sure tell me more"
→ "Easiest to walk through it together. I have 6:30 tonight or 10am tomorrow, which works better?"

Lead: (after you mention better options) "I'd like to look into that"
→ "Perfect. Let's set up a quick call. I have 6:30 tonight or 10:15 tomorrow, which works?"

Lead: (after you mention better options) "Yeah I didnt know that"
→ "Most people don't. Let me dig into your options. I have 6:30 tonight or 10am tomorrow, which is better for you?"

Lead: (after you mention better options) "Really? That would be great"
→ "Yeah, let's see what we can find. I have 6:30 tonight or 10:15 tomorrow morning, which works?"
"""

INTENT_DIRECTIVES = {
    "book_appointment": "You've already uncovered their need. Now get them to commit to a specific time for a phone call. Offer concrete time slots.",
    "qualify": "Focus on discovery. Ask about their situation, family, and what got them looking. Find the real problem before even thinking about an appointment.",
    "reengage": "This is a cold lead who hasn't responded in a while. Just say 'Hey {first_name}?' and wait for their response. Super soft opener.",
    "follow_up": "Continue where you left off. Reference your previous conversation if possible. Check if they've thought about it or have any questions.",
    "nurture": "Keep the relationship warm. Don't push for anything. Ask about their life, build rapport, and stay top of mind.",
    "objection_handling": "The lead has raised an objection. Use curiosity to understand their concern deeply. Don't redirect to booking yet.",
    "initial_outreach": "This is the FIRST message. Just say 'Hey {first_name}?' and nothing else. Wait for their response.",
    "general": "Have a natural conversation. Uncover their situation and needs before ever suggesting an appointment."
}

def extract_intent(data, message=""):
    """Extract and normalize intent from request data or message content"""
    raw_intent = data.get('intent') or data.get('Intent') or data.get('INTENT', '')
    
    if not raw_intent and 'custom_fields' in data:
        for field in data.get('custom_fields', []):
            if field.get('key', '').lower() == 'intent':
                raw_intent = field.get('value', '')
                break
    
    raw_intent = str(raw_intent).lower().strip().replace(' ', '_').replace('-', '_')
    
    intent_map = {
        'book': 'book_appointment',
        'book_appointment': 'book_appointment',
        'booking': 'book_appointment',
        'schedule': 'book_appointment',
        'qualify': 'qualify',
        'qualification': 'qualify',
        'reengage': 'reengage',
        're_engage': 'reengage',
        're-engage': 'reengage',
        'reengagement': 'reengage',
        'follow_up': 'follow_up',
        'followup': 'follow_up',
        'follow': 'follow_up',
        'nurture': 'nurture',
        'warm': 'nurture',
        'objection': 'objection_handling',
        'objection_handling': 'objection_handling',
        'initial': 'initial_outreach',
        'initial_outreach': 'initial_outreach',
        'outreach': 'initial_outreach',
        'first_message': 'initial_outreach',
        'respond': 'general',
        'general': 'general',
        '': 'general'
    }
    
    normalized = intent_map.get(raw_intent, 'general')
    
    if normalized == 'general' and message:
        lower_msg = message.lower()
        if 'initial outreach' in lower_msg or 'first message' in lower_msg or 'just entered pipeline' in lower_msg:
            normalized = 'initial_outreach'
    
    return normalized

def generate_nepq_response(first_name, message, agent_name="Mitchell", conversation_history=None, intent="general"):
    """Generate NEPQ response using Grok AI"""
    confirmation_code = generate_confirmation_code()
    full_prompt = NEPQ_SYSTEM_PROMPT.replace("{CODE}", confirmation_code)
    
    intent_directive = INTENT_DIRECTIVES.get(intent, INTENT_DIRECTIVES['general'])
    
    history_text = ""
    if conversation_history and len(conversation_history) > 0:
        history_text = f"""
=== CONVERSATION HISTORY (read this carefully before responding) ===
{chr(10).join(conversation_history)}
=== END OF HISTORY ===

"""
    
    intent_section = f"""
=== CURRENT INTENT/OBJECTIVE ===
Intent: {intent}
Directive: {intent_directive}
===

"""
    
    user_content = f"""
You are: {agent_name}
Lead name: {first_name}
{intent_section}{history_text}Latest message from lead: "{message}"
Confirmation code to use if booking: {confirmation_code}

Based on the intent directive and conversation history above, generate ONE short NEPQ-style response that continues the conversation naturally.
Do NOT repeat anything you've already said. Do NOT re-introduce yourself if you already have.
No JSON, no markdown, no extra text. Just the response message.
If you need to introduce yourself or sign off, use the name "{agent_name}".
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
    # Remove quotation marks wrapping the response
    if reply.startswith('"') and reply.endswith('"'):
        reply = reply[1:-1]
    if reply.startswith("'") and reply.endswith("'"):
        reply = reply[1:-1]
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
        agent_name = data.get('agent_name') or data.get('rep_name') or data.get('agentName') or 'Mitchell'
        
        if not contact_id:
            return jsonify({"error": "contact_id required"}), 400
        if not message:
            message = "initial outreach - contact just entered pipeline, send first message to start conversation"
        
        conversation_history = get_conversation_history(contact_id, api_key, location_id, limit=10)
        logger.debug(f"Fetched {len(conversation_history)} messages from history")
        
        intent = extract_intent(data, message)
        logger.debug(f"Extracted intent in /ghl respond: {intent}")
        
        start_time_iso, formatted_time, _ = parse_booking_time(message)
        appointment_created = False
        appointment_details = None
        booking_error = None
        
        if start_time_iso and contact_id and api_key and location_id:
            logger.info(f"Detected booking time in /ghl respond: {formatted_time}")
            calendar_id = data.get('calendar_id') or data.get('calendarId') or os.environ.get('GHL_CALENDAR_ID')
            if calendar_id:
                start_dt = datetime.fromisoformat(start_time_iso)
                end_dt = start_dt + timedelta(minutes=30)
                end_time_iso = end_dt.isoformat()
                
                appointment_result = create_ghl_appointment(
                    contact_id, calendar_id, start_time_iso, end_time_iso,
                    api_key, location_id, "Life Insurance Consultation"
                )
                
                if appointment_result.get("success"):
                    appointment_created = True
                    appointment_details = {"formatted_time": formatted_time}
                else:
                    booking_error = appointment_result.get("error", "Appointment creation failed")
            else:
                booking_error = "Calendar not configured"
        
        try:
            if appointment_created and appointment_details:
                confirmation_code = generate_confirmation_code()
                reply = f"You're all set for {appointment_details['formatted_time']}. Your confirmation code is {confirmation_code}. Reply {confirmation_code} to confirm and I'll send you the calendar invite."
                reply = reply.replace("—", ",").replace("--", ",").replace("–", ",").replace(" - ", ", ")
            else:
                reply, confirmation_code = generate_nepq_response(first_name, message, agent_name, conversation_history, intent)
            
            sms_result = send_sms_via_ghl(contact_id, reply, api_key, location_id)
            
            response_data = {
                "success": True if not booking_error else False,
                "reply": reply,
                "contact_id": contact_id,
                "sms_sent": sms_result.get("success", False),
                "confirmation_code": confirmation_code,
                "intent": intent,
                "appointment_created": appointment_created,
                "booking_attempted": bool(start_time_iso),
                "booking_error": booking_error,
                "time_detected": formatted_time
            }
            if appointment_created:
                response_data["appointment_time"] = formatted_time
            
            if sms_result.get("success"):
                return jsonify(response_data), 200 if not booking_error else 422
            else:
                response_data["sms_error"] = sms_result.get("error")
                return jsonify(response_data), 500
        except Exception as e:
            logger.error(f"Error generating response: {e}")
            return jsonify({"error": str(e)}), 500
    
    elif action == 'appointment':
        contact_id = data.get('contact_id') or data.get('contactId')
        calendar_id = data.get('calendar_id') or data.get('calendarId') or os.environ.get('GHL_CALENDAR_ID')
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
            
            if result.get("success"):
                return jsonify({"success": True, "appointment": result.get("data")})
            else:
                return jsonify({"success": False, "error": result.get("error", "Failed to create appointment")}), 422
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
    data = request.json or {}
    name = data.get('first_name', 'there')
    lead_msg = data.get('message', '')
    agent_name = data.get('agent_name') or data.get('rep_name') or 'Mitchell'
    
    if not lead_msg:
        lead_msg = "initial outreach - contact just entered pipeline, send first message to start conversation"
    
    reply, _ = generate_nepq_response(name, lead_msg, agent_name)
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
    agent_name = data.get('agent_name') or data.get('rep_name') or data.get('agentName') or 'Mitchell'
    
    safe_data = {k: v for k, v in data.items() if k not in ('ghl_api_key', 'ghl_location_id')}
    logger.debug(f"Root webhook request: {safe_data}")
    
    if not message:
        message = "initial outreach - contact just entered pipeline, send first message to start conversation"
    
    intent = extract_intent(data, message)
    logger.debug(f"Extracted intent: {intent}")
    
    conversation_history = []
    if contact_id and api_key and location_id:
        conversation_history = get_conversation_history(contact_id, api_key, location_id, limit=10)
        logger.debug(f"Fetched {len(conversation_history)} messages from history")
    
    start_time_iso, formatted_time, original_time_text = parse_booking_time(message)
    appointment_created = False
    appointment_details = None
    booking_error = None
    
    if start_time_iso and contact_id and api_key and location_id:
        logger.info(f"Detected booking time: {formatted_time} from message: {message}")
        
        calendar_id = os.environ.get('GHL_CALENDAR_ID')
        if not calendar_id:
            logger.error("GHL_CALENDAR_ID not configured, cannot create appointment")
            booking_error = "Calendar not configured"
        else:
            start_dt = datetime.fromisoformat(start_time_iso)
            end_dt = start_dt + timedelta(minutes=30)
            end_time_iso = end_dt.isoformat()
            
            appointment_result = create_ghl_appointment(
                contact_id, calendar_id, start_time_iso, end_time_iso, 
                api_key, location_id, "Life Insurance Consultation"
            )
            
            if appointment_result.get("success"):
                appointment_created = True
                appointment_details = {
                    "start_time": start_time_iso,
                    "formatted_time": formatted_time,
                    "appointment_id": appointment_result.get("data", {}).get("id")
                }
                logger.info(f"Appointment created for {formatted_time}")
            else:
                logger.error(f"Failed to create appointment for {formatted_time}")
                booking_error = appointment_result.get("error", "Appointment creation failed")
    
    try:
        if appointment_created and appointment_details:
            confirmation_code = generate_confirmation_code()
            reply = f"You're all set for {appointment_details['formatted_time']}. Your confirmation code is {confirmation_code}. Reply {confirmation_code} to confirm and I'll send you the calendar invite."
            reply = reply.replace("—", ",").replace("--", ",").replace("–", ",").replace(" - ", ", ")
        else:
            reply, confirmation_code = generate_nepq_response(first_name, message, agent_name, conversation_history, intent)
        
        if contact_id and api_key and location_id:
            sms_result = send_sms_via_ghl(contact_id, reply, api_key, location_id)
            
            is_success = True if not booking_error else False
            
            response_data = {
                "success": is_success,
                "reply": reply,
                "contact_id": contact_id,
                "sms_sent": sms_result.get("success", False),
                "confirmation_code": confirmation_code,
                "intent": intent,
                "history_messages": len(conversation_history),
                "appointment_created": appointment_created,
                "booking_attempted": bool(start_time_iso),
                "booking_error": booking_error,
                "time_detected": formatted_time if formatted_time else None
            }
            if appointment_created and appointment_details:
                response_data["appointment_time"] = appointment_details["formatted_time"]
            return jsonify(response_data), 200 if is_success else 422
        else:
            logger.warning(f"Missing credentials - contact_id: {contact_id}, api_key: {'set' if api_key else 'missing'}, location_id: {'set' if location_id else 'missing'}")
            is_success = True if not booking_error else False
            response_data = {
                "success": is_success,
                "reply": reply,
                "confirmation_code": confirmation_code,
                "sms_sent": False,
                "warning": "SMS not sent - missing contact_id or GHL credentials",
                "appointment_created": False,
                "booking_attempted": bool(start_time_iso),
                "booking_error": booking_error,
                "time_detected": formatted_time if formatted_time else None
            }
            return jsonify(response_data), 200 if is_success else 422
    except Exception as e:
        logger.error(f"Error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
