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

    # Auto-idle timeout (minutes). 0 = disabled.
    IDLE_TIMEOUT_MINUTES: int = int(os.getenv("IDLE_TIMEOUT_MINUTES", "30"))

    # Database
    DB_PATH: str = os.getenv("DB_PATH", "/data/computers.db")

    # VNC password for spawned computers
    VNC_PASSWORD: str = os.getenv("VNC_PASSWORD", "kasm123")

    # Health check
    HEALTH_CHECK_TIMEOUT: int = int(os.getenv("HEALTH_CHECK_TIMEOUT", "120"))
    HEALTH_CHECK_INTERVAL: int = int(os.getenv("HEALTH_CHECK_INTERVAL", "2"))
