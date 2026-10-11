' Jalankan file .bat tanpa jendela console (background).
' Pemakaian: wscript.exe //B //Nologo run-hidden.vbs "D:\path\ke\script.bat"
If WScript.Arguments.Count = 0 Then WScript.Quit 1
Set sh = CreateObject("WScript.Shell")
sh.Run "cmd.exe /c """ & WScript.Arguments(0) & """", 0, False
