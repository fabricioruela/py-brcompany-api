# Playbook de Publicação da API em Produção — Ubuntu 26.04

Este documento contém o guia completo e automatizado para o provisionamento e publicação da aplicação `py-brcompany-api` (FastAPI assíncrona) em um servidor com **Ubuntu 26.04 LTS**.

O playbook atende aos seguintes requisitos:
1. **Porta padrão de produção na porta 8040** (configurável via variável de ambiente `APP_PORT`).
2. **Execução dedicada via Linux (`usr_brcompany`)** sem privilégios de superusuário.
3. **Gerenciamento de processo via systemd** com reinício automático (`py-brcompany-api.service`).
4. **Isolamento de credenciais fora do repositório Git** (`/etc/py-brcompany-api/.env` com permissão restrita `0600`).
5. **Integração total com o banco PostgreSQL e schema `registry`** populado pelo pipeline `py-receita-federal-cnpj`.
6. **Automação via GitHub Actions** para CI (testes) e CD (deploy via SSH).

---

## Estrutura dos Arquivos de Deploy

Os artefatos de infraestrutura estão organizados no diretório `deploy/`:

```
deploy/
├── playbook.yml             # Playbook automatizado em Ansible
├── inventory.ini.example    # Modelo de inventário de hosts para Ansible
├── vars.yml.example         # Modelo de variáveis de configuração
├── py-brcompany-api.service # Unit de serviço do systemd (porta 8040)
├── nginx.conf               # Configuração de Reverse Proxy Nginx para a API
└── PLAYBOOK.md              # Este manual operacional
```

---

## Método 1: Execução Automatizada via Ansible (Recomendado)

### 1. Instalar o Ansible na máquina de controle (se ainda não tiver)

```bash
pip install ansible
```

### 2. Configurar o Inventário e as Variáveis

```bash
cd deploy/
cp inventory.ini.example inventory.ini
cp vars.yml.example vars.yml
```

Edite o arquivo `inventory.ini` informando o IP ou hostname do servidor de produção:

```ini
[app_servers]
py-brcompany-api ansible_host=SEU_IP_OU_HOST ansible_user=ubuntu ansible_ssh_private_key_file=~/.ssh/id_rsa
```

Edite o arquivo `vars.yml` ajustando os parâmetros:
- `db_host`, `db_port` (5432)
- `db_name` (`br_company`), `db_user` (`app_br_company`)
- `app_port`: `8040`

> **Importante (Segurança)**: O arquivo `deploy/vars.yml` está no `.gitignore`. Para fornecer a senha do banco (`db_password`):
> 1. **Prompt Interativo**: Deixe `db_password` vazia em `vars.yml` — o Ansible solicitará de forma segura e mascarada no terminal.
> 2. **Via Linha de Comando**:
>    ```bash
>    ansible-playbook -i inventory.ini playbook.yml -e "db_password=SUA_SENHA_FORTE"
>    ```

### 3. Executar o Playbook

```bash
ansible-playbook -i inventory.ini playbook.yml
```

---

## Método 2: Execução Manual Passo a Passo no Servidor

Caso prefira executar diretamente no terminal do servidor Ubuntu 26.04 com privilégios `sudo`:

### Passo 1: Instalação de Pacotes Base

```bash
sudo apt update && sudo apt install -y ca-certificates curl git build-essential \
                    python3 python3-venv python3-pip libpq-dev postgresql-client
```

### Passo 2: Criação do Usuário Dedicado

```bash
sudo groupadd --system usr_brcompany || true
sudo useradd --system -g usr_brcompany -d /home/usr_brcompany -m -s /bin/bash usr_brcompany || true
```

### Passo 3: Clonagem do Repositório e Ambiente Virtual

```bash
sudo mkdir -p /opt/py-brcompany-api
sudo chown -R usr_brcompany:usr_brcompany /opt/py-brcompany-api

sudo -u usr_brcompany git clone https://github.com/fabricioruela/py-brcompany-api.git /opt/py-brcompany-api
cd /opt/py-brcompany-api

sudo -u usr_brcompany python3 -m venv .venv
sudo -u usr_brcompany .venv/bin/pip install --upgrade pip setuptools wheel
sudo -u usr_brcompany .venv/bin/pip install -e .
```

### Passo 4: Configuração Segura de Ambiente (`.env`)

Crie o diretório `/etc/py-brcompany-api`:

```bash
sudo mkdir -p /etc/py-brcompany-api
sudo chown usr_brcompany:usr_brcompany /etc/py-brcompany-api
sudo chmod 0750 /etc/py-brcompany-api
```

Crie o arquivo `/etc/py-brcompany-api/.env` com permissão restrita `0600`:

```bash
sudo bash -c 'cat << "EOF" > /etc/py-brcompany-api/.env
APP_NAME=py-brcompany-api
APP_ENV=production
APP_HOST=0.0.0.0
APP_PORT=8040
APP_LOG_LEVEL=INFO
APP_DATABASE_URL=postgresql://app_br_company:SENHA_AQUI@localhost:5432/br_company
APP_DB_SCHEMA=registry
APP_DB_POOL_MIN_SIZE=4
APP_DB_POOL_MAX_SIZE=20
APP_DB_POOL_TIMEOUT=30.0
APP_CORS_ORIGINS=["http://localhost:3000","http://localhost:5173","http://localhost"]
EOF'

sudo chown usr_brcompany:usr_brcompany /etc/py-brcompany-api/.env
sudo chmod 0600 /etc/py-brcompany-api/.env
```

### Passo 5: Instalação e Ativação do Serviço Systemd

Copie o arquivo de serviço:

```bash
sudo cp /opt/py-brcompany-api/deploy/py-brcompany-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable py-brcompany-api
sudo systemctl restart py-brcompany-api
```

Valide se o serviço está rodando e saudável:

```bash
sudo systemctl status py-brcompany-api
curl -f http://localhost:8040/health
```

---

## Método 3: Execução via Docker / Docker Compose

Caso prefira rodar via container:

```bash
cd /opt/py-brcompany-api
docker compose up -d --build
```

O container mapeia a porta `8040:8040` e executa healthcheck automático em `http://localhost:8040/health`.

---

## Integração Contínua e Deploy Contínuo (GitHub Actions)

O repositório possui 3 workflows em `.github/workflows/`:
1. `ci.yml`: Executa a suíte de testes unitários (`pytest`) a cada push ou pull request.
2. `deploy.yml`: Disparado automaticamente em commits na branch `main`. Executa os testes e, se aprovados, conecta via SSH no servidor de produção, atualiza o código, sincroniza dependências, reinicia o serviço e verifica a saúde da API na porta 8040.
3. `provision.yml`: Disparo manual (`workflow_dispatch`) que executa o playbook Ansible diretamente pelo GitHub Actions.

### Secrets Necessários no GitHub:
- `SSH_HOST`: Endereço IP ou hostname do servidor.
- `SSH_USER`: Usuário SSH com permissão sudo (ex: `ubuntu` ou `usr_brcompany`).
- `SSH_PRIVATE_KEY`: Chave privada SSH correspondente.
- `SSH_PORT`: Porta SSH (padrão `22`).
- `DB_PASSWORD`: Senha do usuário de banco `app_br_company`.
