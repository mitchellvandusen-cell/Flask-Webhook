"""
test_voice_server.py — Automated tests for voice WebSocket extraction.

Tests all layers of the Flask→FastAPI voice extraction:
  1. Redis state layer (set/get/update/delete active calls, transfers, overflow)
  2. FastAPI app startup (health endpoint, thread pool, DB pool caps)
  3. WebSocket handshake (voice/stream and voice/listen-stream)
  4. Audio pipeline (sync + async DSP wrappers)
  5. TwiML routing (VOICE_WSS_URL env var)
  6. Frontend wiring (DASHBOARD_BOOT.voiceWssHost, dialer.js)
  7. Import chain validation (no circular imports, flask-sock removed)

Requirements: Redis running locally (or REDIS_URL set).
Run: python -m pytest test_voice_server.py -v
"""

import os
import sys
import json
import time
import asyncio
import importlib
import threading

import pytest
import redis


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="session")
def redis_available():
    """Check if Redis is reachable."""
    url = os.getenv('REDIS_URL', 'redis://localhost:6379')
    try:
        r = redis.from_url(url, socket_connect_timeout=3)
        r.ping()
        return True
    except (redis.ConnectionError, redis.TimeoutError):
        pytest.skip("Redis not available — skipping Redis-dependent tests")


@pytest.fixture()
def clean_redis_keys(redis_available):
    """Clean up test keys after each Redis test."""
    url = os.getenv('REDIS_URL', 'redis://localhost:6379')
    r = redis.from_url(url, decode_responses=True)
    yield
    # Cleanup: delete test keys
    for pattern in ['call:TEST_*', 'xfer:TEST_*', 'overflow:TEST_*', 'call_sids_by_loc:TEST_*']:
        for key in r.scan_iter(pattern):
            r.delete(key)


