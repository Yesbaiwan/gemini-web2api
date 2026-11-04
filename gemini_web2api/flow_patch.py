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
_OFFSET_KEYS = ["offset", "start_line", "startLine", "line_offset", "start"]
_LIMIT_KEYS = ["limit", "count", "max_lines", "maxLines", "num_lines", "end_line"]


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

    # ------------------------------------------------------------------
    # Adapter (proposal 3): the client decides the schema; the proxy decides
    # the call. Model output is normalised to the client's real parameter
    # names, unknown keys are dropped and required parameters are checked
    # BEFORE the call reaches the client.
    # ------------------------------------------------------------------

    def adapt(self, call):
        """``(call, problems)`` -- normalise one tool_call to this client's schema."""
        if not isinstance(call, dict):
            return call, ["tool call is not an object"]
        fn = call.get("function") or {}
        name = fn.get("name") or ""
        args = T._tool_arguments_dict(fn)
        if not isinstance(args, dict):
            return call, [f"{name or '?'}: arguments are not a JSON object"]
        props = self.schemas.get(name)
        props = set(props) if props else set()
        required = set(self.required.get(name, ()) or ())
        out = {}
        problems = []

        def pick_value(keys):
            for k in keys:
                v = args.get(k)
                if isinstance(v, str) and v.strip():
                    return v
                if isinstance(v, int) and not isinstance(v, bool):
                    return v
            return None

        def put(keys, value, fallback):
            if value is None:
                return
            target = _pick(props, keys)
            if target is None and not props:
                # No schema: keep the key the model actually used rather than
                # renaming it to a canonical name the client may not accept.
                target = next((k for k in keys if k in args), fallback)
            if target:
                out[target] = value

        kind = tool_kind(name)
        path = pick_value(_PATH_KEYS)
        command = pick_value(_CMD_KEYS)
        pattern = pick_value(_PATTERN_KEYS)
        old = pick_value(_OLD_KEYS)
        new = pick_value(_NEW_KEYS)
        content = pick_value(_CONTENT_KEYS)
        offset = pick_value(_OFFSET_KEYS)
        limit = pick_value(_LIMIT_KEYS)
        description = pick_value(["description"])

        if kind == "read":
            put(_PATH_KEYS, path, "filePath")
            put(_OFFSET_KEYS, offset, "offset")
            put(_LIMIT_KEYS, limit, "limit")
            if not path:
                problems.append(f"{name}: missing required parameter for the file path")
        elif kind in ("survey", "search"):
            put(_PATTERN_KEYS, pattern, "pattern")
            put(_PATH_KEYS, path, "path")
            if not pattern and not path:
                problems.append(f"{name}: missing required parameter pattern/path")
        elif kind == "exec":
            put(_CMD_KEYS, command, "command")
            put(["description"], description, "description")
            if not command:
                problems.append(f"{name}: missing required parameter for the command")
        elif kind == "act":
            put(_PATH_KEYS, path, "filePath")
            edit_like = bool(set(_tokens(name)) & {
                "edit", "write", "patch", "replace", "insert", "append",
                "apply", "save", "editor"})
            # An edit uses old/new; a write uses content. Keep whichever the
            # client schema declares, and never mix both shapes silently. A
            # move/rename/delete/mkdir call is not an edit of file contents.
            if edit_like and old is not None:
                put(_OLD_KEYS, old, "oldString")
                put(_NEW_KEYS, new, "newString")
            elif edit_like:
                put(_CONTENT_KEYS, content, "content")
            if edit_like:
                if not path:
                    problems.append(f"{name}: missing required parameter for the file path")
                if old is None and content is None:
                    problems.append(
                        f"{name}: the edit has neither the text to replace "
                        "(oldString) nor the new file content (content)")

        # Preserve declared parameters the model already used correctly
        # (timeout, workdir, ...). Unknown keys are dropped when the schema is
        # known: the client would reject them anyway.
        for k, v in args.items():
            if props and k not in props:
                continue
            if k in out:
                continue
            if any(k in group for group in
                   (_PATH_KEYS, _CMD_KEYS, _PATTERN_KEYS, _OLD_KEYS,
                    _NEW_KEYS, _CONTENT_KEYS, _OFFSET_KEYS, _LIMIT_KEYS)):
                continue
            out[k] = v

        for req in sorted(required):
            if req in out:
                continue
            if str(req).lower() == "description":
                out[req] = description or "Run"
                continue
            problems.append(f"{name}: missing required parameter '{req}'")

        if not out and kind != "other":
            problems.append(f"{name}: no usable arguments after adapting")

        new_args = json.dumps(out, ensure_ascii=False)
        if new_args == (fn.get("arguments") or "") and not problems:
            return call, problems
        return {**call, "function": {**fn, "arguments": new_args}}, problems

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
                        "(no line-number prefixes)."
                        "CRITICAL: Do NOT break or alter existing features/routes. "
                        "When adding a new endpoint, create a NEW branch (e.g. new `elif`) "
                        "instead of merging it into existing ones. Preserve dynamic "
                        "variables (like __version__) for existing paths." + _failed_act_hint(ev),
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
        if getattr(step, "local_text", None) is not None:
            # A deterministic refusal/stop from a transition guard: return it
            # verbatim instead of asking the model again.
            return step.local_text, None
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
                      "(no line-number prefixes)."
                      "CRITICAL: Do NOT break or alter existing features/routes. "
                      "When adding a new endpoint, create a NEW branch (e.g. new `elif`) "
                      "instead of merging it into existing ones. Preserve dynamic "
                      "variables (like __version__) for existing paths." + _DO_NOT_DESCRIBE
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


# ==== flowfix4:begin (generated by flow_fix4_patch.py -- re-run to refresh, --revert to remove) ====
# Overrides (later definitions win: every caller resolves these names at call time).
_ff4_prev_analyze = _analyze
_ff4_prev_plan_for = plan_for
_ff4_prev_map_reduce = _map_reduce
_ff4_prev_install = install


# --------------------------------------------------------------------------
# 1. "doc lap" (độc lập) is not "doc" (đọc): stop mis-routing create/merge
#    requests to FILE_REVIEW
# --------------------------------------------------------------------------

_FF4_NOISE_RE = re.compile(r"\bdoc\s+(?:lap|quyen|dao|ac|hai)\b", re.IGNORECASE)


def _ff4_scrub(folded):
    return _FF4_NOISE_RE.sub(" ", folded or "")


def _ff4_wrap_classifiers():
    """Patch tools._is_file_review/_is_source_review (and server's copies)."""
    from . import server
    if getattr(T._is_file_review, "_ff4", False):
        return
    orig_file, orig_src = T._is_file_review, T._is_source_review

    def is_file_review(folded, scannable):
        folded = _ff4_scrub(folded)
        # A create/merge request with no explicit review word is not a review,
        # even if a weak verb ("kiem tra", "tim") and a file name are present.
        if _create_intent(folded, scannable) and not _REVIEW_RE.search(folded.lower()):
            return False
        return orig_file(folded, scannable)

    def is_source_review(folded):
        return orig_src(_ff4_scrub(folded))

    is_file_review._ff4 = True
    is_source_review._ff4 = True
    T._is_file_review = is_file_review
    T._is_source_review = is_source_review
    server._is_file_review = is_file_review
    server._is_source_review = is_source_review


def _analyze(messages):
    ctx = _ff4_prev_analyze(messages)
    try:
        if (ctx is not None and _c("fix_enabled") and _c("fix_create_flow")
                and getattr(ctx, "pattern", None) in ("FILE_REVIEW", "GENERIC_REVIEW")
                and not _clarified(messages, ctx.idx)):
            folded = _ff4_scrub(ctx.folded or "")
            if (not _REVIEW_RE.search(folded.lower())
                    and _create_intent(folded, ctx.scannable, ctx.last)):
                files = _named_files(ctx)
                ctx.pattern = "CREATE"
                ctx.fix = False
                ctx.target = files[0] if files else ""
                ctx.create_merge = bool(_MERGE_RE.search(folded.lower()))
                ctx.dir_cands = _dir_cands(ctx.last)
    except Exception as e:
        log(f"[fix4] analyze error: {e}")
    return ctx


# --------------------------------------------------------------------------
# 2. More canned upstream sentences
# --------------------------------------------------------------------------

_FF4_CANNED = (
    re.compile(r"\bi\s+seem\s+to\s+be\s+encountering\s+an\s+error\b", re.IGNORECASE),
    re.compile(r"\bhaving\s+a\s+hard\s+time\s+fulfilling\s+your\s+request\b",
               re.IGNORECASE),
    re.compile(r"\bi\s+encountered\s+an\s+error\s+doing\s+what\s+you\s+asked\b",
               re.IGNORECASE),
    re.compile(r"\bsorry,?\s+something\s+went\s+wrong\b", re.IGNORECASE),
)


def _ff4_extend_canned():
    cur = tuple(T._CANNED_FAILURE_RES)
    if not any(r in cur for r in _FF4_CANNED):
        T._CANNED_FAILURE_RES = cur + _FF4_CANNED


# --------------------------------------------------------------------------
# 3. Truncated reads: continue them, and never report them as fully read
# --------------------------------------------------------------------------

_FF4_TRUNC_RE = re.compile(
    r"\[[^\]\n]*truncated[^\]\n]*?offset:?\s*(\d+)[^\]\n]*\]", re.IGNORECASE)
_FF4_MAX_CONT = 8


def _ff4_off(args):
    key = _pick(args or {}, _OFFSET_KEYS)
    try:
        return int((args or {}).get(key)) if key else 0
    except (TypeError, ValueError):
        return 0


def _ff4_offset_key(tm):
    name = tm.first("read")
    props = (tm.schemas.get(name) if name else None) or set()
    return _pick(props, _OFFSET_KEYS)


def _ff4_truncations(ev):
    """[(event_index, path, next_offset)] for reads whose output was cut off."""
    out = []
    for i, e in enumerate(ev):
        if e["kind"] != "read" or not _read_ok(e):
            continue
        hits = _FF4_TRUNC_RE.findall((e["result"] or "")[-400:])
        path = _call_path(e["args"])
        if hits and path:
            out.append((i, path, int(hits[-1])))
    return out


def _ff4_unfinished(ev):
    """Truncations with no later read at or beyond the offset they asked for."""
    out = []
    for i, path, nxt in _ff4_truncations(ev):
        later = [_ff4_off(x["args"]) for x in ev[i + 1:]
                 if x["kind"] == "read" and _call_path(x["args"]) == path]
        if not any(o >= nxt for o in later):
            out.append((i, path, nxt))
    return out


def _ff4_continuations(ev, okey):
    """[(path, tool_call)] reading the rest of every truncated file."""
    if not okey:
        return []
    out, seen = [], set()
    for i, path, nxt in _ff4_unfinished(ev):
        if (path, nxt) in seen:
            continue
        seen.add((path, nxt))
        n_reads = sum(1 for x in ev
                      if x["kind"] == "read" and _call_path(x["args"]) == path)
        if n_reads > _FF4_MAX_CONT:
            continue
        args = dict(ev[i]["args"])
        args[okey] = nxt
        out.append((path, {
            "id": f"call_{uuid.uuid4().hex[:8]}", "type": "function",
            "function": {"name": ev[i]["name"],
                         "arguments": json.dumps(args, ensure_ascii=False)}}))
    return out[:6]


def plan_for(messages, tm, ctx=None):
    step = _ff4_prev_plan_for(messages, tm, ctx)
    try:
        if (step is not None and step.name == "answer"
                and step.pattern in ("GENERIC_REVIEW", "FILE_REVIEW", "EXPLAIN",
                                     "DEBUG", "DIFF_REVIEW")):
            pairs = _ff4_continuations(_answered_calls(messages),
                                       _ff4_offset_key(tm))
            if pairs:
                names = ", ".join(sorted({p for p, _c2 in pairs}))
                return Step(step.pattern, "read", step.index, step.total,
                            {"read"},
                            "A previous read was cut off (output truncated). "
                            "Read the rest of: " + names + ".",
                            [c for _p, c in pairs], True, tm.names({"read"}))
    except Exception as e:
        log(f"[fix4] continuation planner error: {e}")
    return step


def _review_materials(messages):
    """Like the original, but several reads of one file are concatenated."""
    ev = _answered_calls(messages)
    order, bodies = [], {}
    for e in ev:
        if e["kind"] == "read" and _read_ok(e):
            p = _call_path(e["args"])
            if not p:
                continue
            body = _FF4_TRUNC_RE.sub("", e["result"]).rstrip()
            if p in bodies:
                bodies[p] += "\n" + body
            else:
                bodies[p] = body
                order.append(p)
    tests = [e for e in ev if e["kind"] == "exec"
             and _is_test_cmd(_call_command(e["args"]))]
    return [(p, bodies[p]) for p in order], tests


