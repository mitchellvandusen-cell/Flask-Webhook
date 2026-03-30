"""
test_llm_providers.py — Compare Groq vs Gemini for lead intelligence scoring.

Tests the EXACT prompt and JSON schema from lead_intelligence.py against:
  - groq: llama-3.1-8b-instant
  - groq: llama-3.3-70b-versatile
  - gemini: gemini-2.0-flash-lite

Usage:
  pip install openai
  GROQ_API_KEY=your_key GEMINI_API_KEY=your_key python test_llm_providers.py

Get free keys (no credit card):
  Groq:   https://console.groq.com
  Gemini: https://aistudio.google.com/apikey
"""

import json
import os
import re
import time
from datetime import datetime

try:
    from openai import OpenAI
except ImportError:
    print("ERROR: pip install openai")
    raise

# ── Model configs ────────────────────────────────────────────────────────────

MODELS = [
    {
        "id": "xai-mini",
        "label": "xAI grok-3-mini-fast (current)",
        "base_url": "https://api.x.ai/v1",
        "api_key_env": "XAI_API_KEY",
        "model": "grok-3-mini-fast",
    },
    {
        "id": "groq-8b",
        "label": "Groq llama-3.1-8b-instant",
        "base_url": "https://api.groq.com/openai/v1",
        "api_key_env": "GROQ_API_KEY",
        "model": "llama-3.1-8b-instant",
    },
    {
        "id": "groq-70b",
        "label": "Groq llama-3.3-70b-versatile",
        "base_url": "https://api.groq.com/openai/v1",
        "api_key_env": "GROQ_API_KEY",
        "model": "llama-3.3-70b-versatile",
    },
    {
        "id": "gemini-flash-lite",
        "label": "Gemini 2.0 Flash Lite",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "api_key_env": "GEMINI_API_KEY",
        "model": "gemini-2.0-flash-lite",
    },
]

# ── Exact prompt from lead_intelligence.py ──────────────────────────────────

