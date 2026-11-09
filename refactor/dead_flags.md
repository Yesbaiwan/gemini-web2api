# Báo cáo rà soát Config Flags (Phase 4.6)

## 1. Khóa đọc nhưng không khai báo (6)

- `fetch_require_image_magic`
- `flow17_session_id`
- `flow4_max_continuations`
- `runtime_check_local`
- `runtime_check_probe`
- `send_sapisidhash`

## 2. Ứng viên Flag không có chỗ đọc (10)

- `flow16_enabled`
- `flow5_auto_advance`
- `flow5_canned_review`
- `flow5_cross_module`
- `flow5_evidence_gate`
- `flow5_lean_evidence`
- `flow5_lean_evidence_chars`
- `flow5_lean_prompt`
- `flow5_scope_check`
- `flow5_step_driven`

## 3. Toggle luôn bật / Cần quyết định (36)

- `auto_update_bl`
- `defuse_extension_words`
- `flow16_cross_module_constants`
- `flow16_explain_imports`
- `flow16_multifile_act`
- `flow16_plan_todo`
- `flow16_review_quotes`
- `flow16_runtime_outer`
- `flow16_sse_cleanup`
- `flow17_act_context_pack`
- `flow17_enabled`
- `flow17_fuzzy_edit`
- `flow17_plan_compliance_audit`
- `flow17_plan_todo_act`
- `flow17_session_ledger`
- `flow17_static_pre_gate`
- `flow17_symbol_graph`
- `flow17_targeted_tests`
- `flow18_enabled`
- `flow18_honest_final`
- `flow19_anti_oscillation`
- `flow19_call_sig_check`
- `flow19_enabled`
- `flow19_override_sigs`
- `flow8_absence_proof`
- `flow8_bard_strict`
- `flow8_destructive_wide`
- `flow8_independent_grants`
- `flow8_lean_source`
- `flow8_revert_needs_edit`
- `flow8_strict_shell`
- `flow8_write_regate`
- `log_requests`
- `required_tool_fail_on_duplicate`
- `tool_delta_forward`
- `tool_retry_on_miss`
