# 托管 ASR 接口与验收

本接口协议版本为 1，供 Voice 独占使用 Gateway 托管的 SenseVoice 或 Qwen3。
`asr.managed.enabled` 默认 `false`，新增字段不改变原 ASR 配置含义；旧 LLM/Embed 路径及鉴权不变。
所有 managed 请求，包括 GET，都执行 Gateway 既有 API key / IP 白名单鉴权。

## 接口

| 请求 | 参数 | 成功响应 |
| --- | --- | --- |
| `GET /v1/asr/managed/status` | query `model`、`language` | 状态对象 |
| `POST /v1/asr/managed/acquire` | JSON `{owner,model,language}` | 状态对象及随机 `token` |
| `POST /v1/asr/managed/release` | JSON `{owner,token}` | `{"released":true}` |
| `POST /v1/asr/managed/recognize` | multipart `file,owner,token,language` | `{"text":"..."}` |

`model` 只接受 `sensevoice`、`qwen3_asr_0.6b`。`owner` 为 Voice 实例 UUID。
`language` 是逗号分隔列表，例如 `zh,en`；去除空白、转小写、保留顺序去重。
支持语言代码 `zh,en,ja,ko,yue,auto`，其中 `auto` 必须单独使用。

状态对象字段：

```json
{
  "protocol_version": 1,
  "instance_id": "每个 Gateway 进程随机 UUID",
  "model": "qwen3_asr_0.6b",
  "languages": ["zh", "en"],
  "assets_ready": true,
  "ready": false,
  "owner": null,
  "resource_state": "free",
  "reason": "available"
}
```

`ready=true` 只表示本方受管后端已加载、目标模型准确且支持所查询语言、健康检查通过。
未加载时即使资产完整，`ready` 仍是 false，GET 不启动进程、不下载或加载模型。
`assets_ready` 与资源锁状态独立；本地 Voice 持 engine 锁时允许资产 preflight，状态为
`blocked` / `engine_lock_busy`，真正切换必须在本地释放后 acquire 成功。
权限失败为 `engine_lock_permission_denied`，不存在/无效锁文件为 `engine_lock_unavailable`。

Session 绑定模型，recognize 每次独立验证、传递语言；同模型变更语言不 reload，且不会改旧 default。
Qwen 提示词按列表发送，例如 `language Chinese,English<asr_text>`。
status 按本次查询语言判断能力，不要求等于 acquire 初始语言。
旧 SDK 不能表达 SenseVoice 的语言约束列表，`sensevoice + zh,en` 明确返回
`sensevoice_language_list_unsupported`，不静默映射成 auto。
Voice 的 SenseVoice 与其本地实现一致固定请求 `auto`，保留 Qwen3 的语言列表偏好。
固定语言不要求 SDK 动态 setter；仅请求改变已加载引擎语言时调用 `Engine.set_language`，
无 setter 则明确报 `sensevoice_dynamic_language_unsupported`，不静默变更语言。
宿主 worker 协议测试覆盖有/无 setter；真实 SDK、两模型效果与 T18 仍待板验。

错误沿用 Gateway DomainError 响应封装。非法模型/语言为 422；owner/token 不匹配为 403；
资源占有、旧模型、未知进程等冲突为 409；受管后端启动/健康失败为 503。

## 资源寿命与旧调用者

固定默认锁是 `/run/lock/bibit-asr/engine.lock`。目录 `root:bibit-asr 2775`，文件
`root:bibit-asr 0660`；Debian postinst 幂等建组并运行 tmpfiles，两个安装方必须保留同一 inode，
不得 unlink/recreate，不用 `LOCK_UN` 提前释放共享 open-file-description。
Gateway 服务通过 `SupplementaryGroups=bibit-asr` 获得权限。

