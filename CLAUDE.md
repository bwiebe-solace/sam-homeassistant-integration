# Development Guidelines

## Git Workflow

- **Do not commit directly to `main`** unless explicitly instructed to do so.
- All work should be done on a feature branch (e.g. `feat/voice-pipeline`, `fix/ha-auth`).
- Merging to `main` triggers a Docker image build and push to GHCR — only do this when a change is ready to deploy.
