# 当前候选逐场景验收台账

> 最新范围：B05实际12矩阵、B11正确parent链/Memory及B09已完成usage停止子项已passed。原模型实际tool能力可用，O02完整generated run/tool call/memory/compaction正式转本地pending1；外部required由机器重算为8项/7场景。cleanup仍blocked-approval，0consumer启动。43e2当时local0/external9仅历史分类，不能再当当前不可执行结论。

审计日期：2026-09-30（Asia/Shanghai）；源码审计基线 `953cf1c1ef88258197c72b98e89fcd1dab85ac39`。逐项当前证据维度见`acceptance-ledger-state-2026-09-30.json`；下表保留本轮起始状态，不能拿起始列代替最新执行结果。源测试、历史运行和当前完整镜像业务验收分别记录。每次实际验收追加单独 evidence 文件及 candidate/tree/image ID或digest、环境摘要、DB heads、脱敏输入、预期/实际输出、时间与执行者；不输出密钥。

用户异常优先闭环：应用中心现有产品契约仍是 installed apps。已发布且已安装应用缺stats应可见/打开；新App创建正确初始化stats。完整分类/搜索/同步/权限矩阵由F01/F02继续验。

| ID 以下初次审计表仅保留历史来源；当前状态与缺口以末尾机器汇总和顶部说明为准。

