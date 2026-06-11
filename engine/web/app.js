const $=s=>document.querySelector(s);
let UNIVERSE=[];
let CURRENT_REPORT_ID=null;
let CURRENT_SUGGESTIONS=[];
let CURRENT_CYCLE=null;
let BLOCKED_SUGGESTIONS=[];
let DECIDED_SUGGESTIONS=[];
let CURRENT_CONFIG=null;
let currentTab='markets';
let marketsLoaded=false;
let reportShown=false;
let latestSignalLoaded=false;
let LAST_MARKET_ITEMS=[];
let LAST_EXECUTIONS=[];
let LIVE_SIGNALS=null;   // 最近一次"本周信号"对象，供执行记录刷新后重算任务勾选用
let CURRENT_CONSTRUCT=null;   // 最近一次模型组合构建结果，应用时回显其 input_fingerprint（§8.2 阻断项 #4）
let STRATEGY_FLOW={quality:null, construct:null, validated:false, cur:0};   // 长期战略线性流程·步骤条状态
let MARKET_REFRESHING=false;
const MARKET_CACHE_KEY='makemoney.market.snapshot.v1';
const MARKET_RANGE_KEY='makemoney.market.range.v1';
const MARKET_RANGES=[
  {key:'3m', label:'近3月', days:63},
  {key:'6m', label:'近6月', days:126},
  {key:'1y', label:'近1年', days:252},
  {key:'3y', label:'近3年', days:756},
  {key:'all', label:'全部', days:5000}
];
let MARKET_RANGE=currentMarketRange();
const ECHARTS=[];
function initChart(el){
  // L18：同一元素重绘前先 dispose 旧实例，并清掉已 dispose / DOM 已被 innerHTML 换掉的孤儿实例，防 ECHARTS[] 泄漏
  const old=window.echarts&&echarts.getInstanceByDom(el); if(old)old.dispose();
  for(let i=ECHARTS.length-1;i>=0;i--){
    const c=ECHARTS[i];
    if(c.isDisposed()||!document.contains(c.getDom())){ if(!c.isDisposed())c.dispose(); ECHARTS.splice(i,1); }
  }
  const c=echarts.init(el);ECHARTS.push(c);return c;
}
function disposeChart(el){
  try{
    if(!el||!window.echarts)return;
    const inst=echarts.getInstanceByDom(el);
    if(inst){
      const i=ECHARTS.indexOf(inst); if(i>=0)ECHARTS.splice(i,1);
      inst.dispose();
    }
  }catch(e){}
}
function resizeCharts(){ECHARTS.forEach(c=>{try{c.resize();}catch(e){}});}

/* ---------- 术语表 / tooltip ---------- */
const TERMS={
  趋势:'当前价格相对长期均线(MA200)的位置；跌破均线通常代表风险升高，不等于一定会继续下跌。',
  动量:'过去一段时间的涨跌幅，用来看相对强弱；动量强不代表便宜。',
  估值:'当前估值在历史区间里的位置（分位）；分位越高通常越贵，缺失时不能当作中性。',
  回撤:'从历史高点到当前的下跌幅度；它比单日涨跌更能衡量你要承受的痛感。',
  再平衡:'持仓偏离目标权重后做调整；0 持仓阶段优先看首次建仓，不直接做再平衡。',
  最长水下:'净值从某次高点跌下去、再回到该高点所经历的最长天数；衡量你可能要忍受多久的浮亏。',
  折溢价:'场内价格相对基金净值(IOPV)的偏离；溢价买入相当于多付钱，QDII/黄金/货币尤其要留意。',
  规模:'基金体量(总市值)；规模太小的 ETF 有清盘与流动性风险。',
  MA200:'200 日移动平均线，常用的长期趋势基准；价在其上通常视为趋势偏强。',
  缓建:'估值偏高时不一次买满、改分批小额靠近目标的执行节奏，降低买在高位的后悔。',
  TWR:'时间加权收益：剔除你"何时投、投多少"的影响，衡量持仓本身表现；不把追加本金当收益。',
  MWR:'资金加权收益(XIRR)：把你每次投入/取出的时间和金额都算进去，更贴近你实际的年化体验。',
  估值分位:'当前估值在历史区间里的位置；分位越高通常越贵。缺失时如实标"缺失"，绝不当中性。',
  压力回撤:'在一个简化的极端下跌情景里，组合可能的回撤幅度估算(非预测)，用来给风险预算定标。',
  偏离:'当前权重与目标权重之差(百分点)；偏离越大越该考虑再平衡。',
  战术:'在战略锚附近、按趋势/估值做的临时小幅高/低配；信号恢复后自动回归战略，不改长期目标。'
};
const GLOSS_ORDER=['趋势','动量','估值','估值分位','偏离','回撤','压力回撤','再平衡','缓建','TWR','MWR','战术','最长水下','折溢价','规模','MA200'];
function glossary(term,label){
  const def=TERMS[term];const text=label||term;
  if(!def)return escapeHtml(text);
  return `<span class="term" tabindex="0">${escapeHtml(text)}<span class="tip" role="tooltip">${escapeHtml(def)}</span></span>`;
}

/* ---------- 后端过期自检（Flask 长进程不热重载，更新代码后需重启） ---------- */
async function checkBackend(){
  try{
    const r=await fetch('/api/review/monthly');
    if(r.ok) return true;
    throw new Error('stale');
  }catch(e){
    const b=document.createElement('div');
    b.className='stalebanner';
    b.innerHTML='⚠️ 检测到后端缺少新接口——通常是你启动驾驶舱后又更新了代码。请到运行驾驶舱的终端按 <b>Ctrl+C</b>，重新执行 <b>python3 engine/app.py</b>，再刷新本页。（Flask 不会自动加载新代码。）';
    const wrap=document.querySelector('.wrap');
    if(wrap) wrap.prepend(b);
    return false;
  }
}

/* ---------- 标签控制器 ---------- */
function activateTab(name){
  currentTab=name;
  document.querySelectorAll('#tabbar .tab').forEach(t=>t.classList.toggle('active',t.dataset.tab===name));
  document.querySelectorAll('.tabpanel').forEach(p=>{p.hidden=(p.dataset.panel!==name);});
  if(name==='markets' && !marketsLoaded) loadMarketsTab();
  if(name==='review' && CURRENT_REPORT_ID && !reportShown) openReport(CURRENT_REPORT_ID);
  if(name==='review') loadStrategyFlow();
  resizeCharts();
}

/* ---------- 决策工作台信息架构 ---------- */
function buildDecisionWorkspace(){
  const wrap=document.querySelector('.wrap');
  const top=document.querySelector('.top');
  const chips=document.querySelector('.chips');
  const weekly=document.getElementById('weeklyCard');
  const portfolio=document.getElementById('portfolioHome');
  const execution=document.getElementById('executionCard');
  const tabbar=document.getElementById('tabbar');
  if(!wrap||!top||!chips||!weekly||!portfolio||!execution||!tabbar||document.getElementById('workspaceNav'))return;

  const nav=document.createElement('nav');
  nav.id='workspaceNav';
  nav.className='workspaceNav';
  nav.innerHTML=`<div class="workspaceBrand"><b>MM</b><span>MakeMoney</span></div>
    <button class="workspaceLink active" data-space="decision" onclick="showWorkspace('decision')"><span>01</span><b>决策与组合</b><small>行动、持仓与执行</small></button>
    <button class="workspaceLink" data-space="review" onclick="showWorkspace('review')"><span>02</span><b>长期战略</b><small>配置、产品与验证</small></button>
    <button class="workspaceLink" data-space="markets" onclick="showWorkspace('markets')"><span>03</span><b>研究工具</b><small>ETF、观察与回测</small></button>`;
  document.body.prepend(nav);

  const intro=document.createElement('section');
  intro.id='workspaceIntro';
  intro.className='workspaceIntro';
  intro.innerHTML=`<div><span class="eyebrow">DECISION DESK</span><h2 id="workspaceTitle">本周决策</h2><p id="workspaceDesc">只看现在需要处理的动作、阻塞原因和组合异常。</p></div>
    <div class="introActions" id="workspaceActions"></div>`;
  top.insertAdjacentElement('afterend',intro);

  const grid=document.createElement('section');
  grid.id='decisionGrid';
  grid.className='decisionGrid';
  weekly.parentNode.insertBefore(grid,weekly);
  grid.appendChild(portfolio);
  grid.appendChild(weekly);
  grid.appendChild(execution);

  top.classList.add('workspaceTop');
  chips.classList.add('workspaceStatus');
  tabbar.classList.add('toolTabs');
  openStrategyLens('allocation');
  showWorkspace('decision',false);
}

function showWorkspace(space,scroll=true){
  const grid=document.getElementById('decisionGrid');
  const tabbar=document.getElementById('tabbar');
  const status=document.querySelector('.workspaceStatus');
  const intro=document.getElementById('workspaceIntro');
  const title=document.getElementById('workspaceTitle');
  const desc=document.getElementById('workspaceDesc');
  const actions=document.getElementById('workspaceActions');
  const meta={
    decision:['决策与组合','处理本周行动，同时检查持仓偏离、现金与最近执行。'],
    review:['长期战略','审视目标权重、产品选择、真实业绩与历史决策。'],
    markets:['研究工具','按需查看 ETF 质量、观察池与策略回测。']
  };
  const actionHtml={
    decision:'<button class="ghost" onclick="runSignals()">刷新本周判断</button>',
    review:'',
    markets:'<button onclick="loadMarketsTab(true)">刷新 ETF 行情</button><button class="ghost" onclick="activateTab(\'backtest\')">打开回测</button>'
  };
  document.querySelectorAll('.workspaceLink').forEach(b=>b.classList.toggle('active',b.dataset.space===space));
  if(title)title.textContent=meta[space][0];
  if(desc)desc.textContent=meta[space][1];
  if(actions)actions.innerHTML=actionHtml[space];
  if(status)status.hidden=space!=='decision';
  if(space==='decision'){
    if(grid)grid.hidden=false;
    if(tabbar)tabbar.hidden=true;
    document.querySelectorAll('.tabpanel').forEach(p=>p.hidden=true);
    if(scroll){
      const target=document.getElementById('portfolioHome');
      if(target)target.scrollIntoView({behavior:'smooth',block:'start'});
    }
  }else{
    if(grid)grid.hidden=true;
    if(tabbar)tabbar.hidden=space==='review';
    activateTab(space==='review'?'review':'markets');
    if(scroll&&intro)intro.scrollIntoView({behavior:'smooth',block:'start'});
  }
  resizeCharts();
}

function openStrategyLens(lens){
  const meta={
    allocation:['配置决策','长期配置是否合理','比较当前目标与约束下的权威模型组合。'],
    products:['产品决策','当前 ETF 是否仍合适','检查产品质量、角色重合与是否存在替换必要性。'],
    validation:['证据验证','模型组合是否优于简单组合','比较长期收益、回撤、成本与稳健性，直接判断是否值得保留复杂度。'],
    records:['纪律复盘','历史决策是否守纪律','集中查看正式周报、真实业绩与月度执行复盘。']
  };
  const actions={
    allocation:'<button onclick="loadConstruct()">构建模型组合</button>',
    products:'<button onclick="loadIncumbents()">审视当前 ETF</button><button class="ghost" onclick="loadIncumbents(true,true)">补算重合与跟踪</button>',
    validation:'<button onclick="loadStrategicBacktest()">运行战略对比</button>',
    records:'<button onclick="loadReports()">刷新历史周报</button><button class="ghost" onclick="loadMonthlyReview()">刷新月度复盘</button>'
  };
  document.querySelectorAll('.strategyChoice').forEach(b=>b.classList.toggle('active',b.dataset.lens===lens));
  document.querySelectorAll('.strategyLens').forEach(p=>p.hidden=p.dataset.lensPanel!==lens);
  $('#strategyLensEyebrow').textContent=meta[lens][0];
  $('#strategyLensTitle').textContent=meta[lens][1];
  $('#strategyLensDesc').textContent=meta[lens][2];
  $('#strategyLensActions').innerHTML=actions[lens];
  resizeCharts();
}
function openRecordPanel(name,btn){
  document.querySelectorAll('.recordPanel').forEach(p=>p.hidden=p.dataset.recordPanel!==name);
  document.querySelectorAll('.recordTabs button').forEach(b=>b.classList.toggle('active',b===btn));
  resizeCharts();
}
// 复盘快捷入口（首页调仓记录卡 → 战略页纪律复盘对应面板），免去 工作区→专题→小标签 三层手动导航
function gotoRecords(panel){
  showWorkspace('review',false);
  openStrategyLens('records');
  const idx={reports:0,performance:1,monthly:2}[panel]||0;
  const btn=document.querySelectorAll('.recordTabs button')[idx];
  if(btn)btn.click();   // 复用原按钮逻辑（真实业绩会顺带触发 loadPerformance）
  const stage=document.querySelector('.strategyStage');
  if(stage)stage.scrollIntoView({behavior:'smooth',block:'start'});
}

/* ---------- 长期战略·线性流程步骤条 ---------- */
function _flowQualityFresh(){ return !!(STRATEGY_FLOW.quality && STRATEGY_FLOW.quality.fresh); }
function _flowStatusZh(s){ return {no_feasible_portfolio:'无可行组合', blocked_quality_data:'质量数据不足', violated:'越界'}[s]||s; }
async function loadStrategyFlow(){
  try{ const q=await fetch('/api/strategic/quality-status').then(r=>r.json()); STRATEGY_FLOW.quality=(q&&q.ok)?q:null; }
  catch(e){ STRATEGY_FLOW.quality=null; }
  renderStrategyFlow();
}
function renderStrategyFlow(){
  const q=STRATEGY_FLOW.quality||{}, c=STRATEGY_FLOW.construct;
  const fresh=_flowQualityFresh();
  const built=c&&c.validation_status, passed=c&&c.validation_status==='passed';
  const applied=c&&Array.isArray(c.comparison)&&c.comparison.length&&c.comparison.every(x=>Math.abs(x.delta||0)<0.005);
  const st={1:{cls:'done',txt:'✓ 可确认/编辑'}};
  st[2]=fresh?{cls:'done',txt:'✓ 新鲜'+(q.age_days!=null?`(${q.age_days}天)`:'')}
        :q.status==='missing'?{cls:'todo',txt:'待刷新'}
        :(q.status==='cached'&&q.data_ok===false)?{cls:'todo',txt:'数据取不到·交易时段重试'}
        :{cls:'todo',txt:'已过期'+(q.age_days!=null?`(${q.age_days}天)`:'')};
  st[3]=!fresh?{cls:'lock',txt:'🔒 先做第2步'}
        :passed?{cls:'done',txt:'✓ 已构建'}
        :built?{cls:'warn',txt:'⚠ '+_flowStatusZh(c.validation_status)}
        :{cls:'todo',txt:'待构建'};
  st[4]=STRATEGY_FLOW.validated?{cls:'done',txt:'✓ 已验证'}:{cls:'',txt:'可选'};
  st[5]=!passed?{cls:'lock',txt:'🔒 先构建'}:applied?{cls:'done',txt:'✓ 已应用'}:{cls:'todo',txt:'待应用'};
  for(let n=1;n<=5;n++){
    const li=document.querySelector(`.flowStep[data-step="${n}"]`); if(!li)continue;
    li.className='flowStep '+(st[n].cls||'')+(STRATEGY_FLOW.cur===n?' cur':'');
    const b=li.querySelector('[data-badge]'); if(b)b.textContent=st[n].txt;
  }
  let hint='';
  if(!fresh && q.status==='cached' && q.data_ok===false)
    hint='第 2 步取不到行情/准入数据（多为非交易时段、盘后或数据源限频）——不是你配置坏了。请在 A 股交易时段（工作日 9:30–15:00）重新「刷新 ETF 质量与准入」；数据取到前无法构建，这是有意为之：不拿缺失数据替真金做决策。';
  else if(!fresh) hint='下一步 → 第 2 步「刷新 ETF 质量与准入」：拉实时折溢价/规模/费率/申购，解锁第 3 步构建。';
  else if(!built) hint='下一步 → 第 3 步「构建模型组合」：在你的设置与约束下算出权威模型组合。';
  else if(built&&!passed) hint='第 3 步未通过（'+_flowStatusZh(c.validation_status)+'）：按提示调整设置、或先减贵的、加合规的，再重建。';
  else if(passed&&!applied) hint='下一步 → 第 5 步「应用为目标权重」（含指纹核对 + 大跳变二次确认）；可先用第 4 步验证复杂度。';
  else if(passed&&applied) hint='✓ 模型组合已应用为当前目标权重。接下来去「本周决策 / 调仓」真正下单建仓。';
  const h=$('#flowHint'); if(h)h.textContent=hint;
}
// 步骤点击只【导航】，不自动计算——真正算什么由各步右上角的按钮手动触发（所有者要求）。
function goStrategyStep(n){
  STRATEGY_FLOW.cur=n; renderStrategyFlow();
  if(n===1){ openSettings(); return; }
  if(n===2){ openStrategyLens('products'); return; }     // 计算 = 点「审视当前 ETF」
  if(n===3){
    if(!_flowQualityFresh()){ flash('请先完成第 2 步：点「审视当前 ETF」刷新质量与准入','err');
      STRATEGY_FLOW.cur=2; renderStrategyFlow(); openStrategyLens('products'); return; }
    openStrategyLens('allocation'); return;              // 计算 = 点「构建模型组合」
  }
  if(n===4){ openStrategyLens('validation'); return; }   // 计算 = 点「运行战略对比」
  if(n===5){
    if(!(STRATEGY_FLOW.construct&&STRATEGY_FLOW.construct.validation_status==='passed')){
      flash('请先完成第 3 步「构建模型组合」并通过验证','err'); STRATEGY_FLOW.cur=3; renderStrategyFlow(); openStrategyLens('allocation'); return; }
    openStrategyLens('allocation');
    const box=$('#constructBox'); if(box)box.scrollIntoView({behavior:'smooth',block:'nearest'});
    flash('在下方「模型组合」里点「应用模型组合」即可写入目标权重（含指纹核对与大跳变二次确认）。');
    return;
  }
}

// 一键：刷新质量准入(若需) → 构建模型组合 → 落到结果。给"我就想拿到算法长期战略"的最短路径。
async function generateStrategy(){
  openStrategyLens('allocation');
  const box=$('#constructBox'); if(!box)return;
  box.hidden=false;
  box.innerHTML='<div class="hint"><span class="spin"></span>正在生成长期战略：① 检查 ETF 质量与准入…</div>';
  try{
    let qs=await fetch('/api/strategic/quality-status').then(r=>r.json());
    if(!qs||!qs.fresh){
      box.innerHTML='<div class="hint"><span class="spin"></span>① 刷新 ETF 质量与准入（约 30 秒，请稍候）…</div>';
      const inc=await fetch('/api/strategic/incumbents').then(r=>r.json());
      if(!inc||!inc.ok)throw new Error((inc&&inc.error)||'刷新质量与准入失败');
    }
    box.innerHTML='<div class="hint"><span class="spin"></span>② 构建模型组合…</div>';
    await loadConstruct();          // 渲染进 #constructBox + 更新步骤条
    await loadStrategyFlow();
    box.scrollIntoView({behavior:'smooth',block:'start'});
  }catch(e){ box.innerHTML='<div class="msg err" style="display:block">生成失败：'+escapeHtml(String(e.message||e))+'</div>'; }
}

/* ---------- 悬浮帮助 / 数据详情 弹层 ---------- */
function toggleHelp(e){if(e)e.stopPropagation();const p=$('#helpPanel');p.hidden=!p.hidden;}
function toggleHealth(e){if(e)e.stopPropagation();const p=$('#healthPanel');p.hidden=!p.hidden;}
function toggleCollapse(id,btn){
  const el=document.getElementById(id); if(!el)return;
  el.hidden=!el.hidden;
  if(btn)btn.textContent=el.hidden?'展开':'收起';
}
function expandPanel(id,btnId){
  const el=document.getElementById(id); if(!el)return;
  el.hidden=false;
  const btn=btnId&&document.getElementById(btnId); if(btn)btn.textContent='收起';
}
document.addEventListener('keydown',e=>{if(e.key==='Escape'){$('#helpPanel').hidden=true;$('#healthPanel').hidden=true;closeRebalance();closeSettings();closeRebalanceSettings();}});
document.addEventListener('click',e=>{
  const hp=$('#helpPanel'),fab=$('#helpFab');
  if(hp&&!hp.hidden&&!hp.contains(e.target)&&e.target!==fab) hp.hidden=true;
  const dp=$('#healthPanel'),db=$('#healthBtn');
  if(dp&&!dp.hidden&&!dp.contains(e.target)&&e.target!==db) dp.hidden=true;
  if(e.target&&e.target.id==='rebalanceModal') closeRebalance();
  if(e.target&&e.target.id==='settingsModal') closeSettings();
  if(e.target&&e.target.id==='rebalSettingsModal') closeRebalanceSettings();
});

/* ---------- 配置 ---------- */
async function loadConfig(){
  const c=await (await fetch('/api/config')).json();
  CURRENT_CONFIG=c;
  UNIVERSE=c.universe;
  const ip=c.investor_profile||{};
  $('#targetReturn').value=toPct(ip.target_annual_return ?? 0.05, 1);
  $('#maxDrawdown').value=toPct(ip.max_acceptable_drawdown ?? 0.15, 0);
  $('#totalAssets').value=ip.total_assets || ((ip.stable_assets_outside||0)+(ip.planned_etf_capital||0));
  $('#unemploymentGap').value=Math.max(0,(ip.unemployment_monthly_expense ?? 6000)-(ip.unemployment_minimum_monthly_income ?? 0));
  $('#unemploymentYears').value=ip.unemployment_runway_years ?? 5;
  $('#postStressMonths').value=ip.post_stress_reserve_months ?? 12;
  renderPortfolioPreview();
  drawPortfolioAllocation();
  renderPortfolioPnL();
  renderStaleBanner();   // 持仓变了就重判"信号是否已过期"（成交后未重算→提示重新生成）
}

// 添加 / 提取 ETF 桶现金（只改可投现金余额，不是 ETF 成交、不影响 TWR/MWR）。
async function adjustCash(action){
  const cur=Number((CURRENT_CONFIG||{}).cash||0);
  const label=action==='add'?'添加现金':'提取现金';
  const raw=prompt(`${label}（当前现金 ¥${cur.toLocaleString()}）\n请输入金额（元）：`);
  if(raw==null)return;
  const amount=Number(String(raw).replace(/[,，\s¥]/g,''));
  if(!(amount>0)){ flash('请输入大于 0 的金额','err'); return; }
  if(action==='withdraw' && amount>cur+1e-9){ flash(`提取金额超过当前现金 ¥${cur.toLocaleString()}`,'err'); return; }
  const nextCash=action==='add'?cur+amount:cur-amount;
  const okCash=await confirmDialog({
    title:`确认${label}`,
    body:`<div class="act">现金：¥${cur.toLocaleString()} → <b>¥${nextCash.toLocaleString()}</b>（${label} ¥${amount.toLocaleString()}）</div>
      <div class="hint">只调整可投现金，不替你下单、不影响已投 ETF 的业绩计算。</div>`,
    confirmText:`确认${label}`});
  if(!okCash)return;
  try{
    const d=await fetch('/api/portfolio/cash',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action,amount})}).then(r=>r.json());
    if(!d.ok)throw new Error(d.error||'失败');
    await loadConfig();
    const chip=$('#chipCash'); if(chip)chip.textContent='¥'+Number(d.cash).toLocaleString();
    flash(`✓ ${label} ¥${amount.toLocaleString()}；现金 ¥${cur.toLocaleString()} → ¥${Number(d.cash).toLocaleString()}`);
  }catch(e){ flash(label+'失败：'+escapeHtml(String(e.message||e)),'err'); }
}
function collectInvestorProfile(){
  const cur=(CURRENT_CONFIG&&CURRENT_CONFIG.investor_profile)||{};
  return {
    target_annual_return:Number($('#targetReturn').value||0)/100,
    horizon_years:cur.horizon_years ?? 5,
    max_acceptable_drawdown:Number($('#maxDrawdown').value||0)/100,
    experience_level:cur.experience_level || 'beginner',
    emergency_cash_kept_outside:cur.emergency_cash_kept_outside ?? 0,
    monthly_contribution:cur.monthly_contribution ?? 0,
    total_assets:Number($('#totalAssets').value||0),
    stable_assets_outside:cur.stable_assets_outside ?? 0,
    stable_assets_yield:cur.stable_assets_yield ?? 0.025,
    planned_etf_capital:cur.planned_etf_capital ?? 0,
    unemployment_monthly_expense:Number($('#unemploymentGap').value||0),
    unemployment_minimum_monthly_income:0,
    unemployment_runway_years:Number($('#unemploymentYears').value||0),
    post_stress_reserve_months:Number($('#postStressMonths').value||0)
  };
}
async function saveConfig(){
  const m=$('#cfgmsg'); m.className='msg'; $('#save').disabled=true;
  const body={cash:Number(CURRENT_CONFIG.cash||0), risk_profile:CURRENT_CONFIG.risk_profile, holdings:CURRENT_CONFIG.holdings, investor_profile:collectInvestorProfile()};
  const r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const d=await r.json(); $('#save').disabled=false;
    if(d.ok){
      const a=d.strategic_update||{};
      m.className='msg ok';
      m.textContent='✓ '+(a.reason||'设置已保存');
      await loadConfig();
      STRATEGY_FLOW.construct=null; STRATEGY_FLOW.validated=false; loadStrategyFlow();   // 设置变了→模型组合已过期，步骤条回到"待构建"
      flash('✓ 设置已保存；目标权重保持不变。如调整了战略输入，请到「战略与复盘 → 长期配置是否合理」重新构建模型组合并确认应用。');
    }
  else{m.className='msg err';m.textContent='保存失败：\n- '+(d.errors||['未知错误']).join('\n- ');}
}

