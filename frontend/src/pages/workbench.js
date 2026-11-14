import {datasets,jobs,quotas} from '../api/entities.js';
import {workspace} from '../stores/workspace.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {esc,fmt,notify} from '../utils/dom.js';
import {submit} from '../hooks/forms.js';
export async function workbench(){
 const [list,quota]=await Promise.all([datasets.list(),quotas.mine()]);
 workspace.datasets=list;workspace.quota=quota;const m=workspace.meta;
 const selected=list[0];
 const need=selected?selected.rows:0;
 const enough=quota.remaining>=need;
 return heading('03 / ANALYSIS LAB',m.workbench_title,'选择业务批次和分析口径。参数、输入指纹与计算结论随任务保存，便于复核和复现。')
 +'<section class="result-lead"><p class="eyebrow">TODAY '+esc(quota.quota_date)+' · 每日事件额度</p><h2>今日剩余可受理 '+esc(fmt(quota.remaining))+' / '+esc(fmt(quota.daily_limit))+' 个事件（预占 '+esc(fmt(quota.reserved))+' · 实耗 '+esc(fmt(quota.settled))+' · 归还 '+esc(fmt(quota.released))+'）</h2><p class="hint">受理即按所选批次事件数预占额度；成功转实耗、失败归还。超额请求会被拒绝且不生成任务，详见 <a href="#/quotas">额度台账 ↗</a>。</p></section>'
 +'<div class="workbench-grid"><section class="panel"><form id="job-form"><label>分析任务名称<input name="name" required minlength="2" value="'+esc(m.name)+' · 复核试算"></label><label>数据批次<select name="dataset_id" required>'+workspace.datasets.map(d=>'<option value="'+d.id+'" data-rows="'+d.rows+'">'+esc(d.name)+' · '+d.rows+' 条</option>').join('')+'</select></label><div class="parameter-grid">'+m.parameters.map(p=>'<label>'+esc(p.label)+(p.type==='select'?'<select name="param-'+esc(p.key)+'">'+p.options.map(o=>'<option value="'+esc(o.value)+'" '+(o.value==p.default?'selected':'')+'>'+esc(o.label)+'</option>').join('')+'</select>':'<input type="number" step="any" name="param-'+esc(p.key)+'" value="'+esc(p.default)+'" min="'+esc(p.min??'')+'" max="'+esc(p.max??'')+'" required>')+'</label>').join('')+'</div><p id="quota-hint" class="'+(enough?'hint':'warning')+'">本批次将预占 <strong>'+esc(fmt(need))+'</strong> 个事件额度。'+(enough?'受理后剩余约 '+esc(fmt(Math.max(0,quota.remaining-need)))+' 个。':'剩余额度不足，提交将被拒绝。')+'</p><button type="submit" class="primary" '+(session.user.role==='viewer'||!workspace.datasets.length||!enough?'disabled':'')+'>'+esc(m.action_label||'执行分析')+' ↗</button><p class="hint">提交后由后台任务计算。观察员可查看参数和结果，无法提交。同一提交的网络重试不会重复建任务或重复占额。</p></form></section><section class="method-sheet"><p class="eyebrow">METHOD / BUSINESS RULE</p><h2>'+esc(m.method)+'</h2><p>'+esc(m.tagline)+'</p><h3>业务判读边界</h3><ul>'+m.limitations.map(v=>'<li>'+esc(v)+'</li>').join('')+'</ul><div class="method-trace"><span>输入</span><strong>'+esc(m.dataset_name)+'</strong><span>输出</span><strong>可复核结果 / 参数版本 / 分发归档</strong></div></section></div>';
}
export function bindWorkbench(){
 const form=document.querySelector('#job-form');
 form?.querySelector('[name=dataset_id]')?.addEventListener('change',e=>{
  const rows=Number(e.target.selectedOptions[0].dataset.rows||0);
  const q=workspace.quota;const ok=q.remaining>=rows;
  const hint=document.querySelector('#quota-hint');
  if(hint){hint.className=ok?'hint':'warning';hint.innerHTML='本批次将预占 <strong>'+fmt(rows)+'</strong> 个事件额度。'+(ok?'受理后剩余约 '+fmt(Math.max(0,q.remaining-rows))+' 个。':'剩余额度不足，提交将被拒绝。');}
  form.querySelector('[type=submit]').disabled=!ok;
 });
 submit(form,async f=>{
  const params={};for(const p of workspace.meta.parameters){const value=f.get('param-'+p.key);params[p.key]=p.type==='number'?Number(value):value;}
  const key=(crypto.randomUUID?crypto.randomUUID():(Date.now()+'-'+Math.random().toString(16).slice(2)));
  try{
   const j=await jobs.create({name:f.get('name'),dataset_id:Number(f.get('dataset_id')),params},key);
   workspace.selectedJob=j.id;notify(j.replayed?'该受理已存在，返回原任务（未重复占额）':'分析任务已提交，额度已预占');location.hash='#/results/'+j.id;
  }catch(e){notify(e.message,true);}
 });
}
