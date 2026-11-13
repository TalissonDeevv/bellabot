from __future__ import annotations


class RiskEngine:
    """Centraliza critérios contextuais de severidade."""

    ORDER = {"INFO": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}

    @classmethod
    def role_administrator(
        cls,
        *,
        is_everyone: bool,
        managed: bool,
        authorized: bool,
        member_count: int,
    ) -> str:
        if is_everyone:
            return "CRITICAL"
        if managed:
            return "MEDIUM"
        if not authorized and member_count >= 10:
            return "CRITICAL"
        if not authorized and member_count >= 3:
            return "HIGH"
        return "MEDIUM"

    @classmethod
    def control_plane_combination(cls, *, member_count: int, authorized: bool, permission_count: int) -> str:
        if not authorized and member_count >= 10 and permission_count >= 3:
            return "CRITICAL"
        if not authorized and member_count >= 3:
            return "HIGH"
        return "MEDIUM"

    @classmethod
    def bot_privileges(cls, permissions: set[str], *, is_bella: bool) -> str:
        if is_bella:
            return "LOW"
        if "administrator" in permissions:
            return "HIGH"
        if len(permissions & {"manage_roles", "manage_channels", "manage_webhooks", "manage_guild"}) >= 2:
            return "MEDIUM"
        return "LOW"
