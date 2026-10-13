---
name: ppt-report
description: 'Generate an editable Chinese .pptx presentation (medical, scientific or general topics) with rich text-image layouts: web images, generated diagrams, charts, tables and numbered references. Workflow: clarify requirements, get the user to confirm the outline (revise as many times as needed), then generate and render-verify. Use for slides, presentations, 幻灯片, 演示文稿 or PPT; use word-report for .docx and html-report for web reports.'
requires: [report_run_generator, report_list_styles, ppt_propose_outline, ppt_confirm_outline, ppt_verify]
---

# 生成学术 PPT 报告

> **在 LChain 项目中使用（路径映射）**
> - 文中的 `/repo` = 报告工作区（`data/report_workspace`），`/repo/o_outcome` = 最终报告目录；`/repo/skill/common/ppt-report/scripts/...` = 本项目 `skills/ppt-report/scripts/...`。
> - 用 `report_write_file` 把 spec / 骨架写到 `/repo/threads/<会话名>/`，再用 `report_run_generator(skill="ppt-report", spec_path=...)` 生成；**不要自己拼命令行**，也不要手写最终文件。
> - 图片 `path` 必须是工作区内已存在的文件（`/repo/...`）：用 `ppt_download_image` 下载的联网图片、`ppt_make_diagram` 绘制的示意图都会返回可直接使用的路径。不要编造不存在的路径，也不要把联网图片或示意图说成患者原始资料或研究数据。
> - 生成后用 `report_list_outputs` 查看，下载地址为 `/api/reports/<文件名>`，把它告诉用户。

## LChain 对话流程（强制，由系统门禁保证）

在对话中制作 PPT 必须按下面 5 个阶段进行。流程状态保存在 `/repo/threads/ppt-<会话id>/workflow.json`，每轮系统提示词末尾会给出「本会话 PPT 流程状态」和下一步要求，以它为准。大纲字段、示例和图文混排做法见 [references/workflow-outline.md](references/workflow-outline.md)。

| 阶段 | 你要做的 | 工具 | 停下来等用户？ |
| --- | --- | --- | --- |
| 1 需求整理 | 从用户消息中提取主题、受众、用途/场景、页数或时长、风格、已有素材/数据、要强调的重点、图文偏好。**关键信息缺失**（主题不清、受众/用途无法推断）时列出 2~5 个具体问题，放入 `open_questions`；次要信息自行给出合理默认值，写入 `assumptions` | `ppt_save_requirements` | 有 `open_questions` 时：发出需求摘要和问题后**结束本轮** |
| 2 大纲确认 | 设计逐页大纲（标题、版式、核心观点、要点、配图计划与来源），提交后把返回的大纲 Markdown **原样**发给用户。用户提出修改（增删页面、调整顺序、换配图、改重点……）就修改后重新提交**完整新版本**，可反复多轮 | `ppt_propose_outline` | **每次提交大纲后都要结束本轮** |
| 3 记录确认 | 用户明确表示确认（如「确认」「可以，开始做」）后才调用。回复里同时带有修改意见时不算确认，先改大纲 | `ppt_confirm_outline` | 否 |
| 4 素材与生成 | 按已确认大纲准备配图（联网图片 / 示意图 / 图表 / 表格），把 spec 写到会话目录 `spec.json`，**每页 title 与大纲一致**，然后生成 | `ppt_search_images`、`ppt_download_image`、`ppt_make_diagram`、`report_write_file`、`report_run_generator` | 否 |
| 5 渲染验证 | 生成后必须调用验证：检查缺页、配图落实、图文密度、中文字体、文字越界、空白页，并出预览图。有问题就修 spec 重生成再验证，直到通过 | `ppt_verify` | 通过后交付 |

门禁规则（违反时工具会直接拒绝）：

