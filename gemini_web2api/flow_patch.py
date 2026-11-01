"""flow_patch.py -- ép model yếu (gemini-3.5-flash-lite) đi đúng quy trình tool-call.

Ý tưởng: thay vì NHẮC model "hãy đọc file trước khi review", proxy tự giữ
state machine cho từng loại tác vụ (pattern). Mỗi lượt proxy tính ra "bước tiếp
theo" từ bằng chứng có trong hội thoại (client gửi lại toàn bộ messages nên
không cần lưu state ở server):

    GENERIC_REVIEW : survey(glob) -> tests -> read TẤT CẢ file lõi -> answer
    FILE_REVIEW    : read các file user nêu tên -> answer
    CHANGE         : survey -> read file đích -> act(edit/write) -> tests -> answer
    EXPLAIN        : grep/glob -> read file trúng -> answer
    TOOL_DEMAND    : bắt buộc gọi 1 tool -> answer

Với mỗi bước có 2 chế độ:
  * DRIVE (bước xác định được: glob, chạy test, read file đã biết tên):
    proxy TỰ SINH tool_call, không gọi upstream => 100% đúng format, nhanh hơn,
    rẻ hơn. Model chỉ còn việc ở bước "answer"/"act" -- nơi cần phán đoán.
  * GUIDE (bước cần model quyết định: viết edit, chọn grep pattern ...):
    gọi upstream với directive "STEP i/n" kèm template tool_call điền sẵn, bắt
    buộc có tool_call, lọc call sai loại, retry, cuối cùng fallback tự sinh
    (nếu bước xác định được) hoặc trả nguyên kết quả (không bao giờ kẹt).

Tuỳ chọn: hỏi lại client khi yêu cầu mơ hồ (flow_ask_when_unclear).

CÀI ĐẶT (2 dòng trong gemini_web2api/__main__.py, sau load_config + args):

    from . import flow_patch
    flow_patch.install()

Đặt file này cạnh server.py (gemini_web2api/flow_patch.py). Cấu hình trong
config.json (đều tuỳ chọn, xem _DEFAULTS bên dưới). Tắt nhanh:
    {"flow_enabled": false}
Chưa phủ: endpoint Google native (/v1beta) vì có vòng retry riêng.
"""
import functools
import json
import os
import posixpath
import re
import uuid
from collections import Counter

from . import tools as T
from .config import CONFIG
from .gemini import log

__all__ = ["install", "plan_for", "tool_kind", "Step", "ToolMap"]

_DEFAULTS = {
    "flow_enabled": True,
    # True: bước xác định được thì proxy tự sinh tool_call, khỏi gọi upstream.
    "flow_drive": True,
    "flow_explain": True,
    "flow_review_run_tests": True,
    # None = autodetect (package.json -> npm test, .py -> unittest, else skip tests)
    "flow_test_command": None,
    "flow_survey_pattern": "**/*.{py,js,jsx,mjs,cjs,ts,tsx,go,rs,java,kt,rb,php,cs,c,cc,cpp,h,hpp,swift}",
    "flow_read_batch": 6,
    "flow_max_review_files": 40,
    "flow_max_retries": 1,
    "flow_review_include_tests": False,
    "flow_answer_directive": True,
    # Opt-in: yêu cầu quá mơ hồ -> trả câu hỏi chọn pattern 1-4 cho client.
    "flow_ask_when_unclear": False,
}


def _cfg(key):
    return CONFIG.get(key, _DEFAULTS[key])


# --------------------------------------------------------------------------
# Phân loại tool theo "kind" từ tên (client nào cũng khác tên: read/Read/
# read_file/readFile, glob/list_files, bash/run_command ...).
# --------------------------------------------------------------------------

_META_SUBSTR = ("todo", "web", "task", "question", "skill", "agent", "notebook",
                "plan")