| 场景 | 本轮起始状态 | 范围/解除条件 | 已有证据及限制 | 负责节点 |
| --- | --- | --- | --- | --- | --- |
| G01 | 全部冲突处置与新宿主 | historical-source-passed; current-snapshot-review-pending | 当前源码 | M00/M08/R01 | N02/N03–N05 |
| G02 | 四个tag后提交行为保留 | historical-source-passed; current-snapshot-review-pending | 当前源码 | M00/M08/R01 | N02/N03–N05 |
| B01 | setup/邮箱/OAuth/邀请/已有账号额度 | source-tests-only-or-unverified-live | 当前本地必测；条件关闭态必须有证据 | 历史M/V定向源码证据可按hash复用，无完整当前真实场景 | N02/N03–N05 |
| B02 | normalized_email碰撞 | source-tests-only-or-unverified-live | 当前本地必测；条件关闭态必须有证据 | 历史M/V定向源码证据可按hash复用，无完整当前真实场景 | N02/N03–N05 |
| B03 | GitHub/Google/OAuth2/Casdoor/钉钉真实SSO | pending-real-provider-evidence | 已有凭据直接验；无凭据外部条件blocked | M03仅mock/单测；真实账号/配置待执行者安全盘点 | N02/N03–N05 |
| B04 | login_config双阶段/SSR/错误与脱敏 | source-tests-only-or-unverified-live | 当前本地必测；条件关闭态必须有证据 | 历史M/V定向源码证据可按hash复用，无完整当前真实场景 | N02/N03–N05 |
| B05 | WebApp认证12组合和跨应用边界 | source-tests-only-or-unverified-live | 当前本地必测；条件关闭态必须有证据 | 历史M/V定向源码证据可按hash复用，无完整当前真实场景 | N02/N03–N05 |
| B06 | 两个开关/environment passport/logout/401 | source-tests-only-or-unverified-live | 当前本地必测；条件关闭态必须有证据 | 历史M/V定向源码证据可按hash复用，无完整当前真实场景 | N02/N03–N05 |
| B07 | Console/Explore/WebApp真实计费与RMB/USD对账 | partial-real-response; nonzero-charge-pending | 当前本地必测 | V05/28.3：OK/49 tokens/账号正确/price0USD；非零实测设计见evidence/n05-design/nonzero-billing-acceptance.md（尚未实测） | N02/N03–N05 |
| B08 | Service API五模式与额度边界/租户 | source-tests-only-or-unverified-live | 当前本地必测；条件关闭态必须有证据 | 历史M/V定向源码证据可按hash复用，无完整当前真实场景 | N02/N03–N05 |
| B09 | workflow恢复/停止/retry计费 | source-tests-only-or-unverified-live | 当前本地必测；条件关闭态必须有证据 | 历史M/V定向源码证据可按hash复用，无完整当前真实场景 | N02/N03–N05 |
| B10 | 月初/每日重置和beat队列 | source-tests-only-or-unverified-live | 当前本地必测；条件关闭态必须有证据 | 历史M/V定向源码证据可按hash复用，无完整当前真实场景 | N02/N03–N05 |
| B11 | retention/匿名context/跨租户权限 | source-tests-only-or-unverified-live | 当前本地必测；条件关闭态必须有证据 | 历史M/V定向源码证据可按hash复用，无完整当前真实场景 | N02/N03–N05 |
| K01 | app/dataset/environment key额度与关联 | source-tests-only-or-unverified-live | 当前本地必测；条件关闭态必须有证据 | 历史M/V定向源码证据可按hash复用，无完整当前真实场景 | N02/N03–N05 |
| F01 | 应用中心installed可见/打开/分类/标签/搜索/排序/去重/Home | development-review-passed; full-image-live-page-pending | 当前本地必测 | N01已修复缺stats容错与生命周期；52定向测试/独立源码review通过，见evidence/N01；旧image页面仍无卡片 | N02/N03–N05 |
| F02 | 模板同步/取消/缓存/manager与fork权限 | source-tests-only-or-unverified-live | 当前本地必测；条件关闭态必须有证据 | 历史M/V定向源码证据可按hash复用，无完整当前真实场景 | N02/N03–N05 |
| F03 | 系统管理owner/admin/member入口/URL/API | source-tests-only-or-unverified-live | 当前本地必测；条件关闭态必须有证据 | 历史M/V定向源码证据可按hash复用，无完整当前真实场景 | N02/N03–N05 |
| F04 | 系统管理额度/集成/forward token/代码执行 | partial-quota-ui; full-business-pending | 当前本地必测，外部配置明确缺项 | V03本地额度15→16→15读写；其他业务未验 | N02/N03–N05 |
| F05 | 当前源码工具链/双构建及真实镜像产物 | historical-source-passed; current-image-pending | 当前源码与镜像 | V02/16.15双构建；新API源码与镜像尚待N02/N03 | N02/N03–N05 |
| D01 | 完整镜像空库双链/账号 | partial-source-overlay-passed; full-image-pending | 当前本地必测 | V03/28.2：PG/MySQL双head/省略ID额度ORM | N02/N03–N05 |
| D02 | 历史存量双链 | deferred-by-prior-user-scope | 历史升级，后续单验 | 原V03/V06未通过；无历史数据证明 | N02/原V03/V06 |
| D03 | 历史破坏Agent变更数据处置 | deferred-by-prior-user-scope | 历史升级，后续单验 | 原V03/V06未通过；无历史数据证明 | N02/原V03/V06 |
| D04 | 历史模型凭据去重与解密 | deferred-by-prior-user-scope | 历史升级，后续单验 | 原V03/V06未通过；无历史数据证明 | N02/原V03/V06 |
| D05 | 新安装向量/知识库真实闭环与历史升级分开 | pending-fresh-vector-and-knowledge-base | 当前新安装必测；历史逐站升级另验 | 运行执行者已见datasets空态、当前栈无vector；需新安装测试样本与支持向量准备 | N02/N03–N05 |
| O01 | 镜像/Ingress/队列/自动迁移关闭 | partial-local-runtime; complete-topology-pending | 当前本地必测；registry供货单列 | 28.5 WS101/点击；28.7最新workflow消费；其他队列/供货未验 | N02/N03–N05 |
| O02 | Agent/协同/归档/Human Input启用或关闭态 | source-tests-only-or-unverified-live | 当前本地必测；条件关闭态必须有证据 | 历史M/V定向源码证据可按hash复用，无完整当前真实场景 | N02/N03–N05 |
| O03 | 通用和Agent SSRF边界 | source-tests-only-or-unverified-live | 当前本地必测；条件关闭态必须有证据 | 历史M/V定向源码证据可按hash复用，无完整当前真实场景 | N02/N03–N05 |
| O04 | 历史整套旧版本恢复；本地新安装恢复另见29.8 | deferred-by-prior-user-scope | 历史升级，后续单验 | 原V03/V06未通过；无历史数据证明 | N02/原V03/V06 |
| O05 | 生产放行和观察 | pending-independent-production-authorization | 生产，独立放行 | D00–D04未通过 | N02/D00–D04 |

