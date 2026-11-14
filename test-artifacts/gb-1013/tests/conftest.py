import os
import sys
import tempfile
from pathlib import Path

# 环境变量必须在 src.config.settings 导入前设置
_TMP = Path(tempfile.mkdtemp(prefix='quota-tests-'))
os.environ['DATABASE_URL'] = f'sqlite:///{_TMP / "test.db"}'
os.environ['REDIS_URL'] = 'redis://unused/0'
os.environ['JWT_SECRET'] = 'test-secret'
os.environ['ARTIFACT_DIR'] = str(_TMP / 'artifacts')
os.environ['SEED_DEMO'] = 'false'

BACKEND = Path(__file__).resolve().parents[3] / 'backend'
sys.path.insert(0, str(BACKEND))

import redis  # noqa: E402
from sqlalchemy import event  # noqa: E402


class _FakeRedis:
    def __init__(self, *a, **k):
        self.items = []

    def rpush(self, k, v):
        self.items.append(v)
        return len(self.items)

    def blpop(self, k, timeout=0):
        return None

    def ping(self):
        return True

    def close(self):
        pass


redis.Redis.from_url = staticmethod(lambda *a, **k: _FakeRedis())

from src.config.database import engine  # noqa: E402


@event.listens_for(engine, 'connect')
def _sqlite_pragmas(dbapi_conn, _record):
    cur = dbapi_conn.cursor()
    cur.execute('PRAGMA busy_timeout=15000')
    cur.execute('PRAGMA journal_mode=WAL')
    cur.close()
