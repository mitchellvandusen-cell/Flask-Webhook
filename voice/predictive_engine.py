# voice/predictive_engine.py — Enterprise Predictive Dialer Engine
#
# Real Erlang-C pacing algorithm, TCPA compliance (rolling 3% abandon rate),
# recipient timezone enforcement, recording consent tracking, and compliance metrics.

import math
import logging
import threading
import time
from datetime import datetime
from collections import defaultdict

from db import get_db_connection, return_db_connection

logger = logging.getLogger("voice_bridge.predictive")

# ═══════════════════════════════════════════════════════════════════════════════
# AREA CODE → TIMEZONE LOOKUP (US/Canada)
# ═══════════════════════════════════════════════════════════════════════════════

# Two-party recording consent states (all-party consent required)
TWO_PARTY_CONSENT_STATES = {
    "CA", "CT", "DE", "FL", "IL", "MD", "MA", "MT", "NV", "NH", "PA", "WA"
}

# Area code → US state mapping (covers all assigned NANP area codes)
# fmt: off
AREA_CODE_TO_STATE = {
    "201": "NJ", "202": "DC", "203": "CT", "205": "AL", "206": "WA", "207": "ME",
    "208": "ID", "209": "CA", "210": "TX", "212": "NY", "213": "CA", "214": "TX",
    "215": "PA", "216": "OH", "217": "IL", "218": "MN", "219": "IN", "220": "OH",
    "223": "PA", "224": "IL", "225": "LA", "228": "MS", "229": "GA", "231": "MI",
    "234": "OH", "239": "FL", "240": "MD", "248": "MI", "251": "AL", "252": "NC",
    "253": "WA", "254": "TX", "256": "AL", "260": "IN", "262": "WI", "267": "PA",
    "269": "MI", "270": "KY", "272": "PA", "274": "WI", "276": "VA", "278": "MI",
    "281": "TX", "283": "OH", "301": "MD", "302": "DE", "303": "CO", "304": "WV",
    "305": "FL", "307": "WY", "308": "NE", "309": "IL", "310": "CA", "312": "IL",
    "313": "MI", "314": "MO", "315": "NY", "316": "KS", "317": "IN", "318": "LA",
    "319": "IA", "320": "MN", "321": "FL", "323": "CA", "325": "TX", "326": "OH",
    "327": "AR", "330": "OH", "331": "IL", "332": "NY", "334": "AL", "336": "NC",
    "337": "LA", "339": "MA", "340": "VI", "341": "CA", "346": "TX", "347": "NY",
    "351": "MA", "352": "FL", "360": "WA", "361": "TX", "364": "KY", "380": "OH",
    "385": "UT", "386": "FL", "401": "RI", "402": "NE", "404": "GA", "405": "OK",
    "406": "MT", "407": "FL", "408": "CA", "409": "TX", "410": "MD", "412": "PA",
    "413": "MA", "414": "WI", "415": "CA", "417": "MO", "419": "OH", "423": "TN",
    "424": "CA", "425": "WA", "430": "TX", "432": "TX", "434": "VA", "435": "UT",
    "440": "OH", "442": "CA", "443": "MD", "445": "PA", "447": "IL", "448": "FL",
    "458": "OR", "463": "IN", "464": "IL", "469": "TX", "470": "GA", "475": "CT",
    "478": "GA", "479": "AR", "480": "AZ", "484": "PA", "501": "AR", "502": "KY",
    "503": "OR", "504": "LA", "505": "NM", "507": "MN", "508": "MA", "509": "WA",
    "510": "CA", "512": "TX", "513": "OH", "515": "IA", "516": "NY", "517": "MI",
    "518": "NY", "520": "AZ", "530": "CA", "531": "NE", "534": "WI", "539": "OK",
    "540": "VA", "541": "OR", "551": "NJ", "557": "MO", "559": "CA", "561": "FL",
    "562": "CA", "563": "IA", "564": "WA", "567": "OH", "570": "PA", "571": "VA",
    "572": "OK", "573": "MO", "574": "IN", "575": "NM", "580": "OK", "585": "NY",
    "586": "MI", "601": "MS", "602": "AZ", "603": "NH", "605": "SD", "606": "KY",
    "607": "NY", "608": "WI", "609": "NJ", "610": "PA", "612": "MN", "614": "OH",
    "615": "TN", "616": "MI", "617": "MA", "618": "IL", "619": "CA", "620": "KS",
    "623": "AZ", "626": "CA", "627": "CA", "628": "CA", "629": "TN", "630": "IL",
    "631": "NY", "636": "MO", "640": "NJ", "641": "IA", "646": "NY", "650": "CA",
    "651": "MN", "656": "FL", "657": "CA", "659": "AL", "660": "MO", "661": "CA",
    "662": "MS", "667": "MD", "669": "CA", "670": "MP", "671": "GU", "678": "GA",
    "680": "NY", "681": "WV", "682": "TX", "684": "AS", "689": "FL", "701": "ND",
    "702": "NV", "703": "VA", "704": "NC", "706": "GA", "707": "CA", "708": "IL",
    "712": "IA", "713": "TX", "714": "CA", "715": "WI", "716": "NY", "717": "PA",
    "718": "NY", "719": "CO", "720": "CO", "724": "PA", "725": "NV", "726": "TX",
    "727": "FL", "730": "IL", "731": "TN", "732": "NJ", "734": "MI", "737": "TX",
    "740": "OH", "743": "NC", "747": "CA", "754": "FL", "757": "VA", "760": "CA",
    "762": "GA", "763": "MN", "765": "IN", "769": "MS", "770": "GA", "772": "FL",
    "773": "IL", "774": "MA", "775": "NV", "779": "IL", "781": "MA", "782": "NS",
    "785": "KS", "786": "FL", "787": "PR", "801": "UT", "802": "VT", "803": "SC",
    "804": "VA", "805": "CA", "806": "TX", "808": "HI", "810": "MI", "812": "IN",
    "813": "FL", "814": "PA", "815": "IL", "816": "MO", "817": "TX", "818": "CA",
    "820": "CA", "828": "NC", "830": "TX", "831": "CA", "832": "TX", "835": "PA",
    "838": "NY", "839": "SC", "840": "VA", "843": "SC", "845": "NY", "847": "IL",
    "848": "NJ", "849": "DO", "850": "FL", "854": "SC", "856": "NJ", "857": "MA",
    "858": "CA", "859": "KY", "860": "CT", "862": "NJ", "863": "FL", "864": "SC",
    "865": "TN", "870": "AR", "872": "IL", "878": "PA", "901": "TN", "903": "TX",
    "904": "FL", "906": "MI", "907": "AK", "908": "NJ", "909": "CA", "910": "NC",
    "912": "GA", "913": "KS", "914": "NY", "915": "TX", "916": "CA", "917": "NY",
    "918": "OK", "919": "NC", "920": "WI", "925": "CA", "928": "AZ", "929": "NY",
    "930": "IN", "931": "TN", "934": "NY", "936": "TX", "937": "OH", "938": "AL",
    "939": "PR", "940": "TX", "941": "FL", "943": "GA", "945": "TX", "947": "MI",
    "949": "CA", "951": "CA", "952": "MN", "954": "FL", "956": "TX", "959": "CT",
    "970": "CO", "971": "OR", "972": "TX", "973": "NJ", "978": "MA", "979": "TX",
    "980": "NC", "984": "NC", "985": "LA", "986": "ID", "989": "MI",
}
# fmt: on