/* ---------- 生成信号 ---------- */
function fmtElapsed(t0){
  const s=Math.round((Date.now()-t0)/1000);
  return s>=60?`${Math.floor(s/60)}分${String(s%60).padStart(2,'0')}秒`:`${s}秒`;
}
// 长任务进度轮询（通用）：每 1.2s 读 /api/signals/status，把"第X/N步·阶段名·已用时"写进指定元素。
// opts.tasks=接受的任务名数组（过滤上一个任务的残留进度，防串显）；opts.hint=预计耗时文案。返回 stop 函数。
// 构建会隐式触发信号刷新：tasks 含 'signals' 但不在首位时，信号阶段加"自动刷新本周信号"前缀讲明白在等什么。
function pollTaskProgress(elId,t0,opts){
  const o=opts||{}; const accept=o.tasks||['signals'];
  const timer=setInterval(async()=>{
    try{
      const r=await fetch('/api/signals/status'); const d=await r.json();
      const p=d&&d.progress; const el=document.getElementById(elId);
      if(!p||!el||p.done||accept.indexOf(p.task)<0)return;
      const prefix=(p.task==='signals'&&accept.indexOf('signals')>0)?'自动刷新本周信号 · ':'';
      const step=(p.step&&p.total)?`第${p.step}/${p.total}步 · `:'';
      const det=p.detail?`：${escapeHtml(p.detail)}`:'';
      el.innerHTML=`<span class="spin"></span>${prefix}${step}${escapeHtml(p.stage||'')}${det} · 已用 ${fmtElapsed(t0)}${o.hint?`（${o.hint}）`:''}`;
    }catch(e){}
  },1200);
  return ()=>clearInterval(timer);
}
function pollSignalsProgress(elId,t0){return pollTaskProgress(elId,t0,{tasks:['signals'],hint:'通常 1–2 分钟，数据源慢时最长 4 分钟'});}
async function runSignals(){
  const btn=$('#genbtn'); btn.disabled=true; btn.innerHTML='<span class="spin"></span>生成中…';
  $('#weeklyReportLive').innerHTML='<div class="hint" id="sigProgress"><span class="spin"></span>生成本周信号…（通常 1–2 分钟，数据源慢时最长 4 分钟）</div>';
  const stopPoll=pollSignalsProgress('sigProgress',Date.now());
  try{
    const r=await fetch('/api/signals',{method:'POST'}); const d=await r.json();
    if(!d.ok){$('#weeklyReportLive').innerHTML=`<div class="msg err" style="display:block">${d.error||'失败'}</div>`;return;}
    CURRENT_REPORT_ID=d.report&&d.report.id; reportShown=false;
    latestSignalLoaded=true;
    if(d.signals && d.report){
      d.signals._report_created=d.report.created_at || d.report.id;
      d.signals._report_id=d.report.id;
      d.signals._flags=(d.report.flags&&d.report.flags.flags)||[];
      d.signals._flagsMeta=d.report.flags||{};   // C：校验/新鲜度元数据，供"过旧/已忽略"提示
    }
    renderSignals(d.signals);
    await loadReports();
    if(currentTab==='review' && CURRENT_REPORT_ID) openReport(CURRENT_REPORT_ID);
    await loadExecutions();
    if(marketsLoaded) await loadMarketsTab(true);
    await loadDataHealth();
    await loadMonthlyReview();
    await loadWatchlistLearning();
  }finally{stopPoll(); btn.disabled=false; btn.textContent='重新生成';}
}
function renderSignals(s){
  LIVE_SIGNALS=s;
  renderOverview(s);
  renderWeeklyReport(s, {mode:'live', container:$('#weeklyReportLive'), flags:(s.flags&&s.flags.flags)||s._flags||[], flagsMeta:s._flagsMeta||(s.flags||{})});
}

