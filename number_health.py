# number_health.py — Smart Number Rotation & Health Engine
# Tracks per-number call metrics, health scores, and intelligently selects
# outbound caller IDs to maximize connection rates and prevent number burnout.
#
# Architecture:
#   - number_health table: per-number daily metrics (calls, connects, duration, status)
#   - select_outbound_number(): replaces hardcoded from_number selection everywhere
#   - update_number_health(): called from /voice/status callback on every call outcome
#   - Warm-up engine: new numbers ramp gradually (5 → 10 → 20 → 40 → unlimited/day)
#   - Rest/freeze: burned numbers auto-rest, frozen numbers quarantined
#   - Cron jobs: daily metric reset, warm-up progression, rest/freeze expiry

import logging
import random
from datetime import datetime, timedelta
from db import get_db_connection, return_db_connection

logger = logging.getLogger("number_health")

# ── Constants ──────────────────────────────────────────────────────────────

# Health score thresholds
HEALTH_EXCELLENT = 80    # Green — full rotation eligibility
HEALTH_GOOD = 60         # Yellow-green — slightly deprioritized
HEALTH_WARNING = 40      # Orange — reduced volume
HEALTH_CRITICAL = 20     # Red — auto-rested
HEALTH_FROZEN = 0        # Black — quarantined, needs manual unfreeze