参与者协议固定为 `dirname(engine_lock_path)/participant-<pid>.lock`，文件 `0644`。
Voice/Gateway 主进程从启动起持有独占 flock，FD `CLOEXEC`，不传给推理子进程。
Gateway 同进程多个 AsrService 共用同一个全局 FD，保持到进程退出。
判断同机 `bibit-audio` / Gateway 为协作者须有对应 regular 文件，且对它尝试 `flock EX|NB`
返回 EWOULDBLOCK；仅文件存在、进程名或命令行不构成协作证据。
这是同机合作协议，不是针对具有目录写权限者的安全隔离。
未知旧 Voice、未知 Gateway、残存 llama-server/受管 worker 均保守 blocked，不杀、不接管。

engine FD 由 Gateway 在受管 backend 全寿命持有，通过 `pass_fds` 传入子进程。
父进程退出而推理子进程仍活着时，继承 FD 继续占锁。
release 先阻止新推理，等 shield 保护的全部真实推理完成，再终止并等待本方进程组退出，
最后 close 父进程 FD 并回执；shutdown 失败保留锁、归属和 token，允许本人重试。
HTTP 断连、超时、健康失败、端口失联都不能证明资源释放。无 token 超时不自动解锁活模型。

acquire 加载任务在 HTTP 取消后继续收尾，随后只清理该次未交付的精确 session；
清理仍须等待真实子进程退出。HTTP disconnect 在等待加载期间主动检测。
如果响应已经发出但在网络中丢失，现有契约没有客户端确认，服务端无法证明 token 已接收；
相同 owner/model 可以重试 acquire，返回原 token，不重载模型、不接管其他 owner。
客户端应保留同一实例 UUID 完成恢复并 release。另一个 UUID 即使持同一 API key 也不能释放。

旧生产 ASR 首次实际加载也强制竞争同一 engine 锁，即使 managed 未启用；缺锁目录、权限或锁忙明确失败。
SDK 资源回收无法证明时，即使旧 unload/shutdown 或加载失败也保留锁至进程退出。
本进程曾使用旧 ASR 后，managed 保守报 `legacy_asr_loaded`，需重启 Gateway 后才能启用托管。
测试构造器注入已存在 backend 可豁免首次生产加载锁；生产 lifespan 使用空 backends，不走该豁免。

## 配置与离线资产清单

```yaml
asr:
  managed:
    enabled: true
    sensevoice_dir: /opt/bibit/models/asr/sensevoice
    lock_path: /run/lock/bibit-asr/engine.lock
    startup_timeout: 60
    shutdown_timeout: 10
    qwen3:
      model_dir: /opt/bibit/models/asr/qwen3-asr-0.6B-dynq-q40
      server_bin: /usr/bin/llama-server
      port: 8063
      threads: 2
      batch_threads: 2
      ctx_size: 4096
      ep_affinity: "8;9"
      max_tokens: 100
```

`model_dir`、server_bin、运行库路径只由管理员配置，不接受 HTTP 指定路径。
Qwen 使用本方临时 SMT 配置，清理遗留 LD_LIBRARY_PATH/EP 环境，TCM 关闭，单槽、warmup，
核对本方 PID 监听的 loopback socket、`/health` 以及 `/v1/models` 精确别名 `qwen3-asr`。
沿用 Voice 的 A100 affinity guard：只将完全处于 CPU8–15 的 EP 线程收紧到配置目标，
不改变混合/大核线程；权限失败拒绝就绪并保留清理流程。此亲和性行为仍须在真实设备验证。

模型目录需要 `managed-asr-manifest.json`。必需资产：

- SenseVoice：`model_quant_optimized.onnx`、`tokens.txt`、`am.mvn`、`sensevoice_decoder_model.onnx`。
- Qwen：`config.json`（含 audio_model 对象）、`Qwen3-ASR-0.6B-text-q40.gguf`、
  `Qwen3-ASR-0.6B-encoder-frontend.dynq.onnx`、`Qwen3-ASR-0.6B-encoder-backend.dynq.onnx`。

