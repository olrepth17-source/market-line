#!/usr/bin/env python3
"""Flexメッセージの見え方をブラウザで確認する（LINEの通数を消費せずに配色を詰めるため）。

  python preview_flex.py --demo          # ダミー値で preview.html を生成
  python notify_line.py --dry-run > m.json && python preview_flex.py m.json

LINEの完全な再現ではなく、配色・階層・文字サイズの確認用の近似レンダリング。
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime

import notify_line as nl

SIZE_PX = {"xxs": 11, "xs": 12, "sm": 14, "md": 16, "lg": 19, "xl": 22, "xxl": 28}
MARGIN_PX = {"none": 0, "xs": 2, "sm": 4, "md": 8, "lg": 12, "xl": 16, "xxl": 20}


def render(node: dict) -> str:
    t = node.get("type")
    m = MARGIN_PX.get(node.get("margin", "none"), 0)

    if t == "text":
        style = (
            f"font-size:{SIZE_PX.get(node.get('size', 'md'), 16)}px;"
            f"color:{node.get('color', '#111')};"
            f"font-weight:{'700' if node.get('weight') == 'bold' else '400'};"
            f"margin-top:{m}px;"
            f"{'margin-left:auto;text-align:right;' if node.get('align') == 'end' else ''}"
        )
        return f"<span style='{style}'>{node['text']}</span>"

    if t == "separator":
        return f"<hr style='border:none;border-top:1px solid #e5e5e5;margin:{max(m, 6)}px 0 0'>"

    if t == "box":
        layout = node["layout"]
        flex_dir = "column" if layout == "vertical" else "row"
        align = "align-items:baseline;" if layout == "baseline" else ""
        gap = MARGIN_PX.get(node.get("spacing", "none"), 0)
        pad = node.get("paddingAll", "0")
        inner = "".join(render(c) for c in node["contents"])
        return (
            f"<div style='display:flex;flex-direction:{flex_dir};{align}gap:{gap}px;"
            f"padding:{pad};margin-top:{m}px'>{inner}</div>"
        )

    if t == "button":
        primary = node.get("style") == "primary"
        bg = node.get("color", "#42606E") if primary else "#f0f0f0"
        fg = "#ffffff" if primary else "#333333"
        return (
            f"<a href='{node['action']['uri']}' target='_blank' style='display:block;text-align:center;"
            f"background:{bg};color:{fg};border-radius:6px;padding:9px;font-size:14px;"
            f"font-weight:700;text-decoration:none;margin-top:6px'>{node['action']['label']}</a>"
        )

    return ""


def bubble_html(bubble: dict, alt: str) -> str:
    body = render(bubble["body"])
    footer = "".join(render(c) for c in bubble["footer"]["contents"])
    return f"""<!doctype html><meta charset="utf-8">
<title>Flexプレビュー</title>
<style>
 body{{background:#8CA9BC;font-family:"Hiragino Sans","Noto Sans JP",sans-serif;padding:24px;margin:0}}
 .wrap{{max-width:340px;margin:0 auto}}
 .bubble{{background:#fff;border-radius:14px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.2)}}
 .footer{{padding:0 12px 12px}}
 .alt{{color:#fff;font-size:12px;margin:0 0 8px;opacity:.9}}
 .note{{color:#fff;font-size:11px;margin-top:14px;opacity:.85;line-height:1.6}}
</style>
<div class="wrap">
  <p class="alt">通知バー表示: {alt}</p>
  <div class="bubble">{body}<div class="footer">{footer}</div></div>
  <p class="note">※ LINEの実描画とは余白などが多少異なります。配色と情報量の確認用です。<br>
  上部には別メッセージとして「世界の株価」の画像が届きます。</p>
</div>"""


def main() -> None:
    if "--demo" in sys.argv:
        vi = {"date": datetime(2026, 8, 13), "value": 31.67, "prev": 31.81,
              "week_avg": 30.85, "month_avg": 28.44}
        fg = {"score": 66.03, "prev": 61.4, "week_avg": 63.0, "month_avg": 49.6}
        today = datetime.now(nl.JST)
        bubble, alt = nl.build_flex(vi, fg, today), nl.build_alt_text(vi, fg, today)
    else:
        raw = open(sys.argv[1], encoding="utf-8").read()
        msgs = json.loads(re.search(r"\[.*\]", raw, re.S).group(0))
        flex = next(m for m in msgs if m["type"] == "flex")
        bubble, alt = flex["contents"], flex["altText"]

    with open("preview.html", "w", encoding="utf-8") as f:
        f.write(bubble_html(bubble, alt))
    print("生成: preview.html")


if __name__ == "__main__":
    main()
