# LChain 问诊智能体（consult） · 产品介绍视频

线性简笔画风格（白底黑线手绘描边 + 少量强调色）+ **真实界面截图嵌图**，约 179 秒，1920×1080 / 30fps，
带原创背景音乐和中文旁白、字幕。面向**要用同一套框架做别的业务的同事**：既讲产品，也讲框架怎么复用。

成片：`output/consult-intro.mp4`

## 生成

```bash
cd promo-video-consult
./build.sh              # 生成旁白 → 渲染画面 → 合成音乐 → 合成 mp4（约 3~5 分钟）
./build.sh --preview    # 只生成每个分镜的预览图 output/preview/sheet_*.png，调画面时用
./build.sh --no-voice   # 不要旁白，只有背景音乐
VOICE=zh-CN-YunxiNeural ./build.sh   # 换成男声
```

依赖：Python 3.12（`build.sh` 会自动创建 `.venv` 并安装 Pillow、NumPy、SciPy、edge-tts）、ffmpeg。
本机的 `.venv` 是指向 `../promo-video/.venv` 的软链接，两个视频共用一套依赖。
旁白用微软 Edge 神经网络语音（需要联网），连不上时自动回退到 macOS `say`（音质明显较差，会有提示）。

调单个画面：`python probe.py 5 13.2 out.png`（第 5 个分镜第 13.2 秒），或 `python probe.py 5 88% out.png`。

## 内容

分镜脚本见 [STORYBOARD.md](STORYBOARD.md)，共 12 个分镜（165 秒）：

1. 标题：LChain 问诊智能体（患者发一条消息，就回一条）
2. 痛点：只让模型自由发挥会走歪（漏问病史 / 提前下结论 / 回复一大段 / 忘了免责声明）
3. 固定路径：现病史采集 → 分诊 → 鉴别问询 → 诊断意见 → 变化随访 → 更新诊断
4. 十项现病史要素：缺哪项问哪项、每轮最多 2 问、问不出来就记「未明确」
5. 两个判断并发：extract ∥ assess → decide 汇合钳制（单轮 3.99s → 2.62s）
6. 流程闸门：模型只提建议，真正决定流程的是 `clamp_action` 的三条硬规则
7. 安全边界：分诊/诊断文本由 `render.py` 拼装（`render_triage` / `render_diagnosis`），免责声明与紧急提示必带
8. 状态落盘 + 并发：`consult.json` 含对话记录、重启不丢；同会话串行、不同会话并发
9. 可追溯：一次问诊 = 一条 Langfuse Trace（调用链、token、耗时、工作流图示）
10. 用例库 + 回归：`eval/consult/<科室>/*.md` 一个病例一个文件；改完就跑整套，通过率不降才上线
11. 复用清单：画流程图 → 写判断提示词 → 加代码闸门 → 建用例库 + 跑批
12. 结尾：流程由图决定 · 内容由提示词决定 · 安全由代码决定

## 文件

| 文件 | 作用 |
| --- | --- |
| `sketch.py` | 简笔画引擎（与企微群视频同一套，只多了问诊主色 TEAL / TEAL_FILL）：3 倍超采样；样条平滑曲线；笔触两头细中间粗；水彩式填色；柔和投影；纸张纹理；文字逐字浮现 |
| `props.py` | 通用素材：人物（新增 `coat=True` 画白大褂 + 听诊器）、机器人、手机、气泡、服务器、时钟、数据库、卡片、图标 |
| `cprops.py` | 问诊专用素材：真实界面嵌图 `ui_shot()`、病历要素卡 `record_card()`、闸门 `gate()`、代码卡 `code_card()`、目录树 `folder_tree()`、耗时对比条 `bar_compare()`、剪刀、盖章、文件卡、标签页 |
| `scenes.py` | 13 个分镜的动画 + 时间线 `SCENES`（函数、时长、口语化旁白文案） |
| `render.py` | 多进程逐帧渲染；分镜之间交叉淡化；字幕按旁白的实际时间逐句显示 |
| `music.py` | 原创配乐：F 大调轻快温暖（Rhodes 电钢琴 + Pad + 贝斯 + 钢片琴 + 轻鼓组），无版权问题 |
| `voice.py` | 微软神经网络语音生成旁白（默认「晓晓」），同时获取每句的时间生成同步字幕 |
| `build.sh` | 一键合成；旁白说话时音乐自动压低（sidechain ducking），整体响度标准化到 -16 LUFS |
| `audio_check.py` | 音频客观检查：削波、频段能量占比、分段响度 |
| `contact_sheet.py` | 把预览帧拼成总览图 |
| `crop_assets.py` | 把浏览器整屏截图按记录的区域裁成 `assets/*.png`（配合 `shots.json`） |
| `probe.py` | 渲染单个分镜的指定时刻，调画面用 |
| `assets/` | 视频里嵌的**真实界面截图**（裁剪 + 圆角）：进度条、步骤卡（并发）、分诊卡、诊断卡、测试报告、Langfuse Trace（诊断轮）、工作流图示；裁切规则在 `shots.json`，`crop_assets.py` 执行 |

