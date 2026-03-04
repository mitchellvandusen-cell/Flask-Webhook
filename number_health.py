# number_health.py — Smart Number Rotation & Health Engine
# Tracks per-number call metrics, health scores, and intelligently selects
# outbound caller IDs to maximize connection rates and prevent number burnout.
#
# Architecture:
#   - number_health table: per-number daily metrics (calls, connects, duration, status)
#   - select_outbound_number(): replaces hardcoded from_number selection everywhere
#   - update_number_health(): called from /voice/status callback on every call outcome
#   - Warm-up engine: new numbers ramp 50 → 100 → 200 → 300 → unlimited/day
#   - State-level geo-routing: calls to NC use NC numbers, calls to TX use TX numbers
#   - Rest/freeze: burned numbers auto-rest, frozen numbers quarantined
#   - Cron jobs: daily metric reset, warm-up progression, rest/freeze expiry

import logging
import random
import time
from datetime import datetime, timedelta
from db import get_db_connection, return_db_connection

logger = logging.getLogger("number_health")

# Short-lived cache for live Twilio numbers (avoids API call on every dial in queue)
_live_numbers_cache = {}

def invalidate_live_numbers_cache(sub_account_sid=None):
    """Clear cached live numbers (call after buy/release)."""
    if sub_account_sid:
        _live_numbers_cache.pop(f"_live_nums_{sub_account_sid}", None)
    else:
        _live_numbers_cache.clear()

# ── Constants ──────────────────────────────────────────────────────────────

# Health score thresholds
HEALTH_EXCELLENT = 80    # Green — full rotation eligibility
HEALTH_GOOD = 60         # Yellow-green — slightly deprioritized
HEALTH_WARNING = 40      # Orange — reduced volume
HEALTH_CRITICAL = 20     # Red — auto-rested
HEALTH_FROZEN = 0        # Black — quarantined, needs manual unfreeze

# Daily call caps by warm-up stage — calibrated for power dialing (300 dials/day target)
# With 6 numbers at stage 0 = 300 dials. At full warm = unlimited across pool.
WARMUP_STAGES = {
    0: {"daily_cap": 50,   "label": "Stage 0 — New",       "days_required": 0},
    1: {"daily_cap": 100,  "label": "Stage 1 — Warming",   "days_required": 3},
    2: {"daily_cap": 200,  "label": "Stage 2 — Active",    "days_required": 7},
    3: {"daily_cap": 300,  "label": "Stage 3 — Proven",    "days_required": 14},
    4: {"daily_cap": 999,  "label": "Stage 4 — Veteran",   "days_required": 30},
}

# Number statuses
STATUS_ACTIVE = "active"
STATUS_RESTING = "resting"      # Auto-rest (low health), resumes after rest_until
STATUS_FROZEN = "frozen"        # Manual or auto-freeze (critical health), needs action
STATUS_WARMUP = "warmup"        # New number being warmed up

# Default rest duration (hours)
DEFAULT_REST_HOURS = 24
DEFAULT_FREEZE_HOURS = 72

# Connection rate floor — below this, number health drops fast
MIN_CONNECT_RATE = 0.05  # 5% (10% is normal for cold outbound)

# Max calls per day before auto-rest (safety valve, even for veteran numbers)
ABSOLUTE_DAILY_CAP = 500

# Recommended numbers per state for adequate coverage
RECOMMENDED_NUMBERS_PER_STATE = 2


# ── Area Code → State Mapping ──────────────────────────────────────────────
# Complete US area code to state/territory mapping. Used for state-level
# geo-routing so calls to a state use only numbers from that state.

