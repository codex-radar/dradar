# DRadar CLI OTA 架构（#0012 本地集成候选）

发布方的签名、R2 不可变对象、权限和回滚合同见
[`OTA_RELEASE_CENTER.md`](OTA_RELEASE_CENTER.md)。

## 安全目标与边界

OTA 只能提升“下一次安全启动”的版本，不能中断当前 assignment、Pier、Provider 会话、上传、ledger 或历史 checkpoint 写入。不存在 `force release` 或“到点强切”路径。Manifest 不可读取、验签失败、摘要不符、协议不兼容、灰度暂停、未知平台、候选自检失败时都失败关闭；当前版本继续工作。

本候选不接生产服务、不发布真实制品。它提供 `dradar update status|doctor|prepare`，并把 #0011 的统一 event envelope、稳定 `client_id`、`request_id` 关联和离线事件落库接入 `UpdateRuntime`；OTA 不生成第二套身份或飞行记录格式。任意异常文本、版本号和 release 名都不会写入飞行记录，只保留白名单状态、单调 sequence 与 release 名的不可逆摘要关联 ID。

## 稳定 launcher 与磁盘布局

launcher 是小型、低变更、独立发布的入口，永远不由正在更新的业务进程覆盖。建议每台主机使用一个权限收紧的 OTA 根目录：

```text
ota-root/
  launcher/                 # 稳定入口；单独签名和升级策略
  releases/<release-id>/    # 不可变候选与已提交版本
  current.json              # 原子指向下一次启动使用的版本
  last-known-good.json      # 仅在候选通过自检后推进
  pending.json              # 候选、previous 指针、activation_attempted
  update-state.json         # 当前状态机 checkpoint
  update.lock               # 全主机唯一更新写锁
```

launcher 启动时先执行 crash recovery：只要 `pending.activation_attempted=true`、`current` 已指向候选而候选尚未 committed，就恢复 `previous`。每个已提交版本还包含不可变且不得为 symlink 的 `release-record.json`，其中保存精确 pointer 与原始签名 Manifest；launcher 每次启动都用受信公钥重新验签，并核对 release/version/sequence、固定制品路径、size 与 SHA-256。`current` 与 LKG 都有效但不同步时选择最高有效 sequence；相同最高 sequence 却指向不同内容则失败关闭。仅有一个根内普通文件、伪造 pointer 或未签名 LKG 不构成可启动版本，也不能成为防回滚基线。首个 OTA 可从 sequence 0 的包内旧版桥接：只有本机完全没有可信 pointer 时才允许准备首个签名候选；其 previous 是专用 `legacy_fallback` 标记，只表示继续运行包内旧版，绝不作为可信 pointer 或防回滚基线。首个候选提交后，后续更新必须具有完整 signed release record。

## Manifest、供应链与防回滚

Manifest 使用 canonical JSON（移除 `signature` 后按 key 排序、无多余空格）和 Ed25519。原始 JSON 限制为 48 KiB，所有层级拒绝重复键，顶层、签名、灰度、兼容范围和 artifact 都使用封闭 schema；不能利用 JSON last-wins 语义制造验签歧义。受信公钥由 launcher 内置/受控轮换；未知 `key_id`、非 Ed25519、签名不符立即拒绝。签名内容包括：

- `release_id`、严格 SemVer、单调 `sequence`、带时区的发布时间、到期时间与 channel；
- rollout stage、万分比、salt 与全局 pause；
- launcher、runner protocol、doctor、Provider、ledger、checkpoint 兼容范围；
- 每个 OS/架构唯一的 HTTPS URL、文件名、长度与 SHA-256。

候选必须仍在签名有效期内，并同时满足 `sequence > last_committed_sequence` 和 `version > current_version`。防回滚基线从本机 durable LKG/current 指针读取，但每个候选基线也必须通过同一套 signed release-record、信任根、路径和制品摘要验证；调用方传入值必须与最高有效 sequence 一致，不能自行降级基线。两个有效基线具有相同最高 sequence 却指向不同签名内容时按冲突失败关闭。签名防篡改，SHA-256 绑定下载内容，单调 sequence 防止合法旧包回滚。

