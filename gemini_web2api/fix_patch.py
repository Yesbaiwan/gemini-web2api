"""fix_patch.py -- bản vá cho flow_patch.py (đặt cạnh flow_patch.py).

VẤN ĐỀ TÌM THẤY TRONG flow_patch.py / tools.py
  1. Không có flow cho "điều tra / debug test fail": model chạy tự do (đoán
     `npm test` cho project Python, glob "*", rồi bịa nguyên nhân khi chưa đọc file).
  2. _test_command đoán "npm test" chỉ vì có vài file .js dù không có package.json.
     Nay: file marker (pyproject/package.json/go.mod...) > đếm đuôi file; lệnh test
     hỏng (ENOENT, No module named...) thì tự thử lệnh khác.
  3. EXPLAIN dùng chung _answer_step với review => bị ép trả lời theo mẫu báo bug.
     Nay có câu trả lời riêng + chế độ overview (README/AGENTS + file entry).
  4. _keywords nhặt cả từ tiếng Việt không dấu ("giai", "thich", ...).
  5. _SURVEY_CMD dùng `2>/dev/null` / `find` => hỏng trên Windows.
  6. CHANGE: test còn fail sau khi sửa không được phát hiện; không đọc test liên
     quan; không đọc AGENTS/README.
  7. Gate edit không chặn lệnh write ghi đè file bằng đoạn ngắn.
  8. "Tạo file mới / gộp các file thành 1" KHÔNG được nhận là yêu cầu thay đổi code
     (không pattern nào chạy, change_demanded=False nên server không retry):
     model trả lời bằng HƯỚNG DẪN thay vì làm. Nay có pattern CREATE; riêng "gộp file"
     proxy tự sinh script gộp + chạy + kiểm tra cú pháp (không phụ thuộc việc model
     phải chép nguyên 200KB source qua một lệnh write).
  9. tools._created_files coi MỌI file trong câu là "file sẽ tạo" khi có động từ
     add/create/write... => "add retry to server.py" không bao giờ đọc server.py.
     Planner mới tự kiểm tra file có tồn tại trong listing.

THÊM MỚI
  * DEBUG       : (survey ->) chạy test -> đọc file trong traceback -> đọc module mà
                  test import (+1 tầng import) -> trả lời nguyên nhân có trích dẫn,
                  hoặc sửa nếu user bảo "sửa" (act -> test lại -> lặp có giới hạn).
  * CREATE      : tạo file mới (write có chủ đích) / gộp nhiều file thành một.
  * DIFF_REVIEW : "review các thay đổi hiện tại" -> git status/diff -> đọc file đổi.
  * CHANGE      : đọc AGENTS/CLAUDE/README, đọc test liên quan, vòng sửa-test-sửa.

CÀI ĐẶT
    python -m gemini_web2api.fix_patch --apply      # tự thêm 2 dòng vào __main__.py
  hoặc tự thêm ngay sau flow_patch.install() trong __main__.py:
    from . import fix_patch
    fix_patch.install()
KIỂM TRA
    python -m gemini_web2api.fix_patch --selftest
TẮT NHANH: {"fix_enabled": false} trong config.json.
"""
import io
import json
import os
import posixpath
import re
import sys
import uuid

from . import flow_patch as F
from . import tools as T
from .config import CONFIG
from .gemini import log

__all__ = ["install", "apply", "selftest", "plan_for", "test_outcome",
           "test_candidates", "failure_locations", "local_imports"]

_DEFAULTS = {
    "fix_enabled": True,
    "fix_debug_flow": True,
    "fix_create_flow": True,
    "fix_diff_review": True,
    "fix_read_docs_for_change": True,
    "fix_read_related_tests": True,
    "fix_max_test_attempts": 3,
    "fix_write_shrink_ratio": 0.5,
}

_ORIG_PLAN = None
_ORIG_ANALYZE = None
_ORIG_GATE = None
_ORIG_CD = None


def _c(key):
    return CONFIG.get(key, _DEFAULTS[key])


def _norm(p):
    return str(p or "").replace("\\", "/")


def _base(p):
    return _norm(p).rsplit("/", 1)[-1].lower()


_TEST_PATH_RE = re.compile(
    r"(?:^|/)(?:tests?|__tests__|specs?)/|(?:^|/)test_[^/]*$|_test\.[a-z]+$"
    r"|\.(?:test|spec)\.[a-z]+$")
_DOC_NAMES = ("agents.md", "claude.md", "readme.md")


def _is_test_path(p):
    return bool(_TEST_PATH_RE.search(_norm(p).lower()))


def _is_doc(p):
    low = _norm(p).lower()
    return _base(p) in _DOC_NAMES or low.endswith((".md", ".rst", ".txt"))


def _live(paths):
    out = []
    for p in paths:
        n = _norm(p)
        if any(s in F._IGNORE_SEG for s in n.split("/")[:-1]):
            continue
        out.append(n)
    return out


def _doc_files(listing):
    best = {}
    for f in _live(listing):
        b = _base(f)
        if b in _DOC_NAMES and (b not in best or len(f) < len(best[b])):
            best[b] = f
    guide = [best[n] for n in ("agents.md", "claude.md") if n in best]
    return guide or ([best["readme.md"]] if "readme.md" in best else [])


# --------------------------------------------------------------------------
# Lệnh test: marker file > đếm đuôi file; lệnh hỏng thì thử lệnh khác
# --------------------------------------------------------------------------

def test_candidates(names):
    """Danh sách lệnh test theo thứ tự ưu tiên ([] nếu không đoán được)."""
    names = _live(names)
    bases = {_base(n) for n in names}
    score = {"py": 0, "js": 0, "go": 0, "rs": 0}
    if "package.json" in bases:
        score["js"] += 100
    if bases & {"pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
                "pytest.ini", "tox.ini", "conftest.py"}:
        score["py"] += 100
    if "go.mod" in bases:
        score["go"] += 100
    if "cargo.toml" in bases:
        score["rs"] += 100
    for n in names:
        low = n.lower()
        if low.endswith(".py"):
            score["py"] += 1
        elif low.endswith((".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx")):
            score["js"] += 1
        elif low.endswith(".go"):
            score["go"] += 1
        elif low.endswith(".rs"):
            score["rs"] += 1
    unittest_cmd = "python -m unittest discover -s tests"
    if (any("/" in n for n in names)
            and not any("/tests/" in "/" + n.lower() for n in names)):
        unittest_cmd = "python -m unittest discover"
    pytest_cmd = "python -m pytest -q"
    py_first = bool(bases & {"pytest.ini", "conftest.py", "tox.ini"})
    cmds = {
        "py": [pytest_cmd, unittest_cmd] if py_first else [unittest_cmd, pytest_cmd],
        "js": ["npm test"],
        "go": ["go test ./..."],
        "rs": ["cargo test"],
    }
    out = []
    configured = (F._cfg("flow_test_command") or "").strip()
    if configured:
        out.append(configured)
    order = ["py", "js", "go", "rs"]
    for eco in sorted((e for e in order if score[e] > 0),
                      key=lambda e: (-score[e], order.index(e))):
        for cmd in cmds[eco]:
            if cmd not in out:
                out.append(cmd)
    return out


_BROKEN_RE = re.compile(
    r"ENOENT|Could not read package\.json|No module named|ModuleNotFoundError"
    r"|command not found|is not recognized as|No such file or directory"
    r"|cannot find the (?:path|file)|Missing script|no tests? (?:found|ran)"
    r"|Ran 0 tests|collected 0 items|usage: python|can't open file", re.I)
_RAN_RE = re.compile(
    r"\bRan\s+[1-9]\d*\s+tests?\b|\bcollected\s+[1-9]\d*\s+items?\b"
    r"|\b[1-9]\d*\s+(?:passed|failed)\b|(?m:^\W*tests\s+[1-9]\d*\b)", re.I)
_FAILED_RE = re.compile(
    r"(?m:^FAILED\b)|\bFAILED\s*\(|(?m:^FAIL:)|(?m:^ERROR:)|\b[1-9]\d*\s+failed\b"
    r"|\b[1-9]\d*\s+errors?\b|Traceback \(most recent call last\)|(?m:^not ok\b)"
    r"|Exited with code [1-9]|(?m:^\W*fail\s+[1-9])")


def test_outcome(result):
    """'pass' | 'fail' | 'broken' (lệnh không chạy được) | 'unknown'."""
    r = result or ""
    ran = bool(_RAN_RE.search(r))
    if not ran and _BROKEN_RE.search(r):
        return "broken"
    if _FAILED_RE.search(r):
        return "fail"
    if ran or re.search(r"(?m:^OK\b)|Exited with code 0", r):
        return "pass"
    return "unknown"


