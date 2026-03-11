# SAM x HomeAssistant

A bidirectional integration between **Solace Agent Mesh (SAM)** and **Home Assistant** that turns your smart home into a conversational AI system — and lets your home trigger AI workflows back.

> **Note:** This is a hackathon / personal project. It works well for its intended use case but has not been hardened for production deployment. Expect rough edges, evolving APIs, and minimal test coverage.

---

## What It Does

### SAM -> Home Assistant (You talk to your home)

Chat or speak with SAM through a web UI. SAM routes your request to the right agent:

- **"Turn off all the downstairs lights"** -> HomeAssistant Agent controls devices via HA REST API
- **"What's been happening with the washing machine sensor?"** -> HomeAssistant Agent fetches history, analysis tools detect the wash cycle, a chart is generated
- **"Is the Sonos Arc compatible with my receiver?"** -> Research Agent searches the web for a current answer
- **"Give me a weekly energy usage report as a PDF"** -> Orchestrator coordinates HA data retrieval, Visualization Agent creates charts, PDF Report Agent compiles the document

### Home Assistant -> SAM (Your home talks back)

HA automations can send requests to SAM over MQTT. This means your home can:

- Trigger SAM workflows from time-based automations (e.g. 7am briefing)
- Route HA voice assistant queries through SAM's full agent mesh
- Fire off complex multi-agent tasks from HA scripts or automations

### SAM Workflows -> Home Assistant Services (Auto-discovery)

The included `sam_workflows` custom HA integration subscribes to SAM's agent discovery topic. Any SAM workflow automatically appears in Home Assistant as a native service — no HA config changes needed when you deploy new workflows.

---

## Architecture

```
  Browser / Voice
       |
       v
  +------------------+     +-------------------+
  | Web UI Gateway   |     | Solace Broker     |
  | (port 8000)      |<--->| (SMF + MQTT)      |
  +------------------+     +-------------------+
       |                          ^       ^
       v                          |       |
  +--------------------+          |       |
  | OrchestratorAgent  |          |       |
  +--------------------+          |       |
       |                          |       |
       +---> HomeAssistantAgent   |       |
       |      +-- HA REST tools   |       |
       |      +-- Data analysis   |       |
       |                          |       |
       +---> ResearchAgent        |       |
       |      +-- Google Search   |       |
       |                          |       |
       +---> VisualizationAgent   |       |
       |      +-- Plotly charts   |       |
       |                          |       |
       +---> PDFReportAgent       |       |
              +-- fpdf2 docs      |       |
                                  |       |
  +---------------------------+   |       |
  | Home Assistant            |   |       |
  |  +-- MQTT Integration ----+---+       |
  |  +-- sam_workflows component ---------+
  |       +-- Conversation agent          |
  |       +-- STT / TTS providers         |
  |       +-- Workflow service discovery  |
  +---------------------------------------+

  +---------------------------+
  | SAM Workflows             |
  |  House Status Report      |
  |  Morning Briefing         |
  |  Evening Routine          |
  |  Device Health Check      |
  |  Sensor Analysis Report   |
  |  Create Automation        |
  |  ...and more              |
  +---------------------------+
```

