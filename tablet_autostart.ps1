# Turns "start the tablet display when I sign in to Windows" on, or off with -Remove.
param([switch]$Remove)
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$lnk = Join-Path ([Environment]::GetFolderPath("Startup")) "Tablet Display.lnk"

if ($Remove) {
    if (Test-Path $lnk) { Remove-Item $lnk; Write-Host "Removed. The tablet display will no longer start with Windows." }
    else { Write-Host "It was not set to start with Windows." }
    exit
}

$s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
$s.TargetPath = Join-Path $env:WINDIR "System32\wscript.exe"
$s.Arguments = '"' + (Join-Path $here "Tablet Display.vbs") + '" quiet'
$s.WorkingDirectory = $here
$s.Save()
Write-Host "Done. The tablet display will start in the background each time you sign in to Windows."
