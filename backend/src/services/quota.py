"""每日事件额度：受理预占、完成转实耗、失败归还、超额拒绝。

关键口径：
- 额度按“受理日”归属，跨日恢复/结算仍计入受理当天，不影响恢复当天的额度。
- 预占量 = 受理批次事件数（job.event_count）；完成后实耗按实际处理事件数结算，
  差额随结算释放；失败全额归还。
- 同账号并发受理通过行锁串行化当日判断；Idempotency-Key 保证重试不重复建任务或占额。
- 调低额度只对新受理生效，已受理任务的台账不受影响；功能上线前的历史任务无台账、不追扣。
"""
import threading
import time

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from src.models import Dataset, Job, QuotaLedger, QuotaSetting

DEFAULT_DAILY_LIMIT = 1000  # 新账号/未配置账号的默认每日事件额度

# 进程内受理串行锁：SQLite 不支持 FOR UPDATE 行锁，本地/单进程部署靠它保证
# 并发受理不超领；Postgres 部署以 QuotaSetting 行锁为跨进程权威保证。
_reservation_lock = threading.Lock()


class reservation_lock:
    def __enter__(self):
        _reservation_lock.acquire()
        return self

    def __exit__(self, *exc):
        _reservation_lock.release()


class QuotaExceeded(HTTPException):
    def __init__(self, detail):
        super().__init__(429, detail)


def today_key(now=None):
    return time.strftime('%Y-%m-%d', time.localtime(now or time.time()))


def get_setting(db, user_id, lock=False):
    stmt = select(QuotaSetting).where(QuotaSetting.user_id == user_id)
    if lock:
        stmt = stmt.with_for_update()
    setting = db.scalar(stmt)
    if setting is None:
        # 跨进程并发首建：唯一约束兜底，冲突时回滚并重新读取既有配置。
        try:
            setting = QuotaSetting(user_id=user_id, daily_limit=DEFAULT_DAILY_LIMIT,
                                   updated_by='system', updated_at=time.time())
            db.add(setting)
            db.flush()
        except IntegrityError:
            db.rollback()
            setting = db.scalar(select(QuotaSetting).where(
                QuotaSetting.user_id == user_id))
            if setting is None:
                raise
    return setting


def reserve(db, data, user, idempotency_key):
    """在调用方事务内完成校验+预占+建任务。超额直接抛错，不留下任何任务或台账。

    返回 (job, replayed)：携带相同 Idempotency-Key 的重试返回原任务，不重复占额。
    """
    dataset = db.get(Dataset, data.dataset_id)
    if not dataset:
        raise HTTPException(404, '数据批次不存在')
    event_count = len(dataset.records)
    if event_count <= 0:
        raise HTTPException(422, '数据批次没有事件，无法受理')

    # 幂等重放：同账号同键的受理永远返回首次任务，并发下唯一约束兜底。
    # 查询限定本账号，避免撞键时越权返回他人任务。
    if idempotency_key:
        existing = db.scalar(select(QuotaLedger).where(
            QuotaLedger.idempotency_key == idempotency_key,
            QuotaLedger.user_id == user.id))
        if existing:
            return db.get(Job, existing.job_id), True

    setting = get_setting(db, user.id, lock=True)
    quota_date = today_key()
    rows = list(db.scalars(select(QuotaLedger).where(
        QuotaLedger.user_id == user.id, QuotaLedger.quota_date == quota_date)))
    reserved_total = sum(
        (r.event_count if r.status == 'reserved' else r.consumed) for r in rows)
    if reserved_total + event_count > setting.daily_limit:
        raise QuotaExceeded(
            f'今日（{quota_date}）事件额度不足：额度 {setting.daily_limit}，'
            f'已占用 {reserved_total}，本批次需 {event_count}。'
            '本请求已拒绝且不会生成任务。')

    now = time.time()
    job = Job(name=data.name, dataset_id=data.dataset_id, params=data.params,
              created_by=user.id, created_at=now, status='queued',
              event_count=event_count, attempt=1)
    db.add(job)
    db.flush()
    ledger = QuotaLedger(
        user_id=user.id, job_id=job.id, quota_date=quota_date,
        event_count=event_count, consumed=0, status='reserved',
        idempotency_key=idempotency_key or f'job-{job.id}',
        created_at=now, updated_at=now)
    db.add(ledger)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        replay = db.scalar(select(QuotaLedger).where(
            QuotaLedger.idempotency_key == idempotency_key,
            QuotaLedger.user_id == user.id))
        if replay:
            return db.get(Job, replay.job_id), True
        # 键被其他账号占用（客户端应使用随机 UUID，正常不会发生）。
        raise HTTPException(409, 'Idempotency-Key 已被其他请求使用，请更换后重试')
    return job, False


