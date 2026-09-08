"""Application settings and scraper YAML configuration loading."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from bazcar.core.exceptions import ConfigError

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent


class HttpConfig(BaseModel):
    timeout_seconds: float = 15.0
    retries: int = 3
    backoff_base_seconds: float = 2.0
    backoff_max_seconds: float = 30.0
    min_interval_ms: int = 1500
    jitter_ms: int = 500
    headers: dict[str, str] = Field(default_factory=dict)


class SelectorConfig(BaseModel):
    container: str
    title: str
    image: str
    price: str
    location: str
    views: str
    description: str
    detail_description: str
    date: str
    pagination: str


class MarkerConfig(BaseModel):
    ad_path_regex: str
    date_regex: str
    year_regexes: list[str] = Field(default_factory=list)
    mileage_regexes: list[str] = Field(default_factory=list)
    ban_markers: list[str] = Field(default_factory=list)


class BaseConfig(BaseModel):
    base_url: str
    page_size: int = 20


class ScraperConfig(BaseModel):
    base: BaseConfig
    http: HttpConfig
    user_agents: list[str]
    selectors: SelectorConfig
    markers: MarkerConfig


class Settings(BaseSettings):
    """Runtime settings; values come from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        env_prefix="BAZCAR_",
        extra="ignore",
    )

    scraper_config: Path = PROJECT_ROOT / "config" / "scraper.yaml"
    log_level: str = "INFO"
    export_dir: Path = PROJECT_ROOT / "data" / "exports"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


@lru_cache(maxsize=1)
def load_scraper_config(path: Path | None = None) -> ScraperConfig:
    cfg_path = path or get_settings().scraper_config
    try:
        raw = _load_yaml(cfg_path)
        return ScraperConfig.model_validate(raw)
    except FileNotFoundError as exc:
        raise ConfigError(f"Scraper config not found: {cfg_path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {cfg_path}: {exc}") from exc
    except ValidationError as exc:
        raise ConfigError(f"Invalid scraper config schema in {cfg_path}: {exc}") from exc


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}
