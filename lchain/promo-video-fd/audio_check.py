"""音频客观检查（听不到时用数据判断）：python audio_check.py output/music.wav

输出：削波、频段能量占比、分段响度。参考目标（温暖的配乐）：
低频 60-250Hz 约 30~50%，中频 250-2k 约 30~50%，中高 2k-6k 约 5~15%，高频 >6k 约 1~5%。
"""
import sys
import wave

import numpy as np


def main(path: str) -> None:
    w = wave.open(path)
    sr = w.getframerate()
    ch = w.getnchannels()
    x = np.frombuffer(w.readframes(w.getnframes()), np.int16).reshape(-1, ch).astype(float) / 32767
    m = x.mean(1)
    print(f"{path}：{len(m) / sr:.1f}s，削波采样 {int((np.abs(x) > 0.999).sum())}")
    seg = m[int(len(m) * 0.25): int(len(m) * 0.65)]
    spec = np.abs(np.fft.rfft(seg)) ** 2
    f = np.fft.rfftfreq(len(seg), 1 / sr)
    tot = spec[(f > 20) & (f < 16000)].sum()
    for a, b, n in [(20, 60, "超低"), (60, 250, "低频"), (250, 2000, "中频"), (2000, 6000, "中高"), (6000, 16000, "高频")]:
        print(f"  {n} {a:>5}-{b:<5}Hz {100 * spec[(f >= a) & (f < b)].sum() / tot:5.1f}%")
    step = 8
    row = []
    for s in range(0, int(len(m) / sr), step):
        r = np.sqrt((m[s * sr:(s + step) * sr] ** 2).mean()) + 1e-9
        row.append(f"{s}s:{20 * np.log10(r):.0f}")
    print("  分段响度(dB)", " ".join(row))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "output/music.wav")
