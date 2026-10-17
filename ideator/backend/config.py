from pathlib import Path
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    falkordb_host: str = "localhost"
    falkordb_port: int = 6379
    serpapi_api_key: Optional[str] = None
    tabpfn_api_key: Optional[str] = None
    sentry_dsn: Optional[str] = None
    elevenlabs_api_key: Optional[str] = None
    gemma_api_key: Optional[str] = None
    gemini_api_key: Optional[str] = None
    github_client_id: Optional[str] = None
    github_client_secret: Optional[str] = None
    session_secret: Optional[str] = None

    model_config = SettingsConfigDict(
        env_file=(".env", str(Path(__file__).resolve().parents[2] / ".env")),  # repo-root .env works from any CWD
        env_file_encoding="utf-8",
        extra="ignore"
    )

    @property
    def mock_serpapi(self) -> bool:
        return not bool(self.serpapi_api_key)

    @property
    def mock_tabpfn(self) -> bool:
        return not bool(self.tabpfn_api_key)

    @property
    def mock_gemma(self) -> bool:
        return not bool(self.gemma_api_key or self.gemini_api_key)

    @property
    def mock_elevenlabs(self) -> bool:
        return not bool(self.elevenlabs_api_key)


settings = Settings()