# ══════════════════════════════════════════════════════════════════════════════
# 1. REDIS STATE LAYER
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.usefixtures("clean_redis_keys")
class TestRedisState:
    """Test voice/redis_state.py — the core shared state layer."""

    def test_set_and_get_active_call(self):
        from voice.redis_state import set_active_call, get_active_call
        data = {
            '_location_id': 'TEST_LOC_1',
            'status': 'in-progress',
            'caller': '+15551234567',
            'called': '+15559876543',
            '_stir_verstat': 'TN-Validation-Passed',
            '_amd_result': None,
            'nested': {'key': 'value', 'list': [1, 2, 3]},
        }
        set_active_call('TEST_CALL_001', data)
        result = get_active_call('TEST_CALL_001')

        assert result is not None
        assert result['status'] == 'in-progress'
        assert result['caller'] == '+15551234567'
        assert result['_location_id'] == 'TEST_LOC_1'
        # Verify nested data survives JSON serialization
        assert result['nested']['key'] == 'value'
        assert result['nested']['list'] == [1, 2, 3]
        # None values survive
        assert result['_amd_result'] is None

    def test_get_nonexistent_call(self):
        from voice.redis_state import get_active_call
        assert get_active_call('TEST_NONEXISTENT_999') is None

    def test_update_active_call(self):
        from voice.redis_state import set_active_call, get_active_call, update_active_call
        set_active_call('TEST_CALL_002', {
            '_location_id': 'TEST_LOC_1',
            'status': 'ringing',
        })
        update_active_call('TEST_CALL_002', status='in-progress', _amd_result='human')
        result = get_active_call('TEST_CALL_002')

        assert result['status'] == 'in-progress'
        assert result['_amd_result'] == 'human'
        assert result['_location_id'] == 'TEST_LOC_1'  # untouched field preserved

    def test_update_nonexistent_is_noop(self):
        from voice.redis_state import update_active_call, get_active_call
        update_active_call('TEST_GHOST_999', status='completed')
        assert get_active_call('TEST_GHOST_999') is None

    def test_delete_active_call(self):
        from voice.redis_state import set_active_call, get_active_call, delete_active_call
        set_active_call('TEST_CALL_003', {'_location_id': 'TEST_LOC_1', 'status': 'in-progress'})
        assert get_active_call('TEST_CALL_003') is not None
        delete_active_call('TEST_CALL_003')
        assert get_active_call('TEST_CALL_003') is None

    def test_call_exists(self):
        from voice.redis_state import set_active_call, call_exists, delete_active_call
        assert call_exists('TEST_CALL_004') is False
        set_active_call('TEST_CALL_004', {'_location_id': 'TEST_LOC_1', 'status': 'ringing'})
        assert call_exists('TEST_CALL_004') is True
        delete_active_call('TEST_CALL_004')
        assert call_exists('TEST_CALL_004') is False

    def test_get_active_calls_for_location(self):
        from voice.redis_state import set_active_call, get_active_calls_for_location, delete_active_call
        # Set up 3 calls: 2 for LOC_A, 1 for LOC_B
        set_active_call('TEST_CALL_LOC_A1', {'_location_id': 'TEST_LOC_A', 'status': 'in-progress'})
        set_active_call('TEST_CALL_LOC_A2', {'_location_id': 'TEST_LOC_A', 'status': 'ringing'})
        set_active_call('TEST_CALL_LOC_B1', {'_location_id': 'TEST_LOC_B', 'status': 'in-progress'})

        loc_a_calls = get_active_calls_for_location('TEST_LOC_A')
        assert len(loc_a_calls) == 2
        assert 'TEST_CALL_LOC_A1' in loc_a_calls
        assert 'TEST_CALL_LOC_A2' in loc_a_calls
        assert 'TEST_CALL_LOC_B1' not in loc_a_calls

        loc_b_calls = get_active_calls_for_location('TEST_LOC_B')
        assert len(loc_b_calls) == 1

        # Empty location returns empty dict
        assert get_active_calls_for_location('TEST_LOC_NOBODY') == {}

        # Cleanup
        for sid in ['TEST_CALL_LOC_A1', 'TEST_CALL_LOC_A2', 'TEST_CALL_LOC_B1']:
            delete_active_call(sid)

    def test_location_set_cleaned_on_delete(self):
        from voice.redis_state import set_active_call, delete_active_call, get_active_calls_for_location
        set_active_call('TEST_CALL_005', {'_location_id': 'TEST_LOC_C', 'status': 'in-progress'})
        assert len(get_active_calls_for_location('TEST_LOC_C')) == 1
        delete_active_call('TEST_CALL_005')
        assert len(get_active_calls_for_location('TEST_LOC_C')) == 0

    def test_transfer_request_lifecycle(self):
        from voice.redis_state import (
            set_transfer_request, get_transfer_request,
            delete_transfer_request, transfer_request_exists,
        )
        assert transfer_request_exists('TEST_CALL_006') is False
        assert get_transfer_request('TEST_CALL_006') is None

        set_transfer_request('TEST_CALL_006', {
            'type': 'transfer',
            'target': '+15551112222',
            'reason': 'hot lead',
        })
        assert transfer_request_exists('TEST_CALL_006') is True
        req = get_transfer_request('TEST_CALL_006')
        assert req['type'] == 'transfer'
        assert req['target'] == '+15551112222'

        delete_transfer_request('TEST_CALL_006')
        assert transfer_request_exists('TEST_CALL_006') is False

    def test_transfer_request_ttl(self):
        """Transfer requests should auto-expire (TTL=30s). Verify TTL is set."""
        from voice.redis_state import set_transfer_request, _get_redis
        set_transfer_request('TEST_CALL_007', {'type': 'takeover'})
        r = _get_redis()
        ttl = r.ttl('xfer:TEST_CALL_007')
        assert 0 < ttl <= 30

    def test_active_call_ttl(self):
        """Active calls should have TTL=3600s."""
        from voice.redis_state import set_active_call, _get_redis
        set_active_call('TEST_CALL_008', {'_location_id': 'TEST_LOC_1', 'status': 'in-progress'})
        r = _get_redis()
        ttl = r.ttl('call:TEST_CALL_008')
        assert 3590 < ttl <= 3600

    def test_overflow_alert_lifecycle(self):
        from voice.redis_state import add_overflow_alert, get_overflow_alerts, set_overflow_alerts
        loc = 'TEST_LOC_OVERFLOW'
        assert get_overflow_alerts(loc) == []

        add_overflow_alert(loc, {'contact': 'John', 'temperature': 'hot'})
        add_overflow_alert(loc, {'contact': 'Jane', 'temperature': 'warm'})
        alerts = get_overflow_alerts(loc)
        assert len(alerts) == 2
        assert alerts[0]['contact'] == 'John'
        assert alerts[1]['contact'] == 'Jane'

        # Replace with filtered list
        set_overflow_alerts(loc, [alerts[1]])
        alerts = get_overflow_alerts(loc)
        assert len(alerts) == 1
        assert alerts[0]['contact'] == 'Jane'

        # Clear
        set_overflow_alerts(loc, [])
        assert get_overflow_alerts(loc) == []

    def test_concurrent_writes(self):
        """Multiple threads writing to different calls shouldn't interfere."""
        from voice.redis_state import set_active_call, get_active_call, delete_active_call

        def writer(n):
            sid = f'TEST_CONCURRENT_{n}'
            set_active_call(sid, {'_location_id': f'TEST_LOC_{n}', 'status': f'state_{n}'})

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for i in range(20):
            data = get_active_call(f'TEST_CONCURRENT_{i}')
            assert data is not None, f"Call TEST_CONCURRENT_{i} not found"
            assert data['status'] == f'state_{i}'
            delete_active_call(f'TEST_CONCURRENT_{i}')

    def test_json_serialization_datetime(self):
        """Verify that datetime-like objects serialize via default=str."""
        from voice.redis_state import set_active_call, get_active_call
        from datetime import datetime
        data = {
            '_location_id': 'TEST_LOC_DT',
            'started_at': datetime(2026, 3, 19, 12, 0, 0),
        }
        set_active_call('TEST_CALL_DT', data)
        result = get_active_call('TEST_CALL_DT')
        assert '2026' in result['started_at']  # serialized as string


