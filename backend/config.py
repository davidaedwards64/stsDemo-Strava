"""Application settings loaded from environment variables."""

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    anthropic_api_key: str
    session_secret: str = ""

    # OIDC Web App (user login)
    okta_client_id: str = ""
    okta_client_secret: str = ""
    okta_domain: str = ""
    okta_redirect_uri: str = "http://localhost:8000/auth/callback"
    okta_post_logout_redirect_uri: str = ""

    # AI Agent workload principal (API Services app)
    okta_agent_client_id: str = ""
    okta_agent_private_jwk: str = ""

    # APP_INSTANCE resource indicator ORN for the Strava connection
    okta_strava_resource_indicator: str = ""

    @property
    def okta_issuer(self) -> str:
        return f"https://{self.okta_domain}/oauth2" if self.okta_domain else ""

    @property
    def okta_token_url(self) -> str:
        return f"https://{self.okta_domain}/oauth2/v1/token" if self.okta_domain else ""

    @property
    def okta_end_session_url(self) -> str:
        return f"https://{self.okta_domain}/oauth2/v1/logout" if self.okta_domain else ""

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    return Settings()
