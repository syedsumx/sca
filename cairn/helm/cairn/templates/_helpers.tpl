{{/* ------------------------------------------------------------------ names */}}
{{- define "cairn.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "cairn.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* "<fullname>-<component>" */}}
{{- define "cairn.componentName" -}}
{{- printf "%s-%s" (include "cairn.fullname" .root) .component | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "cairn.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* ------------------------------------------------------------------ labels */}}
{{- define "cairn.labels" -}}
helm.sh/chart: {{ include "cairn.chart" . }}
app.kubernetes.io/part-of: cairn
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end -}}

{{- define "cairn.selectorLabels" -}}
app.kubernetes.io/name: {{ include "cairn.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "cairn.componentLabels" -}}
{{ include "cairn.labels" .root }}
{{ include "cairn.selectorLabels" . }}
{{- end -}}

{{- define "cairn.serviceAccountName" -}}
{{- if .Values.cairn.serviceAccount.create -}}
{{- default (include "cairn.fullname" .) .Values.cairn.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.cairn.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "cairn.tag" -}}
{{- default .Chart.AppVersion .Values.global.cairn.image.tag -}}
{{- end -}}

{{/* Control-plane image. */}}
{{- define "cairn.image" -}}
{{- $img := .Values.global.cairn.image -}}
{{- $repo := default (printf "%s/platform" .Values.global.cairn.registry) $img.repository -}}
{{- $digest := get (.Values.global.cairn.digests | default dict) (last (splitList "/" $repo)) -}}
{{- if $digest -}}{{ printf "%s@%s" $repo $digest }}{{- else -}}{{ printf "%s:%s" $repo (include "cairn.tag" .) }}{{- end -}}
{{- end -}}

{{/* Cairn image by name: (dict "root" $ "name" "core") */}}
{{- define "cairn.namedImage" -}}
{{- $digest := get (.root.Values.global.cairn.digests | default dict) .name -}}
{{- if $digest -}}{{ printf "%s/%s@%s" .root.Values.global.cairn.registry .name $digest }}
{{- else -}}{{ printf "%s/%s:%s" .root.Values.global.cairn.registry .name (include "cairn.tag" .root) }}{{- end -}}
{{- end -}}

{{/* ------------------------------------------------------------------ Cairn Core endpoints */}}
{{- define "cairn.coreHost" -}}
{{- printf "%s-core" (include "cairn.fullname" .) -}}
{{- end -}}

{{- define "cairn.coreUrl" -}}
{{- default (printf "http://%s:8080" (include "cairn.coreHost" .)) .Values.cairn.core.url -}}
{{- end -}}

{{- define "cairn.frontendUrl" -}}
{{- default (printf "http://%s-web:9002" (include "cairn.fullname" .)) .Values.cairn.core.frontendUrl -}}
{{- end -}}

{{- define "cairn.kafkaBootstrap" -}}
{{- default .Values.core.kafka.bootstrap .Values.cairn.kafka.bootstrap -}}
{{- end -}}

{{- define "cairn.schemaRegistryUrl" -}}
{{- default (printf "%s/schema-registry/api/" (include "cairn.coreUrl" .)) .Values.cairn.kafka.schemaRegistryUrl -}}
{{- end -}}

{{/* Secret-backed env entry: (dict "name" ENV "secret" NAME "key" KEY) */}}
{{- define "cairn.secretEnv" -}}
- name: {{ .name }}
  valueFrom:
    secretKeyRef: { name: {{ .secret }}, key: {{ .key }} }
{{- end -}}

{{/* Settings shared by the core service and the setup job. */}}
{{- define "cairn.coreEnv" -}}
{{- $c := .Values.core -}}
{{- $s := $c.secrets -}}
- name: CAIRN_DB_TYPE
  value: {{ $c.database.type | default "mysql" | quote }}
{{- if eq ($c.database.type | default "mysql") "postgres" }}
- name: CAIRN_DB_SSLMODE
  value: {{ $c.database.sslMode | default "prefer" | quote }}
{{- end }}
- name: CAIRN_DB_HOST
  value: {{ printf "%s:%v" $c.database.host $c.database.port | quote }}
- name: CAIRN_DB_NAME
  value: {{ $c.database.name | quote }}
- name: CAIRN_DB_USER
  value: {{ $c.database.user | quote }}
{{ include "cairn.secretEnv" (dict "name" "CAIRN_DB_PASSWORD" "secret" $s.name "key" $s.dbPasswordKey) }}
{{- with $c.search.indexPrefix }}
- name: INDEX_PREFIX
  value: {{ . | quote }}
{{- end }}
- name: CAIRN_SEARCH_HOST
  value: {{ $c.search.host | quote }}
- name: CAIRN_SEARCH_PORT
  value: {{ $c.search.port | quote }}
- name: CAIRN_SEARCH_USE_SSL
  value: {{ $c.search.useSsl | quote }}
{{- if $c.search.username }}
- name: CAIRN_SEARCH_USERNAME
  value: {{ $c.search.username | quote }}
{{- end }}
{{- if $c.search.passwordKey }}
{{ include "cairn.secretEnv" (dict "name" "CAIRN_SEARCH_PASSWORD" "secret" $s.name "key" $c.search.passwordKey) }}
{{- end }}
- name: CAIRN_KAFKA_BOOTSTRAP
  value: {{ $c.kafka.bootstrap | quote }}
{{ include "cairn.secretEnv" (dict "name" "CAIRN_SYSTEM_CLIENT_SECRET" "secret" $s.name "key" $s.systemClientSecretKey) }}
{{ include "cairn.secretEnv" (dict "name" "CAIRN_SESSION_SIGNING_KEY" "secret" $s.name "key" $s.sessionSigningKeyKey) }}
{{ include "cairn.secretEnv" (dict "name" "CAIRN_TOKEN_SALT" "secret" $s.name "key" $s.tokenSaltKey) }}
{{- end -}}

{{- define "cairn.policiesConfigMap" -}}
{{- if .Values.cairn.policies.existingConfigMap -}}
{{- .Values.cairn.policies.existingConfigMap -}}
{{- else if .Values.cairn.policies.files -}}
{{- printf "%s-policies" (include "cairn.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "cairn.residencyConfigMap" -}}
{{- if .Values.cairn.residency.rules -}}
{{- printf "%s-residency" (include "cairn.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "cairn.autoMonitorsConfigMap" -}}
{{- if .Values.cairn.quality.autoMonitors.existingConfigMap -}}
{{- .Values.cairn.quality.autoMonitors.existingConfigMap -}}
{{- else if .Values.cairn.quality.autoMonitors.files -}}
{{- printf "%s-auto-monitors" (include "cairn.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/* ------------------------------------------------------------------ shared env */}}
{{- define "cairn.env" -}}
{{- $s := .Values.cairn.secrets -}}
{{- include "cairn.observabilityReadEnv" . }}
- name: CAIRN_CORE_URL
  value: {{ include "cairn.coreUrl" . | quote }}
- name: CAIRN_FRONTEND_URL
  value: {{ include "cairn.frontendUrl" . | quote }}
# Service-account token, written by the core-token bootstrap Job and rotated by the token keeper.
# Mounted from Secret "<release>-core-token"; clients re-read the file when it changes.
- name: CAIRN_CORE_TOKEN_FILE
  value: /run/cairn/core_token
- name: CAIRN_SERVICE_TOKEN
  valueFrom:
    secretKeyRef: { name: {{ $s.name }}, key: {{ $s.serviceTokenKey }} }
- name: CAIRN_DATABASE_URL
  valueFrom:
    secretKeyRef: { name: {{ $s.name }}, key: {{ $s.databaseUrlKey }} }
- name: CAIRN_LICENCE_REQUIRED
  value: {{ .Values.cairn.licence.required | default false | quote }}
{{- with .Values.cairn.licence.tenantId }}
- name: CAIRN_TENANT_ID
  value: {{ . | quote }}
{{- end }}
{{- if .Values.cairn.licence.existingSecret }}
- name: CAIRN_LICENCE_FILE
  value: /run/cairn-licence/licence
{{- end }}
{{- with .Values.cairn.residency.region }}
- name: CAIRN_REGION
  value: {{ . | quote }}
{{- end }}
{{- if include "cairn.residencyConfigMap" . }}
- name: CAIRN_RESIDENCY_FILE
  value: /etc/cairn-residency/residency.json
{{- end }}
- name: CAIRN_AUTHZ_MODE
  value: {{ .Values.cairn.auth.mode | quote }}
- name: CAIRN_IMPORT_NATIVE_POLICIES
  value: {{ .Values.cairn.auth.importNativePolicies | quote }}
- name: CAIRN_DECISION_CACHE_TTL
  value: {{ .Values.cairn.auth.decisionCacheTtlSeconds | quote }}
- name: CAIRN_ROOT_USERS
  value: {{ join "," .Values.cairn.auth.rootUsers | quote }}
- name: CAIRN_POLICY_DIR
  value: /etc/cairn/config/policies
- name: CAIRN_KAFKA_BOOTSTRAP
  value: {{ include "cairn.kafkaBootstrap" . | quote }}
- name: CAIRN_SCHEMA_REGISTRY_URL
  value: {{ include "cairn.schemaRegistryUrl" . | quote }}
- name: CAIRN_GATEWAY_PUBLIC_URL
  value: {{ .Values.cairn.gateway.publicUrl | quote }}
- name: CAIRN_GATEWAY_KEY_DIR
  value: /var/lib/cairn/keys
{{- with .Values.cairn.jobs }}
{{- if eq .runtime "kubernetes" }}
- name: CAIRN_JOBS_RUNTIME
  value: kubernetes
- name: CAIRN_JOBS_K8S_NAMESPACE
  value: {{ .namespace | quote }}
- name: CAIRN_JOBS_IMAGE
  value: {{ .image | quote }}
- name: CAIRN_JOBS_K8S_RESOURCES
  value: {{ toJson .resources | quote }}
- name: CAIRN_JOBS_KEEP_DAYS
  value: {{ .keepDays | quote }}
{{- with .trinoUrl }}
- name: CAIRN_JOBS_TRINO_URL
  value: {{ . | quote }}
{{- end }}
{{- if .trinoCa.configMap }}
- name: CAIRN_JOBS_TRINO_CA_FILE
  value: /etc/cairn-jobs-trino-ca/{{ .trinoCa.key }}
{{- end }}
{{- else if .runtime }}
{{- fail "cairn.jobs.runtime: \"\" or kubernetes" }}
{{- end }}
{{- end }}
{{- with .Values.cairn.egress }}
{{- if .enforce }}
{{- if not .keySecret.name }}
{{- fail "cairn.egress.enforce needs cairn.egress.keySecret.name (the proxy's key, 16+ bytes)" }}
{{- end }}
- name: CAIRN_EGRESS_ENFORCE
  value: "true"
- name: CAIRN_EGRESS_PROXY_URL
  value: {{ printf "http://%s.%s.svc:3128" (include "cairn.componentName" (dict "root" $ "component" "egress-proxy")) $.Release.Namespace | quote }}
- name: CAIRN_EGRESS_PROXY_KEY
  valueFrom:
    secretKeyRef: { name: {{ .keySecret.name }}, key: {{ .keySecret.key }} }
- name: CAIRN_EGRESS_K8S_CAIRN_NAMESPACE
  value: {{ $.Release.Namespace | quote }}
{{- end }}
{{- end }}
{{- with .Values.cairn.agentApps }}
{{- if eq .runtime "kubernetes" }}
{{- $api := include "cairn.componentName" (dict "root" $ "component" "api") }}
- name: CAIRN_AGENT_APPS_RUNTIME
  value: kubernetes
- name: CAIRN_AGENT_APPS_CAIRN_URL
  value: {{ default (printf "http://%s.%s.svc:%v" $api $.Release.Namespace $.Values.cairn.api.port) .cairnUrl | quote }}
- name: CAIRN_AGENT_APPS_K8S_NAMESPACE
  value: {{ .kubernetes.namespace | quote }}
- name: CAIRN_AGENT_APPS_K8S_IMAGE
  value: {{ .kubernetes.image | quote }}
- name: CAIRN_AGENT_APPS_K8S_ACCESS
  value: {{ .kubernetes.access | quote }}
{{- if eq .kubernetes.access "node-port" }}
{{- if not .kubernetes.nodeAddress }}{{ fail "cairn.agentApps.kubernetes.nodeAddress: where Cairn reaches node ports" }}{{ end }}
- name: CAIRN_AGENT_APPS_K8S_NODE_ADDRESS
  value: {{ .kubernetes.nodeAddress | quote }}
{{- end }}
- name: CAIRN_AGENT_APPS_K8S_CAIRN_NAMESPACE
  value: {{ $.Release.Namespace | quote }}
{{- with .kubernetes.ingressFrom }}
- name: CAIRN_AGENT_APPS_K8S_INGRESS_FROM
  value: {{ toJson . | quote }}
{{- end }}
- name: CAIRN_AGENT_APPS_K8S_RESOURCES
  value: {{ toJson .kubernetes.resources | quote }}
{{- else if .runtime }}
{{- fail "cairn.agentApps.runtime: \"\" or kubernetes" }}
{{- end }}
{{- end }}
- name: CAIRN_LLM_PROVIDER
  value: {{ .Values.cairn.ai.llmProvider | quote }}
- name: CAIRN_AI_MODEL
  value: {{ .Values.cairn.ai.model | quote }}
- name: CAIRN_AI_EFFORT
  value: {{ .Values.cairn.ai.effort | quote }}
- name: CAIRN_OLLAMA_URL
  value: {{ .Values.cairn.ai.ollamaUrl | quote }}
- name: CAIRN_EMBEDDING_PROVIDER
  value: {{ .Values.cairn.ai.embeddingProvider | quote }}
- name: CAIRN_AI_AUDIT_PROMPTS
  value: {{ .Values.cairn.ai.auditPrompts | quote }}
{{- with .Values.cairn.ai.dailyBudgetUsd }}
- name: CAIRN_AI_DAILY_BUDGET_USD
  value: {{ . | quote }}
{{- end }}
{{- with .Values.cairn.ai.userDailyTokens }}
- name: CAIRN_AI_USER_DAILY_TOKENS
  value: {{ . | quote }}
{{- end }}
{{- with .Values.cairn.ai.prices }}
- name: CAIRN_AI_PRICES
  value: {{ toJson . | quote }}
{{- end }}
{{- if eq .Values.cairn.ai.llmProvider "anthropic" }}
- name: ANTHROPIC_API_KEY
  valueFrom:
    secretKeyRef: { name: {{ $s.name }}, key: {{ $s.anthropicApiKeyKey }} }
{{- end }}
- name: CAIRN_PUBLIC_URL
  value: {{ .Values.cairn.publicUrl | quote }}
- name: CAIRN_MASKING_PROXY_ENABLED
  value: {{ .Values.cairn.coreProxy.enabled | quote }}
- name: CAIRN_EMBED_ALLOWED_ORIGINS
  value: {{ join "," .Values.cairn.embeds.allowedOrigins | quote }}
- name: CAIRN_ACTION_LINK_SECRET
  valueFrom:
    secretKeyRef: { name: {{ $s.name }}, key: {{ $s.actionLinkSecretKey }} }
- name: CAIRN_EMBED_SIGNING_SECRET
  valueFrom:
    secretKeyRef: { name: {{ $s.name }}, key: {{ $s.embedSigningSecretKey }} }
{{- $i := .Values.cairn.integrations }}
- name: CAIRN_SLACK_API_URL
  value: {{ $i.slack.apiUrl | default "https://slack.com/api" | quote }}
- name: CAIRN_TEAMS_WEBHOOK_HOSTS
  value: {{ $i.teams.webhookHosts | quote }}
- name: CAIRN_WEBHOOK_ALLOW_PRIVATE
  value: {{ $i.webhooks.allowPrivateNetworks | quote }}
{{- if $i.slack.enabled }}
- name: CAIRN_SLACK_BOT_TOKEN
  valueFrom:
    secretKeyRef: { name: {{ $i.slack.existingSecret }}, key: {{ $i.slack.botTokenKey }} }
- name: CAIRN_SLACK_SIGNING_SECRET
  valueFrom:
    secretKeyRef: { name: {{ $i.slack.existingSecret }}, key: {{ $i.slack.signingSecretKey }} }
{{- with $i.slack.channelActor }}
- name: CAIRN_SLACK_CHANNEL_ACTOR
  value: {{ . | quote }}
{{- end }}
{{- end }}
{{- if $i.teams.enabled }}
- name: CAIRN_TEAMS_WEBHOOK_URL
  valueFrom:
    secretKeyRef: { name: {{ $i.teams.existingSecret }}, key: {{ $i.teams.webhookUrlKey }} }
{{- end }}
{{- with $i.teams.assistant }}
{{- if .enabled }}
{{- if not .channelActor }}
{{- fail "cairn.integrations.teams.assistant.channelActor is required: Teams answers are visible to the whole channel" }}
{{- end }}
- name: CAIRN_TEAMS_OUTGOING_SECRET
  valueFrom:
    secretKeyRef: { name: {{ $i.teams.existingSecret }}, key: {{ .outgoingSecretKey }} }
- name: CAIRN_TEAMS_CHANNEL_ACTOR
  value: {{ .channelActor | quote }}
{{- if .replyWebhookKey }}
- name: CAIRN_TEAMS_REPLY_WEBHOOK
  valueFrom:
    secretKeyRef: { name: {{ $i.teams.existingSecret }}, key: {{ .replyWebhookKey }} }
{{- end }}
{{- end }}
{{- end }}
{{- if $i.email.enabled }}
- name: CAIRN_SMTP_URL
  valueFrom:
    secretKeyRef: { name: {{ $i.email.existingSecret }}, key: {{ $i.email.smtpUrlKey }} }
{{- end }}
{{- with (.Values.cairn.samples | default dict).onStart }}
- name: CAIRN_SAMPLES_ON_START
  value: {{ . | quote }}
{{- end }}
{{- if .Values.cairn.ontology.enabled }}
{{- $o := .Values.cairn.ontology }}
- name: CAIRN_ONTOLOGY_URL
  value: {{ printf "http://%s:%v" (include "cairn.componentName" (dict "root" . "component" "ontology" "cfg" $o)) $o.port | quote }}
- name: CAIRN_ONTOLOGY_TOKEN
  valueFrom:
    secretKeyRef: { name: {{ $o.secret.name }}, key: {{ $o.secret.tokenKey }} }
- name: CAIRN_ONTOLOGY_INTERVAL_SECONDS
  value: {{ $o.intervalSeconds | quote }}
{{- with $o.sampleAs }}
- name: CAIRN_ONTOLOGY_SAMPLE_AS
  value: {{ . | quote }}
{{- end }}
{{- with $o.tableAllowRegex }}
- name: CAIRN_ONTOLOGY_TABLE_ALLOW_REGEX
  value: {{ . | quote }}
{{- end }}
{{- with $o.tableDenyRegex }}
- name: CAIRN_ONTOLOGY_TABLE_DENY_REGEX
  value: {{ . | quote }}
{{- end }}
{{- end }}
{{- with ((.Values.cairn.updates).feed) }}
- name: CAIRN_RELEASE_FEED
  value: {{ . | quote }}
- name: CAIRN_RELEASE_CHANNEL
  value: {{ (($.Values.cairn.updates).channel | default "stable") | quote }}
- name: CAIRN_RELEASE_THRESHOLD
  value: {{ (($.Values.cairn.updates).threshold | default 1) | quote }}
{{- if (($.Values.cairn.updates).caConfigMap) }}
- name: CAIRN_RELEASE_CA_FILE
  value: /etc/cairn/release-ca/ca.crt
{{- end }}
{{- end }}
{{- if and ((.Values.cairn.updates).feed) (((.Values.cairn.updates).agent).enabled) }}
- name: CAIRN_UPDATER_CRONJOB
  value: {{ include "cairn.componentName" (dict "root" $ "component" "updater" "cfg" (dict)) | quote }}
- name: CAIRN_UPDATER_NAMESPACE
  value: {{ $.Release.Namespace | quote }}
{{- end }}
{{- with ((.Values.cairn.aiGateway).cloudUpstreams) }}
- name: CAIRN_AI_GATEWAY_CLOUD_UPSTREAMS
  value: {{ toJson . | quote }}
{{- end }}
{{- with .Values.cairn.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/* ------------------------------------------------------------------ deployment
Renders a Cairn Deployment. Arguments (dict):
  root       – top-level context ($)
  component  – component name (api, pdp, gateway, …)
  cfg        – component values
  args       – container args (list) passed to the `cairn` entrypoint
  port       – optional container port (int); enables probes against GET <healthPath>
  healthPath – optional probe path (default /health)
  metricsPath – optional Prometheus scrape path (default /metrics)
  extraEnv   – optional list of extra env entries for this component
  tcpProbes  – optional: probe the port with TCP instead of GET (a port that speaks TLS)
  extraVolumes / extraVolumeMounts – optional lists
*/}}
{{- define "cairn.deployment" -}}
{{- $root := .root -}}
{{- $cfg := .cfg -}}
{{- $replicas := default 1 .replicas -}}
{{- $health := default "/health" .healthPath -}}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "cairn.componentName" . }}
  labels:
    {{- include "cairn.componentLabels" . | nindent 4 }}
spec:
  {{- if not (and $cfg.autoscaling $cfg.autoscaling.enabled) }}
  replicas: {{ $replicas }}
  {{- end }}
  revisionHistoryLimit: 5
  selector:
    matchLabels:
      {{- include "cairn.selectorLabels" . | nindent 6 }}
  {{- if .recreate }}
  strategy:
    type: Recreate
  {{- end }}
  template:
    metadata:
      labels:
        {{- include "cairn.componentLabels" . | nindent 8 }}
        {{- with $root.Values.cairn.podLabels }}
        {{- toYaml . | nindent 8 }}
        {{- end }}
      annotations:
        checksum/policies: {{ toYaml $root.Values.cairn.policies | sha256sum | trunc 16 }}
        {{- if or .port .metricsPort }}
        prometheus.io/scrape: "true"
        prometheus.io/port: {{ default .port .metricsPort | quote }}
        prometheus.io/path: {{ default "/metrics" .metricsPath }}
        {{- end }}
    spec:
      serviceAccountName: {{ include "cairn.serviceAccountName" $root }}
      automountServiceAccountToken: false
      {{- include "cairn.spread" (merge (dict "replicas" $replicas) $) | nindent 6 }}
      {{- with $root.Values.global.cairn.imagePullSecrets }}
      imagePullSecrets:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      securityContext:
        {{- toYaml $root.Values.cairn.podSecurityContext | nindent 8 }}
      containers:
        - name: {{ .component }}
          image: {{ include "cairn.image" $root }}
          imagePullPolicy: {{ $root.Values.global.cairn.image.pullPolicy }}
          {{- with .command }}
          command:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          args:
            {{- toYaml .args | nindent 12 }}
          env:
            {{- include "cairn.env" $root | nindent 12 }}
            {{- include "cairn.otelEnv" (dict "root" $root "service" (printf "cairn-%s" .component)) | nindent 12 }}
            {{- with .extraEnv }}
            {{- toYaml . | nindent 12 }}
            {{- end }}
            {{- if .metricsPort }}
            - name: CAIRN_METRICS_PORT
              value: {{ .metricsPort | quote }}
            {{- end }}
          {{- if and .metricsPort (not .port) }}
          ports:
            - name: metrics
              containerPort: {{ .metricsPort }}
              protocol: TCP
          {{- end }}
          {{- if .port }}
          ports:
            - name: http
              containerPort: {{ .port }}
              protocol: TCP
          readinessProbe:
            {{- if .tcpProbes }}
            tcpSocket: { port: http }      # TLS with client certificates: the kubelet cannot speak it
            {{- else }}
            httpGet: { path: {{ $health }}, port: http }
            {{- end }}
            periodSeconds: 5
            timeoutSeconds: 3
            failureThreshold: 3
          livenessProbe:
            {{- if .tcpProbes }}
            tcpSocket: { port: http }      # TLS with client certificates: the kubelet cannot speak it
            {{- else }}
            httpGet: { path: {{ $health }}, port: http }
            {{- end }}
            initialDelaySeconds: 20
            periodSeconds: 15
            timeoutSeconds: 5
            failureThreshold: 4
          startupProbe:
            {{- if .tcpProbes }}
            tcpSocket: { port: http }      # TLS with client certificates: the kubelet cannot speak it
            {{- else }}
            httpGet: { path: {{ $health }}, port: http }
            {{- end }}
            periodSeconds: 3
            failureThreshold: 40
          {{- end }}
          resources:
            {{- toYaml $cfg.resources | nindent 12 }}
          securityContext:
            {{- toYaml $root.Values.cairn.containerSecurityContext | nindent 12 }}
          volumeMounts:
            - name: tmp
              mountPath: /tmp
            - name: core-token
              mountPath: /run/cairn
              readOnly: true
            {{- if $root.Values.cairn.observability.read.credentialsSecret }}
            - name: observability-credentials
              mountPath: /run/cairn-observability
              readOnly: true
            {{- end }}
            {{- if include "cairn.policiesConfigMap" $root }}
            - name: policies
              mountPath: /etc/cairn/config/policies
              readOnly: true
            {{- end }}
            {{- if include "cairn.autoMonitorsConfigMap" $root }}
            - name: auto-monitors
              mountPath: /etc/cairn/config/quality
              readOnly: true
            {{- end }}
            {{- if include "cairn.residencyConfigMap" $root }}
            - name: residency
              mountPath: /etc/cairn-residency
              readOnly: true
            {{- end }}
            {{- if $root.Values.cairn.licence.existingSecret }}
            - name: licence
              mountPath: /run/cairn-licence
              readOnly: true
            {{- end }}
            {{- if and (eq $root.Values.cairn.jobs.runtime "kubernetes") $root.Values.cairn.jobs.trinoCa.configMap }}
            - name: jobs-trino-ca
              mountPath: /etc/cairn-jobs-trino-ca
              readOnly: true
            {{- end }}
            {{- if and (($root.Values.cairn.updates).feed) (($root.Values.cairn.updates).caConfigMap) }}
            - name: release-ca
              mountPath: /etc/cairn/release-ca
              readOnly: true
            {{- end }}
            {{- with .extraVolumeMounts }}
            {{- toYaml . | nindent 12 }}
            {{- end }}
      volumes:
        - name: tmp
          emptyDir:
            sizeLimit: 512Mi
        - name: core-token
          secret:
            secretName: {{ include "cairn.coreTokenSecret" $root }}
            optional: true
        {{- with $root.Values.cairn.observability.read.credentialsSecret }}
        - name: observability-credentials
          secret:
            secretName: {{ . }}
            optional: true
        {{- end }}
        {{- if include "cairn.policiesConfigMap" $root }}
        - name: policies
          configMap:
            name: {{ include "cairn.policiesConfigMap" $root }}
        {{- end }}
        {{- if include "cairn.autoMonitorsConfigMap" $root }}
        - name: auto-monitors
          configMap:
            name: {{ include "cairn.autoMonitorsConfigMap" $root }}
        {{- end }}
        {{- if include "cairn.residencyConfigMap" $root }}
        - name: residency
          configMap:
            name: {{ include "cairn.residencyConfigMap" $root }}
        {{- end }}
        {{- with $root.Values.cairn.licence.existingSecret }}
        - name: licence
          secret:
            secretName: {{ . }}
            items: [{ key: {{ $root.Values.cairn.licence.secretKey | default "licence" }}, path: licence }]
        {{- end }}
        {{- if and (eq $root.Values.cairn.jobs.runtime "kubernetes") $root.Values.cairn.jobs.trinoCa.configMap }}
        - name: jobs-trino-ca
          configMap:
            name: {{ $root.Values.cairn.jobs.trinoCa.configMap }}
        {{- end }}
        {{- if and (($root.Values.cairn.updates).feed) (($root.Values.cairn.updates).caConfigMap) }}
        - name: release-ca
          configMap:
            name: {{ $root.Values.cairn.updates.caConfigMap }}
            items: [{ key: ca.crt, path: ca.crt }]
        {{- end }}
        {{- with .extraVolumes }}
        {{- toYaml . | nindent 8 }}
        {{- end }}
      {{- with $cfg.nodeSelector }}
      nodeSelector:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with $cfg.tolerations }}
      tolerations:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- if $cfg.affinity }}
      affinity:
        {{- toYaml $cfg.affinity | nindent 8 }}
      {{- else if gt (int $replicas) 1 }}
      affinity:
        podAntiAffinity:
          preferredDuringSchedulingIgnoredDuringExecution:
            - weight: 100
              podAffinityTerm:
                topologyKey: kubernetes.io/hostname
                labelSelector:
                  matchLabels:
                    {{- include "cairn.selectorLabels" . | nindent 20 }}
      {{- end }}
{{- end -}}

{{/* ------------------------------------------------------------------ service */}}
{{- define "cairn.service" -}}
apiVersion: v1
kind: Service
metadata:
  name: {{ include "cairn.componentName" . }}
  labels:
    {{- include "cairn.componentLabels" . | nindent 4 }}
  {{- with .annotations }}
  annotations:
    {{- toYaml . | nindent 4 }}
  {{- end }}
spec:
  type: {{ default "ClusterIP" .type }}
  selector:
    {{- include "cairn.selectorLabels" . | nindent 4 }}
  ports:
    - name: http
      port: {{ .port }}
      targetPort: http
      protocol: TCP
{{- end -}}

{{/* ------------------------------------------------------------------ hpa / pdb */}}
{{- define "cairn.hpa" -}}
{{- if and .cfg.autoscaling .cfg.autoscaling.enabled }}
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: {{ include "cairn.componentName" . }}
  labels:
    {{- include "cairn.componentLabels" . | nindent 4 }}
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: {{ include "cairn.componentName" . }}
  minReplicas: {{ .cfg.autoscaling.minReplicas }}
  maxReplicas: {{ .cfg.autoscaling.maxReplicas }}
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: {{ .cfg.autoscaling.targetCPUUtilizationPercentage }}
{{- end }}
{{- end -}}

{{/* Spread a component's replicas over nodes and zones (cairn.ha.spreadReplicas, > 1 replica).
Context: the component context plus "replicas". */}}
{{- define "cairn.spread" -}}
{{- if and .root.Values.cairn.ha.spreadReplicas (gt (int .replicas) 1) }}
topologySpreadConstraints:
  {{- range $key := list "kubernetes.io/hostname" "topology.kubernetes.io/zone" }}
  - maxSkew: 1
    topologyKey: {{ $key }}
    whenUnsatisfiable: ScheduleAnyway
    labelSelector:
      matchLabels:
        {{- include "cairn.selectorLabels" $ | nindent 8 }}
  {{- end }}
{{- end }}
{{- end -}}

{{- define "cairn.pdb" -}}
{{- if and .cfg.pdb .cfg.pdb.enabled }}
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: {{ include "cairn.componentName" . }}
  labels:
    {{- include "cairn.componentLabels" . | nindent 4 }}
spec:
  minAvailable: {{ .cfg.pdb.minAvailable }}
  selector:
    matchLabels:
      {{- include "cairn.selectorLabels" . | nindent 6 }}
{{- end }}
{{- end -}}

{{/* ------------------------------------------------------------------ Cairn Core pods
The engine images run as uid/gid 100 and write to their own home (web writes its login file at start),
so the root filesystem stays writable; everything else is locked down.
*/}}
{{- define "cairn.corePodSecurity" -}}
runAsNonRoot: true
runAsUser: 100
runAsGroup: 100
fsGroup: 100
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "cairn.coreContainerSecurity" -}}
allowPrivilegeEscalation: false
capabilities:
  drop: ["ALL"]
{{- end -}}

{{/*
Cairn Core Deployment. Arguments (dict): root, component, cfg (replicas/resources/scheduling), image
(core|web|executor), env (rendered YAML list), port + healthPath (optional), probeDelay,
volumes / volumeMounts (optional lists).
*/}}
{{- define "cairn.coreDeployment" -}}
{{- $root := .root -}}
{{- $cfg := .cfg -}}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "cairn.componentName" . }}
  labels:
    {{- include "cairn.componentLabels" . | nindent 4 }}
spec:
  replicas: {{ default 1 $cfg.replicas }}
  revisionHistoryLimit: 5
  selector:
    matchLabels:
      {{- include "cairn.selectorLabels" . | nindent 6 }}
  template:
    metadata:
      labels:
        {{- include "cairn.componentLabels" . | nindent 8 }}
    spec:
      automountServiceAccountToken: false
      {{- with $root.Values.global.cairn.imagePullSecrets }}
      imagePullSecrets:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      securityContext:
        {{- include "cairn.corePodSecurity" . | nindent 8 }}
      {{- with .initContainers }}
      initContainers:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      containers:
        - name: {{ .component }}
          image: {{ include "cairn.namedImage" (dict "root" $root "name" .image) }}
          imagePullPolicy: {{ $root.Values.global.cairn.image.pullPolicy }}
          env:
            {{- .env | nindent 12 }}
          {{- if .port }}
          ports:
            - name: http
              containerPort: {{ .port }}
              protocol: TCP
          startupProbe:
            httpGet: { path: {{ .healthPath }}, port: http }
            periodSeconds: 5
            failureThreshold: 60
          readinessProbe:
            httpGet: { path: {{ .healthPath }}, port: http }
            periodSeconds: 10
            timeoutSeconds: 5
            failureThreshold: 3
          livenessProbe:
            httpGet: { path: {{ .healthPath }}, port: http }
            periodSeconds: 15
            timeoutSeconds: 5
            failureThreshold: 6
          {{- end }}
          resources:
            {{- toYaml $cfg.resources | nindent 12 }}
          securityContext:
            {{- include "cairn.coreContainerSecurity" . | nindent 12 }}
          {{- with .volumeMounts }}
          volumeMounts:
            {{- toYaml . | nindent 12 }}
          {{- end }}
      {{- with .volumes }}
      volumes:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with $cfg.nodeSelector }}
      nodeSelector:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with $cfg.tolerations }}
      tolerations:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with $cfg.affinity }}
      affinity:
        {{- toYaml . | nindent 8 }}
      {{- end }}
{{- end -}}

