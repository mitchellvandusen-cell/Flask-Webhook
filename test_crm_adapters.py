#!/usr/bin/env python3
"""
Comprehensive test suite for all CRM adapters in /home/user/Flask-Webhook/crm_adapters/
Tests: imports, factory registry, method existence, capability flags, messaging infra, time parsing.
"""

import sys
import os
import traceback

# Ensure project root is on sys.path
sys.path.insert(0, "/home/user/Flask-Webhook")

passed = 0
failed = 0
errors = []


def report(test_name, ok, detail=""):
    global passed, failed, errors
    if ok:
        passed += 1
        print(f"  PASS  {test_name}")
    else:
        failed += 1
        errors.append((test_name, detail))
        print(f"  FAIL  {test_name}{(' -- ' + detail) if detail else ''}")


# ============================================================================
# 1. Import all adapters - verify no import errors
# ============================================================================
print("=" * 72)
print("TEST GROUP 1: Import all adapters")
print("=" * 72)

adapter_imports = {
    "GHLAdapter": ("crm_adapters.ghl_adapter", "GHLAdapter"),
    "ZapierAdapter": ("crm_adapters.zapier_adapter", "ZapierAdapter"),
    "SalesforceAdapter": ("crm_adapters.salesforce_adapter", "SalesforceAdapter"),
    "HubSpotAdapter": ("crm_adapters.hubspot_adapter", "HubSpotAdapter"),
    "PipedriveAdapter": ("crm_adapters.pipedrive_adapter", "PipedriveAdapter"),
    "ZohoAdapter": ("crm_adapters.zoho_adapter", "ZohoAdapter"),
    "InsureioAdapter": ("crm_adapters.insureio_adapter", "InsureioAdapter"),
}

imported_classes = {}
for name, (mod_path, cls_name) in adapter_imports.items():
    try:
        import importlib
        mod = importlib.import_module(mod_path)
        cls = getattr(mod, cls_name)
        imported_classes[name] = cls
        report(f"Import {name} from {mod_path}", True)
    except Exception as e:
        report(f"Import {name} from {mod_path}", False, str(e))

# ============================================================================
# 2. Import twilio_messaging module - verify no import errors
# ============================================================================
print()
print("=" * 72)
print("TEST GROUP 2: Import twilio_messaging module")
print("=" * 72)

twilio_mod = None
try:
    from crm_adapters import twilio_messaging as twilio_mod
    report("Import crm_adapters.twilio_messaging", True)
except Exception as e:
    report("Import crm_adapters.twilio_messaging", False, str(e))

# ============================================================================
# 3. Factory registry lookup - all variations
# ============================================================================
print()
print("=" * 72)
print("TEST GROUP 3: Factory registry lookup")
print("=" * 72)

try:
    from crm_adapters.factory import CRM_REGISTRY, get_crm_adapter
    report("Import factory module", True)
except Exception as e:
    report("Import factory module", False, str(e))
    CRM_REGISTRY = {}

registry_lookups = [
    ("ghl", "GHLAdapter"),
    ("gohighlevel", "GHLAdapter"),
    ("salesforce", "SalesforceAdapter"),
    ("hubspot", "HubSpotAdapter"),
    ("pipedrive", "PipedriveAdapter"),
    ("zoho", "ZohoAdapter"),
    ("insureio", "InsureioAdapter"),
    ("zapier", "ZapierAdapter"),
]

for key, expected_cls in registry_lookups:
    found = key in CRM_REGISTRY
    if found:
        mod_path, cls_name = CRM_REGISTRY[key]
        report(f"Registry lookup '{key}' -> {cls_name}", cls_name == expected_cls,
               f"Expected {expected_cls}, got {cls_name}" if cls_name != expected_cls else "")
    else:
        report(f"Registry lookup '{key}'", False, "Key not found in CRM_REGISTRY")

# Test case-insensitive factory (GHL, Salesforce, etc.)
print()
print("  -- Case-insensitive factory tests --")

