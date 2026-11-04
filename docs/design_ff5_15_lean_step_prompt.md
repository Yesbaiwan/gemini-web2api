# Thiết kế (Finding #15): Context "vừa đủ" cho model yếu — `build_step_prompt` / `select_evidence_for_step`

## Trạng thái
- Proposal, **chưa implement**. Phạm vi: `gemini_web2api/flow_patch.py`.
- Liên quan: Feedback #4 (mục tiêu ngắn / quyền / bằng chứng liên quan / việc cần làm / schema tool được phép), Finding #13 (nhãn static), Finding #14 (banner dùng `_analyze`).

## 1. Vấn đề hiện tại (đối chiếu code thực tế)

Hàm `_ff5_context_suffix(ctx, step, perm, messages)` (dòng ~4230) tạo block `[Context] goal=... | intent=... | edit=... | commands=... | next=...` và được nối vào directive trong `_ff5_after_plan`:

```python
if _c5("flow6_context") and not getattr(step, "drive", False):
    suffix = _ff5_context_suffix(ctx, step, perm, messages)
    if suffix and suffix not in (step.instruction or ""):
        step.instruction = (step.instruction or "") + "\n" + suffix
```

Nhưng wrapper vẫn gọi upstream với **full prompt** ở nhánh GUIDE (dòng ~1316/~1323):

```python
return call(prompt + "\n\n" + step.directive())            # answer step
text, calls = call(prompt + "\n\n" + step.directive() + nudge, True)   # guide loop
```

`prompt` là toàn bộ lịch sử chat + tool result cũ đã được build phía server. Kết quả: model yếu vẫn thấy mọi thứ; suffix chỉ là "thêm context", chưa phải "context vừa đủ". Đây chính xác là giới hạn Finding #15 nêu.

## 2. Mục tiêu / phi mục tiêu

Mục tiêu:
- Với GUIDE step, thay full prompt bằng **step prompt tối giản** dựng từ: goal ngắn + permissions + evidence đã chọn + instruction của step + schema của các tool được phép.
- Evidence chọn theo stage (`select_evidence_for_step`), có budget ký tự, fail-open.
- Bật/tắt qua config flag, default **tắt** cho tới khi đo được trên model thật.

