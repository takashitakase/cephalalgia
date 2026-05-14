#!/usr/bin/env python3
"""
PubMed 頭痛論文デイリートラッカー

使い方:
    python tracker.py                  # 昨日の新着論文をチェック（reports/ に保存）
    python tracker.py --days 7         # 過去7日分をチェック
    python tracker.py --all            # 既読済みも含めて全件表示
    python tracker.py --stdout         # レポートをコンソールにも出力

環境変数:
    NCBI_API_KEY   NCBI E-utilities APIキー（省略可。設定すると 10 req/s まで可）
    NCBI_EMAIL     NCBI に通知するメールアドレス（省略可。設定を推奨）

cron 設定例（毎朝9時に実行）:
    0 9 * * * cd /path/to/cephalalgia && python tracker.py >> cron.log 2>&1
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
from datetime import date, timedelta
from pathlib import Path

# ── 設定 ──────────────────────────────────────────────────────────────────────

SEARCH_QUERY = (
    "headache[MeSH Terms] OR "
    "migraine disorders[MeSH Terms] OR "
    "cluster headache[MeSH Terms] OR "
    "tension-type headache[MeSH Terms]"
)

SEEN_FILE  = Path("seen_pmids.json")
REPORT_DIR = Path("reports")
NCBI_BASE  = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"

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

# ── NCBI API ───────────────────────────────────────────────────────────────────

def _ncbi_common_params() -> dict:
    """NCBI 推奨の共通パラメータ（tool 識別・APIキー）を返す。"""
    params: dict = {"tool": "cephalalgia-tracker", "retmode": "json"}
    api_key = os.environ.get("NCBI_API_KEY")
    email   = os.environ.get("NCBI_EMAIL")
    if api_key:
        params["api_key"] = api_key
    if email:
        params["email"] = email
    return params


def _get(url: str) -> bytes:
    """HTTP GET（リトライ付き）。NCBI 推奨レートは APIキーなし 3 req/s。"""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "cephalalgia-tracker/1.0 (Python urllib)"},
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except urllib.error.URLError as exc:
            if attempt == 3:
                raise
            wait = 2 ** attempt
            print(f"  [警告] ネットワークエラー ({exc})、{wait}s 後リトライ...", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError("unreachable")


def search_pubmed(query: str, date_from: str, date_to: str, max_results: int) -> list[str]:
    """指定期間に一致する PMID リストを返す。"""
    params = {
        **_ncbi_common_params(),
        "db":       "pubmed",
        "term":     query,
        "datetype": "pdat",
        "mindate":  date_from,
        "maxdate":  date_to,
        "retmax":   max_results,
    }
    url  = NCBI_BASE + "esearch.fcgi?" + urllib.parse.urlencode(params)
    data = json.loads(_get(url))
    return data["esearchresult"].get("idlist", [])


def fetch_summaries(pmids: list[str]) -> list[dict]:
    """PMID リストの書誌情報を取得する（XML esummary）。"""
    if not pmids:
        return []

    # NCBI レートリミット対策
    api_key = os.environ.get("NCBI_API_KEY")
    time.sleep(0.11 if api_key else 0.34)

    params = {
        **_ncbi_common_params(),
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "xml",
    }
    del params["retmode"]          # XML モードは retmode なしで指定
    params["rettype"] = "docsum"
    url  = NCBI_BASE + "esummary.fcgi?" + urllib.parse.urlencode(params)
    root = ET.fromstring(_get(url))

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

# ── レポート生成 ───────────────────────────────────────────────────────────────

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
    args = parser.parse_args()

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

    report = build_report(summaries, today.isoformat(), args.days)

    # レポートをファイルに保存
    REPORT_DIR.mkdir(exist_ok=True)
    report_file = REPORT_DIR / f"{today.isoformat()}.md"
    report_file.write_text(report, encoding="utf-8")
    print(f"レポート保存: {report_file}")

    if args.stdout:
        print()
        print(report)
    else:
        # サマリーのみコンソール表示
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
