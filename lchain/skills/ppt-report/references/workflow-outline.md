# PPT 需求 → 大纲 → 图文混排：写法与示例

## 1. 需求整理（ppt_save_requirements）

| 字段 | 说明 | 缺失时 |
| --- | --- | --- |
| `topic` | 主题 | 必须问 |
| `audience` | 受众（患者/家属、临床医生、管理层、学生……） | 能从上下文推断就写进 `assumptions`，否则问 |
| `purpose` | 用途/场景（科普讲座、病例汇报、培训、方案评审……） | 同上 |
| `slide_count` | 页数或演讲时长（约 1 分钟 1 页） | 默认「10~12 页」写进 `assumptions` |
| `style` | 风格（学术严谨 / 通俗科普 / 商务简洁） | 按受众给默认值 |
| `materials` | 用户已有的资料、数据、图片 | 没有就留空 |
| `key_points` | 用户强调必须讲到的内容 | — |
| `visual_density` | `rich`（默认）/ `balanced` / `minimal` | 默认 rich |
| `open_questions` | **必须**先问用户的问题；非空时本轮只提问，不出大纲 | — |

提问示例（一次问完，别一轮问一个）：

> 为了把 PPT 做得更贴合，我先确认几点：
> 1. 听众是患者及家属，还是医护人员？
> 2. 大概讲多久 / 需要多少页？
> 3. 有没有必须包含的内容或数据（例如本院的统计数据）？
>
> 其余我先按「通俗科普风格、10 页左右、图文并茂」准备，你也可以直接改。

用户的需求已经很完整（如「给社区居民做 15 分钟高血压科普，10 页左右」）时，不必再问，`open_questions` 留空，直接出大纲。

## 2. 大纲（ppt_propose_outline）

```json
{
  "title": "高血压的居家管理",
  "subtitle": "社区健康讲座",
  "slides": [
    {"title": "高血压的居家管理", "layout": "title"},
    {"title": "高血压离我们有多近", "layout": "columns",
     "key_message": "高血压常见且多数人没有症状",
     "points": ["我国成人患病率较高", "很多人不知道自己血压高", "长期升高会伤害心脑肾"],
     "visual": {"kind": "image", "description": "社区居民测量血压的照片", "source": "联网图片"}},
    {"title": "正确测量血压的步骤", "layout": "columns",
     "key_message": "规范测量才能得到可靠数值",
     "points": ["测前静坐 5 分钟", "袖带与心脏同高", "早晚各测 2 次取平均"],
     "visual": {"kind": "diagram", "description": "flow：静坐→绑袖带→测量→记录", "source": "示意图生成"}},
    {"title": "日常管理四件事", "layout": "image",
     "key_message": "限盐、运动、控体重、按时服药",
     "points": ["每天盐 < 5 g", "每周运动 150 分钟", "规律服药不擅自停药"],
     "visual": {"kind": "diagram", "description": "cards：四个管理要点", "source": "示意图生成"}},
    {"title": "什么时候必须就医", "layout": "bullets",
     "key_message": "出现危险信号立即就医",
     "points": ["血压 ≥180/110 mmHg", "剧烈头痛、胸痛、肢体无力"],
     "visual": {"kind": "none"}}
  ]
}
```

- `layout` 与 spec 的 `type` 对应：`title`/`section`/`bullets`/`text`/`image`/`chart`/`table`/`columns`。
- `visual.kind`：`image`（联网图片或用户图片）、`diagram`（示意图）、`chart`（需要真实数据）、`table`、`none`。
- `layout` 和 `visual.kind` 要对应：`image` 页配 image/diagram，`chart` 页配 chart，`table` 页配 table，`columns` 必须有配图/图表/表格。
- 每页标题唯一，生成时 spec 的页标题必须和大纲一致（系统按标题对照检查）。
- 图文密度不够时工具会拒绝，按提示给更多页面配图。

用户修改示例：「第 3 页换成视频截图不好找，改成示意图；再加一页饮食建议」→ 修改后用 `ppt_propose_outline` 提交**完整的新版本**（不是只提交改动的部分），再次请用户确认。

## 3. 素材准备

| 需要表达 | 做法 |
| --- | --- |
| 步骤、流程、路径 | `ppt_make_diagram(kind="flow")` |
| 发展历程、随访时间点 | `ppt_make_diagram(kind="timeline")` |
| 循环管理、PDCA | `ppt_make_diagram(kind="cycle")` |
| 两三种方案对比 | `ppt_make_diagram(kind="compare")`，或 `table` |
| 总分结构、分类 | `ppt_make_diagram(kind="hierarchy")` |
| 3~6 个并列要点 | `ppt_make_diagram(kind="cards")` |
| 实物、场景、解剖结构 | `ppt_search_images` → 选尺寸大、来源正规的图 → `ppt_download_image` |
| 有真实数据的趋势/比较 | `chart`（数据必须来自用户资料或已核验文献） |

联网图片的选择：优先选百科、医院、政府/学会官网、正规媒体等来源；避免带大水印、带广告文字、分辨率低的图片。下载失败就换下一张，连续失败就改用示意图。

## 4. spec 中的图文混排写法

```json
{
  "type": "columns",
  "title": "正确测量血压的步骤",
  "ratio": [1, 1.2],
  "left": [{"type": "bullets", "bullets": ["测前静坐 5 分钟", "袖带与心脏同高", "早晚各测 2 次取平均"]}],
  "right": [{"type": "image", "path": "/repo/threads/ppt-xxx/images/diagram_measure.png",
             "alt": "流程示意图：静坐、绑袖带、测量、记录",
             "caption": "图 2：家庭血压测量流程（示意图）", "source": "根据本讲内容绘制的示意图"}]
}
```

- 单独一页的大图用 `{"type": "image", "title": "...", "path": "...", "caption": "...", "source": "..."}`。
- 左栏文字 2~4 条，太多会挤压图片；放不下时拆成两页。
- `columns` 的块类型只能是 `text`/`bullets`/`image`/`table`/`chart`。

## 5. 验证（ppt_verify）会检查什么

1. 已确认大纲中的每一页在成品中都存在；计划配图的页面确实有图片/图表/表格。
2. 图文密度达到需求要求。
3. LibreOffice 渲染：页数一致、中文字体可用（不会显示成方框）、文字没有超出页面、没有空白页。
4. 纯文字页面字数过多时给出拆页提示。
5. 配置了 `PPT_VERIFY_VISION_MODEL` 时，还会把缩略图交给多模态模型检查重叠、截断、图文不符等问题。

有问题就修 spec → 重新生成 → 再次验证。通过后把预览图和下载地址给用户。
