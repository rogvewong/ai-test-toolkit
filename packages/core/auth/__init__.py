"""User account system — separate from the Claude credential layer.

`packages/core/auth_config.py` 管理 toolkit 自己怎么调 Claude (OAuth / API Key);
本包管理 web 用户登录 (邮箱 + 密码 + session)。两层互不影响:
所有用户共用 admin 配的那一份 Claude 凭据,但每个 web 用户有独立工作区。
"""
from packages.core.auth.user_store import (
    UserStore,
    user_store,
    UserRecord,
    SessionRecord,
)

__all__ = ["UserStore", "user_store", "UserRecord", "SessionRecord"]