mock_sub = {
    "location_id": "test_loc", "access_token": "test_tok", "calendar_id": "cal",
    "timezone": "America/New_York", "crm_type": "ghl",
    "crm_config": {
        "access_token": "tok", "refresh_token": "ref", "client_id": "cid", "client_secret": "csec",
        "instance_url": "https://test.salesforce.com", "company_domain": "testco", "api_token": "tok",
        "data_center": "com", "api_key": "key", "brand_id": "123",
        "webhook_url": "https://hooks.zapier.com/test",
        "twilio_account_sid": "", "twilio_auth_token": "", "twilio_from_number": "",
        "messaging_webhook_url": "https://example.com/sms",
    }
}

case_tests = [
    ("GHL", "GoHighLevel"),
    ("Salesforce", "Salesforce"),
    ("HUBSPOT", "HubSpot"),
    ("Pipedrive", "Pipedrive"),
    ("ZOHO", "Zoho CRM"),
    ("Insureio", "Insureio"),
    ("ZAPIER", "Zapier"),
]

for input_type, expected_crm_name in case_tests:
    try:
        adapter = get_crm_adapter(input_type, mock_sub)
        ok = adapter.CRM_NAME == expected_crm_name
        report(f"Factory('{input_type}') -> CRM_NAME='{adapter.CRM_NAME}'", ok,
               f"Expected '{expected_crm_name}'" if not ok else "")
    except Exception as e:
        report(f"Factory('{input_type}')", False, str(e))

# ============================================================================
# 4. Instantiate each adapter and verify ALL methods exist
# ============================================================================
print()
print("=" * 72)
print("TEST GROUP 4: Method existence on all adapters")
print("=" * 72)

required_methods = [
    "send_message",
    "get_free_slots",
    "book_appointment",
    "get_contact",
    "create_contact",
    "validate_credentials",
    "search_contact",       # NEW
    "update_contact",       # NEW
    "get_or_create_contact",  # NEW from base
]

adapter_configs = {
    "GHLAdapter": "ghl",
    "ZapierAdapter": "zapier",
    "SalesforceAdapter": "salesforce",
    "HubSpotAdapter": "hubspot",
    "PipedriveAdapter": "pipedrive",
    "ZohoAdapter": "zoho",
    "InsureioAdapter": "insureio",
}

instantiated = {}
for cls_name, crm_type in adapter_configs.items():
    if cls_name not in imported_classes:
        report(f"Instantiate {cls_name}", False, "Class not imported")
        continue

    try:
        sub = dict(mock_sub)
        sub["crm_type"] = crm_type
        adapter = imported_classes[cls_name](sub)
        instantiated[cls_name] = adapter
        report(f"Instantiate {cls_name}", True)
    except Exception as e:
        report(f"Instantiate {cls_name}", False, traceback.format_exc().split('\n')[-2])

    if cls_name not in instantiated:
        continue

    adapter = instantiated[cls_name]
    for method_name in required_methods:
        has_method = hasattr(adapter, method_name) and callable(getattr(adapter, method_name))
        report(f"  {cls_name}.{method_name}() exists", has_method,
               "Method missing or not callable" if not has_method else "")

# ============================================================================
# 5. Verify capability flags: SUPPORTS_MESSAGING = True for all
# ============================================================================
print()
print("=" * 72)
print("TEST GROUP 5: Capability flags (SUPPORTS_MESSAGING = True)")
print("=" * 72)

for cls_name, adapter in instantiated.items():
    val = adapter.SUPPORTS_MESSAGING
    report(f"{cls_name}.SUPPORTS_MESSAGING = {val}", val is True,
           f"Expected True, got {val}" if val is not True else "")

# Also check SUPPORTS_CALENDAR and SUPPORTS_CONTACTS for completeness
print()
print("  -- Additional capability flags --")
expected_calendar = {
    "GHLAdapter": True,
    "ZapierAdapter": True,
    "SalesforceAdapter": True,
    "HubSpotAdapter": True,
    "PipedriveAdapter": True,
    "ZohoAdapter": True,
    "InsureioAdapter": False,  # No native calendar
}
for cls_name, adapter in instantiated.items():
    expected = expected_calendar.get(cls_name, False)
    actual = adapter.SUPPORTS_CALENDAR
    report(f"{cls_name}.SUPPORTS_CALENDAR = {actual} (expected {expected})", actual == expected)

for cls_name, adapter in instantiated.items():
    val = adapter.SUPPORTS_CONTACTS
    report(f"{cls_name}.SUPPORTS_CONTACTS = {val}", val is True)

