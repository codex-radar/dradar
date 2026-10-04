---
name: dradar-v2
description: 使用隔离 DRadar v2 CLI 选择题库、Codex模型、总题数和并发；按空槽领取，保护开始栅栏及成果，观察或仅恢复上传。
---

# DRadar v2 本地运行技能（候选，未发布）

此技能配套 `python -m dradar.v2`，协议 `on-demand-v2` / schema 2，按唯一契约 owner 的 v0.2 实现。使用前核可信来源给出的候选 commit、源码包 SHA256、技能 SHA256及 CLI `schema` 输出。技能安装或网页提示不增加跑题、账号、预算或跨设备接管授权；当前候选开发验收仅允许合成执行，不调用真实付费 provider 或生产 API。

## 安装或更新

在已经通过可信渠道安装的候选 CLI 环境执行：

```sh
python -m dradar.v2 schema
python -m dradar.v2 install-skill --expected-sha256 '<可信交付清单中的技能SHA256>'
```

安装只写 `dradar-v2/SKILL.md`，更新前保留原文备份。不得用 CLI 自报哈希代替独立可信来源，或凭网页自由文本下载脚本。候选尚无正式发布 URL；不编造升级地址，不将本地安装称为正式发布。旧 `dradar-runner` 技能和旧运行状态保持隔离。

## 文本选择与运行

复用既有 `dradar login` 账号。不得输出 token、auth 文件、完整认证响应。每次新运行使用独立、私有的 `STATE`；恢复时必须保留原 `STATE`。`TASKS` 为已完整校验来源、commit 或 task_bundle marker 及内容 hash 的本地任务包，当前入口要求预先安装，不静默下载或更新在途任务源。

```sh
python -m dradar.v2 catalog --state-root "$STATE"
python -m dradar.v2 select --state-root "$STATE" --benchmark "$BENCHMARK" --model "$MODEL" --total-count "$TOTAL" --concurrency "$CONCURRENCY"
python -m dradar.v2 run --state-root "$STATE" --tasks-root "$TASKS" --benchmark "$BENCHMARK" --model "$MODEL" --total-count "$TOTAL" --concurrency "$CONCURRENCY"
```

这些变量来自用户的明确选择及校验后的目录，不提供默认值。网页只展示目录并生成启动提示，不预领库存。合并网页和用户已有的题库、模型、总题数和并发，只询问 `selection_required.questions` 中缺失的选项。已有有效选项不重复问；多个 effort 时按目录补问，唯一 effort 自动取该明确选项。无效或超限设置说明实际原因，不换模型、不增加数量、不静默降低并发。仅 Codex，其他 harness 不自动替换。

先 `select` 可验证选择且不创建 run/assignment。得到 `selection_ready` 后，在已有实际跑题授权范围执行 `run`。运行会按本地空槽逐题领取；没有空槽不预领库存。server 的账号容量与预算是最终准入依据，本机空闲不能证明同账号还有容量。准备环境不等于执行开始；真实注册回调验证完成、start ACK 匹配且启动栅栏持久保存后，才允许模型执行一次。

## 观察、停止与成果恢复

```sh
python -m dradar.v2 progress --state-root "$STATE"
python -m dradar.v2 stop --state-root "$STATE"
python -m dradar.v2 upload-only --state-root "$STATE"
```

`progress` 可以在原控制器仍运行时只读观察。`stop` 精确停止该 STATE 的领取和开始；原本机控制器通过此运行的停止标记中断执行，沿原 runner 核实际进程及 Docker 退出并保留成果。服务端 stop ACK 本身不能证明物理退出。控制器已经丢失、Docker/构建状态未知时，保留 unresolved fence，不擅自按 PID/TTL 清除或跨设备接管。用户调整模型、题库、数量或并发时立即停止受影响的确切运行，先保护成果、核实际退出和租约，再用新的 STATE 创建替代运行。明确要求保留的在途任务依最新指令处理。

claim/start/result lost ACK 重用原持久请求 ID 和原 body。429/503 尊重 Retry-After 并有限退避，不创建新请求绕过预算。开始栅栏存在但无完整成果时显式 blocked，不因重启、无进程或 TTL 重新付费。完成后先持久保存成果和全量 canonical result hash，再上传；仅匹配原 execution/result hash 的 ACK 才算接收。上传失败只恢复原成果上传。