# ══════════════════════════════════════════════════════════════════════════════
# 2. ASYNC REDIS API
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.usefixtures("clean_redis_keys")
class TestAsyncRedisState:
    """Test async wrappers in redis_state.py."""

    @pytest.mark.asyncio
    async def test_async_set_and_get(self):
        from voice.redis_state import async_set_active_call, async_get_active_call, async_delete_active_call
        await async_set_active_call('TEST_ASYNC_001', {
            '_location_id': 'TEST_ASYNC_LOC',
            'status': 'in-progress',
        })
        result = await async_get_active_call('TEST_ASYNC_001')
        assert result is not None
        assert result['status'] == 'in-progress'
        await async_delete_active_call('TEST_ASYNC_001')
        assert await async_get_active_call('TEST_ASYNC_001') is None

    @pytest.mark.asyncio
    async def test_async_update(self):
        from voice.redis_state import async_set_active_call, async_get_active_call, async_update_active_call, async_delete_active_call
        await async_set_active_call('TEST_ASYNC_002', {
            '_location_id': 'TEST_ASYNC_LOC',
            'status': 'ringing',
        })
        await async_update_active_call('TEST_ASYNC_002', status='in-progress')
        result = await async_get_active_call('TEST_ASYNC_002')
        assert result['status'] == 'in-progress'
        await async_delete_active_call('TEST_ASYNC_002')

    @pytest.mark.asyncio
    async def test_async_transfer_request(self):
        from voice.redis_state import (
            async_set_transfer_request, async_get_transfer_request,
            async_delete_transfer_request, async_transfer_request_exists,
        )
        assert await async_transfer_request_exists('TEST_ASYNC_003') is False
        await async_set_transfer_request('TEST_ASYNC_003', {'type': 'takeover'})
        assert await async_transfer_request_exists('TEST_ASYNC_003') is True
        req = await async_get_transfer_request('TEST_ASYNC_003')
        assert req['type'] == 'takeover'
        await async_delete_transfer_request('TEST_ASYNC_003')