{{- define "cairn.provisionerTokenSecret" -}}
{{- printf "%s-provisioner-token" (include "cairn.fullname" .) -}}
{{- end -}}

{{- define "cairn.coreTokenSecret" -}}
{{- printf "%s-core-token" (include "cairn.fullname" .) -}}
{{- end -}}

{{/* Cairn Store: CAIRN_STORE_DISKS from cairn.store.extraDisks ("/disks/<name>:<type>,..."). */}}
{{- define "cairn.storeDisks" -}}
{{- $out := list -}}
{{- range . -}}
{{- if not (regexMatch "^[a-z0-9-]{1,40}$" (toString .name)) -}}{{ fail "cairn.store.extraDisks: each name is 1-40 lowercase letters, digits and -" }}{{- end -}}
{{- if not (regexMatch "^[a-z0-9]{1,16}$" (toString .type)) -}}{{ fail "cairn.store.extraDisks: each type is 1-16 lowercase letters and digits" }}{{- end -}}
{{- $out = append $out (printf "/disks/%s:%s" .name .type) -}}
{{- end -}}
{{- join "," $out -}}
{{- end -}}


{{/* ------------------------------------------------------------------ observability (L141)
Where Cairn's components send OTLP/HTTP: cairn.observability.otlp.endpoint (your own Collector or backend), else the
bundled Collector when cairn.observability.enabled, else nowhere (telemetry off). */}}
{{- define "cairn.otelEndpoint" -}}
{{- $o := .Values.cairn.observability -}}
{{- if $o.otlp.endpoint -}}
{{- $o.otlp.endpoint -}}
{{- else if $o.enabled -}}
http://{{ include "cairn.fullname" . }}-otel-collector:4318
{{- end -}}
{{- end -}}

