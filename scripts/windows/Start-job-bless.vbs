Option Explicit
Dim shell, fs, root, command
Set shell = CreateObject("WScript.Shell")
Set fs = CreateObject("Scripting.FileSystemObject")
root = fs.GetParentFolderName(WScript.ScriptFullName)
command = Chr(34) & fs.BuildPath(root, "runtime\python\pythonw.exe") & Chr(34) & " " & Chr(34) & fs.BuildPath(root, "app\launch.py") & Chr(34)
shell.Run command, 0, False
