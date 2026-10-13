"""旁白：微软 Edge 神经网络语音（edge-tts，接近真人的自然语调），失败时回退到 macOS `say`。

python voice.py [--voice zh-CN-XiaoxiaoNeural] [--rate -4%] [--pitch +0Hz]

- 每个分镜一段旁白，文案在 scenes.py 的 SCENES 里（口语化写法，读起来像真人讲解）
- 同时拿到每一句的起止时间（SentenceBoundary），生成与语音同步的字幕：output/voice/subs.json
- 旁白按分镜时间线对齐混成 output/voice.wav；做了轻微的人声处理：去底噪、温暖 EQ、轻压缩
- 超出分镜时长会提示

推荐音色：
  zh-CN-XiaoxiaoNeural  女声，温暖亲切（默认）
  zh-CN-YunxiNeural     男声，阳光自然
  zh-CN-YunyangNeural   男声，专业沉稳（新闻播报感）
  zh-CN-XiaoyiNeural    女声，活泼
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
from pathlib import Path

import importlib

from paths import OUT, SCENES_MODULE
from render import timeline

SCENES = importlib.import_module(SCENES_MODULE).SCENES

VD = OUT / "voice"
LEAD = 0.6  # 分镜开始后多久开口


def duration(p: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                       capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


async def edge_one(text: str, path: Path, voice: str, rate: str, pitch: str) -> list[dict]:
    import edge_tts

    last: Exception | None = None
    for attempt in range(6):
        try:
            c = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch, boundary="SentenceBoundary",
                                     connect_timeout=15, receive_timeout=60)
            sents, buf = [], bytearray()
            async for ch in c.stream():
                if ch["type"] == "audio":
                    buf += ch["data"]
                elif ch["type"] == "SentenceBoundary":
                    sents.append({"start": ch["offset"] / 1e7, "end": (ch["offset"] + ch["duration"]) / 1e7,
                                  "text": ch["text"]})
            if not buf:
                raise RuntimeError("没有收到音频")
            path.write_bytes(bytes(buf))
            return sents
        except Exception as e:  # noqa: BLE001  网络偶发超时，重试
            last = e
            await asyncio.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"edge-tts 失败：{last}")


def say_one(text: str, path: Path) -> list[dict]:
    aiff = path.with_suffix(".aiff")
    subprocess.run(["say", "-v", "Tingting", "-r", "200", "-o", str(aiff), text], check=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(aiff), str(path)], check=True)
    # 没有句子时间：按字数比例估算
    parts = [p for p in re.split(r"(?<=[。！？；，])", text) if p.strip()]
    total, dur, acc, out = sum(len(p) for p in parts), duration(path), 0.0, []
    for p in parts:
        d = dur * len(p) / total
        out.append({"start": acc, "end": acc + d, "text": p})
        acc += d
    return out


def split_for_subtitle(sents: list[dict], max_chars: int = 22) -> list[dict]:
    """字幕按逗号再切短：每行不超过 max_chars 个字，时间按字数比例分配。"""
    out = []
    for s in sents:
        text = s["text"].strip()
        if len(text) <= max_chars:
            out.append(s)
            continue
        parts = [p for p in re.split(r"(?<=[，、；：])", text) if p.strip()]
        lines, cur = [], ""
        for p in parts:
            if cur and len(cur) + len(p) > max_chars:
                lines.append(cur)
                cur = p
            else:
                cur += p
        if cur:
            lines.append(cur)
        total = sum(len(x) for x in lines)
        acc = s["start"]
        for x in lines:
            d = (s["end"] - s["start"]) * len(x) / total
            out.append({"start": acc, "end": acc + d, "text": x})
            acc += d
    for s in out:  # 字幕里去掉句末标点，更干净
        s["text"] = s["text"].strip().rstrip("，。；、")
    return out


async def main_async(a) -> None:
    VD.mkdir(parents=True, exist_ok=True)
    subs, inputs, filters = [], [], []
    engine = "edge"
    for i, ((_, d, text), (start, _)) in enumerate(zip(SCENES, timeline())):
        f = VD / f"scene{i + 1}.mp3"
        try:
            if engine != "edge":
                raise RuntimeError("edge 不可用")
            sents = await edge_one(text, f, a.voice, a.rate, a.pitch)
        except Exception as e:  # noqa: BLE001
            if engine == "edge":
                print(f"⚠ edge-tts 不可用（{e}），回退到 macOS say（音质较差）")
            engine = "say"
            f = f.with_suffix(".wav")
            sents = say_one(text, f)
        dur = duration(f)
        ok = LEAD + dur <= d - 0.4
        print(f"分镜 {i + 1}：旁白 {dur:4.1f}s / 分镜 {d:4.1f}s  {'✓' if ok else '⚠ 超长，请缩短文案、加快语速或加长分镜'}")
        for s in split_for_subtitle(sents):
            subs.append({"start": round(start + LEAD + s["start"], 3), "end": round(start + LEAD + s["end"] + 0.15, 3),
                         "text": s["text"]})
        inputs += ["-i", str(f)]
        ms = int((start + LEAD) * 1000)
        filters.append(f"[{i}:a]aresample=48000,adelay={ms}|{ms}[v{i}]")
    (VD / "subs.json").write_text(json.dumps(subs, ensure_ascii=False, indent=1))
    mix = "".join(f"[v{i}]" for i in range(len(SCENES)))
    total = sum(d for _, d, _ in SCENES)
    # 人声处理：高通去低频杂音 → 低中频加一点温暖 → 齿音轻压 → 轻压缩 → 响度标准化
    chain = ("highpass=f=80,equalizer=f=180:t=q:w=1:g=2,equalizer=f=3500:t=q:w=1.2:g=1.5,"
             "deesser=i=0.3,acompressor=threshold=-20dB:ratio=2.5:attack=8:release=120:makeup=2")
    filters.append(f"{mix}amix=inputs={len(SCENES)}:normalize=0,{chain},apad[out]")
    subprocess.run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", ";".join(filters), "-map", "[out]",
                    "-t", str(total), "-ac", "2", "-ar", "48000", str(OUT / "voice.wav")], check=True)
    print(f"voice.wav 已生成（{engine}，{a.voice if engine == 'edge' else 'Tingting'}），字幕 {len(subs)} 条")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--voice", default="zh-CN-XiaoxiaoNeural")
    ap.add_argument("--rate", default="-4%")
    ap.add_argument("--pitch", default="+0Hz")
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
