"""原创背景音乐 v2（无版权问题）：python music.py <秒数>  →  output/music.wav

风格：温暖、轻快的产品介绍配乐（Lo-fi / 轻电子感），F 大调，92 BPM
编曲层次（随段落逐渐加入，结尾收回）：
- Rhodes 电钢琴：FM 合成，柔和的铃音泛音 + 轻微颤音，和弦 Fmaj7 – Am7 – Dm7 – B♭maj7 循环
- Pad 铺底：多个轻微失谐的锯齿波 + 低通，缓慢起伏
- 贝斯：正弦 + 少量泛音，跟和弦根音
- 旋律点缀：钢片琴（celesta）音色的五声音阶短句
- 鼓：柔和底鼓 + 轻拍手（军鼓位置）+ 摇摆感的闭镲
- 母带：卷积混响（合成的指数衰减噪声脉冲）、轻度饱和、立体声宽度、淡入淡出
"""
from __future__ import annotations

import sys
import wave
from pathlib import Path

import numpy as np
from scipy.signal import butter, fftconvolve, sosfilt

SR = 44100
BPM = 92
BEAT = 60 / BPM
BAR = BEAT * 4
RNG = np.random.default_rng(11)


def midi(n: int) -> float:
    return 440.0 * 2 ** ((n - 69) / 12)


def env_adsr(n: int, a: float, d: float, s: float, r: float, dur: float) -> np.ndarray:
    t = np.arange(n) / SR
    e = np.where(t < a, t / max(a, 1e-4), s + (1 - s) * np.exp(-(t - a) / max(d, 1e-4)))
    rel_start = max(dur - r, a)
    e = np.where(t > rel_start, e * np.exp(-(t - rel_start) / max(r / 4, 1e-4)), e)
    return e


def lowpass(x: np.ndarray, fc: float, order: int = 2) -> np.ndarray:
    return sosfilt(butter(order, fc, "low", fs=SR, output="sos"), x)


def highpass(x: np.ndarray, fc: float, order: int = 2) -> np.ndarray:
    return sosfilt(butter(order, fc, "high", fs=SR, output="sos"), x)


# ---------------------------------------------------------------------- 音色
def rhodes(f: float, dur: float, vel: float = 0.5) -> np.ndarray:
    n = int(SR * (dur + 1.2))
    t = np.arange(n) / SR
    mod_env = np.exp(-t * 6)
    trem = 1 + 0.06 * np.sin(2 * np.pi * 4.8 * t)
    carrier = np.sin(2 * np.pi * f * t + 1.8 * mod_env * np.sin(2 * np.pi * f * t))
    bell = 0.18 * np.sin(2 * np.pi * f * 4.0 * t) * np.exp(-t * 9)
    e = env_adsr(n, 0.004, 1.1, 0.25, 0.5, dur + 0.6)
    return (carrier + bell) * e * trem * vel


def pad(freqs: list[float], dur: float, vel: float = 0.12) -> np.ndarray:
    n = int(SR * (dur + 1.5))
    t = np.arange(n) / SR
    out = np.zeros(n)
    for f in freqs:
        for det in (-0.12, 0.0, 0.11):
            ff = f * 2 ** (det / 12)
            ph = RNG.random() * 6.28
            saw = 2 * ((ff * t + ph / 6.28) % 1) - 1
            out += saw
    out = lowpass(out, 1400, 2)
    swell = 0.75 + 0.25 * np.sin(2 * np.pi * t / (BAR * 2))
    e = env_adsr(n, 0.8, 1.0, 0.85, 1.2, dur + 0.8)
    return out * e * swell * vel / (len(freqs) * 3)


def bass(f: float, dur: float, vel: float = 0.5) -> np.ndarray:
    n = int(SR * (dur + 0.3))
    t = np.arange(n) / SR
    w = np.sin(2 * np.pi * f * t) + 0.18 * np.sin(2 * np.pi * 2 * f * t) + 0.06 * np.sin(2 * np.pi * 3 * f * t)
    return w * env_adsr(n, 0.01, 0.4, 0.7, 0.12, dur) * vel


