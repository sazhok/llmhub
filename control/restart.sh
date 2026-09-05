#! /bin/bash
wd=.
[ ! -f $wd/serve.sh ] && wd=..
cd $wd
bash control/stop.sh || true
bash control/start.sh
