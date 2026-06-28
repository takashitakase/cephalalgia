#!/usr/bin/env python3
"""
PubMed 頭痛論文デイリートラッカー

使い方:
    python tracker.py                  # 昨日の新着論文をチェック（reports/ に保存）
    python tracker.py --days 7         # 過去7日分をチェック
    python tracker.py --all            # 既読済みも含めて全件表示
    python tracker.py --stdout         # レポートをコンソールにも出力
    python tracker.py --notion         # Notion データベースにも投稿

環境変数:
    NCBI_API_KEY      NCBI E-utilities APIキー（省略可。設定すると 10 req/s まで可）
    NCBI_EMAIL        NCBI に通知するメールアドレス（省略可）
    NOTION_TOKEN      Notion インテグレーショントークン（--notion 使用時に必須）
    NOTION_DATABASE   Notion データベース ID（省略時はデフォルトDBを使用）

cron 設定例（毎朝6時に実行）:
    0 6 * * * cd /path/to/cephalalgia && python tracker.py --notion >> cron.log 2>&1
"""

import json
import os
import sys
import argparse
import time
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
from datetime import date, timedelta, datetime
from pathlib import Path

# ── 設定 ──────────────────────────────────────────────────────────────────────

SEARCH_QUERY = (
    "headache[MeSH Terms] OR "
    "migraine disorders[MeSH Terms] OR "
    "cluster headache[MeSH Terms] OR "
    "tension-type headache[MeSH Terms]"
)

# Notion データベース ID（既存: 頭痛関連 PubMed 新着論文）
DEFAULT_NOTION_DATABASE = "1a5ec534-b29e-410e-b7d9-38fcfa888671"

SEEN_FILE  = Path("seen_pmids.json")
REPORT_DIR = Path("reports")
NCBI_BASE  = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
NOTION_API = "https://api.notion.com/v1"

# ── ストレージ ─────────────────────────────────────────────────────────────────

def load_seen() -> set[str]:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
    return set()