_KIND_WORDS = [
    ("act", {"edit", "write", "patch", "replace", "insert", "append", "create",
             "apply", "delete", "move", "rename", "mkdir", "editor", "save"}),
    ("exec", {"bash", "shell", "exec", "run", "command", "terminal", "cmd",
              "powershell"}),
    ("search", {"grep", "ripgrep", "rg", "search"}),
    ("survey", {"glob", "list", "ls", "find", "tree", "dir", "directory"}),
    ("read", {"read", "cat", "view", "open", "head", "tail", "show"}),
]
_KIND_SUBSTR = [("act", ("edit", "write", "patch", "replace")),
                ("exec", ("bash", "shell", "exec")),
                ("search", ("grep",)),
                ("survey", ("glob",)),
                ("read", ("read",))]


def _tokens(name):
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(name))
    return [t for t in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if t]


def tool_kind(name) -> str:
    """read | survey | search | act | exec | other"""
    low = str(name or "").lower()
    toks = set(_tokens(name))
    if any(m in low for m in _META_SUBSTR):
        return "other"
    for kind, words in _KIND_WORDS:
        if toks & words:
            return kind
    for kind, subs in _KIND_SUBSTR:
        if any(s in low for s in subs):
            return kind
    return "other"


_PATH_KEYS = ["filePath", "file_path", "path", "file", "filename", "file_name",
              "target_file", "target"]
_PATTERN_KEYS = ["pattern", "query", "glob", "regex", "search"]
_CMD_KEYS = ["command", "cmd", "script"]


def _pick(props, keys):
    low = {str(p).lower(): p for p in (props or ())}
    for k in keys:
        if k.lower() in low:
            return low[k.lower()]
    return None


class ToolMap:
    """Các tool client khai báo, gom theo kind."""

    def __init__(self, allowed_names, schemas, required):
        self.schemas = schemas or {}
        self.required = required or {}
        self.by_kind = {}
        for n in sorted(allowed_names or [], key=lambda x: (len(x), x)):
            self.by_kind.setdefault(tool_kind(n), []).append(n)

    def first(self, kind):
        lst = self.by_kind.get(kind)
        return lst[0] if lst else None

    def names(self, kinds):
        out = []
        for k in sorted(kinds):
            out.extend(self.by_kind.get(k, []))
        return out

    def make_call(self, kind, path=None, pattern=None, command=None,
                  description=None):
        """Tool_call (định dạng OpenAI) hoặc None nếu không sinh được."""
        name = self.first(kind)
        if not name:
            return None
        props = self.schemas.get(name) or set()
        args = {}
        if kind == "read":
            key = _pick(props, _PATH_KEYS) or (None if props else "filePath")
            if not key or not path:
                return None
            args[key] = path
        elif kind in ("survey", "search"):
            key = _pick(props, _PATTERN_KEYS)
            if key is None and not props:
                key = "pattern"
            if key and pattern:
                args[key] = pattern
            else:
                pkey = _pick(props, _PATH_KEYS)
                if kind == "survey" and pkey:
                    args[pkey] = "."
                else:
                    return None
        elif kind == "exec":
            key = _pick(props, _CMD_KEYS) or (None if props else "command")
            if not key or not command:
                return None
            args[key] = command
            dkey = _pick(props, ["description"])
            if dkey:
                args[dkey] = description or "Run command"
        else:
            return None
        for req in self.required.get(name, ()):
            if req in args:
                continue
            if str(req).lower() == "description":
                args[req] = description or "Run"
            else:
                return None
        return {"id": f"call_{uuid.uuid4().hex[:8]}", "type": "function",
                "function": {"name": name,
                             "arguments": json.dumps(args, ensure_ascii=False)}}


# --------------------------------------------------------------------------
# Bằng chứng trong lượt hiện tại (kể từ message user cuối)
# --------------------------------------------------------------------------

def _answered_calls(messages):
    """[{name, kind, args, result}] cho mọi call ĐÃ có tool result trong lượt."""
    start = T._last_user_index(messages)
    calls, out = {}, []
    for msg in messages[start + 1:]:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("id"):
                    fn = tc.get("function") or {}
                    calls[tc["id"]] = (fn.get("name") or "",
                                       T._tool_arguments_dict(fn))
        elif msg.get("role") == "tool":
            hit = calls.get(msg.get("tool_call_id"))
            if hit:
                out.append({"name": hit[0], "kind": tool_kind(hit[0]),
                            "args": hit[1],
                            "result": T._stringify_content(msg.get("content"))})
    return out