AREA_CODE_TO_STATE = {
    # Alabama
    205: "AL", 251: "AL", 256: "AL", 334: "AL", 938: "AL",
    # Alaska
    907: "AK",
    # Arizona
    480: "AZ", 520: "AZ", 602: "AZ", 623: "AZ", 928: "AZ",
    # Arkansas
    479: "AR", 501: "AR", 870: "AR",
    # California
    209: "CA", 213: "CA", 279: "CA", 310: "CA", 323: "CA", 341: "CA",
    350: "CA", 408: "CA", 415: "CA", 424: "CA", 442: "CA", 510: "CA",
    530: "CA", 559: "CA", 562: "CA", 619: "CA", 626: "CA", 628: "CA",
    650: "CA", 657: "CA", 661: "CA", 669: "CA", 707: "CA", 714: "CA",
    747: "CA", 760: "CA", 805: "CA", 818: "CA", 820: "CA", 831: "CA",
    840: "CA", 858: "CA", 909: "CA", 916: "CA", 925: "CA", 949: "CA",
    951: "CA",
    # Colorado
    303: "CO", 719: "CO", 720: "CO", 970: "CO",
    # Connecticut
    203: "CT", 475: "CT", 860: "CT", 959: "CT",
    # Delaware
    302: "DE",
    # Florida
    239: "FL", 305: "FL", 321: "FL", 352: "FL", 386: "FL", 407: "FL",
    448: "FL", 561: "FL", 727: "FL", 754: "FL", 772: "FL", 786: "FL",
    813: "FL", 850: "FL", 863: "FL", 904: "FL", 941: "FL", 954: "FL",
    # Georgia
    229: "GA", 404: "GA", 470: "GA", 478: "GA", 678: "GA", 706: "GA",
    762: "GA", 770: "GA", 912: "GA", 943: "GA",
    # Hawaii
    808: "HI",
    # Idaho
    208: "ID", 986: "ID",
    # Illinois
    217: "IL", 224: "IL", 309: "IL", 312: "IL", 331: "IL", 447: "IL",
    464: "IL", 618: "IL", 630: "IL", 708: "IL", 773: "IL", 779: "IL",
    815: "IL", 847: "IL", 872: "IL",
    # Indiana
    219: "IN", 260: "IN", 317: "IN", 463: "IN", 574: "IN", 765: "IN",
    812: "IN", 930: "IN",
    # Iowa
    319: "IA", 515: "IA", 563: "IA", 641: "IA", 712: "IA",
    # Kansas
    316: "KS", 620: "KS", 785: "KS", 913: "KS",
    # Kentucky
    270: "KY", 364: "KY", 502: "KY", 606: "KY", 859: "KY",
    # Louisiana
    225: "LA", 318: "LA", 337: "LA", 504: "LA", 985: "LA",
    # Maine
    207: "ME",
    # Maryland
    240: "MD", 301: "MD", 410: "MD", 443: "MD", 667: "MD",
    # Massachusetts
    339: "MA", 351: "MA", 413: "MA", 508: "MA", 617: "MA", 774: "MA",
    781: "MA", 857: "MA", 978: "MA",
    # Michigan
    231: "MI", 248: "MI", 269: "MI", 313: "MI", 517: "MI", 586: "MI",
    616: "MI", 734: "MI", 810: "MI", 906: "MI", 947: "MI", 989: "MI",
    # Minnesota
    218: "MN", 320: "MN", 507: "MN", 612: "MN", 651: "MN", 763: "MN",
    952: "MN",
    # Mississippi
    228: "MS", 601: "MS", 662: "MS", 769: "MS",
    # Missouri
    314: "MO", 417: "MO", 573: "MO", 636: "MO", 660: "MO", 816: "MO",
    975: "MO",
    # Montana
    406: "MT",
    # Nebraska
    308: "NE", 402: "NE", 531: "NE",
    # Nevada
    702: "NV", 725: "NV", 775: "NV",
    # New Hampshire
    603: "NH",
    # New Jersey
    201: "NJ", 551: "NJ", 609: "NJ", 640: "NJ", 732: "NJ", 848: "NJ",
    856: "NJ", 862: "NJ", 908: "NJ", 973: "NJ",
    # New Mexico
    505: "NM", 575: "NM",
    # New York
    212: "NY", 315: "NY", 332: "NY", 347: "NY", 516: "NY", 518: "NY",
    585: "NY", 607: "NY", 631: "NY", 646: "NY", 680: "NY", 716: "NY",
    718: "NY", 838: "NY", 845: "NY", 914: "NY", 917: "NY", 929: "NY",
    934: "NY",
    # North Carolina
    252: "NC", 336: "NC", 704: "NC", 743: "NC", 828: "NC", 910: "NC",
    919: "NC", 980: "NC", 984: "NC",
    # North Dakota
    701: "ND",
    # Ohio
    216: "OH", 220: "OH", 234: "OH", 326: "OH", 330: "OH", 380: "OH",
    419: "OH", 440: "OH", 513: "OH", 567: "OH", 614: "OH", 740: "OH",
    937: "OH",
    # Oklahoma
    405: "OK", 539: "OK", 572: "OK", 580: "OK", 918: "OK",
    # Oregon
    458: "OR", 503: "OR", 541: "OR", 971: "OR",
    # Pennsylvania
    215: "PA", 223: "PA", 267: "PA", 272: "PA", 412: "PA", 445: "PA",
    484: "PA", 570: "PA", 582: "PA", 610: "PA", 717: "PA", 724: "PA",
    814: "PA", 835: "PA", 878: "PA",
    # Rhode Island
    401: "RI",
    # South Carolina
    803: "SC", 839: "SC", 843: "SC", 854: "SC", 864: "SC",
    # South Dakota
    605: "SD",
    # Tennessee
    423: "TN", 615: "TN", 629: "TN", 731: "TN", 865: "TN", 901: "TN",
    931: "TN",
    # Texas
    210: "TX", 214: "TX", 254: "TX", 281: "TX", 325: "TX", 346: "TX",
    361: "TX", 409: "TX", 430: "TX", 432: "TX", 469: "TX", 512: "TX",
    682: "TX", 713: "TX", 726: "TX", 737: "TX", 806: "TX", 817: "TX",
    830: "TX", 832: "TX", 903: "TX", 915: "TX", 936: "TX", 940: "TX",
    956: "TX", 972: "TX", 979: "TX",
    # Utah
    385: "UT", 435: "UT", 801: "UT",
    # Vermont
    802: "VT",
    # Virginia
    276: "VA", 434: "VA", 540: "VA", 571: "VA", 703: "VA", 757: "VA",
    804: "VA", 826: "VA",
    # Washington
    206: "WA", 253: "WA", 360: "WA", 425: "WA", 509: "WA", 564: "WA",
    # Washington DC
    202: "DC",
    # West Virginia
    304: "WV", 681: "WV",
    # Wisconsin
    262: "WI", 274: "WI", 414: "WI", 534: "WI", 608: "WI", 715: "WI",
    920: "WI",
    # Wyoming
    307: "WY",
    # Territories
    340: "VI", 671: "GU", 684: "AS", 787: "PR", 939: "PR",
}

