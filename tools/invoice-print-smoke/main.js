const { app, BrowserWindow } = require('electron');
const fs = require('fs');
const os = require('os');
const path = require('path');

const baseUrl = process.env.AAS_PRINT_BASE_URL || 'http://127.0.0.1:8877';
const outputDirectory = path.join(os.tmpdir(), 'AdinathInvoicePrintSmoke');
const errors = [];

function delay(milliseconds) {
  return new Promise(resolve => setTimeout(resolve, milliseconds));
}

async function waitForInvoice(window) {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    const isOpen = await window.webContents.executeJavaScript(
      "document.getElementById('invoiceModal').classList.contains('open')"
    );
    if (isOpen) return;
    await delay(250);
  }
  throw new Error('Invoice preview did not open');
}

async function printCurrentInvoice(window, filename) {
  await waitForInvoice(window);
  await window.webContents.executeJavaScript(
    "document.querySelector('#invoiceSheet .invoice-logo').decode().catch(() => {})"
  );
  const metadata = await window.webContents.executeJavaScript(`(() => ({
    invoice: document.querySelector('#invoiceSheet .inv-number').innerText,
    customer: document.querySelector('#invoiceSheet .inv-party strong').innerText,
    rows: document.querySelectorAll('#invoiceSheet .inv-table tbody tr').length,
    total: document.querySelector('#invoiceSheet .inv-totals .row.total').innerText,
    logoWidth: document.querySelector('#invoiceSheet .invoice-logo').naturalWidth
  }))()`);
  const pdf = await window.webContents.printToPDF({
    pageSize: 'A4',
    printBackground: true,
    preferCSSPageSize: true,
  });
  const outputPath = path.join(outputDirectory, filename);
  fs.writeFileSync(outputPath, pdf);
  return { ...metadata, bytes: pdf.length, outputPath };
}

async function createInvoice(window, lines, discount) {
  const payload = JSON.stringify(lines);
  await window.webContents.executeJavaScript(`(async () => {
    await Promise.all([loadProducts(), loadFarmers()]);
    goTo('billing');
    const requested = ${payload};
    bill = requested.map(line => {
      const product = products.find(item => item.skuRaw === line.sku);
      if (!product) throw new Error('Missing test product: ' + line.sku);
      if (line.qty > product.stock) throw new Error('Test quantity exceeds stock: ' + line.sku);
      return { id: product.id, qty: line.qty };
    });
    selectedFarmer = farmers.find(item => item.mobileRaw === '5550101000');
    if (!selectedFarmer) throw new Error('Test customer was not loaded');
    selectedPayMethod = 'Cash';
    paymentIsManual = false;
    document.getElementById('discountInput').value = '${discount}';
    renderProductGrid();
    renderBill();
    await completeBill();
  })()`, true);
  await waitForInvoice(window);
}

app.whenReady().then(async () => {
  fs.mkdirSync(outputDirectory, { recursive: true });
  const window = new BrowserWindow({
    width: 1400,
    height: 950,
    show: false,
    webPreferences: { contextIsolation: true, nodeIntegration: false },
  });
  window.webContents.on('console-message', (_event, level, message) => {
    if (level >= 2) errors.push(message);
  });
  window.webContents.on('did-fail-load', (_event, code, description, url) => {
    errors.push(`load ${code}: ${description} (${url})`);
  });

  await window.loadURL(baseUrl);
  await window.webContents.executeJavaScript(`(async () => {
    document.getElementById('loginUsername').value = 'print-smoke-admin';
    document.getElementById('loginPassword').value = 'Print-Safe-Only-2026';
    await handleLogin();
    await Promise.all([loadProducts(), loadFarmers()]);
  })()`, true);

  await createInvoice(window, [{ sku: 'FERT-UREA-001', qty: 2 }], 20);
  const singlePage = await printCurrentInvoice(window, 'one-product.pdf');

  await window.webContents.executeJavaScript("closeModal('invoiceModal')");
  const multiLines = await window.webContents.executeJavaScript(`products
    .filter(product => product.status !== 'Out of Stock')
    .map(product => ({sku: product.skuRaw, qty: product.skuRaw === 'FERT-UREA-001' ? 40 : 1}))`);
  multiLines.push({ sku: 'PRINT-LONG-NAME-TEST', qty: 1 });
  for (let index = 1; index <= 25; index += 1) {
    multiLines.push({ sku: `PRINT-PAGE-${String(index).padStart(3, '0')}`, qty: 1 });
  }
  await createInvoice(window, multiLines, 2500);
  const multiplePages = await printCurrentInvoice(window, 'many-products.pdf');

  const report = { singlePage, multiplePages, consoleErrors: errors };
  fs.writeFileSync(path.join(outputDirectory, 'print-report.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
  window.destroy();
  app.quit();
}).catch(error => {
  console.error(error.stack || error);
  app.exit(1);
});