/* ========== 统一周报渲染：常驻区(live=本周) 与 复盘详情(history=历史) 共用 ========== */
function renderWeeklyReport(s, opts){
  opts=opts||{}; const mode=opts.mode||'live'; const container=opts.container;
  if(!container||!s)return;
  const flags=opts.flags||[]; const flagsMeta=opts.flagsMeta||{}; const chartId='reportMomentumChart-'+mode;
  const rows=wkSignalsRows(s);
  const evidenceOpen=mode==='history'?' open':'';
  const html=`
    <div class="wk-must">
      ${wkHeadline(s)}
      <div class="wk-taskzone" id="wktaskzone-${mode}">${wkTasks(s,mode)}</div>
      ${wkAlerts(s,mode)}
    </div>
    <details class="wk-evidence"${evidenceOpen}>
      <summary><b>查看完整判断依据</b><span>信号、风险预算、交易纪律与战术诊断</span></summary>
      <div class="wk-why">
        <div class="wk-sec">持仓池信号</div>
        ${wkSignalsTable(rows, chartId)}
        ${wkRiskBudget(s)}
        ${wkFlags(flags, flagsMeta)}
        ${wkDiscipline(s)}
        ${wkBlocked(s)}
        ${wkFirstFunding(s)}
      </div>
      <div class="wk-bg">
        ${wkTacticalShadow(s)}
        ${wkWatchlist(s)}
        ${wkDataNote(s,mode)}
      </div>
    </details>`;
  // 先 dispose 旧的同 id 动量图，避免 ECHARTS[] 泄漏 / 对已销毁实例 resize
  try{ if(window.echarts){const el0=document.getElementById(chartId); if(el0){const inst=echarts.getInstanceByDom(el0); if(inst){const i=ECHARTS.indexOf(inst); if(i>=0)ECHARTS.splice(i,1); inst.dispose();}} } }catch(e){}
  container.innerHTML=html;
  drawReportMomentum(rows, chartId);
  const evidence=container.querySelector('.wk-evidence');
  if(evidence)evidence.addEventListener('toggle',()=>{if(evidence.open)setTimeout(resizeCharts,0);});
}
function wkSignalsRows(s){
  const signals=s.signals||{};
  const actByCode={};
  (s.actionable_rebalance||[]).forEach(a=>{actByCode[a.code]={suggest:a.suggest,reason:a.action_reason,actionable:a.actionable};});
  return Object.entries(signals).map(([code,x])=>{
    const mk=Object.keys(x).find(k=>k.startsWith('momentum_'));
    const a=actByCode[code]||{};
    return {code,name:x.name||code,trend:x.trend,momentum:x[mk],valuation:x.valuation,valuation_na:x.valuation_na,valuation_missing:x.valuation_missing,valuation_accumulating:x.valuation_accumulating,error:x.error,
      suggest:a.suggest,action_reason:a.reason};
  });
}
function suggestCn(sg){return sg==='add'?'<span class="rise">加仓</span>':sg==='trim'?'<span class="fall">减仓</span>':sg==='hold'?'<span class="mut">维持</span>':'<span class="mut">-</span>';}
/* ---- 必看（Tier1） ---- */
function wkHeadline(s){
  const q=s.data_quality||'-';
  const dataOk=q==='完整'||q==='缓存可用';
  const n=((s.first_funding_plan||{}).orders||[]).filter(x=>x.actionable).length
        +(s.actionable_rebalance||[]).filter(x=>x.actionable).length;
  const parts=[];
  if(q==='完整')parts.push('数据完整');
  else if(q==='缓存可用')parts.push('本周用了缓存行情、仅供参考');
  else parts.push(`数据${q}，本周不出操作建议`);
  if(!dataOk){parts.push('暂不操作');}
  else if(!s.rebalance_allowed){parts.push('缺行情/过旧，本次不给再平衡');}
  else parts.push(n>0?`本周有 <b>${n}</b> 项可手动确认的操作（见下）`:'本周无需买卖');
  const rb=s.risk_budget||{};
  if(rb.expected_target_gap!=null)parts.push(rb.expected_target_gap>0.005?'目标年化偏高、预期有缺口':'目标基本可达');
  if((s.trend_alerts||[]).length)parts.push(`⚠️ 有 <b>${s.trend_alerts.length}</b> 只已跌破 MA200、注意回撤`);
  return `<div class="wk-headline">${parts.join('；')}。</div>`;
}
function wkTasks(s, mode){
  const dataOk=s.data_quality==='完整'||s.data_quality==='缓存可用';
  const first=((s.first_funding_plan||{}).orders||[]).filter(x=>x.actionable);
  const acts=(s.actionable_rebalance||[]).filter(x=>x.actionable);
  const tasks=[];
  if(dataOk){
    const eqNote=x=>x.exec_quality==='warn'&&x.exec_quality_note?` · ⚠ ${x.exec_quality_note}`:'';
    first.forEach(o=>{
      const px=o.last!=null?Number(o.last):null;
      tasks.push({id:`first:${o.code}:${o.estimated_shares||0}:${o.estimated_amount||0}`,code:o.code,side:'买入',title:`确认首次试仓 ${o.name}`,
        detail:`${o.code} · ${Number(o.estimated_shares||0).toLocaleString()} 份${px!=null?` · 单价 ¥${px.toFixed(3)}`:''} · 约 ${fmtMoney(o.estimated_amount)}${eqNote(o)}`});
    });
    acts.forEach(a=>{
      const sg=(s.signals||{})[a.code]||{};
      const px=sg.last!=null?Number(sg.last):null;
      const sh=px?Math.floor((a.approx_amount||0)/px/100)*100:null;
      const shTxt=sh==null?'':(sh>0?` · 约 ${sh.toLocaleString()} 份`:' · 不足一手');
      // action_reason 已含偏离/趋势动量估值/执行质量，用它当理由；缺失时回退到简版偏离+eqNote
      const reasonTxt=a.action_reason?` · ${a.action_reason}`:` · 偏离 ${a.deviation_pp>0?'+':''}${a.deviation_pp}pp${eqNote(a)}`;
      const softTxt=a.action_mode==='缓建'?` · 缓建小额约 ${fmtMoney(a.soften_amount)}`:'';
      tasks.push({id:`rebalance:${a.code}:${a.suggest}:${a.approx_amount||0}`,code:a.code,side:a.suggest==='trim'?'卖出':'买入',title:`${a.suggest==='trim'?'确认减仓':'确认加仓'} ${a.name}`,
        detail:`${a.code}${shTxt}${px!=null?` · 单价 ¥${px.toFixed(3)}`:''} · 约 ${fmtMoney(a.approx_amount)}${softTxt}${reasonTxt}`});
    });
  }
  const label=mode==='history'?'这份周报当时的建议':'本周该做什么';
  // WS6：一行任务汇总（买/卖/缓建计数）
  const nBuy=tasks.filter(t=>t.side==='买入').length, nSell=tasks.filter(t=>t.side==='卖出').length;
  const nSoft=tasks.filter(t=>/缓建/.test(t.detail)).length;
  const summary=tasks.length?`<div class="hint">本周共 ${tasks.length} 项：买入 ${nBuy}${nSell?` · 卖出 ${nSell}`:''}${nSoft?` · 含${glossary('缓建')} ${nSoft}`:''}（点每项看理由）。</div>`:'';
  if(!tasks.length){
    // WS6：把"为什么不动"讲清楚——数据/拦截原因
    const blocked=(s.actionable_rebalance||[]).filter(r=>r.triggered&&r.actionable===false);
    const dqBad=s.data_quality!=='完整'&&s.data_quality!=='缓存可用';
    const why=(dqBad?`数据${s.data_quality||'不足'}、本周不出动作；`:'')+(blocked.length?`有 ${blocked.length} 项原始信号被门槛拦截（见下方"被门槛拦截"）；`:'');
    return `<div class="wk-tasklabel">${label}</div><div class="decisionline"><b>本周无需操作</b><span>${why}没有触发可执行的买卖，保持纪律、按计划即可。</span></div>`;
  }
  // 完成判定：①已登记对应成交（随 git 同步、换机器也在）→ 自动打勾；②本机手动勾选（仅本地）
  const enrich=tasks.map(t=>{const exDate=executionMatchDate(s,t);return Object.assign({},t,{exDate,done:!!exDate||(mode!=='history'&&isDecisionTaskDone(s,t.id))});});
  const done=enrich.filter(t=>t.done), open=enrich.filter(t=>!t.done);
  const doneHtml=done.map(t=>{
    // 手动勾选的（非成交推导、live）保留可取消的勾选框，避免误触无法撤销；成交推导/历史则为静态行
    if(mode!=='history' && !t.exDate)
      return `<label class="decisiontask done"><input type="checkbox" checked onchange="toggleDecisionTask('${decisionTaskKey(s,t.id)}',this.checked)"><span><b>✓ ${escapeHtml(t.title)}</b><span>${escapeHtml(t.detail)} · 已手动勾选（取消勾选可恢复为待办）</span></span></label>`;
    return `<div class="decisionline done"><b>✓ ${escapeHtml(t.title)}</b><span class="mut">${escapeHtml(t.detail)} · ${t.exDate?`已于 ${t.exDate} 登记成交`:'已手动勾选'}</span></div>`;
  }).join('');
  if(mode==='history'){
    const openHtml=open.map(t=>`<div class="decisionline"><b>${escapeHtml(t.title)}</b><span>${escapeHtml(t.detail)}</span></div>`).join('');
    return `<div class="wk-tasklabel">${label}</div>${summary}${doneHtml}${openHtml}`;
  }
  if(!open.length)
    return `<div class="wk-tasklabel">${label}</div>${summary}${doneHtml}<div class="decisionline"><b>✓ 本周待办已全部完成</b><span>共 ${tasks.length} 项，已全部登记/勾选。</span></div>`;
  const items=open.map(t=>`<label class="decisiontask"><input type="checkbox" onchange="toggleDecisionTask('${decisionTaskKey(s,t.id)}',this.checked)"><span><b>${escapeHtml(t.title)}</b><span>${escapeHtml(t.detail)}</span></span></label>`).join('');
  return `<div class="wk-tasklabel">${label}（登记对应调仓后自动打勾；也可手动勾掉）</div>${summary}${doneHtml}<div id="wkTasks">${items}</div>`;
}
// 找出与该任务匹配的"已执行"成交记录日期（同周报 report_id + 同 code + 同买卖方向）；没有返回 null。
function executionMatchDate(s, task){
  const rid=(s&&s._report_id)||null;   // 用周报对象自带的 id，live/history 互不干扰
  let hit=null;
  (LAST_EXECUTIONS||[]).forEach(rec=>{
    if(rid && rec.report_id && String(rec.report_id)!==String(rid))return; // 仅匹配本周报；旧记录无 report_id 时放宽
    (rec.items||[]).forEach(it=>{
      if(isExecutedMarker(it) && String(it.code||'').trim()===String(task.code||'').trim() && executionSide(it)===task.side){
        const when=(rec.created_at||rec.id||'').slice(0,10);
        if(when && (!hit||when>hit)) hit=when;
      }
    });
  });
  return hit;
}
// 执行记录刷新后，重算常驻区任务勾选（不重画动量图，避免闪烁）
function refreshLiveTasks(){
  const z=document.getElementById('wktaskzone-live');
  if(z && LIVE_SIGNALS) z.innerHTML=wkTasks(LIVE_SIGNALS,'live');
}
function wkAlerts(s,mode){
  let h='';
  const ta=s.trend_alerts||[];
  if(ta.length){
    const tp=s.trend_protection||{};
    const items=ta.map(a=>{
      // live 模式给"带入调仓"：自动把 代码/卖出/估算份额 填进登记表单，省去手抄金额（历史周报只读不给）
      const fillBtn=(mode==='live'&&a.actionable)?` <button class="ghost chipbtn" onclick="trendDeriskFill('${escapeHtml(String(a.code))}')">带入调仓</button>`:'';
      const act=a.actionable
        ? `减 <b>${escapeHtml(a.name)}(${a.code})</b> 约 ¥${Number(a.derisk_amount||0).toLocaleString()} → ${escapeHtml(a.reserve_name||'债券')}${fillBtn}`
        : `${escapeHtml(a.name)}(${a.code})：${escapeHtml((a.blocked_reasons||[]).join('；')||'暂不可执行')}`;
      return `<div>${a.actionable?'✅ ':'• '}${act}</div>`;
    }).join('');
    const why=tp.delta_pp
      ? `<div class="hint">依据：历史上趋势过滤把最大${glossary('回撤')}从 ${Math.abs(tp.static_maxdd*100).toFixed(0)}% 降到 ${Math.abs(tp.trend_maxdd*100).toFixed(0)}%——<b>不动手约多扛 ${Number(tp.delta_pp).toFixed(0)}pp 回撤</b>（${tp.years}年样本内；线上不自动执行，需你确认下单）。</div>`
      : '';
    const howTo=mode==='live'
      ? '这是建议、不是指令——点「带入调仓」可把代码/方向/估算份额预填进登记表单；实际下单仍由你在券商完成，工具不自动下单。'
      : '这是建议、不是指令——确认要减就到「调仓」按上面金额操作；工具不自动下单。';
    h+=`<div class="wk-alarm"><b>⚠️ 危机保险·趋势减仓建议</b><br>权益跌破 MA200、趋势转弱，建议移到防御/债券：${items}${why}<div class="hint mut">${howTo}</div></div>`;
  }
  if(!s.rebalance_allowed){
    h+=`<div class="wk-alarm"><b>本次不给再平衡建议</b><br>${s.missing_prices&&s.missing_prices.length?'部分行情缺失':'数据过旧'}——按"数据缺失≠中性"，本周不出再平衡动作，请稍后重试。</div>`;
  }
  return h;
}
/* ---- 可看（Tier2） ---- */
function wkSignalsTable(rows, chartId){
  return `<div class="reportviz">
    <div class="chartbox"><div id="${chartId}" class="echart"><canvas width="520" height="220"></canvas></div></div>
    <div><table><thead><tr><th>ETF</th><th>${glossary('趋势')}</th><th>${glossary('动量')}</th><th>${glossary('估值')}</th><th>本周建议</th></tr></thead><tbody>
      ${rows.map(x=>`<tr><td><b>${escapeHtml(x.name)}</b> <span class="mut">${x.code}</span></td>
        <td class="${x.trend==='above'?'rise':(x.trend==='below'?'fall':'mut')}">${x.error?'缺失':(x.trend==='above'?'均线上':(x.trend==='below'?'跌破':'数据不足'))}</td>
        <td>${x.momentum==null?'-':(x.momentum*100).toFixed(1)+'%'}</td>
        <td>${valCell(x)}</td>
        <td>${suggestCn(x.suggest)}${x.action_reason?` <span class="why" title="${escapeHtml(x.action_reason)}">ⓘ</span>`:''}</td></tr>`).join('')}
    </tbody></table></div>
  </div>`;
}
// 积木式预期收益拆解：逐只「中性锚 + 估值回归 / YTM → 前瞻预期」，可展开。
function bbBlocks(rb){
  const blocks=rb.expected_return_blocks;
  if(!Array.isArray(blocks)||!blocks.length)return '';
  const y=rb.bond_ytm||{};
  const ytmNote=y.value!=null
    ?`债券按${escapeHtml(y.tenor||'')}国债YTM ${(y.value*100).toFixed(2)}%${y.as_of?`（${escapeHtml(y.as_of)}）`:''}${y.source==='cache'?'·缓存':(y.source==='assumption'?'·取数失败回退假设':'')}`
    :'债券YTM不可用·暂用假设';
  const u=rb.us_ytm||{};
  const usNote=u.value!=null?` ｜ QDII按美债${escapeHtml(u.tenor||'')} ${(u.value*100).toFixed(2)}%+风险溢价`:'';
  const caveat=blocks.some(b=>b.valuation_caveat)?`<div class="bbNote" style="color:var(--amber)">⚠ 美股估值(CAPE)未建模：当前偏高，QDII 这两块的前瞻数偏乐观；黄金无现金流难锚定，保留 judgment。</div>`:'';
  const rows=blocks.map(b=>{
    const an=b.anchor!=null?`${(b.anchor*100).toFixed(1)}%`:'—';
    const va=b.valuation_adj?`${b.valuation_adj>0?'+':''}${(b.valuation_adj*100).toFixed(2)}%`:'—';
    const vaCls=b.valuation_adj<0?'neg':(b.valuation_adj>0?'pos':'mut');
    return `<tr><td>${escapeHtml(b.name||b.code)}</td><td class="num">${(b.weight*100).toFixed(0)}%</td>`
      +`<td class="num">${an}</td><td class="num ${vaCls}">${va}</td>`
      +`<td class="num"><b>${(b.expected*100).toFixed(2)}%</b></td><td class="mut">${escapeHtml(b.basis||'')}</td></tr>`;
  }).join('');
  return `<details class="bbDetails"><summary>每块怎么拼的（预期收益拆解）</summary>`
    +`<div class="bbNote mut">${ytmNote}${usNote} ｜ 估值回归摊销 ${rb.expected_return_reversion_years||10} 年；回测只管风险，收益锚在今天的利率与估值（这套前瞻积木也直接驱动「模型组合」的权重选择）。</div>`
    +caveat
    +`<table class="bbTable"><thead><tr><th>持仓</th><th class="num">权重</th><th class="num">中性锚</th><th class="num">估值回归</th><th class="num">前瞻预期</th><th>出处/置信</th></tr></thead><tbody>${rows}</tbody></table></details>`;
}
function wkRiskBudget(s){
  const rb=s.risk_budget||{};
  if(rb.expected_etf_return==null)return '';
  const exp=rb.expected_etf_return, tgt=rb.target_annual_return||0;
  // 锚定口径优先（前瞻：债券YTM + A股估值回归）；老数据无该字段则回退冻结口径。
  const anc=rb.expected_return_anchored, frz=rb.expected_return_frozen;
  const head=anc!=null?anc:exp;
  const gap=anc!=null&&rb.expected_target_gap_anchored!=null?rb.expected_target_gap_anchored
            :(rb.expected_target_gap!=null?rb.expected_target_gap:(tgt-exp));
  const ws=rb.whole_portfolio_stress_drawdown, mdd=rb.max_acceptable_drawdown;
  const lead=anc!=null
    ?`按当前目标权重，ETF 桶<b>前瞻</b>预期年化约 <b>${(head*100).toFixed(1)}%</b>（锚定口径：债券按当前YTM、A股按估值回归）${frz!=null?`<span class="mut">；冻结假设口径 ${(frz*100).toFixed(1)}%</span>`:''}`
    :`按当前目标权重，ETF 桶现实预期年化约 <b>${(head*100).toFixed(1)}%</b>`;
  let html=`<div class="wk-sec">目标可行性</div><div class="act">${lead}（目标 ${(tgt*100).toFixed(1).replace(/\.0$/,'')}%${gap>0.005?`，缺口约 ${(gap*100).toFixed(1)}pp：靠低风险资产难补上，需更高权益或下调目标——可在长期战略里构建模型组合`:'，基本匹配'}）。${ws!=null?`<br><span class="mut">全组合压力${glossary('回撤')}约 ${(ws*100).toFixed(1)}%${mdd!=null?`（预算 ${(mdd*100).toFixed(0)}%）`:''}（单一简化情景；非承诺）。</span>`:''}</div>${bbBlocks(rb)}`;
  // B-3：相关性诊断（分散比/平均相关性/有效风险来源数）+ 市场 regime，诚实披露"线性压力未计相关性"
  const co=rb.correlation||{}, rg=rb.regime||{};
  if(co.available){
    const dr=co.diversification_ratio, ac=co.avg_corr, eb=co.effective_bets, pv=co.portfolio_vol_annual;
    html+=`<div class="wk-sec">相关性与市场状态</div><div class="act mut">`
      +`分散比 <b>${dr!=null?Number(dr).toFixed(2):'—'}</b>（=1 完全同涨同跌·越高越分散）`
      +`｜平均相关性 ${ac!=null?Number(ac).toFixed(2):'—'}`
      +`${eb!=null?`｜有效风险来源数 ${Number(eb).toFixed(1)}`:''}`
      +`${pv!=null?`｜组合年化波动 ${(pv*100).toFixed(1)}%`:''}`
      +`${rg.state?`｜市场 <b class="${rg.stressed?'down':''}">${escapeHtml(rg.state)}</b>（${((rg.below_ratio||0)*100).toFixed(0)}% 权益跌破 MA200）`:''}`
      +`<br><span class="hint">${escapeHtml(co.note||'')}</span></div>`;
  } else if(rg.state){
    html+=`<div class="wk-sec">市场状态</div><div class="act mut">市场 <b class="${rg.stressed?'down':''}">${escapeHtml(rg.state)}</b>（${((rg.below_ratio||0)*100).toFixed(0)}% 权益跌破 MA200）${co.reason?`<br><span class="hint">${escapeHtml(co.reason)}</span>`:''}</div>`;
  }
  // §0C #1 历史尾部压力：据真实峰谷标定的多情景，诚实暴露"若 20XX 重演"
  const sc=rb.historical_scenarios||[];
  if(rb.worst_scenario && sc.length){
    const breach=!!rb.scenario_breached;
    const rows=sc.map(x=>`<tr><td>${escapeHtml(x.name)}</td><td style="text-align:right;font-variant-numeric:tabular-nums">−${(x.etf_drawdown*100).toFixed(0)}%</td></tr>`).join('');
    html+=`<div class="wk-sec">历史尾部压力（据真实峰谷标定）</div>`
      +`<div class="act"><b style="${breach?'color:var(--red)':''}">${escapeHtml(rb.worst_scenario_note||'')}</b>`
      +`<details style="margin-top:8px"><summary class="mut" style="cursor:pointer">展开 5 个历史危机情景（ETF 桶口径）</summary>`
      +`<table style="margin-top:8px;width:auto"><thead><tr><th style="text-align:left">情景</th><th style="text-align:right">ETF 桶回撤</th></tr></thead><tbody>${rows}</tbody></table>`
      +`<div class="hint">口径：据 <code>idx_*.csv</code> 种子真实峰谷标定（2008 沪深300 约 −71%）；同情景内债/金的对冲已抵损。当前实投占比小则当前口径更小，故按计划满仓给决策值。</div></details></div>`;
  }
  return html;
}
function wkFlags(flags, meta){
  meta=meta||{};
  let banner='';
  if(meta.validation_status==='rejected') banner='<div class="hint down">⚠ AI 旗标未通过机械校验，已忽略本周旗标（按"本周无重大事件"处理，不参与拦买）。</div>';
  else if(meta.stale) banner=`<div class="hint down">⚠ AI 旗标过旧（生成于 ${escapeHtml(String(meta.generated_for||'?'))}，比本周信号早约 ${meta.age_days} 天）——仅供参考，本周不参与拦买。</div>`;
  else if(Number(meta.age_days)>=3) banner=`<div class="hint">ⓘ AI 旗标为 ${meta.age_days} 天前生成（${escapeHtml(String(meta.generated_for||'?'))}）——如本周有重大事件，建议在 Claude 里刷新舆情旗标。</div>`;
  return `<div class="wk-sec">风险旗标（AI 舆情）</div>${banner}<div class="act">${renderFlags(flags)}</div>`;
}
function rebalanceRuleText(s){
  const p=s.params||{}, ad=s.action_discipline||{};
  const abs=p.rebalance_abs_pp!=null?Math.round(p.rebalance_abs_pp):5;
  const rel=p.rebalance_rel!=null?Math.round(p.rebalance_rel*100):25;
  const freqZh={weekly:'每周',biweekly:'每两周',monthly:'每月',quarterly:'每季'}[ad.check_frequency||'weekly']||'每周';
  const gap=ad.rebalance_min_gap_days;
  const freqTxt=` ｜ 频率：${freqZh}${gap>0?`（最短间隔 ${gap} 天）`:''}`;
  return `偏离目标 ≥${abs} 个百分点 或 相对偏离 ≥${rel}% 才触发（5/25 法则）；单笔 ≥¥${Number(ad.min_trade_amount||0).toLocaleString()}、单周 ≤¥${Number(ad.max_weekly_trade_amount||0).toLocaleString()}；行情缺失或过旧则本周不动手。${freqTxt}`
    +`<br><span class="mut">买卖只由上面的 5/25 偏离触发；趋势 / 动量 / 估值分位为参考背景，<b>不单独触发动作</b>（趋势跌破 MA200 会另给"危机保险"减仓建议；估值偏贵只放慢加仓、不减仓）。</span>`;
}
function wkDiscipline(s){
  if(s.rebalance_allowed===false||!s.action_discipline)return '';
  const ad=s.action_discipline;
  const msg=ad.trade_allowed?'纪律检查通过':'纪律检查拦截：'+(ad.blocked_reasons||[]).join('；');
  return `<div class="wk-sec">再平衡规则</div><div class="act mut">${rebalanceRuleText(s)}</div>`
    +`<div class="wk-sec">交易纪律</div><div class="act ${ad.trade_allowed?'':'mut'}"><b>${msg}</b><br>单笔≥¥${Number(ad.min_trade_amount||0).toLocaleString()} ｜ 单周≤¥${Number(ad.max_weekly_trade_amount||0).toLocaleString()} ｜ 首笔${Math.round((ad.first_tranche_pct||0)*100)}%</div>`+renderPreflightChecks(ad.preflight_checks||[]);
}
function wkBlocked(s){
  if(s.rebalance_allowed===false)return '';
  const actions=s.actionable_rebalance||s.rebalance||[];
  const blocked=actions.filter(r=>r.triggered && r.actionable===false);
  if(blocked.length){
    let h='<div class="wk-sec">被门槛拦截的原始信号</div><div class="act mut">';
    blocked.forEach(r=>{const v=r.suggest==='trim'?'减仓':'加仓';const why=r.action_reason||((r.blocked_reasons||[]).join('；'));h+=`<div>${v} ${escapeHtml(r.name||r.code||'')} 约 ¥${Number(r.approx_amount||0).toLocaleString()}：${escapeHtml(why)}</div>`;});
    return h+'</div>';
  }
  const anyAction=actions.some(r=>r.actionable)||(s.first_funding_plan&&s.first_funding_plan.eligible&&((s.first_funding_plan.orders||[]).some(o=>o.actionable)));
  if(!anyAction)return `<div class="wk-sec">再平衡</div><div class="act mut">✓ 未超过再平衡阈值，无需操作。<br>规则：${rebalanceRuleText(s)}</div>`;
  return '';
}
function wkFirstFunding(s){
  if(!(s.first_funding_plan&&s.first_funding_plan.eligible))return '';
  const p=s.first_funding_plan;
  let h=`<div class="wk-sec">首次建仓预览</div><div class="act">计划投入 ¥${Number(p.planned_deploy_amount||0).toLocaleString()}，估算可成交 ¥${Number(p.estimated_deploy_amount||0).toLocaleString()}，剩余约 ¥${Number(p.estimated_unallocated||0).toLocaleString()}</div>`;
  h+='<table><thead><tr><th>ETF</th><th>估算份额</th><th>估算金额</th><th>状态</th><th>原因</th></tr></thead><tbody>';
  (p.orders||[]).forEach(o=>{h+=`<tr><td><b>${o.name}</b> <span class="mut">${o.code}</span></td><td>${Number(o.estimated_shares||0).toLocaleString()}</td><td>¥${Number(o.estimated_amount||0).toLocaleString()}</td><td class="${o.actionable?'up':'mut'}">${o.actionable?'可手动确认':'暂不执行'}</td><td class="mut">${(o.blocked_reasons||[]).join('；')||'通过金额和一手限制'}</td></tr>`;});
  h+='</tbody></table><div class="hint">按 100 份一手粗略估算；观察池不参与首笔建仓；实际以下单页面为准。</div>';
  if((p.schedule||[]).length){
    h+='<div class="wk-sec">4-8 周分批计划草案</div><div class="hint">只有第 1 周是本周预览；后续周次必须完成复盘后再重新生成信号。</div>';
    h+='<table><thead><tr><th>周次</th><th>计划投入</th><th>估算可成交</th><th>保留现金</th><th>状态</th></tr></thead><tbody>';
    (p.schedule||[]).forEach(w=>{h+=`<tr><td>第 ${w.week} 周</td><td>${fmtMoney(w.planned_amount)}</td><td>${fmtMoney(w.estimated_amount)}</td><td>${fmtMoney(w.estimated_unallocated)}</td><td class="${w.status==='ready'?'up':'mut'}">${w.status==='ready'?'本周可评估':'需先复盘'}</td></tr>`;});
    h+='</tbody></table>';
  }
  return h;
}
/* ---- 真实业绩 TWR/MWR（WS3） ---- */
async function loadPerformance(){
  const box=$('#performancePanel'); if(!box)return;
  box.innerHTML='<span class="hint">加载业绩中…</span>';
  try{
    const r=await fetch('/api/performance').then(x=>x.json());
    if(!r||!r.ok){box.innerHTML='<span class="hint">业绩接口出错。</span>';return;}
    renderPerformance(r.performance);
  }catch(e){box.innerHTML='<span class="hint">业绩加载失败：'+escapeHtml(String(e))+'</span>';}
}
function _perfChip(label,v){return `<div class="chip"><span>${label}</span><b>${v==null?'—':((v>=0?'+':'')+(v*100).toFixed(1)+'%')}</b></div>`;}
function renderPerformance(p){
  const box=$('#performancePanel'); if(!box||!p)return;
  if(!p.snapshots||p.snapshots<2){
    box.innerHTML=`<div class="hint">NAV 快照仅 ${p.snapshots||0} 份，至少 2 份才能算 TWR/MWR——每生成一份正式周报积累一份。</div>`;return;
  }
  const tw=p.twr||{},mw=p.mwr||{},bm=p.benchmark;
  const chips=[_perfChip('TWR(时间加权·年化)', tw.available?tw.annualized:null),
               _perfChip('MWR(资金加权·XIRR)', mw.available?mw.mwr:null),
               _perfChip('沪深300基准·年化', bm?bm.annualized:null)].join('');
  let edge='';
  if(tw.available&&bm&&bm.annualized!=null){
    const d=(tw.annualized||0)-(bm.annualized||0);
    edge=`<div class="hint">相对沪深300 ${d>=0?'跑赢':'跑输'} 约 ${Math.abs(d*100).toFixed(1)}pp（年化，参考·非完全可比）。</div>`;
  }
  const span=tw.available?`<div class="hint">区间 ${escapeHtml(tw.start)} → ${escapeHtml(tw.end)}（${tw.periods} 个子区间）｜快照 ${p.snapshots} 份</div>`:'';
  const fees=p.total_fees?`<div class="hint">累计费用 ¥${Number(p.total_fees).toLocaleString()}（单列、未计入收益）。</div>`:'';
  const reason=(!tw.available&&tw.reason)?`<div class="hint">${escapeHtml(tw.reason)}</div>`:'';
  const cav=(p.caveats||[]).map(c=>`<div class="mut">· ${escapeHtml(c)}</div>`).join('');
  box.innerHTML=`<div class="chips">${chips}</div>${span}${edge}${fees}${reason}<div class="act">${cav}</div>`;
}
/* ---- 背景（Tier3） ---- */
function tacticalStateCn(st){return {neutral:'中性',positive_watch:'正向观察',positive_active:'正向激活',negative_watch:'负向观察',negative_active:'负向激活',recovering:'恢复中'}[st]||st||'-';}
function wkTacticalShadow(s){
  const t=s.tactical; if(!t||t.error||!t.diagnostics||!Object.keys(t.diagnostics).length)return '';
  const rows=Object.entries(t.diagnostics).map(([code,d])=>{
    const name=((s.signals||{})[code]||{}).name||code;
    const tilt=d.tilt_pp;
    return `<tr><td><b>${escapeHtml(name)}</b> <span class="mut">${code}</span></td>
      <td>${tacticalStateCn(d.state)}</td>
      <td>${d.effective_score==null?'-':Number(d.effective_score).toFixed(2)}</td>
      <td>${d.strategic_weight==null?'-':(d.strategic_weight*100).toFixed(0)+'%'}</td>
      <td>${d.tactical_weight==null?'-':(d.tactical_weight*100).toFixed(0)+'%'}</td>
      <td class="${tilt>0?'rise':(tilt<0?'fall':'mut')}">${tilt==null?'-':((tilt>0?'+':'')+tilt+'pp')}</td></tr>`;
  }).join('');
  const cash=t.cash==null?'':` ｜ 目标现金 ${(t.cash*100).toFixed(0)}%`;
  const budget=t.active_weight_budget_used==null?'':` ｜ 主动偏离 ${(t.active_weight_budget_used*100).toFixed(1)}%`;
  const adv=t.mode==='advisory';
  const head=adv
    ? `<div class="wk-sec">战术配置 <span class="mut">（advisory·已接入调仓）</span></div><div class="hint">${escapeHtml(t.note||'战术动作已接入调仓。')}</div>`
    : `<div class="wk-sec">影子战术建议 <span class="mut">（${escapeHtml(t.mode||'shadow')}·只读，不构成本周可执行动作）</span></div><div class="hint">"战略锚附近的临时高/低配"参考，验收通过前不接入调仓；下单仍只看上面的"本周该做什么"。</div>`;
  let actHtml='';
  const live=(t.actions||[]).filter(a=>a.actionable);
  if(live.length){
    const lbl=adv?'本周战术动作（已接入调仓）':'若进入 advisory，将触发的战术动作（当前只读）';
    actHtml=`<div class="wk-sec">${lbl}</div><div class="act${adv?'':' mut'}">`+live.map(a=>{
      const nm=((s.signals||{})[a.code]||{}).name||a.code;
      return `<div>${a.side==='trim'?'减仓':'加仓'} ${escapeHtml(nm)} 约 ¥${Number(a.approx_amount||0).toLocaleString()}（${tacticalStateCn(a.state)}，目标偏离 ${a.deviation_pp>0?'+':''}${a.deviation_pp}pp）</div>`;
    }).join('')+'</div>';
  }
  return `${head}
    <table><thead><tr><th>ETF</th><th>战术状态</th><th>战术分</th><th>战略</th><th>战术目标</th><th>偏离</th></tr></thead><tbody>${rows}</tbody></table>
    <div class="hint">影子组合：reserve ${t.reserve!=null?(t.reserve*100).toFixed(0)+'%':'-'}${cash}${budget}${t.fallback?' ｜ ⚠ 构建回退到战略组合':''}</div>
    ${actHtml}`;
}
function wkWatchlist(s){
  if(!(s.watchlist_signals&&Object.keys(s.watchlist_signals).length))return '';
  let h=`<div class="wk-sec">观察池（只学习和监控，不触发交易） · 数据 ${s.watchlist_data_quality||'未知'} · 截至 ${s.watchlist_as_of_summary||'无'}</div>`;
  for(const code in s.watchlist_signals){
    const x=s.watchlist_signals[code];
    if(x.error){h+=`<div class="sig"><span>${x.name} <span class="mut">${code}</span></span><span class="mut">${x.error}</span></div>`;continue;}
    const mk=Object.keys(x).find(k=>k.startsWith('momentum_'));
    const mom=x[mk]; const trend=x.trend==='above'?'<span class="rise">↑在均线上</span>':(x.trend==='below'?'<span class="fall">↓跌破均线</span>':'<span class="mut">均线数据不足</span>');
    const role=x.role?`<span class="mut">${escapeHtml(x.role)}</span> · `:'';
    const note=x.note?`<div class="hint">${escapeHtml(x.note)}</div>`:'';
    h+=`<div class="sig"><span><b>${escapeHtml(x.name)}</b> <span class="mut">${code}</span>${note}</span><span>${role}${trend}${mom!=null?` ｜ 动量${(mom*100).toFixed(1)}%`:''}</span></div>`;
  }
  return h;
}
function wkDataNote(s, mode){
  const stamp=s._report_created?`${mode==='history'?'周报日期':'生成'} ${formatStamp(s._report_created)} ｜ `:'';
  const cache=s.used_cache?`含缓存行情（最旧约 ${s.stale_days_max||0} 天）；`:'';
  const miss=(s.missing_prices&&s.missing_prices.length)?`缺价：${escapeHtml(s.missing_prices.join('、'))}；`:'';
  return `<div class="wk-sec">数据口径</div><div class="decisionline"><b>口径</b><span>${stamp}策略按日 K，行情截至 ${escapeHtml(s.as_of_summary||'-')}；${cache}${miss}实时价只用于估值参考。</span></div>
    <div class="hint" style="margin-top:6px">这是量化骨架信号。完整周报（叠加 AI 舆情旗标）可在 Claude / Codex 里说“给我本周决策简报”。</div>`;
}
function decisionScope(s){return String(s._report_created||s.generated_for||s.as_of_summary||'latest');}
function decisionTaskKey(s,id){return `makemoney.todo.${decisionScope(s)}.${id}`;}
function isDecisionTaskDone(s,id){try{return localStorage.getItem(decisionTaskKey(s,id))==='done';}catch(e){return false;}}
function toggleDecisionTask(key,done){
  try{done?localStorage.setItem(key,'done'):localStorage.removeItem(key);}catch(e){}
  refreshLiveTasks();
  flash(done?'✓ 已标记完成':'已取消勾选，恢复为待办');
}
function renderPreflightChecks(checks){
  if(!checks.length)return '';
  const statusText={pass:'通过',warn:'关注',block:'拦截'};
  const statusClass={pass:'up',warn:'mut',block:'down'};
  return `<div class="act"><b>交易纪律清单</b>${checks.map(c=>`<div><span class="${statusClass[c.status]||'mut'}">[${statusText[c.status]||c.status}]</span> ${c.label}：${c.message}</div>`).join('')}</div>`;
}
// A（状态指纹）：本周信号据以计算的真实持仓(holdings_basis) 是否仍与当前 portfolio.yaml 一致；
//   成交/改持仓后未重算 → 不一致 → 视为过期（避免把旧现金/权重当现状读）。旧信号无 holdings_basis 时不误报。
function signalsStale(){
  const hb=LIVE_SIGNALS&&LIVE_SIGNALS.holdings_basis;
  if(!hb||!CURRENT_CONFIG) return false;
  if(Math.abs(Number(hb.cash||0)-Number(CURRENT_CONFIG.cash||0))>1) return true;
  const cur={}; (CURRENT_CONFIG.holdings||[]).forEach(h=>cur[String(h.code)]=Number(h.shares||0));
  const basis=hb.shares||{};
  for(const c of new Set([...Object.keys(basis),...Object.keys(cur)])){
    if(Math.abs(Number(basis[c]||0)-Number(cur[c]||0))>1e-6) return true;
  }
  return false;
}
function renderStaleBanner(){
  const el=$('#staleBanner'); if(!el) return;
  const holdStale=signalsStale();
  // M9：第二触发——周期配置指纹失效（如「应用模型组合」改了目标权重）。仅 3.6 秒 toast 不够，
  // 否则用户会一直看着一份按旧目标算的失效清单而不自知（执行端点有 409 兜底，不会执行错单）。
  const ver=CURRENT_CYCLE&&CURRENT_CYCLE.version_status;
  const verStale=!!(ver&&ver.status==='stale');
  el.hidden=!(holdStale||verStale);
  if(holdStale){
    el.innerHTML='⚠ <b>本周信号基于旧持仓</b>（已成交/改动但未重算）——上方"组合总值/现金"与下方本周建议可能不准。'
      +'<button class="ghost chipbtn" onclick="runSignals()">重新生成本周信号</button>';
  }else if(verStale){
    const labels={portfolio_version:'持仓/目标权重',strategy_version:'策略配置',investor_profile_version:'个人档案'};
    const what=(ver.changed||[]).map(k=>labels[k]||k).join('、')||'配置';
    el.innerHTML=`⚠ <b>${escapeHtml(what)}已在本周期生成后变化</b>（如刚应用了模型组合）——下方本周建议按旧配置计算、已失效，执行时会被拦下。`
      +'<button class="ghost chipbtn" onclick="runSignals()">重新生成本周信号</button>';
  }
  ['#chipValue','#chipCash'].forEach(id=>{const c=$(id); if(c) c.classList.toggle('stale',holdStale);});
}
function renderOverview(s){
  const actions=(s.actionable_rebalance||[]).filter(x=>x.actionable).length;
  const first=((s.first_funding_plan||{}).orders||[]).filter(x=>x.actionable).length;
  const q=s.data_quality||'-', cls=q==='完整'?'b-ok':(q==='缓存可用'?'b-warn':'b-bad');
  $('#chipData').innerHTML=`<span class="badge ${cls}">${q}</span>`;
  $('#chipAsof').textContent=s.as_of_summary||'-';
  $('#chipValue').textContent='¥'+Number(s.portfolio_value||0).toLocaleString();
  $('#chipCash').textContent='¥'+Number(s.cash||0).toLocaleString();
  $('#chipActions').textContent=(actions+first);
  renderStaleBanner();
}
/* ---------- 行情与质量：每只 ETF 统一卡（合并 market + quality） ---------- */
function marketTrackCodes(){
  // 行情与质量追踪整个可交易池(universe，含尚未持有的新品种)，并入已有持仓后去重。
  const u=(CURRENT_CONFIG&&CURRENT_CONFIG.universe)||[];
  const h=(CURRENT_CONFIG&&CURRENT_CONFIG.holdings)||[];
  const seen=new Set(), out=[];
  [...u,...h].forEach(x=>{const c=String(x.code); if(c&&c!=='undefined'&&!seen.has(c)){seen.add(c);out.push(c);}});
  return out.join(',');
}
function currentMarketRange(){
  let key='6m';
  try{key=localStorage.getItem(MARKET_RANGE_KEY)||key;}catch(e){}
  return MARKET_RANGES.find(x=>x.key===key)||MARKET_RANGES[1];
}
function marketCacheKey(range){
  const r=range||MARKET_RANGE;
  return `${MARKET_CACHE_KEY}.${r.key}`;
}
function initMarketRangeControl(){
  const sel=$('#marketRange');
  if(!sel)return;
  sel.innerHTML=MARKET_RANGES.map(x=>`<option value="${x.key}" ${x.key===MARKET_RANGE.key?'selected':''}>${x.label}</option>`).join('');
}
async function changeMarketRange(v){
  MARKET_RANGE=MARKET_RANGES.find(x=>x.key===v)||MARKET_RANGES[1];
  try{localStorage.setItem(MARKET_RANGE_KEY,MARKET_RANGE.key);}catch(e){}
  renderMarketCacheOnly();
}
function setMarketRefreshState(refreshing){
  MARKET_REFRESHING=refreshing;
  const btn=$('#marketRefreshBtn');
  if(btn){btn.disabled=refreshing;btn.textContent=refreshing?'刷新中...':'手动刷新';}
}
function renderMarketCacheOnly(){
  initMarketRangeControl();
  const box=$('#marketsbox');
  const cached=readMarketCache();
  if(cached) renderMarketSnapshot(cached,'cache');
  else if(box) box.innerHTML=`<div class="hint">暂无${MARKET_RANGE.label}缓存。可点“手动刷新”拉取。</div>`;
  resizeCharts();
}
async function manualRefreshMarkets(){
  if(MARKET_REFRESHING)return;
  await refreshMarketSnapshot(marketTrackCodes(), true, true);
}
async function loadMarketsTab(force){
  initMarketRangeControl();
  const box=$('#marketsbox');
  const codes=marketTrackCodes();   // 可能为空：两个接口都会回退到持仓默认集
  const cached=readMarketCache();
  if(cached){
    renderMarketSnapshot(cached,'cache');
  }else{
    box.innerHTML='<div class="hint"><span class="spin"></span>加载行情曲线中…</div>';
  }
  marketsLoaded=true;
  if(force || !cached) await refreshMarketSnapshot(codes,!cached,false);
  else refreshMarketSnapshot(codes,false,false);
}
function readMarketCache(){
  try{return JSON.parse(localStorage.getItem(marketCacheKey())||'null');}catch(e){return null;}
}
  function writeMarketCache(snapshot, range){
  try{localStorage.setItem(marketCacheKey(range),JSON.stringify(snapshot));}catch(e){}
}
function renderMarketSnapshot(snapshot,mode){
  const box=$('#marketsbox');
  enrichMarketSnapshot(snapshot);
  const items=(snapshot&&snapshot.items)||[];
  LAST_MARKET_ITEMS=items;
  if(!items.length){box.innerHTML='<div class="hint">暂无行情数据（可点“手动刷新”重试）。</div>';return;}
  const asofs=items.map(x=>x.as_of).filter(Boolean).sort();
  const asof=asofs.length?asofs[asofs.length-1]:'-';
  const stamp=snapshot.updated_at?`｜ ${MARKET_RANGE.label} ｜ ${mode==='cache'?'上次拉取':'本次拉取'} ${formatStamp(snapshot.updated_at)} ｜ 行情截至 ${asof}`:`｜ ${MARKET_RANGE.label} ｜ 行情截至 ${asof}`;
  $('#marketStamp').textContent=stamp;
  box.innerHTML=items.map((x,i)=>etfCardHtml(x,i)).join('');
  items.forEach((x,i)=>{ try{ drawChart(document.getElementById('mchart'+i),x); }catch(e){} });
  const qmap={}; (snapshot.quality||[]).forEach(q=>{qmap[q.code]=q;});
  items.forEach(x=>patchQuality(x.code, qmap[x.code]||null));
  renderPortfolioPreview();
  renderPortfolioPnL();
  resizeCharts();
}
function enrichMarketSnapshot(snapshot){
  if(!snapshot||!snapshot.items)return snapshot;
  const qmap={}; (snapshot.quality||[]).forEach(q=>{qmap[String(q.code)]=q;});
  snapshot.items.forEach(x=>{
    const q=qmap[String(x.code)];
    if(q&&q.last_price!=null){
      x.live_last=Number(q.last_price);
      x.live_source='spot';
    }
  });
  return snapshot;
}
async function refreshMarketSnapshot(codes,showErrors,manual){
  if(MARKET_REFRESHING)return;
  setMarketRefreshState(true);
  const range=MARKET_RANGE;
  const cq=codes?('?codes='+encodeURIComponent(codes)):'';
  let items=[];
  try{
    const m=await fetch('/api/market/kpis'+cq+(cq?'&':'?')+'days='+range.days).then(r=>r.json());
    items=(m&&m.items)||[];
  }catch(e){
    if(showErrors && MARKET_RANGE.key===range.key) $('#marketsbox').innerHTML='<div class="hint">行情加载失败，可点“手动刷新”重试。</div>';
    setMarketRefreshState(false);
    return;
  }
  if(!items.length){
    if(showErrors && MARKET_RANGE.key===range.key) $('#marketsbox').innerHTML='<div class="hint">暂无行情数据（可点“手动刷新”重试）。</div>';
    setMarketRefreshState(false);
    return;
  }
  const snapshot={updated_at:new Date().toISOString(),items,quality:[]};
  if(MARKET_RANGE.key===range.key) renderMarketSnapshot(snapshot,'live');
  try{
    const qd=await fetch('/api/etf/quality'+cq).then(r=>r.json());
    snapshot.quality=(qd&&qd.items)||[];
    enrichMarketSnapshot(snapshot);
    writeMarketCache(snapshot,range);
    if(MARKET_RANGE.key===range.key) renderMarketSnapshot(snapshot,'live');
  }catch(e){
    writeMarketCache(snapshot,range);
    if(MARKET_RANGE.key===range.key) items.forEach(x=>patchQuality(x.code, null, '质量检查加载失败，可点“手动刷新”重试。'));
  }
  setMarketRefreshState(false);
}
function patchQuality(code, q, failMsg){
  const sub=document.querySelector(`[data-q="${code}"]`);
  if(sub) sub.innerHTML = q ? qualityHtml(q) : (failMsg?`<span class="mut">${failMsg}</span>`:qualityHtml(null));
  const b=document.querySelector(`[data-badge="${code}"]`);
  if(b){
    if(q){const cls=q.status==='通过'?'pass':(q.status==='关注'?'warn':'bad');b.className='qstatus '+cls;b.textContent=q.status;}
    else{b.className='qstatus';b.textContent='质量未知';}
  }
}
function etfCardHtml(x,i){
  const px=x.live_last!=null?Number(x.live_last).toFixed(3):(x.last||'-');
  const pxLabel=x.live_last!=null?'实时快照价':'日K收盘价';
  return `<div class="etfcard">
    <h3><span><b>${escapeHtml(x.name)}</b> <span class="mut">${escapeHtml(x.code)}</span></span>
        <span class="qstatus" data-badge="${escapeHtml(x.code)}">检测中…</span></h3>
    <div id="mchart${i}" class="echart"><canvas width="480" height="170"></canvas></div>
    <div class="kpis">
      <div>20日<b>${fmtPct(x.ret_20d)}</b></div>
      <div>60日<b>${fmtPct(x.ret_60d)}</b></div>
      <div>120日<b>${fmtPct(x.ret_120d)}</b></div>
      <div>${glossary('回撤','1年最大回撤')}<b>${fmtPct(x.max_drawdown_1y)}</b></div>
      <div>${glossary('回撤','当前回撤')}<b>${fmtPct(x.current_drawdown)}</b></div>
      <div>${glossary('MA200')}<b class="${x.trend==='above'?'rise':(x.trend==='below'?'fall':'mut')}">${x.trend==='above'?'上方':(x.trend==='below'?'下方':'数据不足')}</b></div>
      <div>${pxLabel}<b>${px}</b></div>
      <div>日K截至<b>${x.as_of||'-'}</b></div>
    </div>
    <div class="hint">曲线、趋势、动量仍按日 K 计算；实时快照价只用于盘中查看和估值参考。</div>
    <div class="qsub" data-q="${escapeHtml(x.code)}"><span class="spin"></span>质量检查加载中（折溢价较慢）…</div>
  </div>`;
}
function qualityHtml(q){
  if(!q)return '<span class="mut">质量数据暂不可用（不影响曲线，可稍后刷新）。</span>';
  const notes=[...(q.issues||[]), ...(q.warnings||[])];
  const premCls=(q.premium_pct!=null&&Math.abs(q.premium_pct)>=1.5)?'down':'';
  const premTxt=q.premium_pct==null?'未知':(q.premium_pct>0?'+':'')+q.premium_pct+'%';
  const scaleTxt=q.market_cap==null?'未知':(q.market_cap/1e8).toFixed(1)+'亿';
  const priceTxt=q.last_price==null?'未知':Number(q.last_price).toFixed(3);
  const turnLabel=q.avg_turnover_20d!=null?'20日成交额':(q.turnover_1d!=null?'近一日成交额':'20日成交额');
  const turnVal=q.avg_turnover_20d!=null?fmtMoney(q.avg_turnover_20d):(q.turnover_1d!=null?fmtMoney(q.turnover_1d):'未知');
  const r=q.returns||{};
  // 涨红跌绿（A股惯例）
  const retCls=v=>v==null?'':(v>0?'rise':'fall');
  const retTxt=v=>v==null?'-':`${v>0?'+':''}${v.toFixed(1)}%`;
  const mddTxt=v=>v==null?'-':`-${Math.abs(v).toFixed(1)}%`;
  const feeTxt=(q.fee&&q.fee.expense_ratio!=null)?(q.fee.expense_ratio*100).toFixed(2)+'%/年':'未知';
  const adm=q.admission||null;
  const admBar=adm?`<div class="hint ${adm.admitted?'':'down'}"><b>§8 准入：${adm.admitted?'✓ 通过':'✗ 未通过'}</b>${(adm.blockers&&adm.blockers.length)?'｜拦截：'+escapeHtml(adm.blockers.join('；')):''}${(adm.data_gaps&&adm.data_gaps.length)?'｜缺数据待复核：'+escapeHtml(adm.data_gaps.join('；')):''}</div>`:'';
  const sc=q.score||null;
  const scBar=sc?`<div class="hint"><b>§8.3 产品分：${sc.total==null?'—':sc.total.toFixed(2)}</b>（覆盖 ${Math.round((sc.coverage||0)*100)}%、置信度 ${sc.confidence||'-'}）${(sc.flags&&sc.flags.length)?'｜'+escapeHtml(sc.flags.join('；')):''}</div>`:'';
  const hasReturns=Object.keys(r).length>0;
  const returnsRow=hasReturns?`
    <div class="mini" style="grid-template-columns:repeat(6,minmax(0,1fr));margin-top:6px;padding-top:6px;border-top:1px solid var(--line)">
      <div>今年以来<b class="${retCls(r.ytd)}">${retTxt(r.ytd)}</b></div>
      <div>近1月<b class="${retCls(r.r1m)}">${retTxt(r.r1m)}</b></div>
      <div>近3月<b class="${retCls(r.r3m)}">${retTxt(r.r3m)}</b></div>
      <div>近6月<b class="${retCls(r.r6m)}">${retTxt(r.r6m)}</b></div>
      <div>近1年<b class="${retCls(r.r1y)}">${retTxt(r.r1y)}</b></div>
      <div>近3年<b class="${retCls(r.r3y)}">${retTxt(r.r3y)}</b></div>
    </div>`:'';
  return `<div class="mini" style="grid-template-columns:repeat(6,minmax(0,1fr));margin-top:0">
      <div>实时价<b>${priceTxt}</b></div>
      <div>历史年限<b>${q.history_years==null?'-':q.history_years+'年'}</b></div>
      <div>${turnLabel}<b>${turnVal}</b></div>
      <div>${glossary('折溢价')}<b class="${premCls}">${premTxt}</b></div>
      <div>${glossary('规模')}<b>${scaleTxt}</b></div>
      <div>综合费率<b>${feeTxt}</b></div>
    </div>${returnsRow}
    <div class="hint">${notes.length?escapeHtml(notes.join('；')):'历史和流动性检查未发现明显问题。'}${q.as_of?` 截至 ${q.as_of}`:''}</div>${admBar}${scBar}`;
}

