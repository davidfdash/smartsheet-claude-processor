"""
config.py
---------
Loads all settings from the .env file.
Users only need to edit .env — nothing in this file changes.
"""

from functools import lru_cache
from typing import List

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        populate_by_name=True,
    )

    # ── Required ──────────────────────────────────────────────────────────
    smartsheet_token: str
    anthropic_api_key: str

    # Reads from SHEET_IDS (or SHEET_IDS_RAW) in .env
    # e.g. SHEET_IDS=5337282696400772,5267249496543108
    sheet_ids_raw: str = Field(
        default="",
        validation_alias=AliasChoices("sheet_ids", "sheet_ids_raw"),
    )

    # ── Optional ──────────────────────────────────────────────────────────
    port: int = 8000
    host: str = "0.0.0.0"

    # How many seconds to wait for the Anthropic API (Claude + MCP can be slow)
    anthropic_timeout: int = 180

    # Claude model to use
    claude_model: str = "claude-sonnet-4-6"

    # How often to poll sheets for rows that missed their webhook (minutes)
    poll_interval_mins: int = 10

    # ── Derived ───────────────────────────────────────────────────────────
    @property
    def sheet_ids(self) -> List[str]:
        return [s.strip() for s in self.sheet_ids_raw.split(",") if s.strip()]

    @field_validator("smartsheet_token", "anthropic_api_key")
    @classmethod
    def not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("must not be empty")
        return v.strip()


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
