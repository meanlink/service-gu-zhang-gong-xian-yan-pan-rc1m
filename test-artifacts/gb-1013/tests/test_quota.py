"""每日事件额度：预占/实耗/归还、超额拒绝、并发与幂等、中断恢复与失联作废、
跨日结算归属、调额不撤销、历史不追扣的端到端测试。

运行（在仓库根目录）：
    python3 -m pytest test-artifacts/gb-1013/tests/test_quota.py -q
"""
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from src.config.database import Session
from migrations.initial import migrate
from src.models import User, Dataset, Job, QuotaLedger, QuotaSetting
from src.services.business import seed_records, metadata, analyze
from src.services import quota, jobs as job_service
from src.services.audit import record
from src.utils.security import hash_password, token_for
from src.schemas.job import JobInput
import src.worker as worker


@pytest.fixture()
def ctx():
    # 每个测试用例使用全新的内存外 sqlite 文件库（conftest 已固定路径），重建表。
    from src.config.database import Base
    Base.metadata.drop_all(bind=__import__('src.config.database', fromlist=['engine']).engine)
    migrate()
    with Session() as db:
        admin = User(email='admin@demo.local', name='负责人', role='admin',
                     password=hash_password('Demo123!'))
        analyst = User(email='analyst@demo.local', name='分析员', role='analyst',
                       password=hash_password('Demo123!'))
        viewer = User(email='viewer@demo.local', name='观察员', role='viewer',
                      password=hash_password('Demo123!'))
        db.add_all([admin, analyst, viewer])
        db.flush()
        rows = seed_records()
        ds = Dataset(name='演示批次', source='unit.csv', records=rows,
                     checksum='x' * 64, created_at=time.time())
        db.add(ds)
        db.flush()
        quota.set_limit(db, admin.id, 2000, 'seed')
        quota.set_limit(db, analyst.id, 500, 'seed')
        quota.set_limit(db, viewer.id, 200, 'seed')
        db.commit()
        ids = {'admin': admin.id, 'analyst': analyst.id, 'viewer': viewer.id,
               'dataset': ds.id, 'events': len(rows)}
    from src.main import app
    client = TestClient(app)
    headers = {role: {'Authorization': 'Bearer ' + token_for(_user(ids[role]))}
               for role in ('admin', 'analyst', 'viewer')}
    return {'client': client, 'headers': headers, **ids}


def _user(uid):
    with Session() as db:
        return db.get(User, uid)


def _job_input(ctx, name='试算'):
    params = {p['key']: p['default'] for p in metadata()['parameters']}
    return JobInput(name=name, dataset_id=ctx['dataset'], params=params)


def _accept(ctx, role='analyst', key=None):
    # 与路由相同：锁覆盖“校验+预占+提交”，模拟单进程部署的并发串行化
    # （Postgres 部署由 QuotaSetting 行锁提供跨进程保证）。
    with quota.reservation_lock():
        with Session() as db:
            user = db.get(User, ctx[role])
            job, replayed = job_service.create(db, _job_input(ctx), user, key)
            record(db, user.email, 'job.create', {'id': job.id})
            db.commit()
            return job.id, replayed


# 1. 受理预占：建任务 + 台账，占用按批次事件数，剩余额度正确。
def test_reserve_on_accept(ctx):
    job_id, _ = _accept(ctx)
    with Session() as db:
        view = quota.dashboard(db, ctx['analyst'])
        assert view['daily_limit'] == 500
        assert view['reserved'] == ctx['events']
        assert view['used'] == ctx['events']
        assert view['remaining'] == 500 - ctx['events']
        assert view['settled'] == 0
        ledger = db.scalar(__import__('sqlalchemy').select(QuotaLedger).where(
            QuotaLedger.job_id == job_id))
        assert ledger.status == 'reserved'
        assert ledger.event_count == ctx['events']
        assert ledger.quota_date == quota.today_key()
        job = db.get(Job, job_id)
        assert job.event_count == ctx['events'] and job.attempt == 1