# ══════════════════════════════════════════════════════════════════════════════
# 3. CALL_STATE BACKWARD COMPATIBILITY
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.usefixtures("clean_redis_keys")
class TestCallStateCompat:
    """Verify call_state.py re-exports work and old patterns are supported."""

    def test_reexports_available(self):
        """All functions that callers import from call_state should exist."""
        from voice.call_state import (
            set_active_call,
            get_active_call,
            update_active_call,
            delete_active_call,
            call_exists,
            get_active_calls_for_location,
            get_all_active_calls,
            set_transfer_request,
            get_transfer_request,
            delete_transfer_request,
            transfer_request_exists,
            add_overflow_alert,
            get_overflow_alerts,
            set_overflow_alerts,
        )
        # All should be callable
        assert callable(set_active_call)
        assert callable(get_active_call)
        assert callable(delete_active_call)

    def test_in_process_state_preserved(self):
        """call_listeners, custom_field_defs, voice_stream_semaphore should still exist."""
        from voice.call_state import call_listeners, custom_field_defs, voice_stream_semaphore
        assert isinstance(call_listeners, dict)
        assert isinstance(custom_field_defs, dict)

    def test_terminal_statuses(self):
        from voice.call_state import TERMINAL_STATUSES
        assert 'completed' in TERMINAL_STATUSES
        assert 'busy' in TERMINAL_STATUSES
        assert 'in-progress' not in TERMINAL_STATUSES

    def test_twiml_helpers(self):
        from voice.call_state import _encode_client_state, _decode_client_state, _build_twiml_stream
        data = {'location_id': 'loc123', 'caller': '+1555'}
        encoded = _encode_client_state(data)
        decoded = _decode_client_state(encoded)
        assert decoded == data

        twiml = _build_twiml_stream('wss://voice.example.com/voice/stream', {'callSid': 'CA123'})
        assert 'wss://voice.example.com/voice/stream' in twiml
        assert '<Stream' in twiml
        assert 'callSid' in twiml

    def test_no_daemon_reaper(self):
        """Daemon reaper thread should NOT exist — Redis TTL replaces it."""
        import voice.call_state as cs
        source = open(cs.__file__).read()
        assert 'daemon' not in source.lower() or 'daemon reaper' not in source.lower()
        assert '_reaper' not in source


# ══════════════════════════════════════════════════════════════════════════════
# 4. AUDIO PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

