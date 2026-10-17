"""
Smart Home Autonomous AI Agent Harness — Central Configuration
================================================================
All settings are overridable via environment variables (12-factor style)
so the same code runs on a Raspberry Pi 5 production deployment and in
local development without edits.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_bool(key: str, default: bool = False) -> bool:
    val = _env(key)
    if not val:
        return default
    return val.lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    # ------------------------------------------------------------------ Server
    host: str = _env("SH_AGENT_HOST", "0.0.0.0")
    port: int = int(_env("SH_AGENT_PORT", "8000"))
    db_url: str = _env("SH_AGENT_DB_URL", "sqlite+aiosqlite:///./smarthome.db")

    # --------------------------------------------------------------- AI / LLM
    ollama_base_url: str = _env("OLLAMA_BASE_URL", "http://localhost:11434")
    ollama_model: str = _env("OLLAMA_MODEL", "bonsai-2:4b")
    llm_temperature: float = float(_env("LLM_TEMPERATURE", "0.7"))
    llm_max_tokens: int = int(_env("LLM_MAX_TOKENS", "1024"))
    max_tool_iterations: int = int(_env("MAX_TOOL_ITERATIONS", "6"))

    # ------------------------------------------------------- Philips Hue Bridge
    hue_bridge_ip: str = _env("HUE_BRIDGE_IP", "")          # e.g. 192.168.1.50
    hue_app_username: str = _env("HUE_APP_USERNAME", "")    # created on first pairing
    hue_api_version: int = int(_env("HUE_API_VERSION", "2"))  # v2 CLIP API recommended

    # ------------------------------------------------------------- Spotify
    spotify_client_id: str = _env("SPOTIFY_CLIENT_ID", "")
    spotify_client_secret: str = _env("SPOTIFY_CLIENT_SECRET", "")
    spotify_redirect_uri: str = _env("SPOTIFY_REDIRECT_URI", "http://localhost:8989/callback")
    spotify_scope: str = _env(
        "SPOTIFY_SCOPE",
        "user-read-playback-state user-modify-playback-state user-read-currently-playing "
        "playlist-read-private playlist-modify-public playlist-modify-private",
    )

    # --------------------------------------------------------- Roidmi / Mi Home
    mi_cloud_country: str = _env("MI_CLOUD_COUNTRY", "de")   # e.g. de, us, cn
    mi_home_user: str = _env("MI_HOME_USER", "")             # Xiaomi account email
    mi_home_password: str = _env("MI_HOME_PASSWORD", "")
    roidmi_device_ip: str = _env("ROIDMI_DEVICE_IP", "")     # LAN IP of the vacuum
    roidmi_device_token: str = _env("ROIDMI_DEVICE_TOKEN", "")  # from `mii report`

    # ------------------------------------------------------------- Withings
    withings_client_id: str = _env("WITHINGS_CLIENT_ID", "")
    withings_client_secret: str = _env("WITHINGS_CLIENT_SECRET", "")
    withings_user_id: str = _env("WITHINGS_USER_ID", "")
    # Long-lived "smart home" app tokens work for the Data API; OAuth2 also supported.
    withings_access_token: str = _env("WITHINGS_ACCESS_TOKEN", "")
    withings_refresh_token: str = _env("WITHINGS_REFRESH_TOKEN", "")

    # ------------------------------------------------------ Home Assistant
    ha_base_url: str = _env("HOME_ASSISTANT_URL", "")        # http://homeassistant.local:8123
    ha_token: str = _env("HOME_ASSISTANT_TOKEN", "")         # long-lived access token

    # ------------------------------------------------------------ Weather
    openweathermap_key: str = _env("OPENWEATHERMAP_API_KEY", "")
    weather_lat: float = float(_env("WEATHER_LAT", "52.52"))
    weather_lon: float = _env("WEATHER_LON", "13.405")
    weather_units: str = _env("WEATHER_UNITS", "metric")     # metric | imperial

    # ----------------------------------------------------------- Web search
    searxng_url: str = _env("SEARXNG_URL", "")               # optional self-hosted SearXNG
    tavily_api_key: str = _env("TAVILY_API_KEY", "")
    rss_feeds: list[str] = field(default_factory=lambda: [
        f for f in _env("RSS_FEEDS", "").split(",") if f
    ] or [
        "https://feeds.bbci.co.uk/news/technology/rss.xml",
        "https://www.theverge.com/rss/index.xml",
    ])

    # ------------------------------------------------------------- Presence
    # MAC addresses (lowercase) of phones that indicate "someone is home".
    presence_macs: list[str] = field(default_factory=lambda: [
        m.strip().lower() for m in _env("PRESENCE_MACS", "").split(",") if m.strip()
    ])

    # ------------------------------------------------------------- Routines
    morning_routine_cron: str = _env("MORNING_ROUTINE_CRON", "0 7 * * *")     # daily 07:00
    evening_winddown_cron: str = _env("EVENING_WINDOWN_CRON", "0 22 * * *")   # daily 22:00
    away_check_interval_min: int = int(_env("AWAY_CHECK_INTERVAL_MIN", "15"))
    away_clean_enabled: bool = _env_bool("AWAY_CLEAN_ENABLED", True)

    # --------------------------------------------------------------- Runtime
    demo_mode: bool = _env_bool("SH_DEMO_MODE", not _env("HUE_BRIDGE_IP"))
    """When credentials/integrations are missing the tools transparently fall
    back to high-fidelity simulated responses so the whole harness (agent loop,
    dashboard, scheduler, liquid-glass glow effects) remains fully functional."""


settings = Settings()
