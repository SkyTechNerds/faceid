import json
from pathlib import Path

import cv2
import numpy as np
from app.gallery import Gallery

g = Gallery(Path("data"))
eli = g._cache["eli"]
chr_ = g._cache["christian"]
d_eli = Path("data/persons/eli")
d_chr = Path("data/persons/christian")

emb, files = eli["emb"], eli["files"]
sims = emb @ emb.T
np.fill_diagonal(sims, -1)

# 1) aehnlichstes Eli-Paar (hoechster Wert unter Eli-Fotos)
i, j = np.unravel_index(int(np.argmax(sims)), sims.shape)
pair_high = (files[i], files[j], float(sims[i, j]))

# 2) mittleres Paar (~0.45)
flat = [(float(sims[a, b]), a, b) for a in range(len(files)) for b in range(a + 1, len(files))]
flat.sort()
mid = min(flat, key=lambda x: abs(x[0] - 0.45))
pair_mid = (files[mid[1]], files[mid[2]], mid[0])

# 3) Eli vs Christian (verschiedene Personen)
cross = eli["emb"] @ chr_["emb"].T
ci, cj = np.unravel_index(int(np.argmax(cross)), cross.shape)
pair_diff = (files[ci], chr_["files"][cj], float(cross[ci, cj]))

TH = 150
rows = [
    ("SAME PERSON - different angle  ->  KEEP BOTH", pair_mid, d_eli, d_eli),
    ("MOST SIMILAR pair still in the gallery  ->  KEPT", pair_high, d_eli, d_eli),
    ("DIFFERENT PEOPLE (Eli vs Christian)", pair_diff, d_eli, d_chr),
]
H = len(rows) * (TH + 46) + 10
W = TH * 2 + 260
canvas = np.full((H, W, 3), 18, dtype=np.uint8)

for r, (label, (fa, fb, score), da, db) in enumerate(rows):
    y = r * (TH + 46) + 34
    cv2.putText(canvas, label, (10, y - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (200, 200, 200), 1, cv2.LINE_AA)
    for k, (f, dd) in enumerate([(fa, da), (fb, db)]):
        img = cv2.imread(str(dd / f))
        if img is None:
            continue
        canvas[y:y + TH, 10 + k * (TH + 8):10 + k * (TH + 8) + TH] = cv2.resize(img, (TH, TH))
    pct = int(round(score * 100))
    col = (80, 220, 80) if score >= 0.60 else (120, 200, 255) if score >= 0.35 else (150, 150, 150)
    cv2.putText(canvas, f"{pct}%", (2 * TH + 34, y + TH // 2), cv2.FONT_HERSHEY_SIMPLEX, 1.1, col, 2, cv2.LINE_AA)
    note = "duplicate" if score >= 0.60 else "same person" if score >= 0.35 else "not the same"
    cv2.putText(canvas, note, (2 * TH + 34, y + TH // 2 + 26), cv2.FONT_HERSHEY_SIMPLEX, 0.45, col, 1, cv2.LINE_AA)

cv2.imwrite("/tmp/explain.jpg", canvas, [cv2.IMWRITE_JPEG_QUALITY, 90])
print("pairs:", [(round(p[1][2], 2)) for p in [(0, r[1]) for r in rows]])
print("saved /tmp/explain.jpg")
