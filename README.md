# dradar — 分布式雷达客户端 CLI

`dradar` 是分布式雷达的开源客户端，运行在参与者自己的电脑上。它负责连接雷达服务、
查看和领取 benchmark 任务、在隔离环境中调用本机已经登录的模型工具、保存中断恢复点、
上传结果，并查看服务端的独立判分。调度、判分和榜单由服务端完成，不在本仓库中。

CLI 的核心能力围绕通用的 benchmark 运行流程设计，包括格子、租约、worker、checkpoint、
artifact 和判分结果，不把产品定位绑定到某一个题库。当前主站首先接入 DeepSWE，并使用
Pier + Docker 作为现阶段的任务执行方案；未来可以继续接入其他 benchmark 和对应 runner。

- 官网与任务大表：[deng.codexradar.com](https://deng.codexradar.com)
- CLI 仓库：[github.com/SecurityMind/dradar](https://github.com/SecurityMind/dradar)
- 当前 CLI 版本：运行 `dradar --version` 查看

## 工作原理

```text
你的机器                                      DRadar 服务端
┌────────────────────────────────┐           ┌────────────────────────────┐
│ dradar cells / go / resume     │──查询/领题▶│ 推荐、原子领取、租约与心跳    │
│  └─ 当前 runner：Pier + Docker  │           │                            │
│      └─ 本机模型工具             │           │ 独立 verifier 重新判分       │
│  └─ checkpoint / 脱敏 / 上传    │──提交结果▶│ 积分、榜单与公开大表          │
└────────────────────────────────┘           └────────────────────────────┘
```

- **客户端结果不直接算分**：服务端使用任务自带的 verifier 重新运行 patch 并独立判分。
- **订阅凭据留在本机**：DRadar 调用本地已经登录的 Codex/Claude，不上传账号 Token、
  Codex `auth.json` 或 Claude OAuth Token。
- **上传前扫描敏感信息**：补丁新增行中的密钥会在临时副本中脱敏并验证后上传，原始补丁
  始终留在本机；无法安全脱敏的补丁会被拒绝。trajectory 等展示数据也会先脱敏。
- **领取由服务端原子裁决**：查询到的 `open` 只是快照；真正领取时服务端会再次检查，
  避免同一空位被并发重复发放。
- **Codex 容器始终使用最新稳定版**：每道 Codex 任务启动或恢复前，CLI 都会从 npm
  确认当前 `latest` 对应的精确版本号，再交给 Pier 构建。精确版本变化会使 Docker
  安装层缓存失效；本机 npm 不可达时，只接受服务端最近确认仍新鲜的精确版本。两边
  都无法确认最新版时不会启动模型，也不会消耗额度。已经运行中的任务不会被中途升级，
  下一道任务或下次恢复时再更新。

## 环境要求

- Python 3.11 或更高版本，以及 [`uv`](https://docs.astral.sh/uv/)
- Docker，推荐 macOS 使用 [OrbStack](https://orbstack.dev/)
- 本机已登录 `codex` 或 `claude` CLI，二者准备好一个即可
- 至少约 20 GB 可用磁盘；多 worker 需要更多 CPU、内存和磁盘

原生 Windows 为候选支持，需要 Docker Desktop 运行 Linux containers，并确保 Codex CLI
能直接在 PowerShell 的 `PATH` 中调用。WSL2 也可使用，不限定 Ubuntu。

```powershell
irm https://chatgpt.com/codex/install.ps1 | iex
codex login
```

## 安装与快速开始

最简单的入口是在官网用 GitHub 登录，选择一个开放格子，然后把页面生成的完整提示词粘贴
给 Codex。提示词会检查环境、安装最新版 CLI、登录并询问运行方式。

手动使用时，可以一直通过 GitHub 主线运行最新版：

```bash
uvx --from git+https://github.com/SecurityMind/dradar dradar --version
uvx --from git+https://github.com/SecurityMind/dradar dradar login \
  --server https://api.codexradar.com --token <YOUR_TOKEN>
uvx --from git+https://github.com/SecurityMind/dradar dradar doctor
```

下面的文档为了简洁统一写成 `dradar ...`。如果没有把它安装成全局命令，就在每条命令前
加上：

```bash
uvx --from git+https://github.com/SecurityMind/dradar
```

最常见的一次运行：

```bash
dradar cells --available --limit 10    # 查看开放格子，不领取
dradar go --auto 3                     # 自动选到总计 3 题，默认串行运行
dradar status                          # 查看自己的提交和判分
```

## 命令总览

| 命令 | 是否改动状态 | 用途 |
| --- | --- | --- |
| `dradar --version` | 否 | 查看当前 CLI 版本 |
| `dradar login` | 本地配置 | 保存服务端和 Token、注册新账号或通过 GitHub 恢复身份 |
| `dradar doctor` | 可能安装依赖 | 检查 Docker、Pier、Codex/Claude、任务仓库、磁盘和登录状态 |
| `dradar capacity` | 否 | 根据 Docker 资源、磁盘和账号上限推荐安全 worker 数 |
| `dradar cells` | 否 | 查看、筛选和排序完整格子表，不领取任务 |
| `dradar go` | 是 | 使用网页已领任务，或从 CLI 精确/自动领题并运行、上传 |
| `dradar resume` | 是 | 优先恢复 checkpoint，再继续当前仍持有的任务 |
| `dradar status` | 否 | 查看自己的积分、最近提交、判分、异常标记和占用摘要 |
| `dradar leases` | 否 | 查看当前持有的 assignment，区分 running 与 waiting |
| `dradar release` | 是 | 释放不再准备运行的租约；运行中任务默认受保护 |
| `dradar checkpoints` | 否 | 查看本地恢复点、阶段、更新时间和磁盘占用 |
| `dradar checkpoint discard` | 是 | 删除指定恢复点，并安全重新开放对应格子 |
| `dradar retry-upload` | 是 | 重试已经运行完成但因网络等原因没有上传的结果 |
| `dradar cleanup` | 本地删除 | 安全清理已结算的本地任务文件及 DRadar/Pier 镜像缓存 |
| `dradar config show/set` | 本地配置 | 查看或调整镜像缓存模式与容量上限，不显示账号凭据 |
| `dradar refill status` | 否 | 查看本机持续补题计划和额度预留 |
| `dradar refill stop` | 是 | 停止继续领取新题，保留已有任务和 checkpoint |
| `dradar rename` | 是 | 修改榜单昵称，积分不变 |
| `dradar link-github` | 是 | 绑定 GitHub 身份，显示头像并支持跨机器找回账号 |

任何命令都可以用 `--help` 查看当前版本的参数：

```bash
dradar --help
dradar go --help
dradar cells --help
```

## 账号与环境命令

### `dradar login`

保存服务端地址、身份 Token 和可选任务仓库位置。配置写入
`$DRADAR_HOME/config.json`，默认 `DRADAR_HOME=~/.dradar`。

```bash
# 使用官网提供的 Token
dradar login --server https://api.codexradar.com --token <YOUR_TOKEN>

# 首次自行注册昵称
dradar login --server https://api.codexradar.com --nickname alice

# 只配置服务端；第一次 go 时自动生成匿名身份
dradar login --server https://api.codexradar.com

# 在新机器上恢复已经绑定 GitHub 的身份
dradar login --server https://api.codexradar.com --github

# 把任务仓库放到自定义位置
dradar login --server https://api.codexradar.com --token <YOUR_TOKEN> \
  --tasks-root /data/benchmark/tasks
```

`--github` 只能恢复之前执行过 `dradar link-github` 的账号。新安装默认把任务仓库放到
`~/.dradar/deep-swe/tasks`。这是当前 DeepSWE 接入使用的兼容默认路径，不代表 CLI 只支持
这一种 benchmark；升级时会保留已有的自定义路径，不会偷偷搬迁或重复克隆。

### `dradar doctor`

运行完整环境体检，并给出与 macOS、Linux、WSL2 或 Windows 对应的修复建议。它会检查：

- Docker CLI、Docker daemon 和 Compose 插件；
- 与 DRadar 固定版本兼容的 Pier，缺失时会尝试安装；
- Codex CLI + `auth.json`、Claude CLI + OAuth Token，以及所选可选 provider 的本地凭据、
  固定 CLI 版本和模型访问能力；
- 当前 benchmark 的任务仓库（主站目前为 DeepSWE），缺失时会尝试克隆；
- Docker daemon 实际可用的 CPU/内存（不足时告警，不读取宿主机宣传配置）；
- 可用磁盘和服务端登录。

```bash
dradar doctor
```

### DeepSeek V4 Flash / Pro 补充 provider

DeepSeek 是 Codex 路径的可选补充，不会替换 `~/.codex/config.toml`、Codex
`auth.json`、Claude 配置，也不会让原有任务自动切换 provider。只有在网页明确选择
DeepSeek 格子时才会使用按量计费的 DeepSeek API。

在用户自己的交互式终端中配置 key（输入不会回显）：

```bash
dradar provider setup deepseek
dradar provider status deepseek --live
dradar doctor
dradar go --pick TASK_ID:deepseek-v4-flash:max
dradar go --pick TASK_ID:deepseek-v4-pro:high
```

key 保存在 `~/.dradar/secrets/deepseek_api_key`，POSIX 系统权限固定为 `0600`，
不会进入 `config.json`、命令参数、复制提示词或 DRadar 服务端。运行时 CLI 生成短期
Codex `auth.json`，通过公开 Pier 的 `CODEX_AUTH_JSON_PATH` 文件上传机制送入容器；
Pier 退出后立即删除。该本地文件存在时优先使用，避免桌面应用或 shell 遗留的旧
`DEEPSEEK_API_KEY` 静默覆盖用户刚配置的新 key；没有本地文件的自动化环境仍可临时
设置 `DEEPSEEK_API_KEY`，CLI 会先写入短期 auth 文件，再从 Pier 的继承环境中移除该变量。

DeepSeek API 价格按北京时间分段：每天 `09:00–12:00`、`14:00–18:00` 为高峰价，
其余时间为非高峰价（高峰价的 50%）。CLI 会在领取/启动前显示当前价段与三类 token
单价，并把每次 provider usage 的时间和 token 桶一并上传；服务端按请求发生时间拆分
跨价段任务并冻结账单，后续调价不会倒算已有记录。

运行前 CLI 会校验随包发布的 DeepSeek 官方 Codex `models.json` 的 SHA-256，并由一个
很窄的公开 Pier 子类把它上传到任务容器隔离的 `/tmp/codex-home/models.json`。文件缺失、
被修改或不含 `none`/`low`/`high`/`max` 上游档位时，能力不会上报、`doctor` 会失败，任务也会在发出任何
付费模型请求前终止。目录启用后，上下文、自动压缩、推理摘要、并行工具和补丁工具等
元数据均由目录决定，不再用本地 TOML 重复覆盖。

当前公开边界：

- 模型固定为 `deepseek-v4-flash` 或 `deepseek-v4-pro`，两者公开启用的产品档位都是
  `off`、`high` 和 `max`。`off` 在 Codex Responses API 链路上严格转换为
  `reasoning.effort=none`，其余两档原样传递。上游目录中的 `low` 仅为完整性校验保留，
  不可领取或运行；`medium`、`xhigh` 也不建立重复实验格。
- 使用已验证的正式版 Codex `0.147.0`、Responses API，以及官方目录声明的
  1,048,576 token 上下文和 95% 有效上下文比例。
- 基于公开 `datacurve-pier==0.3.0` 的标准 `codex` agent；附加代码只负责校验并上传
  官方模型目录，不包含 checkpoint 或任何 DRadar 私有 Pier 实现。
- 基准配置关闭 Codex apps、remote plugin 和 web search，避免无关联网探测影响隔离性。
- DeepSeek 格子只能显式领取，不进入 `/suggest`、`--auto` 或持续补题。
- 第一版公开路径不恢复 DeepSeek checkpoint；中断后重新显式领取，避免把缺少 provider
  身份的旧 Codex checkpoint 恢复到另一个计费 provider。
- 未显式领取 DeepSeek 格子时，原有 OpenAI Codex 与 Claude 行为完全不变。

配置依据：[DeepSeek 官方 Codex 集成文档](https://api-docs.deepseek.com/quick_start/agent_integrations/codex/)、
[官方安装脚本](https://cdn.deepseek.com/api-docs/codex-deepseek-setup-en.sh)、
[Responses API](https://api-docs.deepseek.com/guides/responses_api/) 和
[Codex 自定义 provider](https://developers.openai.com/codex/config-advanced/#custom-model-providers)。

### DeepSeek Harness Minimal

DSH Minimal 是与 Codex 分开的实验 Harness。它复用同一份本地 DeepSeek key，但只接受
`off`、`high`、`max` 三档，并固定使用 DSH 自带的 `minimal` 双工具组合（持久 Bash 与
字符串替换编辑器）。网页认领 DSH 格子后，仍走普通志愿者的本机流程：本机 Docker、
公开 CLI、当前账号已认领批次，再由 `resume` 启动，不需要单独安装宿主机 Pier。

```bash
dradar provider setup deepseek
dradar provider status deepseek --live
dradar doctor --agent dsh-minimal
# 回到网页认领 DSH 格子后：
dradar resume -y
```

运行器通过 `uvx --isolated` 使用公开 `datacurve-pier==0.3.0`，并在任务容器内安装固定的
`@deepseek-ai/dsh` 版本。普通 Codex、Claude、Grok 和 DeepSeek Codex 的环境检查与运行
路径不受影响。DSH 第一版不恢复 checkpoint；中断任务会按现有失效/重新认领流程处理。

### Grok 订阅 OAuth 补充 agent

Grok Build 只使用 grok.com 订阅的官方 OAuth/device login，不接受 `XAI_API_KEY`，也不接
xAI 按量 API。首次在跑题机器的交互式终端中建立 DRadar 专用会话：

```bash
dradar provider setup grok
dradar provider status grok
dradar doctor
dradar go --pick TASK_ID:grok-4.6:high
```

凭证保存在 `~/.dradar/providers/grok/auth.json`（目录 `0700`、文件 `0600`），与日常
`~/.grok` 分离。每次运行只上传一个临时副本，整个模型会话持有独占锁；官方 CLI 静默
刷新后，DRadar 校验并原子回写，再删除副本。因此同一订阅槽固定单并发，不会让两个
Pier 容器同时刷新同一个 token。

DRadar 会在宿主机上先校验官方 CLI 的固定版本，再把解析后的独立可执行文件作为只读
运行输入上传到任务容器并二次验版。Docker build 不访问 x.ai，也不会把 OAuth 凭证或
日常 `~/.grok` 目录烘焙进镜像；凭证仍只在容器启动后临时注入。

当前 canary 边界：官方 Grok CLI 固定为 `1.0.0`，模型固定为 `grok-4.6`，档位为
`low`/`medium`/`high`/`xhigh`；只能显式领取，不进入自动推荐或补题；禁用 web search、memory、
subagents 和 plan，并把容器运行时网络限制为 `auth.x.ai` 与
`cli-chat-proxy.grok.com`、`code.grok.com`。第一版不支持 checkpoint。轨迹按 ATIF-v1.7 保存，但订阅
运行没有 API 账单，因此 cost 保持未知，不伪报为 `$0`。

### Kimi Code K3 订阅 OAuth agent

Kimi Code 只使用官方订阅 OAuth，不接受 `KIMI_API_KEY`、`MOONSHOT_API_KEY` 等按量 key。
先安装官方 Kimi Code CLI `0.36.0`，然后在跑题机器自己的交互式终端中建立 DRadar 专用会话：

```bash
dradar provider setup kimi
dradar provider status kimi
dradar doctor --agent kimi-code
dradar go --pick TASK_ID:k3:high
```

DRadar 把 OAuth 保存在 `~/.dradar/providers/kimi`，不会借用或覆盖日常 Kimi Code 配置。
任务容器只收到一次运行所需的锁定副本，退出后会回收刷新状态并删除临时副本。模型固定为
`k3`，只接受 `low`/`high`/`max`；DeepSWE 与庞贝壁画均可显式领取，但不进入自动推荐、
排序或持续补题。第一版不恢复 checkpoint。

### ZCode GLM-5.3 国内 Coding Plan agent

ZCode 使用国内 BigModel Coding Plan Key，不使用海外入口。先从
[ZCode 官网](https://zcode.z.ai/cn)安装官方 ZCode `3.7.7`，再运行：

```bash
dradar provider setup zcode
dradar provider status zcode --live
dradar doctor --agent zcode
dradar go --pick TASK_ID:glm-5.3:high
```

`provider setup` 会从官方桌面安装的 `Resources/glm/zcode.cjs` 导入 CLI `0.16.3`，并在复制
前校验固定 SHA-256；高级用户也可以临时设置 `ZCODE_CLI_PATH` 指向该文件。Coding Plan Key
通过关闭回显的交互式输入保存到 `~/.dradar/secrets`，不会进入命令行、配置 JSON、Git、
DRadar 服务端或轨迹。容器启动后 Key 会立即转入 ZCode 的内存会话并删除临时文件。

模型固定为 `glm-5.3`，档位为 `low`/`high`/`max`。它只参与 DeepSWE，不会出现在庞贝壁画
认领表；只能显式领取，不进入自动推荐或补题，第一版不恢复 checkpoint。

体检失败不会领取任务。修复所有 `FAIL` 后重新运行即可。

### `dradar capacity`

只读检查本机适合的并发数，不领取任务。它使用 Docker 引擎实际可用资源，而不是宿主机
宣传配置，因为 Docker Desktop/OrbStack 可能只分到一部分资源。

```bash
dradar capacity
```

当前保守规则包括：每个 worker 至少预留 2 CPU、6 GiB 内存，Docker 额外预留 2 GiB；
第一个 worker 预留 20 GiB 磁盘，每增加一个再预留 12 GiB。普通自动推荐最多 4，最终
结果还会被账号并发上限和可运行题目数限制。检测失败时回退到 1。

`doctor` 会按同一内存/CPU预算检查单 worker；手工指定 `--workers N` 时也会在领取任务前
比较 N 与 Docker 实际资源。资源不足只告警，不会偷偷修改 Docker 配置或替用户降低显式
指定的 N，但 CPU/内存压力可能改变 agent 的重试路径和最终测量结果，建议改用
`--workers auto` 或先调整 Docker VM。

### `dradar link-github`、`rename` 与 `status`

```bash
dradar link-github          # 浏览器设备码流程，绑定 GitHub
dradar rename new-name     # 修改榜单昵称，保留积分
dradar status              # 只读查看自己的状态
```

`status` 最多展示最近 20 条提交，包括模型、effort、`pending`/`grading`/`graded`/
`error`/`invalid` 状态、通过或失败、异常标记和客户端错误摘要；还会提示待补传结果和当前
租约数量。它不会自动上传或修改任何数据。

## 查询格子：`dradar cells`

`cells` 读取与网页大表相同的公开快照，只查看、不占位。默认按积分倍率从高到低显示前
20 个格子。

```bash
dradar cells
dradar cells --available --model gpt-5.6-sol --effort high
dradar cells --available --min-multiplier 2 --sort multiplier
dradar cells --model gpt-5.5 --max-tests 2 --sort tests --reverse
dradar cells --state cooldown --task cache --sort minutes
dradar cells --available --format pick
dradar cells --available --all --json
dradar cells --available --max-minutes 15 --max-cost 2 --min-pass-rate 0.5
dradar cells --model deepseek-v4-flash --price-band peak --sort cost
```

### 格子状态

| 状态 | 含义 |
| --- | --- |
| `open` | 当前还有空位，可以尝试领取 |
| `leased` | 已被持有或为专属保留格，当前容量已满 |
| `running` | 已有人真正启动任务并持续上报心跳 |
| `queued` | 已提交并等待服务端判分，判分队列占满该格容量 |
| `cooldown` | 最近产生有效判分，处于重新开放前的冷却期 |

使用 `--available` 等价于只看 `open`；也可以重复传入 `--state` 查看多个状态。

### 输出字段

| 字段 | 含义 |
| --- | --- |
| `TASK` | 任务 ID |
| `MODEL` / `EFFORT` | 模型和推理强度 |
| `MULT` | 如果此时成功领取，预计快照的积分倍率 |
| `PRI` | 服务端推荐优先级；只有服务端实际发布优先级数据时才显示 |
| `TESTS` | 该格子的历史测试总数 |
| `PASS` | 最近滚动窗口中的通过率，不等于终身通过率 |
| `MIN` | 预计运行分钟数 |
| `COST` | 预计模型成本，仅供参考，不是订阅余额 |

### 筛选与排序参数

| 参数 | 作用 |
| --- | --- |
| `--model MODEL` | 按模型筛选；可重复或用逗号分隔 |
| `--effort EFFORT` | 按推理强度筛选；可重复或用逗号分隔 |
| `--available` | 只显示 `open` |
| `--state STATE` | 按状态筛选；可重复，不能和 `--available` 同时使用 |
| `--task TEXT` | 任务 ID 包含指定文本，不区分大小写 |
| `--min-multiplier X` | 最低积分倍率 |
| `--min-tests N` / `--max-tests N` | 历史测试数范围 |
| `--min-minutes N` / `--max-minutes N` | 预计运行分钟数范围；没有估时的数据不匹配 |
| `--min-cost USD` / `--max-cost USD` | 预计模型成本范围（美元）；没有成本估算的数据不匹配 |
| `--price-band off-peak\|peak` | DeepSeek 价签档位；默认低谷价，统一用于输出、费用筛选和排序，不改变实际结算 |
| `--min-pass-rate RATE` / `--max-pass-rate RATE` | 历史通过率范围，取值 0–1（例如 `0.5` 表示 50%）；没有通过率的数据不匹配 |
| `--min-priority N` | 最低推荐优先级；服务端没有发布该数据时明确报错 |
| `--sort FIELD` | `multiplier`、`tests`、`pass-rate`、`minutes`、`cost`、`priority`、`task`、`model`、`effort` 或 `state` |
| `--reverse` | 反转默认排序方向 |
| `--limit N` | 最多显示 N 行，默认 20 |
| `--all` | 显示全部匹配结果，不能和 `--limit` 同时使用 |
| `--json` | 输出适合脚本或 Codex 读取的 JSON |
| `--format pick` | 每行输出一条包含完整任务 ID、可直接复制的 `dradar go --pick ...` 命令 |

普通表格为了控制终端宽度会截断过长的任务 ID；需要精确认领时使用 `--format pick`，它只
输出命令、不输出表头和提示信息。`suggest_priority` 是服务端可选策略字段：当整张表都没有
这个字段时，CLI 会隐藏 `PRI`；显式按 priority 筛选或排序会报出数据不可用，而不是把缺失
数据伪装成有意义的 0。

查询与领取之间可能发生竞争：即使刚看到 `open`，也可能已被别人抢先领取。服务端会在
数据库事务中最终确认，不会启动重复任务；CLI 会收到 `409 Conflict`。精确选题会提示
未领取，自动选题会跳过冲突格继续尝试其他候选。

## 领取与运行：`dradar go`

`go` 会依次完成环境准备、补传旧结果、优先恢复本地 checkpoint、取得任务、运行 Pier、
上传 patch/trajectory/result。任务来源有三种：

### 1. 运行网页已经认领的任务

```bash
dradar go
```

如果账号已经持有任务，`go` 直接运行它们。没有任务时，普通自由选题实例会提示先去网页
选择，或改用 `--pick` / `--auto`。

### 2. 精确领取指定格子

```bash
dradar go --pick TASK:MODEL:EFFORT
dradar go \
  --pick task-a:gpt-5.6-sol:high \
  --pick task-b:gpt-5.6-terra:xhigh
```

`--pick` 可以重复，也可以在已有持有任务的基础上精确补领；已经持有或重复指定的
格子会跳过，新增领取仍受服务端账号上限约束。
指定格子被占用时只跳过该格，不会擅自换成另一道题。

### 3. 使用系统推荐自动选题

```bash
dradar go --auto        # 目标总持有数默认为 5
dradar go --auto 3      # 把当前持有批次补到总计 3 题
```

`--auto N` 的 N 是“目标总数”，不是“再领取 N 题”。它调用与网页随机推荐相同的服务端
`/api/v1/suggest`，不会在 CLI 中维护第二套推荐算法。某个候选在领取时发生 `409`，CLI
会跳过并继续尝试其余候选；达到本人持有上限时会立即停止本轮领取。

`--auto` 和 `--pick` 不能同时使用。

## 继续任务：`dradar resume`

```bash
dradar resume
dradar resume --assignment <ASSIGNMENT_ID>
```

`resume` 首先发现并恢复本地 checkpoint，再运行账号仍持有但尚未完成的任务。指定
`--assignment` 时只恢复对应 assignment，且必须使用单 worker，不能同时开启持续补题。
如果没有 checkpoint 和活动租约，它安全退出，不会重新提交已完成任务。

## `go` / `resume` 通用运行参数

| 参数 | 作用与安全边界 |
| --- | --- |
| `-y`, `--yes` | 跳过人工确认；适合自动化。不会取消服务端领取和额度上限检查 |
| `--keep` | 成功上传后保留最终本地任务目录，供调试或审计 |
| `--archive-session` | 显式选择：成功上传并清理任务目录前，把 Codex session 以私有权限归档到 `~/.dradar/history/codex-sessions/`；默认关闭 |
| `--allow-task-drift` | 显式允许本地 benchmark 版本或任务内容与服务端不一致；这类运行不可可靠比较，默认会在消耗模型额度前停止 |
| `--workers N` | 由一个父进程管理 N 个并发 worker，范围 1–32，默认 1 |
| `--workers auto` | 检测 Docker、磁盘和账号限制后选择保守并发数 |
| `--parallel` | 高级选项：允许手工启动另一个独立 DRadar 会话；隐含 `-y` |
| `--refill` | 显式开启持续自动补题；必须同时给出额度或题数硬上限 |
| `--refill-to N` | 持有/运行队列目标；传入时自动启用 `--refill`，但仍需硬上限 |
| `--max-estimated-quota-pct PCT` | 预计 7 天模型额度占用上限 |
| `--quota-tier TIER` | 额度换算档位：`plus`、`pro-5x`、`pro-20x`，默认 `plus` |
| `--max-tasks N` | 高级题数硬上限；可以低于默认内部安全上限 |

归档不会写入 Codex 自己的 `~/.codex/sessions`，因此不会混入 Codex 的会话索引。
可先用 `dradar sessions prune` 查看占用，再用 `dradar sessions prune --yes` 明确删除。

`--workers` 已经负责启动和监管子进程，不能和 `--parallel` 同时使用。父进程先统一认领，
子进程再通过服务端原子 checkout 分题，因此不会让同一 assignment 在同一批次重复运行。

## 多 worker 并发

```bash
dradar go --auto 5 --workers 3
dradar resume --workers 3
dradar resume --workers auto
```

- 默认始终是 1 worker，不改变普通用户原有行为。
- worker 共享同一台机器的 CPU、内存、磁盘和模型额度。
- 实际启动数不会超过已持有任务数、账号并发上限或用户硬上限。
- 父池运行期间若某个 worker 正常退出，而服务端随后出现新的、已持有且可立即运行的
  waiting 任务，父池会补回空槽；不会自行领取任务，也不会自动复活 paused checkpoint。
- 未传 `-y` 时，父进程会在领取任务之前确认并发数。
- Ctrl-C 或部分子进程启动失败时，父进程会停止已经启动的子进程；已上传结果、现有租约
  和 checkpoint 保留，可用 `dradar resume` 继续。
- 只有需要手工运行多个独立 CLI 进程时才使用 `--parallel`。它们仍通过服务端 checkout
  分配不同任务，但资源需要操作者自行控制。

## 持续自动补题

普通 `go` / `resume` 不会无限领取。交互模式会询问是否持续补题，默认答案为否；无人值守
运行必须显式提供停止条件。

```bash
dradar resume -y --workers 3 \
  --refill --refill-to 3 \
  --quota-tier pro-5x --max-estimated-quota-pct 15
```

- `--refill-to` 是希望持续保持的“运行中 + waiting”队列大小。
- 启动补题计划时已经手工认领的题属于初始选择批次；必须全部成功提交后，CLI 才会领取
  第一批自动题。多 worker 不会让自动题与尚未完成的初始选择题交错运行。
- 使用多个 worker 时，CLI 会把队列目标至少提高到实际 worker 数，但绝不会提高额度或
  题数硬上限。
- `--max-estimated-quota-pct` 是基于服务端成本估价的保守预算，不是订阅平台的实时余额。
- `plus`、`pro-5x`、`pro-20x` 分别按对应额度窗口换算。
- 没有可靠额度换算数据的题不会自动领取。
- 接近预算或题数上限时停止补题，让已持有队列自然排空。
- 任一任务没有正常提交时立即停止继续领题，但不会释放已有租约或删除 checkpoint。
- 本机计划通过文件锁共享；正常新一轮可以安全替换无主旧计划，正常完成或显式执行
  `refill stop` 后会清理活动计划文件。因安全条件自动停止的诊断状态可以暂留供
  `refill status` 查看，但不会阻塞新计划。手工 `--parallel` 无法证明旧计划无人使用时
  会保守拒绝覆盖。

```bash
dradar refill status    # 查看队列目标、已预留题数、额度和停止原因
dradar refill stop      # 停止继续领题；已有任务保持原样
```

补题上限以单机 `DRADAR_HOME` 为边界。不要在多台机器上各自配置同一份“全局预算”，因为
它们会各自维护独立计划。

## 租约：`leases` 与 `release`

```bash
dradar leases
```

`leases` 显示当前账号持有的所有任务、assignment ID、到期时间以及：

- `waiting`：已认领但尚未真正启动；
- `running`：已经执行 started 流程，服务端认为正在运行。

释放方式：

```bash
dradar release                              # 数字菜单交互选择
dradar release <ASSIGNMENT_ID>              # 释放指定任务
dradar release <ID1> <ID2>                  # 一次释放多个任务
dradar release --all                        # 释放所有 waiting，保护 running
dradar release <ASSIGNMENT_ID> --force      # 强制释放卡死的 running
dradar release --all --force -y             # 高风险：无确认释放全部
```

默认不会释放 `running`。只有确认本地 Pier/Codex 已经停止、服务端状态仍卡住时才使用
`--force`，否则任务可能仍在消耗额度，却被重新开放给其他人。

## checkpoint 与中断续跑

Pier 在任务运行期间约每 30 秒把工作区差异、未跟踪文件、Codex session ID、阶段和心跳
写入 `~/.dradar/work/jobs/`。模型容量不足、WebSocket/TLS 断开、代理抖动、CLI 退出或
机器重启后，下一次 `go` / `resume` 会先尝试恢复，而不是从头运行。

```bash
dradar checkpoints
dradar resume
dradar resume --assignment <ASSIGNMENT_ID>
dradar checkpoint discard <CHECKPOINT_ID_OR_ASSIGNMENT_ID>
```

恢复优先级：

1. 原工作区 + 原 Codex session；
2. 原 session 不可用时，保留工作区，用进度摘要启动新 session；
3. checkpoint 损坏、超过 7 天、版本不兼容或租约失效时，安全标记无效并重新开放格子。

同一 assignment 使用本地文件锁，健康运行中的任务不会被第二个 `resume` 重复启动。超级
账号批量运行时，每个 worker 独立认领 checkpoint，一个损坏项不会阻塞整个批次。

checkpoint 不保存账号 Token、assignment nonce 或 Codex `auth.json`。manifest 出现
敏感字段，或者 session 检测到凭据形态内容时，会拒绝持久化相应敏感状态并安全降级。

`checkpoint discard` 会删除本地恢复数据；如果服务端租约仍有效，还会通过恢复协议重新
开放格子。这是明确放弃进度的操作。

## 上传补救：`dradar retry-upload`

任务已经运行完成，但上传因断网、TLS 或服务端临时不可用失败时，CLI 会把最新可上传
现场记录在 `~/.dradar/pending_uploads.json`。

```bash
dradar retry-upload
```

它只补传已有结果，不领取或运行新任务。每次 `go` / `resume` 启动时也会先自动执行同样
的补传。服务端按 assignment 幂等接收，已经提交的结果不会重复计分。

## 本地清理：`dradar cleanup`

```bash
dradar cleanup --dry-run
dradar cleanup
dradar cleanup -y
dradar cleanup --include-kept
dradar cleanup --docker --dry-run
dradar cleanup --docker
dradar cleanup --docker --all-task-images  # 一次性处理升级前遗留镜像
```

清理前必须成功从服务端取得当前租约列表；网络失败时什么都不删除。以下内容默认保护：

- 仍在运行或可以恢复的任务；
- 等待上传的任务；
- 使用 `--keep` 明确保留的任务。

`--dry-run` 只列出候选文件和预计释放空间。`--include-kept` 才会删除由 `--keep` 保护的
已结算目录。成功上传后，非 `--keep` 的交互运行会询问是否立即清理；无人值守运行按
安全生命周期自动回收已确认结算的副本。

Docker 镜像不会在每题结束后立刻删除。CLI 会记录本轮任务实际产生的镜像引用和 ID，
并在没有 worker 运行的批次边界按“最久未使用”顺序维护缓存。连续补领的长期 worker
pool 还会在每题成功提交后竞争一个跨进程节流标记，最多每 15 分钟由一个 worker 执行
同样的安全维护，避免池长期不退出时缓存持续增长。默认 `balanced` 模式的
动态上限为磁盘容量的 5%（最低 20 GiB、最高 50 GiB），超限后清理到上限的 75%；
可用空间低于 25 GiB 时停止领取新题，但不打断已持有任务。运行中、待补传、存在
checkpoint 或由 `--keep` 保护的镜像均不会删除。

自动清理只处理 CLI 自己记录、且镜像 ID 与 Docker Compose 标签仍一致的条目；不运行
全局 `docker image prune`，不使用强制删除，也不会碰其他 Docker 项目。升级前遗留的
Pier 镜像必须显式使用 `--all-task-images`，仍会经过标签、容器和本地恢复状态校验。
`--dry-run` 会先展示镜像列表和预计可回收空间。

Docker 的 BuildKit 构建缓存是全局共享的，无法可靠判断其中哪些记录只属于 DRadar，
因此 CLI 不会自动执行 `docker buildx prune`。删除题目镜像后保留共享构建层也能减少下次
运行的代理下载量；预计释放空间可能因共享层和 BuildKit 缓存而与 Docker 最终值不同。
若达到磁盘安全线后空间仍不足，CLI 会停止领新题并让用户在 Docker 中自行决定是否清理
全局构建缓存，而不是替用户删除其他项目的缓存。

使用计费代理或按量网络时，可以切换为节省流量模式；该模式不因缓存超限自动删除，
磁盘到达安全线时停止领新题并提示人工处理：

```bash
dradar config set image-cache-mode metered
dradar config set image-cache-limit-gb 50
dradar config set image-cache-limit-gb auto  # 恢复动态上限
dradar config show
```

## 领取冲突与常见错误

领取失败统一使用 HTTP `409 Conflict`，同时带稳定的机器错误码。CLI 的处理方式：

| 错误码 | 含义 | CLI 行为 |
| --- | --- | --- |
| `cell_unavailable` | 格子已满、冷却或对当前用户不可领取 | 精确选题跳过；自动选题继续候选 |
| `claim_limit_reached` | 本人持有任务达到上限 | 停止继续领取，先运行或释放已有任务 |
| `invalid_cell` | 模型、effort 或任务已不在当前配置 | 跳过并提示刷新格子表 |
| `run_limit_reached` | 正在运行的任务达到账号并发上限 | 保留租约，不启动超额任务 |

新 CLI 优先读取错误码；连接旧服务端时仍兼容原有错误文案。前三种领取冲突发生在创建
租约之前，因此不会启动容器或调用模型。`run_limit_reached` 针对已经持有、正准备启动的
任务：服务端保留原租约，但拒绝建立超额运行槽。

其他常见情况：

- Docker 镜像构建在 agent 启动前失败：不消耗模型额度，CLI 自动重试一次；
- CLI/Codex/Pier 中断：保留 checkpoint，使用 `dradar resume`；
- 上传失败：使用 `dradar retry-upload`；
- Token 失效：重新执行官网登录命令，或使用已绑定身份的 `login --github`；
- Ctrl-C：CLI 返回退出码 130，租约保留，可通过 `leases`、`resume` 或 `release` 处理。

## 本地文件与生命周期

默认数据目录为 `~/.dradar`，可通过 `DRADAR_HOME` 修改。

| 路径 | 内容 |
| --- | --- |
| `~/.dradar/config.json` | 服务端、Token 和任务仓库路径；私有文件 |
| `~/.dradar/deep-swe/tasks/` | 当前 DeepSWE 接入的兼容默认任务仓库路径 |
| `~/.dradar/work/jobs/` | Pier 任务目录、artifact 和 checkpoint |
| `~/.dradar/pending_uploads.json` | 待补传结果账本，不保存订阅凭据 |
| `~/.dradar/refill-plan.json` | 当前持续补题计划或最近一次安全停止诊断；不会阻塞明确的新计划 |

trial 完成后，CLI 会在对应任务目录内保存一份独立的 `model.patch` 权威副本和 SHA-256
清单，并把源路径、待上传路径、字节数和摘要写入待补传账本。`go`、`resume` 与
`retry-upload` 共用同一套校验：若待上传副本丢失但权威副本仍完整，会通过临时文件和原子
rename 自动重建；若两份文件摘要冲突，则保留两份现场并拒绝上传，不会猜测或覆盖。

提交成功或服务端确认已经提交后，CLI 会清理不再需要的副本；恢复产生新副本时删除旧副本；
无效、过期或无租约的 checkpoint 会回收。需要保留现场时使用 `--keep`，之后可用
`cleanup --include-kept` 清理。

## 心跳与隐私

`go` / `resume` 会建立轻量会话心跳：运行或上传时约 60 秒一次，准备、排队或暂停时约
120 秒一次。它按 CLI 会话上报，不按持有格子数上报。

心跳只包含 CLI 版本、粗粒度平台、阶段、当前 assignment ID、递增序号和进度计数；不
包含任务内容、prompt、trajectory、patch、命令输出、主机名、用户名、硬件详情或订阅
凭据。断网不会主动终止正在运行的 Pier。

服务端处于保守租约模式：心跳用于展示和诊断，不会仅因一次心跳中断就立即释放正在运行
的格子。用户始终可以通过 `leases` 查看，并用 `release` 明确归还。

## 平台说明

### macOS

推荐 OrbStack，也支持 Docker Desktop。OrbStack 第一次安装后通常需要打开一次 GUI 完成
初始化。如果某个 `.venv` 被 macOS 标记为 hidden，Python 可能跳过 editable 安装依赖的
`.pth` 文件；`doctor` 会识别并提示使用 `chflags -R nohidden <目录>`。

使用 Colima 时，建议建立 DRadar 专用 profile（例如先执行
`colima start --profile dradar --cpu 4 --memory 8`），再确认当前 Docker context 指向该
profile。这样可以提高任务资源而不改变其他项目正在使用的默认 Colima VM；DRadar 本身
不会创建、切换或修改任何 Colima profile。

### Windows 与 WSL2

- 原生 Windows：Docker Desktop 必须切换到 Linux containers；IDE 中的 Codex 扩展登录
  不等于 PowerShell 可以执行 `codex`。
- WSL2：Debian、Ubuntu、OpenSUSE 等普通发行版都可使用；Docker Desktop 自带的
  `docker-desktop` 是内部发行版，不能作为用户运行 DRadar 的终端环境。

## 开发

```bash
uv venv
uv pip install -q --no-deps . --python .venv/bin/python
uv pip install -q pytest httpx --python .venv/bin/python
.venv/bin/pytest tests/
```

开发测试推荐非 editable 安装。macOS 可能给 `uv` 创建的 `.venv` 设置 hidden 标志，导致
Python 跳过 editable 安装依赖的 `.pth`。非 editable 安装会把包文件真正复制到
`site-packages`；修改 `src/` 后要重新执行第一条 `uv pip install`，避免测试到旧代码。
