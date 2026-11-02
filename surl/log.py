import time


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def short(s, n=160):
    s = str(s or "").replace("\n", " ")
    return s if len(s) <= n else s[:n - 1] + "…"
