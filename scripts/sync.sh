#!/usr/bin/env bash
# 把本地改动同步到 GitHub（一条命令）
#   用法：./scripts/sync.sh "这次改了什么"
#
# 说明：
#   - 若仓库已配置 git http.proxy（git config --get http.proxy 非空），
#     本脚本会忽略环境里的 HTTP(S)_PROXY —— 沙箱/IDE 注入的代理常常连不通 GitHub。
#   - 首次 push 需要授权：先跑 `python scripts/proxy_bridge.py --push`
#     （SOCKS-only 的机器上 GCM 不认 socks5，必须先过这层 HTTP 桥，详见 troubleshooting.md）。
set -euo pipefail

MSG="${1:-chore: sync}"
cd "$(dirname "$0")/.."

if [ ! -d .git ]; then
  echo "当前目录还不是 git 仓库。先执行一次："
  echo "  git init -b main && git remote add origin <仓库地址>"
  exit 1
fi

# 守卫：本机真实登记表含内部文档 ID，绝不能进仓库
if git status --porcelain | grep -q "assets/groups.json"; then
  echo "⚠️  assets/groups.json 出现在待提交列表里 —— 它含内部文档 ID，应被 .gitignore 忽略。已中止。"
  exit 1
fi

# 仓库自己配了代理时，屏蔽环境代理，避免被注入的代理打断
GIT=(git)
if [ -n "$(git config --get http.proxy || true)" ]; then
  GIT=(env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY git)
fi

git add -A
if git diff --cached --quiet; then
  echo "没有改动，无需提交。"
  exit 0
fi

git commit -m "$MSG"
"${GIT[@]}" pull --rebase --autostash origin main || true
"${GIT[@]}" push origin main
echo "✔ 已同步到远端"