# 2. 完成转实耗：状态完成、台账 settled、实耗=预占。
def test_success_settles_consumption(ctx):
    job_id, _ = _accept(ctx)
    worker.execute(job_id)
    with Session() as db:
        job = db.get(Job, job_id)
        ledger = db.scalar(__import__('sqlalchemy').select(QuotaLedger).where(
            QuotaLedger.job_id == job_id))
        assert job.status == 'completed' and job.result
        assert ledger.status == 'settled'
        assert ledger.consumed == ctx['events']
        view = quota.dashboard(db, ctx['analyst'])
        assert view['settled'] == ctx['events'] and view['reserved'] == 0
        assert view['used'] == ctx['events']


# 3. 失败归还：任务 failed、台账 released、额度全额返还。
def test_failure_releases_quota(ctx):
    job_id, _ = _accept(ctx)
    with Session() as db:  # 制造必然失败：破坏批次记录
        db.get(Dataset, ctx['dataset']).records = []
        db.commit()
    worker.execute(job_id)
    with Session() as db:
        job = db.get(Job, job_id)
        ledger = db.scalar(__import__('sqlalchemy').select(QuotaLedger).where(
            QuotaLedger.job_id == job_id))
        assert job.status == 'failed' and job.error
        assert ledger.status == 'released' and ledger.consumed == 0
        view = quota.dashboard(db, ctx['analyst'])
        assert view['released'] == ctx['events']
        assert view['used'] == 0 and view['remaining'] == 500


# 4. 超额拒绝：429，且不留下任务或台账。
def test_over_quota_rejected_without_task(ctx):
    with Session() as db:
        quota.set_limit(db, ctx['analyst'], ctx['events'] - 1, 'admin')
        db.commit()
    before_jobs, before_ledgers = _counts()
    with pytest.raises(quota.QuotaExceeded) as exc:
        _accept(ctx)
    assert exc.value.status_code == 429 and '额度不足' in exc.value.detail
    after_jobs, after_ledgers = _counts()
    assert (after_jobs, after_ledgers) == (before_jobs, before_ledgers)


def _counts():
    from sqlalchemy import select, func
    with Session() as db:
        return (db.scalar(select(func.count()).select_from(Job)),
                db.scalar(select(func.count()).select_from(QuotaLedger)))


# 5. 同一次受理重试（相同 Idempotency-Key）不重复建任务、不重复占额。
def test_idempotent_retry(ctx):
    key = 'idem-' + 'a1'
    id1, replay1 = _accept(ctx, key=key)
    id2, replay2 = _accept(ctx, key=key)
    assert id1 == id2 and replay1 is False and replay2 is True
    with Session() as db:
        view = quota.dashboard(db, ctx['analyst'])
        assert view['reserved'] == ctx['events']  # 只占一次
        assert len(view['details']) == 1


# 6. 并发受理不超领：额度只够一个批次时，并行请求一成一拒，无超额台账。
def test_concurrent_accept_does_not_overclaim(ctx):
    with Session() as db:
        quota.set_limit(db, ctx['analyst'], ctx['events'] + 1, 'admin')
        db.commit()

    def one(i):
        try:
            return _accept(ctx, key=f'concurrent-{i}')
        except quota.QuotaExceeded as e:
            return e

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(one, range(4)))
    accepted = [r for r in results if isinstance(r, tuple)]
    rejected = [r for r in results if isinstance(r, quota.QuotaExceeded)]
    assert len(accepted) == 1 and len(rejected) == 3
    with Session() as db:
        view = quota.dashboard(db, ctx['analyst'])
        assert view['used'] <= ctx['events'] + 1
        assert view['remaining'] == 1


# 7. 相同幂等键的并发请求只建一个任务。
def test_concurrent_same_idempotency_key(ctx):
    def one(_):
        return _accept(ctx, key='shared-key')

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(one, range(4)))
    job_ids = {r[0] for r in results}
    assert job_ids and len(job_ids) == 1
    with Session() as db:
        ledgers = db.query(QuotaLedger).filter(
            QuotaLedger.idempotency_key == 'shared-key').count()
        assert ledgers == 1


