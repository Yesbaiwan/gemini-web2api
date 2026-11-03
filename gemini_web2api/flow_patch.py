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
import difflib
import functools
import json
import os
import posixpath
import re
import uuid
from collections import Counter

from . import tools as T
from .config import CONFIG
from concurrent.futures import ThreadPoolExecutor

from .gemini import log, generate

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
    # CHANGE: max edit/write calls per turn before the proxy stops the
    # edit-test loop and makes the model report honestly.
    "flow_max_acts": 6,
    "flow_answer_directive": True,
    # Review lớn: chia nội dung đã đọc thành nhóm nhỏ, mỗi nhóm một lượt gọi
    # model (chỉ để tìm lỗi), proxy kiểm chứng trích dẫn rồi tự gộp báo cáo.
    "flow_mapreduce": True,
    "flow_mapreduce_min_bytes": 30000,
    "flow_chunk_bytes": 30000,
    "flow_chunk_files": 4,
    "flow_map_workers": 2,
    # Model riêng cho bước map (tìm lỗi). None = dùng model của request.
    # Vd "gemini-3.5-flash" (cần ticket 'flash' trong model_tickets).
    "flow_map_model": None,
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


def _base_test_command(listing):
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


def _base_keywords(text, limit=6):
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

# ---- flow_fix: act gating, test-command detection, grep ranking ----------
_ACT_FAIL_RE = re.compile(
    r"(?:^\s*<?error|\bfailed\b|old_?string.{0,40}not found|"
    r"could not find|cannot find|not found in|multiple matches|"
    r"found \d+ matches|must be different|no changes)", re.IGNORECASE)
_OLD_KEYS = ("oldString", "old_string", "oldText", "old_text", "old_str")
_NEW_KEYS = ("newString", "new_string", "newText", "new_text", "new_str")
_LINENO_RE = re.compile(r"^\s*\d+(?:: ?|\s*\| ?|\t|\u2192)")


def _act_ok(e):
    # An edit/write whose result reports a failure must not count as done.
    return not _ACT_FAIL_RE.search((e.get("result") or "")[:400])


def _failed_act_hint(ev):
    for e in reversed(ev):
        if e["kind"] == "act":
            if _act_ok(e):
                return ""
            msg = " ".join((e.get("result") or "").split())[:300]
            return (" Your previous edit FAILED with: " + msg +
                    " -- re-copy the exact text from the file you read.")
    return ""


def _grep_hits(ev, files):
    # {file in `files`: number of grep hits} from this turn's search results.
    counts = {}
    for e in ev:
        if e["kind"] != "search":
            continue
        body = e["result"].replace("\\", "/")
        for hf in T._files_in(body):
            counts[hf] = counts.get(hf, 0) + body.count(hf)
    hits = {}
    for f in files:
        n = sum(c for hf, c in counts.items() if T._path_seen(hf, [f]))
        if n:
            hits[f] = n
    return hits


def _strip_linenos(text):
    # All-or-nothing: only strip when EVERY non-empty line carries a prefix.
    lines = text.split("\n")
    nonempty = [ln for ln in lines if ln.strip()]
    if nonempty and all(_LINENO_RE.match(ln) for ln in nonempty):
        return "\n".join(_LINENO_RE.sub("", ln, count=1) for ln in lines)
    return text


def _clean_file_text(text):
    lines = text.replace("\r\n", "\n").split("\n")
    if T._already_numbered(lines):
        lines = [_LINENO_RE.sub("", ln, count=1) for ln in lines]
    return "\n".join(lines)


def _base_gate_act_calls(calls, messages):
    # Check edit calls against the file text the model actually read.
    # Returns (calls, problems). Line-number prefixes copied into
    # oldString/newString are stripped silently; a genuine mismatch is
    # reported (with the closest real line) so the caller can retry.
    texts = {}
    for e in _answered_calls(messages):
        if e["kind"] == "read" and _read_ok(e):
            p = _call_path(e["args"])
            if p:
                texts[p] = _clean_file_text(e["result"])
    problems, out = [], []
    for tc in calls:
        fn = tc.get("function") or {}
        if tool_kind(fn.get("name")) != "act":
            out.append(tc)
            continue
        args = T._tool_arguments_dict(fn)
        path = _call_path(args)
        okey = next((k for k in _OLD_KEYS
                     if isinstance(args.get(k), str) and args[k]), None)
        src_text = next((t for p, t in texts.items()
                         if path and T._path_seen(path, [p])), None)
        if not (okey and src_text is not None):
            out.append(tc)           # write/new file/unread file: nothing to check
            continue
        old = args[okey].replace("\r\n", "\n")
        hay = src_text.replace("\r\n", "\n")
        if old in hay or old.strip("\n") in hay:
            out.append(tc)
            continue
        stripped = _strip_linenos(old)
        if stripped != old and (stripped in hay or stripped.strip("\n") in hay):
            args[okey] = stripped
            for nk in _NEW_KEYS:
                if isinstance(args.get(nk), str):
                    args[nk] = _strip_linenos(args[nk])
            log(f"[flow] edit gate: stripped line-number prefixes in {path}")
            out.append({**tc, "function": {
                **fn, "arguments": json.dumps(args, ensure_ascii=False)}})
            continue
        first = next((ln.strip() for ln in old.split("\n") if ln.strip()), "")
        cands = [ln.strip() for ln in hay.split("\n") if ln.strip()]
        near = difflib.get_close_matches(first, cands, n=1, cutoff=0.6)
        problems.append(f"{path}: oldString does not appear in the file as read"
                        + (f" (closest real line: {near[0]!r})" if near else ""))
        out.append(tc)
    return out, problems


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


def _base_is_code_question(text):
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


def _base_analyze(messages):
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

def _answer_step(pattern, ev, total, index, skipped=None, final=False):
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
    elif pattern == "CHANGE" and final:
        instr = ("Edit budget exhausted. Do NOT call any tool. Reply in plain "
                 "text: what was changed, whether the tests passed, and exactly "
                 "what still fails. Do not claim success you have not seen.")
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
    st = Step(pattern, "answer", index, total, (), instr)
    st.skipped = list(skipped or [])
    return st


def _read_calls(tm, files):
    calls = [tm.make_call("read", path=f) for f in files]
    return calls if calls and all(calls) else None


