# DRadar Harness 接入最佳实践指南

本文给出一个新模型运行 Harness 从调查、认证、Pier 执行、服务端验签到前端格子和上线
验证的完整方法。它来自 Claude Code 订阅 Harness 的实际接入，不绑定某一家模型厂商。

相关安全基线见 [Honey 容器内完整权限与容器外隔离契约](HONEY_EXECUTION_SECURITY.md)。

## 1. 先冻结可验证的运行合同

不要从旧代码、模型别名或营销名称反推合同。发生冲突时按以下优先级裁决：

1. 当前待发布的官方 CLI 实际输出，包括 `--version`、`--help`、认证状态和结构化事件；
2. 厂商最新官方文档、发行说明和官方安装渠道；
3. 仓库中日期更新且与现行架构一致的设计文档；
4. 旧配置、旧站点和旧实现，仅作为迁移线索，不作为事实来源。

在无 API Key 的干净环境中，用目标订阅账号逐一探测候选模型与 reasoning/effort。记录实际
解析后的模型 ID、档位、CLI 精确版本、认证类型、输出 schema 和失败类型。只有所有目标格
都成功后，才能冻结矩阵。

本次冻结出的 Claude 合同是：

| 字段 | 值 |
| --- | --- |
| Harness | `claude-code` |
| Provider | `anthropic-subscription` |
| CLI | Sonnet 5 / Opus 5 历史格子 `2.1.251`；Opus 5.5 `2.1.280` |
| 模型 | `claude-sonnet-5`、`claude-opus-5`、`claude-opus-5-5` |
| effort | `low`、`medium`、`high`、`xhigh`、`max` |
| 认证 | Claude.ai subscription OAuth |
| 初始发布 | 十五格全部 `manual_only` |

模型别名可以继续变化，服务端和客户端合同必须使用探测到的精确模型 ID。若官方文档与当前
账号能力不一致，应保守关闭对应格子，不能用别名 fallback 伪装成功。

## 2. 把认证类型写进产品合同

“支持某模型”不等于“允许所有认证方式”。订阅 Harness 与 API Harness 必须是不同的
provider，不能靠运行时猜测。

订阅 OAuth 的最低要求：

- setup 命令只调用厂商官方 OAuth 流程，并明确拒绝 API Key、云厂商凭证和自定义 Base URL；
- 凭证落在 provider 专用目录，必须是普通文件、非链接、POSIX `0600`；
- 凭证不进入 argv、配置 JSON、提示词、任务目录、patch、trajectory、checkpoint 或 HTTP；
- 宿主环境里的同名 API/OAuth 变量在进入 Pier 前统一清除，防止认证漂移；
- `status --live` 只做最小模型探测，输出认证类型和可用性，不输出 token、邮箱或原始响应；
- OAuth 更新可能改变实际订阅档位，重新登录后必须重新读取状态，不能沿用登录前缓存。

Claude Code 的普通网页登录适合确认账号和模型权限；隔离 Linux 容器则使用官方
`claude setup-token` 生成的订阅 OAuth。DRadar 只在一次性 provider 进程中提供该值，并在
任务结束、取消和异常路径清理临时状态。因为官方客户端当前需要进程级 OAuth 接口，接入
测试必须额外扫描工具输出、环境回显、日志和 trajectory，证明凭证没有逃逸。

交互式 OAuth 还有两个容易漏掉的操作边界：

- `setup-token` 可能被伪终端按列宽硬换行；不要抓取“最后一行”作为 token。应先把终端列宽
  调大，按官方输出边界完整重组，再校验格式和长度，最后必须用最小真实请求验证；
- 浏览器返回的一次性授权 code 应直接输入仍在等待的官方 CLI，不得粘贴到聊天、工单、
  shell history 或日志。若发生误贴，先消费或吊销该 code，并确认长期 setup-token 未泄露。

Claude Code 当前以 `CLAUDE_CODE_OAUTH_TOKEN` 接收长期 OAuth。进入 provider 进程前应清除
`ANTHROPIC_API_KEY`、云厂商认证和自定义 Base URL 等更高优先级或会改变路由的变量，避免
“网页登录看似正确、容器实际按 API 计费”的认证漂移。

## 3. Pier 适配器只做边界，不改模型能力

适配器的职责是固定版本、注入最小认证、建立隔离和还原可审计结果；不应通过私有工具
白名单改变模型能力。

推荐组合是：

- 每题独立、可销毁的 Docker 容器；不挂 Docker socket、宿主 HOME、SSH agent 或云凭证；
- 容器内使用官方无人值守完整权限，文件、shell、测试、补丁和原生子代理正常可用；
- 网络默认拒绝，仅开放官方安装和 provider 请求所需的精确域名；
- 为 CLI 创建隔离 HOME/config，不继承宿主 skills、MCP、hooks、plugins 或个人指令；
- 若 CLI 提供 safe mode，确认它只禁用外来定制，不会关闭内置编码工具或权限模式；
- 固定 CLI 版本和运行配置版本；任一行为变化都要递增 capability/runtime tuple；
- 不只看退出码，同时校验终态、非空响应、workspace diff、Patch 和 token usage。

