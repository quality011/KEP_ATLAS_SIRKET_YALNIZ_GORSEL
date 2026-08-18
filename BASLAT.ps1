$ErrorActionPreference = "Stop"
$klasor = Split-Path -Parent $MyInvocation.MyCommand.Path
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$python = Join-Path $klasor ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Kurulum bulunamadi. Once 1_KURULUM_YAP.cmd dosyasini acin."
}
& $python -c "import selenium, requests, PIL, pydantic"
if ($LASTEXITCODE -ne 0) {
    throw "Python paketleri eksik. 1_KURULUM_YAP.cmd dosyasini yeniden calistirin."
}

$program = Join-Path $klasor "yalniz_gorsel_botu.py"
$logKlasoru = Join-Path $klasor "logs"
New-Item -ItemType Directory -Force -Path $logKlasoru | Out-Null
$log = Join-Path $logKlasoru ("yalniz_gorsel_" + (Get-Date -Format "yyyyMMdd_HHmmss") + ".log")
Start-Transcript -LiteralPath $log -Force | Out-Null

Write-Host "============================================================"
Write-Host "KEP ATLAS - SIRKET YALNIZ GORSEL BOTU"
Write-Host "DOGRUDAN 6 HANELI ID LISTESIYLE CALISIR"
Write-Host "MERKEZI KUMANDA VE OPENAI KULLANMAZ"
Write-Host "============================================================"
Write-Host ""
Write-Host "6 haneli Kep Atlas ID'lerini yapistirin."
Write-Host "Boslukla, virgulle veya alt alta girebilirsiniz."
Write-Host "Tum ID'leri girdikten sonra BOS SATIRDA bir kez daha Enter'a basin."

$satirlar = New-Object System.Collections.Generic.List[string]
while ($true) {
    $satir = Read-Host "ID"
    if ([string]::IsNullOrWhiteSpace($satir)) { break }
    $satirlar.Add($satir)
}

$hamIdler = $satirlar -join " "
$benzersiz = New-Object System.Collections.Generic.List[string]
$gorulen = @{}
foreach ($eslesme in [regex]::Matches($hamIdler, '(?<!\d)\d{6}(?!\d)')) {
    $id = $eslesme.Value
    if (-not $gorulen.ContainsKey($id)) {
        $gorulen[$id] = $true
        $benzersiz.Add($id)
    }
}
if ($benzersiz.Count -eq 0) {
    throw "Gecerli 6 haneli Kep Atlas ID'si bulunamadi."
}
$idler = $benzersiz -join " "

Write-Host ""
Write-Host ("Hazirlanan benzersiz ID sayisi: " + $benzersiz.Count) -ForegroundColor Cyan
Write-Host "Ayni ID listesini baska bir bilgisayarda ayni anda calistirmayin."
$onay = Read-Host "Canli yuklemeyi baslatmak icin BASLAT yazin"
if ($onay -cne "BASLAT") {
    try { Stop-Transcript | Out-Null } catch {}
    exit 0
}

$epostaEklendi = $false
$sifreEklendi = $false
$exaEklendi = $false
$sifreBstr = [IntPtr]::Zero
$exaBstr = [IntPtr]::Zero
try {
    if (-not $env:KEP_ATLAS_EMAIL) {
        $env:KEP_ATLAS_EMAIL = (Read-Host "Kep Atlas e-posta").Trim()
        if (-not $env:KEP_ATLAS_EMAIL) { throw "Kep Atlas e-posta zorunludur." }
        $epostaEklendi = $true
    }
    if (-not $env:KEP_ATLAS_PASSWORD) {
        $guvenli = Read-Host "Kep Atlas sifre (ekranda gorunmez)" -AsSecureString
        $sifreBstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($guvenli)
        $env:KEP_ATLAS_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($sifreBstr)
        if (-not $env:KEP_ATLAS_PASSWORD) { throw "Kep Atlas sifre zorunludur." }
        $sifreEklendi = $true
    }
    if (-not $env:EXA_API_KEY) {
        $guvenliExa = Read-Host "Exa API anahtari (ekranda gorunmez)" -AsSecureString
        $exaBstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($guvenliExa)
        $exaMetni = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($exaBstr)
        if (-not $exaMetni) { throw "Exa API anahtari zorunludur." }
        $env:EXA_API_KEY = $exaMetni
        $exaEklendi = $true
    }

    $env:KEP_CHROME_SEKME_LIMITI = "2"
    & $python $program --idler $idler --gorsel-isci 3 --gorsel-parti 8 `
        --kaynak-tur 4 --gorsel-hata-deneme 4 --min-genislik 600 `
        --min-yukseklik 400 --kabul-esigi 0.78 --onayla
    $kod = $LASTEXITCODE
    if ($kod -ne 0) { throw "Python sureci $kod koduyla durdu. Log: $log" }
}
finally {
    try { Stop-Transcript | Out-Null } catch {}
    if ($sifreBstr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($sifreBstr) }
    if ($exaBstr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($exaBstr) }
    if ($epostaEklendi) { Remove-Item Env:KEP_ATLAS_EMAIL -ErrorAction SilentlyContinue }
    if ($sifreEklendi) { Remove-Item Env:KEP_ATLAS_PASSWORD -ErrorAction SilentlyContinue }
    if ($exaEklendi) { Remove-Item Env:EXA_API_KEY -ErrorAction SilentlyContinue }
}

Write-Host "Islem tamamlandi." -ForegroundColor Green
Write-Host "Log: $log"
exit 0