def save_seen(pmids: set[str]) -> None:
    SEEN_FILE.write_text(
        json.dumps(sorted(pmids), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

# ── HTTP ユーティリティ ────────────────────────────────────────────────────────

def _http(url: str, *, method: str = "GET", data: bytes | None = None,
          headers: dict | None = None) -> bytes:
    """HTTP リクエスト（リトライ付き）。"""
    # urllib は HTTP ヘッダー値を latin-1 でエンコードするため、
    # ASCII 範囲外の文字を含むヘッダー値はエンコードエラーになる。
    # Request を構築する際はヘッダーを後から add_unredirected_header で追加し、
    # バイト列として渡すことで回避する。
    req = urllib.request.Request(url, data=data, method=method)
    all_headers = {
        "User-Agent": "cephalalgia-tracker/1.0 (Python urllib)",
        **(headers or {}),
    }
    for k, v in all_headers.items():
        # ヘッダー値を ASCII に限定（非 ASCII 文字は除去）
        safe_v = v.encode("ascii", errors="ignore").decode("ascii") if isinstance(v, str) else v
        req.add_unredirected_header(k, safe_v)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            # HTTPError は URLError のサブクラス。レスポンス本文に
            # Notion/NCBI からの具体的なエラー理由が入っているので読み取る。
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            detail = f"HTTP Error {exc.code}: {body[:600]}"
            # 4xx はリクエスト自体の誤りでリトライしても無駄。即座に詳細付きで投げる。
            if 400 <= exc.code < 500 or attempt == 3:
                raise RuntimeError(detail) from exc
            wait = 2 ** attempt
            print(f"  [警告] サーバエラー (HTTP {exc.code})、{wait}s 後リトライ...", file=sys.stderr)
            time.sleep(wait)
        except urllib.error.URLError as exc:
            if attempt == 3:
                raise
            wait = 2 ** attempt
            print(f"  [警告] ネットワークエラー ({exc})、{wait}s 後リトライ...", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError("unreachable")

# ── NCBI API ───────────────────────────────────────────────────────────────────

def _ncbi_params() -> dict:
    params: dict = {"tool": "cephalalgia-tracker"}
    if key := os.environ.get("NCBI_API_KEY"):
        params["api_key"] = key
    if email := os.environ.get("NCBI_EMAIL"):
        params["email"] = email
    return params


def _ncbi_sleep() -> None:
    time.sleep(0.11 if os.environ.get("NCBI_API_KEY") else 0.34)


def search_pubmed(query: str, date_from: str, date_to: str, max_results: int) -> list[str]:
    params = {
        **_ncbi_params(),
        "db": "pubmed", "term": query,
        # edat = Entrez date（PubMed に収載された日）。
        # pdat（出版日）は収載とのタイムラグが大きく、直近数日の窓では
        # 新着論文をほとんど取りこぼすため、収載日ベースで検索する。
        "datetype": "edat", "mindate": date_from, "maxdate": date_to,
        "retmax": max_results, "retmode": "json",
    }
    url  = NCBI_BASE + "esearch.fcgi?" + urllib.parse.urlencode(params)
    data = json.loads(_http(url))
    return data["esearchresult"].get("idlist", [])


def fetch_summaries(pmids: list[str]) -> list[dict]:
    """タイトル・著者・雑誌・DOI などを esummary から取得する。"""
    if not pmids:
        return []
    _ncbi_sleep()
    params = {**_ncbi_params(), "db": "pubmed", "id": ",".join(pmids), "rettype": "docsum"}
    url  = NCBI_BASE + "esummary.fcgi?" + urllib.parse.urlencode(params)
    root = ET.fromstring(_http(url))

    articles: list[dict] = []
    for doc in root.findall(".//DocSum"):
        pmid = doc.findtext("Id") or ""
        info: dict = {"pmid": pmid, "authors": []}
        for item in doc.findall("Item"):
            name = item.attrib.get("Name", "")
            if   name == "Title":      info["title"]    = item.text or ""
            elif name == "Source":     info["journal"]  = item.text or ""
            elif name == "PubDate":    info["pub_date"] = item.text or ""
            elif name == "DOI":        info["doi"]      = item.text or ""
            elif name == "AuthorList":
                info["authors"] = [a.text for a in item.findall("Item") if a.text]
        articles.append(info)
    return articles


def fetch_abstracts(pmids: list[str]) -> dict[str, str]:
    """PMID → アブストラクト本文の辞書を efetch から取得する。"""
    if not pmids:
        return {}
    _ncbi_sleep()
    params = {
        **_ncbi_params(),
        "db": "pubmed", "id": ",".join(pmids),
        "rettype": "abstract", "retmode": "xml",
    }
    url  = NCBI_BASE + "efetch.fcgi?" + urllib.parse.urlencode(params)
    root = ET.fromstring(_http(url))

    result: dict[str, str] = {}
    for article in root.findall(".//PubmedArticle"):
        pmid_el = article.find(".//PMID")
        if pmid_el is None:
            continue
        pmid = pmid_el.text or ""
        parts = [
            el.text for el in article.findall(".//AbstractText") if el.text
        ]
        result[pmid] = " ".join(parts)
    return result


def _parse_pubmed_date(raw: str) -> str | None:
    """PubMed の日付文字列を ISO-8601 (YYYY-MM-DD) に変換する。"""
    raw = raw.strip()
    for fmt in ("%Y %b %d", "%Y %b", "%Y/%m/%d", "%Y/%m", "%Y"):
        try:
            dt = datetime.strptime(raw, fmt)
            if fmt in ("%Y %b", "%Y/%m"):
                return dt.strftime("%Y-%m")
            if fmt == "%Y":
                return dt.strftime("%Y")
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None

# ── Notion API ────────────────────────────────────────────────────────────────

def _notion_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }


def notion_page_exists(pmid: str, token: str, database_id: str) -> bool:
    """同じ PMID のページが既に Notion DB に存在するか確認する。"""
    payload = json.dumps({
        "filter": {
            "property": "PMID",
            "rich_text": {"equals": pmid},
        }
    }).encode()
    url = f"{NOTION_API}/databases/{database_id}/query"
    resp = json.loads(_http(url, method="POST", data=payload, headers=_notion_headers(token)))
    return len(resp.get("results", [])) > 0


def notion_post_article(article: dict, today: date, token: str, database_id: str) -> str:
    """論文1件を Notion データベースに投稿し、作成されたページURLを返す。"""
    pmid      = article.get("pmid", "")
    title     = article.get("title", "") or "(no title)"
    journal   = article.get("journal", "") or ""
    doi       = article.get("doi", "") or ""
    abstract  = article.get("abstract", "") or ""
    authors   = article.get("authors", [])
    pub_date_raw = article.get("pub_date", "") or ""

    author_str = ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else "")
    pub_date_iso = _parse_pubmed_date(pub_date_raw)

    properties: dict = {
        "タイトル（英語）": {
            "title": [{"text": {"content": title[:2000]}}]
        },
        "PMID": {
            "rich_text": [{"text": {"content": pmid}}]
        },
        "雑誌名": {
            "rich_text": [{"text": {"content": journal[:2000]}}]
        },
        "著者": {
            "rich_text": [{"text": {"content": author_str[:2000]}}]
        },
        "検索日": {
            "date": {"start": today.isoformat()}
        },
    }

    if doi:
        properties["DOI"] = {"url": f"https://doi.org/{doi}"}

    if pub_date_iso:
        # Notion の日付プロパティは YYYY-MM-DD 形式のみ受け付ける。
        # PubMed は "2026" や "2026-06" のような月/年精度を返すことがあるため、
        # 不足分を月初・年初で補完して有効な ISO 日付にする。
        if len(pub_date_iso) == 4:        # YYYY
            pub_date_iso += "-01-01"
        elif len(pub_date_iso) == 7:      # YYYY-MM
            pub_date_iso += "-01"
        properties["出版日"] = {"date": {"start": pub_date_iso}}

    if abstract:
        properties["要旨（日本語）"] = {
            "rich_text": [{"text": {"content": abstract[:2000]}}]
        }

    payload = json.dumps({
        "parent": {"database_id": database_id},
        "properties": properties,
    }).encode()

    url  = f"{NOTION_API}/pages"
    resp = json.loads(_http(url, method="POST", data=payload, headers=_notion_headers(token)))
    return resp.get("url", "")

# ── Markdown レポート生成 ──────────────────────────────────────────────────────

def build_report(articles: list[dict], search_date: str, days: int) -> str:
    period = f"過去 {days} 日間" if days > 1 else "昨日"
    lines = [
        "# 頭痛関連 PubMed 新着論文レポート",
        "",
        f"**検索日**: {search_date}　　"
        f"**対象期間**: {period}　　"
        f"**新着件数**: {len(articles)} 件",
        "",
        "> 検索対象: headache / migraine disorders / cluster headache / tension-type headache (MeSH Terms)",
        "",
        "---",
        "",
    ]

    for i, a in enumerate(articles, 1):
        pmid     = a.get("pmid", "")
        title    = a.get("title", "（タイトル不明）")
        journal  = a.get("journal", "（雑誌不明）")
        pub_date = a.get("pub_date", "")
        doi      = a.get("doi", "")
        authors  = a.get("authors", [])
        author_str = ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else "")

        lines += [
            f"## {i}. {title}",
            "",
            f"- **著者**: {author_str or '（著者不明）'}",
            f"- **雑誌**: {journal}　（{pub_date}）",
            f"- **PMID**: [{pmid}](https://pubmed.ncbi.nlm.nih.gov/{pmid}/)",
        ]
        if doi:
            lines.append(f"- **DOI**: [https://doi.org/{doi}](https://doi.org/{doi})")
        lines.append("")

    return "\n".join(lines)

