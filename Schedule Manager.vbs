' Launches Schedule Manager with no console window.
' First run on a computer: builds a private Python environment inside this folder (needs internet + Python).
Option Explicit

Dim sh, fso, here, py, pyw, rc
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = here
py = here & "\.venv\Scripts\python.exe"
pyw = here & "\.venv\Scripts\pythonw.exe"

Function Healthy()
    Healthy = False
    If fso.FileExists(py) Then
        rc = sh.Run("""" & py & """ -c ""import customtkinter, PIL, googleapiclient, google_auth_oauthlib, tzdata""", 0, True)
        Healthy = (rc = 0)
    End If
End Function

If Not Healthy() Then
    sh.Run """" & here & "\setup.bat""", 1, True
    If Not Healthy() Then
        MsgBox "Setup did not finish, so Schedule Manager can't start yet." & vbCrLf & vbCrLf & _
               "Make sure Python 3.10+ is installed and you're online, then open this again.", 48, "Schedule Manager"
        WScript.Quit 1
    End If
End If

sh.Run """" & pyw & """ """ & here & "\schedule_gui.py""", 1, False