def _call_path(args):
    for k in _PATH_KEYS:
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().replace("\\", "/")
    return None


def _call_command(args):
    for k in _CMD_KEYS:
        v = args.get(k)
        if isinstance(v, str):
            return v
    return json.dumps(args, ensure_ascii=False)


_LIST_CMD_RE = re.compile(r"\b(ls|find|dir|tree|git\s+ls-files)\b")
_TEST_CMD_RE = re.compile(
    r"\b(unittest|pytest|npm\s+(?:run\s+)?test|yarn\s+test|make\s+test|"
    r"go\s+test|cargo\s+test)\b")
_SURVEY_CMD = ('git ls-files 2>/dev/null || find . -type f -not -path '
               '"./.git/*" -not -path "*/node_modules/*" | head -300')


_PATH_LINE_RE = re.compile(
    r'^\s*(?:[-*]\s+)?((?:[A-Za-z]:)?[/\\]?(?:[^\s:*?"<>|/\\]+[/\\])*'
    r'[^\s:*?"<>|/\\]+\.[A-Za-z0-9]{1,6})\s*$')
_READ_FAIL_RE = re.compile(
    r"^\s*(?:<?error|file not found|no such file|enoent|cannot (?:read|find|open)"
    r"|failed|not found)", re.IGNORECASE)


def _read_ok(e):
    """Tool result của một lệnh read có thật sự chứa nội dung không."""
    return bool(e["result"].strip()) and not _READ_FAIL_RE.match(e["result"])


def _listing_files(ev):
    """File paths từ kết quả survey/search. Giữ nguyên ổ đĩa (D:/...) và
    đường dẫn tuyệt đối: cắt mất chúng làm mọi lệnh read sau đó 'not found'."""
    seen, out = set(), []

    def add(f):
        f = f.replace("\\", "/")
        if f not in seen and f.lower() not in T._NON_FILE_NAMES:
            seen.add(f)
            out.append(f)

    for e in ev:
        if not (e["kind"] in ("survey", "search") or (
                e["kind"] == "exec"
                and _LIST_CMD_RE.search(_call_command(e["args"])))):
            continue
        matched = [m.group(1) for m in map(_PATH_LINE_RE.match,
                                           e["result"].splitlines()) if m]
        if matched:
            for f in matched:
                add(f)
        else:  # grep-style "path:line: text" output
            body = e["result"].replace("\\", "/")
            for f in sorted(T._files_in(body), key=body.find):
                add(f)
    return out


def _read_set(ev):
    return {_call_path(e["args"]).lower() for e in ev
            if e["kind"] == "read" and _call_path(e["args"])}


def _surveyed(ev):
    return any(e["kind"] in ("survey", "search") or (
        e["kind"] == "exec" and _LIST_CMD_RE.search(_call_command(e["args"])))
        for e in ev)


def _survey_count(ev):
    return sum(1 for e in ev if e["kind"] in ("survey", "search") or (
        e["kind"] == "exec" and _LIST_CMD_RE.search(_call_command(e["args"]))))


def _is_test_cmd(cmd):
    configured = (_cfg("flow_test_command") or "").strip()
    return bool(_TEST_CMD_RE.search(cmd) or (configured and configured in cmd))


def _test_command(listing):
    """Lệnh test: config > autodetect theo listing > None (bỏ qua bước test)."""
    configured = _cfg("flow_test_command")
    if configured:
        return configured
    names = [f.replace("\\", "/").lower() for f in listing]
    if any(n.endswith("package.json") for n in names) or any(
            n.endswith((".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx")) for n in names):
        return "npm test"
    if any(n.endswith(".go") for n in names):
        return "go test ./..."
    if any(n.endswith(".rs") for n in names):
        return "cargo test"
    if any(n.endswith(".py") for n in names):
        return "python -m unittest discover -s tests"
    return None


def _tests_ran_after_act(ev):
    last_act = max((i for i, e in enumerate(ev) if e["kind"] == "act"),
                   default=-1)
    return any(i > last_act and e["kind"] == "exec"
               and _is_test_cmd(_call_command(e["args"]))
               for i, e in enumerate(ev))