def _base_plan_for(messages, tm, ctx=None):
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
    test_cmd = _test_command(listing + named + [
        p for p in (_call_path(e["args"]) for e in ev) if p])

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
            kws = _keywords(ctx.request)
            if (kws and tm.first("search")
                    and not any(e["kind"] == "search" for e in ev)):
                scall = tm.make_call("search", pattern="(?i)" + "|".join(
                    re.escape(k) for k in kws[:3]))
                if scall:
                    skinds = {"survey", "search", "exec"}
                    return Step(p, "search", 2, 5, skinds,
                                "Locate the code to change with grep first.",
                                [scall], True, tm.names(skinds))
            core = _core_sources(listing, False, 200)
            ranked = _rank(core, kws, _grep_hits(ev, core))[:2]
            if ranked:
                return read_step(ranked, 2, 5)
            return read_step(core[:2], 2, 5, drive=False) if core else Step(
                p, "read", 2, 5, {"read"},
                "Read the file(s) you are going to change.", None, False,
                tm.names({"read"}))
        acts = [e for e in ev if e["kind"] == "act"]
        if len(acts) >= int(_cfg("flow_max_acts")) and (
                not _act_ok(acts[-1]) or _tests_ran_after_act(ev)):
            return _answer_step(p, ev, 5, 5, final=True)
        if tm.first("act") and (not any(_act_ok(e) for e in acts)
                                or (acts and not _act_ok(acts[-1]))):
            return Step(p, "act", 3, 5, {"act"},
                        "Make the change now with ONE edit/write tool_call per "
                        "file, copying old text exactly from the file you read "
                        "(no line-number prefixes)." + _failed_act_hint(ev),
                        None, False, tm.names({"act"}))
        edited_src = any(e["kind"] == "act" and _act_ok(e)
                         and (_call_path(e["args"]) or "")
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
# Map-reduce review: model yếu chỉ nhìn 1-4 file mỗi lượt
# --------------------------------------------------------------------------

def _review_materials(messages):
    ev = _answered_calls(messages)
    files, seen = [], set()
    for e in ev:
        if e["kind"] == "read" and _read_ok(e):
            p = _call_path(e["args"])
            if p and p not in seen:
                seen.add(p)
                files.append((p, e["result"]))
    tests = [e for e in ev if e["kind"] == "exec"
             and _is_test_cmd(_call_command(e["args"]))]
    return files, tests


def _make_chunks(files, max_bytes, max_files):
    """Nhóm file theo thư mục; file quá lớn được cắt theo dòng."""
    out, cur, size = [], [], 0
    for p, body in sorted(files, key=lambda x: x[0]):
        n = len(body.encode("utf-8", "ignore"))
        if n > max_bytes:
            if cur:
                out.append(cur)
                cur, size = [], 0
            piece, psize, part = [], 0, 1
            for ln in body.split("\n"):
                piece.append(ln)
                psize += len(ln.encode("utf-8", "ignore")) + 1
                if psize >= max_bytes:
                    out.append([(f"{p} (part {part})", "\n".join(piece))])
                    piece, psize, part = [], 0, part + 1
            if piece:
                out.append([(f"{p} (part {part})", "\n".join(piece))])
            continue
        if cur and (size + n > max_bytes or len(cur) >= max_files):
            out.append(cur)
            cur, size = [], 0
        cur.append((p, body))
        size += n
    if cur:
        out.append(cur)
    return out


def _map_prompt(request, chunk):
    parts = ["You are a meticulous code reviewer. User request: "
             + request.strip()[:300] + "\n\n"
             "Find CRITICAL bugs ONLY in the files below (crash, data loss or "
             "corruption, security hole, race condition, wrong logic). Rules:\n"
             "- Report only what is visible in the text below.\n"
             "- One finding per line, exactly: - path:line -- `exact quoted code "
             "line` -- why it is a bug\n"
             "- The quote MUST be copied verbatim from the file (without the "
             "line number).\n"
             "- If you find nothing critical, output exactly: NONE\n"
             "- No tool calls, no summary of what the code does, no praise.\n"]
    for p, body in chunk:
        parts.append(f"\n===== FILE: {p} =====\n{body}\n===== END {p} =====")
    return T.defuse_extension_words("".join(parts))


def _split_findings(text):
    items, cur = [], None
    for ln in (text or "").splitlines():
        if re.match(r"^\s*(?:[-*\u2022]|\d+[.)])\s+\S", ln):
            if cur:
                items.append(cur)
            cur = ln.strip()
        elif cur and ln.strip():
            cur += " " + ln.strip()
    if cur:
        items.append(cur)
    return items


def _verify_findings(text, chunk):
    """(kept, dropped): chỉ giữ finding có trích dẫn nằm thật trong chunk."""
    corpus = T._norm_code("\n".join(b for _p, b in chunk))
    kept, dropped = [], 0
    if re.fullmatch(r"\W*NONE\W*", (text or "").strip(), re.IGNORECASE):
        return kept, dropped
    for item in _split_findings(text):
        snippets = re.findall(r"`([^`\n]{8,})`", item)
        if snippets and all(T._norm_code(x) in corpus for x in snippets):
            kept.append(item)
        else:
            dropped += 1
    return kept, dropped


def _test_summary(tests):
    if not tests:
        return []
    body = tests[-1]["result"]
    pat = re.compile(
        r"^(?:\S\s+|#\s*)?(?:tests?|suites?|pass(?:ed)?|fail(?:ed)?|cancelled|skipped|"
        r"todo|duration_ms)\s*[:=]?\s*\d+|^Ran \d+|^OK\b|^FAILED\b|"
        r"^(?:\S\s+)?\d+\s+(?:passed|failed|passing|failing)\b|"
        r"^Tests?:\s+\d+|^test result:", re.IGNORECASE)
    lines = [re.sub(r"^(?:\S\s+|#\s*)(?=[A-Za-z0-9])", "", ln.strip()) for ln in body.splitlines()
             if pat.search(ln.strip())]
    keep = [ln for ln in lines
            if not ln.lower().startswith(("duration", "suites"))]
    return keep[:6] or [body.strip()[-200:]]


def _map_reduce(messages, step, ctx, gen, client_gone=None):
    files, tests = _review_materials(messages)
    total = sum(len(b.encode("utf-8", "ignore")) for _p, b in files)
    if not files or total < int(_cfg("flow_mapreduce_min_bytes")):
        return None
    chunks = _make_chunks(files, int(_cfg("flow_chunk_bytes")),
                          int(_cfg("flow_chunk_files")))
    log(f"[flow] map-reduce review: {len(files)} files, {total} bytes, "
        f"{len(chunks)} chunk(s)")

    def work(chunk):
        if client_gone is not None and client_gone.is_set():
            return None, "client disconnected"
        for attempt in range(2):
            try:
                text = gen(_map_prompt(ctx.request, chunk))
                if text and not T.looks_like_canned_failure(text):
                    return text, None
            except Exception as e:
                err = str(e)
                log(f"[flow] map chunk attempt {attempt + 1} failed: {e}")
        return None, "upstream failed"

    workers = max(1, int(_cfg("flow_map_workers")))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(work, chunks))

    findings, dropped, failed, leads, clean = [], 0, [], [], 0
    for chunk, (text, err) in zip(chunks, results):
        names = ", ".join(p for p, _b in chunk)
        if text is None:
            failed.append(names)
            continue
        kept, d = _verify_findings(text, chunk)
        findings.extend(kept)
        dropped += d
        is_none = bool(re.fullmatch(r"\W*NONE\W*", (text or "").strip(),
                                    re.IGNORECASE))
        if is_none:
            clean += 1
        elif not kept and not d:
            # Văn xuôi / định dạng lạ: không xác minh được -> KHÔNG tính là sạch.
            dropped += 1
            leads.append(" ".join(text.split())[:220])
        if d:
            kept_set = set(kept)
            leads.extend(x[:220] for x in _split_findings(text)
                         if x not in kept_set)
    vi = bool(_VI_RE.search(ctx.last.lower()))
    read = [p for p, _b in files]
    skipped = list(getattr(step, "skipped", []) or [])
    out = []
    summary = _test_summary(tests)
    if vi:
        out.append(f"**Kết quả review** ({len(files)} file, {len(chunks)} nhóm, "
                   "mỗi nhóm được soi riêng; trích dẫn đã đối chiếu với source)")
        if summary:
            out.append("Test: " + " | ".join(summary))
        if findings:
            out.append("**Phát hiện:**\n" + "\n".join(findings))
        else:
            out.append("Không tìm thấy critical bug nào có trích dẫn xác minh "
                       "được trong các file đã đọc.")
        if dropped:
            out.append(f"({dropped} nhận định bị loại vì trích dẫn không khớp source; "
                       "chỉ là nghi vấn chưa xác minh, cần tự kiểm tra:)\n"
                       + "\n".join(leads[:3]))
        out.append(f"{clean}/{len(chunks)} nhóm model trả về 'không có lỗi'; đó là ý kiến "
                   "của model nhỏ, không phải bằng chứng.")
        out.append("File đã đọc: " + ", ".join(read))
        if skipped or failed:
            out.append("**Chưa review đầy đủ (coverage một phần):** "
                       + ", ".join(skipped + failed))
        out.append("Lưu ý: kết quả test pass không chứng minh không có bug; đây "
                   "là review tự động bằng model nhỏ, nên kiểm tra lại các điểm quan trọng.")
    else:
        out.append(f"**Review result** ({len(files)} files, {len(chunks)} groups, "
                   "each reviewed separately; quotes verified against the source)")
        if summary:
            out.append("Tests: " + " | ".join(summary))
        if findings:
            out.append("**Findings:**\n" + "\n".join(findings))
        else:
            out.append("No critical bug with a verifiable quote was found in the "
                       "files read.")
        if dropped:
            out.append(f"({dropped} claim(s) dropped, quote not found in the source; "
                       "unverified leads, check manually:)\n" + "\n".join(leads[:3]))
        out.append(f"{clean}/{len(chunks)} groups returned 'no bug'; that is a small "
                   "model's opinion, not evidence.")
        out.append("Files read: " + ", ".join(read))
        if skipped or failed:
            out.append("**Partial coverage, not fully reviewed:** "
                       + ", ".join(skipped + failed))
        out.append("Note: passing tests do not prove the absence of bugs; this is "
                   "an automated small-model review, double-check important areas.")
    return "\n\n".join(out)