All agents and workflows communicate via the Solace broker. The Web UI Gateway handles browser sessions. The HA custom component (`sam_workflows`) connects to the same broker via MQTT and provides a conversation agent, STT/TTS providers, and automatic workflow-to-service mapping.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Docker + Docker Compose | For the recommended deployment |
| Solace broker | Free cloud broker at [solace.com/try-it-now](https://solace.com/try-it-now) |
| Home Assistant instance | With a Long-Lived Access Token (Profile -> Security) |
| LLM API key | Gemini (free tier) recommended — [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |

Optional:

| Requirement | Notes |
|---|---|
| HA MQTT integration | Required for HA -> SAM communication (voice, automations triggering SAM) |
| Google Search API key + CSE ID | Required for the Research Agent |

---

## Deployment

### 1. Clone and configure

```bash
git clone git@github.com:bwiebe-solace/sam-homeassistant-integration.git
cd sam-homeassistant-integration
cp .env.example .env
```

Edit `.env` and fill in at minimum:

```env
# Solace broker
SOLACE_BROKER_URL=wss://your-broker.messaging.solace.cloud:443
SOLACE_BROKER_USERNAME=solace-cloud-client
SOLACE_BROKER_PASSWORD=your-password
SOLACE_BROKER_VPN=your-vpn-name

# LLM — Gemini via OpenAI-compatible endpoint
LLM_SERVICE_API_KEY=your-gemini-api-key

# Home Assistant
HA_URL=https://your-ha-instance.ui.nabu.casa
HA_TOKEN=your-long-lived-access-token

# Namespace — isolates SAM topics/queues on the broker
NAMESPACE=your-namespace

# Production settings
USE_TEMPORARY_QUEUES=false

# Session cookie signing — generate with: python -c "import secrets; print(secrets.token_hex(32))"
SESSION_SECRET_KEY=your-generated-key
```

### 2. Start

```bash
docker compose up -d
```

The web UI is available at `http://<host>:8000`.

Docker images are built automatically on every push to `main` and published to GHCR. To update a running deployment:

```bash
docker compose pull && docker compose up -d
```

### 3. (Optional) Enable the Research Agent

Add Google Custom Search credentials to `.env`:

```env
GOOGLE_SEARCH_API_KEY=your-google-api-key
GOOGLE_CSE_ID=your-cse-id
```

- API key: [console.cloud.google.com](https://console.cloud.google.com) -> enable "Custom Search API"
- CSE ID: [cse.google.com](https://cse.google.com) -> create a search engine set to "Search the entire web"

### 4. (Optional) Enable the HA MQTT gateway

The MQTT gateway allows Home Assistant automations to send requests to SAM. It is disabled by default. To enable it, rename the config file to remove the `_` prefix:

```bash
mv configs/gateways/_ha-mqtt.yaml configs/gateways/ha-mqtt.yaml
```

Then add these to your `.env`:

```env
SOLACE_MQTT_HOST=your-broker.messaging.solace.cloud
SOLACE_MQTT_PORT=8883
SOLACE_MQTT_TLS=true
```

---

## Home Assistant Setup

### Install the SAM Workflows custom component

The `sam_workflows` custom component provides:

- **Conversation agent** — Route HA voice queries through SAM's full agent mesh
- **STT / TTS providers** — Speech-to-text and text-to-speech via SAM
- **Workflow discovery** — SAM workflows auto-register as native HA services

#### Install via HACS (recommended)

1. In HACS, go to **Integrations -> Custom repositories**
2. Add `https://github.com/bwiebe-solace/sam-homeassistant-integration` as an Integration
3. Install **SAM Workflows**
4. Restart Home Assistant

#### Install manually

1. Copy `custom_components/sam_workflows/` into your HA `config/custom_components/` directory
2. Restart Home Assistant

#### Configure

1. In HA, go to **Settings -> Devices & Services -> Add Integration** and search for **SAM Workflows**
2. Complete the config flow (broker connection details)

Once installed, any workflow deployed to SAM automatically appears as `sam_workflows.<workflow_name>` in HA's services list.

### Connect HA to the Solace broker via MQTT

Required for HA automations to trigger SAM and for the conversation agent. In HA, go to **Settings -> Devices & Services -> Add Integration -> MQTT** and configure:

| Field | Value |
|---|---|
| Broker | Your Solace broker hostname |
| Port | `8883` (TLS) or `1883` (plaintext) |
| Username | Your Solace username |
| Password | Your Solace password |

### Trigger SAM from automations

Publish an MQTT message from any HA automation to send a request to SAM:

```yaml
action:
  - action: mqtt.publish
    data:
      topic: "home/sam/request"
      payload: '{"text": "Good morning briefing for Ben"}'
```

### Use SAM workflows as HA services

Once the custom component discovers a workflow, call it like any native service:

```yaml
action:
  - action: sam_workflows.house_status_report
    data:
      notify_service: "notify.mobile_app_phone"
```

---

## Agents

| Agent | What It Does |
|---|---|
| **OrchestratorAgent** | Default entry point. Routes requests to the right agent or coordinates multi-agent tasks. |
| **HomeAssistantAgent** | Controls devices, queries state and history, manages automations/scripts/helpers, reads calendars and logbook, analyses sensor data, manages dashboards, queries device/area/floor registries. |
| **ResearchAgent** | Web search for current information — device specs, HA docs, troubleshooting, news. Supports deep multi-source research. Requires Google Search API keys. |
| **VisualizationAgent** | Creates Plotly charts (line, bar, scatter, pie, heatmap, gauge, etc.) from any data. Outputs PNG or SVG artifacts. |
| **PDFReportAgent** | Generates formatted PDF documents combining text, tables, and embedded charts. |

---

## Workflows

Workflows are pre-built multi-step tasks that the orchestrator or Home Assistant can trigger. They appear automatically in the SAM web UI and (via the custom component) as HA services.

| Workflow | Description |
|---|---|
| **House Status Report** | Queries active lights, climate, media, and switches, then sends a summary notification via a specified notify service. |
| **Morning Briefing** | Compiles weather, calendar, and device status into a morning summary notification. |
| **Evening Routine** | Dims lights, adjusts thermostat, and pauses media for wind-down. |
| **Sensor Analysis Report** | Pulls history for a sensor entity, runs statistical analysis, generates a chart, and sends findings. |
| **Create Automation** | Creates a Home Assistant automation from a plain-English description. |
| **Device Health Check** | Audits device battery levels, unavailable entities, and connectivity issues, then sends a health report. |
| **Air Quality Report** | Analyses air quality sensor history (radon, CO2, PM2.5) and sends a report. |
| **Security Roundup** | Reviews door/window sensors, motion detectors, and lock status, then sends a security summary. |
| **Weekly Home Digest** | Compiles a week-long overview of energy usage, device activity, and notable events. |

---

## Home Assistant Agent Permissions

The HA agent's write capabilities are controlled via `.env` flags:

| Variable | Default | Effect |
|---|---|---|
| `HA_READONLY` | `false` | Master switch — disables all write/control tools when `true` |
| `HA_ALLOW_DEVICE_CONTROL` | `true` | Allow `call_service` for lights, switches, climate, media |
| `HA_ALLOW_SCRIPT_EXECUTION` | `true` | Allow triggering scripts and automations |
| `HA_ALLOW_CONFIG_WRITE` | `false` | Allow creating/updating automations, scripts, helpers, and dashboards |
| `HA_ALLOW_DELETES` | `false` | Allow deleting automations and scripts (irreversible) |

---

## Local Development

Running outside Docker requires a Python virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Set `PYTHON_BIN` in your `.env` to the venv Python so the MCP tool servers can find it:

```env
PYTHON_BIN=/path/to/your/.venv/bin/python
```

Then run:

```bash
sam run --system-env
```

---

## Project Structure

```
.
+-- agents/
|   +-- homeassistant/          # HA REST + WebSocket tools MCP server
|   +-- data-analysis/          # Timeseries analysis MCP server
|   +-- pdf-report/             # PDF generation MCP server
+-- configs/
|   +-- agents/                 # Agent YAML configs
|   +-- gateways/               # Web UI and HA MQTT gateway configs
|   +-- workflows/              # SAM workflow YAML configs
+-- custom_components/
|   +-- sam_workflows/          # HA custom integration (conversation, STT, TTS, workflow discovery)
+-- gateways/
|   +-- ha_mqtt/                # HA MQTT gateway adapter
+-- .github/
|   +-- workflows/docker.yml    # CI: build and push Docker image to GHCR on push to main
+-- shared_config.yaml          # Shared broker, LLM, and service config anchors
+-- docker-compose.yml
+-- Dockerfile
+-- requirements.txt
+-- hacs.json                   # HACS metadata for custom component installation
```
