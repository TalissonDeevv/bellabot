from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin

from dotenv import load_dotenv

from .identity import OWNER_DISCORD_ID


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "sim", "on"}


def _as_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} precisa ser um numero inteiro.") from exc


def _as_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return float(value.replace(",", "."))
    except ValueError as exc:
        raise ValueError(f"{name} precisa ser um numero decimal.") from exc


def _as_optional_int(name: str) -> int | None:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} precisa ser um numero inteiro.") from exc


def _as_csv(name: str, default: list[str] | None = None) -> list[str]:
    value = os.getenv(name)
    if value is None:
        return list(default or [])
    return [item.strip() for item in value.split(",") if item.strip()]


def _as_status_codes(name: str, default: list[int]) -> set[int]:
    values = _as_csv(name, [str(item) for item in default])
    status_codes: set[int] = set()
    for value in values:
        try:
            status_codes.add(int(value))
        except ValueError as exc:
            raise ValueError(f"{name} contem um status HTTP invalido: {value}") from exc
    return status_codes


def _as_int_set(name: str) -> set[int]:
    values = _as_csv(name)
    result: set[int] = set()
    for value in values:
        try:
            result.add(int(value))
        except ValueError as exc:
            raise ValueError(f"{name} contem um ID invalido: {value}") from exc
    return result


@dataclass(frozen=True)
class Settings:
    discord_token: str
    discord_channel_id: int | None
    site_url: str
    site_verify_key: str
    site_verify_meta_name: str
    check_interval_seconds: int
    request_timeout_seconds: int
    expected_status_codes: set[int]
    max_response_ms: int
    alert_cooldown_seconds: int
    alert_reminder_seconds: int
    report_on_startup: bool
    report_interval_seconds: int
    verify_tls: bool
    tls_warning_days: int
    content_must_contain: str
    content_must_not_contain: list[str]
    watch_content_changes: bool
    content_change_alert_cooldown_seconds: int
    security_headers: list[str]
    access_log_path: Path | None
    log_scan_interval_seconds: int
    log_burst_window_seconds: int
    log_error_threshold: int
    log_suspicious_threshold: int
    log_ip_threshold: int
    bot_command_prefix: str
    message_content_intent: bool
    members_intent: bool
    admin_role_ids: set[int]
    talisson_user_id: int
    server_invite_url: str
    admin_log_channel_id: int | None
    security_log_channel_id: int | None
    audit_log_channel_id: int | None
    technical_channel_id: int | None
    feedbacks_url: str
    feedback_channel_id: int | None
    feedback_check_interval_seconds: int
    change_channel_id: int | None
    change_check_interval_seconds: int
    site_repository: str
    github_token: str
    report_channel_id: int | None
    pages_to_monitor: list[str]
    render_dynamic_pages: bool
    browser_headless: bool
    knowledge_enabled: bool
    knowledge_channel_ids: set[int]
    knowledge_refresh_interval_seconds: int
    knowledge_min_confidence: float
    knowledge_match_min: float
    question_intent_min: float
    knowledge_user_cooldown_seconds: int
    knowledge_max_message_length: int
    knowledge_pages: list[str]
    knowledge_auto_discover: bool
    knowledge_max_pages: int
    discord_knowledge_enabled: bool
    discord_knowledge_channel_ids: set[int]
    discord_knowledge_refresh_seconds: int
    discord_knowledge_max_channels: int
    discord_knowledge_messages_per_channel: int
    groq_enabled: bool
    groq_api_key: str = field(repr=False)
    groq_model: str
    groq_vision_model: str
    groq_timeout_seconds: int
    ai_min_confidence: float
    ai_public_enabled: bool
    ai_public_require_mention: bool
    ai_public_min_confidence: float
    ai_allowed_guild_ids: set[int]
    ai_context_messages: int
    ai_context_ttl_minutes: int
    ai_max_response_chars: int
    weekly_report_enabled: bool
    weekly_report_channel_id: int | None
    weekly_report_weekday: int
    weekly_report_hour: int
    weekly_report_minute: int
    weekly_report_timezone: str
    default_server_statistics_channel_id: int | None
    default_site_statistics_channel_id: int | None
    daily_report_enabled: bool
    daily_report_hour: int
    daily_report_minute: int
    monthly_report_enabled: bool
    monthly_report_day: int
    monthly_report_hour: int
    monthly_report_minute: int
    traffic_log_path: Path | None
    traffic_scan_interval_seconds: int
    traffic_session_minutes: int
    traffic_retention_days: int
    traffic_max_events: int
    traffic_hosts: list[str]
    traffic_ingest_enabled: bool
    traffic_ingest_host: str
    traffic_ingest_port: int
    traffic_ingest_path: str
    traffic_signature_secret: str
    traffic_ingest_max_bytes: int
    ga4_property_id: str
    ga4_credentials_path: Path | None
    ga4_cache_minutes: int
    voice_auto_connect: bool
    voice_channel_id: int | None
    voice_self_deaf: bool
    voice_self_mute: bool
    voice_reconnect_interval_seconds: int
    welcome_enabled: bool
    welcome_channel_id: int | None
    welcome_delete_after_seconds: int