## 执行要求

1. `passed` 必须限定完整场景和当前精确快照；partial不能勾整项。失败定位首个断点并返回限定修复，稳定后独立验证一次。
2. 当前已有DeepSeek凭据可直接验实际请求，非零价格路径仍需配置/插件模型报价实际读回与账目对账；不能从49 tokens/price0推断扣费正确。
3. 临时单API WebSocket worker及workflow-only消费者只是当前缩减本地栈证据。标准collaboration/api_websocket/nginx和完整队列拓扑需明确选择并验证。
4. 新安装恢复由N06完成；O04原义是旧版全存储恢复，不用新安装恢复关闭历史项。
5. 当前可自主准备的本地知识库/角色账户/额度边界样本由执行者建立并保留可逆方案。真实外部SSO账号/私有registry/旧库副本/生产授权没有证据时保留具体门槛，不阻断可继续的本地任务。


## 明确证据覆盖缺口的子检查（不增加新功能）

| 子项 | 当前证据缺口与实际源码 | 最小验收 | 当前状态/负责人 |
| --- | --- | --- | --- |
| D01.a / F01.a | 28.6 quota模型省略ID两库通过未覆盖AppStatisticsExtend；既有缺stats应用首次使用在app_generate_service_extend.py省略ID构造，model仅server UUID默认 | 两库省略IDstats ORM flush/readback/rollback；现有缺stats实际首次调用后的统计增长/排序读回 | source-fix-review-and-dual-engine-overlay-passed; final-image-pending；app_center双engine，runtime实际业务 |
| D01.b / B05.a / B07.a | EndUserAccountJoinsExtend仅server UUID默认，service_api/wraps.py登录WebApp enduser关联省略ID | 两库省略IDjoin ORM flush/readback/rollback；真实登录WebApp正确账号join且quota归因 | source-fix-review-and-dual-engine-overlay-passed; final-image-pending；app_center双engine，runtime实际业务 |
| D01.c / B11.a | AppExtend仅server UUID默认，console/app/model_config.py首次retention保存省略ID；认证开关服务显式UUID不能证明该入口 | 两库省略IDAppExtend ORM；新App未有extend行时实际保存retention→刷新值正确 | source-fix-review-and-dual-engine-overlay-passed; final-image-pending；app_center双engine，runtime实际业务 |
| D01.d / B11.b | MessageContextExtend仅server UUID默认，base_app_runner.py多轮截断插入省略ID | 两库省略IDcontext ORM；实际超过retention后context marker创建并正确截断 | source-fix-review-and-dual-engine-overlay-passed; final-image-pending；app_center双engine，runtime实际业务 |
| F01.b / F02.a | 新session tag读取源码已修复，但28.x未验实际tag create/rename/delete/bind/unbind→category/filter/cache | 专用App/tag实际CRUD及绑解，中心分类/搜索和sync/cancel后缓存刷新 | pending-live；runtime |
| F04.a | 28.xquota真实表仅1账号/一次金额编辑，分页和多行编辑未验 | 多测试账号跨页，编辑目标行、刷新回读USD金额与RMB header按角色正确展示 | live-partial：11账户/2页与7位API写读过；同used排序漂移confirmed baseline，N07修复后复验；独立权限执行者 |
| K01.a | 12.6i API-key create-copy/edit-delete/禁止管理仅mock/组件tests，非当前真实生成/关联/删除 | 专用key真实create、masked list、copy仅内存、关联quota编辑/删除后不存在、member无mutation | pending-live；runtime |
| B01.a / B03.a | M03账号/OAuth/邀请仅unit mocked external I/O；28.x只验管理员setup | 安全本地邮箱sink/已有测试账户，真实新账号quota仅一行；现有SSO启用态真实callback/state/session授权调用 | pending-live；独立权限/身份执行者；外部未配置单列 |
| B09.a / O01.a | 28.3真实workflow仅start/llm/answer，无传统Tool node配置/执行；WS101也不能证明tool/plugin/sandbox契约 | 当前已支持/已有可用工具node配置save→刷新→发布→run，读回节点输入/输出与plugin task/错误归属 | pending-live；runtime |
| O01.b / B07.b / B09.b | 正式main worker默认有workflow_based_app_execution，worker-gaia有extend_high/low；28.7缩减stack只消费workflow执行，非源码丢失 | 可复用本地override启动完整必要消费者，读active_queues/registered tasks和实际投递/消费/非零账目；holding任务继续保留 | pending-live-runtime-topology；runtime，源码无需queue修复 |

