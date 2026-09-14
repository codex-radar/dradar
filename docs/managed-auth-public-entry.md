# 受控 Codex 认证公共入口（本地候选，尚未发布）

普通使用方式不变：未选择受控模式时继续复用既有官方凭据兼容路径，不请求新的服务端能力端点，也不宣称私有锁能够协调任意官方 CLI。

## 用户入口

```sh
dradar provider codex-managed login
dradar provider codex-managed status
```

`login` 是唯一会发起新交互登录的入口。当前支持 macOS arm64，自动取得固定的官方 Codex 0.154.0 原生包，先校验完整归档 SHA512，再校验原生文件 SHA256；程序保存在 DRadar 私有目录，不更改全局 Codex 安装。可用 `--codex-bin /absolute/native/codex` 指定相同版本的已验证原生文件。命令创建全新的专用登录源和 custody 记录，成功后才选择它，不导入普通 `~/.codex` 的 RT。

`status` 只读本地信息，不续签、不查询额度、不调用模型。缺失、损坏、撤销或存在 pending 时，不将该源声明为可运行。AT 过期可以在启动前由宿主续签，但不保证提供方接受，真实接受状态仍需实际业务验证。

```sh
dradar provider codex-managed recover
dradar provider codex-managed revoke
dradar provider codex-managed use-native
```

- `recover` 只审查并前向恢复已验证候选，不调用 OAuth，不恢复旧 RT；证据不足时拒绝。
- `revoke` 是**本地撤销该受控源**：禁止新会话及后续续签，保留源、pending 和候选。它不保证在途 AT 立即失效，不等同于提供方 OAuth 撤销。选择仍指向这个不可用源，避免静默换账号；再次受控登录会建立新源。
- `use-native` 显式取消后续任务的受控选择，保留所有恢复材料，在途任务不切换。已经绑定受控 profile 的租约不会变成普通 profile；应恢复所需能力后继续，或由用户显式释放后重新领取。

默认选择保存在 `DRADAR_HOME/managed-auth/selection.json`；高级部署可使用 `DRADAR_CODEX_MANAGED_CONFIG` 指定相同严格格式的私有文件。显式路径不存在时拒绝，不回退其他账号。

## 服务端合同与持久绑定

新增默认关闭的服务端配置：

```json
{"managed_codex_auth_enabled": false}
```

本候选未修改任何生产配置。开启应经过最终 QA 和独立发布流程。

`GET /api/v1/runner/auth-runtime-capabilities` 需要现有雷达身份认证，返回 `dradar.auth-runtime.v1`。只有服务端明确启用才列出 `codex-managed-at-v1`、对应 client capability、Codex/OpenAI 与固定 0.154.0。客户端验证本地 custody/readiness 后才声明能力；在领取及受控租约的 checkout/started 前进行有界协商；读取队列后按持久 profile 检查。服务端确认属于其他 provider 的任务保留原认证路径。旧端点 404、未知 schema、未启用或版本不匹配均不继续受控操作，不自动降级。

服务端在单项及批量领取中再次校验开关、profile、客户端能力以及 harness/provider。成功时将 `auth_runtime` 和 `agent_version=0.154.0` 写入 assignment；新 nullable 列对普通旧请求保持 NULL，不增加其响应字段。批量请求的可选 profile 进入幂等指纹，未知 profile 不能释放或替换旧任务。取回、checkout 和 started 使用持久行上的能力门禁，不能因客户端改参数切换运行模式。

旧客户端连接新服务端仍可领取普通 profile；没有能力时不能启动受控 assignment。新客户端默认兼容模式连接旧服务端不探测新端点。新客户端显式受控模式连接旧服务端时拒绝发送领取 POST。服务端停用功能后拒绝受控 assignment 的新 checkout/start；不自动删除租约或凭据。

**服务器回滚边界：** 不应在仍有受控 assignment 时直接回滚到不认识 `auth_runtime` 的旧服务端代码。旧代码无法识别未来列的语义。发布/回滚计划必须先处理这些受控在途任务，再退回旧服务端；仅新增 nullable 列不能证明语义回滚安全。

## 已验证边界

本地假凭据联合测试已串起公开 login 命令、官方归档与原生文件完整性校验、真实本地服务端协商/测试租约/checkout/worker事件/started、生产 custody 与宿主原生续签、真实 Pier DockerEnvironment、第二代 AT 请求、真实工具调用及 protected rollout/usage。容器网络为 none，假 Responses 在容器内部；宿主原生 OAuth 由 sandbox 限定为一个 loopback 假服务。交互登录替换为假数据，归档来源替换为已校验本地官方包；不读取用户普通模型凭据，不执行真实模型或生产领取。

fixture 的 token/cost 是假 SSE 派生，不能算真实使用量。真实订阅、真实交互登录提供方状态、多宿主/多架构与生产评分闭环尚未验证。已有 consumer 独立 QA 通过及版本顺序修复被吸纳；本公共入口/server 增量仍需最终独立 QA。未经具体授权不做真实账户、模型、生产或发布验证。
