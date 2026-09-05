# #0057 A方案：完整本地交付边界

状态：本地实现和测试完成候选；没有生产发布授权。CLI运行时版本0.5.192，独立bootstrap包0.1.0。原0.5.191候选和旧bootstrap资产保留。

## 同一份复制说明的实际链路

1. 助手说明下载、目录写入、Docker/容器、模型额度及上传副作用，自动选择本任务专用的绝对路径。不新增网页阶段，也不让用户手工敲安装命令；uv或新系统权限缺失时才询问。
2. 仅在目录不存在时创建专用venv，在其中写入复制说明给出的固定URL和SHA-256锁行。使用`uv pip install --require-hashes --reinstall-package dradar-bootstrap --no-index --only-binary :all:`安装；不因已有同名包或URL hash片段而跳过校验。安装失败停止，不运行包。
3. 使用该环境的Python，以`uv run ... python -I -m dradar_bootstrap`启动固定模块，避免工作目录或PYTHONPATH遮蔽已核验包。
4. 已加载bootstrap只构造固定仓库、固定revision、同一server/plan和有限确认参数的内层uvx调用。Git解析失败最多在新的UV_CACHE_DIR中重试一次，不改全局缓存。
5. 运行时完成来源核对和导入后，在私有临时目录写入nonce+revision绑定回执；之后才进入本地持续流程。回执不证明Docker就绪或任务完成。
6. CLI内部直接管理运行、进度、补交和有界重查。非TTY普通确认返回非秘密编号/有限选项，助手取得明确答案后只续跑同一个固定入口；秘密登录保留用户终端边界。

## 失败和上报边界

- uv/Python/启动包尚未取得、安装失败或哈希不符：bootstrap尚未运行，只能提供本地脱敏诊断，不声称自动上报。
- bootstrap已加载，内层Git仍失败：保留单次隔离重试，随后一次最小故障上报；只在专用头携带运行码，队列无凭据、命令、路径或原始输出。
- 已收到运行时加载回执：确认、取消、运行失败或观察结束不是bootstrap加载失败，不触发重复启动/错误上报。
- 流式转发使用短读，短进度行不等到进程结束；运行码跨输出块也会脱敏。

## 可复核源码和测试

- `release/bootstrap/dradar_bootstrap.py`：固定参数适配、流式转发、重试与最小上报。
- `src/dradar/bootstrap_receipt.py`、`session_entry.py`：私有加载回执和固定来源运行时入口。
- `src/dradar/run_session.py`、`run_plans.py`：本地持续操作、非TTY确认及既有边界复用。
- `scripts/build_bootstrap.py`：无第三方运行依赖的确定性wheel构建，不上传、不覆盖已有产物。
- `scripts/qa_bootstrap_local.py --web-source <Web/index.html>`：读取真实复制组件的安装配方和固定入口，在loopback服务及专用目录中实际安装/执行。
- `tests/test_bootstrap_package.py`、`test_session_entry.py`、`test_run_session.py`：结构、隐私、确认和边界回归。
- Web `tests/session-copy-entry.test.js`：真实复制、再次复制和手动复制函数，中英文及八类Harness；准备和启动处于同一说明。

安装/进程验证真实执行了uv强制哈希安装、已安装bootstrap进程、私有回执、流式输出和CLI控制代码；内层Git/安装元数据、API/Fleet/Docker仍是明确夹具。场景包括错误哈希、正确安装、已有同版本时错误哈希重装拒绝、隔离模块入口、Git一次失败成功、两次失败一次上报、非TTY确认/取消/重复拒绝和即时短行。macOS带空格路径已执行。

## 待发布的精确资源（仅候选目标）

- 源仓库：codex-radar/dradar，正常审查本次代码后确定最终CLI Git提交；CLI/OTA发布仍需授权并重新核对stable版本/sequence、六平台制品及签名。
- 唯一新增静态分发目标：现有codex-radar/dradar-web Pages项目`dradar-web`的`assets/bootstrap/dradar_bootstrap-0.1.0-py3-none-any.whl`，拟公开URL为`https://deng.codexradar.com/assets/bootstrap/dradar_bootstrap-0.1.0-py3-none-any.whl`。
- 没有创建或上传这个Pages资产。安装锁行和固定SHA来自本地候选构建；生产前应随审查后的Web版本同步核对。未新增PyPI账户、域名、Server端点或云服务。
- 现有公开API bootstrap-v1脚本和旧URL保持不变。新bootstrap继续使用现有`/api/v1/runner/failures`报告契约，不改Server/DB/账号/租约。
- 生产操作前核对既有Pages账户配置、唯一发布者/锁及实际UV User-Agent的边缘访问；未经授权不合入触发部署的Web、不触发ota-production。

## 回滚和剩余验收

当前未发布，取消候选只需继续使用现状，不删除旧资产或用户环境。发布后保留不可变旧版本URL；需要回退Web时使用已验证且CLI契约兼容的版本，不覆盖新旧资产。OTA不能把旧版本/sequence写回current，按既有单调版本流程发布修复或暂停方案。

尚未验证Linux/Windows原生安装/进程、Windows临时目录ACL、全新机器Python下载、正式Pages资产边缘、真实Git安装和Claude实际接受。通用wheel及纯夹具通过不能替代这些结果；真实模型/题目仍是零，原用户材料缺口见SESSION_ENTRY_QA.md。