- 最新大纲未经用户确认时，`report_run_generator(skill="ppt-report")` 拒绝生成；spec 必须放在本会话目录，且大纲中的每一页都要在 spec 中有同名页。
- `ppt_confirm_outline` 只接受大纲提出**之后**用户新发的确认消息，不能替用户确认，也不能在提交大纲的同一轮确认。
- 需求有变化时重新调用 `ppt_save_requirements`；已确认的大纲会失效，需要重新出大纲并确认。
- 交付后用户要求改内容或结构时，先提交修改后的大纲并确认；只调字号、换图、修错字这类排版微调，可以直接改 spec 重新生成并验证。

### 图文混排要求（本项目覆盖下文「不设最低图数」的说法）

- 默认图文密度 `rich`：**至少 50% 的内容页**带图片、示意图、图表或表格；用户要求简洁时设为 `balanced`（≥30%）；只有用户明确要求纯文字时才设为 `minimal`。大纲和成品都会检查这个比例。
- 主力版式是 `columns`：左边写 2~4 条要点/一段结论，右边放图（`ratio` 取 `[1,1]` 或 `[1,1.2]`）；表达流程、时间线、结构的页面优先用示意图，不要堆文字。
- 需要照片感的场景图、人物插画、封面/章节配图，且联网找不到合适图片时，可以用 `generate_image` 生成（见 imagegen skill，大纲 visual.source 写「AI 生成」）；生成较慢，一份 PPT 控制在 3~4 张以内，caption 注明「AI 生成示意图」。
- 素材来源的优先顺序：①用户提供的资料/数据 → ②`ppt_make_diagram` 根据讲解内容绘制示意图（flow 流程 / timeline 时间线 / cycle 循环 / compare 对比 / hierarchy 层级 / cards 要点卡片）→ ③`ppt_search_images` + `ppt_download_image` 联网找实物照片、场景图、结构图 → ④有真实数据时用 `chart`，逐项对照时用 `table`。
- 联网图片只能用作说明/示意，`source` 写明网站；不能当作患者影像或研究证据。示意图的 `caption` 要注明「示意图」。**不得为了配图编造数据**，没有可靠数据时用示意图或文字说明。
- 每页文字精简：要点 ≤5 条、每条 ≤30 字；细节放到备注 `notes`。

### 交付回复

验证通过后告诉用户：下载地址 `/api/reports/<文件名>`、预览图（`ppt_verify` 返回的 Markdown 图片）、总页数/配图数/图文页比例、验证结论；然后询问是否需要调整。验证工具报告「未完成渲染验证」时要如实说明。

## 自定义样式（LChain，三种报告通用）

默认是学术蓝绿样式（academic）。**用户提到风格、配色、品牌、字体、"好看一点""换个颜色"，或者报告用途明显不适合学术风格（如商务汇报、科普宣教、机构正式文件）时**，按下面做：

1. `report_list_styles` 查看可用样式：内置 9 个预设 academic 学术蓝绿 / business 商务蓝 / tech 科技蓝紫 / fresh 清新绿 / warm 暖橙 / hospital 医院红 / elegant 典雅紫 / minimal 极简黑白 / dark 深色（仅 PPT/HTML），以及用户保存过的样式。
2. 根据用户描述选预设，或在预设上覆盖颜色/字体；用户给了品牌色（如"主色用我们医院的 #9E1B32"）时写进 `colors.primary`。
3. 不确定用户是否满意时，先用 `report_preview_style` 生成样张图发给用户确认；用户说"以后都用这个"时用 `report_save_style` 保存为命名样式。
4. 在 spec 顶层写 `style`，其余内容照常写；生成结果里会打印「样式：…」一行，交付时告诉用户用了什么样式。

```json
"style": "business"
```

```json
"style": {
  "preset": "hospital",
  "colors": {"primary": "#9E1B32", "accent": "#1F4E79"},
  "fonts": {"cn": "思源黑体", "en": "Arial", "heading_cn": "宋体"},
  "table": "header_fill",
  "cover": "band",
  "font_scale": 1.1,
  "brand": {"label": "XX 医院 · 质控报告", "footer": "XX 医院医务部 2026"}
}
```

