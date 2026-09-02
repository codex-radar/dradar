# DRadar CLI OTA 安全发布中心

本文描述 #0012 第二阶段的发布方边界。客户端验签、下载和激活安全性见
[`OTA_ARCHITECTURE.md`](OTA_ARCHITECTURE.md)。发布中心不会生成生产私钥，也不会把私钥、
R2 凭据或 Cloudflare Token 写入仓库、日志、Actions artifact 或公开对象。

## 已审查目标

- Cloudflare account：`4d94f3bcb89bc16989d5ea715eaac061`
- R2 bucket：`dradar-cli-ota-production`
- 自定义域名：`https://updates.codexradar.com`
- GitHub Environment：`ota-production`

这些名字是目标合同，不代表资源已经创建或当前工作取得了 Cloudflare/GitHub 写授权。
`r2.dev` 官方定位为限流的开发入口，不用于生产。GitHub Release 资产 URL 实测先返回 302，
而 OTA 下载器有意不跟随重定向，因此 Release 只能作为可选审计镜像，不能写入 manifest。

## 对象布局和发布顺序

不可变对象全部位于 Bucket Lock 覆盖的 `releases/` 前缀：

```text
releases/stable/s0000000001/v0.5.177/
  dradar-0.5.177-s0000000001-<commit>-<os>-<arch>.pyz
  trusted-keys.json
  release-audit.json
  SHA256SUMS
  manifest.json

channels/stable/current.json
```

发布顺序固定如下：

1. 从受保护 `main` 的精确 commit/tree 确定性构建六个平台 zipapp。
2. `publish-r2` 入口通过拒绝 symlink 的 regular-file descriptor，把 plan、manifest、registry、previous manifest、audit、SHA256SUMS 和六个 artifact 各读取一次并固化为内存 snapshot。后续发布必须用同一份 previous bytes 完成链验签、audit previous digest 和 authenticated `current.json` 精确字节比较；路径之后发生任何替换都不能改变本次决策或上传内容。首次发布必须得到 404，后续发布还必须保存 authenticated current 的 ETag。
3. 使用 `ota-production` Environment 中的 Ed25519 secret 签名；私钥派生公钥必须与已审查注册表完全一致。
4. 使用客户端同一 closed manifest/artifact 规则复验签名、schema、版本、sequence、有效期、六平台 hash/size 和前序链。发布边界重新读取 registry，要求 manifest key 仍为 `active`，并要求 `published_at` 与当前发布时点都位于当前 key 有效期内。在任何网络写之前另取一次当前时钟，强制 manifest 满足 `published_at <= now < expires_at`；上传版本对象后、提交 pointer 前必须另取新的时钟，同时重验 key 当前有效期和 manifest 半开有效区间。
5. 从已验证 plan、manifest、前序 snapshot 摘要和 canonical registry snapshot 重新构造唯一 canonical audit；调用方 audit 和 SHA256SUMS snapshot 必须逐字节匹配。SHA256SUMS 覆盖六个 artifact、manifest、registry 和 audit，上传只使用已验证的内存 snapshot 与重构字节，不再打开任何输入路径，也不使用调用方原始 audit。随后用 `If-None-Match: *` 上传版本对象；任何重名对象都失败，绝不覆盖。
6. 通过 R2 鉴权端点和匿名自定义域名分别回读每个对象；两侧都必须直接 HTTP 200、无重定向且字节完全一致。
7. 最后更新 `channels/stable/current.json`：首次使用 `If-None-Match: *`，后续使用旧 ETag 的 `If-Match`。CAS 失败时版本对象成为不可发现的安全孤儿，不能覆盖并发发布。
8. CAS 成功是不可逆 commit point。随后再次从 R2 和公开域名回读 pointer；只有两侧字节、SHA-256 和 ETag 验证通过才报告 `committed_and_verified`。CAS 后任何回读失败返回退出码 `3` 和 `committed_but_unverified`、`retryable=false`、预期 ETag/SHA-256/size 及只读核验步骤；不得自动或人工重跑 publish，不得用覆盖 current 的方式“回滚”。

发布工具只在隔离环境中安装 `release/ota/requirements.lock` 的完整 hash 锁定依赖，
直接从已审查的 `src/` 运行，不触发项目构建隔离或隐式下载。R2 SigV4 由锁定版本
`botocore` 生成，`If-None-Match` / `If-Match`、缓存策略和内容摘要均进入签名头。

版本对象使用 `Cache-Control: public,max-age=31536000,immutable`；pointer 使用
`Cache-Control: no-store,max-age=0`。R2 Bucket Lock 必须有启用的 `releases/` +
`Indefinite` 规则；发布 workflow 只读核验该规则，不能修改 bucket 配置。

## 单调性与回滚

