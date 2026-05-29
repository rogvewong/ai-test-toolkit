# =====================================================================
# 天枢·裁决 — Docker 镜像
#
# 多阶段:
#   1. base       : python:3.12-slim + 系统依赖 + uv
#   2. browser    : 装 Playwright 浏览器 (Chromium + 系统 lib)
#   3. claude-cli : 装 Anthropic Claude Code CLI (npm)
#   4. runtime    : 合并以上,只带运行所需 — 最终镜像
#
# 启动:
#   docker run -d --name tianshu \
#     -p 8084:8084 \
#     -e ANTHROPIC_API_KEY=sk-ant-... \
#     -v $(pwd)/data:/data \
#     ghcr.io/yourorg/tianshu:latest
#
# 数据全部落在 /data:
#   /data/users.db                  账号 + session
#   /data/configs/auth.json         admin 配的 Claude 凭据
#   /data/output/reports/           历史报告 JSON
#   /data/output/evidence/          截图证据
# =====================================================================

# ---------- Stage 1: 基础镜像 + Python 依赖 ----------
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy

# 系统依赖:Playwright Chromium 运行需要的一堆 lib + node (Claude CLI 依赖) + curl + git
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates git \
        nodejs npm \
        # Playwright Chromium 运行需要的系统 lib
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
        libdbus-1-3 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
        libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 libatspi2.0-0 \
        # Playwright 需要的字体 + 中文字体
        fonts-noto-cjk fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# 装 uv (用作 pip 的替代,比 pip 快很多;但镜像里我们也支持 pip 兜底)
RUN curl -fsSL https://astral.sh/uv/install.sh | sh && \
    cp /root/.local/bin/uv /usr/local/bin/uv

WORKDIR /app

# 先单独 copy pyproject 装依赖 — 利用 Docker layer cache,代码改动时不用重新 resolve 依赖
COPY pyproject.toml ./
COPY uv.lock ./

# 装 Python 依赖
# - api    : FastAPI + uvicorn
# - ui     : Playwright + Pillow (截图/对比)
# - parsers: pypdf / python-docx / openpyxl / chardet (上传文件解析)
RUN uv pip install --system --no-cache \
    -e ".[api,ui,parsers]" || \
    pip install --no-cache-dir -e ".[api,ui,parsers]"


# ---------- Stage 2: Playwright Chromium ----------
FROM base AS browser
# 一定要装 chromium,弱网/UI/H5 工具都依赖它
RUN python -m playwright install --with-deps chromium


# ---------- Stage 3: Claude Code CLI ----------
FROM browser AS claude-cli
# Anthropic 官方 CLI,装到 /usr/local/bin/claude
# (Anthropic 推荐的安装脚本)
# CLAUDE_CLI_BUST: 改这个值 (或 --build-arg CLAUDE_CLI_BUST=<新值>) 强制重新拉取最新 CLI。
# 用途: Claude 出新模型(如 4.8)后,让镜像里的 CLI 跟上,使 opus/sonnet/haiku 别名解析到新版本。
ARG CLAUDE_CLI_BUST=20260529-1
RUN echo "claude-cli cache-bust=$CLAUDE_CLI_BUST" && \
    (curl -fsSL https://claude.ai/install.sh | bash || \
     npm install -g @anthropic-ai/claude-code || true)


# ---------- Stage 4: 运行镜像 ----------
FROM claude-cli AS runtime

# 代码 — 放在最后,代码改动不破坏前面 layer 缓存
COPY apps ./apps
COPY packages ./packages
COPY configs ./configs

# 数据目录 — 通过 volume 挂载,确保跨容器持久化
ENV AITK_DATA_DIR=/data \
    AITK_REPORT_DIR=/data/output/reports \
    AITK_EVIDENCE_DIR=/data/output/evidence \
    HOST=0.0.0.0 \
    PORT=8084 \
    HOME=/home/aitk
RUN mkdir -p /data /data/configs /data/output/reports /data/output/evidence

# 创建非 root 用户 — Claude Code CLI 安全保护:
# `--dangerously-skip-permissions cannot be used with root`,
# 所以 uvicorn 必须以普通用户身份跑,否则所有 LLM 调用都会失败。
RUN groupadd -r aitk && useradd -r -m -g aitk -d /home/aitk -s /bin/bash aitk && \
    mkdir -p /home/aitk/.local/bin /home/aitk/.claude && \
    # /root/.local/bin/claude 是 symlink → /root/.local/share/claude/versions/X,
    # 直接 cp -a 会保留 symlink target,但 aitk 没权限读 /root,运行时报
    # PermissionError。所以把整个 share/claude 拷过来,再建新 symlink。
    if [ -e /root/.local/share/claude ]; then \
        mkdir -p /home/aitk/.local/share && \
        cp -RL /root/.local/share/claude /home/aitk/.local/share/claude && \
        LATEST=$(ls /home/aitk/.local/share/claude/versions/ 2>/dev/null | head -1) && \
        if [ -n "$LATEST" ]; then \
            ln -sfn /home/aitk/.local/share/claude/versions/$LATEST /home/aitk/.local/bin/claude; \
        fi; \
    fi && \
    # Playwright 浏览器目录
    if [ -d /root/.cache/ms-playwright ]; then \
        mkdir -p /home/aitk/.cache && cp -a /root/.cache/ms-playwright /home/aitk/.cache/; \
    fi && \
    chown -R aitk:aitk /home/aitk /data /app

# 切换到非 root 用户
USER aitk
ENV PATH=/home/aitk/.local/bin:$PATH \
    CLAUDE_BIN=/home/aitk/.local/bin/claude \
    PLAYWRIGHT_BROWSERS_PATH=/home/aitk/.cache/ms-playwright

# 健康检查 — 5s 超时,30s 间隔
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://localhost:${PORT}/healthz || exit 1

EXPOSE 8084

# 启动命令 — 直接跑 uvicorn,所有 env 已经在上面配
CMD ["sh", "-c", "exec python -m uvicorn apps.api.main:app --host ${HOST:-0.0.0.0} --port ${PORT:-8084} --log-level info"]
