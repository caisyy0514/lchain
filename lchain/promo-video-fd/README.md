# LChain 家庭医生 Agent · 产品介绍视频

线性简笔画风格 + 中文旁白和字幕，约 139 秒，1920×1080 / 30fps。
讲 `app/wecom/fd`（VBC 分级诊疗）：安全门控 → 多轮问诊 → 分级 → 就医计划卡 →
居家观察闭环 → 真人医生审核 → 可追溯。

成片：`output/fd-intro.mp4`

## 生成

```bash
cd promo-video-fd
./build.sh              # 旁白 → 渲染 → 配乐 → 合成 mp4（约 4~6 分钟）
./build.sh --preview    # 只生成每个分镜的预览图 output/preview/sheet_*.png
./build.sh --no-voice   # 不要旁白，只有背景音乐
VOICE=zh-CN-YunxiNeural ./build.sh   # 换成男声
```

依赖：Python 3.12+（`build.sh` 自动创建 `.venv`，安装 Pillow / NumPy / SciPy / edge-tts）、ffmpeg。
本目录的 `.venv` 指向 `../promo-video/.venv`（两个视频共用一套依赖）。
旁白用微软 Edge 神经网络语音（需要联网；本机走 `HTTPS_PROXY`，在 `voice.py` 里读取
`EDGE_TTS_PROXY` / `HTTPS_PROXY` / `HTTP_PROXY`），连不上时自动回退到 macOS `say`。

调单个画面：`python probe.py 6 12.5 out.png`（第 6 个分镜第 12.5 秒），或 `python probe.py 6 80% out.png`。

## 内容

分镜脚本见 [STORYBOARD.md](STORYBOARD.md)，共 10 个分镜（139 秒）：

1. 标题：企微群里的家庭医生，先把安全说清楚，再把下一步说清楚
2. 痛点：自由回答的四个坑（漏红旗 / 过早结论 / 只说去医院 / 让患者自己找医生）
3. 安全门控：28 条确定性红旗规则，毫秒级，命中立即行动
4. 多轮问诊：至少 2 轮、最多 4 轮，每轮 1–2 个关键问题
5. 分级判定：E0–E5 + EU，服务端只升不降
6. 计划卡：病情 / 方向 / 需排除 / 为什么 / 去哪 / 查什么 / 怎么确诊
7. 居家观察：截止时间 + 升级指征 + 定时追问，未回复 ≠ 好转
8. 医生审核：非急症结论提交平台真人医生审核
9. 可追溯：自动化测试 + 样例群 + Langfuse 链路
10. 结尾：安全先于建议 · 交流先于结论 · 结论由医生审核

## 修改

- **改文案**：改 `scenes.py` 末尾 `SCENES` 里的文字（旁白和字幕共用），重新 `./build.sh`
- **改时长**：改 `SCENES` 里每个分镜的秒数，音乐会自动按总时长生成
- **改画面**：改 `scenes.py` 里对应的场景函数，用 `python probe.py <场景号> <秒数>` 快速看效果
- **换声音**：`VOICE=zh-CN-YunxiNeural ./build.sh`（可选 `zh-CN-XiaoxiaoNeural` 默认女声、
  `zh-CN-YunyangNeural` 专业男声），`RATE=-8% ./build.sh` 调语速
- **换配色**：`sketch.py` 顶部的 `PAPER` / `INK` / `TEAL` / `BLUE` 等常量

> 微软 Edge 语音的商用授权请自行确认；对外商用建议用付费 TTS 或真人配音。