def build_prompt(scenario: dict) -> str:
    convo = scenario.get("convo", "No conversation yet.")
    facts = scenario.get("facts", "None known.")
    tags = scenario.get("tags", "None.")
    pipeline_str = scenario.get("pipeline", "No pipeline data.")
    calls_str = scenario.get("calls", "0 calls total (0 outbound by agent, 0 inbound from lead), 0 answered (>30s), 0 unanswered/no-answer, 0 calls >2min")
    narrative_str = scenario.get("narrative", "No prior narrative.")
    timing_str = scenario.get("timing", "")

    return f"""You are an AI sales coach analyzing an insurance lead for an agent. Read ALL the data below carefully and produce a JSON intelligence report. Your classification MUST be based on what the lead ACTUALLY SAID AND DID, not just whether they responded.

CRITICAL — READ CONVERSATIONS IN CONTEXT:
Messages are a back-and-forth thread between "Bot" (the agent's AI assistant) and "Lead" (the prospect). You MUST read each Lead reply as a RESPONSE TO THE PRECEDING Bot message. Short or single-word Lead replies ONLY make sense in context of what the Bot just said.
Examples of contextual reading:
- Bot: "Would you like a free quote?" → Lead: "sure" = INTERESTED (responding yes to the offer)
- Bot: "Reply STOP if you want to stop receiving messages" → Lead: "stop" = WANTS TO OPT OUT (not interested in anything)
- Bot: "Do you currently have life insurance?" → Lead: "no" = NO EXISTING COVERAGE (not rejecting the conversation)
- Bot: "Let me know if you want to stop talking" → Lead: "want" = WANTS TO STOP (completing the Bot's sentence)
- Bot: "I can help with term or whole life — which interests you?" → Lead: "term" = INTERESTED IN TERM LIFE (not a one-word dismissal)
- Bot: "Great! When works best for a call?" → Lead: "never" = REJECTING (not scheduling)
Never classify a message in isolation. A "yes", "no", "ok", "sure", "want", "stop", etc. can mean completely different things depending on what the Bot just said.

CONVERSATION HISTORY:
{convo}

KNOWN FACTS ABOUT LEAD: {facts}
TAGS: {tags}
{pipeline_str}
CALL HISTORY: {calls_str}
PRIOR NARRATIVE: {narrative_str}
{timing_str}
CURRENT DATE: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}

Respond with ONLY valid JSON (no markdown, no code fences, no explanation). Use this exact structure:
{{
  "summary": "2-sentence situational snapshot. What does the agent need to know RIGHT NOW about this lead? Be specific — reference actual conversation details, names, products, objections. Include timing context (e.g. 'Last replied 3 days ago' or 'Responded within minutes').",
  "temperature": "hot | warm | cool | cold",
  "temperature_reason": "One sentence explaining why, referencing BOTH what they said AND their behavioral pattern (timing, responsiveness, message depth).",
  "score": 0-100,
  "should_respond": true or false,
  "should_respond_reason": "Why the agent should or should not respond right now.",
  "engagement_level": 0-3,
  "under_contract": true or false,
  "actions": [
    {{
      "action": "Short imperative action (e.g. 'Call back about the term quote')",
      "reason": "Why this action matters right now",
      "priority": "high | medium | low",
      "icon": "fa-solid fa-<appropriate-icon>"
    }}
  ]
}}

CRITICAL CLASSIFICATION RULES:

"temperature":
- hot = Lead is actively buying: asking for quotes, requesting coverage, comparing products, ready to book. ALSO hot if velocity_trend is "warming" AND engagement_level >= 2.
- warm = Lead is engaged and positive: responding to questions, sharing information, showing genuine interest.
- cool = Lead went quiet (no response to last 2+ messages), giving slow/short replies, soft objection ("let me think"), OR velocity_trend is "cooling."
- cold = Lead sent TCPA opt-out ("stop", "unsubscribe", "do not contact", "remove me"), OR completely ghosted 3+ outreach attempts over multiple days.
IMPORTANT: "not interested", "no thanks", "I'm good" are sales OBJECTIONS — classify as cool, NOT cold.
TIMING RULE: If lead has not responded in 7+ days, they cannot be "warm."

"score" — 0-100 likelihood to convert:
  - TCPA opt-out → MUST be 5-15
  - "not interested" / "no thanks" → 15-30
  - No conversation (only bot outreach, no replies) → MUST be 10-25
  - Surface reply only ("who is this") → 15-30
  - Soft objections ("too expensive", "let me think") → 25-45
  - Actively discussing coverage, answering questions → 45-70
  - Asking for quotes, scheduling calls → 65-85
  - Ready to buy, discussing specific policies → 80-95
  TIMING: Subtract 10-20 points if no reply in 7+ days. Add 5-10 if replied within minutes/hours.

"should_respond": true if lead asked a question unanswered, expressed interest not followed up, or last message is from Lead. false if bot already replied to lead's last message, or lead sent TCPA opt-out.

"engagement_level": 0=no replies, 1=surface contact (one-word, "who is this"), 2=real conversation, 3=deep engagement (multiple exchanges, discussed specifics).

"under_contract": true only if pipeline/conversation clearly shows they already bought a policy.

"actions": 2-4 specific next steps with icons (fa-phone, fa-paper-plane, fa-file-invoice-dollar, fa-calendar, fa-fire, fa-clock, fa-reply, fa-bolt)."""


# ── Test scenarios ───────────────────────────────────────────────────────────

