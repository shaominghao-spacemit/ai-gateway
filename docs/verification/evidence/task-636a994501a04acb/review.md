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
