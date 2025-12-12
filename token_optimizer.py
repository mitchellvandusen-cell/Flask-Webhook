"""
Token Optimization Utilities
Uses tiktoken for token counting and sumy for history summarization.
Reduces API costs by compressing prompts and enforcing limits.
"""
import logging
from typing import List, Optional, Tuple

import tiktoken

logger = logging.getLogger(__name__)

_encoding = None

def get_encoding():
    """Lazy load tiktoken encoding (cl100k_base compatible with GPT-4/Grok)"""
    global _encoding
    if _encoding is None:
        try:
            _encoding = tiktoken.get_encoding("cl100k_base")
            logger.info("tiktoken encoding loaded: cl100k_base")
        except Exception as e:
            logger.error(f"Failed to load tiktoken encoding: {e}")
            return None
    return _encoding


def count_tokens(text: str) -> int:
    """Count tokens in a string"""
    if not text:
        return 0
    encoding = get_encoding()
    if encoding:
        return len(encoding.encode(text))
    return len(text.split()) * 1.3


def count_messages_tokens(messages: List[dict]) -> int:
    """Count total tokens in a list of chat messages"""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        role = msg.get("role", "")
        total += count_tokens(content) + count_tokens(role) + 4
    return int(total)


def trim_text_to_tokens(text: str, max_tokens: int) -> str:
    """Trim text to fit within max_tokens"""
    if not text:
        return text
    
    encoding = get_encoding()
    if not encoding:
        words = text.split()
        max_words = int(max_tokens / 1.3)
        return " ".join(words[:max_words])
    
    tokens = encoding.encode(text)
    if len(tokens) <= max_tokens:
        return text
    
    trimmed_tokens = tokens[:max_tokens]
    return encoding.decode(trimmed_tokens)


def summarize_history_local(history_text: str, max_sentences: int = 5) -> str:
    """
    Summarize conversation history using sumy (local, no API cost).
    Falls back to simple truncation if sumy fails.
    """
    if not history_text or len(history_text) < 200:
        return history_text
    
    try:
        from sumy.parsers.plaintext import PlaintextParser
        from sumy.nlp.tokenizers import Tokenizer
        from sumy.summarizers.lsa import LsaSummarizer
        from sumy.nlp.stemmers import Stemmer
        from sumy.utils import get_stop_words
        
        parser = PlaintextParser.from_string(history_text, Tokenizer("english"))
        stemmer = Stemmer("english")
        summarizer = LsaSummarizer(stemmer)
        summarizer.stop_words = get_stop_words("english")
        
        summary_sentences = summarizer(parser.document, max_sentences)
        summary = " ".join([str(s) for s in summary_sentences])
        
        if summary:
            original_tokens = count_tokens(history_text)
            summary_tokens = count_tokens(summary)
            savings = ((original_tokens - summary_tokens) / original_tokens * 100) if original_tokens > 0 else 0
            logger.info(f"SUMY: Summarized {original_tokens} tokens to {summary_tokens} ({savings:.1f}% reduction)")
            return summary
        return history_text
        
    except Exception as e:
        logger.warning(f"Sumy summarization failed, using fallback: {e}")
        return trim_text_to_tokens(history_text, 500)


def compress_conversation_history(conversation_history: List[str], max_tokens: int = 1500) -> List[str]:
    """
    Compress conversation history to fit within token limit.
    Strategy: Keep recent messages, summarize older ones.
    """
    if not conversation_history:
        return conversation_history
    
    total_text = "\n".join(conversation_history)
    current_tokens = count_tokens(total_text)
    
    if current_tokens <= max_tokens:
        logger.debug(f"History within limit: {current_tokens}/{max_tokens} tokens")
        return conversation_history
    
    logger.info(f"History exceeds limit: {current_tokens}/{max_tokens} tokens - compressing")
    
    keep_recent = min(4, len(conversation_history))
    recent_messages = conversation_history[-keep_recent:]
    older_messages = conversation_history[:-keep_recent]
    
    if older_messages:
        older_text = "\n".join(older_messages)
        summary = summarize_history_local(older_text, max_sentences=3)
        
        compressed = [f"[Earlier conversation summary: {summary}]"] + recent_messages
    else:
        compressed = recent_messages
    
    final_text = "\n".join(compressed)
    final_tokens = count_tokens(final_text)
    
    if final_tokens > max_tokens:
        compressed = compressed[-3:]
        final_text = "\n".join(compressed)
        final_tokens = count_tokens(final_text)
    
    logger.info(f"Compressed history: {current_tokens} -> {final_tokens} tokens ({len(conversation_history)} -> {len(compressed)} messages)")
    return compressed


def optimize_prompt(prompt: str, max_tokens: int = 3000) -> Tuple[str, int]:
    """
    Optimize a prompt to fit within token limits.
    Returns (optimized_prompt, token_count).
    """
    current_tokens = count_tokens(prompt)
    
    if current_tokens <= max_tokens:
        return prompt, current_tokens
    
    logger.warning(f"Prompt exceeds limit: {current_tokens}/{max_tokens} tokens - trimming")
    trimmed = trim_text_to_tokens(prompt, max_tokens)
    final_tokens = count_tokens(trimmed)
    
    return trimmed, final_tokens


def get_token_stats(prompt: str, max_response_tokens: int = 425) -> dict:
    """Get token statistics for a prompt"""
    prompt_tokens = count_tokens(prompt)
    total_estimated = prompt_tokens + max_response_tokens
    
    input_cost = prompt_tokens * 0.20 / 1_000_000
    output_cost = max_response_tokens * 0.50 / 1_000_000
    total_cost = input_cost + output_cost
    
    return {
        "prompt_tokens": prompt_tokens,
        "max_response_tokens": max_response_tokens,
        "total_estimated": total_estimated,
        "estimated_cost_usd": round(total_cost, 6),
        "cost_per_1k_calls": round(total_cost * 1000, 2),
    }