SCENARIOS = [
    {
        "id": "cold_no_reply",
        "name": "Cold lead — bot outreach, zero replies",
        "expected": {"temperature": "cold_or_cool", "score_max": 25, "should_respond": False, "engagement_level": 0},
        "convo": """Bot: Hey James, it's Mike. You filled out a form about life insurance. Still looking?
Bot: Just checking in — did you get a chance to look into options?
Bot: Last try James — happy to answer any questions when you're ready.""",
        "facts": "None known.",
        "calls": "2 calls total (2 outbound by agent, 0 inbound from lead), 0 answered (>30s), 2 unanswered/no-answer, 0 calls >2min",
        "timing": "TIMING: who_spoke_last=Bot, days_since_lead_reply=14, velocity_trend=cooling",
    },
    {
        "id": "who_is_this",
        "name": "Lead asks 'Who is this?'",
        "expected": {"temperature": "cool_or_warm", "score_range": (15, 35), "should_respond": True, "engagement_level": 1},
        "convo": """Bot: Hey Sarah, it's Mike. You filled out a form about life insurance. Still thinking about it?
Lead: Who is this?""",
        "facts": "None known.",
        "timing": "TIMING: who_spoke_last=Lead, days_since_lead_reply=0, velocity_trend=neutral",
    },
    {
        "id": "not_interested",
        "name": "Not interested objection",
        "expected": {"temperature": "cool", "score_range": (15, 35), "should_respond": True},
        "convo": """Bot: Hey Carlos, it's Mike. You reached out about final expense coverage. Still open to options?
Lead: Not really interested honestly
Bot: Completely understand. Most people say that before they see how affordable it actually is. Quick question — do you have anything in place right now if something happened?
Lead: No I don't but I'm still not interested""",
        "facts": "No current coverage.",
        "timing": "TIMING: who_spoke_last=Lead, days_since_lead_reply=0, velocity_trend=cooling",
    },
    {
        "id": "tcpa_stop",
        "name": "TCPA opt-out — STOP",
        "expected": {"temperature": "cold", "score_max": 15, "should_respond": False},
        "convo": """Bot: Hey Linda, it's Mike. You filled out a form about life insurance. Still looking?
Lead: Stop
Bot: Got it — removing you from our list.""",
        "facts": "None known.",
        "timing": "TIMING: who_spoke_last=Bot, days_since_lead_reply=1",
    },
    {
        "id": "warm_engaging",
        "name": "Warm — actively engaging, sharing info",
        "expected": {"temperature": "warm", "score_range": (45, 75), "should_respond": True, "engagement_level": 2},
        "convo": """Bot: Hey Tom, it's Mike. You were looking into term life. Still exploring?
Lead: Yeah I am actually
Bot: Great. How old are you and do you have a family to protect?
Lead: 38, wife and two kids
Bot: Perfect age for term — locks in low rates. Any major health conditions?
Lead: Just high blood pressure, managed with meds
Bot: That usually still qualifies for preferred rates. What coverage amount were you thinking?
Lead: Not sure, maybe 500k?""",
        "facts": "Age 38, married, 2 kids, high blood pressure on meds.",
        "timing": "TIMING: who_spoke_last=Lead, days_since_lead_reply=0, velocity_trend=warming",
    },
    {
        "id": "hot_ready_to_book",
        "name": "Hot — asking for quote, wants to schedule",
        "expected": {"temperature": "hot", "score_range": (65, 95), "should_respond": True, "engagement_level": 3},
        "convo": """Bot: Hey Maria, it's Mike. You were interested in final expense coverage. Still looking?
Lead: Yes I need this. My husband just passed and I realized I have nothing
Bot: I'm so sorry for your loss. We can absolutely help. When would work for a quick call?
Lead: Any time this week. What does it cost usually for someone my age, I'm 61?
Bot: At 61 final expense runs around $40-80/month depending on health. I can get exact numbers on a call. How's Thursday at 2pm?
Lead: Thursday works. Can you send me info before then?""",
        "facts": "Age 61, recently widowed, no current coverage, highly motivated.",
        "timing": "TIMING: who_spoke_last=Lead, days_since_lead_reply=0, velocity_trend=warming",
    },
    {
        "id": "gone_cold_7days",
        "name": "Was warm, went cold — 7 days no reply",
        "expected": {"temperature": "cool_or_cold", "score_max": 45, "should_respond": False},
        "convo": """Bot: Hey David, it's Mike. You filled out a form about term life. Still exploring?
Lead: Yeah maybe. What does it cost?
Bot: Depends on age and health. How old are you?
Lead: 45
Bot: At 45 term is still very affordable. Any major health conditions?
Bot: Just following up — did you want to move forward with a quote?
Bot: Last check-in David — happy to help when you're ready.""",
        "facts": "Age 45. Expressed initial interest but went quiet.",
        "timing": "TIMING: who_spoke_last=Bot, days_since_lead_reply=7, velocity_trend=cooling",
    },
    {
        "id": "already_covered_objection",
        "name": "Already covered objection",
        "expected": {"temperature": "cool_or_warm", "score_range": (20, 55), "should_respond": True},
        "convo": """Bot: Hey Patricia, it's Mike. You came across my info about life insurance. Still looking?
Lead: I already have insurance through my job
Bot: Good to have something in place. Quick question — do you know how much coverage you have through work?
Lead: Uh I think like 50k or something
Bot: That's actually pretty common — most work policies are 1-2x salary which leaves a big gap. Do you have a mortgage or kids?
Lead: Yeah mortgage and 3 kids""",
        "facts": "Has employer life insurance (~$50k), mortgage, 3 kids.",
        "timing": "TIMING: who_spoke_last=Lead, days_since_lead_reply=0, velocity_trend=warming",
    },
    {
        "id": "price_objection",
        "name": "Price objection — too expensive",
        "expected": {"temperature": "cool", "score_range": (20, 45), "should_respond": True},
        "convo": """Bot: Hey Robert, it's Mike. You were looking at final expense. How are you doing?
Lead: Good. I looked into it before but it was too expensive
Bot: That's the most common thing I hear. Rates have actually come down — and at your age it's still very manageable. What were you quoted before?
Lead: Like $120 a month
Bot: For what coverage amount?
Lead: I think $25k""",
        "facts": "Previously quoted $120/mo for $25k final expense. Price-sensitive.",
        "timing": "TIMING: who_spoke_last=Lead, days_since_lead_reply=0, velocity_trend=neutral",
    },
    {
        "id": "booked_appointment",
        "name": "Appointment already booked",
        "expected": {"temperature": "hot", "score_range": (70, 95), "should_respond": False, "engagement_level": 3},
        "convo": """Bot: Hey Kevin, it's Mike. You were looking at term life. Still open to it?
Lead: Yeah I've been meaning to look into it
Bot: Let's get you real numbers. Any major health conditions?
Lead: No nothing major
Bot: How old are you?
Lead: 42
Bot: Perfect. I've got Thursday at 3pm open — want me to grab that?
Lead: Yeah that works for me""",
        "facts": "Age 42, healthy, appointment booked for Thursday 3pm.",
        "pipeline": "Pipeline: Sales | Stage: Appointment Set | Status: open",
        "timing": "TIMING: who_spoke_last=Lead, days_since_lead_reply=0, velocity_trend=warming",
    },
]