在安装了本轮候选 Gateway 及实际 SDK 的 venv 中执行以下离线命令。`--library` 应重复指定
全部实际依赖的厂商 EP/ONNX/媒体运行库绝对路径；`--runtime-package` 可重复指定需要固定的
Python distribution。SenseVoice 自动记录 `spacemit-asr` 版本，Qwen 自动核验并记录
`llama-server` 摘要。`--version` 是管理员提供的资产身份标签，不是 Gateway 包版本，
也不会从模型文件推导；应使用可追溯的归档或发布标识，并在变更任一权重、编码器或运行库时更新。
以下命令以 K3DEB1 实测资产为例，其他板卡必须按本机文件重新生成：

```sh
/opt/spacemit-ai-gateway/venv/bin/python -m spacemit_ai_gateway.domains.asr.managed_assets \
  --model sensevoice \
  --model-dir /opt/bibit/models/asr/sensevoice \
  --version sensevoice-archive-d2670fd0d6247c34b397ca4c48ee3d61 \
  --runtime-package spacemit-asr \
  --library /usr/lib/libonnxruntime.so.1.24.2+spacemit.a1 \
  --library /usr/lib/libspacemit_ep.so.2.0.7 \
  --library /usr/lib/libspert.so.0.6.2

/opt/spacemit-ai-gateway/venv/bin/python -m spacemit_ai_gateway.domains.asr.managed_assets \
  --model qwen3_asr_0.6b \
  --model-dir /opt/bibit/models/asr/qwen3-asr-0.6B-dynq-q40 \
  --version qwen3-asr-0.6B-dynq-q40-archive-61dbc944077331670014cdfbe4de3a73 \
  --server-bin /usr/bin/llama-server \
  --library /usr/lib/libonnxruntime.so.1.24.2+spacemit.a1 \
  --library /usr/lib/libspacemit_ep.so.2.0.7 \
  --library /usr/lib/libspert.so.0.6.2

/opt/spacemit-ai-gateway/venv/bin/python -m spacemit_ai_gateway.domains.asr.managed_assets \
  --model sensevoice --model-dir /opt/bibit/models/asr/sensevoice --verify

/opt/spacemit-ai-gateway/venv/bin/python -m spacemit_ai_gateway.domains.asr.managed_assets \
  --model qwen3_asr_0.6b \
  --model-dir /opt/bibit/models/asr/qwen3-asr-0.6B-dynq-q40 \
  --server-bin /usr/bin/llama-server --verify
```

K3DEB1 的实测运行库摘要如下；清单会直接记录这些路径与 SHA256：

```text
spacemit-asr=1.0.2
libonnxruntime.so.1.24.2+spacemit.a1  5d132e2ae7c322d1d31664a55848048584acd4ae8956f42ac6427defe3ca4036
libspacemit_ep.so.2.0.7              4f0c251df521e3d62cf4a04d63178fd69da9fe3821590e0129fe1a5b0da92509
libspert.so.0.6.2                    0db8febc30aa49d6915c174a8f65b076dbbc7f61a4a3a6e18319eb0aa289b7fe
llama-server                         3ac6b231e28b51ca4be23a15dee85dabf91cefa50573f50037f7ccbdda2cc695
```

生成命令拒绝空文件、缺文件、无运行库、无版本及默认覆盖现有清单；显式 `--force` 才原子替换。
它只盘点管理员已确认来源的本地文件，不下载，不证明权重语义正确；真模型识别另行验收。
清单结构包含 `model/version/files`（相对模型目录文件及 SHA256）、
`runtime.packages`（distribution 版本）、`runtime.libraries`（绝对路径及 SHA256），
Qwen 另有 `runtime.server_sha256`。全部必需文件、列出的运行库和版本都须通过。

