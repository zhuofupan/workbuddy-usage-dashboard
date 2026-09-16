' WorkBuddy usage dashboard - windowless launcher
'
' Double-click this file:
'   1) already running -> just open the browser (no second server)
'   2) not running     -> start python.exe with a HIDDEN window (0 = hidden)
'   3) poll until the server really answers, then open the browser
'   4) if it never comes up -> run --diagnose, which shows a dialog with
'      Chinese troubleshooting text (kept in Python on purpose)
'
' NOTE 1: keep this file pure ASCII + CRLF. wscript reads .vbs source as
'   ANSI/UTF-16, so UTF-8 Chinese here would render as mojibake. All
'   user-facing Chinese lives in server.py (--diagnose).
' NOTE 2: quotes are built with Chr(34) instead of counting " quotes.
'   The """" & x & """ """ & y idiom is famously easy to get wrong and
'   cannot be tested here, so we avoid it entirely.
' NOTE 3: we launch python.exe, not pythonw.exe. Measured on this machine,
'   a pythonw.exe process listened fine but nothing could connect to it.

Option Explicit

Dim shell, fso, q, base, py, url
Dim i, ok, rc

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
q = Chr(34)
base = fso.GetParentFolderName(WScript.ScriptFullName)
url = "http://127.0.0.1:8791/"

' Locate a Python interpreter (see FindPython below). We do NOT hardcode an
' absolute path: that would leak one machine's user name into the repo and
' break for everybody else.
py = FindPython()

' Guard: the folder may have been moved or renamed. Message is ASCII on purpose.
If Not fso.FileExists(base & "\server.py") Then
  MsgBox "server.py not found next to launch.vbs." & vbCrLf & _
         "Expected: " & base & "\server.py", 16, "WorkBuddy Usage Dashboard"
  WScript.Quit 1
End If

' Already running? Then just open the browser and leave.
If ProbeOK() Then
  shell.Run url, 1, False
  WScript.Quit 0
End If

' 0 = hidden window, False = do not wait for it to exit.
shell.CurrentDirectory = base
shell.Run SrvCmd("--port 8791 --no-browser"), 0, False

' Local loopback is flaky on this box, so retry for a while before giving up.
ok = False
For i = 1 To 50
  WScript.Sleep 500
  If ProbeOK() Then
    ok = True
    Exit For
  End If
Next

If ok Then
  shell.Run url, 1, False
Else
  ' Let Python show the Chinese dialog (wscript cannot render UTF-8 source).
  ' If even that cannot run (python missing / blocked), fall back to a native
  ' ASCII MsgBox so the user is never left staring at nothing.
  Dim diagOk
  diagOk = False
  On Error Resume Next
  rc = shell.Run(SrvCmd("--diagnose --port 8791"), 0, True)
  If Err.Number = 0 Then
    diagOk = True
  End If
  On Error GoTo 0
  If Not diagOk Then
    MsgBox "Could not start the dashboard." & vbCrLf & vbCrLf & _
           "The Python fallback could not run either, so Python may be" & vbCrLf & _
           "missing or blocked." & vbCrLf & vbCrLf & _
           "Fallback: run start.bat in this folder instead -" & vbCrLf & _
           "it keeps a console window open and shows the real error." & vbCrLf & vbCrLf & _
           base, 16, "WorkBuddy Usage Dashboard"
  End If
End If

WScript.Quit 0


Function FindPython()
  ' Prefer the interpreter WorkBuddy ships, so a plain double-click works with
  ' no Python on PATH. Otherwise return the bare name "python" and let cmd
  ' resolve it via PATH.
  Dim dir, d, cand
  FindPython = ""
  dir = shell.ExpandEnvironmentStrings("%USERPROFILE%") & "\.workbuddy\binaries\python\versions"
  If fso.FolderExists(dir) Then
    For Each d In fso.GetFolder(dir).SubFolders
      cand = d.Path & "\python.exe"
      If fso.FileExists(cand) Then
        FindPython = cand
        Exit Function
      End If
    Next
  End If
  FindPython = "python"
End Function


Function SrvCmd(options)
  SrvCmd = q & py & q & " " & q & base & "\server.py" & q & " " & options
End Function


Function ProbeOK()
  Dim code
  ProbeOK = False
  On Error Resume Next
  code = shell.Run(SrvCmd("--probe --port 8791"), 0, True)
  If Err.Number = 0 Then
    If code = 0 Then ProbeOK = True
  End If
  On Error GoTo 0
End Function
