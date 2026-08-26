$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectRoot

python -m pip install -e ".[windows,desktop,build]"
python -m unittest discover -s tests -q
python -m PyInstaller --noconfirm --clean WeComFeedbackCollector.spec
Copy-Item -LiteralPath "$ProjectRoot\dist\WeComFeedbackCollector.exe" -Destination "$ProjectRoot\WeComFeedbackCollector.exe" -Force

Write-Host "Build completed: $ProjectRoot\WeComFeedbackCollector.exe"