# ── Validation ───────────────────────────────────────────────────────────────

REQUIRED_FIELDS = ["summary", "temperature", "temperature_reason", "score",
                   "should_respond", "should_respond_reason", "engagement_level",
                   "under_contract", "actions"]
VALID_TEMPS = {"hot", "warm", "cool", "cold"}
VALID_PRIORITIES = {"high", "medium", "low"}


def validate(result: dict, expected: dict) -> dict:
    issues = []
    passes = []

    # Required fields
    for f in REQUIRED_FIELDS:
        if f not in result:
            issues.append(f"MISSING field: {f}")
        else:
            passes.append(f"field:{f}")

    # Temperature valid enum
    temp = result.get("temperature", "")
    if temp not in VALID_TEMPS:
        issues.append(f"BAD temperature: '{temp}'")
    else:
        passes.append("temperature_enum")

    # Score range
    score = result.get("score")
    if not isinstance(score, (int, float)) or not (0 <= score <= 100):
        issues.append(f"BAD score: {score}")
    else:
        passes.append("score_range")

    # Engagement level
    eng = result.get("engagement_level")
    if eng not in (0, 1, 2, 3):
        issues.append(f"BAD engagement_level: {eng}")
    else:
        passes.append("engagement_level")

    # Actions structure
    actions = result.get("actions", [])
    if not isinstance(actions, list) or len(actions) < 2:
        issues.append(f"BAD actions: need 2+, got {len(actions) if isinstance(actions, list) else type(actions)}")
    else:
        passes.append("actions_count")
        for i, a in enumerate(actions):
            for af in ("action", "reason", "priority", "icon"):
                if af not in a:
                    issues.append(f"action[{i}] missing '{af}'")
            if a.get("priority") not in VALID_PRIORITIES:
                issues.append(f"action[{i}] bad priority: '{a.get('priority')}'")

    # Expected checks
    if "temperature" in expected:
        exp_t = expected["temperature"]
        if "/" in exp_t:
            valid_temps = set(exp_t.split("/"))
            if temp not in valid_temps:
                issues.append(f"WRONG temperature: got '{temp}', expected one of {valid_temps}")
            else:
                passes.append("temperature_expected")
        elif exp_t == "cold_or_cool":
            if temp not in ("cold", "cool"):
                issues.append(f"WRONG temperature: got '{temp}', expected cold or cool")
            else:
                passes.append("temperature_expected")
        elif exp_t == "cool_or_warm":
            if temp not in ("cool", "warm"):
                issues.append(f"WRONG temperature: got '{temp}', expected cool or warm")
            else:
                passes.append("temperature_expected")
        elif exp_t == "cool_or_cold":
            if temp not in ("cool", "cold"):
                issues.append(f"WRONG temperature: got '{temp}', expected cool or cold")
            else:
                passes.append("temperature_expected")
        else:
            if temp != exp_t:
                issues.append(f"WRONG temperature: got '{temp}', expected '{exp_t}'")
            else:
                passes.append("temperature_expected")

    if "score_max" in expected and isinstance(score, (int, float)):
        if score > expected["score_max"]:
            issues.append(f"SCORE too high: {score} > max {expected['score_max']}")
        else:
            passes.append("score_max")

    if "score_range" in expected and isinstance(score, (int, float)):
        lo, hi = expected["score_range"]
        if not (lo <= score <= hi):
            issues.append(f"SCORE out of range: {score} not in [{lo}, {hi}]")
        else:
            passes.append("score_in_range")

    if "should_respond" in expected:
        sr = result.get("should_respond")
        if sr != expected["should_respond"]:
            issues.append(f"WRONG should_respond: got {sr}, expected {expected['should_respond']}")
        else:
            passes.append("should_respond")

    if "engagement_level" in expected and isinstance(eng, int):
        if eng != expected["engagement_level"]:
            issues.append(f"WRONG engagement_level: got {eng}, expected {expected['engagement_level']}")
        else:
            passes.append("engagement_level_expected")

    return {"passes": passes, "issues": issues}


