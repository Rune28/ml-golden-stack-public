#!/bin/sh
# make auth-check: two users, each with a file of their own, against the
# platform API on the kind port mapping (127.0.0.1:9001). Passes when each
# sees only their own files and chats, a file or chat of the other is a 404,
# an answer to one never comes from the other's file, the same bytes of both
# are one file whose upload by the second does not say "duplicate", and a
# call without a token is refused. New users and files every run; the files
# are deleted at the end, the users stay (there is no command to delete one).
set -eu

API=http://127.0.0.1:9001
K=${KUBECTL:-kubectl}
RUN=$(date +%s)
A="auth-check-$RUN-a@example.org"
B="auth-check-$RUN-b@example.org"
PASSWORD="auth-check-password-$RUN"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

ok() { echo "ok    $*"; }
fail() { echo "FAIL  $*"; exit 1; }
# KUBECTL from make is "KUBECONFIG=... kubectl": run through env.
chat_service() { env $K -n chat-service exec -i deploy/chat-service -- /chat_service "$@"; }
# call TOKEN METHOD PATH [curl args...]: the status; the body in $TMP/body.
call() {
  token=$1 method=$2 path=$3; shift 3
  if [ -n "$token" ]; then set -- -H "Authorization: Bearer $token" "$@"; fi
  curl -sS -o "$TMP/body" -w '%{http_code}' -X "$method" "$@" "$API$path"
}
body() { cat "$TMP/body"; }

for user in "$A" "$B"; do printf '%s\n' "$PASSWORD" | chat_service user-add "$user" >/dev/null; done
TA=$(chat_service token "$A" 30)
TB=$(chat_service token "$B" 30)
ok "two users: $A, $B"

[ "$(call "" GET /api/files)" = 401 ] || fail "a call without a token is not refused"
ok "without a token: 401"

# Content of this run only: its sha256, the file_id, is new every time.
printf '# Pie %s\n\nThe gooseberry pie of the family: three eggs, a glass of sugar, gooseberries from the dacha, forty minutes at 180 degrees.\n' "$RUN" > "$TMP/a.md"
printf '# Hike %s\n\nThe route to Elbrus: from Terskol to the Barrels, a night at Priyut, the saddle at dawn.\n' "$RUN" > "$TMP/b.md"
[ "$(call "$TA" POST /api/files --data-binary @"$TMP/a.md" --url-query name=pie.md)" = 200 ] || fail "upload of A: $(body)"
FA=$(body | jq -r .file_id)
[ "$(call "$TB" POST /api/files --data-binary @"$TMP/b.md" --url-query name=hike.md)" = 200 ] || fail "upload of B: $(body)"
FB=$(body | jq -r .file_id)
ok "uploads: A $FA, B $FB"

# Until both are indexed: chunks, the document, the keywords.
wait_indexed() {
  token=$1 file=$2 i=0
  while [ $i -lt 90 ]; do
    call "$token" GET /api/files >/dev/null
    [ "$(body | jq -r --arg f "$file" '.[] | select(.file_id == $f) | .status')" = indexed ] && return 0
    i=$((i + 1)); sleep 2
  done
  fail "file $file not indexed in 3 minutes"
}
wait_indexed "$TA" "$FA"
wait_indexed "$TB" "$FB"
ok "both indexed"

call "$TA" GET /api/files >/dev/null
[ "$(body | jq --arg a "$FA" --arg b "$FB" '[.[].file_id] | (index($a) != null) and (index($b) == null)')" = true ] \
  || fail "A's files: $(body | jq -c '[.[] | {file_id, name}]')"
call "$TB" GET /api/files >/dev/null
[ "$(body | jq --arg a "$FA" --arg b "$FB" '[.[].file_id] | (index($b) != null) and (index($a) == null)')" = true ] \
  || fail "B's files: $(body | jq -c '[.[] | {file_id, name}]')"
ok "each sees only their own file"

[ "$(call "$TB" DELETE "/api/files/$FA")" = 404 ] || fail "B could delete A's file: $(body)"
[ "$(call "$TB" POST "/api/files/$FA/index")" = 404 ] || fail "B could index A's file again: $(body)"
call "$TA" GET /api/files >/dev/null
[ "$(body | jq --arg a "$FA" '[.[].file_id] | index($a) != null')" = true ] || fail "A's file is gone after B's attempt"
ok "A's file is a 404 to B, and stays A's"

[ "$(call "$TA" POST /api/chats)" = 200 ] || fail "chat of A: $(body)"
CA=$(body | jq -r .chat_id)
[ "$(call "$TA" POST "/api/chats/$CA/messages" -H 'content-type: application/json' \
  --data '{"text": "What goes into the gooseberry pie?"}')" = 200 ] || fail "question of A: $(body)"
body | jq -e --arg a "$FA" '[.sources[].file_id] | index($a) != null' >/dev/null || fail "A's answer is not from A's file"
ok "A's question is answered from A's file"
[ "$(call "$TB" GET "/api/chats/$CA/messages")" = 404 ] || fail "B could read A's chat: $(body)"
[ "$(call "$TB" POST "/api/chats/$CA/messages" -H 'content-type: application/json' --data '{"text": "hi"}')" = 404 ] \
  || fail "B could write to A's chat: $(body)"
call "$TB" GET /api/chats >/dev/null
[ "$(body | jq --arg c "$CA" '[.[].chat_id] | index($c) == null')" = true ] || fail "A's chat is in B's list"
ok "A's chat is a 404 to B and not in B's list"

[ "$(call "$TB" POST /api/chats)" = 200 ] || fail "chat of B: $(body)"
CB=$(body | jq -r .chat_id)
[ "$(call "$TB" POST "/api/chats/$CB/messages" -H 'content-type: application/json' \
  --data '{"text": "What goes into the gooseberry pie?"}')" = 200 ] || fail "question of B: $(body)"
body | jq -e --arg a "$FA" '[.sources[].file_id] | index($a) == null' >/dev/null \
  || fail "B's answer cites A's file: $(body | jq -c .sources)"
ok "B's question about A's pie is not answered from A's file"

[ "$(call "$TB" POST /api/files --data-binary @"$TMP/a.md" --url-query name=bs-copy.md)" = 200 ] || fail "B's copy: $(body)"
[ "$(body | jq -r .duplicate)" = false ] || fail "B's upload of A's bytes says duplicate: $(body)"
[ "$(body | jq -r .file_id)" = "$FA" ] || fail "the same bytes are not the same file"
call "$TB" GET /api/files >/dev/null
[ "$(body | jq -r --arg a "$FA" '.[] | select(.file_id == $a) | .name')" = bs-copy.md ] || fail "B does not see the file under B's name"
call "$TA" GET /api/files >/dev/null
[ "$(body | jq -r --arg a "$FA" '.[] | select(.file_id == $a) | .name')" = pie.md ] || fail "A's name of the file changed"
ok "the same bytes: one file, no duplicate for B, each with their own name"

[ "$(call "$TB" DELETE "/api/files/$FA")" = 200 ] || fail "B's delete of the copy: $(body)"
call "$TA" GET /api/files >/dev/null
[ "$(body | jq --arg a "$FA" '[.[].file_id] | index($a) != null')" = true ] || fail "B's delete took A's file"
ok "B deletes only B's link; the file stays A's"

[ "$(call "$TA" DELETE "/api/files/$FA")" = 200 ] || fail "A's delete: $(body)"
[ "$(call "$TB" DELETE "/api/files/$FB")" = 200 ] || fail "B's delete: $(body)"
ok "files deleted; the users $A and $B stay"
echo "auth-check passed"
