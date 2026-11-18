/* ================= API LAYER ================= */
let authToken = null;
let currentUser = null;
let paymentIsManual = false;
let initialSetupRequired = false;
let loginInFlight = false;
let notifications = [];
let notificationsLoading = false;
let notificationsLoadFailed = false;
let readNotificationIds = new Set();
let topbarClockTimer = null;
let deferredInstallPrompt = null;
let appIsInstalled = false;
let reportsLoaded = false;
let backgroundDataPromise = null;
const apiCache = new Map();
const apiInflight = new Map();
const API_CACHE_TTL = 5000;

// Convert semantic icon placeholders to the bundled Lucide SVGs, including
// placeholders inserted later by table and modal templates.
let iconRenderScheduled = false;
function renderLucideIcons(){
  if(!window.lucide?.createIcons) return;
  window.lucide.createIcons({attrs:{'stroke-width':2}});
  // Lucide copies data-lucide onto generated SVGs. Clear it so later DOM
  // mutations do not make the observer repeatedly re-render existing icons.
  document.querySelectorAll('svg[data-lucide]').forEach(icon => icon.removeAttribute('data-lucide'));
}
const iconObserver = new MutationObserver(records=>{
  const hasNewIcons = records.some(record => [...record.addedNodes].some(node =>
    node.nodeType === Node.ELEMENT_NODE && (node.matches?.('[data-lucide]') || node.querySelector?.('[data-lucide]'))
  ));
  if(!hasNewIcons || iconRenderScheduled) return;
  iconRenderScheduled = true;
  requestAnimationFrame(()=>{
    iconRenderScheduled = false;
    renderLucideIcons();
  });
});
iconObserver.observe(document.body, {childList:true, subtree:true});
renderLucideIcons();

function isStandaloneApp(){
  return window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
}

function isIOSSafari(){
  const ua = navigator.userAgent;
  return /iPad|iPhone|iPod/.test(ua) && /Safari/.test(ua) && !/CriOS|FxiOS|EdgiOS|OPiOS/.test(ua) && !window.MSStream;
}

function updateInstallControl(){
  const button = document.getElementById('installAppBtn');
  if (!button) return;
  const shouldShow = !appIsInstalled && !isStandaloneApp() && Boolean(deferredInstallPrompt);
  button.hidden = !shouldShow;
}

async function promptInstall(){
  if (!deferredInstallPrompt) return;
  const prompt = deferredInstallPrompt;
  deferredInstallPrompt = null;
  updateInstallControl();
  await prompt.prompt();
  try { await prompt.userChoice; } catch (error) { /* The browser can close the prompt without a choice. */ }
}

function setupPwa(){
  appIsInstalled = isStandaloneApp();
  const installButton = document.getElementById('installAppBtn');
  installButton?.addEventListener('click', promptInstall);
  window.addEventListener('beforeinstallprompt', event => {
    event.preventDefault();
    deferredInstallPrompt = event;
    updateInstallControl();
  });
  window.addEventListener('appinstalled', () => {
    appIsInstalled = true;
    deferredInstallPrompt = null;
    updateInstallControl();
    showToast('Adinath Agri Services has been installed.');
  });
  window.matchMedia('(display-mode: standalone)').addEventListener?.('change', event => {
    appIsInstalled = event.matches;
    updateInstallControl();
  });
  // Safari does not expose beforeinstallprompt. Keep its one-time guidance
  // subtle and only show it on an uninstalled iPhone/iPad browser session.
  if (isIOSSafari() && !appIsInstalled && !sessionStorage.getItem('adinath-ios-install-hint')) {
    window.setTimeout(() => {
      if (!deferredInstallPrompt && !isStandaloneApp()) {
        sessionStorage.setItem('adinath-ios-install-hint', 'shown');
        showToast('To install: tap Share → Add to Home Screen.', 'success');
      }
    }, 1800);
  }
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', async () => {
      try {
        const registration = await navigator.serviceWorker.register('/service-worker.js', { scope: '/' });
        const offerUpdate = worker => {
          if (!worker || !navigator.serviceWorker.controller) return;
          showToast('New version available — tap to update.', 'success', () => worker.postMessage({ type: 'SKIP_WAITING' }));
        };
        offerUpdate(registration.waiting);
        registration.addEventListener('updatefound', () => registration.installing?.addEventListener('statechange', () => {
          if (registration.installing?.state === 'installed') offerUpdate(registration.waiting);
        }));
        navigator.serviceWorker.addEventListener('controllerchange', () => window.location.reload());
      } catch (error) { console.warn('Service worker registration failed', error); }
    });
  }
}

// Presentation-only workspace clock; it does not affect business records.
function startTopbarClock(){
  const target = document.getElementById('topbarDateTime');
  if(!target) return;
  const render = () => {
    const now = new Date();
    target.innerHTML = `<i data-lucide="calendar-days" class="lucide-icon" aria-hidden="true"></i><span>${now.toLocaleDateString('en-IN',{weekday:'short',day:'2-digit',month:'short'})}</span><b>${now.toLocaleTimeString('en-IN',{hour:'2-digit',minute:'2-digit'})}</b>`;
  };
  render();
  if(topbarClockTimer) clearInterval(topbarClockTimer);
  topbarClockTimer = setInterval(render, 60000);
}

// Escape API-provided text before inserting it into HTML templates.
function escapeHtml(value){
  return String(value ?? '').replace(/[&<>'"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
}
function persistAuthToken(token) {
  try {
    localStorage.setItem('adinath-token', token);
  } catch (e) { console.warn('Failed to persist token', e); }
}

function loadPersistedAuth() {
  try {
    const token = localStorage.getItem('adinath-token');
    if (token) {
      authToken = token;
      return true;
    }
  } catch (e) { console.warn('Failed to load persisted token', e); }
  return false;
}

function togglePasswordVisibility(){
  const input = document.getElementById('loginPassword');
  const button = document.getElementById('passwordToggle');
  const showing = input.type === 'text';
  input.type = showing ? 'password' : 'text';
  button.classList.toggle('is-visible', !showing);
  button.setAttribute('aria-label', showing ? 'Show password' : 'Hide password');
  button.setAttribute('aria-pressed', String(!showing));
  button.title = showing ? 'Show password' : 'Hide password';
}

function applyUserIdentity(user){
  document.getElementById('currentUserName').textContent = user.name;
  document.getElementById('currentUserRole').textContent = user.role === 'admin' ? 'Shop Admin' : (user.role === 'accountant' ? 'Accountant' : 'Staff');
  document.getElementById('accountName').textContent = user.name;
  document.querySelector('.staff-name').textContent = user.name;
  document.querySelector('.staff-role').textContent = user.role === 'admin' ? 'Shop Admin' : (user.role === 'accountant' ? 'Accountant' : 'Staff');
  document.querySelectorAll('.avatar').forEach(avatar => {
    if (!avatar.closest('.farmer-card') && !avatar.closest('.farmer-pick-item')) avatar.textContent = initials(user.name);
  });
}

async function resumeSession(){
  if(!loadPersistedAuth()) return;
  try {
    currentUser = await apiFetch('/auth/me');
    applyUserIdentity(currentUser);
    document.getElementById('mainApp').style.display = '';
    document.getElementById('loginScreen').style.display = 'none';
    await bootApp();
  } catch (error) {
    authToken = null;
    try { localStorage.removeItem('adinath-token'); } catch (e) { console.warn('Failed to clear expired session', e); }
  }
}

async function apiFetch(path, opts={}){
  const {timeout = 15000, signal, ...requestOptions} = opts;
  const method = String(requestOptions.method || 'GET').toUpperCase();
  const cacheKey = method === 'GET' ? `${authToken || ''}:${path}` : '';
  if (cacheKey) {
    const cached = apiCache.get(cacheKey);
    if (cached && cached.expiresAt > Date.now()) return cached.value;
    const pending = apiInflight.get(cacheKey);
    if (pending) return pending;
  }
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), timeout);
  if(signal) signal.addEventListener('abort', () => controller.abort(), {once:true});
  const headers = Object.assign({}, requestOptions.headers, authToken ? {Authorization: 'Bearer '+authToken} : {});
  if(requestOptions.body && !(requestOptions.body instanceof URLSearchParams)){
    headers['Content-Type'] = 'application/json';
    requestOptions.body = JSON.stringify(requestOptions.body);
  }
  const request = (async () => {
    let res;
    try { res = await fetch(path, Object.assign({}, requestOptions, {headers, signal:controller.signal})); }
    catch(error) {
      if(controller.signal.aborted) throw new Error('The request took too long. Please check the local service and try again.');
      throw error;
    } finally { window.clearTimeout(timeoutId); }
    if(!res.ok){
      let detail = res.statusText;
      try{ const j = await res.json(); detail = j.detail || detail; }catch(e){}
      throw new Error(detail);
    }
    const value = res.status===204 ? null : await res.json();
    if (cacheKey) apiCache.set(cacheKey, {value, expiresAt: Date.now() + API_CACHE_TTL});
    return value;
  })();
  if (cacheKey) apiInflight.set(cacheKey, request);
  try { return await request; }
  finally {
    if (cacheKey) apiInflight.delete(cacheKey);
    if (method !== 'GET') apiCache.clear();
  }
}

async function fetchWithTimeout(url, options={}, timeout=30000){
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeout);
  try {
    return await fetch(url, {...options, signal: controller.signal});
  } catch (error) {
    if (controller.signal.aborted) throw new Error('The export took too long. Please try again.');
    throw error;
  } finally {
    window.clearTimeout(timer);
  }
}

function selectReportPeriod(period, button){
  document.querySelectorAll('.report-quick-filter button').forEach(item => item.classList.remove('active'));
  if(button) button.classList.add('active');
  if(period === 'custom') { showToast('Custom reporting dates are not available yet. Choose one of the supported report periods.', 'warning'); return; }
  updateKpiSummary(period);
  const card = document.querySelector(`.report-period-card[data-period="${period}"]`);
  card?.scrollIntoView({behavior: 'smooth', block: 'nearest', inline: 'center'});
}

async function downloadReport(period, button, requestedFormat){
  const format = requestedFormat || 'pdf';
  const originalLabel = button?.textContent || 'Export Report';
  const exportMenu = button?.closest('.export-menu');
  const exportButtons = exportMenu ? [...exportMenu.querySelectorAll('button')] : [button];
  exportButtons.filter(Boolean).forEach(control => control.disabled = true);
  if(button){ button.disabled = true; button.textContent = 'Preparing…'; }
  try {
    const response = await fetchWithTimeout(`/reports/export?range=${encodeURIComponent(period)}&format=${encodeURIComponent(format)}`, {
      headers: authToken ? {Authorization: `Bearer ${authToken}`} : {}
    });
    if(!response.ok){
      let message = 'Could not export report';
      try { message = (await response.json()).detail || message; } catch (e) {}
      throw new Error(message);
    }
    const blob = await response.blob();
    const disposition = response.headers.get('Content-Disposition') || '';
    const filename = disposition.match(/filename="?([^";]+)"?/i)?.[1] || `Adinath_Agri_${period[0].toUpperCase() + period.slice(1)}_Report.${format}`;
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url; link.download = filename; link.style.display = 'none';
    document.body.appendChild(link); link.click(); link.remove();
    // Keep the object URL alive briefly; some browsers begin the download asynchronously.
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    showToast('Report downloaded successfully.');
  } catch(error) {
    showToast('Unable to generate the report. Please try again.', 'critical');
  } finally {
    if(button) button.textContent = originalLabel;
    exportButtons.filter(Boolean).forEach(control => control.disabled = false);
    if(exportMenu) exportMenu.open = false;
  }
}

function buildLegacyReportsDashboard(){
  const root = document.getElementById('view-reports');
  if(!root || root.dataset.premiumBuilt) return;
  const icons = {today:'01', weekly:'07', monthly:'30'};
  const labels = {today:["LIVE TODAY", "Today's Report", 'View your business performance for today.'], weekly:['THIS WEEK', 'Weekly Report', 'View your business performance for the current week.'], monthly:['THIS MONTH', 'Monthly Report', 'View your business performance for the current month.']};
  const card = period => { const [kicker, title, description] = labels[period]; return `<article class="report-download-card report-period-card ${period}-card" data-period="${period}"><div class="report-card-top"><span class="report-period-icon ${period}">${icons[period]}</span><div><span class="report-card-kicker">${kicker}</span><strong>${title}</strong><p>${description}</p></div><span class="report-date" id="${period}Range">Loading...</span></div><div class="report-metric-grid"><div><span>Sales</span><b id="${period}Sales">₹0</b></div><div><span>Purchases</span><b id="${period}Purchases">₹0</b></div><div><span>Expenses</span><b id="${period}Expenses">₹0</b></div><div><span>Profit</span><b id="${period}Profit">₹0</b></div><div><span>Transactions</span><b id="${period}Transactions">0</b></div></div><div class="report-sparkline" id="${period}Spark" aria-label="${title} sales performance chart"></div><div class="report-card-footer"><button class="btn btn-secondary" type="button" onclick="viewReport('${period}')">View Report</button><details class="export-menu"><summary class="btn btn-primary">Export Report <span>⌄</span></summary><div><button type="button" onclick="downloadReport('${period}', this, 'pdf')">Download PDF</button><button type="button" onclick="downloadReport('${period}', this, 'csv')">Download CSV</button><button type="button" onclick="downloadReport('${period}', this, 'xlsx')">Download Excel</button></div></details></div></article>`; };
  root.innerHTML = `<header class="reports-hero"><div class="reports-heading"><span class="reports-hero-icon" aria-hidden="true">↗</span><div><span class="report-eyebrow">BUSINESS INTELLIGENCE</span><h1>Reports</h1><p>Track your business performance with detailed insights and downloadable reports.</p></div></div><button class="report-date-selector" type="button" aria-label="Selected report date range"><span aria-hidden="true">◷</span><span><small>REPORTING PERIOD</small><b id="selectedReportRange">Today</b></span><i aria-hidden="true">⌄</i></button></header><div class="report-quick-filter" role="group" aria-label="Quick report periods"><span>Quick filters</span><button type="button" class="active" onclick="selectReportPeriod('today', this)">Today</button><button type="button" onclick="selectReportPeriod('weekly', this)">Last 7 Days</button><button type="button" onclick="selectReportPeriod('monthly', this)">Last 30 Days</button><button type="button" onclick="selectReportPeriod('custom', this)">Custom Range</button></div><div class="reports-kpi-grid" aria-label="Selected period summary"><article class="reports-kpi sales"><span class="reports-kpi-icon"><i data-lucide="chart-no-axes-combined" class="lucide-icon" aria-hidden="true"></i></span><div><span>Total Sales</span><strong id="kpiSales">₹0</strong><small id="kpiSalesNote">Current period</small></div></article><article class="reports-kpi purchases"><span class="reports-kpi-icon"><i data-lucide="shopping-cart" class="lucide-icon" aria-hidden="true"></i></span><div><span>Total Purchases</span><strong id="kpiPurchases">₹0</strong><small>Tracked purchase value</small></div></article><article class="reports-kpi expenses"><span class="reports-kpi-icon"><i data-lucide="receipt-text" class="lucide-icon" aria-hidden="true"></i></span><div><span>Total Expenses</span><strong id="kpiExpenses">₹0</strong><small>Tracked expense value</small></div></article><article class="reports-kpi profit"><span class="reports-kpi-icon"><i data-lucide="chart-no-axes-combined" class="lucide-icon" aria-hidden="true"></i></span><div><span>Total Profit</span><strong id="kpiProfit">₹0</strong><small>Calculated business profit</small></div></article></div><div class="report-downloads report-periods" aria-label="Business report periods">${card('today')}${card('weekly')}${card('monthly')}</div><div id="reportDetail" class="report-detail" aria-live="polite"></div><div class="report-grid report-analytics-grid"><div class="panel reports-analytics-card"><div class="panel-head"><div><span class="panel-title">Sales Performance</span><span class="panel-caption">Track your sales trend over time.</span></div><span class="analytics-total" id="analyticsSalesTotal">₹0 <small>7-day sales</small></span></div><div class="panel-body" id="salesPerformance"></div><button class="report-link-btn" type="button" onclick="viewReport('weekly')">View Full Report →</button></div><div class="panel reports-analytics-card"><div class="panel-head"><div><span class="panel-title">Top Products</span><span class="panel-caption">Your highest-contributing products.</span></div></div><div class="panel-body"><div class="top-products-list" id="topProductsList"></div></div><button class="report-link-btn" type="button" onclick="viewReport('monthly')">View Full Report →</button></div></div>`;
  root.dataset.premiumBuilt = 'true';
}

let reportOverview = {};
function formatReportRange(start, end){
  const options = {day:'2-digit', month:'short', year:'numeric'};
  const first = new Date(`${start}T00:00:00`).toLocaleDateString('en-IN', options);
  const last = new Date(`${end}T00:00:00`).toLocaleDateString('en-IN', options);
  return start === end ? first : `${first} – ${last}`;
}
function renderReportOverview(data){
  reportOverview = data;
  ['today', 'weekly', 'monthly'].forEach(period => {
    const report = data[period]; if(!report) return;
    ['sales','purchases','expenses','profit'].forEach(metric => {
      const target = document.getElementById(`${period}${metric[0].toUpperCase() + metric.slice(1)}`);
      if(target) target.textContent = money(report[metric]);
    });
    const range = document.getElementById(`${period}Range`);
    if(range) range.textContent = formatReportRange(report.start, report.end);
    const transactions = document.getElementById(`${period}Transactions`);
    if(transactions) transactions.textContent = report.transactions;
  });
  const analyticsTotal = document.getElementById('analyticsSalesTotal');
  if(analyticsTotal && data.weekly) analyticsTotal.innerHTML = `${money(data.weekly.sales)} <small>current week</small>`;
  updateKpiSummary('today');
  renderReportSparklines();
}
function updateKpiSummary(period){
  const report = reportOverview[period]; if(!report) return;
  ['sales','purchases','expenses','profit'].forEach(metric => {
    const target = document.getElementById(`kpi${metric[0].toUpperCase() + metric.slice(1)}`);
    if(target) target.textContent = money(report[metric]);
  });
  const note = document.getElementById('kpiSalesNote');
  if(note) note.textContent = `${formatReportRange(report.start, report.end)} · ${report.transactions} transactions`;
  const selectedRange = document.getElementById('selectedReportRange');
  if(selectedRange) selectedRange.textContent = formatReportRange(report.start, report.end);
}
let reportPerformance = [];
function renderReportSparklines(){
  const points = reportPerformance.map(point => Number(point.total) || 0);
  const max = Math.max(...points, 1);
  const svg = points.length ? `<svg viewBox="0 0 160 44" preserveAspectRatio="none" aria-hidden="true"><polyline points="${points.map((value, index) => `${(index / Math.max(points.length - 1, 1) * 160).toFixed(1)},${(40 - value / max * 31).toFixed(1)}`).join(' ')}" fill="none" stroke="#39754b" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/></svg>` : '<span>No sales activity yet</span>';
  ['today', 'weekly', 'monthly'].forEach(period => { const target = document.getElementById(`${period}Spark`); if(target) target.innerHTML = svg; });
}
function viewReport(period){
  const report = reportOverview[period];
  if(!report){ showToast('Report data is still loading', 'warning'); return; }
  document.querySelectorAll('.report-period-card').forEach(card => card.classList.toggle('is-selected', card.dataset.period === period));
  const title = {today:"Today's Business Overview", weekly:'Weekly Performance', monthly:'Monthly Business Summary'}[period];
  const detail = document.getElementById('reportDetail');
  detail.innerHTML = `<div><span>DETAILED REPORT</span><h2>${title}</h2><p>${formatReportRange(report.start, report.end)} · ${report.transactions} transactions recorded</p></div><div class="report-detail-metrics"><span>Sales <b>${money(report.sales)}</b></span><span>Purchases <b>${money(report.purchases)}</b></span><span>Expenses <b>${money(report.expenses)}</b></span><span>Profit <b>${money(report.profit)}</b></span></div>`;
  detail.classList.add('visible'); detail.scrollIntoView({behavior:'smooth', block:'nearest'});
}

