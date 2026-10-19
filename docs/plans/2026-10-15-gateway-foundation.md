# Gateway 安装与运行基础整改

基线：origin/main 47681187722d7ebdfa7923739827e1c0aaf9f727。
交付任务：task-b497f7d8c0e943eb；仅拥有本工作树的 gateway 变更。

## 目标与边界

让 Debian 安装不再依赖实时 PyPI；服务固定为非 root 身份；默认仅本机可访问，启用鉴权时所有管理和推理路径一致执行；管理员配置与模型数据在升级、卸载时可保留；健康状态可用于区分进程存活、模型待加载与降级/失败。

本次不迁移 bibit-voice ASR，不实现 Qwen3 llama-server 托管，不发布 apt 版本或 tag，不修改其它仓库业务代码。Web 控制台现有 CDN 资源是已知限制，本次保证入口与 API 鉴权兼容，完整离线前端另列风险。

## 实施单元

- U1（R1/R2/R5）：debian/、安装构建脚本、打包回归。构建时准备完整 wheel 依赖和摘要，包内携带，配置阶段仅从本地离线安装；候选环境校验成功才切换，失败可重试。固定 spacemit-ai-gateway 系统账户，HOME=/var/lib/spacemit-ai-gateway；配置 /etc/spacemit-ai-gateway，程序 /opt，只清理包自身生成内容。旧 /opt 配置及 installer drop-in 迁移前备份且不覆盖管理员修改。
- U2（R3/R4）：settings、两份默认配置、gateway 鉴权及主入口、必要控制台调用、测试。环境变量优先于 YAML，显式配置缺失报错；默认 127.0.0.1。统一鉴权覆盖所有有副作用 HTTP 路由、兼容路由及 WS 入口；保留现有 IP 白名单语义和健康探针。Deb 默认启用鉴权且首次安装生成设备本地密钥；控制台允许输入密钥，不将密钥放 URL。
- U3（R6）：健康汇总与回归。所有已装配域参与健康判定；idle 可以表示未加载，缺失、degraded、failed 不得显示整体 ok；不伪造推理可用。验证加载失败、进程重启与恢复。
- U4：集成 README、交付配置、验收记录及真机测试。U1/U2/U3 可按无重叠文件范围并行，集成和板端测试串行。

## 验收契约

| 项目 | 宿主机回归 | K3 独占真机验收 |
| --- | --- | --- |
| R1 离线安装 | 安装脚本不联网、摘要损坏阻断、失败保留旧环境并可重试 | 实际 deb 在禁用 pip 网络下安装及重复 configure；检查展开后的 maintainer scripts |
| R2 身份/目录 | unit 与脚本一致、安装不要求 live systemd/D-Bus | 无 SUDO_USER 安装仍非 root；模型/SQLite 可写，程序目录不可写；确认所需设备权限 |
| R3 访问边界 | 未授权管理写/推理/WS 拒绝、带 key 允许、关闭鉴权和白名单兼容 | ss 检查两个端口仅本机；未授权 load/unload/switch/params/download 等不能改变状态；有效 key 可操作 |
| R4 配置优先级 | 嵌套 env > YAML > 默认，缺失/无效配置明确失败 | systemd 环境覆盖实际监听与鉴权，移除覆盖后恢复 |
| R5 生命周期 | configure 重入；remove/purge 仅清理自身资产；旧配置迁移保护 | 安装→改配置/生成数据/管理员 drop-in→升级→remove→重装→purge，核对保留语义 |
| R6 健康/恢复 | 八域健康汇总含缺失/降级/失败与 idle | 真实 SDK 和模型推理结果、故障可见、自动重启恢复；voice 依赖版本与服务前后对比 |

## 真机安全与证据

独占验收为覆盖 remove/purge，会仅对 Gateway 使用 dpkg --force-depends，暂时允许它的反向依赖处于未满足状态，不移除或升级这些依赖包；结束必须恢复 Gateway 并以 dpkg --audit 及包清单确认系统依赖完整。生产安装不使用该参数。

先通过 bibit-dev 确认板端租约；调度不可用时继续本地，禁止直连抢板。保留系统包版本、apt 模拟、gateway 原包与配置/数据、服务状态和 voice 依赖清单；只在独占请求中构建和验收。测试脚本必须在正常和异常退出时恢复原状态；恢复失败先保留租约并记录，不以通过交还。

证据绑定源码快照、deb SHA256、板号、请求 ID、真实测试日志和恢复结果；不把 fake/mock 单测当识别验收。缺少必要模型/依赖时列为未通过并继续补齐。结束用 bibit-dev verify record/export 和交付 record 归档。

## 完成条件

相关 pytest 与打包生命周期回归通过；真实 riscv64 deb 六项验收与板恢复通过；文档说明迁移、鉴权和路径；review 完成后通过 agent-delivery submit/batch 落地。gateway 只合入本地 main，推送 MR 分支供上游 review，不直推远端 main。

## 本次验收结果（2026-10-15）

- 构建：req-5c3c900aac51474a84a3c5095a5ad97f，artifact gateway-2985cf14a56b，K3DEB1-3。
- Deb SHA256：bdceeaf0110ed20ebcf9cb814d7c0c7bed40ec46b76bb1ea20800701d0c1e997。源码快照 691796d5ad966de3bf00ab28eb98710fa461b34ef8973ed38b859f1cda306312；251 个运行/打包/前端/配置/测试文件与工作树逐字节一致。
- 验收：req-d67d971a60da4d7eab853fa087105a7c，attempt att-0a3a272bf08847969c4f36a44843098d，completed，result=ok、restored=true；R1–R6 与 restoration 已记入平台 verify.tsv。R1 的实际机器覆盖离线安装和 configure 重入，候选环境切换失败后重试由宿主测试覆盖。
- 恢复：原 Gateway 0.0.19，两个原服务 active；独立归档核对 4909 条、0 差异；dpkg --audit 无输出；bibit-voice、ORT、llama 相关包清单前后相同。归档核对包含内容、属主、权限、软链及 xattr，不比较 mtime。
- 宿主：498 passed、33 skipped、33 subtests passed。Console 宿主浏览器鉴权通过；同版本资源替代 CDN 传输，未完成板端 UI 与公网 CDN 可用性验证。
- 公开证据：docs/verification/evidence/gateway-foundation/；截图 docs/assets/gateway-console-auth-host.png。备份含设备配置与凭据，仅留板端受限目录，不纳入 Git。

历史失败不计通过：req-2a8c62581d734f00bd9874e981077b66 暴露 SDK 静默初始化失败；req-6eff87aa9a9549bf87372cb4b532d3b3 暴露验收脚本将 error 误写为 code。两次均已 restored=true，独立恢复核对无差异。最终通过只绑定上述新产物与请求。