def _test_runs(ev):
    out = []
    for i, e in enumerate(ev):
        if e["kind"] != "exec":
            continue
        cmd = F._call_command(e["args"])
        if F._is_test_cmd(cmd):
            out.append((i, cmd, e["result"]))
    return out


def pick_test_command(names, ev):
    """Lệnh test cần chạy tiếp, hoặc None khi không còn gì để chạy."""
    cands = test_candidates(names)
    if not cands:
        return None
    last_act = max((i for i, e in enumerate(ev) if e["kind"] == "act"), default=-1)
    runs = _test_runs(ev)
    after = [r for r in runs if r[0] > last_act]
    broken_cmds = {cmd.strip() for _i, cmd, res in runs
                   if test_outcome(res) == "broken"}
    if not after:
        for _i, cmd, res in reversed(runs):
            if test_outcome(res) in ("pass", "fail"):
                return cmd          # lệnh đã chứng tỏ chạy được: chạy lại sau khi sửa
        for c in cands:
            if c.strip() not in broken_cmds:
                return c
        return None
    if test_outcome(after[-1][2]) != "broken":
        return None
    if len(after) >= int(_c("fix_max_test_attempts")):
        return None
    tried = {cmd.strip() for _i, cmd, _r in after}
    for c in cands:
        if c.strip() not in tried:
            return c
    return None


def _excerpt(text, limit=1800):
    lines = (text or "").splitlines()
    for i, ln in enumerate(lines):
        if re.search(r"Traceback|^FAIL|^ERROR|AssertionError|Error:|\bfailed\b|not ok", ln):
            lines = lines[max(0, i - 2):]
            break
    return "\n".join(lines).strip()[:limit]


# --------------------------------------------------------------------------
# Vị trí lỗi trong output test / traceback người dùng dán
# --------------------------------------------------------------------------

_SKIP_PATH = re.compile(
    r"site-packages|dist-packages|node_modules|<frozen|/lib/python|python3\d*/lib/"
    r"|/unittest/|_pytest|pluggy|importlib", re.I)
_SRC_EXTS = r"(?:py|js|mjs|cjs|ts|tsx|jsx|go|rs|java|rb|php)"


def failure_locations(text, listing=()):
    """[(path, line|None)] từ output test/traceback, chỉ file của project."""
    norm = (text or "").replace("\\", "/")
    found = []

    def add(path, line=None):
        path = (path or "").strip()
        if path and not _SKIP_PATH.search(_norm(path)):
            found.append((path, line))

    for m in re.finditer(r'File "([^"\n]+?)", line (\d+)', norm):
        add(m.group(1), int(m.group(2)))
    for m in re.finditer(
            r'(?m)^[ \t]*((?:[A-Za-z]:)?[^\s:"\'<>|()]+\.' + _SRC_EXTS + r'):(\d+)', norm):
        add(m.group(1), int(m.group(2)))
    for m in re.finditer(r'\b((?:[A-Za-z]:)?[^\s:"\'<>|()]+\.py)::', norm):
        add(m.group(1))
    for m in re.finditer(
            r'\(((?:[A-Za-z]:)?[^\s():"\'<>|]+\.(?:js|mjs|cjs|ts|tsx|jsx)):(\d+):\d+\)', norm):
        add(m.group(1), int(m.group(2)))
    if listing:
        for m in re.finditer(r'(?m)^(?:FAIL|ERROR): \S+ \(([\w.]+)\)', norm):
            parts = m.group(1).split(".")
            for k in range(len(parts), 0, -1):
                cand = "/".join(parts[:k]) + ".py"
                if any(T._path_seen(cand, [f]) for f in listing):
                    add(cand)
                    break
    return found


def _resolve_locations(locs, listing):
    out, seen = [], set()
    for path, _line in locs:
        if listing:
            hit = next((f for f in listing if T._path_seen(path, [f])), None)
        else:
            hit = _norm(path)
        if hit and hit.lower() not in seen:
            seen.add(hit.lower())
            out.append(hit)
    out.sort(key=lambda f: 0 if _is_test_path(f) else 1)
    return out


def local_imports(path, text, listing, scored=False):
    """File của project mà `text` (nội dung `path`) import.

    scored=True trả [(file, số lần dùng)] giảm dần: module được dùng nhiều là
    module đáng đọc trước khi tìm nguyên nhân.
    """
    text = text or ""
    low = {_norm(f).lower(): f for f in listing}

    def find(rel):
        rel = rel.lower()
        for k, f in low.items():
            if k == rel or k.endswith("/" + rel):
                return f
        return None

    entries = []
    for m in re.finditer(r"(?m)^[ \t]*from\s+([\w.]+)\s+import\s+\(?([^\n#]+)", text):
        base = m.group(1)
        items = []
        for n in m.group(2).split(","):
            n = n.strip().strip("()").strip()
            if not n or n == "*":
                continue
            parts = n.split(" as ")
            items.append((parts[0].strip(), (parts[1] if len(parts) > 1 else parts[0]).strip()))
        entries.append((base, [a for _n, a in items]))
        for nm, al in items:
            if re.match(r"^\w+$", nm):
                entries.append((base + nm if base.endswith(".") else base + "." + nm, [al]))
    for m in re.finditer(r"(?m)^[ \t]*import\s+([\w.]+)(?:\s+as\s+(\w+))?", text):
        entries.append((m.group(1), [m.group(2) or m.group(1).split(".")[0]]))
    out, score = [], {}
    for mod, idents in entries:
        rel = "/".join(x for x in mod.lstrip(".").split(".") if x)
        if not rel:
            continue
        f = find(rel + ".py") or find(rel + "/__init__.py")
        if (not f or _base(f) == "__init__.py"
                or _norm(f).lower() == _norm(path).lower()):
            continue
        n = sum(len(re.findall(r"\b" + re.escape(i) + r"\b", text))
                for i in idents if re.match(r"^\w+$", i))
        score[f] = max(score.get(f, 0), n)
        if f not in out:
            out.append(f)
    d = posixpath.dirname(_norm(path))
    for m in re.finditer(r"""(?:from\s+|require\()\s*['"](\.{1,2}/[^'"]+)['"]""", text):
        rel = posixpath.normpath(posixpath.join(d, m.group(1)))
        for ext in ("", ".js", ".mjs", ".ts", ".tsx", ".jsx", "/index.js"):
            f = find(rel + ext)
            if f and f not in out:
                out.append(f)
                score[f] = 1
                break
    if scored:
        return sorted(((f, score.get(f, 1)) for f in out), key=lambda x: -x[1])
    return out


def _read_texts(ev):
    return {F._call_path(e["args"]): e["result"] for e in ev
            if e["kind"] == "read" and F._read_ok(e) and F._call_path(e["args"])}


# --------------------------------------------------------------------------
# Nhận diện pattern (bổ sung cho T.detect_task_pattern)
# --------------------------------------------------------------------------

_FAIL_RE = re.compile(
    r"\b(?:fail|fails|failed|failing|failure|failures|broken|breaks|crash|crashes"
    r"|crashed|crashing|exception|traceback|regression|error|errors|loi|hong|sai"
    r"|that bai|khong chay|khong hoat dong|bi loi|panic|stack ?trace"
    r"|(?:does|do|is|are)(?:n'?t| not) work(?:ing)?|not working)\b")
_RT_RE = re.compile(
    r"\b(?:test|tests|testcase|testcases|pytest|unittest|npm|build|builds|compile"
    r"|compilation|ci|lint|run|runs|running|chay|lenh|command|startup|start|boot"
    r"|launch)\b")
_CAUSE_RE = re.compile(
    r"\b(?:why|cause|causes|root cause|investigate|investigation|debug|diagnose"
    r"|diagnosis|figure out|what(?:'s| is|s) wrong|reproduce|nguyen nhan|dieu tra"
    r"|tai sao|vi sao|tim hieu|tim ra|giai thich|xem thu|kiem tra|check|look into"
    r"|find out)\b")
_FIX_RE = re.compile(r"\b(?:fix|repair|resolve|patch|sua|sua loi|khac phuc)\b")
_WANT_CHANGE_RE = re.compile(
    r"\b(?:implement|add|introduce|refactor|upgrade|migrate|rename|them|cap nhat)\b")
_REVIEW_RE = re.compile(r"\b(?:review|audit|danh gia|soat)\b")
_TRACE_RE = re.compile(
    r"Traceback \(most recent call last\)|File \"[^\"\n]+\", line \d+"
    r"|(?m:^\s*(?:FAILED|ERROR)\s+\S+\.py)|AssertionError"
    r"|\bat\s+\S+\s+\([^)\n]*:\d+:\d+\)")
