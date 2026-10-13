---
name: html-report
description: 'Create self-contained, printable Chinese HTML reports using text-only or visual layouts as the content requires, with support for patient screenshots, local images, columns, bar/line charts, tables, and linked scientific references. Use for web reports or Markdown-to-HTML exports; use ppt-report for slides and word-report for Word documents.'
requires: [report_run_generator, report_list_styles]
---

# 生成 HTML 报告

> **在 LChain 项目中使用（路径映射）**
> - 文中的 `/repo` = 报告工作区（`data/report_workspace`），`/repo/o_outcome` = 最终报告目录；`/repo/skill/common/html-report/scripts/...` = 本项目 `skills/html-report/scripts/...`。
> - 用 `report_write_file` 把 spec / 骨架写到 `/repo/threads/<会话名>/`，再用 `report_run_generator(skill="html-report", spec_path=...)` 生成；**不要自己拼命令行**，也不要手写最终文件。
> - 图片 `path` 必须是工作区内已存在的文件（`/repo/...`）；本项目的对话里一般没有患者附件，没有真实素材时按 Skill 规则用纯文字/表格，不编造图片或数据。
> - 生成后用 `report_list_outputs` 查看，下载地址为 `/api/reports/<文件名>`，把它告诉用户。

使用本 skill 的脚本，把 JSON spec 或已有 Markdown 渲染成可离线打开的单文件 HTML。使用图片和图表时，图片内联、图表采用内联 SVG，不依赖 CDN、JavaScript 或远程图片。根据内容组织文字、证据与必要的视觉材料。

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

## 执行与历史报告保护

最终 HTML 必须由脚本生成，不手写最终文件：

~~~bash
python /repo/skill/common/html-report/scripts/generate_html.py \
  --spec "<实际写好的 JSON 路径>" \
  --out-dir "/repo/o_outcome"
~~~

- 中间 spec、素材清单与报告骨架保存在 /repo 下本会话目录；最终报告写入 /repo/o_outcome/。
- 文件名自动生成：{title}_{yyyy}_{mm}_{dd}_v{N}.html。正常生成省略 --name、--date、--version、--out、--force，以当天最大版本号 + 1 新建报告。
- 不改动既有报告。只有用户要求替换指定版本时才使用显式 --version/--out 与 --force。
- 以脚本打印的 Output 路径为准。缺图、坏引用、无效图表数据会报错并停止，不交付占位图片或半成品，不用手写 HTML 绕过失败。

## 内容、图片与证据

先读取本次任务相关的原始检查资料、解析结果及 /repo 中对应的事实、证据、决策内容。患者提交的截图、检查单照片或其他图片有助于解释时，应从本次附件中取得原图并插入相应章节，不能仅以 OCR 文字、文件名或链接替代必要的图片展示。建立骨架时列出每节要解释的结论、来源与引用 id；确定需要图片、图表或表格后，再记录形式、素材路径、数据单位与来源。

纯文字或列表可以独立成篇。单图、单表、图表页或混排均按讲解需要选择，不设最低图数或混排比例，也不要求每页或每个章节配图。确需视觉解释时，可参考：

- 检查结果与局部发现：原始检查图片 + 紧邻说明，保留关键区域与原始比例；信息很密的整页图片应提供清晰局部图或补充可读表格。
- 机制、步骤或关系：可使用有来源的示意图，或根据已核实内容制作并标明“示意图”的本地 SVG/PNG；不得把示意图当作病理、影像或实验结果。
- 同一单位的数量比较：柱状图；有顺序的时间变化：折线图。分类标签与采样时间保持真实，缺测填 null；不可用不等距时间点暗示等距时间，也不可把跨单位检验值放入同一数值轴。
- 数值、参考范围、适用人群和证据限制：表格；需要逐项精确核对时保留表格。