完整校验在线程中执行。成功结果按 manifest 内容指纹、每个文件的解析路径、设备号、inode、
size、mtime_ns、ctime_ns 和 runtime distribution 版本缓存；每次 status 重新采样，变化即重算。
校验期间文件发生变化则拒绝就绪。失败不缓存，缓存最多保留 32 个完整快照。
首轮摘要可能较慢，客户端不得把首轮 HTTP 超时视为模型坏或资源释放。

## 设备 lifecycle 与证据

平台 gateway 配方调用 `debian/tests/test-device-lifecycle.py`。沿用必填
`--binary --expect-sha256 --evidence-dir --request-id --attempt-id --artifact-id`，
本轮增加可选 `--managed-cases /absolute/cases.json` 与
`--managed-cases-sha256 <64位小写SHA256>`。经平台执行时两参数必须配对；
生命周期脚本在停服务和装包前校验原始 JSON 摘要，并冻结本轮解析结果。
用例同时覆盖两种模型，例如：

```json
[
  {
    "model": "qwen3_asr_0.6b",
    "language": "zh,en",
    "switch_languages": ["zh", "en"],
    "audio": "/absolute/real-speech.wav",
    "expect_text": "与该语音对应的期望文本子串"
  },
  {
    "model": "sensevoice",
    "language": "auto",
    "audio": "/absolute/real-speech.wav",
    "expect_text": "与该语音对应的期望文本子串"
  }
]
```

新版平台入口为 `bibit-dev verify deploy <artifact_id> --managed-cases <宿主机绝对路径>
--managed-cases-sha256 <摘要> --wait`；平台校验后上传到验收板，并在板端再次核对摘要。托管请求使用独立 RPC 操作，旧 daemon 会明确拒绝，
须按平台排空流程一同更新 CLI/daemon/worker；不能直接把新 CLI 连到旧 daemon 当作已升级。

模型/运行库/清单须在设备预先就绪，管理员配置指向它们；验收只临时 opt-in，结束恢复原配置。
仅平台独占租约内由负责人执行。此任务未上板，宿主的厂商推理替身不构成真模型证据。
未知 Voice/llama 或本地 engine 持锁时阻塞本轮验收，不自动停止 Voice。

验收包括鉴权、精确目标状态、真实语音/期望文本、语言切换、错 owner/token 不能释放、
运行期间 kernel flock 忙、本人 release 后 kernel flock 可取得且状态回到 free。
连续 acquire 通信失败且未取得 token 时记录 `session_token_unavailable` /
`resource_release=unconfirmed`，不得把 API 故障当作资源回收证据。
公开摘要保留候选 SHA256、artifact/request/attempt、instance_id、cases SHA256、audio SHA256，
不保存 key、token 或识别文本。`managed_asr.result=not_run` 表示只跑了 foundation，
不能据此宣称 U4 通过；单模型通过也不能把 T18 记为通过。
旧 foundation 静态证据文件保持原样；本轮用新的平台候选与摘要。

## 权限交接与回归

向旧登录 session 新增 supplementary group 不会即时修改已启动进程的组列表。
Gateway systemd 服务重启会取得 SupplementaryGroups；Voice 打包方需要为真实用户提供
受控的新 initgroups 启动路径，或对已确认用户 UID 设置目录/engine 文件的精确 POSIX ACL。
不得放宽成 0666、不得替换 engine inode，也不强制关闭用户会话。
Voice postinst 已实现按确认 UID 授权的精确 ACL，双方配置一致；旧登录 session 的实际访问仍须板验。

宿主回归：`.agent-delivery.toml` 的 `[verify].regression`。
新增测试在 `tests/unit/test_managed_asr.py`、`test_managed_asr_assets.py`、
`test_device_lifecycle.py`、`test_managed_asr_worker.py` 及 `tests/integration/test_managed_asr_process.py`。
真实 SDK 能力、板端 runtime/完整库清单、T18 SenseVoice 语义及旧 session 权限须保留待验。

## 升级兼容：保留旧 Intent 与缓存