def _map_reduce(messages, step, ctx, gen, client_gone=None):
    report = _ff4_prev_map_reduce(messages, step, ctx, gen, client_gone)
    if not report:
        return report
    try:
        part = sorted({p for _i, p, _n in _ff4_unfinished(_answered_calls(messages))})
        if part:
            if _VI_RE.search((ctx.last or "").lower()):
                note = ("**Coverage một phần:** các file sau bị cắt khi đọc "
                        "(output truncated) và chưa đọc hết, phần cuối chưa được "
                        "review: ")
            else:
                note = ("**Partial coverage:** these files were cut off when read "
                        "(output truncated) and not read to the end: ")
            report += "\n\n" + note + ", ".join(part)
    except Exception as e:
        log(f"[fix4] coverage note error: {e}")
    return report


# --------------------------------------------------------------------------
# 4. Final CHANGE answer failed upstream: summarise from the tool results
# --------------------------------------------------------------------------

def _ff4_local_answer(exc, messages, tools_active):
    if not (tools_active and messages and "placeholder" in str(exc).lower()):
        return None
    last = messages[-1]
    if not (isinstance(last, dict) and last.get("role") == "tool"):
        return None
    ctx = _analyze(messages)
    if ctx is None or ctx.pattern != "CHANGE":
        return None
    ev = _answered_calls(messages)
    acts = [e for e in ev if e["kind"] == "act"]
    if not acts:
        return None
    done = [e for e in acts if _act_ok(e)]
    bad = [e for e in acts if not _act_ok(e)]
    last_act = max(i for i, e in enumerate(ev) if e["kind"] == "act")
    runs = [r for r in _test_runs(ev) if r[0] > last_act]
    outcome = test_outcome(runs[-1][2]) if runs else None
    summ = " | ".join(_test_summary([{"result": runs[-1][2]}])) if runs else ""
    files = sorted({_call_path(e["args"]) or "?" for e in done})
    vi = bool(_VI_RE.search((ctx.last or "").lower()))
    lines = []
    if vi:
        lines.append(("Đã áp dụng %d thay đổi: %s." % (len(done), ", ".join(files)))
                     if done else "Chưa có thay đổi nào được áp dụng thành công.")
        if bad:
            lines.append("Có %d lần sửa bị lỗi (lần cuối: %s)." % (
                len(bad), " ".join((bad[-1]["result"] or "").split())[:200]))
        if outcome == "pass":
            lines.append("Test sau lần sửa cuối: PASS -- " + summ)
        elif outcome == "fail":
            lines.append("Test sau lần sửa cuối: FAIL.\n" + _excerpt(runs[-1][2], 800))
        elif outcome == "broken":
            lines.append("Lệnh test không chạy được sau lần sửa cuối.")
        else:
            lines.append("Chưa có kết quả test hợp lệ sau lần sửa cuối.")
        lines.append("(Upstream trả về câu báo lỗi thay vì lời tổng kết, nên proxy "
                     "tự tóm tắt từ kết quả công cụ. Model chưa đánh giá lại nội "
                     "dung thay đổi; hãy xem lại diff.)")
    else:
        lines.append(("Applied %d change(s): %s." % (len(done), ", ".join(files)))
                     if done else "No change was applied successfully.")
        if bad:
            lines.append("%d edit(s) failed (last: %s)." % (
                len(bad), " ".join((bad[-1]["result"] or "").split())[:200]))
        if outcome == "pass":
            lines.append("Tests after the last edit: PASS -- " + summ)
        elif outcome == "fail":
            lines.append("Tests after the last edit: FAIL.\n" + _excerpt(runs[-1][2], 800))
        elif outcome == "broken":
            lines.append("The test command could not run after the last edit.")
        else:
            lines.append("No valid test result after the last edit.")
        lines.append("(Upstream returned an error sentence instead of a summary, so "
                     "the proxy summarised the tool results itself. The model did "
                     "not re-review the change; please check the diff.)")
    return "\n\n".join(lines)


def install():
    _ff4_prev_install()
    _ff4_wrap_classifiers()
    _ff4_extend_canned()
    from . import server
    cls = server.GeminiHandler
    cur = cls._generate_with_tool_retry
    if getattr(cur, "_flow4_patched", False):
        return

    @functools.wraps(cur)
    def wrapper4(self, prompt, model_id, think_mode, file_refs, extra,
                 tools_active, allowed_names, tool_schemas, required_tool,
                 messages=None, ticket=None, tool_required=None,
                 client_gone=None):
        try:
            return cur(self, prompt, model_id, think_mode, file_refs, extra,
                       tools_active, allowed_names, tool_schemas, required_tool,
                       messages=messages, ticket=ticket,
                       tool_required=tool_required, client_gone=client_gone)
        except Exception as e:
            try:
                text = _ff4_local_answer(e, messages, tools_active)
            except Exception as inner:
                log(f"[fix4] local answer failed: {inner}")
                text = None
            if text is None:
                raise
            log("[fix4] answer step failed upstream; returning a local summary")
            return text, None

    wrapper4._flow4_patched = True
    cls._generate_with_tool_retry = wrapper4
    log("[fix4] flow_fix4 installed")
# ==== flowfix4:end ====

# ==== flowfix5:begin (generated by flow_fix5_patch.py -- re-run to refresh, --revert to remove) ====
import hashlib as _ff5_hashlib
import threading as _ff5_threading
import time as _ff5_time
import ast as _ff5_ast

_ff5_prev_analyze = _analyze
_ff5_prev_install = install
_ff5_prev_plan_for = plan_for
_ff5_prev_map_reduce = _map_reduce
_ff5_prev_banner = getattr(T, "_task_pattern_banner_legacy",
                           T.task_pattern_banner)

_FF5_DEFAULTS = {
    "flow5_enabled": True,
    # No pattern matched -> ask the model to pick one from the catalog below.
    "flow5_classify": True,
    # Windows PowerShell 5.1 has no `&&`: rewrite it.
    "flow5_ps_fix": True,
    # Refuse wide destructive commands the user did not write themselves.
    "flow5_block_destructive": True,
    # "do not edit my files" is a hard constraint, not a hint.
    "flow5_respect_no_edit": True,
    # A write/edit that still fails the guards after the retries is dropped.
    "flow5_fail_closed_edits": True,
    # Targeted re-asks (with the specific error) before a guarded call is blocked.
    "flow5_guard_retries": 1,
    # Follow-ups (log / path / "continue") keep the earlier goal and its limits.
    "flow5_sticky": True,
    # PLAN pattern ("write a plan ...") is read-only.
    "flow5_plan": True,
    # Completion of the handoff (fix6): one context block per guided step,
    # scope-aware writes, a universal evidence gate and a real diff stage.
    "flow6_context": True,
    "flow6_metrics": True,
    "flow6_diff_step": True,
    "flow6_scope_guard": True,
    "flow6_universal_guard": True,
    "flow6_cross_module": True,
    "flow6_banner": True,
}


def _c5(key):
    return CONFIG.get(key, _FF5_DEFAULTS[key])


# One table drives the classifier catalog, the prompt banner, the step engine
# and the permission checks, so they cannot disagree (proposals 2 + 6).
# `edit` = may write files; `run` = "any" shell command, "readonly" (listing,
# git status/diff/log, test runners) or "none"; `steps` = canonical shared
# pipeline from which the planner, banner and validator are generated.
_FF5_SPEC = {
    "GENERIC_REVIEW": {"desc": "review / audit the whole project or its source code for bugs",
                       "steps": ("discover", "analyze", "verify", "read", "answer"),
                       "edit": False, "run": "readonly"},
    "FILE_REVIEW": {"desc": "",  # not offered to the classifier: the repo's own detector owns it
                    "steps": ("read", "analyze", "answer"),
                    "edit": False, "run": "readonly"},
    "CHANGE": {"desc": "modify, fix or implement something in EXISTING code",
               "steps": ("discover", "read", "edit", "diff", "verify", "answer"),
               "edit": True, "run": "any"},
    "CREATE": {"desc": "create a new file, script or module (or merge several files into one)",
               "steps": ("discover", "read", "edit", "verify", "answer"),
               "edit": True, "run": "any"},
    "EXPLAIN": {"desc": "explain how code works, where something lives, or how to do something (guidance only, no edits)",
                "steps": ("discover", "read", "answer"),
                "edit": False, "run": "readonly"},
    "DEBUG": {"desc": "investigate a failing test, an error message or a crash",
              "steps": ("discover", "read", "reproduce", "analyze", "answer"),
              "edit": False, "run": "readonly"},
    "DIFF_REVIEW": {"desc": "review the current uncommitted git changes",
                    "steps": ("diff", "read", "analyze", "answer"),
                    "edit": False, "run": "readonly"},
    "PLAN": {"desc": "write an implementation plan for a feature or change (no edits)",
             "steps": ("discover", "read", "analyze", "answer"),
             "edit": False, "run": "readonly"},
    "REVERT": {"desc": "undo / revert the changes made earlier in this conversation",
               "steps": ("diff", "edit", "verify", "answer"),
               "edit": True, "run": "any"},
    "RUN_TESTS": {"desc": "run the unit tests and report the result",
                  "steps": ("verify", "answer"),
                  "edit": False, "run": "readonly"},
    "RUN_COMMAND": {"desc": "run one specific shell command that the user wrote",
                    "steps": ("verify", "answer"),
                    "edit": False, "run": "any"},
    "GIT_INFO": {"desc": "show git status / log / diff / branch information",
                 "steps": ("diff", "answer"),
                 "edit": False, "run": "readonly"},
    "TOOL_DEMAND": {"desc": "", "steps": ("edit", "answer"),
                    "edit": True, "run": "any"},
}
_FF5_CATALOG = {k: v["desc"] for k, v in _FF5_SPEC.items() if v.get("desc")}
# Patterns planned entirely by this block (deterministic, no upstream call).
_FF5_OWN = ("REVERT", "RUN_TESTS", "RUN_COMMAND", "GIT_INFO")

# `DEBUG` only gets the edit stage when the user actually asked for a fix.
# The helper is used by the planner, banner and validator alike.
def _ff5_allowed_steps(pattern, fix=False):
    spec = _FF5_SPEC.get(pattern or "", {})
    steps = list(spec.get("steps", ()))
    if pattern == "DEBUG" and fix and "edit" not in steps:
        steps = steps[:-1] + ["edit", "diff", "verify", "answer"]
    if pattern != "DEBUG" or fix:
        return steps
    return [s for s in steps if s not in ("edit", "diff", "verify")]


def _ff5_spec_edit(pattern, fix=False):
    spec = _FF5_SPEC.get(pattern or "", {})
    return bool(spec.get("edit", True) or (pattern == "DEBUG" and fix))


def _ff5_spec_run(pattern):
    return _FF5_SPEC.get(pattern or "", {}).get("run", "any")

# --------------------------------------------------------------------------
# Proposal 1: intent is separate from permission. The verb "fix" never grants
# write access; the user's limits persist across turns until revoked.
# --------------------------------------------------------------------------

_FF5_GRANT_RE = re.compile(
    r"\b(?:go\s+ahead|apply\s+(?:it|the\s+(?:fix|changes?))|you\s+(?:may|can)\s+(?:edit|modify|change)"
    r"|feel\s+free\s+to\s+(?:edit|change|modify)|sua\s+luon|cu\s+sua|ap\s+dung(?:\s+luon)?|cho\s+phep\s+sua)\b")
_FF5_HOWTO_RE = re.compile(
    r"\bhow\s+(?:can|could|do|should|would|to)\b|\bshould\s+(?:i|we)\b"
    r"|\bwhat(?:'s|\s+is)\s+the\s+(?:best\s+)?way\s+to\b"
    r"|\b(?:lam\s+sao|lam\s+the\s+nao|cach\s+nao|huong\s+dan|chi\s+(?:cach|minh\s+cach))\b"
    r"|\bnen\s+(?:sua|lam)\s+(?:nhu\s+the\s+nao|ra\s+sao)\b")
_FF5_NORUN_RE = re.compile(
    r"\b(?:do\s+not|don'?t|dont|never)\s+(?:run|execute)\s+(?:any\s+)?(?:commands?|shell|scripts?|anything)\b"
    r"|\b(?:khong|dung)\s+(?:duoc\s+)?chay\s+(?:bat\s+ky\s+)?(?:lenh|command|script|shell)\b")


def _ff5_fold(text):
    scannable = T._strip_code_spans(text or "")
    return re.sub(r"\s+", " ", T._fold_diacritics(scannable)).lower()


