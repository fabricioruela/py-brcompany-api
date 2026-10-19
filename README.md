# py-brcompany-api

High-performance async REST API providing access to Brazilian Receita Federal CNPJ registry data.

## Features

- **Fast & Asynchronous**: Built with FastAPI, Pydantic v2, and psycopg3 connection pooling (`psycopg_pool.AsyncConnectionPool`).
- **Comprehensive Company Profiles**: Consolidated details for companies, branches, economic activities (CNAEs), tax regimes (Simples Nacional / MEI), and corporate ownership (QSA).
- **Blue/Green & Zero-Downtime Aware**: Built to work seamlessly with the PostgreSQL publication schemas maintained by `py-receita-federal-cnpj`.
- **Domain Metadata**: Fast lookup for CNAEs, municipalities, legal natures, and status codes.

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
uvicorn brcompany_api.main:app --reload --port 8000
```

Access API documentation at:
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`