插入患者资料前确认附件对应本次报告、图片方向与内容，可通过可用工具将 PDF 中的相关页渲染为图片。格式转换、旋转、标注或局部放大另存副本，保留原图与对应关系。`source` 写实际附件名、可核实的页码/检查日期和处理方式；缺失信息不补造，患者附件不冒充公开研究文献。具体字段和示例见 [患者截图与图片](references/spec-guide.md#患者截图与图片)。

没有可靠数值时不凑图表；缺少素材时无需补做流程图，仅当材料缺口影响解释时说明。学术性来自可靠证据、清晰结构与准确表达，引用要求由论断决定，与是否使用图片或图表独立。

为外部医学结论核对原始论文、指南或权威来源的标题、作者、年份、出处与 DOI/URL，明确适用人群、证据局限与更新时间。仅使用已读取或已核实的文献；无法核实的标记待核实，不能包装成已确认参考文献。患者原始资料/本地合成示例用 source 标清，与科学文献区分。图片需说明来源与使用许可；不能为了装饰添加无关图片。

## 输入与组装

新建报告优先使用 JSON，支持：

- 原有 heading、paragraph、bullets、numbered、table、image、quote、code、page_break。
- chart：bar/line、多系列、单位、缺测、图注、来源和可见底层数据表。
- columns：left/right 各是一组 blocks，ratio 指定列宽，可组合文字、图片、表格、图表。
- 顶层 references 定义文献；各 block 的 citations 引用 id，自动生成编号、可点击标注及文末去重文献列表。正文也可用 [@id]。

组装 spec 前读取 [references/spec-guide.md](references/spec-guide.md)，其中给出字段、格式约束、Markdown 支持范围与排版规则。可从 [references/mixed-report.example.json](references/mixed-report.example.json) 复制混排结构；其中全部数值和文献条目均为排版演示材料，不能用于医学结论。

默认主题采用学术报告版式：白底、墨色正文、深蓝层级与青绿强调，三线表、数字列对齐、紧邻的图注/来源及清晰文献列表。编排图片、密集图表或长表格时参考 [references/style-guide.md](references/style-guide.md)，优先保证阅读秩序与证据可读性。

已有 Markdown 可直接渲染，相对图片路径按 Markdown 所在目录解析：

~~~bash
python /repo/skill/common/html-report/scripts/generate_html.py \
  --markdown "<实际 Markdown 路径>" \
  --references "<本地参考文献 JSON 路径>" \
  --out-dir "/repo/o_outcome"
~~~

--references 可省略；有 [@id] 时必须提供相应定义。需要图表与复杂混排时，把 Markdown 内容整理为 JSON spec。

## 生成后的检查

脚本成功是结构校验；不能代替视觉检查。打开实际生成文件，检查桌面宽度、手机窄屏及打印预览：

- 所有图片均清晰、方向与比例正确、关键区域完整，图注和来源紧邻；没有空白图片、拉伸、溢出或遮盖。患者截图按最终显示尺寸回读关键标题、数值、单位及参考范围；缩小后不可读时改用全宽或有位置说明的局部图，不能只检查图片加载成功。
- 文字与图片双列在窄屏变单列；宽图表和表格在局部滚动，不让整页横向溢出；滚动后完整类别仍可读。
- 图表轴、单位、系列标签、数据表与原始输入一致；柱图零基线、折线缺测断开、数值无伪造精度。
- 打印时图和图注在一起，长表跨页且重复表头，章节标题不孤行，不出现意外空白页或被裁切的末行。
- 文内/图注编号都能跳转到正确文献，重复来源仅列一次，DOI/URL 可识别。文件断网后图片和图表仍在。

若环境没有浏览器/打印预览工具，执行可用的结构检查，并在交付中如实说明未完成视觉验证；不要宣称排版已经全部通过。出现问题调整素材、标签、拆表或布局，再生成新版本。

## 参数与交付

--spec 与 --markdown 二选一。可用 --title/--author 覆盖封面，--toc/--no-toc 控制目录，其他参数详见脚本 --help。脚本只使用 Python 标准库。

交付时提供实际 HTML 路径、版本、输入来源、图片/图表/表格/文献数量，以及视觉检查结果或未验证项。本 skill 的最终交付格式为 .html。