{{/* OpenTelemetry settings of one component (dict root, service). Nothing when telemetry is off. */}}
{{- define "cairn.otelEnv" -}}
{{- $root := .root -}}
{{- $o := $root.Values.cairn.observability -}}
{{- $ep := include "cairn.otelEndpoint" $root -}}
{{- if $ep }}
- name: OTEL_EXPORTER_OTLP_ENDPOINT
  value: {{ $ep | quote }}
- name: OTEL_EXPORTER_OTLP_PROTOCOL
  value: http/protobuf
- name: OTEL_SERVICE_NAME
  value: {{ .service | quote }}
- name: OTEL_RESOURCE_ATTRIBUTES
  value: {{ printf "service.namespace=cairn,k8s.namespace.name=%s,deployment.environment=%s" $root.Release.Namespace $o.environment | quote }}
- name: OTEL_TRACES_SAMPLER
  value: {{ $o.sampling.sampler | quote }}
- name: OTEL_TRACES_SAMPLER_ARG
  value: {{ $o.sampling.ratio | quote }}
{{- if $o.otlp.headersSecret }}
- name: OTEL_EXPORTER_OTLP_HEADERS
  valueFrom:
    secretKeyRef: { name: {{ $o.otlp.headersSecret }}, key: {{ $o.otlp.headersKey }} }
{{- end }}
{{- end }}
{{- end -}}

