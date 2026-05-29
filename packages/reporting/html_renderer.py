"""Jinja2-backed HTML rendering for the aggregated report bundle."""
from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    from jinja2 import Environment, FileSystemLoader, select_autoescape
except ImportError:  # pragma: no cover
    Environment = None  # type: ignore[assignment]

from packages.reporting.aggregator import ReportBundle

TEMPLATE_DIR = Path(__file__).parent / "templates"


_FALLBACK_TEMPLATE = """<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>AI Test Toolkit — Run {{ bundle.run_id }}</title>
  <style>
    body { font-family: -apple-system, Segoe UI, sans-serif; margin: 2rem; color: #1f2328; }
    h1 { border-bottom: 1px solid #d0d7de; padding-bottom: .5rem; }
    h2 { margin-top: 2rem; }
    table { border-collapse: collapse; width: 100%; }
    th, td { border: 1px solid #d0d7de; padding: .4rem .6rem; text-align: left; }
    th { background: #f6f8fa; }
    .pass { color: #116329; font-weight: bold; }
    .reject { color: #a40e26; font-weight: bold; }
    .warn { color: #9a6700; font-weight: bold; }
    .critical { background: #ffebe9; }
    .high { background: #fff1e5; }
  </style>
</head>
<body>
  <h1>AI Test Toolkit — Run {{ bundle.run_id }}</h1>
  <p><b>Project:</b> {{ bundle.project_id }} &middot; <b>Tenant:</b> {{ bundle.tenant_id }}</p>

  <h2>Gate Decisions</h2>
  <table>
    <tr><th>Step</th><th>Action</th><th>Reasons</th><th>Next</th></tr>
    {% for g in gates %}
      <tr class="{{ g.action }}">
        <td>{{ g.step }}</td>
        <td class="{{ 'pass' if g.action == 'pass' else ('reject' if 'reject' in g.action else 'warn') }}">{{ g.action }}</td>
        <td>{{ ', '.join(g.get('reasons', [])) }}</td>
        <td>{{ g.get('next_step') or '-' }}</td>
      </tr>
    {% endfor %}
  </table>

  <h2>Confidence</h2>
  <table>
    <tr><th>Step</th><th>Score</th><th>Grade</th></tr>
    {% for c in confidences %}
      <tr><td>{{ c.step }}</td><td>{{ '%.2f'|format(c.score) }}</td><td>{{ c.grade }}</td></tr>
    {% endfor %}
  </table>

  <h2>Defect Totals</h2>
  <table>
    <tr><th>Critical</th><th>High</th><th>Medium</th><th>Low</th></tr>
    <tr class="{{ 'critical' if defects.critical else '' }}">
      <td>{{ defects.critical }}</td><td>{{ defects.high }}</td>
      <td>{{ defects.medium }}</td><td>{{ defects.low }}</td>
    </tr>
  </table>

  {% if bundle.step6 %}
  <h2>Execution Summary</h2>
  <p>Success rate: <b>{{ '%.2f'|format(bundle.step6.metrics.success_rate or 0) }}</b> &middot;
     Failures: {{ bundle.step6.metrics.failed or 0 }}</p>
  {% endif %}

  {% if bundle.module_outputs %}
  <h2>Module Runs</h2>
  <table>
    <tr><th>Module</th><th>Metrics</th><th>Findings</th></tr>
    {% for name, out in bundle.module_outputs.items() %}
      <tr>
        <td>{{ name }}</td>
        <td>{{ out.get('metrics', {}) }}</td>
        <td>{{ out.get('findings', []) | length }}</td>
      </tr>
    {% endfor %}
  </table>
  {% endif %}
</body>
</html>
"""


def render_html(bundle: ReportBundle, *, template_name: str | None = None) -> str:
    ctx: dict[str, Any] = {
        "bundle": bundle,
        "gates": bundle.gates(),
        "confidences": bundle.confidence(),
        "defects": bundle.defect_totals(),
    }
    if Environment is None:
        # Extremely minimal templating fallback (not production-safe, but works)
        return _render_fallback(ctx)
    if template_name and (TEMPLATE_DIR / template_name).exists():
        env = Environment(
            loader=FileSystemLoader(str(TEMPLATE_DIR)),
            autoescape=select_autoescape(["html", "xml"]),
        )
        return env.get_template(template_name).render(**ctx)
    env = Environment(autoescape=select_autoescape(["html", "xml"]))
    return env.from_string(_FALLBACK_TEMPLATE).render(**ctx)


def _render_fallback(ctx: dict[str, Any]) -> str:
    lines = [f"<h1>Run {ctx['bundle'].run_id}</h1>"]
    lines.append("<h2>Gates</h2><ul>")
    for g in ctx["gates"]:
        lines.append(f"<li>{g['step']} → {g['action']}</li>")
    lines.append("</ul>")
    return "\n".join(lines)