_SRC_EXT = (".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt",
            ".rb", ".php", ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".swift")
_IGNORE_SEG = {"__pycache__", "node_modules", ".git", "venv", ".venv", "env",
               "site-packages", "dist", "build", ".tox", ".mypy_cache",
               "vendor", ".idea"}
_TEST_SEG = {"tests", "test", "__tests__", "spec"}


def _core_sources(files, include_tests, limit):
    cand = []
    for f in files:
        p = f.replace("\\", "/")
        segs = p.split("/")
        if any(s in _IGNORE_SEG for s in segs[:-1]):
            continue
        if not p.lower().endswith(_SRC_EXT) or segs[-1] == "__init__.py":
            continue
        if not include_tests and (
                any(s in _TEST_SEG for s in segs[:-1])
                or segs[-1].startswith("test_")
                or segs[-1].endswith(("_test.py", ".test.js", ".test.ts",
                                      ".spec.js", ".spec.ts"))):
            continue
        if p not in cand:
            cand.append(p)
    if not cand:
        return []
    exts = Counter(os.path.splitext(p)[1].lower() for p in cand)
    top = exts.most_common(1)[0][1]
    keep = {e for e, n in exts.items() if n * 10 >= top * 3}
    core = [p for p in cand if os.path.splitext(p)[1].lower() in keep]
    entry = {"index", "main", "app", "server", "cli", "__main__", "mod", "lib"}
    low_dirs = {"scripts", "script", "utils", "util", "tools", "examples",
                "example", "docs", "bench", "benchmarks", "migrations", "types"}

    def key(item):
        i, p = item
        parts = p.lower().split("/")
        base = posixpath.splitext(parts[-1])[0]
        tier = 0 if base in entry else (2 if (set(parts[:-1]) & low_dirs
                                              or base in low_dirs) else 1)
        return (tier, i)

    return [p for _i, p in sorted(enumerate(core), key=key)][:limit]


_STOP = set(
    "the and for with this that these those please can you could would should "
    "how what where why when which does did from into about code file files "
    "project repo update change fix add make implement review check cho toi "
    "minh ban hay nay trong cua nhung cac mot nhu nao khong duoc va la co "
    "giup defined define defines used uses work works function class method".split())


def _keywords(text, limit=6):
    out = []
    for m in re.finditer(r"`([^`\n]{2,60})`", text or ""):
        k = re.sub(r"[()\s]", "", m.group(1))
        if k and k.lower() not in out:
            out.append(k.lower())
    folded = T._fold_diacritics(text or "")
    for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", folded):
        lw = w.lower()
        if lw not in _STOP and lw not in out:
            out.append(lw)
    return out[:limit]


def _rank(files, kws, hits=None):
    scored = []
    for i, f in enumerate(files):
        low = f.lower()
        s = 10 * sum(1 for k in kws if k in low) + (hits or {}).get(f, 0)
        if s > 0:
            scored.append((-s, i, f))
    return [f for _s, _i, f in sorted(scored)]


def _resolve(name, listing):
    """Tên file user nêu -> đường dẫn trong listing nếu khớp, else giữ nguyên."""
    n = name.replace("\\", "/")
    for f in listing:
        if T._path_seen(n, [f]):
            return f
    return n


# --------------------------------------------------------------------------
# Step
# --------------------------------------------------------------------------

class Step:
    def __init__(self, pattern, name, index, total, kinds, instruction,
                 calls=None, drive=False, tool_names=()):
        self.pattern, self.name = pattern, name
        self.index, self.total = index, total
        self.kinds = set(kinds)
        self.instruction = instruction
        self.calls = calls or None
        self.drive = bool(drive and calls)
        self.tool_names = list(tool_names)

    def directive(self):
        head = f"[FLOW {self.pattern} step {self.index}/{self.total} -- {self.name.upper()}]"
        out = [head + " " + self.instruction]
        if self.tool_names:
            out.append("Allowed tools this turn: " + ", ".join(self.tool_names)
                       + ". Output ONLY tool_call block(s), no prose.")
        if self.calls:
            out.append("Emit exactly:")
            for c in self.calls:
                fn = c["function"]
                payload = {"name": fn["name"],
                           "arguments": json.loads(fn["arguments"])}
                out.append("```tool_call\n"
                           + json.dumps(payload, ensure_ascii=False) + "\n```")
        return "\n".join(out)


