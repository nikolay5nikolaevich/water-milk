'use strict';
let state, selectedId, currentView = 'dashboard';
const assessmentBases=new Map();
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels = {approved:'Подтверждено',disputed:'Есть расхождения',rejected:'Отклонено',reference:'Учебный эталон',chat_import:'Ответ из чата',codex_subscription:'ИИ · подписка Codex'};
const qlabels = {answered:'Ответ дан',omitted:'Пропущен',clarification:'Нужно уточнение',unknown:'Недостаточно данных'};
function notify(message, error = false) { const el=$('#notice'); $('#notice-text').textContent=message; el.className=error?'error':'success'; el.hidden=false; }
async function api(url, body) {
  const response=await fetch(url, body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-Workbench-Token':state.token},body:JSON.stringify(body)});
  const data=await response.json(); if(!response.ok) throw new Error(data.error || 'Не удалось выполнить запрос'); return data;
}
function download(name, body, type='application/json') {
  const url=URL.createObjectURL(new Blob([body],{type})); const a=document.createElement('a'); a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
function show(view) {
  currentView=view; document.querySelectorAll('.view').forEach(x=>x.hidden=x.id!==view);
  document.querySelectorAll('nav [data-view]').forEach(x=>x.classList.toggle('active',x.dataset.view===view));
  $('#page-title').textContent={crm:'Новая проверка',dashboard:'Обзор проверок',new:'Добавить диалог',knowledge:'База правил',guide:'Как это работает',detail:'Разбор диалога'}[view];
  if(view==='detail')document.querySelector('nav [data-view="dashboard"]').classList.add('active');
  $('header > .header-actions').hidden=view==='crm';
  window.scrollTo(0,0);
}
function latest(did) { return state.runs.find(r=>r.dialogue_id===did&&r.source!=='reference') || state.runs.find(r=>r.dialogue_id===did); }
function decision(run) {return run?.reviews.at(-1)?.decision;}
function critical(run) {return !!run?.report.findings.some(f=>f.kind==='critical');}
function badge(label, type='neutral') {return `<span class="badge ${type}">${esc(label)}</span>`;}
function renderDashboard() {
  const scope=$('#scope-filter').value;
  let dialogs=state.dialogues.filter(d=>d.scope===scope);
  const runs=dialogs.map(d=>latest(d.id)).filter(Boolean);
  const approved=runs.filter(r=>r.source!=='reference'&&decision(r)==='approved');
  const scored=approved.filter(r=>r.total!==null);
  const average=scored.length?Math.round(scored.reduce((s,r)=>s+r.total,0)/scored.length):'—';
  const metric=(label,value,note,kind='')=>`<div class="metric ${kind}"><span>${label}</span><strong>${value}</strong><small>${note}</small></div>`;
  $('#metrics').innerHTML=metric('Диалоги в выборке',dialogs.length,scope==='demo'?'Учебные, вымышленные данные':'Рабочие переписки')+
    metric('Средний балл',average,`${scored.length} подтверждённых оценок из 100`)+
    metric('Критические сигналы',runs.filter(critical).length,'Требуют отдельного внимания','warning')+
    metric('Ожидают проверки',runs.filter(r=>r.source!=='reference'&&!decision(r)).length,'Результаты ИИ для сверки человеком');
  const filter=$('#status-filter').value, search=$('#search').value.toLocaleLowerCase();
  dialogs=dialogs.filter(d=>{
    const r=latest(d.id);return (!search||[d.title,...d.messages.map(m=>m.author)].join(' ').toLocaleLowerCase().includes(search))&&
      (filter==='all'||filter==='pending'&&(!r||r.source==='reference')||filter==='critical'&&critical(r)||filter==='review'&&r&&r.source!=='reference'&&!decision(r));
  });
  $('#dialogue-list').innerHTML=dialogs.map(d=>{
    const r=latest(d.id), status=r?.source==='reference'?'Учебный эталон':r?labels[decision(r)]||'Ожидает проверки':'Нет оценки ИИ';
    return `<tr><td><button class="row-link" data-dialogue="${esc(d.id)}">${esc(d.title)}</button><small>${esc([...new Set(d.messages.filter(m=>m.role==='manager').map(m=>m.author))].join(', '))} · ${d.messages.length} сообщений</small></td><td>${esc(d.date.split('-').reverse().join('.'))}</td><td><strong class="score-inline">${r?(r.total??'Н/Д'):'—'}</strong>${r?.total!=null?'<small>из 100</small>':''}</td><td>${critical(r)?badge('Критический флаг','danger'):r?.report.findings.some(f=>f.kind==='check')?badge('Нужно уточнение','amber'):r?.report.findings.length?badge('Есть замечания','amber'):r?badge('Без замечаний','green'):'—'}</td><td>${badge(status,decision(r)==='approved'?'green':'neutral')}</td></tr>`;
  }).join('');
  $('#empty').hidden=dialogs.length>0;
}
function renderKnowledge() {
  const kb=state.kb;
  $('#kb-description').textContent=`${kb.version} · ${kb.scope==='demo'?'Учебная база, компанией не утверждена':'База компании'} · ${kb.rules.length} записей`;
  $('#rules').innerHTML=kb.rules.map(r=>`<article class="rule"><span class="rule-id">${esc(r.id)}</span><div><p>${esc(r.text)}</p><small>${esc(r.source)} · ${esc(r.owner)} · ${esc(r.valid_from)} — ${esc(r.valid_to||'без окончания')} · ${esc(r.status)}</small></div></article>`).join('');
  $('#kb-json').value=JSON.stringify(kb,null,2);
}
function ruleLink(rid,run) {return `<a class="rule-link" href="#rule-${esc(run.id)}-${esc(rid)}">${esc(rid)}</a>`;}
function renderDetail() {
  const d=state.dialogues.find(d=>d.id===selectedId);if(!d)return;
  const run=latest(d.id),r=run?.report;
  const chosenBasis=assessmentBases.get(d.id)||((run?.kb.scope||d.scope)==='demo'?'demo':'auto');
  const versions=state.runs.filter(x=>x.dialogue_id===d.id);
  let html=`<div class="detail-heading"><div><h2>${esc(d.title)}</h2><p>${esc(d.date)} · ${d.complete?'Полная история':'Загруженный фрагмент'} · ${chosenBasis==='demo'?'Учебная оценка':'Рабочая оценка'}</p></div>${r?`<div class="big-score">${run.total??'Н/Д'}<small>${run.total!==null?(r.assessment_scope==='visible_messages'?'/ 100 за загруженные ответы':'/ 100'):'общий балл не рассчитывается'}</small></div>`:''}</div>
    <div class="workflow"><div><strong>Проверка ответа менеджера</strong><small>ИИ по подписке · цитаты и ссылки на правила</small></div><label class="assessment-choice">База для проверки<select id="assessment-basis"><option value="auto" ${chosenBasis==='auto'?'selected':''}>Рабочая: правила компании</option><option value="demo" ${chosenBasis==='demo'?'selected':''}>Учебная: DEMO-1.0</option></select></label><div class="workflow-actions"><button id="auto-check" class="primary" data-check-dialogue="${esc(d.id)}">Проверить ответ менеджера</button></div></div>
    <div id="auto-check-status" class="hint" role="status"></div>
    ${chosenBasis==='demo'?'<div class="banner">Учебная оценка по DEMO-1.0. Учебные цены и сроки применяются только к этому тесту.</div>':state.kb.scope==='demo'&&d.scope==='company'?'<div class="banner">Правила компании пока не загружены. Доступна предварительная проверка общения по базовым критериям. Проверка цен, сроков и регламента будет Н/Д.</div>':''}
    <div class="detail-columns"><div><div class="panel transcript"><h3>Переписка</h3>${d.messages.map(m=>`<div class="message ${m.role}"><div><span>${esc(m.id)} · ${esc(m.author)}</span><time>${esc(m.time||'')}</time></div><p>${esc(m.text)}</p></div>`).join('')}<details><summary>Контекст и сведения карточки</summary><p class="prewrap">${esc(d.context||'Не предоставлены')}</p></details></div>
    <details class="panel import-panel"><summary>Ручной перенос через чат (дополнительно)</summary><p>При необходимости можно передать задание и импортировать JSON самостоятельно.</p><button id="copy-packet" class="secondary">Копировать задание</button> <a class="button secondary" href="/api/packet/${esc(d.id)}?basis=${chosenBasis}">Скачать пакет</a><input id="report-file" type="file" accept=".json,.txt,application/json,text/plain"><label>JSON результата<textarea id="report-json" rows="7" spellcheck="false" placeholder='{"dialogue_id": "…", …}'></textarea></label><button id="import-report" class="secondary">Импортировать JSON</button><small>При нажатии основной кнопки перенос выполняется автоматически.</small></details></div><div>`;
  if(r) {
    html+=`<div class="panel"><div class="panel-head"><h3>Результат проверки</h3>${badge(labels[run.source],run.source==='reference'?'amber':'neutral')}</div>${run.source==='reference'?'<div class="banner">Это авторский учебный эталон из материалов MVP. Для оценки нейросетью нажмите «Проверить ответ менеджера».</div>':''}
      <p class="hint">${esc(r.kb_version)} · ${esc(r.rubric_version)} · ${esc(new Date(run.created).toLocaleString('ru-RU'))}</p>${run.kb.mode==='general_rubric'?'<div class="banner">Предварительная оценка общения. Коммерческие условия компании не предоставлены; фактическая правильность и регламент не оценены.</div>':''}
      ${r.assessment_scope==='visible_messages'?`<p class="hint">Оценены загруженные ответы. ${r.history_complete?'История отмечена полной.':'Полнота всей истории не подтверждена; баллы не относятся к сообщениям за пределами выгрузки.'} Действия в CRM не входят в оценку текста ответа.</p>`:''}
      <div class="score-list">${Object.entries(state.criteria).map(([key,[name,weight]])=>{const s=r.scores[key];return `<div class="criterion"><div><span>${esc(r.rubric_version==="QA-ANSWER-1.0"&&key==="compliance"?"Правила общения":r.rubric_version==="QA-ANSWER-1.0"&&key==="completeness"?"Полнота ответа":name)}</span><strong>${s.points??'Н/Д'} <small>/ ${weight}</small></strong></div><progress class="bar" max="${weight}" value="${s.points??0}" aria-label="${esc(name)}"></progress><p>${esc(s.reason)}</p></div>`;}).join('')}</div>
      <h3>Замечания и доказательства</h3>${r.findings.length?r.findings.map(f=>`<article class="finding ${f.kind}">${badge({critical:'Критический флаг · проверить человеку',violation:'Замечание',check:'Требует подтверждения'}[f.kind],f.kind==='critical'?'danger':'amber')}<blockquote>${esc(f.quote)}</blockquote><p>${esc(f.explanation)}</p><small>${esc(f.message_id)} · ${esc(d.messages.find(m=>m.id===f.message_id)?.author)} → ${f.rule_ids.map(id=>ruleLink(id,run)).join(' ')}</small></article>`).join(''):'<p class="ok-text">Замечания не выявлены.</p>'}
      ${r.missing_context.length?`<div class="banner"><strong>Недостающие данные</strong><ul>${r.missing_context.map(x=>`<li>${esc(x)}</li>`).join('')}</ul></div>`:''}
      <h3>Вопросы клиента</h3>${r.questions.map(q=>`<div class="question"><strong>${esc(q.question)}</strong><p>${badge(qlabels[q.status])} ${esc(q.question_message_id)} → ${esc(q.answer_message_ids.join(', ')||'нет сообщения с ответом')}</p><small>${esc(q.explanation)}</small></div>`).join('')}
      <h3>Обратная связь менеджеру</h3><p class="prewrap">${esc(r.feedback)}</p><h3>Пример ответа</h3><div class="reply prewrap">${esc(r.corrected_reply)}</div>
      ${r.strengths.length?`<h3>Что хорошо</h3><ul>${r.strengths.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:''}
      ${r.kb_proposals.length?`<h3>Предложения в базу · черновики</h3><ul>${r.kb_proposals.map(x=>`<li>${esc(x)}</li>`).join('')}</ul><p class="hint">Не включены в действующие правила. Нужна проверка источника владельцем.</p>`:''}
      <details><summary>Правила, использованные при проверке</summary>${run.kb.rules.map(rule=>`<article class="rule" id="rule-${esc(run.id)}-${esc(rule.id)}"><span class="rule-id">${esc(rule.id)}</span><div><p>${esc(rule.text)}</p><small>${esc(rule.source)} · ${esc(rule.owner)}</small></div></article>`).join('')}</details><button id="export-report" class="secondary">Скачать разбор JSON</button></div>`;
    if(run.source!=='reference') {
      html+=`<div class="panel"><span class="step">3</span><h3>Решение проверяющего</h3><p>Сверьте смысл замечаний и соответствие правилу. При расхождении сохраните комментарий; исходный ответ остаётся в журнале.</p><form id="review-form"><label>Проверяющий<input name="reviewer" required maxlength="100"></label><label>Решение<select name="decision"><option value="approved">Подтвердить</option><option value="disputed">Отметить расхождения</option><option value="rejected">Отклонить</option></select></label><label>Обоснование<textarea name="note" rows="3" required></textarea></label><button class="primary">Сохранить решение</button></form>${run.reviews.map(v=>`<div class="review"><strong>${esc(labels[v.decision])}</strong><small>${esc(v.reviewer)} · ${esc(new Date(v.created).toLocaleString('ru-RU'))}</small><p>${esc(v.note)}</p></div>`).join('')}</div>`;
    }
    html+=`<details class="panel"><summary>История результатов · ${versions.length}</summary>${versions.map(v=>`<p>${esc(new Date(v.created).toLocaleString('ru-RU'))} · ${esc(labels[v.source])} · ${v.total??'Н/Д'} · ${esc(labels[decision(v)]||'Ожидает проверки')} <button class="text-link" data-export-run="${esc(v.id)}">Скачать JSON</button></p>`).join('')}</details>`;
  } else html+='<div class="panel empty"><span>◎</span><h3>Результат появится здесь</h3><p>Нажмите «Проверить ответ менеджера». Приложение получит оценку через подписку Codex и проверит ссылки на правила.</p></div>';
  html+='</div></div>';
  $('#detail-content').innerHTML=html;
  $('#assessment-basis').onchange=e=>{assessmentBases.set(d.id,e.target.value);renderDetail();};
  document.dispatchEvent(new Event('check-controls-rendered'));
  $('#copy-packet').onclick=async()=>{
    try{const res=await fetch(`/api/packet/${d.id}?basis=${chosenBasis}`);if(!res.ok){const e=await res.json();throw new Error(e.error);}const content=await res.text();await navigator.clipboard.writeText(content);notify('Задание скопировано. Вставьте его в новый чат по вашей подписке.');}
    catch(e){notify(`${e.message}. Можно скачать пакет файлом.`,true);}
  };
  $('#report-file').onchange=async e=>{try{if(e.target.files[0])$('#report-json').value=await e.target.files[0].text();}catch(err){notify(err.message,true);}};
  $('#import-report').onclick=async()=>{try{await api('/api/import',{dialogue_id:d.id,basis:chosenBasis,raw:$('#report-json').value});await refresh();notify('Ответ сохранён. Цитаты и ссылки проверены; подтвердите выводы после сверки.');}catch(e){notify(e.message,true);}};
  if($('#export-report'))$('#export-report').onclick=()=>download(`report-${d.id}.json`,JSON.stringify(run,null,2));
  if($('#review-form'))$('#review-form').onsubmit=async e=>{e.preventDefault();try{await api('/api/review',{run_id:run.id,...Object.fromEntries(new FormData(e.target))});await refresh();notify('Решение сохранено в журнале.');}catch(err){notify(err.message,true);}};
}
async function refresh(){state=await api('/api/state');renderDashboard();renderKnowledge();if(currentView==='detail')renderDetail();}
document.addEventListener('click', e=>{
  const view=e.target.closest('[data-view]');if(view){$('#notice').hidden=true;show(view.dataset.view);}
  const dialogue=e.target.closest('[data-dialogue]');if(dialogue){selectedId=dialogue.dataset.dialogue;renderDetail();show('detail');}
  const exp=e.target.closest('[data-export-run]');if(exp)download('quality-run.json',JSON.stringify(state.runs.find(r=>r.id===exp.dataset.exportRun),null,2));
  const rule=e.target.closest('.rule-link');if(rule){const target=document.getElementById(rule.hash.slice(1));if(target)target.closest('details').open=true;}
});
$('#back').onclick=()=>show('dashboard');
$('#notice-close').onclick=()=>$('#notice').hidden=true;
for(const id of ['scope-filter','status-filter','search'])$('#'+id).addEventListener('input',renderDashboard);
$('#demo').onclick=async()=>{try{await api('/api/demo',{});$('#scope-filter').value='demo';await refresh();show('dashboard');notify('Загружены пять учебных диалогов и авторские эталоны. Фактического прогона ИИ ещё нет.');}catch(e){notify(e.message,true);}};
$('#dialogue-form').onsubmit=async e=>{e.preventDefault();try{const data=Object.fromEntries(new FormData(e.target));data.complete=data.complete==='true';const result=await api('/api/dialogues',data);$('#scope-filter').value=data.scope;await refresh();selectedId=result.id;renderDetail();show('detail');e.target.reset();setDate();notify('Диалог сохранён. Нажмите «Проверить ответ менеджера».');}catch(err){notify(err.message,true);}};
$('#download-kb').onclick=()=>download('knowledge-base.json',JSON.stringify(state.kb,null,2));
$('#kb-file').onchange=async e=>{try{if(e.target.files[0])$('#kb-json').value=await e.target.files[0].text();}catch(err){notify(err.message,true);}};
$('#save-kb').onclick=async()=>{try{await api('/api/kb',JSON.parse($('#kb-json').value));await refresh();notify('Версия базы сохранена. Новые задания используют её; старые разборы сохраняют свои правила.');}catch(e){notify(e.message,true);}};
function setDate(){const parts=new Intl.DateTimeFormat('en-CA',{timeZone:'Europe/Moscow',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date());const get=t=>parts.find(p=>p.type===t).value;$('#dialogue-form [name=date]').value=`${get('year')}-${get('month')}-${get('day')}`;}
setDate();const appReady=refresh().catch(e=>notify(e.message,true));