/* ---------- 数据健康（折进状态条 + 详情弹层） ---------- */
async function loadDataHealth(){
  const r=await fetch('/api/health/data'); const d=await r.json();
  const h=d.health||{};
  const vals=Object.entries(h.valuation_status||{}).map(([code,v])=>`${code}:${v.available?'可用':'缺失'}(${v.source||'无源'})`);
  $('#healthPanel').innerHTML=`<h3 style="margin:0 0 8px;font-size:14px">数据详情 <button class="x" onclick="toggleHealth(event)">×</button></h3>
    <div class="mini" style="margin-top:0">
      <div>信号日期<b>${h.generated_for||'-'}</b></div>
      <div>数据质量<b>${h.data_quality||'-'}</b></div>
      <div>行情截至<b>${h.as_of_summary||'-'}</b></div>
      <div>缓存文件<b>${h.cache_file_count||0}</b></div>
    </div>
    <div class="hint">缺失价格：${(h.missing_prices||[]).join('、')||'无'}；估值：${vals.join('；')||'无估值项'}；${h.used_cache?'本次信号含缓存。':'本次信号未使用缓存。'}</div>`;
  // 信号尚未生成时，用健康数据先把"数据/行情截至"两个 chip 填上（初始为骨架屏占位或 '-'）
  const chipDataEl=$('#chipData');
  if((chipDataEl.textContent.trim()==='-'||chipDataEl.querySelector('.skel')) && h.data_quality){
    const q=h.data_quality, cls=q==='完整'?'b-ok':(q==='缓存可用'?'b-warn':'b-bad');
    chipDataEl.innerHTML=`<span class="badge ${cls}">${q}</span>`;
    $('#chipAsof').textContent=h.as_of_summary||'-';
  }
  // 健康数据已返回 → 还停留在骨架态的 chip 降级为 '-'（如首次使用、尚无信号），不能让骨架闪个不停
  ['#chipData','#chipAsof','#chipValue','#chipCash','#chipActions'].forEach(id=>{
    const c=$(id); if(c&&c.querySelector('.skel'))c.textContent='-';
  });
}

/* ---------- 历史周报 + 详情（合并主从） ---------- */
async function loadReports(){
  const box=$('#reportlist');
  const r=await fetch('/api/reports'); const d=await r.json();
  const rows=d.reports||[];
  if(!rows.length){
    box.innerHTML='<div class="hint">还没有历史周报。点顶部"生成本周信号"，或在 Claude/Codex 里说"给我本周决策简报"，即可生成第一份。</div>';
    // 首次使用无任何周报 → 本周决策卡不能停在骨架态，给引导文案
    const live=$('#weeklyReportLive');
    if(live&&live.querySelector('.skelRows'))live.innerHTML='<div class="hint">点击"生成本周信号"，这里会给出本周结论、该做什么与依据。</div>';
    return;
  }
  box.innerHTML=rows.slice(0,12).map(x=>`<button class="listbtn" data-id="${x.id}" onclick="openReport('${x.id}')">
    <b>${x.generated_for||x.id} · ${x.data_quality||'未知'} · ¥${Number(x.portfolio_value||0).toLocaleString()}</b>
    <span>${x.id} ｜ 行情截至 ${x.as_of_summary||'无'} ｜ 可执行 ${x.actionable_count||0} ｜ 首次试仓 ${x.first_funding_count||0}</span>
  </button>`).join('');
  if(!latestSignalLoaded && rows[0] && rows[0].id) await loadLatestSignal(rows[0].id);
}
async function loadLatestSignal(id){
  try{
    const r=await fetch('/api/reports/'+encodeURIComponent(id)); const d=await r.json();
    if(!d.ok||!d.report||!d.report.signals)return;
    CURRENT_REPORT_ID=id;
    d.report.signals._report_created=d.report.created_at || d.report.id;
    d.report.signals._report_id=id;
    d.report.signals._flags=(d.report.flags&&d.report.flags.flags)||[];
    d.report.signals._flagsMeta=d.report.flags||{};   // M10：补元数据——否则最常见的开页路径看不到"旗标过旧/已忽略"提示
    renderSignals(d.report.signals);
    latestSignalLoaded=true;
    $('#genbtn').textContent='重新生成本周信号';
    document.querySelectorAll('#reportlist .listbtn').forEach(b=>b.classList.toggle('active', b.dataset.id===String(id)));
  }catch(e){
    const live=$('#weeklyReportLive');
    if(live&&live.querySelector('.skelRows'))live.innerHTML='<div class="hint">最新周报加载失败，点击"生成本周信号"重新生成。</div>';
  }
}
async function openReport(id){
  CURRENT_REPORT_ID=id;
  const panel=$('#reportDetailPanel');
  panel.innerHTML='<div class="hint"><span class="spin"></span>加载周报…</div>';
  const r=await fetch('/api/reports/'+encodeURIComponent(id)); const d=await r.json();
  if(!d.ok){panel.innerHTML='<div class="msg err" style="display:block">周报读取失败</div>';return;}
  renderReportDetail(d.report);
  reportShown=true;
  document.querySelectorAll('#reportlist .listbtn').forEach(b=>b.classList.toggle('active', b.dataset.id===String(id)));
}
function renderReportDetail(report){
  const s=report.signals||{};
  if(s._report_created==null) s._report_created=report.created_at||report.id;
  if(s._report_id==null) s._report_id=report.id;
  renderWeeklyReport(s, {mode:'history', container:$('#reportDetailPanel'), flags:(report.flags&&report.flags.flags)||[], flagsMeta:report.flags||{}});
}
function drawReportMomentum(rows, elId){
  const el=document.getElementById(elId||'reportMomentumChart');
  const data=rows.filter(x=>x.momentum!=null);
  if(!el||!data.length)return;
  if(window.echarts){
    const chart=initChart(el);
    chart.setOption({
      animation:false,
      grid:{left:46,right:18,top:24,bottom:66},
      tooltip:{trigger:'axis',axisPointer:{type:'shadow'},
        formatter:p=>{const d=data[p[0].dataIndex]||{};return `${escapeHtml(d.name||'')} (${d.code})<br/>动量 ${Number(p[0].value).toFixed(1)}%`;}},
      xAxis:{type:'category',data:data.map(x=>x.name||x.code),axisLabel:{color:'#6b7280',interval:0,rotate:32,fontSize:10}},
      yAxis:{type:'value',axisLabel:{formatter:'{value}%',color:'#6b7280'},splitLine:{lineStyle:{color:'#edf1f5'}}},
      series:[{type:'bar',data:data.map(x=>({value:Number((x.momentum*100).toFixed(2)),itemStyle:{color:x.trend==='above'?'#c0392b':'#0a7d4d'}}))}]
    });
    setTimeout(()=>chart.resize(),0);
  }else{
    const canvas=el.querySelector('canvas');
    drawChart({querySelector:()=>canvas}, {series:data.map((x)=>({date:x.name||x.code,return_pct:x.momentum*100}))});
  }
}

/* ---------- 月度复盘 ---------- */
async function loadMonthlyReview(){
  const box=$('#monthlyreview');
  try{
    const r=await fetch('/api/review/monthly'); const d=await r.json();
    const months=(d&&d.months)||[];
    if(!months.length){box.innerHTML='<div class="mut">还没有可复盘的月份。生成周报或记录执行后会自动出现。</div>';return;}
    box.innerHTML=months.map(m=>{
      const lvl={good:'up',warn:'down',none:'mut'}[m.verdict_level]||'mut';
      const findings=(m.findings||[]).map(f=>`<div class="down">· ${escapeHtml(f)}</div>`).join('');
      const reasons=(m.skip_reasons||[]).map(x=>`<div class="mut">· 未执行「${escapeHtml(x.reason)}」 ×${x.count}</div>`).join('');
      return `<div class="act">
        <div><b>${escapeHtml(m.month)}</b> · <span class="${lvl}">${escapeHtml(m.verdict)}</span></div>
        <div class="reportHero" style="margin-top:8px">
          <div>周报<b>${m.reports}</b></div>
          <div>建议动作<b>${m.suggested_actions}</b></div>
          <div>已执行<b>${m.executed_total}</b></div>
          <div>未执行<b>${m.skipped_items}</b></div>
          <div>计划外<b class="${m.off_plan_items?'down':''}">${m.off_plan_items}</b></div>
          <div>手续费<b>${fmtMoney(m.fees_total)}</b></div>
        </div>
        ${findings?`<div style="margin-top:6px"><b>需注意</b>${findings}</div>`:''}
        ${reasons?`<div style="margin-top:6px"><b>未执行原因</b>${reasons}</div>`:''}
        <div class="hint" style="margin-top:6px">偏离复盘：计划投入约 ${fmtMoney(m.suggested_amount)} ｜ 实际成交 ${fmtMoney(m.invested_amount)} ｜ 偏离 ${(m.deviation_amount||0)>=0?'+':''}${fmtMoney(m.deviation_amount)}（少投通常更保守，不是错）。</div>
        <div class="hint" style="margin-top:4px">期末组合估值约 ${fmtMoney(m.portfolio_value_end)}（仅作上下文，不用短期涨跌评价长期策略）。</div>
      </div>`;
    }).join('');
  }catch(e){ box.innerHTML='<div class="mut">月度复盘加载失败。</div>'; }
}

/* ---------- 风险旗标 ---------- */
function renderFlags(flags){
  if(!flags||!flags.length) return '<div class="mut">无重大风险旗标</div>';
  const dirBadge={利好:'ok',利空:'bad',中性:'mut'};
  return flags.map(f=>{
    const dc=dirBadge[f.direction]||'mut';
    const assets=((f.affected_assets||[]).join('、'))||'—';
    const act=f.actionable
      ? '<span class="lcbadge warn">影响本周动作</span>'
      : '<span class="lcbadge mut">仅提示</span>';
    const safeUrl=(f.source_url&&/^https?:\/\//i.test(f.source_url))?f.source_url:'';
    const src=safeUrl
      ? `<a href="${escapeHtml(safeUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(f.source||'来源')} ↗</a>`
      : escapeHtml(f.source||'来源');
    return `<div class="flagitem">
      <div><span class="lcbadge ${dc}">${escapeHtml(f.direction||'')}</span> <b>${escapeHtml(f.title||'')}</b> ${act}</div>
      <div class="flagmeta">类别：${escapeHtml(f.category||'-')}　｜　置信度：${escapeHtml(f.confidence||'-')}　｜　影响：${escapeHtml(assets)}</div>
      <div class="flagmeta">来源：${src}${f.date?'　｜　'+escapeHtml(f.date):''}</div>
    </div>`;
  }).join('');
}

/* ---------- 观察池学习卡 ---------- */
async function loadWatchlistLearning(){
  const box=$('#learnbox');
  try{
    const r=await fetch('/api/watchlist/learning'); const d=await r.json();
    const items=(d&&d.items)||[];
    if(!items.length){box.innerHTML='<div class="mut">无观察池。</div>';return;}
    box.innerHTML=items.map(it=>{
      const c=it.card||{};
      const badge={unlocked:['ok','可讨论纳入'],need_ack:['warn','待确认学习'],observing:['mut','继续观察'],learning:['mut','待学习']}[it.unlock_status]||['mut',it.unlock_status||''];
      const risks=(c.risks||[]).map(x=>`<li>${escapeHtml(x)}</li>`).join('');
      const qs=(c.questions||[]).map(x=>`<li>${escapeHtml(x)}</li>`).join('');
      const ackBtn=it.acknowledged
        ? `<span class="up">✓ 已确认学习${it.acknowledged_at?'（'+String(it.acknowledged_at).slice(0,10)+'）':''}</span>`
        : `<button class="ghost" onclick="ackLearning('${it.code}')">我已学习理解</button>`;
      return `<div class="learncard">
        <h3>${escapeHtml(it.name)} <span class="mut" style="font-size:12px">${escapeHtml(it.code)}</span><span class="lcbadge ${badge[0]}">${escapeHtml(badge[1])}</span></h3>
        <div class="lc-sec">跟踪什么</div><div style="font-size:12px">${escapeHtml(c.tracks||it.note||'—')}</div>
        ${risks?`<div class="lc-sec">主要风险</div><ul>${risks}</ul>`:''}
        ${c.goal?`<div class="lc-sec">观察目标</div><div style="font-size:12px">${escapeHtml(c.goal)}</div>`:''}
        ${qs?`<div class="lc-sec">学习问题（答得上才算看懂）</div><ul>${qs}</ul>`:''}
        <div class="lcfoot"><span class="mut">已观察 ${it.observed}/${it.min_observations} 次</span>${ackBtn}</div>
        <div class="hint" style="margin-top:4px">${escapeHtml(it.unlock_reason||'')}　·　观察池不可直接买入。</div>
      </div>`;
    }).join('');
  }catch(e){ box.innerHTML='<div class="mut">学习卡加载失败（若你刚更新过代码，请重启 python3 engine/app.py 再刷新）。</div>'; }
}
async function ackLearning(code){
  try{
    const r=await fetch('/api/watchlist/learning/ack',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code,acknowledged:true})});
    const d=await r.json();
    if(d.ok) await loadWatchlistLearning();
  }catch(e){}
}

