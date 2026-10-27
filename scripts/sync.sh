#!/usr/bin/env bash
# 把本地改动同步到 GitHub（一条命令）
#   用法：./scripts/sync.sh "这次改了什么"
# 首次 push 会弹 GitHub 授权，登录一次后凭据会缓存在系统凭据管理器里。
set -euo pipefail

MSG="${1:-chore: sync}"
cd "$(dirname "$0")/.."

if [ ! -d .git ]; then
  echo "当前目录还不是 git 仓库。先执行一次："
  echo "  git init -b main && git remote add origin <仓库地址>"
  exit 1
fi

# 上车前确认没有把本机真实配置带上去（assets/groups.json 在 .gitignore 里）
if git status --porcelain --ignored=no | grep -q "assets/groups.json"; then
  echo "⚠️  assets/groups.json 出现在待提交列表里 —— 它含内部文档 ID，应被 .gitignore 忽略。已中止。"
  exit 1
fi

git add -A
if git diff --cached --quiet; then
  echo "没有改动，无需提交。"
  exit 0
fi

git commit -m "$MSG"
git pull --rebase --autostash origin main || true
git push origin main
echo "✔ 已同步到远端"
