---
name: paper-to-zotero
description: 获取指定论文的原版正文PDF和官方补件，核对已有原件并归档Zotero；用于DOI或已确定论文，纯证据查询用literature-retrieval。
---

# 论文获取与归档

在当前Codex循环中复用已安装的`papertrail acquire`，不要另建下载器或任务数据库。先读[工作流](../../../docs/WORKFLOW.md#acquisition)的获取部分；`capabilities`和`acquire --help`描述本候选的真实接口及副作用。

- 固定DOI、题名和版本，核现有ledger、bundle、Zotero父项、原件哈希和未决写入；捕获响应缺失时也查用户正常下载目录的精确论文，不直接重下。
- 仅给主题时先使用当前已配置的文献发现工具确定论文；本包不提供完整主题搜索器。没有可用工具时如实说明，不能将离线路由preview当作搜索结果。
- 主文必须是身份匹配的原版PDF。XML/网页/补件不能替代；官方补件逐项发现和保留，阶段到达时加载[pdf-supplement-fetch](../pdf-supplement-fetch/SKILL.md)。
- 使用显式配置和当前任务允许的执行参数；默认preview不是执行完成。正常执行接续Zotero、受支持解析和配置分库；明确download-only/main-only/no-index时缩小范围。
- 正文与补件归同一书目父项的不同附件，用现有版本保护、哈希和远端回读。只修复缺失阶段，不因后续失败丢弃已验证正文。
- CAPTCHA/MFA/必要身份步骤留给用户；普通按钮、保存和核验由代理完成。拒绝访问不以复制会话或猜测路径绕过。

解析阶段加载[mineru-paper-knowledge-base](../mineru-paper-knowledge-base/SKILL.md)。各阶段单独报告状态、论文ID、输出位置和未完成原因；不默认调用Dify或任何未授权模型。