/* ---------- 持仓总览（只读预览） ---------- */
function expLabel(v){return {beginner:'新手',intermediate:'有经验',advanced:'进阶'}[v]||v||'-';}
// 右侧「策略一览」（设定/背景），与关键数字卡同列
function renderPortfolioPreview(){
  const box=$('#portfolioStrategy'); if(!box)return;
  const c=CURRENT_CONFIG;
  if(!c){box.innerHTML='';return;}
  const ip=c.investor_profile||{}, rc=c.risk_controls||{};
  box.innerHTML=`
    <span>风险偏好 <b>${escapeHtml(c.risk_profile||'-')}</b></span>
    <span>目标年化 <b>${toPct(ip.target_annual_return??0,1)}%</b></span>
    <span>可接受回撤 <b>${toPct(ip.max_acceptable_drawdown??0,0)}%</b></span>
    <span>总资金 <b>¥${Number(ip.total_assets||((ip.stable_assets_outside||0)+(ip.planned_etf_capital||0))).toLocaleString()}</b></span>
    <span>工具上限 <b>¥${Number(ip.planned_etf_capital||0).toLocaleString()}</b></span>
    <span>失业保障 <b>${ip.unemployment_runway_years??'-'} 年</b></span>
    <span>单周上限 <b>¥${Number(rc.max_weekly_trade_amount||0).toLocaleString()}</b></span>
    <span>缓存交易 <b>${rc.allow_trade_with_cache?'允许':'禁止'}</b></span>`;
}
async function loadConstruct(){
  const box=$('#constructBox'); if(!box)return;
  box.hidden=false;
  box.innerHTML='<div class="hint" id="constructProgress"><span class="spin"></span>正在寻找满足风险与配置约束的长期组合…</div>';
  const stopPoll=pollTaskProgress('constructProgress',Date.now(),{tasks:['construct','signals'],hint:'通常半分钟内；信号过期会先自动刷新，最长 4 分钟'});
  try{
    const d=await fetch('/api/strategic/construct').then(r=>r.json());
    if(!d.ok)throw new Error(d.error||'failed');
    renderConstruct(d.construct);
  }catch(e){ box.innerHTML='<div class="msg err" style="display:block">模型组合构建失败：'+escapeHtml(String(e.message||e))+'</div>'; }
  finally{ stopPoll(); }
}
function renderConstruct(s){
  const box=$('#constructBox'); if(!box||!s)return;
  CURRENT_CONSTRUCT=s;
  STRATEGY_FLOW.construct=s; renderStrategyFlow();
  const er=s.employment_resilience||{};
  const resilienceBar=Object.keys(er).length
    ? `<div class="${er.passes?'hint':'wk-alarm'}"><b>职业风险联合压力测试：${er.passes?'通过':'未通过'}</b>｜需隔离生活保障金 ¥${Number(er.required_reserve||0).toLocaleString()}｜可用于投资风险缓冲 ¥${Number(er.risk_buffer_available||0).toLocaleString()}${er.shortfall?`｜缺口 ¥${Number(er.shortfall).toLocaleString()}`:''}</div>`
    : '';
  const qg=s.quality_gate||{}; const qs=s.product_quality_status||qg.status; const qsZh=_zh(_QSTAT_ZH,qs)||'未知';
  const qualityBar=qg.blocked
    ? `<div class="wk-alarm"><b>质量数据不足（${escapeHtml(qsZh)}），已禁止自动应用</b>${(qg.missing_records&&qg.missing_records.length)?`：${qg.missing_records.length} 个成员无准入记录（${escapeHtml(qg.missing_records.join('/'))}）`:''}。请到「ETF 准入审视」刷新质量数据后重新构建。</div>`
    : `<div class="hint mut">质量数据：${escapeHtml(qsZh)}</div>`;
  if(s.validation_status==='no_feasible_portfolio'){
    box.innerHTML=`<h3>模型组合</h3>${resilienceBar}${qualityBar}<div class="wk-alarm"><b>当前约束下没有可行组合</b>：${escapeHtml((s.constraint_diagnostics||[]).join('；'))}。已检查 ${s.candidates_evaluated} 个候选。</div>`;
    return;
  }
  const m=s.metrics||{};
  const rows=(s.comparison||[]).filter(x=>x.current>0||x.constructed>0).map(x=>
    `<tr><td><b>${escapeHtml(x.name||x.code)}</b> <span class="mut">${x.code}</span></td>
      <td>${(x.current*100).toFixed(0)}%</td><td>${(x.constructed*100).toFixed(0)}%</td>
      <td class="${x.delta>0?'rise':(x.delta<0?'fall':'mut')}">${x.delta>=0?'+':''}${(x.delta*100).toFixed(0)}pp</td></tr>`).join('');
  const pol=Object.entries(s.policy_allocation||{}).filter(([,v])=>v>0).map(([k,v])=>`${escapeHtml(_zh(_ROLE_ZH,k))} ${(v*100).toFixed(0)}%`).join(' · ');
  const ce=Object.entries(m.country_equity||{}).map(([k,v])=>`${escapeHtml(_zh(_COUNTRY_ZH,k))} ${(v*100).toFixed(0)}%`).join('/');
  const cu=Object.entries(m.currency_exposure||{}).map(([k,v])=>`${escapeHtml(_zh(_CCY_ZH,k))} ${(v*100).toFixed(0)}%`).join('/');
  const rcu=Object.entries(m.risk_currency_exposure||{}).map(([k,v])=>`${escapeHtml(_zh(_CCY_ZH,k))} ${(v*100).toFixed(0)}%`).join('/');
  const vsZh=_zh(_VSTAT_ZH,s.validation_status);
  const canApply=s.validation_status==='passed' && !qg.blocked;
  const applyBtn=canApply
    ? `<button onclick="applyStrategicConstruct()">应用模型组合</button>`
    : `<button class="ghost" disabled title="${qg.blocked?'质量数据不足，已禁止应用':'未通过最终验证，不能应用'}">应用模型组合（已禁用）</button>`;
  box.innerHTML=`<h3>模型组合 <span class="mut">保存前请确认变化与执行成本</span></h3>
    ${resilienceBar}${qualityBar}
    <div class="hint">在 ${s.candidates_evaluated} 个候选中有 ${s.feasible_count} 个满足约束；最终状态：<b class="${s.validation_status==='passed'?'rise':'down'}">${escapeHtml(vsZh)}</b>。</div>
    <div class="hint">角色配置：${pol}</div>
    <table><thead><tr><th>ETF</th><th>当前</th><th>模型组合</th><th>变化</th></tr></thead><tbody>${rows}</tbody></table>
    <div class="hint">${s.construct_return_basis==='anchored'
      ? `<b>已按前瞻锚定构建</b>（债券=当前YTM、A股=中性锚+估值回归、QDII=美债+ERP）：ETF 桶预期年化 <b>${(m.expected_etf_return*100).toFixed(1)}%</b>（保守 <b>${(m.expected_etf_return_conservative*100).toFixed(1)}%</b>）${m.expected_etf_return_frozen!=null?`<span class="mut">｜冻结假设口径对照 ${(m.expected_etf_return_frozen*100).toFixed(1)}%</span>`:''}${s.incumbent_expected_return_anchored!=null?`｜当前组合锚定 ${(s.incumbent_expected_return_anchored*100).toFixed(1)}%`:''}`
      : `预期年化 <b>${(m.expected_etf_return*100).toFixed(1)}%</b>（保守 ${(m.expected_etf_return_conservative*100).toFixed(1)}%）${s.construct_return_note?`<span class="mut">｜${escapeHtml(s.construct_return_note)}</span>`:''}`}｜缺口 ${(m.target_gap*100).toFixed(1)}%（保守 ${(m.target_gap_conservative*100).toFixed(1)}%）｜${m.worst_scenario?`最坏情景「${escapeHtml(m.worst_scenario)}」`:''}全组合压力 <b>${(m.whole_portfolio_stress*100).toFixed(1)}%</b>${s.construct_stress_budget!=null?`（预算 ${(s.construct_stress_budget*100).toFixed(0)}%）`:''}${m.covariance_stress!=null?`｜协方差压力 ${(m.covariance_stress*100).toFixed(1)}%（真实相关·覆盖 ${((m.covariance_covered_weight||0)*100).toFixed(0)}%）`:''}${m.effective_risk_sources!=null?`｜有效风险源 ${Number(m.effective_risk_sources).toFixed(1)}`:''}｜卫星 ${(m.satellite_total*100).toFixed(0)}%｜成长 ${(m.growth_factor_total*100).toFixed(0)}%｜国别权益 ${ce}｜风险货币 ${rcu||'-'}｜全部货币 ${cu}</div>
    <div class="hint mut">注：上面的「预期年化」是<b>前瞻预期</b>（锚今天的利率/估值·刻意保守·非承诺），与「模型组合是否优于简单组合」里的<b>历史回测年化</b>口径不同，<b>不可直接相比</b>。</div>
    ${bbBlocks(s)}
    ${renderRationale(s.rationale)}
    <details class="assumptions"><summary>查看模型选择口径</summary><div class="hint mut">先缩小保守收益缺口，再控制最坏压力，然后比较收益与集中度。收益不是承诺；压力取 ${s.scenarios_count||'多'} 个情景中的最坏结果。${s.input_fingerprint?`<br>输入指纹 ${escapeHtml(s.input_fingerprint)}`:''}</div></details>
    <div class="row2">${applyBtn}</div>`;
}
function renderRationale(r){
  if(!r||!(r.roles||[]).length)return '';
  const items=r.roles.map(role=>{
    const mem=(role.members||[]).map(x=>`<li><b>${escapeHtml(x.name)}</b> <span class="mut">${escapeHtml(x.code)}</span> ${(x.weight*100).toFixed(0)}% — ${escapeHtml(x.reason)}</li>`).join('');
    return `<div class="ratRole"><div class="ratHead"><b>${escapeHtml(_zh(_ROLE_ZH,role.role))} ${(role.weight*100).toFixed(0)}%</b> <span class="mut">${escapeHtml(_zh(_TIER_ZH,role.tier))}</span></div>
      <div class="hint">${escapeHtml(role.purpose||'')}</div>
      <div class="hint mut">为什么是这个比例：${escapeHtml(role.band||'')}</div>
      ${mem?`<ul class="ratMembers">${mem}</ul>`:''}</div>`;
  }).join('');
  const notes=(r.notes||[]).length?`<div class="hint mut">约束触发：${r.notes.map(escapeHtml).join(' ')}</div>`:'';
  return `<details class="rationale" open><summary><b>为什么这样配（这几只 ETF / 这个比例）</b></summary>
    <div class="hint">${escapeHtml(r.objective||'')}</div>
    <div class="ratRoles">${items}</div>${notes}</details>`;
}
async function applyStrategicConstruct(confirmMoves){
  try{
    const fp=CURRENT_CONSTRUCT&&CURRENT_CONSTRUCT.input_fingerprint;
    if(!fp){ flash('请先查看模型组合再应用','err'); return; }
    const r=await fetch('/api/strategic/apply',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({user_decision:'apply_construct', input_fingerprint:fp, confirm_large_moves:confirmMoves===true})});
    const d=await r.json();
    if(r.status===409 && d.needs_confirmation){
      const th=((d.threshold||0.15)*100).toFixed(0);
      const moveRows=(d.large_moves||[]).map(x=>`<tr><td><b>${escapeHtml(String(x.code))}</b></td><td>${(x.current*100).toFixed(0)}%</td><td><b>${(x.constructed*100).toFixed(0)}%</b></td><td class="${x.delta>=0?'up':'down'}">${x.delta>=0?'+':''}${(x.delta*100).toFixed(0)}pp</td></tr>`).join('');
      const okMove=await confirmDialog({
        title:'目标权重大幅跳变，确认应用？',
        body:`<div class="hint">以下 ${(d.large_moves||[]).length} 项目标权重一次跳变超过 ${th}pp：</div>
          <table class="confirmTable"><thead><tr><th>代码</th><th>当前目标</th><th>新目标</th><th>变化</th></tr></thead><tbody>${moveRows}</tbody></table>
          <div class="hint">应用后旧周度建议将失效，需重新生成本周信号；实际调仓仍按周度建议分批进行、不是一次到位。</div>`,
        confirmText:'确认应用',cancelText:'暂不应用'});
      if(okMove){ return applyStrategicConstruct(true); }
      return;
    }
    if(r.status===409 && d.stale){
      flash('构建输入已变化，正在刷新最新模型组合，请重新确认后再应用','err');
      await loadConstruct();
      return;
    }
    if(!d.ok)throw new Error((d.errors||[d.error]).filter(Boolean).join('；')||'failed');
    await loadConfig();
    await loadConstruct();
    await loadExecutions();
    flash('✓ 权威模型组合已应用为当前目标权重；旧周度建议已失效，请重新生成本周信号。');
  }catch(e){
    flash('应用模型组合失败：'+escapeHtml(String(e.message||e)),'err');
  }
}
async function loadStrategicBacktest(){
  const box=$('#strategicBacktestBox'); if(!box)return;
  box.hidden=false;
  box.innerHTML='<div class="hint" id="strategicBtProgress"><span class="spin"></span>跑战略组合对比回测（全收益长面板·含成本，较慢）…</div>';
  const stopPoll=pollTaskProgress('strategicBtProgress',Date.now(),{tasks:['backtest'],hint:'通常 30–120 秒'});
  try{
    const d=await fetch('/api/strategic/backtest',{method:'POST'}).then(r=>r.json());
    if(!d.ok)throw new Error(d.error||'failed');
    renderStrategicBacktest(d.result);
    STRATEGY_FLOW.validated=true; renderStrategyFlow();   // 跑过对比 → 步骤④标记已验证
    loadEvidenceLedger();                                 // §0C #2：附证据台账 + 真 walk-forward
  }catch(e){ box.innerHTML='<div class="msg err" style="display:block">对比回测失败：'+escapeHtml(String(e.message||e))+'</div>'; }
  finally{ stopPoll(); }
}
function renderStrategicBacktest(res){
  const box=$('#strategicBacktestBox'); if(!box||!res)return;
  const verdict=strategicComplexityVerdict(res);
  const rows=(res.rows||[]).map(r=>{
    const hl=(r.name==='权威构建')?' style="background:rgba(80,140,255,.08)"':'';
    return `<tr${hl}><td><b>${escapeHtml(r.name)}</b></td>
      <td class="${r.cagr>=0?'rise':'fall'}">${(r.cagr*100).toFixed(1)}%</td>
      <td>${(r.vol*100).toFixed(1)}%</td><td class="down">${(r.max_drawdown*100).toFixed(1)}%</td>
      <td>${Number(r.calmar).toFixed(2)}</td><td>${r.calmar_zero_coupon!=null?Number(r.calmar_zero_coupon).toFixed(2):'-'}</td>
      <td>${r.effective_bets!=null?r.effective_bets.toFixed(1):'-'}</td>
      <td>${(r.turnover_annual*100).toFixed(0)}%</td></tr>`;
  }).join('');
  const rm=res.risk_model;
  const roll=(res.rolling||[]).map(r=>`<div class="mut">· ${escapeHtml(r.name)}：三段 Calmar ${r.fold_calmar.map(x=>x.toFixed(2)).join(' / ')}</div>`).join('');
  const pert=(res.perturbation||[]).map(p=>`<div class="mut">· 收益×${(1+p.return_delta).toFixed(1)}：${p.status}｜卫星 ${(p.satellite*100).toFixed(0)}%｜成长 ${(p.growth*100).toFixed(0)}%｜压力 ${(p.whole_stress*100).toFixed(0)}%</div>`).join('');
  const ew=res.excluded_weight||{};
  const ewLine=(ew['权威构建']||ew['当前'])
    ? `<div class="act"><b>诚实口径：可代理子集对比</b><br>无 20 年长代理的成长卫星(创业板/科创50)已统一从各组合剔除并各自归一——被剔权重：权威构建 ${((ew['权威构建']||0)*100).toFixed(0)}%、当前 ${((ew['当前']||0)*100).toFixed(0)}%。这部分成长桶的增量价值不在本回测覆盖内，不可据本表否定它。</div>`
    : '';
  const dedupLine=(res.deduped&&res.deduped.length)
    ? `<div class="hint mut">去退化重复基准：${res.deduped.map(d=>`${escapeHtml(d.name)}≡${escapeHtml(d.same_as)}`).join('、')}（与既有基准完全相同，仅测一次）。</div>`
    : '';
  const bs=res.bond_sensitivity;
  const bondLine=bs
    ? `<details class="assumptions"><summary>债券票息敏感性（Calmar 零息列）</summary><div class="hint mut">主表「Calmar」用 +${(bs.bond_carry*100).toFixed(0)}%/年票息近似债券全收益；「Calmar零息」列为 0% 票息重跑。若两列间"谁的 Calmar 更高"翻转，说明该结论被债券票息假设驱动、不可作上线依据。</div></details>`
    : '';
  const wmap=res.weights||{}, nmap=res.names||{};
  const fmtW=(n)=>{const w=wmap[n]; if(!w)return '—'; return Object.entries(w).sort((a,b)=>b[1]-a[1]).map(([c,v])=>`${escapeHtml(nmap[c]||c)} ${(v*100).toFixed(0)}%`).join(' · ');};
  const weightsBlock=Object.keys(wmap).length
    ? `<details class="assumptions" open><summary><b>各组合分别怎么配仓？</b></summary>`+
      (res.rows||[]).map(r=>`<div class="mut" style="margin:5px 0"><b>${escapeHtml(r.name)}</b>：${fmtW(r.name)}</div>`).join('')+
      `<div class="hint mut">注：① 成长卫星(创业板/科创50)无 20 年代理、已统一剔除并归一；② 这里的「权威构建」是<b>不含今日限购约束</b>的长期战略形态（为跨 21 年历史对比），第 3 步要<b>应用</b>的版本会按今日实时准入把限购品种（标普/纳指）冻结在当前权重，故标普/纳指比例可能不同。<b>本表是对比用的配仓，不是直接拿去下单的</b>。</div></details>`
    : '';
  const actionBlock=`<div class="act"><b>这结果该怎么用（然后呢？要不要调？）</b>
    <br>这是<b>决策支持、不是指令</b>：用约 ${res.years} 年代理数据，把"你的权威构建"和几个更简单的替代放一起，看复杂度值不值。
    <br>· <b>若「建议简化」</b>：更简单的组合在这段历史里并不更差 → 你<b>可以</b>选更简单。要这么做<b>不是手改某只权重</b>，而是到「长期战略设置」<b>调低 卫星/权益 上限</b>，再回第 3 步重新构建，引擎会据此产出更简单的模型。
    <br>· <b>若「保留复杂度」</b>：当前配置的复杂度在历史上有可观察的增量价值 → 照第 3 步的权威构建应用即可。
    <br>· <b>想省事 / 不确定</b>：直接用第 3 步的「权威构建」就行——这一步只是体检，不做也能投。
    <br><span class="mut">硬提醒：代理数据、已剔除成长卫星、过去≠未来。它帮你判断"要不要更简单"，不替你下命令。</span></div>`;
  box.innerHTML=`<h3>战略组合长期对比 <span class="mut">约 ${res.years} 年历史样本</span></h3>
    <div class="${verdict.kind==='keep'?'hint':(verdict.kind==='simplify'?'wk-alarm':'act')}"><b>${verdict.title}</b><br>${verdict.detail}</div>
    ${actionBlock}
    <div class="hint">${res.start} → ${res.end}；剔除无长代理：${(res.dropped||[]).join('、')||'无'}（创业板/科创50/QDII 无长序列）。</div>
    ${ewLine}${dedupLine}
    <div class="hint mut">「年化」为<b>历史已实现回测口径</b>（这段样本跑出来的·非未来预测）；与第 3 步「构建模型组合」的<b>前瞻预期年化</b>是两套口径——后者锚今天的利率/估值、刻意保守，<b>数值本就不同、不可直接相比</b>。</div>
    <table><thead><tr><th>组合</th><th>年化<span class="mut">·回测</span></th><th>波动</th><th>最大回撤</th><th>Calmar</th><th>Calmar零息</th><th>有效风险源</th><th>年换手</th></tr></thead><tbody>${rows}</tbody></table>
    ${weightsBlock}
    ${bondLine}
    ${rm?`<details class="assumptions"><summary>查看风险模型口径</summary><div class="hint mut">使用周频 ${rm.obs} 期的收缩协方差；平均相关 ${rm.avg_corr}，收缩 ${rm.shrink}。有效风险源越多，组合风险越分散。</div></details>`:''}
    ${roll?`<div class="act"><b>稳健性①·滚动子期 Calmar</b>${roll}</div>`:''}
    ${pert?`<div class="act"><b>稳健性②·假设 ±20% 收益扰动重构</b>${pert}</div>`:''}
    <div class="hint">判断规则：若简化组合在风险与成本上并不更差，就不应为复杂配置付出维护成本。过去不代表未来，代理数据仅用于结构比较。</div>
    <div id="evidenceLedgerBox"></div>`;
}
function tierBadge(t){
  const m={logic:['仅逻辑','var(--mut)'],in_sample:['样本内','var(--amber)'],walk_forward:['样本外·WF','var(--green)'],live:['实盘','var(--blue)']};
  const [label,color]=m[t]||[t,'var(--mut)'];
  return `<span style="display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;font-weight:700;color:#fff;background:${color}">${escapeHtml(label)}</span>`;
}
function renderEvidenceLedger(res){
  const box=$('#evidenceLedgerBox'); if(!box||!res)return;
  const badgeFor=(c)=>(c.id==='live_track_record'&&c.tier!=='live')
    ?`<span style="display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;font-weight:700;color:#fff;background:var(--amber)">实盘·积累中</span>`
    :tierBadge(c.tier);
  const claims=(res.claims||[]).map(c=>`<tr><td><b>${escapeHtml(c.claim)}</b></td><td>${badgeFor(c)}</td><td class="mut" style="font-size:12px">${escapeHtml(c.basis||'')}</td><td class="mut" style="font-size:12px">${escapeHtml(c.caveat||'')}</td></tr>`).join('');
  const wf=res.walk_forward; let wfBlock='';
  if(wf&&wf.folds&&wf.folds.length){
    const s=wf.summary||{};
    const foldRows=wf.folds.map(f=>{
      const r=(f.rows||[]).map(x=>`${escapeHtml(x.name)} ${Number(x.calmar).toFixed(2)}`).join(' · ');
      return `<div class="mut">· 测试 ${f.test[0]}→${f.test[1]}（${f.test_years}年，训练截至 ${f.train_end}）：简化${f.simpler_ge_construct?'≥':'<'}构建 ｜ Calmar：${r}</div>`;
    }).join('');
    wfBlock=`<div class="act"><b>真 walk-forward（每折只用过去数据构建、在未来段评估）</b><br>${escapeHtml(s.verdict||'')}（${s.n_folds} 折中 ${s.simpler_wins} 折简化≥构建）${foldRows}<div class="hint">这是<b>样本外</b>检验：构建只看过去、评估在没见过的未来段，把"建议简化"从样本内升级为样本外结论。${escapeHtml(s.note||'')}</div></div>`;
  }
  box.innerHTML=`<h3 style="margin-top:24px">证据台账 <span class="mut">每条"更优"主张 → 证据强度</span></h3>
    <div class="hint">证据档强弱：仅逻辑 &lt; 样本内 &lt; 样本外(walk-forward) &lt; 实盘。<b>没有一条被夸成"实盘已证明"</b>——实盘档要靠每周记账慢慢积累。</div>
    <table><thead><tr><th>主张</th><th>证据档</th><th>依据</th><th>局限</th></tr></thead><tbody>${claims}</tbody></table>
    ${wfBlock}`;
}
async function loadEvidenceLedger(){
  const box=$('#evidenceLedgerBox'); if(!box)return;
  box.innerHTML='<div class="hint"><span class="spin"></span>跑证据台账 + 真 walk-forward（较慢）…</div>';
  try{
    const d=await fetch('/api/strategic/evidence',{method:'POST'}).then(r=>r.json());
    if(!d.ok)throw new Error(d.error||'failed');
    renderEvidenceLedger(d.result);
  }catch(e){ box.innerHTML='<div class="hint mut">证据台账暂不可用：'+escapeHtml(String(e.message||e))+'</div>'; }
}
function strategicComplexityVerdict(res){
  const rows=res.rows||[];
  const model=rows.find(r=>r.name==='权威构建');
  const simple=rows.filter(r=>['仅核心','无卫星','无黄金','更低权益'].includes(r.name));
  if(!model||!simple.length)return {kind:'unknown',title:'证据不足',detail:'缺少权威构建或简单组合的可比数据，暂不据此调整复杂度。'};
  const comparable=simple.filter(r=>r.cagr>=model.cagr-0.005 && r.max_drawdown>=model.max_drawdown-0.01 &&
    r.calmar>=model.calmar*0.95 && r.turnover_annual<=model.turnover_annual+0.05);
  if(comparable.length){
    const best=comparable.sort((a,b)=>b.calmar-a.calmar)[0];
    return {kind:'simplify',title:'建议简化',detail:`${escapeHtml(best.name)}在收益接近的同时，回撤、Calmar 与换手不劣于模型组合，复杂度尚未证明有价值。`};
  }
  const bestSimple=simple.slice().sort((a,b)=>b.calmar-a.calmar)[0];
  if(model.calmar>=bestSimple.calmar*1.10 && model.cagr>=bestSimple.cagr-0.002){
    return {kind:'keep',title:'保留当前复杂度',detail:`模型组合的风险收益效率明显优于最佳简单组合「${escapeHtml(bestSimple.name)}」，当前复杂度有可观察的增量价值。`};
  }
  return {kind:'unknown',title:'证据不足',detail:'模型组合与简单组合互有胜负，尚不足以支持简化或确认复杂度价值。'};
}
const _DISP={keep:['保留','mut'],trim:['减配','down'],review:['评审·二选一','warn'],review_data:['待复核·数据缺失','warn'],replace_candidate:['暂不加仓','down']};
const _RSTAT={within:'区间内',above:'超上限',below:'低于下限'};
// 角色/层级/产品状态 中文名（与 strategy.yaml strategic_policy.roles 对应；未知键回退原文，不报错）
const _ROLE_ZH={china_core_equity:'A股核心权益',us_core_equity:'美股核心权益',defensive_equity:'防御权益',growth_satellite:'成长卫星',government_bond:'国债',gold:'黄金'};
const _TIER_ZH={core:'核心',core_defensive:'核心防御',satellite:'卫星',diversifier:'分散器'};
const _PSTAT_ZH={scored:'数据完整',degraded:'数据偏少',insufficient:'数据不足'};
// 构建结果里的英文/代码字段中文化（未知键回退原文，不报错）
const _VSTAT_ZH={passed:'通过',violated:'约束冲突',no_feasible_portfolio:'无可行组合',blocked_quality_data:'质量数据不足·已锁定'};
const _QSTAT_ZH={cached:'已缓存·新鲜',missing:'缺失',stale:'已过期',ok:'正常'};
const _COUNTRY_ZH={CN:'中国',US:'美国',HK:'香港',JP:'日本',EU:'欧洲'};
const _CCY_ZH={CNY:'人民币',USD:'美元',HKD:'港元',JPY:'日元',EUR:'欧元'};
// 每个处置建议对应的"怎么做"
const _DISP_ACTION={keep:'暂无明显问题，无需动作。',
  trim:'权重超出区间上限 → 下次「调仓」时减回区间内。',
  review:'与同角色另一只重复或高重合 → 二选一：保留更优的一只、清掉另一只。',
  review_data:'当前取不到准入/质量数据（多为非交易时段、盘后或数据源限频）→ 无法判定，先持有不动，等交易时段重新审视，不要据此动作。',
  replace_candidate:'有真实阻断（溢价过高/暂停申购/规模流动性不达标等）→ 暂不加仓、守现状；长期不达标且下方「替代候选」里有合格同类时，才考虑替换。'};
