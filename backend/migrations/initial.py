from sqlalchemy import inspect, text
from src.config.database import Base, engine
from src import models  # noqa: F401  注册全部模型到 Base.metadata


def _ensure_columns():
    """幂等补齐既有库的新列（create_all 只建新表，不会修改已有表）。"""
    inspector = inspect(engine)
    if 'jobs' not in inspector.get_table_names():
        return
    existing = {col['name'] for col in inspector.get_columns('jobs')}
    additions = {
        'event_count': 'INTEGER NOT NULL DEFAULT 0',
        'attempt': 'INTEGER NOT NULL DEFAULT 1',
    }
    with engine.begin() as conn:
        for name, ddl in additions.items():
            if name not in existing:
                conn.execute(text(f'ALTER TABLE jobs ADD COLUMN {name} {ddl}'))


def migrate():
    Base.metadata.create_all(engine)
    _ensure_columns()


if __name__ == '__main__':
    migrate()
