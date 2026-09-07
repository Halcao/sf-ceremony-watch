#!/usr/bin/env bash
# 查看 sf-ceremony-watch 这个 GitHub Actions 监控的运行记录/日志
# 用法:
#   ./watch-log.sh            列出最近几次运行状态
#   ./watch-log.sh latest     看最近一次运行的完整日志
#   ./watch-log.sh <run-id>   看指定 run 的完整日志
#   ./watch-log.sh follow     手动触发一次并实时跟随日志

set -euo pipefail
export PATH="$HOME/bin:$PATH"
export GH_PAGER=cat   # 强制纯文本输出到 stdout，不进 less/vim 等交互分页器
export PAGER=cat
cd "$(dirname "$0")"

cmd="${1:-list}"

case "$cmd" in
  list)
    gh run list --workflow=watch.yml --limit 15
    ;;
  latest)
    run_id=$(gh run list --workflow=watch.yml --limit 1 --json databaseId --jq '.[0].databaseId')
    gh run view "$run_id" --log
    ;;
  result)
    # 只看最近一次运行里 watch.py 自己打印的内容（可用日、命中情况、推送结果）
    run_id=$(gh run list --workflow=watch.yml --limit 1 --json databaseId --jq '.[0].databaseId')
    gh run view "$run_id" --log | grep -F "Run watcher" \
      | sed -E 's/^[^\t]+\tRun watcher\t[^Z]+Z //' \
      | grep -E '^(\[[0-9]{4}-|  [0-9]{4}-|    )'
    ;;
  follow)
    gh workflow run watch.yml
    sleep 5
    run_id=$(gh run list --workflow=watch.yml --limit 1 --json databaseId --jq '.[0].databaseId')
    gh run watch "$run_id" --exit-status
    gh run view "$run_id" --log
    ;;
  tail)
    # 持续监控：每次 Actions 跑完一轮，就打印一次这轮的结果，类似 tail -f
    # 单次网络抖动（gh 请求失败）不应该把整个监控进程带崩，所以这里不用 set -e，全部手动兜底。
    last=""
    while true; do
      run_id=$(gh run list --workflow=watch.yml --status completed --limit 1 --json databaseId --jq '.[0].databaseId' 2>/dev/null) || run_id=""
      if [ -n "$run_id" ] && [ "$run_id" != "$last" ]; then
        last="$run_id"
        echo "===== run $run_id ====="
        gh run view "$run_id" --log 2>/dev/null | grep -F "Run watcher" \
          | sed -E 's/^[^\t]+\tRun watcher\t[^Z]+Z //' \
          | grep -E '^(\[[0-9]{4}-|  [0-9]{4}-|    )' || echo "(本轮日志获取失败，下次轮询重试)"
      fi
      sleep 30
    done
    ;;
  *)
    gh run view "$cmd" --log
    ;;
esac