function _zh(map,k){ return map[k]||k; }
async function loadIncumbents(withTe,withOverlap){
  const box=$('#incumbentBox'); if(!box)return;
  box.hidden=false;
  const extra=[withTe?'跟踪离散度':'',withOverlap?'持仓重合':''].filter(Boolean).join('+');
  box.innerHTML='<div class="hint" id="incumbentProgress"><span class="spin"></span>正在逐只检查产品质量与组合角色'+(extra?'（含'+extra+'，较慢）':'')+'…</div>';
  const stopPoll=pollTaskProgress('incumbentProgress',Date.now(),{tasks:['incumbents'],hint:'通常 1–2 分钟；盘后数据源较慢'});
  try{
    const qs=[]; if(withTe)qs.push('te=1'); if(withOverlap)qs.push('overlap=1');
    const d=await fetch('/api/strategic/incumbents'+(qs.length?'?'+qs.join('&'):'')).then(r=>r.json());
    if(!d.ok)throw new Error(d.error||'failed');
    renderIncumbents(d,!!withTe,!!withOverlap);
    loadStrategyFlow();   // 审视会刷新质量/准入缓存 → 同步步骤条第②步状态
  }catch(e){ box.innerHTML='<div class="msg err" style="display:block">ETF 审视失败：'+escapeHtml(String(e.message||e))+'</div>'; }
  finally{ stopPoll(); }
}
function renderIncumbents(d,withTe,withOverlap){
  const box=$('#incumbentBox'); if(!box)return;
  const rows=(d.incumbents||[]).map(r=>{
    const [dl,dc]=_DISP[r.disposition]||[r.disposition,'mut'];
    const cap=r.role_range_status==='above'||r.single_cap_exceeded
      ? `<span class="down">${_RSTAT[r.role_range_status]||''}${r.single_cap_exceeded?'·单只超10%':''}</span>` : `<span class="mut">区间内</span>`;
    const adm=r.admitted==null?'<span class="mut">待复核</span>':(r.admitted?'<span class="rise">✓</span>':'<span class="down">✗</span>');
    const sc=r.product_total==null?'<span class="mut">—</span>':`${r.product_total.toFixed(2)}<span class="mut">/${_zh(_PSTAT_ZH,r.product_status)}</span>`;
    const ovTip=r.max_same_role_overlap!=null?` title="同角色最大持仓重合 ${(r.max_same_role_overlap*100).toFixed(0)}%"`:'';
    const tags=`${r.consolidation_candidate?' <span class="warn" title="同卫星角色+同资产多成员，§11 建议二选一">二选一</span>':''}${r.holdings_redundant?' <span class="down"'+ovTip+'>高重合</span>':''}`;
    const actTip=escapeHtml(_DISP_ACTION[r.disposition]||'');
    return `<tr><td><b>${escapeHtml(r.name||r.code)}</b> <span class="mut">${r.code}</span>
        <button class="ghost chipbtn" onclick="loadEtfPeers('${escapeHtml(r.code)}','${escapeHtml(r.name||r.code)}')">找同类</button></td>
      <td>${escapeHtml(_zh(_ROLE_ZH,r.role))}<span class="mut"> / ${escapeHtml(_zh(_TIER_ZH,r.tier))}</span></td>
      <td>${(r.current_weight*100).toFixed(0)}%</td><td>${cap}</td><td style="text-align:center">${adm}</td>
      <td>${sc}</td><td><span class="${dc}" title="${actTip}"><b>${dl}</b></span>${tags}</td></tr>`;
  }).join('');
  const cats=(d.catalog||[]).map(c=>{
    const st=c.range_status==='above'?'down':(c.range_status==='below'?'warn':'mut');
    return `<span class="${st}">${escapeHtml(_zh(_ROLE_ZH,c.role))} ${(c.current_total*100).toFixed(0)}%/[${(c.range[0]*100).toFixed(0)}-${(c.range[1]*100).toFixed(0)}]</span>`;
  }).join(' · ');
  const candidates=(d.replacement_candidates||[]);
  const candidateRows=candidates.map(c=>`<tr><td><b>${escapeHtml(c.name||c.code)}</b> <span class="mut">${c.code}</span></td>
    <td>${escapeHtml(_zh(_ROLE_ZH,c.role))}</td><td>${escapeHtml(c.source==='watchlist'?'观察池':'ETF池')}</td>
    <td>${c.admitted===true?'<span class="rise">通过</span>':(c.admitted===false?'<span class="down">不通过</span>':'<span class="mut">待复核</span>')}</td>
    <td>${c.product_total==null?'-':Number(c.product_total).toFixed(2)}</td>
    <td><button class="ghost chipbtn" ${c.admitted===false?'disabled title="基本准入未通过"':''} onclick="introduceStrategicCandidate('${escapeHtml(c.role)}','${escapeHtml(c.code)}')">引入对应角色</button></td></tr>`).join('');
  const candidateBlock=candidates.length
    ? `<div class="wk-sec">替代候选（可换上的同类 ETF）</div><table><thead><tr><th>候选 ETF</th><th>拟引入角色</th><th>来源</th><th>基本准入</th><th>产品质量</th><th>操作</th></tr></thead><tbody>${candidateRows}</tbody></table>
       <div class="hint">「引入对应角色」只是把候选加进该战略角色的备选；之后仍要重新构建模型组合、通过约束，才会真正改变目标权重。</div>`
    : `<div class="hint"><b>替代候选（可换上的同类 ETF）：当前没有。</b> 若某只被标「考虑替换」，要换需先在 ETF 池或观察池加入一只与它<b>同资产类型</b>的候选；没有合格候选时，正确做法是<b>先持有不动、等它准入恢复</b>（QDII 溢价/限购通常是暂时的），而不是带病加仓。</div>`;
  const dataGapN=(d.incumbents||[]).filter(r=>r.disposition==='review_data').length;
  const totalN=(d.incumbents||[]).length;
  const offHours=d.trading_session===false;
  const dataMissing=totalN&&dataGapN>=Math.ceil(totalN*0.6);
  let dataBanner='';
  if(dataMissing)
    dataBanner=`<div class="wk-alarm"><b>当前取不到大部分 ETF 的准入/质量数据（${dataGapN}/${totalN}）。</b>多为盘后或数据源临时限频。请在 <b>A 股交易时段（工作日 9:30–11:30 / 13:00–15:00）</b>重新「审视当前 ETF」；数据取到前不要据此调仓（系统也会拦住带缺失数据的构建）。</div>`;
  else if(offHours)
    dataBanner=`<div class="hint"><b>提示：现在是非交易时段。</b>折溢价是陈旧数据、已<b>不计入长期准入</b>（折溢价只在你下单/调仓时把关）。长期战略的结构性审视与构建不受影响，可正常进行。</div>`;
  box.innerHTML=`<h3>当前 ETF 审视 <span class="mut">规则版本 ${d.policy_version??'-'}</span></h3>
    ${dataBanner}
    <div class="hint">各角色合计 vs 允许区间：${cats}</div>
    <table><thead><tr><th>ETF</th><th>组合角色</th><th>权重</th><th>是否超限</th><th>基本准入</th><th>产品质量</th><th>建议</th></tr></thead><tbody>${rows}</tbody></table>
    <div class="hint"><b>建议怎么做（鼠标悬停每个建议看详情）：</b>
      <br>· <b>保留</b>＝暂无明显问题，无需动作。
      <br>· <b>减配</b>＝权重超出区间上限 → 下次「调仓」时减回区间内。
      <br>· <b>评审·二选一</b>＝与同角色另一只重复/高重合 → 保留更优的一只、清掉另一只。
      <br>· <b>待复核·数据缺失</b>＝当前取不到准入/质量数据（多为非交易时段/盘后/数据源限频）→ <b>无法判定，先持有不动</b>，交易时段重新审视，别据此动作。
      <br>· <b>暂不加仓</b>＝有真实阻断（溢价过高/暂停申购/规模流动性不达标）→ 守现状不加；长期不达标且下方「替代候选」有合格同类才考虑替换。
      <br><span class="mut">产品质量列：分值越高越好；「数据偏少/不足」表示部分指标缺失、置信度降低。</span>
      <br>${withTe?'':`<button class="ghost chipbtn" onclick="loadIncumbents(true,${withOverlap?'true':'false'})">补算跟踪离散度（慢）</button>`}${withOverlap?'':`　<button class="ghost chipbtn" onclick="loadIncumbents(${withTe?'true':'false'},true)">补算持仓重合（慢）</button>`}</div>
    ${candidateBlock}
    <div id="etfPeersBox"></div>`;
}
async function loadEtfPeers(code,name){
  const box=$('#etfPeersBox'); if(!box)return;
  box.scrollIntoView({behavior:'smooth',block:'nearest'});
  box.innerHTML=`<div class="wk-sec">同类发现：${escapeHtml(name)} <span class="mut">${escapeHtml(code)}</span></div><div class="hint"><span class="spin"></span>首次拉全市场 ETF 清单约 30 秒（之后当日缓存）…</div>`;
  try{
    const d=await fetch('/api/etf/peers?code='+encodeURIComponent(code)).then(r=>r.json());
    if(!d.ok){box.innerHTML=`<div class="msg err" style="display:block">${escapeHtml(d.error||'失败')}</div>`;return;}
    if(!d.spot_available){box.innerHTML=`<div class="wk-sec">同类发现：${escapeHtml(name)}</div><div class="hint">暂时取不到全市场 ETF 清单（数据源限频）；稍后在交易时段重试。</div>`;return;}
    const rows=(d.peers||[]).map(p=>{
      const fee=p.fee==null?'<span class="mut">—</span>':(p.fee*100).toFixed(2)+'%';
      const tv=p.turnover?((p.turnover/1e8).toFixed(1)+'亿'):'—';
      const inc=p.is_incumbent?' <span class="rise">←当前持仓</span>':'';
      return `<tr><td><b>${escapeHtml(p.name||p.code)}</b> <span class="mut">${escapeHtml(p.code)}</span>${inc}</td><td>${fee}</td><td>${tv}</td></tr>`;
    }).join('');
    box.innerHTML=`<div class="wk-sec">同类发现：${escapeHtml(name)} <span class="mut">${escapeHtml(code)}（关键词「${escapeHtml(d.keyword||'-')}」· 全市场约 ${d.count} 只）</span></div>
      <table><thead><tr><th>同类 ETF</th><th>综合费率/年</th><th>近一日成交额</th></tr></thead><tbody>${rows}</tbody></table>
      <div class="hint">${escapeHtml(d.note||'')}。按费率升序、只列流动性最高的几只。<b>这是研究发现、不是动作</b>——想换上更优的，先把它加进「观察池/ETF池」，再走上面的正式准入与构建。</div>`;
  }catch(e){box.innerHTML=`<div class="msg err" style="display:block">请求失败：${escapeHtml(String(e))}</div>`;}
}
async function introduceStrategicCandidate(role,code){
  const okIntro=await confirmDialog({
    title:'引入战略角色',
    body:`<div>确认将 <b>${escapeHtml(String(code))}</b> 引入战略角色 <b>${escapeHtml(String(role))}</b>？</div>
      <div class="hint">引入后仍需重新构建模型组合才会改变目标权重。</div>`,
    confirmText:'确认引入'});
  if(!okIntro)return;
  try{
    const d=await fetch('/api/strategic/roles/introduce',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({role,code})}).then(r=>r.json());
    if(!d.ok)throw new Error(d.error||'引入失败');
    await loadIncumbents(true,true);
    flash('✓ 候选已引入对应战略角色；请重新构建模型组合。');
  }catch(e){flash('引入候选失败：'+escapeHtml(String(e.message||e)),'err');}
}
function marketByCode(){
  const m={}; (LAST_MARKET_ITEMS||[]).forEach(x=>{m[String(x.code)]=x;}); return m;
}
function costBasisByCode(){
  // 平均成本法：买入累计「金额+费」并更新均价，卖出按当前均价减持；返回每个 code 的 {avgCost, execShares}。
  // 关键：成本基随「持有份额」缩放（见 portfolioValueRows），不再直接用执行记录净额——
  // 这样重复/补录的执行记录、或在“编辑设置”里手填的持仓，都不会把成本重复计两遍导致假浮亏。
  const acc={};
  (LAST_EXECUTIONS||[]).slice().reverse().forEach(rec=>{   // load_executions 默认新在前，这里转成旧→新
    (rec.items||[]).forEach(i=>{
      const status=String(i.status||'');
      if(!status.includes('执行') || status.includes('未执行'))return;
      const code=String(i.code||''); if(!code)return;
      const side=String(i.side||'buy').toLowerCase();
      const amount=Number(i.amount||0), fee=Number(i.fee||0), shares=Number(i.shares||0);
      const a=acc[code]||(acc[code]={cost:0,shares:0});
      if(side==='sell'){
        const avg=a.shares>0?a.cost/a.shares:0;
        a.shares-=shares; a.cost-=avg*shares;
        if(a.shares<1e-9){a.shares=0;a.cost=0;}
      }else{
        a.cost+=amount+fee; a.shares+=shares;
      }
    });
  });
  const out={};
  for(const code in acc){
    const a=acc[code];
    out[code]={execShares:a.shares, avgCost:a.shares>1e-9?a.cost/a.shares:null};
  }
  return out;
}
function portfolioValueRows(){
  const cfg=CURRENT_CONFIG||{}, prices=marketByCode(), basis=costBasisByCode();
  return (cfg.holdings||[]).map(h=>{
    const code=String(h.code);
    const p=prices[code]||{};
    const last=Number(p.live_last||p.last||0);
    const price_source=p.live_last!=null?'实时快照':'日K收盘';
    const shares=Number(h.shares||0);
    const value=last>0?shares*last:0;
    const b=basis[code]||{};
    const avg=(b.avgCost!=null&&shares>0)?b.avgCost:null;        // 均价（含费）
    const cost=avg!=null?avg*shares:null;                        // 成本基 = 均价 × 当前持有份额
    const pnl=(cost!=null&&value>0)?value-cost:null;             // 无买入记录→成本未知，不编造
    const mismatch=avg!=null && b.execShares!=null && Math.abs(Number(b.execShares)-shares)>1e-6;
    return {...h,last,price_source,as_of:p.as_of,value,cost,pnl,
            pnl_pct:(cost!=null&&cost!==0&&pnl!=null)?pnl/cost:null,
            costEstimated:mismatch};
  });
}
function drawPortfolioAllocation(){
  const el=$('#portfolioAllocationChart'); if(!el||!window.echarts||!CURRENT_CONFIG)return;
  const rows=portfolioValueRows().filter(r=>r.value>0);
  const cash=Number((CURRENT_CONFIG||{}).cash||0);
  const data=[...rows.map(r=>({name:r.name||r.code,value:Number(r.value.toFixed(2))}))];
  if(cash>0)data.push({name:'现金',value:Number(cash.toFixed(2))});
  disposeChart(el);   // 防止重复 init 叠加旧实例
  const chart=initChart(el);
  chart.setOption({
    animation:false,
    tooltip:{trigger:'item',formatter:p=>`${p.name}<br>${fmtMoney(p.value)} · ${p.percent}%`},
    // 用环外标签直接标“名称 + 占比”，不再加图例（避免两套标签重叠）
    series:[{type:'pie',radius:['40%','60%'],center:['50%','50%'],avoidLabelOverlap:true,minShowLabelAngle:2,
      label:{formatter:'{b}\n{d}%',fontSize:11,color:'#374151',lineHeight:14},
      labelLine:{length:8,length2:8},
      emphasis:{scaleSize:6},data}]
  });
}
// 关键数字卡 + 合并持仓明细表（持有→盈亏→配置 一行打通）
function renderPortfolioPnL(){
  if(!CURRENT_CONFIG)return;
  const summary=$('#portfolioSummary'), box=$('#portfolioHoldings');
  if(!box)return;
  const cash=Number((CURRENT_CONFIG||{}).cash||0);
  const hs=(CURRENT_CONFIG.holdings||[]);
  if(!hs.length){
    if(summary)summary.innerHTML=`<div>组合总值<b>${fmtMoney(cash)}</b></div><div>现金<b>${fmtMoney(cash)}</b></div><div>持仓市值<b>¥0</b></div><div>浮动盈亏<b>-</b></div>`;
    box.innerHTML='<div class="mut">还没有持仓。先用 [添加现金] 录入初始资金，再用 [调仓 → 手动加一行] 登记你的第一笔买入。</div>';
    drawPortfolioAllocation();return;
  }
  const allRows=portfolioValueRows();
  const totalValue=allRows.reduce((a,r)=>a+(r.value||0),0)+cash;
  const rows=allRows.filter(r=>Number(r.shares||0)>0 || Number(r.target_weight||0)>0);
  const haveMarket=LAST_MARKET_ITEMS.length>0;
  const body=rows.map(r=>{
    const cur=(haveMarket&&totalValue>0)?(Number(r.value||0)/totalValue):null;
    const tgt=Number(r.target_weight||0);
    const dev=cur!=null?(cur-tgt):null;
    const pnlCell=!haveMarket?'<span class="mut">等待行情</span>'
      :(r.cost!=null?`${r.pnl>=0?'+':''}${fmtMoney(r.pnl)}${r.pnl_pct!=null?` / ${(r.pnl_pct*100).toFixed(2)}%`:''}${r.costEstimated?' <span class="mut" title="成交记录份额与当前持仓不一致，成本按均价估算">⚠</span>':''}`:'<span class="mut">成本未知</span>');
    const pnlClass=(haveMarket&&r.pnl>0)?'rise':((haveMarket&&r.pnl<0)?'fall':'mut');
    return `<tr><td><b>${escapeHtml(r.name||'')}</b> <span class="mut">${r.code}</span></td>
      <td>${Number(r.shares||0).toLocaleString()}</td>
      <td>${(haveMarket&&r.last)?Number(r.last).toFixed(3):'-'}</td>
      <td>${(haveMarket&&r.value)?fmtMoney(r.value):'-'}</td>
      <td class="${pnlClass}">${pnlCell}</td>
      <td>${cur!=null?fmtPct(cur):'-'}</td>
      <td>${fmtPct(tgt)}</td>
      <td class="${dev!=null?(dev>0.03?'up':(dev<-0.03?'down':'mut')):'mut'}">${dev!=null?`${dev>=0?'+':''}${(dev*100).toFixed(1)}pp`:'-'}</td></tr>`;
  }).join('');
  const heldRows=rows.filter(r=>Number(r.shares||0)>0);
  const priced=heldRows.filter(r=>r.cost!=null);
  const totalCost=priced.reduce((a,r)=>a+r.cost,0);
  const totalPnl=priced.reduce((a,r)=>a+(r.pnl||0),0);
  const anyUnknown=heldRows.some(r=>r.cost==null), anyEst=heldRows.some(r=>r.costEstimated);
  const pnlNote=(haveMarket&&(anyUnknown||anyEst))?`<span class="mut" style="font-weight:400;font-size:11px"> （${anyUnknown?'部分无成交记录未计入；':''}${anyEst?'⚠含估算':''}）</span>`:'';
  if(summary){
    summary.innerHTML=haveMarket?`
      <div>组合总值<b>${fmtMoney(totalValue)}</b></div>
      <div>持仓市值<b>${fmtMoney(totalValue-cash)}</b></div>
      <div>现金<b>${fmtMoney(cash)}</b></div>
      <div>持仓成本<b>${fmtMoney(totalCost)}</b></div>
      <div>浮动盈亏<b class="${totalPnl>=0?'rise':'fall'}">${totalPnl>=0?'+':''}${fmtMoney(totalPnl)}</b>${pnlNote}</div>`
      :`<div>组合总值<b>${fmtMoney(totalValue||cash)}</b></div><div>现金<b>${fmtMoney(cash)}</b></div><div>持仓市值<b>等待行情</b></div><div>浮动盈亏<b>-</b></div>`;
  }
  box.innerHTML=`<table class="holdingsTable"><thead><tr><th>ETF</th><th>份额</th><th>现价</th><th>市值</th><th>浮动盈亏</th><th>当前权重</th><th>目标权重</th><th>偏离</th></tr></thead><tbody>${body}</tbody></table>
    <div class="hint">组合估值优先用实时快照价，缺失则回退日 K 收盘；周报与信号仍按日 K 计算。
      <button class="ghost chipbtn" onclick="refreshRealtimePrices()">获取实时快照价</button>
    </div>`;
  drawPortfolioAllocation();
}
async function refreshRealtimePrices(){
  const btn=event&&event.target; const old=btn&&btn.textContent;
  if(btn){btn.disabled=true;btn.textContent='获取中…';}
  try{
    const holdings=(CURRENT_CONFIG&&CURRENT_CONFIG.holdings)||[];
    const codes=holdings.map(h=>h.code).join(',');
    const q=codes?('?codes='+encodeURIComponent(codes)):'';
    const d=await fetch('/api/etf/spot'+q).then(r=>r.json());
    if(!d.ok)throw new Error(d.error||'spot failed');
    mergeSpotPrices(d.items||[]);
    renderPortfolioPreview();
    renderPortfolioPnL();
    flash('✓ 实时快照价已刷新');
  }catch(e){
    flash('实时快照价刷新失败，请稍后重试','err');
  }finally{
    if(btn){btn.disabled=false;btn.textContent=old||'获取实时快照价';}
  }
}
function mergeSpotPrices(items){
  const byCode={}; (items||[]).forEach(x=>{byCode[String(x.code)]=x;});
  const cfg=(CURRENT_CONFIG&&CURRENT_CONFIG.holdings)||[];
  if(!LAST_MARKET_ITEMS.length){
    LAST_MARKET_ITEMS=cfg.map(h=>({code:h.code,name:h.name,last:null,as_of:null,series:[]}));
  }
  const existing=new Set(LAST_MARKET_ITEMS.map(x=>String(x.code)));
  cfg.forEach(h=>{if(!existing.has(String(h.code)))LAST_MARKET_ITEMS.push({code:h.code,name:h.name,last:null,as_of:null,series:[]});});
  LAST_MARKET_ITEMS.forEach(x=>{
    const spot=byCode[String(x.code)];
    if(spot&&spot.last_price!=null){
      x.live_last=Number(spot.last_price);
      x.live_source='spot';
      x.spot_updated_at=new Date().toISOString();
    }
  });
}

