#!/usr/bin/env python3
"""「世界の株価」ページを撮影して1枚のJPEGにする。

撮影先がブロック（CloudflareのWAF等）された場合は、代替サイトへ自動で切り替える。
どこからも取れなかったときは終了コード3で終わり、ワークフロー側は画像なしで通知を送る。

環境変数:
  CAPTURE_URL     撮影対象URL            (既定: https://sekai-kabuka.com/pc-index.html)
  CLIP            x,y,w,h でページ座標を直接指定
  FALLBACK_URL    代替サイト              (既定: https://nikkei225jp.com/)
  FALLBACK_CLIP   代替サイト用の x,y,w,h  (未指定ならビューポートをそのまま撮る)
  VIEWPORT        幅x高さ                (既定: 1000x1320)
  SCROLL_Y        CLIP未指定時のスクロール量 (既定: 0)
  WAIT_MS         描画待ち時間(ms)        (既定: 9000)
  EXPECT_TEXT     成功判定に使う語        (既定: 日経平均)
  OUTPUT          出力ファイル名          (既定: world.jpg)
  MAX_BYTES       上限バイト数            (既定: 900000)

使い方:
  python capture_world.py            # 本番撮影
  python capture_world.py --grid     # 100px方眼つきの全体像を debug_full.jpg に保存
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

from playwright.sync_api import sync_playwright

JST = timezone(timedelta(hours=9))
BANNER_H = 44  # 取得日時バーの高さ(px)

URL = os.getenv("CAPTURE_URL", "https://sekai-kabuka.com/pc-index.html")
CLIP = os.getenv("CLIP", "")
FALLBACK_URL = os.getenv("FALLBACK_URL", "https://nikkei225jp.com/")
FALLBACK_CLIP = os.getenv("FALLBACK_CLIP", "")
VIEWPORT = os.getenv("VIEWPORT", "1000x1320")
SCROLL_Y = int(os.getenv("SCROLL_Y", "0"))
WAIT_MS = int(os.getenv("WAIT_MS", "9000"))
EXPECT_TEXT = os.getenv("EXPECT_TEXT", "日経平均")
OUTPUT = os.getenv("OUTPUT", "world.jpg")
MAX_BYTES = int(os.getenv("MAX_BYTES", "900000"))

# これらが本文に出ていたらブロックされたと判断する
BLOCK_MARKERS = (
    "you have been blocked",
    "Attention Required",
    "Cloudflare Ray ID",
    "Access denied",
    "Just a moment",
    "Enable JavaScript and cookies",
)

# 自動化ブラウザだと見抜かれにくくするための最低限の細工
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['ja-JP', 'ja', 'en-US']});
window.chrome = window.chrome || {runtime: {}};
"""

BANNER_JS = """
(args) => {
  const d = document.createElement('div');
  d.textContent = args.label;
  d.style.cssText =
    (args.fixed
      ? 'position:fixed;left:0;top:0;width:100%;'
      : `position:absolute;left:${args.x}px;top:${args.y}px;width:${args.w}px;`) +
    `height:${args.h}px;background:#17303F;color:#fff;` +
    'font:bold 21px/1 -apple-system,"Hiragino Sans","Noto Sans JP",sans-serif;' +
    'display:flex;align-items:center;padding-left:14px;box-sizing:border-box;' +
    'z-index:2147483647';
  document.body.appendChild(d);
}
"""

# 遅延読み込みのチャートを全て描画させるため、一度最下部まで送ってから戻る
SCROLL_ALL_JS = """
async () => {
  await new Promise((res) => {
    let y = 0;
    const step = () => {
      y += 500;
      window.scrollTo(0, y);
      if (y < document.body.scrollHeight) setTimeout(step, 250);
      else { window.scrollTo(0, 0); res(); }
    };
    step();
  });
}
"""

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


def load_page(context, url: str):
    """ページを開いて全チャートを描画させ、(page, 本文テキスト) を返す。"""
    page = context.new_page()
    page.goto(url, wait_until="load", timeout=60_000)
    page.wait_for_timeout(WAIT_MS)
    page.evaluate(SCROLL_ALL_JS)
    page.wait_for_timeout(2500)
    try:
        body = page.inner_text("body")[:5000]
    except Exception:  # noqa: BLE001 — 本文が読めない場合も判定は続ける
        body = ""
    return page, body


def judge(body: str) -> str | None:
    """問題があればその理由を返す。正常なら None。"""
    for marker in BLOCK_MARKERS:
        if marker.lower() in body.lower():
            return f"ブロックページを検出（{marker}）"
    if EXPECT_TEXT and EXPECT_TEXT not in body:
        return f"期待する文字列『{EXPECT_TEXT}』が見つからない"
    return None


def shoot(page, clip: str, output: str) -> None:
    """日時バーを載せて撮影し、上限バイト数に収まるまで圧縮する。"""
    label = datetime.now(JST).strftime("データ取得 %Y/%m/%d %H:%M JST")
    shot_args: dict = {"type": "jpeg"}

    if clip:
        x, y, cw, ch = (int(v) for v in clip.split(","))
        by = max(0, y - BANNER_H)  # 切り抜きの直上に日時バーを置く
        page.evaluate(BANNER_JS, {"label": label, "fixed": False,
                                  "x": x, "y": by, "w": cw, "h": y - by or BANNER_H})
        shot_args["clip"] = {"x": x, "y": by, "width": cw, "height": ch + (y - by)}
        shot_args["full_page"] = True  # ビューポートより下も含めるために必須
    else:
        if SCROLL_Y:
            page.evaluate(f"window.scrollTo(0, {SCROLL_Y})")
            page.wait_for_timeout(800)
        page.evaluate(BANNER_JS, {"label": label, "fixed": True, "h": BANNER_H})

    quality = 85
    while True:
        page.screenshot(path=output, quality=quality, **shot_args)
        size = os.path.getsize(output)
        print(f"撮影: {output} quality={quality} size={size:,} bytes")
        if size <= MAX_BYTES or quality <= 45:
            break
        quality -= 15


def main() -> None:
    grid = "--grid" in sys.argv
    w, h = (int(v) for v in VIEWPORT.lower().split("x"))

    candidates = [(URL, CLIP)]
    if FALLBACK_URL and FALLBACK_URL != URL:
        candidates.append((FALLBACK_URL, FALLBACK_CLIP))

    with sync_playwright() as p:
        browser = p.chromium.launch(args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ])
        context = browser.new_context(
            viewport={"width": w, "height": h},
            device_scale_factor=2,  # 文字がつぶれないように2倍解像度
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            extra_http_headers={"Accept-Language": "ja-JP,ja;q=0.9,en;q=0.8"},
        )
        context.add_init_script(STEALTH_JS)

        for url, clip in candidates:
            print(f"取得中: {url}")
            try:
                page, body = load_page(context, url)
            except Exception as e:  # noqa: BLE001 — 次の候補を試す
                print(f"  読み込み失敗: {type(e).__name__}: {e}", file=sys.stderr)
                continue

            if grid:
                page.evaluate(GRID_JS)
                page.screenshot(path="debug_full.jpg", type="jpeg", quality=70, full_page=True)
                print("保存: debug_full.jpg（方眼つき全体像。ここからCLIPを決める）")
                browser.close()
                return

            problem = judge(body)
            if problem:
                print(f"  {problem} → 次の候補へ", file=sys.stderr)
                page.close()
                continue

            shoot(page, clip, OUTPUT)
            browser.close()
            return

        browser.close()

    print("すべての撮影先が取得できませんでした（画像なしで通知します）", file=sys.stderr)
    sys.exit(3)


if __name__ == "__main__":
    main()