| 字段 | 说明 |
| --- | --- |
| `preset` | 基础预设或已保存样式名，其余字段在它之上覆盖 |
| `colors` | `primary` 主色（标题栏、表头、章节色）、`accent` 强调色（引用、链接、项目符号）、`text` 正文、`heading` 标题、`muted` 次要文字、`border` 分隔线、`alt` 浅底、`highlight` 封面细线点缀、`background` 背景、`chart` 图表系列色（2~6 个）。只改 primary/accent 时标题色和图表色会自动联动 |
| `fonts` | `cn` 中文、`en` 西文、`heading_cn` 标题中文字体。字体需要打开文件的电脑上已安装，否则会回退系统字体；优先用常见字体（微软雅黑、宋体、黑体、思源黑体、Arial） |
| `table` | `three_line` 三线表 / `header_fill` 表头填充主色 / `grid` 全框线 / `auto` 默认 |
| `cover` | `classic` 白底封面 / `band` 主色色带封面 |
| `font_scale` | 正文字号缩放 0.85~1.25（Word/HTML；PPT 不支持） |
| `brand` | `label` 封面小标签（PPT 页脚左侧），`footer` 页脚/文末说明 |

- 颜色会做可读性检查（正文与背景对比度 ≥4.5 等），不通过会报错并说明哪个颜色要调深/调浅，按提示修改后重试，不要绕开。
- Word 不支持深色背景（`dark`）。
- 样式只改变颜色、字体、表格和封面形式，不改变版式尺寸与排版规则，上文关于内容、字号下限、分页的要求照常适用。
- 以下原文中提到的「默认白底、深蓝、青绿」等配色描述，指的是默认样式 academic。

---

以下为 medex 原版说明（生成脚本、spec 字段、版式与校验规则），写 spec 时照此执行。

把经过核验的报告内容生成为 16:9、Office Open XML Transitional `.pptx`。正文、表格和图表图形可编辑；图片以二进制嵌入，离线分享不会丢图。以 Microsoft PowerPoint 2016 为最低兼容基线，同时使用离线 ECMA-376 5th Edition Transitional XSD 与 Microsoft Open XML SDK 严格验证。

## 必须保留的生成与发布门禁

唯一的生成入口：

```bash
python /repo/skill/common/ppt-report/scripts/generate_ppt.py \
  --spec "<已写入 /repo 的 JSON spec 绝对路径>" \
  --out-dir "/repo/o_outcome"
```

- 最终产物必须为 `/repo/o_outcome/{报告名称}_{yyyy}_{mm}_{dd}_v{N}.pptx`，以脚本打印的 `Output:` 为准。spec 放在 `/repo/threads/<当前会话目录>/` 等中间目录，不放在输出目录。
- 默认省略 `--name`，使用 spec.title 命名；省略 `--date`、`--version`、`--out`、`--force`，自动使用当天已有最大版本 + 1。保留历史文件，不删除、改名或覆盖历史版本。明确要求覆盖时才能同时使用显式目标与 `--force`。
- 脚本在共享 COSFS 输出目录排他预留目标版本，以隐藏 `.candidate.pptx` 构建，完成全部门禁才原子重命名发布；失败删除本次候选并释放本次预留。禁止绕过此过程。
- 生成完成后必须重新读取已写出的 PPTX，逐页验证实际主体，覆盖自动续页与文献页；输入 spec 通过不能代替成品检查。仅明确的封面/章节页允许只有标题，发现普通内容页只剩标题、图注、来源或页码时，必须报错并阻止发布，修复后重新生成、重新验证。
- 必须同时通过内容完整性与内部包/关系/图片/画布校验、固定 `/usr/bin/xmllint --nonet` 离线 XSD 校验、固定 `/usr/local/bin/medex-openxml-sdk-validator --target Office2016 --file <候选绝对路径>` SDK 校验。校验程序必须 root 所有且不可被组/其他用户写入；缺失、不可信或超时即失败，禁止通过 PATH/环境变量替换或降级交付。
- SDK 固定版本源码、依赖锁和镜像安装见 [validators/openxml-sdk/README.md](validators/openxml-sdk/README.md)。不得在生成期间下载 NuGet 或把构建产物塞入 skill。
- 成功输出同时包含 `Output:`、`Content validation: ... passed`（成品检查页数及明确标题页数）、`Compatibility: ... passed`、`Strict validation: ECMA-376 ... passed`、`Strict validation: Microsoft Open XML SDK passed ...`。内容检查成功表示成品包中有有效主体，真实可见性仍须按下述流程渲染核对。报错时报告实际原因，不自行拼装最终文件或用 HTML/Word 兜底。
- 不用 Write 写入或重存二进制 `.pptx`；spec、图片只允许 `/repo` 下绝对路径，符号链接不得逃逸。输出只能是 `/repo/o_outcome` 的直接子文件。

