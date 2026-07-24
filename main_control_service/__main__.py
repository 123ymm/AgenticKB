"""Run main_control_service: python -m main_control_service"""
import uvicorn

from main_control_service.config import MainControlSettings

cfg = MainControlSettings()
uvicorn.run("main_control_service.main:app", host=cfg.host, port=cfg.port, reload=False)
