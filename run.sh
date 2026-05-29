#!/usr/bin/env bash
# 启动天枢·质量（web 端）— 本地浏览器访问 http://127.0.0.1:8081/tools
#
# 前置：
#   1) python 3.11+
#   2) 装依赖：  pip install -e ".[api,ui]"
#   3) 装 Claude Code CLI 并登录：  curl -fsSL https://claude.ai/install.sh | bash && claude login

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

# 默认端口 8081；如被占请用环境变量 PORT 覆盖
PORT="${PORT:-8081}"
HOST="${HOST:-127.0.0.1}"

# 优先用项目 venv 的 python；找不到再回落系统 python
if [[ -x ".venv/bin/python" ]]; then
  PYBIN=".venv/bin/python"
else
  PYBIN="$(command -v python3 || command -v python)"
fi

echo "[天枢·质量] python = $PYBIN"
echo "[天枢·质量] 监听 http://$HOST:$PORT"
echo "[天枢·质量] 启动后浏览器访问 http://$HOST:$PORT/tools"

exec "$PYBIN" -m uvicorn apps.api.main:app --host "$HOST" --port "$PORT" --log-level warning
