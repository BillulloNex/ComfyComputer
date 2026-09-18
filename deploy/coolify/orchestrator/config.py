"""Configuration for the Computer on Demand orchestrator."""

import os


class Config:
    """All settings from environment variables with sensible defaults."""

    # Docker image to use for computers
    COMPUTER_IMAGE: str = os.getenv("COMPUTER_IMAGE", "agent-computer:latest")

    # Concurrency limits
    MAX_RUNNING_COMPUTERS: int = int(os.getenv("MAX_RUNNING_COMPUTERS", "3"))
    MAX_TOTAL_COMPUTERS: int = int(os.getenv("MAX_TOTAL_COMPUTERS", "20"))

    # Port ranges (each computer gets one port from each range)
    PORT_RANGE_VNC_START: int = int(os.getenv("PORT_RANGE_VNC_START", "6901"))
    PORT_RANGE_COMPUTER_START: int = int(os.getenv("PORT_RANGE_COMPUTER_START", "8001"))
    PORT_RANGE_CDP_START: int = int(os.getenv("PORT_RANGE_CDP_START", "9223"))
    PORT_RANGE_SIZE: int = int(os.getenv("PORT_RANGE_SIZE", "20"))

    # Default resource limits per computer
    DEFAULT_CPU_LIMIT: str = os.getenv("DEFAULT_CPU_LIMIT", "1.0")
    DEFAULT_MEMORY_LIMIT: str = os.getenv("DEFAULT_MEMORY_LIMIT", "3g")
    DEFAULT_SHM_SIZE: str = os.getenv("DEFAULT_SHM_SIZE", "2g")
    DEFAULT_RESOLUTION: str = os.getenv("DEFAULT_RESOLUTION", "1920x1080")

    # Networking
    HOST_ADDRESS: str = os.getenv("HOST_ADDRESS", "localhost")
    DOCKER_NETWORK: str = os.getenv("DOCKER_NETWORK", "cua-computers")

    # Public base URL for broker-proxied endpoints (what Starship/agents use).
    # Defaults to http://HOST_ADDRESS:3000; in production set to the Coolify
    # FQDN, e.g. https://computers.beenex.cloud
    PUBLIC_BASE_URL: str = os.getenv(
        "PUBLIC_BASE_URL", f"http://{os.getenv('HOST_ADDRESS', 'localhost')}:3000"
    )

    # API authentication. Fail-closed: when empty, every endpoint except
    # /health returns 503. Set ORCHESTRATOR_API_KEY in Coolify (runtime var);
    # local dev can use ALLOW_ANONYMOUS=true instead of a key.
    API_KEY: str = os.getenv("ORCHESTRATOR_API_KEY", "")
    ALLOW_ANONYMOUS: bool = os.getenv("ALLOW_ANONYMOUS", "false").lower() == "true"

    # CORS origins for browser callers (VNC viewer etc). Comma-separated.
    # Default deny — server-side agent traffic needs no CORS.
    CORS_ORIGINS: list[str] = [
        o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()
    ]

    # Auto-idle timeout (minutes). 0 = disabled.
    IDLE_TIMEOUT_MINUTES: int = int(os.getenv("IDLE_TIMEOUT_MINUTES", "30"))

    # Database
    DB_PATH: str = os.getenv("DB_PATH", "/data/computers.db")

    # VNC password for spawned computers. Fail-closed at container start:
    # crash loudly instead of baking every desktop with a known password.
    # (Already-running computers keep the password from their own env —
    # a broker restart never re-keys them.)
    VNC_PASSWORD: str = os.getenv("VNC_PASSWORD", "")

    # Health check
    HEALTH_CHECK_TIMEOUT: int = int(os.getenv("HEALTH_CHECK_TIMEOUT", "120"))
    HEALTH_CHECK_INTERVAL: int = int(os.getenv("HEALTH_CHECK_INTERVAL", "2"))
