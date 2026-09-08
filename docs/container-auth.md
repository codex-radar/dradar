# Pier 容器认证框架

此框架位于用户版 DRadar CLI，运行在启动任务的用户电脑上。它只管理模型平台凭据进入 Pier 的生命周期，不管理雷达网页登录身份，不依赖 ds0 账号管理局，也不复制 ds0 私有 Pier 的实现。

## 生命周期

`AuthRequest → AuthRegistry.session → AuthBinding → Pier adapter → source context exit`

- `AuthRequest` 选择 harness、provider 和本次工作目录，不包含 token 内容。
- 注册的 `AuthAdapter` 负责打开已存在的本地凭据来源，并声明交付及更新能力。账号选择、凭据格式及权限检查复用现有 provider 代码。
- `AuthBinding` 校验交付合同，生成只含路径的 Pier 参数；共享挂载继续经过现有严格的目标与目录校验。
- 任务执行时，容器内的实际文件／环境变量操作与原生续签仍由已有 harness 适配器负责。
- 无论成功、异常还是取消，源上下文都执行收尾。原生 provider 上下文接收原始异常，负责共享存储验证或有效更新合并。框架只删除它为本次调用创建的临时 API Key 文件，绝不删除用户的权威凭据。

`runner.py` 现在使用相同的注册表完成认证来源获取、凭据参数生成和生命周期收尾，不再为每个平台分别分派认证来源、拼接凭据参数、手工删除临时凭据。模型版本、任务提示、网络策略和 runtime 安装仍属于 harness 执行层。

## 统一文件注入执行层

默认选择任务私有目录中的文件注入。新增 `pier_credential_delivery.py`，统一完成显式文件清单校验、宿主机只读快照、容器私有路径检查、权限设置与部分上传失败清理。它不遍历或上传用户整个配置目录，不覆盖容器中已有的凭据文件，不向日志、workspace 或任意绝对路径投递认证材料。

所有自定义适配器的文件注入分支调用 `inject_private_files()`；Codex 和 DeepSeek-Codex 的 stock Pier 上传通过窄范围代理接入，仅拦截指定认证文件，其他环境调用与产物上传直接交给原环境。所需辅助模块随每次 Pier 模块准备一同提供。

此变更只统一安全传输执行层，不新增或假装解决 refresh token 的协调／合并策略。运行期间和结束后的更新责任仍按下表执行。

共享目录与 setup-token 进程环境方式需要显式的 `compatibility_exception`。Kimi/Grok/Antigravity 在未证明独立副本的并发续签可靠之前保留共享模式；Claude setup-token 仍按官方 CLI 要求通过进程环境消费。不为了减少方式数量而把同一条 refresh token 无协调地复制给多个任务。

## 首版能力与边界

| Harness / 认证模式 | 交付 | 续签责任 | 更新保存 |
|---|---|---|---|
| Codex / OpenAI | OAuth 文件副本 | 容器内官方 CLI | 当前限容器副本 |
| Claude / 原生 OAuth JSON | 私有文件副本 | 容器内官方 CLI | 当前限容器副本 |
| Claude / setup-token | 路径入 Pier，值仅入 CLI 进程环境 | 既有会话 token 模式 | 原文件不变 |
| Kimi、Grok、Antigravity | 受管目录共享挂载 | 官方 CLI 原生协调机制 | 共享存储 |
| CodeBuddy | 私有目录副本 | 官方 CLI | provider 验证并合并较新更新 |
| ZCode / DeepSeek（Codex、DSH） | 临时 Key 文件 | 静态 Key | 收尾删除临时文件 |

能力描述不是验证结论：`native-cli` 不代表跨机器／跨容器的所有竞争已经解决；`shared-store` 不保证不同操作系统的锁或 CLI 内存缓存一致。首次抽象不改变官方 CLI 的授权方式，不发起 OAuth 或模型请求，也不宣称已经实现宿主机统一续签、容器只接收 access token 的协议。

同一条 refresh-token 链的并发协调、Codex/Claude 原生副本的持久化、运行中热更新和跨平台锁行为，仍需后续各平台单独的受控验证。不能通过复制同一份长期凭据给多个容器，就推断续签隔离正确。

## 接入新的 harness

先在执行层实现其 Pier harness，再注册一个 `AuthAdapter`，无需增加 runner 的认证分支：

1. 实现 `source_factory(request, hooks)` 上下文，返回本机选定的凭据路径；在此校验格式、归属及权限，并执行本平台更新合并策略。优先复用有效的官方本机登录；不能找到凭据时给出操作提示，不自动换到其他账号或发起浏览器授权。
2. 实现 `bind(path)`，返回 `AuthBinding`，声明路径类型、传给 Pier 的参数名、交付方式、续签责任及保存策略。
3. 注册到 `AUTH_REGISTRY`。同一 harness/provider 重复注册、绑定到其他 provider、向未注册 harness 交付凭据会被拒绝。
4. 文件注入使用公共 `inject_private_files()`；不要另写裸的凭据上传循环。需要其他交付方式时，在能力描述中填写明确的兼容理由；新增共享目标仍须审查环境安全白名单。
5. 增加启动合同、失败／取消收尾、并发持久化和敏感数据不进入参数／日志的测试。

注册项来自受审查的本地代码，不从服务器 assignment、任务文件或任意插件动态导入认证实现。雷达 server token 不属于本接口。

## 验证范围

使用模拟凭据和模拟任务执行，不使用用户真实凭据、不连接模型平台、不领取题目、不启动生产容器。本次变更需要独立 QA 后才可集成或发布；不同平台的真实长任务刷新能力另行验收。
