# PDF知识库整理 Agent

我想把零散的PDF整理成能持续保存、方便查找、可以回到原文核对的知识页。这个项目基于Deep Agents：给定文字PDF和整理重点，模型调用工具读取文件、查找旧页，再选择新建、补充更新或保存待确认草稿。

目前用公开材料证书做了小规模实践，支持生成中文Markdown知识页、知识库目录、来源页码、历史版本，以及本地的原PDF与知识页对照页面。

## 流程与分工

**PDF与整理要求 → 读取原文 → 查找并读取旧页 → 判断保存方式 → 保存知识页与来源 → 更新目录和展示**

维护版提供四个工具：

| 工具 | 用途 |
|---|---|
| `read_pdf` | 按PDF物理页码提取文字 |
| `search_knowledge` | 用关键词、标识符和中文双字片段查找已有页 |
| `read_library_page` | 读取候选正文、来源身份和已记录事实 |
| `commit_page` | 检查保存条件，执行新建、更新或待确认 |

模型选择检索词、读取哪些旧页、整理哪些内容，并提出保存动作。程序执行文件操作，检查来源身份字符串、页码、短引文存在性及部分旧事实的一致性，备份旧版并更新目录。这里的知识库搜索尚未使用向量检索。

| 输入情况 | 处理方式 |
|---|---|
| 文件字节完全相同 | 程序按SHA256复用当前页，不调用模型 |
| 主题相关，但证书、产品或批次不同 | 新建独立页，必要时关联 |
| 同一资料补齐内容，支持原有关键事实 | 更新原页，保留历史版本 |
| 模型提出待确认，或保存时发现已记录事实冲突 | 保存草稿，保留当前页 |

## 已有运行记录

四次维护记录从已有本地会话中整理，动作和判断见 [runs.json](examples/runs.json)，可以直接打开 [demo.ipynb](demo.ipynb) 查看。

| 输入 | 实际结果 | 观察 |
|---|---|---|
| 管材证书单页摘录 | 新建 | 保存18条事实记录 |
| 同源完整4页证书 | 更新同一页ID | 保留18条旧事实，新增9条；一次提交因未读取关联页被拒绝，补读后保存 |
| WCB材料证书 | 新建并关联 | 与已有材料身份不同，没有覆盖管材页 |
| NST Carbomig 3N证书 | 新建并关联 | 与已有2N证书的产品、证书号和炉号不同，分别保存 |

管材摘录与完整版来自同一原文档，四次记录涉及三份不同原文档；初始知识库已有一篇2N焊丝页。这个更新案例检查的是“摘录补齐”，尚未验证真实修订版冲突。

另有 [本地检查记录](examples/checks.json)：注入一项已记录事实冲突后，保存转为待确认且旧页不变；重新加入旧摘录也没有回退当前页。这些是程序检查，不代表模型已能自主识别所有矛盾。

[知识页节选](examples/知识页节选.md) 展示新证书信息及与旧页的关系。运行记录为节选，完整工具返回、原PDF和本机路径没有上传。

## 安装与运行

本地使用Python 3.12.15。依赖固定在 [requirements.txt](requirements.txt)，完整环境版本另存为 [requirements.lock.txt](requirements.lock.txt)。

```bash
python -m venv .venv
```

Windows PowerShell：

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

在仓库根目录的 `.env` 填写自己的 `DEEPSEEK_API_KEY`。当前读取此文件；密钥不要写进Notebook。模型默认 `deepseek-flash`，可通过同文件中的 `DEEPSEEK_MODEL` 调整。模型可用性取决于服务端，换模型后的工具行为需要重新检查。

打开 [demo.ipynb](demo.ipynb)，选择安装好依赖的Python内核。前半部分只展示已保存案例；实际处理部分修改 `PDF_PATH` 和 `TASK` 后运行。也可以直接调用：

```python
from presentation import process_pdf, show_presentation

session = process_pdf(
    "samples/input.pdf",
    "整理主要信息、条件、数值和表格，核对已有资料，保留页码依据。",
)
show_presentation(session)
```

使用同一个知识库目录可以持续积累资料。完全相同的文件再次加入时会复用已有页，即使更换任务也不会重新整理；比较不同任务时可指定另一个 `library_dir`。

新文件会将提取的文字发送到DeepSeek并产生API费用。直接查看GitHub里的已有案例不请求模型。

## 文件与结果

| 文件 | 内容 |
|---|---|
| `demo.ipynb` | 已保存案例和更换PDF运行入口 |
| `knowledge_maintenance.py` | 查找、身份及事实比较、新建/更新/草稿保存 |
| `knowledge_agent.py` | PDF读取、模型配置、知识页保存及公共函数；保留初版入口 |
| `agent_runtime.py` | 工具范围约束与公开调用记录 |
| `presentation.py` | 处理入口、知识库浏览和原PDF对照HTML |
| `prompts/` | 初版与维护版的系统提示词、默认任务 |
| `examples/` | 来源、已有运行记录节选、本地检查与知识页节选 |

本地运行后，`knowledge_base_v2/pages/` 是当前知识页，`sources/` 是PDF副本，`history/` 保留旧版本，遇到待确认时写入 `drafts/`。`knowledge_base_v2/首页.md` 提供目录。`outputs/` 保存运行日志、提议和本地对照HTML，均由程序生成并被Git忽略。

## 离线检查

```bash
python -m unittest discover -s tests -v
```

四项测试使用合成PDF，检查新建与补充更新、历史保留、相同文件复用、事实冲突转草稿、身份不匹配及无效引文拒绝。测试替换了模型客户端，不请求API。

整理时还检查了模块导入、Notebook前半段的离线展示、文档链接、公开记录与本地日志的一致性。本次沿用已有Python 3.12环境，没有从零安装完整依赖，也没有重新请求DeepSeek；原PDF的当前下载可用性和其他模型尚未重新验证。

## 当前限制

- 支持未加密、有文字层、1～12页、30MiB以内的PDF。尚未接入OCR、图片和Word。
- 查找采用简单词项匹配，会重复检索或建立偏宽的主题关联。没有跨文档主题综合、知识库问答和自动巡检。
- 部分复合事实的引文只覆盖一部分内容，表格仍可能过宽。引文存在、身份相同和保存成功都不能证明全文正确。
- 初版2N焊丝知识页曾加入原文没有说明的“通常为质量百分比”，并过度解释Typical data；旧记录保留在原项目，本仓库的案例节选不代表这些问题已普遍解决。
- 事实比较只覆盖结构化记录，不是全文语义冲突检测。项目不做工程合规判断，也不是液化空气或其他企业的官方项目。

后面我想先补充相似编号、缺少身份信息和真实修订版的样例，检查新建与更新的边界，再改进检索和表格表达。

参考：[Deep Agents](https://docs.langchain.com/oss/python/deepagents/overview)、[LLM Wiki思路](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)。这里借鉴知识页、来源、关联和持续维护的组织方式，Agent运行使用Deep Agents框架。
