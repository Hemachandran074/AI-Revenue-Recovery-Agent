# 🛡️ AI Revenue Recovery Agent

> **Autonomous, compliant, and auditable revenue recovery engine for modern payment stacks.**  
> Powered by **Gemini 3.1 Flash Lite**, **Razorpay Payment Gateway**, **Twilio WhatsApp**, **FastAPI**, and **React + Vite**.

---

## 📌 Overview

Failed payments account for **5% to 15% of lost revenue** across subscription and e-commerce platforms. Common recovery mechanisms rely on brute-force retries (which trigger bank fraud flags) or generic email blasts (which suffer low open rates and alienate customers).

The **AI Revenue Recovery Agent** introduces an intelligent, bounded 4-stage pipeline that:
1. **Detects** payment failures within seconds via webhooks and real-time background sync.
2. **Diagnoses** the precise root cause using **Google Gemini 3.1 Flash Lite** against a strict 8-cause taxonomy with confidence scoring.
3. **Decides** the optimal recovery action deterministically while enforcing **4 non-negotiable compliance guardrails**.
4. **Executes** personalized, friction-free recovery links over **WhatsApp** or schedules quiet provider retries.
5. **Measures & Audits** 100% of pipeline stages with structured JSON records and immutable database trails.

---

## 🏗️ 4-Stage Architecture Pipeline

```
  ┌─────────────────┐
  │ Payment Gateway │  (Razorpay / Stripe)
  └────────┬────────┘
           │ 1. payment.failed event (<3s)
           ▼
  ┌─────────────────┐
  │  Stage 1: DETECT │  HMAC validation • PAN stripping • Identity resolution
  └────────┬────────┘
           │ Sanitized event record
           ▼
  ┌──────────────────┐
  │ Stage 2: DIAGNOSE│  Gemini 3.1 Flash Lite • 8-Cause Taxonomy • Confidence & Reasoning
  └────────┬─────────┘
           │ Root Cause + Confidence
           ▼
  ┌──────────────────┐
  │  Stage 3: DECIDE │  Deterministic Rules • Guardrail Checks (Attempts, Quiet Hours, 7d Stop)
  └────────┬─────────┘
           │ Approved ActionPlan
           ▼
  ┌──────────────────┐
  │ Stage 4: EXECUTE │  Razorpay Recovery Link • Twilio WhatsApp • Silent Retry Scheduling
  └────────┬─────────┘
           │
           ▼
  ┌──────────────────┐
  │ 📊 Observability │  React 19 Dashboard • Real-time Metrics • Full 4/4 Stage Audit Trail
  └──────────────────┘
```

---

## 🧠 Root Cause Taxonomy & Action Matrix

The agent operates strictly on a closed, documented action matrix to ensure safety, auditability, and compliance:

| Root Cause | AI Classification Rationale | Recovery Action | Channel | Customer Experience |
|---|---|---|---|---|
| **`sca_abandoned`** | Customer dropped off during 3DS OTP bank screen | `send_fresh_auth_link` | **WhatsApp** | Receives a fresh 3DS authentication link on WhatsApp |
| **`card_expired`** | Bank declined card due to passed validity date | `send_update_payment_method_link` | **WhatsApp** | Receives a secure link to update card / payment method |
| **`checkout_friction`** | Customer abandoned modal before selecting provider | `send_reminder` | **WhatsApp** | Receives a 1-click checkout recovery reminder |
| **`genuine_abandonment`** | Customer explicitly exited checkout flow | `send_reminder` | **WhatsApp** | Single reminder sent; no repeated chasing |
| **`insufficient_funds`** | Issuer declined due to temporary balance shortage | `schedule_retry` | *None (Silent)* | Auto-scheduled after payday or flat interval |
| **`network_error`** | Gateway or acquirer timeout during processing | `schedule_retry` | *None (Silent)* | Single silent retry after gateway stabilizes (1h) |
| **`bank_risk_block`** | Fraud flag or international card block | `escalate_to_human_review` | *None (Internal)* | Safely queued for human risk review; never auto-retried |
| **`unknown`** | Uninformative or opaque bank decline code | `escalate_to_human_review` | *None (Internal)* | Safely escalated to operations team |

---

## 🛡️ Non-Negotiable Compliance Guardrails

Every decision must pass **4 programmatic guardrails** before any money movement or customer contact occurs:

