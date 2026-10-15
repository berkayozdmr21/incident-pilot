# Verification · 2026-10-15

- React production bundle built successfully.
- HTTP frontend serving and WebSocket handshake/JSON payload verified through FastAPI TestClient.
- Six React/Vitest interaction tests passed in jsdom with isolated API responses.
- Sixteen Python tests passed: real ASGI service dependencies and trace propagation, error/latency faults, durable jobs, explicit approval, replay/stale-revision rejection, rollback, metric-based recovery, service token/Origin validation, report/Prometheus endpoints, and optional LLM reference checks/fallback.
- Tests use ASGI transport, not separate network processes; recovery tests advance old sample timestamps instead of waiting the full window.
- Isolation Forest and rules evaluated on 200 separately generated synthetic windows. This is a constructed sanity check, not a production benchmark.
- Docker Compose schema validated with `docker compose config --quiet`.
- Docker executable was available but its Linux engine was not running. Container build, PostgreSQL runtime, Prometheus scraping and Jaeger export were not exercised here.
- Windows launcher chooses four free ports, generates a fresh control token, starts separate processes, waits for all health endpoints, and cleans up owned processes on exit. Its browser startup in the user's normal session has not yet been verified.
- Real paid OpenAI API calls were not made. Optional narration was verified with mock responses; core functionality uses no API key.
- Localhost browser access from sandbox-started processes has previously failed in this workspace, so no visual browser QA is claimed for this deliverable.