# 7b. 幂等键按账号隔离：不同账号同键各自独立建任务，互不越权。
def test_idempotency_key_scoped_per_user(ctx):
    analyst_job, replay_a = _accept(ctx, role='analyst', key='dup-key')
    admin_job, replay_b = _accept(ctx, role='admin', key='dup-key')
    assert replay_a is False and replay_b is False
    assert analyst_job != admin_job
    with Session() as db:
        rows = db.query(QuotaLedger).filter(
            QuotaLedger.idempotency_key == 'dup-key').all()
        assert {r.user_id for r in rows} == {ctx['analyst'], ctx['admin']}
        assert len(rows) == 2


# 8. 中断恢复：running 任务回排队并换代，恢复后仍能完成且只结算一次。
def test_interrupted_recovery_requeues_with_new_attempt(ctx):
    job_id, _ = _accept(ctx)
    worker.execute  # noqa: B018 - ensure module loaded
    # 模拟执行者在计算中崩溃：任务停在 running（attempt=1）。
    with Session() as db:
        j = db.get(Job, job_id)
        j.status = 'running'
        db.commit()
    with Session() as db:
        recovered = worker.recover_interrupted(db)
    assert recovered == 1
    with Session() as db:
        j = db.get(Job, job_id)
        assert j.status == 'queued' and j.attempt == 2
    worker.execute(job_id)
    with Session() as db:
        j = db.get(Job, job_id)
        ledgers = db.query(QuotaLedger).filter(QuotaLedger.job_id == job_id).all()
        assert j.status == 'completed' and j.attempt == 2
        assert len(ledgers) == 1 and ledgers[0].status == 'settled'


# 9. 失联旧执行者晚回结果：fencing 作废，不覆盖新结果、不重复结算。
def test_stale_executor_result_is_dropped(ctx):
    job_id, _ = _accept(ctx)
    with Session() as db:
        j = db.get(Job, job_id)
        j.status = 'running'  # 旧执行者（attempt=1）失联
        db.commit()
    with Session() as db:
        worker.recover_interrupted(db)  # 换代到 2 并重新排队
    worker.execute(job_id)             # 新执行者完成
    with Session() as db:
        good_result = dict(db.get(Job, job_id).result)
        good_attempt = db.get(Job, job_id).attempt
    # 旧执行者带着 attempt=1 晚回：
    with Session() as db:
        applied = quota.settle(db, job_id, attempt=1, success=True,
                               payload={'summary': 'STALE', 'metrics': [], 'table': {}})
        db.commit()
    assert applied is False
    with Session() as db:
        j = db.get(Job, job_id)
        ledger = db.query(QuotaLedger).filter(QuotaLedger.job_id == job_id).one()
        assert j.attempt == good_attempt == 2
        assert j.result == good_result and j.result.get('summary') != 'STALE'
        assert ledger.status == 'settled' and ledger.consumed == ctx['events']
        # 重复晚回同样作废
        assert quota.settle(db, job_id, 2, False, 'late duplicate') is False


# 10. 跨日恢复仍结算到受理日，不占用恢复当天额度。
def test_cross_day_settlement_keeps_acceptance_date(ctx):
    job_id, _ = _accept(ctx)
    acceptance_day = quota.today_key()
    with Session() as db:
        db.get(Job, job_id).status = 'running'
        db.commit()
    with Session() as db:
        worker.recover_interrupted(db)
    next_day = '2099-01-02'
    original = quota.today_key
    quota.today_key = lambda now=None: next_day
    try:
        worker.execute(job_id)
        with Session() as db:
            ledger = db.query(QuotaLedger).filter(QuotaLedger.job_id == job_id).one()
            assert ledger.quota_date == acceptance_day  # 仍归属受理日
            # 恢复当天视图没有任何占用
            view = quota.dashboard(db, ctx['analyst'])
            assert view['quota_date'] == next_day
            assert view['used'] == 0 and view['details'] == []
    finally:
        quota.today_key = original


# 11. 调低额度不撤销已受理任务，且只影响新受理。
def test_lowering_limit_keeps_accepted_jobs(ctx):
    job_id, _ = _accept(ctx)  # 已预占 188
    with Session() as db:
        quota.set_limit(db, ctx['analyst'], 1, 'admin')  # 调到远低于已占用
        db.commit()
    with Session() as db:
        # 已受理任务照常恢复执行并结算
        assert db.get(Job, job_id).status == 'queued'
    worker.execute(job_id)
    with Session() as db:
        assert db.get(Job, job_id).status == 'completed'
        view = quota.dashboard(db, ctx['analyst'])
        assert view['daily_limit'] == 1 and view['settled'] == ctx['events']
    # 新受理被拒
    with pytest.raises(quota.QuotaExceeded):
        _accept(ctx, key='after-limit-cut')


