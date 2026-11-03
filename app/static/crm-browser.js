'use strict';
let browserTimer, browserPreview=null, browserPendingLead=null, browserReading=false, browserImporting=false, browserPolling=false, browserLoadError='';
function browserControls(status){
  $('#browser-start').disabled=status.phase==='starting';
  $('#browser-stop').hidden=status.phase==='closed';
  $('#browser-status').textContent=status.error||browserLoadError||({closed:'Выберите сделку — окно amoCRM откроется автоматически.',starting:'Открываем amoCRM. Если потребуется, войдите в аккаунт в окне Chrome.',open:'Окно amoCRM открыто. Откройте беседу в карточке сделки, если история не появилась автоматически.'}[status.phase]||'Проверяем состояние окна…');
  $('#browser-status').classList.toggle('status-error',!!(status.error||browserLoadError));
  if(status.phase!=='closed'||status.jobs.length)$('#browser-panel').hidden=false;
  const stages={waiting_response:'Ожидаем историю. Откройте беседу в окне amoCRM.',received_response:'Получили историю, читаем сообщения…',messages_loaded:'Можно посмотреть переписку и перейти к проверке',empty_response:'Сообщений пока нет. Откройте нужную беседу в amoCRM.',request_error:'Проверьте вход в amoCRM и доступ к сделке',parse_error:'Не удалось распознать сообщения'};
  $('#browser-jobs').innerHTML=status.jobs.map(j=>`<article class="crm-candidate browser-job"><div class="panel-head"><div><strong>${esc(j.lead_name||`Сделка #${j.lead_id}`)}</strong><p class="hint">${esc(stages[j.stage]||'Ожидаем историю')}</p></div>${badge(`${j.message_count} сообщений`,j.error?'amber':j.message_count>=2?'green':'neutral')}</div>${j.error?`<p class="status-error">${esc(j.error)}</p>`:''}<div class="job-actions"><button class="primary" data-browser-preview="${esc(j.id)}" ${j.error||j.message_count<2?'disabled':''}>Посмотреть переписку →</button><button class="text-link" data-browser-read="${j.lead_id}">Загрузить заново</button></div><details class="technical-details"><summary>Подробности загрузки</summary><p>Ответов истории: ${j.history_responses??0}${j.last_http_status?` · HTTP ${j.last_http_status}`:''}</p></details></article>`).join('');
}
async function browserPoll(){
  if(browserPolling)return;
  browserPolling=true;clearTimeout(browserTimer);
  const pendingAtRequest=browserPendingLead;
  try{
    const status=await api('/api/crm/browser/status');
    browserControls(status);
    if(browserPendingLead!==null&&status.phase==='open'&&!browserReading){const leadId=browserPendingLead;browserPendingLead=null;await browserLoadLead(leadId);}
    else if(status.phase==='closed'&&browserPendingLead!==null&&pendingAtRequest===browserPendingLead){browserPendingLead=null;$('#browser-status').textContent=status.error||'Окно amoCRM закрыто. Нажмите «Открыть чат» ещё раз.';}
    if(status.phase!=='closed'||browserPendingLead!==null)browserTimer=setTimeout(browserPoll,2000);
  }catch(e){$('#browser-status').textContent=e.message;browserTimer=setTimeout(browserPoll,4000);}
  finally{browserPolling=false;}
}
async function browserLoadLead(leadId){
  browserReading=true;$('#browser-status').textContent='Открываем чат выбранной сделки…';
  try{browserControls(await api('/api/crm/browser/read',{lead_id:leadId}));}
  catch(e){browserLoadError=e.message;$('#browser-status').textContent=e.message;$('#browser-status').classList.add('status-error');notify(e.message,true);}
  finally{browserReading=false;}
}
async function browserOpenLead(leadId){
  if(browserReading||browserPendingLead!==null)return;
  browserPendingLead=leadId;browserLoadError='';browserPreview=null;$('#browser-preview').hidden=true;$('#browser-complete').checked=false;
  $('#browser-panel').hidden=false;$('#notice').hidden=true;
  $('#browser-panel').scrollIntoView({behavior:'smooth',block:'start'});
  $('#browser-status').textContent='Подготавливаем окно amoCRM…';
  try{browserControls(await api('/api/crm/browser/start',{}));await browserPoll();}
  catch(e){browserPendingLead=null;browserLoadError=e.message;$('#browser-status').textContent=e.message;$('#browser-status').classList.add('status-error');notify(e.message,true);}
}
$('#browser-start').onclick=async()=>{if(browserReading||browserPendingLead!==null)return;$('#browser-start').disabled=true;try{browserControls(await api('/api/crm/browser/start',{}));await browserPoll();}catch(e){notify(e.message,true);}finally{$('#browser-start').disabled=false;}};
$('#browser-stop').onclick=async()=>{browserPendingLead=null;try{await api('/api/crm/browser/stop',{});browserPreview=null;$('#browser-preview').hidden=true;await browserPoll();}catch(e){notify(e.message,true);}};
document.addEventListener('click',async e=>{
  const read=e.target.closest('[data-browser-read]');
  if(read&&!read.disabled)await browserOpenLead(Number(read.dataset.browserRead));
  const preview=e.target.closest('[data-browser-preview]');
  if(preview&&!preview.disabled){
    preview.disabled=true;
    try{const result=await api('/api/crm/browser/preview',{job_id:preview.dataset.browserPreview});if(result.error)throw new Error(result.error);browserPreview={job_id:preview.dataset.browserPreview,revision:result.revision};$('#browser-preview-title').textContent=result.dialogue.title;$('#browser-preview-messages').innerHTML=result.dialogue.messages.map(m=>`<div class="message ${m.role}"><div>${esc(m.author)} · ${esc(m.time)}</div><p>${esc(m.text)}</p></div>`).join('');$('#browser-preview').hidden=false;$('#browser-complete').checked=false;$('#browser-preview').scrollIntoView({behavior:'smooth',block:'start'});}
    catch(err){notify(err.message,true);}finally{preview.disabled=false;}
  }
});
async function browserImport(startCheck){
  if(!browserPreview||browserImporting)return;
  browserImporting=true;$('#browser-import').disabled=true;$('#browser-save').disabled=true;
  try{const scope=$('#browser-scope').value;const result=await api('/api/crm/browser/import',{...browserPreview,scope,complete:$('#browser-complete').checked});$('#scope-filter').value=scope;await refresh();selectedId=result.id;assessmentBases.set(result.id,scope==='demo'?'demo':'auto');renderDetail();show('detail');if(startCheck)$('#auto-check').click();else notify('Переписка сохранена. Проверку можно запустить из её карточки.');}
  catch(e){notify(e.message,true);}
  finally{browserImporting=false;$('#browser-import').disabled=false;$('#browser-save').disabled=false;}
}
$('#browser-import').onclick=()=>browserImport(true);
$('#browser-save').onclick=()=>browserImport(false);
appReady.then(browserPoll);
