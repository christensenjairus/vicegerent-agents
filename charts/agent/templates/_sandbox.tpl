{{- define "vicegerent-agent.sandbox" -}}
apiVersion: agents.x-k8s.io/v1alpha1
kind: Sandbox
metadata:
  name: {{ include "vicegerent-agent.name" . }}
  namespace: agent-sandbox
  annotations:
    kustomize.toolkit.fluxcd.io/force: Disabled
    helm.sh/resource-policy: keep
spec:
  podTemplate:
    metadata:
      labels:
        vicegerent.io/dashboard: {{ include "vicegerent-agent.name" . }}
      annotations:
        # gitrepos/models/runtime/tmp are reclonable or reseeded; excluded from Velero FSB.
        backup.velero.io/backup-volumes-excludes: gitrepos,models,runtime,tmp
    spec:
      automountServiceAccountToken: false
      securityContext:
        fsGroup: 10000
        runAsUser: 10000
        runAsGroup: 10000
        runAsNonRoot: true
        seccompProfile:
          type: RuntimeDefault
      initContainers:
        - name: prepare-run
          image: {{ .Values.image.repository }}:{{ .Values.image.tag }}
          imagePullPolicy: Always
          command: [sh, -c]
          args:
            - |
              set -eu
              chown 10000:10000 /run
              # chown -R: stale uid-0 dirs from old subPath design cause EPERM on reseed; idempotent on fresh PVCs.
              mkdir -p /opt/data/.codex /opt/data/.claude
              chown -R 10000:10000 /opt/data/.codex /opt/data/.claude
          securityContext:
            runAsUser: 0
            runAsGroup: 0
            runAsNonRoot: false
          volumeMounts:
            - name: runtime
              mountPath: /run
            - name: data
              mountPath: /opt/data
        - name: seed-data
          image: {{ .Values.image.repository }}:{{ .Values.image.tag }}
          imagePullPolicy: Always
          command: [bash, -c]
          args:
            - |-
              set -euo pipefail
              # fastembed reads HERMES_HOME/cache; the local LLM reads ~/.hermes — different dirs.
              fastembed_dest="/opt/data/cache/fastembed"
              llm_dest="/opt/data/home/.hermes/mnemosyne/models"
              marker_dir="/opt/data/.hermes"
              mkdir -p "${fastembed_dest}" "${llm_dest}" "${marker_dir}" /opt/data/plugins /opt/data/.ssh
              # Seed egress proxy CA cert so curl, pip, git, and Python requests trust it.
              mkdir -p /opt/data/certs
              # Build combined CA bundle: system CAs + proxy CA.
              # Using only the proxy CA would break direct-egress TLS (Slack, SSH).
              cat /etc/ssl/certs/ca-certificates.crt /reload/egress-proxy-ca/ca.crt \
                > /opt/data/certs/ca-bundle.crt
              # digest-gated reseed (rm-first); layout=v2 forces reseed off old dest paths;
              # also reseed if llm_dest is empty (its own PVC, excluded from Velero backup).
              seed="/opt/hermes/mnemosyne-seed"
              marker="${marker_dir}/.mnemosyne-seed.sha256"
              want="$(cat "${seed}.sha256"):layout=v2"
              if [ "$(cat "${marker}" 2>/dev/null || true)" != "${want}" ] || [ -z "$(ls -A "${llm_dest}" 2>/dev/null)" ]; then
                rm -rf "${fastembed_dest}"
                # llm_dest is a mountpoint; rm -rf on it errors EBUSY under set -e.
                find "${llm_dest}" -mindepth 1 -delete 2>/dev/null || true
                mkdir -p "${fastembed_dest}" "${llm_dest}"
                cp -a "${seed}/mnemosyne/models/." "${llm_dest}/"
                cp -a "${seed}/cache/fastembed/." "${fastembed_dest}/"
                printf '%s\n' "${want}" > "${marker}"
              fi
              pkg="$(/opt/hermes/.venv/bin/python -c 'import mnemosyne_hermes, os; print(os.path.dirname(mnemosyne_hermes.__file__))')"
              ln -sfn "${pkg}" /opt/data/plugins/mnemosyne
              # Seed ConfigMap-owned config files; agent runtime state uses different files in the same dirs.
              mkdir -p /opt/data/.codex /opt/data/.claude
              # Render config.toml and settings.json from templates — substitutes
              # AGENTGATEWAY_API_KEY so codex/claude-code send the correct bearer
              # token to agentgateway without relying on shell env var lookup.
              sed "s|\${AGENTGATEWAY_API_KEY}|${AGENTGATEWAY_API_KEY}|g" \
                < /reload/codex-config/config.toml \
                > /opt/data/.codex/config.toml
              sed "s|\${AGENTGATEWAY_API_KEY}|${AGENTGATEWAY_API_KEY}|g" \
                < /reload/claude-config/settings.json \
                > /opt/data/.claude/settings.json
              cp -f /reload/claude-config/claude.json /opt/data/.claude/.claude.json
              cp -f /reload/claude-config/CLAUDE.md /opt/data/.claude/CLAUDE.md
              # kanban init: pre-create SQLite schema on PVC; || true because self-inits on first call anyway.
              mkdir -p /opt/data/tmp
              HERMES_HOME=/opt/data TMPDIR=/opt/data/tmp \
                /opt/hermes/.venv/bin/hermes kanban init || true
              # Remove any stale subPath artifact (dangling symlink or empty file from old design).
              [ ! -s /opt/data/config.yaml ] && rm -f /opt/data/config.yaml
              # Merge GitOps config.yaml onto PVC — ConfigMap keys always win on conflict.
              if [ -f /opt/data/config.yaml ]; then
                yq eval-all 'select(fi == 0) * select(fi == 1)' \
                  /opt/data/config.yaml \
                  /reload/hermes-config/config.yaml \
                  > /opt/data/config.yaml.tmp \
                  && mv /opt/data/config.yaml.tmp /opt/data/config.yaml
              else
                cp /reload/hermes-config/config.yaml /opt/data/config.yaml
              fi
              touch /opt/data/.restart_pending.json
              # shutil.copytree preserves image source permissions (dr-xr-xr-x); make all skill dirs writable.
              find /opt/data/skills -type d -perm 555 -exec chmod u+w {} +
          env:
            - name: PYTHONDONTWRITEBYTECODE
              value: '1'
            - name: AGENTGATEWAY_API_KEY
              valueFrom:
                secretKeyRef:
                  name: {{ include "vicegerent-agent.name" . }}-agentgateway-api-key
                  key: api-key
                  optional: true
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: [ALL]
          volumeMounts:
            - name: data
              mountPath: /opt/data
            - name: models
              mountPath: /opt/data/home/.hermes/mnemosyne/models
            - name: config
              mountPath: /reload/hermes-config
              readOnly: true
            - name: codex-config
              mountPath: /reload/codex-config
              readOnly: true
            - name: claude-config
              mountPath: /reload/claude-config
              readOnly: true
            - name: egress-proxy-ca-cert
              mountPath: /reload/egress-proxy-ca
              readOnly: true
        # Win the startup race: block until egress-proxy, agentgateway (via proxy),
        # and the vMCP route are all reachable before hermes starts, so a cold cluster
        # doesn't require a pod restart to recover.
        - name: wait-deps
          image: {{ .Values.image.repository }}:{{ .Values.image.tag }}
          imagePullPolicy: Always
          command: [bash, -c]
          args:
            - |-
              set -u
              PROXY_HOST=egress-proxy.egress-proxy.svc.cluster.local
              PROXY_PORT=8080
              AGW=http://agentgateway-proxy.agentgateway-system.svc.cluster.local
              VMCP="${AGW}/mcp/vmcp"
              INTERVAL=3
              MAX=50  # ~150s per dependency

              # 1) egress-proxy: direct TCP connect (do NOT go through the proxy itself).
              n=0
              echo "waiting for egress-proxy (${PROXY_HOST}:${PROXY_PORT})..."
              until (exec 3<>"/dev/tcp/${PROXY_HOST}/${PROXY_PORT}") 2>/dev/null; do
                n=$((n+1))
                if [ "${n}" -ge "${MAX}" ]; then
                  echo "WARNING: timed out waiting for egress-proxy; continuing anyway"
                  break
                fi
                sleep "${INTERVAL}"
              done
              [ "${n}" -lt "${MAX}" ] && echo "egress-proxy ready"

              # 2) agentgateway THROUGH the proxy: any HTTP response means it's up.
              n=0
              echo "waiting for agentgateway (via egress-proxy)..."
              until code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "${AGW}/" 2>/dev/null) \
                    && [ "${code}" != "000" ]; do
                n=$((n+1))
                if [ "${n}" -ge "${MAX}" ]; then
                  echo "WARNING: timed out waiting for agentgateway (last=${code:-none}); continuing anyway"
                  break
                fi
                sleep "${INTERVAL}"
              done
              [ "${n}" -lt "${MAX}" ] && echo "agentgateway ready (HTTP ${code})"

              # 3) vMCP: MCP initialize POST through the proxy must return HTTP 200.
              #    This exercises the full path: proxy -> agentgateway -> ghostunnel -> host ToolHive vMCP.
              n=0
              body='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"wait-deps","version":"0"}}}'
              echo "waiting for vMCP initialize (200) at ${VMCP}..."
              until code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 \
                      -X POST \
                      -H "Authorization: Bearer ${AGENTGATEWAY_API_KEY}" \
                      -H "Accept: application/json, text/event-stream" \
                      -H "Content-Type: application/json" \
                      -d "${body}" \
                      "${VMCP}" 2>/dev/null) \
                    && [ "${code}" = "200" ]; do
                n=$((n+1))
                if [ "${n}" -ge "${MAX}" ]; then
                  echo "WARNING: vMCP did not return 200 (last=${code:-none}); continuing anyway"
                  break
                fi
                sleep "${INTERVAL}"
              done
              [ "${n}" -lt "${MAX}" ] && echo "vMCP ready (HTTP ${code})"

              echo "wait-deps: dependency checks complete"
              exit 0
          env:
            - name: http_proxy
              value: http://egress-proxy.egress-proxy.svc.cluster.local:8080
            - name: https_proxy
              value: http://egress-proxy.egress-proxy.svc.cluster.local:8080
            - name: HTTP_PROXY
              value: http://egress-proxy.egress-proxy.svc.cluster.local:8080
            - name: HTTPS_PROXY
              value: http://egress-proxy.egress-proxy.svc.cluster.local:8080
            # Only loopback bypasses the proxy — agentgateway hostname must egress via the proxy.
            - name: no_proxy
              value: 127.0.0.1,localhost
            - name: NO_PROXY
              value: 127.0.0.1,localhost
            - name: TMPDIR
              value: /tmp
            - name: AGENTGATEWAY_API_KEY
              valueFrom:
                secretKeyRef:
                  name: {{ include "vicegerent-agent.name" . }}-agentgateway-api-key
                  key: api-key
                  optional: false
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            runAsNonRoot: true
            capabilities:
              drop: [ALL]
          volumeMounts:
            - name: tmp
              mountPath: /tmp
      containers:
        - name: {{ include "vicegerent-agent.name" . }}
          image: {{ .Values.image.repository }}:{{ .Values.image.tag }}
          imagePullPolicy: Always
          args: [gateway]
          env:
            - name: HERMES_DASHBOARD
              value: '1'
            - name: HERMES_DASHBOARD_HOST
              value: 0.0.0.0
            - name: HERMES_DASHBOARD_PORT
              value: '9119'
            - name: HERMES_DASHBOARD_BASIC_AUTH_USERNAME
              valueFrom:
                fieldRef:
                  fieldPath: metadata.name
            - name: HERMES_DASHBOARD_BASIC_AUTH_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: {{ include "vicegerent-agent.name" . }}-secrets
                  key: password
                  optional: false
            - name: HERMES_DASHBOARD_BASIC_AUTH_SECRET
              valueFrom:
                secretKeyRef:
                  name: {{ include "vicegerent-agent.name" . }}-secrets
                  key: signing-secret
                  optional: false
            - name: SANDBOX_NAME
              valueFrom:
                fieldRef:
                  fieldPath: metadata.name
            - name: HERMES_HOME
              value: /opt/data
            - name: HERMES_SLACK_COMMAND_NAME
              value: {{ .Values.slack.commandName }}
            # Route all HTTP(S) traffic through the GET-only MITM proxy.
            - name: http_proxy
              value: http://egress-proxy.egress-proxy.svc.cluster.local:8080
            - name: https_proxy
              value: http://egress-proxy.egress-proxy.svc.cluster.local:8080
            - name: HTTP_PROXY
              value: http://egress-proxy.egress-proxy.svc.cluster.local:8080
            - name: HTTPS_PROXY
              value: http://egress-proxy.egress-proxy.svc.cluster.local:8080
            # Trust the proxy CA across all tooling that respects these env vars.
            - name: SSL_CERT_FILE
              value: /opt/data/certs/ca-bundle.crt
            - name: REQUESTS_CA_BUNDLE
              value: /opt/data/certs/ca-bundle.crt
            - name: CURL_CA_BUNDLE
              value: /opt/data/certs/ca-bundle.crt
            - name: GIT_SSL_CAINFO
              value: /opt/data/certs/ca-bundle.crt
            - name: NODE_EXTRA_CA_CERTS
              value: /opt/data/certs/ca-bundle.crt
            - name: PIP_CERT
              value: /opt/data/certs/ca-bundle.crt
            # Slack bypasses the proxy — Socket Mode + Web API require POST + WebSocket.
            # Loopback stays direct. All other destinations (agentgateway, searxng, internet)
            # flow through the scrubbing proxy so secrets are redacted before forwarding.
            - name: no_proxy
              value: 127.0.0.1,localhost,slack.com,.slack.com
            - name: NO_PROXY
              value: 127.0.0.1,localhost,slack.com,.slack.com
            - name: SEARXNG_URL
              value: http://searxng.searxng.svc.cluster.local:8080
            # Config homes on PVC (seeded by seed-data) to stay writable under readOnlyRootFilesystem.
            - name: CODEX_HOME
              value: /opt/data/.codex
            - name: CLAUDE_CONFIG_DIR
              value: /opt/data/.claude
            - name: TMPDIR
              value: /tmp
            - name: PYTHONDONTWRITEBYTECODE
              value: '1'
            - name: GIT_SSH_COMMAND
              value: ssh -i /opt/hermes-ssh/hermes_agent_ed25519 -o StrictHostKeyChecking=accept-new
                -o UserKnownHostsFile=/opt/data/.ssh/known_hosts
            - name: ANTHROPIC_API_KEY
              valueFrom:
                secretKeyRef:
                  name: {{ include "vicegerent-agent.name" . }}-agentgateway-api-key
                  key: api-key
                  optional: false
            - name: OPENAI_API_KEY
              valueFrom:
                secretKeyRef:
                  name: {{ include "vicegerent-agent.name" . }}-agentgateway-api-key
                  key: api-key
                  optional: false
            # TODO: haiku is overkill for mnemosyne consolidation; replace with a cheap OpenAI model once org tokens are available.
            - name: MNEMOSYNE_LLM_ENABLED
              value: 'true'
            - name: MNEMOSYNE_LLM_BASE_URL
              value: http://agentgateway-proxy.agentgateway-system.svc.cluster.local/haiku-oai/v1
            - name: MNEMOSYNE_LLM_MODEL
              value: claude-haiku-4-5
            - name: MNEMOSYNE_LLM_API_KEY
              valueFrom:
                secretKeyRef:
                  name: {{ include "vicegerent-agent.name" . }}-agentgateway-api-key
                  key: api-key
                  optional: false
            - name: HF_HUB_OFFLINE
              value: '1'
            - name: HERMES_WRITE_SAFE_ROOT
              value: "/opt/data:/workspace:/tmp"
          envFrom:
            # All agent pod credentials: dashboard auth, SSH key, and optional Slack tokens.
            - secretRef:
                name: {{ include "vicegerent-agent.name" . }}-secrets
                optional: false
          ports:
            - containerPort: 8642
              name: api
            - containerPort: 9119
              name: dashboard
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: [ALL]
          volumeMounts:
            - name: runtime
              mountPath: /run
            - name: tmp
              mountPath: /tmp
            - name: data
              mountPath: /opt/data
            - name: models
              mountPath: /opt/data/home/.hermes/mnemosyne/models
            - name: gitrepos
              mountPath: /workspace
            - name: ssh-key
              mountPath: /opt/hermes-ssh
              readOnly: true
            - name: config
              mountPath: /reload/hermes-config
            - name: soul
              mountPath: /opt/data/SOUL.md
              subPath: SOUL.md
            - name: approval-policy
              mountPath: /opt/hermes/approval-policy.yaml
              subPath: approval-policy.yaml
              readOnly: true
      volumes:
        - name: runtime
          emptyDir: {}
        - name: tmp
          emptyDir: {}
        - name: config
          configMap:
            name: {{ include "vicegerent-agent.name" . }}-config
        - name: soul
          configMap:
            name: {{ include "vicegerent-agent.name" . }}-soul
        - name: approval-policy
          configMap:
            name: {{ include "vicegerent-agent.name" . }}-approval-policy
        - name: codex-config
          configMap:
            name: {{ include "vicegerent-agent.name" . }}-codex-config
        - name: claude-config
          configMap:
            name: {{ include "vicegerent-agent.name" . }}-claude-config
        - name: ssh-key
          secret:
            secretName: {{ include "vicegerent-agent.name" . }}-ssh-key  # pragma: allowlist secret
            defaultMode: 0400
            optional: true
        - name: egress-proxy-ca-cert
          secret:
            secretName: egress-proxy-ca-cert  # pragma: allowlist secret
            optional: false
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: [ReadWriteOnce]
        resources:
          requests:
            storage: {{ .Values.storage.data }}
    - metadata:
        name: gitrepos
      spec:
        accessModes: [ReadWriteOnce]
        resources:
          requests:
            storage: {{ .Values.storage.gitrepos }}
    - metadata:
        name: models
      spec:
        accessModes: [ReadWriteOnce]
        resources:
          requests:
            storage: {{ .Values.storage.models }}
{{- end -}}
