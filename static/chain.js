'use strict';
(() => {
  const base = document.body.dataset.api, T = window.ChainTime;
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let state = null, access = {}, busy = false, editingId = null, formVersion = null, dragging = null;
  let serverTime = Date.now()/1000, syncedAt = performance.now(), connected = false, lastTouch = 0;
  const now = () => serverTime + (performance.now()-syncedAt)/1000;
  const signedIn = () => ['public','tactician'].includes(access.role) && access.expires_at > now();
  const canEdit = () => signedIn() && (access.role==='tactician' || !state?.locked);
  const profile = (id, name) => `<a href="https://www.torn.com/profiles.php?XID=${encodeURIComponent(id)}" target="_blank" rel="noopener noreferrer">${esc(name)}</a>`;
  const date = timestamp => timestamp ? new Date(timestamp*1000).toLocaleString() : 'Unavailable';
  const error = message => { $('error').textContent = message; $('error').hidden = !message; };
  const roster = kind => state?.observations[kind]?.members || [];
  function targetInfo(m) {
    if (!m) return '<p class="muted">Target status unavailable — awaiting API</p>';
    return `<p>${esc(m.status_text)} · ${esc(m.location)}${m.in_torn ? ' · In Torn' : ' · Torn location not confirmed'}</p>
      <p class="hospital-timer" data-member="${esc(m.id)}">${esc(T.hospital(m,now()))}</p>
      ${m.hospital_until ? `<small>Estimated release: ${esc(date(m.hospital_until))}</small>` : ''}
      <small class="status-observation" data-observed="${esc(m.observed_at)}">Last confirmed API status: ${esc(date(m.observed_at))}</small>`;
  }
  function receive(data, force=false) {
    if (data.access) access = data.access;
    if (state && data.revision < state.revision) return;
    const refreshAccess = state && data.auth_revision !== state.auth_revision && !data.access;
    serverTime = data.server_time; syncedAt = performance.now(); state = data;
    render();
    if(refreshAccess)refresh().catch(e=>error(e.message));
  }
  async function refresh() {
    const r = await fetch(base+'/state', {cache:'no-store'});
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || 'Could not load queue.');
    receive(data, true);
  }
  async function change(path, data={}, method='POST', optimistic=null) {
    if (busy) return false;
    busy = true; error('');
    const previousOrder = state?.active;
    if (optimistic) { state.active = optimistic; render(); }
    try {
      const r = await fetch(base+'/'+path, {method, headers:{'Content-Type':'application/json','X-CSRF-Token':access.csrf || ''},
        body:JSON.stringify({version:state?.version,...data})});
      const payload = await r.json();
      if (!r.ok) throw new Error(payload.error || 'Unable to save.');
      if (payload.active) receive(payload, true);
      await refresh();
      return true;
    } catch (e) {
      error(e.message);
      try { await refresh(); } catch (_) { if(previousOrder)state.active=previousOrder; error(e.message+' Connection lost — showing last confirmed order. Reconnecting…'); }
      return false;
    } finally { busy = false; render(); }
  }
  function controls(row, index) {
    if (!canEdit()) return '';
    return `<div class="actions"><button data-action="up" data-id="${row.id}" aria-label="Move ${esc(row.attacker_name)} up" ${index===0?'disabled':''}>↑</button>
      <button data-action="down" data-id="${row.id}" aria-label="Move ${esc(row.attacker_name)} down" ${index===state.active.length-1?'disabled':''}>↓</button>
      <button data-action="edit" data-id="${row.id}">Edit</button><button data-action="delete" data-id="${row.id}">Delete</button></div>`;
  }
  function render() {
    if (!state) return;
    const edit = canEdit();
    $('mode').textContent = !signedIn() ? 'Read-only' : access.role==='tactician' ? 'Tactician' : edit ? 'Public · editing' : 'Public · read-only';
    $('mode').classList.toggle('unlocked', edit);
    $('access').textContent = signedIn() ? 'Sign out' : 'Sign in';
    $('board-status').textContent = state.locked ? 'Board locked · tacticians can edit' : 'Board unlocked';
    $('board-lock').hidden = !signedIn() || access.role!=='tactician';
    $('board-lock').textContent = state.locked ? 'Unlock board' : 'Lock board';
    $('board-lock').disabled = busy;
    $('save-assignment').disabled = busy || !edit;
    document.querySelectorAll('#assignment-form input, #assignment-form select').forEach(el=>{el.disabled=!edit;});
    $('access').disabled = busy;
    $('add').hidden = !edit; $('add').disabled = busy;
    const next = state.active[0];
    $('next').innerHTML = next ? `<span class="eyebrow">NEXT ATTACK</span><h2>${profile(next.attacker_id,next.attacker_name)} <span class="muted">→</span> ${profile(next.target_id,next.target_name)}</h2>
      ${targetInfo(next.target)}<p class="badge">${esc(next.verification === 'review' ? 'Requires coordinator review' : 'Queued · awaiting attack')}</p>
      <div class="actions"><a class="button primary" href="https://www.torn.com/page.php?sid=attack&amp;user2ID=${encodeURIComponent(next.target_id)}" target="_blank" rel="noopener noreferrer">Attack Target ↗</a>
      ${edit ? `<button class="primary" data-action="advance" data-id="${next.id}">Complete &amp; Next</button><button data-action="skip" data-id="${next.id}">Skip / Defer</button>`:''}</div>` : '<span class="eyebrow">NEXT ATTACK</span><h2>Queue is clear</h2><p>Add an assignment to choose who attacks next.</p>';
    $('queue').innerHTML = state.active.map((r,i) => `<li class="queue-row" data-id="${r.id}" draggable="${edit}">
      <div class="queue-order"><span aria-hidden="true">${edit?'☰ ':''}${i+1}</span><span class="badge">${i===0?'NEXT ATTACK':i===1?'ON DECK':'UPCOMING'}</span></div>
      <div class="queue-person"><strong>${profile(r.attacker_id,r.attacker_name)} → ${profile(r.target_id,r.target_name)}</strong>${targetInfo(r.target)}
      <small>${r.verification==='review'?'Requires coordinator review':'Queued'}${r.allow_duplicate?' · Target reuse approved':''}</small></div>${controls(r,i)}</li>`).join('') || '<li class="empty">No active assignments.</li>';
    $('completed-count').textContent = `(${state.completed.length})`;
    $('completed').innerHTML = state.completed.map(r => `<article><strong>${profile(r.attacker_id,r.attacker_name)} → ${profile(r.target_id,r.target_name)}</strong>
      <p>${esc(date(r.completed_at))} · ${r.verification==='verified'?'API verified':r.verification==='review'?'Requires review':'Manually completed · unverified'}</p>
      ${r.verified_attack_id?`<small>Verified attack #${esc(r.verified_attack_id)} · ${esc(date(r.verified_at))}</small>`:''}</article>`).join('') || '<p>No completed attacks.</p>';
    $('deferred-count').textContent = `(${state.deferred.length})`;
    $('deferred').innerHTML = state.deferred.map(r => `<article>${profile(r.attacker_id,r.attacker_name)} → ${profile(r.target_id,r.target_name)}
      <p>Deferred · not completed</p>${edit?`<button data-action="restore" data-id="${r.id}">Restore to end</button><button data-action="edit" data-id="${r.id}">Edit</button><button data-action="delete" data-id="${r.id}">Delete</button>`:''}</article>`).join('') || '<p>No deferred assignments.</p>';
    availability(); tick();
    document.querySelectorAll('main button').forEach(b => { if(busy) b.disabled = true; });
  }
  function availability() {
    if (!state) return;
    const obs = state.observations.enemies, stale = T.stale(obs,now(),state.stale_seconds);
    $('roster-updated').textContent = `${stale?'STALE / UNAVAILABLE — last known status only':'Confirmed API snapshot'} · ${date(obs?.observed_at)}. Hospital expiry does not confirm availability.`;
    const card = m => `<article><strong>${profile(m.id,m.name)}</strong>${targetInfo(m)}${canEdit()?`<button data-action="select" data-id="${esc(m.id)}">Assign target</button>`:''}</article>`;
    $('available').innerHTML = (stale?[]:roster('enemies').filter(m=>m.in_torn && m.status==='Okay')).map(card).join('') || '<p class="muted">No fresh confirmed available enemies.</p>';
    $('releasing').innerHTML = roster('enemies').filter(m=>m.status==='Hospital' && m.hospital_until && m.hospital_until-now()<=300)
      .sort((a,b)=>a.hospital_until-b.hospital_until).map(card).join('') || '<p class="muted">No estimated releases in the next five minutes.</p>';
  }
  function tick() {
    if (!state) return;
    if (access.role && !signedIn()) { access.role=null; render(); return; }
    const c = state.observations.chain, stale = T.stale(c,now(),state.stale_seconds);
    $('chain-count').textContent = c?.current ?? '—';
    const seconds = T.remaining(c?.deadline,now());
    $('chain-clock').textContent = c?.phase==='cooldown' ? T.duration(T.remaining(c.cooldown_until,now())) : T.duration(seconds);
    const label = stale ? 'STALE / UNAVAILABLE' : c.phase==='active' ? T.warning(seconds,state.warnings) :
      c.phase==='warmup' ? 'Warm-up · reach 10 hits' : c.phase==='cooldown' ? 'Cooldown' : c.phase==='inactive' ? 'No active chain' : 'Awaiting confirmation';
    $('warning').textContent=label;
    $('clock-panel').className = !stale && c?.phase==='active' ? label.toLowerCase().split(' ')[0] : '';
    $('chain-status').textContent = `API state: ${c?.phase || 'unavailable'}${stale?' · stale':''}`;
    $('chain-updated').textContent = `Last successful chain API update: ${date(c?.observed_at)} · countdown is a local estimate`;
    const issues = Object.entries(state.observations).filter(([,o])=>o.error).map(([kind,o])=>`${kind}: ${o.error}`);
    $('api-health').textContent = issues.join(' · ');
    for (const el of document.querySelectorAll('.status-observation')) {
      const observed=Number(el.dataset.observed);
      const old=T.stale({observed_at:observed,error:state.observations.enemies?.error},now(),state.stale_seconds);
      el.textContent=`Last confirmed API status: ${date(observed)}${old?' · STALE':''}`;
    }
    for (const el of document.querySelectorAll('.hospital-timer')) {
      const m=roster('enemies').find(m=>String(m.id)===el.dataset.member);
      el.textContent=T.hospital(m,now());
    }
  }
  function options(kind, selectId, searchId) {
    const select=$(selectId), selected=select.value, query=$(searchId).value.toLowerCase();
    const members=roster(kind).filter(m=>`${m.name} ${m.id}`.toLowerCase().includes(query)).slice().sort((a,b)=>
      Number(b.in_torn && b.status==='Okay')-Number(a.in_torn && a.status==='Okay') || a.name.localeCompare(b.name));
    select.replaceChildren(...members.map(m=>new Option(`${m.name} [${m.id}]${kind==='enemies'?` · ${m.status} · ${m.location}`:''}`,m.id)));
    if(members.some(m=>String(m.id)===selected)) select.value=selected;
  }
  function preview() { $('target-preview').innerHTML=targetInfo(roster('enemies').find(m=>String(m.id)===$('target').value)); }
  function assignment(row=null, target=null) {
    editingId=row?.id || null; formVersion=state.version;
    $('assignment-title').textContent=row?'Edit Assignment':'Add Assignment';
    $('assignment-form').reset(); $('assignment-error').textContent='';
    options('friendly','attacker','attacker-search'); options('enemies','target','target-search');
    if(row) { $('attacker').value=row.attacker_id; $('target').value=row.target_id; $('reuse').checked=Boolean(row.allow_duplicate); }
    if(target) $('target').value=target;
    preview(); $('assignment-dialog').showModal();
  }
  async function reorder(ids) {
    const rows=ids.map(id=>state.active.find(r=>r.id===id));
    await change('reorder',{ids},'POST',rows);
  }
  document.addEventListener('click', async e=>{
    const close=e.target.closest('[data-close]'); if(close) $(close.dataset.close).close();
    const b=e.target.closest('[data-action]'); if(!b || !canEdit() || busy) return;
    const action=b.dataset.action, id=Number(b.dataset.id);
    if(action==='select') return assignment(null,b.dataset.id);
    if(action==='edit') return assignment([...state.active,...state.deferred].find(r=>r.id===id));
    if(action==='up' || action==='down') {
      const ids=state.active.map(r=>r.id), i=ids.indexOf(id), j=i+(action==='up'?-1:1);
      if(j>=0 && j<ids.length) { [ids[i],ids[j]]=[ids[j],ids[i]]; await reorder(ids); document.querySelector(`[data-id="${id}"] [data-action="${action}"]`)?.focus(); }
    } else if(action==='delete') await change('assignments/'+id,{},'DELETE');
    else await change(action,{assignment_id:id});
  });
  $('queue').addEventListener('dragstart', e=>{
    if(!canEdit() || busy || e.target.closest('a,button')) { e.preventDefault(); return; }
    dragging=Number(e.target.closest('[data-id]')?.dataset.id);
    e.dataTransfer.setData('text/plain',String(dragging)); e.dataTransfer.effectAllowed='move';
  });
  $('queue').addEventListener('dragover', e=>{if(canEdit())e.preventDefault();});
  $('queue').addEventListener('drop', async e=>{
    e.preventDefault(); const target=Number(e.target.closest('[data-id]')?.dataset.id);
    if(!dragging || !target || dragging===target || !canEdit() || busy)return;
    const ids=state.active.map(r=>r.id), from=ids.indexOf(dragging), to=ids.indexOf(target);
    if(from<0 || to<0)return; ids.splice(from,1); ids.splice(to,0,dragging); dragging=null; await reorder(ids);
  });
  $('add').onclick=()=>assignment();
  $('access').onclick=async()=>{
    if(signedIn()) await change('lock');
    else { $('pin-form').reset(); $('pin-error').textContent=access.configured?'':'Public and tactician PINs are not configured on the server.'; $('pin-dialog').showModal(); $('pin').focus(); }
  };
  $('board-lock').onclick=async()=>{
    if(signedIn() && access.role==='tactician')await change('board-lock',{locked:!state.locked});
  };
  $('pin-form').onsubmit=async e=>{e.preventDefault(); const ok=await change('unlock',{pin:$('pin').value}); $('pin').value=''; if(ok)$('pin-dialog').close(); else $('pin-error').textContent=$('error').textContent;};
  $('assignment-form').onsubmit=async e=>{
    e.preventDefault(); const ok=await change(editingId?'assignments/'+editingId:'assignments',
      {version:formVersion,attacker_id:$('attacker').value,target_id:$('target').value,allow_duplicate:$('reuse').checked},editingId?'PATCH':'POST');
    if(ok)$('assignment-dialog').close(); else { $('assignment-error').textContent=$('error').textContent; formVersion=state.version; }
  };
  $('attacker-search').oninput=()=>options('friendly','attacker','attacker-search');
  $('target-search').oninput=()=>{options('enemies','target','target-search');preview();}; $('target').onchange=preview;
  async function touch() {
    if(!signedIn() || busy || now()-lastTouch<60)return;
    lastTouch=now();
    try {
      const r=await fetch(base+'/touch',{method:'POST',headers:{'X-CSRF-Token':access.csrf}});
      if(r.ok)access.expires_at=now()+1800;
      else await refresh();
    } catch(_) { /* Server still enforces expiration. */ }
  }
  document.addEventListener('pointerdown',touch); document.addEventListener('keydown',touch);
  async function start() {
    try { await refresh(); } catch(e) { error(e.message); }
    const events=new EventSource(base+'/events');
    events.addEventListener('state',e=>{connected=true;$('connection').textContent='Live';receive(JSON.parse(e.data));});
    events.addEventListener('coordinator_error',e=>{connected=false;error(JSON.parse(e.data).error);$('connection').textContent='Coordinator unavailable';});
    events.onopen=()=>{connected=true;$('connection').textContent='Live';refresh().catch(e=>error(e.message));};
    events.onerror=()=>{connected=false;$('connection').textContent='Disconnected · reconnecting';};
    setInterval(tick,1000);
    setInterval(()=>{if(!busy)availability();},15000);
    setInterval(async()=>{if(!busy)try{await refresh();}catch(e){error(e.message);}},30000);
  }
  start();
})();