# 12. 已有分析（额度功能上线前的历史任务，无台账）不追扣。
def test_legacy_jobs_not_clawed_back(ctx):
    with Session() as db:
        admin = db.get(User, ctx['admin'])
        legacy = Job(name='历史任务', dataset_id=ctx['dataset'],
                     params={}, status='completed', result={'ok': True},
                     created_by=admin.id, created_at=time.time() - 99999,
                     finished_at=time.time() - 99990,
                     event_count=ctx['events'], attempt=1)
        db.add(legacy)
        db.commit()
        legacy_id = legacy.id
    # 历史任务没有台账，结算直接跳过而非追扣
    with Session() as db:
        assert quota.settle(db, legacy_id, 1, True, {'x': 1}) is False
        view = quota.dashboard(db, ctx['admin'])
        assert view['used'] == 0 and view['details'] == []


# 13. HTTP 端到端：登录、额度查询、受理 429/201、幂等重放与权限。
def test_http_flows(ctx):
    c = ctx['client']
    login = c.post('/auth/login', json={'email': 'analyst@demo.local',
                                        'password': 'Demo123!'})
    assert login.status_code == 200
    h = ctx['headers']['analyst']
    me = c.get('/quotas/me', headers=h).json()
    assert me['daily_limit'] == 500 and me['remaining'] == 500

    body = {'name': '接口试算', 'dataset_id': ctx['dataset'],
            'params': {p['key']: p['default'] for p in metadata()['parameters']}}
    r1 = c.post('/jobs', json=body, headers={**h, 'Idempotency-Key': 'http-1'})
    assert r1.status_code == 201 and r1.json()['replayed'] is False
    r2 = c.post('/jobs', json=body, headers={**h, 'Idempotency-Key': 'http-1'})
    assert r2.status_code == 200 and r2.json()['replayed'] is True
    assert r1.json()['id'] == r2.json()['id']

    # 观察员无受理权限
    assert c.post('/jobs', json=body, headers=ctx['headers']['viewer']).status_code == 403

    # 把额度调到 0 后，新受理被拒且不建任务
    admin_h = ctx['headers']['admin']
    uid = ctx['analyst']
    assert c.put(f'/quotas/users/{uid}/limit',
                 json={'user_id': uid, 'daily_limit': 0},
                 headers=admin_h).status_code == 200
    blocked = c.post('/jobs', json=body, headers={**h, 'Idempotency-Key': 'http-2'})
    assert blocked.status_code == 429
    jobs_now = c.get('/jobs', headers=h).json()
    assert all(j['name'] != '接口试算' or j['id'] == r1.json()['id'] for j in jobs_now)
    # 非管理员不能调额
    assert c.put(f'/quotas/users/{uid}/limit',
                 json={'user_id': uid, 'daily_limit': 9},
                 headers=h).status_code == 403


# 14. HTTP 并发受理：不同键各自独立，受总额度约束不超领。
def test_http_concurrent_same_key(ctx):
    from src.main import app
    h = ctx['headers']['analyst']
    body = {'name': '并发试算', 'dataset_id': ctx['dataset'],
            'params': {p['key']: p['default'] for p in metadata()['parameters']}}

    def call(i):
        # TestClient 绑定线程局部门户，并发时每个线程使用独立实例。
        local = TestClient(app)
        return local.post('/jobs', json=body,
                          headers={**h, 'Idempotency-Key': f'race-{i}'})

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(call, range(4)))
    # 额度 500、批次 188：最多成功 2 个，其余 429
    created = [r for r in responses if r.status_code in (200, 201)]
    rejected = [r for r in responses if r.status_code == 429]
    assert len(created) == 2 and len(rejected) == 2
    with Session() as db:
        view = quota.dashboard(db, ctx['analyst'])
        assert view['used'] == 2 * ctx['events']
        assert len(view['details']) == 2