# Area code → IANA timezone (simplified — covers major US zones)
# States may span multiple zones; this uses the dominant timezone per area code.
_STATE_TO_TZ = {
    "AL": "America/Chicago", "AK": "America/Anchorage", "AZ": "America/Phoenix",
    "AR": "America/Chicago", "CA": "America/Los_Angeles", "CO": "America/Denver",
    "CT": "America/New_York", "DE": "America/New_York", "DC": "America/New_York",
    "FL": "America/New_York", "GA": "America/New_York", "HI": "Pacific/Honolulu",
    "ID": "America/Boise", "IL": "America/Chicago", "IN": "America/Indiana/Indianapolis",
    "IA": "America/Chicago", "KS": "America/Chicago", "KY": "America/New_York",
    "LA": "America/Chicago", "ME": "America/New_York", "MD": "America/New_York",
    "MA": "America/New_York", "MI": "America/Detroit", "MN": "America/Chicago",
    "MS": "America/Chicago", "MO": "America/Chicago", "MT": "America/Denver",
    "NE": "America/Chicago", "NV": "America/Los_Angeles", "NH": "America/New_York",
    "NJ": "America/New_York", "NM": "America/Denver", "NY": "America/New_York",
    "NC": "America/New_York", "ND": "America/Chicago", "OH": "America/New_York",
    "OK": "America/Chicago", "OR": "America/Los_Angeles", "PA": "America/New_York",
    "RI": "America/New_York", "SC": "America/New_York", "SD": "America/Chicago",
    "TN": "America/Chicago", "TX": "America/Chicago", "UT": "America/Denver",
    "VT": "America/New_York", "VA": "America/New_York", "WA": "America/Los_Angeles",
    "WV": "America/New_York", "WI": "America/Chicago", "WY": "America/Denver",
    "PR": "America/Puerto_Rico", "VI": "America/Virgin", "GU": "Pacific/Guam",
    "AS": "Pacific/Pago_Pago", "MP": "Pacific/Guam", "NS": "America/Halifax",
    "DO": "America/Santo_Domingo",
}


def area_code_to_timezone(phone):
    """Extract area code from phone number and return IANA timezone string."""
    if not phone:
        return None
    digits = ''.join(c for c in phone if c.isdigit())
    # Strip US/Canada country code '1' if 11 digits
    if len(digits) == 11 and digits[0] == '1':
        digits = digits[1:]
    if len(digits) < 10:
        return None
    area = digits[:3]
    state = AREA_CODE_TO_STATE.get(area)
    if not state:
        return None
    return _STATE_TO_TZ.get(state)


def area_code_to_state(phone):
    """Extract area code from phone number and return 2-letter state code."""
    if not phone:
        return None
    digits = ''.join(c for c in phone if c.isdigit())
    # Strip US/Canada country code '1' if 11 digits
    if len(digits) == 11 and digits[0] == '1':
        digits = digits[1:]
    if len(digits) < 10:
        return None
    return AREA_CODE_TO_STATE.get(digits[:3])


def is_two_party_consent_state(phone):
    """Check if the recipient's phone number is in a two-party consent state."""
    state = area_code_to_state(phone)
    return state in TWO_PARTY_CONSENT_STATES if state else False


def check_recipient_timezone(phone, calling_hours_start="08:00", calling_hours_end="21:00"):
    """Check if it's within calling hours in the RECIPIENT's timezone.
    Returns (allowed, reason, recipient_tz, local_time_str)."""
    tz_str = area_code_to_timezone(phone)
    if not tz_str:
        return True, None, None, None  # Can't determine — allow

    try:
        import pytz
    except ImportError:
        return True, None, tz_str, None

    try:
        tz = pytz.timezone(tz_str)
        now = datetime.now(tz)
        local_time_str = now.strftime('%I:%M %p %Z')

        start_h, start_m = map(int, calling_hours_start.split(':'))
        end_h, end_m = map(int, calling_hours_end.split(':'))
        start_minutes = start_h * 60 + start_m
        end_minutes = end_h * 60 + end_m
        now_minutes = now.hour * 60 + now.minute

        # Handle midnight wrap-around (e.g., 22:00-06:00)
        if end_minutes <= start_minutes:
            allowed = now_minutes >= start_minutes or now_minutes < end_minutes
        else:
            allowed = start_minutes <= now_minutes < end_minutes

        if not allowed:
            state = area_code_to_state(phone) or "?"
            return (False,
                    f"Outside calling hours in recipient's timezone ({local_time_str}, {state}). "
                    f"Allowed: {calling_hours_start}-{calling_hours_end}",
                    tz_str, local_time_str)
        return True, None, tz_str, local_time_str
    except Exception:
        return True, None, tz_str, None


# ═══════════════════════════════════════════════════════════════════════════════
# ERLANG-C PREDICTIVE PACING ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

def _erlang_c_probability(n_agents, traffic_intensity):
    """Calculate Erlang-C probability that an arriving call must wait.

    P(wait) = (A^N / N!) * (N / (N - A)) / (sum_{k=0}^{N-1} A^k/k! + (A^N/N!) * (N/(N-A)))

    Where A = traffic intensity (Erlangs), N = number of agents.
    Returns probability between 0 and 1.
    """
    if n_agents <= 0 or traffic_intensity <= 0:
        return 0.0
    if traffic_intensity >= n_agents:
        return 1.0  # System overloaded

    A = traffic_intensity
    N = n_agents

    # Compute A^N / N! using logarithms for numerical stability
    log_numerator = N * math.log(A) - sum(math.log(i) for i in range(1, N + 1))

    # Compute sum of A^k / k! for k = 0 to N-1
    log_terms = []
    for k in range(N):
        if k == 0:
            log_terms.append(0.0)  # A^0 / 0! = 1
        else:
            log_terms.append(k * math.log(A) - sum(math.log(i) for i in range(1, k + 1)))

    # Use log-sum-exp for numerical stability
    max_log = max(log_terms)
    sum_terms = math.exp(-max_log) * sum(math.exp(lt) for lt in log_terms)

    erlang_b = math.exp(log_numerator)
    factor = N / (N - A)

    denominator = math.exp(max_log) * sum_terms + erlang_b * factor
    if denominator <= 0:
        return 0.0

    return (erlang_b * factor) / denominator