1. **Max Recovery Attempts**: Hard stop after **3 recovery attempts** (configurable in testing) per invoice/order to prevent harassment.
2. **Contact Frequency Limit**: Minimum **24 hours** between customer contacts to respect user attention.
3. **Quiet Hours Enforcement**: No messages sent outside allowed local customer hours (`09:00 - 20:00` customer timezone). Messages are deferred to the next legal window.
4. **7-Day Hard Stop**: Automated recovery closes 7 days after the initial failure; remaining unpaid amounts escalate to human review.

---

## 💻 Modern Tech Stack

### Frontend Dashboard
* **Framework**: React 19, TypeScript, Vite
* **Styling**: Tailwind CSS, Glassmorphic Dark Slate Theme
* **Icons & Animation**: Lucide React, Framer Motion
* **Live Features**: Real-time 3s auto-polling, 1-click simulator modal, search/filter table, expandable 4-stage audit drawer.

### Backend Engine
* **Framework**: FastAPI (Python 3.11+)
* **AI/LLM**: Google Gemini 3.1 Flash Lite (`google-genai` SDK) with structured schema outputs
* **Payments**: Razorpay API (Test Mode Orders & Payment Links)
* **Messaging**: Twilio WhatsApp API
* **Database**: PostgreSQL with SQLAlchemy ORM (8 relational audit tables)
* **Background Worker**: Async real-time failure poller (`app.poller`)

---

## 📂 Project Structure

```
.
├── backend/
│   ├── app/
│   │   ├── detect.py          # Stage 1: Webhook signature, PAN stripping & customer join
│   │   ├── diagnose.py        # Stage 2: Gemini LLM root cause classifier
│   │   ├── decide.py          # Stage 3: Deterministic rules & guardrail evaluator
│   │   ├── execute.py         # Stage 4: Razorpay link generator & Twilio WhatsApp dispatcher
│   │   ├── guardrails.py      # The 4 compliance stopping rules
│   │   ├── channels.py        # Twilio WhatsApp client & Razorpay link fallback handler
│   │   ├── poller.py          # Real-time 3s background auto-sync worker
│   │   ├── pipeline.py        # 4-stage orchestrator
│   │   ├── dashboard.py       # Metrics aggregator & HTML/JSON reporting
│   │   ├── main.py            # Ops API server (port 8000)
│   │   ├── tunnel.py          # Public webhook intake server (port 8001)
│   │   ├── demo_recovery.py   # CLI recovery simulator
│   │   ├── trigger_failure.py # Real test-mode payment link generator
│   │   ├── models.py          # 8 SQLAlchemy ORM database models
│   │   └── schemas.py         # Pydantic data schemas mirroring architecture.md
│   ├── prompts/
│   │   └── diagnose.md        # Versioned LLM system prompt (v2)
│   └── tests/                 # 21 test suites, 783 automated tests
│
├── frontend/
│   ├── src/
│   │   ├── components/
│   │   │   ├── Header.tsx           # Brand header, live status pulse, quick actions
│   │   │   ├── MetricsOverview.tsx  # Hero cards (Revenue at risk/recovered, Latency, Violations)
│   │   │   ├── PipelineVisualizer.tsx# Live 4-stage stepper (DETECT -> DIAGNOSE -> DECIDE -> EXECUTE)
│   │   │   ├── SimulatorPanel.tsx   # 1-Click test simulator & checkout creator
│   │   │   ├── EventsTable.tsx      # Filterable, searchable batch events table with IST timestamps
│   │   │   ├── EventDetailModal.tsx # Full audit modal with JSON payloads & guardrail checks
│   │   │   └── ReadinessDrawer.tsx  # System health & credential capability monitor
│   │   ├── services/api.ts          # REST client for backend API
│   │   ├── App.tsx                  # Main dashboard layout
│   │   └── index.css                # Tailwind CSS & theme tokens
│   └── vite.config.ts               # Vite configuration with API proxies
│
├── docker-compose.yml         # PostgreSQL 16 container definition
├── GUIDE.md                   # Comprehensive step-by-step operations guide
└── RESULTS.md                 # Benchmark metrics and validation report
```

---

## 🚀 Getting Started

### 1. Prerequisites
* **Python 3.11+**
* **Node.js 18+** & **npm**
* **Docker Desktop** (for PostgreSQL)
* **ngrok** (for public webhook forwarding)

---

### 2. Backend Setup