def repair_json(raw: str) -> dict:
    """Minimal JSON repair — strip markdown fences, try parse."""
    text = raw.strip()
    text = re.sub(r'^```(?:json)?\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s*```$', '', text)
    text = text.strip()
    # Find first { to last }
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end != -1:
        text = text[start:end+1]
    return json.loads(text)


# ── Runner ───────────────────────────────────────────────────────────────────

def run_model(model_cfg: dict, scenarios: list) -> list:
    api_key = os.getenv(model_cfg["api_key_env"])
    if not api_key:
        print(f"\n  SKIP {model_cfg['label']} — {model_cfg['api_key_env']} not set")
        return []

    client = OpenAI(api_key=api_key, base_url=model_cfg["base_url"])
    results = []

    print(f"\n{'='*60}")
    print(f"  {model_cfg['label']}")
    print(f"{'='*60}")

    for s in scenarios:
        prompt = build_prompt(s)
        t0 = time.time()
        error = None
        raw = ""
        parsed = {}

        try:
            resp = client.chat.completions.create(
                model=model_cfg["model"],
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=600,
                timeout=20.0,
                response_format={"type": "json_object"},
            )
            raw = (resp.choices[0].message.content or "").strip()
            parsed = repair_json(raw)
        except Exception as e:
            error = str(e)

        elapsed = time.time() - t0

        if error:
            validation = {"passes": [], "issues": [f"API/PARSE ERROR: {error}"]}
        else:
            validation = validate(parsed, s["expected"])

        pass_count = len(validation["passes"])
        issue_count = len(validation["issues"])
        total_checks = pass_count + issue_count
        pct = int(100 * pass_count / total_checks) if total_checks else 0

        status = " OK" if issue_count == 0 else (" WARN" if issue_count <= 2 else " FAIL")
        print(f"\n  {status} [{pct:3d}%] {s['name']} ({elapsed:.1f}s)")

        if validation["issues"]:
            for iss in validation["issues"]:
                print(f"          x {iss}")

        if not error and parsed:
            print(f"          temp={parsed.get('temperature')} score={parsed.get('score')} "
                  f"respond={parsed.get('should_respond')} eng={parsed.get('engagement_level')}")

        results.append({
            "scenario_id": s["id"],
            "scenario_name": s["name"],
            "model_id": model_cfg["id"],
            "model_label": model_cfg["label"],
            "elapsed": elapsed,
            "passes": pass_count,
            "issues": issue_count,
            "pct": pct,
            "parsed": parsed,
            "error": error,
        })

        time.sleep(0.5)  # avoid rate limits

    return results