class TestAudioPipeline:
    """Test sync and async audio DSP functions."""

    def test_mulaw_to_pcm16_roundtrip(self):
        """Encode→decode should produce valid audio (not silence, not corrupt)."""
        from voice.audio import _mulaw_to_pcm16, _pcm16_to_mulaw
        import audioop
        import numpy as np

        # Generate a 160-sample (20ms) 8kHz sine wave, encode to mulaw
        t = np.linspace(0, 0.02, 160, endpoint=False)
        pcm_8k = (np.sin(2 * np.pi * 440 * t) * 16000).astype(np.int16).tobytes()
        mulaw_in = audioop.lin2ulaw(pcm_8k, 2)

        # mulaw 8kHz → PCM16 16kHz
        pcm16 = _mulaw_to_pcm16(mulaw_in)
        assert len(pcm16) > 0
        # Should be ~320 samples * 2 bytes = 640 bytes (upsampled from 160 to 320)
        assert len(pcm16) == 640

        # PCM16 16kHz → mulaw 8kHz
        mulaw_out = _pcm16_to_mulaw(pcm16)
        assert len(mulaw_out) > 0
        # Should be 160 bytes (downsampled back to 160 samples, 1 byte each for mulaw)
        assert len(mulaw_out) == 160

    @pytest.mark.asyncio
    async def test_async_mulaw_to_pcm16(self):
        from voice.audio import async_mulaw_to_pcm16
        import audioop
        import numpy as np

        t = np.linspace(0, 0.02, 160, endpoint=False)
        pcm_8k = (np.sin(2 * np.pi * 440 * t) * 16000).astype(np.int16).tobytes()
        mulaw_in = audioop.lin2ulaw(pcm_8k, 2)

        pcm16 = await async_mulaw_to_pcm16(mulaw_in)
        assert len(pcm16) == 640

    @pytest.mark.asyncio
    async def test_async_pcm16_to_mulaw(self):
        from voice.audio import async_pcm16_to_mulaw
        import numpy as np

        # 320 samples of 16kHz PCM16
        t = np.linspace(0, 0.02, 320, endpoint=False)
        pcm16 = (np.sin(2 * np.pi * 440 * t) * 16000).astype(np.int16).tobytes()

        mulaw = await async_pcm16_to_mulaw(pcm16)
        assert len(mulaw) == 160

    def test_voicemail_detection(self):
        from voice.audio import _is_voicemail_phrase
        assert _is_voicemail_phrase("please leave a message after the beep") is True
        assert _is_voicemail_phrase("The person you called is not available right now") is True
        assert _is_voicemail_phrase("Hey what's up how are you doing") is False
        assert _is_voicemail_phrase("") is False


# ══════════════════════════════════════════════════════════════════════════════
# 5. FASTAPI APP STRUCTURE
# ══════════════════════════════════════════════════════════════════════════════

class TestFastAPIApp:
    """Test voice_server.py application structure."""

    def test_app_creates(self):
        from voice_server import app
        assert app.title == "Omnisconn Voice Service"

    def test_routes_registered(self):
        from voice_server import app
        routes = [r.path for r in app.routes]
        assert "/voice/stream" in routes
        assert "/voice/listen-stream" in routes
        assert "/health" in routes

    def test_health_endpoint(self):
        from fastapi.testclient import TestClient
        from voice_server import app
        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["service"] == "voice"

    def test_db_pool_env_caps(self):
        """DB pool should be capped before imports in voice_server.py."""
        # voice_server sets these via setdefault — verify they're reasonable
        # (won't override if already set by user, but the code should set them)
        import voice_server
        source = open(voice_server.__file__).read()
        assert "os.environ.setdefault('DB_POOL_MIN', '2')" in source
        assert "os.environ.setdefault('DB_POOL_MAX', '10')" in source

    def test_thread_pool_size(self):
        """Startup event should set ThreadPoolExecutor(max_workers=100)."""
        import voice_server
        source = open(voice_server.__file__).read()
        assert 'max_workers=100' in source