Claude Code 采用 `bypassPermissions` 保证容器内完整编码权限，并启用 safe mode 隔离所有
外来定制。`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` 用于减少与评测无关的流量，但不能
替代外层网络白名单。

## 4. 客户端必须做三层失败关闭

客户端在上报能力、启动任务和提交结果三个时点都要验证合同：

1. **能力层**：CLI 版本、私有 OAuth 文件、适配器和本地依赖全部满足才上报 capability；
2. **执行层**：provider、模型、effort、runtime profile 任一不匹配，在付费请求前停止；
3. **提交层**：附带精确 attestation 和真实 usage，缺失或不可信的证据不能进入正式统计。

一次运行至少上报：客户端版本、Harness/provider、官方 CLI 版本、模型、effort、认证模式、
运行配置版本、容器安全 profile、子代理策略、并发协调方式和外来定制隔离状态。服务端应
验签这些字段，不能只相信一个布尔 capability。

## 5. 服务端是格子和统计口径的权威源

服务端配置应显式列出每个 `(model, effort)`，并校验：

- provider 与 Harness 一一对应；
- 模型、effort 和 CLI 版本必须在冻结集合中；
- 初始格全部 `manual_only`，不会被默认 `/suggest` 或自动补题误领；
- 领取请求显式携带 Harness，混入其他 Harness 的格子返回冲突；
- 每个 Harness 使用独立 personal queue scope，不能让错误 runner 消费；
- submission 根据不可变的 provider/model 恢复 Harness，再执行专用 attestation；
- 验签失败时延后积分并保留证据，不能静默按普通 Codex 结算。

新增 Harness 时要全文搜索所有枚举、Pydantic `Literal`、排序表、SQL CASE、推荐过滤、持续
补题、队列 scope、公开可见性、成本白名单和提交分支。只改主配置通常会留下跨 Harness
认领或错误归类。

## 6. 前端格子设计以服务端库存为准

Harness 是一级选择，模型是卡片，effort 是卡片内的格子。不要把两个模型合成一张“系列”
卡，也不要为服务端没有发布的组合生成空壳格。

Claude 前端有三张卡片：Claude Sonnet 5、Claude Opus 5 和 Claude Opus 5.5；每张卡都按一致顺序
展示五个 effort。实现要满足：

- 只有服务端广告且客户端 capability 可满足的格子可领取；
- 三张卡共享布局、状态色、历史曲线和 token/价格语义，不共享样本；
- Harness 选择高亮本组卡片，但不隐藏其他模型的对比信息；
- 尚未判分、排队、运行、失败、冷却和已结算使用全站统一视觉语言；
- 文案明确“订阅额度”与“API 等价美元”的区别；
- 新 Harness 在主站中呈现，删除或关闭旧域名、旧主题和旧独立配置的触发入口。

格子布局不能只做字符串或 DOM 存在性测试。桌面端应实测卡片的 `x/y/width/height`，确认
上下行列线一致。若全站使用六列网格、标准模型卡跨两列，新模型卡也必须跨两列并保持统一
高度。两张同族卡片可用一个独立三列 family 容器锁定为“卡、卡、补位”；第三格没有模型时，
条件渲染与页面主题一致的插画，移动端隐藏插画并退化为单列。这样不会被其他 `order` 或
`grid-row: span` 元素占位后挤到下一行。

前端至少有契约测试证明卡片数、模型 ID、每卡 effort 集合、claim payload、capability header、
benchmark 支持范围和旧入口退役。还应对内联脚本做一次独立语法解析。

## 7. 用量与价格绝不使用语义不明的 fallback

订阅 Harness 的 provider 实际扣款通常不可由单次请求还原，因此必须分开记录：

- **真实用量**：只接受当前运行的完整 input、cache read/creation 和 output token；
- **API 等价成本**：服务端按当前版本化官方价目重新计算，并明确标成比较值；
- **订阅实际扣款**：没有可核验证据时显示缺失，不得拿 API 等价成本冒充；
- **认领估价**：只用于调度或 UI 预期，不能进入实际成本榜单。

上游 CLI 回传的 `total_cost_usd` 可能按 API 价格计算，也可能为空。订阅 Harness 不应直接
把它当 provider spend；服务端以真实 token 重算，价格版本、观察时间、生效时间、来源和
计算 basis 必须随矩阵一同发布。

Provider 用量旁车只能是缓存，不能成为唯一证据。运行器应保留并上传原始 provider
trajectory；上传与 `retry-upload` 先读取严格校验的旁车，旁车缺失时再从同一份 trajectory
按认领模型重建逐请求账本。只有模型一致、每条计数非负、cache 不超过 input、逐请求之和与
终态总计完全相等、时间戳完整时，才能标记 complete。通用 Codex session bundle 不得覆盖
或降级一份已完整对账的 Claude Code ATIF 用量。旁车和 trajectory 都不能通过校验时继续
失败关闭，token、价格和榜单资格保持缺失。