def celesta(f: float, dur: float, vel: float = 0.25) -> np.ndarray:
    n = int(SR * (dur + 1.5))
    t = np.arange(n) / SR
    w = np.sin(2 * np.pi * f * t) + 0.35 * np.sin(2 * np.pi * 3 * f * t) * np.exp(-t * 5) \
        + 0.12 * np.sin(2 * np.pi * 5.4 * f * t) * np.exp(-t * 9)
    return w * np.exp(-t * 2.6) * (1 - np.exp(-t * 600)) * vel


def kick(vel=0.8) -> np.ndarray:
    n = int(SR * 0.45)
    t = np.arange(n) / SR
    f = 52 + 90 * np.exp(-t * 25)
    body = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 7)
    click = RNG.standard_normal(n) * np.exp(-t * 300) * 0.15
    return lowpass(body + click, 3000) * vel


def clap(vel=0.35) -> np.ndarray:
    n = int(SR * 0.35)
    t = np.arange(n) / SR
    noise = RNG.standard_normal(n)
    bursts = sum(np.exp(-np.clip(t - k * 0.009, 0, None) * 180) * (t >= k * 0.009) for k in range(3))
    tail = np.exp(-t * 18)
    x = noise * (bursts * 0.6 + tail * 0.5)
    x = highpass(lowpass(x, 5000), 900)
    return x * vel


def hat(vel=0.12, open_=False) -> np.ndarray:
    n = int(SR * (0.25 if open_ else 0.07))
    t = np.arange(n) / SR
    x = highpass(RNG.standard_normal(n), 7000, 4)
    return x * np.exp(-t * (14 if open_ else 70)) * vel


# ---------------------------------------------------------------------- 编曲
# F 大调：Fmaj7 – Am7 – Dm7 – B♭maj7
CHORDS = [
    ([53, 57, 60, 64], 41),   # Fmaj7: F A C E / 贝斯 F
    ([52, 57, 60, 64], 45),   # Am7:   E A C E / A
    ([53, 57, 60, 62], 38),   # Dm7:   F A C D / D
    ([53, 58, 62, 65], 46),   # B♭maj7: F B♭ D F / B♭
]
PENTA = [65, 67, 69, 72, 74, 77, 79, 81]  # F 大调五声音阶
MOTIF = [(0, 2, 0.5), (1.0, 3, 0.5), (1.5, 4, 1.0), (2.5, 3, 0.5), (3.0, 2, 1.0)]  # (拍位, 音阶序号, 时值)


def add(buf: np.ndarray, sig: np.ndarray, at: float, gain: float = 1.0) -> None:
    i = int(at * SR)
    if i >= len(buf) or i < 0:
        return
    j = min(len(buf), i + len(sig))
    buf[i:j] += sig[: j - i] * gain


def reverb_ir(seconds=2.2, decay=3.2) -> tuple[np.ndarray, np.ndarray]:
    n = int(SR * seconds)
    t = np.arange(n) / SR
    irs = []
    for _ in range(2):
        x = RNG.standard_normal(n) * np.exp(-t * decay)
        x = lowpass(x, 6000)
        x[: int(SR * 0.02)] *= np.linspace(0, 1, int(SR * 0.02))  # 预延迟感
        irs.append(x / np.sqrt(np.sum(x ** 2)))
    return irs[0], irs[1]