{{/* Where Cairn reads telemetry back (the health map, journeys, logs, SLOs and alerts). */}}
{{- define "cairn.observabilityReadEnv" -}}
{{- $o := .Values.cairn.observability -}}
{{- $r := $o.read -}}
{{- $base := include "cairn.fullname" . -}}
{{- $prom := $r.prometheusUrl | default (ternary (printf "http://%s-prometheus:9090" $base) "" $o.enabled) -}}
{{- $loki := $r.lokiUrl | default (ternary (printf "http://%s-loki:3100" $base) "" $o.enabled) -}}
{{- $tempo := $r.tempoUrl | default (ternary (printf "http://%s-tempo:3200" $base) "" $o.enabled) -}}
{{- range $k, $v := dict "CAIRN_OBS_PROMETHEUS_URL" $prom "CAIRN_OBS_LOKI_URL" $loki "CAIRN_OBS_TEMPO_URL" $tempo "CAIRN_OBS_TRACE_URL" $r.traceUrl "CAIRN_OBS_LOGS_URL" $r.logsUrl "CAIRN_OBS_VENDOR" $r.vendor }}
{{- if $v }}
- name: {{ $k }}
  value: {{ $v | quote }}
{{- end }}
{{- end }}
{{- if $r.credentialsSecret }}
{{- range $name := list "prometheus" "loki" "tempo" }}
{{- range $kind := list "token" "basic_auth" }}
- name: {{ printf "CAIRN_OBS_%s_%s_FILE" (upper $name) (upper $kind) }}
  value: {{ printf "/run/cairn-observability/%s-%s" $name (replace "_" "-" $kind) | quote }}
{{- end }}
{{- end }}
{{- end }}
{{- end -}}