_DIFF_RE = re.compile(
    r"\b(?:git diff|diff|uncommitted|unstaged|staged|pull request|pr|commit"
    r"|commits|thay doi|changes|changeset|vua sua|vua thay doi|working tree)\b")
_REVIEWV_RE = re.compile(
    r"\b(?:review|audit|check|kiem tra|soat|danh gia|xem lai|look over|look at)\b")
_CHG_V_RE = re.compile(
    r"\b(?:fix|repair|implement|add|introduce|update|upgrade|change|modify|refactor"
    r"|remove|delete|drop|double|halve|increase|decrease|migrate|rename|sua|them"
    r"|cap nhat)\b")
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_./-]*$")

# "tạo file / viết script / gộp file": tools._CHANGE_VERB_RE không biết các động từ này.
_CREATE_V_RE = re.compile(
    r"\b(?:create|creates|generate|scaffold|merge|combine|bundle|consolidate"
    r"|concat\w*|tao|viet|gop|ghep|gom|tong hop|them"
    r"|write\s+(?:a|an|the|new|me)|make\s+(?:a|an|new)|build\s+(?:a|an|new))\b")
_CREATE_O_RE = re.compile(
    r"\b(?:file|files|script|scripts|module|class|function|folder|directory|thu muc"
    r"|tep|ham|tool|cli|command|test|tests|component|endpoint|page|readme)\b")
_EXPLAIN_V_RE = re.compile(
    r"\b(?:explain|describe|summari[sz]e|what|how|why|giai thich|mo ta|tom tat"
    r"|la gi|nhu the nao|tai sao)\b")
_MERGE_RE = re.compile(
    r"\b(?:gop|ghep|gom|tong hop|merge|combine|bundle|consolidate|concat\w*"
    r"|single file|one file|into one|thanh (?:1|mot) file|thanh file duy nhat"
    r"|(?:1|mot) file duy nhat)\b")
_DIR_WORD = r"(?:folder|directory|dir|package|th[uư] m[uụ]c)"


def _create_intent(folded, scannable, raw=""):
    f = (folded or "").lower()
    if not _CREATE_V_RE.search(f) or _EXPLAIN_V_RE.search(f):
        return False
    return bool(_CREATE_O_RE.search(f) or T._files_in(scannable or ""))


def _change_demanded(messages):
    """tools.change_demanded + yêu cầu tạo/gộp file.

    Chỉ vá hàm này (server dùng nó để cấp thêm lượt retry khi model chỉ mô tả thay
    vì làm). KHÔNG vá tools._is_change_request: hàm đó còn dùng cho
    pending_tool_request, nơi "tạo file mới" có chủ đích không đòi phải đọc file.
    """
    if _ORIG_CD(messages):
        return True
    last = None
    for m in messages or []:
        if isinstance(m, dict) and m.get("role") == "user":
            last = T._stringify_content(m.get("content", ""))
    if not last:
        return False
    scannable = T._strip_code_spans(last)
    folded = re.sub(r"\s+", " ", T._fold_diacritics(scannable)).strip()
    # Chỉ khi nêu tên file cụ thể: "tao file" trơn có thể là câu hỏi/trả lời ngắn.
    return (_create_intent(folded, scannable, last)
            and any(f.lower() not in T._NON_FILE_NAMES for f in T._files_in(scannable)))


def _dir_cands(raw):
    out = []
    for m in re.finditer(r"([\w./\\-]+)\s+" + _DIR_WORD + r"\b", raw or "", re.I):
        out.append(m.group(1))
    for m in re.finditer(_DIR_WORD + r"\s+[`'\"]?([\w./\\-]+)", raw or "", re.I):
        out.append(m.group(1))
    for m in re.finditer(r"([\w.-]+)[/\\](?:\*\*?)?(?=\s|$|[,.;)])", raw or ""):
        out.append(m.group(1))
    seen, res = set(), []
    for c in out:
        c = c.strip("/\\ ")
        if c and c.lower() not in seen:
            seen.add(c.lower())
            res.append(c)
    return res


def _named_files(ctx):
    files = [f for f in T._files_in(ctx.scannable)
             if f.lower() not in T._NON_FILE_NAMES]
    return sorted(files, key=lambda f: ctx.scannable.find(f))


def _existing(known, listing):
    """Bỏ file mã nguồn không có trong listing (file sắp tạo): khỏi đọc file không tồn tại."""
    if not listing:
        return known
    return [f for f in known
            if not f.lower().endswith(F._SRC_EXT)
            or any(T._path_seen(f, [x]) for x in listing)]


def _detect(ctx):
    """(pattern, fix) cho các pattern mới, hoặc None."""
    raw = ctx.last or ""
    folded = (ctx.folded or "").lower()
    named = _named_files(ctx)
    has_fail = bool(_FAIL_RE.search(folded))
    has_rt = bool(_RT_RE.search(folded))
    has_cause = bool(_CAUSE_RE.search(folded))
    has_fix = bool(_FIX_RE.search(folded))
    is_review = bool(_REVIEW_RE.search(folded))
    pasted = bool(_TRACE_RE.search(raw))
    wants_change = bool(_WANT_CHANGE_RE.search(folded)) and not has_fix
    if _c("fix_debug_flow") and not is_review:
        if pasted or (has_fail and has_rt and (has_cause or not named)
                      and not (wants_change and not has_cause)):
            return "DEBUG", has_fix
    if (_c("fix_diff_review") and not named and _REVIEWV_RE.search(folded)
            and _DIFF_RE.search(folded)):
        return "DIFF_REVIEW", False
    return None


_STOP2 = set(F._STOP) | set(
    "giai thich source nay hoat dong nao the xem gium voi cho minh toi bang duoc "
    "dang chay lai con vao ra den neu khi cach chi tiet tong quan phan tich kiem "
    "them sua viet tao muon can hien tai nhu nao sao bao gom nguyen nhan dieu tra "
    "tinh nang chuc phai nen thu roi cung nua van ma thi dau kia hay thanh duy "
    "nhat noi dung folder thu muc gop tat ca".split())
_STOP2 -= {"flow"}


def _keywords(text, limit=6):
    """Từ khoá: identifier/backtick trước, từ thường (>=4 ký tự) sau."""
    strong, weak = [], []
    for m in re.finditer(r"`([^`\n]{2,60})`", text or ""):
        k = re.sub(r"[()\s]", "", m.group(1)).lower()
        if k and k not in strong:
            strong.append(k)
    folded = T._fold_diacritics(re.sub(r"```.*?```", " ", text or "", flags=re.S))
    for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", folded):
        lw = w.lower()
        if lw in _STOP2 or lw in strong or lw in weak:
            continue
        if "_" in w or (any(c.isupper() for c in w[1:]) and any(c.islower() for c in w)):
            strong.append(lw)
        elif len(lw) >= 4:
            weak.append(lw)
    return (strong + weak)[:limit]


_QUESTION_RE = re.compile(
    r"\b(how|where|why|what|which|explain|does|show me|giai thich|o dau|nhu the nao"
    r"|hoat dong|la gi|tai sao|dinh nghia|tim hieu|mo ta|tom tat)\b")
_PROJECT_RE = re.compile(
    r"\b(this|the|our|nay|cua)\s+(project|repo|repository|codebase|code|proxy|server"
    r"|module|function|class|file|source|flow|pipeline|system|app)\b"
    r"|\b(du an|repo|codebase|ma nguon|source|source code|trong code|ham|class|module"
    r"|kien truc|architecture|luong|flow|pipeline)\b")


def _is_code_question(text):
    scannable = T._strip_code_spans(text)
    folded = re.sub(r"\s+", " ", T._fold_diacritics(scannable.lower())).strip()
    if not _QUESTION_RE.search(folded):
        return False
    has_ident = bool(re.search(r"`[^`\n]{2,}`", text or ""))
    has_file = any(f for f in T._files_in(scannable)
                   if f.lower() not in T._NON_FILE_NAMES)
    return has_ident or has_file or bool(_PROJECT_RE.search(folded))


