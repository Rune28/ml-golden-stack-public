#!/usr/bin/env bash
# Application images for the local environment.
#
#   images.sh build   build be-service, batch-worker, llm-worker, mle-models and write
#                     infra/envs/local/images.auto.tfvars.json
#   images.sh load    load those images into the kind worker nodes that miss them
#   images.sh remote  use images from the registry instead: TAG=sha-abc1234
#
# Tags come from the image content (dev-<image id>): a changed image gets a
# new tag and `tofu apply` rolls its pods; an unchanged one keeps its tag and
# nothing restarts. A fixed tag like `dev` would hide changes from Kubernetes.
set -euo pipefail
cd "$(dirname "$0")/.."

DOCKER=${DOCKER:-docker}
REGISTRY=${REGISTRY:-ghcr.io/rune28}
CLUSTER=${CLUSTER:-gold}
TFVARS=infra/envs/local/images.auto.tfvars.json

# image name -> source directory (the variable keys use underscores)
IMAGES=(be-service:be_service batch-worker:batch_worker llm-worker:llm_worker mle-models:mle_models)

write_tfvars() { # name=ref ...
  {
    echo '{ "images": {'
    local sep=""
    for pair in "$@"; do
      local name=${pair%%=*} ref=${pair#*=}
      printf '%s  "%s": "%s"' "$sep" "${name//-/_}" "$ref"
      sep=$',\n'
    done
    printf '\n} }\n'
  } > "$TFVARS"
  echo "wrote $TFVARS"
}

build() {
  local refs=()
  for entry in "${IMAGES[@]}"; do
    local name=${entry%%:*} dir=${entry#*:}
    echo "== $name"
    # No provenance attestation: it carries the build time, so every build
    # would get a new id and a new tag even with nothing changed.
    "$DOCKER" build --quiet --provenance=false -t "$REGISTRY/$name:build" "$dir" > /dev/null
    local id
    id=$("$DOCKER" image inspect -f '{{.Id}}' "$REGISTRY/$name:build")
    id=${id#sha256:}
    local ref="$REGISTRY/$name:dev-${id:0:12}"
    "$DOCKER" tag "$REGISTRY/$name:build" "$ref"
    echo "   $ref"
    # Drop older dev tags of this image; shared layers stay.
    # (grep exits 1 when there is nothing to drop, hence `|| true`.)
    { "$DOCKER" images "$REGISTRY/$name" --format '{{.Tag}}' | grep '^dev-' | grep -vx "dev-${id:0:12}" || true; } \
      | xargs -r -I{} "$DOCKER" rmi -f "$REGISTRY/$name:{}" > /dev/null
    refs+=("$name=$ref")
  done
  write_tfvars "${refs[@]}"
}

remote() {
  : "${TAG:?set TAG, e.g. TAG=sha-abc1234}"
  local refs=()
  for entry in "${IMAGES[@]}"; do
    local name=${entry%%:*}
    refs+=("$name=$REGISTRY/$name:$TAG")
  done
  write_tfvars "${refs[@]}"
}

load() {
  [[ -f $TFVARS ]] || { echo "no $TFVARS: run 'images.sh build' first" >&2; exit 1; }
  local nodes
  nodes=$("$DOCKER" ps --filter "label=io.x-k8s.kind.cluster=$CLUSTER" --filter "label=io.x-k8s.kind.role=worker" --format '{{.Names}}')
  [[ -n $nodes ]] || { echo "no worker nodes of kind cluster $CLUSTER" >&2; exit 1; }

  local tmp
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' RETURN
  for ref in $(grep -oE '"[^"]+:[^"]+"' "$TFVARS" | tr -d '"' | grep -v '^images'); do
    local missing=()
    for node in $nodes; do
      if [[ -z $("$DOCKER" exec "$node" ctr -n k8s.io images ls -q "name==$ref") ]]; then
        missing+=("$node")
      fi
    done
    if [[ ${#missing[@]} -eq 0 ]]; then
      echo "$ref: on every node"
      continue
    fi
    echo "$ref: loading into ${missing[*]}"
    "$DOCKER" save -o "$tmp/image.tar" "$ref"
    for node in "${missing[@]}"; do
      # Same import as `kind load docker-image`, without needing the kind CLI.
      "$DOCKER" exec -i "$node" ctr -n k8s.io images import --all-platforms --digests - < "$tmp/image.tar" > /dev/null
    done
    rm -f "$tmp/image.tar"
  done
}

case ${1:-} in
  build) build ;;
  load) load ;;
  remote) remote ;;
  *) echo "usage: $0 build|load|remote" >&2; exit 2 ;;
esac
