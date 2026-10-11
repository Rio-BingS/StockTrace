"""Gunicorn config for StockTrace on low-spec VPS (1H2G).

唯一一份配置（REL-10）：deploy/ 下的副本已删除，systemd 单元直接引用本文件。
systemd already drops privileges with User=www — do NOT set user/group here.
"""
import os

# Self-contained: works whether ExecStart passes the app or only -c
# chdir 取本文件所在目录（backend/），不再硬编码 /opt/stocktrace
chdir = os.path.dirname(os.path.abspath(__file__))
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
# 显式关闭 preload：应用在 master 里预加载后 fork，会把进程内状态（缓存、线程池、
# AI 并发信号量、可能的调度线程）复制进每个 worker，线程不会随 fork 存活，状态各自漂移。
# 调度器在独立进程 run_scheduler 中运行；不要改成 True（REL-10）
preload_app = False
