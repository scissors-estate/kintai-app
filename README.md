# 勤怠管理アプリ

FastAPI + SQLite で作った勤怠管理Webアプリ。

## ローカル起動

```
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

ブラウザで http://localhost:8000

## テスト用アカウント

- 管理者：admin / admin123
- 従業員：tanaka / pass123
