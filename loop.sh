#!/usr/bin/env bash
# Keep one watcher checking for a while inside a single GitHub Actions run.
#
# GitHub's own schedule only starts runs roughly once an hour in practice,
# whatever the cron line says, so each run loops on its own and saves its
# state to the repo after every check that changed it. A run that gets cut
# off therefore loses nothing.
#
# usage: loop.sh MINUTES INTERVAL_SECONDS STATE_FILE command...
set -u
minutes=$1; interval=$2; state=$3; shift 3
end=$((SECONDS + minutes * 60))

git config user.name  "cinema-bot"
git config user.email "cinema-bot@users.noreply.github.com"

save_state() {
  [ "${DRY_RUN:-0}" = "1" ] && return 0
  if git add "$state" 2>/dev/null && ! git diff --cached --quiet; then
    git commit -q -m "$state $(date -u +%FT%TZ)"
  fi
  # Also covers a commit left over from an earlier push that failed.
  [ -z "$(git rev-list origin/main..HEAD 2>/dev/null)" ] && return 0
  for i in 1 2 3 4 5; do
    if git pull -q --rebase --autostash -X ours && git push -q; then
      return 0
    fi
    sleep $((i * 3))
  done
  echo "!! could not push $state; will try again after the next check"
}

while :; do
  "$@" || echo "!! check failed (exit $?); continuing"
  save_state
  [ $((SECONDS + interval)) -ge "$end" ] && break
  sleep "$interval"
done