Phi mục tiêu (v1):
- Không đổi nhánh `drive` (server tự emit calls, không gọi model).
- Không đụng planner/banner/validator (đã thống nhất nguồn `_analyze` từ Finding #14).
- Không cắt history cho các pattern ngoài `_FF5_SPEC` hoặc khi ctx/pattern thiếu.

## 3. API mới

```python
def select_evidence_for_step(stage, messages, *, budget=2400):
    """Trả về list[str]: các mẩu evidence đã cắt gọn, ưu tiên theo độ mới."""

def build_step_prompt(ctx, step, messages, tm):
    """Chuẩn hoá inputs rồi gọi _build_step_prompt_core; MỌI exception -> None."""

def _build_step_prompt_core(ctx, step, messages, tm):
    """Dựng prompt; trả None nếu step nằm ngoài phạm vi lean path."""
```

Ký hiệu:
- `ctx`: kết quả `_analyze(messages)` (đã có ở caller dòng ~1286).
- `tm`: `ToolMap` đang sống trong closure của wrapper — nguồn duy nhất của `allowed_tool_defs` (không truyền raw `tool_schemas`, tránh lệch danh sách allowlist giữa planner và prompt).
- Trả `None` = "không áp dụng lean path" → caller fallback nguyên trạng.

## 4. `select_evidence_for_step` — mapping theo feedback #15

Nguồn: `_answered_calls(messages)` (đã filter theo lượt user cuối, mỗi entry `{name, kind, args, result}`), trạng thái qua `_ff5_status(e)`, đường dẫn qua `_call_path(args)` — tất cả đã tồn tại, tái sử dụng, không viết lại parser.

| Stage (từ `_ff5_stage`) | Bằng chứng lấy | Cắt tỉa |
|---|---|---|
| `discover` | lệnh search/exec gần nhất (query/command + số hit) | bỏ result dài, giữ 1 dòng/call, tối đa 4 call |
| `read` | path cần đọc tiếp (từ goal/instruction + hits chưa đọc), failed_reads (hint "đừng đọc lại") | không đưa nội dung file cũ vào prompt |
| `edit` (act/merge) | path target, failed-edit hint (error detail gần nhất qua `_ff5_status_detail`), oldString mismatch mới nhất | chỉ target + hint, không đưa nguyên văn nội dung file |
| `diff` | output `git diff` exec thành công **mới nhất** | cap ~900 ký tự đầu + `…[truncated]` |
| `verify` (tests/verify) | test output exec **gần nhất** (fail trước, success sau nếu cùng batch) | cap ~1200 ký tự, ưu tiên khối FAIL/error |
| `answer` | danh sách path đã đọc thành công (`good` như suffix hiện tại) + findings đã verify (exec success sau act cuối) | 6 path, 3 finding, mỗi finding 1 dòng ≤140 ký tự |
| `analyze` (mặc định) | như `answer` nhưng thêm 2 search hit gần nhất | như trên |

Quy tắc chung:
- Iterate `_answered_calls` ngược (mới nhất trước), dừng khi hết `budget` (default 2400 ký tự tổng evidence).
- Mỗi mẩu là 1 dòng prefix theo loại: `read:`, `failed_read:`, `search:`, `edit_failed:`, `diff:`, `test:`, `verified:`.
- Không bao giờ đưa nguyên tool result lớn vào prompt — luôn cắt theo cap từng loại rồi cap tổng.

## 5. Định dạng prompt đầu ra

```
[FLOW {pattern} step {i}/{n} -- {NAME}]
{step.instruction}                      (instruction gốc, KHÔNG còn suffix [Context])

Goal: {ctx.last ≤180 ký tự}
Permissions: edit=allowed|forbidden | commands=any|readonly|none | scope={perm.scope or "(unrestricted)"}
Pipeline: discover -> read -> edit -> diff -> verify -> answer   (từ _ff5_allowed_steps)

Evidence:
read: gemini_web2api/flow_patch.py
failed_read: tests/fixtures.bin
test: 3 failed, 2 ok — AssertionError at line 42 …[truncated]

Allowed tools this turn: read_file, grep_search, run_command. Output ONLY tool_call block(s), no prose.
{schema blocks — JSON name+parameters, chỉ tool thuộc step.tool_names ∩ tm}
Emit exactly: (nếu step.calls — giữ y như Step.directive)
```

Tái sử dụng tối đa `Step.directive()` cho phần head/tool-names/`Emit exactly`; `build_step_prompt` chỉ **thay phần history** bằng Goal/Permissions/Evidence, không copy-paste logic directive.

## 6. Wire vào wrapper (nhánh GUIDE)

Thay cục bộ 2 chỗ (~dòng 1316, 1323):

```python
lean = (_c5("flow6_lean_prompt") and ctx is not None
        and getattr(ctx, "pattern", None) in _FF5_SPEC)
step_prompt = build_step_prompt(ctx, step, messages, tm) if lean else None

if step.name == "answer":
    ...
    return call((step_prompt or prompt) + "\n\n" + step.directive())
...
text, calls = call((step_prompt or prompt) + "\n\n" + step.directive() + nudge, True)
```

Và trong `_ff5_after_plan`, chặn double-inject khi lean bật:

```python
if _c5("flow6_context") and not getattr(step, "drive", False) \
        and not _c5("flow6_lean_prompt"):
    ...suffix injection như hiện tại...
```

Retry loop giữ nguyên `nudge` mechanism (nudge append sau `step.directive()`, hoạt động y hệt với step_prompt).

## 7. Config & feature flag

Thêm vào `_FF5_DEFAULTS`:

```python
# Replace the full chat history with a minimal per-step prompt for guided
# steps (finding 15). Off by default until measured against flash-lite.
"flow6_lean_prompt": False,
```

Rollout: off → bật trên branch test → đo metrics (`_FF5_METRICS` đã có: đếm tokens-in xấp xỉ bằng `len(prompt)`, tỉ lệ step hợp lệ) → cân nhắc default-on ở release sau.

## 8. Fallback & bất biến an toàn (fail-open)

- `build_step_prompt` bọc `try/except Exception: return None` — mọi lỗi → dùng full prompt như cũ.
- Trả `None` khi: flag tắt; `ctx` None; pattern ngoài `_FF5_SPEC`; `step.drive` True; step có `local_text` (không tới được đây); `tm` None.
- **Không bao giờ** bỏ `Emit exactly` của deterministic calls, danh sách allowed tools, hay permission line — ba thứ này là lý do lean path tồn tại.
- Map-reduce path (answer + GENERIC_REVIEW/FILE_REVIEW) không đổi: nó tự dựng messages riêng.
- Không đụng `client_gone`, gate `_gate_act_calls`, evidence gate, diff step — chúng chạy trước/sau và độc lập với cách dựng prompt.

## 9. Test dự kiến (thêm vào `tests/test_flow_fix6.py` hoặc file mới `test_lean_prompt.py`)

1. Unit `select_evidence_for_step`: messages giả có read success/fail, exec test output dài → đúng prefix, đúng cap, đúng thứ tự ưu tiên, tôn trọng `budget`.
2. Unit `build_step_prompt`: output chứa `Goal:`, `Permissions:`, `Allowed tools`, `Emit exactly` khi có calls; **không chứa** text của message history cũ.
3. Fallback: flag off → `None`; ctx None → `None`; pattern lạ → `None`; internal raise (monkeypatch `_answered_calls`) → `None`.
4. Integration (stub `call`): GUIDE step với flag on nhận step_prompt (ngắn hơn prompt), flag off nhận `prompt + directive`; retry nudge vẫn append đúng.
5. Regression: `_ff5_after_plan` không inject `[Context]` suffix khi `flow6_lean_prompt` on; vẫn inject khi off (bảo toàn hành vi cũ).
6. Chạy toàn suite hiện có (355 tests) — phải pass nguyên.

## 10. Rủi ro đã lường

| Rủi ro | Giảm thiểu |
|---|---|
| Model mất thông tin ngoài evidence table → trả lời kém hơn | flag default off; đo A/B qua `_FF5_METRICS`; evidence cap tăng dần nếu cần |
| Sticky context (goal từ lượt trước) bị mất | Goal lấy từ `ctx.last` — chính là kết quả sticky của `_analyze` (Finding #14), nên khớp planner |
| Tool schema sai/thiếu so với allowlist | schema lấy từ `tm` — cùng object planner dùng để sinh `step.tool_names` |
| Prompt quá ngắn với step phức tạp (PLAN/GENERIC_REVIEW) | v1 có thể whitelist stage áp dụng (read/edit/diff/verify/answer) trước, `analyze`/PLAN bật sau |
| Double-inject `[Context]` | mục 6: điều kiện `_c5("flow6_context") and not _c5("flow6_lean_prompt")` |

## 11. Estimative

- `select_evidence_for_step` + `build_step_prompt` + `_build_step_prompt_core`: ~90–120 dòng.
- Wire wrapper + guard suffix + flag: ~15 dòng.
- Tests: ~80–100 dòng.
- Không xoá `_ff5_context_suffix` (vẫn dùng khi flag off).
