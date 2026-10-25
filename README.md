# PaperTrail — Traceable Literature Workflow

PaperTrail将文献获取、Zotero归档、文档解析、结构切块和RAGFlow证据检索串联起来，保留来源哈希、章节和页码，区分正文与补充材料。项目复用现有Python业务代码，由Codex单主循环按阶段选择工具。

PaperTrail（论文可追溯路径）是面向科研文献处理的Python集成项目：把获取、原件校验、归档、解析、索引和证据引用作为可单独核验的阶段，处理补件隔离和中断恢复，而不把“下载成功”当成“回答可信”。

**当前为发布前候选。** 可独立安装并运行离线预览和合成样例；真实下载、Zotero、MinerU、RAGFlow及模型服务需要分别配置和验收。完整服务链的新环境复现尚未完成，未发布到GitHub/PyPI。

```mermaid
flowchart LR
  A[DOI / 已有原件] --> B[获取与身份核验]
  B --> C[Zotero与材料bundle]
  C --> D[PDF / DOCX适配]
  D --> E[保留章节和位置的切块]
  E --> F[正文 / 补件分库]
  F --> G[检索证据与引用核查]
```

## 安装和一分钟演示

需要Python 3.11或更新版本；当前本地安装证据为Windows/Python 3.12.14。以下命令在此源码仓库根目录运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\python.exe -m papertrail --version
.\.venv\Scripts\papertrail.exe demo
.\.venv\Scripts\papertrail.exe preview --doi 10.1000/example
.\.venv\Scripts\papertrail.exe capabilities
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

macOS/Linux使用`.venv/bin/python`及`.venv/bin/papertrail`；这些平台尚未实测。`demo`读取自写一页PDF及手工CanonicalDocument fixture，复用现有切块、证据门和引用核查逻辑，输出两类章节与拒答检查。它不调用MinerU或真实检索，不评价科学正确性，固定分数不是模型分数。

`preview`只给离线路由计划，不联网，不读取凭据，不下载，不写业务状态。示例DOI为演示标识，不表示真实论文。所有命令的具体参数用`--help`查看。

## 配置真实业务（尚未做新环境整链验收）

```powershell
Copy-Item src/papertrail/_legacy/Paperdownload/workflow.example.json workflow.local.json
.\.venv\Scripts\papertrail.exe inspect-configuration --configuration workflow.local.json
```

路径相对配置文件所在目录。设置自己的下载、bundle、prepared、正文库/补件库与state目录；占位dataset ID和`confirmed: false`不是已验证目标。索引默认关闭；显式启用需要`workflow.embedding_enabled: true`和一致、已确认的main源。RAGFlow凭据只来自使用者环境，公共CLI禁止借用私人Docker容器身份。不要把真实配置或密钥提交到仓库。详细流程和验收见[工作流](docs/WORKFLOW.md)。

请求文件由调用者提供，例如`{"version":1,"paper":{"doi":"10.1000/example","title":"Synthetic request"},"channel":"auto","include_supplements":true}`。默认先预览：

```powershell
.\.venv\Scripts\papertrail.exe acquire --request request.json --configuration workflow.local.json
```

`--execute`进入真实工作流，各下载/许可确认参数见帮助；需要使用者自行确认来源、服务与费用。正文和官方补件逐阶段记录结果；仅取得文件不等于已归档、解析或索引。`--download-only`停止在获取后，`--no-index`保留归档/本地处理但关闭索引。未知写入结果应先核原作业、bundle和远端状态，不盲目重试。

| 组件 | 此候选的边界 |
| --- | --- |
| Python基础依赖 | `pypdf==6.10.0`；不复制已安装环境 |
| 精确本地tokenizer | 可选`pip install '.[tokenizer]'`，锁定`tokenizers==0.23.2`；自备匹配模型且来源/许可已核的tokenizer JSON。未测试该extra的全新环境安装；未配置时PDF切块使用启发式估算，DOCX索引仍需原有精确tokenizer门 |
| Zotero/MinerU | 需单独安装；现有本地接口默认23119/8000，尚未全面端点参数化；Zotero使用现有本地授权协议，Windows可按服务身份从系统凭据管理器复用本人已记住的grant。候选不携带这些grant，也不将它们改写为环境变量；不包含程序或模型 |
| RAGFlow | 外部服务和新建库；`RAGFLOW_API_KEY`用于索引，检索还需`RETRIEVAL_API_TOKEN`；HTTP地址和dataset/state由配置给出 |
| 模型费用 | 公共版本没有任何个人持续授权；索引需明确配置。重排序、生成、ASR、视觉与付费云解析不能借用Embedding配置授权 |
| Dify | 包含纯函数证据/引用核查及源码拼装依赖，不包含Dify服务、卷或默认回答测试 |
| MCP/部署恢复 | 不属于本次最小安装面；绑定旧部署/SDK源码的组件未装入此wheel |