{{/* Trino's tracing (config.properties): its spans to the Collector's OTLP gRPC port. */}}
{{- define "cairn.trinoTracing" -}}
{{- $o := .Values.cairn.observability -}}
{{- $ep := $o.trino.endpoint | default (ternary (printf "http://%s-otel-collector:4317" (include "cairn.fullname" .)) "" $o.enabled) -}}
{{- if and $o.trino.enabled $ep }}
tracing.enabled=true
tracing.exporter.endpoint={{ $ep }}
{{- end }}
{{- end -}}


{{/* The OpenTelemetry Java agent for a Cairn Core component (dict root, service), as YAML: env (a string of env
entries to append), initContainers, volumes, volumeMounts. Empty when telemetry or the agent is off. */}}
{{- define "cairn.javaAgent" -}}
{{- $root := .root -}}
{{- $o := $root.Values.cairn.observability -}}
{{- if and $o.javaAgent.enabled (include "cairn.otelEndpoint" $root) }}
env: |
{{- include "cairn.otelEnv" (dict "root" $root "service" .service) | indent 2 }}
  - name: JAVA_TOOL_OPTIONS
    value: "-javaagent:/otel-agent/javaagent.jar"
  - name: OTEL_INSTRUMENTATION_COMMON_DB_STATEMENT_SANITIZER_ENABLED
    value: "true"
  - name: OTEL_INSTRUMENTATION_HTTP_SERVER_CAPTURE_REQUEST_HEADERS
    value: ""
  - name: OTEL_LOGS_EXPORTER
    value: otlp
