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


## MR !7 审计修复（2026-10-20）

任务 task-0a454fe6a6aa4051，仅修改 Gateway；复用现有 MR 源分支，禁止远端 main 直推。

- U1：旧 Qwen3 HTTP 接入允许其配置指定的本机外部监听；托管仍拒绝接管。
- U2：源码/wheel 安全初始化同一共享锁，保留既有 inode、权限错误明确失败。
- U3：release 等待在途推理有期限，再终止本方进程组，真实退出后才取消 I/O 和关锁。
- U4：acquire 未交付令牌且清理失败时内部重试；锁保持占有，shutdown 不无限等待重试任务。
- U5：完整管理员 unit 使用旧配置路径时补兼容软链；unit 原字节、自选配置均保留。
- U6：停服升级按有效 EnvironmentFiles 顺序读取 HOME/环境，运行时以 /proc 为准。
- U7：全局健康纳入托管模型、失败及释放状态，释放后恢复 lazy idle。

每项有宿主回归；U3/U7 增加独占板端真实 Qwen3 SIGSTOP 故障注入。完整 Deb 生命周期及双模型推理重新验收，确认 restored=true 和 safe_to_release 后交付。源码安装涉及不同 UID 时仍需管理员预建共享组/目录，不能靠每用户锁规避互斥。

补充回归：加载后 status 抛异常时同样释放未交付会话；完整管理员 unit 的非 root UID 通过原 ACL 助手只获得兼容配置读权限，并验证 configure 重入不扩大权限。

审计后补充（2026-10-20）：保护 acquire 重试时已交付的会话，成功 dispose 后清除旧清理任务，健康探针跨释放后重读当前状态；SenseVoice 忙时也识别停止/僵尸 worker。安装兼容补充 EnvironmentFiles 通配顺序、数字 UID 与旧配置软链祖先的最小遍历 ACL，并增加实际跨 UID 读取及 purge 行为回归。最终宿主为 762 passed、33 skipped、79 subtests passed；新构建 req-cca9081862b6409ba5c3433dd770b9a0 仍待板端验收。设备 CPU 活动与未返回请求不能证明处于推理阶段，厂商 Qwen3 推理阶段内挂死明确保留 unverified，不因宿主回归通过而关闭该项。

2026-10-20 接续：修复 SenseVoice 运行态无响应误报健康（沿用启动预算，不取消 I/O），补正常慢推理恢复、Python 3.10 TERM/KILL 兼容、slot 阶段故障注入及释放全过程独立 flock 观察。当前宿主 766 项通过；新候选 req-9b4b26a2a2ae4461a3788f5a26d48f4a 待独占板验，未提交/推送，不以旧通过替代。

2026-10-20 最新接续：四项新增 P1 已修复，独立专项复核未确认残留 P0/P1；宿主 Python 3.12/3.10 均 836 passed、33 skipped、83 subtests passed。新产物 gateway-b1dfa3d36175（Deb SHA256 a2079300b1389a4122789bf66f0b00b1121fe87d59536942f04c047382de7acc）已构建；req-200dc7b0037548d4a3be7454ddc93211 正在 K3DEB1 重新验收，仍未提交/更新现有 MR。两个旧联合失败已 restored=true，旧租约 completed/occupancy=[]，失败不计通过。

2026-10-20 11:46 UTC 终态核查：同一新产物完整管理员生命周期再次通过（req-cac00e6291394af88246c83d578f171c），原 Intent 三条向量前后一致；保留此前单次向量失败。提交识别前预布防 GDB 仍未命中选定计算符号并超时，U3/U7 真机故障未通过。finally 恢复完成，平台已 completed、occupancy=[]、safe_to_release=true、cleanup_errors=[]。先定位真实调用链，不继续盲选符号；新修复未提交或更新 MR。证据与最新状态见 task-0a454fe6a6aa4051 的 pending 条目和 status.json。

