' 콘솔창 깜빡임 없이 GUI 실행 (서명된 wscript -> 서명된 pythonw -> app.py)
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = fso.GetParentFolderName(WScript.ScriptFullName)
sh.Run "pythonw.exe app.py", 0, False
