from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from src.config.database import get_db
from src.models import User
from src.middlewares.auth import current_user, admin_only
from src.services import quota
from src.services.audit import record

router = APIRouter(prefix='/quotas')


class LimitInput(BaseModel):
    user_id: int
    daily_limit: int = Field(ge=0, le=1_000_000)


@router.get('/me')
def my_quota(db=Depends(get_db), u=Depends(current_user)):
    """工作台：剩余额度、占用任务与结算明细。"""
    return quota.dashboard(db, u.id)


@router.get('/users')
def list_limits(db=Depends(get_db), u=Depends(admin_only)):
    users = list(db.scalars(select(User).order_by(User.id)))
    out = []
    for user in users:
        view = quota.dashboard(db, user.id)
        out.append({'user_id': user.id, 'email': user.email, 'name': user.name,
                    'role': user.role, 'daily_limit': view['daily_limit'],
                    'used': view['used'], 'remaining': view['remaining']})
    return out


@router.get('/users/{user_id}')
def user_quota(user_id: int, db=Depends(get_db), u=Depends(admin_only)):
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(404, '账号不存在')
    return quota.dashboard(db, user_id)


@router.put('/users/{user_id}/limit')
def update_limit(user_id: int, data: LimitInput, db=Depends(get_db),
                 u=Depends(admin_only)):
    """调整每日额度：只影响新受理，已受理任务与既有台账不撤销、不追扣。"""
    target = db.get(User, user_id)
    if not target or data.user_id != user_id:
        raise HTTPException(404, '账号不存在')
    before = quota.dashboard(db, user_id)['daily_limit']
    setting = quota.set_limit(db, user_id, data.daily_limit, u.email)
    record(db, u.email, 'quota.limit.update',
           {'user_id': user_id, 'from': before, 'to': setting.daily_limit})
    db.commit()
    return {'user_id': user_id, 'daily_limit': setting.daily_limit,
            'updated_by': setting.updated_by, 'updated_at': setting.updated_at}
