"""Process-wide upstream throttle (``upstream_min_interval_sec`` /
``upstream_max_concurrency``).

The gate is opt-in: both knobs default to 0 and must leave behavior unchanged.
When enabled it spaces the *starts* of generation calls and caps simultaneous
in-flight calls; retries go through the same gate because each attempt acquires
it again.
"""
import threading
import unittest
import urllib.error
from unittest import mock

from gemini_web2api import gemini
from gemini_web2api.config import CONFIG, DEFAULT_CONFIG


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False


class _FakeStream:
    def __init__(self, chunks=()):
        self._chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def raise_for_status(self):
        return None

    def iter_text(self):
        return iter(self._chunks)


class _FakeClient:
    def stream(self, *args, **kwargs):
        return _FakeStream()


class ThrottleTests(unittest.TestCase):
    _KEYS = ("upstream_min_interval_sec", "upstream_max_concurrency",
             "retry_attempts", "retry_delay_sec", "proxy")

    def setUp(self):
        self._saved = {k: CONFIG.get(k) for k in self._KEYS}
        self._has_httpx = gemini.HAS_HTTPX
        gemini._reset_upstream_gate()

    def tearDown(self):
        CONFIG.update(self._saved)
        gemini.HAS_HTTPX = self._has_httpx
        gemini._reset_upstream_gate()

    def test_defaults_are_off(self):
        self.assertEqual(DEFAULT_CONFIG["upstream_min_interval_sec"], 0.0)
        self.assertEqual(DEFAULT_CONFIG["upstream_max_concurrency"], 0)

    def test_disabled_gate_does_not_sleep(self):
        CONFIG.update({"upstream_min_interval_sec": 0,
                       "upstream_max_concurrency": 0})
        gemini._reset_upstream_gate()

        with mock.patch.object(gemini.time, "sleep") as sleep:
            with gemini._upstream_gate():
                pass

        sleep.assert_not_called()

    def test_interval_spaces_start_times(self):
        CONFIG.update({"upstream_min_interval_sec": 1.0,
                       "upstream_max_concurrency": 0})
        gemini._reset_upstream_gate()

        with mock.patch.object(gemini.time, "monotonic",
                               side_effect=[100.0, 100.2, 100.5, 101.0]), \
                mock.patch.object(gemini.time, "sleep") as sleep:
            with gemini._upstream_gate():
                pass
            with gemini._upstream_gate():
                pass

        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args[0][0], 0.7, delta=1e-6)

    def test_concurrency_cap_serializes(self):
        CONFIG.update({"upstream_min_interval_sec": 0,
                       "upstream_max_concurrency": 1})
        gemini._reset_upstream_gate()

        a_inside = threading.Event()
        release_a = threading.Event()
        b_inside = threading.Event()

        def worker_a():
            with gemini._upstream_gate():
                a_inside.set()
                release_a.wait(2)

        def worker_b():
            with gemini._upstream_gate():
                b_inside.set()

        thread_a = threading.Thread(target=worker_a)
        thread_b = threading.Thread(target=worker_b)
        thread_a.start()
        self.assertTrue(a_inside.wait(2))
        thread_b.start()
        try:
            self.assertFalse(b_inside.wait(0.1))
        finally:
            release_a.set()
            thread_a.join(2)
            thread_b.join(2)

        self.assertFalse(thread_a.is_alive())
        self.assertFalse(thread_b.is_alive())
        self.assertTrue(b_inside.is_set())

    def test_generate_enters_gate_per_attempt(self):
        CONFIG.update({"retry_attempts": 2, "retry_delay_sec": 0,
                       "proxy": None})
        gate = mock.MagicMock()
        error = urllib.error.HTTPError("http://upstream", 500, "boom", {}, None)

        with mock.patch.object(gemini, "_build_payload", return_value="{}"), \
                mock.patch.object(gemini, "_get_url",
                                  return_value="http://upstream"), \
                mock.patch.object(gemini, "_build_headers", return_value={}), \
                mock.patch.object(gemini, "check_routing"), \
                mock.patch.object(gemini, "extract_response_text",
                                  return_value="ok"), \
                mock.patch.object(gemini, "_upstream_gate", gate), \
                mock.patch("urllib.request.urlopen",
                           side_effect=[error, _FakeResponse(b"raw")]):
            text = gemini.generate("hi", 1, 0)

        self.assertEqual(text, "ok")
        self.assertEqual(gate.call_count, 2)
        self.assertEqual(gate.return_value.__enter__.call_count, 2)

    def test_generate_stream_enters_gate(self):
        gemini.HAS_HTTPX = True
        CONFIG.update({"retry_attempts": 1, "proxy": None})
        gate = mock.MagicMock()

        with mock.patch.object(gemini, "_build_payload", return_value="{}"), \
                mock.patch.object(gemini, "_get_url",
                                  return_value="http://upstream"), \
                mock.patch.object(gemini, "_build_headers", return_value={}), \
                mock.patch.object(gemini, "check_routing"), \
                mock.patch.object(gemini, "_get_httpx_client",
                                  return_value=_FakeClient()), \
                mock.patch.object(gemini, "_upstream_gate", gate):
            chunks = list(gemini.generate_stream("hi", 1, 0))

        self.assertEqual(chunks, [])
        gate.assert_called_once()
        self.assertEqual(gate.return_value.__enter__.call_count, 1)


if __name__ == "__main__":
    unittest.main()
