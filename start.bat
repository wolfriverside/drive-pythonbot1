@echo off
chcp 65001 >nul
title LearnPythonBot 啟動器
echo ========================================================================
echo   LearnPythonBot - 環境檢查與一鍵啟動腳本
echo ========================================================================
echo.

:: 1. 檢查 Python 環境
echo [1/4] 檢查 Python 環境...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [錯誤] 找不到 Python！請先安裝 Python 3.10+ 並將其加入系統 PATH。
    echo 下載網址：https://www.python.org/
    echo.
    pause
    exit /b 1
)
for /f "tokens=*" %%v in ('python --version 2^>^&1') do echo 已偵測到：%%v
echo.

:: 2. 檢查 Python 依賴套件
echo [2/4] 檢查必要 Python 套件 (requirements.txt)...
python -c "import gradio, ollama, pypdf, docx, bs4, requests" >nul 2>&1
if %errorlevel% neq 0 (
    echo 偵測到部分套件尚未安裝，正在自動執行 pip install...
    pip install -r requirements.txt
    if %errorlevel% neq 0 (
        echo [錯誤] 套件安裝失敗，請手動檢查網路連線或 pip 設定。
        pause
        exit /b 1
    )
) else (
    echo Python 依賴套件皆已就緒。
)
echo.

:: 3. 檢查 Ollama 本地服務
echo [3/4] 檢查 Ollama 服務與模型...
where ollama >nul 2>&1
if %errorlevel% neq 0 (
    echo [警告] 尚未安裝 Ollama 執行檔！
    echo 請前往官網下載並安裝：https://ollama.com
    echo 安裝後請在終端機拉取必要模型：
    echo   ollama pull qwen3
    echo   ollama pull nomic-embed-text
    echo.
) else (
    echo Ollama 執行檔已就緒。
    :: 執行 Python 診斷檢查服務是否連線
    python -c "from core.health_check import check_ollama_status; res = check_ollama_status(); print('Ollama 狀態: ' + res['summary'])" 2>nul
)
echo.

:: 4. 啟動應用程式
echo [4/4] 正在啟動 LearnPythonBot Web 介面 (app.py)...
echo.
python app.py

pause