2026-10-20 12:05 UTC 收口：真实线程栈和反汇编确认旧基类派发入口只有 ret，改用实际在线权重打包入口同步断点；同 Deb 真实故障验证通过，4925 次 flock 观察未提前解锁，health 降级、release 有界、进程退出后锁可取并回到 idle。req-0ce67ce78362469fbeaf3a7ac4a21a9a 已 restored=true、completed、occupancy=[]、safe_to_release=true；完整管理员生命周期也通过。双版本宿主全量各836通过、33跳过、83子测试通过。生产不变；task-518f8fe89a974c52 接手现有 MR 分支交付，不改本地或远端 main。


## 2026-10-21 MR !7 全问题修复接续

本轮 Gateway 基线 `e4e28ce528fccc3cfebc26a8498a021c1ff5af4b`，任务 `task-636a994501a04acb`；
配套 Voice 基线 `0ce3cfec7e0242947cc862836c79563e3a12abbc`，任务 `task-eb7bcab8202b42a0`。
仅拥有两个隔离工作树；不修改共享主树、其他用户工作或已保存的历史验收证据。

范围：管理员配置原子替换后补 ACL、旧非 root 身份不提升 root、恢复秘密防公开 owner 盗 token、
并发健康探针复用、purge 私有归档恢复源、旧 ASR 构造前拒绝不污染 managed、GET 结果鉴权、
multipart 实际流量限额及有界文件读取。协议升级为 v2，Voice v1 明确不兼容，不在释放本地资源后才发现。

已加入专项回归并同步设备助手、fixture、接口文档；Voice 全宿主回归 223 项通过。
Gateway 首轮864通过1失败：旧测试要求并发 probe 立即失败，与新健康语义相反；已同步为共享探针有界等待。
加强 ACL 测试记录 inode 必须变化；基线执行确实失去 ACL，当前专项通过。
尚未完成新配对包构建、独占板端双模型/安装回滚验收、最终审查及提交。旧产物的成功不能代替本轮。
验收与最终结果统一记录 `docs/verification/pending/task-636a994501a04acb.md`。

2026-10-21本轮收尾：全部已确认源码问题已补修，串行双Python全量各876通过/33跳过/83子测试，配对Voice224通过；第二轮专项复核无已确认P0/P1。Gateway三次候选构建均因公共PyPI TLS EOF失败，平台已安全清理。Voice最终新产物已完成构建，未配对部署、未提交/合入/推送；目标保持未完成。状态见task-636a994501a04acb。


### 2026-10-21 DNS 独占恢复与候选构建

用户已明确允许临时修正并恢复 DNS。平台请求 req-8ab0de76b0164772b92a723748f9b0e0，K3DEB1 独占 attempt att-0cfa6a8777704a89ac549949f3713069 / fence413。
临时 hosts 使用151.101.0.223，同域名SNI、curl正常TLS校验HTTP200；未改Gateway公共依赖源或TLS校验。
恢复守护进程由 BoardExecutor.start_process 登记，平台清理SIGTERM触发恢复原字节/模式/属主。构建 completed，safe_to_release=true、occupancy=[]。
原hosts SHA256 c3f23ac51edb32f59bc0d275f42f984e1c72f4b71cc1db5f4f65e1a3a4f864ea；恢复默认DNS167.82.24.223。
新Gateway artifact gateway-0cfa6a877770，deb SHA256 b3f40283f4168886d44cee48c01af7f22ad336583a0f834900c054f428fd2e10。
联合验收平台要求两产物同构建板，原voice-9af2c213f341在K3DEB1-3，不能与K3DEB1产物直接配对；已在K3DEB1受控重建。同步修正Voice验收输出contract标签为managed-asr-v2（原仅标签陈旧），9项专项通过，最终配对请求req-57723ef9a5114e94abd5241735ffb25d。
DNS工具仅保存在本任务evidence，不进入安装包；本机恢复自检通过。证据JSON中的平台租约token已脱敏。
真机联合及完整安装回滚验收仍待执行，未提交/合入/推送。


### 真机联合通过与回滚助手补修

