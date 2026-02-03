# llm_caller.py - Structural separation of LLM reasoning from response
# This is THE core fix: reasoning models think in one channel and respond in another.
# If they fail to separate, we force the separation with a retry call.
import logging
import re
from openai import OpenAI
from reply_sanitizer import sanitize_reply

logger = logging.getLogger(__name__)


def _extract_reasoning(message) -> str:
    """
    Try every way to get reasoning_content from the API response.
    xAI reasoning models SHOULD put chain-of-thought here, separate from content.
    """
    # Method 1: direct attribute (OpenAI client v1.x with reasoning support)
    try:
        rc = message.reasoning_content
        if rc:
            return rc
    except AttributeError:
        pass

    # Method 2: getattr fallback
    rc = getattr(message, 'reasoning_content', None)
    if rc:
        return rc

    # Method 3: model_extra dict (non-standard fields from xAI)
    if hasattr(message, 'model_extra') and isinstance(message.model_extra, dict):
        rc = message.model_extra.get('reasoning_content', None)
        if rc:
            return rc

    return ""


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
    Generate an LLM reply with structural reasoning/response separation.

    How it works (same as ChatGPT, Claude, Grok chat):
    1. Make the API call — the model reasons AND responds
    2. Extract reasoning_content (thinking) separately from content (response)
    3. If the model properly separated them: return content
    4. If reasoning leaked into content: make a SECOND call that takes
       the analysis and outputs ONLY the text message, with NO system
       prompt context to echo back

    Args:
        full_messages: Pre-built messages array (overrides system_prompt/user_message).
                       Use this when you need multi-turn conversation history.

    Returns the clean text message, or empty string if both attempts fail.
    """
    if full_messages:
        messages = full_messages
    else:
        messages = [{"role": "system", "content": system_prompt}]
        if user_message:
            messages.append({"role": "user", "content": user_message})

    try:
        response = client.chat.completions.create(
            model="grok-4-1-fast-reasoning",
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        msg = response.choices[0].message
        content = (msg.content or "").strip()
        reasoning = _extract_reasoning(msg)

        # Log the separation for monitoring
        if reasoning:
            logger.info(
                f"LLM REASONING SEPARATED: "
                f"{len(reasoning)} chars reasoning | {len(content)} chars content"
            )
        else:
            logger.debug(f"LLM response: {len(content)} chars (no separate reasoning field)")

        # Check if content is a clean message (no reasoning contamination)
        clean = sanitize_reply(content)
        if clean:
            return clean

        # ================================================================
        # CONTENT IS CONTAMINATED — model dumped reasoning into content
        # This is the structural fix: use the reasoning to formulate
        # a clean response in a SECOND call with minimal context
        # ================================================================
        logger.warning(
            f"REASONING LEAKED INTO CONTENT ({len(content)} chars). "
            f"Making response-only retry call."
        )

        # Use whichever has the actual thinking
        analysis = reasoning if reasoning else content

        # Trim analysis to avoid token waste — we only need the gist
        analysis_trimmed = analysis[:600]

        retry_response = client.chat.completions.create(
            model="grok-4-1-fast-reasoning",
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"You are {bot_name}, a life insurance advisor texting a lead.\n"
                        "You just analyzed a conversation. Here is your analysis:\n"
                        f"{analysis_trimmed}\n\n"
                        "Now write the actual text message based on your analysis.\n"
                        "Rules:\n"
                        "- Output ONLY the text message, nothing else\n"
                        "- 1-3 sentences, casual and human\n"
                        "- No reasoning, no commentary, no explanation\n"
                        "- No emojis, no dashes, no bullet points\n"
                        "- Just the message as you would text it"
                    ),
                }
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )

        retry_content = (retry_response.choices[0].message.content or "").strip()
        retry_clean = sanitize_reply(retry_content)

        if retry_clean:
            logger.info(f"RETRY SUCCEEDED: '{retry_clean[:80]}...'")
            return retry_clean

        # Both calls produced contaminated output
        logger.error("BOTH LLM CALLS FAILED to produce clean response")
        return ""

    except Exception as e:
        logger.error(f"LLM call failed: {e}", exc_info=True)
        return ""
