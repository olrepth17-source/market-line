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
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

JST = timezone(timedelta(hours=9))
TIMEOUT = 30

NIKKEI_VI_CSV = "https://indexes.nikkei.co.jp/nkave/historical/nikkei_stock_average_vi_daily_jp.csv"
# ザラ場中の値（15秒更新）が載る日経公式のプロフィールページ
NIKKEI_VI_PROFILE = "https://indexes.nikkei.co.jp/nkave/index/profile?idx=nk225vi"
CNN_FG_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"

BRIEF_URL = os.getenv("BRIEF_URL", "https://olrepth17-source.github.io/morning-brief/")
WORLD_URL = "https://nikkei225jp.com/"
NIKKEI_VI_LINK = "https://www.nikkei.com/marketdata/quote/NK225VI/"
CNN_FG_LINK = "https://edition.cnn.com/markets/fear-and-greed"

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

def fetch_nikkei_vi() -> list[tuple[datetime, float, float]]:
    """日経平均VIの日次CSVを [(日付, 終値, 高値), ...] 古い順で返す。

    列並びは 日付,始値,終値,高値,安値。
    """
    r = requests.get(NIKKEI_VI_CSV, timeout=TIMEOUT, headers={"User-Agent": BROWSER_UA})
    r.raise_for_status()
    rows: list[tuple[datetime, float, float]] = []
    for cols in csv.reader(io.StringIO(r.content.decode("cp932", errors="replace"))):
        if len(cols) < 5:
            continue
        try:
            d = datetime.strptime(cols[0].strip(), "%Y/%m/%d")
            close = float(cols[2].strip())
            high = float(cols[3].strip())
        except ValueError:
            continue  # ヘッダー行・末尾の注記行
        rows.append((d, close, high))
    if not rows:
        raise ValueError("日経VIのCSVから有効な行を取得できませんでした")
    rows.sort(key=lambda r: r[0])
    return rows


def _html_to_text(html: str) -> str:
    """タグを空白に潰して、ラベルと数値の並びだけが残るテキストにする。"""
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = text.replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", text)


# 「28.38 -4.38% -1.30 2026.08.21(15:50)」の並びを拾う
# 上昇日は「+1.38」「+4.36%」のように符号が付くので [-+]? を許す
VI_RT_RE = re.compile(
    r"([-+]?[\d,]+\.\d+)\s+([-+]?[\d,]+\.\d+)%\s+([-+]?[\d,]+\.\d+)\s+"
    r"(\d{4})\.(\d{2})\.(\d{2})\((\d{2}):(\d{2})\)"
)


def fetch_vi_realtime() -> dict | None:
    """日経公式のプロフィールページからザラ場中の日経VIを取る（15秒更新）。

    取れなければ None を返し、呼び出し側は日次CSV（前営業日終値）にフォールバックする。
    """
    try:
        r = requests.get(
            NIKKEI_VI_PROFILE,
            timeout=TIMEOUT,
            headers={"User-Agent": BROWSER_UA, "Accept-Language": "ja,en;q=0.8"},
        )
        r.raise_for_status()
        text = _html_to_text(r.content.decode("utf-8", errors="replace"))
    except requests.RequestException as e:
        print(f"VIリアルタイム取得エラー: {type(e).__name__}: {e}", file=sys.stderr)
        return None

    m = VI_RT_RE.search(text)
    if not m:
        print("VIリアルタイム: ページ構造が想定と異なるためCSVにフォールバック", file=sys.stderr)
        return None

    value, pct, diff, y, mo, d, hh, mi = m.groups()

    def labeled(label: str) -> float | None:
        hit = re.search(label + r"\s*([-+]?[\d,]+\.\d+)", text)
        return float(hit.group(1).replace(",", "")) if hit else None

    return {
        "value": float(value.replace(",", "").lstrip("+")),
        "diff": float(diff.replace(",", "")),
        "pct": float(pct.replace(",", "")),
        "date": datetime(int(y), int(mo), int(d)),
        "time": f"{hh}:{mi}",
        "open": labeled("始値"),
        "high": labeled("高値"),
        "low": labeled("安値"),
    }


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
    hist = sorted(
        (datetime.fromtimestamp(p["x"] / 1000, tz=timezone.utc), float(p["y"]))
        for p in payload.get("fear_and_greed_historical", {}).get("data", [])
    )
    vals = [v for _, v in hist]
    return {
        "score": float(cur["score"]),
        "prev": float(cur["previous_close"]),
        "avg5": mean(vals[-5:]),    # 直近5データ日
        "avg25": mean(vals[-25:]),  # 直近25データ日
    }


