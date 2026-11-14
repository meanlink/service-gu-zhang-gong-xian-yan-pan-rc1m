from fastapi import APIRouter, Depends, HTTPException, Header, Response
from sqlalchemy import select
from src.config.database import get_db
from src.models import Job
from src.schemas.job import JobInput
from src.middlewares.auth import current_user, can_write, admin_only
from src.services import jobs, quota
from src.services.audit import record
from src.utils.serialization import job_dict
router = APIRouter(prefix='/jobs')


@router.get('')
def listing(db=Depends(get_db), u=Depends(current_user)):
    return [job_dict(j) for j in db.scalars(select(Job).order_by(Job.id.desc()).limit(100))]


@router.post('', status_code=201)
def create(data: JobInput, response: Response, db=Depends(get_db), u=Depends(can_write),
           idempotency_key: str | None = Header(default=None, alias='Idempotency-Key')):
    # 重试携带相同 Idempotency-Key 时返回原任务：不重复建任务、不重复占额。
    # 锁覆盖“校验+预占+提交”整段，保证并发受理不会超领（Postgres 行锁兜底跨进程）。
    key = (idempotency_key or '').strip()[:80] or None
    with quota.reservation_lock():
        j, replayed = jobs.create(db, data, u, key)
        record(db, u.email,
               'job.replay' if replayed else 'job.create',
               {'id': j.id, 'params': j.params, 'event_count': j.event_count,
                **({'idempotency_key': key} if key else {})})
        db.commit()
    if not replayed:
        jobs.enqueue(j.id)
    else:
        response.status_code = 200  # 重放既有受理，不是新建
    payload = job_dict(j)
    payload['replayed'] = replayed
    return payload


@router.get('/{id}')
def detail(id: int, db=Depends(get_db), u=Depends(current_user)):
    j = db.get(Job, id)
    if not j:
        raise HTTPException(404, '分析任务不存在')
    return job_dict(j)


@router.post('/{id}/approve')
def approve(id: int, db=Depends(get_db), u=Depends(admin_only)):
    j = db.get(Job, id)
    if not j:
        raise HTTPException(404, '分析任务不存在')
    if j.status != 'completed':
        raise HTTPException(409, '仅已完成结果可以批准')
    j.approved = True
    record(db, u.email, 'job.approve', {'id': id})
    db.commit()
    return job_dict(j)
