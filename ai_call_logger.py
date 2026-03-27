# ai_call_logger.py — intercept every chat.completions.create call and log caller + model
#
# Import this once early (main.py app factory, worker.py top-level) to activate.
# Logs WARNING-level so it always appears regardless of log level settings.
#
# Output format (one line per call):
#   [AI_CALL] model=grok-3-mini-fast | file.py:123 in function_name | file2.py:45 in caller
#
# To activate: add `import ai_call_logger` near the top of main.py and worker.py

import logging
import traceback

logger = logging.getLogger("ai_call_logger")

_SKIP_FRAMES = frozenset([
    "ai_call_logger.py",
    "_client.py",
    "completions.py",
    "httpx",
    "httpcore",
    "_base_client.py",
    "_streaming.py",
    "threading.py",
    "concurrent",
])

_patched = False


def _build_caller_chain():
    """Return the 4 most relevant stack frames, skipping SDK internals."""
    frames = []
    for frame in reversed(traceback.extract_stack()):
        fname = frame.filename.replace("\\", "/")
        # Skip SDK internals, this file, and standard library threading
        if any(skip in fname for skip in _SKIP_FRAMES):
            continue
        short = fname.split("/")[-1]
        frames.append(f"{short}:{frame.lineno} in {frame.name}")
        if len(frames) == 5:
            break
    return " → ".join(reversed(frames))


def patch():
    """Monkey-patch openai Completions.create to log every call with caller info."""
    global _patched
    if _patched:
        return

    try:
        from openai.resources.chat.completions import completions as _completions_module
        Completions = _completions_module.Completions
    except Exception:
        try:
            from openai.resources.chat.completions import Completions
        except Exception as e:
            logger.warning(f"[AI_CALL_LOGGER] Could not import Completions to patch: {e}")
            return

    _original_create = Completions.create

    def _logged_create(self, *args, **kwargs):
        model = kwargs.get("model", args[0] if args else "unknown")
        messages = kwargs.get("messages", [])
        prompt_chars = sum(len(str(m.get("content", ""))) for m in messages)
        caller = _build_caller_chain()
        logger.warning(
            f"[AI_CALL] model={model} | chars={prompt_chars} | {caller}"
        )
        return _original_create(self, *args, **kwargs)

    Completions.create = _logged_create
    _patched = True
    logger.warning("[AI_CALL_LOGGER] Patched — logging all chat.completions.create calls")


# Auto-patch on import
patch()