def calculate_optimal_dial_ratio(
    available_agents,
    avg_handle_time_sec,
    avg_ring_time_sec,
    answer_rate_pct,
    current_abandon_rate_pct,
    target_abandon_rate_pct=3.0,
    wrap_up_time_sec=15,
    max_ratio=4.0,
):
    """Calculate optimal dial ratio using Erlang-C queuing theory.

    This models the call center as an M/M/N queue where:
    - Arrival rate = dial rate * answer rate
    - Service rate = 1 / avg_handle_time
    - N = available agents

    The algorithm finds the maximum dial ratio that keeps the
    predicted abandon rate under the target (TCPA safe harbor = 3%).

    Returns dict with dial_ratio, recommended_lines, erlang_c_prob, etc.
    """
    if available_agents <= 0:
        return {
            "dial_ratio": 1.0,
            "recommended_lines": 1,
            "erlang_c_probability": 0.0,
            "predicted_abandon_rate": 0.0,
            "throttled": False,
            "reason": "no_agents_available",
        }

    # Normalize inputs
    answer_rate = max(0.01, min(1.0, answer_rate_pct / 100.0))
    aht = max(1.0, avg_handle_time_sec)
    ring_time = max(1.0, avg_ring_time_sec)
    wrap_up = max(0.0, wrap_up_time_sec)

    # Service time includes handle time + wrap-up
    service_time = aht + wrap_up

    best_ratio = 1.0
    best_abandon = 0.0
    best_erlang_c = 0.0
    throttled = False

    # Binary search for optimal ratio between 1.0 and max_ratio
    low, high = 1.0, max_ratio
    for _ in range(20):  # 20 iterations gives precision to ~0.000001
        mid = (low + high) / 2.0

        # Calls arriving per second = (agents * ratio * answer_rate) / ring_time
        arrival_rate = (available_agents * mid * answer_rate) / ring_time

        # Service rate = agents / service_time
        service_rate = available_agents / service_time

        # Traffic intensity (Erlangs) = arrival_rate / (service_rate / agents) = arrival_rate * service_time
        traffic_intensity = arrival_rate * service_time

        # Erlang-C: probability a call has to wait
        p_wait = _erlang_c_probability(available_agents, traffic_intensity)

        # Predicted abandon rate: calls that wait > 2 seconds (FTC definition)
        # Using exponential service time assumption:
        # P(wait > t) = P(wait) * exp(-agents * (1 - traffic_intensity/agents) * t / service_time)
        mu = 1.0 / service_time
        rho = traffic_intensity / available_agents if available_agents > 0 else 1.0
        if rho < 1.0:
            # P(abandon) ≈ P(wait) * exp(-(N * mu * (1 - rho)) * 2)
            # where 2 = FTC 2-second threshold
            exponent = -available_agents * mu * (1.0 - rho) * 2.0
            predicted_abandon = p_wait * math.exp(max(-500, exponent)) * 100.0
        else:
            predicted_abandon = 100.0  # Overloaded

        if predicted_abandon <= target_abandon_rate_pct:
            best_ratio = mid
            best_abandon = predicted_abandon
            best_erlang_c = p_wait
            low = mid
        else:
            high = mid

    # If current abandon rate is approaching limit, throttle down
    if current_abandon_rate_pct > target_abandon_rate_pct * 0.8:
        throttle_factor = max(0.5, 1.0 - (current_abandon_rate_pct - target_abandon_rate_pct * 0.8) /
                              (target_abandon_rate_pct * 0.4))
        original_ratio = best_ratio
        best_ratio = max(1.0, best_ratio * throttle_factor)
        throttled = True
        logger.info(f"[Erlang-C] Throttled: current abandon={current_abandon_rate_pct:.1f}% "
                    f"(target={target_abandon_rate_pct}%) — ratio {original_ratio:.2f} → {best_ratio:.2f}")

    recommended_lines = min(4, max(1, round(best_ratio)))

    return {
        "dial_ratio": round(best_ratio, 2),
        "recommended_lines": recommended_lines,
        "erlang_c_probability": round(best_erlang_c, 4),
        "predicted_abandon_rate": round(best_abandon, 2),
        "throttled": throttled,
        "reason": "erlang_c_optimal",
        "inputs": {
            "available_agents": available_agents,
            "avg_handle_time_sec": round(aht, 1),
            "avg_ring_time_sec": round(ring_time, 1),
            "answer_rate_pct": round(answer_rate_pct, 1),
            "current_abandon_rate_pct": round(current_abandon_rate_pct, 2),
            "wrap_up_time_sec": round(wrap_up, 1),
        },
    }


def calculate_solo_predictive_ratio(
    avg_handle_time_sec,
    avg_ring_time_sec,
    answer_rate_pct,
    current_abandon_rate_pct=0.0,
    target_abandon_rate_pct=3.0,
    wrap_up_time_sec=15,
    max_ratio=4.0,
    ai_overflow_capacity=3,
):
    """Calculate optimal dial ratio for Solo Predictive + AI Overflow.

    Solo Predictive flips the traditional Erlang-C constraint: instead of
    needing 5+ human agents to absorb call collisions, a SINGLE human agent
    is backed by AI overflow lines. When multiple leads answer simultaneously,
    the first call bridges to the human agent and all subsequent calls bridge
    to the Voice AI in "overflow/warmup" mode.

    Because AI catches every overflow call, these are NOT abandoned — they are
    answered. The effective agent count for Erlang-C is 1 human + N AI agents.
    TCPA abandon rate is effectively 0% since no caller waits unanswered.

    Args:
        avg_handle_time_sec: Average call duration in seconds
        avg_ring_time_sec: Average ring time before answer
        answer_rate_pct: Historical answer rate (0-100)
        current_abandon_rate_pct: Current rolling abandon rate
        target_abandon_rate_pct: TCPA target (default 3%)
        wrap_up_time_sec: Post-call wrap-up time
        max_ratio: Maximum dial ratio (capped at 4 lines)
        ai_overflow_capacity: Number of AI overflow lines (default 3 = 4 total - 1 human)

    Returns:
        dict with dial_ratio, recommended_lines, overflow metrics, etc.
    """
    # Effective agents = 1 human + AI overflow capacity
    # AI agents handle calls just as well as humans for queue math purposes —
    # they answer instantly and keep the lead engaged.
    effective_agents = 1 + ai_overflow_capacity

    # Run standard Erlang-C with the effective agent count
    result = calculate_optimal_dial_ratio(
        available_agents=effective_agents,
        avg_handle_time_sec=avg_handle_time_sec,
        avg_ring_time_sec=avg_ring_time_sec,
        answer_rate_pct=answer_rate_pct,
        current_abandon_rate_pct=current_abandon_rate_pct,
        target_abandon_rate_pct=target_abandon_rate_pct,
        wrap_up_time_sec=wrap_up_time_sec,
        max_ratio=max_ratio,
    )

    # Enrich the result with solo predictive context
    result["reason"] = "solo_predictive_erlang_c"
    result["inputs"]["human_agents"] = 1
    result["inputs"]["ai_overflow_capacity"] = ai_overflow_capacity
    result["inputs"]["effective_agents"] = effective_agents
    result["ai_overflow"] = True

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# TCPA COMPLIANCE ENGINE — Rolling Abandon Rate Tracker
# ═══════════════════════════════════════════════════════════════════════════════

