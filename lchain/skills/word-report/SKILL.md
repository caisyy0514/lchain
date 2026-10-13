---
name: word-report
description: 'Generate editable Chinese Word (.docx) reports using text-only or visual layouts as the content requires, with support for patient screenshots, embedded images, editable charts, tables, and numbered scientific references. Use when users request a Word report, an editable document, or a downloadable docx. For presentations or web reports use ppt-report or html-report.'
requires: [report_run_generator, report_list_styles]
---

# 生成 Word 报告

> **在 LChain 项目中使用（路径映射）**
> - 文中的 `/repo` = 报告工作区（`data/report_workspace`），`/repo/o_outcome` = 最终报告目录；`/repo/skill/common/word-report/scripts/...` = 本项目 `skills/word-report/scripts/...`。
> - 用 `report_write_file` 把 spec / 骨架写到 `/repo/threads/<会话名>/`，再用 `report_run_generator(skill="word-report", spec_path=...)` 生成；**不要自己拼命令行**，也不要手写最终文件。
> - 图片 `path` 必须是工作区内已存在的文件（`/repo/...`）；本项目的对话里一般没有患者附件，没有真实素材时按 Skill 规则用纯文字/表格，不编造图片或数据。
> - 生成后用 `report_list_outputs` 查看，下载地址为 `/api/reports/<文件名>`，把它告诉用户。

使用本 skill 的 `scripts/generate_word.py` 从 JSON spec 生成 `.docx`。文字、表格、图表数据可编辑；使用图片和图表时，素材与工作簿嵌入文件，离线分享不丢图。Office Open XML Transitional / Office 2016 为兼容基线，仍需在目标 Word/WPS/Pages/LibreOffice 中检查分页。生成器仅需 Python 标准库；已有 Pillow 时还会做完整图片解码校验。

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

## 执行与历史文件保护

```bash
python /repo/skill/common/word-report/scripts/generate_word.py \
  --spec "<实际写出的 spec 文件路径>" \
  --out-dir "/repo/o_outcome"
```

- 最终报告固定输出到 `/repo/o_outcome/{title}_{yyyy}_{mm}_{dd}_v{N}.docx`；中间 spec、骨架与 QA 文件留在 `/repo/threads/<当前会话>/`。仓库开发测试可用临时输出目录隔离样例。
- 正常生成省略 `--name`、`--date`、`--version`、`--out`、`--force`，标题取 spec `title`，版本取同名同日现有最大版本 + 1。保护历史文件，不修改、改名或删除旧版本。
- 只有用户明确要求覆盖某份历史报告时，才将显式版本/路径与 `--force` 一起使用。
- `.docx` 是二进制，必须运行脚本生成，不能用 Write 重存，也不能以 HTML/Markdown 冒充 Word。报错后修复 spec 或素材并重试；不能绕过校验另写生成器。
- 以成功打印的 `Output:` 为准；缺失、格式不支持或损坏图片会报错，不再悄悄生成缺图占位。

## 内容与证据工作流

1. 完整读取本次相关附件原文/图片解析与 `/repo/s_state/`、`d_decision/`、`b_benefit/`、`e_evidence/` 中使用到的事实和证据。区分患者原始检查事实、外部研究证据与作者解释；记录实际附件名、页码、报告日期、单位与原图路径，未知信息不编造。
2. 写 `report_skeleton.md`：每节核心问题、关键结论、来源及引用 id。只有图片、图表或表格确实有助于解释时，才记录相应形式与素材。缺少素材时无需补做流程图；仅当材料缺口影响解释时说明。
3. 核验科学来源：优先指南、系统综述和原始研究；打开原始 DOI/期刊/指南发布页核对题名、作者、年份、研究人群与结论。不得编造作者、DOI、文献、样本量、疗效数字；检索摘要不足以支持具体推断时保留证据边界。访问不到的来源不能写成已核验；可只保留已有可靠资料并向用户说明证据缺口。
4. 组装 spec。外部文献放 `references`，正文/图/表用 `citations: ["id"]` 关联。患者附件放 `source`，不能把个人检查记录包装成公开研究。数据图只画实际可追溯数值，注明单位、时间、样本/分母与必要统计口径；没有可靠数字时用文字说明，必要时选用图像或定性表，不臆造趋势。
5. 运行脚本，检查 stdout 数量、ZIP 内图片和图表工作簿，再进行逐页视觉检查。任何改变分页、图片尺寸、列宽或正文长度的修改后都要重新渲染。

## spec 与版式选择

编写 spec 前读取 [references/spec.md](references/spec.md)，其中包含完整字段、图文/图表混排示例、分页规则及限制。

顶层 `title`、`subtitle`、`author`、`meta`、`blocks` 保持兼容，新增 `references`。

纯文字或列表可以独立成篇。单图、单表、图表页或混排均按讲解需要选择，不设最低图数或混排比例，也不要求每页或每个章节配图。