/* Reports v2: one selected API dataset powers cards, tables and exports. */
let selectedReportPeriod = 'today';
let selectedReport = null;
function reportQuery(){
  const params = new URLSearchParams({range:selectedReportPeriod});
  if(selectedReportPeriod === 'custom'){
    const start = document.getElementById('reportStartDate')?.value;
    const end = document.getElementById('reportEndDate')?.value;
    if(start) params.set('start_date', start); if(end) params.set('end_date', end);
  }
  return params.toString();
}
function buildReportsDashboard(){
  const root = document.getElementById('view-reports'); if(!root) return;
  root.innerHTML = `<section class="reports-v2" aria-live="polite">
    <header class="reports-v2-head"><span class="reports-v2-icon"><i data-lucide="chart-no-axes-combined" class="lucide-icon" aria-hidden="true"></i></span><div><span class="report-eyebrow">ADINATH AGRI SERVICES</span><h1>Business Reports</h1><p>Reconciled sales, collections and product performance from your live business records.</p></div><div class="report-export-actions"><button class="btn btn-secondary" onclick="downloadSelectedReport('csv',this)">CSV</button><button class="btn btn-secondary" onclick="downloadSelectedReport('xlsx',this)">Excel</button><button class="btn btn-primary" onclick="downloadSelectedReport('pdf',this)">Export PDF</button></div></header>
    <div class="report-filter-bar"><div class="report-period-tabs" role="group" aria-label="Report period"><button class="active" data-period="today" onclick="selectReportPeriodV2('today',this)">Today</button><button data-period="weekly" onclick="selectReportPeriodV2('weekly',this)">This Week</button><button data-period="monthly" onclick="selectReportPeriodV2('monthly',this)">This Month</button><button data-period="custom" onclick="selectReportPeriodV2('custom',this)">Custom Range</button></div><div id="customReportDates" class="report-custom-dates" hidden><label>From <input id="reportStartDate" type="date"></label><label>To <input id="reportEndDate" type="date"></label><button class="btn btn-primary" onclick="loadSelectedReport()">Apply</button><button class="btn btn-ghost" onclick="resetCustomReport()">Reset</button></div><span id="reportPeriodLabel" class="report-period-label">Loading report…</span></div>
    <div id="reportError" class="report-message" hidden></div><div id="reportLoading" class="report-loading">Loading reconciled report data…</div>
    <div id="reportContent" hidden><div class="report-kpis-v2"><article class="report-kpi-v2 sales"><span><i data-lucide="chart-no-axes-combined" class="lucide-icon" aria-hidden="true"></i></span><div><small>TOTAL SALES</small><strong id="reportSales">₹0</strong><em>Revenue in selected period</em></div></article><article class="report-kpi-v2 purchases"><span><i data-lucide="shopping-cart" class="lucide-icon" aria-hidden="true"></i></span><div><small>TOTAL PURCHASES</small><strong id="reportPurchases">₹0</strong><em>Recorded purchase cost</em></div></article><article class="report-kpi-v2 expenses"><span><i data-lucide="receipt-text" class="lucide-icon" aria-hidden="true"></i></span><div><small>TOTAL EXPENSES</small><strong id="reportExpenses">₹0</strong><em>Operating expenses</em></div></article><article class="report-kpi-v2 profit"><span><i data-lucide="chart-no-axes-combined" class="lucide-icon" aria-hidden="true"></i></span><div><small>NET PROFIT</small><strong id="reportProfit">₹0</strong><em>Sales less costs and expenses</em></div></article><article class="report-kpi-v2 transactions"><span><i data-lucide="receipt-text" class="lucide-icon" aria-hidden="true"></i></span><div><small>TRANSACTIONS</small><strong id="reportTransactions">0</strong><em>Unique invoices included</em></div></article></div>
    <div class="report-summary-grid"><section class="report-surface"><header><div><small>FINANCIAL OVERVIEW</small><h2>Financial Summary</h2></div></header><table class="report-modern-table"><tbody id="financialSummary"></tbody></table></section><section class="report-surface"><header><div><small>COLLECTIONS</small><h2>Payment Summary</h2></div></header><div id="paymentSummary" class="payment-summary-v2"></div></section></div>
    <section class="report-surface"><header><div><small>SALES MIX</small><h2>Product Performance</h2></div><span id="productContributionNote" class="report-table-note"></span></header><div class="report-table-scroll"><table class="report-modern-table"><thead><tr><th>Product</th><th class="center">Quantity Sold</th><th class="right">Sales Amount</th><th class="right">Contribution</th><th class="right">Average Selling Price</th></tr></thead><tbody id="productPerformance"></tbody></table></div></section>
    <section class="report-surface"><header><div><small>REGISTER</small><h2>Transaction Details</h2></div><span id="transactionCountNote" class="report-table-note"></span></header><div class="report-table-scroll"><table class="report-modern-table transactions-v2"><thead><tr><th>Date</th><th>Invoice No.</th><th>Customer</th><th>Product</th><th class="center">Qty</th><th class="right">Amount</th><th>Payment Method</th><th>Status</th></tr></thead><tbody id="reportTransactionsTable"></tbody></table></div></section></div></section>`;
}
function selectReportPeriodV2(period, button){
  selectedReportPeriod = period;
  document.querySelectorAll('.report-period-tabs button').forEach(b=>b.classList.toggle('active', b===button));
  document.getElementById('customReportDates').hidden = period !== 'custom';
  if(period !== 'custom') loadSelectedReport();
}
function resetCustomReport(){ const s=document.getElementById('reportStartDate'), e=document.getElementById('reportEndDate'); if(s) s.value=''; if(e) e.value=''; }
function reportDateLabel(report){ return formatReportRange(report.start, report.end); }
function reportBadge(value, type){ return `<span class="report-badge ${type}">${escapeHtml(value)}</span>`; }
function renderSelectedReport(report){
  selectedReport = report;
  document.getElementById('reportPeriodLabel').textContent = `${report.period === 'custom' ? 'Custom range' : report.period[0].toUpperCase()+report.period.slice(1)} · ${reportDateLabel(report)}`;
  [['reportSales','sales'],['reportPurchases','purchases'],['reportExpenses','expenses'],['reportProfit','profit']].forEach(([id,key])=>document.getElementById(id).textContent=money(report[key])); document.getElementById('reportTransactions').textContent=report.transactions;
  const finance=[['Sales Revenue',report.sales],['Purchase Cost',report.purchases],['Operating Expenses',report.expenses],['Net Profit',report.profit],['Amount Collected',report.paid],['Outstanding / Credit',report.due]];
  document.getElementById('financialSummary').innerHTML=finance.map(([label,value])=>`<tr><td>${label}</td><td class="right ${label==='Net Profit'?'profit-value':''}">${money(value)}</td></tr>`).join('');
  const payment = report.payments || {}; const cash=Number(payment.Cash||0), bank=Number(payment.Bank||0)+Number(payment.Upi||0), credit=Number(payment.Credit||0);
  document.getElementById('paymentSummary').innerHTML=[['Cash Received',cash,'cash'],['Bank / UPI Received',bank,'bank'],['Credit Sales',credit,'credit'],['Total Collected',report.paid,'total'],['Outstanding Amount',report.due,'outstanding']].map(([label,value,kind])=>`<div class="payment-row ${kind}"><span>${label}</span><b>${money(value)}</b></div>`).join('');
  const products = report.products || []; document.getElementById('productContributionNote').textContent=products.length ? `${products.reduce((sum,p)=>sum+Number(p.contribution),0).toFixed(1)}% reconciled contribution` : 'No product sales';
  document.getElementById('productPerformance').innerHTML=products.length ? products.map(p=>`<tr><td class="strong">${escapeHtml(p.name)}</td><td class="center">${Number(p.quantity).toLocaleString('en-IN',{maximumFractionDigits:2})}</td><td class="right">${money(p.sales)}</td><td class="right"><span class="contribution"><i style="width:${Math.min(100,p.contribution)}%"></i>${Number(p.contribution).toFixed(2)}%</span></td><td class="right">${money(p.average_price)}</td></tr>`).join('') : '<tr><td colspan="5" class="report-empty">No product sales in this period.</td></tr>';
  document.getElementById('transactionCountNote').textContent=`${report.transactions} unique invoice${report.transactions===1?'':'s'}`;
  document.getElementById('reportTransactionsTable').innerHTML=report.rows.length ? report.rows.map(row=>`<tr><td>${new Date(row.date).toLocaleDateString('en-IN',{day:'2-digit',month:'short',year:'numeric'})}</td><td class="strong">${escapeHtml(row.invoice)}</td><td>${escapeHtml(row.customer)}</td><td class="product-cell">${escapeHtml(row.product)}</td><td class="center">${Number(row.quantity).toLocaleString('en-IN',{maximumFractionDigits:2})}</td><td class="right strong">${money(row.amount)}</td><td>${reportBadge(row.payment,'payment-'+row.payment.toLowerCase())}</td><td>${reportBadge(row.status,'status-'+row.status.toLowerCase())}</td></tr>`).join('') : '<tr><td colspan="8" class="report-empty">No transactions recorded for this period.</td></tr>';
}
async function loadSelectedReport(){
  const loading=document.getElementById('reportLoading'), content=document.getElementById('reportContent'), error=document.getElementById('reportError'); if(!loading) return;
  if(selectedReportPeriod==='custom' && (!document.getElementById('reportStartDate')?.value || !document.getElementById('reportEndDate')?.value)){ error.textContent='Choose both dates to run a custom report.'; error.hidden=false; return false; }
  loading.hidden=false; content.hidden=true; error.hidden=true;
  try { renderSelectedReport(await apiFetch(`/reports/data?${reportQuery()}`)); content.hidden=false; return true; }
  catch(e){ error.textContent=e.message || 'Unable to load the report.'; error.hidden=false; return false; }
  finally { loading.hidden=true; }
}
async function downloadSelectedReport(format, button){
  if(selectedReportPeriod==='custom' && (!document.getElementById('reportStartDate')?.value || !document.getElementById('reportEndDate')?.value)){ showToast('Choose both dates before exporting.', 'warning'); return; }
  const label=button.textContent; button.disabled=true; button.textContent='Preparing…';
  try { const response=await fetchWithTimeout(`/reports/export?${reportQuery()}&format=${format}`,{headers:authToken?{Authorization:`Bearer ${authToken}`}:{}}); if(!response.ok) throw new Error('Export could not be generated'); const blob=await response.blob(); const href=URL.createObjectURL(blob); const link=document.createElement('a'); link.href=href; link.download=`Adinath_Agri_${selectedReportPeriod}_Report.${format}`; link.click(); setTimeout(()=>URL.revokeObjectURL(href),1000); showToast(`${format.toUpperCase()} report downloaded successfully.`); }
  catch(e){ showToast(e.message || 'Unable to export report.', 'critical'); } finally { button.disabled=false; button.textContent=label; }
}
async function loadLegacySelectedReport(){
  // One endpoint returns the same reconciled dataset shown in the UI and used by export.
  await loadSelectedReport();
}
async function handleLogin(){
  if(loginInFlight) return;
  const username = document.getElementById('loginUsername').value.trim();
  const password = document.getElementById('loginPassword').value;
  const name = document.getElementById('setupName').value.trim();
  const errEl = document.getElementById('loginError');
  errEl.textContent = '';
  if(!username || !password || (initialSetupRequired && !name)){ errEl.textContent = initialSetupRequired ? 'Enter your name, username, and password' : 'Enter username and password'; return; }
  const btn = document.getElementById('loginBtn');
  const buttonLabel = btn.querySelector('span');
  const originalLabel = buttonLabel.textContent;
  loginInFlight = true;
  btn.disabled = true; buttonLabel.textContent = 'Signing in…';
  try{
    if(initialSetupRequired){
      await apiFetch('/auth/setup', {method:'POST', body:{name, username, password}});
      initialSetupRequired = false;
    }
    const form = new URLSearchParams(); form.append('username', username); form.append('password', password);
    const data = await apiFetch('/auth/login', {method:'POST', body:form});
    authToken = data.access_token;
    if(document.getElementById('rememberSession').checked) persistAuthToken(authToken);
    else { try { localStorage.removeItem('adinath-token'); } catch (e) { console.warn('Failed to clear session preference', e); } }
    currentUser = data.user;
    applyUserIdentity(currentUser);
    const loginScreen = document.getElementById('loginScreen');
    const mainApp = document.getElementById('mainApp');
    const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    mainApp.style.display = '';
    mainApp.classList.add('app-entering');
    loginScreen.classList.add('login-leaving');
    playLeafReveal(reduceMotion);
    if (!reduceMotion) await new Promise(resolve => setTimeout(resolve, 430));
    loginScreen.style.display = 'none';
    loginScreen.classList.remove('login-leaving');
    await bootApp();
    mainApp.classList.remove('app-entering');
  }catch(e){
    errEl.textContent = e.message || 'Login failed';
  }finally{
    loginInFlight = false;
    btn.disabled = false; buttonLabel.textContent = originalLabel;
  }
}

function playLeafReveal(reduceMotion){
  if(reduceMotion) return;
  const overlay = document.getElementById('leafTransition') || document.createElement('div');
  overlay.id = 'leafTransition';
  overlay.className = 'leaf-transition';
  overlay.setAttribute('aria-hidden', 'true');
  if(!overlay.isConnected) document.body.append(overlay);

  const leaves = [
    [4, 12, 28, 0, 1.42, 72, -28, 74, .64],
    [18, 74, 20, .12, 1.36, 68, -34, 102, .52],
    [29, 32, 34, .18, 1.58, 70, 18, 66, .6],
    [43, 88, 23, .27, 1.31, 65, -44, 118, .48],
    [56, 18, 22, .34, 1.48, 60, -31, 92, .55],
    [67, 62, 31, .4, 1.62, 58, 24, 70, .62],
    [78, 29, 18, .48, 1.34, 55, -36, 108, .46],
    [89, 82, 27, .55, 1.52, 50, -22, 84, .58],
    [35, 54, 16, .62, 1.28, 72, 20, 122, .42],
  ];
  const leafSvg = '<svg viewBox="0 0 40 60" focusable="false"><path class="leaf-shape" d="M20 56C8 46 1 31 7 17 12 6 27 3 36 4 38 21 34 44 20 56Z"/><path class="leaf-vein" d="M20 55C19 36 23 21 35 6M20 39l-9-12m11 4 9-11m-12 29-9-10"/></svg>';
  overlay.innerHTML = leaves.map(([left, top, size, delay, duration, driftX, driftY, rotation, opacity]) => {
    const element = document.createElement('span');
    element.className = 'leaf-particle';
    element.style.setProperty('--leaf-left', `${left}%`);
    element.style.setProperty('--leaf-top', `${top}%`);
    element.style.setProperty('--leaf-size', `${size}px`);
    element.style.setProperty('--leaf-delay', `${delay}s`);
    element.style.setProperty('--leaf-duration', `${duration}s`);
    element.style.setProperty('--leaf-drift-x', `${driftX}vw`);
    element.style.setProperty('--leaf-drift-y', `${driftY}vh`);
    element.style.setProperty('--leaf-rotation', `${rotation}deg`);
    element.style.setProperty('--leaf-opacity', opacity);
    element.innerHTML = leafSvg;
    return element.outerHTML;
  }).join('');
  overlay.classList.remove('is-active');
  requestAnimationFrame(() => overlay.classList.add('is-active'));
  window.setTimeout(() => {
    overlay.classList.remove('is-active');
    overlay.replaceChildren();
  }, 2050);
}

document.getElementById('loginPassword').addEventListener('keydown', e=>{ if(e.key==='Enter') handleLogin(); });

async function configureFirstLaunch(){
  try{
    const status = await apiFetch('/auth/setup-status');
    initialSetupRequired = status.setup_required;
    if(initialSetupRequired){
      document.getElementById('loginHeading').textContent = 'Create administrator account';
      document.getElementById('setupName').style.display = '';
      document.getElementById('setupNameLabel').style.display = '';
      document.querySelector('#loginBtn span').textContent = 'Create Administrator';
      document.getElementById('setupHint').textContent = 'This secure one-time setup creates the first administrator. No preconfigured account is provided.';
    }
  }catch(e){ document.getElementById('loginError').textContent = 'Unable to check initial setup status'; }
}

function logout(){
  authToken = null;
  apiCache.clear();
  apiInflight.clear();
  backgroundDataPromise = null;
  reportsLoaded = false;
  try { localStorage.removeItem('adinath-token'); } catch (e) { console.warn('Failed to clear saved session', e); }
  currentUser = null;
  bill = [];
  selectedFarmer = null;
  document.getElementById('mainApp').style.display = 'none';
  document.getElementById('mainApp').classList.remove('app-entering');
  document.getElementById('loginPassword').value = '';
  document.getElementById('loginScreen').style.display = 'flex';
  document.getElementById('loginUsername').focus();
}

function applyPermissions(){
  document.querySelectorAll('.admin-only').forEach(element => { element.hidden = !isAdmin(); });
  document.querySelectorAll('.accounting-only').forEach(element => { element.hidden = !(isAdmin() || isAccountant()); });
  document.querySelectorAll('.cost-only').forEach(element => { element.hidden = !canViewCosts(); });
}

function isAdmin(){
  // API roles normally arrive as strings; accepting a value property keeps
  // the UI reliable if a client serializes the enum as an object.
  return String(currentUser?.role?.value ?? currentUser?.role ?? '').toLowerCase() === 'admin';
}
function isAccountant(){ return String(currentUser?.role?.value ?? currentUser?.role ?? '').toLowerCase() === 'accountant'; }
function canViewCosts(){ return isAdmin() || isAccountant(); }

function openProductForm(productId=null){
  const product = products.find(item => item.id === productId);
  if(!product && !isAdmin()){ showToast('Only an administrator can manage products'); return; }
  const standardGstRates = [0, 5, 12, 18, 28];
  const categoryOptions = [...new Set(['Fertilizers','Pesticides','Herbicides','Fungicides','Insecticides','Plant Growth Regulators','Micronutrients','Agricultural Equipment','Other Agricultural Products',...(product?.categoryRaw?[product.categoryRaw]:[])])];
  const hasCustomGst = product?.gstSelectionType === 'custom' || (product?.gstRate != null && !standardGstRates.includes(Number(product.gstRate)));
  document.getElementById('entityModalTitle').textContent = product ? 'Edit Product' : 'New Product';
  document.getElementById('entityForm').innerHTML = `
    <label>Name<input name="name" required maxlength="160" value="${escapeHtml(product?.nameRaw || '')}"></label>
    <label>Product description <small>(optional; not listed in the PDF)</small><textarea name="description" rows="2" maxlength="1000">${escapeHtml(product?.descriptionRaw || '')}</textarea></label>
    <label>Price-list serial number <small>(optional)</small><input name="catalog_serial" type="number" min="1" step="1" value="${product?.catalogSerial ?? ''}"></label>
    <label>Brand <small>(optional)</small><input name="brand" maxlength="120" value="${escapeHtml(product?.brandRaw || '')}" placeholder="Brand name"></label>
    <label>SKU<input name="sku" required maxlength="80" value="${escapeHtml(product?.skuRaw || '')}"></label>
    <label>Product Type<select name="category" required>${categoryOptions.map(category=>`<option value="${escapeHtml(category)}" ${(product?.categoryRaw||'Fertilizer')===category?'selected':''}>${escapeHtml(category)}</option>`).join('')}</select></label>
    <label>Unit<input name="unit" required maxlength="40" value="${escapeHtml(product?.unitRaw || '')}"></label>
    <label>HSN/SAC<input name="hsn_code" maxlength="20" value="${escapeHtml(product?.hsnCode || '')}"></label>
    <label>Buying price (incl. GST)<input name="purchase_price_including_gst" type="number" min="0" step="0.01" value="${product?.purchasePrice ?? ''}"></label>
    <label>Selling price<input name="selling_price" type="number" min="0" step="0.01" value="${product?.price ?? ''}" placeholder="Not verified"></label>
    <label>Product discount (%)<input name="discount_percentage" type="number" min="0" max="100" step="0.01" value="${product?.discountPercentage ?? ''}"></label>
    <label>GST rate (%)<select name="gst_rate" id="productGstRate"><option value="">Select GST</option>${standardGstRates.map(rate=>`<option value="${rate}" ${!hasCustomGst && product?.gstRate != null && Number(product.gstRate)===rate?'selected':''}>${rate}%</option>`).join('')}<option value="custom" ${hasCustomGst?'selected':''}>Custom GST</option></select></label>
    <label id="customGstRateLabel" ${hasCustomGst?'':'hidden'}>Custom GST (%)<input name="custom_gst_rate" id="customGstRate" type="number" min="0" max="100" step="0.01" value="${hasCustomGst ? product.gstRate : ''}" ${hasCustomGst?'required':''}></label>
    <label>Selling price tax mode<select name="selling_price_tax_mode"><option value="inclusive" ${product?.sellingPriceTaxMode==='inclusive'?'selected':''}>Tax inclusive</option><option value="exclusive" ${!product || product.sellingPriceTaxMode!=='inclusive'?'selected':''}>Tax exclusive</option></select></label>
    <label>Pack size<input name="pack_size" maxlength="80" value="${escapeHtml(product?.packSize || '')}"></label>
    <label>Current stock<input name="stock" type="number" min="0" step="0.01" required value="${product?.stock ?? 0}"></label>
    <label>Low-stock threshold<input name="min_stock" type="number" min="0" step="0.01" required value="${product?.min_stock ?? 10}"></label>
    <label>Expiry date <small>(optional)</small><input name="expires_on" type="date" value="${product?.expiresOn || ''}"></label>
    <label>Supplier <small>(optional)</small><input name="supplier" maxlength="160" value="${escapeHtml(product?.supplierRaw || '')}"></label>
    <div class="form-error" id="entityFormError" role="alert"></div>
    <div class="form-actions"><button class="btn btn-secondary" type="button" onclick="closeModal('entityModal')">Cancel</button><button class="btn btn-primary" type="submit">${product ? 'Save Changes' : 'Create Product'}</button></div>`;
  document.getElementById('entityForm').onsubmit = event => saveProduct(event, productId);
  document.getElementById('productGstRate').addEventListener('change', event => {
    const custom = event.target.value === 'custom';
    const label = document.getElementById('customGstRateLabel');
    const input = document.getElementById('customGstRate');
    label.hidden = !custom;
    input.required = custom;
    if(!custom) input.value = '';
  });
  document.getElementById('entityModal').classList.add('open');
  document.querySelector('#entityForm input').focus();
}

