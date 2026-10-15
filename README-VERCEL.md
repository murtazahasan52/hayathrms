# Hayat HRMS — deploy on Vercel with MongoDB Atlas

This folder is ready to deploy. You don't need to build anything: the website is already built in `frontend/build`,
and the backend (FastAPI, Python) runs as a Vercel function.

## 1. MongoDB Atlas (free database)
1. Go to https://www.mongodb.com/atlas, sign up, and create a free **M0** cluster (region: Mumbai, ap-south-1).
2. **Database Access** → Add new database user → username + password (use letters and digits only, no @ : / #).
3. **Network Access** → Add IP address → **Allow access from anywhere** (`0.0.0.0/0`). Vercel has no fixed IP.
4. **Database** → Connect → Drivers → copy the connection string:
   `mongodb+srv://USER:PASSWORD@cluster0.xxxxx.mongodb.net/?retryWrites=true&w=majority`
   and put your real password in place of `<db_password>`.

## 2. GitHub
1. github.com → New repository → name `hayat-hrms` → **Private** → Create.
2. Click **uploading an existing file** and drag in **everything inside this folder**
   (`vercel.json`, `pyproject.toml`, `requirements.txt`, `backend`, `frontend`, …) → Commit.
   `vercel.json` must be at the top level of the repository, not inside a sub-folder.

## 3. Vercel
1. vercel.com → sign in with GitHub → **Add New → Project** → Import `hayat-hrms`.
2. Framework Preset shows **FastAPI** (from vercel.json). Root Directory: `./`. Leave build settings empty.
3. Open **Environment Variables** and add:

| Name | Value |
|---|---|
| `MONGO_URL` | your Atlas connection string from step 1 |
| `DB_NAME` | `hayat_hrms` |
| `JWT_SECRET` | any long random text, e.g. 40 random letters |
| `SUPERADMIN_EMAIL` | `murtaza@hayatcctv.com` |
| `SUPERADMIN_PASSWORD` | your first password (change it after the first login) |
| `PUBLIC_BASE_URL` | `https://<your-project>.vercel.app` (or your own domain) |
| `CRON_SECRET` | any random text (protects the scheduled-jobs link) |
| `LICENSE_PRIVATE_KEY` | the value from **Hayat-License-Private-Key.txt** — makes this your licence server |
| `DEFAULT_TZ` | `Asia/Kolkata` |

4. Click **Deploy**. When it finishes, open the link → sign in with SUPERADMIN_EMAIL / SUPERADMIN_PASSWORD.
   (The super admin is created automatically on the first sign-in.)
5. If you add `PUBLIC_BASE_URL` after the first deploy: Deployments → ⋯ → **Redeploy**.

## Check it works
Open `https://<your-project>.vercel.app/api/health`
- `"database":"ok"` → all good, open the site and sign in.
- otherwise it says what is wrong (MONGO_URL password, `<db_password>` left in, Atlas Network Access…).
  Fix the variable in Vercel → Settings → Environment Variables → **Redeploy**.

If the site shows an error page instead of the login, it now prints the reason — send a screenshot.

## 4. Scheduled jobs (WhatsApp summary, absent alerts, static-IP machine pulls)
- Vercel's free plan runs `/api/cron/tick` once a day (about 12 pm IST) from `vercel.json`.
- For exact times / every 5 minutes: create a free job at **https://cron-job.org** calling
  `https://<your-project>.vercel.app/api/cron/tick?key=<CRON_SECRET>` every 5 minutes.

## 5. Biometric machines at the shop (added "locally")
Vercel is on the internet and cannot see 192.168.x.x machines by itself. One small program at the shop does it:
1. In the web app: **Biometric devices → Add machine** → mode **LAN agent** → enter the machine's local IP (e.g. 192.168.10.3).
2. Copy the **Agent setup code** from the LAN agent box on the same page.
3. On any always-on PC at the shop run **Hayat-Sync-Agent-Setup.exe** (Python is inside — nothing else to install)
   and paste the setup code when it asks. It starts with Windows and sends punches every minute.
- eSSL with ADMS + HTTPS option → server = your domain, port 443, HTTPS on (no agent needed).
- Hikvision → HTTP push (HTTPS, port 443) or the LAN agent.
- Machines on a static public IP → "Direct pull" works through the cron above or the **Sync all** button.

## Logins with validity (super admin)
**Logins & validity** in the super-admin menu: create an owner / manager / employee login for any shop with a
"valid till" date. After that date the login cannot sign in until you renew it (+1 month / +1 year buttons).

## Employee login
Owner → **Employees** → **Login** on an employee → set a password. The employee signs in with their mobile
number and sees only their own attendance (first IN, last OUT, all punches, hours, OT) and final salary slips.

## 6. Licence server
With `LICENSE_PRIVATE_KEY` set, **Licence keys** appears in the super-admin menu. Customers' local installs
activate with the short code + this site's address, and renewals you make here reach them automatically.

## 7. Custom domain (optional)
Vercel → Project → Settings → **Domains** → add `hrms.hayatcctv.com` → add the DNS record it shows at your
domain provider → update `PUBLIC_BASE_URL` → Redeploy.

## Updating later
Upload the new files to the same GitHub repository (replace) → Vercel redeploys by itself in about a minute.

Hayat Enterprises, Nagpur · Sales 96738 78652 · Services 84462 55253