def settle(db, job_id, attempt, success, payload):
    """任务终态结算。fencing：仅持有当前代次(attempt)且任务仍在 running 的执行者可写终态。

    成功：预占转实耗（整批处理，实耗=预占事件数），写入计算结果。
    失败：全额归还预占，写入错误。每个台账只结算一次；失联旧执行者的晚回结果
    因代次/状态不匹配而作废，既不覆盖新结果也不重复结算。
    返回 True 表示本次调用完成了结算；False 表示结果被丢弃（旧代次或已结算）。
    """
    ledger = db.scalar(select(QuotaLedger).where(QuotaLedger.job_id == job_id))
    if ledger is None:
        return False  # 额度功能上线前的历史任务无台账，不追扣
    if ledger.status != 'reserved':
        return False  # 已结算：幂等跳过，晚到回调不重复处理

    now = time.time()
    if success:
        values = {'status': 'completed', 'result': payload, 'error': '',
                  'finished_at': now}
    else:
        values = {'status': 'failed', 'result': {}, 'error': payload,
                  'finished_at': now}
    stmt = update(Job).where(
        Job.id == job_id, Job.attempt == attempt,
        Job.status == 'running').values(**values)
    if db.execute(stmt).rowcount != 1:
        return False  # fencing 失败：任务已被中断恢复并换代，旧结果丢弃

    ledger.updated_at = now
    if success:
        ledger.consumed = ledger.event_count  # 整批处理完成，预占转实耗
        ledger.status = 'settled'
    else:
        ledger.consumed = 0
        ledger.status = 'released'           # 失败全额归还
    db.flush()
    return True


def dashboard(db, user_id):
    """工作台额度视图：剩余额度、占用任务与结算明细。"""
    setting = db.scalar(select(QuotaSetting).where(QuotaSetting.user_id == user_id))
    limit = setting.daily_limit if setting else DEFAULT_DAILY_LIMIT
    quota_date = today_key()
    rows = list(db.scalars(select(QuotaLedger).where(
        QuotaLedger.user_id == user_id, QuotaLedger.quota_date == quota_date)))

    reserved = sum(r.event_count for r in rows if r.status == 'reserved')
    settled = sum(r.consumed for r in rows if r.status == 'settled')
    released = sum(r.event_count for r in rows if r.status == 'released')
    used = sum((r.event_count if r.status == 'reserved' else r.consumed)
               for r in rows)

    job_ids = [r.job_id for r in rows]
    jobs = {j.id: j for j in db.scalars(select(Job).where(Job.id.in_(job_ids)))} \
        if job_ids else {}
    details = []
    for r in sorted(rows, key=lambda x: x.id, reverse=True):
        j = jobs.get(r.job_id)
        details.append({
            'ledger_id': r.id, 'job_id': r.job_id,
            'job_name': j.name if j else f'#{r.job_id}',
            'status': r.status, 'job_status': j.status if j else 'missing',
            'event_count': r.event_count, 'consumed': r.consumed,
            'quota_date': r.quota_date, 'created_at': r.created_at,
            'updated_at': r.updated_at,
        })
    return {
        'quota_date': quota_date,
        'daily_limit': limit,
        'reserved': reserved,       # 受理中任务的预占
        'settled': settled,         # 已完成实耗
        'released': released,       # 失败归还
        'used': used,               # 当前占用（预占+实耗）
        'remaining': max(0, limit - used),
        'details': details,
    }


def set_limit(db, user_id, limit, actor):
    if not isinstance(limit, int) or limit < 0 or limit > 1_000_000:
        raise HTTPException(422, '每日额度须为 0~1000000 的整数')
    setting = get_setting(db, user_id)
    setting.daily_limit = limit
    setting.updated_by = actor
    setting.updated_at = time.time()
    db.flush()
    return setting
