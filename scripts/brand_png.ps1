# Renders the Internest PNG assets from the SVG sources with headless Microsoft Edge (no online service, no new
# dependency). Run after scripts/brand_assets.py:   powershell -File scripts/brand_png.ps1
#   static/img/brand/og-image.png          1200x630 social card (Open Graph / Twitter)
#   static/img/brand/icon-512.png          app icon master (mark on the dark brand tile)
#   static/img/brand/icon-192.png, apple-touch-icon.png (180), favicon-32.png   downscaled from icon-512
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$brand = Join-Path $root "internest_core\static\img\brand"
$work = Join-Path $env:TEMP "internest-brand-png"
New-Item -ItemType Directory -Force $work | Out-Null
$edge = @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe", "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe") |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $edge) { throw "Microsoft Edge not found" }
$src = ($brand -replace '\\', '/')

function Render($name, $html, $w, $h) {
    $page = Join-Path $work "$name.html"
    Set-Content -Encoding utf8 $page $html
    $out = Join-Path $brand "$name.png"
    $p = Start-Process -FilePath $edge -PassThru -Wait -ArgumentList @("--headless=new", "--disable-gpu", "--hide-scrollbars",
        "--no-first-run", "--allow-file-access-from-files", "--user-data-dir=$work\profile", "--window-size=$w,$h",
        "--screenshot=$out", ("file:///" + ($page -replace '\\', '/')))
    if ($p.ExitCode -ne 0 -or -not (Test-Path $out)) { throw "render failed: $name" }
    return $out
}

$bg = "background:radial-gradient(60% 80% at 15% 10%,rgba(108,76,255,.55),transparent 60%),radial-gradient(50% 70% at 95% 100%,rgba(255,106,61,.35),transparent 60%),#07071a"
Render "og-image" @"
<!doctype html><html><body style="margin:0;width:1200px;height:630px;$bg;display:grid;place-items:center;font-family:'Segoe UI',system-ui,sans-serif">
<div style="display:grid;justify-items:center;gap:30px">
<img src="file:///$src/internest-mark.svg" style="width:230px">
<img src="file:///$src/wordmark-dark.svg" style="height:120px">
<div style="color:#ecebff;font-size:34px;letter-spacing:.2em;font-weight:600">Learn <span style="color:#ff8a1f;padding:0 14px">&bull;</span> Grow <span style="color:#ff8a1f;padding:0 14px">&bull;</span> Get Hired</div>
</div></body></html>
"@ 1200 630 | Out-Null

$master = Render "icon-512" @"
<!doctype html><html><body style="margin:0;width:512px;height:512px;$bg;display:grid;place-items:center">
<img src="file:///$src/internest-mark.svg" style="width:380px;transform:translateY(10px)">
</body></html>
"@ 512 512

Add-Type -AssemblyName System.Drawing
$img = [System.Drawing.Image]::FromFile($master)
foreach ($t in @(@("icon-192", 192), @("apple-touch-icon", 180), @("favicon-32", 32))) {
    $bmp = New-Object System.Drawing.Bitmap $t[1], $t[1]
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
    $g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    $g.DrawImage($img, 0, 0, $t[1], $t[1])
    $bmp.Save((Join-Path $brand "$($t[0]).png"), [System.Drawing.Imaging.ImageFormat]::Png)
    $g.Dispose(); $bmp.Dispose()
}
$img.Dispose()
Get-ChildItem $brand -Filter *.png | ForEach-Object { "{0,-22} {1,6:N0} KB" -f $_.Name, ($_.Length / 1KB) }
