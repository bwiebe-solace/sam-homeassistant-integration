# Development Guidelines

## Git Workflow

- **Do not commit directly to `main`** unless explicitly instructed to do so.
- All work should be done on a feature branch (e.g. `feat/voice-pipeline`, `fix/ha-auth`).
- Merging to `main` triggers a Docker image build and push to GHCR — only do this when a change is ready to deploy.

## Assistant Name

The assistant is currently named **SAM**. If renaming, update the following:

| File | What to change |
|---|---|
| `configs/gateways/webui.yaml` | `frontend_bot_name`, `frontend_welcome_message`, `system_purpose` |
| `configs/agents/*.yaml` | Any agent `instruction` fields that reference the assistant by name |
| `.env` | `NAMESPACE` value (controls Solace topic prefix — keep user-specific, e.g. `yourname-sam-ha`) |
| `docker-compose.yml` | Volume name `sam-artifacts` and mount path |
| `shared_config.yaml` | Default value in `artifact_service.base_path` |

Note: `NAMESPACE` also affects Solace topic routing — if you change it after running the system,
existing queues on the broker under the old namespace will be orphaned and should be deleted
from the Solace Cloud console.