async function saveProduct(event, productId){
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const payload = Object.fromEntries(form.entries());
  if(payload.gst_rate === 'custom') payload.gst_rate = payload.custom_gst_rate;
  payload.gst_selection_type = event.currentTarget.elements.gst_rate.value === 'custom' ? 'custom' : 'preset';
  delete payload.custom_gst_rate;
  payload.description = payload.description.trim() || null;
  payload.catalog_serial = payload.catalog_serial === '' ? null : Number(payload.catalog_serial);
  ['stock','min_stock'].forEach(key => payload[key] = Number(payload[key]));
  ['purchase_price_including_gst','selling_price','gst_rate','discount_percentage'].forEach(key => payload[key] = payload[key] === '' ? null : Number(payload[key]));
  payload.rate = payload.selling_price;
  if(payload.purchase_price_including_gst !== null && payload.gst_rate !== null){
    payload.purchase_price_excluding_gst = Math.round((payload.purchase_price_including_gst / (1 + payload.gst_rate / 100) + Number.EPSILON) * 100) / 100;
    payload.purchase_gst_component = Math.round((payload.purchase_price_including_gst - payload.purchase_price_excluding_gst + Number.EPSILON) * 100) / 100;
  }
  payload.expires_on = payload.expires_on ? `${payload.expires_on}T00:00:00` : null;
  const error = document.getElementById('entityFormError');
  const submit = event.currentTarget.querySelector('[type="submit"]');
  submit.disabled = true;
  try {
    const saved = await apiFetch(productId ? `/products/${productId}` : '/products', {method:productId ? 'PUT' : 'POST', body:payload});
    // A successful write must be visible immediately. Update the local list
    // from the server response, then reconcile it with the canonical list.
    const mapped = mapProduct(saved);
    const existingIndex = products.findIndex(item => item.id === mapped.id);
    if(existingIndex >= 0) products[existingIndex] = mapped;
    else products.push(mapped);
    renderProductsTable(); renderProductGrid();
    await loadProducts();
    renderProductsTable(); renderProductGrid(); renderDashboard();
    closeModal('entityModal'); showToast(productId ? 'Product updated' : 'Product created');
  } catch (errorResponse) { error.textContent = errorResponse.message || 'Could not save product'; }
  finally { submit.disabled = false; }
}

function openFarmerForm(farmerId=null){
  const farmer = farmers.find(item => item.id === farmerId);
  document.getElementById('entityModalTitle').textContent = farmer ? 'Edit Customer' : 'New Customer';
  document.getElementById('entityForm').innerHTML = `
    <label>Name<input name="name" required minlength="2" maxlength="120" value="${escapeHtml(farmer?.nameRaw || '')}"></label>
    <label>Mobile<input name="mobile" required minlength="6" maxlength="30" value="${escapeHtml(farmer?.mobileRaw || '')}"></label>
    <label>Location<input name="location" maxlength="160" value="${escapeHtml(farmer?.locationRaw || '')}"></label>
    <label>Billing address<input name="billing_address" maxlength="500" value="${escapeHtml(farmer?.billingAddress || '')}"></label>
    <label>Delivery address<input name="delivery_address" maxlength="500" value="${escapeHtml(farmer?.deliveryAddress || '')}"></label>
    <label>GSTIN/UIN (optional)<input name="gstin" maxlength="20" value="${escapeHtml(farmer?.gstin || '')}"></label>
    <label>State name<input name="state_name" maxlength="80" value="${escapeHtml(farmer?.stateName || '')}"></label>
    <label>State code<input name="state_code" maxlength="2" value="${escapeHtml(farmer?.stateCode || '')}"></label>
    <div class="form-error" id="entityFormError" role="alert"></div>
    <div class="form-actions"><button class="btn btn-secondary" type="button" onclick="closeModal('entityModal')">Cancel</button><button class="btn btn-primary" type="submit">${farmer ? 'Save Changes' : 'Create Customer'}</button></div>`;
  document.getElementById('entityForm').onsubmit = event => saveFarmer(event, farmerId);
  document.getElementById('entityModal').classList.add('open');
  document.querySelector('#entityForm input').focus();
}

async function saveFarmer(event, farmerId){
  event.preventDefault();
  const payload = Object.fromEntries(new FormData(event.currentTarget).entries());
  const error = document.getElementById('entityFormError');
  const submit = event.currentTarget.querySelector('[type="submit"]');
  submit.disabled = true;
  try {
    await apiFetch(farmerId ? `/farmers/${farmerId}` : '/farmers', {method:farmerId ? 'PUT' : 'POST', body:payload});
    await loadFarmers(); renderFarmers(); renderDashboard(); closeModal('entityModal'); showToast(farmerId ? 'Customer updated' : 'Customer created');
  } catch (errorResponse) { error.textContent = errorResponse.message || 'Could not save customer'; }
  finally { submit.disabled = false; }
}

/* ================= DATA (always loaded from the API; no client-side records) ================= */
let products = [];
let productsLoaded = false;
let productsLoadError = '';
let farmers = [];
let transactions = [];
let recentTransactions = [];
let outstandingRows = [];
let topProducts = [];
let salesChartData = [];
let activeSalesRange = '7d';

/* ================= LOADERS (map backend JSON -> frontend shapes) ================= */
function mapProduct(p){
  const statusMap = {in_stock:'In Stock', low_stock:'Low Stock', out_of_stock:'Out of Stock'};
  const sellingPrice = p.selling_price ?? p.rate ?? null;
  const buyEx = p.purchase_price_excluding_gst ?? null;
  const saleTaxable = sellingPrice == null ? null : (p.selling_price_tax_mode === 'inclusive' && p.gst_rate != null ? Number(sellingPrice) / (1 + Number(p.gst_rate) / 100) : Number(sellingPrice));
  const profitAmount = saleTaxable == null || buyEx == null ? null : saleTaxable - Number(buyEx);
  const profitMargin = profitAmount == null || Number(buyEx) === 0 ? null : profitAmount / Number(buyEx) * 100;
  return {
    id:p.id, name:escapeHtml(p.name), nameRaw:p.name,
    description:escapeHtml(p.description || ''), descriptionRaw:p.description || '',
    catalogSerial:p.catalog_serial ?? null,
    brand:escapeHtml(p.brand || ''), brandRaw:p.brand || '', sku:escapeHtml(p.sku), skuRaw:p.sku,
    category:escapeHtml(p.category), categoryRaw:p.category, unit:escapeHtml(p.unit), unitRaw:p.unit,
    supplier:escapeHtml(p.supplier || ''), supplierRaw:p.supplier || '', isActive:p.is_active !== false,
    price:sellingPrice, sellingPrice, purchasePrice:p.purchase_price_including_gst ?? null,
    purchasePriceExcludingGst:buyEx, profitAmount, profitMargin, hsnCode:p.hsn_code || '',
    discountPercentage:p.discount_percentage ?? null, gstRate:p.gst_rate ?? null,
    gstSelectionType:p.gst_selection_type || 'preset', sellingPriceTaxMode:p.selling_price_tax_mode || 'exclusive',
    packSize:p.pack_size ?? '', stock:p.stock, min_stock:p.min_stock,
    expiresOn:p.expires_on ? String(p.expires_on).slice(0,10) : '', status: statusMap[p.status] || 'In Stock'
  };
}
async function loadProducts(){
  productsLoadError = '';
  try{
    const response = await apiFetch(canViewCosts() ? '/products/internal' : '/products');
    if(!Array.isArray(response)) throw new Error('The products service returned an invalid response');
    products = response.map(mapProduct);
    productsLoaded = true;
    renderProductFilterOptions();
  }catch(error){
    productsLoadError = error.message || 'Could not load products';
    renderDashboardProducts();
    throw error;
  }
}

async function loadFarmers(){
  try{
    const list = await apiFetch('/farmers');
    farmers = list.map(f=>({
      id:f.id, name:escapeHtml(f.name), nameRaw:f.name, mobile:escapeHtml(f.mobile), mobileRaw:f.mobile, location:escapeHtml(f.location||''), locationRaw:f.location||'',
      billingAddress:f.billing_address||'',deliveryAddress:f.delivery_address||'',gstin:f.gstin||'',stateName:f.state_name||'',stateCode:f.state_code||'',
      totalPurchases:f.total_purchases, outstanding:f.outstanding,
      lastPurchase:f.last_purchase ? new Date(f.last_purchase).toLocaleDateString('en-IN',{day:'2-digit',month:'short',year:'numeric'}) : String.fromCharCode(8212)
    }));
  }catch(e){ console.warn('loadFarmers failed', e); }
}

function farmerNameById(id){ const f = farmers.find(x=>x.id===id); return f ? f.name : 'Walk-in Customer'; }

async function loadDashboard(range = activeSalesRange){
  const recentRequest = apiFetch('/dashboard/recent-transactions?limit=6');
  const chartRequest = apiFetch('/dashboard/sales-chart?range='+encodeURIComponent(range));
  try{
    const txns = await recentRequest;
    recentTransactions = txns.map(mapInvoice);
  }catch(e){ console.warn('loadDashboard transactions failed', e); }
  try{
    const chart = await chartRequest;
    salesChartData = chart.map(pt=>({label: new Date(pt.label).toLocaleDateString('en-IN',{weekday:'short'}), val: pt.total}));
    if(!salesChartData.some(d=>d.val>0)){ /* keep zeros -- chart will just render flat */ }
  }catch(e){ console.warn('loadDashboard chart failed', e); }
}
function mapInvoice(invoice){
  return {id:invoice.id, inv:escapeHtml(invoice.invoice_no), farmer:escapeHtml(invoice.farmer_name || 'Walk-in Customer'), items:String.fromCharCode(8212), amount:invoice.grand_total, method:String(invoice.payment_method).toUpperCase(), status:invoice.status, date:invoice.date ? new Date(invoice.date).toLocaleDateString('en-IN',{day:'2-digit',month:'short',year:'numeric'}) : String.fromCharCode(8212)};
}

async function loadInvoices(){
  try { transactions = (await apiFetch('/invoices')).map(mapInvoice); }
  catch(e) { console.warn('loadInvoices failed', e); }
}

async function loadOutstanding(){
  const summaryRequest = apiFetch('/outstanding/summary');
  const rowsRequest = apiFetch('/outstanding');
  try{
    const s = await summaryRequest;
    document.getElementById('oTotalOutstanding').textContent = money(s.total_outstanding);
    document.getElementById('oDueToday').textContent = money(s.due_today);
    document.getElementById('oOverdue').textContent = money(s.overdue);
    document.getElementById('oCustomersWithDues').textContent = s.customers_with_dues;
  }catch(e){ console.warn('loadOutstanding summary failed', e); }
  try{
    const rows = await rowsRequest;
    outstandingRows = rows.map(r=>({
      id: r.id, farmer: r.farmer_name || 'Walk-in Customer', inv: r.invoice_no,
      date: new Date(r.date).toLocaleDateString('en-IN',{day:'2-digit',month:'short',year:'numeric'}),
      total: r.grand_total, paid: r.amount_paid, balance: r.balance_due,
      status: r.status
    })).map(r=>({...r, farmer:escapeHtml(r.farmer), inv:escapeHtml(r.inv)}));
  }catch(e){ console.warn('loadOutstanding rows failed', e); }
}

function loadReadNotifications(){
  try { readNotificationIds = new Set(JSON.parse(localStorage.getItem('adinath-read-notifications') || '[]')); }
  catch(e) { readNotificationIds = new Set(); }
}
function persistReadNotifications(){
  try { localStorage.setItem('adinath-read-notifications', JSON.stringify([...readNotificationIds])); }
  catch(e) { console.warn('Failed to persist notification read state', e); }
}
async function loadNotifications(){
  if(notificationsLoading) return;
  notificationsLoading = true;
  notificationsLoadFailed = false;
  renderNotifications();
  try { notifications = await apiFetch('/notifications'); }
  catch(e) { notificationsLoadFailed = true; console.warn('loadNotifications failed', e); }
  finally { notificationsLoading = false; renderNotifications(); }
}
function notificationIcon(type){
  const icons={low_stock:'triangle-alert',expiry:'calendar-days',payment:'wallet',sale:'receipt-text',system:'circle-check'};
  return `<i data-lucide="${icons[type]||icons.system}" class="lucide-icon" aria-hidden="true"></i>`;
}
function renderNotifications(){
  const unread = notifications.filter(item => !readNotificationIds.has(item.id));
  const badge = document.getElementById('notificationBadge');
  badge.hidden = unread.length === 0;
  const count = document.getElementById('notificationCount');
  count.textContent = notificationsLoadFailed ? 'Notifications could not be loaded' : (unread.length ? `${unread.length} new notification${unread.length === 1 ? '' : 's'}` : "You're all caught up");
  const trigger = document.getElementById('notificationTrigger');
  trigger.setAttribute('aria-label', unread.length ? `Notifications, ${unread.length} unread` : 'Notifications');
  const list = document.getElementById('notificationList');
  list.setAttribute('aria-busy', String(notificationsLoading));
  if(notificationsLoading){
    list.innerHTML = '<div class="notification-state" role="status"><span class="notification-spinner" aria-hidden="true"></span>Loading notifications...</div>';
    return;
  }
  if(notificationsLoadFailed){
    list.innerHTML = '<div class="notification-state notification-error" role="alert"><span>Could not load notifications. Check the connection and try again.</span><button class="btn btn-secondary" type="button" onclick="loadNotifications()">Try again</button></div>';
    return;
  }
  list.innerHTML = notifications.length ? notifications.map(item => `
    <button class="notification-item ${escapeHtml(item.severity)} ${readNotificationIds.has(item.id) ? 'is-read' : ''}" type="button" data-notification-id="${escapeHtml(item.id)}">
      <span class="notification-icon">${notificationIcon(item.type)}</span><span class="notification-copy"><strong>${escapeHtml(item.title)}</strong><span>${escapeHtml(item.message)}</span><time>${new Date(item.created_at).toLocaleString('en-IN',{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'})}</time></span>
      ${readNotificationIds.has(item.id) ? '' : '<i class="notification-unread" aria-label="Unread"></i>'}
    </button>`).join('') : '<div class="notification-empty">No notifications right now.</div>';
}
document.getElementById('notificationList').addEventListener('click', event=>{
  const item=event.target.closest('.notification-item[data-notification-id]');
  if(item) openNotification(item.dataset.notificationId);
});
// The header uses backdrop-filter, which creates a stacking context and a fixed
// containing block. Portal the panel to body so it can layer over page content.
const notificationCenter = document.getElementById('notificationCenter');
if(notificationCenter) document.body.append(notificationCenter);
function positionNotificationCenter(){
  const center = document.getElementById('notificationCenter');
  const trigger = document.getElementById('notificationTrigger');
  if(!center || !trigger) return;
  const triggerRect = trigger.getBoundingClientRect();
  const mobile = window.matchMedia('(max-width:760px)').matches;
  const headerBottom = document.querySelector('.topbar')?.getBoundingClientRect().bottom ?? triggerRect.bottom;
  center.style.top = `${Math.max(triggerRect.bottom + 8, headerBottom + (mobile ? 8 : 12))}px`;
  if(mobile){
    center.style.left = '12px';
    center.style.right = '12px';
  } else {
    const panelWidth = Math.min(390, window.innerWidth - 32);
    const left = Math.max(16, Math.min(triggerRect.right - panelWidth, window.innerWidth - panelWidth - 16));
    center.style.left = `${left}px`;
    center.style.right = 'auto';
  }
}
function toggleNotifications(){
  const center = document.getElementById('notificationCenter');
  const opening = center.hidden;
  if(opening) positionNotificationCenter();
  center.hidden = !opening;
  document.getElementById('notificationTrigger').setAttribute('aria-expanded', String(opening));
  if(opening) loadNotifications();
}
function closeNotifications(restoreFocus=false){
  const center = document.getElementById('notificationCenter');
  if(!center || center.hidden) return;
  center.hidden = true;
  document.getElementById('notificationTrigger').setAttribute('aria-expanded', 'false');
  if(restoreFocus) document.getElementById('notificationTrigger').focus();
}
document.addEventListener('pointerdown', event=>{
  const center = document.getElementById('notificationCenter');
  const trigger = document.getElementById('notificationTrigger');
  if(center && !center.hidden && !center.contains(event.target) && !trigger?.contains(event.target)) closeNotifications();
});
document.addEventListener('keydown', event=>{
  if(event.key === 'Escape') closeNotifications(true);
});
window.addEventListener('resize', ()=>{
  const center = document.getElementById('notificationCenter');
  if(center && !center.hidden) positionNotificationCenter();
}, {passive:true});
function markAllNotificationsRead(){
  notifications.forEach(item => readNotificationIds.add(item.id));
  persistReadNotifications();
  renderNotifications();
}
function announcePriorityNotification(){
  const urgent = notifications.find(item => !readNotificationIds.has(item.id) && (item.severity === 'critical' || item.severity === 'warning'));
  if(!urgent || sessionStorage.getItem(`adinath-announced-${urgent.id}`)) return;
  sessionStorage.setItem(`adinath-announced-${urgent.id}`, '1');
  showToast(`${urgent.title} - open details`, urgent.severity, () => openNotification(urgent.id));
}
function openNotification(id){
  const notification = notifications.find(item => item.id === id);
  if(!notification) return;
  readNotificationIds.add(id); persistReadNotifications(); renderNotifications();
  closeNotifications();
  if(notification.action_view) goTo(notification.action_view);
}
async function loadLegacyReportAnalytics(){
  try{
    const [top, methods, performance, overview] = await Promise.all([
      apiFetch('/reports/top-products?limit=5'), apiFetch('/reports/payment-methods'), apiFetch('/reports/sales-performance'), apiFetch('/reports/overview')
    ]);
    const maxTotal = Math.max(...top.map(t=>t.total), 1);
    topProducts = top.map(t=>({name:escapeHtml(t.name), pct: Math.round((t.total/maxTotal)*100), val:money(t.total)}));
    renderReportCharts(methods, performance);
    renderReportOverview(overview);
  }catch(e){ console.warn('loadReports failed', e); }
}

async function loadAll(){
  await loadProducts();
  await Promise.all([loadFarmers(), loadInvoices(), loadDashboard(), loadOutstanding(), loadNotifications()]);
}

function loadNonCriticalData(){
  if (backgroundDataPromise) return backgroundDataPromise;
  // Products power the Products, Inventory, Billing, and dashboard views. Do
  // not make them wait for unrelated background requests such as receivables
  // or notifications before showing the catalogue to the user.
  const productsReady = loadProducts().then(() => {
    renderProductsTable();
    renderDashboard();
    renderProductGrid();
    renderBill();
  });
  const supportingDataReady = Promise.all([loadFarmers(), loadInvoices(), loadOutstanding(), loadNotifications()])
    .then(() => {
      renderFarmers();
      renderDashboard();
      announcePriorityNotification();
    });
  backgroundDataPromise = Promise.all([productsReady, supportingDataReady])
    .catch(error => console.warn('Background data refresh failed', error));
  return backgroundDataPromise;
}

