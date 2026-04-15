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

    # Yandex Messenger Bot API base URL
    yandex_api_base: str = "https://botapi.messenger.yandex.net/bot/v1"

    # Yandex Messenger Client API base URL (undocumented, for user-as-bot mode)
    yandex_client_api_base: str = "https://api.messenger.yandex.ru"

    # Polling interval for Yandex getUpdates (seconds)
    ym_poll_interval: float = 1.0

    # Max long-polling timeout for TG-compatible getUpdates (seconds)
    tg_long_poll_max_timeout: int = 50

    # --- OAuth settings (for user-as-bot mode) ---
    # Register an app at https://oauth.yandex.ru/client/new/ with yamb:all scope
    oauth_client_id: str = ""
    oauth_client_secret: str = ""

    # --- Yandex 360 Admin API (for automated user/bot provisioning) ---
    yandex360_oauth_token: str = ""  # Admin token with ya360_admin:directory_write scope
    yandex360_org_id: str = ""       # Organization ID

    # --- Yandex Cloud (for service account provisioning via yc CLI) ---
    yc_folder_id: str = ""
    yc_cloud_id: str = ""
    yc_profile: str = ""  # yc CLI profile name (optional)

    model_config = {"env_file": ".env", "env_prefix": "BSF_"}


settings = Settings()
