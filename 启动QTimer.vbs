Option Explicit
Dim shell, fso, base, python, script
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)
python = fso.BuildPath(base, ".venv\Scripts\pythonw.exe")
script = fso.BuildPath(base, "main.py")
If Not fso.FileExists(python) Then
    MsgBox "Create .venv and install requirements.txt first.", 16, "QTimer"
    WScript.Quit 1
End If
shell.CurrentDirectory = base
shell.Run Chr(34) & python & Chr(34) & " " & Chr(34) & script & Chr(34), 1, False