# --------------------------------------------------------------------------
# Hook vào GeminiHandler._generate_with_tool_retry
# --------------------------------------------------------------------------

def _map_model(model_id, think_mode, extra, ticket):
    """(model_id, think, extra, ticket) cho bước map; flow_map_model nếu có."""
    name = _cfg("flow_map_model")
    if not name:
        return model_id, think_mode, extra, ticket
    from .models import resolve_model, ticket_for
    n, m_id, m_think, err, m_extra = resolve_model(
        name, default=CONFIG["default_model"])
    if err:
        log(f"[flow] flow_map_model '{name}' invalid ({err}); using request model")
        return model_id, think_mode, extra, ticket
    log(f"[flow] map step uses model '{n}'")
    return m_id, m_think, m_extra, ticket_for(n)


def install():
    """Monkeypatch server.GeminiHandler. Idempotent."""
    from . import server
    for k, v in _DEFAULTS.items():
        CONFIG.setdefault(k, v)
    cls = server.GeminiHandler
    _fx_install(server)
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
            if (_cfg("flow_mapreduce")
                    and step.pattern in ("GENERIC_REVIEW", "FILE_REVIEW")):
                try:
                    m_id, m_think, m_extra, m_ticket = _map_model(
                        model_id, think_mode, extra, ticket)
                    report = _map_reduce(
                        messages, step, ctx,
                        lambda p: generate(p, m_id, m_think, None,
                                           m_extra, m_ticket), client_gone)
                except Exception as e:
                    log(f"[flow] map-reduce failed, using single answer: {e}")
                    report = None
                if report:
                    return report, None
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
            if ok and step.name == "act":
                ok, problems = _gate_act_calls(ok, messages)
                if problems:
                    log("[flow] edit gate: " + " | ".join(problems))
                    if attempt < max(0, int(_cfg("flow_max_retries"))):
                        nudge = (" Your edit was rejected before sending: "
                                 + " | ".join(problems) + " Copy oldString "
                                 "EXACTLY from the file you read, without "
                                 "line-number prefixes.")
                        continue
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


# ==== fix_patch:begin (do fix_patch.py sinh ra; chạy lại script để cập nhật) ====
_FX_DEFAULTS = {
    "fix_enabled": True,
    "fix_debug_flow": True,
    "fix_create_flow": True,
    "fix_diff_review": True,
    "fix_read_docs_for_change": True,
    "fix_read_related_tests": True,
    "fix_max_test_attempts": 3,
    "fix_write_shrink_ratio": 0.5,
}
_DEFAULTS.update(_FX_DEFAULTS)


def _c(key):
    return CONFIG.get(key, _FX_DEFAULTS[key])


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
        if any(s in _IGNORE_SEG for s in n.split("/")[:-1]):
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
    configured = (_cfg("flow_test_command") or "").strip()
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
        cmd = _call_command(e["args"])
        if _is_test_cmd(cmd):
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
    return {_call_path(e["args"]): e["result"] for e in ev
            if e["kind"] == "read" and _read_ok(e) and _call_path(e["args"])}


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
    if _FX_ORIG_CD(messages):
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
            if not f.lower().endswith(_SRC_EXT)
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