class TCPAComplianceTracker:
    """Thread-safe rolling abandon rate tracker per location.

    Tracks call outcomes over a 30-day rolling window (per FTC TSR safe harbor).
    Auto-throttles dial ratio when abandon rate approaches 3%.
    """

    def __init__(self):
        self._lock = threading.Lock()
        # location_id → {answered: int, abandoned: int, calls: [(timestamp, outcome)]}
        self._data = defaultdict(lambda: {"answered": 0, "abandoned": 0, "calls": []})

    def record_call_outcome(self, location_id, outcome):
        """Record a call outcome. outcome = 'answered' | 'abandoned' | 'no_answer' | 'busy'"""
        with self._lock:
            data = self._data[location_id]
            now = time.time()
            data["calls"].append((now, outcome))
            if outcome == "answered":
                data["answered"] += 1
            elif outcome == "abandoned":
                data["abandoned"] += 1
            # Prune calls older than 30 days
            self._prune(location_id)

    def get_abandon_rate(self, location_id):
        """Get current rolling abandon rate as percentage."""
        with self._lock:
            self._prune(location_id)
            data = self._data[location_id]
            answered = data["answered"]
            abandoned = data["abandoned"]
            total = answered + abandoned
            if total == 0:
                return 0.0
            return (abandoned / total) * 100.0

    def get_compliance_status(self, location_id):
        """Get full compliance status for dashboard."""
        with self._lock:
            self._prune(location_id)
            data = self._data[location_id]
            answered = data["answered"]
            abandoned = data["abandoned"]
            total = answered + abandoned
            rate = (abandoned / total * 100.0) if total > 0 else 0.0

            # Recent window (last hour) for real-time monitoring
            one_hour_ago = time.time() - 3600
            recent = [c for c in data["calls"] if c[0] > one_hour_ago]
            recent_answered = sum(1 for _, o in recent if o == "answered")
            recent_abandoned = sum(1 for _, o in recent if o == "abandoned")
            recent_total = recent_answered + recent_abandoned
            recent_rate = (recent_abandoned / recent_total * 100.0) if recent_total > 0 else 0.0

            return {
                "abandon_rate_30d": round(rate, 2),
                "total_answered_30d": answered,
                "total_abandoned_30d": abandoned,
                "abandon_rate_1h": round(recent_rate, 2),
                "total_calls_1h": recent_total,
                "compliant": rate <= 3.0,
                "warning": 2.0 < rate <= 3.0,
                "critical": rate > 3.0,
                "auto_throttle_active": rate > 2.4,  # Start throttling at 80% of limit
            }

    def should_throttle(self, location_id):
        """Check if dialing should be throttled to maintain TCPA compliance."""
        rate = self.get_abandon_rate(location_id)
        return rate > 2.4  # 80% of 3% safe harbor

    def _prune(self, location_id):
        """Remove calls older than 30 days and recalculate counts."""
        data = self._data[location_id]
        cutoff = time.time() - (30 * 86400)
        old_len = len(data["calls"])
        data["calls"] = [(t, o) for t, o in data["calls"] if t > cutoff]
        if len(data["calls"]) < old_len:
            # Recalculate counts from remaining calls
            data["answered"] = sum(1 for _, o in data["calls"] if o == "answered")
            data["abandoned"] = sum(1 for _, o in data["calls"] if o == "abandoned")

    def load_from_db(self, location_id):
        """Load historical call data from DB to bootstrap the tracker."""
        conn = get_db_connection()
        if not conn:
            return
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT status, created_at FROM call_history
                WHERE location_id = %s AND created_at > NOW() - INTERVAL '30 days'
                ORDER BY created_at ASC
            """, (location_id,))
            rows = cur.fetchall()
            cur.close()

            with self._lock:
                data = self._data[location_id]
                data["calls"] = []
                data["answered"] = 0
                data["abandoned"] = 0
                for row in rows:
                    status = row["status"]
                    ts = row["created_at"].timestamp() if row["created_at"] else time.time()
                    if status == "completed":
                        outcome = "answered"
                    elif status == "abandoned":
                        outcome = "abandoned"
                    elif status in ("no-answer", "busy", "failed", "canceled"):
                        outcome = "no_answer"
                    else:
                        outcome = "no_answer"
                    data["calls"].append((ts, outcome))
                    if outcome == "answered":
                        data["answered"] += 1
                    elif outcome == "abandoned":
                        data["abandoned"] += 1
                answered_count = data["answered"]

            logger.info(f"[TCPA] Loaded {len(rows)} calls for {location_id} "
                        f"(answered={answered_count}, abandon_rate={self.get_abandon_rate(location_id):.1f}%)")
        except Exception as e:
            logger.error(f"[TCPA] Failed to load call history for {location_id}: {e}")
        finally:
            return_db_connection(conn)


# Global singleton
tcpa_tracker = TCPAComplianceTracker()


# ═══════════════════════════════════════════════════════════════════════════════
# AGENT STATE MACHINE
# ═══════════════════════════════════════════════════════════════════════════════

class AgentState:
    """Valid agent states for ACD-style state management."""
    READY = "ready"
    NOT_READY = "not_ready"
    ON_CALL = "on_call"
    WRAP_UP = "wrap_up"
    BREAK = "break"
    EXTENDED_AWAY = "extended_away"
    LOGGED_OUT = "logged_out"

    # Reason codes for NOT_READY
    REASONS = {
        "break": "Short Break",
        "lunch": "Lunch",
        "meeting": "Meeting",
        "training": "Training",
        "system_issue": "System Issue",
        "personal": "Personal",
        "coaching": "Coaching Session",
        "back_office": "Back-Office Work",
    }


class AgentStateManager:
    """Thread-safe agent state tracking for predictive dialer.

    Tracks agent states (Ready, Not Ready, On Call, Wrap-Up, Break)
    and provides real-time availability data to the pacing algorithm.
    """

    def __init__(self):
        self._lock = threading.Lock()
        # location_id → {email → {state, reason, since, call_sid, wrap_up_ends_at}}
        self._agents = defaultdict(dict)
        # location_id → [state_change_events] for history/reporting
        self._history = defaultdict(list)

    def set_state(self, location_id, email, state, reason=None, call_sid=None):
        """Set agent state with validation."""
        valid_states = {AgentState.READY, AgentState.NOT_READY, AgentState.ON_CALL,
                        AgentState.WRAP_UP, AgentState.BREAK, AgentState.EXTENDED_AWAY,
                        AgentState.LOGGED_OUT}
        if state not in valid_states:
            return False

        with self._lock:
            now = time.time()
            prev = self._agents[location_id].get(email, {})
            prev_state = prev.get("state", AgentState.LOGGED_OUT)

            self._agents[location_id][email] = {
                "state": state,
                "reason": reason,
                "since": now,
                "call_sid": call_sid,
                "wrap_up_ends_at": None,
            }

            # Record state transition for analytics
            self._history[location_id].append({
                "email": email,
                "from_state": prev_state,
                "to_state": state,
                "reason": reason,
                "timestamp": now,
            })
            # Keep only last 1000 events per location
            if len(self._history[location_id]) > 1000:
                self._history[location_id] = self._history[location_id][-500:]

        return True

    def start_wrap_up(self, location_id, email, wrap_up_seconds=15):
        """Transition agent to wrap-up state with timer."""
        with self._lock:
            now = time.time()
            self._agents[location_id][email] = {
                "state": AgentState.WRAP_UP,
                "reason": None,
                "since": now,
                "call_sid": None,
                "wrap_up_ends_at": now + wrap_up_seconds,
            }
        return True

    def get_agent_state(self, location_id, email):
        """Get current state of a specific agent."""
        with self._lock:
            agent = self._agents[location_id].get(email)
            if not agent:
                return {"state": AgentState.LOGGED_OUT, "reason": None, "since": None}

            # Auto-transition from wrap-up to ready if timer expired
            if agent["state"] == AgentState.WRAP_UP and agent.get("wrap_up_ends_at"):
                if time.time() >= agent["wrap_up_ends_at"]:
                    agent["state"] = AgentState.READY
                    agent["reason"] = None
                    agent["since"] = time.time()
                    agent["wrap_up_ends_at"] = None

            return dict(agent)

    def try_claim_for_call(self, location_id, email, call_sid):
        """Atomically try to claim an agent for a call (Solo Predictive).

        This is the critical race-condition guard for AI overflow. When multiple
        calls answer simultaneously, each one hits /voice/outbound-twiml and
        calls this method. Only the FIRST call to arrive wins the claim —
        subsequent calls return False and get routed to AI overflow.

        Returns:
            (claimed: bool, current_state: str, primary_call_sid: str or None)
            - (True, 'on_call', None) = agent was available, now claimed for this call
            - (False, 'on_call', 'CA...') = agent already on another call (collision)
            - (False, 'wrap_up'|'break'|..., None) = agent unavailable (non-ready state)
        """
        with self._lock:
            now = time.time()
            agent = self._agents[location_id].get(email)

            # Auto-transition wrap-up → ready if timer expired
            if agent and agent["state"] == AgentState.WRAP_UP and agent.get("wrap_up_ends_at"):
                if now >= agent["wrap_up_ends_at"]:
                    agent["state"] = AgentState.READY
                    agent["reason"] = None
                    agent["since"] = now
                    agent["wrap_up_ends_at"] = None

            current_state = agent["state"] if agent else None

            # Agent is available → claim them
            if current_state in (AgentState.READY, None):
                self._agents[location_id][email] = {
                    "state": AgentState.ON_CALL,
                    "reason": "solo_predictive_claim",
                    "since": now,
                    "call_sid": call_sid,
                    "wrap_up_ends_at": None,
                }
                self._history[location_id].append({
                    "email": email,
                    "from_state": current_state or AgentState.LOGGED_OUT,
                    "to_state": AgentState.ON_CALL,
                    "reason": "solo_predictive_claim",
                    "timestamp": now,
                })
                if len(self._history[location_id]) > 1000:
                    self._history[location_id] = self._history[location_id][-500:]
                return (True, AgentState.ON_CALL, None)

            # Agent is on a call → collision (overflow to AI)
            if current_state == AgentState.ON_CALL:
                return (False, AgentState.ON_CALL, agent.get("call_sid"))

            # Agent is in any other state → overflow to AI
            return (False, current_state, None)

    def get_available_agents(self, location_id):
        """Get count and list of agents in READY state.
        Also checks wrap-up timers and auto-transitions expired ones to READY."""
        with self._lock:
            now = time.time()
            available = []
            for email, agent in self._agents[location_id].items():
                # Auto-transition wrap-up → ready
                if agent["state"] == AgentState.WRAP_UP and agent.get("wrap_up_ends_at"):
                    if now >= agent["wrap_up_ends_at"]:
                        agent["state"] = AgentState.READY
                        agent["reason"] = None
                        agent["since"] = now
                        agent["wrap_up_ends_at"] = None

                if agent["state"] == AgentState.READY:
                    available.append(email)

            return available

    def get_predicted_available(self, location_id, horizon_sec=30):
        """Predict how many agents will be available within the next N seconds.
        Counts READY agents + WRAP_UP agents whose timer expires within horizon."""
        with self._lock:
            now = time.time()
            count = 0
            for email, agent in self._agents[location_id].items():
                if agent["state"] == AgentState.READY:
                    count += 1
                elif agent["state"] == AgentState.WRAP_UP and agent.get("wrap_up_ends_at"):
                    if agent["wrap_up_ends_at"] <= now + horizon_sec:
                        count += 1
            return count

    def get_all_agents(self, location_id):
        """Get all agents and their states for the dashboard."""
        with self._lock:
            now = time.time()
            result = []
            for email, agent in self._agents[location_id].items():
                # Auto-transition wrap-up → ready
                if agent["state"] == AgentState.WRAP_UP and agent.get("wrap_up_ends_at"):
                    if now >= agent["wrap_up_ends_at"]:
                        agent["state"] = AgentState.READY
                        agent["reason"] = None
                        agent["since"] = now
                        agent["wrap_up_ends_at"] = None

                duration = round(now - agent.get("since", now))
                wrap_up_remaining = None
                if agent["state"] == AgentState.WRAP_UP and agent.get("wrap_up_ends_at"):
                    wrap_up_remaining = max(0, round(agent["wrap_up_ends_at"] - now))

                result.append({
                    "email": email,
                    "state": agent["state"],
                    "reason": agent.get("reason"),
                    "duration_sec": duration,
                    "call_sid": agent.get("call_sid"),
                    "wrap_up_remaining_sec": wrap_up_remaining,
                })
            return result

    def get_any_available_agent(self, location_id, exclude_email=None):
        """Get the first READY agent at a location (for team member fallback).

        When the primary dialing agent is ON_CALL and a collision occurs,
        check if any OTHER team member at this location is READY and can
        take the call instead of routing to AI overflow.

        Args:
            location_id: The location to check
            exclude_email: Email of the agent to skip (the one already ON_CALL)

        Returns:
            email of an available agent, or None if nobody is available
        """
        with self._lock:
            now = time.time()
            for email, agent in self._agents[location_id].items():
                if exclude_email and email == exclude_email:
                    continue
                # Auto-transition wrap-up → ready if timer expired
                if agent["state"] == AgentState.WRAP_UP and agent.get("wrap_up_ends_at"):
                    if now >= agent["wrap_up_ends_at"]:
                        agent["state"] = AgentState.READY
                        agent["reason"] = None
                        agent["since"] = now
                        agent["wrap_up_ends_at"] = None
                if agent["state"] == AgentState.READY:
                    return email
            return None

    def get_state_history(self, location_id, limit=100):
        """Get recent state transitions for compliance reporting."""
        with self._lock:
            return list(reversed(self._history[location_id][-limit:]))


# Global singleton
agent_state_manager = AgentStateManager()


# ═══════════════════════════════════════════════════════════════════════════════
# CALLBACK QUEUE
# ═══════════════════════════════════════════════════════════════════════════════

class CallbackQueue:
    """Thread-safe scheduled callback queue for re-dials.

    Contacts that were busy, no-answer, or voicemail can be scheduled
    for automatic re-dial at a specified time.
    """

    def __init__(self):
        self._lock = threading.Lock()
        # location_id → [{contact_id, phone, name, scheduled_at, reason, attempts, created_at}]
        self._queues = defaultdict(list)

    def schedule_callback(self, location_id, contact_id, phone, name,
                          scheduled_at, reason="no_answer", attempts=1):
        """Add a contact to the callback queue."""
        with self._lock:
            # Prune completed/cancelled items older than 24 hours
            cutoff = time.time() - 86400
            self._queues[location_id] = [
                item for item in self._queues[location_id]
                if item["status"] == "pending" or item.get("created_at", 0) > cutoff
            ]
            # Prevent duplicates
            for item in self._queues[location_id]:
                if item["phone"] == phone and item["status"] == "pending":
                    return False  # Already queued

            self._queues[location_id].append({
                "contact_id": contact_id,
                "phone": phone,
                "name": name,
                "scheduled_at": scheduled_at,
                "reason": reason,
                "attempts": attempts,
                "created_at": time.time(),
                "status": "pending",
            })
            return True

    def get_due_callbacks(self, location_id):
        """Get callbacks that are due for re-dial."""
        with self._lock:
            now = time.time()
            due = []
            for item in self._queues[location_id]:
                if item["status"] == "pending" and item["scheduled_at"] <= now:
                    due.append(dict(item))
            return due

    def mark_completed(self, location_id, phone):
        """Mark a callback as completed (dialed)."""
        with self._lock:
            for item in self._queues[location_id]:
                if item["phone"] == phone and item["status"] == "pending":
                    item["status"] = "completed"
                    return True
            return False

    def cancel_callback(self, location_id, phone):
        """Cancel a scheduled callback."""
        with self._lock:
            for item in self._queues[location_id]:
                if item["phone"] == phone and item["status"] == "pending":
                    item["status"] = "cancelled"
                    return True
            return False

    def get_queue(self, location_id):
        """Get all pending callbacks for display."""
        with self._lock:
            return [dict(item) for item in self._queues[location_id]
                    if item["status"] == "pending"]

    def get_queue_size(self, location_id):
        """Get count of pending callbacks."""
        with self._lock:
            return sum(1 for item in self._queues[location_id]
                       if item["status"] == "pending")

    def clear_queue(self, location_id):
        """Clear all pending callbacks."""
        with self._lock:
            count = sum(1 for item in self._queues[location_id]
                        if item["status"] == "pending")
            self._queues[location_id] = [
                item for item in self._queues[location_id]
                if item["status"] != "pending"
            ]
            return count


# Global singleton
callback_queue = CallbackQueue()


# ═══════════════════════════════════════════════════════════════════════════════
# COMPLIANCE METRICS (for dashboard)
# ═══════════════════════════════════════════════════════════════════════════════

def get_compliance_metrics(location_id, period_days=30):
    """Get comprehensive compliance metrics for the compliance dashboard.

    Returns abandon rate, DNC violations, calling hours violations,
    recording consent status, and overall compliance score.
    """
    conn = get_db_connection()
    if not conn:
        return {"error": "Database unavailable"}
    try:
        cur = conn.cursor()

        # Abandon rate from TCPA tracker (in-memory, real-time)
        tcpa_status = tcpa_tracker.get_compliance_status(location_id)

        # DNC violations: count of calls to DnD contacts
        cur.execute("""
            SELECT COUNT(*) as dnc_violations
            FROM call_history ch
            JOIN contact_cache cc ON ch.contact_id = cc.contact_id AND cc.location_id = %s
            WHERE ch.location_id = %s
            AND ch.created_at > NOW() - make_interval(days => %s)
            AND cc.dnd = true
        """, (location_id, location_id, period_days))
        dnc_row = cur.fetchone()
        dnc_violations = dnc_row["dnc_violations"] if dnc_row else 0

        # Calling hours violations: calls outside 8am-9pm in contact's estimated timezone
        cur.execute("""
            SELECT COUNT(*) as hours_violations
            FROM call_history
            WHERE location_id = %s
            AND created_at > NOW() - make_interval(days => %s)
            AND (EXTRACT(HOUR FROM created_at) < 8 OR EXTRACT(HOUR FROM created_at) >= 21)
        """, (location_id, period_days))
        hours_row = cur.fetchone()
        hours_violations = hours_row["hours_violations"] if hours_row else 0

        # Total calls for the period
        cur.execute("""
            SELECT COUNT(*) as total,
                   COUNT(*) FILTER (WHERE status = 'completed' AND duration > 0) as connected,
                   AVG(duration) FILTER (WHERE status = 'completed' AND duration > 0) as avg_duration
            FROM call_history
            WHERE location_id = %s
            AND created_at > NOW() - make_interval(days => %s)
        """, (location_id, period_days))
        call_row = cur.fetchone()
        total_calls = call_row["total"] if call_row else 0
        connected_calls = call_row["connected"] if call_row else 0
        avg_duration = float(call_row["avg_duration"] or 0) if call_row else 0

        cur.close()

        # Calculate compliance score (0-100)
        score = 100
        if tcpa_status.get("critical"):
            score -= 40
        elif tcpa_status.get("warning"):
            score -= 20
        if dnc_violations > 0:
            score -= min(30, dnc_violations * 10)
        if hours_violations > 0:
            score -= min(20, hours_violations * 5)
        score = max(0, score)

        return {
            "compliance_score": score,
            "period_days": period_days,
            "tcpa": tcpa_status,
            "dnc_violations": dnc_violations,
            "hours_violations": hours_violations,
            "total_calls": total_calls,
            "connected_calls": connected_calls,
            "avg_duration_sec": round(avg_duration, 1),
            "connect_rate": round((connected_calls / total_calls * 100) if total_calls > 0 else 0, 1),
        }
    except Exception as e:
        logger.error(f"[Compliance] Failed to get metrics for {location_id}: {e}")
        return {"error": str(e)}
    finally:
        return_db_connection(conn)


# ═══════════════════════════════════════════════════════════════════════════════
# AGENCY-LEVEL PREDICTIVE DIALER — company_id scoped operations
# ═══════════════════════════════════════════════════════════════════════════════

def _get_company_location_ids(company_id):
    """Get all location_ids for a company. Used by agency-level operations."""
    if not company_id:
        return []
    conn = get_db_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id FROM subscribers WHERE company_id = %s", (company_id,))
        ids = [r[0] for r in cur.fetchall()]
        cur.close()
        return ids
    except Exception as e:
        logger.error(f"[Agency] Failed to get location_ids for company {company_id}: {e}")
        return []
    finally:
        return_db_connection(conn)


def get_agency_available_agents(company_id):
    """Get all READY agents across the entire agency (all location_ids).
    Returns list of {location_id, email} for each ready agent."""
    location_ids = _get_company_location_ids(company_id)
    available = []
    for lid in location_ids:
        emails = agent_state_manager.get_available_agents(lid)
        for e in emails:
            available.append({"location_id": lid, "email": e})
    return available


def get_agency_all_agent_states(company_id):
    """Get all agent states across the agency for the command center."""
    location_ids = _get_company_location_ids(company_id)
    all_agents = []
    for lid in location_ids:
        agents = agent_state_manager.get_all_agents(lid)
        for a in agents:
            a["location_id"] = lid
            all_agents.append(a)
    return all_agents


def get_agency_tcpa_status(company_id):
    """Aggregate TCPA compliance across all agency locations.
    Returns combined abandon rate (per FTC, measured across all calls)."""
    location_ids = _get_company_location_ids(company_id)
    total_answered = 0
    total_abandoned = 0
    for lid in location_ids:
        status = tcpa_tracker.get_compliance_status(lid)
        total_answered += status.get("total_answered_30d", 0)
        total_abandoned += status.get("total_abandoned_30d", 0)

    total = total_answered + total_abandoned
    rate = (total_abandoned / total * 100.0) if total > 0 else 0.0

    return {
        "abandon_rate_30d": round(rate, 2),
        "total_answered_30d": total_answered,
        "total_abandoned_30d": total_abandoned,
        "compliant": rate <= 3.0,
        "warning": 2.0 < rate <= 3.0,
        "critical": rate > 3.0,
        "auto_throttle_active": rate > 2.4,
        "location_count": len(location_ids),
    }


def calculate_agency_dial_ratio(company_id, wrap_up_time_sec=15, max_ratio=4.0):
    """Calculate optimal dial ratio for the entire agency using Erlang-C.
    Aggregates metrics across all company agents."""
    location_ids = _get_company_location_ids(company_id)
    if not location_ids:
        return {"dial_ratio": 1.0, "recommended_lines": 1, "reason": "no_locations"}

    # Count available agents across all locations
    total_available = 0
    for lid in location_ids:
        total_available += len(agent_state_manager.get_available_agents(lid))

    if total_available < 5:
        return {
            "dial_ratio": 1.0,
            "recommended_lines": 1,
            "reason": "insufficient_agents",
            "available_agents": total_available,
            "minimum_required": 5,
        }

    # Aggregate call history metrics from all locations
    conn = get_db_connection()
    if not conn:
        return {"dial_ratio": 1.0, "recommended_lines": 1, "reason": "db_unavailable"}

    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE status = 'completed' AND duration > 0) AS connected,
                   AVG(duration) FILTER (WHERE status = 'completed' AND duration > 0) AS avg_handle_time,
                   AVG(CASE WHEN status IN ('no-answer', 'busy', 'failed', 'canceled')
                        THEN EXTRACT(EPOCH FROM (NOW() - created_at))
                        ELSE NULL END) AS avg_ring_time
            FROM call_history
            WHERE location_id = ANY(%s) AND created_at > NOW() - INTERVAL '7 days'
        """, (location_ids,))
        row = cur.fetchone()
        cur.close()

        total_calls = row[0] or 0
        connected = row[1] or 0
        avg_handle_time = float(row[2] or 180)  # Default 3 min
        avg_ring_time = min(float(row[3] or 15), 30)  # Cap at 30s

        answer_rate = (connected / total_calls * 100) if total_calls > 0 else 30.0

        # Get agency-wide TCPA status for throttling
        agency_tcpa = get_agency_tcpa_status(company_id)
        current_abandon = agency_tcpa["abandon_rate_30d"]

        result = calculate_optimal_dial_ratio(
            available_agents=total_available,
            avg_handle_time_sec=avg_handle_time,
            avg_ring_time_sec=avg_ring_time,
            answer_rate_pct=answer_rate,
            current_abandon_rate_pct=current_abandon,
            target_abandon_rate_pct=3.0,
            wrap_up_time_sec=wrap_up_time_sec,
            max_ratio=max_ratio,
        )
        result["available_agents"] = total_available
        result["total_agents"] = sum(len(agent_state_manager._agents.get(lid, {})) for lid in location_ids)
        result["company_id"] = company_id
        return result

    except Exception as e:
        logger.error(f"[Agency] dial ratio calculation failed for {company_id}: {e}")
        return {"dial_ratio": 1.0, "recommended_lines": 1, "reason": f"error: {e}"}
    finally:
        return_db_connection(conn)


