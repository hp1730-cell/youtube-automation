"""キャラクター素材を、動画で使う透過PNGに変換する

使い方: python prepare_character.py

1) 表情シート assets/character_sheet.png（2×2・白背景）
   左上=通常、右上=笑顔、左下=驚き、右下=考え中
   → assets/character/{normal,smile,surprised,thinking}.png

2) 口パクシート assets/mouth_sheet.png（3×2・白背景）
   上段: あ・い・う / 下段: え・お・口を閉じる
   → assets/character/talk/{a,i,u,e,o,n}.png
   「口を閉じる」の顔をベースにして、各母音の絵から口元だけを切り取って重ねます。
   こうすると口以外（髪・目・輪郭）は完全に同じ絵になり、切り替えてもガタつきません。

シートを差し替えたときは、このスクリプトを再実行するだけでOKです。
"""
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

EXPR_ORDER = ["normal", "smile", "surprised", "thinking"]
TALK_ORDER = ["a", "i", "u", "e", "o", "n"]
OUT_DIR = Path("assets/character")


def cut_out(cell: Image.Image) -> Image.Image:
    """外周につながった白い領域だけを背景として透過する（目や歯の白は残る）。"""
    rgb = np.asarray(cell.convert("RGB")).astype(np.int16)
    mn, mx = rgb.min(axis=2), rgb.max(axis=2)
    whiteish = (mn >= 228) & (mx - mn <= 30)

    # 白っぽい画素を 255 にしたマスクを作り、外周から塗りつぶし（=背景）を 128 にする
    mask = Image.fromarray(np.where(whiteish, 255, 0).astype(np.uint8), "L").copy()  # copy: 書き込み可能にする
    w, h = mask.size
    border = [(x, 0) for x in range(w)] + [(x, h - 1) for x in range(w)] + \
             [(0, y) for y in range(h)] + [(w - 1, y) for y in range(h)]
    for p in border:
        if mask.getpixel(p) == 255:
            ImageDraw.floodfill(mask, p, 128, thresh=0)
    bg = np.asarray(mask) == 128

    alpha = np.where(bg, 0, 255).astype(np.float32)
    # 境界の2pxは白さに応じて半透明にし、白いフチ（ハロー）を抑える
    grown = np.asarray(Image.fromarray(bg.astype(np.uint8) * 255).filter(ImageFilter.MaxFilter(5))) > 0
    ring = grown & ~bg
    alpha[ring] = np.clip((255 - mn[ring]) * 255 / 45, 0, 255)

    out = cell.convert("RGBA")
    out.putalpha(Image.fromarray(alpha.astype(np.uint8)))
    return out.crop(out.getbbox())


def split_grid(sheet: Image.Image, cols: int, rows: int) -> list[np.ndarray]:
    w, h = sheet.size
    cw, ch = w // cols, h // rows
    return [np.asarray(sheet.crop((c * cw, r * ch, (c + 1) * cw, (r + 1) * ch))).astype(np.float32)
            for r in range(rows) for c in range(cols)]


def make_expressions(path: Path) -> None:
    if not path.exists():
        print(f"{path} がないため、表情の切り出しをスキップします。")
        return
    out = OUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    for name, cell in zip(EXPR_ORDER, split_grid(Image.open(path).convert("RGB"), 2, 2)):
        img = cut_out(Image.fromarray(cell.astype(np.uint8)))
        img.save(out / f"{name}.png")
        print(f"表情 {name}.png  {img.size}")


def _shift(a: np.ndarray, dx: int, dy: int) -> np.ndarray:
    return np.roll(np.roll(a, -dy, axis=0), -dx, axis=1)


def _align(cell: np.ndarray, base: np.ndarray, search: int = 20) -> tuple[int, int]:
    """目〜鼻の帯（口を含まない）で、ベースの顔に重なる位置を探す。"""
    h, w = base.shape[:2]
    y0, y1, x0, x1 = int(h * 0.29), int(h * 0.52), int(w * 0.29), int(w * 0.74)
    lc, lb = cell.mean(axis=2), base.mean(axis=2)
    best = None
    for dy in range(-search, search + 1):
        for dx in range(-search, search + 1):
            e = np.abs(lc[y0 + dy:y1 + dy, x0 + dx:x1 + dx] - lb[y0:y1, x0:x1]).mean()
            if best is None or e < best[0]:
                best = (e, dx, dy)
    return best[1], best[2]


def make_talk(path: Path) -> None:
    if not path.exists():
        print(f"{path} がないため、口パク素材の作成をスキップします。")
        return
    cells = split_grid(Image.open(path).convert("RGB"), 3, 2)
    base = cells[5]                         # 「口を閉じる」の顔をベースにする
    h, w = base.shape[:2]
    aligned = [_shift(c, *_align(c, base)) for c in cells[:5]] + [base]

    # 口の位置：各母音とベースの差分の重心（顔の下半分・中央付近だけを見る）
    y0, y1, x0, x1 = int(h * 0.50), int(h * 0.65), int(w * 0.42), int(w * 0.63)
    centers = []
    for a in aligned[:5]:
        d = np.abs(a - base).sum(axis=2)[y0:y1, x0:x1] > 150
        ys, xs = np.nonzero(d)
        if len(xs):
            centers.append((xs.mean() + x0, ys.mean() + y0))
    cx, cy = np.median([c[0] for c in centers]), np.median([c[1] for c in centers])
    rx, ry = w * 0.10, h * 0.07
    print(f"口の中心: ({cx:.0f}, {cy:.0f})")

    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=255)
    mask = np.asarray(mask.filter(ImageFilter.GaussianBlur(8))).astype(np.float32)[..., None] / 255
    ring = (mask[..., 0] > 0.05) & (mask[..., 0] < 0.5)

    # ベースの左上に書かれたラベル文字（「口を閉じる」など）を消す
    clean = base.copy()
    lab_h, lab_w = int(h * 0.13), int(w * 0.38)
    median = np.median(np.stack([a[:lab_h, :lab_w] for a in aligned]), axis=0)
    clean[:lab_h, :lab_w] = median
    clean[:lab_h, :int(w * 0.12)] = 255  # 一番左は背景

    out = OUT_DIR / "talk"
    out.mkdir(parents=True, exist_ok=True)
    for name, a in zip(TALK_ORDER, aligned):
        if name == "n":
            comp = clean
        else:
            a = a + (clean[ring].mean(axis=0) - a[ring].mean(axis=0))  # 肌の色味を合わせる
            comp = clean * (1 - mask) + a * mask
        img = cut_out(Image.fromarray(np.clip(comp, 0, 255).astype(np.uint8)))
        img.save(out / f"{name}.png")
        print(f"口パク {name}.png  {img.size}")


if __name__ == "__main__":
    make_expressions(Path("assets/character_sheet.png"))
    make_talk(Path("assets/mouth_sheet.png"))