下载不跟随重定向；POSIX 下载目录从文件系统根开始逐层通过 `dirfd + O_NOFOLLOW` 打开，临时文件和最终 hard-link 发布都锚定同一目录 fd。发布时比较临时文件、最终名称与打开 fd 的 inode/link count，拒绝外部 hardlink。下载 API 不再把通过检查的普通 `Path` 当作安全凭据，而是返回同时持有 directory fd 与 file fd 的 `VerifiedArtifact`；即使攻击者在最后一次名称检查后替换目录项，后续 stage、自检和 commit 读取的仍是已打开、已验摘要的同一 inode。stage 使用同一能力句柄直接消费，或通过该 file fd 向新的安全 dirfd 复制；它不会重新按不可信路径打开源文件。提交前仍要求名称重新绑定该 inode，确保写入的 pointer 可由下次 launcher 重验。能力句柄采用显式所有权转移：下载成功交给 stage，stage 成功交给 controller；commit、rollback、FAILED、stage/pending 写失败以及 LKG 落盘失败的所有出口都在 `finally` 中释放。`close()` 会先原子标记已关闭并清空内部 fd，再各关闭一次，重复 close 不会误关被操作系统复用的新 fd；首次检查预置 final 失败时，尚未转移的 directory fd 也立即释放。中断、断网、目录替换、目标 symlink/hardlink 竞态或校验失败会清除 partial，且不会写出目标目录。已有同名制品只有在签名长度与摘要完全一致且 link count 为 1 时才复用，否则失败关闭，绝不覆盖不可变候选。

威胁模型覆盖 OTA 根目录或其父目录中的恶意 symlink/hardlink、同 UID 并发进程在检查与使用之间替换名称、损坏或伪造 pointer/release-record/Manifest，以及下载中断和进程崩溃。已打开 fd 保证本轮验证与消费的 inode 一致；持久化后的启动安全由内置信任根、签名 release record、固定路径、size/SHA-256 和最高 sequence 再验证保证。拥有内核/管理员权限、可读取或替换 launcher 内置信任根的攻击者不在本层防护边界内。

## 分级灰度与旧客户端桥接

rollout stage 为 `internal → canary → progressive → general`。稳定 `client_instance_id` 由 #0011 注入为 rollout subject；HMAC-SHA256(salt, subject) 给出确定性 cohort，升级重试不会跳组。服务端可把 `paused=true` 作为只读策略；客户端收到后不再推进，但不影响当前工作。

旧客户端桥接分两层：

1. 本候选把 console script 固定为 `dradar.launcher:main`；无 keys、无 Manifest、离线、pointer 损坏或执行候选失败时继续运行包内 CLI。
2. `dradar update prepare` 只接受本地普通 Manifest 文件和显式 `KEY_ID=FILE` 的原始 32-byte Ed25519 公钥；候选制品仍只能从签名 Manifest 中的 credential-free HTTPS URL 下载。generic `uvx --refresh` 不参与激活。

## 发布输入契约与本地端到端演练

发布方必须离线生成 Ed25519 签名 Manifest，并为六个唯一目标提供 zipapp：`macos/linux/windows × x86_64/arm64`。每个 zipapp 必须支持 `python candidate.pyz --version` 自检；Manifest 中的 filename、HTTPS URL、精确 byte size 和小写 SHA-256 必须与最终不可变对象一致。签名覆盖 rollout、有效期、sequence 及全部兼容合同。公钥轮换通过重复 `--trusted-key next-id=/path/to/raw.pub` 同时携带旧/新公钥，不允许从 Manifest 自行引入信任根。

```text
dradar update status --json
dradar update doctor
dradar update prepare --manifest ./release-manifest.json \
  --trusted-key release-2026=./release-2026.pub --ring internal
dradar go ...  # 当前任务自然结束且 pending upload=0 后，由父进程单飞激活
dradar update status
```

