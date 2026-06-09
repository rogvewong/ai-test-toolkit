#!/usr/bin/env python3
"""从花名册 JSON 批量建普通用户(首登强制改密)。幂等:已存在则跳过。

用法(容器内执行,落到容器的 AITK_DATA_DIR/users.db):
    python scripts/seed_users_from_roster.py /tmp/roster_seed.json [初始密码]

JSON 格式: [{"username": "dn2612", "display_name": "夏桑", ...}, ...]
- username  → 登录账号(归一化小写)
- password  → 统一用初始密码(默认 123456),must_change_password=1 强制首登改密
- role      → 一律 user(普通用户)

注意:不覆盖任何已存在账号的凭据(已存在即跳过)。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "/app")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from packages.core.auth.user_store import user_store  # noqa: E402


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/roster_seed.json"
    init_pwd = sys.argv[2] if len(sys.argv) > 2 else "123456"
    seed = json.loads(Path(path).read_text(encoding="utf-8"))

    created: list[str] = []
    exists: list[str] = []
    failed: list[tuple[str, str]] = []
    for s in seed:
        uname = (s.get("username") or "").strip().lower()
        if not uname:
            continue
        try:
            user_store.create_user(
                username=uname,
                password=init_pwd,
                display_name=(s.get("display_name") or uname),
                role="user",
                must_change=True,
            )
            created.append(uname)
        except ValueError as exc:
            if "已存在" in str(exc):
                exists.append(uname)
            else:
                failed.append((uname, str(exc)))

    print(f"输入: {len(seed)}  创建成功: {len(created)}  已存在跳过: {len(exists)}  失败: {len(failed)}")
    if failed:
        print("失败明细(前10):", failed[:10])
    print(f"当前总用户数: {user_store.count_users()}")
    # 抽查 3 个:确认 role=user 且 must_change=1
    for uname in (created[:3] or exists[:3]):
        r = user_store.get_user_by_username(uname)
        if r:
            print(f"  抽查 {uname}: role={r.role} must_change={r.must_change_password} display={r.display_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
