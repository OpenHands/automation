#!/bin/bash
# Times preset setup.sh (prompt preset) on a fresh work dir, warm uv cache, SDK 1.62.0.
export AUTOMATION_API_URL=http://127.0.0.1:18668
variant=$1; n=$2
d=$(mktemp -d /tmp/run-$variant-XXXX); cd $d
s=$(date +%s.%N)
bash ~/work/b668/setup_$variant.sh > setup.log 2>&1; rc=$?
e=$(date +%s.%N)
s2=$(date +%s.%N)
OPENHANDS_SUPPRESS_BANNER=1 .venv/bin/python -c 'import openhands.sdk, openhands.tools, openhands.workspace' ; rc2=$?
e2=$(date +%s.%N)
printf "%-5s run %d  setup.sh exit=%d  setup=%6.1fs   entrypoint SDK import (main.py)=%5.1fs   setup+import=%6.1fs\n" $variant $n $rc $(python3 -c "print($e-$s)") $(python3 -c "print($e2-$s2)") $(python3 -c "print($e2-$s)")
rm -rf $d
