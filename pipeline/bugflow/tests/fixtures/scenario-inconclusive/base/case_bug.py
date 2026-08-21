import time
time.sleep(8)  # 超过 manifest.timeout_s——制造"超时=不可判定"证据
print("REPRO_OUTCOME: fail")
raise SystemExit(1)
