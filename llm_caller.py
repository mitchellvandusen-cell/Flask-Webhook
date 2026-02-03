# llm_caller.py - Message generation with non-reasoning model
#
# Architecture:
#   conversation_engine.py (reasoning) → classifies intent, objections, stage
#   memory.py (reasoning)              → analyzes conversation, builds narrative
#   sales_director.py                  → builds tactical guidance
#   prompt.py                          → assembles system prompt with all context
#   llm_caller.py (NON-reasoning)      → writes the actual text message
#
# All the thinking is already done by the time this file runs.
# The LLM's only job here is to write 1-3 sentences of natural text.
# Non-reasoning model = no thinking tokens = no reasoning leak = no risk.
import logging
from openai import OpenAI
from reply_sanitizer import sanitize_reply

logger = logging.getLogger(__name__)

# Message generation: non-reasoning (fast, direct output, no thinking tokens)
GENERATION_MODEL = "grok-4-1-fast-non-reasoning"


def generate_clean_reply(
    client: OpenAI,
    system_prompt: str = None,
    user_message: str = None,
    bot_name: str = "Mitch",
    max_tokens: int = 200,
    temperature: float = 0.85,
    full_messages: list = None,
) -> str:
    """
    Generate a text message using the non-reasoning model.

    The non-reasoning model outputs the message directly — no thinking tokens,
    no chain-of-thought, no reasoning_content field. Just the response.
    This eliminates the entire class of reasoning-leak bugs.

    The sanitizer still runs as a safety net but should almost never trigger.

    Args:
        full_messages: Pre-built messages array (overrides system_prompt/user_message).
                       Use this when you need multi-turn conversation history.

    Returns the clean text message, or empty string if generation fails.
    """
    if full_messages:
        messages = full_messages
    else:
        messages = [{"role": "system", "content": system_prompt}]
        if user_message:
            messages.append({"role": "user", "content": user_message})

    try:
        response = client.chat.completions.create(
            model=GENERATION_MODEL,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        content = (response.choices[0].message.content or "").strip()
        logger.debug(f"LLM [{GENERATION_MODEL}]: {len(content)} chars")

        # Safety net: sanitize even though non-reasoning model shouldn't need it
        clean = sanitize_reply(content)
        if clean:
            return clean

        # Non-reasoning model produced contaminated output (extremely unlikely)
        # Retry once with a tighter prompt
        logger.warning(
            f"NON-REASONING MODEL produced suspect output ({len(content)} chars). "
            f"Retrying with minimal prompt."
        )

        retry_response = client.chat.completions.create(
            model=GENERATION_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"You are {bot_name}, a life insurance advisor texting a lead.\n"
                        "Write a short, casual text message. 1-3 sentences.\n"
                        "No emojis. No dashes. Just plain text like a real person.\n"
                        "Output ONLY the message."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Context: {content[:400]}\n\nWrite the text message.",
                },
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )

        retry_content = (retry_response.choices[0].message.content or "").strip()
        retry_clean = sanitize_reply(retry_content)

        if retry_clean:
            logger.info(f"RETRY SUCCEEDED: '{retry_clean[:80]}...'")
            return retry_clean

        logger.error("BOTH LLM CALLS FAILED to produce clean response")
        return ""

    except Exception as e:
        logger.error(f"LLM call failed: {e}", exc_info=True)
        return ""
