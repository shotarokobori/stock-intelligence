"""
=============================================================
毎日自動学習メール配信システム
目的：日本株運用で勝つための情報収集・統合分析・配信
=============================================================
作者向けメモ：
- このファイルを実行するだけですべての処理が動きます
- 設定は config.json、ソースは sources.json で管理します
- テスト送信: python main.py --test
- 本番実行:   python main.py
"""

import json
import logging
import os
import re
import smtplib
import sys
import traceback

# Windowsのコマンドプロンプトで日本語・絵文字を正しく表示するための設定
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import anthropic
import feedparser
import requests
import yfinance as yf
from googleapiclient.discovery import build
from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound, TranscriptsDisabled

# ─────────────────────────────────────────────
# 初期設定
# ─────────────────────────────────────────────

BASE_DIR      = Path(__file__).parent
LOG_DIR       = BASE_DIR / "logs"
DATA_DIR      = BASE_DIR / "data"
PICK_FILE     = DATA_DIR / "pick.json"
SITE_BASE_URL = "https://shotarokobori.github.io/stock-intelligence"
LOG_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "system.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


def load_json(path: Path) -> dict:
    """JSONファイルを読み込む"""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_env(path: Path) -> dict:
    """.envファイルを読み込む"""
    env = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip()
    except FileNotFoundError:
        log.warning(f".envファイルが見つかりません: {path}")
    return env