# --------------------------------------------------------------------------
# Phân loại pattern (+ hỏi lại client nếu mơ hồ)
# --------------------------------------------------------------------------

CLARIFY_MARK = "(flow:clarify)"
_CLARIFY_MAP = {"1": "GENERIC_REVIEW", "2": "CHANGE", "3": "EXPLAIN",
                "4": "TOOL_DEMAND"}
_VI_RE = re.compile("[àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡ"
                    "ùúụủũưừứựửữỳýỵỷỹđ]")
_QUESTION_RE = re.compile(
    r"\b(how|where|why|what|which|explain|does|show me|giai thich|o dau|"
    r"nhu the nao|hoat dong|la gi|tai sao|dinh nghia)\b")
_PROJECT_RE = re.compile(
    r"\b(this|the|our)\s+(project|repo|repository|codebase|code|proxy|server|"
    r"module|function|class|file)\b|\b(du an|repo|codebase|ma nguon|trong code|"
    r"ham|class|module)\b")
_VAGUE_RE = re.compile(
    r"\b(improve|optimi[sz]e|clean|handle|help|toi uu|cai thien|xu ly|giup|"
    r"lam|do)\b")


def _is_code_question(text):
    scannable = T._strip_code_spans(text)
    folded = re.sub(r"\s+", " ", T._fold_diacritics(scannable.lower())).strip()
    if not _QUESTION_RE.search(folded):
        return False
    has_ident = bool(re.search(r"`[^`\n]{2,}`", text or ""))
    has_file = any(f for f in T._files_in(scannable)
                   if f.lower() not in T._NON_FILE_NAMES)
    return has_ident or has_file or bool(_PROJECT_RE.search(folded))


class _Ctx:
    pass


def _analyze(messages):
    idx = T._last_user_index(messages)
    if idx < 0:
        return None
    ctx = _Ctx()
    ctx.idx = idx
    ctx.last = T._stringify_content(messages[idx].get("content", ""))
    ctx.request = ctx.last
    forced = _clarified(messages, idx)
    if forced:
        ctx.pattern, ctx.request = forced[0], forced[1] or ctx.last
    else:
        ctx.pattern = T.detect_task_pattern(messages)
        if not ctx.pattern and _cfg("flow_explain") and _is_code_question(ctx.last):
            ctx.pattern = "EXPLAIN"
    ctx.scannable = T._strip_code_spans(ctx.request)
    ctx.folded = re.sub(r"\s+", " ", T._fold_diacritics(ctx.scannable)).strip()
    return ctx


def _clarified(messages, idx):
    if idx < 1:
        return None
    prev = messages[idx - 1]
    if not (isinstance(prev, dict) and prev.get("role") == "assistant"
            and CLARIFY_MARK in T._stringify_content(prev.get("content", ""))):
        return None
    ans = T._fold_diacritics(
        T._stringify_content(messages[idx].get("content", ""))).lower().strip()
    pattern = None
    m = re.match(r"^\W*([1-4])\b", ans)
    if m:
        pattern = _CLARIFY_MAP[m.group(1)]
    elif re.search(r"review|audit|soat|danh gia", ans):
        pattern = "GENERIC_REVIEW"
    elif re.search(r"fix|implement|sua|them|code", ans):
        pattern = "CHANGE"
    elif re.search(r"explain|giai thich|tim hieu", ans):
        pattern = "EXPLAIN"
    if not pattern:
        return None
    original = None
    if idx >= 2 and isinstance(messages[idx - 2], dict) \
            and messages[idx - 2].get("role") == "user":
        original = T._stringify_content(messages[idx - 2].get("content", ""))
    return pattern, original


