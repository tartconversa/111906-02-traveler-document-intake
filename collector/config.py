"""运行配置与本地目录布局。

下载的原件属于不可信数据，统一放在独立的 ``state_dir`` 下，与本包代码隔离；
状态库、原件目录、日志各自分子目录，中断后可直接复用。
"""
from __future__ import annotations

import dataclasses
from pathlib import Path


@dataclasses.dataclass(frozen=True)
class Config:
    base_url: str
    state_dir: Path
    headless: bool = True
    timeout_ms: int = 20000

    @property
    def db_path(self) -> Path:
        return self.state_dir / "state.db"

    @property
    def downloads_dir(self) -> Path:
        return self.state_dir / "downloads"

    @property
    def log_path(self) -> Path:
        return self.state_dir / "logs" / "collector.log"

    def ensure_dirs(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
