from fastapi.testclient import TestClient

from conftest import CSRF, PASSWORD, make_user
from sharedcompute.services import accounts


def test_register_login_logout(app, anon):
    r = anon.post("/api/v1/auth/register", json={"email": "Ann@Example.com", "password": PASSWORD, "display_name": "Ann"})
    assert r.status_code == 201
    assert r.json()["user"]["email"] == "ann@example.com"
    assert "sc_session" in r.cookies
    me = anon.get("/api/v1/me").json()
    assert me["user"]["display_name"] == "Ann"
    assert me["balances"] == {"available_cents": 0, "held_cents": 0, "earnings_cents": 0}

    assert anon.post("/api/v1/auth/logout").status_code == 200
    assert anon.get("/api/v1/me").status_code == 401

    fresh = TestClient(app, headers=CSRF)
    r = fresh.post("/api/v1/auth/login", json={"email": "ANN@example.com", "password": PASSWORD})
    assert r.status_code == 200
    assert fresh.get("/api/v1/me").status_code == 200


def test_logout_revokes_the_token_server_side(app, anon):
    token = anon.post("/api/v1/auth/register", json={"email": "a@example.com", "password": PASSWORD,
                                                     "display_name": "Ann"}).json()["token"]
    api = TestClient(app, headers={"Authorization": f"Bearer {token}"})
    assert api.get("/api/v1/me").status_code == 200
    anon.post("/api/v1/auth/logout")
    assert api.get("/api/v1/me").status_code == 401


def test_bearer_token_auth_for_api_clients(app, anon):
    token = anon.post("/api/v1/auth/register", json={"email": "a@example.com", "password": PASSWORD,
                                                     "display_name": "Ann"}).json()["token"]
    assert token.startswith("scs_")
    api = TestClient(app, headers={"Authorization": f"Bearer {token}", **CSRF})
    assert api.get("/api/v1/me").json()["user"]["email"] == "a@example.com"


def test_registration_validation(anon):
    def reg(**kw):
        body = {"email": "x@example.com", "password": PASSWORD, "display_name": "Xavier", **kw}
        return anon.post("/api/v1/auth/register", json=body)

    assert reg(email="not-an-email").json()["error"]["field"] == "email"
    assert reg(password="short").json()["error"]["field"] == "password"
    assert reg(display_name="<script>").json()["error"]["field"] == "display_name"
    assert reg(extra="x").status_code == 422
    assert reg().status_code == 201
    dup = reg(email="X@EXAMPLE.COM")
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "conflict"


def test_wrong_password_and_unknown_user_look_identical(anon, alice):
    a = anon.post("/api/v1/auth/login", json={"email": "alice@example.com", "password": "wrong password!"})
    b = anon.post("/api/v1/auth/login", json={"email": "nobody@example.com", "password": "wrong password!"})
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json()


def test_system_accounts_cannot_sign_in(anon):
    r = anon.post("/api/v1/auth/login", json={"email": accounts.SAMPLE_NETWORK_EMAIL, "password": ""})
    assert r.status_code == 401


def test_csrf_header_required_for_mutations(app, alice):
    no_header = TestClient(app, cookies=alice.cookies)
    assert no_header.get("/api/v1/me").status_code == 200  # reads are fine
    r = no_header.post("/api/v1/wallet/demo-topup", json={"amount_usd": "5"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf"


def test_protected_endpoints_require_auth(anon):
    for method, path in [("get", "/api/v1/jobs"), ("post", "/api/v1/jobs"), ("get", "/api/v1/provider/nodes"),
                         ("get", "/api/v1/economics"), ("post", "/api/v1/wallet/demo-topup")]:
        r = getattr(anon, method)(path, **({"json": {}} if method == "post" else {}))
        assert r.status_code in (401, 422), (path, r.status_code)
    assert anon.get("/api/v1/jobs").status_code == 401


def test_sessions_expire(app, alice, clock):
    clock.advance(app.state.settings.session_ttl_seconds + 1)
    assert alice.get("/api/v1/me").status_code == 401


def test_login_rate_limit(app, anon):
    app.state.settings.login_attempts_per_minute = 3  # the limiter is created on first use
    codes = [anon.post("/api/v1/auth/login", json={"email": "z@example.com", "password": "x"}).status_code
             for _ in range(5)]
    assert codes == [401, 401, 401, 429, 429]


def test_operator_flag_controls_platform_economics(app, alice):
    assert alice.get("/api/v1/economics").json()["platform"] is None
    with app.state.db.write() as conn:
        accounts.set_operator(conn, "alice@example.com")
    assert alice.get("/api/v1/economics").json()["platform"] is not None


def test_make_operator_cli(app, settings, monkeypatch, capsys):
    from sharedcompute.__main__ import main

    make_user(app, "carol@example.com", credits=None)
    monkeypatch.setenv("SHAREDCOMPUTE_DB", str(settings.db_path))
    assert main(["make-operator", "carol@example.com"]) == 0
    assert main(["make-operator", "nobody@example.com"]) == 1
    with app.state.db.read() as conn:
        assert conn.execute("SELECT is_operator FROM users WHERE email = 'carol@example.com'").fetchone()[0] == 1
