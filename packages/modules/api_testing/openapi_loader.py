"""Minimal OpenAPI / Postman collection loader.

Not a full OpenAPI 3.1 implementation — only the pieces Step 4 needs:
  * method + path
  * operationId / summary
  * auth requirements (security schemes)
  * request schema (body / query / params)
  * response schema (status codes)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class Endpoint:
    operation_id: str
    method: str
    path: str
    summary: str | None = None
    auth_required: bool = False
    parameters: list[dict[str, Any]] = field(default_factory=list)
    request_body: dict[str, Any] | None = None
    responses: dict[str, dict[str, Any]] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)


def load_openapi(path: str | Path) -> list[Endpoint]:
    """Accept OpenAPI JSON/YAML or a Postman v2.1 collection."""
    raw = Path(path).read_text(encoding="utf-8")
    try:
        doc: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError:
        doc = yaml.safe_load(raw)

    if doc.get("info", {}).get("schema", "").startswith(
        "https://schema.getpostman.com"
    ):
        return _parse_postman(doc)
    return _parse_openapi(doc)


def _parse_openapi(doc: dict[str, Any]) -> list[Endpoint]:
    paths = doc.get("paths", {}) or {}
    global_security = bool(doc.get("security"))
    out: list[Endpoint] = []
    for path, ops in paths.items():
        for method, op in ops.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                continue
            security = op.get("security", None)
            auth = global_security if security is None else bool(security)
            out.append(
                Endpoint(
                    operation_id=op.get(
                        "operationId", f"{method.upper()} {path}"
                    ),
                    method=method.upper(),
                    path=path,
                    summary=op.get("summary") or op.get("description"),
                    auth_required=auth,
                    parameters=op.get("parameters", []),
                    request_body=op.get("requestBody"),
                    responses=op.get("responses", {}),
                    tags=op.get("tags", []),
                )
            )
    return out


def _parse_postman(doc: dict[str, Any]) -> list[Endpoint]:
    out: list[Endpoint] = []

    def walk(items: list[dict[str, Any]], folder: list[str]) -> None:
        for it in items:
            if "item" in it:
                walk(it["item"], folder + [it.get("name", "")])
                continue
            req = it.get("request") or {}
            method = (req.get("method") or "GET").upper()
            url = req.get("url")
            if isinstance(url, dict):
                path = "/" + "/".join(url.get("path", []))
            else:
                path = str(url or "/")
            out.append(
                Endpoint(
                    operation_id=it.get("name", f"{method} {path}"),
                    method=method,
                    path=path,
                    summary=it.get("name"),
                    auth_required=bool(req.get("auth")),
                    parameters=[],
                    request_body=req.get("body"),
                    responses={},
                    tags=folder,
                )
            )

    walk(doc.get("item", []), [])
    return out