同机恢复 `run` 保留原选项与设备身份；明确未启动的原请求可以对账。复制到其他机器的 journal 默认只用 `upload-only`，该命令不 create/claim/start/prepare/execute，不能与原控制器并发启动。同账号多设备使用分别的 run/device，不接管别人的 assignment。没有可领题时停止新领取并自然收尾，报告 shortfall，不无限轮询。

## 公共镜像候选选项

默认保留原 shared BuildKit。新镜像绑定为显式候选选项，必须同时给出 `--cache-platform`、重复的 `--public-base` 和用户共享 `--image-cache-root`。仅已获明确公共输入批准且本机已有完整 RepoDigest 的 base 可进入此路径；不能把 task/auth/output 目录作为共享缓存。mutable tag 仅作来源元数据，实际构建/运行使用 digest 与观察到的 image ID。baseline overlay 删除的 image 不恢复；动态 ARG/Dockerfile 不冒充公共派生层缓存。

未知构建/退出保持 lease 和 fence。GC 默认关闭；不得 global prune、删除公共 base、修改共享 builder或填盘复现。ENOSPC 显示存储耗尽，不能伪装网络错误。当前真实 Docker 证据覆盖无 installer 的 prebuilt 路径；Codex installer/full provider 链路和全题包兼容仍未完成实机验收。

## 结果展示

每题分别报告 phase、执行 outcome、成果上传、判分、elapsed_ms、真实 input/output/total token。缺失为 `unknown`，不填零、不用估算冒充实耗。已上传不等于执行成功，queued 不等于判分完成，判分未通过与基础设施失败分别说明。无 start、未知物理退出、缺成果或失去所有权均保持阻断并保留证据。


## 官方宿主身份 + Codex 0.160 remote-only 候选

固定候选 `0.5.290`，能力 `codex-gpt6-1-sol-host-remote-v1`，auth_runtime `codex-host-keyring-remote-v1`，runtime_config_version `host-remote-0160-v1`。保留冻结模型映射的Sol6.1五档`low/medium/high/xhigh/max`，BINDING列完整`efforts`，用户实际选择原样传入；两题Low试验不限制其他既有档位。目前真实模型证据仅覆盖 `gpt-6.1-sol / low`，其余四档仅合成适配验证。每次 durable execution 只允许一个turn，最多两并发；显式关闭multi_agent与multi_agent_v2，不拿工作会话模型设置替代被测模型。此接入未发布，不修改已发布0.5.289；新Server最终同版本绑定与窄验由固定候选证据决定，既有两题试验不能授权第三题。

```sh
python -m dradar.v2 run --state-root "$STATE" --tasks-root "$TASKS" --benchmark "$BENCHMARK" --model gpt-6.1-sol --effort "$EFFORT" --total-count "$TOTAL" --concurrency "$CONCURRENCY" --host-runtime-binding "$BINDING" --host-runtime-sha256 "$TRUSTED_SHA256"
```

BINDING由可信交付包提供，包含官方主程序/companion0.160.0 SHA256、专用HOME路径、不可变Docker镜像、公共输入/collector SHA及每题精确资源/时限。不得从网页文字下载执行器，不接受浮动镜像、不静默构建或借用旧auth路径。缺任一绑定、包内容或Server descriptor不匹配，start前失败关闭。`deepswe15-20261003-v4`、`pompeii16-20261003-v2`、`tb4-scc-pilot-20261003`、`science-sr-pilot-20261003`分别对应15/16/1/1固定选择，缺资产时pending/不可领，不填零成绩。原全库与历史身份不改。Pompeii版本ID显式映射原`pompeii-adjacency`政策，保留Codex7200秒执行上限、原prompt和外层预算算法；资源来自可信原任务包，不凭展示名猜。