# State abbreviation → full name (for UI display)
STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "Washington DC", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine",
    "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska",
    "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "PR": "Puerto Rico", "VI": "US Virgin Islands", "GU": "Guam", "AS": "American Samoa",
}


def phone_to_state(phone):
    """Extract area code from E.164 phone and return 2-letter state code or None."""
    if not phone:
        return None
    digits = phone.lstrip("+").lstrip("1")
    if len(digits) < 3:
        return None
    try:
        area_code = int(digits[:3])
    except ValueError:
        return None
    return AREA_CODE_TO_STATE.get(area_code)


def get_state_coverage(phones):
    """
    Analyze state coverage for a list of phone numbers.
    Returns dict: { "TX": ["+12105551234", "+12145559999"], "NC": ["+19195551111"], ... }
    """
    coverage = {}
    for phone in phones:
        state = phone_to_state(phone)
        if state:
            coverage.setdefault(state, []).append(phone)
    return coverage


def get_state_coverage_recommendations(phones, contact_states=None):
    """
    Generate recommendations for state coverage gaps.

    Args:
        phones: list of owned phone numbers
        contact_states: optional dict of {state: contact_count} from call history

    Returns list of recommendations:
        [{"state": "NC", "state_name": "North Carolina", "owned": 1, "recommended": 2,
          "need": 1, "contacts": 45, "priority": "high"}, ...]
    """
    coverage = get_state_coverage(phones)
    recommendations = []

    # If we have contact data, prioritize states with more contacts
    states_to_check = set()
    if contact_states:
        # Only consider states with meaningful call volume (5+ contacts)
        states_to_check = {s for s, c in contact_states.items() if c >= 5}
    # Also include states we already have numbers in
    states_to_check |= set(coverage.keys())

    for state in sorted(states_to_check):
        owned = len(coverage.get(state, []))
        contacts = contact_states.get(state, 0) if contact_states else 0

        # Determine priority based on contact volume
        if owned == 0 and contacts >= 20:
            priority = "critical"  # Heavy volume with no local number
        elif owned == 0 and contacts >= 10:
            priority = "high"
        elif owned < RECOMMENDED_NUMBERS_PER_STATE and contacts >= 10:
            priority = "medium"
        elif owned < RECOMMENDED_NUMBERS_PER_STATE and contacts >= 5:
            priority = "low"
        elif owned >= RECOMMENDED_NUMBERS_PER_STATE:
            priority = "good"  # Adequate coverage
        else:
            continue  # Skip states with too few contacts to recommend

        recommendations.append({
            "state": state,
            "state_name": STATE_NAMES.get(state, state),
            "owned": owned,
            "numbers": coverage.get(state, []),
            "recommended": RECOMMENDED_NUMBERS_PER_STATE,
            "need": max(0, RECOMMENDED_NUMBERS_PER_STATE - owned),
            "contacts": contacts,
            "priority": priority,
        })

    # Sort: critical first, then high, then by contact count
    priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "good": 4}
    recommendations.sort(key=lambda r: (priority_order.get(r["priority"], 5), -r["contacts"]))

    return recommendations


def get_contact_state_distribution(location_id):
    """
    Analyze call history to find which states the user's contacts are in.
    Returns {state_code: contact_count} for states with > 0 calls.
    """
    conn = get_db_connection()
    if not conn:
        return {}
    try:
        cur = conn.cursor()
        # Get unique destination phones from call history
        cur.execute("""
            SELECT phone, COUNT(DISTINCT contact_id) AS contacts
            FROM call_history
            WHERE location_id = %s AND direction = 'outbound' AND phone IS NOT NULL
            GROUP BY phone
        """, (location_id,))
        rows = cur.fetchall()
        cur.close()

        state_counts = {}
        for row in rows:
            state = phone_to_state(row["phone"])
            if state:
                state_counts[state] = state_counts.get(state, 0) + (row["contacts"] or 1)
        return state_counts
    except Exception as e:
        logger.error(f"Failed to get contact state distribution: {e}")
        return {}
    finally:
        return_db_connection(conn)


# ── Health Score Calculator ────────────────────────────────────────────────

