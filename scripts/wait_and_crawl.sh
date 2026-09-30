#!/usr/bin/env bash
# 等待东财 clist/get 接口解封，然后自动开始爬取行业映射。
#
# 背景：连续高频探测会把 clist/get 端点打进封禁状态（HTTP=000），
# 同域名的其他接口不受影响。本脚本每 3 分钟探测一次，一旦恢复就启动爬虫，
# 避免人工值守。
#
# 用法：bash scripts/wait_and_crawl.sh [最长等待分钟数]

set -u

cd "$(dirname "$0")/.." || exit 1

MAX_WAIT_MIN="${1:-180}"
PROBE_URL="https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=3&po=1&np=1&fltt=2&invt=2&fid=f3&fs=m:90+t:2&fields=f12,f14"
UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
LOG="logs/industry_map_build.log"

mkdir -p logs data

stamp() { date '+%H:%M:%S'; }

echo "[$(stamp)] 开始等待 clist/get 解封（最多 ${MAX_WAIT_MIN} 分钟），日志 -> $LOG"

elapsed=0
while [ "$elapsed" -lt "$MAX_WAIT_MIN" ]; do
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 12 "$PROBE_URL" \
           -H "User-Agent: $UA" -H "Referer: https://quote.eastmoney.com/")
  if [ "$code" = "200" ]; then
    echo "[$(stamp)] ✅ 接口已解封，开始爬取"
    # 爬到解封后仍要放慢节奏，避免再次触发封禁
    PYTHONIOENCODING=utf-8 python scripts/build_industry_map.py \
      --sleep 6 --jitter 2 --retries 3 >> "$LOG" 2>&1
    rc=$?
    if [ -f data/industry_map.json ]; then
      echo "[$(stamp)] ✅ 映射生成完成 (exit=$rc)"
      tail -3 "$LOG"
      exit 0
    fi
    echo "[$(stamp)] ⚠️ 爬取中断 (exit=$rc)，进度已保留，60 秒后重试"
    sleep 60
    continue
  fi
  echo "[$(stamp)] 仍被封 (HTTP=$code)，180 秒后再探"
  sleep 180
  elapsed=$((elapsed + 3))
done

echo "[$(stamp)] ❌ 等待超时，接口仍未解封。进度已保留，稍后重跑本脚本即可续爬。"
exit 1
