# 天枢·裁决 公网部署指南

## 概览

镜像里包含：
- Python 3.12 运行时
- Playwright + Chromium（截图 / 弱网 / H5 工具用）
- Anthropic Claude Code CLI（LLM 调用走它）
- 天枢工具 + 账号系统

部署后会得到：
- 一个公开 web 入口（默认 `http://主机IP:8084`）
- 任何人可以注册账号
- **第一个注册的人自动是管理员**，可以配 Claude 凭据 + 看所有人报告
- 普通用户只看自己的报告

所有 Claude 调用走管理员配的那一份凭据 — 公网部署等于用你的额度给所有人跑。

---

## 1. 本机构建 + 跑起来

```bash
cd ai_test_toolkit
docker compose build
docker compose up -d
```

第一次构建约 5-10 分钟（要装 Chromium + Claude CLI），后续重建因为有 layer cache 通常 1-2 分钟。

```bash
docker compose ps    # 查看运行状态
docker compose logs -f tianshu   # 实时日志
```

打开浏览器访问 `http://localhost:8084`，会自动跳到 `/register` 让你建第一个管理员账号。

---

## 2. 配 Claude 凭据

**方案 A：环境变量（最简单）**

编辑 `docker-compose.yml`，取消注释这一行并填 Key：

```yaml
environment:
  ANTHROPIC_API_KEY: sk-ant-api03-...
```

然后 `docker compose up -d --force-recreate`。

**方案 B：Web 设置页（推荐）**

不在 env 里配，用 admin 账号登录后进 `/settings` → 「模型接入」：
- API Key 模式：粘贴 `sk-ant-...`
- OAuth 模式：点「OAuth 授权」走浏览器跳 console.anthropic.com，把回调里的 code 粘回来

OAuth 凭据会自动落盘 `./data/configs/auth.json`，跨重启保留。

---

## 3. 暴露到公网

最简单：用宿主机公网 IP + 防火墙开放 8084 端口。

更好的做法 1 — **Cloudflare Tunnel**（免费、自带 HTTPS、不用买域名）：

```bash
# 装 cloudflared 后
cloudflared tunnel --url http://localhost:8084
```

会拿到一个 `https://<random>.trycloudflare.com` 公网链接。

更好的做法 2 — **Nginx 反代 + Let's Encrypt 证书**（自有域名）：

```nginx
# /etc/nginx/conf.d/tianshu.conf
server {
    listen 80;
    server_name tianshu.your-domain.com;

    location / {
        proxy_pass http://127.0.0.1:8084;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        # 长任务（深度审计）— uvicorn 默认 KeepAlive 60s 不够
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }
}
```

```bash
sudo certbot --nginx -d tianshu.your-domain.com
```

完成后改 `apps/api/main.py` 顶部的 session cookie `secure=False` 改成 `True`（HTTPS 才设这个）。

---

## 4. 数据备份

所有持久化数据都在 `./data/`：

```
data/
├── users.db              # 账号 + session SQLite
├── configs/
│   └── auth.json         # Claude 凭据（敏感!）
└── output/
    ├── reports/          # 历史报告 JSON
    └── evidence/         # 截图 PNG
```

备份：

```bash
tar czf tianshu-backup-$(date +%Y%m%d).tar.gz ./data
```

恢复：把 tar 解开覆盖到 `./data/`，然后 `docker compose restart`。

---

## 5. 升级

```bash
git pull
docker compose build
docker compose up -d
```

代码改动通常不动 layer cache 前段，只会重新装 Python deps + copy 代码，重建很快。

---

## 6. 常见问题

**Q: 启动后访问页面 502 / 连接超时？**
A: 看 `docker compose logs tianshu`，常见两种：
- Playwright Chromium 装失败 → 镜像有 fallback，但首次构建慢，等 5 分钟
- 端口被占 → 改 docker-compose.yml 里的 `"8084:8084"` 左半边

**Q: 所有运行都失败，错误说 "Claude SDK invocation failed"？**
A: admin 没配 Claude 凭据。进 `/settings` 配一下，或者 docker-compose.yml 里给 `ANTHROPIC_API_KEY`。

**Q: 想关闭公开注册？**
A: 进 `/api/auth/users` 看现有用户列表，目前 admin 可见但 UI 没暴露。要禁注册的话改 `apps/api/main.py` 的 `api_auth_register`，加个「已有 admin 后只允许 admin 邀请」的判断。

**Q: 怎么把别人的账号删了？**
A: 暂时只能直接改 SQLite：

```bash
docker compose exec tianshu sqlite3 /data/users.db \
  "DELETE FROM users WHERE email='someone@example.com'; \
   DELETE FROM sessions WHERE user_id NOT IN (SELECT id FROM users);"
```

**Q: 想让 Claude 调用走每个用户自己的额度？**
A: 当前架构不支持。要做的话需要：
1. 把 `_build_auth_env()` 改成读 user-scoped 凭据
2. 每个用户在 settings 页配自己的 token
3. `_RUNS` 里把当前用户 token 注入 LlmClient 的子进程 env

代码里 TODO 已经留了 hook 点，但工作量不小（≈ 1 天）。

---

## 7. 安全提醒

- 镜像里 session cookie `secure=False`，HTTPS 上线后务必改 True
- `ANTHROPIC_API_KEY` 写在 env 里 → docker inspect 可看到；用 docker secret 或 OAuth 模式更安全
- 公开部署 = 你的额度对所有注册用户开放，每跑一次工具都花钱，**留意月度账单**
- 把 `data/configs/auth.json` 加进备份白名单，但**不要**提交到 git
