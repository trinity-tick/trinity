@echo off
set PYTHONPATH=C:\Users\Administrator\Trinity
start /B "" "C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe" -E -m trinity.api.server --port 8001
