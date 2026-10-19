TOFU        ?= tofu
DOCKER      ?= docker
CLUSTER_DIR := infra/clusters/kind
ENV_DIR     := infra/envs/local
KUBECONFIG  := $(abspath $(CLUSTER_DIR)/.kubeconfig)
KUBECTL     := KUBECONFIG=$(KUBECONFIG) kubectl

# local: build the images and load them into kind; remote: take TAG from GHCR.
IMAGES ?= local
TAG    ?=

# Pipeline shortcuts without the page.
N     ?= 5000
POOL  ?= gpu
MODEL ?= champion

# OpenTofu state written by root can be updated only by root again. sudo is
# usually reached for because docker refuses the user, and then the whole
# make gets root; the fix is docker access for the user (make preflight).
# Targets that need root (host-setup) call sudo themselves.
ifeq ($(shell id -u),0)
$(error do not run make as root: OpenTofu state written by root cannot be updated without it. Give your user access to docker instead, see make preflight)
endif

.PHONY: help infra-init infra-plan infra-up infra-down infra-status \
        platform-init platform-plan platform-up platform-down platform-status \
        images images-load app-status add-data train run report llm-check upload files chat chat-ui chat-ui-service \
        preflight check-owner host-setup test fmt validate

help:
	@echo "preflight        check the host (tools, inotify limits, memory)"
	@echo "host-setup       raise inotify limits for kind, persistently (Linux, uses sudo)"
	@echo "infra-up         create the local kind cluster (runs preflight first)"
	@echo "infra-down       destroy the local kind cluster (run platform-down first)"
	@echo "infra-status     show nodes, pools, taints and GPUs"
	@echo "infra-plan       show planned changes to the cluster"
	@echo "platform-up      build and load the images, install platform services and applications"
	@echo "                 (IMAGES=remote TAG=sha-abc1234 takes the images from GHCR instead)"
	@echo "platform-down    uninstall platform services and applications"
	@echo "platform-status  show platform and application pods"
	@echo "platform-plan    show planned changes to platform services and applications"
	@echo "images           build the application images and pin their tags for platform-up"
	@echo "images-load      load the images into the kind nodes that miss them"
	@echo "add-data N=5000  add the next N MNIST examples (be_service)"
	@echo "train POOL=gpu   start the Train workflow"
	@echo "run MODEL=champion  start the Run workflow"
	@echo "report           start the Report workflow on the latest prediction run"
	@echo "llm-check        one answer and one embedding from the LLM server, from inside the cluster"
	@echo "upload FILE=...  upload a text file for retrieval: stored, then indexed (Index workflow)"
	@echo "files            uploaded files and the state of their index"
	@echo "chat Q=\"...\"      a question about the files; prints CHAT=<id>, pass it to go on in that chat"
	@echo "chat-ui          the chat page on http://127.0.0.1:9002 (a port-forward that restarts itself, Ctrl+C to stop)"
	@echo "chat-ui-service  the same as a systemd user service: always on :9002, also after reboots"
	@echo "test             unit tests of be_service, batch_worker, llm_worker and mle_models"
	@echo "fmt              format OpenTofu code"
	@echo "validate         validate OpenTofu code"

infra-init: check-owner
	$(TOFU) -chdir=$(CLUSTER_DIR) init

infra-plan: infra-init
	$(TOFU) -chdir=$(CLUSTER_DIR) plan

preflight:
	@$(CLUSTER_DIR)/scripts/preflight.sh

# An earlier sudo make leaves state and .terraform owned by root, and init
# fails with "permission denied". Found before anything runs, with the fix.
check-owner:
	@others=$$(find $(CLUSTER_DIR) $(ENV_DIR) ! -user "$$(id -un)" -print 2>/dev/null | head -5); \
	if [ -n "$$others" ]; then \
	  echo "OpenTofu files owned by another user (an earlier sudo make?):"; echo "$$others" | sed 's/^/  /'; \
	  echo "fix once: sudo chown -R $$(id -un): $(CLUSTER_DIR) $(ENV_DIR)"; exit 1; \
	fi

# The only target that changes the host. Kept out of infra-up on purpose:
# it needs sudo, and preflight tells you when to run it.
host-setup:
	printf 'fs.inotify.max_user_watches = 524288\nfs.inotify.max_user_instances = 8192\n' \
	  | sudo tee /etc/sysctl.d/99-kind.conf
	sudo sysctl -p /etc/sysctl.d/99-kind.conf

infra-up: check-owner preflight infra-init
	$(TOFU) -chdir=$(CLUSTER_DIR) apply -auto-approve
	@echo
	@echo "export KUBECONFIG=$(KUBECONFIG)"

infra-down: check-owner
	$(TOFU) -chdir=$(CLUSTER_DIR) destroy -auto-approve

infra-status:
	$(KUBECTL) get nodes -L node-pool,capacity-type
	@echo
	$(KUBECTL) get nodes \
	  -o custom-columns='NAME:.metadata.name,TAINTS:.spec.taints[*].key,GPU:.status.allocatable.nvidia\.com/gpu'

ifeq ($(IMAGES),remote)
images:
	scripts/images.sh remote
images-load:
	@echo "IMAGES=remote: the nodes pull from the registry"
else
images:
	DOCKER=$(DOCKER) scripts/images.sh build
images-load:
	DOCKER=$(DOCKER) scripts/images.sh load
endif

platform-init: check-owner
	$(TOFU) -chdir=$(ENV_DIR) init