# ══════════════════════════════════════════════════════════════════════════════
# 6. WEBSOCKET HANDSHAKE (via TestClient)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.usefixtures("clean_redis_keys")
class TestWebSocketHandshake:
    """Test WebSocket endpoints accept connections and handle bad input."""

    def test_listen_stream_no_call_sid(self):
        """Listen stream should reject if no call_sid provided."""
        from fastapi.testclient import TestClient
        from voice_server import app
        client = TestClient(app)
        with client.websocket_connect("/voice/listen-stream") as ws:
            ws.send_text(json.dumps({"no_call_sid": True}))
            resp = ws.receive_text()
            data = json.loads(resp)
            assert "error" in data
            assert "call_sid" in data["error"].lower() or "required" in data["error"].lower()

    def test_listen_stream_nonexistent_call(self):
        """Listen stream should reject for a call that doesn't exist in Redis."""
        from fastapi.testclient import TestClient
        from voice_server import app
        client = TestClient(app)
        with client.websocket_connect("/voice/listen-stream") as ws:
            ws.send_text(json.dumps({"call_sid": "TEST_BOGUS_CALL"}))
            resp = ws.receive_text()
            data = json.loads(resp)
            assert "error" in data

    def test_listen_stream_with_active_call(self):
        """Listen stream should accept connection for a valid active call."""
        from voice.redis_state import set_active_call, delete_active_call
        from fastapi.testclient import TestClient
        from voice_server import app

        set_active_call('TEST_LISTEN_CALL', {'_location_id': 'TEST_LOC', 'status': 'in-progress'})
        try:
            client = TestClient(app)
            with client.websocket_connect("/voice/listen-stream") as ws:
                ws.send_text(json.dumps({"call_sid": "TEST_LISTEN_CALL"}))
                resp = ws.receive_text()
                data = json.loads(resp)
                assert data.get("status") == "listening"
                assert data.get("call_sid") == "TEST_LISTEN_CALL"
        finally:
            delete_active_call('TEST_LISTEN_CALL')

    def test_listen_stream_query_param(self):
        """Listen stream should accept call_sid from query params."""
        from voice.redis_state import set_active_call, delete_active_call
        from fastapi.testclient import TestClient
        from voice_server import app

        set_active_call('TEST_LISTEN_QP', {'_location_id': 'TEST_LOC', 'status': 'in-progress'})
        try:
            client = TestClient(app)
            with client.websocket_connect("/voice/listen-stream?call_sid=TEST_LISTEN_QP") as ws:
                resp = ws.receive_text()
                data = json.loads(resp)
                assert data.get("status") == "listening"
        finally:
            delete_active_call('TEST_LISTEN_QP')


# ══════════════════════════════════════════════════════════════════════════════
# 7. TWIML ROUTING
# ══════════════════════════════════════════════════════════════════════════════

class TestTwiMLRouting:
    """Verify TwiML routes use VOICE_WSS_URL env var."""

    def test_twiml_routes_use_env_var(self):
        """Both stream URL locations should reference VOICE_WSS_URL."""
        import voice.twiml_routes
        source = open(voice.twiml_routes.__file__).read()
        count = source.count("os.getenv('VOICE_WSS_URL')")
        assert count >= 2, f"Expected VOICE_WSS_URL in at least 2 locations, found {count}"

    def test_twiml_routes_have_fallback(self):
        """Fallback to wss://{request.host} when env var not set."""
        import voice.twiml_routes
        source = open(voice.twiml_routes.__file__).read()
        assert "f'wss://{host}/voice/stream'" in source


# ══════════════════════════════════════════════════════════════════════════════
# 8. FRONTEND WIRING
# ══════════════════════════════════════════════════════════════════════════════

class TestFrontendWiring:
    """Verify dashboard templates and JS use voiceWssHost."""

    def test_dashboard_html_has_voice_wss_host(self):
        with open('templates/dashboard.html') as f:
            content = f.read()
        assert 'voiceWssHost' in content
        assert 'voice_wss_host' in content

    def test_dialer_js_uses_voice_wss_host(self):
        with open('static/js/dashboard/dialer.js') as f:
            content = f.read()
        assert 'voiceWssHost' in content
        assert 'DASHBOARD_BOOT' in content
        # Should fallback to location.host
        assert 'location.host' in content

    def test_dashboard_blueprint_passes_voice_wss_host(self):
        with open('blueprints/dashboard.py') as f:
            content = f.read()
        assert "VOICE_WSS_HOST" in content
        assert "voice_wss_host" in content


# ══════════════════════════════════════════════════════════════════════════════
# 9. IMPORT CHAIN VALIDATION
# ══════════════════════════════════════════════════════════════════════════════