# Daily call caps by warm-up stage
WARMUP_STAGES = {
    0: {"daily_cap": 5,   "label": "Stage 0 — Seeding",    "days_required": 0},
    1: {"daily_cap": 10,  "label": "Stage 1 — Sprouting",  "days_required": 2},
    2: {"daily_cap": 20,  "label": "Stage 2 — Growing",    "days_required": 5},
    3: {"daily_cap": 40,  "label": "Stage 3 — Maturing",   "days_required": 10},
    4: {"daily_cap": 999, "label": "Stage 4 — Fully Warm", "days_required": 20},
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
MIN_CONNECT_RATE = 0.15  # 15%

# Max calls per day before auto-rest (safety valve, even for warm numbers)
ABSOLUTE_DAILY_CAP = 200


# ── Health Score Calculator ────────────────────────────────────────────────

def calculate_health_score(total_calls, connected_calls, no_answers, failed_calls,
                           avg_duration, warmup_stage, days_active):
    """
    Calculate a 0-100 health score for a phone number based on call metrics.

    Scoring breakdown:
      - Connection rate:  40 points (most important signal)
      - Call quality:     25 points (avg duration of connected calls)
      - Failure rate:     20 points (failed/busy ratio)
      - Maturity bonus:   15 points (warm-up stage + days active)
    """
    score = 0.0

    # 1. Connection rate (40 points)
    if total_calls > 0:
        connect_rate = connected_calls / total_calls
        if connect_rate >= 0.40:
            score += 40.0
        elif connect_rate >= 0.25:
            score += 30.0 + (connect_rate - 0.25) / 0.15 * 10.0
        elif connect_rate >= MIN_CONNECT_RATE:
            score += 15.0 + (connect_rate - MIN_CONNECT_RATE) / 0.10 * 15.0
        else:
            score += max(0, connect_rate / MIN_CONNECT_RATE * 15.0)
    else:
        # No calls yet — neutral score for this component
        score += 25.0

    # 2. Call quality — avg duration of connected calls (25 points)
    if avg_duration >= 120:   # 2+ min avg = excellent
        score += 25.0
    elif avg_duration >= 60:  # 1+ min = good
        score += 18.0 + (avg_duration - 60) / 60.0 * 7.0
    elif avg_duration >= 20:  # 20s+ = decent
        score += 8.0 + (avg_duration - 20) / 40.0 * 10.0
    elif avg_duration > 0:
        score += avg_duration / 20.0 * 8.0
    elif total_calls == 0:
        score += 15.0  # Neutral

    # 3. Failure rate (20 points) — lower is better
    if total_calls > 0:
        fail_rate = (failed_calls + no_answers) / total_calls
        if fail_rate <= 0.20:
            score += 20.0
        elif fail_rate <= 0.50:
            score += 20.0 - (fail_rate - 0.20) / 0.30 * 12.0
        elif fail_rate <= 0.80:
            score += 8.0 - (fail_rate - 0.50) / 0.30 * 8.0
        # else: 0 points (80%+ failure rate)
    else:
        score += 12.0  # Neutral

    # 4. Maturity bonus (15 points)
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
    3. If local_presence enabled and area code matches, prefer that subset
    4. Weight remaining numbers by health score (higher health = more likely selected)
    5. Return the selected number

    Returns:
        dict: {"phone": "+1...", "reason": "...", "health_score": N}
        or None if no numbers available
    """
    rotation_config = voice_config.get("number_rotation", {})
    if not rotation_config.get("enabled", False):
        # Rotation disabled — use existing logic (primary + local presence)
        return None

    primary = voice_config.get("twilio_phone_number", "")
    local_pool = voice_config.get("local_presence_numbers", [])
    all_numbers = list(set([primary] + local_pool)) if primary else list(set(local_pool))

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
            "health_score": h.get("health_score", 50.0),
            "daily_calls": daily_calls,
            "daily_cap": daily_cap,
            "status": status,
            "warmup_stage": warmup_stage,
        })

    if not candidates:
        # All numbers at cap or frozen — fall back to primary
        return {"phone": primary, "reason": "fallback_all_exhausted", "health_score": 0}

    # Local presence filter: if enabled and we have a matching area code, prefer it
    local_presence_enabled = voice_config.get("local_presence", False)
    if local_presence_enabled and dest_phone:
        dest_area = dest_phone.lstrip("+").lstrip("1")[:3]
        local_matches = [c for c in candidates
                         if c["phone"].lstrip("+").lstrip("1")[:3] == dest_area]
        if local_matches:
            candidates = local_matches

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

    return {
        "phone": selected["phone"],
        "reason": reason,
        "health_score": selected["health_score"],
        "daily_calls": selected["daily_calls"],
        "daily_cap": selected["daily_cap"],
        "warmup_stage": selected.get("warmup_stage", 4),
    }


# ── Health Update (called after every call) ─────────────────────────────────

def update_number_health(location_id, phone, call_status, duration=0):
    """
    Update health metrics for a phone number after a call completes.

    Called from /voice/status callback for terminal statuses:
    completed, busy, no-answer, failed, canceled.
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

        # Determine which counters to increment
        is_connected = call_status == "completed" and duration > 0
        is_no_answer = call_status in ("no-answer", "canceled")
        is_failed = call_status == "failed"
        is_busy = call_status == "busy"

        # Build dynamic UPDATE
        sets = ["daily_calls_today = daily_calls_today + 1",
                "total_calls = total_calls + 1",
                "last_used_at = %s",
                "updated_at = %s"]
        params = [now, now]

        if is_connected:
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

            # Auto-rest if health drops below critical
            if new_score < HEALTH_CRITICAL and tc >= 10:
                rest_until = now + timedelta(hours=DEFAULT_REST_HOURS)
                cur.execute("""
                    UPDATE number_health
                    SET status = %s, rest_until = %s
                    WHERE location_id = %s AND phone = %s AND status != %s
                """, (STATUS_RESTING, rest_until, location_id, phone, STATUS_FROZEN))
                logger.warning(f"Number {phone} auto-rested (health={new_score}) until {rest_until}")

            # Auto-freeze if health hits zero with enough data
            if new_score < 5 and tc >= 25:
                freeze_until = now + timedelta(hours=DEFAULT_FREEZE_HOURS)
                cur.execute("""
                    UPDATE number_health
                    SET status = %s, rest_until = %s
                    WHERE location_id = %s AND phone = %s
                """, (STATUS_FROZEN, freeze_until, location_id, phone))
                logger.warning(f"Number {phone} auto-FROZEN (health={new_score}) until {freeze_until}")

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
            logger.info(f"Expired {resting_count} resting + {frozen_count} frozen numbers → active")
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

        for stage in range(4):  # 0→1, 1→2, 2→3, 3→4
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
