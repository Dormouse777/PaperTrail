---
name: mineru-paper-knowledge-base
description: 将已取得的PDF和受支持DOCX通过已有解析、结构切块和RAGFlow导入模块处理，核对页/块覆盖与源哈希；用于解析入库和中断恢复，查询已有证据用literature-retrieval。
---

# 解析与入库

只处理本次已选材料，复用现有模块，不重新下载原件。[工作流](../../../docs/WORKFLOW.md#processing)列出实际入口、配置和验收边界。

- 先绑定论文ID、Zotero父项/附件键、材料角色、源SHA、PDF物理页数、作业状态和索引映射。正文/SI/审稿PDF的页数不能混算；未知写入先回读，不重复提交。
- 正常获取链已接续处理，修其缺失阶段。独立PDF使用候选中的`local_pdf_pipeline.py --help`核参数后调用；MinerU服务另行配置，demo不代表该服务可用。
- PDF复核所有物理页、Markdown、结构JSON和图片资源。DOCX沿用原生段落/图注/简单表格适配与源绑定，不编造页码；精确tokenizer门、复杂公式/表格和图内信息限制须保留。
- 复用`prepare_chunks`、`bundle_materials`及现有导入/对账；正文和补件进入显式配置的目标。PDF补件文本复核绑定源SHA；不以取消核验提高导入量。
- 保留原件、资源与provenance；处理不包含删除原件或直接改Zotero数据库。旧DOC、视频和音频解析没有实现。

上传、全页解析、切块、索引回读和实际检索逐项验收。Embedding需当前使用者授权并报告用量/估算费用，不继承项目作者的私人grant；不默认Dify或回答模型测试。