## 模块、数据和维护

安装入口为`papertrail.cli`。业务模块保留相对布局，装在内部`_legacy`命名空间；薄入口仅在独立CLI进程添加这些目录，对外提供命令接口，暂不承诺可嵌入的Python库API。Dify源码生成器所读的相邻`.py`文件和路由JSON一同打包。原开发工作区通过白名单导出此候选，导出副本不作为另一套并行维护来源。

`export-manifest.json`记录每个导出文件的相对来源、长度与SHA-256，供核对来源；它不代表秘密扫描或发布批准。导出不含uni、真实论文、凭据、浏览器会话、原业务台账、生产配置、数据库/卷、上游checkout、运行历史或安装环境。

工程取舍：同一ledger/bundle/job链记录各阶段事实；原件与未知写入先对账，减少重复下载/导入；正文/补件显式分流；引用门拒绝无来源或未知标签，但不声称能证明科学解释正确。合成样例验证的是这些边界，不报告虚构的检索准确率、Agent效率倍数或论文成功率。

## Codex技能与上下文

[AGENTS.md](AGENTS.md)约定一个主循环、按阶段加载和既有任务报告中的恢复检查点。[获取](.agents/skills/paper-to-zotero/SKILL.md)、[补件](.agents/skills/pdf-supplement-fetch/SKILL.md)、[解析入库](.agents/skills/mineru-paper-knowledge-base/SKILL.md)、[证据检索](.agents/skills/literature-retrieval/SKILL.md)、[导出目录整理](.agents/skills/md-literature-organizer/SKILL.md)各有独立触发范围；没有默认规划/路由子Agent、全量技能预加载或另建记忆数据库。

技能放在源码仓库的`.agents/skills`中，wheel只安装业务CLI和资源，不替使用者修改全局Codex设置。使用源码任务时按当前Codex环境发现对应技能；不自动安装第三方工具。查询技能明确要求已配置且核验过的检索工具，本候选没有默认部署好的检索桥。

本项目自有代码、文档、公开技能及合成样例采用[MIT许可证](LICENSE)。贡献与测试见[CONTRIBUTING.md](CONTRIBUTING.md)，第三方依赖/外部服务边界见[LICENSING.md](LICENSING.md)。

[CI配置](.github/workflows/offline-tests.yml)会构建wheel、装入新环境，并在源码目录外运行离线检查；仅设置Windows/Python 3.12。它不需要业务密钥、不调用模型或服务；安装依赖仍需包源网络。使用官方[checkout](https://github.com/actions/checkout)和[setup-python](https://github.com/actions/setup-python)入口。本地等价检查与实际GitHub Actions运行分开报告，目前尚未远端运行，无成功徽章。

## 已知限制与公开前工作

- 当前自动获取实现仍为已核本地→OA→出版社→机构；项目目标D2→D1→D3尚未完全对齐，机构流程暂停。
- 只支持已有PDF和DOCX文档适配。旧DOC没有接通适配；视频/音频只保留原件，未实现解析。OCR、公式、复杂表格和科学解释需要按原文复核。
- 机器默认值只从未导出的本地sidecar读取；分发源码无个人目录、容器身份或持续模型grant。缺sidecar时使用通用data路径，索引关闭。公共CLI仍强制显式配置；能安装不代表任意机器完整服务链可用。
- 已选定MIT；第三方依赖适用其各自许可，未将RAGFlow/Dify整体源码打包或重新授权。完整归属核对、真实服务复现及最终发布审查不能仅由许可证选择替代。

构建与隔离方式依据[setuptools官方配置文档](https://setuptools.pypa.io/en/latest/userguide/pyproject_config.html)和[Python venv官方说明](https://docs.python.org/3/library/venv.html)。具体通过项与环境证据保存在本机验证回执，不把私人回执整份公开。