- 普通正文：`heading`、`paragraph`（也接受 `text`）、`bullets`、`numbered`、`quote`、`code`、`page_break`。
- 患者截图、检查图片：有助解释时，从本次附件取得可用原图；必要时提取 PDF 相关页为图片，通过 `image.path` 指向实际存在的本地文件并嵌入，不能仅用 OCR 文字或附件链接代替必要原图。图片取用与局部放大示例见 [references/spec.md](references/spec.md#患者截图取用与示例)。
- 外部医学插图：远程图片先通过可用工具保存到本地，记录来源/许可再嵌入。自制示意图必须标明“示意”，不能伪装成患者影像或实证数据。
- 图文并排、图表与解释并排、两图比较：`columns` 包含 `left`、`right` block 数组和 `ratio`。复杂图、宽表或长截图优先全宽。禁止嵌套 columns。
- 数值比较/时间趋势：`chart` 支持 `bar` / `line`，写入原生图表及内嵌 XLSX，支持多个系列与 `null` 缺测；不可用 0 代替缺测，不使用 3D、双轴或截断柱形图坐标轴。
- 明细比较：`table` 支持 `column_widths` 权重与每列 `align`，保留真正表头、正文自动换行、重复表头。解释长列加宽，短数值列缩窄。很长的表可以跨页，每个普通数据行保持完整；超长单行需拆行或移到正文。
- 图片按比例 contain，宽高双约束且预留图注/来源空间。详细截图优先全宽或独立页；缩放后关键数值、单位和标题必须可读，必要时用原图上下文配局部放大图。方向修正、格式转换和局部截取均另存派生副本，保留原图；局部图注明在原图中的位置，不改写或遮盖临床信息。
- `caption` 写稳定的图/表编号和解释，多图按正文讲解顺序排列；`alt` 写图像意义，患者图片的 `source` 写实际附件名、页码、日期及派生处理，`citations` 独立关联科学文献。图注和来源与图片绑定，长解释放可分页的正文；表题与来源置于表格前，连接首行。保持相同图表配色含义。

## 学术样式

- 默认白底、墨色正文、深蓝标题与青绿强调，用字号和留白建立层级。正文 11 pt，图注 10 pt，来源 9 pt；不要额外加彩色卡片、粗边框或阴影来制造层次。
- 表格默认三线表、无竖向网格；解释列自动留宽，数值列自动右对齐。需要特定列宽、中文单位或特殊数据对齐时显式写 `column_widths`/`align`。
- 原生图表使用统一细网格、底部图例、色盲友好辅助色；折线另以点形和虚实线区分，灰度分享也能辨认。相同系列跨图保持顺序与含义一致。
- 自制插图沿用相同色系与克制线条，图中文字按最终嵌入尺寸检查；原始医学图片保持真实。详细尺寸与样式见 [references/spec.md](references/spec.md#学术样式与素材约束)。

## 文献与引用

学术性来自可靠证据、清晰结构与准确表达。引用要求由论断决定，与是否使用图片或图表独立。

- `references` 按首次定义顺序编号，同 DOI/URL 合并为一个编号，别名 id 指向同一条。正文与图注自动显示 `[1]`，文末自动生成“参考文献”，DOI/URL 可点击。
- 同一 id 不允许冲突内容；未知引用 id、无来源图表或非数字数据会直接报错。脚本只能验证结构，不能证明医学结论真实，来源核验仍必须执行。
- 引文应紧邻它支持的论断或图表，不要只在文末列出一堆与正文无对应关系的文献。按证据本身支持的范围写结论，保留限制，不把关联写成因果。

## 回读与视觉检查

- 先确认 `Images`、`Charts`、`References` 与计划一致，所有嵌入关系存在。图片复用会去重，因此嵌入文件数可能少于图片 block 数；drawing id 仍各自唯一。
- 有渲染器时转 PDF/逐页图片，逐页检查中文字体、表格列宽与重复表头、每张图的四边、圆形比例、图注不孤立、图例/坐标轴标签、空缺数据断开、文献编号和链接。患者截图需在最终页面尺寸下读清标题、关键数值及单位，核对局部图位置、编号和来源；仅看原文件清晰不算通过。正文末尾引文若单独占一行，精简该句或调整断句后重渲染，不能靠过度缩字解决。仅 XML 校验不能代表展示正常。
- 中文缺字先检查渲染环境是否有等线/微软雅黑/宋体或可用 CJK 回退字体；不要把缺字的 PDF 当作排版通过。
- 若环境缺少渲染能力，完成结构校验后明确报告“尚未完成视觉验证”，不要宣称已验证 Word/WPS 中没有溢出。

## 参数与交付

`--spec` 必填；`--out-dir` 默认 `/repo/o_outcome`。`--title` / `--author` 可覆盖正文；`--name` 覆盖文件名基名；`--date` 接受 `YYYY-MM-DD`；`--version` 显式版本；`--out` 显式路径；`--force` 覆盖已有文件。常规任务遵守前述默认调用。

完成时给出实际输出路径与版本、文字/图片/图表/表格/文献数量、视觉校验结果，以及真实存在的未验证项或失败原因；简述报告关键内容。不要向用户交付中间 QA 文件替代 `.docx`。
