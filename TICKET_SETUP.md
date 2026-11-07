# Hướng dẫn cấu hình ticket & routing model

Tài liệu này dành cho người dùng **hoặc một AI khác** cần cấu hình, kiểm tra
hoặc sửa routing của `gemini-web2api` mà không phải đọc toàn bộ source. Các
tên config/key giữ nguyên tiếng Anh.

---

## 0. Checklist nhanh cho AI

Khi được yêu cầu "cấu hình ticket", "kiểm tra routing" hoặc "sửa model":

1. **Không in, không gửi cookie.** `gemini-auth.json` là credential Google thật.
2. **Không đưa ticket thật vào `config.example.json`.** File này được track và
   `Dockerfile` copy nó thành `config.json`; ticket sẽ bị bake vào image.
3. Đọc `config.json` và `gemini-auth.json`, nhưng chỉ in **tên key/route**, không
   in value của cookie hoặc ticket.
4. Đảm bảo `cookie_file` trỏ tới `gemini-auth.json` (hoặc file auth JSON tương
   ứng). **Tuyệt đối không** trỏ `cookie_file` vào `gemini_web2api.py`,
   `config.json` hay bất kỳ source file nào: server ghi cookie/ticket trở lại
   file đó và sẽ làm hỏng source/config thật.
5. Đảm bảo `model_tickets` có đủ 3 key cho Flash-Lite:
   `lite` → `(6,1)`, `lite-medium` → `(6,4)`, `lite-thinking` → `(6,2)`.
6. Nếu thiếu ticket: **hướng dẫn user capture từ browser**, không tự bịa giá trị.
7. Chạy `python tools/check_tickets.py` để verify.
8. Không gọi upstream thật bằng cookie của user trừ khi được yêu cầu rõ ràng.

---

## 1. Ticket là gì, có phải secret không?

Ticket là toàn bộ giá trị header:

```text
X-Goog-Ext-525001261-Jspb
```

Nó **không phải** cookie, mật khẩu hay OAuth token. Nhưng:

- Là **session-bound routing token**: chứa UUID, timezone, build label, số quota.
- Có thể dùng để route request nếu có một cookie hợp lệ.
- **Hết hạn** theo session.
- Được xem là "session material": không commit, không đưa vào file tracked.

Cookie trong `gemini-auth.json` mới là credential thật. Tuyệt đối không in,
không commit, không chia sẻ file này.

---

## 2. File nào chứa gì

| File | Tracked? | Nội dung |
|---|---|---|
| `config.json` | Không (gitignored) | Config server + có thể chứa `model_tickets` |
| `gemini-auth.json` | Không (gitignored) | Cookie, `sapisid`, `xsrf_token`, `auth_user`, `model_tickets` do extension ghi |
| `config.example.json` | Có | Template công khai; **không chứa ticket thật** |
| `gemini-cookie-sync-extension/` | Có | Extension Chrome để capture cookie + ticket |

Khi đổi máy, copy **cả `gemini-auth.json`** (không chỉ `config.json`). Nếu mất
file này, extension phải capture lại từ đầu.

---

## 3. Bảng routing hiện tại

Routing thực tế do ticket quyết định, cụ thể là cặp `(family, variant)` ở
index `14` và `15` trong ticket. Tên model chỉ là label.

