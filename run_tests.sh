#!/bin/bash
# Прогон всех тестов: ./run_tests.sh
cd "$(dirname "$0")"
fail=0
for t in tests/*.py; do
  out=$(.venv/bin/python "$t" 2>&1 | tail -1)
  case "$out" in *✅*) printf "%-18s %s\n" "$(basename $t)" "$out";; *) printf "%-18s ❌ %s\n" "$(basename $t)" "$out"; fail=1;; esac
done
exit $fail
