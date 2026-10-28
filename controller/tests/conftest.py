import base64
import os

import pytest

from homewatch.config import load
from homewatch.db import DB

KEY = base64.b64encode(os.urandom(32)).decode()


@pytest.fixture
def cfg(tmp_path):
    return load({"HW_DATA_DIR": str(tmp_path), "HW_MASTER_KEY": KEY, "HW_TELEGRAF_DIR": str(tmp_path / "tg"),
                 "HW_SECURE_COOKIES": "0", "HW_GATEWAY_HINT": "192.168.1.1", "HW_DNS_HINT": "192.168.1.1"})


@pytest.fixture
def db(tmp_path):
    return DB(str(tmp_path / "t.db"))
