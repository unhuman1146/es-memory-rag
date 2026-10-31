# 墨渊·Agent识海集成系统（ES-Mind）1.4.1 本机验证记录

最终展示品牌：**墨渊·Agent识海集成系统 / ES-Mind**，即原 ES-Memory RAG 的新品牌。此次仅改对外文档展示名，机器标识与交付文件名保持 `es-memory-rag`；不宣称意识。品牌同步后重新执行发布检查（286 项及两个离线 demo）、MANIFEST、patch 应用与 ZIP 字节校验，具体日志在交付目录。

日期：2026-10-31。基线：`ecce9d71ffc5695c8fb18cac4680233a65739621`，克隆时 origin/main 与旧评审提交相同。隔离分支：`fix/1.4.1-review`。
环境：Windows / Python 3.11.9，标准库；只使用临时合成数据库和离线 HashingEmbedding。

## 实际执行结果

基线完整离线测试：249/249 PASS。修复前新增最初 9 项复现测试：8 FAIL、1 PASS；失败包括双连接可控交错、情绪残留、模型 / 维度 / 旧索引缓存、长记忆挤占、失效候选补拉与完整提示。没有用 sleep 调度竞态。

| 最终测试组 | 数量 | 结果 |
|---|---:|---|
| memory_engine | 43 | PASS |
| context_budget | 17 | PASS |
| adapter | 43 | PASS |
| chat_path | 67 | PASS |
| slow_forget | 35 | PASS |
| release | 22 | PASS |
| context_pager | 22 | PASS |
| patch_141 | 37 | PASS |
| 合计 | 286 | PASS |
| demo_140_alpha.py | 离线演示 | PASS |
| demo_paging.py | 离线演示 | PASS |

`python scripts/run_release_checks.py` 与 unittest discovery 分别完整通过。两 demo 另留原始输出日志。

## 复现与回归对应

- PurgeTests：完整情绪 / 审计自由文本清理，旧 purge 修复，事务回滚，未确认 / 死信删除阻止 purge。双连接在 SELECT.fetchall 后注入提醒：旧版提醒提交后仍被清空；修复版写锁串行化，随后提醒明确拒绝。另测提醒先提交则 purge 跳过，保留源数据及 upsert 同步状态。
- IdentityTests：同维换模型、换 provider / version、变维、旧缓存与混合空间、损坏维度、自定义后端，身份不含 API key / URL 凭据 / 查询串；相同稳定身份跨实例仅权重更新不调用 embedding。
- RecallTests / ChatRefillTests：长记忆不挤掉其他 distinct 记忆，失效 / forbidden 候选补拉、旧适配器补拉、敏感 / 已加载过滤、无进展退出；rerank 前后 ACL 复核和预算仍有效，完整声明不覆盖全库。
- ResidueTests：自然遗忘保留有限抽象感受，明确 erase 只清对应条目，owner-only 删除、private 读取隔离、事件语义不混入事实 RAG、闭式衰减在重启 / 多次读取后一致、旧 schema 增量迁移、无已验证 cue / 未知来源不猜补。
- 正常情绪与独立状态：维护 / 压缩 / charge 归零保留 L1 / L3；换页保留完整关系台账，回收期源情绪仍存在。独立状态层和 ContextPager 实现未修改。

## 新增测试名（scripts/test_patch_141.py）

### PurgeTests

- `test_purge_scrubs_all_emotion_history_and_free_text_audit`
- `test_purge_selection_blocks_concurrent_reminder_without_sleep`
- `test_reminder_committed_before_purge_preserves_history_and_sync`
- `test_purge_failure_rolls_back_content_revision_and_audit`
- `test_legacy_purged_emotion_is_inaccessible_and_scrubbed`
- `test_unacknowledged_or_dead_delete_still_blocks_purge`
- `test_live_emotion_history_survives_decay_compression_and_purge_scan`
- `test_context_paging_preserves_emotion_and_relationship_ledger`

### IdentityTests

- `test_changed_model_same_dimension_reembeds_identical_text`
- `test_changed_dimension_reembeds_identical_text`
- `test_legacy_unidentified_vectors_are_not_reused`
- `test_provider_and_version_changes_invalidate_same_instance_cache`
- `test_same_identity_weight_only_revision_reuses_after_restart`
- `test_search_rejects_legacy_and_mixed_model_indexes_until_rebuilt`
- `test_identity_does_not_store_key_url_credentials_or_query`
- `test_explicit_custom_identity_reuses_and_opaque_backend_fails_closed_on_restart`
- `test_corrupt_vector_dimension_is_not_reused`

### ResidueTests

- `test_natural_forgetting_preserves_only_abstract_cue_and_numbers`
- `test_explicit_erase_clears_only_target_residue_and_never_restores`
- `test_explicit_erase_scrubs_pending_payloads_and_waits_for_index_ack`
- `test_explicit_erase_is_idempotent_and_owner_only_even_for_shared`
- `test_ownerless_explicit_erase_fails_closed`
- `test_residue_private_acl_and_current_authority_both_apply`
- `test_residue_decay_is_restart_and_query_frequency_independent`
- `test_unknown_deletion_source_drops_residue_and_records_policy`
- `test_no_cue_is_inferred_from_old_event_text`
- `test_cues_reject_arbitrary_text_and_match_only_requested_scene`
- `test_erase_failure_rolls_back_residue_source_and_outbox`
- `test_old_schema_upgrade_is_additive_and_defaults_unknown`

### RecallTests

- `test_long_memory_does_not_crowd_out_distinct_matches`
- `test_stale_candidates_are_replaced_by_live_candidates`
- `test_complete_notice_does_not_claim_whole_library_recall`
- `test_forbidden_candidates_are_replaced_before_rerank`

### ChatRefillTests

- `test_chat_refills_legacy_adapter_and_checks_acl_before_and_after_rerank`
- `test_chat_refills_after_already_loaded_and_sensitive_candidates`
- `test_nonprogressing_adapter_terminates_and_budget_still_holds`
- `test_long_memory_window_keeps_other_memory_and_budget`

## 边界和未执行项

- 未调用付费或真实 embedding / reranker / LLM API；未修改真实记忆库、网站或任何生产服务；未 push、tag、release、deploy 或上传云端。
- 自然余韵只接收宿主事先验证的固定 cue；旧数据无线索不生成。宿主还须传 cue 并调用 `emotion_resonance`，本包没有自动场景推断或自动模型接线。
- 明确 `erase` 是 owner-only 的逻辑清除，requester 需要宿主认证；没有管理员越权路径。远端 delete 排队不等于确认，必须真实同步 ack。SQLite 空闲页 / WAL、备份和外部缓存不属于取证级安全擦除承诺。
- 向量旧索引需按升级说明重建；同名权重更新需人为更新模型版本。检索窗口有界，SQLite 仍全量相似度扫描，不保证全库召回或大规模延迟。
- evidence.md 中原始模型实验记录未提供，34 / 15 / 75 次口径与额外无关输出已澄清为待核实。本次没有重测或编造结论。
- Ubuntu / GitHub CI、真实宿主上下文替换、语义质量与凌晨调度未验证。

具体接口与迁移见 [升级说明](references/upgrade-1.4.1.md)。文件打包、MANIFEST 和 patch 应用检查结果由交付目录中的 `packaging-verification.log` 记录。