initContainers:
  - name: otel-java-agent
    image: {{ $o.javaAgent.image }}
    command: ["cp", "/javaagent.jar", "/otel-agent/javaagent.jar"]
    securityContext:
      allowPrivilegeEscalation: false
      readOnlyRootFilesystem: true
      runAsNonRoot: true
      capabilities: { drop: ["ALL"] }
    resources:
      requests: { cpu: 10m, memory: 32Mi }
      limits: { memory: 64Mi }
    volumeMounts:
      - { name: otel-agent, mountPath: /otel-agent }
volumes:
  - name: otel-agent
    emptyDir: { sizeLimit: 64Mi }
volumeMounts:
  - { name: otel-agent, mountPath: /otel-agent, readOnly: true }
{{- end }}
{{- end -}}

{{/* Managed Postgres (L140): the environment of the processes that run instances (API and scheduler), as JSON. */}}
{{- define "cairn.pgInstancesEnv" -}}
{{- $env := list }}
{{- with .Values.cairn.databases.instances }}{{ if .runtime }}
{{- $env = append $env (dict "name" "CAIRN_PG_RUNTIME" "value" .runtime) }}
{{- $env = append $env (dict "name" "CAIRN_PG_INSTANCE_KEY" "valueFrom" (dict "secretKeyRef" (dict "name" (required "cairn.databases.instances.keySecret.name" .keySecret.name) "key" (.keySecret.key | default "key")))) }}
{{- if eq .runtime "kubernetes" }}
{{- $env = append $env (dict "name" "CAIRN_PG_K8S_NAMESPACE" "value" .kubernetes.namespace) }}
{{- $env = append $env (dict "name" "CAIRN_PG_K8S_ACCESS" "value" .kubernetes.access) }}
{{- with .kubernetes.nodeAddress }}{{ $env = append $env (dict "name" "CAIRN_PG_K8S_NODE_ADDRESS" "value" .) }}{{ end }}
{{- with .kubernetes.storageClass }}{{ $env = append $env (dict "name" "CAIRN_PG_K8S_STORAGE_CLASS" "value" .) }}{{ end }}
{{- with .kubernetes.images }}{{ $env = append $env (dict "name" "CAIRN_PG_K8S_IMAGES" "value" (toJson .)) }}{{ end }}
{{- $env = append $env (dict "name" "CAIRN_PG_K8S_BACKUP_SECRET" "value" .kubernetes.backupSecret) }}
{{- end }}
{{- if .backups.enabled }}
{{- $endpoint := .backups.endpoint | default (printf "http%s://%s-store:%v" (ternary "s" "" (not (empty $.Values.cairn.store.tlsSecret))) (include "cairn.fullname" $) $.Values.cairn.store.port) }}
{{- $store := dict "endpoint" $endpoint "bucket" .backups.bucket "region" .backups.region }}
{{- with .backups.endpointForInstances }}{{ $_ := set $store "endpoint_for_instances" . }}{{ end }}
{{- $env = append $env (dict "name" "CAIRN_PG_BACKUP_STORE" "value" (toJson $store)) }}
{{- $env = append $env (dict "name" "CAIRN_PG_BACKUP_ACCESS_KEY" "valueFrom" (dict "secretKeyRef" (dict "name" .backups.secret.name "key" .backups.secret.accessKeyKey))) }}
{{- $env = append $env (dict "name" "CAIRN_PG_BACKUP_SECRET_KEY" "valueFrom" (dict "secretKeyRef" (dict "name" .backups.secret.name "key" .backups.secret.secretKeyKey))) }}
{{- end }}
{{- end }}{{ end }}
{{- toJson $env }}
{{- end }}

