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
