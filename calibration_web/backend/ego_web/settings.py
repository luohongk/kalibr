from pathlib import Path

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EGO_WEB_", extra="ignore")

    data_root: Path = Path("/home/conanluo/kalibr_data")
    repo_root: Path = Path("/root/kalibr")
    runtime_root: Path = Path("/root/kalibr/calibration_web/runtime")
    max_concurrency: int = Field(default=1, ge=1, le=1)
    terminate_grace_sec: float = Field(default=10.0, gt=0)
    host: str = "0.0.0.0"
    port: int = Field(default=8020, ge=1, le=65535)

    @computed_field
    @property
    def runner_script(self) -> Path:
        return self.repo_root / "auto_calib" / "run_all.sh"

    @computed_field
    @property
    def database_path(self) -> Path:
        return self.runtime_root / "kalibr.sqlite3"

    @computed_field
    @property
    def frontend_dist(self) -> Path:
        return self.repo_root / "calibration_web" / "frontend" / "dist"