# ============================================================================
# 6. Verify messaging infrastructure per adapter
# ============================================================================
print()
print("=" * 72)
print("TEST GROUP 6: Messaging infrastructure")
print("=" * 72)

# Adapters that should have messaging_webhook_url attribute
webhook_adapters = ["SalesforceAdapter", "HubSpotAdapter", "PipedriveAdapter", "ZohoAdapter", "InsureioAdapter"]
for cls_name in webhook_adapters:
    if cls_name not in instantiated:
        report(f"{cls_name} has messaging_webhook_url attr", False, "Not instantiated")
        continue
    adapter = instantiated[cls_name]
    has_attr = hasattr(adapter, "messaging_webhook_url")
    report(f"{cls_name} has messaging_webhook_url attr", has_attr)
    if has_attr:
        val = adapter.messaging_webhook_url
        report(f"  messaging_webhook_url = '{val}'", val == "https://example.com/sms",
               f"Got '{val}'" if val != "https://example.com/sms" else "")

# Salesforce should have _log_activity method
print()
print("  -- Salesforce-specific methods --")
if "SalesforceAdapter" in instantiated:
    adapter = instantiated["SalesforceAdapter"]
    has_log = hasattr(adapter, "_log_activity") and callable(adapter._log_activity)
    report("SalesforceAdapter._log_activity() exists", has_log)
else:
    report("SalesforceAdapter._log_activity() exists", False, "Not instantiated")

# HubSpot should have _log_communication method
print()
print("  -- HubSpot-specific methods --")
if "HubSpotAdapter" in instantiated:
    adapter = instantiated["HubSpotAdapter"]
    has_log = hasattr(adapter, "_log_communication") and callable(adapter._log_communication)
    report("HubSpotAdapter._log_communication() exists", has_log)
else:
    report("HubSpotAdapter._log_communication() exists", False, "Not instantiated")

# Pipedrive should have _log_note and create_deal methods
print()
print("  -- Pipedrive-specific methods --")
if "PipedriveAdapter" in instantiated:
    adapter = instantiated["PipedriveAdapter"]
    has_note = hasattr(adapter, "_log_note") and callable(adapter._log_note)
    has_deal = hasattr(adapter, "create_deal") and callable(adapter.create_deal)
    report("PipedriveAdapter._log_note() exists", has_note)
    report("PipedriveAdapter.create_deal() exists", has_deal)
else:
    report("PipedriveAdapter._log_note() exists", False, "Not instantiated")
    report("PipedriveAdapter.create_deal() exists", False, "Not instantiated")

# Zoho should have _log_note and send_email methods
print()
print("  -- Zoho-specific methods --")
if "ZohoAdapter" in instantiated:
    adapter = instantiated["ZohoAdapter"]
    has_note = hasattr(adapter, "_log_note") and callable(adapter._log_note)
    has_email = hasattr(adapter, "send_email") and callable(adapter.send_email)
    report("ZohoAdapter._log_note() exists", has_note)
    report("ZohoAdapter.send_email() exists", has_email)
else:
    report("ZohoAdapter._log_note() exists", False, "Not instantiated")
    report("ZohoAdapter.send_email() exists", False, "Not instantiated")

# ============================================================================
# 7. Verify twilio_messaging module has required functions
# ============================================================================
print()
print("=" * 72)
print("TEST GROUP 7: twilio_messaging module functions")
print("=" * 72)

twilio_funcs = ["send_sms_via_twilio", "get_twilio_config", "has_twilio_config"]
for fn_name in twilio_funcs:
    if twilio_mod:
        has_fn = hasattr(twilio_mod, fn_name) and callable(getattr(twilio_mod, fn_name))
        report(f"twilio_messaging.{fn_name}() exists", has_fn)
    else:
        report(f"twilio_messaging.{fn_name}() exists", False, "Module not imported")