def load_settings() -> Settings:
    load_dotenv(encoding="utf-8-sig", override=True)

    discord_token = os.getenv("DISCORD_TOKEN", "").strip()
    if not discord_token or discord_token == "coloque_o_token_do_bot_aqui":
        raise ValueError("Preencha DISCORD_TOKEN no arquivo .env.")
    if discord_token.lower().startswith("bot "):
        raise ValueError("DISCORD_TOKEN deve conter apenas o token puro, sem o prefixo 'Bot '.")
    if any(char.isspace() for char in discord_token):
        raise ValueError("DISCORD_TOKEN não pode conter espaços, quebras de linha ou tabulações.")

    site_url = os.getenv("SITE_URL", "").strip()
    if not site_url or site_url == "https://seudominio.com":
        raise ValueError("Preencha SITE_URL no arquivo .env.")
    if not site_url.startswith(("http://", "https://")):
        raise ValueError("SITE_URL precisa comecar com http:// ou https://.")

    raw_log_path = os.getenv("ACCESS_LOG_PATH", "").strip()
    configured_traffic_path = os.getenv("TRAFFIC_LOG_PATH", "").strip()
    raw_traffic_log_path = (
        configured_traffic_path
        or raw_log_path
        or str(Path("data") / "traffic.ndjson")
    )
    site_base_url = site_url.rstrip("/") + "/"
    default_pages = [
        "/",
        "/sobre",
        "/feedbacks",
        "/faq",
        "/contato",
        "/politica-de-privacidade",
    ]
    page_values = _as_csv("PAGES_TO_MONITOR", default_pages)
    pages_to_monitor = [
        value
        if value.startswith(("http://", "https://"))
        else urljoin(site_base_url, value.lstrip("/"))
        for value in page_values
    ]
    knowledge_page_values = _as_csv("KNOWLEDGE_PAGES", default_pages)
    knowledge_pages = [
        value
        if value.startswith(("http://", "https://"))
        else urljoin(site_base_url, value.lstrip("/"))
        for value in knowledge_page_values
    ]
    knowledge_min_confidence = _as_float("KNOWLEDGE_MIN_CONFIDENCE", 0.90)
    knowledge_match_min = _as_float("KNOWLEDGE_MATCH_MIN", 0.78)
    question_intent_min = _as_float("QUESTION_INTENT_MIN", 0.90)
    ai_min_confidence = _as_float("AI_MIN_CONFIDENCE", 0.90)
    ai_public_min_confidence = _as_float("AI_PUBLIC_MIN_CONFIDENCE", 0.72)
    for name, value in (
        ("KNOWLEDGE_MIN_CONFIDENCE", knowledge_min_confidence),
        ("KNOWLEDGE_MATCH_MIN", knowledge_match_min),
        ("QUESTION_INTENT_MIN", question_intent_min),
        ("AI_MIN_CONFIDENCE", ai_min_confidence),
        ("AI_PUBLIC_MIN_CONFIDENCE", ai_public_min_confidence),
    ):
        if not 0 <= value <= 1:
            raise ValueError(f"{name} precisa estar entre 0 e 1.")
    weekly_weekday = _as_int("WEEKLY_REPORT_WEEKDAY", 0)
    weekly_hour = _as_int("WEEKLY_REPORT_HOUR", 9)
    weekly_minute = _as_int("WEEKLY_REPORT_MINUTE", 0)
    daily_hour = _as_int("DAILY_REPORT_HOUR", 8)
    daily_minute = _as_int("DAILY_REPORT_MINUTE", 0)
    monthly_day = _as_int("MONTHLY_REPORT_DAY", 1)
    monthly_hour = _as_int("MONTHLY_REPORT_HOUR", 9)
    monthly_minute = _as_int("MONTHLY_REPORT_MINUTE", 0)
    traffic_ingest_enabled = _as_bool(
        os.getenv("TRAFFIC_INGEST_ENABLED"),
        False,
    )
    traffic_signature_secret = os.getenv(
        "TRAFFIC_SIGNATURE_SECRET",
        "",
    ).strip()
    traffic_ingest_path = os.getenv(
        "TRAFFIC_INGEST_PATH",
        "/vercel-analytics",
    ).strip() or "/vercel-analytics"
    groq_api_key = os.getenv("GROQ_API_KEY", "").strip()
    groq_enabled = _as_bool(
        os.getenv("GROQ_ENABLED"),
        True,
    )
    raw_ga4_credentials = os.getenv(
        "GOOGLE_APPLICATION_CREDENTIALS",
        "",
    ).strip()
    if not traffic_ingest_path.startswith("/"):
        raise ValueError("TRAFFIC_INGEST_PATH precisa começar com /.")
    if traffic_ingest_enabled and not traffic_signature_secret:
        raise ValueError(
            "Preencha TRAFFIC_SIGNATURE_SECRET ao ativar TRAFFIC_INGEST_ENABLED."
        )
    if weekly_weekday not in range(7):
        raise ValueError("WEEKLY_REPORT_WEEKDAY precisa estar entre 0 e 6.")
    if weekly_hour not in range(24):
        raise ValueError("WEEKLY_REPORT_HOUR precisa estar entre 0 e 23.")
    if weekly_minute not in range(60):
        raise ValueError("WEEKLY_REPORT_MINUTE precisa estar entre 0 e 59.")
    if daily_hour not in range(24):
        raise ValueError("DAILY_REPORT_HOUR precisa estar entre 0 e 23.")
    if daily_minute not in range(60):
        raise ValueError("DAILY_REPORT_MINUTE precisa estar entre 0 e 59.")
    if monthly_day not in range(1, 29):
        raise ValueError("MONTHLY_REPORT_DAY precisa estar entre 1 e 28.")
    if monthly_hour not in range(24):
        raise ValueError("MONTHLY_REPORT_HOUR precisa estar entre 0 e 23.")
    if monthly_minute not in range(60):
        raise ValueError("MONTHLY_REPORT_MINUTE precisa estar entre 0 e 59.")

    return Settings(
        discord_token=discord_token,
        discord_channel_id=_as_optional_int("DISCORD_CHANNEL_ID"),
        site_url=site_url,
        site_verify_key=os.getenv("SITE_VERIFY_KEY", "").strip(),
        site_verify_meta_name=os.getenv(
            "SITE_VERIFY_META_NAME", "site-sentinel-key"
        ).strip()
        or "site-sentinel-key",
        check_interval_seconds=max(10, _as_int("CHECK_INTERVAL_SECONDS", 60)),
        request_timeout_seconds=max(3, _as_int("REQUEST_TIMEOUT_SECONDS", 15)),
        expected_status_codes=_as_status_codes(
            "EXPECTED_STATUS_CODES", [200, 301, 302]
        ),
        max_response_ms=max(100, _as_int("MAX_RESPONSE_MS", 2500)),
        alert_cooldown_seconds=max(0, _as_int("ALERT_COOLDOWN_SECONDS", 300)),
        alert_reminder_seconds=max(0, _as_int("ALERT_REMINDER_SECONDS", 21600)),
        report_on_startup=_as_bool(os.getenv("REPORT_ON_STARTUP"), True),
        report_interval_seconds=max(300, _as_int("REPORT_INTERVAL_SECONDS", 3600)),
        verify_tls=_as_bool(os.getenv("VERIFY_TLS"), True),
        tls_warning_days=max(1, _as_int("TLS_WARNING_DAYS", 14)),
        content_must_contain=os.getenv("CONTENT_MUST_CONTAIN", "").strip(),
        content_must_not_contain=_as_csv(
            "CONTENT_MUST_NOT_CONTAIN",
            ["hacked", "defaced", "pwned", "invadido"],
        ),
        watch_content_changes=_as_bool(os.getenv("WATCH_CONTENT_CHANGES"), False),
        content_change_alert_cooldown_seconds=max(
            60,
            _as_int("CONTENT_CHANGE_ALERT_COOLDOWN_SECONDS", 3600),
        ),
        security_headers=_as_csv(
            "SECURITY_HEADERS",
            ["Strict-Transport-Security", "Content-Security-Policy", "X-Frame-Options"],
        ),
        access_log_path=Path(raw_log_path).expanduser() if raw_log_path else None,
        log_scan_interval_seconds=max(10, _as_int("LOG_SCAN_INTERVAL_SECONDS", 60)),
        log_burst_window_seconds=max(60, _as_int("LOG_BURST_WINDOW_SECONDS", 300)),
        log_error_threshold=max(1, _as_int("LOG_ERROR_THRESHOLD", 25)),
        log_suspicious_threshold=max(1, _as_int("LOG_SUSPICIOUS_THRESHOLD", 10)),
        log_ip_threshold=max(1, _as_int("LOG_IP_THRESHOLD", 40)),
        bot_command_prefix=os.getenv("BOT_COMMAND_PREFIX", "!").strip() or "!",
        message_content_intent=_as_bool(os.getenv("MESSAGE_CONTENT_INTENT"), True),
        members_intent=_as_bool(os.getenv("MEMBERS_INTENT"), True),
        admin_role_ids=_as_int_set("ADMIN_ROLE_IDS")
        or {1467982180469833979, 1504605819923206204},
        talisson_user_id=_as_int("TALISSON_USER_ID", OWNER_DISCORD_ID),
        server_invite_url=os.getenv(
            "SERVER_INVITE_URL",
            "https://discord.gg/FcdCGU5mQb",
        ).strip()
        or "https://discord.gg/FcdCGU5mQb",
        admin_log_channel_id=_as_optional_int("ADMIN_LOG_CHANNEL_ID"),
        security_log_channel_id=_as_optional_int("SECURITY_LOG_CHANNEL_ID"),
        audit_log_channel_id=_as_optional_int("AUDIT_LOG_CHANNEL_ID"),
        technical_channel_id=_as_optional_int("TECHNICAL_CHANNEL_ID"),
        feedbacks_url=os.getenv(
            "FEEDBACKS_URL", urljoin(site_base_url, "feedbacks")
        ).strip()
        or urljoin(site_base_url, "feedbacks"),
        feedback_channel_id=_as_optional_int("FEEDBACK_CHANNEL_ID"),
        feedback_check_interval_seconds=max(
            60,
            _as_int("FEEDBACK_CHECK_INTERVAL_SECONDS", 300),
        ),
        change_channel_id=_as_optional_int("CHANGE_CHANNEL_ID"),
        change_check_interval_seconds=max(
            120,
            _as_int("CHANGE_CHECK_INTERVAL_SECONDS", 600),
        ),
        site_repository=os.getenv(
            "SITE_REPOSITORY",
            "TalissonDeevv/talisson-souza-showcase-0d8b5f01",
        ).strip(),
        github_token=os.getenv("GITHUB_TOKEN", "").strip(),
        report_channel_id=_as_optional_int("REPORT_CHANNEL_ID"),
        pages_to_monitor=pages_to_monitor,
        render_dynamic_pages=_as_bool(os.getenv("RENDER_DYNAMIC_PAGES"), True),
        browser_headless=_as_bool(os.getenv("BROWSER_HEADLESS"), True),
        knowledge_enabled=_as_bool(os.getenv("KNOWLEDGE_ENABLED"), True),
        knowledge_channel_ids=_as_int_set("KNOWLEDGE_CHANNEL_IDS"),
        knowledge_refresh_interval_seconds=max(
            900,
            _as_int("KNOWLEDGE_REFRESH_INTERVAL_SECONDS", 21600),
        ),
        knowledge_min_confidence=knowledge_min_confidence,
        knowledge_match_min=knowledge_match_min,
        question_intent_min=question_intent_min,
        knowledge_user_cooldown_seconds=max(
            0,
            _as_int("KNOWLEDGE_USER_COOLDOWN_SECONDS", 30),
        ),
        knowledge_max_message_length=max(
            100,
            _as_int("KNOWLEDGE_MAX_MESSAGE_LENGTH", 500),
        ),
        knowledge_pages=knowledge_pages,
        knowledge_auto_discover=_as_bool(
            os.getenv("KNOWLEDGE_AUTO_DISCOVER"),
            True,
        ),
        knowledge_max_pages=min(
            40,
            max(6, _as_int("KNOWLEDGE_MAX_PAGES", 20)),
        ),
        discord_knowledge_enabled=_as_bool(
            os.getenv("DISCORD_KNOWLEDGE_ENABLED"),
            True,
        ),
        discord_knowledge_channel_ids=_as_int_set(
            "DISCORD_KNOWLEDGE_CHANNEL_IDS"
        ),
        discord_knowledge_refresh_seconds=max(
            300,
            _as_int("DISCORD_KNOWLEDGE_REFRESH_SECONDS", 1800),
        ),
        discord_knowledge_max_channels=min(
            100,
            max(5, _as_int("DISCORD_KNOWLEDGE_MAX_CHANNELS", 30)),
        ),
        discord_knowledge_messages_per_channel=min(
            100,
            max(
                5,
                _as_int("DISCORD_KNOWLEDGE_MESSAGES_PER_CHANNEL", 40),
            ),
        ),
        groq_enabled=groq_enabled,
        groq_api_key=groq_api_key,
        groq_model=os.getenv(
            "GROQ_MODEL",
            "openai/gpt-oss-20b",
        ).strip()
        or "openai/gpt-oss-20b",
        groq_vision_model=os.getenv(
            "GROQ_VISION_MODEL",
            "qwen/qwen3.6-27b",
        ).strip()
        or "qwen/qwen3.6-27b",
        groq_timeout_seconds=max(
            3,
            _as_int("GROQ_TIMEOUT_SECONDS", 6),
        ),
        ai_min_confidence=ai_min_confidence,
        ai_public_enabled=_as_bool(os.getenv("AI_PUBLIC_ENABLED"), True),
        ai_public_require_mention=_as_bool(
            os.getenv("AI_PUBLIC_REQUIRE_MENTION"),
            True,
        ),
        ai_public_min_confidence=ai_public_min_confidence,
        ai_allowed_guild_ids=_as_int_set("AI_ALLOWED_GUILD_IDS")
        or {1467976297689911377},
        ai_context_messages=min(
            15,
            max(4, _as_int("AI_CONTEXT_MESSAGES", 5)),
        ),
        ai_context_ttl_minutes=min(
            30,
            max(
                5,
                _as_int("AI_CONTEXT_TTL_MINUTES", 15),
            ),
        ),
        ai_max_response_chars=min(
            1900,
            max(300, _as_int("AI_MAX_RESPONSE_CHARS", 1800)),
        ),
        weekly_report_enabled=_as_bool(os.getenv("WEEKLY_REPORT_ENABLED"), True),
        weekly_report_channel_id=_as_optional_int("WEEKLY_REPORT_CHANNEL_ID"),
        weekly_report_weekday=weekly_weekday,
        weekly_report_hour=weekly_hour,
        weekly_report_minute=weekly_minute,
        weekly_report_timezone=os.getenv(
            "WEEKLY_REPORT_TIMEZONE",
            "America/Sao_Paulo",
        ).strip()
        or "America/Sao_Paulo",
        default_server_statistics_channel_id=_as_optional_int(
            "DEFAULT_SERVER_STATISTICS_CHANNEL_ID"
        )
        or 1521879971012935750,
        default_site_statistics_channel_id=_as_optional_int(
            "DEFAULT_SITE_STATISTICS_CHANNEL_ID"
        )
        or 1521880931869261966,
        daily_report_enabled=_as_bool(os.getenv("DAILY_REPORT_ENABLED"), True),
        daily_report_hour=daily_hour,
        daily_report_minute=daily_minute,
        monthly_report_enabled=_as_bool(
            os.getenv("MONTHLY_REPORT_ENABLED"),
            True,
        ),
        monthly_report_day=monthly_day,
        monthly_report_hour=monthly_hour,
        monthly_report_minute=monthly_minute,
        traffic_log_path=(
            Path(raw_traffic_log_path).expanduser()
            if raw_traffic_log_path
            else None
        ),
        traffic_scan_interval_seconds=max(
            10,
            _as_int("TRAFFIC_SCAN_INTERVAL_SECONDS", 60),
        ),
        traffic_session_minutes=max(
            5,
            _as_int("TRAFFIC_SESSION_MINUTES", 30),
        ),
        traffic_retention_days=max(
            8,
            _as_int("TRAFFIC_RETENTION_DAYS", 45),
        ),
        traffic_max_events=max(
            1_000,
            _as_int("TRAFFIC_MAX_EVENTS", 100_000),
        ),
        traffic_hosts=[
            value.lower()
            for value in _as_csv(
                "TRAFFIC_HOSTS",
                [site_base_url.split("://", 1)[-1].split("/", 1)[0]],
            )
        ],
        traffic_ingest_enabled=traffic_ingest_enabled,
        traffic_ingest_host=os.getenv(
            "TRAFFIC_INGEST_HOST",
            "127.0.0.1",
        ).strip()
        or "127.0.0.1",
        traffic_ingest_port=min(
            65535,
            max(1, _as_int("TRAFFIC_INGEST_PORT", 8787)),
        ),
        traffic_ingest_path=traffic_ingest_path,
        traffic_signature_secret=traffic_signature_secret,
        traffic_ingest_max_bytes=max(
            1024,
            _as_int("TRAFFIC_INGEST_MAX_BYTES", 2_000_000),
        ),
        ga4_property_id=os.getenv("GA4_PROPERTY_ID", "").strip(),
        ga4_credentials_path=(
            Path(raw_ga4_credentials).expanduser()
            if raw_ga4_credentials
            else None
        ),
        ga4_cache_minutes=max(
            5,
            _as_int("GA4_CACHE_MINUTES", 30),
        ),
        voice_auto_connect=_as_bool(os.getenv("VOICE_AUTO_CONNECT"), True),
        voice_channel_id=_as_optional_int("VOICE_CHANNEL_ID"),
        voice_self_deaf=_as_bool(os.getenv("VOICE_SELF_DEAF"), True),
        voice_self_mute=_as_bool(os.getenv("VOICE_SELF_MUTE"), True),
        voice_reconnect_interval_seconds=max(
            15,
            _as_int("VOICE_RECONNECT_INTERVAL_SECONDS", 60),
        ),
        welcome_enabled=_as_bool(os.getenv("WELCOME_ENABLED"), True),
        welcome_channel_id=_as_optional_int("WELCOME_CHANNEL_ID"),
        welcome_delete_after_seconds=min(
            300,
            max(5, _as_int("WELCOME_DELETE_AFTER_SECONDS", 20)),
        ),
    )