容器后处理写出的 trajectory 属于跨进程产物。“路径存在”不等于“内容已经稳定”：部分
Docker 文件系统可能先暴露目录项，再补齐最终 JSON 字节。读取端应在短且有界的窗口内只对
I/O/JSON 截断错误重试；一旦读到完整 JSON 但模型或总计不一致，立即失败关闭，不能继续等待
并期待事实改变。生产 metadata 应记录最终采用的是 provider sidecar 还是 uploaded
trajectory，便于金丝雀直接确认权威证据路径。

## 8. 测试分层与金丝雀门禁

合并前至少完成：

1. 单元测试：凭证权限、API Key 拒绝、模型/effort/version 校验、环境清理和 attestation；
2. 合同测试：CLI capability 与服务端要求完全一致，十个格子的集合相等；
3. 队列/API 测试：领取、推荐、补题、恢复和提交都保持 Harness 隔离；
4. 前端测试：两张卡、每卡五格、claim payload、服务端库存过滤和脚本语法；
5. 安全测试：argv、进程/工具输出、日志、patch、trajectory、checkpoint、上传体均无凭证；
6. 真实本地金丝雀：读写文件、运行 shell/测试、生成非空 Patch、产生可核验 usage；
7. 端到端金丝雀：服务端领取、Pier 执行、上传、独立判分、成本重算和凭证清理全链通过。

真实 Pier 金丝雀应优先调用产品的高层 runner，而不是手拼底层 `pier run`。高层入口负责
工作目录、模块路径、网络边界、overlay、超时、环境清理和产物收集；漏掉其中任何一个参数
造成的失败，都不能误判成 Harness 缺陷。首次运行还可能主要耗在基础镜像拉取和构建，应把
build、provider 调用和结果收集分别打心跳与超时，避免把“仍在冷构建”误报为模型无响应。

十个模型/effort 的最小纯文本探测只能证明账号和 CLI 支持矩阵，不能替代真实代码题金丝雀。
没有 provider 专用 OAuth 或 Docker 时，应把状态明确标为“实现与合同测试完成，真实 Pier
金丝雀待执行”，不能报告已经上线。

## 9. 发布顺序、观测和回滚

推荐顺序是：服务端先兼容新 capability（但格子保持不可自动领取）→ 发布客户端 → 发布
前端入口 → 手动金丝雀 → 小并发 manual-only → 观察稳定后再决定是否自动推荐。

每一步都要记录基线提交和回滚点。发布前比较文件清单、构建产物、配置格子数与价格矩阵；
发布后验证正式 API、两张模型卡、十个格子、claim header、判分和榜单金额。回滚应关闭或
隐藏新格子并恢复上一生产构建，不能 force push、整目录覆盖或删除仍在途的任务证据。

生产启动探针要覆盖冷缓存，而不只是热启动。若进程存在、数据库健康但重接口仍在预热，探针
应在既定总时限内重试，不能用过短的固定次数把健康候选误判为失败并反复重启。新 Harness 的
一次性 run-plan 兑换也应使用独立、与认领频率相符的限流桶；复用注册账号的低频桶会让真实
网页金丝雀在模型启动前被 429 拒绝。

以下任一情况必须停止扩量：认证类型漂移、CLI 输出 schema 变化、模型 ID/effort 不再可用、
usage 不完整、验签失败、凭证扫描命中、跨 Harness 队列混用、空 Patch 假成功或成本语义不明。

## 10. 本次实践得到的关键经验

- 先更新并重新 OAuth，再探测模型矩阵，可以及时发现订阅类型和模型解析发生变化；
- 使用真实 CLI 解析后的模型 ID，避免把文档别名固化进服务端合同；
- safe mode 与完整权限并不冲突：前者隔离定制，后者控制内置工具审批；
- 新 Harness 最容易遗漏的不是模型配置，而是队列 scope、推荐过滤和旧站点分支；
- 两张模型卡必须由服务端实际库存生成，不能靠前端静态数组制造“看起来存在”的格子；
- 两卡一画应放在同一个三列 family 容器，并用浏览器尺寸测量和截图验收真实排版；
- 订阅账号的比较成本只能从真实 token 由服务端重算，不能使用模糊 fallback；
- Provider 用量旁车丢失时必须从原始 trajectory 严格复算，不能回退到语义不同的通用 bundle；
- 前端成本分支必须保留当前 Harness 身份，不能因为复用组件而落入另一个 provider 的分支；
- 伪终端会硬换行长期 token；宽终端、完整重组、最小 live probe 三步缺一不可；
- 真实金丝雀从高层 runner 进入，冷镜像构建与 provider 执行要分阶段观测；
- run-plan 兑换限流与冷启动探针都属于 Harness 上线合同，不能只在单元测试环境验证；
- “十格调用成功”是合同探测，“完成一题并通过独立判分”才是 Harness 金丝雀。

Claude Code 的官方入口包括 [CLI reference](https://code.claude.com/docs/en/cli-usage)、
[authentication](https://code.claude.com/docs/en/authentication)、
[configuration](https://code.claude.com/docs/en/configuration) 和
[environment variables](https://code.claude.com/docs/en/env-vars)。接入时应重新核验最新页面与
当前 CLI 输出；本文中的精确版本和模型矩阵只描述本次冻结合同。
