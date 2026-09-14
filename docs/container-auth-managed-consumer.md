# Codex 受控 AT 消费者（候选，尚未上线）

后续公共入口和协商实现见 [公共入口合同](managed-auth-public-entry.md)。本文保留 consumer 阶段边界；关于公共入口尚未实现的段落是该冻结阶段记录。

这是独立的 `codex-managed-at-v1` 运行模式，使用 Codex 0.154.0 app-server。普通 Codex/Pier 文件副本模式保持原样。服务端尚未启用此模式的派发；不能将本地测试结果解释为公共领取或线上业务已启用。

## 选择与启动

必须同时提供：

- assignment 的 `auth_runtime` 为 `codex-managed-at-v1`；
- 本地 `DRADAR_CODEX_MANAGED_CONFIG`，或 `run_trial(..., managed_auth_config=...)`；
- runner 的归属绑定回调。回调成功前不发起 `thread/start` 或 `turn/start`。

本地配置是 owner-only JSON 文件，字段必须精确为：

```json
{
  "schema": "dradar.managed_selection.v1",
  "store_root": "/absolute/private/managed-store",
  "authority_path": "/absolute/private/managed-store/authorities/STORE/auth.json",
  "executable": "/absolute/path/to/pinned/codex"
}
```

这些路径必须由用户本地选择，不接受 assignment 提供凭据路径。对应 authority 必须由 `ManagedAuthStore.login()` 的显式新登录生成并通过 custody 校验；不能导入普通 `~/.codex` 登录作为受控来源。当前宿主准入仅支持已固定摘要的 macOS arm64 Codex 0.154.0。候选尚无面向普通用户的受控登录/模式选择界面。

runner 固定容器版本为 0.154.0，不在该模式下解析 latest，也不传入完整 auth.json。缺少上述条件会拒绝，不静默回退普通来源。适配器拒绝替代认证环境变量。桥接及其 Python 依赖一起放入内容摘要命名的独立包，避免并发任务混用更新前后的文件。

## 运行与证据

宿主准备 AT 后，通过真实 Pier 文件传输写入私有代次文件；原生 RT、登录 HOME、续签候选和 pending 意图均留在宿主。容器桥接用 `account/login/start(chatgptAuthTokens)` 显式交付 AT。同一 app-server 在代次改变后接受新凭据，不通过改写 stock `codex exec` 的 auth 文件来宣称热更新。

`native_acceptance=confirmed` 仅表示原生 RPC 接受。`request_used` 在生产控制记录中保持 `unknown`：没有与请求关联的直接证据就不推断已使用。测试专用 Responses 服务根据收到的 Authorization 对应假代次记录请求使用，analytics 请求单列，不计作推理证据。保留的工作目录 `*.managed-status.json` 只包含有限状态、不可逆代次标识与上述两类证据状态，不包含 AT 正文或原生响应体。

AT 临近过期时宿主后台续签，心跳继续。原生 401 回调可请求一次针对被拒代次的续签；同代并发请求在宿主锁内重新读取，复用其他进程已更新的代次。新代次仍被拒绝则停止，不自动重跑任务。原生回调等待最多 8 秒，超过期限可能结束当前任务；宿主续签仍按自身有界流程完成或留下待恢复证据，不能撤销不确定的 RT 轮换。

固定原生实现的 proactive 窗口为五分钟。被拒 AT 在窗口内使用原生主动续签，在窗口外使用强制续签；若 RPC 期间跨过窗口边界，原生可能额外轮换一次，不能宣称任何时钟边界下都恰好一个 OAuth 请求。它们均受同一宿主锁及持久候选约束。

宿主停止时先请求 `turn/interrupt`；容器也检查变化的心跳序号，失联会终止。已发起的宿主续签不会因消费任务退出被直接取消。只复制原生 sessions 进入既有 `private_post_run` 路径，由 Pier 生成轨迹与 usage；收尾删除本次容器控制目录，保留宿主的 pending/候选恢复证据。

## 已验证与缺口

已用假凭据、无真实模型验证：

- Node 真实进程协议：无许可无模型启动、错误代次许可拒绝、两代原生接受、宿主失联/停止。
- 真实 macOS Codex 0.154.0：loopback-only 沙箱中第一代 Responses 401，第二代完成；受控宿主四进程处理同一被拒代次，假 OAuth 服务收到一次续签。
- 真实 Pier 0.3.0 DockerEnvironment、Linux arm64、`network_mode=none`：容器内部假 Responses，AT 轮换、真实 `exec_command` 写测试文件、后续请求、轨迹及 usage。传输快照不含假 RT。
- runner 启动顺序、兼容路径回归；本轮全量曾为 2127 passed / 17 skipped，随后针对容器版本输出、包隔离与状态保存继续做增量验证。

仍需独立 QA 审查当前 consumer 增量，以及完整公开 CLI/服务端能力协商和受控登录入口。以上测试不证明真实订阅接受率、各平台长期刷新能力、跨机器协调、其他 harness 的 AT 消费合同或生产评分闭环。实际 Docker 测试使用公共 Pier 的传输与解析，不是 ds0 专用 Pier；只有假 OAuth/Responses 服务，无真实账号登录、领取、模型或生产变更。