四模型现已由独立真实双engine复现：PG通过，MySQL全部NULL identity FlushError。客户端UUID最小修复及60项关联tests/独立源码review、两库各4omitted+4explicit回读rollback已通过（精确model overlay），最终archive已包含修复，完整镜像rebuild仍待通过；不得用当前修复前image通过当最终。具体源码/证据见`evidence/coverage-holes-2026-09-30.md`。


F04.b / D01.e：MySQL PostgreSQL OnConflictDoUpdate异常已修复为方言atomic upsert；27 targeted通过，PG/MySQL实际insert/update、7位精度、保留used_quota、两种4并发/unique/cleanup空库通过。独立只读review无finding：`evidence/N04/independent-quota-upsert-source-review.json`及`evidence/quota-upsert-regression`。此为source-overlay service实证，完整image与身份UI/API仍pending。最终12-overlay context `3c632717…a296ad`包含全部修复及api/uv.lock，candidate image尚未ready。


## 用户原入口最终交付gate（N06 / F01）

当前新栈localhost:23010通过只证明独立验收，不替代用户报告的127.0.0.1:23000。新栈稳定最终镜像验证通过后，运行执行者按已存在授权保留原项目旧image/config/数据库备份恢复锚点，仅更新API及必要standardgaia消费者；原WS performance override继续保留，禁止覆盖用户model/app配置/凭据/plugin/files/旧holding任务。必须在原`/explore/apps-center-extend`真实看到并打开此前遗漏的已安装已发布App；原URL、实际image/hash、无配置覆盖和恢复锚点都入证据，才关闭29.12/N06用户异常交付gate。

最新候选门槛N07：真实F04多页同used记录更新total后行漂页，属于当前实际验收发现的既有UX问题，不宣称mergeintroduced。最小稳定tie修复/源码review/newmanifest/canonical增量image/同子项真实复验完成前N05/N06保持pending。N02/N03 accepted3c历史证据不撤销；每条后续业务证据按源码影响绑定latestcandidate或明确不变hash复用。

机器聚合截至N03通过、N07分页修复待执行：29项中8项有当前fresh/live/artifact部分实证，16项当前场景完成待验，4项historical deferred，1项production gated；当前full business scene尚无整项签收。N03部署就绪已独立passed，不因矩阵尚未全签收反复pending。角色/API先行观察待专用持久report，不能以简讯冒充完整场景通过。

F01/N01当前fullimageAPI实证已持久：`evidence/N01/published-app-missing-stats-runtime-2026-09-30.json`。真实产品API建立/发布/同步/安装两合成workflow，各有唯一stats；仅针对其中一个合成app删1stats后中心list仍返回且installed detail/parameters200。浏览器显示/打开仍pending，N01未提前passed；原23000未改。

Owner角色批次已持久：`evidence/role-permissions-runtime/owner-results.md`。F03 owner入口/直链/6API已过；F04实际10+1分页、两行7位额度编辑/回读、search/clear/size、forward-token与code-control CRUD/cache清理已过。管理员/成员仍运行，same-used稳定tie仍N07，外部integration实际测试缺凭据单列。invitation mismatch403按B01身份子项聚合，不把该证据误当F05工具链通过。

