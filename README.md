# SAM × HomeAssistant

A bidirectional integration between **Solace Agent Mesh (SAM)** and **HomeAssistant** that turns your smart home into a conversational AI system — and lets your home trigger AI workflows back.

---

## What It Does

### SAM → HomeAssistant (You talk to your home)

Chat or speak with SAM through a web UI. SAM routes your request to the right agent:

- **"Turn off all the downstairs lights"** → HomeAssistant Agent controls devices via HA REST API
- **"What's been happening with the washing machine sensor?"** → HomeAssistant Agent fetches history, analysis tools detect the wash cycle, a chart is generated
- **"Is the Sonos Arc compatible with my receiver?"** → Research Agent searches the web for a current answer
- **"Give me a weekly energy usage report as a PDF"** → Orchestrator coordinates HA data retrieval, Visualization Agent creates charts, PDF Report Agent compiles the document

### HomeAssistant → SAM (Your home talks back)

HA automations can send requests to SAM over MQTT. This means your home can:

- Trigger SAM workflows from time-based automations (e.g. 7am briefing)
- Route HA voice assistant queries through SAM's full agent mesh
- Fire off complex multi-agent tasks from HA scripts or automations

### SAM Workflows → HomeAssistant Services (Auto-discovery)

The included `sam_workflows` custom HA integration subscribes to SAM's agent discovery topic. Any SAM workflow tagged as a workflow type automatically appears in HomeAssistant as a native service — no HA config changes needed when you deploy new workflows.

---

## Architecture

```
  Browser / Voice
       │
       ▼
  Web UI Gateway ──────────────────────────────────────────────────────────┐
  (port 8000)                                                               │
       │                                                                    │
       ▼                                                           Solace Broker
  OrchestratorAgent ◄──────────────── HA MQTT Gateway ◄──── MQTT ──────── │
       │                               (HA automations,                     │
       │                                voice pipeline)                     │
       ├──► HomeAssistantAgent                                               │
       │         ├── HA REST tools (state, control, automations, history)   │
       │         ├── HA MCP server (/api/mcp)                               │
       │         └── Data analysis tools (timeseries, anomaly detection)    │
       │                                                                    │
       ├──► ResearchAgent                                                   │
       │         └── Google Custom Search (web_search, deep_research)      │
       │                                                                    │
       ├──► VisualizationAgent                                              │
       │         └── Plotly chart generation (PNG / SVG)                   │
       │                                                                    │
       └──► PDFReportAgent                                                  │
                 └── fpdf2 document generation                              │
                                                                            │
  HomeAssistant                                                             │
       ├── MQTT Integration ──────────────────────── MQTT ─────────────────┘
       └── sam_workflows custom component
               └── Subscribes to SAM discovery, registers workflows as HA services
```

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Docker + Docker Compose | For the recommended deployment |
| Solace broker | Free cloud broker at [solace.com/try-it-now](https://solace.com/try-it-now) |
| HomeAssistant instance | With a Long-Lived Access Token |
| HA MQTT integration | Configured to connect to the same Solace broker |
| LLM API key | Gemini (free tier) recommended — see below |
| Google Search API key + CSE ID | Only needed for the Research Agent |

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

# MQTT (same broker, hostname only)
SOLACE_MQTT_HOST=your-broker.messaging.solace.cloud

# LLM — Gemini via OpenAI-compatible endpoint
LLM_SERVICE_API_KEY=your-gemini-api-key

# HomeAssistant
HA_URL=https://your-ha-instance.ui.nabu.casa
HA_TOKEN=your-long-lived-access-token

# Session cookie signing
SESSION_SECRET_KEY=  # generate: python -c "import secrets; print(secrets.token_hex(32))"
```

### 2. Start

```bash
docker compose up -d
```

The web UI is available at `http://localhost:8000`.

### 3. (Optional) Enable the Research Agent

Add Google Custom Search credentials to `.env`:

```env
GOOGLE_SEARCH_API_KEY=your-google-api-key
GOOGLE_CSE_ID=your-cse-id
```

- API key: [console.cloud.google.com](https://console.cloud.google.com) → enable "Custom Search API"
- CSE ID: [cse.google.com](https://cse.google.com) → create a search engine set to "Search the entire web"

Free tier provides 100 queries/day.

---

## HomeAssistant Setup

### Connect HA to the Solace broker via MQTT

In HA, go to **Settings → Devices & Services → Add Integration → MQTT** and configure:

| Field | Value |
|---|---|
| Broker | Your Solace broker hostname (same as `SOLACE_MQTT_HOST`) |
| Port | `8883` (TLS) or `1883` (plaintext) |
| Username | Your Solace username |
| Password | Your Solace password |

### Send HA voice queries through SAM (optional)

Configure HA's voice assistant pipeline to use SAM as the conversation agent. In HA, go to **Settings → Voice Assistants** and set the conversation agent to the SAM MQTT integration. Voice queries will be routed through the full SAM agent mesh.

### Trigger SAM from automations (optional)

Publish an MQTT message from any HA automation to send a request to SAM:

```yaml
action:
  - action: mqtt.publish
    data:
      topic: "home/sam/request"
      payload: '{"text": "Good morning briefing for Ben"}'
```

### Install the SAM Workflows custom component (optional)

This component auto-discovers SAM workflows and registers them as native HA services.

1. Copy `custom_components/sam_workflows/` into your HA `config/custom_components/` directory.
2. In HA, go to **Settings → Devices & Services → Add Integration** and search for **SAM Workflows**.
3. Complete the config flow (broker connection details).

Once installed, any workflow deployed to SAM will automatically appear as `sam_workflows.<workflow_name>` in HA's services list — no restarts or config changes required. Reference them from automations like any native service:

```yaml
action:
  - action: sam_workflows.good_morning_workflow
    data:
      user: "Ben"
```

---

## Agents

| Agent | What It Does |
|---|---|
| **OrchestratorAgent** | Default entry point. Routes requests to the right agent or coordinates multi-agent tasks. |
| **HomeAssistantAgent** | Controls devices, queries state and history, manages automations/scripts/helpers, reads calendars and logbook, analyses sensor data, manages dashboards. |
| **ResearchAgent** | Web search for current information — device specs, HA docs, troubleshooting, news. Supports deep multi-source research. Requires Google Search API keys. |
| **VisualizationAgent** | Creates Plotly charts (line, bar, scatter, pie, heatmap, gauge, etc.) from any data. Outputs PNG or SVG artifacts. |
| **PDFReportAgent** | Generates formatted PDF documents combining text, tables, and embedded charts. Good for weekly summaries or analysis reports. |

---

## HomeAssistant Agent Permissions

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

Set `PYTHON_BIN` in your `.env` to the venv Python:

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
├── agents/
│   ├── homeassistant/       # HA REST tools MCP server
│   ├── data-analysis/       # Timeseries analysis MCP server
│   └── pdf-report/          # PDF generation MCP server
├── configs/
│   ├── agents/              # Agent YAML configs
│   └── gateways/            # Web UI and HA MQTT gateway configs
├── custom_components/
│   └── sam_workflows/       # HA custom integration for workflow discovery
├── gateways/
│   └── ha_mqtt/             # HA MQTT gateway adapter
├── shared_config.yaml       # Shared broker, LLM, and service anchors
├── docker-compose.yml
├── Dockerfile
└── requirements.txt
```
