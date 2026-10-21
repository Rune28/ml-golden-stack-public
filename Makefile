TOFU        ?= tofu
DOCKER      ?= docker
CLUSTER_DIR := infra/clusters/kind
STORES_DIR  := infra/stores/local
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
        stores-init stores-plan stores-up stores-down \
        platform-init platform-plan platform-up platform-down platform-status \
        images images-load app-status add-data train run report llm-check upload files documents reindex-all chat user-add user-password token adopt-orphans auth-check chat-ui chat-ui-service k9s \
        preflight check-owner host-setup test fmt validate

help:
	@echo "preflight        check the host (tools, inotify limits, memory)"
	@echo "host-setup       raise inotify limits for kind, persistently (Linux, uses sudo)"
	@echo "infra-up         create the local kind cluster (runs preflight first)"
	@echo "infra-down       destroy the local kind cluster (run platform-down and stores-down first)"
	@echo "infra-status     show nodes, pools, taints and GPUs"
	@echo "infra-plan       show planned changes to the cluster"
	@echo "stores-up        install the data stores: Postgres, SeaweedFS, ClickHouse (platform-up runs it)"
	@echo "stores-down      delete the data stores WITH ALL DATA"
	@echo "stores-plan      show planned changes to the data stores"
	@echo "platform-up      install the data stores, build and load the images, install platform services and applications"
	@echo "                 (IMAGES=remote TAG=sha-abc1234 takes the images from GHCR instead)"
	@echo "platform-down    uninstall platform services and applications; the data stores and their data stay"
	@echo "platform-status  show platform and application pods"
	@echo "platform-plan    show planned changes to platform services and applications"
	@echo "images           build the application images and pin their tags for platform-up"
	@echo "images-load      load the images into the kind nodes that miss them"
	@echo "add-data N=5000  add the next N MNIST examples (be_service)"
	@echo "train POOL=gpu   start the Train workflow"
	@echo "run MODEL=champion  start the Run workflow"
	@echo "report           start the Report workflow on the latest prediction run"
	@echo "llm-check        one answer and one embedding from the LLM server, from inside the cluster"
	@echo "upload FILE=...  upload a text or PDF file for retrieval: stored, then indexed (Index workflow)"
	@echo "files            uploaded files and the state of their index"
	@echo "documents        documents read from the files: title, authors, year, keywords"
	@echo "reindex-all      every uploaded file through Index again (after new Index steps)"
	@echo "chat Q=\"...\"      a question about the files; prints CHAT=<id>, pass it to go on in that chat"
	@echo "user-add EMAIL=  a user of the chat page (asks for the password)"
	@echo "user-password EMAIL=  a new password; every session of the user ends"
	@echo "token EMAIL=     a platform token of the user: export TOKEN=\$$(make -s token EMAIL=...) for the targets below"
	@echo "adopt-orphans EMAIL=  once: files and chats without an owner become the user's"
	@echo "auth-check       two users, each sees only their own files, chats and answers"
	@echo "chat-ui          the chat page on http://127.0.0.1:9002 (a port-forward that restarts itself, Ctrl+C to stop)"
	@echo "chat-ui-service  the same as a systemd user service: always on :9002, also after reboots"
	@echo "k9s              the cluster in k9s, all namespaces (k9s itself is installed on the host, not in the cluster)"
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
	@others=$$(find $(CLUSTER_DIR) $(STORES_DIR) $(ENV_DIR) ! -user "$$(id -un)" -print 2>/dev/null | head -5); \
	if [ -n "$$others" ]; then \
	  echo "OpenTofu files owned by another user (an earlier sudo make?):"; echo "$$others" | sed 's/^/  /'; \
	  echo "fix once: sudo chown -R $$(id -un): $(CLUSTER_DIR) $(STORES_DIR) $(ENV_DIR)"; exit 1; \
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

# The data stores have their own state, so platform-down leaves them and
# their data; only stores-down deletes them.
stores-init: check-owner
	$(TOFU) -chdir=$(STORES_DIR) init

stores-plan: stores-init
	$(TOFU) -chdir=$(STORES_DIR) plan

stores-up: stores-init
	$(TOFU) -chdir=$(STORES_DIR) apply -auto-approve

stores-down: check-owner
	$(TOFU) -chdir=$(STORES_DIR) destroy -auto-approve

platform-init: check-owner
	$(TOFU) -chdir=$(ENV_DIR) init

platform-plan: platform-init
	$(TOFU) -chdir=$(ENV_DIR) plan

# Stores first: the platform reads their state. Then images: the applications
# are part of the platform apply, and the image tags come from
# images.auto.tfvars.json. check-owner before the minutes of image builds,
# not after them.
platform-up: check-owner stores-up images images-load platform-init
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

# Users of the chat page are created by an admin: whoever has kubectl on the
# cluster. The password is read here without echo and goes to the command in
# the chat-service pod on stdin, never on a command line.
user-add:
	@test -n "$(EMAIL)" || { echo "usage: make user-add EMAIL=you@example.org"; exit 1; }
	@printf 'Password for $(EMAIL) (12-128 characters): '; stty -echo 2>/dev/null; read -r pw; stty echo 2>/dev/null; echo; \
	  printf '%s\n' "$$pw" | $(KUBECTL) -n chat-service exec -i deploy/chat-service -- /chat_service user-add "$(EMAIL)"

