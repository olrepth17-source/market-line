#!/usr/bin/env python3
"""日経VI・Fear&Greed指数・世界の株価の画像を、LINEに毎朝1回まとめて送る。

送るもの（1回の送信で2通ぶん）:
  1. 画像メッセージ … capture_world.py が撮った「世界の株価」の画像
  2. Flexメッセージ … 日経VIとF&Gの値（数値を色分け）＋リンクボタン2つ

環境変数:
  LINE_CHANNEL_ACCESS_TOKEN  チャネルアクセストークン（長期）
  LINE_USER_ID               送信先ユーザーID
  IMAGE_URL                  画像の公開URL（未設定なら画像は送らずFlexのみ）
  BRIEF_URL                  「ざっくり朝ビュー」のURL

使い方:
  python notify_line.py --dry-run   # 送信せず、送信JSONと本文プレビューを表示
  python notify_line.py             # 送信
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

JST = timezone(timedelta(hours=9))
TIMEOUT = 30

NIKKEI_VI_CSV = "https://indexes.nikkei.co.jp/nkave/historical/nikkei_stock_average_vi_daily_jp.csv"
CNN_FG_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"

BRIEF_URL = os.getenv("BRIEF_URL", "https://olrepth17-source.github.io/morning-brief/")
WORLD_URL = "https://nikkei225jp.com/"

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# ---- 色（危険なほど赤に寄せる） --------------------------------------------
RED, ORANGE, YELLOW, GREEN, DEEP_GREEN, GRAY = (
    "#C62828", "#EF6C00", "#F9A825", "#7CB342", "#1B7F3B", "#9E9E9E",
)
INK, SUB, FAINT = "#222222", "#777777", "#AAAAAA"

# 日経VIの閾値（上から順に判定）: (下限, 数値の色, バッジ文言, バッジの色)
# 数値の色分けは指定どおり50以上=赤 / 40以上=オレンジ。30超はバッジだけで注意喚起する
VI_BANDS: list[tuple[float, str, str, str]] = [
    (50, RED, "⚠ 50超", RED),
    (40, ORANGE, "⚠ 40超", ORANGE),
    (30, INK, "30超", YELLOW),
    (0, INK, "", INK),
]

# F&Gの区分。(下限, 日本語, 英語, 色)
FG_BANDS: list[tuple[int, str, str, str]] = [
    (76, "極端な強欲", "Extreme Greed", DEEP_GREEN),
    (55, "強欲", "Greed", GREEN),
    (46, "中立", "Neutral", GRAY),
    (26, "恐怖", "Fear", ORANGE),
    (0, "極端な恐怖", "Extreme Fear", RED),
]

WEEKDAY_EN = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ---------------------------------------------------------------- データ取得

def fetch_nikkei_vi() -> list[tuple[datetime, float]]:
    """日経平均VIの日次CSVを [(日付, 終値), ...] 古い順で返す。"""
    r = requests.get(NIKKEI_VI_CSV, timeout=TIMEOUT, headers={"User-Agent": BROWSER_UA})
    r.raise_for_status()
    rows: list[tuple[datetime, float]] = []
    for cols in csv.reader(io.StringIO(r.content.decode("cp932", errors="replace"))):
        if len(cols) < 3:
            continue
        try:
            d = datetime.strptime(cols[0].strip(), "%Y/%m/%d")
            close = float(cols[2].strip())
        except ValueError:
            continue  # ヘッダー行・末尾の注記行
        rows.append((d, close))
    if not rows:
        raise ValueError("日経VIのCSVから有効な行を取得できませんでした")
    rows.sort(key=lambda r: r[0])
    return rows


def fetch_fear_greed() -> dict:
    """CNNのFear&Greed指数を取得する（非公式エンドポイント。UAが無いと弾かれる）。"""
    r = requests.get(
        CNN_FG_URL,
        timeout=TIMEOUT,
        headers={
            "User-Agent": BROWSER_UA,
            "Accept": "application/json",
            "Referer": "https://edition.cnn.com/markets/fear-and-greed",
        },
    )
    r.raise_for_status()
    return r.json()


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def fg_stats(payload: dict) -> dict:
    cur = payload["fear_and_greed"]
    hist = [
        (datetime.fromtimestamp(p["x"] / 1000, tz=timezone.utc), float(p["y"]))
        for p in payload.get("fear_and_greed_historical", {}).get("data", [])
    ]
    now = datetime.now(timezone.utc)
    return {
        "score": float(cur["score"]),
        "prev": float(cur["previous_close"]),
        "week_avg": mean([v for t, v in hist if t >= now - timedelta(days=7)]),
        "month_avg": mean([v for t, v in hist if t >= now - timedelta(days=30)]),
    }


def vi_stats(rows: list[tuple[datetime, float]]) -> dict:
    closes = [v for _, v in rows]
    return {
        "date": rows[-1][0],
        "value": closes[-1],
        "prev": closes[-2] if len(closes) >= 2 else None,
        "week_avg": mean(closes[-5:]),    # 直近5営業日
        "month_avg": mean(closes[-21:]),  # 直近21営業日
    }


# ---------------------------------------------------------------- 表示ヘルパ

def vi_band(v: float) -> tuple[str, str, str]:
    """(数値の色, バッジ文言, バッジの色) を返す。"""
    for lo, color, badge, badge_color in VI_BANDS:
        if v >= lo:
            return color, badge, badge_color
    return INK, "", INK


def fg_band(v: float) -> tuple[str, str, str]:
    for lo, ja, en, color in FG_BANDS:
        if v >= lo:
            return ja, en, color
    return FG_BANDS[-1][1], FG_BANDS[-1][2], FG_BANDS[-1][3]


def delta_text(cur: float, prev: float | None, digits: int = 2) -> tuple[str, str]:
    """(表示文字列, 色) を返す。上昇/下降のどちらが「危ない」かは呼び出し側が決める。"""
    if prev is None:
        return "前日比 —", SUB
    d = cur - prev
    pct = f" ({d / prev * 100:+.1f}%)" if prev else ""
    return f"前日比 {d:+.{digits}f}{pct}", ""


def avg_text(week: float | None, month: float | None, unit: str) -> str:
    w = f"{week:.2f}" if week is not None else "—"
    m = f"{month:.2f}" if month is not None else "—"
    return f"週平均 {w} ／ 月平均 {m}  ({unit})"


# ---------------------------------------------------------------- Flex組み立て

def metric_block(
    title: str, note: str, value: str, value_color: str,
    badge: str, badge_color: str, delta: str, delta_color: str, avg: str,
) -> dict:
    value_row: list[dict] = [
        {"type": "text", "text": value, "size": "xxl", "weight": "bold", "color": value_color, "flex": 0},
    ]
    if badge:  # 空文字のtextコンポーネントはAPIに弾かれるので入れない
        value_row.append(
            {"type": "text", "text": badge, "size": "sm", "weight": "bold", "color": badge_color}
        )

    contents = [
        {
            "type": "box", "layout": "baseline", "contents": [
                {"type": "text", "text": title, "size": "sm", "color": SUB, "weight": "bold", "flex": 0},
                {"type": "text", "text": note, "size": "xxs", "color": FAINT, "align": "end"},
            ],
        },
        {"type": "box", "layout": "baseline", "spacing": "sm", "margin": "sm", "contents": value_row},
        {"type": "text", "text": delta, "size": "sm", "color": delta_color, "margin": "xs"},
        {"type": "text", "text": avg, "size": "xs", "color": SUB, "margin": "xs"},
    ]
    return {"type": "box", "layout": "vertical", "margin": "lg", "contents": contents}


def build_flex(vi: dict | None, fg: dict | None, today: datetime) -> dict:
    body: list[dict] = [
        {"type": "text", "text": "市況チェック", "size": "xs", "color": FAINT, "weight": "bold"},
        {
            "type": "text",
            "text": (
                f"{today.strftime('%Y/%m/%d')} ({WEEKDAY_EN[today.weekday()]}) "
                f"{today.strftime('%H:%M')}"
            ),
            "size": "lg", "weight": "bold", "color": INK,
        },
        {"type": "separator", "margin": "md"},
    ]

    if vi:
        v = vi["value"]
        v_color, v_badge, v_badge_color = vi_band(v)
        d_text, _ = delta_text(v, vi["prev"])
        # VIは上昇＝リスク上昇なので赤、低下＝緑
        d_color = SUB if vi["prev"] is None else (RED if v >= vi["prev"] else DEEP_GREEN)
        body.append(metric_block(
            title="日経VI",
            note=f"{vi['date'].strftime('%m/%d')} 終値",
            value=f"{v:.2f}",
            value_color=v_color,
            badge=v_badge,
            badge_color=v_badge_color,
            delta=d_text,
            delta_color=d_color,
            avg=avg_text(vi["week_avg"], vi["month_avg"], "5/21営業日"),
        ))
    else:
        body.append({"type": "text", "text": "日経VI 取得失敗", "size": "sm",
                     "color": RED, "margin": "lg"})

    body.append({"type": "separator", "margin": "lg"})

    if fg:
        s = fg["score"]
        ja, en, color = fg_band(s)
        d_text, _ = delta_text(s, fg["prev"], digits=1)
        # F&Gは低下＝恐怖side なので赤、上昇＝緑
        d_color = SUB if fg["prev"] is None else (DEEP_GREEN if s >= fg["prev"] else RED)
        body.append(metric_block(
            title="Fear & Greed 指数",
            note="CNN",
            value=f"{s:.0f}",
            value_color=color,
            badge=f"{ja}／{en}",
            badge_color=color,
            delta=d_text,
            delta_color=d_color,
            avg=avg_text(fg["week_avg"], fg["month_avg"], "7/30日"),
        ))
    else:
        body.append({"type": "text", "text": "F&G指数 取得失敗", "size": "sm",
                     "color": RED, "margin": "lg"})

    return {
        "type": "bubble",
        "size": "mega",
        "body": {"type": "box", "layout": "vertical", "paddingAll": "16px", "contents": body},
        "footer": {
            "type": "box", "layout": "vertical", "spacing": "sm", "contents": [
                {"type": "button", "style": "primary", "height": "sm", "color": "#42606E",
                 "action": {"type": "uri", "label": "ざっくり朝ビュー", "uri": BRIEF_URL}},
                {"type": "button", "style": "secondary", "height": "sm",
                 "action": {"type": "uri", "label": "世界の株価", "uri": WORLD_URL}},
            ],
        },
    }


def build_alt_text(vi: dict | None, fg: dict | None, today: datetime) -> str:
    parts = [f"市況 {today.strftime('%m/%d %H:%M')}"]
    if vi:
        parts.append(f"日経VI {vi['value']:.2f}")
    if fg:
        parts.append(f"F&G {fg['score']:.0f} {fg_band(fg['score'])[0]}")
    return " ｜ ".join(parts)[:400]


# ---------------------------------------------------------------- 画像URL確認

def wait_for_image(url: str, attempts: int = 10, interval: int = 6) -> bool:
    """LINEが取得できる状態か（HTTP 200・画像形式）を確認する。CDN反映待ちのリトライ付き。"""
    for i in range(1, attempts + 1):
        try:
            r = requests.get(url, timeout=20, stream=True)
            ctype = r.headers.get("Content-Type", "")
            if r.status_code == 200 and ctype.startswith("image/"):
                print(f"画像URL確認OK ({ctype}, {r.headers.get('Content-Length', '?')} bytes)")
                return True
            print(f"[{i}/{attempts}] status={r.status_code} type={ctype}")
        except requests.RequestException as e:
            print(f"[{i}/{attempts}] {type(e).__name__}: {e}")
        time.sleep(interval)
    return False


# ---------------------------------------------------------------- 送信

def push(messages: list[dict], token: str, user_id: str) -> None:
    r = requests.post(
        LINE_PUSH_URL,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"to": user_id, "messages": messages},
        timeout=TIMEOUT,
    )
    if r.status_code != 200:
        raise SystemExit(f"LINE送信失敗 status={r.status_code} body={r.text}")
    print(f"LINE送信成功（{len(messages)}通）")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="送信せず送信内容を表示")
    ap.add_argument("--skip-image-check", action="store_true", help="画像URLの到達確認を省く")
    args = ap.parse_args()

    today = datetime.now(JST)

    vi = None
    try:
        vi = vi_stats(fetch_nikkei_vi())
    except Exception as e:  # noqa: BLE001 — 片方が落ちても通知は出す
        print(f"日経VI取得エラー: {type(e).__name__}: {e}", file=sys.stderr)

    fg = None
    try:
        fg = fg_stats(fetch_fear_greed())
    except Exception as e:  # noqa: BLE001
        print(f"F&G取得エラー: {type(e).__name__}: {e}", file=sys.stderr)

    messages: list[dict] = []
    image_url = os.getenv("IMAGE_URL", "").strip()
    if image_url:
        ok = args.dry_run or args.skip_image_check or wait_for_image(image_url)
        if ok:
            messages.append({
                "type": "image",
                "originalContentUrl": image_url,
                "previewImageUrl": image_url,
            })
        else:
            print("画像URLに到達できないため画像は送りません", file=sys.stderr)

    messages.append({
        "type": "flex",
        "altText": build_alt_text(vi, fg, today),
        "contents": build_flex(vi, fg, today),
    })

    print(json.dumps(messages, ensure_ascii=False, indent=2))

    if args.dry_run:
        return
    token, user_id = os.getenv("LINE_CHANNEL_ACCESS_TOKEN"), os.getenv("LINE_USER_ID")
    if not token or not user_id:
        sys.exit("エラー: LINE_CHANNEL_ACCESS_TOKEN と LINE_USER_ID が未設定です")
    push(messages, token, user_id)


if __name__ == "__main__":
    main()
