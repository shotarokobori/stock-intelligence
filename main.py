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
from googleapiclient.discovery import build
from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound, TranscriptsDisabled

# ─────────────────────────────────────────────
# 初期設定
# ─────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
LOG_DIR  = BASE_DIR / "logs"
DATA_DIR = BASE_DIR / "data"
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
# ① RSSニュース収集
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

def build_prompt(articles: list[dict], videos: list[dict]) -> str:
    """
    Claude に渡すプロンプトを組み立てる。
    日本株運用で勝つことを目的とした統合分析レポートを要求する。
    """
    today = datetime.now().strftime("%Y年%m月%d日（%A）")

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
※スマホ優先デザイン：固定px幅は使わずmax-widthと%を使うこと。文字サイズは本文14px。背景は白または明るい色ベース。余白は指タップしやすい大きさに。長い文章は適切な位置で改行し、読みやすい行長（1行25〜35文字程度）を心がけること。
※全体の目標トークン数：6000以内。最大8000以内。余計な装飾・繰り返しは省くこと。

【取捨選択の指示】
収集した全記事・動画の中から、日本株への影響度が高いものだけを選んで分析すること。
重要度が低い・関連性が薄いと判断した記事は完全に無視してよい。
その日のニュース状況をもとに毎回動的に判断すること。

【①　1行サマリー（3〜7件）】
選んだ記事を日本株影響度が高い順に1行で要約。
番号は②の番号と完全に一致させること。

【②　要点と解説】
①で選んだ各ニュースについて：
- 何が起きたか（1〜2行）
- なぜ重要か・背景（2〜3行）
- 専門用語は「※〇〇とは：〜」で必ず注釈
- 日本株への影響（2〜3行）

【③　横断・統合考察（丁寧に）】
複数ソースを横断して見えてくる今日のテーマ・流れを丁寧に分析すること。
因果関係・リスク・見落とされがちな視点を含め、500字程度でしっかり書くこと。

【④　今後1〜2週間の注目ポイント】
箇条書き3〜4項目のみ。日付・イベント・影響を簡潔に。

【⑤　日本株・個別株への見通し（必須・丁寧に）】
このセクションは絶対に省略せず、最も丁寧に書くこと。
- 日経平均の方向性（↑↓→）と根拠（3〜4行）
- 注目セクター：3〜5業種（方向・理由を各1〜2行）
- 注目個別銘柄：必ず3〜5銘柄（銘柄名・証券コード・注目理由・リスクを各2行）
- 今日の総合的な投資スタンス（2〜3行）
※投資は自己責任である旨を添える

