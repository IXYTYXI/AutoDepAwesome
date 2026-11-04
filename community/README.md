# OWS Community

This directory contains the OWS Community application source copied from the production source tree on 2026-11-04. It includes the FastAPI service, Vue frontend, deployment integration, the bundled Awesome Deploy engine, tests, and deployment examples.

The application builds the frontend from `frontend/src` and installs the local engine from `packages/deploy-engine`. See `Dockerfile` and `docs/DEPLOYMENT.md` for the build and deployment entry points.

Runtime credentials, databases, caches, generated frontend assets, and machine-specific operational records are intentionally absent. Configure the service using the example environment and engine files under this directory; never commit populated credential files.
