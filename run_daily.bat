@echo off
rem Daily JobScout run for Task Scheduler: filter the published dataset, log, open the report.
cd /d "%~dp0"
if not exist output mkdir output
C:\Python314\python.exe -m jobscout --open >> output\jobscout.log 2>&1
