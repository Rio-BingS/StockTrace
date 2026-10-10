"""Gunicorn config for StockTrace on low-spec VPS (1H2G).

systemd already drops privileges with User=www — do NOT set user/group here.
"""
import multiprocessing
import os

# Self-contained: works whether ExecStart passes the app or only -c
chdir = '/opt/stocktrace/backend'
wsgi_app = 'StockTrace.wsgi:application'

bind = '127.0.0.1:8000'
# 1H2G: single worker saves RAM; threads handle concurrent short requests
workers = int(os.environ.get('GUNICORN_WORKERS', '1'))
threads = int(os.environ.get('GUNICORN_THREADS', '2'))
worker_class = 'gthread'
timeout = 120
graceful_timeout = 30
keepalive = 5
accesslog = '-'
errorlog = '-'
loglevel = os.environ.get('GUNICORN_LOGLEVEL', 'info')
# worker 回收：原 500 在前端全天轮询下约 3 小时回收一次，会杀掉进行中的后台拉取线程。
# 调度器已移到独立进程（run_scheduler），这里只保留低频的内存泄漏兜底；0 表示不回收。
max_requests = int(os.environ.get('GUNICORN_MAX_REQUESTS', '20000'))
max_requests_jitter = int(os.environ.get('GUNICORN_MAX_REQUESTS_JITTER', '1000'))
