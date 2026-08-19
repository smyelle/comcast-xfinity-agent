#!/usr/bin/env bash
# Try the barge-in demo: talk over the agent and hear what changes.
#
# Runs the same A/B that produced BARGE_IN_VERIFY.md. Two apps, identical except that the
# treatment asks the platform to report interruptions and declares `repair=` on its
# announces. A synthetic caller says something over the agent's disclosure, and you see
# what each arm does about it.
#
#   ./barge_in_try.sh                 # the A/B, caller says "mhmm" 5s in
#   ./barge_in_try.sh 8               # cut in later -- the resume point moves with it
#   ./barge_in_try.sh 5 "wait, can I just pay my bill"   # a real interruption, not agreement
#   ./barge_in_try.sh --ladder        # one arm, four cut points, to see it track
#   ./barge_in_try.sh --listen        # also judge the AUDIO, not just the text frames
#
# This CANNOT be reproduced in a text simulator. The text part is complete in both arms
# and only the audio is cut -- which is exactly why the defect survived so long.
set -euo pipefail

PROJECT_NUM=555355609568
TREATMENT="projects/${PROJECT_NUM}/locations/us/apps/dc558c85-9f47-49bb-8e14-41f8af51fadc"
CONTROL="projects/${PROJECT_NUM}/locations/us/apps/1a0dda4b-70b1-4f96-b963-a63a12b08bac"

VENV=/Users/fsamuel/Labs/cxas-labs/.venv/bin/python
PROBES=/Users/fsamuel/Labs/cxas-labs/.worktrees/barge-detect/ces-probes
OPENING="hi, I want to open an account"

LADDER=0; LISTEN=0; ARGS=()
for a in "$@"; do
  case "$a" in
    --ladder) LADDER=1 ;;
    --listen) LISTEN=1 ;;
    *) ARGS+=("$a") ;;
  esac
done
WAIT="${ARGS[0]:-5}"
SAYS="${ARGS[1]:-mhmm}"

cd "$PROBES"

drive () {  # label, app, wait
  local label=$1 app=$2 at=$3
  printf '\n\033[1m======== %s — caller says %s at %ss\033[0m\n' "$label" "$(printf '%q' "$SAYS")" "$at"
  "$VENV" drive_barge_audio.py x --app "$app" \
      --say "$OPENING" --wait "$at" --then "$SAYS" --gap 20 --tail 10 \
      --out "/tmp/barge_try_${label}_${at}.wav" 2>&1 \
    | grep -E "INTERRUPTION at|^ +[0-9.]+s +(text|asr) " \
    | sed -e 's/^ *//' -e 's/  */ /g'
}

if [[ $LADDER -eq 1 ]]; then
  echo "TREATMENT only, four cut points. Watch where the replay resumes."
  for at in 3 5 8 11; do drive TREATMENT "$TREATMENT" "$at"; done
else
  # Control FIRST and independently -- never chain it behind the subject, or a treatment
  # failure is indistinguishable from a control that never ran.
  drive CONTROL   "$CONTROL"   "$WAIT"
  drive TREATMENT "$TREATMENT" "$WAIT"
  cat <<'NOTE'

--------------------------------------------------------------------------------
CONTROL    re-asks its question. The retention and opt-out terms were cut off and
           are never spoken again -- yet all four announces are recorded delivered.
TREATMENT  "Sorry - as I was saying, ..." and delivers exactly the lines the caller
           did not reach, then carries on with the pending question.
--------------------------------------------------------------------------------
NOTE
fi

if [[ $LISTEN -eq 1 ]]; then
  printf '\n\033[1m======== judging the AUDIO (not the text frames)\033[0m\n'
  printf 'Calls are recorded for training and quality.\nYour personal data is retained for ninety days after the call.\nYou can opt out of marketing at any time by calling us back.\n' \
    > /tmp/barge_try_lines.txt
  for f in /tmp/barge_try_*_"${WAIT}".turn2.wav; do
    [[ -e "$f" ]] || continue
    printf '%-46s ' "$(basename "$f")"
    "$VENV" judge_audio.py "$f" /tmp/barge_try_lines.txt \
      "Finish with a line exactly: VERDICT: RETENTION=<YES|NO> OPTOUT=<YES|NO>, for whether the ninety-day retention and the marketing opt-out are actually spoken." \
      2>/dev/null | grep -E "^VERDICT" | tail -1
  done
fi
