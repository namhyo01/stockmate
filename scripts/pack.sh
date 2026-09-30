#!/usr/bin/env bash
# 다른 컴퓨터(AWS 등)로 옮길 패키지를 만든다.
# 코드(git 기록 포함) + data/(DB, 기억, 리포트) + .env + Claude Code 메모리와 대화 원본(handoff/)
#
# 사용법: bash scripts/pack.sh [출력 파일]
# ⚠️ .env(한투 키 등)가 들어 있으니 scp로만 옮기고, 옮긴 뒤에는 로컬 사본을 지운다.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$(dirname "$ROOT")/stockmate-handoff-$(date +%Y%m%d-%H%M).tar.gz}"
CLAUDE_PROJECT="$HOME/.claude/projects/$(echo "$ROOT" | sed 's#/#-#g')"

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/handoff"

if [ -d "$CLAUDE_PROJECT/memory" ]; then
  cp -r "$CLAUDE_PROJECT/memory" "$STAGE/handoff/claude-memory"
fi
latest_session="$(ls -t "$CLAUDE_PROJECT"/*.jsonl 2>/dev/null | head -1 || true)"
if [ -n "$latest_session" ]; then
  cp "$latest_session" "$STAGE/handoff/session.jsonl"
fi

tar -czf "$OUT" \
  --exclude=./.venv --exclude='__pycache__' --exclude=./.pytest_cache \
  --exclude=./.playwright-mcp --exclude=./data/kis_token.json \
  -C "$ROOT" . \
  -C "$STAGE" handoff
chmod 600 "$OUT"

echo "📦 $OUT ($(du -h "$OUT" | cut -f1))"
echo "   포함: 코드와 git 기록, data/, .env, handoff/(Claude 메모리, 대화 원본)"
echo "   옮기기: scp -i <키.pem> \"$OUT\" ubuntu@<서버 IP>:~/"
