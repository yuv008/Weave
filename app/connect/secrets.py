"""Encrypted connector credentials. The plaintext token is not stored or logged."""

import base64
import hashlib
import json
import os
from datetime import UTC, datetime

PROVIDERS = ("slack", "notion", "google", "github", "groq", "profile")


def secret_key() -> str:
    key = os.environ.get("WEAVE_SECRET_KEY", "").strip()
    if not key:
        raise RuntimeError("WEAVE_SECRET_KEY is required")
    return key


def _fernet(key: str):
    from cryptography.fernet import Fernet

    derived = base64.urlsafe_b64encode(hashlib.sha256(key.encode("utf-8")).digest())
    return Fernet(derived)


def encrypt(payload: dict, key: str | None = None) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return _fernet(key or secret_key()).encrypt(raw).decode("ascii")


def decrypt(blob: str, key: str | None = None) -> dict:
    raw = _fernet(key or secret_key()).decrypt(blob.encode("ascii"))
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("credential payload is invalid")
    return payload


class MemorySecretStore:
    """In-process store for tests. Rows still hold ciphertext."""

    def __init__(self):
        self.rows: dict[tuple[str, str], dict] = {}

    def put(self, tenant: str, provider: str, external_id: str, payload: dict, *, scopes: str = "", status: str = "connected") -> None:
        self.rows[(tenant, provider)] = {
            "external_id": external_id or "",
            "secret_encrypted": encrypt(payload),
            "scopes": scopes or "",
            "status": status or "connected",
            "updated_at": datetime.now(UTC),
        }

    def update_status(self, tenant: str, provider: str, status: str) -> None:
        row = self.rows.get((tenant, provider))
        if row is not None:
            row["status"] = status
            row["updated_at"] = datetime.now(UTC)

    def get(self, tenant: str, provider: str) -> dict | None:
        row = self.rows.get((tenant, provider))
        if row is None:
            return None
        payload = decrypt(row["secret_encrypted"])
        return {
            **payload,
            "external_id": row["external_id"],
            "scopes": row["scopes"],
            "status": row["status"],
        }


class PostgresSecretStore:
    """Owner connection. A missing table or a database error yields no row."""

    def __init__(self, connect=None):
        self._connect = connect

    def _conn(self):
        if self._connect is not None:
            return self._connect()
        from shared.pg import connect_pg

        return connect_pg()

    def put(self, tenant: str, provider: str, external_id: str, payload: dict, *, scopes: str = "", status: str = "connected") -> None:
        blob = encrypt(payload)
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO connection_secret (tenant_id, provider, external_id, secret_encrypted, scopes, status)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, provider) DO UPDATE SET
                      external_id = EXCLUDED.external_id,
                      secret_encrypted = EXCLUDED.secret_encrypted,
                      scopes = EXCLUDED.scopes,
                      status = EXCLUDED.status,
                      updated_at = now()
                    """,
                    (tenant, provider, external_id or "", blob, scopes or "", status or "connected"),
                )
            conn.commit()
        finally:
            conn.close()

    def update_status(self, tenant: str, provider: str, status: str) -> None:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE connection_secret SET status = %s, updated_at = now() WHERE tenant_id = %s AND provider = %s",
                    (status, tenant, provider),
                )
            conn.commit()
        finally:
            conn.close()

    def get(self, tenant: str, provider: str) -> dict | None:
        try:
            conn = self._conn()
        except Exception:
            return None
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT external_id, secret_encrypted, scopes, status
                    FROM connection_secret WHERE tenant_id = %s AND provider = %s
                    """,
                    (tenant, provider),
                )
                row = cur.fetchone()
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            return None
        finally:
            conn.close()
        if row is None:
            return None
        try:
            payload = decrypt(row[1])
        except Exception:
            return None
        return {**payload, "external_id": row[0] or "", "scopes": row[2] or "", "status": row[3] or ""}