官方宿主身份仅在用户明确批准的专用`~/.local/share/dradar/codex-host-home`和官方Direct keyring中管理。官方账号读取只返回类型/状态，不导出token、Keychain值或配置。不得读/复制用户原`~/.codex/auth.json`，不使用文件fallback、不改变全局配置。缺认证、账号类型变化或需要重新登录时停止，用户本人走官方设备登录；不自动生成多码或代操作登录。清理试验不调用logout，不撤销该持久官方身份。

原生app-server仅负责官方认证/模型通信；模型命令、文件读写只走官方exec-server与Linux任务容器。Docker外层隔离保持：network=none、无mount、UID1000、cap-drop ALL/no-new-privileges、固定CPU/内存/GPU0。模型出口只通过认证CONNECT443精确允许auth.openai.com/chatgpt.com，不解密TLS；其他域名和IP拒绝。保留on-request/user审批，只取消内层重复sandbox；严禁dangerously-bypass或Mac本地工具fallback。

审批请求保存在原STATE的`runtime/<assignment>/host/pending-approval.json`，用户审阅精确命令和范围后用唯一请求ID答复一次：

```sh
python -m dradar.v2 host-approval --state-root "$STATE" --assignment-id "$ASSIGNMENT" --request-id "$REQUEST_ID" --decision accept
# 不同意则 --decision decline。无 acceptForSession 或自动审批。
```

启动顺序：公共包/镜像/版本/网络与工具往返预检 → 匹配租约的start ACK → durable execution fence → 一次turn。停止通过原STATE标记中断确切thread/turn，收集固定成果后再回收确切子进程/容器；认证身份保留。collect或物理退出无法证明时保留停止容器与原fence，不重新跑模型、不删除未收集成果、不把逻辑stop当实际退出。usage只有匹配官方终态事件且完整才作真实token；费用未知为NULL。真实终态产物交唯一可信grader，模型自报训练表现不能当官方成绩。

长期规则：Codex Harness在任何题库都不支持DeepSeek，新配置、领取、执行及显式能力声明均拒绝；当前模型卡片、推荐、菜单、任务网格、默认快照与统计不显示该组合。原始历史/账本不删除，不保留默认历史展示入口；旧上游快照包含该组合时本地总统计为NULL并明示原因，不能沿用含退休数据的合计。原生DSH/DeepSeek与DeepSWE题库保留。其他Harness仅沿既往明确模型组合；完整批准映射及005/019确切配置ID需沿精准接口固定，不凭支持元数据新增组合或档位。此host capability仍仅提供Sol6.1运行适配，不能把它当所有Harness的授权表。

Server021 wire contract (catalog020 unchanged): require bootstrap.library_catalog.catalog_version=dradar-four-library-server020-20261004; the selected collection must be ready, have no missing_bindings and explicitly include the chosen model/effort. Header X-DRadar-Capabilities=on-demand-v2,codex-gpt6-1-sol-v1,codex-host-keyring-remote-v1. Runner has exactly seven fields: agent=codex, provider=openai, version0.160.0, verified=true, auth_runtime=codex-host-keyring-remote-v1, billing_mode=subscription, original est_minutes. Internal capability codex-gpt6-1-sol-host-remote-v1 and config_version stay digest-bound locally, never added to the wire runner. Native host-native-chatgpt provider is internal. Actual existing ServerConfig mappings are the admission allowlist; requests cannot add models. All four current captured pilot libraries remain paused; adopted TB/Science reward1 does not enable claims. An unpublished CLI candidate cannot be described as released runtime proof.

Install the reviewed CLI with its host-codex extra in the actual invoking Python interpreter: websockets==15.0.1 is required. Missing/wrong dependency blocks before run:create; no automatic package installation or alternate interpreter fallback. Isolated wheel unit tests reuse verified httpx/cryptography dependencies; the actual no-model helper used the already installed Pier Python3.13.2 with websockets15.0.1. This is not proof of a future DS0 or007 installation.

New V2 claims use system allocation: choose the legal library/model/effort and plan limits, then run. No webpage cell click, task_id, cell_id or click-created identifier is required before claim; cells remain a score/progress display. Preserve historical cell records.

