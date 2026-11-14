import {quotas} from '../api/entities.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {esc,fmt,date,notify} from '../utils/dom.js';
import {submit} from '../hooks/forms.js';
const STATUS_TEXT={reserved:'预占中',settled:'已实耗',released:'已归还',queued:'排队中',running:'计算中',completed:'已完成',failed:'执行失败'};
const STATUS_CLASS={reserved:'reserved',settled:'settled',released:'released'};

function metric(label,value,small=''){
 return '<div><span>'+esc(label)+'</span><strong>'+esc(fmt(value))+'<small>'+esc(small)+'</small></strong></div>';
}

export async function quotaPage(){
 const mine=await quotas.mine();
 const isAdmin=session.user.role==='admin';
 const users=isAdmin?await quotas.users():[];
 const detailRows=mine.details.map(d=>({
  job_id:d.job_id, job_name:d.job_name, status_text:STATUS_TEXT[d.status]||d.status,
  row_class:'ledger-'+(STATUS_CLASS[d.status]||''),
  job_status_text:STATUS_TEXT[d.job_status]||d.job_status,
  event_count:d.event_count, consumed:d.consumed, quota_date:d.quota_date, updated_at:date(d.updated_at)
 }));
 const columns=[
  {key:'job_id',label:'任务编号'},{key:'job_name',label:'占用任务'},
  {key:'status_text',label:'台账状态'},{key:'job_status_text',label:'任务状态'},
  {key:'event_count',label:'预占事件'},{key:'consumed',label:'实耗事件'},
  {key:'quota_date',label:'受理日'},{key:'updated_at',label:'最近结算'}
 ];
 return heading('06 / QUOTA LEDGER','每日事件额度与结算台账',
  '任务受理时按批次事件数预占当日额度：完成转实耗，失败归还，超额拒绝且不生成任务。跨日恢复仍结算到受理日，调额不影响已受理任务。')
 +'<section class="result-lead"><p class="eyebrow">TODAY '+esc(mine.quota_date)+'</p><h2>今日剩余额度 '+esc(fmt(mine.remaining))+' / '+esc(fmt(mine.daily_limit))+' 个事件</h2><div class="metrics">'
 +metric('每日额度',mine.daily_limit,'事件')+metric('当前占用',mine.used,'事件')
 +metric('其中预占',mine.reserved,'事件')+metric('累计实耗',mine.settled,'事件')
 +metric('失败归还',mine.released,'事件')+metric('剩余可受理',mine.remaining,'事件')
 +'</div></section>'
 +'<section class="panel"><div class="section-title"><h2>占用任务与结算明细</h2><span>'+mine.details.length+' 条台账（仅受理日 '+esc(mine.quota_date)+'）</span></div>'
 +(detailRows.length?'<div class="table-wrap"><table><thead><tr>'+columns.map(c=>'<th>'+esc(c.label)+'</th>').join('')+'</tr></thead><tbody>'
 +detailRows.map(r=>'<tr class="'+esc(r.row_class)+'">'+columns.map(c=>'<td>'+esc(fmt(r[c.key]))+'</td>').join('')+'</tr>').join('')
 +'</tbody></table></div>':'<div class="empty">今日暂无受理任务。到分析工作台提交计算后，这里会出现预占与结算记录。</div>')
 +'<p class="hint">台账按受理日归属：即使任务跨日才恢复执行，实耗或归还仍计入受理当天，不占用恢复当天的额度。</p></section>'
 +(isAdmin?'<section class="panel"><div class="section-title"><h2>账号每日额度设置</h2><span>调低额度不撤销已受理任务</span></div><div class="table-wrap"><table><thead><tr><th>账号</th><th>角色</th><th>每日额度</th><th>今日已用</th><th>今日剩余</th><th>调整额度</th></tr></thead><tbody>'
 +users.map(u=>'<tr><td>'+esc(u.name)+'<br><small>'+esc(u.email)+'</small></td><td>'+esc(u.role)+'</td><td>'+esc(fmt(u.daily_limit))+'</td><td>'+esc(fmt(u.used))+'</td><td>'+esc(fmt(u.remaining))+'</td><td><form class="limit-form" data-user="'+u.id+'"><input name="daily_limit" type="number" min="0" max="1000000" value="'+u.daily_limit+'" required><button class="button small" type="submit">保存</button></form></td></tr>').join('')
 +'</tbody></table></div><p class="hint">新额度仅对之后的受理生效；正在排队或计算的任务继续按原预占结算。额度为 0 时该账号的新受理会被直接拒绝。</p></section>':'')
 +'<section class="method-sheet compact"><p class="eyebrow">SETTLEMENT RULE</p><h3>额度口径</h3><p>预占量等于受理批次的事件数；分析成功后整批转为实耗，分析失败则全额归还。同一受理重试（相同 Idempotency-Key）只建一个任务、只占一次额度；执行者中断后任务自动恢复并换代，失联执行者晚回的结果会被作废，不会重复结算或覆盖新结果。</p></section>';
}

export function bindQuota(refresh){
 document.querySelectorAll('.limit-form').forEach(form=>{
  submit(form,async f=>{
   const id=Number(form.dataset.user);
   const daily_limit=Number(f.get('daily_limit'));
   await quotas.setLimit(id,daily_limit);
   notify('每日额度已更新（仅影响新受理）');
   await refresh();
  });
 });
}