def vi_stats(rows: list[tuple[datetime, float, float]], rt: dict | None = None) -> dict:
    """日次CSV（履歴）とリアルタイム値を合成する。

    現在値・当日の高安はリアルタイム側、5日/25日平均は確定した日足から計算する。
    """
    closes = [c for _, c, _ in rows]
    st: dict = {
        "date": rows[-1][0],
        "time": None,
        "live": False,
        "value": closes[-1],
        "high": rows[-1][2],
        "low": None,
        "open": None,
        "prev": closes[-2] if len(closes) >= 2 else None,
        "avg5": mean(closes[-5:]),    # 直近5営業日（確定値）
        "avg25": mean(closes[-25:]),  # 直近25営業日（確定値）
    }
    if not rt:
        return st

    st.update({
        "date": rt["date"],
        "time": rt["time"],
        "live": True,
        "value": rt["value"],
        "open": rt["open"],
        "low": rt["low"],
        "high": rt["high"] if rt["high"] is not None else st["high"],
        "diff": rt["diff"],
        "pct": rt["pct"],
        # 前日終値はページの前日比から逆算（表示の一貫性のため保持）
        "prev": rt["value"] - rt["diff"],
    })
    return st


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


def avg_text(a5: float | None, a25: float | None) -> str:
    v5 = f"{a5:.2f}" if a5 is not None else "—"
    v25 = f"{a25:.2f}" if a25 is not None else "—"
    return f"5日平均 {v5} ／ 25日平均 {v25}"


# ---------------------------------------------------------------- Flex組み立て

def metric_block(
    title: str, note: str, value: str, value_color: str,
    badge: str, badge_color: str, delta: str, delta_color: str, avg: str,
    extra: str = "", extra_color: str = SUB,
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
    ]
    if extra:
        contents.append({"type": "text", "text": extra, "size": "sm", "color": extra_color, "margin": "xs"})
    contents.append({"type": "text", "text": avg, "size": "xs", "color": SUB, "margin": "xs"})
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

        # 前日比はリアルタイム側の値があればそれを使う（自前の引き算より正確）
        if vi.get("diff") is not None:
            d_text = f"前日比 {vi['diff']:+.2f} ({vi['pct']:+.1f}%)"
            rising = vi["diff"] >= 0
        else:
            d_text, _ = delta_text(v, vi["prev"])
            rising = vi["prev"] is not None and v >= vi["prev"]
        # VIは上昇＝リスク上昇なので赤、低下＝緑
        d_color = SUB if vi.get("prev") is None else (RED if rising else DEEP_GREEN)

        # ザラ場中に跳ねて終値だけ収まる日があるので当日の高安を併記。40超なら色を付ける
        high = vi.get("high")
        high_color = vi_band(high)[0] if high is not None else SUB
        day_parts = []
        if high is not None:
            day_parts.append(f"高値 {high:.2f}")
        if vi.get("low") is not None:
            day_parts.append(f"安値 {vi['low']:.2f}")
        if vi.get("open") is not None:
            day_parts.append(f"始値 {vi['open']:.2f}")

        if vi.get("live"):
            note = f"{vi['date'].strftime('%m/%d')} {vi['time']} 時点"
            day_label = "本日 "
        else:
            note = f"{vi['date'].strftime('%m/%d')} 終値"
            day_label = "当日 "

        body.append(metric_block(
            title="日経VI",
            note=note,
            value=f"{v:.2f}",
            value_color=v_color,
            badge=v_badge,
            badge_color=v_badge_color,
            delta=d_text,
            delta_color=d_color,
            avg=avg_text(vi["avg5"], vi["avg25"]),
            extra=(day_label + " ／ ".join(day_parts)) if day_parts else "",
            extra_color=SUB if high_color == INK else high_color,
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
            avg=avg_text(fg["avg5"], fg["avg25"]),
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
                {
                    "type": "box", "layout": "horizontal", "contents": [
                        {"type": "button", "style": "link", "height": "sm",
                         "action": {"type": "uri", "label": "日経VI", "uri": NIKKEI_VI_LINK}},
                        {"type": "button", "style": "link", "height": "sm",
                         "action": {"type": "uri", "label": "F&G (CNN)", "uri": CNN_FG_LINK}},
                    ],
                },
            ],
        },
    }


def build_alt_text(vi: dict | None, fg: dict | None, today: datetime) -> str:
    parts = [f"市況 {today.strftime('%m/%d %H:%M')}"]
    if vi:
        stamp = f"({vi['time']})" if vi.get("live") else "(終値)"
        parts.append(f"日経VI {vi['value']:.2f}{stamp}")
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
        vi = vi_stats(fetch_nikkei_vi(), fetch_vi_realtime())
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