class _Ff5Perm:
    def __init__(self, edit, run, reason, restricted, scope=None):
        self.edit, self.run = edit, run
        self.reason, self.restricted = reason, restricted
        # None = no restriction; otherwise the paths this request may write.
        self.scope = scope

    def __repr__(self):
        return "Perm(edit=%s, run=%s, %s)" % (self.edit, self.run, self.reason)


def _ff5_permissions(messages, pattern=None, fix=False):
    """Permissions in force for the newest request.

    Sticky: a "do not edit / do not run" from ANY earlier user turn stays in
    force until a later turn grants it back. Guidance questions ("how can I
    fix ...?") withhold write access for that turn only. ``run`` is three
    levels: any / readonly / none -- "do not run commands" now really means
    no command at all, not merely read-only commands.
    """
    texts = [T._stringify_content(m.get("content", "")) for m in messages or []
             if isinstance(m, dict) and m.get("role") == "user"]
    edit_ok = run_ok = True
    why = []
    for t in texts:
        f = _ff5_fold(t)
        if _FF5_GRANT_RE.search(f):
            edit_ok = run_ok = True
        if _FF5_NOEDIT_RE.search(f):
            edit_ok = False
        if _FF5_NORUN_RE.search(f):
            run_ok = False
    if not edit_ok:
        why.append("user forbade edits")
    if not run_ok:
        why.append("user forbade running commands")
    guidance = False
    if texts:
        f = _ff5_fold(texts[-1])
        guidance = bool(_FF5_HOWTO_RE.search(f)) and not _FF5_GRANT_RE.search(f)
        if guidance:
            why.append("guidance question")
    spec_edit = _ff5_spec_edit(pattern, fix)
    spec_run = _ff5_spec_run(pattern)
    can_edit = bool(spec_edit and edit_ok and not guidance)
    if not run_ok:
        run = "none"
    elif spec_run == "any" and not (guidance and pattern == "EXPLAIN"):
        run = "any"
    else:
        run = "readonly"
    restricted = (can_edit != spec_edit) or (run != spec_run)
    return _Ff5Perm(can_edit, run, "; ".join(why) or "default", restricted,
                    scope=None)


def _ff5_scope(ctx, messages):
    """Files this request may write, or None when there is no named/read scope.

    Policy: a request that names files is limited to them. A request that names
    none (a plain "fix the bug") is limited to files actually read this turn.
    CREATE additionally allows its target file, and the proxy's own temporary
    merge helper is always allowed. REVERT/TOOL_DEMAND are unrestricted because
    their writes are derived from the conversation itself.
    """
    pattern = getattr(ctx, "pattern", None)
    if pattern in (None, "REVERT", "TOOL_DEMAND"):
        return None
    named = [f for f in (_named_files(ctx) if ctx is not None else []) if f]
    if pattern == "CREATE":
        allowed = list(named)
        target = getattr(ctx, "target", "") or ""
        if target:
            allowed.append(target)
        return allowed or None
    if pattern in ("CHANGE", "DEBUG"):
        if named:
            return named
        reads = [_call_path(e["args"]) for e in _answered_calls(messages)
                 if e["kind"] == "read" and _read_ok(e) and _call_path(e["args"])]
        return reads or None
    return []          # this pattern must not write at all


def _ff5_in_scope(path, scope):
    if not scope:
        return False
    if _base(path) == _MERGE_SCRIPT:
        return True
    return any(T._path_seen(path, [x]) for x in scope)


def _ff5_scope_problems(calls, messages, perm):
    scope = getattr(perm, "scope", None) if perm is not None else None
    if not scope:
        return []
    problems = []
    shown = ", ".join(scope[:6])
    for tc in calls or []:
        fn = (tc or {}).get("function") or {}
        if tool_kind(fn.get("name")) != "act":
            continue
        path = _call_path(T._tool_arguments_dict(fn))
        if path and not _ff5_in_scope(path, scope):
            problems.append(
                f"{path}: outside this request's write scope ({shown}). "
                "Only edit the files the user named or the files read in this turn.")
    return problems


def _ff5_edit_forbidden(messages):
    return not _ff5_permissions(messages).edit


_FF5_RO_SEG_RE = re.compile(
    r"^\s*(?:git\s+(?:--no-pager\s+)?(?:status|diff|log|ls-files|show|branch|rev-parse|remote)\b|"
    r"ls\b|dir\b|cat\b|type\b|echo\b|find\b|grep\b|rg\b|head\b|tail\b|pwd\b|cd\b|wc\b|sort\b|"
    r"get-childitem\b|get-content\b|select-string\b|where\b|which\b|"
    r"python3?\s+-m\s+(?:py_compile|unittest|pytest)\b|pytest\b|npm\s+(?:run\s+)?test\b|"
    r"go\s+test\b|cargo\s+test\b)", re.I)


_FF5_TEST_SEG_RE = re.compile(
    r"^\s*(?:python3?\s+-m\s+(?:unittest|pytest)\b|pytest\b|npm\s+(?:run\s+)?test\b|node\s+\S*test\S*|"
    r"go\s+test\b|cargo\s+test\b|dotnet\s+test\b|mvn\s+test\b|gradle\s+test\b)", re.I)
_FF5_READFAIL_RE = re.compile(
    r"(?i)^\s*(?:error|cannot\s+(?:read|open|find)|no\s+such\s+file|file\s+not\s+found|"
    r"enoent|the\s+system\s+cannot|failed\s+to\s+read|does\s+not\s+exist|permission\s+denied)")
_FF5_TRUNC_RE = re.compile(r"(?i)(?:truncated|output\s+(?:was\s+)?cut|more\s+lines|\.\.\.\s*\[?\d+\s+(?:more|lines))")


def _ff5_cmd_readonly(cmd):
    c = re.sub(r"\d?>&\d|\d?>\s*/dev/null|\d?>\s*nul\b", "", cmd or "", flags=re.I)
    if ">" in c:
        return False
    segs = [s for s in re.split(r"&&|\|\||;|\|", c) if s.strip()]
    if not segs:
        return False
    return all(_FF5_RO_SEG_RE.match(s) or _FF5_TEST_SEG_RE.match(s) for s in segs)


def _ff5_status(e):
    """success | partial | failed | unknown -- only success/partial unlock the next step.

    ``unknown`` is evidence that exists but proves nothing (an empty read).
    Such a read is retried once by :func:`_ff5_evidence_gate`, then the proxy
    stops instead of letting the model guess.
    """
    res = (e.get("result") or "")
    k = e.get("kind")
    if k == "read":
        if not res.strip():
            return "unknown"
        if not _read_ok(e) or _FF5_READFAIL_RE.match(res):
            return "failed"
        trunc = globals().get("_FF4_TRUNC_RE") or _FF5_TRUNC_RE
        return "partial" if trunc.search(res[-400:]) else "success"
    if k == "act":
        if not res.strip():
            return "unknown"
        return "success" if _act_ok(e) else "failed"
    if re.search(r"Exited with code [1-9]", res):
        return "failed"
    return "success" if res.strip() else "unknown"


def _ff5_status_detail(e):
    """A short, specific reason for a failed/unknown call (used in nudges)."""
    res = " ".join((e.get("result") or "").split())
    path = _call_path(e.get("args") or {}) or "?"
    low = res.lower()
    if e.get("kind") == "read":
        if not res.strip():
            return f"{path}: the read tool returned no content"
        return f"{path}: read failed -- {res[:180]}"
    if e.get("kind") == "act":
        if not res.strip():
            return f"{path}: the edit tool returned no result, so the write is unverified"
        if "oldstring" in low or "old_string" in low or "old text" in low:
            return (f"{path}: oldString does not match the file you read -- copy the "
                    f"exact text, without line-number prefixes ({res[:160]})")
        if re.search(r"not found|no such file|does not exist", low):
            return f"{path}: wrong path or missing file -- {res[:160]}"
        if re.search(r"missing|required|parameter|argument", low):
            return f"{path}: a required parameter is missing -- {res[:160]}"
        return f"{path}: edit failed -- {res[:180]}"
    return f"{path}: command failed -- {res[:180]}"


# --------------------------------------------------------------------------
# Proposal 3: ToolMap adapter -- proxy-side parameter normalisation
# --------------------------------------------------------------------------

def _ff5_adapt_calls(calls, tm):
    """Normalise every call to the client's schema; return (calls, problems)."""
    out, problems = [], []
    for tc in calls or []:
        try:
            fixed, probs = tm.adapt(tc)
        except Exception as e:
            fixed, probs = tc, [f"could not adapt the tool call: {e}"]
        out.append(fixed)
        problems.extend(probs)
    return out, problems


# --------------------------------------------------------------------------
# Proposal 5: universal evidence gate. A step only advances on success or a
# completed continuation; failed/unknown reads never unlock an edit, and a
# truncated read is continued by the proxy before anything else happens.
# --------------------------------------------------------------------------

def _ff5_read_events(messages):
    return [e for e in _answered_calls(messages) if e["kind"] == "read"]


def _ff5_same_path(a, b):
    return bool(a and b and T._path_seen(a, [b]))


def _ff5_retry_call(e):
    fn = e.get("name") or "read"
    args = dict(e.get("args") or {})
    return {"id": "call_" + uuid.uuid4().hex[:8], "type": "function",
            "function": {"name": fn,
                         "arguments": json.dumps(args, ensure_ascii=False)}}


def _ff5_local_step(ctx, text):
    """A planner step that must be returned verbatim, without another model call."""
    st = Step(getattr(ctx, "pattern", None) or "ANSWER", "answer", 1, 1, (), text)
    st.local_text = text
    st.skipped = []
    return st


def _ff5_evidence_gate(ctx, messages, tm):
    """``(step, stop_text)``. A read/continuation step wins; ``stop_text`` is a
    deterministic refusal that must not fall through to an edit or an answer."""
    if ctx is None or not getattr(ctx, "pattern", None):
        return None, None
    ev = _answered_calls(messages)
    reads = [e for e in ev if e["kind"] == "read"]
    if not reads:
        return None, None
    pattern = ctx.pattern
    perm = getattr(ctx, "perm", None)
    edit_expected = bool(getattr(perm, "edit", False)
                         and _ff5_spec_edit(pattern, getattr(ctx, "fix", False)))

    # 1. truncated output: continue at the offset the tool asked for, before
    #    editing or answering. For a pattern that may edit this is mandatory.
    okey = _ff4_offset_key(tm) if "_ff4_offset_key" in globals() else None
    conts = _ff4_continuations(ev, okey) if okey else []
    if conts:
        names = ", ".join(sorted({p for p, _c in conts}))
        return Step(pattern, "read", 1, 1, {"read"},
                    "A previous read was cut off (output truncated). Read the "
                    "rest before doing anything else: " + names + ".",
                    [c for _p, c in conts], True, tm.names({"read"})), None

    # 2. "partial" without an offset parameter: the proxy cannot continue it.
    #    Never let that partial text justify a write.
    partial = [e for e in reads if _ff5_status(e) == "partial"]
    if partial and edit_expected:
        detail = "; ".join(_ff5_status_detail(e) for e in partial[:3])
        if _ff5_vi(ctx):
            return None, ("Một lần đọc đã bị cắt và công cụ đọc không hỗ trợ "
                          "offset nên mình KHÔNG sửa gì (" + detail + "). Hãy "
                          "đọc lại file với giới hạn lớn hơn rồi yêu cầu lại.")
        return None, ("A read was truncated and the read tool exposes no offset "
                      "parameter, so I made NO edit (" + detail + "). Re-read the "
                      "file with a larger limit and ask again.")

    # 3. unknown (empty) read: retry once, then stop rather than guess.
    pending_unknown, exhausted = [], []
    for i, e in enumerate(reads):
        if _ff5_status(e) != "unknown":
            continue
        path = _call_path(e["args"])
        same = [x for x in reads if _ff5_same_path(path, _call_path(x["args"]))]
        if any(_ff5_status(x) in ("success", "partial") for x in same):
            continue
        # A path has one retry. Two (or more) empty reads mean the retry also
        # returned nothing: stop instead of looping forever.
        if sum(1 for x in same if _ff5_status(x) == "unknown") >= 2:
            if not any(_ff5_same_path(path, _call_path(x["args"]))
                       for x in exhausted):
                exhausted.append(e)
        elif not any(_ff5_same_path(path, _call_path(x["args"]))
                     for x in pending_unknown):
            pending_unknown.append(e)
    if pending_unknown:
        detail = "; ".join(_ff5_status_detail(e) for e in pending_unknown[:3])
        return Step(pattern, "read", 1, 1, {"read"},
                    "The read tool returned no content. Read the file again "
                    "before continuing (" + detail + ").",
                    [_ff5_retry_call(e) for e in pending_unknown], True,
                    tm.names({"read"})), None
    if exhausted and not any(_ff5_status(e) in ("success", "partial") for e in reads):
        detail = "; ".join(_ff5_status_detail(e) for e in exhausted[:3])
        if _ff5_vi(ctx):
            return None, ("Công cụ đọc trả về nội dung rỗng nhiều lần nên mình dừng "
                          "thay vì đoán (" + detail + "). Hãy kiểm tra đường dẫn.")
        return None, ("The read tool returned no content repeatedly, so I stopped "
                      "instead of guessing (" + detail + "). Check the path.")

    # 4. failed reads never unlock an edit; if nothing was read successfully,
    #    they do not unlock an answer either.
    failed = [e for e in reads if _ff5_status(e) == "failed"]
    good = [e for e in reads if _ff5_status(e) in ("success", "partial")]
    if edit_expected and failed:
        guard = _ff5_read_guard(ctx, messages)
        if guard:
            return None, guard
    if failed and not good and pattern not in ("CREATE", "TOOL_DEMAND"):
        detail = "; ".join(_ff5_status_detail(e) for e in failed[:3])
        if _ff5_vi(ctx):
            return None, ("Không đọc được file cần thiết nên mình không thể trả lời "
                          "dựa trên nội dung chưa thấy (" + detail + "). Hãy kiểm "
                          "tra đường dẫn rồi yêu cầu lại.")
        return None, ("I could not read the required file, so I will not answer "
                      "from unseen content (" + detail + "). Check the path and "
                      "ask again.")
    return None, None