| Model id | mode | think | variant | ticket key | route mong đợi |
|---|---:|---:|---:|---|---|
| `gemini-3.7-flash` | 1 | 4 | 1 | `flash` | `(1,1)` |
| `gemini-3.6-flash` | 1 | 4 | 1 | `flash` | `(1,1)` |
| `gemini-3.5-flash` | 1 | 4 | 1 | `flash` | `(1,1)` |
| `gemini-3.8-flash` | 1 | 4 | 1 | `flash` | `(1,1)` |
| `gemini-3.8-flash-low` | 1 | 4 | 1 | `flash` | `(1,1)` |
| `gemini-3.8-flash-medium` | 1 | 4 | 4 | `flash-medium` | `(1,4)` |
| `gemini-3.8-flash-high` | 1 | 4 | 2 | `flash-thinking` | `(1,2)` |
| `gemini-3.5-flash-thinking` | 2 | 1 | 2 | `flash-thinking` | `(1,2)` |
| `gemini-3.1-pro` | 3 | 4 | 1 | `pro` | `(3,1)` |
| `gemini-3.1-pro-low` | 3 | 4 | 1 | `pro` | `(3,1)` |
| `gemini-3.1-pro-medium` | 3 | 4 | 4 | `pro-medium` | `(3,4)` |
| `gemini-3.1-pro-high` | 3 | 4 | 2 | `pro-thinking` | `(3,2)` |
| `gemini-4-argon` | 8 | 4 | 1 | `argon` | `(8,1)` |
| `gemini-4-argon-low` | 8 | 4 | 1 | `argon` | `(8,1)` |
| `gemini-4-argon-medium` | 8 | 4 | 4 | `argon-medium` | `(8,4)` |
| `gemini-4-argon-high` | 8 | 4 | 2 | `argon-high` | `(8,2)` |
| `gemini-flash-lite` | 6 | 4 | 1 | `lite` | `(6,1)` |
| `gemini-3.5-flash-lite` | 6 | 4 | 1 | `lite` | `(6,1)` |
| `gemini-3.5-flash-lite-low` | 6 | 4 | 1 | `lite` | `(6,1)` |
| `gemini-3.5-flash-lite-medium` | 6 | 4 | 4 | `lite-medium` | `(6,4)` |
| `gemini-3.5-flash-lite-high` | 6 | 4 | 2 | `lite-thinking` | `(6,2)` |
| `gemini-3.5-flash-thinking-lite` | 5 | 1 | 2 | `lite-thinking` | `(6,2)` |

Ghi chú:

- Index `4` của ticket có thể là `null`; điều đó **hợp lệ**. Routing đọc
  `[14]`/`[15]`. **Index 4 không bao giờ tham gia mapping** (ví dụ
  `95a4c0abcba24364` chỉ là session metadata).
- `gemini-3.5-flash-thinking-lite` là alias legacy: body gửi mode 5/variant 2,
  nhưng ticket `lite-thinking` vẫn route `(6,2)`.
- Extension cũng map `(5,1)` và `(5,2)` về `lite-thinking` để tương thích
  capture cũ. Nếu ticket embed `(5,2)`, startup sẽ cảnh báo vì mong đợi là
  `(6,2)`.
- `gemini-3.1-pro-enhanced` và `gemini-auto` **cố ý không có ticket**: chúng
  dùng body fields (`extra[80]`) hoặc để upstream tự chọn. Đừng thêm ticket
  cho hai model này nếu không có lý do rõ ràng. Hai model này không tham gia
  fallback và luôn trả `None` khi không có ticket.

### 3.1 Fallback cho account free

Account free thường chỉ có một phần ticket. Khi key chính xác không có,
`ticket_for()` tự hạ cấp theo chuỗi (giữ nguyên thứ tự):

```text
argon-high:   argon-high -> argon-medium -> argon
              -> pro-thinking -> pro-medium -> pro
              -> flash-thinking -> flash-medium -> flash
              -> lite-thinking -> lite-medium -> lite
argon-medium: argon-medium -> argon -> pro-medium -> pro
              -> flash-medium -> flash -> lite-medium -> lite
argon:        argon -> pro -> flash -> lite
pro-thinking: pro-thinking -> pro-medium -> pro
              -> flash-thinking -> flash-medium -> flash
              -> lite-thinking -> lite-medium -> lite
pro-medium:   pro-medium -> pro -> flash-medium -> flash
              -> lite-medium -> lite
pro:          pro -> flash -> lite
flash-thinking: flash-thinking -> flash-medium -> flash
                -> lite-thinking -> lite-medium -> lite
flash-medium: flash-medium -> flash -> lite-medium -> lite
flash:        flash -> lite
lite-thinking: lite-thinking -> lite-medium -> lite
lite-medium:  lite-medium -> lite
lite:         lite
```

Mỗi lần fallback được dùng, log ghi **một lần** cho mỗi cặp
`(model, requested key, used key)`:

```text
[model] ticket fallback: gemini-4-argon-high requested=high (argon-high) -> used=medium (pro-medium)
```

