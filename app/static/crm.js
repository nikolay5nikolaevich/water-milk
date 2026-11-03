'use strict';
let crmStatus={connected:false}, crmLeads=[], crmPage=0, crmHasMore=false, crmQuery='', crmBusy=false;
const crmSelected=new Set(), crmPreviews=new Map(), crmCandidates=new Map(), crmChosen=new Set();
function crmSetBusy(value){crmBusy=value;document.querySelectorAll('#crm-connect-form button,#crm-deals button,#crm-previews button,#crm-import-bar button').forEach(el=>el.disabled=value);$('#crm-deals').setAttribute('aria-busy',String(value));if(!value)crmCounts();}
function crmCounts(){
  $('#crm-selected-count').textContent=crmSelected.size?`Выбрано: ${crmSelected.size} из 10`:'Для чата нажмите «Открыть чат» в строке сделки';
  $('#crm-preview-selected').disabled=crmBusy||!crmSelected.size;
  $('#crm-dialogue-count').textContent=`Выбрано диалогов: ${crmChosen.size}`;
  $('#crm-import-selected').disabled=crmBusy||!crmChosen.size;
  $('#crm-import-bar').hidden=crmCandidates.size===0;
  $('#crm-select-candidates').checked=crmCandidates.size>0&&crmChosen.size===crmCandidates.size;
  const visible=crmFilteredLeads();$('#crm-check-all').checked=visible.length>0&&visible.every(l=>crmSelected.has(l.id));
}
function crmFilteredLeads(){return crmLeads.filter(l=>($('#crm-user').value==='all'||String(l.responsible_user_id)===$('#crm-user').value)&&($('#crm-pipeline').value==='all'||String(l.pipeline_id)===$('#crm-pipeline').value)&&($('#crm-stage').value==='all'||![142,143].includes(l.status_id)));}
function crmDate(epoch){return epoch?new Date(epoch*1000).toLocaleString('ru-RU',{timeZone:'Europe/Moscow',day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'}):'—';}
function crmRenderLeads(){
  const users=new Map((crmStatus.users||[]).map(u=>[u.id,u.name]));
  const pipelines=new Map((crmStatus.pipelines||[]).map(p=>[p.id,p]));
  const visible=crmFilteredLeads();
  $('#crm-leads').innerHTML=visible.map(l=>{const pipeline=pipelines.get(l.pipeline_id),stage=pipeline?.statuses.find(s=>s.id===l.status_id);return `<tr><td><input type="checkbox" data-lead-check="${l.id}" aria-label="Выбрать сделку ${l.id}" ${crmSelected.has(l.id)?'checked':''}></td><td><button class="row-link" data-browser-read="${l.id}">${esc(l.name||`Сделка #${l.id}`)}</button><small>#${l.id} · <a href="https://${esc(crmStatus.domain)}/leads/detail/${l.id}" target="_blank" rel="noopener noreferrer">В amoCRM ↗</a></small></td><td>${esc(users.get(l.responsible_user_id)||`Сотрудник #${l.responsible_user_id}`)}</td><td>${esc(pipeline?.name||l.pipeline_id)}<small>${esc(stage?.name||l.status_id)}</small></td><td>${esc(crmDate(l.updated_at))}</td><td><div class="lead-actions"><button class="primary" data-browser-read="${l.id}">Открыть чат</button><button class="text-link" data-crm-preview="${l.id}">Из примечаний</button></div></td></tr>`;}).join('');
  $('#crm-empty').hidden=visible.length>0;
  $('#crm-more').hidden=!crmHasMore;
  crmCounts();
}
async function crmLoadStatus(){
  crmStatus=await api('/api/crm/status');
  $('#crm-status').textContent=crmStatus.connected?'Подключено':'Не подключено';
  $('#crm-status').className='badge '+(crmStatus.connected?'green':'neutral');
  $('#crm-account').textContent=crmStatus.connected?`${crmStatus.account.name} · ${crmStatus.domain} · ${crmStatus.remember?'подключение сохранено в Windows':'до перезапуска приложения'}`:crmStatus.message;
  $('#crm-connect-form [name=domain]').value=crmStatus.domain||'ivanovnn.amocrm.ru';
  $('#crm-connect-form [name=remember]').checked=!!crmStatus.remember;
  $('#crm-connect-form [name=access_token]').placeholder=crmStatus.connected?'Оставьте пустым, чтобы использовать текущий токен':'Токен из настроек интеграции';
  $('#crm-workspace').hidden=!crmStatus.connected;
  $('#crm-disconnect').hidden=!crmStatus.connected;
  $('#crm-connection').open=!crmStatus.connected;
  if(crmStatus.connected){
    $('#crm-user').innerHTML='<option value="all">Все</option>'+crmStatus.users.map(u=>`<option value="${u.id}">${esc(u.name)}</option>`).join('');
    $('#crm-pipeline').innerHTML='<option value="all">Все</option>'+crmStatus.pipelines.map(p=>`<option value="${p.id}">${esc(p.name)}</option>`).join('');
    if(crmStatus.warnings.length)notify(crmStatus.warnings.join(' · '),true);
  }
}
async function crmLoadLeads(reset=true){
  if(crmBusy)return;
  crmSetBusy(true);
  try{
    const page=reset?1:crmPage+1;
    const result=await api('/api/crm/leads',{query:crmQuery,page});
    if(reset){crmLeads=[];crmSelected.clear();crmPreviews.clear();crmCandidates.clear();crmChosen.clear();$('#crm-previews').innerHTML='';}
    const known=new Set(crmLeads.map(l=>l.id));crmLeads.push(...result.leads.filter(l=>!known.has(l.id)));
    crmPage=result.page;crmHasMore=result.has_more;crmRenderLeads();
    $('#crm-sync-label').textContent=`Загружено сделок: ${crmLeads.length} · обновлено ${new Date().toLocaleTimeString('ru-RU',{timeZone:'Europe/Moscow'})}`;
  }catch(e){notify(e.message,true);}finally{crmSetBusy(false);}
}
function crmRenderPreviews(){
  $('#crm-previews').innerHTML=[...crmPreviews.values()].map(p=>`<div class="panel"><div class="panel-head"><h2>${esc(p.lead_name||`Сделка #${p.lead_id}`)}</h2><small>#${p.lead_id}</small><button class="secondary" data-browser-read="${p.lead_id}">Читать чат в браузере</button></div>${p.warnings.map(w=>`<div class="banner">${esc(w)}</div>`).join('')}${p.candidates.map(d=>`<article class="crm-candidate"><label class="checkbox-label"><input type="checkbox" data-candidate="${esc(d.id)}" ${crmChosen.has(d.id)?'checked':''}><strong>${esc(d.title)}</strong></label><p class="hint">${esc(d.date)} · ${d.scope==='demo'?'Учебный сценарий':'Рабочий диалог'} · ${d.complete?'Полная история заявлена в источнике':'Полнота не подтверждена'} · ${d.messages.length} сообщений ${state.dialogues.some(x=>x.id===d.id)?'· уже добавлен':''}</p><details><summary>Посмотреть сообщения и контекст</summary>${d.messages.map(m=>`<div class="message ${m.role}"><div>${esc(m.id)} · ${esc(m.author)} · ${esc(m.time||'')}</div><p>${esc(m.text)}</p></div>`).join('')}<p class="prewrap hint">${esc(d.context)}</p></details></article>`).join('')}${p.other_notes.length?`<details><summary>Прочие примечания · ${p.other_notes.length} (не считаются диалогами)</summary>${p.other_notes.map(n=>`<p class="prewrap">#${n.id} · ${esc(n.text)}${n.truncated?'… (предпросмотр сокращён)':''}</p>`).join('')}</details>`:''}</div>`).join('');
  crmCounts();
}
async function crmPreview(ids){
  if(crmBusy)return;
  if(ids.length>10){notify('Выберите не больше 10 сделок за один просмотр.',true);return;}
  crmSetBusy(true);
  try{
    crmPreviews.clear();crmCandidates.clear();crmChosen.clear();$('#crm-previews').innerHTML='<div class="panel">Загружаем диалоги из выбранных сделок…</div>';
    for(const lead_id of ids){
      try{const result=await api('/api/crm/preview',{lead_id});crmPreviews.set(lead_id,result);result.candidates.forEach(d=>crmCandidates.set(d.id,d));}
      catch(e){crmPreviews.set(lead_id,{lead_id,candidates:[],other_notes:[],warnings:[e.message]});}
      crmRenderPreviews();
    }
    $('#crm-previews').scrollIntoView({behavior:'smooth',block:'start'});
  }finally{crmSetBusy(false);}
}
$('#crm-connect-form').onsubmit=async e=>{
  e.preventDefault();if(crmBusy)return;crmSetBusy(true);
  const data=Object.fromEntries(new FormData(e.target));data.remember=e.target.elements.remember.checked;
  try{await api('/api/crm/connect',data);e.target.elements.access_token.value='';await crmLoadStatus();notify('amoCRM подключена. Нажмите «Открыть чат» у нужной сделки.'+(crmStatus.warnings.length?' '+crmStatus.warnings.join(' · '):''),crmStatus.warnings.length>0);}
  catch(err){notify(err.message,true);return;}
  finally{crmSetBusy(false);}
  crmQuery='';$('#crm-query').value='';await crmLoadLeads();
};
$('#crm-disconnect').onclick=async()=>{if(crmBusy)return;try{await api('/api/crm/disconnect',{});crmLeads=[];crmSelected.clear();crmPreviews.clear();crmCandidates.clear();crmChosen.clear();$('#crm-previews').innerHTML='';await crmLoadStatus();notify('Подключение удалено. Уже импортированные диалоги сохранены.');}catch(e){notify(e.message,true);}};
$('#crm-search-form').onsubmit=e=>{e.preventDefault();crmQuery=$('#crm-query').value.trim();crmLoadLeads();};
$('#crm-refresh').onclick=()=>crmLoadLeads();
$('#crm-more').onclick=()=>crmLoadLeads(false);
for(const id of ['crm-user','crm-pipeline','crm-stage'])$('#'+id).onchange=()=>crmRenderLeads();
$('#crm-check-all').onchange=e=>{crmFilteredLeads().forEach(l=>e.target.checked?crmSelected.add(l.id):crmSelected.delete(l.id));crmRenderLeads();};
$('#crm-preview-selected').onclick=()=>crmPreview([...crmSelected]);
$('#crm-select-candidates').onchange=e=>{crmChosen.clear();if(e.target.checked)crmCandidates.forEach(d=>crmChosen.add(d.id));crmRenderPreviews();};
$('#crm-import-selected').onclick=async()=>{
  if(crmBusy)return;if(crmChosen.size>25){notify('Выберите до 25 диалогов за один импорт.',true);return;}
  crmSetBusy(true);
  try{
    const selected=[...crmChosen].map(id=>{const d=crmCandidates.get(id);return {id,lead_id:d.source.lead_id,note_id:d.source.note_id};});
    const result=await api('/api/crm/import',{selected});await refresh();
    if(result.errors.length){notify(`Добавлено: ${result.added.length}; уже были: ${result.existing.length}. ${result.errors.join(' · ')}`,true);}
    else{const imported=state.dialogues.find(d=>d.id===(result.added[0]||result.existing[0]));if(imported)$('#scope-filter').value=imported.scope;renderDashboard();show('dashboard');notify(`Добавлено диалогов: ${result.added.length}; уже были: ${result.existing.length}. Откройте нужный и нажмите «Проверить ответ менеджера».`);}
    crmRenderPreviews();
  }catch(e){notify(e.message,true);}finally{crmSetBusy(false);}
};
document.addEventListener('change',e=>{
  const lead=e.target.closest('[data-lead-check]');if(lead){const id=Number(lead.dataset.leadCheck);lead.checked?crmSelected.add(id):crmSelected.delete(id);crmCounts();}
  const candidate=e.target.closest('[data-candidate]');if(candidate){candidate.checked?crmChosen.add(candidate.dataset.candidate):crmChosen.delete(candidate.dataset.candidate);crmCounts();}
});
document.addEventListener('click',e=>{const row=e.target.closest('[data-crm-preview]');if(row)crmPreview([Number(row.dataset.crmPreview)]);});
show('crm');appReady.then(async()=>{try{await crmLoadStatus();if(crmStatus.connected)await crmLoadLeads();}catch(e){notify(e.message,true);}});
