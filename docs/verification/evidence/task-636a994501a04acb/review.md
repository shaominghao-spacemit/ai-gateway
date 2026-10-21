# 本轮修复专项复审

范围：Gateway未提交修复相对e4e28ce、配对Voice相对0ce3cfe；不含历史WIP。
首轮专项复核发现2个P1和1个P2，均已补修：
- 自定义User drop-in在配置原子替换后ACL丢失：按与preinst相同的同名/范围/文件排序判断最终User，排除精确旧生成文件；unit/dropin及aa/zz回归覆盖。
- purge归档后命令失败未跟踪journal：purge_with_acl_recovery在finally追踪本次归档，首轮恢复后清引用；模拟归档后抛错的回归覆盖。
- recovery_secret中surrogate编码失败500：资源获取前将UnicodeEncodeError转换为422，真实HTTP使用转义JSON验证。

第二轮只读专项复核未发现仍可证明的P0/P1；独立6项内存断言验证drop-in顺序。
最终Gateway全量严格串行执行：Python3.12与兼容Python3.10各876 passed、33 skipped、83 subtests passed；Voice224 tests OK。
一次并行全量曾互相发现对方fake engine而失败，保留失败记录，不改production排除逻辑，最后串行两套均通过。
不以审查或本机测试替代真实systemd/ACL、双模型、安装/purge/recovery板验。

最小化：仅保留必要协议/权限/生命周期修复；未增加依赖、未改变公开源TLS策略、未做无关架构重构。
交付：未提交/未合入/未推送，Gateway新包因files.pythonhosted.org TLS网络错误三次构建失败，暂不部署。


最终实机补验：完整安装升级purge及管理员非root ACL、HTTP GET/HEAD与chunked上传、最终双模型配对、4x8并发health、GDB真实推理阻塞锁寿命与原现场回滚通过。新增Voice旧包回滚助手12行补修已最小行为回归及225全量通过，并由最终包提取的同字节助手实际运行验证。
工具失误不计生产问题或通过证据：初轮函数名覆盖标准库、CORS断言过严、精简回调遗漏mutated；已立即使用原备份完整恢复并补正确标记后重跑成功。报告子段旧schema标签列为P3记录，不声称全仓安全审计或物理麦克风测试。

## 当前补修核验（取代上文旧标签跳过结论）

报告子段的contract已补修为managed-asr-v2，不再作为P3跳过。改动只涉及生命周期报告初始化及一条实际构造断言；调用处仍使用同一managed_evidence，缺模型用例仍not_run/not_passed，不放宽任何通过条件。包内与归档helper SHA256同为d199a8768eca7c36629c2abbe52a17f322078f13ad3fb946cd49353ae79b560b，与当前工作树逐字节一致。
补修后双Python全量各876项通过；本回合又在宿主Python3.14执行生命周期helper完整unittest，77项通过，日志gateway-report-v2-helper-py314.txt。该宿主测试使用包/服务mock，不是新包systemd或SDK运行时通过证据，也不是新一轮全仓审计。
新包gateway-7ec492e9ea6c及配对voice-a847a0a5f37d已同板构建；构建DNS原字节/权限/解析已恢复。新包含Python3.14/NumPy2.5.4，需独立验收，不沿用旧包推理结果。新生命周期请求的独占attempt在deploy_started后因SSH No route to host转recovering，安装及恢复进度未知；双模型联合仍排队。当前不具备合入/更新MR条件，未提交报告补修、未合入或推送。
覆盖限制：当前工具无codebase-memory索引/coverage接口，本轮仅直接读取补修行、调用处及完整测试；不声明图索引完整、全仓无问题或新运行时安全验收完成。

## 新包验收及最终审查结论

上述受阻状态现已由实际恢复及新验收替代：用户重启后在原保留租约与生命周期互斥锁中复用原helper恢复0.0.19，4263原文件内容和外部数据库通过；原attempt保留interrupted，不改为通过。新配对双模型联合与新完整生命周期均独立completed/result=ok/restored=true/safe_to_release=true、occupancy=[]。最终只读dpkg --audit干净、原包/服务及DNS恢复。
新Python3.14/NumPy2.5.4包真实双模型与安装兼容已证明，包内报告为v2且缺用例仍not_run；最终宿主全量876/33/83通过。当前两行源码补修没有引入已确认P0/P1，未扩大原修复范围；旧GDB锁寿命、并发health、HTTP边界记录保留为同源码历史专项，不声称新包重复执行过。
原卡死后111个FastAPI零字节文件已通过恢复清除，未证明其成因。硬件卡死根因仍是剩余风险；远端MR仍未更新，交付平台对Gateway本地main历史门禁需处理，不绕过钩子或直接推main。
配对Voice交付规定的225项回归已重新通过。一次裸unittest discover共241项有1项dh_installdeb权限错误（该测试未变且仓库明确要求fakeroot），保留失败日志，不修改测试跳过；按原文件注释fakeroot运行打包10项全部通过。完整fakeroot全量另行执行并保留结果，不将裸命令失败包装成成功。
最终按仓库文档fakeroot完整全量241项全部通过（61.946秒），证据voice-report-v2-fakeroot-full.txt；未修改测试或提交环境权限绕过。