联合请求req-b033bf3d430642a3b23b74fec1145538已经completed，restored=true、occupancy=[]。
配对voice-ab60fb7e4884 + gateway-0cfa6a877770：SenseVoice/Qwen3-ASR各local及gateway真人音频全部ok，MainPID稳定切换，安装先后/卸载/回滚通过；报告managed-asr-v2、t18.result=ok。
旧Gateway0.0.17恢复在线pip耗时约4分钟后自行完成；没有执行第二次DNS写操作。补修Voice验收助手：在旧包配置前提取原运行环境，再PIP_NO_INDEX=1配置；新最小回归10项、全量225项通过，候选重新排队req-24e01e87de6f462782e07d034caf05c1。旧联合结果不证明这次新助手修改。
第一次额外HTTP边界探针在配置完成后发生工具自身AttributeError（http函数名覆盖标准库模块），完整finally已restored=true，未记边界通过；已修正函数名/连接关闭并补wrapper自检。
Gateway正式全生命周期新请求req-718395c4495749d7bd163a17564f901a排队中；边界及故障注入待持有独占租约时执行。


### 正式安装生命周期与HTTP边界

req-718395c4495749d7bd163a17564f901a / att-84df44a26f15444d8f97590be8f2b098：包内原生命周期脚本result=ok、restored=true，完整管理员User=bianbu配置ACL可读、升级/remove/purge/reinstall、VAD故障健康与旧包/外部数据库/权限恢复均通过。
额外HTTP探针最初工具异常和CORS断言过严均保留失败记录，不计通过。精简边界回调第一次遗漏mutated标记，只恢复启停；已发现并立即使用该次原备份gateway-backup-39zei0cp执行完整restore（probe-full-restore.log），旧包0.0.17、外部数据库与原文件恢复成功后才重跑。未释放租约或把该次摘要当成实际完整恢复。
修正后的独占HTTP探针gateway-http-boundary-device-fixed.log：result=ok、restored=true；匿名GET/HEAD数据401、有效key查询200/缺失job404、chunked2MiB超1.25MiB限额413且CORS保留、小文件进入字段校验422、无新增已删除临时文件FD。
最终Voice候选voice-175edc917a83（含离线回滚助手）构建completed，deb SHA256 e7809ad3d4a9456883c9a72b04a84f1873f4573648aea6ad4be4c3af28354f8c。当前在同一holding租约上执行该候选完整双模型联合+GDB阻塞推理/并发健康/锁寿命验收；未以先前Voice候选替代。


### 最终候选验收收口

最终候选：gateway-0cfa6a877770（b3f40283f4168886d44cee48c01af7f22ad336583a0f834900c054f428fd2e10）、voice-175edc917a83（e7809ad3d4a9456883c9a72b04a84f1873f4573648aea6ad4be4c3af28354f8c）。
req-718395c4495749d7bd163a17564f901a 已 completed、safe_to_release=true、occupancy=[]。
最终配对完整联合 + 故障注入：result=ok、restored=true；双模型local/gateway真人音频与MainPID切换、协议v2恢复秘密拒盗/合法重试、4轮各8个并发健康请求全部ready（94–324ms）、GDB真实SpineGemmDispatchMMT4D::PackOnlineWeight断点验证，4929个alive_locked样本及退出后锁释放。
最新Voice回滚助手从原备份恢复运行环境后PIP_NO_INDEX=1配置旧包，真实恢复成功，无第二次DNS修改。宿主机最新Voice225项通过；Gateway此前双Python串行876项证据不被旧候选代替。
最终原hosts字节SHA、644/root:root恢复核验通过；原Gateway服务active；共享主树保持原状。没有永久发布包/apt/tag，也未推送或更新MR。
已通过bibit-dev verify record记录3项，并保留完整失败工具尝试和人工完整恢复证据。
原Gateway生命周期报告子段contract仍为旧报告标签managed-asr-v1，并不表示使用协议v1；实际接口status与acquire按v2校验，配对主报告managed-asr-v2。此次不为报告标签重新生成生产包，后续可单独整理报告schema。
