"""日志配置：统一写入 LOG_DIR（默认 ./logs），同时输出到控制台。

文件（按天切分，保留 LOG_RETENTION_DAYS 天，旧文件带日期后缀，如 app.log.2026-09-28）：
- app.log        web 节点的全部应用日志（INFO 及以上，含 uvicorn 启动/错误日志）
- error.log      web 节点 WARNING 及以上，排查问题先看这里
- access.log     HTTP 访问日志（uvicorn.access）
- scheduler.log / scheduler-error.log  定时任务进程（python -m app.scheduler）的全部日志 / WARNING 及以上

多个 web 节点部署在同一台机器时，给每个节点配置不同的 LOG_DIR，避免多个进程写同一个文件
（TimedRotatingFileHandler 不支持多进程同时切分）。

在进程最早期调用 setup_logging()（app/main.py 导入阶段、run.py 启动前），
并通过 uvicorn_log_config() 让 uvicorn 不再覆盖这里的配置。
重复调用是安全的（热重载子进程会重新导入 app.main）。
"""
from __future__ import annotations

import logging
import logging.config
from pathlib import Path
from typing import Any

from app.core.config import get_settings

FMT = "%(asctime)s %(levelname)s [%(process)d] %(name)s: %(message)s"
ACCESS_FMT = "%(asctime)s %(message)s"
NOISY = ("primp", "httpx", "httpx2", "httpcore", "ddgs", "trafilatura", "watchfiles", "urllib3", "openai._base_client")

_configured = False


def log_dir() -> Path:
    s = get_settings()
    d = Path(s.log_dir).expanduser()
    if not d.is_absolute():
        d = Path(__file__).resolve().parents[2] / d  # 相对路径按项目根目录解析，与启动目录无关
    d.mkdir(parents=True, exist_ok=True)
    return d


def _config(process: str = "app") -> dict[str, Any]:
    s = get_settings()
    d = log_dir()
    level = s.log_level.upper()
    main_file = "scheduler.log" if process == "scheduler" else "app.log"
    error_file = "scheduler-error.log" if process == "scheduler" else "error.log"

    def file_handler(name: str, lvl: str, fmt: str = "default") -> dict[str, Any]:
        return {
            "class": "logging.handlers.TimedRotatingFileHandler",
            "filename": str(d / name), "when": "midnight", "backupCount": s.log_retention_days,
            "encoding": "utf-8", "level": lvl, "formatter": fmt, "delay": True,
        }

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {"format": FMT},
            "access": {"format": ACCESS_FMT},
        },
        "handlers": {
            "console": {"class": "logging.StreamHandler", "level": level, "formatter": "default"},
            "app_file": file_handler(main_file, level),
            "error_file": file_handler(error_file, "WARNING"),
            "access_file": file_handler("access.log", "INFO", "access"),
            "access_console": {"class": "logging.StreamHandler", "level": "INFO", "formatter": "access"},
        },
        "root": {"level": level, "handlers": ["console", "app_file", "error_file"]},
        "loggers": {
            # uvicorn 的启动/错误日志走 root；访问日志单独写 access.log，不进 app.log
            "uvicorn": {"level": "INFO", "handlers": [], "propagate": True},
            "uvicorn.error": {"level": "INFO", "handlers": [], "propagate": True},
            "uvicorn.access": {"level": "INFO", "propagate": False,
                               "handlers": ["access_file", *(["access_console"] if s.log_access_console else [])]},
            **{n: {"level": "WARNING"} for n in NOISY},
        },
    }


def setup_logging(process: str = "app") -> Path:
    """初始化日志，返回日志目录。process：app（web 节点）/ scheduler（定时任务进程）。"""
    global _configured
    if not _configured:
        logging.config.dictConfig(_config(process))
        _configured = True
    return log_dir()


def uvicorn_log_config() -> dict[str, Any]:
    """传给 uvicorn.run(log_config=...)，避免 uvicorn 用自带配置覆盖（reload 子进程也会用到）。"""
    return _config()
