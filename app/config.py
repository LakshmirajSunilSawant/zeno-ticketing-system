# WHY: 12-factor config. Every environment-specific value comes from env vars, so the SAME image
# runs locally on SQLite and on Render against Postgres with zero code changes.
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # SQLite default so the project boots with zero setup; Render injects a Postgres URL.
    DATABASE_URL: str = "sqlite:///./ticketing.db"

    # WHY a default: dev convenience only. Render sets a real generated secret (see render.yaml).
    JWT_SECRET: str = "dev-secret-change-me"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24

    # WHY "*": this API must be callable from any machine/browser in the world (interview demo).
    # In a real product this would be an explicit allow-list of front-end origins.
    CORS_ORIGINS: str = "*"

    RATE_LIMIT: str = "100/minute"
    ENV: str = "development"

    @property
    def sqlalchemy_url(self) -> str:
        # WHY: Render/Heroku hand out legacy "postgres://" URLs, SQLAlchemy 2.0 requires
        # "postgresql://". Normalising here avoids a deploy-time crash that costs 10 minutes.
        url = self.DATABASE_URL
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        return url

    @property
    def is_sqlite(self) -> bool:
        return self.sqlalchemy_url.startswith("sqlite")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    # WHY cached: settings are read on every request dependency; parsing env once is enough.
    return Settings()


settings = get_settings()
