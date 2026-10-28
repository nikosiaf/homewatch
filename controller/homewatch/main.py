"""ASGI entry point: uvicorn homewatch.main:app"""

import logging
import os

from .web import create_app

logging.basicConfig(level=os.environ.get("HW_LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # request lines would log the Telegram token in URLs

app = create_app()