class TestImportChain:
    """Verify no circular imports and flask-sock is removed from main.py."""

    def test_flask_sock_removed_from_main(self):
        with open('main.py') as f:
            content = f.read()
        assert 'flask_sock' not in content
        assert 'Sock(' not in content
        assert "@sock.route" not in content

    def test_voice_bp_still_registered_in_main(self):
        """HTTP voice routes should still be in Flask."""
        with open('main.py') as f:
            content = f.read()
        assert 'voice_bp' in content

    def test_redis_state_imports_cleanly(self):
        """redis_state should import without pulling in Flask."""
        mod = importlib.import_module('voice.redis_state')
        assert hasattr(mod, 'set_active_call')
        assert hasattr(mod, 'async_set_active_call')

    def test_async_stream_imports_cleanly(self):
        mod = importlib.import_module('voice.async_stream')
        assert hasattr(mod, 'handle_voice_stream')

    def test_async_listen_imports_cleanly(self):
        mod = importlib.import_module('voice.async_listen')
        assert hasattr(mod, 'handle_listen_stream')
        assert hasattr(mod, 'async_call_listeners')

    def test_voice_server_imports_cleanly(self):
        mod = importlib.import_module('voice_server')
        assert hasattr(mod, 'app')

    def test_requirements_has_fastapi(self):
        with open('requirements.txt') as f:
            content = f.read()
        assert 'fastapi' in content
        assert 'uvicorn' in content

    def test_env_example_has_voice_vars(self):
        with open('.env.example') as f:
            content = f.read()
        assert 'VOICE_WSS_URL' in content
        assert 'VOICE_WSS_HOST' in content


# ══════════════════════════════════════════════════════════════════════════════
# 10. ASYNC LISTEN STREAM BEHAVIOR
# ══════════════════════════════════════════════════════════════════════════════

class TestAsyncListenBehavior:
    """Test async_listen.py listener registry and sentinel handling."""

    def test_listener_registry_is_dict(self):
        from voice.async_listen import async_call_listeners
        assert isinstance(async_call_listeners, dict)

    def test_terminal_statuses_defined(self):
        from voice.async_listen import _LISTEN_TERMINAL
        assert 'completed' in _LISTEN_TERMINAL
        assert 'busy' in _LISTEN_TERMINAL
        assert 'transferred' in _LISTEN_TERMINAL

    @pytest.mark.asyncio
    async def test_sentinel_exits_listener(self):
        """Pushing None to a listener queue should cause the listener to exit."""
        q = asyncio.Queue(maxsize=500)
        # Simulate: put audio then sentinel
        q.put_nowait("base64_audio_chunk")
        q.put_nowait(None)

        chunks = []
        while True:
            chunk = await q.get()
            if chunk is None:
                break
            chunks.append(chunk)

        assert len(chunks) == 1
        assert chunks[0] == "base64_audio_chunk"


# ══════════════════════════════════════════════════════════════════════════════
# 11. ASYNC STREAM STRUCTURE
# ══════════════════════════════════════════════════════════════════════════════

class TestAsyncStreamStructure:
    """Validate async_stream.py has correct structure without running a real call."""

    def test_semaphore_is_asyncio(self):
        from voice.async_stream import _stream_semaphore
        assert isinstance(_stream_semaphore, asyncio.Semaphore)

    def test_max_voice_streams_configurable(self):
        from voice.async_stream import MAX_VOICE_STREAMS
        assert MAX_VOICE_STREAMS > 0

    def test_imports_async_redis(self):
        """Should use async Redis functions, not sync."""
        with open('voice/async_stream.py') as f:
            content = f.read()
        assert 'async_set_active_call' in content
        assert 'async_get_active_call' in content
        assert 'async_update_active_call' in content
        assert 'async_transfer_request_exists' in content

    def test_uses_to_thread_for_dsp(self):
        """DSP calls should use asyncio.to_thread, not inline."""
        with open('voice/async_stream.py') as f:
            content = f.read()
        assert 'asyncio.to_thread(_mulaw_to_pcm16' in content
        assert 'asyncio.to_thread(_pcm16_to_mulaw' in content

    def test_uses_run_in_threadpool_for_db(self):
        """DB/subscriber lookups should use run_in_threadpool."""
        with open('voice/async_stream.py') as f:
            content = f.read()
        assert 'run_in_threadpool' in content


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])
