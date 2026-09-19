# Creates "Schedule Manager v2" shortcuts on the Desktop and in the Start menu, pointing at THIS folder.
# Re-run it after moving the folder to a new location or computer.
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$ws = New-Object -ComObject WScript.Shell

$targets = @(
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "Schedule Manager v2.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Programs")) "Schedule Manager v2.lnk")
)

foreach ($lnk in $targets) {
    $s = $ws.CreateShortcut($lnk)
    $s.TargetPath = Join-Path $env:WINDIR "System32\wscript.exe"
    $s.Arguments = '"' + (Join-Path $here "Schedule Manager.vbs") + '"'
    $s.WorkingDirectory = $here
    $s.IconLocation = (Join-Path $here "app.ico") + ",0"
    $s.Description = "Schedule Manager v2"
    $s.Save()
    Write-Host "Created $lnk"
}
