#!/bin/sh
# Run every colocated *_test.py. No pytest: the repo's convention is
# self-contained test files with a main() and asyncio.run(_run()) inside the
# test body, and there is no conftest.py anywhere.
set -e
cd "$(dirname "$0")"
fail=0
for f in $(find src -name '*_test.py' | sort); do
  printf '%-58s ' "$f"
  if out=$(PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. python3 "$f" 2>&1); then
    echo "$out" | tail -1
  else
    echo "FAILED"; echo "$out" | grep -E '^(FAIL|ERROR)' || true
    fail=1
  fi
done
exit $fail
