# Leonilda Bob | Bob Advogados

Site institucional independente para Leonilda Bob e Bob Advogados.

## Escopo desta primeira versão

- Site multipágina com visual editorial inspirado em escritórios boutique como Minkowski & Partners.
- Foco principal em Direito Trabalhista para trabalhadores.
- Página do Instituto Leonilda Bob como iniciativa em constituição.
- Formulários curtos separados por intenção: trabalhista, instituto e contato geral.
- PostgreSQL no Supabase para contatos, visitas, conversas, sessões e fila de WhatsApp.
- Supabase Storage privado para fotos, áudios, vídeos e documentos do atendimento.
- Primeiro contato por WhatsApp preparado para API oficial ou QR bridge, com atendimento inicial assinado por Lucas.
- Hero editorial com recorte PNG da Leonilda em primeiro plano.
- Mapa do endereço no Google Maps.
- Painel interno em `/admin`.
- Blog público em `/blog`, com busca por texto e organização por temas.
- Editor de artigos no painel em `/admin/artigos`, com rascunho, prévia, publicação e retirada do ar.

## Rodar localmente

```bash
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
chmod 600 .env
uvicorn app.main:app --reload
```

Acesse:

- Site: `http://127.0.0.1:8000`
- Admin local: `http://127.0.0.1:8000/admin`

Sem `DATABASE_URL`, o projeto usa SQLite apenas para desenvolvimento. Com as variáveis do Supabase,
o mesmo código usa PostgreSQL e armazenamento privado.

## Publicar no Render

O arquivo `render.yaml` deixa o serviço pronto para criação no Render. As credenciais marcadas como
`sync: false` devem ser preenchidas no painel e nunca adicionadas ao GitHub.

O plano gratuito serve para validar o deploy. Antes de ativar o WhatsApp em produção, use uma instância
sempre ativa para evitar que o serviço adormeça e atrase o recebimento de mensagens.

### Checklist obrigatório de produção

- `APP_ENV=production`, `APP_BASE_URL` com HTTPS e domínio em `ALLOWED_HOSTS`.
- PostgreSQL persistente em `DATABASE_URL` e bucket privado configurado no Supabase Storage.
- `ADMIN_PASSWORD_HASH` gerado com `python scripts/hash_admin_password.py`; não use `ADMIN_PASSWORD`.
- `ADMIN_SESSION_SECRET` e `METRICS_SALT` aleatórios com pelo menos 32 caracteres.
- Credenciais oficiais, assinatura do webhook e template do WhatsApp configurados antes de desligar o dry-run.
- Backup gerenciado do PostgreSQL ativo. No SQLite local, use `python scripts/backup_sqlite.py`.
- `REQUIRE_VIRUS_SCAN=1` quando o ambiente disponibilizar o executável `clamscan`.

O processo de produção usa um worker para manter a consistência da fila e do SQLite. A fila de WhatsApp
possui retentativas com backoff, os webhooks são persistidos antes da confirmação e o endpoint `/readyz`
valida banco e armazenamento.

## WhatsApp

Por padrão, `WHATSAPP_DRY_RUN=1`, então o contato é salvo e a mensagem fica marcada como simulação.

### API oficial

Para enviar de verdade:

```env
WHATSAPP_PROVIDER="official"
WHATSAPP_ACCESS_TOKEN="..."
WHATSAPP_PHONE_NUMBER_ID="..."
WHATSAPP_APP_SECRET="..."
WHATSAPP_VERIFY_TOKEN="..."
WHATSAPP_FIRST_CONTACT_TEMPLATE="nome_do_modelo_aprovado"
WHATSAPP_TEMPLATE_LANGUAGE="pt_BR"
WHATSAPP_DRY_RUN="0"
```

O endereço para receber mensagens aparece em `/admin` e segue este formato:

```text
https://seu-dominio.com/api/webhooks/whatsapp
```

Observação: quando o contato é iniciado pelo escritório, o WhatsApp oficial normalmente exige um modelo aprovado. Por isso existe `WHATSAPP_FIRST_CONTACT_TEMPLATE`.

### QR como alternativa

Se quiser usar o QR bridge:

```env
WHATSAPP_PROVIDER="qr"
WHATSAPP_QR_BRIDGE_URL="http://127.0.0.1:3333"
WHATSAPP_DRY_RUN="0"
```

Payload esperado pelo bridge:

```json
{
  "to": "5511994926810",
  "text": "mensagem"
}
```

O bridge QR é aceito somente para texto. Arquivos exigem a integração oficial para evitar simulações de envio.

## Testes

```bash
pip-audit -r requirements.txt --progress-spinner off
node --check static/js/site.js
python -m compileall -q app scripts
python scripts/smoke_test.py
```

O smoke test usa banco e uploads temporários, cobre HTML e SEO básico, cabeçalhos, compressão, login com
CSRF, publicação segura de artigos, idempotência de leads e webhooks, validação de arquivos, mídia privada,
controles do chat, exclusão de dados e revogação da sessão, e remove os artefatos ao final.

## Publicar artigos

1. Entre em `/admin` e escolha **Meus artigos**.
2. Use **Escrever novo artigo**.
3. Preencha título, pequena apresentação, texto e tema.
4. Escolha **Guardar para terminar depois** ou **Publicar no site**.

Artigos guardados não aparecem no site. Um artigo publicado pode ser editado ou retirado do ar sem ser
apagado, preservando o texto para uma publicação futura.

## Privacidade e retenção

- Métricas só começam após autorização do visitante e expiram conforme `ANALYTICS_RETENTION_DAYS`.
- Casos encerrados ou arquivados podem ser removidos após `LEAD_RETENTION_DAYS`.
- Arquivos ficam em diretório privado ou bucket privado e só são entregues por rota autenticada.
- Solicitações verificadas de exclusão podem ser executadas na seção Privacidade do painel.

## Observações de conteúdo

- O site usa linguagem informativa e sóbria, sem prometer resultados.
- O Instituto aparece como “em constituição” até o registro estar finalizado.
- O recorte público da Leonilda usa AVIF com fallback PNG; o arquivo-fonte fica fora da pasta pública em `design-source/`.
