"""Runtime settings. Secrets and site specifics come from the environment / .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- data source --------------------------------------------------------
    eg4mon_source: Literal["eg4_cloud", "solar_assistant", "replay"] = "eg4_cloud"

    # EG4 cloud
    eg4_base_url: str = "https://monitor.eg4electronics.com"
    eg4_username: str = ""
    eg4_password: SecretStr = SecretStr("")
    eg4_inverter_serials: Annotated[list[str], NoDecode] = Field(default_factory=list)
    eg4_gridboss_serial: str = ""
    eg4_poll_seconds: float = 30.0
    eg4_field_map: Path = REPO_ROOT / "config" / "eg4_cloud.yaml"

    # SolarAssistant / MQTT
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: SecretStr = SecretStr("")
    mqtt_topics_file: Path = REPO_ROOT / "config" / "topics.yaml"

    # Replay source
    replay_csv: Path | None = None
    replay_speed: float = 0.0  # 0 = as fast as possible, N = N x real time

    # --- storage / model ----------------------------------------------------
    db_path: Path = REPO_ROOT / "data" / "eg4mon.sqlite"
    model_params_file: Path = REPO_ROOT / "config" / "model_params.yaml"
    pipeline_file: Path = REPO_ROOT / "config" / "pipeline.yaml"
    site_tz: str = "UTC"

    # --- API / dashboard ----------------------------------------------------
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_token: SecretStr = SecretStr("")  # optional bearer token; empty = no auth
    dashboard_api_url: str = "http://localhost:8000"
    dashboard_refresh_seconds: int = 10

    log_level: str = "INFO"

    @field_validator("eg4_inverter_serials", mode="before")
    @classmethod
    def _split_serials(cls, v):
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    def model_params(self) -> dict:
        return load_yaml(self.model_params_file)

    def pipeline(self) -> dict:
        return load_yaml(self.pipeline_file)


def load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@lru_cache
def get_settings() -> Settings:
    return Settings()