def clarification_text(messages):
    """Câu hỏi chọn pattern nếu yêu cầu mơ hồ, else None (opt-in)."""
    if not _cfg("flow_ask_when_unclear") or not messages:
        return None
    last = messages[-1]
    if not (isinstance(last, dict) and last.get("role") == "user"):
        return None
    if any(isinstance(m, dict) and (m.get("tool_calls") or m.get("role") == "tool")
           for m in messages):
        return None
    ctx = _analyze(messages)
    if ctx is None or ctx.pattern:
        return None
    text = ctx.last.strip()
    folded = T._fold_diacritics(text.lower())
    if len(text) > 120 or T._files_in(text) or not _VAGUE_RE.search(folded):
        return None
    if _VI_RE.search(text.lower()):
        return ("Mình chưa rõ yêu cầu này thuộc dạng nào. Bạn chọn giúp:\n"
                "1. Review / audit source\n2. Implement / sửa code\n"
                "3. Giải thích / tìm hiểu code\n4. Chạy một lệnh / tool cụ thể\n"
                f"Trả lời 1-4 hoặc mô tả rõ hơn. {CLARIFY_MARK}")
    return ("I am not sure which kind of task this is. Please pick:\n"
            "1. Review / audit the source\n2. Implement / change code\n"
            "3. Explain / understand the code\n4. Run a specific command/tool\n"
            f"Reply 1-4 or describe it more precisely. {CLARIFY_MARK}")


# --------------------------------------------------------------------------
# Planner
# --------------------------------------------------------------------------

def _answer_step(pattern, ev, total, index, skipped=None):
    if not ev or not _cfg("flow_answer_directive"):
        return None
    read = sorted({_call_path(e["args"]) for e in ev
                   if e["kind"] == "read" and _call_path(e["args"])
                   and _read_ok(e)})
    if pattern != "CHANGE" and not read:
        instr = ("NO file was successfully read in this turn (every read "
                 "failed or none was possible). Say that plainly and ask the "
                 "user for the correct project path. Do NOT describe or "
                 "evaluate any code: you have not seen any.")
    elif pattern == "CHANGE":
        instr = ("Evidence complete. If the last test run failed, fix the "
                 "cause with one more edit tool_call; otherwise reply in plain "
                 "text: what you changed and the test result. No other tool calls.")
    else:
        instr = ("Evidence collected (successfully read: "
                 + (", ".join(read[:30]) or "none") + "). Answer NOW in plain "
                 "text with NO tool_call block. Base every claim on those "
                 "files. Report format: each finding as `file:line -- verbatim "
                 "quote -- why it is a bug`. If you found none, write exactly "
                 "'no critical bug found in the files read' and list the files "
                 "read. A passing test suite does not prove absence of bugs. "
                 "Only claim a rule/contract is respected if you compared it "
                 "to the code you read; never cite a document you did not read.")
        if skipped:
            instr += (" NOT read (say your coverage is PARTIAL and name them): "
                      + ", ".join(skipped[:20]) + ".")
    return Step(pattern, "answer", index, total, (), instr)


def _read_calls(tm, files):
    calls = [tm.make_call("read", path=f) for f in files]
    return calls if calls and all(calls) else None


