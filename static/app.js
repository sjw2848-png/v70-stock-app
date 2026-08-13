const $ = id => document.getElementById(id);
const SETTINGS_KEY = 'v70.2-settings';
const SNAPSHOT_KEY = 'v70.2-last-snapshot';
const fields = ['budget','tradeBudget','riskPct','stopPct','minRrr','trustMode','mode','topN','held','appPin'];
let allResults = [];
let activeFilter = 'ALL';
let installPrompt = null;

function fmt(n){ return Number(n||0).toLocaleString('ko-KR'); }
function pct(n){ if(n===null || n===undefined) return '-'; const v=Number(n); return `${v>=0?'+':''}${v.toFixed(2)}%`; }
function escapeText(v){ return String(v ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c])); }
function dateLabel(iso){
  if(!iso) return '없음';
  try { return new Intl.DateTimeFormat('ko-KR',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'}).format(new Date(iso)); }
  catch(e){ return iso; }
}
function saveSettings(){
  const data={}; fields.forEach(k=>data[k]=$(k).value); localStorage.setItem(SETTINGS_KEY,JSON.stringify(data));
}
function loadSettings(){
  try{ const data=JSON.parse(localStorage.getItem(SETTINGS_KEY)||'{}'); fields.forEach(k=>{if(data[k]!==undefined) $(k).value=data[k]}); }catch(e){}
}
fields.forEach(k=>$(k).addEventListener('change',saveSettings));

function settingsPayload(){
  return {
    budget:Number($('budget').value||0), trade_budget:Number($('tradeBudget').value||0),
    risk_pct:Number($('riskPct').value||0), stop_pct:Number($('stopPct').value||0),
    min_rrr:Number($('minRrr').value||1.5), trust_mode:$('trustMode').value,
    mode:$('mode').value, top_n:Number($('topN').value||60), held:$('held').value.trim()
  };
}


function beginnerVerdict(item){
  if(!item || !item.ok) return {level:'red', text:'🔴 지금은 사지 말기', why:['데이터를 정상적으로 받지 못했습니다.']};
  const grade=String(item.grade||'C');
  const qty=Number(item.qty||0);
  const rrr=Number(item.rrr||0);
  const mom=Number(item.momentum||0);
  const tech=String(item.tech||'');
  if(grade==='C' || qty<=0){
    return {level:'red', text:'🔴 지금은 사지 말기', why:[`등급 ${grade} · 진입 조건 미충족`, `손익비 1:${rrr.toFixed(2)} · 모멘텀 ${mom.toFixed(0)}점`, '다른 후보를 보는 편이 낫습니다.']};
  }
  if(tech.includes('상따')){
    return {level:'yellow', text:'🟡 초보자는 일단 패스', why:[`${grade}급이지만 상따매매는 변동성이 매우 큽니다.`, `손익비 1:${rrr.toFixed(2)} · 모멘텀 ${mom.toFixed(0)}점`, '초보자용 화면에서는 안전 우선으로 대기 판정합니다.']};
  }
  const waitEntry=Number(item.entry_krw||0) < Number(item.price_krw||0)*0.995;
  if(['S','A'].includes(grade) && mom>=70 && rrr>=1.5){
    if(waitEntry){
      return {level:'green', text:'🟢 이 가격 오면 매수 후보', why:[`${grade}급 · 모멘텀 ${mom.toFixed(0)}점`, `손익비 1:${rrr.toFixed(2)}로 기준 통과`, `${fmt(item.entry_krw)}원 부근까지 기다렸다가 접근`]};
    }
    return {level:'green', text:'🟢 지금 매수 후보', why:[`${grade}급 · 모멘텀 ${mom.toFixed(0)}점`, `손익비 1:${rrr.toFixed(2)}로 기준 통과`, `예산·손실한도 기준 ${qty}주 이내`]};
  }
  return {level:'yellow', text:'🟡 조금 더 기다리기', why:[`${grade}급 · 아직 강한 확신 구간은 아님`, `손익비 1:${rrr.toFixed(2)} · 모멘텀 ${mom.toFixed(0)}점`, 'S/A급 또는 더 좋은 진입가격을 기다립니다.']};
}

function chartLink(item){
  if(item.market==='US') return `https://finance.yahoo.com/quote/${encodeURIComponent(item.code)}`;
  return `https://finance.naver.com/item/main.naver?code=${encodeURIComponent(item.code)}`;
}

function renderCard(item){
  const tpl=$('stockTemplate').content.cloneNode(true);
  const card=tpl.querySelector('.stock-card');
  card.dataset.category=item.category||'';
  card.dataset.grade=item.grade||'C';
  card.dataset.held=item.held_price_krw?'1':'0';
  card.dataset.buy=(item.ok && ['S','A','B'].includes(item.grade) && Number(item.qty)>0)?'1':'0';
  card.dataset.search=`${item.name||''} ${item.code||''}`.toLowerCase();
  card.dataset.momentum=Number(item.momentum||0);
  card.dataset.rrr=Number(item.rrr||0);
  card.dataset.name=String(item.name||item.code||'');
  tpl.querySelector('.name').textContent=item.name||item.code;
  const grade=tpl.querySelector('.grade'); grade.textContent=`${item.grade||'C'}급`; grade.classList.add(`grade-${item.grade||'C'}`);
  tpl.querySelector('.meta').textContent=`${item.code} · ${item.category||item.market} · ${item.sector||''}`;
  tpl.querySelector('.decision').textContent=item.decision||'';
  const bv=beginnerVerdict(item);
  const ba=tpl.querySelector('.beginner-action'); ba.textContent=bv.text; ba.classList.add(`verdict-${bv.level}`);
  tpl.querySelector('.beginner-why').innerHTML=bv.why.slice(0,3).map(x=>`<div>• ${escapeText(x)}</div>`).join('');

  if(!item.ok){
    card.classList.add('error-card');
    tpl.querySelector('.price-row').innerHTML=''; tpl.querySelector('.signal-row').innerHTML=''; tpl.querySelector('.pnl-row').innerHTML='';
    tpl.querySelector('.reason').textContent=item.error||'데이터 수신 실패';
    return tpl;
  }

  tpl.querySelector('.price').textContent=`${fmt(item.price_krw)}원`;
  tpl.querySelector('.entry').textContent=`${fmt(item.entry_krw)}원`;
  tpl.querySelector('.target').textContent=`${fmt(item.target1_krw)}원`;
  tpl.querySelector('.stop').textContent=`${fmt(item.stop1_krw)}원`;
  tpl.querySelector('.momentum').textContent=`모멘텀 ${item.momentum}점`;
  tpl.querySelector('.rrr').textContent=`손익비 1:${item.rrr}`;
  tpl.querySelector('.tech').textContent=item.tech;
  tpl.querySelector('.qty').textContent=item.qty>0?`${item.qty}주 / ${fmt(item.invested_krw)}원`:'진입 보류';
  tpl.querySelector('.profit').textContent=item.qty>0?`1차 기대손익 +${fmt(item.expected_profit_krw)}원`:'';
  tpl.querySelector('.loss').textContent=item.qty>0?`1차 제한손실 ${fmt(item.expected_loss_krw)}원`:'';

  const dg=tpl.querySelector('.detail-grid');
  const rows=[
    ['가정 승률',`${item.assumed_win_rate}%`],['Kelly 상한',`${item.kelly_pct}%`],
    ['ATR',`${item.atr_pct}%`],['RVOL',`${item.rvol}배`],['RSI',item.rsi??'-'],
    ['시초 갭',pct(item.gap_pct)],['최근 5일',pct(item.recent5_pct)],
    ['5일선',`${fmt(item.ma5_krw)}원`],['20일선',`${fmt(item.ma20_krw)}원`],
    ['5일 거래량가중 기준가(근사)',`${fmt(item.vwap_proxy_krw)}원`],
    ['피봇 S1 / PP / R1',`${fmt(item.pivot_s1_krw)} / ${fmt(item.pivot_pp_krw)} / ${fmt(item.pivot_r1_krw)}`],
    ['MACD / 볼린저',`${item.macd_bull?'상승':'약세'} / ${item.bb_break?'상단돌파':'일반'}`],
    ['2·3차 목표',`${fmt(item.target2_krw)} / ${fmt(item.target3_krw)}원`],
    ['2·3차 손절',`${fmt(item.stop2_krw)} / ${fmt(item.stop3_krw)}원`],
    ['시간외', '미지원 — 임의값 생성 안 함']
  ];
  if(item.held_price_krw) rows.unshift(['보유 평단 / 손익',`${fmt(item.held_price_krw)}원 / ${pct(item.held_pnl_pct)}`]);
  dg.innerHTML=rows.map(([a,b])=>`<div>${escapeText(a)}<b>${escapeText(b)}</b></div>`).join('');
  tpl.querySelector('.reason').innerHTML=`<b>전략 판단:</b> ${escapeText(item.strategy_reason)} · 패턴점수 ${escapeText(item.strategy_confidence)}%<br><span class="reason-note">${escapeText(item.data_note)}</span>`;
  tpl.querySelector('.links').innerHTML=`<a target="_blank" rel="noopener" href="${chartLink(item)}">외부 차트 열기 ↗</a>`;
  return tpl;
}

function sortResults(items){
  const mode=$('sortBy').value;
  const copy=[...items];
  if(mode==='momentum') copy.sort((a,b)=>Number(b.momentum||0)-Number(a.momentum||0));
  else if(mode==='rrr') copy.sort((a,b)=>Number(b.rrr||0)-Number(a.rrr||0));
  else if(mode==='name') copy.sort((a,b)=>String(a.name||'').localeCompare(String(b.name||''),'ko'));
  return copy;
}

function renderCards(){
  const frag=document.createDocumentFragment();
  sortResults(allResults).forEach(x=>frag.appendChild(renderCard(x)));
  $('cards').innerHTML=''; $('cards').appendChild(frag); applyFilter();
}

function applyFilter(){
  const q=$('search').value.trim().toLowerCase();
  document.querySelectorAll('.stock-card').forEach(card=>{
    const qok=!q || card.dataset.search.includes(q);
    let fok=true;
    if(activeFilter==='KR') fok=card.dataset.category==='KR';
    else if(activeFilter==='US') fok=card.dataset.category==='US';
    else if(activeFilter==='ETF') fok=card.dataset.category==='ETF';
    else if(activeFilter==='HELD') fok=card.dataset.held==='1';
    else if(activeFilter==='SA') fok=['S','A'].includes(card.dataset.grade);
    else if(activeFilter==='BUY') fok=card.dataset.buy==='1';
    card.style.display=(qok&&fok)?'block':'none';
  });
}
$('search').addEventListener('input',applyFilter);
$('sortBy').addEventListener('change',renderCards);
document.querySelectorAll('.chip').forEach(btn=>btn.addEventListener('click',()=>{
  document.querySelectorAll('.chip').forEach(x=>x.classList.remove('active')); btn.classList.add('active'); activeFilter=btn.dataset.filter; applyFilter();
}));

function renderTopPick(){
  const ranked=allResults.filter(x=>x.ok).sort((a,b)=>{
    const av=beginnerVerdict(a), bv=beginnerVerdict(b);
    const score=v=>v.level==='green'?3:(v.level==='yellow'?2:1);
    return score(bv)-score(av) || Number(b.momentum||0)-Number(a.momentum||0) || Number(b.rrr||0)-Number(a.rrr||0);
  });
  const best=ranked[0];
  if(!best){ $('topPick').hidden=true; return; }
  const v=beginnerVerdict(best);
  $('topPick').hidden=false;
  $('topPick').classList.remove('verdict-card-green','verdict-card-yellow','verdict-card-red');
  $('topPick').classList.add(`verdict-card-${v.level}`);
  $('topPickAction').textContent=v.text;
  $('topPickName').textContent=`${best.name} (${best.code})`;
  $('topPickReason').textContent=`${best.tech} · ${best.grade}급 · 모멘텀 ${best.momentum}점 · 손익비 1:${best.rrr}`;
  $('topPickReasons').innerHTML=v.why.map(x=>`<div>✓ ${escapeText(x)}</div>`).join('');
  $('topPickEntry').textContent=`${fmt(best.entry_krw)}원`;
  $('topPickTarget').textContent=`${fmt(best.target1_krw)}원`;
  $('topPickStop').textContent=`${fmt(best.stop1_krw)}원`;
  $('topPickQty').textContent=best.qty>0?`${best.qty}주`:'0주';
}

function renderData(data,{restored=false}={}){
  allResults=data.results||[];
  $('marketState').textContent=data.market?.state||'데이터 없음';
  $('marketGuide').textContent=data.market?.guide||'';
  $('kospi').textContent=pct(data.market?.kospi_chg);
  $('kosdaq').textContent=pct(data.market?.kosdaq_chg);
  $('fx').textContent=fmt(data.summary?.usdkrw);
  $('recommended').textContent=data.summary?.recommended ?? '-';
  $('sCount').textContent=data.summary?.s_count ?? '-';
  $('aCount').textContent=data.summary?.a_count ?? '-';
  $('totalCount').textContent=data.summary?.valid ?? '-';
  $('updatedAt').textContent=`마지막 분석: ${dateLabel(data.generated_at)}${restored?' · 이 기기에 저장된 결과':''}`;
  renderCards(); renderTopPick();
}

function saveSnapshot(data){
  try{ localStorage.setItem(SNAPSHOT_KEY,JSON.stringify(data)); }catch(e){}
}
function restoreSnapshot(){
  try{
    const data=JSON.parse(localStorage.getItem(SNAPSHOT_KEY)||'null');
    if(data && data.results){ renderData(data,{restored:true}); $('status').textContent='저장된 마지막 분석 결과를 먼저 표시했습니다. 최신 값이 필요하면 ‘최신 분석’을 누르세요.'; return true; }
  }catch(e){}
  return false;
}

async function analyze(){
  saveSettings();
  const btn=$('analyzeBtn'); btn.disabled=true; btn.textContent='분석 중…';
  $('status').textContent='시장·종목 데이터를 불러와 분석하고 있습니다.';
  try{
    const headers={'Content-Type':'application/json'};
    const pin=$('appPin').value.trim(); if(pin) headers['X-App-Pin']=pin;
    const res=await fetch('/api/analyze',{method:'POST',headers,body:JSON.stringify(settingsPayload())});
    const data=await res.json(); if(!res.ok || !data.ok) throw new Error(data.error||'분석 실패');
    renderData(data); saveSnapshot(data);
    $('status').textContent=`분석 완료 · 정상 ${data.summary.valid}/${data.summary.total}개 · 처리 ${data.summary.elapsed_sec}초${data.cache_hit?' · 서버 캐시 사용':''}`;
  }catch(e){
    const hasSaved=localStorage.getItem(SNAPSHOT_KEY);
    $('status').textContent=`최신 분석 오류: ${e.message}${hasSaved?' · 아래에는 저장된 마지막 결과를 유지합니다.':''}`;
  }finally{ btn.disabled=false; btn.textContent='최신 분석'; }
}
$('analyzeBtn').addEventListener('click',analyze);

$('clearSavedBtn').addEventListener('click',()=>{
  localStorage.removeItem(SNAPSHOT_KEY);
  allResults=[]; $('cards').innerHTML=''; $('topPick').hidden=true;
  $('status').textContent='저장된 분석 결과를 삭제했습니다.';
  $('marketState').textContent='분석 전'; $('marketGuide').textContent='‘최신 분석’을 눌러 새 결과를 만드세요.'; $('updatedAt').textContent='마지막 분석: 없음';
});

async function checkConnection(){
  try{
    const res=await fetch('/health',{cache:'no-store'}); const data=await res.json();
    if(!res.ok || !data.ok) throw new Error();
    $('onlineState').textContent=`온라인 · ${data.version}`; $('onlineDot').classList.add('ok');
    $('connectionText').textContent=data.pin_required?'온라인 접속 · PIN 보호 사용 중':'온라인 접속 가능 · PWA 지원';
  }catch(e){
    $('onlineState').textContent='서버 연결 안 됨'; $('onlineDot').classList.remove('ok'); $('onlineDot').classList.add('bad');
  }
}

$('shareBtn').addEventListener('click',async()=>{
  const shareData={title:'V70.2 주린이 매수판',text:'V70.2 주린이용 매수·대기·금지 대시보드',url:location.href};
  try{ if(navigator.share) await navigator.share(shareData); else { await navigator.clipboard.writeText(location.href); $('status').textContent='현재 주소를 복사했습니다.'; } }catch(e){}
});

window.addEventListener('beforeinstallprompt',e=>{ e.preventDefault(); installPrompt=e; $('installBtn').classList.remove('hidden'); });
$('installBtn').addEventListener('click',async()=>{ if(!installPrompt) return; installPrompt.prompt(); await installPrompt.userChoice; installPrompt=null; $('installBtn').classList.add('hidden'); });
window.addEventListener('appinstalled',()=>{ $('installBtn').classList.add('hidden'); $('status').textContent='홈 화면 앱 설치가 완료되었습니다.'; });

document.querySelectorAll('.bottom-nav button').forEach(btn=>btn.addEventListener('click',()=>{
  if(btn.dataset.action==='held'){
    activeFilter='HELD'; document.querySelectorAll('.chip').forEach(x=>x.classList.toggle('active',x.dataset.filter==='HELD')); applyFilter(); $('resultsSection').scrollIntoView({behavior:'smooth'}); return;
  }
  const el=$(btn.dataset.scroll); if(el) el.scrollIntoView({behavior:'smooth',block:'start'});
}));

loadSettings(); const restoredOnOpen=restoreSnapshot(); checkConnection();
if(!restoredOnOpen){ setTimeout(()=>analyze(),450); }
if('serviceWorker' in navigator){ navigator.serviceWorker.register('/static/sw.js').catch(()=>{}); }