## 界面截图怎么来的

视频里的 UI 嵌图不是画的，是**真实页面截图**：用浏览器打开 `/?scenario=consult` 跑一次真实问诊，
再打开 `/eval`、Langfuse（`localhost:3000`）截图，然后按元素位置裁切：

```bash
# shots.json 里记录每个嵌图的来源文件与裁剪区域（x/y/w/h）
python crop_assets.py     # 输出 assets/<name>.png
```

换截图时只改 `shots.json` 里的 `src` 和 `rect`，或者重新截一遍整屏再跑一次脚本。
`shots.json` 里还支持 `patch` 字段（`[{x, y, w, h, color}]`），用来局部遮盖界面上的旧文案——
比如产品改名后，把页头里的旧名字抹掉，其余像素保持原样。
`ui_shot()` 会把手绘外框画在截图外面，所以截图本身的圆角和留白已经在这里处理好。

### 重抓 Trace 截图（后续演示会话干净）

Langfuse 的 Trace 截图要跟着演示病例走，否则会出现「视频里讲上腹痛、截图里是膝关节」这种不一致。
把病例重新跑一遍，拿诊断那一轮的 trace 链接（`replay.py` 按用例里的 `history` 逐轮回答，
每次跑出来的对话都一样）：

```bash
PYTHONPATH=. .venv/bin/python replay.py promo-abd-01   # 会打印每一轮的 action 和 trace 链接
```

拿到 `.../traces/<trace_id>` 后直接开这个 URL 截图（用 `promo-*` 开头的 session id，
不要在截图里出现 `eval-<n>-<时间戳>` 这种跑批会话名）。

> 正式对外发布时，如果涉及真实患者信息，记得换成脱敏的演示会话再截图。

## 修改

- **改文案**：改 `scenes.py` 末尾 `SCENES` 里的文字（旁白和字幕共用），重新 `./build.sh`。`voice.py` 会提示旁白是否超出分镜时长
- **改时长**：改 `SCENES` 里每个分镜的秒数，音乐会自动按总时长生成
- **改画面**：改 `scenes.py` 里对应的场景函数，用 `python probe.py <场景号> <秒数>` 快速看效果
- **换旁白声音**：`VOICE=zh-CN-YunxiNeural ./build.sh`。可选：`zh-CN-XiaoxiaoNeural`（女声，温暖，默认）、`zh-CN-YunxiNeural`（男声，阳光）、`zh-CN-YunyangNeural`（男声，专业沉稳）、`zh-CN-XiaoyiNeural`（女声，活泼）。语速用 `RATE=-8% ./build.sh` 调整
- **换配色**：`sketch.py` 顶部的 `PAPER` / `INK` / `TEAL` / `BLUE` 等常量
- **换音乐**：替换 `output/music.wav` 后只执行 `build.sh` 的第 4 步，或改 `music.py` 里的 `PROG`（和弦进行）和 `BPM`

> 微软 Edge 语音的商用授权请自行确认；对外商用建议用阿里云、腾讯云、Azure 等付费 TTS 或真人配音。
