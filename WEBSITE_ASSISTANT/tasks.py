# tasks.py - SaaS Webhook Processor
import logging
import os
from openai import OpenAI
from sales_director import generate_strategic_directive
from prompt import build_system_prompt
from memory import save_message, get_narrative, get_known_facts, save_new_facts

logger = logging.getLogger('rq.worker')
client = OpenAI(api_key=os.getenv("XAI_API_KEY"), base_url="https://api.x.ai/v1")

def process_saas_webhook(payload):
    contact_id = payload.get("contact_id")
    message = payload.get("message", {}).get("body", "")
    
    # 1. Save & Context
    save_message(contact_id, message, "lead")
    story = get_narrative(contact_id)
    facts = get_known_facts(contact_id)

    # 2. Strategy
    director = generate_strategic_directive(contact_id, message, story, facts)

    # 3. Prompt
    system_prompt = build_system_prompt(
        profile_str=str(director['profile']), 
        tactical_narrative=director['tactical_narrative'],
        known_facts=facts
    )

    # 4. Generate
    msgs = [{"role": "system", "content": system_prompt}]
    msgs.append({"role": "user", "content": message})
    
    response = client.chat.completions.create(
        model="grok-4-1-fast-reasoning",
        messages=msgs,
        temperature=0.7
    )
    reply = response.choices[0].message.content.strip()

    # 5. Send (You would hook this to your website widget API)
    save_message(contact_id, reply, "assistant")
    # send_to_widget_api(contact_id, reply) <--- Implement this for your chat widget
    
    return {"reply": reply, "strategy": director['tactical_narrative']}