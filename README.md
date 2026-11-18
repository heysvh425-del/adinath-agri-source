# Adinath Agri Services

FastAPI and SQLite billing/inventory application with a static frontend.

## First launch

Run `start.bat` on Windows or `./start.sh` on macOS/Linux, then open
`http://127.0.0.1:8000`. The first screen creates the administrator account.
No default credentials, products, customers, invoices, or other business
records are created by startup.

## Windows desktop application

The application can also be delivered as a native Windows desktop program. It
opens in its own **Adinath Agri Services** window (not Chrome/Edge), with the
standard Windows minimize, maximize, and close controls. The desktop version
starts the same FastAPI application locally, so reports, PDF/CSV/Excel exports,
invoices, authentication, and all existing screens work unchanged.

From the project root, run the following once to install the desktop build
tools (use `npm.cmd` in PowerShell if script execution is restricted):

```powershell
npm.cmd install
backend\venv\Scripts\python.exe -m pip install -r backend\requirements-dev.txt
```

For a development desktop window, use:

```powershell
npm.cmd run desktop:dev
```

To build the installable Windows `.exe` (NSIS installer), use:

```powershell
npm.cmd run desktop:dist
```

The installer is created in `desktop\release`. On Windows, both the desktop
app and the browser app at `http://127.0.0.1:8000` use the same database at
`%APPDATA%\adinath-agri-services-desktop\data\adinath.db`, outside Program
Files, so both interfaces show the same products, customers, invoices, and
stock. `ADINATH_DB_PATH` can override this location for development or tests.
Back up the shared file while both apps are closed.

## Data behavior

- SQLite is the source of truth for products, customers, invoices, inventory,
  and dashboard/report values. On Windows, localhost and desktop use the same
  per-user database; the old `backend/adinath.db` remains as an untouched
  migration source/backup after the data merge.
- New installations start with an empty database schema.
- Products and customers are created only with their respective forms.
- Invoices are created through billing; totals, tax, balance, status, and stock
  deductions are calculated and validated by the backend. Receipts can be
  recorded against outstanding invoices from the Receivables screen.
- The dashboard, receivables, transaction list, top-products report, sales
  performance, and payment-method report return empty/zero states until actual
  records exist.

## Development checks

From `backend/`, run:

```powershell
venv\Scripts\python.exe -m compileall app
venv\Scripts\python.exe -m pip install -r requirements-dev.txt
venv\Scripts\python.exe -m pytest -q
```

## Data protection

Back up the active database while both application interfaces are stopped. For production,
automate a daily encrypted copy to storage outside the server and periodically
test restoring that copy. SQLite is appropriate for a small single-location
business; move to a managed database before serving concurrent locations.

## Production notes

Set `ADINATH_ENV=production` and a strong `ADINATH_SECRET_KEY` before
deployment. Keep the app behind HTTPS and restrict `ADINATH_CORS_ORIGINS` to
the exact frontend origins. The current application implements products, customers, invoices,
inventory, receivables, receipt records, and reports; it does not yet provide
quotation, expense, vendor, credit-note, or debit-note modules.