user-password:
	@test -n "$(EMAIL)" || { echo "usage: make user-password EMAIL=you@example.org"; exit 1; }
	@printf 'New password for $(EMAIL): '; stty -echo 2>/dev/null; read -r pw; stty echo 2>/dev/null; echo; \
	  printf '%s\n' "$$pw" | $(KUBECTL) -n chat-service exec -i deploy/chat-service -- /chat_service user-password "$(EMAIL)"

# A platform token of a user, for the calls below to be_service by hand:
#   export TOKEN=$(make -s token EMAIL=you@example.org)
MINUTES ?= 60
token:
	@test -n "$(EMAIL)" || { echo "usage: make token EMAIL=you@example.org [MINUTES=60]"; exit 1; }
	@$(KUBECTL) -n chat-service exec deploy/chat-service -- /chat_service token "$(EMAIL)" $(MINUTES)

# Once, after the login came: every file and chat without an owner becomes
# the user's (the data uploaded before there were users).
adopt-orphans:
	@test -n "$(EMAIL)" || { echo "usage: make adopt-orphans EMAIL=you@example.org"; exit 1; }
	@uid=$$($(KUBECTL) -n chat-service exec deploy/chat-service -- /chat_service user-id "$(EMAIL)") || exit 1; \
	  echo "$$uid" | grep -Eq '^[0-9a-f-]{36}$$' || { echo "not a user_id: $$uid"; exit 1; }; \
	  $(KUBECTL) -n clickhouse exec clickhouse-0 -- clickhouse-client -n -q " \
	    INSERT INTO ml_data.file_owners (user_id, file_id, name) SELECT '$$uid', file_id, name FROM ml_data.files FINAL \
	      WHERE file_id NOT IN (SELECT file_id FROM ml_data.file_owners FINAL); \
	    INSERT INTO ml_data.chat_owners (user_id, chat_id) SELECT DISTINCT '$$uid', chat_id FROM ml_data.chat_messages \
	      WHERE chat_id NOT IN (SELECT chat_id FROM ml_data.chat_owners FINAL); \
	    SELECT 'files of the user', count() FROM ml_data.file_owners FINAL WHERE user_id = '$$uid'; \
	    SELECT 'chats of the user', count() FROM ml_data.chat_owners FINAL WHERE user_id = '$$uid'"

# Two users, each with a file of their own: each sees only theirs, and an
# answer to one never comes from the other's file (scripts/auth-check.sh).
auth-check:
	@KUBECTL="$(KUBECTL)" sh scripts/auth-check.sh


# The same calls the chat page makes, through the platform API (be_service),
# for the user of TOKEN (make token).
AUTH = -H "Authorization: Bearer $(TOKEN)"
need-token = @test -n "$(TOKEN)" || { echo 'set TOKEN first: export TOKEN=$$(make -s token EMAIL=you@example.org)'; exit 1; }

upload:
	@test -n "$(FILE)" || { echo "usage: make upload FILE=path/to/notes.md"; exit 1; }
	$(need-token)
	@curl -sS $(AUTH) --data-binary @$(FILE) --url-query "name=$(notdir $(FILE))" http://127.0.0.1:9001/api/files; echo

files:
	$(need-token)
	@curl -sS $(AUTH) http://127.0.0.1:9001/api/files; echo

# Documents read from the files: a preprint and the published version of a
# paper are one document with two files.
documents:
	$(need-token)
	@curl -sS $(AUTH) http://127.0.0.1:9001/api/documents; echo

# Every uploaded file through Index again, one by one: chunks, and the steps
# added to Index since the file came (the document read from it).
reindex-all:
	$(need-token)
	@curl -sS $(AUTH) http://127.0.0.1:9001/api/files | jq -r '.[].file_id' | while read -r id; do \
	  curl -sS $(AUTH) -X POST http://127.0.0.1:9001/api/files/$$id/index; echo; \
	done

# Without CHAT a new chat starts and its id is printed; with it the question
# goes on in that chat, so the answer can build on the earlier ones.
chat:
	@test -n "$(Q)" || { echo 'usage: make chat Q="question" [CHAT=<chat_id>]'; exit 1; }
	$(need-token)
	@chat="$(CHAT)"; \
	if [ -z "$$chat" ]; then chat=$$(curl -sS $(AUTH) -X POST http://127.0.0.1:9001/api/chats | jq -r .chat_id); echo "CHAT=$$chat"; fi; \
	jq -n --arg text "$(Q)" '{text: $$text}' \
	  | curl -sS $(AUTH) -H 'content-type: application/json' --data-binary @- http://127.0.0.1:9001/api/chats/$$chat/messages \
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

# A terminal UI over the cluster, as kubectl is: a client on the host. The
# kind kubeconfig is not ~/.kube/config, so k9s is given it here; -A starts
# in all namespaces, since nothing of the platform runs in default.
k9s:
	@command -v k9s >/dev/null || { echo "k9s is not installed: https://k9scli.io/topics/install/"; exit 1; }
	KUBECONFIG=$(KUBECONFIG) k9s -A

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
