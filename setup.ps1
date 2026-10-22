# Martinus Qlik most pre ChatGPT/Codex (Windows) — instalacia jednym prikazom v PowerShelli:
#
#   irm https://raw.githubusercontent.com/DanoMartinus/martinus-qlik-codex/main/setup.ps1 | iex
#
# Co robi: vytvori %USERPROFILE%\.martinus-qlik, stiahne don martinus_qlik.py, najde Python
# z ChatGPT/Codex appky (alebo bezny python) a spusti `martinus_qlik.py setup` (Microsoft prihlasenie,
# zapis do ~/.codex/config.toml, overenie). Nic ine neinstaluje. Spustat v PowerShelli (Win+R -> powershell),
# NIE cez Codex (jeho sandbox nema internet ani zapis mimo pracovneho priecinka).
& {
    $ErrorActionPreference = 'Stop'
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $userHome = $env:USERPROFILE
    $dir = Join-Path $userHome '.martinus-qlik'
    New-Item -ItemType Directory -Force $dir | Out-Null
    $script = Join-Path $dir 'martinus_qlik.py'
    Write-Host "Stahujem martinus_qlik.py do $dir ..."
    Invoke-WebRequest -UseBasicParsing 'https://raw.githubusercontent.com/DanoMartinus/martinus-qlik-codex/main/martinus_qlik.py' -OutFile $script

    $codexPy = Join-Path $userHome '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    $py = $null
    if (Test-Path $codexPy) {
        $py = $codexPy
    } else {
        $cmd = Get-Command python -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source -notlike '*\WindowsApps\*') { $py = $cmd.Source }
    }
    if (-not $py) {
        Write-Host ''
        Write-Host 'CHYBA: Nenasiel som Python z ChatGPT/Codex appky:'
        Write-Host "  $codexPy"
        Write-Host 'Otvor v ChatGPT appke cast Codex, pockaj 2 minuty (stiahne si svoje sucasti), appku uplne zavri'
        Write-Host '(pravy klik na ikonu pri hodinach -> Quit) a spusti tento prikaz znova.'
        return
    }
    Write-Host "Python: $py"
    & $py $script setup
}
