"""
martinus_qlik.py — Qlik dáta v ChatGPT/Codex desktop (Windows) cez lokálny MCP most.
LEN ŠTANDARDNÁ KNIŽNICA (žiadne pip balíky) → beží na Pythone, ktorý si Codex appka
nosí so sebou (~/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe).
Žiadne exe, žiadna inštalácia, žiadny SmartScreen.

Režimy:
  setup      zapíše [mcp_servers.qlik] do ~/.codex/config.toml (command = Codex Python,
             args = [tento súbor, "serve"]), prihlási cez Microsoft, overí spojenie, vypíše HOTOVO
  login      len Microsoft prihlásenie (obnova tokenov)
  verify     test: initialize + tools/list + qlik_list_streams
  status     stav (config, tokeny, expirácia) bez siete
  serve      MCP stdio ↔ streamable-HTTP proxy pre Codex (spúšťa Codex sám)
  uninstall  odstráni config sekciu a tokeny

Autentifikácia: Entra ID public client (ten istý ako Claude Code plugin) — PKCE,
redirect http://localhost:8975/callback, GUID scope, refresh token. Tokeny šifruje Windows
DPAPI (len tento používateľ na tomto PC) v %LOCALAPPDATA%\\MartinusQlik\\tokens.dpapi.
Access token ide VÝLUČNE na https://qlikmcp.martinus.sk (App Proxy → gateway → Qlik ACL
používateľa). Kódy/tokeny sa nikdy nelogujú.

Env na testy: MARTINUSQLIK_HOME, MARTINUSQLIK_CODEX_CONFIG, MARTINUSQLIK_NO_BROWSER=1,
MARTINUSQLIK_PYTHON (vynútená cesta k interpretru pre config).
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import hashlib
import http.server
import json
import os
import re
import secrets
import shutil
import ssl
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

VERSION = "2.0.0"
APP_NAME = "MartinusQlik"
SCRIPT_NAME = "martinus_qlik.py"
TENANT = "38f4f961-e54b-4536-a1f8-6fbc2f81e4f6"
CLIENT = "63a2bf02-b7f4-4bea-a4a5-75c801ee90e4"
SCOPE = CLIENT + "/user_impersonation offline_access"
AUTHORITY = "https://login.microsoftonline.com/" + TENANT + "/oauth2/v2.0"
REDIRECT_PORT = 8975
REDIRECT = f"http://localhost:{REDIRECT_PORT}/callback"
REMOTE_HOST = "qlikmcp.martinus.sk"
REMOTE = f"https://{REMOTE_HOST}/mcp"
SERVER_KEY = "qlik"
CONTACT = "Daniel Krizan (Workvivo / daniel.krizan@martinus.sk)"
CODEX_PYTHON = Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "python" / "python.exe"

HOME = Path(os.environ.get("MARTINUSQLIK_HOME") or Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP_NAME)
CODEX_CONFIG = Path(os.environ.get("MARTINUSQLIK_CODEX_CONFIG") or Path.home() / ".codex" / "config.toml")
TOKENS = HOME / "tokens.dpapi"
LOCK = HOME / "tokens.lock"
LOG = HOME / "bridge.log"
SSL_CTX = ssl.create_default_context()


# ---------------------------------------------------------------------------
# Výstup / log  (v režime serve NIKDY nepísať na stdout — patrí MCP protokolu)
# ---------------------------------------------------------------------------
SERVE_MODE = False


def _setup_stdio() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def say(msg: str = "") -> None:
    print(msg, file=sys.stderr if SERVE_MODE else sys.stdout, flush=True)
    log(msg)


def log(msg: str) -> None:
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except OSError:
        pass


class UserError(Exception):
    """Chyba s hláškou pre používateľa (bez tracebacku)."""


# ---------------------------------------------------------------------------
# DPAPI (ctypes)
# ---------------------------------------------------------------------------
class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> _BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _dpapi(data: bytes, protect: bool) -> bytes:
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    inp, out = _blob(data), _BLOB()
    if protect:
        ok = crypt32.CryptProtectData(ctypes.byref(inp), ctypes.c_wchar_p("Martinus Qlik OAuth"), None, None, None, 0, ctypes.byref(out))
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out))
    if not ok:
        raise UserError("Windows DPAPI zlyhalo (tokeny sa nedaju " + ("zasifrovat" if protect else "desifrovat") + ")")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def save_tokens(tokens: dict, previous: dict | None = None) -> None:
    if previous and not tokens.get("refresh_token"):
        tokens["refresh_token"] = previous.get("refresh_token")
    tokens["expires_at"] = time.time() + int(tokens.get("expires_in", 3600))
    HOME.mkdir(parents=True, exist_ok=True)
    tmp = TOKENS.with_suffix("." + secrets.token_hex(6) + ".tmp")
    tmp.write_bytes(_dpapi(json.dumps(tokens).encode(), protect=True))
    os.replace(tmp, TOKENS)


def load_tokens() -> dict | None:
    if not TOKENS.exists():
        return None
    try:
        return json.loads(_dpapi(TOKENS.read_bytes(), protect=False))
    except Exception:
        log("tokens.dpapi: nedaju sa desifrovat (iny pouzivatel/PC?) — treba nove prihlasenie")
        return None


# ---------------------------------------------------------------------------
# HTTP (urllib)
# ---------------------------------------------------------------------------
def _http(method: str, url: str, data: bytes | None, headers: dict, timeout: float) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


# ---------------------------------------------------------------------------
# Entra OAuth (PKCE, public client)
# ---------------------------------------------------------------------------
def _entra_hint(body: dict | None, status: int) -> str:
    desc = (body or {}).get("error_description", "") if isinstance(body, dict) else ""
    if "AADSTS50105" in desc:
        return "Tvoj ucet nie je v pristupovej skupine Qlik — napis " + CONTACT + "."
    if "AADSTS70000" in desc or "AADSTS700082" in desc or "invalid_grant" in str((body or {}).get("error", "")):
        return "Prihlasenie expirovalo — spusti setup/login znova."
    code = re.search(r"AADSTS\d+", desc)
    return f"Microsoft odmietol prihlasenie (HTTP {status}{', ' + code.group(0) if code else ''}). Napis " + CONTACT + "."


def exchange(data: dict) -> dict:
    body = urllib.parse.urlencode({"client_id": CLIENT, "scope": SCOPE, **data}).encode()
    status, _, raw = _http("POST", AUTHORITY + "/token", body,
                           {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}, 30)
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = None
    if status != 200:
        err = (parsed or {}).get("error", "?") if isinstance(parsed, dict) else "?"
        log(f"token exchange failed: HTTP {status} {err}")
        raise UserError(_entra_hint(parsed, status))
    return parsed


_TOKEN_LOCK = threading.Lock()          # vlákna v TOMTO procese (msvcrt zámok nie je reentrantný)
_TOKEN_CACHE: dict = {"token": None, "exp": 0.0}


def access_token() -> str:
    """Platný access token; refresh ak expiruje do 5 min.
    In-process cache + threading.Lock (paralelné requesty z Codexu), msvcrt file lock = viac procesov."""
    with _TOKEN_LOCK:
        if _TOKEN_CACHE["token"] and _TOKEN_CACHE["exp"] > time.time() + 300:
            return _TOKEN_CACHE["token"]
        tokens = _access_token_locked()
        _TOKEN_CACHE["token"], _TOKEN_CACHE["exp"] = tokens["access_token"], float(tokens["expires_at"])
        return tokens["access_token"]


def _access_token_locked() -> dict:
    import msvcrt
    HOME.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+b") as lock:
        lock.seek(0)
        if not lock.read(1):
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        try:
            tokens = load_tokens()
            if not tokens:
                raise UserError("Nie si prihlaseny — spusti setup (Microsoft prihlasenie).")
            if tokens["expires_at"] < time.time() + 300:
                fresh = exchange({"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]})
                save_tokens(fresh, tokens)
                tokens = fresh
                log("token refreshed")
            return tokens
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def login() -> None:
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    outcome: dict = {}

    class Callback(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            q = urllib.parse.parse_qs(parsed.query)
            if parsed.path != "/callback" or not secrets.compare_digest(q.get("state", [""])[0], state):
                self.send_error(400)
                return
            if "code" in q:
                outcome["code"] = q["code"][0]
                text = "Prihlasenie prebehlo. Toto okno mozes zavriet a vratit sa do ChatGPT/Codex."
            else:
                outcome["error"] = q.get("error", ["?"])[0]
                outcome["desc"] = q.get("error_description", [""])[0]
                text = "Prihlasenie zlyhalo: " + outcome["desc"][:300]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(text.encode("utf-8"))

    try:
        server = http.server.HTTPServer(("127.0.0.1", REDIRECT_PORT), Callback)
    except OSError:
        raise UserError(f"Port {REDIRECT_PORT} je obsadeny (bezi ine prihlasenie? Claude Code?). Zavri ho a skus znova.")
    with server:
        server.timeout = 1
        url = AUTHORITY + "/authorize?" + urllib.parse.urlencode({
            "client_id": CLIENT, "response_type": "code", "redirect_uri": REDIRECT, "scope": SCOPE,
            "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
        say("Otvaram prihlasenie Microsoft v prehliadaci. Prihlas sa FIREMNYM uctom (meno.priezvisko@martinus.sk).")
        say("Ak sa prehliadac neotvori, skopiruj tuto adresu rucne:")
        say("  " + url)
        if os.environ.get("MARTINUSQLIK_NO_BROWSER") != "1":
            webbrowser.open(url)
        deadline = time.time() + 600
        while not outcome and time.time() < deadline:
            server.handle_request()
    if "code" not in outcome:
        desc = outcome.get("desc", "")
        if "AADSTS50105" in desc:
            raise UserError("Tvoj ucet nie je v pristupovej skupine Qlik (AADSTS50105) — napis " + CONTACT + ".")
        raise UserError("Prihlasenie Microsoft sa nedokoncilo" + (f" ({outcome.get('error')})" if outcome else " (timeout 10 min)") + ". Skus znova.")
    save_tokens(exchange({"grant_type": "authorization_code", "code": outcome["code"],
                          "redirect_uri": REDIRECT, "code_verifier": verifier}))
    say("Microsoft prihlasenie OK. Tokeny ulozene (Windows DPAPI).")
    log("login OK")


# ---------------------------------------------------------------------------
# MCP streamable-HTTP klient (stdlib) + stdio proxy
# ---------------------------------------------------------------------------
class Gateway:
    """Jedna MCP session na gateway. Thread-safe (session id + re-init)."""

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.init_msg: dict | None = None
        self._lock = threading.Lock()

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             "Authorization": "Bearer " + access_token(), "User-Agent": f"MartinusQlik/{VERSION}"}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    def post(self, msg: dict, timeout: float = 300.0, _retry: bool = True) -> list[dict]:
        """Pošle JSON-RPC správu; vráti zoznam JSON-RPC správ z odpovede (0..n)."""
        if msg.get("method") == "initialize":
            self.init_msg = msg
        body = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        status, headers, raw = _http("POST", REMOTE, body, self._headers(), timeout)
        if "mcp-session-id" in headers:
            with self._lock:
                self.session_id = headers["mcp-session-id"]
        if status == 202 or (status in (200, 204) and not raw):
            return []
        if status in (400, 404) and _retry and self.init_msg and msg.get("method") != "initialize" and self.session_id:
            # Session na gatewayi zanikla (reštart 05:00) → tichý re-init a jeden retry.
            log(f"gateway HTTP {status} — session lost, re-initializing")
            with self._lock:
                self.session_id = None
            self.post(self.init_msg, timeout=60, _retry=False)
            self.post({"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=60, _retry=False)
            return self.post(msg, timeout, _retry=False)
        if status >= 400:
            snippet = raw[:200].decode("utf-8", "replace")
            log(f"gateway HTTP {status} for {msg.get('method')}: {snippet}")
            if status in (401, 403):
                raise UserError("Gateway odmietol prihlasenie (HTTP %d) — spusti setup/login znova." % status)
            raise UserError(f"Gateway vratil HTTP {status}: {snippet}")
        ctype = headers.get("content-type", "")
        if "text/event-stream" in ctype:
            return _parse_sse(raw)
        if not raw.strip():
            return []
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else [parsed]


def _parse_sse(raw: bytes) -> list[dict]:
    out: list[dict] = []
    for chunk in re.split(rb"\r?\n\r?\n", raw):
        data_lines = [ln[5:].lstrip() for ln in chunk.splitlines() if ln.startswith(b"data:")]
        if not data_lines:
            continue
        try:
            out.append(json.loads(b"\n".join(data_lines)))
        except ValueError:
            log("SSE: nerozparsovatelny data blok (ignorovany)")
    return out


def serve() -> None:
    """stdio (Codex) ↔ HTTP (gateway). Každá správa vo vlastnom vlákne, stdout pod zámkom."""
    global SERVE_MODE
    SERVE_MODE = True
    gw = Gateway()
    out_lock = threading.Lock()
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer

    def emit(obj: dict) -> None:
        line = json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n"
        with out_lock:
            stdout.write(line)
            stdout.flush()

    def handle(msg: dict) -> None:
        try:
            for reply in gw.post(msg):
                emit(reply)
        except UserError as e:
            log(f"serve: {e}")
            if "id" in msg:
                emit({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32000, "message": str(e)}})
        except Exception as e:  # sieť / parse
            import traceback
            log(f"serve: {type(e).__name__}: {str(e)[:200]} | {traceback.format_exc()[-600:]}")
            if "id" in msg:
                emit({"jsonrpc": "2.0", "id": msg["id"],
                      "error": {"code": -32000, "message": f"Qlik gateway nedostupny ({type(e).__name__}). Skus znova; ak pretrvava, napis " + CONTACT}})

    log("serve start")
    threads: list[threading.Thread] = []
    for raw in stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            log("serve: nevalidny JSON od klienta (ignorovany)")
            continue
        msgs = msg if isinstance(msg, list) else [msg]
        for m in msgs:
            if m.get("method") == "initialize":
                handle(m)          # synchrónne — session id musí byť známy pred ďalšími správami
            else:
                t = threading.Thread(target=handle, args=(m,), daemon=True)
                t.start()
                threads.append(t)
        threads = [t for t in threads if t.is_alive()]
    for t in threads:
        t.join(timeout=5)
    log("serve end (stdin EOF)")


def verify() -> None:
    say("Overujem spojenie na Qlik gateway ...")
    gw = Gateway()
    try:
        init = gw.post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                   "clientInfo": {"name": APP_NAME, "version": VERSION}}}, timeout=60)
        if not init or "result" not in init[0]:
            raise UserError("Gateway neodpovedal na initialize — napis " + CONTACT + ".")
        gw.post({"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=60)
        tools = gw.post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, timeout=60)
        n_tools = len(tools[0].get("result", {}).get("tools", [])) if tools else 0
        res = gw.post({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                       "params": {"name": "qlik_list_streams", "arguments": {}}}, timeout=120)
        result = res[0].get("result", {}) if res else {}
        if result.get("isError"):
            raise UserError("Gateway vratil chybu pri qlik_list_streams — napis " + CONTACT + ".")
        text = next((c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"), "[]")
        try:
            streams = json.loads(text)
            names = [s.get("name", "?") for s in streams] if isinstance(streams, list) else []
        except ValueError:
            names = []
    except UserError:
        raise
    except Exception as e:
        msg = f"{type(e).__name__}: {str(e)[:200]}"
        log(f"verify failed: {msg}")
        raise UserError("Spojenie na gateway zlyhalo (" + msg + "). Skontroluj internet a skus znova; inak napis " + CONTACT + ".")
    say(f"OK: {n_tools} nastrojov, {len(names)} Qlik streamov" + (": " + ", ".join(names[:6]) + (" ..." if len(names) > 6 else "") if names else ""))
    if not names:
        say("POZOR: ziadne streamy — tvoj Qlik ucet asi nema pristup na ziadnu aplikaciu. Napis " + CONTACT + ".")
    log(f"verify OK tools={n_tools} streams={len(names)}")


# ---------------------------------------------------------------------------
# Codex config.toml
# ---------------------------------------------------------------------------
_SECTION_RE = re.compile(r"^\s*\[\s*mcp_servers\.(?:\"qlik\"|qlik)(?:\.[^\]]*)?\]\s*$")
_ANY_HEADER_RE = re.compile(r"^\s*\[")


def _strip_qlik_sections(text: str) -> str:
    out, skipping = [], False
    for line in text.splitlines(keepends=True):
        if _SECTION_RE.match(line):
            skipping = True
            continue
        if skipping and _ANY_HEADER_RE.match(line):
            skipping = False
        if not skipping:
            out.append(line)
    return "".join(out)


def _toml_str(s: str) -> str:
    return '"' + s.replace("\\", "/").replace('"', '\\"') + '"'


def interpreter() -> Path:
    """Python, ktorým Codex spustí most: Codexov vlastný runtime (stabilná cesta), inak tento."""
    forced = os.environ.get("MARTINUSQLIK_PYTHON")
    if forced:
        return Path(forced)
    if CODEX_PYTHON.exists():
        return CODEX_PYTHON
    exe = Path(sys.executable)
    if exe.name.lower().startswith("python") and exe.exists():
        return exe
    raise UserError("Nenasiel som Python z Codex appky (" + str(CODEX_PYTHON) + "). Otvor ChatGPT/Codex appku, pockaj minutu (stiahne si runtime) a spusti setup znova.")


def install_self() -> Path:
    HOME.mkdir(parents=True, exist_ok=True)
    src = Path(__file__).resolve()
    dst = HOME / SCRIPT_NAME
    if src != dst:
        shutil.copyfile(src, dst)
        say(f"Skript nainstalovany do {dst}")
    return dst


def write_config(python: Path, script: Path) -> None:
    CODEX_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    old = CODEX_CONFIG.read_text(encoding="utf-8") if CODEX_CONFIG.exists() else ""
    if old:
        try:
            tomllib.loads(old)
        except tomllib.TOMLDecodeError as e:
            raise UserError(f"Existujuci {CODEX_CONFIG} nie je platny TOML ({e}). Nebudem ho menit — napis " + CONTACT + ".")
        shutil.copyfile(CODEX_CONFIG, CODEX_CONFIG.with_name(f"config.toml.bak-{APP_NAME}"))
    block = (
        f"\n# Martinus Qlik most ({SCRIPT_NAME} v{VERSION}) — zapisane {datetime.now():%Y-%m-%d %H:%M}\n"
        f"[mcp_servers.{SERVER_KEY}]\n"
        f"command = {_toml_str(str(python))}\n"
        f"args = [{_toml_str(str(script))}, \"serve\"]\n"
        f"startup_timeout_sec = 120\n"
        f"tool_timeout_sec = 300\n"
        f"default_tools_approval_mode = \"auto\"\n"
        f"\n[mcp_servers.{SERVER_KEY}.env]\n"
        f"PYTHONUTF8 = \"1\"\n"
    )
    new = _strip_qlik_sections(old).rstrip("\n") + "\n" + block
    try:
        parsed = tomllib.loads(new)
        assert parsed["mcp_servers"][SERVER_KEY]["args"][1] == "serve"
    except Exception as e:  # nikdy nezapisat nevalidny config (MS Store appka by sa zasekla na starte)
        raise UserError(f"Interna chyba pri generovani configu ({e}) — napis " + CONTACT + ".")
    CODEX_CONFIG.write_text(new, encoding="utf-8")
    say(f"Codex config zapisany: {CODEX_CONFIG}")
    log("config written")


def config_state() -> str:
    if not CODEX_CONFIG.exists():
        return "chyba (config.toml neexistuje)"
    try:
        srv = tomllib.loads(CODEX_CONFIG.read_text(encoding="utf-8")).get("mcp_servers", {}).get(SERVER_KEY)
    except tomllib.TOMLDecodeError:
        return "config.toml NIE JE platny TOML"
    if not srv:
        return "chyba sekcia [mcp_servers.qlik] (Codex appka ju mohla pri starte zmazat — spusti setup znova)"
    cmd, args = str(srv.get("command", "")), srv.get("args") or []
    if not Path(cmd).exists():
        return f"sekcia existuje, ale command neexistuje: {cmd}"
    if args and not Path(str(args[0])).exists():
        return f"sekcia existuje, ale skript neexistuje: {args[0]}"
    return f"OK ({cmd} {' '.join(map(str, args))})"


# ---------------------------------------------------------------------------
# Príkazy
# ---------------------------------------------------------------------------
def cmd_setup() -> None:
    say(f"=== Martinus Qlik pre ChatGPT/Codex — setup v{VERSION} ===")
    python = interpreter()
    script = install_self()
    write_config(python, script)
    tokens = load_tokens()
    if tokens and tokens.get("refresh_token"):
        say("Uz si prihlaseny — skusim obnovit tokeny bez prihlasenia ...")
        try:
            access_token()
        except UserError as e:
            say(f"  ({e}) -> nove prihlasenie")
            login()
    else:
        login()
    verify()
    say("")
    say("HOTOVO. Teraz:")
    say("  1. Uplne zavri ChatGPT/Codex appku (pravy klik na ikonu pri hodinach -> Quit) a spusti ju znova.")
    say("  2. V Codex zalozke napis: vylistuj mi Qlik streamy")
    log("setup OK")


def cmd_status() -> None:
    say(f"{APP_NAME} v{VERSION}  home={HOME}")
    say(f"Codex Python: {'OK ' + str(CODEX_PYTHON) if CODEX_PYTHON.exists() else 'nenajdeny (' + str(CODEX_PYTHON) + ')'}")
    say(f"Codex config: {config_state()}")
    t = load_tokens()
    if not t:
        say("Prihlasenie: NIE (spusti setup/login)")
    else:
        left = int(t.get("expires_at", 0) - time.time())
        say(f"Prihlasenie: ano, access token {'platny este ' + str(left // 60) + ' min' if left > 0 else 'expirovany (obnovi sa refresh tokenom pri dalsom volani)'}; refresh token: {'ano' if t.get('refresh_token') else 'NIE'}")


def cmd_uninstall() -> None:
    if CODEX_CONFIG.exists():
        new = _strip_qlik_sections(CODEX_CONFIG.read_text(encoding="utf-8"))
        tomllib.loads(new)
        CODEX_CONFIG.write_text(new, encoding="utf-8")
        say("Sekcia [mcp_servers.qlik] odstranena z Codex configu.")
    for f in (TOKENS, LOCK):
        if f.exists():
            f.unlink()
    say("Tokeny zmazane. Priecinok " + str(HOME) + " mozes zmazat rucne.")


def main(argv: list[str]) -> int:
    _setup_stdio()
    args = [a for a in argv if not a.startswith("--")]
    flags = {a for a in argv if a.startswith("--")}
    mode = args[0] if args else "setup"
    pause = (mode in ("setup", "login", "verify", "status", "uninstall")) and "--no-pause" not in flags
    rc = 0
    try:
        {"setup": cmd_setup, "login": login, "verify": verify, "status": cmd_status,
         "serve": serve, "uninstall": cmd_uninstall}[mode]()
    except KeyError:
        say(f"Pouzitie: python {SCRIPT_NAME} [setup|login|verify|status|serve|uninstall] [--no-pause]")
        rc = 2
    except UserError as e:
        say("")
        say("CHYBA: " + str(e))
        rc = 1
    except KeyboardInterrupt:
        rc = 130
    except Exception as e:
        log(f"unexpected {type(e).__name__}: {str(e)[:300]}")
        say("")
        say(f"NEOCAKAVANA CHYBA: {type(e).__name__}: {str(e)[:200]}")
        say("Posli prosim tento text + subor " + str(LOG) + " -> " + CONTACT)
        rc = 1
    if pause and sys.stdin and sys.stdin.isatty():
        try:
            input("\nStlac Enter na zatvorenie okna ...")
        except EOFError:
            pass
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
