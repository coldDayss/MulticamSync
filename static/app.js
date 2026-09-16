'use strict';

const $ = id => document.getElementById(id);
const COLORS = ['#65dfdc','#e7b66a','#9c9ce4','#e291b3','#84bddd','#a8c983','#e6936f','#b694de'];
let state = {project:null,job:null,exports:[],sampleFolder:''};
let project = null;
let layout = 'horizontal';
let cameraOrder = [];
let globalTime = 0;
let playing = false;
let buffering = false;
let playbackMasterId = null;
let frameHandle = null;
let lastClock = 0;
let viewStart = 0;
let viewEnd = 1;
let exportStart = 0;
let exportEnd = 0;
let activeTab = 'sync';
let currentProjectKey = '';
let currentStructureKey = '';
let currentMediaKey = '';
let processedJob = null;
let requestedJobKind = null;
let workflowQueue = [];
let busy = false;
let toastTimer;
let pollInFlight = false;
let stopped = false;
let previewAudio = 'none';
let audioCamera = 'none';
let currentStep = 1;
let updateQueue = Promise.resolve();
const videoStates = new Map();
const waveforms = new Map();
const waveformPending = new Set();

function icon(name) { return `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`; }
function esc(value) { return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function numeric(value, fallback=0) { const n=Number(value); return Number.isFinite(n)?n:fallback; }
function clamp(n,min,max) { return Math.min(max,Math.max(min,n)); }
function cameras() { return project?.cameras || []; }
function clipsFor(id) { return (project?.clips||[]).filter(c=>c.cameraId===id).sort((a,b)=>numeric(a.start)-numeric(b.start)); }
function camOffset(id) { return numeric(cameras().find(c=>c.id===id)?.offset); }
function colorFor(id) { const index=cameras().findIndex(c=>c.id===id); return cameras()[index]?.color || COLORS[Math.max(0,index)%COLORS.length]; }
function frameSize() { return 1/numeric($('exportFps').value,30); }
function fmt(sec, short=false) {
  const negative=sec<0; const ms=Math.round(Math.abs(numeric(sec))*1000);
  const h=Math.floor(ms/3600000), m=Math.floor(ms/60000)%60, s=Math.floor(ms/1000)%60, f=ms%1000;
  const prefix=negative?'-':'';
  if(short) return prefix+(h?String(h).padStart(2,'0')+':':'')+String(m).padStart(2,'0')+':'+String(s).padStart(2,'0');
  return prefix+String(h).padStart(2,'0')+':'+String(m).padStart(2,'0')+':'+String(s).padStart(2,'0')+'.'+String(f).padStart(3,'0');
}
function parseTime(value) {
  const text=String(value).trim();
  if(!text) return NaN;
  const sign=text.startsWith('-')?-1:1;
  const parts=text.replace(/^[+-]/,'').split(':');
  if(parts.length>3||parts.some(p=>!/^\d+(\.\d+)?$/.test(p)))return NaN;
  return sign*parts.reduce((sum,p)=>sum*60+Number(p),0);
}
function durationText(sec) { return sec>=60 ? `${Math.floor(sec/60)}분 ${Math.floor(sec%60)}초` : `${numeric(sec).toFixed(3)}초`; }
function sizeText(bytes) { if(!Number.isFinite(Number(bytes))) return ''; const n=Number(bytes);return n>1073741824?(n/1073741824).toFixed(2)+' GB':(n/1048576).toFixed(1)+' MB'; }
function mediaReady(c) { return Boolean(c.previewReady || c.proxyReady); }
function bounds() {
  if(!project?.clips?.length)return [0,1];
  const starts=project.clips.map(c=>numeric(c.start)+camOffset(c.cameraId));
  const ends=project.clips.map(c=>numeric(c.start)+camOffset(c.cameraId)+numeric(c.duration));
  return [Math.min(0,...starts),Math.max(1,...ends)];
}
function activeClip(id,t) {return clipsFor(id).find(c=>t>=numeric(c.start)+camOffset(id)-0.0001&&t<numeric(c.start)+camOffset(id)+numeric(c.duration)-0.0001);}
function showToast(message,error=false) {
  clearTimeout(toastTimer);$('toast').textContent=message;$('toast').classList.toggle('error',error);$('toast').classList.remove('hidden');toastTimer=setTimeout(()=>$('toast').classList.add('hidden'),error?8500:4800);
}
function setStep(step) {
  currentStep=step;document.querySelectorAll('.workflow-step').forEach(b=>{const n=Number(b.dataset.step);b.classList.toggle('active',n===step);b.classList.toggle('done',Boolean(project)&&n<step);});
}
function selectTab(tab) {
  activeTab=tab;
  $('syncTab').classList.toggle('selected',tab==='sync');$('syncTab').setAttribute('aria-selected',tab==='sync');
  $('exportTab').classList.toggle('selected',tab==='export');$('exportTab').setAttribute('aria-selected',tab==='export');
  $('syncSettings').classList.toggle('hidden',tab!=='sync');$('exportSettings').classList.toggle('hidden',tab!=='export');
  if(project)setStep(tab==='export'?4:3);
}
async function api(path,body,method='POST') {
  const options={method,headers:{'Content-Type':'application/json'}};
  if(body!==undefined)options.body=JSON.stringify(body);
  const res=await fetch(path,options);
  let data;try{data=await res.json();}catch{throw new Error(`서버 응답을 읽지 못했습니다 (${res.status}).`);}
  if(!res.ok||data.error) throw new Error(data.error||data.message||`요청 실패 (${res.status})`);
  return data;
}
async function getState() {
  if(pollInFlight||stopped)return;
  pollInFlight=true;
  try {const data=await api('/api/state',undefined,'GET');applyState(data);}
  catch(err) {if(!state.project)$('footerStatus').textContent='로컬 서버 연결을 기다리는 중';}
  finally{pollInFlight=false;}
}
function jobActive(job) {return job&&['running','queued','pending','processing'].includes(job.status);}
function applyState(data) {
  const previousProject=project;
  state={...state,...data};project=state.project;
  busy=jobActive(state.job);
  if(project?.cameras?.length){
    const newKey=`${project.id||''}|${project.folder||''}|${(project.clips||[]).map(c=>c.id).join(',')}`;
    const changed=newKey!==currentProjectKey;
    if(changed){
      currentProjectKey=newKey;currentStructureKey='';currentMediaKey='';pause();
      cameraOrder=cameras().map(c=>c.id);audioCamera=cameras().find(c=>clipsFor(c.id).some(v=>v.hasAudio))?.id||'none';previewAudio='none';
      [viewStart,viewEnd]=bounds();
      const overlaps=commonIntervals();
      const suggested=overlaps.length?overlaps.reduce((a,b)=>b[1]-b[0]>a[1]-a[0]?b:a):firstUsefulInterval();
      exportStart=suggested[0];exportEnd=suggested[1];globalTime=exportStart;
      restoreUiSettings(project.ui);
      if(viewEnd-viewStart>600&&exportEnd>exportStart){viewStart=Math.max(bounds()[0],exportStart-3);viewEnd=Math.min(bounds()[1],exportEnd+3);}
      setStep(3);renderSources();renderAudio();renderOrder();renderStage();renderRangeInputs();
    }
    const structureKey=JSON.stringify(project.clips.map(c=>[c.id,c.start,c.autoStart,c.sync]))+JSON.stringify(cameras().map(c=>[c.id,c.offset]));
    if(changed||structureKey!==currentStructureKey){
      currentStructureKey=structureKey;
      if(!changed&&requestedJobKind==='sync'&&!busy){
        const overlaps=commonIntervals();if(overlaps.length){const best=overlaps.reduce((a,b)=>b[1]-b[0]>a[1]-a[0]?b:a);exportStart=best[0];exportEnd=best[1];globalTime=best[0];[viewStart,viewEnd]=bounds();if(viewEnd-viewStart>600){viewStart=Math.max(bounds()[0],exportStart-3);viewEnd=Math.min(bounds()[1],exportEnd+3);}renderRangeInputs();}
      }
      renderSources();renderManual();renderTimeline();renderOverlaps();renderSyncSummary();renderWarnings();renderStats();updateRangeVisuals();
    }
    const mediaKey=JSON.stringify(project.clips.map(c=>[c.id,mediaReady(c)]));
    if(mediaKey!==currentMediaKey){currentMediaKey=mediaKey;updateVideos(true);renderStats();}
    if(!changed&&previousProject===null)renderStage();
    updateControls();
  } else updateControls();
  renderExports();renderJob();
  const j=state.job;
  if(j&&!busy&&processedJob!==j.id){
    processedJob=j.id;
    const kind=requestedJobKind;requestedJobKind=null;
    if(j.status==='failed'||j.status==='error'){workflowQueue=[];showToast(j.error||j.message||'작업을 완료하지 못했습니다.',true);}
    else if(j.status==='cancelled'||j.status==='canceled'){workflowQueue=[];showToast('작업을 취소했습니다.');}
    else if(['completed','complete','done','success'].includes(j.status)){
      if(kind==='export'){showToast('추출이 완료되었습니다. 아래 다운로드 버튼으로 저장하세요.');selectTab('export');}
      else if(kind==='sync'){
        const overlaps=commonIntervals();
        if(overlaps.length){const best=overlaps.reduce((a,b)=>b[1]-b[0]>a[1]-a[0]?b:a);exportStart=Math.max(0,best[0]);exportEnd=best[1];globalTime=exportStart;[viewStart,viewEnd]=bounds();if(viewEnd-viewStart>600){viewStart=Math.max(bounds()[0],exportStart-3);viewEnd=Math.min(bounds()[1],exportEnd+3);}renderRangeInputs();renderTimeline();updateVideos(true);}
        showToast('자동 싱크 분석을 마쳤습니다. 영상과 소리를 확인하고 미세 조정하세요.');setStep(3);
      }
      else if(kind==='proxies'){showToast('미리보기 영상이 준비되었습니다.');}
      if(workflowQueue.length){const next=workflowQueue.shift();setTimeout(()=>startJob(next.path,next.body,next.kind).catch(()=>{}),100);}
    }
  }
}
async function startJob(path,body,kind) {
  if(busy){showToast('진행 중인 작업을 마친 후 실행하세요.');return;}
  pause();busy=true;requestedJobKind=kind;updateControls();
  if(kind==='sync')setStep(2);if(kind==='export')setStep(4);
  try {const data=await api(path,body);applyState(data);if(!data.job)await getState();}
  catch(err){busy=false;requestedJobKind=null;workflowQueue=[];updateControls();showToast(err.message,true);throw err;}
}
function updateControls() {
  const has=Boolean(project?.cameras?.length);
  for(const id of ['autoSync','preparePreview','startExport','setCommonRange','playPause','previousFrame','nextFrame','fitTimeline','focusRange','startHere','endHere','goExport']) $(id).disabled=!has||(busy&&['autoSync','preparePreview','startExport'].includes(id));
  $('openFolder').disabled=busy;$('welcomeFolder')?.toggleAttribute('disabled',busy);$('loadSample')?.toggleAttribute('disabled',busy);
  $('saveProject').style.pointerEvents=has?'':'none';$('saveProject').style.opacity=has?'1':'.4';
  $('loadProject').disabled=busy;
  document.querySelectorAll('.manual-controls input,.manual-controls button,.camera-order button').forEach(el=>el.disabled=busy||el.dataset.boundary==='true');
}
function renderJob() {
  $('jobPanel').classList.toggle('hidden',!busy);
  if(!busy){
    const status=state.job?.status;const text=['error','failed'].includes(status)?'마지막 작업 실패 · 메시지를 확인하세요':['cancelled','canceled'].includes(status)?'작업 취소됨':project?'프로젝트 준비 완료':'준비 완료';
    $('footerStatus').innerHTML='<i></i>'+esc(text);return;
  }
  const job=state.job||{};const progress=clamp(numeric(job.progress),0,100);
  const title=requestedJobKind==='import'?'촬영 폴더 분석 중':requestedJobKind==='sync'?'자동 싱크 분석 중':requestedJobKind==='proxies'?'미리보기 영상 준비 중':requestedJobKind==='export'?'영상 추출 중':'작업 진행 중';
  $('jobTitle').textContent=title;$('jobPercent').textContent=Math.round(progress)+'%';$('jobProgress').style.width=progress+'%';$('jobMessage').textContent=job.message||'작업을 준비하고 있습니다.';
  $('footerStatus').innerHTML='<i></i>'+esc(title);
}
function renderStats() {
  const clips=project?.clips||[];const duration=clips.reduce((s,c)=>s+numeric(c.duration),0);
  $('cameraCount').textContent=`${cameras().length} CAM`;
  $('projectStats').textContent=`${cameras().length} CAM / ${clips.length} CLIPS / ${durationText(duration)}`;
  $('totalTime').textContent=fmt(bounds()[1]);
  const ready=clips.filter(mediaReady).length;
  $('previewStatus').textContent=ready===clips.length?'호환 미리보기 준비 완료':`${cameras().length}개 카메라 · ${ready}/${clips.length} 미리보기 준비`;
  if(!busy)$('footerStatus').innerHTML='<i></i>프로젝트 준비 완료';
}
function renderSources() {
  const folder=project.folder||'';const basename=folder.split(/[\\/]/).filter(Boolean).pop()||'촬영 프로젝트';
  $('folderInfo').classList.remove('hidden');$('folderInfo').innerHTML=`<strong>${esc(basename)}</strong>${esc(folder)}`;
  $('sourceList').innerHTML=cameras().map(c=>{
    const list=clipsFor(c.id);const total=list.reduce((s,v)=>s+numeric(v.duration),0);const dims=list[0]?`${list[0].width||'?'}×${list[0].height||'?'}`:'';
    return `<section class="source-card" style="--camera-color:${esc(colorFor(c.id))}"><div class="source-card-top"><i class="camera-dot"></i><strong title="${esc(c.name)}">${esc(c.name)}</strong><span>${list.length} CLIPS</span></div><div class="source-card-info"><span>${esc(dims)}</span><span>·</span><span>${fmt(total,true)}</span><span>·</span><span>${list.some(v=>v.hasAudio)?'AUDIO':'NO AUDIO'}</span></div><div class="source-clips">${list.map(v=>`<button class="source-clip" data-clip-jump="${esc(v.id)}" title="${esc(v.name)}">${icon('play')}<span>${esc(v.name)}</span><small>${fmt(v.duration,true)}</small></button>`).join('')}</div></section>`;
  }).join('');
}
function renderAudio() {
  const options=cameras().filter(c=>clipsFor(c.id).some(v=>v.hasAudio)).map(c=>`<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('');
  $('previewAudio').innerHTML='<option value="none">음소거</option>'+options;$('previewAudio').value=previewAudio;
  $('audioCamera').innerHTML='<option value="none">오디오 없음</option>'+options;$('audioCamera').value=audioCamera;
}
function renderStage() {
  if(!project)return;
  pauseVideos();videoStates.clear();
  $('videoStage').className=`video-stage layout-${layout}`;
  $('videoStage').style.setProperty('--grid-columns',Math.ceil(Math.sqrt(cameraOrder.length)));
  applyAspectRatio();
  $('videoStage').innerHTML=cameraOrder.map((id,i)=>{
    const cam=cameras().find(c=>c.id===id);if(!cam)return '';
    return `<div class="camera-view ${layout==='pip'&&i?'pip-small':''}" data-camera-view="${esc(id)}" style="--camera-color:${esc(colorFor(id))};${layout==='pip'&&i?pipStyle(i):''}"><video preload="auto" playsinline muted></video><div class="camera-empty">${icon('camera')}<span>이 시각에 촬영된 영상이 없습니다</span></div><span class="camera-badge"><i class="camera-dot"></i>${esc(cam.name)}</span><span class="clip-label"></span><div class="preview-error hidden">브라우저에서 원본 재생이 어렵습니다.<br>미리보기 영상 준비가 끝나면 자동으로 전환됩니다.</div></div>`;
  }).join('');
  document.querySelectorAll('[data-camera-view]').forEach(el=>{
    const video=el.querySelector('video');const id=el.dataset.cameraView;
    const record={video,el,clip:null,src:'',seekPending:null,failed:false};videoStates.set(id,record);
    video.addEventListener('loadedmetadata',()=>{record.failed=false;el.querySelector('.preview-error').classList.add('hidden');if(record.seekPending!==null){try{video.currentTime=clamp(record.seekPending,0,Math.max(0,video.duration-.001));}catch{}record.seekPending=null;}});
    video.addEventListener('error',()=>{record.failed=true;el.querySelector('.preview-error').classList.remove('hidden');});
    video.addEventListener('canplay',()=>{if(playing&&!buffering)video.play().catch(()=>{});});
  });
  updateVideos(true);
}
function updateVideos(force=false) {
  if(!project)return;
  for(const [id,r] of videoStates){
    const clip=activeClip(id,globalTime);const v=r.video;
    r.el.querySelector('.camera-empty').classList.toggle('hidden',Boolean(clip));v.style.visibility=clip?'visible':'hidden';
    if(!clip){v.pause();r.el.querySelector('.clip-label').textContent='';r.el.querySelector('.preview-error').classList.add('hidden');r.clip=null;continue;}
    const src=`/media/${encodeURIComponent(clip.id)}${mediaReady(clip)?'?proxy=1':''}`;
    const local=clamp(globalTime-numeric(clip.start)-camOffset(id),0,Math.max(0,numeric(clip.duration)-.001));
    const baseRate=numeric($('playbackRate').value,1);const drift=local-v.currentTime;
    const correction=playbackCorrection(drift,id===playbackMasterId,playing);
    if(r.src!==src){v.pause();r.src=src;r.clip=clip;r.failed=false;r.seekPending=local;v.src=src;v.load();r.el.querySelector('.preview-error').classList.add('hidden');}
    else if(force||correction.seek){if(v.readyState>=1&&!v.seeking){try{v.currentTime=local;}catch{r.seekPending=local;}}else r.seekPending=local;}
    r.clip=clip;v.muted=id!==previewAudio;v.volume=1;v.playbackRate=baseRate*(force?1:correction.rate);
    r.el.querySelector('.clip-label').textContent=`${clip.name}  ·  ${fmt(local)}`;
    if(playing&&v.paused&&!r.failed&&v.readyState>=2)v.play().catch(()=>{});if(!playing&&!v.paused)v.pause();
  }
  $('currentTime').textContent=fmt(globalTime);$('scrubber').value=String(clamp(globalTime,viewStart,viewEnd));updatePlayheads();
}
function pauseVideos() {for(const r of videoStates.values())r.video.pause();}
function pause() {playing=false;if(buffering){buffering=false;if(project)renderStats();}if(frameHandle)cancelAnimationFrame(frameHandle);frameHandle=null;pauseVideos();if(project)updateVideos(true);$('playPause').classList.remove('playing');$('playPause').innerHTML=icon('play');$('playPause').setAttribute('aria-label','재생');}
function play() {
  if(!project)return;
  if(globalTime>=bounds()[1]-.02)globalTime=exportStart;
  playbackMasterId=null;playing=true;lastClock=performance.now();$('playPause').classList.add('playing');$('playPause').innerHTML=icon('pause');$('playPause').setAttribute('aria-label','일시 정지');updateVideos(true);frameHandle=requestAnimationFrame(tick);
}
function playbackCorrection(drift,isMaster,isPlaying) {
  if(isMaster&&isPlaying)return {seek:false,rate:1};
  if(Math.abs(drift)>.04)return {seek:true,rate:1};
  return {seek:false,rate:isPlaying&&Math.abs(drift)>.01?clamp(1+drift*1.2,.97,1.03):1};
}
function tick(now) {
  if(!playing)return;
  const waiting=[...videoStates.values()].some(r=>r.clip&&!r.failed&&(r.video.readyState<3||r.video.seeking));
  if(waiting){
    buffering=true;lastClock=now;updateVideos(false);pauseVideos();$('previewStatus').textContent='카메라 영상 버퍼를 함께 기다리는 중';frameHandle=requestAnimationFrame(tick);return;
  }
  if(buffering){buffering=false;lastClock=now;renderStats();}
  const elapsed=Math.min((now-lastClock)/1000,.25);lastClock=now;
  const candidates=[...new Set([previewAudio,...cameraOrder])];
  const masterId=candidates.find(id=>{const r=videoStates.get(id);return r?.clip&&!r.failed&&r.video.readyState>=3&&!r.video.ended&&r.video.currentTime<numeric(r.clip.duration)-.025;});
  playbackMasterId=masterId||null;
  if(masterId){const r=videoStates.get(masterId);globalTime=numeric(r.clip.start)+camOffset(masterId)+r.video.currentTime;}
  else globalTime+=elapsed*numeric($('playbackRate').value,1);
  if(globalTime>=bounds()[1]){globalTime=bounds()[1];pause();updateVideos(true);return;}
  updateVideos(false);frameHandle=requestAnimationFrame(tick);
}
function seek(time,pausePlayback=false) {if(!project)return;if(pausePlayback)pause();globalTime=clamp(numeric(time),...bounds());updateVideos(true);}
function renderManual() {
  const focused=document.activeElement;const focusKey=focused?.dataset?.offsetCamera;const focusClip=focused?.dataset?.clipAdjust;
  const openDetails=new Set([...$('manualControls').querySelectorAll('details[open]')].map(d=>d.closest('.manual-card').querySelector('[data-offset-camera]').dataset.offsetCamera));
  $('manualControls').innerHTML=cameras().map(cam=>{
    const list=clipsFor(cam.id);const ms=numeric(cam.offset)*1000;const fps=numeric(list[0]?.fps,30)||30;
    return `<div class="manual-card" style="--camera-color:${esc(colorFor(cam.id))}"><div class="manual-title"><strong><i class="camera-dot"></i>${esc(cam.name)}</strong><button class="text-button" data-reset-camera="${esc(cam.id)}">초기화</button></div><div class="offset-input"><input type="number" step="1" value="${Math.round(ms*1000)/1000}" data-offset-camera="${esc(cam.id)}" aria-label="${esc(cam.name)} 수동 오프셋 (밀리초)"><span>ms</span></div><div class="nudge-buttons"><button data-nudge="${esc(cam.id)}" data-delta="-0.1" title="100 밀리초 앞으로">−100 ms</button><button data-nudge="${esc(cam.id)}" data-delta="${-1/fps}" title="${fps.toFixed(2)} fps 기준 한 프레임 앞으로">−1 F</button><button data-nudge="${esc(cam.id)}" data-delta="${1/fps}" title="${fps.toFixed(2)} fps 기준 한 프레임 뒤로">+1 F</button><button data-nudge="${esc(cam.id)}" data-delta="0.1" title="100 밀리초 뒤로">+100 ms</button></div><details><summary>클립별 추가 조정 · ${list.length}개</summary>${list.map(c=>`<div class="clip-adjustment"><label title="${esc(c.name)}">${esc(c.name)}</label><div class="offset-input"><input type="number" step="1" value="${Math.round((numeric(c.start)-numeric(c.autoStart,c.start))*1000000)/1000}" data-clip-adjust="${esc(c.id)}" aria-label="${esc(c.name)} 자동 싱크 대비 추가 오프셋 (밀리초)"><span>ms</span></div><div class="clip-sync">${syncDescription(c)}<br>시작 ${fmt(numeric(c.start)+ms/1000)}</div></div>`).join('')}</details></div>`;
  }).join('');
  $('manualControls').querySelectorAll('.manual-card').forEach(card=>{if(openDetails.has(card.querySelector('[data-offset-camera]').dataset.offsetCamera))card.querySelector('details').open=true;});
  if(focusKey){const input=[...$('manualControls').querySelectorAll('[data-offset-camera]')].find(e=>e.dataset.offsetCamera===focusKey);input?.focus();}
  if(focusClip){const input=[...$('manualControls').querySelectorAll('[data-clip-adjust]')].find(e=>e.dataset.clipAdjust===focusClip);if(input){input.closest('details').open=true;input.focus();}}
}
function syncDescription(clip) {
  const sync=clip.sync||{};const method=sync.method||'미분석';const names={audio:'오디오',timecode:'타임코드',reference:'기준 영상',manual:'수동',filename:'파일 시각',creation_time:'촬영 시각',metadata:'촬영 시각',sequential:'순차 배치',unconfirmed:'미확정',unknown:'미분석',none:'미분석'};
  let confidence='';if(Number.isFinite(Number(sync.confidence))){const n=Number(sync.confidence);confidence=` · 신뢰도 ${Math.round(n<=1?n*100:n)}%`;}
  return esc((names[method]||method)+confidence+(sync.note?' · '+sync.note:''));
}
function renderSyncSummary() {
  const clips=project.clips||[];const analyzed=clips.filter(c=>c.sync&&c.sync.method&&!['none','unknown'].includes(c.sync.method));
  $('syncSummary').textContent=analyzed.length?`${clips.length}개 클립 중 ${analyzed.length}개 분석 정보 있음 · 결과를 재생해서 확인하세요.`:'자동 싱크를 실행하면 분석 결과가 표시됩니다.';
}
function persistUpdate(body) {
  pause();
  updateQueue=updateQueue.then(async()=>{try{const data=await api('/api/update',typeof body==='function'?body():body);applyState(data);updateVideos(true);}catch(err){showToast(err.message,true);await getState();}});
  return updateQueue;
}
function commonIntervals() {
  if(cameras().length<2)return [];
  let result=clipsFor(cameras()[0].id).map(c=>[numeric(c.start)+camOffset(c.cameraId),numeric(c.start)+camOffset(c.cameraId)+numeric(c.duration)]);
  for(const cam of cameras().slice(1)){
    const next=[];const intervals=clipsFor(cam.id).map(c=>[numeric(c.start)+camOffset(c.cameraId),numeric(c.start)+camOffset(c.cameraId)+numeric(c.duration)]);
    for(const a of result)for(const b of intervals){const s=Math.max(a[0],b[0]),e=Math.min(a[1],b[1]);if(e-s>.05)next.push([s,e]);}
    result=next;
  }
  result.sort((a,b)=>a[0]-b[0]);const merged=[];
  for(const interval of result){const last=merged[merged.length-1];if(last&&interval[0]<=last[1]+.02)last[1]=Math.max(last[1],interval[1]);else merged.push([...interval]);}
  return merged;
}
function firstUsefulInterval() {
  const list=(project?.clips||[]).slice().sort((a,b)=>numeric(a.start)-numeric(b.start));
  const first=list[0];if(!first)return [0,0];
  const start=numeric(first.start)+camOffset(first.cameraId);return [start,start+Math.min(60,numeric(first.duration))];
}
function renderOverlaps() {
  const overlaps=commonIntervals();
  $('overlapSelect').innerHTML=`<option value="">${overlaps.length?`모든 카메라 공통 구간 · ${overlaps.length}개`:'모든 카메라가 겹치는 구간 없음'}</option>`+overlaps.map((x,i)=>`<option value="${i}">${i+1}. ${fmt(x[0],true)} – ${fmt(x[1],true)} (${durationText(x[1]-x[0])})</option>`).join('');
  $('rangeHelp').textContent=overlaps.length?'공통 구간을 선택하면 모든 카메라를 동시에 볼 수 있습니다. 촬영이 없는 부분은 검은 화면으로 채웁니다.':'공통 구간이 없습니다. 수동 싱크로 맞추거나 원하는 구간을 직접 입력하세요. 촬영이 없는 부분은 검은 화면으로 채웁니다.';
}
function renderRangeInputs() {$('rangeStart').value=fmt(exportStart);$('rangeEnd').value=fmt(exportEnd);updateRangeVisuals();}
function updateRangeVisuals() {
  const valid=exportEnd>exportStart;$('rangeDuration').textContent=valid?durationText(exportEnd-exportStart):'구간을 확인하세요';$('rangeDuration').style.color=valid?'':'var(--danger)';
  document.querySelectorAll('.range-highlight').forEach(el=>{el.style.left=((exportStart-viewStart)/(viewEnd-viewStart)*100)+'%';el.style.width=((exportEnd-exportStart)/(viewEnd-viewStart)*100)+'%';});
}
function renderOrder() {
  $('cameraOrder').innerHTML=cameraOrder.map((id,i)=>`<div class="order-row"><span class="order-no">${i+1}</span><i class="camera-dot" style="--camera-color:${esc(colorFor(id))}"></i><strong>${esc(cameras().find(c=>c.id===id)?.name||id)}</strong><button data-order="${esc(id)}" data-dir="-1" ${i===0?'disabled data-boundary="true"':''} aria-label="${esc(id)} 위로">↑</button><button data-order="${esc(id)}" data-dir="1" ${i===cameraOrder.length-1?'disabled data-boundary="true"':''} aria-label="${esc(id)} 아래로">↓</button></div>`).join('');
}
function renderTimeline() {
  if(!project)return;
  if(!(viewEnd>viewStart)){[viewStart,viewEnd]=bounds();}
  const span=viewEnd-viewStart;
  $('timelineRuler').innerHTML=Array.from({length:5},(_,i)=>`<span class="ruler-tick" style="left:${i*25}%">${fmt(viewStart+span*i/4,true)}</span>`).join('');
  $('timelineTracks').innerHTML=cameraOrder.map(id=>{
    const cam=cameras().find(c=>c.id===id);if(!cam)return '';
    return `<div class="timeline-track" style="--camera-color:${esc(colorFor(id))}"><div class="track-label"><strong><i class="camera-dot"></i>${esc(cam.name)}</strong><small>${numeric(cam.offset)>=0?'+':''}${(numeric(cam.offset)*1000).toFixed(1)} ms</small></div><div class="track-lane" data-lane="${esc(id)}">${clipsFor(id).filter(c=>numeric(c.start)+camOffset(id)+numeric(c.duration)>=viewStart&&numeric(c.start)+camOffset(id)<=viewEnd).map(c=>{
      const start=numeric(c.start)+camOffset(id);return `<button class="timeline-clip" data-clip-id="${esc(c.id)}" style="left:${(start-viewStart)/span*100}%;width:${numeric(c.duration)/span*100}%" title="${esc(c.name)} · ${fmt(start)} – ${fmt(start+numeric(c.duration))}"><span class="clip-name">${esc(c.name)}</span><canvas data-waveform="${esc(c.id)}" aria-label="${esc(c.name)} 오디오 파형"></canvas></button>`;
    }).join('')}<div class="range-highlight"></div><div class="playhead"></div></div></div>`;
  }).join('');
  $('scrubber').min=viewStart;$('scrubber').max=viewEnd;$('scrubber').value=clamp(globalTime,viewStart,viewEnd);
  updateRangeVisuals();updatePlayheads();drawWaveforms();
}
function updatePlayheads(){const pos=(globalTime-viewStart)/(viewEnd-viewStart)*100;document.querySelectorAll('.playhead').forEach(el=>{el.style.left=pos+'%';el.style.display=pos<0||pos>100?'none':'';});}
async function drawWaveforms() {
  const canvases=[...document.querySelectorAll('[data-waveform]')];
  for(const canvas of canvases){
    const id=canvas.dataset.waveform;const clip=project?.clips.find(c=>c.id===id);if(!clip?.hasAudio)continue;
    if(waveforms.has(id)){paintWaveform(canvas,waveforms.get(id),colorFor(clip.cameraId));continue;}
    if(waveformPending.has(id))continue;
    waveformPending.add(id);
    fetch('/api/waveform/'+encodeURIComponent(id)).then(res=>res.ok?res.json():null).then(data=>{
      const values=Array.isArray(data)?data:data?.peaks||data?.waveform||data?.samples||data?.values;
      if(Array.isArray(values)){waveforms.set(id,values);document.querySelectorAll('[data-waveform]').forEach(c=>{if(c.dataset.waveform===id)paintWaveform(c,values,colorFor(clip.cameraId));});}
    }).catch(()=>{}).finally(()=>waveformPending.delete(id));
  }
}
function paintWaveform(canvas,data,color) {
  if(!data.length)return;const width=Math.min(1800,Math.max(10,canvas.clientWidth));const height=22;canvas.width=width*2;canvas.height=height*2;const ctx=canvas.getContext('2d');ctx.scale(2,2);ctx.clearRect(0,0,width,height);ctx.strokeStyle=color;ctx.lineWidth=1;
  const max=Math.max(.01,...data.map(x=>Array.isArray(x)?Math.max(...x.map(Math.abs)):Math.abs(numeric(x))));ctx.beginPath();
  for(let x=0;x<width;x+=2){const from=Math.floor(x/width*data.length),to=Math.min(data.length,Math.max(from+1,Math.floor((x+2)/width*data.length)));let value=0;for(let i=from;i<to;i++){const sample=data[i];value=Math.max(value,Array.isArray(sample)?Math.max(...sample.map(Math.abs)):Math.abs(numeric(sample)));}const amplitude=Math.max(.4,value/max*height*.44);ctx.moveTo(x,height/2-amplitude);ctx.lineTo(x,height/2+amplitude);}
  ctx.stroke();
}
function renderWarnings() {
  const warnings=project?.warnings||[];$('warnings').classList.toggle('hidden',!warnings.length);
  $('warnings').innerHTML=warnings.length?`<details><summary>${icon('info')} 확인이 필요한 항목 ${warnings.length}개</summary><ul>${warnings.map(x=>`<li>${esc(typeof x==='string'?x:x.message||JSON.stringify(x))}</li>`).join('')}</ul></details>`:'';
}
function renderExports() {
  const exports=state.exports||[];
  $('exportList').innerHTML=exports.length?'<div class="section-title"><h3>완료된 파일</h3><span class="mini-tag">DOWNLOAD</span></div>'+exports.map(f=>`<a class="export-item" href="${esc(f.url||'/api/download/'+encodeURIComponent(f.id))}" download>${icon('download')}<div><strong>${esc(f.name)}</strong><small>${sizeText(f.size)} · 다운로드</small></div>${icon('check')}</a>`).join(''):'';
}
function openFolderDialog() {if(busy)return;$('folderPath').value=project?.folder||state.sampleFolder||'';$('folderDialog').showModal();setTimeout(()=>$('folderPath').focus(),50);}
async function importFolder(path) {
  if(!path.trim()){showToast('촬영 폴더 경로를 입력하세요.',true);return;}
  const tasks=$('syncAfterImport').checked?[{path:'/api/sync',body:{method:'auto'},kind:'sync'}]:[];
  tasks.push({path:'/api/proxies',body:{},kind:'proxies'});workflowQueue=tasks;
  $('folderDialog').close();setStep(1);
  await startJob('/api/import',{path:path.trim()},'import').catch(()=>{});
}
function chooseCommonInterval(index) {
  const list=commonIntervals();const interval=list[index];
  if(!interval){showToast('모든 카메라가 겹치는 구간이 없습니다. 수동 싱크를 확인하세요.',true);return;}
  exportStart=Math.max(0,interval[0]);exportEnd=interval[1];renderRangeInputs();seek(exportStart,true);
  viewStart=Math.max(bounds()[0],exportStart-1);viewEnd=Math.min(bounds()[1],exportEnd+1);renderTimeline();
}
function outputSize() {
  let [width,height]=$('resolution').value.split('x').map(Number);const aspect=$('aspectRatio').value;
  if(aspect==='32:9')height=Math.round(height/4)*2;
  else if(aspect==='9:16')[width,height]=[height,width];
  return [width,height];
}
function applyAspectRatio() {
  const [width,height]=outputSize();if(project)$('videoStage').style.aspectRatio=`${width} / ${height}`;
  $('outputDimensions').textContent=`통합 영상 크기: ${width} × ${height}`;
  if(layout==='pip')$('videoStage').querySelectorAll('.camera-view').forEach((el,i)=>{if(i)el.style.cssText=`--camera-color:${colorFor(cameraOrder[i])};${pipStyle(i)}`;});
}
function pipStyle(index) {
  const count=cameraOrder.length;if(count<2||index===0)return '';
  const [width,height]=outputSize();const gap=Math.max(0,Math.min(8,Math.floor((height-24-2*(count-1))/(count-1))));
  const h=Math.max(2,Math.floor(Math.min(height*.28,(height-24)/(count-1)-gap)/2)*2);
  const w=Math.max(2,Math.floor(h*width/height/2)*2);
  const x=width-w-12;const y=height-12-(count-index)*(h+gap);
  return `left:${x/width*100}%;top:${y/height*100}%;right:auto;width:${w/width*100}%;height:${h/height*100}%;`;
}
function collectUiSettings() {
  return {layout,order:cameraOrder,start:exportStart,end:exportEnd,aspectRatio:$('aspectRatio').value,resolution:$('resolution').value,fps:$('exportFps').value,audioCamera:$('audioCamera').value,mode:$('exportMode').value,filename:$('exportFilename').value};
}
function restoreUiSettings(ui) {
  if(!ui||typeof ui!=='object')return;
  if(['horizontal','vertical','grid','pip'].includes(ui.layout))layout=ui.layout;
  if(Array.isArray(ui.order)&&ui.order.length===cameras().length&&new Set(ui.order).size===cameras().length&&ui.order.every(id=>cameras().some(c=>c.id===id)))cameraOrder=[...ui.order];
  if(Number.isFinite(ui.start)&&Number.isFinite(ui.end)&&ui.end>ui.start){exportStart=ui.start;exportEnd=ui.end;globalTime=ui.start;}
  for(const [id,key] of [['aspectRatio','aspectRatio'],['resolution','resolution'],['exportFps','fps'],['exportMode','mode']]){if([...$(id).options].some(o=>o.value===String(ui[key])))$(id).value=String(ui[key]);}
  if(cameras().some(c=>c.id===ui.audioCamera)||ui.audioCamera==='none')audioCamera=ui.audioCamera;
  if(typeof ui.filename==='string')$('exportFilename').value=ui.filename.slice(0,100);
  document.querySelectorAll('[data-layout]').forEach(b=>b.classList.toggle('active',b.dataset.layout===layout));
  applyAspectRatio();
}
function bindEvents() {
  $('openFolder').addEventListener('click',openFolderDialog);$('welcomeFolder').addEventListener('click',openFolderDialog);$('closeFolder').addEventListener('click',()=>$('folderDialog').close());
  $('folderDialog').addEventListener('click',e=>{if(e.target===$('folderDialog')){const r=e.target.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)e.target.close();}});
  $('folderForm').addEventListener('submit',e=>{e.preventDefault();importFolder($('folderPath').value);});
  $('browseFolder').addEventListener('click',async()=>{const b=$('browseFolder');b.disabled=true;b.textContent='선택 중…';try{const data=await api('/api/pick-folder',{});if(data.path)$('folderPath').value=data.path;}catch(err){showToast(err.message+' 폴더 경로를 직접 입력할 수도 있습니다.',true);}finally{b.disabled=false;b.textContent='찾아보기';}});
  $('dialogSample').addEventListener('click',()=>{if(state.sampleFolder)$('folderPath').value=state.sampleFolder;else showToast('설정된 샘플 폴더가 없습니다. 경로를 직접 입력하세요.',true);});
  $('loadSample').addEventListener('click',()=>{if(!state.sampleFolder){openFolderDialog();return;}importFolder(state.sampleFolder);});
  $('syncTab').addEventListener('click',()=>selectTab('sync'));$('exportTab').addEventListener('click',()=>selectTab('export'));$('goExport').addEventListener('click',()=>{selectTab('export');$('exportTab').scrollIntoView({block:'nearest',behavior:'smooth'});});
  document.querySelectorAll('[data-step]').forEach(b=>b.addEventListener('click',()=>{if(b.dataset.step==='1'){openFolderDialog();return;}if(!project){showToast('먼저 촬영 폴더를 불러오세요.');return;}selectTab(b.dataset.step==='4'?'export':'sync');setStep(Number(b.dataset.step));}));
  $('autoSync').addEventListener('click',()=>{workflowQueue=[];startJob('/api/sync',{method:$('syncMethod').value},'sync').catch(()=>{});});
  $('preparePreview').addEventListener('click',()=>{workflowQueue=[];startJob('/api/proxies',{},'proxies').catch(()=>{});});
  $('cancelJob').addEventListener('click',async()=>{workflowQueue=[];try{const data=await api('/api/cancel',{});applyState(data);showToast('취소 요청을 보냈습니다. 현재 처리가 정리될 때까지 기다려 주세요.');}catch(err){showToast(err.message,true);}});
  $('playPause').addEventListener('click',()=>playing?pause():play());$('previousFrame').addEventListener('click',()=>seek(globalTime-frameSize(),true));$('nextFrame').addEventListener('click',()=>seek(globalTime+frameSize(),true));
  $('scrubber').addEventListener('input',e=>seek(Number(e.target.value),false));
  $('previewAudio').addEventListener('change',e=>{previewAudio=e.target.value;updateVideos(true);});$('audioCamera').addEventListener('change',e=>audioCamera=e.target.value);$('playbackRate').addEventListener('change',()=>updateVideos(true));
  $('fullscreen').addEventListener('click',async()=>{try{if(document.fullscreenElement)await document.exitFullscreen();else await $('videoStage').requestFullscreen();}catch{showToast('이 브라우저에서는 전체 화면을 사용할 수 없습니다.',true);}});
  $('fitTimeline').addEventListener('click',()=>{[viewStart,viewEnd]=bounds();renderTimeline();});$('focusRange').addEventListener('click',()=>{if(exportEnd<=exportStart)return;viewStart=Math.max(bounds()[0],exportStart-1);viewEnd=Math.min(bounds()[1],exportEnd+1);renderTimeline();});
  $('sourceList').addEventListener('click',e=>{const b=e.target.closest('[data-clip-jump]');if(!b)return;const c=project.clips.find(c=>c.id===b.dataset.clipJump);if(c){const t=numeric(c.start)+camOffset(c.cameraId);seek(t,true);if(t<viewStart||t>viewEnd){viewStart=Math.max(bounds()[0],t-2);viewEnd=Math.min(bounds()[1],t+numeric(c.duration)+2);renderTimeline();}}});
  $('timelineTracks').addEventListener('click',e=>{const lane=e.target.closest('[data-lane]');if(!lane)return;const rect=lane.getBoundingClientRect();seek(viewStart+(e.clientX-rect.left)/rect.width*(viewEnd-viewStart),false);});
  $('manualControls').addEventListener('click',e=>{
    const nudge=e.target.closest('[data-nudge]');const reset=e.target.closest('[data-reset-camera]');
    if(nudge&&!busy){const id=nudge.dataset.nudge;const delta=Number(nudge.dataset.delta);persistUpdate(()=>({cameraOffsets:{[id]:camOffset(id)+delta}}));}
    if(reset&&!busy)persistUpdate({cameraOffsets:{[reset.dataset.resetCamera]:0}});
  });
  $('manualControls').addEventListener('change',e=>{
    if(busy)return;const value=Number(e.target.value);if(!Number.isFinite(value)){showToast('유효한 밀리초 값을 입력하세요.',true);return;}
    if(e.target.dataset.offsetCamera)persistUpdate({cameraOffsets:{[e.target.dataset.offsetCamera]:value/1000}});
    if(e.target.dataset.clipAdjust){const c=project.clips.find(c=>c.id===e.target.dataset.clipAdjust);if(c)persistUpdate({clipStarts:{[c.id]:numeric(c.autoStart,c.start)+value/1000}});}
  });
  document.querySelectorAll('[data-layout]').forEach(b=>b.addEventListener('click',()=>{layout=b.dataset.layout;document.querySelectorAll('[data-layout]').forEach(x=>x.classList.toggle('active',x===b));renderStage();}));
  $('aspectRatio').addEventListener('change',applyAspectRatio);$('resolution').addEventListener('change',applyAspectRatio);
  $('cameraOrder').addEventListener('click',e=>{const b=e.target.closest('[data-order]');if(!b||b.disabled)return;const i=cameraOrder.indexOf(b.dataset.order),j=i+Number(b.dataset.dir);if(j<0||j>=cameraOrder.length)return;[cameraOrder[i],cameraOrder[j]]=[cameraOrder[j],cameraOrder[i]];renderOrder();renderStage();renderTimeline();});
  $('overlapSelect').addEventListener('change',e=>{if(e.target.value!=='')chooseCommonInterval(Number(e.target.value));});$('setCommonRange').addEventListener('click',()=>{const list=commonIntervals();if(!list.length){chooseCommonInterval(-1);return;}const index=list.findIndex(x=>globalTime>=x[0]&&globalTime<=x[1]);chooseCommonInterval(index<0?0:index);});
  for(const [id,key] of [['rangeStart','start'],['rangeEnd','end']]){
    $(id).addEventListener('input',()=>{const value=parseTime($(id).value);if(!Number.isFinite(value))return;if(key==='start')exportStart=value;else exportEnd=value;updateRangeVisuals();});
    $(id).addEventListener('change',()=>{const value=parseTime($(id).value);if(!Number.isFinite(value)){showToast('시간은 00:00:00.000 또는 초 단위 숫자로 입력하세요.',true);renderRangeInputs();return;}if(key==='start')exportStart=value;else exportEnd=value;renderRangeInputs();});
  }
  $('startHere').addEventListener('click',()=>{exportStart=globalTime;renderRangeInputs();});$('endHere').addEventListener('click',()=>{exportEnd=globalTime;renderRangeInputs();});
  $('startExport').addEventListener('click',async()=>{
    const start=parseTime($('rangeStart').value),end=parseTime($('rangeEnd').value);if(!Number.isFinite(start)||!Number.isFinite(end)||end<=start){showToast('끝 시각이 시작 시각보다 뒤인 추출 구간을 입력하세요.',true);return;}
    exportStart=start;exportEnd=end;renderRangeInputs();
    const limits=bounds();if(start<Math.min(0,limits[0])-.01||end>limits[1]+.1){showToast('추출 구간이 촬영 타임라인을 벗어났습니다.',true);return;}
    const [width,height]=outputSize();workflowQueue=[];
    await updateQueue;
    startJob('/api/export',{mode:$('exportMode').value,layout,order:cameraOrder,start,end,width,height,fps:Number($('exportFps').value),audioCamera:$('audioCamera').value,filename:$('exportFilename').value.trim()||'multicam_sync'},'export').catch(()=>{});
  });
  $('saveProject').addEventListener('click',async e=>{e.preventDefault();if(!project)return;await updateQueue;const contents={...project,ui:collectUiSettings()};const blob=new Blob([JSON.stringify(contents,null,2)],{type:'application/json;charset=utf-8'});const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download='multicam-project.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),30000);showToast('싱크 조정과 화면 배치를 프로젝트 파일로 저장했습니다.');});
  $('loadProject').addEventListener('click',()=>$('projectFile').click());$('projectFile').addEventListener('change',async e=>{const file=e.target.files[0];if(!file)return;try{const data=JSON.parse(await file.text());const result=await api('/api/project-load',{project:data.project||data});currentProjectKey='';applyState(result);showToast('프로젝트를 불러왔습니다.');}catch(err){showToast('프로젝트를 불러오지 못했습니다. '+err.message,true);}finally{e.target.value='';}});
  document.addEventListener('keydown',e=>{
    if(['INPUT','TEXTAREA','SELECT','BUTTON'].includes(e.target.tagName)||e.target.isContentEditable||$('folderDialog').open)return;
    if(e.code==='Space'){e.preventDefault();if(project)playing?pause():play();}
    else if(e.code==='ArrowLeft'){e.preventDefault();seek(globalTime-(e.shiftKey?1:frameSize()),true);}
    else if(e.code==='ArrowRight'){e.preventDefault();seek(globalTime+(e.shiftKey?1:frameSize()),true);}
  });
  window.addEventListener('resize',()=>drawWaveforms());
  const shutdown=document.createElement('button');shutdown.className='text-button small';shutdown.textContent='프로그램 종료';shutdown.style.color='#728a96';shutdown.addEventListener('click',async()=>{if(busy){showToast('작업을 마치거나 취소한 뒤 종료하세요.');return;}try{pause();await api('/api/shutdown',{});stopped=true;document.body.innerHTML='<main style="min-height:100vh;display:grid;place-content:center;text-align:center;gap:18px;padding:30px"><h1 style="font-size:25px">MULTICAM SYNC를 종료했습니다.</h1><p style="color:#8ca5b2">이 탭을 닫으셔도 됩니다. 다시 시작하려면 프로그램을 실행하세요.</p></main>';}catch(err){showToast(err.message,true);}});document.querySelector('.app-footer').append(shutdown);
}

bindEvents();
updateControls();
getState();
setInterval(getState,1200);

