$ErrorActionPreference = "Stop"
$klasor = Split-Path -Parent $MyInvocation.MyCommand.Path
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
. (Join-Path $klasor "PYTHON_BUL.ps1")

Write-Host "============================================================"
Write-Host "KEP ATLAS SIRKET YALNIZ GORSEL BOTU - KURULUM"
Write-Host "============================================================"

$chromeAdaylari = New-Object System.Collections.Generic.List[string]
foreach ($kok in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA)) {
    if (-not $kok) { continue }
    $aday = Join-Path $kok "Google\Chrome\Application\chrome.exe"
    if (Test-Path -LiteralPath $aday) { $chromeAdaylari.Add($aday) }
}
if ($chromeAdaylari.Count -eq 0) {
    Write-Warning "Google Chrome bulunamadi. Botu baslatmadan once Chrome kurun."
}

$temelPython = Get-KepPython
if (-not $temelPython) {
    Write-Host "Python bulunamadi." -ForegroundColor Red
    Write-Host "1) https://www.python.org/downloads/windows/ adresinden 64-bit Python 3.11 veya ustunu kurun."
    Write-Host "2) Kurulumda 'Add python.exe to PATH' kutusunu secin."
    Write-Host "3) Bu kurulum dosyasini yeniden acin."
    exit 2
}

& $temelPython -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "Python 3.11 veya ustu gereklidir."
}

$venvPython = Join-Path $klasor ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "Bu klasore ozel Python ortami olusturuluyor..."
    & $temelPython -m venv (Join-Path $klasor ".venv")
    if ($LASTEXITCODE -ne 0) { throw "Python sanal ortami olusturulamadi." }
}

Write-Host "Gerekli paketler kuruluyor/guncelleniyor..."
& $venvPython -m pip install --disable-pip-version-check --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip guncellenemedi." }
& $venvPython -m pip install --disable-pip-version-check -r (Join-Path $klasor "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Python paketleri kurulamadi." }

Write-Host "Kurulum ve kodlar test ediliyor..."
& $venvPython -c "import selenium, requests, PIL, pydantic; print('Python paketleri hazir.')"
if ($LASTEXITCODE -ne 0) { throw "Gerekli Python paketlerinden biri yuklenemedi." }
$kodlar = Get-ChildItem -LiteralPath $klasor -Filter "*.py" -File | ForEach-Object FullName
& $venvPython -m py_compile $kodlar
if ($LASTEXITCODE -ne 0) { throw "Python kod denetimi basarisiz." }
Push-Location $klasor
try {
    & $venvPython -m unittest test_yalniz_gorsel_botu.py
    if ($LASTEXITCODE -ne 0) { throw "Yalniz-gorsel testleri basarisiz." }
}
finally { Pop-Location }

Write-Host ""
Write-Host "KURULUM BASARILI" -ForegroundColor Green
Write-Host "Simdi 2_ID_LISTESINI_CALISTIR.cmd dosyasini acabilirsiniz."
Write-Host "Sifreler ve API anahtarlari bu pakete kaydedilmez."
exit 0