# ── Summary table ─────────────────────────────────────────────────────────────

def print_summary(all_results: list):
    print(f"\n\n{'='*70}")
    print("  SUMMARY")
    print(f"{'='*70}")

    # Group by model
    model_ids = list(dict.fromkeys(r["model_id"] for r in all_results))
    scenario_ids = list(dict.fromkeys(r["scenario_id"] for r in all_results))

    # Header
    col_w = 22
    header = f"{'Scenario':<32}"
    for mid in model_ids:
        label = next(r["model_label"] for r in all_results if r["model_id"] == mid)
        short = label.split()[-1][:col_w]
        header += f"  {short:>{col_w}}"
    print(header)
    print("-" * (32 + len(model_ids) * (col_w + 2)))

    for sid in scenario_ids:
        sname = next(r["scenario_name"] for r in all_results if r["scenario_id"] == sid)
        row = f"{sname[:31]:<32}"
        for mid in model_ids:
            match = next((r for r in all_results if r["scenario_id"] == sid and r["model_id"] == mid), None)
            if match:
                icon = "OK  " if match["issues"] == 0 else ("WARN" if match["issues"] <= 2 else "FAIL")
                cell = f"{icon} {match['pct']}% ({match['elapsed']:.1f}s)"
            else:
                cell = "SKIPPED"
            row += f"  {cell:>{col_w}}"
        print(row)

    print()
    # Per-model totals
    for mid in model_ids:
        model_res = [r for r in all_results if r["model_id"] == mid]
        if not model_res:
            continue
        total_pass = sum(r["passes"] for r in model_res)
        total_checks = sum(r["passes"] + r["issues"] for r in model_res)
        avg_time = sum(r["elapsed"] for r in model_res) / len(model_res)
        overall_pct = int(100 * total_pass / total_checks) if total_checks else 0
        label = model_res[0]["model_label"]
        print(f"  {label}: {overall_pct}% overall accuracy, {avg_time:.1f}s avg latency")

    print()
    print("RECOMMENDATION:")
    # Find best model by overall pct
    model_scores = {}
    for mid in model_ids:
        model_res = [r for r in all_results if r["model_id"] == mid]
        if not model_res:
            continue
        total_pass = sum(r["passes"] for r in model_res)
        total_checks = sum(r["passes"] + r["issues"] for r in model_res)
        model_scores[mid] = int(100 * total_pass / total_checks) if total_checks else 0

    if model_scores:
        best_id = max(model_scores, key=model_scores.get)
        best_label = next(r["model_label"] for r in all_results if r["model_id"] == best_id)
        print(f"  Best accuracy: {best_label} ({model_scores[best_id]}%)")
        fastest_id = min(
            (mid for mid in model_ids if [r for r in all_results if r["model_id"] == mid]),
            key=lambda mid: sum(r["elapsed"] for r in all_results if r["model_id"] == mid)
        )
        fastest_label = next(r["model_label"] for r in all_results if r["model_id"] == fastest_id)
        print(f"  Fastest:       {fastest_label}")
    print()


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("InsuranceGrokBot — LLM Provider Comparison Test")
    print(f"Testing {len(SCENARIOS)} scenarios across {len(MODELS)} models\n")

    keys_found = []
    for m in MODELS:
        if os.getenv(m["api_key_env"]):
            keys_found.append(m["label"])
        else:
            print(f"  MISSING: {m['api_key_env']} — {m['label']} will be skipped")

    if not keys_found:
        print("\nNo API keys found. Set at least one of: XAI_API_KEY, GROQ_API_KEY, GEMINI_API_KEY")
        print("  xAI:    https://console.x.ai  (already in .env)")
        print("  Groq (free, no CC):   https://console.groq.com")
        print("  Gemini (free, Google account): https://aistudio.google.com/apikey")
        exit(1)

    print(f"\nRunning with: {', '.join(keys_found)}")

    all_results = []
    for model_cfg in MODELS:
        results = run_model(model_cfg, SCENARIOS)
        all_results.extend(results)

    print_summary(all_results)

    # Save raw results
    out_path = "llm_test_results.json"
    with open(out_path, "w") as f:
        safe_results = [{k: v for k, v in r.items() if k != "parsed"} for r in all_results]
        json.dump(safe_results, f, indent=2)
    print(f"Raw results saved to {out_path}")
