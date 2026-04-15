from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Bot StepFather's own Yandex Messenger token
    stepfather_token: str = ""

    # Adapter server settings
    adapter_host: str = "0.0.0.0"
    adapter_port: int = 8081

    # Public URL where the adapter is accessible (for webhook forwarding)
    adapter_public_url: str = "http://localhost:8081"

    # Database
    database_url: str = "sqlite+aiosqlite:///bot_registry.db"

    # Yandex Messenger API base URL
    yandex_api_base: str = "https://botapi.messenger.yandex.net/bot/v1"

    # Polling interval for Yandex getUpdates (seconds)
    ym_poll_interval: float = 1.0

    # Max long-polling timeout for TG-compatible getUpdates (seconds)
    tg_long_poll_max_timeout: int = 50

    model_config = {"env_file": ".env", "env_prefix": "BSF_"}


settings = Settings()
