"""User + session store. SQLite + bcrypt, runs out of process.

Storage location:
    $AITK_DATA_DIR/users.db  (Linux/Docker default: /data/users.db)

Schema:
    users     (id, username, password_hash, display_name, role, created_at, last_login_at)
    sessions  (token, user_id, created_at, expires_at, user_agent)

Roles:
    "admin"  — 第一个建的用户(seed/首次注册)。可以管别人账号 + 配 Claude 凭据
    "user"   — 普通用户。只能看自己的报告

Username 规则:
    3-40 字符,允许字母/数字/下划线/点/横杠/@,放宽以兼容邮箱
"""
from __future__ import annotations

import os
import re
import secrets
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any

import bcrypt


# ---------- data dir resolution ----------

def _resolve_data_dir() -> Path:
    explicit = os.environ.get("AITK_DATA_DIR")
    if explicit:
        p = Path(explicit).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        return p
    home = Path.home()
    if sys.platform == "darwin":
        base = home / "Library" / "Application Support" / "AITestToolkit"
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) / "AITestToolkit" if appdata else home / "AITestToolkit"
    else:
        xdg = os.environ.get("XDG_DATA_HOME")
        base = Path(xdg) / "AITestToolkit" if xdg else home / ".local" / "share" / "AITestToolkit"
    base.mkdir(parents=True, exist_ok=True)
    return base


DATA_DIR = _resolve_data_dir()
DB_PATH = DATA_DIR / "users.db"

SESSION_TTL_SECONDS = 30 * 24 * 3600
SESSION_COOKIE_NAME = "aitk_session"

# 用户名校验:3-40 字符,允许字母/数字/下划线/点/横杠/@(@ 让邮箱也能通过)
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-@]{3,40}$")


# ---------- records ----------

@dataclass(slots=True)
class UserRecord:
    id: int
    username: str
    display_name: str
    role: str
    created_at: float
    last_login_at: float | None
    must_change_password: bool = False

    def is_admin(self) -> bool:
        # 管理员或超级管理员都算 admin(用于「用户管理」类门禁)
        return self.role in ("admin", "superadmin")

    def is_superadmin(self) -> bool:
        return self.role == "superadmin"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name,
            "role": self.role,
            "created_at": self.created_at,
            "last_login_at": self.last_login_at,
            "must_change_password": self.must_change_password,
        }


@dataclass(slots=True)
class SessionRecord:
    token: str
    user_id: int
    created_at: float
    expires_at: float


# ---------- store ----------