async function bootApp(){
  loadReadNotifications();
  startTopbarClock();
  applyPermissions();
  applySavedTheme();
  document.querySelectorAll('.metric-card, .panel').forEach(element => element.classList.add('skeleton'));
  setupDashboardLinks();
  // Start the catalog request as part of app boot so Products is populated
  // even when optional background refresh work is delayed or interrupted.
  const productCatalogReady = loadProducts()
    .then(() => renderProductsTable())
    .catch(error => {
      console.warn('Product catalog failed to load during app startup', error);
      renderDashboardProducts();
    });
  await Promise.all([loadDashboard(), productCatalogReady]);
  document.querySelectorAll('.skeleton').forEach(element => element.classList.remove('skeleton'));
  renderDashboard();
  renderProductGrid();
  renderBill();
  playDashboardEntrance();
  void loadNonCriticalData();
  if(isAdmin()) loadBusinessSettings().catch(error=>console.warn('Business settings could not load',error));
}

function setupDashboardLinks(){
  const targets = ['reports', 'sales', 'farmers', 'products'];
  document.querySelectorAll('#view-dashboard .metric-card').forEach((card, index) => {
    // bootApp can run again after sign-in; bind these handlers only once.
    if(card.dataset.navigationBound === 'true') return;
    card.dataset.navigationBound = 'true';
    card.tabIndex = 0;
    card.setAttribute('role', 'button');
    card.setAttribute('aria-label', `Open ${titles[targets[index]]}`);
    const openTarget = () => goTo(targets[index]);
    card.addEventListener('click', openTarget);
    card.addEventListener('keydown', event => { if(event.key === 'Enter' || event.key === ' ') { event.preventDefault(); openTarget(); } });
  });
}

function toggleSidebar(){
  const app = document.getElementById('mainApp');
  if(window.matchMedia('(max-width: 760px)').matches){
    const open = app.classList.toggle('sidebar-open');
    document.body.classList.toggle('nav-open', open);
    return;
  }
  const collapsed = app.classList.toggle('sidebar-collapsed');
  localStorage.setItem('adinath-sidebar-collapsed', collapsed ? 'true' : 'false');
}

function toggleTheme(){
  const dark = document.body.classList.toggle('dark-theme');
  localStorage.setItem('adinath-theme', dark ? 'dark' : 'light');
  syncThemeToggle();
}

function syncThemeToggle(){
  const button = document.querySelector('.theme-toggle');
  if(!button) return;
  const dark = document.body.classList.contains('dark-theme');
  const label = dark ? 'Switch to light mode' : 'Switch to dark mode';
  button.setAttribute('aria-label', label);
  button.setAttribute('title', label);
  button.setAttribute('aria-pressed', String(dark));
}

function applySavedTheme(){
  document.body.classList.toggle('dark-theme', localStorage.getItem('adinath-theme') === 'dark');
  document.getElementById('mainApp').classList.toggle('sidebar-collapsed', localStorage.getItem('adinath-sidebar-collapsed') === 'true');
  document.querySelectorAll('.nav-item').forEach(item => item.title = item.textContent.trim());
  syncThemeToggle();
}

/* ================= NAVIGATION ================= */
const titles = {dashboard:"Dashboard", billing:"Billing", products:"Products", inventory:"Inventory", farmers:"Customers", sales:"Sales & Invoices", purchases:"Purchases", outstanding:"Receivables", reports:"Reports", accounting:"CA Accounting", settings:"Account Settings"};
let viewHistory = ['dashboard'];

function updateBackButton(){
  const button = document.getElementById('appBackBtn');
  button.disabled = viewHistory.length < 2;
  button.title = button.disabled ? 'No previous section' : `Back to ${titles[viewHistory[viewHistory.length - 2]]}`;
}

