const { app, BrowserWindow, dialog, shell } = require('electron');
const { spawn } = require('child_process');
const crypto = require('crypto');
const http = require('http');
const net = require('net');
const path = require('path');
const fs = require('fs');

const APP_TITLE = 'Adinath Agri Services';
const DEFAULT_PORT = 8765;
let port = DEFAULT_PORT;
let serverUrl = `http://127.0.0.1:${port}`;
let backend = null;
let mainWindow = null;
let splashWindow = null;

const projectRoot = path.join(__dirname, '..');
const iconPath = path.join(__dirname, 'assets', 'adinath-agri-services.ico');

process.on('uncaughtException', error => startupLog('Unhandled Electron exception.', error));
process.on('unhandledRejection', error => startupLog('Unhandled Electron rejection.', error));

function startupLog(message, error) {
  const details = error ? ` ${error.stack || error.message || error}` : '';
  const line = `[${new Date().toISOString()}] ${message}${details}\n`;
  try {
    const logDirectory = path.join(app.getPath('userData'), 'logs');
    fs.mkdirSync(logDirectory, { recursive: true });
    fs.appendFileSync(path.join(logDirectory, 'startup.log'), line, 'utf8');
  } catch {
    // Logging must never prevent the application from showing its startup error.
  }
}

function availablePort(preferredPort) {
  return new Promise(resolve => {
    const probe = net.createServer();
    probe.once('error', () => resolve(null));
    probe.listen(preferredPort, '127.0.0.1', () => {
      const selectedPort = probe.address().port;
      probe.close(() => resolve(selectedPort));
    });
  });
}

async function choosePort() {
  const preferredPort = Number(process.env.ADINATH_PORT || DEFAULT_PORT);
  const preferredAvailable = await availablePort(preferredPort);
  if (preferredAvailable) return preferredAvailable;
  return availablePort(0);
}

function backendEnvironment() {
  const dataDirectory = path.join(app.getPath('userData'), 'data');
  fs.mkdirSync(dataDirectory, { recursive: true });
  const secretFile = path.join(dataDirectory, 'desktop-secret.key');
  // The API requires a production secret. Generate it once per Windows user so
  // sessions remain valid between launches without putting a secret in source.
  let secret = fs.existsSync(secretFile) ? fs.readFileSync(secretFile, 'utf8').trim() : '';
  if (!secret) {
    secret = crypto.randomBytes(48).toString('hex');
    fs.writeFileSync(secretFile, secret, { encoding: 'utf8', mode: 0o600 });
  }
  const environment = {
    ...process.env,
    ADINATH_PORT: String(port),
    // Installed applications cannot write into Program Files. Business data is
    // deliberately stored per Windows user and survives application upgrades.
    ADINATH_DB_PATH: process.env.ADINATH_DB_PATH || path.join(dataDirectory, 'adinath.db'),
    ADINATH_ENV: 'production',
    ADINATH_SECRET_KEY: secret,
  };
  // Source runs use the checked-out frontend; the packaged Python executable
  // sets its own path to the frontend bundled by PyInstaller.
  if (!app.isPackaged) environment.ADINATH_FRONTEND_DIR = path.join(projectRoot, 'frontend');
  return environment;
}

function startBackend() {
  const env = backendEnvironment();
  const executable = app.isPackaged
    ? path.join(process.resourcesPath, 'backend', 'AdinathAgriBackend.exe')
    : path.join(projectRoot, 'backend', 'venv', 'Scripts', 'python.exe');
  if (!fs.existsSync(executable)) {
    throw new Error(`Backend executable not found at ${executable}`);
  }
  startupLog(`Starting backend on port ${port}: ${executable}`);
  if (app.isPackaged) {
    backend = spawn(executable, [], { windowsHide: true, env });
  } else {
    const script = path.join(projectRoot, 'backend', 'desktop_server.py');
    if (!fs.existsSync(script)) throw new Error(`Backend entry point not found at ${script}`);
    backend = spawn(executable, [script], { windowsHide: true, env });
  }
  backend.on('error', error => {
    startupLog('Backend process failed to start.', error);
    showStartupError(`The local application service could not start.\n\n${error.message}`);
  });
  backend.on('exit', code => {
    startupLog(`Backend process exited with code ${code}.`);
    if (!app.isQuitting && code !== 0) showStartupError('The local application service stopped unexpectedly.');
  });
}

