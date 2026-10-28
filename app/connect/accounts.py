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
VERIFY_FOR = timedelta(hours=24)
RESET_FOR = timedelta(minutes=30)
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
    def __init__(self, *, auto_verify: bool = True):
        # Tests and local runs treat a new address as verified; the Postgres store does not.
        self.auto_verify = auto_verify
        self.verified: set[str] = set()
        self.email_tokens: dict[str, dict] = {}
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
        if self.auto_verify:
            self.verified.add(user_id)
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

    def logout(self, token: str) -> None:
        self.sessions.pop(hash_session(token), None)

    def start_reset(self, email: str) -> str | None:
        try:
            user_id = self.by_email.get(clean_email(email))
        except AccountError:
            return None
        return self._mail_token(user_id, "reset", RESET_FOR) if user_id else None

    def finish_reset(self, raw: str, password: str) -> bool:
        secret = check_password(password)
        user_id = self._use_token(raw, "reset")
        if user_id is None:
            return False
        user = self.users[user_id]
        user["password_hash"] = hash_password(secret)
        # The link came by email, so the address is proven too.
        self.verified.add(user_id)
        self.sessions = {key: row for key, row in self.sessions.items() if row["user_id"] != user_id}
        self.attempts.pop(user["email"], None)
        return True

    def start_verify(self, user_id: str) -> str:
        return self._mail_token(user_id, "verify", VERIFY_FOR)

    def verify(self, raw: str) -> bool:
        user_id = self._use_token(raw, "verify")
        if user_id is None:
            return False
        self.verified.add(user_id)
        return True

    def _mail_token(self, user_id: str, purpose: str, life: timedelta) -> str:
        raw = secrets.token_urlsafe(32)
        self.email_tokens[hash_session(raw)] = {
            "user_id": user_id, "purpose": purpose, "expires_at": datetime.now(UTC) + life, "used": False,
        }
        return raw

    def _use_token(self, raw: str, purpose: str) -> str | None:
        row = self.email_tokens.get(hash_session(raw or ""))
        if row is None or row["purpose"] != purpose or row["used"] or row["expires_at"] <= datetime.now(UTC):
            return None
        row["used"] = True
        return row["user_id"]

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
            "verified": user_id in self.verified,
            "role": dict(self.memberships.get(user_id, [])).get(tenant, ""),
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
        return raw, _row(user_id, cleaned, display, tenant, [{"tenant": tenant, "role": "owner"}], verified=False)

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
                cur.execute(
                    "SELECT id, name, password_hash, email_verified_at IS NOT NULL FROM app_user WHERE email = %s",
                    (cleaned,),
                )
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
        return raw, _row(found[0], cleaned, found[1], memberships[0]["tenant"], memberships, verified=found[3])

    def read_session(self, token: str) -> dict | None:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT s.user_id, s.tenant_id, u.email, u.name, u.email_verified_at IS NOT NULL
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
        return _row(found[0], found[2], found[3], found[1], memberships, verified=found[4])

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
        current["role"] = _role(current["memberships"], tenant)
        return current

    def logout(self, token: str) -> None:
        _write("DELETE FROM app_session WHERE token_sha256 = %s", (hash_session(token),))

    def start_reset(self, email: str) -> str | None:
        try:
            cleaned = clean_email(email)
        except AccountError:
            return None
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM app_user WHERE email = %s", (cleaned,))
                found = cur.fetchone()
                raw = _insert_mail_token(cur, found[0], "reset", RESET_FOR) if found else None
            conn.commit()
        finally:
            conn.close()
        return raw

    def finish_reset(self, raw: str, password: str) -> bool:
        secret = check_password(password)
        conn = _connect()
        try:
            with conn.cursor() as cur:
                user_id = _use_mail_token(cur, raw, "reset")
                if user_id is None:
                    conn.rollback()
                    return False
                # The link came by email, so the address is proven too.
                cur.execute(
                    "UPDATE app_user SET password_hash = %s, email_verified_at = coalesce(email_verified_at, now())"
                    " WHERE id = %s RETURNING email",
                    (hash_password(secret), user_id),
                )
                email = cur.fetchone()[0]
                cur.execute("DELETE FROM app_session WHERE user_id = %s", (user_id,))
                cur.execute("DELETE FROM login_attempt WHERE email = %s", (email,))
            conn.commit()
        finally:
            conn.close()
        return True

    def start_verify(self, user_id: str) -> str:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                raw = _insert_mail_token(cur, user_id, "verify", VERIFY_FOR)
            conn.commit()
        finally:
            conn.close()
        return raw

    def verify(self, raw: str) -> bool:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                user_id = _use_mail_token(cur, raw, "verify")
                if user_id is not None:
                    cur.execute(
                        "UPDATE app_user SET email_verified_at = coalesce(email_verified_at, now()) WHERE id = %s",
                        (user_id,),
                    )
            conn.commit()
        finally:
            conn.close()
        return user_id is not None


def _row(user_id: str, email: str, name: str, tenant: str, memberships: list[dict], *, verified: bool) -> dict:
    return {
        "user_id": user_id,
        "email": email,
        "name": name,
        "tenant_id": tenant,
        "verified": bool(verified),
        "role": _role(memberships, tenant),
        "memberships": memberships,
    }


def _role(memberships: list[dict], tenant: str) -> str:
    return next((item["role"] for item in memberships if item["tenant"] == tenant), "")


def _write(sql: str, params: tuple) -> None:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _insert_mail_token(cur, user_id: str, purpose: str, life: timedelta) -> str:
    raw = secrets.token_urlsafe(32)
    cur.execute(
        "INSERT INTO email_token (token_sha256, user_id, purpose, expires_at) VALUES (%s, %s, %s, %s)",
        (hash_session(raw), user_id, purpose, datetime.now(UTC) + life),
    )
    return raw


def _use_mail_token(cur, raw: str, purpose: str) -> str | None:
    """Mark a live, unused link used and return its account. Single use: a second call finds nothing."""

    cur.execute(
        """
        UPDATE email_token SET used_at = now()
        WHERE token_sha256 = %s AND purpose = %s AND used_at IS NULL AND expires_at > now()
        RETURNING user_id
        """,
        (hash_session(raw or ""), purpose),
    )
    found = cur.fetchone()
    return found[0] if found else None


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
