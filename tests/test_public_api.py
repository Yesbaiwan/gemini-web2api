"""Test public API symbols and submodule aliases (Phase 0.6)."""
import sys
import unittest
from pathlib import Path

# Add project root to sys.path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import gemini_web2api

PUBLIC_API_SYMBOLS = [
    "CONFIG",
    "DEFAULT_CONFIG",
    "MODELS",
    "resolve_model",
    "ticket_for",
    "generate",
    "generate_stream",
    "messages_to_prompt",
    "google_contents_to_prompt",
    "parse_tool_calls",
    "parse_google_function_calls",
    "pending_tool_request",
    "detect_task_pattern",
    "task_pattern_banner",
    "GeminiHandler",
    "ThreadedServer",
    "main",
    "install",
]

SUBMODULE_ALIASES = [
    "gemini_web2api.config",
    "gemini_web2api.models",
    "gemini_web2api.tools",
    "gemini_web2api.gemini",
    "gemini_web2api.multimodal",
    "gemini_web2api.server",
]


class TestPublicAPI(unittest.TestCase):
    def test_public_symbols_exist(self):
        for sym in PUBLIC_API_SYMBOLS:
            self.assertTrue(
                hasattr(gemini_web2api, sym),
                f"Missing public API symbol: {sym}",
            )

    def test_submodule_aliases_exist_and_point_to_root(self):
        for sub in SUBMODULE_ALIASES:
            self.assertIn(sub, sys.modules, f"Missing submodule in sys.modules: {sub}")
            self.assertIs(
                sys.modules[sub],
                gemini_web2api,
                f"Submodule {sub} does not point to gemini_web2api root module",
            )


if __name__ == "__main__":
    unittest.main()
