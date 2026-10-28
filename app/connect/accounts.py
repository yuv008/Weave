"""Email accounts, company membership, and browser sessions.

A company is a tenant id. The live viewer mailbox claims tenant ``real``.
Every other signup gets a new slug. ``acme`` is never a tenant.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
from datetime import UTC, datetime, timedelta

log = logging.getLogger("weave.accounts")
SKIP_GRAPH = False

SESSION_DAYS = 14
MAX_FAILURES = 8
WINDOW = timedelta(minutes=15)
_RESERVED = {"acme", "real"}


class AccountError(Exception):
    def __init__(self, code: str):
        self.code = code


def hash_password(password: str) -> str:
    from argon2 import PasswordHasher

    return PasswordHasher().hash(password)


def verify_password(password: str, hashed: str) -> bool:
    from argon2 import PasswordHasher
    from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

    try:
        return PasswordHasher().verify(hashed, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def hash_session(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def clean_email(value: str) -> str:
    email = value.strip().lower()
    if len(email) > 200 or " " in email or email.count("@") != 1:
        raise AccountError("email")
    local, domain = email.split("@", 1)
    if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
        raise AccountError("email")
    return email


def clean_name(value: str) -> str:
    name = " ".join(value.split())
    if not name or len(name) > 80:
        raise AccountError("name")
    return name


def company_slug(value: str) -> str:
    from shared.ids import slug

    try:
        result = slug(value)
    except ValueError as exc:
        raise AccountError("company") from exc
    if len(result) < 2 or len(result) > 40:
        raise AccountError("company")
    if result in _RESERVED:
        raise AccountError("reserved")
    return result


def check_password(password: str) -> str:
    if len(password) < 10 or len(password) > 200:
        raise AccountError("password")
    return password


def viewer_email() -> str:
    return os.environ.get("WEAVE_VIEWER_EMAIL", "").strip().lower()


def choose_tenant(email: str, company: str) -> str:
    if viewer_email() and email == viewer_email():
        return "real"
    return company_slug(company)


def remember_owner(tenant: str, email: str, name: str) -> None:
    """Write the owner Person on ``co:{tenant}``. A missing graph does not undo signup."""

    if SKIP_GRAPH or not tenant or tenant == "acme":
        return
    try:
        from app.graph.client import connect_graph
        from shared.ids import person_id
        from shared.schema import Label, Prop

        graph = connect_graph(tenant=tenant)
        graph.query(
            f"""
            MERGE (p:{Label.PERSON} {{id: $person_id}})
            SET p.{Prop.TENANT_ID} = $tenant_id,
                p.{Prop.EMAIL} = $email,
                p.{Prop.NAME} = $name,
                p.{Prop.LEVEL} = coalesce(p.{Prop.LEVEL}, 0),
                p.{Prop.PROVISIONAL} = true
            """,
            {
                "person_id": person_id(tenant, email),
                "tenant_id": tenant,
                "email": email,
                "name": name,
            },
        )
    except Exception as exc:
        log.warning("owner person was not written (%s)", type(exc).__name__)


class MemoryAccountStore:
    def __init__(self):
        self.users: dict[str, dict] = {}
        self.by_email: dict[str, str] = {}
        self.memberships: dict[str, list[tuple[str, str]]] = {}
        self.tenants: set[str] = set()
        self.sessions: dict[str, dict] = {}
        self.attempts: dict[str, tuple[int, datetime]] = {}

    def signup(self, *, email: str, password: str, name: str, company: str) -> tuple[str, dict]:
        cleaned = clean_email(email)
        display = clean_name(name)
        secret = check_password(password)
        tenant = choose_tenant(cleaned, company)
        if cleaned in self.by_email:
            raise AccountError("registered")
        if tenant != "real" and tenant in self.tenants:
            raise AccountError("taken")
        if tenant == "real" and any(item[0] == "real" for rows in self.memberships.values() for item in rows):
            raise AccountError("registered")
        user_id = "user_" + secrets.token_urlsafe(9)
        self.users[user_id] = {
            "email": cleaned,
            "name": display,
            "password_hash": hash_password(secret),
        }
        self.by_email[cleaned] = user_id
        self.memberships[user_id] = [(tenant, "owner")]
        self.tenants.add(tenant)
        return self._open(user_id)

    def login(self, email: str, password: str) -> tuple[str, dict] | None:
        try:
            cleaned = clean_email(email)
        except AccountError:
            return None
        if self._blocked(cleaned):
            return None
        user_id = self.by_email.get(cleaned)
        user = self.users.get(user_id or "")
        if user is None or not verify_password(password, user["password_hash"]):
            self._fail(cleaned)
            return None
        self.attempts.pop(cleaned, None)
        return self._open(user_id)

    def read_session(self, token: str) -> dict | None:
        row = self.sessions.get(hash_session(token))
        if row is None or row["expires_at"] <= datetime.now(UTC):
            return None
        return self._public(row["user_id"], row["tenant_id"])

    def switch(self, token: str, tenant: str) -> dict | None:
        digest = hash_session(token)
        row = self.sessions.get(digest)
        if row is None:
            return None
        allowed = {item[0] for item in self.memberships.get(row["user_id"], [])}
        if tenant not in allowed:
            return None
        row["tenant_id"] = tenant
        return self._public(row["user_id"], tenant)

    def _open(self, user_id: str) -> tuple[str, dict]:
        raw = secrets.token_urlsafe(32)
        memberships = self.memberships[user_id]
        self.sessions[hash_session(raw)] = {
            "user_id": user_id,
            "tenant_id": memberships[0][0],
            "expires_at": datetime.now(UTC) + timedelta(days=SESSION_DAYS),
        }
        return raw, self._public(user_id, memberships[0][0])

    def _public(self, user_id: str, tenant: str) -> dict:
        user = self.users[user_id]
        return {
            "user_id": user_id,
            "email": user["email"],
            "name": user["name"],
            "tenant_id": tenant,
            "memberships": [
                {"tenant": item[0], "role": item[1]} for item in self.memberships.get(user_id, [])
            ],
        }

    def _blocked(self, email: str) -> bool:
        row = self.attempts.get(email)
        if row is None:
            return False
        failures, started = row
        if datetime.now(UTC) - started > WINDOW:
            self.attempts.pop(email, None)
            return False
        return failures >= MAX_FAILURES

    def _fail(self, email: str) -> None:
        now = datetime.now(UTC)
        failures, started = self.attempts.get(email, (0, now))
        if now - started > WINDOW:
            failures, started = 0, now
        self.attempts[email] = (failures + 1, started)


class PostgresAccountStore:
    def signup(self, *, email: str, password: str, name: str, company: str) -> tuple[str, dict]:
        cleaned = clean_email(email)
        display = clean_name(name)
        secret = check_password(password)
        tenant = choose_tenant(cleaned, company)
        user_id = "user_" + secrets.token_urlsafe(9)
        raw = secrets.token_urlsafe(32)
        expires = datetime.now(UTC) + timedelta(days=SESSION_DAYS)
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM app_user WHERE email = %s", (cleaned,))
                if cur.fetchone():
                    raise AccountError("registered")
                if tenant == "real":
                    cur.execute("SELECT 1 FROM tenant_membership WHERE tenant_id = %s", ("real",))
                    if cur.fetchone():
                        raise AccountError("registered")
                else:
                    cur.execute("SELECT 1 FROM tenant_membership WHERE tenant_id = %s", (tenant,))
                    if cur.fetchone():
                        raise AccountError("taken")
                cur.execute(
                    "INSERT INTO app_user (id, email, name, password_hash) VALUES (%s, %s, %s, %s)",
                    (user_id, cleaned, display, hash_password(secret)),
                )
                cur.execute(
                    "INSERT INTO tenant_membership (user_id, tenant_id, role) VALUES (%s, %s, %s)",
                    (user_id, tenant, "owner"),
                )
                cur.execute(
                    "INSERT INTO app_session (token_sha256, user_id, tenant_id, expires_at) VALUES (%s, %s, %s, %s)",
                    (hash_session(raw), user_id, tenant, expires),
                )
            conn.commit()
        except AccountError:
            conn.rollback()
            raise
        except Exception as exc:
            conn.rollback()
            if (getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)) != "23505":
                raise
            # Two signups passed the checks above at once. The unique indexes pick one.
            constraint = getattr(getattr(exc, "diag", None), "constraint_name", None) or ""
            raise AccountError("registered" if "email" in constraint or tenant == "real" else "taken") from exc
        finally:
            conn.close()
        return raw, _row(user_id, cleaned, display, tenant, [{"tenant": tenant, "role": "owner"}])

    def login(self, email: str, password: str) -> tuple[str, dict] | None:
        try:
            cleaned = clean_email(email)
        except AccountError:
            return None
        conn = _connect()
        try:
            with conn.cursor() as cur:
                if _postgres_blocked(cur, cleaned):
                    conn.commit()
                    return None
                cur.execute("SELECT id, name, password_hash FROM app_user WHERE email = %s", (cleaned,))
                found = cur.fetchone()
                if found is None or not verify_password(password, found[2]):
                    _postgres_fail(cur, cleaned)
                    conn.commit()
                    return None
                cur.execute("DELETE FROM login_attempt WHERE email = %s", (cleaned,))
                cur.execute(
                    "SELECT tenant_id, role FROM tenant_membership WHERE user_id = %s ORDER BY tenant_id",
                    (found[0],),
                )
                memberships = [{"tenant": row[0], "role": row[1]} for row in cur.fetchall()]
                if not memberships:
                    conn.commit()
                    return None
                raw = secrets.token_urlsafe(32)
                cur.execute(
                    "INSERT INTO app_session (token_sha256, user_id, tenant_id, expires_at) VALUES (%s, %s, %s, %s)",
                    (hash_session(raw), found[0], memberships[0]["tenant"], datetime.now(UTC) + timedelta(days=SESSION_DAYS)),
                )
            conn.commit()
        finally:
            conn.close()
        return raw, _row(found[0], cleaned, found[1], memberships[0]["tenant"], memberships)

    def read_session(self, token: str) -> dict | None:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT s.user_id, s.tenant_id, u.email, u.name
                    FROM app_session AS s
                    JOIN app_user AS u ON u.id = s.user_id
                    WHERE s.token_sha256 = %s AND s.expires_at > now()
                    """,
                    (hash_session(token),),
                )
                found = cur.fetchone()
                if found is None:
                    return None
                cur.execute(
                    "SELECT tenant_id, role FROM tenant_membership WHERE user_id = %s ORDER BY tenant_id",
                    (found[0],),
                )
                memberships = [{"tenant": row[0], "role": row[1]} for row in cur.fetchall()]
            conn.commit()
        finally:
            conn.close()
        return _row(found[0], found[2], found[3], found[1], memberships)

    def switch(self, token: str, tenant: str) -> dict | None:
        current = self.read_session(token)
        if current is None or tenant not in {item["tenant"] for item in current["memberships"]}:
            return None
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE app_session SET tenant_id = %s WHERE token_sha256 = %s",
                    (tenant, hash_session(token)),
                )
            conn.commit()
        finally:
            conn.close()
        current["tenant_id"] = tenant
        return current


