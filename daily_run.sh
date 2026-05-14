#!/bin/bash
# 頭痛 PubMed 日次チェック — Claude MCP 経由で実行
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$SCRIPT_DIR/cron.log"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 日次チェック開始" | tee -a "$LOG"

cd "$SCRIPT_DIR"

# 環境変数を読み込む（NOTION_TOKEN など）
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    source "$SCRIPT_DIR/.env"
    set +a
fi

# Claude CLI で MCP ツールを使って実行
claude \
    --output-format text \
    -p "$(cat "$SCRIPT_DIR/daily_prompt.md")" \
    2>&1 | tee -a "$LOG"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 日次チェック完了" | tee -a "$LOG"