# --------------------------------------------------------------------------
# Proposal 2: shared stages. The rich discover/read builders above stay the
# primitives; this table names the stage each step belongs to and inserts the
# missing "read the diff" stage before verify/answer.
# --------------------------------------------------------------------------

_FF5_STAGE_NAME = {
    "survey": "discover", "search": "discover", "read": "read",
    "act": "edit", "merge": "edit", "diff": "diff", "git": "diff",
    "tests": "verify", "verify": "verify", "answer": "answer",
}


def _ff5_stage(step):
    return _FF5_STAGE_NAME.get(getattr(step, "name", ""), "analyze")


def _ff5_diff_step(ctx, step, messages, tm):
    if not _c5("flow6_diff_step") or step is None:
        return None
    pattern = getattr(ctx, "pattern", None)
    if pattern not in ("CHANGE", "DEBUG") or (pattern == "DEBUG" and not ctx.fix):
        return None
    if step.name not in ("answer", "tests", "verify"):
        return None
    if not tm.first("exec"):
        return None
    ev = _answered_calls(messages)
    acts = [e for e in ev if e["kind"] == "act" and _act_ok(e)
            and _call_path(e["args"])]
    if not acts:
        return None
    last = max(i for i, e in enumerate(ev) if e["kind"] == "act")
    if any(e["kind"] == "exec"
           and re.search(r"\bgit\b[^\n]*\bdiff\b", _call_command(e["args"]))
           for e in ev[last + 1:]):
        return None
    paths = sorted({_call_path(e["args"]) for e in acts
                    if _base(_call_path(e["args"])) != _MERGE_SCRIPT})
    if not paths:
        return None
    quoted = " ".join('"%s"' % p.replace('"', "") for p in paths[:12])
    call = tm.make_call("exec", command="git --no-pager diff -- " + quoted,
                        description="Read the diff of the change")
    if not call:
        return None
    return Step(pattern, "diff", getattr(step, "index", 1),
                getattr(step, "total", 1), {"exec"},
                "Read the diff now. Check that only the intended lines changed; "
                "if the change is wrong, fix it with one edit call, otherwise "
                "continue to verification.", [call], True, tm.names({"exec"}))


def _ff5_context_suffix(ctx, step, perm, messages):
    """The smallest context a weak model needs for this one step (proposal 4)."""
    spec = _FF5_SPEC.get(getattr(ctx, "pattern", None) or "", {})
    goal = " ".join((getattr(ctx, "last", "") or "").split())[:180]
    ev = _answered_calls(messages)
    good = sorted({_call_path(e["args"]) for e in ev
                   if e["kind"] == "read" and _ff5_status(e) in ("success", "partial")
                   and _call_path(e["args"])})
    bad = sorted({_call_path(e["args"]) for e in ev
                  if e["kind"] == "read" and _ff5_status(e) == "failed"
                  and _call_path(e["args"])})
    bits = ["goal=" + (goal or "(none)"),
            "intent=" + (getattr(ctx, "pattern", None) or "?"),
            "edit=" + ("allowed" if getattr(perm, "edit", False) else "forbidden"),
            "commands=" + str(getattr(perm, "run", "readonly")),
            "next=" + getattr(step, "stage", _ff5_stage(step))]
    if good:
        bits.append("read=" + ", ".join(good[:6]))
    if bad:
        bits.append("failed_reads=" + ", ".join(bad[:4]))
    if _FF5_SPEC.get(ctx.pattern or "", {}).get("steps"):
        bits.append("pipeline=" + " -> ".join(_ff5_allowed_steps(
            ctx.pattern, getattr(ctx, "fix", False))))
    text = "[Context] " + " | ".join(bits)
    return text[:900]


def _ff5_after_plan(step, messages, tm, ctx=None):
    """Final transition guard shared by every pattern (proposal 5 + 2 + 6)."""
    if ctx is None:
        try:
            ctx = _analyze(messages)
        except Exception:
            ctx = None
    if not (ctx is not None and getattr(ctx, "pattern", None)):
        return step
    perm = getattr(ctx, "perm", None)
    if perm is None:
        try:
            perm = _ff5_permissions(messages, ctx.pattern, getattr(ctx, "fix", False))
            perm.scope = _ff5_scope(ctx, messages)
        except Exception:
            perm = None
    if _c5("flow6_universal_guard"):
        gate, stop = _ff5_evidence_gate(ctx, messages, tm)
        if stop is not None:
            log("[fix6] evidence gate stopped the transition: " + stop[:160])
            return _ff5_local_step(ctx, stop)
        if gate is not None:
            step = gate
    if step is not None and _c5("flow6_diff_step"):
        diff = _ff5_diff_step(ctx, step, messages, tm)
        if diff is not None:
            step = diff
    if step is not None:
        step.stage = _ff5_stage(step)
        # Validator generated from the same SPEC as the planner/banner.
        if step.name in ("act", "merge") and perm is not None and not perm.edit:
            msg = ("Bạn không cho phép sửa file nên mình không thực hiện chỉnh sửa nào."
                   if _ff5_vi(ctx) else
                   "Edits are not allowed here, so I made no edit.")
            return _ff5_local_step(ctx, msg)
        if step.name in ("tests", "verify", "diff", "git") and perm is not None \
                and perm.run == "none":
            msg = ("Bạn không cho phép chạy lệnh nên mình không chạy bước này."
                   if _ff5_vi(ctx) else
                   "Running commands is not allowed here, so this step was skipped.")
            return _ff5_local_step(ctx, msg)
        if _c5("flow6_context") and not getattr(step, "drive", False):
            suffix = _ff5_context_suffix(ctx, step, perm, messages)
            if suffix and suffix not in (step.instruction or ""):
                step.instruction = (step.instruction or "") + "\n" + suffix
    return step


def _ff5_plan_after(step, messages, tm, ctx=None):
    try:
        return _ff5_after_plan(step, messages, tm, ctx)
    except Exception as e:
        log(f"[fix6] transition guard error: {e}")
        return step


def plan_for(messages, tm, ctx=None):
    """Final planner: every pattern passes the universal transition guard."""
    step = _ff5_prev_plan_for(messages, tm, ctx)
    return _ff5_plan_after(step, messages, tm, ctx)


# --------------------------------------------------------------------------
# Proposal 4 / metrics: tiny counters so the real-model measurement (flash-lite)
# has a place to land without changing behaviour when disabled.
# --------------------------------------------------------------------------

_FF5_METRICS = {}


def _ff5_metric(key, n=1):
    if not _c5("flow6_metrics"):
        return
    _FF5_METRICS[key] = _FF5_METRICS.get(key, 0) + n


# --------------------------------------------------------------------------
# Cross-module pass (supplement): check the files read this turn against each
# other. This validates structure only (names, attributes, arity); the quote
# verifier likewise confirms the *position* of a quote, never the conclusion
# drawn from it.
# --------------------------------------------------------------------------

_FF5_CROSS_NOTE = (
    "Static cross-module checks confirm names/arity only; they do not confirm "
    "that a finding or conclusion is correct.")


def _ff5_trunc_re():
    return globals().get("_FF4_TRUNC_RE") or _FF5_TRUNC_RE


def _ff5_py_defs(tree):
    """Top-level definitions of one module: name -> facts used by the pass."""
    out = {}
    for node in getattr(tree, "body", ()):
        if isinstance(node, (_ff5_ast.FunctionDef, _ff5_ast.AsyncFunctionDef)):
            args = node.args
            total = len(getattr(args, "posonlyargs", ())) + len(args.args)
            required = max(0, total - len(args.defaults))
            out[node.name] = {
                "kind": "function", "required": required, "total": total,
                "vararg": args.vararg is not None,
                "line": getattr(node, "lineno", 0),
            }
        elif isinstance(node, _ff5_ast.ClassDef):
            out[node.name] = {"kind": "class", "required": 0, "total": 0,
                              "vararg": True, "line": getattr(node, "lineno", 0)}
    return out


def _ff5_py_modules(messages):
    """{stem: [module]} for every Python file read successfully this turn."""
    mods = {}
    for e in _answered_calls(messages):
        if e["kind"] != "read" or not _read_ok(e):
            continue
        path = _call_path(e["args"])
        if not path or not path.lower().endswith(".py"):
            continue
        text = _ff5_trunc_re().sub("", e["result"] or "").rstrip()
        try:
            tree = _ff5_ast.parse(_clean_file_text(text))
        except (SyntaxError, ValueError):
            continue
        stem = _base(path)[:-3]
        mods.setdefault(stem, []).append({
            "path": path, "tree": tree, "defs": _ff5_py_defs(tree)})
    return mods


def _ff5_py_target(mods, dotted):
    stem = (dotted or "").split(".")[-1].lower()
    cands = mods.get(stem) or []
    if not cands:
        return None
    want = (dotted or "").replace(".", "/").lower()
    for m in cands:
        p = m["path"].replace("\\", "/").lower()
        if p.endswith("/" + want + ".py") or p == want + ".py":
            return m
    return cands[0]


def _ff5_arg_problem(node, info):
    if info.get("kind") != "function":
        return ""
    given = len(node.args)
    if any(isinstance(a, _ff5_ast.Starred) for a in node.args):
        return ""
    required, total = info.get("required", 0), info.get("total", 0)
    if given < required:
        return ("expects at least %d positional argument(s) but got %d"
                % (required, given))
    if given > total and not info.get("vararg"):
        return ("expects at most %d positional argument(s) but got %d"
                % (total, given))
    return ""


def _ff5_cross_module(messages):
    """Static findings between the Python files read in this turn."""
    if not _c5("flow6_cross_module"):
        return []
    mods = _ff5_py_modules(messages)
    if len(mods) < 2 and not any(len(v) > 1 for v in mods.values()):
        return []
    out, seen = [], set()

    def add(text):
        if text not in seen:
            seen.add(text)
            out.append(text)

    for items in mods.values():
        for m in items:
            path = m["path"]
            defs = m["defs"]
            imported, imported_mods = {}, {}
            for node in _ff5_ast.walk(m["tree"]):
                if isinstance(node, _ff5_ast.ImportFrom):
                    if not node.module:
                        continue
                    target = _ff5_py_target(mods, node.module)
                    if target is None:
                        continue
                    for alias in node.names:
                        if alias.name == "*":
                            continue
                        if alias.name not in target["defs"]:
                            add("%s:%s: imports %r from %r, but %s does not "
                                "define it" % (path, getattr(node, "lineno", "?"),
                                               alias.name, node.module,
                                               target["path"]))
                            continue
                        imported[alias.asname or alias.name] = (
                            target, alias.name)
                elif isinstance(node, _ff5_ast.Import):
                    for alias in node.names:
                        base = alias.name.split(".")[0]
                        target = _ff5_py_target(mods, alias.name) \
                            or _ff5_py_target(mods, base)
                        if target is not None:
                            imported_mods[alias.asname or base] = target

            for node in _ff5_ast.walk(m["tree"]):
                if not isinstance(node, _ff5_ast.Call):
                    continue
                fn = node.func
                if isinstance(fn, _ff5_ast.Name) and fn.id in imported:
                    target, name = imported[fn.id]
                    info = target["defs"].get(name)
                    if info:
                        problem = _ff5_arg_problem(node, info)
                        if problem:
                            add("%s:%s: %s() %s (defined in %s)" % (
                                path, getattr(node, "lineno", "?"), name,
                                problem, target["path"]))
                elif (isinstance(fn, _ff5_ast.Attribute)
                      and isinstance(fn.value, _ff5_ast.Name)
                      and fn.value.id in imported_mods):
                    target = imported_mods[fn.value.id]
                    info = target["defs"].get(fn.attr)
                    if info is None:
                        add("%s:%s: %s.%s is not defined in %s" % (
                            path, getattr(node, "lineno", "?"), fn.value.id,
                            fn.attr, target["path"]))
                    else:
                        problem = _ff5_arg_problem(node, info)
                        if problem:
                            add("%s:%s: %s.%s() %s (defined in %s)" % (
                                path, getattr(node, "lineno", "?"),
                                fn.value.id, fn.attr, problem, target["path"]))
    return out


