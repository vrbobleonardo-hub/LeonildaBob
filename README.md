# Leonilda Bob | Bob Advogados

Site institucional independente para Leonilda Bob, Ladislau Bob e Bob Advogados.

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

## Rodar localmente

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
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

## WhatsApp

Por padrão, `WHATSAPP_DRY_RUN=1`, então o contato é salvo e a mensagem fica marcada como simulação.

### API oficial

Para enviar de verdade:

```env
WHATSAPP_PROVIDER="official"
WHATSAPP_ACCESS_TOKEN="..."
WHATSAPP_PHONE_NUMBER_ID="..."
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

## Observações de conteúdo

- O site usa linguagem informativa e sóbria, sem prometer resultados.
- O Instituto aparece como “em constituição” até o registro estar finalizado.
- O recorte atual da Leonilda foi gerado a partir da referência enviada na conversa e salvo como `static/assets/leonilda-cutout.png`.
