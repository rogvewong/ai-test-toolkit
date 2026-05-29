"""Markdown exporter for the report bundle (ideal for PR comments, Wiki dumps)."""
from __future__ import annotations

from packages.reporting.aggregator import ReportBundle


def render_markdown(bundle: ReportBundle) -> str:
    lines: list[str] = []
    lines.append(f"# AI Test Toolkit — Run `{bundle.run_id}`")
    lines.append("")
    lines.append(f"- Project: **{bundle.project_id}**")
    lines.append(f"- Tenant: **{bundle.tenant_id}**")
    lines.append("")

    lines.append("## Gate Decisions")
    lines.append("")
    lines.append("| Step | Action | Next | Reasons |")
    lines.append("| --- | --- | --- | --- |")
    for g in bundle.gates():
        reasons = "<br>".join(g.get("reasons", [])) or "-"
        lines.append(
            f"| {g['step']} | {g['action']} | {g.get('next_step', '-')} | {reasons} |"
        )
    lines.append("")

    lines.append("## Confidence")
    lines.append("")
    lines.append("| Step | Score | Grade |")
    lines.append("| --- | --- | --- |")
    for c in bundle.confidence():
        lines.append(
            f"| {c['step']} | {float(c.get('score', 0)):.2f} | {c.get('grade', '?')} |"
        )
    lines.append("")

    d = bundle.defect_totals()
    lines.append("## Defect Totals")
    lines.append("")
    lines.append("| Critical | High | Medium | Low |")
    lines.append("| --- | --- | --- | --- |")
    lines.append(f"| {d['critical']} | {d['high']} | {d['medium']} | {d['low']} |")
    lines.append("")

    if bundle.step6 and bundle.step6.get("metrics"):
        m = bundle.step6["metrics"]
        lines.append("## Execution Summary")
        lines.append("")
        lines.append(
            f"- Success rate: **{float(m.get('success_rate', 0)):.2f}**"
        )
        lines.append(f"- Total: {int(m.get('total', 0))}")
        lines.append(f"- Failed: {int(m.get('failed', 0))}")
        lines.append("")

    if bundle.module_outputs:
        lines.append("## Module Runs")
        lines.append("")
        lines.append("| Module | Metrics | Findings |")
        lines.append("| --- | --- | --- |")
        for name, out in bundle.module_outputs.items():
            metrics = ", ".join(
                f"{k}={v}" for k, v in (out.get("metrics") or {}).items()
            )
            findings = len(out.get("findings") or [])
            lines.append(f"| {name} | {metrics or '-'} | {findings} |")
        lines.append("")

    return "\n".join(lines)