# --------------------------------------------------------------------------
# Own-pattern results (REVERT / RUN_TESTS / RUN_COMMAND / GIT_INFO): adapt the
# proxy-generated call to the client schema and enforce the same permissions.
# --------------------------------------------------------------------------

def _map_reduce(messages, step, ctx, gen, client_gone=None):
    """Final map-reduce: append the cross-module pass to every review report."""
    report = _ff5_prev_map_reduce(messages, step, ctx, gen, client_gone)
    if not report or not _c5("flow6_cross_module"):
        return report
    try:
        findings = _ff5_cross_module(messages)
        if findings:
            report += ("\n\n**Cross-module checks (static):**\n"
                       + "\n".join("- " + f for f in findings[:20]))
        report += "\n\n_" + _FF5_CROSS_NOTE + "_"
    except Exception as e:
        log(f"[fix6] cross-module pass error: {e}")
    return report


def _ff5_own_result(step, ctx, messages, tm, perm):
    if step is None:
        return None
    if step.text is not None:
        return step.text, None
    calls = list(step.calls or [])
    if not calls:
        return None
    calls, probs = _ff5_adapt_calls(calls, tm)
    if probs:
        text = ("Không tạo được lời gọi công cụ hợp lệ: " + "; ".join(probs[:3])
                if _ff5_vi(ctx) else
                "Could not build a valid tool call: " + "; ".join(probs[:3]))
        return text, None
    if perm is not None:
        kinds = [tool_kind((tc.get("function") or {}).get("name")) for tc in calls]
        if not perm.edit and "act" in kinds:
            return _ff5_say(
                ctx, "Bạn không cho phép sửa file nên mình không thực hiện chỉnh sửa nào.",
                "Edits are not allowed here, so I made no edit.").text, None
        if perm.run == "none" and "exec" in kinds:
            return _ff5_say(
                ctx, "Bạn không cho phép chạy lệnh nên mình không chạy bước này.",
                "Running commands is not allowed here, so this step was skipped."
            ).text, None
        if perm.run == "readonly" and "exec" in kinds:
            for tc in calls:
                fn = tc.get("function") or {}
                if tool_kind(fn.get("name")) != "exec":
                    continue
                cmd = _call_command(T._tool_arguments_dict(fn))
                if not _ff5_cmd_readonly(cmd):
                    return _ff5_say(
                        ctx, "Yêu cầu này chỉ cho phép lệnh chỉ-đọc nên mình không chạy `%s`."
                             % cmd[:160],
                        "Only read-only commands are allowed here, so I did not run `%s`."
                        % cmd[:160]).text, None
        scope = _ff5_scope_problems(calls, messages, perm)
        if scope:
            body = "\n".join("- " + x for x in scope[:5])
            return _ff5_say(
                ctx, "Mình không ghi ra ngoài phạm vi được phép:\n" + body,
                "I did not write outside the allowed scope:\n" + body).text, None
    return "", calls


def _ff5_install_banner():
    """Banner, planner and validator now share the same SPEC (proposal 6)."""
    if not _c5("flow6_banner"):
        return
    if getattr(T, "_TASK_BANNER_HOOK", None) is not None:
        return
    T._TASK_BANNER_HOOK = _ff5_spec_banner



# --------------------------------------------------------------------------
# Proposal 6: one banner generated from the same SPEC as the planner.
# --------------------------------------------------------------------------

def _ff5_spec_banner(messages):
    try:
        last = ""
        for msg in messages or []:
            if isinstance(msg, dict) and msg.get("role") == "user":
                last = T._stringify_content(msg.get("content", ""))
        if not last:
            return _ff5_prev_banner(messages)
        from . import server as _srv
        pattern = T.detect_task_pattern(messages)
        fix = False
        if not pattern:
            tmp = _Ctx()
            tmp.last, tmp.request = last, last
            tmp.scannable = T._strip_code_spans(last)
            tmp.folded = re.sub(r"\s+", " ",
                                T._fold_diacritics(tmp.scannable)).strip()
            tmp.pattern = None
            try:
                det = _ff5_detect(tmp)
            except Exception:
                det = None
            if det:
                pattern, fix = det, bool(getattr(tmp, "fix", False))
        if pattern not in _FF5_SPEC:
            return _ff5_prev_banner(messages)
        perm = _ff5_permissions(messages, pattern, fix)
        # Same downgrade the planner performs: a CHANGE/CREATE request without
        # write permission is guidance only, so the banner must not announce an
        # edit stage the validator will reject.
        if pattern in ("CHANGE", "CREATE") and not perm.edit:
            pattern = "EXPLAIN"
            fix = False
            perm = _ff5_permissions(messages, pattern, fix)
        steps = _ff5_allowed_steps(pattern, fix)
        if not perm.edit:
            steps = [s for s in steps if s not in ("edit", "diff")]
        if perm.run == "none":
            steps = [s for s in steps if s != "verify"]
        limits = []
        if not perm.edit:
            limits.append("no file edits")
        if perm.run == "none":
            limits.append("no commands")
        elif perm.run == "readonly":
            limits.append("read-only commands")
        tail = (" Limits: " + ", ".join(limits) + ".") if limits else ""
        return ("[Task pattern: %s. Mandatory flow: %s.%s]"
                % (pattern, " -> ".join(steps), tail))
    except Exception as e:
        log(f"[fix6] banner error, using the previous banner: {e}")
        return _ff5_prev_banner(messages)


_FF5_REVERT_RE = re.compile(
    r"\b(?:revert|undo|rollback|roll\s+back|hoan\s+tac|khoi\s+phuc|huy|quay\s+lai|tra\s+ve)\b"
    r"[^.\n]{0,80}?\b(?:thay\s+doi|changes?|session|modifications?|edits?|sua\s+doi|tat\s+ca|everything)\b")
_FF5_RUNV_RE = re.compile(r"\b(?:run|execute|exec|chay|thuc\s+thi)\b")
_FF5_SHOWV_RE = re.compile(r"\b(?:show|xem|cho|list|liet\s+ke|display)\b")
_FF5_TESTS_RE = re.compile(
    r"\b(?:run|execute|chay|thuc\s+thi)\b[^.\n]{0,25}?"
    r"\b(?:unit\s*tests?|tests?|test\s+suite|pytest|unittest)\b")
_FF5_GIT_RE = re.compile(
    r"\bgit\s+(?:--no-pager\s+)?(status|log|diff|branch|show)\b([^\n`\"';&|<>]*)", re.I)
_FF5_CMD_START_RE = re.compile(
    r"^(?:git|npm|npx|pip|pip3|python|python3|node|make|cargo|go|docker|ls|dir|cat|"
    r"pytest|uv|yarn|pnpm|dotnet|mvn|gradle)\b", re.I)

# The user forbids touching files. Object-aware on purpose: "don't change the
# API, just add logging" must NOT match.
_FF5_NOEDIT_RE = re.compile(
    r"\b(?:do\s+not|don'?t|dont|never)\s+(?:edit|modify|change|touch|write\s+to|alter|"
    r"overwrite|rewrite|update)\s+(?:(?:my|the|any|these|those|your|our)\s+)?"
    r"(?:files?|code|source|anything|repo|repository|project|codebase)\b"
    r"|\bwithout\s+(?:editing|modifying|changing|touching|writing|altering)\b"
    r"|\bread[\s-]*only\b"
    r"|\b(?:khong|dung)\s+(?:duoc\s+)?(?:sua|chinh\s+sua|thay\s+doi|ghi|cap\s+nhat|dong\s+vao)"
    r"\s+(?:vao\s+)?(?:file|code|ma\s+nguon|source|bat\s+ky|gi|du\s+an)\b"
    r"|\bchi\s+(?:doc|giai\s+thich|huong\s+dan|goi\s+y)\b")

_FF5_DESTRUCTIVE_RE = re.compile(
    r"\bgit\s+(?:clean\s+-\w*[fdx]|reset\s+--hard|checkout\s+(?:--\s+)?\.(?:\s|$)|"
    r"restore\s+(?:--\S+\s+)*\.(?:\s|$)|stash\s+(?:drop|clear))"
    r"|\brm\s+-\w*r\w*f|\bremove-item\b[^\n]*-recurse|\brmdir\s+/s|\bdel\s+/s", re.I)

_FF5_READONLY_CMD_RE = re.compile(
    r"^\s*(?:git\s+(?:--no-pager\s+)?(?:status|diff|log|ls-files|show|branch|rev-parse)\b|"
    r"ls\b|dir\b|cat\b|type\b|echo\b|find\b|grep\b|rg\b|head\b|tail\b|pwd\b|cd\b|"
    r"python3?\s+-m\s+(?:py_compile|unittest|pytest)\b|pytest\b|npm\s+(?:run\s+)?test\b|"
    r"go\s+test\b|cargo\s+test\b)", re.I)
_FF5_FAIL_RE = re.compile(
    r"(?i)\b(?:error|cannot|denied|not\s+exist|exception|failed|fatal)\b|Exited with code [1-9]")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

class _Ff5Step:
    """Planned step: either tool calls to emit, or a final local text."""

    def __init__(self, name, calls=None, text=None):
        self.name, self.calls, self.text = name, calls, text


def _ff5_vi(ctx):
    return bool(_VI_RE.search((getattr(ctx, "last", "") or "").lower()))


def _ff5_say(ctx, vi_text, en_text):
    return _Ff5Step("say", text=vi_text if _ff5_vi(ctx) else en_text)


def _ff5_last_user_text(messages):
    idx = T._last_user_index(messages)
    if idx < 0:
        return ""
    return T._stringify_content(messages[idx].get("content", ""))


def _ff5_noedit_text(text):
    if not isinstance(text, str) or not text:
        return False
    scannable = T._strip_code_spans(text)
    folded = re.sub(r"\s+", " ", T._fold_diacritics(scannable)).lower()
    return bool(_FF5_NOEDIT_RE.search(folded))


def _ff5_is_windows(messages):
    pat = re.compile(r"ParserError|PowerShell|not a valid statement separator|\bwin32\b|"
                     r"Platform:\s*windows|\b[A-Za-z]:[\\/]", re.I)
    for msg in messages or []:
        if isinstance(msg, dict):
            body = T._stringify_content(msg.get("content", ""))[:20000]
            if pat.search(body):
                return True
    return False


def _ff5_split_and(cmd):
    parts, buf, quote, i = [], [], None, 0
    while i < len(cmd):
        ch = cmd[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif cmd.startswith("&&", i):
            parts.append("".join(buf).strip())
            buf = []
            i += 2
            continue
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf).strip())
    return [p for p in parts if p]


def _ff5_ps_command(cmd):
    """`a && b && c` -> `a; if ($?) { b; if ($?) { c } }` (Windows PowerShell 5.1)."""
    parts = _ff5_split_and(cmd)
    if len(parts) < 2:
        return cmd
    out = parts[-1]
    for p in reversed(parts[:-1]):
        out = f"{p}; if ($?) {{ {out} }}"
    return out


def _ff5_fix_for(cmd, messages):
    if _c5("flow5_ps_fix") and "&&" in cmd and _ff5_is_windows(messages):
        return _ff5_ps_command(cmd)
    return cmd


def _ff5_sig(call):
    fn = call["function"]
    return (fn["name"], T._canonical_args(T._tool_arguments_dict(fn)))


def _ff5_mk(name, args):
    return {"id": "call_" + uuid.uuid4().hex[:8], "type": "function",
            "function": {"name": name,
                         "arguments": json.dumps(args, ensure_ascii=False)}}


