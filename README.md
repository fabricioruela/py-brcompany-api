# py-brcompany-api (PanoramaPJ SaaS & DaaS Backend)

API assíncrona de alta performance construída com **FastAPI, Pydantic v2 e psycopg3**, servindo a plataforma **PanoramaPJ** de Inteligência Comercial B2B e Enriquecimento de Dados Cadastrais sobre a base oficial da Receita Federal (`py-receita-federal-cnpj`).

## Arquitetura & Principais Módulos

- **Schema Transacional Isolado (`app_saas`)**:
  - Inicializado automaticamente no startup (`src/brcompany_api/sql/001_app_saas.sql`), mantendo tabelas de usuários, carteira FIFO (`credit_lots`), razão contábil imutável (`credit_transactions`), histórico de CNPJs exportados (`user_exported_cnpjs`), listas salvas por 30 dias (`exported_lists`), pedidos PIX/Asaas (`orders`), chaves DaaS (`api_keys`), radares (`saved_radars`), opt-outs LGPD (`lgpd_opt_outs`) e configurações globais (`platform_settings`) 100% imunes ao swap `blue_green` do schema `registry`.
- **Motor de Busca B2B (`/api/v1/search/count` e `/api/v1/search/preview`)**:
  - Consulta a tabela desnormalizada `registry.company_search_index` com suporte a cascata geográfica (`UF -> Município -> Bairro` via `registry.location_hierarchy`), CNAE principal e secundário (GIN), porte, natureza jurídica, capital social, Simples/MEI e filtros de entregabilidade (`only_with_phone`, `only_with_email`).
- **Motor de Exportação & Armazenamento 30 Dias (`/api/v1/export/*`)**:
  - Exportação síncrona de até **5.000 empresas por arquivo** em `.csv` (UTF-8 com BOM) ou `.xlsx` (Excel nativo), incluindo sócios concatenados (`partners_summary`).
  - **Desduplicação Global Controlada pelo Administrador**: Configurável em tempo real por usuários com perfil `admin` em `PUT /api/v1/admin/settings` (`export_deduplication_enabled`).
- **Carteira FIFO, PIX Instantâneo (SSE) & Webhook Asaas (`/api/v1/checkout/*`, `/api/v1/wallet`, `/api/v1/webhooks/asaas`)**:
  - Suporte aos pacotes avulsos (`starter`, `pro`, `scale` — sem expiração) e à `smart_monthly` (assinatura mensal com rollover de 90 dias).
  - Consumo FIFO por data de expiração mais próxima e confirmação em tempo real via Server-Sent Events (`/api/v1/checkout/sse/{order_id}`).
- **API Comercial DaaS (`X-API-Key`) & Conformidade LGPD (`/api/v1/privacy/opt-out`)**:
  - Mascaramento dinâmico na ficha pública `/api/v1/companies/{cnpj}`, desbloqueio unitário por 1 crédito (`?unlock=true` ou `X-API-Key`), proteção de dados pessoais de MEI e remoção imediata por Opt-Out LGPD com suporte a purge de cache Cloudflare.

## Executando Testes

```bash
.venv/bin/python -m pytest -v -m "not integration"
```