/* ---------- 弹窗开合（调仓 / 编辑设置） ---------- */
function openSettings(){ $('#settingsModal').hidden=false; }
function closeSettings(){ $('#settingsModal').hidden=true; $('#cfgmsg').className='msg'; $('#cfgmsg').textContent=''; }
async function openRebalanceSettings(){
  $('#rebalSettingsModal').hidden=false;
  const msg=$('#rebalCfgMsg'); if(msg){ msg.className='msg'; msg.textContent=''; msg.style.display=''; }
  $('#rebalPolicyBody').innerHTML='<div class="hint"><span class="spin"></span>加载再平衡策略…</div>';
  try{
    const p=await fetch('/api/rebalance-policy').then(r=>r.json());
    if(!p||!p.ok) throw new Error('加载失败');
    renderRebalancePolicy(p);
  }catch(e){ $('#rebalPolicyBody').innerHTML='<div class="msg err" style="display:block">加载失败：'+escapeHtml(String(e.message||e))+'</div>'; }
}
function closeRebalanceSettings(){ $('#rebalSettingsModal').hidden=true; }
function renderRebalancePolicy(p){
  const body=$('#rebalPolicyBody'); if(!body)return;
  const opts=(p.options||[]).map(o=>`<option value="${o.value}"${o.value===p.check_frequency?' selected':''}>${escapeHtml(o.label)}${o.gap_days>0?`（最短间隔约 ${o.gap_days} 天）`:'（每次周报都可触发）'}</option>`).join('');
  const ds=p.days_since_last_rebalance;
  body.innerHTML=`
    <div class="subcard">
      <h3>当前再平衡规则（5/25 法则）</h3>
      <div class="hint">某只 ETF 偏离目标 <b>≥${Math.round(p.abs_threshold_pp)} 个百分点</b> 或 <b>相对偏离 ≥${Math.round(p.rel_threshold*100)}%</b> 才触发买卖（谁先到算谁）。单笔 ≥¥${Number(p.min_trade_amount).toLocaleString()}、单周 ≤¥${Number(p.max_weekly_trade_amount).toLocaleString()}；行情缺失或过旧则本周不动手。</div>
    </div>
    <div class="profilegrid">
      <div class="field"><label>再平衡频率（两次再平衡最短间隔）</label>
        <select id="rebalFreqSel">${opts}</select></div>
    </div>
    <div class="hint">${ds==null?'还没有成交记录，频率限制暂不生效。':`距上次成交 <b>${ds}</b> 天。`}</div>
    <div class="hint mut">· <b>频率越低</b>（双周/月/季）＝ 交易越少、越省心，但两次检查之间对行情的反应越慢。<b>越担心波动变大，越该选「每周」</b>（响应最快）。</div>
    <div class="hint mut">· <b>崩盘熔断</b>：无论选哪档，只要任一品种偏离 <b>≥${Math.round(p.circuit_breaker_pp)} 个百分点</b>，仍会无视频率强制触发——降低频率不会让你在极端行情里失去保护。</div>
    <div class="hint mut">· 只改“多久检查一次”，不改 5/25 阈值，也不动你的持仓或目标权重。建仓阶段以“用新钱补低配”为主，这个频率主要在建满仓后起作用。</div>`;
}
async function saveRebalanceFrequency(){
  const sel=$('#rebalFreqSel'); const msg=$('#rebalCfgMsg'); if(!sel||!msg)return;
  try{
    const r=await fetch('/api/rebalance-frequency',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({frequency:sel.value})});
    const d=await r.json();
    if(!r.ok||!d.ok) throw new Error((d.errors||[d.error]).filter(Boolean).join('；')||'保存失败');
    msg.className='msg ok'; msg.style.display='block'; msg.textContent=`已设为「${d.label}」。下次生成周报即按此频率，可随波动情况随时再改。`;
    flash(`再平衡频率已设为「${d.label}」`);
  }catch(e){ msg.className='msg err'; msg.style.display='block'; msg.textContent='保存失败：'+escapeHtml(String(e.message||e)); }
}
async function openRebalance(){
  const card=$('#rebalanceModal'); card.hidden=false;   // 先弹窗，避免点击后无反馈
  setRebalanceStep(1);
  setRebalMeta('<span class="spin"></span> 正在后台审查：载入本周建议、校验决策周期与执行质量…','ok');
  await loadExecutions(true);
  showRebalanceReviewStatus();   // 把后台审查结果常驻在弹窗顶部（#rebalmeta 不随步骤切换隐藏）
  renderRebalanceSource();
  refreshRebalancePreview();
}
function setRebalMeta(html,cls){const m=$('#rebalmeta'); if(!m)return; m.className='msg '+(cls||'ok'); m.innerHTML=html;}
function showRebalanceReviewStatus(){
  const version=CURRENT_CYCLE&&CURRENT_CYCLE.version_status;
  const stale=version&&version.status==='stale';
  const n=(CURRENT_SUGGESTIONS||[]).length, b=(BLOCKED_SUGGESTIONS||[]).length;
  if(stale){ setRebalMeta('⚠ 后台审查：当前决策周期已失效（生成建议后配置发生变化），请重新生成本周信号后再调仓。','err'); return; }
  const cyc=(CURRENT_CYCLE&&CURRENT_CYCLE.id)?(' '+escapeHtml(CURRENT_CYCLE.id)):'';
  setRebalMeta(`✓ 后台审查通过：决策周期${cyc}有效 · 可执行建议 <b>${n}</b> 条${b?` · 因执行质量暂缓 <b>${b}</b> 条`:''}`,'ok');
}
function closeRebalance(){
  $('#rebalanceModal').hidden=true;
  $('#rebalmsg').className='msg'; $('#rebalmsg').textContent='';
  const m=$('#rebalmeta'); if(m){m.className='hint'; m.textContent='';}
}
function setRebalanceStep(n){
  if(n===3){
    // 进入预览前先把步骤2的填写错误拦下来（错误就地标红，不让用户到最后一步才被弹回）
    const errs=validateRebalanceRows(true);
    if(errs.length){
      flash('请先修正成交填写：'+errs[0],'err');
      const bad=document.querySelector('#rebalform .badfield'); if(bad)bad.focus();
      return;
    }
  }
  [1,2,3].forEach(i=>{
    const p=$(`#rebalStep${i}`), b=$(`#wiz${i}`);
    if(p)p.hidden=i!==n;
    if(b)b.classList.toggle('active',i===n);
  });
  if(n===3){refreshRebalancePreview();updateRebalanceConfirmGate();}
}
// 步骤2 即时校验：对"填了任何内容"的行检查 代码/份额/价格/整手；空行不算错（视为未登记）。
// decorate=true 时就地标红问题字段并在行内给原因。返回错误文案数组（空=通过）。
function validateRebalanceRows(decorate){
  const errs=[];
  document.querySelectorAll('#rebalform .execrow').forEach(row=>{
    const get=k=>row.querySelector(`[data-k=${k}]`);
    const code=(get('code')&&get('code').value||'').trim();
    const shares=Number((get('shares')&&get('shares').value)||0);
    const price=Number((get('price')&&get('price').value)||0);
    const side=(get('side')&&get('side').value)||'buy';
    const touched=code||shares||price;
    const rowErrs=[]; const badKeys=[];
    if(touched){
      if(!code){rowErrs.push('ETF 代码不能为空');badKeys.push('code');}
      if(!(shares>0)){rowErrs.push('成交份额须为正数');badKeys.push('shares');}
      else if(side!=='sell'&&shares%100!==0){rowErrs.push(`买入份额 ${shares} 不是 100 的整数倍（场内 ETF 一手=100份）`);badKeys.push('shares');}
      if(!(price>0)){rowErrs.push('成交均价须大于 0');badKeys.push('price');}
    }
    if(decorate){
      let err=row.querySelector('.rowerr');
      row.querySelectorAll('.badfield').forEach(x=>x.classList.remove('badfield'));
      if(rowErrs.length){
        badKeys.forEach(k=>{const el=get(k);if(el)el.classList.add('badfield');});
        if(!err){err=document.createElement('div');err.className='rowerr';row.appendChild(err);}
        err.textContent=rowErrs.join('；');
      }else if(err){err.remove();}
    }
    rowErrs.forEach(t=>errs.push(`${code||'某行'}：${t}`));
  });
  const next=$('#rebalNextBtn');
  if(next){next.disabled=errs.length>0;next.title=errs.length?'请先修正标红的填写错误':'';}
  return errs;
}
// 第3步确认门：交易前确认清单未勾全 → 禁用"确认完成调仓"（清单只在带入本周建议时出现）
function updateRebalanceConfirmGate(){
  const btn=$('#rebalConfirmBtn'); if(!btn)return;
  const checks=[...document.querySelectorAll('#tradeChecklist [data-confirm]')];
  const blocked=checks.length>0&&checks.some(x=>!x.checked);
  btn.disabled=blocked;
  btn.title=blocked?'请先逐项勾选上方「交易前确认」清单':'';
  const hint=$('#rebalConfirmHint'); if(hint)hint.hidden=!blocked;
}
function renderRebalanceSource(){
  const count=(CURRENT_SUGGESTIONS||[]).length;
  const blocked=(BLOCKED_SUGGESTIONS||[]).length;
  const cycle=CURRENT_CYCLE&&CURRENT_CYCLE.id?`决策周期 ${CURRENT_CYCLE.id}：`:'';
  const version=CURRENT_CYCLE&&CURRENT_CYCLE.version_status;
  const stale=version&&version.status==='stale';
  const staleHtml=stale?`<div class="wk-alarm"><b>当前周期已失效</b>：生成建议后配置发生了变化，请重新生成本周信号再执行。<button class="ghost chipbtn" onclick="closeRebalance();runSignals()">重新生成本周信号</button></div>`:'';
  const blockedHtml=blocked?`<div class="wk-alarm"><b>${blocked} 条建议因当前交易质量暂缓</b>${BLOCKED_SUGGESTIONS.map(x=>`<div>${escapeHtml(x.name||x.code)}：${escapeHtml((x.execution_quality_notes||[]).join('；')||'当前不宜执行')}</div>`).join('')}</div>`:'';
  const actionsHtml=count?`<div class="decisionActions">${CURRENT_SUGGESTIONS.map(x=>`<div class="act"><b>${escapeHtml(x.name||x.code)}</b> <span class="mut">${x.side==='sell'?'卖出':'买入'} · ${escapeHtml(x.source||'')}</span><span class="cardacts"><button class="ghost chipbtn" onclick="decideSuggestion('${escapeHtml(x.source||'rebalance')}','${escapeHtml(x.code||'')}','${escapeHtml(x.side||'buy')}','skipped')">跳过本周期</button><button class="ghost chipbtn" onclick="decideSuggestion('${escapeHtml(x.source||'rebalance')}','${escapeHtml(x.code||'')}','${escapeHtml(x.side||'buy')}','rejected')">否决建议</button></span></div>`).join('')}</div>`:'';
  const decided=(DECIDED_SUGGESTIONS||[]).filter(x=>x.action_status==='skipped'||x.action_status==='rejected');
  const decidedHtml=decided.length?`<div class="subcard"><h3>已处理建议</h3>${decided.map(x=>`<div class="act"><b>${escapeHtml(x.name||x.code)}</b> · ${x.action_status==='rejected'?'已否决':'已跳过'}${x.decision_reason?` · ${escapeHtml(x.decision_reason)}`:''} <button class="ghost chipbtn" onclick="decideSuggestion('${escapeHtml(x.source||'rebalance')}','${escapeHtml(x.code||'')}','${escapeHtml(x.side||'buy')}','pending')">恢复</button></div>`).join('')}</div>`:'';
  $('#rebalsuggest').innerHTML=staleHtml+(count
    ? `${escapeHtml(cycle)}检测到 <b>${count}</b> 条尚未完成且当前可执行的建议。请改成券商真实成交。`
    : `${escapeHtml(cycle)}当前没有可带入的建议。可手动补录真实成交。`)+actionsHtml+blockedHtml+decidedHtml;
}
function useSuggestedRebalance(){
  if(CURRENT_CYCLE&&CURRENT_CYCLE.version_status&&CURRENT_CYCLE.version_status.status==='stale'){
    flash('当前周期配置已变化，请先重新生成本周信号','err'); return;
  }
  renderRebalanceFlow(CURRENT_SUGGESTIONS||[]);
  setRebalanceStep(2);
}
function useManualRebalance(){
  renderRebalanceFlow([]);
  setRebalanceStep(2);
}
// 趋势减仓建议 → 一键带入调仓登记：代码/卖出/估算份额（金额÷现价、取整百、不超持有）预填进表单。
// 只是预填登记表单，不绕任何闸门；实际成交仍由用户在券商完成后回来改成真实数字。
async function trendDeriskFill(code){
  const s=LIVE_SIGNALS||{};
  const a=(s.trend_alerts||[]).find(x=>String(x.code)===String(code));
  if(!a||!a.actionable){flash('该建议当前不可执行','err');return;}
  const sig=(s.signals||s.per||{})[String(code)]||{};
  const price=Number(sig.last||0);
  const hold=((CURRENT_CONFIG||{}).holdings||[]).find(h=>String(h.code)===String(code))||{};
  const held=Number(hold.shares||0);
  let shares=price>0?Math.round((Number(a.derisk_amount||0)/price)/100)*100:0;
  if(held>0&&shares>held)shares=held;
  await openRebalance();   // 等后台审查完成（其内部的 loadExecutions 会重渲染表单，先等它跑完再预填）
  renderRebalanceFlow([{name:a.name,code:String(code),side:'sell',source:'trend_derisk',
    suggested_amount:Number(a.derisk_amount||0),suggested_shares:shares||'',suggested_price:price||''}]);
  setRebalanceStep(2);
  flash('已按趋势减仓建议预填（份额为估算，请改成你的真实成交）');
}

