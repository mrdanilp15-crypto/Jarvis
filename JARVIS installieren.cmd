@echo off
rem JARVIS als Windows-Programm installieren oder aktualisieren - ohne Docker. Einfach doppelklicken.
rem Optional: "JARVIS installieren.cmd" -Browser chrome   bzw.   -Stop   bzw.   -Uninstall
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\windows\jarvis-install.ps1" %*
