#!/bin/zsh
set -u
source ~/.local/bin/mm-activate nf-metro-dev >/dev/null 2>&1
G=/private/tmp/claude-501/-Users-jonathan-manning-projects-nf-metro/a5224bd2-cc85-4aba-a2ed-df4f454e3559/scratchpad/2042/gate
S=$G/..
python -c 'import sys; print(sys.version)'
for corpus in repo real rough; do
  ( cd $S/mainref && PYTHONPATH=$S/mainref/src python $G/runner.py $S/mainref $G/out/list_$corpus.txt $G/out/main_$corpus.jsonl $G/out/svg_main_$corpus 5 2>>$G/out/main_$corpus.err ) &
  ( cd $S/gate_pr && PYTHONPATH=$S/gate_pr/src python $G/runner.py $S/gate_pr $G/out/list_$corpus.txt $G/out/pr_$corpus.jsonl $G/out/svg_pr_$corpus 5 2>>$G/out/pr_$corpus.err ) &
  wait
  echo "done $corpus $(date)"
done
