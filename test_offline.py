"""外部通信なしで、平均計算・色分岐・Flex構造を検証する。

サンドボックスから日経・CNN・LINEへは到達できないため、requests.get をスタブに差し替える。
"""
import json
import re
import types
from datetime import datetime, timedelta, timezone

import notify_line as nl

# --- スタブデータ -----------------------------------------------------------
# 日経VI: 直近25営業日ぶん。最終日 31.67 / 前日 31.81
VI_VALUES = [
    20.1, 20.5, 21.0, 22.4, 23.9, 24.1, 25.0, 26.2, 27.5, 28.0,
    28.4, 29.1, 30.2, 31.0, 31.5, 32.0, 33.1, 34.0, 35.2, 30.5,
    29.9, 30.4, 31.2, 31.81, 31.67,
]
_head = '"データ日付","始値","終値","高値","安値"\n'
_rows = "".join(
    f'"2026/07/{i + 1:02d}","{v:.2f}","{v:.2f}","{v:.2f}","{v:.2f}"\n'
    for i, v in enumerate(VI_VALUES)
)
VI_CSV = (_head + _rows + "著作権表示などの注記行\n").encode("cp932")

_now = datetime.now(timezone.utc)
FG_PAYLOAD = {
    "fear_and_greed": {"score": 66.03, "rating": "greed", "previous_close": 61.4},
    "fear_and_greed_historical": {
        "data": (
            # 直近7日: 60〜66 / 8〜30日前: 45前後 → 週平均と月平均が明確に違う値になる
            [{"x": (_now - timedelta(days=d)).timestamp() * 1000, "y": 60 + d} for d in range(0, 7)]
            + [{"x": (_now - timedelta(days=d)).timestamp() * 1000, "y": 45.0} for d in range(8, 30)]
        )
    },
}


class FakeResp:
    def __init__(self, content=b"", payload=None, status=200, headers=None):
        self.content, self._payload, self.status_code = content, payload, status
        self.headers = headers or {}
        self.text = ""

    def raise_for_status(self):
        if self.status_code != 200:
            raise RuntimeError(self.status_code)

    def json(self):
        return self._payload


def fake_get(url, timeout=None, headers=None, stream=None):
    if url == nl.NIKKEI_VI_CSV:
        return FakeResp(content=VI_CSV)
    if url == nl.CNN_FG_URL:
        return FakeResp(payload=FG_PAYLOAD)
    raise AssertionError(f"想定外のURL: {url}")


nl.requests = types.SimpleNamespace(get=fake_get, post=None, RequestException=Exception)

# --- 統計 -------------------------------------------------------------------
vi = nl.vi_stats(nl.fetch_nikkei_vi())
assert abs(vi["value"] - 31.67) < 1e-9
assert abs(vi["prev"] - 31.81) < 1e-9
assert abs(vi["week_avg"] - sum(VI_VALUES[-5:]) / 5) < 1e-9, "週平均は直近5営業日"
assert abs(vi["month_avg"] - sum(VI_VALUES[-21:]) / 21) < 1e-9, "月平均は直近21営業日"

fg = nl.fg_stats(nl.fetch_fear_greed())
assert abs(fg["score"] - 66.03) < 1e-9 and abs(fg["prev"] - 61.4) < 1e-9
assert 62.9 < fg["week_avg"] < 63.1, f"週平均が想定外: {fg['week_avg']}"
assert 48.0 < fg["month_avg"] < 52.0, f"月平均が想定外: {fg['month_avg']}"

# --- 色分岐 -----------------------------------------------------------------
assert nl.vi_band(55)[0] == nl.RED and nl.vi_band(50)[0] == nl.RED
assert nl.vi_band(49.9)[0] == nl.ORANGE and nl.vi_band(40)[0] == nl.ORANGE
assert nl.vi_band(39.9)[0] == nl.INK and nl.vi_band(39.9)[1] == "30超"
assert nl.vi_band(29.9)[0] == nl.INK and nl.vi_band(29.9)[1] == ""

bands = {
    100: "極端な強欲", 76: "極端な強欲", 75: "強欲", 55: "強欲",
    54: "中立", 46: "中立", 45: "恐怖", 26: "恐怖", 25: "極端な恐怖", 0: "極端な恐怖",
}
for score, ja in bands.items():
    assert nl.fg_band(score)[0] == ja, f"F&G {score} の区分が {nl.fg_band(score)[0]}"
# 下の区分ほど赤に近い順序になっているか
order = [nl.fg_band(s)[2] for s in (90, 65, 50, 35, 10)]
assert order == [nl.DEEP_GREEN, nl.GREEN, nl.GRAY, nl.ORANGE, nl.RED]

# --- Flex構造 ---------------------------------------------------------------
today = datetime(2026, 8, 14, 7, 30, tzinfo=nl.JST)
flex = nl.build_flex(vi, fg, today)
alt = nl.build_alt_text(vi, fg, today)
payload = json.dumps(flex, ensure_ascii=False)

assert flex["type"] == "bubble" and flex["size"] == "mega"
assert "2026/08/14 (Fri)" in payload
assert "31.67" in payload and "66" in payload
assert "強欲／Greed" in payload
assert "週平均" in payload and "月平均" in payload
assert len(alt) <= 400 and "日経VI" in alt


def walk(node, path="root"):
    """LINE Flexの基本ルールを再帰チェック。"""
    if isinstance(node, dict):
        t = node.get("type")
        if t == "text":
            assert isinstance(node.get("text"), str) and node["text"] != "", f"空テキスト: {path}"
            if "color" in node:
                assert re.fullmatch(r"#[0-9A-Fa-f]{6}", node["color"]), f"色形式: {path}"
        if t == "box":
            assert node.get("layout") in ("vertical", "horizontal", "baseline"), path
            assert node.get("contents"), f"空のbox: {path}"
            if node["layout"] == "baseline":
                for c in node["contents"]:
                    assert c["type"] in ("text", "icon", "filler"), f"baseline直下: {path}"
        if t == "button":
            label = node["action"]["label"]
            assert len(label) <= 20, f"ボタンラベル20字超: {label}"
            assert node["action"]["uri"].startswith("https://"), path
        for k, v in node.items():
            walk(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            walk(v, f"{path}[{i}]")


walk(flex)

# リンク2つが末尾にあるか
labels = [b["action"]["label"] for b in flex["footer"]["contents"]]
assert labels == ["ざっくり朝ビュー", "世界の株価"], labels
uris = [b["action"]["uri"] for b in flex["footer"]["contents"]]
assert uris[0].startswith("https://olrepth17-source.github.io/morning-brief")
assert uris[1] == "https://nikkei225jp.com/"

# --- 片方が落ちても通知は成立するか ----------------------------------------
flex_partial = nl.build_flex(None, fg, today)
walk(flex_partial)
assert "日経VI 取得失敗" in json.dumps(flex_partial, ensure_ascii=False)

# --- VIが高い日の見た目 ------------------------------------------------------
vi_high = dict(vi, value=52.4, prev=41.0)
flex_high = nl.build_flex(vi_high, fg, today)
walk(flex_high)
assert nl.RED in json.dumps(flex_high) and "⚠ 50超" in json.dumps(flex_high, ensure_ascii=False)

print("全アサーション通過")
print(json.dumps(flex, ensure_ascii=False, indent=2)[:1200])
print("...")
print("altText:", alt)