def calculate_health_score(total_calls, connected_calls, no_answers, failed_calls,
                           avg_duration, warmup_stage, days_active):
    """
    Calculate a 0-100 health score for a phone number based on call metrics.

    Calibrated for cold outbound insurance dialing where 10% pickup rate is
    normal and expected. Numbers should NOT be penalized for low connection
    rates — only for truly anomalous signals like high hard-failure rates
    (network errors, carrier blocks) vs normal no-answers.

    Key principle: numbers with insufficient data (under 100 lifetime calls
    OR under 3 days active) always score 75+ ("healthy"). We refuse to
    downgrade numbers that simply haven't been used enough yet — there's no
    statistical signal in 0/50 dials showing 0% connect.

    Scoring breakdown:
      - Baseline:            50 points (every active number starts healthy)
      - Hard failure penalty: -20 points max (carrier blocks, network errors — NOT no-answers)
      - Call quality bonus:   20 points (avg duration when connected)
      - Connection bonus:     15 points (rewards above-average connect rates)
      - Maturity bonus:       15 points (warm-up stage + days active)
    """
    # ── Insufficient data: return a confident healthy score ──
    # Under 100 lifetime calls or under 3 days of data — there is no
    # meaningful signal yet. Don't scare users with red scores on day 1.
    MIN_CALLS_FOR_SCORING = 100
    MIN_DAYS_FOR_SCORING = 3

    if total_calls < MIN_CALLS_FOR_SCORING or days_active < MIN_DAYS_FOR_SCORING:
        # Start at 80 (solid green), give small bonuses for early good signals
        score = 80.0
        if connected_calls > 0 and avg_duration >= 30:
            score += 5.0  # Some calls connected and had real conversations
        if connected_calls > 5:
            score += 5.0  # Multiple connections — great early sign
        # Only penalize if there's an extremely high hard-fail rate even in early data
        if total_calls >= 20:
            hard_fail_rate = failed_calls / total_calls
            if hard_fail_rate > 0.50:
                score -= 15.0  # More than half failing = something is clearly wrong
        return round(min(100, max(50, score)), 1)

    # ── Full scoring: only applied with 100+ calls AND 3+ days of data ──
    score = 50.0  # Baseline — numbers are healthy until proven otherwise

    # 1. Hard failure penalty (up to -20 points)
    # Only penalize actual failures (busy, network error, carrier block).
    # No-answers are NORMAL for cold outbound — they are NOT failures.
    if total_calls > 0:
        hard_fail_rate = failed_calls / total_calls
        if hard_fail_rate <= 0.05:
            pass  # No penalty — under 5% hard failure is fine
        elif hard_fail_rate <= 0.15:
            score -= (hard_fail_rate - 0.05) / 0.10 * 10.0  # Up to -10
        elif hard_fail_rate <= 0.30:
            score -= 10.0 + (hard_fail_rate - 0.15) / 0.15 * 10.0  # Up to -20
        else:
            score -= 20.0  # Max penalty

    # 2. Call quality bonus (up to +20 points)
    # Rewards numbers that produce real conversations when connected
    if avg_duration >= 120:   # 2+ min avg = excellent
        score += 20.0
    elif avg_duration >= 60:  # 1+ min = good
        score += 12.0 + (avg_duration - 60) / 60.0 * 8.0
    elif avg_duration >= 20:  # 20s+ = decent
        score += 5.0 + (avg_duration - 20) / 40.0 * 7.0
    elif avg_duration > 0:
        score += avg_duration / 20.0 * 5.0
    else:
        score += 10.0  # Neutral — no duration data yet

    # 3. Connection rate bonus (up to +15 points)
    # This is a BONUS for above-average rates, not a penalty for normal ones.
    # 10% connect rate is the industry norm for cold outbound — no penalty.
    if total_calls > 0:
        connect_rate = connected_calls / total_calls
        if connect_rate >= 0.25:
            score += 15.0   # Exceptional
        elif connect_rate >= 0.15:
            score += 8.0 + (connect_rate - 0.15) / 0.10 * 7.0
        elif connect_rate >= 0.08:
            score += (connect_rate - 0.08) / 0.07 * 8.0
        # Below 8% = no bonus (but no penalty either)
    else:
        score += 8.0  # Neutral

    # 4. Maturity bonus (up to +15 points)
    stage_bonus = min(warmup_stage, 4) * 2.5  # 0-10 points
    age_bonus = min(days_active / 30.0, 1.0) * 5.0  # 0-5 points over 30 days
    score += stage_bonus + age_bonus

    return round(min(100, max(0, score)), 1)


# ── Smart Number Selection ─────────────────────────────────────────────────

