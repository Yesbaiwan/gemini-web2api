# Báo cáo kiểm tra và phân loại 34 vị trí dùng `globals()` (Bước B1)

| STT | Dòng | Enclosing Scope | Dạng | Target Name | Tồn tại ở Module Level? | Nhận định & Hướng xử lý |
|---|---|---|---|---|---|---|
| 1 | L3128 | `def task_pattern_banner` | `globals().get` | `_TASK_BANNER_HOOK` | `True` | Gán lúc `install()`, có thể chưa tồn tại lúc gọi: **GIỮ NGUYÊN** |
| 2 | L5940 | `def _clean_file_text` | `globals().get` | `_FF4_TRUNC_RE` | `True` | Hằng số regex / helper đã định nghĩa cố định ở module level: **CÓ THỂ THAY BẰNG THAM CHIẾU TRỰC TIẾP** (hoặc giữ an toàn) |
| 3 | L6023 | `def _base_analyze` | `globals().get` | `_strip_pasted_content` | `True` | Hằng số regex / helper đã định nghĩa cố định ở module level: **CÓ THỂ THAY BẰNG THAM CHIẾU TRỰC TIẾP** (hoặc giữ an toàn) |
| 4 | L6571 | `def wrapper` | `in globals()` | `_ff17_attach_todowrite` | `True` | Hàm flowfix đã định nghĩa cố định ở module level: **ĐÃ TỒN TẠI** |
| 5 | L6579 | `def wrapper` | `in globals()` | `_ff16_is_todo_tool` | `True` | Hàm flowfix đã định nghĩa cố định ở module level: **ĐÃ TỒN TẠI** |
| 6 | L6603 | `def wrapper` | `in globals()` | `_ff16_is_todo_tool` | `True` | Hàm flowfix đã định nghĩa cố định ở module level: **ĐÃ TỒN TẠI** |
| 7 | L6631 | `def wrapper` | `in globals()` | `_ff16_is_todo_tool` | `True` | Hàm flowfix đã định nghĩa cố định ở module level: **ĐÃ TỒN TẠI** |
| 8 | L6636 | `def wrapper` | `in globals()` | `_ff17_attach_todowrite` | `True` | Hàm flowfix đã định nghĩa cố định ở module level: **ĐÃ TỒN TẠI** |
| 9 | L9217 | `def _ff5_fold` | `globals().get` | `_strip_pasted_content` | `True` | Hằng số regex / helper đã định nghĩa cố định ở module level: **CÓ THỂ THAY BẰNG THAM CHIẾU TRỰC TIẾP** (hoặc giữ an toàn) |
| 10 | L9521 | `def _ff5_status` | `globals().get` | `_FF4_TRUNC_RE` | `True` | Hằng số regex / helper đã định nghĩa cố định ở module level: **CÓ THỂ THAY BẰNG THAM CHIẾU TRỰC TIẾP** (hoặc giữ an toàn) |
| 11 | L9649 | `def _ff5_evidence_gate_s1` | `in globals()` | `_ff4_offset_key` | `True` | Trạng thái tồn tại: True |
| 12 | L10206 | `def _ff5_trunc_re` | `globals().get` | `_FF4_TRUNC_RE` | `True` | Hằng số regex / helper đã định nghĩa cố định ở module level: **CÓ THỂ THAY BẰNG THAM CHIẾU TRỰC TIẾP** (hoặc giữ an toàn) |
| 13 | L10770 | `def _analyze_s3` | `globals().get` | `_rf_prev_is_change_request` | `False` | Alias prev: **GIỮ NGUYÊN** nếu chưa chứng minh luôn có |
| 14 | L10771 | `def _analyze_s3` | `globals().get` | `_ff_first_clause` | `True` | Trạng thái tồn tại: True |
| 15 | L10772 | `def _analyze_s3` | `globals().get` | `_EXPLAIN_LEAD_RE` | `True` | Trạng thái tồn tại: True |
| 16 | L11275 | `def _ff5_has_fresh_evidence_after_last_act` | `in globals()` | `_ff9_read_texts` | `True` | Hàm flowfix đã định nghĩa cố định ở module level: **ĐÃ TỒN TẠI** |
| 17 | L11277 | `def _ff5_has_fresh_evidence_after_last_act` | `in globals()` | `_ff17_find_key` | `True` | Hàm flowfix đã định nghĩa cố định ở module level: **ĐÃ TỒN TẠI** |
| 18 | L11279 | `def _ff5_has_fresh_evidence_after_last_act` | `in globals()` | `_tests_ran_after_act` | `True` | Trạng thái tồn tại: True |
| 19 | L11982 | `def _ff8_write_check` | `in globals()` | `_ff9_read_texts` | `True` | Hàm flowfix đã định nghĩa cố định ở module level: **ĐÃ TỒN TẠI** |
| 20 | L11983 | `def _ff8_write_check` | `in globals()` | `_ff17_find_key` | `True` | Hàm flowfix đã định nghĩa cố định ở module level: **ĐÃ TỒN TẠI** |
| 21 | L11985 | `def _ff8_write_check` | `in globals()` | `_tests_ran_after_act` | `True` | Trạng thái tồn tại: True |
| 22 | L12060 | `def _ff8_file_views` | `in globals()` | `_ff4_off` | `True` | Trạng thái tồn tại: True |
| 23 | L15788 | `def _ff12_user_creates` | `in globals()` | `_ff16_plan_files` | `True` | Trạng thái tồn tại: True |
| 24 | L15806 | `def _ff5_act_needs_read_problems` | `in globals()` | `_ff9_read_texts` | `True` | Hàm flowfix đã định nghĩa cố định ở module level: **ĐÃ TỒN TẠI** |
| 25 | L17232 | `def _ff15_label` | `globals().get` | `_FF10_DEFECT_RE` | `True` | Hằng số regex / helper đã định nghĩa cố định ở module level: **CÓ THỂ THAY BẰNG THAM CHIẾU TRỰC TIẾP** (hoặc giữ an toàn) |
| 26 | L17233 | `def _ff15_label` | `globals().get` | `_SOURCE_STRONG_VERB_RE` | `True` | Hằng số regex / helper đã định nghĩa cố định ở module level: **CÓ THỂ THAY BẰNG THAM CHIẾU TRỰC TIẾP** (hoặc giữ an toàn) |
| 27 | L17234 | `def _ff15_label` | `globals().get` | `_FF5_HOWTO_RE` | `True` | Hằng số regex / helper đã định nghĩa cố định ở module level: **CÓ THỂ THAY BẰNG THAM CHIẾU TRỰC TIẾP** (hoặc giữ an toàn) |
| 28 | L18497 | `def _ff17_strip_numbered_lines` | `globals().get` | `_FF4_TRUNC_RE` | `True` | Hằng số regex / helper đã định nghĩa cố định ở module level: **CÓ THỂ THAY BẰNG THAM CHIẾU TRỰC TIẾP** (hoặc giữ an toàn) |
| 29 | L19275 | `def _ff17_merge_read_chunk` | `in globals()` | `_ff4_off` | `True` | Trạng thái tồn tại: True |
| 30 | L20224 | `def _ff17_evaluate_plan_steps` | `in globals()` | `_ff17_extract_verification_commands` | `True` | Trạng thái tồn tại: True |
| 31 | L20252 | `def _ff17_evaluate_plan_steps` | `in globals()` | `_ff17_audit_plan_compliance` | `True` | Trạng thái tồn tại: True |
| 32 | L21385 | `def _change_tail_s6` | `in globals()` | `_ff17_attach_audit_note_to_answer` | `True` | Trạng thái tồn tại: True |
| 33 | L21810 | `def _change_tail` | `in globals()` | `_ff17_attach_audit_note_to_answer` | `True` | Trạng thái tồn tại: True |
| 34 | L21955 | `def _gate_act_calls` | `in globals()` | `_ff17_find_key` | `True` | Hàm flowfix đã định nghĩa cố định ở module level: **ĐÃ TỒN TẠI** |
