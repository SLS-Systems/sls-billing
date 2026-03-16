from pydantic_settings import BaseSettings


class BillingSettings(BaseSettings):
    database_url: str = ""
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    frontend_url: str = "https://app.ci.care"
    environment: str = "production"

    model_config = {"env_file": ".env"}


settings = BillingSettings()