def select_outbound_number(location_id, voice_config, dest_phone=None):
    """
    Intelligently select the best outbound number for a call.

    Selection algorithm:
    1. Gather all numbers (primary + local presence pool)
    2. Filter out frozen/resting numbers and numbers at daily cap
    3. State-level geo-routing: if dest is TX, only use TX numbers (if available)
    4. Weight remaining numbers by health score (higher health = more likely selected)
    5. Return the selected number

    Returns:
        dict: {"phone": "+1...", "reason": "...", "health_score": N, "state_match": bool}
        or None if rotation disabled
    """
    rotation_config = voice_config.get("number_rotation", {})
    if not rotation_config.get("enabled", False):
        # Rotation disabled — use existing logic (primary + local presence)
        return None

    primary = voice_config.get("twilio_phone_number", "")

    # Fetch live numbers from Twilio sub-account (not stale voice_config list)
    # Cache for 60s to avoid Twilio API calls on every dial in a queue run
    sub_sid = voice_config.get("twilio_sub_account_sid", "")
    all_numbers = []
    if sub_sid:
        try:
            import twilio_provisioning
            cache_key = f"_live_nums_{sub_sid}"
            cached = _live_numbers_cache.get(cache_key)
            if cached and (time.time() - cached["ts"]) < 60:
                all_numbers = cached["numbers"]
            else:
                live = twilio_provisioning.list_phone_numbers(sub_sid)
                all_numbers = [n.get("phone", "") for n in live if n.get("phone")]
                _live_numbers_cache[cache_key] = {"numbers": all_numbers, "ts": time.time()}
        except Exception as e:
            logger.warning(f"Smart rotation: could not fetch live numbers, falling back to voice_config: {e}")
    if not all_numbers:
        # Fallback to voice_config if Twilio fetch fails or no sub-account
        local_pool = voice_config.get("local_presence_numbers", [])
        all_numbers = list(set([primary] + local_pool)) if primary else list(set(local_pool))
    else:
        # Ensure primary is in the pool
        if primary and primary not in all_numbers:
            all_numbers.append(primary)
    all_numbers = [p for p in all_numbers if p]  # Filter empty strings

    if not all_numbers:
        return None

    # Fetch health data for all numbers
    health_map = get_number_health_batch(location_id, all_numbers)

    # Build candidates list
    candidates = []
    now = datetime.utcnow()

    for phone in all_numbers:
        h = health_map.get(phone)
        if not h:
            # No health record yet — treat as new healthy number
            candidates.append({
                "phone": phone,
                "health_score": 75.0,
                "daily_calls": 0,
                "daily_cap": WARMUP_STAGES[0]["daily_cap"],
                "status": STATUS_WARMUP,
                "warmup_stage": 0,
                "state": phone_to_state(phone),
            })
            continue

        status = h.get("status", STATUS_ACTIVE)

        # Skip frozen numbers
        if status == STATUS_FROZEN:
            continue

        # Skip resting numbers (unless rest period expired)
        if status == STATUS_RESTING:
            rest_until = h.get("rest_until")
            if rest_until and now < rest_until:
                continue
            # Rest expired — will be re-activated by cron, but allow it now

        # Check daily cap
        daily_calls = h.get("daily_calls_today", 0)
        warmup_stage = h.get("warmup_stage", 4)
        daily_cap = WARMUP_STAGES.get(warmup_stage, WARMUP_STAGES[4])["daily_cap"]

        # Override cap from config if set
        config_cap = rotation_config.get("daily_cap_override")
        if config_cap and config_cap > 0:
            daily_cap = min(daily_cap, config_cap)

        # Safety valve
        daily_cap = min(daily_cap, ABSOLUTE_DAILY_CAP)

        if daily_calls >= daily_cap:
            continue

        candidates.append({
            "phone": phone,
            "health_score": float(h.get("health_score", 50.0)),
            "daily_calls": daily_calls,
            "daily_cap": daily_cap,
            "status": status,
            "warmup_stage": warmup_stage,
            "state": phone_to_state(phone),
        })

    if not candidates:
        # All numbers at cap or frozen — fall back to primary
        return {"phone": primary, "reason": "fallback_all_exhausted", "health_score": 0, "state_match": False}

    # ── State-level geo-routing ──
    # If dest phone maps to a state, prefer numbers from that same state.
    # This is statewide local presence — not just area code matching.
    state_match = False
    dest_state = phone_to_state(dest_phone) if dest_phone else None

    if dest_state:
        state_candidates = [c for c in candidates if c.get("state") == dest_state]
        if state_candidates:
            candidates = state_candidates
            state_match = True

    # Weighted random selection by health score
    strategy = rotation_config.get("strategy", "weighted_health")

    if strategy == "round_robin":
        # Simple round robin — pick the one with fewest calls today
        candidates.sort(key=lambda c: (c["daily_calls"], -c["health_score"]))
        selected = candidates[0]
        reason = "round_robin"

    elif strategy == "highest_health":
        # Always pick the healthiest number
        candidates.sort(key=lambda c: -c["health_score"])
        selected = candidates[0]
        reason = "highest_health"

    else:
        # Default: weighted_health — higher health = higher probability
        # Add small floor so even low-health numbers have a chance
        weights = [max(c["health_score"], 5.0) for c in candidates]
        total_weight = sum(weights)
        rand = random.random() * total_weight
        cumulative = 0
        selected = candidates[0]
        for i, c in enumerate(candidates):
            cumulative += weights[i]
            if rand <= cumulative:
                selected = c
                break
        reason = "weighted_health"

    if state_match:
        reason += f"_state_{dest_state}"

    return {
        "phone": selected["phone"],
        "reason": reason,
        "health_score": selected["health_score"],
        "daily_calls": selected["daily_calls"],
        "daily_cap": selected["daily_cap"],
        "warmup_stage": selected.get("warmup_stage", 4),
        "state_match": state_match,
        "state": selected.get("state", ""),
    }