def plan_for(messages, tm, ctx=None):
    """Bước tiếp theo bắt buộc, hoặc None (model được trả lời tự do)."""
    ctx = ctx or _analyze(messages)
    if ctx is None or not ctx.pattern:
        return None
    p = ctx.pattern
    ev = _answered_calls(messages)
    listing = _listing_files(ev)
    read_set = _read_set(ev)
    batch = max(1, int(_cfg("flow_read_batch")))
    test_cmd = _test_command(listing)
    named = [f for f in T._files_in(ctx.scannable)
             if f.lower() not in T._NON_FILE_NAMES
             and f.lower() not in T._created_files(ctx.scannable, ctx.folded)]

    def survey_step(index, total):
        retry = _survey_count(ev) >= 1  # first listing came back empty
        glob_call = tm.make_call("survey", pattern="**/*" if retry
                                 else _cfg("flow_survey_pattern"))
        exec_call = tm.make_call("exec", command=_SURVEY_CMD,
                                 description="List project files")
        call = (exec_call or glob_call) if retry else (glob_call or exec_call)
        if not call and not tm.names({"survey", "search", "exec"}):
            return None
        return Step(p, "survey", index, total, {"survey", "search", "exec"},
                    "List the project files before anything else.", [call] if call else None,
                    True, tm.names({"survey", "search", "exec"}))

    def tests_step(index, total):
        if not test_cmd:
            return None
        call = tm.make_call("exec", command=test_cmd, description="Run unit tests")
        if not call:
            return None
        return Step(p, "tests", index, total, {"exec"},
                    "Run the unit tests.", [call], True, tm.names({"exec"}))

    def read_step(files, index, total, drive=True):
        calls = _read_calls(tm, files)
        return Step(p, "read", index, total, {"read"},
                    "Read these files fully before answering: " + ", ".join(files) + ".",
                    calls, drive, tm.names({"read"}))

    if p == "GENERIC_REVIEW":
        if not listing and _survey_count(ev) < 2:
            s = survey_step(1, 4)
            if s:
                return s
        if (_cfg("flow_review_run_tests") and tm.first("exec")
                and not any(e["kind"] == "exec" and _is_test_cmd(_call_command(e["args"]))
                            for e in ev)):
            s = tests_step(2, 4)
            if s:
                return s
        core = _core_sources(listing, _cfg("flow_review_include_tests"),
                             int(_cfg("flow_max_review_files")))
        unread = [f for f in core if not T._path_seen(f, read_set)]
        if unread and tm.first("read"):
            return read_step(unread[:batch], 3, 4)
        skipped = [f for f in _core_sources(
            listing, _cfg("flow_review_include_tests"), 10 ** 6)
            if not T._path_seen(f, read_set)]
        if not read_set and tm.first("read"):
            # No recognised source files: read whatever the listing has
            # (README/AGENTS/config...) rather than answer from nothing.
            fallback = [f for f in listing if not any(
                s in _IGNORE_SEG for s in f.replace("\\", "/").split("/")[:-1])]
            if fallback:
                return read_step(fallback[:batch], 3, 4)
        return _answer_step(p, ev, 4, 4, skipped)

    if p == "FILE_REVIEW":
        files = [_resolve(f, listing) for f in named]
        unread = [f for f in files if not T._path_seen(f, read_set)]
        if unread and tm.first("read"):
            return read_step(unread[:batch], 1, 2)
        return _answer_step(p, ev, 2, 2)

    if p == "CHANGE":
        known = [_resolve(f, listing) for f in named]
        if not known and not _surveyed(ev):
            s = survey_step(1, 5)
            if s:
                return s
        if known:
            unread = [f for f in known if not T._path_seen(f, read_set)]
            if unread and tm.first("read"):
                return read_step(unread[:batch], 2, 5)
        elif not read_set and tm.first("read"):
            core = _core_sources(listing, False, 200)
            ranked = _rank(core, _keywords(ctx.request))[:2]
            if ranked:
                return read_step(ranked, 2, 5)
            return read_step(core[:2], 2, 5, drive=False) if core else Step(
                p, "read", 2, 5, {"read"},
                "Read the file(s) you are going to change.", None, False,
                tm.names({"read"}))
        if not any(e["kind"] == "act" for e in ev) and tm.first("act"):
            return Step(p, "act", 3, 5, {"act"},
                        "Make the change now with ONE edit/write tool_call per "
                        "file, copying old text exactly from the file you read.",
                        None, False, tm.names({"act"}))
        edited_src = any(e["kind"] == "act" and (_call_path(e["args"]) or "")
                         .lower().endswith(_SRC_EXT) for e in ev)
        if (edited_src and tm.first("exec") and not _tests_ran_after_act(ev)
                and not T._NO_TEST_RE.search(ctx.folded)):
            s = tests_step(4, 5)
            if s:
                return s
        return _answer_step(p, ev, 5, 5)

    if p == "EXPLAIN":
        kws = _keywords(ctx.request)
        if not _surveyed(ev):
            call = None
            if kws and tm.first("search"):
                call = tm.make_call("search", pattern="(?i)" + "|".join(
                    re.escape(k) for k in kws[:3]))
            call = call or tm.make_call("survey", pattern=_cfg("flow_survey_pattern"))
            kinds = {"survey", "search", "exec"}
            return Step(p, "search", 1, 3, kinds,
                        "Locate the relevant code with grep/glob first.",
                        [call] if call else None, True, tm.names(kinds))
        if not read_set and tm.first("read"):
            hits = {}
            for e in ev:
                if e["kind"] == "search":
                    body = e["result"].replace("\\", "/")
                    for f in T._files_in(body):
                        hits[f] = hits.get(f, 0) + body.count(f)
            pool = list(hits) or listing
            pool = _core_sources(pool, True, 400) or pool
            ranked = _rank(pool, kws, hits)[:2] or pool[:1]
            if ranked:
                return read_step(ranked, 2, 3)
        return _answer_step(p, ev, 3, 3)

    if p == "TOOL_DEMAND":
        if not ev:
            kinds = {"read", "survey", "search", "act", "exec"}
            return Step(p, "call", 1, 1, kinds,
                        "The user asked you to use tools: call one now.",
                        None, False, tm.names(kinds))
        return None
    return None


