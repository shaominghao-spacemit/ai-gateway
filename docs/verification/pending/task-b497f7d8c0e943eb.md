# Gateway 安装与运行基础整改

- 任务：task-b497f7d8c0e943eb

## 功能/特性

Deb 构建时收齐带摘要的 Python 运行依赖，安装只消费本地资产；服务使用固定系统账户；默认本机监听且 Deb 自动启用鉴权；环境变量覆盖 YAML；升级、卸载保留配置与模型数据；健康汇总覆盖八个域。

## 用户可见行为

安装不再等待 PyPI。管理和推理入口需要设备密钥，Console 支持手动输入且只保存在页面内存。未加载模型显示未就绪，故障域不再被其它就绪模型遮盖。管理员的环境文件、drop-in 和模型数据不因重复安装消失。

## UI 测试步骤

1. 安装明确 SHA256 的候选 Deb，通过 SSH 同时转发 8326 和 18790，打开 Console。
2. 不输入密钥尝试模型加载，确认请求被拒绝；在密码框输入设备密钥后重试。
3. 在健康接口查看八域状态，加载现有 Silero 模型并执行一次 VAD。
4. 修改管理员配置、环境文件并留下数据，执行升级、remove、重装和 purge，按 README 保留表核对。
5. 执行包内 debian/tests/test-device-lifecycle.py 完成独占板端验收，并检查 restored=true。

## 期望结果

两端口仅监听 127.0.0.1、进程为非 root；有效密钥允许操作且不出现在 URL 或浏览器存储；真实 VAD 后端为 silero；失败状态可见、进程故障能恢复；配置及数据按保留规则存在；原始包和服务状态恢复。Console 的 React/Babel/Chart.js CDN 仍需可达，未将其列为离线运行保证。

## 覆盖的测试
- tests/unit/test_foundation_packaging.py：真实 dh_installsystemd 产出的前端 unit 固定非 root 和本机监听；最终切换故障保留旧环境入口并允许重试。

- tests/unit/test_foundation_packaging.py：资产摘要、ABI、离线安装、失败重试、旧配置迁移及清理边界；标记写入磁盘满后可安全重试且候选目录可供服务账户访问。
- tests/unit/test_foundation_auth_settings.py：配置优先级、HTTP/WS 鉴权和 Console 凭据约束。
- tests/unit/test_foundation_health.py：八域状态、缺失和异常隔离、混合就绪与错误状态。
- tests/unit/test_device_lifecycle.py：设备脚本预检、拒绝依赖变更、异常恢复和证据脱敏；停服后快照、持续 SQLite 写入停止及备份失败只恢复服务。
- debian/tests/test-device-lifecycle.py：真实候选安装、权限、鉴权、保留、Silero 推理、故障恢复和原始状态恢复。

- tests/unit/test_vad_load_health.py：真实服务加载失败与重试恢复、SDK 缺失时 mock 保持降级。
- tests/unit/test_llm_service.py：隔离临时模型目录及本地下载状态源，避免用户缓存或公网速度影响交付回归。

## 本次验收结果（2026-10-15）

- 构建：req-5c3c900aac51474a84a3c5095a5ad97f，artifact gateway-2985cf14a56b，K3DEB1-3。
- Deb SHA256：bdceeaf0110ed20ebcf9cb814d7c0c7bed40ec46b76bb1ea20800701d0c1e997。源码快照 691796d5ad966de3bf00ab28eb98710fa461b34ef8973ed38b859f1cda306312；251 个运行/打包/前端/配置/测试文件与工作树逐字节一致。
- 验收：req-d67d971a60da4d7eab853fa087105a7c，attempt att-0a3a272bf08847969c4f36a44843098d，completed，result=ok、restored=true；R1–R6 与 restoration 已记入平台 verify.tsv。R1 的实际机器覆盖离线安装和 configure 重入，候选环境切换失败后重试由宿主测试覆盖。
- 恢复：原 Gateway 0.0.19，两个原服务 active；独立归档核对 4909 条、0 差异；dpkg --audit 无输出；bibit-voice、ORT、llama 相关包清单前后相同。归档核对包含内容、属主、权限、软链及 xattr，不比较 mtime。
- 宿主：498 passed、33 skipped、33 subtests passed。Console 宿主浏览器鉴权通过；同版本资源替代 CDN 传输，未完成板端 UI 与公网 CDN 可用性验证。
- 公开证据：docs/verification/evidence/gateway-foundation/；截图 docs/assets/gateway-console-auth-host.png。备份含设备配置与凭据，仅留板端受限目录，不纳入 Git。

历史失败不计通过：req-2a8c62581d734f00bd9874e981077b66 暴露 SDK 静默初始化失败；req-6eff87aa9a9549bf87372cb4b532d3b3 暴露验收脚本将 error 误写为 code。两次均已 restored=true，独立恢复核对无差异。最终通过只绑定上述新产物与请求。