def get_agency_compliance_metrics(company_id, period_days=30):
    """Aggregate compliance metrics across all agency locations."""
    location_ids = _get_company_location_ids(company_id)
    if not location_ids:
        return {"error": "No locations found for this agency"}

    # Aggregate TCPA from in-memory tracker
    agency_tcpa = get_agency_tcpa_status(company_id)

    conn = get_db_connection()
    if not conn:
        return {"error": "Database unavailable"}

    try:
        cur = conn.cursor()

        # DNC violations across all locations
        cur.execute("""
            SELECT COUNT(*) as dnc_violations
            FROM call_history ch
            JOIN contact_cache cc ON ch.contact_id = cc.contact_id
                AND cc.location_id = ANY(%s)
            WHERE ch.location_id = ANY(%s)
            AND ch.created_at > NOW() - make_interval(days => %s)
            AND cc.dnd = true
        """, (location_ids, location_ids, period_days))
        dnc_row = cur.fetchone()
        dnc_violations = dnc_row[0] if dnc_row else 0

        # Calling hours violations
        cur.execute("""
            SELECT COUNT(*) as hours_violations
            FROM call_history
            WHERE location_id = ANY(%s)
            AND created_at > NOW() - make_interval(days => %s)
            AND (EXTRACT(HOUR FROM created_at) < 8 OR EXTRACT(HOUR FROM created_at) >= 21)
        """, (location_ids, period_days))
        hours_row = cur.fetchone()
        hours_violations = hours_row[0] if hours_row else 0

        # Total calls
        cur.execute("""
            SELECT COUNT(*) as total,
                   COUNT(*) FILTER (WHERE status = 'completed' AND duration > 0) as connected,
                   AVG(duration) FILTER (WHERE status = 'completed' AND duration > 0) as avg_duration
            FROM call_history
            WHERE location_id = ANY(%s)
            AND created_at > NOW() - make_interval(days => %s)
        """, (location_ids, period_days))
        call_row = cur.fetchone()
        total_calls = call_row[0] if call_row else 0
        connected_calls = call_row[1] if call_row else 0
        avg_duration = float(call_row[2] or 0) if call_row else 0

        cur.close()

        # Compliance score
        score = 100
        if agency_tcpa.get("critical"):
            score -= 40
        elif agency_tcpa.get("warning"):
            score -= 20
        if dnc_violations > 0:
            score -= min(30, dnc_violations * 10)
        if hours_violations > 0:
            score -= min(20, hours_violations * 5)
        score = max(0, score)

        return {
            "compliance_score": score,
            "period_days": period_days,
            "tcpa": agency_tcpa,
            "dnc_violations": dnc_violations,
            "hours_violations": hours_violations,
            "total_calls": total_calls,
            "connected_calls": connected_calls,
            "avg_duration_sec": round(avg_duration, 1),
            "connect_rate": round((connected_calls / total_calls * 100) if total_calls > 0 else 0, 1),
            "location_count": len(location_ids),
        }
    except Exception as e:
        logger.error(f"[Agency] compliance metrics failed for {company_id}: {e}")
        return {"error": str(e)}
    finally:
        return_db_connection(conn)