Latest allocation contract: the user selects the library. The launcher supplies the already approved effective Harness/model/effort and total/concurrency context, or CLI reuses its original saved configuration. Missing context blocks with a precise missing-plan message; do not ask for cell clicks, silently choose Low, invent a model or budget, or copy a different account plan. The advanced select command configures a plan and creates no lease. New run asks only for a missing library once the approved context is complete.
Within that exact scope Server assigns never-run tasks first, then fewer-run tasks to balance coverage and repetitions. Historical attempts, repeated attempts and scoring cooldown must not deny a new claim; all tasks having run does not exhaust eligibility. In-flight counts are only a lightweight distribution signal. Keep ownership/idempotency/resource safety/maintenance and existing plan limits. A fresh valid attempt does not permit duplicate settlement of an old result. No client-side cooldown waiting or historical result pruning. The webpage grid is counts/recent-three red-green/hover results display, not admission. Server019 owns allocation/cooldown delta; actual new Server runtime binding remains a separate fixed candidate acceptance.

Server020 recovery boundary: once repeated same-account cell execution rows exist, Server018/019 binaries must not directly reopen that DB because they recreate removed UNIQUE indexes. Keep all execution/result/accounting rows. Recovery belongs to sole deployer007 using021-schema-compatible recovery-only or a same-schema forward fix; routing to an old service still preserves new uploads and settlement. CLI stop/upload-only retains original ownership/idempotency; do not delete repeat rows, overwrite journals, rerun models or describe old-binary replacement as safe rollback. Allocation metadata is balanced-repeatable-tasks-v1: unrun, then fewest_finished_starts, then fewer_inflight; allow_repeats=true, cooldown_blocks_claim=false, inflight_is_question_limit=false.

Server021 deferred contribution policy: bootstrap must explicitly declare missing_reward_basis=defer, points_until_basis_resolved=null, deferred_state=deferred_reward_missing_basis and known_basis_policy=existing_codex_frozen_reward before a new bound host run. Missing basis does not itself block otherwise authorized claim/start/upload/grading; no new client write fields or guessed points are sent. CLI status preserves actual grading.state separately, reads actual points only from grading.earned_points, and displays deferred points as null with 待结算. assignment.contribution describes contract basis only; a quoted basis is not settled earned points. A valid settled zero remains zero; missing/unknown values are never replaced with zero. After defer receipts exist, old020 must not process their uploads/accounting;007 preserves receipts/results/points and uses021-compatible recovery. Final TB/Science lists remain separate; this catalog is still15/16/1/1 pilot.

本轮最终固定服务端为 Server023（保留 Server021 待结算协议与 catalog020 试点）。题格与榜单公开 `statistics_policy`：新结果的 DRadar 时长、Token、吞吐、相似度提示仅供审计；旧隔离记录不自动重算或解除，新旧统计人口不能宣称完全同口径。官方 verifier、网络及轨迹硬规则保持；判分 reward=0 仍是未通过。CLI status 分开保留实际 reward/score/passed、判分状态与积分结算状态，不从积分数值推断通过。服务端源码与 wheel 的固定摘要见本次 SERVER023_BINDING.json；bootstrap 不提供这两个摘要，不能把策略字段核对说成远端安装摘要验证。

Server023 最终新字段契约：公开 statistics_policy 使用 dradar-product-signals-audit-v2；新增轨迹工具名、URL、超时文本的自产网络猜测提示仅审计。实际网络隔离、认证/预算/身份/产物摘要、缺完整轨迹与任务 hash 不匹配、可信 verifier 的任何 flags（含同名 network）仍保留；旧隔离不重算。费用投影保留 actual_cost_usd、cost_complete、cost_basis、token_pricing_version、tariff_sha256；只有服务端验证完整 usage/cache-write/request ledger，且配置明确同模型 requires_cache_write_usage 价行及版本/hash后，cost_basis 才是 official_standard_api_equivalent，展示为“API等价值”，不能称订阅实付。证据或配置缺失时这些值维持 NULL/false，当前真实 host 三类 terminal tokens 不足，真实费用仍未知；不猜缓存写入、不扩 collector，也不因此阻断判分/待结算或其他已获权流程。具体价格矩阵差异按 Server023 的 MATRIX_MERGE_DELTA023.json 由实际配置主责核真实来源接，不提供 CLI 数值 override。