# --------------------------------------------------------------------------
# Hook vào GeminiHandler._generate_with_tool_retry
# --------------------------------------------------------------------------

def install():
    """Monkeypatch server.GeminiHandler. Idempotent."""
    from . import server
    for k, v in _DEFAULTS.items():
        CONFIG.setdefault(k, v)
    cls = server.GeminiHandler
    orig = cls._generate_with_tool_retry
    if getattr(orig, "_flow_patched", False):
        return

    @functools.wraps(orig)
    def wrapper(self, prompt, model_id, think_mode, file_refs, extra,
                tools_active, allowed_names, tool_schemas, required_tool,
                messages=None, ticket=None, tool_required=None,
                client_gone=None):
        def call(p, req=required_tool):
            return orig(self, p, model_id, think_mode, file_refs, extra,
                        tools_active, allowed_names, tool_schemas, req,
                        messages=messages, ticket=ticket,
                        tool_required=tool_required, client_gone=client_gone)

        if not (_cfg("flow_enabled") and tools_active and not required_tool
                and messages and allowed_names):
            return call(prompt)
        try:
            tm = ToolMap(allowed_names, tool_schemas, tool_required)
            ask = clarification_text(messages)
            if ask:
                log("[flow] request unclear -> asking the client to pick a pattern")
                return ask, None
            ctx = _analyze(messages)
            last_role = messages[-1].get("role") if isinstance(messages[-1], dict) else None
            step = plan_for(messages, tm, ctx) if last_role in ("user", "tool") else None
        except Exception as e:  # never let the guard break a request
            log(f"[flow] planner error, falling back: {e}")
            return call(prompt)
        if step is None:
            return call(prompt)
        log(f"[flow] pattern={step.pattern} step {step.index}/{step.total} "
            f"{step.name} mode={'drive' if step.drive and _cfg('flow_drive') else 'guide'}")

        if step.name == "answer":
            return call(prompt + "\n\n" + step.directive())
        if step.drive and _cfg("flow_drive"):
            return "", step.calls

        nudge, last = "", None
        for attempt in range(1 + max(0, int(_cfg("flow_max_retries")))):
            try:
                text, calls = call(prompt + "\n\n" + step.directive() + nudge, True)
            except Exception as e:
                log(f"[flow] guide attempt {attempt + 1} failed: {e}")
                if step.calls:
                    return "", step.calls
                if attempt + 1 >= 1 + max(0, int(_cfg("flow_max_retries"))):
                    raise
                continue
            last = (text, calls)
            ok = [tc for tc in (calls or [])
                  if tool_kind((tc.get("function") or {}).get("name")) in step.kinds]
            if ok:
                return "", ok
            bad = ", ".join((tc.get("function") or {}).get("name", "?")
                            for tc in (calls or [])) or "none"
            nudge = (f" Your previous reply used: {bad}. That is not allowed at "
                     f"this step; use only: {', '.join(step.tool_names)}.")
        if step.calls:
            log("[flow] guide failed, synthesizing the step's call")
            return "", step.calls
        log("[flow] guide failed and nothing to synthesize: returning model output")
        return last if last else call(prompt)

    wrapper._flow_patched = True
    cls._generate_with_tool_retry = wrapper
    log("[flow] flow_patch installed")
