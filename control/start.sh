#! /bin/bash
wd=.
[ ! -f $wd/serve.sh ] && wd=..
cd $wd
rm -f .exit_llmhub
bash serve.sh