platform-plan: platform-init
	$(TOFU) -chdir=$(ENV_DIR) plan

# Images first: the applications are part of the platform apply, and the
# image tags come from images.auto.tfvars.json. check-owner before the
# minutes of image builds, not after them.
platform-up: check-owner images images-load platform-init
	$(TOFU) -chdir=$(ENV_DIR) apply -auto-approve
	@echo
	@$(TOFU) -chdir=$(ENV_DIR) output

platform-down: check-owner
	$(TOFU) -chdir=$(ENV_DIR) destroy -auto-approve

platform-status:
	$(KUBECTL) get pods -o wide -n postgres
	@echo
	$(KUBECTL) get pods -o wide -n temporal
	@echo
	@$(MAKE) --no-print-directory app-status

app-status:
	@for ns in seaweedfs clickhouse mlflow batch-worker llm-worker be-service chat-service; do \
	  echo "== $$ns"; $(KUBECTL) get pods,jobs -o wide -n $$ns; echo; done

# The same call the Add data button makes (be_service on the kind port mapping `app`).
add-data:
	@curl -sS -H 'content-type: application/json' -d '{"count": $(N)}' http://127.0.0.1:9001/api/batches; echo

train:
	$(KUBECTL) -n temporal exec deploy/temporal-admintools -- temporal workflow start \
	  --address temporal-frontend:7233 --task-queue batch --type Train --workflow-id train \
	  --input '{"pool": "$(POOL)"}'

run:
	$(KUBECTL) -n temporal exec deploy/temporal-admintools -- temporal workflow start \
	  --address temporal-frontend:7233 --task-queue batch --type Run --workflow-id run \
	  --input '{"model": "$(MODEL)"}'

# Through be_service: it picks the latest prediction run.
report:
	@curl -sS -H 'content-type: application/json' -d '{}' http://127.0.0.1:9001/api/report; echo

# Through the adapter in the llm worker pod: checks the server, the cluster's name
# for it (llm.llm.svc), the models and the adapter in one call.
llm-check:
	$(KUBECTL) -n llm-worker exec deploy/llm-worker -- python -m workflow.llm

# The same calls the chat page makes, through the platform API (be_service).
upload:
	@test -n "$(FILE)" || { echo "usage: make upload FILE=path/to/notes.md"; exit 1; }
	@curl -sS --data-binary @$(FILE) --url-query "name=$(notdir $(FILE))" http://127.0.0.1:9001/api/files; echo

files:
	@curl -sS http://127.0.0.1:9001/api/files; echo

# Without CHAT a new chat starts and its id is printed; with it the question
# goes on in that chat, so the answer can build on the earlier ones.
chat:
	@test -n "$(Q)" || { echo 'usage: make chat Q="question" [CHAT=<chat_id>]'; exit 1; }
	@chat="$(CHAT)"; \
	if [ -z "$$chat" ]; then chat=$$(curl -sS -X POST http://127.0.0.1:9001/api/chats | jq -r .chat_id); echo "CHAT=$$chat"; fi; \
	jq -n --arg text "$(Q)" '{text: $$text}' \
	  | curl -sS -H 'content-type: application/json' --data-binary @- http://127.0.0.1:9001/api/chats/$$chat/messages \
	  | jq -r 'if .error then "error: \(.error)" else "\(.text)\n\nturn \(.turn), sources: \([.sources[] | "\(.name)#\(.chunk_id)"] | join(", "))" end'

# The chat product. The kind cluster maps no host port for it (a new mapping
# means recreating the cluster), so a port-forward. It ends whenever its pod
# is replaced, on every rollout of chat-service: the loop starts it again.
# On a server the page is an Ingress instead (README, Infrastructure layers).
chat-ui:
	@echo "chat page on http://127.0.0.1:9002 (Ctrl+C to stop)"
	@while true; do $(KUBECTL) -n chat-service port-forward svc/chat-service 9002:8080; echo "port-forward ended, starting again"; sleep 2; done

# The same port-forward as a systemd user service: the page stays on :9002
# across rollouts and reboots, and waits while the cluster is down. Then
# make chat-ui is not needed (both would want the port).
chat-ui-service:
	mkdir -p ~/.config/systemd/user
	printf '%s\n' '[Unit]' 'Description=Chat page of ml-golden-stack on 127.0.0.1:9002 (kubectl port-forward)' \
	  '# The cluster may be down for hours: keep trying, never give up.' 'StartLimitIntervalSec=0' '' \
	  '[Service]' 'Environment=KUBECONFIG=$(KUBECONFIG)' \
	  'ExecStart=$(shell command -v kubectl) -n chat-service port-forward svc/chat-service 9002:8080' \
	  '# Ends when its pod is replaced or the cluster is down: start again.' 'Restart=always' 'RestartSec=3' '' \
	  '[Install]' 'WantedBy=default.target' > ~/.config/systemd/user/chat-ui.service
	systemctl --user daemon-reload && systemctl --user enable --now chat-ui
	@echo "chat page on http://127.0.0.1:9002; logs: journalctl --user -u chat-ui"

test:
	cd be_service && cargo test
	cd chat_service && cargo test
	cd batch_worker && uv run pytest
	cd llm_worker && uv run pytest
	cd mle_models && uv run pytest

fmt:
	$(TOFU) fmt -recursive

validate: infra-init platform-init
	$(TOFU) -chdir=$(CLUSTER_DIR) validate
	$(TOFU) -chdir=$(ENV_DIR) validate
