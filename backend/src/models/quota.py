from sqlalchemy import String, Integer, BigInteger, ForeignKey, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base


class QuotaSetting(Base):
    """账号每日可处理事件额度。调额只影响新受理，不撤销已受理任务。"""
    __tablename__ = 'quota_settings'
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    daily_limit: Mapped[int] = mapped_column(Integer)
    updated_by: Mapped[str] = mapped_column(String(150), default='')
    updated_at: Mapped[float] = mapped_column(BigInteger().with_variant(Integer, 'sqlite'))
    __table_args__ = (UniqueConstraint('user_id', name='uq_quota_setting_user'),)


class QuotaLedger(Base):
    """每个受理任务一条额度台账：受理时预占，完成转实耗，失败归还，按受理日归属。"""
    __tablename__ = 'quota_ledgers'
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id'))
    job_id: Mapped[int] = mapped_column(ForeignKey('jobs.id'))
    quota_date: Mapped[str] = mapped_column(String(10))  # 受理日，本地时区 YYYY-MM-DD
    event_count: Mapped[int] = mapped_column(Integer)     # 受理批次事件数（预占量）
    consumed: Mapped[int] = mapped_column(Integer, default=0)  # 实耗事件数
    status: Mapped[str] = mapped_column(String(20), default='reserved')  # reserved/settled/released
    idempotency_key: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[float] = mapped_column(BigInteger().with_variant(Integer, 'sqlite'))
    updated_at: Mapped[float] = mapped_column(BigInteger().with_variant(Integer, 'sqlite'))
    __table_args__ = (
        UniqueConstraint('user_id', 'idempotency_key', name='uq_quota_ledger_user_idem'),
        UniqueConstraint('job_id', name='uq_quota_ledger_job'),
        Index('ix_quota_ledger_user_date', 'user_id', 'quota_date'),
    )
