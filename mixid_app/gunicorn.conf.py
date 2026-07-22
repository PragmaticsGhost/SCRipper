bind = "0.0.0.0:8080"
workers = 1
worker_class = "gthread"
threads = 8
timeout = 300
graceful_timeout = 30
accesslog = "-"


def worker_exit(_server, _worker):
    from app import stop_workers

    stop_workers(timeout=5)
