@echo off
cd /d "%~dp0"
echo 勤怠管理アプリを起動します...
echo.
pip install -r requirements.txt
echo.
echo ブラウザで http://localhost:8000 を開いてください
echo スマホからは http://[このPCのIPアドレス]:8000 でアクセスできます
echo.
python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
pause
