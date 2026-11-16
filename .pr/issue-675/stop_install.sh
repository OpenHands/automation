#!/bin/bash
for f in $1/*.pid; do kill $(cat $f) 2>/dev/null; done; sleep 1
