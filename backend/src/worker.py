import time, json, logging
from pathlib import Path
from sqlalchemy import select, update
from redis import Redis
from src.config.database import Session
from src.config.settings import REDIS_URL
from src.models import Job, Dataset, Report
from src.services.business import analyze
from src.services.reports import deliver
from src.services.audit import record
from src.services import quota
logging.basicConfig(level=logging.INFO)


def execute(job_id):
    with Session() as db:
        j = db.get(Job, job_id)
        # 已终态（含重复投递、旧执行者晚回）直接忽略，不重复结算。
        if not j or j.status != 'queued':
            return
        token = j.attempt
        # 条件认领：只有 queued 能转 running，并发受理/重复投递不会超领。
        claimed = db.execute(
            update(Job).where(Job.id == job_id, Job.status == 'queued')
            .values(status='running')).rowcount
        if claimed != 1:
            return
        dataset = db.get(Dataset, j.dataset_id)
        params = dict(j.params)
        db.commit()

    # 计算放在事务外，避免长计算占用数据库连接/锁。
    try:
        result = analyze(dataset.records, params)
        json.dumps(result, allow_nan=False)
        success, payload, err = True, result, ''
    except Exception as e:
        logging.exception('Analysis failed')
        success, payload, err = False, str(e)[:990], str(e)[:990]

    with Session() as db:
        # fencing 结算：仅当前代次可写终态并完成台账流转；旧执行者晚回结果作废。
        applied = quota.settle(db, job_id, token, success, payload)
        if not applied:
            logging.warning('Drop stale result for job %s (attempt %s)', job_id, token)
            record(db, 'worker', 'job.stale_result_dropped',
                   {'id': job_id, 'attempt': token})
            db.commit()
            return
        record(db, 'worker',
               'job.completed' if success else 'job.failed',
               {'id': job_id, **({'error': err} if err else {})})
        db.commit()


def tick():
    with Session() as db:
        queued = list(db.scalars(select(Job.id).where(Job.status == 'queued').limit(3)))
    for job_id in queued:
        execute(job_id)
    with Session() as db:
        for r in db.scalars(select(Report).where(Report.enabled == True, Report.next_run <= time.time())):
            d = deliver(db, r)
            if d:
                record(db, 'worker', 'report.delivered', {'id': r.id, 'delivery': d.id})
        db.commit()


def recover_interrupted(db):
    """中断恢复：仍在 running 的任务回到排队并递增执行代次（fencing token）。"""
    stale = list(db.scalars(select(Job).where(Job.status == 'running')))
    for j in stale:
        j.status = 'queued'
        j.attempt += 1
        record(db, 'worker', 'job.recovered', {'id': j.id, 'attempt': j.attempt})
    db.commit()
    return len(stale)


def main():
    r = Redis.from_url(REDIS_URL, socket_timeout=3)
    # This Compose stack has a single worker. A crash must not strand its jobs.
    with Session() as db:
        recover_interrupted(db)
    while True:
        Path('/tmp/heartbeat').touch()
        try:
            item = r.blpop('analysis:queue', timeout=1)
            if item:
                execute(int(item[1]))
            tick()
        except Exception:
            logging.exception('Worker retry')
            time.sleep(2)


if __name__ == '__main__':
    main()
