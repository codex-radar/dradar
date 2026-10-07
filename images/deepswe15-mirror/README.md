# DeepSWE15 官方镜像转存

固定清单001–015，仅将官方 `task.toml` 指定的公开 ECR 镜像原样转存到 GHCR，平台 `linux/amd64`。没有运行模型、判分或修改镜像层、config、labels；包关联通过 GitHub 的包元数据完成。

匿名拉取（无需 GHCR 登录或 Claude 账号；此命令仅下载环境）：

```sh
docker pull --platform linux/amd64 ghcr.io/codex-radar/dradar-env-igel-persist-feature-schema@sha256:434ed4187abdb6948dec6434ab4a354d11a19955ebcc706a605ebc6e2470c140
```

全部15题的不可变引用见 `BATCH.json`。

## 来源与许可

官方任务来源：[datacurve-ai/deep-swe](https://github.com/datacurve-ai/deep-swe/tree/0b9fabbb63b9104d678fe965e1632f2dd9eaa2ea)。每题源tag、解析后的digest、固定上游项目与commit及原provenance许可字段均保存在BATCH.json。随附原样DeepSWE-LICENSE和DeepSWE-PROVENANCE.md。镜像中的原许可证、版权及来源说明随原层保留，未重新许可。DeepSWE的Apache-2.0仅覆盖其自身贡献，不覆盖所有上游组件。

整幅镜像所有第三方组件的分发条件没有逐项确认；不声称获得额外上游授权，也不把用户转存授权写成上游许可证明。004中的Claude相关依赖保持官方字节，不要求Claude账号完成镜像下载，不代表提供解题模型或订阅。

## 验收

COPY_RECEIPT核原始source/target manifest和config逐字一致、全部layer descriptors与rootfs diff IDs一致。新匿名runner用专用空Docker data root，空Docker config和空Skopeo auth；逐题实际完整docker pull，记录首次空缓存与后续共享层缓存。Public及codex-radar/dradar仓库关联通过CI临时GITHUB_TOKEN单独回读。实际网络/磁盘字节未测，不宣称零流量或测速收益。
