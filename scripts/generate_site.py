"""
docs/archive/*.html を読み込んで docs/index.html を自動生成するスクリプト。
GitHub Actions から毎日実行される。
"""

import re
import sys
from pathlib import Path
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR  = Path(__file__).parent.parent
DOCS_DIR  = BASE_DIR / "docs"
ARCHIVE_DIR = DOCS_DIR / "archive"


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
      <div class="card-header">{badge}<div class="card-date">{info['date_str']}</div></div>
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
  <div class="hero-label">LATEST REPORT</div>
  <div class="hero-date">{latest['date_str']}</div>
  <div class="hero-nikkei">{hero_nikkei}</div>
  <a href="archive/{latest['filename']}" class="hero-btn">今日のレポートを読む →</a>
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
