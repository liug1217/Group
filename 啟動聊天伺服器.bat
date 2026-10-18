@echo off
chcp 65001 >nul
title ESN 聊天伺服器
rem 雙擊這個檔案啟動聊天伺服器。伺服器停掉會在 5 秒後自動重開；要關掉就直接關這個視窗。
rem 只開給這台電腦（127.0.0.1），外面的人一律透過 Tailscale Funnel 的 https 網址連進來。
rem 電腦要設定成不自動睡眠，睡著時聊天就停了（網頁本身照常可以用）。

cd /d "%~dp0"
set ESN_HOST=127.0.0.1
set ESN_PORT=8800
rem 聊天資料放在 OneDrive 外面，避免一直觸發同步
set ESN_DATA_DIR=C:\ESNData

:loop
py -u server.py
echo.
echo 伺服器停止了，5 秒後自動重開...（要關掉請直接關閉這個視窗）
timeout /t 5 /nobreak >nul
goto loop
