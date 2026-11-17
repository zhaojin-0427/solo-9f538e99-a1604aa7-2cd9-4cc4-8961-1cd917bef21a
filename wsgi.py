"""本地/容器统一启动入口：gunicorn 通过 wsgi:app 加载。"""

import os

from app import create_app

app = create_app(
    db_path=os.environ.get("APP_DB_PATH", os.path.join(os.getcwd(), "data", "snp.db"))
)

if __name__ == "__main__":
    # 无 gunicorn 的开发场景（python wsgi.py），生产由 gunicorn 托管
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