只升级 Gateway deb 时，旧 Intent 无需增加 API key 或修改配置。安装前保留旧 YAML，
并记录旧服务的实际 HOME、运行身份及 Gateway 环境；运行中的服务以 `/proc/<pid>/environ`
为准，已停止的服务读取原 unit 环境及管理员环境文件。chroot/offline 无法查询 systemd
时，从 unpack 前的旧 unit、按优先级合并的 drop-in 及其 EnvironmentFile 生成同格式快照；
检测到 chroot 时不查询可能绑定挂载的宿主 systemd。常见 root unit 和包生成的用户身份
drop-in 均支持，不要求存在登录会话。正常在线 systemd 查询失败仍明确报错。无 runtime-user.conf 的旧 root
服务使用原 `/root` HOME，不使用 sudo 调用者推测缓存位置。管理员自选 YAML 也留存快照。

升级沿用旧 YAML 与环境的有效鉴权选择，不再无条件写入 AUTH__ENABLED=true；管理员的
`/etc/spacemit-ai-gateway/environment` 保持较高优先级，已有 key 不重新生成。全新安装仍
生成随机 key 并默认开启鉴权、使用 loopback 监听。升级保留旧 YAML 的监听地址
（包括 `0.0.0.0`）及旧 unit/进程环境中的 `SPACEMIT_AI_GATEWAY_APP__HOST` 覆盖，
不再强制收敛为 loopback，因此远程旧客户端可继续使用原地址。管理员显式 YAML 和环境
配置不被重写；现有管理员 environment 仍按服务 EnvironmentFile 顺序覆盖迁移凭据。

服务继续使用固定非 root 账户 `spacemit-ai-gateway`。全新安装的 HOME 为
`/var/lib/spacemit-ai-gateway`；升级通过 credentials 保留旧有效 HOME，并按管理员
EnvironmentFile 的优先级保留 XDG_CACHE_HOME，以维持 SDK preset、视觉目录和缓存的路径语义。
旧 LLM/Embed/Rerank 等 storage 的 `~` 固化为原 HOME 的绝对路径，相对路径按旧工作目录
固化。LLM/Embed/Rerank/VLM 省略域、storage 或 db_path 时，仅按应用 StorageConfig 的默认
storage 叶子物化到旧 HOME；不合并或覆盖管理员模型列表、启动参数等其他字段。
管理员自定义 YAML 字节保留，必要的路径通过 credentials 环境叶子补充；管理员环境中
依赖旧 HOME 的 storage 叶子会备份后物化，其他鉴权设置保持。模型与 SQLite 数据库原地复用，
不复制模型、不修改模型属主；父目录仅给服务 UID 添加穿越 ACL，明确配置的模型、状态和
数据库目录授予所需读写及默认 ACL，以保留下载更新与 SQLite WAL/SHM 功能。其他主体原来被
ACL mask 屏蔽的权限不会因新增服务 ACL 而生效，也不放宽 HOME 对其他用户的权限。

同时按最终有效 YAML、旧进程环境及管理员 EnvironmentFile 的优先级定位模型 DB，使用
SQLite `mode=ro` 只读读取本地注册引用，包含 WAL 内已提交的记录；不修改 DB 内容或 API key。
外部 `local_path` 为文件时仅对该文件授予服务 UID 读 ACL，其祖先仅授予穿越 `x`；为目录时
仅对确认的模型子树授予目录 `r-x`、普通文件 `r--`，不新增 default ACL，不授权兄弟模型或
整个 HOME。模型内容、inode 和属主保持。失效/相对路径、`..`、任意级或子树内 symlink、
特殊文件、整 HOME 授权，以及损坏/结构不兼容 DB 均明确失败。完整路径集合在修改 ACL 前
验证；源配置与 DB 保留，不发布迁移完成标记。文件和目录模型的外部 ACL 都记录在同一
journal，并由现有设备 lifecycle 回滚实现恢复。