资源上限：spec 4 MiB、每张图片 64 MiB、媒体总量 256 MiB，含自动续页和参考文献最多 200 页。

## 内容策略：按讲解需要选择版式

根据每页讲解目标选择最清楚的版式。纯文字、要点、封面和章节页都是正常选择；只有展示所见、趋势、对比或关系能帮助理解时，才加入图片、图表或表格。单图、单表、独立图表与混排均可按需使用，不设最低图数、图表比例或每页混排要求。学术性来自准确内容、清晰结构和可核验证据，版式保持统一即可。

- 原始检查、影像所见、结构或流程适合图片与紧邻说明；先查找当前患者附件中的可用原始截图/图片，必要时提取 PDF 相关页。有助核对字段、位置或解释所见时应嵌入对应原图，不能只留 OCR 文字或链接。保留原始文件，转换格式、旋转、定位标记和局部放大另存派生副本；不得补写、擦除或重绘临床信息。外部来源图片须可核验且可使用，自绘解释图标明“示意图”；不要用装饰图充当医学证据，也不要把生成图片称为真实检查影像。
- 需要可视化比较同一指标的分组数值时使用柱状图；呈现有序时间点的趋势时使用折线图；需要逐项对照方案、证据等级或不同单位指标时使用表格。没有可靠数据时用文字说明已知内容与局限，定性示意按需使用；禁止为画图编造数值、误差线、样本量或统计显著性。
- 需要并排阅读相关内容时可用 `columns` 做“说明 + 图片”“结论 + 图表”“图片 + 表格”等混排；复杂表格、长文献和细节图片使用独立页。长图应保留原图上下文，并另做有明确区域标记的局部放大页，不能只把扫描长图缩得很小。
- 涉及外部科学结论、诊断标准、风险数字或治疗建议时，无论是否配图，都要核对原文/指南，使用 `citations` 连接到顶层 `references`。脚本在每页底部列出该页实际引用的文献，文末保留完整参考文献页。关键患者事实还应注明检查名、日期或原始文件来源。
- 文献优先来自本次已核验的 `/repo/e_evidence/` 等资料；需要补充时通过可用检索工具核实题名、作者、年份、期刊/机构、DOI/URL及实际支持的结论。不要根据记忆编造 DOI、作者或论文。未核实的内容应说明不确定性，不能伪装成已核验文献。
- 需要视觉说明且有合适原图或真实数据时主动纳入；缺少素材时可保留文字，仅在缺口影响内容解释时说明，不必为补位另造流程图。文献按实际论断需要引用，不凑数量。

## 输入与版式

