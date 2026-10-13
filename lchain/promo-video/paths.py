"""输出目录与分镜模块的解析：让同一套引擎能生成多支视频。

    ./build.sh                                  # 默认：output/            → lchain-intro.mp4
    ./build.sh guide                            # 见 build.sh：output/guide/ → agent-dev-process.mp4

用环境变量控制，是为了不改 render/voice/music 里那套已经调好的逻辑：
- `VIDEO`         视频名（空 = 老片的 output/，非空 = output/<VIDEO>/）
- `SCENES_MODULE` 分镜模块名（默认 scenes）
- `VIDEO_FILE`    输出文件名
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).parent
VIDEO = os.environ.get("VIDEO", "").strip()
SCENES_MODULE = os.environ.get("SCENES_MODULE", "scenes").strip() or "scenes"
VIDEO_FILE = os.environ.get("VIDEO_FILE", "lchain-intro.mp4").strip() or "lchain-intro.mp4"

# 默认（VIDEO 为空）保持原有的 output/ 布局，老命令 ./build.sh 行为不变
OUT = ROOT / "output" / VIDEO if VIDEO else ROOT / "output"