①〜⑤を必ずすべて完結させること。⑤は絶対に省略しないこと。"""

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

    # ソース一覧を生成
    sources_html = ""
    for a in articles:
        sources_html += f'<li><a href="{a["url"]}" style="color:#1a73e8;">{a["source"]}：{a["title"]}</a></li>'
    for v in videos:
        sources_html += f'<li><a href="{v["url"]}" style="color:#c00;">[YouTube] {v["source"]}：{v["title"]}</a></li>'

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>日本株インテリジェンス｜{today}</title>
<style>
  /* ── スマホ対応 ── */
  body {{ margin:0; padding:0; background:#f4f4f4; }}
  .outer {{ width:100%; background:#f4f4f4; padding:12px 0; }}
  .inner {{ width:100%; max-width:680px; margin:0 auto; background:#ffffff;
            border-radius:8px; overflow:hidden;
            box-shadow:0 2px 8px rgba(0,0,0,0.1); }}
  .header {{ background:linear-gradient(135deg,#1a237e,#283593); padding:20px 16px; }}
  .header h1 {{ margin:6px 0 4px; color:#fff; font-size:20px; font-weight:700; }}
  .header p  {{ margin:0; color:#bbdefb; font-size:13px; }}
  .header .label {{ color:#90caf9; font-size:11px; letter-spacing:2px; margin:0; }}
  .body  {{ padding:16px; font-size:15px; line-height:1.8; color:#333; }}
  .sources {{ padding:0 16px 16px; }}
  .sources-inner {{ background:#f8f9fa; border-radius:6px; padding:14px 16px; }}
  .sources-inner p  {{ margin:0 0 8px; font-size:13px; font-weight:700; color:#555; }}
  .sources-inner ul {{ margin:0; padding-left:16px; font-size:13px; color:#555; line-height:2.0; }}
  .footer {{ background:#f4f4f4; padding:14px 16px; border-top:1px solid #e0e0e0; }}
  .footer p {{ margin:0; font-size:11px; color:#999; line-height:1.6; }}
  /* PC表示では少し余白を増やす */
  @media (min-width:480px) {{
    .header {{ padding:28px 32px; }}
    .header h1 {{ font-size:22px; }}
    .body  {{ padding:32px; }}
    .sources {{ padding:0 32px 24px; }}
    .footer {{ padding:16px 32px; }}
  }}
</style>
</head>
<body>
<div class="outer">
  <div class="inner">

    <!-- ヘッダー -->
    <div class="header">
      <p class="label">DAILY INTELLIGENCE REPORT</p>
      <h1>📊 日本株インテリジェンス</h1>
      <p>{today}　|　日本株運用サポート</p>
    </div>

    <!-- 本文 -->
    <div class="body">
      {report_html}
    </div>

    <!-- ソース一覧 -->
    <div class="sources">
      <div class="sources-inner">
        <p>■ 本日の参照ソース</p>
        <ul>{sources_html}</ul>
      </div>
    </div>

    <!-- フッター -->
    <div class="footer">
      <p>このメールは自動生成されたAI分析レポートです。投資判断はご自身の責任で行ってください。<br>
      システム設定変更: sources.json / config.json を編集してください。</p>
    </div>

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
    subject = f"📊 日本株インテリジェンス｜{today}"

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

def send_line(report_text: str, config: dict, test_mode: bool = False) -> None:
    """
    LINE Messaging API でレポートを送信する。
    複数ユーザーへのマルチキャスト送信に対応。
    """
    line_cfg = config["line"]
    token    = line_cfg.get("channel_access_token", "")

    if not token:
        log.warning("LINEチャンネルアクセストークンが未設定です。送信をスキップします。")
        return

    # HTMLタグを除去してプレーンテキストに変換
    plain_text = re.sub(r'<[^>]+>', '', report_text)
    plain_text = re.sub(r'\n{3,}', '\n\n', plain_text).strip()

    # LINEの1メッセージ上限（5000文字）に合わせてカット
    today = datetime.now().strftime("%Y年%m月%d日")
    header = f"📊 日本株インテリジェンス {today}\n\n"
    limit = 4900 - len(header)
    if len(plain_text) > limit:
        plain_text = plain_text[:limit] + "\n\n…（全文は省略されました）"
    message = header + plain_text

    if test_mode:
        log.info("[テストモード] LINE送信スキップ")
        print("\n✅ [テストモード] LINE送信スキップ\n")
        return

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messages": [{"type": "text", "text": message}],
    }
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
    print("📊 日本株インテリジェンス 起動")
    print(f"   実行時刻: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   モード: {'テスト' if test_mode else '本番'}")
    print("=" * 50)

    # 設定ファイル読み込み
    json_config = {}
    config_path = BASE_DIR / "config.json"
    if config_path.exists():
        json_config = load_json(config_path)
    env_config = load_env(BASE_DIR / ".env")
    sources = load_json(BASE_DIR / "sources.json")

    config = {
        "anthropic": {
            "api_key": env_config.get("ANTHROPIC_API_KEY", json_config.get("anthropic", {}).get("api_key", "")),
            "model": env_config.get("ANTHROPIC_MODEL", json_config.get("anthropic", {}).get("model", "claude-opus-4-5")),
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

    # ── Claude APIで統合分析 ──
    log.info("【ステップ3】Claude APIで統合分析開始")
    prompt      = build_prompt(all_articles, all_videos)
    report_text = generate_report_with_claude(prompt, config)

    # ── LINE送信 ──
    log.info("【ステップ5】LINE送信")
    send_line(report_text, config, test_mode=test_mode or sys_cfg.get("test_mode", False))

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
