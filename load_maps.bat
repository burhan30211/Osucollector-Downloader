@echo off

powershell -Command "Get-ChildItem '.\osz_downloads\*.osz' | ForEach-Object { Start-Process $_.FullName; Start-Sleep -Milliseconds 1000 }"

pause