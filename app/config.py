from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "sqlite:///./eventpass.db"
    public_base_url: str = "http://localhost:8000"
    cookie_secure: bool = True
    admin_username: str = "admin"
    admin_password: str = ""
    meta_app_secret: str = ""
    meta_verify_token: str = ""
    meta_access_token: str = ""
    meta_account_id: str = ""
    meta_api_version: str = "v24.0"
    instagram_username: str = ""


settings = Settings()