_STOP2 = set(_STOP) | set(
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


_FX_QUESTION_RE = re.compile(
    r"\b(how|where|why|what|which|explain|does|show me|giai thich|o dau|nhu the nao"
    r"|hoat dong|la gi|tai sao|dinh nghia|tim hieu|mo ta|tom tat)\b")
_FX_PROJECT_RE = re.compile(
    r"\b(this|the|our|nay|cua)\s+(project|repo|repository|codebase|code|proxy|server"
    r"|module|function|class|file|source|flow|pipeline|system|app)\b"
    r"|\b(du an|repo|codebase|ma nguon|source|source code|trong code|ham|class|module"
    r"|kien truc|architecture|luong|flow|pipeline)\b")


def _is_code_question(text):
    scannable = T._strip_code_spans(text)
    folded = re.sub(r"\s+", " ", T._fold_diacritics(scannable.lower())).strip()
    if not _FX_QUESTION_RE.search(folded):
        return False
    has_ident = bool(re.search(r"`[^`\n]{2,}`", text or ""))
    has_file = any(f for f in T._files_in(scannable)
                   if f.lower() not in T._NON_FILE_NAMES)
    return has_ident or has_file or bool(_FX_PROJECT_RE.search(folded))


def _analyze(messages):
    ctx = _base_analyze(messages)
    if ctx is None or not _c("fix_enabled"):
        return ctx
    ctx.fix = False
    ctx.create_merge = False
    ctx.target = ""
    ctx.dir_cands = []
    try:
        if _clarified(messages, ctx.idx):
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
    st = Step(pat, "answer", index, total, (), instruction)
    st.skipped = []
    return st


def _read_step(pat, tm, files, index, total, drive=True, instruction=None):
    calls = _read_calls(tm, files)
    return Step(pat, "read", index, total, {"read"},
                  instruction or ("Read these files fully before answering: "
                                  + ", ".join(files) + "."),
                  calls, drive, tm.names({"read"}))


def _survey_step(pat, tm, ev, index, total):
    n = _survey_count(ev)
    glob_call = (tm.make_call("survey", pattern=(_cfg("flow_survey_pattern")
                                                 if n == 0 else "**/*"))
                 if n < 2 else None)
    exec_call = tm.make_call("exec", command="git ls-files",
                             description="List project files")
    call = glob_call or exec_call
    kinds = {"survey", "search", "exec"}
    if not call and not tm.names(kinds):
        return None
    return Step(pat, "survey", index, total, kinds,
                  "List the project files before anything else.",
                  [call] if call else None, True, tm.names(kinds))


def _tests_step(pat, tm, cmd, index, total,
                instruction="Run the unit tests."):
    call = tm.make_call("exec", command=cmd, description="Run unit tests")
    if not call:
        return None
    return Step(pat, "tests", index, total, {"exec"}, instruction, [call],
                  True, tm.names({"exec"}))


def _code_reads(read_set):
    return [p for p in read_set if not _is_doc(p)]


def _matching_tests(read_set, listing):
    tests = [f for f in listing if _is_test_path(f)
             and f.lower().endswith(_SRC_EXT)]
    out = []
    for p in read_set:
        if _is_test_path(p) or _is_doc(p) or not p.endswith(_SRC_EXT):
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
    max_acts = int(_cfg("flow_max_acts"))
    if len(acts) >= max_acts and (not _act_ok(acts[-1])
                                  or _tests_ran_after_act(ev)):
        st = _answer_step("CHANGE", ev, total, total, final=True)
        if st:
            st.pattern = pat
        return st
    ok_acts = [e for e in acts if _act_ok(e)]
    if tm.first("act") and (not ok_acts or (acts and not _act_ok(acts[-1]))):
        return Step(pat, "act", 3, total, {"act"},
                      "Make the change now with ONE edit/write tool_call per "
                      "file, copying old text exactly from the file you read "
                      "(no line-number prefixes)." + _DO_NOT_DESCRIBE
                      + _failed_act_hint(ev) + extra, None, False,
                      tm.names({"act"}))
    edited_src = any(e["kind"] == "act" and _act_ok(e)
                     and (_call_path(e["args"]) or "").lower().endswith(_SRC_EXT)
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
            return Step(pat, "act", 3, total, {"act"},
                          "The tests still FAIL after your edit. Fix the cause "
                          "with ONE more edit tool_call (copy old text exactly "
                          "from the file as it is NOW; re-read it first if you "
                          "are unsure). Failure:\n" + _excerpt(after[-1][2], 1500),
                          None, False, tm.names({"act"}))
    st = _answer_step("CHANGE", ev, total, total)
    if st:
        st.pattern = pat
    return st


def _plan_change(ctx, ev, tm, pat="CHANGE", total=5):
    listing = _listing_files(ev)
    read_set = _read_set(ev)
    batch = max(1, int(_cfg("flow_read_batch")))
    named = _named_files(ctx)
    known = _existing([_resolve(f, listing) for f in named], listing)
    paths = [x for x in (_call_path(e["args"]) for e in ev) if x]
    names_all = listing + named + paths
    can_read = bool(tm.first("read"))

    if (not known or pat == "CREATE") and not _surveyed(ev):
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
                return Step(pat, "search", 2, total, skinds,
                              "Locate the code to change with grep first.",
                              [scall], True, tm.names(skinds))
        core = _core_sources(listing, False, 200)
        ranked = _rank(core, kws, _grep_hits(ev, core))[:2]
        if ranked:
            return _read_step(pat, tm, ranked, 2, total)
        if core:
            return _read_step(pat, tm, core[:2], 2, total, drive=False)
        if pat != "CREATE":
            return Step(pat, "read", 2, total, {"read"},
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
    pk = _pick(props, _PATH_KEYS) or (None if props else "filePath")
    ck = _pick(props, ["content", "contents", "text", "file_text", "filetext",
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
    if not _surveyed(ev):
        s = _survey_step(pat, tm, ev, 1, total)
        if s:
            return s
    listing = _listing_files(ev)
    srcs, prefix = _merge_sources(ctx, listing)
    if not srcs:
        return fallback()
    target = _norm(ctx.target)
    root = posixpath.dirname(prefix)
    out_path = target if "/" in target else (posixpath.join(root, target) if root else target)
    ext = (posixpath.splitext(target)[1] or ".py").lower()
    names = ", ".join(_base(f) for f in srcs)

    def run_events(word):
        return [e for e in ev if e["kind"] == "exec" and word in _call_command(e["args"])]

    wrote = [e for e in ev if e["kind"] == "act"
             and _MERGE_SCRIPT in (_call_path(e["args"]) or "")]
    if not wrote:
        call = _write_call(tm, _MERGE_SCRIPT, merge_script(srcs, out_path, ext))
        if not call:
            return fallback()
        return Step(pat, "merge", 2, total, {"act"},
                      "Create the helper script that merges " + names + ".",
                      [call], True, tm.names({"act"}))
    if not _act_ok(wrote[-1]):
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
        return Step(pat, "merge", 3, total, {"exec"}, "Run the merge script.",
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
                return Step(pat, "verify", 4, total, {"exec"},
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
    listing = _listing_files(ev)
    read_set = _read_set(ev)
    batch = max(1, int(_cfg("flow_read_batch")))
    paths = [x for x in (_call_path(e["args"]) for e in ev) if x]
    names_all = listing + paths
    can_read = bool(tm.first("read"))
    pasted = ctx.last if _TRACE_RE.search(ctx.last or "") else ""
    fix = bool(getattr(ctx, "fix", False))

    if not pasted and not listing and _survey_count(ev) < 2:
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
                     and "git" in _call_command(e["args"])
                     and word in _call_command(e["args"])), None)

    def git_step(cmd, index, why):
        call = tm.make_call("exec", command=cmd, description=why)
        if not call:
            return None
        return Step(pat, "git", index, total, {"exec"}, why + ".", [call],
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
    read_set = _read_set(ev)
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
    listing = _listing_files(ev)
    read_set = _read_set(ev)
    can_read = bool(tm.first("read"))
    named = _named_files(ctx)
    known = [_resolve(f, listing) for f in named]
    if known and can_read:
        unread = [f for f in known if not T._path_seen(f, read_set)]
        if unread:
            return _read_step(pat, tm, unread[:6], 1, total)
    code_read = _code_reads(read_set)
    kws = _keywords(ctx.request)
    if not _surveyed(ev):
        call = None
        if kws and tm.first("search"):
            call = tm.make_call("search", pattern="(?i)" + "|".join(
                re.escape(k) for k in kws[:3]))
        if call:
            kinds = {"survey", "search", "exec"}
            return Step(pat, "search", 1, total, kinds,
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
            pool = _core_sources(pool, True, 400) or pool
            ranked = _rank(pool, kws, hits)[:2] or pool[:1]
            if ranked:
                return _read_step(pat, tm, ranked, 2, total)
        if not listing and _survey_count(ev) < 2:
            s = _survey_step(pat, tm, ev, 2, total)
            if s:
                return s
        docs = [d for d in _doc_files(listing) if not T._path_seen(d, read_set)]
        if docs:
            return _read_step(pat, tm, docs[:2], 2, total,
                              instruction="Read the project docs first: "
                                          + ", ".join(docs[:2]) + ".")
        entry = [f for f in _core_sources(listing, False, 200)
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
    ctx = ctx or _analyze(messages)
    if ctx is None or not ctx.pattern:
        return None
    ev = _answered_calls(messages)
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
    return _base_plan_for(messages, tm, ctx)


def plan_for(messages, tm, ctx=None):
    if not _c("fix_enabled"):
        return _base_plan_for(messages, tm, ctx)
    try:
        return _plan(messages, tm, ctx)
    except Exception as e:      # never let the planner break a request
        log(f"[fix] planner error, using the original planner: {e}")
        return _base_plan_for(messages, tm, ctx)


_CONTENT_KEYS = ("content", "contents", "text", "fileText", "file_text")


def _gate_act_calls(calls, messages):
    out, problems = _base_gate_act_calls(calls, messages)
    try:
        ratio = float(_c("fix_write_shrink_ratio"))
        texts = {}
        for e in _answered_calls(messages):
            if e["kind"] == "read" and _read_ok(e):
                p = _call_path(e["args"])
                if p:
                    texts[p] = _clean_file_text(e["result"])
        for tc in out:
            fn = tc.get("function") or {}
            if tool_kind(fn.get("name")) != "act":
                continue
            args = T._tool_arguments_dict(fn)
            if any(isinstance(args.get(k), str) and args[k] for k in _OLD_KEYS):
                continue                      # an edit: already checked
            new = next((args[k] for k in _CONTENT_KEYS
                        if isinstance(args.get(k), str)), None)
            path = _call_path(args)
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


_SURVEY_CMD = "git ls-files"


def _test_command(listing):
    return (test_candidates(list(listing or [])) or [None])[0]


_FX_ORIG_CD = None


def _fx_install(server):
    """Gọi từ install(): mở rộng tools.change_demanded (và bản import trong server)."""
    global _FX_ORIG_CD
    for k, v in _FX_DEFAULTS.items():
        CONFIG.setdefault(k, v)
    if not CONFIG.get("fix_enabled", True):
        return
    cur = T.change_demanded
    if cur is _change_demanded:
        return
    _FX_ORIG_CD = cur
    T.change_demanded = _change_demanded
    if getattr(server, "change_demanded", None) is cur:
        server.change_demanded = _change_demanded
    log("[fix] change_demanded extended for create/merge requests")
# ==== fix_patch:end ====


# ==== flowfix2:begin (generated by flow_fix2_patch.py -- re-run to refresh, --revert to remove) ====
# Overrides (later definitions win: every caller resolves these names at call time).
_ff2_orig_gate_act_calls = _gate_act_calls
_ff2_orig_plan_create = _plan_create


# --------------------------------------------------------------------------
# 1+2. Listing: keep drive letters, read grep headers, drop truncated twins
# --------------------------------------------------------------------------

_FF2_GREP_HEAD_RE = re.compile(
    r'^\s*((?:[A-Za-z]:)?[^:*?"<>|\r\n]+?\.[A-Za-z0-9]{1,8})\s*:\s*$')
_FF2_GREP_INLINE_RE = re.compile(
    r'^\s*((?:[A-Za-z]:)?[^:*?"<>|\r\n]+?\.[A-Za-z0-9]{1,8}):\d+[:\-]')


def _listing_files(ev):
    """File paths from survey/search results (glob, ls, git ls-files, grep).

    Order of trust: whole-line paths (glob/ls) > grep header lines
    ``D:\\dir\\file.py:`` > ``path:line:text`` > scraping file-like tokens from
    the text. Scraping is the last resort because it cannot see the drive
    letter: ``D:/Project/x/README.md`` came out as ``Project/x/README.md``,
    which then won the "shortest path" tie-break and failed to read.
    """
    seen, out = set(), []

    def add(f):
        f = f.replace("\\", "/").strip()
        base = f.rsplit("/", 1)[-1].lower()
        if f and f not in seen and base not in T._NON_FILE_NAMES:
            seen.add(f)
            out.append(f)

    for e in ev:
        if not (e["kind"] in ("survey", "search") or (
                e["kind"] == "exec"
                and _LIST_CMD_RE.search(_call_command(e["args"])))):
            continue
        lines = e["result"].splitlines()
        matched = []
        for rx in (_PATH_LINE_RE, _FF2_GREP_HEAD_RE, _FF2_GREP_INLINE_RE):
            matched = [m.group(1) for m in map(rx.match, lines) if m]
            if matched:
                break
        if matched:
            for f in matched:
                add(f)
        else:
            body = e["result"].replace("\\", "/")
            for f in sorted(T._files_in(body), key=body.find):
                add(f)
    low = [f.lower() for f in out]
    return [f for f, l in zip(out, low)
            if not any(o != l and o.endswith("/" + l) for o in low)]


# --------------------------------------------------------------------------
# 1. Which files does "merge everything in X" mean?
# --------------------------------------------------------------------------

def _merge_sources(ctx, listing):
    """``(files, prefix)`` to merge, or ``([], "")``.

    Candidate directories, in order: words the request marks as folders, EVERY
    word of the request that equals a directory in the listing, the target
    file's own stem, and finally the biggest Python package (a directory with
    ``__init__.py``). The old version needed the word "folder"/"thu muc".
    """
    target = _norm(getattr(ctx, "target", "") or "")
    ext = (posixpath.splitext(target)[1] or ".py").lower()
    live = [f for f in _live(listing) if f.lower().endswith(ext)]
    tbase = posixpath.basename(target).lower()

    def usable(f):
        return (not _is_test_path(f) and _base(f) != tbase
                and _base(f) != _MERGE_SCRIPT)

    def group(dl):
        groups = {}
        for f in live:
            segs = f.split("/")
            for i, s in enumerate(segs[:-1]):
                if s.lower() == dl:
                    groups.setdefault("/".join(segs[:i + 1]), []).append(f)
        return groups

    def pick(groups):
        if not groups:
            return None
        prefix = max(groups, key=lambda k: (
            sum(1 for f in groups[k] if posixpath.dirname(f) == k), len(k)))
        files = sorted({f for f in groups[prefix] if usable(f)})
        return (files, prefix) if len(files) >= 2 else None

    cands = list(getattr(ctx, "dir_cands", []) or [])
    raw = getattr(ctx, "last", "") or ""
    for tok in re.findall(r"[A-Za-z0-9_.\-]+", raw):
        tok = tok.strip(".-")
        if len(tok) >= 3:
            cands.append(tok)
    stem_t = posixpath.splitext(posixpath.basename(target))[0]
    if stem_t:
        cands.append(stem_t)
    seen = set()
    for cand in cands:
        dl = _norm(cand).strip("/").split("/")[-1].lower()
        if not dl or dl in seen:
            continue
        seen.add(dl)
        hit = pick(group(dl))
        if hit:
            return hit
    if ext == ".py":
        best = None
        pkgs = sorted({posixpath.dirname(f) for f in live
                       if _base(f) == "__init__.py" and not _is_test_path(f)},
                      key=len)
        for d in pkgs:
            files = sorted(f for f in live
                           if f.startswith(d + "/") and usable(f))
            if len(files) >= 2 and (best is None or len(files) > len(best[0])):
                best = (files, d)
        if best:
            return best
    return [], ""


# --------------------------------------------------------------------------
# 3. merge_script: real single-file bundle for packages with relative imports
# --------------------------------------------------------------------------

_FF2_MERGE_TMPL = r'''# -*- coding: utf-8 -*-
# Temporary helper generated by the proxy; it deletes itself when done.
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile

SRC = @@SRC@@
OUT = @@OUT@@
C = @@C@@
IS_PY = @@PY@@

LOADER = r"""
import importlib.abc
import importlib.util
import linecache
import sys


class _BundleLoader(importlib.abc.Loader):
    def __init__(self, name):
        self._name = name

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        text, is_pkg, fname = _MODULES[self._name]
        module.__file__ = fname
        if is_pkg:
            module.__path__ = []
        linecache.cache[fname] = (len(text), None, text.splitlines(True), fname)
        exec(compile(text, fname, "exec", dont_inherit=True), module.__dict__)

    def get_source(self, fullname):
        return _MODULES[fullname][0]


class _BundleFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        entry = _MODULES.get(fullname)
        if entry is None:
            return None
        return importlib.util.spec_from_loader(
            fullname, _BundleLoader(fullname), is_package=entry[1])


if not any(isinstance(_f, _BundleFinder) for _f in sys.meta_path):
    sys.meta_path.insert(0, _BundleFinder())
"""


def stem(p):
    return os.path.splitext(os.path.basename(p))[0]


def read(p):
    with io.open(p, "r", encoding="utf-8") as fh:
        return fh.read()


def write(path, data):
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(data)


def plain(s):
    return str(s).encode("ascii", "replace").decode("ascii")


def smoke(path, name):
    # Run the bundle from a clean temp dir that has NO package folder next to
    # it: proves it is standalone, not just importable from the repo.
    tmp = tempfile.mkdtemp(prefix="bundle_smoke_")
    try:
        copy = os.path.join(tmp, name)
        shutil.copyfile(path, copy)
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env.pop("PYTHONPATH", None)
        probe = ("import importlib.util as u, sys\n"
                 "s = u.spec_from_file_location('_bundle', sys.argv[1])\n"
                 "m = u.module_from_spec(s)\n"
                 "s.loader.exec_module(m)\n"
                 "for n in m._MODULES:\n"
                 "    __import__(n)\n"
                 "print(len(m._MODULES))\n")
        runs = []
        for argv in ([sys.executable, "-c", probe, copy],
                     [sys.executable, copy, "--version"]):
            r = subprocess.run(argv, cwd=tmp, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=180)
            runs.append((r.returncode,
                         r.stdout.decode("utf-8", "replace").strip(),
                         r.stderr.decode("utf-8", "replace").strip()))
        (rc1, o1, e1), (rc2, o2, e2) = runs
        if rc1 != 0:
            return False, "importing the bundled modules failed:\n" + (e1 or o1)[-1500:]
        if "Traceback" in e2:
            return False, "running the bundle with --version failed:\n" + e2[-1500:]
        first = (o2 or e2).splitlines()
        return True, "all %s modules import in a clean dir; --version rc=%d %s" % (
            o1.splitlines()[-1] if o1 else "?", rc2, plain(first[0] if first else ""))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


names = {stem(p): p for p in SRC}
texts = {p: read(p) for p in SRC}
inits = sorted((p for p in SRC if os.path.basename(p) == "__init__.py"), key=len)
rel_import = re.compile(r"(?m)^[ \t]*from[ \t]+\.")
use_bundle = IS_PY and bool(inits) and any(rel_import.search(t) for t in texts.values())


def build_bundle():
    root = os.path.dirname(inits[0])
    pkg = os.path.basename(root)
    mods = []
    for p in sorted(SRC):
        rel = os.path.relpath(p, root).replace(os.sep, "/")
        if rel.startswith(".."):
            mods.append((stem(p), False, texts[p], os.path.basename(p)))
            continue
        parts = rel[:-3].split("/")
        is_pkg = parts[-1] == "__init__"
        if is_pkg:
            parts = parts[:-1]
        mods.append((".".join([pkg] + parts), is_pkg, texts[p], pkg + "/" + rel))
    entry = pkg + ".__main__"
    has_entry = any(m[0] == entry for m in mods)
    body = ["#!/usr/bin/env python3",
            "# -*- coding: utf-8 -*-",
            "# Single-file bundle of the %s package (%d modules), generated by the proxy." % (pkg, len(mods)),
            "# Every module source is embedded below and served by an import hook, so",
            "# relative imports keep working. Edit the package and regenerate, not this file.",
            "_MODULES = {"]
    for name, is_pkg, text, fname in mods:
        body.append("    %r: (%r, %r, %r)," % (name, text, is_pkg, fname))
    body.append("}")
    body.append(LOADER)
    if has_entry:
        body.append("")
        body.append('if __name__ == "__main__":')
        body.append("    import importlib")
        body.append("    _entry = importlib.import_module(%r)" % entry)
        body.append('    _main = getattr(_entry, "main", None)')
        body.append("    if callable(_main):")
        body.append("        _main()")
    return "\n".join(body) + "\n", [m[0] for m in mods]


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


try:
    out_dir = os.path.dirname(OUT)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    if use_bundle:
        data, mod_names = build_bundle()
        tmp_out = OUT + ".tmp"
        write(tmp_out, data)
        ok, msg = smoke(tmp_out, os.path.basename(OUT))
        if not ok:
            os.remove(tmp_out)
            print("SMOKE TEST FAILED -- the output file was NOT written")
            print(plain(msg))
            sys.exit(1)
        os.replace(tmp_out, OUT)
        print("merged %d files -> %s (%d bytes)" % (len(mod_names), OUT, len(data.encode("utf-8"))))
        print("mode: package bundle (every module embedded, import hook installed)")
        print("smoke: " + plain(msg))
        print("order: " + ", ".join(mod_names))
    else:
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
        write(OUT, data)
        print("merged %d files -> %s (%d bytes)" % (len(order), OUT, len(data.encode("utf-8"))))
        print("mode: plain concatenation")
        print("order: " + ", ".join(os.path.basename(p) for p in order))
finally:
    try:
        os.remove(__file__)
    except OSError:
        pass
'''


def merge_script(srcs, out_path, ext):
    comment = "//" if ext in _SLASH_COMMENT_EXT else "#"
    return (_FF2_MERGE_TMPL.replace("@@SRC@@", json.dumps(list(srcs)))
            .replace("@@OUT@@", json.dumps(out_path))
            .replace("@@C@@", json.dumps(comment))
            .replace("@@PY@@", "True" if ext == ".py" else "False"))


# --------------------------------------------------------------------------
# 5. Honest final answer once the bundle has been verified
# --------------------------------------------------------------------------

def _ff2_merge_output(ev):
    ran = [e for e in ev if e["kind"] == "exec"
           and _MERGE_SCRIPT in _call_command(e["args"])]
    return (ran[-1]["result"] if ran else "") or ""


def _ff2_bundle_answer(res):
    facts = " | ".join(ln.strip() for ln in res.splitlines()
                       if ln.strip())[:700]
    facts = facts.encode("ascii", "replace").decode("ascii")
    return ("DONE -- the file already exists and the helper VERIFIED it. Reply "
            "in the user's language, plainly: the package was merged into ONE "
            "standalone file. It embeds the source of every module and installs "
            "an import hook, so the relative imports keep working and the "
            "program behaves exactly as `python -m <package>` (same CLI "
            "options). It was tested in a clean temp directory with no package "
            "folder next to it: every embedded module imports and `--version` "
            "runs. Facts printed by the helper: " + facts + ". Not verified: a "
            "real request through Gemini. Mention the trade-off: sources are "
            "embedded as strings, so edit the package and regenerate rather than "
            "editing the bundle. Do NOT give the user a script or steps to do by "
            "hand; the temporary helper already deleted itself.")


def _plan_create(ctx, ev, tm):
    step = _ff2_orig_plan_create(ctx, ev, tm)
    try:
        if (step is not None and step.name == "answer"
                and (step.instruction or "").startswith(
                    "DONE -- the file already exists")):
            res = _ff2_merge_output(ev)
            if "package bundle" in res:
                step.instruction = _ff2_bundle_answer(res)
    except Exception as e:
        log(f"[fix2] bundle answer error: {e}")
    return step


# --------------------------------------------------------------------------
# 4. Refuse a stub write for a "merge files" request
# --------------------------------------------------------------------------

def _ff2_stub_problems(calls, messages):
    ctx = _analyze(messages)
    if (ctx is None or getattr(ctx, "pattern", None) != "CREATE"
            or not getattr(ctx, "create_merge", False)):
        return []
    total = 0
    for e in _answered_calls(messages):
        if e["kind"] != "read" or not _read_ok(e):
            continue
        p = _call_path(e["args"])
        if (p and not _is_doc(p) and not _is_test_path(p)
                and p.lower().endswith(_SRC_EXT)):
            total += len(_clean_file_text(e["result"]))
    problems = []
    for tc in calls:
        fn = tc.get("function") or {}
        if tool_kind(fn.get("name")) != "act":
            continue
        args = T._tool_arguments_dict(fn)
        if any(isinstance(args.get(k), str) and args[k] for k in _OLD_KEYS):
            continue
        new = next((args[k] for k in _CONTENT_KEYS
                    if isinstance(args.get(k), str)), None)
        path = _call_path(args)
        if new is None or not path or _MERGE_SCRIPT in path:
            continue
        if len(new) < max(400, total // 2):
            problems.append(
                f"{path}: this write holds only {len(new)} chars, but a merged "
                f"file must contain the COMPLETE code of every source file (the "
                f"files you read alone are {total} chars). Do not write a "
                "placeholder; read every file and include all of it, or use the "
                "bash tool to run a script that concatenates them")
    return problems


def _gate_act_calls(calls, messages):
    out, problems = _ff2_orig_gate_act_calls(calls, messages)
    try:
        problems = list(problems) + _ff2_stub_problems(out, messages)
    except Exception as e:
        log(f"[fix2] stub gate error: {e}")
    return out, problems
# ==== flowfix2:end ====


# ==== flowfix3:begin (generated by flow_fix3_patch.py -- re-run to refresh, --revert to remove) ====
# Overrides (later definitions win: every caller resolves these names at call time).
_ff3_prev_plan_create = _plan_create
_ff3_prev_install = install


def _ff3_key(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


# --------------------------------------------------------------------------
# 1. Which files does "merge everything in X" mean?  (no "folder" word needed)
# --------------------------------------------------------------------------

def _merge_sources(ctx, listing):
    target = _norm(getattr(ctx, "target", "") or "")
    ext = (posixpath.splitext(target)[1] or ".py").lower()
    live = [f for f in _live(listing) if f.lower().endswith(ext)]
    tbase = posixpath.basename(target).lower()

    def usable(f):
        return (not _is_test_path(f) and _base(f) != tbase
                and _base(f) != _MERGE_SCRIPT)

    def group(match):
        groups = {}
        for f in live:
            segs = f.split("/")
            for i, s in enumerate(segs[:-1]):
                if match(s):
                    groups.setdefault("/".join(segs[:i + 1]), []).append(f)
        return groups

    def pick(groups):
        best = None
        for prefix, fl in groups.items():
            files = sorted({f for f in fl if usable(f)})
            direct = sum(1 for f in files if posixpath.dirname(f) == prefix)
            score = (direct, len(prefix))
            if len(files) >= 2 and (best is None or score > best[0]):
                best = (score, files, prefix)
        return (best[1], best[2]) if best else None

    cands = list(getattr(ctx, "dir_cands", []) or [])
    raw = getattr(ctx, "last", "") or ""
    for tok in re.findall(r"[A-Za-z0-9_.\-]+", raw):
        tok = tok.strip(".-")
        if len(tok) >= 3:
            cands.append(tok)
    stem_t = posixpath.splitext(posixpath.basename(target))[0]
    if stem_t:
        cands.append(stem_t)
    names, seen = [], set()
    for c in cands:
        dl = _norm(c).strip("/").split("/")[-1].lower()
        if dl and dl not in seen:
            seen.add(dl)
            names.append(dl)
    # pass 1: exact directory name; pass 2: ignore '-' / '_' / '.' differences
    for exact in (True, False):
        for dl in names:
            if exact:
                hit = pick(group(lambda s, d=dl: s.lower() == d))
            else:
                k = _ff3_key(dl)
                if len(k) < 4:
                    continue
                hit = pick(group(lambda s, k=k: _ff3_key(s) == k))
            if hit:
                return hit
    if ext == ".py":
        best = None
        pkgs = sorted({posixpath.dirname(f) for f in live
                       if _base(f) == "__init__.py" and not _is_test_path(f)},
                      key=len)
        for d in pkgs:
            files = sorted(f for f in live
                           if f.startswith(d + "/") and usable(f))
            if len(files) >= 2 and (best is None or len(files) > len(best[0])):
                best = (files, d)
        if best:
            return best
    return [], ""


# --------------------------------------------------------------------------
# 2. Honest answer when the verified merge helper never ran
# --------------------------------------------------------------------------

def _ff3_merge_output(ev):
    ran = [e for e in ev if e["kind"] == "exec"
           and _MERGE_SCRIPT in _call_command(e["args"])]
    return (ran[-1]["result"] if ran else "") or ""


def _ff3_why(ctx, ev, tm):
    listing = _listing_files(ev)
    srcs, _p = _merge_sources(ctx, listing)
    return ("target=%r write_tool=%r exec_tool=%r listing=%d merge_sources=%d"
            % (getattr(ctx, "target", ""), _write_tool(tm), tm.first("exec"),
               len(listing), len(srcs)))


def _ff3_honest(ctx, ev):
    wrote = []
    for e in ev:
        if e["kind"] != "act" or not _act_ok(e):
            continue
        a = e["args"]
        p = _call_path(a)
        c = next((a[k] for k in _CONTENT_KEYS if isinstance(a.get(k), str)), None)
        if p:
            wrote.append("%s (%s chars)" % (p, len(c) if c is not None else "?"))
    read_total = 0
    for e in ev:
        if e["kind"] == "read" and _read_ok(e):
            p = _call_path(e["args"]) or ""
            if p.lower().endswith(_SRC_EXT) and not _is_doc(p) and not _is_test_path(p):
                read_total += len(_clean_file_text(e["result"]))
    return ("HONESTY RULE -- the proxy's verified merge helper did NOT run, so the "
            "single-file merge is NOT done and NOT verified. Reply in the user's "
            "language, plainly: do NOT say the files were merged, do NOT say the "
            "program runs from one file. Facts: file(s) written by you: "
            + (", ".join(wrote) or "none") + "; source text you actually read: "
            + str(read_total) + " chars (not every file); the output was never "
            "compiled or run; the unit tests passing says nothing about it. State "
            "what is missing and offer to retry the merge.")


def _plan_create(ctx, ev, tm):
    step = _ff3_prev_plan_create(ctx, ev, tm)
    try:
        if step is None or not getattr(ctx, "create_merge", False):
            return step
        if step.name not in ("search", "read", "act", "tests", "answer"):
            return step
        if _ff3_merge_output(ev):
            return step                     # helper ran: ff2 reports honestly
        log("[fix3] merge request is NOT on the verified helper path ("
            + _ff3_why(ctx, ev, tm) + "); step=" + step.name)
        if step.name in ("tests", "answer"):
            return _custom_answer("CREATE", _ff3_honest(ctx, ev), 5, 5)
    except Exception as e:
        log("[fix3] create guard error: %s" % e)
    return step


# --------------------------------------------------------------------------
# 3. Never let a stub write reach the client for a merge request
# --------------------------------------------------------------------------

def _ff3_stub_problems(calls, messages):
    ctx = _analyze(messages)
    if (ctx is None or getattr(ctx, "pattern", None) != "CREATE"
            or not getattr(ctx, "create_merge", False)):
        return []
    total = 0
    for e in _answered_calls(messages):
        if e["kind"] != "read" or not _read_ok(e):
            continue
        p = _call_path(e["args"])
        if (p and not _is_doc(p) and not _is_test_path(p)
                and p.lower().endswith(_SRC_EXT)):
            total += len(_clean_file_text(e["result"]))
    problems = []
    for tc in calls:
        fn = tc.get("function") or {}
        if tool_kind(fn.get("name")) != "act":
            continue
        args = T._tool_arguments_dict(fn)
        if any(isinstance(args.get(k), str) and args[k] for k in _OLD_KEYS):
            continue
        new = next((args[k] for k in _CONTENT_KEYS
                    if isinstance(args.get(k), str)), None)
        path = _call_path(args)
        if new is None or not path or _MERGE_SCRIPT in path:
            continue
        tiny = len(new) < max(400, total // 2)
        fake = "placeholder" in new.lower() and len(new) < 5000
        if tiny or fake:
            problems.append("%s: write of %d chars is a stub/placeholder, not a "
                            "merge (sources read: %d chars)" % (path, len(new), total))
    return problems


def _ff3_postfilter(result, messages, allowed_names, tool_schemas,
                    tool_required, tools_active):
    if not (tools_active and messages and isinstance(result, tuple)
            and len(result) == 2):
        return result
    text, calls = result
    if not calls:
        return result
    probs = _ff3_stub_problems(calls, messages)
    if not probs:
        return result
    log("[fix3] blocked a stub write for a merge request: " + " | ".join(probs))
    tm = ToolMap(allowed_names, tool_schemas, tool_required)
    ctx = _analyze(messages)
    ev = _answered_calls(messages)
    listing = _listing_files(ev)
    srcs, prefix = _merge_sources(ctx, listing)
    helper_written = any(e["kind"] == "act" and _MERGE_SCRIPT in (_call_path(e["args"]) or "")
                         for e in ev)
    if srcs and not helper_written and _write_tool(tm) and tm.first("exec"):
        target = _norm(ctx.target)
        root = posixpath.dirname(prefix)
        out_path = target if "/" in target else (
            posixpath.join(root, target) if root else target)
        ext = (posixpath.splitext(target)[1] or ".py").lower()
        call = _write_call(tm, _MERGE_SCRIPT, merge_script(srcs, out_path, ext))
        if call:
            log("[fix3] replaced the stub by the verified merge helper (%d files)"
                % len(srcs))
            return "", [call]
    why = _ff3_why(ctx, ev, tm)
    if _VI_RE.search((ctx.last or "").lower()):
        msg = ("Mình CHƯA gộp được file. Model chỉ sinh ra bản giữ chỗ "
               "(placeholder) nên proxy đã chặn, không ghi gì xuống đĩa. "
               "Lý do không chạy được bước gộp tự động: " + why +
               ". Hãy gửi lại kèm tên thư mục cần gộp, ví dụ: "
               "gộp thư mục gemini_web2api thành gemini_web2api.py.")
    else:
        msg = ("I did NOT merge the files. The model only produced a placeholder, "
               "so the proxy blocked it and nothing was written. Why the automatic "
               "merge could not run: " + why + ". Retry naming the folder to merge.")
    return msg, None


def install():
    _ff3_prev_install()
    from . import server
    cls = server.GeminiHandler
    cur = cls._generate_with_tool_retry
    if getattr(cur, "_flow3_patched", False):
        return

    @functools.wraps(cur)
    def wrapper3(self, prompt, model_id, think_mode, file_refs, extra,
                 tools_active, allowed_names, tool_schemas, required_tool,
                 messages=None, ticket=None, tool_required=None,
                 client_gone=None):
        result = cur(self, prompt, model_id, think_mode, file_refs, extra,
                     tools_active, allowed_names, tool_schemas, required_tool,
                     messages=messages, ticket=ticket,
                     tool_required=tool_required, client_gone=client_gone)
        try:
            return _ff3_postfilter(result, messages, allowed_names,
                                   tool_schemas, tool_required, tools_active)
        except Exception as e:
            log("[fix3] postfilter error: %s" % e)
            return result

    wrapper3._flow3_patched = True
    cls._generate_with_tool_retry = wrapper3
    log("[fix3] flow_fix3 installed")
# ==== flowfix3:end ====
