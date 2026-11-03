'use strict';
const checkActive=new Set(['queued','checking_login','running','correcting','validating']);
const checkLabels={queued:'Проверка поставлена в очередь…',checking_login:'Проверяем вход через подписку ChatGPT…',running:'ИИ проверяет ответ менеджера по правилам…',correcting:'Уточняем ответ ИИ после проверки ссылок…',validating:'Проверяем цитаты и ссылки на правила…',complete:'Проверка завершена. Результат сохранён.',error:'Проверка не завершена'};
let checkTimer,checkPolling=false;
function renderCheckControls(){
  if(!state)return;
  const button=$('#auto-check'),status=$('#auto-check-status');if(!button||!status)return;
  const job=(state.checks||[]).find(j=>j.dialogue_id===button.dataset.checkDialogue);
  button.disabled=!!job&&checkActive.has(job.status);
  if($('#assessment-basis'))$('#assessment-basis').disabled=button.disabled;
  button.textContent=button.disabled?'Проверяем ответ…':'Проверить ответ менеджера';
  status.textContent=job?(job.status==='error'?job.error:checkLabels[job.status]):'Задание и результат передаются автоматически. Проверка может занять несколько минут.';
  if(job&&checkActive.has(job.status))scheduleCheckPoll();
}
function scheduleCheckPoll(){clearTimeout(checkTimer);checkTimer=setTimeout(pollChecks,1200);}
async function pollChecks(){
  if(checkPolling)return;checkPolling=true;
  try{
    const active=(state.checks||[]).filter(j=>checkActive.has(j.status));let completed=false;
    for(const old of active){
      const job=await api('/api/check/jobs/'+old.id);
      state.checks=state.checks.map(j=>j.id===job.id?job:j);
      if(job.status==='complete')completed=true;
      if(job.status==='error')notify(job.error,true);
    }
    if(completed){await refresh();notify('Разбор готов: оценки, замечания и ссылки на правила сохранены.');}
    renderCheckControls();
    if((state.checks||[]).some(j=>checkActive.has(j.status)))scheduleCheckPoll();
  }catch(e){notify(e.message,true);scheduleCheckPoll();}finally{checkPolling=false;}
}
document.addEventListener('check-controls-rendered',renderCheckControls);
document.addEventListener('click',async e=>{
  const button=e.target.closest('[data-check-dialogue]');if(!button||button.disabled)return;
  button.disabled=true;$('#auto-check-status').textContent='Запускаем проверку…';
  try{
    const job=await api('/api/check/start',{dialogue_id:button.dataset.checkDialogue,basis:$('#assessment-basis')?.value||'auto'});
    state.checks=[job,...(state.checks||[]).filter(j=>j.id!==job.id)];renderCheckControls();
    if(checkActive.has(job.status))scheduleCheckPoll();else if(job.status==='error')notify(job.error,true);else if(job.status==='complete'){await refresh();notify('Разбор готов и сохранён.');}
  }catch(err){button.disabled=false;$('#auto-check-status').textContent=err.message;notify(err.message,true);}
});
appReady.then(()=>{renderCheckControls();if((state?.checks||[]).some(j=>checkActive.has(j.status)))scheduleCheckPoll();});