使用 JSON `title`、可选 `subtitle`/`author`、`slides`、可选 `references`。准备混排、图表或引用前，读取完整字段与可直接改写的示例：[references/multimedia-spec.md](references/multimedia-spec.md)。插入患者资料时同时参照其中的[原图与细节页](references/multimedia-spec.md#患者附件图片原图与细节页)，核对实际图片路径、附件来源和放大区域。

支持页型：

| 页型 | 用途 |
| --- | --- |
| `title` / `section` | 封面与章节过渡 |
| `bullets` | 要点，项可为字符串或 `{text, level, citations, source}` |
| `text` / `paragraph` | 连贯说明 |
| `image` | 保持宽高比的完整图片、图注、来源和替代文本 |
| `table` | 可编辑表格，自动测量行高、重复表头、生成续表页 |
| `chart` | 柱状/折线图、图例、单位和对齐的原始数值表 |
| `columns` | `left`/`right` 内容块数组，支持文字、图片、图表和表格混排 |
| `two_column` | 兼容旧版左右要点数组 |

内容页必须有与标题对应的可见主体：文字、要点、图片、实际数据表或图表任一种即可。仅当骨架明确安排封面或章节过渡时，才使用 `type: "title"` / `"section"` 允许只有标题；普通内容页不能因缺正文改成标题页。`notes`、图注、来源、页底文献和页码都不替代正文。

正文必须放在页型支持的字段里：要点用 `bullets`，段落用 `text`，表格用 `headers`/`rows`，双栏用 `left`/`right`。不要套用 Word/HTML 的 `items`，也不要把正文写到未支持的 `content`、`body` 或 `blocks`。字段误用或正文缺失时，按报错中的页号、标题和字段修正 spec 后重跑；不能删除内容、填占位句或将内容藏进备注来通过检查。详见 [内容完整性与标题页](references/multimedia-spec.md#内容完整性与标题页)。

每个 slide 或内容块可带 `caption`、`source`、`citations`；图片/图表支持 `alt`。引文编号以上标紧邻论断或图注，页底“本页参考文献”列出可核对的条目，不能只在演讲者备注或文末显示。`notes` 仍用于补充讲解背景。

- `references:[{id,title,authors,year,source,doi,url}]` 以首次定义顺序编号；相同 DOI/URL 合并、其它 id 作为别名，未知引用和冲突 id 报错。引用标号与自动追加的末尾文献页一致。
- 页底文献自动收集当前 slide、要点项及双栏子块的 `citations`，去重后按全局编号排列；续页按该页内容重新计算。无引用的页不占文献区，文末文献页不重复添加页底列表。不要手写编号、把所有文献复制到每页，或用普通文本 `[1]` 代替 `citations`。
- `chart_type` 仅为 `bar`/`line`，`categories` 与每个 `series[].values` 等长；单位、系列名称和来源必填。`null` 表示缺测，图中留空/断线、数值表标“缺测”，不当作零。不得把不同单位拼成一个系列。
- 图表由 Office 原生形状、线条及文字构成，便于编辑；不是带 Excel 工作簿的原生 Chart 对象。修改数据时改 spec 后重新生成，避免只移动图形造成比例错误。
- 支持 PNG、常见 Huffman 编码 JPEG/JPG、GIF、BMP。WebP、AVIF、HEIC/HEIF、SVG 先转换为 PNG/JPEG，使用本地绝对 `path`，不依赖在线图片链接。缺失、损坏、不支持或过大的图片会明确报错，不能静默丢图交付。

## 工作流与视觉验收

1. 读取相关原始报告、解析结果、决策和证据资料，检查当前患者附件中是否有帮助解释的原图。确认受众和核心结论，分清原始事实、推断和建议。
2. 在 `/repo/threads/<当前会话目录>/slides_skeleton.md` 记录每页回答的问题、所选版式（可为纯文字）、要展示的实际内容及其来源、计划引用的文献 id；明确标出有意仅展示标题的封面/章节页。骨架覆盖“事实 → 证据 → 结论 → 建议”。
3. 写 spec：逐页将骨架中的实际内容写入对应页型字段，不能只复制标题。需要图片或图表时，让它与相关解释处于同一阅读区域；科学结论在具体内容块上附对应引用，只把适用于整页的引用放在 slide 上。逐项核对数值、单位、时间点、图注及文献。有引用时为页底文献留出空间。材料不足时在正文写清已有事实和具体缺失信息，不根据标题编造病情、诊断或治疗内容。
4. 运行唯一脚本。长要点列表、跨页表格及参考文献会自动拆页；单个过长要点/单元格、超量双栏、过小图表、不可读长图会明确报错。按原因精简或拆页后重跑，不缩到不可读字号，也不裁掉溢出的内容。
5. 严格结构校验通过后，环境有 LibreOffice/目标预览引擎时必须打开并导出 PDF/逐页图片检查。对照骨架逐页核对标题和实际主体，覆盖自动续页，不能只抽查；正文既要存在于文件中，也要在渲染后可见，不能出现只剩标题、来源或页码的内容页。按最终演示尺寸确认截图关键字段可读、局部图与原图位置对应，多图顺序及图号稳定。检查正文、图片边缘、图注、来源、页底文献与页码是否相交，表格是否丢行/丢列，文献是否完整，折线缺测是否断开，图例和数值是否对应。逐页核对上标与底部条目一一对应，编号没有孤行、裁切或与正文相撞；根据真实渲染修复后再生成，并重新完成全部检查后才能报告完成。

默认采用白底、深蓝 `#19364D` 与墨色层级，以青绿 `#007F7A` 克制强调。图表辅助色使用蓝、琥珀与紫，并以不同点形/线型区分系列；图例与带系列名的数值区一致，便于灰度核对。坐标轴采用 1/2/2.5/5 × 10ⁿ 易读步长，保留零点与原始数值。

表格使用深蓝表头、浅斑马底和轻水平线，纯数字列自动右对齐；可用 `column_widths` 给解释列留更多宽度，或用 `align` 明确对齐。图片与图注成组顶对齐，图注紧贴图片。正文默认 20 pt，表格/图注/数据标签不低于 14 pt；页底文献 13 pt，文末完整文献正文 16 pt、DOI/URL 14 pt，采用悬挂编号；坐标刻度 12 pt、页码信息 10 pt。行距与排版测量保持一致，不靠缩小字号容纳内容。

按科学问题选择比例和版式，见 [学术版式选择](references/multimedia-spec.md#学术版式选择)。用留白和一致对齐组织图文，避免密集网格、立体图、渐变、无意义阴影和装饰。

标题尽量一行；要点页每页最多 6 条。表格最多 6 列、每页至多 10 行（实际按文字行高拆页），不可读的窄列应拆表。双栏不自动拆页，超量时移动整块到独立页。每个待嵌入图像保持比例且完整显示；患者截图优先全宽或独立细节页，必要时以原图上下文页配位置明确的局部放大页。图注/来源紧贴图片，并为页底文献保留空间，不能靠缩图使关键字段失去可读性。

结构校验不等于真实客户端渲染。未验证 PowerPoint/目标插件时如实说明；环境无渲染器时明确“已通过结构校验，未完成视觉验收”，不能宣称所有客户端均无排版问题。目标插件仍需支持 PPTX，下载响应 MIME 应为 `application/vnd.openxmlformats-officedocument.presentationml.presentation`。

## 参数与完成报告

参数：`--spec` 必填；`--name` 文件名、`--out-dir` 输出目录、`--date YYYY-MM-DD` 日期、`--version` 正整数版本、`--out` 显式文件路径、`--title` 正文标题、`--author` 作者、`--force` 覆盖明确目标。常规生成仅传 `--spec` 与 `--out-dir`。

完成时简要列出：spec 路径、最终 PPTX 路径/版本、总页数、图文混排与图表页数、嵌入图片数、文献数及文献页数、成品内容检查结果（检查页数及明确标题页数）、已完成的严格校验/视觉验收。列明实际缺失材料或失败原因；有空正文异常页时不能报告完成，不声称尚未完成的 SDK/客户端验证已通过。
