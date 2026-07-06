from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["LEONILDA_DB_PATH"] = "tmp/smoke.sqlite"
os.environ["PRIVATE_UPLOAD_DIR"] = "tmp/uploads/whatsapp"
os.environ["DATABASE_URL"] = ""
os.environ["SUPABASE_URL"] = ""
os.environ["SUPABASE_SECRET_KEY"] = ""
os.environ["WHATSAPP_DRY_RUN"] = "1"
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "smoke-password"
os.environ["ADMIN_PASSWORD_HASH"] = ""
os.environ["ADMIN_SESSION_SECRET"] = "smoke-session-secret"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def main() -> None:
    smoke_db = Path("tmp/smoke.sqlite")
    if smoke_db.exists():
        smoke_db.unlink()

    with TestClient(app) as client:
        for path in ["/", "/sobre", "/atuacao", "/instituto", "/contato"]:
            response = client.get(path)
            assert response.status_code == 200, f"{path} returned {response.status_code}"
            if path == "/sobre":
                assert "Ladislau Bob" in response.text
                assert "282.631" in response.text

        admin_redirect = client.get("/admin", follow_redirects=False)
        assert admin_redirect.status_code == 303, admin_redirect.text

        login = client.post(
            "/admin/login",
            data={"username": "admin", "password": "smoke-password", "next_path": "/admin"},
            follow_redirects=False,
        )
        assert login.status_code == 303, login.text

        admin_response = client.get("/admin")
        assert admin_response.status_code == 200, admin_response.text

        lead_response = client.post(
            "/api/leads",
            json={
                "kind": "trabalhista",
                "name": "Lead Teste",
                "phone": "(11) 99999-9999",
                "email": "teste@example.com",
                "message": "Teste de triagem trabalhista.",
                "consent": True,
                "source_path": "/contato",
            },
        )
        assert lead_response.status_code == 200, lead_response.text
        payload = lead_response.json()
        assert payload["ok"] is True
        assert payload["whatsapp"] == "dry_run"

        metrics_response = client.get("/api/admin/metrics")
        assert metrics_response.status_code == 200, metrics_response.text
        metrics = metrics_response.json()
        assert metrics["totals"]["leads_total"] == 1

        conversations_response = client.get("/api/admin/conversations")
        assert conversations_response.status_code == 200, conversations_response.text
        conversations = conversations_response.json()["conversations"]
        assert len(conversations) == 1

        send_response = client.post(
            f"/api/admin/conversations/{conversations[0]['id']}/messages",
            data={"text": "Mensagem de teste"},
        )
        assert send_response.status_code == 200, send_response.text

        file_response = client.post(
            f"/api/admin/conversations/{conversations[0]['id']}/messages",
            data={"text": "Arquivo de teste"},
            files={"attachment": ("teste.txt", b"conteudo", "text/plain")},
        )
        assert file_response.status_code == 200, file_response.text

        messages_response = client.get(
            f"/api/admin/conversations/{conversations[0]['id']}/messages"
        )
        messages = messages_response.json()["messages"]
        media_url = next(message["media_url"] for message in messages if message.get("media_url"))
        media_response = client.get(media_url)
        assert media_response.status_code == 200, media_response.text
        assert media_response.content == b"conteudo"

    print("smoke ok")


if __name__ == "__main__":
    main()