def analyze(messages):
    ctx = _ORIG_ANALYZE(messages)
    if ctx is None:
        return None
    ctx.fix = False
    ctx.create_merge = False
    ctx.target = ""
    ctx.dir_cands = []
    try:
        if F._clarified(messages, ctx.idx):
            return ctx                      # người dùng đã chọn pattern thủ công
        det = _detect(ctx)
        if det:
            ctx.pattern, ctx.fix = det
        elif ctx.pattern in (None, "CHANGE") and _c("fix_create_flow") \
                and _create_intent(ctx.folded, ctx.scannable, ctx.last):
            ctx.pattern = "CREATE"
            files = _named_files(ctx)
            ctx.target = files[0] if files else ""
            ctx.create_merge = bool(_MERGE_RE.search((ctx.folded or "").lower()))
            ctx.dir_cands = _dir_cands(ctx.last)
        elif not ctx.pattern:
            folded = (ctx.folded or "").lower()
            idents = [s for s in re.findall(r"`([^`\n]+)`", ctx.last or "")
                      if _IDENT_RE.match(s.strip())]
            if _CHG_V_RE.search(folded) and (idents or _named_files(ctx)):
                ctx.pattern = "CHANGE"
    except Exception as e:
        log(f"[fix] analyze error: {e}")
    return ctx


# --------------------------------------------------------------------------
# Step builders
# --------------------------------------------------------------------------

def _custom_answer(pat, instruction, index, total):
    st = F.Step(pat, "answer", index, total, (), instruction)
    st.skipped = []
    return st


def _read_step(pat, tm, files, index, total, drive=True, instruction=None):
    calls = F._read_calls(tm, files)
    return F.Step(pat, "read", index, total, {"read"},
                  instruction or ("Read these files fully before answering: "
                                  + ", ".join(files) + "."),
                  calls, drive, tm.names({"read"}))


def _survey_step(pat, tm, ev, index, total):
    n = F._survey_count(ev)
    glob_call = (tm.make_call("survey", pattern=(F._cfg("flow_survey_pattern")
                                                 if n == 0 else "**/*"))
                 if n < 2 else None)
    exec_call = tm.make_call("exec", command="git ls-files",
                             description="List project files")
    call = glob_call or exec_call
    kinds = {"survey", "search", "exec"}
    if not call and not tm.names(kinds):
        return None
    return F.Step(pat, "survey", index, total, kinds,
                  "List the project files before anything else.",
                  [call] if call else None, True, tm.names(kinds))


def _tests_step(pat, tm, cmd, index, total,
                instruction="Run the unit tests."):
    call = tm.make_call("exec", command=cmd, description="Run unit tests")
    if not call:
        return None
    return F.Step(pat, "tests", index, total, {"exec"}, instruction, [call],
                  True, tm.names({"exec"}))


def _code_reads(read_set):
    return [p for p in read_set if not _is_doc(p)]


def _matching_tests(read_set, listing):
    tests = [f for f in listing if _is_test_path(f)
             and f.lower().endswith(F._SRC_EXT)]
    out = []
    for p in read_set:
        if _is_test_path(p) or _is_doc(p) or not p.endswith(F._SRC_EXT):
            continue
        stem = posixpath.splitext(_base(p))[0]
        if len(stem) < 3 or stem == "__init__":
            continue
        for t in tests:
            if stem in _base(t) and t not in out:
                out.append(t)
    return [t for t in out if not T._path_seen(t, read_set)]


# --------------------------------------------------------------------------
# CHANGE / CREATE (không merge)
# --------------------------------------------------------------------------

_DO_NOT_DESCRIBE = (" Never answer with instructions for the user to do it "
                    "themselves: make the tool call.")
_CREATE_NOTE = ("\nThe file(s) to create do not exist yet: create each one with "
                "the write tool and the COMPLETE file content.")


def _change_tail(pat, ctx, ev, tm, names_all, total, extra=""):
    acts = [e for e in ev if e["kind"] == "act"]
    max_acts = int(F._cfg("flow_max_acts"))
    if len(acts) >= max_acts and (not F._act_ok(acts[-1])
                                  or F._tests_ran_after_act(ev)):
        st = F._answer_step("CHANGE", ev, total, total, final=True)
        if st:
            st.pattern = pat
        return st
    ok_acts = [e for e in acts if F._act_ok(e)]
    if tm.first("act") and (not ok_acts or (acts and not F._act_ok(acts[-1]))):
        return F.Step(pat, "act", 3, total, {"act"},
                      "Make the change now with ONE edit/write tool_call per "
                      "file, copying old text exactly from the file you read "
                      "(no line-number prefixes)." + _DO_NOT_DESCRIBE
                      + F._failed_act_hint(ev) + extra, None, False,
                      tm.names({"act"}))
    edited_src = any(e["kind"] == "act" and F._act_ok(e)
                     and (F._call_path(e["args"]) or "").lower().endswith(F._SRC_EXT)
                     for e in ev)
    if edited_src and tm.first("exec") and not T._NO_TEST_RE.search(ctx.folded):
        cmd = pick_test_command(names_all, ev)
        if cmd:
            s = _tests_step(pat, tm, cmd, 4, total)
            if s:
                return s
        runs = _test_runs(ev)
        last_act = max((i for i, e in enumerate(ev) if e["kind"] == "act"), default=-1)
        after = [r for r in runs if r[0] > last_act]
        if (after and test_outcome(after[-1][2]) == "fail" and tm.first("act")
                and len(acts) < max_acts):
            return F.Step(pat, "act", 3, total, {"act"},
                          "The tests still FAIL after your edit. Fix the cause "
                          "with ONE more edit tool_call (copy old text exactly "
                          "from the file as it is NOW; re-read it first if you "
                          "are unsure). Failure:\n" + _excerpt(after[-1][2], 1500),
                          None, False, tm.names({"act"}))
    st = F._answer_step("CHANGE", ev, total, total)
    if st:
        st.pattern = pat
    return st


def _plan_change(ctx, ev, tm, pat="CHANGE", total=5):
    listing = F._listing_files(ev)
    read_set = F._read_set(ev)
    batch = max(1, int(F._cfg("flow_read_batch")))
    named = _named_files(ctx)
    known = _existing([F._resolve(f, listing) for f in named], listing)
    paths = [x for x in (F._call_path(e["args"]) for e in ev) if x]
    names_all = listing + named + paths
    can_read = bool(tm.first("read"))

    if (not known or pat == "CREATE") and not F._surveyed(ev):
        s = _survey_step(pat, tm, ev, 1, total)
        if s:
            return s
    code_read = _code_reads(read_set)
    if known and pat != "CREATE":
        unread = [f for f in known if not T._path_seen(f, read_set)]
        if unread and can_read:
            return _read_step(pat, tm, unread[:batch], 2, total)
    elif not code_read and can_read:
        if _c("fix_read_docs_for_change") and listing:
            docs = [d for d in _doc_files(listing) if not T._path_seen(d, read_set)]
            if docs:
                return _read_step(
                    pat, tm, docs[:2], 2, total,
                    instruction="Read the project guidelines first (conventions, "
                                "where things live): " + ", ".join(docs[:2]) + ".")
        kws = _keywords(ctx.request)
        if (kws and tm.first("search")
                and not any(e["kind"] == "search" for e in ev)):
            scall = tm.make_call("search", pattern="(?i)" + "|".join(
                re.escape(k) for k in kws[:3]))
            if scall:
                skinds = {"survey", "search", "exec"}
                return F.Step(pat, "search", 2, total, skinds,
                              "Locate the code to change with grep first.",
                              [scall], True, tm.names(skinds))
        core = F._core_sources(listing, False, 200)
        ranked = F._rank(core, kws, F._grep_hits(ev, core))[:2]
        if ranked:
            return _read_step(pat, tm, ranked, 2, total)
        if core:
            return _read_step(pat, tm, core[:2], 2, total, drive=False)
        if pat != "CREATE":
            return F.Step(pat, "read", 2, total, {"read"},
                          "Read the file(s) you are going to change.", None, False,
                          tm.names({"read"}))
    if can_read and listing and _c("fix_read_related_tests") and pat != "CREATE":
        tests = _matching_tests(read_set, listing)
        if tests:
            return _read_step(
                pat, tm, tests[:2], 2, total,
                instruction="Read the related test file(s) so the change can be "
                            "verified or covered by a test: " + ", ".join(tests[:2]) + ".")
    return _change_tail(pat, ctx, ev, tm, names_all, total,
                        extra=_CREATE_NOTE if pat == "CREATE" else "")


# --------------------------------------------------------------------------
# CREATE: gộp nhiều file thành một (proxy tự sinh script, model không phải chép source)
# --------------------------------------------------------------------------

