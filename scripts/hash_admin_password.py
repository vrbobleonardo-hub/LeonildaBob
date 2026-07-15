from __future__ import annotations

import getpass
import hashlib
import secrets


def main() -> None:
    password = getpass.getpass("Nova senha administrativa: ")
    confirmation = getpass.getpass("Repita a senha: ")
    if password != confirmation:
        raise SystemExit("As senhas não coincidem.")
    if len(password) < 12:
        raise SystemExit("Use ao menos 12 caracteres.")
    iterations = 600_000
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations
    ).hex()
    print(f"pbkdf2_sha256${iterations}${salt}${digest}")


if __name__ == "__main__":
    main()
