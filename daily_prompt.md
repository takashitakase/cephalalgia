# 頭痛 PubMed 日次チェック

以下の手順で頭痛関連 PubMed 新着論文の日次チェックを実行してください。

## 手順

1. `/home/user/cephalalgia/seen_pmids.json` を読み込み、既読 PMID のリストを取得する。

2. PubMed MCP ツールで下記クエリを使い、昨日から今日までの論文を最大100件検索する：
   ```
   headache[MeSH Terms] OR migraine disorders[MeSH Terms] OR cluster headache[MeSH Terms] OR tension-type headache[MeSH Terms]
   ```

3. 取得した PMID のうち、seen_pmids.json に含まれていないものだけを「新着」とする。

4. 新着論文について `get_article_metadata` でタイトル・著者・雑誌・DOI・アブストラクトを取得する。

5. 新着論文を1件ずつ Notion の「頭痛関連 PubMed 新着論文」データベース（data_source_id: e39642ec-ba2f-42bb-a8cd-39a7e7364afd）に投稿する。
   プロパティのマッピング：
   - タイトル（英語）: 論文タイトル
   - PMID: PMID 文字列
   - DOI: DOI URL（https://doi.org/xxx 形式）
   - 雑誌名: ジャーナル名
   - 著者: 第1〜3著者 + "et al."
   - 出版日: 出版日（ISO-8601）
   - 検索日: 今日の日付
   - 要旨（日本語）: アブストラクト本文

6. 今回取得した全 PMID（新着・既読問わず）を seen_pmids.json に追記して保存する。
   ファイル形式: ソート済みの JSON 配列。

7. 完了後、投稿件数をコンソールに出力する。