## 历史逐项缺口快照（不代表当前状态）

当前完整候选为15路径/4316文件、context `1a805b0e…eecb3d8`，canonical API image `51347867…108619`。本表以每项必要子检查明确缺口；源码静态证明写 `source_verified`，实际API/UI证明写 `passed`，缺provider/model/embedding为条件门槛。旧启动表与历史证据保持原样。

| 场景 | 当前结论 | 本地/源码待验子项 | 外部条件待验子项 |
| --- | --- | --- | --- |
| `G01` | passed | — | — |
| `G02` | passed | — | — |
| `B01` | partial-current-proof | B01.one_initial_quota_and_existing_login_not_reset<br>B01.failed_transaction_no_partial_account_quota | B01.enabled_external_oauth_account_init |
| `B02` | pending-current-completion | B02.gmail_googlemail_case_alias_collision_invite_login<br>B02.existing_duplicate_not_merged_or_crosslogged | — |
| `B03` | pending-current-completion | B03.unconfigured_provider_disabled_and_error_paths | B03.enabled_provider_callback_state_invite_session_api |
| `B04` | pending-current-completion | B04.cookie_header_config_bootstrap_privacy<br>B04.missing_bad_signature_wrongIP_reject<br>B04.SSR_config_not_cached_ping | — |
| `B05` | pending-current-completion | B05.on_blocks_anonymous_and_bad_code_three_modes<br>B05.cross_app_identity_scope | B05.off_authenticated_and_anonymous_actual_generation_12_matrix |
| `B06` | pending-current-completion | B06.two_switches_singlefield_save_refresh_cache<br>B06.environment_scope_disabled_or_passport_logout_401 | — |
| `B07` | pending-current-completion | — | B07.console_explore_webapp_nonzero_usage_actor_RMB_USD_reconcile<br>B07.anonymous_billing_baseline_separately_recorded |
| `B08` | pending-current-completion | B08.workflow_service_token_run_join<br>B08.cross_tenant_reject | B08.five_mode_nonzero_daily_monthly_total_balance_boundary |
| `B09` | partial-current-proof | B09.no_new_dispatch_bypass_source_scope | B09.stop_resume_retry_usage_preserved_LLM_attribution |
| `B10` | pending-current-completion | B10.three_beat_unique_registration_switch_queue<br>B10.isolated_daily_monthly_reset_snapshot_readback | — |
| `B11` | pending-current-completion | B11.null_missing_authonly_AppExtend_create<br>B11.anonymous_no_console_context_cross_tenant_reject | B11.authenticated_multiturn_retention_truncation |
| `K01` | partial-current-proof | K01.cross_tenant_key_reject | — |
| `F01` | partial-current-proof | F01.category_tag_search_sort_dedup<br>F01.default_home_no_redirect_explicit_destination | — |
| `F02` | pending-current-completion | F02.workspace_summary_manager_fork_permission_matrix<br>F02.sync_cancel_cache_refresh_member_reject | — |
| `F03` | passed | — | — |
| `F04` | partial-current-proof | F04.sameused_pages_edit_refetch_latestimage | F04.enabled_dingtalk_oauth2_email_actual_test |
| `F05` | passed | — | — |
| `D01` | partial-current-proof | D01.latest_fullimage_dualengine_4models_quota_write_read_rollback | — |
| `D02` | deferred | — | 历史延期 |
| `D03` | deferred | — | 历史延期 |
| `D04` | deferred | — | 历史延期 |
| `D05` | partial-current-proof | D05.own_samples_delete_rebuild_file_consistency | D05.high_quality_vector_counts_retrieval_grpc_TLS_config |
| `O01` | partial-current-proof | O01.plugin_enabled_operation_and_registry_identity | — |
| `O02` | pending-current-completion | O02.actual_disabled_flags_effective_no_dependency_startup_break | — |
| `O03` | pending-current-completion | O03.generic_allowhost_deniedhost_actual_proxy<br>O03.agent_specific_scope_no_generic_whitelist_expansion | — |
| `O04` | deferred | — | 历史延期 |
| `O05` | production-not-authorized | — | 生产未授权 |

