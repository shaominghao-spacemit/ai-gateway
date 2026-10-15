# Gateway 基础整改的后续改进

## P2：配置白名单文件时每个请求同步读取

Gateway 中间件与旧路由依赖可能各执行一次白名单校验。仅在配置文件白名单时发生，默认 Deb 未配置该文件；当前实现保证权限修改立即可见。暂不增加缓存失效逻辑，后续存在实际并发负载时可恢复基于 inode/mtime/size 的解析缓存，并回归文件替换、删除和权限撤销。不得缓存过期许可。

## 已知范围限制

- Console 仍使用外部 React/Babel/Chart.js CDN，Deb 离线安装不等于 Console 离线运行。
- GET/HEAD 保持原有只读接口兼容；默认 loopback 约束访问边界。若要对局域网开放，需同时考虑只读接口的信息暴露。
- 通用 Python 依赖在构建时解析，Deb 内精确版本和摘要固定；不同日期重建的包不保证字节一致。
- 真实模型验收选用现有 Silero VAD；没有把此结果扩展为 ASR/TTS/LLM 所有模型的兼容性证明。

## 简化审查记录

已完成 code-reuse、code-quality、efficiency 三项只读审查。复用建议 0；效率建议采用 2 项：大 wheel 分块哈希、复用构建阶段摘要；文件白名单性能建议以上述 P2 保留。三项 P3 风格建议（异常表达式提取、局部列表、状态枚举字面值）不改变正确性，本轮不扩大修改。

## 最终源码审查与宿主机验证

2026-10-15 逐段复核 Debian runtime/preinst/postinst/postrm、两份 systemd unit、wheel 摘要与 ABI、构建锁刷新、HTTP/WS 鉴权及 Console 调用、配置优先级、八域健康与 VAD 加载失败。采用直接源码审查；当前工具未提供代码图查询。

修复构建 staging 中 wheel ZIP 元数据变更导致摘要失效、视觉模块缺少 cv2 导致服务退出，以及 Silero SDK 吞掉初始化异常仍返回实例导致假成功。VAD adapter 显式检查 SDK is_initialized；HTTP 回归确认 503 与 error=load_failed，健康转为 failed/degraded。生命周期脚本也必须按 error 字段判定，不能用返回码代替健康断言。

最终宿主机：498 passed、33 skipped、33 subtests passed；跳过项包含缺少真实 llama 等环境，不能视为所有模型实测。两条 warning 是 Starlette httpx 弃用与已有 OpenAPI 重复 operation ID。

Console 宿主机浏览器验收：未授权请求 401，授权后的未知模型请求 404，密码输入、仅内存凭据与刷新清空通过，无页面错误。外部 CDN 在本环境不可达；测试用相同版本 npm 资产替代传输，React/ReactDOM/Babel 摘要与页面 SRI 一致，Google 字体被阻断。此证据不能替代板端 UI、公网 CDN 或完整离线前端验收。截图和公开 JSON 在 docs/assets、docs/verification/evidence/gateway-foundation。

经验沉淀判断：以上根因和边界已由实现注释、回归、README 及本文记录，不再重复建立 solutions 文档。
