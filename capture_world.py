#!/usr/bin/env python3
"""「世界の株価」ページを撮影して1枚のJPEGにする。

既定では https://sekai-kabuka.com/pc-index.html をビューポート幅1000pxで開き、
チャートの描画を待ってビューポートをそのまま撮る。
撮影範囲は環境変数で調整できる（初回だけ現物を見て合わせる想定）。

環境変数:
  CAPTURE_URL     撮影対象URL            (既定: https://nikkei225jp.com/)
  VIEWPORT        幅x高さ                (既定: 1000x1320)
  SCROLL_Y        撮影前に縦スクロールするpx (既定: 0)
  CLIP            x,y,w,h でページ座標を直接指定（指定時はSCROLL_Y/ビューポート撮影より優先）
  WAIT_MS         描画待ち時間(ms)        (既定: 9000)
  OUTPUT          出力ファイル名          (既定: world.jpg)
  MAX_BYTES       上限バイト数            (既定: 900000 ≒0.9MB)

使い方:
  python capture_world.py            # 本番撮影
  python capture_world.py --grid     # 100px方眼と座標ラベルを重ねた全体像を debug_full.jpg に保存
"""
from __future__ import annotations

import os
import sys

from playwright.sync_api import sync_playwright

URL = os.getenv("CAPTURE_URL", "https://sekai-kabuka.com/pc-index.html")
VIEWPORT = os.getenv("VIEWPORT", "1000x1320")
SCROLL_Y = int(os.getenv("SCROLL_Y", "0"))
CLIP = os.getenv("CLIP", "")
WAIT_MS = int(os.getenv("WAIT_MS", "9000"))
OUTPUT = os.getenv("OUTPUT", "world.jpg")
MAX_BYTES = int(os.getenv("MAX_BYTES", "900000"))

GRID_JS = """
() => {
  const o = document.createElement('div');
  o.style.cssText = 'position:absolute;left:0;top:0;width:100%;height:' +
    document.body.scrollHeight + 'px;z-index:2147483647;pointer-events:none';
  const H = document.body.scrollHeight, W = document.body.scrollWidth;
  let html = '';
  for (let y = 0; y < H; y += 100) {
    html += `<div style="position:absolute;left:0;top:${y}px;width:100%;height:1px;background:rgba(255,0,0,.45)"></div>`;
    html += `<div style="position:absolute;left:2px;top:${y}px;font:11px monospace;color:#f00;background:#fff">y=${y}</div>`;
  }
  for (let x = 0; x < W; x += 100) {
    html += `<div style="position:absolute;left:${x}px;top:0;width:1px;height:100%;background:rgba(0,0,255,.35)"></div>`;
    html += `<div style="position:absolute;left:${x}px;top:2px;font:11px monospace;color:#00f;background:#fff">x=${x}</div>`;
  }
  o.innerHTML = html;
  document.body.appendChild(o);
}
"""


def main() -> None:
    grid = "--grid" in sys.argv
    w, h = (int(v) for v in VIEWPORT.lower().split("x"))

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page(
            viewport={"width": w, "height": h},
            device_scale_factor=2,  # 文字がつぶれないように2倍解像度で撮る
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
        )
        page.goto(URL, wait_until="load", timeout=60_000)
        page.wait_for_timeout(WAIT_MS)  # チャートは遅れて描画されるので待つ

        if grid:
            page.evaluate(GRID_JS)
            page.screenshot(path="debug_full.jpg", type="jpeg", quality=70, full_page=True)
            print("保存: debug_full.jpg（方眼つき全体像。ここから CLIP / SCROLL_Y を決める）")
            browser.close()
            return

        shot_args: dict = {"type": "jpeg"}
        if CLIP:
            x, y, cw, ch = (int(v) for v in CLIP.split(","))
            shot_args["clip"] = {"x": x, "y": y, "width": cw, "height": ch}
        elif SCROLL_Y:
            page.evaluate(f"window.scrollTo(0, {SCROLL_Y})")
            page.wait_for_timeout(800)

        quality = 85
        while True:
            page.screenshot(path=OUTPUT, quality=quality, **shot_args)
            size = os.path.getsize(OUTPUT)
            print(f"撮影: {OUTPUT} quality={quality} size={size:,} bytes")
            if size <= MAX_BYTES or quality <= 45:
                break
            quality -= 15  # LINEのプレビュー上限に収まるまで圧縮を強める

        browser.close()

    if os.path.getsize(OUTPUT) > MAX_BYTES:
        print("警告: 上限バイト数を超えています。CLIPで範囲を狭めてください", file=sys.stderr)


if __name__ == "__main__":
    main()
