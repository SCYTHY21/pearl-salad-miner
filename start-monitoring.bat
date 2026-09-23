@echo off
rem Pornește monitorizarea + dashboard-ul și balansorul de prioritate, fiecare în fereastra lui.
rem Dublu-click după fiecare restart al PC-ului. Dashboard: http://localhost:8787/
cd /d "%~dp0"
start "pearl-monitor (dashboard http://localhost:8787)" cmd /k python monitor.py --serve 8787 --interval 120
timeout /t 3 /nobreak >nul
start "pearl-balancer" cmd /k python priority_balancer.py --interval 300
echo Pornit. Dashboard: http://localhost:8787/