# Functional test: has_twilio_config with empty creds returns False
if twilio_mod:
    empty_cfg = {"twilio_account_sid": "", "twilio_auth_token": "", "twilio_from_number": ""}
    result = twilio_mod.has_twilio_config(empty_cfg)
    report("has_twilio_config({empty}) returns False", result is False, f"Got {result}")

    full_cfg = {"twilio_account_sid": "AC123", "twilio_auth_token": "tok", "twilio_from_number": "+15551234567"}
    result = twilio_mod.has_twilio_config(full_cfg)
    report("has_twilio_config({full_creds}) returns True", result is True, f"Got {result}")

    cfg = twilio_mod.get_twilio_config(full_cfg)
    report("get_twilio_config returns correct dict",
           cfg.get("account_sid") == "AC123" and cfg.get("auth_token") == "tok" and cfg.get("from_number") == "+15551234567",
           f"Got {cfg}")

# ============================================================================
# 8. Test booking time parsing: "2:00 p.m." -> hour 14
# ============================================================================
print()
print("=" * 72)
print("TEST GROUP 8: Booking time parsing '2:00 p.m.' -> hour 14")
print("=" * 72)

# Adapters that have _parse_booking_time
parseable_adapters = ["SalesforceAdapter", "HubSpotAdapter", "PipedriveAdapter", "ZohoAdapter"]
for cls_name in parseable_adapters:
    if cls_name not in instantiated:
        report(f"{cls_name} parse '2:00 p.m.'", False, "Not instantiated")
        continue
    adapter = instantiated[cls_name]
    if not hasattr(adapter, "_parse_booking_time"):
        report(f"{cls_name} parse '2:00 p.m.'", False, "_parse_booking_time not found")
        continue
    try:
        start_dt, end_dt = adapter._parse_booking_time("2:00 p.m.")
        hour = start_dt.hour
        report(f"{cls_name} parse '2:00 p.m.' -> hour={hour}", hour == 14,
               f"Expected 14, got {hour}" if hour != 14 else "")
        # Verify duration is 30 min
        dur = (end_dt - start_dt).total_seconds()
        report(f"  Duration = {int(dur)}s (expected 1800)", dur == 1800)
    except Exception as e:
        report(f"{cls_name} parse '2:00 p.m.'", False, str(e))

# Additional time parsing tests on one adapter (Salesforce)
print()
print("  -- Additional time parsing tests (SalesforceAdapter) --")
if "SalesforceAdapter" in instantiated:
    adapter = instantiated["SalesforceAdapter"]
    time_tests = [
        ("9:00 am", 9),
        ("9:00 a.m.", 9),
        ("12:00 pm", 12),
        ("12:00 p.m.", 12),
        ("4:30 pm", 16),
        ("4:30 p.m.", 16),
        ("3", 15),        # bare number 1-7 -> PM inference
        ("10", 10),       # bare number > 7 -> as-is
        ("1 pm", 13),
    ]
    for time_str, expected_hour in time_tests:
        try:
            start_dt, _ = adapter._parse_booking_time(time_str)
            actual = start_dt.hour
            report(f"  parse '{time_str}' -> hour={actual} (expected {expected_hour})",
                   actual == expected_hour,
                   f"Got {actual}" if actual != expected_hour else "")
        except Exception as e:
            report(f"  parse '{time_str}'", False, str(e))

# GHL adapter doesn't have its own _parse_booking_time (delegates to ghl_calendar),
# and Zapier/Insureio don't parse times. Verify they don't error on book_appointment signature.
print()
print("  -- Non-parsing adapters: book_appointment method signature check --")
for cls_name in ["GHLAdapter", "ZapierAdapter", "InsureioAdapter"]:
    if cls_name not in instantiated:
        report(f"{cls_name} book_appointment callable", False, "Not instantiated")
        continue
    adapter = instantiated[cls_name]
    import inspect
    sig = inspect.signature(adapter.book_appointment)
    params = list(sig.parameters.keys())
    expected_params = ["contact_id", "first_name", "selected_time"]
    has_all = all(p in params for p in expected_params)
    report(f"{cls_name} book_appointment(contact_id, first_name, selected_time)", has_all,
           f"Params: {params}" if not has_all else "")


# ============================================================================
# SUMMARY
# ============================================================================
print()
print("=" * 72)
print(f"RESULTS: {passed} passed, {failed} failed, {passed + failed} total")
print("=" * 72)

if errors:
    print()
    print("FAILURES:")
    for test_name, detail in errors:
        print(f"  - {test_name}: {detail}")

sys.exit(0 if failed == 0 else 1)