function stopBackend() {
  if (backend && backend.pid && process.platform === 'win32') {
    spawn('taskkill', ['/pid', String(backend.pid), '/t', '/f'], { windowsHide: true });
  } else if (backend && !backend.killed) {
    backend.kill();
  }
  startupLog('Backend process stopped.');
  backend = null;
}

function serverIsReady() {
  return new Promise(resolve => {
    const request = http.get(`${serverUrl}/api/health`, response => {
      response.resume();
      resolve(response.statusCode === 200);
    });
    request.on('error', () => resolve(false));
    request.setTimeout(1000, () => { request.destroy(); resolve(false); });
  });
}

async function waitForServer(timeoutMs = 30000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    if (await serverIsReady()) return true;
    await new Promise(resolve => setTimeout(resolve, 300));
  }
  return false;
}

function createSplash() {
  splashWindow = new BrowserWindow({
    width: 540, height: 340, resizable: false, frame: false, transparent: false,
    alwaysOnTop: true, center: true, show: false, icon: iconPath,
    webPreferences: { contextIsolation: true, nodeIntegration: false },
  });
  splashWindow.loadFile(path.join(__dirname, 'splash.html'));
  splashWindow.once('ready-to-show', () => splashWindow?.show());
}

function createMainWindow() {
  mainWindow = new BrowserWindow({
    title: APP_TITLE,
    width: 1440, height: 900, minWidth: 1024, minHeight: 680,
    show: false, autoHideMenuBar: true, icon: iconPath,
    backgroundColor: '#f4f7f1',
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true },
  });
  mainWindow.setMenuBarVisibility(false);
  mainWindow.loadURL(serverUrl);
  mainWindow.once('ready-to-show', () => {
    startupLog(`Main window ready at ${serverUrl}.`);
    mainWindow.show();
    splashWindow?.destroy();
    splashWindow = null;
  });
  mainWindow.webContents.on('did-fail-load', (_event, errorCode, errorDescription, validatedURL) => {
    startupLog(`Frontend failed to load (${errorCode}) at ${validatedURL}: ${errorDescription}`);
    showStartupError(`The application screen could not load.\n\n${errorDescription}`);
  });
  mainWindow.on('closed', () => startupLog('Main window closed.'));
  mainWindow.webContents.on('render-process-gone', (_event, details) => {
    startupLog(`Frontend renderer exited: ${details.reason}.`);
    if (!app.isQuitting) showStartupError('The application screen stopped unexpectedly.');
  });
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    // WhatsApp and other genuine external links use the default Windows app;
    // application pages never create a browser tab or a second web window.
    if (!url.startsWith(serverUrl)) shell.openExternal(url);
    return { action: 'deny' };
  });
  mainWindow.webContents.on('will-navigate', (event, url) => {
    if (!url.startsWith(serverUrl)) { event.preventDefault(); shell.openExternal(url); }
  });
}

function showStartupError(message) {
  if (app.isQuitting) return;
  startupLog(message);
  dialog.showErrorBox(APP_TITLE, message);
  app.quit();
}

const locked = app.requestSingleInstanceLock();
if (!locked) app.quit();
app.on('second-instance', () => { if (mainWindow) { if (mainWindow.isMinimized()) mainWindow.restore(); mainWindow.focus(); } });
app.on('window-all-closed', () => app.quit());
app.on('before-quit', () => { app.isQuitting = true; stopBackend(); });
app.whenReady().then(async () => {
  app.setName(APP_TITLE);
  startupLog(`Application starting. Packaged: ${app.isPackaged}`);
  createSplash();
  port = await choosePort();
  if (!port) return showStartupError('No local network port is available for the application service.');
  serverUrl = `http://127.0.0.1:${port}`;
  try {
    startBackend();
  } catch (error) {
    startupLog('Backend startup validation failed.', error);
    return showStartupError(`The local application service could not start.\n\n${error.message}`);
  }
  if (await waitForServer()) createMainWindow();
  else showStartupError('Adinath Agri Services took too long to start. Please restart the application.');
});
