# Martinus Qlik pre ChatGPT / Codex (Windows)

Jeden Python súbor (`martinus_qlik.py`, len štandardná knižnica), ktorý sprístupní Qlik dáta
v ChatGPT desktop appke pre Windows (voľba **Codex** v menu vľavo hore) alebo v Codex CLI. Beží na
Pythone, ktorý si Codex nosí so sebou, takže sa nič neinštaluje. Prihlásenie firemným Microsoft
účtom (Entra ID, PKCE), tokeny šifrované Windows DPAPI, prístup presne podľa Qlik oprávnení používateľa.

## Inštalácia (bežný používateľ)

Interný návod „Qlik MCP Inštalačný návod ChatGPT/Codex (Windows)" = jeden riadok do PowerShellu
(Win + R → `powershell` → vložiť → Enter), potom Microsoft prihlásenie a reštart ChatGPT appky:

```powershell
[Net.ServicePointManager]::SecurityProtocol='Tls12'; $d="$env:USERPROFILE\.martinus-qlik"; New-Item -ItemType Directory -Force $d | Out-Null; Invoke-WebRequest -UseBasicParsing https://raw.githubusercontent.com/DanoMartinus/martinus-qlik-codex/main/martinus_qlik.py -OutFile "$d\martinus_qlik.py"; & "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" "$d\martinus_qlik.py" setup
```

⚠️ Inštaláciu nerob cez Codex samotný: jeho sandbox nemá prístup na internet ani zápis mimo otvoreného
priečinka (aj proces spustený cez `Start-Process` dedí obmedzenia), takže `setup`/`verify` v ňom padnú na
`Access denied` / `PermissionError: tokens.lock`. Most ako MCP server appka spúšťa mimo sandboxu, ten funguje.

ChatGPT appka pre Windows je v Microsoft Store (ID `9PLM9XGG6VKS`,
`winget install --id 9PLM9XGG6VKS -s msstore`). Staršia samostatná „Codex" appka sa aktualizáciou
zmení na túto ChatGPT appku (Chat / Work / Codex).

## Príkazy skriptu

`setup` zapíše `[mcp_servers.qlik]` do `%USERPROFILE%\.codex\config.toml` (staré `qlik` sekcie
odstráni, ostatné zachová, zálohuje do `config.toml.bak-MartinusQlik`), prihlási cez Microsoft,
overí spojenie (a tým vytvorí cache nástrojov) a vypíše HOTOVO. Potom reštartuj ChatGPT appku.

Ďalšie príkazy: `login`, `verify`, `status`, `uninstall`. Prepínač `--no-pause` nečaká na Enter.
Codexov Python chýba? Otvor ChatGPT/Codex appku, počkaj minútu (stiahne si runtime) a skús znova;
ide to aj s ľubovoľným Pythonom 3.11+ (`MARTINUSQLIK_PYTHON=cesta`).

Ak by Codex config nešiel zapísať (firemná politika), server sa dá pridať aj v appke:
**Settings → MCP servers → Add server** (typ STDIO, command a args vypíše `status`) → **Restart**.

## Čo to robí a čo nie

- `serve` = stdio ↔ streamable-HTTP proxy: každú MCP správu z Codexu pošle s Bearer tokenom na
  `https://qlikmcp.martinus.sk/mcp`, odpovede (JSON/SSE) vráti späť. Po reštarte gatewayu session
  ticho obnoví.
- Na `initialize` a `tools/list` odpovie Codexu okamžite z lokálnej cache
  (`%USERPROFILE%\.martinus-qlik\mcp_cache.json` — len popisy nástrojov, žiadne tokeny ani dáta)
  a session na gatewayi založí v pozadí. Dôvod: Codex čaká na „optional" MCP server pri skladaní
  zoznamu nástrojov len 1 s (`mcp_optional_startup_grace_ms`), spojenie cez App Proxy trvá 2–3 s —
  bez cache by Qlik nástroje v Codexe chýbali, hoci server beží. Cache vytvorí `verify`/`setup`
  a každý štart ju obnoví.
- Nástroje sú v configu nastavené na `default_tools_approval_mode = "approve"` (bez schvaľovacích
  otázok); `auto`/`writes` sa pri Qlik nástrojoch pýtajú pri každom volaní.
- Všetko (skript, tokeny, cache, log) je v `%USERPROFILE%\.martinus-qlik`, zámerne NIE v `AppData\Local`:
  Store (MSIX) appky ako ChatGPT/Codex alebo Claude presmerúvajú zápisy do `AppData\Local` do vlastného
  `Packages\…\LocalCache`, takže súbor nainštalovaný z jednej appky druhá nevidí (most potom padne na
  „connection closed: initialize response“). `%USERPROFILE%` sa nevirtualizuje (preto funguje aj `~/.codex`).
- Token ide výlučne na ten jeden host. Kódy, tokeny ani dáta sa nelogujú
  (`%USERPROFILE%\.martinus-qlik\bridge.log` obsahuje len stavové riadky).
- Nič sa nezapisuje do Qliku: rola používateľa na gatewayi je len na čítanie.

## Riešenie problémov

| Príznak | Čo s tým |
|---|---|
| Qlik v Codexe zmizol (appka si po update prepísala config) | spusti `setup` znova (prihlásenie si pamätá) |
| V appke je server `qlik` červený / „connection closed: initialize response“ | skript nie je na ceste z configu (napr. inštalácia z inej Store appky do virtualizovaného AppData\Local, verzie ≤ 2.1) → spusti `setup` znova z v2.2+ |
| `PermissionError: tokens.lock` / `Access denied` pri setup alebo verify | príkaz bežal v Codex sandboxe → spusti ho v PowerShelli (Win+R); ak už bolo HOTOVO, len reštartuj appku |
| Codex píše, že nástroj `qlik_…` nie je dostupný | spusti `verify` (obnoví cache) a appku úplne reštartuj; `status` musí hlásiť „Cache nastrojov: …" |
| Codex sa pri každom Qlik volaní pýta na schválenie | v `config.toml` chýba `default_tools_approval_mode = "approve"` → spusti `setup` znova |
| `AADSTS50105` pri prihlásení | účet nie je v prístupovej skupine, napíš správcovi (Daniel Križan) |
| `Port 8975 je obsadeny` | beží iné prihlásenie (napr. Claude Code), zavri ho a opakuj |
| `ziadne streamy` | Qlik účet nemá prístup na aplikácie, napíš správcovi |

Overené 2026-10-21 (v2.2.0): ChatGPT/Codex appka 26.1013 (codex 0.163), Codex Python 3.12.14, 53 nástrojov,
`codex exec` vráti zoznam Qlik streamov v čistom aj plnom configu.

Kontakt: Daniel Križan (Workvivo).