def _ff5_allowed(tm):
    return {n for names in tm.by_kind.values() for n in names}


def _ff5_cmd_failed(res):
    return bool(_FF5_FAIL_RE.search(res or ""))


# --------------------------------------------------------------------------
# pattern detection + model-assisted classification
# --------------------------------------------------------------------------

def _ff5_command_from(raw):
    for span in re.findall(r"`([^`\n]+)`", raw or ""):
        s = span.strip()
        if _FF5_CMD_START_RE.match(s) and (" " in s or s.lower() in ("ls", "dir", "pytest")):
            return s
    m = re.search(r"```(?:\w+)?\n([^`]+?)```", raw or "", re.S)
    if m:
        lines = m.group(1).strip().splitlines()
        if lines and _FF5_CMD_START_RE.match(lines[0].strip()):
            return lines[0].strip()
    return ""


def _ff5_git_cmd(m):
    sub = m.group(1).lower()
    flags = []
    for tok in (m.group(2) or "").split():
        if tok.startswith("-"):
            flags.append(tok)
        else:
            break
    if not flags:
        flags = {"status": ["--short", "--branch"],
                 "log": ["--oneline", "-n", "20"]}.get(sub, [])
    return " ".join(["git", "--no-pager", sub] + flags)


_FF5_PLAN_RE = re.compile(
    r"\b(?:write|draft|make|give\s+me|create|lap|viet|soan)\s+(?:me\s+)?(?:an?\s+|the\s+)?"
    r"(?:implementation\s+|detailed\s+)?(?:plan|ke\s+hoach)\b|\bplan\s+(?:for|how|to)\b|\blap\s+ke\s+hoach\b")


def _ff5_detect(ctx):
    """Pattern for the new request shapes, else None."""
    folded = (ctx.folded or "").lower()
    raw = ctx.last or ""
    prev = getattr(ctx, "pattern", None)
    if _FF5_REVERT_RE.search(folded):
        return "REVERT"
    if _c5("flow5_plan") and prev in (None, "EXPLAIN", "CHANGE", "CREATE") and _FF5_PLAN_RE.search(folded):
        # "plan" is intent=explain with a plan-shaped answer; it never writes.
        ctx.plan = True
        return "EXPLAIN"
    if prev in (None, "TOOL_DEMAND", "EXPLAIN"):
        m = _FF5_GIT_RE.search(raw)
        if m and (_FF5_RUNV_RE.search(folded) or _FF5_SHOWV_RE.search(folded)):
            ctx.git_cmd = _ff5_git_cmd(m)
            return "GIT_INFO"
        if _FF5_RUNV_RE.search(folded):
            cmd = _ff5_command_from(raw)
            if cmd:
                ctx.cmd = cmd
                return "RUN_COMMAND"
            if _FF5_TESTS_RE.search(folded):
                return "RUN_TESTS"
    return None


_ff5_tls = _ff5_threading.local()
_ff5_cache = {}


def _ff5_classify_prompt(text):
    lines = ["Classify the user's request into exactly ONE label from the list, "
             "or NONE if no label fits.", "Labels:"]
    lines += [f"- {k}: {v}" for k, v in _FF5_CATALOG.items()]
    lines += ["- NONE: anything else (chat, a question unrelated to the project, ...)",
              "", "Request:", text.strip()[:600], "",
              "Answer with the label only, nothing else."]
    return T.defuse_extension_words("\n".join(lines))


def _ff5_classify(text):
    """Label from the catalog, or None (NONE / unknown / error -> model runs freely)."""
    text = (text or "").strip()
    if len(text) < 8:
        return None
    key = _ff5_hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()
    if key in _ff5_cache:
        return _ff5_cache[key]
    model = getattr(_ff5_tls, "model", None)
    if not model:
        return None
    label = None
    try:
        m_id, m_think, m_extra, m_ticket = model
        raw = generate(_ff5_classify_prompt(text), m_id, m_think, None, m_extra, m_ticket)
        if raw and not T.looks_like_canned_failure(raw):
            for w in re.findall(r"[A-Z][A-Z_]{3,}", raw.upper()):
                if w in _FF5_CATALOG:
                    label = w
                    break
                if w == "NONE":
                    break
        log(f"[fix5] model classified the request as {label or 'NONE'}")
    except Exception as e:
        log(f"[fix5] classify failed, model runs freely: {e}")
    if len(_ff5_cache) > 300:
        _ff5_cache.clear()
    _ff5_cache[key] = label
    return label


def _ff5_apply_label(ctx, label):
    if not label or label not in _FF5_CATALOG:
        return
    raw = ctx.last or ""
    if label == "PLAN":
        # PLAN is intent=explain with a plan-shaped answer; it never writes.
        ctx.plan = True
        label = "EXPLAIN"
    if label == "RUN_COMMAND":
        cmd = _ff5_command_from(raw)
        if not cmd:
            return
        ctx.cmd = cmd
    elif label == "GIT_INFO":
        m = _FF5_GIT_RE.search(raw)
        ctx.git_cmd = _ff5_git_cmd(m) if m else "git --no-pager status --short --branch"
    elif label == "CREATE":
        files = _named_files(ctx)
        ctx.target = files[0] if files else ""
        ctx.dir_cands = _dir_cands(raw)
    elif label == "DEBUG":
        ctx.fix = bool(_FIX_RE.search((ctx.folded or "").lower()))
    ctx.pattern = label


def _ff5_supplement(ctx):
    """True when the newest message only supplements the previous request
    (pasted log / traceback, a bare path, "continue")."""
    raw = (ctx.last or "").strip()
    if not raw:
        return False
    if _TRACE_RE.search(raw):
        return True
    f = _ff5_fold(raw)
    if re.match(r"^\W*(?:ok|okay|yes|continue|go\s+on|proceed|tiep\s+tuc|tiep|duoc)\W*$", f):
        return True
    return len(raw) < 300 and bool(_named_files(ctx)) and not re.search(r"\w{3,}\s+\w{3,}\s+\w{3,}\s+\w{3,}", f)


def _ff5_inherit(messages):
    idxs = [i for i, m in enumerate(messages) if isinstance(m, dict) and m.get("role") == "user"]
    for j in reversed(idxs[:-1]):
        prev = _ff5_prev_analyze(messages[:j + 1])
        if prev is None:
            continue
        for k, v in (("git_cmd", ""), ("cmd", ""), ("fix", False), ("target", ""),
                     ("create_merge", False), ("dir_cands", [])):
            if not hasattr(prev, k):
                setattr(prev, k, v)
        if not getattr(prev, "pattern", None):
            try:
                det = _ff5_detect(prev)
            except Exception:
                det = None
            if det:
                prev.pattern = det
        if getattr(prev, "pattern", None):
            return prev
    return None


def _analyze(messages):
    ctx = _ff5_prev_analyze(messages)
    if ctx is None or not _c5("flow5_enabled"):
        return ctx
    try:
        for k, v in (("git_cmd", ""), ("cmd", ""), ("fix", False), ("target", ""),
                     ("create_merge", False), ("dir_cands", []), ("no_edit", False)):
            if not hasattr(ctx, k):
                setattr(ctx, k, v)
        if _clarified(messages, ctx.idx):
            ctx.perm = _ff5_permissions(messages, ctx.pattern, ctx.fix)
            ctx.perm.scope = _ff5_scope(ctx, messages)
            return ctx
        det = _ff5_detect(ctx)
        if det:
            ctx.pattern = det
        elif ctx.pattern is None and _c5("flow5_sticky") and _ff5_supplement(ctx):
            prev = _ff5_inherit(messages)
            if prev is not None:
                log(f"[fix5] follow-up message: keeping the earlier goal ({prev.pattern})")
                ctx.pattern = prev.pattern
                ctx.request = (prev.request or "") + "\n" + (ctx.last or "")
                ctx.scannable = (getattr(prev, "scannable", "") or "") + "\n" + ctx.scannable
                ctx.folded = (getattr(prev, "folded", "") or "") + " " + ctx.folded
                for k in ("fix", "target", "create_merge", "dir_cands", "cmd", "git_cmd"):
                    setattr(ctx, k, getattr(prev, k, getattr(ctx, k)))
        if ctx.pattern is None and _c5("flow5_classify"):
            _ff5_apply_label(ctx, _ff5_classify(ctx.last))
        perm = _ff5_permissions(messages, ctx.pattern, ctx.fix)
        # Proposal 1: "how can I fix X?" / "do not edit" -> intent stays explain.
        if ctx.pattern in ("CHANGE", "CREATE") and not perm.edit and _c5("flow5_respect_no_edit"):
            log(f"[fix5] {ctx.pattern} -> EXPLAIN ({perm.reason})")
            ctx.pattern = "EXPLAIN"
            perm = _ff5_permissions(messages, ctx.pattern, ctx.fix)
        if ctx.pattern == "DEBUG" and ctx.fix and not perm.edit:
            ctx.fix = False
            perm = _ff5_permissions(messages, ctx.pattern, False)
        ctx.perm = perm
        ctx.no_edit = not perm.edit
        ctx.intent = (ctx.pattern or "").lower()
        ctx.scope = _named_files(ctx)
        perm.scope = _ff5_scope(ctx, messages)
    except Exception as e:
        log(f"[fix5] analyze error: {e}")
    if not hasattr(ctx, "perm"):
        ctx.perm = _Ff5Perm(True, "any", "default", False)
        ctx.perm.scope = None
    return ctx


# --------------------------------------------------------------------------
# REVERT: undo exactly what this conversation changed, nothing else
# --------------------------------------------------------------------------

def _ff5_session_acts(hist):
    """Successful write/edit calls in ``hist`` (messages BEFORE the revert request)."""
    calls, out = {}, []
    for i, msg in enumerate(hist):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("id"):
                    fn = tc.get("function") or {}
                    calls[tc["id"]] = (i, fn.get("name") or "", T._tool_arguments_dict(fn))
        elif msg.get("role") == "tool":
            hit = calls.get(msg.get("tool_call_id"))
            if not hit or tool_kind(hit[1]) != "act":
                continue
            res = T._stringify_content(msg.get("content"))
            path = _call_path(hit[2])
            if not path or not _act_ok({"result": res}):
                continue
            op = "edit" if any(isinstance(hit[2].get(k), str) for k in _OLD_KEYS) else "write"
            out.append({"idx": hit[0], "name": hit[1], "path": path, "op": op,
                        "args": hit[2]})
    return out


def _ff5_session_exec_files(hist):
    """Files named in the command/output of non-read-only shell calls."""
    calls, out = {}, []
    for msg in hist:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("id"):
                    fn = tc.get("function") or {}
                    calls[tc["id"]] = (fn.get("name") or "", T._tool_arguments_dict(fn))
        elif msg.get("role") == "tool":
            hit = calls.get(msg.get("tool_call_id"))
            if not hit or tool_kind(hit[0]) != "exec":
                continue
            cmd = _call_command(hit[1])
            if _FF5_READONLY_CMD_RE.match(cmd):
                continue
            res = T._stringify_content(msg.get("content"))[:4000]
            for f in sorted(T._files_in(cmd, res)):
                if f.lower() not in T._NON_FILE_NAMES and f not in out:
                    out.append(f)
    return out


def _ff5_read_before(hist, path, idx):
    for msg in hist[:idx]:
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                fn = (tc or {}).get("function") or {}
                if tool_kind(fn.get("name")) == "read":
                    p = _call_path(T._tool_arguments_dict(fn))
                    if p and T._path_seen(path, [p]):
                        return True
    return False


def _ff5_parse_status(text):
    """{path: XY} from `git status --short`; None when it is not a usable git status."""
    low = (text or "").lower()
    if "not a git repository" in low or re.search(
            r"\bfatal:|is not recognized|command not found", low):
        return None
    out = {}
    for ln in (text or "").splitlines():
        m = re.match(r"^([ MADRCUT?!]{2}) (.+?)\s*$", ln.rstrip("\r"))
        if m:
            p = m.group(2).split(" -> ")[-1].strip().strip('"').replace("\\", "/")
            out[p] = m.group(1)
    return out


def _ff5_status_path(path, status):
    for p in status or {}:
        if not p.endswith("/") and T._path_seen(path, [p]):
            return p
    return None


def _ff5_inverse_edits(ops, allowed):
    out = []
    for o in reversed(ops):
        args = o["args"]
        okey = next((k for k in _OLD_KEYS if isinstance(args.get(k), str)), None)
        nkey = next((k for k in _NEW_KEYS if isinstance(args.get(k), str)), None)
        if not okey or not nkey or not args[nkey] or o["name"] not in allowed:
            return None            # a pure deletion cannot be inverted safely
        inv = dict(args)
        inv[okey], inv[nkey] = args[nkey], args[okey]
        out.append((o, inv))
    return out


