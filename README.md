# External Link Auditor

A tool that crawls your entire website and generates an interactive report showing every external link on every page — so you can identify unnecessary links and replace them with authoritative (.gov, .edu) sources.

**Two versions included:**
- `app.py` — Streamlit web app (deploy online or run locally)
- `crawler.py` — CLI script (run from terminal, outputs HTML report)

---

## 🚀 Deployment Options

### Option 1: Streamlit Cloud (Free, Recommended)

The easiest way — deploy in 5 minutes, no server needed.

1. **Push to GitHub:**
   ```bash
   # Create a new repo on GitHub, then:
   git init
   git add .
   git commit -m "External Link Auditor"
   git remote add origin https://github.com/YOUR-USERNAME/external-link-auditor.git
   git push -u origin main
   ```

2. **Deploy on Streamlit Cloud:**
   - Go to [share.streamlit.io](https://share.streamlit.io)
   - Sign in with your GitHub account
   - Click **"New app"**
   - Select your repo → Branch: `main` → Main file: `app.py`
   - Click **Deploy**

3. **Access your tool at:** `https://YOUR-APP-NAME.streamlit.app`

> Free tier gives you 1 app with reasonable usage. Perfect for internal team tools.

---

### Option 2: Run Locally

```bash
# Install dependencies
pip install -r requirements.txt

# Run the web app
streamlit run app.py

# Opens automatically at http://localhost:8501
```

---

### Option 3: Deploy on Your Own Server (VPS/Cloud)

If you want it on your own domain or behind a firewall:

```bash
# On your server (Ubuntu/Debian)
sudo apt update && sudo apt install python3-pip python3-venv -y

# Clone your repo
git clone https://github.com/YOUR-USERNAME/external-link-auditor.git
cd external-link-auditor

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run (accessible on port 8501)
streamlit run app.py --server.port 8501 --server.address 0.0.0.0
```

**To keep it running permanently**, use a systemd service:

```bash
sudo nano /etc/systemd/system/link-auditor.service
```

Paste this:
```ini
[Unit]
Description=External Link Auditor
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/external-link-auditor
ExecStart=/home/ubuntu/external-link-auditor/venv/bin/streamlit run app.py --server.port 8501 --server.address 0.0.0.0
Restart=always

[Install]
WantedBy=multi-user.target
```

Then:
```bash
sudo systemctl enable link-auditor
sudo systemctl start link-auditor
```

**Optional: Add Cloudflare Tunnel** to put it behind your domain (e.g., `tools.ovlg.com`):
```bash
cloudflared tunnel --url http://localhost:8501
```

---

### Option 4: CLI Only (No Web UI)

Just run the Python script directly:

```bash
pip install requests beautifulsoup4

python crawler.py https://www.ovlg.com --max-pages 1000 -o ovlg_report.html
```

Open the HTML file in any browser.

---

## What It Does

1. **Discovers all pages** on your site via sitemap.xml + internal link spidering
2. **Extracts every external link** from each page (URL, anchor text, domain, rel attributes)
3. **Classifies domains** as Authority (.gov, .edu, CFPB, FTC, NCUA, etc.) or Non-Authority
4. **Three interactive views:**
   - **Pages View**: Every page + its external link count (expandable to see details)
   - **Domains View**: Every external domain ranked by frequency, filterable by authority status
   - **All Links View**: Flat list of every external link with source page
5. **CSV export** for each view

## Usage Parameters

| Parameter     | Default | Description                          |
|---------------|---------|--------------------------------------|
| Website URL   | —       | Your website URL (required)          |
| Max Pages     | 500     | Maximum pages to crawl               |
| Delay         | 0.3s    | Seconds between requests             |

## Authority Domain Detection

Auto-flags these as "Authority":
- `.gov`, `.edu`, `.mil` TLDs
- CFPB, FTC, SEC, IRS, FDIC, NCUA, CDC, NIH, FDA
- Wikipedia, Britannica, Reuters, AP News

Customize in the sidebar (web app) or edit `AUTHORITY_DOMAINS` in the code.

## Workflow for Your Sites

```bash
# Audit each property
streamlit run app.py
# Then enter: www.ovlg.com → Start Audit
# Then enter: www.debtconsolidationcare.com → Start Audit
# Then enter: www.savantcare.com → Start Audit
```

1. Go to **Domains View** → filter **Non-Authority Only**
2. Identify domains to remove or replace
3. **Download CSV** → share with content team
4. Re-run monthly to track progress