_MERGE_TMPL = r'''# -*- coding: utf-8 -*-
# Temporary helper generated by the proxy; it deletes itself when done.
import io
import os
import re

SRC = @@SRC@@
OUT = @@OUT@@
C = @@C@@
IS_PY = @@PY@@


def stem(p):
    return os.path.splitext(os.path.basename(p))[0]


def read(p):
    with io.open(p, "r", encoding="utf-8") as fh:
        return fh.read()


names = {stem(p): p for p in SRC}
texts = {p: read(p) for p in SRC}


def deps(text):
    out = []
    for m in re.finditer(r"(?m)^[ \t]*from[ \t]+\.+(\w*)[ \t]+import[ \t]+([^\n#]+)", text):
        if m.group(1):
            out.append(m.group(1))
        else:
            for t in m.group(2).strip("() ").split(","):
                out.append(t.split(" as ")[0].strip())
    return [d for d in out if d in names]


order, seen = [], set()


def visit(p):
    if p in seen:
        return
    seen.add(p)
    for d in deps(texts[p]):
        visit(names[d])
    order.append(p)


rest = sorted(p for p in SRC if stem(p) not in ("__init__", "__main__"))
firsts = [p for p in SRC if stem(p) == "__init__"]
lasts = [p for p in SRC if stem(p) == "__main__"]
for p in firsts + rest + lasts:
    visit(p)

bar = C + " " + "=" * 60
parts = []
if IS_PY:
    parts.append("# -*- coding: utf-8 -*-\n\"\"\"Combined module generated from %d files.\"\"\"\n" % len(order))
for p in order:
    parts.append("\n" + bar + "\n" + C + " FILE: " + os.path.basename(p) + "\n" + bar
                 + "\n\n" + texts[p].rstrip() + "\n")
data = "".join(parts)
with io.open(OUT, "w", encoding="utf-8", newline="\n") as fh:
    fh.write(data)
print("merged %d files -> %s (%d bytes)" % (len(order), OUT, len(data.encode("utf-8"))))
print("order: " + ", ".join(os.path.basename(p) for p in order))
try:
    os.remove(__file__)
except OSError:
    pass
'''

_SLASH_COMMENT_EXT = (".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs", ".go", ".rs",
                      ".java", ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".kt",
                      ".swift", ".php")
_MERGE_SCRIPT = "_merge_tmp.py"


def merge_script(srcs, out_path, ext):
    comment = "//" if ext in _SLASH_COMMENT_EXT else "#"
    return (_MERGE_TMPL.replace("@@SRC@@", json.dumps(list(srcs)))
            .replace("@@OUT@@", json.dumps(out_path))
            .replace("@@C@@", json.dumps(comment))
            .replace("@@PY@@", "True" if ext == ".py" else "False"))


def _merge_sources(ctx, listing):
    """(files, prefix): file cùng đuôi với target nằm trong thư mục user nêu."""
    target = _norm(getattr(ctx, "target", "") or "")
    ext = (posixpath.splitext(target)[1] or ".py").lower()
    live = [f for f in _live(listing) if f.lower().endswith(ext)]
    tbase = posixpath.basename(target).lower()
    for cand in getattr(ctx, "dir_cands", []) or []:
        dl = _norm(cand).strip("/").split("/")[-1].lower()
        if not dl:
            continue
        groups = {}
        for f in live:
            segs = f.split("/")
            for i, s in enumerate(segs[:-1]):
                if s.lower() == dl:
                    groups.setdefault("/".join(segs[:i + 1]), []).append(f)
        if not groups:
            continue
        prefix = max(groups, key=lambda k: (
            sum(1 for f in groups[k] if posixpath.dirname(f) == k), len(k)))
        files = sorted({f for f in groups[prefix]
                        if not _is_test_path(f) and _base(f) != tbase
                        and _base(f) != _MERGE_SCRIPT})
        if len(files) >= 2:
            return files, prefix
    return [], ""


def _write_tool(tm):
    for n in tm.by_kind.get("act", []):
        if re.search(r"write|create|save", n, re.I):
            return n
    return None


def _write_call(tm, path, content):
    name = _write_tool(tm)
    if not name:
        return None
    props = tm.schemas.get(name) or set()
    pk = F._pick(props, F._PATH_KEYS) or (None if props else "filePath")
    ck = F._pick(props, ["content", "contents", "text", "file_text", "filetext",
                         "data"]) or (None if props else "content")
    if not pk or not ck:
        return None
    args = {pk: path, ck: content}
    for req in tm.required.get(name, ()):
        if req not in args:
            return None
    return {"id": f"call_{uuid.uuid4().hex[:8]}", "type": "function",
            "function": {"name": name,
                         "arguments": json.dumps(args, ensure_ascii=False)}}


def _plan_create(ctx, ev, tm):
    pat, total = "CREATE", 5
    fallback = lambda: _plan_change(ctx, ev, tm, pat="CREATE")  # noqa: E731
    if not getattr(ctx, "create_merge", False) or not ctx.target:
        return fallback()
    if not _write_tool(tm) or not tm.first("exec"):
        return fallback()
    if not F._surveyed(ev):
        s = _survey_step(pat, tm, ev, 1, total)
        if s:
            return s
    listing = F._listing_files(ev)
    srcs, prefix = _merge_sources(ctx, listing)
    if not srcs:
        return fallback()
    target = _norm(ctx.target)
    root = posixpath.dirname(prefix)
    out_path = target if "/" in target else (posixpath.join(root, target) if root else target)
    ext = (posixpath.splitext(target)[1] or ".py").lower()
    names = ", ".join(_base(f) for f in srcs)

    def run_events(word):
        return [e for e in ev if e["kind"] == "exec" and word in F._call_command(e["args"])]

    wrote = [e for e in ev if e["kind"] == "act"
             and _MERGE_SCRIPT in (F._call_path(e["args"]) or "")]
    if not wrote:
        call = _write_call(tm, _MERGE_SCRIPT, merge_script(srcs, out_path, ext))
        if not call:
            return fallback()
        return F.Step(pat, "merge", 2, total, {"act"},
                      "Create the helper script that merges " + names + ".",
                      [call], True, tm.names({"act"}))
    if not F._act_ok(wrote[-1]):
        return _custom_answer(
            pat, "Writing the helper script failed: " + _excerpt(wrote[-1]["result"], 400)
                 + "\nReport this exact error to the user in their language. Do NOT "
                   "tell them to do the merge by hand.", 2, total)
    ran = run_events(_MERGE_SCRIPT)
    if not ran:
        call = tm.make_call("exec", command="python " + _MERGE_SCRIPT,
                            description="Merge the files")
        if not call:
            return fallback()
        return F.Step(pat, "merge", 3, total, {"exec"}, "Run the merge script.",
                      [call], True, tm.names({"exec"}))
    res = ran[-1]["result"] or ""
    if "merged" not in res.lower() or "traceback" in res.lower():
        return _custom_answer(
            pat, "The merge script FAILED. Report the exact error to the user in "
                 "their language and what you would change; do not claim the "
                 "file was created.\n" + _excerpt(res, 1500), 3, total)
    if ext == ".py":
        chk = run_events("py_compile")
        if not chk:
            call = tm.make_call("exec", command='python -m py_compile "' + out_path + '"',
                                description="Check syntax of the merged file")
            if call:
                return F.Step(pat, "verify", 4, total, {"exec"},
                              "Check that the merged file compiles.", [call], True,
                              tm.names({"exec"}))
        else:
            cres = chk[-1]["result"] or ""
            syntax = ("FAILED: " + _excerpt(cres, 400)) if re.search(
                r"Error|Traceback", cres) else "passed (python -m py_compile)"
            return _custom_answer(pat, _create_answer(out_path, srcs, names, syntax), 5, total)
    return _custom_answer(pat, _create_answer(out_path, srcs, names, "not checked"), 5, total)


def _create_answer(out_path, srcs, names, syntax):
    return ("DONE -- the file already exists. Reply in the user's language: `"
            + out_path + "` was created by merging " + str(len(srcs)) + " files ("
            + names + ") in dependency order, each under a '# FILE:' header. Syntax "
            "check: " + syntax + ". Be honest about the limit: the sources are "
            "concatenated verbatim, so intra-package relative imports (`from . "
            "import x`) are NOT rewritten and the merged module will not run "
            "standalone until imports/aliases are unified -- offer to do that as a "
            "follow-up. Do NOT give the user a script or steps to do it themselves: "
            "it is already done, and the temporary helper script deleted itself.")


# --------------------------------------------------------------------------
# DEBUG: reproduce -> read what the failure points at -> explain (or fix)
# --------------------------------------------------------------------------