def _ff5_revert_actions(sess, xfiles, status, hist, tm, win):
    """(actions, notes). Conservative: only undo what is provably this session's."""
    actions, notes, handled = [], [], set()
    allowed = _ff5_allowed(tm)
    by_path = {}
    for a in sess:
        by_path.setdefault(_norm(a["path"]).lower(), []).append(a)

    def add_edits(path, ops):
        inv = _ff5_inverse_edits(ops, allowed)
        if inv is None:
            notes.append(("manual_checkout", path))
            return
        for o, args in inv:
            call = _ff5_mk(o["name"], args)
            actions.append({"kind": "edit", "call": call, "sig": _ff5_sig(call),
                            "label": "undo edit in " + path})

    def add_delete(spath):
        q = spath.replace('"', "")
        cmd = ('Remove-Item -LiteralPath "%s" -Force' % q) if win else ('rm -f -- "%s"' % q)
        call = tm.make_call("exec", command=cmd, description="Delete a file created in this session")
        if call:
            actions.append({"kind": "delete", "call": call, "sig": _ff5_sig(call),
                            "label": "delete " + spath})

    for _key, ops in by_path.items():
        path = ops[0]["path"]
        if status is None:
            if all(o["op"] == "edit" for o in ops):
                add_edits(path, ops)
            else:
                notes.append(("nogit", path))
            continue
        spath = _ff5_status_path(path, status)
        if spath is None:
            notes.append(("clean", path))
            continue
        handled.add(spath)
        code = status.get(spath, "")
        if code == "??":
            if _ff5_read_before(hist, path, ops[0]["idx"]):
                notes.append(("manual_overwrite", spath))
            else:
                add_delete(spath)
        elif all(o["op"] == "edit" for o in ops):
            add_edits(spath, ops)
        else:
            notes.append(("manual_checkout", spath))
    for f in xfiles:
        spath = _ff5_status_path(f, status) if status else None
        if not spath or spath in handled:
            continue
        handled.add(spath)
        if status.get(spath) == "??":
            add_delete(spath)
        else:
            notes.append(("manual_exec", spath))
    return actions, notes


def _ff5_revert_report(ctx, actions, exe, notes, runs):
    vi = _ff5_vi(ctx)

    def t(a, b):
        return a if vi else b

    done, failed = [], []
    for a in actions:
        res = exe.get(a["sig"])
        if res is None:
            bad = True
        elif a["kind"] == "edit":
            bad = not _act_ok({"result": res})
        else:
            bad = _ff5_cmd_failed(res)
        (failed if bad else done).append((a, res))
    L = []
    if done:
        L.append(t("**Đã hoàn tác:**", "**Reverted:**"))
        L += ["- " + a["label"] for a, _r in done]
    if failed:
        L.append(t("**Không hoàn tác được:**", "**Could NOT be reverted:**"))
        L += ["- %s -- %s" % (a["label"], " ".join((r or "not run").split())[:200])
              for a, r in failed]
    man = []
    for n in notes:
        k, p = n[0], n[1]
        if k == "manual_checkout":
            man.append(t("- %s: file có trong git nhưng bị ghi đè/sửa theo cách không đảo ngược an toàn được; "
                         "chưa tự hoàn tác vì có thể mất thay đổi chưa commit từ trước. Nếu chắc chắn: "
                         "git checkout -- \"%s\"",
                         "- %s: tracked file overwritten in a way that cannot be inverted safely; not "
                         "reverted because that could drop earlier uncommitted work. If you are sure: "
                         "git checkout -- \"%s\"") % (p, p))
        elif k == "manual_overwrite":
            man.append(t("- %s: file chưa được git theo dõi nhưng đã tồn tại trước session; không xoá.",
                         "- %s: untracked file that already existed before this session; not deleted.") % p)
        elif k == "manual_exec":
            man.append(t("- %s: bị thay đổi bởi một lệnh shell trong session; không tự hoàn tác.",
                         "- %s: changed by a shell command in this session; not reverted automatically.") % p)
        elif k == "nogit":
            man.append(t("- %s: không có git nên không xác minh được trạng thái; không tự hoàn tác.",
                         "- %s: no git repository, state cannot be verified; not reverted automatically.") % p)
    if man:
        L.append(t("**Cần bạn tự quyết:**", "**Left for you to decide:**"))
        L += man
    if actions:
        after = [ln for ln in (runs[-1]["result"] or "").splitlines() if ln.strip()]
        L.append(t("**git status sau khi hoàn tác:**", "**git status afterwards:**"))
        L.append("```\n" + ("\n".join(after[:20]) or t("(sạch)", "(clean)")) + "\n```")
    if not done and not failed and not man:
        L.append(t("Không có thay đổi nào của session cần hoàn tác (mọi file đã khớp git).",
                   "Nothing from this session needed reverting."))
    L.append(t("Lưu ý: chỉ động tới file mà session này đã ghi/sửa (file tạo gián tiếp qua script chỉ "
               "được nhận ra khi tên nó xuất hiện trong lệnh hoặc output); công việc chưa commit khác "
               "của bạn được giữ nguyên.",
               "Note: only files this session wrote or edited were touched (files created indirectly "
               "by a script are recognised only when their name appears in the command or output); "
               "your other uncommitted work was left alone."))
    return "\n".join(L)


def _ff5_plan_revert(ctx, messages, tm):
    ev = _answered_calls(messages)
    start = T._last_user_index(messages)
    hist = messages[:max(start, 0)]
    sess = [a for a in _ff5_session_acts(hist) if _base(a["path"]) != _MERGE_SCRIPT]
    xfiles = _ff5_session_exec_files(hist)
    if not sess and not xfiles:
        return _ff5_say(ctx, "Không thấy thay đổi nào do session này thực hiện để hoàn tác.",
                        "I found no change made by this conversation that needs reverting.")
    if not tm.first("exec"):
        return _ff5_say(ctx, "Cần công cụ shell để kiểm tra git status nhưng không có.",
                        "I need a shell tool to inspect git status and none is available.")
    runs = [e for e in ev if e["kind"] == "exec"
            and re.search(r"\bgit\b[^\n]*\bstatus\b", _call_command(e["args"]))]
    if not runs:
        call = tm.make_call("exec", command="git status --short",
                            description="List uncommitted changes")
        if call:
            return _Ff5Step("status", calls=[call])
        return _ff5_say(ctx, "Không tạo được lệnh git status.", "Could not build git status.")
    status = _ff5_parse_status(runs[0]["result"])
    win = _ff5_is_windows(messages)
    actions, notes = _ff5_revert_actions(sess, xfiles, status, hist, tm, win)
    exe = {(e["name"], T._canonical_args(e["args"])): e["result"] for e in ev}
    for a in actions:
        if a["sig"] not in exe:
            return _Ff5Step("revert", calls=[a["call"]])
    if actions and len(runs) < 2:
        call = tm.make_call("exec", command="git status --short", description="Verify the result")
        if call:
            return _Ff5Step("verify", calls=[call])
    return _Ff5Step("say", text=_ff5_revert_report(ctx, actions, exe, notes, runs))


# --------------------------------------------------------------------------
# RUN_TESTS / RUN_COMMAND / GIT_INFO
# --------------------------------------------------------------------------

def _ff5_block(out, limit=4000):
    out = (out or "").strip()
    if len(out) > limit:
        out = "[...]\n" + out[-limit:]
    return "```\n" + (out or "(no output)") + "\n```"


def _ff5_plan_tests(ctx, messages, tm):
    ev = _answered_calls(messages)
    if not tm.first("exec"):
        return _ff5_say(ctx, "Không có công cụ shell để chạy test.", "No shell tool to run tests.")
    listing = _listing_files(ev)
    if not listing and _survey_count(ev) < 1:
        s = _survey_step("RUN_TESTS", tm, ev, 1, 3)
        if s and s.calls:
            return _Ff5Step("survey", calls=s.calls)
    names_all = listing + [x for x in (_call_path(e["args"]) for e in ev) if x]
    cmd = pick_test_command(names_all, ev)
    if cmd:
        call = tm.make_call("exec", command=_ff5_fix_for(cmd, messages),
                            description="Run unit tests")
        if call:
            return _Ff5Step("tests", calls=[call])
    runs = _test_runs(ev)
    if not runs:
        return _ff5_say(ctx, "Không đoán được lệnh test của project. Hãy cho mình biết lệnh cần chạy.",
                        "I could not work out this project's test command. Tell me which one to run.")
    _i, c, res = runs[-1]
    outcome = test_outcome(res)
    summ = " | ".join(_test_summary([{"result": res}]))
    vi = _ff5_vi(ctx)
    head = {"pass": ("Test PASS", "Tests PASSED"), "fail": ("Test FAIL", "Tests FAILED"),
            "broken": ("Lệnh test không chạy được", "The test command itself could not run"),
            }.get(outcome, ("Không xác định được kết quả test", "Test outcome unclear"))
    body = _excerpt(res, 1500) if outcome in ("fail", "broken") else summ
    return _Ff5Step("say", text="%s (`%s`)\n%s" % (head[0] if vi else head[1], c,
                                                    _ff5_block(body, 1600)))


def _ff5_plan_command(ctx, messages, tm, cmd, label):
    ev = _answered_calls(messages)
    cmd = _ff5_fix_for(cmd, messages)
    runs = [e for e in ev if e["kind"] == "exec" and _call_command(e["args"]).strip() == cmd.strip()]
    if not runs:
        call = tm.make_call("exec", command=cmd, description=label) if tm.first("exec") else None
        if call:
            return _Ff5Step("exec", calls=[call])
        return _ff5_say(ctx, "Không có công cụ shell để chạy lệnh.", "No shell tool to run the command.")
    return _Ff5Step("say", text="`%s`\n%s" % (cmd, _ff5_block(runs[-1]["result"])))


def _ff5_plan(ctx, messages, tm):
    p = ctx.pattern
    if p == "REVERT":
        return _ff5_plan_revert(ctx, messages, tm)
    if p == "RUN_TESTS":
        return _ff5_plan_tests(ctx, messages, tm)
    if p == "RUN_COMMAND" and ctx.cmd:
        return _ff5_plan_command(ctx, messages, tm, ctx.cmd, "Run the requested command")
    if p == "GIT_INFO" and ctx.git_cmd:
        return _ff5_plan_command(ctx, messages, tm, ctx.git_cmd, "Show git information")
    return None


# --------------------------------------------------------------------------
# Findings 3 + 4: fail-closed act gate, read guard, per-file quote verifier
# --------------------------------------------------------------------------

def _ff5_read_guard(ctx, messages):
    """Text when a failed read would otherwise lead on to an edit step, else None."""
    if getattr(ctx, "pattern", None) not in ("CHANGE", "DEBUG"):
        return None
    if ctx.pattern == "DEBUG" and not getattr(ctx, "fix", False):
        return None
    ev = _answered_calls(messages)
    if any(e["kind"] == "act" for e in ev):
        return None
    reads = [e for e in ev if e["kind"] == "read"]
    bad = [e for e in reads if _ff5_status(e) == "failed" and not _is_doc(_call_path(e["args"]) or "")]
    if not bad:
        return None
    good = [e for e in reads if _ff5_status(e) in ("success", "partial")
            and not _is_doc(_call_path(e["args"]) or "")]
    named = [f.lower() for f in _named_files(ctx)]
    bad_named = any(any(T._path_seen(n, [_call_path(e["args"]) or ""]) for n in named)
                    for e in bad)
    if good and not bad_named:
        return None
    detail = "; ".join("%s: %s" % (_call_path(e["args"]), " ".join((e["result"] or "").split())[:120])
                       for e in bad[:3])
    if _ff5_vi(ctx):
        return ("Không đọc được file cần sửa nên mình KHÔNG thực hiện chỉnh sửa nào (" + detail +
                "). Hãy kiểm tra lại đường dẫn rồi yêu cầu lại.")
    return ("I could not read the file to change, so I made NO edit (" + detail +
            "). Check the path and ask again.")


_FF5_PART_RE = re.compile(r"\s*\(part\s+\d+\)\s*$")


def _ff5_quote_ok(snip, cand, line):
    qn = T._norm_code(snip)
    if not qn:
        return False
    for label, body in cand:
        is_part = bool(_FF5_PART_RE.search(label))
        for i, ln in enumerate(body.split("\n")):
            if qn in T._norm_code(ln):
                if line is None or is_part:
                    return True
                mm = re.match(r"^\s*(\d+)\s*(?:\||:|\t|\u2192)", ln)
                actual = int(mm.group(1)) if mm else i + 1
                if abs(actual - line) <= 5:
                    return True
    return False


