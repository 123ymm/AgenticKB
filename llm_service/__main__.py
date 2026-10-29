"""Run LLM Service: python -m llm_service"""
import asyncio
import copy
import logging
import os
import sys

# Windows: psycopg async requires SelectorEventLoop, not ProactorEventLoop.
if sys.platform == "win32":
    import uvicorn.loops.asyncio as _uv_loop  # noqa: E402
    _uv_loop.asyncio_loop_factory = lambda use_subprocess=False: asyncio.SelectorEventLoop

import uvicorn  # noqa: E402
from uvicorn.config import LOGGING_CONFIG  # noqa: E402

from llm_service.config import load_domain_configs, dig  # noqa: E402
from llm_service.main import set_startup_config, set_startup_domain_configs  # noqa: E402

_DATEFMT = "%Y-%m-%d %H:%M:%S"
_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()


def _timestamped_log_config() -> dict:
    """Application + uvicorn logging with timestamps.

    Without the basicConfig below the root logger has no handler: uvicorn's own
    LOGGING_CONFIG only configures the `uvicorn*` loggers, so every
    `logging.getLogger(__name__)` call in this package falls through to Python's
    lastResort handler, which drops anything below WARNING. That silently
    swallowed all INFO/DEBUG logging from this service.
    """
    logging.basicConfig(
        level=_LOG_LEVEL,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt=_DATEFMT,
    )
    cfg = copy.deepcopy(LOGGING_CONFIG)
    cfg["formatters"]["default"]["fmt"] = "%(asctime)s %(levelprefix)s %(message)s"
    cfg["formatters"]["default"]["datefmt"] = _DATEFMT
    cfg["formatters"]["access"]["fmt"] = (
        '%(asctime)s %(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s'
    )
    cfg["formatters"]["access"]["datefmt"] = _DATEFMT
    for _logger in cfg.get("loggers", {}).values():
        _logger["level"] = _LOG_LEVEL
    return cfg


# 62号：全局 + 各域 effective 配置一次拉齐（全局失败致命；单个域覆盖
# 作废回落全局，见 config.fetch_llm_domain_overrides）。
_domain_configs = load_domain_configs()
cfg = _domain_configs.global_config
set_startup_config(cfg)
set_startup_domain_configs(_domain_configs)

uvicorn.run(
    "llm_service.main:create_app_from_startup_config",
    host=dig(cfg, "host"),
    port=dig(cfg, "port"),
    factory=True,
    log_config=_timestamped_log_config(),
)