{{/* Managed Airflow (L144): the environment of the processes that run deployments (API and scheduler), as JSON. */}}
{{- define "cairn.airflowDeploymentsEnv" -}}
{{- $env := list }}
{{- with .Values.cairn.airflowDeployments }}{{ if .runtime }}
{{- $env = append $env (dict "name" "CAIRN_AIRFLOW_RUNTIME" "value" .runtime) }}
{{- $env = append $env (dict "name" "CAIRN_AIRFLOW_DEPLOY_KEY" "valueFrom" (dict "secretKeyRef" (dict "name" (required "cairn.airflowDeployments.keySecret.name" .keySecret.name) "key" (.keySecret.key | default "key")))) }}
{{- $env = append $env (dict "name" "CAIRN_AIRFLOW_CAIRN_URL" "value" (.cairnUrl | default (printf "http://%s-api.%s.svc:%v" (include "cairn.fullname" $) $.Release.Namespace $.Values.cairn.api.port))) }}
{{- with .publicUrl }}{{ $env = append $env (dict "name" "CAIRN_AIRFLOW_PUBLIC_URL" "value" .) }}{{ end }}
{{- with .trino }}{{ $env = append $env (dict "name" "CAIRN_AIRFLOW_TRINO" "value" (toJson .)) }}{{ end }}
{{- with .otelCollector }}{{ $env = append $env (dict "name" "CAIRN_AIRFLOW_OTEL_COLLECTOR" "value" .) }}{{ end }}
{{- if .selfApprove }}{{ $env = append $env (dict "name" "CAIRN_AIRFLOW_SELF_APPROVE" "value" "true") }}{{ end }}
{{- $endpoint := .bundles.endpoint | default (printf "http%s://%s-store:%v" (ternary "s" "" (not (empty $.Values.cairn.store.tlsSecret))) (include "cairn.fullname" $) $.Values.cairn.store.port) }}
{{- $env = append $env (dict "name" "CAIRN_AIRFLOW_BUNDLE_STORE" "value" (toJson (dict "endpoint" $endpoint "bucket" .bundles.bucket "region" .bundles.region))) }}
{{- $env = append $env (dict "name" "CAIRN_AIRFLOW_BUNDLE_ACCESS_KEY" "valueFrom" (dict "secretKeyRef" (dict "name" .bundles.secret.name "key" .bundles.secret.accessKeyKey))) }}
{{- $env = append $env (dict "name" "CAIRN_AIRFLOW_BUNDLE_SECRET_KEY" "valueFrom" (dict "secretKeyRef" (dict "name" .bundles.secret.name "key" .bundles.secret.secretKeyKey))) }}
{{- if eq .runtime "kubernetes" }}
{{- $env = append $env (dict "name" "CAIRN_AIRFLOW_K8S_NAMESPACE_PREFIX" "value" .kubernetes.namespacePrefix) }}
{{- $env = append $env (dict "name" "CAIRN_AIRFLOW_K8S_URL_TEMPLATE" "value" .kubernetes.urlTemplate) }}
{{- $env = append $env (dict "name" "CAIRN_AIRFLOW_K8S_INGRESS_NAMESPACE" "value" .kubernetes.ingressNamespace) }}
{{- $env = append $env (dict "name" "CAIRN_AIRFLOW_K8S_CAIRN_NAMESPACE" "value" $.Release.Namespace) }}
{{- $env = append $env (dict "name" "CAIRN_AIRFLOW_K8S_WORKLOAD_ISSUER" "value" .kubernetes.workloadIssuer) }}
{{- with .kubernetes.ingressClass }}{{ $env = append $env (dict "name" "CAIRN_AIRFLOW_K8S_INGRESS_CLASS" "value" .) }}{{ end }}
{{- with .kubernetes.images }}{{ $env = append $env (dict "name" "CAIRN_AIRFLOW_K8S_IMAGES" "value" (toJson .)) }}{{ end }}
{{- with .kubernetes.storageClass }}{{ $env = append $env (dict "name" "CAIRN_AIRFLOW_K8S_STORAGE_CLASS" "value" .) }}{{ end }}
{{- with .kubernetes.dbImage }}{{ $env = append $env (dict "name" "CAIRN_AIRFLOW_K8S_DB_IMAGE" "value" .) }}{{ end }}
{{- $env = append $env (dict "name" "CAIRN_AIRFLOW_K8S_HELM" "value" "/usr/local/bin/helm") }}
{{- $env = append $env (dict "name" "CAIRN_AIRFLOW_K8S_CHART" "value" "/usr/share/cairn/airflow-chart.tgz") }}
{{- end }}
{{- end }}{{ end }}
{{- toJson $env }}
{{- end }}

