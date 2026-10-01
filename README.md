# Martinus Qlik pre ChatGPT / Codex (Windows)

Jeden Python súbor (`martinus_qlik.py`, len štandardná knižnica), ktorý sprístupní Qlik dáta
v ChatGPT desktop appke (záložka Codex) alebo v Codex appke/CLI. Beží na Pythone, ktorý si Codex
nosí so sebou, takže sa nič neinštaluje. Prihlásenie firemným Microsoft účtom (Entra ID, PKCE),
tokeny šifrované Windows DPAPI, prístup presne podľa Qlik oprávnení používateľa.

## Inštalácia (bežný používateľ)

Otvor ChatGPT appku → Codex → vlož master prompt z interného návodu
(„Qlik dáta v ChatGPT (Codex) — inštalačný návod"). Codex si súbor stiahne, nastaví a otvorí
Microsoft prihlásenie. Nič viac netreba.

## Inštalácia ručne (technický používateľ)

```powershell
$dir = "$env:LOCALAPPDATA\MartinusQlik"; New-Item -ItemType Directory -Force $dir | Out-Null
Invoke-WebRequest https://raw.githubusercontent.com/DanoMartinus/martinus-qlik-codex/main/martinus_qlik.py -OutFile "$dir\martinus_qlik.py"
& "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" "$dir\martinus_qlik.py" setup
```

`setup` zapíše `[mcp_servers.qlik]` do `%USERPROFILE%\.codex\config.toml` (staré `qlik` sekcie
odstráni, ostatné zachová, zálohuje do `config.toml.bak-MartinusQlik`), prihlási cez Microsoft,
overí spojenie a vypíše HOTOVO. Potom reštartuj ChatGPT/Codex appku.

Ďalšie príkazy: `login`, `verify`, `status`, `uninstall`. Prepínač `--no-pause` nečaká na Enter.
Codexov Python chýba? Otvor Codex appku, počkaj minútu (stiahne si runtime) a skús znova; ide to aj
s ľubovoľným Pythonom 3.11+ (`MARTINUSQLIK_PYTHON=cesta`).

## Čo to robí a čo nie

- `serve` = stdio ↔ streamable-HTTP proxy: každú MCP správu z Codexu pošle s Bearer tokenom na
  `https://qlikmcp.martinus.sk/mcp`, odpovede (JSON/SSE) vráti späť. Po reštarte gatewayu session
  ticho obnoví.
- Token ide výlučne na ten jeden host. Kódy, tokeny ani dáta sa nelogujú
  (`%LOCALAPPDATA%\MartinusQlik\bridge.log` obsahuje len stavové riadky).
- Nič sa nezapisuje do Qliku: rola používateľa na gatewayi je len na čítanie.

## Riešenie problémov

| Príznak | Čo s tým |
|---|---|
| Qlik v Codexe zmizol (appka si po update prepísala config) | spusti `setup` znova (prihlásenie si pamätá) |
| `AADSTS50105` pri prihlásení | účet nie je v prístupovej skupine, napíš správcovi (Daniel Križan) |
| `Port 8975 je obsadeny` | beží iné prihlásenie (napr. Claude Code), zavri ho a opakuj |
| `ziadne streamy` | Qlik účet nemá prístup na aplikácie, napíš správcovi |

Kontakt: Daniel Križan (daniel.krizan@martinus.sk).
