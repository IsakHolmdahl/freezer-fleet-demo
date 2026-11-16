"""Settings of the service, read from the environment (and a local `.env` file)."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    store_api_url: str = "http://127.0.0.1:8100/api/v1/stores/ica-teststad"  # the store endpoint
    store_api_token: str = "dev-token"
    database_url: str = "sqlite:///storecloud.db"
    poll_interval_seconds: float = 5.0     # how often the store API is read
    offline_after_seconds: float = 30.0    # a freezer not seen for this long counts as offline
    alarm_above_c: float = -15.0           # warmer than this raises an alarm
    defrost_alarm_above_c: float = 0.0     # the limit while a freezer defrosts (it warms a little)
