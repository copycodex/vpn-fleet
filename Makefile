.DEFAULT_GOAL := help
SHELL := /bin/bash

# Usage: make <target> NODE=<node> [NAME=<client>] [IP=<ip>] [FROM=<user>@<port>]
#        [FLEET=<имя установки>]
#
# Частные данные (инвентарь, секреты, known_hosts, .sops.yaml) живут в private/
# и не входят в публичную allowlist. Переопределяется общим набором параметров:
#   PRIVATE_DIR / SOPS_CONFIG / SECRETS_FILE, ANSIBLE_INVENTORY для ansible.
PRIVATE_DIR  ?= private
SOPS_CONFIG  ?= $(PRIVATE_DIR)/.sops.yaml
SECRETS_FILE ?= $(PRIVATE_DIR)/inventories/group_vars/all/secrets.sops.yml
NODE ?=
NAME ?=
IP   ?=
SINGBOX_TEST_BIN ?= sing-box
SINGBOX_LEGACY_TEST_BIN ?= sing-box-1.11
XRAY_TEST_BIN ?= xray
# Дверь свежей ноды для bootstrap: пользователь и порт, которые оставил
# first-contact. Пусто = steady-state ноды, то есть повторный прогон.
FROM ?=

# Ключ шифрования лежит НЕ в дефолтном ~/.config/sops/age/keys.txt: у установки
# своё имя, и ключ зовётся по нему. Ключ один на все серверы парка.
#
# Имя нигде не зашито и выводится тем же правилом, что в
# wizard/keys.sh: FLEET=<имя> побеждает; если имя не задано и
# ключ в каталоге ровно один — берётся он; если их несколько, _require-age-key
# скажет назвать нужный.
#
# Присваивание ЖЁСТКОЕ (`:=`, не `?=`) и это не вкус. Рабочая станция может
# экспортировать SOPS_AGE_KEY_FILE с ключом другого проекта. При `?=` окружение
# побеждает, sops молча берёт чужой ключ и падает "no identity matched any of
# the recipients" уже посреди прогона.
# Присваивание из командной строки (make ... SOPS_AGE_KEY_FILE=<path>) по
# правилам make всё равно сильнее.
FLEET ?=
AGE_DIR := $(HOME)/.config/sops/age
_age_keys := $(wildcard $(AGE_DIR)/*.txt)
# $(strip): перенос строки внутри $(if ...) иначе оставил бы ведущий пробел,
# и путь перестал бы читаться.
SOPS_AGE_KEY_FILE := $(strip $(if $(FLEET),$(AGE_DIR)/$(FLEET).txt,\
                       $(if $(filter 1,$(words $(_age_keys))),$(_age_keys),)))
export SOPS_AGE_KEY_FILE
# Тот же ключ для community.sops: ansible.cfg его не фиксирует, потому что
# имя набора не константа.
ANSIBLE_SOPS_AGE_KEYFILE := $(SOPS_AGE_KEY_FILE)
export ANSIBLE_SOPS_AGE_KEYFILE

.PHONY: help
help: ## Показать доступные команды
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

.PHONY: _require-node
_require-node: _require-age-key
	@[ -n "$(NODE)" ] || { echo "ERROR: задай NODE=<node>"; exit 1; }

.PHONY: _require-age-key
_require-age-key:
	@[ -r "$(SOPS_AGE_KEY_FILE)" ] || { \
		echo "ERROR: age-ключ не читается: $(if $(SOPS_AGE_KEY_FILE),$(SOPS_AGE_KEY_FILE),имя набора не определено)"; \
		echo "       Он корень шифрования: без него не читается ни один секрет."; \
		echo "       Ключи в $(AGE_DIR): $(if $(_age_keys),$(notdir $(_age_keys)),их там нет)"; \
		echo "       Назови нужный:  make <target> FLEET=<имя>"; \
		echo "       Создать с нуля: wizard/keys.sh"; \
		echo "       Другой путь:    make <target> SOPS_AGE_KEY_FILE=<path>"; \
		exit 1; }

.PHONY: new-server
new-server: ## Завести сервер или дозаполнить: инвентарь, карта входа, секреты
	python3 wizard/server.py

.PHONY: bootstrap
bootstrap: _require-node ## Предполёт и бутстрап ноды (FROM=<user>@<port>)
	python3 wizard/bootstrap.py "$(NODE)" $(if $(FROM),--from "$(FROM)",)

.PHONY: first-contact
first-contact: _require-node ## Открыть первый вход ключом парка (NODE=<node>)
	@VPN_FLEET_PRIVATE_DIR="$(PRIVATE_DIR)" python3 scripts/first-contact.py "$(NODE)" --apply

.PHONY: deploy
deploy: _require-node ## Деплой sing-box на ноду
	ansible-playbook playbooks/deploy-node.yml -l "$(NODE)"

.PHONY: base
base: _require-node ## Базовая конфигурация: hardening + firewall
	ansible-playbook playbooks/base.yml -l "$(NODE)"

.PHONY: site
site: _require-node ## Base (hardening + firewall) + deploy sing-box
	ansible-playbook playbooks/site.yml -l "$(NODE)"

.PHONY: add-client
add-client: _require-node ## Добавить клиента в SOPS, deploy и export (NAME=<client>)
	@[ -n "$(NAME)" ] || { echo "ERROR: NAME=<client>"; exit 1; }
	ansible-playbook playbooks/add-client.yml -l "$(NODE)" -e "client_name=$(NAME)"

.PHONY: export
export: _require-node ## Экспорт JSON + VLESS URL + QR в clients/
	ansible-playbook playbooks/export-clients.yml -l "$(NODE)"

.PHONY: win-bundle
win-bundle: _require-node ## Собрать Windows-бандл для клиента (NAME=<client>-win)
	@[ -n "$(NAME)" ] || { echo "ERROR: NAME=<client>-win"; exit 1; }
	scripts/build-win-bundle.sh "$(NODE)" "$(NAME)"

.PHONY: add-wg-peer
add-wg-peer: _require-node ## Завести WG-пира управления в SOPS (NAME=<peer>)
	@[ -n "$(NAME)" ] || { echo "ERROR: NAME=<peer>"; exit 1; }
	scripts/add-wg-peer.sh "$(NODE)" "$(NAME)"

.PHONY: wireguard
wireguard: _require-node ## Деплой management-WireGuard на ноду
	ansible-playbook playbooks/wireguard.yml -l "$(NODE)"

.PHONY: doctor
doctor: _require-node ## Проверить готовность к первому прогону (NODE=<node>)
	@echo "age-ключ:        $(SOPS_AGE_KEY_FILE)"
	@scripts/check-private-inputs.sh "$(NODE)"
	@ansible-inventory --host "$(NODE)" > /dev/null && \
		echo "инвентарь+sops:  читается"
	@VPN_FLEET_PRIVATE_DIR="$(PRIVATE_DIR)" python3 scripts/first-contact.py "$(NODE)"
	@ansible-inventory --host "$(NODE)" | \
		python3 -c 'import json,sys; d=json.load(sys.stdin); \
		    print("станет:          {}@{}".format(d.get("bootstrap_user","?"), \
		        d.get("bootstrap_ssh_port","?"))); \
		    print("пароль консоли:  {}".format("есть в sops" \
		        if d.get("node_{}_console_password".format(d.get("node_name",""))) \
		        else "нет — пароль двери ещё не менялся"))'
	@door="$$(VPN_FLEET_PRIVATE_DIR="$(PRIVATE_DIR)" python3 scripts/first-contact.py "$(NODE)" --owned-known-hosts door | grep -c . || true)"; \
	steady="$$(VPN_FLEET_PRIVATE_DIR="$(PRIVATE_DIR)" python3 scripts/first-contact.py "$(NODE)" --owned-known-hosts steady | grep -c . || true)"; \
	echo "known_hosts:     метки $(NODE): door=$$door steady=$$steady"

.PHONY: probe
probe: ## Однократно проверить настроенные VPN transports и SSH
	client/probe/vpn-probe.sh

.PHONY: test
test: ## Публичные тесты и render
	tests/reality-frontend-preflight.sh
	tests/reality-keypair.sh
	tests/switch-config-safe-smoke.sh
	tests/xray-recoverable-deploy.sh
	tests/singbox-recoverable-deploy.sh
	tests/firewall-reconcile.sh
	tests/ssh-authorized-keys-atomic.sh
	tests/secrets-argv.sh
	SINGBOX_TEST_BIN="$(SINGBOX_TEST_BIN)" \
	SINGBOX_LEGACY_TEST_BIN="$(SINGBOX_LEGACY_TEST_BIN)" \
	XRAY_TEST_BIN="$(XRAY_TEST_BIN)" \
		ansible-playbook -i localhost, tests/render-singbox-config.yml
	yamllint .
	python3 -m pytest

.PHONY: first-contact-stand-up first-contact-stand-down first-contact-stand
first-contact-stand-up: ## Поднять локальный sshd-стенд первого входа
	tests/first-contact-stand.sh up

first-contact-stand-down: ## Снести локальный sshd-стенд первого входа
	tests/first-contact-stand.sh down

first-contact-stand: ## Полный прогон first-contact на локальном sshd-стенде
	tests/first-contact-stand.sh test

.PHONY: first-contact-stand-answer
first-contact-stand-answer: ## Замер ответов PAM на четырёх локальных docker-сценариях
	tests/first-contact-stand-answer.sh test

.PHONY: first-contact-stand-resume
first-contact-stand-resume: ## Восстановление first-contact после каждого побочного эффекта
	tests/first-contact-stand-resume.sh test

.PHONY: secrets-edit
secrets-edit: _require-age-key ## Редактировать sops-секреты (FILE=private/inventories/group_vars/all/secrets.sops.yml)
	python3 scripts/secrets_lock.py "$(or $(FILE),$(SECRETS_FILE))" -- sops --config "$(SOPS_CONFIG)" "$(or $(FILE),$(SECRETS_FILE))"

.PHONY: provision
provision: ## Поднять ноду в Hetzner (нужен export HCLOUD_TOKEN=...)
	cd terraform && terraform init -input=false && terraform apply

.PHONY: destroy
destroy: ## Снести ноду (остановить биллинг полигона)
	cd terraform && terraform destroy
