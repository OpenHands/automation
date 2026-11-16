#!/bin/bash
# repro_675.sh <repo_dir>: two independent local installations (own agent-server,
# automation service, SQLite DB) each dispatch a run; the run's entrypoint records
# AUTOMATION_USER_ID, the value the preset runners pass as the conversation user_id
# (agent-server telemetry uses it verbatim as the PostHog distinct_id).
REPO=$1; L=$HOME/work/local
rm -rf /tmp/675-A /tmp/675-B
seen() { cat $1/workspace/observed.txt | tail -1; }
$L/start_install.sh $REPO /tmp/675-A 18001 18101 >/dev/null && $L/probe_identity.sh 18001 >/dev/null
echo "installation A, run 1:              AUTOMATION_USER_ID=$(seen /tmp/675-A)"
$L/start_install.sh $REPO /tmp/675-B 18002 18102 >/dev/null && $L/probe_identity.sh 18002 >/dev/null
echo "installation B, run 1:              AUTOMATION_USER_ID=$(seen /tmp/675-B)"
$L/stop_install.sh /tmp/675-A; $L/start_install.sh $REPO /tmp/675-A 18001 18101 >/dev/null && $L/probe_identity.sh 18001 >/dev/null
echo "installation A, after restart:      AUTOMATION_USER_ID=$(seen /tmp/675-A)"
$L/stop_install.sh /tmp/675-A; $L/stop_install.sh /tmp/675-B
A1=$(head -1 /tmp/675-A/workspace/observed.txt); A2=$(tail -1 /tmp/675-A/workspace/observed.txt); B=$(seen /tmp/675-B)
SHARED=c1a96d89-eae7-53bd-86ad-07f26266402b
echo
if [ "$A1" = "$B" ]; then echo "BUG: both installations attribute their conversations to the same identity"; else echo "OK: installations A and B have different identities"; fi
if [ "$A1" = "$SHARED" ] || [ "$B" = "$SHARED" ]; then echo "BUG: the shared local placeholder $SHARED is forwarded"; else echo "OK: the shared placeholder $SHARED is not forwarded"; fi
if [ "$A1" = "$A2" ]; then echo "OK: installation A keeps its identity across a restart"; else echo "BUG: installation A's identity changed on restart"; fi