function goTo(view, recordHistory = true){
  if(view === 'reports') {
    buildReportsDashboard();
    if (reportsLoaded && selectedReport) {
      renderSelectedReport(selectedReport);
      document.getElementById('reportContent').hidden = false;
      document.getElementById('reportLoading').hidden = true;
    } else {
      loadReports().catch(error => console.warn('Deferred report load failed', error));
    }
  }
  if(view === 'purchases' && canViewCosts()) loadPurchases().catch(error=>console.warn('Deferred purchase load failed',error));
  if(view === 'accounting' && canViewCosts()) loadAccountingRows().catch(error=>showToast(error.message||'Could not load accounting report','critical'));
  if(view === 'settings' && isAdmin()) loadBusinessSettings().catch(error=>console.warn('Business settings could not load',error));
  const currentView = viewHistory[viewHistory.length - 1];
  if(recordHistory && view !== currentView) viewHistory.push(view);
  document.querySelectorAll('.view').forEach(v=>v.classList.remove('active'));
  document.getElementById('view-'+view).classList.add('active');
  document.querySelectorAll('.nav-item').forEach(n=>n.classList.toggle('active', n.dataset.view===view));
  document.querySelectorAll('.mobile-nav-item').forEach(n=>n.classList.toggle('active', n.dataset.view===view));
  document.getElementById('pageTitle').textContent = titles[view];
  document.body.dataset.activeView = view;
  document.getElementById('mainApp').classList.remove('sidebar-open');
  document.body.classList.remove('nav-open');
  if(view === 'dashboard') playDashboardEntrance();
  updateBackButton();
  document.querySelector('.content').scrollTo({top:0, behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'});
}
function goBack(){
  if(viewHistory.length < 2) return;
  viewHistory.pop();
  goTo(viewHistory[viewHistory.length - 1], false);
}
updateBackButton();
document.querySelectorAll('.nav-item, .mobile-nav-item').forEach(item=>{
  item.addEventListener('click', ()=> goTo(item.dataset.view));
});

function playDashboardEntrance(){
  if(window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  const dashboard = document.getElementById('view-dashboard');
  dashboard.classList.remove('dashboard-enter');
  void dashboard.offsetWidth;
  dashboard.classList.add('dashboard-enter');
}

document.querySelectorAll('.range-tab[data-range]').forEach(button => {
  button.addEventListener('click', async () => {
    const range = button.dataset.range;
    if(range === activeSalesRange) return;
    activeSalesRange = range;
    document.querySelectorAll('.range-tab[data-range]').forEach(tab => tab.classList.toggle('active', tab === button));
    const chart = document.getElementById('salesChart');
    chart.classList.add('is-loading');
    chart.setAttribute('aria-busy', 'true');
    try{
      await loadDashboard(range);
      renderDashboard();
    }finally{
      chart.classList.remove('is-loading');
      chart.removeAttribute('aria-busy');
    }
  });
});

/* ================= HELPERS ================= */
const inrFormatter = new Intl.NumberFormat('en-IN', {
  style: 'currency', currency: 'INR', currencyDisplay: 'narrowSymbol',
  minimumFractionDigits: 2, maximumFractionDigits: 2
});
function money(n){ return inrFormatter.format(Number(n) || 0); }
function deleteIcon(){ return '<i data-lucide="trash-2" class="lucide-icon" aria-hidden="true"></i>'; }
function statusBadge(status){
  const map = {
    paid:['badge-paid','Paid'], partial:['badge-partial','Partial'], credit:['badge-credit','Credit'],
    'In Stock':['badge-in','In Stock'], 'Low Stock':['badge-low','Low Stock'], 'Out of Stock':['badge-out','Out of Stock'],
    Normal:['badge-in','Normal'], 'Expiring Soon':['badge-partial','Expiring Soon'], Expired:['badge-out','Expired'],
    overdue:['badge-credit','Overdue'], 'due-today':['badge-partial','Due Today']
  };
  const m = map[status] || ['badge-in', status];
  return `<span class="badge ${m[0]}">${m[1]}</span>`;
}

function productExpiryStatus(product){
  if(!product.expiresOn) return 'Normal';
  const [year, month, day] = product.expiresOn.split('-').map(Number);
  const expiry = Date.UTC(year, month - 1, day);
  const now = Date.now();
  if(expiry <= now) return 'Expired';
  return expiry <= now + 30 * 24 * 60 * 60 * 1000 ? 'Expiring Soon' : 'Normal';
}

function formatProductExpiry(product){
  if(!product.expiresOn) return '—';
  const [year, month, day] = product.expiresOn.split('-');
  return `${day}/${month}/${year}`;
}

/* ================= DASHBOARD RENDER ================= */
function renderDashboard(){
  const totalSales = transactions.reduce((sum, invoice) => sum + (Number(invoice.amount) || 0), 0);
  document.getElementById('mTotalSales').textContent = money(totalSales);
  document.getElementById('mTotalInvoices').textContent = String(transactions.length);
  document.getElementById('mTotalCustomers').textContent = String(farmers.length);
  document.getElementById('mTotalProducts').textContent = productsLoaded ? String(products.length) : '…';
  renderDashboardProducts();
  const chart = document.getElementById('salesChart');
  if(!salesChartData.length){
    chart.innerHTML = '<div class="dashboard-chart-empty"><i data-lucide="chart-no-axes-combined" class="lucide-icon" aria-hidden="true"></i><strong>No sales data yet</strong><span>Sales will appear after you create invoices.</span></div>';
  }else{
    const width=900, height=230, left=50, right=12, top=12, bottom=34;
    const plotWidth=width-left-right, plotHeight=height-top-bottom;
    const values=salesChartData.map(point=>Math.max(0,Number(point.val)||0));
    const maxValue=Math.max(...values,1), axisMax=maxValue*1.16;
    const points=values.map((value,index)=>({
      x:left+(salesChartData.length===1?plotWidth/2:index/(salesChartData.length-1)*plotWidth),
      y:top+(1-value/axisMax)*plotHeight,
      value,
      label:salesChartData[index].label
    }));
    const linePath=points.reduce((path,point,index)=>{
      if(index===0) return `M ${point.x} ${point.y}`;
      const previous=points[index-1], controlX=(previous.x+point.x)/2;
      return `${path} C ${controlX} ${previous.y}, ${controlX} ${point.y}, ${point.x} ${point.y}`;
    },'');
    const areaPath=`${linePath} L ${points[points.length-1].x} ${top+plotHeight} L ${points[0].x} ${top+plotHeight} Z`;
    const compact=value=>value>=100000?`${(value/100000).toFixed(1).replace(/\.0$/,'')}L`:value>=1000?`${Math.round(value/1000)}K`:String(Math.round(value));
    const grid=[0,1,2,3].map(index=>{
      const ratio=index/3, y=top+ratio*plotHeight, label=compact(axisMax*(1-ratio));
      return `<g class="chart-grid-row"><line x1="${left}" y1="${y}" x2="${width-right}" y2="${y}"/><text x="${left-10}" y="${y+4}" text-anchor="end">${label}</text></g>`;
    }).join('');
    const labelStep=Math.max(1,Math.ceil(points.length/6));
    const labels=points.map((point,index)=>index===0||index===points.length-1||index%labelStep===0?`<text class="chart-x-label" x="${point.x}" y="${height-8}" text-anchor="middle">${escapeHtml(point.label)}</text>`:'').join('');
    const dots=points.map(point=>`<circle class="chart-point" cx="${point.x}" cy="${point.y}" r="3.6" tabindex="0" aria-label="${escapeHtml(point.label)}: ${money(point.value)}"><title>${escapeHtml(point.label)}: ${money(point.value)}</title></circle>`).join('');
    chart.innerHTML=`<svg class="sales-line-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="Sales over the selected period"> <defs><linearGradient id="salesAreaFill" x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stop-color="var(--chart-line)" stop-opacity=".23"/><stop offset="100%" stop-color="var(--chart-line)" stop-opacity=".015"/></linearGradient></defs>${grid}<path class="chart-area" d="${areaPath}"/><path class="chart-line" d="${linePath}"/>${dots}${labels}</svg>`;
  }

  const low = products.filter(p=>p.status!=='In Stock').slice(0,5);
  document.getElementById('stockAlertList').innerHTML = low.length ? low.map(p=>`
    <div class="alert-row">
      <div class="alert-dot" style="background:${p.status==='Out of Stock' ? 'var(--danger)':'var(--earth)'}"></div>
      <div><div class="alert-name">${p.name}</div><div class="alert-sub">${p.category} · ${p.sku}</div></div>
      <div class="alert-qty">${p.stock} left</div>
    </div>`).join('') : empty('<h3>No stock alerts</h3><p>Products with low stock will appear here.</p>');

  document.getElementById('recentTxnTable').innerHTML = `
    <thead><tr><th>Invoice</th><th class="invoice-customer-cell">Customer</th><th>Amount</th><th>Status</th><th>Date</th><th><span class="visually-hidden">Action</span></th></tr></thead>
    <tbody>${recentTransactions.length ? recentTransactions.slice(0,4).map(t=>`
      <tr><td class="cell-strong">${t.inv}</td><td class="invoice-customer-cell">${t.farmer}</td><td class="cell-num">${money(t.amount)}</td><td>${statusBadge(t.status)}</td><td class="cell-muted">${t.date}</td><td><button class="invoice-open-btn" type="button" onclick="openInvoicePdf(${t.id})" aria-label="View invoice ${t.inv}" title="View invoice"><i data-lucide="eye" class="lucide-icon" aria-hidden="true"></i></button></td></tr>`).join('') : '<tr><td colspan="6" class="cell-muted dashboard-no-invoices">No invoices yet. Create an invoice to see transactions here.</td></tr>'}
    </tbody>`;
}

function renderDashboardProducts(){
  const list = document.getElementById('dashboardProductList');
  const count = document.getElementById('dashboardProductPreviewCount');
  if(!list || !count) return;
  count.textContent = productsLoaded ? String(products.length) : '…';
  if(!productsLoaded){
    list.innerHTML = productsLoadError
      ? `<div class="dashboard-product-empty"><i data-lucide="triangle-alert" class="lucide-icon" aria-hidden="true"></i><span>Products could not load.</span><button class="btn btn-secondary" type="button" onclick="retryProductLoad(this)">Retry</button></div>`
      : '<div class="dashboard-product-empty"><span class="product-loading-dot"></span><span>Loading your product catalog…</span></div>';
    return;
  }
  if(!products.length){
    list.innerHTML = `<div class="dashboard-product-empty"><i data-lucide="package-open" class="lucide-icon" aria-hidden="true"></i><span>No products in your catalog yet.</span>${isAdmin() ? '<button class="btn btn-primary" type="button" onclick="openProductForm()">Add product</button>' : ''}</div>`;
    return;
  }
  list.innerHTML = products.slice(0,4).map(product=>{
    const categoryText = product.category || 'Product';
    const icon = /fertili|seed/i.test(categoryText) ? 'sprout' : 'package';
    const stockClass = product.status === 'Out of Stock' ? 'is-out' : product.status === 'Low Stock' ? 'is-low' : 'is-in';
    const stock = Number(product.stock) || 0;
    const stockText = stock === 1 ? `1 ${product.unit} left` : `${stock} ${product.unit} left`;
    return `<button class="dashboard-product-item" type="button" onclick="goTo('products')" aria-label="View ${escapeHtml(product.nameRaw)} in Products">
      <span class="dashboard-product-icon"><i data-lucide="${icon}" class="lucide-icon" aria-hidden="true"></i></span>
      <span class="dashboard-product-copy"><strong>${product.name}</strong><small>${categoryText} · ${product.sku}</small></span>
      <span class="dashboard-product-summary"><strong>${product.price == null ? 'Price not set' : money(product.price)}</strong><small class="${stockClass}">${stockText}</small></span>
    </button>`;
  }).join('');
}

async function retryProductLoad(button){
  if(button) button.disabled = true;
  try{
    await loadProducts();
    renderProductsTable();
    renderDashboard();
    renderProductGrid();
    renderBill();
  }catch(error){
    showToast(error.message || 'Could not load products', 'critical');
  }
}

function renderReportCharts(methods, performance){
  reportPerformance = performance;
  renderReportSparklines();
  const sales = document.getElementById('salesPerformance');
  sales.innerHTML = performance.length ? performance.map(point => `<div class="sum-row"><span>${new Date(point.label).toLocaleDateString('en-IN', {day:'2-digit', month:'short'})}</span><span class="cell-num">${money(point.total)}</span></div>`).join('') : '<div class="empty-state"><h3>No sales data yet</h3><p>Create invoices to populate this report.</p></div>';
  const payment = document.getElementById('paymentMethodsReport');
  payment.innerHTML = methods.length ? methods.map(item => `<div class="pm-legend-row"><span class="pm-name">${escapeHtml(item.method.toUpperCase())}</span><span class="pm-val">${money(item.total)} (${item.percent}%)</span></div>`).join('') : '<div class="empty-state"><h3>No payments recorded yet</h3><p>Payment totals will appear after invoices are created.</p></div>';
}

/* ================= PRODUCTS / INVENTORY TABLES ================= */
function renderProductsTable(visibleProducts=products){
  const costHead = canViewCosts() ? '<th class="cost-only">Buying Price (incl. GST)</th><th class="cost-only">Margin</th>' : '';
  const costCell = p => canViewCosts() ? `<td class="cell-num cost-only">${p.purchasePrice == null ? 'Not Set' : money(p.purchasePrice)}</td><td class="cell-num cost-only">${p.profitAmount == null ? '?' : `<strong>${money(p.profitAmount)}</strong><small class="table-subline">${p.profitMargin==null?'?':`${p.profitMargin.toFixed(1)}% on cost`}</small>`}</td>` : '';
  const productLabel = p => `${p.name}<small class="table-subline">${p.catalogSerial ? `PDF serial ${p.catalogSerial} | ` : ''}${p.packSize ? escapeHtml(p.packSize) : ''}</small>${p.description ? `<small class="table-subline">${p.description}</small>` : ''}`;
  const rows = visibleProducts.map(p=>`
    <tr><td class="cell-strong product-name-cell">${productLabel(p)}</td><td>${p.brand || '?'}</td><td class="cell-muted">${p.category}</td>${costCell(p)}<td class="cell-num">${p.price == null ? '<span class="price-review">Selling price needs review</span>' : money(p.price)}</td><td>${escapeHtml(p.hsnCode||'?')}</td>
    <td>${p.unit}</td><td class="cell-muted">${p.sku}</td><td>${p.supplier || '?'}</td><td>${formatProductExpiry(p)}</td><td class="cell-num">${p.stock} ${p.unit}</td>
    <td class="cell-num">${p.min_stock}</td><td>${statusBadge(p.status)}</td><td>${statusBadge(productExpiryStatus(p))}</td>
    <td class="product-row-actions"><button class="btn btn-ghost" type="button" style="padding:4px;" onclick="openProductDetails(${p.id})" aria-label="View ${p.name}"><i data-lucide="eye" class="lucide-icon" aria-hidden="true"></i></button><button class="btn btn-ghost admin-only" type="button" style="padding:4px;" onclick="openProductForm(${p.id})" aria-label="Edit ${p.name}"><i data-lucide="pencil" class="lucide-icon" aria-hidden="true"></i></button><button class="btn btn-ghost admin-only" type="button" style="padding:4px;" onclick="openStockMovementForm(${p.id},'PURCHASE')" aria-label="Receive stock for ${p.name}"><i data-lucide="package-plus" class="lucide-icon" aria-hidden="true"></i></button><button class="btn btn-danger-icon admin-only" type="button" onclick="deleteProduct(${p.id}, '${p.nameRaw.replace(/'/g, "\\'")}')" aria-label="Delete ${p.name}">${deleteIcon()}</button></td></tr>`).join('');
  const productColspan = canViewCosts() ? 16 : 14;
  document.getElementById('productsTable').innerHTML = `
    <thead><tr><th>Product</th><th>Brand</th><th>Category</th>${costHead}<th>Selling Price (master)</th><th>HSN/SAC</th><th>Unit</th><th>SKU</th><th>Supplier</th><th>Expiry Date</th><th>Current Stock</th><th>Low Stock Threshold</th><th>Stock Status</th><th>Expiry Status</th><th>Actions</th></tr></thead>
    <tbody>${rows || `<tr><td colspan="${productColspan}" class="cell-muted">No products match these filters.</td></tr>`}</tbody>`;
  if(!isAdmin()) document.querySelectorAll('#productsTable .admin-only').forEach(element => { element.hidden = true; });
  const inventoryRows=products.map(p=>`<tr><td class="cell-strong">${productLabel(p)}</td><td>${p.brand || '?'}</td><td>${p.category}</td>${costCell(p)}<td class="cell-num">${p.price == null ? '<span class="price-review">Needs review</span>' : money(p.price)}</td><td>${escapeHtml(p.hsnCode||'?')}</td><td>${p.unit}</td><td class="cell-muted">${p.sku}</td><td>${p.supplier || '?'}</td><td>${formatProductExpiry(p)}</td><td class="cell-num">${p.stock} ${p.unit}</td><td class="cell-num">${p.min_stock}</td><td>${statusBadge(p.status)}</td><td>${statusBadge(productExpiryStatus(p))}</td><td class="product-row-actions"><button class="btn btn-ghost" type="button" onclick="openProductDetails(${p.id})" aria-label="View ${p.name}"><i data-lucide="eye" class="lucide-icon" aria-hidden="true"></i></button><button class="btn btn-ghost admin-only" type="button" onclick="openStockMovementForm(${p.id},'PURCHASE')" aria-label="Receive stock for ${p.name}"><i data-lucide="package-plus" class="lucide-icon" aria-hidden="true"></i></button></td></tr>`).join('');
  document.getElementById('inventoryTable').innerHTML = `
    <thead><tr><th>Product</th><th>Brand</th><th>Category</th>${costHead}<th>Selling Price (master)</th><th>HSN/SAC</th><th>Unit</th><th>SKU</th><th>Supplier</th><th>Expiry Date</th><th>Current Stock</th><th>Low Stock Threshold</th><th>Stock Status</th><th>Expiry Status</th><th>Actions</th></tr></thead>
    <tbody>${inventoryRows || '<tr><td colspan="16" class="cell-muted">No inventory yet. Add a product to begin.</td></tr>'}</tbody>`;
  applyPermissions();
}

function renderProductFilterOptions(){
  const category=document.getElementById('productCategoryFilter');
  const gst=document.getElementById('productGstFilter');
  if(!category||!gst) return;
  const previousCategory=category.value, previousGst=gst.value;
  const categories=[...new Set(products.map(p=>p.categoryRaw).filter(Boolean))].sort((a,b)=>a.localeCompare(b));
  const rates=[...new Set(products.map(p=>p.gstRate==null?'unset':Number(p.gstRate)))].sort((a,b)=>a==='unset'?1:b==='unset'?-1:a-b);
  category.innerHTML='<option value="">All categories</option>'+categories.map(value=>`<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join('');
  gst.innerHTML='<option value="">All GST rates</option>'+rates.map(value=>value==='unset'?'<option value="unset">GST not set</option>':`<option value="${value}">${value}% GST</option>`).join('');
  category.value=categories.includes(previousCategory)?previousCategory:'';
  gst.value=rates.some(value=>String(value)===previousGst)?previousGst:'';
}

async function openProductDetails(productId){
  const p=products.find(item=>item.id===productId);
  if(!p) return;
  const buy=canViewCosts()?(p.purchasePrice==null?'Not set':money(p.purchasePrice)):'Restricted';
  const margin=canViewCosts()?(p.profitAmount==null?'Not available':`${money(p.profitAmount)} (${p.profitMargin==null?'margin unavailable':`${p.profitMargin.toFixed(1)}% on cost`})`):'Restricted';
  const rows=[['Description',p.descriptionRaw||'Not provided in the PDF'],['PDF serial number',p.catalogSerial??'Not listed'],['Brand',p.brand||'Not specified'],['Category',p.categoryRaw],['SKU',p.skuRaw],['Packaging',p.packSize||p.unitRaw],['HSN/SAC',p.hsnCode||'Not set'],['GST',p.gstRate==null?'Not set':`${p.gstRate}%`],['Buying price (incl. GST)',buy],['Selling price (master)',p.price==null?'Needs review - missing from the PDF':money(p.price)],['Profit / margin',margin],['Available stock',`${p.stock} ${p.unitRaw}`],['Low-stock threshold',`${p.min_stock} ${p.unitRaw}`],['Supplier',p.supplierRaw||'Not specified']];
  const body=document.getElementById('productDetailsBody');
  body.innerHTML=`<div class="product-detail-hero"><span class="product-detail-icon"><i data-lucide="package" class="lucide-icon" aria-hidden="true"></i></span><div><span class="product-detail-kicker">PRODUCT MASTER</span><h2>${p.name}</h2><p>${statusBadge(p.status)} <span>${p.sku}</span></p></div></div><dl class="product-detail-grid">${rows.map(([label,value])=>`<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(String(value))}</dd></div>`).join('')}</dl><div class="product-history-head"><div><h3>Stock movement history</h3><p>Purchases, sales and inventory adjustments</p></div>${isAdmin()?`<button class="btn btn-secondary" type="button" onclick="openStockMovementForm(${p.id},'PURCHASE')">Receive stock</button>`:''}</div><div id="productMovementHistory" class="product-movement-history"><div class="empty-state">Loading stock history?</div></div>`;
  document.getElementById('productDetailsTitle').textContent=p.nameRaw;
  document.getElementById('productDetailsModal').classList.add('open');
  if(window.lucide) window.lucide.createIcons();
  try{
    const movements=await apiFetch(`/products/${p.id}/stock-movements`);
    const history=document.getElementById('productMovementHistory');
    if(!history) return;
    history.innerHTML=movements.length?`<div class="table-wrap"><table><thead><tr><th>Date</th><th>Activity</th><th>In</th><th>Out</th><th>Balance</th><th>Reference</th></tr></thead><tbody>${movements.map(m=>`<tr><td>${new Date(m.movement_date).toLocaleString('en-IN')}</td><td>${escapeHtml(m.movement_type.replaceAll('_',' '))}<small class="table-subline">${escapeHtml(m.notes||'')}</small></td><td>${m.quantity_in}</td><td>${m.quantity_out}</td><td>${m.new_stock} ${p.unitRaw}</td><td>${escapeHtml(m.reference_number||'?')}</td></tr>`).join('')}</tbody></table></div>`:'<div class="product-history-empty">No stock movements recorded.</div>';
  }catch(error){const history=document.getElementById('productMovementHistory');if(history)history.innerHTML=`<div class="product-history-empty">${escapeHtml(error.message||'Could not load stock history')}</div>`;}
}

function openStockMovementForm(productId=null, movementType='PURCHASE'){
  if(!isAdmin()){showToast('Only an administrator can manage stock','critical');return;}
  const product=products.find(item=>item.id===productId);
  const purchase=movementType==='PURCHASE';
  const options=products.filter(item=>item.isActive).map(item=>`<option value="${item.id}" ${item.id===productId?'selected':''}>${item.nameRaw}${item.brandRaw?` ? ${item.brandRaw}`:''} (${item.stock} ${item.unitRaw})</option>`).join('');
  document.getElementById('entityModalTitle').textContent=purchase?'Record Purchase':'Adjust Stock';
  document.getElementById('entityForm').innerHTML=`
    <label>Product<select name="product_id" required ${product?'disabled':''}>${options}</select></label>
    ${product?`<input type="hidden" name="locked_product_id" value="${product.id}">`:''}
    <label>Movement type<select name="movement_type" id="stockMovementType"><option value="PURCHASE" ${purchase?'selected':''}>Purchase / stock in</option><option value="ADJUSTMENT_IN" ${movementType==='ADJUSTMENT_IN'?'selected':''}>Stock adjustment in</option><option value="ADJUSTMENT_OUT" ${movementType==='ADJUSTMENT_OUT'?'selected':''}>Stock adjustment out</option></select></label>
    <label>Quantity (${product?.unitRaw||'product unit'})<input name="quantity" type="number" min="0.01" step="0.01" required></label>
    <label id="stockPurchasePriceLabel">Actual buying rate per unit (incl. GST)<input name="unit_purchase_price" type="number" min="0" step="0.01" value="${product?.purchasePrice??''}" ${purchase?'required':''}><small>Defaults to the product master buying price. This purchase rate is saved separately.</small></label>
    <label id="stockSupplierLabel">Supplier<input name="supplier" maxlength="160" value="${escapeHtml(product?.supplierRaw||'')}"></label>
    <label id="stockInvoiceLabel">Supplier invoice / reference<input name="reference_number" maxlength="120" placeholder="Optional supplier invoice number"></label>
    <label id="stockDateLabel">Purchase date<input name="purchase_date" type="date" value="${new Date().toISOString().slice(0,10)}"></label>
    <label>Notes<input name="notes" maxlength="500" placeholder="Optional note"></label>
    <div class="form-error" id="entityFormError" role="alert"></div>
    <div class="form-actions"><button class="btn btn-secondary" type="button" onclick="closeModal('entityModal')">Cancel</button><button class="btn btn-primary" type="submit">${purchase?'Save Purchase':'Save Adjustment'}</button></div>`;
  const select=document.getElementById('stockMovementType');
  const priceLabel=document.getElementById('stockPurchasePriceLabel'),supplierLabel=document.getElementById('stockSupplierLabel'),invoiceLabel=document.getElementById('stockInvoiceLabel'),dateLabel=document.getElementById('stockDateLabel');
  const refresh=()=>{const isPurchase=select.value==='PURCHASE';[priceLabel,supplierLabel,invoiceLabel,dateLabel].forEach(label=>label.hidden=!isPurchase);document.querySelector('#stockPurchasePriceLabel input').required=isPurchase;};
  select.addEventListener('change',refresh);refresh();
  document.getElementById('entityForm').onsubmit=event=>saveStockMovement(event,productId);
  document.getElementById('entityModal').classList.add('open');
}

async function saveStockMovement(event,productId){
  event.preventDefault();
  const form=new FormData(event.currentTarget), id=productId||Number(form.get('product_id'));
  const kind=form.get('movement_type');
  const payload={movement_type:kind,quantity:Number(form.get('quantity')),reference_number:form.get('reference_number')||null,notes:form.get('notes')||null};
  if(kind==='PURCHASE'){
    payload.unit_purchase_price=Number(form.get('unit_purchase_price'));
    payload.supplier=form.get('supplier')||null;
    payload.purchase_date=form.get('purchase_date')?`${form.get('purchase_date')}T12:00:00`:null;
  }
  const error=document.getElementById('entityFormError'),submit=event.currentTarget.querySelector('[type="submit"]');
  submit.disabled=true;
  try{
    await apiFetch(`/products/${id}/stock-movements`,{method:'POST',body:payload});
    apiCache.clear();await loadProducts();renderProductsTable();renderProductGrid();renderDashboard();
    if(kind==='PURCHASE'){await loadPurchases();renderPurchases();}
    closeModal('entityModal');showToast(kind==='PURCHASE'?'Purchase recorded and stock received':'Stock adjustment recorded');
    if(document.getElementById('productDetailsModal').classList.contains('open'))openProductDetails(id);
    await loadNotifications();
  }catch(response){error.textContent=response.message||'Could not save stock movement';}
  finally{submit.disabled=false;}
}

let purchases=[];
async function loadPurchases(){
  if(!canViewCosts()) return;
  try{purchases=await apiFetch('/products/purchases');renderPurchases();}
  catch(error){console.warn('Could not load purchases',error);document.getElementById('purchasesTable').innerHTML=`<tbody><tr><td class="cell-muted">${escapeHtml(error.message||'Could not load purchases')}</td></tr></tbody>`;}
}
function renderPurchases(){
  const table=document.getElementById('purchasesTable');if(!table)return;
  const rows=purchases.map(entry=>{
    const p=products.find(item=>item.id===entry.product_id);
    const rate=Number(entry.gst_rate||0),gross=Number(entry.total_amount)||0;
    const taxable=rate?gross/(1+rate/100):gross,gstAmount=gross-taxable;
    return `<tr><td>${new Date(entry.purchase_date).toLocaleDateString('en-IN')}</td><td>${escapeHtml(entry.invoice_number||'?')}</td><td class="cell-strong">${escapeHtml(entry.product_name||p?.nameRaw||`Product ${entry.product_id}`)}${entry.hsn_code?`<small class="table-subline">HSN ${escapeHtml(entry.hsn_code)}</small>`:''}</td><td>${escapeHtml(entry.supplier||'?')}</td><td>${entry.quantity} ${escapeHtml(p?.unitRaw||'')}</td><td class="cell-num">${money(entry.unit_price_including_gst)}</td><td class="cell-num">${entry.master_buying_price_snapshot==null?'-':money(entry.master_buying_price_snapshot)}</td><td class="cell-num">${entry.master_selling_price_snapshot==null?'-':money(entry.master_selling_price_snapshot)}</td><td>${rate}%</td><td class="cell-num">${money(taxable)}</td><td class="cell-num">${money(gstAmount)}</td><td class="cell-num">${money(gross)}</td></tr>`;
  }).join('');
  table.innerHTML=`<thead><tr><th>Date</th><th>Supplier invoice</th><th>Product / HSN</th><th>Supplier</th><th>Quantity</th><th>Actual buying rate</th><th>Buying price (master snapshot)</th><th>Selling price reference</th><th>GST</th><th>Taxable purchase</th><th>Input GST</th><th>Total purchase</th></tr></thead><tbody>${rows||'<tr><td colspan="12" class="cell-muted">No purchases recorded yet. Record a supplier purchase to receive stock.</td></tr>'}</tbody>`;
}
async function downloadPurchases(format){
  if(!canViewCosts()){showToast('Purchase export access is restricted','critical');return;}
  try{
    const response=await fetch(`/accounting/purchases/export?format=${encodeURIComponent(format)}`,{headers:authToken?{Authorization:`Bearer ${authToken}`}:{}});
    if(!response.ok)throw new Error('Could not export purchase report');
    const blob=await response.blob(),url=URL.createObjectURL(blob),a=document.createElement('a');
    a.href=url;a.download=`Adinath_Purchase_Report.${format}`;document.body.appendChild(a);a.click();a.remove();URL.revokeObjectURL(url);
  }catch(error){showToast(error.message||'Purchase export failed','critical');}
}

async function exportProductsCsv(){
  try{
    const response=await fetch('/products/export.csv',{headers:authToken?{Authorization:`Bearer ${authToken}`}:{}});
    if(!response.ok)throw new Error('Product export could not be generated');
    const blob=await response.blob(),href=URL.createObjectURL(blob),link=document.createElement('a');
    link.href=href;link.download='adinath-products.csv';link.click();setTimeout(()=>URL.revokeObjectURL(href),1000);showToast('Product CSV exported');
  }catch(error){showToast(error.message||'Product export failed','critical');}
}
function parseCsv(text){
  const rows=[];let row=[],field='',quoted=false;
  for(let i=0;i<text.length;i++){const ch=text[i];if(quoted){if(ch==='"'&&text[i+1]==='"'){field+='"';i++;}else if(ch==='"')quoted=false;else field+=ch;}else if(ch==='"')quoted=true;else if(ch===','){row.push(field);field='';}else if(ch==='\n'){row.push(field.replace(/\r$/,''));rows.push(row);row=[];field='';}else field+=ch;}
  if(field.length||row.length){row.push(field.replace(/\r$/,''));rows.push(row);}
  const headers=(rows.shift()||[]).map(value=>value.trim());
  return rows.filter(values=>values.some(value=>value.trim())).map(values=>Object.fromEntries(headers.map((header,index)=>[header,(values[index]||'').trim()])));
}
async function importProductsCsv(file){
  if(!file)return;
  try{
    const rows=parseCsv(await file.text()).map(row=>{
      const product={};
      ['name','brand','sku','category','unit','pack_size','hsn_code','supplier','source_reference','gst_selection_type','selling_price_tax_mode'].forEach(key=>{if(row[key]!==undefined)product[key]=row[key]||null;});
      ['catalog_serial','rate','selling_price','purchase_price_including_gst','purchase_price_excluding_gst','purchase_gst_component','gst_rate','discount_percentage','initial_quantity','stock','min_stock'].forEach(key=>{product[key]=row[key]===''||row[key]===undefined?null:Number(row[key]);});
      product.min_stock??=10;product.stock??=0;product.is_active=!['false','0','no'].includes(String(row.is_active||'true').toLowerCase());
      return product;
    });
    if(!rows.length)throw new Error('CSV contains no product rows');
    const preview=await apiFetch('/products/import?dry_run=true',{method:'POST',body:{products:rows}});
    const summary=`${preview.processed} rows ? ${preview.rows.filter(row=>row.status==='matched').length} existing ? ${preview.rows.filter(row=>row.status==='new').length} new ? ${preview.rows.filter(row=>row.status.includes('duplicate')||row.status==='ambiguous_duplicate').length} needs review`;
    if(preview.rows.some(row=>row.status==='duplicate_in_file'||row.status==='ambiguous_duplicate'))throw new Error(`${summary}. Resolve duplicate or ambiguous product rows before importing.`);
    if(!window.confirm(`Import product CSV?\n${summary}\nExisting stock quantities will be retained.`))return;
    const result=await apiFetch('/products/import?dry_run=false',{method:'POST',body:{products:rows}});
    apiCache.clear();await loadProducts();renderProductsTable();renderProductGrid();renderDashboard();
    showToast(`Import complete: ${result.processed} rows processed`);
  }catch(error){showToast(error.message||'Product import failed','critical');}
}

/* ================= FARMERS ================= */
function initials(name){ return name.split(' ').map(w=>w[0]).slice(0,2).join(''); }
function renderFarmers(){
  document.getElementById('farmerGrid').innerHTML = farmers.length ? farmers.map(f=>`
    <article class="farmer-card">
      <div class="farmer-card-top">
        <div class="avatar">${initials(f.name)}</div>
        <div><div class="farmer-card-name">${f.name}</div><div class="farmer-card-loc">${f.location} · ${f.mobile}</div></div>
      </div>
      <div class="farmer-stats">
        <div><div class="farmer-stat-label">Total Purchases</div><div class="farmer-stat-value">${money(f.totalPurchases)}</div></div>
        <div><div class="farmer-stat-label">Outstanding</div><div class="farmer-stat-value ${f.outstanding>0?'due':''}">${money(f.outstanding)}</div></div>
        <div><div class="farmer-stat-label">Last Purchase</div><div class="farmer-stat-value" style="font-size:12px;">${f.lastPurchase}</div></div>
      </div>
      <div class="farmer-card-actions"><button class="btn btn-ghost" type="button" onclick="openFarmerForm(${f.id})">Edit</button><button class="btn btn-danger-icon admin-only" type="button" onclick="deleteFarmer(${f.id}, '${f.nameRaw.replace(/'/g, "\\'")}')" aria-label="Delete ${f.name}">${deleteIcon()}</button></div>
    </article>`).join('') : '<div class="empty-state card"><h3>No customers yet</h3><p>Add your first customer to start managing customer accounts.</p><button class="btn btn-primary" type="button" onclick="openFarmerForm()">Add Customer</button></div>';

  document.getElementById('salesTable').innerHTML = `
    <thead><tr><th>Invoice</th><th>Customer</th><th>Products</th><th>Amount</th><th>Payment</th><th>Status</th><th>PDF</th><th class="admin-only">Action</th></tr></thead>
    <tbody>${transactions.map(t=>`<tr><td class="cell-strong">${t.inv}</td><td>${t.farmer}</td><td class="cell-muted">${t.items}</td><td class="cell-num">${money(t.amount)}</td><td>${t.method}</td><td>${statusBadge(t.status)}</td><td><button class="btn btn-ghost" type="button" onclick="openInvoicePdf(${t.id})">View / Print</button></td><td class="admin-only"><button class="btn btn-danger-icon" type="button" onclick="deleteInvoice(${t.id}, '${t.inv}')" aria-label="Delete ${t.inv}">${deleteIcon()}</button></td></tr>`).join('') || '<tr><td colspan="8" class="cell-muted">No invoices yet. Create an invoice to get started.</td></tr>'}</tbody>`;

  document.getElementById('outstandingTable').innerHTML = `
    <thead><tr><th>Customer</th><th>Invoice</th><th>Date</th><th>Total</th><th>Paid</th><th>Balance</th><th>Status</th><th>Action</th></tr></thead>
    <tbody>${outstandingRows.map(r=>`<tr><td class="cell-strong">${r.farmer}</td><td class="cell-muted">${r.inv}</td><td class="cell-muted">${r.date}</td><td class="cell-num">${money(r.total)}</td><td class="cell-num">${money(r.paid)}</td><td class="cell-num" style="color:var(--danger)">${money(r.balance)}</td><td>${statusBadge(r.status)}</td><td><button class="btn btn-ghost" type="button" onclick="recordPayment(${r.id})">Receive</button><button class="btn btn-danger-icon admin-only" type="button" onclick="deleteInvoice(${r.id}, '${r.inv}')" aria-label="Delete ${r.inv}">${deleteIcon()}</button></td></tr>`).join('') || '<tr><td colspan="8" class="cell-muted">No outstanding payments.</td></tr>'}</tbody>`;
  applyPermissions();

  document.getElementById('topProductsList').innerHTML = topProducts.map(p=>`
    <div class="tp-row"><span class="tp-name">${p.name}</span><div class="tp-bar-track"><div class="tp-bar-fill" style="width:${p.pct}%"></div></div><span class="tp-val">${p.val}</span></div>`).join('') || '<div class="empty-state"><h3>No product sales yet</h3><p>Sales data will appear after invoices are created.</p></div>';
}

/* ================= BILLING ================= */
let bill = []; // {id, qty}
let selectedFarmer = null;
let selectedPayMethod = 'Cash';
let billSubmissionInFlight = false;
let billRequestKey = null;
let billRequestPayload = null;
let productSearchTimer = null;

function billTotals(discountRequested=0){
  const roundMoney = value => Math.round((value + Number.EPSILON) * 100) / 100;
  const lines = bill.map(item=>{
    const product = products.find(row=>row.id===item.id);
    if(!product) return {item,product:null,taxable:0,tax:0,total:0};
    const gstRate = Number(product.gstRate ?? 5);
    const mode = product.sellingPriceTaxMode || 'exclusive';
    const rate = item.rate == null || item.rate === '' ? Number(product.price || 0) : Number(item.rate);
    const listed = roundMoney(rate * item.qty);
    const taxable = roundMoney(mode === 'inclusive' && gstRate > 0 ? listed / (1 + gstRate / 100) : listed);
    const grossDiscount = Math.min(listed,roundMoney((Number(item.discountAmount)||0)+(listed*(Number(item.discountPercentage)||0)/100)));
    const lineDiscount = roundMoney(mode === 'inclusive' && gstRate > 0 ? grossDiscount/(1+gstRate/100) : grossDiscount);
    return {item,product,gstRate,taxable,lineDiscount,tax:0,total:0};
  });
  const subtotal = roundMoney(lines.reduce((sum,line)=>sum+line.taxable,0));
  const productDiscount = roundMoney(lines.reduce((sum,line)=>sum+line.lineDiscount,0));
  const invoiceDiscount = Math.min(Math.max(0,Number(discountRequested)||0),Math.max(0,subtotal-productDiscount));
  const discount = roundMoney(productDiscount+invoiceDiscount);
  let remaining = invoiceDiscount, tax = 0, rawTotal = 0;
  const remainingBase = Math.max(0,subtotal-productDiscount);
  lines.forEach((line,index)=>{
    const afterProductDiscount = Math.max(0,line.taxable-line.lineDiscount);
    const allocated = index === lines.length-1 ? remaining : Math.min(afterProductDiscount,roundMoney(remainingBase ? invoiceDiscount*afterProductDiscount/remainingBase : 0));
    remaining = roundMoney(remaining-allocated);
    line.taxable = roundMoney(Math.max(0,afterProductDiscount-allocated));
    line.discount = roundMoney(line.lineDiscount+allocated);
    line.tax = roundMoney(line.taxable*(line.gstRate||0)/100);
    line.total = roundMoney(line.taxable+line.tax);
    tax = roundMoney(tax+line.tax); rawTotal = roundMoney(rawTotal+line.total);
  });
  return {lines,subtotal,discount,invoiceDiscount,tax,total:Math.round(rawTotal),rounding:Math.round(rawTotal)-rawTotal};
}

function renderProductGrid(){
  const q = (document.getElementById('productSearch').value || '').toLowerCase();
  const list = products.filter(p => p.nameRaw.toLowerCase().includes(q) || p.skuRaw.toLowerCase().includes(q));
  document.getElementById('productGrid').innerHTML = list.map(p=>{
    const inBill = bill.find(b=>b.id===p.id);
    const out = p.status==='Out of Stock';
    const productType = p.categoryRaw === 'Pesticides' ? 'pesticides' : 'fertilizer';
    const typeIcon = productType === 'fertilizer'
      ? '<i data-lucide="sprout" class="lucide-icon" aria-hidden="true"></i>'
      : '<i data-lucide="flask-conical" class="lucide-icon" aria-hidden="true"></i>';
    return `
    <div class="product-card ${productType} ${out?'out':''} ${inBill ? 'in-bill' : ''}">
      <div class="product-card-top">
        <span class="product-cat-tag"><i class="product-type-dot"></i>${p.category}</span>
        <span class="product-type-mark" aria-hidden="true">${typeIcon}</span>
      </div>
      <div class="product-name">${p.name}</div>
      <div class="product-sku">${p.sku} · ${p.unit}</div>
      <div class="product-meta-row">
        <div>
          <div class="product-price">${p.price == null ? '<span class="price-review">Price review needed</span>' : money(p.price)}</div>
          <div class="product-tax-rate">${p.gstRate == null ? 'GST not set' : `${p.gstRate}% GST${p.gstSelectionType === 'custom' ? ' · Custom' : ''}`}</div>
          <div class="product-stock" style="color:${out?'var(--danger)':(p.status==='Low Stock'?'var(--earth)':'var(--charcoal-soft)')}">${out?'Out of stock':p.stock+' in stock'}</div>
        </div>
        <div class="product-qty-stepper ${inBill ? 'is-active' : ''}" aria-label="Quantity controls for ${p.name}">
          <button type="button" onclick="changeQty(${p.id},-1)" aria-label="Remove one ${p.name}" title="Remove one" ${inBill ? '' : 'disabled'}><i data-lucide="minus" class="lucide-icon" aria-hidden="true"></i></button>
          <span aria-live="polite">${inBill ? inBill.qty : 0}</span>
          <button type="button" onclick="addToBill(${p.id})" aria-label="Add one ${p.name}" title="Add one"><i data-lucide="plus" class="lucide-icon" aria-hidden="true"></i></button>
        </div>
      </div>
    </div>`;
  }).join('') || `<div class="empty-state" style="grid-column:1/-1;"><i data-lucide="search" class="lucide-icon" aria-hidden="true"></i><h3>No products found</h3><p>Try a different search term or category.</p></div>`;
}
document.getElementById('productSearch').addEventListener('input', () => {
  // Filtering is local, but rendering every product card for every keystroke
  // makes a large catalogue feel sluggish.
  window.clearTimeout(productSearchTimer);
  productSearchTimer = window.setTimeout(renderProductGrid, 120);
});

function addToBill(id){
  const product = products.find(p=>p.id===id);
  const currentQty = bill.find(b=>b.id===id)?.qty || 0;
  if(!product || product.status==='Out of Stock' || currentQty >= product.stock){
    showToast('Cannot add more than available stock');
    return;
  }
  const existing = bill.find(b=>b.id===id);
  if(existing){ existing.qty += 1; } else { bill.push({id, qty:1, rate:product.price, discountAmount:0, discountPercentage:Number(product.discountPercentage)||0}); }
  renderProductGrid();
  renderBill();
  showToast('Added to bill');
}
function changeQty(id, delta){
  const item = bill.find(b=>b.id===id);
  if(!item) return;
  const product = products.find(p=>p.id===id);
  if(delta > 0 && product && item.qty >= product.stock){ showToast('Cannot add more than available stock'); return; }
  item.qty += delta;
  if(item.qty<=0){ bill = bill.filter(b=>b.id!==id); }
  renderProductGrid();
  renderBill();
}
function removeFromBill(id){ bill = bill.filter(b=>b.id!==id); renderProductGrid(); renderBill(); }
function setBillField(id, field, value){
  const item=bill.find(line=>line.id===id);if(!item)return;
  item[field]=value===''?null:Number(value);
  billRequestKey=null;billRequestPayload=null;renderBill();
}
function resetBillingDraft(){
  bill = [];
  selectedFarmer = null;
  paymentIsManual = false;
  document.getElementById('discountInput').value = 0;
  document.getElementById('amountPaid').value = 0;
  selectedPayMethod = 'Cash';
  billRequestKey = null;
  billRequestPayload = null;
  document.querySelectorAll('.pay-method').forEach(button => button.classList.toggle('active', button.dataset.pm === 'Cash'));
  document.getElementById('farmerSelectBtn').classList.remove('picked');
  document.getElementById('farmerSelectText').textContent = 'Select Customer';
  ['invoiceCustomerName','invoiceCustomerPhone','invoiceCustomerGstin','invoiceCustomerState','invoiceCustomerStateCode','invoiceCustomerAddress','invoiceDueDate','invoicePaymentTerms','invoiceRemarks'].forEach(id=>{const field=document.getElementById(id);if(field)field.value='';});
  document.getElementById('invoiceType').value='TAX INVOICE';
  renderProductGrid();
  renderBill();
}
function clearCurrentBill(){
  if(!bill.length || !window.confirm('Clear all items from the current bill?')) return;
  resetBillingDraft();
  showToast('Current bill cleared');
}

async function refreshAfterDeletion(){
  reportsLoaded = false;
  selectedReport = null;
  await loadAll();
  renderDashboard(); renderProductsTable(); renderFarmers(); renderProductGrid(); renderBill();
}
async function deleteProduct(id, name){
  if(!window.confirm(`Remove product “${name}”? It will no longer appear in billing, but past invoices are kept.`)) return;
  try { await apiFetch(`/products/${id}`, {method:'DELETE'}); await refreshAfterDeletion(); showToast('Product removed'); }
  catch(error){ showToast(error.message || 'Could not remove product'); }
}
async function deleteFarmer(id, name){
  if(!window.confirm(`Delete customer “${name}”? This cannot be undone.`)) return;
  try { await apiFetch(`/farmers/${id}`, {method:'DELETE'}); await refreshAfterDeletion(); showToast('Customer deleted'); }
  catch(error){ showToast(error.message || 'Could not delete customer'); }
}
async function deleteInvoice(id, invoiceNo){
  if(!window.confirm(`Delete invoice ${invoiceNo}? This permanently removes the invoice, its payments, and restores sold stock.`)) return;
  try { await apiFetch(`/invoices/${id}`, {method:'DELETE'}); await refreshAfterDeletion(); showToast('Invoice deleted and stock restored'); }
  catch(error){ showToast(error.message || 'Could not delete invoice'); }
}

function renderBill(){
  // Products can be changed in another screen while a draft is open. Remove
  // those stale lines instead of allowing a render error to freeze checkout.
  const validBill = bill.filter(item => products.some(product => product.id === item.id));
  if(validBill.length !== bill.length) {
    bill = validBill;
    showToast('An unavailable product was removed from the draft.', 'warning');
  }
  const itemsEl = document.getElementById('billItems');
  const billDate = document.getElementById('billDate');
  if (billDate) billDate.textContent = new Date().toLocaleDateString('en-IN', {day:'2-digit', month:'short', year:'numeric'});
  const itemCount = bill.reduce((total, item) => total + item.qty, 0);
  const liveTotals = billTotals(Number(document.getElementById('discountInput').value) || 0);
  document.getElementById('billItemCount').textContent = `${itemCount} ${itemCount === 1 ? 'item' : 'items'}`;
  if(bill.length===0){
    itemsEl.innerHTML = `<div class="bill-empty"><i data-lucide="receipt-text" class="lucide-icon" aria-hidden="true"></i><div>No items added yet.<br>Tap a product to add it to this bill.</div></div>`;
  } else {
    itemsEl.innerHTML = bill.map(b=>{
      const p = products.find(x=>x.id===b.id);
      return `<div class="bill-item">
        <div style="flex:1; min-width:0;">
          <div class="bill-item-name">${p.name}</div>
          <div class="bill-item-rate">${money(b.rate ?? p.price)} / ${p.unit} · HSN ${escapeHtml(p.hsnCode||'—')} · GST ${p.gstRate == null ? 'Not set' : `${p.gstRate}%${p.gstSelectionType === 'custom' ? ' Custom' : ''}`}</div>
          <div class="bill-item-rate">Rate <input aria-label="Transaction rate for ${p.name}" type="number" min="0" step="0.01" value="${b.rate ?? ''}" placeholder="${p.price ?? 'Enter rate'}" onchange="setBillField(${p.id},'rate',this.value)"> Discount ₹ <input aria-label="Line discount amount for ${p.name}" type="number" min="0" step="0.01" value="${b.discountAmount || 0}" onchange="setBillField(${p.id},'discountAmount',this.value)"> % <input aria-label="Line discount percentage for ${p.name}" type="number" min="0" max="100" step="0.01" value="${b.discountPercentage || 0}" onchange="setBillField(${p.id},'discountPercentage',this.value)"></div>
          <div class="bill-item-rate">Taxable ${money(liveTotals.lines.find(line=>line.item.id===b.id)?.taxable ?? 0)} · GST ${money(liveTotals.lines.find(line=>line.item.id===b.id)?.tax ?? 0)}</div>
        </div>
        <div class="qty-ctrl">
          <button onclick="changeQty(${p.id},-1)">−</button>
          <span class="qty-num">${b.qty}</span>
          <button onclick="changeQty(${p.id},1)">+</button>
        </div>
        <div class="bill-item-total">${money(liveTotals.lines.find(line=>line.item.id===b.id)?.total ?? p.price*b.qty)}</div>
        <button class="bill-item-remove" onclick="removeFromBill(${p.id})"><i data-lucide="x" class="lucide-icon" aria-hidden="true"></i></button>
      </div>`;
    }).join('');
  }

  const subtotal = liveTotals.subtotal;
  const discountInput = document.getElementById('discountInput');
  const discount = liveTotals.discount;
  // Keep the live form and API calculation identical: discount cannot exceed
  // the goods subtotal and GST is applied to the discounted value.
  if(Number(discountInput.value) !== liveTotals.invoiceDiscount) discountInput.value = liveTotals.invoiceDiscount.toFixed(2);
  discountInput.max = subtotal.toFixed(2);
  const tax = liveTotals.tax;
  const grandTotal = liveTotals.total;
  if(!paymentIsManual && selectedPayMethod !== 'Credit') document.getElementById('amountPaid').value = grandTotal.toFixed(2);
  const amountPaidInput = document.getElementById('amountPaid');
  const requestedPaid = Math.max(0, Number(amountPaidInput.value) || 0);
  const amountPaid = selectedPayMethod === 'Credit' ? 0 : Math.min(requestedPaid, grandTotal);
  if(Number(amountPaidInput.value) !== amountPaid) amountPaidInput.value = amountPaid.toFixed(2);
  amountPaidInput.max = grandTotal.toFixed(2);
  const balance = Math.max(grandTotal - amountPaid, 0);

  document.getElementById('sumSubtotal').textContent = money(subtotal);
  document.getElementById('sumProductDiscount').textContent = money(Math.max(0,discount-liveTotals.invoiceDiscount));
  document.getElementById('sumTax').textContent = money(tax);
  const gstRates = [...new Set(liveTotals.lines.map(line => line.gstRate).filter(rate => rate != null))].sort((a,b)=>a-b);
  document.getElementById('sumTaxLabel').textContent = gstRates.length ? `GST (${gstRates.map(rate=>`${rate}%`).join(', ')})` : 'GST';
  document.getElementById('grandTotal').textContent = money(grandTotal);
  document.getElementById('balanceDue').value = money(balance);
}

document.querySelectorAll('.pay-method').forEach(btn=>{
  btn.addEventListener('click', ()=>{
    document.querySelectorAll('.pay-method').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    selectedPayMethod = btn.dataset.pm;
    paymentIsManual = false;
    if(selectedPayMethod==='Credit') document.getElementById('amountPaid').value = 0;
    renderBill();
  });
});

/* Farmer picker modal */
function openFarmerPicker(){
  const search = document.getElementById('farmerPickerSearch');
  if(search) search.value = '';
  renderFarmerPicker();
  document.getElementById('farmerModal').classList.add('open');
  search?.focus();
}
function renderFarmerPicker(query=''){
  const normalized = query.trim().toLowerCase();
  const matches = normalized ? farmers.filter(f => f.nameRaw.toLowerCase().includes(normalized) || f.mobileRaw.toLowerCase().includes(normalized)) : farmers;
  document.getElementById('farmerPickList').innerHTML = matches.length ? matches.map(f=>`
    <div class="farmer-pick-item" onclick="pickFarmer(${f.id})">
      <div class="avatar">${initials(f.name)}</div>
      <div><div class="fp-name">${f.name}</div><div class="fp-sub">${f.mobile} · ${f.location}</div></div>
    </div>`).join('') : '<div class="empty-state"><h3>No customers yet</h3><p>Add a customer before assigning one to an invoice.</p><button class="btn btn-primary" type="button" onclick="closeModal(\'farmerModal\'); openFarmerForm()">Add Customer</button></div>';
}
function pickFarmer(id){
  selectedFarmer = farmers.find(f=>f.id===id);
  const btn = document.getElementById('farmerSelectBtn');
  btn.classList.add('picked');
  document.getElementById('farmerSelectText').innerHTML = `${selectedFarmer.name} <span class="sub">· ${selectedFarmer.mobile}</span>`;
  document.getElementById('invoiceCustomerName').value=selectedFarmer.nameRaw;
  document.getElementById('invoiceCustomerPhone').value=selectedFarmer.mobileRaw;
  document.getElementById('invoiceCustomerGstin').value=selectedFarmer.gstin;
  document.getElementById('invoiceCustomerState').value=selectedFarmer.stateName;
  document.getElementById('invoiceCustomerStateCode').value=selectedFarmer.stateCode;
  document.getElementById('invoiceCustomerAddress').value=selectedFarmer.deliveryAddress||selectedFarmer.billingAddress||selectedFarmer.locationRaw;
  closeModal('farmerModal');
}
function closeModal(id){ document.getElementById(id).classList.remove('open'); }

async function recordPayment(invoiceId){
  const amount = Number(window.prompt('Amount received'));
  if(!Number.isFinite(amount) || amount <= 0) return;
  const method = window.prompt('Payment method: cash, upi, or bank', 'cash');
  if(!['cash', 'upi', 'bank'].includes((method || '').toLowerCase())){
    showToast('Choose cash, upi, or bank');
    return;
  }
  try {
    await apiFetch(`/invoices/${invoiceId}/payments`, {method:'POST', body:{amount, method:method.toLowerCase()}});
    await Promise.all([loadOutstanding(), loadDashboard(), loadFarmers(), loadReports()]);
    renderDashboard(); renderFarmers();
    showToast('Payment recorded');
  } catch (error) { showToast(error.message || 'Could not record payment'); }
}

let activeInvoiceId = null;
let activeInvoicePdfUrl = null;
async function showInvoicePdf(invoiceId){
  activeInvoiceId=invoiceId;
  document.getElementById('invoiceSheet').innerHTML='<p style="padding:28px;text-align:center">Preparing invoice PDF…</p>';
  const response=await fetch(`/invoices/${invoiceId}/pdf`,{headers:authToken?{Authorization:`Bearer ${authToken}`}:{}});
  if(!response.ok) throw new Error('Invoice PDF could not be generated');
  const blob=await response.blob();
  if(activeInvoicePdfUrl)URL.revokeObjectURL(activeInvoicePdfUrl);
  activeInvoicePdfUrl=URL.createObjectURL(blob);
  document.getElementById('invoiceSheet').innerHTML=`<iframe class="invoice-preview-frame" src="${activeInvoicePdfUrl}" title="Invoice PDF preview"></iframe>`;
}
async function openInvoicePdf(invoiceId){
  document.getElementById('invoiceModal').classList.add('open');
  document.getElementById('invoiceSheet').textContent='Preparing PDF preview…';
  try{await showInvoicePdf(invoiceId);}catch(e){document.getElementById('invoiceSheet').textContent=e.message||'Invoice PDF could not be loaded';}
}
function downloadInvoicePdf(){
  if(!activeInvoicePdfUrl||!activeInvoiceId){showToast('Create or preview an invoice first');return;}
  const a=document.createElement('a');a.href=activeInvoicePdfUrl;a.download=`Invoice-${activeInvoiceId}.pdf`;document.body.appendChild(a);a.click();a.remove();
}
function printInvoice(){
  const frame=document.querySelector('#invoiceSheet iframe');
  if(frame?.contentWindow){frame.contentWindow.focus();frame.contentWindow.print();}
  else window.print();
}

let latestInvoiceWhatsApp = null;
function invoiceMessage(details){
  return `Hello ${details.customer},\n\nThank you for your purchase from Adinath Agri Services.\n\nInvoice: ${details.invoiceNo}\nDate: ${details.dateTime}\nTotal: ${money(details.total)}\nStatus: ${details.status}\n\nGSTIN: 29ACPFA8181K1ZM\nSH-72, Kusanal Road, Ugar Budruk, Karnataka - 591316`;
}
function invoicePhone(){
  if(!latestInvoiceWhatsApp){ showToast('Create an invoice first'); return ''; }
  let phone = String(latestInvoiceWhatsApp.mobile || '').replace(/\D/g, '');
  if(phone.startsWith('0')) phone = phone.slice(1);
  if(phone.length === 10) phone = '91' + phone;
  if(phone.length < 11){ showToast('Add a valid customer mobile number before sending'); return ''; }
  return phone;
}
function sendInvoiceWhatsApp(){
  const phone = invoicePhone();
  if(!phone) return;
  const message = invoiceMessage(latestInvoiceWhatsApp);
  window.open(`https://wa.me/${phone}?text=${encodeURIComponent(message)}`, '_blank', 'noopener');
}
function sendInvoiceSms(){
  const phone = invoicePhone();
  if(!phone) return;
  const separator = /iPhone|iPad|iPod/i.test(navigator.userAgent) ? '&' : '?';
  window.location.href = `sms:${phone}${separator}body=${encodeURIComponent(invoiceMessage(latestInvoiceWhatsApp))}`;
}

function showToast(msg, type='success', action=null){
  const t = document.getElementById('toast');
  document.getElementById('toastText').textContent = msg;
  t.className = `toast ${type}`;
  t.setAttribute('role', type === 'critical' ? 'alert' : 'status');
  t.setAttribute('aria-live', type === 'critical' ? 'assertive' : 'polite');
  t.onclick = action ? () => { t.classList.remove('show'); action(); } : null;
  t.title = action ? 'Open details' : '';
  t.classList.add('show');
  clearTimeout(window._toastTimer);
  window._toastTimer = setTimeout(()=>t.classList.remove('show'), 2200);
}

async function refreshAfterInvoice(){
  // The invoice POST has committed before this runs. These requests only keep
  // the other screens current and must never delay the invoice preview.
  reportsLoaded = false;
  selectedReport = null;
  await Promise.all([loadProducts(), loadInvoices(), loadDashboard(), loadOutstanding(), loadNotifications()]);
  renderDashboard();
  renderProductsTable();
  renderProductGrid();
}

/* Complete bill -> invoice (creates a real invoice via the backend API) */
async function completeBill(){
  if(billSubmissionInFlight) return;
  if(bill.length===0){ showToast('Add at least one product first'); return; }
  const draftLines = bill.map(item => ({item, product: products.find(product => product.id === item.id)}));
  if(draftLines.some(line => !line.product || !Number.isFinite(line.item.qty) || line.item.qty <= 0)){
    renderBill();
    showToast('This draft contains an unavailable or invalid product. Please review it.', 'critical');
    return;
  }
  const draftTotals = billTotals(Number(document.getElementById('discountInput').value) || 0);
  const discount = draftTotals.invoiceDiscount;
  const draftTotal = draftTotals.total;
  const amountPaid = selectedPayMethod === 'Credit' ? 0 : Math.min(Math.max(0, Number(document.getElementById('amountPaid').value) || 0), draftTotal);
  const payload = {
    farmer_id: selectedFarmer ? selectedFarmer.id : null,
    customer_name: document.getElementById('invoiceCustomerName').value.trim() || null,
    customer_phone: document.getElementById('invoiceCustomerPhone').value.trim() || null,
    customer_gstin: document.getElementById('invoiceCustomerGstin').value.trim() || null,
    customer_address: document.getElementById('invoiceCustomerAddress').value.trim() || null,
    customer_state_name: document.getElementById('invoiceCustomerState').value.trim() || null,
    customer_state_code: document.getElementById('invoiceCustomerStateCode').value.trim() || null,
    place_of_supply_state_name: document.getElementById('invoiceCustomerState').value.trim() || null,
    place_of_supply_state_code: document.getElementById('invoiceCustomerStateCode').value.trim() || null,
    invoice_type: document.getElementById('invoiceType').value,
    due_date: document.getElementById('invoiceDueDate').value ? `${document.getElementById('invoiceDueDate').value}T00:00:00` : null,
    payment_terms: document.getElementById('invoicePaymentTerms').value.trim() || null,
    remarks: document.getElementById('invoiceRemarks').value.trim() || null,
    items: bill.map(b=>({product_id: b.id, qty: b.qty, rate:b.rate, discount_amount:b.discountAmount||0, discount_percentage:b.discountPercentage||0})),
    discount: discount,
    payment_method: (selectedPayMethod || 'Cash').toLowerCase(),
    amount_paid: amountPaid
  };

  const submitBtn = document.getElementById('completeBillBtn');
  if(!submitBtn) return;
  billSubmissionInFlight = true;
  // Retain this key if the network response is interrupted: pressing retry
  // then returns the committed invoice instead of creating another one.
  const requestPayload = JSON.stringify(payload);
  if(billRequestPayload !== requestPayload || !billRequestKey){
    billRequestKey = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
    billRequestPayload = requestPayload;
  }
  submitBtn.disabled = true;
  const originalLabel = submitBtn.textContent;
  submitBtn.textContent = 'Creating bill…';
  let invoice;
  try{
    invoice = await apiFetch('/invoices', {method:'POST', body: payload, timeout:20000, headers:{'X-Idempotency-Key': billRequestKey}});

  const farmer = selectedFarmer || {name:payload.customer_name||'Walk-in Customer', mobile:payload.customer_phone||'—', location:payload.customer_address||''};
  const invNo = invoice.invoice_no;
  const invoiceCreatedAt = new Date(`${invoice.date}Z`);
  const invoiceDate = invoiceCreatedAt.toLocaleDateString('en-IN',{day:'2-digit',month:'short',year:'numeric'});
  const invoiceTime = invoiceCreatedAt.toLocaleTimeString('en-IN',{hour:'2-digit',minute:'2-digit'});
  const subtotal = invoice.subtotal, tax = invoice.tax, grandTotal = invoice.grand_total, balance = invoice.balance_due;
  const status = invoice.status==='paid' ? 'PAID' : (invoice.status==='partial' ? 'PARTIALLY PAID' : 'CREDIT');
  const statusColor = invoice.status==='paid' ? 'var(--leaf-bg);color:var(--forest)' : (invoice.status==='partial' ? 'var(--earth-bg);color:var(--earth)' : 'var(--danger-bg);color:var(--danger)');
  latestInvoiceWhatsApp = {customer: farmer.name, mobile: farmer.mobile, invoiceNo: invNo, dateTime: `${invoiceDate}, ${invoiceTime}`, total: grandTotal, status};

  // New invoice rows contain immutable taxable/GST/total snapshots. Fall back
  // to the historical 5% layout only for invoices created before those fields.
  let remainingDiscount = Number(invoice.discount) || 0;
  let remainingTax = Number(invoice.tax) || 0;
  const rowsHtml = invoice.items.map((it, index)=>{
    const isLastLine = index === invoice.items.length - 1;
    let taxableLine, lineTax, lineTotal, lineDiscount=0, rateLabel;
    if(it.taxable_amount != null && it.tax_amount != null){
      taxableLine=Number(it.taxable_amount); lineTax=Number(it.tax_amount);
      lineTotal=Number(it.total_amount ?? it.amount); rateLabel=`${it.gst_rate ?? 0}%${it.gst_selection_type==='custom'?' Custom':''}`;
    } else {
      const grossLine=Number(it.amount)||0;
      lineDiscount=isLastLine?remainingDiscount:Math.min(grossLine,Math.round((Number(invoice.discount)*grossLine/(Number(invoice.subtotal)||1))*100)/100);
      taxableLine=Math.max(0,grossLine-lineDiscount);
      lineTax=isLastLine?remainingTax:Math.round(taxableLine*0.05*100)/100;
      lineTotal=taxableLine+lineTax; rateLabel='5% (legacy)';
      remainingDiscount=Math.max(0,remainingDiscount-lineDiscount); remainingTax=Math.max(0,remainingTax-lineTax);
    }
    const rowDiscount=it.discount_amount==null?lineDiscount:Number(it.discount_amount);
    return `<tr><td class="inv-line-no">${index + 1}</td><td><strong>${escapeHtml(it.product_name)}</strong><span class="inv-product-unit">Unit: ${escapeHtml(it.unit)}</span></td><td>${it.qty}</td><td>${money(it.rate)}</td><td>${money(rowDiscount)}</td><td>${money(taxableLine)}</td><td><span class="gst-rate">${rateLabel}</span>${money(lineTax)}</td><td class="inv-line-total">${money(lineTotal)}</td></tr>`;
  }).join('');

  document.getElementById('invoiceSheet').innerHTML = `
    <div class="invoice-accent"></div>
    <header class="inv-top">
      <div class="inv-brand">
        <img class="invoice-logo" src="/icons/adinath-agri-logo.svg" alt="Adinath Agri Services logo">
        <div>
          <div class="inv-brand-name">Adinath Agri Services</div>
          <div class="inv-gstin">GSTIN: 29ACPFA8181K1ZM</div>
          <div class="inv-brand-tag">SH-72, Kusanal Road, Ugar Budruk<br>Karnataka – 591316</div>
        </div>
      </div>
      <div class="inv-document">
        <div class="inv-type-label">TAX INVOICE</div>
        <div class="inv-number">${invNo}</div>
        <div class="inv-document-meta"><span><b>Issue date</b>${invoiceDate}</span><span><b>Issue time</b>${invoiceTime}</span></div>
      </div>
    </header>
    <section class="inv-party-row">
      <div class="inv-party"><span class="inv-section-label">BILL TO</span><strong>${farmer.name}</strong><span>${farmer.mobile}</span>${farmer.location ? `<span>${farmer.location}</span>` : ''}</div>
      <div class="inv-payment-summary"><span><b>Payment method</b>${escapeHtml(String(invoice.payment_method).toUpperCase())}</span><span><b>Amount received</b>${money(invoice.amount_paid)}</span><span><b>Balance due</b>${money(balance)}</span></div>
    </section>
    <div class="inv-legacy-party">
    <div class="inv-farmer">${farmer.name}<br><span class="sub">${farmer.mobile}${farmer.location? ' · '+farmer.location:''}</span></div>
    <div class="inv-info-grid">
      <div><span>PAYMENT MODE</span><strong>${escapeHtml(String(invoice.payment_method).toUpperCase())}</strong></div>
      <div><span>AMOUNT PAID</span><strong>${money(invoice.amount_paid)}</strong></div>
      <div><span>BALANCE DUE</span><strong>${money(balance)}</strong></div>
    </div>
    </div>
    <table class="inv-table">
      <thead>
        <tr class="inv-print-repeat-head"><th colspan="8"><div class="inv-print-banner"><div><img src="/icons/adinath-agri-logo.svg" alt=""><strong>Adinath Agri Services</strong></div><span>TAX INVOICE · ${invNo}</span></div></th></tr>
        <tr class="inv-column-headers"><th>#</th><th>ITEM DESCRIPTION</th><th>QTY</th><th>RATE</th><th>DISCOUNT</th><th>TAXABLE</th><th>GST</th><th>AMOUNT</th></tr>
      </thead>
      <tbody>${rowsHtml}</tbody>
    </table>
    <section class="inv-bottom-row">
      <div class="inv-notes"><span class="inv-section-label">NOTES</span><p>Goods once sold will not be taken back. Please retain this invoice for your records.</p><p class="inv-tax-note">Product GST rates are shown on each invoice line.</p></div>
      <div><div class="inv-totals">
        <div class="row"><span>Subtotal</span><span>${money(subtotal)}</span></div>
        <div class="row"><span>Discount</span><span>-${money(invoice.discount)}</span></div>
        <div class="row"><span>GST</span><span>${money(tax)}</span></div>
        <div class="row total"><span>Grand total</span><span>${money(grandTotal)}</span></div>
      </div><div class="inv-status-row"><span>Payment status</span><span class="inv-status" style="background:${statusColor}">${status}</span></div></div>
    </section>
    <div class="inv-legacy-totals">
    <div class="inv-totals">
      <div class="row"><span>Subtotal</span><span>${money(subtotal)}</span></div>
      <div class="row"><span>Discount</span><span>−${money(invoice.discount)}</span></div>
      <div class="row"><span>GST</span><span>${money(tax)}</span></div>
      <div class="row total"><span>TOTAL</span><span>${money(grandTotal)}</span></div>
    </div>
    <div class="inv-status-row"><span>Payment status</span><span class="inv-status" style="background:${statusColor}">${status}</span></div>
    </div>
    <div class="inv-footer">
      <div><div class="inv-brand-line"></div><p>Thank you for choosing Adinath Agri Services.</p></div>
      <div class="inv-signature"><span>For Adinath Agri Services</span><b>Authorised Signatory</b></div>
    </div>`;

  document.getElementById('invoiceModal').classList.add('open');
  await showInvoicePdf(invoice.id);
  resetBillingDraft();
  showToast('Bill created successfully — view invoice', 'success', () => goTo('sales'));
  refreshAfterInvoice().catch(error => console.warn('Post-invoice refresh failed', error));
  }catch(error){
    // A committed invoice is never retried automatically. If its optional
    // preview fails, clear the draft and refresh data so the next sale is safe.
    if(invoice){
      console.error('Invoice created but the preview could not be completed', error);
      resetBillingDraft();
      showToast('Bill was created. The invoice preview could not be displayed.', 'warning');
      refreshAfterInvoice().catch(refreshError => console.warn('Post-invoice refresh failed', refreshError));
    } else {
      console.error('Bill creation failed', {error, payload});
      showToast(`Bill creation failed: ${error.message || 'Unknown error'}`, 'critical');
    }
  }finally{
    billSubmissionInFlight = false;
    submitBtn.disabled = false;
    submitBtn.textContent = originalLabel;
  }
}

document.querySelectorAll('.modal-overlay').forEach(m=>{
  m.addEventListener('click', e=>{ if(e.target===m) m.classList.remove('open'); });
});

function filterProductsList(){
  const query = document.getElementById('productsListSearch').value.trim().toLowerCase();
  const category = document.getElementById('productCategoryFilter').value;
  const gst = document.getElementById('productGstFilter').value;
  const stock = document.getElementById('productStockFilter').value;
  const visible = products.filter(p =>
    (!query || p.nameRaw.toLowerCase().includes(query) || p.brandRaw.toLowerCase().includes(query) || p.skuRaw.toLowerCase().includes(query)) &&
    (!category || p.categoryRaw === category) &&
    (!gst || (gst === 'unset' ? p.gstRate == null : Number(p.gstRate) === Number(gst))) &&
    (!stock || (stock === 'low' && p.status === 'Low Stock') || (stock === 'out' && p.status === 'Out of Stock') || (stock === 'in' && p.status === 'In Stock'))
  );
  renderProductsTable(visible);
}

function toggleProductFilter(){
  const select = document.getElementById('productCategoryFilter');
  const gstSelect = document.getElementById('productGstFilter');
  const stockSelect = document.getElementById('productStockFilter');
  const button = document.getElementById('productFilterBtn');
  select.hidden = !select.hidden;
  gstSelect.hidden = select.hidden;
  stockSelect.hidden = select.hidden;
  button.setAttribute('aria-expanded', String(!select.hidden));
  if(!select.hidden) select.focus();
}

function filterFarmersList(){
  const query = document.getElementById('farmersListSearch').value.trim().toLowerCase();
  const original = farmers;
  if(query) farmers = original.filter(f => f.nameRaw.toLowerCase().includes(query) || f.mobileRaw.toLowerCase().includes(query) || f.locationRaw.toLowerCase().includes(query));
  renderFarmers();
  farmers = original;
}

document.getElementById('productsListSearch').addEventListener('input', filterProductsList);
document.getElementById('farmersListSearch').addEventListener('input', filterFarmersList);
document.getElementById('farmerPickerSearch').addEventListener('input', event => renderFarmerPicker(event.target.value));
const globalSearch = document.getElementById('globalSearch');
const globalSearchResults = document.getElementById('globalSearchResults');
document.addEventListener('keydown', event=>{
  if((event.ctrlKey || event.metaKey) && event.key.toLowerCase()==='k'){
    event.preventDefault();
    globalSearch?.focus();
    globalSearch?.select();
  }
});
function closeGlobalSearch(){
  if(!globalSearchResults || !globalSearch) return;
  globalSearchResults.hidden = true;
  globalSearch.setAttribute('aria-expanded', 'false');
}
function updateGlobalSearch(query){
  const term = query.trim().toLocaleLowerCase();
  if(!term){ closeGlobalSearch(); globalSearchResults.replaceChildren(); return; }
  const productMatches = products.filter(item => [item.nameRaw,item.skuRaw,item.categoryRaw].some(value => String(value || '').toLocaleLowerCase().includes(term))).slice(0,4);
  const farmerMatches = farmers.filter(item => [item.nameRaw,item.mobileRaw,item.locationRaw].some(value => String(value || '').toLocaleLowerCase().includes(term))).slice(0,4);
  const resultButton = (kind, name, detail, icon) => `<button class="global-search-option" type="button" role="option" data-search-kind="${kind}" data-search-name="${escapeHtml(name)}"><i data-lucide="${icon}" class="lucide-icon" aria-hidden="true"></i><span><strong>${escapeHtml(name)}</strong><small>${escapeHtml(detail)}</small></span><span class="global-search-kind">${kind === 'products' ? 'Product' : 'Customer'}</span></button>`;
  const options = [
    ...productMatches.map(item => resultButton('products', item.nameRaw, item.skuRaw || item.categoryRaw || 'Product', 'package')),
    ...farmerMatches.map(item => resultButton('farmers', item.nameRaw, item.mobileRaw || item.locationRaw || 'Customer', 'users'))
  ];
  globalSearchResults.innerHTML = options.length ? options.join('') : '<div class="global-search-empty" role="status">No matching products or customers.</div>';
  globalSearchResults.hidden = false;
  globalSearch.setAttribute('aria-expanded', 'true');
}
globalSearch.addEventListener('input', event => updateGlobalSearch(event.target.value));
globalSearchResults.addEventListener('click', event => {
  const option = event.target.closest('.global-search-option');
  if(!option) return;
  const kind = option.dataset.searchKind;
  const name = option.dataset.searchName;
  closeGlobalSearch();
  if(kind === 'products'){
    document.getElementById('productsListSearch').value = name;
    filterProductsList();
  } else {
    document.getElementById('farmersListSearch').value = name;
    filterFarmersList();
  }
  goTo(kind);
});
document.querySelector('.topbar-search').addEventListener('keydown', event => {
  const options = [...globalSearchResults.querySelectorAll('.global-search-option')];
  if(event.key === 'Escape'){ closeGlobalSearch(); globalSearch.blur(); return; }
  if(!options.length) return;
  if(event.key === 'ArrowDown' || event.key === 'ArrowUp'){
    event.preventDefault();
    const direction = event.key === 'ArrowDown' ? 1 : -1;
    const current = options.indexOf(document.activeElement);
    options[(current + direction + options.length) % options.length].focus();
  } else if(event.key === 'Enter' && document.activeElement === globalSearch){
    event.preventDefault();
    options[0].click();
  }
});
document.addEventListener('pointerdown', event => {
  if(!document.querySelector('.topbar-search').contains(event.target)) closeGlobalSearch();
});

async function loadBusinessSettings(){
  if(!isAdmin())return;
  const settings=await apiFetch('/business-settings');
  const form=document.getElementById('businessSettingsForm');if(!form)return;
  for(const [key,value] of Object.entries(settings))if(form.elements[key])form.elements[key].value=value??'';
}
async function saveBusinessSettings(event){
  event.preventDefault();const form=event.currentTarget;const error=document.getElementById('businessSettingsError');
  const payload=Object.fromEntries(new FormData(form).entries());
  for(const key of ['bank_name','account_holder_name','account_number','branch','ifsc_code','phone','declaration'])if(!payload[key])payload[key]=null;
  try{await apiFetch('/business-settings',{method:'PUT',body:payload});showToast('Invoice business settings saved');}
  catch(e){error.textContent=e.message||'Could not save settings';}
}
let accountingRows = [];
let accountingPage = 1;
let accountingPageSize = 25;
let accountingSort = {key:'transaction_date', direction:'desc'};
let accountingCorrectionLineId = null;

function accountingFilterParams(includeTableFilters=true){
  const params=new URLSearchParams();
  const values={start_date:document.getElementById('caFilterFrom')?.value, end_date:document.getElementById('caFilterTo')?.value,
    invoice_search:document.getElementById('caFilterInvoice')?.value, product_search:document.getElementById('caFilterProduct')?.value,
    customer_search:document.getElementById('caFilterCustomer')?.value};
  Object.entries(values).forEach(([key,value])=>{if(value?.trim())params.set(key,value.trim());});
  if(includeTableFilters){
    params.set('transaction_type',document.getElementById('caFilterType')?.value||'all');
    params.set('missing_cost_snapshot',document.getElementById('caFilterMissing')?.value||'all');
  }
  return params;
}
function accountingTableRows(){
  const type=document.getElementById('caFilterType')?.value||'all';
  const snapshot=document.getElementById('caFilterMissing')?.value||'all';
  return accountingRows.filter(row=>{
    if(type!=='all'&&row.transaction_type.toLowerCase()!==type)return false;
    if(snapshot==='missing'&&(row.transaction_type!=='SALE'||row.cost_snapshot_available))return false;
    if(snapshot==='saved'&&row.transaction_type==='SALE'&&!row.cost_snapshot_available)return false;
    return true;
  });
}
function accountingSortValue(row,key){
  const value=row[key];
  if(value==null)return null;
  if(key==='transaction_date')return new Date(value).getTime();
  if(typeof value==='number')return value;
  return String(value).toLocaleLowerCase();
}
function sortedAccountingRows(rows){
  const {key,direction}=accountingSort;
  return [...rows].sort((a,b)=>{
    const av=accountingSortValue(a,key),bv=accountingSortValue(b,key);
    if(av==null&&bv==null)return 0;
    if(av==null)return 1;
    if(bv==null)return -1;
    if(av<bv)return direction==='asc'?-1:1;
    if(av>bv)return direction==='asc'?1:-1;
    return 0;
  });
}
function sortAccountingBy(key){
  accountingSort=accountingSort.key===key?{key,direction:accountingSort.direction==='asc'?'desc':'asc'}:{key,direction:['transaction_date','quantity','buying_price','selling_price','actual_purchase_rate','actual_sales_rate','purchase_value','sales_value','gross_margin'].includes(key)?'desc':'asc'};
  renderAccountingTable();
}
function accountingCellValue(value){return value==null||value===''?'<span class="ca-not-recorded">Not recorded</span>':money(value);}
function accountingDate(value){return value?new Date(value).toLocaleDateString('en-IN',{day:'2-digit',month:'short',year:'numeric'}):'—';}
function accountingNumber(value){return value==null?'—':new Intl.NumberFormat('en-IN',{maximumFractionDigits:2}).format(Number(value));}
function accountingEmptyChart(label,detail){return `<div class="ca-chart-empty"><span class="ca-empty-chart-icon"><i data-lucide="chart-no-axes-combined" class="lucide-icon" aria-hidden="true"></i></span><strong>${escapeHtml(label)}</strong><span>${escapeHtml(detail)}</span></div>`;}
function compactAccountingMoney(value){return new Intl.NumberFormat('en-IN',{style:'currency',currency:'INR',notation:'compact',maximumFractionDigits:1}).format(Number(value)||0);}

async function loadAccountingRows(){
  if(!canViewCosts())throw new Error('Accounting report access is restricted');
  const status=document.getElementById('accountingSyncStatus');
  if(status)status.innerHTML='<i data-lucide="refresh-cw" class="lucide-icon" aria-hidden="true"></i> Syncing report data';
  const params=accountingFilterParams(false);
  params.set('transaction_type','all');params.set('missing_cost_snapshot','all');
  try{
    accountingRows=await apiFetch(`/accounting/rows?${params.toString()}`);
    accountingPage=1;
    renderAccountingSummary();renderAccountingCharts();renderAccountingTable();
    if(status)status.innerHTML='<i data-lucide="circle-check" class="lucide-icon" aria-hidden="true"></i> Live · shared data connected';
    const updated=document.getElementById('accountingLastUpdated');
    if(updated)updated.textContent=`Last report update ${new Date().toLocaleTimeString('en-IN',{hour:'2-digit',minute:'2-digit'})}`;
    if(window.lucide)window.lucide.createIcons();
  }catch(error){
    if(status)status.innerHTML='<i data-lucide="circle-alert" class="lucide-icon" aria-hidden="true"></i> Report data unavailable';
    throw error;
  }
}
function renderAccountingSummary(){
  const sales=accountingRows.filter(r=>r.transaction_type==='SALE');
  const purchases=accountingRows.filter(r=>r.transaction_type==='PURCHASE');
  const snapshots=sales.filter(r=>r.cost_snapshot_available);
  const missing=sales.length-snapshots.length;
  const salesTotal=sales.reduce((sum,r)=>sum+(Number(r.sales_value)||0),0);
  const recordedPurchases=purchases.reduce((sum,r)=>sum+(Number(r.purchase_value)||0),0);
  const savedCosts=snapshots.reduce((sum,r)=>sum+(Number(r.purchase_value)||0),0);
  const knownSales=snapshots.reduce((sum,r)=>sum+(Number(r.sales_value)||0),0);
  const grossMargin=snapshots.reduce((sum,r)=>sum+(Number(r.gross_margin)||0),0);
  const marginPercent=knownSales?grossMargin/knownSales*100:null;
  const set=(id,value)=>{const element=document.getElementById(id);if(element)element.textContent=value;};
  set('caPurchaseTotal',money(recordedPurchases));set('caPurchaseNote',purchases.length?`${purchases.length} supplier transaction${purchases.length===1?'':'s'} · actual purchase totals`:'No purchase transactions recorded in this period');
  set('caSalesTotal',money(salesTotal));set('caSalesNote',sales.length?`${sales.length} sales invoice lines · taxable revenue`:'No sales records in this period');
  set('caMarginTotal',snapshots.length?money(grossMargin):'—');set('caMarginNote',snapshots.length?`Partial · ${snapshots.length} of ${sales.length} sale lines with saved cost`:'Unavailable until cost snapshots are recorded');
  set('caMissingTotal',new Intl.NumberFormat('en-IN').format(missing));set('caMissingNote',sales.length?`${sales.length?((missing/sales.length)*100).toFixed(1):'0.0'}% of filtered sales lines`:'No sales invoice lines in this scope');
  set('caSnapshotTotal',new Intl.NumberFormat('en-IN').format(sales.length));set('caSnapshotSaved',new Intl.NumberFormat('en-IN').format(snapshots.length));
  set('caSnapshotMissing',new Intl.NumberFormat('en-IN').format(missing));set('caSnapshotPercent',sales.length?`${(missing/sales.length*100).toFixed(1)}%`:'0%');
  set('caAnalysisPurchase',snapshots.length?money(savedCosts):'—');set('caAnalysisSales',snapshots.length?money(knownSales):'—');
  set('caAnalysisMargin',snapshots.length?money(grossMargin):'—');set('caAnalysisPercent',marginPercent==null?'—':`${marginPercent.toFixed(2)}%`);
  set('caMarginCoverage',sales.length?`${snapshots.length} / ${sales.length} sales lines have saved costs · margin is partial`:'No sales lines in this scope');
  set('accountingReportStatus',`${accountingRows.length.toLocaleString('en-IN')} transaction lines in current date and search scope`);
}
function renderAccountingTable(){
  const table=document.getElementById('accountingTable');if(!table)return;
  const rows=sortedAccountingRows(accountingTableRows());
  const columns=[['invoice_number','Invoice Number'],['transaction_date','Date'],['customer_name','Customer / Supplier'],['product_name','Product'],['hsn_code','HSN Code'],['quantity','Quantity'],['buying_price','Buying Price (Master)'],['selling_price','Selling Price (Master)'],['actual_purchase_rate','Actual Purchase Rate'],['actual_sales_rate','Actual Sales Rate'],['purchase_value','Purchase Value'],['sales_value','Sales Value'],['gross_margin','Gross Margin']];
  const header=columns.map(([key,label])=>`<th scope="col" class="${['invoice_number','transaction_date','customer_name','product_name','hsn_code'].includes(key)?'':'ca-number-col'}"><button type="button" class="ca-sort-button" onclick="sortAccountingBy('${key}')" aria-label="Sort by ${label}">${label}<span>${accountingSort.key===key?(accountingSort.direction==='asc'?'↑':'↓'):'↕'}</span></button></th>`).join('');
  const start=(accountingPage-1)*accountingPageSize;
  const pageRows=rows.slice(start,start+accountingPageSize);
  const body=pageRows.map(row=>{
    const isSale=row.transaction_type==='SALE';
    const missing=isSale&&!row.cost_snapshot_available;
    const costMeta=isSale?'Saved historical cost · ex GST':'Supplier bill rate · incl GST';
    const purchaseMeta=isSale?'Saved cost snapshot':'Supplier transaction total';
    const purchaseCell=missing?'<span class="ca-not-recorded">Not recorded</span>':accountingCellValue(row.actual_purchase_rate);
    const saleCell=isSale?accountingCellValue(row.actual_sales_rate):'<span class="ca-not-recorded">Not applicable</span>';
    const margin=missing?'<span class="ca-not-recorded">Incomplete cost</span>':accountingCellValue(row.gross_margin);
    return `<tr class="${missing?'ca-row-missing':''}"><td class="ca-sticky-col"><div class="ca-invoice-cell"><strong>${escapeHtml(row.invoice_number)}</strong><span class="ca-type-badge ${isSale?'is-sale':'is-purchase'}">${isSale?'SALE':'PURCHASE'}</span>${missing?`<button type="button" class="ca-correct-link" onclick="openAccountingCostCorrection(${Number(row.line_id)},'${escapeHtml(row.product_name).replace(/'/g,'&#39;')}')">Add sourced cost</button>`:''}</div></td><td class="ca-date-cell">${accountingDate(row.transaction_date)}</td><td>${escapeHtml(row.customer_name||'—')}</td><td class="ca-product-cell"><strong>${escapeHtml(row.product_name||'—')}</strong>${missing?'<small>Historical cost missing</small>':''}</td><td>${escapeHtml(row.hsn_code||'—')}</td><td class="ca-number-cell">${accountingNumber(row.quantity)}</td><td class="ca-number-cell">${accountingCellValue(row.buying_price)}</td><td class="ca-number-cell">${accountingCellValue(row.selling_price)}</td><td class="ca-number-cell">${purchaseCell}<small class="ca-cell-subline">${costMeta}</small></td><td class="ca-number-cell">${saleCell}${isSale?'<small class="ca-cell-subline">Saved invoice rate</small>':''}</td><td class="ca-number-cell">${accountingCellValue(row.purchase_value)}<small class="ca-cell-subline">${purchaseMeta}</small></td><td class="ca-number-cell">${accountingCellValue(row.sales_value)}</td><td class="ca-number-cell ${row.gross_margin<0?'ca-negative':''}">${margin}</td></tr>`;
  }).join('');
  table.innerHTML=`<thead><tr>${header}</tr></thead><tbody>${body||'<tr><td colspan="13" class="ca-table-empty">No accounting records match these filters.</td></tr>'}</tbody>`;
  const totalPages=Math.max(1,Math.ceil(rows.length/accountingPageSize));
  document.getElementById('caPaginationStatus').textContent=rows.length?`Showing ${start+1}–${Math.min(start+pageRows.length,rows.length)} of ${rows.length.toLocaleString('en-IN')}`:'No records to display';
  document.getElementById('caPrevPage').disabled=accountingPage<=1;
  document.getElementById('caNextPage').disabled=accountingPage>=totalPages;
  const detail=document.getElementById('accountingReportStatusDetail');
  const type=document.getElementById('caFilterType').value;
  const snapshot=document.getElementById('caFilterMissing').value;
  detail.textContent=`${rows.length.toLocaleString('en-IN')} rows shown · ${type==='all'?'all transaction types':type+' records'} · ${snapshot==='all'?'all snapshot statuses':snapshot+' cost snapshots'}. Master prices are current references; transaction rates are historical.`;
  if(window.lucide)window.lucide.createIcons();
}
function renderAccountingCharts(){
  const sales=accountingRows.filter(r=>r.transaction_type==='SALE');
  const purchases=accountingRows.filter(r=>r.transaction_type==='PURCHASE');
  const monthKey=value=>{const date=new Date(value);return `${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,'0')}`;};
  const months=new Map();
  [...sales,...purchases].forEach(row=>{const key=monthKey(row.transaction_date);if(!months.has(key))months.set(key,{sales:0,purchases:0});const bucket=months.get(key);if(row.transaction_type==='SALE')bucket.sales+=Number(row.sales_value)||0;else bucket.purchases+=Number(row.purchase_value)||0;});
  const monthEntries=[...months.entries()].sort(([a],[b])=>a.localeCompare(b)).slice(-8);
  const monthlyTarget=document.getElementById('caMonthlyChart');
  if(monthlyTarget){
    if(!monthEntries.length)monthlyTarget.innerHTML=accountingEmptyChart('No monthly transactions','Monthly sales and purchases will appear when records are available.');
    else{
      const max=Math.max(1,...monthEntries.flatMap(([,v])=>[v.sales,v.purchases]));const w=620,h=205,left=45,right=12,top=12,bottom=32,plotH=h-top-bottom,groupW=(w-left-right)/monthEntries.length,barW=Math.min(24,groupW*.3);
      const grid=[0,.5,1].map(frac=>{const y=top+plotH*(1-frac);return `<g><line x1="${left}" y1="${y}" x2="${w-right}" y2="${y}" class="ca-chart-grid"/><text x="${left-7}" y="${y+3}" text-anchor="end" class="ca-chart-axis">${compactAccountingMoney(max*frac)}</text></g>`;}).join('');
      const bars=monthEntries.map(([key,v],i)=>{const center=left+groupW*(i+.5),salesH=(v.sales/max)*plotH,purchaseH=(v.purchases/max)*plotH;const label=new Date(`${key}-01T00:00:00`).toLocaleDateString('en-IN',{month:'short',year:'2-digit'});return `<g><rect x="${center-barW-2}" y="${top+plotH-salesH}" width="${barW}" height="${salesH}" rx="4" class="ca-bar-sales"><title>${label}: Sales ${money(v.sales)}</title></rect><rect x="${center+2}" y="${top+plotH-purchaseH}" width="${barW}" height="${purchaseH}" rx="4" class="ca-bar-purchase"><title>${label}: Purchase ${money(v.purchases)}</title></rect><text x="${center}" y="${h-9}" text-anchor="middle" class="ca-chart-axis">${label}</text></g>`;}).join('');
      monthlyTarget.innerHTML=`<svg class="ca-chart-svg" viewBox="0 0 ${w} ${h}" role="img" aria-label="Monthly taxable sales versus recorded purchases">${grid}${bars}</svg>${purchases.length?'':'<span class="ca-chart-footnote">No supplier purchase transactions recorded in this period.</span>'}`;
    }
  }
  const known=sales.filter(r=>r.cost_snapshot_available);
  const marginMonths=new Map();known.forEach(row=>{const key=monthKey(row.transaction_date);marginMonths.set(key,(marginMonths.get(key)||0)+(Number(row.gross_margin)||0));});
  const marginEntries=[...marginMonths.entries()].sort(([a],[b])=>a.localeCompare(b)).slice(-8);
  const marginTarget=document.getElementById('caMarginChart');
  if(marginTarget){
    if(!marginEntries.length)marginTarget.innerHTML=accountingEmptyChart('Margin not available','No sales lines in this filtered period have a saved cost snapshot.');
    else{const w=430,h=205,left=38,right=12,top=18,bottom=34,min=Math.min(0,...marginEntries.map(([,v])=>v)),max=Math.max(0,...marginEntries.map(([,v])=>v)),span=max-min||1,plotH=h-top-bottom,plotW=w-left-right;const points=marginEntries.map(([,v],i)=>({x:marginEntries.length===1?left+plotW/2:left+i*plotW/(marginEntries.length-1),y:top+(max-v)*plotH/span,value:v}));const line=points.map((p,i)=>`${i?'L':'M'} ${p.x} ${p.y}`).join(' ');const dots=points.map((p,i)=>`<circle cx="${p.x}" cy="${p.y}" r="4" class="ca-margin-point"><title>${marginEntries[i][0]}: ${money(p.value)}</title></circle><text x="${p.x}" y="${h-10}" text-anchor="middle" class="ca-chart-axis">${new Date(`${marginEntries[i][0]}-01T00:00:00`).toLocaleDateString('en-IN',{month:'short',year:'2-digit'})}</text>`).join('');marginTarget.innerHTML=`<svg class="ca-chart-svg" viewBox="0 0 ${w} ${h}" role="img" aria-label="Gross margin trend based on saved cost snapshots"><line x1="${left}" y1="${top+plotH}" x2="${w-right}" y2="${top+plotH}" class="ca-chart-grid"/><path d="${line}" class="ca-margin-line"/>${dots}</svg><span class="ca-chart-footnote">${known.length} of ${sales.length} sale lines have saved costs. Trend excludes uncosted records.</span>`;}
  }
  const renderProductBars=(targetId,source,key,emptyTitle,emptyDetail)=>{const target=document.getElementById(targetId);if(!target)return;const grouped=new Map();source.forEach(row=>{const name=row.product_name||'Unknown product';grouped.set(name,(grouped.get(name)||0)+(Number(row[key])||0));});const ranked=[...grouped.entries()].sort((a,b)=>b[1]-a[1]).slice(0,6);if(!ranked.length){target.innerHTML=accountingEmptyChart(emptyTitle,emptyDetail);return;}const max=Math.max(1,...ranked.map(([,v])=>v));target.innerHTML=ranked.map(([name,value])=>`<div class="ca-product-bar-row" title="${escapeHtml(name)}: ${money(value)}"><div class="ca-product-bar-label"><span>${escapeHtml(name)}</span><strong>${compactAccountingMoney(value)}</strong></div><div class="ca-product-bar-track"><i style="width:${Math.max(3,value/max*100)}%"></i></div></div>`).join('');};
  renderProductBars('caProductSalesChart',sales,'sales_value','No sales to chart','Product sales will appear once invoices match this report scope.');
  renderProductBars('caProductPurchaseChart',purchases,'purchase_value','No purchase costs recorded','Supplier purchase transactions are shown here when recorded.');
  if(window.lucide)window.lucide.createIcons();
}
async function applyAccountingFilters(event){
  event?.preventDefault();
  if(document.getElementById('caFilterFrom').value&&document.getElementById('caFilterTo').value&&document.getElementById('caFilterTo').value<document.getElementById('caFilterFrom').value){showToast('To date must be on or after the from date','critical');return;}
  try{await loadAccountingRows();}catch(error){showToast(error.message||'Could not load accounting report','critical');}
}
function clearAccountingFilters(){
  document.getElementById('accountingFilters').reset();accountingPage=1;
  loadAccountingRows().catch(error=>showToast(error.message||'Could not load accounting report','critical'));
}
function viewMissingAccountingRecords(){document.getElementById('caFilterMissing').value='missing';renderAccountingTable();document.getElementById('caTransactionsTitle').scrollIntoView({behavior:'smooth',block:'start'});}
function changeAccountingPage(delta){const pages=Math.max(1,Math.ceil(accountingTableRows().length/accountingPageSize));accountingPage=Math.min(pages,Math.max(1,accountingPage+delta));renderAccountingTable();}
function changeAccountingPageSize(value){accountingPageSize=Number(value)||25;accountingPage=1;renderAccountingTable();}
function openAccountingCostCorrection(lineId,productName){
  if(!canViewCosts())return;
  accountingCorrectionLineId=lineId;document.getElementById('caCorrectionProduct').textContent=`${productName} · Enter a source-backed historical purchase cost. Invoice totals and product prices will not change.`;
  document.getElementById('accountingCorrectionForm').reset();document.getElementById('accountingCorrectionDialog').showModal();if(window.lucide)window.lucide.createIcons();
}
function closeAccountingCostCorrection(){document.getElementById('accountingCorrectionDialog').close();accountingCorrectionLineId=null;}
async function saveAccountingCostCorrection(event){
  event.preventDefault();if(!accountingCorrectionLineId||!canViewCosts())return;
  const form=event.currentTarget;const payload=Object.fromEntries(new FormData(form).entries());
  payload.unit_cost_ex_tax=Number(payload.unit_cost_ex_tax);payload.unit_cost_incl_gst=payload.unit_cost_incl_gst===''?null:Number(payload.unit_cost_incl_gst);
  try{await apiFetch(`/accounting/lines/${accountingCorrectionLineId}/cost-correction`,{method:'POST',body:payload});closeAccountingCostCorrection();showToast('Historical cost snapshot saved with audit details');await loadAccountingRows();}
  catch(error){showToast(error.message||'Could not save historical cost correction','critical');}
}
async function downloadAccounting(format){
  if(!canViewCosts()){showToast('Accounting report access is restricted','critical');return;}
  try{
    const params=accountingFilterParams(true);params.set('format',format);
    const response=await fetch(`/accounting/export?${params.toString()}`,{headers:authToken?{Authorization:`Bearer ${authToken}`}:{}});
    if(!response.ok)throw new Error('Could not export accounting report');
    const blob=await response.blob(),url=URL.createObjectURL(blob),a=document.createElement('a');
    a.href=url;a.download=`Adinath_CA_Accounting.${format}`;document.body.appendChild(a);a.click();a.remove();URL.revokeObjectURL(url);
  }catch(e){showToast(e.message||'Accounting export failed','critical');}
}

/* ================= INIT ================= */
setupPwa();
async function initializeApp(){
  await configureFirstLaunch();
  await resumeSession();
}
initializeApp();

// Keep Reports entirely on the reconciled selected-period endpoint.  This
// declaration intentionally follows legacy dashboard helpers retained for
// other views, so it is the active loader used by boot and navigation.
async function loadReports(){
  if (reportsLoaded) return;
  reportsLoaded = await loadSelectedReport();
}
