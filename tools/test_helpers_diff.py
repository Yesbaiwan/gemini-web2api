"""Differential test for path and tool status helpers (Phase 5.1 & 5.2)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import gemini_web2api as m

def test_path_helpers():
    test_cases = [
        "server.py",
        "d:\\Project\\gemini-web2api\\server.py",
        "/abs/pkg/server.py",
        "./server.py",
        "pkg/./server.py",
        "pkg//server.py",
        "",
        None,
        "a/b/c/test.py",
        "C:\\Users\\Admin\\test.js",
    ]

    for p in test_cases:
        # Check _norm
        n = m._norm(p)
        assert "\\" not in n, f"_norm failed for {p}: {n}"

        # Check _base vs path_base
        b = m._base(p)
        if hasattr(m, "path_base"):
            assert m.path_base(p) == b

        # Check _ff12_canon vs canonical_path
        c = m._ff12_canon(p)
        if hasattr(m, "canonical_path"):
            assert m.canonical_path(p) == c

    # Check same_file
    pairs = [
        ("server.py", "/abs/pkg/server.py", True),
        ("server.py", "server.py", True),
        ("a.py", "b.py", False),
        ("pkg/server.py", "other/server.py", False),
        ("/pkg/server.py", "/pkg/server.py", True),
        ("/pkg/server.py", "/other/server.py", False),
    ]
    for a, b, expected in pairs:
        assert m._ff12_same_file(a, b) == expected, f"_ff12_same_file({a}, {b}) != {expected}"
        if hasattr(m, "is_same_path"):
            assert m.is_same_path(a, b) == expected

    # Check _path_seen
    assert m._path_seen("server.py", ["/abs/pkg/server.py"]) is True
    assert m._path_seen("server.py", ["/abs/pkg/other.py"]) is False
    assert m._path_seen("pkg/server.py", ["server.py"]) is True

    print("Path helpers differential test: PASS")

def test_tool_status_helpers():
    events = [
        {"kind": "read", "result": "def foo(): pass"},
        {"kind": "read", "result": ""},
        {"kind": "read", "result": "Error: File not found"},
        {"kind": "read", "result": "Output truncated at 2000 lines..."},
        {"kind": "act", "result": "Successfully updated 1 file"},
        {"kind": "act", "result": "Failed to apply edit"},
    ]
    for ev in events:
        s = m._ff5_status(ev)
        assert s in ("success", "partial", "failed", "unknown"), f"Unexpected status: {s}"

    print("Tool status helpers differential test: PASS")

def main():
    test_path_helpers()
    test_tool_status_helpers()
    print("ALL HELPER DIFFERENTIAL TESTS PASSED!")
    return 0

if __name__ == "__main__":
    sys.exit(main())