{{/* Managed NiFi (L152): the environment of the processes that run deployments and sync flows (API and scheduler),
     as JSON. */}}
{{- define "cairn.nifiDeploymentsEnv" -}}
{{- $env := list }}
{{- with .Values.cairn.nifiDeployments }}{{ if .runtime }}
{{- $env = append $env (dict "name" "CAIRN_NIFI_RUNTIME" "value" .runtime) }}
{{- $env = append $env (dict "name" "CAIRN_NIFI_DEPLOY_KEY" "valueFrom" (dict "secretKeyRef" (dict "name" (required "cairn.nifiDeployments.keySecret.name" .keySecret.name) "key" (.keySecret.key | default "key")))) }}
{{- $env = append $env (dict "name" "CAIRN_NIFI_CAIRN_URL" "value" (.cairnUrl | default (printf "http://%s-api.%s.svc:%v" (include "cairn.fullname" $) $.Release.Namespace $.Values.cairn.api.port))) }}
{{- with .publicUrl }}{{ $env = append $env (dict "name" "CAIRN_NIFI_PUBLIC_URL" "value" .) }}{{ end }}
{{- $env = append $env (dict "name" "CAIRN_NIFI_PROXY_PREFIX" "value" (.proxyPrefix | default "/cairn/api")) }}
{{- with .catalogUrl }}{{ $env = append $env (dict "name" "CAIRN_NIFI_CATALOG_URL" "value" .) }}{{ end }}
{{- with .storeUrl }}{{ $env = append $env (dict "name" "CAIRN_NIFI_STORE_URL" "value" .) }}{{ end }}
{{- $env = append $env (dict "name" "CAIRN_NIFI_SYNC_SECONDS" "value" (toString (.syncSeconds | default 300))) }}
{{- if eq .runtime "kubernetes" }}
{{- $env = append $env (dict "name" "CAIRN_NIFI_K8S_NAMESPACE_PREFIX" "value" .kubernetes.namespacePrefix) }}
{{- $env = append $env (dict "name" "CAIRN_NIFI_K8S_CAIRN_NAMESPACE" "value" $.Release.Namespace) }}
{{- with .kubernetes.images }}{{ $env = append $env (dict "name" "CAIRN_NIFI_K8S_IMAGES" "value" (toJson .)) }}{{ end }}
{{- with .kubernetes.storageClass }}{{ $env = append $env (dict "name" "CAIRN_NIFI_K8S_STORAGE_CLASS" "value" .) }}{{ end }}
{{- end }}
{{- end }}{{ end }}
{{- toJson $env }}
{{- end }}
