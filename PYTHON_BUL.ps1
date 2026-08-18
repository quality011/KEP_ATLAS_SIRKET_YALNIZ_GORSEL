function Get-KepPython {
    $adaylar = @(
        (Join-Path $PSScriptRoot ".venv\Scripts\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python314\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Python\pythoncore-3.14-64\python.exe")
    )
    foreach ($aday in $adaylar) {
        if ($aday -and (Test-Path -LiteralPath $aday)) {
            return (Resolve-Path -LiteralPath $aday).Path
        }
    }
    foreach ($komut in @("python", "python3")) {
        $bulunan = Get-Command $komut -ErrorAction SilentlyContinue
        if ($bulunan -and $bulunan.Source -and
            $bulunan.Source -notlike "*WindowsApps*") {
            return $bulunan.Source
        }
    }
    return $null
}