def parse_bool(value, default=False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def parse_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_list(value):
    if isinstance(value, list):
        return value
    if not value:
        return []
    return [item.strip() for item in str(value).split(",") if item.strip()]


# ─────────────────────────────────────────────
# ① リアルタイム市場データ取得
# ─────────────────────────────────────────────

def fetch_market_data() -> dict:
    """
    yfinance を使って主要市場指数のリアルタイムデータを取得する。
    取得できなかった場合は None を返す。
    返り値: {"nikkei": 数値, "usdjpy": 数値, "dow": 数値, "sp500": 数値}
    """
    result = {}
    symbols = {
        "nikkei": "^N225",    # 日経平均株価
        "usdjpy": "USDJPY=X", # ドル円
        "dow":    "^DJI",     # NYダウ
        "sp500":  "^GSPC",    # S&P500
    }
    try:
        for key, symbol in symbols.items():
            try:
                ticker = yf.Ticker(symbol)
                price = ticker.fast_info.get("lastPrice") or ticker.fast_info.get("previousClose")
                result[key] = round(float(price), 2) if price else None
            except Exception as e:
                log.warning(f"  市場データ取得失敗 [{symbol}]: {e}")
                result[key] = None
        log.info(f"  市場データ取得完了: 日経={result.get('nikkei')}, ドル円={result.get('usdjpy')}, NYダウ={result.get('dow')}")
    except Exception as e:
        log.warning(f"  市場データ全体の取得に失敗しました: {e}")
    return result


# ─────────────────────────────────────────────
# ② RSSニュース収集
# ─────────────────────────────────────────────

def fetch_rss_articles(feed_info: dict, max_articles: int, preview_chars: int) -> list[dict]:
    """
    RSSフィードから新着記事を取得する。
    返り値: [{"source": ..., "title": ..., "url": ..., "preview": ..., "published": ...}, ...]
    """
    articles = []
    try:
        feed = feedparser.parse(feed_info["url"])
        # 24時間以内の記事のみ対象
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

        for entry in feed.entries[:max_articles * 2]:  # 多めに取ってフィルタ
            # 公開日チェック（ない場合は今日とみなす）
            published = None
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                import time
                published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
            else:
                published = datetime.now(timezone.utc)

            if published < cutoff:
                continue

            # 本文プレビュー取得
            preview = ""
            if hasattr(entry, "summary"):
                preview = entry.summary[:preview_chars]
            elif hasattr(entry, "description"):
                preview = entry.description[:preview_chars]

            # HTMLタグを除去
            import re
            preview = re.sub(r"<[^>]+>", "", preview).strip()

            articles.append({
                "source": feed_info["name"],
                "title":  entry.get("title", "（タイトルなし）"),
                "url":    entry.get("link", ""),
                "preview": preview,
                "published": published.strftime("%Y-%m-%d %H:%M"),
            })

            if len(articles) >= max_articles:
                break

        log.info(f"  RSS [{feed_info['name']}]: {len(articles)}件取得")
    except Exception as e:
        log.warning(f"  RSS [{feed_info['name']}] 取得エラー: {e}")

    return articles


# ─────────────────────────────────────────────
# ② YouTube動画収集 + 字幕取得
# ─────────────────────────────────────────────

def fetch_youtube_videos(channel_info: dict, api_key: str,
                         max_results: int, hours: int) -> list[dict]:
    """
    YouTubeチャンネルから新着動画を取得し、字幕も取得する。
    返り値: [{"source": ..., "title": ..., "url": ..., "description": ..., "transcript": ...}, ...]
    """
    videos = []
    try:
        youtube = build("youtube", "v3", developerKey=api_key)
        published_after = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

        # 新着動画を検索
        response = youtube.search().list(
            channelId=channel_info["channel_id"],
            part="snippet",
            order="date",
            publishedAfter=published_after,
            maxResults=max_results,
            type="video",
        ).execute()

        for item in response.get("items", []):
            video_id = item["id"]["videoId"]
            snippet  = item["snippet"]
            title    = snippet.get("title", "（タイトルなし）")
            desc     = snippet.get("description", "")[:500]

            # 字幕を取得（なければ空文字）
            transcript_text = ""
            try:
                transcript_list = YouTubeTranscriptApi.get_transcript(
                    video_id, languages=["ja", "en"]
                )
                # 字幕を結合して指定文字数まで
                full_text = " ".join([t["text"] for t in transcript_list])
                transcript_text = full_text[:3000]
            except (NoTranscriptFound, TranscriptsDisabled):
                log.info(f"    字幕なし: {title}")
            except Exception as e:
                log.warning(f"    字幕取得エラー [{title}]: {e}")

            videos.append({
                "source":     channel_info["name"],
                "title":      title,
                "url":        f"https://www.youtube.com/watch?v={video_id}",
                "description": desc,
                "transcript": transcript_text,
                "type":       "youtube",
            })

        log.info(f"  YouTube [{channel_info['name']}]: {len(videos)}件取得")
    except Exception as e:
        log.warning(f"  YouTube [{channel_info['name']}] 取得エラー: {e}")

    return videos


# ─────────────────────────────────────────────
# ③ Claude APIで統合分析レポートを生成
# ─────────────────────────────────────────────

def build_prompt(articles: list[dict], videos: list[dict], market_data: dict | None = None, pick_context: str = "") -> str:
    """
    Claude に渡すプロンプトを組み立てる。
    日本株運用で勝つことを目的とした統合分析レポートを要求する。
    """
    today = datetime.now().strftime("%Y年%m月%d日（%A）")

    # ── リアルタイム市場データ ──
    if market_data:
        def fmt(v, suffix=""):
            return f"{v:,.2f}{suffix}" if v is not None else "取得不可"
        market_text = f"""
━━━━━━━━━━━━━━━━━━━━━━━━
■ 本日のリアルタイム市場データ（{today}時点）
━━━━━━━━━━━━━━━━━━━━━━━━
・日経平均株価: {fmt(market_data.get('nikkei'))} 円
・ドル円:       {fmt(market_data.get('usdjpy'))} 円
・NYダウ:       {fmt(market_data.get('dow'))} ドル
・S&P500:       {fmt(market_data.get('sp500'))}

※上記は取得時点の実際の値です。レポート内で日経平均などの株価水準に言及する際は、必ずこの数値を基準にしてください。
"""
    else:
        market_text = ""

    # ── ニュース記事パート ──
    articles_text = ""
    for i, a in enumerate(articles, 1):
        articles_text += f"""
【ニュース記事 {i}】
ソース: {a['source']}
タイトル: {a['title']}
URL: {a['url']}
要点: {a['preview']}
---"""

    # ── YouTube動画パート ──
    videos_text = ""
    for i, v in enumerate(videos, 1):
        content = v["transcript"] if v["transcript"] else v["description"]
        content_label = "字幕" if v["transcript"] else "概要欄"
        videos_text += f"""
【YouTube動画 {i}】
チャンネル: {v['source']}
タイトル: {v['title']}
URL: {v['url']}
{content_label}: {content}
---"""

    # ── プロンプト本文 ──
    prompt = f"""あなたは日本株投資の専門アナリストです。
今日（{today}）収集した以下の金融ニュースとYouTube動画を、日本株運用で利益を上げることを唯一の目的として分析し、統合レポートを作成してください。
{market_text}
━━━━━━━━━━━━━━━━━━━━━━━━
■ 今日のニュース記事
━━━━━━━━━━━━━━━━━━━━━━━━
{articles_text if articles_text else "（本日の新着記事なし）"}

━━━━━━━━━━━━━━━━━━━━━━━━
■ 今日のYouTube動画
━━━━━━━━━━━━━━━━━━━━━━━━
{videos_text if videos_text else "（本日の新着動画なし）"}

━━━━━━━━━━━━━━━━━━━━━━━━
■ レポート作成の指示
━━━━━━━━━━━━━━━━━━━━━━━━

以下の構成で、スタイル付きのHTMLコンテンツを日本語で作成してください。
※出力はbodyタグ内に入れるdivのみ。DOCTYPE・html・head・bodyタグは不要。
※出力の一番最初の行に、その日のレポート全体を1行で表現した見出しを必ず入れること。形式は以下の通り（このタグのみ・改行なし）：
<p class="daily-headline">（ここに20字以内の1行見出し）</p>
例：「米利上げ懸念が再燃、日経は下値模索へ」「TSMC好決算で半導体株に追い風」
※その直後に「まいにち日本株短信」というh1タイトルを入れること。「日本株デイリーレポート」「日本株投資レポート」等の別タイトルは使わないこと。
※スマホ優先デザイン：固定px幅は使わずmax-widthと%を使うこと。文字サイズは本文14px。背景は白または明るい色ベース。余白は指タップしやすい大きさに。長い文章は適切な位置で改行し、読みやすい行長（1行25〜35文字程度）を心がけること。
※全体の目標トークン数：6000以内。最大8000以内。余計な装飾・繰り返しは省くこと。

【取捨選択の指示】
収集した全記事・動画の中から、日本株への影響度が高いものだけを選んで分析すること。
重要度が低い・関連性が薄いと判断した記事は完全に無視してよい。
その日のニュース状況をもとに毎回動的に判断すること。

【①　1行サマリー（計5件）】
以下の2グループに分けて、それぞれ番号を振ること。番号は②の番号と完全に一致させること。

▼ マクロ重要ニュース（3件）：日本株市場全体に最も影響を与えるニュースを3件、影響度が高い順に1行で要約。
▼ 個別株・セクター注目ニュース（2件）：特定の銘柄やセクターへの影響が大きいニュースを2件、1行で要約。

【②　要点と解説】
①で選んだ5件それぞれについて：
- 何が起きたか（1〜2行）
- なぜ重要か・背景（2〜3行）
- 専門用語は「※〇〇とは：〜」で必ず注釈
- 日本株・関連銘柄への影響（2〜3行）

【③　横断・統合考察】
複数ソースを横断して見えてくる今日のテーマ・流れを丁寧に分析すること。
因果関係・リスク・見落とされがちな視点を含め、300字程度でしっかり書くこと。

【④　今後1〜2週間の注目ポイント】
箇条書き3〜4項目のみ。日付・イベント・影響を簡潔に。

【⑤　日本株・個別株への見通し（☆最重要・絶対に省略しない）】
このセクションは最も丁寧に・最も具体的に書くこと。

- 日経平均の方向性（↑↓→）と根拠（3〜4行）
- TOPIXの方向性（↑↓→）と根拠（2〜3行）
- 注目セクター：3〜5業種（方向・理由を各1〜2行）
- 注目個別銘柄：必ず3〜5銘柄（銘柄名・証券コード・注目理由・リスクを各2行）
- 今日の総合的な投資スタンス（2〜3行）
※投資は自己責任である旨を添える

①〜⑤を必ずすべて完結させること。⑤は絶対に省略しないこと。

【⑦　本日の激推し株（必須・絶対に省略しない）】
あなたが最も自信を持って「今日中に上がりそう」と思う株を1銘柄だけ選ぶこと。
ニュースや市場データを参考にしても、独自の判断でも良い。

必ず以下のdata属性を持つspan要素を含めること（システムが読み取るため必須）：
<span class="pick-meta" data-ticker="[証券コード].T" data-name="[銘柄名]" style="display:none;"></span>

表示フォーマット（HTMLで生成）：
- 背景：濃紺グラデーション（#1a237e → #283593）、角丸、余白あり、白文字
- 見出し：🌱 本日の激推し株 🌱（ゴールド色・大きめ）
- 銘柄名と証券コード・現在株価（取得できない場合は「株価取得中」）
- 推奨理由を2〜3行（簡潔に）

{pick_context}

⑦は絶対に省略しないこと。"""

    return prompt


def generate_report_with_claude(prompt: str, config: dict) -> str:
    """
    Claude API を呼び出してレポートを生成する。
    """
    client = anthropic.Anthropic(api_key=config["anthropic"]["api_key"])

    log.info("Claude API にリクエスト送信中...")
    message = client.messages.create(
        model=config["anthropic"]["model"],
        max_tokens=config["anthropic"]["max_tokens"],
        messages=[{"role": "user", "content": prompt}],
    )
    log.info("Claude API からレポート受信完了")
    return message.content[0].text


# ─────────────────────────────────────────────
# ④ HTMLメール組み立て
# ─────────────────────────────────────────────

def build_html_email(report_text: str, articles: list[dict], videos: list[dict]) -> str:
    """
    Claude が生成したレポートをHTMLメールに変換する。
    """
    today = datetime.now().strftime("%Y年%m月%d日")

    # ClaudeがHTML形式で生成したレポートをそのまま使用する
    report_html = report_text

    # ソース名をコンパクトにまとめる（重複除去）
    source_names = list(dict.fromkeys(
        [a["source"] for a in articles] + [v["source"] for v in videos]
    ))
    sources_text = "　｜　".join(source_names)

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>まいにち日本株短信｜{today}</title>
<style>
  body {{ margin:0; padding:0; background:#fff; }}
  .site-nav {{ background:#1a1f3a; border-bottom:2px solid #c9a84c; padding:12px 20px; }}
  .site-nav a {{ color:#c9a84c; text-decoration:none; font-weight:700; font-size:14px; letter-spacing:0.3px; }}
  .inner {{ width:100%; max-width:680px; margin:0 auto; background:#fff; }}
  .body  {{ padding:16px; font-size:15px; line-height:1.8; color:#333; }}
  .sources {{ padding:4px 16px 12px; border-top:1px solid #e8e8e8; }}
  .footer {{ background:#f9f9f9; padding:12px 16px; border-top:1px solid #e0e0e0; }}
  .footer p {{ margin:0; font-size:11px; color:#999; line-height:1.6; }}
  @media (min-width:480px) {{
    .body  {{ padding:24px 32px; }}
    .sources {{ padding:4px 32px 16px; }}
    .footer {{ padding:14px 32px; }}
  }}
</style>
</head>
<body>
  <div class="site-nav">
    <a href="../">← まいにち日本株短信</a>
  </div>
  <div class="inner">

    <!-- 本文（Claudeが生成したヘッダー込み） -->
    <div class="body">
      {report_html}
    </div>

    <!-- ソース一覧（コンパクト） -->
    <div class="sources">
      <p style="margin:8px 0 0;font-size:11px;color:#aaa;">📎 {sources_text}</p>
    </div>

    <!-- フッター -->
    <div class="footer">
      <p>このメールは自動生成されたAI分析レポートです。投資判断はご自身の責任で行ってください。</p>
    </div>

  </div>
</body>
</html>"""
    return html


# ─────────────────────────────────────────────
# ⑤ Gmail送信
# ─────────────────────────────────────────────

def send_gmail(html_content: str, config: dict, test_mode: bool = False):
    """
    HTMLメールをGmailで送信する。
    test_mode=True の場合は送信せずにファイルに保存する。
    """
    today = datetime.now().strftime("%Y年%m月%d日")
    subject = f"📊 まいにち日本株短信｜{today}"

    if test_mode:
        # テストモード：HTMLファイルに保存
        output_path = DATA_DIR / f"test_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html_content)
        log.info(f"[テストモード] レポートをファイルに保存しました: {output_path}")
        print(f"\n✅ テストレポートを保存しました: {output_path}")
        print("このファイルをブラウザで開くとメールのプレビューが確認できます。\n")
        return

    # 本番送信
    # 複数送信先に対応（recipient_emails が list の場合も、recipient_email の場合も両方OK）
    gmail_cfg  = config["gmail"]
    recipients = gmail_cfg.get("recipient_emails", gmail_cfg.get("recipient_email", ""))
    if isinstance(recipients, str):
        recipients = [recipients]  # 文字列の場合はリストに変換

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = gmail_cfg["sender_email"]
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_cfg["sender_email"], gmail_cfg["sender_password"])
        server.send_message(msg)

    log.info(f"メール送信完了: {', '.join(recipients)}")


# ─────────────────────────────────────────────
# ⑥ LINE送信
# ─────────────────────────────────────────────

def extract_headline(report_text: str) -> str:
    """レポートHTMLから1行見出しを抽出する"""
    m = re.search(r'class="daily-headline"[^>]*>\s*([^<]+)\s*<', report_text)
    return m.group(1).strip() if m else ""


def send_line(report_text: str, config: dict, test_mode: bool = False) -> None:
    """
    LINE Messaging API でレポートURLを一斉送信する。
    形式：タイトル・1行見出し・サイトURL
    """
    line_cfg = config["line"]
    token    = line_cfg.get("channel_access_token", "")

    if not token:
        log.warning("LINEチャンネルアクセストークンが未設定です。送信をスキップします。")
        return

    today    = datetime.now()
    date_str = f"{today.month}月{today.day}日"
    date_key = today.strftime("%Y%m%d")
    headline = extract_headline(report_text)
    url      = f"{SITE_BASE_URL}/archive/{date_key}.html"

    message = f"📊 まいにち日本株短信　{date_str}\n{headline}\n{url}"

    if test_mode:
        log.info("[テストモード] LINE送信スキップ")
        log.info(f"  送信予定メッセージ:\n{message}")
        print("\n✅ [テストモード] LINE送信スキップ\n")
        return

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"messages": [{"type": "text", "text": message}]}
    response = requests.post(
        "https://api.line.me/v2/bot/message/broadcast",
        headers=headers,
        json=payload,
    )
    if response.status_code == 200:
        log.info("LINE一斉送信完了")
        print("\n✅ LINE一斉送信完了\n")
    else:
        log.error(f"LINE送信失敗: {response.status_code} {response.text}")
        raise Exception(f"LINE送信エラー: {response.status_code} {response.text}")


# ─────────────────────────────────────────────
# ⑦ 激推し株：昨日データ読み込み・損益計算・今日分保存
# ─────────────────────────────────────────────

def load_yesterday_pick() -> dict | None:
    """昨日の激推し株データをpick.jsonから読み込む"""
    if not PICK_FILE.exists():
        return None
    try:
        with open(PICK_FILE, encoding="utf-8") as f:
            data = json.load(f)
        pick_date = datetime.strptime(data["date"], "%Y-%m-%d").date()
        days_diff  = (datetime.now().date() - pick_date).days
        if days_diff > 4:  # 土日・祝日を考慮し4日以上前なら無効
            return None
        return data
    except Exception as e:
        log.warning(f"昨日の激推し株データ読み込みエラー: {e}")
        return None


def fetch_pick_result(pick: dict) -> dict | None:
    """昨日の激推し株の寄り値→引き値を取得し100株損益を計算する"""
    try:
        ticker = yf.Ticker(pick["ticker"])
        hist   = ticker.history(period="5d")
        if hist.empty:
            return None
        latest      = hist.iloc[-1]
        open_price  = round(float(latest["Open"]),  1)
        close_price = round(float(latest["Close"]), 1)
        profit      = int((close_price - open_price) * 100)
        return {
            "name": pick["name"], "code": pick["code"],
            "open": open_price,   "close": close_price, "profit": profit,
        }
    except Exception as e:
        log.warning(f"激推し株価格取得エラー: {e}")
        return None


def fetch_previous_close(ticker: str) -> str:
    """銘柄の前日終値を取得して「前日終値：XX,XXX円」形式で返す"""
    try:
        t = yf.Ticker(ticker)
        price = t.fast_info.get("previousClose")
        if price:
            return f"前日終値：{int(price):,}円"
        # fast_info で取れない場合は history から取得
        hist = t.history(period="5d")
        if not hist.empty:
            return f"前日終値：{int(hist['Close'].iloc[-1]):,}円"
    except Exception as e:
        log.warning(f"前日終値取得エラー [{ticker}]: {e}")
    return "株価取得中"


def save_today_pick(report_text: str) -> None:
    """Claude が選んだ今日の激推し株をpick.jsonに保存する"""
    # data-ticker と data-name を順不同・間に他属性があっても拾えるように個別抽出
    m_ticker = re.search(r'data-ticker=["\']([^"\']+)["\']', report_text)
    m_name   = re.search(r'data-name=["\']([^"\']+)["\']', report_text)
    if not m_ticker or not m_name:
        log.warning("今日の激推し株データが抽出できませんでした")
        return
    ticker = m_ticker.group(1)
    name   = m_name.group(1)
    code = ticker.replace(".T", "")
    data = {"date": datetime.now().strftime("%Y-%m-%d"), "ticker": ticker, "name": name, "code": code}
    with open(PICK_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log.info(f"今日の激推し株を保存: {name}（{code}）")


# ─────────────────────────────────────────────
# ⑧ 送信時刻まで待機
# ─────────────────────────────────────────────

def wait_until_send_time():
    """
    8:00 JST になるまで待機する。
    すでに8:00を過ぎていた場合は即座に送信する。
    """
    import time as time_module
    JST = timezone(timedelta(hours=9))
    now = datetime.now(JST)
    target = now.replace(hour=8, minute=0, second=0, microsecond=0)
    if now >= target:
        log.info("送信時刻（8:00 JST）を過ぎているため即座に送信します")
        return
    wait_seconds = (target - now).total_seconds()
    log.info(f"8:00 JSTまで待機中... あと {int(wait_seconds // 60)}分{int(wait_seconds % 60)}秒")
    time_module.sleep(wait_seconds)
    log.info("送信時刻になりました。送信を開始します。")


# ─────────────────────────────────────────────
# メイン処理
# ─────────────────────────────────────────────

def main():
    """
    メイン処理。
    1. 設定ファイル読み込み
    2. RSSニュース収集
    3. YouTube動画収集
    4. Claude APIで統合分析
    5. HTMLメール生成
    6. Gmail送信
    """
    # コマンドライン引数チェック
    test_mode = "--test" in sys.argv

    print("=" * 50)
    print("📊 まいにち日本株短信 起動")
    print(f"   実行時刻: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   モード: {'テスト' if test_mode else '本番'}")
    print("=" * 50)

    # 設定ファイル読み込み
    json_config = {}
    config_path = BASE_DIR / "config.json"
    if config_path.exists():
        json_config = load_json(config_path)
    # docs/.env（中央管理）を優先して読み込み、なければプロジェクト内 .env を使用
    central_env_path = BASE_DIR.parent.parent / "docs" / ".env"
    env_config = load_env(central_env_path) if central_env_path.exists() else load_env(BASE_DIR / ".env")
    sources = load_json(BASE_DIR / "sources.json")

    config = {
        "anthropic": {
            "api_key": env_config.get("ANTHROPIC_API_KEY", json_config.get("anthropic", {}).get("api_key", "")),
            "model": env_config.get("ANTHROPIC_MODEL", json_config.get("anthropic", {}).get("model", "claude-sonnet-4-6")),
            "max_tokens": parse_int(env_config.get("ANTHROPIC_MAX_TOKENS"), json_config.get("anthropic", {}).get("max_tokens", 8000)),
            "memo": env_config.get("ANTHROPIC_MEMO", json_config.get("anthropic", {}).get("memo", "")),
        },
        "youtube": {
            "api_key": env_config.get("YOUTUBE_API_KEY", json_config.get("youtube", {}).get("api_key", "")),
            "max_results_per_channel": parse_int(env_config.get("YOUTUBE_MAX_RESULTS_PER_CHANNEL"), json_config.get("youtube", {}).get("max_results_per_channel", 3)),
            "published_within_hours": parse_int(env_config.get("YOUTUBE_PUBLISHED_WITHIN_HOURS"), json_config.get("youtube", {}).get("published_within_hours", 24)),
            "memo": env_config.get("YOUTUBE_MEMO", json_config.get("youtube", {}).get("memo", "")),
        },
        "gmail": {
            "sender_email": env_config.get("GMAIL_SENDER_EMAIL", json_config.get("gmail", {}).get("sender_email", "")),
            "sender_password": env_config.get("GMAIL_SENDER_PASSWORD", json_config.get("gmail", {}).get("sender_password", "")),
            "recipient_emails": parse_list(env_config.get("GMAIL_RECIPIENT_EMAILS", json_config.get("gmail", {}).get("recipient_emails", []))),
            "memo": env_config.get("GMAIL_MEMO", json_config.get("gmail", {}).get("memo", "")),
        },
        "system": {
            "max_articles_per_source": parse_int(env_config.get("SYSTEM_MAX_ARTICLES_PER_SOURCE"), json_config.get("system", {}).get("max_articles_per_source", 5)),
            "article_preview_chars": parse_int(env_config.get("SYSTEM_ARTICLE_PREVIEW_CHARS"), json_config.get("system", {}).get("article_preview_chars", 300)),
            "youtube_transcript_chars": parse_int(env_config.get("SYSTEM_YOUTUBE_TRANSCRIPT_CHARS"), json_config.get("system", {}).get("youtube_transcript_chars", 3000)),
            "log_file": env_config.get("SYSTEM_LOG_FILE", json_config.get("system", {}).get("log_file", "logs/system.log")),
            "data_dir": env_config.get("SYSTEM_DATA_DIR", json_config.get("system", {}).get("data_dir", "data")),
            "test_mode": parse_bool(env_config.get("SYSTEM_TEST_MODE", json_config.get("system", {}).get("test_mode", False))),
            "memo": env_config.get("SYSTEM_MEMO", json_config.get("system", {}).get("memo", "")),
        },
        "report": {
            "send_hour": parse_int(env_config.get("REPORT_SEND_HOUR"), json_config.get("report", {}).get("send_hour", 7)),
            "send_minute": parse_int(env_config.get("REPORT_SEND_MINUTE"), json_config.get("report", {}).get("send_minute", 0)),
            "language": env_config.get("REPORT_LANGUAGE", json_config.get("report", {}).get("language", "日本語")),
            "investment_focus": env_config.get("REPORT_INVESTMENT_FOCUS", json_config.get("report", {}).get("investment_focus", "日本株")),
            "memo": env_config.get("REPORT_MEMO", json_config.get("report", {}).get("memo", "")),
        },
        "line": {
            "channel_access_token": env_config.get("LINE_CHANNEL_ACCESS_TOKEN", ""),
            "user_ids": parse_list(env_config.get("LINE_USER_IDS", "")),
        },
    }

    sys_cfg = config["system"]
    yt_cfg = config["youtube"]
    all_articles = []
    all_videos = []

    # ── RSSニュース収集 ──
    log.info("【ステップ1】RSSニュース収集開始")
    for cat_key, category in sources["categories"].items():
        if not category.get("enabled", False):
            continue
        log.info(f"  カテゴリ: {category['name']}")
        for feed in category.get("rss_feeds", []):
            if not feed.get("enabled", True):
                continue
            articles = fetch_rss_articles(
                feed,
                max_articles=sys_cfg["max_articles_per_source"],
                preview_chars=sys_cfg["article_preview_chars"],
            )
            all_articles.extend(articles)

    log.info(f"  → 合計 {len(all_articles)} 件の記事を収集")

    # ── YouTube動画収集 ──
    log.info("【ステップ2】YouTube動画収集開始")
    if yt_cfg["api_key"] and yt_cfg["api_key"] != "ここにYouTube Data APIキーを貼り付けてください":
        for cat_key, category in sources["categories"].items():
            if not category.get("enabled", False):
                continue
            for channel in category.get("youtube_channels", []):
                if not channel.get("enabled", True):
                    continue
                videos = fetch_youtube_videos(
                    channel,
                    api_key=yt_cfg["api_key"],
                    max_results=yt_cfg["max_results_per_channel"],
                    hours=yt_cfg["published_within_hours"],
                )
                all_videos.extend(videos)
        log.info(f"  → 合計 {len(all_videos)} 件の動画を収集")
    else:
        log.info("  → YouTube APIキー未設定のためスキップ（設定後に有効化できます）")

    # ── コンテンツがゼロの場合はスキップ ──
    if not all_articles and not all_videos:
        log.warning("本日の新着コンテンツが0件です。メール送信をスキップします。")
        print("\n⚠️  本日の新着コンテンツが0件でした。ソース設定を確認してください。")
        return

    # ── リアルタイム市場データ取得 ──
    log.info("【ステップ3】リアルタイム市場データ取得")
    market_data = fetch_market_data()

    # ── 昨日の激推し株データ読み込み ──
    log.info("【ステップ4準備】昨日の激推し株データを確認")
    yesterday_pick   = load_yesterday_pick()
    yesterday_result = fetch_pick_result(yesterday_pick) if yesterday_pick else None

    if yesterday_result:
        sign  = "+" if yesterday_result["profit"] >= 0 else ""
        emoji = "📈" if yesterday_result["profit"] >= 0 else "📉"
        pick_context = f"""
昨日の振り返りデータ（以下を⑦セクションに必ず含めること）：
昨日の激推し株：{yesterday_result['name']}（{yesterday_result['code']}）
寄り値：{yesterday_result['open']:,}円 → 引き値：{yesterday_result['close']:,}円
100株損益：{sign}{yesterday_result['profit']:,}円 {emoji}

表示フォーマット：
「昨日100株買ってたら、、？」という見出しの後に
{yesterday_result['name']}（{yesterday_result['code']}）　{yesterday_result['open']:,}円 → {yesterday_result['close']:,}円
±○○○円の部分は font-size:32px・font-weight:bold・text-align:center で大きく表示すること。"""
    else:
        pick_context = "昨日の振り返り：昨日の激推し株データなし（初回のため省略）。"

    # ── Claude APIで統合分析 ──
    log.info("【ステップ4】Claude APIで統合分析開始")
    prompt      = build_prompt(all_articles, all_videos, market_data, pick_context)
    report_text = generate_report_with_claude(prompt, config)

    # ── 今日の激推し株を保存・前日終値を差し替え ──
    save_today_pick(report_text)
    # 抽出したtickerで前日終値を取得し「株価取得中」を差し替える
    m_ticker = re.search(r'data-ticker="([^"]+)"', report_text)
    if m_ticker:
        prev_close_str = fetch_previous_close(m_ticker.group(1))
        report_text = report_text.replace("株価取得中", prev_close_str)

    # ── HTMLメール生成 ──
    log.info("【ステップ5】HTMLメール生成")
    html = build_html_email(report_text, all_articles, all_videos)

    # ── アーカイブ保存（docs/archive/YYYYMMDD.html）──
    archive_dir = BASE_DIR / "docs" / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"{datetime.now().strftime('%Y%m%d')}.html"
    with open(archive_path, "w", encoding="utf-8") as f:
        f.write(html)
    log.info(f"アーカイブ保存: {archive_path}")

    # ── 8:00 JST まで待機 ──
    if not (test_mode or sys_cfg.get("test_mode", False)):
        log.info("【ステップ6準備】送信時刻（8:00 JST）まで待機")
        wait_until_send_time()

    # ── メール送信 ──
    log.info("【ステップ6】メール送信")
    send_gmail(html, config, test_mode=test_mode or sys_cfg.get("test_mode", False))

    # ── LINE送信 ──
    log.info("【ステップ7】LINE送信")
    try:
        send_line(report_text, config, test_mode=test_mode or sys_cfg.get("test_mode", False))
    except Exception as e:
        log.error(f"LINE送信に失敗しましたが処理を続行します: {e}")

    print("\n✅ すべての処理が完了しました！\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n⛔ ユーザーによって中断されました")
    except Exception as e:
        log.error(f"予期しないエラーが発生しました: {e}")
        log.error(traceback.format_exc())
        print(f"\n❌ エラーが発生しました。logs/system.log を確認してください。\n  エラー内容: {e}")
        sys.exit(1)
