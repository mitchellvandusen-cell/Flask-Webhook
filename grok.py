import os
import logging

from openai import OpenAI

logger = logging.getLogger(__name__)

_client = None

DEFAULT_NEPQ_PROMPT = """You are an expert sales assistant trained in NEPQ (Neuro-Emotional Persuasion Questioning) methodology. 
Your role is to respond to inbound SMS messages from potential customers in a way that:

1. Builds rapport and trust immediately
2. Asks thought-provoking questions that help prospects discover their own needs
3. Uses empathy and understanding rather than pushy sales tactics
4. Guides the conversation toward understanding their pain points
5. Keeps responses concise and conversational (suitable for SMS - max 160 characters ideally, never more than 320)
6. Avoids sounding robotic or scripted
7. Uses the prospect's first name naturally when appropriate

Remember: NEPQ is about helping people buy, not selling to them. Focus on their emotional needs and desired outcomes."""


def get_client():
    """Get or create the xAI client with lazy initialization."""
    global _client
    if _client is None:
        api_key = os.environ.get("XAI_API_KEY")
        if not api_key:
            raise ValueError("XAI_API_KEY environment variable is not set")
        _client = OpenAI(base_url="https://api.x.ai/v1", api_key=api_key)
    return _client


def generate_nepq_response(first_name: str, message: str, system_prompt: str = None, conversation_history: list = None) -> str:
    """
    Generate an NEPQ-style response using xAI Grok.
    
    Args:
        first_name: The customer's first name
        message: The incoming SMS message from the customer
        system_prompt: Custom system prompt (uses default if None)
        conversation_history: List of previous messages for context
        
    Returns:
        A personalized NEPQ-style response suitable for SMS
    """
    try:
        prompt = system_prompt or DEFAULT_NEPQ_PROMPT
        user_prompt = f"Customer name: {first_name}\nTheir message: {message}\n\nGenerate an appropriate NEPQ-style SMS response."
        
        messages = [{"role": "system", "content": prompt}]
        
        if conversation_history:
            messages.extend(conversation_history[-10:])
        
        messages.append({"role": "user", "content": user_prompt})
        
        client = get_client()
        response = client.chat.completions.create(
            model="grok-2-1212",
            messages=messages,
            max_tokens=150,
            temperature=0.7
        )
        
        reply = response.choices[0].message.content.strip()
        logger.info(f"Generated NEPQ response for {first_name}")
        return reply
        
    except Exception as e:
        logger.error(f"Error generating NEPQ response: {str(e)}")
        raise Exception(f"Failed to generate response: {str(e)}")
