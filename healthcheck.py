import os, time, sys
try:
    mtime = os.path.getmtime("/tmp/healthcheck")
    if time.time() - mtime < 300:
        sys.exit(0)
except OSError:
    pass
sys.exit(1)