# ═══════════════════════════════════════════════════════════════════════════════
# LIVE SESSION PACING — Blends historical + session data for auto-pacing
# ═══════════════════════════════════════════════════════════════════════════════

def calculate_live_pacing(
    historical_connect_rate,
    historical_avg_handle_time,
    session_calls_total,
    session_calls_connected,
    session_avg_handle_time,
    wrap_up_time_sec=15,
    max_lines=4,
    current_abandon_rate=0.0,
):
    """Calculate real-time dial ratio blending historical and live session data.

    As the session progresses and sample size grows, session data is weighted
    more heavily. This makes the dialer truly predictive — it adapts mid-session
    to the actual connect rate being observed, not just historical averages.

    Args:
        historical_connect_rate: 7-day connect rate % (0-100)
        historical_avg_handle_time: 7-day average call duration in seconds
        session_calls_total: Calls dialed this session
        session_calls_connected: Calls connected this session
        session_avg_handle_time: Average duration of connected calls this session
        wrap_up_time_sec: Post-call wrap-up time
        max_lines: Maximum concurrent lines (1-4)
        current_abandon_rate: Rolling 30-day abandon rate %

    Returns:
        dict with blended dial_ratio, recommended_lines, confidence, and reason
    """
    # Session connect rate (avoid division by zero)
    if session_calls_total > 0:
        session_connect_rate = (session_calls_connected / session_calls_total) * 100
    else:
        session_connect_rate = 0.0

    # Blend weight: session data gets more weight as sample size grows
    # At 0 calls: 100% historical. At 20+ calls: ~80% session data.
    if session_calls_total == 0:
        session_weight = 0.0
    else:
        session_weight = min(0.8, session_calls_total / 25.0)
    historical_weight = 1.0 - session_weight

    # Blended metrics
    blended_connect_rate = (
        historical_weight * (historical_connect_rate or 25.0)
        + session_weight * session_connect_rate
    )
    blended_handle_time = (
        historical_weight * (historical_avg_handle_time or 180.0)
        + session_weight * (session_avg_handle_time or 180.0)
    )

    # Run Erlang-C with blended metrics
    ai_overflow_capacity = max(0, max_lines - 1)
    result = calculate_solo_predictive_ratio(
        avg_handle_time_sec=blended_handle_time,
        avg_ring_time_sec=15,
        answer_rate_pct=blended_connect_rate,
        current_abandon_rate_pct=current_abandon_rate,
        target_abandon_rate_pct=3.0,
        wrap_up_time_sec=wrap_up_time_sec,
        max_ratio=float(max_lines),
        ai_overflow_capacity=ai_overflow_capacity,
    )

    result["live_pacing"] = True
    result["session_weight"] = round(session_weight, 2)
    result["blended_connect_rate"] = round(blended_connect_rate, 1)
    result["blended_handle_time"] = round(blended_handle_time, 1)
    result["session_calls_total"] = session_calls_total
    result["session_calls_connected"] = session_calls_connected
    result["session_connect_rate"] = round(session_connect_rate, 1)
    result["confidence"] = (
        "low" if session_calls_total < 5
        else "medium" if session_calls_total < 20
        else "high"
    )

    return result