def _debug_answer_text(read_files, excerpt):
    read = ", ".join(sorted(read_files)[:20]) or "none"
    return ("Evidence collected (successfully read: " + read + "). Answer NOW in "
            "plain text with NO tool_call, in the user's language. Structure: "
            "(1) which test/command failed and the exact error line; (2) ROOT "
            "CAUSE: quote the exact lines (`file:line -- verbatim code`) from the "
            "files you read and explain the mechanism step by step; (3) the "
            "minimal fix as a small diff -- say clearly it is NOT applied yet; "
            "(4) what you could not verify. Discuss only files you read; never "
            "guess a symbol you have not seen."
            + (("\nFailure output:\n" + excerpt) if excerpt else ""))


def _hop_targets(ev, listing, read_set, excerpt):
    """Module cần đọc tiếp: (1) module mà test import, (2) import của file đã đọc."""
    texts = _read_texts(ev)
    stage1 = []
    for p_, body in texts.items():
        if _is_test_path(p_):
            for f, _s in local_imports(p_, body, listing, scored=True):
                if (not _is_test_path(f) and f not in stage1
                        and not T._path_seen(f, read_set)):
                    stage1.append(f)
    if stage1:
        return stage1[:3]
    nontest = [p for p in texts if not _is_doc(p) and not _is_test_path(p)]
    if len(nontest) >= 5:
        return []
    cand = {}
    for p_ in nontest:
        for f, s in local_imports(p_, texts[p_], listing, scored=True):
            if _is_test_path(f) or T._path_seen(f, read_set):
                continue
            cand[f] = max(cand.get(f, 0), s)
    low = (excerpt or "").lower()
    ranked = sorted(cand, key=lambda f: (
        0 if posixpath.splitext(_base(f))[0] in low else 1, -cand[f]))
    return ranked[:2]


def _plan_debug(ctx, ev, tm):
    pat, total = "DEBUG", 6
    listing = F._listing_files(ev)
    read_set = F._read_set(ev)
    batch = max(1, int(F._cfg("flow_read_batch")))
    paths = [x for x in (F._call_path(e["args"]) for e in ev) if x]
    names_all = listing + paths
    can_read = bool(tm.first("read"))
    pasted = ctx.last if _TRACE_RE.search(ctx.last or "") else ""
    fix = bool(getattr(ctx, "fix", False))

    if not pasted and not listing and F._survey_count(ev) < 2:
        s = _survey_step(pat, tm, ev, 1, total)
        if s:
            return s
    if fix and any(e["kind"] == "act" for e in ev):
        return _change_tail(pat, ctx, ev, tm, names_all, total)

    runs = _test_runs(ev)
    if not pasted and tm.first("exec"):
        cmd = pick_test_command(names_all, ev)
        if cmd:
            s = _tests_step(pat, tm, cmd, 2, total,
                            "Run the unit tests to reproduce the failure.")
            if s:
                return s
    outcome = test_outcome(runs[-1][2]) if runs else None
    result_text = (pasted + "\n" + (runs[-1][2] if runs else "")).strip()
    excerpt = _excerpt(result_text)

    if not pasted:
        if outcome is None:
            return _custom_answer(
                pat, "You could not run any test command here. Do NOT guess a "
                     "cause. Tell the user what you tried and ask them to paste "
                     "the failing output or name the test.", 2, total)
        if outcome == "broken":
            return _custom_answer(
                pat, "The test command itself failed to run (not a test "
                     "failure). Report the exact error and ask the user for the "
                     "correct test command. Do NOT guess a cause.\n" + excerpt,
                2, total)
        if outcome in ("pass", "unknown"):
            if fix:
                return _plan_change(ctx, ev, tm, pat="CHANGE")
            return _custom_answer(
                pat, "The test run showed NO failure (outcome: " + outcome + "). "
                     "Say plainly that you could not reproduce it, show the "
                     "summary line of the run, and ask for the exact failing "
                     "command or log. Do NOT invent a cause.\n" + excerpt,
                2, total)

    located = _resolve_locations(failure_locations(result_text, listing), listing)
    unread = [f for f in located if not T._path_seen(f, read_set)]
    if unread and can_read:
        return _read_step(pat, tm, unread[:batch], 3, total,
                          instruction="Read the file(s) the failure points at: "
                                      + ", ".join(unread[:batch]) + ".")
    if can_read and listing:
        hop = _hop_targets(ev, listing, read_set, excerpt)
        if hop:
            return _read_step(
                pat, tm, hop, 4, total,
                instruction="The failure runs through these project modules; "
                            "read them to find the root cause: " + ", ".join(hop) + ".")
    if fix:
        return _change_tail(pat, ctx, ev, tm, names_all, total,
                            extra="\nFailure to fix:\n" + excerpt)
    return _custom_answer(pat, _debug_answer_text(
        [p for p in read_set if not _is_doc(p)], excerpt), 5, total)


# --------------------------------------------------------------------------
# DIFF_REVIEW
# --------------------------------------------------------------------------

_BINARY_EXT = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".pyc", ".lock", ".log",
               ".zip", ".pdf", ".woff", ".woff2")


def _changed_files(status, diff):
    files = []
    for ln in (status or "").splitlines():
        m = re.match(r"^\s*([MADRCU?!]{1,2})\s+(.+?)\s*$", ln)
        if m and "D" not in m.group(1):
            p = m.group(2).split(" -> ")[-1].strip('"')
            if p not in files:
                files.append(p)
    for m in re.finditer(r"(?m)^\+\+\+ b/(.+)$", diff or ""):
        if m.group(1) not in files:
            files.append(m.group(1))
    return [f for f in _live(files)
            if not f.endswith("/") and not f.lower().endswith(_BINARY_EXT)]


def _plan_diff(ctx, ev, tm):
    pat, total = "DIFF_REVIEW", 4
    if not tm.first("exec"):
        return None

    def find(word):
        return next((e for e in ev if e["kind"] == "exec"
                     and "git" in F._call_command(e["args"])
                     and word in F._call_command(e["args"])), None)

    def git_step(cmd, index, why):
        call = tm.make_call("exec", command=cmd, description=why)
        if not call:
            return None
        return F.Step(pat, "git", index, total, {"exec"}, why + ".", [call],
                      True, tm.names({"exec"}))

    status_ev = find("status")
    if status_ev is None:
        return git_step("git status --short", 1, "List the changed files")
    diff_ev = find("diff")
    if diff_ev is None:
        return git_step("git --no-pager diff HEAD", 2, "Show the changes")
    if "not a git repository" in (status_ev["result"] + diff_ev["result"]).lower():
        return _custom_answer(pat, "This directory is not a git repository, so "
                                   "there is no diff to review. Say so and ask "
                                   "which files to review.", 3, total)
    changed = _changed_files(status_ev["result"], diff_ev["result"])
    read_set = F._read_set(ev)
    unread = [f for f in changed if not T._path_seen(f, read_set)][:6]
    if unread and tm.first("read"):
        return _read_step(pat, tm, unread, 3, total,
                          instruction="Read the changed files for context: "
                                      + ", ".join(unread) + ".")
    if not changed and not (diff_ev["result"] or "").strip():
        return _custom_answer(pat, "There are no uncommitted changes. Say so in "
                                   "one sentence.", 4, total)
    return _custom_answer(
        pat, "Review ONLY the changes shown by the diff (the changed files you "
             "read are context). Report each problem as `file:line -- verbatim "
             "quote -- why it is a bug or risk`. If you find none, write exactly "
             "'no problem found in the diff'. Do not claim the tests pass: they "
             "were not run.", 4, total)


# --------------------------------------------------------------------------
# EXPLAIN
# --------------------------------------------------------------------------

def _explain_text(read_files):
    if not read_files:
        return ("NO file was successfully read. Say that plainly and ask the "
                "user for the project path; do NOT describe code you have not seen.")
    return ("Evidence collected (read: " + ", ".join(sorted(read_files)[:20]) +
            "). Answer NOW in plain text with NO tool_call, in the user's "
            "language. Start with a 2-3 sentence overview, then walk through the "
            "flow step by step, naming the REAL functions/classes and citing "
            "`file:line` with SHORT verbatim snippets from the files you read. "
            "Never invent symbols; list important files you did not read as 'not "
            "read'. This is an explanation, NOT a bug report.")


