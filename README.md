# py-brcompany-api

High-performance async REST API providing access to Brazilian Receita Federal CNPJ registry data.

## Features

- **Fast & Asynchronous**: Built with FastAPI, Pydantic v2, and psycopg3 connection pooling (`psycopg_pool.AsyncConnectionPool`).
- **Standard Port 8040**: Configured to run on port 8040 by default, avoiding common port collisions.
- **Comprehensive Company Profiles**: Consolidated details for companies, branches, economic activities (CNAEs), tax regimes (Simples Nacional / MEI), and corporate ownership (QSA).
- **Blue/Green & Zero-Downtime Aware**: Built to work seamlessly with the PostgreSQL publication schemas maintained by `py-receita-federal-cnpj`.
- **Domain Metadata**: Fast lookup for CNAEs, municipalities, legal natures, and status codes.
- **Production-Ready CI/CD & Playbook**: Complete Ansible playbook, systemd unit, and GitHub Actions workflows for automated continuous deployment.

## Quick Start

### 1. Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 2. Environment Configuration

Copy `.env.example` to `.env` and set your database connection:

```bash
cp .env.example .env
```

### 3. Run development server

```bash
uvicorn brcompany_api.main:app --reload --port 8040
```
or via entrypoint script:
```bash
brcompany-api
```

Access API documentation at:
- Swagger UI: `http://localhost:8040/docs`
- ReDoc: `http://localhost:8040/redoc`
- Health check: `http://localhost:8040/health`

### 4. Running Tests

```bash
pytest -v
```

---

## Production Deployment (Ubuntu 26.04)

O projeto inclui automação completa para provisionamento e deploy em produção:
- **Playbook Ansible**: `deploy/playbook.yml`
- **Unit do Systemd**: `deploy/py-brcompany-api.service` (executa na porta 8040 com usuário `usr_brcompany`)
- **Docker Compose**: `docker-compose.yml` (mapeamento `8040:8040`)
- **Workflows GitHub Actions**: `.github/workflows/` (`ci.yml`, `deploy.yml`, `provision.yml`)

Consulte o manual operacional completo em [deploy/PLAYBOOK.md](deploy/PLAYBOOK.md).
