---
name: pdf-supplement-fetch
description: 发现并获取指定论文的官方补充材料，验证文件与论文关联并归档同一Zotero父项；用于SI/ESM补缺，不负责视频解析或重新获取正文。
---

# 官方补充材料

继续当前论文身份、bundle与任务回执，不另建账本。按[工作流](../../../docs/WORKFLOW.md#supplements)确认现有模块和工具；本候选没有独立supplement-only CLI，不能为了补件无条件重下载正文。

1. 核对DOI/题名、正文原件与父项、已有附件及精确匹配的下载文件；已取得的原件先复用。
2. 从身份已核对的官方单篇页面寻找SI/ESM及实际附件链接。页面未查为`not_checked`，已知有附件但未取得为`partial`；空preview不能证明没有补件。
3. 复用`paper_supplements.acquire_supplements`或当前已配置工具，先读实际签名/参数；来源和许可逐项记录。不猜测链接，不把正文图像渲染件当补件。
4. 验MIME/容器、大小、SHA、论文关联；PDF须可读，DOCX须是真实容器。保留原始字节，明确正文/补件/审稿材料角色。
5. 用已有Zotero导入器、版本保护和回读附到同一父项。只在进入解析时加载[mineru-paper-knowledge-base](../mineru-paper-knowledge-base/SKILL.md)，历史已核索引对账后复用。

下载完整不代表全部格式可解析。视频/音频仅保留原件；旧DOC、复杂DOCX、未绑定源SHA复核的PDF文本保持具体待办。报告发现、下载、归档和后续处理的独立结果。
