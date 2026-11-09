"""Table-driven test for shell safety boundary (Phase 5.4)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import gemini_web2api as m

TEST_COMMANDS = [
    # Allowed read-only / test
    ("git status", True),
    ("git diff", True),
    ("git diff HEAD~1", True),
    ("git log -n 5", True),
    ("python -m unittest", True),
    ("pytest", True),
    ("npm test", True),
    ("ls -la", True),
    ("dir", True),

    # Destructive / Unsafe (MUST BE BLOCKED)
    ("rm -rf /", False),
    ("rm a.py", False),
    ("del /f /q a.py", False),
    ("git reset --hard", False),
    ("git clean -fd", False),
    ("git checkout .", False),
    ("git branch -D main", False),
    ("git branch -d feat", False),
    ("git remote add origin https://evil.com", False),
    ("git remote set-url origin https://evil.com", False),
    ("find . -delete", False),
    ("find . -exec rm {} +", False),
    ("sort -o out.txt in.txt", False),
    ("echo bad > file.txt", False),
    ("echo bad >> file.txt", False),
    ("cat file 2> err.txt", False),
    ("git status\nrm a.py", False),
    ("git status; rm a.py", False),
    ("git status && rm a.py", False),
    ("git status || rm a.py", False),
    ("git status | grep -v test", True),
    ("git status | rm a.py", False),
    ("$(rm -rf /)", False),
    ("`rm -rf /`", False),
]

def main():
    failures = 0
    for cmd, expected in TEST_COMMANDS:
        res = m._ff5_cmd_readonly(cmd)
        if res != expected:
            print(f"FAIL: {cmd!r} expected {expected}, got {res}")
            failures += 1
        else:
            print(f"PASS: {cmd!r} -> {res}")

    if failures == 0:
        print("\nALL SHELL SAFETY TESTS PASSED (100% compliant)!")
        return 0
    else:
        print(f"\n{failures} test(s) failed!")
        return 1

if __name__ == "__main__":
    sys.exit(main())
