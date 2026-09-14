# 构建预检、启动错误与进度核对

多 worker 运行在创建 worker session 和 checkout 题目前，会用独立的临时
BuildKit builder 检查基础镜像访问。检查失败不应被解释为用户修改了网页选择。

## 镜像源和 Buildx 兼容

- Docker daemon 返回 `null` 或 `[]` 表示未设置镜像源，使用直连路径。
- `DRADAR_BUILDKIT_REGISTRY_MIRRORS_V1` 是父进程传给 worker 的版本化数组
  快照。它不接受 `null`、空白、畸形 JSON、非字符串元素或超过 8 个元素。
  有效镜像源保持 HTTPS、无 URL 凭据、无 query/fragment 的原有要求，并按顺序去重。
- 预检查询 `docker buildx build --help`，支持时使用 `--check`。
  旧版使用只有基础镜像 `FROM` 的临时 Dockerfile，显式指定
  `--output=type=cacheonly`，不会加载或推送镜像。
- 若帮助与实际插件能力不同，只有明确的 `unknown flag: --check` CLI 错误
  允许切换一次兼容路径。DNS、TLS、超时、鉴权、限流等真实故障不会触发该切换。
- 构建能力查询失败会明确报告，不能据此假定插件是旧版。
- 创建失败、本地 I/O 异常和中断均尝试清理本次专属 builder。查询失败不等于
  builder 已不存在。原始故障与清理故障分别保留；清理不确定时不报告成功。

Build checks 要求 Buildx 0.15.0 及以上，参见
[Docker 官方说明](https://docs.docker.com/build/checks/)。兼容预检不会修改用户的
默认 builder、镜像源、代理或并发设置，也不会运行正式模型。

## 启动诊断

预检通过结构化结果携带 `stage`、`failure_code`、`returncode` 和脱敏详情。
Fleet 将它保存在启动事件的可选 `diagnostic` 中，进度 JSON 可通过
`agent.local_runner.diagnostic` 展示。`cleanup_detail` 单独记录清理故障，
不会覆盖原始失败。用户提示不包含原始日志或凭据。

启动事件绑定 controller、batch 和 pool PID。在同一启动锁下先注册 pending
归属，再允许进程报告 ready 或 failed。首个终态保持不变；重复兜底上报不能
覆盖具体失败，旧进程不能写入新一轮运行。进程退出后还会读取一次最终事件，
避免遗漏刚写入的诊断。

如果进程退出但没有任何启动确认，报告“未收到启动确认”，请先检查当前状态。
不能据此断言用户重新选题，也不能保证一个响应丢失的请求没有在服务端生效。

## 进度与恢复

客户端保留 `agent.server_status`，结合相同 plan 的本地 Fleet 活性和同批次
待上传记录展示进度。运行进程存活只说明仍在处理任务，不代表模型已开始推理。

| 本机状态 | 行为 |
| --- | --- |
| 正在准备 | 显示准备中并继续查询 |
| 正在处理 | 显示处理中；不会透传矛盾的“已结束”提示 |
| 正在停止、孤立进程收尾、状态不确定 | 继续核对，避免重复启动或提前补交 |
| 活跃进程持有待上传结果 | 由该运行继续处理；受阻结果提示人工检查 |
| 进程已结束，结果尚未上传 | 仅进入上传恢复，不重新推理 |
| 服务端确认结束，本机无活跃进程或待上传结果 | 保留服务端最终结果 |

用户确认和服务端停止动作优先。客户端核对不会重新登记设备、checkout、扩大续领
范围或改变网页保存的累计上限。其他设备继续运行时，原有整体进度监测仍然保留。

## 开发验证

测试进程必须在导入模块之前设置独立 `DRADAR_HOME`，因为部分模块保存导入时路径。
完整测试需要 `dev` 依赖；部分 Pier 夹具还需要 `botocore`。在 group-writable
默认 umask 的系统上，应仅为测试进程使用 `umask 022`，使日志测试夹具的父目录
符合现有 host-private 检查；不要修改真实运行目录的权限来迁就测试。