# ── Health Update (called after every call) ─────────────────────────────────


# SIP response codes that indicate carrier-level blocking.
# 403 = Forbidden (carrier block), 603 = Decline (carrier reject),
# 607 = Unwanted (robocall/spam filter), 608 = Rejected (explicit block).
CARRIER_BLOCK_SIP_CODES = {403, 603, 607, 608}


def update_number_health(location_id, phone, call_status, duration=0, sip_code=None):
    """
    Update health metrics for a phone number after a call completes.

    Called from /voice/status callback for terminal statuses:
    completed, busy, no-answer, failed, canceled.

    sip_code: Twilio's SipResponseCode (int or None). Codes 403/603/607/608
    indicate the carrier blocked the call vs a normal no-answer.
    """
    if not phone or not location_id:
        return

    conn = get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor()
        now = datetime.utcnow()

        # Upsert: create row if doesn't exist, update metrics
        cur.execute("""
            INSERT INTO number_health (location_id, phone, status, warmup_stage,
                                        daily_calls_today, daily_connected, daily_no_answer,
                                        daily_failed, daily_busy, daily_duration_secs,
                                        total_calls, total_connected, total_no_answer,
                                        total_failed, total_busy, total_duration_secs,
                                        health_score, last_used_at, created_at, updated_at)
            VALUES (%s, %s, 'active', 0,
                    0, 0, 0, 0, 0, 0,
                    0, 0, 0, 0, 0, 0,
                    75.0, %s, %s, %s)
            ON CONFLICT (location_id, phone) DO NOTHING
        """, (location_id, phone, now, now, now))

        # Check if this call was carrier-blocked via SIP response code
        is_carrier_blocked = False
        if sip_code:
            try:
                is_carrier_blocked = int(sip_code) in CARRIER_BLOCK_SIP_CODES
            except (ValueError, TypeError):
                pass

        # Determine which counters to increment
        # Twilio "completed" means the call was answered, even if duration=0
        is_connected = call_status == "completed"
        is_no_answer = call_status in ("no-answer", "canceled")
        is_failed = call_status == "failed"
        is_busy = call_status == "busy"

        # Build dynamic UPDATE
        sets = ["daily_calls_today = daily_calls_today + 1",
                "total_calls = total_calls + 1",
                "last_used_at = %s",
                "updated_at = %s"]
        params = [now, now]

        if is_carrier_blocked:
            sets.append("daily_carrier_blocked = daily_carrier_blocked + 1")
            sets.append("total_carrier_blocked = total_carrier_blocked + 1")
            # Also count as failed for health score purposes
            sets.append("daily_failed = daily_failed + 1")
            sets.append("total_failed = total_failed + 1")
            logger.warning(f"Carrier-blocked call detected: {phone} SIP={sip_code}")
        elif is_connected:
            sets.append("daily_connected = daily_connected + 1")
            sets.append("total_connected = total_connected + 1")
            sets.append("daily_duration_secs = daily_duration_secs + %s")
            sets.append("total_duration_secs = total_duration_secs + %s")
            params.extend([int(duration), int(duration)])
        elif is_no_answer:
            sets.append("daily_no_answer = daily_no_answer + 1")
            sets.append("total_no_answer = total_no_answer + 1")
        elif is_failed:
            sets.append("daily_failed = daily_failed + 1")
            sets.append("total_failed = total_failed + 1")
        elif is_busy:
            sets.append("daily_busy = daily_busy + 1")
            sets.append("total_busy = total_busy + 1")

        params.extend([location_id, phone])
        cur.execute(
            f"UPDATE number_health SET {', '.join(sets)} WHERE location_id = %s AND phone = %s",
            params
        )

        # Re-calculate health score
        cur.execute("""
            SELECT total_calls, total_connected, total_no_answer, total_failed,
                   total_duration_secs, warmup_stage, created_at
            FROM number_health
            WHERE location_id = %s AND phone = %s
        """, (location_id, phone))
        row = cur.fetchone()
        if row:
            tc = row["total_calls"] or 0
            days_active = max(1, (now - row["created_at"]).days) if row["created_at"] else 1
            avg_dur = (row["total_duration_secs"] / row["total_connected"]) if row["total_connected"] else 0
            new_score = calculate_health_score(
                total_calls=tc,
                connected_calls=row["total_connected"] or 0,
                no_answers=row["total_no_answer"] or 0,
                failed_calls=row["total_failed"] or 0,
                avg_duration=avg_dur,
                warmup_stage=row["warmup_stage"] or 0,
                days_active=days_active,
            )
            cur.execute(
                "UPDATE number_health SET health_score = %s WHERE location_id = %s AND phone = %s",
                (new_score, location_id, phone)
            )

            # Auto-rest only when a number is clearly blocked by carriers:
            # 300+ dials AND under 2% connection rate. Normal cold outbound
            # is ~10% pickup — only flag genuinely burned numbers.
            cc = row["total_connected"] or 0
            connect_pct = (cc / tc * 100) if tc > 0 else 100
            if tc >= 300 and connect_pct < 2.0:
                rest_until = now + timedelta(hours=DEFAULT_REST_HOURS)
                cur.execute("""
                    UPDATE number_health
                    SET status = %s, rest_until = %s
                    WHERE location_id = %s AND phone = %s AND status != %s
                """, (STATUS_RESTING, rest_until, location_id, phone, STATUS_FROZEN))
                logger.warning(f"Number {phone} auto-rested (health={new_score}, connect={connect_pct:.1f}%, dials={tc}) until {rest_until}")

            # Auto-freeze: same criteria but even worse — 500+ dials, under 1%
            if tc >= 500 and connect_pct < 1.0:
                freeze_until = now + timedelta(hours=DEFAULT_FREEZE_HOURS)
                cur.execute("""
                    UPDATE number_health
                    SET status = %s, rest_until = %s
                    WHERE location_id = %s AND phone = %s
                """, (STATUS_FROZEN, freeze_until, location_id, phone))
                logger.warning(f"Number {phone} auto-FROZEN (health={new_score}, connect={connect_pct:.1f}%, dials={tc}) until {freeze_until}")

        conn.commit()
        cur.close()
    except Exception as e:
        logger.error(f"Failed to update number health for {phone}: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        return_db_connection(conn)


# ── DB Query Helpers ───────────────────────────────────────────────────────

def get_number_health_batch(location_id, phones):
    """Fetch health data for multiple numbers in one query. Returns {phone: {row}}."""
    if not phones:
        return {}
    conn = get_db_connection()
    if not conn:
        return {}
    try:
        cur = conn.cursor()
        placeholders = ",".join(["%s"] * len(phones))
        cur.execute(f"""
            SELECT * FROM number_health
            WHERE location_id = %s AND phone IN ({placeholders})
        """, [location_id] + list(phones))
        rows = cur.fetchall()
        cur.close()
        return {r["phone"]: dict(r) for r in rows}
    except Exception as e:
        logger.error(f"Failed to batch-fetch number health: {e}")
        return {}
    finally:
        return_db_connection(conn)


def get_all_number_health(location_id):
    """Fetch all health records for a location. Returns list of dicts."""
    conn = get_db_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM number_health
            WHERE location_id = %s
            ORDER BY health_score DESC
        """, (location_id,))
        rows = cur.fetchall()
        cur.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"Failed to fetch all number health: {e}")
        return []
    finally:
        return_db_connection(conn)


def reset_daily_metrics():
    """
    Reset daily counters for all numbers. Called by daily cron job.
    Preserves total/lifetime counters.
    """
    conn = get_db_connection()
    if not conn:
        return 0
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE number_health
            SET daily_calls_today = 0,
                daily_connected = 0,
                daily_no_answer = 0,
                daily_failed = 0,
                daily_busy = 0,
                daily_carrier_blocked = 0,
                daily_duration_secs = 0,
                updated_at = NOW()
        """)
        count = cur.rowcount
        conn.commit()
        cur.close()
        logger.info(f"Daily metrics reset for {count} number(s)")
        return count
    except Exception as e:
        logger.error(f"Failed to reset daily metrics: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return 0
    finally:
        return_db_connection(conn)


def expire_resting_numbers():
    """
    Re-activate numbers whose rest period has expired.
    Called by cron job (every 15 min or hourly).
    """
    conn = get_db_connection()
    if not conn:
        return 0
    try:
        cur = conn.cursor()
        now = datetime.utcnow()
        cur.execute("""
            UPDATE number_health
            SET status = %s, rest_until = NULL, updated_at = %s
            WHERE status = %s AND rest_until IS NOT NULL AND rest_until <= %s
        """, (STATUS_ACTIVE, now, STATUS_RESTING, now))
        resting_count = cur.rowcount

        cur.execute("""
            UPDATE number_health
            SET status = %s, rest_until = NULL, updated_at = %s
            WHERE status = %s AND rest_until IS NOT NULL AND rest_until <= %s
        """, (STATUS_ACTIVE, now, STATUS_FROZEN, now))
        frozen_count = cur.rowcount

        conn.commit()
        cur.close()
        total = resting_count + frozen_count
        if total > 0:
            logger.info(f"Expired {resting_count} resting + {frozen_count} frozen numbers -> active")
        return total
    except Exception as e:
        logger.error(f"Failed to expire resting numbers: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return 0
    finally:
        return_db_connection(conn)


def advance_warmup_stages():
    """
    Progress numbers through warm-up stages based on days active.
    Called by daily cron job.
    """
    conn = get_db_connection()
    if not conn:
        return 0
    try:
        cur = conn.cursor()
        now = datetime.utcnow()
        advanced = 0

        for stage in range(4):  # 0->1, 1->2, 2->3, 3->4
            next_stage = stage + 1
            days_required = WARMUP_STAGES[next_stage]["days_required"]
            cur.execute("""
                UPDATE number_health
                SET warmup_stage = %s, updated_at = %s
                WHERE warmup_stage = %s
                  AND status IN (%s, %s)
                  AND created_at <= %s
                  AND health_score >= %s
            """, (
                next_stage, now,
                stage,
                STATUS_ACTIVE, STATUS_WARMUP,
                now - timedelta(days=days_required),
                HEALTH_WARNING,  # Must have decent health to advance
            ))
            advanced += cur.rowcount

        # Graduate warmup status to active for stage 4
        cur.execute("""
            UPDATE number_health
            SET status = %s, updated_at = %s
            WHERE warmup_stage >= 4 AND status = %s
        """, (STATUS_ACTIVE, now, STATUS_WARMUP))
        advanced += cur.rowcount

        conn.commit()
        cur.close()
        if advanced > 0:
            logger.info(f"Advanced {advanced} number(s) through warm-up stages")
        return advanced
    except Exception as e:
        logger.error(f"Failed to advance warm-up stages: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return 0
    finally:
        return_db_connection(conn)


def set_number_status(location_id, phone, status, rest_hours=None):
    """Manually set a number's status (e.g. freeze/unfreeze from dashboard)."""
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        now = datetime.utcnow()
        rest_until = (now + timedelta(hours=rest_hours)) if rest_hours else None
        cur.execute("""
            UPDATE number_health
            SET status = %s, rest_until = %s, updated_at = %s
            WHERE location_id = %s AND phone = %s
        """, (status, rest_until, now, location_id, phone))
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        logger.error(f"Failed to set number status: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    finally:
        return_db_connection(conn)


def get_number_health_summary(location_id):
    """
    Get a summary overview of number health for the dashboard.
    Returns aggregate stats across all numbers.
    """
    conn = get_db_connection()
    if not conn:
        return {}
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT
                COUNT(*) AS total_numbers,
                COUNT(*) FILTER (WHERE status = 'active') AS active_count,
                COUNT(*) FILTER (WHERE status = 'resting') AS resting_count,
                COUNT(*) FILTER (WHERE status = 'frozen') AS frozen_count,
                COUNT(*) FILTER (WHERE status = 'warmup') AS warmup_count,
                COALESCE(AVG(health_score), 0) AS avg_health,
                COALESCE(MIN(health_score), 0) AS min_health,
                COALESCE(MAX(health_score), 0) AS max_health,
                COALESCE(SUM(daily_calls_today), 0) AS total_daily_calls,
                COALESCE(SUM(daily_connected), 0) AS total_daily_connected,
                COALESCE(SUM(total_calls), 0) AS total_lifetime_calls,
                COALESCE(SUM(total_connected), 0) AS total_lifetime_connected
            FROM number_health
            WHERE location_id = %s
        """, (location_id,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return {}

        total_daily = int(row["total_daily_calls"] or 0)
        connected_daily = int(row["total_daily_connected"] or 0)
        total_lifetime = int(row["total_lifetime_calls"] or 0)
        connected_lifetime = int(row["total_lifetime_connected"] or 0)

        return {
            "total_numbers": row["total_numbers"],
            "active_count": row["active_count"],
            "resting_count": row["resting_count"],
            "frozen_count": row["frozen_count"],
            "warmup_count": row["warmup_count"],
            "avg_health": round(float(row["avg_health"] or 0), 1),
            "min_health": round(float(row["min_health"] or 0), 1),
            "max_health": round(float(row["max_health"] or 0), 1),
            "daily_calls": total_daily,
            "daily_connected": connected_daily,
            "daily_connect_rate": round(connected_daily / total_daily * 100, 1) if total_daily else 0,
            "lifetime_calls": total_lifetime,
            "lifetime_connected": connected_lifetime,
            "lifetime_connect_rate": round(connected_lifetime / total_lifetime * 100, 1) if total_lifetime else 0,
        }
    except Exception as e:
        logger.error(f"Failed to get number health summary: {e}")
        return {}
    finally:
        return_db_connection(conn)


def ensure_number_health_records(location_id, voice_config):
    """
    Ensure health records exist for all numbers in voice_config.
    Called when rotation is first enabled or numbers change.
    """
    primary = voice_config.get("twilio_phone_number", "")
    local_pool = voice_config.get("local_presence_numbers", [])
    all_numbers = list(set([primary] + local_pool)) if primary else list(set(local_pool))

    if not all_numbers:
        return

    conn = get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor()
        now = datetime.utcnow()
        for phone in all_numbers:
            if not phone:
                continue
            cur.execute("""
                INSERT INTO number_health (location_id, phone, status, warmup_stage,
                                            health_score, created_at, updated_at)
                VALUES (%s, %s, %s, 0, 75.0, %s, %s)
                ON CONFLICT (location_id, phone) DO NOTHING
            """, (location_id, phone, STATUS_WARMUP, now, now))
        conn.commit()
        cur.close()
    except Exception as e:
        logger.error(f"Failed to ensure number health records: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        return_db_connection(conn)
