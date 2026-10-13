# 产品介绍视频

同一个引擎出两支片子：**精致线性简笔画 + 原创背景音乐 + 真人感中文旁白 + 同步字幕**，1920×1080 / 30fps。

| 片子 | 成片 | 内容 | 时长 |
| --- | --- | --- | --- |
| 产品介绍 | `output/lchain-intro.mp4` | 企微群智能助手：痛点 → 流程 → 分支 → 可靠性 → 部署 | 83s |
| 开发流程 | `output/guide/agent-dev-process.mp4` | **和 AI 一起开发业务 Agent 的真实过程**：提需求 → 跑起来看 → 给反馈 → 它改进 → 沉淀（与 `docs/business-agent-guide.md` 配套） | 108s |

## 生成

```bash
cd promo-video
./build.sh              # 产品介绍片：旁白 → 渲染 → 音乐 → mp4（约 1 分钟）
./build.sh guide        # 开发流程片（分镜在 scenes_guide.py）
./build.sh --preview    # 只出每个分镜的预览图，调画面时先跑这个（几秒）
./build.sh --no-voice   # 不要旁白，只有背景音乐
VOICE=zh-CN-YunxiNeural ./build.sh guide   # 换成男声
```

多支视频靠环境变量切换（`paths.py`）：`VIDEO`（输出子目录）、`SCENES_MODULE`（分镜模块）、`VIDEO_FILE`（文件名）；
不加任何参数时行为与原来完全一致。

依赖：Python 3.12（`build.sh` 会自动创建 `.venv` 并安装 Pillow、NumPy、SciPy、edge-tts）、ffmpeg。
旁白用微软 Edge 神经网络语音（需要联网），连不上时自动回退到 macOS `say`（音质明显较差，会有提示）。

## 内容

分镜脚本见 [STORYBOARD.md](STORYBOARD.md)，共 8 个分镜：

1. 标题：LChain 企微群智能助手
2. 痛点：消息太多，病历经理回复不过来
3. 流程：判断 → 解读 → 检索 → 意图 → 回复
4. 分支：普通问题直接答 / 复杂问题推医生卡片 / 紧急情况提醒就医
5. 像真人：连发消息合并回复，病历经理接手后自动让开
6. 可靠：服务重启从断点继续，不丢不重
7. 部署：多个 web 节点 + 一个定时任务进程
8. 结尾：让每一条消息，都被好好回应

## 文件

| 文件 | 作用 |
| --- | --- |
| `sketch.py` | 简笔画引擎：3 倍超采样；样条平滑曲线；笔触两头细中间粗（像真实的笔）；水彩式填色（淡入 + 轻微错位）；柔和投影；纸张纹理背景；PingFang 字体，文字逐字浮现；胶囊标签；字幕条 |
| `props.py` | 素材：带表情和上衣的人物、圆润的助手机器人（会眨眼、挥手、浮动）、手机、对话气泡（弹出回弹）、服务器、时钟、数据库、医生卡片、流程节点卡片和图标、盾牌、进度条、锁、闪光 |
| `scenes.py` | 产品介绍片的 8 个分镜 + 时间线 `SCENES`（函数、时长、口语化旁白文案） |
| `scenes_guide.py` | 开发流程片的 9 个分镜（同上结构），画面内容取自真实开发对话 |
| `paths.py` | 输出目录与分镜模块解析（`VIDEO` / `SCENES_MODULE` / `VIDEO_FILE`），让同一套引擎出多支片子 |
| `render.py` | 多进程逐帧渲染；分镜之间交叉淡化；字幕按旁白的实际时间逐句显示 |
| `music.py` | 原创配乐：F 大调 92 BPM，Rhodes 电钢琴（FM 合成）+ Pad 铺底 + 贝斯 + 钢片琴旋律 + 轻鼓组，卷积混响、母带 EQ、淡入淡出 |
| `voice.py` | 微软神经网络语音生成旁白（默认「晓晓」，温暖女声），同时获取每句的时间生成同步字幕；人声 EQ + 轻压缩 |
| `build.sh` | 一键合成；旁白说话时音乐自动压低（sidechain ducking），整体响度标准化到 -16 LUFS |
| `audio_check.py` | 音频客观检查：削波、频段能量占比、分段响度 |
| `contact_sheet.py` | 把预览帧拼成总览图 |

## 修改

- **改文案**：修改对应分镜文件末尾 `SCENES` 里的文字（旁白和字幕共用），重新 `./build.sh`。`voice.py` 会提示旁白是否超出分镜时长
- **加一支新片**：复制 `scenes_guide.py` 改成你的内容，`VIDEO=xxx SCENES_MODULE=你的模块 VIDEO_FILE=xxx.mp4 ./build.sh`
- **改时长**：修改 `SCENES` 里每个分镜的秒数，音乐会自动按总时长生成
- **换旁白声音**：`VOICE=zh-CN-YunxiNeural ./build.sh`。可选：`zh-CN-XiaoxiaoNeural`（女声，温暖，默认）、`zh-CN-YunxiNeural`（男声，阳光）、`zh-CN-YunyangNeural`（男声，专业沉稳）、`zh-CN-XiaoyiNeural`（女声，活泼）。语速用 `RATE=-8% ./build.sh` 调整
- **换配色**：`sketch.py` 顶部的 `BG` / `INK` / `ACCENT` 等常量
- **换音乐**：替换 `output/music.wav` 后只执行 `build.sh` 的第 4 步，或修改 `music.py` 里的 `PROG`（和弦进行）和 `BPM`

> 正式对外发布时，也可以换成真人配音：录好后替换 `output/voice.wav`，并修改 `output/voice/subs.json` 里的字幕时间，再只执行 `build.sh` 的第 4 步。
> 微软 Edge 语音的商用授权请自行确认；对外商用建议使用阿里云、腾讯云、Azure 等付费 TTS 或真人配音。