def _row(user_id: str, email: str, name: str, tenant: str, memberships: list[dict]) -> dict:
    return {
        "user_id": user_id,
        "email": email,
        "name": name,
        "tenant_id": tenant,
        "memberships": memberships,
    }


def _connect():
    from shared.pg import connect_pg

    return connect_pg()


def _postgres_blocked(cur, email: str) -> bool:
    cur.execute("SELECT failures, window_started FROM login_attempt WHERE email = %s", (email,))
    row = cur.fetchone()
    if row is None:
        return False
    started = row[1]
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    if datetime.now(UTC) - started > WINDOW:
        cur.execute("DELETE FROM login_attempt WHERE email = %s", (email,))
        return False
    return row[0] >= MAX_FAILURES


def _postgres_fail(cur, email: str) -> None:
    cur.execute(
        """
        INSERT INTO login_attempt (email, failures, window_started) VALUES (%s, 1, now())
        ON CONFLICT (email) DO UPDATE SET
          failures = CASE WHEN now() - login_attempt.window_started > interval '15 minutes' THEN 1 ELSE login_attempt.failures + 1 END,
          window_started = CASE WHEN now() - login_attempt.window_started > interval '15 minutes' THEN now() ELSE login_attempt.window_started END
        """,
        (email,),
    )