Nếu **không** key nào trong chuỗi tồn tại, hàm trả `None` như cũ; upstream sẽ
dùng body fields hoặc model mặc định của account.

---

## 4. `reasoning_effort` (OpenCode variants)

Các model gốc sau phản ứng với `reasoning_effort` / `reasoning.effort`:

- `gemini-3.8-flash`
- `gemini-3.1-pro`
- `gemini-4-argon`
- `gemini-3.5-flash-lite`
- `gemini-flash-lite`

Mapping:

| Effort | Hậu tố được chọn |
|---|---|
| `minimal`, `low` | `-low` |
| `medium` | `-medium` |
| `high` | `-high` |

Ví dụ: `gemini-3.8-flash` + `high` → `gemini-3.8-flash-high` → ticket
`flash-thinking` → `(1,2)`; `gemini-3.1-pro` + `medium` →
`gemini-3.1-pro-medium` → ticket `pro-medium` → `(3,4)`; `gemini-4-argon` +
`low` → `gemini-4-argon-low` → ticket `argon` → `(8,1)`. Hai model lite vẫn
map về `gemini-3.5-flash-lite-low/medium/high`.

Quy tắc:

- Tên model có hậu tố `-low`/`-medium`/`-high` **luôn thắng** `reasoning_effort`.
- Hậu tố `@think=` vẫn được giữ.
- Các model khác (ví dụ `gemini-3.6-flash`) bỏ qua `reasoning_effort`.
- Mỗi request log dòng `[model] requested=... effort=... -> ... (family=...
  variant=... ticket=...)`; fallback ticket có thêm dòng `[model] ticket
  fallback: ...`.

Ví dụ config OpenCode V2:

```jsonc
"gemini-3.5-flash-lite": {
  "modelID": "gemini-3.5-flash-lite",
  "variants": [
    { "id": "low",    "body": { "reasoning_effort": "low" } },
    { "id": "medium", "body": { "reasoning_effort": "medium" } },
    { "id": "high",   "body": { "reasoning_effort": "high" } }
  ]
}
```

---

## 5. Cài đặt nhanh trên máy mới

### Cách A — có `config.json` (khuyến nghị)

```powershell
Copy-Item config.example.json config.json
```

Sửa `config.json`:

```json
{
  "cookie_file": "gemini-auth.json"
}
```

Sau đó chạy:

```powershell
python gemini_web2api.py
```

### Cách B — không cần `config.json`

```powershell
python gemini_web2api.py --cookie-file gemini-auth.json
```

Lưu ý: `--cookie-file` **không được lưu** vào config. Lần sau chạy lại phải
truyền lại, nếu không server sẽ chạy anonymous. `--cookie-file` chỉ nhận file
JSON auth (`gemini-auth.json`), **không bao giờ** nhận file source như
`gemini_web2api.py` — server sẽ ghi đè và làm hỏng file.

### `--cookie-file` có tự tải ticket không?

**Không.** Nó chỉ trỏ tới file JSON. Ticket đến từ:

1. Extension capture header `StreamGenerate` rồi push `POST /v1/cookies`, hoặc
2. File `gemini-auth.json` bạn copy sẵn.

Nếu file không có `model_tickets` và extension không chạy → không có ticket;
upstream sẽ dùng model mặc định của account.

### Lưu ý thứ tự load ticket

Server resolve ticket trong `_resolve_request_model()` **trước** khi
`load_cookie()` được gọi trong `_build_payload()`. Nếu `config.json` **không**
có block `model_tickets`, request chat đầu tiên sau restart có thể đi không
ticket; từ request thứ 2 trở đi mới có.

Cách tránh:

- Giữ block `model_tickets` trong `config.json`, hoặc
- Để extension push trước khi chat, hoặc
- Patch thêm `load_cookie()` ở đầu `_resolve_request_model` / trước
  `serve_forever`.

### Đổi account Google (free ↔ paid)

Ticket gắn với session/account. Sau khi đổi account:

1. **Xóa ticket cũ** khỏi `config.json` và `gemini-auth.json` (hoặc xóa hẳn
   `gemini-auth.json`).
2. Capture lại ticket trên account mới.
3. Chạy `python tools/check_tickets.py --all` để xem key nào có route đúng.