历史机器汇总：{"passed": 4, "partial-current-proof": 8, "pending-current-completion": 12, "deferred": 4, "production-not-authorized": 1}；19个scene有31个本地/源码待验子检查，另外B07只有外部条件。N06新安装恢复与原23000真实交付仍为独立必做门槛。

本轮补齐：真实TenantB同资源200 + OwnerA foreign App/Dataset/Key拒绝、bound/unbound service scope、工作流token→run精确join1；K01、B04、B06本轮局部契约全部通过。Context同租户正向500显式N09待完整候选受影响复验，不以foreign404替代正向可用。

最新机器汇总（以JSON为事实源）：

| 场景 | 状态 | 剩余本地子检查 | 外部条件子检查 |
| --- | --- | --- | --- |
| `G01` | passed | 0 | 0 |
| `G02` | passed | 0 | 0 |
| `B01` | partial-current-proof | 0 | B01.enabled_external_oauth_account_init |
| `B02` | passed | 0 | 0 |
| `B03` | partial-current-proof | 0 | B03.enabled_provider_callback_state_invite_session_api |
| `B04` | passed | 0 | 0 |
| `B05` | passed | 0 | 0 |
| `B06` | passed | 0 | 0 |
| `B07` | pending | 0 | B07.console_explore_webapp_nonzero_usage_actor_RMB_USD_reconcile, B07.anonymous_billing_baseline_separately_recorded |
| `B08` | partial-current-proof | 0 | B08.five_mode_nonzero_daily_monthly_total_balance_boundary |
| `B09` | partial-current-proof | 0 | B09.stop_resume_retry_usage_preserved_LLM_attribution |
| `B10` | passed | 0 | 0 |
| `B11` | passed | 0 | 0 |
| `K01` | passed | 0 | 0 |
| `F01` | passed | 0 | 0 |
| `F02` | passed | 0 | 0 |
| `F03` | passed | 0 | 0 |
| `F04` | partial-current-proof | 0 | F04.enabled_dingtalk_oauth2_email_actual_test |
| `F05` | passed | 0 | 0 |
| `D01` | passed | 0 | 0 |
| `D02` | deferred | 0 | 0 |
| `D03` | deferred | 0 | 0 |
| `D04` | deferred | 0 | 0 |
| `D05` | partial-current-proof | 0 | D05.high_quality_vector_counts_retrieval_grpc_TLS_config |
| `O01` | passed | 0 | 0 |
| `O02` | partial-current-proof | O02.generated_agent_run_tool_call_memory_compaction | 0 |
| `O03` | passed | 0 | 0 |
| `O04` | deferred | 0 | 0 |
| `O05` | production-not-authorized | 0 | 0 |

本轮真实生成/retention/停止证据留档43e2；新的O02本地完整范围不得由health/config/JWE/files替代。原control token空、3services absent/DNSfail，但模型tool schema三个true，无新modelcall；5服务sidecar二审四项修订已解决，最后Stub ACL alias修正+quiet后按条件授权无模型启动；真实预算guard为独立后续调用gate。cleanup自动审核拒绝，待exact3人类授权。

O02 本轮实际阶段：5 个专用服务 running、API 容器内部 health200、4 个内部依赖 TCP 可达、内部认证字段相等及原密钥文件 hash 相等布尔成立；主机端口映射仍为空，合法 sidecar owner/tenant 门槛未过。加入原共享 access network 的动作被自动审批拒绝，等待明确人类授权，未执行或换网绕过；Agent0/上传0/模型调用0。文件挂载已采用 owned RW 根加原 privkeys 目录 RO subpath，完整 Agent 工具/记忆/压缩仍本地待验。见 original-generation-runtime/independent-o02-runtime-health-review.json。
