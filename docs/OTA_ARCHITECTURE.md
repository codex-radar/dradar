# DRadar CLI OTA 架构（#0012 非生产骨架）

## 安全目标与边界

OTA 只能提升“下一次安全启动”的版本，不能中断当前 assignment、Pier、Provider 会话、上传、ledger 或历史 checkpoint 写入。不存在 `force release` 或“到点强切”路径。Manifest 不可读取、验签失败、摘要不符、协议不兼容、灰度暂停、未知平台、候选自检失败时都失败关闭；当前版本继续工作。

本骨架不接生产服务、不修改 `runloop.py` / `telemetry.py` / `refill.py` / `doctor.py`，也不下载真实发布物。#0011 稳定统一 event envelope、`client_instance_id` / `session_id` / `request_id` 和事件落库后，再给 `EventSink` 增加适配器；OTA 不生成第二套身份或飞行记录格式。

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

launcher 启动时先执行 crash recovery：只要 `pending.activation_attempted=true`、`current` 已指向候选而候选尚未 committed，就恢复 `previous`。`current` 缺失、越界、符号链接或目标文件无效时才使用有效 LKG；两者都不可用则失败关闭，不从网络临时执行代码。首次安装必须由独立安装器建立 current/LKG，OTA 不承担无回滚基线的首次安装。

## Manifest、供应链与防回滚

Manifest 使用 canonical JSON（移除 `signature` 后按 key 排序、无多余空格）和 Ed25519。受信公钥由 launcher 内置/受控轮换；未知 `key_id`、非 Ed25519、签名不符立即拒绝。签名内容包括：

- `release_id`、严格 SemVer、单调 `sequence`、发布时间与 channel；
- rollout stage、万分比、salt 与全局 pause；
- launcher、runner protocol、doctor、Provider、ledger、checkpoint 兼容范围；
- 每个 OS/架构唯一的 HTTPS URL、文件名、长度与 SHA-256。

候选必须同时满足 `sequence > last_committed_sequence` 和 `version > current_version`。签名防篡改，SHA-256 绑定下载内容，单调 sequence 防止合法旧包回滚。下载不跟随重定向，使用同目录临时文件，长度与摘要通过并 `fsync` 后才 `os.replace`；断网或校验失败会清除 partial，绝不覆盖已有候选。

## 分级灰度与旧客户端桥接

rollout stage 为 `internal → canary → progressive → general`。稳定 `client_instance_id` 由 #0011 注入为 rollout subject；HMAC-SHA256(salt, subject) 给出确定性 cohort，升级重试不会跳组。服务端可把 `paused=true` 作为只读策略；客户端收到后不再推进，但不影响当前工作。

旧客户端桥接分两层：

1. 尚无 launcher 的旧客户端只收到“下一题前需要升级”的兼容提示/退出码，必须自然完成当前题后退出；不允许服务端终止进程。
2. 已安装 launcher 的客户端使用签名 Manifest 和本地状态机。generic `uvx --refresh` 原型只能作为一次性引导，不能作为最终 OTA 激活机制，因为它没有本机 LKG、主机锁和 crash recovery。

## 多 worker 安全点

发现并验证候选后，调度层把 refill 从“接受新题”切为“自然排空”，但不释放 assignment、不取消任务、不关闭 Provider。所有 worker 继续完成模型、产物收集、上传、ledger/checkpoint 收尾。只有以下条件全部为真才可从 `waiting_safe_point` 进入 `activated`：

- active assignment、checkout、upload、ledger write、checkpoint write 全为 0；
- durable pending upload 为 0（有待上传结果不能换版本）；
- refill 已停止接受新题；
- worker supervisor 已确认空闲，且不会补位/respawn。

父 supervisor 使用 `UpdateController.transaction()` 持有主机 update lock，并负责汇总 worker barrier；所有状态变更在未持锁时都会拒绝，子 worker 不自行切换。激活只是原子修改 `current.json`，随后由 launcher 启动候选。任何失败都回到 LKG，不重跑或释放已有 assignment。

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

激活前任一步可 `failed`；pause 会保存精确 `resume_state`。activated 后不允许“暂停悬挂”，只能完成自检或回滚。自检至少包含：候选可导入/启动、`doctor` 只读合同、runner protocol、Provider capability snapshot、ledger/checkpoint schema 可读、当前 pending upload 可读。提交顺序是：候选自检通过 → LKG 推进到候选 → state committed → 清 pending。

## 集成冲突键与并行边界

冲突键：`src/dradar/runloop.py`、`telemetry.py`、`refill.py`、`doctor.py`、CLI 命令注册、服务端 heartbeat/envelope schema、发布 Manifest 生成与签名、launcher 安装路径、生产 release channel。

可并行：独立 OTA core 与单测；各平台 launcher 打包验证；服务端只读 Manifest API；#0011 envelope/飞行记录；发布签名密钥的离线运维设计。合流时必须由一个集成人员在最新 `origin/main` 上重放，先确定 #0011 的 ID 字段和 event schema，再接入 `EventSink` 与 safe-point snapshot。

## 测试与发布闸门

当前单测覆盖验签/篡改、SHA/长度、断网 partial 清理、协议与 Provider/ledger/checkpoint 兼容、防回滚、灰度暂停/ring、主机锁、非法强切、safe-point blocker、候选提交、候选失败与 launcher crash recovery。

后续平台矩阵必须包含 macOS/Linux/Windows × x64/ARM64（Windows ARM64 可先明确不支持并由 Manifest 缺包失败关闭），以及真实文件占用、杀进程、磁盘满、代理断流、并发 40 worker、旧客户端桥接和 LKG 端到端恢复。任何生产集成都需要独立 QA、签名密钥演练、分级灰度、暂停开关与回滚演练；本工单不授权发布或全量下发。