1. **Navigate to the backend directory and create a virtual environment**:
   ```powershell
   cd backend
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

2. **Configure Environment Variables**:
   ```powershell
   Copy-Item ..\.env.example ..\.env
   ```
   Open `.env` and fill in your test-mode credentials:
   ```ini
   APP_ENV=development
   GEMINI_API_KEY=your_gemini_api_key
   RAZORPAY_KEY_ID=rzp_test_...
   RAZORPAY_KEY_SECRET=your_razorpay_secret
   RAZORPAY_WEBHOOK_SECRET=your_webhook_secret
   TWILIO_ACCOUNT_SID=your_twilio_sid
   TWILIO_AUTH_TOKEN=your_twilio_auth_token
   TWILIO_FROM_NUMBER=whatsapp:+14155238886
   TWILIO_WHATSAPP_TEST_RECIPIENTS=whatsapp:+919566687795
   ```

3. **Start PostgreSQL Database**:
   ```powershell
   docker compose up -d --wait
   ```

4. **Initialize Database Tables**:
   ```powershell
   .\.venv\Scripts\python.exe -c "from app.db import init_db; init_db()"
   ```

5. **Start the Backend API Server**:
   ```powershell
   .\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000 --reload
   ```

---

### 3. Frontend Setup

1. **Navigate to the frontend directory**:
   ```powershell
   cd frontend
   npm install
   ```

2. **Launch Vite Development Server**:
   ```powershell
   npm run dev
   ```
3. Open **[http://localhost:5173/](http://localhost:5173/)** in your browser.

---

### 4. Setting Up the Webhook Tunnel (for Live Razorpay Testing)

1. **Start the Public Webhook Intake Server**:
   ```powershell
   .\.venv\Scripts\python.exe -m uvicorn app.tunnel:app --port 8001 --reload
   ```

2. **Start ngrok Tunnel**:
   ```powershell
   ngrok http 8001
   ```

3. **Register Webhook in Razorpay Dashboard**:
   * Go to **Razorpay Dashboard $\rightarrow$ Account & Settings $\rightarrow$ Webhooks**.
   * Add URL: `https://<your-ngrok-domain>.ngrok-free.dev/webhooks/razorpay`
   * Subscribe to event: **`payment.failed`**
   * Webhook Secret: Enter the same secret as `RAZORPAY_WEBHOOK_SECRET` in `.env`.

---

## 🧪 Testing & Recovery Simulation

### Method 1: Interactive UI Simulator (Fastest)
1. Open the dashboard at **[http://localhost:5173/](http://localhost:5173/)**.
2. Click **"Simulate Failure"** in the top navigation bar.
3. Select any failure cause (e.g. **3DS Auth Drop-off**, **Expired Card**, or **Checkout Friction**).
4. Click **"Simulate & Execute Recovery"**.
5. Check your WhatsApp on `+919566687795` — the personalized recovery link arrives in seconds!

---

### Method 2: Live Browser Payment Gateway Simulation
1. In the UI, click **"Test Checkout"** $\rightarrow$ **"Generate Test Gateway Session"**.
2. Click **"Open Razorpay Checkout in Browser"**.
3. Select **Card** and use Razorpay's domestic test card:
   * **Card Number**: `4012 0000 0000 0002`
   * **Expiry**: `12/28` | **CVV**: `123`
4. On the simulated bank 3DS verification screen, click **"Cancel"**.
5. The background poller will automatically detect the drop-off, Gemini AI will diagnose `sca_abandoned`, and the recovery message will be delivered to your WhatsApp immediately.

---

## 📊 Verification & Test Suite

Run the full automated test suite (783 tests covering schema contracts, guardrails, LLM evaluations, and idempotency):

```powershell
# Run all unit tests
.\.venv\Scripts\python.exe -m pytest

# Run database integration tests
.\.venv\Scripts\python.exe -m pytest -m integration

# Run linter
.\.venv\Scripts\python.exe -m ruff check app tests
```

---

## 🔒 Security & Privacy Guarantees

* **Test-Mode Enforcement**: The application strictly refuses to start if live keys (`sk_live_` / `rzp_live_`) are detected in configuration.
* **Zero PAN Storage**: Full card numbers and CVVs are stripped in Stage 1 (`DETECT`) and never stored in database tables or passed to LLMs.
* **Separation of Concerns**: Unauthenticated operations endpoints (`/dashboard`, `/api/*`) run exclusively on loopback (`port 8000`), while only the signature-verified webhook endpoint is exposed via tunnel (`port 8001`).
* **Schema Contract Testing**: Unit tests actively parse `context/architecture.md` and fail if code drift occurs across enums, rules, or action sets.

---

## 📄 License

Built for the **Razorpay Buildathon**.  
Demo & Test-Mode implementation only.