Không xóa ticket cũ thì request có thể vẫn route theo model mà account mới
không dùng được; log sẽ liên tục báo `Routing mismatch` hoặc fallback xuống
level thấp hơn.

---

## 5.4 Capture tự động bằng extension

Extension bắt header `X-Goog-Ext-525001261-Jspb` theo hai đường: `webRequest`
(chính) và hook `fetch`/`XMLHttpRequest` trong page (dự phòng). Làm đúng các
bước sau:

1. `chrome://extensions` → **Reload** extension (bắt buộc sau khi cập nhật
   `ticket-map.js` / thêm content script).
2. Mở `https://gemini.google.com` trong **đúng browser/profile** đang cài
   extension, rồi refresh tab (content script chỉ inject khi trang load).
3. Chọn đúng model + level trong UI và gửi một tin nhắn bình thường.
4. Mở popup extension → **Inspect session**. Kiểm tra:

```text
model_tickets: N captured (flash, flash-medium, flash-thinking, ...)
Ticket scan: X request(s), Y with Jspb header, Z mapped
Last scan: <thời gian> | <reason> | <webRequest|page-hook> | <số ký tự>
```

Cách đọc dòng chẩn đoán:

| Dòng | Ý nghĩa |
|---|---|
| `X = 0` | Không thấy request Gemini nào: sai browser/profile, tab chưa refresh, hoặc extension chưa reload |
| `Y = 0` với `reason=no-header` | Có request nhưng header bị che/không có; thường do request phát từ service worker của page hoặc extension cũ |
| `reason=no-route(len=...)` | Header có nhưng thiếu `(family, variant)` ở index 14/15 (header dạng ngắn) |
| `reason=unknown-route(x,y)` | Route lạ, cần bổ sung vào `ticket-map.js` |
| `Z > 0` và `model_tickets` có key | Capture thành công; extension sẽ push `/v1/cookies` |

5. Nếu capture thành công, extension tự push ticket lên server. Có thể kiểm
   tra lại bằng `python tools/check_tickets.py --all`.

Nếu popup vẫn báo `none yet` dù `Ticket scan` có request và header, gửi lại
dòng `Last scan` (che cookie/ticket value) để biết đang tắc ở đâu.

---

## 6. Capture ticket thủ công