def compose(total: float) -> np.ndarray:
    n = int(SR * (total + 3))
    L, R = np.zeros(n), np.zeros(n)
    wetL, wetR = np.zeros(n), np.zeros(n)  # 送混响
    drums = np.zeros(n)
    n_bars = int(np.ceil(total / BAR))
    for b in range(n_bars):
        t0 = b * BAR
        notes, root = CHORDS[b % 4]
        intro = b < 2
        outro = t0 >= total - BAR * 2
        full = not intro and not outro and b >= 4
        # Pad：始终铺底
        p = pad([midi(x) for x in notes], BAR, vel=0.45)
        add(L, p, t0, 0.9)
        add(R, p, t0, 0.9)
        add(wetL, p, t0, 0.5)
        add(wetR, p, t0, 0.5)
        # Rhodes：切分节奏（1、2&、4 拍）
        rhythm = [(0, 1.4, 0.55), (1.5, 0.9, 0.4), (3.0, 0.9, 0.45)] if not intro else [(0, 3.8, 0.5)]
        for beat, dur, v in rhythm:
            for k, x in enumerate(notes):
                s = rhodes(midi(x + 12), dur * BEAT, vel=v * 0.22)
                pan = 0.35 + 0.1 * k
                add(L, s, t0 + beat * BEAT + k * 0.008, 1 - pan)
                add(R, s, t0 + beat * BEAT + k * 0.008, pan)
                add(wetL, s, t0 + beat * BEAT, 0.35)
                add(wetR, s, t0 + beat * BEAT, 0.35)
        # 贝斯（音量压低，避免低频轰鸣）
        if not intro:
            for beat, dur in ((0, 1.5), (1.5, 0.5), (2.5, 1.2)):
                s = bass(midi(root), dur * BEAT, vel=0.13)
                add(L, s, t0 + beat * BEAT)
                add(R, s, t0 + beat * BEAT)
        # 旋律点缀：每 2 小节一句
        if b >= 2 and not outro and b % 2 == 0:
            shift = (b // 2) % 3
            for beat, idx, dur in MOTIF:
                f = midi(PENTA[min(len(PENTA) - 1, idx + shift)])
                s = celesta(f, dur * BEAT, vel=0.3)
                add(L, s, t0 + beat * BEAT, 0.55)
                add(R, s, t0 + beat * BEAT, 0.8)
                add(wetL, s, t0 + beat * BEAT, 0.6)
                add(wetR, s, t0 + beat * BEAT, 0.6)
        # 鼓（底鼓轻一些，镲片亮一些）
        if full or (b >= 2 and not outro):
            for beat in (0, 2.5):
                add(drums, kick(0.2 if full else 0.13), t0 + beat * BEAT)
            if full:
                for beat in (1, 3):
                    add(drums, clap(0.4), t0 + beat * BEAT)
            for i in range(8):
                swing = 0.06 * BEAT if i % 2 else 0
                add(drums, hat(0.16 if i % 2 else 0.22, open_=(i == 7 and full)), t0 + i * BEAT / 2 + swing)
    # 混响
    irL, irR = reverb_ir()
    revL = fftconvolve(wetL, irL)[:n] * 0.9
    revR = fftconvolve(wetR, irR)[:n] * 0.9
    dr = drums * 0.9
    L = L + revL + dr + fftconvolve(drums, irL)[:n] * 0.06
    R = R + revR + dr + fftconvolve(drums, irR)[:n] * 0.06
    L, R = L[: int(SR * total)], R[: int(SR * total)]
    # 母带 EQ：切掉 35Hz 以下的超低频，削弱 120~300Hz 的浑浊感，提亮 3k 以上（空气感）
    def master_eq(x: np.ndarray) -> np.ndarray:
        x = highpass(x, 35, 2)
        mud = sosfilt(butter(2, [120, 300], "bandpass", fs=SR, output="sos"), x)
        presence = sosfilt(butter(2, [1500, 5000], "bandpass", fs=SR, output="sos"), x)
        air = highpass(x, 7000, 2)
        return x - 0.4 * mud + 1.2 * presence + 0.3 * air

    L, R = master_eq(L), master_eq(R)
    # 母带：轻度饱和 + 淡入淡出 + 归一化
    st = np.stack([L, R], axis=1)
    st /= np.max(np.abs(st)) + 1e-9
    st = np.tanh(st * 1.3) / np.tanh(1.3)
    m = len(st)
    fade = np.ones(m)
    fi, fo = int(1.5 * SR), int(3.5 * SR)
    fade[:fi] = np.linspace(0, 1, fi) ** 2
    fade[-fo:] = np.linspace(1, 0, fo) ** 1.6
    st *= fade[:, None]
    return st * 0.89


def main() -> None:
    total = float(sys.argv[1]) if len(sys.argv) > 1 else 80.0
    out = Path(__file__).parent / "output"
    out.mkdir(exist_ok=True)
    data = (compose(total) * 32767).astype(np.int16)
    with wave.open(str(out / "music.wav"), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(data.tobytes())
    print(f"music.wav：{total:.1f}s")


if __name__ == "__main__":
    main()