# ── メイン ────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="PubMed 頭痛論文デイリートラッカー",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--days",   type=int, default=1,
                        help="何日前まで遡るか（デフォルト: 1）")
    parser.add_argument("--max",    type=int, default=100,
                        help="最大取得件数（デフォルト: 100）")
    parser.add_argument("--all",    action="store_true",
                        help="既読済みを含め全件表示")
    parser.add_argument("--stdout", action="store_true",
                        help="レポートをコンソールにも出力する")
    parser.add_argument("--notion", action="store_true",
                        help="Notion データベースに投稿する")
    args = parser.parse_args()

    # Notion モードの事前チェック
    notion_token = os.environ.get("NOTION_TOKEN")
    notion_db    = os.environ.get("NOTION_DATABASE", DEFAULT_NOTION_DATABASE)
    if args.notion and not notion_token:
        print("[エラー] --notion を使うには環境変数 NOTION_TOKEN を設定してください。", file=sys.stderr)
        return 1

    today = date.today()
    since = today - timedelta(days=args.days)

    print(
        f"[{today}] 検索期間: {since} 〜 {today}  "
        f"（{'APIキーあり' if os.environ.get('NCBI_API_KEY') else 'APIキーなし'}）"
    )

    try:
        pmids = search_pubmed(
            SEARCH_QUERY,
            since.strftime("%Y/%m/%d"),
            today.strftime("%Y/%m/%d"),
            args.max,
        )
    except Exception as exc:
        print(f"[エラー] PubMed 検索失敗: {exc}", file=sys.stderr)
        return 1

    seen         = load_seen()
    target_pmids = pmids if args.all else [p for p in pmids if p not in seen]
    print(f"取得: {len(pmids)} 件　新着: {len(target_pmids)} 件")

    if not target_pmids:
        print("新着論文はありません。")
        seen.update(pmids)
        save_seen(seen)
        return 0

    try:
        summaries = fetch_summaries(target_pmids)
    except Exception as exc:
        print(f"[エラー] 書誌情報取得失敗: {exc}", file=sys.stderr)
        return 1

    # --notion のときだけアブストラクトを取得
    if args.notion:
        try:
            abstracts = fetch_abstracts(target_pmids)
            for a in summaries:
                a["abstract"] = abstracts.get(a["pmid"], "")
        except Exception as exc:
            print(f"[警告] アブストラクト取得失敗（スキップ）: {exc}", file=sys.stderr)

    # Markdown レポートをファイルに保存
    report = build_report(summaries, today.isoformat(), args.days)
    REPORT_DIR.mkdir(exist_ok=True)
    report_file = REPORT_DIR / f"{today.isoformat()}.md"
    report_file.write_text(report, encoding="utf-8")
    print(f"レポート保存: {report_file}")

    if args.stdout:
        print()
        print(report)

    # Notion 投稿
    if args.notion:
        print(f"\nNotion に投稿中（DB: {notion_db}）...")
        ok = err = 0
        for a in summaries:
            pmid = a.get("pmid", "")
            try:
                if not args.all and notion_page_exists(pmid, notion_token, notion_db):
                    print(f"  [スキップ] {pmid} は既に Notion に存在します")
                    continue
                page_url = notion_post_article(a, today, notion_token, notion_db)
                print(f"  [投稿済] [{pmid}] {a.get('title', '')[:60]}…")
                print(f"           {page_url}")
                ok += 1
                time.sleep(0.34)  # Notion API レートリミット
            except Exception as exc:
                print(f"  [エラー] {pmid} の投稿失敗: {exc}", file=sys.stderr)
                err += 1
        print(f"\nNotion 投稿完了: 成功 {ok} 件 / 失敗 {err} 件")

    if not args.stdout and not args.notion:
        print()
        for a in summaries:
            pmid  = a.get("pmid", "")
            title = a.get("title", "（不明）")
            trunc = title[:75] + "…" if len(title) > 75 else title
            print(f"  [{pmid}] {trunc}")

    # 今回検索した PMID を既読として記録
    seen.update(pmids)
    save_seen(seen)

    return 0


if __name__ == "__main__":
    sys.exit(main())
