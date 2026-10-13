"""把预览帧拼成总览图：python contact_sheet.py  →  output/preview/sheet_55.png / sheet_88.png"""
import glob
from pathlib import Path

from PIL import Image

PV = Path(__file__).parent / "output" / "preview"

for tag in ("50", "88"):
    fs = sorted(glob.glob(str(PV / f"scene*_{tag}.png")))
    tw, th = 960, 540
    sheet = Image.new("RGB", (tw * 2, th * ((len(fs) + 1) // 2)), "white")
    for i, f in enumerate(fs):
        sheet.paste(Image.open(f).resize((tw, th)), ((i % 2) * tw, (i // 2) * th))
    sheet.save(PV / f"sheet_{tag}.png")
print("ok")
