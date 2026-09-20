' Starts the tablet display in the background (no window) and shows the address to type on the tablet.
' Pass "quiet" to skip the pop-up, which is what the start-with-Windows shortcut does.
Option Explicit

Dim sh, fso, here, pyw, status, quiet, i, msg
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = here
pyw = here & "\.venv\Scripts\pythonw.exe"
status = here & "\tablet_status.txt"
quiet = (WScript.Arguments.Count > 0 And LCase(WScript.Arguments(0)) = "quiet")

If Not fso.FileExists(pyw) Then
    If Not quiet Then MsgBox "Open Schedule Manager once first so its Python environment gets built, then try again.", 48, "Tablet display"
    WScript.Quit 1
End If

If fso.FileExists(status) Then fso.DeleteFile status, True
sh.Run """" & pyw & """ """ & here & "\tablet_server.py""", 0, False

For i = 1 To 40
    If fso.FileExists(status) Then Exit For
    WScript.Sleep 250
Next

If Not quiet Then
    If fso.FileExists(status) Then
        WScript.Sleep 200
        msg = fso.OpenTextFile(status, 1).ReadAll
        MsgBox msg & vbCrLf & vbCrLf & "It keeps running in the background. Use ""Stop Tablet Display"" to turn it off.", 64, "Tablet display"
    Else
        MsgBox "The tablet display did not start. Details may be in tablet.log in this folder.", 48, "Tablet display"
    End If
End If