- 首次 stable 发布必须为 `sequence=1`，且公网 pointer 必须不存在。
- 当前共享 `main` 与传统发布基线是 `0.5.177`，策略继续拒绝低于 `0.5.177` 的 OTA。`0.5.177` 可作为旧 `0.5.176` 客户端的首个 OTA；已经运行 `0.5.177` 的客户端仍会按客户端 anti-rollback 规则拒绝同版本，面向它们的后续 OTA 必须使用严格更高版本。
- 后续发布必须携带并验签公网现有 manifest；新 sequence、SemVer、`published_at` 都必须严格增加。
- 旧 sequence 或旧版本即使签名合法也不能重新发布为 current。
- 已锁定版本对象不删除、不修改。错误版本的处置是立即停止下一阶段 rollout，并用更高
  sequence/更高版本发布修复；需要暂停时同样发布更高 sequence 的 `paused=true` manifest。
- pointer CAS 失败不重试覆盖；重新读取最新 pointer、重新计算 sequence、重新签名并重新走完整验证。
- manifest 在入口已经过期或尚未生效时，不进行任何网络写；若上传不可变对象期间跨过 `expires_at`，停止 pointer 提交并保留不可发现的安全 orphan，重新生成更高 sequence/版本且有效期充足的候选。
- pointer CAS 成功后即视为已提交；后续回读失败不是普通发布失败。操作者只能按输出从鉴权 R2 和公开 URL 只读核验预期 ETag、size 与 SHA-256，状态未确认前停止 rollout。绝不能重跑同一 publish、覆盖 current 或报告“未提交”。
- 若私钥疑似泄露，停止 Environment，保留旧公钥用于验证历史链；先通过受保护 PR 把新公钥登记为 `next` 并分发信任，再切为 `active`。旧 key 进入 `retired` 后不能签新版本，但不能过早从注册表删除。

## 密钥和权限

`release/ota/trusted-keys.json` 只保存公钥、状态和有效期。真实公钥加入必须单独审查；真实
私钥不得由本工具生成。临时测试 key 只在测试临时目录/内存中存在。

`ota-production` Environment 应配置 required reviewer、prevent self-review、禁止 bypass，
并仅允许受保护 `main`。工作流所需秘密：

- `DRADAR_OTA_ED25519_PRIVATE_KEY_B64`：32-byte Ed25519 seed 的 base64；仅签名步骤引用。
- `DRADAR_OTA_R2_ACCESS_KEY_ID` / `DRADAR_OTA_R2_SECRET_ACCESS_KEY`：仅限
  `dradar-cli-ota-production` bucket 的 Object Read & Write。
- `DRADAR_OTA_CLOUDFLARE_API_TOKEN`：只读 bucket 配置，用于核验 Bucket Lock；不能编辑锁、DNS 或域名。

Environment variable `DRADAR_OTA_KEY_ID` 指向注册表中的 active key。账户、bucket 和公开域名
固定在已审查 workflow 中，防止调度参数把产物发往其他环境。

## 本地演练

发布工具不提供生产 keygen。测试通过 `Ed25519PrivateKey.generate()` 在临时目录创建 key，退出后
由测试框架清理：

```bash
uv run pytest -q tests/test_ota_release_tool.py
uvx ruff check scripts/ota_release.py tests/test_ota_release_tool.py
```

人工 dry-run 应使用测试域名/MockTransport，不得把临时 key 配入 `ota-production`。正式 workflow
只允许手动触发；输入包括 sequence、有效期、rollout、basis points、pause 和明确 bootstrap 标志。
发布工具退出码 `2` 表示 commit point 前失败；退出码 `3` 表示 pointer 已提交但最终回读未确认，
工作流会明确标注禁止重跑，并保留只读核验所需的 expected ETag/hash/size。

## 尚需逐项授权的外部写

在独立 QA 通过前不得执行以下操作：

1. 在确认账户中创建 `dradar-cli-ota-production` bucket。
2. 给 `releases/` 添加 indefinite Bucket Lock。回退方式是停止发布；锁保护期内不能删除已写对象。
3. 把 `updates.codexradar.com` 连接到该 bucket；这会写 R2 custom-domain 配置和 DNS。回退是断开域名并恢复原 DNS，不删除锁定对象。
4. 创建只限该 bucket 的 R2 Object Read & Write 凭据，以及 bucket-config 只读 Token。轮换时先并行验证新凭据，再撤销旧凭据。
5. 创建并保护 GitHub `ota-production` Environment，写入上述 secrets 和 key-id variable。回退是禁用 workflow/Environment 并撤销凭据。
6. 在受控离线或硬件边界生成生产 Ed25519 key，把私钥写入唯一权威 secret，把非秘密公钥通过独立 PR 登记；轮换按 `next → active → retired` 执行。
7. 首次 `sequence=1` bootstrap 发布。发布前必须再次确认 pointer 404、目标 worktree clean、HEAD 等于最新 `origin/main`、Bucket Lock 生效且公开域名直接返回 200。

## 官方能力依据

- Cloudflare R2 Bucket Lock 可按前缀禁止覆盖/删除并支持 indefinite retention：
  <https://developers.cloudflare.com/r2/buckets/bucket-locks/>
- R2 S3 API 支持 PutObject 条件请求：
  <https://developers.cloudflare.com/r2/api/s3/api/>
- R2 生产公网访问应使用 custom domain，`r2.dev` 仅供开发：
  <https://developers.cloudflare.com/r2/buckets/public-buckets/>
- GitHub Environment 可在 reviewer 批准前阻止 job 获取 environment secrets：
  <https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments>
