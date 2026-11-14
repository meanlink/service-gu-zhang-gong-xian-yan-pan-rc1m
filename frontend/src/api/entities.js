import {api} from './client.js';
export const datasets={list:()=>api('/datasets'),get:id=>api('/datasets/'+id),create:data=>api('/datasets',{method:'POST',body:JSON.stringify(data)})};
export const jobs={list:()=>api('/jobs'),get:id=>api('/jobs/'+id),create:(data,idempotencyKey)=>api('/jobs',{method:'POST',headers:idempotencyKey?{'Idempotency-Key':idempotencyKey}:{},body:JSON.stringify(data)}),approve:id=>api('/jobs/'+id+'/approve',{method:'POST'})};
export const reports={list:()=>api('/reports'),create:data=>api('/reports',{method:'POST',body:JSON.stringify(data)}),toggle:id=>api('/reports/'+id+'/toggle',{method:'POST'}),deliveries:()=>api('/deliveries')};
export const quotas={mine:()=>api('/quotas/me'),users:()=>api('/quotas/users'),user:id=>api('/quotas/users/'+id),setLimit:(id,daily_limit)=>api('/quotas/users/'+id+'/limit',{method:'PUT',body:JSON.stringify({user_id:id,daily_limit})})};
