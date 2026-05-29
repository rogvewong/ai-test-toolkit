"""FastAPI entry point for the AI Test Toolkit.

Provides multi-tenant REST endpoints for each step orchestrator. The API layer
is intentionally thin — it wires incoming requests to the same StepOrchestrator
classes that the CLI drives.

Run locally:
    uvicorn apps.api.main:app --reload --port 8080
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

try:
    from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request
    from fastapi.responses import (
        HTMLResponse,
        PlainTextResponse,
        StreamingResponse,
        FileResponse,
        Response,
        RedirectResponse,
        JSONResponse,
    )
    from pydantic import BaseModel
except ImportError as _exc:  # pragma: no cover
    raise RuntimeError(
        "FastAPI not installed. Install the 'api' extra: pip install -e '.[api]'"
    ) from _exc

# stdlib html.escape for OAuth callback page text safety
from html import escape  # noqa: E402

from packages.core.config import settings
from packages.core.llm import LlmClient
from packages.core.memory import LayeredMemory, SqliteMemoryStore
from packages.reporting import aggregate, render_html, render_markdown
from packages.tdr import TdrWorkstation
from packages.workflow.base import StepContext, resolve_evidence_dir
from packages.workflow.step1_requirement import Step1Orchestrator
from packages.workflow.step2_testcase import Step2Orchestrator
from packages.workflow.step4_api import Step4Orchestrator
from packages.workflow.step5_ui import Step5Orchestrator
from packages.workflow.step6_agent import Step6Orchestrator
from packages.workflow.step6_agent.executor import ExecutionEnvironment
from packages.core.auth import user_store, UserRecord
from packages.core.auth.user_store import SESSION_COOKIE_NAME, SESSION_TTL_SECONDS

app = FastAPI(title="AI Test Toolkit", version="0.1.0")


@app.on_event("startup")
async def _seed_admin_from_env() -> None:
    """启动时检查 $AITK_ADMIN_USER + $AITK_ADMIN_PASSWORD 是否已设。
    若有 + 数据库里还没这位用户 → 自动建管理员账号(idempotent)。
    Docker 部署时这是给"零交互"建第一个 admin 的钩子。
    """
    import os as _os
    u = (_os.environ.get("AITK_ADMIN_USER") or "").strip()
    p = _os.environ.get("AITK_ADMIN_PASSWORD") or ""
    if not u or not p:
        return
    try:
        # 已 import 在文件头部:`from packages.core.auth import user_store`
        # user_store 这里就是 UserStore 单例
        if user_store.get_user_by_username(u):
            return  # 已经存在,不要重置密码
        user_store.create_user(
            username=u, password=p, role="admin",
            display_name=_os.environ.get("AITK_ADMIN_DISPLAY_NAME") or u,
        )
        print(f"[bootstrap] seeded admin user: {u}")
    except Exception as exc:
        print(f"[bootstrap] failed to seed admin: {exc}")


@app.on_event("startup")
async def _start_oauth_refresh_loop() -> None:
    """后台周期性刷新 OAuth token — 每 20 分钟查一次,过期前 5 分钟自动续。
    这样即使没人跑工具、或一个 run 跑很久,token 也不会过期。
    """
    import asyncio as _a

    async def _loop() -> None:
        # 启动后先等几秒让 app 完全起来,然后立即刷一次
        await _a.sleep(5)
        while True:
            try:
                r = await ensure_fresh_oauth_token()
                if r.get("status") in ("refreshed", "error", "no_refresh_token"):
                    print(f"[oauth-loop] {r.get('status')}: {r.get('detail','')}")
            except Exception as exc:
                print(f"[oauth-loop] 异常: {exc}")
            await _a.sleep(10 * 60)  # 10 分钟查一次

    _asyncio.create_task(_loop())


# ---------------------------------------------------------------------
# Web 用户账号体系 (与 Claude 凭据完全独立)
# ---------------------------------------------------------------------
# 设计:
# - SESSION_COOKIE_NAME = aitk_session,HttpOnly cookie,30 天 TTL
# - 凡是 /tools /reports /settings 及大多数 /api/* 必须 已登录
# - 白名单(免登录):/login /register /api/auth/* /healthz /static /api/healthz
# - 注册的第一个用户自动变 admin,后续注册都是普通 user
# - admin 可以改 Claude 凭据 + 看所有人的报告;user 只看自己

_AUTH_EXEMPT_PATHS: tuple[str, ...] = (
    "/healthz",
    "/login",
    "/register",
    "/favicon.ico",
)
_AUTH_EXEMPT_PREFIXES: tuple[str, ...] = (
    "/api/auth/",
    "/api/healthz",
    "/static/",
    "/api/static/",
)


def _is_auth_exempt(path: str) -> bool:
    if path in _AUTH_EXEMPT_PATHS:
        return True
    return any(path.startswith(p) for p in _AUTH_EXEMPT_PREFIXES)


@app.middleware("http")
async def session_guard(request: Request, call_next):
    """全局 session 闸:未登录 + 访问非白名单路径 → 网页 302 到 /login,
    API 返回 401。已登录:把 user 注到 request.state.user。
    """
    path = request.url.path
    token = request.cookies.get(SESSION_COOKIE_NAME, "")
    user = user_store.resolve_session(token) if token else None
    request.state.current_user = user
    if user is None and not _is_auth_exempt(path):
        # 第一次部署 — 还没有任何账号,直接放行 /register 引导建第一个 admin
        if user_store.count_users() == 0:
            if path == "/":
                return RedirectResponse("/register", status_code=302)
            if path.startswith("/api/"):
                # API 也允许穿透 — 让前端 hint
                response = await call_next(request)
                return response
        # 已经有账号但当前未登录 → 网页登录,API 401
        if path.startswith("/api/"):
            return JSONResponse({"error": "unauthorized", "detail": "请先登录"}, status_code=401)
        return RedirectResponse(f"/login?next={path}", status_code=302)
    response = await call_next(request)
    return response


def require_user(request: Request) -> UserRecord:
    user = getattr(request.state, "current_user", None)
    if not user:
        raise HTTPException(401, "unauthorized")
    return user


def require_admin(request: Request) -> UserRecord:
    user = require_user(request)
    if not user.is_admin():
        raise HTTPException(403, "需要管理员权限")
    return user


def _set_session_cookie(resp: Response, token: str) -> None:
    # secure=False 是因为先 HTTP 跑;上线 HTTPS 时改 True
    resp.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(resp: Response) -> None:
    resp.delete_cookie(SESSION_COOKIE_NAME, path="/")


# ---------- auth endpoints ----------

class RegisterReq(BaseModel):
    username: str
    password: str
    display_name: str | None = None


class LoginReq(BaseModel):
    username: str
    password: str


@app.post("/api/auth/register")
async def api_auth_register(req: RegisterReq, request: Request) -> JSONResponse:
    """注册端点 — 仅在系统还没用户时开放(首次安装建 admin)。
    一旦有 admin,公开注册自动失效;后续新用户必须由 admin 在管理后台创建。
    """
    if user_store.count_users() > 0:
        raise HTTPException(
            403,
            "公开注册已关闭。新账号需由管理员在「用户管理」创建。",
        )
    try:
        user = user_store.create_user(
            username=req.username,
            password=req.password,
            display_name=(req.display_name or "").strip(),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    ua = request.headers.get("user-agent", "")[:240]
    sess = user_store.create_session(user.id, user_agent=ua)
    resp = JSONResponse({"ok": True, "user": user.to_dict(), "is_first_admin": user.is_admin()})
    _set_session_cookie(resp, sess.token)
    return resp


@app.post("/api/auth/login")
async def api_auth_login(req: LoginReq, request: Request) -> JSONResponse:
    user = user_store.verify_password(req.username, req.password)
    if not user:
        raise HTTPException(401, "用户名或密码错误")
    ua = request.headers.get("user-agent", "")[:240]
    sess = user_store.create_session(user.id, user_agent=ua)
    resp = JSONResponse({"ok": True, "user": user.to_dict()})
    _set_session_cookie(resp, sess.token)
    return resp


@app.post("/api/auth/logout")
async def api_auth_logout(request: Request) -> JSONResponse:
    token = request.cookies.get(SESSION_COOKIE_NAME, "")
    if token:
        user_store.delete_session(token)
    resp = JSONResponse({"ok": True})
    _clear_session_cookie(resp)
    return resp


@app.get("/api/auth/me")
async def api_auth_me(request: Request) -> dict[str, Any]:
    user = getattr(request.state, "current_user", None)
    if not user:
        return {"authenticated": False, "first_setup_needed": user_store.count_users() == 0}
    return {"authenticated": True, "user": user.to_dict()}


class ChangePasswordReq(BaseModel):
    old_password: str
    new_password: str


@app.post("/api/auth/change_password")
async def api_auth_change_password(req: ChangePasswordReq, request: Request) -> dict[str, Any]:
    """用户主动改密 — 验旧密码 + 自主选择是否改 (即非强制)。"""
    user = require_user(request)
    verified = user_store.verify_password(user.username, req.old_password)
    if not verified:
        raise HTTPException(400, "原密码不正确")
    try:
        user_store.change_password(user.id, req.new_password)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "msg": "密码已更新,所有现有 session 已撤销,请重新登录"}


# ---------- admin-only user management ----------

class AdminCreateUserReq(BaseModel):
    username: str
    password: str
    display_name: str | None = None
    role: str | None = "user"  # 'user' 或 'admin'


class AdminResetPasswordReq(BaseModel):
    new_password: str


@app.get("/api/auth/users")
async def api_auth_list_users(request: Request) -> dict[str, Any]:
    """管理员看用户列表。"""
    require_admin(request)
    return {"users": [u.to_dict() for u in user_store.list_users()]}


@app.post("/api/auth/users")
async def api_auth_admin_create_user(req: AdminCreateUserReq, request: Request) -> dict[str, Any]:
    """管理员创建新用户。
    Body: {username, password, display_name?, role?}
    """
    require_admin(request)
    role = req.role if req.role in ("admin", "user") else "user"
    try:
        user = user_store.create_user(
            username=req.username,
            password=req.password,
            display_name=(req.display_name or "").strip(),
            role=role,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "user": user.to_dict()}


# ---------- 批量创建 ----------

def _gen_random_password(length: int = 10) -> str:
    """生成强但可读的随机密码:字母+数字 (避开易混淆的 0/O/l/1)。"""
    import secrets as _s
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(_s.choice(alphabet) for _ in range(length))


class BulkUserItem(BaseModel):
    username: str
    password: str | None = None  # 留空则自动生成
    display_name: str | None = None
    role: str | None = None


class BulkCreateUsersReq(BaseModel):
    users: list[BulkUserItem]
    default_role: str = "user"
    default_password_length: int = 10  # auto-gen 时长度


@app.post("/api/auth/users/bulk")
async def api_auth_admin_bulk_create_users(
    req: BulkCreateUsersReq, request: Request,
) -> dict[str, Any]:
    """批量创建用户。
    - 每个 item 的 password 留空 → 用 _gen_random_password 生成
    - 失败的不会阻塞剩下的;返回 {created:[...], failed:[...]} 让前端展示
    - created 列表里会把刚才用的密码原文带回去(后端不会留明文,只是这一次响应有),
      admin 复制下来发给员工即可
    """
    require_admin(request)
    default_role = req.default_role if req.default_role in ("admin", "user") else "user"
    created: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    seen_in_batch: set[str] = set()
    for raw in req.users:
        uname = (raw.username or "").strip().lower()
        if not uname:
            failed.append({"username": raw.username, "error": "用户名为空"})
            continue
        if uname in seen_in_batch:
            failed.append({"username": uname, "error": "本批中重复"})
            continue
        seen_in_batch.add(uname)
        pwd = raw.password
        was_auto = False
        if not pwd:
            pwd = _gen_random_password(max(8, req.default_password_length))
            was_auto = True
        role = raw.role if raw.role in ("admin", "user") else default_role
        try:
            u = user_store.create_user(
                username=uname,
                password=pwd,
                display_name=(raw.display_name or "").strip(),
                role=role,
            )
        except ValueError as exc:
            failed.append({"username": uname, "error": str(exc)})
            continue
        created.append({
            **u.to_dict(),
            "password": pwd,             # 仅这一次响应里返回原文密码
            "password_auto_generated": was_auto,
        })
    return {
        "ok": True,
        "summary": {"total": len(req.users), "created": len(created), "failed": len(failed)},
        "created": created,
        "failed": failed,
    }


@app.post("/api/auth/users/{user_id}/reset_password")
async def api_auth_admin_reset_password(
    user_id: int, req: AdminResetPasswordReq, request: Request,
) -> dict[str, Any]:
    """管理员给某用户重置密码(不验旧密码)。"""
    require_admin(request)
    target = user_store.get_user(user_id)
    if not target:
        raise HTTPException(404, "用户不存在")
    try:
        user_store.admin_reset_password(user_id, req.new_password)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "msg": f"已重置 {target.username} 的密码;该用户所有现有 session 已撤销"}


@app.delete("/api/auth/users/{user_id}")
async def api_auth_admin_delete_user(user_id: int, request: Request) -> dict[str, Any]:
    """管理员删除用户。禁止自删,禁止删最后一个 admin。"""
    admin = require_admin(request)
    if user_id == admin.id:
        raise HTTPException(400, "不能删除自己")
    target = user_store.get_user(user_id)
    if not target:
        raise HTTPException(404, "用户不存在")
    # 检查是不是最后一个 admin
    if target.is_admin():
        all_admins = [u for u in user_store.list_users() if u.is_admin()]
        if len(all_admins) <= 1:
            raise HTTPException(400, "不能删除最后一个管理员")
    user_store.delete_user(user_id)
    return {"ok": True, "msg": f"已删除用户 {target.username}"}


# ---------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------

class Tenant(BaseModel):
    tenant_id: str
    project_id: str


async def get_tenant(
    x_tenant_id: str | None = Header(default=None),
    x_project_id: str | None = Header(default=None),
) -> Tenant:
    if not x_tenant_id or not x_project_id:
        raise HTTPException(status_code=400, detail="X-Tenant-Id / X-Project-Id required")
    return Tenant(tenant_id=x_tenant_id, project_id=x_project_id)


async def make_context(tenant: Tenant, inputs: dict[str, Any]) -> StepContext:
    run_id = str(uuid4())
    db_path = str((settings.report_output_dir / "memory.db").resolve())
    settings.report_output_dir.mkdir(parents=True, exist_ok=True)
    store = SqliteMemoryStore(db_path)
    memory = LayeredMemory(
        store=store,
        run_id=run_id,
        project_id=tenant.project_id,
        tenant_id=tenant.tenant_id,
    )
    return StepContext(
        run_id=run_id,
        project_id=tenant.project_id,
        tenant_id=tenant.tenant_id,
        inputs=inputs,
        memory=memory,
        llm=LlmClient(),
        evidence_dir=resolve_evidence_dir(),
    )


# ---------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------

class Step1Request(BaseModel):
    prd: str | dict[str, Any] | None = None
    prototype: str | None = None
    ui_design: str | None = None
    flow_chart: str | None = None
    api_doc: str | None = None
    business_rules: str | None = None


class Step2Request(BaseModel):
    requirement_report: dict[str, Any]


class Step4Request(BaseModel):
    requirement_report: dict[str, Any]
    test_case_report: dict[str, Any]
    api_doc: str | dict[str, Any] | None = None
    execution_results: dict[str, Any] | None = None


class Step5Request(BaseModel):
    requirement_report: dict[str, Any] | None = None
    test_case_report: dict[str, Any] | None = None
    design_assets: list[dict[str, Any]] = []
    actual_snapshots: list[dict[str, Any]] = []
    state_captures: list[dict[str, Any]] = []


class Step6Request(BaseModel):
    test_case_report: dict[str, Any]
    automation_risk: dict[str, Any] = {}
    env_info: dict[str, Any] = {}
    dry_run: bool = True


class TdrRequest(BaseModel):
    run_id: str
    artifacts: list[str]
    reviewer_scores: list[dict[str, dict[str, float]]]
    comments: list[dict[str, Any]] = []


# ---------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------

@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "version": "0.1.0"}


@app.post("/runs/step1")
async def run_step1(req: Step1Request, tenant: Tenant = Depends(get_tenant)) -> dict[str, Any]:
    ctx = await make_context(tenant, req.model_dump())
    report = await Step1Orchestrator(ctx).execute()
    return report.model_dump(mode="json")


@app.post("/runs/step2")
async def run_step2(req: Step2Request, tenant: Tenant = Depends(get_tenant)) -> dict[str, Any]:
    ctx = await make_context(tenant, req.model_dump())
    report = await Step2Orchestrator(ctx).execute()
    return report.model_dump(mode="json")


@app.post("/runs/step4")
async def run_step4(req: Step4Request, tenant: Tenant = Depends(get_tenant)) -> dict[str, Any]:
    ctx = await make_context(tenant, req.model_dump())
    report = await Step4Orchestrator(ctx).execute()
    return report.model_dump(mode="json")


@app.post("/runs/step5")
async def run_step5(req: Step5Request, tenant: Tenant = Depends(get_tenant)) -> dict[str, Any]:
    ctx = await make_context(tenant, req.model_dump())
    report = await Step5Orchestrator(ctx).execute()
    return report.model_dump(mode="json")


@app.post("/runs/step6")
async def run_step6(req: Step6Request, tenant: Tenant = Depends(get_tenant)) -> dict[str, Any]:
    inputs = req.model_dump()
    inputs["execution_env"] = ExecutionEnvironment(
        evidence_dir=Path(settings.evidence_output_dir) / "step6",
        dry_run=req.dry_run,
    )
    ctx = await make_context(tenant, inputs)
    report = await Step6Orchestrator(ctx).execute()
    return report.model_dump(mode="json")


def _normalize_tdr_comment(c: dict[str, Any]) -> dict[str, Any] | None:
    """把 UI / API 提交的 TDR 评论字段，规范化为 add_comment 接受的完整 7 字段。

    宽容映射：
      - text / comment → observation
      - 缺 id          → 自动生成
      - 缺 dimension   → "general"
      - 缺 severity    → "info"
      - 缺其他字段     → 空字符串
    完全空的评论（无 text/observation）返回 None，调用方应跳过。

    被 /tdr 和 /api/tdr/submit 共用，保证两个入口行为一致。
    """
    if not isinstance(c, dict):
        return None
    body_text = c.get("observation") or c.get("text") or c.get("comment") or ""
    if not body_text:
        return None
    return {
        "id":                c.get("id") or f"cmt-{uuid4().hex[:8]}",
        "dimension":         c.get("dimension") or "general",
        "severity":          c.get("severity") or "info",
        "location":          c.get("location") or "",
        "observation":       body_text,
        "suggestion":        c.get("suggestion") or "",
        "requires_response": bool(c.get("requires_response", False)),
    }


@app.post("/tdr")
async def submit_tdr(req: TdrRequest, tenant: Tenant = Depends(get_tenant)) -> dict[str, Any]:
    ws = TdrWorkstation(
        run_id=req.run_id, project_id=tenant.project_id, tenant_id=tenant.tenant_id
    )
    ws.submit_artifacts(req.artifacts)
    ws.score(req.reviewer_scores)
    for raw in req.comments:
        # 兼容两种格式：完整字段 / UI 简短字段（{severity, text}）
        normalized = _normalize_tdr_comment(raw)
        if normalized is None:
            continue
        try:
            ws.add_comment(**normalized)
        except TypeError as e:
            raise HTTPException(422, f"评论字段不被工作站接受：{e}")
    return ws.finalize()


# ---------------------------------------------------------------------
# Standards + TDR toolkit — dashboard-facing JSON endpoints
# ---------------------------------------------------------------------


@app.get("/api/standards")
async def standards_all() -> dict[str, Any]:
    """Return all five governance standards (process / quality / data / tdr / team)."""
    from packages.core.config import standards as S
    return {
        "process": S.process,
        "quality": S.quality,
        "data": S.data,
        "tdr": S.tdr,
        "team": S.team,
    }


@app.get("/api/standards/{name}")
async def standards_one(name: str) -> dict[str, Any]:
    from packages.core.config import standards as S
    mapping = {
        "process": S.process,
        "quality": S.quality,
        "data": S.data,
        "tdr": S.tdr,
        "team": S.team,
    }
    if name not in mapping:
        raise HTTPException(status_code=404, detail=f"unknown standard: {name}")
    return mapping[name]


@app.get("/api/tdr/reviews")
async def tdr_list() -> dict[str, Any]:
    """Enumerate all TDR reviews on disk + summary for dashboard listing.

    扫描两种位置（兼容旧/新落盘）：
      - 顶层文件: <report_dir>/tdr_<run_id>.json （/api/tdr/submit mirror 出来的）
      - 嵌套目录: <report_dir>/tdr/<run_id>/review.json （TdrWorkstation.finalize 写入）
    """
    import json as _json
    base = settings.report_output_dir
    if not base.exists():
        return {"reviews": []}
    reviews: list[dict[str, Any]] = []
    seen_run_ids: set[str] = set()

    # 路径 A: 顶层 tdr_*.json
    for f in sorted(base.glob("tdr_*.json")):
        try:
            data = _json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        rid = data.get("meta", {}).get("run_id") or f.stem.removeprefix("tdr_")
        if rid in seen_run_ids:
            continue
        seen_run_ids.add(rid)
        reviews.append(_summarize_tdr_review(data, rid, f.name))

    # 路径 B: tdr/<run_id>/review.json（TdrWorkstation 默认写入位置）
    tdr_dir = base / "tdr"
    if tdr_dir.exists():
        for review_file in sorted(tdr_dir.glob("*/review.json")):
            try:
                data = _json.loads(review_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            rid = data.get("meta", {}).get("run_id") or review_file.parent.name
            if rid in seen_run_ids:
                continue  # 已被 A 覆盖
            seen_run_ids.add(rid)
            reviews.append(_summarize_tdr_review(data, rid, f"tdr/{review_file.parent.name}/review.json"))

    return {"reviews": reviews}


def _iter_saved_report_files() -> Iterable[Path]:
    """枚举所有已保存的报告文件（顶层 *.json + 嵌套 tdr/<run_id>/review.json）。

    被 /api/reports、/api/reports/export 等用来扫盘。统一这里让两种位置兼容。
    """
    out_dir = Path(settings.report_output_dir)
    if not out_dir.exists():
        return
    yield from out_dir.glob("*.json")
    nested_tdr = out_dir / "tdr"
    if nested_tdr.exists():
        yield from nested_tdr.glob("*/review.json")


def _find_report_files_by_run_id(run_id: str) -> list[Path]:
    """按 run_id 找所有匹配的报告文件（含嵌套 TDR）。

    返回多个 Path 是因为：
      - 工具报告：<report_dir>/<tool_id>_<run_id>.json
      - TDR mirror: <report_dir>/tdr_<run_id>.json
      - TDR 嵌套:   <report_dir>/tdr/<run_id>/review.json
    /api/reports/{run_id} 取第一个，DELETE 全部删。
    """
    out_dir = Path(settings.report_output_dir)
    if not out_dir.exists():
        return []
    found: list[Path] = list(out_dir.glob(f"*_{run_id}.json"))
    nested = out_dir / "tdr" / run_id / "review.json"
    if nested.exists():
        found.append(nested)
    return found


def _parse_tool_id_from_stem(stem: str, run_id: str | None = None) -> str:
    """从报告 JSON 的 stem 解出 tool_id。

    文件名形如 `<tool_id>_<run_id>.json`，但 tool_id 自身可能含下划线
    （network_resilience / h5_adapt / seo_audit）。简单的 `split("_", 1)[0]`
    会把 network_resilience 截成 network — 导致导出 HTML 标题、`/api/reports/{run_id}`
    返回的 tool_id 都错。

    解析顺序：
      1. 若知道 run_id 且 stem 以 `_<run_id>` 结尾，直接剪掉。
      2. 否则按 TOOL_CATALOG 的 id（含 'tdr'）做最长前缀匹配。
      3. 最后兜底 split("_", 1)[0]，至少不抛异常。
    """
    if run_id and stem.endswith("_" + run_id):
        return stem[: -(len(run_id) + 1)]
    valid_ids = sorted(
        ({t["id"] for t in TOOL_CATALOG} | {"tdr"}),
        key=lambda s: -len(s),
    )
    for tid in valid_ids:
        if stem == tid or stem.startswith(tid + "_"):
            return tid
    return stem.split("_", 1)[0] if "_" in stem else stem


def _summarize_tdr_review(data: dict[str, Any], run_id: str, file_repr: str) -> dict[str, Any]:
    """共用：把 TDR 报告 JSON 收成 dashboard 卡片。"""
    meta = data.get("meta") or {}
    return {
        "run_id": run_id,
        "file": file_repr,
        "decision": data.get("decision"),
        "overall_score": data.get("overall_score"),
        "dimension_scores": data.get("dimension_scores") or {},
        "comments_n": len(data.get("comments") or []),
        "comments_blocker": sum(
            1 for c in (data.get("comments") or [])
            if c.get("severity") in {"blocker", "major"} and not c.get("resolved")
        ),
        "signatures_n": len(data.get("signatures") or []),
        "artifacts_n": len(data.get("artifacts_reviewed") or []),
        "follow_up_n": len(data.get("follow_up_items") or []),
        "created_at": meta.get("created_at") or meta.get("timestamp"),
        "project_id": meta.get("project_id"),
        "tenant_id": meta.get("tenant_id"),
    }


@app.get("/api/tdr/reviews/{run_id}")
async def tdr_get(run_id: str) -> dict[str, Any]:
    """读 TDR 详情 — 同时支持顶层 mirror 和嵌套 review.json 两种存储。"""
    import json as _json
    base = settings.report_output_dir
    # 顶层 mirror（/api/tdr/submit 写出来的）
    flat = base / f"tdr_{run_id}.json"
    if flat.exists():
        return _json.loads(flat.read_text(encoding="utf-8"))
    # 嵌套（TdrWorkstation.finalize 默认位置）
    nested = base / "tdr" / run_id / "review.json"
    if nested.exists():
        return _json.loads(nested.read_text(encoding="utf-8"))
    raise HTTPException(status_code=404, detail=f"no TDR review for {run_id}")


class TdrWorkbenchRequest(BaseModel):
    """Header-less TDR submission for the browser toolkit.

    Tenant / project are optional; fall back to 'default' so the UI has zero
    friction. The TDR spec governance is enforced by the workstation itself
    (dimensions, decision rules, signing).
    """
    run_id: str | None = None
    project_id: str = "default"
    tenant_id: str = "default"
    artifacts: list[str] = []
    reviewer_scores: list[dict[str, dict[str, float]]] = []
    comments: list[dict[str, Any]] = []


@app.post("/api/tdr/submit")
async def tdr_submit_ui(req: TdrWorkbenchRequest) -> dict[str, Any]:
    """Run a TDR review end-to-end from UI inputs, persist to disk, return the full report."""
    import json as _json
    ws = TdrWorkstation(
        run_id=req.run_id or f"ui-{uuid4().hex[:8]}",
        project_id=req.project_id,
        tenant_id=req.tenant_id,
    )
    ws.submit_artifacts(req.artifacts)
    ws.score(req.reviewer_scores)
    for raw in req.comments:
        normalized = _normalize_tdr_comment(raw)
        if normalized is None:
            continue
        try:
            ws.add_comment(**normalized)
        except TypeError as e:
            raise HTTPException(422, f"评论字段不被工作站接受：{e}")
    result = ws.finalize()
    # Mirror the CLI: persist next to the other reports so the listing endpoint picks it up
    path = settings.report_output_dir / f"tdr_{ws.run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return {"path": str(path), "report": result}


def _load_step_reports(run_id: str) -> dict[str, dict[str, Any]]:
    """Best-effort load of stepN_<run_id>.json files from the reports dir.

    Returns a dict keyed by "step1"…"step6" so aggregate() can render a real
    bundle instead of an empty shell.
    """
    import json as _json
    out: dict[str, dict[str, Any]] = {}
    base = settings.report_output_dir
    if not base.exists():
        return out
    for n in ("1", "2", "4", "5", "6"):
        p = base / f"step{n}_{run_id}.json"
        if p.exists():
            try:
                out[f"step{n}"] = _json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
    return out


def _load_tdr(run_id: str) -> dict[str, Any] | None:
    import json as _json
    p = settings.report_output_dir / f"tdr_{run_id}.json"
    if p.exists():
        try:
            return _json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _infer_tenant_from_reports(run_id: str) -> tuple[str, str]:
    """Read tenant_id / project_id out of the first stepN_<run_id>.json on disk.

    The report-viewing endpoints are read-only against files the CLI/API already
    produced — requiring the caller to also know the headers is friction with
    no security benefit. We pull them straight from report.meta.
    """
    reports = _load_step_reports(run_id)
    for _, rpt in reports.items():
        meta = rpt.get("meta") or {}
        t = meta.get("tenant_id")
        p = meta.get("project_id")
        if t and p:
            return str(t), str(p)
    return "default", "unknown"


@app.get("/runs/{run_id}/report.md", response_class=PlainTextResponse)
async def report_md(
    run_id: str,
    x_tenant_id: str | None = Header(default=None),
    x_project_id: str | None = Header(default=None),
) -> str:
    reports = _load_step_reports(run_id)
    if not reports:
        raise HTTPException(status_code=404, detail=f"no step reports for run_id={run_id}")
    inferred_t, inferred_p = _infer_tenant_from_reports(run_id)
    bundle = aggregate(
        run_id=run_id,
        project_id=x_project_id or inferred_p,
        tenant_id=x_tenant_id or inferred_t,
        step_reports=reports,
        tdr=_load_tdr(run_id),
    )
    return render_markdown(bundle)


@app.get("/runs/{run_id}/report.html", response_class=HTMLResponse)
async def report_html(
    run_id: str,
    x_tenant_id: str | None = Header(default=None),
    x_project_id: str | None = Header(default=None),
) -> str:
    reports = _load_step_reports(run_id)
    if not reports:
        raise HTTPException(status_code=404, detail=f"no step reports for run_id={run_id}")
    inferred_t, inferred_p = _infer_tenant_from_reports(run_id)
    bundle = aggregate(
        run_id=run_id,
        project_id=x_project_id or inferred_p,
        tenant_id=x_tenant_id or inferred_t,
        step_reports=reports,
        tdr=_load_tdr(run_id),
    )
    return render_html(bundle)


@app.get("/api/runs")
async def runs_json() -> dict[str, Any]:
    """Dashboard data source — enumerate runs, pull gate/confidence/key metrics per step."""
    import re
    base = settings.report_output_dir
    if not base.exists():
        return {"runs": []}
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    pattern = re.compile(r"^step(\d)_([0-9a-f-]+)\.json$")
    for f in sorted(base.glob("step*_*.json")):
        m = pattern.match(f.name)
        if not m:
            continue
        step, rid = m.group(1), m.group(2)
        try:
            import json as _json
            data = _json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        meta = data.get("meta") or {}
        gd = data.get("gate_decision") or {}
        conf = data.get("confidence") or {}
        metrics = data.get("metrics") or {}
        entry: dict[str, Any] = {
            "gate_action": gd.get("action"),
            "gate_reasons": gd.get("reasons") or [],
            "gate_blockers": gd.get("blockers") or [],
            "next_step": gd.get("next_step"),
            "confidence": conf.get("score"),
            "confidence_grade": conf.get("grade"),
            "project_id": meta.get("project_id"),
            "tenant_id": meta.get("tenant_id"),
            "model": meta.get("model_id"),
        }
        # Per-step key counts
        if step == "1":
            entry["materials_n"] = len(data.get("materials") or [])
            entry["modules_n"] = len(data.get("modules") or [])
            entry["flows_n"] = len(data.get("flows") or [])
            entry["ambiguities_n"] = len(data.get("ambiguities") or [])
        elif step == "2":
            entry["p0_n"] = len(data.get("p0_cases") or [])
            entry["p1_n"] = len(data.get("p1_cases") or [])
            entry["p2_n"] = len(data.get("p2_cases") or [])
            entry["automation"] = data.get("automation_summary") or {}
        elif step == "4":
            entry["apis_n"] = len(data.get("api_list") or [])
            entry["functional_n"] = len(data.get("functional_results") or [])
            entry["boundary_n"] = len(data.get("boundary_results") or [])
            entry["security_n"] = len(data.get("security_results") or [])
            entry["defects_n"] = len(data.get("defects") or [])
            entry["pass_rate"] = metrics.get("pass_rate")
        elif step == "5":
            entry["references_n"] = len(data.get("references") or [])
            entry["diffs_n"] = len(data.get("diffs") or [])
            entry["severity_map"] = data.get("severity_map") or {}
            entry["deviation"] = data.get("overall_deviation_ratio")
        elif step == "6":
            entry["steps_n"] = len(data.get("steps") or [])
            entry["failures_n"] = len(data.get("failures") or [])
            entry["success_rate"] = metrics.get("success_rate")
        grouped.setdefault(rid, {})[f"step{step}"] = entry
    runs = []
    for rid, steps in grouped.items():
        any_meta = next(iter(steps.values()), {})
        runs.append({
            "run_id": rid,
            "project_id": any_meta.get("project_id"),
            "tenant_id": any_meta.get("tenant_id"),
            "steps": steps,
        })
    runs.sort(key=lambda r: r["run_id"])
    return {"runs": runs}


@app.get("/api/runs/{run_id}/raw/{step}")
async def run_step_raw(run_id: str, step: str) -> dict[str, Any]:
    """Return the raw step report JSON for dashboard drill-down."""
    if step not in {"1", "2", "4", "5", "6"}:
        raise HTTPException(status_code=400, detail="step must be 1/2/4/5/6")
    p = settings.report_output_dir / f"step{step}_{run_id}.json"
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"no step{step} for run_id={run_id}")
    import json as _json
    return _json.loads(p.read_text(encoding="utf-8"))


PIPELINE_DASHBOARD_HTML = r"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8" />
<title>AI Test Toolkit — Pipeline Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@300;400;500;600;700&family=Noto+Sans+SC:wght@300;400;500;600&family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
  :root {
    --bg: #0b1020; --panel: #131a2e; --panel2: #1a2340; --ink: #e7eaf3;
    --muted: #8a93b0; --border: #2a3352;
    --pass: #1fba8a; --warn: #f2b035; --reject: #ef4d5e; --info: #5aa9ff;
    --accent: #9a6cff;
  }
  * { box-sizing: border-box; }
  html, body { background: var(--bg); color: var(--ink); margin: 0; font-family: -apple-system, "PingFang SC", "Segoe UI", sans-serif; }
  header { padding: 20px 32px; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; align-items: center; }
  header h1 { margin: 0; font-size: 20px; letter-spacing: .02em; }
  header .sub { color: var(--muted); font-size: 13px; margin-top: 2px; }
  header a { color: var(--info); text-decoration: none; margin-left: 12px; font-size: 13px; }
  main { padding: 24px 32px 80px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(440px, 1fr)); gap: 18px; }
  .card { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }
  .card header { padding: 14px 18px; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; align-items: start; gap: 12px; }
  .card header h2 { font-size: 13px; margin: 0; font-weight: 600; color: var(--muted); letter-spacing: .08em; text-transform: uppercase; }
  .card header .rid { font-family: ui-monospace, Menlo, monospace; font-size: 12px; color: var(--ink); margin-top: 2px; word-break: break-all; }
  .card .meta { font-size: 12px; color: var(--muted); }
  .steprow { display: flex; flex-wrap: wrap; gap: 6px; padding: 14px 18px; border-bottom: 1px solid var(--border); }
  .pill { font-size: 11px; padding: 3px 10px; border-radius: 999px; font-weight: 600; letter-spacing: .03em; border: 1px solid transparent; }
  .pill.missing { background: transparent; border-color: var(--border); color: var(--muted); }
  .pill.pass { background: rgba(31,186,138,.12); color: var(--pass); border-color: rgba(31,186,138,.4); }
  .pill.warn { background: rgba(242,176,53,.12); color: var(--warn); border-color: rgba(242,176,53,.4); }
  .pill.reject { background: rgba(239,77,94,.12); color: var(--reject); border-color: rgba(239,77,94,.4); }
  .stepdetail { padding: 10px 18px; border-bottom: 1px dashed var(--border); }
  .stepdetail:last-child { border-bottom: none; }
  .stepdetail h3 { font-size: 12px; margin: 0 0 6px; color: var(--muted); letter-spacing: .06em; text-transform: uppercase; display: flex; justify-content: space-between; align-items: center; }
  .stepdetail h3 .right { font-family: ui-monospace, Menlo, monospace; font-weight: 400; color: var(--muted); font-size: 11px; }
  .kv { display: flex; flex-wrap: wrap; gap: 10px 18px; font-size: 12px; }
  .kv span { color: var(--muted); }
  .kv b { color: var(--ink); font-weight: 500; }
  .reason { font-size: 12px; color: var(--ink); margin-top: 4px; line-height: 1.5; }
  .reason.reject { color: #ffb5bd; }
  .reason.warn { color: #ffdb8a; }
  .footer { padding: 12px 18px; display: flex; gap: 14px; font-size: 12px; background: var(--panel2); }
  .footer a { color: var(--info); text-decoration: none; }
  .footer a:hover { text-decoration: underline; }
  .empty { text-align: center; padding: 80px 20px; color: var(--muted); }
  .toolbar { display: flex; gap: 14px; align-items: center; padding-bottom: 14px; font-size: 13px; color: var(--muted); }
  .toolbar input { background: var(--panel); color: var(--ink); border: 1px solid var(--border); border-radius: 6px; padding: 6px 10px; font-size: 13px; width: 260px; }
  details { margin-top: 8px; }
  details summary { cursor: pointer; color: var(--info); font-size: 12px; }
  details pre { background: var(--bg); padding: 10px; border-radius: 6px; font-size: 11px; overflow-x: auto; color: #c9d3ee; max-height: 320px; }
  .confbar { height: 4px; background: var(--panel2); border-radius: 2px; overflow: hidden; margin-top: 6px; }
  .confbar > div { height: 100%; background: linear-gradient(90deg, var(--reject), var(--warn) 50%, var(--pass)); }
</style>
</head>
<body>
<header>
  <div>
    <h1>AI Test Toolkit <span style="color:var(--accent)">· Dashboard</span></h1>
    <div class="sub">5-step pipeline runs · gate decisions · confidence · drill-down</div>
  </div>
  <nav>
    <a href="/docs">Swagger</a>
    <a href="/redoc">ReDoc</a>
    <a href="/healthz">Health</a>
  </nav>
</header>
<main>
  <div class="toolbar">
    <input id="filter" placeholder="按 run_id / project 过滤…" />
    <span id="count"></span>
  </div>
  <div id="grid" class="grid"></div>
  <div id="empty" class="empty" style="display:none">暂无 run 报告</div>
</main>
<script>
const STEP_NAMES = {1:"需求拆解",2:"用例设计",4:"接口测试",5:"UI 一致性",6:"Agent 执行"};
const GATE_CLASS = {
  "pass": "pass", "warn_and_continue": "warn", "reject_with_report": "reject"
};
function pct(v){ return v==null ? "—" : (v*100).toFixed(0)+"%"; }
function num(v){ return v==null ? "—" : v; }

function cardHtml(run){
  const s = run.steps || {};
  const stepPills = [1,2,4,5,6].map(n => {
    const e = s["step"+n];
    if(!e) return `<span class="pill missing">step${n}</span>`;
    const cls = GATE_CLASS[e.gate_action] || "missing";
    return `<span class="pill ${cls}" title="${(e.gate_reasons||[]).join(" · ")}">step${n} · ${e.gate_action||"?"}</span>`;
  }).join("");

  const details = [1,2,4,5,6].map(n => {
    const e = s["step"+n];
    if(!e) return "";
    const cls = GATE_CLASS[e.gate_action] || "";
    const reasons = (e.gate_reasons||[]).slice(0,2).join(" · ");
    const confScore = e.confidence;
    let kv = "";
    if(n===1) kv = `<span>材料 <b>${num(e.materials_n)}</b></span><span>模块 <b>${num(e.modules_n)}</b></span><span>流程 <b>${num(e.flows_n)}</b></span><span>歧义 <b>${num(e.ambiguities_n)}</b></span>`;
    if(n===2){ const a=e.automation||{}; kv = `<span>P0 <b>${num(e.p0_n)}</b></span><span>P1 <b>${num(e.p1_n)}</b></span><span>P2 <b>${num(e.p2_n)}</b></span><span>auto <b>${num(a.auto||0)}</b>/semi <b>${num(a.semi_auto||0)}</b>/手工 <b>${num(a.manual||0)}</b></span>`; }
    if(n===4) kv = `<span>API <b>${num(e.apis_n)}</b></span><span>功能 <b>${num(e.functional_n)}</b></span><span>边界 <b>${num(e.boundary_n)}</b></span><span>安全 <b>${num(e.security_n)}</b></span><span>pass_rate <b>${pct(e.pass_rate)}</b></span>`;
    if(n===5){ const sm=e.severity_map||{}; kv = `<span>基准 <b>${num(e.references_n)}</b></span><span>差异 <b>${num(e.diffs_n)}</b></span><span class="sev">crit <b style="color:var(--reject)">${num(sm.critical)}</b> · high <b style="color:var(--warn)">${num(sm.high)}</b> · med <b>${num(sm.medium)}</b> · low <b>${num(sm.low)}</b></span><span>偏差 <b>${pct(e.deviation)}</b></span>`; }
    if(n===6) kv = `<span>执行步 <b>${num(e.steps_n)}</b></span><span>失败 <b>${num(e.failures_n)}</b></span><span>success <b>${pct(e.success_rate)}</b></span>`;

    const confHtml = confScore!=null ? `<div class="confbar" title="confidence ${confScore.toFixed(2)}"><div style="width:${Math.round(confScore*100)}%"></div></div>` : "";

    return `<div class="stepdetail">
      <h3><span>STEP ${n} · ${STEP_NAMES[n]}</span><span class="right">${e.model||""}</span></h3>
      <div class="kv">${kv}</div>
      ${reasons ? `<div class="reason ${cls}">${reasons}</div>` : ""}
      ${confHtml}
    </div>`;
  }).join("");

  const meta = run.project_id || run.tenant_id ? `<div class="meta">project <b style="color:#c9d3ee">${run.project_id||"?"}</b> · tenant ${run.tenant_id||"?"}</div>` : "";

  return `<div class="card">
    <header>
      <div>
        <h2>RUN</h2>
        <div class="rid">${run.run_id}</div>
        ${meta}
      </div>
    </header>
    <div class="steprow">${stepPills}</div>
    ${details}
    <div class="footer">
      <a href="/runs/${run.run_id}/report.html" target="_blank">HTML 报告 ↗</a>
      <a href="/runs/${run.run_id}/report.md" target="_blank">Markdown ↗</a>
      <a href="/api/runs/${run.run_id}/raw/1" target="_blank" style="color:var(--muted)">step1 JSON</a>
      <a href="/api/runs/${run.run_id}/raw/2" target="_blank" style="color:var(--muted)">2</a>
      <a href="/api/runs/${run.run_id}/raw/4" target="_blank" style="color:var(--muted)">4</a>
      <a href="/api/runs/${run.run_id}/raw/5" target="_blank" style="color:var(--muted)">5</a>
      <a href="/api/runs/${run.run_id}/raw/6" target="_blank" style="color:var(--muted)">6</a>
    </div>
  </div>`;
}

async function load(){
  const res = await fetch("/api/runs");
  const data = await res.json();
  window.__runs = data.runs || [];
  render();
}
function render(){
  const q = (document.getElementById("filter").value||"").toLowerCase();
  const runs = window.__runs.filter(r =>
    !q || (r.run_id||"").toLowerCase().includes(q) || (r.project_id||"").toLowerCase().includes(q)
  );
  document.getElementById("grid").innerHTML = runs.map(cardHtml).join("");
  document.getElementById("count").textContent = `共 ${runs.length} / ${window.__runs.length} 个 run`;
  document.getElementById("empty").style.display = runs.length ? "none" : "block";
}
document.getElementById("filter").addEventListener("input", render);
load();
</script>
</body>
</html>
"""


TDR_WORKBENCH_HTML = r"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8" />
<title>TDR 工作台 — AI Test Toolkit</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@300;400;500;600;700&family=Noto+Sans+SC:wght@300;400;500;600&family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
  :root {
    --bg: #f7f8fb; --panel: #ffffff; --panel2: #f1f3f9;
    --ink: #1f2530; --muted: #6b7488; --border: #e3e6ef;
    --pass: #16a875; --warn: #e29a10; --reject: #dc3851; --info: #3366ff; --accent: #6c4bff;
    --blocker-bg: #fdecee; --major-bg: #fff4e4; --minor-bg: #eef4ff; --suggestion-bg: #eefaf0;
  }
  * { box-sizing: border-box; }
  html, body { background: var(--bg); color: var(--ink); margin: 0; font-family: -apple-system, "PingFang SC", "Segoe UI", Roboto, sans-serif; font-size: 14px; line-height: 1.55; }
  header.topbar { background: #fff; border-bottom: 1px solid var(--border); padding: 14px 28px; display: flex; justify-content: space-between; align-items: center; position: sticky; top: 0; z-index: 10; }
  header.topbar h1 { margin: 0; font-size: 17px; font-weight: 600; }
  header.topbar .sub { color: var(--muted); font-size: 12px; margin-top: 2px; }
  header.topbar nav { display: flex; gap: 4px; }
  header.topbar nav button {
    background: transparent; border: 1px solid transparent; padding: 7px 16px;
    border-radius: 6px; color: var(--muted); cursor: pointer; font-size: 13px; font-weight: 500;
  }
  header.topbar nav button.active { background: var(--panel2); color: var(--ink); }
  header.topbar nav button:hover { color: var(--ink); }
  header.topbar .links a { color: var(--muted); margin-left: 14px; font-size: 12px; text-decoration: none; }
  header.topbar .links a:hover { color: var(--info); }
  main { padding: 24px 28px 80px; max-width: 1400px; margin: 0 auto; }
  h2 { font-size: 16px; margin: 0 0 12px; font-weight: 600; }
  h3 { font-size: 13px; margin: 0 0 8px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: .06em; }
  .tab { display: none; }
  .tab.active { display: block; }

  /* Cards */
  .card { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 20px; margin-bottom: 18px; }
  .row { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
  @media (max-width: 900px) { .row { grid-template-columns: 1fr; } }

  /* Standards viewer */
  .std-nav { display: flex; gap: 6px; margin-bottom: 12px; }
  .std-nav button {
    background: var(--panel); border: 1px solid var(--border); padding: 8px 14px;
    border-radius: 6px; cursor: pointer; font-size: 13px; color: var(--muted);
  }
  .std-nav button.active { background: var(--accent); color: #fff; border-color: var(--accent); }
  .dim-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px; }
  .dim-card { background: var(--panel2); border-radius: 8px; padding: 14px; border-left: 3px solid var(--accent); }
  .dim-card h4 { margin: 0 0 4px; font-size: 14px; display: flex; justify-content: space-between; }
  .dim-card .weight { font-size: 12px; color: var(--muted); font-weight: 400; }
  .dim-card ul { margin: 6px 0 0; padding-left: 18px; font-size: 12px; color: var(--muted); }
  .dim-card li { margin: 2px 0; }
  .kvlist { display: grid; grid-template-columns: 140px 1fr; gap: 6px 14px; font-size: 13px; }
  .kvlist dt { color: var(--muted); }
  .kvlist dd { margin: 0; color: var(--ink); }
  .tag { display: inline-block; background: var(--panel2); border-radius: 4px; padding: 2px 8px; font-size: 11px; margin: 2px 4px 2px 0; color: var(--muted); }
  .tag.blocker { background: var(--blocker-bg); color: var(--reject); }
  .tag.major { background: var(--major-bg); color: var(--warn); }
  .tag.minor { background: var(--minor-bg); color: var(--info); }
  .tag.suggestion { background: var(--suggestion-bg); color: var(--pass); }
  pre.yaml { background: #11161f; color: #d8def0; padding: 14px; border-radius: 8px; font-size: 12px; overflow-x: auto; max-height: 480px; margin: 0; }

  /* Workbench */
  fieldset { border: 1px solid var(--border); border-radius: 8px; padding: 14px 18px 16px; margin: 0 0 14px; background: var(--panel); }
  fieldset legend { padding: 0 6px; font-size: 13px; font-weight: 600; color: var(--accent); }
  label { display: block; margin: 6px 0 4px; font-size: 12px; color: var(--muted); font-weight: 500; }
  input, select, textarea { width: 100%; padding: 7px 10px; border: 1px solid var(--border); border-radius: 6px; font-family: inherit; font-size: 13px; background: #fff; }
  textarea { font-family: ui-monospace, Menlo, monospace; font-size: 12px; min-height: 80px; resize: vertical; }
  button.primary { background: var(--accent); color: #fff; border: none; padding: 10px 22px; border-radius: 6px; font-size: 14px; font-weight: 600; cursor: pointer; }
  button.primary:hover { filter: brightness(1.08); }
  button.ghost { background: var(--panel2); color: var(--ink); border: 1px solid var(--border); padding: 6px 12px; border-radius: 6px; font-size: 12px; cursor: pointer; }
  button.ghost:hover { background: #fff; }
  .score-grid { display: grid; grid-template-columns: 160px repeat(auto-fit, minmax(130px, 1fr)); gap: 8px; align-items: center; margin-bottom: 8px; font-size: 12px; }
  .score-grid input[type=number] { width: 100%; }
  .comment-row { display: grid; grid-template-columns: auto 1fr 1fr 1fr auto; gap: 8px; align-items: start; margin-bottom: 8px; }
  .comment-row textarea { min-height: 50px; }

  /* Result */
  .banner { padding: 14px 18px; border-radius: 8px; margin-top: 14px; font-weight: 500; }
  .banner.pass { background: #dff6eb; color: #0a6b3e; border: 1px solid #a6e2c0; }
  .banner.conditional_pass { background: #fff4db; color: #8a5a06; border: 1px solid #f1d28a; }
  .banner.reject { background: #fce0e4; color: #a21a30; border: 1px solid #ed98a3; }
  .ringbox { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; margin-top: 10px; }
  .ring { background: var(--panel2); border-radius: 8px; padding: 10px 14px; }
  .ring .label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; }
  .ring .val { font-size: 20px; font-weight: 600; margin-top: 2px; }
  .bar { height: 6px; background: var(--border); border-radius: 3px; overflow: hidden; margin-top: 6px; }
  .bar > div { height: 100%; background: linear-gradient(90deg, var(--reject), var(--warn) 50%, var(--pass)); }

  /* Review history table */
  table { width: 100%; border-collapse: collapse; font-size: 13px; background: var(--panel); border-radius: 10px; overflow: hidden; }
  th, td { padding: 10px 14px; text-align: left; border-bottom: 1px solid var(--border); }
  th { background: var(--panel2); font-weight: 600; color: var(--muted); text-transform: uppercase; font-size: 11px; letter-spacing: .05em; }
  tr:last-child td { border-bottom: none; }
  .pill { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 11px; font-weight: 600; }
  .pill.pass { background: #dff6eb; color: var(--pass); }
  .pill.conditional_pass { background: #fff4db; color: var(--warn); }
  .pill.reject { background: #fce0e4; color: var(--reject); }
  details summary { cursor: pointer; user-select: none; color: var(--info); font-size: 12px; }
  details[open] summary { margin-bottom: 6px; }
  .muted { color: var(--muted); font-size: 12px; }
</style>
</head>
<body>
<header class="topbar">
  <div>
    <h1>AI Test Toolkit · <span style="color:var(--accent)">TDR 工作台</span></h1>
    <div class="sub">Test Design Review · 规范 / 工作台 / 历史</div>
  </div>
  <nav id="tabs">
    <button data-tab="spec" class="active">规范 Spec</button>
    <button data-tab="workbench">工作台 Workbench</button>
    <button data-tab="history">评审历史</button>
    <button data-tab="pipeline">Pipeline 报告</button>
  </nav>
  <div class="links">
    <a href="/docs" target="_blank">Swagger</a>
    <a href="/healthz" target="_blank">Health</a>
  </div>
</header>

<main>

<!-- ========= TAB: 规范 ========= -->
<section id="tab-spec" class="tab active">
  <div class="std-nav" id="std-nav"></div>
  <div id="std-body"></div>
</section>

<!-- ========= TAB: 工作台 ========= -->
<section id="tab-workbench" class="tab">
  <div class="card">
    <h2>新建 TDR 评审</h2>
    <p class="muted" style="margin-top:-4px">填写评审基本信息、打分、评论，提交后按 <code>configs/standards/tdr.yaml</code> 的加权规则自动计算维度分、综合分与闸门决策。</p>

    <div class="row" style="margin-top:14px">
      <div>
        <label>Run ID <span class="muted">（可留空自动生成）</span></label>
        <input id="w-run" placeholder="例如 build-2026-04-release" />
      </div>
      <div>
        <label>Project ID</label>
        <input id="w-project" value="demo-tdr" />
      </div>
    </div>

    <fieldset>
      <legend>1 · 评审产物 Artifacts</legend>
      <p class="muted" style="margin-top:-4px">每行一个产物路径或说明。规范要求至少包含：<span id="required-artifacts"></span></p>
      <textarea id="w-artifacts" placeholder="test_strategy_v1.md&#10;coverage_matrix.xlsx&#10;entry_exit_criteria.md"></textarea>
    </fieldset>

    <fieldset>
      <legend>2 · 评审员打分 Scores (0.0–1.0)</legend>
      <div id="w-scores"></div>
      <div style="display:flex;gap:10px;margin-top:8px;">
        <button class="ghost" onclick="addReviewer()">+ 加评审员</button>
        <button class="ghost" onclick="fillSampleScores()">填示例</button>
      </div>
    </fieldset>

    <fieldset>
      <legend>3 · 评论 Comments</legend>
      <div id="w-comments"></div>
      <button class="ghost" onclick="addComment()">+ 加评论</button>
    </fieldset>

    <div style="display:flex;gap:10px;align-items:center;margin-top:6px;">
      <button class="primary" onclick="submitReview()">提交并计算决策</button>
      <span class="muted" id="w-status"></span>
    </div>
  </div>

  <div id="w-result"></div>
</section>

<!-- ========= TAB: 历史 ========= -->
<section id="tab-history" class="tab">
  <h2 style="margin-bottom:14px">已归档 TDR 评审 <span class="muted" id="hist-count"></span></h2>
  <div id="hist-body"></div>
</section>

<!-- ========= TAB: Pipeline ========= -->
<section id="tab-pipeline" class="tab">
  <div class="card">
    <h2>Pipeline 运行报告</h2>
    <p class="muted">5 步流水线的 run 报告（step1 需求拆解 → step2 用例设计 → step4 接口测试 → step5 UI 一致性 → step6 Agent 执行）。</p>
    <p><a href="/pipeline" target="_blank">打开 Pipeline Dashboard →</a></p>
  </div>
</section>

</main>

<script>
// ------------------ tab switching ------------------
document.querySelectorAll("#tabs button").forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll("#tabs button").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
    document.getElementById("tab-" + btn.dataset.tab).classList.add("active");
    if (btn.dataset.tab === "history") loadHistory();
  };
});

// ------------------ standards viewer ------------------
let SPEC = null;
async function loadSpec() {
  const res = await fetch("/api/standards");
  SPEC = await res.json();
  const nav = document.getElementById("std-nav");
  nav.innerHTML = ["tdr","process","quality","data"].map(n =>
    `<button data-s="${n}" class="${n==='tdr'?'active':''}">${n.toUpperCase()}</button>`
  ).join("");
  nav.querySelectorAll("button").forEach(b => b.onclick = () => {
    nav.querySelectorAll("button").forEach(x=>x.classList.remove("active"));
    b.classList.add("active");
    renderSpec(b.dataset.s);
  });
  renderSpec("tdr");

  // populate the required-artifacts hint in workbench
  const req = (SPEC.tdr?.review_artifacts?.required_per_review) || [];
  document.getElementById("required-artifacts").innerHTML =
    req.map(a=>`<span class="tag">${a}</span>`).join("");
}

function renderSpec(which) {
  const S = SPEC[which];
  const body = document.getElementById("std-body");
  if (which === "tdr") {
    const dims = S.review_dimensions || [];
    const roles = S.roles || [];
    const gate = S.gate_rules || {};
    const signing = S.signing || {};
    const comment = S.comment_structure || {};
    const audit = S.audit_trail || {};
    body.innerHTML = `
      <div class="card">
        <h2>TDR 规范 v${S.version||"?"} <span class="muted" style="font-size:12px;margin-left:10px">last_updated ${S.last_updated||"?"}</span></h2>
      </div>

      <div class="card">
        <h3>5 维评审（加权平均）</h3>
        <div class="dim-grid">
          ${dims.map(d => `
            <div class="dim-card">
              <h4><span>${d.name} <span class="muted">· ${d.id}</span></span><span class="weight">权重 ${d.weight}</span></h4>
              <ul>${(d.checkpoints||[]).map(c=>`<li>${c}</li>`).join("")}</ul>
            </div>`).join("")}
        </div>
      </div>

      <div class="row">
        <div class="card">
          <h3>角色 Roles</h3>
          <dl class="kvlist">
            ${roles.map(r=>`
              <dt>${r.name} <span class="muted">(${r.id})</span></dt>
              <dd>
                ${r.min_count?`min ${r.min_count}`:""}${r.count?`count ${r.count}`:""}
                ${r.seniority?`<br><span class="muted">seniority: ${r.seniority}</span>`:""}
                <br><span class="muted">${(r.responsibilities||[]).join("、")}</span>
                ${r.required_expertise?`<br>${r.required_expertise.map(e=>`<span class="tag">${e}</span>`).join("")}`:""}
              </dd>`).join("")}
          </dl>
        </div>

        <div class="card">
          <h3>评论结构 Comment</h3>
          <dl class="kvlist">
            <dt>必填字段</dt>
            <dd>${(comment.required_fields||[]).map(f=>`<span class="tag">${f}</span>`).join("")}</dd>
            <dt>严重度</dt>
            <dd>${(comment.severity_levels||[]).map(s=>`<span class="tag ${s}">${s}</span>`).join("")}</dd>
            <dt>blocker 出口</dt>
            <dd>${comment.blocker_must_resolve_before_exit ? "必须全部 resolved" : "不强制"}</dd>
          </dl>
        </div>
      </div>

      <div class="row">
        <div class="card">
          <h3>闸门规则 Gate</h3>
          <dl class="kvlist">
            <dt>pass_threshold</dt><dd>${gate.pass_threshold}</dd>
            <dt>conditional_pass</dt><dd>${gate.conditional_pass_threshold}</dd>
            <dt>block_if</dt>
            <dd>${(gate.block_if||[]).map(r=>{
              const [k,v] = Object.entries(r)[0];
              return `<div><code>${k}</code> → <b>${v}</b></div>`;
            }).join("")}</dd>
            <dt>条件通过要求</dt>
            <dd class="muted">${JSON.stringify(gate.on_conditional_pass||{})}</dd>
          </dl>
        </div>
        <div class="card">
          <h3>签名 Signing</h3>
          <dl class="kvlist">
            <dt>算法</dt><dd>${signing.algorithm}</dd>
            <dt>要求签名者</dt><dd>${(signing.required_signers||[]).map(x=>`<span class="tag">${x}</span>`).join("")}</dd>
            <dt>不可抵赖</dt><dd>${signing.non_repudiation ? "是（含 artifact_hash + TSA）" : "否"}</dd>
            <dt>归档位置</dt><dd><code>${signing.storage||""}</code></dd>
          </dl>
          <h3 style="margin-top:16px">审计轨迹</h3>
          <div>${(audit.log_events||[]).map(e=>`<span class="tag">${e}</span>`).join("")}</div>
          <div class="muted" style="margin-top:6px">保留年限 ${audit.retention_years||"—"}</div>
        </div>
      </div>

      <div class="card">
        <h3>必备评审产物</h3>
        <div>${(S.review_artifacts?.required_per_review||[]).map(a=>`<span class="tag">${a}</span>`).join("")}</div>
      </div>
    `;
  } else {
    body.innerHTML = `<div class="card">
      <h3>${which.toUpperCase()} 规范 v${S.version||"?"}</h3>
      <pre class="yaml">${JSON.stringify(S, null, 2)}</pre>
    </div>`;
  }
}

// ------------------ workbench ------------------
let REVIEWER_COUNTER = 0;
let COMMENT_COUNTER = 0;

function dimIds() { return (SPEC?.tdr?.review_dimensions || []).map(d => d.id); }

function addReviewer(prefill) {
  REVIEWER_COUNTER++;
  const id = "rev-" + REVIEWER_COUNTER;
  const container = document.getElementById("w-scores");
  const row = document.createElement("div");
  row.className = "score-grid";
  row.dataset.reviewer = id;
  const inputs = dimIds().map(d =>
    `<div><label>${d}</label><input type="number" min="0" max="1" step="0.05" data-dim="${d}" value="${prefill?.[d] ?? ''}" /></div>`
  ).join("");
  row.innerHTML = `<div><input placeholder="评审员名" value="${prefill?.name||('reviewer_'+REVIEWER_COUNTER)}" data-name /></div>${inputs}<button class="ghost" onclick="this.parentElement.remove()" style="grid-column:1/-1;justify-self:end;margin-top:4px">删除</button>`;
  container.appendChild(row);
}

function fillSampleScores() {
  document.getElementById("w-scores").innerHTML = "";
  REVIEWER_COUNTER = 0;
  addReviewer({ name: "alice", completeness: 0.88, correctness: 0.82, feasibility: 0.8, risk_awareness: 0.85, traceability: 0.9 });
  addReviewer({ name: "bob", completeness: 0.87, correctness: 0.76, feasibility: 0.82, risk_awareness: 0.8, traceability: 0.88 });
  if (!document.querySelectorAll("#w-comments .comment-row").length) {
    addComment({ dimension: "correctness", severity: "minor", location: "case TC-001", observation: "断言覆盖不足", suggestion: "增加 response.body.code 断言" });
  }
  if (!document.getElementById("w-artifacts").value) {
    document.getElementById("w-artifacts").value = [
      "test_strategy_v1.md","test_scope_definition.md","risk_analysis.md",
      "coverage_matrix.xlsx","entry_exit_criteria.md","test_schedule.xlsx",
      "resource_plan.md","quality_metrics_baseline.md"
    ].join("\n");
  }
}

function addComment(prefill) {
  COMMENT_COUNTER++;
  const id = prefill?.id || "CMT-" + String(COMMENT_COUNTER).padStart(3,"0");
  const sev = SPEC?.tdr?.comment_structure?.severity_levels || ["blocker","major","minor","suggestion"];
  const dims = dimIds();
  const row = document.createElement("div");
  row.className = "comment-row";
  row.innerHTML = `
    <input style="width:80px" value="${id}" data-f="id" />
    <select data-f="dimension">${dims.map(d=>`<option ${d===prefill?.dimension?"selected":""}>${d}</option>`).join("")}</select>
    <select data-f="severity">${sev.map(s=>`<option ${s===(prefill?.severity||'minor')?"selected":""}>${s}</option>`).join("")}</select>
    <input placeholder="location" data-f="location" value="${prefill?.location||''}" />
    <button class="ghost" onclick="this.parentElement.remove()">×</button>
    <textarea style="grid-column:1/-1" placeholder="observation · 建议分两行" data-f="body">${prefill? (prefill.observation||'')+"\n"+(prefill.suggestion||'') : ''}</textarea>
  `;
  document.getElementById("w-comments").appendChild(row);
}

function collectScores() {
  const out = [];
  document.querySelectorAll("#w-scores .score-grid").forEach(row => {
    const name = row.querySelector("[data-name]").value.trim() || "anon";
    const scores = {};
    row.querySelectorAll("input[data-dim]").forEach(i => {
      const v = parseFloat(i.value);
      if (!Number.isNaN(v)) scores[i.dataset.dim] = v;
    });
    if (Object.keys(scores).length) out.push({ [name]: scores });
  });
  return out;
}
function collectComments() {
  const out = [];
  document.querySelectorAll("#w-comments .comment-row").forEach(row => {
    const body = (row.querySelector("[data-f=body]").value || "").split("\n");
    out.push({
      id: row.querySelector("[data-f=id]").value.trim(),
      dimension: row.querySelector("[data-f=dimension]").value,
      severity: row.querySelector("[data-f=severity]").value,
      location: row.querySelector("[data-f=location]").value.trim(),
      observation: (body[0]||"").trim(),
      suggestion: (body.slice(1).join("\n")||"").trim(),
      requires_response: true,
    });
  });
  return out;
}

async function submitReview() {
  const artifacts = document.getElementById("w-artifacts").value.split("\n").map(s=>s.trim()).filter(Boolean);
  const scores = collectScores();
  const comments = collectComments();
  const payload = {
    run_id: document.getElementById("w-run").value.trim() || null,
    project_id: document.getElementById("w-project").value.trim() || "default",
    tenant_id: "default",
    artifacts, reviewer_scores: scores, comments
  };
  document.getElementById("w-status").textContent = "提交中…";
  const res = await fetch("/api/tdr/submit", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  document.getElementById("w-status").textContent = "";
  if (!res.ok) {
    const err = await res.text();
    document.getElementById("w-result").innerHTML = `<div class="card banner reject">提交失败: ${err}</div>`;
    return;
  }
  const data = await res.json();
  renderResult(data);
}

// XSS 防御：所有插入 innerHTML 的动态文本都用 escapeHtml() 包一下
function escapeHtml(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
const ALLOWED_SEVERITY = new Set(["info", "minor", "major", "blocker"]);
const ALLOWED_DECISION = new Set(["pass", "conditional_pass", "reject"]);
function safeSeverity(s) { return ALLOWED_SEVERITY.has(s) ? s : "info"; }
function safeDecision(s) { return ALLOWED_DECISION.has(s) ? s : "reject"; }

function renderResult(data) {
  const r = data.report;
  const dec = safeDecision(r.decision);
  const dims = r.dimension_scores || {};
  const dimCards = Object.entries(dims).map(([k,v]) => `
    <div class="ring"><div class="label">${escapeHtml(k)}</div><div class="val">${(v*100).toFixed(0)}%</div>
    <div class="bar"><div style="width:${Math.round(v*100)}%"></div></div></div>`).join("");
  const cmts = (r.comments||[]).map(c=>`
    <div style="padding:8px 10px;border-left:3px solid var(--border);margin-bottom:6px">
      <div><span class="tag ${safeSeverity(c.severity)}">${escapeHtml(c.severity)}</span> <b>${escapeHtml(c.id)}</b> · <span class="muted">${escapeHtml(c.dimension)} @ ${escapeHtml(c.location)}</span></div>
      <div style="margin-top:4px">${escapeHtml(c.observation)}</div>
      ${c.suggestion?`<div class="muted" style="margin-top:2px">→ ${escapeHtml(c.suggestion)}</div>`:""}
    </div>`).join("");
  const followups = (r.follow_up_items||[]).map(f=>`<li>${escapeHtml(f)}</li>`).join("");
  document.getElementById("w-result").innerHTML = `
    <div class="card">
      <div class="banner ${dec}">闸门决策: <b>${escapeHtml(dec.toUpperCase())}</b> · 综合分 ${(r.overall_score*100).toFixed(1)}%</div>
      <div class="ringbox">${dimCards}
        <div class="ring"><div class="label">评论数</div><div class="val">${(r.comments||[]).length}</div></div>
        <div class="ring"><div class="label">签名数</div><div class="val">${(r.signatures||[]).length}</div></div>
        <div class="ring"><div class="label">产物数</div><div class="val">${(r.artifacts_reviewed||[]).length}</div></div>
      </div>
      ${cmts ? `<h3 style="margin-top:18px">评论</h3>${cmts}` : ""}
      ${followups ? `<h3 style="margin-top:18px">Follow-up</h3><ul>${followups}</ul>` : ""}
      <details style="margin-top:14px"><summary>原始 JSON (持久化到 ${escapeHtml(data.path)})</summary>
        <pre class="yaml">${escapeHtml(JSON.stringify(r, null, 2))}</pre></details>
    </div>`;
}

// ------------------ history ------------------
async function loadHistory() {
  const res = await fetch("/api/tdr/reviews");
  const data = await res.json();
  const reviews = data.reviews || [];
  document.getElementById("hist-count").textContent = `· 共 ${reviews.length} 条`;
  if (!reviews.length) {
    document.getElementById("hist-body").innerHTML = `<div class="card muted">尚无归档评审</div>`;
    return;
  }
  const rows = reviews.map(r => {
    const dims = r.dimension_scores || {};
    const dimBar = Object.entries(dims).slice(0,5).map(([k,v]) =>
      `<span class="tag">${escapeHtml(k)} ${(v*100).toFixed(0)}</span>`
    ).join("");
    const dec = safeDecision(r.decision);
    const ridSafe = encodeURIComponent(r.run_id || "");
    return `<tr>
      <td><code>${escapeHtml(r.run_id)}</code></td>
      <td><span class="pill ${dec}">${escapeHtml(r.decision||"?")}</span></td>
      <td><b>${r.overall_score!=null? (r.overall_score*100).toFixed(1)+"%":"—"}</b></td>
      <td>${dimBar}</td>
      <td>${r.artifacts_n}</td>
      <td>${r.comments_n} <span class="muted">(${r.comments_blocker} blocker/major)</span></td>
      <td>${r.signatures_n}</td>
      <td>${r.follow_up_n}</td>
      <td><a href="/api/tdr/reviews/${ridSafe}" target="_blank">JSON ↗</a></td>
    </tr>`;
  }).join("");
  document.getElementById("hist-body").innerHTML = `<table>
    <tr><th>Run ID</th><th>Decision</th><th>Overall</th><th>维度</th><th>Artifacts</th><th>Comments</th><th>Sigs</th><th>Follow-up</th><th></th></tr>
    ${rows}
  </table>`;
}

// boot
loadSpec();
</script>
</body>
</html>
"""


QMS_PORTAL_HTML = r"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8" />
<title>质量体系 · Quality Management System</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@300;400;500;600;700&family=Noto+Sans+SC:wght@300;400;500;600&family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
  :root {
    --bg: #f6f7fb; --panel: #ffffff; --panel2: #f1f3f9; --panel3: #e8ecf5;
    --ink: #1f2530; --ink2: #3b4352; --muted: #6b7488; --border: #e3e6ef;
    --pass: #16a875; --warn: #e29a10; --reject: #dc3851; --info: #3366ff;
    --accent: #6c4bff; --accent2: #2a7bff; --accent3: #00a3a3;
    --ai: #6c4bff; --human: #575f6f;
    --heat1: #eef2ff; --heat2: #c5d2ff; --heat3: #8ba4ff; --heat4: #5a7bff; --heat5: #3355e6;
    --r-bg: #ffe4e7; --r-ink: #b01028; --a-bg: #fff4db; --a-ink: #8a5a06;
    --c-bg: #dbeafe; --c-ink: #1e4a9a; --i-bg: #eef0f6; --i-ink: #5a6277;
  }
  * { box-sizing: border-box; }
  html, body { background: var(--bg); color: var(--ink); margin: 0;
    font-family: -apple-system, "PingFang SC", "Segoe UI", Roboto, sans-serif;
    font-size: 14px; line-height: 1.55; }
  a { color: var(--info); text-decoration: none; }
  a:hover { text-decoration: underline; }

  header.topbar {
    background: linear-gradient(180deg, #ffffff 0%, #fbfcff 100%);
    border-bottom: 1px solid var(--border);
    padding: 14px 28px; display: flex; justify-content: space-between; align-items: center;
    position: sticky; top: 0; z-index: 100;
    box-shadow: 0 1px 0 rgba(0,0,0,.02);
  }
  header.topbar .brand h1 { margin: 0; font-size: 18px; font-weight: 700; letter-spacing: .01em; }
  header.topbar .brand .sub { color: var(--muted); font-size: 12px; margin-top: 2px; }
  header.topbar .brand .badge {
    display: inline-block; background: var(--accent); color: #fff; border-radius: 4px;
    font-size: 10px; padding: 2px 7px; margin-left: 8px; font-weight: 600; letter-spacing: .04em; vertical-align: middle;
  }
  header.topbar nav.pillars { display: flex; gap: 6px; }
  header.topbar nav.pillars button {
    background: transparent; border: 1px solid transparent; padding: 9px 22px;
    border-radius: 8px; color: var(--muted); cursor: pointer;
    font-size: 14px; font-weight: 600; letter-spacing: .02em;
  }
  header.topbar nav.pillars button.active {
    background: var(--panel2); color: var(--accent); border-color: var(--border);
    box-shadow: 0 1px 2px rgba(108,75,255,.08);
  }
  header.topbar nav.pillars button:hover { color: var(--ink); }
  header.topbar .links { display: flex; gap: 14px; align-items: center; }
  header.topbar .links a { color: var(--muted); font-size: 12px; }
  header.topbar .links a:hover { color: var(--info); }

  main { padding: 24px 28px 80px; max-width: 1500px; margin: 0 auto; }
  .pillar { display: none; }
  .pillar.active { display: block; }

  /* Meta banner */
  .meta-banner {
    display: flex; gap: 28px; align-items: center; padding: 14px 22px;
    background: var(--panel); border: 1px solid var(--border); border-radius: 10px; margin-bottom: 20px;
  }
  .meta-banner .kv { font-size: 12px; color: var(--muted); }
  .meta-banner .kv b { color: var(--ink); font-weight: 600; margin-left: 4px; }

  h2 { font-size: 17px; margin: 0 0 12px; font-weight: 700; letter-spacing: .01em; }
  h2 .cnt { font-weight: 400; color: var(--muted); font-size: 13px; margin-left: 8px; }
  h3 { font-size: 12px; margin: 0 0 10px; font-weight: 700; color: var(--muted);
       text-transform: uppercase; letter-spacing: .08em; }
  h4 { font-size: 14px; margin: 0 0 6px; font-weight: 600; }

  .card { background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
          padding: 20px; margin-bottom: 18px; }
  .card.sub { padding: 16px; }

  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
  .grid3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; }
  .grid4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; }
  @media (max-width: 1100px) { .grid3, .grid4 { grid-template-columns: repeat(2, 1fr); } }
  @media (max-width: 720px)  { .grid2, .grid3, .grid4 { grid-template-columns: 1fr; } }

  /* Sub-nav inside Standards pillar */
  .sub-nav { display: flex; gap: 6px; margin-bottom: 14px; }
  .sub-nav button {
    background: var(--panel); border: 1px solid var(--border); padding: 8px 18px;
    border-radius: 8px; cursor: pointer; font-size: 13px; color: var(--muted); font-weight: 500;
  }
  .sub-nav button.active { background: var(--accent); color: #fff; border-color: var(--accent); }
  .sub-tab { display: none; }
  .sub-tab.active { display: block; }

  /* Tags, pills, badges */
  .tag { display: inline-block; background: var(--panel2); border-radius: 4px;
         padding: 2px 8px; font-size: 11px; margin: 2px 4px 2px 0; color: var(--ink2); font-family: ui-monospace, Menlo, monospace; }
  .tag.ai    { background: #ebe5ff; color: var(--ai); }
  .tag.human { background: #e2e5ed; color: var(--human); }
  .tag.skip  { background: #fff1e0; color: #a25d00; }
  .tag.blocker { background: #fde1e5; color: #b01028; }
  .tag.critical { background: #fde1e5; color: #b01028; }
  .tag.high    { background: #ffe0d8; color: #a33b0b; }
  .tag.medium  { background: #fff4db; color: #8a5a06; }
  .tag.low     { background: #dff6eb; color: #0a6b3e; }
  .tag.p0      { background: #fde1e5; color: #b01028; font-weight: 700; }
  .tag.p1      { background: #ffe4cc; color: #a34400; font-weight: 700; }
  .tag.p2      { background: #e7eeff; color: #2a7bff; font-weight: 700; }

  .mode-pill {
    display: inline-block; padding: 2px 10px; border-radius: 999px;
    font-size: 10px; font-weight: 700; letter-spacing: .05em; text-transform: uppercase;
  }
  .mode-pill.ai { background: #ebe5ff; color: var(--ai); }
  .mode-pill.human { background: #e2e5ed; color: var(--human); }

  /* SOP step cards */
  .sop-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
  @media (max-width: 1100px) { .sop-grid { grid-template-columns: repeat(2, 1fr); } }
  @media (max-width: 600px)  { .sop-grid { grid-template-columns: 1fr; } }
  .sop-card {
    background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
    padding: 14px; position: relative; transition: transform .15s;
  }
  .sop-card:hover { transform: translateY(-1px); box-shadow: 0 4px 12px rgba(0,0,0,.05); }
  .sop-card.skipped { opacity: .65; background: #fafafa; }
  .sop-card .idx {
    position: absolute; top: -10px; left: 14px;
    background: var(--accent); color: #fff; border-radius: 999px;
    width: 28px; height: 28px; text-align: center; line-height: 28px;
    font-size: 12px; font-weight: 700;
  }
  .sop-card.skipped .idx { background: var(--muted); }
  .sop-card h4 { margin: 10px 0 4px; font-size: 13px; }
  .sop-card .mode-line { margin-top: 4px; }
  .sop-card .io { margin-top: 8px; font-size: 11px; }
  .sop-card .io b { color: var(--muted); text-transform: uppercase; letter-spacing: .05em; font-size: 10px; }
  .sop-card .conf { margin-top: 6px; font-size: 11px; color: var(--muted); }
  .sop-card .conf b { color: var(--info); }

  /* Gate cards */
  .gate-list .gate-card {
    padding: 14px 16px; margin-bottom: 10px; border-radius: 8px;
    background: var(--panel2); border-left: 3px solid var(--warn);
  }
  .gate-list .gate-card .gid { font-weight: 700; font-family: ui-monospace, Menlo, monospace; font-size: 12px; color: var(--accent); }
  .gate-list .gate-card .desc { margin-top: 4px; font-size: 13px; }
  .gate-list .gate-card .cond { margin-top: 8px; font-size: 12px; color: var(--muted); }
  .gate-list .gate-card .blockers { margin-top: 6px; }

  /* Boundary column layout */
  .boundary-cols { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; }
  @media (max-width: 900px) { .boundary-cols { grid-template-columns: 1fr; } }
  .boundary-col { background: var(--panel2); border-radius: 8px; padding: 14px; }
  .boundary-col h4 { font-size: 13px; margin-bottom: 8px; }
  .boundary-col.ai h4    { color: var(--ai); }
  .boundary-col.mixed h4 { color: var(--warn); }
  .boundary-col.human h4 { color: var(--human); }
  .boundary-col ul { margin: 6px 0 0; padding-left: 18px; font-size: 12px; color: var(--ink2); }
  .boundary-col li { margin: 3px 0; }

  /* Degradation chain */
  .chain { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .chain .node {
    background: var(--panel2); border: 1px solid var(--border); border-radius: 8px;
    padding: 10px 14px; min-width: 180px;
  }
  .chain .node .lv { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; }
  .chain .node .name { font-weight: 600; margin-top: 2px; }
  .chain .node .trig { font-size: 11px; color: var(--muted); margin-top: 4px; }
  .chain .arrow { color: var(--muted); font-size: 16px; }

  /* Exceptions */
  table { width: 100%; border-collapse: collapse; font-size: 13px;
          background: var(--panel); border-radius: 10px; overflow: hidden; }
  th, td { padding: 10px 14px; text-align: left; border-bottom: 1px solid var(--border); vertical-align: top; }
  th { background: var(--panel2); font-weight: 600; color: var(--muted);
       text-transform: uppercase; font-size: 11px; letter-spacing: .06em; }
  tr:last-child td { border-bottom: none; }
  td.code { font-family: ui-monospace, Menlo, monospace; font-size: 12px; color: var(--accent); font-weight: 600; }
  td.muted { color: var(--muted); }

  /* TDR dimension cards */
  .dim-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; }
  .dim-card { background: var(--panel2); border-radius: 8px; padding: 14px; border-left: 3px solid var(--accent); }
  .dim-card h4 { display: flex; justify-content: space-between; align-items: baseline; }
  .dim-card .weight { font-weight: 400; color: var(--muted); font-size: 12px; }
  .dim-card ul { margin: 6px 0 0; padding-left: 18px; font-size: 12px; color: var(--ink2); }

  /* Role cards */
  .role-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 14px; }
  @media (max-width: 900px) { .role-grid { grid-template-columns: 1fr; } }
  .role-card {
    background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 16px;
    display: flex; flex-direction: column; gap: 8px;
  }
  .role-card .role-head {
    display: flex; justify-content: space-between; align-items: baseline; gap: 8px;
  }
  .role-card .role-head h4 { font-size: 15px; font-weight: 700; margin: 0; }
  .role-card .role-head .lvl { font-family: ui-monospace, Menlo, monospace; font-size: 11px; color: var(--muted); }
  .role-card .mission { font-size: 13px; color: var(--ink2); font-style: italic; }
  .role-card .role-foot { display: flex; justify-content: space-between; align-items: center; font-size: 11px; color: var(--muted); margin-top: 4px; padding-top: 8px; border-top: 1px dashed var(--border); }
  .role-card .tdr-map { display: inline-block; padding: 1px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; background: #ebe5ff; color: var(--accent); }
  .role-card ul { margin: 0; padding-left: 18px; font-size: 12px; color: var(--ink2); }
  .role-card ul li { margin: 2px 0; }

  /* Competency heat matrix */
  .heat-wrap { overflow-x: auto; }
  table.heat { min-width: 820px; }
  table.heat th, table.heat td { text-align: center; padding: 10px 8px; }
  table.heat th.dim { min-width: 100px; font-size: 11px; }
  table.heat td.role-name { text-align: left; font-weight: 600; white-space: nowrap; }
  .heat-cell {
    display: inline-block; min-width: 38px; padding: 4px 10px;
    border-radius: 5px; font-family: ui-monospace, Menlo, monospace;
    font-size: 12px; font-weight: 700;
  }
  .heat-cell.L1 { background: var(--heat1); color: #3a4670; }
  .heat-cell.L2 { background: var(--heat2); color: #1e2d5e; }
  .heat-cell.L3 { background: var(--heat3); color: #fff; }
  .heat-cell.L4 { background: var(--heat4); color: #fff; }
  .heat-cell.L5 { background: var(--heat5); color: #fff; }

  /* RACI matrix */
  table.raci th, table.raci td { text-align: center; padding: 8px 6px; vertical-align: middle; }
  table.raci td.step { text-align: left; font-weight: 600; white-space: nowrap; padding-left: 14px; }
  .raci-cell {
    display: inline-flex; gap: 2px; flex-wrap: wrap; justify-content: center;
  }
  .raci-chip {
    display: inline-block; width: 22px; height: 22px; line-height: 22px; text-align: center;
    border-radius: 4px; font-size: 11px; font-weight: 700;
  }
  .raci-chip.R { background: var(--r-bg); color: var(--r-ink); }
  .raci-chip.A { background: var(--a-bg); color: var(--a-ink); }
  .raci-chip.C { background: var(--c-bg); color: var(--c-ink); }
  .raci-chip.I { background: var(--i-bg); color: var(--i-ink); }
  .raci-legend { font-size: 12px; color: var(--muted); margin-bottom: 10px; }
  .raci-legend .raci-chip { margin-right: 4px; vertical-align: middle; }

  /* Career ladder */
  .ladder { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; position: relative; }
  @media (max-width: 1000px) { .ladder { grid-template-columns: repeat(2, 1fr); } }
  .rung {
    background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 14px;
    display: flex; flex-direction: column; gap: 6px;
  }
  .rung .lv-badge {
    display: inline-block; background: var(--accent); color: #fff;
    font-family: ui-monospace, Menlo, monospace; font-size: 11px; font-weight: 700;
    padding: 2px 10px; border-radius: 5px; align-self: flex-start;
  }
  .rung .lv-badge.L1 { background: #94a3c9; }
  .rung .lv-badge.L2 { background: #6d83b7; }
  .rung .lv-badge.L3 { background: var(--accent); }
  .rung .lv-badge.L4 { background: #4a2ed4; }
  .rung .lv-badge.L5 { background: #2c1a8f; }
  .rung h4 { font-size: 14px; margin: 0; }
  .rung .tenure { font-size: 11px; color: var(--muted); font-family: ui-monospace, Menlo, monospace; }
  .rung ul { margin: 6px 0 0; padding-left: 18px; font-size: 12px; color: var(--ink2); }
  .rung ul li { margin: 2px 0; }

  /* Onboarding timeline */
  .timeline-cols { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
  @media (max-width: 900px) { .timeline-cols { grid-template-columns: repeat(2, 1fr); } }
  .tl-col {
    background: var(--panel2); border-radius: 8px; padding: 14px;
    border-top: 3px solid var(--accent2);
  }
  .tl-col h4 { font-size: 13px; margin-bottom: 8px; }
  .tl-col ul { margin: 0; padding-left: 18px; font-size: 12px; color: var(--ink2); }
  .tl-col ul li { margin: 3px 0; }

  /* Health metrics */
  .kv-card { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 14px; }
  .kv-card .kv-title { font-weight: 600; margin-bottom: 4px; }
  .kv-card .kv-desc { font-size: 12px; color: var(--muted); }
  .kv-card .kv-target { margin-top: 8px; font-size: 12px; }
  .kv-card .kv-target b { font-family: ui-monospace, Menlo, monospace; color: var(--pass); }
  .kv-card .kv-warn b { font-family: ui-monospace, Menlo, monospace; color: var(--warn); }

  /* kvlist */
  .kvlist { display: grid; grid-template-columns: 180px 1fr; gap: 6px 14px; font-size: 13px; margin: 0; }
  .kvlist dt { color: var(--muted); font-size: 12px; }
  .kvlist dd { margin: 0; color: var(--ink); font-size: 13px; }

  /* skeleton */
  .loading { color: var(--muted); font-style: italic; padding: 20px 0; text-align: center; }
  .err { color: var(--reject); padding: 12px; background: #fce0e4; border-radius: 8px; }

  /* Accuracy table */
  .acc-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
  @media (max-width: 900px) { .acc-grid { grid-template-columns: 1fr; } }
  .acc-card { background: var(--panel2); border-radius: 8px; padding: 12px; border-left: 3px solid var(--info); }
  .acc-card .step { font-weight: 700; font-family: ui-monospace, Menlo, monospace; font-size: 12px; color: var(--accent); }
  .acc-card .metric { font-size: 12px; color: var(--muted); margin-top: 4px; }
  .acc-card .metric b { color: var(--pass); font-family: ui-monospace, Menlo, monospace; }

  /* Confidence gradient */
  .conf-bar {
    display: flex; border-radius: 8px; overflow: hidden; margin: 10px 0;
    font-size: 12px; font-weight: 600; color: #fff; text-align: center;
  }
  .conf-bar div { padding: 10px 0; }
  .conf-bar .auto   { background: var(--pass); flex: 10; }
  .conf-bar .suggest{ background: var(--warn); flex: 20; }
  .conf-bar .mand   { background: var(--reject); flex: 70; }
</style>
</head>
<body>
<header class="topbar">
  <div class="brand">
    <h1>质量体系 <span class="badge">QMS</span></h1>
    <div class="sub">Quality Management System · 流程 · 标准 · 团队建设</div>
  </div>
  <nav class="pillars" id="pillars">
    <button data-pillar="process" class="active">流程 Process</button>
    <button data-pillar="standards">标准 Standards</button>
    <button data-pillar="team">团队建设 Team</button>
  </nav>
  <div class="links">
    <a href="/tdr-ui">TDR 工作台 →</a>
    <a href="/pipeline">Pipeline 报告 →</a>
    <a href="/runs">运行列表 →</a>
    <a href="/docs">API</a>
  </div>
</header>

<main>
  <!-- ======================================================== -->
  <!-- 流程 PROCESS -->
  <!-- ======================================================== -->
  <section class="pillar active" id="pillar-process">
    <div id="process-body" class="loading">加载中…</div>
  </section>

  <!-- ======================================================== -->
  <!-- 标准 STANDARDS -->
  <!-- ======================================================== -->
  <section class="pillar" id="pillar-standards">
    <div class="sub-nav" id="std-nav">
      <button data-std="tdr" class="active">TDR 评审标准</button>
      <button data-std="quality">质量标准</button>
      <button data-std="data">数据标准</button>
    </div>
    <div class="sub-tab active" id="std-tdr"><div class="loading">加载中…</div></div>
    <div class="sub-tab" id="std-quality"><div class="loading">加载中…</div></div>
    <div class="sub-tab" id="std-data"><div class="loading">加载中…</div></div>
  </section>

  <!-- ======================================================== -->
  <!-- 团队建设 TEAM -->
  <!-- ======================================================== -->
  <section class="pillar" id="pillar-team">
    <div id="team-body" class="loading">加载中…</div>
  </section>
</main>

<script>
/* ============================================================
 * Boot: fetch all 5 standards, render each pillar lazily.
 * ============================================================ */
let STD = null;
const esc = (s) => String(s == null ? "" : s)
  .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;").replaceAll("'", "&#39;");
const tag = (s, cls="") => `<span class="tag ${cls}">${esc(s)}</span>`;
const tags = (arr, cls="") => (arr || []).map(x => tag(x, cls)).join("");

async function boot() {
  try {
    const res = await fetch("/api/standards");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    STD = await res.json();
  } catch (err) {
    document.querySelectorAll(".pillar .loading").forEach(el => {
      el.outerHTML = `<div class="err">加载标准失败：${esc(err.message)}</div>`;
    });
    return;
  }
  renderProcess();
  renderStandards();
  renderTeam();
}

/* Pillar tabs */
document.getElementById("pillars").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-pillar]");
  if (!btn) return;
  document.querySelectorAll("#pillars button").forEach(b => b.classList.remove("active"));
  document.querySelectorAll(".pillar").forEach(p => p.classList.remove("active"));
  btn.classList.add("active");
  document.getElementById("pillar-" + btn.dataset.pillar).classList.add("active");
});

/* Standards sub-tabs */
document.getElementById("std-nav").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-std]");
  if (!btn) return;
  document.querySelectorAll("#std-nav button").forEach(b => b.classList.remove("active"));
  document.querySelectorAll("#pillar-standards .sub-tab").forEach(t => t.classList.remove("active"));
  btn.classList.add("active");
  document.getElementById("std-" + btn.dataset.std).classList.add("active");
});

/* ============================================================
 * 流程 PROCESS
 * ============================================================ */
function renderProcess() {
  const p = STD.process || {};
  const sop = p.sop || [];
  const gates = p.gate_rules || [];
  const hab = p.human_ai_boundary || {};
  const chain = p.degradation_chain || [];
  const exc = p.exception_handling || {};
  const ver = p.version_traceability || [];

  // SOP cards
  const sopHTML = sop.map((s, i) => {
    const skipped = s.skipped_in_toolkit === true;
    return `
    <div class="sop-card ${skipped ? "skipped" : ""}">
      <div class="idx">${s.id ? s.id.replace(/^step/, "") : i+1}</div>
      <h4>${esc(s.name || s.id || "")}</h4>
      <div class="mode-line">
        <span class="mode-pill ${s.mode === 'ai' ? 'ai' : 'human'}">${esc((s.mode || "").toUpperCase())}</span>
        ${skipped ? `<span class="tag skip">本工具集跳过</span>` : ""}
      </div>
      ${s.inputs ? `<div class="io"><b>IN</b><br>${tags(s.inputs)}</div>` : ""}
      ${s.outputs ? `<div class="io"><b>OUT</b><br>${tags(s.outputs)}</div>` : ""}
      ${s.confidence_threshold != null ? `<div class="conf">confidence ≥ <b>${esc(s.confidence_threshold)}</b></div>` : ""}
    </div>`;
  }).join("");

  // Gates
  const gateHTML = gates.map(g => `
    <div class="gate-card">
      <div class="gid">${esc(g.id || "")}</div>
      <div class="desc">${esc(g.description || "")}</div>
      ${g.condition ? `<div class="cond"><b>触发：</b>${esc(g.condition)}</div>` : ""}
      ${g.threshold != null ? `<div class="cond"><b>阈值：</b>${esc(JSON.stringify(g.threshold))}</div>` : ""}
      ${g.blocker_if ? `<div class="blockers"><b>blocker 条件：</b>${tags(g.blocker_if, "blocker")}</div>` : ""}
    </div>`).join("");

  // Human-AI boundary
  const boundaryHTML = `
    <div class="boundary-cols">
      <div class="boundary-col ai"><h4>AI 独立完成</h4><ul>${
        (hab.ai_exclusive || []).map(x => `<li>${esc(x)}</li>`).join("")
      }</ul></div>
      <div class="boundary-col mixed"><h4>AI + 人工确认</h4><ul>${
        (hab.ai_with_confirmation || []).map(x => `<li>${esc(x)}</li>`).join("")
      }</ul></div>
      <div class="boundary-col human"><h4>人工主导</h4><ul>${
        (hab.human_exclusive || []).map(x => `<li>${esc(x)}</li>`).join("")
      }</ul></div>
    </div>`;

  // Degradation chain
  const chainHTML = `<div class="chain">${
    chain.map((c, i) => `
      <div class="node">
        <div class="lv">Level ${c.level ?? (i+1)}</div>
        <div class="name">${esc(c.model || c.name || "")}</div>
        ${c.trigger ? `<div class="trig">trigger: ${esc(c.trigger)}</div>` : ""}
        ${c.action ? `<div class="trig">${esc(c.action)}</div>` : ""}
      </div>
      ${i < chain.length - 1 ? '<div class="arrow">→</div>' : ""}
    `).join("")
  }</div>`;

  // Exception table
  const excRows = Object.entries(exc).map(([k, v]) => {
    if (typeof v === "object" && v !== null) {
      return `<tr>
        <td class="code">${esc(k)}</td>
        <td>${Object.entries(v).map(([k2, v2]) =>
          `<div><b>${esc(k2)}:</b> <span class="muted">${esc(Array.isArray(v2) ? v2.join(", ") : v2)}</span></div>`
        ).join("")}</td>
      </tr>`;
    }
    return `<tr><td class="code">${esc(k)}</td><td>${esc(v)}</td></tr>`;
  }).join("");

  const html = `
    <div class="meta-banner">
      <div class="kv">标准：<b>流程 Process</b></div>
      <div class="kv">版本：<b>${esc(p.version || "—")}</b></div>
      <div class="kv">Owner：<b>${esc(p.owner || "—")}</b></div>
      <div class="kv">更新于：<b>${esc(p.last_updated || "—")}</b></div>
    </div>

    <div class="card">
      <h2>SOP 八步流水线 <span class="cnt">${sop.length} 步</span></h2>
      <div class="sop-grid">${sopHTML}</div>
    </div>

    <div class="card">
      <h2>闸门规则 Gate Rules <span class="cnt">${gates.length} 条</span></h2>
      <div class="gate-list">${gateHTML}</div>
    </div>

    <div class="card">
      <h2>人机边界 Human-AI Boundary</h2>
      ${boundaryHTML}
    </div>

    <div class="grid2">
      <div class="card">
        <h2>降级链路 Degradation Chain</h2>
        ${chainHTML}
      </div>
      <div class="card">
        <h2>异常处理 Exception Handling</h2>
        <table><thead><tr><th>类型</th><th>策略</th></tr></thead>
          <tbody>${excRows}</tbody></table>
      </div>
    </div>

    <div class="card">
      <h2>版本可追溯 Version Traceability</h2>
      <div style="font-size:12px;color:var(--muted);margin-bottom:8px;">每次运行必须归档以下字段，确保审计可回放：</div>
      <div>${tags(ver)}</div>
    </div>`;
  document.getElementById("process-body").innerHTML = html;
}

/* ============================================================
 * 标准 STANDARDS — TDR / Quality / Data
 * ============================================================ */
function renderStandards() {
  renderStdTdr();
  renderStdQuality();
  renderStdData();
}

function renderStdTdr() {
  const t = STD.tdr || {};
  const dims = t.review_dimensions || [];
  const roles = t.roles || {};
  const cmt = t.comment_structure || {};
  const gate = t.gate_rules || {};
  const sign = t.signing || {};
  const audit = t.audit_trail || {};

  const dimHTML = dims.map(d => `
    <div class="dim-card">
      <h4>${esc(d.name || d.id || "")} <span class="weight">权重 ${esc(d.weight)}</span></h4>
      <div style="font-size:12px;color:var(--muted);">${esc(d.description || "")}</div>
      ${d.checklist ? `<ul>${d.checklist.map(c => `<li>${esc(c)}</li>`).join("")}</ul>` : ""}
    </div>`).join("");

  const rolesHTML = Object.entries(roles).map(([k, v]) => `
    <div class="kv-card">
      <div class="kv-title">${esc(k)}</div>
      <div class="kv-desc">${esc(v.description || "")}</div>
      ${v.min_count != null ? `<div class="kv-target">最少 <b>${esc(v.min_count)}</b> 人</div>` : ""}
      ${v.required_expertise ? `<div style="margin-top:6px;">${tags(v.required_expertise)}</div>` : ""}
      ${v.seniority ? `<div style="margin-top:4px;font-size:11px;color:var(--muted);">资历：${esc(v.seniority)}</div>` : ""}
    </div>`).join("");

  const cmtHTML = `
    <div class="grid2">
      <div>
        <h3>Severity</h3>
        ${Object.entries(cmt.severity || {}).map(([k, v]) =>
          `<div style="margin:4px 0;">${tag(k, k.toLowerCase())}<span class="muted" style="font-size:12px;">${esc(v)}</span></div>`
        ).join("")}
      </div>
      <div>
        <h3>必填字段</h3>
        ${tags(cmt.required_fields || [])}
        ${cmt.resolution_states ? `<h3 style="margin-top:12px;">状态迁移</h3>${tags(cmt.resolution_states)}` : ""}
      </div>
    </div>`;

  const gateHTML = `
    <dl class="kvlist">
      ${Object.entries(gate).map(([k, v]) =>
        `<dt>${esc(k)}</dt><dd>${esc(Array.isArray(v) ? v.join(", ") : (typeof v === "object" ? JSON.stringify(v) : v))}</dd>`
      ).join("")}
    </dl>`;

  const signHTML = `
    <dl class="kvlist">
      ${Object.entries(sign).map(([k, v]) =>
        `<dt>${esc(k)}</dt><dd>${esc(Array.isArray(v) ? v.join(", ") : v)}</dd>`
      ).join("")}
    </dl>`;

  const auditHTML = `
    <dl class="kvlist">
      ${Object.entries(audit).map(([k, v]) =>
        `<dt>${esc(k)}</dt><dd>${esc(Array.isArray(v) ? v.join(", ") : v)}</dd>`
      ).join("")}
    </dl>`;

  const html = `
    <div class="meta-banner">
      <div class="kv">标准：<b>TDR 评审标准</b></div>
      <div class="kv">版本：<b>${esc(t.version || "—")}</b></div>
      <div class="kv">更新于：<b>${esc(t.last_updated || "—")}</b></div>
    </div>

    <div class="card">
      <h2>五大评审维度 <span class="cnt">权重合计 = 1.0</span></h2>
      <div class="dim-grid">${dimHTML}</div>
    </div>

    <div class="card">
      <h2>三种角色 Roles</h2>
      <div class="grid3">${rolesHTML}</div>
    </div>

    <div class="grid2">
      <div class="card"><h2>评审意见结构</h2>${cmtHTML}</div>
      <div class="card"><h2>闸门规则</h2>${gateHTML}</div>
    </div>

    <div class="grid2">
      <div class="card"><h2>签名与完整性</h2>${signHTML}</div>
      <div class="card"><h2>审计留痕</h2>${auditHTML}</div>
    </div>`;
  document.getElementById("std-tdr").innerHTML = html;
}

function renderStdQuality() {
  const q = STD.quality || {};
  const acc = q.accuracy_baselines || {};
  const conf = q.confidence_grading || {};
  const golden = q.golden_set || {};
  const slo = q.slo || {};
  const cost = q.cost_guardrails || {};
  const llmVal = q.llm_output_validation || {};

  const accHTML = Object.entries(acc).map(([step, metrics]) => `
    <div class="acc-card">
      <div class="step">${esc(step)}</div>
      ${Object.entries(metrics || {}).map(([k, v]) =>
        `<div class="metric">${esc(k)} <b>≥ ${esc(v)}</b></div>`
      ).join("")}
    </div>`).join("");

  const confHTML = `
    <div class="conf-bar">
      <div class="mand">必须人工复核 &lt; 0.70</div>
      <div class="suggest">建议复核 0.70 – 0.90</div>
      <div class="auto">自动通过 ≥ 0.90</div>
    </div>
    <dl class="kvlist">
      ${Object.entries(conf).map(([k, v]) => {
        const body = typeof v === "object"
          ? Object.entries(v).map(([k2, v2]) =>
              `<div><b>${esc(k2)}:</b> <span class="muted">${esc(Array.isArray(v2) ? v2.join(", ") : v2)}</span></div>`
            ).join("")
          : esc(v);
        return `<dt>${esc(k)}</dt><dd>${body}</dd>`;
      }).join("")}
    </dl>`;

  const sloHTML = Object.keys(slo).length === 0
    ? `<div class="muted">未定义 SLO</div>`
    : Object.entries(slo).map(([step, targets]) => `
        <div class="acc-card">
          <div class="step">${esc(step)}</div>
          ${typeof targets === "object" ? Object.entries(targets).map(([k, v]) =>
            `<div class="metric"><b>${esc(k)}:</b> ${esc(v)}</div>`
          ).join("") : esc(targets)}
        </div>`).join("");

  const kvCard = (title, obj) => `
    <div class="card">
      <h2>${esc(title)}</h2>
      ${typeof obj === "object" && obj !== null
        ? `<dl class="kvlist">${Object.entries(obj).map(([k, v]) => {
            const body = Array.isArray(v) ? tags(v) :
              (typeof v === "object" && v !== null
                ? Object.entries(v).map(([k2, v2]) =>
                    `<div><b>${esc(k2)}:</b> <span class="muted">${esc(Array.isArray(v2) ? v2.join(", ") : v2)}</span></div>`
                  ).join("")
                : esc(v));
            return `<dt>${esc(k)}</dt><dd>${body}</dd>`;
          }).join("")}</dl>`
        : `<div class="muted">—</div>`}
    </div>`;

  const html = `
    <div class="meta-banner">
      <div class="kv">标准：<b>质量标准</b></div>
      <div class="kv">版本：<b>${esc(q.version || "—")}</b></div>
      <div class="kv">更新于：<b>${esc(q.last_updated || "—")}</b></div>
    </div>

    <div class="card">
      <h2>准确率基线 Accuracy Baselines <span class="cnt">按步骤</span></h2>
      <div class="acc-grid">${accHTML}</div>
    </div>

    <div class="card">
      <h2>置信度分级 Confidence Grading</h2>
      ${confHTML}
    </div>

    <div class="grid2">
      ${kvCard("Golden Set", golden)}
      <div class="card">
        <h2>SLO 目标</h2>
        <div class="acc-grid">${sloHTML}</div>
      </div>
    </div>

    <div class="grid2">
      ${kvCard("成本护栏 Cost Guardrails", cost)}
      ${kvCard("LLM 输出校验", llmVal)}
    </div>`;
  document.getElementById("std-quality").innerHTML = html;
}

function renderStdData() {
  const d = STD.data || {};
  const schemas = d.report_schemas || {};
  const ids = d.id_conventions || {};
  const prio = d.priorities || {};
  const sev = d.severity || {};
  const defectSM = d.defect_state_machine || d.state_machines?.defect || {};
  const execSM = d.execution_state_machine || d.state_machines?.execution || {};
  const fp = d.input_fingerprint || {};
  const ev = d.evidence_archive || {};

  const schemaHTML = Object.entries(schemas).map(([name, fields]) => {
    if (Array.isArray(fields)) {
      return `<div class="kv-card">
        <div class="kv-title">${esc(name)}</div>
        <div style="margin-top:6px;">${tags(fields)}</div>
      </div>`;
    }
    // object with required/optional keys
    const body = typeof fields === "object" && fields !== null
      ? Object.entries(fields).map(([k, v]) =>
          `<div style="margin-top:4px;"><b style="font-size:11px;color:var(--muted);text-transform:uppercase;">${esc(k)}</b>:<br>${Array.isArray(v) ? tags(v) : esc(v)}</div>`
        ).join("")
      : esc(fields);
    return `<div class="kv-card">
      <div class="kv-title">${esc(name)}</div>
      ${body}
    </div>`;
  }).join("");

  const idHTML = Object.entries(ids).map(([k, v]) =>
    `<tr><td class="code">${esc(k)}</td><td>${esc(typeof v === "object" ? JSON.stringify(v) : v)}</td></tr>`
  ).join("");

  const prioHTML = Object.entries(prio).map(([k, v]) => {
    const k2 = k.toLowerCase();
    return `<div class="kv-card">
      <div class="kv-title">${tag(k, k2)} ${esc(typeof v === "object" ? (v.label || v.name || "") : "")}</div>
      <div class="kv-desc">${esc(typeof v === "object" ? (v.description || v.criteria || JSON.stringify(v)) : v)}</div>
    </div>`;
  }).join("");

  const sevHTML = Object.entries(sev).map(([k, v]) => {
    const k2 = k.toLowerCase();
    return `<div class="kv-card">
      <div class="kv-title">${tag(k, k2)}</div>
      ${typeof v === "object" && v !== null
        ? Object.entries(v).map(([k2, v2]) =>
            `<div class="kv-desc"><b>${esc(k2)}:</b> ${esc(v2)}</div>`
          ).join("")
        : `<div class="kv-desc">${esc(v)}</div>`}
    </div>`;
  }).join("");

  const smHTML = (sm) => {
    if (!sm || Object.keys(sm).length === 0) return `<div class="muted">—</div>`;
    const states = sm.states || sm.nodes || (Array.isArray(sm) ? sm : null);
    if (states) return `<div>${tags(states)}</div>`;
    return `<dl class="kvlist">${Object.entries(sm).map(([k, v]) =>
      `<dt>${esc(k)}</dt><dd>${Array.isArray(v) ? tags(v) : esc(typeof v === "object" ? JSON.stringify(v) : v)}</dd>`
    ).join("")}</dl>`;
  };

  const kvCard = (title, obj) => `
    <div class="card">
      <h2>${esc(title)}</h2>
      ${obj && Object.keys(obj).length > 0
        ? `<dl class="kvlist">${Object.entries(obj).map(([k, v]) => {
            const body = Array.isArray(v) ? tags(v) :
              (typeof v === "object" && v !== null ? JSON.stringify(v) : esc(v));
            return `<dt>${esc(k)}</dt><dd>${body}</dd>`;
          }).join("")}</dl>`
        : `<div class="muted">—</div>`}
    </div>`;

  const html = `
    <div class="meta-banner">
      <div class="kv">标准：<b>数据标准</b></div>
      <div class="kv">版本：<b>${esc(d.version || "—")}</b></div>
      <div class="kv">更新于：<b>${esc(d.last_updated || "—")}</b></div>
    </div>

    <div class="card">
      <h2>报告 Schema <span class="cnt">${Object.keys(schemas).length} 份</span></h2>
      <div class="grid3">${schemaHTML}</div>
    </div>

    <div class="grid2">
      <div class="card">
        <h2>ID 命名规范</h2>
        <table><thead><tr><th>类型</th><th>格式</th></tr></thead>
          <tbody>${idHTML}</tbody></table>
      </div>
      <div class="card">
        <h2>优先级 P0 / P1 / P2</h2>
        <div class="grid3">${prioHTML}</div>
      </div>
    </div>

    <div class="card">
      <h2>Severity 等级与 SLA</h2>
      <div class="grid4">${sevHTML}</div>
    </div>

    <div class="grid2">
      <div class="card">
        <h2>缺陷状态机</h2>
        ${smHTML(defectSM)}
      </div>
      <div class="card">
        <h2>用例执行状态机</h2>
        ${smHTML(execSM)}
      </div>
    </div>

    <div class="grid2">
      ${kvCard("输入指纹 Input Fingerprint", fp)}
      ${kvCard("证据归档 Evidence Archive", ev)}
    </div>`;
  document.getElementById("std-data").innerHTML = html;
}

/* ============================================================
 * 团队建设 TEAM
 * ============================================================ */
function renderTeam() {
  const t = STD.team || {};
  const roles = t.roles || [];
  const comp = t.competency_framework || {};
  const raci = t.raci || {};
  const ladder = t.career_ladder || [];
  const onboard = t.onboarding || {};
  const certs = t.certifications || [];
  const trains = t.training_cadence || [];
  const health = t.team_health_metrics || [];

  // Roles cards
  const roleHTML = roles.map(r => `
    <div class="role-card">
      <div class="role-head">
        <h4>${esc(r.name || r.id)}</h4>
        <div class="lvl">${esc((r.level_range || []).join(" – "))}</div>
      </div>
      <div class="mission">“${esc(r.mission || "")}”</div>
      <div>
        <h3>职责</h3>
        <ul>${(r.responsibilities || []).map(x => `<li>${esc(x)}</li>`).join("")}</ul>
      </div>
      ${r.required_expertise ? `<div style="margin-top:4px;">${tags(r.required_expertise)}</div>` : ""}
      <div class="role-foot">
        <span>${r.tdr_role_mapping ? `<span class="tdr-map">TDR · ${esc(r.tdr_role_mapping)}</span>` : ""}</span>
        <span>↗ 汇报给 <b>${esc(r.reports_to || "—")}</b></span>
      </div>
    </div>`).join("");

  // Competency matrix
  const dims = comp.dimensions || [];
  const levels = comp.levels || {};
  const matrix = comp.matrix || {};
  const matrixRows = Object.entries(matrix).map(([role, perDim]) => {
    const cells = dims.map(d => {
      const lv = perDim[d.id] || "";
      return `<td><span class="heat-cell ${esc(lv)}">${esc(lv)}</span></td>`;
    }).join("");
    return `<tr><td class="role-name">${esc(role)}</td>${cells}</tr>`;
  }).join("");

  const levelLegendHTML = Object.entries(levels).map(([k, v]) =>
    `<span class="heat-cell ${esc(k)}" style="margin-right:6px;">${esc(k)}</span> <span class="muted" style="font-size:12px;margin-right:14px;">${esc(v)}</span>`
  ).join("");

  const compHTML = `
    <div class="raci-legend">${levelLegendHTML}</div>
    <div class="heat-wrap">
      <table class="heat">
        <thead><tr><th>角色 \\ 维度</th>${
          dims.map(d => `<th class="dim">${esc(d.name || d.id)}</th>`).join("")
        }</tr></thead>
        <tbody>${matrixRows}</tbody>
      </table>
    </div>`;

  // RACI matrix
  const allRoles = Array.from(new Set([
    ...Object.values(raci).flatMap(v => [
      ...(v.R || []),
      ...(v.A ? [v.A] : []),
      ...(v.C || []),
      ...(v.I || []),
    ]),
  ]));
  const raciRows = Object.entries(raci).map(([step, v]) => {
    const cells = allRoles.map(role => {
      const chips = [];
      if ((v.R || []).includes(role)) chips.push(`<span class="raci-chip R">R</span>`);
      if (v.A === role) chips.push(`<span class="raci-chip A">A</span>`);
      if ((v.C || []).includes(role)) chips.push(`<span class="raci-chip C">C</span>`);
      if ((v.I || []).includes(role)) chips.push(`<span class="raci-chip I">I</span>`);
      return `<td><div class="raci-cell">${chips.join("")}</div></td>`;
    }).join("");
    return `<tr><td class="step">${esc(step)}</td>${cells}</tr>`;
  }).join("");

  const raciHTML = `
    <div class="raci-legend">
      <span class="raci-chip R">R</span> Responsible 执行 &nbsp;
      <span class="raci-chip A">A</span> Accountable 拍板 &nbsp;
      <span class="raci-chip C">C</span> Consulted 咨询 &nbsp;
      <span class="raci-chip I">I</span> Informed 知情
    </div>
    <div class="heat-wrap">
      <table class="raci">
        <thead><tr><th>SOP 步骤 \\ 角色</th>${
          allRoles.map(r => `<th>${esc(r)}</th>`).join("")
        }</tr></thead>
        <tbody>${raciRows}</tbody>
      </table>
    </div>`;

  // Career ladder
  const ladderHTML = ladder.map(r => `
    <div class="rung">
      <div class="lv-badge ${esc(r.level)}">${esc(r.level)}</div>
      <h4>${esc(r.title || "")}</h4>
      ${r.tenure_months ? `<div class="tenure">${esc(r.tenure_months)} 月</div>` : ""}
      ${r.typical_roles ? `<div>${tags(r.typical_roles)}</div>` : ""}
      <div>
        <h3 style="margin-top:6px;">晋升标准</h3>
        <ul>${(r.promotion_criteria || []).map(x => `<li>${esc(x)}</li>`).join("")}</ul>
      </div>
    </div>`).join("");

  // Onboarding timeline
  const phaseTitles = {
    day_1_to_7:   "Day 1 – 7 · 入职第一周",
    day_8_to_30:  "Day 8 – 30 · 首月",
    day_31_to_60: "Day 31 – 60 · 转正备考",
    day_61_to_90: "Day 61 – 90 · 转正答辩",
  };
  const obHTML = Object.entries(phaseTitles).map(([k, title]) => `
    <div class="tl-col">
      <h4>${esc(title)}</h4>
      <ul>${(onboard[k] || []).map(x => `<li>${esc(x)}</li>`).join("")}</ul>
    </div>`).join("");

  // Certifications
  const certHTML = certs.map(c => `
    <div class="kv-card">
      <div class="kv-title">${esc(c.name)}</div>
      ${c.required_for ? `<div class="kv-desc"><b style="color:var(--reject);">必须：</b>${tags(c.required_for)}</div>` : ""}
      ${c.recommended_for ? `<div class="kv-desc"><b style="color:var(--warn);">推荐：</b>${tags(c.recommended_for)}</div>` : ""}
    </div>`).join("");

  // Training cadence
  const trainHTML = trains.map(t => `
    <div class="kv-card">
      <div class="kv-title">${esc(t.name)}</div>
      <div class="kv-desc">频率：<b>${esc(t.frequency)}</b></div>
      ${t.duration_days ? `<div class="kv-desc">时长：${esc(t.duration_days)} 天</div>` : ""}
      ${t.duration_hours ? `<div class="kv-desc">时长：${esc(t.duration_hours)} 小时</div>` : ""}
      ${t.topics ? `<div style="margin-top:6px;">${tags(t.topics)}</div>` : ""}
    </div>`).join("");

  // Health metrics
  const healthHTML = health.map(h => `
    <div class="kv-card">
      <div class="kv-title">${esc(h.id)}</div>
      <div class="kv-desc">${esc(h.description || "")}</div>
      ${h.target ? `<div class="kv-target">目标：<b>${esc(h.target)}</b></div>` : ""}
      ${h.warn_if ? `<div class="kv-target kv-warn">告警：<b>${esc(h.warn_if)}</b></div>` : ""}
    </div>`).join("");

  const html = `
    <div class="meta-banner">
      <div class="kv">标准：<b>团队建设 Team Building</b></div>
      <div class="kv">版本：<b>${esc(t.version || "—")}</b></div>
      <div class="kv">Owner：<b>${esc(t.owner || "—")}</b></div>
      <div class="kv">更新于：<b>${esc(t.last_updated || "—")}</b></div>
    </div>

    <div class="card">
      <h2>角色体系 Roles <span class="cnt">${roles.length} 个角色</span></h2>
      <div class="role-grid">${roleHTML}</div>
    </div>

    <div class="card">
      <h2>能力矩阵 Competency Matrix <span class="cnt">${dims.length} 维 × 5 级</span></h2>
      ${compHTML}
    </div>

    <div class="card">
      <h2>RACI × SOP <span class="cnt">${Object.keys(raci).length} 个交付物</span></h2>
      ${raciHTML}
    </div>

    <div class="card">
      <h2>职业梯队 Career Ladder <span class="cnt">L1 – L5</span></h2>
      <div class="ladder">${ladderHTML}</div>
    </div>

    <div class="card">
      <h2>入职路径 Onboarding <span class="cnt">30 / 60 / 90 天</span></h2>
      <div class="timeline-cols">${obHTML}</div>
    </div>

    <div class="grid2">
      <div class="card">
        <h2>认证 Certifications</h2>
        <div class="grid2">${certHTML}</div>
      </div>
      <div class="card">
        <h2>培训节奏 Training Cadence</h2>
        <div class="grid2">${trainHTML}</div>
      </div>
    </div>

    <div class="card">
      <h2>团队健康度 Health Metrics <span class="cnt">${health.length} 项</span></h2>
      <div class="grid3">${healthHTML}</div>
    </div>`;
  document.getElementById("team-body").innerHTML = html;
}

// boot
boot();
</script>
</body>
</html>
"""


@app.get("/")
async def root(request: Request):
    """根路径默认带去 /tools (已登录) 或 /login (未登录)。
    middleware 已经把未登录的拦走;这里走到说明已登录。
    """
    return RedirectResponse("/tools", status_code=302)


@app.get("/qms", response_class=HTMLResponse)
async def qms_portal() -> str:
    return QMS_PORTAL_HTML


@app.get("/tdr-ui", response_class=HTMLResponse)
async def tdr_ui() -> str:
    return TDR_WORKBENCH_HTML


@app.get("/pipeline", response_class=HTMLResponse)
async def pipeline_dashboard() -> str:
    return PIPELINE_DASHBOARD_HTML


@app.get("/runs", response_class=HTMLResponse)
async def runs_index() -> str:
    return PIPELINE_DASHBOARD_HTML


# =====================================================================
# Prompts — 提示词集成进工具详情页
# =====================================================================

_STEP_LABELS = [
    ("step1_requirement", "Step 1 需求拆解"),
    ("step2_testcase", "Step 2 测试用例设计"),
    ("step4_api", "Step 4 接口测试"),
    ("step5_ui", "Step 5 UI 一致性比对"),
    ("step6_agent", "Step 6 Agent 自动化执行"),
]


def _all_prompt_steps() -> list[dict[str, Any]]:
    """Load every step's prompts and return JSON-serializable summaries."""
    from packages.core.prompts import load_step

    out: list[dict[str, Any]] = []
    for dir_name, label in _STEP_LABELS:
        try:
            step = load_step(dir_name)
        except Exception as exc:
            out.append({"step_id": dir_name, "name": label, "error": str(exc)})
            continue
        substeps = []
        for fname in step.order:
            tpl = next((t for t in step.templates.values() if t.path.name == fname), None)
            if tpl is None:
                continue
            substeps.append({
                "id": tpl.id,
                "name": tpl.name,
                "version": tpl.version,
                "model_tier": tpl.model_tier.value,
                "temperature": tpl.temperature,
                "max_tokens": tpl.max_tokens,
                "placeholders": tpl.placeholders,
                "output_format": tpl.output_format,
                "output_schema": tpl.output_schema,
                "body": tpl.body,
                "source_path": str(tpl.path),
            })
        out.append({
            "step_id": step.step_id,
            "dir_name": dir_name,
            "display_name": label,
            "name": step.name,
            "version": step.version,
            "common_system_suffix": step.common_system_suffix,
            "substeps": substeps,
        })
    return out


@app.get("/api/prompts")
async def api_prompts_list() -> dict[str, Any]:
    """列出全部 25 个 SOP 提示词（按 5 个 AI 步骤分组）。"""
    steps = _all_prompt_steps()
    total = sum(len(s.get("substeps", [])) for s in steps)
    return {"total": total, "steps": steps}


@app.get("/api/prompts/{step_dir}/{sub_id}")
async def api_prompt_show(step_dir: str, sub_id: str) -> dict[str, Any]:
    """获取单个提示词的完整正文与 frontmatter。"""
    from packages.core.prompts import load_step
    from packages.core.prompts.loader import OVERRIDES_DIR, PROMPTS_DIR

    try:
        step = load_step(step_dir)
    except Exception as exc:
        raise HTTPException(404, f"step not found: {exc}")
    try:
        tpl = step.get(sub_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    is_override = str(tpl.path).startswith(str(OVERRIDES_DIR))
    return {
        "id": tpl.id,
        "name": tpl.name,
        "version": tpl.version,
        "model_tier": tpl.model_tier.value,
        "temperature": tpl.temperature,
        "max_tokens": tpl.max_tokens,
        "placeholders": tpl.placeholders,
        "output_format": tpl.output_format,
        "output_schema": tpl.output_schema,
        "body": tpl.body,
        "common_system_suffix": step.common_system_suffix,
        "source_path": str(tpl.path),
        "is_override": is_override,
    }


class PromptUpdate(BaseModel):
    body: str  # body-only or full markdown with frontmatter


@app.put("/api/prompts/{step_dir}/{sub_id}")
async def api_prompt_update(step_dir: str, sub_id: str, payload: PromptUpdate) -> dict[str, Any]:
    """编辑提示词正文 — 写入 configs/prompts_overrides/，下次运行立即生效。"""
    from packages.core.prompts import load_step, write_prompt_override

    try:
        step = load_step(step_dir)
        tpl = step.get(sub_id)
    except Exception as exc:
        raise HTTPException(404, str(exc))
    fname = Path(tpl.path).name
    target = write_prompt_override(step_dir, fname, payload.body)
    return {"saved": str(target), "is_override": True}


@app.delete("/api/prompts/{step_dir}/{sub_id}")
async def api_prompt_reset(step_dir: str, sub_id: str) -> dict[str, Any]:
    """重置覆盖：删除 overrides 文件，下次运行回退到 configs/prompts/ 原版。"""
    from packages.core.prompts import load_step, reset_prompt_override
    from packages.core.prompts.loader import PROMPTS_DIR

    # We need the original filename from configs/prompts/ (override may have been deleted already)
    try:
        step = load_step(step_dir)
        tpl = step.get(sub_id)
    except Exception as exc:
        raise HTTPException(404, str(exc))
    fname = Path(tpl.path).name
    removed = reset_prompt_override(step_dir, fname)
    return {"reset": removed, "filename": fname}


PROMPTS_DETAIL_HTML = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SOP 提示词详情</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@300;400;500;600;700&family=Noto+Sans+SC:wght@300;400;500;600&family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:#07080a; --surface:#0f1216; --line:#1f2530; --line-2:#2a3140;
    --fg:#f5f7fa; --fg-2:#a8aeb8; --fg-3:#6c7380;
    --ac:#10b981; --ac-2:#6ee7b7; --warn:#fbbf24;
    --mono:ui-monospace,SFMono-Regular,"JetBrains Mono",Menlo,monospace;
    --sans:-apple-system,BlinkMacSystemFont,"SF Pro Display","SF Pro Text","PingFang SC","Helvetica Neue",Arial,sans-serif;
  }
  *{box-sizing:border-box}
  html,body{margin:0;background:var(--bg);color:var(--fg);font-family:var(--sans);min-height:100%}
  body{
    background:
      radial-gradient(ellipse 90% 55% at 50% 0%, rgba(16,185,129,.10), transparent 65%) fixed,
      radial-gradient(ellipse 85% 45% at 50% 100%, rgba(110,231,183,.06), transparent 60%) fixed,
      var(--bg);
  }
  header{
    display:flex;align-items:center;gap:18px;padding:18px 28px;
    border-bottom:1px solid var(--line);position:sticky;top:0;background:rgba(7,8,10,.78);
    backdrop-filter:blur(12px);z-index:10;
  }
  header .logo{
    width:32px;height:32px;border:1.5px solid var(--ac);border-radius:8px;
    display:grid;place-items:center;color:var(--ac);font-family:var(--mono);font-weight:700;font-size:14px;
  }
  header h1{margin:0;font-size:17px;font-weight:600;letter-spacing:-.01em}
  header .meta{margin-left:auto;font-size:12px;color:var(--fg-3);font-family:var(--mono)}
  header a{color:var(--ac);text-decoration:none;font-size:13px;margin-left:18px}
  header a:hover{text-decoration:underline}
  main{display:grid;grid-template-columns:300px 1fr;min-height:calc(100vh - 65px)}
  nav{
    border-right:1px solid var(--line);padding:18px 16px;overflow-y:auto;
    max-height:calc(100vh - 65px);position:sticky;top:65px;
  }
  nav h2{font-size:11px;color:var(--fg-3);text-transform:uppercase;letter-spacing:.12em;margin:0 0 10px;font-weight:600}
  nav .step{margin-bottom:18px}
  nav .step-name{font-size:13px;color:var(--fg);font-weight:600;margin-bottom:6px;letter-spacing:-.01em}
  nav .sub{
    display:block;padding:6px 10px;margin:2px 0;border-radius:6px;
    font-family:var(--mono);font-size:12px;color:var(--fg-2);text-decoration:none;
    cursor:pointer;border:1px solid transparent;
  }
  nav .sub:hover{background:rgba(16,185,129,.06);border-color:var(--line-2);color:var(--ac)}
  nav .sub.active{background:rgba(16,185,129,.10);border-color:var(--ac);color:var(--ac)}
  nav .sub small{display:block;color:var(--fg-3);font-family:var(--sans);font-size:11px;margin-top:2px}
  section.detail{padding:32px 40px;max-width:1100px;overflow-x:hidden}
  .badge-row{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 14px}
  .badge{
    font-family:var(--mono);font-size:11px;padding:3px 10px;border-radius:999px;
    border:1px solid var(--line-2);color:var(--fg-2)
  }
  .badge.model{border-color:rgba(16,185,129,.4);color:var(--ac)}
  .badge.tier{border-color:rgba(251,191,36,.4);color:var(--warn)}
  h2.sub-title{margin:0 0 4px;font-size:24px;letter-spacing:-.02em;font-weight:600}
  .sub-id{font-family:var(--mono);font-size:13px;color:var(--ac);margin-bottom:18px}
  .meta-grid{
    display:grid;grid-template-columns:140px 1fr;gap:8px 18px;margin:18px 0 22px;
    font-size:13px;
  }
  .meta-grid dt{color:var(--fg-3);font-size:12px}
  .meta-grid dd{margin:0;font-family:var(--mono);font-size:12.5px;color:var(--fg)}
  .meta-grid code{background:var(--surface);border:1px solid var(--line);padding:1px 6px;border-radius:4px}
  .body-panel{
    background:var(--surface);border:1px solid var(--line);border-radius:12px;
    padding:20px 24px;margin:14px 0 8px;position:relative;
  }
  .body-panel h3{margin:0 0 12px;font-size:13px;color:var(--fg-3);text-transform:uppercase;letter-spacing:.1em;font-weight:600}
  .body-panel pre{
    margin:0;font-family:var(--mono);font-size:13px;line-height:1.62;color:var(--fg);
    white-space:pre-wrap;word-break:break-word;max-height:none;
  }
  .body-panel .copy{
    position:absolute;top:14px;right:16px;background:rgba(16,185,129,.08);
    border:1px solid rgba(16,185,129,.3);color:var(--ac);
    padding:4px 12px;border-radius:6px;font-family:var(--mono);font-size:12px;
    cursor:pointer;
  }
  .body-panel .copy:hover{background:rgba(16,185,129,.16)}
  .body-panel .copy.ok{background:rgba(74,222,128,.12);border-color:#4ade80;color:#4ade80}
  .placeholder{color:var(--ac);font-weight:600}
  .empty{padding:60px 0;text-align:center;color:var(--fg-3)}
  footer{padding:18px 40px;border-top:1px solid var(--line);font-size:12px;color:var(--fg-3)}
  @media(max-width:900px){
    main{grid-template-columns:1fr}
    nav{position:static;max-height:none;border-right:none;border-bottom:1px solid var(--line)}
    section.detail{padding:24px 20px}
  }
</style></head>
<body>
<header>
  <a class="brand-link" href="/tools" title="天枢 · 裁决 · 返回主页" style="text-decoration:none;display:inline-flex;align-items:center;gap:10px;font-family:'Noto Serif SC',Georgia,serif;font-size:19px;font-weight:600;letter-spacing:.18em;color:var(--fg);margin-right:24px;padding:4px 0"><svg viewBox="0 0 24 24" fill="currentColor" width="24" height="24" aria-hidden="true" style="color:var(--ac);opacity:1;flex-shrink:0;filter:drop-shadow(0 1px 2px rgba(196,90,58,.28))"><path d="M12 1.5L13.4 10.6L22.5 12L13.4 13.4L12 22.5L10.6 13.4L1.5 12L10.6 10.6Z"/></svg><span>天枢</span><span style="color:var(--ac);margin:0 6px;font-weight:400">·</span><span>裁决</span></a>
  <h1>SOP 提示词详情</h1>
  <div class="meta">25 prompts · 5 AI steps</div>
  <a href="/">← 总览</a>
  <a href="/pipeline">Pipeline</a>
  <a href="/api/prompts" target="_blank">JSON</a>
</header>
<main>
  <nav id="nav"><h2>加载中…</h2></nav>
  <section class="detail" id="detail"><div class="empty">从左侧选择一个提示词</div></section>
</main>
<footer>来源：<code>configs/prompts/</code> · 工具：<code>qactl prompts list / show / export</code></footer>
<script>
async function load() {
  const r = await fetch('/api/prompts');
  const data = await r.json();
  const nav = document.getElementById('nav');
  nav.innerHTML = '<h2>SOP 5 个 AI 步骤</h2>';
  data.steps.forEach(step => {
    const div = document.createElement('div');
    div.className = 'step';
    div.innerHTML = `<div class="step-name">${step.display_name}</div>`;
    step.substeps.forEach(s => {
      const a = document.createElement('a');
      a.className = 'sub';
      a.dataset.id = s.id;
      a.dataset.dir = step.dir_name;
      a.innerHTML = `${s.id} <small>${s.name}</small>`;
      a.onclick = () => show(step, s, a);
      div.appendChild(a);
    });
    nav.appendChild(div);
  });
  // auto-show first
  const first = nav.querySelector('.sub');
  if (first) first.click();
}
function highlightPlaceholders(body) {
  return body.replace(/\\{\\{[^}]+\\}\\}/g, m => `<span class="placeholder">${m}</span>`);
}
function show(step, s, el) {
  document.querySelectorAll('nav .sub').forEach(x => x.classList.remove('active'));
  if (el) el.classList.add('active');
  const d = document.getElementById('detail');
  const ph = (s.placeholders || []).map(p => `<code>{{${p}}}</code>`).join(' ') || '—';
  d.innerHTML = `
    <div class="badge-row">
      <span class="badge">${step.display_name}</span>
      <span class="badge model">model: ${s.model_tier}</span>
      <span class="badge tier">temperature: ${s.temperature}</span>
      <span class="badge">max_tokens: ${s.max_tokens}</span>
      <span class="badge">${s.output_format}${s.output_schema ? ': ' + s.output_schema : ''}</span>
    </div>
    <h2 class="sub-title">${s.name}</h2>
    <div class="sub-id">${s.id} · v${s.version}</div>
    <dl class="meta-grid">
      <dt>占位符</dt><dd>${ph}</dd>
      <dt>源文件</dt><dd><code>${s.source_path}</code></dd>
    </dl>
    <div class="body-panel">
      <button class="copy" onclick="copyBody(this, ${JSON.stringify(s.body).replace(/"/g, '&quot;')})">复制</button>
      <h3>提示词正文</h3>
      <pre>${highlightPlaceholders(escapeHtml(s.body))}</pre>
    </div>
    ${step.common_system_suffix ? `
    <div class="body-panel">
      <h3>通用系统后缀（${step.step_id}）</h3>
      <pre>${escapeHtml(step.common_system_suffix)}</pre>
    </div>` : ''}
  `;
  history.replaceState(null, '', `#${s.id}`);
}
function escapeHtml(s) { return s.replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
async function copyBody(btn, text) {
  try { await navigator.clipboard.writeText(text); btn.textContent = '已复制'; btn.classList.add('ok');
    setTimeout(() => { btn.textContent = '复制'; btn.classList.remove('ok'); }, 1500); }
  catch(e) { btn.textContent = '失败'; }
}
load().then(() => {
  // jump to hash if present
  const h = location.hash.slice(1);
  if (h) {
    const el = document.querySelector(`nav .sub[data-id="${h}"]`);
    if (el) el.click();
  }
});
</script>
</body></html>
"""


@app.get("/prompts", response_class=HTMLResponse)
async def prompts_detail_page() -> str:
    return PROMPTS_DETAIL_HTML


# =====================================================================
# Tools — 工具集（每个 SOP AI 步骤一个工具）
# =====================================================================

import asyncio as _asyncio
import json as _json
import re as _re
import time as _time
import traceback as _tb

# Tool catalog: 8 个 AI 工具
# 每个工具：id / step / name / icon / description / fields / output / 提示词跳转
#
# 命名说明:8 步 SOP 里的 step3(原"开发自测")在本工具集合并到 step4 接口测试,
# 因此 catalog 里只有 step1/2/4/5/6 + network_resilience / h5_adapt / seo_audit。
# 如需补回 step3,在此列表新增 id="step3" 的项并新建 configs/prompts/step3_*/。
TOOL_CATALOG: list[dict[str, Any]] = [
    {
        "id": "step1",
        "step": "step1",
        "prompt_dir": "step1_requirement",
        "name": "需求评审",
        "icon": "📋",
        "tagline": "把 PRD/UI/原型拆成可测结构，识别遗漏与门禁",
        "description": (
            "AI 自动阅读需求材料，输出《需求拆解报告》：模块拆解、主流程/异常流程、"
            "前后端交互链路、状态流转、需求遗漏点。命中关键缺失时直接 reject_with_report 阻止下一步。"
        ),
        "responsible": "AI 主执行",
        "output": "《需求拆解报告》",
        "gate": "材料完整度 < 80% 或 blocker 遗漏 ≥ 3 时阻止进入测试用例设计",
        "prompts": ["step1.1", "step1.2", "step1.3", "step1.4", "step1.5"],
        "endpoint": "/api/tools/step1/run",
        "substeps_optional": True,
        "input": {
            "label": "需求与文档",
            "hint": "粘贴需求文本 / 上传 PRD/原型/UI/流程/接口文档 — 任意拼合",
            "primary_key": "documents",
            "format": "text",
        },
    },
    {
        "id": "step2",
        "step": "step2",
        "prompt_dir": "step2_testcase",
        "name": "测试用例设计",
        "icon": "🧪",
        "tagline": "基于需求拆解报告生成 P0/P1/P2 用例集",
        "description": (
            "覆盖主流程、异常、边界、权限、状态流转、双端差异。每条用例含步骤、预期、"
            "依赖、自动化适配标记。最终输出统一字段的《测试用例集》。"
        ),
        "responsible": "AI 主执行",
        "output": "《测试用例集（P0/P1/P2）》",
        "gate": "主流程未覆盖、优先级错误、用例不可执行时不流转",
        "prompts": ["step2.1", "step2.2", "step2.3", "step2.4", "step2.5"],
        "endpoint": "/api/tools/step2/run",
        "substeps_optional": True,
        "input": {
            "label": "需求与场景",
            "hint": "粘贴需求文档 / 业务流程 / 用例骨架 — 直接生成 P0/P1/边界/权限/状态机用例",
            "primary_key": "documents",
            "format": "text",
        },
    },
    {
        "id": "step4",
        "step": "step4",
        "prompt_dir": "step4_api",
        "name": "接口测试",
        "icon": "🔌",
        "tagline": "覆盖功能、安全、边界值，给版本可测性结论",
        "description": (
            "梳理核心接口清单与业务链路，覆盖功能正确性 / 安全与权限 / 边界值与异常处理。"
            "命中核心接口不通、鉴权失效、关键字段缺失等条件 → 退回提测。"
        ),
        "responsible": "AI + 测试",
        "output": "《接口测试报告》",
        "gate": "核心接口不通 / 鉴权失效 / 主流程接口报错 → 退回提测",
        "prompts": ["step4.1", "step4.2", "step4.3", "step4.4", "step4.5"],
        "endpoint": "/api/tools/step4/run",
        "substeps_optional": True,
        "input": {
            "label": "接口与场景",
            "hint": "粘贴 API 文档 / OpenAPI / Postman / 接口清单 / 业务说明 — 直接出功能/性能/安全/边界/契约用例",
            "primary_key": "documents",
            "format": "text",
        },
    },
    {
        "id": "step5",
        "step": "step5",
        "prompt_dir": "step5_ui",
        "name": "UI 一致性比对",
        "icon": "🎨",
        "tagline": "对照 PRD/UI 检查实现偏差，超 20% 退回",
        "description": (
            "分析页面结构、入口、按钮、文案、交互流程、状态反馈、双端一致性。"
            "统计每个核心页面/模块的不符合率，超过 20% 阈值的关键页面直接退回提测。"
        ),
        "responsible": "AI + 测试",
        "output": "《需求/UI 一致性评估报告》",
        "gate": "核心页面/模块偏差 > 20% 或主流程交互不一致 → 退回提测",
        "prompts": ["step5.1", "step5.2", "step5.3", "step5.4", "step5.5"],
        "endpoint": "/api/tools/step5/run",
        "substeps_optional": True,
        "input": {
            "label": "UI 材料（设计稿 + 实际页面）",
            "hint": (
                "建议按下面两段格式贴：\n\n"
                "## 设计稿\n"
                "<Figma URL / 设计稿描述 / 设计 token / 配色规约>\n\n"
                "## 实际页面\n"
                "<线上 URL（系统自动截图）/ 页面状态描述 / 已发现的差异>\n\n"
                "工具会自动截取「实际页面」URL（Mobile 375×812 + Desktop 1440×900），LLM 对照「设计稿」做图片比对并给出 bbox 框选坐标。"
            ),
            "primary_key": "documents",
            "format": "text",
        },
    },
    {
        "id": "step6",
        "step": "step6",
        "prompt_dir": "step6_agent",
        "name": "Agent 自动化执行",
        "icon": "🤖",
        "tagline": "Agent 跑 P0 + 关键 P1/P2，归因 + 阻塞清单",
        "description": (
            "执行前检查（环境/账号/数据/依赖）→ P0 全量执行 → 关键 P1/P2 执行 → "
            "失败归因（前端/后端/接口/数据/环境/需求）→ 输出《Agent 自动化执行报告》。"
            "默认 dry-run，需要真实环境时手动开 --live。"
        ),
        "responsible": "AI Agent",
        "output": "《Agent 自动化执行报告》",
        "gate": "页面不可达 / 元素缺失 / 数据或权限阻塞 → 标记阻塞",
        "prompts": ["step6.1", "step6.2", "step6.3", "step6.4", "step6.5"],
        "endpoint": "/api/tools/step6/run",
        "substeps_optional": True,
        "input": {
            "label": "执行材料",
            "hint": "粘贴用例集 / 业务场景 / 环境信息 — 出主流程/异常/数据驱动/失败归因/覆盖率方案",
            "primary_key": "documents",
            "format": "text",
        },
        "run_options": [
            {"key": "dry_run", "label": "Dry-run（不真实操作）", "default": True},
        ],
    },
    {
        "id": "network_resilience",
        "step": "network",
        "prompt_dir": "network_resilience",
        "name": "弱网/断网测试",
        "icon": "📶",
        "substeps_optional": True,
        "tagline": "弱网档位 + 离线/恢复 + 容错审计 + 退回判定",
        "description": (
            "识别网络敏感操作 → 设计弱网（慢/丢包/延迟）+ 断网（offline/reconnect/切换/挂起）"
            "用例 → 审计客户端容错 7 维度（超时/重试/幂等/取消/队列/错误分类/可观测）"
            "→ 输出《弱网容灾报告》。命中重复提交/数据丢失/缺幂等等即退回提测。"
        ),
        "responsible": "AI + 测试",
        "output": "《弱网容灾报告》",
        "gate": "断网→恢复出现重复提交/数据丢失/无幂等 → 退回提测",
        "prompts": ["net.1", "net.2", "net.3", "net.4", "net.5"],
        "endpoint": "/api/tools/network_resilience/run",
        "input": {
            "label": "业务材料",
            "hint": "PRD / 接口文档 / 主流程描述 / 客户端实现资料 — 任意拼合",
            "primary_key": "documents",
            "format": "text",
        },
    },
    {
        "id": "h5_adapt",
        "step": "h5",
        "prompt_dir": "h5_adapt",
        "name": "H5 适配初审",
        "icon": "📱",
        "substeps_optional": True,
        "tagline": "页面盘点 + 视口/安全区/浏览器矩阵/交互/性能 全维度",
        "description": (
            "针对 H5 / Web Mobile 在 iOS Safari、Android Chrome、微信 X5/MQQ、QQ/UC、"
            "钉钉/飞书/企业微信、抖音/小红书等 15+ 浏览器和 WebView 下的适配深度审计。"
            "覆盖 viewport / safe-area / 像素密度 / 长屏 / 表单键盘 / 触摸手势 / 性能 / 分享 / 唤起 App / 暗色模式 / 无障碍。"
        ),
        "responsible": "AI + 前端",
        "output": "《H5 适配初审报告》",
        "gate": "主流程在微信/iOS Safari/Android Chrome 任一 critical 失败 → 退回",
        "prompts": ["h5.1", "h5.2", "h5.3", "h5.4", "h5.5"],
        "endpoint": "/api/tools/h5_adapt/run",
        "input": {
            "label": "H5 资料（URL + 业务说明）",
            "hint": (
                "建议按下面两段格式贴：\n\n"
                "## 待测 H5 URL\n"
                "<https://...> （系统自动多 viewport 截图）\n\n"
                "## 业务/客诉/已知差异\n"
                "<页面用途 / 已知兼容问题 / UA 样本 / HTML head / CSS 片段>\n\n"
                "工具会自动截取 6 个常见手机分辨率（iPhone SE / 13 / 14 ProMax / Galaxy S20 / iPad / Desktop）。"
            ),
            "primary_key": "documents",
            "format": "text",
        },
    },
    {
        "id": "seo_audit",
        "step": "seo",
        "prompt_dir": "seo_audit",
        "name": "SEO 深度审计",
        "icon": "🔍",
        "substeps_optional": True,
        "tagline": "抓取 + 元数据 + 内容 + Core Web Vitals 四维深度",
        "description": (
            "审计协议/重定向/robots/sitemap/canonical/hreflang → 元数据与 schema.org → "
            "内容结构与可访问性 → Core Web Vitals → 输出按 ROI 排序的整改清单 + 快速胜利。"
        ),
        "responsible": "AI 主执行",
        "output": "《SEO 深度审计报告》",
        "gate": "全站 noindex 误配 / robots 屏蔽全站 / sitemap 大量 4xx → 退回",
        "prompts": ["seo.1", "seo.2", "seo.3", "seo.4", "seo.5"],
        "endpoint": "/api/tools/seo_audit/run",
        "input": {
            "label": "站点资料",
            "hint": "robots.txt / sitemap.xml / 抽样 HTML / Lighthouse 数据 / 业务定位 — 任意拼合",
            "primary_key": "documents",
            "format": "text",
        },
    },
]


@app.get("/api/tools")
async def api_tools_catalog() -> dict[str, Any]:
    """工具集目录（5 个 AI 工具）。"""
    return {"total": len(TOOL_CATALOG), "tools": TOOL_CATALOG}


@app.get("/api/tools/{tool_id}")
async def api_tool_detail(tool_id: str, request: Request) -> dict[str, Any]:
    # 路由顺序兼容：FastAPI 会用 wildcard 抢先匹配 /api/tools/runs，
    # 这里转发到正确的 list 端点。/api/tools/runs/{run_id} 是不同 path
    # 模板，不会被这个 wildcard 拦下。
    if tool_id == "runs":
        return await api_tool_run_list(request)
    tool = next((t for t in TOOL_CATALOG if t["id"] == tool_id), None)
    if not tool:
        raise HTTPException(404, f"unknown tool: {tool_id}")
    return tool


# ----- 异步任务管理 -----
# 简单内存版任务表：{run_id: {status, started_at, finished_at, tool_id, progress, report, error}}
_RUNS: dict[str, dict[str, Any]] = {}


_KNOWN_INPUT_KEYS = {
    "prd", "prototype", "ui_design", "flow_chart", "api_doc", "business_rules",
    "requirement_report", "test_case_report",
    "automation_risk", "env_info",
    "design_assets", "actual_snapshots", "state_captures",
    # network_resilience / seo_audit single-blob input
    "documents", "raw_documents",
    "main_flow", "client_impl",
    "site_info", "robots_txt", "sitemap_xml", "crawl_samples",
    "page_samples", "page_dom_samples", "business_positioning",
    "perf_data", "resource_inventory", "waterfall",
    # h5_adapt advanced keys
    "business_doc", "page_list", "screenshots", "user_scenarios",
    "html_head_samples", "css_snippets", "js_entry_deps", "key_apis",
    "ua_samples", "complaints", "interactive_components", "forms",
    "perf_samples",
}


def _shape_orchestrator_inputs(
    tool: dict[str, Any], raw: dict[str, Any]
) -> dict[str, Any]:
    """Map simplified single-field UI body to the orchestrator's expected keys.

    Logic:
      - If the tool's primary_key is a real input key → assign directly.
      - If primary_key == "_documents" (auto mode for step4/step5):
          - If the value is a dict, pass through any known keys
            (requirement_report, test_case_report, api_doc, …).
          - Otherwise dump as 'documents' raw text — orchestrators tolerate
            missing keys via `(未提供)` placeholders.
      - Run options (e.g. dry_run) are merged last.
    """
    inp_meta = tool.get("input", {}) or {}
    primary = inp_meta.get("primary_key")
    out: dict[str, Any] = {}

    payload = raw.pop(primary, None) if primary and primary != "_documents" else None
    docs = raw.pop("_documents", None) if primary == "_documents" else None

    if primary and primary != "_documents" and payload is not None:
        out[primary] = payload
    elif docs is not None:
        if isinstance(docs, dict):
            for k, v in docs.items():
                if k in _KNOWN_INPUT_KEYS:
                    out[k] = v
            if not out and "raw" in docs:
                out["raw_documents"] = docs["raw"]
        else:
            # Free text — we don't know how to split; orchestrators expect
            # specific keys, so park it under 'raw_documents'. Most prompts
            # treat absent keys as `(未提供)` and proceed.
            out["raw_documents"] = docs

    # Run options (checkboxes etc.) come from the form too.
    for ro in tool.get("run_options", []) or []:
        out[ro["key"]] = bool(raw.pop(ro["key"], ro.get("default", False)))

    # Anything left in raw that is a real orchestrator key (advanced override)
    for k, v in list(raw.items()):
        if k.startswith("__"):
            continue
        if k in _KNOWN_INPUT_KEYS:
            out[k] = v
    # 项目编号 + 名称 透传(build_meta 会读)
    if raw.get("project_code"):
        out["project_code"] = str(raw["project_code"]).strip()
    if raw.get("project_name"):
        out["project_name"] = str(raw["project_name"]).strip()
    return out


_URL_RE = _re.compile(r"https?://[\w\.\-/:?#\[\]@!$&'()*+,;=%]+", _re.IGNORECASE)

# Tool ID → (viewport tuples). Each tuple = (label, width, height).
_TOOL_VIEWPORTS = {
    "h5_adapt": [
        ("iPhone SE", 375, 667),
        ("iPhone 13", 390, 844),
        ("iPhone 14 Pro Max", 430, 932),
        ("Galaxy S20", 360, 800),
        ("iPad Mini", 768, 1024),
        ("Desktop", 1920, 1080),
    ],
    "step5": [
        ("Mobile", 375, 812),
        ("Desktop", 1440, 900),
    ],
    # SEO 审计:抓首页 + Mobile 视图,LLM 看 LCP/CLS 时有图可参
    "seo_audit": [
        ("Desktop", 1440, 900),
        ("Mobile", 375, 812),
    ],
    # 弱网/断网:有图能看「断网态错误页 vs 在线态」对比
    "network_resilience": [
        ("Desktop", 1440, 900),
    ],
}


async def _capture_screenshots_for_tool(
    tool_id: str,
    ctx: Any,
    state: dict[str, Any],
) -> list[dict[str, Any]] | None:
    """Detect URLs in the tool's `documents` input and capture viewport screenshots.

    Only runs for tools listed in `_TOOL_VIEWPORTS` (currently step5 / h5_adapt).
    Saves PNGs to <evidence_output_dir>/screenshots/.
    """
    viewports = _TOOL_VIEWPORTS.get(tool_id)
    if not viewports:
        return None

    docs = (ctx.inputs or {}).get("documents") or ""
    if not isinstance(docs, str):
        return None
    urls = list(dict.fromkeys(m.group(0).rstrip(".,;:!?)」")
                              for m in _URL_RE.finditer(docs)))
    if not urls:
        return None
    urls = urls[:5]  # cap so we don't run for hours

    try:
        from playwright.async_api import async_playwright  # type: ignore
    except ImportError:
        state.setdefault("logs", []).append({
            "ts": _time.time(), "event": "screenshot.skip",
            "reason": "playwright not installed",
        })
        return None

    out_dir = Path(settings.evidence_output_dir) / "screenshots"
    out_dir.mkdir(parents=True, exist_ok=True)

    state["progress"] = f"截图准备：{len(urls)} URL × {len(viewports)} 视口…"
    captured: list[dict[str, Any]] = []
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                for url in urls:
                    for vp_label, w, h in viewports:
                        state["progress"] = f"截图 {url[:60]} @ {vp_label}"
                        safe = _re.sub(r"[^\w]", "_", url)[:40]
                        fname = f"{tool_id}_{ctx.run_id[:8]}_{safe}_{w}x{h}.png"
                        fpath = out_dir / fname
                        try:
                            page = await browser.new_page(viewport={"width": w, "height": h})
                            await page.goto(url, timeout=30000, wait_until="domcontentloaded")
                            try:
                                await page.wait_for_load_state("networkidle", timeout=8000)
                            except Exception:
                                pass  # tolerate slow networks; capture what we have
                            await page.screenshot(path=str(fpath), full_page=True)
                            await page.close()
                            captured.append({
                                "url": url, "viewport": vp_label,
                                "width": w, "height": h,
                                "filename": fname,
                                "size": fpath.stat().st_size,
                            })
                        except Exception as exc:
                            captured.append({
                                "url": url, "viewport": vp_label,
                                "width": w, "height": h,
                                "error": str(exc)[:200],
                            })
            finally:
                await browser.close()
    except Exception as exc:
        state.setdefault("logs", []).append({
            "ts": _time.time(), "event": "screenshot.failed",
            "error": str(exc)[:200],
        })
        return captured or None

    return captured or None


_VALID_SEVERITIES = {"critical", "high", "medium", "low", "info"}
_SEVERITY_ALIASES = {
    "blocker":    "high",
    "major":      "high",
    "minor":      "low",
    "suggestion": "low",
    "trivial":    "info",
    "warning":    "medium",
    "warn":       "medium",
    "error":      "high",
    "fatal":      "critical",
}


def _normalize_severity(value: Any) -> str:
    """把任意 severity 值规范化为白名单内的 5 个值之一。

    防御 XSS：模型输出 / 报告导入可能包含 `high" onclick="alert(1)` 这种
    破坏 HTML 属性的串。这里把所有值都映射到固定枚举，确保 class 拼接安全。
    """
    s = str(value or "").lower().strip()
    if s in _VALID_SEVERITIES:
        return s
    if s in _SEVERITY_ALIASES:
        return _SEVERITY_ALIASES[s]
    return "medium"


def _build_executive_summary(report: dict[str, Any], tool: dict[str, Any]) -> dict[str, Any]:
    """Aggregate any existing report into the 5-section structure (统一报告契约):
       测试结论 (含 verdict_summary + KPI) / 风险结论 / 阻碍 / Bug 表 (issues 按 sev×pri 排序) / 执行用例记录.

    优先读 report 顶层契约字段(verdict/verdict_summary/risks/blockers/issues/cases);
    缺失则走 walk-substeps 兜底。"""

    sev_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    pri_rank = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}

    walked_issues: list[dict[str, Any]] = []
    walked_cases: list[dict[str, Any]] = []

    def _walk(x: Any) -> None:
        if isinstance(x, dict):
            if any(k in x for k in ("expected", "scenario")) and "id" in x:
                walked_cases.append(x)
            elif "severity" in x and any(k in x for k in ("issue", "title", "description", "name")):
                walked_issues.append(x)
            for v in x.values():
                _walk(v)
        elif isinstance(x, list):
            for v in x:
                _walk(v)

    _walk(report.get("substeps") or {})

    top_issues = report.get("issues") if isinstance(report.get("issues"), list) else walked_issues
    top_cases = report.get("cases") if isinstance(report.get("cases"), list) else walked_cases
    top_risks = report.get("risks") if isinstance(report.get("risks"), list) else []
    top_blockers = report.get("blockers") if isinstance(report.get("blockers"), list) else []

    sev_counts = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
    pri_counts = {"P0": 0, "P1": 0, "P2": 0, "P3": 0, "其他": 0}

    natural_issues: list[dict[str, Any]] = []
    for it in top_issues:
        sev = _normalize_severity(it.get("severity"))
        pri = str(it.get("priority") or "P2").upper()
        sev_counts[sev] = sev_counts.get(sev, 0) + 1
        title = (it.get("title") or it.get("name") or it.get("issue")
                 or it.get("description") or "未命名问题")
        module = (it.get("module") or it.get("endpoint") or it.get("file_path")
                  or it.get("location") or it.get("viewport") or it.get("page")
                  or it.get("viewport_filename") or "")
        repro = it.get("reproduce_steps") or []
        if isinstance(repro, str):
            repro = [repro]
        rcases = it.get("related_test_cases") or []
        if isinstance(rcases, str):
            rcases = [rcases]
        natural_issues.append({
            "issue_id": str(it.get("issue_id") or it.get("id") or "")[:50],
            "title": str(title)[:200],
            "severity": sev,
            "priority": pri,
            "module": str(module)[:200],
            "current": str(it.get("current_behavior") or it.get("current") or it.get("observed")
                           or it.get("description") or it.get("issue") or "")[:800],
            "expected": str(it.get("expected_behavior") or it.get("expected")
                            or it.get("requirement") or "")[:800],
            "fix": str(it.get("fix_suggestion") or it.get("fix") or it.get("recommendation")
                       or it.get("suggestion") or it.get("remediation") or "")[:1000],
            "repro": [str(s)[:200] for s in repro if s][:10],
            "accept": str(it.get("acceptance_criteria") or it.get("acceptance")
                          or it.get("verify") or "")[:600],
            "cases": [str(c)[:50] for c in rcases if c][:10],
            "owner": str(it.get("owner_role") or it.get("owner") or "").lower()[:30],
            "hours": it.get("estimated_hours") or it.get("effort"),
            "impact": str(it.get("impact_scope") or it.get("impact") or "")[:400],
            "evidence": str(it.get("evidence") or it.get("source") or "")[:400],
        })
    natural_issues.sort(key=lambda x: (sev_rank.get(x["severity"], 9), pri_rank.get(x["priority"], 9)))
    natural_issues = natural_issues[:60]

    natural_cases: list[dict[str, Any]] = []
    for c in top_cases:
        pri = str(c.get("priority") or "P2").upper()
        if pri in pri_counts:
            pri_counts[pri] += 1
        elif pri:
            pri_counts["其他"] += 1
        natural_cases.append({
            "id": str(c.get("id") or c.get("case_id") or c.get("tc_id") or "")[:50],
            "title": str(c.get("title") or c.get("name") or c.get("scenario") or "")[:200],
            "priority": pri,
            "type": str(c.get("type") or c.get("kind") or "").lower()[:30],
            "status": str(c.get("status") or "designed").lower()[:30],
            "automation": str(c.get("automation_tag") or c.get("automation") or "").lower()[:30],
            "expected": str(c.get("expected") or c.get("expected_result") or "")[:400],
        })
    natural_cases.sort(key=lambda x: pri_rank.get(x["priority"], 9))
    natural_cases = natural_cases[:200]

    natural_risks: list[dict[str, Any]] = []
    for r in top_risks:
        if isinstance(r, str):
            natural_risks.append({"title": r, "impact": "", "why": "", "severity": "medium"})
        elif isinstance(r, dict):
            natural_risks.append({
                "id": str(r.get("id") or "")[:30],
                "title": str(r.get("title") or r.get("name") or r.get("risk") or "未命名风险")[:200],
                "impact": str(r.get("impact") or r.get("affects") or "")[:400],
                "why": str(r.get("why") or r.get("reason") or r.get("detail") or "")[:400],
                "severity": _normalize_severity(r.get("severity") or "medium"),
            })
    if not natural_risks:
        gate = report.get("gate_decision") or {}
        for r in (gate.get("reasons") or []):
            if r:
                natural_risks.append({"title": str(r)[:200], "impact": "", "why": "", "severity": "medium"})

    natural_blockers: list[dict[str, Any]] = []
    for b in top_blockers:
        if isinstance(b, dict):
            natural_blockers.append({
                "id": str(b.get("id") or "")[:30],
                "title": str(b.get("title") or b.get("name") or "未命名阻碍")[:200],
                "why_blocking": str(b.get("why_blocking") or b.get("reason") or b.get("why") or "")[:500],
                "what_to_unblock": str(b.get("what_to_unblock") or b.get("action") or b.get("fix") or "")[:500],
                "owner_role": str(b.get("owner_role") or b.get("owner") or "").lower()[:30],
                "hours": b.get("estimated_hours") or b.get("effort"),
            })

    # verdict / verdict_summary 优先用 report 顶层
    verdict_text = report.get("verdict")
    verdict_summary = str(report.get("verdict_summary") or "")[:300]
    if verdict_text:
        v = str(verdict_text)
        if "不通过" in v:
            verdict, verdict_class = v, "fail"
        elif "有条件" in v or "警告" in v or "部分" in v:
            verdict, verdict_class = v, "warn"
        else:
            verdict, verdict_class = v, "pass"
    else:
        gate = report.get("gate_decision") or {}
        action = (gate.get("action") or "").lower()
        if "reject" in action or sev_counts["critical"] > 0:
            verdict, verdict_class = "不通过", "fail"
        elif "warn" in action or sev_counts["high"] > 2 or natural_blockers:
            verdict, verdict_class = "有条件通过", "warn"
        elif action == "" and not natural_issues and not natural_cases:
            verdict, verdict_class = "未产出", "skip"
        else:
            verdict, verdict_class = "通过", "pass"

    return {
        "测试结论": {"判定": verdict, "level": verdict_class, "agent": tool.get("name", ""), "summary": verdict_summary},
        "风险结论": natural_risks,
        "阻碍": natural_blockers,
        "问题描述": natural_issues,
        "用例执行": {
            "总数": len(natural_cases),
            "列表": natural_cases,
            "按优先级": pri_counts,
            "说明": "Agent 负责用例设计与方案输出；实际执行需由测试团队 / CI 落地。",
        },
        "严重度分布": sev_counts,
    }


@app.get("/api/reports/{run_id}/export.{fmt}")
async def api_report_export(run_id: str, fmt: str, request: Request) -> Any:
    """Server-side export — guarantees download dialog in webview.

    Replaces the JS Blob+<a download> path which can fail in pywebview.
    """
    if fmt not in ("json", "md", "html", "xlsx"):
        raise HTTPException(400, f"unsupported format: {fmt}")
    user = require_user(request)
    # Fetch report (memory or disk)
    if run_id in _RUNS:
        r = _RUNS[run_id]
        if not _user_can_see(user, r.get("owner_user_id")):
            raise HTTPException(403, "无权访问此报告")
        report = r.get("report") or {}
    else:
        out_dir = Path(settings.report_output_dir)
        candidate = next(out_dir.glob(f"*_{run_id}.json"), None) if out_dir.exists() else None
        if not candidate:
            raise HTTPException(404, f"report {run_id} not found")
        try:
            file_data = _json.loads(candidate.read_text(encoding="utf-8"))
        except Exception as exc:
            raise HTTPException(500, f"failed to read report: {exc}")
        owner_uid = (file_data.get("meta") or {}).get("owner_user_id")
        if not _user_can_see(user, owner_uid):
            raise HTTPException(403, "无权访问此报告")
        r = {
            "report": file_data,
            "run_id": run_id,
            "tool_id": _parse_tool_id_from_stem(candidate.stem, run_id),
        }
        report = file_data
    tool_id = r.get("tool_id", "?")
    tool = next((t for t in TOOL_CATALOG if t["id"] == tool_id), None) or {"id": tool_id, "name": tool_id, "icon": "?"}
    fname_base = f"{tool_id}_{run_id[:8]}"

    # step2 测试用例工具:产出只有 Excel,不出 HTML / Markdown 报告。
    # 直接请求 export.html / export.md 一律重定向到 Excel。
    if tool_id == "step2" and fmt in ("html", "md"):
        return RedirectResponse(f"/api/reports/{run_id}/export.xlsx", status_code=302)

    if fmt == "json":
        body = _json.dumps(report, ensure_ascii=False, indent=2)
        return Response(
            content=body, media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{fname_base}.json"'},
        )
    if fmt == "md":
        body = _build_markdown_report(r, tool, report)
        return Response(
            content=body, media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{fname_base}.md"'},
        )
    if fmt == "xlsx":
        # Excel 用例表 — 标准人工测试用例格式
        xlsx_bytes = _build_testcase_xlsx(r, tool, report)
        return Response(
            content=xlsx_bytes,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{fname_base}_testcases.xlsx"'},
        )
    # html
    body = _build_html_report(r, tool, report)
    return Response(
        content=body, media_type="text/html; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname_base}.html"'},
    )


def _build_testcase_xlsx(r: dict[str, Any], tool: dict[str, Any], report: dict[str, Any]) -> bytes:
    """把报告里的 cases 导出成标准人工测试用例 Excel。

    列:用例编号 / 所属模块 / 用例标题 / 优先级 / 用例类型 / 前置条件 /
        测试步骤 / 预期结果 / 备注 / 执行结果(留空给人填) / 实际结果(留空)
    步骤是自然语言数组,在单元格里按行展示。
    """
    import io as _io
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
        from openpyxl.utils import get_column_letter
    except ImportError as exc:
        raise HTTPException(500, f"openpyxl 未安装,无法导出 Excel: {exc}")

    cases = report.get("cases") or []
    # 兼容:contract 字段没提升时从 substeps 里捞
    if not cases:
        for sub in (report.get("substeps") or {}).values():
            if isinstance(sub, dict) and sub.get("cases"):
                cases = sub["cases"]
                break

    wb = Workbook()
    ws = wb.active
    ws.title = "测试用例"

    meta = report.get("meta") or {}
    tool_name = tool.get("name", tool.get("id", "?"))

    # ── 表头信息行 ──
    ws["A1"] = f"{tool_name} · 测试用例"
    ws["A1"].font = Font(size=14, bold=True)
    ws.merge_cells("A1:K1")
    info = f"项目:{meta.get('project_name') or '—'}　编号:{meta.get('project_code') or '—'}　" \
           f"生成:{meta.get('produced_at_utc') or '—'}　共 {len(cases)} 条用例"
    ws["A2"] = info
    ws["A2"].font = Font(size=10, color="555555")
    ws.merge_cells("A2:K2")

    # ── 列头 ──
    headers = ["用例编号", "所属模块", "用例标题", "优先级", "用例类型",
               "前置条件", "测试步骤", "预期结果", "备注", "执行结果", "实际结果"]
    header_row = 4
    hdr_fill = PatternFill("solid", fgColor="2E2E2E")
    hdr_font = Font(color="FFFFFF", bold=True, size=10)
    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for ci, h in enumerate(headers, 1):
        c = ws.cell(row=header_row, column=ci, value=h)
        c.fill = hdr_fill
        c.font = hdr_font
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.border = border

    # ── 列宽 ──
    widths = [16, 14, 30, 8, 10, 26, 40, 36, 18, 12, 24]
    for ci, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(ci)].width = w

    # 优先级配色
    pri_fill = {
        "P0": PatternFill("solid", fgColor="F4CCCC"),
        "P1": PatternFill("solid", fgColor="FCE5CD"),
        "P2": PatternFill("solid", fgColor="FFF2CC"),
        "P3": PatternFill("solid", fgColor="EFEFEF"),
    }

    def _steps_text(steps: Any) -> str:
        if isinstance(steps, list):
            out = []
            for i, s in enumerate(steps, 1):
                if isinstance(s, dict):
                    # 兼容旧 {order,action,data} 结构
                    txt = s.get("action") or s.get("step") or _json.dumps(s, ensure_ascii=False)
                    order = s.get("order", i)
                    out.append(f"{order}、{txt}")
                else:
                    s = str(s).strip()
                    # 已带序号的不重复加
                    if s and s[0].isdigit():
                        out.append(s)
                    else:
                        out.append(f"{i}、{s}")
            return "\n".join(out)
        return str(steps or "")

    def _expected_text(exp: Any) -> str:
        if isinstance(exp, list):
            out = []
            for e in exp:
                if isinstance(e, dict):
                    out.append(str(e.get("assert") or e.get("expected") or _json.dumps(e, ensure_ascii=False)))
                else:
                    out.append(str(e))
            return "\n".join(out)
        return str(exp or "")

    wrap = Alignment(wrap_text=True, vertical="top")
    center = Alignment(horizontal="center", vertical="center")
    row = header_row + 1
    for c in cases:
        if not isinstance(c, dict):
            continue
        vals = [
            c.get("id", ""),
            c.get("module", ""),
            c.get("title") or c.get("name") or c.get("scenario", ""),
            str(c.get("priority", "")).upper(),
            c.get("type", ""),
            c.get("preconditions", ""),
            _steps_text(c.get("steps")),
            _expected_text(c.get("expected")),
            c.get("remark", ""),
            "",  # 执行结果 — 留空给人填
            "",  # 实际结果 — 留空给人填
        ]
        for ci, v in enumerate(vals, 1):
            cell = ws.cell(row=row, column=ci, value=v)
            cell.border = border
            cell.alignment = center if ci in (1, 4, 5, 10) else wrap
        # 优先级单元格上色
        pr = str(c.get("priority", "")).upper()
        if pr in pri_fill:
            ws.cell(row=row, column=4).fill = pri_fill[pr]
        row += 1

    # 冻结表头
    ws.freeze_panes = f"A{header_row + 1}"

    if len(cases) == 0:
        ws.cell(row=header_row + 1, column=1, value="(本报告无测试用例)")

    buf = _io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _build_markdown_report(r: dict[str, Any], tool: dict[str, Any], report: dict[str, Any]) -> str:
    """中文 Markdown 报告，按 测试结论 / 风险结论 / 问题描述 / 用例执行 4 块组织。"""
    summary = _build_executive_summary(report, tool)
    lines: list[str] = []
    name = tool.get("name", tool.get("id", "?"))
    icon = tool.get("icon", "")
    lines.append(f"# {icon} {name} · 分析报告\n")

    rid = r.get("run_id") or "?"
    lines.append(f"- **Run ID**：`{rid}`")
    meta = report.get("meta", {})
    if meta.get("model_id"):
        lines.append(f"- **模型**：`{meta['model_id']}`")
    if meta.get("produced_at_utc"):
        lines.append(f"- **生成时间**：{meta['produced_at_utc']}")
    lines.append("")

    # 测试结论
    verdict = summary["测试结论"]["判定"]
    icon_map = {"pass": "✅", "warn": "⚠️", "fail": "❌", "skip": "—"}
    vlevel = summary["测试结论"]["level"]
    lines.append(f"## ① 测试结论\n")
    lines.append(f"**{icon_map.get(vlevel, '·')} {verdict}**\n")
    sev = summary["严重度分布"]
    lines.append(f"严重度分布：致命 {sev['critical']} · 高 {sev['high']} · 中 {sev['medium']} · 低 {sev['low']} · 信息 {sev['info']}\n")

    # 风险结论
    lines.append(f"## ② 风险结论\n")
    if summary["风险结论"]:
        for rk in summary["风险结论"]:
            lines.append(f"- {rk}")
    else:
        lines.append("- （无显著风险）")
    lines.append("")

    # 问题描述 — ticket 级
    lines.append(f"## ③ 问题描述\n")
    issues = summary["问题描述"]
    owner_map = {"backend": "后端", "frontend": "前端", "product": "产品",
                 "test": "测试", "devops": "运维", "security": "安全", "data": "数据"}
    if not issues:
        lines.append("（本次未识别到具体问题）\n")
    else:
        for idx, it in enumerate(issues, start=1):
            sev_icon = {"critical": "🔴", "high": "🟠", "medium": "🟡",
                        "low": "🟢", "info": "🔵"}.get(it["severity"], "·")
            head = f"### {idx}. {sev_icon} {it['title']}"
            if it.get("issue_id"):
                head = f"### {idx}. {sev_icon} `{it['issue_id']}` {it['title']}"
            lines.append(head + "\n")
            meta_parts = []
            if it.get("owner") and it["owner"] in owner_map:
                meta_parts.append(f"👤 {owner_map[it['owner']]}")
            if it.get("hours"):
                meta_parts.append(f"⏱ 估时 {it['hours']}h")
            if meta_parts:
                lines.append("> " + " · ".join(meta_parts) + "\n")
            if it.get("module"):
                lines.append(f"**位置**：`{it['module']}`\n")
            if it.get("current"):
                lines.append(f"**现状**：{it['current']}\n")
            if it.get("expected"):
                lines.append(f"**期望**：{it['expected']}\n")
            if it.get("fix"):
                lines.append(f"**修复建议**：{it['fix']}\n")
            if it.get("repro"):
                lines.append("**✅ 复现步骤**：\n")
                for ri, step in enumerate(it["repro"], 1):
                    lines.append(f"  {ri}. {step}")
                lines.append("")
            if it.get("accept"):
                lines.append(f"**🧪 验收标准**：{it['accept']}\n")
            if it.get("cases"):
                lines.append(f"**关联用例**：{', '.join('`'+c+'`' for c in it['cases'])}\n")
            if it.get("impact"):
                lines.append(f"**影响面**：{it['impact']}\n")
            if it.get("evidence"):
                lines.append(f"**证据**：{it['evidence']}\n")
            lines.append("---\n")

    # 用例执行
    cse = summary["用例执行"]
    lines.append(f"## ④ 用例执行情况\n")
    lines.append(f"- 共生成 **{cse['总数']}** 条用例")
    pri = cse["按优先级"]
    if any(pri.values()):
        lines.append(f"- 优先级分布：P0 {pri['P0']} · P1 {pri['P1']} · P2 {pri['P2']} · P3 {pri['P3']}"
                     + (f" · 其他 {pri['其他']}" if pri['其他'] else ""))
    lines.append(f"- 说明：{cse['说明']}\n")

    lines.append("---\n")
    lines.append(f"由 天枢·裁决 生成 · {tool.get('id','?')}\n")
    return "\n".join(lines)


def _build_html_report(r: dict[str, Any], tool: dict[str, Any], report: dict[str, Any]) -> str:
    """中文独立 HTML 报告（含截图、4 块结构、左上「← 返回」）。"""
    summary = _build_executive_summary(report, tool)
    name = (tool.get("name") or "?").replace("<", "&lt;")
    icon = tool.get("icon") or ""
    rid = r.get("run_id") or "?"
    meta = report.get("meta") or {}

    # Build screenshot section if any
    shots = (meta.get("screenshots") or [])
    valid_shots = [s for s in shots if not s.get("error") and s.get("filename")]
    shots_html = ""
    if valid_shots:
        # Embed each screenshot as base64 data URI (truly portable file)
        import base64 as _b64
        sd = Path(settings.evidence_output_dir) / "screenshots"
        rows = []
        groups: dict[str, list[dict[str, Any]]] = {}
        for s in valid_shots:
            groups.setdefault(s.get("url", ""), []).append(s)
        for url, arr in groups.items():
            cells = []
            for s in arr:
                fn = s.get("annotated_filename") or s.get("filename")
                p = sd / fn
                if not p.exists():
                    continue
                try:
                    if p.stat().st_size > 6 * 1024 * 1024:
                        continue
                    b64 = _b64.b64encode(p.read_bytes()).decode("ascii")
                except Exception:
                    continue
                ic = s.get("issue_count", 0)
                badge = f' <span class="issue-badge">{ic} 问题</span>' if ic else ""
                cells.append(
                    f'<div class="shot-cell">'
                    f'<img src="data:image/png;base64,{b64}" alt="{s.get("viewport","?")}"/>'
                    f'<div class="shot-cap">{s.get("viewport","?")} · {s.get("width","?")}×{s.get("height","?")}{badge}</div>'
                    f'</div>'
                )
            if cells:
                rows.append(
                    f'<div class="shot-group"><div class="shot-url"><code>{url}</code></div>'
                    f'<div class="shot-grid">{"".join(cells)}</div></div>'
                )
        if rows:
            shots_html = (
                '<section><h2><span class="num">⑤</span>页面截图证据</h2>'
                + "".join(rows)
                + '</section>'
            )

    # Verdict card
    vmap = {"pass": ("", "通过", "ok"), "warn": ("", "有条件通过", "warn"),
            "fail": ("", "不通过", "bad"), "skip": ("", "未产出", "skip")}
    vicon, vtext, vcls = vmap.get(summary["测试结论"]["level"], ("", "?", "skip"))

    # Risks — 现在风险是结构化 dict 列表
    def _esc_pre(s):
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    risks_list = summary["风险结论"]
    if risks_list:
        risk_items = []
        for rk in risks_list:
            sev = _normalize_severity(rk.get("severity") or "medium")
            badge = f'<span class="sev-tag sev-{sev}">{sev}</span>'
            line_impact = (
                f'<div class="risk-line"><span class="lbl">影响</span>{_esc_pre(rk.get("impact",""))}</div>'
                if rk.get("impact") else ""
            )
            line_why = (
                f'<div class="risk-line"><span class="lbl">原因</span>{_esc_pre(rk.get("why",""))}</div>'
                if rk.get("why") else ""
            )
            risk_items.append(
                f'<div class="risk-card">'
                f'<div class="risk-head">{badge}<span class="risk-title">{_esc_pre(rk.get("title",""))}</span></div>'
                f'{line_impact}{line_why}'
                f'</div>'
            )
        risks_html = '<div class="risk-list">' + "".join(risk_items) + '</div>'
    else:
        risks_html = "<p class='muted'>（无显著风险）</p>"

    # Issues — ticket 级 4 段卡片
    sev_map = {"critical": "🔴 致命", "high": "🟠 高", "medium": "🟡 中",
               "low": "🟢 低", "info": "🔵 信息"}
    owner_map = {"backend": "后端", "frontend": "前端", "product": "产品",
                 "test": "测试", "devops": "运维", "security": "安全", "data": "数据"}
    def _esc(s):
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def _render_issue_card(it: dict[str, Any], idx: int) -> str:
        meta_chips = []
        if it.get("issue_id"):
            meta_chips.append(f'<span class="meta-chip">{_esc(it["issue_id"])}</span>')
        if it.get("priority"):
            pri_v = str(it["priority"]).upper()
            meta_chips.append(f'<span class="meta-chip pri-tag pri-{pri_v}">{pri_v}</span>')
        if it.get("owner") and it["owner"] in owner_map:
            meta_chips.append(f'<span class="meta-chip role">{owner_map[it["owner"]]}</span>')
        if it.get("hours"):
            meta_chips.append(f'<span class="meta-chip">{it["hours"]}h</span>')
        repro_html = ""
        if it.get("repro"):
            repro_html = '<ol class="repro-list">' + "".join(
                f"<li>{_esc(s)}</li>" for s in it["repro"]
            ) + "</ol>"
        cases_html = ""
        if it.get("cases"):
            cases_html = '<div class="related-cases">关联用例:' + " ".join(
                f"<code>{_esc(c)}</code>" for c in it["cases"]
            ) + "</div>"
        sev_safe = _normalize_severity(it.get("severity"))
        sev_label_map = {"critical":"CRITICAL","high":"HIGH","medium":"MEDIUM","low":"LOW","info":"INFO"}
        return (
            f'<div class="issue-card sev-{sev_safe}">'
            f'<div class="issue-head">'
            f'<span class="sev-tag sev-{sev_safe}">{sev_label_map.get(sev_safe, sev_safe.upper())}</span>'
            f'<span class="issue-title">{_esc(it["title"])}</span>'
            f'</div>'
            + (f'<div class="issue-meta">{"".join(meta_chips)}</div>' if meta_chips else "")
            + (f'<div class="issue-loc">{_esc(it["module"])}</div>' if it.get("module") else "")
            + (f'<div class="issue-section"><div class="sec-lbl">现状</div><div class="sec-body">{_esc(it["current"])}</div></div>' if it.get("current") else "")
            + (f'<div class="issue-section"><div class="sec-lbl">期望</div><div class="sec-body">{_esc(it["expected"])}</div></div>' if it.get("expected") else "")
            + (f'<div class="issue-section fix"><div class="sec-lbl">修复建议</div><div class="sec-body">{_esc(it["fix"])}</div></div>' if it.get("fix") else "")
            + (f'<div class="issue-section verify"><div class="sec-lbl">验收</div><div class="sec-body">{repro_html}{("<div class=\"accept-line\">验收标准:"+_esc(it["accept"])+"</div>") if it.get("accept") else ""}</div></div>' if (repro_html or it.get("accept")) else "")
            + cases_html
            + (f'<div class="issue-impact">影响面:{_esc(it["impact"])}</div>' if it.get("impact") else "")
            + (f'<div class="issue-evidence">证据:{_esc(it["evidence"])}</div>' if it.get("evidence") else "")
            + '</div>'
        )

    if summary["问题描述"]:
        issue_cards = "".join(
            _render_issue_card(it, idx)
            for idx, it in enumerate(summary["问题描述"], start=1)
        )
    else:
        issue_cards = "<p class='muted'>（本次未识别到具体问题）</p>"

    # Blockers — 新增 section ③
    blockers_list = summary.get("阻碍") or []
    if blockers_list:
        b_items = []
        for i, b in enumerate(blockers_list, start=1):
            meta_chips = []
            if b.get("id"):
                meta_chips.append(f'<span class="meta-chip">{_esc(b["id"])}</span>')
            if b.get("owner_role") and b["owner_role"] in owner_map:
                meta_chips.append(f'<span class="meta-chip role">👤 {owner_map[b["owner_role"]]}</span>')
            if b.get("hours"):
                meta_chips.append(f'<span class="meta-chip">⏱ {b["hours"]}h</span>')
            why_html = (
                f'<div class="blocker-line"><span class="lbl">为何阻碍</span>{_esc(b.get("why_blocking",""))}</div>'
                if b.get("why_blocking") else ""
            )
            unblock_html = (
                f'<div class="blocker-line fix"><span class="lbl">如何解开</span>{_esc(b.get("what_to_unblock",""))}</div>'
                if b.get("what_to_unblock") else ""
            )
            b_items.append(
                f'<div class="blocker-card">'
                f'<div class="blocker-head"><span class="blocker-tag">BLOCKER</span>'
                f'<span class="blocker-title">{_esc(b.get("title",""))}</span>'
                f'{"".join(meta_chips)}</div>'
                f'{why_html}{unblock_html}'
                f'</div>'
            )
        blockers_html = '<div class="blocker-list">' + "".join(b_items) + '</div>'
    else:
        blockers_html = "<p class='muted'>（无阻碍）</p>"

    # Cases table
    cse = summary["用例执行"]
    pri = cse["按优先级"]
    case_list = cse.get("列表") or []
    pri_map = {"P0":"🚨 P0","P1":"🔥 P1","P2":"⚡ P2","P3":"·  P3"}
    status_map = {
        "designed": ("","已设计","muted"),
        "executed_pass": ("","已执行通过","ok"),
        "executed_fail": ("","执行失败","bad"),
        "skipped": ("","已跳过","muted"),
        "blocked": ("","阻塞","bad"),
    }
    if case_list:
        rows = []
        for i, c in enumerate(case_list, start=1):
            s_icon, s_label, s_cls = status_map.get(c.get("status",""), ("📝","未定义","muted"))
            ctype = c.get("type", "")
            cauto = c.get("automation", "")
            type_cell = f'<span class="case-type">{_esc(ctype)}</span>' if ctype else ""
            auto_cell = f'<span class="case-auto">{_esc(cauto)}</span>' if cauto else ""
            cpri = c.get("priority", "P2")
            rows.append(
                f'<tr class="case-row pri-{cpri}">'
                f'<td class="case-idx">{i}</td>'
                f'<td><span class="pri-tag pri-{cpri}">{pri_map.get(cpri, cpri)}</span></td>'
                f'<td><code class="case-id">{_esc(c.get("id",""))}</code></td>'
                f'<td class="case-title">{_esc(c.get("title",""))}</td>'
                f'<td>{type_cell}</td>'
                f'<td>{auto_cell}</td>'
                f'<td><span class="case-status case-status-{s_cls}">{s_label}</span></td>'
                f'</tr>'
            )
        case_table_html = (
            '<div class="case-table-wrap"><table class="case-table">'
            '<thead><tr><th>#</th><th>优先级</th><th>用例 ID</th><th>用例标题</th>'
            '<th>类型</th><th>自动化</th><th>状态</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>'
        )
    else:
        case_table_html = "<p class='muted'>（本次未生成用例）</p>"

    # 优先级堆叠条
    pri_total = sum(pri.values())
    pri_bar_html = ""
    if pri_total:
        segs = []
        for k in ("P0","P1","P2","P3"):
            n = pri.get(k, 0)
            if n:
                segs.append(f'<span class="pri-bar-seg pri-bar-{k}" style="flex:{n}" title="{k}·{n}">{k}: {n}</span>')
        if segs:
            pri_bar_html = '<div class="pri-bar">' + "".join(segs) + '</div>'

    # 严重度堆叠条 + KPI 卡(测试结论用)
    sev = summary["严重度分布"]
    sev_total = sum(sev.values())
    sev_summary = (
        f'致命 <strong>{sev["critical"]}</strong> · 高 <strong>{sev["high"]}</strong> · '
        f'中 <strong>{sev["medium"]}</strong> · 低 <strong>{sev["low"]}</strong> · '
        f'信息 <strong>{sev["info"]}</strong>'
    )
    sev_bar_html = ""
    if sev_total:
        segs = []
        for k in ("critical","high","medium","low","info"):
            n = sev.get(k, 0)
            if n:
                segs.append(f'<span class="sev-bar-seg sev-bar-{k}" style="flex:{n}" title="{k}·{n}">{n}</span>')
        if segs:
            sev_bar_html = '<div class="sev-bar">' + "".join(segs) + '</div>'

    verdict_summary_text = summary["测试结论"].get("summary", "")
    verdict_summary_html = (
        f'<div class="verdict-summary">{_esc(verdict_summary_text)}</div>'
        if verdict_summary_text else ""
    )
    kpi_html = (
        f'<div class="kpi-row">'
        f'<div class="kpi"><div class="num">{sev_total}</div><div class="lbl">问题总数</div></div>'
        f'<div class="kpi"><div class="num">{sev["critical"]+sev["high"]}</div><div class="lbl">需立即处理</div></div>'
        f'<div class="kpi"><div class="num">{len(blockers_list)}</div><div class="lbl">阻碍</div></div>'
        f'<div class="kpi"><div class="num">{cse["总数"]}</div><div class="lbl">用例</div></div>'
        f'</div>'
    )

    css = """
    /* === MUJI 暖深色 报告样式 — 精装本/古籍藏书 感 === */
    :root {
      --bg:#ffffff; --surface:#f0f0f0; --surface-2:#2e2920;
      --line:#c4c4c4; --line-2:#9e9e9e;
      --fg:#0a0a0a; --fg-2:#262626; --fg-3:#4a4a4a; --fg-4:#6e6e6e;
      --ink:#e8e3d5; --accent:#a8401f;
      --ok:#8aa56b; --warn:#a8893d; --bad:#c45a3a;
      --crit:#c45a3a; --hi:#d97a5a; --med:#a8893d; --lo:#7a9460; --info:#7896a2;
      --font-sans:'Noto Sans SC','PingFang SC',-apple-system,'Microsoft YaHei',sans-serif;
      --font-serif:'Noto Serif SC','Songti SC','STSong',Georgia,serif;
      --font-mono:'Inter','SF Mono',ui-monospace,Menlo,monospace;
    }
    *{box-sizing:border-box}
    html,body{margin:0;background:var(--bg);color:var(--fg);font-family:var(--font-sans);
      font-size:14px;line-height:1.65;-webkit-font-smoothing:antialiased;
      font-feature-settings:"ss01" on,"cv11" on}
    .container{max-width:880px;margin:0 auto;padding:32px 32px 48px}
    .nav{position:fixed;top:12px;left:12px;right:12px;display:flex;justify-content:space-between;
      pointer-events:none;z-index:50}
    .nav button{pointer-events:auto;background:#fff;color:var(--fg-2);
      border:1px solid var(--line);border-radius:6px;
      padding:6px 12px;font-size:12.5px;font-weight:500;cursor:pointer;
      box-shadow:0 1px 2px rgba(0,0,0,.04)}
    .nav button:hover{border-color:var(--line-2);color:var(--fg)}
    @media print{.nav{display:none}}
    .hero{padding:0 0 12px;margin-bottom:18px;border-bottom:1px solid var(--line)}
    .hero h1{margin:0 0 5px;font-size:22px;font-weight:600;letter-spacing:-.02em;color:var(--fg)}
    .hero .sub{font-family:var(--font-mono);font-size:11.5px;color:var(--fg-3)}
    .hero .sub code{background:transparent;color:var(--fg-2);padding:0}
    .hero .project-block{margin-top:10px;display:flex;flex-wrap:wrap;gap:18px;
      padding:8px 0 0;border-top:1px dashed var(--line)}
    .hero .project-row{display:flex;align-items:center;gap:8px;font-size:12.5px}
    .hero .project-row .lbl{color:var(--fg-3);font-size:11px;letter-spacing:.04em;
      text-transform:uppercase;font-weight:500}
    .hero .project-row code{background:var(--surface);padding:2px 8px;border-radius:4px;
      color:var(--fg);font-family:var(--font-mono);font-size:12px;font-weight:500}
    .hero .project-row .val{color:var(--fg);font-weight:500}
    section{padding:0 0 20px;margin-bottom:0;border-bottom:1px solid var(--line)}
    section:last-of-type{border-bottom:none}
    h2{font-size:12px;font-weight:600;margin:0 0 12px;display:flex;align-items:center;gap:10px;
      color:var(--fg-3);text-transform:uppercase;letter-spacing:.08em;
      padding-top:18px}
    h2 .num{display:inline-grid;place-items:center;width:22px;height:22px;border-radius:50%;
      background:var(--fg);color:#fff;font-size:11px;font-weight:600;font-family:var(--font-mono)}
    h2 .sec-count{margin-left:auto;font-family:var(--font-mono);font-size:11px;
      color:var(--fg-3);font-weight:500;letter-spacing:0;text-transform:none}
    h2 .sec-count.danger{color:var(--bad)}
    h2 .sec-hint{font-size:11px;color:var(--fg-4);font-weight:400;letter-spacing:0;text-transform:none}
    /* 测试结论 verdict — 卡片化 + 醒目大字 */
    .verdict{padding:16px 20px;display:flex;align-items:center;gap:14px;
      font-size:22px;font-weight:700;letter-spacing:-.01em;
      border:1px solid var(--line);border-left:4px solid var(--line-2);border-radius:0 8px 8px 0;
      background:var(--surface)}
    .verdict.ok{color:var(--ok);border-left-color:var(--ok);background:rgba(22,163,74,.05)}
    .verdict.warn{color:var(--warn);border-left-color:var(--warn);background:rgba(202,138,4,.05)}
    .verdict.bad{color:var(--bad);border-left-color:var(--bad);background:rgba(220,38,38,.05)}
    .verdict.skip{color:var(--fg-3)}
    .verdict-icon{font-size:20px}
    .verdict-summary{margin-top:12px;padding:14px 18px;font-size:14px;line-height:1.7;color:var(--fg);
      background:var(--surface);border:1px solid var(--line);border-radius:6px}
    .sev-strip{margin-top:12px;padding:10px 14px;font-size:12.5px;color:var(--fg-2);
      font-family:var(--font-mono);letter-spacing:.02em;background:var(--surface);
      border:1px solid var(--line);border-radius:6px}
    .sev-strip strong{color:var(--fg);font-weight:700}
    /* KPI 卡片 */
    .kpi-row{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:16px}
    .kpi{padding:16px 18px;background:var(--surface);border:1px solid var(--line);border-radius:8px;
      text-align:left}
    .kpi .num{font-size:30px;font-weight:700;color:var(--fg);font-family:var(--font-sans);
      font-variant-numeric:tabular-nums;letter-spacing:-.025em;line-height:1}
    .kpi .lbl{font-size:11.5px;color:var(--fg-2);text-transform:uppercase;letter-spacing:.08em;
      margin-top:8px;font-weight:600}
    /* 严重度分布条 — 加厚 */
    .sev-bar{display:flex;height:10px;border-radius:5px;overflow:hidden;margin-top:14px;
      background:var(--surface);border:1px solid var(--line)}
    .sev-bar-seg{display:block;font-size:0;line-height:0;min-width:0;transition:flex .2s}
    .sev-bar-critical{background:#dc2626}.sev-bar-high{background:#ea580c}
    .sev-bar-medium{background:#ca8a04}.sev-bar-low{background:#16a34a}.sev-bar-info{background:#0891b2}
    /* 优先级分布条 — 24px 带文字 */
    .pri-bar{display:flex;height:24px;border-radius:6px;overflow:hidden;margin-bottom:14px;margin-top:8px;
      background:var(--surface);border:1px solid var(--line)}
    .pri-bar-seg{display:flex;align-items:center;justify-content:center;color:#fff;
      font-size:11.5px;font-weight:700;padding:0 6px;letter-spacing:.04em}
    .pri-bar-P0{background:#dc2626}.pri-bar-P1{background:#ea580c}
    .pri-bar-P2{background:#ca8a04}.pri-bar-P3{background:#737373}
    /* sev/pri 药丸 tag */
    .sev-tag{font-size:10.5px;font-weight:700;padding:2px 8px;border-radius:4px;
      background:var(--surface);border:1px solid var(--line);
      color:var(--fg-2);font-family:var(--font-sans);letter-spacing:.06em;
      white-space:nowrap;flex-shrink:0;text-transform:uppercase}
    .sev-tag.sev-critical{color:#dc2626;background:rgba(220,38,38,.08);border-color:rgba(220,38,38,.30)}
    .sev-tag.sev-high{color:#ea580c;background:rgba(234,88,12,.08);border-color:rgba(234,88,12,.30)}
    .sev-tag.sev-medium{color:#ca8a04;background:rgba(202,138,4,.08);border-color:rgba(202,138,4,.30)}
    .sev-tag.sev-low{color:#16a34a;background:rgba(22,163,74,.08);border-color:rgba(22,163,74,.30)}
    .sev-tag.sev-info{color:#0891b2;background:rgba(8,145,178,.08);border-color:rgba(8,145,178,.30)}
    .pri-tag{font-size:10.5px;font-weight:700;padding:2px 8px;border-radius:4px;
      background:var(--surface);border:1px solid var(--line);
      font-family:var(--font-sans);color:var(--fg-2);letter-spacing:.04em;white-space:nowrap}
    .pri-tag.pri-P0{color:#dc2626;background:rgba(220,38,38,.08);border-color:rgba(220,38,38,.30)}
    .pri-tag.pri-P1{color:#ea580c;background:rgba(234,88,12,.08);border-color:rgba(234,88,12,.30)}
    .pri-tag.pri-P2{color:#ca8a04;background:rgba(202,138,4,.08);border-color:rgba(202,138,4,.30)}
    .pri-tag.pri-P3{color:#525252;background:var(--surface);border-color:var(--line)}
    /* 风险卡 — 左侧黄条 */
    .risk-list{display:flex;flex-direction:column;gap:10px}
    .risk-card{padding:12px 16px;background:var(--surface);border:1px solid var(--line);
      border-left:4px solid #ca8a04;border-radius:0 6px 6px 0}
    .risk-head{display:flex;align-items:baseline;gap:10px;margin-bottom:6px;flex-wrap:wrap}
    .risk-title{font-weight:700;color:var(--fg);font-size:14.5px;line-height:1.5}
    .risk-line{font-size:13px;color:var(--fg-2);line-height:1.7;margin-top:4px;
      display:flex;align-items:baseline;gap:8px}
    .risk-line .lbl{display:inline-block;min-width:48px;font-size:10.5px;color:var(--fg-3);
      font-weight:700;letter-spacing:.06em;text-transform:uppercase;flex-shrink:0;
      background:#f5f5f4;padding:2px 6px;border-radius:3px}
    /* 阻碍 — 红色卡片 */
    .blocker-list{display:flex;flex-direction:column;gap:10px}
    .blocker-card{padding:14px 16px;background:rgba(220,38,38,.04);border:1px solid rgba(220,38,38,.20);
      border-left:4px solid #dc2626;border-radius:0 6px 6px 0}
    .blocker-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:6px}
    .blocker-tag{font-size:10px;font-weight:700;padding:2px 8px;background:#dc2626;color:#fff;
      letter-spacing:.10em;text-transform:uppercase;border-radius:3px}
    .blocker-title{font-weight:700;color:var(--fg);font-size:14.5px;line-height:1.5;flex:1;min-width:0}
    .blocker-line{font-size:13px;color:var(--fg-2);line-height:1.7;margin-top:4px;
      display:flex;align-items:baseline;gap:8px}
    .blocker-line.fix{color:var(--fg)}
    .blocker-line .lbl{display:inline-block;min-width:66px;font-size:10.5px;color:var(--fg-3);
      font-weight:700;letter-spacing:.06em;text-transform:uppercase;flex-shrink:0;
      background:#f5f5f4;padding:2px 6px;border-radius:3px}
    .blocker-line.fix .lbl{color:#16a34a;background:rgba(22,163,74,.10)}
    /* Bug 卡 — 卡片化 + 左色条按严重度 */
    .issue-card{padding:14px 16px;margin-bottom:10px;background:var(--surface);
      border:1px solid var(--line);border-left:4px solid var(--line-2);border-radius:0 6px 6px 0}
    .issue-card:last-of-type{margin-bottom:0}
    .issue-card.sev-critical{border-left-color:#dc2626;background:rgba(220,38,38,.02)}
    .issue-card.sev-high{border-left-color:#ea580c;background:rgba(234,88,12,.02)}
    .issue-card.sev-medium{border-left-color:#ca8a04}
    .issue-card.sev-low{border-left-color:#16a34a}
    .issue-card.sev-info{border-left-color:#0891b2}
    .issue-head{display:flex;align-items:baseline;gap:10px;margin-bottom:6px;flex-wrap:wrap}
    .issue-title{font-weight:700;color:var(--fg);font-size:14.5px;line-height:1.5;flex:1;min-width:0}
    .issue-loc{color:var(--fg-3);font-size:12px;margin:4px 0;font-family:var(--font-mono);
      padding:3px 8px;background:#f5f5f4;border-radius:4px;display:inline-block}
    .issue-meta{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0 10px;
      font-size:11.5px;color:var(--fg-3);align-items:center}
    .issue-meta .meta-chip{font-size:11px;font-family:var(--font-mono);font-weight:600;
      padding:2px 8px;background:#f5f5f4;border:1px solid var(--line);
      color:var(--fg-2);border-radius:4px}
    .issue-meta .meta-chip.role{color:var(--fg);border-color:var(--line-2)}
    .issue-section{margin:8px 0 0;padding:10px 12px;background:#f5f5f4;
      border:1px solid var(--line);border-radius:6px}
    .issue-section.fix{background:rgba(22,163,74,.04);border-color:rgba(22,163,74,.20)}
    .issue-section.verify{background:rgba(8,145,178,.04);border-color:rgba(8,145,178,.20)}
    .issue-section .sec-lbl{font-size:10.5px;font-weight:700;color:var(--fg-3);
      margin-bottom:6px;letter-spacing:.08em;text-transform:uppercase}
    .issue-section.fix .sec-lbl{color:#16a34a}
    .issue-section.verify .sec-lbl{color:#0891b2}
    .issue-section .sec-body{padding-left:0;border-left:none;font-size:13.5px;color:var(--fg);line-height:1.7}
    .repro-list{margin:0;padding-left:20px;font-size:13.5px;line-height:1.7;color:var(--fg)}
    .accept-line{margin-top:6px;padding:10px 12px;background:rgba(8,145,178,.04);
      border:1px solid rgba(8,145,178,.20);border-radius:6px;font-size:13.5px;color:var(--fg);line-height:1.7}
    .accept-line::before{content:"验收: ";font-weight:700;color:#0891b2;font-size:11.5px;
      letter-spacing:.06em;text-transform:uppercase;margin-right:6px}
    .related-cases{margin-top:8px;font-size:12px;color:var(--fg-3);font-family:var(--font-mono);
      display:flex;flex-wrap:wrap;gap:6px;align-items:center}
    .related-cases::before{content:"关联用例";font-size:10.5px;font-weight:700;color:var(--fg-3);
      letter-spacing:.06em;text-transform:uppercase;font-family:var(--font-sans);margin-right:4px}
    .related-cases code{background:#f5f5f4;padding:2px 8px;color:var(--fg-2);
      font-size:11.5px;border:1px solid var(--line);border-radius:3px;margin-right:0}
    .issue-impact,.issue-evidence{margin-top:6px;font-size:12.5px;color:var(--fg-2);
      padding:6px 10px;background:#f5f5f4;border:1px solid var(--line);border-radius:4px;line-height:1.6}
    .issue-impact::before{content:"影响范围: ";font-weight:700;color:var(--fg-3);font-size:10.5px;
      letter-spacing:.06em;text-transform:uppercase}
    .issue-evidence::before{content:"证据: ";font-weight:700;color:var(--fg-3);font-size:10.5px;
      letter-spacing:.06em;text-transform:uppercase}
    /* 用例表 — 卡片化 + 斑马纹 */
    .case-table-wrap{margin-top:8px;border:1px solid var(--line);border-radius:8px;overflow-x:auto;
      background:var(--surface)}
    .case-table{width:100%;border-collapse:collapse;font-size:13.5px}
    .case-table th{padding:10px 12px;text-align:left;color:var(--fg);
      font-weight:700;font-size:11px;text-transform:uppercase;letter-spacing:.08em;
      border-bottom:1px solid var(--line);white-space:nowrap;background:#f5f5f4}
    .case-table td{padding:10px 12px;border-bottom:1px solid var(--line);vertical-align:top;
      color:var(--fg);font-size:13.5px;line-height:1.6}
    .case-table tr:last-child td{border-bottom:none}
    .case-table tr:nth-child(even) td{background:#fafaf9}
    .case-table tr.pri-P0 td{background:rgba(220,38,38,.04)}
    .case-table tr.pri-P1 td{background:rgba(234,88,12,.03)}
    .case-table .case-idx{font-family:var(--font-mono);color:var(--fg-3);font-size:12px;width:36px;font-weight:600}
    .case-table .case-id{background:#f5f5f4;padding:2px 8px;color:var(--fg);font-size:12px;
      font-family:var(--font-mono);border:1px solid var(--line);border-radius:4px;display:inline-block}
    .case-table .case-title{max-width:420px;line-height:1.55;color:var(--fg);font-size:14px;font-weight:500}
    .case-type,.case-auto{font-size:11px;padding:2px 8px;border-radius:4px;font-weight:600;
      background:#f5f5f4;color:var(--fg-2);font-family:var(--font-sans);white-space:nowrap;
      border:1px solid var(--line);display:inline-block}
    .case-status{font-size:12px;font-family:var(--font-sans);font-weight:600;white-space:nowrap;
      color:var(--fg-3);padding:2px 8px;background:#f5f5f4;border:1px solid var(--line);
      border-radius:4px;display:inline-block}
    .case-status-ok{color:#16a34a;background:rgba(22,163,74,.08);border-color:rgba(22,163,74,.30)}
    .case-status-bad{color:#dc2626;background:rgba(220,38,38,.08);border-color:rgba(220,38,38,.30)}
    .case-status-muted{color:var(--fg-3)}
    .case-note{margin-top:12px;padding:10px 14px;background:#f5f5f4;border:1px solid var(--line);
      border-left:3px solid var(--accent);border-radius:0 6px 6px 0;
      color:var(--fg-2);font-size:13px;line-height:1.65}
    /* 通用 */
    code{background:var(--surface);padding:1px 6px;border-radius:3px;
      font-family:var(--font-mono);font-size:12.5px;color:var(--fg-2)}
    p.muted{color:var(--fg-3);font-size:13px;margin:0}
    ul.clean{list-style:none;padding:0;margin:0}
    /* 截图 */
    .shot-group{margin-top:18px}
    .shot-group:first-child{margin-top:0}
    .shot-url{font-family:var(--font-mono);font-size:12px;color:var(--fg-3);margin-bottom:10px}
    .shot-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:14px}
    .shot-cell{background:transparent;border:1px solid var(--line);border-radius:6px;overflow:hidden}
    .shot-cell img{display:block;width:100%;height:auto;max-height:320px;object-fit:cover}
    .shot-cap{padding:8px 12px;font-size:11.5px;font-family:var(--font-mono);color:var(--fg-3);
      border-top:1px solid var(--line);display:flex;align-items:center;justify-content:space-between}
    .issue-badge{color:var(--bad);font-size:11px;font-weight:500}
    body{padding-top:48px}
    .footer{margin-top:24px;padding-top:14px;border-top:1px solid var(--line);
      text-align:center;font-size:11.5px;color:var(--fg-4)}
    /* === 报告页脚:logo + 天枢 · 裁决 === */
    .brand-footer{margin-top:48px;padding:24px 0 8px;border-top:1px solid var(--line);
      display:flex;align-items:center;justify-content:space-between;
      font-family:var(--font-mono);font-size:11px;color:var(--fg-3);
      letter-spacing:.04em}
    .brand-footer .brand-mark{display:flex;align-items:center;gap:10px;color:var(--accent)}
    .brand-footer .brand-mark svg{display:block;opacity:.92}
    .brand-footer .brand-text{font-family:var(--font-serif);font-size:16px;
      font-weight:600;color:var(--ink);letter-spacing:.18em}
    .brand-footer .brand-sep{color:var(--accent);margin:0 4px}
    .brand-footer .brand-meta{font-family:var(--font-mono);font-size:10.5px;
      color:var(--fg-3);letter-spacing:.08em;text-transform:uppercase}
    @media (max-width:720px){.kpi-row{grid-template-columns:repeat(2,1fr);gap:16px}
      .brand-footer{flex-direction:column;gap:10px;align-items:flex-start}}
    """

    return f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{name} · 分析报告</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@300;400;500;600;700&family=Noto+Sans+SC:wght@300;400;500;600&family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>{css}</style>
</head><body>
<div class="nav">
<button onclick="(function(){{if(history.length>1){{history.back();}}else if(window.opener){{window.close();}}else{{window.scrollTo({{top:0,behavior:'smooth'}});}}}})()">← 返回</button>
<button onclick="window.scrollTo({{top:0,behavior:'smooth'}})">↑ 顶部</button>
</div>
<div class="container">
<div class="hero">
<h1>{name}</h1>
<div class="sub">{meta.get('produced_at_utc','—')} · <code>{rid}</code></div>
{f'<div class="project-block"><div class="project-row"><span class="lbl">项目编号</span><code>{_esc(meta.get("project_code") or "—")}</code></div><div class="project-row"><span class="lbl">项目名称</span><span class="val">{_esc(meta.get("project_name") or "—")}</span></div></div>' if (meta.get("project_code") or meta.get("project_name")) else ''}
</div>

<section>
<h2><span class="num">1</span>测试结论</h2>
<div class="verdict {vcls}"><span>{vtext}</span></div>
{verdict_summary_html}
{kpi_html}
{sev_bar_html}
<div class="sev-strip">{sev_summary}</div>
</section>

<section>
<h2><span class="num">2</span>风险结论 <span class="sec-count">{len(risks_list)}</span></h2>
{risks_html}
</section>

<section>
<h2><span class="num">3</span>阻碍 <span class="sec-count danger">{len(blockers_list)}</span></h2>
{blockers_html}
</section>

<section>
<h2><span class="num">4</span>Bug 表 <span class="sec-count">{len(summary["问题描述"])}</span> <span class="sec-hint">按严重度 × 优先级排序</span></h2>
{issue_cards}
</section>

<section>
<h2><span class="num">5</span>执行用例记录 <span class="sec-count">{cse['总数']}</span></h2>
{pri_bar_html}
{case_table_html}
<div class="case-note">{cse['说明']}</div>
</section>

{shots_html}

<footer class="brand-footer">
  <div class="brand-mark">
    <svg viewBox="0 0 24 24" fill="currentColor" width="24" height="24" aria-hidden="true" style="filter:drop-shadow(0 1px 2px rgba(212,103,74,.2))">
      <path d="M12 1.5L13.4 10.6L22.5 12L13.4 13.4L12 22.5L10.6 13.4L1.5 12L10.6 10.6Z"/>
    </svg>
    <span class="brand-text">天枢 <span class="brand-sep">·</span> 裁决</span>
  </div>
  <div class="brand-meta">
    AI 裁决手册   ·   {tool.get('id','?')}   ·   Run {(r.get('run_id') or '')[:12]}
  </div>
</footer>
</div>
</body></html>"""


@app.get("/api/screenshots/{filename}")
async def api_screenshot_file(filename: str) -> Any:
    """Serve a captured screenshot PNG. Filename is the stored name from the
    report's `meta.screenshots[*].filename`."""
    # Defense in depth: no path traversal
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(400, "invalid filename")
    p = Path(settings.evidence_output_dir) / "screenshots" / filename
    if not p.exists():
        raise HTTPException(404, f"{filename} not found")
    return FileResponse(p, media_type="image/png")


def _annotate_screenshots(report_dump: dict[str, Any], shots: list[dict[str, Any]]) -> None:
    """Walk substep outputs for issue items with bbox + viewport_filename, draw
    red rectangles + numeric labels onto a copy of each screenshot, and add the
    annotated filenames into shots[*].annotated_filename so the report renderer
    can show them.

    The LLM is asked to emit issues in the shape:
        {"viewport_filename": <str>, "bbox": [x, y, w, h], "issue": "...", "severity": ...}
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return
    if not shots:
        return

    # Aggregate issues per filename
    issues_by_file: dict[str, list[dict[str, Any]]] = {}
    def _walk(x: Any) -> None:
        if isinstance(x, dict):
            fn = x.get("viewport_filename") or x.get("filename")
            bbox = x.get("bbox")
            if fn and isinstance(bbox, (list, tuple)) and len(bbox) == 4:
                issues_by_file.setdefault(str(fn), []).append({
                    "bbox": [int(b) for b in bbox],
                    "issue": str(x.get("issue") or x.get("title") or "")[:200],
                    "severity": _normalize_severity(x.get("severity")),
                })
            for v in x.values():
                _walk(v)
        elif isinstance(x, list):
            for v in x:
                _walk(v)
    _walk(report_dump.get("substeps", {}))

    if not issues_by_file:
        return

    out_dir = Path(settings.evidence_output_dir) / "screenshots"
    sev_color = {
        "critical": (220, 38, 38, 255),
        "high":     (220, 38, 38, 255),
        "major":    (220, 38, 38, 255),
        "medium":   (217, 119, 6, 255),
        "low":      (234, 179, 8, 255),
        "minor":    (234, 179, 8, 255),
        "cosmetic": (234, 179, 8, 255),
    }

    for shot in shots:
        fn = shot.get("filename")
        if not fn or fn not in issues_by_file:
            continue
        src = out_dir / fn
        if not src.exists():
            continue
        try:
            img = Image.open(src).convert("RGBA")
            draw = ImageDraw.Draw(img, "RGBA")
            for idx, it in enumerate(issues_by_file[fn], start=1):
                x, y, w, h = it["bbox"]
                color = sev_color.get(it["severity"], (217, 119, 6, 255))
                # Outer box
                for off in (3, 2, 1, 0):
                    draw.rectangle([x - off, y - off, x + w + off, y + h + off],
                                   outline=color, width=1)
                # Number badge top-left
                badge_size = 28
                badge_box = [x, y, x + badge_size, y + badge_size]
                draw.rectangle(badge_box, fill=color)
                try:
                    fnt = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 16)
                except Exception:
                    fnt = ImageFont.load_default()
                draw.text((x + 8, y + 4), str(idx), fill=(255, 255, 255), font=fnt)
            ann_fn = src.stem + "_annotated.png"
            ann_path = out_dir / ann_fn
            img.save(ann_path, "PNG", optimize=True)
            shot["annotated_filename"] = ann_fn
            shot["issue_count"] = len(issues_by_file[fn])
        except Exception:
            continue


async def _run_tool_async(
    *, run_id: str, tool: dict[str, Any], inputs: dict[str, Any], tenant: Tenant
) -> None:
    """Background task — execute orchestrator and stash result in _RUNS."""
    state = _RUNS[run_id]
    state["status"] = "running"
    state["progress"] = "构造执行上下文…"
    # Real-time log stream — frontend polls this list and renders entries
    state.setdefault("logs", [])

    def _emit(event: str, fields: dict[str, Any] | None = None, **kwargs: Any) -> None:
        # Accept both `_emit("ev", {"k":v})` (StepContext callback) and
        # `_emit("ev", k=v)` (direct kwargs from this file).
        if fields is None:
            fields = kwargs
        elif kwargs:
            fields = {**fields, **kwargs}
        entry = {"ts": _time.time(), "event": event, **fields}
        state["logs"].append(entry)
        # Cap log size to avoid memory bloat on long runs
        if len(state["logs"]) > 500:
            del state["logs"][:100]
        # Mirror substep.start / substep.done into the visible "progress" line
        if event == "substep.start":
            sub = fields.get("sub_id", "?")
            name = fields.get("name", "")
            state["progress"] = f"执行子步骤 {sub} · {name}"
        elif event == "substep.done":
            sub = fields.get("sub_id", "?")
            tokens = fields.get("output_tokens", 0)
            state["progress"] = f"完成 {sub} · {tokens:,} tokens"
    try:
        # Strip and apply model/effort/thinking overrides from the request.
        model_override = inputs.pop("__model", None)
        effort_override = inputs.pop("__effort", None)
        thinking_override = inputs.pop("__thinking", None)
        # Per-substep enable list (UI checkboxes). If empty list → run nothing,
        # which usually means user-error so we treat it as "all enabled".
        enabled_substeps = inputs.pop("__enabled_substeps", None)
        # Map the simplified UI form to orchestrator-expected keys.
        inputs = _shape_orchestrator_inputs(tool, inputs)
        if isinstance(enabled_substeps, list) and enabled_substeps:
            inputs["__enabled_substeps"] = enabled_substeps
        # Step 6 needs an ExecutionEnvironment object (dataclass; can't go in JSON body)
        if tool["id"] == "step6":
            inputs.setdefault("execution_env", ExecutionEnvironment(
                evidence_dir=Path(settings.evidence_output_dir) / "step6",
                dry_run=bool(inputs.get("dry_run", True)),
            ))
        ctx = await make_context(tenant, inputs)
        ctx.log_callback = _emit
        # Resolve dropdown key (e.g. "opus-4-7-1m") → actual model + betas.
        # Backwards-compat: bare aliases ("opus"/"sonnet"/"haiku") also accepted.
        model_id: str | None = None
        betas: list[str] = []
        if model_override:
            entry = _model_by_key(model_override)
            if entry:
                model_id = entry["model"]
                betas = list(entry.get("betas") or [])
            else:
                model_id = model_override  # treat as raw alias / model name
        if model_id or effort_override or thinking_override or betas:
            ctx.llm = LlmClient(
                model_override=model_id,
                effort=effort_override or None,
                thinking=thinking_override or None,
                betas=betas,
            )
        # 记下本次用的模型 key,失败时 except 分支用它做运行时黑名单
        state["model_key"] = model_override
        state["model_id"] = model_id
        # 跑之前确保 OAuth token 没过期 — 过期会让所有 LLM 调用拿 401
        try:
            refresh_result = await ensure_fresh_oauth_token()
            if refresh_result.get("status") == "refreshed":
                _emit("oauth.refreshed", detail=refresh_result.get("detail", ""))
            elif refresh_result.get("status") in ("no_refresh_token", "error"):
                _emit("oauth.warn", detail=refresh_result.get("detail", ""))
        except Exception as exc:
            _emit("oauth.warn", detail=f"token 刷新检查异常: {exc}")
        _emit("run.start", tool_id=tool["id"], tool_name=tool["name"])
        # ctx.run_id is independent of our public run_id; we keep ours for tracking
        state["ctx_run_id"] = ctx.run_id
        from packages.workflow.generic import (
            AgentExecutionOrchestrator,
            ApiTestOrchestrator,
            H5AdaptOrchestrator,
            NetworkResilienceOrchestrator,
            RequirementReviewOrchestrator,
            SeoAuditOrchestrator,
            TestCaseDesignOrchestrator,
            UiConsistencyOrchestrator,
        )
        orch_cls = {
            # 全部走 DirectChainOrchestrator —— 每个 substep 独立测试
            "step1": RequirementReviewOrchestrator,
            "step2": TestCaseDesignOrchestrator,
            "step4": ApiTestOrchestrator,
            "step5": UiConsistencyOrchestrator,
            "step6": AgentExecutionOrchestrator,
            "network_resilience": NetworkResilienceOrchestrator,
            "seo_audit": SeoAuditOrchestrator,
            "h5_adapt": H5AdaptOrchestrator,
        }[tool["id"]]
        # For UI-related tools, capture page screenshots before LLM analysis.
        # Attaches PNG paths to ctx so run_substep can pass them as image
        # content blocks (Claude will then actually SEE the page).
        shots = await _capture_screenshots_for_tool(tool["id"], ctx, state)
        if shots:
            state["screenshots"] = shots
            ctx.screenshots = shots  # consumed by base.run_substep
            # Append a summary so the prompt also names the screenshots in text.
            docs = (ctx.inputs or {}).get("documents") or ""
            if docs:
                lines = ["", "", "## 已采集的页面截图（已作为 image content 附在本次请求中）"]
                ok_shots = [s for s in shots if not s.get("error")]
                if ok_shots:
                    for s in ok_shots:
                        lines.append(f"- {s['url']} @ {s['viewport']} ({s['width']}×{s['height']})")
                err_shots = [s for s in shots if s.get("error")]
                if err_shots:
                    lines.append("")
                    lines.append("(以下 URL 截图失败，请在分析时标注无法获取真实视图)")
                    for s in err_shots:
                        lines.append(f"- {s['url']} @ {s['viewport']}: {s['error'][:100]}")
                ctx.inputs["documents"] = docs + "\n".join(lines)

        state["progress"] = "调用 Claude（本地客户端）…"
        report = await orch_cls(ctx).execute()
        # Persist a JSON copy alongside the run-id for download
        out_dir = Path(settings.report_output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        report_dump = report.model_dump(mode="json")
        # 把 run 持有人写到 meta — 持久化跨重启可用,/api/reports 据此过滤
        report_meta = report_dump.setdefault("meta", {})
        if state.get("owner_user_id"):
            report_meta["owner_user_id"] = state["owner_user_id"]
            report_meta["owner_username"] = state.get("owner_username")
        # Attach screenshot metadata so HTML/MD renderers can embed them
        if state.get("screenshots"):
            shots_list = state["screenshots"]
            # Phase C: annotate originals with red boxes for any LLM-reported bbox
            try:
                _annotate_screenshots(report_dump, shots_list)
            except Exception:
                pass
            report_meta["screenshots"] = shots_list
        report_path = out_dir / f"{tool['id']}_{ctx.run_id}.json"
        report_path.write_text(
            _json.dumps(report_dump, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        state["progress"] = "完成"
        state["report"] = report_dump
        state["report_path"] = str(report_path)
        state["usage"] = {
            "input_tokens": ctx.usage.input_tokens,
            "output_tokens": ctx.usage.output_tokens,
            "cache_read_tokens": ctx.usage.cache_read_tokens,
            "cost_usd": round(ctx.usage.cost_usd, 4),
        }
        state["status"] = "succeeded"
    except Exception as exc:
        state["status"] = "failed"
        # 给前端可读的一句话:常见根因(模型不可用 / 未登录 / 网络)抽出来作业务语言
        raw = f"{type(exc).__name__}: {exc}"
        low = raw.lower()
        hint = None
        if "haiku-4-5" in low or "claude-haiku-4-5" in low:
            hint = "Haiku 4.5 调用失败 — 该模型仍在受限灰度，请确认当前账号已开通；或先切回 Sonnet 4.6 验证工具链。"
        elif "authnotconfigured" in low or "未配置" in raw or "未登录" in raw:
            hint = "认证未就绪 — 请到「设置 → 模型接入」选择 OAuth 或填 API Key 后重试。"
        elif "sdk invocation failed" in low or "claude sdk" in low:
            hint = "Claude SDK 调用失败 — 通常是模型不可用、配额已用尽或本地 CLI 未登录。请先在「设置」做一次模型探测。"
        state["error"] = f"{hint}\n\n{raw}" if hint else raw
        state["traceback"] = _tb.format_exc()
        # 失败模型加入会话黑名单 → 下次 /api/claude/info 返回时 available=false,
        # 下拉自动 disable, 用户不会继续踩同一个模型。
        try:
            failing_model = inputs.get("__model") if isinstance(inputs, dict) else None
            if not failing_model:
                # 走过 inputs.pop 之后 __model 已经被消费,从 state 里拿
                failing_model = state.get("model_key")
            if failing_model:
                _record_model_failure(failing_model, raw)
        except Exception:
            pass
        _emit("run.failed", error=str(exc)[:200])
    finally:
        state["finished_at"] = _time.time()
        if state["status"] == "succeeded":
            _emit("run.done", cost_usd=state.get("usage", {}).get("cost_usd"))


@app.post("/api/tools/{tool_id}/run")
async def api_tool_run(
    tool_id: str,
    body: dict[str, Any],
    request: Request,
    x_tenant_id: str | None = Header(default="default"),
    x_project_id: str | None = Header(default=None),
) -> dict[str, Any]:
    """异步启动一个工具，立即返回 run_id 用于轮询。

    入口闸：用户没在「设置 → 认证模式」选过连接方式（mode == 'unset'）就拒绝
    跑工具，给清晰的 412 错误。前端 toast 出来直接引导去设置页。
    """
    tool = next((t for t in TOOL_CATALOG if t["id"] == tool_id), None)
    if not tool:
        raise HTTPException(404, f"unknown tool: {tool_id}")

    # 全局并发闸:同一工具同一时间只能跑一个 run。
    # 多人同时按运行 → 第二位拿到 409 + 触发者用户名。前端 toast。
    for _r in _RUNS.values():
        if _r.get("tool_id") == tool_id and _r.get("status") in ("queued", "running"):
            owner = _r.get("owner_username") or "其他用户"
            raise HTTPException(
                409,
                f"该工具正被 {owner} 运行,请等它完成再试 (进度:{_r.get('progress') or '初始化'})",
            )

    # 认证闸：未配置/未登录直接 412 拒绝
    # 三种情况都要拦：
    #   - mode=unset            → 用户没选过任何模式
    #   - mode=oauth + 未登录    → 选了 OAuth 但还没浏览器走完授权
    #   - mode=api_key + 无 key  → 选了 API Key 但没填
    try:
        from packages.core.auth_config import (
            get_api_key, get_auth_mode, get_oauth_access_token,
        )
        mode = get_auth_mode()
        if mode == "unset":
            raise HTTPException(
                412,
                "未选择连接方式 — 请到「设置 → 模型接入」选择 OAuth 或 API Key 后再试",
            )
        if mode == "oauth" and not get_oauth_access_token():
            raise HTTPException(
                412,
                "OAuth 模式但还未完成授权 — 请到「设置 → 模型接入」点「OAuth 授权」完成浏览器授权",
            )
        if mode == "api_key" and not get_api_key():
            raise HTTPException(
                412,
                "API Key 模式未填 Key — 请到「设置 → 模型接入」粘贴 sk-ant-... 或换其他模式",
            )
    except HTTPException:
        raise
    except Exception:
        pass  # 配置模块异常不阻塞跑工具，让 LLM 客户端自己抛

    # 强制必填:项目编号 + 项目名称
    project_code = str(body.get("project_code", "") or "").strip()
    project_name = str(body.get("project_name", "") or "").strip()
    if not project_code:
        raise HTTPException(400, "项目编号 (project_code) 必填")
    if not project_name:
        raise HTTPException(400, "项目名称 (project_name) 必填")
    # 简单长度校验
    if len(project_code) > 64:
        raise HTTPException(400, "项目编号过长 (最多 64 字符)")
    if len(project_name) > 128:
        raise HTTPException(400, "项目名称过长 (最多 128 字符)")

    project_id = x_project_id or body.get("project_id") or project_code or "tools-portal"
    tenant = Tenant(tenant_id=x_tenant_id or "default", project_id=project_id)

    # Strip UI-only keys before passing as orchestrator inputs;
    # project_code/name 透传给 orchestrator,后续会被写入 report.meta
    helper_keys = {"project_id"}  # project_code/name 保留给 orchestrator 用
    inputs = {k: v for k, v in body.items() if k not in helper_keys}

    # 把当前登录用户钉到 run 上,用于报告隔离
    current_user = getattr(request.state, "current_user", None)
    owner_user_id = current_user.id if current_user else None
    owner_username = current_user.username if current_user else None

    run_id = str(uuid4())
    _RUNS[run_id] = {
        "run_id": run_id,
        "tool_id": tool_id,
        "tool_name": tool["name"],
        "status": "queued",
        "progress": "等待执行…",
        "started_at": _time.time(),
        "finished_at": None,
        "tenant_id": tenant.tenant_id,
        "project_id": tenant.project_id,
        "project_code": project_code,
        "project_name": project_name,
        "owner_user_id": owner_user_id,
        "owner_username": owner_username,
    }
    # Fire-and-forget; client polls /api/tools/runs/{run_id}
    _asyncio.create_task(_run_tool_async(
        run_id=run_id, tool=tool, inputs=inputs, tenant=tenant
    ))
    return {"run_id": run_id, "status": "queued"}


def _promote_contract_fields(report: dict[str, Any]) -> dict[str, Any]:
    """读时把 finalize substep 的统一报告契约字段提升到 report 顶层。

    历史 run 是旧 Python 代码跑的，contract 字段只存在于 substeps[finalize_id] 里。
    这里做无副作用的"读时 patch"，让 UI 不用重跑就能看到完整 5 段报告。
    新版 GenericReport 已经在 execute() 里直接写到顶层，此函数对新报告也保持幂等。
    """
    if not isinstance(report, dict):
        return report
    subs = report.get("substeps") or {}
    if not isinstance(subs, dict) or not subs:
        return report
    # 找最后一个非空 substep 当 final
    final: dict[str, Any] = {}
    for sid in list(subs.keys())[::-1]:
        v = subs.get(sid)
        if isinstance(v, dict) and v:
            final = v
            break
    if not final:
        return report

    def merge_list(field: str) -> list[Any]:
        # 永远合并所有 substep 的该数组(去重靠 ID)。
        # 不能"顶层非空就不动"—— finalize 只贡献自己那部分用例时,
        # 会漏掉前 4 个子步骤的用例(实测 step2:8 vs 应有 57)。
        merged: list[Any] = []
        seen_ids: set[str] = set()
        no_id_items: list[Any] = []
        for sid in subs:
            out = subs.get(sid)
            if not isinstance(out, dict):
                continue
            for item in (out.get(field) or []):
                key = ""
                if isinstance(item, dict):
                    key = str(item.get("id") or item.get("issue_id") or item.get("case_id") or "")
                if key:
                    if key in seen_ids:
                        continue
                    seen_ids.add(key)
                    merged.append(item)
                else:
                    no_id_items.append(item)  # 无 ID 的不能去重,但也不丢
        merged.extend(no_id_items)
        # 如果合并结果为空但顶层本来有(旧报告无 substeps 的情况),保留顶层
        cur = report.get(field)
        if not merged and isinstance(cur, list) and cur:
            return cur
        return merged

    if not report.get("verdict") and isinstance(final.get("verdict"), str):
        report["verdict"] = final["verdict"]
    if not report.get("verdict_summary") and isinstance(final.get("verdict_summary"), str):
        report["verdict_summary"] = final["verdict_summary"]
    report["risks"] = merge_list("risks")
    report["blockers"] = merge_list("blockers")
    report["issues"] = merge_list("issues")
    report["cases"] = merge_list("cases")
    return report


@app.get("/api/tools/runs/{run_id}")
async def api_tool_run_status(run_id: str, request: Request) -> dict[str, Any]:
    state = _RUNS.get(run_id)
    if not state:
        raise HTTPException(404, f"run {run_id} not found (server may have restarted)")
    user = require_user(request)
    if not _user_can_see(user, state.get("owner_user_id")):
        raise HTTPException(403, "无权访问此运行")
    # 读时把契约字段提升到 report 顶层（无副作用 / 幂等）
    if isinstance(state.get("report"), dict):
        state["report"] = _promote_contract_fields(state["report"])
    return state


def _user_can_see(user: UserRecord, owner_user_id: int | None) -> bool:
    """权限判定:admin 可看所有;user 只能看自己的;legacy (owner_user_id 缺失) 当作 admin 可见。"""
    if user.is_admin():
        return True
    if owner_user_id is None:
        return False  # 旧数据,普通用户看不到
    return int(owner_user_id) == int(user.id)


@app.get("/api/tools/{tool_id}/active")
async def api_tool_active_status(tool_id: str, request: Request) -> dict[str, Any]:
    """查询某个工具是否正在被任意用户运行(全局锁状态)。

    设计:同一工具同时只能跑一个 — 因为底层共用一份 Claude 凭据 + 同一个进程,
    并发跑会触发 Anthropic 速率限制 + 资源争用。所以 UI 侧锁掉「运行」按钮,
    任何人打开页面都看到「运行中 · 由 xxx 触发」。
    完成后此端点 2-3 秒内会返回 active=False,前端轮询自动解锁。

    所有登录用户都能查 — 只暴露 tool/run_id/owner_username/started_at/progress,
    不暴露报告内容,所以不需要 _user_can_see 过滤。
    """
    require_user(request)
    for r in _RUNS.values():
        if r.get("tool_id") != tool_id:
            continue
        if r.get("status") not in ("queued", "running"):
            continue
        return {
            "active": True,
            "run_id": r.get("run_id"),
            "started_at": r.get("started_at"),
            "owner_username": r.get("owner_username"),
            "progress": r.get("progress"),
        }
    return {"active": False}


@app.get("/api/tools/runs")
async def api_tool_run_list(request: Request) -> dict[str, Any]:
    """List recent in-process runs (memory only — clears on server restart).
    普通用户只看自己的;admin 看全部。"""
    user = require_user(request)
    visible = [r for r in _RUNS.values() if _user_can_see(user, r.get("owner_user_id"))]
    runs = sorted(visible, key=lambda r: r["started_at"], reverse=True)
    summarized = [
        {k: v for k, v in r.items() if k not in ("report", "traceback")}
        for r in runs[:50]
    ]
    return {"total": len(visible), "recent": summarized}


# ----- HTML pages -----

TOOLS_INDEX_HTML = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>天枢·裁决 · AI 裁决手册</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@300;400;500;600;700&family=Noto+Sans+SC:wght@300;400;500;600&family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
/* ======= MUJI 出版物 · 设计 Token ======= */
:root {
  --paper:    #ffffff;
  --paper-2:  #f0f0f0;
  --paper-3:  #e3e3e3;
  --ink:      #0a0a0a;
  --ink-2:    #262626;
  --ink-3:    #4a4a4a;
  --ink-4:    #6e6e6e;
  --line:     #c4c4c4;
  --line-2:   #9e9e9e;
  --accent:   #a8401f;
  --accent-h: #82301a;
  --ok:       #4f6b35;
  --warn:     #8a5300;
  --bad:      #9a3315;
  --serif:    'Noto Serif SC', 'Songti SC', 'STSong', 'SimSun', Georgia, serif;
  --sans:     'Noto Sans SC', 'PingFang SC', -apple-system, 'Microsoft YaHei', sans-serif;
  --mono:     'Inter', 'SF Mono', ui-monospace, Menlo, monospace;
}
*{box-sizing:border-box}
html,body{margin:0;background:var(--paper);color:var(--ink);
  font-family:var(--sans);font-weight:300;font-size:15px;line-height:1.85;
  -webkit-font-smoothing:antialiased;font-feature-settings:"palt" on}
body{min-height:100vh}
a{color:inherit;text-decoration:none}
button{font-family:inherit;cursor:pointer}
::selection{background:rgba(196,90,58,.22);color:var(--accent)}
::-webkit-scrollbar{width:8px;height:8px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--line-2);border-radius:4px}

/* ======= 顶栏:极简 ======= */
.topbar{position:sticky;top:0;background:rgba(255,255,255,.94);
  backdrop-filter:saturate(140%) blur(10px);-webkit-backdrop-filter:saturate(140%) blur(10px);
  border-bottom:1px solid var(--line);z-index:50;
  display:flex;align-items:center;gap:12px;padding:0 24px;height:56px}
.topbar .brand{font-family:var(--serif);font-size:16px;font-weight:500;
  letter-spacing:.1em;color:var(--ink)}
.topbar .brand .sep{margin:0 6px;color:var(--ink-3)}
.topbar nav{display:flex;gap:2px;margin-left:24px;margin-right:auto}
.topbar nav a{font-size:13px;color:var(--ink-2);letter-spacing:.04em;
  font-family:var(--sans);font-weight:400;padding:6px 12px;border-radius:6px;
  text-decoration:none;transition:all .15s}
.topbar nav a:hover{background:var(--paper-2);color:var(--ink)}
.topbar nav a.active{background:var(--accent-soft,rgba(168,64,31,.12));color:var(--accent)}
.topbar .kbd-hint{margin-left:12px;font-family:var(--mono);font-size:11px;color:var(--ink-3);
  cursor:pointer}
.topbar .kbd-hint:hover{color:var(--accent)}
.topbar .kbd-hint kbd{background:var(--paper-2);border:1px solid var(--line);
  padding:1px 5px;border-radius:3px;font-family:var(--mono);font-size:10.5px;color:var(--ink-2);
  margin:0 1px}

/* ======= Env banner (alert) — 仅必要时显示 ======= */
.env-banner{display:none;margin:24px 48px 0;padding:14px 18px;
  background:rgba(196,90,58,.10);border-left:2px solid var(--accent);
  font-size:13px;color:var(--ink-2);font-weight:400}
.env-banner.show{display:block}
.env-banner h3{margin:0 0 6px;font-family:var(--sans);font-size:13px;
  font-weight:500;color:var(--ink);letter-spacing:.04em}
.env-banner .icon{margin-right:8px;color:var(--accent)}
.env-banner .actions{margin-top:10px;display:flex;gap:14px;font-size:12px}
.env-banner .actions button{background:transparent;border:none;border-bottom:1px solid var(--ink-3);
  color:var(--ink);font-size:12px;padding:1px 0;letter-spacing:.04em}
.env-banner .actions button:hover{border-color:var(--accent);color:var(--accent)}
.env-banner .env-progress{font-family:var(--mono);font-size:11px;
  color:var(--ink-3);margin-top:8px;white-space:pre-wrap;max-height:120px;overflow:auto}

/* ======= Hero ======= */
.hero{min-height:88vh;display:flex;flex-direction:column;justify-content:center;
  padding:0 48px;max-width:1080px;margin:0 auto}
.hero .eyebrow{font-family:var(--mono);font-size:11px;color:var(--ink-3);
  letter-spacing:.24em;text-transform:uppercase;margin-bottom:22px}
.hero h1{font-family:var(--serif);font-size:52px;font-weight:400;
  line-height:1.25;color:var(--ink);margin:0 0 32px;letter-spacing:.02em}
.hero h1 em{font-style:normal;color:var(--accent)}
.hero .lede{font-size:17px;line-height:1.95;color:var(--ink-2);font-weight:300;
  max-width:560px;margin:0 0 56px}
.hero .actions{display:flex;gap:14px;align-items:center}
.hero .btn{display:inline-flex;align-items:center;gap:10px;
  padding:14px 28px;border:1px solid var(--ink);background:transparent;
  font-family:var(--sans);font-size:14px;letter-spacing:.06em;color:var(--ink);
  font-weight:400;transition:all .18s}
.hero .btn:hover{background:var(--ink);color:var(--paper)}
.hero .btn.primary{background:var(--ink);color:var(--paper)}
.hero .btn.primary:hover{background:var(--accent);border-color:var(--accent)}
.hero .btn .arrow{font-family:var(--serif);font-size:14px}
.hero-foot{margin-top:80px;font-family:var(--mono);font-size:11px;color:var(--ink-3);
  letter-spacing:.16em;text-transform:uppercase;display:flex;align-items:center;gap:12px}
.hero-foot .dot{display:inline-block;width:5px;height:5px;border-radius:50%;background:var(--accent)}
.hero-foot .claude-status{margin-left:auto;font-family:var(--mono);font-size:11px}

/* ======= 章节分隔 ======= */
.chapter-divider{max-width:1080px;margin:120px auto 80px;padding:0 48px;
  display:flex;align-items:center;gap:24px}
.chapter-divider .label{font-family:var(--mono);font-size:11px;
  letter-spacing:.32em;color:var(--ink-3);text-transform:uppercase;white-space:nowrap}
.chapter-divider .line{flex:1;height:1px;background:var(--line)}

/* ======= 核心能力 三列 ======= */
.capabilities{max-width:1080px;margin:0 auto;padding:0 48px;
  display:grid;grid-template-columns:repeat(3,1fr);gap:80px}
.cap{}
.cap .num-zh{font-family:var(--serif);font-size:64px;font-weight:300;
  color:var(--ink);line-height:1;margin-bottom:10px;letter-spacing:.04em}
.cap .name{font-family:var(--serif);font-size:18px;font-weight:500;
  color:var(--ink);margin-bottom:18px;letter-spacing:.04em}
.cap .desc{font-size:14px;line-height:1.85;color:var(--ink-2);font-weight:300}

/* ======= 工具目录 ======= */
.directory{max-width:840px;margin:0 auto;padding:0 48px}
.directory-group{margin-bottom:56px}
.directory-group:last-child{margin-bottom:0}
.directory-group .group-label{font-family:var(--mono);font-size:10.5px;
  letter-spacing:.32em;color:var(--ink-3);text-transform:uppercase;
  margin-bottom:28px;padding-bottom:14px;border-bottom:1px solid var(--line)}
.directory-row{display:grid;grid-template-columns:auto 1fr auto;gap:24px;
  align-items:baseline;padding:18px 0;border-bottom:1px dashed var(--line);
  transition:padding .18s ease,background .18s ease;cursor:pointer}
.directory-row:hover{background:linear-gradient(90deg,rgba(196,90,58,.08),transparent);
  padding-left:12px}
.directory-row:hover .name{color:var(--accent)}
.directory-row .chapter{font-family:var(--serif);font-size:14.5px;color:var(--ink-3);
  font-weight:400;letter-spacing:.16em;min-width:80px;white-space:nowrap}
.directory-row .name{font-family:var(--serif);font-size:21px;font-weight:500;
  color:var(--ink);letter-spacing:.04em;transition:color .15s}
.directory-row .desc{font-size:13px;color:var(--ink-2);line-height:1.7;
  display:block;margin-top:6px;font-weight:300}
.directory-row .open{font-family:var(--serif);font-size:18px;color:var(--ink-3);
  transition:color .15s,transform .15s}
.directory-row:hover .open{color:var(--accent);transform:translateX(4px)}

/* ======= 最近报告(索引式) ======= */
.recent{max-width:1080px;margin:0 auto;padding:0 48px}
.recent-month{font-family:var(--serif);font-size:24px;font-weight:400;
  color:var(--ink);margin:0 0 32px;letter-spacing:.08em}
.recent-table{width:100%;border-collapse:collapse}
.recent-table tr{cursor:pointer;transition:background .18s}
.recent-table tr:hover{background:rgba(196,90,58,.08)}
.recent-table td{padding:18px 0;border-bottom:1px solid var(--line);
  font-size:13.5px;color:var(--ink);vertical-align:baseline}
.recent-table .date{font-family:var(--mono);color:var(--ink-3);font-size:12.5px;
  width:120px;letter-spacing:.04em}
.recent-table .chapter-cell{font-family:var(--serif);color:var(--ink-2);
  width:180px;letter-spacing:.06em;font-size:14px}
.recent-table .code-cell{font-family:var(--mono);color:var(--ink-2);font-size:12px;
  width:200px;letter-spacing:.02em}
.recent-table .name-cell{color:var(--ink);font-weight:400}
.recent-table .status-cell{font-family:var(--mono);font-size:11px;
  letter-spacing:.16em;text-transform:uppercase;width:80px;text-align:right;
  color:var(--ink-3)}
.recent-table .status-cell.ok{color:var(--ok)}
.recent-table .status-cell.bad{color:var(--bad)}
.recent-table .status-cell.warn{color:var(--warn)}
.recent-table tr:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
.directory-row:focus-visible{outline:2px solid var(--accent);outline-offset:4px}
.recent-foot{margin-top:32px;text-align:center}
.recent-foot a{font-family:var(--sans);font-size:13px;color:var(--ink-2);
  letter-spacing:.08em;border-bottom:1px solid var(--ink-3);padding-bottom:2px}
.recent-foot a:hover{color:var(--accent);border-color:var(--accent)}
.recent-empty{text-align:center;color:var(--ink-3);font-size:13px;
  padding:48px 0;font-family:var(--serif);letter-spacing:.04em}

/* ======= Footer ======= */
.footer{max-width:1080px;margin:160px auto 0;padding:48px 48px 48px;
  border-top:1px solid var(--line);
  display:flex;align-items:baseline;justify-content:space-between;
  font-family:var(--mono);font-size:11px;color:var(--ink-3);
  letter-spacing:.16em;text-transform:uppercase}
.footer .left .brand{font-family:var(--serif);font-size:13px;letter-spacing:.16em;
  color:var(--ink);text-transform:none;margin-right:14px}
.footer .right{display:flex;gap:20px}

/* ======= 浮动「运行中」标记 ======= */
.task-fab{position:fixed;bottom:24px;right:24px;z-index:90;display:none;
  background:var(--paper);border:1px solid var(--line);
  padding:14px 18px;min-width:240px;max-width:320px;
  font-family:var(--sans);font-size:12.5px;color:var(--ink-2);
  box-shadow:0 4px 24px rgba(44,44,44,.06)}
.task-fab.show{display:block}
.task-fab .head{display:flex;align-items:center;gap:8px;
  font-family:var(--mono);font-size:11px;color:var(--ink-3);
  letter-spacing:.16em;text-transform:uppercase;margin-bottom:10px;
  padding-bottom:10px;border-bottom:1px solid var(--line)}
.task-fab .head .dot{width:6px;height:6px;border-radius:50%;background:var(--accent);
  animation:pulse 1.8s ease-in-out infinite}
.task-fab .head .count{margin-left:auto;font-family:var(--mono);font-size:11px;
  color:var(--ink-2)}
.task-fab .body{max-height:432px;overflow-y:auto}
.task-fab .row{padding:8px 0;border-bottom:1px dashed var(--line);font-size:12.5px;display:block}
.task-fab .row:last-child{border-bottom:none}
.task-fab .row .title{color:var(--ink);font-family:var(--serif);font-size:13.5px;
  letter-spacing:.04em}
.task-fab .row .progress{color:var(--ink-3);font-family:var(--mono);font-size:11px;
  margin-top:3px}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}

/* ======= 命令面板 ======= */
.cmd-overlay{position:fixed;inset:0;background:rgba(44,44,44,.30);
  backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);z-index:100;
  display:none;align-items:flex-start;justify-content:center;padding-top:120px}
.cmd-overlay.open{display:flex}
.cmd-panel{background:var(--paper);border:1px solid var(--line);
  width:min(640px,92vw);padding:6px;
  box-shadow:0 16px 60px rgba(44,44,44,.20)}
.cmd-input{width:100%;padding:14px 18px;background:transparent;border:none;
  font-family:var(--serif);font-size:16px;color:var(--ink);
  letter-spacing:.04em;outline:none;border-bottom:1px solid var(--line)}
.cmd-input::placeholder{color:var(--ink-3)}
.cmd-results{max-height:380px;overflow-y:auto;margin-top:4px}
.cmd-result{display:flex;align-items:baseline;gap:14px;padding:12px 18px;cursor:pointer;
  border-bottom:1px dashed var(--line);font-size:13.5px;color:var(--ink-2)}
.cmd-result:last-child{border-bottom:none}
.cmd-result.active,.cmd-result:hover{background:var(--paper-2);color:var(--ink)}
.cmd-result .label{font-family:var(--serif);color:var(--ink)}
.cmd-result .hint{margin-left:auto;font-family:var(--mono);font-size:11px;color:var(--ink-3)}

/* ======= Toast ======= */
.toast-stack{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);
  z-index:200;display:flex;flex-direction:column;gap:8px;align-items:center}
.toast{background:var(--ink);color:var(--paper);padding:12px 22px;
  font-family:var(--sans);font-size:13px;letter-spacing:.04em;
  box-shadow:0 6px 20px rgba(44,44,44,.18);
  animation:toast-in .22s ease}
@keyframes toast-in{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
.toast.leaving{animation:toast-out .2s ease forwards}
@keyframes toast-out{to{opacity:0;transform:translateY(8px)}}
.toast a{color:var(--accent);text-decoration:underline}

/* ======= 响应式 ======= */
@media (max-width:880px){
  .topbar{padding:14px 24px}.topbar .kbd-hint{display:none}
  .hero{padding:0 24px}.hero h1{font-size:36px}
  .chapter-divider{padding:0 24px;margin:80px auto 48px}
  .capabilities{grid-template-columns:1fr;gap:48px;padding:0 24px}
  .directory{padding:0 24px}
  .directory-row{grid-template-columns:1fr;gap:6px}
  .directory-row .chapter{font-size:11px;letter-spacing:.24em}
  .directory-row .open{display:none}
  .recent{padding:0 24px}
  .recent-table .date{width:auto}
  .recent-table .chapter-cell,.recent-table .code-cell{display:none}
  .footer{padding:32px 24px;flex-direction:column;gap:14px;margin-top:80px}
}
</style></head>
<body>

<header class="topbar">
  <a class="brand-link" href="/tools" style="display:inline-flex;align-items:center;gap:10px;text-decoration:none;color:inherit;margin-right:24px;padding:6px 0"><svg class="brand-mark" viewBox="0 0 24 24" fill="currentColor" width="24" height="24" aria-hidden="true" style="color:var(--accent);opacity:1;filter:drop-shadow(0 1px 3px rgba(212,103,74,.3))"><path d="M12 1.5L13.4 10.6L22.5 12L13.4 13.4L12 22.5L10.6 13.4L1.5 12L10.6 10.6Z"/></svg><span class="brand" style="font-family:'Noto Serif SC',Georgia,serif;font-size:19px;font-weight:600;letter-spacing:.18em;color:var(--ink)">天枢<span class="sep" style="color:var(--accent);margin:0 6px;font-weight:400">·</span>裁决</span></a>
  <nav>
    <a href="/tools" class="active">工具</a>
    <a href="/reports">报告</a>
    <a href="/settings">设置</a>
    <a href="/admin/users" class="admin-only" style="display:none">用户管理</a>
  </nav>
  <span class="kbd-hint" id="cmd-trigger" title="打开命令面板"><kbd>⌘</kbd><kbd>K</kbd></span>
</header>
<!-- 顶部用户徽标 + 登出 + admin-only 链接显示由 _SHARED_OVERLAY_SNIPPET 注入,无需在此页脚本里再做 -->

<div class="env-banner" id="env-banner">
  <h3><span class="icon">!</span><span id="env-title">环境检查</span></h3>
  <div id="env-body"></div>
  <div class="actions" id="env-actions"></div>
  <pre class="env-progress" id="env-progress"></pre>
</div>

<!-- HERO -->
<section class="hero">
  <div class="eyebrow">天枢 · 裁决 ── AI Verdict Manual · Edition 0.1</div>
  <h1>AI 驱动的<br>软件<em>质量裁决</em>手册</h1>
  <p class="lede">把需求拆解、用例设计、接口测试、UI 比对、H5 适配 到 SEO 审计 — 全流程交给 AI 智能体,出具可分派的裁决报告。</p>
  <div class="actions">
    <a class="btn primary" href="#directory">开始第一章 <span class="arrow">→</span></a>
    <a class="btn" href="/reports">查看报告索引</a>
  </div>
  <div class="hero-foot">
    <span class="dot"></span>
    <span>覆盖八个测试环节</span>
    <span class="claude-status" id="claude-status">检测中…</span>
  </div>
</section>

<!-- 核心能力 -->
<div class="chapter-divider">
  <span class="label">核 心 能 力</span>
  <span class="line"></span>
</div>
<section class="capabilities">
  <div class="cap">
    <div class="num-zh">八</div>
    <div class="name">智能体覆盖</div>
    <div class="desc">从需求评审到 SEO 审计的完整测试流程,八个独立 Agent 各司其职、可单独运行也可链式接力。</div>
  </div>
  <div class="cap">
    <div class="num-zh">五</div>
    <div class="name">统一报告契约</div>
    <div class="desc">每份报告都按「结论 / 风险 / 阻碍 / Bug 表 / 执行用例」五段结构输出,可直接分派到研发、产品、测试。</div>
  </div>
  <div class="cap">
    <div class="num-zh">上下游</div>
    <div class="name">接力链路</div>
    <div class="desc">上一工具的产出可一键导入下一工具作为输入,从 PRD 到上线形成完整闭环。</div>
  </div>
</section>

<!-- 工具目录 -->
<div class="chapter-divider">
  <span class="label">工 具 目 录</span>
  <span class="line"></span>
</div>
<section class="directory" id="directory">
  <div class="directory-group" id="dir-pre">
    <div class="group-label">提 测 前</div>
    <div id="dir-pre-rows"></div>
  </div>
  <div class="directory-group" id="dir-post">
    <div class="group-label">提 测 后</div>
    <div id="dir-post-rows"></div>
  </div>
</section>

<!-- 最近报告 -->
<div class="chapter-divider">
  <span class="label">最 近 报 告</span>
  <span class="line"></span>
</div>
<section class="recent">
  <h3 class="recent-month" id="recent-month">最近运行</h3>
  <table class="recent-table"><tbody id="recent-rows"></tbody></table>
  <div class="recent-foot"><a href="/reports">查看全部索引 →</a></div>
</section>

<!-- Footer -->
<footer class="footer">
  <div class="left"><span class="brand">天枢 · 裁决</span> v0.1.0</div>
  <div class="right">
    <span>本地 Claude 驱动</span>
    <span>· 二〇二六</span>
  </div>
</footer>

<aside class="task-fab" id="task-fab" aria-label="运行中的任务">
  <div class="head">
    <span class="dot"></span><span>运行中</span><span class="count" id="task-count">0</span>
  </div>
  <div class="body" id="task-list"></div>
</aside>

<div class="cmd-overlay" id="cmd-overlay">
  <div class="cmd-panel">
    <input class="cmd-input" id="cmd-input" placeholder="搜索工具 / 报告 / 设置…" autocomplete="off">
    <div class="cmd-results" id="cmd-results"></div>
  </div>
</div>

<div class="toast-stack" id="toast-stack"></div>

<script>
const PHASE_MAP = {step1:'pre', step2:'pre', step4:'post', step5:'post', step6:'post',
  network_resilience:'post', h5_adapt:'post', seo_audit:'post'};
const CN_CH = ['一','二','三','四','五','六','七','八','九','十'];
let tools = [];
let chapterMap = {};

async function load() {
  try {
    const cat = await fetch('/api/tools').then(r => r.json());
    tools = cat.tools || [];
    renderDirectory();
    checkClaude();
    pollRuns();
    loadRecent();
    setInterval(pollRuns, 4000);
    checkEnvironment();
  } catch(e) {
    console.error('load failed', e);
  }
}

function renderDirectory() {
  // 按 catalog 顺序分配章节号一至八
  tools.forEach((t, i) => { chapterMap[t.id] = CN_CH[i] || String(i+1); });

  const pre = tools.filter(t => PHASE_MAP[t.id] === 'pre');
  const post = tools.filter(t => PHASE_MAP[t.id] !== 'pre');
  document.getElementById('dir-pre-rows').innerHTML = pre.map(t => rowHtml(t)).join('');
  document.getElementById('dir-post-rows').innerHTML = post.map(t => rowHtml(t)).join('');
  document.querySelectorAll('.directory-row').forEach(r => {
    const go = () => { location.href = '/tools/' + r.dataset.tid; };
    r.onclick = go;
    r.addEventListener('keydown', ev => {
      if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); go(); }
    });
  });
}

function rowHtml(t){
  const ch = chapterMap[t.id] || '?';
  const aria = `进入第 ${ch} 章 · ${t.name}`;
  return `<div class="directory-row" data-tid="${t.id}" role="link" tabindex="0" aria-label="${escapeHtml(aria)}">
    <div class="chapter">第 ${ch} 章</div>
    <div>
      <div class="name">${escapeHtml(t.name)}</div>
      <div class="desc">${escapeHtml(t.tagline || t.description || '')}</div>
    </div>
    <div class="open" aria-hidden="true">→</div>
  </div>`;
}

async function checkClaude() {
  try {
    const ci = await fetch('/api/claude/info').then(r => r.json());
    const el = document.getElementById('claude-status');
    if (ci.bin_found) {
      const auth = ci.auth_state || '';
      const acc = ci.account || {};
      if (auth === 'toolkit_api_key') {
        el.textContent = '· API Key · ' + (acc.display_name || '');
      } else if (auth === 'toolkit_oauth') {
        el.textContent = '· OAuth' + (acc.email ? ' · ' + acc.email : '');
      } else {
        el.textContent = '· 未接入 · 请到设置选择连接方式';
        el.style.color = 'var(--accent)';
      }
    } else {
      el.textContent = '· 未找到 Claude CLI';
      el.style.color = 'var(--accent)';
    }
  } catch(e) {}
}

async function loadRecent() {
  try {
    const d = await fetch('/api/reports').then(r => r.json());
    // 同一个 run_id 可能 in_memory + saved 双重出现 — 先按 run_id 去重,
    // 留下「有真实 status 的内存条目」优先;避免最近报告出现两行同 run_id。
    const seen = new Set();
    const all = [];
    (d.in_memory||[]).forEach(r => { if (r.run_id && !seen.has(r.run_id)){ seen.add(r.run_id); all.push(r); } });
    (d.saved||[]).forEach(r => { if (r.run_id && !seen.has(r.run_id)){ seen.add(r.run_id); all.push(r); } });
    all.sort((a,b) => (b.mtime || b.started_at || 0) - (a.mtime || a.started_at || 0));
    const top = all.slice(0, 6);
    const tbody = document.getElementById('recent-rows');
    if (!top.length) {
      tbody.innerHTML = '<tr><td colspan="5"><div class="recent-empty">尚无报告 · 选一章开始</div></td></tr>';
      document.getElementById('recent-month').textContent = '';
      return;
    }
    // 头部用最新一条的月份
    const newest = top[0];
    const t = new Date((newest.mtime || newest.started_at) * 1000);
    const yMap = {0:'〇',1:'一',2:'二',3:'三',4:'四',5:'五',6:'六',7:'七',8:'八',9:'九'};
    const yStr = String(t.getFullYear()).split('').map(c => yMap[c]||c).join('');
    const monthZh = ['一','二','三','四','五','六','七','八','九','十','十一','十二'][t.getMonth()];
    document.getElementById('recent-month').textContent = `${yStr} ── ${monthZh}月`;

    // 先合并：同一个 run_id 在 in_memory + saved 都出现时，以 in_memory 的真实状态为准
    const memIndex = {};
    (d.in_memory||[]).forEach(m => { if (m.run_id) memIndex[m.run_id] = m; });
    tbody.innerHTML = top.map(r => {
      const tt = new Date((r.mtime || r.started_at) * 1000);
      const dd = String(tt.getDate()).padStart(2,'0');
      const hh = String(tt.getHours()).padStart(2,'0');
      const mm = String(tt.getMinutes()).padStart(2,'0');
      const tm = tools.find(x => x.id === r.tool_id);
      const ch = chapterMap[r.tool_id] || '?';
      const chName = tm ? tm.name : r.tool_id;
      const pc = r.project_code || '—';
      const pn = r.project_name || '—';
      // 状态来源：内存里有真实 status 优先；磁盘 only 才说"已存"
      // 之前用 r.kind === 'memory' 取，但 /api/reports 返回的字段是 r.source — 永远是 false
      const mem = memIndex[r.run_id];
      let status;
      if (mem) {
        status = mem.status || '';
      } else if (r.source === 'memory') {
        status = r.status || '';
      } else {
        status = 'saved';
      }
      const sCls = (status === 'succeeded' || status === 'saved') ? 'ok'
                 : (status === 'failed') ? 'bad'
                 : (status === 'running' || status === 'queued') ? 'warn'
                 : '';
      const sLabel = {succeeded:'成 功',failed:'失 败',running:'运行中',queued:'排 队',saved:'已 存'}[status] || (status || '—');
      return `<tr onclick="location.href='/tools/${r.tool_id}?run=${r.run_id}'" tabindex="0" role="link" aria-label="第 ${ch} 章 · ${escapeHtml(chName)} · ${escapeHtml(pn)} · ${sLabel}">
        <td class="date">${dd}日 ${hh}:${mm}</td>
        <td class="chapter-cell">第 ${ch} 章 · ${escapeHtml(chName)}</td>
        <td class="code-cell">${escapeHtml(pc)}</td>
        <td class="name-cell">${escapeHtml(pn)}</td>
        <td class="status-cell ${sCls}">${sLabel}</td>
      </tr>`;
    }).join('');
    // 行键盘可达：Enter / Space 触发同一个跳转
    tbody.querySelectorAll('tr[onclick]').forEach(tr => {
      tr.addEventListener('keydown', ev => {
        if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); tr.click(); }
      });
    });
  } catch(e) {}
}

async function pollRuns() {
  try {
    const data = await fetch('/api/tools/runs').then(r => r.json());
    const runs = (data.recent || []).filter(r => r.status === 'running' || r.status === 'queued');
    const fab = document.getElementById('task-fab');
    if (!runs.length) { fab.classList.remove('show'); return; }
    document.getElementById('task-count').textContent = runs.length;
    document.getElementById('task-list').innerHTML = runs.map(t => {
      const ch = chapterMap[t.tool_id] || '?';
      return `<a class="row" href="/tools/${t.tool_id}?run=${t.run_id}">
        <div class="title">第 ${ch} 章 · ${escapeHtml(t.tool_name)}</div>
        <div class="progress">${escapeHtml(t.progress || t.status)}</div>
      </a>`;
    }).join('');
    fab.classList.add('show');
  } catch(e) {}
}

async function checkEnvironment() {
  try {
    const r = await fetch('/api/settings/ready').then(r => r.json());
    const allMissing = [...(r.missing_required||[]), ...(r.missing_optional||[])];
    if (!allMissing.length) return;
    const hasAuthIssue = (r.missing_required||[]).some(m => m.pkg === 'auth_mode' || m.pkg === 'claude_api_key');
    if (!hasAuthIssue && sessionStorage.getItem('env-skip-once')) return;

    const banner = document.getElementById('env-banner');
    banner.classList.add('show');
    document.getElementById('env-title').textContent =
      hasAuthIssue ? '需要先选择连接方式' : '环境检查';
    document.getElementById('env-body').innerHTML = allMissing.map(m =>
      `<div style="font-size:12.5px;padding:4px 0">— ${escapeHtml(m.label || m.pkg)}:<span style="color:var(--ink-3)">${escapeHtml(m.purpose || '')}</span></div>`
    ).join('');
    document.getElementById('env-actions').innerHTML = hasAuthIssue
      ? `<button onclick="location.href='/settings'">前往设置 →</button>`
      : `<button onclick="sessionStorage.setItem('env-skip-once','1'); document.getElementById('env-banner').classList.remove('show')">本次跳过</button>`;
  } catch(e) {}
}

// 命令面板
const cmdOverlay = document.getElementById('cmd-overlay');
const cmdInput = document.getElementById('cmd-input');
const cmdResults = document.getElementById('cmd-results');
let cmdActive = 0;
function openCmd(){ cmdOverlay.classList.add('open'); cmdInput.value=''; cmdInput.focus(); renderCmd(''); }
function closeCmd(){ cmdOverlay.classList.remove('open'); }
function cmdItems(){
  return [
    ...tools.map(t => ({name:`第 ${chapterMap[t.id]||'?'} 章 · ${t.name}`, hint:t.id, href:`/tools/${t.id}`})),
    {name:'报告索引', hint:'reports', href:'/reports'},
    {name:'设置', hint:'settings', href:'/settings'},
  ];
}
function renderCmd(q){
  const items = cmdItems().filter(x => !q || (x.name+x.hint).toLowerCase().includes(q.toLowerCase()));
  cmdActive = 0;
  cmdResults.innerHTML = items.length
    ? items.map((x,i) => `<div class="cmd-result ${i===0?'active':''}" data-href="${x.href}" data-idx="${i}">
        <span class="label">${escapeHtml(x.name)}</span>
        <span class="hint">${escapeHtml(x.hint)}</span>
      </div>`).join('')
    : `<div style="padding:20px;text-align:center;color:var(--ink-3);font-size:12.5px;font-family:var(--serif)">无匹配</div>`;
  cmdResults.querySelectorAll('.cmd-result').forEach(el => {
    el.onclick = () => { location.href = el.dataset.href; };
  });
}
document.addEventListener('keydown', e => {
  if ((e.metaKey||e.ctrlKey) && e.key === 'k'){ e.preventDefault(); openCmd(); }
  else if (e.key === 'Escape' && cmdOverlay.classList.contains('open')){ closeCmd(); }
  else if (cmdOverlay.classList.contains('open')){
    const rs = cmdResults.querySelectorAll('.cmd-result');
    if (e.key === 'ArrowDown'){ e.preventDefault(); cmdActive = Math.min(rs.length-1, cmdActive+1); rs.forEach((el,i)=>el.classList.toggle('active', i===cmdActive)); }
    else if (e.key === 'ArrowUp'){ e.preventDefault(); cmdActive = Math.max(0, cmdActive-1); rs.forEach((el,i)=>el.classList.toggle('active', i===cmdActive)); }
    else if (e.key === 'Enter'){ const el = rs[cmdActive]; if (el) location.href = el.dataset.href; }
  }
});
cmdInput.addEventListener('input', () => renderCmd(cmdInput.value));
cmdOverlay.addEventListener('click', e => { if (e.target === cmdOverlay) closeCmd(); });
document.getElementById('cmd-trigger').addEventListener('click', openCmd);

function toast(msg){
  const t = document.createElement('div');
  t.className = 'toast'; t.textContent = msg;
  document.getElementById('toast-stack').appendChild(t);
  setTimeout(() => { t.classList.add('leaving'); setTimeout(() => t.remove(), 220); }, 3000);
}

function escapeHtml(s){ return String(s == null ? '' : s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }

load();
</script>
</body></html>
"""


TOOL_DETAIL_HTML_OLD_DEPRECATED = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>工具 — 天枢·裁决</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@300;400;500;600;700&family=Noto+Sans+SC:wght@300;400;500;600&family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
  :root{--bg:#07080a;--surface:#0f1216;--surface-2:#161a1f;--surface-3:#1d2228;
    --line:#1f2530;--line-2:#2a3140;
    --fg:#f5f7fa;--fg-2:#a8aeb8;--fg-3:#6c7380;--fg-4:#4a5060;
    --ac:#10b981;--ac-2:#6ee7b7;
    --warn:#fbbf24;--ok:#4ade80;--bad:#f87171;
    --mono:ui-monospace,SFMono-Regular,"JetBrains Mono",Menlo,monospace;
    --sans:-apple-system,BlinkMacSystemFont,"SF Pro Display","SF Pro Text","PingFang SC","Helvetica Neue",Arial,sans-serif;}
  *{box-sizing:border-box}
  html,body{margin:0;background:var(--bg);color:var(--fg);font-family:var(--sans);height:100%;overflow:hidden}

  /* === Top bar (always visible) === */
  .topbar{display:flex;align-items:center;gap:0;height:48px;
    padding:0 16px;border-bottom:1px solid var(--line);
    background:rgba(7,8,10,.92);position:relative;z-index:30}
  .topbar .logo{width:24px;height:24px;border:1.5px solid var(--ac);border-radius:6px;
    display:grid;place-items:center;color:var(--ac);font-family:var(--mono);font-weight:700;
    font-size:11px;margin-right:14px;flex-shrink:0}
  .topbar .crumbs{display:flex;align-items:center;gap:8px;font-size:13px}
  .topbar .crumbs a{color:var(--fg-3);text-decoration:none;transition:color .15s}
  .topbar .crumbs a:hover{color:var(--ac)}
  .topbar .crumbs .sep{color:var(--fg-4)}
  .topbar .crumbs .current{color:var(--fg);font-weight:500;display:flex;align-items:center;gap:6px}
  .topbar .crumbs .icon{font-size:15px}
  .topbar .stats{margin-left:auto;display:flex;gap:14px;align-items:center;
    font-family:var(--mono);font-size:11px;color:var(--fg-3)}
  .topbar .stats{display:flex;gap:14px;align-items:center;
    font-family:"SF Mono",ui-monospace,monospace;font-size:11px;color:var(--fg-3)}
  .topbar .stats .stat{display:flex;align-items:center;gap:5px}
  .topbar .stats .stat .lbl{color:var(--fg-4);font-size:10px;text-transform:uppercase;letter-spacing:.05em}
  .topbar .stats .stat .v{color:var(--fg-2)}
  .topbar .stats .stat.cost .v{color:var(--ac)}
  .topbar .stats .stat.gate.reject .v{color:var(--bad)}
  .topbar .stats .stat.gate.proceed .v{color:var(--ok)}
  .topbar .stats .stat.gate.warn .v{color:var(--warn)}
  .topbar .actions{display:flex;gap:8px;align-items:center;margin-left:14px}
  .topbar .icon-btn{background:transparent;border:1px solid var(--line);color:var(--fg-2);
    width:32px;height:30px;border-radius:6px;cursor:pointer;font-size:13px;
    display:grid;place-items:center;transition:all .15s}
  .topbar .icon-btn:hover{border-color:var(--ac);color:var(--ac)}
  .topbar .icon-btn.active{background:rgba(16,185,129,.10);border-color:var(--ac);color:var(--ac)}
  .topbar button.run{background:var(--ac);color:#001a14;border:none;
    padding:0 18px;height:30px;border-radius:6px;font-family:var(--sans);font-size:13px;
    font-weight:600;cursor:pointer;display:flex;align-items:center;gap:6px;transition:all .15s}
  .topbar button.run:disabled{background:var(--line-2);color:var(--fg-3);cursor:not-allowed}
  .topbar button.run:hover:not(:disabled){background:var(--ac-2)}
  .topbar button.run kbd{background:rgba(0,26,20,.18);padding:1px 5px;border-radius:3px;
    font-family:var(--mono);font-size:10px;font-weight:600;margin-left:4px}

  /* === Workspace === */
  .workspace{display:grid;grid-template-columns:1fr 1px 1fr;height:calc(100vh - 48px);overflow:hidden}
  .pane{display:flex;flex-direction:column;height:100%;overflow:hidden}
  .pane.input{background:var(--bg)}
  .pane.output{background:var(--surface)}
  .divider{background:var(--line);width:1px}

  .pane-head{display:flex;align-items:center;height:38px;padding:0 18px;
    border-bottom:1px solid var(--line);font-size:11.5px;color:var(--fg-3);
    text-transform:uppercase;letter-spacing:.12em;font-weight:600;flex-shrink:0;
    gap:14px}
  .pane-head .label{display:flex;align-items:center;gap:6px}
  .pane-head .dot{width:6px;height:6px;border-radius:50%;background:var(--fg-4)}
  .pane-head .dot.live{background:var(--ac);animation:pulse 1.6s ease-in-out infinite}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
  .pane-head .right{margin-left:auto;display:flex;gap:6px;font-family:var(--mono);
    font-size:11px;color:var(--fg-3);text-transform:none;letter-spacing:0;font-weight:400}

  /* Form */
  .pane-body{flex:1;overflow-y:auto;padding:20px 24px}
  form{display:flex;flex-direction:column;gap:14px}
  .field{display:flex;flex-direction:column;gap:5px}
  .field-head{display:flex;align-items:center;gap:8px;font-size:12.5px}
  .field label{color:var(--fg);font-weight:500}
  .field .req{color:var(--bad)}
  .field .opt{color:var(--fg-4);font-family:var(--mono);font-size:11px;font-weight:400}
  .field .upload-btn{margin-left:auto;background:transparent;border:1px solid var(--line);
    color:var(--fg-3);padding:3px 10px;border-radius:5px;font-family:var(--mono);
    font-size:11px;cursor:pointer;transition:all .15s}
  .field .upload-btn:hover{border-color:var(--ac);color:var(--ac)}
  .field textarea, .field input[type=text]{
    background:var(--surface);border:1px solid var(--line);border-radius:6px;
    color:var(--fg);font-family:var(--mono);font-size:12.5px;padding:10px 12px;
    resize:vertical;min-height:64px;line-height:1.55;width:100%;transition:all .15s}
  .field textarea:focus, .field input[type=text]:focus{
    outline:none;border-color:var(--ac);background:var(--surface-2)}
  .field textarea::placeholder{color:var(--fg-4)}
  .field input[type=file]{display:none}
  .field .checkbox-row{display:flex;align-items:center;gap:10px;
    background:var(--surface);border:1px solid var(--line);border-radius:6px;
    padding:9px 12px;font-size:12.5px}
  .field .checkbox-row input{width:15px;height:15px;accent-color:var(--ac);cursor:pointer}
  .field .checkbox-row label{cursor:pointer}

  /* Output pane */
  .out-empty{display:flex;flex-direction:column;align-items:center;justify-content:center;
    text-align:center;padding:60px 24px;color:var(--fg-3);font-size:13px;height:100%;
    font-family:var(--mono)}
  .out-empty .ascii{font-size:11px;color:var(--fg-4);line-height:1.4;margin-bottom:14px;
    white-space:pre}
  .step-list{display:flex;flex-direction:column;gap:0;padding:0;border-bottom:1px solid var(--line)}
  .step-row{display:grid;grid-template-columns:24px 1fr auto;gap:10px;padding:10px 18px;
    border-bottom:1px solid var(--line);align-items:center;font-size:12.5px;
    transition:background .15s}
  .step-row:last-child{border-bottom:none}
  .step-row .marker{font-family:var(--mono);font-size:11px;color:var(--fg-4);text-align:center}
  .step-row.done .marker{color:var(--ok)}
  .step-row.running .marker{color:var(--ac);animation:pulse 1.4s infinite}
  .step-row.failed .marker{color:var(--bad)}
  .step-row .name{color:var(--fg-2)}
  .step-row.done .name{color:var(--fg)}
  .step-row .info{font-family:var(--mono);font-size:11px;color:var(--fg-3)}

  .gate-banner{padding:14px 20px;border-bottom:1px solid var(--line);display:flex;
    align-items:flex-start;gap:12px;font-size:13px;line-height:1.55}
  .gate-banner.proceed{background:rgba(74,222,128,.05);border-bottom-color:rgba(74,222,128,.2)}
  .gate-banner.reject{background:rgba(248,113,113,.05);border-bottom-color:rgba(248,113,113,.2)}
  .gate-banner.warn{background:rgba(251,191,36,.05);border-bottom-color:rgba(251,191,36,.2)}
  .gate-banner .badge{padding:3px 9px;border-radius:4px;font-family:var(--mono);font-size:11px;
    font-weight:600;text-transform:uppercase;letter-spacing:.05em;flex-shrink:0;margin-top:1px}
  .gate-banner.proceed .badge{background:rgba(74,222,128,.12);color:var(--ok)}
  .gate-banner.reject .badge{background:rgba(248,113,113,.12);color:var(--bad)}
  .gate-banner.warn .badge{background:rgba(251,191,36,.12);color:var(--warn)}
  .gate-banner .reasons{color:var(--fg-2);font-family:var(--mono);font-size:12px}
  .gate-banner .reasons div{margin-top:3px}

  .json-view{flex:1;overflow:auto;padding:14px 18px;background:var(--bg)}
  .json-view pre{margin:0;font-family:var(--mono);font-size:12px;line-height:1.65;
    color:var(--fg);white-space:pre-wrap;word-break:break-word}
  .err-view{padding:14px 18px;font-family:var(--mono);font-size:12px;line-height:1.6;
    color:var(--bad);white-space:pre-wrap;word-break:break-word;flex:1;overflow:auto}

  /* === Drawer (right side, slides in) === */
  .drawer{position:fixed;top:48px;right:-460px;width:460px;height:calc(100vh - 48px);
    background:var(--surface-2);border-left:1px solid var(--line);
    transition:right .25s ease;z-index:25;display:flex;flex-direction:column;
    box-shadow:-8px 0 24px rgba(0,0,0,.4)}
  .drawer.open{right:0}
  .drawer-tabs{display:flex;border-bottom:1px solid var(--line);flex-shrink:0}
  .drawer-tabs button{flex:1;background:transparent;border:none;color:var(--fg-3);
    padding:12px 16px;font-size:12px;cursor:pointer;border-bottom:2px solid transparent;
    transition:all .15s;font-family:var(--sans);font-weight:500}
  .drawer-tabs button:hover{color:var(--fg-2)}
  .drawer-tabs button.active{color:var(--ac);border-bottom-color:var(--ac)}
  .drawer-close{position:absolute;top:8px;right:10px;background:transparent;
    border:none;color:var(--fg-3);font-size:18px;cursor:pointer;width:24px;height:24px;
    border-radius:4px;display:grid;place-items:center}
  .drawer-close:hover{background:var(--surface);color:var(--fg)}
  .drawer-panel{flex:1;overflow-y:auto;display:none;padding:18px 20px}
  .drawer-panel.active{display:block}
  .drawer-panel h4{margin:0 0 4px;font-size:13px;font-weight:600;color:var(--fg);letter-spacing:-.005em}
  .drawer-panel .sub-id{font-family:var(--mono);font-size:11px;color:var(--ac);margin-bottom:8px}
  .drawer-panel .meta-row{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px}
  .drawer-panel .chip{font-family:var(--mono);font-size:10px;padding:2px 7px;
    border-radius:3px;background:var(--surface);border:1px solid var(--line);color:var(--fg-3)}
  .drawer-panel .chip.ac{color:var(--ac);border-color:rgba(16,185,129,.3)}
  .drawer-panel pre.body{margin:0 0 18px;background:var(--bg);border:1px solid var(--line);
    border-radius:6px;padding:10px 12px;font-family:var(--mono);font-size:11.5px;
    line-height:1.55;color:var(--fg-2);white-space:pre-wrap;word-break:break-word;
    max-height:240px;overflow-y:auto}
  .drawer-panel .placeholder{color:var(--ac)}
  .drawer-panel hr{border:none;border-top:1px solid var(--line);margin:14px 0}
  .info-grid{display:grid;grid-template-columns:max-content 1fr;gap:7px 14px;
    font-size:12px;margin-bottom:18px}
  .info-grid dt{color:var(--fg-3)}
  .info-grid dd{margin:0;color:var(--fg);font-family:var(--mono);font-size:11.5px}

  /* Status pill in topbar */
  .status-pill{display:inline-flex;align-items:center;gap:6px;font-family:var(--mono);
    font-size:11px;padding:2px 9px;border-radius:3px;font-weight:600;text-transform:uppercase;
    letter-spacing:.05em}
  .status-pill.queued{background:rgba(168,174,184,.10);color:var(--fg-2)}
  .status-pill.running{background:rgba(251,191,36,.10);color:var(--warn)}
  .status-pill.succeeded{background:rgba(74,222,128,.10);color:var(--ok)}
  .status-pill.failed{background:rgba(248,113,113,.10);color:var(--bad)}

  @media(max-width:880px){
    .workspace{grid-template-columns:1fr;grid-template-rows:1fr 1px 1fr}
    .divider{height:1px;width:auto}
    .drawer{width:100%;right:-100%}
  }
</style></head>
<body>
<div class="topbar">
  <a class="brand-link" href="/tools" title="天枢 · 裁决 · 返回主页" style="text-decoration:none;display:inline-flex;align-items:center;gap:10px;font-family:'Noto Serif SC',Georgia,serif;font-size:19px;font-weight:600;letter-spacing:.18em;color:var(--fg);margin-right:24px;padding:4px 0"><svg viewBox="0 0 24 24" fill="currentColor" width="24" height="24" aria-hidden="true" style="color:var(--ac);opacity:1;flex-shrink:0;filter:drop-shadow(0 1px 2px rgba(196,90,58,.28))"><path d="M12 1.5L13.4 10.6L22.5 12L13.4 13.4L12 22.5L10.6 13.4L1.5 12L10.6 10.6Z"/></svg><span>天枢</span><span style="color:var(--ac);margin:0 6px;font-weight:400">·</span><span>裁决</span></a>
  <div class="crumbs">
    <a href="/tools">工具</a>
    <span class="sep">/</span>
    <span class="current"><span class="icon" id="tb-icon">·</span><span id="tb-name">…</span></span>
    <span class="sep" id="tb-sep" style="display:none">·</span>
    <span id="tb-status"></span>
  </div>
  <div class="stats">
    <span class="stat"><span>tokens in</span><span class="v" id="s-in">—</span></span>
    <span class="stat"><span>out</span><span class="v" id="s-out">—</span></span>
    <span class="stat"><span>cache</span><span class="v" id="s-cache">—</span></span>
    <span class="stat cost"><span>$</span><span class="v" id="s-cost">—</span></span>
    <span class="stat" id="s-elapsed-wrap" style="display:none"><span>·</span><span class="v" id="s-elapsed"></span></span>
    <span class="stat gate" id="s-gate-wrap" style="display:none"><span>gate</span><span class="v" id="s-gate"></span></span>
  </div>
  <div class="actions">
    <button class="icon-btn" id="btn-info" title="工具信息">ⓘ</button>
    <button class="icon-btn" id="btn-prompts" title="提示词">P</button>
    <button class="icon-btn" id="btn-history" title="历史">H</button>
    <button class="run" id="btn-run">▶ 运行<kbd>⌘↵</kbd></button>
  </div>
</div>

<div class="workspace">
  <div class="pane input">
    <div class="pane-head">
      <span class="label"><span class="dot"></span>输入</span>
      <span class="right" id="input-meta"></span>
    </div>
    <div class="pane-body">
      <form id="form"></form>
    </div>
  </div>
  <div class="divider"></div>
  <div class="pane output">
    <div class="pane-head">
      <span class="label"><span class="dot" id="out-dot"></span>输出</span>
      <span class="right" id="output-meta"></span>
    </div>
    <div id="output-area" style="flex:1;display:flex;flex-direction:column;overflow:hidden">
      <div class="out-empty">
        <div class="ascii">┌──────────────┐
│   no run     │
│   yet        │
└──────────────┘</div>
        <div>填好左侧表单后按 <strong>⌘↵</strong> 或 <strong>▶ 运行</strong></div>
      </div>
    </div>
  </div>
</div>

<!-- Drawer (info / prompts / history) -->
<div class="drawer" id="drawer">
  <button class="drawer-close" onclick="closeDrawer()">×</button>
  <div class="drawer-tabs">
    <button data-tab="info" class="active">信息</button>
    <button data-tab="prompts">提示词</button>
    <button data-tab="history">历史</button>
  </div>
  <div class="drawer-panel active" data-panel="info" id="panel-info"></div>
  <div class="drawer-panel" data-panel="prompts" id="panel-prompts"></div>
  <div class="drawer-panel" data-panel="history" id="panel-history"></div>
</div>

<script>
const TOOL_ID = location.pathname.split('/').filter(Boolean).pop();
let tool = null;
let pollTimer = null;
let elapsedTimer = null;
let currentRunId = new URLSearchParams(location.search).get('run');
let currentRun = null;

async function load(){
  tool = await fetch('/api/tools/' + TOOL_ID).then(r=>r.json());
  document.title = tool.name + ' — 天枢·裁决';
  document.getElementById('tb-icon').textContent = tool.icon;
  document.getElementById('tb-name').textContent = tool.name;
  document.getElementById('input-meta').textContent = tool.fields.length + ' 字段';
  document.getElementById('output-meta').textContent = tool.prompts.length + ' substeps · ' + tool.output.replace(/[《》]/g,'');

  buildForm();
  buildInfoPanel();
  buildPromptsPanel();
  buildHistoryPanel();
  if (currentRunId) startPolling(currentRunId);
}

// ---------- form ----------
function buildForm(){
  const form = document.getElementById('form');
  tool.fields.forEach((f,i) => {
    const wrap = document.createElement('div');
    wrap.className = 'field';
    if (f.type === 'checkbox'){
      wrap.innerHTML = `
        <div class="checkbox-row">
          <input type="checkbox" id="f-${f.key}" ${f.default ? 'checked' : ''}>
          <label for="f-${f.key}">${f.label}</label>
        </div>`;
    } else {
      const isJson = f.type === 'json-file' || f.type === 'json-text';
      const placeholder = isJson ? '粘贴 JSON 或上传 .json' : '粘贴文本或上传文件';
      const minH = isJson ? 'min-height:90px' : 'min-height:64px';
      wrap.innerHTML = `
        <div class="field-head">
          <label for="f-${f.key}">${f.label}</label>
          ${f.required ? '<span class="req">*</span>' : '<span class="opt">optional</span>'}
          <button type="button" class="upload-btn" data-target="f-${f.key}" data-accept="${f.accept || '*'}">↑ 上传</button>
        </div>
        <textarea id="f-${f.key}" placeholder="${placeholder}" style="${minH}">${f.default || ''}</textarea>
      `;
    }
    form.appendChild(wrap);
  });
  // upload-btn handlers
  form.querySelectorAll('.upload-btn').forEach(btn => {
    btn.onclick = () => {
      const inp = document.createElement('input');
      inp.type = 'file';
      inp.accept = btn.dataset.accept;
      inp.onchange = async e => {
        const file = inp.files[0];
        if (!file) return;
        document.getElementById(btn.dataset.target).value = await file.text();
      };
      inp.click();
    };
  });
}

async function runTool(){
  if (!tool) return;
  const body = {};
  for (const f of tool.fields){
    const el = document.getElementById('f-' + f.key);
    if (!el) continue;
    if (f.type === 'checkbox'){ body[f.key] = el.checked; continue; }
    const v = el.value.trim();
    if (!v && !f.required) continue;
    if (f.type === 'json-file' || f.type === 'json-text'){
      try { body[f.key] = JSON.parse(v); }
      catch(err){ alert(`字段「${f.label}」不是合法 JSON：${err.message}`); return; }
    } else {
      if (v) body[f.key] = v;
    }
  }
  const btn = document.getElementById('btn-run');
  btn.disabled = true; btn.innerHTML = '提交中…';
  try {
    const res = await fetch(`/api/tools/${tool.id}/run`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    }).then(r => r.json());
    if (!res.run_id){ alert('启动失败：' + JSON.stringify(res)); return; }
    currentRunId = res.run_id;
    history.replaceState(null, '', `?run=${res.run_id}`);
    startPolling(res.run_id);
  } finally {
    btn.disabled = false; btn.innerHTML = '▶ 运行<kbd>⌘↵</kbd>';
  }
}

document.getElementById('btn-run').onclick = runTool;
document.addEventListener('keydown', e => {
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter'){ e.preventDefault(); runTool(); }
});
// 右侧 56px 窄条上的 ▶ 圆按钮也能触发运行
document.addEventListener('click', e => {
  const t = e.target;
  if (t && (t.id === 'play-circle' || (t.closest && t.closest('#play-circle')))) {
    runTool();
  }
});

// ---------- polling & rendering ----------
function startPolling(runId){
  if (pollTimer) clearInterval(pollTimer);
  if (elapsedTimer) clearInterval(elapsedTimer);
  document.getElementById('out-dot').classList.add('live');
  poll(runId);
  pollTimer = setInterval(()=>poll(runId), 3000);
  elapsedTimer = setInterval(updateElapsed, 1000);
}

async function poll(runId){
  try {
    const r = await fetch(`/api/tools/runs/${runId}`).then(r => r.json());
    currentRun = r;
    renderRun(r);
    if (r.status === 'succeeded' || r.status === 'failed'){
      clearInterval(pollTimer); pollTimer = null;
      clearInterval(elapsedTimer); elapsedTimer = null;
      document.getElementById('out-dot').classList.remove('live');
      buildHistoryPanel();
    }
  } catch(e){
    clearInterval(pollTimer); pollTimer = null;
    clearInterval(elapsedTimer); elapsedTimer = null;
  }
}

function updateElapsed(){
  if (!currentRun || currentRun.finished_at) return;
  const e = Date.now()/1000 - currentRun.started_at;
  document.getElementById('s-elapsed').textContent = e.toFixed(1) + 's';
  document.getElementById('s-elapsed-wrap').style.display = '';
}

function renderRun(r){
  // top-bar status pill
  const tb = document.getElementById('tb-status');
  document.getElementById('tb-sep').style.display = '';
  tb.innerHTML = `<span class="status-pill ${r.status}">${r.status}</span>`;

  // top-bar usage
  const u = r.usage || {};
  document.getElementById('s-in').textContent = u.input_tokens != null ? u.input_tokens.toLocaleString() : '—';
  document.getElementById('s-out').textContent = u.output_tokens != null ? u.output_tokens.toLocaleString() : '—';
  document.getElementById('s-cache').textContent = u.cache_read_tokens != null ? u.cache_read_tokens.toLocaleString() : '—';
  document.getElementById('s-cost').textContent = u.cost_usd != null ? u.cost_usd.toFixed(4) : '—';
  if (r.finished_at){
    document.getElementById('s-elapsed').textContent = (r.finished_at - r.started_at).toFixed(1) + 's';
    document.getElementById('s-elapsed-wrap').style.display = '';
  }

  // gate
  if (r.report && r.report.gate_decision){
    const g = r.report.gate_decision;
    const cls = gateClass(g.action);
    const wrap = document.getElementById('s-gate-wrap');
    wrap.style.display = '';
    wrap.classList.remove('reject','proceed','warn');
    wrap.classList.add(cls);
    document.getElementById('s-gate').textContent = g.action.replace(/^GateAction\\./,'').toLowerCase();
  }

  // output area
  renderOutput(r);
}

function gateClass(action){
  const a = String(action || '').toLowerCase();
  if (a.includes('reject')) return 'reject';
  if (a.includes('proceed') || a.includes('approve')) return 'proceed';
  return 'warn';
}

function renderOutput(r){
  const area = document.getElementById('output-area');
  if (r.status === 'failed'){
    area.innerHTML = `<div class="err-view">${escapeHtml(r.traceback || r.error || 'failed')}</div>`;
    return;
  }
  // build progress steplist (synthetic — based on prompts)
  const stepHtml = renderSteps(r);
  let gateHtml = '';
  if (r.report && r.report.gate_decision){
    const g = r.report.gate_decision;
    const cls = gateClass(g.action);
    const reasons = (g.reasons || []).map(x => `<div>· ${escapeHtml(x)}</div>`).join('');
    gateHtml = `<div class="gate-banner ${cls}"><span class="badge">${g.action.replace(/^GateAction\\./,'').toLowerCase()}</span><div class="reasons"><strong>${escapeHtml(g.action)}</strong>${reasons}</div></div>`;
  }
  let jsonHtml = '';
  if (r.report){
    jsonHtml = `<div class="json-view"><pre>${escapeHtml(JSON.stringify(r.report, null, 2))}</pre></div>`;
  } else {
    jsonHtml = `<div class="out-empty"><div>${escapeHtml(r.progress || '运行中…')}</div></div>`;
  }
  area.innerHTML = stepHtml + gateHtml + jsonHtml;
}

function renderSteps(r){
  if (!tool || !tool.prompts) return '';
  // Heuristic: if status==succeeded → all done; if running → guess from progress text
  const done = r.status === 'succeeded';
  const failed = r.status === 'failed';
  let html = '<div class="step-list">';
  tool.prompts.forEach((p, i) => {
    let cls = '';
    let marker = '○';
    if (done){ cls = 'done'; marker = '✓'; }
    else if (failed && i === 0){ cls = 'failed'; marker = '✗'; }
    else if (r.status === 'running' && i === 0){ cls = 'running'; marker = '◔'; }
    const shortP = String(p).split('.').pop();
    html += `<div class="step-row ${cls}"><span class="marker">${marker}</span><span class="name" title="${p}">#${shortP}</span><span class="info">${cls === 'done' ? 'OK' : (cls === 'running' ? '执行中' : '')}</span></div>`;
  });
  html += '</div>';
  return html;
}

function escapeHtml(s){ return String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }

// ---------- drawer ----------
const drawer = document.getElementById('drawer');
function openDrawer(tab){
  drawer.classList.add('open');
  document.querySelectorAll('.drawer-tabs button').forEach(b => b.classList.toggle('active', b.dataset.tab===tab));
  document.querySelectorAll('.drawer-panel').forEach(p => p.classList.toggle('active', p.dataset.panel===tab));
}
function closeDrawer(){ drawer.classList.remove('open'); }
window.closeDrawer = closeDrawer;

document.getElementById('btn-info').onclick = () => openDrawer('info');
document.getElementById('btn-prompts').onclick = () => openDrawer('prompts');
document.getElementById('btn-history').onclick = () => openDrawer('history');
document.querySelectorAll('.drawer-tabs button').forEach(b => {
  b.onclick = () => openDrawer(b.dataset.tab);
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeDrawer();
});

function buildInfoPanel(){
  const el = document.getElementById('panel-info');
  el.innerHTML = `
    <div style="display:flex;gap:14px;align-items:flex-start;margin-bottom:18px">
      <div style="font-size:30px">${tool.icon}</div>
      <div>
        <h4>${tool.name}</h4>
        <div class="sub-id">${tool.step}</div>
      </div>
    </div>
    <p style="font-size:13px;color:var(--fg-2);line-height:1.65;margin:0 0 18px">${tool.description}</p>
    <dl class="info-grid">
      <dt>主责</dt><dd>${tool.responsible}</dd>
      <dt>核心输出</dt><dd>${tool.output}</dd>
      <dt>提示词</dt><dd>${tool.prompts.length} × ${tool.prompts[0]}–${tool.prompts.slice(-1)[0]}</dd>
      <dt>API</dt><dd><a href="/api/tools/${tool.id}" target="_blank" style="color:var(--ac)">${tool.endpoint}</a></dd>
    </dl>
    <hr>
    <h4 style="margin-bottom:6px;color:var(--warn)">⚠ 闸门规则</h4>
    <p style="font-size:12px;color:var(--fg-2);line-height:1.6;margin:0">${tool.gate}</p>
  `;
}

async function buildPromptsPanel(){
  const el = document.getElementById('panel-prompts');
  el.innerHTML = '<div style="color:var(--fg-3);font-size:12px;padding:10px 0">加载中…</div>';
  // Use the prompt step's metadata loaded by /api/prompts
  const all = await fetch('/api/prompts').then(r=>r.json());
  const step = all.steps.find(s => s.dir_name === tool.prompt_dir);
  if (!step){ el.innerHTML = '<div>找不到提示词</div>'; return; }
  el.innerHTML = '';
  step.substeps.forEach(s => {
    const div = document.createElement('div');
    const ph = (s.placeholders || []).map(p => `<span class="chip ac">${p}</span>`).join('');
    div.innerHTML = `
      <h4>${s.name}</h4>
      <div class="sub-id">${s.id} · ${s.model_tier} · max_tokens ${s.max_tokens}</div>
      <div class="meta-row">${ph}</div>
      <pre class="body">${highlightPlaceholders(escapeHtml(s.body))}</pre>
    `;
    el.appendChild(div);
  });
}

function highlightPlaceholders(s){
  return s.replace(/\\{\\{[^}]+\\}\\}/g, m => `<span class="placeholder">${m}</span>`);
}

async function buildHistoryPanel(){
  const el = document.getElementById('panel-history');
  el.innerHTML = '<div style="color:var(--fg-3);font-size:12px">加载中…</div>';
  const data = await fetch('/api/tools/runs').then(r=>r.json());
  const mine = data.recent.filter(r => r.tool_id === tool.id);
  if (!mine.length){ el.innerHTML = '<div style="color:var(--fg-3);font-size:12px;font-family:var(--mono)">尚无运行记录</div>'; return; }
  el.innerHTML = '';
  mine.forEach(run => {
    const div = document.createElement('a');
    div.href = `?run=${run.run_id}`;
    div.style.cssText = 'display:block;text-decoration:none;color:inherit;background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:10px 12px;margin-bottom:6px;transition:all .15s';
    div.onmouseover = () => div.style.borderColor = 'var(--ac)';
    div.onmouseout = () => div.style.borderColor = 'var(--line)';
    const t = new Date(run.started_at*1000).toLocaleString();
    div.innerHTML = `
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
        <span class="status-pill ${run.status}">${run.status}</span>
        <span style="font-family:var(--mono);font-size:11px;color:var(--fg-3)">${t}</span>
      </div>
      <div style="font-family:var(--mono);font-size:11px;color:var(--fg-3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${run.run_id}</div>
    `;
    el.appendChild(div);
  });
}

load();
</script>
</body></html>
"""


_SHARED_OVERLAY_SNIPPET = """
<script>
(function sharedOverlays(){
  // Idempotent: skip if the page already has the overlays
  if (document.getElementById('task-fab') && document.getElementById('cmd-overlay')) return;
  const css = `
.cmd-overlay{position:fixed;inset:0;z-index:200;background:rgba(0,0,0,.55);
  -webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px);
  display:none;align-items:flex-start;justify-content:center;padding-top:14vh}
.cmd-overlay.show{display:flex}
.cmd-panel{width:580px;max-width:90vw;background:var(--surface-2,#171b22);
  border:1px solid var(--line-2,#2a3140);border-radius:14px;
  box-shadow:0 12px 32px rgba(0,0,0,.42);overflow:hidden;
  animation:cmd-in 200ms cubic-bezier(.5,1.4,.3,.95)}
@keyframes cmd-in{from{opacity:0;transform:translateY(-12px) scale(.97)}to{opacity:1;transform:translateY(0) scale(1)}}
.cmd-input{width:100%;padding:16px;border:none;outline:none;background:transparent;
  color:var(--fg,#f5f7fa);font-size:16px;
  font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","PingFang SC",sans-serif;
  border-bottom:1px solid var(--line-1,#1e242d)}
.cmd-input::placeholder{color:var(--fg-3,#6b7380)}
.cmd-results{max-height:380px;overflow-y:auto;padding:8px}
.cmd-result{padding:10px 12px;display:flex;align-items:center;gap:12px;cursor:pointer;
  border-radius:6px;transition:background .12s}
.cmd-result.active{background:var(--surface-3,#1d222b)}
.cmd-result .icon{font-size:18px;opacity:.85}
.cmd-result .name{font-size:13px;color:var(--fg,#f5f7fa)}
.cmd-result .hint{margin-left:auto;font-family:"SF Mono",ui-monospace,monospace;
  font-size:10.5px;color:var(--fg-3,#6b7380)}
.cmd-empty{padding:24px;text-align:center;color:var(--fg-3,#6b7380);font-size:13px}
.task-fab-shared{position:fixed;bottom:24px;right:24px;z-index:90;display:none;
  background:var(--surface-2,#171b22);border:1px solid var(--line-2,#2a3140);
  border-radius:10px;box-shadow:0 12px 32px rgba(0,0,0,.42);width:300px;overflow:hidden;
  animation:fab-in 200ms cubic-bezier(.5,1.4,.3,.95)}
.task-fab-shared.show{display:block}
@keyframes fab-in{from{opacity:0;transform:translateY(20px) scale(.94)}to{opacity:1;transform:translateY(0) scale(1)}}
.task-fab-shared .head{display:flex;align-items:center;gap:8px;padding:10px 12px;
  border-bottom:1px solid var(--line-1,#1e242d);background:var(--surface-3,#1d222b)}
.task-fab-shared .head .dot{width:8px;height:8px;border-radius:50%;background:#a78bfa;
  animation:fab-pulse 1.4s ease-in-out infinite}
@keyframes fab-pulse{0%,100%{opacity:.4;transform:scale(.85)}50%{opacity:1;transform:scale(1.05)}}
.task-fab-shared .head .label{font-size:11px;font-weight:600;text-transform:uppercase;
  letter-spacing:.04em;color:var(--fg-2,#a8b0bd)}
.task-fab-shared .head .count{margin-left:auto;font-family:"SF Mono",ui-monospace,monospace;
  font-size:10.5px;color:var(--fg-3,#6b7380);padding:2px 7px;border-radius:999px;background:var(--surface,#101216)}
.task-fab-shared .body{padding:8px;max-height:432px;overflow-y:auto;
  scrollbar-width:thin;scrollbar-color:var(--line-2) transparent}
.task-fab-shared .body::-webkit-scrollbar{width:6px}
.task-fab-shared .body::-webkit-scrollbar-thumb{background:var(--line-2);border-radius:3px}
.task-fab-shared .body::-webkit-scrollbar-track{background:transparent}
.task-row-shared{display:block;padding:8px 12px;border-radius:6px;cursor:pointer;
  transition:background .12s;text-decoration:none;color:inherit}
.task-row-shared:hover{background:var(--surface,#101216)}
.task-row-shared .name{font-size:13px;font-weight:500;color:var(--fg,#f5f7fa)}
.task-row-shared .progress{font-size:10.5px;font-family:"SF Mono",ui-monospace,monospace;
  color:var(--fg-3,#6b7380);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.task-row-shared .bar{height:2px;background:var(--surface,#101216);border-radius:2px;
  overflow:hidden;margin-top:6px;position:relative}
.task-row-shared .bar-fill{height:100%;width:30%;
  background:linear-gradient(90deg,#a78bfa,#10b981);border-radius:2px;position:absolute;
  animation:indet-shared 1.6s ease-in-out infinite}
@keyframes indet-shared{0%{left:-30%}100%{left:100%}}
`;
  const style = document.createElement('style');
  style.textContent = css;
  document.head.appendChild(style);

  // DOM
  const overlay = document.createElement('div');
  overlay.className = 'cmd-overlay';
  overlay.id = 'cmd-overlay';
  overlay.innerHTML = `
    <div class="cmd-panel">
      <input class="cmd-input" id="cmd-input-shared" placeholder="输入工具名 / 报告 / 设置…" autocomplete="off">
      <div class="cmd-results" id="cmd-results-shared"></div>
    </div>`;
  document.body.appendChild(overlay);

  const fab = document.createElement('aside');
  fab.className = 'task-fab-shared';
  fab.id = 'task-fab';
  fab.innerHTML = `
    <div class="head">
      <span class="dot"></span>
      <span class="label">运行中</span>
      <span class="count" id="task-count-shared">0</span>
    </div>
    <div class="body" id="task-list-shared"></div>`;
  document.body.appendChild(fab);

  // Tools data fetched once
  let tools = [];
  fetch('/api/tools').then(r => r.json()).then(c => { tools = c.tools || []; });

  function escapeHtml(s){ return String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }

  // ⌘K palette
  const cmdInput = document.getElementById('cmd-input-shared');
  const cmdResults = document.getElementById('cmd-results-shared');
  let cmdActive = 0;
  function openCmd(){
    overlay.classList.add('show');
    setTimeout(() => cmdInput.focus(), 50);
    renderCmd('');
  }
  function closeCmd(){ overlay.classList.remove('show'); cmdInput.value = ''; }
  function renderCmd(q){
    const items = [
      ...tools.map(t => ({icon:t.icon, name:t.name, hint:t.id, href:`/tools/${t.id}`})),
      {icon:'📊', name:'查看报告', hint:'reports', href:'/reports'},
      {icon:'⚙', name:'设置 / 环境', hint:'settings', href:'/settings'},
    ];
    const filtered = q
      ? items.filter(x => x.name.toLowerCase().includes(q.toLowerCase()) || x.hint.toLowerCase().includes(q.toLowerCase()))
      : items;
    cmdActive = 0;
    cmdResults.innerHTML = filtered.length
      ? filtered.map((x, i) => `<div class="cmd-result ${i===0?'active':''}" data-href="${x.href}" data-idx="${i}">
          <span class="icon">${x.icon}</span>
          <span class="name">${escapeHtml(x.name)}</span>
          <span class="hint">${escapeHtml(x.hint)}</span></div>`).join('')
      : '<div class="cmd-empty">无匹配项</div>';
    cmdResults.querySelectorAll('.cmd-result').forEach(el => {
      el.onclick = () => location.href = el.dataset.href;
      el.onmouseenter = () => {
        cmdResults.querySelectorAll('.cmd-result').forEach(x => x.classList.remove('active'));
        el.classList.add('active');
        cmdActive = parseInt(el.dataset.idx, 10);
      };
    });
  }
  document.addEventListener('keydown', e => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); openCmd(); }
    else if (overlay.classList.contains('show')) {
      if (e.key === 'Escape') { closeCmd(); return; }
      const items = cmdResults.querySelectorAll('.cmd-result');
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        cmdActive = Math.min(cmdActive + 1, items.length - 1);
        items.forEach((el, i) => el.classList.toggle('active', i === cmdActive));
        items[cmdActive] && items[cmdActive].scrollIntoView({block:'nearest'});
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        cmdActive = Math.max(cmdActive - 1, 0);
        items.forEach((el, i) => el.classList.toggle('active', i === cmdActive));
        items[cmdActive] && items[cmdActive].scrollIntoView({block:'nearest'});
      } else if (e.key === 'Enter') {
        e.preventDefault();
        const sel = items[cmdActive];
        if (sel) location.href = sel.dataset.href;
      }
    }
  });
  cmdInput.addEventListener('input', () => renderCmd(cmdInput.value));
  overlay.addEventListener('click', e => { if (e.target === overlay) closeCmd(); });
  // Make any element with id="cmd-trigger" or class "kbd-hint" open the palette
  document.querySelectorAll('#cmd-trigger,.kbd-hint').forEach(el => el.addEventListener('click', openCmd));

  // Floating running tasks widget
  async function pollTasks(){
    try {
      const data = await fetch('/api/tools/runs').then(r => r.json());
      const runs = (data.recent || []).filter(r => r.status === 'running' || r.status === 'queued');
      const list = document.getElementById('task-list-shared');
      if (!runs.length) { fab.classList.remove('show'); return; }
      document.getElementById('task-count-shared').textContent = runs.length;
      // 显示全部任务;最多 6 个在视口内,多了通过滚动条访问
      list.innerHTML = runs.map(t => {
        const elapsed = Math.max(0, Math.floor(Date.now()/1000 - (t.started_at||0)));
        const min = String(Math.floor(elapsed/60)).padStart(2,'0');
        const sec = String(elapsed%60).padStart(2,'0');
        return `<a class="task-row-shared" href="/tools/${t.tool_id}?run=${t.run_id}">
          <div class="name">${escapeHtml(t.tool_name)}</div>
          <div class="progress">${escapeHtml(t.progress || t.status)} · ${min}:${sec}</div>
          <div class="bar"><div class="bar-fill"></div></div></a>`;
      }).join('');
      fab.classList.add('show');
    } catch(e){}
  }
  pollTasks();
  setInterval(pollTasks, 4000);
})();

// ── 共享:登录用户徽标 + 登出按钮 + admin-only 导航链接可见性 ──
(function sharedAuthWidget(){
  if (document.getElementById('shared-auth-widget')) return;
  // 注意:这里改成把 widget 注入到 topbar 内部成为自然 inline 元素,不再 position:fixed,
  // 否则会覆盖 nav 右侧的 admin-only 链接(实测:widget 占据 nav 同一区域,叠在上面挡住"用户管理")。
  const css = `
  .shared-auth-widget{
    display:inline-flex;align-items:center;gap:10px;
    font-family:'Inter','SF Mono',monospace;font-size:12.5px;color:var(--fg-2,#262626);
    background:rgba(255,255,255,.94);
    border:1px solid var(--line-1,#9e9e9e);border-radius:999px;padding:4px 4px 4px 14px;
    margin-left:14px;flex-shrink:0;white-space:nowrap}
  .shared-auth-widget .admin-tag{color:#a8401f;font-size:10.5px;letter-spacing:.18em;font-weight:600}
  .shared-auth-widget .name{font-weight:600;color:var(--fg,#0a0a0a)}
  .shared-auth-widget .logout{background:none;border:1px solid var(--line-1,#9e9e9e);
    color:var(--fg-2,#262626);padding:4px 12px;border-radius:999px;
    font-family:inherit;font-size:11.5px;cursor:pointer;letter-spacing:.04em;
    transition:all .15s;font-weight:500}
  .shared-auth-widget .logout:hover{border-color:#a8401f;color:#a8401f}
  /* fallback: 没找到 topbar 时退回右上浮动 */
  .shared-auth-widget.shared-auth-floating{
    position:fixed;top:14px;right:18px;z-index:80;margin-left:0;
    box-shadow:0 1px 3px rgba(0,0,0,.06)}

  /* === 测试用例工具(step2)报告:用例表 + Excel 主体 === */
  .tc-block{margin:16px 18px;border:1px solid var(--line-1,#c4c4c4);border-radius:8px;overflow:hidden;background:#fff}
  .tc-banner{display:flex;align-items:center;gap:18px;padding:18px 22px;
    background:linear-gradient(135deg,#f4f1e6,#ece8da);border-bottom:1px solid var(--line-1,#c4c4c4)}
  .tc-banner .tc-count{font-family:'Noto Serif SC',Georgia,serif;font-size:38px;font-weight:600;
    color:#a8401f;line-height:1}
  .tc-banner .tc-count-label{font-size:12px;color:var(--fg-2,#262626);letter-spacing:.1em;margin-top:2px}
  .tc-banner .tc-mid{flex:1;font-size:13px;color:var(--fg-2,#262626);line-height:1.7}
  .tc-banner .tc-mid b{color:#a8401f}
  .tc-excel-btn{background:#1a7a3a;color:#fff;border:none;border-radius:5px;
    padding:13px 22px;font-family:'Noto Sans SC',sans-serif;font-size:14px;font-weight:600;
    letter-spacing:.06em;cursor:pointer;white-space:nowrap;transition:background .15s;
    box-shadow:0 2px 6px rgba(26,122,58,.28)}
  .tc-excel-btn:hover{background:#15692f}
  .tc-excel-btn:active{transform:translateY(1px)}
  .tc-table-wrap{overflow-x:auto;max-height:560px;overflow-y:auto}
  .tc-table{width:100%;border-collapse:collapse;font-size:12.5px}
  .tc-table th{position:sticky;top:0;background:#2e2e2e;color:#fff;font-weight:600;
    padding:9px 10px;text-align:left;white-space:nowrap;font-size:11.5px;letter-spacing:.04em;z-index:1}
  .tc-table td{padding:9px 10px;border-bottom:1px solid #e3e3e3;vertical-align:top;
    color:var(--fg,#0a0a0a);line-height:1.6}
  .tc-table tr:hover td{background:#faf8f2}
  .tc-table .tc-id{font-family:'Inter','SF Mono',monospace;font-size:11.5px;color:#a8401f;
    white-space:nowrap;font-weight:600}
  .tc-table .tc-title{min-width:170px;font-weight:500}
  .tc-table .tc-steps{min-width:240px;white-space:pre-line;color:var(--fg-2,#262626)}
  .tc-table .tc-exp{min-width:200px;color:var(--fg-2,#262626)}
  .tc-table .tc-pre{min-width:130px;color:var(--fg-3,#4a4a4a);font-size:12px}
  .tc-pri{display:inline-block;padding:1px 8px;border-radius:3px;font-size:11px;font-weight:700;
    font-family:'Inter',monospace}
  .tc-pri-P0{background:#f4cccc;color:#7a1c1c}
  .tc-pri-P1{background:#fce5cd;color:#7a3d0c}
  .tc-pri-P2{background:#fff2cc;color:#6b5500}
  .tc-pri-P3{background:#efefef;color:#555}
  `;
  const st = document.createElement('style'); st.textContent = css;
  document.head.appendChild(st);

  // 测试用例 Excel 下载 — 委托点击,所有页面通用
  document.addEventListener('click', async (ev) => {
    const btn = ev.target.closest && ev.target.closest('.tc-excel-btn[data-tc-runid]');
    if (!btn) return;
    ev.preventDefault();
    const runId = btn.dataset.tcRunid;
    const toolId = btn.dataset.tcToolid || 'step2';
    const orig = btn.textContent;
    btn.disabled = true; btn.textContent = '导出中…';
    try {
      const resp = await fetch(`/api/reports/${runId}/export.xlsx`, {credentials:'same-origin'});
      if (!resp.ok){
        btn.textContent = '导出失败'; setTimeout(()=>{btn.textContent=orig;btn.disabled=false;}, 1800);
        return;
      }
      const blob = await resp.blob();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `${toolId}_${runId.slice(0,8)}_测试用例.xlsx`;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(()=>URL.revokeObjectURL(a.href), 1000);
      btn.textContent = '✓ 已下载'; setTimeout(()=>{btn.textContent=orig;btn.disabled=false;}, 1800);
    } catch(e){
      btn.textContent = '导出出错'; setTimeout(()=>{btn.textContent=orig;btn.disabled=false;}, 1800);
    }
  });
  fetch('/api/auth/me').then(r => r.json()).then(d => {
    if (!d || !d.authenticated || !d.user) return;
    const u = d.user;
    // admin-only 元素可见(放在最前,确保即使 widget 注入失败这一步也跑了)
    if (u.role === 'admin'){
      document.querySelectorAll('.admin-only').forEach(el => { el.style.display = ''; });
    }
    // 如果页面已经有自己的用户信息区(如 /admin/users 的 user-chip),跳过 widget 注入
    if (document.getElementById('user-chip') || document.getElementById('user-badge')){
      return;
    }
    const wrap = document.createElement('div');
    wrap.className = 'shared-auth-widget';
    wrap.id = 'shared-auth-widget';
    const tag = u.role === 'admin'
      ? '<span class="admin-tag">[admin]</span>' : '';
    wrap.innerHTML = `${tag}<span class="name">${u.display_name || u.username}</span>` +
      `<button class="logout" title="退出登录">登出</button>`;
    // 找 topbar 把 widget 塞进去成自然元素 — 优先级:header.topbar > .topbar > header
    const host = document.querySelector('header.topbar, .topbar, header');
    if (host){
      host.appendChild(wrap);
    } else {
      // 没找到 topbar — 退回 fixed 右上,加 floating class
      wrap.classList.add('shared-auth-floating');
      document.body.appendChild(wrap);
    }
    wrap.querySelector('.logout').onclick = async () => {
      await fetch('/api/auth/logout', {method:'POST'});
      location.href = '/login';
    };
  }).catch(()=>{});
})();
</script>
"""


def _inject_shared_overlays(html: str) -> str:
    """Inject ⌘K palette + floating tasks widget into pages that don't have them.

    Use rsplit to only target the FINAL `</body></html>` (the real page close).
    Naive .replace breaks pages whose JS template literals contain the same
    string (e.g. report download functions that return a full HTML document).
    """
    parts = html.rsplit("</body></html>", 1)
    if len(parts) != 2:
        return html  # malformed input, do nothing
    return parts[0] + _SHARED_OVERLAY_SNIPPET + "</body></html>" + parts[1]


_AUTH_PAGE_CSS = """
:root {
  --paper:#ffffff; --paper-2:#ebebeb; --paper-3:#dcdcdc;
  --ink:#0a0a0a; --ink-2:#262626; --ink-3:#4a4a4a; --ink-4:#6e6e6e;
  --line:#bdbdbd; --line-2:#9e9e9e;
  --accent:#a8401f; --accent-h:#82301a; --accent-soft:rgba(168,64,31,.14);
  --bad:#8a2d12; --ok:#4f6b35; --gold:#876b1f;
  --serif:'Noto Serif SC','Songti SC','STSong',Georgia,serif;
  --sans:'Noto Sans SC','PingFang SC',-apple-system,'Microsoft YaHei',sans-serif;
  --mono:'Inter','SF Mono',ui-monospace,Menlo,monospace;
}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--paper);color:var(--ink);
  font-family:var(--sans);font-weight:300;font-size:15px;line-height:1.7;
  -webkit-font-smoothing:antialiased;font-feature-settings:"palt" on,"ss01" on}
a{color:inherit;text-decoration:none}
button{font-family:inherit;cursor:pointer}

/* === 两栏布局 === */
.auth-shell{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(0,.9fr);
  min-height:100vh}
@media (max-width:900px){.auth-shell{grid-template-columns:1fr}}

/* === 左侧:品牌叙事区 === */
.brand-side{padding:64px 72px;display:flex;flex-direction:column;justify-content:space-between;
  background:
    radial-gradient(circle at 22% 18%,rgba(184,85,58,.06),transparent 50%),
    radial-gradient(circle at 78% 78%,rgba(169,139,58,.05),transparent 45%),
    linear-gradient(135deg,#f4f1e6 0%,#ebe7d8 100%);
  position:relative;overflow:hidden;border-right:1px solid var(--line)}
.brand-side::before{
  content:"";position:absolute;inset:48px 48px auto auto;width:96px;height:96px;
  background:
    repeating-linear-gradient(45deg,transparent 0 4px,rgba(184,85,58,.05) 4px 5px);
  border-radius:50%;pointer-events:none}
.brand-side::after{
  content:"";position:absolute;left:-160px;bottom:-160px;width:400px;height:400px;
  background:radial-gradient(circle,rgba(184,85,58,.06) 0%,transparent 65%);
  pointer-events:none}
.brand-mark{display:flex;align-items:center;gap:14px;position:relative;z-index:2}
.brand-mark svg{color:var(--accent);filter:drop-shadow(0 2px 6px rgba(184,85,58,.25))}
.brand-name{font-family:var(--serif);font-size:24px;font-weight:600;letter-spacing:.2em;
  color:var(--ink)}
.brand-sep{color:var(--accent);margin:0 6px;font-weight:400}
.brand-body{flex:1;display:flex;flex-direction:column;justify-content:center;
  padding:48px 0;position:relative;z-index:2;max-width:520px}
.eyebrow{font-family:var(--mono);font-size:11.5px;letter-spacing:.4em;color:var(--ink-3);
  text-transform:uppercase;margin-bottom:32px;border-left:2px solid var(--accent);padding-left:14px}
.brand-headline{font-family:var(--serif);font-size:40px;font-weight:500;
  letter-spacing:.04em;line-height:1.35;margin:0 0 24px;color:var(--ink)}
.brand-headline em{font-style:normal;color:var(--accent);font-weight:600}
.brand-lede{font-size:15px;color:var(--ink-2);line-height:1.85;margin:0 0 36px;
  font-weight:300;max-width:460px}
.brand-pillars{display:grid;grid-template-columns:repeat(2,1fr);gap:20px;
  margin-top:24px;max-width:480px}
.pillar{padding:14px 0;border-top:1px solid var(--line);font-size:13px;color:var(--ink-2)}
.pillar-no{font-family:var(--mono);font-size:11px;color:var(--accent);letter-spacing:.18em;
  margin-bottom:4px}
.brand-foot{position:relative;z-index:2;display:flex;align-items:center;justify-content:space-between;
  font-family:var(--mono);font-size:11px;color:var(--ink-3);letter-spacing:.12em}

/* === 右侧:表单区 === */
.form-side{padding:64px 56px;display:flex;flex-direction:column;justify-content:center;
  background:var(--paper);position:relative}
@media (max-width:900px){.form-side{padding:48px 32px}}
.form-card{width:100%;max-width:380px;margin:0 auto}
.notice{padding:14px 16px;background:linear-gradient(180deg,var(--accent-soft),rgba(184,85,58,.02));
  border-left:3px solid var(--accent);border-radius:0 4px 4px 0;
  color:var(--ink);font-size:13px;line-height:1.7;margin-bottom:32px}
.notice strong{color:var(--accent);font-weight:600}
.form-title{font-family:var(--serif);font-size:30px;font-weight:500;letter-spacing:.05em;
  margin:0 0 8px;color:var(--ink)}
.form-sub{font-size:13.5px;color:var(--ink-3);margin-bottom:36px;letter-spacing:.04em;font-weight:300}
form{display:flex;flex-direction:column;gap:20px}
.field{display:flex;flex-direction:column;gap:8px}
.field label{font-family:var(--mono);font-size:10.5px;color:var(--ink-3);
  letter-spacing:.22em;text-transform:uppercase;font-weight:500}
.field input{font-family:var(--sans);font-size:15px;padding:13px 16px;
  border:1px solid var(--line);border-radius:4px;background:#fff;color:var(--ink);
  transition:border .18s,box-shadow .18s;outline:none;font-weight:400}
.field input:hover{border-color:var(--line-2)}
.field input:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.field .hint{font-size:11.5px;color:var(--ink-4);letter-spacing:.02em;margin-top:2px}
.submit{margin-top:14px;padding:14px 20px;background:var(--ink);color:#fff;border:none;
  border-radius:4px;font-family:var(--serif);font-size:15px;font-weight:500;letter-spacing:.28em;
  transition:background .18s,transform .08s}
.submit:hover{background:var(--accent)}
.submit:active{transform:translateY(1px)}
.submit:disabled{opacity:.5;cursor:not-allowed;transform:none}
.error{margin-bottom:18px;padding:12px 16px;border-left:3px solid var(--bad);
  background:rgba(154,63,41,.06);color:#5a2418;font-size:13px;
  border-radius:0 4px 4px 0;display:none;line-height:1.6}
.error.show{display:block}
.alt{margin-top:28px;text-align:center;font-size:13px;color:var(--ink-3);font-weight:300}
.alt a{color:var(--accent);font-weight:500}
.alt a:hover{color:var(--accent-h);text-decoration:underline}
.form-foot{margin-top:48px;padding-top:24px;border-top:1px solid var(--line);
  text-align:center;font-family:var(--mono);font-size:10.5px;color:var(--ink-4);
  letter-spacing:.18em;text-transform:uppercase}
"""


def _auth_page_shell(*, kind: str) -> str:
    """渲染登录 / 注册页 — 出版物风两栏布局."""
    first_setup = user_store.count_users() == 0
    is_login = (kind == "login")

    if not is_login and not first_setup:
        # 已经有用户了 — 公开注册关闭,直接 302 不会走到这里
        return ""  # caller 应该重定向

    # 左侧根据是否首次安装变文案
    if first_setup and not is_login:
        eyebrow = "首次安装 · INITIAL SETUP"
        headline = '建立你的<em>第一位管理员</em>'
        lede = ("天枢·裁决 是一套面向软件质量的 AI 裁决工具。"
                "现在为这个实例创建第一位管理员账号 — 之后由你掌控用户、凭据与报告。")
    else:
        eyebrow = "AI VERDICT MANUAL · 0.1"
        headline = '回到你的<em>裁决工作台</em>'
        lede = ("用户名 + 密码进入。"
                "所有报告、审计与凭据都已经为你保留。")

    pillars = (
        '<div class="pillar"><div class="pillar-no">章 一</div>需求评审</div>'
        '<div class="pillar"><div class="pillar-no">章 二</div>用例设计</div>'
        '<div class="pillar"><div class="pillar-no">章 三</div>接口测试</div>'
        '<div class="pillar"><div class="pillar-no">章 四</div>UI 一致性</div>'
    )

    if is_login:
        # 首次安装时,登录页显示提示 + 注册入口
        notice = (
            '<div class="notice">系统里还没有任何用户。请先 '
            '<strong><a href="/register" style="color:var(--accent)">建立第一位管理员</a></strong> '
            '再回来登录。</div>'
            if first_setup else ""
        )
        form_title = "登 录"
        form_sub = "凭用户名与密码进入工作台"
        submit_label = "进 入"
        extra_field = ""
        pwd_hint = ""
        pwd_autocomplete = "current-password"
        alt_html = ""  # 没有"去注册"链接 — 公开注册已关
        form_kind = "login"
    else:
        notice = (
            '<div class="notice"><strong>初始化</strong> · 这位管理员将拥有最高权限:'
            '配置 Claude 凭据、查看所有用户的报告、新建/重置其他账号。</div>'
            if first_setup else ""
        )
        form_title = "建立管理员"
        form_sub = "为本实例的第一位用户"
        submit_label = "创 建"
        extra_field = (
            '<div class="field"><label for="display_name">显示名 (可选)</label>'
            '<input id="display_name" name="display_name" type="text" '
            'autocomplete="nickname" placeholder="留空则同用户名"></div>'
        )
        pwd_hint = '<div class="hint">至少 6 位。bcrypt 哈希存储,不上传外部服务。</div>'
        pwd_autocomplete = "new-password"
        alt_html = ""  # 不引导回登录,先建管理员
        form_kind = "register"

    return f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{'登录' if is_login else '建立管理员'} — 天枢·裁决</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@300;400;500;600;700&family=Noto+Sans+SC:wght@300;400;500;600&family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>{_AUTH_PAGE_CSS}</style>
</head><body>
<div class="auth-shell">
  <!-- 左侧:品牌叙事区 -->
  <aside class="brand-side">
    <div class="brand-mark">
      <svg viewBox="0 0 24 24" fill="currentColor" width="32" height="32" aria-hidden="true">
        <path d="M12 1.5L13.4 10.6L22.5 12L13.4 13.4L12 22.5L10.6 13.4L1.5 12L10.6 10.6Z"/>
      </svg>
      <span class="brand-name">天枢 <span class="brand-sep">·</span> 裁决</span>
    </div>
    <div class="brand-body">
      <div class="eyebrow">{eyebrow}</div>
      <h1 class="brand-headline">{headline}</h1>
      <p class="brand-lede">{lede}</p>
      <div class="brand-pillars">{pillars}</div>
    </div>
    <div class="brand-foot">
      <span>EDITION 0.1</span>
      <span>八章测评 · 一份裁决</span>
    </div>
  </aside>

  <!-- 右侧:表单区 -->
  <main class="form-side">
    <div class="form-card">
      {notice}
      <h2 class="form-title">{form_title}</h2>
      <div class="form-sub">{form_sub}</div>
      <div class="error" id="err"></div>
      <form id="auth-form" autocomplete="on">
        <div class="field">
          <label for="username">用户名</label>
          <input id="username" name="username" type="text" required autocomplete="username"
                 placeholder="如 zhangsan" autofocus>
        </div>
        {extra_field}
        <div class="field">
          <label for="password">密码</label>
          <input id="password" name="password" type="password" required
                 autocomplete="{pwd_autocomplete}" placeholder="至少 6 位">
          {pwd_hint}
        </div>
        <button class="submit" id="submit-btn" type="submit">{submit_label}</button>
      </form>
      {f'<div class="alt">{alt_html}</div>' if alt_html else ''}
      <div class="form-foot">SECURE · BCRYPT · LOCAL ONLY</div>
    </div>
  </main>
</div>
<script>
const FORM_KIND = "{form_kind}";
const errBox = document.getElementById('err');
function showErr(msg){{ errBox.textContent = msg; errBox.classList.add('show'); }}
function clearErr(){{ errBox.classList.remove('show'); errBox.textContent = ''; }}
async function doSubmit(e){{
  e.preventDefault(); clearErr();
  const btn = document.getElementById('submit-btn');
  btn.disabled = true; btn.textContent = '处 理 中...';
  const body = {{
    username: document.getElementById('username').value.trim(),
    password: document.getElementById('password').value,
  }};
  const nm = document.getElementById('display_name');
  if (nm) body.display_name = nm.value.trim();
  try {{
    const path = FORM_KIND === 'login' ? '/api/auth/login' : '/api/auth/register';
    const r = await fetch(path, {{
      method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify(body),
    }});
    if (!r.ok){{
      const d = await r.json().catch(()=>({{}}));
      showErr(d.detail || (FORM_KIND === 'login' ? '登录失败' : '创建失败'));
      btn.disabled = false; btn.textContent = '{submit_label}';
      return;
    }}
    const params = new URLSearchParams(location.search);
    const next = params.get('next') || '/tools';
    location.href = next;
  }} catch(err){{
    showErr('网络错误:' + err.message);
    btn.disabled = false; btn.textContent = '{submit_label}';
  }}
}}
document.getElementById('auth-form').addEventListener('submit', doSubmit);
</script>
</body></html>"""


@app.get("/login", response_class=HTMLResponse)
async def login_page() -> str:
    return _auth_page_shell(kind="login")


@app.get("/register")
async def register_page():
    """注册页 — 仅在系统还没用户时存在。否则 302 到 /login。"""
    if user_store.count_users() > 0:
        return RedirectResponse("/login?msg=registration_disabled", status_code=302)
    return HTMLResponse(_auth_page_shell(kind="register"))


@app.get("/tools", response_class=HTMLResponse)
async def tools_index_page() -> str:
    # 注入共享 overlay:右上角 admin 徽标 + 登出 + admin-only 元素显示逻辑
    return _inject_shared_overlays(TOOLS_INDEX_HTML)


@app.get("/tools/{tool_id}", response_class=HTMLResponse)
async def tool_detail_page(tool_id: str) -> str:
    if not any(t["id"] == tool_id for t in TOOL_CATALOG):
        raise HTTPException(404, f"unknown tool: {tool_id}")
    return _inject_shared_overlays(TOOL_DETAIL_HTML)


# =====================================================================
# Settings — 本地 Claude 接入状态、模型与 effort、工具环境需求
# =====================================================================

import shutil as _shutil
import subprocess as _sp
from datetime import datetime, timezone


def _read_json_safe(p: Path) -> dict[str, Any]:
    try:
        return _json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


# Single source of truth for Claude model registry. 档位化设计:每项的 model
# 字段用档位别名(opus/sonnet/haiku),由 Claude CLI 自动解析到账号最新版本
# (4.8/4.9…),无需随 Anthropic 发版手动改版本号。真实版本在探测后读回展示。
# Each entry:
#   key:                 stable identifier used in API requests / localStorage
#   model:               档位别名 (opus/sonnet/haiku) — 自动解析最新版本
#   label:               UI display label (不含版本号,版本由探测动态补)
#   version_badge:       small badge text (1M / Legacy / null)
#   tag:                 short Chinese capability descriptor
#   default:             True for the model with the ✓ in desktop
#   legacy:              True → shown grey at bottom of list
#   betas:               beta flags to pass via ClaudeAgentOptions
#   supports_effort / supports_thinking:  capability flags
#   supported_efforts / supported_thinking:  per-model allowed lists
CLAUDE_MODELS: list[dict[str, Any]] = [
    {
        "key": "opus",
        "model": "opus",  # 档位别名 → Claude CLI 自动解析账号最新 Opus(4.8/4.9…)
        "label": "Opus",
        "version_badge": None,
        "tag": "最强推理 · 慢",
        "default": True,
        "legacy": False,
        "betas": [],
        "supports_effort": True,
        "supports_thinking": True,
        "supported_efforts": ["low", "medium", "high", "xhigh", "max"],
        "supported_thinking": ["disabled", "adaptive", "enabled"],
    },
    {
        "key": "opus-1m",
        "model": "opus",
        "label": "Opus",
        "version_badge": "1M",
        "tag": "100 万上下文 · 适合超长输入",
        "default": False,
        "legacy": False,
        "betas": ["context-1m-2025-08-07"],
        "supports_effort": True,
        "supports_thinking": True,
        "supported_efforts": ["low", "medium", "high", "xhigh", "max"],
        "supported_thinking": ["disabled", "adaptive", "enabled"],
    },
    {
        "key": "sonnet",
        "model": "sonnet",
        "label": "Sonnet",
        "version_badge": None,
        "tag": "平衡 · 默认推荐",
        "default": False,
        "legacy": False,
        "betas": [],
        "supports_effort": True,
        "supports_thinking": True,
        "supported_efforts": ["low", "medium", "high", "xhigh", "max"],
        "supported_thinking": ["disabled", "adaptive", "enabled"],
    },
    {
        "key": "haiku",
        "model": "haiku",
        "label": "Haiku",
        "version_badge": None,
        "tag": "最快 · 简单任务",
        "default": False,
        "legacy": False,
        "betas": [],
        "supports_effort": False,
        "supports_thinking": False,
        "supported_efforts": [],
        "supported_thinking": [],
    },
]


def _model_by_key(key: str) -> dict[str, Any] | None:
    hit = next((m for m in CLAUDE_MODELS if m["key"] == key), None)
    if hit:
        return hit
    # 兼容旧的本地保存键(档位化前写死版本的 key,如 opus-4-7 / sonnet-4-6 / haiku-4-5):
    # 按"是否带 1M"+ 档位词收敛到新键,避免老用户下拉选项失效。
    k = (key or "").lower()
    if "opus" in k:
        return _model_by_key("opus-1m" if "1m" in k else "opus")
    if "haiku" in k:
        return next((m for m in CLAUDE_MODELS if m["key"] == "haiku"), None)
    if "sonnet" in k:
        return next((m for m in CLAUDE_MODELS if m["key"] == "sonnet"), None)
    return None


# ---- 实时模型列表:直接读 Anthropic /v1/models(版本/精度/上下文,全部实时,不写死)----
_LIVE_MODELS_CACHE: dict[str, Any] = {"at": 0.0, "models": None}
_ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
_LIVE_MODELS_TTL = 60.0  # 秒;/v1/models 是轻量元数据接口,不耗推理 token


def _tier_word(model_id: str) -> str | None:
    ml = (model_id or "").lower()
    if "opus" in ml:
        return "opus"
    if "haiku" in ml:
        return "haiku"
    if "sonnet" in ml:
        return "sonnet"
    return None


async def _fetch_live_models() -> list[dict[str, Any]] | None:
    """实时从 Anthropic /v1/models 拉账号可用模型(版本 / 精度能力 / 上下文)。

    返回与 CLAUDE_MODELS 同构的列表(前端无需改);失败返回 None,调用方回退静态表。
    结果缓存 _LIVE_MODELS_TTL 秒。
    """
    now = _time.time()
    cache = _LIVE_MODELS_CACHE
    if cache.get("models") is not None and (now - cache.get("at", 0)) < _LIVE_MODELS_TTL:
        return cache["models"]
    try:
        await ensure_fresh_oauth_token()
    except Exception:
        pass
    headers = {"anthropic-version": "2023-06-01"}
    try:
        from packages.core.auth_config import get_api_key, get_oauth_access_token
    except Exception:
        return None
    token = None
    try:
        token = get_oauth_access_token()
    except Exception:
        token = None
    if token:
        headers["authorization"] = f"Bearer {token}"
        headers["anthropic-beta"] = "oauth-2025-04-20"
    else:
        try:
            key = get_api_key()
        except Exception:
            key = None
        if not key:
            return None
        headers["x-api-key"] = key
    import httpx
    try:
        async with httpx.AsyncClient(timeout=15.0) as cli:
            resp = await cli.get(_ANTHROPIC_MODELS_URL, headers=headers, params={"limit": 100})
        if resp.status_code != 200:
            return None
        data = resp.json().get("data", []) or []
    except Exception:
        return None
    effort_order = ["low", "medium", "high", "xhigh", "max"]
    out: list[dict[str, Any]] = []
    for m in data:
        mid = m.get("id")
        if not mid:
            continue
        cap = m.get("capabilities", {}) or {}
        eff = cap.get("effort", {}) or {}
        efforts = [e for e in effort_order if isinstance(eff.get(e), dict) and eff[e].get("supported")]
        think = cap.get("extended_thinking") or cap.get("thinking") or {}
        supports_think = bool(think.get("supported")) if isinstance(think, dict) else False
        ctx = m.get("max_input_tokens") or 0
        tag = {"opus": "最强推理 · 慢", "sonnet": "平衡 · 默认推荐",
               "haiku": "最快 · 简单任务"}.get(_tier_word(mid) or "", "")
        out.append({
            "key": mid,
            "model": mid,  # 全 ID → 选哪个版本就跑哪个版本
            "label": m.get("display_name") or mid,
            "version_badge": "1M" if ctx and ctx >= 1_000_000 else None,
            "tag": tag,
            "default": False,
            "legacy": False,
            "betas": [],
            "supports_effort": bool(efforts),
            "supports_thinking": supports_think,
            "supported_efforts": efforts,
            "supported_thinking": (["disabled", "adaptive", "enabled"] if supports_think else []),
            "context": ctx,
        })
    if not out:
        return None
    # 排序:同 tier 内版本号新→旧(id 倒序),再把 opus>sonnet>haiku 排前
    out.sort(key=lambda x: x["model"], reverse=True)
    rank = {"opus": 0, "sonnet": 1, "haiku": 2}
    out.sort(key=lambda x: rank.get(_tier_word(x["model"]) or "", 9))
    # 默认 = 排序后第一个 opus(即最新 opus)
    for x in out:
        if _tier_word(x["model"]) == "opus":
            x["default"] = True
            break
    else:
        out[0]["default"] = True
    cache["models"] = out
    cache["at"] = now
    return out


# ----- 模型可用性探测 -----
# 用最小 token 调一次,把不可用的模型(账号未开通 / 区域限制 / 模型已下线)
# 在前端 disable 掉。降级到运行报告里的"失败"已经在 _run_tool_async 里有兜底,
# 这是个预防——让用户看一眼就知道哪些模型能用。
_MODEL_PROBE_CACHE: dict[str, dict[str, Any]] = {}
# 单次会话内的"运行时黑名单":只要某个 model 在真实运行里报 SDK 失败,
# 立即标 unavailable + reason,避免用户继续踩同一个坑。下次 server 重启清空。
_MODEL_RUNTIME_BLACKLIST: dict[str, str] = {}


def _record_model_failure(model_key: str, reason: str) -> None:
    """运行失败时调用 — 把模型加入会话黑名单。

    只记录"看起来是模型本身不行"的情况(claude-haiku 命名错误、未开通、找不到等),
    不记录通用网络错误,否则会把好模型也错误标记。
    """
    low = (reason or "").lower()
    looks_like_model_issue = any(
        kw in low for kw in (
            "not found", "model_not_found", "404", "permission",
            "unsupported", "not available", "无权限", "未开通",
            "not enabled", "model not enabled",
        )
    )
    if looks_like_model_issue and model_key:
        _MODEL_RUNTIME_BLACKLIST[model_key] = reason[:240]


def _looks_like_model_specific_failure(msg: str) -> bool:
    """判断 SDK 错误信息是不是"这个模型本身不可用"。

    - 是: "model_not_found" / "not enabled" / "permission" / "404"
    - 否: 网络抖动 / 认证失败 / 通用 "returned an error result" (CLI 本身就坏了)
    通用失败时只记 probe ok=False,但 NOT 影响 dropdown 的 available 标记 —
    避免把"CLI 整个挂了"误判成"这一个模型不可用"。
    """
    low = (msg or "").lower()
    return any(kw in low for kw in (
        "model_not_found", "model not found", "not enabled",
        "404", "permission denied", "no access", "not available",
        "未开通", "无权限", "model is not supported",
    ))


async def _probe_model(model_key: str) -> dict[str, Any]:
    """对一个模型做最小成本探测:发一个小 JSON 请求,看 SDK 是否能完成一轮。

    结果缓存 10 分钟。返回 {ok, model, error, model_specific, checked_at}.
    model_specific=True 才会被 /api/claude/info 标 available=False;
    通用失败(网络 / 认证 / CLI 损坏)不归咎于模型本身。
    """
    cached = _MODEL_PROBE_CACHE.get(model_key)
    now = _time.time()
    if cached and (now - cached.get("checked_at", 0)) < 600:
        return cached
    entry = _model_by_key(model_key)
    if not entry and _tier_word(model_key):
        # 实时列表里的全 ID 键(如 claude-opus-4-7)直接当模型 ID 探测
        entry = {"model": model_key}
    if not entry:
        result = {"ok": False, "model": model_key, "error": "unknown model key",
                  "model_specific": True, "checked_at": now}
        _MODEL_PROBE_CACHE[model_key] = result
        return result
    # 探测前先确保 OAuth token 没过期 — 否则探测会因 401 失败,
    # 把"接入没刷新"误判成"模型不可用"。
    try:
        await ensure_fresh_oauth_token()
    except Exception:
        pass
    try:
        from packages.core.llm.client import LlmClient
        client = LlmClient(model_override=entry["model"])
        # 探测请求要"够丰满":LlmClient 默认会注入"必须返回 JSON / 严格运行规则"等长系统约束,
        # 太短的请求(单字 "ok")会让 CLI 子进程返回 is_error=True 而误判。
        # 给一个明确要求 JSON 输出的小问题,任何可用模型都能秒回。
        resp = await client.complete(
            system="你是一个连接测试小助手。请严格按 JSON 输出。",
            messages=[{
                "role": "user",
                "content": '只返回 JSON 对象 {"ok": true}，不要任何其他文字。',
            }],
            allow_degrade=False,
            max_tokens=64,
        )
        sample = (resp.text or "").strip()
        result = {
            "ok": True,
            "model": entry["model"],
            # 探测时别名解析出的真实版本号(如 claude-opus-4-8-…)— 用于 UI 动态显示版本
            "resolved_model": getattr(resp, "model_id", None) or entry["model"],
            "model_key": model_key,
            "checked_at": now,
            "sample_text": sample[:120],
            "model_specific": False,
        }
    except Exception as exc:
        msg = f"{type(exc).__name__}: {str(exc)[:240]}"
        model_specific = _looks_like_model_specific_failure(msg)
        result = {
            "ok": False,
            "model": entry["model"],
            "model_key": model_key,
            "checked_at": now,
            "error": msg,
            "model_specific": model_specific,
        }
        if model_specific:
            _record_model_failure(model_key, msg)
    _MODEL_PROBE_CACHE[model_key] = result
    return result


@app.post("/api/claude/probe_model")
async def api_claude_probe_model(body: dict[str, Any]) -> dict[str, Any]:
    """手动触发对一个模型的可用性探测。前端在用户选实验模型时调用。"""
    key = (body or {}).get("model_key")
    if not key:
        raise HTTPException(400, "missing model_key")
    return await _probe_model(str(key))


@app.get("/api/claude/info")
async def api_claude_info() -> dict[str, Any]:
    """探测本地 Claude Code 的接入状态、版本、登录账户、默认 effort/model。

    完全只读：不写任何 ~/.claude 文件。
    用于工具详情页"模型 & 精度"区域和 /settings 页。
    """
    home = Path.home()
    candidates = [
        os.environ.get("CLAUDE_BIN"),
        _shutil.which("claude"),
        str(home / ".local" / "bin" / "claude"),
        str(home / "bin" / "claude"),
        str(home / ".npm-global" / "bin" / "claude"),
        "/opt/homebrew/bin/claude",
        "/usr/local/bin/claude",
    ]
    def _safe_exists(p: str) -> bool:
        try:
            return Path(p).exists()
        except (PermissionError, OSError):
            return False
    bin_path = next(
        (c for c in candidates if c and _safe_exists(c)),
        None,
    )
    bin_exists = bool(bin_path)

    version = None
    if bin_exists:
        try:
            out = _sp.run(
                [bin_path, "--version"], capture_output=True, text=True, timeout=5
            )
            version = (out.stdout or out.stderr).strip()
        except Exception as exc:
            version = f"(error: {exc})"

    # 默认精度由 toolkit 自己管理 — 不再读 ~/.claude/settings.json
    # 这个值只是个"默认 dropdown 选项"，跟登录态、订阅、账号都无关。
    # 用户每次跑工具时仍可单独选其他精度。
    effort = "medium"

    state_path = home / ".claude.json"
    state = _read_json_safe(state_path) if state_path.exists() else {}
    oauth = state.get("oauthAccount") or {}

    # 优先用 toolkit 自己的接入信息(API Key 或 web OAuth);
    # ~/.claude.json 的 oauthAccount 是本机 claude CLI 残留,只是兜底
    toolkit_mode = "unset"
    toolkit_api_masked = None
    toolkit_oauth_acc: dict[str, Any] = {}
    try:
        from packages.core.auth_config import (
            get_api_key, get_auth_mode, get_oauth_account, get_oauth_access_token,
            mask_api_key,
        )
        toolkit_mode = get_auth_mode()
        if toolkit_mode == "api_key":
            k = get_api_key()
            if k:
                toolkit_api_masked = mask_api_key(k)
        elif toolkit_mode == "oauth" and get_oauth_access_token():
            toolkit_oauth_acc = get_oauth_account() or {}
    except Exception:
        pass

    if toolkit_mode == "api_key" and toolkit_api_masked:
        account = {
            "mode_label": "API Key 模式",
            "display_name": toolkit_api_masked,
            "billing_type": "api",
        }
        auth_state = "toolkit_api_key"
    elif toolkit_mode == "oauth" and toolkit_oauth_acc:
        account = {
            "mode_label": "OAuth 模式",
            "email": toolkit_oauth_acc.get("email"),
            "display_name": toolkit_oauth_acc.get("display_name") or toolkit_oauth_acc.get("email"),
            "billing_type": toolkit_oauth_acc.get("billing_type") or "subscription",
            "organization_name": toolkit_oauth_acc.get("organization_name"),
        }
        auth_state = "toolkit_oauth"
    elif oauth:
        # 兜底:本机有残留的 claude login,显示但标"本机 CLI 凭据"
        account = {
            "mode_label": "本机 CLI 凭据",
            "email": oauth.get("emailAddress"),
            "display_name": oauth.get("displayName"),
            "billing_type": oauth.get("billingType"),
        }
        auth_state = "external_claude_cli"
    else:
        account = None
        auth_state = "未接入" if toolkit_mode == "unset" else "未授权"

    install_method = state.get("installMethod")
    first_start = state.get("firstStartTime")
    first_token = state.get("claudeCodeFirstTokenDate")

    # available_models 是 UI 下拉的源头。
    # 优先用 /v1/models 实时列表(账号当前全部可用模型 + 版本 + 精度能力);
    # 拉取失败(无 token / 网络)回退到内置档位静态表。
    # 只在"模型本身明确不可用"时才 disable(model_specific 失败,或运行时黑名单);
    # 通用 SDK 失败(CLI 整个挂、网络抖动)只在 dropdown 显示警告,不禁用。
    source_models = await _fetch_live_models() or CLAUDE_MODELS
    models_live = source_models is not CLAUDE_MODELS
    models_with_avail: list[dict[str, Any]] = []
    for m in source_models:
        key = m["key"]
        probe = _MODEL_PROBE_CACHE.get(key)
        blacklisted = _MODEL_RUNTIME_BLACKLIST.get(key)
        avail = True
        unavail_reason: str | None = None
        last_probe_status: str | None = None
        if blacklisted:
            avail = False
            unavail_reason = blacklisted
        elif probe and probe.get("ok") is False and probe.get("model_specific"):
            avail = False
            unavail_reason = probe.get("error")
        # 通用探测失败也回传给前端做提示,但不 disable
        if probe and probe.get("ok") is False and not probe.get("model_specific"):
            last_probe_status = (probe.get("error") or "")[:240]
        elif probe and probe.get("ok"):
            last_probe_status = "ok"
        models_with_avail.append({
            **m,
            "available": avail,
            "unavailable_reason": unavail_reason,
            "last_probe_status": last_probe_status,
            "last_probed_at": (probe or {}).get("checked_at"),
            # 探测读回的真实版本号(无探测时为 None,UI 退回只显示档位名)
            "resolved_model": (probe or {}).get("resolved_model"),
        })

    return {
        "bin_path": bin_path if bin_exists else None,
        "bin_found": bin_exists,
        "version": version,
        "install_method": install_method,
        "first_start_at": first_start,
        "first_token_at": first_token,
        "auth_state": auth_state,
        "account": account,
        "settings_effort_level": effort,
        "available_models": models_with_avail,
        "models_live": models_live,  # True=来自 /v1/models 实时列表;False=内置静态回退
        "available_efforts": [
            {"key": "low", "label": "Low", "tag": "最快"},
            {"key": "medium", "label": "Medium", "tag": "默认"},
            {"key": "high", "label": "High", "tag": "细致"},
            {"key": "xhigh", "label": "X-High", "tag": "深度推理"},
            {"key": "max", "label": "Max", "tag": "最强 · 最慢"},
        ],
        "available_thinking": [
            {"key": "disabled", "label": "关闭"},
            {"key": "adaptive", "label": "自适应"},
            {"key": "enabled", "label": "开启（自定义预算）"},
        ],
    }


# Per-tool environment requirements: which optional packages each tool benefits from.
# Plus a global pool of file-parsing deps shared by all tools (file upload).
_FILE_PARSING_REQS = [
    {"pkg": "pypdf", "required": False, "purpose": "上传 PDF 自动提取文本"},
    {"pkg": "python-docx", "required": False, "purpose": "上传 DOCX 自动提取文本"},
    {"pkg": "openpyxl", "required": False, "purpose": "上传 XLSX 自动提取文本"},
    {"pkg": "chardet", "required": False, "purpose": "非 UTF-8 文本编码自动识别"},
]
TOOL_ENV_REQUIREMENTS: dict[str, list[dict[str, Any]]] = {
    "step1": [
        {"pkg": "claude-agent-sdk", "required": True, "purpose": "本地 Claude 接入"},
        *_FILE_PARSING_REQS,
    ],
    "step2": [
        {"pkg": "claude-agent-sdk", "required": True, "purpose": "本地 Claude 接入"},
        *_FILE_PARSING_REQS,
    ],
    "step4": [
        {"pkg": "claude-agent-sdk", "required": True, "purpose": "本地 Claude 接入"},
        {"pkg": "httpx", "required": False, "purpose": "live 模式实际请求接口"},
        *_FILE_PARSING_REQS,
    ],
    "step5": [
        {"pkg": "claude-agent-sdk", "required": True, "purpose": "本地 Claude 接入"},
        {"pkg": "Pillow", "required": False, "purpose": "截图差异比对"},
        *_FILE_PARSING_REQS,
    ],
    "step6": [
        {"pkg": "claude-agent-sdk", "required": True, "purpose": "本地 Claude 接入"},
        {"pkg": "playwright", "required": False, "purpose": "live 模式浏览器自动化"},
        *_FILE_PARSING_REQS,
    ],
}


# PyPI distribution name → Python import name (for packages where they differ)
_PKG_IMPORT_NAME: dict[str, str] = {
    "Pillow": "PIL",
    "pillow": "PIL",
    "claude-agent-sdk": "claude_agent_sdk",
    "python-docx": "docx",
    "PyYAML": "yaml",
    "pyyaml": "yaml",
    "beautifulsoup4": "bs4",
    "opencv-python": "cv2",
    "scikit-learn": "sklearn",
}


def _check_pkg(name: str) -> dict[str, Any]:
    """Check installed via importlib.metadata, with import fallback.

    Strategy:
      1) Try importlib.metadata.version() — pip's authoritative record
      2) If that fails, try import. If the module loads we accept it
         (version='unknown')
      3) Otherwise it really is missing
    """
    import importlib
    import importlib.metadata as md

    import_name = _PKG_IMPORT_NAME.get(name, name.replace("-", "_").lower())

    # 1) Pip-registered distribution
    metadata_version: str | None = None
    try:
        metadata_version = md.version(name)
    except md.PackageNotFoundError:
        metadata_version = None

    # 2) Verify the module actually loads
    importable = True
    try:
        importlib.import_module(import_name)
    except ImportError:
        importable = False

    if metadata_version and importable:
        result = {"installed": True, "version": metadata_version}
    elif importable and not metadata_version:
        # Module loadable but no dist-info — accept with placeholder version
        result = {"installed": True, "version": "unknown"}
    elif metadata_version and not importable:
        # Metadata exists but module won't load — partial / broken install
        result = {"installed": False, "version": metadata_version, "broken": True}
    else:
        result = {"installed": False, "version": None}

    if name == "playwright" and result["installed"]:
        result["browsers_installed"] = _playwright_browsers_present()
    return result


def _playwright_browsers_present() -> bool:
    """Check whether playwright's chromium browser is downloaded.

    Mirrors playwright's own resolution:
      - If PLAYWRIGHT_BROWSERS_PATH is set (and not "0"), look ONLY there.
        That's the runtime contract — playwright does not fall back to the
        default cache when the env override is set.
      - Otherwise check the platform defaults.

    Looks for an actual `chromium*` directory so a leftover empty parent
    doesn't make us claim browsers are present.
    """
    env_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if env_path and env_path != "0":
        candidates: list[Path] = [Path(env_path)]
    else:
        candidates = [
            Path.home() / "Library/Caches/ms-playwright",  # macOS default
            Path.home() / ".cache/ms-playwright",          # Linux
            Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright",  # Windows
        ]
    for d in candidates:
        if not d.exists():
            continue
        try:
            for child in d.iterdir():
                if child.is_dir() and child.name.startswith("chromium"):
                    return True
        except OSError:
            continue
    return False


def _check_claude_login() -> dict[str, Any]:
    """Detect whether Claude Code is "ready to call" given the active auth mode.

    认证状态完全由 toolkit 自己的 auth.json 决定:
    - oauth   : 看 toolkit 是否存有 OAuth access_token (web OAuth flow 拿到的)
    - api_key : 看 toolkit 是否存了 sk-ant-... API Key
    - unset   : 用户从未选过模式
    """
    try:
        from packages.core.auth_config import (
            get_api_key, get_auth_mode, get_oauth_access_token,
        )
        mode = get_auth_mode()
    except Exception:
        return {"logged_in": False, "needs_login": True, "mode": "unset"}

    if mode == "unset":
        return {"logged_in": False, "needs_login": True, "mode": "unset"}

    if mode == "api_key":
        has_key = bool(get_api_key())
        return {"logged_in": has_key, "needs_login": not has_key, "mode": "api_key"}

    # mode == "oauth" — toolkit 自己存的 access_token
    has_token = bool(get_oauth_access_token())
    return {"logged_in": has_token, "needs_login": not has_token, "mode": "oauth"}


def _check_claude_cli() -> dict[str, Any]:
    """Locate the user's Claude Code CLI binary.

    Search order (handles macOS Finder-launch PATH sanitization):
      1) $CLAUDE_BIN env override
      2) shutil.which("claude") — covers anything on inherited $PATH
      3) Common user-bin locations (~/.local/bin, ~/bin, brew, etc.)

    Required at runtime — every tool dispatches to it. Without it, the
    env-banner shows a manual install hint.
    """
    home = Path.home()
    candidates = [
        os.environ.get("CLAUDE_BIN"),
        _shutil.which("claude"),
        str(home / ".local" / "bin" / "claude"),
        str(home / "bin" / "claude"),
        str(home / ".npm-global" / "bin" / "claude"),
        "/opt/homebrew/bin/claude",
        "/usr/local/bin/claude",
    ]
    # 用 try/except 包住 .exists() — 容器里 symlink 可能指向当前用户无权访问的路径
    # (cp -a 保留了 symlink target,但目标 /root/... 普通用户读不到 → PermissionError)
    def _safe_exists(p: str) -> bool:
        try:
            return Path(p).exists()
        except (PermissionError, OSError):
            return False
    bin_path = next(
        (c for c in candidates if c and _safe_exists(c)),
        None,
    )
    if not bin_path:
        return {"installed": False, "version": None}
    try:
        out = _sp.run([bin_path, "--version"], capture_output=True, text=True, timeout=5)
        ver = (out.stdout or out.stderr or "").strip()
    except Exception:
        ver = "?"
    return {"installed": True, "version": ver, "path": bin_path}


@app.get("/api/settings/ready")
async def api_settings_ready() -> dict[str, Any]:
    """启动检查 — 检查 Python 依赖 + 系统级二进制（claude）+ 浏览器。"""
    missing_required: list[dict[str, Any]] = []
    optional_missing: list[dict[str, Any]] = []

    # System: Claude Code CLI — 每个工具都依赖，未打进 .app
    claude_stat = _check_claude_cli()
    if not claude_stat["installed"]:
        missing_required.append({
            "pkg": "claude_cli",
            "label": "Claude Code CLI",
            "required": True,
            "purpose": "本地 Claude Code CLI 二进制 — 没有它所有工具都跑不起来",
            "installed": False,
            "auto_installable": True,  # JS uses to show 一键安装 button
            "install_hint": "curl -fsSL https://claude.ai/install.sh | bash",
        })
    else:
        # Claude exists — check 用户是否在设置页选过连接方式 + 凭据是否就绪
        try:
            login_state = _check_claude_login()
            login_mode = login_state.get("mode")
            if login_state.get("needs_login"):
                if login_mode == "unset":
                    # 用户从没选过 — 这是默认初始状态
                    missing_required.append({
                        "pkg": "auth_mode",
                        "label": "认证模式（必选）",
                        "required": True,
                        "purpose": "首次使用必须主动选择连接方式 — 进设置页点选 OAuth（CLI 订阅）或 API Key，本机已有的凭据不会被自动复用",
                        "installed": False,
                        "auto_installable": False,  # 必须人工到设置页选
                        "install_hint": "设置 → 认证模式 → 任选一种连接方式",
                    })
                elif login_mode == "api_key":
                    missing_required.append({
                        "pkg": "claude_api_key",
                        "label": "Anthropic API Key",
                        "required": True,
                        "purpose": "已选择 API Key 模式但还没填 key — 请到设置页输入 sk-ant-... 或切到「本地」「OAuth」模式",
                        "installed": False,
                        "auto_installable": False,
                        "install_hint": "设置 → 连接 Claude → 粘贴 API Key",
                    })
                else:
                    missing_required.append({
                        "pkg": "claude_login",
                        "label": "Claude OAuth 授权",
                        "required": True,
                        "purpose": "已选 OAuth 模式但还没在本工具完成授权 — 到「设置 → 模型接入」点「OAuth 授权」浏览器走授权流程",
                        "installed": False,
                        "auto_installable": False,
                        "install_hint": "设置 → 模型接入 → OAuth 授权（浏览器内完成）",
                    })
        except Exception:
            pass  # don't break env-check on detection failure

    # Python packages — check what each tool needs
    seen = set()
    for tool in TOOL_CATALOG:
        for r in TOOL_ENV_REQUIREMENTS.get(tool["id"], []):
            key = r["pkg"]
            if key in seen:
                continue
            seen.add(key)
            stat = _check_pkg(key)
            if not stat["installed"]:
                entry = {**r, **stat}
                if r["required"]:
                    missing_required.append(entry)
                else:
                    optional_missing.append(entry)
            elif key == "playwright" and not stat.get("browsers_installed"):
                # Special case: playwright python is installed but browsers not
                optional_missing.append({
                    "pkg": "playwright_browsers", "required": False,
                    "purpose": "playwright 已装但 chromium 浏览器未下载",
                    "installed": False,
                })
    # System info — useful for the dialog
    return {
        "ready": len(missing_required) == 0,
        "missing_required": missing_required,
        "missing_optional": optional_missing,
    }


@app.get("/api/settings/tools")
async def api_settings_tools() -> dict[str, Any]:
    """每个工具的环境需求 + 当前安装状态。"""
    out = []
    for tool in TOOL_CATALOG:
        reqs = TOOL_ENV_REQUIREMENTS.get(tool["id"], [])
        checked = []
        ok = True
        for r in reqs:
            stat = _check_pkg(r["pkg"])
            checked.append({**r, **stat})
            if r["required"] and not stat["installed"]:
                ok = False
        out.append({
            "id": tool["id"],
            "name": tool["name"],
            "icon": tool["icon"],
            "ready": ok,
            "requirements": checked,
        })
    return {"tools": out}


@app.get("/api/settings/overrides")
async def api_settings_overrides() -> dict[str, Any]:
    """已激活的提示词覆盖列表。"""
    from packages.core.prompts import list_prompt_overrides
    return {"overrides": list_prompt_overrides()}


# ----- One-click install -----

import platform as _platform
import sys as _sys

# Whitelist of package names we'll allow installing — never accept arbitrary input.
_INSTALLABLE_PKGS = {
    "claude-agent-sdk", "httpx", "Pillow", "playwright",
    "pypdf", "python-docx", "openpyxl", "chardet",
}
_PLAYWRIGHT_BROWSER_INSTALL = "playwright_browsers"  # special token


@app.get("/api/settings/system")
async def api_settings_system() -> dict[str, Any]:
    """检测当前系统 + venv，给一键安装路径。"""
    venv = _sys.prefix if hasattr(_sys, "real_prefix") or _sys.base_prefix != _sys.prefix else None
    return {
        "os": _platform.system(),
        "os_release": _platform.release(),
        "machine": _platform.machine(),
        "python": _sys.version.split()[0],
        "python_executable": _sys.executable,
        "venv": venv,
        "pip_executable": str(Path(_sys.executable).parent / "pip"),
    }


# ─── 认证模式（OAuth / API Key 二选一） ──────────────────────────────────────
# OAuth 走 Claude 订阅；API Key 直接按量计费走 Anthropic API。

class AuthModeReq(BaseModel):
    mode: str   # "oauth" | "api_key"
    api_key: str | None = None   # 仅 mode=api_key 时用


@app.get("/api/settings/auth")
async def api_settings_auth() -> dict[str, Any]:
    """返回当前认证模式 + 两种模式的就绪状态。

    UI 渲染 2 张模式卡时使用：
    - oauth: toolkit 自身是否有 OAuth access_token（web OAuth flow 拿到的）+ 账号信息
    - api_key: 是否已存 API key（带 mask 显示）

    注意：本端点完全不再读 ~/.claude/ 的任何文件 — OAuth 凭据由 toolkit
    自己通过 web OAuth flow 拿到并存进 auth.json。
    """
    try:
        from packages.core.auth_config import (
            get_api_key, get_auth_mode, get_oauth_access_token,
            get_oauth_account, mask_api_key,
        )
    except Exception:
        return {"current_mode": "oauth", "modes": {}, "error": "auth_config 模块加载失败"}

    cli_stat = _check_claude_cli()
    current_mode = get_auth_mode()  # "unset" / "oauth" / "api_key"

    # toolkit 自己存的 OAuth token
    oauth_token_present = bool(get_oauth_access_token())
    oauth_account = get_oauth_account()

    api_key = get_api_key()
    # OAuth ready: mode=oauth + toolkit 有 token + CLI 装好
    oauth_ready  = (current_mode == "oauth") and cli_stat["installed"] and oauth_token_present
    apikey_ready = (current_mode == "api_key") and cli_stat["installed"] and bool(api_key)

    return {
        "current_mode": current_mode,
        "is_unset": current_mode == "unset",
        "modes": {
            "oauth": {
                "label": "OAuth 授权",
                "tag": "推荐 · 含订阅免费",
                "selectable": True,
                "ready": oauth_ready,
                "token_present": oauth_token_present,
                "account": oauth_account,  # ← 授权后展示用
                "needs": (
                    [] if cli_stat["installed"] else ["claude_cli"]
                ) + (
                    [] if oauth_token_present else ["oauth_authorize"]
                ) + (
                    [] if current_mode == "oauth" else ["主动选择此模式"]
                ),
                "summary": "在浏览器内完成 Claude OAuth 授权 — 凭据存在本工具，不依赖本机 claude login。",
            },
            "api_key": {
                "label": "API Key",
                "tag": "按量计费 · 跳过登录",
                "selectable": True,
                "ready": apikey_ready,
                "has_api_key": bool(api_key),
                "api_key_masked": mask_api_key(api_key),
                "needs": (
                    [] if cli_stat["installed"] else ["claude_cli"]
                ) + (
                    [] if api_key else ["api_key"]
                ) + (
                    [] if current_mode == "api_key" else ["主动选择此模式"]
                ),
                "summary": "粘贴 sk-ant-... API Key，跳过 OAuth 登录。按 Anthropic 价目按量计费。",
            },
        },
    }


@app.put("/api/settings/auth")
async def api_settings_auth_put(req: AuthModeReq, request: Request) -> dict[str, Any]:
    require_admin(request)
    """切换认证模式 + 可选保存 API key。

    Body 示例：
        {"mode": "api_key", "api_key": "sk-ant-api03-..."}
        {"mode": "oauth"}
    """
    mode = req.mode
    if mode not in ("oauth", "api_key"):
        raise HTTPException(400, f"unsupported mode: {mode}")
    if mode == "api_key" and (not req.api_key or not req.api_key.strip()):
        try:
            from packages.core.auth_config import get_api_key
            if not get_api_key():
                raise HTTPException(400, "切换到 API Key 模式需要至少提供一次 api_key")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(400, "切换到 API Key 模式需要 api_key")
    try:
        from packages.core.auth_config import set_auth_mode
        set_auth_mode(mode, req.api_key)
    except Exception as e:
        raise HTTPException(500, f"保存失败：{e}")
    # 立即回查一遍状态返回 — 前端可直接 refresh
    return await api_settings_auth()


@app.get("/api/claude/account")
async def api_claude_account() -> dict[str, Any]:
    """OAuth 登录成功后展示用 — 从 ~/.claude.json#oauthAccount 读详细账号信息。

    UI 在 OAuth 卡 ready 时展示：邮箱 / 订阅类型 / 生效日期 / 组织（如有）。
    没登录返回 logged_in=False，前端不渲染账号块。
    """
    home = Path.home()
    has_creds = False
    account: dict[str, Any] = {}

    state_file = home / ".claude.json"
    if state_file.exists():
        try:
            state = _read_json_safe(state_file) or {}
            oa = state.get("oauthAccount") or {}
            if oa:
                has_creds = True
                account = {
                    "email": oa.get("emailAddress"),
                    "display_name": oa.get("displayName"),
                    "organization_name": oa.get("organizationName"),
                    "billing_type": oa.get("billingType"),  # subscription_pro / subscription_max / api / 等
                    "has_extra_usage": bool(oa.get("hasExtraUsageEnabled")),
                    "account_created_at": oa.get("accountCreatedAt"),
                    "subscription_created_at": oa.get("subscriptionCreatedAt"),
                    "account_uuid": (oa.get("accountUuid") or "")[:8] + "…",
                }
        except Exception:
            pass
    # 也看 ~/.claude/account.json（独立文件版本）
    acct_file = home / ".claude" / "account.json"
    if not has_creds and acct_file.exists():
        try:
            acct = _read_json_safe(acct_file) or {}
            if acct:
                has_creds = True
                account = {
                    "email": acct.get("email") or acct.get("emailAddress"),
                    "display_name": acct.get("displayName") or acct.get("name"),
                    "billing_type": acct.get("billingType") or acct.get("plan"),
                }
        except Exception:
            pass

    return {
        "logged_in": has_creds,
        "account": account,
        # 计费类型友好名（UI 显示用）
        "billing_label": _billing_label(account.get("billing_type") if account else None),
    }


def _billing_label(billing_type: str | None) -> str:
    if not billing_type:
        return ""
    mapping = {
        "subscription_max":          "Claude Max 订阅",
        "subscription_pro":          "Claude Pro 订阅",
        "subscription_team":         "Claude Team",
        "subscription_enterprise":   "Claude Enterprise",
        "google_play_subscription":  "Claude Pro 订阅 (Google Play)",
        "apple_app_store_subscription": "Claude Pro 订阅 (App Store)",
        "api":                       "API 按量计费",
        "free":                      "免费版",
    }
    return mapping.get(billing_type, billing_type)


@app.delete("/api/settings/auth/api-key")
async def api_settings_auth_clear_key(request: Request) -> dict[str, Any]:
    """清掉已存的 API Key（不切模式）。"""
    require_admin(request)
    try:
        from packages.core.auth_config import clear_api_key
        clear_api_key()
    except Exception as e:
        raise HTTPException(500, f"清除失败：{e}")
    return await api_settings_auth()


class DisconnectReq(BaseModel):
    """指定要断开的认证目标。

    - mode='oauth'  : 清掉 toolkit 自己存的 OAuth token（不动 ~/.claude/）
    - mode='api_key': 清空已保存的 API key + 切回 unset
    purge 现在没意义（toolkit 凭据天然独立于本机 claude），保留以兼容旧调用。
    """
    mode: str
    purge: bool = False  # 已废弃；忽略其值


@app.post("/api/settings/auth/disconnect")
async def api_settings_auth_disconnect(req: DisconnectReq, request: Request) -> dict[str, Any]:
    require_admin(request)
    """断开当前认证 — 只清 toolkit 自身存的凭据，不动本机 claude。

    - api_key 模式: 清掉本工具保存的 sk-ant-... + 切回 unset
    - oauth 模式: 清掉本工具存的 access_token/refresh_token/账号 + 切回 unset
    """
    mode = (req.mode or "").strip()
    if mode not in ("oauth", "api_key"):
        raise HTTPException(400, f"unsupported disconnect target: {mode}")

    deleted: list[str] = []
    errors: list[str] = []

    if mode == "api_key":
        try:
            from packages.core.auth_config import clear_api_key, set_auth_mode
            clear_api_key()
            set_auth_mode("unset")
            deleted.append("toolkit api_key")
        except Exception as e:
            errors.append(f"clear api_key: {e}")
            raise HTTPException(500, f"清除 API Key 失败：{e}")
    else:  # oauth
        try:
            from packages.core.auth_config import clear_oauth_tokens, set_auth_mode
            clear_oauth_tokens()
            set_auth_mode("unset")
            deleted.append("toolkit oauth tokens")
        except Exception as e:
            errors.append(f"clear oauth: {e}")
            raise HTTPException(500, f"清除 OAuth 失败：{e}")

    out = await api_settings_auth()
    out["disconnect_result"] = {"deleted": deleted, "errors": errors}
    return out


# ── Web OAuth flow ─────────────────────────────────────────────────────────
# 借用 Claude Code 的 OAuth client_id 在浏览器跑授权流程。
# token 存到 toolkit auth.json，LLM client 在 mode=oauth 时把 access_token
# 作为 ANTHROPIC_AUTH_TOKEN 注给 CLI 子进程。
#
# 注意：client_id 是 Claude Code 公开值；redirect_uri 是本机回调。如果 Anthropic
# 拒绝本机 redirect 或 client_id 失效，需要换 OOB 流程（用户复制粘贴 code）。

CLAUDE_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_OAUTH_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
CLAUDE_OAUTH_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
CLAUDE_OAUTH_SCOPES = "org:create_api_key user:profile user:inference"
# OAuth client_id 的 redirect_uri 被 Anthropic 锁死成这个 OOB callback,
# 用户授权后会看到 code 显示在 console.anthropic.com 内部页面,需要手动复制
# 粘贴回 toolkit。本机 redirect 试过(127.0.0.1:8084)— Anthropic 拒绝。
CLAUDE_OAUTH_REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"

# 进行中的 OAuth 会话：state → (code_verifier, created_at)
_OAUTH_PENDING: dict[str, dict[str, Any]] = {}
_OAUTH_TTL_SEC = 600  # 10 分钟未完成视为失效

# OAuth token 刷新锁 — 防止并发跑工具时多个协程同时刷新
import asyncio as _asyncio_oauth
_OAUTH_REFRESH_LOCK = _asyncio_oauth.Lock()


async def ensure_fresh_oauth_token() -> dict[str, Any]:
    """确保 OAuth access_token 没过期 — 过期(或 5 分钟内将过期)就用 refresh_token 换新的。

    Anthropic OAuth access_token 只活 8 小时,过期后所有 LLM 调用拿 401。
    refresh_token 活得久(数十天),用 grant_type=refresh_token 可无人值守续期。

    返回 {"status": "fresh|refreshed|expired|not_oauth|no_refresh_token|error", "detail": ...}
    - fresh           : token 还有效,没动
    - refreshed       : 成功刷新
    - not_oauth       : 当前不是 oauth 模式,跳过
    - no_refresh_token: 没存 refresh_token,只能让 admin 重新授权
    - error           : 刷新请求失败(refresh_token 也可能过期了)
    """
    try:
        from packages.core.auth_config import (
            get_auth_mode, get_oauth_access_token, get_oauth_refresh_token,
            get_oauth_expires_at, get_oauth_account, set_oauth_tokens,
        )
    except Exception as exc:
        return {"status": "error", "detail": f"auth_config 加载失败: {exc}"}

    if get_auth_mode() != "oauth":
        return {"status": "not_oauth"}

    # 还有 >30 分钟才到期 → 不用动。阈值留大是因为单个 run 可能跑 8-10 分钟,
    # token 必须在 run 全程有效,不能跑一半过期。
    _REFRESH_THRESHOLD = 1800  # 30 分钟
    expires_at = get_oauth_expires_at() or 0
    now = _time.time()
    if expires_at and (expires_at - now) > _REFRESH_THRESHOLD:
        return {"status": "fresh", "detail": f"还有 {(expires_at-now)/60:.0f} 分钟到期"}

    refresh_token = get_oauth_refresh_token()
    if not refresh_token:
        return {"status": "no_refresh_token",
                "detail": "没有 refresh_token,需 admin 到设置页重新 OAuth 授权"}

    async with _OAUTH_REFRESH_LOCK:
        # 拿到锁后再查一次 — 可能别的协程已经刷过了
        expires_at = get_oauth_expires_at() or 0
        now = _time.time()
        if expires_at and (expires_at - now) > _REFRESH_THRESHOLD:
            return {"status": "fresh", "detail": "并发协程已刷新"}

        import httpx
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLAUDE_OAUTH_CLIENT_ID,
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as cli:
                resp = await cli.post(
                    CLAUDE_OAUTH_TOKEN_URL, json=payload,
                    headers={"Content-Type": "application/json",
                             "Accept": "application/json"},
                )
        except Exception as exc:
            return {"status": "error", "detail": f"刷新请求失败: {exc}"}

        if resp.status_code != 200:
            return {"status": "error",
                    "detail": f"Anthropic 拒绝刷新 (HTTP {resp.status_code}): {resp.text[:200]}"}
        try:
            data = resp.json()
        except Exception as exc:
            return {"status": "error", "detail": f"刷新返回非 JSON: {exc}"}

        new_access = data.get("access_token")
        if not new_access:
            return {"status": "error", "detail": f"刷新未返回 access_token: {_json.dumps(data)[:200]}"}
        new_refresh = data.get("refresh_token") or refresh_token  # 有些实现不轮换 refresh
        expires_in = data.get("expires_in")
        new_expires_at = int(_time.time()) + int(expires_in) if expires_in else None
        set_oauth_tokens(
            access_token=new_access,
            refresh_token=new_refresh,
            expires_at=new_expires_at,
            account=get_oauth_account() or None,
        )
        print(f"[oauth] token refreshed, expires_in={expires_in}s")
        return {"status": "refreshed",
                "detail": f"已续期,新 token {expires_in}s 后到期"}


def _gc_oauth_pending() -> None:
    now = _time.time()
    stale = [s for s, v in _OAUTH_PENDING.items() if now - v.get("created_at", 0) > _OAUTH_TTL_SEC]
    for s in stale:
        _OAUTH_PENDING.pop(s, None)


def _make_pkce() -> tuple[str, str]:
    """返回 (code_verifier, code_challenge) — PKCE S256。"""
    import base64
    import hashlib
    import secrets
    verifier = secrets.token_urlsafe(48)[:64]
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


@app.post("/api/auth/oauth/start")
async def api_auth_oauth_start(request: Request) -> dict[str, Any]:
    """启动 OAuth flow (OOB 模式) — 生成 PKCE + state,返回 authorize URL。
    只有 admin 可以改全局 Claude 凭据。

    Anthropic OAuth client_id 锁死 redirect_uri 在 console.anthropic.com 的
    OOB callback,授权完成后用户会看到 code 显示在网页上,需要手动复制粘贴。
    """
    require_admin(request)
    import secrets as _secrets
    _gc_oauth_pending()
    verifier, challenge = _make_pkce()
    state = _secrets.token_urlsafe(24)

    _OAUTH_PENDING[state] = {
        "code_verifier": verifier,
        "redirect_uri": CLAUDE_OAUTH_REDIRECT_URI,
        "created_at": _time.time(),
    }

    from urllib.parse import urlencode
    params = {
        "client_id": CLAUDE_OAUTH_CLIENT_ID,
        "redirect_uri": CLAUDE_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": CLAUDE_OAUTH_SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    authorize_url = f"{CLAUDE_OAUTH_AUTHORIZE_URL}?{urlencode(params)}"
    return {
        "authorize_url": authorize_url,
        "state": state,
        "expires_in": _OAUTH_TTL_SEC,
        "mode": "oob",  # 提示前端走"粘贴 code"流程
    }


class OAuthExchangeReq(BaseModel):
    code: str
    state: str


@app.post("/api/auth/oauth/exchange")
async def api_auth_oauth_exchange(req: OAuthExchangeReq, request: Request) -> dict[str, Any]:
    """OOB 模式:用户从授权页复制 code 后,POST 到这里换 token。
    只有 admin 可以改全局 Claude 凭据。

    code 格式可能是 "abc-def#state=xxx" 或纯 "abc-def" — 都接受。
    """
    require_admin(request)
    _gc_oauth_pending()
    code_raw = (req.code or "").strip()
    state = (req.state or "").strip()
    if not code_raw or not state:
        raise HTTPException(400, "code 和 state 都不能为空")

    # 用户可能直接粘贴完整 URL 或 code#state= 这种格式 — 清理一下
    # Anthropic OAuth code 形如 "uuid#state=xxx",支持把 fragment 部分摘掉
    if "#" in code_raw:
        code = code_raw.split("#", 1)[0].strip()
    else:
        code = code_raw

    pending = _OAUTH_PENDING.pop(state, None)
    if not pending:
        raise HTTPException(
            410,
            "OAuth 会话失效或 state 不匹配(10 分钟有效)。请回到弹窗重新点「OAuth 授权」。"
        )

    import httpx
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": pending["redirect_uri"],
        "client_id": CLAUDE_OAUTH_CLIENT_ID,
        "code_verifier": pending["code_verifier"],
        "state": state,
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as cli:
            resp = await cli.post(
                CLAUDE_OAUTH_TOKEN_URL,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
    except Exception as e:
        raise HTTPException(502, f"调用 Anthropic token 端点失败:{e}")

    if resp.status_code != 200:
        raise HTTPException(
            resp.status_code,
            f"Anthropic 拒绝 token 换取(HTTP {resp.status_code}):{resp.text[:400]}"
        )
    try:
        data = resp.json()
    except Exception as e:
        raise HTTPException(502, f"Anthropic 返回非 JSON:{resp.text[:400]} ({e})")

    access_token = data.get("access_token")
    if not access_token:
        raise HTTPException(502, f"Anthropic 未返回 access_token:{_json.dumps(data)[:400]}")

    refresh_token = data.get("refresh_token")
    expires_in = data.get("expires_in")
    expires_at = int(_time.time()) + int(expires_in) if expires_in else None
    acc = data.get("account") or {}
    account_info = {
        "email": acc.get("email") or data.get("email"),
        "display_name": acc.get("name") or acc.get("display_name"),
        "organization_name": (
            (data.get("organization") or {}).get("name")
            if isinstance(data.get("organization"), dict) else None
        ),
        "billing_type": acc.get("billing_type"),
        "scope": data.get("scope"),
    }
    account_info = {k: v for k, v in account_info.items() if v}

    try:
        from packages.core.auth_config import set_auth_mode, set_oauth_tokens
        set_oauth_tokens(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            account=account_info,
        )
        set_auth_mode("oauth")
    except Exception as e:
        raise HTTPException(500, f"保存 token 失败:{e}")

    return {
        "status": "ok",
        "account": account_info,
        "expires_at": expires_at,
        "has_refresh": bool(refresh_token),
    }


def _html_callback_response(ok: bool, title: str, detail: str) -> HTMLResponse:
    """OAuth callback 完成后给用户看的最小页面。"""
    color = "#10b981" if ok else "#f87171"
    icon = "✓" if ok else "✕"
    return HTMLResponse(
        f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"/>
<title>{title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@300;400;500;600;700&family=Noto+Sans+SC:wght@300;400;500;600&family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
  body{{font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;
       background:#0a0a0a;color:#e4e4e7;margin:0;padding:0;
       display:flex;align-items:center;justify-content:center;min-height:100vh}}
  .card{{max-width:480px;padding:36px 40px;border-radius:16px;
        background:#18181b;border:1px solid #27272a;text-align:center}}
  .icon{{font-size:48px;color:{color};margin-bottom:18px}}
  h1{{margin:0 0 12px;font-size:20px;font-weight:600;letter-spacing:-.01em}}
  p{{margin:0;color:#a1a1aa;font-size:13.5px;line-height:1.6}}
  .hint{{margin-top:18px;font-size:12px;color:#71717a}}
</style></head><body>
<div class="card">
  <div class="icon">{icon}</div>
  <h1>{title}</h1>
  <p>{detail}</p>
  <p class="hint">可关闭此 tab，返回原页面查看接入状态。</p>
</div>
</body></html>""",
        status_code=200 if ok else 400,
    )


@app.get("/api/auth/oauth/callback")
async def api_auth_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> HTMLResponse:
    """OAuth 回调 — 拿 code 换 token，存到 toolkit auth.json。"""
    _gc_oauth_pending()
    if error:
        return _html_callback_response(
            False, "OAuth 授权被拒绝",
            f"Anthropic 返回错误：{error}" + (f"<br/>{error_description}" if error_description else "")
        )
    if not code or not state:
        return _html_callback_response(
            False, "OAuth 回调参数缺失",
            "URL 里没有 code 或 state — 可能不是从授权页正常跳回。"
        )
    pending = _OAUTH_PENDING.pop(state, None)
    if not pending:
        return _html_callback_response(
            False, "OAuth 会话失效",
            "state 不匹配或已过期(10 分钟有效)。请回到 toolkit 重新点击「OAuth 授权」。"
        )

    # 用 code + verifier 换 token
    import httpx
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": pending["redirect_uri"],
        "client_id": CLAUDE_OAUTH_CLIENT_ID,
        "code_verifier": pending["code_verifier"],
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as cli:
            resp = await cli.post(
                CLAUDE_OAUTH_TOKEN_URL,
                json=payload,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
        if resp.status_code != 200:
            return _html_callback_response(
                False, "换取 access_token 失败",
                f"Anthropic token endpoint 返回 {resp.status_code}：<br/>"
                f"<code style='font-family:ui-monospace,monospace;font-size:11px'>"
                f"{escape(resp.text[:400])}</code>"
            )
        data = resp.json()
    except Exception as e:
        return _html_callback_response(
            False, "OAuth 网络错误",
            f"调用 Anthropic token endpoint 时出错：<br/>{escape(str(e)[:300])}"
        )

    access_token = data.get("access_token")
    if not access_token:
        return _html_callback_response(
            False, "Anthropic 未返回 access_token",
            f"<code style='font-family:ui-monospace,monospace;font-size:11px'>"
            f"{escape(_json.dumps(data, ensure_ascii=False)[:400])}</code>"
        )
    refresh_token = data.get("refresh_token")
    expires_in = data.get("expires_in")
    expires_at = int(_time.time()) + int(expires_in) if expires_in else None
    # 部分 OAuth 响应直接含账号信息
    acc = data.get("account") or {}
    account_info = {
        "email": acc.get("email") or data.get("email"),
        "display_name": acc.get("name") or acc.get("display_name"),
        "organization_name": (data.get("organization") or {}).get("name") if isinstance(data.get("organization"), dict) else None,
        "billing_type": acc.get("billing_type"),
        "scope": data.get("scope"),
    }
    account_info = {k: v for k, v in account_info.items() if v}

    try:
        from packages.core.auth_config import set_auth_mode, set_oauth_tokens
        set_oauth_tokens(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            account=account_info,
        )
        set_auth_mode("oauth")
    except Exception as e:
        return _html_callback_response(
            False, "保存 token 失败",
            f"换到 token 但写入 auth.json 出错：{escape(str(e)[:300])}"
        )

    return _html_callback_response(
        True, "OAuth 授权成功",
        "已把凭据保存到 toolkit。回到「设置 → 模型接入」页面应已显示 OAuth 模式已接入。"
    )


# Track install jobs so the UI can poll their progress.
_INSTALL_JOBS: dict[str, dict[str, Any]] = {}


class InstallReq(BaseModel):
    target: str  # package name or "playwright_browsers"


async def _run_install_job(job_id: str, target: str) -> None:
    job = _INSTALL_JOBS[job_id]
    job["status"] = "running"
    if target == _PLAYWRIGHT_BROWSER_INSTALL:
        # Use the current playwright module to download chromium. Calls the
        # python entry point of the playwright pkg.
        return await _run_playwright_install_in_process(job)
    if target == "claude_cli":
        # Auto-install Claude Code via official curl pipe. Drops binary at
        # ~/.local/bin/claude. After install, user still needs to run
        # `claude login` (interactive OAuth) — we surface that next.
        return await _run_claude_cli_install(job)

    pip = str(Path(_sys.executable).parent / "pip")
    argv = [pip, "install", "-U", target]
    job["command"] = " ".join(argv)
    try:
        proc = await _asyncio.create_subprocess_exec(
            *argv,
            stdout=_asyncio.subprocess.PIPE,
            stderr=_asyncio.subprocess.STDOUT,
        )
        out_b, _ = await _asyncio.wait_for(proc.communicate(), timeout=600)
        out = out_b.decode("utf-8", errors="replace")
        job["log"] = out[-4000:]
        job["return_code"] = proc.returncode
        job["status"] = "succeeded" if proc.returncode == 0 else "failed"
    except _asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        job["status"] = "failed"
        job["log"] = (job.get("log") or "") + "\n[timeout after 600s]"
    except Exception as exc:
        job["status"] = "failed"
        job["log"] = f"{type(exc).__name__}: {exc}"
    finally:
        job["finished_at"] = _time.time()


async def _run_playwright_install_in_process(job: dict[str, Any]) -> None:
    """下载 chromium 浏览器到 PLAYWRIGHT_BROWSERS_PATH。

    用 playwright 自带的 CLI（同进程导入）触发，不依赖外部 pip。
    """
    job["command"] = "python -m playwright install chromium"
    job["log"] = ""
    try:
        # The playwright package ships an `install` driver we can invoke.
        # Capture its stdout/stderr so the user sees download progress.
        import io
        import subprocess
        # playwright bundles a Node driver — we need to call its CLI script.
        # The cleanest cross-mode approach: locate the playwright CLI inside
        # the python package and invoke via subprocess so output streams.
        import playwright
        from pathlib import Path as _P
        pkg_dir = _P(playwright.__file__).parent
        node_exe = pkg_dir / "driver" / "node"
        cli_js = pkg_dir / "driver" / "package" / "cli.js"
        if not (node_exe.exists() and cli_js.exists()):
            job["status"] = "failed"
            job["log"] = (
                f"找不到 playwright driver。预期路径:\n"
                f"  {node_exe}\n  {cli_js}\n"
                "可能 playwright 包安装不完整 — 试 `pip install -U playwright` 后重启。"
            )
            return
        argv = [str(node_exe), str(cli_js), "install", "chromium"]
        proc = await _asyncio.create_subprocess_exec(
            *argv,
            stdout=_asyncio.subprocess.PIPE,
            stderr=_asyncio.subprocess.STDOUT,
            env={**os.environ},  # respects PLAYWRIGHT_BROWSERS_PATH if set
        )
        out_b, _ = await _asyncio.wait_for(proc.communicate(), timeout=900)
        out = out_b.decode("utf-8", errors="replace")
        job["log"] = out[-4000:]
        job["return_code"] = proc.returncode
        job["status"] = "succeeded" if proc.returncode == 0 else "failed"
    except _asyncio.TimeoutError:
        try: proc.kill()
        except Exception: pass
        job["status"] = "failed"
        job["log"] = (job.get("log") or "") + "\n[timeout after 900s]"
    except Exception as exc:
        import traceback as _tbm
        job["status"] = "failed"
        job["log"] = f"{type(exc).__name__}: {exc}\n{_tbm.format_exc()}"
    finally:
        job["finished_at"] = _time.time()


async def _run_claude_cli_install(job: dict[str, Any]) -> None:
    """Auto-install Claude Code CLI via the official one-line installer.

    Equivalent to user running:
        curl -fsSL https://claude.ai/install.sh | bash

    Drops the binary at ~/.local/bin/claude. After this the user MUST run
    `claude login` interactively (we can't automate the OAuth) — surfaced
    in the post-install message.
    """
    job["command"] = "curl -fsSL https://claude.ai/install.sh | bash"
    job["log"] = ""
    try:
        proc = await _asyncio.create_subprocess_shell(
            "curl -fsSL https://claude.ai/install.sh | bash",
            stdout=_asyncio.subprocess.PIPE,
            stderr=_asyncio.subprocess.STDOUT,
            env={**os.environ},
        )
        out_b, _ = await _asyncio.wait_for(proc.communicate(), timeout=300)
        out = out_b.decode("utf-8", errors="replace")
        job["log"] = out[-4000:]
        job["return_code"] = proc.returncode

        # Verify by running --version on the installed binary
        installed = _check_claude_cli()
        if installed.get("installed"):
            job["status"] = "succeeded"
            job["log"] += (
                "\n\n[✓ Claude Code 二进制已安装]\n"
                f"路径：{installed.get('path')}\n"
                f"版本：{installed.get('version')}\n\n"
                "下一步：请在终端执行 `claude login` 完成账户授权 — "
                "这一步必须用户手动做（OAuth 浏览器流程）。\n"
                "完成后回到本应用「设置」页点「重新检测」。"
            )
            job["next_action"] = "manual_login"
            job["next_command"] = "claude login"
        else:
            job["status"] = "failed"
            job["log"] += (
                "\n\n[✗ 安装脚本退出但未找到 claude 二进制]\n"
                "请手动执行：\n"
                "  curl -fsSL https://claude.ai/install.sh | bash\n"
                "  claude login"
            )
    except _asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        job["status"] = "failed"
        job["log"] = (job.get("log") or "") + "\n[timeout after 300s — 检查网络]"
    except Exception as exc:
        import traceback as _tbm
        job["status"] = "failed"
        job["log"] = f"{type(exc).__name__}: {exc}\n{_tbm.format_exc()}"
    finally:
        job["finished_at"] = _time.time()


@app.post("/api/settings/install")
async def api_settings_install(req: InstallReq) -> dict[str, Any]:
    """一键安装某个工具依赖。返回 job_id 后异步轮询。"""
    target = req.target.strip()
    # System-level targets we can self-install on macOS:
    #   claude_cli   — curl pipe → ~/.local/bin/claude
    #   playwright_browsers — playwright + chromium download
    if target == "claude_cli":
        pass
    elif target not in _INSTALLABLE_PKGS and target != _PLAYWRIGHT_BROWSER_INSTALL:
        raise HTTPException(400, f"package not in install allowlist: {target}")
    job_id = str(uuid4())
    _INSTALL_JOBS[job_id] = {
        "job_id": job_id,
        "target": target,
        "status": "queued",
        "started_at": _time.time(),
        "finished_at": None,
        "log": "",
    }
    _asyncio.create_task(_run_install_job(job_id, target))
    return {"job_id": job_id}


@app.get("/api/settings/install/{job_id}")
async def api_settings_install_status(job_id: str) -> dict[str, Any]:
    job = _INSTALL_JOBS.get(job_id)
    if not job:
        raise HTTPException(404, f"install job {job_id} not found")
    # 过滤内部字段（asyncio.subprocess / Task 等不可 JSON 序列化）
    # 任何 _ 开头的 key 都视作内部字段
    return {k: v for k, v in job.items() if not k.startswith("_")}


# ----- Reports listing -----

@app.get("/api/reports")
async def api_reports(request: Request) -> dict[str, Any]:
    """合并：本会话内存 runs + 持久化 JSON 报告文件。
    普通用户只看自己的;admin 看全部。"""
    user = require_user(request)
    out_dir = Path(settings.report_output_dir)
    saved: list[dict[str, Any]] = []
    if out_dir.exists():
        # 匹配 step*, h5_adapt, network_resilience, seo_audit, tdr_*
        # 标准格式 `<tool_id>_<run_id>.json`
        valid_tool_ids = {t["id"] for t in TOOL_CATALOG}
        seen_paths: set[str] = set()
        # 最长前缀匹配优先，避免把 network_resilience_xxx.json 解成 network。
        for p in sorted(out_dir.glob("*.json"), key=lambda x: -x.stat().st_mtime):
            stem = p.stem
            if "_" not in stem:
                continue
            tool_id: str | None = None
            run_id: str | None = None
            for tid in sorted(valid_tool_ids | {"tdr"}, key=lambda s: -len(s)):
                if stem.startswith(tid + "_"):
                    tool_id = tid
                    run_id = stem[len(tid) + 1 :]
                    break
            if tool_id is None:
                tool_id, run_id = stem.split("_", 1)
            seen_paths.add(str(p))
            # 轻量预读 project info (只读 meta 字段,避免完整 JSON 反序列化大文件)
            pc = pn = None
            owner_uid: int | None = None
            owner_un: str | None = None
            try:
                # 文件可能很大;读前 4 KB 用 json.loads 兜底失败再扫一次
                data = _json.loads(p.read_text(encoding="utf-8"))
                m = (data or {}).get("meta") or {}
                pc = m.get("project_code")
                pn = m.get("project_name")
                owner_uid = m.get("owner_user_id")
                owner_un = m.get("owner_username") or m.get("owner_email")
            except Exception:
                pass
            # 权限过滤:普通用户跳过别人的报告 (legacy 无 owner 的报告也只对 admin 可见)
            if not _user_can_see(user, owner_uid):
                continue
            saved.append({
                "source": "file",
                "tool_id": tool_id,
                "run_id": run_id,
                "filename": p.name,
                "path": str(p),
                "size": p.stat().st_size,
                "mtime": p.stat().st_mtime,
                "project_code": pc,
                "project_name": pn,
                "owner_user_id": owner_uid,
                "owner_username": owner_un,
            })

        # 也扫嵌套 TDR 目录：<report_dir>/tdr/<run_id>/review.json
        # （TdrWorkstation.finalize 默认写到这里，跟 /api/tdr/submit mirror 文件不同）
        tdr_dir = out_dir / "tdr"
        if tdr_dir.exists():
            for review_file in sorted(tdr_dir.glob("*/review.json"), key=lambda x: -x.stat().st_mtime):
                if str(review_file) in seen_paths:
                    continue
                run_id = review_file.parent.name
                tdr_owner = None
                try:
                    tdr_data = _json.loads(review_file.read_text(encoding="utf-8"))
                    tdr_owner = (tdr_data.get("meta") or {}).get("owner_user_id")
                except Exception:
                    pass
                if not _user_can_see(user, tdr_owner):
                    continue
                saved.append({
                    "source": "file",
                    "tool_id": "tdr",
                    "run_id": run_id,
                    "filename": f"tdr/{run_id}/review.json",
                    "path": str(review_file),
                    "size": review_file.stat().st_size,
                    "mtime": review_file.stat().st_mtime,
                    "owner_user_id": tdr_owner,
                })
    in_memory = sorted(
        (r for r in _RUNS.values() if _user_can_see(user, r.get("owner_user_id"))),
        key=lambda r: -r["started_at"],
    )
    in_memory_summarized = [
        {
            "source": "memory",
            "tool_id": r["tool_id"],
            "tool_name": r.get("tool_name"),
            "run_id": r["run_id"],
            "status": r["status"],
            "started_at": r["started_at"],
            "finished_at": r.get("finished_at"),
            "report_path": r.get("report_path"),
            "usage": r.get("usage"),
            "project_code": r.get("project_code"),
            "project_name": r.get("project_name"),
            "owner_user_id": r.get("owner_user_id"),
            "owner_username": r.get("owner_username"),
        }
        for r in in_memory
    ]
    return {
        "in_memory": in_memory_summarized,
        "saved": saved,
        "total_saved": len(saved),
    }


@app.get("/api/reports/export")
async def api_reports_export() -> StreamingResponse:
    """打包全部已保存报告为一个 zip 下载。

    通过 _iter_saved_report_files() 同时打包顶层 *.json 和嵌套 tdr/<rid>/review.json，
    保证 raw /tdr 提交的报告也在导出范围内。

    NOTE: 必须注册在 /api/reports/{run_id} 之前，否则 run_id 会匹配 "export"。
    """
    import io as _io
    import zipfile as _zip
    out_dir = Path(settings.report_output_dir)
    buf = _io.BytesIO()
    with _zip.ZipFile(buf, mode="w", compression=_zip.ZIP_DEFLATED) as zf:
        for p in sorted(_iter_saved_report_files(), key=lambda x: -x.stat().st_mtime):
            try:
                arc = p.relative_to(out_dir).as_posix()
            except ValueError:
                arc = p.name
            zf.write(p, arcname=arc)
    buf.seek(0)
    fname = f"tianshu-reports-{int(_time.time())}.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/api/reports/{run_id}")
async def api_report_detail(run_id: str, request: Request) -> dict[str, Any]:
    """按 run_id 拿报告：先查内存，再用 _find_report_files_by_run_id 扫盘。

    读时把 finalize substep 的统一报告契约字段提升到顶层，让 UI 直接读到
    verdict / risks / blockers / issues / cases — 兼容旧 run。
    """
    user = require_user(request)
    if run_id in _RUNS:
        state = _RUNS[run_id]
        if not _user_can_see(user, state.get("owner_user_id")):
            raise HTTPException(403, "无权访问此报告")
        if isinstance(state.get("report"), dict):
            state["report"] = _promote_contract_fields(state["report"])
        return state
    files = _find_report_files_by_run_id(run_id)
    for p in files:
        try:
            stem = p.stem
            if p.parent.parent.name == "tdr" and p.name == "review.json":
                tid = "tdr"
            else:
                tid = _parse_tool_id_from_stem(stem, run_id)
            data = _json.loads(p.read_text(encoding="utf-8"))
            owner_uid = (data.get("meta") or {}).get("owner_user_id")
            if not _user_can_see(user, owner_uid):
                raise HTTPException(403, "无权访问此报告")
            return {
                "source": "file",
                "run_id": run_id,
                "tool_id": tid,
                "report": _promote_contract_fields(data),
                "report_path": str(p),
            }
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(500, f"failed to read {p}: {exc}")
    raise HTTPException(404, f"report {run_id} not found")


@app.delete("/api/reports/{run_id}")
async def api_report_delete(run_id: str, request: Request) -> dict[str, Any]:
    """删除某条报告（内存 + 磁盘 + 嵌套 review.json）。owner 或 admin 才能删。"""
    user = require_user(request)
    # 先检查权限
    if run_id in _RUNS:
        state = _RUNS[run_id]
        if not _user_can_see(user, state.get("owner_user_id")):
            raise HTTPException(403, "无权删除此报告")
    for p in _find_report_files_by_run_id(run_id):
        try:
            data = _json.loads(p.read_text(encoding="utf-8"))
            owner_uid = (data.get("meta") or {}).get("owner_user_id")
            if not _user_can_see(user, owner_uid):
                raise HTTPException(403, "无权删除此报告")
        except HTTPException:
            raise
        except Exception:
            pass  # 读失败不阻塞,继续走删
    deleted_files: list[str] = []
    deleted_memory = False
    if run_id in _RUNS:
        del _RUNS[run_id]
        deleted_memory = True
    for p in _find_report_files_by_run_id(run_id):
        try:
            p.unlink()
            deleted_files.append(p.name)
            # 嵌套目录也清掉空 dir（tdr/<run_id>/）
            try:
                if p.parent.name == run_id and p.parent.parent.name == "tdr":
                    if not any(p.parent.iterdir()):
                        p.parent.rmdir()
            except Exception:
                pass
        except Exception as exc:
            raise HTTPException(500, f"failed to delete {p}: {exc}")
    if not deleted_memory and not deleted_files:
        raise HTTPException(404, f"report {run_id} not found")
    return {"ok": True, "memory": deleted_memory, "files": deleted_files}


# ---------- 批量删除 ----------

class BatchDeleteReq(BaseModel):
    run_ids: list[str] = []
    all_visible: bool = False  # True = 删当前用户能看到的所有报告(admin=全部, user=本人)


def _delete_one_report(run_id: str, user: UserRecord) -> tuple[bool, str | None]:
    """删除单条报告 — 返回 (成功?, 错误原因)。"""
    # 权限检查 + 收集要删的文件
    has_memory = run_id in _RUNS
    if has_memory:
        state = _RUNS[run_id]
        if not _user_can_see(user, state.get("owner_user_id")):
            return False, "无权删除"
    files = _find_report_files_by_run_id(run_id)
    for p in files:
        try:
            data = _json.loads(p.read_text(encoding="utf-8"))
            owner_uid = (data.get("meta") or {}).get("owner_user_id")
            if not _user_can_see(user, owner_uid):
                return False, "无权删除"
        except Exception:
            pass
    if not has_memory and not files:
        return False, "不存在"
    # 真删
    if has_memory:
        del _RUNS[run_id]
    for p in files:
        try:
            p.unlink()
            try:
                if p.parent.name == run_id and p.parent.parent.name == "tdr":
                    if not any(p.parent.iterdir()):
                        p.parent.rmdir()
            except Exception:
                pass
        except Exception as exc:
            return False, f"删文件失败: {exc}"
    return True, None


@app.post("/api/reports/batch_delete")
async def api_reports_batch_delete(req: BatchDeleteReq, request: Request) -> dict[str, Any]:
    """批量删除。两种模式:
      - run_ids = [...] : 删指定的这些 run
      - all_visible=True : 删当前用户能看到的所有报告(admin=系统所有,user=自己所有)
    返回 {deleted: N, failed: [{run_id, reason}], total: M}
    """
    user = require_user(request)
    targets: list[str] = []
    if req.all_visible:
        # 收集所有可见 run_id
        for r in list(_RUNS.values()):
            if _user_can_see(user, r.get("owner_user_id")):
                targets.append(r["run_id"])
        out_dir = Path(settings.report_output_dir)
        if out_dir.exists():
            valid_tool_ids = {t["id"] for t in TOOL_CATALOG} | {"tdr"}
            for p in out_dir.glob("*.json"):
                stem = p.stem
                if "_" not in stem:
                    continue
                rid = None
                for tid in sorted(valid_tool_ids, key=lambda s: -len(s)):
                    if stem.startswith(tid + "_"):
                        rid = stem[len(tid)+1:]
                        break
                if not rid or rid in targets:
                    continue
                try:
                    data = _json.loads(p.read_text(encoding="utf-8"))
                    owner_uid = (data.get("meta") or {}).get("owner_user_id")
                except Exception:
                    owner_uid = None
                if _user_can_see(user, owner_uid):
                    targets.append(rid)
    else:
        # 去重保留顺序
        seen = set()
        for rid in req.run_ids or []:
            if rid and rid not in seen:
                seen.add(rid); targets.append(rid)
    if not targets:
        return {"deleted": 0, "failed": [], "total": 0}
    deleted = 0
    failed: list[dict[str, Any]] = []
    for rid in targets:
        ok, reason = _delete_one_report(rid, user)
        if ok:
            deleted += 1
        else:
            failed.append({"run_id": rid, "reason": reason or "未知错误"})
    return {"deleted": deleted, "failed": failed, "total": len(targets)}


# ----- URL fetcher (input convenience) -----

class FetchUrlReq(BaseModel):
    url: str


_BINARY_PARSERS_AVAILABLE: dict[str, bool] = {}


def _parser_available(name: str) -> bool:
    if name in _BINARY_PARSERS_AVAILABLE:
        return _BINARY_PARSERS_AVAILABLE[name]
    try:
        if name == "pypdf":
            import pypdf  # noqa: F401
        elif name == "python-docx":
            import docx  # noqa: F401
        elif name == "openpyxl":
            import openpyxl  # noqa: F401
        elif name == "chardet":
            import chardet  # noqa: F401
        _BINARY_PARSERS_AVAILABLE[name] = True
    except ImportError:
        _BINARY_PARSERS_AVAILABLE[name] = False
    return _BINARY_PARSERS_AVAILABLE[name]


def _extract_pdf(blob: bytes) -> str:
    if not _parser_available("pypdf"):
        return "(pypdf 未安装；无法解析 PDF — 请到 /settings 安装)"
    import io
    import pypdf
    reader = pypdf.PdfReader(io.BytesIO(blob))
    parts = []
    for i, page in enumerate(reader.pages, 1):
        try:
            txt = page.extract_text() or ""
        except Exception as exc:
            txt = f"[页 {i} 解析失败：{exc}]"
        parts.append(f"--- page {i} ---\n{txt.strip()}")
    return "\n\n".join(parts) if parts else "(空 PDF)"


def _extract_docx(blob: bytes) -> str:
    if not _parser_available("python-docx"):
        return "(python-docx 未安装；无法解析 DOCX)"
    import io
    import docx
    doc = docx.Document(io.BytesIO(blob))
    paras = [p.text for p in doc.paragraphs if p.text.strip()]
    # Tables
    for ti, t in enumerate(doc.tables, 1):
        rows = []
        for row in t.rows:
            cells = [c.text.strip().replace("\n", " ") for c in row.cells]
            rows.append(" | ".join(cells))
        if rows:
            paras.append(f"\n--- table {ti} ---\n" + "\n".join(rows))
    return "\n".join(paras) if paras else "(空 DOCX)"


def _extract_xlsx(blob: bytes) -> str:
    if not _parser_available("openpyxl"):
        return "(openpyxl 未安装；无法解析 XLSX)"
    import io
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(blob), data_only=True, read_only=True)
    parts = []
    for ws in wb.worksheets:
        parts.append(f"--- sheet: {ws.title} ---")
        for row in ws.iter_rows(values_only=True):
            cells = ["" if v is None else str(v) for v in row]
            if any(c.strip() for c in cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts) if parts else "(空 XLSX)"


def _extract_text_with_encoding(blob: bytes) -> str:
    """Decode arbitrary text bytes with chardet fallback."""
    # Try UTF-8 first (BOM-tolerant)
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return blob.decode(enc)
        except UnicodeDecodeError:
            continue
    # Then chardet detect
    if _parser_available("chardet"):
        import chardet
        det = chardet.detect(blob)
        enc = det.get("encoding") or "gbk"
        try:
            return blob.decode(enc, errors="replace")
        except (UnicodeDecodeError, LookupError):
            pass
    # Last resort
    return blob.decode("utf-8", errors="replace")


def _extract_image_meta(blob: bytes, name: str) -> str:
    """Return metadata stub for images — actual OCR is out of scope."""
    if not _parser_available_pillow():
        return f"(图片 {name} · {len(blob)}B — Pillow 未安装无法读取尺寸)"
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(blob))
        return f"[图片 {name}: {img.format} · {img.width}×{img.height}px · 模式 {img.mode} · {len(blob)}B]\n(图片内容暂不解析；请用文字描述图中信息)"
    except Exception as exc:
        return f"[图片 {name} · {len(blob)}B · 读取失败：{exc}]"


def _parser_available_pillow() -> bool:
    try:
        from PIL import Image  # noqa: F401
        return True
    except ImportError:
        return False


try:
    from fastapi import File, UploadFile
except ImportError:
    pass


@app.post("/api/extract-file")
async def api_extract_file(file: UploadFile = File(...)) -> dict[str, Any]:
    """从上传文件中提取文本内容。

    支持：
      文本类：.md .txt .json .yaml .yml .csv .tsv .html .xml .py .js 等
      二进制文档：.pdf / .docx / .xlsx
      图片：返回元信息摘要（暂不 OCR）

    大小限制（防止单大文件 OOM）：
      - 文本类：10 MB
      - PDF/DOCX/XLSX：50 MB
      - 图片：20 MB
    超限直接 413，不会读完整个文件到内存。
    """
    name = file.filename or "uploaded"
    ext = Path(name).suffix.lower().lstrip(".")
    ct = (file.content_type or "").lower()

    # 按类型决定上限
    is_pdf_or_doc = ext in ("pdf", "docx", "xlsx", "xlsm") or "officedocument" in ct or ct == "application/pdf"
    is_image = ct.startswith("image/") or ext in ("png", "jpg", "jpeg", "gif", "webp", "bmp", "svg")
    if is_pdf_or_doc:
        max_bytes, kind = 50 * 1024 * 1024, "PDF/DOCX/XLSX"
    elif is_image:
        max_bytes, kind = 20 * 1024 * 1024, "图片"
    else:
        max_bytes, kind = 10 * 1024 * 1024, "文本"

    # 优先走 Content-Length 提前拒绝
    cl = file.headers.get("content-length") if hasattr(file, "headers") else None
    if cl:
        try:
            if int(cl) > max_bytes:
                raise HTTPException(413, f"{kind}文件超过 {max_bytes // 1024 // 1024}MB 上限（Content-Length={cl}）")
        except (ValueError, TypeError):
            pass

    # 流式累计读取，超限立即 413
    chunks: list[bytes] = []
    received = 0
    while True:
        chunk = await file.read(64 * 1024)  # 64K 块
        if not chunk:
            break
        received += len(chunk)
        if received > max_bytes:
            raise HTTPException(413, f"{kind}文件超过 {max_bytes // 1024 // 1024}MB 上限")
        chunks.append(chunk)
    blob = b"".join(chunks)

    # Decide handler
    if ext == "pdf" or ct == "application/pdf":
        text = _extract_pdf(blob)
    elif ext == "docx" or ct in ("application/vnd.openxmlformats-officedocument.wordprocessingml.document",):
        text = _extract_docx(blob)
    elif ext in ("xlsx", "xlsm") or ct in ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",):
        text = _extract_xlsx(blob)
    elif ct.startswith("image/") or ext in ("png", "jpg", "jpeg", "gif", "webp", "bmp", "svg"):
        text = _extract_image_meta(blob, name)
    elif ext in ("doc", "ppt", "pptx"):
        text = (
            f"[暂未支持 .{ext} 格式自动提取（建议另存为 .docx / .pdf 后再上传）]\n"
            f"文件名：{name}，大小 {len(blob)}B"
        )
    else:
        # Treat as text (md/txt/json/csv/yaml/html/source code/log...)
        text = _extract_text_with_encoding(blob)

    # Cap returned text to protect frontend
    MAX = 400_000
    truncated = False
    if len(text) > MAX:
        text = text[:MAX] + f"\n\n[截断：原始文本 {len(text):,} 字符，已显示前 {MAX:,} 字符]"
        truncated = True

    return {
        "filename": name,
        "content_type": ct or "application/octet-stream",
        "size": len(blob),
        "text": text,
        "truncated": truncated,
    }


def _is_safe_public_host(host: str) -> tuple[bool, str]:
    """判断 host 解析后是否所有 IP 都是公网 — SSRF 防御。

    返回 (safe, reason)。拒绝：loopback / private / link-local / multicast / unspecified /
    reserved。元数据地址 169.254.169.254 也被 link-local 覆盖。
    """
    import ipaddress, socket
    if not host:
        return False, "host 为空"
    # 同时拒绝直接给的字面 IP（即使域名也会被解析后再判一次）
    try:
        # getaddrinfo 拿全部解析结果（一个域名可能解析到多个 IP）
        infos = socket.getaddrinfo(host, None)
        for info in infos:
            ip_str = info[4][0]
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                continue
            if (ip.is_loopback or ip.is_private or ip.is_link_local
                or ip.is_multicast or ip.is_unspecified or ip.is_reserved):
                return False, f"{host} 解析到非公网地址 {ip_str}"
    except socket.gaierror as e:
        return False, f"DNS 解析失败：{e}"
    return True, ""


@app.post("/api/fetch-url")
async def api_fetch_url(req: FetchUrlReq) -> dict[str, Any]:
    """把任意可读 URL 抓回来作为输入文本。

    SSRF 防御：
      - 仅允许 http/https
      - 域名解析后所有 IP 必须是公网（拒绝 127.x / 10.x / 169.254.x 等）
      - 跟随重定向后**重新校验**最终落脚 URL
      - 响应大小硬上限 5MB（不只是字符串截断）
    """
    import httpx
    from urllib.parse import urlparse

    if not req.url.startswith(("http://", "https://")):
        raise HTTPException(400, "URL 必须是 http:// 或 https:// 开头")

    # 第一道：发送前校验 host
    parsed = urlparse(req.url)
    safe, reason = _is_safe_public_host(parsed.hostname or "")
    if not safe:
        raise HTTPException(403, f"拒绝抓取内网/loopback 地址：{reason}")

    MAX_BYTES = 5 * 1024 * 1024  # 5MB 硬上限
    try:
        # 改流式 — 实时限流，不等响应完整下载
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as cli:
            async with cli.stream("GET", req.url, headers={"User-Agent": "ai-test-toolkit/0.1"}) as r:
                r.raise_for_status()
                # 第二道：重定向后重新校验最终 URL
                final_parsed = urlparse(str(r.url))
                safe2, reason2 = _is_safe_public_host(final_parsed.hostname or "")
                if not safe2:
                    raise HTTPException(403, f"重定向到内网/loopback 地址：{reason2}")
                # Content-Length 提前拒绝（如果 server 给了准确值）
                cl = r.headers.get("content-length")
                if cl:
                    try:
                        if int(cl) > MAX_BYTES:
                            raise HTTPException(413, f"响应过大（Content-Length={cl} 超过 {MAX_BYTES // 1024 // 1024}MB 上限）")
                    except ValueError:
                        pass
                # 流式累计 — 超限立即中断（即使 server 不给 Content-Length 也能护住）
                chunks: list[bytes] = []
                received = 0
                async for chunk in r.aiter_bytes():
                    received += len(chunk)
                    if received > MAX_BYTES:
                        raise HTTPException(413, f"响应过大（流式累计超过 {MAX_BYTES // 1024 // 1024}MB 上限）")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                # 解码（按 charset；fallback utf-8）
                charset = "utf-8"
                ct_header = r.headers.get("content-type", "").lower()
                if "charset=" in ct_header:
                    charset = ct_header.split("charset=", 1)[1].split(";", 1)[0].strip() or "utf-8"
                try:
                    text = raw.decode(charset, errors="replace")
                except LookupError:
                    text = raw.decode("utf-8", errors="replace")
                return {
                    "url": str(r.url),
                    "status": r.status_code,
                    "content_type": ct_header,
                    "size": received,
                    "text": text[:200_000],  # frontend cap
                }
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"fetch failed: {exc}")


# =====================================================================
# Redesigned tool detail page + Settings page
# =====================================================================

TOOL_DETAIL_HTML = r"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>工具 — 天枢·裁决</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@300;400;500;600;700&family=Noto+Sans+SC:wght@300;400;500;600&family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
  :root{--bg:#ffffff;--surface:#f0f0f0;--surface-2:#ebebeb;--surface-3:#dcdcdc;
    --line:#c4c4c4;--line-2:#9e9e9e;
    --fg:#0a0a0a;--fg-2:#262626;--fg-3:#4a4a4a;--fg-4:#6e6e6e;
    --ac:#a8401f;--ac-2:#c45a3a;--ac-bg:rgba(168,64,31,.14);--ac-line:rgba(168,64,31,.58);
    --warn:#8a5300;--ok:#4f6b35;--bad:#8a2d12;--info:#3f5560;
    --running:#7a4f00;
    --mono:'Inter','SF Mono',ui-monospace,Menlo,monospace;
    --sans:'Noto Sans SC','PingFang SC',-apple-system,'Microsoft YaHei',sans-serif;
    --serif:'Noto Serif SC','Songti SC','STSong',Georgia,serif;}
  @media (prefers-reduced-motion: reduce){*,*::before,*::after{animation-duration:.01ms!important;transition-duration:.01ms!important}}
  *{box-sizing:border-box}
  html,body{margin:0;background:var(--bg);color:var(--fg);font-family:var(--sans);min-height:100%;
    -webkit-font-smoothing:antialiased}
  body{background:
    radial-gradient(ellipse 90% 50% at 50% -10%, rgba(196,90,58,.07), transparent 65%) fixed,
    radial-gradient(ellipse 80% 40% at 50% 110%, rgba(255,255,255,.02), transparent 60%) fixed,
    var(--bg);}
  ::selection{background:var(--ac-bg);color:var(--ac-2)}
  ::-webkit-scrollbar{width:10px;height:10px}
  ::-webkit-scrollbar-track{background:transparent}
  ::-webkit-scrollbar-thumb{background:var(--surface-3);border-radius:5px;border:2px solid var(--bg)}
  ::-webkit-scrollbar-thumb:hover{background:var(--line-2)}

  .topbar{display:flex;align-items:center;gap:0;height:56px;
    padding:0 24px;border-bottom:1px solid var(--line);
    background:rgba(255,255,255,.94);position:sticky;top:0;z-index:30;
    backdrop-filter:saturate(180%) blur(20px);-webkit-backdrop-filter:saturate(180%) blur(20px)}
  .topbar .logo{width:28px;height:28px;flex-shrink:0;
    background:linear-gradient(135deg,#262626,#1a1a1a);border-radius:6px;
    display:grid;place-items:center;color:#001f1a;font-weight:700;font-size:14px;letter-spacing:0;
    font-family:"PingFang SC",-apple-system,sans-serif;
    margin-right:14px;
    box-shadow:0 1px 2px rgba(0,0,0,.4),inset 0 1px 0 rgba(255,255,255,.18)}
  .topbar .logo .logo-mark{width:18px;height:18px;display:block;
    filter:drop-shadow(0 .5px 0 rgba(255,255,255,.18))}
  .topbar .logo-old-replaced{width:24px;height:24px;border:1.5px solid var(--ac);border-radius:6px;
    display:grid;place-items:center;color:var(--ac);font-family:var(--mono);font-weight:700;
    font-size:11px;margin-right:14px}
  .topbar .crumbs{display:flex;align-items:center;gap:8px;font-size:13px}
  .topbar .crumbs a{color:var(--fg-3);text-decoration:none}
  .topbar .crumbs a:hover{color:var(--ac)}
  .topbar .crumbs .sep{color:var(--fg-4)}
  .topbar .crumbs .current{color:var(--fg);font-weight:500;display:flex;align-items:center;gap:6px}
  .topbar .stats{margin-left:auto;display:flex;gap:14px;align-items:center;
    font-family:var(--mono);font-size:11px;color:var(--fg-3)}
  .topbar .stats .stat .v{color:var(--fg-2);margin-left:5px}
  .topbar .stats .stat.cost .v{color:var(--ac)}
  .topbar .stats .stat.gate.reject .v{color:var(--bad)}
  .topbar .stats .stat.gate.proceed .v{color:var(--ok)}
  .topbar button.run{background:linear-gradient(180deg,var(--ac-2),var(--ac));color:#001a14;border:none;
    padding:0 18px;height:34px;border-radius:8px;font-family:var(--sans);font-size:13px;
    font-weight:600;cursor:pointer;display:flex;align-items:center;gap:8px;margin-left:14px;
    box-shadow:0 1px 2px rgba(0,0,0,.4),inset 0 1px 0 rgba(255,255,255,.24);
    transition:transform .12s,filter .12s}
  .topbar button.run:hover:not(:disabled){filter:brightness(1.06);transform:translateY(-.5px)}
  .topbar button.run:active:not(:disabled){transform:translateY(0);filter:brightness(.97)}
  .topbar button.run:disabled{background:var(--surface-3);color:var(--fg-4);cursor:not-allowed;box-shadow:none}
  .topbar button.run kbd{background:rgba(0,26,20,.20);padding:2px 6px;border-radius:4px;
    font-family:var(--mono);font-size:10.5px;font-weight:600;letter-spacing:.02em}
  .topbar a.set{color:var(--fg-3);font-size:13px;text-decoration:none;padding:6px 10px;
    border-radius:6px;margin-left:6px;transition:background .12s,color .12s}
  .topbar a.set:hover{background:var(--surface-2);color:var(--ac-2)}
  .topbar .back-btn{
    display:inline-flex;align-items:center;gap:6px;
    margin-right:14px;padding:6px 12px;border-radius:6px;
    background:var(--surface-2);border:1px solid var(--line-2);
    color:var(--fg-2);text-decoration:none;font-size:12.5px;font-weight:500;
    transition:background .12s,color .12s,border-color .12s}
  .topbar .back-btn:hover{border-color:var(--ac-line);color:var(--ac-2);background:var(--ac-bg)}

  /* === Premium hero card === */
  .hero{position:relative;padding:32px 36px 28px;
    border-bottom:1px solid var(--line);overflow:hidden}
  .hero::before{
    content:"";position:absolute;inset:0;
    background:radial-gradient(circle 380px at 12% 30%, var(--ac-bg) 0%, transparent 70%);
    opacity:.85;pointer-events:none}
  .hero-inner{position:relative;display:flex;align-items:flex-start;gap:22px;max-width:1200px;margin:0 auto}
  .hero-icon-frame{
    flex-shrink:0;width:88px;height:88px;border-radius:20px;
    background:linear-gradient(135deg,var(--surface-3) 0%,var(--surface-2) 100%);
    border:1px solid var(--line-2);
    display:grid;place-items:center;
    box-shadow:var(--shadow-2),inset 0 1px 0 rgba(255,255,255,.04);
    position:relative;overflow:hidden}
  .hero-icon-frame::before{
    content:"";position:absolute;inset:0;
    background:radial-gradient(circle at 30% 20%, rgba(110,231,183,.18), transparent 60%);
    pointer-events:none}
  .hero-icon-frame .ic{font-size:42px;line-height:1;position:relative;z-index:1;
    filter:drop-shadow(0 2px 8px rgba(0,0,0,.4))}
  .hero-body{flex:1;min-width:0}
  .hero-meta{display:flex;align-items:center;gap:8px;
    font-family:"SF Mono",ui-monospace,monospace;
    font-size:11px;color:var(--fg-3);margin-top:10px;
    text-transform:uppercase;letter-spacing:.06em}
  /* hero 右侧:接入状态紧凑显示 */
  .hero-right{margin-left:auto;display:flex;flex-direction:column;align-items:flex-end;gap:6px;
    font-family:var(--mono);font-size:11.5px;color:var(--fg-3);min-width:160px}
  .hero-right .row{display:flex;align-items:center;gap:6px}
  .hero-right .row.ok{color:var(--ok)}
  .hero-right .row.warn{color:var(--warn)}
  .hero-right .row.bad{color:var(--bad)}
  .hero-right .dot{width:6px;height:6px;border-radius:50%;background:currentColor}
  .hero-right a{color:var(--fg-3);text-decoration:none;font-size:11px}
  .hero-right a:hover{color:var(--ac)}
  .hero-meta .step{
    background:var(--ac-bg);color:var(--ac-2);
    padding:3px 10px;border-radius:4px;font-weight:600;
    border:1px solid var(--ac-line)}
  .hero-meta .resp{color:var(--fg-2)}
  .hero h2{margin:0 0 4px;font-size:24px;letter-spacing:-.02em;font-weight:600;line-height:1.2;
    color:var(--fg)}
  .hero .tag{color:var(--fg-3);font-size:13px;line-height:1.6;margin:0;max-width:780px}
  .hero-pills{display:flex;gap:8px;flex-wrap:wrap;margin-top:4px}
  .hero-pill{
    display:inline-flex;align-items:center;gap:6px;
    padding:5px 11px;border-radius:999px;
    background:var(--surface-2);border:1px solid var(--line-2);
    font-size:11.5px;color:var(--fg-2);font-family:"SF Mono",ui-monospace,monospace;
    transition:border-color .12s,color .12s}
  .hero-pill:hover{border-color:var(--ac-line);color:var(--fg)}
  .hero-pill .lbl{color:var(--fg-3);font-size:10.5px;text-transform:uppercase;letter-spacing:.05em}
  .hero-pill.success{border-color:rgba(52,211,153,.28);color:var(--ok);background:rgba(52,211,153,.06)}
  .hero-pill.fail{border-color:rgba(248,113,113,.28);color:var(--bad);background:rgba(248,113,113,.06)}
  .hero-pill.warn{border-color:rgba(251,191,36,.28);color:var(--warn);background:rgba(251,191,36,.06)}

  main{max-width:1280px;margin:0 auto;padding:0 28px 80px}
  .runner-grid{
    display:grid;
    grid-template-columns:minmax(0,1fr) 420px;
    gap:20px;
    padding-top:20px;
    align-items:start;
    transition:grid-template-columns .25s ease}
  /* 初态(无运行任务):右侧面板折叠成 56px 窄条,只显示状态 + 快捷运行提示 */
  .runner-grid.idle{grid-template-columns:minmax(0,1fr) 56px}
  .runner-grid.idle .run-panel{padding:0;cursor:pointer}
  .runner-grid.idle .run-panel:hover{border-color:var(--ac-line)}
  .runner-grid.idle .run-panel-body{display:flex;flex-direction:column;align-items:center;
    justify-content:center;gap:14px;padding:18px 4px;
    writing-mode:vertical-rl;text-orientation:upright;
    font-family:var(--mono);font-size:11px;color:var(--fg-3);letter-spacing:.1em}
  .runner-grid.idle #run-area{display:flex;flex-direction:column;align-items:center;
    justify-content:center;gap:14px}
  .runner-grid.idle #run-area > *{writing-mode:horizontal-tb;text-orientation:mixed}
  /* 隐藏空态长文本,只露 ▶ 圆按钮 + 「⌘↵」提示 */
  .runner-grid.idle .run-empty-pretty .title,
  .runner-grid.idle .run-empty-pretty .desc{display:none}
  .runner-grid.idle .run-empty-pretty{padding:14px 4px;gap:10px}
  .runner-grid.idle .run-empty-pretty .play-circle{width:36px;height:36px;font-size:16px}
  .runner-grid.idle .run-empty-pretty .hint-row{font-size:10px;flex-direction:column;
    gap:3px;writing-mode:horizontal-tb}
  .runner-grid.idle .run-empty-pretty .hint-row kbd{font-size:9.5px;padding:1px 4px}
  .runner-grid.idle .run-panel-head{
    flex-direction:column;padding:14px 6px;gap:10px;writing-mode:vertical-rl;
    text-orientation:upright;height:auto;background:transparent;border-bottom:none}
  .runner-grid.idle .run-panel-head .meta-tail{display:none}
  .workspace{min-width:0;display:flex;flex-direction:column;gap:18px}
  .run-panel{
    position:sticky;top:76px;
    max-height:calc(100vh - 96px);
    background:var(--surface);border:1px solid var(--line);
    border-radius:14px;overflow:hidden;
    display:flex;flex-direction:column;transition:none}
  .run-panel-head{
    display:flex;align-items:center;gap:10px;
    padding:16px 20px;border-bottom:1px solid var(--line);
    background:var(--surface-2);
    font-size:11px;color:var(--fg-3);
    text-transform:uppercase;letter-spacing:.08em;font-weight:600}
  .run-panel-head .live-dot{
    width:8px;height:8px;border-radius:50%;background:var(--fg-4);flex-shrink:0}
  .run-panel-head .live-dot.queued{background:var(--fg-3)}
  .run-panel-head .live-dot.running{background:var(--running);
    box-shadow:0 0 0 3px rgba(167,139,250,.18);
    animation:dot-pulse 1.4s ease-in-out infinite}
  .run-panel-head .live-dot.succeeded{background:var(--ok)}
  .run-panel-head .live-dot.failed{background:var(--bad)}
  @keyframes dot-pulse{
    0%,100%{transform:scale(1);box-shadow:0 0 0 3px rgba(167,139,250,.18)}
    50%{transform:scale(1.15);box-shadow:0 0 0 6px rgba(167,139,250,.10)}
  }
  .run-panel-head .label-text{color:var(--fg-2)}
  .run-panel-head .meta-tail{margin-left:auto;font-family:"SF Mono",ui-monospace,monospace;
    font-size:10.5px;color:var(--fg-3);text-transform:none;letter-spacing:0}
  .run-panel-body{flex:1;overflow-y:auto;padding:0}
  /* Idle/empty state inside panel */
  #run-area .run-empty-pretty{
    padding:48px 28px;text-align:center;
    display:flex;flex-direction:column;align-items:center;gap:14px}
  #run-area .run-empty-pretty .play-circle{
    width:72px;height:72px;border-radius:50%;
    background:linear-gradient(135deg,var(--ac-bg),transparent);
    border:1px solid var(--ac-line);
    display:grid;place-items:center;
    color:var(--ac-2);font-size:24px;
    box-shadow:inset 0 1px 0 rgba(255,255,255,.04)}
  #run-area .run-empty-pretty .title{
    font-size:16px;font-weight:600;color:var(--fg);margin:0}
  #run-area .run-empty-pretty .desc{
    font-size:13px;color:var(--fg-2);line-height:1.55;margin:0;max-width:280px}
  #run-area .run-empty-pretty .hint-row{
    display:flex;align-items:center;gap:8px;
    font-family:"SF Mono",ui-monospace,monospace;
    font-size:11px;color:var(--fg-3);margin-top:8px}
  #run-area .run-empty-pretty kbd{
    background:var(--surface-3);border:1px solid var(--line-2);
    border-bottom-width:2px;
    padding:2px 7px;border-radius:5px;font-size:10.5px;color:var(--fg-2);
    font-family:"SF Mono",ui-monospace,monospace;font-weight:600}
  /* Responsive: stack on narrow viewport */
  @media (max-width:1080px){
    .runner-grid{grid-template-columns:1fr}
    .run-panel{position:static;max-height:none}
  }

  /* Section card */
  .sec{background:var(--surface);border:1px solid var(--line);
    border-radius:14px;overflow:hidden;
    transition:border-color .15s,box-shadow .25s}
  .sec:hover{border-color:var(--line-2)}
  .sec-head{display:flex;align-items:center;gap:14px;padding:18px 22px;
    border-bottom:1px solid var(--line);background:var(--surface-2);
    position:relative}
  .sec-head .num{
    width:22px;height:22px;border-radius:50%;flex-shrink:0;
    display:grid;place-items:center;
    font-family:var(--mono);font-size:11px;font-weight:600;
    color:var(--bg);background:var(--fg-2);border:none}
  .sec-head h3{margin:0;font-size:14px;font-weight:600;letter-spacing:.02em;color:var(--fg-3);
    text-transform:uppercase}
  .sec-head .sub{margin-left:auto;font-size:12.5px;color:var(--fg-3);max-width:50%;
    text-align:right;line-height:1.45}
  /* 折叠式填写指引 — 标题右侧按钮,点开展示 hint */
  .hint-toggle{margin-left:auto;background:transparent;border:1px solid var(--line);
    color:var(--fg-3);padding:4px 10px;border-radius:5px;
    font-size:11.5px;cursor:pointer;font-family:var(--mono);
    transition:border-color .12s,color .12s}
  .hint-toggle:hover{border-color:var(--ac-line);color:var(--ac)}
  .hint-toggle.open{color:var(--ac);border-color:var(--ac-line);background:var(--ac-bg)}
  .hint-panel{display:none;padding:0 22px;margin:0 0 -4px}
  .hint-panel.open{display:block}
  .hint-body{padding:12px 14px;background:var(--surface-2);border-radius:6px;
    border-left:3px solid var(--ac-line);color:var(--fg-2);font-size:12.5px;
    line-height:1.7;white-space:pre-wrap;font-family:var(--sans)}
  .sec-body{padding:20px 22px}

  /* Section 1: Inputs (single textarea + upload + drag-drop) */
  .input-zone{position:relative;border-radius:8px;transition:all .15s}
  .input-zone.dragging{outline:2px dashed var(--ac);outline-offset:4px}
  .input-zone .drop-overlay{
    position:absolute;inset:0;
    background:rgba(7,8,10,.85);backdrop-filter:blur(4px);
    border:2px dashed var(--ac);border-radius:8px;
    display:none;flex-direction:column;align-items:center;justify-content:center;
    z-index:10;pointer-events:none;
  }
  .input-zone.dragging .drop-overlay{display:flex}
  .drop-overlay-inner{display:flex;flex-direction:column;align-items:center;gap:8px}
  .drop-icon{font-size:42px;color:var(--ac);line-height:1;animation:bounce 1.4s ease-in-out infinite}
  @keyframes bounce{0%,100%{transform:translateY(0)}50%{transform:translateY(-6px)}}
  .drop-text{color:var(--fg);font-size:14px;font-weight:500}
  .drop-sub{color:var(--fg-3);font-size:11.5px;font-family:var(--mono)}
  .input-toolbar{display:flex;gap:8px;align-items:center;margin-bottom:10px}
  .input-toolbar button{background:transparent;border:1px solid var(--line-2);
    color:var(--fg-2);padding:0 14px;height:32px;border-radius:6px;
    font-family:var(--mono);font-size:12px;cursor:pointer;white-space:nowrap;
    transition:all .15s}
  .input-toolbar button:hover{border-color:var(--ac);color:var(--ac)}
  .input-toolbar button:disabled{opacity:.5;cursor:wait}
  .input-toolbar .drag-hint{font-family:var(--mono);font-size:11px;color:var(--fg-3);
    padding-left:4px;display:flex;align-items:center;gap:4px}
  .input-toolbar .drag-hint::before{content:"⇲";color:var(--fg-4)}
  .input-toolbar .size-hint{font-family:var(--mono);font-size:11px;color:var(--fg-3);
    padding:0 4px}
  #doc-input{
    background:var(--surface-2);border:1px solid var(--line);border-radius:6px;
    color:var(--fg);font-family:var(--mono);font-size:12.5px;padding:12px 14px;
    resize:vertical;min-height:240px;line-height:1.55;width:100%;
    transition:border-color .15s}
  #doc-input:focus{outline:none;border-color:var(--ac);background:var(--bg)}
  #doc-input::placeholder{color:var(--fg-4)}
  .run-options{display:flex;gap:14px;margin-top:10px;flex-wrap:wrap}
  .run-options label{display:inline-flex;align-items:center;gap:8px;
    background:var(--surface-2);border:1px solid var(--line);border-radius:6px;
    padding:8px 12px;font-size:12.5px;color:var(--fg-2);cursor:pointer}
  .run-options label input{width:14px;height:14px;accent-color:var(--ac);cursor:pointer}

  /* Section 2: Prompts (collapsible + editable + optional check) */
  .prompt-row{border-bottom:1px solid var(--line);transition:opacity .15s}
  .prompt-row:last-child{border-bottom:none}
  .prompt-row.disabled{opacity:.45}
  .prompt-row.disabled .prompt-head{cursor:default}
  .prompt-head{display:flex;align-items:center;gap:12px;padding:10px 18px;
    cursor:pointer;font-size:13px;transition:background .12s}
  .prompt-head:hover{background:var(--surface-2)}
  .prompt-head .check{accent-color:var(--ac);width:14px;height:14px;cursor:pointer;
    flex-shrink:0;margin:0}
  .prompt-head .check:disabled{cursor:not-allowed;accent-color:var(--fg-4)}
  .prompt-head .twirl{color:var(--fg-4);font-family:var(--mono);font-size:9px;
    transition:transform .15s;flex-shrink:0;width:10px}
  .prompt-row.open .prompt-head .twirl{transform:rotate(90deg);color:var(--fg-3)}
  .prompt-head .id{font-family:var(--mono);color:var(--fg-3);min-width:64px;font-size:11.5px}
  .prompt-head .name{color:var(--fg)}
  .prompt-row.disabled .id{color:var(--fg-4)}
  .prompt-row.disabled .name{color:var(--fg-3)}
  /* chip 极简:无底色,只灰字 */
  .prompt-head .chip{font-family:var(--mono);font-size:11px;padding:0;
    border-radius:0;background:transparent;border:none;
    color:var(--fg-4);margin-left:auto;letter-spacing:.02em;text-transform:uppercase}
  .prompt-head .chip.dirty{color:var(--warn);background:transparent;border:none}
  .prompt-head .chip.override{color:var(--ac);background:transparent;border:none}
  .prompt-body{display:none;padding:10px 18px 14px 18px;background:transparent;
    border-top:1px dashed var(--line)}
  .prompt-row.open .prompt-body{display:block}
  .prompt-body textarea{
    background:var(--bg);border:1px solid var(--line);border-radius:6px;
    color:var(--fg);font-family:var(--mono);font-size:12px;padding:11px 13px;
    resize:vertical;min-height:200px;line-height:1.62;width:100%}
  .prompt-body textarea:focus{outline:none;border-color:var(--ac)}
  .prompt-body .btn-row{display:flex;gap:8px;margin-top:8px;align-items:center}
  .prompt-body .btn-row button{
    background:transparent;border:1px solid var(--line-2);color:var(--fg-2);
    padding:5px 12px;border-radius:5px;font-family:var(--mono);font-size:11.5px;cursor:pointer}
  .prompt-body .btn-row button.save{background:var(--ac);color:#001a14;
    border-color:var(--ac);font-weight:600}
  .prompt-body .btn-row button.save:hover{background:var(--ac-2)}
  .prompt-body .btn-row button:hover:not(.save){border-color:var(--ac);color:var(--ac)}
  .prompt-body .btn-row .info{margin-left:auto;font-family:var(--mono);font-size:11px;color:var(--fg-3)}

  /* Section 3: Model & precision */
  .model-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
  .model-cell{background:var(--surface-2);border:1px solid var(--line);border-radius:8px;
    padding:14px}
  .model-cell .label{font-size:11.5px;color:var(--fg-3);text-transform:uppercase;
    letter-spacing:.1em;margin-bottom:8px;font-weight:600}
  .model-cell .label .from{font-family:var(--mono);font-size:10px;color:var(--fg-4);
    text-transform:none;letter-spacing:0;font-weight:400;margin-left:6px}
  .model-cell select{
    width:100%;background:var(--bg);border:1px solid var(--line-2);border-radius:6px;
    color:var(--fg);font-family:var(--mono);font-size:13px;padding:8px 10px;cursor:pointer}
  .model-cell select:focus{outline:none;border-color:var(--ac)}
  .model-cell select option.legacy{color:var(--fg-3);font-style:italic}
  .model-cell .hint{margin-top:6px;font-size:11px;color:var(--fg-3);font-family:var(--mono)}
  .model-cell.unsupported{background:rgba(196,90,58,.06);
    border:1px dashed rgba(16,185,129,.25)}
  .model-cell.unsupported .label{color:var(--fg-2)}
  .model-cell.unsupported .hint{color:var(--fg-2);font-family:var(--sans);font-size:12px;line-height:1.55}
  .claude-status{display:flex;align-items:center;gap:8px;font-family:var(--mono);font-size:11.5px;
    margin-top:14px;padding:10px 12px;background:var(--surface-2);border-radius:6px;
    border:1px solid var(--line)}
  .claude-status.ok{border-color:rgba(74,222,128,.3);background:rgba(74,222,128,.04)}
  .claude-status.bad{border-color:rgba(248,113,113,.3);background:rgba(248,113,113,.04)}
  .claude-status .dot{width:8px;height:8px;border-radius:50%;background:var(--fg-4);flex-shrink:0}
  .claude-status.ok .dot{background:var(--ok)}
  .claude-status.bad .dot{background:var(--bad)}
  .claude-status .v{color:var(--fg-2)}
  .claude-status a{color:var(--ac);text-decoration:none;margin-left:auto}

  /* Section 4: Run + report */
  .run-empty{padding:40px;text-align:center;color:var(--fg-3);font-size:13px;
    font-family:var(--mono);border:1px dashed var(--line);border-radius:8px}
  .step-list{display:flex;flex-direction:column}
  .step-row{display:grid;grid-template-columns:24px 1fr auto;gap:10px;padding:10px 16px;
    border-bottom:1px solid var(--line);align-items:center;font-size:12.5px}
  .step-row:last-child{border-bottom:none}
  .step-row .marker{font-family:var(--mono);font-size:11px;color:var(--fg-4);text-align:center}
  .step-row.done .marker{color:var(--ok)}
  .step-row.running .marker{color:var(--ac);animation:pulse 1.4s infinite}
  .step-row.failed .marker{color:var(--bad)}
  .step-row .name{color:var(--fg-2);font-family:var(--mono);font-size:12px}
  .step-row.done .name{color:var(--fg)}
  .step-row .info{font-family:var(--mono);font-size:11px;color:var(--fg-3)}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}

  .run-stats{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;padding:14px 16px;
    border-bottom:1px solid var(--line);background:var(--surface-2)}
  .run-stats .cell{text-align:center}
  .run-stats .cell .v{font-family:var(--mono);font-size:14px;color:var(--ac);font-weight:600}
  .run-stats .cell .l{font-size:10px;color:var(--fg-3);text-transform:uppercase;letter-spacing:.08em;margin-top:2px}

  .gate-banner{padding:14px 18px;border-bottom:1px solid var(--line);display:flex;
    align-items:flex-start;gap:12px;font-size:13px;line-height:1.55}
  .gate-banner.proceed{background:rgba(74,222,128,.05)}
  .gate-banner.reject{background:rgba(248,113,113,.05)}
  .gate-banner.warn{background:rgba(251,191,36,.05)}
  .gate-banner .badge{padding:3px 9px;border-radius:4px;font-family:var(--mono);font-size:11px;
    font-weight:600;text-transform:uppercase;letter-spacing:.05em;flex-shrink:0;margin-top:1px}
  .gate-banner.proceed .badge{background:rgba(74,222,128,.12);color:var(--ok)}
  .gate-banner.reject .badge{background:rgba(248,113,113,.12);color:var(--bad)}
  .gate-banner.warn .badge{background:rgba(251,191,36,.12);color:var(--warn)}
  .gate-banner .reasons{color:var(--fg-2);font-family:var(--mono);font-size:12px}
  .gate-banner .reasons div{margin-top:3px}

  .json-view{padding:14px 16px;background:var(--bg);max-height:560px;overflow:auto}
  .json-view pre{margin:0;font-family:var(--mono);font-size:11.5px;line-height:1.65;
    color:var(--fg);white-space:pre-wrap;word-break:break-word}
  /* Real-time log panel */
  .log-panel{
    background:var(--bg);border-bottom:1px solid var(--line);
    max-height:280px;overflow-y:auto;padding:10px 14px;font-family:var(--mono);
    font-size:11.5px;line-height:1.5;color:var(--fg-2)}
  .log-panel-head{display:flex;align-items:center;gap:8px;padding:8px 16px 6px;
    font-family:var(--mono);font-size:10.5px;color:var(--fg-3);
    text-transform:uppercase;letter-spacing:.1em;font-weight:600;
    border-bottom:1px solid var(--line);background:var(--surface-2)}
  .log-panel-head .live{display:inline-flex;align-items:center;gap:5px}
  .log-panel-head .live .dot{width:6px;height:6px;border-radius:50%;background:var(--ac);
    animation:pulse 1.4s ease-in-out infinite}
  .log-panel-head .count{margin-left:auto;color:var(--fg-2)}
  .log-line{display:grid;grid-template-columns:64px 110px 1fr;gap:10px;
    padding:3px 0;align-items:start}
  .log-line .ts{color:var(--fg-4);font-size:10.5px}
  .log-line .ev{font-weight:600;font-size:10.5px;text-transform:uppercase;letter-spacing:.05em}
  .log-line .ev.run-start, .log-line .ev.substep-start{color:var(--ac-2)}
  .log-line .ev.substep-done, .log-line .ev.run-done{color:var(--ok)}
  .log-line .ev.llm-call{color:var(--fg-3)}
  .log-line .ev.substep-parse-failed{color:var(--warn)}
  .log-line .ev.substep-skipped{color:var(--fg-4)}
  .log-line .ev.run-failed{color:var(--bad)}
  .log-line .ev.llm-thinking{color:#c084fc}     /* purple — extended thinking */
  .log-line .ev.llm-thinking-final{color:#c084fc;font-weight:700}
  .log-line .ev.llm-text{color:var(--fg-2)}     /* normal output streaming */
  .log-line .ev.llm-text-final{color:var(--ac);font-weight:700}
  /* Stream lines: dim italic for the tail snippet */
  .log-line.stream .msg .v{color:var(--fg-2);font-style:normal}
  .log-line.stream .msg .tail{display:block;margin-top:2px;padding:4px 8px;
    background:rgba(192,132,252,.06);border-left:2px solid #c084fc;
    color:var(--fg-2);font-size:11px;line-height:1.5;
    max-height:60px;overflow:hidden;text-overflow:ellipsis;
    white-space:pre-wrap;word-break:break-word}
  .log-line.stream.text .msg .tail{background:rgba(16,185,129,.05);border-left-color:var(--ac)}
  .log-line .msg{color:var(--fg-2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .log-line .msg .k{color:var(--fg-3)}
  .log-line .msg .v{color:var(--fg)}
  .log-line .msg .v.ac{color:var(--ac)}

  /* Report view (post-run, structured) */
  .report-tabs{display:flex;gap:0;background:var(--surface-2);
    border-bottom:1px solid var(--line);padding:0 16px}
  .report-tabs button{background:transparent;border:none;color:var(--fg-3);
    padding:10px 16px;cursor:pointer;font-size:12px;font-family:var(--sans);
    border-bottom:2px solid transparent;transition:all .15s}
  .report-tabs button:hover{color:var(--fg-2)}
  .report-tabs button.active{color:var(--ac);border-bottom-color:var(--ac)}
  .report-tabs .spacer{flex:1}
  .report-tabs .export{padding:6px 12px;margin:auto 4px;background:transparent;
    border:1px solid var(--line-2);color:var(--fg-2);font-size:11px;
    border-radius:5px;cursor:pointer;font-family:var(--mono)}
  .report-tabs .export:hover{border-color:var(--ac);color:var(--ac)}

  .report-hero{display:flex;align-items:flex-start;gap:14px;padding:16px 20px;
    border-bottom:1px solid var(--line);background:var(--surface)}
  .report-hero .report-icon{font-size:22px;width:40px;height:40px;border-radius:8px;
    background:var(--surface-2);border:1px solid var(--line-2);
    display:grid;place-items:center;flex-shrink:0}
  /* === 执行摘要 — 卡片化、有层次 === */
  .exec-block{background:var(--surface);border:1px solid var(--line);border-radius:10px;
    padding:18px 22px;margin:0 22px 16px}
  .exec-block:last-of-type{margin-bottom:22px}
  .exec-head{display:flex;align-items:center;gap:12px;margin-bottom:14px;padding-bottom:12px;
    border-bottom:1px solid var(--line)}
  .exec-head h3{margin:0;font-size:13px;font-weight:700;color:var(--fg);
    text-transform:uppercase;letter-spacing:.08em}
  .exec-num{display:inline-grid;place-items:center;width:24px;height:24px;border-radius:50%;
    background:var(--fg);color:var(--bg);font-size:11.5px;font-weight:700;
    border:none;font-family:var(--mono);flex-shrink:0}
  /* verdict — 醒目左色条 + 浅底色 */
  .verdict{padding:14px 18px;background:var(--surface-2);border:1px solid var(--line);
    border-left:4px solid var(--line-2);border-radius:0 8px 8px 0;
    display:flex;align-items:center;gap:12px;font-size:20px;font-weight:700;letter-spacing:-.01em}
  .verdict.ok{color:var(--ok);border-left-color:var(--ok);background:rgba(22,163,74,.06)}
  .verdict.warn{color:var(--warn);border-left-color:var(--warn);background:rgba(202,138,4,.06)}
  .verdict.bad{color:var(--bad);border-left-color:var(--bad);background:rgba(220,38,38,.06)}
  .verdict.skip{color:var(--fg-3)}
  .verdict-icon{font-size:18px;display:inline-block}
  .verdict-summary{margin-top:12px;padding:12px 16px;background:var(--bg);border:1px solid var(--line);
    border-radius:6px;font-size:14px;line-height:1.7;color:var(--fg)}
  .sev-strip{margin-top:12px;padding:10px 14px;background:var(--bg);border:1px solid var(--line);
    border-radius:6px;font-family:var(--mono);font-size:12.5px;color:var(--fg-2);letter-spacing:.02em}
  .sev-strip strong{color:var(--fg);font-weight:700}
  .exec-muted{color:var(--fg-3);font-size:13.5px;margin:0;padding:10px 14px;
    background:var(--bg);border:1px dashed var(--line);border-radius:6px;font-style:normal}
  .exec-head .exec-count{margin-left:auto;font-family:var(--mono);font-size:12px;font-weight:600;
    color:var(--fg-2);background:var(--surface-2);border:1px solid var(--line);
    padding:3px 10px;border-radius:999px;text-transform:none;letter-spacing:0}
  .exec-head .exec-count.danger{color:#fff;background:var(--bad);border-color:var(--bad)}
  .exec-head .exec-count-note{font-size:11.5px;color:var(--fg-3);font-style:normal;
    font-weight:500;text-transform:none;letter-spacing:0;margin-left:6px}
  /* KPI 卡片 — 大字 + 卡边 */
  .exec-kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:14px}
  .exec-kpi{display:flex;flex-direction:column;align-items:flex-start;justify-content:center;
    padding:16px 18px;background:var(--bg);border:1px solid var(--line);border-radius:8px}
  .exec-kpi-num{font-size:30px;font-weight:700;color:var(--fg);font-family:var(--sans);
    font-variant-numeric:tabular-nums;line-height:1;letter-spacing:-.025em}
  .exec-kpi-lbl{font-size:11.5px;color:var(--fg-2);margin-top:8px;letter-spacing:.08em;
    text-transform:uppercase;font-weight:600}
  /* 严重度分布条 — 加厚到 10px 可见 */
  .sev-bar{display:flex;height:10px;border-radius:5px;overflow:hidden;margin-top:14px;
    background:var(--surface-2);border:1px solid var(--line)}
  .sev-bar-seg{display:flex;align-items:center;justify-content:center;color:#fff;font-size:0;
    line-height:0;min-width:0;transition:flex .2s}
  .sev-bar-critical{background:#dc2626}
  .sev-bar-high{background:#ea580c}
  .sev-bar-medium{background:#ca8a04}
  .sev-bar-low{background:#16a34a}
  .sev-bar-info{background:#0891b2}
  /* 优先级分布条 — 24px 大字带文字 */
  .pri-bar{display:flex;height:24px;border-radius:6px;overflow:hidden;margin-top:8px;
    margin-bottom:14px;background:var(--surface-2);border:1px solid var(--line)}
  .pri-bar-seg{display:flex;align-items:center;justify-content:center;color:#fff;
    font-size:11.5px;font-weight:700;padding:0 6px;min-width:0;
    font-family:var(--sans);letter-spacing:.04em}
  .pri-bar-P0{background:#dc2626}
  .pri-bar-P1{background:#ea580c}
  .pri-bar-P2{background:#ca8a04}
  .pri-bar-P3{background:#737373}
  /* sev-tag — 带底色的小药丸 */
  .sev-tag{font-size:10.5px;font-weight:700;padding:2px 8px;background:var(--surface-2);
    color:var(--fg-2);font-family:var(--sans);letter-spacing:.06em;
    white-space:nowrap;flex-shrink:0;text-transform:uppercase;border-radius:4px;border:1px solid var(--line)}
  .sev-tag.sev-critical{color:#dc2626;background:rgba(220,38,38,.10);border-color:rgba(220,38,38,.30)}
  .sev-tag.sev-high{color:#ea580c;background:rgba(234,88,12,.10);border-color:rgba(234,88,12,.30)}
  .sev-tag.sev-medium{color:#ca8a04;background:rgba(202,138,4,.10);border-color:rgba(202,138,4,.30)}
  .sev-tag.sev-low{color:#16a34a;background:rgba(22,163,74,.10);border-color:rgba(22,163,74,.30)}
  .sev-tag.sev-info{color:#0891b2;background:rgba(8,145,178,.10);border-color:rgba(8,145,178,.30)}
  /* 优先级标签 — 带底色 */
  .pri-tag,.meta-chip.pri-P0,.meta-chip.pri-P1,.meta-chip.pri-P2,.meta-chip.pri-P3{
    font-size:10.5px;font-weight:700;padding:2px 8px;border-radius:4px;
    background:var(--surface-2);color:var(--fg-2);font-family:var(--sans);
    letter-spacing:.04em;white-space:nowrap;border:1px solid var(--line)}
  .pri-tag.pri-P0,.meta-chip.pri-P0{color:#dc2626;background:rgba(220,38,38,.10);border-color:rgba(220,38,38,.30)}
  .pri-tag.pri-P1,.meta-chip.pri-P1{color:#ea580c;background:rgba(234,88,12,.10);border-color:rgba(234,88,12,.30)}
  .pri-tag.pri-P2,.meta-chip.pri-P2{color:#ca8a04;background:rgba(202,138,4,.10);border-color:rgba(202,138,4,.30)}
  .pri-tag.pri-P3,.meta-chip.pri-P3{color:#525252;background:var(--surface-2);border-color:var(--line)}
  /* 风险卡 — 左侧黄条 */
  .exec-risk-list{display:flex;flex-direction:column;gap:10px;margin-top:4px}
  .exec-risk-item{padding:12px 16px;background:var(--bg);border:1px solid var(--line);
    border-left:4px solid #ca8a04;border-radius:0 6px 6px 0;
    transition:transform .12s ease,box-shadow .12s ease}
  .exec-risk-item:hover{transform:translateX(2px);box-shadow:0 2px 8px rgba(0,0,0,.06)}
  .exec-risk-head{display:flex;align-items:baseline;gap:10px;margin-bottom:6px;flex-wrap:wrap}
  .exec-risk-title{font-weight:700;color:var(--fg);font-size:14.5px;line-height:1.5;flex:1;min-width:0}
  .exec-risk-line{font-size:13px;color:var(--fg-2);line-height:1.7;margin-top:4px;
    display:flex;align-items:baseline;gap:8px}
  .exec-risk-line .lbl{display:inline-block;min-width:48px;font-size:10.5px;color:var(--fg-3);
    font-weight:700;letter-spacing:.06em;text-transform:uppercase;flex-shrink:0;
    background:var(--surface-2);padding:2px 6px;border-radius:3px}
  /* 阻碍卡 — 红色强调 */
  .exec-blocker-list{display:flex;flex-direction:column;gap:10px;margin-top:4px}
  .exec-blocker-item{padding:14px 16px;background:rgba(220,38,38,.04);
    border:1px solid rgba(220,38,38,.20);border-left:4px solid #dc2626;border-radius:0 6px 6px 0;
    transition:transform .12s ease,box-shadow .12s ease}
  .exec-blocker-item:hover{transform:translateX(2px);box-shadow:0 2px 8px rgba(220,38,38,.10)}
  .exec-blocker-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:6px}
  .blocker-tag{font-size:10px;font-weight:700;padding:2px 8px;background:#dc2626;color:#fff;
    letter-spacing:.10em;text-transform:uppercase;border-radius:3px}
  .exec-blocker-title{font-weight:700;color:var(--fg);font-size:14.5px;line-height:1.5;flex:1;min-width:0}
  .exec-blocker-line{font-size:13px;color:var(--fg-2);line-height:1.7;margin-top:4px;
    display:flex;align-items:baseline;gap:8px}
  .exec-blocker-line.fix{color:var(--fg)}
  .exec-blocker-line .lbl{display:inline-block;min-width:66px;font-size:10.5px;color:var(--fg-3);
    font-weight:700;letter-spacing:.06em;text-transform:uppercase;flex-shrink:0;
    background:var(--surface-2);padding:2px 6px;border-radius:3px}
  .exec-blocker-line.fix .lbl{color:#16a34a;background:rgba(22,163,74,.10)}
  /* Bug 卡 — 卡片化 + 左色条按严重度 */
  .exec-issue{border:1px solid var(--line);border-left:4px solid var(--line-2);
    border-radius:0 6px 6px 0;padding:14px 16px;margin-bottom:10px;
    background:var(--bg);transition:transform .12s ease,box-shadow .12s ease}
  .exec-issue:hover{transform:translateX(2px);box-shadow:0 2px 8px rgba(0,0,0,.06)}
  .exec-issue:last-of-type{margin-bottom:0}
  .exec-issue.sev-critical{border-left-color:#dc2626;background:rgba(220,38,38,.03)}
  .exec-issue.sev-high{border-left-color:#ea580c;background:rgba(234,88,12,.03)}
  .exec-issue.sev-medium{border-left-color:#ca8a04}
  .exec-issue.sev-low{border-left-color:#16a34a}
  .exec-issue.sev-info{border-left-color:#0891b2}
  .exec-issue-head{display:flex;align-items:baseline;gap:10px;margin-bottom:6px;flex-wrap:wrap}
  .exec-issue-title{font-weight:700;color:var(--fg);font-size:14.5px;flex:1;min-width:0;line-height:1.5}
  .exec-issue-loc{color:var(--fg-3);font-size:12px;margin:4px 0;font-family:var(--mono);
    padding:3px 8px;background:var(--surface-2);border-radius:4px;display:inline-block}
  .exec-issue-meta{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0 10px;
    font-size:11.5px;color:var(--fg-3);align-items:center}
  .exec-issue-meta .meta-chip{font-size:11px;font-family:var(--mono);font-weight:600;
    padding:2px 8px;background:var(--surface-2);border:1px solid var(--line);
    color:var(--fg-2);border-radius:4px}
  .exec-issue-meta .meta-chip.role{color:var(--fg);background:var(--surface-2);border-color:var(--line-2)}
  .exec-issue-section{margin:8px 0 0;padding:10px 12px;background:var(--surface-2);
    border:1px solid var(--line);border-radius:6px}
  .exec-issue-section.fix{background:rgba(22,163,74,.04);border-color:rgba(22,163,74,.20)}
  .exec-issue-section.verify{background:rgba(8,145,178,.04);border-color:rgba(8,145,178,.20)}
  .exec-issue-section .sec-body{padding-left:0;border-left:none}
  .sec-lbl{font-size:10.5px;font-weight:700;color:var(--fg-3);margin-bottom:6px;
    letter-spacing:.08em;text-transform:uppercase}
  .exec-issue-section.fix .sec-lbl{color:#16a34a}
  .exec-issue-section.verify .sec-lbl{color:#0891b2}
  .sec-body{font-size:13.5px;color:var(--fg);line-height:1.7}
  .repro-list{margin:0;padding-left:20px;font-size:13.5px;line-height:1.7;color:var(--fg)}
  .repro-list li{margin-bottom:3px}
  .accept-line{margin-top:6px;padding:10px 12px;background:rgba(8,145,178,.04);
    border:1px solid rgba(8,145,178,.20);border-radius:6px;font-size:13.5px;color:var(--fg);line-height:1.7}
  .accept-line::before{content:"验收: ";font-weight:700;color:#0891b2;font-size:11.5px;
    letter-spacing:.06em;text-transform:uppercase;margin-right:6px}
  .related-cases{margin-top:8px;font-size:12px;color:var(--fg-3);font-family:var(--mono);
    display:flex;flex-wrap:wrap;gap:6px;align-items:center}
  .related-cases::before{content:"关联用例";font-size:10.5px;font-weight:700;color:var(--fg-3);
    letter-spacing:.06em;text-transform:uppercase;font-family:var(--sans);margin-right:4px}
  .related-cases code{background:var(--surface-2);padding:2px 8px;color:var(--fg-2);
    font-size:11.5px;border:1px solid var(--line);border-radius:3px}
  .exec-issue-impact,.exec-issue-evidence{margin-top:6px;font-size:12.5px;color:var(--fg-2);
    padding:6px 10px;background:var(--surface-2);border:1px solid var(--line);border-radius:4px;line-height:1.6}
  .exec-issue-impact::before{content:"影响范围: ";font-weight:700;color:var(--fg-3);font-size:10.5px;
    letter-spacing:.06em;text-transform:uppercase}
  .exec-issue-evidence::before{content:"证据: ";font-weight:700;color:var(--fg-3);font-size:10.5px;
    letter-spacing:.06em;text-transform:uppercase}
  /* 用例表 — 加边框、斑马纹 */
  .case-table-wrap{margin-top:8px;border:1px solid var(--line);border-radius:8px;overflow-x:auto;
    background:var(--bg)}
  .case-table{width:100%;border-collapse:collapse;font-size:13.5px}
  .case-table th{padding:10px 12px;text-align:left;background:var(--surface-2);
    color:var(--fg);font-weight:700;font-size:11px;text-transform:uppercase;
    letter-spacing:.08em;border-bottom:1px solid var(--line);white-space:nowrap;position:sticky;top:0}
  .case-table td{padding:10px 12px;border-bottom:1px solid var(--line);
    vertical-align:top;color:var(--fg);font-size:13.5px;line-height:1.6}
  .case-table tr:last-child td{border-bottom:none}
  .case-table tr:nth-child(even) td{background:var(--surface-2)}
  .case-table tr.pri-P0 td{background:rgba(220,38,38,.04)}
  .case-table tr.pri-P1 td{background:rgba(234,88,12,.03)}
  .case-table tr:hover td{background:rgba(0,0,0,.02)}
  .case-table .case-idx{font-family:var(--mono);color:var(--fg-3);font-size:12px;width:36px;font-weight:600}
  .case-table .case-id{background:var(--surface-2);padding:2px 8px;color:var(--fg);
    font-size:12px;font-family:var(--mono);border:1px solid var(--line);border-radius:4px;display:inline-block}
  .case-table .case-title{max-width:420px;line-height:1.55;color:var(--fg);font-size:14px;font-weight:500}
  .case-type,.case-auto{font-size:11px;padding:2px 8px;border-radius:4px;font-weight:600;
    background:var(--surface-2);color:var(--fg-2);font-family:var(--sans);white-space:nowrap;
    border:1px solid var(--line);display:inline-block}
  .case-status{font-size:12px;font-family:var(--sans);font-weight:600;white-space:nowrap;
    color:var(--fg-3);padding:2px 8px;background:var(--surface-2);border:1px solid var(--line);
    border-radius:4px;display:inline-block}
  .case-status-ok{color:#16a34a;background:rgba(22,163,74,.10);border-color:rgba(22,163,74,.30)}
  .case-status-bad{color:#dc2626;background:rgba(220,38,38,.10);border-color:rgba(220,38,38,.30)}
  .case-status-muted{color:var(--fg-3)}
  .exec-case-note{margin-top:12px;padding:10px 14px;background:var(--surface-2);
    border:1px solid var(--line);border-left:3px solid var(--ac-line);
    border-radius:0 6px 6px 0;color:var(--fg-2);font-size:13px;line-height:1.65}
  /* === 子步骤原始输出区(默认折叠) === */
  .substep-section-head{display:flex;align-items:baseline;gap:10px;
    margin:0 24px 8px;padding:16px 0 6px 0;border-top:1px solid var(--line)}
  .substep-section-title{font-size:12px;font-weight:600;color:var(--fg-3);letter-spacing:.08em;
    text-transform:uppercase}
  .substep-section-hint{font-size:11px;color:var(--fg-4);font-style:normal;margin-left:auto}
  .report-sub{margin:0 24px}
  .report-sub-head{cursor:pointer;user-select:none}
  .report-sub-head:hover{background:transparent}
  .report-sub-twirl{display:inline-block;width:12px;color:var(--fg-3);font-size:10px;
    transition:transform .15s ease}
  .report-sub.open .report-sub-twirl{transform:rotate(90deg)}
  /* KPI 网格在窄屏自适应 */
  @media (max-width: 720px){
    .exec-kpis{grid-template-columns:repeat(2,1fr);gap:14px}
    .exec-block{padding:0 16px 16px}
    .substep-section-head{margin:0 16px 8px}
    .report-sub{margin:0 16px}
  }
  /* === Screenshot evidence === */
  .report-screenshots{margin:14px 18px 0;padding:14px;background:var(--surface-2);
    border:1px solid var(--line);border-radius:10px}
  .screenshots-head{font-size:13px;font-weight:600;color:var(--fg);margin-bottom:10px}
  .screenshots-hint{font-size:10.5px;font-weight:400;color:var(--fg-3);margin-left:6px;font-style:italic}
  .shot-group{margin-top:10px;padding-top:10px;border-top:1px dashed var(--line)}
  .shot-group:first-of-type{margin-top:0;padding-top:0;border-top:none}
  .shot-url{font-family:"SF Mono",ui-monospace,monospace;font-size:11.5px;color:var(--fg-3);margin-bottom:8px}
  .shot-url code{background:var(--bg);padding:2px 8px;border-radius:4px;color:var(--ac-2)}
  .shot-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px}
  .shot-cell{display:block;background:var(--bg);border:1px solid var(--line);border-radius:8px;
    overflow:hidden;text-decoration:none;color:var(--fg-2);
    transition:transform .15s,border-color .15s,box-shadow .15s}
  .shot-cell:hover{transform:translateY(-2px);border-color:var(--ac-line);
    box-shadow:0 8px 22px rgba(0,0,0,.35)}
  .shot-cell img{display:block;width:100%;height:auto;max-height:240px;object-fit:cover;background:#000}
  .shot-cap{padding:6px 10px;font-size:11px;font-family:"SF Mono",ui-monospace,monospace;
    color:var(--fg-3);border-top:1px solid var(--line);
    display:flex;align-items:center;justify-content:space-between;gap:6px}
  .shot-cap .issue-badge{background:rgba(220,38,38,.14);color:#dc2626;
    padding:1px 7px;border-radius:999px;font-weight:600;font-size:10px}
  .report-hero h4{margin:0;font-size:15px;font-weight:600;letter-spacing:-.005em;color:var(--fg)}
  .report-hero .meta{font-family:var(--mono);font-size:11px;color:var(--fg-3);margin-top:4px}
  .report-hero .meta code{background:var(--bg);padding:1px 6px;border-radius:3px;
    color:var(--ac-2);border:1px solid var(--line)}
  .report-project{margin-top:8px;display:flex;flex-wrap:wrap;gap:14px;
    padding:6px 0 0;border-top:1px dashed var(--line);font-size:12.5px;
    align-items:center}
  .report-project .lbl{color:var(--fg-3);font-size:10.5px;letter-spacing:.04em;
    text-transform:uppercase;font-weight:500;margin-right:2px}
  .report-project code{background:var(--surface-2);padding:2px 7px;border-radius:4px;
    color:var(--fg);font-family:var(--mono);font-size:12px}
  .report-project .val{color:var(--fg);font-weight:500}
  .report-hero .stat-pills{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
  .report-hero .stat-pills .pill{font-family:var(--mono);font-size:10.5px;
    padding:2px 8px;border-radius:3px;background:var(--surface-2);border:1px solid var(--line);
    color:var(--fg-2)}
  .report-hero .stat-pills .pill .v{color:var(--ac);font-weight:600}

  .report-sub{border-bottom:1px solid var(--line)}
  .report-sub:last-child{border-bottom:none}
  .report-sub-head{display:flex;align-items:center;gap:10px;padding:10px 18px;
    cursor:pointer;font-size:12.5px;background:var(--surface);
    transition:background .15s}
  .report-sub-head:hover{background:var(--surface-2)}
  .report-sub.open .report-sub-head{background:var(--surface-2)}
  .report-sub-twirl{color:var(--fg-3);font-family:var(--mono);font-size:10px;
    width:10px;transition:transform .15s}
  .report-sub.open .report-sub-twirl{transform:rotate(90deg);color:var(--ac)}
  .report-sub-num{
    font-family:var(--mono);font-size:10.5px;font-weight:600;color:var(--ac);
    background:rgba(16,185,129,.10);border:1px solid rgba(16,185,129,.25);
    width:20px;height:20px;border-radius:50%;display:inline-grid;place-items:center;flex-shrink:0}
  .report-sub-name{color:var(--fg);font-weight:500}
  .report-sub-stats{margin-left:auto;font-family:var(--mono);font-size:11px;color:var(--fg-3);
    display:flex;gap:8px}
  .report-sub-stats .chip{padding:2px 7px;border-radius:3px;background:var(--surface-2);
    border:1px solid var(--line-2)}
  .report-sub-body{display:none;padding:14px 20px;background:var(--bg)}
  .report-sub.open .report-sub-body{display:block}

  /* Smart renderer outputs */
  .report-kv{display:grid;grid-template-columns:max-content 1fr;gap:6px 16px;margin:0;font-size:12.5px}
  .report-kv dt{color:var(--fg-3);font-family:var(--mono);font-size:11.5px;align-self:start;
    padding-top:1px}
  .report-kv dd{margin:0;color:var(--fg);min-width:0;line-height:1.55}
  .report-kv dd.muted{color:var(--fg-3)}

  .report-table{width:100%;border-collapse:collapse;font-size:11.5px;margin:8px 0;
    border:1px solid var(--line);border-radius:6px;overflow:hidden}
  .report-table th{font-family:var(--mono);font-size:10px;text-align:left;
    padding:8px 10px;background:var(--surface-2);color:var(--fg-3);
    text-transform:uppercase;letter-spacing:.07em;font-weight:600;
    border-bottom:1px solid var(--line);white-space:nowrap}
  .report-table td{padding:8px 10px;border-bottom:1px solid var(--line);
    font-family:var(--mono);font-size:11px;color:var(--fg);vertical-align:top;
    word-break:break-word;max-width:320px}
  .report-table tr:last-child td{border-bottom:none}
  .report-table tr:hover td{background:var(--surface-2)}
  .report-table tfoot td{font-family:var(--mono);color:var(--fg-3);font-style:italic;
    text-align:center;font-size:10.5px;background:var(--surface-2)}

  .sev{font-family:var(--mono);font-size:9.5px;font-weight:700;padding:1px 6px;
    border-radius:3px;text-transform:uppercase;letter-spacing:.05em;display:inline-block;
    line-height:1.5}
  .sev-critical{background:rgba(248,113,113,.18);color:var(--bad);
    border:1px solid rgba(248,113,113,.4)}
  .sev-high{background:rgba(248,113,113,.10);color:var(--bad)}
  .sev-major{background:rgba(248,113,113,.10);color:var(--bad)}
  .sev-medium{background:rgba(251,191,36,.10);color:var(--warn)}
  .sev-warn{background:rgba(251,191,36,.10);color:var(--warn)}
  .sev-low{background:rgba(168,174,184,.10);color:var(--fg-2)}
  .sev-minor{background:rgba(168,174,184,.10);color:var(--fg-2)}
  .sev-cosmetic{background:rgba(168,174,184,.10);color:var(--fg-3)}
  .sev-info{background:rgba(16,185,129,.08);color:var(--ac)}

  .confbar{display:inline-flex;align-items:center;gap:6px;font-family:var(--mono);font-size:11px}
  .confbar .track{width:80px;height:5px;border-radius:3px;background:var(--line);overflow:hidden}
  .confbar .fill{height:100%;background:var(--ac)}
  .confbar .pct{color:var(--ac);font-weight:600}

  .empty-array{color:var(--fg-4);font-family:var(--mono);font-size:11px;font-style:italic}
  details.report-detail{margin:4px 0}
  details.report-detail summary{cursor:pointer;font-family:var(--mono);font-size:11px;
    color:var(--ac);user-select:none}
  details.report-detail summary:hover{color:var(--ac-2)}
  details.report-detail[open] > div{margin:6px 0 6px 16px;padding:8px 10px;
    background:var(--surface-2);border-radius:5px;border-left:2px solid var(--line-2)}

  ul.report-list{margin:0;padding-left:18px;font-size:12px;line-height:1.7}
  ul.report-list li{color:var(--fg-2)}
  .err-view{padding:14px 16px;font-family:var(--mono);font-size:11.5px;line-height:1.6;
    color:var(--bad);white-space:pre-wrap;word-break:break-word;background:var(--bg)}

  .status-pill{display:inline-flex;align-items:center;gap:5px;font-family:var(--mono);
    font-size:10px;padding:2px 7px;border-radius:3px;font-weight:600;text-transform:uppercase;
    letter-spacing:.05em}
  .status-pill.queued{background:rgba(168,174,184,.10);color:var(--fg-2)}
  .status-pill.running{background:rgba(251,191,36,.10);color:var(--warn)}
  .status-pill.succeeded{background:rgba(74,222,128,.10);color:var(--ok)}
  .status-pill.failed{background:rgba(248,113,113,.10);color:var(--bad)}

  .toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);
    background:var(--surface-2);border:1px solid var(--ac);color:var(--fg);
    padding:9px 16px;border-radius:8px;font-size:12.5px;font-family:var(--mono);
    z-index:50;box-shadow:0 12px 30px rgba(0,0,0,.4);opacity:0;transition:opacity .2s}
  .toast.show{opacity:1}

  /* Floating section anchor nav (right rail) */
  .anchor-nav{
    position:fixed;top:50%;right:24px;transform:translateY(-50%);
    display:flex;flex-direction:column;gap:2px;
    background:rgba(20,24,30,.78);
    backdrop-filter:saturate(180%) blur(14px);
    -webkit-backdrop-filter:saturate(180%) blur(14px);
    border:1px solid var(--line-2);border-radius:12px;padding:6px;
    z-index:40;box-shadow:var(--shadow-2);
    opacity:0;transition:opacity .2s}
  .anchor-nav.show{opacity:1}
  .anchor-nav a{
    display:flex;align-items:center;gap:8px;
    padding:8px 12px;border-radius:8px;
    color:var(--fg-3);text-decoration:none;
    font-size:11.5px;font-weight:500;letter-spacing:.02em;
    transition:background .12s,color .12s;
    white-space:nowrap}
  .anchor-nav a:hover{background:var(--surface-2);color:var(--fg)}
  .anchor-nav a.current{background:var(--ac-bg);color:var(--ac-2)}
  .anchor-nav a .num{
    width:20px;height:20px;border-radius:6px;flex-shrink:0;
    display:grid;place-items:center;font-family:var(--mono);font-size:10.5px;font-weight:600;
    background:var(--surface-3);color:var(--fg-3);border:1px solid var(--line-1)}
  .anchor-nav a.current .num{background:var(--ac);color:#001a14;border-color:transparent}
  @media (max-width:1080px){.anchor-nav{display:none}}
</style></head>
<body>
<div class="topbar">
  <a class="brand-link" href="/tools" title="天枢 · 裁决 · 返回主页" style="text-decoration:none;display:inline-flex;align-items:center;gap:10px;font-family:'Noto Serif SC',Georgia,serif;font-size:19px;font-weight:600;letter-spacing:.18em;color:var(--fg);margin-right:24px;padding:4px 0"><svg viewBox="0 0 24 24" fill="currentColor" width="24" height="24" aria-hidden="true" style="color:var(--ac);opacity:1;flex-shrink:0;filter:drop-shadow(0 1px 2px rgba(196,90,58,.28))"><path d="M12 1.5L13.4 10.6L22.5 12L13.4 13.4L12 22.5L10.6 13.4L1.5 12L10.6 10.6Z"/></svg><span>天枢</span><span style="color:var(--ac);margin:0 6px;font-weight:400">·</span><span>裁决</span></a>
  <a class="back-btn" href="/tools" title="返回工具列表">
    <span style="font-size:14px;line-height:1">←</span>
    <span>返回</span>
  </a>
  <div class="crumbs">
    <a href="/tools">工具</a><span class="sep">/</span>
    <span class="current"><span id="tb-icon">·</span><span id="tb-name">…</span></span>
    <span class="sep" id="tb-sep" style="display:none">·</span>
    <span id="tb-status"></span>
  </div>
  <div class="stats" id="tb-stats" style="display:none">
    <span class="stat"><span class="lbl">入</span><span class="v" id="s-in">—</span></span>
    <span class="stat"><span class="lbl">出</span><span class="v" id="s-out">—</span></span>
    <span class="stat"><span class="lbl">缓存</span><span class="v" id="s-cache">—</span></span>
    <span class="stat cost"><span class="lbl">$</span><span class="v" id="s-cost">—</span></span>
    <span class="stat" id="s-elapsed-wrap" style="display:none"><span class="v" id="s-elapsed"></span></span>
  </div>
  <a href="/settings" class="set">设置</a>
  <button class="run" id="btn-run">▶ 运行<kbd>⌘↵</kbd></button>
</div>

<div class="hero">
  <div class="hero-inner">
    <div class="hero-icon-frame"><span class="ic" id="hero-icon">·</span></div>
    <div class="hero-body">
      <h2 id="hero-name">…</h2>
      <p class="tag" id="hero-tag">·</p>
      <div class="hero-meta">
        <span class="step" id="hero-step">·</span>
        <span class="resp" id="hero-resp">·</span>
        <span id="hero-prompts">·</span>
      </div>
    </div>
    <div class="hero-right" id="hero-claude-status"></div>
  </div>
</div>

<nav class="anchor-nav" id="anchor-nav">
  <a href="#sec-0" data-target="sec-0"><span class="num">0</span>项目</a>
  <a href="#sec-1" data-target="sec-1"><span class="num">1</span>材料</a>
  <a href="#sec-2" data-target="sec-2"><span class="num">2</span>步骤</a>
  <a href="#sec-3" data-target="sec-3"><span class="num">3</span>配置</a>
</nav>

<main>
<div class="runner-grid idle">
<div class="workspace">
  <!-- Section 0: 项目信息 (必填) -->
  <div class="sec" id="sec-0">
    <div class="sec-head">
      <span class="num">0</span><h3>项目信息</h3>
      <span style="margin-left:auto;font-size:11px;color:var(--fg-4)">必填 · 会写入报告</span>
    </div>
    <div class="sec-body" style="display:grid;grid-template-columns:240px 1fr;gap:14px">
      <div>
        <label for="proj-code" style="display:block;font-size:11px;color:var(--fg-3);margin-bottom:5px;letter-spacing:.04em;text-transform:uppercase">项目编号 *</label>
        <input id="proj-code" type="text" maxlength="64" placeholder="例:PROJ-2026-Q1-001"
          style="width:100%;background:var(--surface-2);border:1px solid var(--line);border-radius:6px;
                 color:var(--fg);font-family:var(--mono);font-size:12.5px;padding:9px 11px;outline:none">
      </div>
      <div>
        <label for="proj-name" style="display:block;font-size:11px;color:var(--fg-3);margin-bottom:5px;letter-spacing:.04em;text-transform:uppercase">项目名称 *</label>
        <input id="proj-name" type="text" maxlength="128" placeholder="例:Miku 18 红包活动 H5"
          style="width:100%;background:var(--surface-2);border:1px solid var(--line);border-radius:6px;
                 color:var(--fg);font-family:var(--sans);font-size:13px;padding:9px 11px;outline:none">
      </div>
    </div>
  </div>

  <!-- Section 1: 输入 -->
  <div class="sec" id="sec-1">
    <div class="sec-head">
      <span class="num">1</span><h3 id="input-label">需求材料</h3>
      <button type="button" id="hint-toggle" class="hint-toggle">填写指引 ▾</button>
    </div>
    <details id="hint-panel" class="hint-panel">
      <summary style="display:none"></summary>
      <div id="input-hint" class="hint-body">粘贴文本、上传文档或拖入 — 这是整个分析的源头</div>
    </details>
    <div class="sec-body">
      <div class="input-zone" id="input-zone">
        <div class="input-toolbar">
          <button type="button" id="upload-btn">↑ 上传文件</button>
          <input type="file" id="file-input" multiple style="display:none"
            accept=".md,.txt,.json,.yaml,.yml,.csv,.tsv,.html,.xml,.pdf,.png,.jpg,.jpeg,.gif,.webp,.docx,.xlsx,.pptx,.py,.js,.ts,.tsx,.go,.rs,.java,.cpp,.c,.swift,.kt,.rb,.sh,.sql,.log">
          <button type="button" id="import-btn" title="把上游工具(step1/2/4 等)的报告作为输入">⇡ 从上游报告导入</button>
          <span class="drag-hint">支持拖拽文件到此处</span>
          <span class="size-hint" id="size-hint" style="margin-left:auto">0 B</span>
        </div>
        <div id="import-panel" style="display:none;margin:8px 0 0;padding:10px 12px;
          background:var(--surface-2);border:1px solid var(--line);border-radius:6px">
          <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
            <span style="font-size:12px;color:var(--fg-2);font-weight:500">选择要作为输入的上游报告</span>
            <button type="button" id="import-close" style="margin-left:auto;background:transparent;border:none;color:var(--fg-3);cursor:pointer;font-size:12px">关闭</button>
          </div>
          <div id="import-list" style="max-height:240px;overflow-y:auto;
            display:flex;flex-direction:column;gap:4px"></div>
        </div>
        <textarea id="doc-input" placeholder="粘贴 PRD / 接口定义 / 页面信息&#10;&#10;或拖一个 .md / .pdf / .docx / 截图 进来"></textarea>
        <div class="drop-overlay">
          <div class="drop-overlay-inner">
            <div class="drop-icon">⤓</div>
            <div class="drop-text">释放鼠标 — 文件将自动加载</div>
            <div class="drop-sub" id="drop-sub">.md .txt .json .yaml .csv .pdf .png .docx …</div>
          </div>
        </div>
      </div>
      <div class="run-options" id="run-options"></div>
    </div>
  </div>

  <!-- Section 2: 提示词 -->
  <div class="sec" id="sec-2">
    <div class="sec-head">
      <span class="num">2</span><h3>执行步骤</h3>
      <span class="sub" id="prompts-sub">展开查看 prompt · 修改自动保存为覆盖</span>
    </div>
    <div id="prompts-toolbar" style="display:none;padding:9px 18px;border-bottom:1px solid var(--line);background:var(--surface-2);font-size:11.5px;color:var(--fg-3);font-family:var(--mono);display:flex;align-items:center;gap:10px">
      <span>勾选要执行的子步骤：</span>
      <button id="prompt-all" style="background:transparent;border:1px solid var(--line-2);color:var(--fg-2);padding:3px 9px;border-radius:5px;font-family:var(--mono);font-size:11px;cursor:pointer">全选</button>
      <button id="prompt-none" style="background:transparent;border:1px solid var(--line-2);color:var(--fg-2);padding:3px 9px;border-radius:5px;font-family:var(--mono);font-size:11px;cursor:pointer">全不选</button>
      <span style="margin-left:auto" id="prompt-count">0/0</span>
    </div>
    <div id="prompts-list"></div>
  </div>

  <!-- Section 3: 模型 & 精度 -->
  <div class="sec" id="sec-3">
    <div class="sec-head">
      <span class="num">3</span><h3>运行配置</h3>
      <span class="sub">所有子步骤共用一份模型与精度 · 自动记忆</span>
    </div>
    <div class="sec-body">
      <div class="model-grid">
        <div class="model-cell" id="cell-model">
          <div class="label">模型 <span class="from" id="model-from">来源：本地</span></div>
          <select id="sel-model"></select>
          <div class="hint" id="model-hint">所有子步骤统一使用</div>
          <div class="probe-status" id="model-probe-status" style="margin-top:6px;font-family:var(--mono);font-size:11px;letter-spacing:.02em;display:none"></div>
        </div>
        <div class="model-cell" id="cell-effort">
          <div class="label">精度 <span class="from" id="effort-from">默认</span></div>
          <select id="sel-effort"></select>
          <div class="hint">越高越细 · max 最强最慢</div>
        </div>
        <div class="model-cell" id="cell-thinking">
          <div class="label">扩展思考</div>
          <select id="sel-thinking"></select>
          <div class="hint">复杂推理选 adaptive</div>
        </div>
        <div class="model-cell unsupported" id="cell-unsupported" style="display:none;grid-column:span 2">
          <div class="label">当前模型不支持精度 / 思考调节</div>
          <div class="hint" id="unsupported-hint">Haiku 系列定位高速低成本。如需调精度请切到 Sonnet 或 Opus。</div>
        </div>
      </div>
      <div class="claude-status" id="claude-status">检测中…</div>
    </div>
  </div>
</div><!-- /workspace -->

<aside class="run-panel" aria-label="运行状态">
  <div class="run-panel-head">
    <span class="live-dot" id="run-status-dot"></span>
    <span class="label-text" id="run-status-label">就绪</span>
    <span class="meta-tail" id="run-status-meta"></span>
  </div>
  <div class="run-panel-body">
    <div id="run-area">
      <div class="run-empty-pretty">
        <div class="play-circle" id="play-circle" title="运行" style="cursor:pointer">▶</div>
        <h4 class="title">准备开始运行</h4>
        <p class="desc">填写左侧三块（材料 · 步骤 · 配置）后启动 — 这里会实时显示进度与报告。</p>
        <div class="hint-row"><kbd>⌘</kbd><kbd>↵</kbd>&nbsp;快速运行</div>
      </div>
    </div>
  </div>
</aside>
</div><!-- /runner-grid -->
</main>

<div class="toast" id="toast"></div>

<script>
const TOOL_ID = location.pathname.split('/').filter(Boolean).pop();
let tool = null;
let claudeInfo = null;
let pollTimer = null, elapsedTimer = null;
let currentRunId = new URLSearchParams(location.search).get('run');
let currentRun = null;

async function load(){
  const [t, ci, cat] = await Promise.all([
    fetch('/api/tools/' + TOOL_ID).then(r=>r.json()),
    fetch('/api/claude/info').then(r=>r.json()),
    fetch('/api/tools').then(r=>r.json()).catch(()=>({tools:[]})),
  ]);
  tool = t; claudeInfo = ci;
  // 顶部跟目录(/tools)显示的"第 X 章"完全一致 — 不再用 STEP4/5/6,
  // 因为目录给 step4 分配的是第三章,详情页却写 STEP4,Codex AI-FE-006 报告了这个矛盾。
  const allTools = (cat && cat.tools) || [];
  const CN_CH = ['一','二','三','四','五','六','七','八','九','十'];
  const idx = allTools.findIndex(x => x.id === tool.id);
  const chapterLabel = (idx >= 0) ? ('第 ' + (CN_CH[idx] || String(idx+1)) + ' 章') : (tool.step || tool.id).toUpperCase();
  document.title = tool.name + ' — 天枢·裁决';
  document.getElementById('tb-icon').textContent = tool.icon;
  document.getElementById('tb-name').textContent = tool.name;
  document.getElementById('hero-icon').textContent = tool.icon;
  // 顶部 meta:章节(与目录一致) · 负责方 · 子步骤数
  document.getElementById('hero-step').textContent = chapterLabel;
  document.getElementById('hero-step').title = (tool.step || tool.id).toUpperCase() + ' · 内部步骤标识';
  document.getElementById('hero-resp').textContent = '· ' + (tool.responsible || '');
  document.getElementById('hero-prompts').textContent = '· ' + tool.prompts.length + ' 子步骤';
  document.getElementById('hero-name').textContent = tool.name;
  document.getElementById('hero-tag').textContent = tool.description;
  // 取消 hero-pills(已合并到 meta 一行)
  // hero 右侧:接入状态 + 输出物
  const hr = document.getElementById('hero-claude-status');
  if (hr) {
    const auth = (claudeInfo && claudeInfo.auth_state) || '';
    const acc = (claudeInfo && claudeInfo.account) || {};
    let row1 = '';
    if (auth === 'toolkit_api_key') {
      row1 = `<span class="row ok"><span class="dot"></span>API Key · ${acc.display_name || ''}</span>`;
    } else if (auth === 'toolkit_oauth') {
      row1 = `<span class="row ok"><span class="dot"></span>OAuth${acc.email ? ' · ' + acc.email : ''}</span>`;
    } else if (auth === 'external_claude_cli' && acc.email) {
      row1 = `<span class="row warn"><span class="dot"></span>本机 CLI · ${acc.email}</span>`;
    } else {
      row1 = `<span class="row warn"><span class="dot"></span>未接入 · <a href="/settings">去设置</a></span>`;
    }
    const out = (tool.output || '').replace(/[《》]/g, '');
    hr.innerHTML = row1 + `<span class="row" style="font-size:10.5px">输出 · ${out || '—'}</span>`;
  }

  // Section 1 — single field label + hint + run options
  const inp = tool.input || {};
  // 项目编号 + 名称:从 localStorage 预填,聚焦时清除红边
  try {
    const last = JSON.parse(localStorage.getItem('toolkit.last_project') || '{}');
    if (last.code) document.getElementById('proj-code').value = last.code;
    if (last.name) document.getElementById('proj-name').value = last.name;
  } catch(_){}
  ['proj-code','proj-name'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('focus', () => { el.style.borderColor = ''; });
  });

  document.getElementById('input-label').textContent = inp.label || '需求材料';
  const hintEl = document.getElementById('input-hint');
  if (hintEl) hintEl.textContent = inp.hint || '粘贴文本、上传文档或拖入 — 这是整个分析的源头';
  // 折叠式 hint 切换
  const hintBtn = document.getElementById('hint-toggle');
  const hintPanel = document.getElementById('hint-panel');
  if (hintBtn && hintPanel) {
    hintBtn.onclick = () => {
      const open = hintPanel.classList.toggle('open');
      hintBtn.classList.toggle('open', open);
      hintBtn.textContent = open ? '填写指引 ▴' : '填写指引 ▾';
    };
  }
  document.getElementById('input-hint').textContent = inp.hint || '粘贴文本、上传文档或拖入 — 这是整个分析的源头';
  buildRunOptions();
  wireInputToolbar();

  await buildPrompts();
  buildModelControls();
  if (currentRunId) startPolling(currentRunId);
  // 全局运行锁:不管谁打开、是否刷新,都按后端真实状态显示"运行中" / "▶ 运行"
  startActiveStatusPolling();
}

function buildRunOptions(){
  const root = document.getElementById('run-options');
  root.innerHTML = '';
  const opts = tool.run_options || [];
  opts.forEach(o => {
    const lbl = document.createElement('label');
    lbl.innerHTML = `<input type="checkbox" id="ro-${o.key}" ${o.default ? 'checked' : ''}> ${o.label}`;
    root.appendChild(lbl);
  });
}

function wireInputToolbar(){
  const ta = document.getElementById('doc-input');
  const sizeEl = document.getElementById('size-hint');
  const updateSize = () => {
    const len = new Blob([ta.value]).size;
    sizeEl.textContent = len < 1024 ? `${len} B` :
                         len < 1024*1024 ? `${(len/1024).toFixed(1)} KB` :
                         `${(len/1024/1024).toFixed(2)} MB`;
  };
  ta.addEventListener('input', updateSize);
  updateSize();

  // Files which are safe to read as UTF-8 in the browser
  const TEXT_EXTS = new Set([
    'md','txt','json','yaml','yml','csv','tsv','html','htm','xml','svg',
    'py','js','ts','tsx','jsx','vue','go','rs','java','cpp','c','h','swift','kt','rb','sh','bash',
    'sql','log','env','toml','ini','conf','properties','dockerfile','makefile',
    'patch','diff'
  ]);
  // Files which need server-side parsing (binary or encoding-tricky)
  const BINARY_EXTS = new Set(['pdf','docx','xlsx','xlsm','png','jpg','jpeg','gif','webp','bmp','doc','ppt','pptx']);

  function fileExt(name){
    const i = name.lastIndexOf('.');
    return i >= 0 ? name.slice(i+1).toLowerCase() : '';
  }

  async function readFileAsText(f){
    // Decide: local text read vs server-side extraction
    const ext = fileExt(f.name);
    const ct = (f.type || '').toLowerCase();
    const looksBinary = BINARY_EXTS.has(ext) || ct.startsWith('image/') || ct === 'application/pdf' ||
      ct.includes('officedocument') || ct === 'application/msword';
    if (TEXT_EXTS.has(ext) && !looksBinary){
      try { return await f.text(); }
      catch(_){ /* fall through to server */ }
    }
    // Send to server for extraction
    const fd = new FormData();
    fd.append('file', f, f.name);
    const r = await fetch('/api/extract-file', { method: 'POST', body: fd });
    if (!r.ok) throw new Error('extract failed: HTTP ' + r.status);
    const data = await r.json();
    return data.text;
  }

  // Helper: append a list of files into the textarea with separators
  async function loadFiles(files){
    if (!files || !files.length) return;
    const parts = [];
    let skipped = 0;
    let failed = 0;
    for (const f of files){
      // Skip directories (dragged folders show up as 0-byte type-empty entries)
      if (f.size === 0 && !f.type){
        skipped++;
        continue;
      }
      let text;
      try {
        text = await readFileAsText(f);
      } catch(err){
        text = `(读取失败：${err.message || err})`;
        failed++;
      }
      parts.push(`--- file: ${f.name} (${f.type || 'text/plain'}, ${f.size}B) ---\n${text}`);
    }
    if (parts.length){
      const sep = ta.value.trim() ? '\n\n' : '';
      ta.value += sep + parts.join('\n\n');
      updateSize();
      const bits = [`已加载 ${parts.length} 个文件`];
      if (skipped) bits.push(`跳过 ${skipped} 个空/目录`);
      if (failed) bits.push(`${failed} 个读取失败`);
      toast(bits.join(' · '));
    } else if (skipped > 0){
      toast(`跳过 ${skipped} 个目录或空文件，未加载任何内容`);
    }
  }

  // Click to upload
  document.getElementById('upload-btn').onclick = () => {
    document.getElementById('file-input').click();
  };
  document.getElementById('file-input').onchange = async (e) => {
    await loadFiles(Array.from(e.target.files || []));
    e.target.value = '';
  };

  // 从上游报告导入 — 列出最近 8 个其他工具的报告,选一个把核心内容塞 textarea
  const importBtn = document.getElementById('import-btn');
  const importPanel = document.getElementById('import-panel');
  const importList = document.getElementById('import-list');
  document.getElementById('import-close').onclick = () => { importPanel.style.display='none'; };
  importBtn.onclick = async () => {
    importPanel.style.display = 'block';
    importList.innerHTML = '<div style="color:var(--fg-3);font-size:11.5px;padding:8px">加载中…</div>';
    try {
      const [reps, catRaw] = await Promise.all([
        fetch('/api/reports').then(r => r.json()),
        fetch('/api/tools').then(r => r.json()),
      ]);
      const toolNameMap = {};
      (catRaw.tools || []).forEach(t => { toolNameMap[t.id] = (t.icon || '') + ' ' + t.name; });
      // 合并 in-memory + saved,排除当前工具的 run(导入上游是另一个工具)
      const all = [];
      (reps.in_memory || []).forEach(r => { if (r.tool_id !== tool.id) all.push(r); });
      (reps.saved || []).forEach(r => {
        if (r.tool_id !== tool.id && !all.find(x => x.run_id === r.run_id)) all.push(r);
      });
      all.sort((a, b) => (b.mtime || b.started_at || 0) - (a.mtime || a.started_at || 0));
      const top = all.slice(0, 12);
      if (!top.length) {
        importList.innerHTML = '<div style="color:var(--fg-3);font-size:11.5px;padding:8px">还没有其他工具的报告，先跑一个上游工具(比如 step1 需求评审)</div>';
        return;
      }
      importList.innerHTML = top.map(r => {
        const label = toolNameMap[r.tool_id] || r.tool_id;
        const t = r.mtime ? new Date(r.mtime * 1000) : new Date((r.started_at || 0) * 1000);
        const ts = t.toLocaleString('zh-CN', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
        return `<button type="button" class="import-row" data-rid="${r.run_id}" data-tid="${r.tool_id}"
          style="display:flex;align-items:center;gap:8px;padding:6px 10px;border:1px solid var(--line);
                 background:transparent;border-radius:4px;cursor:pointer;text-align:left;
                 font-size:12px;color:var(--fg-2)">
          <span style="font-weight:500;color:var(--fg)">${label}</span>
          <span style="font-family:var(--mono);font-size:10.5px;color:var(--fg-3)">${r.run_id.slice(0,8)}</span>
          <span style="margin-left:auto;font-size:10.5px;color:var(--fg-3)">${ts}</span>
        </button>`;
      }).join('');
      importList.querySelectorAll('.import-row').forEach(b => {
        b.onclick = async () => {
          const rid = b.dataset.rid;
          const tid = b.dataset.tid;
          b.disabled = true;
          b.textContent = '加载中…';
          try {
            const rep = await fetch('/api/reports/' + rid).then(r => r.json());
            const r = rep.report || {};
            const ta = document.getElementById('doc-input');
            // 把上游报告的核心契约字段格式化成 markdown 注入,LLM 比单纯 JSON 友好
            const lines = [];
            lines.push('# 上游报告 — ' + (toolNameMap[tid] || tid));
            lines.push('Run ID: `' + rid + '`');
            lines.push('');
            if (r.verdict) lines.push('## 测试结论\n' + r.verdict + (r.verdict_summary ? '\n\n' + r.verdict_summary : ''));
            if (Array.isArray(r.risks) && r.risks.length) {
              lines.push('\n## 风险');
              r.risks.forEach(x => {
                const t = typeof x === 'string' ? x : (x.title || '');
                const imp = typeof x === 'object' ? (x.impact || '') : '';
                lines.push('- ' + t + (imp ? ' — ' + imp : ''));
              });
            }
            if (Array.isArray(r.blockers) && r.blockers.length) {
              lines.push('\n## 阻碍');
              r.blockers.forEach(x => {
                lines.push('- **' + (x.title || '') + '** (' + (x.owner_role || '') + ')');
                if (x.why_blocking) lines.push('  - 为何阻碍:' + x.why_blocking);
                if (x.what_to_unblock) lines.push('  - 如何解开:' + x.what_to_unblock);
              });
            }
            if (Array.isArray(r.issues) && r.issues.length) {
              lines.push('\n## 已知问题 (top ' + Math.min(15, r.issues.length) + ')');
              r.issues.slice(0, 15).forEach(it => {
                lines.push('- [' + (it.severity || '?') + '/' + (it.priority || '?') + '] ' + (it.title || ''));
                if (it.module) lines.push('  - 位置:' + it.module);
              });
            }
            if (Array.isArray(r.cases) && r.cases.length) {
              lines.push('\n## 用例集 (top ' + Math.min(20, r.cases.length) + ')');
              r.cases.slice(0, 20).forEach(c => {
                lines.push('- [' + (c.priority || '?') + '] `' + (c.id || '') + '` ' + (c.title || ''));
              });
            }
            const text = lines.join('\n');
            // 追加到 textarea 末尾,不覆盖现有输入
            if (ta.value && ta.value.trim()) {
              ta.value += '\n\n---\n\n' + text;
            } else {
              ta.value = text;
            }
            ta.dispatchEvent(new Event('input', {bubbles:true}));
            importPanel.style.display = 'none';
            // 提示
            const hint = document.getElementById('size-hint');
            if (hint) hint.textContent = '已导入上游报告';
          } catch (e) {
            b.textContent = '加载失败';
          }
        };
      });
    } catch (e) {
      importList.innerHTML = '<div style="color:var(--bad);font-size:11.5px;padding:8px">加载失败:' + (e.message || e) + '</div>';
    }
  };

  // Drag & drop — entire input-zone receives files
  const zone = document.getElementById('input-zone');
  let dragDepth = 0;
  // Stop the browser from "navigating away" if a file is dropped outside the zone
  ['dragenter','dragover','drop'].forEach(evt => {
    window.addEventListener(evt, e => {
      // Only intercept if files are being dragged
      if (e.dataTransfer && [...(e.dataTransfer.types||[])].includes('Files')){
        e.preventDefault();
      }
    });
  });
  zone.addEventListener('dragenter', e => {
    if (e.dataTransfer && [...(e.dataTransfer.types||[])].includes('Files')){
      e.preventDefault();
      dragDepth++;
      zone.classList.add('dragging');
    }
  });
  zone.addEventListener('dragover', e => {
    if (e.dataTransfer && [...(e.dataTransfer.types||[])].includes('Files')){
      e.preventDefault();
      e.dataTransfer.dropEffect = 'copy';
    }
  });
  zone.addEventListener('dragleave', e => {
    e.preventDefault();
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) zone.classList.remove('dragging');
  });
  zone.addEventListener('drop', async e => {
    e.preventDefault();
    dragDepth = 0;
    zone.classList.remove('dragging');
    const files = Array.from(e.dataTransfer?.files || []);
    await loadFiles(files);
  });
}

// === Section 2: prompts (collapsible + editable + optional checkbox) ===
async function buildPrompts(){
  const root = document.getElementById('prompts-list');
  const toolbar = document.getElementById('prompts-toolbar');
  root.innerHTML = '';
  const optional = !!tool.substeps_optional;
  toolbar.style.display = optional ? 'flex' : 'none';

  // Side effect: populate a substep id → name map used by the HTML report renderer.
  tool._substepNames = tool._substepNames || {};

  for (const sub_id of tool.prompts){
    const data = await fetch(`/api/prompts/${tool.prompt_dir}/${sub_id}`).then(r=>r.json());
    tool._substepNames[sub_id] = data.name;
    const row = document.createElement('div');
    row.className = 'prompt-row';
    row.dataset.subId = sub_id;
    const checkboxHtml = optional
      ? `<input type="checkbox" class="check" data-sub="${sub_id}" checked title="勾选 = 执行该子步骤；取消 = 跳过">`
      : '';
    const shortId = String(data.id).split('.').pop();
    row.innerHTML = `
      <div class="prompt-head">
        ${checkboxHtml}
        <span class="twirl">▶</span>
        <span class="id" title="${data.id}">#${shortId}</span>
        <span class="name">${data.name}</span>
        <span class="chip" style="margin-left:auto" title="模板字段数 · 输出格式">${data.placeholders.length} 字段 · ${data.output_format}</span>
        <span class="chip override" style="margin-left:6px;display:${data.is_override ? '' : 'none'}">已覆盖</span>
        <span class="chip dirty" style="margin-left:6px;display:none">未保存</span>
      </div>
      <div class="prompt-body">
        <textarea>${escapeHtml(data.body)}</textarea>
        <div class="btn-row">
          <button class="save">保存覆盖</button>
          <button class="reset" style="display:${data.is_override ? '' : 'none'}">重置回原版</button>
          <span class="info">占位符 ${data.placeholders.length} · 输出 ${data.output_format}</span>
        </div>
      </div>`;
    const head = row.querySelector('.prompt-head');
    const cb = row.querySelector('.check');
    head.onclick = (e) => {
      // Don't toggle expand when clicking the checkbox
      if (e.target === cb) return;
      row.classList.toggle('open');
    };
    if (cb){
      cb.onclick = (e) => e.stopPropagation();
      cb.onchange = () => {
        row.classList.toggle('disabled', !cb.checked);
        updatePromptCount();
      };
    }
    const ta = row.querySelector('textarea');
    const saveBtn = row.querySelector('.save');
    const resetBtn = row.querySelector('.reset');
    const overrideChip = row.querySelectorAll('.chip.override')[0];
    const dirtyChip = row.querySelectorAll('.chip.dirty')[0];
    let original = data.body;
    ta.oninput = () => {
      dirtyChip.style.display = (ta.value !== original) ? '' : 'none';
    };
    saveBtn.onclick = async (ev) => {
      ev.stopPropagation();
      saveBtn.disabled = true; saveBtn.textContent = '保存中…';
      try {
        await fetch(`/api/prompts/${tool.prompt_dir}/${sub_id}`, {
          method:'PUT', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({body: ta.value}),
        });
        original = ta.value;
        dirtyChip.style.display = 'none';
        overrideChip.style.display = '';
        resetBtn.style.display = '';
        toast(`${sub_id} 已保存覆盖`);
      } finally {
        saveBtn.disabled = false; saveBtn.textContent = '保存覆盖';
      }
    };
    resetBtn.onclick = async (ev) => {
      ev.stopPropagation();
      if (!confirm(`确认重置 ${sub_id} 到原版？当前编辑会丢失。`)) return;
      await fetch(`/api/prompts/${tool.prompt_dir}/${sub_id}`, {method:'DELETE'});
      // Reload original body
      const r2 = await fetch(`/api/prompts/${tool.prompt_dir}/${sub_id}`).then(r=>r.json());
      ta.value = r2.body;
      original = r2.body;
      dirtyChip.style.display = 'none';
      overrideChip.style.display = 'none';
      resetBtn.style.display = 'none';
      toast(`${sub_id} 已重置`);
    };
    root.appendChild(row);
  }
  // Wire toolbar select-all / select-none
  if (tool.substeps_optional){
    document.getElementById('prompt-all').onclick = () => {
      root.querySelectorAll('.check').forEach(cb => {
        if (!cb.checked){ cb.checked = true; cb.dispatchEvent(new Event('change')); }
      });
    };
    document.getElementById('prompt-none').onclick = () => {
      root.querySelectorAll('.check').forEach(cb => {
        if (cb.checked){ cb.checked = false; cb.dispatchEvent(new Event('change')); }
      });
    };
    updatePromptCount();
  }
}

function updatePromptCount(){
  const all = document.querySelectorAll('#prompts-list .check');
  const checked = document.querySelectorAll('#prompts-list .check:checked');
  const el = document.getElementById('prompt-count');
  if (el) el.textContent = `${checked.length}/${all.length}`;
}

function getEnabledSubsteps(){
  if (!tool.substeps_optional) return null;
  const ids = [];
  document.querySelectorAll('#prompts-list .check:checked').forEach(cb => ids.push(cb.dataset.sub));
  return ids;
}

// === Section 3: model controls ===
function buildModelControls(){
  const stored = JSON.parse(localStorage.getItem('toolkit.model_prefs') || '{}');

  // Model dropdown — match Claude desktop list (specific versions + Legacy badge + 1M)
  const ms = document.getElementById('sel-model');
  ms.innerHTML = '';
  // Default to whichever entry the registry marks as default (currently Opus 4.7)
  // 跳过已知不可用的模型,避免默认选到一个会失败的 model
  const availableModels = claudeInfo.available_models.filter(m => m.available !== false);
  const defaultPick = availableModels.find(m => m.default) || availableModels[0]
                      || claudeInfo.available_models[0];
  const desktopDefault = defaultPick.key;
  // 用户上次选的如果已经不可用,也别再默认到它
  const storedAvailable = stored.model && claudeInfo.available_models.find(
    m => m.key === stored.model && m.available !== false);
  const defaultModel = storedAvailable ? stored.model : desktopDefault;
  claudeInfo.available_models.forEach(m => {
    const sel = m.key === defaultModel ? 'selected' : '';
    const badge = m.version_badge ? ` ${m.version_badge}` : '';
    // 探测读回的真实版本号 → 动态附在档位名后(如 "Opus · 4.8");未探测则只显示档位名
    const _vm = (m.resolved_model || '').match(/([0-9]+)[.-]([0-9]+)/);
    const ver = _vm ? ` · ${_vm[1]}.${_vm[2]}` : '';
    const classes = [];
    if (m.legacy) classes.push('legacy');
    if (m.available === false) classes.push('unavailable');
    if (m.experimental) classes.push('experimental');
    const disabled = m.available === false ? 'disabled' : '';
    // 不可用的模型在 label 前缀里挂个标记,disabled 浏览器自带样式会变灰
    const prefix = m.available === false ? '✕ ' : (m.experimental ? '⚠ ' : '');
    const tail = m.available === false ? '（账号未开通 / 已下线）' : '';
    ms.insertAdjacentHTML('beforeend',
      `<option value="${m.key}" ${sel} ${disabled} class="${classes.join(' ')}">${prefix}${m.label}${ver}${badge} — ${m.tag}${tail}</option>`);
  });
  const defaultLabel = (claudeInfo.available_models.find(m => m.key === desktopDefault) || {}).label || 'Opus';
  document.getElementById('model-hint').textContent = '· 所有子步骤统一使用此模型';
  document.getElementById('model-from').textContent = stored.model
    ? '来源：上次选择'
    : `来源：默认 ${defaultLabel}`;

  // Effort dropdown — only filled if model supports it (handled by syncCapabilities)
  const ef = document.getElementById('sel-effort');
  const defaultEffort = stored.effort || claudeInfo.settings_effort_level || 'medium';

  // Thinking dropdown
  const th = document.getElementById('sel-thinking');
  const defaultThinking = stored.thinking || 'disabled';

  // Build effort/thinking options ONCE; visibility & population per model
  function fillEffort(model){
    const allowed = new Set(model.supported_efforts || []);
    ef.innerHTML = '';
    claudeInfo.available_efforts
      .filter(e => allowed.has(e.key))
      .forEach(e => {
        const sel = e.key === defaultEffort ? 'selected' : '';
        ef.insertAdjacentHTML('beforeend', `<option value="${e.key}" ${sel}>${e.label} — ${e.tag}</option>`);
      });
    // If stored default isn't in allowed set, just pick first
    if (!allowed.has(defaultEffort) && ef.options.length){
      ef.selectedIndex = 0;
    }
  }
  function fillThinking(model){
    const allowed = new Set(model.supported_thinking || []);
    th.innerHTML = '';
    claudeInfo.available_thinking
      .filter(t => allowed.has(t.key))
      .forEach(t => {
        const sel = t.key === defaultThinking ? 'selected' : '';
        th.insertAdjacentHTML('beforeend', `<option value="${t.key}" ${sel}>${t.label}</option>`);
      });
    if (!allowed.has(defaultThinking) && th.options.length){
      th.selectedIndex = 0;
    }
  }

  function syncCapabilities(){
    const sel = document.getElementById('sel-model').value;
    const m = claudeInfo.available_models.find(x => x.key === sel) || claudeInfo.available_models[0];
    const supportsEffort = !!m.supports_effort;
    const supportsThinking = !!m.supports_thinking;

    document.getElementById('cell-effort').style.display = supportsEffort ? '' : 'none';
    document.getElementById('cell-thinking').style.display = supportsThinking ? '' : 'none';
    // experimental / unavailable 也强制显示 — 不光是"两个都不支持时才提示"
    const forceShow = !!m.experimental || m.available === false;
    document.getElementById('cell-unsupported').style.display =
      (forceShow || (!supportsEffort && !supportsThinking)) ? 'block' : 'none';

    if (supportsEffort) fillEffort(m);
    if (supportsThinking) fillThinking(m);

    // Tailor unsupported hint to current model
    const isHaikuFamily = String(m.model || '').includes('haiku');
    let hint = isHaikuFamily
      ? `Haiku 系列定位是高速 / 低成本任务，桌面端也不暴露 effort 与 thinking 选项。如需调精度请切换到 Sonnet 或 Opus。`
      : `${m.label} 不支持精度 / 思考调节。`;
    if (m.experimental) {
      hint = `⚠️ ${m.label} 当前为实验级。若你的账号未开通该模型，运行会因 SDK 调用失败而无报告 — 建议先用 Sonnet / Opus 验证流程，或点 [测试该模型] 现场探测。\n` + hint;
    }
    if (m.available === false) {
      hint = `✕ ${m.label} 在当前账号上不可用。原因：${m.unavailable_reason || '上次调用失败已被会话禁用'}。请改选 Sonnet / Opus，或到设置页修复后重启。\n` + hint;
    }
    const hintEl = document.getElementById('unsupported-hint');
    hintEl.textContent = hint;
    hintEl.style.whiteSpace = 'pre-line';
    // 给实验模型加一个"现场探测"按钮 — 现场打一个 1-token 请求看模型能不能跑
    const cell = document.getElementById('cell-unsupported');
    let btn = cell.querySelector('button.probe-btn');
    if (m.experimental && m.available !== false){
      if (!btn){
        btn = document.createElement('button');
        btn.className = 'probe-btn';
        btn.type = 'button';
        btn.style.cssText = 'margin-top:8px;padding:6px 12px;border:1px solid var(--accent);background:transparent;color:var(--accent);border-radius:3px;font-family:var(--mono);font-size:11.5px;letter-spacing:.04em;cursor:pointer';
        cell.appendChild(btn);
      }
      btn.textContent = '测试该模型 (1-token 探测)';
      btn.onclick = async () => {
        btn.disabled = true; btn.textContent = '探测中…';
        try {
          const r = await fetch('/api/claude/probe_model', {
            method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify({model_key: m.key})
          }).then(r=>r.json());
          if (r.ok){
            btn.textContent = `✓ ${m.label} 可用`;
            btn.style.color = 'var(--ok)'; btn.style.borderColor = 'var(--ok)';
          } else if (r.model_specific){
            btn.textContent = `✕ ${m.label} 当前账号未开通 — 已禁用`;
            btn.style.color = 'var(--bad)'; btn.style.borderColor = 'var(--bad)';
            // 刷新 claudeInfo,让下拉立即 disable
            try {
              claudeInfo = await fetch('/api/claude/info').then(r => r.json());
              buildModelControls();
            } catch(_){}
          } else {
            // 非模型问题(认证/网络/CLI 损坏)— 不禁用,只警告,让用户去设置页排查
            btn.textContent = '⚠ 探测失败 — 可能是认证/网络,请到设置排查';
            btn.title = String(r.error || '').slice(0, 400);
            btn.style.color = 'var(--warn)'; btn.style.borderColor = 'var(--warn)';
          }
        } catch(e){
          btn.textContent = '探测请求失败';
        } finally {
          setTimeout(() => { btn.disabled = false; }, 1500);
        }
      };
    } else if (btn){
      btn.remove();
    }

    document.getElementById('effort-from').textContent = stored.effort && supportsEffort
      ? `沿用上次：${ef.value}`
      : `默认：${claudeInfo.settings_effort_level || 'medium'}`;
  }
  syncCapabilities();

  // ── 选模型 → 自动探测该模型 + 刷新接入,即时显示 ✓/✗ ──
  // 用户选什么模型就跑什么,选完立刻知道这个模型通不通,不用跑完整工具才发现。
  let _probeTimer = null;
  async function autoProbeModel(){
    const sel = document.getElementById('sel-model');
    const statusEl = document.getElementById('model-probe-status');
    if (!sel || !statusEl) return;
    const m = claudeInfo.available_models.find(x => x.key === sel.value);
    const label = m ? m.label : sel.value;
    statusEl.style.display = '';
    statusEl.style.color = 'var(--fg-3)';
    statusEl.textContent = `· 正在探测 ${label} 接入…`;
    try {
      const r = await fetch('/api/claude/probe_model', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({model_key: sel.value}),
      }).then(x => x.json());
      if (r.ok){
        statusEl.style.color = 'var(--ok)';
        statusEl.textContent = `✓ ${label} 接入正常,可运行`;
      } else if (r.model_specific){
        statusEl.style.color = 'var(--bad)';
        statusEl.textContent = `✗ ${label} 当前账号不可用 — 请换其他模型`;
      } else {
        statusEl.style.color = 'var(--warn)';
        statusEl.textContent = `⚠ ${label} 探测失败(认证/网络),运行可能受影响`;
        statusEl.title = String(r.error || '').slice(0, 300);
      }
    } catch(e){
      statusEl.style.color = 'var(--warn)';
      statusEl.textContent = `⚠ 探测请求失败:${e.message}`;
    }
  }
  function scheduleProbe(){
    // debounce 600ms — 用户连续切换时只探最后一次
    if (_probeTimer) clearTimeout(_probeTimer);
    _probeTimer = setTimeout(autoProbeModel, 600);
  }

  // Persist + re-sync on any change
  document.getElementById('sel-model').addEventListener('change', () => {
    syncCapabilities();
    persistPrefs();
    scheduleProbe();   // 换模型 → 自动探测
  });
  ['sel-effort','sel-thinking'].forEach(id => {
    document.getElementById(id).addEventListener('change', () => {
      persistPrefs();
      scheduleProbe();  // 换精度/思考 → 也重新确认一次接入
    });
  });
  // 页面加载后先探一次当前选中的模型
  scheduleProbe();

  function persistPrefs(){
    const cur = JSON.parse(localStorage.getItem('toolkit.model_prefs') || '{}');
    cur.model = document.getElementById('sel-model').value;
    // Only persist effort/thinking if their cells are visible
    if (document.getElementById('cell-effort').style.display !== 'none'){
      cur.effort = document.getElementById('sel-effort').value;
    }
    if (document.getElementById('cell-thinking').style.display !== 'none'){
      cur.thinking = document.getElementById('sel-thinking').value;
    }
    localStorage.setItem('toolkit.model_prefs', JSON.stringify(cur));
  }

  // Claude status line —— 优先显示 toolkit 接入模式(API Key / OAuth),
  // 没接入则提示去设置;本机 CLI 版本作为辅助信息
  const cs = document.getElementById('claude-status');
  if (claudeInfo.bin_found){
    const acc = claudeInfo.account || {};
    const auth = claudeInfo.auth_state || '';
    let modeChip = '';
    if (auth === 'toolkit_api_key') {
      modeChip = `<span>API Key</span><span>·</span><span style="font-family:var(--mono);font-size:11.5px">${acc.display_name || ''}</span>`;
    } else if (auth === 'toolkit_oauth') {
      const email = acc.email ? ` · ${acc.email}` : '';
      modeChip = `<span>OAuth</span>${email ? '<span>·</span><span>'+email+'</span>' : ''}`;
    } else if (auth === 'external_claude_cli' && acc.email) {
      modeChip = `<span>本机 CLI 凭据</span><span>·</span><span>${acc.email}</span>`;
    } else {
      modeChip = `<span style="color:var(--warn)">未接入 — 请先到 <a href="/settings">设置</a> 选 OAuth 或 API Key</span>`;
    }
    const okOrWarn = (auth === 'toolkit_api_key' || auth === 'toolkit_oauth') ? 'ok' : 'warn';
    cs.className = 'claude-status ' + okOrWarn;
    cs.innerHTML = `<span class="dot"></span>
      <span>Claude CLI <span class="v">${claudeInfo.version || '?'}</span></span>
      <span>·</span>${modeChip}
      <a href="/settings" style="margin-left:auto">详情</a>`;
  } else {
    cs.className = 'claude-status bad';
    cs.innerHTML = `<span class="dot"></span><span>未找到 Claude Code CLI</span><a href="/settings">查看设置</a>`;
  }
}

// === Run ===
async function runTool(){
  if (!tool) return;
  // 强校验:项目编号 + 项目名称必填
  const projCodeEl = document.getElementById('proj-code');
  const projNameEl = document.getElementById('proj-name');
  const projCode = (projCodeEl ? projCodeEl.value : '').trim();
  const projName = (projNameEl ? projNameEl.value : '').trim();
  if (!projCode){
    toast('请填写项目编号');
    if (projCodeEl) { projCodeEl.focus(); projCodeEl.style.borderColor = 'var(--bad)'; }
    return;
  }
  if (!projName){
    toast('请填写项目名称');
    if (projNameEl) { projNameEl.focus(); projNameEl.style.borderColor = 'var(--bad)'; }
    return;
  }
  // 记住上次填的项目,下次预填
  try {
    localStorage.setItem('toolkit.last_project', JSON.stringify({code:projCode, name:projName}));
  } catch(_){}

  const body = {project_code: projCode, project_name: projName};
  const text = document.getElementById('doc-input').value.trim();
  if (!text){ toast('请先填入文档内容'); return; }
  const inp = tool.input || {};
  const fmt = inp.format || 'text';
  const key = inp.primary_key || 'documents';

  if (fmt === 'json'){
    try { body[key] = JSON.parse(text); }
    catch(err){ toast(`输入不是合法 JSON：${err.message}`); return; }
  } else if (fmt === 'auto'){
    // Try JSON first, fall back to raw text under '_documents'
    try { body[key] = JSON.parse(text); }
    catch(_){ body[key] = text; }
  } else {
    body[key] = text;
  }

  // Run options (e.g. dry_run for step6)
  (tool.run_options || []).forEach(o => {
    const el = document.getElementById('ro-' + o.key);
    if (el) body[o.key] = el.checked;
  });

  // Always use the user's selected model/effort/thinking — applied to all substeps.
  // The per-prompt model_tier in frontmatter is now reference-only.
  const m = document.getElementById('sel-model').value;
  const ef = document.getElementById('sel-effort').value;
  const th = document.getElementById('sel-thinking').value;
  if (m) body.__model = m;
  if (ef) body.__effort = ef;
  if (th) body.__thinking = th;
  // Optional per-substep filter — only sent when the tool supports it
  const enabled = getEnabledSubsteps();
  if (enabled !== null){
    if (enabled.length === 0){
      toast('至少勾选一个子步骤');
      return;
    }
    if (enabled.length < tool.prompts.length){
      body.__enabled_substeps = enabled;
    }
  }

  const btn = document.getElementById('btn-run');
  btn.disabled = true; btn.innerHTML = '提交中…';
  let started = false;
  try {
    const r = await fetch(`/api/tools/${tool.id}/run`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body),
    });
    // 412 = 认证未配置 — 给清晰指引而不是丢一坨 JSON
    if (r.status === 412){
      const errBody = await r.json().catch(()=>({}));
      const msg = errBody.detail || '需要先到设置页选择连接方式';
      if (confirm(`无法启动:${msg}\n\n是否现在跳到设置页?`)){
        location.href = '/settings#auth';
      }
      return;
    }
    if (r.status === 409){
      // 409 = 已有正在运行的同 tool 任务 — 提示用户等待
      const errBody = await r.json().catch(()=>({}));
      toast(errBody.detail || '该工具已被其他人触发,请等它完成');
      return;
    }
    const res = await r.json();
    if (!res.run_id){ toast('启动失败:' + JSON.stringify(res)); return; }
    started = true;
    currentRunId = res.run_id;
    history.replaceState(null, '', `?run=${res.run_id}`);
    // reset tab state for new run
    _activeReportTab = null;
    window._userSwitchedTab = false;
    _lastLogCount = 0;
    startPolling(res.run_id);
    document.getElementById('run-area').scrollIntoView({behavior:'smooth', block:'start'});
    // 启动成功 — 按钮维持锁定状态(显示"运行中"),由 pollActiveStatus 接管最终解锁
    btn.disabled = true; btn.innerHTML = '<span>运行中</span>';
    // 立刻拉一次 active 状态确保 UI 同步(也避免 3s 间隔的空窗期能再次点击)
    if (typeof pollActiveStatus === 'function') pollActiveStatus();
  } catch(err){
    toast('启动请求出错: ' + err.message);
  } finally {
    if (!started){
      // 没成功启动 — 解锁让用户能修正后重试
      btn.disabled = false; btn.innerHTML = '▶ 运行<kbd>⌘↵</kbd>';
    }
  }
}

document.getElementById('btn-run').onclick = runTool;
document.addEventListener('keydown', e => {
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter'){ e.preventDefault(); runTool(); }
});
// 右侧 56px 窄条上的 ▶ 圆按钮也能触发运行
document.addEventListener('click', e => {
  const t = e.target;
  if (t && (t.id === 'play-circle' || (t.closest && t.closest('#play-circle')))) {
    runTool();
  }
});

// ============== 全局运行状态:不管谁触发,不管刷不刷新,都能看到 "运行中" ==============
// 后端 /api/tools/{tool_id}/active 返回当前是否有任意用户在跑这个工具。
// 前端每 3s 轮询一次:active=true → 锁按钮 + "运行中";active=false → 解锁。
let _activeStatusTimer = null;
let _lastActiveSig = null;

async function pollActiveStatus(){
  if (!tool || !tool.id) return;
  let r;
  try {
    r = await fetch(`/api/tools/${tool.id}/active`).then(x => x.json());
  } catch(_) { return; }
  const btn = document.getElementById('btn-run');
  if (!btn) return;
  const sig = r.active ? ('active:' + (r.run_id || '?')) : 'idle';
  if (sig === _lastActiveSig) return;  // 没变化就不更新
  _lastActiveSig = sig;
  if (r.active){
    btn.disabled = true;
    const owner = r.owner_username ? ` · 由 ${escapeHtml(r.owner_username)} 触发` : '';
    btn.innerHTML = `<span>运行中</span><span style="opacity:.7;font-size:11px;margin-left:6px">${owner}</span>`;
    btn.title = (r.progress || '') + (owner ? '\n' + r.owner_username : '');
  } else {
    btn.disabled = false;
    btn.innerHTML = '▶ 运行<kbd>⌘↵</kbd>';
    btn.title = '';
  }
}

function startActiveStatusPolling(){
  pollActiveStatus();  // 立即拉一次,页面加载完就反映当前状态
  if (_activeStatusTimer) clearInterval(_activeStatusTimer);
  _activeStatusTimer = setInterval(pollActiveStatus, 3000);
}

function startPolling(runId){
  if (pollTimer) clearInterval(pollTimer);
  if (elapsedTimer) clearInterval(elapsedTimer);
  poll(runId);
  pollTimer = setInterval(()=>poll(runId), 1500);
  elapsedTimer = setInterval(updateElapsed, 1000);
}
async function poll(runId){
  // 内存里没找着先回退看磁盘:URL 带 ?run=xxx 的"分享 / 回看历史"场景
  // 之前直接报 404 → 顶栏 UNDEFINED + 暂无日志,看上去像报告丢了。
  try {
    const resp = await fetch(`/api/tools/runs/${runId}`);
    if (resp.status === 404){
      clearInterval(pollTimer); pollTimer = null;
      clearInterval(elapsedTimer); elapsedTimer = null;
      await loadHistoryFromDisk(runId);
      return;
    }
    if (!resp.ok) throw new Error('http ' + resp.status);
    const r = await resp.json();
    currentRun = r;
    renderRun(r);
    if (r.status === 'succeeded' || r.status === 'failed'){
      clearInterval(pollTimer); pollTimer = null;
      clearInterval(elapsedTimer); elapsedTimer = null;
    }
  } catch(e){
    clearInterval(pollTimer); pollTimer = null;
    clearInterval(elapsedTimer); elapsedTimer = null;
    // 网络/JSON 错误也尝试从磁盘读 — 如果是 history 链接至少能渲染。
    try { await loadHistoryFromDisk(runId); } catch(_){}
  }
}

async function loadHistoryFromDisk(runId){
  // 把 /api/reports/{run_id} 的返回包成 poll 已经认识的 run 形状再喂给 renderRun。
  let payload;
  try {
    const resp = await fetch('/api/reports/' + encodeURIComponent(runId));
    if (!resp.ok) throw new Error('http ' + resp.status);
    payload = await resp.json();
  } catch(e){
    const area = document.getElementById('run-area');
    if (area) {
      area.innerHTML = `<div class="run-empty">报告未找到 — 可能服务重启后内存清掉了，且磁盘也没保存。<br><a href="/reports">去 /reports 查找</a></div>`;
    }
    const rsl = document.getElementById('run-status-label');
    const rsd = document.getElementById('run-status-dot');
    if (rsl) rsl.textContent = '未找到';
    if (rsd) rsd.className = 'live-dot failed';
    return;
  }
  // 磁盘 payload: {source, run_id, tool_id, report, report_path}
  const report = payload.report || {};
  const meta = report.meta || {};
  const finalizedAt = meta.produced_at_epoch || ((Date.now()/1000) - 0.1);
  const fakeRun = {
    run_id: payload.run_id || runId,
    tool_id: payload.tool_id || (tool && tool.id) || '',
    status: 'succeeded',
    progress: '历史报告',
    started_at: finalizedAt,
    finished_at: finalizedAt,
    report,
    logs: [],
    usage: meta.usage || {},
    project_code: meta.project_code,
    project_name: meta.project_name,
    __historical: true,
  };
  currentRun = fakeRun;
  renderRun(fakeRun);
}
function updateElapsed(){
  if (!currentRun || currentRun.finished_at) return;
  const e = Date.now()/1000 - currentRun.started_at;
  document.getElementById('s-elapsed').textContent = e.toFixed(1) + 's';
  document.getElementById('s-elapsed-wrap').style.display = '';
}
function gateClass(action){
  const a = String(action || '').toLowerCase();
  if (a.includes('reject')) return 'reject';
  if (a.includes('proceed') || a.includes('approve')) return 'proceed';
  return 'warn';
}
// Track active tab so re-renders preserve user choice. Default rules:
//   running → 'logs'   succeeded → 'report'   failed → 'logs'
let _activeReportTab = null;

function renderRun(r){
  // 有运行任务 → 展开右侧运行面板
  const grid = document.querySelector('.runner-grid');
  if (grid) grid.classList.remove('idle');
  // Top-bar status + token/cost stats (unchanged)
  document.getElementById('tb-sep').style.display = '';
  document.getElementById('tb-status').innerHTML = `<span class="status-pill ${r.status}">${r.status}</span>`;
  const u = r.usage || {};
  document.getElementById('s-in').textContent = u.input_tokens != null ? u.input_tokens.toLocaleString() : '—';
  document.getElementById('s-out').textContent = u.output_tokens != null ? u.output_tokens.toLocaleString() : '—';
  document.getElementById('s-cache').textContent = u.cache_read_tokens != null ? u.cache_read_tokens.toLocaleString() : '—';
  document.getElementById('s-cost').textContent = u.cost_usd != null ? u.cost_usd.toFixed(4) : '—';
  if (r.finished_at){
    document.getElementById('s-elapsed').textContent = (r.finished_at - r.started_at).toFixed(1) + 's';
    document.getElementById('s-elapsed-wrap').style.display = '';
  }
  // Reveal stats once we have a run (idle: hidden)
  const stats = document.getElementById('tb-stats');
  if (stats) stats.style.display = 'flex';
  // Right-panel header: live status dot + label + meta
  const rsd = document.getElementById('run-status-dot');
  const rsl = document.getElementById('run-status-label');
  const rsm = document.getElementById('run-status-meta');
  if (rsd && rsl) {
    rsd.className = 'live-dot ' + r.status;
    const labels = {queued:'排队中', running:'执行中', succeeded:'已完成', failed:'失败'};
    rsl.textContent = labels[r.status] || r.status;
    if (rsm) {
      const elapsed = r.finished_at ? (r.finished_at - r.started_at) : (Date.now()/1000 - r.started_at);
      const min = String(Math.floor(elapsed/60)).padStart(2,'0');
      const sec = String(Math.floor(elapsed%60)).padStart(2,'0');
      rsm.textContent = (r.status === 'queued') ? '' : `${min}:${sec}${u.cost_usd != null ? ' · $'+u.cost_usd.toFixed(4) : ''}`;
    }
  }

  // Default tab choice based on status
  if (_activeReportTab === null){
    _activeReportTab = (r.status === 'succeeded') ? 'report' : 'logs';
  }
  // If just completed and still on logs, auto-switch to report
  if (r.status === 'succeeded' && _activeReportTab === 'logs' && !window._userSwitchedTab){
    _activeReportTab = 'report';
  }

  const area = document.getElementById('run-area');

  // Failed: show error + logs tab
  if (r.status === 'failed'){
    const tabs = renderReportTabs(r, 'logs');
    const errBlock = `<div class="err-view">${escapeHtml(r.traceback || r.error || 'failed')}</div>`;
    area.innerHTML = tabs.head + errBlock + tabs.bodyOf('logs', r);
    bindTabClicks(r);
    autoScrollLogs(r);
    return;
  }

  // Still running: tabs (logs default + step list at top)
  if (r.status !== 'succeeded'){
    const tabs = renderReportTabs(r, _activeReportTab);
    const stepList = renderStepList(r);
    area.innerHTML = tabs.head + stepList + tabs.bodyOf(_activeReportTab, r);
    bindTabClicks(r);
    autoScrollLogs(r);
    return;
  }

  // Succeeded — show structured report by default, logs+JSON in tabs
  const tabs = renderReportTabs(r, _activeReportTab);
  area.innerHTML = tabs.head + tabs.bodyOf(_activeReportTab, r);
  bindTabClicks(r);
  bindReportInteractions();
  autoScrollLogs(r);
  // 报告渲染后异步把所有 <img data-screenshot-filename=...> 的 src 替换成 data: URI
  inlineScreenshotsInArea(area);
}

// 把当前 area 下所有标记了 data-screenshot-filename 的 img,异步 fetch 内容
// 然后把 src 替换为 base64 data URI — 报告就完全自包含了。
// 已经是 data: 开头的跳过。同一文件名缓存避免重复请求。
const _screenshotInlineCache = {};
async function inlineScreenshotsInArea(rootEl){
  if (!rootEl) return;
  const imgs = rootEl.querySelectorAll('img[data-screenshot-filename]');
  if (!imgs.length) return;
  await Promise.all(Array.from(imgs).map(async img => {
    if (img.src && img.src.startsWith('data:')) return;
    const fn = img.dataset.screenshotFilename;
    if (!fn) return;
    if (_screenshotInlineCache[fn]) {
      img.src = _screenshotInlineCache[fn];
      return;
    }
    try {
      const resp = await fetch('/api/screenshots/' + encodeURIComponent(fn));
      if (!resp.ok) return;
      const blob = await resp.blob();
      const reader = new FileReader();
      const dataUri = await new Promise((res, rej) => {
        reader.onload = () => res(reader.result);
        reader.onerror = () => rej(reader.error);
        reader.readAsDataURL(blob);
      });
      _screenshotInlineCache[fn] = dataUri;
      img.src = dataUri;
    } catch(_){}
  }));
}

function renderStepList(r){
  let html = '<div class="step-list">';
  tool.prompts.forEach(p => {
    let cls='', m='○';
    if (r.status === 'succeeded'){ cls='done'; m='✓'; }
    else if (r.status === 'running'){ cls='running'; m='◔'; }
    const shortP = String(p).split('.').pop();
    html += `<div class="step-row ${cls}"><span class="marker">${m}</span><span class="name" title="${p}">#${shortP}</span><span class="info">${cls === 'done' ? 'OK' : (cls === 'running' ? '执行中' : '')}</span></div>`;
  });
  html += '</div>';
  return html;
}

function renderReportTabs(r, active){
  // step2 测试用例工具:产出就是用例,不是"报告" —— tab 叫"用例",只给 Excel 下载。
  const isStep2 = (typeof tool !== 'undefined' && tool && tool.id === 'step2');
  const reportTabLabel = isStep2 ? '用例' : '报告';
  const dlButtons = isStep2
    ? `${(r.report && (r.report.cases||[]).length) ? `<button class="export" data-action="download-xlsx">↓ 下载 Excel 用例表</button>` : ''}`
    : `${r.report ? `<button class="export" data-action="download-html">↓ HTML 报告</button>` : ''}
       ${r.report ? `<button class="export" data-action="download-md">↓ Markdown</button>` : ''}
       ${r.report ? `<button class="export" data-action="download-json">↓ JSON</button>` : ''}
       ${r.report ? `<button class="export" data-action="copy">⧉ 复制</button>` : ''}`;
  const head = `<div class="report-tabs">
    <button class="${active==='report'?'active':''}" data-tab="report">${reportTabLabel}</button>
    <button class="${active==='logs'?'active':''}" data-tab="logs">日志 ${(r.logs||[]).length ? '('+r.logs.length+')' : ''}</button>
    <button class="${active==='json'?'active':''}" data-tab="json">{ } 原始 JSON</button>
    <span class="spacer"></span>
    ${dlButtons}
  </div>`;
  const bodyOf = (tab, run) => {
    if (tab === 'logs') return renderLogs(run) || '<div class="run-empty">暂无日志</div>';
    if (tab === 'json'){
      if (!run.report) return '<div class="run-empty">报告尚未生成</div>';
      return `<div class="json-view"><pre>${escapeHtml(JSON.stringify(run.report, null, 2))}</pre></div>`;
    }
    // report
    if (!run.report) return '<div class="run-empty">报告尚未生成</div>';
    return renderHtmlReport(run);
  };
  return { head, bodyOf };
}

function bindTabClicks(r){
  document.querySelectorAll('#run-area .report-tabs button[data-tab]').forEach(btn => {
    btn.onclick = () => {
      _activeReportTab = btn.dataset.tab;
      window._userSwitchedTab = true;
      renderRun(r);
    };
  });
  document.querySelectorAll('#run-area .report-tabs button[data-action]').forEach(btn => {
    const action = btn.dataset.action;
    if (action === 'download-json'){
      btn.onclick = () => downloadBlob(
        JSON.stringify(r.report, null, 2),
        `${tool.id}_${r.run_id.slice(0,8)}.json`,
        'application/json'
      );
    } else if (action === 'download-html'){
      btn.onclick = () => downloadBlob(
        buildStandaloneHtml(r),
        `${tool.id}_${r.run_id.slice(0,8)}.html`,
        'text/html;charset=utf-8'
      );
    } else if (action === 'download-md'){
      btn.onclick = () => downloadBlob(
        buildMarkdownReport(r),
        `${tool.id}_${r.run_id.slice(0,8)}.md`,
        'text/markdown;charset=utf-8'
      );
    } else if (action === 'download-xlsx'){
      // Excel 用例表 — 服务端 openpyxl 生成,走 export.xlsx 端点
      btn.onclick = async () => {
        try {
          const resp = await fetch(`/api/reports/${r.run_id}/export.xlsx`, {credentials:'same-origin'});
          if (!resp.ok){
            const d = await resp.json().catch(()=>({}));
            toast('Excel 导出失败:' + (d.detail || resp.status));
            return;
          }
          const blob = await resp.blob();
          const a = document.createElement('a');
          a.href = URL.createObjectURL(blob);
          a.download = `${tool.id}_${r.run_id.slice(0,8)}_testcases.xlsx`;
          document.body.appendChild(a); a.click(); a.remove();
          setTimeout(()=>URL.revokeObjectURL(a.href), 1000);
        } catch(e){ toast('Excel 导出出错:' + e.message); }
      };
    } else if (action === 'copy'){
      btn.onclick = async () => {
        try {
          await navigator.clipboard.writeText(JSON.stringify(r.report, null, 2));
          toast('已复制完整 JSON');
        } catch(e){ toast('复制失败'); }
      };
    }
  });
}

function downloadBlob(content, filename, mime){
  const blob = new Blob([content], {type: mime});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  toast(`已下载 ${filename}`);
}

function bindReportInteractions(){
  document.querySelectorAll('#run-area .report-sub-head').forEach(h => {
    h.onclick = () => h.parentElement.classList.toggle('open');
  });
}

// === Exporters: standalone HTML & Markdown ===

function buildStandaloneHtml(r){
  // Lift the same renderer + the on-page CSS we use in the live preview.
  // Two notable adjustments: all sub-cards are open, and we inline a print-friendly
  // stylesheet so the file looks reasonable when opened in another browser.
  const body = renderHtmlReport(r, {expandAll: true});
  const title = `${tool.name} 报告 — ${r.run_id.slice(0,8)}`;
  const css = `
:root{--bg:#fff;--surface:#f8fafc;--surface-2:#eef2f7;--line:#dde3ec;--line-2:#cbd5e1;
  --fg:#1f2937;--fg-2:#4b5563;--fg-3:#6b7280;--fg-4:#9ca3af;
  --ac:#0d9488;--ac-2:#0891b2;--warn:#b45309;--ok:#059669;--bad:#dc2626;
  --mono:ui-monospace,SFMono-Regular,"JetBrains Mono",Menlo,monospace;
  --sans:-apple-system,BlinkMacSystemFont,"SF Pro Display","SF Pro Text","PingFang SC","Helvetica Neue",Arial,sans-serif;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font-family:var(--sans);
  line-height:1.6;padding:32px;max-width:1080px;margin:0 auto}
h1{font-size:22px;letter-spacing:-.02em;margin:0 0 12px}
.meta-row{font-family:var(--mono);font-size:12px;color:var(--fg-3);margin-bottom:18px;
  padding:10px 14px;background:var(--surface);border-radius:8px;border:1px solid var(--line)}
code{background:var(--surface);padding:1px 6px;border-radius:3px;color:var(--ac-2);
  border:1px solid var(--line);font-family:var(--mono);font-size:11.5px}
.report-hero{display:flex;align-items:flex-start;gap:14px;padding:18px 22px;
  border:1px solid var(--line);border-radius:10px;background:var(--surface);margin-bottom:18px}
.report-hero .report-icon{font-size:24px;width:42px;height:42px;border-radius:8px;
  background:#fff;border:1px solid var(--line-2);display:grid;place-items:center}
.report-hero h4{margin:0;font-size:16px;font-weight:600}
.report-hero .meta{font-family:var(--mono);font-size:11.5px;color:var(--fg-3);margin-top:5px}
.report-project{margin-top:8px;display:flex;flex-wrap:wrap;gap:14px;padding:6px 0 0;border-top:1px dashed var(--line);font-size:12.5px;align-items:center}
.report-project .lbl{color:var(--fg-3);font-size:10.5px;letter-spacing:.04em;text-transform:uppercase;font-weight:500;margin-right:2px}
.report-project code{background:var(--surface-2);padding:2px 7px;border-radius:4px;color:var(--fg);font-family:var(--mono);font-size:12px;border:none}
.report-project .val{color:var(--fg);font-weight:500}
.stat-pills{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
.stat-pills .pill{font-family:var(--mono);font-size:11px;padding:3px 9px;border-radius:4px;
  background:#fff;border:1px solid var(--line);color:var(--fg-2)}
.stat-pills .pill .v{color:var(--ac);font-weight:600}
.gate-banner{padding:14px 18px;display:flex;gap:12px;border-radius:10px;
  border:1px solid var(--line);margin-bottom:16px}
.gate-banner.proceed{background:rgba(5,150,105,.06);border-color:rgba(5,150,105,.3)}
.gate-banner.reject{background:rgba(220,38,38,.06);border-color:rgba(220,38,38,.3)}
.gate-banner.warn{background:rgba(180,83,9,.06);border-color:rgba(180,83,9,.3)}
.gate-banner .badge{padding:3px 10px;border-radius:4px;font-family:var(--mono);font-size:11px;
  font-weight:700;text-transform:uppercase;letter-spacing:.05em;flex-shrink:0;margin-top:1px}
.gate-banner.proceed .badge{background:rgba(5,150,105,.15);color:var(--ok)}
.gate-banner.reject .badge{background:rgba(220,38,38,.15);color:var(--bad)}
.gate-banner.warn .badge{background:rgba(180,83,9,.15);color:var(--warn)}
.gate-banner .reasons{font-family:var(--mono);font-size:12.5px;color:var(--fg-2);line-height:1.6}
.gate-banner .reasons div{margin-top:4px}
.report-sub{border:1px solid var(--line);border-radius:10px;margin-bottom:14px;overflow:hidden;
  page-break-inside:avoid}
.report-sub-head{display:flex;align-items:center;gap:10px;padding:13px 18px;
  background:var(--surface);font-size:14px;border-bottom:1px solid var(--line)}
.report-sub-twirl{display:none}
.report-sub-num{font-family:var(--mono);font-size:11px;font-weight:700;color:#fff;
  background:var(--ac);width:22px;height:22px;border-radius:50%;
  display:inline-grid;place-items:center;flex-shrink:0}
.report-sub-name{font-weight:600}
.report-sub-stats{margin-left:auto;font-family:var(--mono);font-size:11px;color:var(--fg-3);display:flex;gap:6px}
.report-sub-stats .chip{padding:2px 8px;border-radius:3px;background:#fff;border:1px solid var(--line)}
.report-sub-body{padding:16px 22px;background:#fff}
.report-sub:not(.open) .report-sub-body{display:none}
.report-kv{display:grid;grid-template-columns:max-content 1fr;gap:7px 16px;margin:0;font-size:13px}
.report-kv dt{color:var(--fg-3);font-family:var(--mono);font-size:12px;align-self:start}
.report-kv dd{margin:0;color:var(--fg);min-width:0}
.report-table{width:100%;border-collapse:collapse;font-size:12px;margin:8px 0;
  border:1px solid var(--line);border-radius:6px;overflow:hidden}
.report-table th{font-family:var(--mono);font-size:11px;text-align:left;padding:8px 10px;
  background:var(--surface-2);color:var(--fg-3);text-transform:uppercase;letter-spacing:.05em;
  font-weight:600;border-bottom:1px solid var(--line);white-space:nowrap}
.report-table td{padding:8px 10px;border-bottom:1px solid var(--line);
  font-family:var(--mono);font-size:11.5px;color:var(--fg);vertical-align:top;
  word-break:break-word}
.report-table tr:last-child td{border-bottom:none}
.sev{font-family:var(--mono);font-size:10px;font-weight:700;padding:1px 7px;border-radius:3px;
  text-transform:uppercase;letter-spacing:.05em}
.sev-critical{background:#fee2e2;color:#991b1b}
.sev-high{background:#fee2e2;color:#dc2626}
.sev-major{background:#fee2e2;color:#dc2626}
.sev-medium{background:#fef3c7;color:#92400e}
.sev-low{background:#f3f4f6;color:var(--fg-2)}
.sev-info{background:#cffafe;color:#0e7490}
.confbar{display:inline-flex;align-items:center;gap:6px}
.confbar .track{width:80px;height:6px;border-radius:3px;background:var(--line);overflow:hidden}
.confbar .fill{height:100%;background:var(--ac)}
.confbar .pct{color:var(--ac);font-weight:600;font-family:var(--mono);font-size:11px}
ul.report-list{margin:0;padding-left:20px;font-size:13px;line-height:1.75}
.empty-array{color:var(--fg-4);font-family:var(--mono);font-size:11px;font-style:italic}
details.report-detail{margin:4px 0}
details.report-detail summary{cursor:pointer;font-family:var(--mono);font-size:11.5px;color:var(--ac)}
details.report-detail[open] > div{margin:6px 0 6px 16px;padding:8px 12px;
  background:var(--surface);border-left:2px solid var(--line);border-radius:4px}
.print-footer{margin-top:32px;padding-top:14px;border-top:1px solid var(--line);
  font-family:var(--mono);font-size:11px;color:var(--fg-3);text-align:center}
@media print{body{padding:0}.report-sub{page-break-inside:avoid}}
`;
  const meta = (r.report && r.report.meta) || {};
  const generatedAt = new Date().toLocaleString('zh-CN', {hour12:false});
  return `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>${escapeHtml(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@300;400;500;600;700&family=Noto+Sans+SC:wght@300;400;500;600&family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
  <style>${css}
  /* === Standalone report nav === */
  .standalone-nav{position:fixed;top:14px;left:14px;right:14px;display:flex;
    justify-content:space-between;pointer-events:none;z-index:50}
  .standalone-nav button{pointer-events:auto;background:rgba(15,17,21,.92);
    color:#e5e7eb;border:1px solid rgba(255,255,255,.12);border-radius:8px;
    padding:8px 14px;font-size:13px;font-weight:500;cursor:pointer;
    backdrop-filter:saturate(180%) blur(12px);
    -webkit-backdrop-filter:saturate(180%) blur(12px);
    box-shadow:0 4px 14px rgba(0,0,0,.18);
    display:inline-flex;align-items:center;gap:6px;
    transition:background .15s,transform .12s}
  .standalone-nav button:hover{background:rgba(15,17,21,.98);transform:translateY(-1px)}
  .standalone-nav button:active{transform:translateY(0)}
  @media print{.standalone-nav{display:none}}
  body{padding-top:60px}
  </style>
</head>
<body>
  <div class="standalone-nav">
    <button onclick="(function(){if(history.length>1){history.back();}else if(window.opener){window.close();}else{window.scrollTo({top:0,behavior:'smooth'});}})()" title="返回上一页 / 滚动到顶部">← 返回</button>
    <button onclick="window.scrollTo({top:0,behavior:'smooth'})" title="返回顶部">↑ 顶部</button>
  </div>
  <h1>${escapeHtml(tool.name)} · 分析报告</h1>
  <div class="meta-row">
    生成时间：${generatedAt}
    · run id：<code>${r.run_id}</code>
    · 模型：<code>${escapeHtml(meta.model_id || '?')}</code>
  </div>
  ${body}
  <div class="print-footer">由 天枢·裁决生成 · ${escapeHtml(tool.id)} · ${new Date().toISOString()}</div>
</body>
</html>`;
}

function buildMarkdownReport(r){
  // Produce a clean markdown version for sharing/version control.
  const rep = r.report || {};
  const meta = rep.meta || {};
  const u = r.usage || {};
  const lines = [];
  lines.push(`# ${tool.name} · 分析报告`);
  lines.push('');
  lines.push(`- **生成时间**：${new Date().toLocaleString('zh-CN', {hour12:false})}`);
  lines.push(`- **Run ID**：\`${r.run_id}\``);
  lines.push(`- **模型**：\`${meta.model_id || '?'}\``);
  if (r.finished_at && r.started_at) lines.push(`- **耗时**：${(r.finished_at - r.started_at).toFixed(1)}s`);
  if (u.cost_usd != null) lines.push(`- **成本**：$${u.cost_usd}`);
  if (u.output_tokens != null) lines.push(`- **输出 tokens**：${u.output_tokens.toLocaleString()}`);
  lines.push('');

  // Gate
  if (rep.gate_decision){
    const g = rep.gate_decision;
    lines.push(`## ⚠️ 闸门决策：\`${g.action}\``);
    (g.reasons || []).forEach(rs => lines.push(`- ${rs}`));
    lines.push('');
  }

  // Substeps
  let idx = 0;
  for (const sid of tool.prompts){
    idx++;
    const data = (rep.substeps || {})[sid];
    const title = (tool._substepNames && tool._substepNames[sid]) || extractTitle(data) || `子分析 ${idx}`;
    lines.push(`## ${idx}. ${title}`);
    if (data == null){
      lines.push('_已跳过_');
      lines.push('');
      continue;
    }
    lines.push('');
    lines.push('```json');
    lines.push(JSON.stringify(data, null, 2));
    lines.push('```');
    lines.push('');
  }
  return lines.join('\n');
}

// === Structured report renderer ===

function buildExecutiveSummary(rep, tool){
  // 收集所有 substep 输出里的 issue/case 节点(向后兼容旧报告)
  const walkedIssues = [], walkedCases = [];
  function walk(x){
    if (x && typeof x === 'object'){
      if (Array.isArray(x)){ x.forEach(walk); return; }
      const hasCase = ('expected' in x || 'scenario' in x) && ('id' in x);
      const hasIssue = 'severity' in x && (('issue' in x) || ('title' in x) || ('description' in x) || ('name' in x));
      if (hasCase) walkedCases.push(x);
      else if (hasIssue) walkedIssues.push(x);
      Object.values(x).forEach(walk);
    }
  }
  walk(rep.substeps || {});

  // 顶层契约字段(新报告)优先,缺失则 fallback 到 walk 结果
  const topIssues = Array.isArray(rep.issues) ? rep.issues : walkedIssues;
  const topCases = Array.isArray(rep.cases) ? rep.cases : walkedCases;
  const topRisks = Array.isArray(rep.risks) ? rep.risks : [];
  const topBlockers = Array.isArray(rep.blockers) ? rep.blockers : [];

  // severity & priority 排序权重
  const sevRank = {critical:0, high:1, medium:2, low:3, info:4};
  const priRank = {P0:0, P1:1, P2:2, P3:3};
  function getSev(x){ return String(x.severity||'medium').toLowerCase(); }
  function getPri(x){ return String(x.priority||'P2').toUpperCase(); }

  // 整形 issues
  const naturalIssues = topIssues.map(it => ({
    issue_id: String(it.issue_id || it.id || ''),
    title: String(it.title || it.name || it.issue || it.description || '未命名问题').slice(0, 200),
    severity: getSev(it),
    priority: getPri(it),
    module: String(it.module || it.endpoint || it.file_path || it.location || it.viewport || it.page || it.viewport_filename || '').slice(0, 200),
    current: String(it.current_behavior || it.current || it.observed || it.description || it.issue || '').slice(0, 800),
    expected: String(it.expected_behavior || it.expected || it.requirement || '').slice(0, 800),
    fix: String(it.fix_suggestion || it.fix || it.recommendation || it.suggestion || it.remediation || '').slice(0, 1000),
    repro: Array.isArray(it.reproduce_steps) ? it.reproduce_steps : (it.reproduce_steps ? [String(it.reproduce_steps)] : []),
    accept: String(it.acceptance_criteria || it.acceptance || it.verify || '').slice(0, 600),
    cases: Array.isArray(it.related_test_cases) ? it.related_test_cases : (it.related_test_cases ? [String(it.related_test_cases)] : []),
    owner: String(it.owner_role || it.owner || it.assignee || '').toLowerCase(),
    hours: it.estimated_hours || it.effort || null,
    impact: String(it.impact_scope || it.impact || '').slice(0, 400),
    evidence: String(it.evidence || it.source || '').slice(0, 400),
  })).sort((a,b) => {
    const sa = sevRank[a.severity] ?? 9, sb = sevRank[b.severity] ?? 9;
    if (sa !== sb) return sa - sb;
    const pa = priRank[a.priority] ?? 9, pb = priRank[b.priority] ?? 9;
    return pa - pb;
  }).slice(0, 60);

  // 整形 risks (新报告是对象; 旧报告 gate.reasons 是字符串数组)
  let naturalRisks = topRisks.map(r => {
    if (typeof r === 'string') return {title: r, impact:'', why:'', severity:'medium'};
    return {
      id: String(r.id || ''),
      title: String(r.title || r.name || r.risk || '未命名风险').slice(0, 200),
      impact: String(r.impact || r.affects || '').slice(0, 400),
      why: String(r.why || r.reason || r.detail || '').slice(0, 400),
      severity: getSev(r),
    };
  });
  // fallback: 旧报告从 gate_decision.reasons 抽 risks
  if (!naturalRisks.length){
    const gate = rep.gate_decision || {};
    if (Array.isArray(gate.reasons)){
      naturalRisks = gate.reasons.filter(Boolean).map(s => ({title:String(s), impact:'', why:'', severity:'medium'}));
    }
  }

  // 整形 blockers
  const naturalBlockers = topBlockers.map(b => ({
    id: String(b.id || ''),
    title: String(b.title || b.name || '未命名阻碍').slice(0, 200),
    why_blocking: String(b.why_blocking || b.reason || b.why || '').slice(0, 500),
    what_to_unblock: String(b.what_to_unblock || b.action || b.fix || '').slice(0, 500),
    owner_role: String(b.owner_role || b.owner || '').toLowerCase(),
    hours: b.estimated_hours || b.effort || null,
  }));

  // 汇总 severity & priority 计数(基于排序后的 issues + walkedCases)
  const sev = {critical:0, high:0, medium:0, low:0, info:0};
  naturalIssues.forEach(it => { if (sev[it.severity] !== undefined) sev[it.severity]++; });
  const pri = {P0:0, P1:0, P2:0, P3:0, 其他:0};
  topCases.forEach(c => {
    const p = String(c.priority||'').toUpperCase();
    if (pri[p] !== undefined) pri[p]++;
    else if (p) pri.其他++;
  });

  // 整形 cases (按 priority 排序)
  const naturalCases = topCases.map(c => ({
    id: String(c.id || c.case_id || c.tc_id || ''),
    title: String(c.title || c.name || c.scenario || '').slice(0, 200),
    priority: String(c.priority || 'P2').toUpperCase(),
    type: String(c.type || c.kind || '').toLowerCase(),
    status: String(c.status || 'designed').toLowerCase(),
    automation: String(c.automation_tag || c.automation || '').toLowerCase(),
    preconditions: String(c.preconditions || c.precondition || '').slice(0, 300),
    steps: Array.isArray(c.steps) ? c.steps : (c.steps ? [String(c.steps)] : []),
    expected: String(c.expected || c.expected_result || '').slice(0, 400),
  })).sort((a,b) => {
    const pa = priRank[a.priority] ?? 9, pb = priRank[b.priority] ?? 9;
    return pa - pb;
  }).slice(0, 200);

  // verdict / verdict_summary
  const gate = rep.gate_decision || {};
  const action = String(gate.action || '').toLowerCase();
  let verdict, vlevel;
  if (rep.verdict){
    const v = String(rep.verdict);
    verdict = v;
    if (v.includes('不通过')) vlevel = 'fail';
    else if (v.includes('有条件') || v.includes('警告') || v.includes('部分')) vlevel = 'warn';
    else vlevel = 'pass';
  } else {
    if (action.includes('reject') || sev.critical > 0){ verdict = '不通过'; vlevel = 'fail'; }
    else if (action.includes('warn') || sev.high > 2 || naturalBlockers.length){ verdict = '有条件通过'; vlevel = 'warn'; }
    else if (!action && !naturalIssues.length && !naturalCases.length){ verdict = '未产出'; vlevel = 'skip'; }
    else { verdict = '通过'; vlevel = 'pass'; }
  }
  const verdictSummary = String(rep.verdict_summary || '').slice(0, 200);

  return {
    verdict, vlevel, verdictSummary,
    risks: naturalRisks,
    blockers: naturalBlockers,
    issues: naturalIssues,
    cases: naturalCases,
    casesCount: naturalCases.length,
    pri, sev,
    agent: tool ? tool.name : '',
  };
}

function renderExecutiveSummary(s){
  const vmap = {pass:['✅','通过','ok'], warn:['⚠️','有条件通过','warn'], fail:['❌','不通过','bad'], skip:['—','未产出','skip']};
  const [vicon, vtext, vcls] = vmap[s.vlevel] || ['·','—','skip'];
  const sevMap = {critical:'CRITICAL', high:'HIGH', medium:'MEDIUM', low:'LOW', info:'INFO'};
  const priMap = {P0:'P0', P1:'P1', P2:'P2', P3:'P3'};
  const ownerMap = {backend:'后端', frontend:'前端', product:'产品', test:'测试',
                    devops:'运维', security:'安全', data:'数据'};
  const sevTotal = s.sev.critical + s.sev.high + s.sev.medium + s.sev.low + s.sev.info;
  const priTotal = s.pri.P0 + s.pri.P1 + s.pri.P2 + s.pri.P3 + s.pri.其他;

  // ── 严重度可视化条 ──
  function sevBar(){
    if (!sevTotal) return '<span class="exec-muted">（暂无问题）</span>';
    const cells = ['critical','high','medium','low','info'].map(k => {
      const n = s.sev[k]; if (!n) return '';
      const pct = (n / sevTotal * 100).toFixed(0);
      return `<span class="sev-bar-seg sev-bar-${k}" style="flex:${n}" title="${sevMap[k]} · ${n} 个 (${pct}%)">${n}</span>`;
    }).filter(Boolean).join('');
    return `<div class="sev-bar">${cells}</div>`;
  }
  function priBar(){
    if (!priTotal) return '';
    const cells = ['P0','P1','P2','P3'].map(k => {
      const n = s.pri[k]; if (!n) return '';
      return `<span class="pri-bar-seg pri-bar-${k}" style="flex:${n}" title="${k} · ${n} 条">${k}: ${n}</span>`;
    }).filter(Boolean).join('');
    return `<div class="pri-bar">${cells}</div>`;
  }

  let html = '';

  // ── ① 测试结论(verdict + verdict_summary 大字)──
  html += `<div class="exec-block exec-verdict-block">
    <div class="exec-head"><span class="exec-num">①</span><h3>测试结论</h3></div>
    <div class="verdict ${vcls}"><span class="verdict-icon">${vicon}</span><span>${escapeHtml(vtext)}</span></div>
    ${s.verdictSummary ? `<div class="verdict-summary">${escapeHtml(s.verdictSummary)}</div>` : ''}
    <div class="exec-kpis">
      <div class="exec-kpi"><span class="exec-kpi-num">${sevTotal}</span><span class="exec-kpi-lbl">问题总数</span></div>
      <div class="exec-kpi"><span class="exec-kpi-num">${s.sev.critical + s.sev.high}</span><span class="exec-kpi-lbl">需立即处理</span></div>
      <div class="exec-kpi"><span class="exec-kpi-num">${s.blockers.length}</span><span class="exec-kpi-lbl">阻碍</span></div>
      <div class="exec-kpi"><span class="exec-kpi-num">${s.casesCount}</span><span class="exec-kpi-lbl">用例</span></div>
    </div>
    ${sevBar()}
  </div>`;

  // ── ② 风险结论 ──
  let risksHtml;
  if (s.risks.length){
    risksHtml = '<div class="exec-risk-list">' + s.risks.map(r => {
      const sevBadge = r.severity ? `<span class="sev-tag sev-${r.severity}">${escapeHtml(r.severity)}</span>` : '';
      return `<div class="exec-risk-item">
        <div class="exec-risk-head">${sevBadge}<span class="exec-risk-title">${escapeHtml(r.title)}</span></div>
        ${r.impact ? `<div class="exec-risk-line"><span class="lbl">影响</span>${escapeHtml(r.impact)}</div>` : ''}
        ${r.why ? `<div class="exec-risk-line"><span class="lbl">原因</span>${escapeHtml(r.why)}</div>` : ''}
      </div>`;
    }).join('') + '</div>';
  } else {
    risksHtml = '<p class="exec-muted">（无显著风险）</p>';
  }
  html += `<div class="exec-block">
    <div class="exec-head"><span class="exec-num">②</span><h3>风险结论</h3><span class="exec-count">${s.risks.length}</span></div>
    ${risksHtml}
  </div>`;

  // ── ③ 阻碍 ──
  let blockersHtml;
  if (s.blockers.length){
    blockersHtml = '<div class="exec-blocker-list">' + s.blockers.map((b, idx) => `
      <div class="exec-blocker-item">
        <div class="exec-blocker-head">
          <span class="blocker-tag">BLOCKER</span>
          <span class="exec-blocker-title">${idx+1}. ${escapeHtml(b.title)}</span>
          ${b.id ? `<span class="meta-chip">${escapeHtml(b.id)}</span>` : ''}
          ${b.owner_role && ownerMap[b.owner_role] ? `<span class="meta-chip role">👤 ${ownerMap[b.owner_role]}</span>` : ''}
          ${b.hours ? `<span class="meta-chip">⏱ ${b.hours}h</span>` : ''}
        </div>
        ${b.why_blocking ? `<div class="exec-blocker-line"><span class="lbl">为何阻碍</span>${escapeHtml(b.why_blocking)}</div>` : ''}
        ${b.what_to_unblock ? `<div class="exec-blocker-line fix"><span class="lbl">如何解开</span>${escapeHtml(b.what_to_unblock)}</div>` : ''}
      </div>`).join('') + '</div>';
  } else {
    blockersHtml = '<p class="exec-muted">（无阻碍）</p>';
  }
  html += `<div class="exec-block">
    <div class="exec-head"><span class="exec-num">③</span><h3>阻碍</h3><span class="exec-count danger">${s.blockers.length}</span></div>
    ${blockersHtml}
  </div>`;

  // ── ④ Bug 表(按严重度+优先级排序)──
  function renderIssueCard(it, idx){
    const metaChips = [];
    if (it.issue_id) metaChips.push(`<span class="meta-chip">${escapeHtml(it.issue_id)}</span>`);
    if (it.priority) metaChips.push(`<span class="meta-chip pri-${it.priority}">${priMap[it.priority] || it.priority}</span>`);
    if (it.owner && ownerMap[it.owner]) metaChips.push(`<span class="meta-chip role">👤 ${ownerMap[it.owner]}</span>`);
    if (it.hours) metaChips.push(`<span class="meta-chip">⏱ ${it.hours}h</span>`);
    const reproHtml = it.repro.length
      ? '<ol class="repro-list">' + it.repro.map(s => `<li>${escapeHtml(s)}</li>`).join('') + '</ol>' : '';
    const casesHtml = it.cases.length
      ? `<div class="related-cases">关联用例:${it.cases.map(c => `<code>${escapeHtml(c)}</code>`).join(' ')}</div>` : '';
    return `
      <div class="exec-issue sev-${it.severity}">
        <div class="exec-issue-head">
          <span class="sev-tag sev-${it.severity}">${sevMap[it.severity] || '·'}</span>
          <span class="exec-issue-title">${idx+1}. ${escapeHtml(it.title)}</span>
        </div>
        <div class="exec-issue-meta">${metaChips.join('')}</div>
        ${it.module ? `<div class="exec-issue-loc">位置:<code>${escapeHtml(it.module)}</code></div>` : ''}
        ${it.current ? `<div class="exec-issue-section"><div class="sec-lbl">现状</div><div class="sec-body">${escapeHtml(it.current)}</div></div>` : ''}
        ${it.expected ? `<div class="exec-issue-section"><div class="sec-lbl">期望</div><div class="sec-body">${escapeHtml(it.expected)}</div></div>` : ''}
        ${it.fix ? `<div class="exec-issue-section fix"><div class="sec-lbl">修复建议</div><div class="sec-body">${escapeHtml(it.fix)}</div></div>` : ''}
        ${(reproHtml || it.accept) ? `<div class="exec-issue-section verify"><div class="sec-lbl">验收</div><div class="sec-body">${reproHtml}${it.accept ? `<div class="accept-line">验收标准：${escapeHtml(it.accept)}</div>` : ''}</div></div>` : ''}
        ${casesHtml}
        ${it.impact ? `<div class="exec-issue-impact">影响面:${escapeHtml(it.impact)}</div>` : ''}
        ${it.evidence ? `<div class="exec-issue-evidence">证据:${escapeHtml(it.evidence)}</div>` : ''}
      </div>`;
  }
  const issuesHtml = s.issues.length
    ? s.issues.map((it, i) => renderIssueCard(it, i)).join('')
    : '<p class="exec-muted">（本次未识别到具体问题）</p>';
  html += `<div class="exec-block">
    <div class="exec-head">
      <span class="exec-num">④</span>
      <h3>Bug 表</h3>
      <span class="exec-count">${s.issues.length}</span>
      <span class="exec-count-note">(按严重度 × 优先级排序)</span>
    </div>
    ${issuesHtml}
  </div>`;

  // ── ⑤ 执行用例记录(按 P0→P3)──
  let casesListHtml;
  if (s.cases.length){
    const rows = s.cases.map((c, i) => {
      const statusMap = {
        designed: ['','已设计','muted'],
        executed_pass: ['','已执行通过','ok'],
        executed_fail: ['','执行失败','bad'],
        skipped: ['','已跳过','muted'],
        blocked: ['','阻塞','bad'],
      };
      const [sIcon, sLabel, sCls] = statusMap[c.status] || ['','未定义','muted'];
      return `<tr class="case-row pri-${c.priority}">
        <td class="case-idx">${i+1}</td>
        <td><span class="pri-tag pri-${c.priority}">${priMap[c.priority]||c.priority}</span></td>
        <td><code class="case-id">${escapeHtml(c.id)}</code></td>
        <td class="case-title">${escapeHtml(c.title)}</td>
        <td>${c.type ? `<span class="case-type">${escapeHtml(c.type)}</span>` : ''}</td>
        <td>${c.automation ? `<span class="case-auto">${escapeHtml(c.automation)}</span>` : ''}</td>
        <td><span class="case-status case-status-${sCls}">${sIcon} ${sLabel}</span></td>
      </tr>`;
    }).join('');
    casesListHtml = `<div class="case-table-wrap">
      <table class="case-table">
        <thead><tr><th>#</th><th>优先级</th><th>用例 ID</th><th>用例标题</th><th>类型</th><th>自动化</th><th>状态</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
  } else if (s.casesCount){
    casesListHtml = `<div class="exec-case-total"><span class="num">${s.casesCount}</span><span class="lbl">条已生成用例(未提供详细列表)</span></div>`;
  } else {
    casesListHtml = '<p class="exec-muted">（本次未生成用例）</p>';
  }
  html += `<div class="exec-block">
    <div class="exec-head">
      <span class="exec-num">⑤</span>
      <h3>执行用例记录</h3>
      <span class="exec-count">${s.casesCount}</span>
    </div>
    ${priBar()}
    ${casesListHtml}
  </div>`;

  return html;
}

// 测试用例工具:把用例表 + Excel 下载做成报告主体
function buildTestCaseBlock(rep, runId, toolId){
  const cases = (rep && rep.cases) || [];
  if (!cases.length) return '';
  const rows = cases.map(c => {
    const stepsArr = Array.isArray(c.steps) ? c.steps : (c.steps ? [c.steps] : []);
    const steps = stepsArr.filter(Boolean).map(s => escapeHtml(String(s))).join('\n');
    let exp = c.expected;
    if (Array.isArray(exp)) exp = exp.map(e => typeof e==='string'?e:JSON.stringify(e)).join('\n');
    exp = escapeHtml(String(exp || ''));
    const pri = String(c.priority||'').toUpperCase();
    return '<tr>' +
      '<td class="tc-id">' + escapeHtml(c.id||'') + '</td>' +
      '<td>' + escapeHtml(c.module||'') + '</td>' +
      '<td class="tc-title">' + escapeHtml(c.title||c.name||'') + '</td>' +
      '<td><span class="tc-pri tc-pri-' + pri + '">' + escapeHtml(pri||'-') + '</span></td>' +
      '<td>' + escapeHtml(c.type||'') + '</td>' +
      '<td class="tc-pre">' + escapeHtml(c.preconditions||'') + '</td>' +
      '<td class="tc-steps">' + steps + '</td>' +
      '<td class="tc-exp">' + exp + '</td>' +
    '</tr>';
  }).join('');
  return '<div class="tc-block">' +
    '<div class="tc-banner">' +
      '<div><div class="tc-count">' + cases.length + '</div><div class="tc-count-label">条测试用例</div></div>' +
      '<div class="tc-mid">本工具产出 <b>纯人工执行</b> 测试用例,步骤是自然语言操作清单。' +
        '点右侧按钮导出标准 Excel(含「执行结果 / 实际结果」空列),可直接发给测试团队执行。</div>' +
      '<button class="tc-excel-btn" data-tc-runid="' + runId + '" data-tc-toolid="' + (toolId||'step2') + '">↓ 下载 Excel 用例表</button>' +
    '</div>' +
    '<div class="tc-table-wrap"><table class="tc-table"><thead><tr>' +
      '<th>用例编号</th><th>模块</th><th>用例标题</th><th>优先级</th><th>类型</th>' +
      '<th>前置条件</th><th>测试步骤</th><th>预期结果</th>' +
    '</tr></thead><tbody>' + rows + '</tbody></table></div>' +
  '</div>';
}

function renderHtmlReport(r, opts){
  opts = opts || {};
  const expandAll = !!opts.expandAll;
  const rep = r.report || {};
  const meta = rep.meta || {};
  const u = r.usage || {};
  let html = '';

  // Hero card
  const elapsed = r.finished_at ? (r.finished_at - r.started_at).toFixed(1) : '—';
  const pc = meta.project_code || r.project_code || '';
  const pn = meta.project_name || r.project_name || '';
  const projectRow = (pc || pn)
    ? `<div class="report-project">
        <span class="lbl">项目编号</span><code>${escapeHtml(pc || '—')}</code>
        <span class="lbl">项目名称</span><span class="val">${escapeHtml(pn || '—')}</span>
      </div>` : '';
  html += `<div class="report-hero">
    <div class="report-icon">${tool.icon}</div>
    <div style="flex:1;min-width:0">
      <h4>${escapeHtml(tool.name)} · 报告</h4>
      <div class="meta">
        ${meta.produced_at_utc ? new Date(meta.produced_at_utc).toLocaleString('zh-CN', {hour12:false}) : ''}
        · 模型 <code>${escapeHtml(meta.model_id || u.model || '?')}</code>
        · run <code>${r.run_id.slice(0, 12)}</code>
      </div>
      ${projectRow}
      <div class="stat-pills">
        <span class="pill">用时 <span class="v">${elapsed}s</span></span>
        <span class="pill">成本 <span class="v">$${u.cost_usd ?? '—'}</span></span>
        <span class="pill">输出 <span class="v">${(u.output_tokens||0).toLocaleString()}</span> tokens</span>
        <span class="pill">缓存 <span class="v">${(u.cache_read_tokens||0).toLocaleString()}</span></span>
      </div>
    </div>
  </div>`;

  // 测试用例工具(step2):只出用例表 + Excel,不要 verdict / 风险 / Bug 那套"报告"。
  // 用例就是用例 —— 报告主体只有用例表,到此为止直接返回。
  if (tool && tool.id === 'step2'){
    const tcBlock = buildTestCaseBlock(rep, r.run_id, tool.id);
    html += tcBlock || '<div class="run-empty" style="padding:48px;text-align:center">本次未生成测试用例</div>';
    return html;
  }

  // === 4 块结构（测试结论 / 风险结论 / 问题描述 / 用例执行情况）===
  // 适用于其余 7 个 Agent 的报告
  const summary = buildExecutiveSummary(rep, tool);
  html += renderExecutiveSummary(summary);

  // Inline page screenshots (step5 / h5_adapt only)
  // 用 data-screenshot-filename 占位,渲染后 inlineScreenshotsInArea() 把 src
  // 替换成 data: URI,报告自包含、不暴露任何本地文件夹路径。
  const shots = (meta.screenshots || []).filter(s => !s.error);
  if (shots.length){
    const groupedByUrl = {};
    shots.forEach(s => {(groupedByUrl[s.url] = groupedByUrl[s.url] || []).push(s);});
    const imgMap = (opts && opts.imgMap) || {};
    let shotsHtml = '<div class="report-screenshots">';
    shotsHtml += '<div class="screenshots-head">页面截图证据 <span class="screenshots-hint">（已嵌入报告，无本地文件依赖）</span></div>';
    Object.entries(groupedByUrl).forEach(([url, arr]) => {
      shotsHtml += `<div class="shot-group"><div class="shot-url"><code>${escapeHtml(url)}</code></div>`;
      shotsHtml += '<div class="shot-grid">';
      arr.forEach(s => {
        const annotated = s.annotated_filename;
        const fnPrimary = annotated || s.filename;
        // 已 inline 直接用;否则放占位 src,后续 inliner 异步替换为 data: URI
        const initialSrc = imgMap[fnPrimary] || `/api/screenshots/${encodeURIComponent(fnPrimary)}`;
        const issueBadge = s.issue_count
          ? `<span class="issue-badge">${s.issue_count} 个问题</span>` : '';
        shotsHtml += `<div class="shot-cell" title="${escapeHtml(s.viewport)} · ${s.width}×${s.height}${annotated?' · 已标注':''}">
          <img src="${initialSrc}" data-screenshot-filename="${escapeHtml(fnPrimary)}" alt="${escapeHtml(s.viewport)}" loading="lazy">
          <div class="shot-cap">${escapeHtml(s.viewport)} · ${s.width}×${s.height}${issueBadge}</div>
        </div>`;
      });
      shotsHtml += '</div></div>';
    });
    shotsHtml += '</div>';
    html += shotsHtml;
  }

  // Substep cards — labeled by Chinese name + sequence number, NOT internal step id
  // 当顶层契约字段已含 issues/cases 时,substep 数据已被合并入卡片,默认折叠
  // 避免内容重复展示导致报告"很乱"。点击标题可手动展开看原始数据。
  const hasContractData = (Array.isArray(rep.issues) && rep.issues.length) ||
                          (Array.isArray(rep.cases) && rep.cases.length);
  const subs = rep.substeps || {};
  let firstWithContent = null;
  for (const sid of tool.prompts){
    if (subs[sid] && Object.keys(subs[sid]).length){ firstWithContent = sid; break; }
  }
  // 折叠区标题
  html += `<div class="substep-section-head">
    <span class="substep-section-title">各子步骤原始输出</span>
    <span class="substep-section-hint">${hasContractData ? '已聚合到上方 5 段;点击展开看原始数据' : '点击展开'}</span>
  </div>`;
  let idx = 0;
  for (const sid of tool.prompts){
    idx++;
    const data = subs[sid];
    const stats = data ? quickStats(data) : null;
    // 有契约字段时一律默认折叠;否则保留旧逻辑(首个有内容的展开)
    const open = expandAll || (!hasContractData && sid === firstWithContent);
    const title = (tool._substepNames && tool._substepNames[sid]) || extractTitle(data) || `子分析 ${idx}`;
    if (data == null){
      html += `<div class="report-sub">
        <div class="report-sub-head">
          <span class="report-sub-twirl">▶</span>
          <span class="report-sub-num">${idx}</span>
          <span class="report-sub-name" style="color:var(--fg-3)">${escapeHtml(title)} · 已跳过</span>
        </div>
      </div>`;
      continue;
    }
    html += `<div class="report-sub${open?' open':''}">
      <div class="report-sub-head">
        <span class="report-sub-twirl">▶</span>
        <span class="report-sub-num">${idx}</span>
        <span class="report-sub-name">${escapeHtml(title)}</span>
        <span class="report-sub-stats">${stats || ''}</span>
      </div>
      <div class="report-sub-body">
        ${renderSmart(data)}
      </div>
    </div>`;
  }
  return html;
}

// Pull a friendly title from common substep schemas
function extractTitle(data){
  if (!data || typeof data !== 'object') return '';
  // Look for common labels
  const keys = ['name','title'];
  for (const k of keys){
    if (typeof data[k] === 'string') return data[k];
  }
  return '';
}

// Quick stats chip on the substep header
function quickStats(data){
  if (!data || typeof data !== 'object') return '';
  const chips = [];
  // Common high-signal keys
  if (Array.isArray(data.cases)) chips.push(`<span class="chip">${data.cases.length} 用例</span>`);
  if (Array.isArray(data.issues)) chips.push(`<span class="chip">${data.issues.length} 问题</span>`);
  if (Array.isArray(data.scenarios)) chips.push(`<span class="chip">${data.scenarios.length} 场景</span>`);
  if (Array.isArray(data.endpoints)) chips.push(`<span class="chip">${data.endpoints.length} 接口</span>`);
  if (Array.isArray(data.pages)) chips.push(`<span class="chip">${data.pages.length} 页面</span>`);
  if (Array.isArray(data.matrix)) chips.push(`<span class="chip">${data.matrix.length} 矩阵项</span>`);
  if (Array.isArray(data.fix_list)) chips.push(`<span class="chip">${data.fix_list.length} 待修</span>`);
  if (data.confidence && typeof data.confidence.score === 'number'){
    chips.push(`<span class="chip">conf ${(data.confidence.score*100).toFixed(0)}%</span>`);
  }
  return chips.join('');
}

// === Smart auto-renderer for arbitrary JSON ===

function renderSmart(data, depth){
  depth = depth || 0;
  if (data === null || data === undefined) return '<span class="empty-array">—</span>';
  if (typeof data === 'string'){
    if (!data) return '<span class="empty-array">""</span>';
    return escapeHtml(data);
  }
  if (typeof data === 'number' || typeof data === 'boolean'){
    return `<span style="color:var(--ac)">${escapeHtml(String(data))}</span>`;
  }
  if (Array.isArray(data)){
    if (!data.length) return '<span class="empty-array">[]</span>';
    // Array of objects with shared keys → table
    const allObjects = data.every(x => x && typeof x === 'object' && !Array.isArray(x));
    if (allObjects && data.length >= 2){
      return renderObjectArrayAsTable(data);
    }
    if (allObjects){
      // Single object — just unwrap
      return renderSmart(data[0], depth+1);
    }
    // Array of primitives
    return '<ul class="report-list">' +
      data.slice(0, 50).map(x => `<li>${renderSmart(x, depth+1)}</li>`).join('') +
      (data.length > 50 ? `<li class="empty-array">…剩余 ${data.length - 50} 条已折叠</li>` : '') +
      '</ul>';
  }
  if (typeof data === 'object'){
    return renderObjectAsKv(data, depth);
  }
  return escapeHtml(String(data));
}

function renderObjectAsKv(obj, depth){
  const entries = Object.entries(obj);
  if (!entries.length) return '<span class="empty-array">{}</span>';
  return '<dl class="report-kv">' + entries.map(([k, v]) => {
    return `<dt>${escapeHtml(k)}</dt><dd>${renderFieldValue(k, v, depth)}</dd>`;
  }).join('') + '</dl>';
}

function renderFieldValue(key, v, depth){
  // Special schemas
  if ((key === 'severity' || key.endsWith('_severity')) && typeof v === 'string'){
    return `<span class="sev sev-${escapeHtml(v.toLowerCase())}">${escapeHtml(v)}</span>`;
  }
  if (key === 'confidence' && v && typeof v === 'object' && typeof v.score === 'number'){
    const pct = (v.score * 100).toFixed(0);
    return `<span class="confbar"><span class="track"><span class="fill" style="width:${pct}%"></span></span><span class="pct">${pct}%</span></span>${
      v.rationale ? ` <span style="color:var(--fg-3)">— ${escapeHtml(String(v.rationale).slice(0,140))}</span>` : ''
    }`;
  }
  if (key === 'gate_decision' && v && typeof v === 'object'){
    const cls = gateClass(v.action);
    const rs = (v.reasons || []).map(x => `<div>· ${escapeHtml(x)}</div>`).join('');
    return `<div class="gate-banner ${cls}" style="margin:0">
      <span class="badge">${escapeHtml(String(v.action).toLowerCase())}</span>
      <div class="reasons">${rs}</div>
    </div>`;
  }
  if (Array.isArray(v) && v.length > 5){
    // Big array → wrap in details
    return `<details class="report-detail" ${depth === 0 ? 'open' : ''}>
      <summary>${v.length} 项 · 点击展开</summary>
      <div>${renderSmart(v, depth+1)}</div>
    </details>`;
  }
  if (typeof v === 'object' && v !== null && !Array.isArray(v) && Object.keys(v).length > 6){
    return `<details class="report-detail">
      <summary>对象（${Object.keys(v).length} 字段）· 点击展开</summary>
      <div>${renderSmart(v, depth+1)}</div>
    </details>`;
  }
  return renderSmart(v, depth+1);
}

function renderObjectArrayAsTable(arr){
  // Collect all keys, prioritize common ones
  const keySet = new Set();
  arr.forEach(o => Object.keys(o).forEach(k => keySet.add(k)));
  const priority = ['id','title','name','severity','status','category','kind','endpoint','page','area','module','expected','actual','fix','impact','effort_hours','severity_if_fails'];
  const keys = [...keySet].sort((a,b)=>{
    const ai = priority.indexOf(a), bi = priority.indexOf(b);
    if (ai >= 0 && bi >= 0) return ai - bi;
    if (ai >= 0) return -1;
    if (bi >= 0) return 1;
    return 0;
  }).slice(0, 6);  // cap to 6 columns

  const head = `<thead><tr>${keys.map(k=>`<th>${escapeHtml(k)}</th>`).join('')}</tr></thead>`;
  const body = arr.slice(0, 30).map(o => {
    const cells = keys.map(k => `<td>${renderTableCell(k, o[k])}</td>`).join('');
    return `<tr>${cells}</tr>`;
  }).join('');
  const foot = arr.length > 30
    ? `<tfoot><tr><td colspan="${keys.length}">… 共 ${arr.length} 条，已显示前 30 条；查看全部请切到 JSON tab</td></tr></tfoot>`
    : '';
  return `<table class="report-table">${head}<tbody>${body}</tbody>${foot}</table>`;
}

function renderTableCell(key, v){
  if (v === null || v === undefined) return '<span class="empty-array">—</span>';
  if ((key === 'severity' || key.endsWith('_severity')) && typeof v === 'string'){
    return `<span class="sev sev-${escapeHtml(v.toLowerCase())}">${escapeHtml(v)}</span>`;
  }
  if (typeof v === 'string'){
    const s = v.length > 100 ? v.slice(0, 97) + '…' : v;
    return escapeHtml(s);
  }
  if (typeof v === 'number' || typeof v === 'boolean'){
    return `<span style="color:var(--ac)">${escapeHtml(String(v))}</span>`;
  }
  if (Array.isArray(v)){
    if (!v.length) return '<span class="empty-array">[]</span>';
    if (v.length <= 3 && v.every(x => typeof x === 'string' || typeof x === 'number')){
      return v.map(x => escapeHtml(String(x))).join(', ');
    }
    return `<details class="report-detail"><summary>${v.length} 项</summary><div>${renderSmart(v)}</div></details>`;
  }
  if (typeof v === 'object'){
    return `<details class="report-detail"><summary>{${Object.keys(v).length} 字段}</summary><div>${renderSmart(v)}</div></details>`;
  }
  return escapeHtml(String(v));
}

function escapeHtml(s){ return String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }

function fmtLogTs(ts, baseTs){
  const dt = ts - baseTs;
  if (dt < 0) return '+0s';
  if (dt < 60) return '+' + dt.toFixed(1) + 's';
  return '+' + Math.floor(dt/60) + 'm' + Math.round(dt%60) + 's';
}
function fmtLogMsg(entry){
  const skip = new Set(['ts','event']);
  const parts = [];
  for (const [k, v] of Object.entries(entry)){
    if (skip.has(k) || v == null) continue;
    let vs = String(v);
    if (vs.length > 60) vs = vs.slice(0, 57) + '…';
    const cls = (k === 'cost_usd' || k === 'output_tokens' || k === 'model') ? 'v ac' : 'v';
    parts.push(`<span class="k">${escapeHtml(k)}</span>=<span class="${cls}">${escapeHtml(vs)}</span>`);
  }
  return parts.join(' · ');
}
function renderLogs(r){
  const logs = r.logs || [];
  if (!logs.length) return '';
  const base = r.started_at || logs[0].ts;
  const recent = logs.slice(-120);  // streaming events spike count, allow more
  const lines = recent.map(e => {
    const evClass = e.event.replace(/\./g, '-');
    const isStream = e.event === 'llm.thinking' || e.event === 'llm.text';
    const isStreamFinal = e.event === 'llm.thinking.final' || e.event === 'llm.text.final';
    let msgHtml;
    if (isStream && e.tail){
      // Render streaming with prominent tail snippet on its own line
      const head = `<span class="k">sub</span>=<span class="v">${escapeHtml(e.sub_id||'?')}</span> · <span class="k">chars</span>=<span class="v ac">${(e.chars||0).toLocaleString()}</span>`;
      const tail = `<span class="tail">${escapeHtml(e.tail)}</span>`;
      msgHtml = head + tail;
    } else {
      msgHtml = fmtLogMsg(e);
    }
    const cls = isStream ? `log-line stream ${e.event === 'llm.text' ? 'text' : 'thinking'}` : 'log-line';
    return `<div class="${cls}">
      <span class="ts">${fmtLogTs(e.ts, base)}</span>
      <span class="ev ${evClass}">${escapeHtml(e.event)}</span>
      <span class="msg">${msgHtml}</span>
    </div>`;
  }).join('');
  const liveBadge = (r.status === 'running')
    ? '<span class="live"><span class="dot"></span>实时</span>'
    : '<span style="color:var(--fg-3)">已停止</span>';
  // Show streaming progress count too (helpful for long thinking sequences)
  const streamingCount = logs.filter(x => x.event === 'llm.thinking' || x.event === 'llm.text').length;
  return `<div class="log-panel-head">
    ${liveBadge}
    <span style="color:var(--fg-3)">日志</span>
    <span class="count">${logs.length} 条${streamingCount ? ` · ${streamingCount} 流式` : ''}</span>
  </div>
  <div class="log-panel" id="log-panel">${lines}</div>`;
}
let _lastLogCount = 0;
function autoScrollLogs(r){
  const lp = document.getElementById('log-panel');
  if (!lp) return;
  // Only auto-scroll if there are new entries since last render and user hasn't scrolled up
  const total = (r.logs || []).length;
  const atBottom = (lp.scrollHeight - lp.scrollTop - lp.clientHeight) < 40;
  if (total !== _lastLogCount && atBottom){
    lp.scrollTop = lp.scrollHeight;
  }
  _lastLogCount = total;
}

function toast(msg){
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(window._tt);
  window._tt = setTimeout(() => t.classList.remove('show'), 2400);
}

// === Anchor nav (right rail) ===
(function(){
  const nav = document.getElementById('anchor-nav');
  if (!nav) return;
  const links = nav.querySelectorAll('a');
  links.forEach(a => {
    a.addEventListener('click', e => {
      e.preventDefault();
      const target = document.getElementById(a.dataset.target);
      if (target) target.scrollIntoView({behavior:'smooth', block:'start'});
    });
  });
  function update(){
    const heroBottom = document.querySelector('.hero')?.getBoundingClientRect().bottom ?? 200;
    nav.classList.toggle('show', heroBottom < 80);
    let activeIdx = 0;
    ['sec-1','sec-2','sec-3'].forEach((id, i) => {
      const el = document.getElementById(id);
      if (!el) return;
      const rect = el.getBoundingClientRect();
      if (rect.top < window.innerHeight * 0.45) activeIdx = i;
    });
    links.forEach((a, i) => a.classList.toggle('current', i === activeIdx));
  }
  window.addEventListener('scroll', update, {passive:true});
  window.addEventListener('resize', update);
  update();
})();

load();
</script>
</body></html>
"""


SETTINGS_HTML = r"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>设置 — 天枢·裁决</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@300;400;500;600;700&family=Noto+Sans+SC:wght@300;400;500;600&family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
  :root{--bg:#ffffff;--surface:#f0f0f0;--surface-2:#ebebeb;--surface-3:#dcdcdc;
    --line:#c4c4c4;--line-2:#9e9e9e;
    --fg:#0a0a0a;--fg-2:#262626;--fg-3:#4a4a4a;--fg-4:#6e6e6e;
    --ac:#a8401f;--ac-2:#c45a3a;--ac-bg:rgba(168,64,31,.14);--ac-line:rgba(168,64,31,.58);
    --warn:#8a5300;--ok:#4f6b35;--bad:#8a2d12;--info:#3f5560;
    --running:#7a4f00;
    --mono:'Inter','SF Mono',ui-monospace,Menlo,monospace;
    --sans:'Noto Sans SC','PingFang SC',-apple-system,'Microsoft YaHei',sans-serif;
    --serif:'Noto Serif SC','Songti SC','STSong',Georgia,serif;}
  @media (prefers-reduced-motion: reduce){*,*::before,*::after{animation-duration:.01ms!important;transition-duration:.01ms!important}}
  *{box-sizing:border-box}
  html,body{margin:0;background:var(--bg);color:var(--fg);font-family:var(--sans);min-height:100%;
    -webkit-font-smoothing:antialiased}
  body{background:
    radial-gradient(ellipse 90% 50% at 50% -10%, rgba(196,90,58,.07), transparent 65%) fixed,
    radial-gradient(ellipse 80% 40% at 50% 110%, rgba(255,255,255,.02), transparent 60%) fixed,
    var(--bg);}
  ::selection{background:var(--ac-bg);color:var(--ac-2)}
  ::-webkit-scrollbar{width:10px;height:10px}
  ::-webkit-scrollbar-track{background:transparent}
  ::-webkit-scrollbar-thumb{background:var(--surface-3);border-radius:5px;border:2px solid var(--bg)}
  ::-webkit-scrollbar-thumb:hover{background:var(--line-2)}
  .topbar{display:flex;align-items:center;gap:12px;height:56px;padding:0 24px;
    border-bottom:1px solid var(--line);background:rgba(255,255,255,.94);
    position:sticky;top:0;z-index:100;backdrop-filter:saturate(180%) blur(20px);
    -webkit-backdrop-filter:saturate(180%) blur(20px)}
  .topbar .logo{width:28px;height:28px;flex-shrink:0;
    background:linear-gradient(135deg,#262626,#1a1a1a);border-radius:6px;
    display:grid;place-items:center;color:#001f1a;font-weight:700;font-size:11px;
    letter-spacing:-.02em;
    box-shadow:0 1px 2px rgba(0,0,0,.4),inset 0 1px 0 rgba(255,255,255,.18)}
  .topbar .logo .logo-mark{width:18px;height:18px;display:block;
    filter:drop-shadow(0 .5px 0 rgba(255,255,255,.18))}
  .topbar h1{margin:0;font-size:15px;font-weight:600;letter-spacing:-.015em}
  .topbar h1.brand{display:inline-flex;align-items:baseline;gap:0}
  .topbar h1.brand strong{font-weight:600;color:var(--fg);letter-spacing:-.02em}
  .topbar h1.brand .brand-sub{font-weight:400;color:var(--fg-3);margin-left:8px;font-size:13px;letter-spacing:.005em}
  .topbar nav{display:flex;gap:2px;margin-left:24px}
  .topbar nav a{padding:6px 12px;border-radius:6px;color:var(--fg-2);text-decoration:none;
    font-size:13px;transition:background .12s,color .12s}
  .topbar nav a:hover{background:var(--surface-2);color:var(--fg)}
  .topbar nav a.active{background:var(--ac-bg);color:var(--ac-2)}
  main{max-width:1080px;margin:0 auto;padding:40px 24px 80px}
  h2{margin:0 0 8px;font-size:24px;letter-spacing:-.025em;font-weight:600}
  .sub{color:var(--fg-2);font-size:13px;margin-bottom:32px;line-height:1.55}
  .sec{background:var(--surface);border:1px solid var(--line);border-radius:14px;
    margin-top:14px;overflow:hidden}
  .sec-head{padding:16px 22px;border-bottom:1px solid var(--line);display:flex;
    align-items:center;gap:12px;background:var(--surface-2)}
  .sec-head h3{margin:0;font-size:15px;font-weight:600;letter-spacing:-.005em}
  .sec-head .dot{width:8px;height:8px;border-radius:50%;background:var(--fg-4)}
  .sec-head .dot.ok{background:var(--ok)}
  .sec-head .dot.bad{background:var(--bad)}
  .sec-head .dot.warn{background:var(--warn)}
  .sec-body{padding:14px 18px}
  dl.kv{display:grid;grid-template-columns:160px 1fr;gap:8px 16px;margin:0;font-size:13px}
  dl.kv dt{color:var(--fg-3)}
  dl.kv dd{margin:0;color:var(--fg);font-family:var(--mono);font-size:12.5px}
  dl.kv dd code{background:var(--surface-2);padding:2px 8px;border-radius:4px;border:1px solid var(--line)}
  dl.kv dd .ok{color:var(--ok)}
  dl.kv dd .bad{color:var(--bad)}
  dl.kv dd .warn{color:var(--warn)}
  table{width:100%;border-collapse:collapse;font-size:12.5px}
  table th{font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:var(--fg-3);
    text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);font-weight:600}
  table td{padding:12px;border-bottom:1px solid var(--line);font-family:var(--mono);font-size:12px;
    color:var(--fg-2)}
  table tr:last-child td{border-bottom:none}
  table .name{color:var(--fg)}
  table .pkg{color:var(--ac-2)}
  table .ok{color:var(--ok)}
  table .bad{color:var(--bad)}
  table .req{font-size:10px;padding:1px 7px;border-radius:3px;
    background:rgba(248,113,113,.08);color:var(--bad);border:1px solid rgba(248,113,113,.3)}
  table .opt{font-size:10px;padding:1px 7px;border-radius:3px;
    background:var(--surface-2);color:var(--fg-3);border:1px solid var(--line-2)}
  table .install-btn{background:rgba(16,185,129,.10);border:1px solid var(--ac);color:var(--ac);
    padding:3px 11px;border-radius:5px;font-family:var(--mono);font-size:11px;cursor:pointer;
    transition:all .15s}
  table .install-btn:hover{background:var(--ac);color:#001a14}
  table .install-btn:disabled{background:transparent;border-color:var(--line-2);color:var(--fg-3);
    cursor:wait}
  /* === Hero & KPI tiles for Reports === */
  .reports-hero-wrap{margin-bottom:32px}
  .hero-eyebrow{display:inline-flex;align-items:center;gap:6px;
    font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;
    color:var(--ac-2);margin-bottom:12px;padding:5px 12px;border-radius:999px;
    background:var(--ac-bg);border:1px solid var(--ac-line)}
  .hero-eyebrow::before{content:"";width:6px;height:6px;border-radius:50%;
    background:var(--ac-2);box-shadow:0 0 0 2px var(--ac-bg)}
  .reports-hero-wrap h2{font-size:32px;letter-spacing:-.03em;margin:0 0 8px;line-height:1.1;
    background:linear-gradient(135deg,var(--fg) 0%,var(--fg-2) 75%);
    -webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
  .reports-hero-wrap .sub{color:var(--fg-2);font-size:14.5px;line-height:1.55;margin:0 0 24px;max-width:680px}
  .kpi-strip{display:grid;grid-template-columns:repeat(4,1fr);gap:24px;margin-bottom:14px;
    padding:14px 0;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
  .kpi{background:transparent;border:none;border-radius:0;
    padding:0;position:relative;overflow:visible;
    transition:none}
  .kpi:hover{transform:none;border-color:transparent}
  .kpi::before{display:none}
  .kpi .num{font-size:20px;font-weight:600;letter-spacing:-.02em;color:var(--fg);
    font-variant-numeric:tabular-nums;display:flex;align-items:baseline;gap:6px}
  .kpi .num.ok{color:var(--ok)}
  .kpi .num.bad{color:var(--bad)}
  .kpi .num.warn{color:var(--warn)}
  .kpi .lbl{font-size:11px;color:var(--fg-3);text-transform:uppercase;letter-spacing:.06em;
    margin-top:4px;font-weight:500}
  .kpi .icon-bg{display:none;position:absolute;right:-8px;bottom:-8px;font-size:60px;opacity:.04;line-height:1;
    pointer-events:none}
  /* === Hero & KPI tiles === */
  .settings-hero{margin-bottom:32px}
  .hero-eyebrow{display:inline-flex;align-items:center;gap:6px;
    font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;
    color:var(--ac-2);margin-bottom:12px;padding:5px 12px;border-radius:999px;
    background:var(--ac-bg);border:1px solid var(--ac-line)}
  .hero-eyebrow::before{content:"";width:6px;height:6px;border-radius:50%;
    background:var(--ac-2);box-shadow:0 0 0 2px var(--ac-bg)}
  .settings-hero h2{font-size:32px;letter-spacing:-.03em;margin:0 0 8px;line-height:1.1;
    background:linear-gradient(135deg,var(--fg) 0%,var(--fg-2) 75%);
    -webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
  .settings-hero .sub{color:var(--fg-2);font-size:14.5px;line-height:1.55;margin:0 0 24px;max-width:680px}
  .kpi-strip{display:grid;grid-template-columns:repeat(4,1fr);gap:24px;margin-bottom:14px;
    padding:14px 0;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
  .kpi{background:transparent;border:none;border-radius:0;
    padding:0;position:relative;overflow:visible;
    transition:none}
  .kpi:hover{transform:none;border-color:transparent}
  .kpi::before{display:none}
  .kpi .num{font-size:20px;font-weight:600;letter-spacing:-.02em;color:var(--fg);
    font-variant-numeric:tabular-nums;display:flex;align-items:baseline;gap:6px}
  .kpi .num.ok{color:var(--ok)}
  .kpi .num.bad{color:var(--bad)}
  .kpi .num.warn{color:var(--warn)}
  .kpi .lbl{font-size:11px;color:var(--fg-3);text-transform:uppercase;letter-spacing:.06em;
    margin-top:4px;font-weight:500}
  .kpi .icon-bg{display:none;position:absolute;right:-8px;bottom:-8px;font-size:60px;opacity:.04;line-height:1;
    pointer-events:none}
  .install-toast{position:fixed;bottom:24px;right:24px;width:480px;background:var(--surface-2);
    border:1px solid var(--ac);border-radius:10px;padding:14px 18px;z-index:50;
    box-shadow:0 12px 40px rgba(0,0,0,.4);display:none;font-size:12.5px}
  .install-toast.show{display:block}
  .install-toast h4{margin:0 0 6px;font-size:13px;font-weight:600;display:flex;align-items:center;gap:8px}
  .install-toast .cmd{font-family:var(--mono);color:var(--fg-3);font-size:11px;
    padding:6px 8px;background:var(--bg);border-radius:4px;margin:6px 0}
  .install-toast pre{margin:0;font-family:var(--mono);font-size:11px;line-height:1.5;
    color:var(--fg-2);max-height:240px;overflow-y:auto;white-space:pre-wrap;word-break:break-word;
    background:var(--bg);padding:8px 10px;border-radius:4px;border:1px solid var(--line)}
  .install-toast .row{display:flex;align-items:center;gap:8px;margin-top:8px}
  .install-toast button{background:transparent;border:1px solid var(--line-2);color:var(--fg-2);
    padding:4px 12px;border-radius:5px;font-family:var(--mono);font-size:11.5px;cursor:pointer}
  .install-toast button:hover{border-color:var(--ac);color:var(--ac)}
  .install-toast .status-tag{font-family:var(--mono);font-size:10.5px;padding:2px 8px;
    border-radius:3px;font-weight:600;text-transform:uppercase}
  .install-toast .status-tag.queued{background:rgba(168,174,184,.15);color:var(--fg-2)}
  .install-toast .status-tag.running{background:rgba(251,191,36,.15);color:var(--warn)}
  .install-toast .status-tag.succeeded{background:rgba(74,222,128,.15);color:var(--ok)}
  .install-toast .status-tag.failed{background:rgba(248,113,113,.15);color:var(--bad)}
  .pill{display:inline-block;padding:1px 8px;border-radius:3px;font-family:var(--mono);
    font-size:10.5px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;
    background:rgba(74,222,128,.12);color:var(--ok)}
  .pill.bad{background:rgba(248,113,113,.12);color:var(--bad)}
  .pill.warn{background:rgba(251,191,36,.12);color:var(--warn)}
  .empty{color:var(--fg-3);font-size:12.5px;font-family:var(--mono);padding:14px}
  .ovr-row{display:grid;grid-template-columns:1fr auto;gap:14px;padding:11px 14px;
    border-bottom:1px solid var(--line);align-items:center;font-size:12.5px}
  .ovr-row:last-child{border-bottom:none}
  .ovr-row code{font-family:var(--mono);color:var(--fg-2);font-size:12px}
  .ovr-row .reset{background:transparent;border:1px solid var(--line-2);color:var(--fg-2);
    padding:4px 10px;border-radius:5px;font-family:var(--mono);font-size:11px;cursor:pointer}
  .ovr-row .reset:hover{border-color:var(--bad);color:var(--bad)}
  /* === 连接 Claude 三模式纵向卡 === */
  .modes{display:flex;flex-direction:column;gap:10px}
  .mode-card{position:relative;padding:16px 18px 16px 50px;border-radius:10px;
    border:1px solid var(--line);background:var(--surface-2);cursor:pointer;
    transition:border-color .15s,background .15s,transform .12s}
  .mode-card:hover{border-color:var(--line-2);background:var(--surface-3)}
  .mode-card.active{border-color:var(--ac);background:linear-gradient(135deg,rgba(16,185,129,.08),rgba(16,185,129,.02));
    cursor:default}
  .mode-card.active:hover{background:linear-gradient(135deg,rgba(16,185,129,.10),rgba(16,185,129,.03))}
  .mode-card .radio{position:absolute;top:18px;left:18px;width:18px;height:18px;
    border-radius:50%;border:2px solid var(--line-2);background:var(--bg);transition:all .15s}
  .mode-card.active .radio{border-color:var(--ac);background:var(--ac);
    box-shadow:inset 0 0 0 4px var(--bg)}
  .mode-card .head{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
  .mode-card .name{font-size:14.5px;font-weight:600;color:var(--fg);letter-spacing:-.005em}
  .mode-card .badge{font-size:10px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;
    padding:2px 8px;border-radius:999px;background:var(--surface-3);color:var(--fg-3);
    border:1px solid var(--line-2)}
  .mode-card .badge.recommend{background:linear-gradient(135deg,var(--ac),#0ea671);
    color:#001a14;border-color:transparent}
  .mode-card .badge.simple{background:rgba(96,165,250,.12);color:#7eb6ff;
    border-color:rgba(96,165,250,.3)}
  .mode-card .badge.metered{background:rgba(251,191,36,.12);color:var(--warn);
    border-color:rgba(251,191,36,.3)}
  .mode-card .status{margin-left:auto;font-size:11.5px;font-family:var(--mono);
    padding:3px 10px;border-radius:999px;border:1px solid var(--line-2);
    background:var(--surface);color:var(--fg-3);white-space:nowrap}
  .mode-card .status.ok{color:var(--ok);border-color:rgba(52,211,153,.36);
    background:rgba(52,211,153,.10)}
  .mode-card .status.warn{color:var(--warn);border-color:rgba(251,191,36,.36);
    background:rgba(251,191,36,.10)}
  .mode-card .status.bad{color:var(--bad);border-color:rgba(248,113,113,.36);
    background:rgba(248,113,113,.10)}
  .mode-card .desc{font-size:12.5px;color:var(--fg-2);line-height:1.55;margin-top:6px}
  /* 激活后展开的操作区 */
  .mode-card .action-zone{margin-top:12px;padding:12px 14px;border-radius:8px;
    background:var(--surface);border:1px solid var(--line);
    display:none;flex-direction:column;gap:10px}
  .mode-card.active .action-zone{display:flex}
  .mode-card .info-line{font-size:12px;color:var(--fg-2);line-height:1.55}
  .mode-card .info-line code{background:var(--surface-2);padding:1px 7px;border-radius:4px;
    font-size:11px;color:var(--fg);font-family:var(--mono)}
  .mode-card .btn-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
  .mode-btn{padding:8px 14px;border-radius:7px;font-size:12.5px;font-weight:500;
    cursor:pointer;border:1px solid var(--line-2);background:transparent;
    color:var(--fg-2);transition:all .12s;white-space:nowrap}
  .mode-btn:hover{background:var(--surface-2);color:var(--fg)}
  .mode-btn.primary{background:var(--ac);color:#001a14;border-color:var(--ac);font-weight:600}
  .mode-btn.primary:hover{background:#0ea671;border-color:#0ea671}
  .mode-btn.danger{color:var(--bad);border-color:rgba(248,113,113,.3)}
  .mode-btn.danger:hover{background:rgba(248,113,113,.12);border-color:var(--bad)}
  .mode-btn:disabled{opacity:.55;cursor:wait}
  .mode-card .api-input{flex:1;padding:8px 12px;background:var(--surface-2);
    border:1px solid var(--line-2);border-radius:6px;color:var(--fg);
    font-family:var(--mono);font-size:12.5px;outline:none;transition:border-color .12s;
    min-width:200px}
  .mode-card .api-input:focus{border-color:var(--ac)}
  .mode-card .checkbox-line{display:flex;align-items:center;gap:5px;
    font-size:11.5px;color:var(--fg-3)}
  /* OAuth 已登录后的账号信息块 */
  .mode-card .account-block{background:linear-gradient(135deg,rgba(16,185,129,.08),rgba(16,185,129,.02));
    border:1px solid var(--ac-line);border-radius:8px;padding:12px 14px;
    display:flex;flex-direction:column;gap:6px;margin-bottom:4px}
  .mode-card .account-row{display:grid;grid-template-columns:80px 1fr;gap:10px;
    font-size:12.5px;line-height:1.5}
  .mode-card .account-row .lbl{color:var(--fg-3);font-size:11.5px;font-family:var(--mono)}
  .mode-card .account-row .val{color:var(--fg);word-break:break-word}
  .mode-card .account-row code{font-size:11px;background:var(--surface-3);padding:1px 6px;
    border-radius:4px}

  /* === 模型接入主区（单按钮 + 状态条） === */
  .access-pane{display:flex;flex-direction:column;gap:14px}
  .access-status{padding:16px 18px;border-radius:10px;border:1px solid var(--line);
    background:var(--surface-2);display:flex;align-items:center;gap:14px}
  .access-status.ready{border-color:var(--ac-line);
    background:linear-gradient(135deg,rgba(16,185,129,.08),rgba(16,185,129,.02))}
  .access-status.unset{border-color:rgba(251,191,36,.32);
    background:linear-gradient(135deg,rgba(251,191,36,.06),rgba(251,191,36,.02))}
  .access-status .icon{font-size:24px;line-height:1;flex-shrink:0}
  .access-status .info{flex:1;min-width:0}
  .access-status .title{font-size:14px;font-weight:600;color:var(--fg);margin-bottom:3px}
  .access-status .sub{font-size:12px;color:var(--fg-3);line-height:1.5;word-break:break-word}
  .access-status code{font-family:var(--mono);font-size:11.5px;background:var(--surface-3);
    padding:1px 7px;border-radius:4px;color:var(--fg-2)}
  .access-btn-row{display:flex;gap:10px;align-items:center}
  .access-btn{padding:11px 22px;border-radius:8px;font-size:13.5px;font-weight:600;
    cursor:pointer;border:1px solid var(--ac);background:var(--ac);color:#001a14;
    transition:all .15s;letter-spacing:-.005em}
  .access-btn:hover{background:#0ea671;border-color:#0ea671;transform:translateY(-1px);
    box-shadow:0 4px 14px rgba(16,185,129,.25)}
  .access-btn.danger{background:transparent;color:var(--bad);border-color:rgba(248,113,113,.35)}
  .access-btn.danger:hover{background:rgba(248,113,113,.10);border-color:var(--bad);
    transform:translateY(-1px);box-shadow:0 4px 14px rgba(248,113,113,.18)}
  .access-btn:disabled{opacity:.55;cursor:wait;transform:none;box-shadow:none}

  /* === 模型接入弹窗 === */
  .auth-modal-backdrop{position:fixed;inset:0;background:rgba(0,0,0,.55);
    backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px);
    display:flex;align-items:center;justify-content:center;z-index:9000;
    animation:authModalFade .15s ease-out}
  .auth-modal-backdrop[hidden]{display:none}
  @keyframes authModalFade{from{opacity:0}to{opacity:1}}
  .auth-modal{width:min(540px,92vw);max-height:88vh;background:var(--surface);
    border:1px solid var(--line-2);border-radius:14px;overflow:hidden;
    display:flex;flex-direction:column;
    box-shadow:0 24px 80px rgba(0,0,0,.5);animation:authModalRise .18s ease-out}
  @keyframes authModalRise{from{transform:translateY(12px);opacity:.6}to{transform:none;opacity:1}}
  .auth-modal-head{display:flex;align-items:center;padding:16px 20px;
    border-bottom:1px solid var(--line)}
  .auth-modal-head h4{margin:0;font-size:15px;font-weight:600;color:var(--fg);
    letter-spacing:-.005em}
  .auth-modal-close{margin-left:auto;background:transparent;border:none;color:var(--fg-3);
    font-size:22px;line-height:1;cursor:pointer;padding:2px 8px;border-radius:6px;
    transition:all .12s}
  .auth-modal-close:hover{background:var(--surface-2);color:var(--fg)}
  .auth-modal-body{padding:18px 20px;overflow-y:auto;display:flex;flex-direction:column;gap:12px}

  /* 兼容旧选择器（防 grep 漏） */
  .auth-primary{padding:20px 22px;border-radius:12px;
    background:linear-gradient(135deg,rgba(16,185,129,.08),rgba(16,185,129,.02));
    border:1px solid var(--ac-line);position:relative}
  .auth-primary.ready{border-color:rgba(16,185,129,.45)}
  .auth-primary.unset{background:linear-gradient(135deg,rgba(251,191,36,.06),rgba(251,191,36,.02));
    border-color:rgba(251,191,36,.32)}
  .ap-header{display:flex;align-items:flex-start;gap:14px;margin-bottom:10px}
  .ap-emoji{font-size:26px;line-height:1.1;flex-shrink:0}
  .ap-title-block{flex:1;min-width:0}
  .ap-title{font-size:16px;font-weight:600;color:var(--fg);letter-spacing:-.01em;
    display:flex;align-items:center;gap:10px}
  .ap-badge{font-size:10px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;
    padding:3px 9px;border-radius:999px;
    background:linear-gradient(135deg,var(--ac),#0ea671);color:#001a14;
    box-shadow:0 1px 2px rgba(16,185,129,.4)}
  .ap-subtitle{font-size:12.5px;color:var(--fg-2);line-height:1.55;margin-top:5px}
  .ap-status{flex-shrink:0;font-family:var(--mono);font-size:11.5px;padding:3px 10px;
    border-radius:999px;border:1px solid var(--line-2);background:var(--surface-2);color:var(--fg-3)}
  .ap-status.ok{color:var(--ok);border-color:rgba(52,211,153,.36);background:rgba(52,211,153,.10)}
  .ap-status.warn{color:var(--warn);border-color:rgba(251,191,36,.36);background:rgba(251,191,36,.10)}
  .ap-status.bad{color:var(--bad);border-color:rgba(248,113,113,.36);background:rgba(248,113,113,.10)}
  .ap-body{padding:12px 14px;border-radius:8px;background:var(--surface-2);border:1px solid var(--line);
    font-size:12.5px;color:var(--fg-2);line-height:1.6;margin-bottom:12px}
  .ap-body.empty{display:none}
  .ap-actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  .ap-btn{padding:9px 20px;border-radius:8px;font-size:13px;font-weight:600;
    cursor:pointer;border:none;transition:all .15s;letter-spacing:-.005em}
  .ap-btn.primary{background:var(--ac);color:#001a14;
    box-shadow:0 1px 3px rgba(16,185,129,.3),inset 0 1px 0 rgba(255,255,255,.18)}
  .ap-btn.primary:hover{background:#0ea671;transform:translateY(-1px);
    box-shadow:0 3px 8px rgba(16,185,129,.4),inset 0 1px 0 rgba(255,255,255,.2)}
  .ap-btn.danger{background:transparent;color:var(--bad);
    border:1px solid rgba(248,113,113,.3)}
  .ap-btn.danger:hover{background:rgba(248,113,113,.12);border-color:var(--bad)}
  .ap-btn.ghost{background:transparent;color:var(--fg-2);border:1px solid var(--line-2)}
  .ap-btn.ghost:hover{background:var(--surface-2);color:var(--fg);border-color:var(--line-2)}
  .ap-btn:disabled{opacity:.55;cursor:wait;transform:none!important}

  /* === 高级选项（折叠） === */
  .auth-advanced{margin-top:14px;border:1px solid var(--line);border-radius:10px;
    background:var(--surface);overflow:hidden}
  .auth-advanced summary{padding:12px 16px;cursor:pointer;display:flex;align-items:center;gap:10px;
    font-size:12.5px;color:var(--fg-2);user-select:none;list-style:none;outline:none;
    transition:background .12s}
  .auth-advanced summary::-webkit-details-marker{display:none}
  .auth-advanced summary:hover{background:var(--surface-2)}
  .auth-advanced[open] summary{border-bottom:1px solid var(--line);background:var(--surface-2)}
  .auth-advanced[open] .adv-icon{transform:rotate(90deg)}
  .adv-icon{display:inline-block;color:var(--fg-3);transition:transform .15s}
  .adv-text{font-weight:500;color:var(--fg)}
  .adv-hint{margin-left:auto;font-size:11.5px;color:var(--fg-3);font-family:var(--mono)}
  .adv-body{padding:14px 16px;display:flex;flex-direction:column;gap:12px}
  .adv-card{padding:14px 16px;border-radius:8px;background:var(--surface-2);border:1px solid var(--line)}
  .adv-card-head{display:flex;align-items:center;gap:10px;margin-bottom:8px;flex-wrap:wrap}
  .adv-card-head strong{font-size:13.5px;color:var(--fg)}
  .adv-tag{font-size:10.5px;padding:2px 8px;border-radius:999px;background:var(--surface-3);
    color:var(--fg-3);border:1px solid var(--line-2)}
  .adv-card-state{font-size:11.5px;font-family:var(--mono);color:var(--fg-3)}
  .adv-card-state.ok{color:var(--ok)}
  .adv-desc{margin:0 0 10px;font-size:12px;color:var(--fg-2);line-height:1.55}
  .adv-input-row{display:flex;gap:8px;align-items:center}
  .adv-input-row input[type=password],.adv-input-row input[type=text]{flex:1;padding:8px 12px;
    background:var(--surface-3);border:1px solid var(--line-2);border-radius:6px;
    color:var(--fg);font-family:var(--mono);font-size:12.5px;outline:none;transition:border-color .12s}
  .adv-input-row input:focus{border-color:var(--ac)}
  .adv-btn{padding:8px 14px;border-radius:6px;font-size:12px;font-weight:500;cursor:pointer;
    border:1px solid var(--line-2);background:transparent;color:var(--fg-2);transition:all .12s;white-space:nowrap}
  .adv-btn:hover{background:var(--surface-3);color:var(--fg)}
  .adv-btn.primary{background:var(--ac);color:#001a14;border-color:var(--ac)}
  .adv-btn.primary:hover{background:#0ea671}
  .adv-btn.danger{color:var(--bad);border-color:rgba(248,113,113,.3)}
  .adv-btn.danger:hover{background:rgba(248,113,113,.12)}
  .adv-btn:disabled{opacity:.55;cursor:wait}
  .adv-checkbox{display:flex;align-items:center;gap:5px;font-size:11.5px;color:var(--fg-3)}
  .adv-checkbox input{margin:0}
  .adv-info{padding:11px 14px;border-radius:7px;background:var(--surface-2);
    border:1px solid var(--line);display:flex;gap:10px;align-items:flex-start}
  .adv-info-icon{font-size:16px;line-height:1.2;flex-shrink:0;opacity:.65}
  .adv-info-text{font-size:12px;color:var(--fg-2);line-height:1.6}
  .adv-info-text strong{color:var(--fg)}
  .adv-info-text u{text-decoration:underline;text-decoration-color:rgba(248,113,113,.5);
    text-underline-offset:2px}
</style></head>
<body>
<div class="topbar">
  <a class="brand-link" href="/tools" title="天枢 · 裁决 · 返回主页" style="text-decoration:none;display:inline-flex;align-items:center;gap:10px;font-family:'Noto Serif SC',Georgia,serif;font-size:19px;font-weight:600;letter-spacing:.18em;color:var(--fg);margin-right:24px;padding:4px 0"><svg viewBox="0 0 24 24" fill="currentColor" width="24" height="24" aria-hidden="true" style="color:var(--ac);opacity:1;flex-shrink:0;filter:drop-shadow(0 1px 2px rgba(196,90,58,.28))"><path d="M12 1.5L13.4 10.6L22.5 12L13.4 13.4L12 22.5L10.6 13.4L1.5 12L10.6 10.6Z"/></svg><span>天枢</span><span style="color:var(--ac);margin:0 6px;font-weight:400">·</span><span>裁决</span></a>
  
  <nav>
    <a href="/tools">工具</a>
    <a href="/reports">报告</a>
    <a href="/settings" class="active">设置</a>
    <a href="/admin/users" class="admin-only" style="display:none">用户管理</a>
  </nav>
  <div class="right" style="margin-left:auto">
    <span class="kbd-hint" id="cmd-trigger" title="打开命令面板"
      style="display:inline-flex;align-items:center;gap:4px;font-size:11px;color:var(--fg-3);
        padding:4px 8px;border-radius:6px;cursor:pointer;transition:all .12s">
      <kbd style="background:var(--surface-3);border:1px solid var(--line-1);border-bottom-width:2px;
        padding:1px 6px;border-radius:4px;font-size:10.5px;color:var(--fg-2);
        font-family:'SF Mono',ui-monospace,monospace;min-width:18px;text-align:center">⌘</kbd>
      <kbd style="background:var(--surface-3);border:1px solid var(--line-1);border-bottom-width:2px;
        padding:1px 6px;border-radius:4px;font-size:10.5px;color:var(--fg-2);
        font-family:'SF Mono',ui-monospace,monospace;min-width:18px;text-align:center">K</kbd>
      跳转
    </span>
  </div>
</div>
<main>
  <section class="settings-hero">
    <span class="hero-eyebrow">偏好与环境</span>
    <h2>设置</h2>
    <p class="sub">本地 Claude 接入、默认模型、依赖健康度、提示词覆盖 — 一处管理。</p>
    <div class="kpi-strip">
      <div class="kpi"><div class="num ok" id="kpi-claude">—</div><div class="lbl" id="kpi-claude-lbl">Claude</div><div class="icon-bg">🤖</div></div>
      <div class="kpi"><div class="num" id="kpi-models">—</div><div class="lbl">可用模型</div><div class="icon-bg">⚡</div></div>
      <div class="kpi"><div class="num" id="kpi-env">—</div><div class="lbl">工具就绪</div><div class="icon-bg">🛠</div></div>
      <div class="kpi"><div class="num" id="kpi-overrides">—</div><div class="lbl">提示词覆盖</div><div class="icon-bg">📝</div></div>
    </div>
  </section>

  <div class="sec" id="auth">
    <div class="sec-head">
      <span class="dot" id="auth-dot"></span>
      <h3>模型接入</h3>
      <span style="margin-left:auto;font-size:11.5px;color:var(--fg-3);font-family:var(--mono)" id="auth-mode-current">—</span>
    </div>
    <div class="sec-body" id="auth-body">
      <div style="color:var(--fg-3);font-size:12.5px;padding:6px 0">加载中…</div>
    </div>
  </div>

  <div class="sec" id="sec-defaults" hidden>
    <div class="sec-head">
      <span class="dot ok"></span>
      <h3>默认模型与精度（Claude Code 一致）</h3>
    </div>
    <div class="sec-body" id="defaults">加载中…</div>
  </div>

  <div class="sec">
    <div class="sec-head">
      <span class="dot ok"></span>
      <h3>每个工具的环境需求</h3>
      <button id="env-refresh" style="margin-left:auto;background:transparent;border:1px solid var(--line-2);color:var(--fg-2);padding:4px 12px;border-radius:5px;font-family:var(--mono);font-size:11px;cursor:pointer">↻ 重新检测</button>
    </div>
    <div id="system-info" style="padding:10px 18px;border-bottom:1px solid var(--line);
      font-family:var(--mono);font-size:11.5px;color:var(--fg-3);background:var(--surface-2)"></div>
    <table id="tool-env-table"><thead>
      <tr><th>工具</th><th>包</th><th>必需</th><th>已安装</th><th>版本</th><th>用途</th><th></th></tr>
    </thead><tbody id="tool-env-tbody"></tbody></table>
  </div>

  <div class="sec">
    <div class="sec-head">
      <span class="dot warn"></span>
      <h3>提示词覆盖（已激活）</h3>
    </div>
    <div id="overrides"></div>
  </div>
</main>

<div class="install-toast" id="install-toast">
  <h4 id="it-title">安装中…</h4>
  <div class="cmd" id="it-cmd">·</div>
  <div class="row">
    <span class="status-tag" id="it-status">queued</span>
    <span style="font-family:var(--mono);font-size:11px;color:var(--fg-3)" id="it-elapsed"></span>
    <button style="margin-left:auto" onclick="document.getElementById('install-toast').classList.remove('show')">×</button>
  </div>
  <pre id="it-log" style="margin-top:8px"></pre>
</div>

<!-- 模型接入选择弹窗 — 一次渲染，按需 show/hide -->
<div class="auth-modal-backdrop" id="auth-modal" hidden>
  <div class="auth-modal" role="dialog" aria-modal="true">
    <div class="auth-modal-head">
      <h4>选择模型接入方式</h4>
      <button class="auth-modal-close" data-act="close" aria-label="关闭">×</button>
    </div>
    <div class="auth-modal-body" id="auth-modal-body">
      <!-- renderAuthModal() 填充 -->
    </div>
  </div>
</div>

<script>
// 局部 escapeHtml — SETTINGS 页内的 renderAuthSection 等多处依赖。
// 不能依赖 _inject_shared_overlays 注入的版本，因为那个 script 块在本块之后执行，
// 而本块末尾的 load() 调用在它之前；hoisting 跨 script 不共享。
function escapeHtml(s){ return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

function fmtDate(s){
  if (!s) return '—';
  const d = new Date(s);
  return d.toLocaleString('zh-CN', {year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
}

async function load(){
  const ci = await fetch('/api/claude/info').then(r=>r.json());
  // 顶部 KPI：Claude 状态 — 状态语义已经迁移到「连接 Claude」section；这里仅作信息汇总
  const k1 = document.getElementById('kpi-claude');
  const k1l = document.getElementById('kpi-claude-lbl');
  if (ci.bin_found && ci.account){
    k1.textContent = '✓'; k1.className = 'num ok'; k1l.textContent = 'CLI 就绪';
  } else if (ci.bin_found){
    k1.textContent = '◔'; k1.className = 'num warn'; k1l.textContent = 'CLI 已找到 · 未登录';
  } else {
    k1.textContent = '✗'; k1.className = 'num bad'; k1l.textContent = '未找到 CLI';
  }
  document.getElementById('kpi-models').textContent = (ci.available_models || []).length;

  document.getElementById('defaults').innerHTML = `
    <dl class="kv">
      <dt>默认精度</dt><dd><code>${ci.settings_effort_level}</code> <span style="color:var(--fg-3)">每次跑工具可单独调整</span></dd>
      <dt>可用模型</dt><dd>${ci.available_models.map(m=>{
        const isDefault = m.default ? ' <span style="color:var(--ac-2);font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;margin-left:6px">默认</span>' : '';
        const legacy = m.legacy ? ' <span style="color:var(--fg-3);font-size:10px;text-transform:uppercase;margin-left:6px">旧</span>' : '';
        const key = m.key ? ` <span style="color:var(--fg-4);font-family:'SF Mono',ui-monospace,monospace;font-size:10px;margin-left:6px">${m.key}</span>` : '';
        return `<div style="margin:5px 0;display:flex;align-items:center;gap:4px;flex-wrap:wrap"><code style="font-weight:600">${m.label || m.key}</code>${key}<span style="color:var(--fg-3);font-size:11.5px;margin-left:8px">${m.tag || ''}</span>${isDefault}${legacy}</div>`;
      }).join('')}</dd>
      <dt>可选精度</dt><dd>${ci.available_efforts.map(e=>`<code style="margin-right:6px">${e.key}</code>`).join('')}</dd>
      <dt>扩展思考</dt><dd>${ci.available_thinking.map(t=>`<code style="margin-right:6px">${t.key}</code>`).join('')}</dd>
    </dl>`;

  await renderToolEnv();
  await renderAuthSection();

  const sys = await fetch('/api/settings/system').then(r=>r.json());
  document.getElementById('system-info').innerHTML =
    `OS <code>${sys.os} ${sys.os_release}</code> · arch <code>${sys.machine}</code> · python <code>${sys.python}</code> · venv <code>${sys.venv || '系统'}</code>`;

  // KPI: tools env health (pass count)
  try {
    const tEnv = await fetch('/api/settings/tools').then(r => r.json());
    const total = tEnv.tools.length;
    const okCount = tEnv.tools.filter(t => t.ready).length;
    const kEnv = document.getElementById('kpi-env');
    kEnv.innerHTML = `${okCount}<span class="unit" style="font-size:14px;font-weight:500;color:var(--fg-3)">/${total}</span>`;
    kEnv.className = okCount === total ? 'num ok' : (okCount > 0 ? 'num warn' : 'num bad');
  } catch(e){}

  const ovr = await fetch('/api/settings/overrides').then(r=>r.json());
  const ovrEl = document.getElementById('overrides');
  document.getElementById('kpi-overrides').textContent = (ovr.overrides || []).length;
  if (!ovr.overrides.length){
    ovrEl.innerHTML = '<div class="empty">没有覆盖 · 所有工具使用 configs/prompts/ 中的原版提示词</div>';
  } else {
    ovrEl.innerHTML = '';
    ovr.overrides.forEach(o => {
      const id = o.filename.replace('.md','').replace(/^(\d)_(\d)_.*/, 'step$1.$2');
      const div = document.createElement('div');
      div.className = 'ovr-row';
      div.innerHTML = `
        <div>
          <code>${o.step_dir}/${o.filename}</code>
          <span style="color:var(--fg-3);font-family:var(--mono);font-size:11px;margin-left:10px">${(o.size/1024).toFixed(1)}KB · ${fmtDate(new Date(o.mtime*1000).toISOString())}</span>
        </div>
        <button class="reset" data-step="${o.step_dir}" data-id="${id}">重置回原版</button>`;
      ovrEl.appendChild(div);
    });
    ovrEl.querySelectorAll('button.reset').forEach(b => {
      b.onclick = async () => {
        if (!confirm('确认重置 ' + b.dataset.id + ' 到原版？')) return;
        await fetch(`/api/prompts/${b.dataset.step}/${b.dataset.id}`, {method:'DELETE'});
        load();
      };
    });
  }
}

// === 模型接入 主区 ===
// 状态机：
//   unset      → 显示「+ 模型接入」按钮（点开弹窗 picker）
//   oauth/ok   → 显示账号信息 + 「✕ 模型移除」按钮
//   api_key/ok → 显示 key masked + 「✕ 模型移除」按钮
// OAuth 和 API Key 互斥；同一时间只能选一种。
async function renderAuthSection(){
  let data;
  try { data = await fetch('/api/settings/auth').then(r=>r.json()); }
  catch(e){
    document.getElementById('auth-body').innerHTML =
      `<div style="color:var(--bad);font-size:12.5px">加载失败：${escapeHtml(String(e))}</div>`;
    return;
  }
  // Stash latest snapshot for the modal renderer (避免再发一次请求)
  window.__authSnap = data;

  const cur = data.current_mode;
  const o = data.modes.oauth;
  const ak = data.modes.api_key;
  const oauthReady = !!o.ready;
  const apiReady = !!ak.ready;
  const ready = oauthReady || apiReady;

  // 顶部 dot + tag
  document.getElementById('auth-dot').className = 'dot ' + (ready ? 'ok' : 'warn');
  document.getElementById('auth-mode-current').textContent =
    oauthReady ? 'OAuth · 已接入' :
    apiReady   ? 'API Key · 已接入' :
                 '未接入';

  // 「默认模型与精度」section — 只在接入后显示
  const secDefaults = document.getElementById('sec-defaults');
  if (secDefaults) secDefaults.hidden = !ready;

  // 主区 — 状态条 + 单个按钮
  const body = document.getElementById('auth-body');
  if (oauthReady) {
    const acc = o.account || {};
    const billingMap = {
      'subscription_max'              : 'Claude Max 订阅',
      'subscription_pro'              : 'Claude Pro 订阅',
      'subscription_team'             : 'Claude Team',
      'subscription_enterprise'       : 'Claude Enterprise',
      'google_play_subscription'      : 'Claude Pro 订阅 (Google Play)',
      'apple_app_store_subscription'  : 'Claude Pro 订阅 (App Store)',
      'api'                           : 'API 按量计费',
      'free'                          : '免费版',
    };
    const billingLabel = billingMap[acc.billing_type] || acc.billing_type || '订阅类型未知';
    const sub = acc.email
      ? `账号 <code>${escapeHtml(acc.email)}</code>${acc.billing_type ? ` · 订阅 <strong style="color:var(--ac-2)">${escapeHtml(billingLabel)}</strong>` : ''}${acc.organization_name ? ` · 组织 ${escapeHtml(acc.organization_name)}` : ''}`
      : `已通过 OAuth 接入 · 凭据存于本工具`;
    body.innerHTML = `
      <div class="access-pane">
        <div class="access-status ready">
          <span class="icon">🔗</span>
          <div class="info">
            <div class="title">OAuth 模式已接入</div>
            <div class="sub">${sub}</div>
          </div>
          <div class="access-btn-row">
            <button class="access-btn danger" id="btn-access-remove">✕ 模型移除</button>
          </div>
        </div>
        <div style="font-size:11.5px;color:var(--fg-3);line-height:1.55">
          移除会清掉本工具内的 OAuth 凭据（access_token / refresh_token），不影响其他客户端。想换 API Key 模式，先移除再点接入。
        </div>
      </div>`;
    document.getElementById('btn-access-remove').onclick = () => disconnectAccess('oauth');
    return;
  }
  if (apiReady) {
    body.innerHTML = `
      <div class="access-pane">
        <div class="access-status ready">
          <span class="icon">🔑</span>
          <div class="info">
            <div class="title">API Key 模式已接入</div>
            <div class="sub">当前 Key <code>${escapeHtml(ak.api_key_masked || '')}</code> · 调用按 Anthropic 价目计费</div>
          </div>
          <div class="access-btn-row">
            <button class="access-btn danger" id="btn-access-remove">✕ 模型移除</button>
          </div>
        </div>
        <div style="font-size:11.5px;color:var(--fg-3);line-height:1.55">
          移除会清除本机存储的 API Key；想换 OAuth 订阅模式，先移除再点接入。
        </div>
      </div>`;
    document.getElementById('btn-access-remove').onclick = () => disconnectAccess('api_key');
    return;
  }
  // 未接入
  body.innerHTML = `
    <div class="access-pane">
      <div class="access-status unset">
        <span class="icon">⚠️</span>
        <div class="info">
          <div class="title">尚未接入模型</div>
          <div class="sub">所有工具都依赖 Claude 模型；点右侧按钮选择接入方式（OAuth 订阅或 API Key）。</div>
        </div>
        <div class="access-btn-row">
          <button class="access-btn" id="btn-access-add">+ 模型接入</button>
        </div>
      </div>
    </div>`;
  document.getElementById('btn-access-add').onclick = () => openAccessModal();
}


// === 弹窗 ===
function openAccessModal(){
  renderAccessModal();
  const m = document.getElementById('auth-modal');
  m.hidden = false;
  // 绑定一次性 listener（避免重复绑定）
  if (!m.dataset.bound) {
    m.dataset.bound = '1';
    m.addEventListener('click', e => {
      // 点空白处或关闭按钮关闭
      if (e.target === m || e.target.dataset.act === 'close') closeAccessModal();
    });
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape' && !m.hidden) closeAccessModal();
    });
  }
}

function closeAccessModal(){
  document.getElementById('auth-modal').hidden = true;
}

function renderAccessModal(){
  const data = window.__authSnap;
  if (!data) { document.getElementById('auth-modal-body').innerHTML = '<div style="color:var(--fg-3)">加载中…</div>'; return; }
  const o = data.modes.oauth;
  const ak = data.modes.api_key;
  // OAuth 子卡内容 — 走 web OAuth 流程,不读本机 claude login
  let oauthCard;
  if (o.token_present) {
    oauthCard = `
      <div class="info-line">
        ✓ 本工具已存有 OAuth 凭据。点下方按钮即可激活;或换号请先点「OAuth 授权」重新走流程。
      </div>
      <div class="btn-row">
        <button class="mode-btn primary" data-act="oauth-authorize">↻ 重新授权(换号)</button>
      </div>`;
  } else {
    oauthCard = `
      <div class="info-line">
        在浏览器内完成 Claude 账号授权 — 凭据由本工具保存,不依赖本机 <code>claude login</code>。
        <div style="margin-top:6px;color:var(--fg-3);font-size:11.5px">点击下方按钮 → 新 tab 跳 claude.ai 授权 → 完成后自动回写本工具。</div>
      </div>
      <div class="btn-row">
        <button class="mode-btn primary" data-act="oauth-authorize">↗ OAuth 授权</button>
      </div>`;
  }
  // API Key 子卡内容
  const keyHasStored = ak.has_api_key === true;
  const apiCard = `
    <div class="info-line">
      从 <a href="https://console.anthropic.com/settings/keys" target="_blank" style="color:var(--ac-2)">console.anthropic.com</a> 获取 Key（<code>sk-ant-</code> 开头）。本机明文保存于 <code style="font-size:11px">~/Library/Application Support/AITestToolkit/configs/auth.json</code>（权限 0600）。
    </div>
    <div class="btn-row">
      <input class="api-input" id="modal-api-input" type="password" placeholder="${keyHasStored ? '已存 Key（' + escapeHtml(ak.api_key_masked || '') + '）— 粘贴新 Key 可替换' : 'sk-ant-api03-...'}"
             autocomplete="off" spellcheck="false">
      <label class="checkbox-line"><input id="modal-api-show" type="checkbox">明文</label>
    </div>
    <div class="btn-row">
      ${keyHasStored ? '<button class="mode-btn primary" data-act="use-stored-key">✓ 用已存 Key 接入</button>' : ''}
      <button class="mode-btn ${keyHasStored ? '' : 'primary'}" data-act="save-key">${keyHasStored ? '替换并接入' : '保存并接入'}</button>
    </div>`;

  document.getElementById('auth-modal-body').innerHTML = `
    <div style="font-size:12px;color:var(--fg-3);line-height:1.55;margin-bottom:4px">
      二选一 — 接入后可随时点「模型移除」再换。
    </div>
    <div class="mode-card active" data-mode="oauth">
      <div class="radio"></div>
      <div class="head">
        <span class="name">OAuth 登录</span>
        <span class="badge recommend">推荐 · 含订阅</span>
      </div>
      <div class="desc">${escapeHtml(o.summary)}</div>
      <div class="action-zone" style="display:flex">${oauthCard}</div>
    </div>
    <div class="mode-card active" data-mode="api_key">
      <div class="radio"></div>
      <div class="head">
        <span class="name">API Key</span>
        <span class="badge metered">${escapeHtml(ak.tag || '按量计费')}</span>
      </div>
      <div class="desc">${escapeHtml(ak.summary)}</div>
      <div class="action-zone" style="display:flex">${apiCard}</div>
    </div>`;

  // 绑事件 — 弹窗内按钮
  document.getElementById('auth-modal-body').querySelectorAll('button[data-act]').forEach(btn => {
    btn.onclick = async (e) => {
      e.stopPropagation();
      const act = btn.dataset.act;
      btn.disabled = true;
      try {
        if (act === 'oauth-authorize') {
          await doOAuthAuthorize();
        } else if (act === 'use-stored-key') {
          await switchAuthMode('api_key', null);
          closeAccessModal();
        } else if (act === 'save-key') {
          const inp = document.getElementById('modal-api-input');
          const v = (inp ? inp.value : '').trim();
          if (!v) { alert('请粘贴 API Key（以 sk-ant- 开头）'); btn.disabled = false; return; }
          if (!v.startsWith('sk-') && !confirm('Key 不是以 sk- 开头，确认保存？')) { btn.disabled = false; return; }
          await switchAuthMode('api_key', v);
          closeAccessModal();
        }
      } catch(err) {
        alert('操作失败：' + (err.detail || err.message || JSON.stringify(err)));
      } finally {
        btn.disabled = false;
      }
    };
  });
  const showCk = document.getElementById('modal-api-show');
  if (showCk) showCk.onchange = (e) => {
    const inp = document.getElementById('modal-api-input');
    if (inp) inp.type = e.target.checked ? 'text' : 'password';
  };
}


// Web OAuth flow (OOB 模式):
//   1. POST /api/auth/oauth/start → 拿 authorize_url + state
//   2. window.open() 打开新 tab → 用户在 claude.ai 授权
//   3. Anthropic 把 code 显示在 console.anthropic.com 内部页面，让用户复制
//   4. 用户回 toolkit 弹窗，粘到输入框 → POST /api/auth/oauth/exchange 换 token
async function doOAuthAuthorize(){
  let startResp;
  try {
    const r = await fetch('/api/auth/oauth/start', {method:'POST'});
    if (!r.ok) {
      const err = await r.json().catch(()=>({detail:r.statusText}));
      alert('启动 OAuth 失败：' + (err.detail || JSON.stringify(err)));
      return;
    }
    startResp = await r.json();
  } catch(e) {
    alert('启动 OAuth 失败：' + (e.message || e));
    return;
  }
  const authWin = window.open(startResp.authorize_url, '_blank');
  if (!authWin) {
    alert('浏览器拦截了新 tab。请允许弹窗后重试，或复制下面 URL 手动打开：\n\n' + startResp.authorize_url);
  }
  // 弹窗内换成"粘贴 code"状态
  const body = document.getElementById('auth-modal-body');
  if (!body) return;
  body.innerHTML = `
    <div class="info-line" style="line-height:1.6">
      <div style="font-size:14px;color:var(--fg);margin-bottom:10px"><strong>1.</strong> 在新打开的 tab 完成 Anthropic 账号登录 + 授权</div>
      <div style="font-size:14px;color:var(--fg);margin-bottom:10px"><strong>2.</strong> 授权完成后，Anthropic 会显示一段授权码 — 复制整段</div>
      <div style="font-size:14px;color:var(--fg);margin-bottom:6px"><strong>3.</strong> 把授权码粘贴到下方输入框，点「完成接入」</div>
      <div style="font-size:11.5px;color:var(--fg-3);margin-bottom:4px">
        授权 tab 没自动打开？
        <a href="${startResp.authorize_url}" target="_blank" style="color:var(--ac-2);text-decoration:underline">点这里手动打开</a>
      </div>
    </div>
    <div class="btn-row" style="margin-top:6px">
      <input class="api-input" id="oauth-code-input" type="text"
             placeholder="粘贴授权码（形如 abc-def#state=...）"
             autocomplete="off" spellcheck="false" style="flex:1">
    </div>
    <div class="btn-row" style="margin-top:2px">
      <button class="mode-btn primary" data-act="submit-code">✓ 完成接入</button>
      <button class="mode-btn" data-act="cancel-oauth">取消</button>
    </div>
    <div id="oauth-exchange-msg" style="font-size:12px;color:var(--fg-3);margin-top:6px;min-height:16px"></div>`;

  const inp = document.getElementById('oauth-code-input');
  const msg = document.getElementById('oauth-exchange-msg');
  if (inp) inp.focus();

  const submit = async () => {
    const v = (inp.value || '').trim();
    if (!v) { msg.style.color='var(--warn)'; msg.textContent='请粘贴授权码'; return; }
    msg.style.color='var(--fg-3)'; msg.textContent='换取 access_token…';
    try {
      const r = await fetch('/api/auth/oauth/exchange', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({code:v, state: startResp.state})
      });
      if (r.ok) {
        const d = await r.json();
        msg.style.color='var(--ok)'; msg.textContent='✓ 接入成功';
        toastSettings('✓ OAuth 接入成功' + (d.account && d.account.email ? ` · ${d.account.email}` : ''));
        await new Promise(r=>setTimeout(r,400));
        closeAccessModal();
        await renderAuthSection();
        await load();
      } else {
        const err = await r.json().catch(()=>({detail:r.statusText}));
        msg.style.color='var(--bad)'; msg.textContent='✗ ' + (err.detail || JSON.stringify(err));
      }
    } catch(e) {
      msg.style.color='var(--bad)'; msg.textContent='✗ ' + (e.message || e);
    }
  };

  body.querySelector('button[data-act="submit-code"]').onclick = submit;
  body.querySelector('button[data-act="cancel-oauth"]').onclick = () => renderAccessModal();
  if (inp) inp.addEventListener('keydown', e => { if (e.key==='Enter') submit(); });
}


// 移除（断开）当前模式
async function disconnectAccess(mode){
  const modeLabel = mode === 'oauth' ? 'OAuth' : 'API Key';
  if (!confirm(`确认移除「${modeLabel}」模式？\n\n` +
               (mode === 'oauth'
                 ? '会清除本工具内存的 OAuth 凭据（access_token / refresh_token），不影响其他客户端。'
                 : '会清除本工具存储的 API Key — 想保留可先复制。'))) return;
  try {
    await fetch('/api/settings/auth/disconnect', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({mode, purge: mode === 'api_key'})
    });
    if (mode === 'api_key') {
      // 同时清除存储的 key
      await fetch('/api/settings/auth/api-key', {method:'DELETE'}).catch(()=>{});
    }
    toastSettings('已移除「' + modeLabel + '」模式');
  } catch(e) {
    alert('移除失败：' + (e.message || JSON.stringify(e)));
  }
  await renderAuthSection();
  await load();
}

async function switchAuthMode(mode, apiKey){
  try {
    const r = await fetch('/api/settings/auth', {
      method:'PUT', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ mode, api_key: apiKey })
    });
    if (!r.ok) {
      const e = await r.json().catch(()=>({detail:r.statusText}));
      alert('切换失败：' + (e.detail || JSON.stringify(e)));
      return;
    }
    await renderAuthSection();
    // 刷新主 KPI 和 Claude 信息卡（loading state 改了）
    await load();
    const labels = {oauth:'OAuth 订阅', api_key:'API Key'};
    toastSettings(`已接入「${labels[mode] || mode}」模式 · 立即可用`);
  } catch(e) {
    alert('切换失败：' + e);
  }
}

function toastSettings(msg){
  // 复用全局 toast 若存在；否则简单 alert
  if (typeof toast === 'function') { toast(msg); return; }
  const t = document.createElement('div');
  t.textContent = msg;
  t.style.cssText = 'position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:var(--surface-2);border:1px solid var(--ac-line);color:var(--fg);padding:10px 18px;border-radius:8px;font-size:13px;z-index:9999;box-shadow:0 8px 24px rgba(0,0,0,.4)';
  document.body.appendChild(t);
  setTimeout(()=>t.remove(), 2400);
}

async function renderToolEnv(){
  const tEnv = await fetch('/api/settings/tools').then(r=>r.json());
  const tbody = document.getElementById('tool-env-tbody');
  tbody.innerHTML = '';
  tEnv.tools.forEach(t => {
    if (!t.requirements.length){
      tbody.insertAdjacentHTML('beforeend',
        `<tr><td><span class="name">${t.icon} ${t.name}</span></td><td colspan="6" style="color:var(--fg-3)">无额外环境需求</td></tr>`);
      return;
    }
    t.requirements.forEach((r, i) => {
      const ok = r.installed;
      const reqClass = r.required ? 'req' : 'opt';
      const reqLabel = r.required ? '必需' : '可选';
      let action = '';
      if (!ok){
        action = `<button class="install-btn" data-target="${r.pkg}">↓ 安装</button>`;
      } else if (r.pkg === 'playwright' && r.browsers_installed === false){
        action = `<button class="install-btn" data-target="playwright_browsers" title="playwright 已装但缺浏览器">↓ 装浏览器</button>`;
      } else {
        action = '';
      }
      tbody.insertAdjacentHTML('beforeend', `
        <tr>
          <td>${i === 0 ? `<span class="name">${t.icon} ${t.name}</span>` : ''}</td>
          <td><span class="pkg">${r.pkg}</span></td>
          <td><span class="${reqClass}">${reqLabel}</span></td>
          <td><span class="${ok ? 'ok' : 'bad'}">${ok ? '✓' : '✗'}</span>${(r.pkg==='playwright' && ok && r.browsers_installed===false) ? ' <span class="bad" style="font-size:10px">浏览器缺</span>' : ''}</td>
          <td>${r.version || '—'}</td>
          <td style="color:var(--fg-3)">${r.purpose}</td>
          <td>${action}</td>
        </tr>`);
    });
  });
  // wire install buttons
  tbody.querySelectorAll('.install-btn').forEach(btn => {
    btn.onclick = () => installPackage(btn.dataset.target, btn);
  });
}

async function installPackage(target, btn){
  if (!confirm(`确认安装 ${target}？将在当前 venv 内执行 pip 安装，可能需要 1-3 分钟。`)) return;
  btn.disabled = true; btn.textContent = '安装中…';
  const toast = document.getElementById('install-toast');
  document.getElementById('it-title').textContent = `安装 ${target}`;
  document.getElementById('it-cmd').textContent = '提交中…';
  document.getElementById('it-status').className = 'status-tag queued';
  document.getElementById('it-status').textContent = 'queued';
  document.getElementById('it-log').textContent = '';
  document.getElementById('it-elapsed').textContent = '';
  toast.classList.add('show');

  const startedAt = Date.now();
  let timer = null;
  try {
    const j = await fetch('/api/settings/install', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({target}),
    }).then(r=>r.ok ? r.json() : r.json().then(e=>Promise.reject(e)));

    const poll = async () => {
      try {
        const s = await fetch('/api/settings/install/' + j.job_id).then(r=>r.json());
        document.getElementById('it-cmd').textContent = '$ ' + (s.command || '·');
        document.getElementById('it-status').className = 'status-tag ' + s.status;
        document.getElementById('it-status').textContent = s.status;
        document.getElementById('it-log').textContent = s.log || '';
        document.getElementById('it-elapsed').textContent = ((Date.now() - startedAt)/1000).toFixed(1) + 's';
        if (s.status === 'succeeded' || s.status === 'failed'){
          clearInterval(timer);
          btn.disabled = false; btn.textContent = '↓ 安装';
          if (s.status === 'succeeded'){
            await renderToolEnv();
          }
        }
      } catch(e){ clearInterval(timer); }
    };
    poll();
    timer = setInterval(poll, 2000);
  } catch(e){
    document.getElementById('it-status').className = 'status-tag failed';
    document.getElementById('it-status').textContent = 'failed';
    document.getElementById('it-log').textContent = JSON.stringify(e);
    btn.disabled = false; btn.textContent = '↓ 安装';
  }
}

document.getElementById('env-refresh').onclick = renderToolEnv;

load();
</script>
</body></html>
"""


@app.get("/settings", response_class=HTMLResponse)
async def settings_page() -> str:
    return _inject_shared_overlays(SETTINGS_HTML)


# =====================================================================
# Admin · 用户管理页 (仅 admin 可访问)
# =====================================================================

ADMIN_USERS_HTML = r"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>用户管理 — 天枢·裁决</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@300;400;500;600;700&family=Noto+Sans+SC:wght@300;400;500;600&family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
:root {
  --paper:#ffffff;--paper-2:#ebebeb;--ink:#0a0a0a;--ink-2:#262626;--ink-3:#4a4a4a;--ink-4:#6e6e6e;
  --line:#bdbdbd;--line-2:#9e9e9e;--accent:#a8401f;--accent-h:#82301a;--accent-soft:rgba(168,64,31,.14);
  --bad:#8a2d12;--ok:#4f6b35;--warn:#876b1f;
  --serif:'Noto Serif SC',Georgia,serif;
  --sans:'Noto Sans SC','PingFang SC',-apple-system,sans-serif;
  --mono:'Inter','SF Mono',ui-monospace,Menlo,monospace;
}
*{box-sizing:border-box}
html,body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--sans);
  font-weight:300;font-size:15px;line-height:1.7;-webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}
button{font-family:inherit;cursor:pointer}

.topbar{display:flex;align-items:center;gap:12px;padding:0 24px;height:56px;
  border-bottom:1px solid var(--line);background:#fff;position:sticky;top:0;z-index:10}
.brand-link{display:inline-flex;align-items:center;gap:10px;margin-right:24px;text-decoration:none;color:inherit}
.brand-link svg{color:var(--accent);width:24px;height:24px}
.brand{font-family:var(--serif);font-size:19px;font-weight:600;letter-spacing:.18em;color:var(--ink)}
.brand .sep{color:var(--accent);margin:0 6px;font-weight:400}
.topbar nav{display:flex;gap:2px;margin-left:24px;margin-right:auto;font-family:var(--sans)}
.topbar nav a{font-size:13px;color:var(--ink-2);padding:6px 12px;border-radius:6px;
  text-decoration:none;letter-spacing:.04em;transition:all .15s}
.topbar nav a:hover{background:var(--paper-2,#ebebeb);color:var(--ink)}
.topbar nav a.active{background:var(--accent-soft,rgba(168,64,31,.12));color:var(--accent);font-weight:500}
.spacer{display:none}
.user-chip{font-family:var(--sans);font-size:12.5px;color:var(--ink-2);display:inline-flex;
  align-items:center;gap:8px;font-weight:600}
.user-chip .admin-tag{color:var(--accent);font-size:10.5px;letter-spacing:.18em;font-weight:600}
.logout-btn{background:none;border:1px solid var(--line);color:var(--ink-3);
  padding:4px 14px;border-radius:999px;font-family:var(--sans);font-size:11.5px;letter-spacing:.04em;
  margin-left:4px;transition:all .15s}
.logout-btn:hover{border-color:var(--accent);color:var(--accent)}

.shell{max-width:1080px;margin:0 auto;padding:48px 48px}
.page-head{display:flex;align-items:baseline;justify-content:space-between;
  border-bottom:1px solid var(--line);padding-bottom:18px;margin-bottom:36px}
h1{font-family:var(--serif);font-size:30px;font-weight:500;letter-spacing:.05em;margin:0}
.page-sub{font-size:13px;color:var(--ink-3);font-family:var(--mono);letter-spacing:.06em}

.section{margin-bottom:48px}
.section-title{font-family:var(--mono);font-size:11px;letter-spacing:.22em;color:var(--ink-3);
  text-transform:uppercase;margin-bottom:18px;padding-bottom:8px;border-bottom:1px solid var(--line)}

/* 新建用户表单 */
.create-form{background:#fff;border:1px solid var(--line);border-radius:6px;
  padding:28px 32px;display:grid;grid-template-columns:repeat(4,1fr);gap:18px}
.create-form .field{display:flex;flex-direction:column;gap:6px}
.create-form .field.span2{grid-column:span 2}
.create-form label{font-family:var(--mono);font-size:10.5px;color:var(--ink-3);
  letter-spacing:.18em;text-transform:uppercase}
.create-form input,.create-form select{font-family:var(--sans);font-size:14px;
  padding:9px 12px;border:1px solid var(--line);border-radius:3px;background:#fff;
  color:var(--ink);outline:none;transition:border .15s}
.create-form input:focus,.create-form select:focus{border-color:var(--accent);
  box-shadow:0 0 0 3px var(--accent-soft)}
.create-form .submit-cell{grid-column:span 4;display:flex;justify-content:flex-end;align-items:end;gap:14px}
.create-form .submit-cell .form-err{flex:1;color:var(--bad);font-size:12.5px}
.create-form button{padding:11px 24px;background:var(--ink);color:#fff;border:none;
  border-radius:3px;font-family:var(--serif);font-size:14px;letter-spacing:.22em;
  transition:background .18s}
.create-form button:hover{background:var(--accent)}
.create-form button:disabled{opacity:.5;cursor:not-allowed}

/* 用户表格 */
table.users{width:100%;border-collapse:collapse;background:#fff;
  border:1px solid var(--line);border-radius:6px;overflow:hidden}
table.users th{background:var(--paper-2);font-family:var(--mono);font-size:10.5px;
  color:var(--ink-3);letter-spacing:.18em;text-transform:uppercase;
  text-align:left;padding:14px 18px;font-weight:500;border-bottom:1px solid var(--line)}
table.users td{padding:14px 18px;border-bottom:1px solid var(--line);font-size:14px}
table.users tr:last-child td{border-bottom:none}
table.users tr:hover{background:var(--paper-2)}
.role-tag{font-family:var(--mono);font-size:10.5px;letter-spacing:.16em;padding:2px 8px;
  border-radius:3px;text-transform:uppercase}
.role-tag.admin{color:#fff;background:var(--accent)}
.role-tag.user{color:var(--ink-3);border:1px solid var(--line)}
.row-actions{display:flex;gap:8px;justify-content:flex-end}
.row-actions button{padding:5px 10px;font-family:var(--mono);font-size:11px;
  border-radius:3px;border:1px solid var(--line);background:#fff;color:var(--ink-2);
  letter-spacing:.04em}
.row-actions button:hover{border-color:var(--accent);color:var(--accent)}
.row-actions button.danger:hover{border-color:var(--bad);color:var(--bad)}
.mono{font-family:var(--mono);font-size:12.5px;color:var(--ink-2)}
.muted{color:var(--ink-3)}
.empty{padding:48px;text-align:center;color:var(--ink-3);font-family:var(--serif);letter-spacing:.04em}

/* Modal */
.modal-overlay{position:fixed;inset:0;background:rgba(26,26,26,.42);display:none;
  align-items:center;justify-content:center;z-index:100;backdrop-filter:blur(2px)}
.modal-overlay.open{display:flex}
.modal{background:#fff;border-radius:6px;padding:32px 36px;width:100%;max-width:420px;
  box-shadow:0 12px 48px rgba(0,0,0,.2)}
.modal h3{font-family:var(--serif);font-size:20px;font-weight:500;margin:0 0 6px}
.modal .modal-sub{color:var(--ink-3);font-size:13px;margin-bottom:20px}
.modal .field{display:flex;flex-direction:column;gap:6px;margin-bottom:14px}
.modal label{font-family:var(--mono);font-size:10.5px;color:var(--ink-3);letter-spacing:.18em;text-transform:uppercase}
.modal input{font-family:var(--sans);font-size:14px;padding:10px 12px;border:1px solid var(--line);
  border-radius:3px;background:#fff;color:var(--ink);outline:none}
.modal input:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.modal-err{color:var(--bad);font-size:12.5px;margin-top:8px;display:none}
.modal-err.show{display:block}
.modal-actions{display:flex;justify-content:flex-end;gap:12px;margin-top:24px}
.modal-actions button{padding:9px 20px;font-family:var(--mono);font-size:12px;letter-spacing:.1em;
  border-radius:3px;border:1px solid var(--line);background:#fff;color:var(--ink-2)}
.modal-actions button.primary{background:var(--ink);color:#fff;border-color:var(--ink)}
.modal-actions button.primary:hover{background:var(--accent);border-color:var(--accent)}
.toast{position:fixed;top:24px;right:24px;background:var(--ink);color:#fff;padding:12px 20px;
  border-radius:4px;font-size:13px;z-index:200;opacity:0;transform:translateY(-8px);
  transition:opacity .2s,transform .2s;font-family:var(--mono);letter-spacing:.04em}
.toast.show{opacity:1;transform:translateY(0)}
.toast.bad{background:var(--bad)}
.toast.ok{background:var(--ok)}
</style>
</head><body>
<header class="topbar">
  <a class="brand-link" href="/tools">
    <svg viewBox="0 0 24 24" fill="currentColor" width="26" height="26"><path d="M12 1.5L13.4 10.6L22.5 12L13.4 13.4L12 22.5L10.6 13.4L1.5 12L10.6 10.6Z"/></svg>
    <span class="brand">天枢<span class="sep">·</span>裁决</span>
  </a>
  <nav>
    <a href="/tools">工具</a>
    <a href="/reports">报告</a>
    <a href="/settings">设置</a>
    <a href="/admin/users" class="active">用户管理</a>
  </nav>
  <div class="spacer"></div>
  <div class="user-chip" id="user-chip"></div>
  <button class="logout-btn" id="logout-btn">登出</button>
</header>

<main class="shell">
  <div class="page-head">
    <h1>用户管理</h1>
    <div class="page-sub" id="user-count">— 位用户</div>
  </div>

  <!-- 新建用户 -->
  <section class="section">
    <div class="section-title">
      <span>新建用户</span>
      <button type="button" id="open-bulk-btn"
        style="float:right;font-family:var(--mono);font-size:11px;letter-spacing:.12em;
               padding:5px 14px;border:1px solid var(--line-2);background:#fff;
               color:var(--ink-2);border-radius:3px;cursor:pointer;transition:all .15s">
        批量创建 →
      </button>
    </div>
    <form class="create-form" id="create-form" autocomplete="off">
      <div class="field">
        <label>用户名</label>
        <input id="c-username" required placeholder="如 lisi" autocomplete="off">
      </div>
      <div class="field">
        <label>初始密码</label>
        <input id="c-password" type="text" required placeholder="至少 6 位" autocomplete="off">
      </div>
      <div class="field">
        <label>显示名 (可选)</label>
        <input id="c-display" placeholder="留空则同用户名" autocomplete="off">
      </div>
      <div class="field">
        <label>角色</label>
        <select id="c-role">
          <option value="user" selected>普通用户</option>
          <option value="admin">管理员</option>
        </select>
      </div>
      <div class="submit-cell">
        <span class="form-err" id="form-err"></span>
        <button type="submit" id="create-btn">创 建 用 户</button>
      </div>
    </form>
  </section>

  <!-- 用户列表 -->
  <section class="section">
    <div class="section-title">用户列表</div>
    <div id="list-wrap">加载中…</div>
  </section>
</main>

<!-- 重置密码 modal -->
<div class="modal-overlay" id="reset-modal">
  <div class="modal">
    <h3 id="reset-title">重置密码</h3>
    <div class="modal-sub" id="reset-sub"></div>
    <div class="field">
      <label>新密码</label>
      <input id="reset-pwd" type="text" placeholder="至少 6 位">
    </div>
    <div class="modal-err" id="reset-err"></div>
    <div class="modal-actions">
      <button id="reset-cancel">取消</button>
      <button class="primary" id="reset-ok">确认重置</button>
    </div>
  </div>
</div>

<!-- 批量创建 modal — 多行可叠加表单 -->
<style>
.bulk-modal{max-width:900px;max-height:90vh;overflow-y:auto}
.bulk-modal .bulk-tip{background:var(--paper-2);border-left:3px solid var(--accent);
  padding:10px 14px;font-size:12.5px;color:var(--ink-2);line-height:1.7;margin:6px 0 14px;
  border-radius:0 4px 4px 0}
.bulk-modal .bulk-defaults{display:flex;align-items:center;gap:14px;margin:0 0 14px;
  padding:10px 14px;background:var(--paper-2);border-radius:4px;border:1px solid var(--line)}
.bulk-modal .bulk-defaults label{font-family:var(--mono);font-size:11px;color:var(--ink-3);
  letter-spacing:.14em;text-transform:uppercase}
.bulk-modal .bulk-defaults select{font-family:var(--sans);font-size:13px;padding:5px 10px;
  border:1px solid var(--line);border-radius:3px;background:#fff;color:var(--ink)}

/* 行容器 */
.bulk-rows{display:flex;flex-direction:column;gap:8px;max-height:380px;overflow-y:auto;
  padding:2px;margin-bottom:10px}
.bulk-row-item{display:grid;
  grid-template-columns:30px minmax(0,1.4fr) minmax(0,1.4fr) minmax(0,1fr) 110px 28px;
  gap:10px;align-items:center;padding:8px;border-radius:4px;
  transition:background .12s;border:1px solid transparent}
.bulk-row-item:hover{background:var(--paper-2);border-color:var(--line)}
.bulk-row-item .row-num{font-family:var(--mono);font-size:11px;color:var(--ink-3);
  text-align:center;letter-spacing:.1em}
.bulk-row-item input,.bulk-row-item select{font-family:var(--sans);font-size:13.5px;
  padding:8px 10px;border:1px solid var(--line);border-radius:3px;background:#fff;
  color:var(--ink);outline:none;transition:border .15s;width:100%}
.bulk-row-item input:focus,.bulk-row-item select:focus{
  border-color:var(--accent);box-shadow:0 0 0 2px var(--accent-soft)}
.bulk-row-item input.has-error{border-color:var(--bad);background:rgba(138,45,18,.04)}
.bulk-row-item .del-row{width:28px;height:28px;border:1px solid var(--line);
  background:#fff;color:var(--ink-3);border-radius:3px;cursor:pointer;
  font-size:14px;line-height:1;padding:0;display:flex;align-items:center;justify-content:center;
  transition:all .15s}
.bulk-row-item .del-row:hover{border-color:var(--bad);color:var(--bad)}
.bulk-row-item .del-row:disabled{opacity:.3;cursor:not-allowed}
.bulk-row-headers{display:grid;
  grid-template-columns:30px minmax(0,1.4fr) minmax(0,1.4fr) minmax(0,1fr) 110px 28px;
  gap:10px;padding:0 8px 6px;border-bottom:1px solid var(--line);margin-bottom:6px}
.bulk-row-headers div{font-family:var(--mono);font-size:10.5px;color:var(--ink-3);
  letter-spacing:.16em;text-transform:uppercase}

.add-row-btn{display:flex;align-items:center;justify-content:center;gap:8px;
  width:100%;padding:10px;border:1px dashed var(--line-2);background:transparent;
  color:var(--ink-2);border-radius:4px;cursor:pointer;font-family:var(--mono);
  font-size:12px;letter-spacing:.1em;transition:all .15s;margin-bottom:8px}
.add-row-btn:hover{border-color:var(--accent);color:var(--accent);background:var(--accent-soft)}
.add-row-btn .plus{font-size:14px}

/* 结果 */
.bulk-results{margin-top:20px;border-top:1px solid var(--line);padding-top:18px}
.bulk-results .bulk-summary{display:flex;gap:18px;font-family:var(--mono);font-size:12px;
  margin-bottom:14px;color:var(--ink-2)}
.bulk-results .bulk-summary .ok{color:var(--ok);font-weight:600}
.bulk-results .bulk-summary .fail{color:var(--bad);font-weight:600}
.bulk-results table{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:8px}
.bulk-results th{background:var(--paper-2);font-family:var(--mono);font-size:10.5px;
  letter-spacing:.16em;color:var(--ink-3);text-transform:uppercase;
  text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);font-weight:500}
.bulk-results td{padding:8px 10px;border-bottom:1px solid var(--line)}
.bulk-results .pwd-cell{font-family:var(--mono);font-size:12px;color:var(--accent);
  font-weight:600;letter-spacing:.04em}
.bulk-results .fail-cell{color:var(--bad);font-size:12px}
.bulk-export-row{display:flex;gap:10px;margin-top:14px;flex-wrap:wrap;align-items:center}
.bulk-export-row button{padding:7px 14px;font-family:var(--mono);font-size:11.5px;
  letter-spacing:.08em;border:1px solid var(--line);background:#fff;color:var(--ink-2);
  border-radius:3px;cursor:pointer;transition:all .15s}
.bulk-export-row button:hover{border-color:var(--accent);color:var(--accent)}
</style>
<div class="modal-overlay" id="bulk-modal">
  <div class="modal bulk-modal">
    <h3>批量创建用户</h3>
    <div class="modal-sub">每行一个用户。失败的不会阻塞其他行,完成后可下载 CSV 把账号密码发给员工。</div>

    <div class="bulk-tip">
      ① 密码留空 → 按下方「默认密码长度」自动生成 &nbsp;·&nbsp;
      ② 角色留空 → 用「默认角色」&nbsp;·&nbsp;
      ③ 用户名重复会失败但不阻塞其他行
    </div>

    <div class="bulk-defaults">
      <label>默认角色</label>
      <select id="bulk-default-role">
        <option value="user" selected>普通用户</option>
        <option value="admin">管理员</option>
      </select>
      <label style="margin-left:auto">自动密码长度</label>
      <select id="bulk-pwd-length">
        <option value="8">8 位</option>
        <option value="10" selected>10 位</option>
        <option value="12">12 位</option>
        <option value="16">16 位</option>
      </select>
    </div>

    <!-- 表头 -->
    <div class="bulk-row-headers">
      <div>#</div>
      <div>用户名 *</div>
      <div>初始密码</div>
      <div>显示名</div>
      <div>角色</div>
      <div></div>
    </div>

    <!-- 动态行容器 -->
    <div class="bulk-rows" id="bulk-rows"></div>

    <!-- 加行按钮 -->
    <button type="button" class="add-row-btn" id="add-row-btn">
      <span class="plus">+</span><span>添加一行</span>
    </button>

    <div class="modal-err" id="bulk-err"></div>

    <div class="modal-actions">
      <button id="bulk-cancel">取消</button>
      <button class="primary" id="bulk-submit">开 始 创 建</button>
    </div>

    <div class="bulk-results" id="bulk-results" style="display:none"></div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
let currentUser = null;
let users = [];

function fmtTs(t){
  if (!t) return '—';
  const d = new Date(t * 1000);
  return d.toLocaleString('zh-CN',{hour12:false}).replace(/\//g,'-');
}

function toast(msg, kind){
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast show' + (kind ? ' ' + kind : '');
  setTimeout(() => { t.className = 'toast'; }, 2500);
}

async function fetchMe(){
  const r = await fetch('/api/auth/me').then(r=>r.json());
  if (!r.authenticated){ location.href = '/login'; return; }
  if (r.user.role !== 'admin'){ alert('需要管理员权限'); location.href = '/tools'; return; }
  currentUser = r.user;
  const tag = r.user.role === 'admin' ? '<span class="admin-tag">[admin]</span>' : '';
  document.getElementById('user-chip').innerHTML = tag + (r.user.display_name || r.user.username);
}

async function loadUsers(){
  try {
    const r = await fetch('/api/auth/users').then(r=>r.json());
    users = r.users || [];
    document.getElementById('user-count').textContent = users.length + ' 位用户';
    renderUsers();
  } catch(e){ toast('加载用户失败:' + e.message, 'bad'); }
}

function renderUsers(){
  const w = document.getElementById('list-wrap');
  if (!users.length){
    w.innerHTML = '<div class="empty">暂无用户</div>';
    return;
  }
  const rows = users.map(u => {
    const isMe = currentUser && u.id === currentUser.id;
    const isLastAdmin = u.role === 'admin' && users.filter(x => x.role === 'admin').length === 1;
    const delDisabled = isMe || isLastAdmin;
    const delTitle = isMe ? '不能删除自己' : (isLastAdmin ? '不能删除最后一个管理员' : '删除用户');
    return `<tr>
      <td class="mono">${u.id}</td>
      <td><strong>${escapeHtml(u.username)}</strong></td>
      <td class="muted">${escapeHtml(u.display_name || '—')}</td>
      <td><span class="role-tag ${u.role}">${u.role === 'admin' ? 'ADMIN' : 'USER'}</span></td>
      <td class="mono muted">${fmtTs(u.created_at)}</td>
      <td class="mono muted">${fmtTs(u.last_login_at)}</td>
      <td>
        <div class="row-actions">
          <button onclick="openReset(${u.id}, '${escapeHtml(u.username)}')">重置密码</button>
          <button class="danger" onclick="deleteUser(${u.id}, '${escapeHtml(u.username)}')"
                  ${delDisabled ? 'disabled style="opacity:.4;cursor:not-allowed"' : ''}
                  title="${delTitle}">删除</button>
        </div>
      </td>
    </tr>`;
  }).join('');
  w.innerHTML = `<table class="users">
    <thead><tr>
      <th>ID</th><th>用户名</th><th>显示名</th><th>角色</th>
      <th>创建</th><th>最近登录</th><th style="text-align:right">操作</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

function escapeHtml(s){
  return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

document.getElementById('create-form').onsubmit = async (e) => {
  e.preventDefault();
  const err = document.getElementById('form-err');
  err.textContent = '';
  const btn = document.getElementById('create-btn');
  btn.disabled = true;
  const body = {
    username: document.getElementById('c-username').value.trim(),
    password: document.getElementById('c-password').value,
    display_name: document.getElementById('c-display').value.trim(),
    role: document.getElementById('c-role').value,
  };
  try {
    const r = await fetch('/api/auth/users', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body),
    });
    if (!r.ok){
      const d = await r.json().catch(()=>({}));
      err.textContent = d.detail || '创建失败';
      btn.disabled = false;
      return;
    }
    const d = await r.json();
    toast('✓ 已创建 ' + d.user.username, 'ok');
    document.getElementById('create-form').reset();
    btn.disabled = false;
    await loadUsers();
  } catch(e){
    err.textContent = e.message;
    btn.disabled = false;
  }
};

let resetTargetId = null;
function openReset(id, username){
  resetTargetId = id;
  document.getElementById('reset-title').textContent = '重置密码 · ' + username;
  document.getElementById('reset-sub').textContent =
    '不需要原密码,生效后该用户必须用新密码重新登录。';
  document.getElementById('reset-pwd').value = '';
  document.getElementById('reset-err').classList.remove('show');
  document.getElementById('reset-modal').classList.add('open');
  setTimeout(() => document.getElementById('reset-pwd').focus(), 50);
}
document.getElementById('reset-cancel').onclick = () => {
  document.getElementById('reset-modal').classList.remove('open');
};
document.getElementById('reset-ok').onclick = async () => {
  const pwd = document.getElementById('reset-pwd').value;
  const err = document.getElementById('reset-err');
  if (pwd.length < 6){
    err.textContent = '密码至少 6 位';
    err.classList.add('show');
    return;
  }
  try {
    const r = await fetch('/api/auth/users/' + resetTargetId + '/reset_password', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({new_password: pwd}),
    });
    if (!r.ok){
      const d = await r.json().catch(()=>({}));
      err.textContent = d.detail || '重置失败';
      err.classList.add('show');
      return;
    }
    toast('✓ 密码已更新', 'ok');
    document.getElementById('reset-modal').classList.remove('open');
    await loadUsers();
  } catch(e){
    err.textContent = e.message;
    err.classList.add('show');
  }
};

async function deleteUser(id, username){
  if (!confirm('确认删除 ' + username + '?\n该用户的所有报告归属也会留在系统里,但他无法再登录。')) return;
  try {
    const r = await fetch('/api/auth/users/' + id, {method:'DELETE'});
    if (!r.ok){
      const d = await r.json().catch(()=>({}));
      toast('删除失败:' + (d.detail || r.status), 'bad');
      return;
    }
    toast('✓ 已删除 ' + username, 'ok');
    await loadUsers();
  } catch(e){ toast('删除请求失败:' + e.message, 'bad'); }
}

document.getElementById('logout-btn').onclick = async () => {
  await fetch('/api/auth/logout', {method:'POST'});
  location.href = '/login';
};

// ============== 批量创建(行式表单) ==============
let bulkRowCounter = 0;

function makeBulkRow(){
  bulkRowCounter += 1;
  const id = 'br-' + bulkRowCounter;
  const div = document.createElement('div');
  div.className = 'bulk-row-item';
  div.dataset.rid = String(bulkRowCounter);
  div.innerHTML = `
    <div class="row-num"></div>
    <input class="bulk-username" type="text" placeholder="必填,如 lisi" autocomplete="off">
    <input class="bulk-password" type="text" placeholder="留空自动生成" autocomplete="off">
    <input class="bulk-display"  type="text" placeholder="留空同用户名" autocomplete="off">
    <select class="bulk-role">
      <option value="">默认</option>
      <option value="user">普通用户</option>
      <option value="admin">管理员</option>
    </select>
    <button class="del-row" type="button" title="删除此行">×</button>
  `;
  div.querySelector('.del-row').onclick = () => {
    const rows = document.querySelectorAll('#bulk-rows .bulk-row-item');
    if (rows.length <= 1){
      // 最后一行只清空,不删
      div.querySelectorAll('input').forEach(i => i.value = '');
      div.querySelector('select').selectedIndex = 0;
      div.querySelectorAll('input').forEach(i => i.classList.remove('has-error'));
    } else {
      div.remove();
      renumberRows();
    }
  };
  // 按 Tab 在最后一格按下时自动加新行
  div.querySelector('.bulk-role').addEventListener('keydown', e => {
    if (e.key === 'Tab' && !e.shiftKey){
      const all = [...document.querySelectorAll('#bulk-rows .bulk-row-item')];
      if (all[all.length-1] === div){
        // 是最后一行 → 加新行
        e.preventDefault();
        addBulkRow();
      }
    }
  });
  // 按下 Enter 也加一行
  div.querySelectorAll('input').forEach(inp => {
    inp.addEventListener('keydown', e => {
      if (e.key === 'Enter'){
        e.preventDefault();
        const all = [...document.querySelectorAll('#bulk-rows .bulk-row-item')];
        if (all[all.length-1] === div) addBulkRow();
        else {
          const next = all[all.indexOf(div)+1];
          next.querySelector('.bulk-username').focus();
        }
      }
    });
  });
  return div;
}

function addBulkRow(){
  const row = makeBulkRow();
  document.getElementById('bulk-rows').appendChild(row);
  renumberRows();
  row.querySelector('.bulk-username').focus();
  return row;
}

function renumberRows(){
  document.querySelectorAll('#bulk-rows .bulk-row-item').forEach((el, idx) => {
    el.querySelector('.row-num').textContent = String(idx + 1);
  });
}

function resetBulkModal(){
  document.getElementById('bulk-rows').innerHTML = '';
  // 默认开 3 行 — 大多数批量场景至少几个用户
  for (let i = 0; i < 3; i++) addBulkRow();
  document.getElementById('bulk-err').classList.remove('show');
  document.getElementById('bulk-results').style.display = 'none';
  document.getElementById('bulk-results').innerHTML = '';
  const sb = document.getElementById('bulk-submit');
  sb.disabled = false;
  sb.textContent = '开 始 创 建';
  sb.onclick = submitBulk;  // 上一次完成后被改成 closeBulkAndReset,重新打开要恢复
  document.getElementById('bulk-cancel').textContent = '取 消';
}

function openBulk(){
  resetBulkModal();
  document.getElementById('bulk-modal').classList.add('open');
  setTimeout(() => {
    const first = document.querySelector('#bulk-rows .bulk-username');
    if (first) first.focus();
  }, 50);
}

document.getElementById('open-bulk-btn').onclick = openBulk;
document.getElementById('add-row-btn').onclick = addBulkRow;
document.getElementById('bulk-cancel').onclick = () => {
  document.getElementById('bulk-modal').classList.remove('open');
  loadUsers();
};

function collectBulkRows(){
  // 把表单里的行收集成提交 payload + 本地校验
  const items = [];
  const localErrors = [];
  const rows = [...document.querySelectorAll('#bulk-rows .bulk-row-item')];
  rows.forEach((row, idx) => {
    const unInput = row.querySelector('.bulk-username');
    const pwInput = row.querySelector('.bulk-password');
    const dnInput = row.querySelector('.bulk-display');
    const roleSel = row.querySelector('.bulk-role');
    const un = unInput.value.trim();
    const pw = pwInput.value;
    const dn = dnInput.value.trim();
    const role = roleSel.value || null;
    // 清掉旧的错误样式
    [unInput, pwInput].forEach(i => i.classList.remove('has-error'));
    // 整行全空 — 跳过(允许用户留空白行)
    if (!un && !pw && !dn && !role) return;
    if (!un){
      unInput.classList.add('has-error');
      localErrors.push({line: idx+1, username: '(空)', error: '用户名为空'});
      return;
    }
    if (pw && pw.length < 6){
      pwInput.classList.add('has-error');
      localErrors.push({line: idx+1, username: un, error: '密码至少 6 位 (留空可自动生成)'});
      return;
    }
    const item = {username: un};
    if (pw) item.password = pw;
    if (dn) item.display_name = dn;
    if (role) item.role = role;
    items.push(item);
  });
  return {items, localErrors};
}

function closeBulkAndReset(){
  // 关掉模态 + 复原按钮状态,让下次打开还是干净的"开始创建"
  document.getElementById('bulk-modal').classList.remove('open');
  const btn = document.getElementById('bulk-submit');
  btn.onclick = submitBulk;
  btn.disabled = false;
  btn.textContent = '开 始 创 建';
  document.getElementById('bulk-cancel').textContent = '取 消';
  loadUsers();
}

async function submitBulk(){
  const err = document.getElementById('bulk-err');
  err.classList.remove('show');
  const {items, localErrors} = collectBulkRows();
  if (!items.length){
    err.textContent = localErrors.length
      ? '请修正红色标出的字段后再提交'
      : '至少填一行用户名';
    err.classList.add('show'); return;
  }
  if (localErrors.length){
    err.textContent = `有 ${localErrors.length} 行格式不合法 — 已标红,请修正后重试`;
    err.classList.add('show'); return;
  }
  const btn = document.getElementById('bulk-submit');
  btn.disabled = true; btn.textContent = '创建中…';
  try {
    const r = await fetch('/api/auth/users/bulk', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        users: items,
        default_role: document.getElementById('bulk-default-role').value,
        default_password_length: parseInt(document.getElementById('bulk-pwd-length').value, 10) || 10,
      }),
    });
    if (!r.ok){
      const d = await r.json().catch(()=>({}));
      err.textContent = d.detail || '批量创建失败';
      err.classList.add('show');
      btn.disabled = false; btn.textContent = '开 始 创 建';
      return;
    }
    const d = await r.json();
    renderBulkResults(d.created || [], d.failed || []);
    // 完成态:换文案 + 改 onclick 为"关闭模态"
    btn.textContent = '完 成';
    btn.disabled = false;
    document.getElementById('bulk-cancel').textContent = '关 闭';
    btn.onclick = closeBulkAndReset;
    await loadUsers();
  } catch(e){
    err.textContent = '请求出错:' + e.message;
    err.classList.add('show');
    btn.disabled = false; btn.textContent = '开 始 创 建';
  }
}

document.getElementById('bulk-submit').onclick = submitBulk;

function renderBulkResults(created, failed){
  const wrap = document.getElementById('bulk-results');
  let html = `<div class="bulk-summary">
    <span>合计 ${created.length + failed.length}</span>
    <span class="ok">✓ 成功 ${created.length}</span>
    <span class="fail">✕ 失败 ${failed.length}</span>
  </div>`;
  if (created.length){
    html += '<table><thead><tr><th>用户名</th><th>密码 (一次性显示)</th><th>角色</th></tr></thead><tbody>';
    created.forEach(u => {
      const role = u.role === 'admin' ? '<span class="role-tag admin">ADMIN</span>' : '<span class="role-tag user">USER</span>';
      html += `<tr>
        <td><strong>${escapeHtml(u.username)}</strong></td>
        <td class="pwd-cell">${escapeHtml(u.password)}${u.password_auto_generated ? ' <span style="color:var(--ink-3);font-weight:400">(自动)</span>' : ''}</td>
        <td>${role}</td>
      </tr>`;
    });
    html += '</tbody></table>';
    html += '<div class="bulk-export-row">';
    html += '<button id="bulk-copy-csv">复制 CSV</button>';
    html += '<button id="bulk-download-csv">下载 CSV</button>';
    html += '<span style="margin-left:auto;font-size:11.5px;color:var(--ink-3);align-self:center">' +
            '⚠ 密码只在此次响应中显示,关掉窗口就再也拿不到原文 — 请立刻保存' +
            '</span>';
    html += '</div>';
  }
  if (failed.length){
    html += '<div style="margin-top:18px"><div class="section-title" style="margin-bottom:8px">失败明细</div>';
    html += '<table><thead><tr><th>用户名</th><th>失败原因</th></tr></thead><tbody>';
    failed.forEach(f => {
      html += `<tr>
        <td>${escapeHtml(f.username)}</td>
        <td class="fail-cell">${escapeHtml(f.error)}</td>
      </tr>`;
    });
    html += '</tbody></table></div>';
  }
  wrap.innerHTML = html;
  wrap.style.display = 'block';

  // 复制 / 下载 CSV
  if (created.length){
    const csv = ['username,password,display_name,role']
      .concat(created.map(u =>
        [u.username, u.password, u.display_name || '', u.role]
          .map(s => /[,"\n]/.test(String(s)) ? '"' + String(s).replace(/"/g,'""') + '"' : s)
          .join(',')
      )).join('\n');
    document.getElementById('bulk-copy-csv').onclick = async () => {
      try {
        await navigator.clipboard.writeText(csv);
        toast('✓ 已复制到剪贴板', 'ok');
      } catch(e){ toast('复制失败 — 浏览器拦截了剪贴板', 'bad'); }
    };
    document.getElementById('bulk-download-csv').onclick = () => {
      const blob = new Blob(['﻿' + csv], {type:'text/csv;charset=utf-8'});
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'tianshu-users-' + new Date().toISOString().slice(0,10) + '.csv';
      document.body.appendChild(a); a.click(); a.remove();
      URL.revokeObjectURL(a.href);
    };
  }
}

(async () => {
  await fetchMe();
  await loadUsers();
})();
</script>
</body></html>
"""


@app.get("/admin/users", response_class=HTMLResponse)
async def admin_users_page(request: Request) -> str:
    # middleware 已经验证登录;这里再确认 admin
    user = require_user(request)
    if not user.is_admin():
        raise HTTPException(403, "需要管理员权限")
    return ADMIN_USERS_HTML


REPORTS_HTML = r"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>报告 — 天枢·裁决</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Noto+Serif+SC:wght@300;400;500;600;700&family=Noto+Sans+SC:wght@300;400;500;600&family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
  :root{--bg:#ffffff;--surface:#f0f0f0;--surface-2:#ebebeb;--surface-3:#dcdcdc;
    --line:#c4c4c4;--line-2:#9e9e9e;
    --fg:#0a0a0a;--fg-2:#262626;--fg-3:#4a4a4a;--fg-4:#6e6e6e;
    --ac:#a8401f;--ac-2:#c45a3a;--ac-bg:rgba(168,64,31,.14);--ac-line:rgba(168,64,31,.58);
    --warn:#8a5300;--ok:#4f6b35;--bad:#8a2d12;--info:#3f5560;
    --running:#7a4f00;
    --mono:'Inter','SF Mono',ui-monospace,Menlo,monospace;
    --sans:'Noto Sans SC','PingFang SC',-apple-system,'Microsoft YaHei',sans-serif;
    --serif:'Noto Serif SC','Songti SC','STSong',Georgia,serif;}
  @media (prefers-reduced-motion: reduce){*,*::before,*::after{animation-duration:.01ms!important;transition-duration:.01ms!important}}
  *{box-sizing:border-box}
  html,body{margin:0;background:var(--bg);color:var(--fg);font-family:var(--sans);min-height:100%;
    -webkit-font-smoothing:antialiased}
  body{background:
    radial-gradient(ellipse 90% 50% at 50% -10%, rgba(196,90,58,.07), transparent 65%) fixed,
    radial-gradient(ellipse 80% 40% at 50% 110%, rgba(255,255,255,.02), transparent 60%) fixed,
    var(--bg);}
  ::selection{background:var(--ac-bg);color:var(--ac-2)}
  ::-webkit-scrollbar{width:10px;height:10px}
  ::-webkit-scrollbar-track{background:transparent}
  ::-webkit-scrollbar-thumb{background:var(--surface-3);border-radius:5px;border:2px solid var(--bg)}
  ::-webkit-scrollbar-thumb:hover{background:var(--line-2)}
  .topbar{display:flex;align-items:center;gap:12px;height:56px;padding:0 24px;
    border-bottom:1px solid var(--line);background:rgba(255,255,255,.94);
    position:sticky;top:0;z-index:100;backdrop-filter:saturate(180%) blur(20px);
    -webkit-backdrop-filter:saturate(180%) blur(20px)}
  .topbar .logo{width:28px;height:28px;flex-shrink:0;
    background:linear-gradient(135deg,#262626,#1a1a1a);border-radius:6px;
    display:grid;place-items:center;color:#001f1a;font-weight:700;font-size:11px;
    letter-spacing:-.02em;
    box-shadow:0 1px 2px rgba(0,0,0,.4),inset 0 1px 0 rgba(255,255,255,.18)}
  .topbar .logo .logo-mark{width:18px;height:18px;display:block;
    filter:drop-shadow(0 .5px 0 rgba(255,255,255,.18))}
  .topbar h1{margin:0;font-size:15px;font-weight:600;letter-spacing:-.015em}
  .topbar h1.brand{display:inline-flex;align-items:baseline;gap:0}
  .topbar h1.brand strong{font-weight:600;color:var(--fg);letter-spacing:-.02em}
  .topbar h1.brand .brand-sub{font-weight:400;color:var(--fg-3);margin-left:8px;font-size:13px;letter-spacing:.005em}
  .topbar nav{display:flex;gap:2px;margin-left:24px}
  .topbar nav a{padding:6px 12px;border-radius:6px;color:var(--fg-2);text-decoration:none;
    font-size:13px;transition:background .12s,color .12s}
  .topbar nav a:hover{background:var(--surface-2);color:var(--fg)}
  .topbar nav a.active{background:var(--ac-bg);color:var(--ac-2)}
  main{max-width:1200px;margin:0 auto;padding:40px 24px 80px}
  h2{margin:0 0 8px;font-size:24px;letter-spacing:-.025em;font-weight:600}
  .sub{color:var(--fg-2);font-size:13px;margin-bottom:32px;line-height:1.55}
  /* 批量删除工具栏 */
  .bulk-bar{display:flex;align-items:center;gap:12px;padding:10px 14px;
    background:var(--surface);border:1px solid var(--line);border-radius:6px;
    margin-bottom:12px;font-size:13px;color:var(--fg-2)}
  .bulk-bar .bulk-all{display:inline-flex;align-items:center;gap:8px;cursor:pointer;user-select:none}
  .bulk-bar .bulk-all input{margin:0;cursor:pointer;width:14px;height:14px}
  .bulk-bar .bulk-sep{color:var(--line-2)}
  .bulk-bar .bulk-count{font-family:var(--mono);font-size:12px;color:var(--fg-3)}
  .bulk-bar .bulk-count.has-sel{color:var(--ac);font-weight:600}
  .bulk-bar .spacer{flex:1}
  .bulk-bar .bulk-btn{padding:6px 14px;border-radius:4px;font-family:var(--sans);
    font-size:12.5px;cursor:pointer;border:1px solid;transition:all .15s;font-weight:500}
  .bulk-bar .bulk-del{background:#fff;border-color:#c44a2e;color:#c44a2e}
  .bulk-bar .bulk-del:hover:not(:disabled){background:#c44a2e;color:#fff}
  .bulk-bar .bulk-del:disabled{opacity:.4;cursor:not-allowed;border-color:var(--line)}
  .bulk-bar .bulk-clear{background:#fff;border-color:var(--line-2);color:var(--fg-2)}
  .bulk-bar .bulk-clear:hover{border-color:#8a2d12;color:#8a2d12;background:rgba(138,45,18,.04)}
  /* 表里的复选框列 */
  td.row-pick, th.row-pick{width:36px;text-align:center;padding-left:14px}
  td.row-pick input, th.row-pick input{cursor:pointer;width:14px;height:14px;margin:0}
  tr.row.row-selected{background:rgba(196,90,58,.06)}

  .filter-row{
    display:grid;
    grid-template-columns:1fr auto auto;
    grid-template-rows:auto auto;
    gap:10px;
    margin-bottom:18px;
    align-items:center}
  .filter-row .search{grid-column:1;grid-row:1;
    background:var(--surface);border:1px solid var(--line);
    border-radius:8px;color:var(--fg);font-family:var(--sans);font-size:13px;
    padding:0 12px;height:36px;width:100%}
  .filter-row #export-all{grid-column:2;grid-row:1;text-decoration:none}
  .filter-row #refresh{grid-column:3;grid-row:1}
  .filter-row .search::placeholder{color:var(--fg-3)}
  .filter-row .search:focus{outline:none;border-color:var(--ac-line);
    box-shadow:0 0 0 3px var(--ac-bg)}
  /* Filter chips — 紧凑横向滚动,无 wrap;activated 用下划线而非整体填色 */
  .filter-row .filters{
    grid-column:1 / -1;grid-row:2;
    display:flex;gap:2px;align-items:center;
    background:transparent;border:none;border-bottom:1px solid var(--line);
    border-radius:0;padding:0;
    overflow-x:auto;scrollbar-width:none;max-width:100%}
  .filter-row .filters::-webkit-scrollbar{display:none}
  .filter-row .filters button{
    background:transparent;border:none;color:var(--fg-3);
    padding:9px 10px;font-size:12.5px;border-radius:0;cursor:pointer;
    font-family:var(--sans);font-weight:500;white-space:nowrap;flex-shrink:0;
    display:inline-flex;align-items:center;gap:6px;line-height:1;
    border-bottom:2px solid transparent;margin-bottom:-1px;
    transition:color .12s,border-color .12s}
  .filter-row .filters button:hover{color:var(--fg)}
  .filter-row .filters button.active{color:var(--fg);border-bottom-color:var(--ac);font-weight:600}
  .filter-row .filters button .ic{display:none}
  .filter-row .refresh{grid-column:2;grid-row:1;
    background:transparent;border:1px solid var(--line-2);color:var(--fg-2);
    padding:0 14px;height:36px;border-radius:8px;
    font-family:var(--sans);font-size:12.5px;font-weight:500;cursor:pointer;
    white-space:nowrap;flex-shrink:0;
    display:inline-flex;align-items:center;gap:6px;
    transition:background .12s,color .12s,border-color .12s}
  .filter-row .refresh:hover{border-color:var(--ac-line);color:var(--ac-2);background:var(--surface-2)}
  table{width:100%;border-collapse:collapse;font-size:12.5px;background:var(--surface);
    border:1px solid var(--line);border-radius:10px;overflow:hidden}
  table th{font-size:10.5px;text-transform:uppercase;letter-spacing:.1em;color:var(--fg-3);
    text-align:left;padding:11px 14px;border-bottom:1px solid var(--line);font-weight:600;
    background:var(--surface-2)}
  table td{padding:12px 14px;border-bottom:1px solid var(--line);font-family:var(--mono);
    font-size:12px;color:var(--fg-2)}
  table tr:last-child td{border-bottom:none}
  table tr.row{cursor:pointer;transition:background .15s}
  table tr.row:hover{background:var(--surface-2)}
  /* 项目编号 + 项目名称 单元格样式 */
  .proj-code{background:var(--surface-2);padding:2px 8px;border-radius:4px;
    color:var(--fg);font-family:var(--mono);font-size:11.5px;border:1px solid var(--line);
    display:inline-block;max-width:160px;overflow:hidden;text-overflow:ellipsis;
    white-space:nowrap;vertical-align:middle}
  .proj-name{font-size:12.5px;color:var(--fg);max-width:220px;
    display:inline-block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
    vertical-align:middle}
  .muted{color:var(--fg-4);font-size:11.5px}
  table .name{color:var(--fg)}
  table .id{color:var(--fg-3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:240px;display:block}
  .status-pill{display:inline-flex;align-items:center;gap:4px;font-family:var(--mono);
    font-size:10px;padding:3px 9px;border-radius:999px;font-weight:600;text-transform:uppercase;
    letter-spacing:.05em;line-height:1}
  .status-pill.queued{background:rgba(168,174,184,.10);color:var(--fg-2)}
  .status-pill.running{background:rgba(167,139,250,.14);color:#c4b5fd}
  .status-pill.succeeded{background:rgba(52,211,153,.10);color:var(--ok)}
  .status-pill.failed{background:rgba(248,113,113,.10);color:var(--bad)}
  .status-pill.saved{background:rgba(168,176,189,.08);color:var(--fg-2);font-weight:500}
  .empty{padding:40px;text-align:center;color:var(--fg-3);font-size:13px;
    border:1px dashed var(--line);border-radius:10px;font-family:var(--mono)}
  .reports-pre{background:var(--surface);border:1px solid var(--line);border-radius:8px;
    padding:14px 18px;font-family:var(--mono);font-size:11.5px;line-height:1.6;color:var(--fg);
    max-height:520px;overflow-y:auto;white-space:pre-wrap;word-break:break-word;margin-top:14px}
  .modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.7);display:none;
    align-items:flex-start;justify-content:center;padding:40px 20px;z-index:50}
  .modal-overlay.open{display:flex}
  .modal{background:var(--surface);border:1px solid var(--line);border-radius:12px;
    max-width:1080px;width:100%;max-height:calc(100vh - 80px);display:flex;flex-direction:column;
    overflow:hidden;box-shadow:0 24px 60px rgba(0,0,0,.6)}
  .modal-head{display:flex;align-items:center;gap:10px;padding:14px 20px;
    border-bottom:1px solid var(--line);background:var(--surface-2)}
  .modal-head .close{background:transparent;border:none;color:var(--fg-3);
    font-size:18px;cursor:pointer;width:28px;height:28px;border-radius:5px}
  .modal-head .close:hover{background:var(--surface);color:var(--fg)}
  .modal-head .back{
    background:transparent;border:1px solid var(--line-2);color:var(--fg-2);
    padding:5px 12px;border-radius:6px;cursor:pointer;
    font-family:var(--sans);font-size:12.5px;font-weight:500;
    display:inline-flex;align-items:center;gap:6px;
    transition:background .12s,color .12s,border-color .12s}
  .modal-head .back:hover{border-color:var(--ac-line);color:var(--ac-2);background:var(--ac-bg)}
  .modal-divider{width:1px;height:18px;background:var(--line-2);margin:0 4px}
  .modal-head .download{background:transparent;border:1px solid var(--line-2);color:var(--fg-2);
    padding:5px 12px;border-radius:5px;font-family:var(--mono);font-size:11.5px;cursor:pointer}
  .modal-head .download:hover{border-color:var(--ac);color:var(--ac)}
  .modal-tabs{display:flex;background:var(--surface-2);border-bottom:1px solid var(--line);padding:0 18px}
  .modal-tabs button{background:transparent;border:none;color:var(--fg-3);
    padding:9px 14px;cursor:pointer;font-size:12px;font-family:var(--sans);
    border-bottom:2px solid transparent;transition:all .15s}
  .modal-tabs button:hover{color:var(--fg-2)}
  .modal-tabs button.active{color:var(--ac);border-bottom-color:var(--ac)}
  .modal-body{flex:1;overflow-y:auto;background:var(--bg)}
  .modal-body .pane-json{padding:14px 20px}
  .modal-body .pane-json pre{margin:0;font-family:var(--mono);font-size:11.5px;line-height:1.65;
    color:var(--fg);white-space:pre-wrap;word-break:break-word}
  /* === 失败状态卡片 — 不要把 failed 包成"5 段都是 0"的报告 === */
  .fail-card{padding:24px 28px;font-family:var(--sans),inherit;color:var(--fg)}
  .fail-banner{display:flex;align-items:center;gap:12px;margin-bottom:18px}
  .fail-banner .fail-tag{background:#b9482e;color:#fff;font-family:var(--mono);font-size:11px;
    letter-spacing:.12em;padding:4px 10px;border-radius:3px;font-weight:600}
  .fail-banner .fail-title{font-family:var(--serif,inherit);font-size:18px;font-weight:500;color:var(--fg)}
  .fail-meta{display:flex;gap:18px;font-family:var(--mono);font-size:12px;color:var(--fg-3);
    border-bottom:1px solid var(--line);padding-bottom:14px;margin-bottom:18px;flex-wrap:wrap}
  .fail-meta code{background:var(--surface-2);padding:1px 6px;border-radius:3px;color:var(--fg-2)}
  .fail-section{margin-bottom:14px}
  .fail-section .fail-label{font-size:11.5px;color:var(--fg-3);letter-spacing:.16em;
    text-transform:uppercase;margin-bottom:6px}
  .fail-section .fail-err{margin:0;padding:12px 14px;background:rgba(185,72,46,.06);
    border-left:3px solid #b9482e;color:#7a2d1c;font-family:var(--mono);font-size:12.5px;
    line-height:1.65;white-space:pre-wrap;word-break:break-word;border-radius:0 4px 4px 0}
  .fail-trace{margin:14px 0;border:1px solid var(--line);border-radius:4px;
    background:var(--surface)}
  .fail-trace summary{cursor:pointer;padding:10px 14px;font-size:12px;color:var(--fg-2);
    font-family:var(--mono);user-select:none}
  .fail-trace summary:hover{color:var(--fg)}
  .fail-trace pre{margin:0;padding:14px;border-top:1px solid var(--line);font-family:var(--mono);
    font-size:11.5px;color:var(--fg-2);line-height:1.6;white-space:pre-wrap;word-break:break-word}
  .fail-logs{margin-top:14px}
  .fail-logs .fail-logs-head{font-size:11.5px;color:var(--fg-3);letter-spacing:.16em;
    text-transform:uppercase;margin-bottom:6px}
  .fail-logs pre{margin:0;padding:12px 14px;background:var(--surface);border:1px solid var(--line);
    border-radius:4px;font-family:var(--mono);font-size:11.5px;color:var(--fg-2);
    line-height:1.6;white-space:pre-wrap;word-break:break-word}
  .fail-hint{margin-top:18px;padding:12px 14px;background:rgba(161,98,7,.08);border-left:3px solid var(--warn);
    color:var(--fg-2);font-size:12.5px;line-height:1.7;border-radius:0 4px 4px 0}

  /* Embedded report renderer styles (mirror tool detail page) */
  .report-hero{display:flex;align-items:flex-start;gap:14px;padding:16px 20px;
    border-bottom:1px solid var(--line);background:var(--surface)}
  .report-hero .report-icon{font-size:22px;width:40px;height:40px;border-radius:8px;
    background:var(--surface-2);border:1px solid var(--line-2);display:grid;place-items:center}
  .report-hero h4{margin:0;font-size:15px;font-weight:600;color:var(--fg)}
  .report-hero .meta{font-family:var(--mono);font-size:11px;color:var(--fg-3);margin-top:4px}
  .report-hero .meta code{background:var(--bg);padding:1px 6px;border-radius:3px;color:var(--ac-2);border:1px solid var(--line)}
  .stat-pills{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
  .stat-pills .pill{font-family:var(--mono);font-size:10.5px;padding:2px 8px;border-radius:3px;
    background:var(--surface-2);border:1px solid var(--line);color:var(--fg-2)}
  .stat-pills .pill .v{color:var(--ac);font-weight:600}
  .gate-banner{padding:14px 18px;border-bottom:1px solid var(--line);display:flex;
    align-items:flex-start;gap:12px;font-size:13px;line-height:1.55}
  .gate-banner.proceed{background:rgba(74,222,128,.05)}
  .gate-banner.reject{background:rgba(248,113,113,.05)}
  .gate-banner.warn{background:rgba(251,191,36,.05)}
  .gate-banner .badge{padding:3px 9px;border-radius:4px;font-family:var(--mono);font-size:11px;
    font-weight:600;text-transform:uppercase;letter-spacing:.05em;flex-shrink:0;margin-top:1px}
  .gate-banner.proceed .badge{background:rgba(74,222,128,.12);color:var(--ok)}
  .gate-banner.reject .badge{background:rgba(248,113,113,.12);color:var(--bad)}
  .gate-banner.warn .badge{background:rgba(251,191,36,.12);color:var(--warn)}
  .gate-banner .reasons{color:var(--fg-2);font-family:var(--mono);font-size:12px}
  .gate-banner .reasons div{margin-top:3px}
  .report-sub{border-bottom:1px solid var(--line)}
  .report-sub:last-child{border-bottom:none}
  .report-sub-head{display:flex;align-items:center;gap:10px;padding:10px 18px;
    cursor:pointer;font-size:12.5px;background:var(--surface);transition:background .15s}
  .report-sub-head:hover{background:var(--surface-2)}
  .report-sub.open .report-sub-head{background:var(--surface-2)}
  .report-sub-twirl{color:var(--fg-3);font-family:var(--mono);font-size:10px;width:10px;transition:transform .15s}
  .report-sub.open .report-sub-twirl{transform:rotate(90deg);color:var(--ac)}
  .report-sub-num{font-family:var(--mono);font-size:10.5px;font-weight:600;color:var(--ac);
    background:rgba(16,185,129,.10);border:1px solid rgba(16,185,129,.25);
    width:20px;height:20px;border-radius:50%;display:inline-grid;place-items:center;flex-shrink:0}
  .report-sub-name{color:var(--fg);font-weight:500}
  .report-sub-stats{margin-left:auto;font-family:var(--mono);font-size:11px;color:var(--fg-3);display:flex;gap:8px}
  .report-sub-stats .chip{padding:2px 7px;border-radius:3px;background:var(--surface-2);border:1px solid var(--line-2)}
  .report-sub-body{display:none;padding:14px 20px;background:var(--bg)}
  .report-sub.open .report-sub-body{display:block}
  .report-kv{display:grid;grid-template-columns:max-content 1fr;gap:6px 16px;margin:0;font-size:12.5px}
  .report-kv dt{color:var(--fg-3);font-family:var(--mono);font-size:11.5px;align-self:start}
  .report-kv dd{margin:0;color:var(--fg);min-width:0;line-height:1.55}
  .report-table{width:100%;border-collapse:collapse;font-size:11.5px;margin:8px 0;
    border:1px solid var(--line);border-radius:6px;overflow:hidden}
  .report-table th{font-family:var(--mono);font-size:10px;text-align:left;padding:8px 10px;
    background:var(--surface-2);color:var(--fg-3);text-transform:uppercase;letter-spacing:.07em;
    font-weight:600;border-bottom:1px solid var(--line);white-space:nowrap}
  .report-table td{padding:8px 10px;border-bottom:1px solid var(--line);font-family:var(--mono);
    font-size:11px;color:var(--fg);vertical-align:top;word-break:break-word;max-width:320px}
  .report-table tr:last-child td{border-bottom:none}
  .sev{font-family:var(--mono);font-size:9.5px;font-weight:700;padding:1px 6px;border-radius:3px;
    text-transform:uppercase;letter-spacing:.05em;display:inline-block}
  .sev-critical{background:rgba(248,113,113,.18);color:var(--bad);border:1px solid rgba(248,113,113,.4)}
  .sev-high,.sev-major{background:rgba(248,113,113,.10);color:var(--bad)}
  .sev-medium,.sev-warn{background:rgba(251,191,36,.10);color:var(--warn)}
  .sev-low,.sev-minor,.sev-cosmetic{background:rgba(168,174,184,.10);color:var(--fg-2)}
  .sev-info{background:rgba(16,185,129,.08);color:var(--ac)}
  .confbar{display:inline-flex;align-items:center;gap:6px;font-family:var(--mono);font-size:11px}
  .confbar .track{width:80px;height:5px;border-radius:3px;background:var(--line);overflow:hidden}
  .confbar .fill{height:100%;background:var(--ac)}
  .confbar .pct{color:var(--ac);font-weight:600}
  .empty-array{color:var(--fg-4);font-family:var(--mono);font-size:11px;font-style:italic}
  details.report-detail summary{cursor:pointer;font-family:var(--mono);font-size:11px;color:var(--ac)}
  details.report-detail[open] > div{margin:6px 0 6px 16px;padding:8px 10px;background:var(--surface-2);
    border-radius:5px;border-left:2px solid var(--line-2)}
  ul.report-list{margin:0;padding-left:18px;font-size:12px;line-height:1.7}
  /* === 执行摘要 4 块结构 === */
  .exec-block{background:var(--surface);border:1px solid var(--line);border-radius:10px;
    padding:18px 22px;margin:14px 0;page-break-inside:avoid}
  .exec-head{display:flex;align-items:center;gap:10px;margin-bottom:14px}
  .exec-head h3{margin:0;font-size:15px;font-weight:600;color:var(--fg)}
  .exec-num{display:inline-grid;place-items:center;width:28px;height:28px;border-radius:8px;
    background:rgba(16,185,129,.10);color:var(--ac);font-size:14px;font-weight:700;
    border:1px solid rgba(16,185,129,.32);font-family:var(--mono)}
  .verdict{padding:14px 18px;border-radius:8px;display:flex;align-items:center;gap:14px;
    font-size:16px;font-weight:600}
  .verdict.ok{background:rgba(74,222,128,.10);color:var(--ok);border:1px solid rgba(74,222,128,.4)}
  .verdict.warn{background:rgba(251,191,36,.10);color:var(--warn);border:1px solid rgba(251,191,36,.4)}
  .verdict.bad{background:rgba(248,113,113,.10);color:var(--bad);border:1px solid rgba(248,113,113,.4)}
  .verdict.skip{background:var(--surface-2);color:var(--fg-2);border:1px solid var(--line-2)}
  .verdict-icon{font-size:22px}
  .verdict-summary{margin-top:12px;padding:10px 14px;background:var(--surface-2);
    border-left:3px solid var(--line-2);border-radius:0 6px 6px 0;
    font-size:13.5px;line-height:1.7;color:var(--fg)}
  .sev-strip{margin-top:10px;padding:8px 14px;background:var(--surface-2);border-radius:6px;
    font-family:var(--mono);font-size:12px;color:var(--fg-2)}
  .sev-strip strong{color:var(--fg);font-weight:600}
  .exec-muted{color:var(--fg-3);font-size:13px;margin:4px 0;font-style:normal}

  /* === KPI 卡片(测试结论里的 4 个数字)=== */
  .exec-kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:14px}
  .exec-kpi{display:flex;flex-direction:column;align-items:flex-start;justify-content:center;
    padding:14px 16px;background:var(--bg);border:1px solid var(--line);border-radius:8px}
  .exec-kpi-num{font-size:28px;font-weight:700;color:var(--fg);font-family:var(--sans);
    font-variant-numeric:tabular-nums;line-height:1;letter-spacing:-.02em}
  .exec-kpi-lbl{font-size:11px;color:var(--fg-3);margin-top:6px;letter-spacing:.08em;
    text-transform:uppercase;font-weight:600}

  /* === 严重度分布条 === */
  .sev-bar{display:flex;height:8px;border-radius:4px;overflow:hidden;margin-top:14px;
    background:var(--surface-2);border:1px solid var(--line)}
  .sev-bar-seg{display:flex;align-items:center;justify-content:center;color:#fff;
    font-size:10px;font-weight:600;line-height:8px;min-width:8px;transition:flex .2s}
  .sev-bar-critical{background:#dc2626}
  .sev-bar-high{background:#ea580c}
  .sev-bar-medium{background:#ca8a04}
  .sev-bar-low{background:#16a34a}
  .sev-bar-info{background:#0891b2}

  /* === 优先级分布条(用例段)=== */
  .pri-bar{display:flex;height:24px;border-radius:6px;overflow:hidden;margin-top:8px;margin-bottom:12px;
    background:var(--surface-2);border:1px solid var(--line)}
  .pri-bar-seg{display:flex;align-items:center;justify-content:center;color:#fff;
    font-size:11px;font-weight:600;min-width:36px;letter-spacing:.04em;padding:0 8px}
  .pri-bar-P0{background:#dc2626}
  .pri-bar-P1{background:#ea580c}
  .pri-bar-P2{background:#ca8a04}
  .pri-bar-P3{background:#737373}

  /* === 风险卡片 === */
  .exec-risk-list{display:flex;flex-direction:column;gap:10px;margin-top:4px}
  .exec-risk-item{padding:12px 14px 12px 14px;background:var(--bg);border:1px solid var(--line);
    border-left:3px solid #ca8a04;border-radius:6px}
  .exec-risk-head{display:flex;align-items:baseline;gap:10px;margin-bottom:6px;flex-wrap:wrap}
  .exec-risk-title{font-weight:600;color:var(--fg);font-size:14px;line-height:1.5;flex:1;min-width:0}
  .exec-risk-line{font-size:12.5px;color:var(--fg-2);line-height:1.65;margin-top:3px;
    display:flex;align-items:baseline;gap:8px}
  .exec-risk-line .lbl{display:inline-block;min-width:42px;font-size:11px;color:var(--fg-3);
    font-weight:600;letter-spacing:.04em;text-transform:uppercase;flex-shrink:0}

  /* === 阻碍卡片 === */
  .exec-blocker-list{display:flex;flex-direction:column;gap:10px;margin-top:4px}
  .exec-blocker-item{padding:12px 14px;background:rgba(220,38,38,.04);border:1px solid rgba(220,38,38,.20);
    border-left:3px solid #dc2626;border-radius:6px}
  .exec-blocker-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:6px}
  .blocker-tag{font-size:10.5px;font-weight:700;padding:2px 8px;background:rgba(220,38,38,.12);
    color:#dc2626;letter-spacing:.1em;text-transform:uppercase;border-radius:4px}
  .exec-blocker-title{font-weight:600;color:var(--fg);font-size:14px;flex:1;min-width:0}
  .exec-blocker-line{font-size:12.5px;color:var(--fg-2);line-height:1.65;margin-top:3px;
    display:flex;align-items:baseline;gap:8px}
  .exec-blocker-line .lbl{display:inline-block;min-width:62px;font-size:11px;color:var(--fg-3);
    font-weight:600;letter-spacing:.04em;text-transform:uppercase;flex-shrink:0}
  .exec-blocker-line.fix .lbl{color:var(--ok)}
  .exec-blocker-line.fix{color:var(--fg)}

  /* === 优先级 pill(用例表 + Bug 卡)=== */
  .pri-tag,.exec-issue-meta .meta-chip.pri-P0,.exec-issue-meta .meta-chip.pri-P1,
  .exec-issue-meta .meta-chip.pri-P2,.exec-issue-meta .meta-chip.pri-P3,
  .exec-blocker-head .meta-chip.pri-P0,.exec-blocker-head .meta-chip.pri-P1,
  .exec-blocker-head .meta-chip.pri-P2,.exec-blocker-head .meta-chip.pri-P3{
    font-size:10.5px;font-weight:700;padding:2px 8px;border-radius:4px;letter-spacing:.04em;
    background:var(--surface-2);color:var(--fg-2);font-family:var(--sans)}
  .pri-tag.pri-P0,.exec-issue-meta .meta-chip.pri-P0{background:rgba(220,38,38,.12);color:#dc2626}
  .pri-tag.pri-P1,.exec-issue-meta .meta-chip.pri-P1{background:rgba(234,88,12,.12);color:#ea580c}
  .pri-tag.pri-P2,.exec-issue-meta .meta-chip.pri-P2{background:rgba(202,138,4,.12);color:#a16207}
  .pri-tag.pri-P3{background:var(--surface-2);color:var(--fg-3)}
  .exec-issue{border:1px solid var(--line);border-radius:8px;padding:12px 16px;margin-bottom:10px;
    background:var(--bg);border-left:3px solid var(--line-2)}
  .exec-issue.sev-critical{border-left-color:var(--bad)}
  .exec-issue.sev-high{border-left-color:var(--warn)}
  .exec-issue.sev-medium{border-left-color:#eab308}
  .exec-issue.sev-low{border-left-color:var(--ok)}
  .exec-issue.sev-info{border-left-color:var(--ac)}
  .exec-issue-head{display:flex;align-items:center;gap:10px;margin-bottom:6px}
  .sev-tag{font-size:11px;font-weight:600;padding:2px 8px;border-radius:999px;
    background:rgba(16,185,129,.10);color:var(--ac);border:1px solid rgba(16,185,129,.32)}
  .exec-issue-title{font-weight:600;color:var(--fg);font-size:13.5px}
  .exec-issue-loc{color:var(--fg-3);font-size:12px;margin:6px 0;font-family:var(--mono)}
  .exec-issue-desc{margin:6px 0;color:var(--fg);line-height:1.6;font-size:13px}
  .exec-issue-fix{margin-top:8px;padding:8px 12px;background:rgba(16,185,129,.06);
    border-radius:5px;font-size:12.5px;color:var(--fg-2);
    border:1px solid rgba(16,185,129,.18)}
  .exec-issue-meta{display:flex;flex-wrap:wrap;gap:6px;margin:4px 0 8px}
  .exec-issue-meta .meta-chip{font-size:11px;font-family:var(--mono);
    padding:2px 9px;border-radius:999px;background:var(--surface-2);
    border:1px solid var(--line-2);color:var(--fg-2)}
  .exec-issue-meta .meta-chip.role{color:var(--ac);border-color:var(--ac-line);background:rgba(16,185,129,.08)}
  .exec-issue-section{margin:10px 0;padding:10px 14px;border-radius:6px;background:var(--surface-2)}
  .exec-issue-section.fix{background:rgba(16,185,129,.06);border:1px solid rgba(16,185,129,.20)}
  .exec-issue-section.verify{background:rgba(96,165,250,.05);border:1px solid rgba(96,165,250,.20)}
  .sec-lbl{font-size:12px;font-weight:600;color:var(--fg-2);margin-bottom:6px;letter-spacing:.02em}
  .sec-body{font-size:13px;color:var(--fg);line-height:1.65}
  .repro-list{margin:0;padding-left:22px;font-size:12.5px;line-height:1.75;color:var(--fg-2)}
  .accept-line{margin-top:8px;padding:8px 12px;background:rgba(96,165,250,.08);
    border-left:3px solid #60a5fa;border-radius:0 5px 5px 0;font-size:12.5px;color:var(--fg-2)}
  .related-cases{margin-top:10px;font-size:12px;color:var(--fg-3);font-family:var(--mono)}
  .related-cases code{background:var(--surface-2);padding:2px 7px;border-radius:3px;
    color:var(--ac);font-size:11.5px;margin-right:4px}
  .exec-issue-impact,.exec-issue-evidence{margin-top:8px;font-size:12px;color:var(--fg-3)}
  .exec-case-total{display:flex;align-items:baseline;gap:10px}
  .exec-case-total .num{font-size:32px;font-weight:700;color:var(--fg);font-family:var(--mono);
    font-variant-numeric:tabular-nums}
  .exec-case-total .lbl{font-size:13px;color:var(--fg-2)}
  .exec-pri{display:flex;flex-wrap:wrap;gap:10px;margin-top:12px}
  .exec-pri-cell{display:flex;flex-direction:column;align-items:center;
    background:var(--surface-2);border:1px solid var(--line);border-radius:8px;
    padding:10px 14px;min-width:80px}
  .exec-pri-cell .num{font-size:18px;font-weight:700;color:var(--fg);font-family:var(--mono)}
  .exec-pri-cell .lbl{font-size:10.5px;color:var(--fg-3);text-transform:uppercase;letter-spacing:.04em;margin-top:2px}
  .exec-case-note{margin-top:12px;padding:10px 14px;background:rgba(251,191,36,.06);
    border-left:3px solid var(--warn);border-radius:0 6px 6px 0;
    color:var(--fg-2);font-size:12.5px}
  /* === Screenshot evidence (step5 / h5_adapt) === */
  .report-screenshots{margin:14px 0 0;padding:14px;background:var(--surface-2);
    border:1px solid var(--line);border-radius:10px}
  .screenshots-head{font-size:13px;font-weight:600;color:var(--fg);margin-bottom:10px}
  .screenshots-hint{font-size:10.5px;font-weight:400;color:var(--fg-3);margin-left:6px;font-style:italic}
  .shot-group{margin-top:10px;padding-top:10px;border-top:1px dashed var(--line)}
  .shot-group:first-of-type{margin-top:0;padding-top:0;border-top:none}
  .shot-url{font-family:"SF Mono",ui-monospace,monospace;font-size:11.5px;color:var(--fg-3);margin-bottom:8px}
  .shot-url code{background:var(--bg);padding:2px 8px;border-radius:4px;color:var(--ac-2)}
  .shot-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px}
  .shot-cell{display:block;background:var(--bg);border:1px solid var(--line);border-radius:8px;
    overflow:hidden;text-decoration:none;color:var(--fg-2);
    transition:transform .15s,border-color .15s,box-shadow .15s}
  .shot-cell:hover{transform:translateY(-2px);border-color:var(--ac-line);
    box-shadow:0 8px 22px rgba(0,0,0,.35)}
  .shot-cell img{display:block;width:100%;height:auto;max-height:240px;object-fit:cover;
    background:#000}
  .shot-cap{padding:6px 10px;font-size:11px;font-family:"SF Mono",ui-monospace,monospace;
    color:var(--fg-3);border-top:1px solid var(--line);
    display:flex;align-items:center;justify-content:space-between;gap:6px}
  .shot-cap .issue-badge{background:rgba(220,38,38,.14);color:#dc2626;
    padding:1px 7px;border-radius:999px;font-weight:600;font-size:10px}
  /* === Row action buttons (download / delete) === */
  td.actions{white-space:nowrap;display:flex;gap:6px;align-items:center}
  td.actions a{
    display:inline-flex;align-items:center;justify-content:center;
    padding:4px 10px;border-radius:6px;
    font-family:"SF Mono",ui-monospace,monospace;font-size:11px;
    text-decoration:none;cursor:pointer;
    transition:background .12s,color .12s,border-color .12s;
    border:1px solid var(--line-2);color:var(--fg-2);background:transparent}
  td.actions a:hover{border-color:var(--ac-line);color:var(--ac-2);background:var(--ac-bg)}
  td.actions a.del{color:var(--fg-3)}
  td.actions a.del:hover{border-color:rgba(248,113,113,.4);color:var(--bad);background:rgba(248,113,113,.08)}
  /* === Hero & KPI tiles === */
  .reports-hero{margin-bottom:32px}
  .hero-eyebrow{display:inline-flex;align-items:center;gap:6px;
    font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;
    color:var(--ac-2);margin-bottom:12px;padding:5px 12px;border-radius:999px;
    background:var(--ac-bg);border:1px solid var(--ac-line)}
  .hero-eyebrow::before{content:"";width:6px;height:6px;border-radius:50%;
    background:var(--ac-2);box-shadow:0 0 0 2px var(--ac-bg)}
  .reports-hero h2{font-size:32px;letter-spacing:-.03em;margin:0 0 8px;line-height:1.1;
    background:linear-gradient(135deg,var(--fg) 0%,var(--fg-2) 75%);
    -webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
  .reports-hero .sub{color:var(--fg-2);font-size:14.5px;line-height:1.55;margin:0 0 24px;max-width:680px}
  .kpi-strip{display:grid;grid-template-columns:repeat(4,1fr);gap:24px;margin-bottom:14px;
    padding:14px 0;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
  .kpi{background:transparent;border:none;border-radius:0;
    padding:0;position:relative;overflow:visible;
    transition:none}
  .kpi:hover{transform:none;border-color:transparent}
  .kpi::before{display:none}
  .kpi .num{font-size:20px;font-weight:600;letter-spacing:-.02em;color:var(--fg);
    font-variant-numeric:tabular-nums;display:flex;align-items:baseline;gap:6px}
  .kpi .num.ok{color:var(--ok)}
  .kpi .num.bad{color:var(--bad)}
  .kpi .num.warn{color:var(--warn)}
  .kpi .lbl{font-size:11px;color:var(--fg-3);text-transform:uppercase;letter-spacing:.06em;
    margin-top:4px;font-weight:500}
  .kpi .icon-bg{display:none;position:absolute;right:-8px;bottom:-8px;font-size:60px;opacity:.04;line-height:1;
    pointer-events:none}
</style></head>
<body>
<div class="topbar">
  <a class="brand-link" href="/tools" title="天枢 · 裁决 · 返回主页" style="text-decoration:none;display:inline-flex;align-items:center;gap:10px;font-family:'Noto Serif SC',Georgia,serif;font-size:19px;font-weight:600;letter-spacing:.18em;color:var(--fg);margin-right:24px;padding:4px 0"><svg viewBox="0 0 24 24" fill="currentColor" width="24" height="24" aria-hidden="true" style="color:var(--ac);opacity:1;flex-shrink:0;filter:drop-shadow(0 1px 2px rgba(196,90,58,.28))"><path d="M12 1.5L13.4 10.6L22.5 12L13.4 13.4L12 22.5L10.6 13.4L1.5 12L10.6 10.6Z"/></svg><span>天枢</span><span style="color:var(--ac);margin:0 6px;font-weight:400">·</span><span>裁决</span></a>
  
  <nav>
    <a href="/tools">工具</a>
    <a href="/reports" class="active">报告</a>
    <a href="/settings">设置</a>
    <a href="/admin/users" class="admin-only" style="display:none">用户管理</a>
  </nav>
  <div class="right" style="margin-left:auto">
    <span class="kbd-hint" id="cmd-trigger" title="打开命令面板"
      style="display:inline-flex;align-items:center;gap:4px;font-size:11px;color:var(--fg-3);
        padding:4px 8px;border-radius:6px;cursor:pointer;transition:all .12s">
      <kbd style="background:var(--surface-3);border:1px solid var(--line-1);border-bottom-width:2px;
        padding:1px 6px;border-radius:4px;font-size:10.5px;color:var(--fg-2);
        font-family:'SF Mono',ui-monospace,monospace;min-width:18px;text-align:center">⌘</kbd>
      <kbd style="background:var(--surface-3);border:1px solid var(--line-1);border-bottom-width:2px;
        padding:1px 6px;border-radius:4px;font-size:10.5px;color:var(--fg-2);
        font-family:'SF Mono',ui-monospace,monospace;min-width:18px;text-align:center">K</kbd>
      跳转
    </span>
  </div>
</div>
<main>
  <section class="reports-hero">
    <span class="hero-eyebrow">运行档案</span>
    <h2>报告</h2>
    <p class="sub">所有跑过的任务、生成的用例与漏测分析。本地保存，可导出为 HTML / Markdown / JSON。</p>
    <div class="kpi-strip">
      <div class="kpi"><div class="num" id="kpi-total">—</div><div class="lbl">累计运行</div><div class="icon-bg">📊</div></div>
      <div class="kpi"><div class="num ok" id="kpi-success">—</div><div class="lbl">成功</div><div class="icon-bg">✓</div></div>
      <div class="kpi"><div class="num bad" id="kpi-fail">—</div><div class="lbl">失败</div><div class="icon-bg">✗</div></div>
      <div class="kpi"><div class="num" id="kpi-recent">—</div><div class="lbl">近 24h</div><div class="icon-bg">⏱</div></div>
    </div>
  </section>
  <div class="filter-row">
    <input class="search" id="search" placeholder="搜索 run id / 工具 / 项目编号 / 项目名称">
    <div class="filters" id="filters"></div>
    <a class="refresh" id="export-all" href="/api/reports/export" download>↓ 全部下载</a>
    <button class="refresh" id="refresh">↻ 刷新</button>
  </div>
  <!-- 多选批量操作工具栏 — 只在表格有内容时显示 -->
  <div class="bulk-bar" id="bulk-bar" style="display:none">
    <label class="bulk-all"><input type="checkbox" id="bulk-all-cb"><span id="bulk-all-label">全选当前页</span></label>
    <span class="bulk-sep">·</span>
    <span class="bulk-count" id="bulk-count">已选 0 条</span>
    <span class="spacer"></span>
    <button class="bulk-btn bulk-del" id="bulk-del-sel" disabled>✕ 删除选中</button>
    <button class="bulk-btn bulk-clear" id="bulk-clear-all">⚠ 清空所有</button>
  </div>
  <div id="content"></div>
</main>

<div class="modal-overlay" id="modal-overlay">
  <div class="modal">
    <div class="modal-head">
      <button class="back" onclick="closeModal()" title="返回报告列表（Esc）">
        <span style="font-size:14px;line-height:1">←</span>
        <span>返回</span>
      </button>
      <span class="modal-divider"></span>
      <span id="modal-icon" style="font-size:18px">·</span>
      <span id="modal-tool" style="font-size:14px;font-weight:500">·</span>
      <span style="font-family:var(--mono);font-size:11px;color:var(--fg-3)" id="modal-id">·</span>
      <span style="margin-left:auto"></span>
      <button class="download" data-act="html" id="modal-dl-html">↓ HTML</button>
      <button class="download" data-act="md" id="modal-dl-md">↓ Markdown</button>
      <button class="download" data-act="xlsx" id="modal-dl-xlsx" style="display:none">↓ 下载 Excel 用例表</button>
      <button class="download" data-act="json" id="modal-dl-json">↓ JSON</button>
      <button class="close" onclick="closeModal()" title="关闭（Esc）">×</button>
    </div>
    <div class="modal-tabs">
      <button class="active" data-tab="report" id="modal-tab-report">报告</button>
      <button data-tab="json">{ } 原始 JSON</button>
    </div>
    <div class="modal-body" id="modal-body"><div style="padding:40px;text-align:center;color:var(--fg-3)">加载中…</div></div>
  </div>
</div>

<script>
let toolCatalog = [];                    // {id, icon, name, prompts, prompt_dir, ...}
const toolMap = {};                      // tool_id → catalog entry
const substepNamesCache = {};            // tool_id → {sub_id: name}
let allReports = [];
let currentFilter = 'all';
// 多选批量删除状态
const selectedRunIds = new Set();
let currentVisibleRunIds = [];
let currentRun = null;                   // currently-open run (for download buttons)
let currentTool = null;                  // tool meta of current run
let activeModalTab = 'report';

async function load(){
  // Load catalog first so renderer can look up tool icon/prompts
  if (!toolCatalog.length){
    const cat = await fetch('/api/tools').then(r=>r.json());
    toolCatalog = cat.tools;
    toolCatalog.forEach(t => toolMap[t.id] = t);
    buildFilters();
  }
  const data = await fetch('/api/reports').then(r=>r.json());
  const seen = new Set();
  allReports = [];
  data.in_memory.forEach(r => { seen.add(r.run_id); allReports.push({...r, kind:'memory'}); });
  data.saved.forEach(r => { if (!seen.has(r.run_id)) allReports.push({...r, kind:'saved'}); });
  // KPI tiles
  const kt = document.getElementById('kpi-total');
  const ks = document.getElementById('kpi-success');
  const kf = document.getElementById('kpi-fail');
  const kr = document.getElementById('kpi-recent');
  if (kt && ks && kf && kr) {
    const total = allReports.length;
    // saved-on-disk reports always count as success (they only persist if completed)
    const okN = allReports.filter(r => r.status === 'succeeded' || r.kind === 'saved').length;
    const failN = allReports.filter(r => r.status === 'failed').length;
    const dayAgo = Date.now()/1000 - 86400;
    const recentN = allReports.filter(r => (r.started_at || r.mtime || 0) > dayAgo).length;
    kt.textContent = total;
    ks.textContent = okN;
    kf.textContent = failN;
    kr.textContent = recentN;
  }
  render();
}

function buildFilters(){
  const f = document.getElementById('filters');
  f.innerHTML = '<button class="active" data-f="all">全部</button>';
  toolCatalog.forEach(t => {
    f.insertAdjacentHTML('beforeend',
      `<button data-f="${t.id}"><span class="ic">${t.icon}</span>${t.name}</button>`);
  });
}

function render(){
  const q = document.getElementById('search').value.toLowerCase().trim();
  const filtered = allReports.filter(r => {
    if (currentFilter !== 'all' && r.tool_id !== currentFilter) return false;
    if (q) {
      const hay = (r.run_id + ' ' + (r.tool_name||'') + ' ' + (r.project_code||'') + ' ' + (r.project_name||'')).toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
  const c = document.getElementById('content');
  const bulkBar = document.getElementById('bulk-bar');
  if (!filtered.length){
    c.innerHTML = '<div class="empty">暂无报告 · 去 <a href="/tools" style="color:var(--ac);text-decoration:none">工具</a> 触发一次运行</div>';
    if (bulkBar) bulkBar.style.display = 'none';
    selectedRunIds.clear();
    return;
  }
  if (bulkBar) bulkBar.style.display = '';
  c.innerHTML = `<table><thead><tr>
    <th class="row-pick"></th>
    <th>状态</th><th>工具</th><th>项目编号</th><th>项目名称</th><th>Run ID</th><th>开始</th><th>用时</th><th>下载</th>
  </tr></thead><tbody id="tbody"></tbody></table>`;
  const tbody = document.getElementById('tbody');
  // 当前页 run_ids 给"全选"用
  currentVisibleRunIds = filtered.map(r => r.run_id);
  filtered.forEach(r => {
    const t = r.started_at || r.mtime;
    const ts = t ? new Date(t*1000).toLocaleString('zh-CN', {hour12:false}) : '—';
    const elapsed = (r.finished_at && r.started_at) ? ((r.finished_at - r.started_at).toFixed(1) + 's') : '—';
    const status = r.kind === 'memory' ? r.status : 'saved';
    const tm = toolMap[r.tool_id];
    const name = tm ? `${tm.icon} ${tm.name}` : r.tool_id;
    const pc = r.project_code ? `<code class="proj-code">${escapeHtml(r.project_code)}</code>` : '<span class="muted">—</span>';
    const pn = r.project_name ? `<span class="proj-name">${escapeHtml(r.project_name)}</span>` : '<span class="muted">—</span>';
    const row = document.createElement('tr');
    row.className = 'row' + (selectedRunIds.has(r.run_id) ? ' row-selected' : '');
    row.dataset.runid = r.run_id;
    row.onclick = (e) => {
      // 复选框点击不要触发打开报告
      if (e.target && (e.target.closest('.row-pick') || e.target.matches('a,button,input'))) return;
      openReport(r);
    };
    const checked = selectedRunIds.has(r.run_id) ? 'checked' : '';
    row.innerHTML = `
      <td class="row-pick"><input type="checkbox" class="row-cb" data-runid="${r.run_id}" ${checked}></td>
      <td><span class="status-pill ${status}">${status}</span></td>
      <td class="name">${name}</td>
      <td>${pc}</td>
      <td>${pn}</td>
      <td><span class="id">${r.run_id}</span></td>
      <td>${ts}</td>
      <td>${elapsed}</td>
      <td class="actions">
        ${r.tool_id === 'step2'
          ? `<a href="javascript:void(0)" data-runid="${r.run_id}" data-toolid="${r.tool_id}" data-fmt="xlsx" class="dl">用例 Excel</a>`
          : `<a href="javascript:void(0)" data-runid="${r.run_id}" data-toolid="${r.tool_id}" data-fmt="html" class="dl">HTML</a>
             <a href="javascript:void(0)" data-runid="${r.run_id}" data-toolid="${r.tool_id}" data-fmt="md" class="dl">MD</a>
             <a href="/api/reports/${r.run_id}" target="_blank" class="action-link" onclick="event.stopPropagation()">JSON</a>`}
        <a href="javascript:void(0)" data-runid="${r.run_id}" class="del">删除</a>
      </td>
    `;
    tbody.appendChild(row);
  });
  // 复选框 change → 维护 selectedRunIds + 行高亮 + bulk bar 状态
  tbody.querySelectorAll('input.row-cb').forEach(cb => {
    cb.onclick = (e) => e.stopPropagation();
    cb.onchange = () => {
      const rid = cb.dataset.runid;
      if (cb.checked) selectedRunIds.add(rid); else selectedRunIds.delete(rid);
      cb.closest('tr').classList.toggle('row-selected', cb.checked);
      updateBulkBar();
    };
  });
  updateBulkBar();
  // Wire quick-download links
  tbody.querySelectorAll('a.dl').forEach(a => {
    a.onclick = async (e) => {
      e.stopPropagation();
      const runId = a.dataset.runid;
      const toolId = a.dataset.toolid;
      await quickDownload(runId, toolId, a.dataset.fmt);
    };
  });
  // Wire delete buttons
  tbody.querySelectorAll('a.del').forEach(a => {
    a.onclick = async (e) => {
      e.stopPropagation();
      const runId = a.dataset.runid;
      if (!confirm('确认删除这条报告？\n\n此操作会同时删除内存与磁盘记录，不可恢复。')) return;
      try {
        const res = await fetch('/api/reports/' + runId, {method: 'DELETE'});
        if (!res.ok) {
          const err = await res.text();
          alert('删除失败：' + err);
          return;
        }
        // Reload list
        await load();
      } catch (e) {
        alert('删除请求出错：' + e.message);
      }
    };
  });
}

async function quickDownload(runId, toolId, fmt){
  // 下载流程:先 preflight 一次 HEAD/GET 看是否 200,再触发真正的下载。
  // 这样 401 (未登录) / 403 (无权限) / 404 (报告丢失) 会显示明确提示,
  // 避免浏览器把错误 JSON 当成 .html 保存导致 "无法从网站上提取文件"。
  if (!['html', 'md', 'json', 'xlsx'].includes(fmt)) { alert('不支持的格式:' + fmt); return; }
  const url = `/api/reports/${runId}/export.${fmt}`;
  try {
    const resp = await fetch(url, {method:'GET', credentials:'same-origin'});
    if (!resp.ok){
      let detail = '';
      try {
        const j = await resp.json();
        detail = j.detail || j.error || '';
      } catch(_) {}
      const msg = ({
        401: '会话已过期 — 请重新登录',
        403: detail || '无权下载此报告',
        404: '报告不存在 — 可能服务重启后内存清掉了,且磁盘也没保存',
      })[resp.status] || ('下载失败: HTTP ' + resp.status + (detail ? ' · ' + detail : ''));
      // 用页面已有的 toast (如果有),否则降级 alert
      if (typeof toast === 'function') toast(msg, 'bad');
      else alert(msg);
      return;
    }
    // 200 — 拿到 blob 触发下载
    const blob = await resp.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `${toolId}_${runId.slice(0,8)}.${fmt}`;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  } catch(err){
    const msg = '下载请求失败: ' + err.message;
    if (typeof toast === 'function') toast(msg, 'bad');
    else alert(msg);
  }
}

async function prefetchScreenshotsAsDataUri(r){
  const shots = ((r.report && r.report.meta && r.report.meta.screenshots) || []).filter(s => !s.error && s.filename);
  if (!shots.length) return {};
  const map = {};
  await Promise.all(shots.map(async s => {
    try {
      const resp = await fetch('/api/screenshots/' + encodeURIComponent(s.filename));
      if (!resp.ok) return;
      const blob = await resp.blob();
      const reader = new FileReader();
      const dataUri = await new Promise((res, rej) => {
        reader.onload = () => res(reader.result);
        reader.onerror = () => rej(reader.error);
        reader.readAsDataURL(blob);
      });
      map[s.filename] = dataUri;
    } catch(_){}
  }));
  return map;
}

async function ensureSubstepNames(tool){
  if (substepNamesCache[tool.id]) { tool._substepNames = substepNamesCache[tool.id]; return; }
  const m = {};
  for (const sid of tool.prompts){
    try {
      const d = await fetch(`/api/prompts/${tool.prompt_dir}/${sid}`).then(r=>r.json());
      m[sid] = d.name;
    } catch(_){ m[sid] = sid; }
  }
  substepNamesCache[tool.id] = m;
  tool._substepNames = m;
}

async function openReport(r){
  const tool = toolMap[r.tool_id];
  if (!tool){ alert('未知工具：' + r.tool_id); return; }
  document.getElementById('modal-icon').textContent = tool.icon;
  document.getElementById('modal-tool').textContent = tool.name;
  document.getElementById('modal-id').textContent = r.run_id;
  document.getElementById('modal-body').innerHTML =
    '<div style="padding:40px;text-align:center;color:var(--fg-3);font-family:var(--mono)">加载中…</div>';
  document.getElementById('modal-overlay').classList.add('open');
  activeModalTab = 'report';
  document.querySelectorAll('.modal-tabs button').forEach(b => b.classList.toggle('active', b.dataset.tab==='report'));

  try {
    const data = await fetch('/api/reports/' + r.run_id).then(r=>r.json());
    const run = (data.tool_id) ? data : {
      tool_id: r.tool_id, run_id: r.run_id,
      report: data.report || data,
      usage: data.usage || {}, started_at: r.started_at || 0, finished_at: r.finished_at || 0,
    };
    await ensureSubstepNames(tool);
    currentRun = run;
    currentTool = tool;
    // step2 测试用例工具:产出就是用例,只给 Excel,藏掉 HTML/MD/JSON 那套"报告"下载。
    const caseCount = ((run.report || {}).cases || []).length;
    const isStep2 = tool.id === 'step2';
    const setBtn = (id, show) => {
      const el = document.getElementById(id);
      if (el) el.style.display = show ? '' : 'none';
    };
    setBtn('modal-dl-xlsx', caseCount > 0);
    setBtn('modal-dl-html', !isStep2);
    setBtn('modal-dl-md',   !isStep2);
    setBtn('modal-dl-json', !isStep2);
    // step2:tab 标签「报告」→「用例」
    const tabReport = document.getElementById('modal-tab-report');
    if (tabReport) tabReport.textContent = isStep2 ? '用例' : '报告';
    renderModalBody();
  } catch(e){
    document.getElementById('modal-body').innerHTML =
      `<div style="padding:40px;text-align:center;color:var(--bad)">加载失败：${e}</div>`;
  }
}

function renderModalBody(){
  if (!currentRun) return;
  const body = document.getElementById('modal-body');
  if (activeModalTab === 'json'){
    body.innerHTML = `<div class="pane-json"><pre>${escapeHtml(JSON.stringify(currentRun.report, null, 2))}</pre></div>`;
    return;
  }
  // 失败任务短路 — 不要把 failed 包装成正常的"5 段都是 0"报告:
  // 这是 Codex AI-FE-002 报告的:用户点 FAILED 行,弹窗显示"未产出/0 问题",
  // 真实失败原因被掩盖。
  if (currentRun.status === 'failed'){
    body.innerHTML = renderFailureCard(currentRun, currentTool);
    return;
  }
  // expandAll=false:子步骤默认折叠,executive summary 5 段直接展示,避免重复
  body.innerHTML = renderHtmlReport(currentRun, currentTool, {expandAll: false});
  body.querySelectorAll('.report-sub-head').forEach(h => {
    h.onclick = () => h.parentElement.classList.toggle('open');
  });
  inlineScreenshotsInBody(body);
}

function renderFailureCard(r, tool){
  const errRaw = r.traceback || r.error || '(未捕获到错误详情)';
  const lastProgress = r.progress || '';
  const logs = Array.isArray(r.logs) ? r.logs.slice(-6) : [];
  const elapsed = (r.finished_at && r.started_at) ? (r.finished_at - r.started_at).toFixed(1) + 's' : '—';
  const errFirstLine = String(errRaw).split('\n')[0].slice(0, 240);
  const logsHtml = logs.length
    ? '<div class="fail-logs"><div class="fail-logs-head">最近日志</div><pre>' +
      logs.map(l => {
        const t = l.ts ? new Date(l.ts * 1000).toLocaleTimeString('zh-CN', {hour12:false}) : '';
        return escapeHtml(`[${t}] ${l.event || ''} ${JSON.stringify(l.fields || {})}`);
      }).join('\n') + '</pre></div>'
    : '';
  return `
    <div class="fail-card">
      <div class="fail-banner">
        <span class="fail-tag">FAILED</span>
        <span class="fail-title">${escapeHtml(tool && tool.name || r.tool_id || '?')} · 未生成有效报告</span>
      </div>
      <div class="fail-meta">
        <span>run <code>${escapeHtml((r.run_id || '').slice(0,12))}</code></span>
        <span>用时 ${elapsed}</span>
        ${lastProgress ? `<span>最后进度 · ${escapeHtml(lastProgress)}</span>` : ''}
      </div>
      <div class="fail-section">
        <div class="fail-label">错误摘要</div>
        <pre class="fail-err">${escapeHtml(errFirstLine)}</pre>
      </div>
      <details class="fail-trace">
        <summary>完整 traceback / stderr (点击展开)</summary>
        <pre>${escapeHtml(String(errRaw))}</pre>
      </details>
      ${logsHtml}
      <div class="fail-hint">
        说明：本次运行未产出结构化报告。请确认 ① 所选模型是否可用（如 Haiku 4.5 需当前账户已开通），
        ② 输入是否触发了上游限制，再按相同输入重试。
      </div>
    </div>`;
}

// 详情页 modal 内的截图 inliner — 与 list 页同逻辑(两份 script 块各自定义)
const _screenshotInlineCache2 = {};
async function inlineScreenshotsInBody(rootEl){
  if (!rootEl) return;
  const imgs = rootEl.querySelectorAll('img[data-screenshot-filename]');
  if (!imgs.length) return;
  await Promise.all(Array.from(imgs).map(async img => {
    if (img.src && img.src.startsWith('data:')) return;
    const fn = img.dataset.screenshotFilename;
    if (!fn) return;
    if (_screenshotInlineCache2[fn]) { img.src = _screenshotInlineCache2[fn]; return; }
    try {
      const resp = await fetch('/api/screenshots/' + encodeURIComponent(fn));
      if (!resp.ok) return;
      const blob = await resp.blob();
      const reader = new FileReader();
      const dataUri = await new Promise((res, rej) => {
        reader.onload = () => res(reader.result);
        reader.onerror = () => rej(reader.error);
        reader.readAsDataURL(blob);
      });
      _screenshotInlineCache2[fn] = dataUri;
      img.src = dataUri;
    } catch(_){}
  }));
}

function closeModal(){
  document.getElementById('modal-overlay').classList.remove('open');
  currentRun = null; currentTool = null;
}
window.closeModal = closeModal;

document.getElementById('refresh').onclick = load;
document.getElementById('search').oninput = render;
document.getElementById('filters').onclick = e => {
  if (e.target.tagName !== 'BUTTON') return;
  document.querySelectorAll('#filters button').forEach(b => b.classList.remove('active'));
  e.target.classList.add('active');
  currentFilter = e.target.dataset.f;
  render();
};

// ===================== 批量删除工具栏 =====================
function updateBulkBar(){
  const countEl = document.getElementById('bulk-count');
  const delBtn = document.getElementById('bulk-del-sel');
  const allCb = document.getElementById('bulk-all-cb');
  const allLabel = document.getElementById('bulk-all-label');
  if (!countEl || !delBtn || !allCb) return;
  // 当前页选中数
  const visibleSel = currentVisibleRunIds.filter(rid => selectedRunIds.has(rid)).length;
  const n = selectedRunIds.size;
  countEl.textContent = '已选 ' + n + ' 条';
  countEl.classList.toggle('has-sel', n > 0);
  delBtn.disabled = n === 0;
  delBtn.textContent = '✕ 删除选中' + (n > 0 ? '(' + n + ')' : '');
  // 「全选当前页」状态
  const allVisChecked = currentVisibleRunIds.length > 0 && visibleSel === currentVisibleRunIds.length;
  const someVisChecked = visibleSel > 0 && visibleSel < currentVisibleRunIds.length;
  allCb.checked = allVisChecked;
  allCb.indeterminate = someVisChecked;
  allLabel.textContent = '全选当前页 (' + currentVisibleRunIds.length + ' 条)';
}

document.getElementById('bulk-all-cb').onchange = (e) => {
  const checked = e.target.checked;
  currentVisibleRunIds.forEach(rid => {
    if (checked) selectedRunIds.add(rid); else selectedRunIds.delete(rid);
  });
  // 直接 re-render 更新所有行的高亮
  render();
};

document.getElementById('bulk-del-sel').onclick = async () => {
  const ids = Array.from(selectedRunIds);
  if (!ids.length) return;
  if (!confirm('确认删除选中的 ' + ids.length + ' 条报告?\n\n该操作不可恢复:内存运行 + 磁盘 JSON + 截图全部删除。')) return;
  const btn = document.getElementById('bulk-del-sel');
  btn.disabled = true; btn.textContent = '删除中…';
  try {
    const r = await fetch('/api/reports/batch_delete', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({run_ids: ids}),
    });
    const d = await r.json();
    if (!r.ok){ alert('批量删除失败:' + (d.detail || r.status)); return; }
    const failedTxt = d.failed && d.failed.length ? `\n失败 ${d.failed.length} 条:` + d.failed.slice(0,5).map(f=>`\n  ${f.run_id.slice(0,12)} — ${f.reason}`).join('') : '';
    alert(`已删除 ${d.deleted} / ${d.total} 条${failedTxt}`);
    selectedRunIds.clear();
    await load();
  } catch(e){
    alert('请求出错:' + e.message);
  } finally {
    btn.disabled = false;
  }
};

document.getElementById('bulk-clear-all').onclick = async () => {
  // 二次确认 — 强制让用户输入 "DELETE"
  const total = allReports.length;
  if (total === 0){ alert('没有报告可删'); return; }
  const c1 = confirm('⚠ 警告:即将清空你能看到的【全部 ' + total + ' 条】报告。\n\n继续吗?');
  if (!c1) return;
  const c2 = prompt('请输入「DELETE」(大写)确认清空所有报告:');
  if (c2 !== 'DELETE'){ alert('已取消'); return; }
  const btn = document.getElementById('bulk-clear-all');
  btn.disabled = true; btn.textContent = '清空中…';
  try {
    const r = await fetch('/api/reports/batch_delete', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({all_visible: true}),
    });
    const d = await r.json();
    if (!r.ok){ alert('清空失败:' + (d.detail || r.status)); return; }
    alert(`已清空 ${d.deleted} / ${d.total} 条报告${d.failed && d.failed.length ? '(失败 '+d.failed.length+' 条)' : ''}`);
    selectedRunIds.clear();
    await load();
  } catch(e){
    alert('请求出错:' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = '⚠ 清空所有';
  }
};
document.querySelectorAll('.modal-tabs button').forEach(b => {
  b.onclick = () => {
    activeModalTab = b.dataset.tab;
    document.querySelectorAll('.modal-tabs button').forEach(x => x.classList.toggle('active', x === b));
    renderModalBody();
  };
});
document.querySelectorAll('.modal-head .download').forEach(b => {
  b.onclick = async () => {
    if (!currentRun || !currentTool) return;
    const r = currentRun;
    const fmt = b.dataset.act;
    if (!['html', 'md', 'json', 'xlsx'].includes(fmt)) return;
    // 委托给 quickDownload 统一处理 preflight + 错误提示
    if (typeof quickDownload === 'function'){
      await quickDownload(r.run_id, r.tool_id, fmt);
      return;
    }
    // 兜底:没有 quickDownload(其他页面)时走老路径
    const url = `/api/reports/${r.run_id}/export.${fmt}`;
    const a = document.createElement('a');
    a.href = url; a.download = `${r.tool_id}_${r.run_id.slice(0,8)}.${fmt}`;
    document.body.appendChild(a); a.click(); a.remove();
  };
});
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });
document.getElementById('modal-overlay').onclick = e => {
  if (e.target.id === 'modal-overlay') closeModal();
};

// === Shared renderer functions (mirror tool detail page, parametric on `tool`) ===
function escapeHtml(s){ return String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function gateClass(action){
  const a = String(action || '').toLowerCase();
  if (a.includes('reject')) return 'reject';
  if (a.includes('proceed') || a.includes('approve') || a === 'pass') return 'proceed';
  return 'warn';
}
function extractTitle(data){
  if (!data || typeof data !== 'object') return '';
  for (const k of ['name','title']) if (typeof data[k] === 'string') return data[k];
  return '';
}
function quickStats(data){
  if (!data || typeof data !== 'object') return '';
  const chips = [];
  if (Array.isArray(data.cases)) chips.push(`<span class="chip">${data.cases.length} 用例</span>`);
  if (Array.isArray(data.issues)) chips.push(`<span class="chip">${data.issues.length} 问题</span>`);
  if (Array.isArray(data.scenarios)) chips.push(`<span class="chip">${data.scenarios.length} 场景</span>`);
  if (Array.isArray(data.endpoints)) chips.push(`<span class="chip">${data.endpoints.length} 接口</span>`);
  if (Array.isArray(data.pages)) chips.push(`<span class="chip">${data.pages.length} 页面</span>`);
  if (Array.isArray(data.matrix)) chips.push(`<span class="chip">${data.matrix.length} 矩阵</span>`);
  if (Array.isArray(data.fix_list)) chips.push(`<span class="chip">${data.fix_list.length} 待修</span>`);
  if (data.confidence && typeof data.confidence.score === 'number'){
    chips.push(`<span class="chip">conf ${(data.confidence.score*100).toFixed(0)}%</span>`);
  }
  return chips.join('');
}
// 详情页的 build/render 与列表页保持一致 — 复用同一实现。
// (两份 script 块只能各自定义；逻辑必须同步;若改一份请同步另一份。)
function buildExecutiveSummary(rep, tool){
  const walkedIssues = [], walkedCases = [];
  function walk(x){
    if (x && typeof x === 'object'){
      if (Array.isArray(x)){ x.forEach(walk); return; }
      const hasCase = ('expected' in x || 'scenario' in x) && ('id' in x);
      const hasIssue = 'severity' in x && (('issue' in x) || ('title' in x) || ('description' in x) || ('name' in x));
      if (hasCase) walkedCases.push(x);
      else if (hasIssue) walkedIssues.push(x);
      Object.values(x).forEach(walk);
    }
  }
  walk(rep.substeps || {});
  const topIssues = Array.isArray(rep.issues) ? rep.issues : walkedIssues;
  const topCases = Array.isArray(rep.cases) ? rep.cases : walkedCases;
  const topRisks = Array.isArray(rep.risks) ? rep.risks : [];
  const topBlockers = Array.isArray(rep.blockers) ? rep.blockers : [];
  const sevRank = {critical:0, high:1, medium:2, low:3, info:4};
  const priRank = {P0:0, P1:1, P2:2, P3:3};
  function getSev(x){ return String(x.severity||'medium').toLowerCase(); }
  function getPri(x){ return String(x.priority||'P2').toUpperCase(); }
  const naturalIssues = topIssues.map(it => ({
    issue_id: String(it.issue_id || it.id || ''),
    title: String(it.title || it.name || it.issue || it.description || '未命名问题').slice(0, 200),
    severity: getSev(it), priority: getPri(it),
    module: String(it.module || it.endpoint || it.file_path || it.location || it.viewport || it.page || it.viewport_filename || '').slice(0, 200),
    current: String(it.current_behavior || it.current || it.observed || it.description || it.issue || '').slice(0, 800),
    expected: String(it.expected_behavior || it.expected || it.requirement || '').slice(0, 800),
    fix: String(it.fix_suggestion || it.fix || it.recommendation || it.suggestion || it.remediation || '').slice(0, 1000),
    repro: Array.isArray(it.reproduce_steps) ? it.reproduce_steps : (it.reproduce_steps ? [String(it.reproduce_steps)] : []),
    accept: String(it.acceptance_criteria || it.acceptance || it.verify || '').slice(0, 600),
    cases: Array.isArray(it.related_test_cases) ? it.related_test_cases : (it.related_test_cases ? [String(it.related_test_cases)] : []),
    owner: String(it.owner_role || it.owner || it.assignee || '').toLowerCase(),
    hours: it.estimated_hours || it.effort || null,
    impact: String(it.impact_scope || it.impact || '').slice(0, 400),
    evidence: String(it.evidence || it.source || '').slice(0, 400),
  })).sort((a,b) => {
    const sa = sevRank[a.severity] ?? 9, sb = sevRank[b.severity] ?? 9;
    if (sa !== sb) return sa - sb;
    const pa = priRank[a.priority] ?? 9, pb = priRank[b.priority] ?? 9;
    return pa - pb;
  }).slice(0, 60);
  let naturalRisks = topRisks.map(r => {
    if (typeof r === 'string') return {title: r, impact:'', why:'', severity:'medium'};
    return {
      id: String(r.id || ''),
      title: String(r.title || r.name || r.risk || '未命名风险').slice(0, 200),
      impact: String(r.impact || r.affects || '').slice(0, 400),
      why: String(r.why || r.reason || r.detail || '').slice(0, 400),
      severity: getSev(r),
    };
  });
  if (!naturalRisks.length){
    const gate = rep.gate_decision || {};
    if (Array.isArray(gate.reasons)){
      naturalRisks = gate.reasons.filter(Boolean).map(s => ({title:String(s), impact:'', why:'', severity:'medium'}));
    }
  }
  const naturalBlockers = topBlockers.map(b => ({
    id: String(b.id || ''),
    title: String(b.title || b.name || '未命名阻碍').slice(0, 200),
    why_blocking: String(b.why_blocking || b.reason || b.why || '').slice(0, 500),
    what_to_unblock: String(b.what_to_unblock || b.action || b.fix || '').slice(0, 500),
    owner_role: String(b.owner_role || b.owner || '').toLowerCase(),
    hours: b.estimated_hours || b.effort || null,
  }));
  const sev = {critical:0, high:0, medium:0, low:0, info:0};
  naturalIssues.forEach(it => { if (sev[it.severity] !== undefined) sev[it.severity]++; });
  const pri = {P0:0, P1:0, P2:0, P3:0, 其他:0};
  topCases.forEach(c => {
    const p = String(c.priority||'').toUpperCase();
    if (pri[p] !== undefined) pri[p]++;
    else if (p) pri.其他++;
  });
  const naturalCases = topCases.map(c => ({
    id: String(c.id || c.case_id || c.tc_id || ''),
    title: String(c.title || c.name || c.scenario || '').slice(0, 200),
    priority: String(c.priority || 'P2').toUpperCase(),
    type: String(c.type || c.kind || '').toLowerCase(),
    status: String(c.status || 'designed').toLowerCase(),
    automation: String(c.automation_tag || c.automation || '').toLowerCase(),
    preconditions: String(c.preconditions || c.precondition || '').slice(0, 300),
    steps: Array.isArray(c.steps) ? c.steps : (c.steps ? [String(c.steps)] : []),
    expected: String(c.expected || c.expected_result || '').slice(0, 400),
  })).sort((a,b) => {
    const pa = priRank[a.priority] ?? 9, pb = priRank[b.priority] ?? 9;
    return pa - pb;
  }).slice(0, 200);
  const gate = rep.gate_decision || {};
  const action = String(gate.action || '').toLowerCase();
  let verdict, vlevel;
  if (rep.verdict){
    const v = String(rep.verdict);
    verdict = v;
    if (v.includes('不通过')) vlevel = 'fail';
    else if (v.includes('有条件') || v.includes('警告') || v.includes('部分')) vlevel = 'warn';
    else vlevel = 'pass';
  } else {
    if (action.includes('reject') || sev.critical > 0){ verdict = '不通过'; vlevel = 'fail'; }
    else if (action.includes('warn') || sev.high > 2 || naturalBlockers.length){ verdict = '有条件通过'; vlevel = 'warn'; }
    else if (!action && !naturalIssues.length && !naturalCases.length){ verdict = '未产出'; vlevel = 'skip'; }
    else { verdict = '通过'; vlevel = 'pass'; }
  }
  const verdictSummary = String(rep.verdict_summary || '').slice(0, 200);
  return {
    verdict, vlevel, verdictSummary,
    risks: naturalRisks, blockers: naturalBlockers,
    issues: naturalIssues, cases: naturalCases, casesCount: naturalCases.length,
    pri, sev, agent: tool.name,
  };
}

function renderExecutiveSummary(s){
  const vmap = {pass:['✅','通过','ok'], warn:['⚠️','有条件通过','warn'], fail:['❌','不通过','bad'], skip:['—','未产出','skip']};
  const [vicon, vtext, vcls] = vmap[s.vlevel] || ['·','—','skip'];
  const sevMap = {critical:'CRITICAL', high:'HIGH', medium:'MEDIUM', low:'LOW', info:'INFO'};
  const priMap = {P0:'P0', P1:'P1', P2:'P2', P3:'P3'};
  const ownerMap = {backend:'后端', frontend:'前端', product:'产品', test:'测试',
                    devops:'运维', security:'安全', data:'数据'};
  const sevTotal = s.sev.critical + s.sev.high + s.sev.medium + s.sev.low + s.sev.info;
  const priTotal = s.pri.P0 + s.pri.P1 + s.pri.P2 + s.pri.P3 + s.pri.其他;
  function sevBar(){
    if (!sevTotal) return '<span class="exec-muted">（暂无问题）</span>';
    const cells = ['critical','high','medium','low','info'].map(k => {
      const n = s.sev[k]; if (!n) return '';
      const pct = (n / sevTotal * 100).toFixed(0);
      return `<span class="sev-bar-seg sev-bar-${k}" style="flex:${n}" title="${sevMap[k]} · ${n} 个 (${pct}%)">${n}</span>`;
    }).filter(Boolean).join('');
    return `<div class="sev-bar">${cells}</div>`;
  }
  function priBar(){
    if (!priTotal) return '';
    const cells = ['P0','P1','P2','P3'].map(k => {
      const n = s.pri[k]; if (!n) return '';
      return `<span class="pri-bar-seg pri-bar-${k}" style="flex:${n}" title="${k} · ${n} 条">${k}: ${n}</span>`;
    }).filter(Boolean).join('');
    return `<div class="pri-bar">${cells}</div>`;
  }
  let html = '';
  html += `<div class="exec-block exec-verdict-block">
    <div class="exec-head"><span class="exec-num">①</span><h3>测试结论</h3></div>
    <div class="verdict ${vcls}"><span class="verdict-icon">${vicon}</span><span>${escapeHtml(vtext)}</span></div>
    ${s.verdictSummary ? `<div class="verdict-summary">${escapeHtml(s.verdictSummary)}</div>` : ''}
    <div class="exec-kpis">
      <div class="exec-kpi"><span class="exec-kpi-num">${sevTotal}</span><span class="exec-kpi-lbl">问题总数</span></div>
      <div class="exec-kpi"><span class="exec-kpi-num">${s.sev.critical + s.sev.high}</span><span class="exec-kpi-lbl">需立即处理</span></div>
      <div class="exec-kpi"><span class="exec-kpi-num">${s.blockers.length}</span><span class="exec-kpi-lbl">阻碍</span></div>
      <div class="exec-kpi"><span class="exec-kpi-num">${s.casesCount}</span><span class="exec-kpi-lbl">用例</span></div>
    </div>
    ${sevBar()}
  </div>`;
  let risksHtml;
  if (s.risks.length){
    risksHtml = '<div class="exec-risk-list">' + s.risks.map(r => {
      const sevBadge = r.severity ? `<span class="sev-tag sev-${r.severity}">${escapeHtml(r.severity)}</span>` : '';
      return `<div class="exec-risk-item">
        <div class="exec-risk-head">${sevBadge}<span class="exec-risk-title">${escapeHtml(r.title)}</span></div>
        ${r.impact ? `<div class="exec-risk-line"><span class="lbl">影响</span>${escapeHtml(r.impact)}</div>` : ''}
        ${r.why ? `<div class="exec-risk-line"><span class="lbl">原因</span>${escapeHtml(r.why)}</div>` : ''}
      </div>`;
    }).join('') + '</div>';
  } else {
    risksHtml = '<p class="exec-muted">（无显著风险）</p>';
  }
  html += `<div class="exec-block">
    <div class="exec-head"><span class="exec-num">②</span><h3>风险结论</h3><span class="exec-count">${s.risks.length}</span></div>
    ${risksHtml}
  </div>`;
  let blockersHtml;
  if (s.blockers.length){
    blockersHtml = '<div class="exec-blocker-list">' + s.blockers.map((b, idx) => `
      <div class="exec-blocker-item">
        <div class="exec-blocker-head">
          <span class="blocker-tag">BLOCKER</span>
          <span class="exec-blocker-title">${idx+1}. ${escapeHtml(b.title)}</span>
          ${b.id ? `<span class="meta-chip">${escapeHtml(b.id)}</span>` : ''}
          ${b.owner_role && ownerMap[b.owner_role] ? `<span class="meta-chip role">👤 ${ownerMap[b.owner_role]}</span>` : ''}
          ${b.hours ? `<span class="meta-chip">⏱ ${b.hours}h</span>` : ''}
        </div>
        ${b.why_blocking ? `<div class="exec-blocker-line"><span class="lbl">为何阻碍</span>${escapeHtml(b.why_blocking)}</div>` : ''}
        ${b.what_to_unblock ? `<div class="exec-blocker-line fix"><span class="lbl">如何解开</span>${escapeHtml(b.what_to_unblock)}</div>` : ''}
      </div>`).join('') + '</div>';
  } else {
    blockersHtml = '<p class="exec-muted">（无阻碍）</p>';
  }
  html += `<div class="exec-block">
    <div class="exec-head"><span class="exec-num">③</span><h3>阻碍</h3><span class="exec-count danger">${s.blockers.length}</span></div>
    ${blockersHtml}
  </div>`;
  function renderIssueCard(it, idx){
    const metaChips = [];
    if (it.issue_id) metaChips.push(`<span class="meta-chip">${escapeHtml(it.issue_id)}</span>`);
    if (it.priority) metaChips.push(`<span class="meta-chip pri-${it.priority}">${priMap[it.priority] || it.priority}</span>`);
    if (it.owner && ownerMap[it.owner]) metaChips.push(`<span class="meta-chip role">👤 ${ownerMap[it.owner]}</span>`);
    if (it.hours) metaChips.push(`<span class="meta-chip">⏱ ${it.hours}h</span>`);
    const reproHtml = it.repro.length
      ? '<ol class="repro-list">' + it.repro.map(s => `<li>${escapeHtml(s)}</li>`).join('') + '</ol>' : '';
    const casesHtml = it.cases.length
      ? `<div class="related-cases">关联用例:${it.cases.map(c => `<code>${escapeHtml(c)}</code>`).join(' ')}</div>` : '';
    return `
      <div class="exec-issue sev-${it.severity}">
        <div class="exec-issue-head">
          <span class="sev-tag sev-${it.severity}">${sevMap[it.severity] || '·'}</span>
          <span class="exec-issue-title">${idx+1}. ${escapeHtml(it.title)}</span>
        </div>
        <div class="exec-issue-meta">${metaChips.join('')}</div>
        ${it.module ? `<div class="exec-issue-loc">位置:<code>${escapeHtml(it.module)}</code></div>` : ''}
        ${it.current ? `<div class="exec-issue-section"><div class="sec-lbl">现状</div><div class="sec-body">${escapeHtml(it.current)}</div></div>` : ''}
        ${it.expected ? `<div class="exec-issue-section"><div class="sec-lbl">期望</div><div class="sec-body">${escapeHtml(it.expected)}</div></div>` : ''}
        ${it.fix ? `<div class="exec-issue-section fix"><div class="sec-lbl">修复建议</div><div class="sec-body">${escapeHtml(it.fix)}</div></div>` : ''}
        ${(reproHtml || it.accept) ? `<div class="exec-issue-section verify"><div class="sec-lbl">验收</div><div class="sec-body">${reproHtml}${it.accept ? `<div class="accept-line">验收标准：${escapeHtml(it.accept)}</div>` : ''}</div></div>` : ''}
        ${casesHtml}
        ${it.impact ? `<div class="exec-issue-impact">影响面:${escapeHtml(it.impact)}</div>` : ''}
        ${it.evidence ? `<div class="exec-issue-evidence">证据:${escapeHtml(it.evidence)}</div>` : ''}
      </div>`;
  }
  const issuesHtml = s.issues.length
    ? s.issues.map((it, i) => renderIssueCard(it, i)).join('')
    : '<p class="exec-muted">（本次未识别到具体问题）</p>';
  html += `<div class="exec-block">
    <div class="exec-head">
      <span class="exec-num">④</span>
      <h3>Bug 表</h3>
      <span class="exec-count">${s.issues.length}</span>
      <span class="exec-count-note">(按严重度 × 优先级排序)</span>
    </div>
    ${issuesHtml}
  </div>`;
  let casesListHtml;
  if (s.cases.length){
    const rows = s.cases.map((c, i) => {
      const statusMap = {
        designed: ['','已设计','muted'],
        executed_pass: ['','已执行通过','ok'],
        executed_fail: ['','执行失败','bad'],
        skipped: ['','已跳过','muted'],
        blocked: ['','阻塞','bad'],
      };
      const [sIcon, sLabel, sCls] = statusMap[c.status] || ['','未定义','muted'];
      return `<tr class="case-row pri-${c.priority}">
        <td class="case-idx">${i+1}</td>
        <td><span class="pri-tag pri-${c.priority}">${priMap[c.priority]||c.priority}</span></td>
        <td><code class="case-id">${escapeHtml(c.id)}</code></td>
        <td class="case-title">${escapeHtml(c.title)}</td>
        <td>${c.type ? `<span class="case-type">${escapeHtml(c.type)}</span>` : ''}</td>
        <td>${c.automation ? `<span class="case-auto">${escapeHtml(c.automation)}</span>` : ''}</td>
        <td><span class="case-status case-status-${sCls}">${sIcon} ${sLabel}</span></td>
      </tr>`;
    }).join('');
    casesListHtml = `<div class="case-table-wrap">
      <table class="case-table">
        <thead><tr><th>#</th><th>优先级</th><th>用例 ID</th><th>用例标题</th><th>类型</th><th>自动化</th><th>状态</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
  } else if (s.casesCount){
    casesListHtml = `<div class="exec-case-total"><span class="num">${s.casesCount}</span><span class="lbl">条已生成用例(未提供详细列表)</span></div>`;
  } else {
    casesListHtml = '<p class="exec-muted">（本次未生成用例）</p>';
  }
  html += `<div class="exec-block">
    <div class="exec-head">
      <span class="exec-num">⑤</span>
      <h3>执行用例记录</h3>
      <span class="exec-count">${s.casesCount}</span>
    </div>
    ${priBar()}
    ${casesListHtml}
  </div>`;
  return html;
}

// 测试用例工具:用例表 + Excel 下载做成报告主体(reports 弹窗版)
function buildTestCaseBlock(rep, runId, toolId){
  const cases = (rep && rep.cases) || [];
  if (!cases.length) return '';
  const rows = cases.map(c => {
    const stepsArr = Array.isArray(c.steps) ? c.steps : (c.steps ? [c.steps] : []);
    const steps = stepsArr.filter(Boolean).map(s => escapeHtml(String(s))).join('\n');
    let exp = c.expected;
    if (Array.isArray(exp)) exp = exp.map(e => typeof e==='string'?e:JSON.stringify(e)).join('\n');
    exp = escapeHtml(String(exp || ''));
    const pri = String(c.priority||'').toUpperCase();
    return '<tr>' +
      '<td class="tc-id">' + escapeHtml(c.id||'') + '</td>' +
      '<td>' + escapeHtml(c.module||'') + '</td>' +
      '<td class="tc-title">' + escapeHtml(c.title||c.name||'') + '</td>' +
      '<td><span class="tc-pri tc-pri-' + pri + '">' + escapeHtml(pri||'-') + '</span></td>' +
      '<td>' + escapeHtml(c.type||'') + '</td>' +
      '<td class="tc-pre">' + escapeHtml(c.preconditions||'') + '</td>' +
      '<td class="tc-steps">' + steps + '</td>' +
      '<td class="tc-exp">' + exp + '</td>' +
    '</tr>';
  }).join('');
  return '<div class="tc-block">' +
    '<div class="tc-banner">' +
      '<div><div class="tc-count">' + cases.length + '</div><div class="tc-count-label">条测试用例</div></div>' +
      '<div class="tc-mid">本工具产出 <b>纯人工执行</b> 测试用例,步骤是自然语言操作清单。' +
        '点右侧按钮导出标准 Excel(含「执行结果 / 实际结果」空列),可直接发给测试团队执行。</div>' +
      '<button class="tc-excel-btn" data-tc-runid="' + runId + '" data-tc-toolid="' + (toolId||'step2') + '">↓ 下载 Excel 用例表</button>' +
    '</div>' +
    '<div class="tc-table-wrap"><table class="tc-table"><thead><tr>' +
      '<th>用例编号</th><th>模块</th><th>用例标题</th><th>优先级</th><th>类型</th>' +
      '<th>前置条件</th><th>测试步骤</th><th>预期结果</th>' +
    '</tr></thead><tbody>' + rows + '</tbody></table></div>' +
  '</div>';
}

function renderHtmlReport(r, tool, opts){
  opts = opts || {};
  const expandAll = !!opts.expandAll;
  const rep = r.report || {};
  const meta = rep.meta || {};
  const u = r.usage || {};
  let html = '';
  const elapsed = (r.finished_at && r.started_at) ? (r.finished_at - r.started_at).toFixed(1) : '—';

  // === 执行摘要 4 块结构 (测试结论 / 风险结论 / 问题描述 / 用例执行) ===
  const summary = buildExecutiveSummary(rep, tool);

  const pc2 = meta.project_code || r.project_code || '';
  const pn2 = meta.project_name || r.project_name || '';
  const projectRow2 = (pc2 || pn2)
    ? `<div class="report-project">
        <span class="lbl">项目编号</span><code>${escapeHtml(pc2 || '—')}</code>
        <span class="lbl">项目名称</span><span class="val">${escapeHtml(pn2 || '—')}</span>
      </div>` : '';
  html += `<div class="report-hero">
    <div class="report-icon">${tool.icon}</div>
    <div style="flex:1;min-width:0">
      <h4>${escapeHtml(tool.name)} · 报告</h4>
      <div class="meta">
        ${meta.produced_at_utc ? new Date(meta.produced_at_utc).toLocaleString('zh-CN', {hour12:false}) : ''}
        · 模型 <code>${escapeHtml(meta.model_id || '?')}</code>
        · run <code>${r.run_id.slice(0,12)}</code>
      </div>
      ${projectRow2}
      <div class="stat-pills">
        <span class="pill">用时 <span class="v">${elapsed}s</span></span>
        <span class="pill">成本 <span class="v">$${u.cost_usd ?? '—'}</span></span>
        <span class="pill">输出 <span class="v">${(u.output_tokens||0).toLocaleString()}</span></span>
        <span class="pill">缓存 <span class="v">${(u.cache_read_tokens||0).toLocaleString()}</span></span>
      </div>
    </div>
  </div>`;

  // 测试用例工具(step2):只出用例表 + Excel,不要 verdict / 风险 / Bug 那套"报告"。
  if (tool && tool.id === 'step2'){
    const tcBlock = buildTestCaseBlock(rep, r.run_id, tool.id);
    html += tcBlock || '<div class="run-empty" style="padding:48px;text-align:center">本次未生成测试用例</div>';
    return html;
  }

  // 测试结论 + 风险结论 + 问题描述 + 用例执行
  html += renderExecutiveSummary(summary);
  // Inline page screenshots (step5 / h5_adapt only)
  // 用 data-screenshot-filename 占位,渲染后 inlineScreenshotsInArea() 把 src
  // 替换成 data: URI,报告自包含、不暴露任何本地文件夹路径。
  const shots = (meta.screenshots || []).filter(s => !s.error);
  if (shots.length){
    const groupedByUrl = {};
    shots.forEach(s => {(groupedByUrl[s.url] = groupedByUrl[s.url] || []).push(s);});
    const imgMap = (opts && opts.imgMap) || {};
    let shotsHtml = '<div class="report-screenshots">';
    shotsHtml += '<div class="screenshots-head">页面截图证据 <span class="screenshots-hint">（已嵌入报告，无本地文件依赖）</span></div>';
    Object.entries(groupedByUrl).forEach(([url, arr]) => {
      shotsHtml += `<div class="shot-group"><div class="shot-url"><code>${escapeHtml(url)}</code></div>`;
      shotsHtml += '<div class="shot-grid">';
      arr.forEach(s => {
        const annotated = s.annotated_filename;
        const fnPrimary = annotated || s.filename;
        // 已 inline 直接用;否则放占位 src,后续 inliner 异步替换为 data: URI
        const initialSrc = imgMap[fnPrimary] || `/api/screenshots/${encodeURIComponent(fnPrimary)}`;
        const issueBadge = s.issue_count
          ? `<span class="issue-badge">${s.issue_count} 个问题</span>` : '';
        shotsHtml += `<div class="shot-cell" title="${escapeHtml(s.viewport)} · ${s.width}×${s.height}${annotated?' · 已标注':''}">
          <img src="${initialSrc}" data-screenshot-filename="${escapeHtml(fnPrimary)}" alt="${escapeHtml(s.viewport)}" loading="lazy">
          <div class="shot-cap">${escapeHtml(s.viewport)} · ${s.width}×${s.height}${issueBadge}</div>
        </div>`;
      });
      shotsHtml += '</div></div>';
    });
    shotsHtml += '</div>';
    html += shotsHtml;
  }
  const hasContractData = (Array.isArray(rep.issues) && rep.issues.length) ||
                          (Array.isArray(rep.cases) && rep.cases.length);
  const subs = rep.substeps || {};
  let firstWithContent = null;
  for (const sid of tool.prompts){
    if (subs[sid] && Object.keys(subs[sid]).length){ firstWithContent = sid; break; }
  }
  html += `<div class="substep-section-head">
    <span class="substep-section-title">各子步骤原始输出</span>
    <span class="substep-section-hint">${hasContractData ? '已聚合到上方 5 段;点击展开看原始数据' : '点击展开'}</span>
  </div>`;
  let idx = 0;
  for (const sid of tool.prompts){
    idx++;
    const data = subs[sid];
    const title = (tool._substepNames && tool._substepNames[sid]) || extractTitle(data) || `子分析 ${idx}`;
    const open = expandAll || (!hasContractData && sid === firstWithContent);
    if (data == null){
      html += `<div class="report-sub">
        <div class="report-sub-head">
          <span class="report-sub-twirl">▶</span>
          <span class="report-sub-num">${idx}</span>
          <span class="report-sub-name" style="color:var(--fg-3)">${escapeHtml(title)} · 已跳过</span>
        </div>
      </div>`;
      continue;
    }
    html += `<div class="report-sub${open?' open':''}">
      <div class="report-sub-head">
        <span class="report-sub-twirl">▶</span>
        <span class="report-sub-num">${idx}</span>
        <span class="report-sub-name">${escapeHtml(title)}</span>
        <span class="report-sub-stats">${quickStats(data) || ''}</span>
      </div>
      <div class="report-sub-body">${renderSmart(data)}</div>
    </div>`;
  }
  return html;
}
function renderSmart(data, depth){
  depth = depth || 0;
  if (data === null || data === undefined) return '<span class="empty-array">—</span>';
  if (typeof data === 'string'){ return data ? escapeHtml(data) : '<span class="empty-array">""</span>'; }
  if (typeof data === 'number' || typeof data === 'boolean') return `<span style="color:var(--ac)">${escapeHtml(String(data))}</span>`;
  if (Array.isArray(data)){
    if (!data.length) return '<span class="empty-array">[]</span>';
    const allObj = data.every(x => x && typeof x === 'object' && !Array.isArray(x));
    if (allObj && data.length >= 2) return renderObjectArrayAsTable(data);
    if (allObj) return renderSmart(data[0], depth+1);
    return '<ul class="report-list">' + data.slice(0,50).map(x => `<li>${renderSmart(x, depth+1)}</li>`).join('') +
      (data.length > 50 ? `<li class="empty-array">…剩余 ${data.length - 50} 已折叠</li>` : '') + '</ul>';
  }
  if (typeof data === 'object') return renderObjectAsKv(data, depth);
  return escapeHtml(String(data));
}
function renderObjectAsKv(obj, depth){
  const entries = Object.entries(obj);
  if (!entries.length) return '<span class="empty-array">{}</span>';
  return '<dl class="report-kv">' + entries.map(([k,v]) =>
    `<dt>${escapeHtml(k)}</dt><dd>${renderFieldValue(k, v, depth)}</dd>`
  ).join('') + '</dl>';
}
function renderFieldValue(key, v, depth){
  if ((key === 'severity' || key.endsWith('_severity')) && typeof v === 'string'){
    return `<span class="sev sev-${escapeHtml(v.toLowerCase())}">${escapeHtml(v)}</span>`;
  }
  if (key === 'confidence' && v && typeof v === 'object' && typeof v.score === 'number'){
    const pct = (v.score*100).toFixed(0);
    return `<span class="confbar"><span class="track"><span class="fill" style="width:${pct}%"></span></span><span class="pct">${pct}%</span></span>${
      v.rationale ? ` <span style="color:var(--fg-3)">— ${escapeHtml(String(v.rationale).slice(0,140))}</span>` : ''
    }`;
  }
  if (key === 'gate_decision' && v && typeof v === 'object'){
    const cls = gateClass(v.action);
    const rs = (v.reasons || []).map(x => `<div>· ${escapeHtml(x)}</div>`).join('');
    return `<div class="gate-banner ${cls}" style="margin:0;border-bottom:none">
      <span class="badge">${escapeHtml(String(v.action).toLowerCase())}</span>
      <div class="reasons">${rs}</div></div>`;
  }
  if (Array.isArray(v) && v.length > 5){
    return `<details class="report-detail" ${depth === 0 ? 'open' : ''}><summary>${v.length} 项</summary><div>${renderSmart(v, depth+1)}</div></details>`;
  }
  if (typeof v === 'object' && v !== null && !Array.isArray(v) && Object.keys(v).length > 6){
    return `<details class="report-detail"><summary>对象（${Object.keys(v).length} 字段）</summary><div>${renderSmart(v, depth+1)}</div></details>`;
  }
  return renderSmart(v, depth+1);
}
function renderObjectArrayAsTable(arr){
  const keySet = new Set();
  arr.forEach(o => Object.keys(o).forEach(k => keySet.add(k)));
  const priority = ['id','title','name','severity','status','category','kind','endpoint','page','area','module','expected','actual','fix','impact','effort_hours','severity_if_fails'];
  const keys = [...keySet].sort((a,b)=>{
    const ai = priority.indexOf(a), bi = priority.indexOf(b);
    if (ai >= 0 && bi >= 0) return ai - bi;
    if (ai >= 0) return -1; if (bi >= 0) return 1; return 0;
  }).slice(0,6);
  const head = `<thead><tr>${keys.map(k => `<th>${escapeHtml(k)}</th>`).join('')}</tr></thead>`;
  const body = arr.slice(0,30).map(o => `<tr>${keys.map(k => `<td>${renderTableCell(k, o[k])}</td>`).join('')}</tr>`).join('');
  const foot = arr.length > 30
    ? `<tfoot><tr><td colspan="${keys.length}" style="text-align:center;color:var(--fg-3)">… 共 ${arr.length} 条，已显示前 30 条</td></tr></tfoot>` : '';
  return `<table class="report-table">${head}<tbody>${body}</tbody>${foot}</table>`;
}
function renderTableCell(key, v){
  if (v == null) return '<span class="empty-array">—</span>';
  if ((key === 'severity' || key.endsWith('_severity')) && typeof v === 'string'){
    return `<span class="sev sev-${escapeHtml(v.toLowerCase())}">${escapeHtml(v)}</span>`;
  }
  if (typeof v === 'string'){ return escapeHtml(v.length > 100 ? v.slice(0,97)+'…' : v); }
  if (typeof v === 'number' || typeof v === 'boolean') return `<span style="color:var(--ac)">${escapeHtml(String(v))}</span>`;
  if (Array.isArray(v)){
    if (!v.length) return '<span class="empty-array">[]</span>';
    if (v.length <= 3 && v.every(x => typeof x === 'string' || typeof x === 'number')) return v.map(x => escapeHtml(String(x))).join(', ');
    return `<details class="report-detail"><summary>${v.length} 项</summary><div>${renderSmart(v)}</div></details>`;
  }
  if (typeof v === 'object') return `<details class="report-detail"><summary>{${Object.keys(v).length}}</summary><div>${renderSmart(v)}</div></details>`;
  return escapeHtml(String(v));
}

// === Exporters (mirror tool detail page) ===
function downloadBlob(content, filename, mime){
  const blob = new Blob([content], {type: mime});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function buildStandaloneHtml(r, tool, opts){
  opts = opts || {};
  const body = renderHtmlReport(r, tool, {expandAll: true, imgMap: opts.imgMap});
  const title = `${tool.name} 报告 — ${r.run_id.slice(0,8)}`;
  // Light theme inline CSS suitable for sharing/printing
  const css = `:root{--bg:#fff;--surface:#f8fafc;--surface-2:#eef2f7;--line:#dde3ec;--line-2:#cbd5e1;--fg:#1f2937;--fg-2:#4b5563;--fg-3:#6b7280;--fg-4:#9ca3af;--ac:#0d9488;--ac-2:#0891b2;--warn:#b45309;--ok:#059669;--bad:#dc2626;--mono:ui-monospace,SFMono-Regular,"JetBrains Mono",Menlo,monospace;--sans:-apple-system,BlinkMacSystemFont,"SF Pro Display","SF Pro Text","PingFang SC","Helvetica Neue",Arial,sans-serif}*{box-sizing:border-box}body{margin:0 auto;background:var(--bg);color:var(--fg);font-family:var(--sans);line-height:1.6;padding:32px;max-width:1080px}h1{font-size:22px;margin:0 0 12px}.meta-row{font-family:var(--mono);font-size:12px;color:var(--fg-3);margin-bottom:18px;padding:10px 14px;background:var(--surface);border-radius:8px;border:1px solid var(--line)}code{background:var(--surface);padding:1px 6px;border-radius:3px;color:var(--ac-2);border:1px solid var(--line);font-family:var(--mono);font-size:11.5px}.report-hero{display:flex;gap:14px;padding:18px 22px;border:1px solid var(--line);border-radius:10px;background:var(--surface);margin-bottom:18px}.report-hero .report-icon{font-size:24px;width:42px;height:42px;border-radius:8px;background:#fff;border:1px solid var(--line-2);display:grid;place-items:center}.report-hero h4{margin:0;font-size:16px;font-weight:600}.report-hero .meta{font-family:var(--mono);font-size:11.5px;color:var(--fg-3);margin-top:5px}
.report-project{margin-top:8px;display:flex;flex-wrap:wrap;gap:14px;padding:6px 0 0;border-top:1px dashed var(--line);font-size:12.5px;align-items:center}
.report-project .lbl{color:var(--fg-3);font-size:10.5px;letter-spacing:.04em;text-transform:uppercase;font-weight:500;margin-right:2px}
.report-project code{background:var(--surface-2);padding:2px 7px;border-radius:4px;color:var(--fg);font-family:var(--mono);font-size:12px;border:none}
.report-project .val{color:var(--fg);font-weight:500}.stat-pills{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}.stat-pills .pill{font-family:var(--mono);font-size:11px;padding:3px 9px;border-radius:4px;background:#fff;border:1px solid var(--line);color:var(--fg-2)}.stat-pills .pill .v{color:var(--ac);font-weight:600}.gate-banner{padding:14px 18px;display:flex;gap:12px;border-radius:10px;border:1px solid var(--line);margin-bottom:16px}.gate-banner.proceed{background:rgba(5,150,105,.06);border-color:rgba(5,150,105,.3)}.gate-banner.reject{background:rgba(220,38,38,.06);border-color:rgba(220,38,38,.3)}.gate-banner.warn{background:rgba(180,83,9,.06);border-color:rgba(180,83,9,.3)}.gate-banner .badge{padding:3px 10px;border-radius:4px;font-family:var(--mono);font-size:11px;font-weight:700;text-transform:uppercase;flex-shrink:0;margin-top:1px}.gate-banner.proceed .badge{background:rgba(5,150,105,.15);color:var(--ok)}.gate-banner.reject .badge{background:rgba(220,38,38,.15);color:var(--bad)}.gate-banner.warn .badge{background:rgba(180,83,9,.15);color:var(--warn)}.gate-banner .reasons{font-family:var(--mono);font-size:12.5px;color:var(--fg-2)}.report-sub{border:1px solid var(--line);border-radius:10px;margin-bottom:14px;overflow:hidden;page-break-inside:avoid}.report-sub-head{display:flex;align-items:center;gap:10px;padding:13px 18px;background:var(--surface);font-size:14px;border-bottom:1px solid var(--line)}.report-sub-twirl{display:none}.report-sub-num{font-family:var(--mono);font-size:11px;font-weight:700;color:#fff;background:var(--ac);width:22px;height:22px;border-radius:50%;display:inline-grid;place-items:center;flex-shrink:0}.report-sub-name{font-weight:600}.report-sub-stats{margin-left:auto;font-family:var(--mono);font-size:11px;color:var(--fg-3);display:flex;gap:6px}.report-sub-stats .chip{padding:2px 8px;border-radius:3px;background:#fff;border:1px solid var(--line)}.report-sub-body{padding:16px 22px;background:#fff}.report-sub:not(.open) .report-sub-body{display:none}.report-kv{display:grid;grid-template-columns:max-content 1fr;gap:7px 16px;margin:0;font-size:13px}.report-kv dt{color:var(--fg-3);font-family:var(--mono);font-size:12px;align-self:start}.report-kv dd{margin:0;color:var(--fg);min-width:0}.report-table{width:100%;border-collapse:collapse;font-size:12px;margin:8px 0;border:1px solid var(--line);border-radius:6px;overflow:hidden}.report-table th{font-family:var(--mono);font-size:11px;text-align:left;padding:8px 10px;background:var(--surface-2);color:var(--fg-3);text-transform:uppercase;font-weight:600;border-bottom:1px solid var(--line)}.report-table td{padding:8px 10px;border-bottom:1px solid var(--line);font-family:var(--mono);font-size:11.5px;color:var(--fg);vertical-align:top;word-break:break-word}.sev{font-family:var(--mono);font-size:10px;font-weight:700;padding:1px 7px;border-radius:3px;text-transform:uppercase}.sev-critical{background:#fee2e2;color:#991b1b}.sev-high,.sev-major{background:#fee2e2;color:#dc2626}.sev-medium{background:#fef3c7;color:#92400e}.sev-low{background:#f3f4f6;color:var(--fg-2)}.confbar{display:inline-flex;align-items:center;gap:6px}.confbar .track{width:80px;height:6px;border-radius:3px;background:var(--line);overflow:hidden}.confbar .fill{height:100%;background:var(--ac)}.confbar .pct{color:var(--ac);font-weight:600;font-family:var(--mono);font-size:11px}ul.report-list{margin:0;padding-left:20px;font-size:13px;line-height:1.75}.empty-array{color:var(--fg-4);font-family:var(--mono);font-size:11px;font-style:italic}details.report-detail summary{cursor:pointer;font-family:var(--mono);font-size:11.5px;color:var(--ac)}details.report-detail[open] > div{margin:6px 0 6px 16px;padding:8px 12px;background:var(--surface);border-left:2px solid var(--line);border-radius:4px}@media print{body{padding:0}.report-sub{page-break-inside:avoid}}`;
  const meta = (r.report && r.report.meta) || {};
  const generatedAt = new Date().toLocaleString('zh-CN', {hour12:false});
  const navCss = `.standalone-nav{position:fixed;top:14px;left:14px;right:14px;display:flex;justify-content:space-between;pointer-events:none;z-index:50}.standalone-nav button{pointer-events:auto;background:rgba(15,17,21,.92);color:#fff;border:1px solid rgba(255,255,255,.12);border-radius:8px;padding:8px 14px;font-size:13px;font-weight:500;cursor:pointer;-webkit-backdrop-filter:saturate(180%) blur(12px);backdrop-filter:saturate(180%) blur(12px);box-shadow:0 4px 14px rgba(0,0,0,.18);display:inline-flex;align-items:center;gap:6px;transition:background .15s,transform .12s}.standalone-nav button:hover{background:rgba(15,17,21,.98);transform:translateY(-1px)}.standalone-nav button:active{transform:translateY(0)}@media print{.standalone-nav{display:none}}body{padding-top:60px}`;
  const navHtml = `<div class="standalone-nav"><button onclick="(function(){if(history.length>1){history.back();}else if(window.opener){window.close();}else{window.scrollTo({top:0,behavior:'smooth'});}})()" title="返回上一页">← 返回</button><button onclick="window.scrollTo({top:0,behavior:'smooth'})" title="返回顶部">↑ 顶部</button></div>`;
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>${escapeHtml(title)}</title><style>${css}${navCss}</style></head><body>${navHtml}<h1>${escapeHtml(tool.name)} · 分析报告</h1><div class="meta-row">生成时间：${generatedAt} · run id：<code>${r.run_id}</code> · 模型：<code>${escapeHtml(meta.model_id || '?')}</code></div>${body}<div style="margin-top:32px;padding-top:14px;border-top:1px solid #ddd;font-family:monospace;font-size:11px;color:#888;text-align:center">由 天枢·裁决生成 · ${escapeHtml(tool.id)} · ${new Date().toISOString()}</div></body></html>`;
}
function buildMarkdownReport(r, tool){
  const rep = r.report || {};
  const meta = rep.meta || {};
  const u = r.usage || {};
  const lines = [];
  lines.push(`# ${tool.name} · 分析报告`); lines.push('');
  lines.push(`- **生成时间**：${new Date().toLocaleString('zh-CN', {hour12:false})}`);
  lines.push(`- **Run ID**：\`${r.run_id}\``);
  lines.push(`- **模型**：\`${meta.model_id || '?'}\``);
  if (r.finished_at && r.started_at) lines.push(`- **耗时**：${(r.finished_at - r.started_at).toFixed(1)}s`);
  if (u.cost_usd != null) lines.push(`- **成本**：$${u.cost_usd}`);
  lines.push('');
  if (rep.gate_decision){
    const g = rep.gate_decision;
    lines.push(`## ⚠️ 闸门决策：\`${g.action}\``);
    (g.reasons || []).forEach(rs => lines.push(`- ${rs}`));
    lines.push('');
  }
  let idx = 0;
  for (const sid of tool.prompts){
    idx++;
    const data = (rep.substeps || {})[sid];
    const title = (tool._substepNames && tool._substepNames[sid]) || extractTitle(data) || `子分析 ${idx}`;
    lines.push(`## ${idx}. ${title}`);
    if (data == null){ lines.push('_已跳过_'); lines.push(''); continue; }
    lines.push(''); lines.push('```json'); lines.push(JSON.stringify(data, null, 2)); lines.push('```'); lines.push('');
  }
  return lines.join('\n');
}

load();
</script>
</body></html>
"""


@app.get("/reports", response_class=HTMLResponse)
async def reports_page() -> str:
    return _inject_shared_overlays(REPORTS_HTML)