1. Mở [https://gemini.google.com](https://gemini.google.com) và đăng nhập.
2. Chọn đúng model/level trong UI (low / medium / high).
3. Mở DevTools → tab **Network**.
4. Gửi một tin nhắn bất kỳ.
5. Tìm request `StreamGenerate`.
6. Mở request → **Headers** → **Request Headers**.
7. Copy toàn bộ giá trị của `X-Goog-Ext-525001261-Jspb`.
8. Dán vào `config.json` dưới đúng key:

```json
{
  "model_tickets": {
    "flash": "[1,null,...]",
    "flash-medium": "[1,null,...]",
    "flash-thinking": "[1,null,...]",
    "lite": "[1,null,...]",
    "lite-medium": "[1,null,...]",
    "lite-thinking": "[1,null,...]",
    "pro": "[1,null,...]",
    "pro-medium": "[1,null,...]",
    "pro-thinking": "[1,null,...]",
    "argon": "[1,null,...]",
    "argon-medium": "[1,null,...]",
    "argon-high": "[1,null,...]"
  }
}
```

Chỉ cần các key account của bạn thực sự có; thiếu key sẽ tự fallback theo
chuỗi ở mục 3.1.

Giá trị là một JSON array, nhưng trong `config.json` nó là **string**. Giữ
nguyên dấu ngoặc kép đã escape.

Kiểm tra nhanh route mà không in value:

```powershell
python -c "import json; t=json.loads(input()); print('family=',t[14],'variant=',t[15])"
```

Dán ticket vào prompt, nhấn Enter.

---

## 7. Verify

### 7.1 Kiểm tra file ticket

```powershell
python tools/check_tickets.py
```

Kỳ vọng:

```text
config: config.json found
auth:   gemini-auth.json found
cookie_file: 'gemini-auth.json' exists
cookie: present
model_tickets:
  lite           OK      route=(6,1)    expected=(6,1)
  lite-medium    OK      route=(6,4)    expected=(6,4)
  lite-thinking  OK      route=(6,2)    expected=(6,2)
RESULT: required lite tickets are ready.
```

Script chỉ trả exit code `1` khi 3 key Flash-Lite thiếu/sai. Dùng `--all` để
xem thêm `flash`, `flash-medium`, `flash-thinking`, `pro`, `pro-medium`,
`pro-thinking`, `argon`, `argon-medium`, `argon-high`; thiếu các key này
không bị coi là lỗi.

### 7.2 Kiểm tra server

Khởi động server và xem log:

```powershell
python gemini_web2api.py
```

- Không có `WARNING: model ticket ...` → ticket đúng route.
- Có `Routing mismatch: requested ... but upstream served ...` → ticket sai
  hoặc hết hạn.
- Có `Routing hint: ... fell back to Flash-Lite low (6,1)` → account anonymous
  hoặc hết quota; medium/high không dùng được.

### 7.3 Kiểm tra model list

```powershell
curl.exe -s http://127.0.0.1:8081/v1/models
```

Phải thấy:

```text
gemini-3.8-flash
gemini-3.8-flash-low
gemini-3.8-flash-medium
gemini-3.8-flash-high
gemini-3.1-pro-low
gemini-3.1-pro-medium
gemini-3.1-pro-high
gemini-4-argon
gemini-4-argon-low
gemini-4-argon-medium
gemini-4-argon-high
gemini-3.5-flash-lite
gemini-3.5-flash-lite-low
gemini-3.5-flash-lite-medium
gemini-3.5-flash-lite-high
```

### 7.4 Kiểm tra routing thật

Gửi 1 request cho mỗi level và xem log:

```text
[model] reasoning_effort=low -> gemini-3.8-flash-low
[model] reasoning_effort=medium -> gemini-3.8-flash-medium
[model] reasoning_effort=high -> gemini-3.8-flash-high
[model] requested=gemini-3.8-flash effort=high -> gemini-3.8-flash-high (family=1 variant=2 ticket=flash-thinking)
[model] ticket fallback: gemini-3.8-flash-high requested=high (flash-thinking) -> used=medium (lite-medium)
```

Dòng `requested=...` xuất hiện cho **mọi** request. Dòng `ticket fallback`
chỉ xuất hiện khi key chính xác thiếu và một key khác trong chuỗi được dùng.

---

## 8. Troubleshooting

| Triệu chứng | Nguyên nhân | Cách sửa |
|---|---|---|
| `Routing mismatch ...` | Ticket thiếu, hết hạn hoặc sai `(family,variant)` | Capture lại ticket; kiểm tra `[14]/[15]` |
| `Routing hint: ... fell back to <family> low` | Anonymous, hết quota, hoặc account không có ticket medium/high | Dùng account có quota; low là mặc định an toàn |
| `[model] ticket fallback: ... requested=... -> used=...` | Key chính xác thiếu; server đang hạ cấp trong chuỗi fallback | Bình thường với account free; capture key cao hơn nếu muốn đúng level |
| `Unknown model ... falling back` | Tên model không có trong `MODELS` | Dùng tên đã hỗ trợ hoặc thêm mapping |
| `401` / `403` | Cookie hết hạn hoặc bị Google từ chối | Đăng nhập lại, để extension sync cookie mới |
| Request đầu sau restart thiếu ticket | Ticket chỉ có trong `gemini-auth.json`, resolve trước `load_cookie` | Giữ `model_tickets` trong `config.json` hoặc patch load sớm |
| `model ticket ... could not be parsed` | Value không phải JSON array hợp lệ | Copy lại toàn bộ header, kiểm tra escape |
| `model ticket ... embeds (family=..., variant=...)` | Ticket sai route | Capture lại đúng model/level |

---

## 9. Thêm model / family mới (khi Google đổi)

Model mới **không tự xuất hiện**. Routing đang hardcode ở các nơi:

1. `MODELS` trong `gemini_web2api.py`
2. `EXPECTED_TICKET_ROUTES` + `_TICKET_FALLBACKS` trong `gemini_web2api.py`
3. `TICKET_ROUTE_KEYS` trong `gemini-cookie-sync-extension/ticket-map.js`
4. `MODELS` + `TICKET_FALLBACKS` trong `cloudflare/worker.js` (nếu dùng worker)
5. `EXPECTED` trong `tools/check_tickets.py`

Các bước:

1. Capture ticket mới từ browser.
2. Đọc `[14]` và `[15]` để biết `(family, variant)`.
3. Thêm entry vào `MODELS` với `mode`, `think`, `variant`, `ticket`.
4. Thêm key vào `EXPECTED_TICKET_ROUTES`.
5. Thêm cặp `(family,variant) → key` vào `ticket-map.js`.
6. Nếu dùng Cloudflare worker: cập nhật `worker.js` và env `MODEL_TICKETS`.
7. Cập nhật README và chạy test:
   - `pytest`
   - `node tests/extension_ticket_test.js`
   - `node tests/worker_routing_test.js`

Lưu ý: tên `3.1`, `3.5`, `3.6` chỉ là label. Không có field nào chọn đúng
point version trong một family; upstream chọn bản mặc định của family đó.

---

## 10. Vận hành an toàn (rate limit)

Đây là endpoint web không chính thức, không có SLA. Request dồn dập có thể bị:

- `429` / `RESOURCE_EXHAUSTED`
- `403` tạm thời
- CAPTCHA / login challenge
- Trường hợp nặng: account bị hạn chế

Hiện tại code có:

- `retry_attempts` (mặc định 3) + `retry_delay_sec` (mặc định 2s) khi lỗi.
- `tool_retry_attempts` (mặc định 1) và `tool_retry_on_miss`.
- Flow có thể gọi upstream nhiều bước; map-reduce review có thể gọi nhiều lần.
- Throttle toàn cục (opt-in):
  - `upstream_min_interval_sec` (mặc định code `0.0`): khoảng cách tối thiểu
    giữa hai lần *bắt đầu* request generation upstream; `<= 0` là tắt.
  - `upstream_max_concurrency` (mặc định code `0`): số request generation
    đồng thời tối đa; `<= 0` là không giới hạn.
  - Cả hai được đọc tại thời điểm gọi, nên đổi config có hiệu lực ngay.
    Retry và các bước follow-up của tool/flow cũng đi qua cùng cổng này.

Đề xuất nếu cần chạy tần suất cao:

- Giữ 1 account cho 1 proxy; không share account.
- Giảm `tool_retry_attempts`, `flow_max_retries` nếu không cần.
- Theo dõi log `429`/`403`.
- Bật throttle trong `config.json` khi dùng một account:
  - Account free: `upstream_min_interval_sec` 1–3, `upstream_max_concurrency` 1.
  - Account Pro: `upstream_min_interval_sec` 0.5–1, `upstream_max_concurrency` 1.
  - `config.example.json` dùng sẵn `1.0` / `1`; code mặc định vẫn là `0.0` / `0`
    để không đổi hành vi cũ.
- Tôn trọng `Retry-After` và backoff có jitter khi gặp `429`/`503` là hướng
  cải tiến tiếp theo, hiện chưa triển khai.

Volume lớn / production nên dùng API chính thức của Google thay vì web endpoint.

---

## 11. Prompt mẫu cho AI khác

Copy prompt này và giao cho AI đang mở repo:

```text
Bạn đang làm việc trong repo gemini-web2api. Hãy đọc TICKET_SETUP.md trước.

Nhiệm vụ: kiểm tra ticket cho gemini-3.5-flash-lite low/medium/high.

Quy tắc:
- Không in cookie, sapisid, xsrf_token hoặc value của ticket.
- Không đưa ticket thật vào config.example.json.
- Đọc config.json và gemini-auth.json; chỉ báo tên key và route.
- Đảm bảo cookie_file trỏ tới gemini-auth.json.
- Đảm bảo model_tickets có:
  lite (6,1), lite-medium (6,4), lite-thinking (6,2).
- Chạy: python tools/check_tickets.py
- Nếu thiếu/sai: chỉ hướng dẫn user capture lại; không tự bịa ticket.
- Không gọi upstream thật bằng cookie của user.

Báo cáo:
1. Key nào OK / MISSING / WRONG.
2. Route thực tế của từng ticket.
3. Bước tiếp theo cần làm.
```
