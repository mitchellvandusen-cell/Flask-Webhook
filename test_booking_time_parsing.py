#!/usr/bin/env python3
"""
Test script for _parse_booking_time across CRM adapters.
Tests HubSpot, Pipedrive, Zoho (dedicated method) and Salesforce (inline parser extracted).
"""

import sys
import os
import re
from datetime import datetime, timedelta, time as dt_time
from zoneinfo import ZoneInfo

# Add project root to path
sys.path.insert(0, "/home/user/Flask-Webhook")

from crm_adapters.hubspot_adapter import HubSpotAdapter
from crm_adapters.pipedrive_adapter import PipedriveAdapter
from crm_adapters.zoho_adapter import ZohoAdapter

# ---- Mock subscriber data ----
mock_sub = {
    "location_id": "test",
    "access_token": "tok",
    "calendar_id": "cal",
    "timezone": "America/New_York",
    "crm_type": "hubspot",
    "crm_config": {
        "access_token": "tok",
        "company_domain": "test",
        "api_token": "tok",
        "data_center": "com",
    },
}


# ---- Salesforce inline parser extracted as standalone function ----
def salesforce_parse_booking_time(selected_time: str, timezone: str = "America/New_York"):
    """Extracted from SalesforceAdapter.book_appointment() inline parsing logic."""
    local_tz = ZoneInfo(timezone)
    now_local = datetime.now(local_tz)
    target_date = now_local.date()

    time_str = selected_time.lower().strip()
    if "tomorrow" in time_str:
        target_date = (now_local + timedelta(days=1)).date()

    hour, minute = 14, 0
    match = re.search(r'(\d{1,2}):?(\d{2})?\s*(pm|p\.m\.|am|a\.m\.)', time_str)
    if match:
        h = int(match.group(1))
        m = int(match.group(2) or 0)
        period = match.group(3).lower().replace(".", "")
        if "pm" in period and h != 12:
            h += 12
        elif "am" in period and h == 12:
            h = 0
        hour, minute = h, m
    else:
        bare = re.search(r'(\d{1,2})', time_str)
        if bare:
            h = int(bare.group(1))
            if 1 <= h <= 7:
                h += 12
            hour = h

    start_dt = datetime.combine(target_date, dt_time(max(9, min(19, hour)), minute), tzinfo=local_tz)
    end_dt = start_dt + timedelta(minutes=30)
    return start_dt, end_dt


# ---- Test cases ----
# Format: (input_string, expected_hour, expected_minute, description, expect_tomorrow)
local_tz = ZoneInfo("America/New_York")
now_local = datetime.now(local_tz)
today = now_local.date()
tomorrow = (now_local + timedelta(days=1)).date()

TEST_CASES = [
    ("4 pm",          16, 0,  "explicit PM",                    today),
    ("4",             16, 0,  "bare number PM inference (1-7)",  today),
    ("9 am",           9, 0,  "explicit AM",                    today),
    ("2:00 p.m.",     14, 0,  "period-style PM (p.m.)",         today),
    ("2:30pm",        14, 30, "time with minutes, no space",    today),
    ("12 pm",         12, 0,  "noon",                           today),
    ("12 am",          9, 0,  "midnight -> clamped to 9",       today),
    ("tomorrow 3pm",  15, 0,  "tomorrow prefix",                tomorrow),
    ("10",            10, 0,  "bare 10, no PM inference (>7)",   today),
    ("7 pm",          19, 0,  "7 PM explicit",                  today),
]


def run_tests_for_adapter(adapter_name, parse_fn):
    """Run all test cases against a parse function. Returns (pass_count, fail_count)."""
    passes = 0
    fails = 0

    print(f"\n{'='*70}")
    print(f"  {adapter_name}")
    print(f"{'='*70}")

    for i, (input_str, exp_hour, exp_minute, desc, exp_date) in enumerate(TEST_CASES, 1):
        start_dt, end_dt = parse_fn(input_str)
        actual_hour = start_dt.hour
        actual_minute = start_dt.minute
        actual_date = start_dt.date()

        hour_ok = (actual_hour == exp_hour)
        minute_ok = (actual_minute == exp_minute)
        date_ok = (actual_date == exp_date)
        all_ok = hour_ok and minute_ok and date_ok

        status = "PASS" if all_ok else "FAIL"
        if all_ok:
            passes += 1
        else:
            fails += 1

        # Build detail string
        details = []
        if not hour_ok:
            details.append(f"hour: got {actual_hour}, expected {exp_hour}")
        if not minute_ok:
            details.append(f"minute: got {actual_minute}, expected {exp_minute}")
        if not date_ok:
            details.append(f"date: got {actual_date}, expected {exp_date}")

        detail_str = f"  ** {'; '.join(details)} **" if details else ""

        print(f"  [{status}] Case {i:>2}: {input_str!r:20s} -> hour={actual_hour:>2}, min={actual_minute:>2}, date={actual_date}"
              f"  (expected h={exp_hour:>2} m={exp_minute:>2} d={exp_date})  [{desc}]{detail_str}")

    return passes, fails


def main():
    total_passes = 0
    total_fails = 0

    print(f"Running booking time parsing tests")
    print(f"Timezone: America/New_York")
    print(f"Current local time: {now_local.strftime('%Y-%m-%d %H:%M %Z')}")
    print(f"Today: {today}  |  Tomorrow: {tomorrow}")

    # ---- HubSpot ----
    hs = HubSpotAdapter(mock_sub)
    p, f = run_tests_for_adapter("HubSpot Adapter (_parse_booking_time)", hs._parse_booking_time)
    total_passes += p
    total_fails += f

    # ---- Pipedrive ----
    pd_sub = dict(mock_sub)
    pd_sub["crm_type"] = "pipedrive"
    pd = PipedriveAdapter(pd_sub)
    p, f = run_tests_for_adapter("Pipedrive Adapter (_parse_booking_time)", pd._parse_booking_time)
    total_passes += p
    total_fails += f

    # ---- Zoho ----
    zh_sub = dict(mock_sub)
    zh_sub["crm_type"] = "zoho"
    zh = ZohoAdapter(zh_sub)
    p, f = run_tests_for_adapter("Zoho Adapter (_parse_booking_time)", zh._parse_booking_time)
    total_passes += p
    total_fails += f

    # ---- Salesforce (inline parser extracted) ----
    p, f = run_tests_for_adapter(
        "Salesforce Adapter (inline parser, extracted)",
        lambda s: salesforce_parse_booking_time(s, "America/New_York")
    )
    total_passes += p
    total_fails += f

    # ---- Summary ----
    print(f"\n{'='*70}")
    print(f"  SUMMARY: {total_passes} passed, {total_fails} failed out of {total_passes + total_fails} total")
    print(f"{'='*70}")

    if total_fails > 0:
        print(f"\n  WARNING: {total_fails} test(s) FAILED. Review details above.")
        sys.exit(1)
    else:
        print(f"\n  All tests passed.")
        sys.exit(0)


if __name__ == "__main__":
    main()