def _plan_explain(ctx, ev, tm):
    pat, total = "EXPLAIN", 4
    listing = F._listing_files(ev)
    read_set = F._read_set(ev)
    can_read = bool(tm.first("read"))
    named = _named_files(ctx)
    known = [F._resolve(f, listing) for f in named]
    if known and can_read:
        unread = [f for f in known if not T._path_seen(f, read_set)]
        if unread:
            return _read_step(pat, tm, unread[:6], 1, total)
    code_read = _code_reads(read_set)
    kws = _keywords(ctx.request)
    if not F._surveyed(ev):
        call = None
        if kws and tm.first("search"):
            call = tm.make_call("search", pattern="(?i)" + "|".join(
                re.escape(k) for k in kws[:3]))
        if call:
            kinds = {"survey", "search", "exec"}
            return F.Step(pat, "search", 1, total, kinds,
                          "Locate the relevant code with grep first.", [call],
                          True, tm.names(kinds))
        s = _survey_step(pat, tm, ev, 1, total)
        if s:
            return s
    hits = {}
    for e in ev:
        if e["kind"] == "search":
            body = e["result"].replace("\\", "/")
            for f in T._files_in(body):
                hits[f] = hits.get(f, 0) + body.count(f)
    if not code_read and not known and can_read:
        if hits:
            pool = list(hits)
            pool = F._core_sources(pool, True, 400) or pool
            ranked = F._rank(pool, kws, hits)[:2] or pool[:1]
            if ranked:
                return _read_step(pat, tm, ranked, 2, total)
        if not listing and F._survey_count(ev) < 2:
            s = _survey_step(pat, tm, ev, 2, total)
            if s:
                return s
        docs = [d for d in _doc_files(listing) if not T._path_seen(d, read_set)]
        if docs:
            return _read_step(pat, tm, docs[:2], 2, total,
                              instruction="Read the project docs first: "
                                          + ", ".join(docs[:2]) + ".")
        entry = [f for f in F._core_sources(listing, False, 200)
                 if not T._path_seen(f, read_set)][:3]
        if entry:
            return _read_step(pat, tm, entry, 3, total,
                              instruction="Read the main source files: "
                                          + ", ".join(entry) + ".")
    if not ev:
        return None
    return _custom_answer(pat, _explain_text(sorted(read_set)), 4, total)


# --------------------------------------------------------------------------
# Dispatcher + gate + install
# --------------------------------------------------------------------------

def _plan(messages, tm, ctx):
    ctx = ctx or F._analyze(messages)
    if ctx is None or not ctx.pattern:
        return None
    ev = F._answered_calls(messages)
    pat = ctx.pattern
    if pat == "DEBUG":
        return _plan_debug(ctx, ev, tm)
    if pat == "DIFF_REVIEW":
        return _plan_diff(ctx, ev, tm)
    if pat == "CREATE":
        return _plan_create(ctx, ev, tm)
    if pat == "CHANGE":
        return _plan_change(ctx, ev, tm)
    if pat == "EXPLAIN":
        return _plan_explain(ctx, ev, tm)
    return _ORIG_PLAN(messages, tm, ctx)


def plan_for(messages, tm, ctx=None):
    try:
        return _plan(messages, tm, ctx)
    except Exception as e:      # never let the planner break a request
        log(f"[fix] planner error, using the original planner: {e}")
        return _ORIG_PLAN(messages, tm, ctx)


_CONTENT_KEYS = ("content", "contents", "text", "fileText", "file_text")


def _gate(calls, messages):
    out, problems = _ORIG_GATE(calls, messages)
    try:
        ratio = float(_c("fix_write_shrink_ratio"))
        texts = {}
        for e in F._answered_calls(messages):
            if e["kind"] == "read" and F._read_ok(e):
                p = F._call_path(e["args"])
                if p:
                    texts[p] = F._clean_file_text(e["result"])
        for tc in out:
            fn = tc.get("function") or {}
            if F.tool_kind(fn.get("name")) != "act":
                continue
            args = T._tool_arguments_dict(fn)
            if any(isinstance(args.get(k), str) and args[k] for k in F._OLD_KEYS):
                continue                      # an edit: already checked
            new = next((args[k] for k in _CONTENT_KEYS
                        if isinstance(args.get(k), str)), None)
            path = F._call_path(args)
            if new is None or not path:
                continue
            old = next((t for p, t in texts.items() if T._path_seen(path, [p])), None)
            if old is not None and len(old.strip()) >= 400 \
                    and len(new) < ratio * len(old):
                problems.append(
                    f"{path}: this write would replace a {len(old)}-char file "
                    f"you read with only {len(new)} chars -- use the edit tool "
                    "(oldString/newString) to change part of a file, or write "
                    "the COMPLETE new file")
    except Exception as e:
        log(f"[fix] write gate error: {e}")
    return out, problems


def install():
    """Monkeypatch flow_patch/tools. Idempotent; gọi trước hoặc sau flow_patch.install()."""
    global _ORIG_PLAN, _ORIG_ANALYZE, _ORIG_GATE, _ORIG_CD
    for k, v in _DEFAULTS.items():
        CONFIG.setdefault(k, v)
    if getattr(F, "_fix_patched", False) or not CONFIG.get("fix_enabled", True):
        return
    _ORIG_PLAN, _ORIG_ANALYZE, _ORIG_GATE = F.plan_for, F._analyze, F._gate_act_calls
    _ORIG_CD = T.change_demanded
    F.plan_for = plan_for
    F._analyze = analyze
    F._gate_act_calls = _gate
    F._keywords = _keywords
    F._is_code_question = _is_code_question
    F._SURVEY_CMD = "git ls-files"
    F._test_command = lambda listing: (test_candidates(list(listing or [])) or [None])[0]
    # server.py làm `from .tools import change_demanded` nên phải vá cả bản trong server.
    T.change_demanded = _change_demanded
    try:
        from . import server as _S
        if getattr(_S, "change_demanded", None) is _ORIG_CD:
            _S.change_demanded = _change_demanded
    except Exception as e:
        log(f"[fix] could not patch server.change_demanded: {e}")
    F._fix_patched = True
    log("[fix] fix_patch installed")


def apply(main_path=None):
    """Thêm 2 dòng cài đặt vào __main__.py (có backup .bak, idempotent)."""
    path = main_path or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "__main__.py")
    with io.open(path, "r", encoding="utf-8", newline="") as fh:
        src = fh.read()
    if "fix_patch" in src:
        print("__main__.py already contains fix_patch: nothing to do")
        return 0
    i = src.find("flow_patch.install()")
    if i == -1:
        print("Anchor `flow_patch.install()` not found. Add manually after it:\n"
              "    from . import fix_patch\n    fix_patch.install()")
        return 1
    eol = "\r\n" if "\r\n" in src else "\n"
    line_start = src.rfind("\n", 0, i) + 1
    indent = src[line_start:i]
    if indent.strip():
        indent = ""
    line_end = src.find("\n", i)
    if line_end == -1:
        src += eol
        line_end = len(src) - 1
    insert = f"{indent}from . import fix_patch{eol}{indent}fix_patch.install(){eol}"
    new = src[:line_end + 1] + insert + src[line_end + 1:]
    with io.open(path + ".bak", "w", encoding="utf-8", newline="") as fh:
        fh.write(src)
    with io.open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(new)
    print(f"Patched {path} (backup: {path}.bak)")
    return 0


# --------------------------------------------------------------------------
# Self-test: chạy các kịch bản thực tế qua planner thật
# --------------------------------------------------------------------------

