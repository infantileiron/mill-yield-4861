import {api,post} from './api/client.js';
import {batches,inspections,reviews,index} from './api/business.js';
import {state,refresh,refreshIndex} from './stores/business.js';
import {shell} from './components/common/shell.js';
import {login} from './pages/login.js';
import {batchesPage} from './pages/batches.js';
import {inspectionsPage} from './pages/inspections.js';
import {diagnosisPage} from './pages/diagnosis.js';
import {reportsPage} from './pages/reports.js';
import {formData,notify,paint} from './hooks/forms.js';
import {route} from './router/router.js';

const app=document.getElementById('app');let timer;
async function render(){
  clearTimeout(timer);
  if(!localStorage.getItem('mill-token')){
    app.innerHTML=login();
    document.getElementById('login-form').onsubmit=async e=>{
      e.preventDefault();
      try{const d=await post('/auth/login',formData(e.target));localStorage.setItem('mill-token',d.access_token);state.user=d.user;await render();}
      catch(err){document.getElementById('login-error').textContent=err.message;}
    };
    return;
  }
  try{
    state.user=await api('/auth/me');await refresh();
    const p=route();
    app.innerHTML=shell(p,state.user,({batches:batchesPage,inspections:inspectionsPage,diagnosis:diagnosisPage,reports:reportsPage}[p])(state));
    bind();paint();
    const busy=state.reviews.some(x=>['queued','running'].includes(x.status))||state.indexStatus?.status==='building';
    if(busy)timer=setTimeout(render,1500);
  }catch(e){app.innerHTML='<div class="fatal">数据暂未加载。<button onclick="location.reload()">重新加载</button></div>';}
}
function bind(){
  document.getElementById('logout').onclick=()=>{localStorage.removeItem('mill-token');state.selected=null;render();};
  const bindForm=(id,fn,nums=[])=>{const f=document.getElementById(id);if(f)f.onsubmit=async e=>{e.preventDefault();const btn=f.querySelector('button');btn.disabled=true;try{await fn(formData(f,nums));await render();notify('已保存，数据与复核记录已更新。');}catch(err){notify(err.message,true);btn.disabled=false;}};};
  bindForm('batch-form',batches.create,['tonnes']);
  bindForm('inspection-form',d=>{
    // datetime-local 是本地时间；转换为带时区的 ISO 时刻，留空则作为旧记录。
    if(!d.sampled_at)delete d.sampled_at;else d.sampled_at=new Date(d.sampled_at).toISOString();
    return inspections.create(d);
  },['batch_id','flour_yield','moisture']);
  bindForm('review-form',async d=>{state.selected=await reviews.create(d);});
  bindForm('indexed-review-form',async d=>{state.selected=await reviews.createIndexed(d);},['window_hours']);
  const approval=document.getElementById('approve-form');
  if(approval)bindForm('approve-form',d=>reviews.approve(approval.dataset.id,d.note));

  const build=document.getElementById('index-build');
  if(build)build.onclick=async()=>{
    build.disabled=true;
    try{await index.build(state.indexStatus?.window_hours||24);await refreshIndex();await render();notify('索引构建已提交，完成后自动切换；期间继续使用上一个完整水位。');}
    catch(e){notify(e.message,true);build.disabled=false;}
  };
  const sync=document.getElementById('index-sync');
  if(sync)sync.onclick=async()=>{
    sync.disabled=true;
    try{const r=await index.sync();await refreshIndex();await render();notify(`追平完成：新增 ${r.inserted} 条，过期 ${r.expired} 条。`);}
    catch(e){notify(e.message,true);sync.disabled=false;}
  };
  const line=document.getElementById('index-line');
  if(line)line.onchange=async()=>{state.indexLine=line.value;await refreshIndex();await render();};

  document.querySelectorAll('[data-review]').forEach(b=>b.onclick=async()=>{
    try{state.selected=await reviews.get(b.dataset.review);await render();}catch(e){notify(e.message,true);}
  });
  const dl=document.getElementById('download');
  if(dl)dl.onclick=async()=>{try{await reviews.download(dl.dataset.id);notify('CSV 已生成并下载。');}catch(e){notify(e.message,true);}};
}
window.addEventListener('hashchange',()=>{state.selected=null;render();});
render();
