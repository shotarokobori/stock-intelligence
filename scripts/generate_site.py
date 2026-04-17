"""
docs/archive/*.html を読み込んで docs/index.html を自動生成するスクリプト。
GitHub Actions から毎日実行される。
"""

import json
import re
import sys
from pathlib import Path
from datetime import datetime

import yfinance as yf

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR  = Path(__file__).parent.parent
DOCS_DIR  = BASE_DIR / "docs"
ARCHIVE_DIR = DOCS_DIR / "archive"
PICK_FILE = BASE_DIR / "data" / "pick.json"


def get_pick_chart_svg() -> str:
    """激推し株の直近30日足チャートをSVGで返す。取得失敗時は空文字。"""
    if not PICK_FILE.exists():
        return ""
    try:
        with open(PICK_FILE, encoding="utf-8") as f:
            pick = json.load(f)
        hist = yf.Ticker(pick["ticker"]).history(period="30d")
        closes = hist["Close"].tolist()
        if len(closes) < 5:
            return ""
        mn, mx = min(closes), max(closes)
        if mx == mn:
            return ""

        W, H = 1100, 320
        pad_l, pad_r, pad_t, pad_b = 70, 10, 35, 50

        def price_to_y(p):
            return H - pad_b - (p - mn) / (mx - mn) * (H - pad_t - pad_b)

        def idx_to_x(i):
            return pad_l + i / (len(closes) - 1) * (W - pad_l - pad_r)

        pts = [(idx_to_x(i), price_to_y(c)) for i, c in enumerate(closes)]
        line = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in pts)
        fill = line + f" L {pts[-1][0]:.1f} {H - pad_b:.1f} L {pad_l} {H - pad_b:.1f} Z"

        # 価格軸の目盛り：上2桁以下を0に（桁数から単位を決定）
        digits = len(str(int(mn)))
        unit = 10 ** (digits - 2)
        tick_lo = (int(mn) // unit + 1) * unit
        tick_hi = (int(mx) // unit) * unit
        ticks = list(range(tick_lo, tick_hi + 1, unit))
        # 多すぎる場合は間引く
        while len(ticks) > 6 and unit > 0:
            unit *= 2
            tick_lo = (int(mn) // unit + 1) * unit
            tick_hi = (int(mx) // unit) * unit
            ticks = list(range(tick_lo, tick_hi + 1, unit))

        grid_svg = ""
        for tick in ticks:
            y = price_to_y(tick)
            if pad_t <= y <= H - pad_b:
                label = f"{tick:,}"
                grid_svg += (
                    f'  <line x1="{pad_l}" y1="{y:.1f}" x2="{W - pad_r}" y2="{y:.1f}"'
                    f' stroke="#a8c8e8" stroke-width="0.6" opacity="0.2"/>\n'
                    f'  <text x="{pad_l - 6}" y="{y + 4:.1f}" text-anchor="end"'
                    f' font-size="11" fill="#a8c8e8" opacity="0.45"'
                    f' font-family="monospace">{label}</text>\n'
                )

        name_label = (
            f'  <text x="{W - pad_r - 8}" y="{H - pad_b + 34:.1f}" text-anchor="end"'
            f' font-size="11" fill="#a8c8e8" opacity="0.38"'
            f' font-family="sans-serif">{pick["name"]} 30日チャート</text>'
        )

        return f"""<svg class="hero-chart" xmlns="http://www.w3.org/2000/svg"
  viewBox="0 0 {W} {H}" preserveAspectRatio="none">
  <defs>
    <linearGradient id="cg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#8ab4d8" stop-opacity="0.18"/>
      <stop offset="100%" stop-color="#8ab4d8" stop-opacity="0"/>
    </linearGradient>
  </defs>
{grid_svg}  <path d="{fill}" fill="url(#cg)"/>
  <path d="{line}" fill="none" stroke="#a8c8e8" stroke-width="2" opacity="0.45"/>
{name_label}
</svg>"""
    except Exception:
        return ""


def extract_info(html_path: Path) -> dict | None:
    """HTMLファイルから日付・日経平均を抽出する"""
    try:
        content = html_path.read_text(encoding="utf-8")
    except Exception:
        return None

    # ファイル名（YYYYMMDD.html）から日付を取得
    try:
        date = datetime.strptime(html_path.stem, "%Y%m%d")
        date_str = f"{date.year}年{date.month}月{date.day}日"
    except ValueError:
        return None

    # 日経平均を抽出（HTMLタグをスキップして数値を取得）
    nikkei = None
    m = re.search(r'日経平均[：:]\s*(?:<[^>]+>)*\s*([0-9]{2,3},[0-9]{3}(?:\.[0-9]+)?)', content)
    if m:
        nikkei = m.group(1)

    # 1行見出しを抽出
    headline = ""
    m = re.search(r'class="daily-headline"[^>]*>\s*([^<]+)\s*<', content)
    if m:
        headline = m.group(1).strip()

    return {"filename": html_path.name, "date": date, "date_str": date_str, "nikkei": nikkei, "headline": headline}


def make_card(info: dict, is_latest: bool) -> str:
    badge    = '<span class="badge">最新</span>' if is_latest else ""
    nikkei   = f'<div class="card-nikkei">日経平均 {info["nikkei"]}円</div>' if info["nikkei"] else ""
    headline = f'<div class="card-headline">{info["headline"]}</div>' if info.get("headline") else ""
    return f"""    <a href="archive/{info['filename']}" class="card">
      <div class="card-header"><div class="card-date">{info['date_str']}</div>{badge}</div>
      {nikkei}
      {headline}
      <div class="card-link">レポートを読む →</div>
    </a>"""


def main():
    files   = sorted(ARCHIVE_DIR.glob("[0-9]*.html"), reverse=True)
    reports = [r for f in files if (r := extract_info(f))]

    if not reports:
        print("アーカイブファイルが見つかりません")
        return

    latest      = reports[0]
    hero_nikkei = f"日経平均 {latest['nikkei']}円" if latest["nikkei"] else ""
    cards_html  = "\n".join(make_card(r, i == 0) for i, r in enumerate(reports))
    pick_chart  = get_pick_chart_svg()

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>まいにち日本株短信 | アーカイブ</title>
<link rel="stylesheet" href="assets/style.css">
</head>
<body>

<header class="site-header">
  <div class="header-inner">
    <div class="logo">まいにち日本株短信</div>
    <div class="tagline">AI分析による日本株デイリーレポート</div>
  </div>
</header>

<section class="hero">
  {pick_chart}
  <div class="hero-content">
    <div class="hero-label">LATEST REPORT</div>
    <div class="hero-date">{latest['date_str']}</div>
    <div class="hero-nikkei">{hero_nikkei}</div>
    <a href="archive/{latest['filename']}" class="hero-btn">今日のレポートを読む →</a>
  </div>
</section>

<main class="main-content">
  <div class="section-title">過去のレポート一覧</div>
  <div class="archive-grid">
{cards_html}
  </div>
</main>

<footer class="site-footer">
  <p>まいにち日本株短信 &nbsp;|&nbsp; AI分析による日本株デイリーレポート</p>
  <p>本サイトの情報は投資判断の参考情報です。投資はご自身の責任で行ってください。</p>
</footer>

</body>
</html>"""

    out = DOCS_DIR / "index.html"
    out.write_text(html, encoding="utf-8")
    print(f"生成完了: {out}  ({len(reports)}件)")


if __name__ == "__main__":
    main()
