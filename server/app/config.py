"""Server settings, all overridable by environment variable.

Every tuning threshold the spec calls out -- association margins, play gap,
quiet-alert window -- lives here rather than at its use site (spec 11), so the
association job can be retuned and re-run without a code change.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SETLIST_", env_file=".env",
                                      extra="ignore")

    database_url: str = "postgresql+psycopg://setlist:setlist@localhost:5432/setlist"

    #: Bind to the tailnet address in production. Never port-forward (spec 3).
    host: str = "127.0.0.1"
    port: int = 8000

    #: Batch size cap for POST /detections (spec 6).
    max_batch: int = 500

    # --- association job (spec 8) ---------------------------------------
    #: An event's window is widened by these margins before matching
    #: detections into it. Doors are early, sets overrun.
    association_before_minutes: int = 30
    association_after_minutes: int = 45

    # --- sessionization (spec 8) ----------------------------------------
    #: The same track reappearing after a longer gap is a new play.
    play_gap_minutes: int = 15

    # --- health alerting (spec 8) ---------------------------------------
    #: Fire when a venue goes quiet for longer than this during a scheduled
    #: event, or when a client trips its daily API ceiling.
    quiet_alert_minutes: int = 20
    home_assistant_webhook_url: str = ""

    # --- scraping (spec 7) ----------------------------------------------
    scrape_timeout_s: float = 20.0
    scrape_user_agent: str = "venue-setlist/1.0 (+self-hosted analytics)"


@lru_cache
def get_settings() -> Settings:
    return Settings()