演练必须验证：旧版无状态时保持包内版本；签名候选 prepare 后在 40 个 active worker、checkout/upload、durable upload、refill 或 supervisor 未空闲时均不切换；全部归零后候选 `--version` 成功才 commit；自检失败恢复 `legacy_fallback` 或上一个 signed LKG。`update status/doctor` 不创建目录、不联网、不写锁。

## 多 worker 安全点

发现并验证候选后，调度层把 refill 从“接受新题”切为“自然排空”，但不释放 assignment、不取消任务、不关闭 Provider。所有 worker 继续完成模型、产物收集、上传、ledger/checkpoint 收尾。只有以下条件全部为真才可从 `waiting_safe_point` 进入 `activated`：

- active assignment、checkout、upload、ledger write、checkpoint write 全为 0；
- durable pending upload 为 0（有待上传结果不能换版本）；
- refill 已停止接受新题；
- worker supervisor 已确认空闲，且不会补位/respawn。

父 supervisor 使用 `UpdateController.transaction()` 持有主机 update lock，并负责汇总 worker barrier；实时安全点回调必须在该事务内重新采样，不能复用锁外旧快照。所有状态变更在未持锁时都会拒绝，子 worker 不自行切换。激活只是原子修改 `current.json`，随后由 launcher 启动候选。候选自检抛出普通异常、`KeyboardInterrupt` 或 `SystemExit` 时都先回到 LKG；中断随后继续向上抛出，不重跑或释放已有 assignment。

## 状态机与暂停/回滚

```text
detected → downloaded → verified → staged → waiting_safe_point
    ↘ pause/resume（仅激活前）                     ↓
                                              activated
                                                   ↓
                                              self_testing
                                              ↙          ↘
                                        committed     rollback_pending
                                                           ↓
                                                      rolled_back
```

激活前任一步可 `failed`；pause 会保存精确 `resume_state`。activated 后不允许“暂停悬挂”，只能完成自检或回滚。自检至少包含：候选可导入/启动、`doctor` 只读合同、runner protocol、Provider capability snapshot、ledger/checkpoint schema 可读、当前 pending upload 可读。提交顺序是：候选自检通过 → 验签并原子写入不可变 release record → state committed → LKG 推进到候选 → 清 pending。若在 state committed 后、LKG 推进前崩溃，launcher 只在重新验证 pending 中的签名 Manifest、pointer 与制品后补写 release record/LKG。

## 集成冲突键与并行边界

冲突键：`src/dradar/runloop.py`、`telemetry.py`、`refill.py`、`doctor.py`、CLI 命令注册、服务端 heartbeat/envelope schema、发布 Manifest 生成与签名、launcher 安装路径、生产 release channel。

可并行：独立 OTA core 与单测；各平台 launcher 打包验证；服务端只读 Manifest API；#0011 envelope/飞行记录；发布签名密钥的离线运维设计。合流时必须由一个集成人员在最新 `origin/main` 上重放，先确定 #0011 的 ID 字段和 event schema，再接入 `EventSink` 与 safe-point snapshot。

## 测试与发布闸门

当前单测覆盖验签/篡改、macOS/Linux/Windows × x64/ARM64 包选择、SHA/长度、断网 partial 清理、协议与 Provider/ledger/checkpoint 兼容、防回滚、灰度暂停/ring、主机锁、非法强切、40-worker safe-point blocker、候选提交、候选失败、launcher crash recovery，以及从发现更新到提交/回滚的飞行记录审计。测试同时固定了接入前 `NullEventSink` 无审计的缺口，避免仅证明接入后 happy path。

后续平台矩阵必须包含 macOS/Linux/Windows × x64/ARM64（Windows ARM64 可先明确不支持并由 Manifest 缺包失败关闭），以及真实文件占用、杀进程、磁盘满、代理断流、并发 40 worker、旧客户端桥接和 LKG 端到端恢复。任何生产集成都需要独立 QA、签名密钥演练、分级灰度、暂停开关与回滚演练；本工单不授权发布或全量下发。