def selftest():
    install()

    def call(i, name, args, result):
        return [
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": f"c{i}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}]},
            {"role": "tool", "tool_call_id": f"c{i}", "content": result},
        ]

    props = {"read": {"filePath"}, "edit": {"filePath", "oldString", "newString"},
             "write": {"filePath", "content"},
             "bash": {"command", "description"}, "glob": {"pattern"},
             "grep": {"pattern"}}
    req = {"read": {"filePath"}, "bash": {"command"},
           "write": {"filePath", "content"},
           "edit": {"filePath", "oldString", "newString"}}

    def tm():
        return F.ToolMap(set(props), props, req)

    results = []

    def check(label, cond, detail=""):
        results.append(bool(cond))
        print(("PASS  " if cond else "FAIL  ") + label + ("" if cond else f"  -> {detail}"))

    def step(msgs):
        return F.plan_for(msgs, tm())

    def cmd_of(st):
        return json.loads(st.calls[0]["function"]["arguments"]).get("command", "")

    # --- A: điều tra test fail (ca thật trong log) ---------------------------
    root = "d:\\Project\\proj\\"
    listing = "\n".join(root + p for p in [
        "pyproject.toml", "cloudflare\\worker.js", "tests\\worker_routing_test.js",
        "tests\\test_flow_patch_change.py", "gemini_web2api\\flow_patch.py",
        "gemini_web2api\\tools.py", "gemini_web2api\\config.py"])
    msgs = [{"role": "user",
             "content": "Hiện tại source đang chạy fail 1 test, điều tra nguyên nhân"}]
    st = step(msgs)
    check("A1 debug: survey first", st and st.pattern == "DEBUG" and st.name == "survey",
          st and (st.pattern, st.name))
    msgs += call(0, "glob", {"pattern": "**/*"}, listing)
    st = step(msgs)
    check("A2 debug: python test cmd (not npm)",
          st and st.name == "tests" and "npm" not in cmd_of(st), st and st.name)
    msgs += call(1, "bash", {"command": "python -m unittest discover -s tests"},
                 "======\nFAIL: test_x (tests.test_flow_patch_change.C.test_x)\n"
                 "Traceback (most recent call last):\n  File \"" + root
                 + "tests\\test_flow_patch_change.py\", line 109, in test_x\n"
                 "AttributeError: 'NoneType' object has no attribute 'name'\n"
                 "FAILED (failures=1)\nRan 8 tests")
    st = step(msgs)
    check("A3 debug: read failing test", st and st.name == "read"
          and "test_flow_patch_change.py" in st.instruction, st and st.instruction)
    msgs += call(2, "read", {"filePath": "tests/test_flow_patch_change.py"},
                 "from gemini_web2api import flow_patch as F\n" + "x = 1\n" * 5)
    st = step(msgs)
    check("A4 debug: hop to the module the test imports",
          st and st.name == "read" and "flow_patch.py" in st.instruction,
          st and (st.name, st.instruction))
    msgs += call(3, "read", {"filePath": "gemini_web2api/flow_patch.py"},
                 "from . import tools as T\nfrom .config import CONFIG\n"
                 + "T.x\n" * 8 + "def plan_for(): pass")
    st = step(msgs)
    check("A5 debug: second hop to the most-used import (tools.py)",
          st and st.name == "read" and "tools.py" in st.instruction,
          st and (st.name, st.instruction))
    msgs += call(4, "read", {"filePath": "gemini_web2api/tools.py"}, "def detect(): pass")
    msgs += call(5, "read", {"filePath": "gemini_web2api/config.py"}, "CONFIG = {}")
    st = step(msgs)
    check("A6 debug: answer with root-cause format",
          st and st.name == "answer" and "ROOT CAUSE" in st.instruction,
          st and (st.name, st.instruction[:80]))

    m2 = [{"role": "user", "content": "test đang fail, tại sao?"}]
    m2 += call(0, "glob", {"pattern": "**/*"}, listing)
    m2 += call(1, "bash", {"command": "npm test"},
               "npm error code ENOENT\nnpm error Could not read package.json")
    st = step(m2)
    check("A7 broken command -> try another", st and st.name == "tests"
          and "npm" not in cmd_of(st), st and st.name)

    # --- B: CHANGE ------------------------------------------------------------
    mb = [{"role": "user", "content": "fix the `retry_delay` handling"}]
    mb += call(0, "glob", {"pattern": "**/*.py"}, "pkg/a.py\npkg/b.py")
    st = step(mb)
    check("B1 change: grep first", st and st.name == "search", st and st.name)
    mb += call(1, "grep", {"pattern": "x"},
               "pkg/b.py:10: retry_delay = 1\npkg/b.py:20: retry_delay")
    st = step(mb)
    check("B2 change: read ranked file", st and st.name == "read"
          and "pkg/b.py" in st.instruction, st and st.instruction)

    # "add ... to server.py" phải ĐỌC server.py (tools._created_files bỏ qua file này)
    mk = [{"role": "user", "content": "add a retry to server.py"}]
    st = step(mk)
    check("B3 change: named file is read, not treated as 'to create'",
          st and st.name == "read" and "server.py" in st.instruction,
          st and (st.name, st.instruction))

    # --- C: EXPLAIN chung chung -> overview ---------------------------------
    mc = [{"role": "user", "content": "giải thích source này hoạt động thế nào"}]
    st = step(mc)
    check("C1 explain: survey (no junk grep)", st and st.name == "survey",
          st and (st.name, st.calls))
    mc += call(0, "glob", {"pattern": "**/*"}, "README.md\npkg/server.py\npkg/util.py")
    st = step(mc)
    check("C2 explain: docs first", st and st.name == "read" and "README.md" in st.instruction,
          st and st.instruction)
    mc += call(1, "read", {"filePath": "README.md"}, "# Title\nbody")
    st = step(mc)
    check("C3 explain: read entry sources", st and st.name == "read"
          and "server.py" in st.instruction, st and st.instruction)
    mc += call(2, "read", {"filePath": "pkg/server.py"}, "def main(): pass")
    st = step(mc)
    check("C4 explain: explanation (not bug report)", st and st.name == "answer"
          and "NOT a bug report" in st.instruction, st and st.instruction[:60])

    # --- D: DIFF_REVIEW ------------------------------------------------------
    md = [{"role": "user", "content": "review các thay đổi hiện tại giúp mình"}]
    st = step(md)
    check("D1 diff: git status", st and st.pattern == "DIFF_REVIEW"
          and "git status" in cmd_of(st), st and (st.pattern, st.calls))

    # --- E: vòng sửa-test ----------------------------------------------------
    me = [{"role": "user", "content": "fix the retry bug in server.py"}]
    me += call(0, "read", {"filePath": "server.py"}, "def retry(): pass")
    me += call(1, "edit", {"filePath": "server.py"}, "Edit applied successfully.")
    st = step(me)
    check("E1 change: tests after edit", st and st.name == "tests"
          and "unittest" in cmd_of(st), st and st.name)
    me += call(2, "bash", {"command": "python -m unittest discover -s tests"},
               "FAIL: test_a (tests.t.C.test_a)\nAssertionError: 1 != 2\nFAILED (failures=1)\nRan 3 tests")
    st = step(me)
    check("E2 change: failing tests -> act again", st and st.name == "act"
          and "FAIL" in st.instruction, st and (st.name, st.instruction[:60]))
    body = "line of code\n" * 100
    mg = [{"role": "user", "content": "fix a.py"}] + call(0, "read", {"filePath": "a.py"}, body)
    tc = {"id": "x", "type": "function", "function": {
        "name": "write", "arguments": json.dumps({"filePath": "a.py", "content": "x = 1"})}}
    _c_, problems = F._gate_act_calls([tc], mg)
    check("E3 gate: shrinking write is flagged", len(problems) == 1, problems)

    # --- G: tạo file mới = gộp các file (ca user báo "chỉ hướng dẫn") ---------
    mp = "d:\\P\\"
    ml = "\n".join(mp + p for p in [
        "gemini_web2api\\__init__.py", "gemini_web2api\\config.py",
        "gemini_web2api\\tools.py", "gemini_web2api\\__main__.py",
        "setup.py", "tests\\test_a.py"])
    gm = [{"role": "user", "content": "Hãy tạo ra 1 file gemini_web2api.py, nội dung "
           "file sẽ là tổng hợp tất cả file trong gemini_web2api folder thành 1 file duy nhất."}]
    st = step(gm)
    check("G1 create: recognised, survey first", st and st.pattern == "CREATE"
          and st.name == "survey", st and (st.pattern, st.name))
    gm += call(0, "glob", {"pattern": "**/*.py"}, ml)
    st = step(gm)
    wargs = json.loads(st.calls[0]["function"]["arguments"]) if st and st.calls else {}
    check("G2 create: proxy writes the merge script itself",
          st and st.name == "merge" and st.calls
          and st.calls[0]["function"]["name"] == "write"
          and wargs.get("filePath") == _MERGE_SCRIPT
          and "tools.py" in wargs.get("content", "")
          and "test_a.py" not in wargs.get("content", "")
          and "setup.py" not in wargs.get("content", ""), st and st.name)
    gm += call(1, "write", {"filePath": _MERGE_SCRIPT, "content": "x"}, "Wrote file successfully.")
    st = step(gm)
    check("G3 create: run the script", st and st.calls and cmd_of(st).startswith("python _merge"),
          st and (st.name, st.calls))
    gm += call(2, "bash", {"command": "python " + _MERGE_SCRIPT},
               "merged 4 files -> d:/P/gemini_web2api.py (100 bytes)\norder: a")
    st = step(gm)
    check("G4 create: syntax check", st and st.calls and "py_compile" in cmd_of(st),
          st and (st.name, st.calls))
    gm += call(3, "bash", {"command": "python -m py_compile x"}, "")
    st = step(gm)
    check("G5 create: final answer says it is DONE", st and st.name == "answer"
          and "DONE" in st.instruction, st and (st.name, st.instruction[:60]))

    ok = all(results)
    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if ok else 1


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--apply" in argv:
        sys.exit(apply())
    if "--selftest" in argv:
        sys.exit(selftest())
    print(__doc__)