恢复源位于 `/etc/spacemit-ai-gateway/`：`legacy-base.yaml`、可选的
`legacy-effective-base.yaml`、`legacy-context.json`、`*-legacy-runtime-user.conf`，以及实际
改写过的 `legacy-environment` / `legacy-credentials.env`。逐路径原 ACL、权限及缺省 ACL
记录在 `legacy-storage-acl.jsonl`。这些恢复文件仅 root 可读，重入不覆盖最初备份；它们供
管理员审阅和回滚使用，不会在卸载时递归删除用户模型。迁移完成前不会删除生成的旧身份
配置；ACL 不受支持、无法确认旧身份或存储树内存在未解析符号链接时明确失败，禁止以关闭
新安装鉴权或开放 HOME 权限绕过。离线无法解析的动态身份、unit specifier/转义或必须存在却
缺失的 EnvironmentFile 同样明确失败，避免猜测旧 HOME 或丢失鉴权/监听覆盖。迁移完成标记为 `.legacy-storage-migrated` 与 `.legacy-assets-migrated`，仅在授权和配置
发布成功后写入；只有旧 storage 标记的系统仍补做原生模型与缓存迁移，不把旧
foundation 的 `.legacy-migrated` 标记误认为已完成本轮路径兼容迁移。

宿主覆盖位于 `tests/unit/test_foundation_packaging.py`，包括旧 root/普通用户 HOME、无 key
HTTP 调用、新装与管理员鉴权、监听地址与环境优先级、缺省 storage 叶子、真实 ACL 与
SQLite WAL、外部文件/目录模型的范围和 lifecycle 回滚、坏路径拒绝、幂等和中断
重试，以及无 systemctl 的独立进程快照、离线身份/drop-in、自选配置和绑定宿主运行目录的
chroot 分支。独立进程测试模拟离线文件树，不等同于完整 deb 在真实 chroot 中安装的验收。
这些证据不替代独占板端 deb 升级及固定账户实际加载原模型的验收。

原生默认资产兼容已补宿主覆盖：默认路径直接读取 SenseVoice、Matcha、Kokoro、Silero
adapter 常量和 vision catalog；沿用旧 HOME，显式 ASR/TTS/VAD model_dir 与 XDG 缓存相对路径
按旧工作目录物化。管理员 YAML 字节不变，通过低优先级 credentials 补充路径；环境文件
保留备份及优先级。仅授权实际默认模型子树、Gateway 错误日志缓存和有效 XDG 下的视觉
configs 子树，不开放整个 HOME、models 或 XDG 缓存。外部注册模型继续只读授权。

压缩模型下载将锁和下载/解压临时目录置于目标模型目录内，缺文件补下载无需向管理员
父目录写入；ASR/TTS 域目录仅需穿越权限，不授权其兄弟模型。宿主回归以父目录只读、
目标可写的真实权限复现原 PermissionError，并验证补下载、已有文件保留与临时目录清理。
默认目录真实 POSIX ACL 测试同时检查兄弟模型无新增权限及 journal 还原。

以上是源码和宿主证据，不能证明所有旧应用已完成升级验收。旧 SDK 动态 preset/
HuggingFace 缓存、真实 chroot 完整 deb 升级及固定账户板端原模型加载仍待独占验收。
旧 Gateway 未参与资源协议时的 ASR 调用边界仍为未关闭 **P1**，不因安装整改而放宽检查。


生命周期验收区分升级与新装：已安装且运行中的旧 Gateway 在停服前记录 18790/8326
监听地址及无 key 请求的鉴权结果；安装后必须保持一致，仍检查非 root 和八域健康。
没有原包的新装才要求 loopback、默认401及包生成的 key 可用。原服务未运行或无法取得
有效基线时，在修改服务/文件前失败，不把缺失证据当成升级通过。该基础比较不替代
旧 Intent 的真实 Embedding/LLM 推理。凭据解析同时支持 systemd 单引号和双引号 JSON。
