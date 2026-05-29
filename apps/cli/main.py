"""qactl — command-line entry for the AI Test Toolkit.

Usage:
    qactl step1 run --prd path/to/prd.md --api-doc path/to/api.md \\
        --project-id proj-1 --out ./output/reports/
    qactl step2 run --requirement-report <path.json> --project-id proj-1
    qactl step4 run --requirement-report <path.json> --test-case-report <path.json> \\
        --api-doc <path>
    qactl step5 run --requirement-report <path.json> --test-case-report <path.json>
    qactl step6 run --test-case-report <path.json> --dry-run
    qactl tdr submit --review <path.json>
    qactl serve --host 0.0.0.0 --port 8080

Environment:
    ANTHROPIC_API_KEY must be set (or placed in .env).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from packages.core.config import settings
from packages.core.llm import LlmClient
from packages.core.memory import LayeredMemory, SqliteMemoryStore
from packages.core.telemetry import configure_logging, get_logger
from packages.workflow.base import StepContext, resolve_evidence_dir
from packages.workflow.step1_requirement import Step1Orchestrator
from packages.workflow.step2_testcase import Step2Orchestrator
from packages.workflow.step4_api import Step4Orchestrator
from packages.workflow.step5_ui import Step5Orchestrator
from packages.workflow.step6_agent import Step6Orchestrator
from packages.workflow.step6_agent.executor import ExecutionEnvironment

app = typer.Typer(
    name="qactl",
    help="AI Test Toolkit — 8步SOP × TDR质量标准",
    no_args_is_help=True,
)
step1_app = typer.Typer(help="Step 1 — 需求拆解")
step2_app = typer.Typer(help="Step 2 — 测试用例设计")
step4_app = typer.Typer(help="Step 4 — 接口测试")
step5_app = typer.Typer(help="Step 5 — UI 一致性比对")
step6_app = typer.Typer(help="Step 6 — Agent 自动化执行")
tdr_app = typer.Typer(help="TDR — 评审工作台")
prompts_app = typer.Typer(help="提示词库 — 列表 / 查看 / 导出")
app.add_typer(step1_app, name="step1")
app.add_typer(step2_app, name="step2")
app.add_typer(step4_app, name="step4")
app.add_typer(step5_app, name="step5")
app.add_typer(step6_app, name="step6")
app.add_typer(tdr_app, name="tdr")
app.add_typer(prompts_app, name="prompts")
console = Console()
logger = get_logger("cli")


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _read_if_exists(path: Path | None) -> str | None:
    if path is None:
        return None
    if not path.exists():
        raise typer.BadParameter(f"文件不存在：{path}")
    return path.read_text(encoding="utf-8")


def _read_json(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    if not path.exists():
        raise typer.BadParameter(f"文件不存在：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _require_api_key() -> None:
    """No-op — toolkit talks to the local Claude Code via claude-agent-sdk.

    Kept as a callable so existing call sites keep working. Verifies the
    Python SDK is importable; auth comes from the user's local Claude Code
    login (no API key needed).
    """
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError as exc:
        console.print(
            Panel.fit(
                "[red]claude-agent-sdk 未安装。[/red]\n"
                "请执行：pip install claude-agent-sdk\n"
                "并确保本地 Claude Code 已登录。",
                border_style="red",
            )
        )
        raise typer.Exit(code=2) from exc


def _make_ctx(
    *, inputs: dict[str, Any], project_id: str, tenant_id: str, out: Path
) -> StepContext:
    run_id = str(uuid4())
    out.mkdir(parents=True, exist_ok=True)
    db_path = str((out / "memory.db").resolve())
    store = SqliteMemoryStore(db_path)
    memory = LayeredMemory(
        store=store, run_id=run_id, project_id=project_id, tenant_id=tenant_id
    )
    return StepContext(
        run_id=run_id,
        project_id=project_id,
        tenant_id=tenant_id,
        inputs=inputs,
        memory=memory,
        llm=LlmClient(),
        evidence_dir=resolve_evidence_dir(),
    )


def _print_report_summary(title: str, ctx: StepContext, path: Path, gate: Any, confidence: Any) -> None:
    t = Table(title=title, show_header=False)
    t.add_column("指标")
    t.add_column("值", style="bold")
    t.add_row("闸门决策", str(getattr(gate, "action", gate)))
    t.add_row("闸门原因", "\n".join(getattr(gate, "reasons", []) or []))
    t.add_row("置信度", f"{confidence.score:.2f} ({confidence.grade.value})")
    t.add_row("Token-in", str(ctx.usage.input_tokens))
    t.add_row("Token-out", str(ctx.usage.output_tokens))
    t.add_row("Cache-read", str(ctx.usage.cache_read_tokens))
    t.add_row("成本USD", f"{ctx.usage.cost_usd:.4f}")
    t.add_row("报告路径", str(path))
    console.print(t)


# ---------------------------------------------------------------------
# Step 1
# ---------------------------------------------------------------------

@step1_app.command("run")
def step1_run(
    prd: Path | None = typer.Option(None, "--prd"),
    prototype: Path | None = typer.Option(None, "--prototype"),
    ui_design: Path | None = typer.Option(None, "--ui-design"),
    flow_chart: Path | None = typer.Option(None, "--flow-chart"),
    api_doc: Path | None = typer.Option(None, "--api-doc"),
    business_rules: Path | None = typer.Option(None, "--business-rules"),
    project_id: str = typer.Option(..., "--project-id"),
    tenant_id: str = typer.Option("default", "--tenant-id"),
    out: Path = typer.Option(Path("./output/reports"), "--out"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    configure_logging(settings.log_level)

    inputs = {
        "prd": _read_if_exists(prd),
        "prototype": _read_if_exists(prototype),
        "ui_design": _read_if_exists(ui_design),
        "flow_chart": _read_if_exists(flow_chart),
        "api_doc": _read_if_exists(api_doc),
        "business_rules": _read_if_exists(business_rules),
    }

    if dry_run:
        from packages.core.prompts import load_step

        prompts = load_step("step1_requirement")
        tpl = prompts.get("step1.1")
        rendered = tpl.render(
            {
                "PRD": inputs["prd"],
                "原型图": inputs["prototype"],
                "UI设计稿": inputs["ui_design"],
                "流程图": inputs["flow_chart"],
                "接口文档": inputs["api_doc"],
                "业务规则": inputs["business_rules"],
            }
        )
        console.print(Panel(rendered, title="step1.1 rendered (dry-run)"))
        raise typer.Exit(code=0)

    _require_api_key()
    ctx = _make_ctx(inputs=inputs, project_id=project_id, tenant_id=tenant_id, out=out)
    console.print(Panel.fit(f"[cyan]Step 1[/cyan] run_id=[bold]{ctx.run_id}[/bold]"))
    report = asyncio.run(Step1Orchestrator(ctx).execute())
    path = out / f"step1_{ctx.run_id}.json"
    path.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _print_report_summary("Step 1 摘要", ctx, path, report.gate_decision, report.confidence)


# ---------------------------------------------------------------------
# Step 2
# ---------------------------------------------------------------------

@step2_app.command("run")
def step2_run(
    requirement_report: Path = typer.Option(..., "--requirement-report"),
    project_id: str = typer.Option(..., "--project-id"),
    tenant_id: str = typer.Option("default", "--tenant-id"),
    out: Path = typer.Option(Path("./output/reports"), "--out"),
) -> None:
    configure_logging(settings.log_level)
    _require_api_key()
    inputs = {"requirement_report": _read_json(requirement_report)}
    ctx = _make_ctx(inputs=inputs, project_id=project_id, tenant_id=tenant_id, out=out)
    console.print(Panel.fit(f"[cyan]Step 2[/cyan] run_id=[bold]{ctx.run_id}[/bold]"))
    report = asyncio.run(Step2Orchestrator(ctx).execute())
    path = out / f"step2_{ctx.run_id}.json"
    path.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _print_report_summary("Step 2 摘要", ctx, path, report.gate_decision, report.confidence)


# ---------------------------------------------------------------------
# Step 4
# ---------------------------------------------------------------------

@step4_app.command("run")
def step4_run(
    requirement_report: Path = typer.Option(..., "--requirement-report"),
    test_case_report: Path = typer.Option(..., "--test-case-report"),
    api_doc: Path | None = typer.Option(None, "--api-doc"),
    project_id: str = typer.Option(..., "--project-id"),
    tenant_id: str = typer.Option("default", "--tenant-id"),
    out: Path = typer.Option(Path("./output/reports"), "--out"),
) -> None:
    configure_logging(settings.log_level)
    _require_api_key()
    inputs = {
        "requirement_report": _read_json(requirement_report),
        "test_case_report": _read_json(test_case_report),
        "api_doc": _read_if_exists(api_doc),
    }
    ctx = _make_ctx(inputs=inputs, project_id=project_id, tenant_id=tenant_id, out=out)
    console.print(Panel.fit(f"[cyan]Step 4[/cyan] run_id=[bold]{ctx.run_id}[/bold]"))
    report = asyncio.run(Step4Orchestrator(ctx).execute())
    path = out / f"step4_{ctx.run_id}.json"
    path.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _print_report_summary("Step 4 摘要", ctx, path, report.gate_decision, report.confidence)


# ---------------------------------------------------------------------
# Step 5
# ---------------------------------------------------------------------

@step5_app.command("run")
def step5_run(
    requirement_report: Path | None = typer.Option(None, "--requirement-report"),
    test_case_report: Path | None = typer.Option(None, "--test-case-report"),
    project_id: str = typer.Option(..., "--project-id"),
    tenant_id: str = typer.Option("default", "--tenant-id"),
    out: Path = typer.Option(Path("./output/reports"), "--out"),
) -> None:
    configure_logging(settings.log_level)
    _require_api_key()
    inputs = {
        "requirement_report": _read_json(requirement_report) or {},
        "test_case_report": _read_json(test_case_report) or {},
        "design_assets": [],
        "actual_snapshots": [],
        "state_captures": [],
    }
    ctx = _make_ctx(inputs=inputs, project_id=project_id, tenant_id=tenant_id, out=out)
    console.print(Panel.fit(f"[cyan]Step 5[/cyan] run_id=[bold]{ctx.run_id}[/bold]"))
    report = asyncio.run(Step5Orchestrator(ctx).execute())
    path = out / f"step5_{ctx.run_id}.json"
    path.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _print_report_summary("Step 5 摘要", ctx, path, report.gate_decision, report.confidence)


# ---------------------------------------------------------------------
# Step 6
# ---------------------------------------------------------------------

@step6_app.command("run")
def step6_run(
    test_case_report: Path = typer.Option(..., "--test-case-report"),
    project_id: str = typer.Option(..., "--project-id"),
    tenant_id: str = typer.Option("default", "--tenant-id"),
    out: Path = typer.Option(Path("./output/reports"), "--out"),
    dry_run: bool = typer.Option(True, "--dry-run/--live"),
) -> None:
    configure_logging(settings.log_level)
    _require_api_key()
    inputs = {
        "test_case_report": _read_json(test_case_report),
        "automation_risk": {},
        "env_info": {},
    }
    ctx = _make_ctx(inputs=inputs, project_id=project_id, tenant_id=tenant_id, out=out)
    inputs["execution_env"] = ExecutionEnvironment(
        evidence_dir=ctx.evidence_dir / ctx.run_id / "step6",
        dry_run=dry_run,
    )
    console.print(
        Panel.fit(
            f"[cyan]Step 6[/cyan] run_id=[bold]{ctx.run_id}[/bold]\n"
            f"mode={'dry-run' if dry_run else 'live'}"
        )
    )
    report = asyncio.run(Step6Orchestrator(ctx).execute())
    path = out / f"step6_{ctx.run_id}.json"
    path.write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _print_report_summary("Step 6 摘要", ctx, path, report.gate_decision, report.confidence)


# ---------------------------------------------------------------------
# TDR
# ---------------------------------------------------------------------

@tdr_app.command("submit")
def tdr_submit(
    review: Path = typer.Option(..., "--review", help="TDR review JSON payload"),
    project_id: str = typer.Option(..., "--project-id"),
    tenant_id: str = typer.Option("default", "--tenant-id"),
    out: Path = typer.Option(Path("./output/reports"), "--out"),
) -> None:
    """Accept a prepared TDR JSON, compute decision, persist."""
    from packages.tdr import TdrWorkstation

    payload = _read_json(review) or {}
    ws = TdrWorkstation(
        run_id=payload.get("run_id", str(uuid4())),
        project_id=project_id,
        tenant_id=tenant_id,
    )
    ws.submit_artifacts(payload.get("artifacts", []))
    ws.score(payload.get("reviewer_scores", []))
    for c in payload.get("comments", []):
        ws.add_comment(**c)
    result = ws.finalize()
    path = out / f"tdr_{ws.run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    console.print_json(data=result)
    console.print(f"[green]TDR 决策已写入:[/green] {path}")


@tdr_app.command("keys")
def tdr_keys(out_dir: Path = typer.Option(Path("./output/keys"), "--out")) -> None:
    """Generate a fresh Ed25519 keypair for reviewer signatures."""
    from packages.tdr import generate_keypair
    from packages.tdr.signing import write_keypair

    try:
        kp = generate_keypair()
    except RuntimeError as exc:
        console.print(
            Panel.fit(
                f"[red]{exc}[/red]\n"
                "TDR 签名依赖 cryptography 库，请执行:\n"
                "  pip install 'cryptography>=42'",
                border_style="red",
                title="缺少可选依赖",
            )
        )
        raise typer.Exit(code=2) from exc
    priv, pub = write_keypair(kp, out_dir)
    console.print(f"[green]私钥:[/green] {priv}\n[green]公钥:[/green] {pub}")


# ---------------------------------------------------------------------
# Serve
# ---------------------------------------------------------------------

@app.command("serve")
def serve(
    host: str = typer.Option("0.0.0.0", "--host"),
    port: int = typer.Option(8080, "--port"),
    reload: bool = typer.Option(False, "--reload"),
) -> None:
    """Launch the FastAPI server (requires the `api` extra)."""
    try:
        import uvicorn
    except ImportError as exc:
        raise typer.BadParameter(
            "uvicorn 未安装。请执行：pip install -e '.[api]'"
        ) from exc
    uvicorn.run("apps.api.main:app", host=host, port=port, reload=reload)


# ---------------------------------------------------------------------
# Standards & version
# ---------------------------------------------------------------------

@app.command("standards")
def standards_show(
    which: str = typer.Argument("process", help="process | quality | data | tdr"),
) -> None:
    from packages.core.config import standards as S

    mapping = {
        "process": S.process,
        "quality": S.quality,
        "data": S.data,
        "tdr": S.tdr,
    }
    if which not in mapping:
        console.print(f"[red]未知标准：{which}[/red]")
        raise typer.Exit(code=2)
    console.print_json(data=mapping[which])


@app.command("version")
def version() -> None:
    from packages.core.config import standards as S

    console.print_json(
        data={
            "toolkit": "0.1.0",
            "sop_version": S.process.get("sop", {}).get("name"),
            "process_version": S.process.get("version"),
            "quality_version": S.quality.get("version"),
            "data_version": S.data.get("version"),
            "tdr_version": S.tdr.get("version"),
        }
    )


# ---------------------------------------------------------------------
# Prompts — list / show / export the SOP prompt library
# ---------------------------------------------------------------------

_STEP_DIRS = [
    ("step1_requirement", "Step 1 需求拆解"),
    ("step2_testcase", "Step 2 测试用例"),
    ("step4_api", "Step 4 接口测试"),
    ("step5_ui", "Step 5 UI 一致性"),
    ("step6_agent", "Step 6 Agent 执行"),
]


def _load_all_steps() -> list[tuple[str, str, Any]]:
    """Return list of (dir_name, display_name, PromptStep)."""
    from packages.core.prompts import load_step

    out = []
    for d, label in _STEP_DIRS:
        try:
            out.append((d, label, load_step(d)))
        except Exception as exc:
            console.print(f"[red]load {d} failed:[/red] {exc}")
    return out


@prompts_app.command("list")
def prompts_list() -> None:
    """打印 SOP 25 个提示词的总览（id / 名称 / 模型 / 占位符）。"""
    t = Table(title="SOP 提示词库", show_lines=False)
    t.add_column("Step", style="cyan", no_wrap=True)
    t.add_column("ID", style="bold")
    t.add_column("名称")
    t.add_column("模型", style="magenta")
    t.add_column("占位符", style="dim")

    for dir_name, label, step in _load_all_steps():
        for tpl_id in step.order:
            sub_id = tpl_id.replace("_", ".").rsplit(".md", 1)[0]
            tpl = next(
                (v for k, v in step.templates.items() if step_filename_matches(v, tpl_id)),
                None,
            )
            if tpl is None:
                continue
            t.add_row(
                label,
                tpl.id,
                tpl.name,
                tpl.model_tier.value,
                ", ".join(tpl.placeholders) if tpl.placeholders else "—",
            )
    console.print(t)


def step_filename_matches(tpl: Any, fname: str) -> bool:
    """Match prompt template to its source file by filename stem."""
    return tpl.path.name == fname


@prompts_app.command("show")
def prompts_show(
    sub_id: str = typer.Argument(..., help="子步骤 ID，如 step1.1 / step4.3"),
    raw: bool = typer.Option(False, "--raw", help="输出原始 markdown（含 frontmatter）"),
) -> None:
    """显示某个子步骤提示词的完整内容。"""
    from packages.core.prompts import load_step

    # Find which step dir owns this sub_id
    step_num = sub_id.split(".")[0].replace("step", "")
    dir_map = {
        "1": "step1_requirement", "2": "step2_testcase",
        "4": "step4_api", "5": "step5_ui", "6": "step6_agent",
    }
    if step_num not in dir_map:
        console.print(f"[red]未知 step：{sub_id}[/red]")
        raise typer.Exit(2)

    step = load_step(dir_map[step_num])
    try:
        tpl = step.get(sub_id)
    except KeyError:
        console.print(f"[red]找不到子步骤：{sub_id}[/red]")
        console.print(f"可用：{', '.join(step.templates.keys())}")
        raise typer.Exit(2)

    if raw:
        console.print(tpl.path.read_text(encoding="utf-8"))
        return

    # Pretty print: header table + body panel
    h = Table(show_header=False, box=None, padding=(0, 2))
    h.add_column("k", style="dim")
    h.add_column("v", style="bold")
    h.add_row("ID", tpl.id)
    h.add_row("名称", tpl.name)
    h.add_row("版本", tpl.version)
    h.add_row("模型", tpl.model_tier.value)
    h.add_row("temperature", str(tpl.temperature))
    h.add_row("max_tokens", str(tpl.max_tokens))
    h.add_row("output_format", tpl.output_format)
    h.add_row("output_schema", tpl.output_schema or "—")
    h.add_row("占位符", ", ".join(tpl.placeholders) if tpl.placeholders else "—")
    h.add_row("源文件", str(tpl.path))
    console.print(Panel.fit(h, title=f"[cyan]{tpl.id}[/cyan]", border_style="cyan"))
    console.print(Panel(tpl.body, title="提示词正文", border_style="dim"))
    if step.common_system_suffix:
        console.print(
            Panel(step.common_system_suffix, title=f"通用系统后缀（{step.step_id}）",
                  border_style="dim")
        )


@prompts_app.command("export")
def prompts_export(
    out_path: Path = typer.Option(
        Path("./output/prompts_handbook.md"), "--out",
        help="导出的 Markdown 文件路径",
    ),
) -> None:
    """把全部 25 个提示词导出成一份 Markdown 手册。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    parts: list[str] = []
    parts.append("# SOP 提示词手册（自动生成）\n")
    parts.append("> 来自 `configs/prompts/`，含 5 个 AI 步骤 × 5 个子步骤 = 25 个提示词。\n")
    parts.append("> 人工步骤（Step 3 / 7 / 8）不在自动化范围。\n\n")

    for dir_name, label, step in _load_all_steps():
        parts.append(f"## {label}\n")
        if step.common_system_suffix:
            parts.append("**通用系统后缀**\n\n```\n")
            parts.append(step.common_system_suffix)
            parts.append("\n```\n\n")
        for fname in step.order:
            tpl = next((v for v in step.templates.values() if v.path.name == fname), None)
            if tpl is None:
                continue
            parts.append(f"### {tpl.id} — {tpl.name}\n\n")
            parts.append(
                f"- 模型：`{tpl.model_tier.value}` | "
                f"temperature `{tpl.temperature}` | "
                f"max_tokens `{tpl.max_tokens}`\n"
            )
            ph = ", ".join(f"`{{{{{p}}}}}`" for p in tpl.placeholders) or "—"
            parts.append(f"- 占位符：{ph}\n")
            parts.append(f"- 输出：`{tpl.output_format}`")
            if tpl.output_schema:
                parts.append(f"，schema=`{tpl.output_schema}`")
            parts.append("\n\n")
            parts.append("```\n")
            parts.append(tpl.body)
            parts.append("\n```\n\n")
    out_path.write_text("".join(parts), encoding="utf-8")
    console.print(f"[green]导出完成：[/green]{out_path} （{out_path.stat().st_size:,} bytes）")


if __name__ == "__main__":
    app()