/* ---------- 调仓流程（带入本周建议 → 可改 → 预览 → 一次确认） ---------- */
const COMMISSION_RATE=0.0003, COMMISSION_MIN=5;   // 场内ETF佣金：万3/最低5元（与后端 reports.estimate_commission 一致）
function estCommission(amount){const a=Math.abs(Number(amount)||0);return a<=0?0:Math.max(COMMISSION_MIN, Math.round(a*COMMISSION_RATE*100)/100);}
function execRowHtml(x,i){
  x=x||{};
  const qualityNote=(x.execution_quality_notes||[]).length
    ? `<br><span class="hint ${x.execution_quality==='warn'?'down':'mut'}">当前执行检查：${escapeHtml(x.execution_quality_notes.join('；'))}</span>`:'';
  const sShares=(x.suggested_shares!=null)?x.suggested_shares:'';
  const sPrice=(x.suggested_price!=null)?x.suggested_price:'';
  const sAmount=(sShares!==''&&sPrice!=='')?Math.round(Number(sShares)*Number(sPrice)*100)/100
               :(x.suggested_amount?Math.round(x.suggested_amount):'');
  const sFee=(sAmount!=='')?estCommission(sAmount):0;
  return `<div class="execrow" data-i="${i}">
    <span class="execname"><b>${escapeHtml(x.name||'手动登记')}</b> <span class="mut">${x.code||''}</span>${x.source?`<br><span class="hint">${x.source} · 建议${x.side==='sell'?'卖出':'买入'} ¥${Number(x.suggested_amount||0).toLocaleString()}${sShares!==''?` · ${Number(sShares).toLocaleString()}份`:''}</span>`:''}${qualityNote}</span>
    <span class="execfield"><label>ETF代码</label><input data-k="code" value="${x.code||''}" placeholder="代码"></span>
    <span class="execfield"><label>方向</label><select data-k="side" ${x.source?'disabled title="方向来自本周建议，不可改；计划外操作请删掉本行、用「+ 手动加一行」登记"':'title="买入=份额增加、现金减少；卖出=份额减少、现金增加。在券商卖出后补录，务必选「卖出」"'}><option value="buy"${(x.side||'buy')!=='sell'?' selected':''}>买入</option><option value="sell"${(x.side||'buy')==='sell'?' selected':''}>卖出</option></select></span>
    <span class="execfield"><label>成交份额</label><input data-k="shares" type="number" step="100" min="0" value="${sShares}" placeholder="份额（100整数倍）" title="场内 ETF 一手=100份，买入须为 100 的整数倍；已带入本周建议份额，请改成真实成交"></span>
    <span class="execfield"><label>成交均价</label><input data-k="price" type="number" value="${sPrice}" placeholder="成交价" title="已带入最新价；请改成你的真实成交均价"></span>
    <span class="execfield"><label>成交金额 <span class="mut">·自动</span></label><input data-k="amount" type="number" value="${sAmount}" readonly tabindex="-1" title="成交金额 = 成交份额 × 成交均价（自动计算，不可改）"></span>
    <span class="execfield"><label>手续费 <span class="mut">·自动</span></label><input data-k="fee" type="number" value="${sFee}" readonly tabindex="-1" title="按场内ETF佣金 万3/最低5元 自动计算（不可改）"></span>
    <span class="execfield"><label>原因</label><input data-k="reason" value="${x.source==='first_funding'?'首次试仓':(x.source==='rebalance'?'再平衡':(x.source==='trend_derisk'?'趋势减仓':''))}" placeholder="原因"></span>
    <span class="execfield execdelwrap"><label>&nbsp;</label><button type="button" class="execdel" onclick="removeRebalanceRow(this)" title="移除这一行（没做的成交不要登记）">删除</button></span>
    <input type="hidden" data-k="suggestion_source" value="${x.source||''}">
  </div>`;
}
function renderRebalanceFlow(rows){
  const box=$('#rebalform');
  const ck=$('#rebalChecklistBox');
  if(!rows||!rows.length){
    $('#rebalsuggest').textContent='当前没有本周建议。可点“+ 手动加一行”登记你的实际成交。';
    box.innerHTML=execRowHtml({},0);
    if(ck)ck.innerHTML='';   // 手动补录不出确认清单（与原行为一致）
  }else{
    $('#rebalsuggest').textContent='已带入本周可执行建议，请改成你的实际成交；没做的那条点“删除”移除即可。';
    box.innerHTML=rows.map((x,i)=>execRowHtml(x,i)).join('');
    // 确认清单放在第3步（最终确认旁）：勾选与拦截同屏，不再"确认时才发现要回上一步勾选"
    if(ck)ck.innerHTML=`<div class="checklist" id="tradeChecklist"><b>交易前确认</b>
      <label><input type="checkbox" data-confirm="understand">我理解本次涉及的 ETF 跟踪什么，以及主要风险。</label>
      <label><input type="checkbox" data-confirm="drawdown">我接受买入后短期下跌的可能，不因当天涨跌改变规则。</label>
      <label><input type="checkbox" data-confirm="manual">我知道工具不会自动下单，实际交易由我在券商手动完成。</label>
    </div>`;
  }
  box.oninput=scheduleRebalancePreview;
  if(ck)ck.onchange=updateRebalanceConfirmGate;
  validateRebalanceRows(true);
  updateRebalanceConfirmGate();
}
function addRebalanceRow(){
  const box=$('#rebalform'); if($('#rebalanceModal').hidden)return;
  const tmp=document.createElement('div');
  tmp.innerHTML=execRowHtml({}, box.querySelectorAll('.execrow').length);
  box.appendChild(tmp.firstElementChild);
}
function removeRebalanceRow(btn){
  const row=btn&&btn.closest('.execrow'); if(!row)return;
  row.remove();
  const box=$('#rebalform');
  if(box && !box.querySelectorAll('.execrow').length) addRebalanceRow(); // 删到一行不剩时补个空行，便于继续登记
  scheduleRebalancePreview();
}
function collectRebalanceItems(){
  return [...document.querySelectorAll('#rebalform .execrow')].map(row=>{
    const get=k=>row.querySelector(`[data-k=${k}]`);
    return {
      status:'已执行',   // 登记流程里每条都是真实成交；不做的请删除该行（不再有“执行状态”选项）
      code:get('code')&&get('code').value,
      shares:Number((get('shares')&&get('shares').value)||0),
      price:Number((get('price')&&get('price').value)||0),
      fee:Number((get('fee')&&get('fee').value)||0),
      amount:Number((get('amount')&&get('amount').value)||0),
      reason:(get('reason')&&get('reason').value)||'',
      suggestion_source:(get('suggestion_source')&&get('suggestion_source').value)||'',
      side:(get('side')&&get('side').value)||''
    };
  }).filter(x=>x.code||x.amount||x.shares);
}
let _rebalTimer=null;
function syncRowAmount(row){
  const shares=Number((row.querySelector('[data-k=shares]')||{}).value||0);
  const price=Number((row.querySelector('[data-k=price]')||{}).value||0);
  const amount=row.querySelector('[data-k=amount]');
  const fee=row.querySelector('[data-k=fee]');
  const amt=(shares>0&&price>0)?Math.round(shares*price*100)/100:0;
  if(amount) amount.value=amt?amt.toFixed(2):'';
  if(fee) fee.value=amt?estCommission(amt):0;   // 成交金额、手续费始终由份额×价自动算
}
function scheduleRebalancePreview(e){
  const row=e&&e.target&&e.target.closest?e.target.closest('.execrow'):null;
  if(row && (e.target.dataset.k==='shares'||e.target.dataset.k==='price')) syncRowAmount(row);
  validateRebalanceRows(true);   // 输入即校验：错误当场标红，而不是最后一步才报
  clearTimeout(_rebalTimer);_rebalTimer=setTimeout(refreshRebalancePreview,250);
}
function renderDraftTable(box,dr){
  if(!dr||!dr.changed){box.innerHTML='<div class="mut">暂无成交，持仓不变。</div>';return;}
  const rows=(dr.holdings||[]).filter(h=>h.delta_shares).map(h=>`<tr><td><b>${escapeHtml(h.name||'')}</b> <span class="mut">${h.code}</span></td>
    <td>${Number(h.old_shares).toLocaleString()}</td><td>${Number(h.new_shares).toLocaleString()}</td>
    <td class="${h.delta_shares>0?'up':(h.delta_shares<0?'down':'mut')}">${h.delta_shares>0?'+':''}${Number(h.delta_shares).toLocaleString()}</td></tr>`).join('');
  const warn=(dr.warnings||[]).length?`<div class="hint">注意：${dr.warnings.map(escapeHtml).join('；')}</div>`:'';
  box.innerHTML=`<table><thead><tr><th>ETF</th><th>原份额</th><th>成交后</th><th>变化</th></tr></thead><tbody>${rows}</tbody></table>
    <div class="act">现金：¥${Number(dr.cash_old).toLocaleString()} → <b>¥${Number(dr.cash_new).toLocaleString()}</b></div>${warn}`;
}
async function refreshRebalancePreview(){
  const box=$('#rebalpreview'); if(!box||$('#rebalanceModal').hidden)return;
  const items=collectRebalanceItems();
  const live=items.filter(x=>(x.status||'').includes('执行') && !(x.status||'').includes('未执行'));
  if(!live.length){box.innerHTML='<div class="mut">还没有填写成交，持仓不变。</div>';return;}
  try{
    const r=await fetch('/api/portfolio/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({items:live})});
    if(r.status===404){box.innerHTML='<div class="mut">请重启驾驶舱（python3 engine/app.py）以启用调仓预览。</div>';return;}
    const d=await r.json();
    if(!d.ok){box.innerHTML='<div class="mut">预览失败。</div>';return;}
    renderDraftTable(box,d.draft);
  }catch(e){ box.innerHTML='<div class="mut">预览失败（后端未响应）。</div>'; }
}
async function afterRebalanceReload(){
  await loadConfig();        // 刷新 CURRENT_CONFIG + 编辑表单 + 持仓总览
  await loadExecutions();    // 刷新只读调仓记录
  await loadMonthlyReview();
}
function _localToday(){const d=new Date();const p=n=>String(n).padStart(2,'0');return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}`;}
function _daysBetween(aStr,bStr){const a=new Date(aStr+'T00:00:00'),b=new Date(bStr+'T00:00:00');return Math.round(Math.abs(a-b)/86400000);}
function recentDuplicateItems(items, executions, todayStr, windowDays){
  // 软查重：找出"近 windowDays 天内已登记过的相同成交"（同 code+方向+份额+金额）。返回命中明细，不硬拦。
  const fp=i=>`${String(i.code||'')}|${String(i.side||'buy').toLowerCase()}|${Number(i.shares||0)}|${Math.round(Number(i.amount||0))}`;
  const live=(items||[]).filter(x=>x.code && (x.status||'').includes('执行') && !(x.status||'').includes('未执行'));
  if(!live.length)return [];
  const seen={};
  (executions||[]).forEach(rec=>{
    const day=String(rec.created_at||rec.id||'').slice(0,10);
    if(!day || _daysBetween(day,todayStr)>windowDays)return;
    (rec.items||[]).forEach(i=>{
      const st=String(i.status||''); if(!st.includes('执行')||st.includes('未执行'))return;
      seen[fp(i)]=day;
    });
  });
  const out=[];
  live.forEach(x=>{ if(seen[fp(x)]) out.push({code:x.code,shares:Number(x.shares||0),when:seen[fp(x)]}); });
  return out;
}
async function confirmRebalance(){
  const msg=$('#rebalmsg'); msg.className='msg';
  const items=collectRebalanceItems();
  if(!items.length){msg.className='msg err';msg.textContent='没有可登记的成交，请先填写或带入建议；没做的那条点“删除”移除即可。';return;}
  // M12：整手校验（此前 step=100 只是微调按钮的步长，手输 250 份/负数会静默入账）。
  // 买入须 100 的整数倍；卖出允许清零股（券商支持卖出零股），但负数/0 一律拦。
  const lotErrs=[];
  items.forEach(x=>{
    const sh=Number(x.shares||0);
    if(!(sh>0)) lotErrs.push(`${x.code||'某行'}：成交份额须为正数`);
    else if(String(x.side||'buy').toLowerCase()!=='sell' && sh%100!==0) lotErrs.push(`${x.code}：买入份额 ${sh} 不是 100 的整数倍（场内 ETF 一手=100份）`);
  });
  if(lotErrs.length){msg.className='msg err';msg.textContent='请先修正：'+lotErrs.join('；')+'。';return;}
  const liveItems=items;   // 登记流程里每条都视为已执行；没做的请删除该行
  const checks=[...document.querySelectorAll('#tradeChecklist [data-confirm]')];
  if(checks.length && checks.some(x=>!x.checked)){
    msg.className='msg err';
    msg.textContent='确认前请先逐项勾选上方「交易前确认」清单；还没想清楚的，可返回上一步点该行“删除”先不登记。';
    updateRebalanceConfirmGate();
    return;
  }
  const _dups=recentDuplicateItems(liveItems, LAST_EXECUTIONS, _localToday(), 7);
  if(_dups.length){
    msg.className='msg err';
    msg.textContent='⚠ 近 7 天内似乎已登记过相同成交：'+_dups.map(d=>`${d.code} ${d.shares}份(${d.when})`).join('、')+'。若不是新的一笔，请勿重复登记（会让持仓成本/浮亏算错）。';
  }
  const _dupHtml=_dups.length?`<div class="msg err" style="display:block">⚠ 近 7 天内似乎已登记过相同成交：${_dups.map(d=>`${escapeHtml(String(d.code))} ${d.shares}份(${escapeHtml(String(d.when))})`).join('、')}。重复登记会让持仓成本/浮动盈亏算错。</div>`:'';
  const _sumRows=liveItems.map(x=>`<tr><td><b>${escapeHtml(String(x.code||''))}</b></td><td>${x.side==='sell'?'卖出':'买入'}</td><td>${Number(x.shares||0).toLocaleString()}份</td><td>${x.price?('@'+x.price):'-'}</td><td>¥${Number(x.amount||0).toLocaleString()}</td><td>费¥${Number(x.fee||0).toLocaleString()}</td></tr>`).join('');
  const okGo=await confirmDialog({
    title:'确认完成本次调仓？',
    danger:_dups.length>0,
    body:`${_dupHtml}<table class="confirmTable"><thead><tr><th>代码</th><th>方向</th><th>份额</th><th>均价</th><th>金额</th><th>手续费</th></tr></thead><tbody>${_sumRows}</tbody></table>
      <div class="hint">将 ①登记执行记录 ②按成交后持仓更新本地组合记录。<b>工具不会替你下单</b>——请确认以上都是已在券商完成的真实成交。</div>`,
    confirmText:'确认登记',cancelText:'返回检查'});
  if(!okGo) return;
  const btns=[...document.querySelectorAll('#rebalanceModal button')];
  btns.forEach(b=>b.disabled=true);
  msg.className='msg ok'; msg.innerHTML='<span class="spin"></span> 正在后台审查（决策周期 · 执行质量 · 成交后持仓校验）并登记本次调仓…';
  try{
    // 后端单一事务：登记执行记录 + 更新持仓；失败不留下半完成状态
    const er=await fetch('/api/decision-cycle/execute',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({report_id:(CURRENT_CYCLE&&CURRENT_CYCLE.id)||CURRENT_REPORT_ID,note:$('#rebalnote').value,items:liveItems})});
    const ed=await er.json();
    if(!ed.ok){msg.className='msg err';msg.textContent=ed.error||'调仓保存失败（未修改持仓）';return;}
    $('#rebalnote').value='';
    closeRebalance();
    await afterRebalanceReload();
    flash('✓ 调仓已完成：执行已登记，持仓已更新（见上方持仓总览）。');
  }finally{ btns.forEach(b=>b.disabled=false); }
}

async function decideSuggestion(source,code,side,status){
  let reason='';
  if(status!=='pending'){
    reason=prompt(status==='rejected'?'简要记录否决原因：':'简要记录本周期跳过原因：','')||'';
  }
  const r=await fetch('/api/decision-cycle/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
    cycle_id:CURRENT_CYCLE&&CURRENT_CYCLE.id,source,code,side,status,reason
  })});
  const d=await r.json();
  if(!d.ok){flash(d.error||'保存决策失败','err');return;}
  await loadExecutions(true);
  renderRebalanceSource();
  flash(status==='pending'?'建议已恢复':(status==='rejected'?'建议已否决并留痕':'建议已跳过并留痕'));
}

/* ---------- 调仓记录（只读） ---------- */
async function loadExecutions(recheck){
  const r=await fetch('/api/executions'+(recheck?'?recheck=1':'')); const d=await r.json();
  CURRENT_CYCLE=d.cycle||null;
  if(CURRENT_CYCLE&&CURRENT_CYCLE.id) CURRENT_REPORT_ID=CURRENT_CYCLE.id;
  CURRENT_SUGGESTIONS=d.suggestions||[];
  BLOCKED_SUGGESTIONS=d.blocked_suggestions||[];
  DECIDED_SUGGESTIONS=d.decided_suggestions||[];
  LAST_EXECUTIONS=d.executions||[];
  const chip=$('#chipActions');
  if(chip){
    const stale=CURRENT_CYCLE&&CURRENT_CYCLE.version_status&&CURRENT_CYCLE.version_status.status==='stale';
    chip.textContent=stale?'需刷新':CURRENT_SUGGESTIONS.length;
    chip.title=stale?'配置已变化，请重新生成本周信号':(BLOCKED_SUGGESTIONS.length?`${BLOCKED_SUGGESTIONS.length} 条建议因当前交易质量被暂缓`:'当前活动决策周期剩余可执行建议');
  }
  renderStaleBanner();   // M9：周期指纹失效（如应用模型组合后）→ 常驻横幅，而非只有 3.6 秒 toast
  renderPortfolioPnL();
  refreshLiveTasks();   // 执行记录变化 → 重算本周任务自动勾选
  if(!$('#rebalanceModal').hidden) renderRebalanceFlow(CURRENT_SUGGESTIONS);
  const rows=LAST_EXECUTIONS;
  const box=$('#exechistory');
  if(!rows.length){box.innerHTML='<div class="hint">还没有调仓记录。用上方 [调仓] 登记你的第一笔成交——会自动更新持仓、并在行情曲线上标注买卖点。</div>';return;}
  box.innerHTML=rows.slice(0,12).map(x=>{
    const items=(x.items||[]).map(i=>`${i.status||'记录'} ${i.code||''} ${i.shares||0}份 ¥${Number(i.amount||0).toLocaleString()}${i.price?` @${i.price}`:''}${i.fee?` 费¥${Number(i.fee).toLocaleString()}`:''}${i.reason?` · ${escapeHtml(i.reason)}`:''}`).join('；');
    return `<div class="act"><b>${x.created_at||x.id}</b><br>${items||'无明细'}${x.note?`<div class="hint">${escapeHtml(x.note)}</div>`:''}</div>`;
  }).join('');
}
function flash(text,kind){
  let t=$('#toast');
  if(!t){t=document.createElement('div');t.id='toast';document.body.appendChild(t);}
  t.className='toast '+(kind||'ok'); t.textContent=text; t.style.opacity='1';
  clearTimeout(t._h); t._h=setTimeout(()=>{t.style.opacity='0';},3600);
}
// 页面内确认层：替代原生 confirm()——能承载结构化内容（新旧权重对比表、成交摘要），样式与工作台一致。
// body 是 HTML（调用方负责对动态值 escapeHtml）；resolve(true)=确认。
function confirmDialog({title='请确认',body='',confirmText='确认',cancelText='取消',danger=false}={}){
  return new Promise(resolve=>{
    const bd=document.createElement('div');
    bd.className='modalBackdrop confirmLayer';
    bd.innerHTML=`<div class="modal confirmBox" role="dialog" aria-modal="true">
      <div class="modalhead"><h2>${escapeHtml(title)}</h2></div>
      <div class="confirmBody">${body}</div>
      <div class="row2"><button class="${danger?'danger':''}" data-cf="ok">${escapeHtml(confirmText)}</button><button class="ghost" data-cf="cancel">${escapeHtml(cancelText)}</button></div>
    </div>`;
    document.body.appendChild(bd);
    const done=v=>{document.removeEventListener('keydown',onKey);bd.remove();resolve(v);};
    const onKey=e=>{if(e.key==='Escape')done(false);};
    bd.querySelector('[data-cf=ok]').onclick=()=>done(true);
    bd.querySelector('[data-cf=cancel]').onclick=()=>done(false);
    bd.addEventListener('click',e=>{if(e.target===bd)done(false);});
    document.addEventListener('keydown',onKey);
    bd.querySelector('[data-cf=cancel]').focus();   // 默认焦点在"取消"，防回车误确认
  });
}

/* ---------- ETF 曲线（ECharts 或 canvas 兜底） ---------- */
function isExecutedMarker(e){
  const status=String((e&&e.status)||'');
  return status.includes('执行') && !status.includes('未执行');
}
function executionSide(e){
  const side=String((e&&e.side)||'').toLowerCase();
  const isSell=side==='sell'||side==='卖出'||String((e&&e.note)||'').includes('卖')||Number((e&&e.amount)||0)<0;
  return isSell?'卖出':'买入';
}
function drawChart(el, item){
  const rows=item.series||[];
  if(!el||!rows.length)return;
  if(window.echarts){
    disposeChart(el);
    const chart=initChart(el);
    const firstDate=rows[0].date, lastDate=rows[rows.length-1].date;
    const markersByDate={};
    const execs=(item.executions||[]).filter(isExecutedMarker).map(e=>{
      if(!e.date||e.date<firstDate||e.date>lastDate)return null;
      const row=rows.find(r=>r.date>=e.date) || rows[rows.length-1];
      const side=executionSide(e);
      const isSell=side==='卖出';
      const marker={
        date:e.date,
        chartDate:row.date,
        side,
        status:e.status||'已执行',
        shares:Number(e.shares||0),
        amount:Number(e.amount||0),
        note:e.note||''
      };
      (markersByDate[row.date]=markersByDate[row.date]||[]).push(marker);
      return {
        name:side,
        coord:[row.date,row.return_pct],
        value:side,
        symbol:'circle',
        symbolSize:6,
        itemStyle:{color:isSell?'#c0392b':'#0a7d4d',borderColor:'#fff',borderWidth:1},
        emphasis:{scale:1.35,itemStyle:{borderColor:'#fff',borderWidth:1}},
        label:{show:false},
        tooltip:{formatter:`${e.date}<br><span style="color:${isSell?'#c0392b':'#0a7d4d'}">${side}</span> · ${e.status||'已执行'}<br>${Number(e.shares||0).toLocaleString()} 份 / ¥${Number(e.amount||0).toLocaleString()}${e.note?'<br>'+escapeHtml(e.note):''}`}
      };
    }).filter(Boolean);
    chart.setOption({
      animation:false,
      grid:{left:42,right:20,top:24,bottom:34},
      tooltip:{
        trigger:'axis',
        formatter:params=>{
          const list=Array.isArray(params)?params:[params];
          const axisValue=(list[0]&&list[0].axisValue)||'';
          const line=list.find(p=>p.seriesType==='line');
          const val=line&&line.data!=null?Number(line.data):null;
          const parts=[`${axisValue}`];
          if(val!=null&&!Number.isNaN(val))parts.push(`涨跌幅：${val.toFixed(2)}%`);
          (markersByDate[axisValue]||[]).forEach(m=>{
            const amt=Math.abs(m.amount);
            const sideColor=m.side==='卖出'?'#c0392b':'#0a7d4d';
            parts.push(`<span style="color:${sideColor}">${m.side}</span> · ${m.status}<br>${m.shares.toLocaleString()} 份 / ¥${amt.toLocaleString()}${m.note?'<br>'+escapeHtml(m.note):''}`);
          });
          return parts.join('<br>');
        }
      },
      xAxis:{type:'category',data:rows.map(r=>r.date),axisLabel:{fontSize:10,color:'#6b7280'}},
      yAxis:{type:'value',axisLabel:{formatter:'{value}%',fontSize:10,color:'#6b7280'},splitLine:{lineStyle:{color:'#edf1f5'}}},
      series:[{
        name:'涨跌幅',
        type:'line',
        data:rows.map(r=>r.return_pct),
        smooth:false,
        showSymbol:false,
        lineStyle:{width:2,color:'#2563eb'},
        markLine:{silent:true,symbol:'none',lineStyle:{color:'#cbd5e1'},data:[{yAxis:0}]},
        markPoint:{symbol:'circle',symbolSize:6,itemStyle:{borderColor:'#fff',borderWidth:1},emphasis:{scale:1.35,itemStyle:{borderColor:'#fff',borderWidth:1}},data:execs}
      }]
    });
    return;
  }
  const canvas=el.querySelector('canvas');
  if(!canvas)return;
  const ctx=canvas.getContext('2d'), w=canvas.width, h=canvas.height, pad=24;
  ctx.clearRect(0,0,w,h);
  const vals=rows.map(r=>r.return_pct);
  const min=Math.min(...vals,0), max=Math.max(...vals,0), span=(max-min)||1;
  const y=v=>h-pad-((v-min)/span)*(h-pad*2);
  ctx.strokeStyle='#e5e9f0'; ctx.lineWidth=1;
  ctx.beginPath(); ctx.moveTo(pad,y(0)); ctx.lineTo(w-pad,y(0)); ctx.stroke();
  ctx.strokeStyle='#2563eb'; ctx.lineWidth=2; ctx.beginPath();
  rows.forEach((r,i)=>{const x=pad+i*(w-pad*2)/Math.max(rows.length-1,1), yy=y(r.return_pct); if(i===0)ctx.moveTo(x,yy);else ctx.lineTo(x,yy);});
  ctx.stroke();
  const firstDate=rows[0].date, lastDate=rows[rows.length-1].date;
  (item.executions||[]).filter(isExecutedMarker).forEach(e=>{
    if(!e.date||e.date<firstDate||e.date>lastDate)return;
    const idx=rows.findIndex(r=>r.date>=e.date);
    const row=idx>=0?rows[idx]:rows[rows.length-1];
    const i=idx>=0?idx:rows.length-1;
    const isSell=executionSide(e)==='卖出';
    const x=pad+i*(w-pad*2)/Math.max(rows.length-1,1);
    ctx.fillStyle=isSell?'#c0392b':'#0a7d4d';
    ctx.beginPath(); ctx.arc(x,y(row.return_pct),3,0,Math.PI*2); ctx.fill();
    ctx.strokeStyle='#fff'; ctx.lineWidth=1; ctx.stroke();
  });
  const last=rows[rows.length-1], lx=w-pad, ly=y(last.return_pct);
  ctx.fillStyle=last.return_pct>=0?'#c0392b':'#0a7d4d'; ctx.beginPath(); ctx.arc(lx,ly,3,0,Math.PI*2); ctx.fill();
  ctx.strokeStyle='#fff'; ctx.lineWidth=1; ctx.stroke();
  ctx.fillStyle='#6b7280'; ctx.font='12px sans-serif'; ctx.fillText(`${last.return_pct.toFixed(1)}%`, Math.max(pad,lx-48), Math.max(14,ly-8));
}
function fmtPct(v){return v==null||Number.isNaN(v)?'-':`${(v*100).toFixed(1)}%`;}
function fmtMoney(v){return `¥${Math.round(Number(v||0)).toLocaleString()}`;}
function toPct(v,digits){return (Number(v||0)*100).toFixed(digits).replace(/\.0$/,'');}
function formatStamp(v){
  if(!v)return '-';
  const d=new Date(v);
  if(!Number.isNaN(d.getTime())){
    return d.toLocaleString('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});
  }
  return String(v).replace('T',' ').slice(0,16);
}
function valTagCn(tag){return {cheap:'偏便宜',rich:'偏贵',neutral:'中性'}[tag] || tag || '-';}
function valCell(x){
  const v=x.valuation;
  if(v){
    const pct=`${(v.percentile*100).toFixed(0)}% ${valTagCn(v.tag)}`;
    if(v.proxy){
      const tip=`创业板指无长历史 PE 源 → 用强相关代理指数「${escapeHtml(v.proxy)}」(legulegu 长历史) 近似分位。仅供参考，非创业板指本身。`;
      return `<span title="${tip}">${pct} <span class="mut">（代理·近似）ⓘ</span></span>`;
    }
    if(v.window_years){
      const tip=`${escapeHtml(v.index_name||'')} 自建累积分位，窗口约 ${v.window_years} 年（中证指数公司官方）。`;
      return `<span title="${tip}">${pct}</span>`;
    }
    return pct;
  }
  const acc=x.valuation_accumulating;
  if(acc){
    const tip=`${escapeHtml(acc.index_name||'')}（中证指数公司官方 PE）无长历史分位源：自 ${acc.history_from} 起按日累积自建分位，已积累约 ${acc.history_months} 个月，需约 ${acc.needed_years} 年才给出可靠分位。当前仅显示 PE 水平、不做贵/便宜判定。`;
    return `<span class="mut" title="${tip}">PE ${acc.pe} · 分位积累中 ⓘ</span>`;
  }
  if(x.valuation_na) return '<span class="mut">不适用</span>';
  if(x.valuation_missing) return '<span class="mut">'+glossary('估值','缺失(非中性)')+'</span>';
  return '-';
}
function escapeHtml(s){return String(s).replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));}

/* ---------- 回测 ---------- */
async function runBacktest(){
  const btn=$('#btbtn'); btn.disabled=true; btn.innerHTML='<span class="spin"></span>回测中…';
  $('#btbox').innerHTML=''; $('#btviz').innerHTML='<div class="hint" id="btProgress"><span class="spin"></span>回测中…（ETF 段 + 指数代理长段，通常 1–2 分钟）</div>';
  const stopPoll=pollTaskProgress('btProgress',Date.now(),{tasks:['backtest'],hint:'两段回测，通常 1–2 分钟'});
  try{
    const jr=await fetch('/api/backtest/json',{method:'POST'}); const jd=await jr.json();
    if(jd.ok)renderBacktestViz(jd.result);
    else $('#btviz').innerHTML=`<div class="msg err" style="display:block">${jd.error||'结构化回测失败'}</div>`;
    const r=await fetch('/api/backtest',{method:'POST'}); const d=await r.json();
    $('#btbox').innerHTML=`<pre>${(d.output||'无输出').replace(/</g,'&lt;')}</pre>`;
  }finally{stopPoll(); btn.disabled=false; btn.textContent='重新回测';}
}
function renderBacktestViz(result){
  const etf=(result&&result.etf_segment)||{};
  const proxy=(result&&result.proxy_segment)||{};
  const rows=etf.rows||[];
  const proxyRows=proxy.rows||[];
  const rowHtml=rows.map(r=>metricRow(r)).join('');
  const proxyHtml=proxyRows.map(r=>metricRow(r)).join('');
  const dca=result&&result.dca;
  let dcaHtml='';
  if(dca){
    const planRows=(dca.plans||[]).map(p=>{
      const win=p.beats_lumpsum_window_pct==null?'<span class="mut">基准</span>':`${(p.beats_lumpsum_window_pct*100).toFixed(0)}%`;
      return `<tr><td><b>${escapeHtml(p.label)}</b></td><td>${Number(p.median_final_multiple).toFixed(3)}x</td><td>${fmtPct(p.median_total_return)}</td><td class="${p.median_max_drawdown<-0.2?'down':'mut'}">${fmtPct(p.median_max_drawdown)}</td><td>${win}</td></tr>`;
    }).join('');
    const notes=(dca.notes||[]).map(n=>`<div>· ${escapeHtml(n)}</div>`).join('');
    const rep=dca.representative||{};
    dcaHtml=`<div class="watchhead">分批 / 定投建仓对比</div>
      <div class="chartbox"><b>建仓路径价值曲线（代表窗口 ${rep.start||'-'} 至 ${rep.end||'-'}）</b><div id="btDcaChart" class="echart"></div></div>
      <table><thead><tr><th>建仓节奏</th><th>期末倍数中位</th><th>总收益中位</th><th>最大${glossary('回撤')}中位</th><th>跑赢一次性</th></tr></thead><tbody>${planRows}</tbody></table>
      <div class="hint">滚动 ${dca.windows} 个起点、窗口约 ${dca.horizon_years} 年；未投现金按 ${(dca.cash_yield*100).toFixed(0)}% 计息。${notes}</div>`;
  }
  $('#btviz').innerHTML=`<div class="act"><b>推荐口径</b><br>${result.recommendation||'-'}<div class="hint">ETF 段 ${etf.start||'-'} 至 ${etf.end||'-'}，约 ${etf.years||'-'} 年；长样本代理段约 ${proxy.years||'-'} 年。</div></div>
    <div class="btcharts">
      <div class="chartbox"><b>ETF 段净值曲线</b><div id="btNavChart" class="echart"></div></div>
      <div class="chartbox"><b>ETF 段${glossary('回撤')}曲线</b><div id="btDdChart" class="echart"></div></div>
      <div class="chartbox"><b>均线周期敏感性</b><div id="btMaChart" class="echart"></div></div>
      <div class="chartbox"><b>再平衡频率敏感性</b><div id="btFreqChart" class="echart"></div></div>
    </div>
    <table><thead><tr><th>组合</th><th>年化</th><th>最大${glossary('回撤')}</th><th>波动</th><th>${glossary('最长水下')}</th><th>年换手</th></tr></thead><tbody>${rowHtml}</tbody></table>
    <div class="hint">ETF 可交易段更贴近真实产品；指数代理段更适合看危机期回撤轮廓。</div>
    <div class="hint">成本假设：再平衡按单边约 0.03%（万3）计费，未计入滑点、买卖价差与“一手=100份”最小单位的凑整损耗；实盘成本通常略高于回测。</div>
    ${proxyRows.length?`<div class="watchhead">指数代理长期段${proxy.basis?` <span class="mut">（${escapeHtml(proxy.basis)}${proxy.dropped&&proxy.dropped.length?'；剔除并披露：'+proxy.dropped.map(escapeHtml).join('、'):''}）</span>`:''}</div><table><thead><tr><th>组合</th><th>年化</th><th>最大回撤</th><th>波动</th><th>最长水下</th><th>年换手</th></tr></thead><tbody>${proxyHtml}</tbody></table>`:''}
    ${dcaHtml}`;
  drawBacktestCharts(result);
}
function metricRow(r){
  return `<tr><td><b>${r.name}</b></td><td>${fmtPct(r.cagr)}</td><td class="${r.max_drawdown<-0.2?'down':'mut'}">${fmtPct(r.max_drawdown)}</td><td>${fmtPct(r.vol)}</td><td>${r.underwater_days||0}日</td><td>${fmtPct(r.turnover_annual)}</td></tr>`;
}
function drawBacktestCharts(result){
  if(!window.echarts)return;
  const etf=(result&&result.etf_segment)||{};
  drawCurveChart('btNavChart', etf.curves||[], 'nav', v=>Number(v).toFixed(2));
  drawCurveChart('btDdChart', etf.curves||[], 'drawdown', v=>(Number(v)*100).toFixed(1)+'%');
  drawSensitivityChart('btMaChart', etf.sensitivity_ma||[], 'ma_days');
  drawSensitivityChart('btFreqChart', etf.sensitivity_freq||[], 'label');
  const dca=result&&result.dca;
  if(dca&&dca.representative){
    const curves=(dca.representative.curves||[]).map(c=>({name:c.label,points:c.points,kind:c.deploy_months===1?'benchmark':'static'}));
    drawCurveChart('btDcaChart', curves, 'value', v=>Number(v).toFixed(2));
  }
}
function drawCurveChart(id, curves, field, fmt){
  const el=document.getElementById(id); if(!el||!curves.length)return;
  const dates=(curves[0].points||[]).map(p=>p.date);
  const chart=initChart(el);
  chart.setOption({
    animation:false,
    tooltip:{trigger:'axis',valueFormatter:fmt},
    legend:{top:0,textStyle:{fontSize:11}},
    grid:{left:48,right:18,top:42,bottom:32},
    xAxis:{type:'category',data:dates,axisLabel:{fontSize:10,color:'#6b7280'}},
    yAxis:{type:'value',axisLabel:{formatter:v=>fmt(v),fontSize:10,color:'#6b7280'},splitLine:{lineStyle:{color:'#edf1f5'}}},
    series:curves.map(c=>({name:c.name,type:'line',showSymbol:false,data:(c.points||[]).map(p=>p[field]),lineStyle:{width:c.kind==='benchmark'?1.5:2}}))
  });
}
function drawSensitivityChart(id, rows, labelField){
  const el=document.getElementById(id); if(!el||!rows.length||!window.echarts)return;
  const chart=initChart(el);
  chart.setOption({
    animation:false,
    tooltip:{trigger:'axis',axisPointer:{type:'shadow'},valueFormatter:v=>(Number(v)*100).toFixed(1)+'%'},
    legend:{top:0,textStyle:{fontSize:11}},
    grid:{left:48,right:18,top:42,bottom:32},
    xAxis:{type:'category',data:rows.map(r=>String(r[labelField])),axisLabel:{color:'#6b7280'}},
    yAxis:{type:'value',axisLabel:{formatter:v=>(v*100).toFixed(0)+'%',color:'#6b7280'},splitLine:{lineStyle:{color:'#edf1f5'}}},
    series:[
      {name:'年化',type:'bar',data:rows.map(r=>r.cagr),itemStyle:{color:'#2563eb'}},
      {name:'最大回撤',type:'bar',data:rows.map(r=>Math.abs(r.max_drawdown)),itemStyle:{color:'#c0392b'}}
    ]
  });
}

/* ---------- 初始化（组合行情自动加载；回测仍懒加载） ---------- */
$('#glossList').innerHTML=GLOSS_ORDER.map(k=>`<div><b>${k}</b>：${escapeHtml(TERMS[k])}</div>`).join('');
buildDecisionWorkspace();
window.addEventListener('resize',()=>resizeCharts());
checkBackend();
async function loadStartupPortfolio(){
  try{
    await loadConfig();
    await loadMarketsTab(false);
  }catch(e){
    const box=$('#portfolioHoldings');
    if(box)box.innerHTML='<div class="msg err" style="display:block">组合数据自动加载失败，可在 ETF 行情页手动刷新。</div>';
    const sum=$('#portfolioSummary');
    if(sum&&sum.querySelector('.skel'))sum.innerHTML='';   // 失败时清掉骨架，不留"永远在加载"的假象
  }
}
loadStartupPortfolio();
loadReports();
loadExecutions();
loadDataHealth();
loadMonthlyReview();
loadWatchlistLearning();
showWorkspace('decision',false);
