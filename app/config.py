import logging

from pydantic_settings import BaseSettings

_logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    # Partner 1
    partner_1_username: str = "francisco"
    partner_1_password: str = ""
    partner_1_display_name: str = "Francisco Covarrubias"

    # Partner 2
    partner_2_username: str = "juliana"
    partner_2_password: str = ""
    partner_2_display_name: str = "Juliana Soto"

    # Session signing
    secret_key: str = "change-me-in-production"

    # LLM
    gemini_api_key: str = ""

    # Data sources
    perplexity_api_key: str = ""
    serpapi_api_key: str = ""
    fred_api_key: str = ""

    # App
    database_path: str = "data/thefindbrief.db"
    host: str = "0.0.0.0"
    port: int = 8000

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    def get_partner_accounts(self) -> dict[str, dict[str, str]]:
        """Return a dict mapping username -> {password, display_name}."""
        return {
            self.partner_1_username: {
                "password": self.partner_1_password,
                "display_name": self.partner_1_display_name,
            },
            self.partner_2_username: {
                "password": self.partner_2_password,
                "display_name": self.partner_2_display_name,
            },
        }


settings = Settings()

# Startup validation
if settings.secret_key == "change-me-in-production":
    _logger.warning("SECRET_KEY is set to the default value — change it before deploying")
if not settings.partner_1_password or not settings.partner_2_password:
    _logger.warning("Partner passwords are empty — set them in .env")