class UserStore:
    """线程安全的 SQLite 用户/会话存储 (单进程足够,不需要连接池)。"""

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path
        self._lock = RLock()
        self._init()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self.db_path), isolation_level=None, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA foreign_keys=ON;")
        return c

    def _init(self) -> None:
        with self._lock, self._conn() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT 'user',
                created_at REAL NOT NULL,
                last_login_at REAL,
                must_change_password INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                user_agent TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_exp ON sessions(expires_at);
            """)
            # 迁移:给老库补 must_change_password 列(幂等;列已存在则忽略)
            try:
                c.execute(
                    "ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0"
                )
            except sqlite3.OperationalError:
                pass

    # ----- users -----

    @staticmethod
    def _normalize_username(s: str) -> str:
        return (s or "").strip().lower()

    @staticmethod
    def _validate_username(s: str) -> None:
        if not s or not _USERNAME_RE.match(s):
            raise ValueError(
                "用户名只能包含字母/数字/下划线/点/横杠/@,长度 3-40"
            )

    def count_users(self) -> int:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()
            return int(row["n"])

    def create_user(
        self,
        username: str,
        password: str,
        display_name: str = "",
        role: str | None = None,
        must_change: bool = False,
    ) -> UserRecord:
        """创建用户。第一个创建的自动成为超级管理员。
        must_change=True → 该用户首次登录后被强制修改密码(用于批量发初始密码)。
        """
        username = self._normalize_username(username)
        self._validate_username(username)
        if len(password) < 6:
            raise ValueError("密码至少 6 位")
        pwd_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        actual_role = role if role in ("user", "admin", "superadmin") else (
            "superadmin" if self.count_users() == 0 else "user"
        )
        now = time.time()
        with self._lock, self._conn() as c:
            try:
                cur = c.execute(
                    "INSERT INTO users(username, password_hash, display_name, role, created_at, must_change_password) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (username, pwd_hash, display_name or username, actual_role, now, 1 if must_change else 0),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("该用户名已存在") from exc
            uid = int(cur.lastrowid or 0)
        return self.get_user(uid)  # type: ignore[return-value]

    def get_user(self, user_id: int) -> UserRecord | None:
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT id, username, display_name, role, created_at, last_login_at, must_change_password "
                "FROM users WHERE id=?", (user_id,)
            ).fetchone()
            return self._row_to_user(row) if row else None

    def get_user_by_username(self, username: str) -> UserRecord | None:
        username = self._normalize_username(username)
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT id, username, display_name, role, created_at, last_login_at, must_change_password "
                "FROM users WHERE username=?", (username,)
            ).fetchone()
            return self._row_to_user(row) if row else None

    def verify_password(self, username: str, password: str) -> UserRecord | None:
        username = self._normalize_username(username)
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT id, username, password_hash, display_name, role, created_at, last_login_at, must_change_password "
                "FROM users WHERE username=?", (username,)
            ).fetchone()
            if not row:
                return None
            try:
                ok = bcrypt.checkpw(password.encode("utf-8"), row["password_hash"].encode("utf-8"))
            except Exception:
                return None
            if not ok:
                return None
            c.execute("UPDATE users SET last_login_at=? WHERE id=?", (time.time(), row["id"]))
            return self._row_to_user(row)

    def change_password(self, user_id: int, new_password: str) -> None:
        if len(new_password) < 6:
            raise ValueError("密码至少 6 位")
        h = bcrypt.hashpw(new_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        with self._lock, self._conn() as c:
            # 改密同时清除「强制改密」标记
            c.execute(
                "UPDATE users SET password_hash=?, must_change_password=0 WHERE id=?",
                (h, user_id),
            )
            # 撤销该用户所有现有 session — 改密后必须重新登录
            c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))

    def admin_reset_password(self, user_id: int, new_password: str) -> None:
        """admin 给用户重置密码 — 不验旧密码。"""
        self.change_password(user_id, new_password)

    def set_role(self, user_id: int, role: str) -> bool:
        """改用户角色。role ∈ {user, admin, superadmin}。改角色后撤销其会话以重置权限。"""
        if role not in ("user", "admin", "superadmin"):
            raise ValueError("非法角色")
        with self._lock, self._conn() as c:
            cur = c.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))
            if (cur.rowcount or 0) > 0:
                c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
                return True
            return False

    def count_by_role(self, role: str) -> int:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT COUNT(*) AS n FROM users WHERE role=?", (role,)).fetchone()
            return int(row["n"])

    def delete_user(self, user_id: int) -> bool:
        with self._lock, self._conn() as c:
            cur = c.execute("DELETE FROM users WHERE id=?", (user_id,))
            return (cur.rowcount or 0) > 0

    def list_users(self) -> list[UserRecord]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT id, username, display_name, role, created_at, last_login_at, must_change_password "
                "FROM users ORDER BY id"
            ).fetchall()
            return [self._row_to_user(r) for r in rows]

    def _row_to_user(self, row: sqlite3.Row) -> UserRecord:
        keys = row.keys()
        mcp = bool(row["must_change_password"]) if "must_change_password" in keys else False
        return UserRecord(
            id=int(row["id"]),
            username=str(row["username"]),
            display_name=str(row["display_name"] or ""),
            role=str(row["role"] or "user"),
            created_at=float(row["created_at"]),
            last_login_at=float(row["last_login_at"]) if row["last_login_at"] is not None else None,
            must_change_password=mcp,
        )

    # ----- sessions -----

    def create_session(self, user_id: int, user_agent: str = "") -> SessionRecord:
        token = secrets.token_urlsafe(32)
        now = time.time()
        expires = now + SESSION_TTL_SECONDS
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO sessions(token, user_id, created_at, expires_at, user_agent) "
                "VALUES (?, ?, ?, ?, ?)",
                (token, user_id, now, expires, (user_agent or "")[:240]),
            )
        return SessionRecord(token=token, user_id=user_id, created_at=now, expires_at=expires)

    def resolve_session(self, token: str) -> UserRecord | None:
        if not token:
            return None
        now = time.time()
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT s.expires_at, u.id, u.username, u.display_name, u.role, "
                "       u.created_at, u.last_login_at, u.must_change_password "
                "FROM sessions s JOIN users u ON s.user_id = u.id "
                "WHERE s.token = ?",
                (token,),
            ).fetchone()
            if not row:
                return None
            if float(row["expires_at"]) < now:
                c.execute("DELETE FROM sessions WHERE token=?", (token,))
                return None
            return UserRecord(
                id=int(row["id"]),
                username=str(row["username"]),
                display_name=str(row["display_name"] or ""),
                role=str(row["role"] or "user"),
                created_at=float(row["created_at"]),
                last_login_at=float(row["last_login_at"]) if row["last_login_at"] is not None else None,
                must_change_password=bool(row["must_change_password"]),
            )

    def delete_session(self, token: str) -> None:
        if not token:
            return
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM sessions WHERE token=?", (token,))

    def cleanup_expired_sessions(self) -> int:
        with self._lock, self._conn() as c:
            cur = c.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
            return cur.rowcount or 0


user_store = UserStore()