def _verify_findings(text, chunk):
    """(kept, dropped). A quote counts only inside the file the finding names,
    and (when the file shows line numbers) within 5 lines of the claimed one."""
    kept, dropped = [], 0
    if re.fullmatch(r"\W*NONE\W*", (text or "").strip(), re.IGNORECASE):
        return kept, dropped
    files = [(p, b) for p, b in chunk]
    for item in _split_findings(text):
        snippets = re.findall(r"`([^`\n]{8,})`", item)
        if not snippets:
            dropped += 1
            continue
        head = item.split("`", 1)[0]
        m = re.search(r"((?:[A-Za-z]:)?[^\s:`'\"<>|*?]+?\.[A-Za-z0-9]{1,8})"
                      r"(?:\s*\(part\s+\d+\))?\s*(?::\s*(\d+))?", head)
        line = int(m.group(2)) if m and m.group(2) else None
        if m:
            want = m.group(1).replace("\\", "/")
            cand = [(p, b) for p, b in files
                    if T._path_seen(want, [_FF5_PART_RE.sub("", p).replace("\\", "/")])]
            if not cand:
                dropped += 1          # names a file that is not in this chunk
                continue
        else:
            cand = files
        if all(_ff5_quote_ok(s, cand, line) for s in snippets):
            kept.append(item)
        else:
            dropped += 1
    return kept, dropped


# --------------------------------------------------------------------------
# post-filter on whatever the planner/model produced
# --------------------------------------------------------------------------

def _ff5_postfilter(result, messages, perm=None, last=True, tm=None):
    """Returns (result, nudge). ``nudge`` is a corrective instruction naming the
    SPECIFIC problem when a guard rejected the output and a retry is still
    allowed (last=False); on the last attempt the guard blocks instead
    (fail closed) and ``nudge`` is None."""
    if not (isinstance(result, tuple) and len(result) == 2):
        return result, None
    text, calls = result
    if not calls:
        return result, None
    last_user = _ff5_last_user_text(messages)
    vi = bool(_VI_RE.search(last_user.lower()))
    if perm is None:
        perm = _ff5_permissions(messages)

    def blocked(vi_text, en_text, nudge=None, retry=False):
        if retry and not last and nudge:
            return None, nudge
        msg = vi_text if vi else en_text
        return (((text + "\n\n") if text else "") + msg, None), None

    # 0. adapt every call to the schema the client actually declared (proposal 3)
    if tm is not None:
        calls, probs = _ff5_adapt_calls(calls, tm)
        if probs:
            body = "\n".join("- " + x for x in probs[:5])
            log("[fix6] tool calls did not match the client schema: " + " | ".join(probs))
            return blocked(
                "Mình chưa gửi lời gọi công cụ vì nó không khớp schema của client "
                "(chưa có gì được thực thi):\n" + body,
                "I did not send the tool call because it does not match the "
                "client's schema (nothing ran):\n" + body,
                "Your tool call does not match the tool schema. Fix exactly these "
                "problems and call it again:\n" + body, retry=True)

    # 0b. scope: writes are limited to the files the user named/read (proposal 1)
    if _c5("flow6_scope_guard"):
        scope = _ff5_scope_problems(calls, messages, perm)
        if scope:
            body = "\n".join("- " + x for x in scope[:5])
            log("[fix6] out-of-scope write(s): " + " | ".join(scope))
            return blocked(
                "Mình chưa ghi vì (các) file đó nằm ngoài phạm vi yêu cầu này "
                "(chưa có gì được ghi):\n" + body,
                "I did not write because those files are outside this request's "
                "scope (nothing was written):\n" + body,
                "Your edit targets a file outside this request. Edit only the "
                "files the user named or the files read in this turn:\n" + body,
                retry=True)

    # 1. intent vs permission: the user (or a guidance question) forbade edits
    if _c5("flow5_respect_no_edit") and not perm.edit:
        kept = [tc for tc in calls
                if tool_kind((tc.get("function") or {}).get("name")) != "act"]
        if len(kept) != len(calls):
            log("[fix5] dropped file-writing tool call(s): %s" % perm.reason)
            if not kept:
                return blocked("Bạn không cho phép sửa file (%s) nên mình không thực hiện chỉnh sửa nào." % perm.reason,
                               "Edits are not allowed here (%s), so I made no edit." % perm.reason)[0], None
            calls = kept

    # 2. command policy: three permission levels, wide destructive commands,
    #    PowerShell syntax.
    low_user = last_user.lower()
    win = _c5("flow5_ps_fix") and _ff5_is_windows(messages)
    if perm.run == "none":
        kept = [tc for tc in calls
                if tool_kind((tc.get("function") or {}).get("name")) != "exec"]
        if len(kept) != len(calls):
            log("[fix6] dropped command(s): the user forbade running any command")
            if not kept:
                return blocked(
                    "Bạn không cho phép chạy lệnh nên mình không chạy gì cả (%s)."
                    % perm.reason,
                    "Running commands is not allowed here (%s), so I ran nothing."
                    % perm.reason,
                    "Do not run any command. Answer from the files you already "
                    "read, or say what information you still need.", retry=True)
            calls = kept
    new = []
    for tc in calls:
        fn = tc.get("function") or {}
        if tool_kind(fn.get("name")) != "exec":
            new.append(tc)
            continue
        args = T._tool_arguments_dict(fn)
        ckey = next((k for k in _CMD_KEYS if isinstance(args.get(k), str)), None)
        if not ckey:
            new.append(tc)
            continue
        cmd = args[ckey]
        if perm.run == "readonly" and not _ff5_cmd_readonly(cmd):
            log("[fix5] blocked a non read-only command in read-only mode: " + cmd[:200])
            return blocked("Mình chặn lệnh `%s` vì yêu cầu này chỉ cho phép lệnh chỉ-đọc (%s)." % (cmd[:200], perm.reason),
                           "I blocked `%s`: only read-only commands are allowed here (%s)." % (cmd[:200], perm.reason),
                           "Your last command `%s` is not read-only. Use only read-only commands "
                           "(ls, git status/diff/log, cat, grep, test runners), or answer from what you have read." % cmd[:120],
                           retry=True)
        m = _FF5_DESTRUCTIVE_RE.search(cmd)
        if (m and _c5("flow5_block_destructive")
                and m.group(0).strip().lower() not in low_user):
            log("[fix5] blocked a destructive command: " + cmd[:200])
            return blocked("Mình đã chặn lệnh `%s`: nó xoá/ghi đè MỌI thay đổi chưa commit trong repo, không "
                           "chỉ thay đổi của session này. Hãy tự chạy nếu bạn thật sự muốn." % cmd[:200],
                           "I blocked `%s`: it would wipe ALL uncommitted work in the repo, not only this "
                           "session's changes. Run it yourself if you really mean it." % cmd[:200],
                           "`%s` would wipe ALL uncommitted work. Undo only the specific files this session "
                           "changed (edit them back), never the whole tree." % cmd[:120], retry=True)
        if win and "&&" in cmd:
            fixed = _ff5_ps_command(cmd)
            if fixed != cmd:
                log("[fix5] rewrote && for PowerShell")
                args[ckey] = fixed
                tc = {**tc, "function": {**fn, "arguments": json.dumps(args, ensure_ascii=False)}}
        new.append(tc)
    calls = new

    # 3. fail closed: a write that still fails the guards is NOT handed to the client
    if _c5("flow5_fail_closed_edits"):
        touched = [p for p in (_call_path(e["args"]) for e in _answered_calls(messages)
                               if e["kind"] == "act") if p]
        probs, new = [], []
        for tc in calls:
            fn = tc.get("function") or {}
            if tool_kind(fn.get("name")) != "act":
                new.append(tc)
                continue
            p = _call_path(T._tool_arguments_dict(fn))
            if p and any(T._path_seen(p, [x]) for x in touched):
                new.append(tc)     # earlier edit this turn: the read copy is stale
                continue
            fixed, pr = _gate_act_calls([tc], messages)
            if pr:
                probs.extend(pr)
            else:
                new.append(fixed[0])
        if probs:
            log("[fix5] unsafe write(s): " + " | ".join(probs))
            body = "\n".join("- " + x for x in probs[:5])
            return blocked("Mình đã chặn thao tác ghi file vì không qua được kiểm tra an toàn (chưa có gì "
                           "được ghi):\n" + body, "I blocked the file write because it failed the safety "
                           "checks (nothing was written):\n" + body,
                           "Your file edit was rejected. Fix exactly these problems and call the edit again:\n"
                           + body, retry=True)
        calls = new
    return (text, calls), None


# --------------------------------------------------------------------------
# install
# --------------------------------------------------------------------------

def _ff5_wrap_change(server):
    if not getattr(T._is_change_request, "_ff5", False):
        orig = T._is_change_request

        def is_change_request(folded, scannable, raw=""):
            if _c5("flow5_respect_no_edit") and (
                    _FF5_NOEDIT_RE.search((folded or "").lower()) or _ff5_noedit_text(raw)):
                return False
            return orig(folded, scannable, raw)

        is_change_request._ff5 = True
        T._is_change_request = is_change_request
    cur = T.change_demanded
    if not getattr(cur, "_ff5", False):
        def change_demanded(messages):
            if _c5("flow5_respect_no_edit") and not _ff5_permissions(messages).edit:
                return False
            return cur(messages)

        change_demanded._ff5 = True
        T.change_demanded = change_demanded
        if getattr(server, "change_demanded", None) is cur:
            server.change_demanded = change_demanded


def install():
    _ff5_prev_install()
    for k, v in _FF5_DEFAULTS.items():
        CONFIG.setdefault(k, v)
    from . import server
    _ff5_wrap_change(server)
    _ff5_install_banner()
    cls = server.GeminiHandler
    cur = cls._generate_with_tool_retry
    if getattr(cur, "_flow5_patched", False):
        return

    @functools.wraps(cur)
    def wrapper5(self, prompt, model_id, think_mode, file_refs, extra,
                 tools_active, allowed_names, tool_schemas, required_tool,
                 messages=None, ticket=None, tool_required=None,
                 client_gone=None):
        def call():
            return cur(self, prompt, model_id, think_mode, file_refs, extra,
                       tools_active, allowed_names, tool_schemas, required_tool,
                       messages=messages, ticket=ticket,
                       tool_required=tool_required, client_gone=client_gone)

        if not (_c5("flow5_enabled") and tools_active and messages and allowed_names):
            return call()
        _ff5_tls.model = (model_id, think_mode, extra, ticket)
        tm, perm = None, None
        try:
            last = messages[-1] if isinstance(messages[-1], dict) else {}
            if not required_tool and last.get("role") in ("user", "tool"):
                tm = ToolMap(allowed_names, tool_schemas, tool_required)
                ctx = _analyze(messages)
                if ctx is not None:
                    perm = getattr(ctx, "perm", None)
                    guard = _ff5_read_guard(ctx, messages)
                    if guard:
                        log("[fix5] read failed before an edit: stopping instead of editing")
                        return guard, None
                    if getattr(ctx, "pattern", None) in _FF5_OWN:
                        step = _ff5_plan(ctx, messages, tm)
                        if step is not None:
                            log(f"[fix5] pattern={ctx.pattern} step={step.name} "
                                f"({'local answer' if step.text is not None else 'drive'})")
                            _ff5_metric("own_" + step.name)
                            final = _ff5_own_result(step, ctx, messages, tm, perm)
                            if final is not None:
                                return final
        except Exception as e:
            log(f"[fix5] planner error, falling back: {e}")
        result = call()
        try:
            retries = max(0, int(_c5("flow5_guard_retries")))
            for attempt in range(retries + 1):
                final, nudge = _ff5_postfilter(
                    result, messages, perm, last=(attempt >= retries), tm=tm)
                if nudge is None:
                    return final
                _ff5_metric("guard_retries")
                log(f"[fix5] guard rejected the output, re-asking (attempt {attempt + 1}): {nudge[:160]}")
                result = cur(self, prompt + "\n\n[SYSTEM NOTE] " + nudge, model_id, think_mode, file_refs,
                             extra, tools_active, allowed_names, tool_schemas, required_tool,
                             messages=messages, ticket=ticket, tool_required=tool_required,
                             client_gone=client_gone)
            return result
        except Exception as e:
            log(f"[fix5] postfilter error: {e}")
            return result

    wrapper5._flow5_patched = True
    cls._generate_with_tool_retry = wrapper5
    log("[fix5] flow_fix5 installed")
# ==== flowfix5:end ====
